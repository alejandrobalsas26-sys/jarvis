"""
core/ops_views.py — V66 Milestone 26: bounded, redacted AURA operational views.

Extends the existing AURA surface (it does NOT build a second frontend). It turns
the operational-state spine (M20–M25) into the typed events declared in
``core.aura_events`` and into seven bounded HUD panels — ASSET STATUS, INCIDENTS,
DRIFT, SENSOR HEALTH, CORRELATIONS, RUNBOOKS, CURRENT SITUATION — each safe to
broadcast over the existing WebSocket pipeline.

Redaction is the point. Nothing here ever emits raw PCAP, massive log payloads,
credentials, private keys, tokens, unredacted command history, or a whole memory
store. Every free-text field is passed through the reused HUD redaction
(``feed_sanitizer.sanitize_for_hud`` + ``memory_router.redact_secrets``) and a
sensitive-key denylist; every list is length-capped. The builders are pure
functions of injected objects (or the live singletons), so they are testable
without a running HUD.
"""
from __future__ import annotations

from core.aura_events import (
    AssetConflictEvent,
    AssetGraphUpdatedEvent,
    CorrelationFindingEvent,
    DriftFindingEvent,
    IncidentCaseUpdatedEvent,
    RunbookExecutionEvent,
    RunbookPlanEvent,
    ServiceHealthEvent,
    SituationSnapshotEvent,
    VerificationOutcomeEvent,
)

# Bounds — no panel ever dumps an unbounded collection.
_MAX_LIST = 12
_MAX_TEXT = 200

# Keys that must NEVER reach the HUD, regardless of source.
_FORBIDDEN_KEYS = frozenset({
    "command_line", "commandline", "cmdline", "password", "secret", "token",
    "api_key", "apikey", "private_key", "credential", "credentials", "id_rsa",
    "env", ".env", "raw", "payload", "pcap", "memory", "history", "untrusted_text",
})


def _redact(text) -> str:
    """Reuse the existing HUD redaction: strip secrets, then bound + escape."""
    s = "" if text is None else str(text)
    try:
        from core.memory_router import redact_secrets
        s = redact_secrets(s)
    except Exception:  # noqa: BLE001 — redaction must never crash a broadcast
        pass
    try:
        from core.feed_sanitizer import sanitize_for_hud
        return sanitize_for_hud(s, max_length=_MAX_TEXT)
    except Exception:  # noqa: BLE001
        return s[:_MAX_TEXT]


def _scrub(d: dict) -> dict:
    """Drop forbidden keys and redact string values from a dict projection."""
    out: dict = {}
    for k, v in (d or {}).items():
        if str(k).lower() in _FORBIDDEN_KEYS:
            continue
        if isinstance(v, str):
            out[k] = _redact(v)
        elif isinstance(v, dict):
            out[k] = _scrub(v)
        elif isinstance(v, list):
            out[k] = v[:_MAX_LIST]
        else:
            out[k] = v
    return out


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Typed event builders (bounded + redacted projections)
# ══════════════════════════════════════════════════════════════════════════════
def asset_graph_event(graph) -> dict:
    assets = list(getattr(graph, "assets", {}).values())
    conflicts = graph.get_conflicts() if hasattr(graph, "get_conflicts") else []
    unknown = 0
    try:
        from core.asset_graph import AssetType
        for a in assets:
            if a.current_type() is AssetType.UNKNOWN or a.conflicts():
                unknown += 1
    except Exception:  # noqa: BLE001
        pass
    changed = [a.id for a in assets[:_MAX_LIST]]
    return AssetGraphUpdatedEvent(
        known=len(assets), healthy=max(0, len(assets) - unknown),
        degraded=0, unknown=unknown, conflicts=len(conflicts),
        changed_assets=changed).to_dict()


def asset_conflict_event(conflict) -> dict:
    d = _as_dict(conflict)
    return AssetConflictEvent(
        asset_id=_redact(d.get("asset_id", "")), attribute=_redact(d.get("attribute", "")),
        current_value=_redact(d.get("current_value", "")),
        values=[{"value": _redact(v.get("value")), "confidence": v.get("confidence"),
                 "source": v.get("source")} for v in d.get("values", [])[:_MAX_LIST]]).to_dict()


def service_health_event(svc: dict) -> dict:
    exposure = str(svc.get("exposure", "unknown"))
    return ServiceHealthEvent(
        host=_redact(svc.get("host", "")), port=int(svc.get("port") or 0),
        service=_redact(svc.get("service_name") or svc.get("protocol") or ""),
        exposure=exposure, reachable=exposure.lower() not in ("localhost", "")).to_dict()


def correlation_finding_event(finding) -> dict:
    d = _as_dict(finding)
    return CorrelationFindingEvent(
        finding_id=d.get("finding_id", ""), rule=d.get("rule", ""),
        group_entity=_redact(d.get("group_entity", "")), severity=d.get("severity", "unknown"),
        confidence=float(d.get("confidence", 0.0) or 0.0),
        techniques=list(d.get("mitre_techniques", []))[:_MAX_LIST],
        explanation=_redact(d.get("explanation", {}).get("reason", "")),
        matched=len(d.get("matched_event_ids", []))).to_dict()


def incident_case_event(case) -> dict:
    d = _as_dict(case)
    return IncidentCaseUpdatedEvent(
        incident_id=d.get("incident_id", ""), title=_redact(d.get("title", "")),
        status=d.get("status", "new"), severity=d.get("severity", "medium"),
        open_questions=len([q for q in d.get("open_questions", [])
                            if q.get("status") == "open"]),
        hypotheses=len(d.get("hypotheses", [])),
        findings=len(d.get("correlation_findings", [])),
        proposals=len(d.get("proposed_actions", []))).to_dict()


def drift_finding_event(finding) -> dict:
    d = _as_dict(finding)
    return DriftFindingEvent(
        asset=_redact(d.get("asset", "")), drift_type=d.get("drift_type", ""),
        severity=d.get("severity", "low"),
        recommended_investigation=d.get("recommended_investigation", ""),
        confidence=float(d.get("confidence", 0.0) or 0.0),
        verification_required=bool(d.get("verification_required", True))).to_dict()


def situation_event(snapshot) -> dict:
    d = _as_dict(snapshot)
    summary = _scrub(d.get("summary", {}))
    top = d.get("summary", {}).get("top_priority")
    return SituationSnapshotEvent(
        severity=d.get("severity", "calm"), summary=summary,
        top_priority=_scrub(top) if top else None,
        recommended_next_step=d.get("summary", {}).get("recommended_next_step",
                                                        "monitor")).to_dict()


def runbook_plan_event(plan_result) -> dict:
    d = _as_dict(plan_result)
    plan = d.get("plan") or {}
    return RunbookPlanEvent(
        runbook=d.get("runbook", ""), steps=len(plan.get("steps", [])),
        requires_hitl_steps=list(plan.get("requires_hitl_steps", []))[:_MAX_LIST],
        scope_targets=[_redact(t) for t in plan.get("scope_targets", [])[:_MAX_LIST]]).to_dict()


def runbook_execution_event(result) -> dict:
    d = _as_dict(result)
    audit = d.get("audit", [])
    return RunbookExecutionEvent(
        runbook=d.get("runbook", ""), status=d.get("status", "completed"),
        steps_completed=sum(1 for a in audit if a.get("status") == "completed"),
        steps_failed=sum(1 for a in audit if a.get("status") == "failed"),
        steps_blocked=sum(1 for a in audit if a.get("status") == "blocked")).to_dict()


def verification_outcome_event(subject: str, verified: bool, confidence: float,
                               note: str = "") -> dict:
    return VerificationOutcomeEvent(
        subject=_redact(subject), verified=bool(verified),
        confidence=float(confidence), note=_redact(note)).to_dict()


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded panels
# ══════════════════════════════════════════════════════════════════════════════
def asset_status_panel(graph) -> dict:
    assets = list(getattr(graph, "assets", {}).values())
    unknown = 0
    degraded = 0
    try:
        from core.asset_graph import AssetType
        for a in assets:
            if a.current_type() is AssetType.UNKNOWN or a.conflicts():
                unknown += 1
    except Exception:  # noqa: BLE001
        pass
    conflicts = graph.get_conflicts() if hasattr(graph, "get_conflicts") else []
    services = graph.exposed_services(only_reachable=True) if hasattr(graph, "exposed_services") else []
    return {
        "panel": "asset_status",
        "known": len(assets), "unknown": unknown, "degraded": degraded,
        "healthy": max(0, len(assets) - unknown - degraded),
        "conflicts": len(conflicts),
        "exposed_services": [service_health_event(s) for s in services[:_MAX_LIST]],
    }


def incidents_panel(open_cases) -> dict:
    cases = [_as_dict(c) for c in (open_cases or [])]
    cases = [c for c in cases if c.get("status") not in ("closed", "false_positive")]
    return {
        "panel": "incidents",
        "open": len(cases),
        "critical": sum(1 for c in cases if c.get("severity") == "critical"),
        "cases": [incident_case_event(c) for c in cases[:_MAX_LIST]],
    }


def drift_panel(twin_snapshot) -> dict:
    d = _as_dict(twin_snapshot)
    findings = d.get("findings", [])
    return {
        "panel": "drift",
        "count": len(findings),
        "by_severity": d.get("by_severity", {}),
        "findings": [drift_finding_event(f) for f in findings[:_MAX_LIST]],
    }


def sensor_health_panel(sensors: dict) -> dict:
    items = {}
    down = 0
    for name, status in (sensors or {}).items():
        st = _redact(status)
        items[_redact(name)] = st
        if any(w in str(status).lower() for w in ("down", "disconnected", "offline", "inactive")):
            down += 1
    return {"panel": "sensor_health", "sensors": items, "degraded": down,
            "total": len(items)}


def correlations_panel(findings) -> dict:
    items = [correlation_finding_event(f) for f in (findings or [])[:_MAX_LIST]]
    return {"panel": "correlations", "count": len(findings or []), "recent": items}


def runbooks_panel(engine) -> dict:
    names = engine.registry.names() if engine and hasattr(engine, "registry") else []
    return {"panel": "runbooks", "available": names[:_MAX_LIST], "count": len(names)}


def situation_panel(snapshot) -> dict:
    d = _as_dict(snapshot)
    return {
        "panel": "current_situation",
        "severity": d.get("severity", "calm"),
        "summary": _scrub(d.get("summary", {})),
        "priorities": [_scrub(p) for p in d.get("priorities", [])[:_MAX_LIST]],
        "recommendations": d.get("recommendations", [])[:_MAX_LIST],
        "uncertainties": [_redact(u) for u in d.get("uncertainties", [])[:_MAX_LIST]],
    }


def system_status_panel(*, graph=None, open_cases=None, twin_snapshot=None,
                        sensors=None, findings=None, situation=None) -> dict:
    """The combined SYSTEM STATUS view (the M26 example), each leg bounded/redacted."""
    latest = None
    if findings:
        latest = correlation_finding_event(list(findings)[-1])
    return {
        "panel": "system_status",
        "assets": asset_status_panel(graph) if graph is not None else {},
        "sensors": sensor_health_panel(sensors or {}),
        "incidents": incidents_panel(open_cases or []),
        "drift": drift_panel(twin_snapshot) if twin_snapshot is not None else {"count": 0},
        "latest_correlation": latest,
        "situation": situation_panel(situation) if situation is not None else {},
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Live-singleton convenience (guarded; reads only)
# ══════════════════════════════════════════════════════════════════════════════
def build_live_system_status(*, sensors: dict | None = None) -> dict:
    """Assemble the SYSTEM STATUS panel from the live V66 singletons. Read-only;
    never triggers a computation with a world-effect."""
    graph = incidents = twin_snap = findings = situation = None
    try:
        from core.asset_graph import graph as _graph
        graph = _graph
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.incident_workspace import workspace
        incidents = workspace.open_cases()
    except Exception:  # noqa: BLE001
        incidents = []
    try:
        from core.digital_twin import twin
        twin_snap = twin.compute_drift()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.correlation_v2 import correlator_v2
        findings = correlator_v2.recent(_MAX_LIST)
    except Exception:  # noqa: BLE001
        findings = []
    try:
        from core.situation_engine import engine as _sit
        situation = _sit.build(
            asset_graph=graph, incidents=incidents,
            drift=twin_snap, correlation_findings=findings, sensor_health=sensors or {})
    except Exception:  # noqa: BLE001
        pass
    return system_status_panel(graph=graph, open_cases=incidents, twin_snapshot=twin_snap,
                               sensors=sensors or {}, findings=findings, situation=situation)


# ══════════════════════════════════════════════════════════════════════════════
#  V67 M31 — live operator command center (composes existing panels + new sources)
# ══════════════════════════════════════════════════════════════════════════════
def collectors_panel(fabric=None) -> dict:
    """COLLECTORS panel from the V67 collector fabric (already bounded/redacted).
    Read-only; reflects lifecycle/health, never a command line or secret."""
    try:
        if fabric is None:
            from core.collector_fabric import fabric as _fab
            fabric = _fab
        panel = fabric.aura_panel()
    except Exception:  # noqa: BLE001 — an absent/half-wired fabric never breaks AURA
        return {"panel": "collectors", "metrics": {}, "collectors": []}
    panel["panel"] = "collectors"
    panel["collectors"] = panel.get("collectors", [])[:_MAX_LIST]
    return panel


def environments_panel(registry=None) -> dict:
    """ENVIRONMENTS panel from the V67 enrollment registry. NEVER emits the
    credentials reference — only whether credentials are configured."""
    try:
        if registry is None:
            from core.environment_registry import env_registry as registry
        panel = registry.to_aura_panel()
    except Exception:  # noqa: BLE001
        return {"panel": "environments", "total": 0, "authorized": 0, "environments": []}
    rows = panel.get("environments", [])[:_MAX_LIST]
    # defence in depth: strip any credentials_ref that slipped through a projection
    for row in rows:
        row.pop("credentials_ref", None)
    return {"panel": "environments", "total": panel.get("total", 0),
            "authorized": panel.get("authorized", 0), "environments": rows}


def model_runtime_panel(settings=None) -> dict:
    """MODEL / RUNTIME panel: the resolved concrete model per cognitive role. Pure
    config view — does NOT query Ollama (no I/O, never blocks the event loop)."""
    try:
        if settings is None:
            from core.config import settings as settings
        roles = settings.resolved_role_models()   # installed=None → no Ollama query
        host = _redact(getattr(settings, "ollama_host", "") or getattr(settings, "ollama_url", ""))
    except Exception:  # noqa: BLE001
        return {"panel": "model_runtime", "roles": {}, "host": "", "probe": "not_checked"}
    return {"panel": "model_runtime",
            "roles": {r: _redact(m) for r, m in list(roles.items())[:_MAX_LIST]},
            "host": host, "probe": "not_checked"}   # reachability is an M34 concern


def operational_digest(situation) -> dict:
    """The operator's five questions, derived ONLY from the situation snapshot:
    WHAT IS HAPPENING / WHAT CHANGED / WHAT MATTERS / WHAT IS UNCERTAIN / WHAT
    SHOULD I DO NEXT. Empty state is honest — 'no evidence of an active incident',
    never 'everything is secure' (unknown != safe)."""
    d = _as_dict(situation)
    summary = d.get("summary", {}) or {}
    priorities = d.get("priorities", []) or []
    changed = d.get("what_changed", {}) or {}
    uncertainties = [_redact(u) for u in d.get("uncertainties", [])[:_MAX_LIST]]
    recs = d.get("recommendations", []) or []
    severity = d.get("severity", "calm")
    open_inc = summary.get("open_incidents", 0)
    top = summary.get("top_priority")

    if not priorities and not open_inc:
        happening = f"Situation {severity.upper()}: no active operational priorities."
        do_next = ("Monitor. I do not have evidence of an active incident "
                   "(absence of evidence is not proof of safety).")
    else:
        happening = (f"Situation {severity.upper()}: {open_inc} open incident(s), "
                     f"{len(priorities)} active priorit{'y' if len(priorities) == 1 else 'ies'}.")
        do_next = (f"{recs[0].get('runbook', 'monitor')} ({recs[0].get('mode', 'dry_run')}) — "
                   f"{_redact(recs[0].get('rationale', ''))}") if recs else "Monitor."

    return {
        "happening": happening,
        "changed": {"new": list(changed.get("new", []))[:_MAX_LIST],
                    "resolved": list(changed.get("resolved", []))[:_MAX_LIST],
                    "baseline": bool(changed.get("baseline", False))},
        "matters": (_scrub(top) if top else None),
        "uncertain": uncertainties,
        "do_next": do_next,
    }


def command_center(*, graph=None, open_cases=None, twin_snapshot=None, sensors=None,
                   findings=None, situation=None, fabric=None, environments=None,
                   settings=None, runbook_engine=None) -> dict:
    """The unified live operator command center. Composes the EXISTING bounded panels
    with the V67 collector/environment/model sources plus the five-question digest.
    Pure function of injected objects (or live singletons) — testable without a HUD."""
    return {
        "panel": "command_center",
        "situation": situation_panel(situation) if situation is not None else {},
        "digest": operational_digest(situation) if situation is not None else {},
        "panels": {
            "system_status": system_status_panel(
                graph=graph, open_cases=open_cases, twin_snapshot=twin_snapshot,
                sensors=sensors or {}, findings=findings, situation=situation),
            "assets": asset_status_panel(graph) if graph is not None else {},
            "incidents": incidents_panel(open_cases or []),
            "drift": drift_panel(twin_snapshot) if twin_snapshot is not None else {"count": 0},
            "sensors": sensor_health_panel(sensors or {}),
            "correlations": correlations_panel(findings or []),
            "runbooks": runbooks_panel(runbook_engine) if runbook_engine is not None
            else {"panel": "runbooks", "available": [], "count": 0},
            "collectors": collectors_panel(fabric),
            "environments": environments_panel(environments),
            "model_runtime": model_runtime_panel(settings),
        },
    }


def build_live_command_center(*, sensors: dict | None = None) -> dict:
    """Assemble the command center from the live singletons. Read-only; bounded; no
    I/O beyond the pure in-memory spine — safe to call while a DEEP inference runs."""
    graph = incidents = twin_snap = findings = situation = runbook_engine = None
    try:
        from core.asset_graph import graph as graph
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.incident_workspace import workspace
        incidents = workspace.open_cases()
    except Exception:  # noqa: BLE001
        incidents = []
    try:
        from core.digital_twin import twin
        twin_snap = twin.compute_drift()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.correlation_v2 import correlator_v2
        findings = correlator_v2.recent(_MAX_LIST)
    except Exception:  # noqa: BLE001
        findings = []
    try:
        from core.runbook_engine import engine as runbook_engine
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.situation_engine import engine as _sit
        situation = _sit.build(asset_graph=graph, incidents=incidents, drift=twin_snap,
                               correlation_findings=findings, sensor_health=sensors or {})
    except Exception:  # noqa: BLE001
        pass
    return command_center(graph=graph, open_cases=incidents, twin_snapshot=twin_snap,
                          sensors=sensors or {}, findings=findings, situation=situation,
                          runbook_engine=runbook_engine)
