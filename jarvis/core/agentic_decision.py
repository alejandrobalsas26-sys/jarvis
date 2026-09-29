"""core/agentic_decision.py — V69 M68A §C. The validated agentic-decision contract.

WHAT THIS MODULE EXISTS TO PREVENT
----------------------------------
:meth:`core.llm.JarvisLLM.decide_next_action` ended with this, and an external
static audit was right to name it::

    return {"tool": "RESOLVED",
            "input": {},
            "reasoning": f"LLM response not parseable: {raw[:100]}"}

A model whose reply could not be parsed produced the *same* value as a model that
had assessed and contained an incident. ``run_agentic_incident`` read ``tool ==
"RESOLVED"``, broadcast ``agentic_resolved``, and stopped. A truncated stream, an
Ollama 500 rendered as prose, a model that emitted a bare apology — each one
silently closed a live security incident, and the event log said the incident was
*resolved*, not that reasoning had failed.

The required separation, and the invariant this module enforces::

    RESOLVED  !=  INVALID_DECISION  !=  MODEL_ERROR  !=  UNKNOWN

``RESOLVED`` is a CLAIM about the world. It may be reached only from a
well-formed model decision that explicitly says so. No parse failure, no shape
failure, no transport failure and no absent value may ever produce it — that is
:func:`validate_decision`'s whole job, and
``test_no_malformed_input_can_ever_yield_resolved`` proves it over a corpus of
malformed inputs rather than over the branches that happen to exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

#: The sentinel tool name that means "the incident is assessed or contained".
#: It is the ONLY route to :attr:`DecisionStatus.RESOLVED`.
RESOLVED_SENTINEL = "RESOLVED"

#: The tools the SOC ReAct prompt advertises, in prompt order.
#:
#: ONE definition, because two would drift: ``decide_next_action`` renders its
#: "Available tools:" line from this tuple and ``run_agentic_incident`` validates
#: against it, so a tool cannot be offered to the model without becoming
#: dispatchable, or validated without being offered.
#: ``test_the_advertised_set_and_the_validated_set_are_the_same_object`` pins it.
SOC_ADVERTISED_TOOLS: tuple[str, ...] = (
    "network_scan", "whois_lookup", "check_connectivity",
    "forensic_capture", "run_shell_command",
)

#: Reasoning is model-authored text that reaches logs and the HUD. Bound it.
MAX_REASONING_CHARS = 2000

#: Detail is OUR text (a parse error, an exception class). Bound it too.
MAX_DETAIL_CHARS = 320


class DecisionStatus(Enum):
    """The closed set of outcomes of asking a model what to do next.

    Every member is a different statement about a different thing, which is why
    collapsing any two of them is a defect rather than a simplification:

    * :attr:`ACT` — a well-formed decision naming a tool. Dispatchable.
    * :attr:`RESOLVED` — the model asserts the incident needs nothing more.
    * :attr:`INVALID_DECISION` — a reply arrived and is not a decision (it did
      not parse, or it parsed into the wrong shape). The model is reachable; its
      answer is unusable.
    * :attr:`MODEL_ERROR` — no usable reply arrived: transport failed, the call
      raised, or it timed out. Says nothing about the incident.
    * :attr:`UNKNOWN` — no decision was produced at all and we cannot say which
      of the above it was. The honest bottom of the lattice, never a default for
      convenience.
    """

    ACT = "ACT"
    RESOLVED = "RESOLVED"
    INVALID_DECISION = "INVALID_DECISION"
    MODEL_ERROR = "MODEL_ERROR"
    UNKNOWN = "UNKNOWN"


#: The statuses that end a run. ``ACT`` continues it; the three failure statuses
#: are handled under a bounded budget by the caller and are NOT terminal on their
#: own — a single unparseable reply must not end an incident either way.
TERMINAL_STATUSES = (DecisionStatus.RESOLVED,)

#: Statuses that mean "no usable decision this cycle".
NON_ACTIONABLE_STATUSES = (
    DecisionStatus.INVALID_DECISION,
    DecisionStatus.MODEL_ERROR,
    DecisionStatus.UNKNOWN,
)


@dataclass(frozen=True)
class Decision:
    """One validated decision. Immutable, and shaped before anything dispatches."""

    status: DecisionStatus
    tool: str = ""
    tool_input: dict = field(default_factory=dict)
    reasoning: str = ""
    #: Why this is not an ACT, when it is not. Ours, never the model's.
    detail: str = ""

    @property
    def may_dispatch(self) -> bool:
        """The ONE predicate a dispatcher may consult.

        Deliberately not ``status != RESOLVED``: that formulation is what let a
        failure state reach a tool call. A decision is dispatchable only when it
        positively IS an action and positively names a tool.
        """
        return self.status is DecisionStatus.ACT and bool(self.tool)

    @property
    def is_resolved(self) -> bool:
        return self.status is DecisionStatus.RESOLVED

    def as_dict(self) -> dict:
        """The wire form. ``status`` is always present — there is no shape of this
        envelope in which the status has to be guessed from the tool name."""
        return {
            "status": self.status.value,
            "tool": self.tool,
            "input": dict(self.tool_input),
            "reasoning": self.reasoning,
            "detail": self.detail,
        }


def _clean(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def invalid(detail: str, *, reasoning: str = "") -> Decision:
    """An INVALID_DECISION. A constructor, so no call site has to hand-roll one
    and get the status wrong."""
    return Decision(status=DecisionStatus.INVALID_DECISION,
                    reasoning=_clean(reasoning, MAX_REASONING_CHARS),
                    detail=_clean(detail, MAX_DETAIL_CHARS))


def model_error(detail: str) -> Decision:
    """A MODEL_ERROR: the model could not be reached or did not answer."""
    return Decision(status=DecisionStatus.MODEL_ERROR,
                    detail=_clean(detail, MAX_DETAIL_CHARS))


def unknown(detail: str = "") -> Decision:
    """An UNKNOWN: nothing usable, and we will not pretend to know which."""
    return Decision(status=DecisionStatus.UNKNOWN,
                    detail=_clean(detail, MAX_DETAIL_CHARS))


def validate_decision(raw, *, allowed_tools=None,
                      honour_status: bool = False) -> Decision:
    """Validate an arbitrary object into a :class:`Decision`. Total, fail-closed.

    ``raw`` is untrusted: it is either JSON a model produced or an envelope a
    client built. Every rejection lands in :attr:`DecisionStatus.INVALID_DECISION`
    (a reply that is not a decision) or :attr:`DecisionStatus.UNKNOWN` (no reply
    at all). **No input whatsoever yields RESOLVED except one that explicitly
    names the** ``RESOLVED`` **sentinel in a well-formed decision.**

    ``allowed_tools`` — when given, the closed set of tool names this decision may
    name. A name outside it is INVALID_DECISION, not a dispatch: a hallucinated
    tool is a malformed decision, and the place to say so is before the executor,
    not inside it.

    ``honour_status`` — whether ``raw["status"]`` is authoritative. **False for
    anything a model produced**: the envelope status is ours to assign, and a
    model that could set it could assert ``RESOLVED`` without the sentinel, or
    assert ``ACT`` over a shape we rejected. True only for an envelope built by a
    trusted in-process caller (an LLM client returning :meth:`Decision.as_dict`).
    """
    if raw is None:
        return unknown("no decision was produced")
    if isinstance(raw, Decision):
        return raw
    if not isinstance(raw, dict):
        return invalid(f"decision is {type(raw).__name__}, not an object")

    reasoning = _clean(raw.get("reasoning"), MAX_REASONING_CHARS)

    if honour_status:
        declared = raw.get("status")
        if isinstance(declared, DecisionStatus):
            declared = declared.value
        if isinstance(declared, str) and declared.strip():
            try:
                status = DecisionStatus(declared.strip())
            except ValueError:
                return invalid(f"unknown status {declared.strip()[:64]!r}",
                               reasoning=reasoning)
            if status in NON_ACTIONABLE_STATUSES:
                return Decision(status=status, reasoning=reasoning,
                                detail=_clean(raw.get("detail"), MAX_DETAIL_CHARS))
            if status is DecisionStatus.RESOLVED:
                return Decision(status=DecisionStatus.RESOLVED, reasoning=reasoning)
            # ACT still has to satisfy the shape rules below; a declared ACT is
            # not a licence to skip them.

    tool = raw.get("tool")
    if tool is None:
        return invalid("decision names no tool", reasoning=reasoning)
    if not isinstance(tool, str):
        return invalid(f"tool is {type(tool).__name__}, not a string",
                       reasoning=reasoning)
    tool = tool.strip()
    if not tool:
        return invalid("tool name is blank", reasoning=reasoning)

    if tool == RESOLVED_SENTINEL:
        return Decision(status=DecisionStatus.RESOLVED, reasoning=reasoning)

    tool_input = raw.get("input", {})
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return invalid(f"input is {type(tool_input).__name__}, not an object",
                       reasoning=reasoning)
    if not all(isinstance(k, str) for k in tool_input):
        return invalid("input has a non-string key", reasoning=reasoning)

    if allowed_tools is not None and tool not in set(allowed_tools):
        return invalid(f"tool {tool[:64]!r} is not an advertised tool",
                       reasoning=reasoning)

    return Decision(status=DecisionStatus.ACT, tool=tool,
                    tool_input=dict(tool_input), reasoning=reasoning)
