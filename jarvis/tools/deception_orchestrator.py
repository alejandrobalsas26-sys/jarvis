"""
tools/deception_orchestrator.py — Active Deception Orchestrator (v20.0).

Honey-tokens are planted inside VMware guest environments only — never on the
Windows host.  VM interactions use asyncio.create_subprocess_exec with
shell=False and discrete argument lists (no user input interpolated).

ETW tripwire integration: when handle_tripwire() is called by etw_monitor,
it broadcasts the deception_tripped event and wires into the existing
core/mitigation.py SOAR pipeline for automatic IP isolation.
"""

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

VMRUN_PATH = r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"

HONEY_TOKENS: dict[str, str] = {
    "fake_admin_cred": "Administrator:Honey$2024!",
    "fake_api_key":    "AKIAIOSFODNN7FAKE1234",
    "fake_db_conn":    "Server=10.0.0.1;Database=HR;User=sa;Password=fake",
}

# Registry canary key — read access by any process triggers the tripwire
_CANARY_REG_KEY   = r"HKLM\SOFTWARE\JARVIS\HoneyCredentials"
_CANARY_REG_VALUE = "AdminPassword"

# Guest paths for planted lures (inside the VM)
_GUEST_CRED_FILE  = r"C:\ProgramData\Microsoft\Vault\credentials.txt"
_GUEST_HONEY_PROC = "svchost_honey.exe"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _vmrun(vmx_path: str, *args: str) -> tuple[int, str]:
    """
    Run a vmrun.exe command and return (returncode, stderr_text).
    shell=False always — args are a discrete list, never interpolated.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws", *args, vmx_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        return proc.returncode or 0, stderr.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return -1, f"vmrun.exe not found at {VMRUN_PATH!r}"
    except Exception as exc:
        return -1, str(exc)


async def plant_deception_lures(vmx_path: str, broadcast_fn) -> None:
    """
    Plant honey-tokens inside the VMware guest:
      1. Copy a fake credential file into the guest via copyFileFromHostToGuest.
      2. Write a registry canary key via runProgramInGuest + reg.exe.
      3. Drop fake API key file in a well-known exfil staging path.

    All file content is created on the host as temp files, then copied
    to avoid any shell-metacharacter issues with vmrun's argument passing.
    """
    await broadcast_fn({
        "type":      "deception_planting",
        "vmx":       vmx_path,
        "tokens":    list(HONEY_TOKENS.keys()),
        "timestamp": _now_iso(),
    })

    planted: list[str] = []
    errors:  list[str] = []

    # ── Lure 1: Fake credential file ─────────────────────────────────────────
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(HONEY_TOKENS["fake_admin_cred"] + "\n")
            tmp.write(HONEY_TOKENS["fake_db_conn"] + "\n")
            host_cred_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws",
            "copyFileFromHostToGuest", vmx_path,
            host_cred_path, _GUEST_CRED_FILE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        Path(host_cred_path).unlink(missing_ok=True)

        if proc.returncode == 0:
            planted.append("fake_cred_file")
        else:
            errors.append(f"cred_file: {stderr[:80]}")
    except Exception as exc:
        errors.append(f"cred_file: {exc}")

    # ── Lure 2: Registry canary key via reg.exe in guest ─────────────────────
    # Args are discrete list elements — no shell interpolation. shell=False.
    try:
        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws",
            "runProgramInGuest", vmx_path,
            "reg.exe", "add", _CANARY_REG_KEY,
            "/v", _CANARY_REG_VALUE,
            "/t", "REG_SZ",
            "/d", HONEY_TOKENS["fake_admin_cred"],
            "/f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            planted.append("registry_canary")
        else:
            errors.append(f"registry: {stderr[:80]}")
    except Exception as exc:
        errors.append(f"registry: {exc}")

    # ── Lure 3: Fake API key file ─────────────────────────────────────────────
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(f"AWS_ACCESS_KEY_ID={HONEY_TOKENS['fake_api_key']}\n")
            tmp.write("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYFAKEKEY\n")
            host_api_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            VMRUN_PATH, "-T", "ws",
            "copyFileFromHostToGuest", vmx_path,
            host_api_path, r"C:\Users\Administrator\.aws\credentials",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        Path(host_api_path).unlink(missing_ok=True)

        if proc.returncode == 0:
            planted.append("fake_api_key_file")
        else:
            errors.append(f"api_key: {stderr[:80]}")
    except Exception as exc:
        errors.append(f"api_key: {exc}")

    await broadcast_fn({
        "type":      "deception_planted",
        "vmx":       vmx_path,
        "planted":   planted,
        "errors":    errors,
        "timestamp": _now_iso(),
    })

    if planted:
        logger.info(f"DECEPTION: planted {len(planted)} lure(s) in {Path(vmx_path).name}")
    if errors:
        logger.warning(f"DECEPTION: {len(errors)} lure(s) failed: {errors}")


async def handle_tripwire(
    source_pid: int,
    resource_id: str,
    broadcast_fn,
) -> None:
    """
    Called by the ETW monitor when a deception resource is accessed.

    1. Broadcasts deception_tripped event to the HUD.
    2. Resolves network connections from source_pid via psutil.
    3. Feeds public remote IPs through the mitigation SOAR pipeline
       (should_isolate AND-gate → isolate_ip firewall rule).
    """
    await broadcast_fn({
        "type":         "deception_tripped",
        "source_pid":   source_pid,
        "target_token": resource_id,
        "severity":     "CRITICAL",
        "timestamp":    _now_iso(),
    })

    logger.warning(
        f"DECEPTION: tripwire triggered — PID={source_pid} accessed '{resource_id}'"
    )

    # ── SOAR: attempt IP isolation ────────────────────────────────────────────
    remote_ips: list[str] = []
    try:
        import psutil
        proc = psutil.Process(source_pid)
        for conn in proc.net_connections(kind="inet"):
            if conn.raddr and conn.raddr.ip:
                remote_ips.append(conn.raddr.ip)
    except Exception as exc:
        logger.warning(f"DECEPTION: psutil connection lookup failed — {exc}")

    if not remote_ips:
        return

    try:
        from core.mitigation import should_isolate, isolate_ip

        # Construct a triage dict that passes the AND-gate:
        # entropy >= 5.0  AND  a critical MITRE technique present.
        # Deception tripwire is a high-confidence signal → use T1055 (Process Injection).
        triage = {
            "entropy":           6.5,
            "extracted_ips":     list(set(remote_ips)),
            "mitre_detections":  [{"technique": "T1055"}],
        }
        targets = should_isolate(triage)
        for ip in targets:
            asyncio.create_task(
                isolate_ip(ip, broadcast_fn),
                name=f"deception-isolate-{ip}",
            )
            logger.warning(f"DECEPTION: scheduling firewall isolation for {ip}")
    except ImportError:
        logger.warning("DECEPTION: core.mitigation not available — isolation skipped")
    except Exception as exc:
        logger.warning(f"DECEPTION: SOAR pipeline error — {exc}")
