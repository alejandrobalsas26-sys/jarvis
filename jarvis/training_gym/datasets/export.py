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
returning, and it opens exactly one file per export — ``train.jsonl`` for the corpus a
model is FITTED on, ``validation.jsonl`` for the corpus a run is STEERED by. The held-out
shards are never read, not even to count them: code that has a path to
hidden-evaluation material is code that can leak it, and the cheapest way not to leak a
file is not to open it.

TWO EXPORTS, ONE CODE PATH, TWO CLOSED NAMES
--------------------------------------------
:func:`export_sft` and :func:`export_sft_validation` are the same procedure over a
different split, and the split is not a caller-supplied value: it is chosen by which
function was called, and each has exactly one legal pair of filenames. That is why
:data:`EXPORTABLE_SPLITS` is a table rather than a membership test — a caller cannot ask
for the hidden-evaluation split under the train export's filename, because the
``(split, filename)`` pair has to be an entry in it. VALIDATION is exportable because
``TRAIN_SIDE_SPLITS`` already says a model may be steered on it; the four held-out splits
are absent from the table and there is no argument that adds them.

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

#: The train-side steering export. A SEPARATE pair of names rather than a suffix on the
#: train export's: two files whose names differ by one word are two files somebody
#: eventually confuses, and the manifest binds the name it was written under.
SFT_VALIDATION_FILENAME = "sft_validation.jsonl"
SFT_VALIDATION_MANIFEST_FILENAME = "sft_validation.manifest.json"

#: The only split an SFT *training* export may read.
SFT_SOURCE_SPLIT = DatasetSplit.TRAIN

#: The only split an SFT *validation* export may read.
SFT_VALIDATION_SOURCE_SPLIT = DatasetSplit.VALIDATION

#: The closed table of exportable splits and the one legal pair of filenames for each.
#: Membership here is the whole authorisation: a split absent from this mapping has no
#: export function, no filename and no manifest that would accept it. Both members are
#: train-side (``TRAIN_SIDE_SPLITS``) — a model may be fitted on TRAIN and steered by
#: VALIDATION. HIDDEN_EVALUATION, SECURITY_REGRESSION, ADVERSARIAL and QUARANTINE are
#: deliberately absent, and no argument adds them.
EXPORTABLE_SPLITS: dict[DatasetSplit, tuple[str, str]] = {
    SFT_SOURCE_SPLIT: (SFT_FILENAME, SFT_MANIFEST_FILENAME),
    SFT_VALIDATION_SOURCE_SPLIT: (SFT_VALIDATION_FILENAME,
                                  SFT_VALIDATION_MANIFEST_FILENAME),
}


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
                      revocation: RevocationSnapshot, *,
                      source_split: DatasetSplit = SFT_SOURCE_SPLIT) -> str:
    """Why this record may not be exported, or ``""``. Every check is deliberate.

    ``source_split`` is the split the export was asked for, never a caller-supplied
    filter: the reason string names it, so ``not_train_split`` keeps exactly the wording
    — and therefore exactly the ``excluded_counts`` distribution, and therefore exactly
    the export hash — that every train export written before the validation export
    existed already recorded.
    """
    if split is not source_split:
        return f"not_{source_split.value}_split"
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
        # The pair is checked, not the two fields independently. A manifest naming the
        # validation split under the train export's filename describes a file that is
        # not the one it was written as, and either field alone would accept it.
        legal = {split.value: names for split, names in EXPORTABLE_SPLITS.items()}
        names = legal.get(self.source_split)
        if names is None:
            raise ExportError(
                f"export.source_split: {self.source_split!r}; an SFT export reads a "
                f"train-side split — {sorted(legal)} — and nothing else, because every "
                f"other split exists to measure the model rather than to fit or steer "
                f"it")
        if self.filename != names[0]:
            raise ExportError(
                f"export.filename: {self.filename!r} is not the one legal export name "
                f"for the {self.source_split} split ({names[0]!r})")
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
    """Write the SFT TRAINING export — the rows a model is fitted on."""
    return _export_split(
        split=SFT_SOURCE_SPLIT, root=root, dataset_id=dataset_id,
        dataset_version=dataset_version, revocation=revocation,
        created_at_utc=created_at_utc, out_root=out_root)


def export_sft_validation(*, root: str | Path, dataset_id: str, dataset_version: str,
                          revocation: RevocationSnapshot, created_at_utc: str,
                          out_root: str | Path | None = None) -> SFTExport:
    """Write the SFT VALIDATION export — the rows a run is steered BY, never fitted on.

    Same procedure, same filters, same row shape and same tokenizer-free output as the
    training export; a different split and a different pair of filenames. It exists
    because the split was already assigned by the deterministic splitter and already
    bound into the training plan by digest, and the trainer had no file to read it from —
    so ``Trainer`` was built with no ``eval_dataset`` and the split contributed nothing
    to any measurement.

    **These rows never contribute a gradient.** They are the trainer's evaluation arm,
    and evaluation is teacher-forced loss over the same assistant-only mask the training
    rows use. Nothing here makes them held-out evidence: held-out eligibility material is
    ``m62-defensive-eval``, it is ``evaluation_only``, and three authorities refuse it a
    train-side split.
    """
    return _export_split(
        split=SFT_VALIDATION_SOURCE_SPLIT, root=root, dataset_id=dataset_id,
        dataset_version=dataset_version, revocation=revocation,
        created_at_utc=created_at_utc, out_root=out_root)


def _export_split(*, split: DatasetSplit, root: str | Path, dataset_id: str,
                  dataset_version: str, revocation: RevocationSnapshot,
                  created_at_utc: str,
                  out_root: str | Path | None = None) -> SFTExport:
    """Write one train-side split's export for one verified dataset version.

    Refuses rather than producing an empty file when nothing is eligible: a zero-record
    export is not a small dataset, it is the absence of one, and it would sail through
    every downstream integrity check on the way to a training run that learned nothing.

    ``split`` is not reachable from a command line, a config field or a manifest: the two
    public wrappers above are the only callers, and a split outside
    :data:`EXPORTABLE_SPLITS` is refused here before a directory is read.
    """
    names = EXPORTABLE_SPLITS.get(split)
    if names is None:
        raise ExportError(
            f"export: {getattr(split, 'value', split)!r} is not an exportable split; "
            f"only {sorted(s.value for s in EXPORTABLE_SPLITS)} may be written, because "
            f"every other split exists to measure the model rather than to fit or steer "
            f"it")
    data_name, manifest_name = names

    manifest = load_manifest(root=root, dataset_id=dataset_id,
                             dataset_version=dataset_version)
    shard = manifest.shard_for(split)
    if shard is None:
        raise ExportError(
            f"export: dataset {dataset_id}/{dataset_version} has no {split.value} "
            f"shard; there is nothing to export and an empty export would not say so")

    directory = version_dir(root, dataset_id, dataset_version)
    records = read_shard(directory / shard_filename(split))
    by_id = {row.candidate_id: row for row in manifest.candidates}

    rows: list[dict] = []
    kept: list[DatasetCandidate] = []
    excluded: list[str] = []
    for candidate in sorted(records, key=lambda c: c.candidate_id):
        row_meta = by_id.get(candidate.candidate_id)
        if row_meta is None:  # pragma: no cover — verify_version already enforces this
            raise ExportError(f"export: {candidate.candidate_id} is in the shard but not "
                              f"in the manifest")
        reason = _exclusion_reason(candidate, row_meta.split, revocation,
                                   source_split=split)
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
            f"export: dataset {dataset_id}/{dataset_version} yielded no eligible "
            f"{split.value} records (excluded: {_distribution(excluded)}). Refusing to "
            f"write an empty export rather than producing a file that verifies and "
            f"teaches nothing")

    text = "".join(f"{canonical_json(row)}\n" for row in rows)
    destination = export_dir(out_root if out_root is not None else root, dataset_id,
                             dataset_version)
    path = destination / data_name
    manifest_path = destination / manifest_name
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
        source_manifest_hash=shard.sha256_file, filename=data_name,
        source_split=split.value,
        sha256_file=sha256_file(path), size_bytes=path.stat().st_size,
        record_count=len(rows), created_at_utc=created_at_utc,
        system_prompt_versions=tuple(c.system_prompt_version for c in kept),
        task_family_distribution=_distribution([c.task_family.value for c in kept]),
        target_source_distribution=_distribution([c.target_source.value for c in kept]),
        source_model_distribution=_distribution([c.student_model_id for c in kept]),
        excluded_counts=_distribution(excluded),
        row_hashes_hash=sha256_obj([row_hash(r) for r in rows]))
    atomic_write_text(manifest_path, canonical_json(export_manifest.to_record()))

    verified = _verify_export(split=split,
                              out_root=out_root if out_root is not None else root,
                              dataset_id=dataset_id, dataset_version=dataset_version)
    if verified.problems:
        raise ExportError(f"export: written but immediately unverifiable "
                          f"({list(verified.problems)})")
    return SFTExport(manifest=export_manifest, relative_paths=(
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{data_name}",
        f"{EXPORT_DIR}/{dataset_id}/{dataset_version}/{manifest_name}"))


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
    """Re-hash a written TRAINING export against its manifest. Reports, never raises."""
    return _verify_export(split=SFT_SOURCE_SPLIT, out_root=out_root,
                          dataset_id=dataset_id, dataset_version=dataset_version)


def verify_sft_validation_export(*, out_root: str | Path, dataset_id: str,
                                 dataset_version: str) -> ExportVerification:
    """Re-hash a written VALIDATION export against its manifest. Reports, never raises.

    Separate from :func:`verify_sft_export` rather than a parameter on it, so that a
    caller asking about the training corpus can never be answered about the validation
    corpus — including the caller inside ``_export_split`` that decides whether what it
    just wrote is usable.
    """
    return _verify_export(split=SFT_VALIDATION_SOURCE_SPLIT, out_root=out_root,
                          dataset_id=dataset_id, dataset_version=dataset_version)


def _verify_export(*, split: DatasetSplit, out_root: str | Path, dataset_id: str,
                   dataset_version: str) -> ExportVerification:
    """Re-hash one written export against its manifest. Reports rather than raises."""
    names = EXPORTABLE_SPLITS.get(split)
    if names is None:  # pragma: no cover — unreachable from the two public wrappers
        return ExportVerification(
            dataset_id=str(dataset_id), dataset_version=str(dataset_version),
            problems=(f"{getattr(split, 'value', split)!r} is not an exportable split",))
    try:
        directory = export_dir(out_root, dataset_id, dataset_version)
    except SchemaError as exc:
        return ExportVerification(dataset_id=str(dataset_id),
                                  dataset_version=str(dataset_version),
                                  problems=(str(exc),))
    manifest_path = directory / names[1]
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
    if manifest.source_split != split.value:
        # The manifest cross-binds its own (source_split, filename) pair, so this can
        # only fire when a manifest for one split was written at the OTHER split's
        # manifest path. Answering the caller's question about the wrong corpus is worse
        # than answering it with a problem.
        problems.append(
            f"{names[1]}: describes the {manifest.source_split} split, not "
            f"{split.value}")
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
    "EXPORTABLE_SPLITS", "EXPORT_DIR", "EXPORT_VERSION", "SFT_FILENAME",
    "SFT_MANIFEST_FILENAME", "SFT_SOURCE_SPLIT", "SFT_VALIDATION_FILENAME",
    "SFT_VALIDATION_MANIFEST_FILENAME", "SFT_VALIDATION_SOURCE_SPLIT", "ExportError",
    "ExportVerification", "ManifestError", "SFTExport", "SFTExportManifest",
    "export_dir", "export_sft", "export_sft_validation", "row_hash", "sft_row",
    "verify_sft_export", "verify_sft_validation_export",
]
