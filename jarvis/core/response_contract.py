"""core/response_contract.py — V69 M57.1: deterministic adaptive response contracts.

WHAT PROBLEM THIS SOLVES
------------------------
M55/M56 made the FIRST token fast (warm ~1.2 s). They did nothing about the SECOND
half of perceived latency: sustained generation on this 15 W CPU runs at ~5.2-6.4
tokens/second, so answer LENGTH is now the dominant cost:

    50 tokens  -> ~8-10 s        100 tokens -> ~16-20 s      200 tokens -> ~31-39 s

A greeting that costs 200 tokens costs the operator half a minute. The fix is not a
different model — it is answering a short question shortly.

WHAT A CONTRACT IS (AND IS NOT)
-------------------------------
A response contract describes HOW the already-selected model should answer. It is
NOT a second model router and it never touches authority:

  * it never chooses the model, the role, or the transport (core.model_router and
    core.fast_path keep that authority);
  * it never widens tool eligibility, RAG eligibility, verification policy or risk
    class — those are INHERITED verbatim from the existing
    :class:`~core.turn_policy.TurnPolicy` and merely carried for inspection;
  * it never inspects or emits chain of thought; the reason code is a deterministic
    policy label, not a model rationalisation.

It composes signals the live turn ALREADY computed (request class, reason code,
verify policy, security flag, routed role, active language, power profile) plus two
new deterministic signals: explicit operator brevity/detail instructions in the
turn itself, and the session response profile set by ``/brief`` | ``/standard`` |
``/detailed``.

Pure, side-effect free, no I/O, no LLM — fully unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Absolute generation bounds. NO contract, override, adaptation or operator command
# may leave this window — an unbounded num_predict at ~6 tok/s is a hung turn.
HARD_MIN_OUTPUT_TOKENS = 16
HARD_MAX_OUTPUT_TOKENS = 1024


class ResponseContract(str, Enum):
    """How the answer should be shaped. Ten closed values — never free-form."""

    INSTANT = "INSTANT"                        # greeting / acknowledgement
    BRIEF = "BRIEF"                            # one direct answer + one example
    STANDARD = "STANDARD"                      # definition + core points + example
    TECHNICAL = "TECHNICAL"                    # structured, deeper explanation
    DEEP = "DEEP"                              # DEEP-role reasoning turn
    STRUCTURED = "STRUCTURED"                  # explicitly enumerated/comparative
    CODE = "CODE"                              # code produced or explained
    DOCUMENT_GROUNDED = "DOCUMENT_GROUNDED"    # answer bound to indexed evidence
    OPERATIONAL = "OPERATIONAL"                # runtime/operational state answer
    ERROR_RECOVERY = "ERROR_RECOVERY"          # recovering from a failed/partial turn


class ContractReason(str, Enum):
    """WHY this contract was selected. Inspectable diagnostics — never reasoning."""

    GREETING_SMALLTALK = "GREETING_SMALLTALK"
    SIMPLE_HOWTO = "SIMPLE_HOWTO"
    GENERAL_EDUCATIONAL = "GENERAL_EDUCATIONAL"
    EXPLICIT_BRIEF_REQUEST = "EXPLICIT_BRIEF_REQUEST"
    EXPLICIT_DETAIL_REQUEST = "EXPLICIT_DETAIL_REQUEST"
    SESSION_PROFILE_BRIEF = "SESSION_PROFILE_BRIEF"
    SESSION_PROFILE_STANDARD = "SESSION_PROFILE_STANDARD"
    SESSION_PROFILE_DETAILED = "SESSION_PROFILE_DETAILED"
    ENUMERATION_REQUEST = "ENUMERATION_REQUEST"
    CODING_TASK = "CODING_TASK"
    PRIVATE_DOCUMENT_EVIDENCE = "PRIVATE_DOCUMENT_EVIDENCE"
    MEMORY_RECALL = "MEMORY_RECALL"
    OPERATIONAL_STATE = "OPERATIONAL_STATE"
    DEEP_ROLE_INHERITED = "DEEP_ROLE_INHERITED"
    SECURITY_SENSITIVE_PROCEDURE = "SECURITY_SENSITIVE_PROCEDURE"
    EFFECTFUL_ACTION = "EFFECTFUL_ACTION"
    CONTINUATION_EXPANSION = "CONTINUATION_EXPANSION"
    RECOVERY_AFTER_INCOMPLETE = "RECOVERY_AFTER_INCOMPLETE"
    DETERMINISTIC_ANSWER = "DETERMINISTIC_ANSWER"


class FormattingPolicy(str, Enum):
    """What structure the answer may use. A greeting with an H2 heading is noise."""

    PLAIN = "PLAIN"                # sentences only: no headings, no bullets, no fences
    LIGHT_LIST = "LIGHT_LIST"      # short bullets allowed, no headings
    SECTIONS = "SECTIONS"          # headings + bullets allowed
    CODE_FIRST = "CODE_FIRST"      # fenced code plus short prose
    EVIDENCE = "EVIDENCE"          # grounded prose that must reference its evidence


class SpeechPolicy(str, Enum):
    """How much of the answer progressive TTS may speak (M57.4 consumes this)."""

    SPEAK_FULL = "SPEAK_FULL"      # every prose sentence
    SPEAK_PROSE = "SPEAK_PROSE"    # prose only — code blocks are never spoken
    SPEAK_LEAD = "SPEAK_LEAD"      # only the leading sentences of a long answer
    SILENT = "SILENT"              # nothing is spoken for this turn


class ResponseProfile(str, Enum):
    """Session-level operator preference set by /brief, /standard, /detailed."""

    AUTO = "AUTO"
    BRIEF = "BRIEF"
    STANDARD = "STANDARD"
    DETAILED = "DETAILED"


# ── Deterministic operator-instruction vocab (ES + EN) ────────────────────────
# Explicit instructions about ANSWER LENGTH only. Deliberately narrow: these must
# not fire on ordinary prose ("resumen ejecutivo del informe" is a topic, not an
# instruction), so each marker is an imperative or an adverbial the user aims at
# JARVIS. Accent-optional throughout — the operator types without accents.
_BRIEF_MARKERS: tuple[str, ...] = (
    "hazlo corto", "hazlo mas corto", "hazlo más corto", "mas corto", "más corto",
    "se breve", "sé breve", "brevemente", "en breve", "de forma breve",
    "resumelo", "resúmelo", "resumido", "en resumen", "mas conciso", "más conciso",
    "conciso", "cortito", "sin rodeos", "al grano", "solo lo esencial",
    "sólo lo esencial", "una frase", "en una linea", "en una línea",
    "be brief", "briefly", "in brief", "keep it short", "make it short",
    "make it shorter", "shorter", "in short", "tl;dr", "tldr", "summarize it",
    "summarise it", "sum it up", "one sentence", "concise", "be concise",
    "just the essentials", "get to the point",
)
_DETAIL_MARKERS: tuple[str, ...] = (
    "en detalle", "con detalle", "detalladamente", "con mas detalle",
    "con más detalle", "mas detalle", "más detalle", "a fondo", "en profundidad",
    "profundiza", "explayate", "explaýate", "extiendete", "extiéndete",
    "paso a paso", "exhaustivo", "completo y detallado", "mas profundo",
    "más profundo", "todos los detalles",
    "in detail", "in depth", "in-depth", "detailed", "more detail", "elaborate",
    "thoroughly", "step by step", "step-by-step", "comprehensive", "deep dive",
    "full explanation", "explain fully",
)
# Enumerations/comparisons genuinely benefit from structure even when short.
_STRUCTURE_MARKERS: tuple[str, ...] = (
    "lista", "listame", "lístame", "enumera", "enumérame", "enumerame",
    "cuales son los", "cuáles son los", "cuales son las", "cuáles son las",
    "compara", "comparación", "comparacion", "diferencias entre", "pros y contras",
    "ventajas y desventajas", "list ", "list the", "enumerate", "compare",
    "comparison", "pros and cons", "differences between", "advantages and",
)
# A request to WRITE code (not merely to explain a concept).
_CODE_REQUEST_RE = re.compile(
    r"\b(?:escribe|escríbeme|escribeme|crea|créame|creame|dame|gener[ao]|"
    r"implementa|programa|refactoriza|corrige|arregla|depura)\b[^.?!]{0,40}"
    r"\b(?:c[oó]digo|funci[oó]n|clase|script|snippet|ejemplo\s+de\s+c[oó]digo|"
    r"programa)\b"
    r"|\b(?:write|create|give me|generate|implement|refactor|fix|debug)\b"
    r"[^.?!]{0,40}\b(?:code|function|class|script|snippet|program)\b"
    r"|```"
)
# Deterministic continuation/expansion intents (M57.7 owns the workflow; the
# contract layer only needs to know a turn IS one so it does not restart from
# INSTANT).
_CONTINUATION_MARKERS: tuple[str, ...] = (
    "continua", "continúa", "sigue", "adelante con eso", "y luego",
    "mas detalles", "más detalles", "amplia", "amplía", "expande",
    "continue", "go on", "keep going", "more details", "expand on",
    "elaborate on that", "tell me more",
)


def _hits(text: str, markers: tuple[str, ...]) -> list[str]:
    return [m for m in markers if m in text]


@dataclass(frozen=True)
class ResponseShape:
    """The deterministic answer shape for ONE turn. Pure data, fully bounded.

    ``verify_policy`` / ``tools_allowed`` / ``rag_allowed`` / ``security_sensitive``
    are INHERITED copies carried for inspection and for the budget layer — this
    object is never the authority that grants them.
    """

    contract: ResponseContract
    reason: ContractReason
    language: str = "es"

    # Answer-size targets (advisory, used for the style directive and diagnostics).
    target_sentences_min: int = 1
    target_sentences_max: int = 3
    first_sentence_max_chars: int = 160

    # Generation budget window (M57.2 clamps and adapts inside this window).
    min_output_tokens: int = HARD_MIN_OUTPUT_TOKENS
    base_output_tokens: int = 96
    max_output_tokens: int = 128
    target_completion_ms: int = 16000

    formatting: FormattingPolicy = FormattingPolicy.PLAIN
    speech: SpeechPolicy = SpeechPolicy.SPEAK_FULL
    continuation_allowed: bool = True
    deterministic_checks: tuple[str, ...] = ()

    # Inherited, never granted here.
    verify_policy: str = ""
    tools_allowed: bool = False
    rag_allowed: bool = False
    security_sensitive: bool = False

    explicit_override: bool = False
    power_profile: str = "UNKNOWN"
    matched: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    # ── style ────────────────────────────────────────────────────────────────
    def style_directive(self) -> str:
        """M57.3.1 — the bounded first-party ANSWER-STYLE instruction.

        Stylistic only: it constrains shape and opening, never content, never
        truth, never scope. Emitted in the turn's active language so it cannot
        itself cause language drift.
        """
        return _STYLE_DIRECTIVES[self.contract]["en" if self.language.startswith("en")
                                                else "es"]

    def allows_code_block(self) -> bool:
        return self.formatting in (FormattingPolicy.CODE_FIRST, FormattingPolicy.SECTIONS)

    def telemetry(self) -> dict:
        """Bounded, content-free diagnostics (runtime health + /response-status)."""
        return {
            "contract": self.contract.value,
            "selection_reason": self.reason.value,
            "language": self.language,
            "explicit_override": self.explicit_override,
            "formatting": self.formatting.value,
            "speech": self.speech.value,
            "continuation_allowed": self.continuation_allowed,
            "min_output_tokens": self.min_output_tokens,
            "base_output_tokens": self.base_output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "target_completion_ms": self.target_completion_ms,
            "verify_policy": self.verify_policy,
            "tools_allowed": self.tools_allowed,
            "rag_allowed": self.rag_allowed,
            "security_sensitive": self.security_sensitive,
            "power_profile": self.power_profile,
        }


# ── The closed contract table ─────────────────────────────────────────────────
# Token windows are STARTING points calibrated against the measured ~5.2-6.4 tok/s
# host throughput (M56) and the M57 latency targets; the adaptive layer (M57.8.1)
# moves inside the window, never outside it.
_BASE_SHAPES: dict[ResponseContract, dict] = {
    ResponseContract.INSTANT: dict(
        target_sentences_min=1, target_sentences_max=2, first_sentence_max_chars=120,
        min_output_tokens=24, base_output_tokens=40, max_output_tokens=64,
        target_completion_ms=8000, formatting=FormattingPolicy.PLAIN,
        speech=SpeechPolicy.SPEAK_FULL, continuation_allowed=False,
        deterministic_checks=("no_repetition", "no_reasoning_marker", "language_match"),
    ),
    ResponseContract.BRIEF: dict(
        target_sentences_min=2, target_sentences_max=4, first_sentence_max_chars=160,
        min_output_tokens=64, base_output_tokens=96, max_output_tokens=128,
        target_completion_ms=16000, formatting=FormattingPolicy.PLAIN,
        speech=SpeechPolicy.SPEAK_FULL, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "brevity_respected"),
    ),
    ResponseContract.STANDARD: dict(
        target_sentences_min=3, target_sentences_max=8, first_sentence_max_chars=180,
        min_output_tokens=96, base_output_tokens=160, max_output_tokens=224,
        target_completion_ms=28000, formatting=FormattingPolicy.LIGHT_LIST,
        speech=SpeechPolicy.SPEAK_PROSE, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "format_closed"),
    ),
    ResponseContract.TECHNICAL: dict(
        target_sentences_min=6, target_sentences_max=16, first_sentence_max_chars=200,
        min_output_tokens=160, base_output_tokens=256, max_output_tokens=384,
        target_completion_ms=60000, formatting=FormattingPolicy.SECTIONS,
        speech=SpeechPolicy.SPEAK_LEAD, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "format_closed"),
    ),
    ResponseContract.STRUCTURED: dict(
        target_sentences_min=4, target_sentences_max=12, first_sentence_max_chars=180,
        min_output_tokens=128, base_output_tokens=224, max_output_tokens=352,
        target_completion_ms=48000, formatting=FormattingPolicy.SECTIONS,
        speech=SpeechPolicy.SPEAK_LEAD, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "format_closed"),
    ),
    ResponseContract.CODE: dict(
        target_sentences_min=1, target_sentences_max=8, first_sentence_max_chars=180,
        min_output_tokens=128, base_output_tokens=288, max_output_tokens=512,
        target_completion_ms=72000, formatting=FormattingPolicy.CODE_FIRST,
        speech=SpeechPolicy.SPEAK_PROSE, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "format_closed"),
    ),
    ResponseContract.DOCUMENT_GROUNDED: dict(
        target_sentences_min=2, target_sentences_max=10, first_sentence_max_chars=200,
        min_output_tokens=96, base_output_tokens=176, max_output_tokens=288,
        target_completion_ms=40000, formatting=FormattingPolicy.EVIDENCE,
        speech=SpeechPolicy.SPEAK_PROSE, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "evidence_present"),
    ),
    ResponseContract.OPERATIONAL: dict(
        target_sentences_min=1, target_sentences_max=6, first_sentence_max_chars=180,
        min_output_tokens=64, base_output_tokens=128, max_output_tokens=192,
        target_completion_ms=24000, formatting=FormattingPolicy.LIGHT_LIST,
        speech=SpeechPolicy.SPEAK_PROSE, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "language_match",
                              "evidence_present"),
    ),
    ResponseContract.DEEP: dict(
        target_sentences_min=6, target_sentences_max=20, first_sentence_max_chars=200,
        min_output_tokens=192, base_output_tokens=384, max_output_tokens=640,
        target_completion_ms=120000, formatting=FormattingPolicy.SECTIONS,
        speech=SpeechPolicy.SPEAK_LEAD, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_placeholder", "no_tool_json",
                              "no_reasoning_marker", "format_closed"),
    ),
    ResponseContract.ERROR_RECOVERY: dict(
        target_sentences_min=1, target_sentences_max=3, first_sentence_max_chars=160,
        min_output_tokens=24, base_output_tokens=64, max_output_tokens=96,
        target_completion_ms=12000, formatting=FormattingPolicy.PLAIN,
        speech=SpeechPolicy.SPEAK_FULL, continuation_allowed=True,
        deterministic_checks=("no_repetition", "no_reasoning_marker", "language_match"),
    ),
}

# ── M57.3.1 first-sentence / answer-style directives ─────────────────────────
# BOUNDED and STYLISTIC. They tell the model to put the answer first and skip the
# "Claro, con mucho gusto te explicaré..." preamble that costs 6-8 seconds of pure
# courtesy at ~6 tok/s. No subject knowledge is hardcoded anywhere.
_ANSWER_FIRST_ES = (
    "Empieza con la respuesta directa en la primera frase (la fórmula, el valor o "
    "la definición). No repitas la pregunta, no anuncies lo que vas a explicar y "
    "no empieces con fórmulas de cortesía largas. La explicación va después."
)
_ANSWER_FIRST_EN = (
    "Open with the direct answer in the first sentence (the formula, value or "
    "definition). Do not restate the question, do not announce what you are about "
    "to explain, and do not open with a long courtesy preamble. Explanation follows."
)
_STYLE_DIRECTIVES: dict[ResponseContract, dict[str, str]] = {
    ResponseContract.INSTANT: {
        "es": ("ESTILO: responde en una o dos frases cortas y naturales. Sin "
               "títulos, sin viñetas, sin código, sin listas."),
        "en": ("STYLE: reply in one or two short, natural sentences. No headings, "
               "no bullets, no code, no lists."),
    },
    ResponseContract.BRIEF: {
        "es": ("ESTILO: respuesta breve, de 2 a 4 frases. " + _ANSWER_FIRST_ES +
               " Añade como mucho un ejemplo corto. Sin títulos ni viñetas."),
        "en": ("STYLE: brief answer, 2 to 4 sentences. " + _ANSWER_FIRST_EN +
               " Add at most one short example. No headings, no bullets."),
    },
    ResponseContract.STANDARD: {
        "es": ("ESTILO: respuesta compacta. " + _ANSWER_FIRST_ES +
               " Después, los puntos esenciales y un ejemplo pequeño. Viñetas "
               "cortas si ayudan; sin títulos."),
        "en": ("STYLE: compact answer. " + _ANSWER_FIRST_EN +
               " Then the essential points and one small example. Short bullets if "
               "they help; no headings."),
    },
    ResponseContract.TECHNICAL: {
        "es": ("ESTILO: explicación técnica estructurada. " + _ANSWER_FIRST_ES +
               " Después organiza en secciones cortas. Evita relleno y repetición."),
        "en": ("STYLE: structured technical explanation. " + _ANSWER_FIRST_EN +
               " Then organise into short sections. Avoid filler and repetition."),
    },
    ResponseContract.STRUCTURED: {
        "es": ("ESTILO: responde con una lista o comparación clara y ordenada. "
               "Una línea por elemento, sin párrafos largos de introducción."),
        "en": ("STYLE: answer with a clear, ordered list or comparison. One line "
               "per item, no long introductory paragraph."),
    },
    ResponseContract.CODE: {
        "es": ("ESTILO: primero el código en un bloque cerrado con ``` y su "
               "lenguaje; después una explicación breve. Sin introducción larga."),
        "en": ("STYLE: code first, in a closed ``` block with its language; then a "
               "short explanation. No long introduction."),
    },
    ResponseContract.DOCUMENT_GROUNDED: {
        "es": ("ESTILO: responde solo con lo que respalda la evidencia recuperada "
               "e indica de qué documento procede. Si la evidencia no lo cubre, "
               "dilo explícitamente en vez de completarlo con conocimiento general."),
        "en": ("STYLE: answer only from the retrieved evidence and say which "
               "document it comes from. If the evidence does not cover it, say so "
               "explicitly instead of filling the gap with general knowledge."),
    },
    ResponseContract.OPERATIONAL: {
        "es": ("ESTILO: responde con el estado real observado, en frases cortas. "
               "No inventes métricas: si un dato no está disponible, dilo."),
        "en": ("STYLE: answer with the actually-observed state, in short sentences. "
               "Never invent metrics: if a value is unavailable, say so."),
    },
    ResponseContract.DEEP: {
        "es": ("ESTILO: explicación profunda pero sin relleno. " + _ANSWER_FIRST_ES +
               " Estructura en secciones y no repitas ideas ya dichas."),
        "en": ("STYLE: deep explanation without filler. " + _ANSWER_FIRST_EN +
               " Structure it into sections and do not repeat points already made."),
    },
    ResponseContract.ERROR_RECOVERY: {
        "es": ("ESTILO: retoma la respuesta incompleta desde donde se quedó, en "
               "pocas frases y sin repetir lo ya mostrado."),
        "en": ("STYLE: resume the incomplete answer from where it stopped, in a few "
               "sentences, without repeating what was already shown."),
    },
}


def parse_response_profile(value) -> ResponseProfile:
    """Parse an operator/session profile value. Unknown input yields AUTO — a typo
    must never silently pin the session to a verbosity it did not ask for."""
    if isinstance(value, ResponseProfile):
        return value
    raw = str(value or "").strip().upper().replace("-", "_")
    try:
        return ResponseProfile(raw)
    except ValueError:
        return ResponseProfile.AUTO


def detect_length_instruction(user_message: str) -> tuple[str | None, tuple[str, ...]]:
    """Deterministically detect an explicit ANSWER-LENGTH instruction in the turn.

    Returns ``("brief"|"detail"|None, matched_markers)``. When BOTH families match
    (``"explícalo en detalle pero breve"``) brevity wins: it is the cheaper, safer
    reading on a CPU-bound host, and the operator can always expand afterwards with
    an explicit continuation. The choice is fixed and documented, never random.
    """
    text = (user_message or "").lower().strip()
    if not text:
        return None, ()
    brief = _hits(text, _BRIEF_MARKERS)
    detail = _hits(text, _DETAIL_MARKERS)
    if brief:
        return "brief", tuple(brief)
    if detail:
        return "detail", tuple(detail)
    return None, ()


def is_continuation_request(user_message: str) -> bool:
    """True when the turn is a deterministic continue/expand instruction."""
    return bool(_hits((user_message or "").lower().strip(), _CONTINUATION_MARKERS))


def _role_value(model_decision) -> str:
    role = getattr(model_decision, "role", None)
    return str(getattr(role, "value", role) or "").lower()


def _enum_value(obj, attr: str) -> str:
    v = getattr(obj, attr, None)
    return str(getattr(v, "value", v) or "")


def _build(contract: ResponseContract, reason: ContractReason, *, language: str,
           turn_policy, explicit: bool, matched: tuple[str, ...], detail: str,
           power_profile: str, power_cap: int | None) -> ResponseShape:
    """Materialise a shape from the closed table plus the INHERITED turn policy."""
    base = dict(_BASE_SHAPES[contract])
    if power_cap is not None:
        # A battery profile may only REDUCE nonessential generation, and never
        # below the contract's own floor — a safety-relevant answer is not allowed
        # to become unusable because the laptop is unplugged.
        #
        # Capping only the CEILING is not a reduction: an unadapted turn generates
        # its contract BASE and would be byte-identical on battery. So a cap that
        # actually bites also scales the base by the same ratio, floored at the
        # contract minimum. A cap ABOVE the contract ceiling changes nothing — a
        # greeting is not shortened further because the laptop is unplugged.
        floor = int(base["min_output_tokens"])
        original_max = int(base["max_output_tokens"])
        cap = max(floor, int(power_cap))
        if cap < original_max:
            ratio = cap / float(original_max)
            base["max_output_tokens"] = cap
            base["base_output_tokens"] = max(
                floor, min(int(base["base_output_tokens"] * ratio), cap))
    verify = _enum_value(turn_policy, "verify_policy")
    reason_code = _enum_value(turn_policy, "reason_code")
    return ResponseShape(
        contract=contract, reason=reason, language=language,
        verify_policy=verify,
        # INHERITED verbatim. A contract never grants a tool or the vault.
        tools_allowed=(reason_code in ("TOOL_REQUIRED", "PRIVATE_RAG",
                                       "OPERATIONAL_QUERY", "MEMORY_RECALL")),
        rag_allowed=bool(getattr(turn_policy, "knowledge_vault_allowed", False)),
        security_sensitive=bool(getattr(turn_policy, "security_sensitive", False)),
        explicit_override=explicit, matched=matched, detail=detail,
        power_profile=power_profile,
        **base,
    )


def select_contract(
    user_message: str,
    *,
    turn_policy,
    model_decision=None,
    language: str = "es",
    session_profile: ResponseProfile | str = ResponseProfile.AUTO,
    continuation: bool = False,
    recovering: bool = False,
    power_policy=None,
) -> ResponseShape:
    """Select the response contract for ONE turn. Deterministic and total.

    Precedence (most authoritative first, each step inspectable):

      1. recovery after an incomplete/failed previous turn  -> ERROR_RECOVERY
      2. DEEP/CODER role already chosen by the router       -> DEEP / CODE
      3. security-sensitive or effectful turn               -> TECHNICAL / STANDARD
      4. evidence-bound classes (private doc / operational / memory)
      5. explicit operator length instruction in this turn  -> BRIEF / TECHNICAL
      6. session profile (/brief, /standard, /detailed)
      7. request class (greeting / educational / coding / how-to)

    ``power_policy`` is a :class:`~core.runtime_profile.ProfilePolicy` (or anything
    exposing ``max_generation_tokens`` / ``profile``); it may only REDUCE the token
    ceiling, never raise it.
    """
    text = (user_message or "").lower().strip()
    lang = "en" if str(language or "es").lower().startswith("en") else "es"
    profile = parse_response_profile(session_profile)
    power_cap = None
    power_name = "UNKNOWN"
    if power_policy is not None:
        try:
            power_cap = int(getattr(power_policy, "max_generation_tokens", 0)) or None
            power_name = str(_enum_value(power_policy, "profile") or "UNKNOWN")
        except Exception:  # noqa: BLE001 — power detection never breaks a turn
            power_cap, power_name = None, "UNKNOWN"

    def _mk(contract: ResponseContract, reason: ContractReason, *,
            explicit: bool = False, matched: tuple[str, ...] = (),
            detail: str = "") -> ResponseShape:
        return _build(contract, reason, language=lang, turn_policy=turn_policy,
                      explicit=explicit, matched=matched, detail=detail,
                      power_profile=power_name, power_cap=power_cap)

    request_class = _enum_value(turn_policy, "request_class")
    reason_code = _enum_value(turn_policy, "reason_code")
    role = _role_value(model_decision)
    length_kind, length_matched = detect_length_instruction(text)

    # 1. Recovering from a previous incomplete answer.
    if recovering:
        return _mk(ResponseContract.ERROR_RECOVERY,
                   ContractReason.RECOVERY_AFTER_INCOMPLETE,
                   detail="previous turn ended incomplete")

    # 2. The router already committed to a heavier role — inherit, never override.
    if role == "coder":
        return _mk(ResponseContract.CODE, ContractReason.CODING_TASK,
                   detail="router selected the coding specialist")
    if role in ("deep", "cloud"):
        return _mk(ResponseContract.DEEP, ContractReason.DEEP_ROLE_INHERITED,
                   detail=f"router selected role={role}")

    # 3. Security-sensitive / effectful turns keep their heavier shape regardless of
    #    a brevity request: a procedure that must stay correct is never compressed
    #    below its contract floor.
    if request_class == "cyber_sensitive" or getattr(turn_policy, "security_sensitive",
                                                     False):
        return _mk(ResponseContract.TECHNICAL,
                   ContractReason.SECURITY_SENSITIVE_PROCEDURE,
                   detail="security-sensitive content keeps a structured shape")
    if request_class == "effectful_tool" or reason_code == "TOOL_REQUIRED":
        return _mk(ResponseContract.STANDARD, ContractReason.EFFECTFUL_ACTION,
                   detail="effectful action turn")

    # 4. Evidence-bound classes.
    if request_class == "private_document" or reason_code == "PRIVATE_RAG":
        return _mk(ResponseContract.DOCUMENT_GROUNDED,
                   ContractReason.PRIVATE_DOCUMENT_EVIDENCE,
                   detail="answer bound to the user's indexed documents")
    if request_class == "operational_status" or reason_code == "OPERATIONAL_QUERY":
        return _mk(ResponseContract.OPERATIONAL, ContractReason.OPERATIONAL_STATE,
                   detail="operational/runtime state answer")
    if request_class == "memory_recall" or reason_code == "MEMORY_RECALL":
        return _mk(ResponseContract.BRIEF, ContractReason.MEMORY_RECALL,
                   detail="recall of prior conversation")
    if request_class == "current_time" or reason_code == "DETERMINISTIC_TIME":
        # A deterministic bypass normally answers this with ZERO model tokens; the
        # contract exists only for the path where the bypass declined.
        return _mk(ResponseContract.INSTANT, ContractReason.DETERMINISTIC_ANSWER,
                   detail="time/date question")

    # 5. Explicit operator length instruction in THIS turn — highest conversational
    #    authority, bounded by the hard ceilings.
    if length_kind == "brief":
        return _mk(ResponseContract.BRIEF, ContractReason.EXPLICIT_BRIEF_REQUEST,
                   explicit=True, matched=length_matched,
                   detail="operator explicitly asked for a short answer")
    if length_kind == "detail":
        return _mk(ResponseContract.TECHNICAL, ContractReason.EXPLICIT_DETAIL_REQUEST,
                   explicit=True, matched=length_matched,
                   detail="operator explicitly asked for more detail")

    # 6. Session profile set by /brief | /standard | /detailed.
    if profile is ResponseProfile.BRIEF:
        return _mk(ResponseContract.BRIEF, ContractReason.SESSION_PROFILE_BRIEF,
                   explicit=True, detail="session response profile = BRIEF")
    if profile is ResponseProfile.DETAILED:
        return _mk(ResponseContract.TECHNICAL,
                   ContractReason.SESSION_PROFILE_DETAILED,
                   explicit=True, detail="session response profile = DETAILED")
    if profile is ResponseProfile.STANDARD:
        return _mk(ResponseContract.STANDARD,
                   ContractReason.SESSION_PROFILE_STANDARD,
                   explicit=True, detail="session response profile = STANDARD")

    # 7. Request class / turn shape.
    if continuation or is_continuation_request(text):
        return _mk(ResponseContract.STANDARD, ContractReason.CONTINUATION_EXPANSION,
                   detail="continuation of the previous answer")
    if _CODE_REQUEST_RE.search(text):
        return _mk(ResponseContract.CODE, ContractReason.CODING_TASK,
                   detail="explicit request to produce code")
    if _hits(text, _STRUCTURE_MARKERS):
        return _mk(ResponseContract.STRUCTURED, ContractReason.ENUMERATION_REQUEST,
                   matched=tuple(_hits(text, _STRUCTURE_MARKERS)),
                   detail="enumeration/comparison benefits from structure")
    if request_class == "ordinary_conversation":
        return _mk(ResponseContract.INSTANT, ContractReason.GREETING_SMALLTALK,
                   detail="greeting / small talk")
    if request_class == "coding_explanation":
        return _mk(ResponseContract.STANDARD, ContractReason.CODING_TASK,
                   detail="coding explanation without an explicit code request")
    if request_class == "general_educational":
        # A how-to ("cómo saco la raíz cuadrada") wants the formula and one example,
        # not an essay; a definitional question gets the compact standard shape.
        if any(m.startswith("howto:") for m in getattr(turn_policy, "matched", ())):
            return _mk(ResponseContract.BRIEF, ContractReason.SIMPLE_HOWTO,
                       detail="direct how-to question")
        return _mk(ResponseContract.STANDARD, ContractReason.GENERAL_EDUCATIONAL,
                   detail="general educational question")

    # Total fallback — never raises, never returns None.
    return _mk(ResponseContract.BRIEF, ContractReason.GENERAL_EDUCATIONAL,
               detail="default conversational shape")
