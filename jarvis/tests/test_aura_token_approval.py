"""
tests/test_aura_token_approval.py — Phase 2B: AURA /ws token auth + F1b.

Covers:
  - /ws rejects missing / invalid token; accepts valid token + allowed Origin
  - Origin check still wins (valid token + bad Origin -> rejected)
  - the token is never leaked in the connect banner
  - high-risk HUD commands are approved OUT OF BAND (executor._challenge),
    never over the initiating WebSocket, and refuse if no channel exists
  - low-risk connect path still works with a valid token
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from aura import server


class _FakeWS:
    def __init__(self, origin="http://127.0.0.1:8765", token="__valid__",
                 cookie=True, incoming=None):
        self.headers = {"origin": origin} if origin is not None else {}
        tok = server._AURA_WS_TOKEN if token == "__valid__" else token
        self.cookies = {"aura_token": tok} if (cookie and tok is not None) else {}
        self.query_params = {} if (cookie or tok is None) else {"token": tok}
        self.accepted = False
        self.closed_code = None
        self.sent: list = []
        self._incoming = list(incoming or [])

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000):
        self.closed_code = code

    async def send_text(self, data):
        self.sent.append(data)

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_text(self):
        from fastapi import WebSocketDisconnect
        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect(1000)


def _run_endpoint(ws):
    asyncio.run(server._ws_endpoint(ws))


# ─────────────────────────────── token gate ─────────────────────────────────

def test_rejects_missing_token():
    ws = _FakeWS(token=None)                 # no cookie, no query token
    _run_endpoint(ws)
    assert ws.accepted is False
    assert ws.closed_code == 1008


def test_rejects_invalid_token():
    ws = _FakeWS(token="wrong-token")
    _run_endpoint(ws)
    assert ws.accepted is False
    assert ws.closed_code == 1008


def test_accepts_valid_token_and_origin():
    ws = _FakeWS(token="__valid__")
    _run_endpoint(ws)
    assert ws.accepted is True
    assert ws.closed_code is None


def test_rejects_valid_token_bad_origin():
    ws = _FakeWS(origin="http://evil.example", token="__valid__")
    _run_endpoint(ws)
    assert ws.accepted is False
    assert ws.closed_code == 1008            # Origin check runs first


def test_token_not_leaked_in_banner():
    ws = _FakeWS(token="__valid__")
    _run_endpoint(ws)
    joined = " ".join(str(s) for s in ws.sent)
    assert server._AURA_WS_TOKEN not in joined


def test_query_param_token_accepted():
    ws = _FakeWS(token="__valid__", cookie=False)   # token via ?token=
    _run_endpoint(ws)
    assert ws.accepted is True


# ─────────────────────── high-risk out-of-band approval (F1b) ───────────────

class _FakeExecutor:
    def __init__(self, grant):
        self._grant = grant
        self.challenged: list = []

    async def _challenge(self, tool_name, preview):
        self.challenged.append(tool_name)
        return (self._grant, "test")


def _run_hud(cmd, executor):
    ws = _FakeWS(token="__valid__")
    raw = {"cmd": cmd, "args": {}, "request_id": "r1"}
    asyncio.run(server._handle_hud_command(raw, ws, executor, server.broadcast))
    return ws


def _types(ws):
    return [s.get("type") for s in ws.sent if isinstance(s, dict)]


def test_high_risk_uses_out_of_band_challenge_not_ws():
    ex = _FakeExecutor(grant=False)
    ws = _run_hud("sliver_interact", ex)
    # It challenged the operator out-of-band …
    assert ex.challenged == ["hud:sliver_interact"]
    # … and never issued an OTP the SAME socket could answer.
    assert "hud_otp_challenge" not in _types(ws)
    # denied → error, no dispatch
    assert "hud_command_error" in _types(ws)


def test_high_risk_without_channel_requires_out_of_band():
    ws = _run_hud("sliver_interact", object())      # executor with no _challenge
    assert "hud_approval_required_out_of_band" in _types(ws)
    assert "hud_otp_challenge" not in _types(ws)
