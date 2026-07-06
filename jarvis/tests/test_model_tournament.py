"""
tests/test_model_tournament.py — V65 bounded domain-aware model tournament.

Proves: the same eval basis for all participants, a bounded participant set,
deterministic ranking on fixed snapshots, per-domain leaderboards (a coder model
wins CODER without winning everything), a latency/resource penalty that actually
moves rankings, and that running a tournament NEVER activates a model (the
registry assignments are untouched — recommendations are advisory only).
"""
from __future__ import annotations

from core.model_registry import (
    ModelEvaluationSnapshot,
    ModelRecord,
    ModelRegistry,
)
from core.model_router import ModelRole
from core.model_tournament import (
    ModelTournament,
    ScoringWeights,
    TournamentConfig,
)


def _snap(pass_rate, *, metrics=None, domains=None, latency=1.0, resource=1.0):
    return ModelEvaluationSnapshot(
        run_id="shared-eval", pass_rate=pass_rate, mean_score=pass_rate,
        metric_pass_rates=dict(metrics or {"injection_resistance": 1.0, "citation_validity": 1.0}),
        domain_pass_rates=dict(domains or {}), latency_s=latency, resource_gb=resource,
    )


def _cfg(**kw):
    base = dict(tournament_id="t1", eval_set_id="shared-eval",
                domains=("general", "coder"), max_participants=8)
    base.update(kw)
    return TournamentConfig(**base)


# ── basic run / shared eval set ───────────────────────────────────────────────
def test_report_records_shared_eval_set():
    snaps = {
        "A": _snap(0.8, domains={"general": 0.9, "coder": 0.6}),
        "B": _snap(0.8, domains={"general": 0.6, "coder": 0.9}),
    }
    report = ModelTournament().run(_cfg(), snaps, participant_ids=["A", "B"])
    assert report.eval_set_id == "shared-eval"
    assert set(report.participants) == {"A", "B"}


# ── domain-specific leaderboard ───────────────────────────────────────────────
def test_domain_specific_winners():
    snaps = {
        "generalist": _snap(0.85, domains={"general": 0.95, "coder": 0.55}),
        "coder": _snap(0.80, domains={"general": 0.55, "coder": 0.95}),
    }
    report = ModelTournament().run(_cfg(), snaps, participant_ids=["generalist", "coder"])
    lbs = {lb.domain: lb for lb in report.leaderboards}
    assert lbs["general"].winner == "generalist"
    assert lbs["coder"].winner == "coder"  # a coder wins CODER without winning general


def test_recommendation_maps_domain_to_winner():
    snaps = {
        "generalist": _snap(0.85, domains={"general": 0.95, "coder": 0.55}),
        "coder": _snap(0.80, domains={"general": 0.55, "coder": 0.95}),
    }
    report = ModelTournament().run(_cfg(), snaps, participant_ids=["generalist", "coder"])
    assert report.recommendation.by_domain["coder"] == "coder"
    assert report.recommendation.by_domain["general"] == "generalist"
    # Advisory only — never auto-applied.
    assert report.recommendation.to_dict()["auto_applied"] is False


# ── deterministic ranking ─────────────────────────────────────────────────────
def test_ranking_is_deterministic():
    snaps = {
        "A": _snap(0.8, domains={"coder": 0.9}), "B": _snap(0.8, domains={"coder": 0.9}),
        "C": _snap(0.8, domains={"coder": 0.7}),
    }
    ids = ["C", "A", "B"]
    r1 = ModelTournament().run(_cfg(domains=("coder",)), snaps, participant_ids=ids)
    r2 = ModelTournament().run(_cfg(domains=("coder",)), snaps, participant_ids=list(reversed(ids)))
    board1 = r1.leaderboards[0].ranked
    board2 = r2.leaderboards[0].ranked
    assert board1 == board2  # stable regardless of input order
    # Tie between A and B broken by model_id asc.
    assert [m for m, _ in board1][:2] == ["A", "B"]


# ── latency / resource penalty ────────────────────────────────────────────────
def test_latency_penalty_moves_ranking():
    # Two models identical on the domain; the slower one must rank lower.
    fast = _snap(0.8, domains={"coder": 0.9}, latency=1.0)
    slow = _snap(0.8, domains={"coder": 0.9}, latency=10.0)
    report = ModelTournament().run(_cfg(domains=("coder",)),
                                   {"fast": fast, "slow": slow}, participant_ids=["slow", "fast"])
    ranked = [m for m, _ in report.leaderboards[0].ranked]
    assert ranked == ["fast", "slow"]


def test_resource_penalty_moves_ranking():
    light = _snap(0.8, domains={"coder": 0.9}, resource=2.0)
    heavy = _snap(0.8, domains={"coder": 0.9}, resource=40.0)
    report = ModelTournament().run(_cfg(domains=("coder",)),
                                   {"light": light, "heavy": heavy}, participant_ids=["heavy", "light"])
    assert [m for m, _ in report.leaderboards[0].ranked] == ["light", "heavy"]


def test_safety_weight_prevents_unsafe_fast_win():
    # A fast model that regressed on injection resistance should not top a safe one.
    unsafe_fast = _snap(0.8, metrics={"injection_resistance": 0.4, "citation_validity": 1.0},
                        domains={"coder": 0.9}, latency=1.0)
    safe = _snap(0.8, metrics={"injection_resistance": 1.0, "citation_validity": 1.0},
                 domains={"coder": 0.9}, latency=2.0)
    report = ModelTournament().run(_cfg(domains=("coder",)),
                                   {"unsafe_fast": unsafe_fast, "safe": safe},
                                   participant_ids=["unsafe_fast", "safe"])
    assert report.leaderboards[0].winner == "safe"


# ── bounded participants ──────────────────────────────────────────────────────
def test_participants_are_bounded():
    snaps = {f"m{i}": _snap(0.8, domains={"coder": 0.5 + i * 0.05}) for i in range(10)}
    cfg = _cfg(domains=("coder",), max_participants=3)
    report = ModelTournament().run(cfg, snaps, participant_ids=list(snaps.keys()))
    assert len(report.participants) == 3 and report.dropped_participants == 7


# ── registry discovery + no automatic activation ──────────────────────────────
def _registry_with_models():
    reg = ModelRegistry()
    for mid, dom in (("A", {"coder": 0.9}), ("B", {"coder": 0.7})):
        reg.register(ModelRecord(model_id=mid, base_model="x", created_ts=1.0))
        reg.attach_evaluation(mid, _snap(0.8, domains=dom))
    return reg


def test_select_participants_from_registry_bounded():
    reg = _registry_with_models()
    t = ModelTournament(registry=reg)
    ids = t.select_participants(_cfg(max_participants=1))
    assert len(ids) == 1


def test_tournament_does_not_activate_any_model():
    reg = _registry_with_models()
    t = ModelTournament(registry=reg)
    assert reg.active_for_role(ModelRole.CODER) is None  # nothing active before
    t.run(_cfg(domains=("coder",)))
    # A tournament recommends but never assigns — still nothing active.
    assert reg.active_for_role(ModelRole.CODER) is None


def test_empty_registry_yields_empty_report():
    t = ModelTournament(registry=ModelRegistry())
    report = t.run(_cfg())
    assert report.participants == () and report.recommendation.by_domain == {}


def test_custom_weights_are_used():
    # Zero out safety weight → the fast unsafe model can now win (proves weights wired).
    weights = ScoringWeights(domain=0.6, injection_resistance=0.0, citation_validity=0.0,
                             overall=0.2, latency_penalty=0.2, resource_penalty=0.0)
    unsafe_fast = _snap(0.8, metrics={"injection_resistance": 0.4}, domains={"coder": 0.9}, latency=1.0)
    safe = _snap(0.8, metrics={"injection_resistance": 1.0}, domains={"coder": 0.9}, latency=3.0)
    report = ModelTournament(weights=weights).run(
        _cfg(domains=("coder",)), {"unsafe_fast": unsafe_fast, "safe": safe},
        participant_ids=["unsafe_fast", "safe"])
    assert report.leaderboards[0].winner == "unsafe_fast"
