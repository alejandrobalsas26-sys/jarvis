"""training_gym/datasets/manifests.py — V69 M62 S2d: the immutable dataset version.

WHY THIS EXISTS
---------------
Every stage before this one produced a *decision*. This module produces the **artefact**
— the bytes a training run will actually read — and it is the last place where "we
checked earlier" can still be turned into evidence. After this, the only thing anybody
has is a directory and a manifest, so the manifest has to be able to prove, on its own,
months later, on a different host:

  * exactly which candidates are in the dataset, and which split each one landed in;
  * that each shard on disk is byte-for-byte the shard that was written;
  * which seed, which split algorithm, which policy version produced the assignment;
  * which leakage analysis permitted it and which revocation snapshot was in force;
  * which manifest came before this one.

WHY ``sha256_file`` AND NOT ``sha256_tree``
-------------------------------------------
:func:`~training_gym.schemas.sha256_tree` deliberately SKIPS symlinks — right for an
adapter directory, fatal as shard evidence: a shard replaced by a symlink would vanish
from the digest and the tree would still verify. Every shard is therefore hashed
individually with :func:`~training_gym.schemas.sha256_file`, which raises when the file
is absent rather than returning a digest of nothing, and the shard is refused outright if
it is a link.

WHAT A VERSION IS, AND WHEN IT EXISTS
-------------------------------------
``<root>/datasets/<dataset_id>/<dataset_version>/`` holds one ``*.jsonl`` shard per
populated split plus ``manifest.json``. The manifest is written LAST, so a directory
without one is not a version — it is residue, and :func:`verify_version` says so. The
directory itself is created with ``exist_ok=False``, which is both the "never overwrite a
version" rule and the only mutual exclusion this module needs: two concurrent promotions
race on one atomic ``mkdir`` and exactly one wins.

QUARANTINE IS AUDITED, NEVER WRITTEN
------------------------------------
Five splits get a shard: TRAIN, VALIDATION, HIDDEN_EVALUATION, SECURITY_REGRESSION,
ADVERSARIAL. Quarantined candidates are recorded in the manifest by id and digest only —
their content is never materialised in a shard, because a quarantine shard is one
mis-specified glob away from being an export.

REVOCATION DOES NOT REWRITE HISTORY
-----------------------------------
A revoked candidate is excluded from FUTURE versions. The version that already contains
it stays byte-identical and stays verifiable; the revocation ledger is what tells a reader
it is no longer trusted. That is the only design in which "still verifies" and "no longer
trusted" can both be true at once.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..atomicio import atomic_write_text
from ..schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    SensitivityClass,
    canonical_json,
    check_schema_version,
    reject_unknown_fields,
    require_bool,
    require_enum,
    require_id,
    require_int,
    require_mapping,
    sha256_file,
    sha256_obj,
    short,
)
from ..task_spec import TaskFamily, require_timestamp
from .candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    TargetSource,
    require_digest,
)
from .promotion import PROMOTION_POLICY_VERSION
from .split import SPLIT_ALGORITHM_VERSION

#: Bump when the manifest SHAPE changes in a way a reader cannot tolerate.
DATASET_MANIFEST_VERSION = "m62.dataset_manifest.1"

#: The parent value of the first version in a lineage. An explicit sentinel rather than
#: an empty string: "" is also what a truncated field looks like, and a chain whose root
#: is indistinguishable from a missing link is not a chain.
GENESIS_PARENT = "genesis"

#: Directory names under the store root. Fixed; no caller ever supplies a path segment.
DATASET_ROOT_DIR = "datasets"
MANIFEST_FILENAME = "manifest.json"

#: The splits that get a shard file. QUARANTINE is deliberately absent — see the module
#: docstring. Ordered, so shard records sort identically everywhere.
SHARD_SPLITS: tuple[DatasetSplit, ...] = (
    DatasetSplit.TRAIN, DatasetSplit.VALIDATION, DatasetSplit.HIDDEN_EVALUATION,
    DatasetSplit.SECURITY_REGRESSION, DatasetSplit.ADVERSARIAL,
)

#: Splits a model is fitted or steered on. Evaluation-only material may never land here.
TRAIN_SIDE_SPLITS: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.TRAIN, DatasetSplit.VALIDATION})

#: Ceilings. A dataset version is a reviewable artefact, not an archive.
MAX_VERSION_RECORDS = 100_000
MAX_SHARD_BYTES = 512 * 1024 * 1024


class ManifestError(SchemaError):
    """A manifest or a dataset version was refused. Never a partial write."""


def reviewer_safe_id(approval_hash: str) -> str:
    """A stable pseudonym for the human decision that approved a record.

    Derived from the approval digest rather than from the reviewer's identifier: the
    manifest is the most widely copied artefact in the subsystem, and it has no reason to
    carry an operator's name around with it. An auditor holding the review can confirm
    the pseudonym; nobody else learns anything from it.
    """
    return f"rev-{short(require_digest(approval_hash, 'manifest.approval_hash'), 16)}"


def shard_filename(split: DatasetSplit) -> str:
    """The one legal filename for a split's shard. Never caller-controlled."""
    if split not in SHARD_SPLITS:
        raise ManifestError(
            f"manifest: {split.value} has no shard. Quarantined material is recorded by "
            f"id and digest only; writing its content would put it one glob away from an "
            f"export")
    return f"{split.value}.jsonl"


def version_dir(root: str | Path, dataset_id: str, dataset_version: str) -> Path:
    """The version directory, with both identifiers validated BEFORE any join.

    ``require_id`` refuses separators, traversal, drive letters, UNC prefixes, leading
    dots and Windows device names, so the join below cannot escape *root* — the
    validation is deliberately upstream of the ``/`` operator rather than a check applied
    to the result, because a check applied to the result has already built the path.
    """
    ident = require_id(dataset_id, "manifest.dataset_id")
    version = require_id(dataset_version, "manifest.dataset_version")
    return Path(root) / DATASET_ROOT_DIR / ident / version


# ── the revocation snapshot ───────────────────────────────────────────────────
_REVOCATION_FIELDS: tuple[str, ...] = ("candidate_id", "candidate_hash", "reason",
                                       "actor", "at", "affected_dataset_versions")


@dataclass(frozen=True)
class RevocationSnapshot:
    """The set of revoked candidates a plan was computed against, frozen and hashed.

    A snapshot rather than a live query, because "which records were revoked" must be a
    fact the plan hash covers. If a revocation lands between planning and promotion, the
    recomputed snapshot hash differs, the recomputed plan hash differs, and the operator's
    confirmation stops matching — which is exactly the outcome that must not be silent.
    """

    entries: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        clean: list[dict] = []
        for index, raw in enumerate(self.entries):
            entry = require_mapping(raw, f"revocation[{index}]")
            reject_unknown_fields({k: v for k, v in entry.items() if k != SCHEMA_KEY},
                                  _REVOCATION_FIELDS, label=f"revocation[{index}]")
            reason = str(entry.get("reason", "")).strip()
            if not reason:
                raise ManifestError(
                    f"revocation[{index}]: a revocation with no reason cannot be audited "
                    f"and must not silently exclude a record from every future dataset")
            clean.append({
                "candidate_id": require_id(entry.get("candidate_id"),
                                           f"revocation[{index}].candidate_id"),
                "candidate_hash": str(entry.get("candidate_hash", "")).strip().lower(),
                "reason": reason[:1_000],
                "actor": require_id(entry.get("actor"), f"revocation[{index}].actor"),
                "at": require_timestamp(entry.get("at"), f"revocation[{index}].at"),
                "affected_dataset_versions": sorted(
                    str(v) for v in entry.get("affected_dataset_versions") or ()),
            })
        object.__setattr__(self, "entries", tuple(sorted(
            clean, key=lambda e: (e["candidate_id"], e["at"], e["candidate_hash"]))))

    @classmethod
    def from_store(cls, store: object) -> "RevocationSnapshot":
        """Read every revocation the store has recorded. Fails closed on a bad entry."""
        read = getattr(store, "revocations", None)
        if not callable(read):
            raise ManifestError("revocation snapshot: the store must expose "
                                "revocations()")
        return cls(entries=tuple(read()))

    @property
    def revoked_ids(self) -> frozenset[str]:
        return frozenset(e["candidate_id"] for e in self.entries)

    @property
    def revoked_hashes(self) -> frozenset[str]:
        return frozenset(e["candidate_hash"] for e in self.entries if e["candidate_hash"])

    def is_revoked(self, candidate: DatasetCandidate) -> bool:
        """True when either the id or the exact content digest has been revoked.

        Both, because a revocation naming an id must not be evaded by re-issuing the same
        bytes under a new id, and a revocation naming a digest must not be evaded by
        editing an unrelated field.
        """
        return (candidate.candidate_id in self.revoked_ids
                or candidate.candidate_hash() in self.revoked_hashes)

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "entries": [dict(e) for e in self.entries],
                "count": len(self.entries)}

    def snapshot_hash(self) -> str:
        return sha256_obj(self.to_dict())


# ── one shard's evidence ──────────────────────────────────────────────────────
_SHARD_FIELDS: tuple[str, ...] = ("split", "filename", "sha256_file", "size_bytes",
                                  "line_count")


@dataclass(frozen=True)
class ShardManifest:
    """What one split's file is: its name, its bytes, its digest and its record count.

    The digest is named ``sha256_file`` in the record on purpose. It is the hash of the
    FILE'S BYTES, not of the records it holds — a reader that confuses the two would
    happily accept a re-serialised shard whose content hash still matched.
    """

    split: DatasetSplit
    filename: str
    sha256_file: str
    size_bytes: int
    line_count: int

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "split", require_enum(self.split, DatasetSplit, "shard.split"))
        expected = shard_filename(self.split)
        if self.filename != expected:
            raise ManifestError(
                f"shard.filename: {self.filename!r} is not the one legal name for "
                f"{self.split.value} ({expected!r}); a caller-chosen shard filename is a "
                f"caller-chosen write destination")
        setattr_(self, "sha256_file", require_digest(self.sha256_file,
                                                     "shard.sha256_file"))
        setattr_(self, "size_bytes", require_int(self.size_bytes, "shard.size_bytes",
                                                 minimum=0, maximum=MAX_SHARD_BYTES))
        setattr_(self, "line_count", require_int(self.line_count, "shard.line_count",
                                                 minimum=0,
                                                 maximum=MAX_VERSION_RECORDS))

    def to_dict(self) -> dict:
        return {"split": self.split.value, "filename": self.filename,
                "sha256_file": self.sha256_file, "size_bytes": self.size_bytes,
                "line_count": self.line_count}

    @classmethod
    def from_dict(cls, payload: object) -> "ShardManifest":
        data = require_mapping(payload, "shard")
        reject_unknown_fields(data, _SHARD_FIELDS, label="shard")
        return cls(split=require_enum(data.get("split"), DatasetSplit, "shard.split"),
                   filename=str(data.get("filename", "")),
                   sha256_file=str(data.get("sha256_file", "")),
                   size_bytes=data.get("size_bytes", -1),
                   line_count=data.get("line_count", -1))


# ── one candidate's row in the manifest ───────────────────────────────────────
_ROW_FIELDS: tuple[str, ...] = (
    "candidate_id", "candidate_hash", "split", "task_hash", "attempt_hash",
    "human_reviewer_safe_id", "task_family", "target_source", "sensitivity",
    "source_model_id", "state", "evaluation_only", "dataset_eligible")


@dataclass(frozen=True)
class ManifestCandidateRow:
    """The manifest's own record of one candidate. Summary, but binding.

    It carries the candidate's digest, so the shard line it describes cannot be swapped;
    and it carries the state and the evaluation flag, so the manifest can refuse an
    illegal combination WITHOUT re-reading the shard — a manifest that could only detect
    contamination by consulting the file it is supposed to be vouching for would be
    proving nothing.
    """

    candidate_id: str
    candidate_hash: str
    split: DatasetSplit
    task_hash: str
    attempt_hash: str
    human_reviewer_safe_id: str
    task_family: TaskFamily
    target_source: TargetSource
    sensitivity: SensitivityClass
    source_model_id: str
    state: CandidateState = CandidateState.PROMOTED
    evaluation_only: bool = False
    dataset_eligible: bool = True

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "candidate_id", require_id(self.candidate_id, "row.candidate_id"))
        for name in ("candidate_hash", "task_hash", "attempt_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"row.{name}"))
        setattr_(self, "split", require_enum(self.split, DatasetSplit, "row.split"))
        setattr_(self, "task_family", require_enum(self.task_family, TaskFamily,
                                                   "row.task_family"))
        setattr_(self, "target_source", require_enum(self.target_source, TargetSource,
                                                     "row.target_source"))
        setattr_(self, "sensitivity", require_enum(self.sensitivity, SensitivityClass,
                                                   "row.sensitivity"))
        setattr_(self, "state", require_enum(self.state, CandidateState, "row.state"))
        setattr_(self, "evaluation_only", require_bool(self.evaluation_only,
                                                       "row.evaluation_only"))
        setattr_(self, "dataset_eligible", require_bool(self.dataset_eligible,
                                                        "row.dataset_eligible"))
        if not str(self.source_model_id or "").strip():
            raise ManifestError("row.source_model_id: required; a record nobody can "
                                "attribute to a model is not reproducible")
        if not str(self.human_reviewer_safe_id or "").strip():
            raise ManifestError(
                "row.human_reviewer_safe_id: required; a dataset row that names no "
                "approving decision is a row nobody approved")
        if self.split not in SHARD_SPLITS:
            raise ManifestError(
                f"row {self.candidate_id}: {self.split.value} may not appear in a "
                f"finalized version's records")
        if self.state is not CandidateState.PROMOTED:
            raise ManifestError(
                f"row {self.candidate_id}: state is {self.state.value}; a finalized "
                f"dataset version contains promoted records only")
        if not self.sensitivity.dataset_eligible:
            raise ManifestError(
                f"row {self.candidate_id}: sensitivity {self.sensitivity.value} is never "
                f"stored in a dataset version, in any split; restricted material is not "
                f"held-out data, it is data that does not belong in the artefact at all")
        if (self.evaluation_only or not self.dataset_eligible) \
                and self.split in TRAIN_SIDE_SPLITS:
            raise ManifestError(
                f"row {self.candidate_id}: evaluation_only or dataset-ineligible "
                f"material may never be placed in {self.split.value}; that is held-out "
                f"data a model would be fitted on")

    @classmethod
    def from_candidate(cls, candidate: DatasetCandidate,
                       split: DatasetSplit) -> "ManifestCandidateRow":
        return cls(candidate_id=candidate.candidate_id,
                   candidate_hash=candidate.candidate_hash(),
                   split=split, task_hash=candidate.task_hash,
                   attempt_hash=candidate.attempt_hash,
                   human_reviewer_safe_id=reviewer_safe_id(candidate.approval_hash),
                   task_family=candidate.task_family,
                   target_source=candidate.target_source,
                   sensitivity=candidate.sensitivity,
                   source_model_id=candidate.student_model_id,
                   state=candidate.state,
                   evaluation_only=candidate.evaluation_only,
                   dataset_eligible=candidate.dataset_eligible)

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "candidate_hash": self.candidate_hash,
                "split": self.split.value, "task_hash": self.task_hash,
                "attempt_hash": self.attempt_hash,
                "human_reviewer_safe_id": self.human_reviewer_safe_id,
                "task_family": self.task_family.value,
                "target_source": self.target_source.value,
                "sensitivity": self.sensitivity.value,
                "source_model_id": self.source_model_id, "state": self.state.value,
                "evaluation_only": self.evaluation_only,
                "dataset_eligible": self.dataset_eligible}

    @classmethod
    def from_dict(cls, payload: object) -> "ManifestCandidateRow":
        data = require_mapping(payload, "row")
        reject_unknown_fields(data, _ROW_FIELDS, label="row")
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            candidate_hash=str(data.get("candidate_hash", "")),
            split=require_enum(data.get("split"), DatasetSplit, "row.split"),
            task_hash=str(data.get("task_hash", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            human_reviewer_safe_id=str(data.get("human_reviewer_safe_id", "")),
            task_family=require_enum(data.get("task_family"), TaskFamily,
                                     "row.task_family"),
            target_source=require_enum(data.get("target_source"), TargetSource,
                                       "row.target_source"),
            sensitivity=require_enum(data.get("sensitivity"), SensitivityClass,
                                     "row.sensitivity"),
            source_model_id=str(data.get("source_model_id", "")),
            state=require_enum(data.get("state", CandidateState.PROMOTED.value),
                               CandidateState, "row.state"),
            evaluation_only=data.get("evaluation_only", False),
            dataset_eligible=data.get("dataset_eligible", True))


# ── the version manifest ──────────────────────────────────────────────────────
_MANIFEST_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "gym_version", "dataset_manifest_version", "split_algorithm_version",
    "promotion_policy_version", "seed", "split_policy_hash", "dataset_id",
    "dataset_version", "parent_manifest_hash", "created_at_utc", "candidates", "shards",
    "quarantine_audit", "allow_empty_splits", "total_records",
    "task_family_distribution", "target_source_distribution",
    "sensitivity_distribution", "source_model_distribution", "leakage_report_hash",
    "revocation_snapshot_hash", "manifest_hash")


def _distribution(values: Iterable[str]) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


@dataclass(frozen=True)
class DatasetVersionManifest:
    """One immutable dataset version, described completely enough to be re-verified.

    Named ``DatasetVersionManifest`` rather than ``DatasetManifest`` because
    :class:`core.dataset_pipeline.DatasetManifest` already exists and describes an M16
    dataset. They are different artefacts with different authority, and one name for both
    is how a bridge quietly becomes a second writer.
    """

    dataset_id: str
    dataset_version: str
    parent_manifest_hash: str
    created_at_utc: str
    seed: str
    split_policy_hash: str
    leakage_report_hash: str
    revocation_snapshot_hash: str
    candidates: tuple[ManifestCandidateRow, ...] = ()
    shards: tuple[ShardManifest, ...] = ()
    quarantine_audit: tuple[dict, ...] = ()
    allow_empty_splits: tuple[str, ...] = ()
    dataset_manifest_version: str = DATASET_MANIFEST_VERSION
    split_algorithm_version: str = SPLIT_ALGORITHM_VERSION
    promotion_policy_version: str = PROMOTION_POLICY_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "dataset_id", require_id(self.dataset_id, "manifest.dataset_id"))
        setattr_(self, "dataset_version", require_id(self.dataset_version,
                                                     "manifest.dataset_version"))
        setattr_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                           "manifest.created_at_utc"))
        setattr_(self, "seed", require_id(self.seed, "manifest.seed"))
        for name in ("split_policy_hash", "leakage_report_hash",
                     "revocation_snapshot_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"manifest.{name}"))
        if self.parent_manifest_hash != GENESIS_PARENT:
            setattr_(self, "parent_manifest_hash",
                     require_digest(self.parent_manifest_hash,
                                    "manifest.parent_manifest_hash"))
        setattr_(self, "allow_empty_splits", tuple(sorted(
            {require_enum(name, DatasetSplit, "manifest.allow_empty_splits").value
             for name in self.allow_empty_splits})))

        self._validate_records()
        self._validate_shards()

    def _validate_records(self) -> None:
        rows = tuple(sorted(self.candidates, key=lambda r: r.candidate_id))
        object.__setattr__(self, "candidates", rows)
        if len(rows) > MAX_VERSION_RECORDS:
            raise ManifestError(f"manifest: {len(rows)} records exceeds the "
                                f"{MAX_VERSION_RECORDS} ceiling")
        seen_ids: dict[str, str] = {}
        seen_hashes: dict[str, str] = {}
        for row in rows:
            if row.candidate_id in seen_ids:
                raise ManifestError(
                    f"manifest: candidate {row.candidate_id!r} appears twice (splits "
                    f"{seen_ids[row.candidate_id]} and {row.split.value}); one record "
                    f"belongs to exactly one split, and a duplicate is either a "
                    f"double-count or the same example on both sides of the boundary")
            if row.candidate_hash in seen_hashes:
                raise ManifestError(
                    f"manifest: candidates {seen_hashes[row.candidate_hash]!r} and "
                    f"{row.candidate_id!r} share content digest "
                    f"{short(row.candidate_hash)}; the identical example twice is a "
                    f"duplicate whatever it is called")
            seen_ids[row.candidate_id] = row.split.value
            seen_hashes[row.candidate_hash] = row.candidate_id

        audit: list[dict] = []
        for index, raw in enumerate(self.quarantine_audit):
            entry = require_mapping(raw, f"manifest.quarantine_audit[{index}]")
            reject_unknown_fields(entry, ("candidate_id", "candidate_hash", "reason"),
                                  label=f"manifest.quarantine_audit[{index}]")
            cid = require_id(entry.get("candidate_id"),
                             f"manifest.quarantine_audit[{index}].candidate_id")
            if cid in seen_ids:
                raise ManifestError(
                    f"manifest: candidate {cid!r} is both quarantined and placed in "
                    f"{seen_ids[cid]}; quarantine is not a split a record can also be in")
            audit.append({"candidate_id": cid,
                          "candidate_hash": str(entry.get("candidate_hash", "")).lower(),
                          "reason": str(entry.get("reason", ""))[:500]})
        object.__setattr__(self, "quarantine_audit",
                           tuple(sorted(audit, key=lambda e: e["candidate_id"])))

    def _validate_shards(self) -> None:
        shards = tuple(sorted(self.shards, key=lambda s: s.split.value))
        object.__setattr__(self, "shards", shards)
        by_split = {s.split: s for s in shards}
        if len(by_split) != len(shards):
            raise ManifestError("manifest: two shard records name the same split")
        counts = self.counts()
        for split, shard in sorted(by_split.items(), key=lambda kv: kv[0].value):
            if shard.line_count != counts.get(split.value, 0):
                raise ManifestError(
                    f"manifest: shard {shard.filename} declares {shard.line_count} lines "
                    f"but the manifest lists {counts.get(split.value, 0)} records for "
                    f"{split.value}")
        for split in SHARD_SPLITS:
            populated = counts.get(split.value, 0)
            if populated and split not in by_split:
                raise ManifestError(
                    f"manifest: {populated} record(s) are assigned to {split.value} but "
                    f"no shard describes them; a manifest without its shards is not a "
                    f"dataset")
            if not populated and split in by_split:
                raise ManifestError(
                    f"manifest: a shard is declared for {split.value} but no record is "
                    f"assigned to it")
        if not counts.get(DatasetSplit.TRAIN.value, 0):
            if DatasetSplit.TRAIN.value not in self.allow_empty_splits:
                raise ManifestError(
                    "manifest: this version has no TRAIN records. An empty training set "
                    "passes every integrity check and teaches nothing; if that is "
                    "genuinely intended the split policy must say so explicitly via "
                    "allow_empty_splits")

    # -- derived facts -----------------------------------------------------------
    def counts(self) -> dict:
        return _distribution(row.split.value for row in self.candidates)

    def candidate_ids_in(self, split: DatasetSplit) -> tuple[str, ...]:
        return tuple(row.candidate_id for row in self.candidates if row.split is split)

    @property
    def total_records(self) -> int:
        return len(self.candidates)

    def shard_for(self, split: DatasetSplit) -> ShardManifest | None:
        for shard in self.shards:
            if shard.split is split:
                return shard
        return None

    # -- serialization -----------------------------------------------------------
    def to_dict(self) -> dict:
        """The canonical record. Excludes ``manifest_hash`` — that is computed over it.

        Every field below is security-relevant: change the seed, the policy version, the
        parent, the leakage verdict, the revocation snapshot or any single record and the
        digest changes. That is the whole mechanism, and it is why nothing is omitted here
        "because it is only metadata".
        """
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "gym_version": GYM_VERSION,
            "dataset_manifest_version": self.dataset_manifest_version,
            "split_algorithm_version": self.split_algorithm_version,
            "promotion_policy_version": self.promotion_policy_version,
            "seed": self.seed,
            "split_policy_hash": self.split_policy_hash,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "parent_manifest_hash": self.parent_manifest_hash,
            "created_at_utc": self.created_at_utc,
            "candidates": [row.to_dict() for row in self.candidates],
            "shards": [shard.to_dict() for shard in self.shards],
            "quarantine_audit": [dict(e) for e in self.quarantine_audit],
            "allow_empty_splits": list(self.allow_empty_splits),
            "total_records": self.total_records,
            "task_family_distribution": _distribution(
                r.task_family.value for r in self.candidates),
            "target_source_distribution": _distribution(
                r.target_source.value for r in self.candidates),
            "sensitivity_distribution": _distribution(
                r.sensitivity.value for r in self.candidates),
            "source_model_distribution": _distribution(
                r.source_model_id for r in self.candidates),
            "leakage_report_hash": self.leakage_report_hash,
            "revocation_snapshot_hash": self.revocation_snapshot_hash,
        }

    def to_record(self) -> dict:
        return {**self.to_dict(), "manifest_hash": self.manifest_hash()}

    def manifest_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "DatasetVersionManifest":
        """Rebuild a manifest, refusing an absent, malformed or mismatched digest.

        Unlike the candidate reader, the digest is REQUIRED here rather than checked when
        present. A candidate without one is a record in flight; a manifest without one is
        an artefact claiming to be a verifiable dataset version while carrying no evidence
        at all, and accepting it would make every downstream integrity claim vacuous.
        """
        data = require_mapping(payload, "dataset manifest")
        reject_unknown_fields(data, _MANIFEST_FIELDS, label="dataset manifest")
        check_schema_version(data, label="dataset manifest")
        declared = str(data.get("manifest_hash", "")).strip()
        if not declared:
            raise ManifestError(
                "dataset manifest: missing manifest_hash; a version that carries no "
                "digest of itself cannot be shown to be the version that was written")
        require_digest(declared, "dataset manifest.manifest_hash")

        manifest = cls(
            dataset_id=str(data.get("dataset_id", "")),
            dataset_version=str(data.get("dataset_version", "")),
            parent_manifest_hash=str(data.get("parent_manifest_hash", "")),
            created_at_utc=str(data.get("created_at_utc", "")),
            seed=str(data.get("seed", "")),
            split_policy_hash=str(data.get("split_policy_hash", "")),
            leakage_report_hash=str(data.get("leakage_report_hash", "")),
            revocation_snapshot_hash=str(data.get("revocation_snapshot_hash", "")),
            candidates=tuple(ManifestCandidateRow.from_dict(r)
                             for r in data.get("candidates") or ()),
            shards=tuple(ShardManifest.from_dict(s) for s in data.get("shards") or ()),
            quarantine_audit=tuple(require_mapping(e, "quarantine_audit[]")
                                   for e in data.get("quarantine_audit") or ()),
            allow_empty_splits=tuple(str(s) for s in
                                     data.get("allow_empty_splits") or ()),
            dataset_manifest_version=str(data.get("dataset_manifest_version",
                                                  DATASET_MANIFEST_VERSION)),
            split_algorithm_version=str(data.get("split_algorithm_version",
                                                 SPLIT_ALGORITHM_VERSION)),
            promotion_policy_version=str(data.get("promotion_policy_version",
                                                  PROMOTION_POLICY_VERSION)),
        )
        # Recomputed rather than trusted: every aggregate below is derivable from the
        # records, so a stored value that disagrees is evidence of an edit, not an
        # alternative source of truth.
        rebuilt = manifest.to_dict()
        for field_name in ("total_records", "task_family_distribution",
                           "target_source_distribution", "sensitivity_distribution",
                           "source_model_distribution"):
            if field_name in data and data[field_name] != rebuilt[field_name]:
                raise ManifestError(
                    f"dataset manifest: stored {field_name} does not match the records "
                    f"it summarises; the manifest has been edited")
        if declared.lower() != manifest.manifest_hash():
            raise ManifestError(
                f"dataset manifest {manifest.dataset_version}: stored digest "
                f"{short(declared)!r} does not match its content "
                f"{short(manifest.manifest_hash())!r}; this is not the manifest that was "
                f"written")
        return manifest


# ── shard encoding ────────────────────────────────────────────────────────────
def encode_shard(candidates: Sequence[DatasetCandidate]) -> str:
    """The exact bytes of one shard: canonical JSON, one record per line, sorted by id.

    Deterministic on every axis that could otherwise drift — key order, whitespace,
    non-ASCII escaping and record order — because the shard digest is only evidence if
    the same candidates always produce the same file. The trailing newline is
    unconditional so ``line_count`` equals the number of records with no off-by-one.
    """
    lines: list[str] = []
    for candidate in sorted(candidates, key=lambda c: c.candidate_id):
        record = candidate.to_record()
        declared = str(record.get("candidate_hash", ""))
        if declared != candidate.candidate_hash():  # pragma: no cover — defensive
            raise ManifestError(f"shard: candidate {candidate.candidate_id} does not "
                                f"agree with its own digest")
        line = canonical_json(record)
        if "\n" in line:  # pragma: no cover — canonical_json never emits a newline
            raise ManifestError("shard: refusing a multi-line record")
        lines.append(line)
    return "".join(f"{line}\n" for line in lines)


def read_shard(path: Path) -> tuple[DatasetCandidate, ...]:
    """Load one shard, verifying every record's own digest. Refuses a link."""
    if path.is_symlink():
        raise ManifestError(f"shard {path.name}: is a symlink; a shard is the dataset, "
                            f"and a link is a destination somebody else chose")
    if not path.is_file():
        raise ManifestError(f"shard {path.name}: missing")
    import json
    records: list[DatasetCandidate] = []
    text = path.read_text(encoding="utf-8")
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            raise ManifestError(f"shard {path.name}: line {index + 1} is not a JSON "
                                f"record") from None
        records.append(DatasetCandidate.from_dict(payload))
    return tuple(records)


def _refuse_linked(target: Path) -> None:
    """A shard or manifest destination may not be a symlink or a hard link.

    Same reasoning as the candidate store: a symlink is a caller-chosen destination
    wearing a store-chosen name, and a hard link is a second name for one inode, which
    makes a file editable behind its own recorded digest.
    """
    if target.is_symlink():
        raise ManifestError(f"dataset version: {target.name!r} is a symlink")
    if target.exists():
        try:
            links = target.stat().st_nlink
        except OSError:  # pragma: no cover — a stat failure is itself a refusal
            raise ManifestError(f"dataset version: cannot stat {target.name!r}") from None
        if links > 1:
            raise ManifestError(
                f"dataset version: {target.name!r} has {links} hard links; a second name "
                f"for the same inode makes a shard editable behind its own digest")


# ── writing a version ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WrittenVersion:
    """What a finalized version actually is, reported back without a private path."""

    dataset_id: str
    dataset_version: str
    manifest: DatasetVersionManifest
    manifest_hash: str
    relative_paths: tuple[str, ...]
    residue: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
                "manifest_hash": self.manifest_hash,
                "relative_paths": list(self.relative_paths),
                "total_records": self.manifest.total_records,
                "counts": self.manifest.counts(), "residue": list(self.residue)}


def write_dataset_version(*, root: str | Path, dataset_id: str, dataset_version: str,
                          candidates: Sequence[DatasetCandidate],
                          assignments: Mapping[str, str], parent_manifest_hash: str,
                          created_at_utc: str, seed: str, split_policy_hash: str,
                          leakage_report_hash: str,
                          revocation: RevocationSnapshot,
                          quarantine_audit: Sequence[Mapping[str, object]] = (),
                          allow_empty_splits: Sequence[str] = ()) -> WrittenVersion:
    """Write one immutable dataset version, or leave nothing finalized behind.

    The order is the contract:

      1. the version directory is created with ``exist_ok=False`` — this is both the
         no-overwrite rule and the mutual exclusion;
      2. every shard is written atomically and immediately re-hashed FROM DISK, so the
         manifest records what is actually there rather than what was intended;
      3. the manifest is written last, so a directory without one is residue and not a
         version;
      4. the manifest is reloaded from disk and re-verified before this returns.

    Any failure after step 1 removes what was written and re-raises. Residue that cannot
    be removed is reported rather than swallowed — a half-written directory somebody
    believes was cleaned up is worse than one they know about.
    """
    destination = version_dir(root, dataset_id, dataset_version)
    if destination.exists():
        raise ManifestError(
            f"dataset version {dataset_version!r} already exists for dataset "
            f"{dataset_id!r}; a version is immutable and is never rewritten, because a "
            f"training run that pinned it must keep resolving to the same bytes")

    revoked = [c.candidate_id for c in candidates if revocation.is_revoked(c)]
    if revoked:
        raise ManifestError(
            f"dataset version {dataset_version}: {len(revoked)} revoked candidate(s) "
            f"({revoked[:5]}) were offered for promotion; a revoked record is excluded "
            f"from every future version")

    by_split: dict[DatasetSplit, list[DatasetCandidate]] = {}
    rows: list[ManifestCandidateRow] = []
    for candidate in candidates:
        name = str(assignments.get(candidate.candidate_id, ""))
        if not name:
            raise ManifestError(
                f"dataset version: candidate {candidate.candidate_id} has no split "
                f"assignment; an unplaced record must not be written anywhere")
        split = require_enum(name, DatasetSplit, "assignment.split")
        if split not in SHARD_SPLITS:
            raise ManifestError(
                f"dataset version: candidate {candidate.candidate_id} is assigned to "
                f"{split.value}, which has no shard")
        by_split.setdefault(split, []).append(candidate)
        rows.append(ManifestCandidateRow.from_candidate(candidate, split))

    destination.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        shards: list[ShardManifest] = []
        for split in SHARD_SPLITS:
            members = by_split.get(split, [])
            if not members:
                continue
            text = encode_shard(members)
            path = destination / shard_filename(split)
            _refuse_linked(path)
            atomic_write_text(path, text)
            written.append(path)
            # Re-hashed FROM DISK, not from the string in memory: the manifest must
            # describe the file a reader will open, and the two can differ if anything
            # touched the path between the write and now.
            shards.append(ShardManifest(
                split=split, filename=shard_filename(split),
                sha256_file=sha256_file(path), size_bytes=path.stat().st_size,
                line_count=len(members)))

        manifest = DatasetVersionManifest(
            dataset_id=dataset_id, dataset_version=dataset_version,
            parent_manifest_hash=parent_manifest_hash, created_at_utc=created_at_utc,
            seed=seed, split_policy_hash=split_policy_hash,
            leakage_report_hash=leakage_report_hash,
            revocation_snapshot_hash=revocation.snapshot_hash(),
            candidates=tuple(rows), shards=tuple(shards),
            quarantine_audit=tuple(dict(e) for e in quarantine_audit),
            allow_empty_splits=tuple(allow_empty_splits))

        manifest_path = destination / MANIFEST_FILENAME
        _refuse_linked(manifest_path)
        atomic_write_text(manifest_path, canonical_json(manifest.to_record()))
        written.append(manifest_path)

        verification = verify_version(root=root, dataset_id=dataset_id,
                                      dataset_version=dataset_version)
        if not verification.ok:
            raise ManifestError(
                f"dataset version {dataset_version}: written, but re-reading it failed "
                f"({list(verification.problems)}); a version that cannot be verified "
                f"immediately after being written is not a version")
    except BaseException:
        residue = _remove(written, destination)
        if residue:
            raise ManifestError(
                f"dataset version {dataset_version}: finalization failed AND cleanup "
                f"left residue at {residue}; that residue is not a dataset version and "
                f"must be removed by hand before retrying") from None
        raise

    return WrittenVersion(
        dataset_id=dataset_id, dataset_version=dataset_version,
        manifest=verification.manifest or manifest,
        manifest_hash=manifest.manifest_hash(),
        relative_paths=tuple(sorted(
            f"{DATASET_ROOT_DIR}/{dataset_id}/{dataset_version}/{p.name}"
            for p in written)))


def _remove(paths: Sequence[Path], directory: Path) -> tuple[str, ...]:
    """Best-effort cleanup that reports what it could not remove. Names only, no paths."""
    residue: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            residue.append(path.name)
    try:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
        elif directory.is_dir():
            residue.extend(sorted(p.name for p in directory.iterdir()))
    except OSError:  # pragma: no cover — reported rather than raised
        residue.append(directory.name)
    return tuple(sorted(set(residue)))


# ── verifying a version ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class VersionVerification:
    """Every way a stored version failed to be what its manifest says it is."""

    dataset_id: str
    dataset_version: str
    problems: tuple[str, ...] = ()
    manifest: DatasetVersionManifest | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.manifest is not None

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
                "ok": self.ok, "problems": list(self.problems),
                "manifest_hash": (self.manifest.manifest_hash() if self.manifest
                                  else ""),
                "total_records": (self.manifest.total_records if self.manifest else 0)}


def verify_version(*, root: str | Path, dataset_id: str,
                   dataset_version: str) -> VersionVerification:
    """Re-derive every claim the manifest makes, from the bytes on disk.

    Reports rather than raises so an operator sees every damaged shard at once. The
    manifest's own digest is checked first: if that fails nothing else it says can be
    relied on, and continuing would produce findings computed from an untrusted map.
    """
    try:
        directory = version_dir(root, dataset_id, dataset_version)
    except SchemaError as exc:
        return VersionVerification(dataset_id=str(dataset_id),
                                   dataset_version=str(dataset_version),
                                   problems=(str(exc),))
    if not directory.is_dir():
        return VersionVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                   problems=("the version directory does not exist",))
    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        return VersionVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                   problems=("manifest.json is a symlink",))
    if not manifest_path.is_file():
        return VersionVerification(
            dataset_id=dataset_id, dataset_version=dataset_version,
            problems=("manifest.json is missing; a shard directory with no manifest is "
                      "residue from a failed promotion, not a dataset version",))
    import json
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        return VersionVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                   problems=(f"manifest.json is unreadable ({exc})",))
    try:
        manifest = DatasetVersionManifest.from_dict(payload)
    except SchemaError as exc:
        return VersionVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                   problems=(str(exc),))

    problems: list[str] = []
    if manifest.dataset_id != dataset_id or manifest.dataset_version != dataset_version:
        problems.append(f"the manifest describes {manifest.dataset_id}/"
                        f"{manifest.dataset_version}, not the directory it is stored in")

    declared_names = {shard.filename for shard in manifest.shards} | {MANIFEST_FILENAME}
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.name not in declared_names:
            problems.append(f"{entry.name}: present in the version directory but named "
                            f"by no shard record")

    for shard in manifest.shards:
        path = directory / shard.filename
        if path.is_symlink():
            problems.append(f"{shard.filename}: is a symlink")
            continue
        if not path.is_file():
            problems.append(f"{shard.filename}: recorded in the manifest but missing")
            continue
        try:
            if path.stat().st_nlink > 1:
                problems.append(f"{shard.filename}: has more than one hard link")
        except OSError:  # pragma: no cover — defensive
            problems.append(f"{shard.filename}: cannot be stat'ed")
            continue
        size = path.stat().st_size
        if size != shard.size_bytes:
            problems.append(f"{shard.filename}: is {size} bytes, the manifest says "
                            f"{shard.size_bytes}")
        try:
            actual = sha256_file(path)
        except SchemaError as exc:  # pragma: no cover — is_file already checked
            problems.append(f"{shard.filename}: {exc}")
            continue
        if actual != shard.sha256_file:
            problems.append(f"{shard.filename}: content digest {short(actual)} does not "
                            f"match the manifest's {short(shard.sha256_file)}")
            continue
        try:
            records = read_shard(path)
        except SchemaError as exc:
            problems.append(f"{shard.filename}: {exc}")
            continue
        if len(records) != shard.line_count:
            problems.append(f"{shard.filename}: holds {len(records)} records, the "
                            f"manifest says {shard.line_count}")
        expected = set(manifest.candidate_ids_in(shard.split))
        actual_ids = {r.candidate_id for r in records}
        if expected != actual_ids:
            problems.append(f"{shard.filename}: the records it holds are not the records "
                            f"the manifest assigns to {shard.split.value}")
        by_id = {row.candidate_id: row for row in manifest.candidates}
        for record in records:
            row = by_id.get(record.candidate_id)
            if row is not None and row.candidate_hash != record.candidate_hash():
                problems.append(f"{shard.filename}: candidate {record.candidate_id} does "
                                f"not match the digest the manifest records for it")

    return VersionVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                               problems=tuple(problems), manifest=manifest)


def load_manifest(*, root: str | Path, dataset_id: str,
                  dataset_version: str) -> DatasetVersionManifest:
    """The manifest of a version that verifies. Raises when it does not.

    Deliberately the only loader the exporters and the bridge are allowed to use: reading
    a manifest without re-hashing its shards would let an edited dataset be exported on
    the strength of a manifest that still parses.
    """
    result = verify_version(root=root, dataset_id=dataset_id,
                            dataset_version=dataset_version)
    if not result.ok or result.manifest is None:
        raise ManifestError(
            f"dataset {dataset_id}/{dataset_version}: refusing to use an unverified "
            f"version — {list(result.problems) or ['no manifest']}")
    return result.manifest


def verify_parent(*, root: str | Path, dataset_id: str,
                  parent_manifest_hash: str) -> None:
    """Refuse a parent link that names a version this store cannot produce.

    ``GENESIS_PARENT`` is accepted only when the dataset has no versions yet: a lineage
    that can re-declare itself the first version whenever it likes has no lineage at all.
    """
    base = Path(root) / DATASET_ROOT_DIR / require_id(dataset_id, "manifest.dataset_id")
    existing = sorted(p.name for p in base.iterdir()
                      if p.is_dir() and not p.is_symlink()) if base.is_dir() else []
    if parent_manifest_hash == GENESIS_PARENT:
        if existing:
            raise ManifestError(
                f"dataset {dataset_id}: {len(existing)} version(s) already exist, so this "
                f"one may not declare itself the genesis of the lineage; name the parent "
                f"manifest hash it descends from")
        return
    digest = require_digest(parent_manifest_hash, "manifest.parent_manifest_hash")
    for name in existing:
        result = verify_version(root=root, dataset_id=dataset_id, dataset_version=name)
        if result.ok and result.manifest is not None:
            if result.manifest.manifest_hash() == digest:
                return
    raise ManifestError(
        f"dataset {dataset_id}: parent manifest {short(digest)} names no verifiable "
        f"existing version; a chain whose parent cannot be produced is not a chain")


def latest_manifest_hash(*, root: str | Path, dataset_id: str) -> str:
    """The most recent verifiable version's digest, or :data:`GENESIS_PARENT`.

    "Most recent" is by ``created_at_utc`` then by version id, never by filesystem mtime:
    an mtime is trivially rewritten and is not evidence of anything.
    """
    base = Path(root) / DATASET_ROOT_DIR / require_id(dataset_id, "manifest.dataset_id")
    if not base.is_dir():
        return GENESIS_PARENT
    best: tuple[str, str, str] | None = None
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        result = verify_version(root=root, dataset_id=dataset_id,
                                dataset_version=entry.name)
        if not result.ok or result.manifest is None:
            continue
        key = (result.manifest.created_at_utc, result.manifest.dataset_version,
               result.manifest.manifest_hash())
        if best is None or key > best:
            best = key
    return best[2] if best else GENESIS_PARENT


__all__ = [
    "DATASET_MANIFEST_VERSION", "DATASET_ROOT_DIR", "GENESIS_PARENT",
    "MANIFEST_FILENAME", "MAX_SHARD_BYTES", "MAX_VERSION_RECORDS", "SHARD_SPLITS",
    "TRAIN_SIDE_SPLITS", "DatasetVersionManifest",
    "ManifestCandidateRow", "ManifestError", "RevocationSnapshot", "ShardManifest",
    "VersionVerification", "WrittenVersion", "encode_shard", "latest_manifest_hash",
    "load_manifest", "read_shard", "reviewer_safe_id", "shard_filename",
    "verify_parent", "verify_version", "version_dir", "write_dataset_version",
]
