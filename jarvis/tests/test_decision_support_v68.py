"""tests/test_decision_support_v68.py — V68 M43 transparent operator decision support.

Proves the advisory is transparent, ordinal, and never acts:
  * ordinal only — dimensions are LOW/MED/HIGH, never fabricated decimals;
  * transparent heuristic — a safe, high-info, reversible diagnostic outranks a risky,
    high-impact, irreversible remediation;
  * conservative unknowns — UNKNOWN risk/impact are treated as MED (never assumed safe),
    UNKNOWN benefits as LOW (never assumed valuable);
  * honesty about ties — a near-tie surfaces "no clear winner", not a false pick;
  * flags — high-risk/low-reversibility and authorization-required options are flagged;
  * NEVER auto-executes — the advisory's auto_execute is always False, operator action is
    always required, and the module exposes no execution path;
  * bounded + ASCII output.

Deterministic and pure; no control-plane calls, no network.
"""
from __future__ import annotations

from core import decision_support as ds
from core.decision_support import (
    AUTO_EXECUTE,
    DecisionOption,
    Level,
    rank_options,
)


def _opt(oid, **kw):
    return DecisionOption(option_id=oid, title=kw.pop("title", oid), **kw)


SAFE_DIAGNOSTIC = dict(risk=Level.LOW, impact=Level.LOW, reversibility=Level.HIGH,
                       info_gain=Level.HIGH, uncertainty_reduction=Level.HIGH)
RISKY_REMEDIATION = dict(risk=Level.HIGH, impact=Level.HIGH, reversibility=Level.LOW,
                         info_gain=Level.LOW, uncertainty_reduction=Level.MED)


class TestRanking:
    def test_safe_high_info_outranks_risky_remediation(self):
        adv = rank_options([_opt("remediate", **RISKY_REMEDIATION),
                            _opt("diagnose", **SAFE_DIAGNOSTIC)])
        assert adv.top.option_id == "diagnose"
        assert adv.ranked[0].score() > adv.ranked[1].score()

    def test_scores_are_integers_not_fake_precision(self):
        o = _opt("d", **SAFE_DIAGNOSTIC)
        assert isinstance(o.score(), int)
        for dim in ("risk", "impact", "reversibility", "info_gain", "uncertainty_reduction"):
            assert o.to_dict()[dim] in ("low", "med", "high", "unknown")


class TestConservativeUnknowns:
    def test_unknown_risk_treated_as_med_not_safe(self):
        known_low = _opt("a", risk=Level.LOW, impact=Level.LOW, reversibility=Level.HIGH,
                         info_gain=Level.MED, uncertainty_reduction=Level.MED)
        unknown = _opt("b", reversibility=Level.HIGH, info_gain=Level.MED,
                       uncertainty_reduction=Level.MED)   # risk/impact UNKNOWN -> MED
        # the option with explicitly LOW risk should score higher than the UNKNOWN one
        assert known_low.score() > unknown.score()

    def test_unknown_option_is_flagged(self):
        assert "risk/impact not fully assessed" in _opt("b").flags()


class TestHonestyAboutTies:
    def test_near_tie_surfaces_no_clear_winner(self):
        a = _opt("a", risk=Level.LOW, impact=Level.LOW, reversibility=Level.MED,
                 info_gain=Level.MED, uncertainty_reduction=Level.MED)
        b = _opt("b", risk=Level.LOW, impact=Level.MED, reversibility=Level.MED,
                 info_gain=Level.MED, uncertainty_reduction=Level.MED)   # score diff == 1
        adv = rank_options([a, b])
        assert adv.no_clear_winner is True
        assert "NO CLEAR WINNER" in adv.render()

    def test_clear_winner_not_flagged(self):
        adv = rank_options([_opt("remediate", **RISKY_REMEDIATION),
                            _opt("diagnose", **SAFE_DIAGNOSTIC)])
        assert adv.no_clear_winner is False


class TestFlagsAndAuthorization:
    def test_high_risk_low_reversibility_flagged(self):
        assert "high-risk / low-reversibility" in _opt("r", **RISKY_REMEDIATION).flags()

    def test_authorization_flagged(self):
        o = _opt("r", risk=Level.MED, impact=Level.MED, reversibility=Level.MED,
                 info_gain=Level.MED, uncertainty_reduction=Level.MED,
                 requires_authorization=True)
        assert "requires HITL / NATO-OTP authorization" in o.flags()


class TestNeverAutoExecutes:
    def test_module_declares_no_auto_execute(self):
        assert AUTO_EXECUTE is False

    def test_advisory_never_auto_executes(self):
        adv = rank_options([_opt("diagnose", **SAFE_DIAGNOSTIC)])
        d = adv.to_dict()
        assert d["auto_execute"] is False
        assert d["operator_action_required"] is True
        assert "ADVISORY ONLY" in d["advisory"]

    def test_no_execution_method_exposed(self):
        # The module must not expose anything that runs an option.
        forbidden = {"execute", "run", "apply", "perform", "dispatch"}
        assert not (forbidden & {n.lower() for n in dir(ds)})


class TestBoundedAscii:
    def test_render_ascii_and_dict_bounded(self):
        opts = [_opt(f"o{i}", risk=Level.MED, impact=Level.MED, reversibility=Level.MED,
                     info_gain=Level.MED, uncertainty_reduction=Level.MED)
                for i in range(64)]
        adv = rank_options(opts)
        assert len(adv.to_dict()["options"]) <= 32
        assert adv.render().isascii()
