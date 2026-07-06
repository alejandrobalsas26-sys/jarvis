"""
core/self_debug.py — V65: bounded runtime failure diagnosis and repair loop.

When a tool/operation fails, this runtime diagnoses *why* and proposes a
**bounded, safety-constrained** repair — a couple of retries at most, and only
for failures that a retry can legitimately fix. It is deliberately conservative:

Hard rules (V65 non-negotiables), enforced structurally:
  * **No infinite retry** — a hard retry cap (default 2).
  * **No destructive auto-retry** — a destructive operation is never retried
    automatically; it stops and reports.
  * **No authority/scope expansion, no HITL bypass, no security-policy change** —
    a repair may only adjust *ordinary tool arguments*; any proposed argument that
    touches an authority/scope/consent/force key is rejected fail-closed, and
    scope-denied / auth failures escalate to a human instead of being retried.
  * **No silent error hiding** — every attempt and the final error are recorded in
    the `RepairOutcome`.

This module diagnoses and *decides*; it never reaches a tool directly. Execution
stays with the caller's injected `attempt_fn`, which goes through the normal
`ToolExecutor` gate — self-debug adds no new execution path.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

_DEFAULT_MAX_RETRIES = 2

# Argument keys a repair may NEVER introduce or change — these would expand
# authority/scope, bypass consent/HITL, or force a destructive action. Fail-closed.
_FORBIDDEN_ARG_KEYS: frozenset[str] = frozenset({
    "authority", "authority_mode", "scope", "scopes", "authorized_scope",
    "mode", "otp", "nato_otp", "consent", "hitl", "approve", "approved",
    "force", "force_dangerous", "allow_destructive", "sudo", "elevate",
    "bypass", "override", "security_policy", "trusted_lab",
})


# ── failure / diagnosis / proposal / decision / outcome ───────────────────────
class RuntimeFailureType(str, Enum):
    BAD_ARGUMENTS = "bad_arguments"
    WRONG_TOOL = "wrong_tool"
    TIMEOUT = "timeout"
    AUTH_FAILURE = "auth_failure"
    UNAVAILABLE_CAPABILITY = "unavailable_capability"
    SCOPE_DENIED = "scope_denied"
    MALFORMED_OUTPUT = "malformed_output"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    DESTRUCTIVE_BLOCKED = "destructive_blocked"
    UNKNOWN = "unknown"


class RepairAction(str, Enum):
    RETRY_SAME = "retry_same"
    RETRY_WITH_ARGS = "retry_with_args"
    SWITCH_TOOL = "switch_tool"
    ESCALATE_HITL = "escalate_hitl"
    ABORT = "abort"


@dataclass(frozen=True)
class RuntimeFailure:
    operation: str
    error: str = ""
    args: dict = field(default_factory=dict)
    destructive: bool = False
    context: dict = field(default_factory=dict)
    created_ts: float = 0.0

    def to_dict(self) -> dict:
        return {"operation": self.operation, "error": self.error, "args": self.args,
                "destructive": self.destructive, "context": self.context,
                "created_ts": self.created_ts}


@dataclass(frozen=True)
class FailureDiagnosis:
    failure_type: RuntimeFailureType
    retryable: bool
    requires_human: bool
    cause: str
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {"failure_type": self.failure_type.value, "retryable": self.retryable,
                "requires_human": self.requires_human, "cause": self.cause,
                "confidence": self.confidence}


@dataclass(frozen=True)
class RepairProposal:
    action: RepairAction
    new_args: dict | None = None
    rationale: str = ""
    requires_human: bool = False

    def to_dict(self) -> dict:
        return {"action": self.action.value, "new_args": self.new_args,
                "rationale": self.rationale, "requires_human": self.requires_human}


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    attempt: int
    max_retries: int
    proposal: RepairProposal
    reason: str = ""

    def to_dict(self) -> dict:
        return {"should_retry": self.should_retry, "attempt": self.attempt,
                "max_retries": self.max_retries, "proposal": self.proposal.to_dict(),
                "reason": self.reason}


@dataclass(frozen=True)
class RepairOutcome:
    success: bool
    attempts: int
    resolved_by: str
    final_error: str | None = None
    diagnoses: tuple[FailureDiagnosis, ...] = ()

    def to_dict(self) -> dict:
        return {"success": self.success, "attempts": self.attempts,
                "resolved_by": self.resolved_by, "final_error": self.final_error,
                "diagnoses": [d.to_dict() for d in self.diagnoses]}


# ── diagnosis (deterministic) ─────────────────────────────────────────────────
# Order matters: safety-relevant causes (destructive/scope/auth) are matched
# BEFORE generic ones so they can never be mistaken for a retryable glitch.
def diagnose(failure: RuntimeFailure) -> FailureDiagnosis:
    err = (failure.error or "").lower()

    if failure.destructive:
        return FailureDiagnosis(RuntimeFailureType.DESTRUCTIVE_BLOCKED, retryable=False,
                                requires_human=True, cause="destructive operation — never auto-retried",
                                confidence=1.0)
    if any(k in err for k in ("scope", "not authorized", "unauthorized scope",
                              "authorization required", "requires authorization")):
        return FailureDiagnosis(RuntimeFailureType.SCOPE_DENIED, retryable=False,
                                requires_human=True, cause="scope/authorization denied — escalate, never expand",
                                confidence=0.9)
    if any(k in err for k in ("auth", "401", "403", "forbidden", "credential",
                              "token expired", "permission denied")):
        return FailureDiagnosis(RuntimeFailureType.AUTH_FAILURE, retryable=False,
                                requires_human=True, cause="authentication failure — needs human/credential fix",
                                confidence=0.8)
    if any(k in err for k in ("timeout", "timed out", "deadline")):
        return FailureDiagnosis(RuntimeFailureType.TIMEOUT, retryable=True, requires_human=False,
                                cause="transient timeout — a single bounded retry may succeed", confidence=0.7)
    if any(k in err for k in ("not found", "unavailable", "no such tool", "not permitted",
                              "unknown tool", "unsupported")):
        return FailureDiagnosis(RuntimeFailureType.UNAVAILABLE_CAPABILITY, retryable=False,
                                requires_human=False, cause="capability unavailable — retry cannot help",
                                confidence=0.8)
    if any(k in err for k in ("invalid argument", "missing required", "missing argument",
                              "typeerror", "validationerror", "keyerror", "required field",
                              "bad request", "invalid parameter")):
        return FailureDiagnosis(RuntimeFailureType.BAD_ARGUMENTS, retryable=True, requires_human=False,
                                cause="malformed arguments — retry only with corrected args", confidence=0.7)
    if any(k in err for k in ("json", "parse", "malformed", "decode", "unexpected token")):
        return FailureDiagnosis(RuntimeFailureType.MALFORMED_OUTPUT, retryable=True, requires_human=False,
                                cause="malformed model/tool output — one regeneration may fix", confidence=0.6)
    if any(k in err for k in ("modulenotfound", "importerror", "environment", "not installed",
                              "no module named")):
        return FailureDiagnosis(RuntimeFailureType.ENVIRONMENT_MISMATCH, retryable=False,
                                requires_human=False, cause="environment mismatch — retry cannot help",
                                confidence=0.7)
    return FailureDiagnosis(RuntimeFailureType.UNKNOWN, retryable=False, requires_human=False,
                            cause="unclassified failure — stop and report (no blind retry)", confidence=0.3)


def _sanitize_repair_args(new_args: dict | None) -> tuple[dict | None, str | None]:
    """A repair may only touch ordinary tool arguments. If it tries to introduce a
    key that would expand authority/scope/consent/force, reject the whole repair
    (fail-closed). Returns (safe_args, rejection_reason)."""
    if not new_args:
        return new_args, None
    bad = sorted(k for k in new_args if k.lower() in _FORBIDDEN_ARG_KEYS)
    if bad:
        return None, f"repair rejected — would touch privileged keys {bad}"
    return dict(new_args), None


# ── the self-debug runtime ────────────────────────────────────────────────────
AttemptFn = Callable[[dict], "Awaitable[tuple[bool, object]]"]  # args -> (ok, result_or_error)
ArgRepairFn = Callable[[RuntimeFailure], "dict | None"]
VerifyFn = Callable[[object], bool]


class SelfDebugRuntime:
    """Bounded diagnose→repair→retry loop. Never expands authority/scope, never
    bypasses HITL, never auto-retries destructive actions, never hides errors."""

    def __init__(self, *, max_retries: int = _DEFAULT_MAX_RETRIES) -> None:
        self.max_retries = max(0, min(int(max_retries), 3))   # hard ceiling of 3

    # ── decide whether/how to retry (pure) ───────────────────────────────────
    def decide_retry(self, failure: RuntimeFailure, *, retries_done: int,
                     arg_repair_fn: ArgRepairFn | None = None) -> RetryDecision:
        diag = diagnose(failure)

        # Hard cap first — never exceed the retry budget.
        if retries_done >= self.max_retries:
            return RetryDecision(False, retries_done, self.max_retries,
                                 RepairProposal(RepairAction.ABORT, rationale="retry budget exhausted"),
                                 reason="retry cap reached")

        # Non-retryable diagnoses → escalate or abort, never retry.
        if not diag.retryable:
            if diag.requires_human:
                return RetryDecision(False, retries_done, self.max_retries,
                                     RepairProposal(RepairAction.ESCALATE_HITL, requires_human=True,
                                                    rationale=diag.cause),
                                     reason=f"{diag.failure_type.value}: requires human")
            return RetryDecision(False, retries_done, self.max_retries,
                                 RepairProposal(RepairAction.ABORT, rationale=diag.cause),
                                 reason=f"{diag.failure_type.value}: not retryable")

        # Retryable: timeout / malformed → retry the same call once more.
        if diag.failure_type in (RuntimeFailureType.TIMEOUT, RuntimeFailureType.MALFORMED_OUTPUT):
            return RetryDecision(True, retries_done, self.max_retries,
                                 RepairProposal(RepairAction.RETRY_SAME, rationale=diag.cause),
                                 reason=f"{diag.failure_type.value}: bounded retry")

        # Bad arguments → retry ONLY if a repair produces different, safe args.
        if diag.failure_type is RuntimeFailureType.BAD_ARGUMENTS:
            proposed = arg_repair_fn(failure) if arg_repair_fn else None
            safe, rejection = _sanitize_repair_args(proposed)
            if rejection:
                logger.warning(f"SELF_DEBUG: {rejection}")
                return RetryDecision(False, retries_done, self.max_retries,
                                     RepairProposal(RepairAction.ABORT, rationale=rejection),
                                     reason="repair violated privilege boundary")
            if not safe or safe == failure.args:
                return RetryDecision(False, retries_done, self.max_retries,
                                     RepairProposal(RepairAction.ABORT,
                                                    rationale="no safe corrected arguments available"),
                                     reason="bad_arguments: no repair")
            return RetryDecision(True, retries_done, self.max_retries,
                                 RepairProposal(RepairAction.RETRY_WITH_ARGS, new_args=safe,
                                                rationale="retry with corrected arguments"),
                                 reason="bad_arguments: repaired args")

        return RetryDecision(False, retries_done, self.max_retries,
                             RepairProposal(RepairAction.ABORT, rationale=diag.cause),
                             reason="no applicable repair")

    # ── bounded execute-with-repair loop ─────────────────────────────────────
    async def run_with_repair(
        self, operation: str, attempt_fn: AttemptFn, initial_args: dict, *,
        destructive: bool = False, arg_repair_fn: ArgRepairFn | None = None,
        verify_fn: VerifyFn | None = None, now_ts: float = 0.0,
    ) -> RepairOutcome:
        """Run *attempt_fn* with bounded, safety-constrained repair. Destructive
        operations get exactly one attempt and are never auto-retried."""
        args = dict(initial_args)
        retries = 0
        diagnoses: list[FailureDiagnosis] = []
        last_error: str | None = None

        while True:
            try:
                ok, result = await attempt_fn(args)
            except Exception as e:  # noqa: BLE001 — a raising op is just a failure to diagnose
                ok, result = False, f"{type(e).__name__}: {e}"

            if ok and (verify_fn is None or _safe_verify(verify_fn, result)):
                return RepairOutcome(success=True, attempts=retries + 1, resolved_by="success",
                                     final_error=None, diagnoses=tuple(diagnoses))

            # Either the op failed, or it "succeeded" but failed verification.
            last_error = (str(result) if not ok
                          else "result failed post-verification")
            failure = RuntimeFailure(operation=operation, error=last_error, args=args,
                                     destructive=destructive, created_ts=now_ts)
            diagnoses.append(diagnose(failure))
            decision = self.decide_retry(failure, retries_done=retries, arg_repair_fn=arg_repair_fn)
            if not decision.should_retry:
                logger.info(f"SELF_DEBUG: {operation} stop — {decision.reason} "
                            f"(action={decision.proposal.action.value})")
                return RepairOutcome(success=False, attempts=retries + 1,
                                     resolved_by=decision.proposal.action.value,
                                     final_error=last_error, diagnoses=tuple(diagnoses))
            retries += 1
            if decision.proposal.action is RepairAction.RETRY_WITH_ARGS and decision.proposal.new_args:
                args = decision.proposal.new_args


def _safe_verify(verify_fn: VerifyFn, result: object) -> bool:
    try:
        return bool(verify_fn(result))
    except Exception:  # noqa: BLE001 — a throwing verifier fails closed (unverified)
        return False
