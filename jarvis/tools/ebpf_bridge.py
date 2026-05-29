"""
tools/ebpf_bridge.py — eBPF Kernel Telemetry Bridge from Kali VM via Falco.

Architecture:
  - Falco runs on KALI VM (Linux/RDNA2). eBPF is production-ready on Linux.
  - SSH tunnel from Jarvis Windows host → Kali VM using paramiko in run_in_executor.
  - All Falco JSON output passes through core/feed_sanitizer before HUD injection.
  - Credentials from env vars: KALI_HOST, KALI_USER, KALI_KEY_PATH.
  - If KALI_HOST not set or SSH fails: total silence, no warnings on loop.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from loguru import logger

from core.feed_sanitizer import sanitize_for_hud, SanitizationError

KALI_HOST     = os.getenv("KALI_HOST",     "")
KALI_USER     = os.getenv("KALI_USER",     "kali")
KALI_KEY_PATH = os.getenv("KALI_KEY_PATH", "")
FALCO_CMD     = (
    "sudo falco --unbuffered "
    "-o json_output=true "
    "-o json_include_output_property=true "
    "2>/dev/null"
)

_ssh_client = None   # module-level ref prevents GC killing connection


async def start_ebpf_bridge(broadcast_fn) -> None:
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "ebpf")

    if not KALI_HOST or not KALI_KEY_PATH:
        logger.info("EBPF: KALI_HOST not configured — bridge dormant")
        await asyncio.Event().wait()   # sleep forever, watchdog stays happy
        return

    global _ssh_client
    loop = asyncio.get_running_loop()

    while True:
        try:
            client, stdout = await loop.run_in_executor(
                None, _connect_falco
            )
            if client is None:
                await asyncio.sleep(60)
                continue

            _ssh_client = client   # keep alive — prevent GC disconnect
            logger.info(f"EBPF_BRIDGE: connected → {KALI_HOST}, streaming Falco")

            while True:
                line = await loop.run_in_executor(None, stdout.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    alert = json.loads(line)
                    rule   = alert.get("rule",     "")
                    output = alert.get("output",   "")
                    prio   = alert.get("priority", "NOTICE")
                    host   = alert.get("hostname", "kali")

                    await broadcast_fn({
                        "type":      "ebpf_alert",
                        "rule":      sanitize_for_hud(rule,   80),
                        "output":    sanitize_for_hud(output, 200),
                        "priority":  sanitize_for_hud(prio,   20),
                        "hostname":  sanitize_for_hud(host,   40),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                except (json.JSONDecodeError, SanitizationError):
                    continue

        except Exception as e:
            logger.debug(f"EBPF_BRIDGE: {e}")
        finally:
            _ssh_client = None

        await asyncio.sleep(30)


def _connect_falco():
    """Blocking — runs in executor. Returns (client, stdout) or (None, None)."""
    try:
        import paramiko
        key    = paramiko.RSAKey.from_private_key_file(KALI_KEY_PATH)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(KALI_HOST, username=KALI_USER, pkey=key, timeout=10)
        _, stdout, _ = client.exec_command(FALCO_CMD, get_pty=False)
        return client, stdout
    except Exception:
        return None, None
