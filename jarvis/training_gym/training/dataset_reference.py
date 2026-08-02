"""training_gym/training/dataset_reference.py — V69 M62 S3A: what a run may be fitted on.

WHY THIS IS NOT A THIRD DEFINITION OF "A DATASET"
-------------------------------------------------
There are already two: :class:`core.dataset_pipeline.DatasetManifest` (M16) and
:class:`training_gym.datasets.manifests.DatasetVersionManifest` (M62). A third would be
the point at which a bridge quietly becomes a second writer. This module defines no
dataset. It defines a *reference* — a bundle of digests naming a version that already
exists — and it verifies that reference by calling the existing authorities:
``load_manifest`` (which re-hashes every shard from disk before returning) and
``verify_sft_export`` / ``verify_preference_export``.

THE RULE THE PLANNER ENFORCES
-----------------------------
A caller-supplied record count is a claim, and a caller-supplied digest is a claim. This
module converts claims into evidence by loading the artifact and re-deriving the value.
Anything that cannot be re-derived is reported as missing evidence, never assumed.

Placeholders (``REQUIRED_DATASET_MANIFEST_HASH`` and friends) are deliberately invalid
digests. A template config carrying them *parses* — so the planner can explain exactly
what is unfilled — but can never verify, so it can never be executable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..datasets.candidate import DatasetSplit
from ..schemas import (
    SchemaError,
    reject_unknown_fields,
    require_enum,
    require_id,
    require_int,
    require_mapping,
    sha256_obj,
    short,
)

#: Bumped when the shape of a reference changes. Part of the plan hash.
DATASET_REFERENCE_VERSION = "m62.training_dataset_reference.1"

#: Every unfilled slot in a template config starts with this. Chosen so it can never be
#: mistaken for a digest: it is uppercase, and a digest is lowercase hex.
PLACEHOLDER_PREFIX = "REQUIRED_"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(r"^REQUIRED_[A-Z0-9_]{1,64}$")

#: A dataset version is a reviewable artefact, not an archive.
MAX_REFERENCE_RECORDS = 100_000


class DatasetReferenceError(SchemaError):
    """A training dataset reference that will not be used as written."""


def is_placeholder(value: object) -> bool:
    """True for an unfilled template slot such as ``REQUIRED_IMMUTABLE_REVISION``."""
    return bool(_PLACEHOLDER_RE.match(str(value or "")))


def require_digest_or_placeholder(value: object, field: str) -> str:
    """A 64-character lowercase digest, or an explicit ``REQUIRED_*`` placeholder.

    Nothing else. A 12-character prefix is a claim about a hash and not the hash; an
    uppercase digest is a different string that compares unequal to the one on disk.
    """
    if not isinstance(value, str):
        raise DatasetReferenceError(
            f"{field}: expected a digest string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise DatasetReferenceError(
            f"{field}: required; an unnamed artefact cannot be verified against one")
    if is_placeholder(text):
        return text
    if not _DIGEST_RE.match(text):
        raise DatasetReferenceError(
            f"{field}: {short(text)!r} is neither a 64-character lowercase sha256 digest "
            f"nor a {PLACEHOLDER_PREFIX}* placeholder")
    return text


class ExportFormat(str, Enum):
    """The one artefact shape a run reads. Closed: there is no ``custom``."""

    SFT_JSONL = "sft_jsonl"
    PREFERENCE_JSONL = "preference_jsonl"
    M16_BRIDGE = "m16_bridge"


#: Every digest slot on a reference, in the order they are reported.
_DIGEST_FIELDS = (
    "dataset_manifest_hash", "export_manifest_hash", "source_dataset_content_hash",
    "train_split_manifest_hash", "train_shard_hash",
    "validation_split_manifest_hash", "validation_shard_hash",
    "hidden_evaluation_manifest_hash", "security_regression_manifest_hash",
)

_FIELDS = ("dataset_id", "dataset_version", "export_format", "record_count",
           "bridge_version", "dataset_schema_version", "reference_version",
           *_DIGEST_FIELDS)


@dataclass(frozen=True)
class TrainingDatasetReference:
    """The complete binding between a run and the corpus it may read.

    Every split the milestone cares about is named by two digests: the manifest record
    that describes the shard, and the shard file's own bytes. Binding only one would
    leave the other free to be something the reference never described.
    """

    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    export_format: ExportFormat
    export_manifest_hash: str
    source_dataset_content_hash: str
    train_split_manifest_hash: str
    train_shard_hash: str
    validation_split_manifest_hash: str
    validation_shard_hash: str
    hidden_evaluation_manifest_hash: str
    security_regression_manifest_hash: str
    record_count: int = 0
    bridge_version: str = ""
    dataset_schema_version: str = ""
    reference_version: str = DATASET_REFERENCE_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "dataset_id", require_id(self.dataset_id, "dataset_id"))
        setattr_(self, "dataset_version", require_id(self.dataset_version,
                                                     "dataset_version"))
        setattr_(self, "export_format",
                 require_enum(self.export_format, ExportFormat, "export_format"))
        for name in _DIGEST_FIELDS:
            setattr_(self, name,
                     require_digest_or_placeholder(getattr(self, name),
                                                   f"dataset_reference.{name}"))
        setattr_(self, "record_count",
                 require_int(self.record_count, "dataset_reference.record_count",
                             minimum=0, maximum=MAX_REFERENCE_RECORDS))
        for name in ("bridge_version", "dataset_schema_version"):
            value = getattr(self, name)
            if value:
                setattr_(self, name, require_id(value, f"dataset_reference.{name}"))
        setattr_(self, "reference_version",
                 require_id(self.reference_version, "dataset_reference.reference_version"))
        if (self.export_format is ExportFormat.M16_BRIDGE) != bool(self.bridge_version):
            raise DatasetReferenceError(
                "dataset_reference.bridge_version: an m16_bridge reference must name the "
                "bridge version that produced it, and no other format may claim one")
        if self.train_shard_hash == self.hidden_evaluation_manifest_hash:
            raise DatasetReferenceError(
                "dataset_reference: the train shard and the hidden evaluation are named "
                "by the same digest; a held-out set the model was fitted on is not held "
                "out")

    # ── derived ─────────────────────────────────────────────────────────────
    @property
    def placeholders(self) -> tuple[str, ...]:
        """Every slot still carrying a template value. Empty means fully specified."""
        found = [name for name in _DIGEST_FIELDS if is_placeholder(getattr(self, name))]
        if self.record_count <= 0:
            found.append("record_count")
        return tuple(sorted(found))

    @property
    def is_fully_specified(self) -> bool:
        return not self.placeholders

    def to_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "export_format": self.export_format.value,
            "record_count": self.record_count,
            "bridge_version": self.bridge_version,
            "dataset_schema_version": self.dataset_schema_version,
            "reference_version": self.reference_version,
            **{name: getattr(self, name) for name in _DIGEST_FIELDS},
        }

    def reference_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "TrainingDatasetReference":
        data = require_mapping(payload, "dataset_reference")
        reject_unknown_fields(data, _FIELDS, label="dataset_reference")
        for required in ("dataset_id", "dataset_version", "export_format"):
            if required not in data:
                raise DatasetReferenceError(
                    f"dataset_reference: missing {required!r}")
        blank = cls.placeholder()
        kwargs: dict[str, object] = {
            "dataset_id": data["dataset_id"],
            "dataset_version": data["dataset_version"],
            "export_format": data["export_format"],
            "record_count": data.get("record_count", 0),
            "bridge_version": data.get("bridge_version", ""),
            "dataset_schema_version": data.get("dataset_schema_version", ""),
            "reference_version": data.get("reference_version",
                                          DATASET_REFERENCE_VERSION),
        }
        for name in _DIGEST_FIELDS:
            kwargs[name] = data.get(name, getattr(blank, name))
        return cls(**kwargs)  # type: ignore[arg-type]

    @classmethod
    def placeholder(cls, *, dataset_id: str = "REPLACE-ME",
                    dataset_version: str = "REPLACE-ME") -> "TrainingDatasetReference":
        """A fully unfilled reference. Parses, reports, and can never verify."""
        return cls(
            dataset_id=dataset_id, dataset_version=dataset_version,
            export_format=ExportFormat.SFT_JSONL,
            dataset_manifest_hash="REQUIRED_DATASET_MANIFEST_HASH",
            export_manifest_hash="REQUIRED_EXPORT_MANIFEST_HASH",
            source_dataset_content_hash="REQUIRED_SOURCE_DATASET_CONTENT_HASH",
            train_split_manifest_hash="REQUIRED_TRAIN_SPLIT_MANIFEST_HASH",
            train_shard_hash="REQUIRED_TRAIN_SHARD_HASH",
            validation_split_manifest_hash="REQUIRED_VALIDATION_SPLIT_MANIFEST_HASH",
            validation_shard_hash="REQUIRED_VALIDATION_SHARD_HASH",
            hidden_evaluation_manifest_hash="REQUIRED_HIDDEN_EVALUATION_MANIFEST_HASH",
            security_regression_manifest_hash=(
                "REQUIRED_SECURITY_REGRESSION_MANIFEST_HASH"),
            record_count=0)


# ══════════════════════════════════════════════════════════════════════════════
#  Verification
# ══════════════════════════════════════════════════════════════════════════════
class DatasetEvidence(str, Enum):
    """How much of the reference could actually be re-derived from disk."""

    VERIFIED = "verified"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REFUSED = "refused"


@dataclass(frozen=True)
class DatasetReferenceVerification:
    """Everything the planner learned by loading the artefacts the reference names.

    ``problems`` are refusals — a claim contradicted by the bytes. ``missing`` is absent
    evidence. They are reported separately because they mean different things: one is a
    dataset that is wrong, the other is a dataset nobody has finished describing, and
    collapsing them would let a template look like a tamper.
    """

    dataset_id: str
    dataset_version: str
    evidence: DatasetEvidence
    problems: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    verified_record_count: int = 0
    split_counts: tuple[tuple[str, int], ...] = ()

    @property
    def ok(self) -> bool:
        return self.evidence is DatasetEvidence.VERIFIED

    def to_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "evidence": self.evidence.value,
            "ok": self.ok,
            "problems": list(self.problems),
            "missing": list(self.missing),
            "verified_record_count": self.verified_record_count,
            "split_counts": {name: count for name, count in self.split_counts},
        }

    def verification_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _shard_digests(manifest, split: DatasetSplit) -> tuple[str, str] | None:
    """``(manifest-record digest, file-bytes digest)`` for one split, or None."""
    shard = manifest.shard_for(split)
    if shard is None:
        return None
    record = {"split": shard.split.value, "filename": shard.filename,
              "sha256_file": shard.sha256_file, "size_bytes": shard.size_bytes,
              "line_count": shard.line_count}
    return sha256_obj(record), shard.sha256_file


def split_digests(manifest, split: DatasetSplit) -> tuple[str, str] | None:
    """Public form of :func:`_shard_digests`, used to fill a reference from a store."""
    return _shard_digests(manifest, split)


def verify_training_dataset_reference(
    reference: TrainingDatasetReference, *, root: str | Path,
    needs_preference_data: bool = False,
    training_split: DatasetSplit = DatasetSplit.TRAIN,
    validation_split: DatasetSplit = DatasetSplit.VALIDATION,
    hidden_evaluation_split: DatasetSplit = DatasetSplit.HIDDEN_EVALUATION,
    security_regression_split: DatasetSplit = DatasetSplit.SECURITY_REGRESSION,
    revoked_candidate_ids: frozenset[str] = frozenset(),
    revocation_snapshot_hash: str = "",
    out_root: str | Path | None = None,
) -> DatasetReferenceVerification:
    """Re-derive every claim the reference makes, from the artefacts it names.

    Reports rather than raises so an operator sees every unfilled slot and every
    contradiction at once. A reference carrying placeholders short-circuits to
    ``INSUFFICIENT_EVIDENCE`` *before* any file is opened — there is nothing to check a
    template against, and opening files to prove that would be theatre.
    """
    unfilled = reference.placeholders
    if unfilled:
        return DatasetReferenceVerification(
            dataset_id=reference.dataset_id, dataset_version=reference.dataset_version,
            evidence=DatasetEvidence.INSUFFICIENT_EVIDENCE,
            missing=tuple(f"dataset_reference.{name} is still a template placeholder"
                          for name in unfilled))

    problems: list[str] = []
    missing: list[str] = []

    # 1. The only sanctioned loader: it re-hashes every shard before it returns.
    from ..datasets.manifests import ManifestError, load_manifest

    try:
        manifest = load_manifest(root=root, dataset_id=reference.dataset_id,
                                 dataset_version=reference.dataset_version)
    except (ManifestError, SchemaError, OSError) as exc:
        return DatasetReferenceVerification(
            dataset_id=reference.dataset_id, dataset_version=reference.dataset_version,
            evidence=DatasetEvidence.REFUSED, problems=(f"{type(exc).__name__}: {exc}",))

    # 2. The manifest is the one the reference was built against.
    actual_manifest_hash = manifest.manifest_hash()
    if actual_manifest_hash != reference.dataset_manifest_hash:
        problems.append(
            f"dataset_manifest_hash: the stored version hashes to "
            f"{short(actual_manifest_hash)}, the reference names "
            f"{short(reference.dataset_manifest_hash)}; the dataset changed after the "
            f"config was written")

    # 3. Every split this milestone requires is present and matches, digest for digest.
    required = (
        (training_split, "train_split_manifest_hash", "train_shard_hash"),
        (validation_split, "validation_split_manifest_hash", "validation_shard_hash"),
        (hidden_evaluation_split, "hidden_evaluation_manifest_hash", ""),
        (security_regression_split, "security_regression_manifest_hash", ""),
    )
    counts = manifest.counts()
    for split, record_field, file_field in required:
        digests = _shard_digests(manifest, split)
        if digests is None:
            missing.append(
                f"{split.value}: the version declares no shard for this split; "
                f"{'training cannot proceed' if split is training_split else 'the run has no way to be measured'}")
            continue
        record_digest, file_digest = digests
        declared_record = getattr(reference, record_field)
        if record_digest != declared_record:
            problems.append(
                f"{record_field}: the manifest's {split.value} record hashes to "
                f"{short(record_digest)}, the reference names {short(declared_record)}")
        if file_field:
            declared_file = getattr(reference, file_field)
            if file_digest != declared_file:
                problems.append(
                    f"{file_field}: the {split.value} shard's bytes hash to "
                    f"{short(file_digest)}, the reference names {short(declared_file)}")
        if counts.get(split.value, 0) <= 0 and split in (training_split,
                                                         validation_split):
            problems.append(
                f"{split.value}: holds no records; a split a run is fitted or stopped on "
                f"may not be empty")

    # 4. No two required splits may be the same bytes.
    seen: dict[str, str] = {}
    for split, _record_field, _file_field in required:
        digests = _shard_digests(manifest, split)
        if digests is None:
            continue
        collision = seen.get(digests[1])
        if collision is not None:
            problems.append(
                f"{split.value}: has the same shard digest as {collision}; two splits "
                f"holding identical bytes are one split wearing two names")
        seen[digests[1]] = split.value

    # 5. No record a human revoked may still be trainable.
    train_side_ids: set[str] = set()
    for split in (training_split, validation_split):
        train_side_ids |= set(manifest.candidate_ids_in(split))
    revoked_in_train = sorted(train_side_ids & set(revoked_candidate_ids))
    if revoked_in_train:
        problems.append(
            f"{len(revoked_in_train)} revoked candidate(s) are still assigned to a "
            f"train-side split; a revocation that does not remove the record from "
            f"training is a revocation in name only")
    if revocation_snapshot_hash and (
            revocation_snapshot_hash != manifest.revocation_snapshot_hash):
        problems.append(
            "revocation_snapshot_hash: the current revocation state is not the one this "
            "version was promoted under; re-promote before training on it")

    # 6. No evaluation-only record may be trainable.
    by_id = {row.candidate_id: row for row in manifest.candidates}
    leaked = sorted(cid for cid in train_side_ids
                    if getattr(by_id.get(cid), "evaluation_only", False))
    if leaked:
        problems.append(
            f"{len(leaked)} evaluation-only record(s) are assigned to a train-side "
            f"split")
    ineligible = sorted(cid for cid in train_side_ids
                        if by_id.get(cid) is not None
                        and not getattr(by_id[cid], "dataset_eligible", True))
    if ineligible:
        problems.append(
            f"{len(ineligible)} record(s) in a train-side split are not dataset-eligible")

    # 7. Split provenance: a version with no recorded seed cannot be reproduced.
    if not manifest.seed:
        missing.append("the version records no split seed")
    if not manifest.split_policy_hash:
        missing.append("the version records no split policy hash")
    if not manifest.leakage_report_hash:
        missing.append("the version records no leakage report hash; a check that did not "
                       "run proved nothing")

    # 8. The export the trainer would actually read.
    export_root = out_root if out_root is not None else root
    if reference.export_format is ExportFormat.SFT_JSONL:
        from ..datasets.export import verify_sft_export

        result = verify_sft_export(out_root=export_root,
                                   dataset_id=reference.dataset_id,
                                   dataset_version=reference.dataset_version)
        if not result.ok or result.manifest is None:
            problems.extend(f"sft export: {p}" for p in result.problems)
            if not result.problems:
                missing.append("sft export: no manifest was produced")
        else:
            _check_export(reference, result.manifest, problems, label="sft export")
    elif reference.export_format is ExportFormat.PREFERENCE_JSONL:
        from ..datasets.preference import verify_preference_export

        pref_problems = verify_preference_export(
            out_root=export_root, dataset_id=reference.dataset_id,
            dataset_version=reference.dataset_version)
        problems.extend(f"preference export: {p}" for p in pref_problems)
    elif reference.export_format is ExportFormat.M16_BRIDGE:
        from ..datasets.bridge import BRIDGE_VERSION

        if reference.bridge_version != BRIDGE_VERSION:
            problems.append(
                f"bridge_version: the reference names {reference.bridge_version!r}, this "
                f"build produces {BRIDGE_VERSION!r}")

    # 9. A preference method may not be planned against an SFT corpus.
    if needs_preference_data and reference.export_format is not (
            ExportFormat.PREFERENCE_JSONL):
        problems.append(
            f"export_format: a preference method requires a verified preference export, "
            f"not {reference.export_format.value!r}; an SFT file has no rejected side, "
            f"so training on it optimises a preference against nothing")
    if not needs_preference_data and reference.export_format is (
            ExportFormat.PREFERENCE_JSONL):
        problems.append(
            "export_format: a supervised method may not read a preference export; the "
            "rejected response in each pair is material a human marked as the WRONG "
            "answer, and an SFT run would fit it as a target")

    verified_count = manifest.total_records
    if reference.record_count != verified_count:
        problems.append(
            f"record_count: the reference claims {reference.record_count}, the verified "
            f"version holds {verified_count}")

    if problems:
        evidence = DatasetEvidence.REFUSED
    elif missing:
        evidence = DatasetEvidence.INSUFFICIENT_EVIDENCE
    else:
        evidence = DatasetEvidence.VERIFIED
    return DatasetReferenceVerification(
        dataset_id=reference.dataset_id, dataset_version=reference.dataset_version,
        evidence=evidence, problems=tuple(problems), missing=tuple(missing),
        verified_record_count=verified_count,
        split_counts=tuple(sorted(counts.items())))


def _check_export(reference: TrainingDatasetReference, manifest,
                  problems: list[str], *, label: str) -> None:
    """Bind the reference to the export manifest that was actually written."""
    actual = manifest.export_hash()
    if actual != reference.export_manifest_hash:
        problems.append(
            f"{label}: the written export manifest hashes to {short(actual)}, the "
            f"reference names {short(reference.export_manifest_hash)}")
    source = getattr(manifest, "row_hashes_hash", "")
    if source and source != reference.source_dataset_content_hash:
        problems.append(
            f"{label}: the exported rows hash to {short(source)}, the reference names "
            f"{short(reference.source_dataset_content_hash)}")
    declared = getattr(manifest, "dataset_manifest_hash", "")
    if declared and declared != reference.dataset_manifest_hash:
        problems.append(
            f"{label}: the export was produced from dataset manifest "
            f"{short(declared)}, not the one the reference names")


__all__ = [
    "DATASET_REFERENCE_VERSION", "MAX_REFERENCE_RECORDS", "PLACEHOLDER_PREFIX",
    "DatasetEvidence", "DatasetReferenceError", "DatasetReferenceVerification",
    "ExportFormat", "TrainingDatasetReference", "is_placeholder",
    "require_digest_or_placeholder", "split_digests",
    "verify_training_dataset_reference",
]
