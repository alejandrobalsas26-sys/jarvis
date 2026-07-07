"""
tests/test_correlation_v2.py — V66 M21 evidence-linked correlator.

Covers the required M21 surface: legacy dict compatibility, canonical event
ingest, bounded window, auth-failures→success, process→network sequence,
cross-source evidence, dedup, explanation, and asset links.
"""
from __future__ import annotations

import asyncio

from core.asset_graph import AssetGraph, AssetType
from core.correlation_v2 import CorrelatorV2, DEFAULT_RULES
from core.ops_events import normalize_event


def _canon(payload, ts):
    res = normalize_event(payload, now_iso=ts)
    assert res.ok, payload
    return res.event


def _ts(sec: int) -> str:
    return f"2026-07-07T12:00:{sec:02d}+00:00"


def _engine(**kw) -> CorrelatorV2:
    return CorrelatorV2(**kw)


# ── process → network sequence ────────────────────────────────────────────────
def test_process_then_network_sequence():
    eng = _engine()
    p1 = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "win-client-01",
                 "process": "powershell.exe", "pid": 10}, _ts(0))
    p2 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "win-client-01",
                 "target_ip": "10.0.0.9", "pid": 10}, _ts(5))
    assert eng.ingest_event(p1) == []
    findings = eng.ingest_event(p2)
    rules = {f.rule for f in findings}
    assert "suspicious_process_then_network" in rules
    f = next(f for f in findings if f.rule == "suspicious_process_then_network")
    assert len(f.matched_event_ids) == 2
    assert f.group_entity == "host:win-client-01"
    assert f.explanation.reason
    assert f.evidence and all(e.event_id for e in f.evidence)


# ── auth failures → success ───────────────────────────────────────────────────
def test_auth_failures_then_success():
    eng = _engine()
    # three auth failures then a success, same host (via internal auth telemetry)
    for i in range(3):
        eng.ingest_event(_canon({
            "type": "auth_event", "host": "win-client-01", "user": "admin",
            "technique": "logon failed 4625", "severity": "MEDIUM",
        }, _ts(i)))
    findings = eng.ingest_event(_canon({
        "type": "auth_event", "host": "win-client-01", "user": "admin",
        "technique": "logon success 4624", "severity": "MEDIUM",
    }, _ts(4)))
    # internal adapter maps these to category SYSTEM, not AUTH, so tune: assert the
    # rule fires only when category is AUTH. Use canonical events with AUTH category.
    # (auth_event is an internal type → SYSTEM). This asserts the rule does NOT
    # false-fire on non-auth categories.
    assert all(f.rule != "auth_failures_then_success" for f in findings)


def test_auth_rule_fires_on_auth_category():
    from core.ops_events import (
        EntityReference,
        EntityType,
        EventCategory,
        EventProvenance,
        EventSeverity,
        EventSource,
        OperationalEvent,
    )

    def auth_ev(seq, sig, sev="medium"):
        ev = OperationalEvent(
            event_id=f"oe_auth{seq}",
            provenance=EventProvenance(EventSource.JARVIS_INTERNAL),
            source=EventSource.JARVIS_INTERNAL, category=EventCategory.AUTH,
            severity=EventSeverity(sev), timestamp=_ts(seq),
            observed_at=_ts(seq), signature=sig,
            entities=(EntityReference(EntityType.HOST, "win-client-01"),),
        )
        return ev

    eng = _engine()
    for i in range(3):
        eng.ingest_event(auth_ev(i, "logon failed 4625"))
    findings = eng.ingest_event(auth_ev(4, "logon success 4624"))
    assert any(f.rule == "auth_failures_then_success" for f in findings)
    f = next(f for f in findings if f.rule == "auth_failures_then_success")
    assert "T1110" in f.mitre_techniques


# ── cross-source evidence (IDS/anomaly + activity on same IP) ──────────────────
def test_cross_source_same_ip():
    eng = _engine()
    # A network/IDS anomaly on an IP, plus host network activity to that IP.
    a = _canon({"type": "network_anomaly", "detector": "beaconing", "src_ip": "192.168.56.40",
                "description": "beaconing", "severity": "HIGH"}, _ts(0))
    b = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "win-client-01",
                "target_ip": "192.168.56.40", "pid": 22}, _ts(3))
    eng.ingest_event(a)
    findings = eng.ingest_event(b)
    f = next((f for f in findings if f.rule == "ids_alert_with_host_activity"), None)
    assert f is not None
    # cross-source: evidence spans two distinct producers
    sources = {e.source for e in f.evidence}
    assert "network_baseline" in sources and "sysmon" in sources


# ── same IOC across multiple assets ───────────────────────────────────────────
def test_same_ioc_multiple_assets():
    from core.ops_events import (
        EntityReference,
        EntityType,
        EventCategory,
        EventProvenance,
        EventSeverity,
        EventSource,
        OperationalEvent,
    )

    def ev(seq, host):
        return OperationalEvent(
            event_id=f"oe_ioc{seq}",
            provenance=EventProvenance(EventSource.JARVIS_INTERNAL),
            source=EventSource.JARVIS_INTERNAL, category=EventCategory.NETWORK,
            severity=EventSeverity.MEDIUM, timestamp=_ts(seq), observed_at=_ts(seq),
            entities=(EntityReference(EntityType.IP, "45.0.0.6"),
                      EntityReference(EntityType.HOST, host)),
        )
    eng = _engine()
    eng.ingest_event(ev(0, "host-a"))
    findings = eng.ingest_event(ev(1, "host-b"))
    assert any(f.rule == "same_ioc_multiple_assets" for f in findings)


# ── high severity sequence ────────────────────────────────────────────────────
def test_high_severity_sequence():
    eng = _engine()
    ev = lambda s: _canon({"type": "network_anomaly", "detector": "beaconing",  # noqa: E731
                           "src_ip": "10.1.1.1", "description": "x", "severity": "HIGH"}, _ts(s))
    eng.ingest_event(ev(0))
    eng.ingest_event(ev(1))
    # third HIGH within window on same IP → fires (min_count 3)
    findings = eng.ingest_event(_canon({"type": "dpi_alert", "protocol": "HTTP",
                                        "src_ip": "10.1.1.1", "technique": "t",
                                        "detail": "y", "severity": "HIGH"}, _ts(2)))
    assert any(f.rule == "high_severity_sequence" for f in findings)


# ── bounded window ────────────────────────────────────────────────────────────
def test_bounded_window_no_stale_match():
    eng = _engine()
    p1 = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "h",
                 "process": "a.exe", "pid": 1}, _ts(0))
    eng.ingest_event(p1)
    # a network event 200s later — outside the 60s process→network window
    p2 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "h",
                 "target_ip": "1.2.3.4", "pid": 1}, "2026-07-07T12:03:20+00:00")
    findings = eng.ingest_event(p2)
    assert all(f.rule != "suspicious_process_then_network" for f in findings)


# ── dedup ─────────────────────────────────────────────────────────────────────
def test_dedup_same_burst():
    eng = _engine()
    p1 = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "h",
                 "process": "a.exe", "pid": 1}, _ts(0))
    p2 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "h",
                 "target_ip": "1.2.3.4", "pid": 1}, _ts(2))
    eng.ingest_event(p1)
    first = eng.ingest_event(p2)
    assert any(f.rule == "suspicious_process_then_network" for f in first)
    # another network event immediately after — same (rule,host) burst, deduped
    p3 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "h",
                 "target_ip": "5.6.7.8", "pid": 1}, _ts(3))
    second = eng.ingest_event(p3)
    assert all(f.rule != "suspicious_process_then_network" for f in second)


# ── asset links ───────────────────────────────────────────────────────────────
def test_findings_link_entities_to_asset_graph():
    g = AssetGraph()
    eng = _engine(asset_graph=g)
    p1 = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "win-client-01",
                 "process": "powershell.exe", "pid": 10}, _ts(0))
    p2 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "win-client-01",
                 "target_ip": "10.0.0.9", "pid": 10}, _ts(2))

    async def drive():
        await eng.ingest(p1)
        await eng.ingest(p2)

    asyncio.run(drive())
    host = g.get(AssetType.PHYSICAL_HOST, "win-client-01")
    assert host is not None
    assert host.current("seen_in_correlation") is not None
    assert host.history("seen_in_correlation")[0].source.value == "canonical_event"


# ── legacy dict compatibility (feeds wrapped TemporalCorrelator) ──────────────
def test_legacy_dict_feeds_wrapped_correlator():
    fed: list[dict] = []

    class _FakeLegacy:
        async def ingest(self, ev):
            fed.append(ev)

    eng = _engine(legacy=_FakeLegacy())
    legacy_event = {"type": "sysmon_event", "event_id": 1, "agent_host": "h",
                    "process": "a.exe", "pid": 1}

    async def drive():
        return await eng.ingest(legacy_event)

    findings = asyncio.run(drive())
    # legacy correlator received the original dict unchanged
    assert fed == [legacy_event]
    assert isinstance(findings, list)


def test_canonical_event_ingest_no_legacy_feed():
    fed: list[dict] = []

    class _FakeLegacy:
        async def ingest(self, ev):
            fed.append(ev)

    eng = _engine(legacy=_FakeLegacy())
    ev = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "h",
                 "process": "a.exe", "pid": 1}, _ts(0))

    asyncio.run(eng.ingest(ev))
    # a canonical event is NOT fed back to legacy (no double-ingest)
    assert fed == []


# ── feed() operational-type filter ────────────────────────────────────────────
def test_feed_ignores_non_operational_types():
    eng = _engine()

    async def drive():
        eng.feed({"type": "telemetry", "cpu_pct": 5})
        eng.feed({"type": "model_decision", "role": "fast"})
        await asyncio.sleep(0)  # let any scheduled tasks run

    asyncio.run(drive())
    # window stays empty — noise was filtered before normalization
    assert len(eng._window) == 0


def test_default_rules_bounded():
    assert all(r.window_sec <= 300.0 for r in DEFAULT_RULES)
    assert len(DEFAULT_RULES) == 7


# ── emission to sinks ─────────────────────────────────────────────────────────
def test_finding_emitted_to_sink_and_broadcast():
    got: list = []
    broadcasts: list = []
    eng = _engine()

    async def sink(f):
        got.append(f)

    async def bcast(ev):
        broadcasts.append(ev)

    eng.attach(broadcast_fn=bcast, sinks=[sink])
    p1 = _canon({"type": "sysmon_event", "event_id": 1, "agent_host": "h",
                 "process": "a.exe", "pid": 1}, _ts(0))
    p2 = _canon({"type": "sysmon_event", "event_id": 3, "agent_host": "h",
                 "target_ip": "1.2.3.4", "pid": 1}, _ts(2))

    async def drive():
        await eng.ingest(p1)
        await eng.ingest(p2)

    asyncio.run(drive())
    assert got and got[0].rule == "suspicious_process_then_network"
    assert any(b.get("type") == "correlation_finding" for b in broadcasts)
