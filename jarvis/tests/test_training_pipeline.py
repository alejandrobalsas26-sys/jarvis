"""
tests/test_training_pipeline.py — V65 M17 reproducible training pipeline.

Proves the safety contract: a config is statically validated, a dataset must
pass the M16 provenance gate (exists / manifest / pinned version / content-hash
match / all-approved / no secrets / schema / min samples), dry-runs are
reproducible and never execute, a missing backend is reported honestly (never a
faked success), failed runs are recorded, and a run id can never silently
overwrite another. Synchronous tests; coroutines via asyncio.run.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.dataset_pipeline import (
    DatasetPipeline,
    ExampleProvenance,
    TrainingCandidate,
    approve,
    write_dataset,
)
from core.training_pipeline import (
    BaseModelReference,
    DatasetReference,
    HardwareProfile,
    MemoryPressure,
    TrainingConfig,
    TrainingMethod,
    TrainingPipeline,
    TrainingStatus,
    estimate_memory_gb,
)


def _run(coro):
    return asyncio.run(coro)


# ── fixtures ──────────────────────────────────────────────────────────────────
def _approved_examples(n: int):
    p = DatasetPipeline()
    out = []
    for i in range(n):
        c = TrainingCandidate(
            prompt=f"question number {i} about python source tiers",
            ideal_output=f"answer {i}: python.org is a PRIMARY documentation source",
            domain="research", provenance=ExampleProvenance.EVAL_GROUND_TRUTH,
            failure_ref=f"c{i}",
        )
        out.append(approve(_run(p.evaluate(c)), "operator", now_ts=1.0))
    return out


def _dataset(tmp_path, n=10, version="v1"):
    exs = _approved_examples(n)
    manifest = write_dataset(exs, tmp_path, version=version, now_ts=1.0)
    ref = DatasetReference(version=version, path=str(tmp_path / version),
                           expected_hash=manifest.content_sha256)
    return ref, manifest


def _hw():
    return HardwareProfile(name="local", device="cpu", total_ram_gb=64.0, cpu_threads=12)


def _config(ref, tmp_path, *, method=TrainingMethod.LORA, run_id="exp-001", **kw):
    base = dict(
        run_id=run_id,
        base_model=BaseModelReference("qwen2.5-0.5b", "qwen", 0.5, 4096),
        dataset=ref, training_method=method, hardware_profile=_hw(),
        output_path=str(tmp_path / "adapters" / run_id),
    )
    base.update(kw)
    return TrainingConfig(**base)


def _pipe(tmp_path):
    return TrainingPipeline(root=tmp_path / "training")


# ── config validation ─────────────────────────────────────────────────────────
def test_invalid_config_rejected(tmp_path):
    ref, _ = _dataset(tmp_path)
    bad = _config(ref, tmp_path, epochs=0, learning_rate=5.0, run_id="bad id")
    issues = bad.validate()
    assert any("epochs" in i for i in issues)
    assert any("learning_rate" in i for i in issues)
    assert any("run_id" in i for i in issues)


def test_seq_len_exceeding_context_rejected(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, max_sequence_length=99999)
    assert any("context length" in i for i in cfg.validate())


# ── dataset provenance gate ───────────────────────────────────────────────────
def test_valid_dataset_passes_gate(tmp_path):
    ref, _ = _dataset(tmp_path)
    gate = _pipe(tmp_path).verify_dataset(ref)
    assert gate.ok and gate.example_count == 10 and gate.issues == ()


def test_missing_dataset_rejected(tmp_path):
    ref = DatasetReference(version="v9", path=str(tmp_path / "v9"))
    gate = _pipe(tmp_path).verify_dataset(ref)
    assert not gate.ok and any("missing" in i for i in gate.issues)


def test_hash_mismatch_rejected(tmp_path):
    ref, _ = _dataset(tmp_path)
    tampered = DatasetReference(version="v1", path=ref.path, expected_hash="deadbeef")
    gate = _pipe(tmp_path).verify_dataset(tampered)
    assert not gate.ok and any("pinned config hash" in i for i in gate.issues)


def test_manifest_drift_rejected(tmp_path):
    ref, _ = _dataset(tmp_path)
    # Append an extra approved row to train.jsonl without updating the manifest.
    extra = _approved_examples(1)[0]
    train_p = ref.train_path()
    with train_p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**extra.to_dict(), "id": "drifted", "prompt": "p", "ideal_output": "o"}) + "\n")
    gate = _pipe(tmp_path).verify_dataset(ref)
    assert not gate.ok and any("drifted from its manifest" in i for i in gate.issues)


def test_unapproved_record_rejected(tmp_path):
    ref, manifest = _dataset(tmp_path)
    # Inject a non-approved record directly.
    ex = _approved_examples(1)[0].to_dict()
    ex["status"] = "pending_review"
    ex["id"] = "pending-1"
    with ref.train_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ex) + "\n")
    gate = _pipe(tmp_path).verify_dataset(ref)
    assert not gate.ok and any("non-approved" in i for i in gate.issues)


def test_secret_bearing_record_rejected(tmp_path):
    ref, _ = _dataset(tmp_path)
    ex = _approved_examples(1)[0].to_dict()
    ex["id"] = "leak-1"
    ex["ideal_output"] = "the password = hunter2secretvalue is embedded"
    with ref.train_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ex) + "\n")
    gate = _pipe(tmp_path).verify_dataset(ref)
    assert not gate.ok and any("secret" in i for i in gate.issues)


def test_insufficient_samples_rejected(tmp_path):
    ref, _ = _dataset(tmp_path, n=3)
    gate = _pipe(tmp_path).verify_dataset(ref, min_samples=8)
    assert not gate.ok and any("insufficient samples" in i for i in gate.issues)


# ── dry-run planning ──────────────────────────────────────────────────────────
def test_dry_run_reproducible(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path)
    pipe = _pipe(tmp_path)
    r1 = pipe.dry_run(cfg, now_ts=1.0)
    r2 = pipe.dry_run(cfg, now_ts=2.0)
    assert r1.plan.to_dict() == r2.plan.to_dict()  # deterministic (ts excluded from plan)
    assert r1.status is TrainingStatus.PLANNED


def test_dry_run_never_executes_and_produces_no_artifact(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path)
    run = _pipe(tmp_path).dry_run(cfg, now_ts=1.0)
    assert run.artifact is None
    assert not cfg.resolved_output().exists()


def test_backend_unavailable_reported_honestly(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.LORA)  # peft missing on host
    run = _pipe(tmp_path).dry_run(cfg, now_ts=1.0)
    assert run.plan.backend_available is False
    assert any("no available backend" in w for w in run.plan.warnings)


def test_dpo_warns_about_preference_schema(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.DPO)
    run = _pipe(tmp_path).dry_run(cfg, now_ts=1.0)
    assert any("preference dataset" in w for w in run.plan.warnings)


def test_memory_pressure_over_is_infeasible(tmp_path):
    ref, _ = _dataset(tmp_path)
    # A 70B full SFT on a 64GB CPU host is over budget.
    cfg = _config(ref, tmp_path, method=TrainingMethod.SFT,
                  base_model=BaseModelReference("huge-70b", "x", 70.0, 4096))
    run = _pipe(tmp_path).dry_run(cfg, now_ts=1.0)
    assert run.plan.memory_pressure is MemoryPressure.OVER
    assert run.plan.feasible is False


def test_memory_estimate_orders_by_method(tmp_path):
    ref, _ = _dataset(tmp_path)
    base = BaseModelReference("m", "x", 7.0, 4096)
    sft = _config(ref, tmp_path, method=TrainingMethod.SFT, base_model=base)
    qlora = _config(ref, tmp_path, method=TrainingMethod.QLORA, base_model=base)
    assert estimate_memory_gb(sft) > estimate_memory_gb(qlora)


# ── explicit execution (never automatic, never faked) ─────────────────────────
def test_execute_requires_confirmation(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path)
    run = _pipe(tmp_path).execute(cfg, confirm="not-the-run-id", now_ts=1.0)
    assert run.status is TrainingStatus.ABORTED


def test_execute_without_backend_records_failure(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.LORA)
    pipe = _pipe(tmp_path)
    run = pipe.execute(cfg, confirm="exp-001", now_ts=1.0)
    assert run.status is TrainingStatus.FAILED
    assert "NOT performed" in run.error and run.artifact is None
    # Failure state is recorded on disk.
    saved = json.loads((pipe._run_dir("exp-001") / "run.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"


def test_execute_with_gate_failure_records_failure(tmp_path):
    ref = DatasetReference(version="v1", path=str(tmp_path / "v1"))  # nonexistent
    cfg = _config(ref, tmp_path, method=TrainingMethod.SFT)
    run = _pipe(tmp_path).execute(cfg, confirm="exp-001", now_ts=1.0)
    assert run.status is TrainingStatus.FAILED
    assert any("dataset:" in g for g in run.gate_issues)


def test_run_cannot_silently_overwrite_another(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.LORA)
    pipe = _pipe(tmp_path)
    pipe.execute(cfg, confirm="exp-001", now_ts=1.0)  # writes runs/exp-001/run.json
    with pytest.raises(FileExistsError):
        pipe.save_run(pipe.dry_run(cfg, now_ts=2.0))
    # explicit overwrite is allowed
    pipe.save_run(pipe.dry_run(cfg, now_ts=3.0), allow_overwrite=True)


def test_discover_artifact_returns_none_when_absent(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path)
    assert _pipe(tmp_path).discover_artifact(cfg) is None


def test_discover_artifact_metadata_preserved(tmp_path):
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.LORA)
    # Simulate an externally-produced adapter directory.
    out = cfg.resolved_output()
    out.mkdir(parents=True, exist_ok=True)
    (out / "adapter_model.safetensors").write_bytes(b"weights")
    art = _pipe(tmp_path).discover_artifact(cfg, now_ts=5.0)
    assert art is not None and art.exists()
    assert art.run_id == "exp-001" and art.dataset_version == "v1"
    assert art.base_model == "qwen2.5-0.5b" and art.size_bytes > 0


# ── config write produces argv, never a shell string ──────────────────────────
def test_backend_generates_argv_list_not_shell_string(tmp_path):
    from core.training_pipeline import TransformersPeftBackend
    ref, _ = _dataset(tmp_path)
    cfg = _config(ref, tmp_path, method=TrainingMethod.LORA)
    argv = TransformersPeftBackend().generate_command(cfg)
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert "--config" in argv  # structured args, no interpolated shell command


def test_hardware_detect_is_safe():
    hw = HardwareProfile.detect()
    assert hw.device in ("cpu", "cuda") and hw.usable_gb >= 0
