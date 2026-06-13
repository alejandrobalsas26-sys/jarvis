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
from dataclasses import dataclass, field

from loguru import logger

from core.model_router import ModelDecision, ModelRole, model_for_role, route

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VERIFY_TIMEOUT_S = 25.0


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


async def verify_answer(
    llm_client,
    prompt: str,
    draft_answer: str,
    model_decision: ModelDecision | None = None,
) -> VerificationResult:
    """Run the VERIFIER model over *draft_answer*. Fail-closed on any error.

    *llm_client* is an AsyncOpenAI-compatible client (e.g. LLM.client). The
    verifier always uses the dedicated VERIFIER-role model, independent of the
    model that produced the draft.
    """
    verifier_model = model_for_role(ModelRole.VERIFIER)
    user_block = (
        f"USER REQUEST:\n{prompt}\n\n"
        f"DRAFT ANSWER TO AUDIT:\n{draft_answer}"
    )
    try:
        response = await asyncio.wait_for(
            llm_client.chat.completions.create(
                model=verifier_model,
                messages=[
                    {"role": "system", "content": _VERIFIER_SYSTEM},
                    {"role": "user", "content": user_block},
                ],
                stream=False,
            ),
            timeout=_VERIFY_TIMEOUT_S,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _parse_verdict(raw)
    except asyncio.TimeoutError:
        logger.warning("Verifier: timeout — failing closed.")
        return VerificationResult.fail_closed("verifier timeout")
    except Exception as e:  # noqa: BLE001 — verifier must never crash the turn
        logger.warning(f"Verifier: error ({e}) — failing closed.")
        return VerificationResult.fail_closed(f"verifier error: {e}")
