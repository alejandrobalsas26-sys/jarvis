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

#: V69 M68A §B — how many tool calls ONE model response may request.
#:
#: The round budget bounded how many times the model could be asked; nothing
#: bounded how much it could ask for each time. `for tc in tool_calls_list:`
#: iterated whatever arrived, so a response carrying fifty calls executed fifty
#: tools inside one "round", and the documented bound of four rounds described a
#: quantity that was not what it sounded like. Generous enough for genuine
#: parallel tool use, finite so that "bounded" is true of calls and not only of
#: rounds.
_MAX_CALLS_PER_RESPONSE = 8


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


def eligible_tool_names(turn_tools) -> frozenset[str]:
    """The names a turn's tool schemas make eligible — TOTAL, and never ``None``.

    V69 M68A §B. This function exists because the expression it replaces ended in
    ``or None``, and ``None`` was the one value :func:`validate_tool_call` read as
    *unrestricted*. So the moment the round budget dropped the tools — the moment
    the loop had decided NOTHING more may run — every name the model could emit,
    ``run_shell_command`` included, validated. Exhaustion was normalised into
    omnipotence.

    An empty input yields an empty frozenset, which is a real answer meaning
    "nothing is eligible", and it is not falsy-collapsible into a wildcard by any
    caller. A name that is absent, blank or not a string is not a tool.
    """
    names: set[str] = set()
    for t in turn_tools or ():
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        nm = fn.get("name")
        if isinstance(nm, str) and nm.strip():
            names.add(nm.strip())
    return frozenset(names)


def validate_tool_call(name, arguments_json, eligible_names) -> tuple[bool, dict, str]:
    """Deterministically validate ONE tool call BEFORE it can execute.

    Returns ``(ok, parsed_args, reason)``. A call is rejected (``ok=False``) when:
      * the name is empty or NOT in the eligible set (a hallucinated/withheld tool
        never executes — the M58.7 "malformed tool calls never execute" guarantee);
      * the eligible set is EMPTY or absent — see below;
      * the arguments are not valid JSON, or are not a JSON object.
    A rejected call yields an empty ``{}`` args and a reason; the caller must NOT
    execute it and must not guess/repair effectful arguments freely.

    V69 M68A §B — ``eligible_names`` is a REQUIREMENT, not a hint. An empty set
    and ``None`` both mean **no tool may execute**, and both are refused with
    ``no_eligible_tools``. Before M68A, ``None`` skipped the eligibility check
    altogether, so "I have no eligible tools" and "every tool is eligible" were
    the same argument. This is the fail-closed half of the fix; the other half is
    :func:`eligible_tool_names`, which no longer produces ``None`` at the call
    site. Either alone would close the audited path — both are present so that
    neither layer masks a regression in the other.
    """
    import json
    nm = str(name or "").strip()
    if not nm:
        return False, {}, "empty_name"
    if not eligible_names:
        return False, {}, "no_eligible_tools"
    if nm not in set(eligible_names):
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
    max_calls_per_response: int = _MAX_CALLS_PER_RESPONSE

    rounds: int = 0
    retries: int = 0
    malformed_calls: int = 0
    denied_calls: int = 0
    repairs: int = 0
    tools_used: int = 0
    #: Calls refused because ONE response asked for more than the per-response
    #: ceiling. Content-free, and it is a refusal count, not a warning count:
    #: every one of them was prevented from executing.
    dropped_calls: int = 0
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

    def admit_response_calls(self, requested: int) -> int:
        """How many of *requested* calls this response may execute.

        V69 M68A §B. Returns the admitted count and records the remainder as
        refused. The excess is dropped BEFORE the assistant turn is written to
        history, so the tool_call/tool pairing stays coherent: the model is never
        shown a call it will not get a result for.
        """
        requested = max(0, int(requested))
        admitted = min(requested, max(0, self.max_calls_per_response))
        self.dropped_calls += requested - admitted
        return admitted

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
            "dropped_calls": self.dropped_calls,
            "max_calls_per_response": self.max_calls_per_response,
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
