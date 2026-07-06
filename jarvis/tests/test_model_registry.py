"""
tests/test_model_registry.py — V65 evaluation-gated Model Registry.

Proves: registration + duplicate handling, artifact hash validation, evaluation
snapshot linkage from a real EvalRun, role-specific promotion (a coder candidate
wins CODER without displacing GENERAL), critical-regression blocking, rollback
restoring the prior assignment, and append-only promotion audit history.
"""
from __future__ import annotations

import pytest

from core.eval_harness import EvalResult, EvalRun
from core.model_registry import (
    ModelArtifact,
    ModelEvaluationSnapshot,
    ModelRecord,
    ModelRegistry,
    ModelRoleAssignment,
    ModelStatus,
    PromotionPolicy,
    PromotionStatus,
)
from core.model_router import ModelRole


# ── helpers ───────────────────────────────────────────────────────────────────
def _snap(run_id="r", pass_rate=0.8, *, metrics=None, domains=None, ts=1.0) -> ModelEvaluationSnapshot:
    return ModelEvaluationSnapshot(
        run_id=run_id, pass_rate=pass_rate, mean_score=pass_rate,
        metric_pass_rates=dict(metrics or {}), domain_pass_rates=dict(domains or {}), created_ts=ts,
    )


def _record(model_id, base="qwen2.5-0.5b", **kw) -> ModelRecord:
    return ModelRecord(model_id=model_id, base_model=base, created_ts=1.0, **kw)


def _registry_with_active(role, snapshot, model_id="base-model") -> ModelRegistry:
    reg = ModelRegistry()
    reg.register(_record(model_id))
    reg.attach_evaluation(model_id, snapshot)
    decision = reg.propose_promotion(model_id, role)  # first model → promoted
    reg.promote(decision, now_ts=1.0)
    return reg


# ── registration & duplicates ─────────────────────────────────────────────────
def test_register_and_get():
    reg = ModelRegistry()
    rec = reg.register(_record("m1"))
    assert reg.get("m1") is rec and rec.status is ModelStatus.EXPERIMENTAL


def test_duplicate_model_id_rejected():
    reg = ModelRegistry()
    reg.register(_record("m1"))
    with pytest.raises(ValueError):
        reg.register(_record("m1"))
    # explicit replace allowed
    reg.register(_record("m1", base="other"), allow_replace=True)
    assert reg.get("m1").base_model == "other"


# ── artifact hash validation ──────────────────────────────────────────────────
def test_artifact_integrity_pass_and_fail(tmp_path):
    f = tmp_path / "adapter.bin"
    f.write_bytes(b"model-weights-v1")
    art = ModelArtifact(kind="adapter", path=str(f))
    good = ModelArtifact(kind="adapter", path=str(f), content_hash=art.compute_hash())
    assert good.verify_integrity()
    bad = ModelArtifact(kind="adapter", path=str(f), content_hash="deadbeef")
    assert not bad.verify_integrity()


def test_register_rejects_bad_artifact_hash(tmp_path):
    f = tmp_path / "adapter.bin"
    f.write_bytes(b"weights")
    reg = ModelRegistry()
    bad = ModelArtifact(kind="adapter", path=str(f), content_hash="deadbeef")
    with pytest.raises(ValueError):
        reg.register(_record("m1", adapter_artifact=bad))


# ── evaluation snapshot linkage ───────────────────────────────────────────────
def test_snapshot_from_eval_run_and_linkage():
    run = EvalRun(run_id="run-1", results=[
        EvalResult(case_id="c1", domain="coder", passed=True, score=1.0,
                   metrics={"correctness": {"passed": True}}),
    ])
    snap = ModelEvaluationSnapshot.from_eval_run(run, now_ts=2.0)
    reg = ModelRegistry()
    reg.register(_record("m1"))
    rec = reg.attach_evaluation("m1", snap)
    assert rec.status is ModelStatus.EVALUATED
    assert rec.evaluation_snapshot.run_id == "run-1"
    assert rec.evaluation_snapshot.domain("coder") == 1.0


# ── promotion: first model, no baseline ───────────────────────────────────────
def test_first_model_for_role_is_promotable():
    reg = ModelRegistry()
    reg.register(_record("m1"))
    reg.attach_evaluation("m1", _snap(pass_rate=0.7))
    decision = reg.propose_promotion("m1", ModelRole.CODER)
    assert decision.promoted
    reg.promote(decision, now_ts=1.0)
    assert reg.active_for_role(ModelRole.CODER).model_id == "m1"


def test_candidate_without_snapshot_rejected():
    reg = ModelRegistry()
    reg.register(_record("m1"))
    decision = reg.propose_promotion("m1", ModelRole.CODER)
    assert decision.status is PromotionStatus.REJECTED
    with pytest.raises(ValueError):
        reg.promote(decision)


# ── role-specific promotion ───────────────────────────────────────────────────
def test_role_specific_promotion_does_not_disturb_other_roles():
    reg = ModelRegistry()
    # FAST (general) baseline.
    reg.register(_record("general-1"))
    reg.attach_evaluation("general-1", _snap("g", 0.8, domains={"general": 0.8}))
    reg.promote(reg.propose_promotion("general-1", ModelRole.FAST), now_ts=1.0)
    # CODER candidate wins CODER without disturbing the FAST assignment.
    reg.register(_record("coder-1"))
    reg.attach_evaluation("coder-1", _snap("c", 0.85, domains={"coder": 0.9}))
    dec = reg.propose_promotion("coder-1", ModelRole.CODER, target_domains=("coder",))
    assert dec.promoted
    reg.promote(dec, now_ts=2.0)
    assert reg.active_for_role(ModelRole.CODER).model_id == "coder-1"
    assert reg.active_for_role(ModelRole.FAST).model_id == "general-1"  # untouched


def test_promotion_allows_tradeoff_within_budget():
    # Candidate: +coding, -1% general (within 2% budget), no safety regression.
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0, "tool_safety": 1.0},
                 domains={"coder": 0.70, "general": 0.90})
    reg = _registry_with_active(ModelRole.CODER, base)
    reg.register(_record("cand"))
    reg.attach_evaluation("cand", _snap("cand", 0.79,  # -1% overall, within budget
                                        metrics={"injection_resistance": 1.0, "tool_safety": 1.0},
                                        domains={"coder": 0.82, "general": 0.89}))
    dec = reg.propose_promotion("cand", ModelRole.CODER, target_domains=("coder",))
    assert dec.promoted and dec.target_delta > 0


# ── critical regression blocks promotion ──────────────────────────────────────
def test_critical_injection_regression_blocks_promotion():
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0, "tool_safety": 1.0},
                 domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base)
    reg.register(_record("cand"))
    reg.attach_evaluation("cand", _snap("cand", 0.90,  # better overall...
                                        metrics={"injection_resistance": 0.8, "tool_safety": 1.0},
                                        domains={"coder": 0.95}))  # ...but injection regressed
    dec = reg.propose_promotion("cand", ModelRole.CODER, target_domains=("coder",))
    assert not dec.promoted
    assert any("injection_resistance" in r for r in dec.regressions)
    with pytest.raises(ValueError):
        reg.promote(dec)


def test_catastrophic_overall_regression_blocks_promotion():
    base = _snap("b", 0.90, metrics={"injection_resistance": 1.0},
                 domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base)
    reg.register(_record("cand"))
    reg.attach_evaluation("cand", _snap("cand", 0.50,  # -40% overall, way past budget
                                        metrics={"injection_resistance": 1.0},
                                        domains={"coder": 0.95}))
    dec = reg.propose_promotion("cand", ModelRole.CODER, target_domains=("coder",))
    assert not dec.promoted
    assert any("overall_pass_rate" in r for r in dec.regressions)


def test_insufficient_target_improvement_rejected():
    policy = PromotionPolicy(min_target_improvement=0.05)
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0}, domains={"coder": 0.80})
    reg = ModelRegistry(policy=policy)
    reg.register(_record("base"))
    reg.attach_evaluation("base", base)
    reg.promote(reg.propose_promotion("base", ModelRole.CODER), now_ts=1.0)
    reg.register(_record("cand"))
    reg.attach_evaluation("cand", _snap("cand", 0.81, metrics={"injection_resistance": 1.0},
                                        domains={"coder": 0.81}))  # +1% < required 5%
    dec = reg.propose_promotion("cand", ModelRole.CODER, target_domains=("coder",))
    assert not dec.promoted and any("target" in r for r in dec.reasons)


# ── rollback ──────────────────────────────────────────────────────────────────
def test_rollback_restores_prior_assignment():
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0}, domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base, model_id="model-A")
    reg.register(_record("model-B"))
    reg.attach_evaluation("model-B", _snap("cand", 0.85, metrics={"injection_resistance": 1.0},
                                           domains={"coder": 0.85}))
    reg.promote(reg.propose_promotion("model-B", ModelRole.CODER, target_domains=("coder",)), now_ts=2.0)
    assert reg.active_for_role(ModelRole.CODER).model_id == "model-B"
    # Regression observed in production → roll back to A.
    restored = reg.rollback(ModelRole.CODER, reason="prod regression", now_ts=3.0)
    assert restored.model_id == "model-A"
    assert reg.active_for_role(ModelRole.CODER).model_id == "model-A"
    assert reg.get("model-A").status is ModelStatus.ACTIVE
    assert reg.get("model-B").status is ModelStatus.DEPRECATED


def test_rollback_with_no_history_returns_none():
    reg = ModelRegistry()
    assert reg.rollback(ModelRole.CODER) is None


# ── audit history preserved ───────────────────────────────────────────────────
def test_promotion_history_is_append_only():
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0}, domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base, model_id="A")
    reg.register(_record("B"))
    reg.attach_evaluation("B", _snap("cand", 0.85, metrics={"injection_resistance": 1.0},
                                     domains={"coder": 0.85}))
    reg.promote(reg.propose_promotion("B", ModelRole.CODER, target_domains=("coder",)), now_ts=2.0)
    reg.rollback(ModelRole.CODER, reason="regressed", now_ts=3.0)
    hist_b = reg.get("B").promotion_history
    assert any(h.get("action") == "promoted" for h in hist_b)
    assert any(h.get("action") == "rolled_back" for h in hist_b)


def test_rejected_candidate_is_archived_with_audit():
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0}, domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base)
    reg.register(_record("cand"))
    reg.attach_evaluation("cand", _snap("cand", 0.9, metrics={"injection_resistance": 0.5},
                                        domains={"coder": 0.95}))
    dec = reg.propose_promotion("cand", ModelRole.CODER, target_domains=("coder",))
    reg.reject(dec, now_ts=4.0)
    assert reg.get("cand").status is ModelStatus.ARCHIVED
    assert any(h.get("action") == "rejected" for h in reg.get("cand").promotion_history)


# ── persistence ───────────────────────────────────────────────────────────────
def test_registry_save_load_round_trip(tmp_path):
    base = _snap("b", 0.80, metrics={"injection_resistance": 1.0}, domains={"coder": 0.70})
    reg = _registry_with_active(ModelRole.CODER, base, model_id="A")
    path = reg.save(tmp_path / "registry.json")
    loaded = ModelRegistry.load(path)
    assert loaded.active_for_role(ModelRole.CODER).model_id == "A"
    assert loaded.get("A").evaluation_snapshot.metric("injection_resistance") == 1.0
    assert isinstance(loaded._assignments[ModelRole.CODER], ModelRoleAssignment)
