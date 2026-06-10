"""
core/cisco_controller.py — V57.0 NEXUS: Cisco bare-metal containment controller.

Dormant when env vars are absent. Supports Cisco 1921 routers (drop ACL injection)
and Catalyst 2960S switches (MAC blackhole VLAN). SSH via asyncssh (preferred) or
netmiko via asyncio.to_thread (fallback). Never executes arbitrary alert commands.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from loguru import logger

# ── Input validators ──────────────────────────────────────────────────────────

_MAC_RE  = re.compile(
    r'^[0-9a-fA-F]{2}(?:[:\-][0-9a-fA-F]{2}){5}$'
    r'|^[0-9a-fA-F]{12}$'
    r'|^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$'
)
_IP_RE   = re.compile(
    r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$'
)
_VLAN_RE = re.compile(r'^\d{1,4}$')
# Cisco interface names: e.g. GigabitEthernet0/0, Fa0/1, Te1/0/1
_IFACE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9/\.\-]{0,63}$')


def _normalize_mac(mac: str) -> str:
    """Normalize any MAC format to Cisco IOS dotted format: aabb.ccdd.eeff."""
    clean = re.sub(r'[:\-\.]', '', mac.lower())
    if len(clean) != 12:
        raise ValueError(f"Invalid MAC length: {mac!r}")
    int(clean, 16)  # raises ValueError if not valid hex
    return f"{clean[0:4]}.{clean[4:8]}.{clean[8:12]}"


def _validate_ip(ip: str) -> str:
    """Raise ValueError if ip is not a valid IPv4 address, else return it."""
    if not _IP_RE.match(str(ip)):
        raise ValueError(f"Invalid IP: {ip!r}")
    return str(ip)


# ── SSH backend probes ────────────────────────────────────────────────────────

def _check_asyncssh() -> bool:
    try:
        import asyncssh  # noqa: F401
        return True
    except ImportError:
        return False


def _check_netmiko() -> bool:
    try:
        from netmiko import ConnectHandler  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_host(url: str) -> tuple[str, int]:
    """Parse host and port from 'host', 'host:port', or 'scheme://host:port'."""
    url = re.sub(r'^\w[\w+\-.]*://', '', url).rstrip('/')
    if ':' in url:
        host, port_str = url.rsplit(':', 1)
        if port_str.isdigit():
            return host, int(port_str)
    return url, 22


def _map_device_type(dt: str) -> str:
    """Map env device-type string to netmiko device type."""
    if "nxos" in dt or "nexus" in dt:
        return "cisco_nxos"
    return dt if dt.startswith("cisco_") else "cisco_ios"


# ── CiscoController ────────────────────────────────────────────────────────────

class CiscoController:
    """
    Manages containment actions on Cisco 1921 routers and Catalyst 2960S switches.
    Completely dormant when required env vars are absent.
    """

    _HEALTH_INTERVAL = 300  # seconds between SSH health-checks when enabled

    # ── Configuration (read from env at call-time for hot-reload) ─────────────

    @property
    def _ssh_url(self) -> str:
        return os.environ.get("JARVIS_HW_SSH_URL", "")

    @property
    def _username(self) -> str:
        return os.environ.get("JARVIS_HW_USERNAME", "")

    @property
    def _password(self) -> str:
        return os.environ.get("JARVIS_HW_PASSWORD", "")

    @property
    def _enable_secret(self) -> str:
        return os.environ.get("JARVIS_HW_ENABLE_SECRET", "")

    @property
    def _device_type(self) -> str:
        return os.environ.get("JARVIS_HW_DEVICE_TYPE", "cisco_ios").lower()

    @property
    def _blackhole_vlan(self) -> str:
        return os.environ.get("JARVIS_BLACKHOLE_VLAN", "999")

    @property
    def _timeout(self) -> float:
        return float(os.environ.get("JARVIS_HW_TIMEOUT", "30"))

    @property
    def _hw_interface(self) -> str:
        # No default: router ACL injection requires an explicit interface.
        return os.environ.get("JARVIS_HW_INTERFACE", "").strip()

    def _hw_action_enabled(self) -> bool:
        """Real hardware actions require an explicit JARVIS_HW_ENABLE=true opt-in."""
        return os.environ.get("JARVIS_HW_ENABLE", "").lower() in ("1", "true", "yes")

    def _persist_config(self) -> bool:
        """Persist running-config to startup only when explicitly opted in."""
        return os.environ.get("JARVIS_HW_PERSIST_CONFIG", "").lower() in ("1", "true", "yes")

    def _dry_run(self) -> bool:
        """
        Safe-by-default: dry-run unless real actions are enabled AND the operator
        has explicitly set JARVIS_HW_DRY_RUN=false. Without JARVIS_HW_ENABLE=true
        every action is forced to dry-run.
        """
        if not self._hw_action_enabled():
            return True
        return os.environ.get("JARVIS_HW_DRY_RUN", "true").lower() not in ("0", "false", "no")

    def is_enabled(self) -> bool:
        """True only when SSH URL, username, and password are all configured."""
        return bool(self._ssh_url and self._username and self._password)

    # ── Watchdog / dormant loop ────────────────────────────────────────────────

    async def start(self) -> None:
        """Periodic SSH health-check loop. Dormant (sleep-only) when not enabled."""
        while True:
            if self.is_enabled():
                await self._health_check()
            await asyncio.sleep(self._HEALTH_INTERVAL)

    async def _health_check(self) -> None:
        if not _check_asyncssh():
            return
        try:
            import asyncssh
            host, port = _parse_host(self._ssh_url)
            async with asyncssh.connect(
                host, port=port,
                username=self._username, password=self._password,
                known_hosts=None, connect_timeout=min(self._timeout, 10),
            ):
                logger.debug("CISCO: SSH health-check OK → %s", self._ssh_url)
        except Exception as e:
            logger.warning("CISCO: SSH health-check failed: %s", e)

    # ── Public containment API ─────────────────────────────────────────────────

    async def contain_alert(self, alert: dict) -> dict:
        """
        High-level containment dispatcher for sev >= 9.5 alerts.
        Extracts MAC/IP indicators and routes to the appropriate action.
        """
        if not self.is_enabled():
            return {"status": "dormant", "reason": "hw_not_configured"}

        sev = float(alert.get("severity_score") or alert.get("severity") or 0)
        if sev < 9.5:
            return {"status": "skip", "reason": f"sev {sev:.1f} < 9.5"}

        device  = self._device_type
        results: list[dict] = []

        mac = alert.get("mac") or alert.get("mac_address")
        if mac and ("2960" in device or "catalyst" in device):
            try:
                r = await self.blackhole_mac(mac, reason=f"auto-contain sev={sev:.1f}")
                results.append(r)
            except Exception as e:
                logger.warning("CISCO: MAC containment error: %s", e)

        src_ip = (alert.get("src_ip") or alert.get("attacker_ip")
                  or alert.get("source_ip") or alert.get("remote_ip"))
        dst_ip = alert.get("dst_ip") or alert.get("destination_ip")
        if (src_ip or dst_ip) and ("1921" in device or "router" in device):
            try:
                r = await self.inject_drop_acl(
                    src_ip, dst_ip, reason=f"auto-contain sev={sev:.1f}"
                )
                results.append(r)
            except Exception as e:
                logger.warning("CISCO: ACL containment error: %s", e)

        if not results:
            logger.info(
                "CISCO: contain_alert sev=%.1f — no usable indicators for device "
                "type %r; no-op", sev, device
            )
            return {"status": "no_op", "reason": "no_indicators"}

        return {"status": "ok", "actions": results}

    async def blackhole_mac(self, mac: str, reason: str = "") -> dict:
        """
        Catalyst 2960S: add a static MAC-table drop entry to blackhole the MAC.
        IOS command: mac address-table static {mac} vlan {vlan} drop
        """
        if not self.is_enabled():
            return {"status": "dormant"}

        try:
            norm = _normalize_mac(mac)
        except ValueError as e:
            logger.warning("CISCO: blackhole_mac invalid MAC %r: %s", mac, e)
            return {"status": "error", "reason": str(e)}

        vlan = self._blackhole_vlan
        if not _VLAN_RE.match(vlan):
            return {"status": "error", "reason": f"invalid VLAN value {vlan!r}"}

        commands = [
            "configure terminal",
            f"mac address-table static {norm} vlan {vlan} drop",
            "end",
        ]
        if self._persist_config():
            commands.append("write memory")

        if self._dry_run():
            logger.info(
                "CISCO[DRY-RUN] blackhole_mac %s vlan=%s reason=%r",
                norm, vlan, reason,
            )
            return {"status": "dry_run", "mac": norm, "vlan": vlan, "commands": commands}

        logger.warning("CISCO: blackholing MAC %s vlan=%s reason=%r", norm, vlan, reason)
        ok, output = await self._exec_commands(commands)
        return {
            "status": "ok" if ok else "error",
            "mac": norm, "vlan": vlan,
            "output": output[:500],
        }

    async def inject_drop_acl(
        self,
        src_ip: str | None,
        dst_ip: str | None,
        reason: str = "",
    ) -> dict:
        """
        Cisco 1921: create a named ephemeral drop ACL and apply it inbound
        on the configured interface.
        """
        if not self.is_enabled():
            return {"status": "dormant"}

        if not src_ip and not dst_ip:
            return {"status": "no_op", "reason": "no IP indicators"}

        deny_lines: list[str] = []
        try:
            if src_ip:
                _validate_ip(str(src_ip))
                deny_lines.append(f"deny ip host {src_ip} any log")
            if dst_ip:
                _validate_ip(str(dst_ip))
                deny_lines.append(f"deny ip any host {dst_ip} log")
        except ValueError as e:
            return {"status": "error", "reason": str(e)}

        interface = self._hw_interface
        if not interface:
            return {"status": "error",
                    "reason": "JARVIS_HW_INTERFACE not set (required for ACL injection)"}
        if not _IFACE_RE.match(interface):
            return {"status": "error", "reason": f"invalid interface {interface!r}"}

        acl_token = str(uuid.uuid4())[:8].upper()
        acl_name  = f"JARVIS-DROP-{acl_token}"

        commands = (
            ["configure terminal", f"ip access-list extended {acl_name}"]
            + deny_lines
            + [
                "permit ip any any",
                "exit",
                f"interface {interface}",
                f"ip access-group {acl_name} in",
                "exit",
                "end",
            ]
        )
        if self._persist_config():
            commands.append("write memory")

        if self._dry_run():
            logger.info(
                "CISCO[DRY-RUN] inject_drop_acl acl=%s reason=%r", acl_name, reason
            )
            return {"status": "dry_run", "acl": acl_name, "interface": interface,
                    "commands": commands}

        logger.warning(
            "CISCO: injecting drop ACL %s src=%s dst=%s reason=%r",
            acl_name, src_ip, dst_ip, reason,
        )
        ok, output = await self._exec_commands(commands)
        return {
            "status": "ok" if ok else "error",
            "acl": acl_name, "interface": interface,
            "output": output[:500],
        }

    # ── SSH execution back-end ────────────────────────────────────────────────

    async def _exec_commands(self, commands: list[str]) -> tuple[bool, str]:
        """Execute IOS commands. Prefer asyncssh; fall back to netmiko."""
        if _check_asyncssh():
            try:
                return await self._asyncssh_exec(commands)
            except Exception as e:
                logger.warning("CISCO: asyncssh exec failed (%s) — trying netmiko", e)

        if _check_netmiko():
            return await asyncio.to_thread(self._netmiko_exec, commands)

        logger.error("CISCO: no SSH backend available (install asyncssh or netmiko)")
        return False, "no_backend"

    async def _asyncssh_exec(self, commands: list[str]) -> tuple[bool, str]:
        import asyncssh  # noqa: F401

        host, port = _parse_host(self._ssh_url)
        all_out: list[str] = []

        async with asyncssh.connect(
            host, port=port,
            username=self._username, password=self._password,
            known_hosts=None,
            connect_timeout=self._timeout,
        ) as conn:
            async with conn.create_process(term_type="dumb") as proc:
                await asyncio.sleep(1.5)  # drain banner + initial prompt

                async def _send(text: str, delay: float = 0.8) -> str:
                    proc.stdin.write(text + "\n")
                    await asyncio.sleep(delay)
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(4096), timeout=5.0
                        )
                        decoded = (chunk if isinstance(chunk, str)
                                   else chunk.decode(errors="replace"))
                        all_out.append(decoded)
                        return decoded
                    except asyncio.TimeoutError:
                        return ""

                if self._enable_secret:
                    await _send("enable")
                    await _send(self._enable_secret)

                for cmd in commands:
                    write_delay = 3.0 if "write" in cmd else 0.8
                    await _send(cmd, delay=write_delay)

        return True, "\n".join(all_out)

    def _netmiko_exec(self, commands: list[str]) -> tuple[bool, str]:
        """Blocking netmiko execution — must be called via asyncio.to_thread."""
        from netmiko import ConnectHandler  # noqa: F401

        host, port = _parse_host(self._ssh_url)
        device_params = {
            "device_type": _map_device_type(self._device_type),
            "host":        host,
            "port":        port,
            "username":    self._username,
            "password":    self._password,
            "secret":      self._enable_secret,
            "timeout":     int(self._timeout),
            "fast_cli":    True,
        }
        with ConnectHandler(**device_params) as conn:
            if self._enable_secret:
                conn.enable()
            config_cmds = [
                c for c in commands
                if c not in ("write memory", "end", "exit", "configure terminal")
            ]
            output = conn.send_config_set(config_cmds)
            if self._persist_config():
                conn.save_config()
        return True, output


# Module-level singleton
cisco_controller = CiscoController()
