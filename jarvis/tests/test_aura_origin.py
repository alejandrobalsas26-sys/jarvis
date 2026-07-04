"""
tests/test_aura_origin.py — Phase 1A: AURA WebSocket Origin allowlist (CSWSH defense).

Verifies the fail-closed Origin gate on the /ws handshake:
  - loopback origins (localhost / 127.0.0.0/8 / ::1) are allowed (the local HUD)
  - foreign origins are rejected
  - a missing Origin is rejected
  - malformed / non-http origins fail closed
  - operator-configured trusted origins (settings.aura_allowed_origins) are allowed

Pure unit tests — no network, no running server. The endpoint-level cases drive
_ws_endpoint with a minimal fake WebSocket, so no ASGI server / TestClient is used.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from aura import server


# ─────────────────────────────── pure predicate ─────────────────────────────

@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "https://localhost",
    "http://127.0.0.1",
    "http://127.0.0.53:9000",     # anywhere in 127.0.0.0/8
    "http://[::1]:8765",
    "http://127.0.0.1:8765/",     # trailing slash tolerated
])
def test_loopback_origins_allowed(origin):
    assert server._origin_allowed(origin) is True


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "https://attacker.example:8765",
    "http://192.168.1.50:8765",   # LAN, not loopback
    "http://10.0.0.9",
    "http://169.254.169.254",     # link-local / metadata
    "http://jarvis.local:8765",
])
def test_foreign_origins_rejected(origin):
    assert server._origin_allowed(origin) is False


@pytest.mark.parametrize("origin", [None, "", "   "])
def test_missing_origin_rejected(origin):
    assert server._origin_allowed(origin) is False


@pytest.mark.parametrize("origin", [
    "null",                       # browsers send this for opaque/file origins
    "not a url",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "http://",                    # no host
    "ws://127.0.0.1:8765",        # non-http scheme fails closed
])
def test_malformed_origins_fail_closed(origin):
    assert server._origin_allowed(origin) is False


def test_configured_trusted_origin_allowed(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "aura_allowed_origins",
                        "http://hud.lab:8765, http://ops.internal")
    assert server._origin_allowed("http://hud.lab:8765") is True
    assert server._origin_allowed("http://ops.internal") is True
    # still fails closed for anything not on the list
    assert server._origin_allowed("http://other.host:8765") is False


# ───────────────────────────── endpoint handshake gate ──────────────────────

class _FakeWS:
    """Minimal WebSocket double: records accept()/close() and yields no frames."""

    def __init__(self, origin):
        self.headers = {} if origin is None else {"origin": origin}
        # Carry a valid session token so these cases isolate the Origin check;
        # rejected-origin cases still reject at the Origin gate (before token).
        self.cookies = {"aura_token": server._AURA_WS_TOKEN}
        self.query_params: dict = {}
        self.accepted = False
        self.closed_code = None
        self.sent: list = []

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000):
        self.closed_code = code

    async def send_text(self, data):
        self.sent.append(data)

    async def receive_text(self):
        from fastapi import WebSocketDisconnect
        raise WebSocketDisconnect(1000)


def test_endpoint_rejects_foreign_origin():
    ws = _FakeWS("http://evil.example")
    asyncio.run(server._ws_endpoint(ws))
    assert ws.accepted is False
    assert ws.closed_code == 1008
    assert ws not in server.manager._clients


def test_endpoint_rejects_missing_origin():
    ws = _FakeWS(None)
    asyncio.run(server._ws_endpoint(ws))
    assert ws.accepted is False
    assert ws.closed_code == 1008
    assert ws not in server.manager._clients


def test_endpoint_accepts_loopback_origin():
    ws = _FakeWS("http://127.0.0.1:8765")
    asyncio.run(server._ws_endpoint(ws))
    # Accepted, never policy-closed, and pruned from the client set on disconnect.
    assert ws.accepted is True
    assert ws.closed_code is None
    assert ws not in server.manager._clients
