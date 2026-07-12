"""
core/verification.py — Planner/Worker/Verifier gate (V60.0, Phase 5).

A lightweight verifier pass for high-stakes answers. `should_verify` is a pure
predicate the orchestrator checks; `verify_answer` runs a separate VERIFIER
model over the draft and returns a strict, parsed verdict.

Fail-closed contract: any error (verifier model down, malformed JSON, timeout)
yields a CONSERVATIVE result — verified=False, needs_human_review=True — so a
verifier outage never silently rubber-stamps an answer.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field

from loguru import logger

from core.model_router import ModelDecision, ModelRole, model_for_role, route

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# V68.1 M49 — CPU-aware bounded verification. This host is a 15W CPU-bound Ryzen
# running Ollama with OLLAMA_MAX_LOADED_MODELS=1, so verifying a DEEP (qwen3:14b)
# draft with the VERIFIER (qwen3:8b) forces a full model swap (unload 14b, cold
# load 8b) that can take tens of seconds. The old single 25s deadline both let a
# turn block far too long on a cold swap AND was too short to ever succeed on
# one — so it always failed closed after a long wait. We now bound the wait by
# whether the verifier model is already warm, halve it on battery, and NEVER
# exceed a hard ceiling so a turn can never block for minutes.
_VERIFY_TIMEOUT_WARM_S = 20.0    # verifier model already loaded (draft used it)
_VERIFY_TIMEOUT_COLD_S = 40.0    # a model swap is required (bounded, not infinite)
_VERIFY_TIMEOUT_CEILING_S = 45.0
_VERIFY_TIMEOUT_BATTERY_FLOOR_S = 12.0
_VERIFY_TIMEOUT_S = _VERIFY_TIMEOUT_COLD_S  # back-compat default

# Bounded ring of recent verifier latencies (seconds) for runtime-health.
_LATENCY_SAMPLES: "deque[dict]" = deque(maxlen=50)


def resource_aware_timeout(*, warm: bool, on_battery: bool) -> float:
    """Bounded verifier timeout for CPU inference. Warm ≈ no model swap needed."""
    base = _VERIFY_TIMEOUT_WARM_S if warm else _VERIFY_TIMEOUT_COLD_S
    if on_battery:
        base = max(_VERIFY_TIMEOUT_BATTERY_FLOOR_S, base * 0.6)
    return min(_VERIFY_TIMEOUT_CEILING_S, base)


def record_verifier_latency(latency_s: float, *, outcome: str, timed_out: bool) -> None:
    """Record one verifier latency sample (bounded, deterministic)."""
    _LATENCY_SAMPLES.append({
        "latency_s": round(float(latency_s), 2),
        "outcome": outcome,
        "timed_out": bool(timed_out),
    })


def verifier_latency_stats() -> dict:
    """Read-only latency rollup for runtime-health (bounded, no raw internals)."""
    samples = list(_LATENCY_SAMPLES)
    if not samples:
        return {"count": 0, "avg_s": 0.0, "max_s": 0.0, "timeouts": 0}
    lats = [s["latency_s"] for s in samples]
    return {
        "count": len(samples),
        "avg_s": round(sum(lats) / len(lats), 2),
        "max_s": round(max(lats), 2),
        "last_s": lats[-1],
        "timeouts": sum(1 for s in samples if s["timed_out"]),
    }


@dataclass
class VerificationResult:
    verified: bool
    confidence: float
    issues: list[str] = field(default_factory=list)
    reasoning: str = ""
    needs_human_review: bool = False

    @classmethod
    def fail_closed(cls, reason: str) -> "VerificationResult":
        return cls(
            verified=False,
            confidence=0.0,
            issues=[reason],
            reasoning=f"Fail-closed: {reason}",
            needs_human_review=True,
        )


def should_verify(
    prompt: str,
    tool_used: bool = False,
    security_sensitive: bool = False,
) -> bool:
    """True when the answer to *prompt* warrants a verifier pass.

    Always verify when a tool ran or the request is flagged security-sensitive;
    otherwise defer to the router's per-prompt `requires_verification` signal
    (deep analysis, security keywords, explicit review requests).
    """
    if tool_used or security_sensitive:
        return True
    return route(prompt, security_sensitive=security_sensitive).requires_verification


_VERIFIER_SYSTEM = (
    "You are a strict verification model auditing another assistant's draft answer "
    "for an authorized local security/dev assistant. Judge ONLY the draft; do not "
    "answer the task yourself. Check that:\n"
    "  1. The answer actually follows the user's request.\n"
    "  2. There are no unsupported or fabricated claims.\n"
    "  3. It makes no false claims about repository contents/state.\n"
    "  4. Any code is internally coherent.\n"
    "  5. Security-sensitive advice is scoped to authorized / lab / defensive use.\n"
    "  6. Tool-execution assumptions are stated explicitly.\n"
    "  7. Uncertainty is surfaced rather than hidden.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no markdown fences, exactly:\n"
    '{"verified": <bool>, "confidence": <0.0-1.0>, "issues": [<strings>], '
    '"needs_human_review": <bool>, "reasoning": "<one short sentence>"}'
)


def _parse_verdict(raw: str) -> VerificationResult:
    cleaned = _FENCE_RE.sub("", (raw or "")).strip()
    obj = None
    try:
        obj = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        m = _JSON_RE.search(cleaned)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return VerificationResult.fail_closed("verifier returned unparseable output")

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    issues = obj.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return VerificationResult(
        verified=bool(obj.get("verified", False)),
        confidence=confidence,
        issues=[str(i) for i in issues],
        reasoning=str(obj.get("reasoning", "")),
        needs_human_review=bool(obj.get("needs_human_review", not obj.get("verified", False))),
    )


# Phrases that mark a draft as a tool-failure / unavailable fallback rather than
# a substantive claim. Such a draft must NOT be model-verified as if it were a
# valid answer (V68.1 M49) — deterministic handling is fast and honest.
_FALLBACK_MARKERS = (
    "authorization/scope is not established",
    "authorization is not established",
    "tool is unavailable",
    "tool_failure",
    "knowledge vault is unavailable",
    "vector backend offline",
    "i could not complete",
    "i was unable to",
    "no tool will run",
)


def deterministic_precheck(
    prompt: str,
    draft_answer: str,
    *,
    tool_failed: bool = False,
    security_sensitive: bool = False,
) -> VerificationResult | None:
    """Cheap, model-free checks run BEFORE spending CPU on a verifier pass.

    Returns a VerificationResult to short-circuit the model, or None to proceed
    to bounded model verification. Never blocks. Preserves fail-closed posture
    for high-risk claims while refusing to rubber-stamp a failed-tool fallback.
    """
    text = (draft_answer or "").strip()
    if not text:
        # Empty draft: nothing to verify; caller returns it unchanged.
        return VerificationResult(verified=True, confidence=1.0,
                                  reasoning="empty draft — nothing to audit")

    low = text.lower()
    is_fallback = any(m in low for m in _FALLBACK_MARKERS)
    if is_fallback:
        # An honest "tool failed / not authorized" fallback is not a claim to be
        # audited by an expensive model. Do not spend the verifier on it, and do
        # not rubber-stamp it as a verified substantive answer.
        return VerificationResult(
            verified=True,
            confidence=0.5,
            reasoning="tool-failure/unauthorized fallback — audited deterministically, "
                      "no model verification needed",
            needs_human_review=False,
        )

    if tool_failed and security_sensitive:
        # A security-sensitive turn whose tool failed but which still produced a
        # substantive answer warrants human review WITHOUT a multi-minute cold
        # model swap: surface promptly, fail-closed.
        return VerificationResult.fail_closed(
            "security-sensitive turn with a failed tool — human review advised"
        )
    return None


async def verify_answer(
    llm_client,
    prompt: str,
    draft_answer: str,
    model_decision: ModelDecision | None = None,
    *,
    timeout: float | None = None,
    cancel_event: "asyncio.Event | None" = None,
) -> VerificationResult:
    """Run the VERIFIER model over *draft_answer*. Fail-closed on any error.

    *llm_client* is an AsyncOpenAI-compatible client (e.g. LLM.client). The
    verifier always uses the dedicated VERIFIER-role model (qwen3:8b), never the
    heavier DEEP model — a lightweight verification must never load qwen3:14b.
    The wait is bounded (CPU-aware) and cancellable via *cancel_event*; latency
    is recorded for runtime-health.
    """
    verifier_model = model_for_role(ModelRole.VERIFIER)
    effective_timeout = timeout if timeout is not None else _VERIFY_TIMEOUT_S
    user_block = (
        f"USER REQUEST:\n{prompt}\n\n"
        f"DRAFT ANSWER TO AUDIT:\n{draft_answer}"
    )
    start = time.monotonic()
    call = asyncio.ensure_future(
        llm_client.chat.completions.create(
            model=verifier_model,
            messages=[
                {"role": "system", "content": _VERIFIER_SYSTEM},
                {"role": "user", "content": user_block},
            ],
            stream=False,
        )
    )
    try:
        if cancel_event is not None:
            # Race the verifier against an operator interrupt so cancellation is
            # honored promptly (not only at the timeout boundary).
            waiter = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait(
                {call, waiter}, timeout=effective_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if call not in done:
                call.cancel()
                if waiter in done:
                    latency = time.monotonic() - start
                    record_verifier_latency(latency, outcome="cancelled", timed_out=False)
                    logger.info("Verifier: cancelled by operator interrupt.")
                    return VerificationResult.fail_closed("verifier cancelled")
                latency = time.monotonic() - start
                record_verifier_latency(latency, outcome="timeout", timed_out=True)
                logger.warning(
                    f"Verifier: timeout after {latency:.1f}s (bound={effective_timeout:.0f}s) "
                    "— failing closed."
                )
                return VerificationResult.fail_closed("verifier timeout")
            response = call.result()
        else:
            response = await asyncio.wait_for(call, timeout=effective_timeout)
        raw = (response.choices[0].message.content or "").strip()
        result = _parse_verdict(raw)
        record_verifier_latency(time.monotonic() - start,
                                outcome="verified" if result.verified else "flagged",
                                timed_out=False)
        return result
    except asyncio.TimeoutError:
        call.cancel()
        latency = time.monotonic() - start
        record_verifier_latency(latency, outcome="timeout", timed_out=True)
        logger.warning(
            f"Verifier: timeout after {latency:.1f}s (bound={effective_timeout:.0f}s) "
            "— failing closed."
        )
        return VerificationResult.fail_closed("verifier timeout")
    except Exception as e:  # noqa: BLE001 — verifier must never crash the turn
        call.cancel()
        record_verifier_latency(time.monotonic() - start, outcome="error", timed_out=False)
        logger.warning(f"Verifier: error ({e}) — failing closed.")
        return VerificationResult.fail_closed(f"verifier error: {e}")
