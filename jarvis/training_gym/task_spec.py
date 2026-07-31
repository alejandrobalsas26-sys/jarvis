"""training_gym/task_spec.py — V69 M62: what a practice task is allowed to be.

WHY THIS EXISTS
---------------
A task specification is the only place a human describes work that JARVIS will then
attempt inside a sandbox, be graded on, and possibly be TRAINED on. Everything that
must never happen has to be made unrepresentable here, because by the time a task
reaches a container it is too late to argue about what it meant.

Three mechanisms do that work, in order of strength:

1. **A closed action vocabulary.** :class:`ActionKind` enumerates every action an
   episode may take. There is no member for "scan a host", "send a request", "run an
   arbitrary command against a target" or "modify the operator's machine" — so a task
   that wants those cannot be written, misspelled into existence, or smuggled in as a
   free-text string. This is the primary safe-task boundary; the textual screen below
   is a second line, not the first.

2. **A closed grader registry.** :data:`GRADER_IDS` is the set of deterministic
   graders that exist. A task naming ``"pytest_grader"`` instead of ``"pytest"`` is
   REJECTED at parse time rather than quietly running one fewer check — a typo must
   not be able to reduce the evidence required for approval.

3. **A textual screen on the instruction fields.** :func:`unsafe_task_markers` looks
   for imperatives that direct the model to do something outside the defensive
   boundary. It reads the *prompt and constraints only* — never fixture content —
   because a DFIR fixture legitimately narrates attacker behaviour while a prompt
   never legitimately instructs it. It errs toward refusal: the author rephrases.

FAIL-CLOSED
-----------
Unknown field, unknown enum value, unknown grader id, an action that is both allowed
and forbidden, a mandatory grader that is not in the required set, a family whose
evidence requirements are unmet, ``evaluation_only`` combined with dataset
eligibility, or ``RESTRICTED`` material marked trainable — every one of these raises
:class:`~training_gym.schemas.SchemaError` rather than resolving to a default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .policies import (
    ArtifactPolicy,
    NetworkSpec,
    ResourceBudget,
    SandboxPolicy,
    ScoringPolicy,
)
from .schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    SensitivityClass,
    check_schema_version,
    reject_unknown_fields,
    require_bool,
    require_enum,
    require_id,
    require_mapping,
    require_str_tuple,
    require_text,
    sha256_obj,
)
from .workspace import safe_relative_path


# ── task families ─────────────────────────────────────────────────────────────
class TaskFamily(str, Enum):
    """The ten kinds of work the gym practises. All defensive, all on fixtures."""

    CODING_FIX = "coding_fix"
    SOC_TRIAGE = "soc_triage"
    DFIR_TIMELINE = "dfir_timeline"
    SIGMA_RULE = "sigma_rule"
    YARA_RULE = "yara_rule"
    SURICATA_RULE = "suricata_rule"
    TOOL_CALL_SCHEMA = "tool_call_schema"
    SAFETY_REFUSAL = "safety_refusal"
    EVIDENCE_REQUEST = "evidence_request"
    STRUCTURED_REPORT = "structured_report"

    @property
    def is_detection_rule(self) -> bool:
        """Families whose answer is a rule an external validator must accept."""
        return self in (TaskFamily.SIGMA_RULE, TaskFamily.YARA_RULE,
                        TaskFamily.SURICATA_RULE)

    @property
    def requires_structured_output(self) -> bool:
        """Families whose answer is machine-checkable JSON, not prose."""
        return self in (TaskFamily.SOC_TRIAGE, TaskFamily.DFIR_TIMELINE,
                        TaskFamily.TOOL_CALL_SCHEMA, TaskFamily.EVIDENCE_REQUEST,
                        TaskFamily.STRUCTURED_REPORT)


class ActionKind(str, Enum):
    """Every action an episode may take. The absence of a member is the control.

    Nothing here reaches the network, the operator's machine, or a real target: the
    most powerful members run a fixed local tool (tests, linter, scanner, rule
    validator) against the disposable workspace and nothing else.
    """

    READ_WORKSPACE_FILE = "read_workspace_file"
    WRITE_WORKSPACE_FILE = "write_workspace_file"
    RUN_TESTS = "run_tests"
    RUN_LINTER = "run_linter"
    RUN_SECURITY_SCANNER = "run_security_scanner"
    RUN_RULE_VALIDATOR = "run_rule_validator"
    PROPOSE_TOOL_CALL = "propose_tool_call"
    REQUEST_EVIDENCE = "request_evidence"
    EMIT_ANSWER = "emit_answer"
    EMIT_ARTIFACT = "emit_artifact"
    REFUSE = "refuse"


#: The deterministic graders that exist. A task may only name these.
GRADER_IDS: tuple[str, ...] = (
    "pytest", "ruff", "bandit", "json_schema", "secret_pii", "tool_call_schema",
    "evidence_citation", "diff_budget", "file_boundary", "detection_rule",
    "safety_policy",
)

#: Every task must prove its material is clean, whatever else it measures.
UNIVERSAL_MANDATORY_GRADERS: tuple[str, ...] = ("secret_pii",)

#: Evidence a family cannot be approved without. Enforced against
#: ``scoring.mandatory_graders``, so a family's core check can never be advisory.
FAMILY_MANDATORY_GRADERS: dict[TaskFamily, tuple[str, ...]] = {
    TaskFamily.CODING_FIX: ("pytest", "diff_budget", "file_boundary"),
    TaskFamily.SOC_TRIAGE: ("json_schema", "evidence_citation"),
    TaskFamily.DFIR_TIMELINE: ("json_schema", "evidence_citation"),
    TaskFamily.SIGMA_RULE: ("detection_rule",),
    TaskFamily.YARA_RULE: ("detection_rule",),
    TaskFamily.SURICATA_RULE: ("detection_rule",),
    TaskFamily.TOOL_CALL_SCHEMA: ("tool_call_schema",),
    TaskFamily.SAFETY_REFUSAL: ("safety_policy",),
    TaskFamily.EVIDENCE_REQUEST: ("evidence_citation",),
    TaskFamily.STRUCTURED_REPORT: ("json_schema",),
}


# ── the safe-task textual screen ──────────────────────────────────────────────
#: ``(pattern, label)``. Each requires an ACTION verb next to the dangerous object,
#: so defensive phrasing ("triage this phishing email", "write a Sigma rule that
#: detects ransomware file-extension changes") does not match while an instruction to
#: perform the act does.
_UNSAFE_TASK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), label) for pattern, label in (
        (r"(?i)\b(?:deploy|install|drop|detonate|launch|execute)\s+"
         r"(?:the\s+|a\s+|this\s+)?"
         r"(?:ransomware|malware|implant|backdoor|rootkit|keylogger|stealer)\b",
         "malware deployment"),
        (r"(?i)\b(?:steal|exfiltrate|harvest|crack|brute[\s-]?force)\s+"
         r"(?:the\s+|these\s+|real\s+|live\s+|his\s+|her\s+|their\s+)?"
         r"(?:credentials?|passwords?|password\s+hashes|api\s+keys?|tokens?|secrets?)\b",
         "credential theft"),
        (r"(?i)\b(?:establish|maintain|set\s?up|deploy)\s+"
         r"(?:persistence|a\s+beacon|command[\s-]and[\s-]control|"
         r"c2\s+(?:channel|infrastructure|server))\b",
         "persistence or live command-and-control"),
        (r"(?i)(?:\bwithout\s+(?:being\s+)?(?:detect\w*|trigger\w*|alert\w*)|"
         r"\bavoid(?:ing)?\s+detection\s+by\b|"
         r"\bso\s+that\s+(?:it|the\s+\w+)\s+is\s+not\s+detected\b)",
         "detection evasion"),
        (r"(?i)\b(?:scan|enumerate|exploit|attack|pentest)\s+(?:the\s+)?"
         r"(?:public|internet[\s-]facing|production|external|live)\s+"
         r"(?:hosts?|targets?|network|range|ips?|systems?)\b",
         "unauthorized live target"),
        (r"(?i)\b(?:create|write|generate|craft|compose|build)\s+(?:a\s+|the\s+)?"
         r"(?:phishing|smishing|vishing)\s+"
         r"(?:campaign|email|message|lure|pretext|kit|page|site)\b",
         "real phishing content"),
        (r"(?i)\bweaponi[sz]\w*\s+(?:the\s+|this\s+|a\s+)?"
         r"(?:exploit|payload|poc|proof[\s-]of[\s-]concept)\b",
         "weaponized payload"),
        (r"(?i)\b(?:escape|break\s+out\s+of)\s+(?:the\s+)?"
         r"(?:container|sandbox|hypervisor|vm|jail)\b",
         "host or sandbox escape research"),
        (r"(?i)(?:\brm\s+-rf\s+/(?!\S)|\bformat\s+[a-z]:|\bmkfs\.\w+|"
         r"\bdd\s+if=\S+\s+of=/dev/|\bdel\s+/[sfq]\s)",
         "destructive command"),
    )
)


def unsafe_task_markers(text: str) -> tuple[str, ...]:
    """Labels of out-of-boundary instructions found in *text*. Empty tuple = clean.

    Apply to instruction fields only. Fixture CONTENT is never screened this way:
    an inert malware sample's strings and a DFIR log's narration of an intrusion are
    exactly the material these tasks exist to reason about.
    """
    blob = str(text or "")
    return tuple(sorted({label for pattern, label in _UNSAFE_TASK_PATTERNS
                         if pattern.search(blob)}))


# ── supporting records ────────────────────────────────────────────────────────
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def require_timestamp(value: object, field_name: str) -> str:
    """An explicit UTC ISO-8601 instant. No clock is read here — the caller supplies
    it, so a spec hashes identically wherever it is loaded."""
    text = str(value or "").strip()
    if not _TIMESTAMP_RE.match(text):
        raise SchemaError(f"{field_name}: expected UTC ISO-8601 "
                          f"(YYYY-MM-DDTHH:MM:SSZ), got {text!r}")
    return text


@dataclass(frozen=True)
class FixtureRef:
    """One input file, bound to its content hash.

    The hash is mandatory. A fixture referenced by path alone would let the material
    under a task change without the task changing, which silently invalidates every
    grade and every training example ever derived from it.
    """

    path: str
    sha256: str
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", safe_relative_path(self.path,
                                                            "fixture.path").as_posix())
        digest = str(self.sha256 or "").strip().lower()
        if not _SHA256_RE.match(digest):
            raise SchemaError(f"fixture.sha256: expected 64 hex characters for "
                              f"{self.path!r}, got {self.sha256!r}")
        object.__setattr__(self, "sha256", digest)

    def to_dict(self) -> dict:
        return {"path": self.path, "sha256": self.sha256,
                "description": self.description}

    @classmethod
    def from_dict(cls, payload: object) -> "FixtureRef":
        data = require_mapping(payload, "fixture")
        reject_unknown_fields(data, ("path", "sha256", "description"), label="fixture")
        return cls(path=str(data.get("path", "")),
                   sha256=str(data.get("sha256", "")),
                   description=str(data.get("description", "")))


@dataclass(frozen=True)
class Provenance:
    """Where a task came from, so a training example can always be traced back."""

    source: str = "handwritten"
    author: str = "unknown"
    license: str = "internal-use"
    upstream_ref: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not str(self.source or "").strip():
            raise SchemaError("provenance.source: required (a task of unknown origin "
                              "may not become training data)")

    def to_dict(self) -> dict:
        return {"source": self.source, "author": self.author,
                "license": self.license, "upstream_ref": self.upstream_ref,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, payload: object | None) -> "Provenance":
        if not payload:
            return cls()
        data = require_mapping(payload, "provenance")
        reject_unknown_fields(
            data, ("source", "author", "license", "upstream_ref", "notes"),
            label="provenance")
        return cls(source=str(data.get("source", "handwritten")),
                   author=str(data.get("author", "unknown")),
                   license=str(data.get("license", "internal-use")),
                   upstream_ref=str(data.get("upstream_ref", "")),
                   notes=str(data.get("notes", "")))


# ── the task specification ────────────────────────────────────────────────────
_SPEC_FIELDS: tuple[str, ...] = (
    "schema_version", "task_id", "task_family", "version", "title", "prompt",
    "system_constraints", "allowed_actions", "forbidden_actions", "fixtures",
    "expected_output_schema", "required_graders", "resources", "network",
    "sandbox", "artifacts", "scoring", "provenance", "sensitivity", "created_by",
    "created_at", "evaluation_only", "dataset_eligible", "tags",
)


@dataclass(frozen=True)
class TaskSpec:
    """One fully-specified, bounded, gradeable practice task."""

    task_id: str
    task_family: TaskFamily
    prompt: str
    scoring: ScoringPolicy
    created_by: str
    created_at: str
    version: str = "1.0.0"
    title: str = ""
    system_constraints: tuple[str, ...] = ()
    allowed_actions: tuple[ActionKind, ...] = (ActionKind.EMIT_ANSWER,)
    forbidden_actions: tuple[ActionKind, ...] = ()
    fixtures: tuple[FixtureRef, ...] = ()
    expected_output_schema: dict = field(default_factory=dict)
    required_graders: tuple[str, ...] = ()
    resources: ResourceBudget = field(default_factory=ResourceBudget)
    network: NetworkSpec = field(default_factory=NetworkSpec)
    sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    artifacts: ArtifactPolicy = field(default_factory=ArtifactPolicy)
    provenance: Provenance = field(default_factory=Provenance)
    sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC
    evaluation_only: bool = False
    dataset_eligible: bool = True
    tags: tuple[str, ...] = ()

    # -- validation -------------------------------------------------------------
    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", require_id(self.task_id, "task_id"))
        object.__setattr__(self, "created_by", require_id(self.created_by, "created_by"))
        object.__setattr__(self, "created_at",
                           require_timestamp(self.created_at, "created_at"))
        if not _VERSION_RE.match(str(self.version)):
            raise SchemaError(f"version: expected MAJOR.MINOR.PATCH, got "
                              f"{self.version!r}")
        require_text(self.prompt, "prompt", min_len=16, max_len=20_000)

        self._validate_safe_boundary()
        self._validate_actions()
        self._validate_graders()
        self._validate_output_schema()
        self._validate_fixtures()
        self._validate_eligibility()

    def _validate_safe_boundary(self) -> None:
        """The prompt and constraints are instructions; screen those, nothing else."""
        instruction = "\n".join((self.prompt, *self.system_constraints, self.title))
        markers = unsafe_task_markers(instruction)
        if markers:
            raise SchemaError(
                f"task {self.task_id}: instruction requests out-of-boundary work "
                f"({', '.join(markers)}). The gym trains defensive security, code "
                f"quality and safe tool behaviour on fixtures only.")

    def _validate_actions(self) -> None:
        allowed = set(self.allowed_actions)
        forbidden = set(self.forbidden_actions)
        if not allowed:
            raise SchemaError(f"task {self.task_id}: allowed_actions is empty "
                              f"(an episode that may do nothing is not a task)")
        overlap = sorted(a.value for a in allowed & forbidden)
        if overlap:
            raise SchemaError(f"task {self.task_id}: ambiguous authority — "
                              f"{overlap} are both allowed and forbidden")
        if ActionKind.EMIT_ANSWER not in allowed and ActionKind.REFUSE not in allowed:
            raise SchemaError(f"task {self.task_id}: allowed_actions must permit "
                              f"emit_answer or refuse, or nothing can be graded")
        if self.scoring.expect_refusal and ActionKind.REFUSE not in allowed:
            raise SchemaError(f"task {self.task_id}: expect_refusal=true but the "
                              f"refuse action is not allowed")

    def _validate_graders(self) -> None:
        known = set(GRADER_IDS)
        required = set(self.required_graders)
        unknown = sorted(required - known)
        if unknown:
            raise SchemaError(f"task {self.task_id}: unknown grader id(s) {unknown}; "
                              f"known ids are {list(GRADER_IDS)}")
        mandatory = set(self.scoring.mandatory_graders)
        unknown_mandatory = sorted(mandatory - known)
        if unknown_mandatory:
            raise SchemaError(f"task {self.task_id}: unknown mandatory grader id(s) "
                              f"{unknown_mandatory}")
        orphaned = sorted(mandatory - required)
        if orphaned:
            raise SchemaError(f"task {self.task_id}: mandatory grader(s) {orphaned} "
                              f"are not in required_graders")
        missing_universal = sorted(set(UNIVERSAL_MANDATORY_GRADERS) - mandatory)
        if missing_universal:
            raise SchemaError(f"task {self.task_id}: grader(s) {missing_universal} "
                              f"are mandatory for every task")
        needed = set(FAMILY_MANDATORY_GRADERS.get(self.task_family, ()))
        missing_family = sorted(needed - mandatory)
        if missing_family:
            raise SchemaError(
                f"task {self.task_id}: family {self.task_family.value} cannot be "
                f"approved without mandatory grader(s) {missing_family}")

    def _validate_output_schema(self) -> None:
        if self.task_family.requires_structured_output:
            if not self.expected_output_schema:
                raise SchemaError(
                    f"task {self.task_id}: family {self.task_family.value} produces "
                    f"machine-checkable output and requires expected_output_schema")
            if "type" not in self.expected_output_schema:
                raise SchemaError(f"task {self.task_id}: expected_output_schema must "
                                  f"declare a top-level 'type'")

    def _validate_fixtures(self) -> None:
        seen: set[str] = set()
        for ref in self.fixtures:
            if ref.path in seen:
                raise SchemaError(f"task {self.task_id}: duplicate fixture path "
                                  f"{ref.path!r}")
            seen.add(ref.path)
        if len(self.fixtures) > self.artifacts.max_count * 8:
            raise SchemaError(f"task {self.task_id}: too many fixtures "
                              f"({len(self.fixtures)})")

    def _validate_eligibility(self) -> None:
        if self.evaluation_only and self.dataset_eligible:
            raise SchemaError(
                f"task {self.task_id}: evaluation_only tasks may never be dataset "
                f"eligible — a held-out evaluator answer must not become training data")
        if not self.sensitivity.dataset_eligible and self.dataset_eligible:
            raise SchemaError(f"task {self.task_id}: sensitivity="
                              f"{self.sensitivity.value} material is never trainable")
        if self.network.policy.enabled and self.sensitivity is SensitivityClass.RESTRICTED:
            raise SchemaError(f"task {self.task_id}: restricted material may not be "
                              f"combined with an enabled network policy")

    # -- identity ---------------------------------------------------------------
    def spec_hash(self) -> str:
        """SHA-256 over the whole canonical spec. Binds grades, reviews and examples.

        A teacher review or a training example carries this hash; if the task is
        edited the hash changes and the stale review can no longer be replayed onto
        the new task."""
        return sha256_obj(self.to_dict())

    @property
    def exportable_to_teacher(self) -> bool:
        """Whether this task's material may appear in a cloud teacher packet."""
        return self.sensitivity.exportable_to_teacher

    # -- serialization ----------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "task_id": self.task_id,
            "task_family": self.task_family.value,
            "version": self.version,
            "title": self.title,
            "prompt": self.prompt,
            "system_constraints": list(self.system_constraints),
            "allowed_actions": [a.value for a in self.allowed_actions],
            "forbidden_actions": [a.value for a in self.forbidden_actions],
            "fixtures": [f.to_dict() for f in self.fixtures],
            "expected_output_schema": dict(self.expected_output_schema),
            "required_graders": list(self.required_graders),
            "resources": self.resources.to_dict(),
            "network": self.network.to_dict(),
            "sandbox": self.sandbox.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "scoring": self.scoring.to_dict(),
            "provenance": self.provenance.to_dict(),
            "sensitivity": self.sensitivity.value,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "evaluation_only": self.evaluation_only,
            "dataset_eligible": self.dataset_eligible,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, payload: object) -> "TaskSpec":
        data = require_mapping(payload, "task")
        check_schema_version(data, label="task")
        reject_unknown_fields(data, _SPEC_FIELDS, label="task")

        actions = tuple(require_enum(a, ActionKind, "allowed_actions")
                        for a in require_str_tuple(data.get("allowed_actions"),
                                                   "allowed_actions", max_items=32))
        forbidden = tuple(require_enum(a, ActionKind, "forbidden_actions")
                          for a in require_str_tuple(data.get("forbidden_actions"),
                                                     "forbidden_actions", max_items=32))
        return cls(
            task_id=str(data.get("task_id", "")),
            task_family=require_enum(data.get("task_family"), TaskFamily, "task_family"),
            version=str(data.get("version", "1.0.0")),
            title=str(data.get("title", "")),
            prompt=str(data.get("prompt", "")),
            system_constraints=require_str_tuple(data.get("system_constraints"),
                                                 "system_constraints", max_items=32),
            allowed_actions=actions or (ActionKind.EMIT_ANSWER,),
            forbidden_actions=forbidden,
            fixtures=tuple(FixtureRef.from_dict(f) for f in data.get("fixtures") or ()),
            expected_output_schema=require_mapping(
                data.get("expected_output_schema") or {}, "expected_output_schema"),
            required_graders=require_str_tuple(data.get("required_graders"),
                                               "required_graders", max_items=32),
            resources=ResourceBudget.from_dict(data.get("resources")),
            network=NetworkSpec.from_dict(data.get("network")),
            sandbox=SandboxPolicy.from_dict(data.get("sandbox")),
            artifacts=ArtifactPolicy.from_dict(data.get("artifacts")),
            scoring=ScoringPolicy.from_dict(data.get("scoring")),
            provenance=Provenance.from_dict(data.get("provenance")),
            sensitivity=require_enum(data.get("sensitivity",
                                              SensitivityClass.SYNTHETIC.value),
                                     SensitivityClass, "sensitivity"),
            created_by=str(data.get("created_by", "")),
            created_at=str(data.get("created_at", "")),
            evaluation_only=require_bool(data.get("evaluation_only", False),
                                         "evaluation_only"),
            dataset_eligible=require_bool(data.get("dataset_eligible", True),
                                          "dataset_eligible"),
            tags=require_str_tuple(data.get("tags"), "tags", max_items=32),
        )


__all__ = [
    "FAMILY_MANDATORY_GRADERS", "GRADER_IDS", "UNIVERSAL_MANDATORY_GRADERS",
    "ActionKind", "FixtureRef", "Provenance", "TaskFamily", "TaskSpec",
    "require_timestamp", "unsafe_task_markers",
]
