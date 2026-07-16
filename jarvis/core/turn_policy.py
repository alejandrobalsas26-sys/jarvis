"""
core/turn_policy.py — V69 M54.3 + M54.6: deterministic pre-tool & verification policy.

The live run answered "POO" (a general OOP question) by calling `query_knowledge`,
finding the vault empty, and then REFUSING to explain OOP — general knowledge was
made hostage to a private document store (symptom #3). It also ran an LLM verifier
over trivial educational answers, contributing to the multi-minute turn (symptom #5).

This module classifies each turn ONCE, deterministically, into a request class and
attaches a reason code, the set of tool families this turn may use, and the
verification policy. It does NOT replace the model router or the security gate — it
composes the existing signals (core.task_domain.classify_domain, core.model_router
.is_security_sensitive_turn, core.cyber_intent authorization state) and adds the
missing discipline: *should this turn touch the private document vault at all, and
does its answer need an LLM verifier?*

Pure and side-effect free. Inspectable (every decision carries matched markers and
a reason code) and fully unit-testable without a live model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.model_router import is_security_sensitive_turn
from core.task_domain import TaskDomain, classify_domain


class RequestClass(str, Enum):
    """The interaction class of a turn — what the user is really asking for."""

    ORDINARY_CONVERSATION = "ordinary_conversation"     # greetings, chit-chat
    GENERAL_EDUCATIONAL = "general_educational"         # "explain OOP / inheritance"
    CODING_EXPLANATION = "coding_explanation"           # explain code / a language feature
    PRIVATE_DOCUMENT = "private_document"               # "what does MY pdf say about X"
    MEMORY_RECALL = "memory_recall"                     # "what did I tell you about my project"
    OPERATIONAL_STATUS = "operational_status"           # "system status / are we healthy"
    CURRENT_TIME = "current_time"                       # "what time / date is it"
    EFFECTFUL_TOOL = "effectful_tool"                   # "run / open / write / scan"
    CYBER_SENSITIVE = "cyber_sensitive"                 # offensive/operational security ask


class ReasonCode(str, Enum):
    """Inspectable routing reason recorded in diagnostics (never the model's chain
    of thought — just the deterministic policy decision)."""

    DIRECT_FAST = "DIRECT_FAST"
    PRIVATE_RAG = "PRIVATE_RAG"
    MEMORY_RECALL = "MEMORY_RECALL"
    DETERMINISTIC_TIME = "DETERMINISTIC_TIME"
    OPERATIONAL_QUERY = "OPERATIONAL_QUERY"
    TOOL_REQUIRED = "TOOL_REQUIRED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"


class VerifyPolicy(str, Enum):
    """How much verification the turn's answer warrants (M54.6 matrix)."""

    SKIP_LLM_VERIFIER = "SKIP_LLM_VERIFIER"                 # greetings, basic education
    DETERMINISTIC_CHECKS_ONLY = "DETERMINISTIC_CHECKS_ONLY"  # general educational
    GROUNDING_CHECK = "GROUNDING_CHECK"                    # private-doc factual answer
    EVIDENCE_REFERENCE_CHECK = "EVIDENCE_REFERENCE_CHECK"  # operational state answer
    BOUNDED_MODEL_VERIFIER = "BOUNDED_MODEL_VERIFIER"      # cyber-sensitive procedural
    FULL_VERIFICATION = "FULL_VERIFICATION"                # effectful action recommendation


# Tool families the model MAY use this turn. The private-vault family is the one
# the POO bug hinged on — it is offered ONLY for a private-document/research turn.
_PRIVATE_VAULT_TOOLS: frozenset[str] = frozenset({
    "query_knowledge", "consultar_base_conocimiento",
})

# ── Marker vocab (EN + ES). Deterministic substring/word cues, NOT an LLM. ────
_TIME_MARKERS = (
    "qué hora", "que hora", "qué día", "que dia", "qué fecha", "que fecha",
    "hora es", "fecha de hoy", "día es hoy", "dia es hoy", "hora actual",
    "what time", "what day", "what date", "current time", "current date",
    "today's date", "todays date", "what's the time", "whats the time",
    "time is it", "date is it",
)
# Private-document cues: the user points at THEIR indexed material specifically.
_PRIVATE_DOC_MARKERS = (
    "mi pdf", "mis pdf", "mi documento", "mis documentos", "mi archivo",
    "mis archivos", "mis apuntes", "mi apunte", "que indexé", "que indexe",
    "que subí", "que subi", "en mis documentos", "según el archivo",
    "segun el archivo", "según el documento", "segun el documento",
    "en el pdf", "del pdf", "en mis pdf", "base de conocimiento", "knowledge vault",
    "my pdf", "my document", "my documents", "my file", "my files", "my notes",
    "i indexed", "i uploaded", "in my documents", "in the pdf", "from the pdf",
    "in my notes", "the file i", "document i indexed", "según mis", "segun mis",
    "busca en mis", "search my", "in my knowledge base",
)
# Explicit long-term memory recall about prior conversation / the user's project.
_MEMORY_MARKERS = (
    "qué te dije", "que te dije", "lo que te conté", "lo que te conte",
    "te comenté", "te comente", "recuerdas", "te acuerdas", "mi proyecto",
    "sobre mi proyecto", "lo que hablamos", "antes te dije", "te mencioné",
    "te mencione", "what did i tell you", "do you remember", "you remember",
    "we talked about", "i told you", "about my project", "earlier i said",
    "as i mentioned",
)
_OPERATIONAL_MARKERS = (
    "system status", "estado del sistema", "estás bien", "estas bien",
    "runtime health", "salud del sistema", "readiness", "are we healthy",
    "how are the systems", "cómo están los sistemas", "como estan los sistemas",
    "estado de jarvis", "system health", "diagnóstico", "diagnostico",
    "are you healthy", "self test", "self-test",
)
# Effectful verbs — the user asks JARVIS to DO something with world effect. These
# stay gated downstream by the executor/HITL/NATO; the policy only records intent.
_EFFECTFUL_MARKERS = (
    "run ", "ejecuta", "ejecutar", "abre ", "abrir ", "open ", "launch ",
    "escribe un archivo", "write a file", "write to file", "crea el archivo",
    "borra ", "delete ", "elimina ", "kill ", "mata el proceso", "scan ",
    "escanea", "escanear", "nmap", "descarga", "download ", "instala ", "install ",
    "deploy", "despliega", "ejecuta el comando", "run the command",
    "toma una captura", "take a screenshot", "screenshot",
)
_GREETING_MARKERS = (
    "hola", "buenas", "buenos días", "buenos dias", "buenas tardes",
    "buenas noches", "qué tal", "que tal", "hey", "hello", "hi ", "hi!",
    "good morning", "good evening", "gracias", "thank you", "thanks",
    "adiós", "adios", "hasta luego", "bye",
)
_EDUCATIONAL_MARKERS = (
    "qué es", "que es", "explícame", "explicame", "explica", "cómo funciona",
    "como funciona", "diferencia entre", "para qué sirve", "para que sirve",
    "qué significa", "que significa", "what is", "what are", "explain",
    "how does", "difference between", "what does", "define ", "meaning of",
    "teach me", "enséñame", "ensename", "concepto de",
)

# V69 M54.1.11 — the "how do I <verb>?" interrogative form.
#
# The live turn "como saco la raiz cubica de algo" matched NO marker set: the
# educational list had "qué es"/"cómo funciona"/"explica" but nothing for
# "cómo + verb". It fell through to branch 9 (ORDINARY_CONVERSATION), which is a
# real misclassification — an ordinary how-to question IS general educational.
#
# This is an ALLOWLIST of educational verbs, not a broad "cómo \w+" pattern:
# "cómo estás" is a greeting, not a lesson. It is also safe by PRECEDENCE — the
# cyber-sensitive (5) and effectful (6) branches are evaluated BEFORE educational
# (7), so "cómo ejecuto un exploit" can never be promoted to educational by this.
# Accent-optional throughout: the operator types without accents.
_HOWTO_RE = re.compile(
    r"\bc[oó]mo\s+(?:se\s+|puedo\s+|podr[ií]a\s+|deber[ií]a\s+)?"
    r"(?:saco|sacar|saca|hago|hacer|hace|calculo|calcular|calcula|"
    r"obtengo|obtener|obtiene|resuelvo|resolver|resuelve|"
    r"escribo|escribir|escribe|uso|usar|usa|aprendo|aprender|"
    r"defino|definir|declaro|declarar|convierto|convertir|"
    r"funciona|funcionan|sirve)\b"
    r"|\bhow\s+(?:do|can|could|would|should)\s+(?:i|you|we)\b"
    r"|\bhow\s+to\s+\w+"
)


def _hits(text: str, markers: tuple[str, ...]) -> list[str]:
    return [m for m in markers if m in text]


@dataclass(frozen=True)
class TurnPolicy:
    """The deterministic pre-tool + verification decision for one turn."""

    request_class: RequestClass
    reason_code: ReasonCode
    verify_policy: VerifyPolicy
    security_sensitive: bool
    knowledge_vault_allowed: bool     # may the private-vault tool run this turn?
    matched: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    def filter_tools(self, tools: list[dict]) -> list[dict]:
        """Return the per-turn tool subset. The private-vault family is dropped
        unless this turn is a private-document/research query — so a general
        educational question ("POO") can never be sent to `query_knowledge`.
        Every other tool is preserved (their own gates are unchanged)."""
        if self.knowledge_vault_allowed:
            return tools
        return [
            t for t in tools
            if t.get("function", {}).get("name") not in _PRIVATE_VAULT_TOOLS
        ]

    def wants_llm_verifier(self) -> bool:
        """True only for policies that genuinely require a model verification pass.
        Basic education and greetings never do (fixes the trivial-answer verifier)."""
        return self.verify_policy in (
            VerifyPolicy.BOUNDED_MODEL_VERIFIER,
            VerifyPolicy.FULL_VERIFICATION,
        )

    def telemetry(self) -> dict:
        return {
            "request_class": self.request_class.value,
            "reason_code": self.reason_code.value,
            "verify_policy": self.verify_policy.value,
            "knowledge_vault_allowed": self.knowledge_vault_allowed,
            "security_sensitive": self.security_sensitive,
        }


def classify_request(
    user_message: str,
    *,
    authority=None,
    tool_names: list[str] | None = None,
) -> TurnPolicy:
    """Classify a turn deterministically into a ``TurnPolicy``.

    Precedence is fixed and inspectable (most-specific first):
      current-time → private-document → memory-recall → operational-status →
      cyber-sensitive → effectful-tool → coding-explanation → general-educational
      → ordinary-conversation.

    ``authority`` (core.authority) only affects a cyber-sensitive turn: without an
    established authorized scope it becomes AUTHORIZATION_REQUIRED (fail-closed);
    with scope it is TOOL_REQUIRED. Never widens authority.
    """
    text = (user_message or "").lower().strip()
    security = is_security_sensitive_turn(user_message, tool_names)
    dom = classify_domain(user_message, tool_names)

    # 1. Current time/date — deterministic host clock, no vault, no verifier.
    tm = _hits(text, _TIME_MARKERS)
    if tm:
        return TurnPolicy(
            RequestClass.CURRENT_TIME, ReasonCode.DETERMINISTIC_TIME,
            VerifyPolicy.SKIP_LLM_VERIFIER, security,
            knowledge_vault_allowed=False, matched=tuple(tm),
            detail="time/date resolved from host clock",
        )

    # 2. Private-document query — the ONLY class that may touch the vault.
    pd = _hits(text, _PRIVATE_DOC_MARKERS)
    if pd:
        return TurnPolicy(
            RequestClass.PRIVATE_DOCUMENT, ReasonCode.PRIVATE_RAG,
            VerifyPolicy.GROUNDING_CHECK, security,
            knowledge_vault_allowed=True, matched=tuple(pd),
            detail="explicit reference to the user's indexed documents",
        )

    # 3. Explicit memory recall about the prior conversation / project.
    mr = _hits(text, _MEMORY_MARKERS)
    if mr:
        return TurnPolicy(
            RequestClass.MEMORY_RECALL, ReasonCode.MEMORY_RECALL,
            VerifyPolicy.DETERMINISTIC_CHECKS_ONLY, security,
            knowledge_vault_allowed=False, matched=tuple(mr),
            detail="explicit recall of prior conversation/project",
        )

    # 4. Operational status of JARVIS itself.
    op = _hits(text, _OPERATIONAL_MARKERS)
    if op:
        return TurnPolicy(
            RequestClass.OPERATIONAL_STATUS, ReasonCode.OPERATIONAL_QUERY,
            VerifyPolicy.EVIDENCE_REFERENCE_CHECK, security,
            knowledge_vault_allowed=False, matched=tuple(op),
            detail="operational/runtime status query",
        )

    # 5. Cyber-sensitive (offensive/operational security). Authorization decides
    #    whether tools may run; the answer gets a bounded model verifier.
    if security or dom.domain in (TaskDomain.CYBER_PURPLE, TaskDomain.DFIR):
        authorized = _authorization_established(authority)
        reason = ReasonCode.TOOL_REQUIRED if authorized else ReasonCode.AUTHORIZATION_REQUIRED
        return TurnPolicy(
            RequestClass.CYBER_SENSITIVE, reason,
            VerifyPolicy.BOUNDED_MODEL_VERIFIER, True,
            knowledge_vault_allowed=False, matched=dom.matched,
            detail=f"security-sensitive; authorized={authorized}",
        )

    # 6. Effectful tool request (run/open/write/scan/…). Full verification + HITL
    #    downstream; the executor still gates the actual action.
    ef = _hits(text, _EFFECTFUL_MARKERS)
    if ef:
        return TurnPolicy(
            RequestClass.EFFECTFUL_TOOL, ReasonCode.TOOL_REQUIRED,
            VerifyPolicy.FULL_VERIFICATION, security,
            knowledge_vault_allowed=False, matched=tuple(ef),
            detail="effectful action requested (world-effect tool)",
        )

    # 7. General educational — "what is X / explain Y". An explicit educational
    #    frame wins even when a coding keyword is present ("¿Qué es una clase?"),
    #    because it is still a definitional question. Direct FAST, no vault,
    #    deterministic checks only. THIS is the POO path.
    ed = _hits(text, _EDUCATIONAL_MARKERS)
    _howto = _HOWTO_RE.search(text)
    if ed or _howto:
        _matched = tuple(ed) if ed else (f"howto:{_howto.group(0)}",)
        return TurnPolicy(
            RequestClass.GENERAL_EDUCATIONAL, ReasonCode.DIRECT_FAST,
            VerifyPolicy.DETERMINISTIC_CHECKS_ONLY, security,
            knowledge_vault_allowed=False, matched=_matched,
            detail="general educational knowledge — answered directly with FAST",
        )

    # 8. Coding request without an educational frame (refactor/debug/traceback) —
    #    direct FAST, deterministic checks only.
    if dom.domain == TaskDomain.CODER:
        return TurnPolicy(
            RequestClass.CODING_EXPLANATION, ReasonCode.DIRECT_FAST,
            VerifyPolicy.DETERMINISTIC_CHECKS_ONLY, security,
            knowledge_vault_allowed=False, matched=dom.matched,
            detail="coding explanation — answered directly",
        )

    # 9. Ordinary conversation / greeting — the lightest path.
    return TurnPolicy(
        RequestClass.ORDINARY_CONVERSATION, ReasonCode.DIRECT_FAST,
        VerifyPolicy.SKIP_LLM_VERIFIER, security,
        knowledge_vault_allowed=False, matched=tuple(_hits(text, _GREETING_MARKERS)),
        detail="ordinary conversation",
    )


def _authorization_established(authority) -> bool:
    """True when the operator authority object reports an established authorized
    scope. Fail-closed: any absence/uncertainty is treated as unauthorized."""
    if authority is None:
        return False
    try:
        # core.authority exposes scope state; treat a non-empty authorized scope
        # OR an explicit is_authorized()/has_scope() as established. Kept defensive
        # so an API shape change degrades to fail-closed, never crashes the turn.
        for attr in ("is_authorized", "has_active_scope", "has_scope"):
            fn = getattr(authority, attr, None)
            if callable(fn):
                try:
                    if bool(fn()):
                        return True
                except TypeError:
                    continue
        scopes = getattr(authority, "scopes", None)
        if scopes:
            return True
    except Exception:
        return False
    return False
