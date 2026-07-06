"""
core/training_pipeline.py — V65 Milestone 17: reproducible training experiments.

A *practical* training-experiment system for the local host (AMD Ryzen 5 7430U,
64 GB RAM, no discrete GPU). It does **not** pretrain from scratch and it **never
fakes a training run**: without an available backend, an experiment plans and
validates but reports honestly that it did not execute.

What it provides:
  * a backend-agnostic interface (`TrainingBackend`) so Transformers/PEFT/TRL/
    Unsloth/Axolotl can each be an adapter — only adapters justified by installed
    dependencies are *available*; the rest report `available=False` (never faked);
  * config validation, dataset **provenance gating**, and a **dry-run planner**
    (estimated examples/tokens, sequence length, memory pressure, backend
    availability, expected artifact path);
  * run metadata, artifact discovery, and failure-state recording under a
    versioned `training/` tree where a run id can never silently overwrite another.

Safety contract (V65 non-negotiables):
  * An experiment may consume **only** an M16-produced dataset (or an import with
    an equivalent manifest) — `verify_dataset` re-checks existence, manifest,
    pinned version, **content-hash match**, all-approved status, no quarantined/
    rejected/secret-bearing records, schema, and a minimum sample count.
  * Training never runs on the chat event loop, never uses `shell=True`, and never
    bypasses the tool gateway — backends generate **argv lists**, not shell
    strings, and execution is explicit (a confirm token), never automatic.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from loguru import logger

from core.dataset_pipeline import (
    CandidateStatus,
    TrainingExample,
    dataset_content_hash,
    load_dataset,
)

PIPELINE_VERSION = "v65.m17"
_REPO_DIR = Path(__file__).resolve().parents[1]
_TRAINING_ROOT = _REPO_DIR / "training"

_CHARS_PER_TOKEN = 4.0          # crude but stable token estimate (offline, deterministic)
_MIN_SAMPLES_DEFAULT = 8


# ── enums ─────────────────────────────────────────────────────────────────────
class TrainingMethod(str, Enum):
    DRY_RUN = "dry_run"     # plan/validate only — never executes
    SFT = "sft"             # supervised fine-tune (full)
    LORA = "lora"           # low-rank adapters
    QLORA = "qlora"         # 4-bit base + low-rank adapters
    DPO = "dpo"             # preference optimization (needs a preference dataset)

    @property
    def required_deps(self) -> tuple[str, ...]:
        return {
            TrainingMethod.DRY_RUN: (),
            TrainingMethod.SFT: ("torch", "transformers"),
            TrainingMethod.LORA: ("torch", "transformers", "peft"),
            TrainingMethod.QLORA: ("torch", "transformers", "peft", "bitsandbytes"),
            TrainingMethod.DPO: ("torch", "transformers", "peft", "trl"),
        }[self]

    @property
    def needs_preference_data(self) -> bool:
        return self is TrainingMethod.DPO


class TrainingStatus(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"     # config + dataset passed all gates
    PLANNED = "planned"         # dry-run plan produced
    RUNNING = "running"
    COMPLETED = "completed"     # a real artifact exists
    FAILED = "failed"
    ABORTED = "aborted"


class MemoryPressure(str, Enum):
    OK = "ok"
    TIGHT = "tight"
    OVER = "over"


# ── hardware / references ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class HardwareProfile:
    name: str = "local"
    device: str = "cpu"            # "cpu" | "cuda"
    total_ram_gb: float = 0.0
    vram_gb: float = 0.0
    cpu_threads: int = 0

    @property
    def is_gpu(self) -> bool:
        return self.device == "cuda" and self.vram_gb > 0

    @property
    def usable_gb(self) -> float:
        """The memory budget training may lean on (VRAM on GPU, else a fraction of
        system RAM — CPU training must leave headroom for the OS/JARVIS)."""
        return self.vram_gb if self.is_gpu else round(self.total_ram_gb * 0.6, 1)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def detect(cls) -> "HardwareProfile":
        """Best-effort local probe. Fail-safe: unknowns default to conservative."""
        device, vram = "cpu", 0.0
        try:
            import torch
            if torch.cuda.is_available():  # pragma: no cover — no GPU on target host
                device = "cuda"
                vram = round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1)
        except Exception:
            pass
        total_ram, threads = 0.0, 0
        try:
            import psutil
            total_ram = round(psutil.virtual_memory().total / 1e9, 1)
            threads = psutil.cpu_count(logical=True) or 0
        except Exception:
            pass
        return cls(name="local", device=device, total_ram_gb=total_ram,
                   vram_gb=vram, cpu_threads=threads)


@dataclass(frozen=True)
class BaseModelReference:
    model_id: str
    family: str = ""
    params_b: float = 0.0          # parameter count in billions
    context_length: int = 4096
    quantization: str = "none"     # none | 4bit | 8bit

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DatasetReference:
    """Points at an M16 dataset version directory (`<dir>/train.jsonl` +
    `manifest.json`). ``expected_hash`` pins the content the config was built for."""

    version: str
    path: str                      # the <version> directory
    expected_hash: str = ""        # from the M16 manifest at config time

    def resolve(self) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() else _REPO_DIR / p

    def manifest_path(self) -> Path:
        return self.resolve() / "manifest.json"

    def train_path(self) -> Path:
        return self.resolve() / "train.jsonl"

    def to_dict(self) -> dict:
        return asdict(self)


# ── config ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TrainingConfig:
    run_id: str
    base_model: BaseModelReference
    dataset: DatasetReference
    training_method: TrainingMethod
    output_path: str = ""
    epochs: int = 1
    learning_rate: float = 2e-4
    batch_size: int = 1
    gradient_accumulation: int = 8
    max_sequence_length: int = 1024
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    seed: int = 42
    hardware_profile: HardwareProfile = field(default_factory=HardwareProfile)
    evaluation_profile: str = ""   # skill-profile / eval suite to gate promotion later
    min_samples: int = _MIN_SAMPLES_DEFAULT
    notes: str = ""

    def resolved_output(self) -> Path:
        if self.output_path:
            p = Path(self.output_path)
            return p if p.is_absolute() else _REPO_DIR / p
        return _TRAINING_ROOT / "adapters" / self.run_id

    def validate(self) -> tuple[str, ...]:
        """Static config sanity (independent of dataset/backend). Deterministic."""
        issues: list[str] = []
        if not self.run_id or not self.run_id.strip():
            issues.append("run_id is required")
        if any(c in self.run_id for c in "/\\ ") if self.run_id else False:
            issues.append("run_id must not contain path separators or spaces")
        if not self.base_model.model_id:
            issues.append("base_model.model_id is required")
        if self.epochs < 1:
            issues.append("epochs must be >= 1")
        if not (0 < self.learning_rate < 1):
            issues.append("learning_rate out of range (0,1)")
        if self.batch_size < 1 or self.gradient_accumulation < 1:
            issues.append("batch_size and gradient_accumulation must be >= 1")
        if self.max_sequence_length < 16:
            issues.append("max_sequence_length too small")
        if self.max_sequence_length > self.base_model.context_length:
            issues.append("max_sequence_length exceeds base model context length")
        if self.training_method in (TrainingMethod.LORA, TrainingMethod.QLORA, TrainingMethod.DPO):
            if self.lora_rank < 1 or self.lora_alpha < 1:
                issues.append("lora_rank / lora_alpha must be >= 1")
        return tuple(issues)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["training_method"] = self.training_method.value
        return d


# ── plan / metrics / artifact / run ───────────────────────────────────────────
@dataclass(frozen=True)
class DryRunPlan:
    method: str
    backend: str
    backend_available: bool
    estimated_examples: int
    estimated_tokens: int
    max_sequence_length: int
    estimated_memory_gb: float
    memory_pressure: MemoryPressure
    usable_memory_gb: float
    output_artifact_path: str
    feasible: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["memory_pressure"] = self.memory_pressure.value
        d["warnings"] = list(self.warnings)
        return d


@dataclass(frozen=True)
class TrainingMetrics:
    steps: int = 0
    epochs_completed: int = 0
    train_loss: float | None = None
    eval_loss: float | None = None
    tokens_seen: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AdapterArtifact:
    run_id: str
    method: str
    path: str
    base_model: str
    dataset_version: str
    content_hash: str = ""
    size_bytes: int = 0
    created_ts: float = 0.0

    def exists(self) -> bool:
        p = Path(self.path)
        return p.exists() and (p.is_dir() and any(p.iterdir()) if p.is_dir() else p.stat().st_size > 0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrainingRun:
    run_id: str
    config: TrainingConfig
    status: TrainingStatus = TrainingStatus.CREATED
    plan: DryRunPlan | None = None
    metrics: TrainingMetrics | None = None
    artifact: AdapterArtifact | None = None
    error: str | None = None
    gate_issues: tuple[str, ...] = ()
    created_ts: float = 0.0
    updated_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "config": self.config.to_dict(),
            "status": self.status.value,
            "plan": self.plan.to_dict() if self.plan else None,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "error": self.error, "gate_issues": list(self.gate_issues),
            "pipeline_version": PIPELINE_VERSION,
            "created_ts": self.created_ts, "updated_ts": self.updated_ts,
        }


@dataclass(frozen=True)
class DatasetGateResult:
    ok: bool
    version: str
    example_count: int
    content_hash: str
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issues"] = list(self.issues)
        return d


# ── backend interface ─────────────────────────────────────────────────────────
@runtime_checkable
class TrainingBackend(Protocol):
    name: str

    def supports(self, method: TrainingMethod) -> bool: ...
    def is_available(self, method: TrainingMethod) -> bool: ...
    def probe(self) -> dict: ...
    def generate_command(self, config: TrainingConfig) -> list[str]: ...


def _dep_available(mod: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


class TransformersPeftBackend:
    """Adapter over the Transformers/PEFT/TRL ecosystem. Availability is probed
    honestly per method — a method whose deps are missing is simply unavailable;
    nothing is faked. Planning/command generation are pure and work regardless, so
    a config remains reproducible even before the deps are installed."""

    name = "transformers_peft"

    def supports(self, method: TrainingMethod) -> bool:
        return method in (TrainingMethod.SFT, TrainingMethod.LORA,
                          TrainingMethod.QLORA, TrainingMethod.DPO)

    def is_available(self, method: TrainingMethod) -> bool:
        return self.supports(method) and all(_dep_available(d) for d in method.required_deps)

    def probe(self) -> dict:
        return {"backend": self.name,
                "deps": {d: _dep_available(d)
                         for d in ("torch", "transformers", "peft", "trl", "bitsandbytes")}}

    def generate_command(self, config: TrainingConfig) -> list[str]:
        """Produce the **argv** a training launcher would run — never a shell
        string (shell=False contract). Points at a scripts/ entrypoint and passes
        the run's config file; the actual launch stays explicit and out-of-loop."""
        cfg_path = _TRAINING_ROOT / "configs" / f"{config.run_id}.json"
        return [
            "python", "-m", "scripts.train_experiment",
            "--config", str(cfg_path),
            "--method", config.training_method.value,
            "--output", str(config.resolved_output()),
        ]


class DryRunBackend:
    """The honest default: always available, plans and generates config/argv, but
    **never executes** and never produces an artifact. Used when no real training
    ecosystem is installed (the current local host)."""

    name = "dry_run"

    def supports(self, method: TrainingMethod) -> bool:
        return True

    def is_available(self, method: TrainingMethod) -> bool:
        return method is TrainingMethod.DRY_RUN

    def probe(self) -> dict:
        return {"backend": self.name, "executes": False}

    def generate_command(self, config: TrainingConfig) -> list[str]:
        return ["true"]  # a no-op argv; dry-run never launches anything


def default_backends() -> list[TrainingBackend]:
    return [TransformersPeftBackend(), DryRunBackend()]


# ── memory estimation (deterministic heuristic) ───────────────────────────────
def estimate_memory_gb(config: TrainingConfig) -> float:
    """Rough peak-memory estimate (GB). Deterministic, backend-independent — for
    planning only. Full SFT keeps fp16 weights + Adam states (~16 B/param); LoRA
    keeps fp16 weights + tiny adapters (~2.5 B/param); QLoRA 4-bit base (~0.7)."""
    p = max(config.base_model.params_b, 0.0)
    per_param = {
        TrainingMethod.SFT: 16.0,
        TrainingMethod.LORA: 2.5,
        TrainingMethod.QLORA: 0.7,
        TrainingMethod.DPO: 3.5,     # LoRA-style + reference model overhead
        TrainingMethod.DRY_RUN: 2.0,
    }[config.training_method]
    weights = p * per_param
    # Activation term scales with batch * seq; small, bounded contribution.
    act = (config.batch_size * config.max_sequence_length / 1024.0) * 0.5
    return round(weights + act, 2)


def _pressure(est_gb: float, usable_gb: float) -> MemoryPressure:
    if usable_gb <= 0:
        return MemoryPressure.TIGHT   # unknown budget → be cautious, not reckless
    ratio = est_gb / usable_gb
    if ratio > 1.0:
        return MemoryPressure.OVER
    if ratio > 0.7:
        return MemoryPressure.TIGHT
    return MemoryPressure.OK


# ── the pipeline ──────────────────────────────────────────────────────────────
class TrainingPipeline:
    """Validates configs, gates datasets on M16 provenance, plans dry runs, and
    records run metadata. Executes **only** when a backend is available and an
    explicit confirm token is supplied — otherwise it records an honest failure,
    never a fabricated success."""

    def __init__(self, *, backends: list[TrainingBackend] | None = None,
                 root: Path | None = None) -> None:
        self.backends = backends if backends is not None else default_backends()
        self.root = root or _TRAINING_ROOT

    # ── dataset provenance gate (the safety contract) ────────────────────────
    def verify_dataset(self, ref: DatasetReference, *, min_samples: int = _MIN_SAMPLES_DEFAULT) -> DatasetGateResult:
        from core.dlp_sensor import classify as dlp_classify
        from core.memory_router import contains_secret

        issues: list[str] = []
        train_p, manifest_p = ref.train_path(), ref.manifest_path()
        if not train_p.is_file():
            return DatasetGateResult(False, ref.version, 0, "", (f"train.jsonl missing: {train_p}",))
        if not manifest_p.is_file():
            return DatasetGateResult(False, ref.version, 0, "", (f"manifest.json missing: {manifest_p}",))

        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return DatasetGateResult(False, ref.version, 0, "", (f"manifest unreadable: {e}",))

        if str(manifest.get("version", "")) != ref.version:
            issues.append(f"version mismatch: manifest={manifest.get('version')} ref={ref.version}")

        try:
            examples: list[TrainingExample] = load_dataset(ref.resolve())
        except Exception as e:  # noqa: BLE001
            return DatasetGateResult(False, ref.version, 0, "", (f"dataset unreadable: {e}",))

        actual_hash = dataset_content_hash(examples)
        manifest_hash = str(manifest.get("content_sha256", ""))
        if manifest_hash and actual_hash != manifest_hash:
            issues.append("content hash mismatch (dataset drifted from its manifest)")
        if ref.expected_hash and actual_hash != ref.expected_hash:
            issues.append("content hash does not match the pinned config hash")

        # Every record must be APPROVED — never quarantined/rejected/pending.
        bad_status = [e.id for e in examples if e.status is not CandidateStatus.APPROVED]
        if bad_status:
            issues.append(f"{len(bad_status)} non-approved example(s) present")
        # Defense in depth: re-scan for secrets even though M16 quarantined them.
        secret_hits = [e.id for e in examples
                       if contains_secret(f"{e.prompt}\n{e.ideal_output}")
                       or dlp_classify(f"{e.prompt}\n{e.ideal_output}")]
        if secret_hits:
            issues.append(f"{len(secret_hits)} secret/PII-bearing record(s) present")
        # Schema: non-empty prompt + target.
        malformed = [e.id for e in examples if not e.prompt.strip() or not e.ideal_output.strip()]
        if malformed:
            issues.append(f"{len(malformed)} malformed record(s) (empty prompt/target)")
        if len(examples) < min_samples:
            issues.append(f"insufficient samples: {len(examples)} < {min_samples}")

        return DatasetGateResult(
            ok=not issues, version=ref.version, example_count=len(examples),
            content_hash=actual_hash, issues=tuple(issues),
        )

    # ── backend resolution ───────────────────────────────────────────────────
    def backend_for(self, method: TrainingMethod, *, require_available: bool = False):
        for b in self.backends:
            if b.supports(method) and (b.is_available(method) if require_available else True):
                return b
        return None

    def available_backend_for(self, method: TrainingMethod):
        return self.backend_for(method, require_available=True)

    # ── token estimate ───────────────────────────────────────────────────────
    def _estimate_tokens(self, examples: list[TrainingExample], max_seq: int) -> int:
        total = 0
        for e in examples:
            chars = len(e.prompt) + len(e.ideal_output)
            total += min(int(chars / _CHARS_PER_TOKEN), max_seq)
        return total

    # ── dry-run planner ──────────────────────────────────────────────────────
    def dry_run(self, config: TrainingConfig, *, now_ts: float = 0.0) -> TrainingRun:
        """Validate config, gate the dataset, and produce a `DryRunPlan`. Never
        executes. Always safe to call."""
        run = TrainingRun(run_id=config.run_id, config=config, created_ts=now_ts, updated_ts=now_ts)
        cfg_issues = config.validate()
        gate = self.verify_dataset(config.dataset, min_samples=config.min_samples)
        gate_issues = tuple(cfg_issues) + tuple(f"dataset:{i}" for i in gate.issues)

        examples = load_dataset(config.dataset.resolve()) if gate.example_count else []
        est_tokens = self._estimate_tokens(examples, config.max_sequence_length)
        est_mem = estimate_memory_gb(config)
        usable = config.hardware_profile.usable_gb
        pressure = _pressure(est_mem, usable)

        backend = self.backend_for(config.training_method)
        backend_available = bool(
            backend is not None and backend.is_available(config.training_method)
        )
        warnings: list[str] = []
        if config.training_method.needs_preference_data:
            warnings.append("DPO requires a preference dataset (chosen/rejected pairs); "
                            "the M16 SFT schema is not preference-shaped")
        if not backend_available:
            missing = [d for d in config.training_method.required_deps if not _dep_available(d)]
            warnings.append(f"no available backend to execute {config.training_method.value}"
                            + (f" (missing: {', '.join(missing)})" if missing else ""))
        if pressure is MemoryPressure.OVER:
            warnings.append(f"estimated {est_mem}GB exceeds usable {usable}GB — infeasible locally")

        feasible = gate.ok and not cfg_issues and pressure is not MemoryPressure.OVER
        plan = DryRunPlan(
            method=config.training_method.value,
            backend=backend.name if backend else "none",
            backend_available=backend_available,
            estimated_examples=gate.example_count, estimated_tokens=est_tokens,
            max_sequence_length=config.max_sequence_length,
            estimated_memory_gb=est_mem, memory_pressure=pressure, usable_memory_gb=usable,
            output_artifact_path=str(config.resolved_output()),
            feasible=feasible, warnings=tuple(warnings),
        )
        run.plan = plan
        run.gate_issues = gate_issues
        run.status = TrainingStatus.PLANNED if gate.ok and not cfg_issues else TrainingStatus.CREATED
        run.updated_ts = now_ts
        return run

    # ── run persistence (never overwrite another run) ────────────────────────
    def _run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def save_run(self, run: TrainingRun, *, allow_overwrite: bool = False) -> Path:
        run_dir = self._run_dir(run.run_id)
        run_json = run_dir / "run.json"
        if run_json.exists() and not allow_overwrite:
            raise FileExistsError(f"run already exists (won't overwrite): {run_json}")
        run_dir.mkdir(parents=True, exist_ok=True)
        run_json.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
                            encoding="utf-8")
        return run_json

    def write_config(self, config: TrainingConfig) -> Path:
        cfg_path = self.root / "configs" / f"{config.run_id}.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
                            encoding="utf-8")
        return cfg_path

    # ── explicit execution (never automatic, never faked) ────────────────────
    def execute(self, config: TrainingConfig, *, confirm: str, now_ts: float = 0.0,
                allow_overwrite: bool = False) -> TrainingRun:
        """Explicitly attempt a training run. Requires ``confirm == config.run_id``
        (a deliberate human/operator gesture) and an **available** backend for the
        method. With no available backend, records a FAILED run — never a fake
        success and never an empty 'artifact'."""
        run = self.dry_run(config, now_ts=now_ts)
        if confirm != config.run_id:
            run.status = TrainingStatus.ABORTED
            run.error = "execution not confirmed (confirm must equal run_id)"
            return run
        if run.gate_issues:
            run.status = TrainingStatus.FAILED
            run.error = f"pre-flight gates failed: {list(run.gate_issues)}"
            self.save_run(run, allow_overwrite=allow_overwrite)
            return run

        backend = self.available_backend_for(config.training_method)
        if backend is None:
            run.status = TrainingStatus.FAILED
            run.error = (f"no available backend can execute {config.training_method.value} "
                         f"on this host — training NOT performed (no artifact produced)")
            logger.warning(f"M17: {run.error}")
            self.save_run(run, allow_overwrite=allow_overwrite)
            return run

        # A real backend is available: persist the config + argv for the explicit,
        # out-of-loop launcher. We deliberately do not spawn training here — the
        # launcher (scripts/train_experiment) runs it as a background job.
        self.write_config(config)
        run.status = TrainingStatus.VALIDATED
        run.error = None
        run.updated_ts = now_ts
        self.save_run(run, allow_overwrite=allow_overwrite)
        logger.info(f"M17: run {config.run_id} validated + config written; "
                    f"launch argv: {backend.generate_command(config)}")
        return run

    def discover_artifact(self, config: TrainingConfig, *, now_ts: float = 0.0) -> AdapterArtifact | None:
        """Look for a materialized adapter from a completed external run. Returns
        None when nothing exists — never invents an artifact."""
        out = config.resolved_output()
        if not out.exists():
            return None
        if out.is_dir() and not any(out.iterdir()):
            return None
        size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() \
            else out.stat().st_size
        return AdapterArtifact(
            run_id=config.run_id, method=config.training_method.value, path=str(out),
            base_model=config.base_model.model_id, dataset_version=config.dataset.version,
            size_bytes=size, created_ts=now_ts,
        )
