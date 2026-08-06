"""training_gym/evaluation/references.py — V69 M62 S3C: what is being compared.

THE SUBSTITUTION PROBLEM
------------------------
"The adapter improved the model by 4%" is a claim about two specific things: a base model
at a specific revision with a specific tokenizer, and one specific adapter. If either can
be swapped without the report noticing, the number measures nothing. A mutable tag is a
swap waiting to happen — ``main`` on Tuesday and ``main`` on Wednesday are two models.

So both sides of the comparison are *references*, not paths:

  * :class:`BaseModelEvaluationReference` binds an immutable revision, a tokenizer
    identity and cache evidence, and refuses remote code outright.
  * :class:`AdapterEvaluationReference` is derived from **verified S3B evidence** — a
    re-validated run directory and its manifest — never from a filesystem path a caller
    handed in. ``build_adapter_reference`` is the only constructor that reaches disk, and
    it re-runs S3B's own ``verify_completed_run`` rather than trusting what was written.

Both carry a reference hash covering every security-relevant field, so the plan digest
changes the moment either side changes.

CONTAMINATION
-------------
An adapter that was fitted on the split it is about to be evaluated on has memorised the
answer, not learned the behaviour. The adapter manifest already records the digests of
the hidden-evaluation and security-regression shards it was bound to at training time;
:func:`contamination_blockers` compares them against the shards this evaluation intends
to use and refuses on overlap.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..datasets.candidate import require_digest
from ..schemas import (
    SchemaError,
    assert_no_private_content,
    require_bool,
    require_enum,
    require_id,
    require_str_tuple,
    require_text,
    sha256_obj,
    short,
)
from ..training.artifacts import (
    ADAPTER_MANIFEST_FILE,
    AdapterManifest,
    verify_completed_run,
)
from ..training.config import TrainingMethod, TrainingRunState
from ..training.model_identity import (
    CacheStatus,
    ModelIdentity,
    RevisionKind,
    canonical_identity_hash,
)

#: Bumped when either reference's shape changes.
REFERENCE_SCHEMA_VERSION = "m62.evaluation_reference.1"

#: The training methods whose output S3C knows how to attach for inference. DPO produces
#: a LoRA adapter too, but S3B has never executed that path, so an evaluation that
#: claimed to load one would be describing an artefact this repository cannot produce.
EVALUABLE_TRAINING_METHODS: frozenset[TrainingMethod] = frozenset(
    {TrainingMethod.SFT_LORA})


class EvaluationReferenceError(SchemaError):
    """A reference that does not identify exactly one verifiable subject."""


class EvaluationRole(str, Enum):
    """Which arm of the comparison a reference describes."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class BaseModelEvaluationReference:
    """The exact base model and tokenizer both arms load. No substitution is possible."""

    base_model_id: str
    base_model_revision: str
    base_model_identity_hash: str
    tokenizer_id: str
    tokenizer_revision: str
    tokenizer_identity_hash: str
    #: The durable byte identity. Empty only on a reference restored from a record
    #: written before this field existed; :func:`pairing_blockers` says so out loud
    #: rather than treating an absent digest as a match.
    base_model_canonical_identity_hash: str = ""
    architecture_family: str = ""
    parameter_class: str = ""
    trust_remote_code: bool = False
    cache_status: CacheStatus = CacheStatus.UNKNOWN
    cache_evidence: str = ""
    revision_kind: RevisionKind = RevisionKind.PLACEHOLDER
    reference_version: str = REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "cache_status", require_enum(self.cache_status, CacheStatus,
                                                "base reference.cache_status"))
        set_(self, "revision_kind", require_enum(self.revision_kind, RevisionKind,
                                                 "base reference.revision_kind"))
        set_(self, "trust_remote_code",
             require_bool(self.trust_remote_code, "base reference.trust_remote_code"))
        for name in ("base_model_id", "base_model_revision", "tokenizer_id",
                     "tokenizer_revision"):
            set_(self, name, require_text(getattr(self, name), f"base reference.{name}",
                                          max_len=200).strip())
        for name in ("base_model_identity_hash", "tokenizer_identity_hash"):
            set_(self, name, require_digest(getattr(self, name),
                                            f"base reference.{name}"))
        if self.base_model_canonical_identity_hash:
            set_(self, "base_model_canonical_identity_hash",
                 require_digest(self.base_model_canonical_identity_hash,
                                "base reference.base_model_canonical_identity_hash"))
        if self.trust_remote_code:
            raise EvaluationReferenceError(
                "base reference: trust_remote_code is refused. Evaluating a model that "
                "executes repository code from its own weights directory means the "
                "measurement and the code being measured are the same untrusted thing")
        # A private path must not travel inside a reference an operator pastes into a
        # ticket. Cache PRESENCE is the fact; the directory is the operator's filesystem.
        if any(sep in self.cache_evidence for sep in ("/", "\\")):
            raise EvaluationReferenceError(
                "base reference: cache_evidence may state whether weights are present, "
                "never where; a path here would publish the operator's home directory")

    # -- derived ---------------------------------------------------------------
    @property
    def is_live_executable(self) -> bool:
        """Whether a FUTURE live evaluation could act on this. Never whether one did."""
        return not self.live_blockers()

    def live_blockers(self) -> tuple[str, ...]:
        """What would stop a live evaluation. Cache absence is a blocker, not a fetch."""
        problems: list[str] = []
        if self.revision_kind is not RevisionKind.IMMUTABLE_COMMIT:
            problems.append(
                f"base model revision {self.base_model_revision!r} is a "
                f"{self.revision_kind.value}; a mutable tag names a different model "
                f"tomorrow, so a comparison against it is not reproducible")
        if self.cache_status is not CacheStatus.PRESENT:
            problems.append(
                f"base model weights are {self.cache_status.value} in the reviewed "
                f"cache; S3C never downloads, so a live evaluation would refuse")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "reference_version": self.reference_version,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "base_model_identity_hash": self.base_model_identity_hash,
            "base_model_canonical_identity_hash":
                self.base_model_canonical_identity_hash,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "architecture_family": self.architecture_family,
            "parameter_class": self.parameter_class,
            "trust_remote_code": self.trust_remote_code,
            "cache_status": self.cache_status.value,
            "cache_evidence": self.cache_evidence,
            "revision_kind": self.revision_kind.value,
            "evaluation_role": EvaluationRole.BASELINE.value,
        }

    def reference_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "reference_hash": self.reference_hash(),
                  "live_blockers": list(self.live_blockers())}
        assert_no_private_content(record, label="base model evaluation reference")
        return record


def base_reference_from_identity(identity: ModelIdentity) -> BaseModelEvaluationReference:
    """Derive a baseline reference from the frozen S3A model identity.

    The identity authority already refuses remote code and already classifies the
    revision; re-deriving either here would create a second, weaker opinion about the
    same fact.
    """
    if not isinstance(identity, ModelIdentity):
        raise EvaluationReferenceError(
            f"base reference: expected a ModelIdentity, got {type(identity).__name__}; "
            f"a reference built from a bare string identifies a name, not a model")
    return BaseModelEvaluationReference(
        base_model_id=identity.model_id,
        base_model_revision=identity.revision,
        base_model_identity_hash=identity.identity_hash(),
        base_model_canonical_identity_hash=identity.canonical_identity_hash(),
        tokenizer_id=identity.tokenizer_id or identity.model_id,
        tokenizer_revision=identity.tokenizer_revision or identity.revision,
        tokenizer_identity_hash=identity.tokenizer_identity_hash(),
        architecture_family=identity.family,
        parameter_class=f"{identity.parameters_b}b" if identity.parameters_b else "",
        trust_remote_code=bool(identity.requires_remote_code),
        cache_status=identity.cache_status,
        cache_evidence=_safe_cache_evidence(identity.cache_evidence),
        revision_kind=identity.revision_kind,
    )


def _safe_cache_evidence(evidence: object) -> str:
    """Keep the fact, drop the path. A cache probe's evidence string may name a
    directory; the reference records only whether something was found."""
    text = str(evidence or "").strip()
    if not text:
        return ""
    if any(sep in text for sep in ("/", "\\")):
        return "a cache directory was inspected; its location is not published"
    return text[:200]


@dataclass(frozen=True)
class AdapterEvaluationReference:
    """The exact adapter under evaluation, bound to the run that produced it.

    Never construct this directly from a path. :func:`build_adapter_reference` is the
    only supported constructor, and it re-verifies the artefacts from disk.
    """

    run_id: str
    adapter_manifest_hash: str
    adapter_artifact_tree_hash: str
    plan_hash: str
    training_config_hash: str
    base_model_identity_hash: str
    tokenizer_identity_hash: str
    tokenizer_chat_template_hash: str
    dataset_reference_hash: str
    dataset_manifest_hash: str
    train_shard_hash: str
    validation_shard_hash: str
    hidden_evaluation_hash: str
    security_regression_hash: str
    method: TrainingMethod
    lora: dict
    run_state: str
    #: Re-derived from the manifest's own recorded model/tokenizer fields, not read
    #: back from a stored digest. A manifest written before the canonical authority
    #: existed still names everything this needs.
    base_model_canonical_identity_hash: str = ""
    interrupted: bool = False
    completed: bool = True
    artifact_verified: bool = False
    artifact_file_count: int = 0
    artifact_total_bytes: int = 0
    manifest_schema_version: str = ""
    known_limitations: tuple[str, ...] = ()
    reference_version: str = REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "run_id", require_id(self.run_id, "adapter reference.run_id"))
        set_(self, "method", require_enum(self.method, TrainingMethod,
                                          "adapter reference.method"))
        for name in ("interrupted", "completed", "artifact_verified"):
            set_(self, name, require_bool(getattr(self, name),
                                          f"adapter reference.{name}"))
        for name in ("adapter_manifest_hash", "adapter_artifact_tree_hash", "plan_hash",
                     "training_config_hash", "base_model_identity_hash",
                     "tokenizer_identity_hash", "dataset_reference_hash",
                     "dataset_manifest_hash", "train_shard_hash", "hidden_evaluation_hash",
                     "security_regression_hash"):
            set_(self, name, require_digest(getattr(self, name),
                                            f"adapter reference.{name}"))
        if self.base_model_canonical_identity_hash:
            set_(self, "base_model_canonical_identity_hash",
                 require_digest(self.base_model_canonical_identity_hash,
                                "adapter reference.base_model_canonical_identity_hash"))
        set_(self, "known_limitations",
             require_str_tuple(self.known_limitations,
                               "adapter reference.known_limitations", max_items=64))
        set_(self, "lora", dict(self.lora or {}))

        if self.method not in EVALUABLE_TRAINING_METHODS:
            raise EvaluationReferenceError(
                f"adapter reference: {self.method.value} produces an artefact this "
                f"repository has never built; allowed "
                f"{sorted(m.value for m in EVALUABLE_TRAINING_METHODS)}")
        if self.interrupted:
            raise EvaluationReferenceError(
                f"adapter reference: run {self.run_id!r} was interrupted. An adapter "
                f"whose training stopped part-way is not the adapter its plan describes, "
                f"and evaluating it would attribute the result to a plan nobody ran")
        if not self.completed:
            raise EvaluationReferenceError(
                f"adapter reference: run {self.run_id!r} did not complete")
        if str(self.run_state) != TrainingRunState.COMPLETED.value:
            raise EvaluationReferenceError(
                f"adapter reference: run {self.run_id!r} is in state "
                f"{self.run_state!r}; only {TrainingRunState.COMPLETED.value!r} may be "
                f"evaluated — a failed or quarantined run is evidence of a problem, not "
                f"a candidate")
        if not self.artifact_verified:
            raise EvaluationReferenceError(
                f"adapter reference: the artefacts for run {self.run_id!r} were not "
                f"re-verified from disk. A reference built from a manifest nobody "
                f"re-checked proves the manifest is well-formed, not that the files "
                f"still match it")

    # -- derived ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "reference_version": self.reference_version,
            "manifest_schema_version": self.manifest_schema_version,
            "run_id": self.run_id,
            "run_state": self.run_state,
            "adapter_manifest_hash": self.adapter_manifest_hash,
            "adapter_artifact_tree_hash": self.adapter_artifact_tree_hash,
            "plan_hash": self.plan_hash,
            "training_config_hash": self.training_config_hash,
            "base_model_identity_hash": self.base_model_identity_hash,
            "base_model_canonical_identity_hash":
                self.base_model_canonical_identity_hash,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "tokenizer_chat_template_hash": self.tokenizer_chat_template_hash,
            "dataset_reference_hash": self.dataset_reference_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "train_shard_hash": self.train_shard_hash,
            "validation_shard_hash": self.validation_shard_hash,
            "hidden_evaluation_hash": self.hidden_evaluation_hash,
            "security_regression_hash": self.security_regression_hash,
            "method": self.method.value,
            "lora": dict(sorted(self.lora.items())),
            "interrupted": self.interrupted,
            "completed": self.completed,
            "artifact_verified": self.artifact_verified,
            "artifact_file_count": self.artifact_file_count,
            "artifact_total_bytes": self.artifact_total_bytes,
            "known_limitations": list(self.known_limitations),
            "evaluation_role": EvaluationRole.CANDIDATE.value,
        }

    def reference_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "reference_hash": self.reference_hash()}
        assert_no_private_content(record, label="adapter evaluation reference")
        return record


def build_adapter_reference(run_directory: str | Path, *, lora: dict,
                            base_model_id: str) -> AdapterEvaluationReference:
    """The ONLY way to obtain an adapter reference. Re-verifies everything from disk.

    ``verify_completed_run`` is S3B's own authority: it re-parses the manifest, re-hashes
    every file it names, refuses anything the manifest does not name, and re-derives the
    tree digest. Calling it here rather than re-implementing the checks means an adapter
    cannot become evaluable through a second, weaker validator.
    """
    directory = Path(run_directory)
    problems = verify_completed_run(directory, lora=lora, base_model_id=base_model_id)
    if problems:
        raise EvaluationReferenceError(
            f"adapter reference: the run directory does not verify — "
            f"{'; '.join(problems[:5])}")
    manifest_path = directory / ADAPTER_MANIFEST_FILE
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvaluationReferenceError(
            f"adapter reference: {ADAPTER_MANIFEST_FILE} is unreadable ({exc})") from None
    manifest = AdapterManifest.from_dict(payload)
    return reference_from_manifest(manifest, artifact_verified=True)


def reference_from_manifest(manifest: AdapterManifest, *,
                            artifact_verified: bool) -> AdapterEvaluationReference:
    """Project a verified manifest onto the reference. Carries no path, only digests.

    The canonical identity is *re-derived* from the manifest's own ``base_model_id``,
    ``base_model_revision`` and tokenizer fields rather than read from a stored digest.
    That is the whole compatibility bridge: a manifest written before the canonical
    authority existed is upgraded by recomputation, never by rewriting the file. Its
    legacy ``base_model_identity_hash`` is carried through untouched alongside it.
    """
    if not isinstance(manifest, AdapterManifest):
        raise EvaluationReferenceError(
            f"adapter reference: expected an AdapterManifest, got "
            f"{type(manifest).__name__}; a reference may not be built from a bare path "
            f"or a raw dictionary")
    try:
        canonical = canonical_identity_hash(
            model_id=manifest.base_model_id, revision=manifest.base_model_revision,
            tokenizer_id=manifest.tokenizer_id,
            tokenizer_revision=manifest.tokenizer_revision)
    except SchemaError as exc:
        raise EvaluationReferenceError(
            f"adapter reference: run {manifest.run_id!r} records a base model this "
            f"repository will not evaluate ({exc}); the durable identity could not be "
            f"re-derived, and an unverifiable identity is not a matching one") from None
    return AdapterEvaluationReference(
        base_model_canonical_identity_hash=canonical,
        run_id=manifest.run_id,
        adapter_manifest_hash=manifest.manifest_hash(),
        adapter_artifact_tree_hash=manifest.tree_hash,
        plan_hash=manifest.plan_hash,
        training_config_hash=manifest.training_config_hash,
        base_model_identity_hash=manifest.base_model_identity_hash,
        tokenizer_identity_hash=manifest.tokenizer_identity_hash,
        tokenizer_chat_template_hash=manifest.tokenizer_chat_template_hash,
        dataset_reference_hash=manifest.dataset_reference_hash,
        dataset_manifest_hash=manifest.dataset_manifest_hash,
        train_shard_hash=manifest.train_shard_hash,
        validation_shard_hash=manifest.validation_shard_hash,
        hidden_evaluation_hash=manifest.hidden_evaluation_hash,
        security_regression_hash=manifest.security_regression_hash,
        method=manifest.method,
        lora=dict(manifest.lora),
        run_state=str(manifest.run_state),
        interrupted=bool(manifest.interrupted),
        completed=bool(manifest.completed),
        artifact_verified=bool(artifact_verified),
        artifact_file_count=len(manifest.files),
        artifact_total_bytes=int(manifest.total_bytes),
        manifest_schema_version=manifest.manifest_version,
        known_limitations=tuple(manifest.known_limitations),
    )


# ── pairing ───────────────────────────────────────────────────────────────────
def pairing_blockers(base: BaseModelEvaluationReference,
                     adapter: AdapterEvaluationReference) -> tuple[str, ...]:
    """Why these two things cannot be compared. Empty means they can.

    The adapter was fitted onto ONE base model with ONE tokenizer. Attaching it to a
    different one produces output, and the output means nothing — the delta would be
    the mismatch, not the training.
    """
    problems: list[str] = []
    base_canonical = base.base_model_canonical_identity_hash
    adapter_canonical = adapter.base_model_canonical_identity_hash
    if base_canonical and adapter_canonical:
        # The durable comparison. Both sides describe which weights load, so a licence
        # sentence or a cache probe on a different host cannot break a real pairing —
        # and no wording can make two different models look like one.
        if base_canonical != adapter_canonical:
            problems.append(
                f"the baseline is model {short(base_canonical)} but the adapter was "
                f"trained on {short(adapter_canonical)}; attaching it to a different "
                f"base measures the mismatch")
    elif base.base_model_identity_hash != adapter.base_model_identity_hash:
        # One side predates the canonical authority and cannot be upgraded. Fall back to
        # the legacy whole-record digest, which is stricter than necessary rather than
        # weaker: it refuses some legitimate pairs, and accepts no illegitimate one.
        problems.append(
            f"the baseline is model {short(base.base_model_identity_hash)} but the "
            f"adapter was trained on {short(adapter.base_model_identity_hash)}; "
            f"attaching it to a different base measures the mismatch")
    if base.tokenizer_identity_hash != adapter.tokenizer_identity_hash:
        problems.append(
            f"the baseline tokenizer is {short(base.tokenizer_identity_hash)} but the "
            f"adapter was trained against {short(adapter.tokenizer_identity_hash)}; "
            f"two tokenizations of one prompt are two prompts")
    return tuple(problems)


def contamination_blockers(adapter: AdapterEvaluationReference, *,
                           hidden_evaluation_hash: str,
                           security_regression_hash: str,
                           train_shard_hash: str = "",
                           forbid_security_overlap: bool = True) -> tuple[str, ...]:
    """Refuse an evaluation on material the adapter was fitted on.

    The adapter's manifest records which held-out shards existed at training time. If the
    shard this evaluation intends to use is the shard the adapter TRAINED on, the model
    has seen the answers and the resulting score is a memorisation measurement.
    """
    problems: list[str] = []
    hidden = str(hidden_evaluation_hash or "").strip().lower()
    security = str(security_regression_hash or "").strip().lower()
    train = str(train_shard_hash or "").strip().lower()

    if hidden and train and hidden == train:
        problems.append(
            "the hidden-evaluation shard and the training shard are the same bytes; "
            "the model was fitted on the answers it is about to be scored against")
    if hidden and hidden == adapter.train_shard_hash:
        problems.append(
            f"the hidden-evaluation shard {short(hidden)} is the shard adapter run "
            f"{adapter.run_id!r} trained on; this measures memorisation, not capability")
    if security and security == adapter.train_shard_hash:
        problems.append(
            f"the security-regression shard {short(security)} is the shard adapter run "
            f"{adapter.run_id!r} trained on")
    if forbid_security_overlap and security and security == adapter.validation_shard_hash:
        problems.append(
            f"the security-regression shard {short(security)} was used as adapter run "
            f"{adapter.run_id!r}'s validation set; policy forbids scoring a security "
            f"regression against material the run already selected against")
    if hidden and adapter.hidden_evaluation_hash and hidden != adapter.hidden_evaluation_hash:
        problems.append(
            f"the hidden-evaluation shard {short(hidden)} is not the shard the adapter "
            f"was bound to ({short(adapter.hidden_evaluation_hash)}); the comparison "
            f"would be against a different held-out set than the one that was approved")
    return tuple(problems)


@dataclass(frozen=True)
class ReferencePair:
    """A verified baseline and candidate, with the reasons they may not be used."""

    baseline: BaseModelEvaluationReference
    candidate: AdapterEvaluationReference
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "baseline_reference_hash": self.baseline.reference_hash(),
            "candidate_reference_hash": self.candidate.reference_hash(),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }

    def pair_hash(self) -> str:
        return sha256_obj(self.to_dict())


def verify_reference_pair(base: BaseModelEvaluationReference,
                          adapter: AdapterEvaluationReference, *,
                          hidden_evaluation_hash: str = "",
                          security_regression_hash: str = "",
                          require_live_executable: bool = False) -> ReferencePair:
    """Everything that must hold before these two may be compared."""
    blockers = list(pairing_blockers(base, adapter))
    if hidden_evaluation_hash or security_regression_hash:
        blockers.extend(contamination_blockers(
            adapter, hidden_evaluation_hash=hidden_evaluation_hash,
            security_regression_hash=security_regression_hash))
    warnings = list(base.live_blockers())
    if require_live_executable:
        blockers.extend(warnings)
        warnings = []
    return ReferencePair(baseline=base, candidate=adapter, blockers=tuple(blockers),
                         warnings=tuple(warnings))


__all__ = [
    "EVALUABLE_TRAINING_METHODS", "REFERENCE_SCHEMA_VERSION",
    "AdapterEvaluationReference", "BaseModelEvaluationReference",
    "EvaluationReferenceError", "EvaluationRole", "ReferencePair",
    "base_reference_from_identity", "build_adapter_reference",
    "contamination_blockers", "pairing_blockers", "reference_from_manifest",
    "verify_reference_pair",
]
