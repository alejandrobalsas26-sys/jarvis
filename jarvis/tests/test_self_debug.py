"""
tests/test_self_debug.py — V65 bounded runtime failure diagnosis + repair.

Proves the hard safety rules: timeouts/invalid-args/unavailable-capability are
correctly diagnosed, the retry cap is enforced (no infinite retry), destructive
actions are never auto-retried, scope-denied failures escalate to HITL and never
expand scope, and a repair that tries to touch an authority/scope/consent/force
argument is rejected fail-closed. Synchronous tests drive coroutines via
asyncio.run.
"""
from __future__ import annotations

import asyncio

from core.self_debug import (
    RepairAction,
    RuntimeFailure,
    RuntimeFailureType,
    SelfDebugRuntime,
    diagnose,
)


def _run(coro):
    return asyncio.run(coro)


def _fail(error, *, destructive=False, args=None):
    return RuntimeFailure(operation="op", error=error, args=args or {}, destructive=destructive)


# ── diagnosis ─────────────────────────────────────────────────────────────────
def test_timeout_classified_retryable():
    d = diagnose(_fail("operation timed out after 30s"))
    assert d.failure_type is RuntimeFailureType.TIMEOUT and d.retryable


def test_invalid_args_diagnosed():
    d = diagnose(_fail("TypeError: missing required argument 'target'"))
    assert d.failure_type is RuntimeFailureType.BAD_ARGUMENTS and d.retryable


def test_unavailable_capability_diagnosed_not_retryable():
    d = diagnose(_fail("no such tool: quantum_teleport"))
    assert d.failure_type is RuntimeFailureType.UNAVAILABLE_CAPABILITY and not d.retryable


def test_auth_failure_requires_human():
    d = diagnose(_fail("401 unauthorized: token expired"))
    assert d.failure_type is RuntimeFailureType.AUTH_FAILURE
    assert not d.retryable and d.requires_human


def test_scope_denied_requires_human():
    d = diagnose(_fail("authorization required: target outside authorized scope"))
    assert d.failure_type is RuntimeFailureType.SCOPE_DENIED
    assert not d.retryable and d.requires_human


def test_destructive_is_blocked_regardless_of_error():
    d = diagnose(_fail("timed out", destructive=True))  # would otherwise be a retryable timeout
    assert d.failure_type is RuntimeFailureType.DESTRUCTIVE_BLOCKED and not d.retryable


# ── retry cap ─────────────────────────────────────────────────────────────────
def test_retry_cap_enforced():
    rt = SelfDebugRuntime(max_retries=2)
    calls = {"n": 0}

    async def always_timeout(args):
        calls["n"] += 1
        return False, "operation timed out"

    outcome = _run(rt.run_with_repair("net_scan", always_timeout, {"t": "x"}))
    assert not outcome.success
    # 1 initial attempt + at most 2 retries = 3 total; never more.
    assert calls["n"] == 3 and outcome.attempts == 3


def test_max_retries_hard_ceiling():
    rt = SelfDebugRuntime(max_retries=99)  # clamped to 3
    assert rt.max_retries == 3


# ── destructive never auto-retried ────────────────────────────────────────────
def test_destructive_action_never_auto_retried():
    rt = SelfDebugRuntime(max_retries=2)
    calls = {"n": 0}

    async def destructive_fail(args):
        calls["n"] += 1
        return False, "timed out"

    outcome = _run(rt.run_with_repair("delete_all", destructive_fail, {}, destructive=True))
    assert not outcome.success
    assert calls["n"] == 1  # exactly one attempt — no retry
    assert outcome.resolved_by == RepairAction.ESCALATE_HITL.value


# ── scope failure never expands scope / HITL enforced ─────────────────────────
def test_scope_denied_escalates_and_never_retries():
    rt = SelfDebugRuntime(max_retries=2)
    calls = {"n": 0}

    async def scope_denied(args):
        calls["n"] += 1
        return False, "target outside authorized scope"

    outcome = _run(rt.run_with_repair("recon", scope_denied, {"target": "10.0.0.1"}))
    assert not outcome.success and calls["n"] == 1
    assert outcome.resolved_by == RepairAction.ESCALATE_HITL.value


def test_repair_touching_privileged_key_is_rejected():
    rt = SelfDebugRuntime(max_retries=2)

    def malicious_repair(failure):
        # Try to make the call succeed by escalating scope — must be refused.
        return {**failure.args, "scope": "all", "force": True}

    decision = rt.decide_retry(_fail("invalid argument: bad target", args={"target": "x"}),
                               retries_done=0, arg_repair_fn=malicious_repair)
    assert not decision.should_retry
    assert decision.proposal.action is RepairAction.ABORT
    assert "privileged" in decision.reason or "privilege" in decision.proposal.rationale


def test_bad_args_retries_only_with_safe_corrected_args():
    rt = SelfDebugRuntime(max_retries=2)
    seen_args = []

    async def attempt(args):
        seen_args.append(dict(args))
        # Succeeds once the 'target' argument is corrected.
        return (args.get("target") == "valid.example", "ok" if args.get("target") == "valid.example"
                else "invalid argument: target")

    def repair(failure):
        return {**failure.args, "target": "valid.example"}

    outcome = _run(rt.run_with_repair("scan", attempt, {"target": "bad"}, arg_repair_fn=repair))
    assert outcome.success and outcome.attempts == 2
    assert seen_args[0]["target"] == "bad" and seen_args[1]["target"] == "valid.example"


def test_bad_args_without_repair_aborts():
    rt = SelfDebugRuntime(max_retries=2)

    async def attempt(args):
        return False, "missing required argument 'x'"

    outcome = _run(rt.run_with_repair("op", attempt, {}))
    assert not outcome.success and outcome.resolved_by == RepairAction.ABORT.value


# ── success paths / verification / no silent hiding ───────────────────────────
def test_success_on_first_try():
    rt = SelfDebugRuntime()

    async def ok(args):
        return True, "done"

    outcome = _run(rt.run_with_repair("op", ok, {}))
    assert outcome.success and outcome.attempts == 1 and outcome.final_error is None


def test_verification_failure_treated_as_retryable_then_reported():
    rt = SelfDebugRuntime(max_retries=1)

    async def ok_but_bad(args):
        return True, "garbage"  # 'succeeds' but fails verification

    outcome = _run(rt.run_with_repair("op", ok_but_bad, {}, verify_fn=lambda r: r == "good"))
    assert not outcome.success  # never silently accepted
    assert outcome.final_error is not None


def test_unknown_failure_is_not_blindly_retried():
    rt = SelfDebugRuntime(max_retries=2)
    calls = {"n": 0}

    async def weird(args):
        calls["n"] += 1
        return False, "some entirely novel failure mode"

    outcome = _run(rt.run_with_repair("op", weird, {}))
    assert not outcome.success and calls["n"] == 1  # no blind retry on unknown
    assert outcome.diagnoses[0].failure_type is RuntimeFailureType.UNKNOWN


def test_raising_attempt_is_diagnosed_not_crashing():
    rt = SelfDebugRuntime(max_retries=1)

    async def boom(args):
        raise RuntimeError("kaboom timed out")

    outcome = _run(rt.run_with_repair("op", boom, {}))
    # The raise is captured as an error string and diagnosed (timeout → retried once).
    assert not outcome.success and outcome.attempts == 2 and "kaboom" in outcome.final_error
