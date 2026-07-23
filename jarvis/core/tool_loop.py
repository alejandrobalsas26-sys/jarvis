"""core/tool_loop.py — V69 M58.7: bounded tool-enabled generation.

WHAT M57 DELIBERATELY LEFT UNBOUNDED
------------------------------------
M57 set ``num_predict`` on the /v1 path ONLY on tool-free segments, because
truncating a tool call mid-JSON would corrupt it and break the agentic loop. Correct
— but it left the tool-ENABLED path effectively unbounded except by wall-clock: the
``while True`` loop could keep asking for another tool round until the whole turn
budget burned.

This module makes the tool loop bounded WITHOUT ever truncating a structured call:

  PHASE 1 TOOL DECISION   bounded rounds; a complete tool call is allowed to finish;
                          malformed calls are detected, never executed
  PHASE 2 TOOL EXECUTION  unchanged — ToolExecutor / authority / scope / risk / HITL
  PHASE 3 FINAL RESPONSE  when the round budget is spent, tools are DROPPED so the
                          model must produce a final answer, and THAT answer gets the
                          contract's num_predict bound (never truncating a JSON call)

Hard limits: max tool rounds, max model retries, max malformed-repair attempts, plus
the existing turn budget. This module owns the COUNTERS and the deterministic
validation; the live loop in ``core.llm`` consults it. Pure and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_MAX_TOOL_ROUNDS = 4
_MAX_MODEL_RETRIES = 2
_MAX_MALFORMED_REPAIRS = 2


class ToolTurnState(str, Enum):
    """Terminal (or in-progress) states for one tool-enabled turn."""

    TOOL_CALL_COMPLETE = "TOOL_CALL_COMPLETE"
    TOOL_CALL_MALFORMED = "TOOL_CALL_MALFORMED"
    TOOL_CALL_TIMED_OUT = "TOOL_CALL_TIMED_OUT"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_FAILED = "TOOL_FAILED"
    TOOL_RESULT_READY = "TOOL_RESULT_READY"
    FINAL_RESPONSE_COMPLETE = "FINAL_RESPONSE_COMPLETE"
    FINAL_RESPONSE_TRUNCATED = "FINAL_RESPONSE_TRUNCATED"


def validate_tool_call(name, arguments_json, eligible_names) -> tuple[bool, dict, str]:
    """Deterministically validate ONE tool call BEFORE it can execute.

    Returns ``(ok, parsed_args, reason)``. A call is rejected (``ok=False``) when:
      * the name is empty or NOT in the eligible set (a hallucinated/withheld tool
        never executes — the M58.7 "malformed tool calls never execute" guarantee);
      * the arguments are not valid JSON, or are not a JSON object.
    A rejected call yields an empty ``{}`` args and a reason; the caller must NOT
    execute it and must not guess/repair effectful arguments freely.
    """
    import json
    nm = str(name or "").strip()
    if not nm:
        return False, {}, "empty_name"
    if eligible_names is not None and nm not in set(eligible_names):
        return False, {}, "tool_not_eligible"
    raw = arguments_json if isinstance(arguments_json, str) else "{}"
    if not raw.strip():
        # An empty-argument call to a no-parameter tool is legitimate.
        return True, {}, ""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return False, {}, "malformed_json"
    if not isinstance(parsed, dict):
        return False, {}, "arguments_not_object"
    return True, parsed, ""


@dataclass
class ToolLoopBudget:
    """Bounded counters for one tool-enabled turn. Content-free.

    ``rounds`` counts loop iterations that requested tools; when it reaches
    ``max_rounds`` the loop must DROP tools and force a final response, so the loop is
    bounded by a real limit rather than only by wall-clock.
    """

    max_rounds: int = _MAX_TOOL_ROUNDS
    max_retries: int = _MAX_MODEL_RETRIES
    max_repairs: int = _MAX_MALFORMED_REPAIRS

    rounds: int = 0
    retries: int = 0
    malformed_calls: int = 0
    denied_calls: int = 0
    repairs: int = 0
    tools_used: int = 0
    final_response_tokens: int = 0
    state: ToolTurnState | None = None

    def begin_round(self) -> None:
        self.rounds += 1

    def force_final(self) -> bool:
        """True once the round budget is spent — the next leg must drop tools and
        produce a bounded final answer instead of requesting yet another round."""
        return self.rounds >= self.max_rounds

    def note_malformed(self) -> bool:
        """Record a malformed tool call. Returns True if a repair attempt remains
        (bounded); False once the repair budget is exhausted — after which the loop
        must stop offering tools rather than spin repairing."""
        self.malformed_calls += 1
        if self.repairs < self.max_repairs:
            self.repairs += 1
            return True
        return False

    def note_denied(self) -> None:
        self.denied_calls += 1

    def note_tool_used(self) -> None:
        self.tools_used += 1

    def note_retry(self) -> bool:
        """Record a model retry. Returns True while retries remain."""
        if self.retries < self.max_retries:
            self.retries += 1
            return True
        return False

    def snapshot(self) -> dict:
        return {
            "tool_rounds": self.rounds,
            "max_tool_rounds": self.max_rounds,
            "retries": self.retries,
            "malformed_calls": self.malformed_calls,
            "denied_calls": self.denied_calls,
            "repairs": self.repairs,
            "tools_used": self.tools_used,
            "final_response_tokens": self.final_response_tokens,
            "state": self.state.value if self.state else None,
        }


# ── Bounded last-turn tool metrics for runtime health (content-free) ──────────
_last_tool_metrics: dict = {}


def publish_tool_metrics(metrics: dict) -> None:
    global _last_tool_metrics
    _last_tool_metrics = dict(metrics or {})


def last_tool_metrics() -> dict:
    return dict(_last_tool_metrics)
