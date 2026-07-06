"""
core/specialist_runtime.py — V63 Milestone 4: controlled multi-agent runtime.

Evolves the legacy ``core.agent_orchestrator.AgentOrchestrator`` (5 sequential
cyber specialists, preserved intact) into a *controlled general specialist
runtime*: a bounded, resource-aware, provenance-carrying team executive that the
unified per-turn ``TaskDecision`` (M1) can dispatch to.

Design invariants (from the V63 directive):

  * A specialist is a **capability role**, not a resident model. Every spec
    shares ONE inference client and ONE tool gateway, differing only by role
    instructions, model tier, allowed tool categories, timeout, context budget,
    and memory scope. No 14-model fan-out is ever loaded.
  * **Reasoning freedom ≠ execution authority.** Specialists reason broadly; the
    only way any specialist touches the world is through :class:`ToolBroker`,
    which delegates to ``ToolExecutor.aexecute`` — the same risk-class / HITL /
    audit gate every tool call already passes. There is no ``subprocess``,
    ``os.system``, MCP, or raw-handler path in this module.
  * **Resource-aware & bounded.** Conservative default concurrency (≤2 FAST,
    ≤1 DEEP), a global agent cap, per-task and per-team timeouts, and dynamic
    back-off under CPU/RAM/battery pressure. Never unbounded fan-out.
  * **Bounded blackboard.** Structured, size-capped shared state with dedup,
    provenance, trust labels, timestamps, and structured conflict detection —
    never an unlimited transcript dump.

The inference and resource probes are dependency-injected (a plain async
callable + a sync probe), so the whole runtime is unit-testable with fakes and
requires no Ollama/psutil at test time. ``attach()`` wires the production
implementations (shared LLM client + models).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Protocol, runtime_checkable

from loguru import logger

# The 5 legacy cyber-specialist prompts are preserved verbatim and re-mapped onto
# the controlled runtime (see legacy_spec) — no behavior is deleted.
from core.agent_orchestrator import _AGENT_PROMPTS as _LEGACY_PROMPTS
from core.memory_fabric import MemoryFabric, Sensitivity, get_fabric
from core.task_domain import TaskDomain

# ── Bounds (module-level, conservative for the 15W CPU-bound target host) ─────
_MAX_TOTAL_AGENTS = 4
_MAX_FAST_AGENTS = 2
_MAX_DEEP_AGENTS = 1
_TASK_TIMEOUT_S = 60.0
_TEAM_TIMEOUT_S = 180.0
_CONTEXT_BUDGET = 2048          # per-specialist prompt ctx window
_BLACKBOARD_BUDGET = 4000       # max chars in a context digest handed downstream

# Blackboard list caps — a hard ceiling on shared state so it can never grow
# unbounded across a long-running team.
_BB_MAX_FACTS = 60
_BB_MAX_EVIDENCE = 60
_BB_MAX_HYPOTHESES = 30
_BB_MAX_QUESTIONS = 30
_BB_MAX_DECISIONS = 30
_BB_MAX_CONFLICTS = 20
_BB_MAX_ARTIFACTS = 30
_BB_MAX_REPORTS = _MAX_TOTAL_AGENTS + 2
_DEDUP_PREFIX = 160
_REPORT_SUMMARY_CAP = 1500
_EVIDENCE_CAP = 600

# Resource-pressure ceilings (mirror core.ironman_mode's Rule of Silicon).
_CPU_CEIL_PCT = 85.0
_RAM_CEIL_PCT = 90.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())[:_DEDUP_PREFIX]


# ════════════════════════════════════════════════════════════════════════════
#  Capability model
# ════════════════════════════════════════════════════════════════════════════
class ModelTier(str, Enum):
    """Which resident model a specialist runs on. FAST/DEEP are the two the
    dependency guardian confirms; SYNTHESIS is a fan-in-only DEEP pass; VISION/
    EMBEDDING are on-demand and never part of a reasoning fan-out."""
    FAST = "fast"
    DEEP = "deep"
    VISION = "vision"
    EMBEDDING = "embedding"
    SYNTHESIS = "synthesis"


class ToolCategory(str, Enum):
    """Coarse capability families a spec may be *allowed* to reach. Enforced by
    :class:`ToolBroker` against a tool→category map before any delegation. NONE
    means the specialist is reasoning-only (the safe default)."""
    NONE = "none"
    READ = "read"          # read-only, non-mutating tools
    SYSTEM = "system"      # host/process introspection
    WEB = "web"            # outward HTTP / page fetch
    RECON = "recon"        # active network / OSINT reconnaissance
    CODE = "code"          # code analysis / execution
    FORENSIC = "forensic"  # DFIR / memory / artifact tooling


class SpecialistRole(str, Enum):
    """The 14 capability roles. Purely a *role*; several share a ModelTier."""
    GENERAL = "general"
    RESEARCH = "research"
    CODE = "code"
    ARCHITECT = "architect"
    MATH = "math"
    VISION = "vision"
    LANGUAGE = "language"
    CYBER_BLUE = "cyber_blue"
    CYBER_PURPLE = "cyber_purple"
    DFIR = "dfir"
    GRC = "grc"
    OPERATIONAL = "operational"
    PLANNER = "planner"
    CRITIC = "critic"
    VERIFIER = "verifier"


@dataclass(frozen=True)
class SpecialistSpec:
    """A capability role: role instructions + scoped runtime budget. Immutable."""
    role: SpecialistRole
    tier: ModelTier
    system_prompt: str
    allowed_tools: frozenset[ToolCategory] = frozenset({ToolCategory.NONE})
    timeout_s: float = _TASK_TIMEOUT_S
    context_budget: int = _CONTEXT_BUDGET
    memory_scope: str = "session"   # M5 fabric scope this role may retrieve from
    temperature: float = 0.2

    @property
    def is_deep(self) -> bool:
        return self.tier in (ModelTier.DEEP, ModelTier.SYNTHESIS)


_P = ToolCategory
_SPECIALISTS: dict[SpecialistRole, SpecialistSpec] = {
    SpecialistRole.GENERAL: SpecialistSpec(
        SpecialistRole.GENERAL, ModelTier.FAST,
        "You are a concise, capable general assistant. Answer directly and "
        "flag uncertainty. No fabrication.",
        allowed_tools=frozenset({_P.READ}),
    ),
    SpecialistRole.RESEARCH: SpecialistSpec(
        SpecialistRole.RESEARCH, ModelTier.DEEP,
        "You are a rigorous research specialist. Gather, compare, and cite "
        "sources; separate confirmed fact from inference; surface open "
        "questions. Never invent citations.",
        allowed_tools=frozenset({_P.READ, _P.WEB}),
        memory_scope="project",
    ),
    SpecialistRole.CODE: SpecialistSpec(
        SpecialistRole.CODE, ModelTier.DEEP,
        "You are an expert software engineer. Produce correct, typed, minimal "
        "code and precise diagnoses. State assumptions about repo state "
        "explicitly.",
        allowed_tools=frozenset({_P.READ, _P.CODE}),
    ),
    SpecialistRole.ARCHITECT: SpecialistSpec(
        SpecialistRole.ARCHITECT, ModelTier.DEEP,
        "You are a principal systems architect. Evaluate tradeoffs, Big-O, "
        "failure modes, and async edge-cases. Recommend, do not hand-wave.",
        allowed_tools=frozenset({_P.READ}),
        memory_scope="project",
    ),
    SpecialistRole.MATH: SpecialistSpec(
        SpecialistRole.MATH, ModelTier.DEEP,
        "You are a careful mathematician. Show the derivation, state each step, "
        "and verify the result. Do not skip algebra.",
    ),
    SpecialistRole.VISION: SpecialistSpec(
        SpecialistRole.VISION, ModelTier.VISION,
        "You are a visual-analysis specialist. Describe only what is present in "
        "the provided image/OCR context; never hallucinate unseen detail.",
        allowed_tools=frozenset({_P.READ}),
    ),
    SpecialistRole.LANGUAGE: SpecialistSpec(
        SpecialistRole.LANGUAGE, ModelTier.FAST,
        "You are a precise language specialist: translation, grammar, tone, "
        "summarization. Preserve meaning exactly.",
    ),
    SpecialistRole.CYBER_BLUE: SpecialistSpec(
        SpecialistRole.CYBER_BLUE, ModelTier.DEEP,
        "You are a Blue Team detection engineer. Map behavior to MITRE ATT&CK, "
        "propose detections (Sigma/YARA), and defensive countermeasures. "
        "Analysis and defense only.",
        allowed_tools=frozenset({_P.READ, _P.FORENSIC}),
    ),
    SpecialistRole.CYBER_PURPLE: SpecialistSpec(
        SpecialistRole.CYBER_PURPLE, ModelTier.DEEP,
        "You are a Purple Team operator. Reason freely about offensive "
        "techniques, adversary emulation, and detection validation. Any action "
        "requires authorized scope and passes the tool gateway — never assume "
        "authorization.",
        allowed_tools=frozenset({_P.READ, _P.RECON}),
    ),
    SpecialistRole.DFIR: SpecialistSpec(
        SpecialistRole.DFIR, ModelTier.DEEP,
        "You are a DFIR analyst. Build evidence-backed timelines, extract IOCs, "
        "and recommend containment. Every conclusion must cite its evidence.",
        allowed_tools=frozenset({_P.READ, _P.FORENSIC}),
        memory_scope="project",
    ),
    SpecialistRole.GRC: SpecialistSpec(
        SpecialistRole.GRC, ModelTier.DEEP,
        "You are a GRC specialist. Map findings to controls/frameworks (NIST, "
        "ISO 27001, SOC 2) with explicit provenance and evidence requirements.",
        allowed_tools=frozenset({_P.READ}),
        memory_scope="project",
    ),
    SpecialistRole.OPERATIONAL: SpecialistSpec(
        SpecialistRole.OPERATIONAL, ModelTier.FAST,
        "You are a local systems operator. Diagnose host/app state and propose "
        "concrete, reversible operational steps. Never execute destructive "
        "actions without approval.",
        allowed_tools=frozenset({_P.READ, _P.SYSTEM}),
    ),
    SpecialistRole.PLANNER: SpecialistSpec(
        SpecialistRole.PLANNER, ModelTier.DEEP,
        "You are a task planner. Decompose the objective into an ordered, "
        "dependency-aware, bounded set of steps with completion conditions.",
        memory_scope="project",
    ),
    SpecialistRole.CRITIC: SpecialistSpec(
        SpecialistRole.CRITIC, ModelTier.FAST,
        "You are a strict critic. Identify flaws, missing evidence, unstated "
        "assumptions, and risks in the team's findings. Do not solve the task; "
        "audit it.",
        temperature=0.1,
    ),
    SpecialistRole.VERIFIER: SpecialistSpec(
        SpecialistRole.VERIFIER, ModelTier.FAST,
        "You are a verifier. Judge whether the synthesized answer is supported "
        "by the collected evidence and free of fabrication. Surface uncertainty "
        "rather than hide it.",
        temperature=0.0,
    ),
}


def spec_for(role: SpecialistRole) -> SpecialistSpec:
    return _SPECIALISTS[role]


# ── Legacy cyber specialists (preserved) mapped onto the controlled runtime ──
# The 5 names the live AURA `multi_agent_analyze` command and voice macros pass
# must keep resolving to a working specialist. They inherit the exact legacy
# prompts and route onto the appropriate role/tier — no behavior is deleted.
_LEGACY_ROLE_MAP: dict[str, SpecialistRole] = {
    "MalwareAnalyst": SpecialistRole.DFIR,
    "NetworkRecon": SpecialistRole.CYBER_PURPLE,
    "ThreatIntelligence": SpecialistRole.RESEARCH,
    "IncidentResponder": SpecialistRole.DFIR,
    "CodeAnalyst": SpecialistRole.CODE,
}


def legacy_spec(name: str) -> SpecialistSpec | None:
    """Resolve a legacy cyber-specialist name to a SpecialistSpec carrying its
    original prompt but the controlled runtime's tier/tool policy."""
    role = _LEGACY_ROLE_MAP.get(name)
    prompt = _LEGACY_PROMPTS.get(name)
    if role is None or not prompt:
        return None
    base = _SPECIALISTS[role]
    # Preserve the legacy prompt verbatim; keep the mapped role's runtime policy.
    return SpecialistSpec(
        role=role, tier=base.tier, system_prompt=prompt,
        allowed_tools=base.allowed_tools, timeout_s=base.timeout_s,
        context_budget=base.context_budget, memory_scope=base.memory_scope,
        temperature=base.temperature,
    )


# ════════════════════════════════════════════════════════════════════════════
#  Structured task / report / evidence / conflict types
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AgentTask:
    """A single unit of work handed to one specialist."""
    objective: str
    role: SpecialistRole
    context: dict = field(default_factory=dict)
    task_id: str = ""
    label: str = ""   # optional display name (e.g. legacy specialist name)


@dataclass
class EvidenceItem:
    """One piece of evidence with provenance and a trust label."""
    content: str
    source: str = "specialist"
    confidence: float = 0.5
    trusted: bool = True
    agent: str = ""
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.content = (self.content or "")[:_EVIDENCE_CAP]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


@dataclass
class AgentReport:
    """A specialist's structured output. Free-chat is never stored — only this."""
    agent: str
    role: SpecialistRole
    task_id: str
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    verdict: str | None = None       # e.g. "malicious" / "benign" — drives conflict detection
    confidence: float = 0.5
    error: str | None = None
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.summary = (self.summary or "")[:_REPORT_SUMMARY_CAP]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "role": self.role.value, "task_id": self.task_id,
            "summary": self.summary, "facts": list(self.facts),
            "evidence": [e.content for e in self.evidence],
            "hypotheses": list(self.hypotheses),
            "open_questions": list(self.open_questions),
            "verdict": self.verdict, "confidence": round(self.confidence, 2),
            "error": self.error, "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class Conflict:
    """A detected contradiction between two agent reports."""
    topic: str
    verdict_a: str
    agent_a: str
    verdict_b: str
    agent_b: str

    def to_dict(self) -> dict:
        return {
            "topic": self.topic, "verdict_a": self.verdict_a, "agent_a": self.agent_a,
            "verdict_b": self.verdict_b, "agent_b": self.agent_b,
        }


# Opposing-verdict pairs used for deterministic structured conflict detection.
_OPPOSING: dict[str, frozenset[str]] = {
    "malicious": frozenset({"benign", "clean", "safe"}),
    "benign": frozenset({"malicious", "compromised", "infected"}),
    "compromised": frozenset({"clean", "uncompromised", "benign"}),
    "clean": frozenset({"compromised", "malicious", "infected"}),
    "vulnerable": frozenset({"secure", "patched", "safe"}),
    "secure": frozenset({"vulnerable", "exploitable"}),
    "true": frozenset({"false"}),
    "false": frozenset({"true"}),
    "pass": frozenset({"fail"}),
    "fail": frozenset({"pass"}),
}


def _verdicts_conflict(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b or a == b:
        return False
    return b in _OPPOSING.get(a, frozenset())


@dataclass
class SynthesisResult:
    """The fan-in product of a controlled team run."""
    objective: str
    summary: str = ""
    confidence: float = 0.0
    key_findings: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    reports: list[AgentReport] = field(default_factory=list)
    critic: dict | None = None
    verified: bool | None = None      # None = verification not required this run
    evidence_count: int = 0
    agents: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    timestamp: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "objective": self.objective, "summary": self.summary,
            "confidence": round(self.confidence, 2),
            "key_findings": list(self.key_findings),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "unresolved": list(self.unresolved),
            "reports": [r.to_dict() for r in self.reports],
            "critic": self.critic, "verified": self.verified,
            "evidence_count": self.evidence_count, "agents": list(self.agents),
            "elapsed_s": self.elapsed_s, "timestamp": self.timestamp,
        }

    def to_legacy_dict(self) -> dict:
        """Backward-compatible shape for the pre-existing AURA/voice callers that
        expect {task, agents, results, synthesis, elapsed_s, timestamp}."""
        return {
            "task": self.objective,
            "agents": list(self.agents),
            "results": {r.agent: r.summary for r in self.reports},
            "synthesis": self.summary,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "elapsed_s": self.elapsed_s,
            "timestamp": self.timestamp,
        }


# ════════════════════════════════════════════════════════════════════════════
#  Shared blackboard — bounded, structured, provenance-carrying
# ════════════════════════════════════════════════════════════════════════════
class SharedBlackboard:
    """Bounded shared state for one team run. Every list is size-capped and
    deduplicated; nothing here is an unbounded transcript. Provenance/trust and
    timestamps are preserved; conflicts are detected structurally."""

    def __init__(self, objective: str = "") -> None:
        self.objective = objective
        self.facts: list[str] = []
        self.evidence: list[EvidenceItem] = []
        self.sources: list[str] = []
        self.assumptions: list[str] = []
        self.hypotheses: list[str] = []
        self.open_questions: list[str] = []
        self.decisions: list[str] = []
        self.conflicts: list[Conflict] = []
        self.artifacts: list[str] = []
        self.reports: list[AgentReport] = []
        self.unresolved_items: list[str] = []
        self._seen: set[str] = set()
        self.created_at = _now_iso()

    # ── bounded, deduped append helpers ──────────────────────────────────────
    def _add(self, bucket: list[str], value: str, cap: int) -> bool:
        v = (value or "").strip()
        if not v:
            return False
        key = f"{id(bucket)}:{_norm(v)}"
        if key in self._seen or len(bucket) >= cap:
            return False
        self._seen.add(key)
        bucket.append(v[:_EVIDENCE_CAP])
        return True

    def add_fact(self, fact: str) -> bool:
        return self._add(self.facts, fact, _BB_MAX_FACTS)

    def add_hypothesis(self, h: str) -> bool:
        return self._add(self.hypotheses, h, _BB_MAX_HYPOTHESES)

    def add_open_question(self, q: str) -> bool:
        return self._add(self.open_questions, q, _BB_MAX_QUESTIONS)

    def add_decision(self, d: str) -> bool:
        return self._add(self.decisions, d, _BB_MAX_DECISIONS)

    def add_artifact(self, a: str) -> bool:
        return self._add(self.artifacts, a, _BB_MAX_ARTIFACTS)

    def add_evidence(self, item: EvidenceItem) -> bool:
        key = f"ev:{_norm(item.content)}"
        if not item.content or key in self._seen or len(self.evidence) >= _BB_MAX_EVIDENCE:
            return False
        self._seen.add(key)
        self.evidence.append(item)
        if item.source and item.source not in self.sources:
            self.sources.append(item.source)
        return True

    def add_report(self, report: AgentReport) -> None:
        """Ingest a specialist report: store it (bounded), fan its structured
        fields into the shared buckets (deduped), and detect conflicts against
        prior reports."""
        if len(self.reports) < _BB_MAX_REPORTS:
            self.reports.append(report)
        for f in report.facts:
            self.add_fact(f)
        for h in report.hypotheses:
            self.add_hypothesis(h)
        for q in report.open_questions:
            self.add_open_question(q)
        for ev in report.evidence:
            self.add_evidence(ev)
        self._detect_conflicts(report)

    def _detect_conflicts(self, new: AgentReport) -> None:
        if not new.verdict:
            return
        for prior in self.reports:
            if prior is new or not prior.verdict:
                continue
            if _verdicts_conflict(prior.verdict, new.verdict):
                conflict = Conflict(
                    topic=self.objective or "verdict",
                    verdict_a=prior.verdict, agent_a=prior.agent,
                    verdict_b=new.verdict, agent_b=new.agent,
                )
                if len(self.conflicts) < _BB_MAX_CONFLICTS:
                    self.conflicts.append(conflict)
                    logger.warning(
                        f"BLACKBOARD: conflict {prior.agent}={prior.verdict} vs "
                        f"{new.agent}={new.verdict}"
                    )

    # ── read views ───────────────────────────────────────────────────────────
    def context_digest(self, budget: int = _BLACKBOARD_BUDGET) -> str:
        """A bounded textual digest for feeding the critic/synthesis stage.
        NEVER dumps full reports — only the capped, deduped structured fields."""
        lines: list[str] = [f"OBJECTIVE: {self.objective}"]
        if self.facts:
            lines.append("FACTS:\n" + "\n".join(f"- {f}" for f in self.facts[:20]))
        if self.evidence:
            lines.append("EVIDENCE:\n" + "\n".join(
                f"- ({e.agent}, conf={e.confidence:.2f}) {e.content}"
                for e in self.evidence[:20]
            ))
        if self.hypotheses:
            lines.append("HYPOTHESES:\n" + "\n".join(f"- {h}" for h in self.hypotheses[:15]))
        if self.open_questions:
            lines.append("OPEN QUESTIONS:\n" + "\n".join(f"- {q}" for q in self.open_questions[:15]))
        if self.conflicts:
            lines.append("CONFLICTS:\n" + "\n".join(
                f"- {c.agent_a}:{c.verdict_a} vs {c.agent_b}:{c.verdict_b}"
                for c in self.conflicts
            ))
        return "\n\n".join(lines)[:budget]

    def snapshot(self) -> dict:
        return {
            "objective": self.objective,
            "facts": list(self.facts),
            "evidence": [e.content for e in self.evidence],
            "sources": list(self.sources),
            "hypotheses": list(self.hypotheses),
            "open_questions": list(self.open_questions),
            "decisions": list(self.decisions),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "artifacts": list(self.artifacts),
            "unresolved_items": list(self.unresolved_items),
            "report_count": len(self.reports),
            "created_at": self.created_at,
        }


# ════════════════════════════════════════════════════════════════════════════
#  Execution policy — resource-aware concurrency
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TeamExecutionPolicy:
    """Bounded, resource-aware execution limits. Defaults are conservative for
    the CPU-bound target host and can only be *reduced* by pressure, never
    raised past the module ceilings."""
    max_fast_agents: int = _MAX_FAST_AGENTS
    max_deep_agents: int = _MAX_DEEP_AGENTS
    max_total_agents: int = _MAX_TOTAL_AGENTS
    task_timeout_s: float = _TASK_TIMEOUT_S
    team_timeout_s: float = _TEAM_TIMEOUT_S
    context_budget: int = _CONTEXT_BUDGET
    blackboard_budget: int = _BLACKBOARD_BUDGET

    def under_pressure(
        self, cpu_pct: float, ram_pct: float, on_battery: bool
    ) -> "TeamExecutionPolicy":
        """Return a policy with concurrency reduced if the host is stressed.
        High CPU/RAM or battery collapses FAST→1 and DEEP→1 and halves the
        total. Fail-safe: any unreadable metric is treated as pressure."""
        try:
            stressed = (
                float(cpu_pct) >= _CPU_CEIL_PCT
                or float(ram_pct) >= _RAM_CEIL_PCT
                or bool(on_battery)
            )
        except (TypeError, ValueError):
            stressed = True
        if not stressed:
            return self
        from dataclasses import replace
        return replace(
            self,
            max_fast_agents=1,
            max_deep_agents=1,
            max_total_agents=max(1, self.max_total_agents // 2),
        )


# ════════════════════════════════════════════════════════════════════════════
#  Team selection from a per-turn TaskDecision (M1)
# ════════════════════════════════════════════════════════════════════════════
_DOMAIN_TEAM: dict[TaskDomain, list[SpecialistRole]] = {
    TaskDomain.GENERAL: [SpecialistRole.GENERAL],
    TaskDomain.LANGUAGE: [SpecialistRole.LANGUAGE],
    TaskDomain.MATHEMATICS: [SpecialistRole.MATH],
    TaskDomain.VISION: [SpecialistRole.VISION],
    TaskDomain.CODER: [SpecialistRole.CODE],
    TaskDomain.ARCHITECT: [SpecialistRole.ARCHITECT],
    TaskDomain.RESEARCH: [SpecialistRole.RESEARCH],
    TaskDomain.DFIR: [SpecialistRole.DFIR],
    TaskDomain.CYBER_BLUE: [SpecialistRole.CYBER_BLUE],
    TaskDomain.CYBER_PURPLE: [SpecialistRole.CYBER_PURPLE],
    TaskDomain.GRC: [SpecialistRole.GRC],
    TaskDomain.PLANNER: [SpecialistRole.PLANNER],
    TaskDomain.CRITIC: [SpecialistRole.CRITIC],
    TaskDomain.VERIFIER: [SpecialistRole.VERIFIER],
}


class AgentTeamSelector:
    """Chooses a bounded specialist team from a per-turn ``TaskDecision``.

    The hot fast path stays fast: a simple GENERAL turn that neither prefers a
    team, requires planning, nor is complex/security-sensitive yields an EMPTY
    team — the caller then uses the direct single-model path. A team is formed
    only when it is actually warranted, and is always capped at
    ``policy.max_total_agents`` including the CRITIC/VERIFIER fan-in roles."""

    def __init__(self, policy: TeamExecutionPolicy | None = None, registry=None) -> None:
        self.policy = policy or TeamExecutionPolicy()
        # M15 skill-profile registry (lazy default). Consulted only to ADD
        # verification for high-risk domains — it can never remove a role or grant
        # a capability, so it cannot weaken any control.
        self._registry = registry

    def _skill_registry(self):
        if self._registry is None:
            try:
                from core.skill_profiles import get_skill_registry
                self._registry = get_skill_registry()
            except Exception:  # noqa: BLE001 — profiles are advisory; never block selection
                return None
        return self._registry

    def should_form_team(self, task_decision) -> bool:
        return bool(
            getattr(task_decision, "prefers_agent_team", False)
            or getattr(task_decision, "requires_planning", False)
            or getattr(task_decision, "complexity", 0.0) >= 0.75
        )

    def select(self, task_decision) -> list[SpecialistRole]:
        if not self.should_form_team(task_decision):
            return []
        domain = getattr(task_decision, "domain", TaskDomain.GENERAL)
        roles: list[SpecialistRole] = list(_DOMAIN_TEAM.get(domain, [SpecialistRole.GENERAL]))

        complex_turn = getattr(task_decision, "complexity", 0.0) >= 0.6 \
            or getattr(task_decision, "requires_planning", False)
        if complex_turn and SpecialistRole.CRITIC not in roles:
            roles.append(SpecialistRole.CRITIC)

        # M15: the domain's skill profile can REQUIRE verification (e.g. RESEARCH,
        # DFIR, CYBER_PURPLE, GRC). This only ever adds the VERIFIER — additive,
        # never a removal or a capability grant.
        registry = self._skill_registry()
        profile_requires_verify = bool(
            registry is not None and registry.requires_verification_for_domain(domain)
        )
        if (getattr(task_decision, "requires_verification", False)
                or getattr(task_decision, "security_sensitive", False)
                or profile_requires_verify):
            if SpecialistRole.VERIFIER not in roles:
                roles.append(SpecialistRole.VERIFIER)

        # Never exceed the global cap (keep primary specialists, then fan-in).
        return roles[: self.policy.max_total_agents]


# ════════════════════════════════════════════════════════════════════════════
#  Inference / resource / tool injection points
# ════════════════════════════════════════════════════════════════════════════
@runtime_checkable
class InferenceFn(Protocol):
    async def __call__(
        self, system: str, user: str, *,
        tier: ModelTier, timeout_s: float, num_ctx: int, temperature: float,
    ) -> str: ...


ResourceProbe = Callable[[], "tuple[float, float, bool]"]  # -> (cpu%, ram%, on_battery)
BroadcastFn = Callable[[dict], Awaitable[None]]


class ToolBroker:
    """The ONLY path a specialist can reach a tool. Enforces the spec's category
    allowlist, then delegates to ``ToolExecutor.aexecute`` — which re-applies the
    full risk-class / HITL / audit gate. There is deliberately no other method;
    a specialist cannot construct a subprocess, MCP, or raw-handler call."""

    # Bounded tool→category map. A tool absent here is treated as uncategorized
    # and refused unless the spec explicitly allows its category (fail-closed).
    _TOOL_CATEGORY: dict[str, ToolCategory] = {
        "read_file": ToolCategory.READ, "list_directory": ToolCategory.READ,
        "leer_archivo_universal": ToolCategory.READ, "query_knowledge": ToolCategory.READ,
        "consultar_base_conocimiento": ToolCategory.READ, "get_datetime": ToolCategory.READ,
        "hash_file": ToolCategory.READ, "decode_payload": ToolCategory.READ,
        "system_info": ToolCategory.SYSTEM, "list_processes": ToolCategory.SYSTEM,
        "get_system_status": ToolCategory.SYSTEM, "check_connectivity": ToolCategory.SYSTEM,
        "fetch_webpage": ToolCategory.WEB, "web_search": ToolCategory.WEB,
        "http_request": ToolCategory.WEB,
        "network_scan": ToolCategory.RECON, "osint_lookup": ToolCategory.RECON,
        "whois_lookup": ToolCategory.RECON,
        "code_execute": ToolCategory.CODE, "analizar_codigo_sast": ToolCategory.CODE,
    }

    def __init__(self, tool_executor, spec: SpecialistSpec) -> None:
        self._executor = tool_executor
        self._allowed = spec.allowed_tools
        self._role = spec.role

    def category_of(self, tool_name: str) -> ToolCategory | None:
        return self._TOOL_CATEGORY.get(tool_name)

    def is_allowed(self, tool_name: str) -> bool:
        cat = self.category_of(tool_name)
        return cat is not None and cat in self._allowed

    async def call(self, tool_name: str, tool_input: dict, reasoning: str = "") -> dict:
        """Delegate a tool call through the protected executor. Fail-closed on a
        disallowed category or a missing executor — never a bypass."""
        if self._executor is None:
            return {"error": "tool gateway unavailable"}
        if not self.is_allowed(tool_name):
            logger.warning(
                f"TOOLBROKER: role {self._role.value} denied tool {tool_name!r} "
                f"(category not in {sorted(c.value for c in self._allowed)})"
            )
            return {"error": f"tool '{tool_name}' not permitted for role {self._role.value}"}
        return await self._executor.aexecute(
            tool_name, tool_input, f"[{self._role.value}] {reasoning}"[:200]
        )


# ════════════════════════════════════════════════════════════════════════════
#  The controlled team runtime (executive)
# ════════════════════════════════════════════════════════════════════════════
class SpecialistTeamRuntime:
    """Controlled specialist team executive:

        select team → run specialists (bounded concurrency) →
        shared blackboard → critic → conflict view → verifier → synthesis

    Reasoning is done by the injected ``infer`` callable (one shared model);
    world-effects only ever go through :class:`ToolBroker`. Dependency-injected
    end-to-end so it is fully unit-testable without Ollama/psutil.
    """

    def __init__(
        self,
        *,
        infer: InferenceFn | None = None,
        tool_executor=None,
        fabric: MemoryFabric | None = None,
        resource_probe: ResourceProbe | None = None,
        broadcast_fn: BroadcastFn | None = None,
        policy: TeamExecutionPolicy | None = None,
    ) -> None:
        self._infer = infer
        self._tool_executor = tool_executor
        self._fabric = fabric
        self._resource_probe = resource_probe
        self._broadcast = broadcast_fn
        self.policy = policy or TeamExecutionPolicy()
        self.selector = AgentTeamSelector(self.policy)
        self._running = False

    # ── production wiring ────────────────────────────────────────────────────
    def attach(
        self,
        *,
        ollama_client,
        fast_model: str,
        deep_model: str,
        tool_executor=None,
        broadcast_fn: BroadcastFn | None = None,
        resource_probe: ResourceProbe | None = None,
        fabric: MemoryFabric | None = None,
    ) -> None:
        """Wire the production inference (shared Ollama client), tool gateway,
        broadcast, and resource probe. Vision/embedding tiers fall back to the
        fast model on this local streaming client (same policy as
        model_router.resolve_inference_model)."""
        self._infer = _make_ollama_infer(ollama_client, fast_model, deep_model)
        if tool_executor is not None:
            self._tool_executor = tool_executor
        if broadcast_fn is not None:
            self._broadcast = broadcast_fn
        if resource_probe is not None:
            self._resource_probe = resource_probe
        elif self._resource_probe is None:
            self._resource_probe = _default_resource_probe
        self._fabric = fabric or self._fabric or get_fabric()
        logger.info("V63 M4: SpecialistTeamRuntime attached (fast/deep shared client)")

    # ── resource-aware effective policy ──────────────────────────────────────
    def _effective_policy(self) -> TeamExecutionPolicy:
        if self._resource_probe is None:
            return self.policy
        try:
            cpu, ram, batt = self._resource_probe()
        except Exception:
            return self.policy.under_pressure(100.0, 100.0, True)  # fail-safe: reduce
        return self.policy.under_pressure(cpu, ram, batt)

    async def _emit(self, event: dict) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast({**event, "timestamp": _now_iso()})
        except Exception:
            pass

    # ── one specialist run ───────────────────────────────────────────────────
    async def _run_specialist(self, task: AgentTask, policy: TeamExecutionPolicy) -> AgentReport:
        spec = spec_for(task.role)
        label = task.label or spec.role.value
        await self._emit({"type": "agent_running", "agent": label, "role": spec.role.value})
        if self._infer is None:
            return AgentReport(agent=label, role=spec.role, task_id=task.task_id,
                               error="no inference backend", confidence=0.0)

        ctx_str = "\n".join(
            f"{k}: {v}" for k, v in (task.context or {}).items()
            if v is not None and str(v).strip()
        )[:policy.context_budget]
        ctx_block = f"CONTEXT:\n{ctx_str}\n\n" if ctx_str else ""
        user = (
            f"TASK: {task.objective}\n\n"
            f"{ctx_block}"
            "Provide your expert analysis. Be precise; separate fact from "
            "inference; state assumptions."
        )
        try:
            text = await asyncio.wait_for(
                self._infer(
                    spec.system_prompt, user,
                    tier=spec.tier, timeout_s=spec.timeout_s,
                    num_ctx=min(spec.context_budget, policy.context_budget),
                    temperature=spec.temperature,
                ),
                timeout=min(spec.timeout_s, policy.task_timeout_s),
            )
        except asyncio.TimeoutError:
            logger.warning(f"SPECIALIST {label}: timeout")
            return AgentReport(agent=label, role=spec.role, task_id=task.task_id,
                               error="timeout", confidence=0.0)
        except Exception as e:  # noqa: BLE001 — a specialist crash must not kill the team
            logger.warning(f"SPECIALIST {label}: error {e}")
            return AgentReport(agent=label, role=spec.role, task_id=task.task_id,
                               error=str(e)[:200], confidence=0.0)

        report = _parse_report(text, agent=label, role=spec.role, task_id=task.task_id)
        await self._emit({
            "type": "agent_complete", "agent": label, "role": spec.role.value,
            "preview": report.summary[:200], "confidence": round(report.confidence, 2),
        })
        return report

    # ── bounded-concurrency fan-out ──────────────────────────────────────────
    async def _run_team_tasks(
        self, tasks: list[AgentTask], policy: TeamExecutionPolicy
    ) -> list[AgentReport]:
        fast_sem = asyncio.Semaphore(max(1, policy.max_fast_agents))
        deep_sem = asyncio.Semaphore(max(1, policy.max_deep_agents))

        async def _guarded(task: AgentTask) -> AgentReport:
            sem = deep_sem if spec_for(task.role).is_deep else fast_sem
            async with sem:
                return await self._run_specialist(task, policy)

        return list(await asyncio.gather(*(_guarded(t) for t in tasks)))

    # ── critic / synthesis fan-in ────────────────────────────────────────────
    async def _run_critic(self, board: SharedBlackboard) -> dict:
        """Deterministic structured critique from the read-only CriticEngine,
        optionally enriched by a critic-model pass. Never executes tools."""
        result = {
            "conflicts": len(board.conflicts),
            "evidence": len(board.evidence),
            "open_questions": len(board.open_questions),
            "flags": [],
        }
        if not board.evidence:
            result["flags"].append("no_evidence")
        if board.conflicts:
            result["flags"].append(f"unresolved_conflicts:{len(board.conflicts)}")
        # Optional LLM critic pass over the bounded digest (fail-open).
        if self._infer is not None and board.reports:
            spec = _SPECIALISTS[SpecialistRole.CRITIC]
            try:
                text = await asyncio.wait_for(
                    self._infer(
                        spec.system_prompt,
                        "Audit these team findings. List the 3 most important "
                        "gaps or risks, one per line:\n\n" + board.context_digest(),
                        tier=spec.tier, timeout_s=spec.timeout_s,
                        num_ctx=spec.context_budget, temperature=spec.temperature,
                    ),
                    timeout=spec.timeout_s,
                )
                result["notes"] = [ln.strip("-• ").strip()
                                   for ln in text.splitlines() if ln.strip()][:3]
            except Exception:
                pass
        return result

    async def _synthesize(
        self, board: SharedBlackboard, policy: TeamExecutionPolicy
    ) -> str:
        if self._infer is None:
            # Deterministic fallback: concatenate report summaries (bounded).
            return "\n\n".join(f"[{r.agent}] {r.summary}" for r in board.reports)[:policy.blackboard_budget]
        prompt = (
            "Synthesize the team's findings into ONE unified, prioritized answer. "
            "Resolve conflicts where the evidence allows, and explicitly list any "
            "that remain unresolved. Do not introduce claims not supported by the "
            "evidence below.\n\n" + board.context_digest(policy.blackboard_budget)
        )
        try:
            return await asyncio.wait_for(
                self._infer(
                    "You are the team's synthesis lead. Produce an evidence-backed, "
                    "conflict-aware final answer.",
                    prompt, tier=ModelTier.SYNTHESIS, timeout_s=policy.task_timeout_s,
                    num_ctx=policy.context_budget, temperature=0.1,
                ),
                timeout=policy.task_timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SYNTHESIS: error {e}")
            return "\n\n".join(f"[{r.agent}] {r.summary}" for r in board.reports)[:policy.blackboard_budget]

    async def _verify(self, board: SharedBlackboard, summary: str) -> bool | None:
        """Verifier fan-in: judge whether the synthesis is supported. Fail-closed
        (unsupported) on any error so an outage never rubber-stamps."""
        if self._infer is None:
            return None
        spec = _SPECIALISTS[SpecialistRole.VERIFIER]
        try:
            text = await asyncio.wait_for(
                self._infer(
                    spec.system_prompt,
                    "Answer only YES or NO on the first line: is the SYNTHESIS "
                    "fully supported by the EVIDENCE with no fabrication?\n\n"
                    f"SYNTHESIS:\n{summary[:1500]}\n\nEVIDENCE:\n{board.context_digest()}",
                    tier=spec.tier, timeout_s=spec.timeout_s,
                    num_ctx=spec.context_budget, temperature=0.0,
                ),
                timeout=spec.timeout_s,
            )
            return text.strip().lower().startswith(("yes", "y", "true", "supported"))
        except Exception:
            return False

    # ── public entry points ──────────────────────────────────────────────────
    async def run_team(
        self,
        objective: str,
        roles: list[SpecialistRole],
        context: dict | None = None,
        *,
        verify: bool = False,
        labels: dict[SpecialistRole, str] | None = None,
        board: SharedBlackboard | None = None,
    ) -> SynthesisResult:
        """Run a controlled team for *objective* over the given *roles*.

        Bounded end-to-end: total agents capped, per-task and whole-team
        timeouts enforced, concurrency reduced under resource pressure. Returns a
        structured :class:`SynthesisResult`."""
        import time
        start = time.monotonic()
        if self._running:
            return SynthesisResult(objective=objective, summary="",
                                   unresolved=["team runtime already busy"])
        context = context or {}
        policy = self._effective_policy()
        roles = list(dict.fromkeys(roles))[: policy.max_total_agents]  # dedup + cap
        board = board or SharedBlackboard(objective)

        self._running = True
        try:
            await self._emit({"type": "agent_task_started", "task": objective[:100],
                              "agents": [r.value for r in roles]})
            tasks = [
                AgentTask(objective=objective, role=r, context=context,
                          task_id=f"t{i}", label=(labels or {}).get(r, ""))
                for i, r in enumerate(roles)
            ]
            try:
                reports = await asyncio.wait_for(
                    self._run_team_tasks(tasks, policy), timeout=policy.team_timeout_s
                )
            except asyncio.TimeoutError:
                logger.warning("TEAM: whole-team timeout — partial results")
                reports = []
                board.unresolved_items.append("team timeout — partial or no results")

            for rep in reports:
                board.add_report(rep)

            critic = await self._run_critic(board)
            summary = await self._synthesize(board, policy)
            verified = await self._verify(board, summary) if verify else None

            for c in board.conflicts:
                board.unresolved_items.append(
                    f"conflict: {c.agent_a}={c.verdict_a} vs {c.agent_b}={c.verdict_b}"
                )

            elapsed = round(time.monotonic() - start, 2)
            confidence = _team_confidence(reports, board.conflicts, verified)
            result = SynthesisResult(
                objective=objective, summary=summary, confidence=confidence,
                key_findings=board.facts[:10], conflicts=list(board.conflicts),
                unresolved=list(dict.fromkeys(board.unresolved_items)),
                reports=reports, critic=critic, verified=verified,
                evidence_count=len(board.evidence),
                agents=[r.agent for r in reports], elapsed_s=elapsed,
            )
            await self._emit({
                "type": "agent_task_complete", "task": objective[:80],
                "agents": result.agents, "elapsed_s": elapsed,
                "conflicts": len(board.conflicts),
                "synthesis_preview": summary[:300], "verified": verified,
            })
            await self._persist(result)
            return result
        finally:
            self._running = False

    async def run_team_for_decision(
        self, task_decision, objective: str, context: dict | None = None,
    ) -> SynthesisResult | None:
        """Select a team from a per-turn ``TaskDecision`` and run it. Returns
        ``None`` when no team is warranted (the caller should use the direct
        single-model path) — this is what keeps simple chat off the team path."""
        roles = self.selector.select(task_decision)
        if not roles:
            return None
        return await self.run_team(
            objective, roles, context,
            verify=bool(getattr(task_decision, "requires_verification", False)),
        )

    async def run_legacy_agents(
        self, task: str, agents: list[str], context: dict | None = None,
    ) -> SynthesisResult:
        """Backward-compatible entry for the pre-existing cyber-specialist callers
        (AURA ``multi_agent_analyze`` / voice macros). Maps legacy names to specs,
        preserving their exact prompts, and runs them through the controlled
        runtime. Unknown names are skipped."""
        specs = [(name, legacy_spec(name)) for name in (agents or [])]
        valid = [(name, s) for name, s in specs if s is not None]
        if not valid:
            return SynthesisResult(objective=task, summary="", unresolved=["no valid agents"])
        roles = [s.role for _, s in valid]
        labels = {s.role: name for name, s in valid}
        return await self.run_team(task, roles, context, labels=labels)

    async def _persist(self, result: SynthesisResult) -> None:
        """Write a bounded, provenance-tagged team summary to the memory fabric
        (project scope). Fail-open; never blocks the turn."""
        if self._fabric is None or not result.summary.strip():
            return
        try:
            await self._fabric.store(
                f"[team:{result.objective[:80]}] {result.summary[:800]}",
                memory_type="agent_team_synthesis",
                source="specialist_team",
                scope="project",
                sensitivity=Sensitivity.NORMAL,
                confidence=result.confidence,
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
#  Report parsing, confidence, production inference/resource helpers
# ════════════════════════════════════════════════════════════════════════════
_VERDICT_TOKENS = (
    "malicious", "benign", "clean", "compromised", "infected", "vulnerable",
    "secure", "patched", "exploitable", "true", "false", "pass", "fail",
)


def _parse_report(text: str, *, agent: str, role: SpecialistRole, task_id: str) -> AgentReport:
    """Best-effort structuring of a free-text specialist answer into an
    AgentReport. Deterministic: summary = whole text (capped); facts = bulleted
    lines; verdict = first recognized verdict token; confidence heuristic."""
    text = (text or "").strip()
    facts: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s[:2] in ("- ", "* ", "• ") or (len(s) > 2 and s[0].isdigit() and s[1] in ").:"):
            cleaned = s.lstrip("-*•0123456789).: ").strip()
            if cleaned:
                facts.append(cleaned)
    lower = text.lower()
    verdict = next((v for v in _VERDICT_TOKENS if v in lower), None)
    confidence = 0.6 if facts else (0.4 if text else 0.0)
    return AgentReport(
        agent=agent, role=role, task_id=task_id,
        summary=text, facts=facts[:_BB_MAX_FACTS // 2], verdict=verdict,
        confidence=confidence,
    )


def _team_confidence(
    reports: list[AgentReport], conflicts: list[Conflict], verified: bool | None
) -> float:
    ok = [r for r in reports if not r.error]
    if not ok:
        return 0.0
    base = sum(r.confidence for r in ok) / len(ok)
    base *= (0.6 if conflicts else 1.0)          # unresolved conflicts lower confidence
    if verified is True:
        base = min(1.0, base + 0.15)
    elif verified is False:
        base *= 0.5
    return round(max(0.0, min(1.0, base)), 2)


def _make_ollama_infer(ollama_client, fast_model: str, deep_model: str) -> InferenceFn:
    """Build the production inference callable over a shared Ollama client.
    DEEP/SYNTHESIS → deep model; everything else → fast model (vision/embedding
    can't stream chat on this local client, matching resolve_inference_model)."""
    async def _infer(
        system: str, user: str, *, tier: ModelTier, timeout_s: float,
        num_ctx: int, temperature: float,
    ) -> str:
        model = deep_model if tier in (ModelTier.DEEP, ModelTier.SYNTHESIS) else fast_model
        resp = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                stream=False,
                extra_body={"options": {"num_ctx": num_ctx, "temperature": temperature}},
            ),
            timeout=timeout_s,
        )
        return (resp.choices[0].message.content or "").strip()

    return _infer


def _default_resource_probe() -> tuple[float, float, bool]:
    """(cpu%, ram%, on_battery) via psutil, best-effort. Any failure reports
    pressure so the runtime backs off conservatively."""
    try:
        import psutil
        cpu = float(psutil.cpu_percent(interval=None))
        ram = float(psutil.virtual_memory().percent)
        batt = psutil.sensors_battery()
        on_battery = bool(batt is not None and not batt.power_plugged)
        return cpu, ram, on_battery
    except Exception:
        return 100.0, 100.0, True


# Module singleton — attached in main.py alongside the legacy orchestrator.
team_runtime = SpecialistTeamRuntime()
