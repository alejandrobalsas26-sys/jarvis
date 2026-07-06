"""
core/task_domain.py — V63 Milestone 2: semantic task-domain classification.

A ``TaskDomain`` is the *semantic subject* of a turn — what kind of problem it is
— and is deliberately **independent** of three other dimensions:

  * ``ModelRole`` (core.model_router): which local model actually runs. Domain
    only *advises* a preferred role; ``route()`` remains the sole authority for
    model selection and its precedence/enum are untouched by this module.
  * complexity: a continuous score (core.model_router.calculate_complexity).
  * risk: whether the turn is security-sensitive (core.model_router
    .is_security_sensitive_turn) / needs HITL (core.risk_classes).

This module is pure, dependency-light, and deterministic: classification is a
weighted keyword count with a fixed tie-break priority, so the same prompt always
yields the same domain. Composition of domain + role + complexity + risk into a
single per-turn decision happens in ``core.agent_runtime`` (M1), not here.

Bilingual (EN + ES) vocab mirrors the style of core.model_router's keyword sets.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.model_router import ModelRole


class TaskDomain(str, Enum):
    """The 14 semantic domains a turn can belong to."""

    GENERAL = "general"
    RESEARCH = "research"
    CODER = "coder"
    ARCHITECT = "architect"
    MATHEMATICS = "mathematics"
    LANGUAGE = "language"
    VISION = "vision"
    CYBER_BLUE = "cyber_blue"
    CYBER_PURPLE = "cyber_purple"
    DFIR = "dfir"
    GRC = "grc"
    PLANNER = "planner"
    CRITIC = "critic"
    VERIFIER = "verifier"


# ── Per-domain keyword vocab (EN + ES). Substring match on lowercased text,
# matching core.model_router._kw_hits semantics (``kw in text``). ─────────────
_DOMAIN_KEYWORDS: dict[TaskDomain, set[str]] = {
    TaskDomain.VISION: {
        "screenshot", "screen capture", "camera", "webcam", "image", "picture",
        "photo", "ocr", "diagram", "topology", "visual", "screen",
        "captura", "pantalla", "imagen", "foto", "cámara", "camara",
        "diagrama", "topología", "topologia", "visión", "vision",
    },
    TaskDomain.MATHEMATICS: {
        "equation", "integral", "derivative", "algebra", "calculus",
        "probability", "matrix", "theorem", "proof", "geometry", "statistics",
        "solve for", "factorial", "polynomial", "arithmetic",
        "ecuación", "ecuacion", "derivada", "álgebra", "algebra", "cálculo",
        "calculo", "probabilidad", "matriz", "teorema", "demostración",
        "estadística", "estadistica", "resolver la ecuación",
    },
    TaskDomain.CODER: {
        "python", "javascript", "typescript", "refactor", "refactoring",
        "debug", "debugging", "stacktrace", "traceback", "pytest", "unit test",
        "compile", "function", "class ", "regex", "code", "coding", "script",
        "código", "codigo", "programa", "programar", "depurar", "función",
        "funcion", "clase", "compilar", "prueba unitaria", "refactorizar",
    },
    TaskDomain.DFIR: {
        "dfir", "forensic", "forensics", "incident response", "incident",
        "triage", "memory dump", "timeline", "artifact", "ioc", "kill chain",
        "root cause", "compromise", "breach",
        "forense", "forensia", "respuesta a incidentes", "incidente",
        "cronología", "cronologia", "artefacto", "causa raíz", "causa raiz",
        "compromiso",
    },
    TaskDomain.CYBER_PURPLE: {
        "purple team", "adversary emulation", "attack simulation", "red team",
        "offensive", "exploit", "payload", "beacon", "lateral movement",
        "privilege escalation", "persistence", "shellcode", "c2",
        "equipo púrpura", "emulación de adversario", "simulación de ataque",
        "equipo rojo", "ofensivo", "movimiento lateral",
        "escalada de privilegios", "persistencia",
    },
    TaskDomain.CYBER_BLUE: {
        "blue team", "detection", "detection engineering", "siem", "sigma rule",
        "yara", "hardening", "defense", "defensive", "monitoring", "edr", "soc",
        "threat hunting", "log analysis",
        "equipo azul", "detección", "deteccion", "defensa", "defensivo",
        "endurecimiento", "monitoreo", "caza de amenazas",
    },
    TaskDomain.GRC: {
        "grc", "governance", "compliance", "audit", "policy", "risk assessment",
        "iso 27001", "nist", "soc 2", "gdpr", "regulatory", "control framework",
        "gobernanza", "cumplimiento", "auditoría", "auditoria", "política",
        "politica", "evaluación de riesgos", "regulatorio", "marco de control",
    },
    TaskDomain.ARCHITECT: {
        "architecture", "architect", "design pattern", "system design",
        "scalability", "tradeoff", "trade-off", "microservice", "schema design",
        "high-level design", "blueprint",
        "arquitectura", "patrón de diseño", "diseño de sistema", "escalabilidad",
        "compromiso de diseño", "microservicio",
    },
    TaskDomain.RESEARCH: {
        "research", "investigate", "find out", "sources", "literature",
        "survey", "gather information", "compare options", "state of the art",
        "latest developments", "look up",
        "investigar", "investigación", "fuentes", "recopilar información",
        "comparar opciones", "estado del arte",
    },
    TaskDomain.LANGUAGE: {
        "translate", "translation", "grammar", "rephrase", "rewrite",
        "summarize this", "proofread", "wording", "tone of",
        "traducir", "traducción", "traduccion", "gramática", "gramatica",
        "reformular", "reescribir", "corregir el texto", "resumir este",
    },
    TaskDomain.PLANNER: {
        "plan this", "roadmap", "break down", "milestones", "task list",
        "organize tasks", "step-by-step plan", "action plan",
        "hoja de ruta", "desglosar", "hitos", "lista de tareas",
        "organizar tareas", "plan de acción",
    },
    TaskDomain.CRITIC: {
        "critique", "review this", "find flaws", "weaknesses", "what's wrong",
        "evaluate quality", "pros and cons", "red flags",
        "crítica", "critica", "revisar esto", "defectos", "debilidades",
        "evaluar la calidad", "pros y contras",
    },
    TaskDomain.VERIFIER: {
        "verify", "fact-check", "fact check", "validate", "is this correct",
        "confirm", "double-check", "double check",
        "verificar", "comprobar", "validar", "es correcto", "confirmar",
    },
}

# Fixed tie-break priority: when two domains tie on hit count, the domain listed
# EARLIER here wins. Ordered most-specific / highest-signal first. Deterministic.
_TIE_BREAK_ORDER: tuple[TaskDomain, ...] = (
    TaskDomain.VISION,
    TaskDomain.MATHEMATICS,
    TaskDomain.DFIR,
    TaskDomain.CYBER_PURPLE,
    TaskDomain.CYBER_BLUE,
    TaskDomain.GRC,
    TaskDomain.CODER,
    TaskDomain.ARCHITECT,
    TaskDomain.RESEARCH,
    TaskDomain.LANGUAGE,
    TaskDomain.PLANNER,
    TaskDomain.VERIFIER,
    TaskDomain.CRITIC,
    TaskDomain.GENERAL,
)

# Domain → advisory preferred model role. NOT authoritative — route() decides the
# actual model. Used only as a hint composed in core.agent_runtime.
_DOMAIN_ROLE: dict[TaskDomain, ModelRole] = {
    TaskDomain.GENERAL: ModelRole.FAST,
    TaskDomain.LANGUAGE: ModelRole.FAST,
    TaskDomain.CODER: ModelRole.CODER,
    TaskDomain.VISION: ModelRole.VISION,
    TaskDomain.RESEARCH: ModelRole.DEEP,
    TaskDomain.ARCHITECT: ModelRole.DEEP,
    TaskDomain.MATHEMATICS: ModelRole.DEEP,
    TaskDomain.DFIR: ModelRole.DEEP,
    TaskDomain.CYBER_BLUE: ModelRole.DEEP,
    TaskDomain.CYBER_PURPLE: ModelRole.DEEP,
    TaskDomain.GRC: ModelRole.DEEP,
    TaskDomain.PLANNER: ModelRole.DEEP,
    TaskDomain.CRITIC: ModelRole.VERIFIER,
    TaskDomain.VERIFIER: ModelRole.VERIFIER,
}

# Domains that inherently benefit from a planner / specialist team / verification.
# Advisory booleans consumed by the composite decision (M1). Conservative — the
# fast path (GENERAL/LANGUAGE) requires none of these.
_PLANNING_DOMAINS: frozenset[TaskDomain] = frozenset({
    TaskDomain.RESEARCH, TaskDomain.ARCHITECT, TaskDomain.DFIR,
    TaskDomain.CYBER_PURPLE, TaskDomain.GRC, TaskDomain.PLANNER,
})
_AGENT_TEAM_DOMAINS: frozenset[TaskDomain] = frozenset({
    TaskDomain.RESEARCH, TaskDomain.DFIR, TaskDomain.CYBER_PURPLE,
    TaskDomain.ARCHITECT, TaskDomain.GRC,
})

# Tool-name hints: presence of these tools nudges a domain even absent keywords.
_TOOL_DOMAIN_HINTS: dict[str, TaskDomain] = {
    "take_screenshot": TaskDomain.VISION,
    "escanear_pantalla": TaskDomain.VISION,
    "analyze_room": TaskDomain.VISION,
    "capture_camera": TaskDomain.VISION,
    "webcam_capture": TaskDomain.VISION,
    "code_execute": TaskDomain.CODER,
    "network_scan": TaskDomain.CYBER_PURPLE,
    "osint_lookup": TaskDomain.RESEARCH,
    "query_knowledge": TaskDomain.RESEARCH,
    "estudiar_tema": TaskDomain.RESEARCH,
}


@dataclass(frozen=True)
class DomainSignal:
    """Result of semantic domain classification for a single turn."""

    domain: TaskDomain
    confidence: float          # 0.0 (fallback) .. 1.0
    preferred_role: ModelRole  # advisory only — route() is authoritative
    requires_planning: bool
    prefers_agent_team: bool
    reason: str
    matched: tuple[str, ...]   # keywords/tools that fired


def preferred_role_for(domain: TaskDomain) -> ModelRole:
    """Advisory model role for *domain* (never overrides route())."""
    return _DOMAIN_ROLE.get(domain, ModelRole.FAST)


def _count_hits(text: str, vocab: set[str]) -> tuple[int, list[str]]:
    matched = [kw for kw in vocab if kw in text]
    return len(matched), matched


def classify_domain(
    prompt: str,
    tool_names: list[str] | None = None,
) -> DomainSignal:
    """Classify *prompt* into a ``TaskDomain`` deterministically.

    Scoring: keyword-hit count per domain (+1 per hinted tool). Highest score
    wins; ties broken by ``_TIE_BREAK_ORDER``. Zero hits → ``GENERAL`` with low
    confidence. Pure and side-effect free; safe on the hot per-turn path.
    """
    text = (prompt or "").lower()

    scores: dict[TaskDomain, int] = {}
    matched_by: dict[TaskDomain, list[str]] = {}
    for domain, vocab in _DOMAIN_KEYWORDS.items():
        hits, matched = _count_hits(text, vocab)
        if hits:
            scores[domain] = hits
            matched_by[domain] = matched

    # Tool hints add weight (and can introduce a domain with no keyword hits).
    for tool in tool_names or []:
        hinted = _TOOL_DOMAIN_HINTS.get(tool or "")
        if hinted is not None:
            scores[hinted] = scores.get(hinted, 0) + 1
            matched_by.setdefault(hinted, []).append(f"tool:{tool}")

    if not scores:
        return DomainSignal(
            domain=TaskDomain.GENERAL,
            confidence=0.3,
            preferred_role=ModelRole.FAST,
            requires_planning=False,
            prefers_agent_team=False,
            reason="no domain keywords — general chat",
            matched=(),
        )

    best_score = max(scores.values())
    # Deterministic tie-break: first in _TIE_BREAK_ORDER among the top scorers.
    winner = next(
        d for d in _TIE_BREAK_ORDER if scores.get(d, 0) == best_score
    )
    matched = tuple(sorted(matched_by.get(winner, ())))
    confidence = round(min(1.0, 0.4 + 0.2 * best_score), 2)

    return DomainSignal(
        domain=winner,
        confidence=confidence,
        preferred_role=preferred_role_for(winner),
        requires_planning=winner in _PLANNING_DOMAINS,
        prefers_agent_team=winner in _AGENT_TEAM_DOMAINS,
        reason=f"{winner.value} keywords ({best_score} hit(s))",
        matched=matched,
    )
