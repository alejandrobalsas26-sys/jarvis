"""
tests/test_incident_workspace.py — V66 M22 incident case workspace.

Covers the required M22 surface: CompoundIncident adapter, incident creation,
timeline append, evidence provenance, hypothesis lifecycle, IOC linkage,
containment-proposal-does-not-execute, explicit status transitions, reporter +
intel_fusion compatibility, and persistence round-trip.
"""
from __future__ import annotations

import asyncio

import pytest

from core.incident_workspace import (
    HypothesisStatus,
    IncidentCase,
    IncidentSeverity,
    IncidentStatus,
    IncidentWorkspace,
    ProposalStatus,
    incident_finding_sink,
    workspace,
)

T0 = "2026-07-07T12:00:00+00:00"


def _finding_dict(group="host:win-client-01", rule="suspicious_process_then_network"):
    return {
        "finding_id": f"cf_{rule}", "rule": rule,
        "matched_event_ids": ["oe_1", "oe_2"], "group_entity": group,
        "entities": ["host:win-client-01", "ip:10.0.0.9"],
        "asset_refs": ["physical_host:win-client-01", "unknown:10.0.0.9"],
        "confidence": 0.72, "severity": "high",
        "mitre_techniques": ["T1059", "T1071"],
        "explanation": {"rule": rule, "summary": "proc then net",
                        "matched_steps": ["process_activity", "network_activity"],
                        "reason": "grouped 2 events by host"},
        "evidence": [], "window_start": T0, "window_end": T0, "created_at": T0,
    }


# ── incident creation from a finding ──────────────────────────────────────────
def test_ingest_finding_creates_case():
    ws = IncidentWorkspace()
    case = ws.ingest_finding(_finding_dict(), now_iso=T0)
    assert case.status is IncidentStatus.NEW
    assert case.severity is IncidentSeverity.HIGH
    assert "cf_suspicious_process_then_network" in case.correlation_findings
    assert "physical_host:win-client-01" in case.affected_assets
    assert set(case.mitre_techniques) == {"T1059", "T1071"}
    # IOC linkage from entities
    assert any(i.type == "ip" and i.value == "10.0.0.9" for i in case.iocs)
    # timeline recorded the open + the finding evidence
    kinds = {e.kind for e in case.timeline.entries}
    assert "open" in kinds and "evidence" in kinds


def test_second_finding_appends_to_open_case():
    ws = IncidentWorkspace()
    c1 = ws.ingest_finding(_finding_dict(), now_iso=T0)
    c2 = ws.ingest_finding(_finding_dict(rule="high_severity_sequence"), now_iso=T0)
    assert c1.incident_id == c2.incident_id           # same group → same open case
    assert len(c1.correlation_findings) == 2


def test_new_case_after_close():
    ws = IncidentWorkspace()
    c1 = ws.ingest_finding(_finding_dict(), now_iso=T0)
    c1.transition(IncidentStatus.CLOSED)
    c2 = ws.ingest_finding(_finding_dict(), now_iso=T0)
    assert c1.incident_id != c2.incident_id           # closed case is not reused


# ── timeline + evidence provenance ────────────────────────────────────────────
def test_evidence_provenance_preserved():
    case = IncidentCase(incident_id="inc1")
    item = case.add_evidence("event", "sysmon spawned powershell",
                             source="sysmon", trusted=True,
                             event_refs=["oe_9"], provenance={"pid": 4321})
    assert item.provenance["pid"] == 4321
    assert item.event_refs == ["oe_9"]
    assert item.source == "sysmon"
    assert case.timeline.entries[-1].kind == "evidence"


# ── hypothesis lifecycle ──────────────────────────────────────────────────────
def test_hypothesis_lifecycle():
    case = IncidentCase(incident_id="inc1")
    h = case.add_hypothesis("attacker used a phishing payload", confidence=0.4)
    assert h.status is HypothesisStatus.OPEN
    assert case.update_hypothesis(h.id, HypothesisStatus.SUPPORTED, confidence=0.8)
    assert case.hypotheses[0].status is HypothesisStatus.SUPPORTED
    assert case.hypotheses[0].confidence == 0.8
    assert case.update_hypothesis("nope", HypothesisStatus.REFUTED) is False


def test_investigation_question_lifecycle():
    case = IncidentCase(incident_id="inc1")
    q = case.add_question("was the outbound connection to a known C2?")
    assert case.answer_question(q.id, "no, benign update server")
    assert case.open_questions[0].answer.startswith("no")


# ── explicit status transitions ───────────────────────────────────────────────
def test_valid_status_transition():
    case = IncidentCase(incident_id="inc1")
    case.transition(IncidentStatus.TRIAGE)
    case.transition(IncidentStatus.INVESTIGATING)
    assert case.status is IncidentStatus.INVESTIGATING
    assert case.timeline.entries[-1].kind == "status"


def test_illegal_status_transition_rejected():
    case = IncidentCase(incident_id="inc1")
    # NEW → CONTAINED is not allowed
    with pytest.raises(ValueError):
        case.transition(IncidentStatus.CONTAINED)


# ── containment proposal does NOT execute ─────────────────────────────────────
def test_containment_proposal_does_not_execute():
    calls: list = []

    class _Executor:
        authority = None

        async def aexecute(self, tool, args, reasoning=""):
            calls.append((tool, args))
            return {"ok": True}

    case = IncidentCase(incident_id="inc1")
    p = case.propose_containment("kill_process", {"pid": 1337},
                                 target="win-client-01", rationale="stop the beacon")
    # merely proposing performs NO execution
    assert calls == []
    assert p.status is ProposalStatus.PROPOSED
    assert p.requires_hitl is True          # kill_process is HIGH_IMPACT
    assert p.risk_class == "high_impact"
    # the timeline explicitly records it was NOT executed
    assert any("NOT executed" in e.message for e in case.timeline.entries)


def test_execute_proposal_is_the_only_execution_path():
    calls: list = []

    class _Executor:
        authority = None

        async def aexecute(self, tool, args, reasoning=""):
            calls.append((tool, args))
            return {"status": "killed", "pid": 1337}

    ws = IncidentWorkspace()
    case = ws.add_case(IncidentCase(incident_id="inc1", status=IncidentStatus.INVESTIGATING))
    p = case.propose_containment("kill_process", {"pid": 1337})

    async def verify(case_, prop, result):
        return True, "process no longer present", 0.9

    async def drive():
        return await ws.execute_proposal(case, p, _Executor(), verify_fn=verify)

    action = asyncio.run(drive())
    assert calls == [("kill_process", {"pid": 1337})]     # executed exactly once
    assert action.status == "completed"
    assert p.status is ProposalStatus.EXECUTED
    # verification recorded and case advanced to CONTAINED
    assert case.verification_results and case.verification_results[0].verified
    assert case.status is IncidentStatus.CONTAINED


def test_execute_proposal_blocked_by_authority():
    class _Deny:
        allowed = False
        reason = "target out of scope"

    class _Executor:
        authority = object()

        async def aexecute(self, tool, args, reasoning=""):
            raise AssertionError("must not execute when authority denies")

    import core.incident_workspace as iw
    ws = IncidentWorkspace()
    case = ws.add_case(IncidentCase(incident_id="inc1", status=IncidentStatus.INVESTIGATING))
    p = case.propose_containment("network_scan", {"target": "8.8.8.8"})

    def fake_authorize(state, tool, args):
        return _Deny()

    orig = iw.__dict__.get("authorize_action")
    # patch the lazily-imported symbol via monkeypatching core.authority
    import core.authority as authmod
    authmod.authorize_action = fake_authorize  # type: ignore
    try:
        action = asyncio.run(ws.execute_proposal(case, p, _Executor()))
    finally:
        if orig is not None:
            authmod.authorize_action = orig  # type: ignore
    assert action.status == "blocked"
    assert p.status is ProposalStatus.REJECTED


# ── CompoundIncident adapter ──────────────────────────────────────────────────
def test_compound_incident_adapter():
    compound = {
        "incident_id": "AB12CD34", "rule": "c2_beacon_detected",
        "severity_score": 8.5, "kill_chain_phase": "Command & Control",
        "mitre_techniques": ["T1071", "T1071.004"],
        "involved_hosts": ["192.168.56.40"], "involved_pids": [1337],
    }
    case = IncidentCase.from_compound_incident(compound, now_iso=T0)
    assert case.incident_id == "AB12CD34"
    assert case.severity is IncidentSeverity.CRITICAL
    assert case.provenance["kill_chain_phase"] == "Command & Control"
    assert any(i.value == "192.168.56.40" for i in case.iocs)
    assert case.timeline.entries[0].kind == "ingest"


# ── reporter + intel_fusion compatibility ─────────────────────────────────────
def test_reporter_input_shape():
    case = IncidentCase(incident_id="inc1", mitre_techniques=["T1059"],
                        affected_assets=["physical_host:win-client-01"])
    case.provenance["kill_chain_phase"] = "Execution"
    case.add_evidence("event", "powershell spawned", provenance={"pid": 10})
    r = case.to_reporter_input()
    assert r["incident_id"] == "inc1"
    assert r["mitre_techniques"] == ["T1059"]
    assert r["kill_chain_phase"] == "Execution"
    assert isinstance(r["sub_events"], list)


def test_intel_fusion_shape_and_sync():
    ingested: list = []
    iocs: list = []

    async def fake_incident(d):
        ingested.append(d)

    async def fake_ioc(t, v, score, ctx):
        iocs.append((t, v))

    ws = IncidentWorkspace()
    case = ws.add_case(IncidentCase(incident_id="inc1",
                                    severity=IncidentSeverity.HIGH,
                                    affected_assets=["unknown:192.168.56.40"]))
    case.add_ioc("ip", "192.168.56.40", source="correlation")
    shape = case.to_intel_fusion_incident()
    assert shape["incident_id"] == "inc1"
    assert shape["involved_hosts"] == ["192.168.56.40"]   # asset_id prefix stripped
    asyncio.run(ws.sync_to_intel_fusion(case, ingest_incident_fn=fake_incident,
                                        ingest_ioc_fn=fake_ioc))
    assert ingested and ingested[0]["incident_id"] == "inc1"
    assert ("ip", "192.168.56.40") in iocs


# ── persistence round-trip ────────────────────────────────────────────────────
def test_persistence_roundtrip(tmp_path):
    ws = IncidentWorkspace()
    case = ws.ingest_finding(_finding_dict(), now_iso=T0)
    case.transition(IncidentStatus.TRIAGE)
    case.add_hypothesis("lateral movement", confidence=0.6)
    case.propose_containment("kill_process", {"pid": 1})
    path = tmp_path / "incidents.json"
    ws.save(path)
    ws2 = IncidentWorkspace.load(path)
    c2 = ws2.get(case.incident_id)
    assert c2 is not None
    assert c2.status is IncidentStatus.TRIAGE
    assert len(c2.hypotheses) == 1
    assert len(c2.proposed_actions) == 1
    assert set(c2.mitre_techniques) == {"T1059", "T1071"}
    # open-case group index rebuilt so new findings still append
    c3 = ws2.ingest_finding(_finding_dict(), now_iso=T0)
    assert c3.incident_id == case.incident_id


# ── module sink (used by correlator_v2) ───────────────────────────────────────
def test_module_finding_sink():
    before = len(workspace.cases)

    class _F:
        def to_dict(self):
            return _finding_dict(group="host:sink-test-host")

    asyncio.run(incident_finding_sink(_F()))
    assert len(workspace.cases) == before + 1
