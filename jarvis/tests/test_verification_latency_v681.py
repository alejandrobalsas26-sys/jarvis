"""
tests/test_verification_latency_v681.py — V68.1 M49 CPU-aware verification.

Locks: deterministic model-free pre-checks, bounded/cancellable timeouts,
and latency observability — so a verifier can never block a turn for minutes
yet still fails closed for high-risk claims.
"""
from __future__ import annotations

import asyncio

from core.verification import (
    VerificationResult,
    deterministic_precheck,
    resource_aware_timeout,
    verify_answer,
    verifier_latency_stats,
    record_verifier_latency,
    _VERIFY_TIMEOUT_CEILING_S,
    _VERIFY_TIMEOUT_WARM_S,
)


# ── Resource-aware bounded timeout ────────────────────────────────────────────

def test_warm_timeout_shorter_than_cold():
    warm = resource_aware_timeout(warm=True, on_battery=False)
    cold = resource_aware_timeout(warm=False, on_battery=False)
    assert warm < cold
    assert warm == _VERIFY_TIMEOUT_WARM_S


def test_timeout_never_exceeds_ceiling():
    for w in (True, False):
        for b in (True, False):
            assert resource_aware_timeout(warm=w, on_battery=b) <= _VERIFY_TIMEOUT_CEILING_S


def test_battery_reduces_timeout():
    ac = resource_aware_timeout(warm=False, on_battery=False)
    bat = resource_aware_timeout(warm=False, on_battery=True)
    assert bat < ac


# ── Deterministic pre-check ───────────────────────────────────────────────────

def test_precheck_fallback_not_model_verified():
    pre = deterministic_precheck(
        "hack the vending machine",
        "Authorization/scope is not established for this offensive request.",
        tool_failed=True, security_sensitive=True,
    )
    assert isinstance(pre, VerificationResult)
    # A fallback is audited deterministically; it is NOT flagged for human review
    # (that would be noise) and it did not require a model pass.
    assert pre.needs_human_review is False


def test_precheck_security_sensitive_tool_failure_fails_closed():
    pre = deterministic_precheck(
        "assess the exposure of host X",
        "Here is a substantive answer that does not acknowledge any failure.",
        tool_failed=True, security_sensitive=True,
    )
    assert pre is not None
    assert pre.verified is False
    assert pre.needs_human_review is True


def test_precheck_empty_draft_short_circuits():
    pre = deterministic_precheck("q", "   ", tool_failed=False, security_sensitive=False)
    assert pre is not None and pre.verified is True


def test_precheck_normal_answer_proceeds_to_model():
    pre = deterministic_precheck(
        "explain quicksort", "Quicksort is a divide-and-conquer sort...",
        tool_failed=False, security_sensitive=False,
    )
    assert pre is None  # -> caller runs the bounded model verification


# ── Bounded / cancellable verify_answer with a fake client ────────────────────

class _SlowClient:
    """AsyncOpenAI-shaped stub whose completion never resolves in time."""
    def __init__(self, delay: float):
        self._delay = delay
        self.chat = self
        self.completions = self

    async def create(self, **_kwargs):
        await asyncio.sleep(self._delay)
        class _R:
            class _C:
                class _M:
                    content = '{"verified": true, "confidence": 1.0, "issues": [], ' \
                              '"needs_human_review": false, "reasoning": "ok"}'
                message = _M()
            choices = [_C()]
        return _R()


def test_verify_times_out_and_fails_closed_bounded():
    async def _run():
        client = _SlowClient(delay=5.0)
        start = asyncio.get_event_loop().time()
        result = await verify_answer(client, "q", "draft", timeout=0.3)
        elapsed = asyncio.get_event_loop().time() - start
        return result, elapsed

    result, elapsed = asyncio.run(_run())
    assert result.verified is False
    assert result.needs_human_review is True
    assert elapsed < 2.0  # bounded — nowhere near minutes
    stats = verifier_latency_stats()
    assert stats["count"] >= 1


def test_verify_cancellation_via_event():
    async def _run():
        client = _SlowClient(delay=5.0)
        cancel = asyncio.Event()

        async def _trip():
            await asyncio.sleep(0.2)
            cancel.set()

        asyncio.ensure_future(_trip())
        return await verify_answer(client, "q", "draft", timeout=10.0, cancel_event=cancel)

    result = asyncio.run(_run())
    assert result.verified is False  # fail-closed on cancel


def test_verify_success_records_latency():
    class _FastClient(_SlowClient):
        def __init__(self):
            super().__init__(delay=0.0)

    result = asyncio.run(verify_answer(_FastClient(), "q", "draft", timeout=5.0))
    assert result.verified is True
    stats = verifier_latency_stats()
    assert stats["count"] >= 1


# ── Latency stats shape ───────────────────────────────────────────────────────

def test_latency_stats_shape():
    record_verifier_latency(1.5, outcome="verified", timed_out=False)
    stats = verifier_latency_stats()
    for key in ("count", "avg_s", "max_s", "timeouts"):
        assert key in stats
