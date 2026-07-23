"""core/response_quality.py — V69 M57.8: deterministic output quality governor.

NOT A VERIFIER
--------------
M54.6 already decided which turns warrant an LLM verifier, and ordinary knowledge
answers deliberately do not. This module never calls a model: adding a second
generation to check the first would cost more than the answer itself on a ~6 tok/s
host, and would replace one unverified text with another.

So every check here is a bounded, deterministic property of the OUTPUT ARTEFACT —
repetition, an unclosed fence, a leaked reasoning marker, a placeholder nobody
filled in. It never judges whether the answer is CORRECT, and it never performs
semantic censorship. Security and authorization checks live elsewhere and are
untouched.

WHAT IT MAY DO
--------------
  * suppress an exact duplicate fragment BEFORE it is displayed;
  * close a formatting structure the stream left open;
  * append a truthful status line (truncated / incomplete / continuation offered);
  * ask for a bounded retry ONLY before any user-visible content exists.

WHAT IT MAY NEVER DO
--------------------
Silently rewrite a completed factual answer through another model call. Once the
operator has seen text, the runtime's job is to be honest about it, not to replace
it with something that reads better.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_MIN_REPEAT_CHARS = 24        # below this a repeat is usually legitimate
_MAX_QUESTION_ECHO = 0.6      # share of the question restated verbatim
_MAX_ISSUES = 12              # bounded report


class QualityIssue(str, Enum):
    """The closed set of deterministic defects this governor can detect."""

    REPEATED_SENTENCE = "REPEATED_SENTENCE"
    REPEATED_PARAGRAPH = "REPEATED_PARAGRAPH"
    QUESTION_ECHO = "QUESTION_ECHO"
    UNRESOLVED_PLACEHOLDER = "UNRESOLVED_PLACEHOLDER"
    TOOL_JSON_LEAK = "TOOL_JSON_LEAK"
    REASONING_MARKER = "REASONING_MARKER"
    INTRO_BOILERPLATE = "INTRO_BOILERPLATE"
    LANGUAGE_DRIFT = "LANGUAGE_DRIFT"
    UNCLOSED_CODE_FENCE = "UNCLOSED_CODE_FENCE"
    TRUNCATED_AT_CAP = "TRUNCATED_AT_CAP"
    FILLER_ONLY = "FILLER_ONLY"
    BREVITY_IGNORED = "BREVITY_IGNORED"


class QualityAction(str, Enum):
    """What the runtime is permitted to do about an issue."""

    NONE = "NONE"
    SUPPRESS_FRAGMENT = "SUPPRESS_FRAGMENT"      # before display only
    CLOSE_FORMAT = "CLOSE_FORMAT"
    APPEND_STATUS = "APPEND_STATUS"
    BLOCK_DISPLAY = "BLOCK_DISPLAY"              # pre-content only
    RETRY_BOUNDED = "RETRY_BOUNDED"              # pre-content only


# Actions that may only be taken BEFORE the operator has seen anything.
_PRE_CONTENT_ONLY: frozenset[QualityAction] = frozenset({
    QualityAction.BLOCK_DISPLAY, QualityAction.RETRY_BOUNDED,
})

# ── Detection vocab (bounded, deterministic) ─────────────────────────────────
_PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|FIXME|XXX|TBD)\b"
    r"|\[(?:insert|inserta|tu\s+\w+|your\s+\w+|placeholder|pendiente)[^\]]*\]"
    r"|\{\{\s*\w+\s*\}\}"
    r"|<(?:insert|placeholder|your[_ ]\w+)>"
    r"|\blorem ipsum\b",
    re.IGNORECASE)
# A tool call that leaked into prose. Narrow: real prose about JSON must not trip.
_TOOL_JSON_RE = re.compile(
    r"<tool_call>|</tool_call>|<\|tool"
    r"|\{\s*\"(?:name|tool_name|function)\"\s*:\s*\"[^\"]+\"\s*,\s*"
    r"\"(?:arguments|parameters|args)\"\s*:",
    re.IGNORECASE)
_REASONING_RE = re.compile(
    r"<think>|</think>|\[THINKING\]|\[/THINKING\]|<\|thought"
    r"|^\s*(?:okay|ok|alright|bien|vale)[,;]?\s+(?:let me think|let's think|"
    r"pensemos|déjame pensar|dejame pensar)\b",
    re.IGNORECASE | re.MULTILINE)
_INTRO_RE = re.compile(
    r"^\s*(?:claro|por supuesto|desde luego|con mucho gusto|certainly|of course|"
    r"sure|absolutely|great question|excelente pregunta)\b[^.!?]{0,120}[.!?]",
    re.IGNORECASE)
_FILLER_RE = re.compile(
    r"^\s*(?:claro|ok|okay|vale|entendido|understood|sure|hmm|bien)[\s.!,…]*$",
    re.IGNORECASE)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_FENCE_RE = re.compile(r"^\s{0,3}```", re.MULTILINE)
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]{3,}")


@dataclass(frozen=True)
class QualityFinding:
    """One deterministic defect and the bounded action it permits."""

    issue: QualityIssue
    action: QualityAction
    detail: str = ""

    def snapshot(self) -> dict:
        return {"issue": self.issue.value, "action": self.action.value,
                "detail": self.detail[:80]}


@dataclass
class QualityReport:
    """The bounded verdict for ONE answer. Content-free except short details."""

    findings: tuple[QualityFinding, ...] = ()
    repaired_text: str | None = None
    status_note: str = ""
    counters: dict = field(default_factory=dict)

    @property
    def issues(self) -> tuple[QualityIssue, ...]:
        return tuple(f.issue for f in self.findings)

    def has(self, issue: QualityIssue) -> bool:
        return issue in self.issues

    def actions(self) -> tuple[QualityAction, ...]:
        return tuple(f.action for f in self.findings)

    def snapshot(self) -> dict:
        return {
            "findings": [f.snapshot() for f in self.findings],
            "repaired": self.repaired_text is not None,
            "counters": dict(self.counters),
        }


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def find_repetition(text: str) -> list[QualityFinding]:
    """Exact repeated sentences / paragraphs above a meaningful length."""
    out: list[QualityFinding] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        if len(sentence) < _MIN_REPEAT_CHARS:
            continue
        key = _norm(sentence)
        if key in seen:
            out.append(QualityFinding(QualityIssue.REPEATED_SENTENCE,
                                      QualityAction.SUPPRESS_FRAGMENT,
                                      f"{len(sentence)} chars"))
            break
        seen.add(key)
    seen_p: set[str] = set()
    for para in _paragraphs(text):
        if len(para) < _MIN_REPEAT_CHARS * 2:
            continue
        key = _norm(para)
        if key in seen_p:
            out.append(QualityFinding(QualityIssue.REPEATED_PARAGRAPH,
                                      QualityAction.SUPPRESS_FRAGMENT,
                                      f"{len(para)} chars"))
            break
        seen_p.add(key)
    return out


def _question_echo(text: str, question: str) -> bool:
    """Does the opening restate most of the question verbatim?"""
    q_terms = {w.lower() for w in _WORD_RE.findall(question or "")}
    if len(q_terms) < 4:
        return False
    opening = " ".join(_sentences(text)[:1])
    if not opening:
        return False
    o_terms = {w.lower() for w in _WORD_RE.findall(opening)}
    if not o_terms:
        return False
    return len(q_terms & o_terms) / float(len(q_terms)) >= _MAX_QUESTION_ECHO


def close_open_fence(text: str) -> tuple[str, bool]:
    """Close an odd number of ``` fences. Repair, not rewrite: the answer's words
    are untouched — only the structure the stream left open is completed."""
    if len(_FENCE_RE.findall(text or "")) % 2 == 0:
        return text, False
    suffix = "" if (text or "").endswith("\n") else "\n"
    return f"{text}{suffix}```", True


def evaluate_answer(text: str, *, question: str = "", shape=None,
                    language: str = "es", truncated_by_cap: bool = False,
                    pre_content: bool = False) -> QualityReport:
    """Run every deterministic check over a finished answer. Never calls a model.

    ``pre_content`` says whether the operator has seen anything yet: it is the ONLY
    condition under which a blocking or retrying action is permitted.
    """
    findings: list[QualityFinding] = []
    counters: dict = {}
    body = text or ""

    findings.extend(find_repetition(body))

    if _PLACEHOLDER_RE.search(body):
        findings.append(QualityFinding(
            QualityIssue.UNRESOLVED_PLACEHOLDER,
            QualityAction.RETRY_BOUNDED if pre_content else QualityAction.APPEND_STATUS,
            "unfilled placeholder"))
    if _TOOL_JSON_RE.search(body):
        findings.append(QualityFinding(
            QualityIssue.TOOL_JSON_LEAK,
            QualityAction.BLOCK_DISPLAY if pre_content else QualityAction.SUPPRESS_FRAGMENT,
            "tool-call syntax in prose"))
    if _REASONING_RE.search(body):
        findings.append(QualityFinding(
            QualityIssue.REASONING_MARKER,
            QualityAction.BLOCK_DISPLAY if pre_content else QualityAction.SUPPRESS_FRAGMENT,
            "reasoning marker"))
    if _INTRO_RE.match(body):
        findings.append(QualityFinding(QualityIssue.INTRO_BOILERPLATE,
                                       QualityAction.NONE, "courtesy preamble"))
    if body.strip() and _FILLER_RE.match(body.strip()):
        findings.append(QualityFinding(
            QualityIssue.FILLER_ONLY,
            QualityAction.RETRY_BOUNDED if pre_content else QualityAction.APPEND_STATUS,
            "no substantive content"))
    if question and _question_echo(body, question):
        findings.append(QualityFinding(QualityIssue.QUESTION_ECHO,
                                       QualityAction.NONE, "restates the question"))

    repaired, closed = close_open_fence(body)
    if closed:
        findings.append(QualityFinding(QualityIssue.UNCLOSED_CODE_FENCE,
                                       QualityAction.CLOSE_FORMAT, "``` left open"))
    if truncated_by_cap:
        findings.append(QualityFinding(QualityIssue.TRUNCATED_AT_CAP,
                                       QualityAction.APPEND_STATUS,
                                       "stopped at the token budget"))

    # Language drift: the answer is not in the language the turn committed to.
    drift = detect_language_drift(body, language)
    if drift:
        findings.append(QualityFinding(QualityIssue.LANGUAGE_DRIFT,
                                       QualityAction.NONE, f"expected {language}"))

    # An explicit brevity request that the answer plainly ignored.
    if shape is not None and getattr(shape, "explicit_override", False):
        target = int(getattr(shape, "max_output_tokens", 0) or 0)
        if target and len(body) // 4 > target * 1.75:
            findings.append(QualityFinding(QualityIssue.BREVITY_IGNORED,
                                           QualityAction.APPEND_STATUS,
                                           "answer exceeded the requested brevity"))

    counters["sentences"] = len(_sentences(body))
    counters["chars"] = len(body)
    findings = findings[:_MAX_ISSUES]
    return QualityReport(findings=tuple(findings),
                         repaired_text=repaired if closed else None,
                         counters=counters)


def detect_language_drift(text: str, expected: str) -> bool:
    """True when the answer is confidently in a DIFFERENT language than committed.

    Reuses the existing deterministic detector so there is one language authority;
    an ambiguous or short answer is never called drift.
    """
    try:
        from core.language_context import detect_text_language
        detected, confidence = detect_text_language(text or "")
    except Exception:  # noqa: BLE001
        return False
    if detected is None or confidence < 0.7:
        return False
    return detected != (str(expected or "es").lower()[:2])


def suppress_duplicate(fragment_text: str, recent: list[str]) -> bool:
    """Should this fragment be withheld from display as an exact duplicate?

    Pre-display only — this is the one repetition action that is safe, because
    nothing has been shown yet.
    """
    key = _norm(fragment_text)
    if len(key) < _MIN_REPEAT_CHARS:
        return False
    return key in {_norm(r) for r in recent}


_STATUS_NOTES = {
    QualityIssue.TRUNCATED_AT_CAP: (
        "(Respuesta acortada por el límite de longitud.)",
        "(Answer shortened by the length budget.)"),
    QualityIssue.UNRESOLVED_PLACEHOLDER: (
        "(La respuesta contiene un marcador sin completar.)",
        "(The answer contains an unfilled placeholder.)"),
    QualityIssue.FILLER_ONLY: (
        "(No obtuve una respuesta con contenido; inténtalo de nuevo.)",
        "(I did not get a substantive answer; please try again.)"),
    QualityIssue.BREVITY_IGNORED: (
        "(La respuesta salió más larga de lo pedido.)",
        "(The answer came out longer than requested.)"),
}


def status_note(report: QualityReport, *, language: str = "es") -> str:
    """The bounded, truthful status line for a report, or ``""``.

    One note maximum: a stack of parentheses is noise, and the most severe issue
    is the one the operator needs.
    """
    en = str(language or "es").lower().startswith("en")
    for issue in (QualityIssue.FILLER_ONLY, QualityIssue.UNRESOLVED_PLACEHOLDER,
                  QualityIssue.TRUNCATED_AT_CAP, QualityIssue.BREVITY_IGNORED):
        if report.has(issue):
            pair = _STATUS_NOTES[issue]
            return pair[1] if en else pair[0]
    return ""


# ── Bounded process counters for runtime health ──────────────────────────────
_counters: dict = {
    "repetition_suppressions": 0, "placeholder_blocks": 0,
    "incomplete_format_repairs": 0, "reasoning_marker_blocks": 0,
    "tool_json_blocks": 0, "truncation_notices": 0, "evaluations": 0,
}
_ISSUE_COUNTER = {
    QualityIssue.REPEATED_SENTENCE: "repetition_suppressions",
    QualityIssue.REPEATED_PARAGRAPH: "repetition_suppressions",
    QualityIssue.UNRESOLVED_PLACEHOLDER: "placeholder_blocks",
    QualityIssue.UNCLOSED_CODE_FENCE: "incomplete_format_repairs",
    QualityIssue.REASONING_MARKER: "reasoning_marker_blocks",
    QualityIssue.TOOL_JSON_LEAK: "tool_json_blocks",
    QualityIssue.TRUNCATED_AT_CAP: "truncation_notices",
}


def record_report(report: QualityReport) -> None:
    """Fold a report into the bounded process counters. Never raises."""
    try:
        _counters["evaluations"] += 1
        for issue in report.issues:
            key = _ISSUE_COUNTER.get(issue)
            if key:
                _counters[key] += 1
    except Exception:  # noqa: BLE001
        pass


def quality_counters() -> dict:
    return dict(_counters)


def reset_quality_counters() -> None:
    for key in _counters:
        _counters[key] = 0
