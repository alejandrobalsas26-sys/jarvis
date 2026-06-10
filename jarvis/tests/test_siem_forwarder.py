"""tests/test_siem_forwarder.py — JARVIS V55.0 TITAN SIEM forwarder tests."""
from __future__ import annotations

import asyncio
import time

import pytest

from core.siem_forwarder import SIEMForwarder


_SAMPLE = {
    "type":             "lateral_movement_chain",
    "rule":             "lateral_movement_chain",
    "incident_id":      "ABC12345",
    "severity_score":   8.5,
    "kill_chain_phase": "Defense Evasion",
    "mitre_techniques": ["T1055.012"],
    "involved_hosts":   ["192.168.1.105"],
    "src_ip":           "192.168.1.105",
    "process":          "svchost.exe",
    "pid":              1234,
    "file_path":        r"C:\evil.exe",
    "ts":               time.time(),
}


def test_map_to_ecs_required_fields():
    fwd = SIEMForwarder(endpoint="http://test/siem")
    ecs = fwd.map_to_ecs(_SAMPLE)

    assert "@timestamp" in ecs
    assert ecs["event"]["action"]  == "lateral_movement_chain"
    assert ecs["event"]["dataset"] == "jarvis.alerts"
    assert ecs["rule"]["name"]     == "lateral_movement_chain"
    assert ecs["rule"]["id"]       == "ABC12345"
    assert ecs["host"]["name"]  # populated from socket.gethostname()


def test_map_to_ecs_optional_fields():
    fwd = SIEMForwarder(endpoint="http://test/siem")
    ecs = fwd.map_to_ecs(_SAMPLE)

    assert ecs["source"]["ip"]     == "192.168.1.105"
    assert ecs["process"]["name"]  == "svchost.exe"
    assert ecs["process"]["pid"]   == 1234
    assert ecs["file"]["path"]     == r"C:\evil.exe"
    assert ecs["threat"]["technique"]["name"] == "T1055.012"


def test_noop_mode_without_endpoint():
    fwd = SIEMForwarder(endpoint="")
    fwd.enqueue(_SAMPLE)
    assert fwd._queue.empty(), "noop mode must not enqueue events"


def test_noop_start_does_not_spawn_task():
    fwd = SIEMForwarder(endpoint="")
    asyncio.run(fwd.start())
    assert fwd._flush_task is None


def test_enqueue_batches_events():
    fwd = SIEMForwarder(endpoint="http://test/siem", batch_size=10)
    for i in range(5):
        fwd.enqueue({**_SAMPLE, "incident_id": f"INC{i:02d}"})
    assert fwd._queue.qsize() == 5


def test_flush_sends_batch():
    fwd = SIEMForwarder(endpoint="http://test/siem", batch_size=10)
    fwd.enqueue(_SAMPLE)

    sent: list[list] = []

    async def _mock_send(batch):
        sent.append(batch)

    async def drive():
        fwd._send = _mock_send
        await fwd.flush()

    asyncio.run(drive())
    assert len(sent) == 1
    assert len(sent[0]) == 1
    assert sent[0][0]["rule"]["name"] == "lateral_movement_chain"


def test_queue_empty_after_flush():
    fwd = SIEMForwarder(endpoint="http://test/siem", batch_size=10)
    fwd.enqueue(_SAMPLE)

    async def drive():
        fwd._send = lambda b: asyncio.sleep(0)
        await fwd.flush()

    asyncio.run(drive())
    assert fwd._queue.empty()


def test_http_failure_does_not_raise():
    """SIEM endpoint unreachable must log and swallow — never crash the caller."""
    fwd = SIEMForwarder(endpoint="http://127.0.0.1:19999/no-siem", batch_size=10)
    fwd.enqueue(_SAMPLE)

    async def drive():
        await fwd.flush()

    try:
        asyncio.run(drive())
    except Exception as exc:
        pytest.fail(f"flush() raised unexpectedly: {exc}")


def test_load_tactic_audit_missing_file():
    fwd = SIEMForwarder(endpoint="")
    events = fwd.load_tactic_audit("/nonexistent/path/tactic_audit.jsonl")
    assert events == []


def test_load_tactic_audit_parses_jsonl(tmp_path):
    import json
    audit = tmp_path / "tactic_audit.jsonl"
    audit.write_text(
        json.dumps({"type": "test", "severity": 5}) + "\n"
        + json.dumps({"type": "test2", "severity": 7}) + "\n",
        encoding="utf-8",
    )
    fwd = SIEMForwarder(endpoint="")
    events = fwd.load_tactic_audit(audit)
    assert len(events) == 2
    assert events[0]["type"] == "test"
