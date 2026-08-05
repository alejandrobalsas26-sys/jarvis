"""training_gym/training/artifacts.py — V69 M62 S3B: what an adapter is allowed to be.

WHY AN ALLOWLIST AND NOT A DENYLIST
-----------------------------------
A training framework writes what it likes into its output directory: model cards,
optimizer state, tokenizer dumps, checkpoint trees, and — if serialization falls back —
a pickle. A denylist would need to anticipate every one of those. The allowlist names the
five files an adapter may consist of, and everything else is a refusal, so a framework
upgrade that starts writing something new fails loudly instead of shipping it.

WHY THE MANIFEST IS THE COMMIT POINT
------------------------------------
``adapter-manifest.json`` is written LAST, after every other file has been re-hashed from
disk. A run directory without a verifiable manifest is residue, not a run. This is the
same rule :func:`~training_gym.datasets.manifests.write_dataset_version` is written under,
and it is what makes an interrupt safe: there is no moment at which a partial
``adapter_model.safetensors`` is readable as a finished adapter, because the file that
says "finished" does not exist until everything else is already correct.

WHY THE TREE DIGEST IS OVER THE FILE LIST, NOT OVER THE DIRECTORY
-----------------------------------------------------------------
:func:`~training_gym.schemas.sha256_tree` walks a directory and SKIPS symlinks, so that a
link planted in an adapter directory cannot pull the operator's key material into the
digest. That safety property is also a blind spot — a planted link is invisible to it —
and it has a second problem here: the manifest lives in the directory it would describe,
so a directory digest computed before the manifest exists can never be re-derived after
it does. A hash that cannot cover itself cannot cover the tree it lives in.

So the adapter digest is taken over the explicit, validated file list instead:
``(name, sha256, size)`` for every entry, each one already proven to be a real, unlinked,
regular file on the allowlist. It re-derives exactly, it is order-independent, and a
planted link is a refusal rather than an omission.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    assert_no_private_content,
    require_id,
    require_int,
    sha256_file,
    sha256_obj,
    short,
)
from ..task_spec import require_timestamp
from .plan import TrainingPlanError

#: Bumped when the manifest's shape changes.
ADAPTER_MANIFEST_VERSION = "m62.adapter_manifest.1"

#: The manifest's own filename. Its presence in a run directory is what "finished" means.
ADAPTER_MANIFEST_FILE = "adapter-manifest.json"

#: Written by PEFT. Both are mandatory: a config without weights describes an adapter
#: that does not exist, and weights without a config cannot be loaded onto a base model.
ADAPTER_CONFIG_FILE = "adapter_config.json"
ADAPTER_WEIGHTS_FILE = "adapter_model.safetensors"

#: The complete set of names an adapter directory may contain. Anything else is refused.
REQUIRED_ADAPTER_FILES: frozenset[str] = frozenset({
    ADAPTER_CONFIG_FILE, ADAPTER_WEIGHTS_FILE, ADAPTER_MANIFEST_FILE})

#: ``README.md`` is not optional in the sense of being a choice: PEFT's
#: ``save_pretrained`` calls ``create_or_update_model_card`` unconditionally, so every
#: adapter this backend can write has one. It is the stock model-card template -- the
#: base model id, the library name and placeholder prose -- and the manifest hashes it
#: like any other file, so admitting it by name is narrower than the alternative of
#: deleting a file the library insists on writing.
OPTIONAL_ADAPTER_FILES: frozenset[str] = frozenset({
    "training_args.json", "run.json", "training_log.jsonl", "backend_result.json",
    "tokenizer_config.json", "special_tokens_map.json", "README.md"})

#: The LoRA target sentinel. PEFT resolves it against the loaded model and records the
#: concrete module names it actually adapted, so the saved config never echoes the word
#: back. Approving the sentinel approves whatever it resolved to -- but not nothing.
ALL_LINEAR_SENTINEL = "all-linear"

ALLOWED_ADAPTER_FILES: frozenset[str] = REQUIRED_ADAPTER_FILES | OPTIONAL_ADAPTER_FILES

#: A LoRA adapter for a 0.6B model is single-digit megabytes. The ceiling exists so a
#: backend that accidentally saved the base model is refused rather than archived.
MAX_ADAPTER_FILE_BYTES = 512 * 1024 * 1024
MAX_ADAPTER_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ADAPTER_FILES = 32

#: Extensions that are never an adapter, whatever they are called.
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset({
    ".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib", ".npy", ".npz",
    ".py", ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bat", ".cmd",
    ".ps1", ".sh", ".zip", ".tar", ".gz", ".7z", ".rar", ".msi", ".scr"})

#: The safetensors header is a little-endian u64 length followed by that many bytes of
#: JSON. Reading it is not "parsing safetensors" — it is checking that the file claims to
#: be one, without importing a library that could execute anything.
_SAFETENSORS_HEADER_BYTES = 8
MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024


class AdapterArtifactError(TrainingPlanError):
    """An artifact that will not be recorded as a completed adapter."""


class ArtifactVerdict(str, Enum):
    """Whether the bytes on disk are an adapter this milestone will vouch for."""

    VALID = "valid"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ArtifactFile:
    """One file in a run directory, named relatively and hashed from disk."""

    name: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {"name": self.name, "sha256": self.sha256,
                "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class ArtifactValidation:
    """Every reason the directory is or is not a completed adapter."""

    verdict: ArtifactVerdict
    files: tuple[ArtifactFile, ...] = ()
    problems: tuple[str, ...] = ()
    tensor_count: int = 0
    total_bytes: int = 0
    tree_hash: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is ArtifactVerdict.VALID

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "ok": self.ok,
                "files": [f.to_dict() for f in self.files],
                "problems": list(self.problems), "tensor_count": self.tensor_count,
                "total_bytes": self.total_bytes, "tree_hash": self.tree_hash}


def _entry_problem(path: Path, name: str) -> str | None:
    """Why this directory entry may not be part of an adapter, or None."""
    if path.is_symlink():
        return (f"{name}: is a symlink; a link inside an adapter directory is invisible "
                f"to the tree digest and may point anywhere on the host")
    if path.is_dir():
        return (f"{name}: is a directory; an adapter is a flat set of files, and a "
                f"nested tree is where a checkpoint or a cached model hides")
    if not path.is_file():
        return (f"{name}: is not a regular file (socket, FIFO or device); nothing that "
                f"is not bytes on disk belongs in an artifact set")
    try:
        stat = path.stat()
    except OSError as exc:  # pragma: no cover - unreadable entry
        return f"{name}: cannot be inspected ({type(exc).__name__})"
    if getattr(stat, "st_nlink", 1) > 1:
        return (f"{name}: has more than one hard link; a second name for an adapter file "
                f"is a second way to change it behind its own recorded digest")
    if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES:
        return (f"{name}: has a forbidden extension; pickles, archives, scripts and "
                f"shared objects are never adapter weights, whatever they are named")
    if name not in ALLOWED_ADAPTER_FILES:
        return (f"{name}: is not in the adapter allowlist "
                f"({sorted(ALLOWED_ADAPTER_FILES)})")
    if stat.st_size <= 0:
        return f"{name}: is empty; a zero-byte artifact records that nothing was written"
    if stat.st_size > MAX_ADAPTER_FILE_BYTES:
        return (f"{name}: is {stat.st_size} bytes, above the "
                f"{MAX_ADAPTER_FILE_BYTES}-byte ceiling; an adapter this large is a "
                f"base model that was saved by mistake")
    return None


def read_safetensors_header(path: Path) -> dict:
    """The tensor index, read without importing anything that could execute.

    ``safetensors`` is a length-prefixed JSON header followed by raw tensor bytes. Parsing
    the header with :mod:`json` proves the file is structurally what it claims to be and
    names the tensors it holds — which is all validation needs, and is strictly less
    capability than handing the path to a deserializer.
    """
    with path.open("rb") as handle:
        raw_length = handle.read(_SAFETENSORS_HEADER_BYTES)
        if len(raw_length) != _SAFETENSORS_HEADER_BYTES:
            raise AdapterArtifactError(
                f"{path.name}: shorter than a safetensors header; this is a truncated "
                f"write, not an adapter")
        length = int.from_bytes(raw_length, "little")
        if length <= 0 or length > MAX_SAFETENSORS_HEADER_BYTES:
            raise AdapterArtifactError(
                f"{path.name}: declares a {length}-byte header, which is not a plausible "
                f"safetensors index")
        payload = handle.read(length)
        if len(payload) != length:
            raise AdapterArtifactError(
                f"{path.name}: header is truncated; the file stops inside its own index")
    try:
        header = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AdapterArtifactError(
            f"{path.name}: the safetensors header is not valid JSON ({exc})") from None
    if not isinstance(header, dict):
        raise AdapterArtifactError(f"{path.name}: the safetensors header is not an object")
    return header


def _tensor_names(header: dict) -> tuple[str, ...]:
    """Every tensor in the index, excluding the reserved metadata key."""
    return tuple(sorted(k for k in header if k != "__metadata__"))


def validate_adapter_directory(directory: str | Path, *, lora: dict,
                               base_model_id: str,
                               expect_manifest: bool = False) -> ArtifactValidation:
    """Decide whether these bytes are an adapter. Reports rather than raises.

    *expect_manifest* is False while a run is finalizing — the manifest does not exist
    yet, because it is the thing being decided — and True when re-verifying a directory
    that already claims to be complete.
    """
    path = Path(directory)
    problems: list[str] = []
    if not path.is_dir() or path.is_symlink():
        return ArtifactValidation(
            verdict=ArtifactVerdict.REJECTED,
            problems=("the run directory is missing or is a link",))

    entries = sorted(path.iterdir(), key=lambda p: p.name)
    if len(entries) > MAX_ADAPTER_FILES:
        problems.append(f"the directory holds {len(entries)} entries, above the "
                        f"{MAX_ADAPTER_FILES} ceiling")
    files: list[ArtifactFile] = []
    total = 0
    for entry in entries:
        problem = _entry_problem(entry, entry.name)
        if problem:
            problems.append(problem)
            continue
        if entry.name == ADAPTER_MANIFEST_FILE and not expect_manifest:
            problems.append(
                f"{ADAPTER_MANIFEST_FILE}: already exists before validation; the manifest "
                f"is written last and its presence is what completion means")
            continue
        size = entry.stat().st_size
        total += size
        files.append(ArtifactFile(name=entry.name, sha256=sha256_file(entry),
                                  size_bytes=size))

    present = {f.name for f in files}
    required = REQUIRED_ADAPTER_FILES if expect_manifest else (
        REQUIRED_ADAPTER_FILES - {ADAPTER_MANIFEST_FILE})
    for missing in sorted(required - present):
        problems.append(f"{missing}: required and absent")
    if total > MAX_ADAPTER_TOTAL_BYTES:
        problems.append(f"the adapter totals {total} bytes, above the "
                        f"{MAX_ADAPTER_TOTAL_BYTES}-byte ceiling")

    tensor_count = 0
    if ADAPTER_WEIGHTS_FILE in present:
        try:
            header = read_safetensors_header(path / ADAPTER_WEIGHTS_FILE)
        except AdapterArtifactError as exc:
            problems.append(str(exc))
        else:
            names = _tensor_names(header)
            tensor_count = len(names)
            if not names:
                problems.append(
                    f"{ADAPTER_WEIGHTS_FILE}: holds no tensors; a run that saved an empty "
                    f"adapter learned nothing and must not be recorded as complete")
            problems.extend(_tensor_problems(names))
    if ADAPTER_CONFIG_FILE in present:
        problems.extend(_config_problems(path / ADAPTER_CONFIG_FILE, lora=lora,
                                         base_model_id=base_model_id))

    verdict = ArtifactVerdict.REJECTED if problems else ArtifactVerdict.VALID
    return ArtifactValidation(
        verdict=verdict, files=tuple(files), problems=tuple(problems),
        tensor_count=tensor_count, total_bytes=total,
        tree_hash=adapter_tree_hash(files) if verdict is ArtifactVerdict.VALID else "")


def adapter_tree_hash(files) -> str:
    """The one digest that covers the whole adapter, excluding the manifest itself."""
    return sha256_obj([f.to_dict() for f in sorted(files, key=lambda f: f.name)
                       if f.name != ADAPTER_MANIFEST_FILE])


#: Substrings that mark a tensor as belonging to the adapter rather than the base model.
_ADAPTER_TENSOR_MARKERS = ("lora_a", "lora_b", "lora_embedding", "lora_magnitude",
                           "modules_to_save")


def _tensor_problems(names: tuple[str, ...]) -> list[str]:
    """Refuse a file that is really a base-model dump wearing an adapter's filename."""
    foreign = [n for n in names
               if not any(marker in n.lower() for marker in _ADAPTER_TENSOR_MARKERS)]
    if foreign:
        return [f"{ADAPTER_WEIGHTS_FILE}: {len(foreign)} tensor(s) are not LoRA "
                f"parameters (for example {short(foreign[0], 40)!r}); an adapter that "
                f"carries base-model weights is a full fine-tune in an adapter's clothing"]
    return []


def _config_problems(path: Path, *, lora: dict, base_model_id: str) -> list[str]:
    """Bind the saved adapter config to the plan that authorised it."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError) as exc:
        return [f"{ADAPTER_CONFIG_FILE}: is not readable strict JSON ({exc})"]
    if not isinstance(payload, dict):
        return [f"{ADAPTER_CONFIG_FILE}: is not a JSON object"]

    problems: list[str] = []
    peft_type = str(payload.get("peft_type", "")).upper()
    if peft_type != "LORA":
        problems.append(
            f"{ADAPTER_CONFIG_FILE}: peft_type is {peft_type or 'absent'!r}, not LORA; "
            f"this milestone vouches for LoRA adapters and nothing else")
    saved_base = str(payload.get("base_model_name_or_path", ""))
    if saved_base and saved_base != base_model_id:
        problems.append(
            f"{ADAPTER_CONFIG_FILE}: was fitted on {saved_base!r}, the plan names "
            f"{base_model_id!r}; an adapter is only meaningful against the exact weights "
            f"it was trained against")
    for key, expected in (("r", lora.get("rank")), ("lora_alpha", lora.get("alpha"))):
        if expected is None:
            continue
        actual = payload.get(key)
        if actual != expected:
            problems.append(
                f"{ADAPTER_CONFIG_FILE}: {key} is {actual!r}, the plan approved "
                f"{expected!r}")
    expected_modules = lora.get("target_modules")
    actual_modules = payload.get("target_modules")
    if expected_modules and actual_modules is not None:
        wanted = sorted(expected_modules) if not isinstance(
            expected_modules, str) else [expected_modules]
        got = sorted(actual_modules) if not isinstance(
            actual_modules, str) else [actual_modules]
        if wanted == [ALL_LINEAR_SENTINEL]:
            # Resolved, not echoed. An empty set is still refused: it would describe an
            # adapter that adapted nothing while the run reported that it had trained.
            if not got:
                problems.append(
                    f"{ADAPTER_CONFIG_FILE}: target_modules are empty; the plan approved "
                    f"{wanted}, which has to resolve to at least one real module")
        elif wanted != got:
            problems.append(
                f"{ADAPTER_CONFIG_FILE}: target_modules are {got}, the plan approved "
                f"{wanted}")
    if payload.get("task_type") not in (None, "CAUSAL_LM"):
        problems.append(f"{ADAPTER_CONFIG_FILE}: task_type is "
                        f"{payload.get('task_type')!r}, not CAUSAL_LM")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  The manifest
# ══════════════════════════════════════════════════════════════════════════════
_MANIFEST_FIELDS: tuple[str, ...] = (
    SCHEMA_KEY, "manifest_version", "run_id", "run_state", "plan_hash",
    "training_config_hash", "method", "backend_id", "backend_version",
    "base_model_id", "base_model_revision", "base_model_identity_hash",
    "tokenizer_id", "tokenizer_revision", "tokenizer_identity_hash",
    "tokenizer_chat_template_hash", "dataset_id", "dataset_version",
    "dataset_reference_hash", "dataset_manifest_hash", "train_shard_hash",
    "validation_shard_hash", "hidden_evaluation_hash", "security_regression_hash",
    "export_manifest_hash", "split_algorithm_version", "seed", "lora",
    "assistant_only_loss", "package_versions", "device_category", "precision",
    "requested_steps", "completed_steps", "epochs_completed", "train_loss",
    "eval_loss", "truncated_records", "duration_seconds", "files", "total_bytes",
    "tree_hash", "interrupted", "completed", "known_limitations", "created_at_utc",
    "manifest_hash",
)


@dataclass(frozen=True)
class AdapterManifest:
    """What an adapter is, what produced it, and what it may be compared against.

    Carries no username, hostname, home directory, output path, model cache location,
    credential, environment, GPU serial or hidden reasoning. The only path-shaped values
    are the relative artifact filenames, and :func:`assert_no_private_content` is run over
    the whole record before it is written.
    """

    run_id: str
    run_state: str
    plan_hash: str
    training_config_hash: str
    method: str
    backend_id: str
    backend_version: str
    base_model_id: str
    base_model_revision: str
    base_model_identity_hash: str
    tokenizer_id: str
    tokenizer_revision: str
    tokenizer_identity_hash: str
    tokenizer_chat_template_hash: str
    dataset_id: str
    dataset_version: str
    dataset_reference_hash: str
    dataset_manifest_hash: str
    train_shard_hash: str
    validation_shard_hash: str
    hidden_evaluation_hash: str
    security_regression_hash: str
    export_manifest_hash: str
    split_algorithm_version: str
    seed: int
    lora: dict
    assistant_only_loss: dict
    package_versions: dict
    device_category: str
    precision: str
    requested_steps: int
    completed_steps: int
    epochs_completed: float
    train_loss: float
    eval_loss: float
    truncated_records: int
    duration_seconds: float
    files: tuple[ArtifactFile, ...]
    total_bytes: int
    tree_hash: str
    created_at_utc: str
    interrupted: bool = False
    completed: bool = True
    known_limitations: tuple[str, ...] = ()
    manifest_version: str = ADAPTER_MANIFEST_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "run_id", require_id(self.run_id, "adapter.run_id"))
        setattr_(self, "created_at_utc",
                 require_timestamp(self.created_at_utc, "adapter.created_at_utc"))
        setattr_(self, "seed", require_int(self.seed, "adapter.seed",
                                           minimum=0, maximum=2**32 - 1))
        for name in ("requested_steps", "completed_steps", "truncated_records",
                     "total_bytes"):
            setattr_(self, name, require_int(getattr(self, name), f"adapter.{name}",
                                             minimum=0, maximum=1 << 62))
        if self.completed and self.interrupted:
            raise AdapterArtifactError(
                "adapter manifest: a run cannot be both completed and interrupted; one "
                "of those two words is describing something that did not happen")
        if self.completed and self.run_state != "completed":
            raise AdapterArtifactError(
                f"adapter manifest: completed=true with run_state "
                f"{self.run_state!r}; the flag and the state must agree, or the flag is "
                f"the only thing anyone reads")
        if self.completed and self.completed_steps <= 0:
            raise AdapterArtifactError(
                "adapter manifest: a completed run reports zero completed steps; a run "
                "that took no step produced an adapter identical to its initialisation")
        if self.completed_steps > self.requested_steps > 0:
            raise AdapterArtifactError(
                f"adapter manifest: {self.completed_steps} completed steps exceeds the "
                f"{self.requested_steps} the plan approved")
        for name in ("train_loss", "eval_loss", "epochs_completed", "duration_seconds"):
            value = float(getattr(self, name))
            if value != value or value in (float("inf"), float("-inf")):
                raise AdapterArtifactError(
                    f"adapter manifest: {name} is {value}; a run whose metrics are not "
                    f"finite diverged, and its adapter is not a result")
            setattr_(self, name, value)
        if self.duration_seconds < 0:
            raise AdapterArtifactError("adapter manifest: duration_seconds is negative")
        setattr_(self, "files", tuple(sorted(self.files, key=lambda f: f.name)))
        setattr_(self, "known_limitations", tuple(self.known_limitations))

    def to_dict(self) -> dict:
        """The canonical body. Excludes ``manifest_hash`` — that is computed over it."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "manifest_version": self.manifest_version,
            "run_id": self.run_id, "run_state": self.run_state,
            "plan_hash": self.plan_hash,
            "training_config_hash": self.training_config_hash,
            "method": self.method,
            "backend_id": self.backend_id, "backend_version": self.backend_version,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "base_model_identity_hash": self.base_model_identity_hash,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "tokenizer_identity_hash": self.tokenizer_identity_hash,
            "tokenizer_chat_template_hash": self.tokenizer_chat_template_hash,
            "dataset_id": self.dataset_id, "dataset_version": self.dataset_version,
            "dataset_reference_hash": self.dataset_reference_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "train_shard_hash": self.train_shard_hash,
            "validation_shard_hash": self.validation_shard_hash,
            "hidden_evaluation_hash": self.hidden_evaluation_hash,
            "security_regression_hash": self.security_regression_hash,
            "export_manifest_hash": self.export_manifest_hash,
            "split_algorithm_version": self.split_algorithm_version,
            "seed": self.seed,
            "lora": dict(sorted(self.lora.items())),
            "assistant_only_loss": dict(sorted(self.assistant_only_loss.items())),
            "package_versions": dict(sorted(self.package_versions.items())),
            "device_category": self.device_category, "precision": self.precision,
            "requested_steps": self.requested_steps,
            "completed_steps": self.completed_steps,
            "epochs_completed": round(self.epochs_completed, 6),
            "train_loss": round(self.train_loss, 6),
            "eval_loss": round(self.eval_loss, 6),
            "truncated_records": self.truncated_records,
            "duration_seconds": round(self.duration_seconds, 3),
            "files": [f.to_dict() for f in self.files],
            "total_bytes": self.total_bytes, "tree_hash": self.tree_hash,
            "interrupted": self.interrupted, "completed": self.completed,
            "known_limitations": list(self.known_limitations),
            "created_at_utc": self.created_at_utc,
        }

    def manifest_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def to_record(self) -> dict:
        record = {**self.to_dict(), "manifest_hash": self.manifest_hash()}
        assert_no_private_content(record, label="adapter manifest")
        return record

    @classmethod
    def from_dict(cls, payload: object) -> "AdapterManifest":
        """Reload and re-check the stored digest. A manifest that disagrees is refused."""
        if not isinstance(payload, dict):
            raise AdapterArtifactError("adapter manifest: expected an object")
        unknown = sorted(set(payload) - set(_MANIFEST_FIELDS))
        if unknown:
            raise AdapterArtifactError(
                f"adapter manifest: unknown field(s) {unknown}")
        declared = str(payload.get("manifest_hash", "")).strip().lower()
        if len(declared) != 64:
            raise AdapterArtifactError("adapter manifest: missing manifest_hash")
        body = {k: v for k, v in payload.items()
                if k not in (SCHEMA_KEY, "manifest_hash")}
        files = tuple(ArtifactFile(name=str(f.get("name", "")),
                                   sha256=str(f.get("sha256", "")),
                                   size_bytes=int(f.get("size_bytes", 0)))
                      for f in body.pop("files", []))
        manifest = cls(files=files, **body)  # type: ignore[arg-type]
        if manifest.manifest_hash() != declared:
            raise AdapterArtifactError(
                f"adapter manifest: the stored digest {short(declared)} does not match "
                f"its content {short(manifest.manifest_hash())}; the manifest was edited "
                f"after it was written")
        return manifest


def verify_completed_run(directory: str | Path, *, lora: dict,
                         base_model_id: str) -> tuple[str, ...]:
    """Re-derive every claim a finished run directory makes. Returns problems.

    An empty tuple means: the manifest parses, its own digest matches, every file it
    names is present and hashes to what it recorded, no file is present that it does not
    name, and the tree digest re-derives. Anything less is not a completed run.
    """
    path = Path(directory)
    manifest_path = path / ADAPTER_MANIFEST_FILE
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return ("the adapter manifest is missing; a run directory without one is "
                "residue, not a run",)
    try:
        manifest = AdapterManifest.from_dict(
            json.loads(manifest_path.read_text(encoding="utf-8")))
    except (AdapterArtifactError, SchemaError, json.JSONDecodeError, ValueError,
            OSError) as exc:
        return (f"the adapter manifest is unusable ({exc})",)

    problems: list[str] = []
    if not manifest.completed:
        problems.append("the manifest does not claim completion")
    recorded = {f.name: f for f in manifest.files}
    on_disk = {p.name for p in path.iterdir()} - {ADAPTER_MANIFEST_FILE}
    for extra in sorted(on_disk - set(recorded)):
        problems.append(f"{extra}: present but not named by the manifest")
    for name, entry in sorted(recorded.items()):
        target = path / name
        problem = _entry_problem(target, name)
        if problem:
            problems.append(problem)
            continue
        actual = sha256_file(target)
        if actual != entry.sha256:
            problems.append(f"{name}: hashes to {short(actual)}, the manifest recorded "
                            f"{short(entry.sha256)}")
        if target.stat().st_size != entry.size_bytes:
            problems.append(f"{name}: size does not match the manifest")
    validation = validate_adapter_directory(path, lora=lora,
                                            base_model_id=base_model_id,
                                            expect_manifest=True)
    problems.extend(validation.problems)
    if not problems and adapter_tree_hash(manifest.files) != manifest.tree_hash:
        problems.append("the manifest's own file list does not produce its tree hash")
    if not problems and validation.tree_hash != manifest.tree_hash:
        problems.append(
            f"the directory tree hashes to {short(validation.tree_hash)}, the manifest "
            f"recorded {short(manifest.tree_hash)}")
    return tuple(problems)


__all__ = [
    "ADAPTER_CONFIG_FILE", "ADAPTER_MANIFEST_FILE", "ADAPTER_MANIFEST_VERSION",
    "ADAPTER_WEIGHTS_FILE", "ALLOWED_ADAPTER_FILES", "FORBIDDEN_SUFFIXES",
    "MAX_ADAPTER_FILES", "MAX_ADAPTER_FILE_BYTES", "MAX_ADAPTER_TOTAL_BYTES",
    "adapter_tree_hash",
    "OPTIONAL_ADAPTER_FILES", "REQUIRED_ADAPTER_FILES", "AdapterArtifactError",
    "AdapterManifest", "ArtifactFile", "ArtifactValidation", "ArtifactVerdict",
    "read_safetensors_header", "validate_adapter_directory", "verify_completed_run",
]
