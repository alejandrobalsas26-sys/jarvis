"""training_gym/datasets/export.py — V69 M62 S2d: the SFT export.

WHY THIS EXISTS
---------------
An export is the moment the dataset stops being an auditable store and becomes a file
somebody feeds to a trainer. Everything the milestone has built — the human approval, the
split, the leakage analysis, the manifest — protects records *inside* the store. An
exporter that reads the wrong shard, or reads the right shard from an unverified
directory, hands all of it away in one line of code.

So this module has exactly one source: a dataset version that VERIFIES. It calls
:func:`~training_gym.datasets.manifests.load_manifest`, which re-hashes every shard before
returning, and it opens exactly one file — ``train.jsonl``. The held-out shards are never
read, not even to count them: code that has a path to hidden-evaluation material is code
that can leak it, and the cheapest way not to leak a file is not to open it.

FIVE FILTERS THAT ARE ALL REDUNDANT, ON PURPOSE
-----------------------------------------------
The TRAIN shard of a verified version already contains only promoted, non-evaluation
records — the manifest refuses anything else. Every one of those conditions is re-checked
here anyway, and every exclusion is COUNTED in the export manifest rather than silently
skipped. Redundancy is the point: this is the last gate, and a gate that trusts the
previous gate is a comment.

WHAT AN SFT ROW MAY NOT CONTAIN
-------------------------------
A held-out target, a rejected response, hidden reasoning, a raw teacher response, a
credential, a private absolute path, or a field name the export sanitizer would blank.
The rows are re-scanned individually before the file is written, because a scan of the
whole structure can be satisfied by a payload that is only dangerous once it is a row.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not tokenize, download a tokenizer, apply a chat template, contact a network or
know which model the data is for. The output is a plain JSONL of messages; making it
model-specific is the trainer's job and belongs where the model is known.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..atomicio import atomic_write_text
from ..schemas import (
    GYM_VERSION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    canonical_json,
    check_schema_version,
    reject_unknown_fields,
    require_id,
    require_int,
    require_mapping,
    sha256_file,
    sha256_obj,
    short,
)
from ..task_spec import require_timestamp
from ..teachers.sanitization import assert_clean
from .candidate import CandidateState, DatasetCandidate, DatasetSplit, require_digest
from .manifests import (
    ManifestError,
    RevocationSnapshot,
    load_manifest,
    read_shard,
    shard_filename,
    version_dir,
)
from .promotion import refuse_hidden_field_names

#: Bump when the row SHAPE changes. Recorded in every export manifest.
EXPORT_VERSION = "m62.sft_export.1"

#: Directory and filenames. Fixed: an exporter that took a caller's filename would take a
#: caller's write destination, and "just the name" is how traversal arrives.
EXPORT_DIR = "exports"
SFT_FILENAME = "sft_train.jsonl"
SFT_MANIFEST_FILENAME = "sft_train.manifest.json"

#: The only split an SFT export may read.
SFT_SOURCE_SPLIT = DatasetSplit.TRAIN


class ExportError(SchemaError):
    """An export was refused. Never a partial file, never a silent exclusion."""


def export_dir(root: str | Path, dataset_id: str, dataset_version: str) -> Path:
    """Where an export lands. Both identifiers validated before any join."""
    ident = require_id(dataset_id, "export.dataset_id")
    version = require_id(dataset_version, "export.dataset_version")
    return Path(root) / EXPORT_DIR / ident / version


# ── the row ───────────────────────────────────────────────────────────────────
def sft_row(candidate: DatasetCandidate, *, dataset_version: str,
            source_manifest_hash: str) -> dict:
    """One SFT example, carrying enough provenance to be traced back to its approval.

    The system message is present only when the candidate has one: emitting an empty
    system turn would teach the model that an empty system prompt is normal, and every
    row would then differ from what the episode actually ran with.

    Tool calls stay OUT of ``messages`` and keep their strict ``{name, arguments}``
    shape. A tool call is the one output a model learns to emit verbatim, so flattening
    it into prose here would be inventing a wire format on the way to the trainer.
    """
    messages: list[dict] = []
    if candidate.system_message.strip():
        messages.append({"role": "system", "content": candidate.system_message})
    messages.append({"role": "user", "content": candidate.user_prompt})
    messages.append({"role": "assistant", "content": candidate.target_text})
    row: dict = {
        "messages": messages,
        "metadata": {
            "candidate_id": candidate.candidate_id,
            "candidate_hash": candidate.candidate_hash(),
            "task_family": candidate.task_family.value,
            "target_source": candidate.target_source.value,
            "dataset_version": dataset_version,
            "source_manifest_hash": source_manifest_hash,
            "system_prompt_version": candidate.system_prompt_version,
            "source_model_id": candidate.student_model_id,
            "sensitivity": candidate.sensitivity.value,
        },
    }
    if candidate.tool_calls:
        row["tool_calls"] = [{"name": call["name"],
                              "arguments": dict(sorted(call["arguments"].items()))}
                             for call in candidate.tool_calls]
    return row


def row_hash(row: dict) -> str:
    """Every row is individually hashable, so one bad line can be named precisely."""
    return sha256_obj(row)


def _exclusion_reason(candidate: DatasetCandidate, split: DatasetSplit,
                      revocation: RevocationSnapshot) -> str:
    """Why this record may not be exported, or ``""``. Every check is deliberate."""
    if split is not SFT_SOURCE_SPLIT:
        return "not_train_split"
    if candidate.state is not CandidateState.PROMOTED:
        return "not_promoted"
    if revocation.is_revoked(candidate):
        return "revoked"
    if candidate.evaluation_only:
        return "evaluation_only"
    if not candidate.dataset_eligible:
        return "not_dataset_eligible"
    if not candidate.sensitivity.dataset_eligible:
        return "sensitivity_not_trainable"
    if not candidate.target_text.strip() or not candidate.user_prompt.strip():
        return "empty_prompt_or_target"
    return ""


# ── the export manifest ───────────────────────────────────────────────────────
_EXPORT_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "gym_version", "export_version", "dataset_id", "dataset_version",
    "dataset_manifest_hash", "source_split", "source_manifest_hash", "filename",
    "sha256_file", "size_bytes", "record_count", "created_at_utc",
    "system_prompt_versions", "task_family_distribution", "target_source_distribution",
    "source_model_distribution", "excluded_counts", "row_hashes_hash", "export_hash")


@dataclass(frozen=True)
class SFTExportManifest:
    """What the export file is, and which dataset version it came from."""

    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    source_manifest_hash: str
    filename: str
    sha256_file: str
    size_bytes: int
    record_count: int
    created_at_utc: str
    system_prompt_versions: tuple[str, ...] = ()
    task_family_distribution: dict = None  # type: ignore[assignment]
    target_source_distribution: dict = None  # type: ignore[assignment]
    source_model_distribution: dict = None  # type: ignore[assignment]
    excluded_counts: dict = None  # type: ignore[assignment]
    row_hashes_hash: str = ""
    source_split: str = SFT_SOURCE_SPLIT.value
    export_version: str = EXPORT_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "dataset_id", require_id(self.dataset_id, "export.dataset_id"))
        setattr_(self, "dataset_version", require_id(self.dataset_version,
                                                     "export.dataset_version"))
        for name in ("dataset_manifest_hash", "source_manifest_hash", "sha256_file",
                     "row_hashes_hash"):
            setattr_(self, name, require_digest(getattr(self, name), f"export.{name}"))
        setattr_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                           "export.created_at_utc"))
        setattr_(self, "size_bytes", require_int(self.size_bytes, "export.size_bytes",
                                                 minimum=1, maximum=1 << 40))
        setattr_(self, "record_count", require_int(self.record_count,
                                                   "export.record_count", minimum=1,
                                                   maximum=1_000_000))
        if self.filename != SFT_FILENAME:
            raise ExportError(f"export.filename: {self.filename!r} is not the one legal "
                              f"export name ({SFT_FILENAME!r})")
        if self.source_split != SFT_SOURCE_SPLIT.value:
            raise ExportError(
                f"export.source_split: {self.source_split!r}; an SFT export reads the "
                f"train split and nothing else, because every other split exists to "
                f"measure the model rather than to fit it")
        for name in ("task_family_distribution", "target_source_distribution",
                     "source_model_distribution", "excluded_counts"):
            setattr_(self, name, dict(sorted((getattr(self, name) or {}).items())))
        setattr_(self, "system_prompt_versions",
                 tuple(sorted(set(self.system_prompt_versions))))

    def to_dict(self) -> dict:
        return {
            SCHEMA_KEY: SCHEMA_VERSION, "gym_version": GYM_VERSION,
            "export_version": self.export_version, "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "source_split": self.source_split,
            "source_manifest_hash": self.source_manifest_hash,
            "filename": self.filename, "sha256_file": self.sha256_file,
            "size_bytes": self.size_bytes, "record_count": self.record_count,
            "created_at_utc": self.created_at_utc,
            "system_prompt_versions": list(self.system_prompt_versions),
            "task_family_distribution": dict(self.task_family_distribution),
            "target_source_distribution": dict(self.target_source_distribution),
            "source_model_distribution": dict(self.source_model_distribution),
            "excluded_counts": dict(self.excluded_counts),
            "row_hashes_hash": self.row_hashes_hash,
        }

    def to_record(self) -> dict:
        return {**self.to_dict(), "export_hash": self.export_hash()}

    def export_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "SFTExportManifest":
        data = require_mapping(payload, "export manifest")
        reject_unknown_fields(data, _EXPORT_FIELDS, label="export manifest")
        check_schema_version(data, label="export manifest")
        declared = str(data.get("export_hash", "")).strip()
        if not declared:
            raise ExportError("export manifest: missing export_hash")
        manifest = cls(
            dataset_id=str(data.get("dataset_id", "")),
            dataset_version=str(data.get("dataset_version", "")),
            dataset_manifest_hash=str(data.get("dataset_manifest_hash", "")),
            source_manifest_hash=str(data.get("source_manifest_hash", "")),
            filename=str(data.get("filename", "")),
            sha256_file=str(data.get("sha256_file", "")),
            size_bytes=data.get("size_bytes", 0),
            record_count=data.get("record_count", 0),
            created_at_utc=str(data.get("created_at_utc", "")),
            system_prompt_versions=tuple(data.get("system_prompt_versions") or ()),
            task_family_distribution=data.get("task_family_distribution") or {},
            target_source_distribution=data.get("target_source_distribution") or {},
            source_model_distribution=data.get("source_model_distribution") or {},
            excluded_counts=data.get("excluded_counts") or {},
            row_hashes_hash=str(data.get("row_hashes_hash", "")),
            source_split=str(data.get("source_split", SFT_SOURCE_SPLIT.value)),
            export_version=str(data.get("export_version", EXPORT_VERSION)))
        if require_digest(declared, "export.export_hash") != manifest.export_hash():
            raise ExportError(
                f"export manifest: stored digest {short(declared)!r} does not match its "
                f"content {short(manifest.export_hash())!r}")
        return manifest


@dataclass(frozen=True)
class SFTExport:
    """A written export, reported without a private absolute path."""

    manifest: SFTExportManifest
    relative_paths: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"record_count": self.manifest.record_count,
                "sha256_file": self.manifest.sha256_file,
                "export_hash": self.manifest.export_hash(),
                "dataset_id": self.manifest.dataset_id,
                "dataset_version": self.manifest.dataset_version,
                "excluded_counts": dict(self.manifest.excluded_counts),
                "relative_paths": list(self.relative_paths)}


def _distribution(values: Sequence[str]) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# ── the export ────────────────────────────────────────────────────────────────
def export_sft(*, root: str | Path, dataset_id: str, dataset_version: str,
               revocation: RevocationSnapshot, created_at_utc: str,
               out_root: str | Path | None = None) -> SFTExport:
    """Write the SFT export for one verified dataset version.

    Refuses rather than producing an empty file when nothing is eligible: a zero-record
    export is not a small dataset, it is the absence of one, and it would sail through
    every downstream integrity check on the way to a training run that learned nothing.
    """
    manifest = load_manifest(root=root, dataset_id=dataset_id,
                             dataset_version=dataset_version)
    shard = manifest.shard_for(SFT_SOURCE_SPLIT)
    if shard is None:
        raise ExportError(
            f"export: dataset {dataset_id}/{dataset_version} has no train shard; there "
            f"is nothing to fine-tune on and an empty export would not say so")

    directory = version_dir(root, dataset_id, dataset_version)
    records = read_shard(directory / shard_filename(SFT_SOURCE_SPLIT))
    by_id = {row.candidate_id: row for row in manifest.candidates}

    rows: list[dict] = []
    kept: list[DatasetCandidate] = []
    excluded: list[str] = []
    for candidate in sorted(records, key=lambda c: c.candidate_id):
        row_meta = by_id.get(candidate.candidate_id)
        if row_meta is None:  # pragma: no cover — verify_version already enforces this
            raise ExportError(f"export: {candidate.candidate_id} is in the shard but not "
                              f"in the manifest")
        reason = _exclusion_reason(candidate, row_meta.split, revocation)
        if reason:
            excluded.append(reason)
            continue
        row = sft_row(candidate, dataset_version=dataset_version,
                      source_manifest_hash=shard.sha256_file)
        # Re-scanned per row rather than once over the batch: the hidden-key guard and
        # the redaction scanner both answer questions about a record, and a row is the
        # record a trainer will actually read.
        refuse_hidden_field_names(row, label=f"sft row {candidate.candidate_id}")
        assert_clean(row, label=f"sft row {candidate.candidate_id}")
        rows.append(row)
        kept.append(candidate)

    if not rows:
        raise ExportError(
            f"export: dataset {dataset_id}/{dataset_version} yielded no eligible train "
            f"records (excluded: {_distribution(excluded)}). Refusing to write an empty "
            f"export rather than producing a file that verifies and teaches nothing")

    text = "".join(f"{canonical_json(row)}\n" for row in rows)
    destination = export_dir(out_root if out_root is not None else root, dataset_id,
                             dataset_version)
    path = destination / SFT_FILENAME
    manifest_path = destination / SFT_MANIFEST_FILENAME
    # BOTH names are checked. Removing only the data file and re-exporting would
    # otherwise overwrite a manifest that still describes the previous bytes, and the
    # replacement would verify against it exactly once — while it happened to match.
    for existing in (path, manifest_path):
        if existing.exists() or existing.is_symlink():
            raise ExportError(
                f"export: {existing.name} already exists for "
                f"{dataset_id}/{dataset_version}; an export is as immutable as the "
                f"version it came from")
    atomic_write_text(path, text)

    export_manifest = SFTExportManifest(
        dataset_id=dataset_id, dataset_version=dataset_version,
        dataset_manifest_hash=manifest.manifest_hash(),
        source_manifest_hash=shard.sha256_file, filename=SFT_FILENAME,
        sha256_file=sha256_file(path), size_bytes=path.stat().st_size,
        record_count=len(rows), created_at_utc=created_at_utc,
        system_prompt_versions=tuple(c.system_prompt_version for c in kept),
        task_family_distribution=_distribution([c.task_family.value for c in kept]),
        target_source_distribution=_distribution([c.target_source.value for c in kept]),
        source_model_distribution=_distribution([c.student_model_id for c in kept]),
        excluded_counts=_distribution(excluded),
        row_hashes_hash=sha256_obj([row_hash(r) for r in rows]))
    atomic_write_text(manifest_path, canonical_json(export_manifest.to_record()))

    verified = verify_sft_export(out_root=out_root if out_root is not None else root,
                                 dataset_id=dataset_id, dataset_version=dataset_version)
    if verified.problems:
        raise ExportError(f"export: written but immediately unverifiable "
                          f"({list(verified.problems)})")
    return SFTExport(manifest=export_manifest, relative_paths=(
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{SFT_FILENAME}",
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{SFT_MANIFEST_FILENAME}"))


@dataclass(frozen=True)
class ExportVerification:
    """Every way a stored export failed to be what its manifest says it is."""

    dataset_id: str
    dataset_version: str
    problems: tuple[str, ...] = ()
    manifest: SFTExportManifest | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.manifest is not None

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
                "ok": self.ok, "problems": list(self.problems),
                "record_count": self.manifest.record_count if self.manifest else 0}


def verify_sft_export(*, out_root: str | Path, dataset_id: str,
                      dataset_version: str) -> ExportVerification:
    """Re-hash a written export against its manifest. Reports rather than raises."""
    try:
        directory = export_dir(out_root, dataset_id, dataset_version)
    except SchemaError as exc:
        return ExportVerification(dataset_id=str(dataset_id),
                                  dataset_version=str(dataset_version),
                                  problems=(str(exc),))
    manifest_path = directory / SFT_MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return ExportVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                  problems=("the export manifest is missing",))
    import json
    try:
        manifest = SFTExportManifest.from_dict(
            json.loads(manifest_path.read_text(encoding="utf-8")))
    except (SchemaError, json.JSONDecodeError, ValueError, OSError) as exc:
        return ExportVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                                  problems=(f"the export manifest is unusable ({exc})",))

    problems: list[str] = []
    path = directory / manifest.filename
    if path.is_symlink():
        problems.append(f"{manifest.filename}: is a symlink")
    elif not path.is_file():
        problems.append(f"{manifest.filename}: recorded but missing")
    else:
        if path.stat().st_size != manifest.size_bytes:
            problems.append(f"{manifest.filename}: size does not match the manifest")
        actual = sha256_file(path)
        if actual != manifest.sha256_file:
            problems.append(f"{manifest.filename}: content digest {short(actual)} does "
                            f"not match the manifest's "
                            f"{short(manifest.sha256_file)}")
        else:
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            if len(lines) != manifest.record_count:
                problems.append(f"{manifest.filename}: holds {len(lines)} rows, the "
                                f"manifest says {manifest.record_count}")
            try:
                digests = [row_hash(json.loads(ln)) for ln in lines]
            except (json.JSONDecodeError, ValueError):
                problems.append(f"{manifest.filename}: contains a malformed row")
            else:
                if sha256_obj(digests) != manifest.row_hashes_hash:
                    problems.append(f"{manifest.filename}: the row digests do not match "
                                    f"the manifest")
    return ExportVerification(dataset_id=dataset_id, dataset_version=dataset_version,
                              problems=tuple(problems), manifest=manifest)


__all__ = [
    "EXPORT_DIR", "EXPORT_VERSION", "SFT_FILENAME", "SFT_MANIFEST_FILENAME",
    "SFT_SOURCE_SPLIT", "ExportError", "ExportVerification", "ManifestError", "SFTExport",
    "SFTExportManifest", "export_dir", "export_sft", "row_hash", "sft_row",
    "verify_sft_export",
]
