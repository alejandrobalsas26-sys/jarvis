"""
tests/test_runbook_engine.py — V66 M24 guarded runbook orchestration.

Covers the required M24 surface: legacy YAML loading, dry run, precondition
failure, scope denial, risk gating, HITL preservation, cancellation, timeout,
self-debug cap, postcondition verification, audit record, and no direct
world-effect bypass.
"""
from __future__ import annotations

import asyncio

from core.runbook_engine import (
    ParamType,
    RunbookDefinition,
    RunbookEngine,
    RunbookParameter,
    RunbookPostcondition,
    RunbookPrecondition,
    RunbookStep,
    StepKind,
)
from core.task_graph import CancelToken


class _RecordingExecutor:
    """A fake ToolExecutor.aexecute — records every call, returns a canned result.
    Its mere existence proves the engine never bypasses it (all effects funnel here)."""

    def __init__(self, results=None, fail=None):
        self.calls: list = []
        self._results = results or {}
        self._fail = fail or set()

    async def aexecute(self, tool, args, reasoning=""):
        self.calls.append((tool, dict(args), reasoning))
        if tool in self._fail:
            return {"error": f"{tool} failed"}
        return self._results.get(tool, {"ok": True, "tool": tool})


def _engine(**kw) -> RunbookEngine:
    return RunbookEngine(**kw)


# ── dry run (no execution) ────────────────────────────────────────────────────
def test_dry_run_plans_without_executing():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = eng.dry_run("NEW_SERVICE_EXPOSURE_REVIEW", {"host": "192.168.56.10"})
    assert res.status == "dry_run"
    assert ex.calls == []                       # NOTHING ran
    assert res.plan is not None
    # the active scan step is flagged HITL-required in the plan
    assert "scan" in res.plan.requires_hitl_steps
    assert "192.168.56.10" in res.plan.scope_targets


def test_dry_run_via_execute_flag():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("SERVICE_DIAGNOSIS", {"host": "10.0.0.1"}, dry_run=True))
    assert res.status == "dry_run"
    assert ex.calls == []


# ── read-only diagnostic execution routes through the executor ────────────────
def test_diagnostic_execution_uses_guarded_executor():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("SERVICE_DIAGNOSIS", {"host": "10.0.0.1"}))
    assert res.status == "completed"
    tools = [c[0] for c in ex.calls]
    assert tools == ["check_connectivity", "system_info"]     # all via the gate
    assert all(a.status == "completed" for a in res.audit)


def test_no_world_effect_bypass_without_executor():
    # No executor wired → an action cannot effect the world; it fails closed.
    eng = _engine()
    res = asyncio.run(eng.execute("HOST_CONNECTIVITY_DIAGNOSIS", {"host": "10.0.0.1"}))
    assert res.status in ("failed", "partial")
    assert any("no tool executor" in a.summary for a in res.audit)


# ── parameter validation ──────────────────────────────────────────────────────
def test_parameter_validation_rejects_bad_target():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("HOST_CONNECTIVITY_DIAGNOSIS", {"host": "10.0.0.1; rm -rf /"}))
    assert res.status == "blocked"
    assert ex.calls == []                       # never reached execution


def test_missing_required_param_blocks():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("HOST_CONNECTIVITY_DIAGNOSIS", {}))
    assert res.status == "blocked"


# ── precondition failure ──────────────────────────────────────────────────────
def test_precondition_failure_blocks_step():
    ex = _RecordingExecutor()

    def always_fail(ctx):
        return False, "simulated precondition failure"

    rb = RunbookDefinition(
        name="RB_PRE", description="",
        parameters=(RunbookParameter("host", ParamType.TARGET),),
        steps=(RunbookStep("s1", "gated step", StepKind.DIAGNOSTIC, "system_info", {},
                           precondition=RunbookPrecondition("p", always_fail)),))
    eng = _engine(tool_executor=ex)
    eng.registry.register(rb)
    res = asyncio.run(eng.execute("RB_PRE", {"host": "10.0.0.1"}))
    assert ex.calls == []
    assert any(a.status == "blocked" and "precondition" in a.summary for a in res.audit)


# ── postcondition verification ────────────────────────────────────────────────
def test_postcondition_failure_fails_step():
    ex = _RecordingExecutor(results={"system_info": {"ok": True}})

    def post_fail(ctx):
        return False, "expected state not reached"

    rb = RunbookDefinition(
        name="RB_POST", description="",
        parameters=(RunbookParameter("host", ParamType.TARGET),),
        steps=(RunbookStep("s1", "step", StepKind.DIAGNOSTIC, "system_info", {},
                           postcondition=RunbookPostcondition("p", post_fail)),))
    eng = _engine(tool_executor=ex)
    eng.registry.register(rb)
    res = asyncio.run(eng.execute("RB_POST", {"host": "10.0.0.1"}))
    assert len(ex.calls) == 1                    # the tool DID run
    assert any(a.status == "failed" and "postcondition" in a.summary for a in res.audit)
    assert res.status in ("failed", "partial")


# ── HITL preservation (explicit approval gate, fail-closed) ───────────────────
def test_hitl_approval_gate_fail_closed():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    # no approval_fn → the gated scan step is blocked, and never reaches aexecute
    res = asyncio.run(eng.execute("IDS_ALERT_INVESTIGATION", {"target": "192.168.56.10"}))
    tools = [c[0] for c in ex.calls]
    assert "network_scan" not in tools          # gated action never ran
    assert any(a.step_id == "scan" and a.status == "blocked" for a in res.audit)


def test_hitl_approval_granted_runs_gated_action():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)

    async def approve(step):
        return True

    res = asyncio.run(eng.execute("IDS_ALERT_INVESTIGATION", {"target": "192.168.56.10"},
                                  approval_fn=approve))
    tools = [c[0] for c in ex.calls]
    assert "network_scan" in tools              # approved → routed through the gate
    assert res.status == "completed"


# ── scope denial (executor refuses out-of-scope) ──────────────────────────────
def test_scope_denial_recorded_as_failed_not_bypassed():
    # The guarded executor returns an authority-scope error; the engine records it
    # and never treats the effect as successful.
    ex = _RecordingExecutor(fail={"check_connectivity"})
    ex._results = {}
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("HOST_CONNECTIVITY_DIAGNOSIS", {"host": "8.8.8.8"}))
    assert any(a.status == "failed" for a in res.audit)
    assert res.status in ("failed", "partial")


# ── risk gating classification present in audit + plan ────────────────────────
def test_risk_classification_in_plan():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    plan = eng.dry_run("IDS_ALERT_INVESTIGATION", {"target": "192.168.56.10"}).plan
    by_id = {s["id"]: s for s in plan.steps}
    assert by_id["connectivity"]["risk_class"] == "read_only"
    assert by_id["scan"]["risk_class"] == "high_impact"
    assert by_id["scan"]["requires_hitl"] is True


# ── self-debug cap (bounded retries; no destructive auto-retry) ───────────────
def test_self_debug_bounded_retry_on_timeout():
    attempts = {"n": 0}

    class _FlakyExec:
        async def aexecute(self, tool, args, reasoning=""):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"error": "operation timed out"}    # retryable once
            return {"ok": True}

    rb = RunbookDefinition(
        name="RB_FLAKY", description="",
        parameters=(RunbookParameter("host", ParamType.TARGET),),
        steps=(RunbookStep("s1", "flaky", StepKind.DIAGNOSTIC, "system_info", {}),))
    eng = _engine(tool_executor=_FlakyExec())
    eng.registry.register(rb)
    res = asyncio.run(eng.execute("RB_FLAKY", {"host": "10.0.0.1"}))
    assert attempts["n"] == 2                    # exactly one bounded retry
    assert res.status == "completed"


def test_self_debug_no_destructive_auto_retry():
    attempts = {"n": 0}

    class _FailExec:
        async def aexecute(self, tool, args, reasoning=""):
            attempts["n"] += 1
            return {"error": "operation timed out"}

    rb = RunbookDefinition(
        name="RB_DESTRUCTIVE", description="",
        parameters=(RunbookParameter("host", ParamType.TARGET),),
        steps=(RunbookStep("s1", "destructive", StepKind.ACTION, "kill_process",
                           {"pid": 1}, destructive=True),))
    eng = _engine(tool_executor=_FailExec())
    eng.registry.register(rb)

    async def approve(step):
        return True

    res = asyncio.run(eng.execute("RB_DESTRUCTIVE", {"host": "10.0.0.1"}, approval_fn=approve))
    assert attempts["n"] == 1                    # destructive → exactly one attempt
    assert res.status in ("failed", "partial")


# ── cancellation ──────────────────────────────────────────────────────────────
def test_cancellation_stops_the_run():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    token = CancelToken()
    token.cancel()      # pre-cancelled
    res = asyncio.run(eng.execute("INCIDENT_EVIDENCE_COLLECTION", {"host": "10.0.0.1"},
                                  cancel=token))
    assert res.status == "cancelled"
    assert ex.calls == []


# ── audit record ──────────────────────────────────────────────────────────────
def test_audit_trail_records_every_step():
    ex = _RecordingExecutor()
    eng = _engine(tool_executor=ex)
    res = asyncio.run(eng.execute("INCIDENT_EVIDENCE_COLLECTION", {"host": "10.0.0.1"}))
    step_ids = {a.step_id for a in res.audit}
    assert step_ids == {"host_info", "processes"}
    for a in res.audit:
        assert a.risk_class and a.ts and a.status


# ── legacy YAML loading + migration ───────────────────────────────────────────
def test_legacy_playbook_migration():
    legacy = {
        "name": "ransomware_response", "description": "legacy",
        "trigger": {"incident_type": "credential_harvesting", "severity_min": 7},
        "steps": [
            {"action": "broadcast_alert", "params": {"message": "hi"}},
            {"action": "run_nmap", "params": {"target": "10.0.0.5"}},
            {"action": "isolate_ip", "params": {"ip": "10.0.0.5"}},
        ],
    }
    rb = RunbookDefinition.from_legacy_playbook(legacy)
    kinds = {s.description: s for s in rb.steps}
    # a notification becomes a non-effecting REASON step
    assert kinds["broadcast_alert"].kind is StepKind.REASON
    assert kinds["broadcast_alert"].action is None
    # run_nmap migrates to the guarded network_scan tool, HITL-gated
    assert kinds["run_nmap"].action == "network_scan"
    assert kinds["run_nmap"].requires_approval is True
    # isolate_ip is migrated to a guarded action (no direct side-effect)
    assert kinds["isolate_ip"].kind is StepKind.ACTION
    assert kinds["isolate_ip"].requires_approval is True
    # legacy matching preserved
    assert rb.matches({"rule": "credential_harvesting", "severity_score": 8})
    assert not rb.matches({"rule": "other", "severity_score": 8})


def test_legacy_playbook_no_bypass():
    # A migrated isolate_ip routes through aexecute (recorded), never a direct call.
    ex = _RecordingExecutor()
    legacy = {"name": "iso", "description": "",
              "steps": [{"action": "isolate_ip", "params": {"ip": "10.0.0.5"}}]}
    eng = _engine(tool_executor=ex)
    eng.registry.register(RunbookDefinition.from_legacy_playbook(legacy))

    async def approve(step):
        return True

    asyncio.run(eng.execute("iso", {}, approval_fn=approve))
    assert [c[0] for c in ex.calls] == ["isolate_ip"]    # via the guarded gate only


# ── built-in runbook classes present ──────────────────────────────────────────
def test_all_required_runbook_classes_registered():
    eng = _engine()
    required = {"SERVICE_DIAGNOSIS", "CONTAINER_HEALTH_CHECK", "HOST_CONNECTIVITY_DIAGNOSIS",
                "AUTH_FAILURE_TRIAGE", "IDS_ALERT_INVESTIGATION", "NEW_SERVICE_EXPOSURE_REVIEW",
                "INCIDENT_EVIDENCE_COLLECTION"}
    assert required <= set(eng.registry.names())
