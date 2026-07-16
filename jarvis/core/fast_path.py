"""core/fast_path.py — V69 M55.3: FAST transport selection policy (pure).

Decides, for ONE already-classified turn, whether it should be served by the new
native no-think Ollama transport (:mod:`core.ollama_native`) or stay on the existing
OpenAI-compatible tool-chat path (:meth:`core.llm.LLM.chat_stream`'s loop).

This is a thin, deterministic policy over signals the live path already computed —
the :class:`~core.turn_policy.TurnPolicy` (reason code / verify policy / security),
the routing :class:`~core.model_router.ModelDecision` (role), the cached native
:class:`~core.ollama_native.NativeCapability`, and the operator's FAST config. It
NEVER re-runs classification and never widens authority. It only routes SUITABLE
turns onto the fast path and leaves everything else exactly where it was.

Native no-think serves: ordinary greetings, simple educational questions, simple
math explanations, low-risk conversational turns — i.e. reason_code DIRECT_FAST on
the FAST role, not security-sensitive, needing no LLM verifier and no tools.

Everything else is preserved on the existing path: DEEP reasoning, the coding
specialist, cyber-sensitive analysis, effectful planning, tool-call orchestration,
the verifier, and any turn whose policy demands the OpenAI-compatible interface.

Pure and dependency-light so it is fully unit-testable without a live model.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FastReason(str, Enum):
    """Transparent transport-selection reason (diagnostics only — never the model's
    chain of thought)."""

    NATIVE_FAST_NO_THINK = "NATIVE_FAST_NO_THINK"          # served by native /api/chat, think=false
    OPENAI_TOOL_CHAT = "OPENAI_TOOL_CHAT"                  # tool/effectful/security → keep /v1
    DEEP_REASONING = "DEEP_REASONING"                      # DEEP/CODER role → keep /v1
    NATIVE_UNAVAILABLE_FALLBACK = "NATIVE_UNAVAILABLE_FALLBACK"  # fast-eligible but native unusable
    OPENAI_FORCED = "OPENAI_FORCED"                        # operator pinned fast_transport=openai


# Reason codes the fast path is allowed to serve (deterministic pre-tool policy).
_FAST_REASON_CODES = frozenset({"DIRECT_FAST"})
# Verify policies compatible with a no-verifier fast turn.
_FAST_VERIFY_POLICIES = frozenset({"SKIP_LLM_VERIFIER", "DETERMINISTIC_CHECKS_ONLY"})
# Native-capability states on which "auto" will use the native transport. UNKNOWN /
# PROBING are optimistic: trying native costs at most one fast round-trip before a
# clean fallback, and reasoning is never surfaced (thinking chunks are dropped).
_AUTO_NATIVE_OK = frozenset({"NATIVE_READY", "UNKNOWN", "PROBING"})


@dataclass(frozen=True)
class FastRouteDecision:
    """The transport decision for one turn."""

    use_native: bool
    reason: FastReason
    model: str = ""
    think: bool | None = False
    max_tokens: int = 256
    context: int = 2048
    keep_alive: str = "10m"
    detail: str = ""

    def telemetry(self) -> dict:
        return {
            "fast_transport": "native" if self.use_native else "openai",
            "fast_reason": self.reason.value,
            "fast_model": self.model,
            "fast_think": self.think,
            "fast_max_tokens": self.max_tokens,
        }


def _role_value(model_decision) -> str:
    role = getattr(model_decision, "role", None)
    return getattr(role, "value", str(role)) if role is not None else ""


def decide_fast_route(
    *,
    turn_policy,
    model_decision,
    routed_model: str,
    native_state: str,
    settings,
) -> FastRouteDecision:
    """Decide whether this turn takes the native no-think fast path.

    ``native_state`` is the cached :class:`~core.ollama_native.NativeProbeState`
    value (a string). ``routed_model`` is the model the router already resolved for
    this turn. ``settings`` supplies the operator FAST config (core.config).
    """
    transport = (getattr(settings, "fast_transport", "auto") or "auto").lower()
    think = settings.fast_think_value() if hasattr(settings, "fast_think_value") else False
    model = (getattr(settings, "fast_model", "") or "").strip() or routed_model
    max_tokens = int(getattr(settings, "fast_max_tokens", 256))
    context = int(getattr(settings, "fast_context", 2048))
    keep_alive = getattr(settings, "fast_keep_alive", "10m")

    def _decide(use_native: bool, reason: FastReason, detail: str) -> FastRouteDecision:
        return FastRouteDecision(
            use_native=use_native, reason=reason, model=model, think=think,
            max_tokens=max_tokens, context=context, keep_alive=keep_alive,
            detail=detail,
        )

    # Operator pinned the legacy transport → never use native.
    if transport == "openai":
        return _decide(False, FastReason.OPENAI_FORCED, "fast_transport=openai")

    role = _role_value(model_decision)
    reason_code = getattr(getattr(turn_policy, "reason_code", None), "value", "")
    verify_policy = getattr(getattr(turn_policy, "verify_policy", None), "value", "")
    security = bool(getattr(turn_policy, "security_sensitive", False))

    # DEEP / CODER / CLOUD reasoning must keep the full OpenAI-compatible path.
    if role in ("deep", "coder", "cloud"):
        return _decide(False, FastReason.DEEP_REASONING, f"role={role}")

    # Tool / effectful / cyber-sensitive / verifier turns stay on the tool-chat path.
    fast_eligible = (
        reason_code in _FAST_REASON_CODES
        and verify_policy in _FAST_VERIFY_POLICIES
        and not security
        and role == "fast"
    )
    if not fast_eligible:
        return _decide(False, FastReason.OPENAI_TOOL_CHAT,
                       f"reason={reason_code} verify={verify_policy} sec={security}")

    # Fast-eligible: use native unless capability rules it out.
    state = (native_state or "UNKNOWN").upper()
    if transport == "native":
        # Operator forced native: try it (best effort; the runtime falls back on a
        # transport error). Only a proven-degraded server (think=false ignored) is
        # refused, because using it would silently re-enable reasoning.
        if state == "NATIVE_DEGRADED":
            return _decide(False, FastReason.NATIVE_UNAVAILABLE_FALLBACK,
                           "forced-native but think=false not honored")
        return _decide(True, FastReason.NATIVE_FAST_NO_THINK, f"forced-native state={state}")

    # transport == "auto"
    if state in _AUTO_NATIVE_OK:
        return _decide(True, FastReason.NATIVE_FAST_NO_THINK, f"auto state={state}")
    return _decide(False, FastReason.NATIVE_UNAVAILABLE_FALLBACK, f"auto state={state}")
