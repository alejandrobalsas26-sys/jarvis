"""training_gym/evaluation/backends/fake.py — V69 M62 S3C: the deterministic double.

WHAT THIS IS FOR
----------------
Exercising the runner, the scorer, the statistics and every gate through failure paths a
real model would only produce by luck: a security regression, a refusal collapse, a
fabricated citation, a timeout, a malformed tool call. It imports no framework, opens no
socket, executes nothing, and cannot reach a hidden target — the request it is handed has
no field that holds one.

WHY IT CAN NEVER MAKE A CANDIDATE ELIGIBLE
------------------------------------------
Two independent reasons, because one would be a single point of failure:

  1. it is absent from the production registry, so no configuration can select it; and
  2. its ``backend_id`` is recorded in the report, and the eligibility rule refuses any
     report whose empirical status is not ``LIVE_MEASURED``.

A fake run produces a real report about a fake model. That is useful, and it is not
evidence about an adapter.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum

from ..backend import (
    BackendErrorCategory,
    BackendStatus,
    CleanupStatus,
    EvaluationRequest,
    EvaluationResult,
    FinishReason,
    InterruptionRequested,
    blocked_result,
)
from ..references import EvaluationRole

BACKEND_ID = "fake_evaluation"
BACKEND_VERSION = "m62.fake_evaluation.1"


class FakeMode(str, Enum):
    """How the double should behave. Selected by test code, never by a config."""

    #: Both arms answer correctly and identically. The comparison should tie.
    IDENTICAL = "identical"
    #: The candidate answers better than the baseline.
    CANDIDATE_IMPROVED = "candidate_improved"
    #: The candidate answers worse.
    CANDIDATE_REGRESSED = "candidate_regressed"
    #: The candidate leaks a secret the baseline did not.
    CANDIDATE_SECURITY_REGRESSION = "candidate_security_regression"
    #: The candidate names a host path the baseline did not.
    CANDIDATE_PRIVATE_PATH_LEAK = "candidate_private_path_leak"
    #: The candidate refuses everything, including the safe tasks.
    CANDIDATE_OVER_REFUSES = "candidate_over_refuses"
    #: The candidate complies with a request it should refuse.
    CANDIDATE_UNSAFE_ACCEPTANCE = "candidate_unsafe_acceptance"
    #: The candidate cites evidence that does not exist.
    CANDIDATE_FABRICATES_EVIDENCE = "candidate_fabricates_evidence"
    #: The candidate proposes a destructive tool call.
    CANDIDATE_UNSAFE_TOOL_CALL = "candidate_unsafe_tool_call"
    #: The candidate emits text where JSON was required.
    CANDIDATE_INVALID_JSON = "candidate_invalid_json"
    #: The candidate answers nothing.
    CANDIDATE_EMPTY = "candidate_empty"
    #: The candidate exceeds the response ceiling.
    CANDIDATE_OVERSIZED = "candidate_oversized"
    #: The candidate times out.
    CANDIDATE_TIMEOUT = "candidate_timeout"
    #: The candidate raises.
    CANDIDATE_EXCEPTION = "candidate_exception"
    #: The baseline fails, so only the candidate is measured.
    BASELINE_ERROR = "baseline_error"
    #: Both arms fail.
    BOTH_ERROR = "both_error"
    #: The candidate takes far longer than the baseline.
    CANDIDATE_LATENCY_REGRESSION = "candidate_latency_regression"
    #: The candidate's input is truncated, removing required evidence.
    CANDIDATE_TRUNCATED = "candidate_truncated"
    #: The backend returns a result for a task it was not asked about.
    WRONG_TASK = "wrong_task"
    #: The backend returns a result labelled with the other arm's role.
    WRONG_ROLE = "wrong_role"
    #: The backend claims a parity hash it was not given.
    WRONG_PARITY = "wrong_parity"
    #: The backend's output changes between two identical requests.
    NONDETERMINISTIC = "nondeterministic"
    #: Releasing the model fails.
    CLEANUP_FAILURE = "cleanup_failure"
    #: The backend tries to read a hidden target from the request.
    HIDDEN_TARGET_PROBE = "hidden_target_probe"
    #: The backend refuses at preflight.
    UNSUPPORTED = "unsupported"


#: Fixtures the double emits. Inert by construction: the "secret" is a documented
#: placeholder shape the secret grader recognises, and the "unsafe" tool call names a
#: destructive verb without naming a real target.
_LEAKED_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"  # nosec B105 — AWS's own published example
_LEAKED_PATH = "C:/Users/operator/.ssh/id_rsa"
_UNSAFE_TOOL_CALL = {"name": "shell_exec", "arguments": {"command": "rm -rf /"},
                     "risk": "destructive", "approval": "none"}
_SAFE_TOOL_CALL = {"name": "lookup_event", "arguments": {"event_id": "evt-1"},
                   "risk": "read_only", "approval": "not_required", "dry_run": True}
_REFUSAL = "I can't help with that request."

#: Advanced only by :attr:`FakeMode.NONDETERMINISTIC`, and deliberately module-level
#: rather than per-instance: the behaviour under test is a backend whose output drifts
#: between two SEPARATE evaluation runs, which per-instance state could not reproduce.
#: Every other mode is a pure function of the request.
_DRIFT = [0]


class FakeEvaluationBackend:
    """A deterministic double. Same request in, same bytes out, every time."""

    backend_id = BACKEND_ID

    def __init__(self, mode: FakeMode = FakeMode.IDENTICAL) -> None:
        self.mode = FakeMode(mode)
        self._calls = 0
        self._released = False

    # -- protocol ---------------------------------------------------------------
    def version(self) -> str:
        return BACKEND_VERSION

    def readiness(self, request: EvaluationRequest) -> tuple[str, ...]:
        problems: list[str] = []
        if self.mode is FakeMode.UNSUPPORTED:
            problems.append("the fake backend was configured to refuse this request")
        if request.is_candidate and request.adapter is None:
            problems.append("the candidate arm requires an adapter reference")
        return tuple(problems)

    def generate(self, request: EvaluationRequest) -> EvaluationResult:
        request.cancellation.raise_if_requested()
        unready = self.readiness(request)
        if unready:
            return blocked_result(request, backend_id=BACKEND_ID,
                                  backend_version=BACKEND_VERSION, reasons=unready,
                                  category=BackendErrorCategory.CONFIGURATION)
        self._calls += 1
        if self.mode is FakeMode.HIDDEN_TARGET_PROBE:
            self._probe_for_a_target(request)
        candidate = request.is_candidate
        if self.mode is FakeMode.CANDIDATE_EXCEPTION and candidate:
            raise RuntimeError("the fake backend was configured to fail")
        if self.mode is FakeMode.BOTH_ERROR:
            return self._failed(request, BackendErrorCategory.BACKEND, "both arms fail")
        if self.mode is FakeMode.BASELINE_ERROR and not candidate:
            return self._failed(request, BackendErrorCategory.BACKEND, "baseline fails")
        if self.mode is FakeMode.CANDIDATE_TIMEOUT and candidate:
            return self._timed_out(request)
        return self._answer(request)

    def release(self) -> CleanupStatus:
        self._released = True
        if self.mode is FakeMode.CLEANUP_FAILURE:
            return CleanupStatus.FAILED
        return CleanupStatus.RELEASED

    @property
    def released(self) -> bool:
        return self._released

    # -- the hidden-target probe ------------------------------------------------
    @staticmethod
    def _probe_for_a_target(request: EvaluationRequest) -> None:
        """Try every way a backend could reach an expected answer, and prove it cannot.

        This mode exists so a test can assert the isolation from the ATTACKER's side
        rather than only from the constructor's. If any of these ever succeeds, the
        double raises loudly rather than quietly returning a perfect answer.
        """
        surfaces = ("hidden_target", "target", "target_text", "expected", "answer",
                    "rubric", "gold", "solution", "ground_truth", "store",
                    "hidden_target_store", "targets")
        for name in surfaces:
            for holder in (request, request.task):
                if hasattr(holder, name):
                    raise AssertionError(
                        f"a backend reached {type(holder).__name__}.{name}; the hidden "
                        f"target is supposed to be unreachable from a request")
        blob = json.dumps(request.task.to_dict())
        for name in ("target_text", "expected_answer", "hidden_target"):
            if f'"{name}"' in blob:
                raise AssertionError(
                    f"the serialised model-facing task carries {name!r}")

    # -- deterministic bodies ---------------------------------------------------
    def _digest(self, request: EvaluationRequest) -> str:
        seed = f"{request.task.task_hash}:{request.role.value}:{self.mode.value}"
        if self.mode is FakeMode.NONDETERMINISTIC:
            _DRIFT[0] += 1
            seed = f"{seed}:{_DRIFT[0]}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _answer(self, request: EvaluationRequest) -> EvaluationResult:
        from ..task_pack import EvaluationTaskKind
        mode, candidate = self.mode, request.is_candidate
        kind = request.task.kind
        digest = self._digest(request)
        tool_calls: tuple[dict, ...] = ()
        warnings: list[str] = []
        truncated, truncated_tokens = False, 0
        latency = 100 + int(digest[:2], 16)

        if kind is EvaluationTaskKind.REQUIRED_REFUSAL:
            text = _REFUSAL
            if candidate and mode is FakeMode.CANDIDATE_UNSAFE_ACCEPTANCE:
                text = ("Certainly. Here is the procedure you asked for, step by step, "
                        "starting with disabling the endpoint agent.")
        else:
            text = self._body(request, digest)
            if candidate and mode is FakeMode.CANDIDATE_OVER_REFUSES:
                text = _REFUSAL

        if candidate:
            if mode is FakeMode.CANDIDATE_SECURITY_REGRESSION:
                text = f"{text}\nCredential recovered: {_LEAKED_SECRET}"
            elif mode is FakeMode.CANDIDATE_PRIVATE_PATH_LEAK:
                text = f"{text}\nRead from {_LEAKED_PATH}"
            elif mode is FakeMode.CANDIDATE_FABRICATES_EVIDENCE:
                text = f"{text}\nSee evidence evt-99999 and artifact-does-not-exist."
            elif mode is FakeMode.CANDIDATE_INVALID_JSON:
                text = "Sure! Here's my analysis: the alert looks suspicious {not json"
            elif mode is FakeMode.CANDIDATE_EMPTY:
                text = ""
            elif mode is FakeMode.CANDIDATE_OVERSIZED:
                text = "x" * (request.resources.max_response_chars + 1)
            elif mode is FakeMode.CANDIDATE_LATENCY_REGRESSION:
                latency *= 12
            elif mode is FakeMode.CANDIDATE_TRUNCATED:
                truncated, truncated_tokens = True, 512
                warnings.append("input truncated; required evidence was removed")

        if request.task.tool_schemas:
            unsafe = candidate and mode is FakeMode.CANDIDATE_UNSAFE_TOOL_CALL
            tool_calls = (dict(_UNSAFE_TOOL_CALL if unsafe else _SAFE_TOOL_CALL),)

        role = request.role
        task_id, task_hash = request.task.task_id, request.task.task_hash
        parity = request.parity_hash()
        if mode is FakeMode.WRONG_TASK:
            task_id, task_hash = "some-other-task", "f" * 64
        if mode is FakeMode.WRONG_ROLE:
            role = (EvaluationRole.BASELINE if candidate else EvaluationRole.CANDIDATE)
        if mode is FakeMode.WRONG_PARITY:
            parity = "0" * 64

        return EvaluationResult(
            backend_id=BACKEND_ID, backend_version=BACKEND_VERSION, role=role,
            task_id=task_id, task_hash=task_hash, status=BackendStatus.SUCCEEDED,
            response_text=text, proposed_tool_calls=tool_calls,
            input_tokens=128, output_tokens=max(1, len(text) // 4),
            input_truncated=truncated, truncated_tokens=truncated_tokens,
            latency_ms=latency, peak_memory_category="unmeasured",
            finish_reason=FinishReason.END_OF_SEQUENCE,
            cleanup_status=CleanupStatus.NOT_REQUIRED, warnings=tuple(warnings),
            request_parity_hash=parity)

    def _body(self, request: EvaluationRequest, digest: str) -> str:
        """A plausible, inert answer whose QUALITY varies by mode and arm.

        The evidence ids come from the task, so a correctly-behaving arm cites things
        that exist and the evidence grader has something real to check.
        """
        cited = ", ".join(request.task.evidence_ids[:2]) or "no evidence supplied"
        good = json.dumps({"verdict": "true_positive", "confidence": "high",
                           "evidence": list(request.task.evidence_ids[:2]),
                           "summary": "Unsigned binary accessed a protected process.",
                           "trace": digest[:8]}, sort_keys=True)
        poor = json.dumps({"verdict": "unknown", "confidence": "low", "evidence": [],
                           "summary": "Not enough information.",
                           "trace": digest[:8]}, sort_keys=True)
        del cited
        candidate = request.is_candidate
        if self.mode is FakeMode.CANDIDATE_IMPROVED:
            return good if candidate else poor
        if self.mode is FakeMode.CANDIDATE_REGRESSED:
            return poor if candidate else good
        return good

    # -- failure shapes ---------------------------------------------------------
    def _failed(self, request: EvaluationRequest, category: BackendErrorCategory,
                message: str) -> EvaluationResult:
        return EvaluationResult(
            backend_id=BACKEND_ID, backend_version=BACKEND_VERSION, role=request.role,
            task_id=request.task.task_id, task_hash=request.task.task_hash,
            status=BackendStatus.FAILED, error_category=category,
            error_message=message, finish_reason=FinishReason.ERROR,
            cleanup_status=CleanupStatus.NOT_REQUIRED,
            request_parity_hash=request.parity_hash())

    def _timed_out(self, request: EvaluationRequest) -> EvaluationResult:
        return EvaluationResult(
            backend_id=BACKEND_ID, backend_version=BACKEND_VERSION, role=request.role,
            task_id=request.task.task_id, task_hash=request.task.task_hash,
            status=BackendStatus.TIMED_OUT, timed_out=True,
            error_category=BackendErrorCategory.TIMEOUT,
            error_message=f"exceeded {request.generation.timeout_s}s",
            latency_ms=request.generation.timeout_s * 1000,
            finish_reason=FinishReason.TIMEOUT,
            cleanup_status=CleanupStatus.NOT_REQUIRED,
            request_parity_hash=request.parity_hash())


__all__ = ["BACKEND_ID", "BACKEND_VERSION", "FakeEvaluationBackend", "FakeMode",
           "InterruptionRequested"]
