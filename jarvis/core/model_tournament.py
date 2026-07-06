"""
core/model_tournament.py — V65: bounded, domain-aware model benchmark tournament.

Evaluates registered models on the **same** eval basis and produces *empirical,
reviewable* routing recommendations — it never re-routes production by itself.
Promotion/activation remains governed solely by the Model Registry's
evaluation-gated promotion policy; a tournament only *recommends*.

Design constraints (V65 performance + safety):
  * **Bounded** — at most `max_participants`; never an unbounded sweep.
  * **Shared eval set** — every participant is scored on the same datasets
    (snapshots injected or produced one-model-at-a-time by an injected
    `evaluate_fn`; the tournament itself never loads N models at once).
  * **Deterministic** — ranking on fixed snapshots is stable (score desc, then
    model_id asc); no `Math.random`/wallclock in scoring.
  * **Domain-aware** — a per-domain leaderboard, plus a latency/resource penalty,
    so "best overall" never silently wins a domain it is weak in.
  * **No automatic activation** — `RoutingRecommendation` is advisory; applying it
    is a separate, registry-gated human step.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from loguru import logger

from core.model_registry import ModelEvaluationSnapshot, ModelStatus

TOURNAMENT_VERSION = "v65.tournament"


# ── scoring weights ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScoringWeights:
    """How a composite domain score is formed. Safety dimensions are weighted so a
    fast-but-unsafe model cannot top a leaderboard on speed alone."""

    domain: float = 0.5
    injection_resistance: float = 0.2
    citation_validity: float = 0.15
    overall: float = 0.15
    latency_penalty: float = 0.15      # scaled by normalized latency
    resource_penalty: float = 0.1      # scaled by normalized resource use


@dataclass(frozen=True)
class TournamentConfig:
    tournament_id: str
    eval_set_id: str                          # the shared eval basis (dataset/run family)
    domains: tuple[str, ...] = ()             # domains to build leaderboards for
    max_participants: int = 8
    weights: ScoringWeights = field(default_factory=ScoringWeights)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class TournamentParticipant:
    model_id: str
    snapshot: ModelEvaluationSnapshot

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "snapshot": self.snapshot.to_dict()}


@dataclass(frozen=True)
class DomainLeaderboard:
    domain: str
    ranked: tuple[tuple[str, float], ...]     # (model_id, score) desc

    @property
    def winner(self) -> str | None:
        return self.ranked[0][0] if self.ranked else None

    def to_dict(self) -> dict:
        return {"domain": self.domain, "winner": self.winner,
                "ranked": [{"model_id": m, "score": round(s, 4)} for m, s in self.ranked]}


@dataclass(frozen=True)
class RoutingRecommendation:
    """Advisory only — never auto-applied. Maps a domain (and, where derivable, a
    ModelRole) to the empirically strongest participant."""

    by_domain: dict[str, str]
    by_role: dict[str, str]
    reviewed: bool = False

    def to_dict(self) -> dict:
        return {"by_domain": dict(self.by_domain), "by_role": dict(self.by_role),
                "reviewed": self.reviewed, "auto_applied": False}


@dataclass(frozen=True)
class TournamentReport:
    tournament_id: str
    eval_set_id: str
    participants: tuple[str, ...]
    leaderboards: tuple[DomainLeaderboard, ...]
    recommendation: RoutingRecommendation
    dropped_participants: int = 0
    created_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tournament_id": self.tournament_id, "eval_set_id": self.eval_set_id,
            "participants": list(self.participants),
            "leaderboards": [lb.to_dict() for lb in self.leaderboards],
            "recommendation": self.recommendation.to_dict(),
            "dropped_participants": self.dropped_participants,
            "version": TOURNAMENT_VERSION, "created_ts": self.created_ts,
        }


# ── the tournament ────────────────────────────────────────────────────────────
class ModelTournament:
    """Runs a bounded, deterministic domain tournament over injected snapshots and
    emits reviewable routing recommendations. Never mutates the registry."""

    def __init__(self, registry=None, *, weights: ScoringWeights | None = None) -> None:
        self._registry = registry
        self.weights = weights or ScoringWeights()

    # ── participant discovery (bounded) ──────────────────────────────────────
    def select_participants(self, config: TournamentConfig) -> list[str]:
        """Discover evaluated models from the registry, bounded to
        ``max_participants`` in a deterministic order. Only models carrying an
        evaluation snapshot are eligible."""
        if self._registry is None:
            return []
        eligible = sorted(
            m.model_id for m in self._registry.all()
            if m.status in (ModelStatus.EVALUATED, ModelStatus.CANDIDATE,
                            ModelStatus.ACTIVE, ModelStatus.PROMOTED)
            and m.evaluation_snapshot is not None
        )
        return eligible[: config.max_participants]

    # ── composite domain score ───────────────────────────────────────────────
    def _score(self, snap: ModelEvaluationSnapshot, domain: str,
               *, max_latency: float, max_resource: float) -> float:
        w = self.weights
        base = (
            w.domain * snap.domain(domain)
            + w.injection_resistance * snap.metric("injection_resistance", 1.0)
            + w.citation_validity * snap.metric("citation_validity", 1.0)
            + w.overall * snap.pass_rate
        )
        lat_pen = w.latency_penalty * (snap.latency_s / max_latency if max_latency > 0 else 0.0)
        res_pen = w.resource_penalty * (snap.resource_gb / max_resource if max_resource > 0 else 0.0)
        return round(base - lat_pen - res_pen, 6)

    # ── run ──────────────────────────────────────────────────────────────────
    def run(
        self, config: TournamentConfig,
        snapshots: dict[str, ModelEvaluationSnapshot] | None = None, *,
        participant_ids: list[str] | None = None, now_ts: float = 0.0,
    ) -> TournamentReport:
        """Score every participant on the shared eval basis and build per-domain
        leaderboards + a routing recommendation. ``snapshots`` supplies each
        model's evaluation (produced one-at-a-time upstream); when omitted, they
        are read from the registry records."""
        ids = participant_ids if participant_ids is not None else self.select_participants(config)
        # Bound participants (deterministic order preserved).
        bounded = ids[: config.max_participants]
        dropped = max(0, len(ids) - len(bounded))
        if dropped:
            logger.warning(f"TOURNAMENT {config.tournament_id}: bounded to "
                           f"{config.max_participants} participants ({dropped} dropped)")

        snaps: dict[str, ModelEvaluationSnapshot] = {}
        for mid in bounded:
            snap = (snapshots or {}).get(mid)
            if snap is None and self._registry is not None:
                rec = self._registry.get(mid)
                snap = rec.evaluation_snapshot if rec else None
            if snap is not None:
                snaps[mid] = snap

        max_latency = max((s.latency_s for s in snaps.values()), default=0.0)
        max_resource = max((s.resource_gb for s in snaps.values()), default=0.0)

        domains = config.domains or tuple(sorted(
            {d for s in snaps.values() for d in s.domain_pass_rates}
        )) or ("overall",)

        leaderboards: list[DomainLeaderboard] = []
        by_domain: dict[str, str] = {}
        for domain in domains:
            scored = [
                (mid, self._score(s, domain, max_latency=max_latency, max_resource=max_resource))
                for mid, s in snaps.items()
            ]
            # Deterministic: score desc, then model_id asc.
            scored.sort(key=lambda t: (-t[1], t[0]))
            lb = DomainLeaderboard(domain=domain, ranked=tuple(scored))
            leaderboards.append(lb)
            if lb.winner:
                by_domain[domain] = lb.winner

        recommendation = RoutingRecommendation(
            by_domain=by_domain, by_role=self._roles_from_domains(by_domain),
        )
        report = TournamentReport(
            tournament_id=config.tournament_id, eval_set_id=config.eval_set_id,
            participants=tuple(snaps.keys()), leaderboards=tuple(leaderboards),
            recommendation=recommendation, dropped_participants=dropped, created_ts=now_ts,
        )
        logger.info(f"TOURNAMENT {config.tournament_id}: {len(snaps)} participant(s), "
                    f"winners={by_domain} (advisory — not applied)")
        return report

    def _roles_from_domains(self, by_domain: dict[str, str]) -> dict[str, str]:
        """Map winning domains to preferred ModelRoles via the M15 skill profiles
        (advisory). Best-effort — never blocks the tournament."""
        out: dict[str, str] = {}
        try:
            from core.skill_profiles import get_skill_registry
            from core.task_domain import TaskDomain
            reg = get_skill_registry()
            for domain_str, model_id in by_domain.items():
                try:
                    domain = TaskDomain(domain_str)
                except ValueError:
                    continue
                prof = reg.for_domain(domain)
                if prof is not None:
                    out[prof.preferred_model_role.value] = model_id
        except Exception:  # noqa: BLE001 — recommendation is advisory
            pass
        return out
