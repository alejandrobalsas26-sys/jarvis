"""core/mitigation.py — Non-blocking SOAR engine with TTL-based firewall isolation.

Infrastructure safety: RFC1918 private and loopback ranges are NEVER isolated.
Isolation gate: entropy AND-gate with critical MITRE technique check before any action.
"""

import asyncio
import ipaddress

from loguru import logger

from core.config import settings
from core.events import make_event

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]

_CRITICAL_TECHNIQUES: frozenset[str] = frozenset({
    "T1059.001",  # PowerShell / Script Execution
    "T1055",      # Process Injection
    "T1562.001",  # Security Software Tampering
    "T1036",      # Masquerading
})


def _is_public_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return not any(addr in net for net in _PRIVATE_NETWORKS) and not addr.is_loopback
    except ValueError:
        return False


def should_isolate(triage: dict) -> list[str]:
    """Return public IPs to isolate, or [] if AND-gate conditions are not met."""
    entropy = triage.get("entropy", 0.0)
    if entropy < settings.entropy_threshold:
        return []

    detections   = triage.get("mitre_detections", [])
    detected_ids = {d.get("technique", "") for d in detections}
    if not detected_ids & _CRITICAL_TECHNIQUES:
        return []

    return [ip for ip in triage.get("extracted_ips", []) if _is_public_ip(ip)]


def _isolation_authorized(ip: str) -> bool:
    """Ask the ONE gate. Synchronous, fail-closed, and never self-satisfied.

    Evidence is built here because the primitive is what has it: the caller
    established that this address is worth blocking, and the gate refuses a
    request that cites nothing. Fail-closed on ANY error — an authorization
    check that crashes has not authorized anything.
    """
    try:
        from core.cognitive_mesh import SpecialistId
        from core.mesh_contracts import EvidenceGraph, EvidenceRef, Provenance
        from core.security_effects import (
            CONTAINMENT, DefensiveActionClass, authorize_effect, propose_effect,
        )

        graph = EvidenceGraph()
        ref = graph.add_evidence(EvidenceRef(
            content=f"mitigation: {ip} selected for outbound isolation",
            provenance=Provenance.TELEMETRY, source="mitigation",
            specialist=SpecialistId.GUARDIAN))
        request = propose_effect(
            action=DefensiveActionClass.FIREWALL_BLOCK_ADDRESS, target=ip,
            justification="automated outbound isolation",
            requested_by=SpecialistId.GUARDIAN,
            evidence_ids=(ref,) if ref else (),
            rollback_plan="the rule carries a TTL and is removed on expiry")
        decision = authorize_effect(request, registry=CONTAINMENT, graph=graph)
        return bool(decision.allowed and decision.unattended)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED, always
        logger.warning(f"mitigation: authorization check failed ({exc}) — refusing")
        return False


async def isolate_ip(ip: str, broadcast_fn, ttl_minutes: int = 60) -> None:
    """Block outbound traffic to ip via Windows Defender Firewall with TTL auto-expiry.

    V69 M64.1 §51 — GATED AT THE PRIMITIVE.

    This function is the shared firewall-mutation primitive behind three
    independent callers, and every one of them reached it with no operator in
    the loop: the SOAR hook in ``tools/executor.py`` after a *blocked* shell
    command, the RF out-of-band ``isolate:<ip>`` command in ``tools/rf_oob.py``,
    and (until M64.1) the playbook engine. The playbook path is now gated at its
    own call site; gating the other two individually would leave the primitive
    itself open to the next caller.

    So the check lives HERE, where it cannot be forgotten. Without a registered,
    unexpired, target-scoped ContainmentAuthorization granting
    FIREWALL_BLOCK_ADDRESS unattended, this returns having mutated nothing and
    having told the operator what it would have done.
    """
    from core.telemetry_auth import make_signed_broadcaster
    broadcast_fn = make_signed_broadcaster(broadcast_fn, "mitigation")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        logger.warning(f"mitigation: invalid IP '{ip}' — isolation aborted")
        return

    if not _isolation_authorized(ip):
        logger.warning(
            f"mitigation: isolation of {ip} RECOMMENDED but NOT authorized — "
            f"no operator ContainmentAuthorization covers it"
        )
        try:
            await broadcast_fn(make_event(
                "containment_recommended",
                target=ip,
                action="firewall_block_address",
                authorized=False,
                reason="no operator ContainmentAuthorization covers this target",
            ))
        except Exception as exc:  # noqa: BLE001 — never blocks a refusal
            logger.debug(f"mitigation: refusal notification skipped ({exc})")
        return

    rule_name     = f"JARVIS_BLOCK_{ip}"
    sleep_seconds = ttl_minutes * 60
    ps_cmd = (
        f"New-NetFirewallRule -DisplayName '{rule_name}' "
        f"-Direction Outbound -Action Block -RemoteAddress {ip}; "
        f"Start-Job -ScriptBlock {{Start-Sleep -Seconds {sleep_seconds}; "
        f"Remove-NetFirewallRule -DisplayName '{rule_name}'}}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            logger.info(f"mitigation: firewall rule added — BLOCK {ip} (TTL={ttl_minutes}m)")
            await broadcast_fn(make_event(
                "firewall_block",
                isolated_ip=ip,
                ttl_minutes=ttl_minutes,
                rule_name=rule_name,
            ))
        else:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                f"mitigation: PowerShell returned {proc.returncode} for {ip} — {err[:200]}"
            )
    except FileNotFoundError:
        logger.warning("mitigation: powershell.exe not found — firewall isolation unavailable")
    except Exception as exc:
        logger.warning(f"mitigation: isolation failed for {ip} — {exc}")
