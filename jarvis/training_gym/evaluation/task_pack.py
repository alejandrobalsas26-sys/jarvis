"""training_gym/evaluation/task_pack.py — V69 M62 S3C: what the model sees, and what it must not.

THE SPLIT THAT MATTERS
----------------------
A held-out evaluation record contains two things that must never travel together: the
question, and the answer. If the answer reaches the model, the score measures reading
comprehension of the prompt. If the answer reaches the *backend* at all — even in a field
the backend ignores — then one refactor away it reaches the model.

So this module produces TWO objects from one source record:

  * :class:`EvaluationTask` — the model-facing half. It is the only thing an
    :class:`~training_gym.evaluation.backend.EvaluationRequest` can carry. It has **no
    field** that can hold a target, and a task whose visible material still contains the
    target's text is refused at construction.
  * :class:`HiddenTarget`, held in a :class:`HiddenTargetStore` — the scoring half. The
    store is never passed to a backend, never serialised into a generation artefact, and
    is looked up only after both arms have finished generating.

The isolation is structural. ``EvaluationTask`` cannot express a target because the
dataclass has nowhere to put one — that is a stronger guarantee than a rule saying not to.

EXPOSURE THROUGH SCHEMAS
------------------------
The subtler leak is not the target field; it is the JSON schema. ``{"verdict": {"const":
"malicious"}}`` publishes the answer while looking like a validation rule, and so do
``enum`` with one member, ``default``, ``examples`` and a ``description`` that spells the
expected value out. :func:`answer_exposure` walks the expected-output schema AND the tool
catalogue looking for exactly that, and its findings are refusals rather than warnings.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ..datasets.candidate import DatasetCandidate, DatasetSplit, require_digest
from ..policies import credential_shaped
from ..schemas import (
    body_free_repr,
    SchemaError,
    SensitivityClass,
    assert_no_private_content,
    canonical_json,
    require_bool,
    require_enum,
    require_id,
    require_int,
    require_str_tuple,
    require_text,
    scan_private_content,
    sha256_obj,
    sha256_text,
    short,
)
from ..task_spec import GRADER_IDS, TaskFamily

#: Bumped when the task pack's shape changes.
TASK_PACK_VERSION = "m62.evaluation_task_pack.1"

#: Splits an evaluation may draw authoritative tasks from.
EVALUABLE_SPLITS: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.VALIDATION, DatasetSplit.HIDDEN_EVALUATION,
    DatasetSplit.SECURITY_REGRESSION, DatasetSplit.ADVERSARIAL})

#: Splits without which a headline improvement claim is not supportable.
MANDATORY_SPLITS: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION})

#: TRAIN may be looked at, and may never decide anything. A model scoring well on what it
#: was fitted on is the expected outcome, not evidence.
DIAGNOSTIC_ONLY_SPLITS: frozenset[DatasetSplit] = frozenset({DatasetSplit.TRAIN})

#: QUARANTINE material was set aside for a reason. It is never model input.
NEVER_EVALUABLE_SPLITS: frozenset[DatasetSplit] = frozenset({DatasetSplit.QUARANTINE})

#: Splits whose results may contribute to candidate eligibility.
ELIGIBILITY_SPLITS: frozenset[DatasetSplit] = EVALUABLE_SPLITS

#: Bounds. A pack larger than this is a config error, not an ambition.
MAX_PACK_TASKS = 5_000
MAX_TASK_PROMPT_CHARS = 20_000
MAX_TASK_TOOL_SCHEMAS = 32
MAX_EVIDENCE_IDS = 128
MAX_HIDDEN_TARGET_CHARS = 20_000

#: Schema keys that publish an expected value whatever they contain. ``const`` and
#: ``default`` name one permitted value; ``example`` shows a correct one; the rest are
#: answer fields wearing a schema's clothes. None of these has a non-leaking use in a
#: model-facing evaluation schema.
ALWAYS_ANSWER_BEARING_KEYS: tuple[str, ...] = (
    "const", "default", "example", "examples", "expected", "expected_output",
    "answer", "target", "solution", "gold", "ground_truth", "correct",
    "reference_answer", "rubric",
)

#: Free-text schema keys. These have a legitimate use — but they are also where an
#: author writes "must be 'malicious'" and calls it documentation, so their VALUES are
#: scanned against the target in :func:`schema_target_leak`.
NARRATIVE_SCHEMA_KEYS: tuple[str, ...] = ("description", "title", "comment", "$comment")

#: The shortest target fragment worth searching a prompt for. Below this, a match is a
#: coincidence between two common words rather than a leak.
MIN_LEAK_FRAGMENT_CHARS = 24


class TaskPackError(SchemaError):
    """A task pack that would expose an answer or measure the wrong thing."""


class HiddenTargetError(SchemaError):
    """A hidden target that is not bound to exactly one task."""


class EvaluationTaskKind(str, Enum):
    """What a task asks for, in the vocabulary the graders and metrics both use."""

    #: The model should produce an answer.
    COMPLETION = "completion"
    #: The model should refuse. A completion here is an unsafe acceptance.
    REQUIRED_REFUSAL = "required_refusal"
    #: The model should NOT refuse. A refusal here is over-refusal.
    REQUIRED_COMPLETION = "required_completion"


# ══════════════════════════════════════════════════════════════════════════════
#  Answer exposure
# ══════════════════════════════════════════════════════════════════════════════
def answer_exposure(payload: object, *, path: str = "schema",
                    depth: int = 0) -> tuple[str, ...]:
    """Structural exposure: keys in *payload* that publish an expected value.

    Reports the PATH, never the value — a finding that quoted the leaked answer would
    itself become a place the answer is written down.

    ``enum`` is judged by arity rather than banned. A two-member enum is a genuine
    constraint ("the verdict is one of these"); a ONE-member enum is ``const`` spelled
    differently, and it hands the model the answer.
    """
    if depth > 12:
        return (f"{path}: nesting exceeds the reviewable depth",)
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key)
            here = f"{path}.{name}"
            if name.lower() in ALWAYS_ANSWER_BEARING_KEYS:
                found.append(here)
                continue
            if name.lower() == "enum":
                members = value if isinstance(value, (list, tuple)) else [value]
                if len({canonical_json(m) for m in members}) < 2:
                    found.append(f"{here} (single-member enum is a const)")
                continue
            found.extend(answer_exposure(value, path=here, depth=depth + 1))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            found.extend(answer_exposure(value, path=f"{path}[{i}]", depth=depth + 1))
    return tuple(found)


def schema_target_leak(payload: object, target: str, *, path: str = "schema",
                       depth: int = 0) -> tuple[str, ...]:
    """Paths in *payload* whose free text spells the target out.

    This is the check ``answer_exposure`` structurally cannot make: a ``description``
    reading "the correct verdict is malicious" is well-formed JSON Schema and publishes
    the answer. It needs the target to detect, so it runs where the target is in scope —
    in :func:`build_task`, never inside the model-facing task itself.
    """
    if depth > 12 or not _fragments(target):
        return ()
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}"
            if isinstance(value, str) and target_leaked_into(value, target):
                found.append(here)
            else:
                found.extend(schema_target_leak(value, target, path=here,
                                                depth=depth + 1))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            here = f"{path}[{i}]"
            if isinstance(value, str) and target_leaked_into(value, target):
                found.append(here)
            else:
                found.extend(schema_target_leak(value, target, path=here,
                                                depth=depth + 1))
    elif isinstance(payload, str) and target_leaked_into(payload, target):
        found.append(path)
    return tuple(found)


def _fragments(text: str) -> tuple[str, ...]:
    """Distinctive slices of a target, long enough that a match is not a coincidence."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) < MIN_LEAK_FRAGMENT_CHARS:
        return ()
    slices = [cleaned[:96]]
    if len(cleaned) > 200:
        mid = len(cleaned) // 2
        slices.append(cleaned[mid:mid + 96])
    return tuple(s for s in slices if len(s) >= MIN_LEAK_FRAGMENT_CHARS)


def target_leaked_into(visible: str, target: str) -> bool:
    """Whether the model-facing material already contains the target."""
    haystack = re.sub(r"\s+", " ", str(visible or "")).strip().casefold()
    return any(frag.casefold() in haystack for frag in _fragments(target))


# ══════════════════════════════════════════════════════════════════════════════
#  The model-facing task
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EvaluationTask:
    """Everything the model is given, and — by construction — nothing else.

    There is deliberately no ``target``, ``expected_output``, ``rubric`` or ``reference``
    field. A future contributor who wants to pass an answer to a backend has to add one
    here, in a file whose docstring says why they must not, rather than setting an
    existing attribute nobody noticed.
    """

    task_id: str
    task_family: TaskFamily
    task_hash: str
    split: DatasetSplit
    kind: EvaluationTaskKind
    system_prompt: str
    user_prompt: str
    source_dataset_manifest_hash: str
    source_shard_hash: str
    input_record_hash: str
    evidence_ids: tuple[str, ...] = ()
    tool_schemas: tuple[dict, ...] = ()
    expected_output_schema: dict = field(default_factory=dict)
    grader_ids: tuple[str, ...] = ()
    mandatory_grader_ids: tuple[str, ...] = ()
    security_required: bool = False
    refusal_expected: bool = False
    evaluation_only: bool = True
    lineage_group: str = ""
    fixture_hashes: tuple[str, ...] = ()
    sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC
    pack_version: str = TASK_PACK_VERSION

    def __repr__(self) -> str:
        """Identity and digests only. See :func:`body_free_repr`.

        This class holds ``system_prompt`` and ``user_prompt``. The generated
        dataclass repr rendered both, and ``repr`` of any bound method of a pack
        holding these tasks rendered them too.
        """
        return body_free_repr(
            self, "task_id", "task_family", "split", "kind", "task_hash",
            "source_dataset_manifest_hash", "input_record_hash")

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "task_id", require_id(self.task_id, "task.task_id"))
        set_(self, "task_family", require_enum(self.task_family, TaskFamily,
                                               "task.task_family"))
        set_(self, "split", require_enum(self.split, DatasetSplit, "task.split"))
        set_(self, "kind", require_enum(self.kind, EvaluationTaskKind, "task.kind"))
        set_(self, "sensitivity", require_enum(self.sensitivity, SensitivityClass,
                                               "task.sensitivity"))
        for name in ("security_required", "refusal_expected", "evaluation_only"):
            set_(self, name, require_bool(getattr(self, name), f"task.{name}"))
        for name in ("task_hash", "source_dataset_manifest_hash", "source_shard_hash",
                     "input_record_hash"):
            set_(self, name, require_digest(getattr(self, name), f"task.{name}"))
        set_(self, "user_prompt", require_text(self.user_prompt, "task.user_prompt",
                                               max_len=MAX_TASK_PROMPT_CHARS))
        set_(self, "system_prompt", require_text(self.system_prompt,
                                                 "task.system_prompt", min_len=0,
                                                 max_len=MAX_TASK_PROMPT_CHARS))
        set_(self, "evidence_ids", require_str_tuple(self.evidence_ids,
                                                     "task.evidence_ids",
                                                     max_items=MAX_EVIDENCE_IDS))
        set_(self, "fixture_hashes", require_str_tuple(self.fixture_hashes,
                                                       "task.fixture_hashes",
                                                       max_items=MAX_EVIDENCE_IDS))
        set_(self, "grader_ids", require_str_tuple(self.grader_ids, "task.grader_ids",
                                                   max_items=len(GRADER_IDS)))
        set_(self, "mandatory_grader_ids",
             require_str_tuple(self.mandatory_grader_ids, "task.mandatory_grader_ids",
                               max_items=len(GRADER_IDS)))
        set_(self, "tool_schemas", tuple(dict(s) for s in (self.tool_schemas or ())))
        set_(self, "expected_output_schema", dict(self.expected_output_schema or {}))

        self._validate_split()
        self._validate_graders()
        self._validate_exposure()
        self._validate_privacy()

    def _validate_split(self) -> None:
        if self.split in NEVER_EVALUABLE_SPLITS:
            raise TaskPackError(
                f"task {self.task_id!r}: {self.split.value} material is never model "
                f"input; it was set aside because something about it was wrong")
        if self.split not in EVALUABLE_SPLITS and self.split not in DIAGNOSTIC_ONLY_SPLITS:
            raise TaskPackError(
                f"task {self.task_id!r}: split {self.split.value} is not an evaluation "
                f"split; allowed {sorted(s.value for s in EVALUABLE_SPLITS)}")
        if not self.evaluation_only:
            raise TaskPackError(
                f"task {self.task_id!r}: evaluation_only must remain true. A record that "
                f"can flow back into training is a record whose held-out status is "
                f"one promotion away from being lost")

    def _validate_graders(self) -> None:
        if len(self.tool_schemas) > MAX_TASK_TOOL_SCHEMAS:
            raise TaskPackError(f"task {self.task_id!r}: too many tool schemas")
        for grader_id in set(self.grader_ids) | set(self.mandatory_grader_ids):
            if grader_id not in GRADER_IDS:
                raise TaskPackError(
                    f"task {self.task_id!r}: unknown grader {grader_id!r}; a task may "
                    f"only name a grader this build implements, or the check silently "
                    f"does not happen")
        missing = set(self.mandatory_grader_ids) - set(self.grader_ids)
        if missing:
            raise TaskPackError(
                f"task {self.task_id!r}: mandatory grader(s) {sorted(missing)} are not "
                f"in the requested set; a mandatory check nobody runs blocks nothing")
        if self.security_required and not self.mandatory_grader_ids:
            raise TaskPackError(
                f"task {self.task_id!r}: declares a security requirement but names no "
                f"mandatory grader, so nothing would enforce it")
        if self.refusal_expected and self.kind is not EvaluationTaskKind.REQUIRED_REFUSAL:
            raise TaskPackError(
                f"task {self.task_id!r}: refusal_expected disagrees with kind "
                f"{self.kind.value}; the metric layer would classify it two ways")

    def _validate_exposure(self) -> None:
        exposure = answer_exposure(self.expected_output_schema, path="expected_schema")
        for i, schema in enumerate(self.tool_schemas):
            exposure += answer_exposure(schema, path=f"tool_schemas[{i}]")
        if exposure:
            raise TaskPackError(
                f"task {self.task_id!r}: the model-facing schema publishes the expected "
                f"answer at {sorted(set(exposure))[:6]}. A constraint that names the "
                f"correct value is the answer written in the grammar of a validator")

    def _validate_privacy(self) -> None:
        for name in ("system_prompt", "user_prompt"):
            text = getattr(self, name)
            found = scan_private_content(text)
            if found and found != ("scanner_unavailable",):
                raise TaskPackError(
                    f"task {self.task_id!r}: {name} carries private content "
                    f"({', '.join(found)}); an evaluation prompt is written into an "
                    f"artefact and shown to an operator")
        for evidence_id in self.evidence_ids:
            if credential_shaped(evidence_id):
                raise TaskPackError(
                    f"task {self.task_id!r}: evidence id {short(evidence_id)!r} is "
                    f"credential-shaped")

    # -- derived ---------------------------------------------------------------
    def to_dict(self) -> dict:
        """The canonical model-facing task. This IS what the backend receives."""
        return {
            "pack_version": self.pack_version,
            "task_id": self.task_id,
            "task_family": self.task_family.value,
            "task_hash": self.task_hash,
            "split": self.split.value,
            "kind": self.kind.value,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "source_dataset_manifest_hash": self.source_dataset_manifest_hash,
            "source_shard_hash": self.source_shard_hash,
            "input_record_hash": self.input_record_hash,
            "evidence_ids": list(self.evidence_ids),
            "tool_schemas": [dict(sorted(s.items())) for s in self.tool_schemas],
            "expected_output_schema": dict(sorted(self.expected_output_schema.items())),
            "grader_ids": sorted(self.grader_ids),
            "mandatory_grader_ids": sorted(self.mandatory_grader_ids),
            "security_required": self.security_required,
            "refusal_expected": self.refusal_expected,
            "evaluation_only": self.evaluation_only,
            "lineage_group": self.lineage_group,
            "fixture_hashes": list(self.fixture_hashes),
            "sensitivity": self.sensitivity.value,
        }

    def identity_hash(self) -> str:
        """Digest of everything the model sees. Both arms must hash identically."""
        return sha256_obj(self.to_dict())

    def prompt_hash(self) -> str:
        """Digest of the prompt alone, for the cheap parity assertion per task."""
        return sha256_text(canonical_json({
            "system_prompt": self.system_prompt, "user_prompt": self.user_prompt,
            "tool_schemas": [dict(sorted(s.items())) for s in self.tool_schemas],
            "expected_output_schema": dict(sorted(self.expected_output_schema.items())),
            "evidence_ids": list(self.evidence_ids)}))


# ══════════════════════════════════════════════════════════════════════════════
#  The hidden half
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class HiddenTarget:
    """The expected answer for one task. Never reachable from a backend request."""

    task_id: str
    task_hash: str
    target_text: str
    target_hash: str
    target_source: str = ""
    rubric: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        """Identity and digests only — never ``target_text``."""
        return body_free_repr(self, "task_id", "task_hash", "target_hash",
                              "target_source")

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "task_id", require_id(self.task_id, "hidden target.task_id"))
        set_(self, "task_hash", require_digest(self.task_hash, "hidden target.task_hash"))
        set_(self, "target_hash", require_digest(self.target_hash,
                                                 "hidden target.target_hash"))
        set_(self, "target_text", require_text(self.target_text,
                                               "hidden target.target_text", min_len=0,
                                               max_len=MAX_HIDDEN_TARGET_CHARS))
        set_(self, "rubric", dict(self.rubric or {}))
        recomputed = sha256_text(self.target_text)
        if recomputed != self.target_hash:
            raise HiddenTargetError(
                f"hidden target for {self.task_id!r}: the stored digest "
                f"{short(self.target_hash)} does not match the text "
                f"({short(recomputed)}); a target that can be edited without the digest "
                f"moving is a target that can be edited to match any answer")

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "task_hash": self.task_hash,
                "target_hash": self.target_hash, "target_source": self.target_source,
                "rubric": dict(sorted(self.rubric.items()))}


class HiddenTargetStore:
    """The scoring-side authority. Deliberately NOT a dataclass and NOT serialisable.

    ``to_dict`` returns digests only, so a store that accidentally reaches an artefact
    writer publishes hashes rather than answers. There is no method that returns every
    target at once, and lookup requires the task hash as well as the id — a caller who
    has only an id has not proved it is asking about the task it thinks it is.
    """

    __slots__ = ("_targets", "_generation", "_frozen")

    def __init__(self, *, generation: int = 1) -> None:
        self._targets: dict[str, HiddenTarget] = {}
        self._generation = require_int(generation, "hidden target store.generation",
                                       minimum=1, maximum=10_000)
        self._frozen = False

    def add(self, target: HiddenTarget) -> None:
        if self._frozen:
            raise HiddenTargetError(
                "hidden target store: frozen. A target added after the pack was hashed "
                "would not be covered by the plan digest")
        if not isinstance(target, HiddenTarget):
            raise HiddenTargetError("hidden target store: expected a HiddenTarget")
        if target.task_id in self._targets:
            raise HiddenTargetError(
                f"hidden target store: {target.task_id!r} already has a target; two "
                f"answers for one task means the scorer picks one")
        self._targets[target.task_id] = target

    def freeze(self) -> "HiddenTargetStore":
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def generation(self) -> int:
        return self._generation

    def __len__(self) -> int:
        return len(self._targets)

    def __contains__(self, task_id: object) -> bool:
        return str(task_id) in self._targets

    def task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._targets))

    def lookup(self, task_id: str, *, task_hash: str) -> HiddenTarget:
        """Retrieve a target, proving the caller means THIS task.

        Requiring the task hash is not ceremony. Task ids are stable across dataset
        versions; hashes are not. A scorer that looked up by id alone would happily grade
        version 2's answer against version 1's question.
        """
        target = self._targets.get(str(task_id))
        if target is None:
            raise HiddenTargetError(
                f"hidden target store: no target for {str(task_id)!r}; a task scored "
                f"against a missing target has not been scored")
        if target.task_hash != str(task_hash).strip().lower():
            raise HiddenTargetError(
                f"hidden target store: {task_id!r} is bound to task "
                f"{short(target.task_hash)}, not {short(str(task_hash))}")
        return target

    def to_dict(self) -> dict:
        """Digests only. There is no method on this class that exports target text."""
        return {"generation": self._generation, "count": len(self._targets),
                "targets": [t.to_dict() for t in
                            sorted(self._targets.values(), key=lambda t: t.task_id)]}

    def store_hash(self) -> str:
        return sha256_obj(self.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
#  The pack
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EvaluationTaskPack:
    """The frozen, ordered set of tasks. Both arms answer exactly this and no more."""

    tasks: tuple[EvaluationTask, ...]
    dataset_id: str
    dataset_version: str
    generation: int = 1
    pack_version: str = TASK_PACK_VERSION

    def __repr__(self) -> str:
        """Shape only. The tasks tuple is summarised by count, never rendered.

        This is the object whose bound-method repr leaked a held-out prompt body:
        ``repr(pack.pack_hash)`` expands to ``<bound method ... of {pack!r}>``.
        With this override that expansion is body-free.
        """
        return body_free_repr(self, "dataset_id", "dataset_version", "generation",
                              "pack_version", task_count=len(self.tasks))

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "dataset_id", require_id(self.dataset_id, "pack.dataset_id"))
        set_(self, "dataset_version", require_id(self.dataset_version,
                                                 "pack.dataset_version"))
        set_(self, "generation", require_int(self.generation, "pack.generation",
                                             minimum=1, maximum=10_000))
        tasks = tuple(self.tasks)
        if not tasks:
            raise TaskPackError(
                "task pack: zero tasks. An evaluation over nothing produces a perfect "
                "score over an empty denominator")
        if len(tasks) > MAX_PACK_TASKS:
            raise TaskPackError(f"task pack: {len(tasks)} tasks exceeds {MAX_PACK_TASKS}")
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        for task in tasks:
            if not isinstance(task, EvaluationTask):
                raise TaskPackError("task pack: every entry must be an EvaluationTask")
            if task.task_id in seen_ids:
                raise TaskPackError(
                    f"task pack: duplicate task id {task.task_id!r}; the same task "
                    f"counted twice weights it twice")
            if task.task_hash in seen_hashes:
                raise TaskPackError(
                    f"task pack: two tasks share hash {short(task.task_hash)}; a "
                    f"duplicated easy task inflates the pass rate for free")
            seen_ids.add(task.task_id)
            seen_hashes.add(task.task_hash)
        # Canonical order is by task hash, not by insertion: the pack digest must not
        # depend on the order a caller happened to append in.
        set_(self, "tasks", tuple(sorted(tasks, key=lambda t: (t.task_hash, t.task_id))))

    # -- views ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.tasks)

    def by_id(self, task_id: str) -> EvaluationTask:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise TaskPackError(f"task pack: no task {str(task_id)!r}")

    def splits(self) -> tuple[DatasetSplit, ...]:
        return tuple(sorted({t.split for t in self.tasks}, key=lambda s: s.value))

    def families(self) -> tuple[TaskFamily, ...]:
        return tuple(sorted({t.task_family for t in self.tasks}, key=lambda f: f.value))

    def counts_by_split(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.tasks:
            counts[task.split.value] = counts.get(task.split.value, 0) + 1
        return dict(sorted(counts.items()))

    def counts_by_family(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.tasks:
            counts[task.task_family.value] = counts.get(task.task_family.value, 0) + 1
        return dict(sorted(counts.items()))

    def eligibility_blockers(self, *,
                             mandatory_splits: frozenset[DatasetSplit] = MANDATORY_SPLITS
                             ) -> tuple[str, ...]:
        """Why this pack cannot support a candidate-eligibility decision."""
        problems: list[str] = []
        present = set(self.splits())
        for split in sorted(mandatory_splits, key=lambda s: s.value):
            if split not in present:
                problems.append(
                    f"the {split.value} split contributes no task; a missing mandatory "
                    f"split is INSUFFICIENT_EVIDENCE, never a pass")
        authoritative = present & ELIGIBILITY_SPLITS
        if not authoritative:
            problems.append(
                "no task comes from an authoritative evaluation split; TRAIN results are "
                "diagnostics and may not decide eligibility")
        if present <= DIAGNOSTIC_ONLY_SPLITS:
            problems.append(
                "every task comes from TRAIN; a model scoring well on what it was fitted "
                "on is the expected outcome, not evidence of improvement")
        if not any(t.kind is EvaluationTaskKind.REQUIRED_REFUSAL for t in self.tasks):
            problems.append(
                "no task requires a refusal; without one, a model that refuses "
                "everything cannot be distinguished from a model that is safe")
        if not any(t.kind is EvaluationTaskKind.REQUIRED_COMPLETION for t in self.tasks):
            problems.append(
                "no task requires a completion; without one, refusing everything scores "
                "perfectly on safety")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "pack_version": self.pack_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "generation": self.generation,
            "task_count": len(self.tasks),
            "counts_by_split": self.counts_by_split(),
            "counts_by_family": self.counts_by_family(),
            "task_hashes": [t.task_hash for t in self.tasks],
            "task_identity_hashes": [t.identity_hash() for t in self.tasks],
        }

    def pack_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "pack_hash": self.pack_hash(),
                  "eligibility_blockers": list(self.eligibility_blockers())}
        assert_no_private_content(record, label="evaluation task pack")
        return record

    def task_records(self) -> tuple[dict, ...]:
        """One line per model-facing task, for the ``task-pack.jsonl`` artefact."""
        return tuple(t.to_dict() for t in self.tasks)


# ══════════════════════════════════════════════════════════════════════════════
#  Construction from held-out dataset records
# ══════════════════════════════════════════════════════════════════════════════
def _kind_for(candidate: DatasetCandidate, split: DatasetSplit) -> EvaluationTaskKind:
    if candidate.task_family is TaskFamily.SAFETY_REFUSAL:
        return EvaluationTaskKind.REQUIRED_REFUSAL
    if split is DatasetSplit.SECURITY_REGRESSION:
        # A security-regression task that is not a refusal task is there to prove the
        # model still DOES the safe thing; refusing it is the failure mode.
        return EvaluationTaskKind.REQUIRED_COMPLETION
    return EvaluationTaskKind.COMPLETION


def build_task(candidate: DatasetCandidate, *, split: DatasetSplit,
               dataset_manifest_hash: str, shard_hash: str,
               grader_ids: Sequence[str], mandatory_grader_ids: Sequence[str],
               expected_output_schema: Mapping | None = None,
               tool_schemas: Sequence[Mapping] = (),
               kind: EvaluationTaskKind | None = None) -> tuple[EvaluationTask,
                                                                HiddenTarget]:
    """Split one held-out record into its model-facing and hidden halves.

    Returns both, so a caller cannot obtain the task without also being handed the target
    it must place in the store — the shape makes "forgot to hide the answer" impossible
    and "forgot to record the answer" loud.
    """
    if not isinstance(candidate, DatasetCandidate):
        raise TaskPackError(
            f"task: expected a DatasetCandidate, got {type(candidate).__name__}; a task "
            f"built from a raw dictionary has no verified provenance")
    split = require_enum(split, DatasetSplit, "task.split")
    resolved_kind = kind or _kind_for(candidate, split)
    system_prompt = str(candidate.system_message or "")
    user_prompt = str(candidate.user_prompt or "")

    # The leak check runs BEFORE the task object exists, so a leaking record can never be
    # momentarily represented as a valid task.
    for name, text in (("system_prompt", system_prompt), ("user_prompt", user_prompt)):
        if target_leaked_into(text, candidate.target_text):
            raise TaskPackError(
                f"task {candidate.task_id!r}: the {name} already contains the expected "
                f"answer; the model would be scored on copying rather than reasoning")
    leaks = schema_target_leak(expected_output_schema or {}, candidate.target_text,
                               path="expected_schema")
    for i, schema in enumerate(tool_schemas):
        leaks += schema_target_leak(schema, candidate.target_text,
                                    path=f"tool_schemas[{i}]")
    if leaks:
        raise TaskPackError(
            f"task {candidate.task_id!r}: schema free text spells the expected answer "
            f"out at {sorted(set(leaks))[:6]}; a description that names the correct "
            f"value is the answer filed under documentation")

    task = EvaluationTask(
        task_id=candidate.task_id,
        task_family=candidate.task_family,
        task_hash=candidate.task_hash,
        split=split,
        kind=resolved_kind,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        source_dataset_manifest_hash=dataset_manifest_hash,
        source_shard_hash=shard_hash,
        input_record_hash=sha256_obj({"system": system_prompt, "user": user_prompt}),
        evidence_ids=tuple(candidate.input_fixture_hashes),
        tool_schemas=tuple(dict(s) for s in tool_schemas),
        expected_output_schema=dict(expected_output_schema or {}),
        grader_ids=tuple(grader_ids),
        mandatory_grader_ids=tuple(mandatory_grader_ids),
        security_required=split is DatasetSplit.SECURITY_REGRESSION
        or candidate.task_family is TaskFamily.SAFETY_REFUSAL,
        refusal_expected=resolved_kind is EvaluationTaskKind.REQUIRED_REFUSAL,
        evaluation_only=True,
        lineage_group=str(candidate.lineage_group or ""),
        fixture_hashes=tuple(candidate.artifact_hashes),
        sensitivity=candidate.sensitivity,
    )
    target = HiddenTarget(
        task_id=candidate.task_id,
        task_hash=candidate.task_hash,
        target_text=candidate.target_text,
        target_hash=sha256_text(candidate.target_text),
        target_source=candidate.target_source.value,
    )
    return task, target


__all__ = [
    "ALWAYS_ANSWER_BEARING_KEYS", "DIAGNOSTIC_ONLY_SPLITS", "ELIGIBILITY_SPLITS",
    "EVALUABLE_SPLITS", "MANDATORY_SPLITS", "MAX_PACK_TASKS",
    "NARRATIVE_SCHEMA_KEYS", "NEVER_EVALUABLE_SPLITS", "TASK_PACK_VERSION",
    "EvaluationTask", "EvaluationTaskKind", "EvaluationTaskPack", "HiddenTarget",
    "HiddenTargetError", "HiddenTargetStore", "TaskPackError", "answer_exposure",
    "build_task", "schema_target_leak", "target_leaked_into",
]
