"""
core/tool_result.py — V68.1 M46 typed tool-failure envelope & context isolation.

A real interactive run showed that when a tool failed (the Knowledge Vault
torch/infer_schema fault), the raw dependency error entered the conversation and
the model *switched tool families* — inventing an unrelated Packet Tracer / XML
task. That is context contamination plus incorrect tool-error recovery.

This module defines a small, deterministic envelope so that a failed tool:
  * keeps its own identity (the failed tool cannot be silently swapped),
  * exposes only a SAFE message (never a raw dependency stack trace),
  * states whether a *bounded, single* retry is permitted,
  * carries an explicit fallback flag, and
  * is scoped to the current turn.

Nothing here talks to the network, an LLM, or the filesystem — it is pure,
bounded, ASCII, and deterministic, matching the JARVIS spine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Discriminating status values placed on tool-result dicts.
STATUS_OK = "ok"
STATUS_FAILURE = "failure"

# Error classes that are NEVER retryable — retrying cannot change the outcome
# because the fault is structural (schema / config / programming / dependency).
_NON_RETRYABLE_CLASSES: frozenset[str] = frozenset({
    "invalid_query",
    "invalid_arguments",
    "schema_error",
    "not_implemented",
    "configuration_error",
    "dependency_missing",
    "dependency_import_failed",
    "dependency_incompatibility",
    "permission_denied",
    "out_of_scope",
    "programming_error",
})

# Error classes that MAY warrant exactly one bounded retry (transient faults).
_RETRYABLE_CLASSES: frozenset[str] = frozenset({
    "timeout",
    "transient",
    "temporary_unavailable",
    "rate_limited",
})


@dataclass(frozen=True)
class ToolFailure:
    """A typed, LLM-safe description of a failed tool invocation."""

    tool: str
    error_class: str
    safe_message: str
    status: str = STATUS_FAILURE
    retryable: bool = False
    retry_after: float = 0.0
    fallback_allowed: bool = True
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        # `error` is retained for back-compat with every existing caller/test
        # that keys off ``"error" in result`` to detect failure.
        return {
            "status": self.status,
            "tool": self.tool,
            "error_class": self.error_class,
            "safe_message": self.safe_message,
            "error": self.safe_message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "fallback_allowed": self.fallback_allowed,
            "evidence_refs": list(self.evidence_refs),
        }


def make_failure(
    tool: str,
    error_class: str,
    safe_message: str,
    *,
    retryable: bool | None = None,
    retry_after: float = 0.0,
    fallback_allowed: bool = True,
    evidence_refs: tuple[str, ...] = (),
) -> dict:
    """Build a failure envelope dict. ``retryable`` defaults from error_class."""
    if retryable is None:
        retryable = error_class in _RETRYABLE_CLASSES and error_class not in _NON_RETRYABLE_CLASSES
    # Structural classes can never be flagged retryable, even if a caller asks.
    if error_class in _NON_RETRYABLE_CLASSES:
        retryable = False
    return ToolFailure(
        tool=tool,
        error_class=error_class,
        safe_message=_sanitize(safe_message),
        retryable=bool(retryable),
        retry_after=float(retry_after),
        fallback_allowed=bool(fallback_allowed),
        evidence_refs=tuple(evidence_refs),
    ).to_dict()


def is_failure(result) -> bool:
    """True if a tool result represents a failure (typed envelope or legacy)."""
    if not isinstance(result, dict):
        return False
    if result.get("status") == STATUS_FAILURE:
        return True
    return "error" in result


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map an arbitrary tool exception to (error_class, safe_message).

    Never returns a raw stack trace or dependency internals; the raw text is
    logged by the caller, not surfaced to the model.
    """
    name = type(exc).__name__
    low = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in low or "timed out" in low:
        return ("timeout", "The tool did not complete in time.")
    if isinstance(exc, (ValueError, TypeError, KeyError)) and (
        "schema" in low or "signature" in low or "unsupported type" in low
    ):
        return ("schema_error", "The tool received or produced data it could not process.")
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return ("dependency_missing", "A dependency required by the tool is unavailable.")
    if isinstance(exc, PermissionError):
        return ("permission_denied", "The tool was not permitted to perform this action.")
    if isinstance(exc, FileNotFoundError):
        return ("not_found", "A resource the tool needed was not found.")
    # Default: an internal, non-retryable programming/runtime fault.
    return ("internal_error", f"The tool failed with an internal {name}.")


def _sanitize(message: str) -> str:
    """Collapse a message to a single bounded line with no stack-trace shape."""
    if not isinstance(message, str):
        message = str(message)
    # A stack trace is inherently multi-line; keep only the first meaningful line.
    line = next((ln.strip() for ln in message.splitlines() if ln.strip()), "")
    if len(line) > 300:
        line = line[:297] + "..."
    return line or "The tool failed."


def recovery_guidance(failure: dict) -> str:
    """A short instruction block appended to a failed tool result in the prompt.

    This is the context-isolation boundary: it tells the model to stay on the
    SAME tool/topic, forbids switching to an unrelated tool family because of an
    error, and states the only two valid next moves.
    """
    tool = failure.get("tool", "the tool")
    msg = failure.get("safe_message") or failure.get("error") or "The tool failed."
    fallback_allowed = failure.get("fallback_allowed", True)
    retryable = failure.get("retryable", False)

    lines = [
        f"TOOL_FAILURE: `{tool}` failed. Reason: {msg}",
        "This failure is scoped to THIS request only.",
        "Do NOT switch to an unrelated tool or task because of this error.",
        f"Do NOT call a different tool family than `{tool}` unless the user's "
        "request independently requires it.",
    ]
    if retryable:
        lines.append(f"You may retry `{tool}` at most once if it is clearly transient.")
    else:
        lines.append(f"Do NOT retry `{tool}` — this failure is not transient.")
    if fallback_allowed:
        lines.append(
            "Either answer the user's ORIGINAL question from your own knowledge "
            "without this tool, or state plainly that the tool is unavailable. "
            "Briefly name the specific failure; do not invent unrelated tasks."
        )
    else:
        lines.append(
            "State plainly that the required tool is unavailable and stop; do not "
            "substitute an unrelated action."
        )
    return "\n".join(lines)
