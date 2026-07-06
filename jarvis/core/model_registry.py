"""
core/model_registry.py — V65: evaluation-gated Model Registry + promotion/rollback.

The registry is the system of record for model artifacts and — critically — the
**only** path by which a model becomes ACTIVE for a role. A model is never
promoted because "it feels smarter": promotion is an evidence-based comparison of
a candidate's evaluation snapshot against the current baseline for that role,
governed by a fail-closed `PromotionPolicy`.

Design pillars (V65 non-negotiables):
  * **No promotion without evaluation.** A model must carry a
    `ModelEvaluationSnapshot` (built from a real M14 `EvalRun`) before it can be a
    promotion candidate.
  * **Critical regressions block, always.** A drop on any safety dimension
    (injection resistance, tool safety, forbidden-output, verification) blocks
    promotion regardless of gains elsewhere.
  * **Role-specific promotion.** Assignments are per `ModelRole`; a coder
    candidate can win the CODER role without displacing GENERAL. One model need
    not win every domain.
  * **Reversible.** Every activation records a `RollbackPointer` to the model it
    replaced; `rollback(role)` restores it. Promotion history is append-only and
    never rewritten — regressions are never hidden.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from loguru import logger

from core.model_router import ModelRole

REGISTRY_VERSION = "v65.registry"


# ── lifecycle / decision status ───────────────────────────────────────────────
class ModelStatus(str, Enum):
    EXPERIMENTAL = "experimental"   # registered, not yet evaluated
    EVALUATED = "evaluated"         # carries an evaluation snapshot
    CANDIDATE = "candidate"         # proposed for a role
    PROMOTED = "promoted"           # won a promotion decision
    ACTIVE = "active"               # assigned to a role in production
    DEPRECATED = "deprecated"       # superseded
    ARCHIVED = "archived"           # retired / rejected

    @property
    def is_live(self) -> bool:
        return self is ModelStatus.ACTIVE


class PromotionStatus(str, Enum):
    PROMOTED = "promoted"
    REJECTED = "rejected"
    HELD = "held"           # inconclusive — needs more evidence / human review


# ── artifact ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelArtifact:
    """A base model reference or an adapter on disk, with an integrity hash."""

    kind: str                       # "base" | "adapter"
    path: str
    content_hash: str = ""
    size_bytes: int = 0
    quantization: str = "none"

    def resolve(self) -> Path:
        return Path(self.path)

    def compute_hash(self) -> str:
        """Deterministic sha256 over the artifact bytes (file) or the sorted
        (relpath, bytes) of a directory. Empty string if nothing exists."""
        p = self.resolve()
        if not p.exists():
            return ""
        h = hashlib.sha256()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    h.update(f.relative_to(p).as_posix().encode("utf-8", "ignore"))
                    h.update(f.read_bytes())
        else:
            h.update(p.read_bytes())
        return h.hexdigest()

    def verify_integrity(self) -> bool:
        """True only if a stored hash exists and matches the bytes on disk."""
        return bool(self.content_hash) and self.compute_hash() == self.content_hash

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelArtifact":
        return cls(kind=str(d.get("kind", "adapter")), path=str(d.get("path", "")),
                   content_hash=str(d.get("content_hash", "")),
                   size_bytes=int(d.get("size_bytes", 0)),
                   quantization=str(d.get("quantization", "none")))


# ── evaluation snapshot (the measured evidence) ───────────────────────────────
@dataclass(frozen=True)
class ModelEvaluationSnapshot:
    run_id: str
    pass_rate: float
    mean_score: float
    metric_pass_rates: dict[str, float] = field(default_factory=dict)
    domain_pass_rates: dict[str, float] = field(default_factory=dict)
    skill_profile_scores: dict[str, float] = field(default_factory=dict)
    latency_s: float = 0.0
    resource_gb: float = 0.0
    created_ts: float = 0.0

    def metric(self, dim: str, default: float = 0.0) -> float:
        return float(self.metric_pass_rates.get(dim, default))

    def domain(self, dom: str, default: float = 0.0) -> float:
        return float(self.domain_pass_rates.get(dom, default))

    def domain_avg(self, domains: tuple[str, ...]) -> float:
        vals = [self.domain(d) for d in domains if d in self.domain_pass_rates]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEvaluationSnapshot":
        return cls(
            run_id=str(d.get("run_id", "")), pass_rate=float(d.get("pass_rate", 0.0)),
            mean_score=float(d.get("mean_score", 0.0)),
            metric_pass_rates=dict(d.get("metric_pass_rates", {})),
            domain_pass_rates=dict(d.get("domain_pass_rates", {})),
            skill_profile_scores=dict(d.get("skill_profile_scores", {})),
            latency_s=float(d.get("latency_s", 0.0)), resource_gb=float(d.get("resource_gb", 0.0)),
            created_ts=float(d.get("created_ts", 0.0)),
        )

    @classmethod
    def from_eval_run(cls, run, *, latency_s: float = 0.0, resource_gb: float = 0.0,
                      skill_profile_scores: dict[str, float] | None = None,
                      now_ts: float = 0.0) -> "ModelEvaluationSnapshot":
        return cls(
            run_id=getattr(run, "run_id", ""), pass_rate=run.pass_rate,
            mean_score=run.mean_score, metric_pass_rates=dict(run.metric_pass_rates()),
            domain_pass_rates=dict(run.domain_pass_rates()),
            skill_profile_scores=dict(skill_profile_scores or {}),
            latency_s=latency_s, resource_gb=resource_gb, created_ts=now_ts,
        )


# ── promotion decision / policy ───────────────────────────────────────────────
@dataclass(frozen=True)
class PromotionDecision:
    candidate_id: str
    baseline_id: str | None
    role: ModelRole
    status: PromotionStatus
    reasons: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    improvements: tuple[str, ...] = ()
    target_delta: float = 0.0
    created_ts: float = 0.0

    @property
    def promoted(self) -> bool:
        return self.status is PromotionStatus.PROMOTED

    def to_dict(self) -> dict:
        d = asdict(self)
        d["role"] = self.role.value
        d["status"] = self.status.value
        for k in ("reasons", "regressions", "improvements"):
            d[k] = list(getattr(self, k))
        return d


@dataclass(frozen=True)
class PromotionPolicy:
    """Evaluation-gated promotion. Safety dimensions may never regress; the target
    must improve enough; overall pass-rate may not fall past the regression
    budget. Tradeoffs outside the target are allowed within budget."""

    critical_dimensions: tuple[str, ...] = (
        "injection_resistance", "tool_safety", "forbidden_output",
        "injection_detection", "verification", "citation_validity",
    )
    tolerance: float = 0.001
    regression_budget: float = 0.02        # max tolerated overall pass-rate drop
    min_target_improvement: float = 0.0    # target must improve by at least this

    def evaluate(
        self, baseline: ModelEvaluationSnapshot | None, candidate: ModelEvaluationSnapshot, *,
        role: ModelRole, target_domains: tuple[str, ...] = (), target_metric: str | None = None,
        now_ts: float = 0.0,
    ) -> PromotionDecision:
        regressions: list[str] = []
        improvements: list[str] = []
        reasons: list[str] = []

        # First model for a role (no baseline) is promotable if it was evaluated.
        if baseline is None:
            return PromotionDecision(
                candidate_id="", baseline_id=None, role=role,
                status=PromotionStatus.PROMOTED, reasons=("no baseline — first model for role",),
                target_delta=candidate.pass_rate, created_ts=now_ts,
            )

        # 1) Critical safety dimensions must not regress at all.
        for dim in self.critical_dimensions:
            b, c = baseline.metric(dim), candidate.metric(dim)
            if dim not in baseline.metric_pass_rates and dim not in candidate.metric_pass_rates:
                continue
            if c < b - self.tolerance:
                regressions.append(f"critical:{dim}:{c - b:+.3f}")
            elif c > b + self.tolerance:
                improvements.append(f"{dim}:{c - b:+.3f}")

        # 2) Overall pass-rate may not fall past the regression budget.
        overall_delta = candidate.pass_rate - baseline.pass_rate
        if overall_delta < -self.regression_budget:
            regressions.append(f"overall_pass_rate:{overall_delta:+.3f} exceeds "
                               f"budget {self.regression_budget}")

        # 3) The target must improve enough (role-specific — not every domain).
        if target_domains:
            target_delta = candidate.domain_avg(target_domains) - baseline.domain_avg(target_domains)
            target_desc = f"domains {list(target_domains)}"
        elif target_metric:
            target_delta = candidate.metric(target_metric) - baseline.metric(target_metric)
            target_desc = f"metric {target_metric}"
        else:
            target_delta = overall_delta
            target_desc = "overall pass rate"
        target_ok = target_delta >= self.min_target_improvement - self.tolerance
        if not target_ok:
            reasons.append(f"target ({target_desc}) improved {target_delta:+.3f} < "
                           f"required {self.min_target_improvement}")
        elif target_delta > self.tolerance:
            improvements.append(f"target:{target_desc}:{target_delta:+.3f}")

        if regressions:
            reasons.append("blocked by regression(s)")
            status = PromotionStatus.REJECTED
        elif not target_ok:
            status = PromotionStatus.REJECTED
        else:
            status = PromotionStatus.PROMOTED
            reasons.append("no critical regression; target improvement satisfied")

        return PromotionDecision(
            candidate_id="", baseline_id=None, role=role, status=status,
            reasons=tuple(reasons), regressions=tuple(regressions),
            improvements=tuple(improvements), target_delta=round(target_delta, 4),
            created_ts=now_ts,
        )


# ── role assignment / rollback ────────────────────────────────────────────────
@dataclass(frozen=True)
class RollbackPointer:
    role: ModelRole
    previous_model_id: str | None
    replaced_by: str
    reason: str = ""
    created_ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["role"] = self.role.value
        return d


@dataclass(frozen=True)
class ModelRoleAssignment:
    role: ModelRole
    model_id: str
    snapshot_run_id: str = ""
    previous_model_id: str | None = None
    assigned_ts: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["role"] = self.role.value
        return d


# ── model record ──────────────────────────────────────────────────────────────
@dataclass
class ModelRecord:
    model_id: str
    base_model: str
    adapter_artifact: ModelArtifact | None = None
    training_run_id: str = ""
    dataset_version: str = ""
    supported_domains: tuple[str, ...] = ()
    quantization: str = "none"
    context_length: int = 4096
    hardware_requirements: dict = field(default_factory=dict)
    skill_profile_scores: dict[str, float] = field(default_factory=dict)
    evaluation_snapshot: ModelEvaluationSnapshot | None = None
    status: ModelStatus = ModelStatus.EXPERIMENTAL
    promotion_history: list[dict] = field(default_factory=list)
    rollback_target: str | None = None
    created_ts: float = 0.0

    @property
    def is_evaluated(self) -> bool:
        return self.evaluation_snapshot is not None

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id, "base_model": self.base_model,
            "adapter_artifact": self.adapter_artifact.to_dict() if self.adapter_artifact else None,
            "training_run_id": self.training_run_id, "dataset_version": self.dataset_version,
            "supported_domains": list(self.supported_domains), "quantization": self.quantization,
            "context_length": self.context_length, "hardware_requirements": self.hardware_requirements,
            "skill_profile_scores": self.skill_profile_scores,
            "evaluation_snapshot": self.evaluation_snapshot.to_dict() if self.evaluation_snapshot else None,
            "status": self.status.value, "promotion_history": list(self.promotion_history),
            "rollback_target": self.rollback_target, "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelRecord":
        art = d.get("adapter_artifact")
        snap = d.get("evaluation_snapshot")
        return cls(
            model_id=str(d["model_id"]), base_model=str(d.get("base_model", "")),
            adapter_artifact=ModelArtifact.from_dict(art) if art else None,
            training_run_id=str(d.get("training_run_id", "")),
            dataset_version=str(d.get("dataset_version", "")),
            supported_domains=tuple(d.get("supported_domains", ())),
            quantization=str(d.get("quantization", "none")),
            context_length=int(d.get("context_length", 4096)),
            hardware_requirements=dict(d.get("hardware_requirements", {})),
            skill_profile_scores=dict(d.get("skill_profile_scores", {})),
            evaluation_snapshot=ModelEvaluationSnapshot.from_dict(snap) if snap else None,
            status=ModelStatus(d.get("status", ModelStatus.EXPERIMENTAL.value)),
            promotion_history=list(d.get("promotion_history", [])),
            rollback_target=d.get("rollback_target"), created_ts=float(d.get("created_ts", 0.0)),
        )


# ── the registry ──────────────────────────────────────────────────────────────
class ModelRegistry:
    """In-memory registry with JSON persistence. The single owner of model
    lifecycle transitions and role assignments."""

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self._models: dict[str, ModelRecord] = {}
        self._assignments: dict[ModelRole, ModelRoleAssignment] = {}
        self._rollbacks: dict[ModelRole, list[RollbackPointer]] = {}
        self.policy = policy or PromotionPolicy()

    # ── registration ─────────────────────────────────────────────────────────
    def register(self, record: ModelRecord, *, allow_replace: bool = False) -> ModelRecord:
        """Register a model. A duplicate ``model_id`` is refused unless
        ``allow_replace`` — versions are immutable identities."""
        if record.model_id in self._models and not allow_replace:
            raise ValueError(f"model_id already registered (immutable): {record.model_id}")
        if record.adapter_artifact and record.adapter_artifact.content_hash:
            if not record.adapter_artifact.verify_integrity():
                raise ValueError(f"artifact integrity check failed for {record.model_id}")
        self._models[record.model_id] = record
        logger.info(f"REGISTRY: registered {record.model_id} ({record.status.value})")
        return record

    def get(self, model_id: str) -> ModelRecord | None:
        return self._models.get(model_id)

    def all(self) -> list[ModelRecord]:
        return list(self._models.values())

    def by_status(self, status: ModelStatus) -> list[ModelRecord]:
        return [m for m in self._models.values() if m.status is status]

    # ── evaluation linkage ───────────────────────────────────────────────────
    def attach_evaluation(self, model_id: str, snapshot: ModelEvaluationSnapshot) -> ModelRecord:
        rec = self._require(model_id)
        rec.evaluation_snapshot = snapshot
        if snapshot.skill_profile_scores:
            rec.skill_profile_scores = dict(snapshot.skill_profile_scores)
        if rec.status is ModelStatus.EXPERIMENTAL:
            rec.status = ModelStatus.EVALUATED
        return rec

    # ── role assignment / baseline ───────────────────────────────────────────
    def active_for_role(self, role: ModelRole) -> ModelRecord | None:
        assignment = self._assignments.get(role)
        return self._models.get(assignment.model_id) if assignment else None

    def baseline_snapshot(self, role: ModelRole) -> ModelEvaluationSnapshot | None:
        active = self.active_for_role(role)
        return active.evaluation_snapshot if active else None

    # ── promotion (evidence-gated) ───────────────────────────────────────────
    def propose_promotion(
        self, candidate_id: str, role: ModelRole, *,
        target_domains: tuple[str, ...] = (), target_metric: str | None = None,
        policy: PromotionPolicy | None = None, now_ts: float = 0.0,
    ) -> PromotionDecision:
        """Compare a candidate against the current role baseline. Never mutates
        state — returns a decision the caller can inspect before promoting."""
        cand = self._require(candidate_id)
        if cand.evaluation_snapshot is None:
            return PromotionDecision(
                candidate_id=candidate_id, baseline_id=None, role=role,
                status=PromotionStatus.REJECTED,
                reasons=("candidate has no evaluation snapshot",), created_ts=now_ts,
            )
        baseline = self.active_for_role(role)
        base_snap = baseline.evaluation_snapshot if baseline else None
        decision = (policy or self.policy).evaluate(
            base_snap, cand.evaluation_snapshot, role=role,
            target_domains=target_domains, target_metric=target_metric, now_ts=now_ts,
        )
        # Bind the ids (policy.evaluate is id-agnostic and pure).
        from dataclasses import replace
        return replace(decision, candidate_id=candidate_id,
                       baseline_id=(baseline.model_id if baseline else None))

    def promote(self, decision: PromotionDecision, *, now_ts: float = 0.0) -> ModelRoleAssignment:
        """Activate a candidate for a role — only if the decision approved it.
        Records a rollback pointer to the model it replaced and an append-only
        audit entry. Fail-closed: a non-promoted decision raises."""
        if not decision.promoted:
            raise ValueError(f"refusing to promote: decision is {decision.status.value} "
                             f"({list(decision.regressions) or list(decision.reasons)})")
        cand = self._require(decision.candidate_id)
        if cand.evaluation_snapshot is None:  # defensive — never activate unevaluated
            raise ValueError(f"cannot promote unevaluated model {decision.candidate_id}")

        prev = self._assignments.get(decision.role)
        prev_id = prev.model_id if prev else None
        if prev_id and prev_id in self._models and prev_id != cand.model_id:
            self._models[prev_id].status = ModelStatus.DEPRECATED

        rollback = RollbackPointer(
            role=decision.role, previous_model_id=prev_id, replaced_by=cand.model_id,
            reason="; ".join(decision.improvements) or "promotion", created_ts=now_ts,
        )
        self._rollbacks.setdefault(decision.role, []).append(rollback)

        assignment = ModelRoleAssignment(
            role=decision.role, model_id=cand.model_id,
            snapshot_run_id=cand.evaluation_snapshot.run_id,
            previous_model_id=prev_id, assigned_ts=now_ts,
        )
        self._assignments[decision.role] = assignment
        cand.status = ModelStatus.ACTIVE
        cand.rollback_target = prev_id
        cand.promotion_history.append({
            **decision.to_dict(), "action": "promoted", "previous_model_id": prev_id,
            "assigned_ts": now_ts,
        })
        logger.info(f"REGISTRY: promoted {cand.model_id} → {decision.role.value} "
                    f"(was {prev_id})")
        return assignment

    def reject(self, decision: PromotionDecision, *, now_ts: float = 0.0) -> ModelRecord:
        """Record a rejected candidate (audit preserved, model archived)."""
        cand = self._require(decision.candidate_id)
        cand.promotion_history.append({**decision.to_dict(), "action": "rejected",
                                       "rejected_ts": now_ts})
        cand.status = ModelStatus.ARCHIVED
        return cand

    # ── rollback ─────────────────────────────────────────────────────────────
    def rollback(self, role: ModelRole, *, reason: str = "", now_ts: float = 0.0) -> ModelRoleAssignment | None:
        """Restore the model this role held before the last promotion. Returns the
        restored assignment, or None if there is nothing to roll back to."""
        pointers = self._rollbacks.get(role) or []
        if not pointers:
            return None
        last = pointers[-1]
        current = self._assignments.get(role)
        if last.previous_model_id is None or last.previous_model_id not in self._models:
            # Nothing valid to restore — clear the assignment fail-safe.
            logger.warning(f"REGISTRY: rollback for {role.value} has no valid prior model")
            return None
        restored = self._models[last.previous_model_id]
        restored.status = ModelStatus.ACTIVE
        if current and current.model_id in self._models and current.model_id != restored.model_id:
            self._models[current.model_id].status = ModelStatus.DEPRECATED
            self._models[current.model_id].promotion_history.append(
                {"action": "rolled_back", "role": role.value, "reason": reason,
                 "restored_model_id": restored.model_id, "ts": now_ts})
        assignment = ModelRoleAssignment(
            role=role, model_id=restored.model_id,
            snapshot_run_id=restored.evaluation_snapshot.run_id if restored.evaluation_snapshot else "",
            previous_model_id=(current.model_id if current else None), assigned_ts=now_ts,
        )
        self._assignments[role] = assignment
        pointers.append(RollbackPointer(role=role, previous_model_id=(current.model_id if current else None),
                                        replaced_by=restored.model_id,
                                        reason=f"rollback: {reason}", created_ts=now_ts))
        logger.info(f"REGISTRY: rolled back {role.value} → {restored.model_id}")
        return assignment

    def rollback_history(self, role: ModelRole) -> list[RollbackPointer]:
        return list(self._rollbacks.get(role, []))

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": REGISTRY_VERSION,
            "models": {mid: rec.to_dict() for mid, rec in self._models.items()},
            "assignments": {r.value: a.to_dict() for r, a in self._assignments.items()},
            "rollbacks": {r.value: [p.to_dict() for p in ps] for r, ps in self._rollbacks.items()},
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path, *, policy: PromotionPolicy | None = None) -> "ModelRegistry":
        reg = cls(policy=policy)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for mid, rec in data.get("models", {}).items():
            reg._models[mid] = ModelRecord.from_dict(rec)
        for rname, a in data.get("assignments", {}).items():
            role = ModelRole(rname)
            reg._assignments[role] = ModelRoleAssignment(
                role=role, model_id=str(a.get("model_id", "")),
                snapshot_run_id=str(a.get("snapshot_run_id", "")),
                previous_model_id=a.get("previous_model_id"),
                assigned_ts=float(a.get("assigned_ts", 0.0)),
            )
        for rname, ps in data.get("rollbacks", {}).items():
            role = ModelRole(rname)
            reg._rollbacks[role] = [
                RollbackPointer(role=role, previous_model_id=p.get("previous_model_id"),
                                replaced_by=str(p.get("replaced_by", "")),
                                reason=str(p.get("reason", "")), created_ts=float(p.get("created_ts", 0.0)))
                for p in ps
            ]
        return reg

    # ── internal ─────────────────────────────────────────────────────────────
    def _require(self, model_id: str) -> ModelRecord:
        rec = self._models.get(model_id)
        if rec is None:
            raise KeyError(f"unknown model_id: {model_id}")
        return rec


_REGISTRY: ModelRegistry | None = None


def get_model_registry() -> ModelRegistry:
    """Process-wide registry singleton."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ModelRegistry()
    return _REGISTRY
