"""
tests/test_bind_and_dedup_v681.py — V68.1 M50 bind hygiene + finding dedup.

Locks: deception services default to localhost (external exposure is explicit
and auditable), and repeated identical security findings are deduplicated with
operator classification — no re-warning the same UDP/41641/tailscaled every few
minutes.
"""
from __future__ import annotations

import core.canary as canary
import core.security_auditor as sa


# ── Canary bind scope ─────────────────────────────────────────────────────────

def test_canary_default_bind_is_localhost(monkeypatch):
    monkeypatch.delenv(canary._EXPOSE_ENV, raising=False)
    monkeypatch.delenv(canary._BIND_ENV, raising=False)
    assert canary._canary_bind_host() == "127.0.0.1"


def test_canary_exposure_requires_explicit_optin(monkeypatch):
    monkeypatch.setenv(canary._EXPOSE_ENV, "1")
    monkeypatch.delenv(canary._BIND_ENV, raising=False)
    assert canary._canary_bind_host() == "0.0.0.0"


def test_canary_explicit_bind_address_honored(monkeypatch):
    monkeypatch.setenv(canary._EXPOSE_ENV, "true")
    monkeypatch.setenv(canary._BIND_ENV, "10.10.0.5")
    assert canary._canary_bind_host() == "10.10.0.5"


def test_canary_non_truthy_expose_stays_local(monkeypatch):
    monkeypatch.setenv(canary._EXPOSE_ENV, "0")
    assert canary._canary_bind_host() == "127.0.0.1"


# ── Security-finding deduplication ────────────────────────────────────────────

def _reset():
    sa._finding_state.clear()
    sa._finding_class.clear()


def test_repeated_finding_is_deduplicated():
    _reset()
    t0 = 1000.0
    first = sa._register_finding("UDP", 41641, "tailscaled.exe", t0)
    assert first["should_log"] is True and first["count"] == 1

    # Subsequent scans within the suppression window: counted, not re-logged.
    for i in range(1, 6):
        r = sa._register_finding("UDP", 41641, "tailscaled.exe", t0 + i * 600)
        assert r["should_log"] is False
        assert r["count"] == i + 1


def test_finding_resurfaces_after_suppression_window():
    _reset()
    t0 = 5000.0
    sa._register_finding("UDP", 41641, "tailscaled.exe", t0)
    later = t0 + sa._SUPPRESSION_WINDOW_S + 1
    r = sa._register_finding("UDP", 41641, "tailscaled.exe", later)
    assert r["should_log"] is True  # re-surfaced with a rollup count
    assert r["count"] == 2


def test_expected_classification_silences_finding():
    _reset()
    sa.classify_finding(41641, "tailscaled.exe", sa.EXPECTED, proto="UDP")
    r = sa._register_finding("UDP", 41641, "tailscaled.exe", 1.0)
    assert r["should_log"] is False
    assert r["suppressed_by"] == sa.EXPECTED


def test_suppress_until_expires():
    _reset()
    sa.classify_finding(41641, "tailscaled.exe", sa.SUPPRESS_UNTIL, proto="UDP", until=100.0)
    # Before expiry → silenced.
    r1 = sa._register_finding("UDP", 41641, "tailscaled.exe", 50.0)
    assert r1["should_log"] is False
    # After expiry → the finding surfaces again (first log since state reset).
    r2 = sa._register_finding("UDP", 41641, "tailscaled.exe", 200.0)
    assert r2["should_log"] is True


def test_classification_not_inferred_from_process_name():
    _reset()
    # No classification set → the finding is NOT auto-trusted just for its name.
    assert sa._classification_of(sa._finding_key("UDP", 41641, "tailscaled.exe"), 1.0) is None


def test_distinct_ports_tracked_separately():
    _reset()
    a = sa._register_finding("UDP", 41641, "tailscaled.exe", 1.0)
    b = sa._register_finding("TCP", 8080, "mystery.exe", 1.0)
    assert a["should_log"] and b["should_log"]
    assert sa.finding_dedup_summary()["tracked"] == 2


def test_dedup_summary_shape():
    _reset()
    sa._register_finding("UDP", 41641, "tailscaled.exe", 1.0)
    summ = sa.finding_dedup_summary()
    assert summ["tracked"] == 1
    assert summ["findings"][0]["port"] == 41641
