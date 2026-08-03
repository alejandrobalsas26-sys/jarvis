"""training_gym/evaluation/artifacts.py — V69 M62 S3C: the files, and proving they are the files.

WHAT IS WRITTEN
---------------
A fixed allowlist of nine filenames. Anything else in the generation directory is a
refusal rather than a curiosity: an evaluation output tree is reviewed, and a file nobody
expected is either a mistake or something that should not be there.

WHY THE MANIFEST IS WRITTEN LAST
--------------------------------
Same reason S3B writes the adapter manifest last: every digest it carries is taken from
the bytes already on disk, so a manifest that exists at all describes a complete tree.
A crash halfway through leaves a directory with no manifest, which is unambiguously
incomplete rather than plausibly finished.

WHAT THE VERIFIER RE-DERIVES
----------------------------
Everything. File digests, byte sizes, line counts and the manifest's own hash are all
recomputed from disk. A verifier that trusted any recorded number would prove the
manifest is well-formed rather than that the tree still matches it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..atomicio import AtomicIOError, atomic_write_text, count_lines
from ..schemas import (
    SchemaError,
    assert_no_private_content,
    canonical_json,
    scan_private_content,
    sha256_file,
    sha256_obj,
    short,
)
from .plan import EXPECTED_EVALUATION_FILES
from .policy import ResourceCeilings

#: Bumped when the manifest's shape changes.
EVALUATION_MANIFEST_VERSION = "m62.evaluation_manifest.1"

#: The manifest cannot cover itself, so it is excluded from its own digest list.
EVALUATION_MANIFEST_FILE = "evaluation-manifest.json"

#: Files a completed generation must contain.
REQUIRED_EVALUATION_FILES: frozenset[str] = frozenset({
    "evaluation-plan.json", "task-pack.jsonl", "task-pack-manifest.json",
    "baseline-results.jsonl", "candidate-results.jsonl", "paired-comparisons.jsonl",
    "metrics.json", "evaluation-report.json", EVALUATION_MANIFEST_FILE})

#: Files that may be present.
OPTIONAL_EVALUATION_FILES: frozenset[str] = frozenset()

#: Everything permitted in a generation directory.
ALLOWED_EVALUATION_FILES: frozenset[str] = (
    REQUIRED_EVALUATION_FILES | OPTIONAL_EVALUATION_FILES)

#: Files whose records are one per line, so a line count is a meaningful check.
JSONL_FILES: frozenset[str] = frozenset({
    "task-pack.jsonl", "baseline-results.jsonl", "candidate-results.jsonl",
    "paired-comparisons.jsonl"})


class EvaluationArtifactError(SchemaError):
    """An artefact tree that will not be written, or will not be believed."""


class ArtifactVerdict(str, Enum):
    VALID = "valid"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ArtifactFile:
    """One file, by digest rather than by trust."""

    name: str
    sha256: str
    size_bytes: int
    line_count: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "line_count": self.line_count}


@dataclass(frozen=True)
class ArtifactValidation:
    """What a directory actually contains, and why it may not be trusted."""

    verdict: ArtifactVerdict
    files: tuple[ArtifactFile, ...] = ()
    problems: tuple[str, ...] = ()
    total_bytes: int = 0
    tree_hash: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is ArtifactVerdict.VALID and not self.problems

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value,
                "files": [f.to_dict() for f in self.files],
                "problems": list(self.problems), "total_bytes": self.total_bytes,
                "tree_hash": self.tree_hash}


def _entry_problem(path: Path, name: str, ceilings: ResourceCeilings) -> str | None:
    """Why this directory entry may not be part of a verified tree.

    Links are refused rather than followed: a link planted in an output directory would
    make the file that is reviewed and the file that is hashed two different files, and
    could pull the operator's key material into a digest.
    """
    if path.is_symlink():
        return (f"{name}: is a symlink; the file that is reviewed and the file that is "
                f"hashed must be the same file")
    if not path.exists():
        return f"{name}: is missing"
    if not path.is_file():
        return f"{name}: is not a regular file (a directory, device, socket or FIFO)"
    try:
        stat = path.stat()
    except OSError as exc:
        return f"{name}: is unreadable ({exc.strerror})"
    if getattr(stat, "st_nlink", 1) > 1:
        return (f"{name}: has more than one hard link; a second name for an evaluation "
                f"artefact is a second way to change it")
    if stat.st_size == 0:
        return f"{name}: is empty"
    if stat.st_size > ceilings.max_artifact_bytes:
        return (f"{name}: {stat.st_size} bytes exceeds the "
                f"{ceilings.max_artifact_bytes} ceiling")
    if name not in ALLOWED_EVALUATION_FILES:
        return (f"{name}: is not an artefact this subsystem writes; an unexpected file "
                f"in a reviewed output tree is a refusal, not a curiosity")
    return None


def evaluation_tree_hash(files: Sequence[ArtifactFile]) -> str:
    """Digest over the validated ``(name, digest, size, lines)`` list.

    Excludes the manifest, which cannot cover itself. Computed from the VALIDATED list
    rather than by walking the directory again, so the digest describes exactly what was
    checked.
    """
    return sha256_obj([f.to_dict() for f in
                       sorted((f for f in files if f.name != EVALUATION_MANIFEST_FILE),
                              key=lambda f: f.name)])


def validate_evaluation_directory(directory: str | Path, *,
                                  expect_manifest: bool = False,
                                  ceilings: ResourceCeilings | None = None
                                  ) -> ArtifactValidation:
    """Everything that must hold about a generation directory, re-derived from disk."""
    limits = ceilings or ResourceCeilings()
    path = Path(directory)
    problems: list[str] = []
    if path.is_symlink():
        return ArtifactValidation(
            verdict=ArtifactVerdict.REJECTED,
            problems=("the generation directory is a symlink",))
    if not path.is_dir():
        return ArtifactValidation(
            verdict=ArtifactVerdict.REJECTED,
            problems=(f"{path.name!r} is not a directory",))

    present = sorted(entry.name for entry in path.iterdir())
    if len(present) > limits.max_artifact_files:
        problems.append(f"{len(present)} entries exceeds the "
                        f"{limits.max_artifact_files} ceiling")

    required = set(REQUIRED_EVALUATION_FILES)
    if not expect_manifest:
        required.discard(EVALUATION_MANIFEST_FILE)
    for missing in sorted(required - set(present)):
        problems.append(f"{missing}: is missing")

    files: list[ArtifactFile] = []
    total = 0
    for name in present:
        entry = path / name
        problem = _entry_problem(entry, name, limits)
        if problem:
            problems.append(problem)
            continue
        if not expect_manifest and name == EVALUATION_MANIFEST_FILE:
            continue
        size = entry.stat().st_size
        total += size
        files.append(ArtifactFile(
            name=name, sha256=sha256_file(entry), size_bytes=size,
            line_count=count_lines(entry) if name in JSONL_FILES else 0))

    verdict = ArtifactVerdict.REJECTED if problems else ArtifactVerdict.VALID
    return ArtifactValidation(
        verdict=verdict, files=tuple(sorted(files, key=lambda f: f.name)),
        problems=tuple(problems), total_bytes=total,
        tree_hash=evaluation_tree_hash(files) if not problems else "")


# ══════════════════════════════════════════════════════════════════════════════
#  The manifest
# ══════════════════════════════════════════════════════════════════════════════
_MANIFEST_FIELDS: tuple[str, ...] = (
    "manifest_version", "evaluation_id", "generation", "plan_hash", "report_hash",
    "task_pack_hash", "hidden_target_store_hash", "baseline_reference_hash",
    "candidate_adapter_reference_hash", "comparison_manifest_hash", "backend_ids",
    "empirical_status", "eligibility", "task_count", "measured_pairs", "files",
    "total_bytes", "tree_hash", "created_at_utc",
)


@dataclass(frozen=True)
class EvaluationManifest:
    """What one generation produced, by digest. Written last, verified on reload."""

    evaluation_id: str
    generation: int
    plan_hash: str
    report_hash: str
    task_pack_hash: str
    hidden_target_store_hash: str
    baseline_reference_hash: str
    candidate_adapter_reference_hash: str
    comparison_manifest_hash: str
    backend_ids: tuple[str, ...]
    empirical_status: str
    eligibility: str
    task_count: int
    measured_pairs: int
    files: tuple[ArtifactFile, ...]
    total_bytes: int
    tree_hash: str
    created_at_utc: str
    manifest_version: str = EVALUATION_MANIFEST_VERSION

    def to_dict(self) -> dict:
        return {
            "manifest_version": self.manifest_version,
            "evaluation_id": self.evaluation_id, "generation": self.generation,
            "plan_hash": self.plan_hash, "report_hash": self.report_hash,
            "task_pack_hash": self.task_pack_hash,
            "hidden_target_store_hash": self.hidden_target_store_hash,
            "baseline_reference_hash": self.baseline_reference_hash,
            "candidate_adapter_reference_hash": self.candidate_adapter_reference_hash,
            "comparison_manifest_hash": self.comparison_manifest_hash,
            "backend_ids": sorted(self.backend_ids),
            "empirical_status": self.empirical_status,
            "eligibility": self.eligibility,
            "task_count": self.task_count, "measured_pairs": self.measured_pairs,
            "files": [f.to_dict() for f in sorted(self.files, key=lambda f: f.name)],
            "total_bytes": self.total_bytes, "tree_hash": self.tree_hash,
            "created_at_utc": self.created_at_utc,
        }

    def manifest_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "manifest_hash": self.manifest_hash()}
        assert_no_private_content(record, label="evaluation manifest")
        return record

    @classmethod
    def from_dict(cls, payload: object) -> "EvaluationManifest":
        from ..schemas import reject_unknown_fields, require_mapping
        data = dict(require_mapping(payload, "evaluation manifest"))
        stored = str(data.pop("manifest_hash", "")).strip().lower()
        reject_unknown_fields(data, _MANIFEST_FIELDS, label="evaluation manifest")
        manifest = cls(
            evaluation_id=data["evaluation_id"], generation=int(data["generation"]),
            plan_hash=data["plan_hash"], report_hash=data["report_hash"],
            task_pack_hash=data["task_pack_hash"],
            hidden_target_store_hash=data["hidden_target_store_hash"],
            baseline_reference_hash=data["baseline_reference_hash"],
            candidate_adapter_reference_hash=data["candidate_adapter_reference_hash"],
            comparison_manifest_hash=data["comparison_manifest_hash"],
            backend_ids=tuple(data.get("backend_ids", ())),
            empirical_status=str(data["empirical_status"]),
            eligibility=str(data["eligibility"]),
            task_count=int(data["task_count"]),
            measured_pairs=int(data["measured_pairs"]),
            files=tuple(ArtifactFile(name=f["name"], sha256=f["sha256"],
                                     size_bytes=int(f["size_bytes"]),
                                     line_count=int(f.get("line_count", 0)))
                        for f in data.get("files", ())),
            total_bytes=int(data["total_bytes"]), tree_hash=str(data["tree_hash"]),
            created_at_utc=str(data["created_at_utc"]),
            manifest_version=str(data.get("manifest_version",
                                          EVALUATION_MANIFEST_VERSION)))
        if stored and stored != manifest.manifest_hash():
            raise EvaluationArtifactError(
                f"evaluation manifest: the stored digest {short(stored)} does not match "
                f"the document ({short(manifest.manifest_hash())})")
        return manifest


# ══════════════════════════════════════════════════════════════════════════════
#  Writing
# ══════════════════════════════════════════════════════════════════════════════
def write_jsonl(path: Path, records: Sequence[Mapping]) -> None:
    """Deterministic JSONL: one canonical record per line, written atomically."""
    body = "\n".join(canonical_json(dict(record)) for record in records)
    _write(path, body + ("\n" if body else ""))


def write_json(path: Path, payload: Mapping) -> None:
    _write(path, canonical_json(dict(payload)) + "\n")


def _write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise EvaluationArtifactError(
            f"{path.name}: is a symlink; refusing to write through it")
    found = scan_private_content(text)
    if found and found != ("scanner_unavailable",):
        raise EvaluationArtifactError(
            f"{path.name}: refusing to write — private content present "
            f"({', '.join(found)})")
    try:
        atomic_write_text(path, text)
    except AtomicIOError as exc:
        raise EvaluationArtifactError(f"{path.name}: {exc}") from None


def write_evaluation_artifacts(directory: str | Path, *, plan_record: Mapping,
                               task_pack_records: Sequence[Mapping],
                               task_pack_manifest: Mapping,
                               baseline_records: Sequence[Mapping],
                               candidate_records: Sequence[Mapping],
                               comparison_records: Sequence[Mapping],
                               metrics_record: Mapping,
                               report_record: Mapping,
                               manifest: EvaluationManifest,
                               ceilings: ResourceCeilings | None = None
                               ) -> ArtifactValidation:
    """Write the tree, then verify it from disk. The manifest goes last.

    Post-write verification is not belt-and-braces: an atomic write can succeed and
    still leave a file a later reader rejects — a truncating disk, a filter driver, a
    sync tool. Reading it back is the only thing that proves what is there.
    """
    path = Path(directory)
    if path.is_symlink():
        raise EvaluationArtifactError("the generation directory is a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if (path / EVALUATION_MANIFEST_FILE).exists():
        raise EvaluationArtifactError(
            f"a manifest already exists in {path.name!r}; a completed generation is "
            f"never overwritten, because two runs under one directory produce one set "
            f"of artefacts and no way to tell which run made them")

    write_json(path / "evaluation-plan.json", plan_record)
    write_jsonl(path / "task-pack.jsonl", task_pack_records)
    write_json(path / "task-pack-manifest.json", task_pack_manifest)
    write_jsonl(path / "baseline-results.jsonl", baseline_records)
    write_jsonl(path / "candidate-results.jsonl", candidate_records)
    write_jsonl(path / "paired-comparisons.jsonl", comparison_records)
    write_json(path / "metrics.json", metrics_record)
    write_json(path / "evaluation-report.json", report_record)

    interim = validate_evaluation_directory(path, expect_manifest=False,
                                            ceilings=ceilings)
    if not interim.ok:
        raise EvaluationArtifactError(
            f"the artefacts did not verify after writing: "
            f"{'; '.join(interim.problems[:5])}")

    sealed = EvaluationManifest(**{**manifest.__dict__, "files": interim.files,
                                   "total_bytes": interim.total_bytes,
                                   "tree_hash": interim.tree_hash})
    write_json(path / EVALUATION_MANIFEST_FILE, sealed.to_record())

    final = validate_evaluation_directory(path, expect_manifest=True, ceilings=ceilings)
    if not final.ok:
        raise EvaluationArtifactError(
            f"the sealed tree did not verify: {'; '.join(final.problems[:5])}")
    return final


def verify_evaluation_generation(directory: str | Path, *,
                                 ceilings: ResourceCeilings | None = None
                                 ) -> tuple[str, ...]:
    """Re-derive every claim in a generation directory. Empty tuple = fully verified."""
    path = Path(directory)
    validation = validate_evaluation_directory(path, expect_manifest=True,
                                               ceilings=ceilings)
    if not validation.ok:
        return validation.problems

    manifest_path = path / EVALUATION_MANIFEST_FILE
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return (f"{EVALUATION_MANIFEST_FILE}: unreadable ({exc})",)
    try:
        manifest = EvaluationManifest.from_dict(payload)
    except SchemaError as exc:
        return (str(exc),)

    problems: list[str] = []
    on_disk = {f.name: f for f in validation.files if f.name != EVALUATION_MANIFEST_FILE}
    recorded = {f.name: f for f in manifest.files}
    for missing in sorted(set(recorded) - set(on_disk)):
        problems.append(f"{missing}: named by the manifest and absent from the tree")
    for extra in sorted(set(on_disk) - set(recorded)):
        problems.append(f"{extra}: present in the tree and not named by the manifest")
    for name in sorted(set(recorded) & set(on_disk)):
        want, have = recorded[name], on_disk[name]
        if want.sha256 != have.sha256:
            problems.append(
                f"{name}: digest {short(have.sha256)} does not match the manifest's "
                f"{short(want.sha256)}; the file has changed since it was sealed")
        if want.size_bytes != have.size_bytes:
            problems.append(
                f"{name}: {have.size_bytes} bytes against the manifest's "
                f"{want.size_bytes}")
        if want.line_count != have.line_count:
            problems.append(
                f"{name}: {have.line_count} lines against the manifest's "
                f"{want.line_count}")
    if not problems and manifest.tree_hash != validation.tree_hash:
        problems.append(
            f"the tree digest {short(validation.tree_hash)} does not match the "
            f"manifest's {short(manifest.tree_hash)}")

    report_path = path / "evaluation-report.json"
    if report_path.is_file():
        try:
            from .reports import verify_report_payload
            report = verify_report_payload(
                json.loads(report_path.read_text(encoding="utf-8")))
            if report["report_hash"] != manifest.report_hash:
                problems.append(
                    "the report's digest does not match the one the manifest sealed")
        except (SchemaError, OSError, ValueError) as exc:
            problems.append(f"evaluation-report.json: {exc}")
    return tuple(problems)


__all__ = [
    "ALLOWED_EVALUATION_FILES", "EVALUATION_MANIFEST_FILE",
    "EVALUATION_MANIFEST_VERSION", "EXPECTED_EVALUATION_FILES", "JSONL_FILES",
    "OPTIONAL_EVALUATION_FILES", "REQUIRED_EVALUATION_FILES", "ArtifactFile",
    "ArtifactValidation", "ArtifactVerdict", "EvaluationArtifactError",
    "EvaluationManifest", "evaluation_tree_hash", "validate_evaluation_directory",
    "verify_evaluation_generation", "write_evaluation_artifacts", "write_json",
    "write_jsonl",
]
