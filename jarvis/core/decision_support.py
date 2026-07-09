"""core/decision_support.py — V68 M43: transparent operator decision support.

Given a set of candidate next actions, this lays their trade-offs side by side so the
OPERATOR can choose. It is advisory only. There is deliberately no execution path in this
module: it ranks and explains, it never acts, and it never auto-selects the top option.

Every option is scored on five ORDINAL dimensions — LOW / MED / HIGH — never invented
decimals. Fake precision ("0.83 confidence") would launder a guess into authority; an
ordinal band states exactly as much as is known and no more:

  RISK                    harm if the action goes wrong          (higher = worse)
  IMPACT                  operational blast radius / disruption  (higher = worse)
  REVERSIBILITY           can it be undone                       (higher = better)
  INFO_GAIN               what we learn by doing it              (higher = better)
  UNCERTAINTY_REDUCTION   how much current unknowns it resolves  (higher = better)

The ordering is a transparent heuristic (favour learning + reversibility, penalise risk +
impact) with the formula visible in code — not a black box. When the top two options are
close, that is surfaced as "no clear winner: operator judgment required" rather than hidden
behind a ranking. High-risk / low-reversibility options and any action requiring HITL /
NATO-OTP authorization are flagged prominently. Reasoning freedom is not execution
authority: this module informs a decision, the operator (and the guarded control plane)
make it.

Deterministic and pure over its inputs; bounded; ASCII output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

_MAX_OPTIONS = 32

# This module NEVER executes. The constant documents (and lets tests assert) the contract.
AUTO_EXECUTE = False


class Level(str, Enum):
    LOW = "low"
    MED = "med"
    HIGH = "high"
    UNKNOWN = "unknown"       # explicit: not assessed (treated conservatively)


_ORD = {Level.LOW: 1, Level.MED: 2, Level.HIGH: 3, Level.UNKNOWN: 0}


def _lvl(v) -> Level:
    if isinstance(v, Level):
        return v
    try:
        return Level(str(v).lower())
    except ValueError:
        return Level.UNKNOWN


@dataclass
class DecisionOption:
    """One candidate action. Descriptive only — carries no capability to run anything."""
    option_id: str
    title: str
    risk: Level = Level.UNKNOWN
    impact: Level = Level.UNKNOWN
    reversibility: Level = Level.UNKNOWN
    info_gain: Level = Level.UNKNOWN
    uncertainty_reduction: Level = Level.UNKNOWN
    rationale: str = ""
    requires_authorization: bool = False     # HITL / NATO-OTP gate on the control plane
    prerequisites: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def _norm(self) -> "DecisionOption":
        self.risk = _lvl(self.risk); self.impact = _lvl(self.impact)
        self.reversibility = _lvl(self.reversibility); self.info_gain = _lvl(self.info_gain)
        self.uncertainty_reduction = _lvl(self.uncertainty_reduction)
        return self

    # Transparent heuristic: reward learning + reversibility, penalise risk + impact.
    # UNKNOWN risk/impact are treated as MED (conservative — never assumed safe);
    # UNKNOWN benefits are treated as LOW (never assumed valuable).
    def score(self) -> int:
        risk = _ORD[self.risk] or _ORD[Level.MED]
        impact = _ORD[self.impact] or _ORD[Level.MED]
        rev = _ORD[self.reversibility] or _ORD[Level.LOW]
        info = _ORD[self.info_gain] or _ORD[Level.LOW]
        unc = _ORD[self.uncertainty_reduction] or _ORD[Level.LOW]
        return (info + unc + rev) - (risk + impact)

    def flags(self) -> list[str]:
        f: list[str] = []
        if _lvl(self.risk) is Level.HIGH and _lvl(self.reversibility) is Level.LOW:
            f.append("high-risk / low-reversibility")
        if self.risk is Level.UNKNOWN or self.impact is Level.UNKNOWN:
            f.append("risk/impact not fully assessed")
        if self.requires_authorization:
            f.append("requires HITL / NATO-OTP authorization")
        return f

    def to_dict(self) -> dict:
        return {
            "option_id": self.option_id, "title": self.title[:160],
            "risk": self.risk.value, "impact": self.impact.value,
            "reversibility": self.reversibility.value, "info_gain": self.info_gain.value,
            "uncertainty_reduction": self.uncertainty_reduction.value,
            "score": self.score(), "flags": self.flags(),
            "requires_authorization": self.requires_authorization,
            "prerequisites": list(self.prerequisites)[:8],
            "rationale": self.rationale[:240],
        }


@dataclass
class DecisionAdvisory:
    ranked: list[DecisionOption] = field(default_factory=list)
    no_clear_winner: bool = False

    # The contract, made explicit and machine-checkable.
    auto_execute: bool = False
    operator_action_required: bool = True
    method: str = ("ordinal heuristic: (info_gain + uncertainty_reduction + reversibility) "
                   "- (risk + impact); advisory only, never auto-executed")

    @property
    def top(self) -> DecisionOption | None:
        return self.ranked[0] if self.ranked else None

    def to_dict(self) -> dict:
        return {
            "panel": "decision_support",
            "auto_execute": False,                 # invariant: always False
            "operator_action_required": True,
            "no_clear_winner": self.no_clear_winner,
            "method": self.method,
            "advisory": ("ADVISORY ONLY - the operator selects and authorizes; the "
                         "top-ranked option is NOT auto-executed"),
            "options": [o.to_dict() for o in self.ranked[:_MAX_OPTIONS]],
        }

    def render(self) -> str:
        out = ["OPERATOR DECISION SUPPORT (advisory only - no action is taken)"]
        for i, o in enumerate(self.ranked, 1):
            out.append(
                f"{i}. {o.title}  [score {o.score()}]  "
                f"risk={o.risk.value} impact={o.impact.value} rev={o.reversibility.value} "
                f"info={o.info_gain.value} unc={o.uncertainty_reduction.value}")
            for fl in o.flags():
                out.append(f"     ! {fl}")
        if self.no_clear_winner:
            out.append("")
            out.append("NO CLEAR WINNER: top options are close - operator judgment required.")
        out.append("")
        out.append("The operator selects and authorizes. Nothing here is executed.")
        return "\n".join(out)


def rank_options(options: list[DecisionOption]) -> DecisionAdvisory:
    """Rank candidate actions by the transparent ordinal heuristic. Stable, deterministic,
    and non-executing. Ties / near-ties are surfaced, never silently broken in a way that
    implies false confidence."""
    norm = [o._norm() for o in options[:_MAX_OPTIONS]]
    # Sort by score desc; tie-break deterministically by (lower risk, higher reversibility,
    # then option_id) so ordering is stable — but a near-tie is reported as such.
    ranked = sorted(
        norm,
        key=lambda o: (-o.score(), _ORD[o.risk] or 2, -(_ORD[o.reversibility] or 1),
                       o.option_id))
    no_clear = False
    if len(ranked) >= 2 and (ranked[0].score() - ranked[1].score()) <= 1:
        no_clear = True
    return DecisionAdvisory(ranked=ranked, no_clear_winner=no_clear)


def option_from_runbook(runbook, *, risk: Level = Level.UNKNOWN,
                        impact: Level = Level.UNKNOWN,
                        reversibility: Level = Level.UNKNOWN,
                        info_gain: Level = Level.UNKNOWN,
                        uncertainty_reduction: Level = Level.UNKNOWN,
                        requires_authorization: bool = True) -> DecisionOption:
    """Build a decision option from a runbook definition (dict or object). Defaults are
    conservative: a runbook is assumed to require authorization unless stated otherwise —
    this NEVER implies the runbook will be run here; it only describes the option."""
    rid = getattr(runbook, "runbook_id", None) or (
        runbook.get("runbook_id") if isinstance(runbook, dict) else None) or "runbook"
    name = getattr(runbook, "name", None) or (
        runbook.get("name") if isinstance(runbook, dict) else None) or str(rid)
    return DecisionOption(
        option_id=str(rid), title=str(name), risk=risk, impact=impact,
        reversibility=reversibility, info_gain=info_gain,
        uncertainty_reduction=uncertainty_reduction,
        requires_authorization=requires_authorization)
