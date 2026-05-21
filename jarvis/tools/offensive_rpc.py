"""
tools/offensive_rpc.py — Metasploit RPC async bridge (v22.0).

Architecture:
- pymetasploit3 is synchronous → all RPC calls run in run_in_executor(None, ...)
- Connection target: msfrpcd on Kali VM (MSF_HOST / MSF_PORT / MSF_PASSWORD)
- NATO OTP challenge is MANDATORY before any exploit launch
- Sessions are closed immediately after verification
- MsfRpcClient created per-operation to avoid stale connection issues
"""

import asyncio
import os
from datetime import datetime, timezone

MSF_HOST = os.getenv("MSF_HOST", "192.168.1.100")
MSF_PORT = int(os.getenv("MSF_PORT", "55553"))
MSF_PASS = os.getenv("MSF_PASSWORD", "msf")


def _connect_msf():
    """Blocking — runs in executor. Returns MsfRpcClient."""
    from pymetasploit3.msfrpc import MsfRpcClient
    return MsfRpcClient(MSF_PASS, server=MSF_HOST, port=MSF_PORT, ssl=False)


def _run_module_worker(module_path: str, payload: str, options: dict) -> dict:
    """
    Blocking worker — runs in executor.
    1. Connect to msfrpcd on Kali VM.
    2. Instantiate exploit module.
    3. Set options (RHOSTS, LPORT, PAYLOAD, etc.).
    4. Execute module, wait up to 30s for session.
    5. If session opened: capture session info, close session immediately.
    6. Return result dict (no MsfRpcClient objects — not picklable).
    """
    import time
    from pymetasploit3.msfrpc import MsfRpcClient

    client = MsfRpcClient(MSF_PASS, server=MSF_HOST, port=MSF_PORT, ssl=False)
    exploit = client.modules.use("exploit", module_path)
    exploit["PAYLOAD"] = payload
    for k, v in options.items():
        exploit[k] = v

    exploit.execute(payload=payload)
    deadline = time.time() + 30
    session_info = None
    while time.time() < deadline:
        sessions = client.sessions.list
        if sessions:
            sid = list(sessions.keys())[0]
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
    tool_executor,          # ToolExecutor instance for NATO challenge
) -> None:
    """NATO OTP gate → async RPC bridge → result broadcast."""
    auth_ok, auth_word = await tool_executor._challenge(
        tool_name="offensive_rpc",
        preview=f"{module_path} → {options.get('RHOSTS', '?')}",
    )
    if not auth_ok:
        await broadcast_fn({
            "type":  "error",
            "error": f"[DENIED] Adversary emulation blocked — NATO challenge failed: {auth_word}",
        })
        return

    await broadcast_fn({
        "type":   "emulation_start",
        "module": module_path,
        "target": options.get("RHOSTS", "unknown"),
    })

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, _run_module_worker, module_path, payload, options
        )
        await broadcast_fn({
            "type":           "emulation_complete",
            "module":         result["module"],
            "session_opened": result["session_opened"],
            "session_info":   result["session_info"],
            "timestamp":      result["timestamp"],
        })
    except Exception as e:
        await broadcast_fn({"type": "error", "error": f"MSF RPC failed: {e}"})
