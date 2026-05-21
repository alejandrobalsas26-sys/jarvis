"""
tools/deception_orchestrator.py — Active Deception Orchestrator (v24.0).

Honey-tokens are planted inside VMware guest environments only — never on the
Windows host.  VM interactions use asyncio.create_subprocess_exec with
shell=False and discrete argument lists (no user input interpolated).
"""

import asyncio
import tempfile
from pathlib import Path

from loguru import logger

from core.config import settings
from core.events import make_event

HONEY_TOKENS: dict[str, str] = {
    "fake_admin_cred": "Administrator:Honey$2024!",
    "fake_api_key":    "AKIAIOSFODNN7FAKE1234",
    "fake_db_conn":    "Server=10.0.0.1;Database=HR;User=sa;Password=fake",
}

_CANARY_REG_KEY   = r"HKLM\SOFTWARE\JARVIS\HoneyCredentials"
_CANARY_REG_VALUE = "AdminPassword"
_GUEST_CRED_FILE  = r"C:\ProgramData\Microsoft\Vault\credentials.txt"
_GUEST_HONEY_PROC = "svchost_honey.exe"


async def _vmrun(vmx_path: str, *args: str) -> tuple[int, str]:
    """Run a vmrun command — shell=False always."""
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.vmrun_path, "-T", "ws", *args, vmx_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        return proc.returncode or 0, stderr.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return -1, f"vmrun.exe not found at {settings.vmrun_path!r}"
    except Exception as exc:
        return -1, str(exc)


async def plant_deception_lures(vmx_path: str, broadcast_fn) -> None:
    """Plant honey-tokens inside the VMware guest."""
    await broadcast_fn(make_event(
        "deception_planting",
        vmx=vmx_path,
        tokens=list(HONEY_TOKENS.keys()),
    ))

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
            settings.vmrun_path, "-T", "ws",
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
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.vmrun_path, "-T", "ws",
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
            settings.vmrun_path, "-T", "ws",
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

    await broadcast_fn(make_event(
        "deception_planted",
        vmx=vmx_path,
        planted=planted,
        errors=errors,
    ))

    if planted:
        logger.info(f"DECEPTION: planted {len(planted)} lure(s) in {Path(vmx_path).name}")
    if errors:
        logger.warning(f"DECEPTION: {len(errors)} lure(s) failed: {errors}")


async def handle_tripwire(
    source_pid: int,
    resource_id: str,
    broadcast_fn,
) -> None:
    """Called by ETW monitor when a deception resource is accessed."""
    await broadcast_fn(make_event(
        "deception_tripped",
        source_pid=source_pid,
        target_token=resource_id,
        severity="CRITICAL",
    ))

    logger.warning(
        f"DECEPTION: tripwire triggered — PID={source_pid} accessed '{resource_id}'"
    )

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
