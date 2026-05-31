"""
tools/rf_oob.py — RF Out-of-Band Command Channel (v28.0).

Listens for AES-256-GCM encrypted commands embedded in 802.11 beacon
vendor IEs on a designated OOB channel via the ALFA AWUS036ACM adapter.
Provides network-independent command channel when primary network is compromised.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from loguru import logger

from core.feed_sanitizer import check_prompt_injection, SanitizationError

RF_OOB_KEY     = os.getenv("RF_OOB_KEY", "")           # 64 hex chars = 32 bytes
RF_OOB_CHANNEL = int(os.getenv("RF_OOB_CHANNEL", "6")) # 802.11 channel
RF_OOB_IFACE   = os.getenv("RF_OOB_IFACE", "")         # ALFA interface name
RF_OOB_VENDOR  = bytes.fromhex("DEADBEEF")             # vendor IE OUI magic

_last_seq: int = -1                  # replay protection
_key_bytes: bytes | None = None


def _get_key() -> bytes | None:
    global _key_bytes
    if _key_bytes:
        return _key_bytes
    if len(RF_OOB_KEY) != 64:
        return None
    try:
        _key_bytes = bytes.fromhex(RF_OOB_KEY)
        return _key_bytes
    except ValueError:
        return None


def _decrypt_payload(raw_b64: str) -> dict | None:
    """
    Decrypt AES-256-GCM payload.
    Format: base64(nonce[12] + ciphertext + tag[16])
    """
    key = _get_key()
    if not key:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw    = base64.b64decode(raw_b64)
        nonce  = raw[:12]
        data   = raw[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, data, None)
        return json.loads(plaintext.decode())
    except Exception:
        return None


def _verify_hmac(payload: dict) -> bool:
    """Verify HMAC-SHA256 of the command payload."""
    key = _get_key()
    if not key:
        return False
    sig  = payload.pop("hmac", "")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def _process_oob_frame(frame_json: dict, broadcast_fn) -> None:
    """Process a captured 802.11 frame for OOB command extraction."""
    global _last_seq
    key = _get_key()
    if not key:
        return

    # Extract vendor IE payload from frame JSON (tshark ek format)
    try:
        layers = frame_json.get("layers", {})
        vendor_data = layers.get("wlan_mgt_tag_vendor_oui_data", "")
        if not vendor_data:
            return
        raw = bytes.fromhex(vendor_data.replace(":", ""))
        if raw[:4] != RF_OOB_VENDOR:
            return
        payload_b64 = raw[4:].decode(errors="replace").strip()
    except Exception:
        return

    # Decrypt
    cmd_data = _decrypt_payload(payload_b64)
    if cmd_data is None:
        return

    # Replay protection
    seq = cmd_data.get("seq", -1)
    if seq <= _last_seq:
        logger.debug(f"RF_OOB: replayed seq {seq} dropped")
        return

    # HMAC verification
    if not _verify_hmac(cmd_data):
        logger.warning("RF_OOB: HMAC verification failed — frame dropped")
        return

    _last_seq = seq
    cmd = str(cmd_data.get("cmd", ""))

    # Sanitize command string
    try:
        check_prompt_injection(cmd, source="rf_oob")
    except SanitizationError:
        logger.warning("RF_OOB: injection attempt in command — dropped")
        return

    logger.info(f"RF_OOB: authenticated command received: '{cmd[:40]}'")
    await broadcast_fn({
        "type":      "rf_oob_command",
        "cmd":       cmd[:40],
        "seq":       seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    await _dispatch_oob_command(cmd, broadcast_fn)


async def _dispatch_oob_command(cmd: str, broadcast_fn) -> None:
    """Execute authenticated OOB command."""
    if cmd == "status":
        from core.correlator import correlator
        incidents = correlator.get_active_incidents()
        await broadcast_fn({
            "type":             "rf_oob_status",
            "active_incidents": len(incidents),
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        })

    elif cmd.startswith("isolate:"):
        ip = cmd.split(":", 1)[1].strip()[:45]
        from core.mitigation import isolate_ip
        asyncio.create_task(asyncio.shield(isolate_ip(ip, broadcast_fn, 60)))

    elif cmd.startswith("alert:"):
        msg = cmd.split(":", 1)[1].strip()[:200]
        await broadcast_fn({
            "type":     "playbook_alert",
            "message":  f"RF-OOB: {msg}",
            "severity": "HIGH",
        })

    elif cmd.startswith("snapshot:"):
        vmx = cmd.split(":", 1)[1].strip()
        from tools.forensic_volatility import trigger_forensic_capture
        asyncio.create_task(trigger_forensic_capture(vmx, broadcast_fn))

    elif cmd.startswith("playbook:"):
        name = cmd.split(":", 1)[1].strip()
        from core.playbook_engine import playbook_engine
        synthetic = {
            "incident_id":      f"OOB_{int(datetime.now().timestamp())}",
            "rule":             name,
            "severity_score":   9.0,
            "mitre_techniques": [],
            "involved_hosts":   [],
            "involved_pids":    [],
            "kill_chain_phase": "Manual",
        }
        asyncio.create_task(playbook_engine.evaluate(synthetic))


async def start_rf_oob(broadcast_fn) -> None:
    """
    Start RF OOB listener using tshark on ALFA interface.
    Silent if RF_OOB_KEY or RF_OOB_IFACE not configured.
    """
    if not RF_OOB_KEY or not RF_OOB_IFACE:
        logger.info("RF_OOB: RF_OOB_KEY not configured — OOB channel dormant")
        await asyncio.Event().wait()
        return

    key = _get_key()
    if not key:
        logger.warning("RF_OOB: RF_OOB_KEY must be 64 hex chars (32 bytes) — disabled")
        return

    import shutil
    tshark = shutil.which("tshark")
    if not tshark:
        logger.warning("RF_OOB: tshark not found — RF OOB disabled")
        return

    logger.info(f"RF_OOB: listening on {RF_OOB_IFACE} channel {RF_OOB_CHANNEL}")

    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                tshark,
                "-i", RF_OOB_IFACE,
                "-f", "type mgt subtype beacon",
                "-T", "ek",
                "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line or line.startswith(b'{"index"'):
                    continue
                try:
                    frame = json.loads(line)
                    await _process_oob_frame(frame, broadcast_fn)
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.debug(f"RF_OOB: {e}")
        await asyncio.sleep(15)
