"""
tools/offensive_rpc.py — Metasploit RPC async bridge (v24.0).

Architecture:
- pymetasploit3 is synchronous → all RPC calls run in run_in_executor(None, ...)
- Connection target: msfrpcd on Kali VM (settings.msf_host / msf_port / msf_password)
- NATO OTP challenge is MANDATORY before any exploit launch
- Sessions are closed immediately after verification
- MsfRpcClient created per-operation to avoid stale connection issues
"""

import asyncio
from datetime import datetime, timezone

from core.config import settings
from core.events import make_event


def _connect_msf():
    from pymetasploit3.msfrpc import MsfRpcClient
    return MsfRpcClient(
        settings.msf_password,
        server=settings.msf_host,
        port=settings.msf_port,
        ssl=False,
    )


def _run_module_worker(module_path: str, payload: str, options: dict) -> dict:
    """
    Blocking worker — runs in executor.
    1. Connect to msfrpcd. 2. Run exploit module. 3. Wait up to 30s for session.
    4. Close session immediately. 5. Return plain dict.
    """
    import time
    from pymetasploit3.msfrpc import MsfRpcClient

    client  = MsfRpcClient(
        settings.msf_password,
        server=settings.msf_host,
        port=settings.msf_port,
        ssl=False,
    )
    exploit = client.modules.use("exploit", module_path)
    exploit["PAYLOAD"] = payload
    for k, v in options.items():
        exploit[k] = v

    exploit.execute(payload=payload)
    deadline     = time.time() + 30
    session_info = None
    while time.time() < deadline:
        sessions = client.sessions.list
        if sessions:
            sid          = list(sessions.keys())[0]
            session_info = dict(sessions[sid])
            client.sessions.session(sid).stop()
            break
        time.sleep(1)

    return {
        "module":         module_path,
        "payload":        payload,
        "options":        options,
        "session_opened": session_info is not None,
        "session_info":   session_info,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    }


async def launch_adversary_emulation(
    module_path: str,
    payload: str,
    options: dict,
    broadcast_fn,
    tool_executor,
) -> None:
    """NATO OTP gate → async RPC bridge → result broadcast."""
    auth_ok, auth_word = await tool_executor._challenge(
        tool_name="offensive_rpc",
        preview=f"{module_path} → {options.get('RHOSTS', '?')}",
    )
    if not auth_ok:
        await broadcast_fn(make_event(
            "error",
            error=f"[DENIED] Adversary emulation blocked — NATO challenge failed: {auth_word}",
        ))
        return

    await broadcast_fn(make_event(
        "emulation_start",
        module=module_path,
        target=options.get("RHOSTS", "unknown"),
    ))

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, _run_module_worker, module_path, payload, options
        )
        await broadcast_fn(make_event(
            "emulation_complete",
            module=result["module"],
            session_opened=result["session_opened"],
            session_info=result["session_info"],
            timestamp=result["timestamp"],
        ))
    except Exception as e:
        await broadcast_fn(make_event("error", error=f"MSF RPC failed: {e}"))
