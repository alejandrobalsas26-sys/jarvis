"""training_gym/evaluation/pack_builder.py — V69 M62 S3E.1: promoted shards to a pack.

THE ORCHESTRATION THAT WAS MISSING
----------------------------------
:func:`training_gym.evaluation.task_pack.build_task` converts one candidate. Nothing
converted a *dataset*. The CLI therefore planned an evaluation from split *counts* read
out of a manifest and bound a placeholder digest where the pack hash belonged, which
meant a plan could be approved without anyone having built the thing it authorised.

This module closes that gap and nothing else. It reads a promoted, hash-verified
:class:`~training_gym.datasets.manifests.DatasetVersionManifest`, materialises the
held-out shards it names, and returns the model-facing pack together with the store
holding the answers.

THE SHAPE THAT KEEPS THE ANSWERS HIDDEN
---------------------------------------
``build_task`` hands back a task *and* its target, so a caller cannot obtain one without
the other. This module preserves that: it returns a :class:`BuiltPack` carrying both, the
store is frozen before it is returned, and every task is re-checked for exposure after
construction. The pack is what a model sees; the store is what only the scorer may open.

WHAT IT REFUSES
---------------
TRAIN, unconditionally — a model scoring well on what it was fitted on is the expected
outcome, not evidence. QUARANTINE, because that material was set aside for a reason. Any
candidate that is not ``PROMOTED``. Any duplicate id. Any family this repository has no
grader for. Any schema that names the answer. And a pack that cannot support the claim
an evaluation would make from it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..datasets.candidate import CandidateState, DatasetCandidate, DatasetSplit
from ..datasets.manifests import (
    DatasetVersionManifest,
    load_manifest,
    read_shard,
    shard_filename,
    version_dir,
)
from ..schemas import SchemaError, sha256_obj
from ..task_spec import TaskFamily
from .task_pack import (
    DIAGNOSTIC_ONLY_SPLITS,
    EVALUABLE_SPLITS,
    MANDATORY_SPLITS,
    MAX_PACK_TASKS,
    NEVER_EVALUABLE_SPLITS,
    EvaluationTaskKind,
    EvaluationTaskPack,
    HiddenTargetStore,
    answer_exposure,
    build_task,
)

#: Bumped when the builder's mapping decisions change. Part of the pack manifest.
PACK_BUILDER_VERSION = "m62.evaluation_pack_builder.1"


class PackBuilderError(SchemaError):
    """A dataset version that will not become an evaluation pack as written."""


# ══════════════════════════════════════════════════════════════════════════════
#  Deterministic registries
# ══════════════════════════════════════════════════════════════════════════════
#: Family -> (graders that run, graders that may not be skipped).
#:
#: ``secret_pii`` is mandatory everywhere: a response that leaks a secret has failed
#: whatever else it got right, and that judgement does not depend on the family.
GRADER_REGISTRY: dict[TaskFamily, tuple[tuple[str, ...], tuple[str, ...]]] = {
    TaskFamily.STRUCTURED_REPORT: (("json_schema", "secret_pii"),
                                   ("json_schema", "secret_pii")),
    TaskFamily.EVIDENCE_REQUEST: (("evidence_citation", "secret_pii"),
                                  ("evidence_citation", "secret_pii")),
    TaskFamily.SAFETY_REFUSAL: (("safety_policy", "secret_pii"),
                                ("safety_policy", "secret_pii")),
    TaskFamily.TOOL_CALL_SCHEMA: (("tool_call_schema", "secret_pii"),
                                  ("tool_call_schema", "secret_pii")),
}

#: Family -> the response shape the model is asked for.
#:
#: Deliberately structural and empty of content. A schema that lists the permitted
#: values of a verdict field has published the answer key under a heading the leak
#: checker would have to guess at, so these declare a type and stop.
RESPONSE_SCHEMA_REGISTRY: dict[TaskFamily, dict] = {
    TaskFamily.STRUCTURED_REPORT: {"type": "object", "additionalProperties": True},
    TaskFamily.EVIDENCE_REQUEST: {"type": "object", "additionalProperties": True},
    TaskFamily.SAFETY_REFUSAL: {"type": "object", "additionalProperties": True},
    TaskFamily.TOOL_CALL_SCHEMA: {
        "type": "object", "additionalProperties": False,
        "properties": {"tool": {"type": "string"},
                       "arguments": {"type": "object"}},
        "required": ["tool", "arguments"]},
}

#: Family -> the tool definitions the model may propose against. Proposals are graded;
#: none of them is ever executed, by this module or by anything it hands the pack to.
TOOL_SCHEMA_REGISTRY: dict[TaskFamily, tuple[dict, ...]] = {
    TaskFamily.TOOL_CALL_SCHEMA: (
        {"name": "query_auth_events", "description": "Read authentication events.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"account": {"type": "string"},
                                       "limit": {"type": "integer"}},
                        "required": ["account"]}},
        {"name": "lookup_indicator", "description": "Look up an indicator reputation.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"indicator_type": {"type": "string"},
                                       "value": {"type": "string"}},
                        "required": ["indicator_type", "value"]}},
        {"name": "isolate_host", "description": "Contain a host at the network layer.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"hostname": {"type": "string"},
                                       "reason": {"type": "string"}},
                        "required": ["hostname"]}},
        {"name": "get_process_ancestry", "description": "Read a process ancestry tree.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"pid": {"type": "integer"},
                                       "host": {"type": "string"}},
                        "required": ["pid"]}},
        {"name": "get_alert", "description": "Read one alert by identifier.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"alert_id": {"type": "string"}},
                        "required": ["alert_id"]}},
        {"name": "list_services", "description": "List services running on a host.",
         "parameters": {"type": "object", "additionalProperties": False,
                        "properties": {"host": {"type": "string"}},
                        "required": ["host"]}},
    ),
}


def graders_for(family: TaskFamily) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The graders a family is judged by, or a refusal naming it."""
    if family not in GRADER_REGISTRY:
        raise PackBuilderError(
            f"task family {getattr(family, 'value', family)!r} has no grader mapping; a "
            f"task nothing can judge contributes a denominator and no evidence")
    return GRADER_REGISTRY[family]


def response_schema_for(family: TaskFamily) -> dict:
    if family not in RESPONSE_SCHEMA_REGISTRY:
        raise PackBuilderError(
            f"task family {getattr(family, 'value', family)!r} has no response schema")
    return dict(RESPONSE_SCHEMA_REGISTRY[family])


def tool_schemas_for(family: TaskFamily) -> tuple[dict, ...]:
    return tuple(dict(s) for s in TOOL_SCHEMA_REGISTRY.get(family, ()))


# ══════════════════════════════════════════════════════════════════════════════
#  Result
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class BuiltPack:
    """A pack and its store, handed back together so neither can be forgotten."""

    pack: EvaluationTaskPack
    targets: HiddenTargetStore
    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    shard_hashes: dict = field(default_factory=dict)
    builder_version: str = PACK_BUILDER_VERSION

    def manifest(self) -> dict:
        """The pack manifest. Digests and counts; no prompt and no answer."""
        return {
            "builder_version": self.builder_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "shard_hashes": dict(sorted(self.shard_hashes.items())),
            "pack_hash": self.pack.pack_hash(),
            "hidden_target_store_hash": self.targets.store_hash(),
            "task_count": len(self.pack),
            "counts_by_split": self.pack.counts_by_split(),
            "counts_by_family": self.pack.counts_by_family(),
            "counts_by_kind": self.counts_by_kind(),
            "eligibility_blockers": list(self.pack.eligibility_blockers()),
        }

    def counts_by_kind(self) -> dict:
        counts: dict[str, int] = {}
        for task in self.pack.tasks:
            counts[task.kind.value] = counts.get(task.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    def manifest_hash(self) -> str:
        return sha256_obj(self.manifest())


# ══════════════════════════════════════════════════════════════════════════════
#  The builder
# ══════════════════════════════════════════════════════════════════════════════
def resolve_splits(splits: Sequence[object]) -> tuple[DatasetSplit, ...]:
    """Coerce and refuse. TRAIN and QUARANTINE are not selectable, ever."""
    if not splits:
        raise PackBuilderError(
            "task pack: no split was selected; an evaluation drawn from nothing "
            "produces a report with no denominator")
    resolved: list[DatasetSplit] = []
    for entry in splits:
        try:
            split = DatasetSplit(getattr(entry, "value", entry))
        except (ValueError, TypeError):
            raise PackBuilderError(
                f"task pack: {entry!r} is not a split this repository knows") from None
        if split in NEVER_EVALUABLE_SPLITS:
            raise PackBuilderError(
                f"task pack: {split.value} material was set aside for a reason and is "
                f"never model input")
        if split in DIAGNOSTIC_ONLY_SPLITS:
            raise PackBuilderError(
                f"task pack: {split.value} may be looked at and may never decide "
                f"anything; a model scoring well on what it was fitted on is the "
                f"expected outcome, not evidence")
        if split not in EVALUABLE_SPLITS:
            raise PackBuilderError(f"task pack: {split.value} is not evaluable")
        if split in resolved:
            raise PackBuilderError(f"task pack: split {split.value} selected twice")
        resolved.append(split)
    # Canonical order, so two callers naming the same splits build the same pack.
    return tuple(sorted(resolved, key=lambda s: s.value))


def build_task_pack_from_dataset(
        *, root: str | Path, dataset_id: str, dataset_version: str,
        splits: Sequence[object], generation: int = 1,
        manifest: DatasetVersionManifest | None = None,
        grader_registry: Mapping | None = None) -> BuiltPack:
    """Materialise a held-out evaluation pack from a promoted dataset version.

    ``load_manifest`` is the only loader used: it re-hashes every shard from disk,
    refuses symlinks and hard links, and re-derives each record's candidate digest. This
    function adds no second, weaker opinion about whether the version is intact.
    """
    selected = resolve_splits(splits)
    if manifest is None:
        manifest = load_manifest(root=root, dataset_id=dataset_id,
                                 dataset_version=dataset_version)
    directory = version_dir(root, dataset_id, dataset_version)

    tasks = []
    store = HiddenTargetStore(generation=generation)
    shard_hashes: dict[str, str] = {}
    seen_ids: set[str] = set()
    registry = dict(grader_registry) if grader_registry is not None else None

    for split in selected:
        shard = manifest.shard_for(split)
        if shard is None:
            raise PackBuilderError(
                f"task pack: the version names no {split.value} shard, so the split the "
                f"configuration selected contributes nothing")
        shard_hashes[split.value] = shard.sha256_file
        path = directory / shard_filename(split)
        for candidate in read_shard(path):
            _check_record(candidate, split, seen_ids)
            family = candidate.task_family
            if registry is not None and family in registry:
                grader_ids, mandatory = registry[family]
            else:
                grader_ids, mandatory = graders_for(family)
            task, target = build_task(
                candidate, split=split,
                dataset_manifest_hash=manifest.manifest_hash(),
                shard_hash=shard.sha256_file,
                grader_ids=grader_ids, mandatory_grader_ids=mandatory,
                expected_output_schema=response_schema_for(family),
                tool_schemas=tool_schemas_for(family))
            _assert_no_exposure(task)
            tasks.append(task)
            store.add(target)
            seen_ids.add(candidate.task_id)

    if not tasks:
        raise PackBuilderError(
            "task pack: the selected splits contributed no task; there is nothing to "
            "compare two models on")
    if len(tasks) > MAX_PACK_TASKS:
        raise PackBuilderError(
            f"task pack: {len(tasks)} tasks exceeds the {MAX_PACK_TASKS} ceiling")

    pack = EvaluationTaskPack(tasks=tuple(tasks), dataset_id=dataset_id,
                              dataset_version=dataset_version)
    if len(store) != len(pack):
        raise PackBuilderError(
            f"task pack: {len(pack)} tasks but {len(store)} hidden targets; a task with "
            f"no answer is scored against nothing")
    return BuiltPack(pack=pack, targets=store.freeze(), dataset_id=dataset_id,
                     dataset_version=dataset_version,
                     dataset_manifest_hash=manifest.manifest_hash(),
                     shard_hashes=shard_hashes)


def _check_record(candidate: DatasetCandidate, split: DatasetSplit,
                  seen_ids: set[str]) -> None:
    """Everything about one record that must hold before it becomes a task."""
    if candidate.state is not CandidateState.PROMOTED:
        raise PackBuilderError(
            f"task pack: candidate {candidate.candidate_id!r} is {candidate.state.value} "
            f"in a promoted version; only promoted material may be evaluated on")
    if candidate.task_id in seen_ids:
        raise PackBuilderError(
            f"task pack: duplicate task id {candidate.task_id!r} across the selected "
            f"splits; a repeated task inflates whichever arm happens to answer it well")
    if not str(candidate.user_prompt or "").strip():
        raise PackBuilderError(
            f"task pack: candidate {candidate.candidate_id!r} has no prompt")
    if not str(candidate.target_text or "").strip():
        raise PackBuilderError(
            f"task pack: candidate {candidate.candidate_id!r} has no expected answer, "
            f"so no grader could distinguish a good response from any other")
    if split in EVALUABLE_SPLITS and not candidate.evaluation_only \
            and not split.held_out:
        raise PackBuilderError(
            f"task pack: candidate {candidate.candidate_id!r} is trainable material in "
            f"{split.value}")


def _assert_no_exposure(task) -> None:
    """A last look at the finished task, after every mapping decision was made."""
    exposure = answer_exposure(task.expected_output_schema, path="expected_schema")
    for index, schema in enumerate(task.tool_schemas):
        exposure += answer_exposure(schema, path=f"tool_schemas[{index}]")
    if exposure:
        raise PackBuilderError(
            f"task {task.task_id!r}: the schemas this builder attached publish an "
            f"expected value at {sorted(set(exposure))[:6]}")


def pack_blockers(built: BuiltPack, *, min_tasks: int,
                  mandatory_families: Sequence[str] = ()) -> tuple[str, ...]:
    """Why this pack cannot support an eligibility claim. Empty means it can.

    Separate from construction on purpose: a pack that is too small to support a claim
    is still a legitimate pack to run and look at. What it may not do is decide.
    """
    problems = list(built.pack.eligibility_blockers())
    if len(built.pack) < int(min_tasks):
        problems.append(
            f"{len(built.pack)} paired task(s) is below the policy minimum of "
            f"{int(min_tasks)}; no directional claim is supportable from this many")
    present_families = {f.value for f in built.pack.families()}
    for family in mandatory_families:
        if str(family) not in present_families:
            problems.append(
                f"no {family} task is present; a comparison that never exercises it "
                f"cannot notice a regression in it")
    kinds = built.counts_by_kind()
    if not kinds.get(EvaluationTaskKind.REQUIRED_REFUSAL.value):
        problems.append(
            "no task requires a refusal; a model that refuses everything would be "
            "indistinguishable from a safe one")
    if not (kinds.get(EvaluationTaskKind.REQUIRED_COMPLETION.value)
            or kinds.get(EvaluationTaskKind.COMPLETION.value)):
        problems.append(
            "no task requires a completion; a corpus of pure refusals rewards "
            "refusing everything")
    for split in MANDATORY_SPLITS:
        if split.value not in built.pack.counts_by_split():
            problems.append(f"no {split.value} task is present")
    return tuple(dict.fromkeys(problems))


__all__ = [
    "GRADER_REGISTRY", "PACK_BUILDER_VERSION", "RESPONSE_SCHEMA_REGISTRY",
    "TOOL_SCHEMA_REGISTRY", "BuiltPack", "PackBuilderError",
    "build_task_pack_from_dataset", "graders_for", "pack_blockers",
    "resolve_splits", "response_schema_for", "tool_schemas_for",
]
