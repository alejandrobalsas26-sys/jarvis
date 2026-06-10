"""
core/pcap_capture.py — V57.0 NEXUS: PCAP Forensics Orchestrator.

Captures network traffic for high-severity exfiltration and C2 alerts.
Uses tshark (preferred) or tcpdump. Never uses shell=True.
Prevents capture storms via per-alert cooldown/dedup.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# ── Input validators ──────────────────────────────────────────────────────────

_IP_RE    = re.compile(
    r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$'
)
_IFACE_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{1,64}$')

_EXFIL_TECHNIQUES = frozenset({
    "T1041", "T1567", "T1567.001", "T1567.002",
    "T1020", "T1048", "T1048.001", "T1048.002",
})
_C2_TECHNIQUES = frozenset({
    "T1071", "T1071.001", "T1071.004",
    "T1095", "T1102", "T1132",
})


# ── PCAPCaptureOrchestrator ───────────────────────────────────────────────────

class PCAPCaptureOrchestrator:
    """
    Triggers forensic packet captures for high-severity exfil/C2 events.
    Safe-by-default: dormant unless JARVIS_PCAP_ENABLED is set.
    """

    def __init__(self) -> None:
        self._cooldown: dict[str, float] = {}

    def _cooldown_secs(self) -> float:
        return 120.0

    def is_enabled(self) -> bool:
        return os.environ.get("JARVIS_PCAP_ENABLED", "").lower() in ("1", "true", "yes")

    def _output_dir(self) -> Path:
        return Path(os.environ.get("JARVIS_PCAP_OUTPUT_DIR", "logs/forensics"))

    def _duration(self) -> int:
        return max(1, int(os.environ.get("JARVIS_PCAP_DURATION", "60")))

    def _dry_run(self) -> bool:
        return os.environ.get("JARVIS_PCAP_DRY_RUN", "").lower() in ("1", "true", "yes")

    # ── Public API ─────────────────────────────────────────────────────────────

    async def capture_for_alert(self, alert: dict) -> dict:
        """
        Schedule a timed packet capture for a high-severity alert.
        Returns immediately; the actual capture runs as a background asyncio task.
        """
        if not self.is_enabled():
            return {"status": "disabled"}

        key = self._dedup_key(alert)
        now = time.monotonic()
        if key in self._cooldown and now - self._cooldown[key] < self._cooldown_secs():
            remaining = self._cooldown_secs() - (now - self._cooldown[key])
            logger.debug(
                "PCAP: cooldown active for %r (%.0fs remaining)", key, remaining
            )
            return {"status": "cooldown", "key": key}
        self._cooldown[key] = now

        interface = self.detect_interface()
        if interface is None:
            logger.warning("PCAP: no usable interface found — capture skipped")
            return {"status": "no_interface"}

        output_dir = self._output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("PCAP: cannot create output dir %s: %s", output_dir, e)
            return {"status": "error", "reason": str(e)}

        ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        inc_id = re.sub(r'[^a-zA-Z0-9_\-]', '_',
                        str(alert.get("incident_id") or alert.get("type") or "alert"))
        output = output_dir / f"pcap_{inc_id}_{ts}.pcap"

        try:
            cmd = self.build_command(interface, output, self._duration(), alert)
        except ValueError as e:
            logger.warning("PCAP: build_command failed: %s", e)
            return {"status": "error", "reason": str(e)}

        if self._dry_run():
            logger.info("PCAP[DRY-RUN]: %s", " ".join(cmd))
            return {"status": "dry_run", "cmd": cmd, "output": str(output)}

        asyncio.create_task(
            self._run_capture(cmd, output),
            name=f"pcap-{inc_id}-{ts}",
        )
        logger.info(
            "PCAP: capture scheduled → %s (interface=%s duration=%ds)",
            output, interface, self._duration(),
        )
        return {"status": "started", "file": str(output), "duration": self._duration()}

    def detect_interface(self) -> str | None:
        """Detect the primary network interface. Explicit env var takes priority."""
        explicit = os.environ.get("JARVIS_PCAP_INTERFACE", "").strip()
        if explicit:
            return explicit if _IFACE_RE.match(explicit) else None

        try:
            import psutil
            for name, stats in psutil.net_if_stats().items():
                lower = name.lower()
                if (stats.isup
                        and lower not in ("lo", "loopback")
                        and not lower.startswith("loop")
                        and _IFACE_RE.match(name)):
                    return name
        except Exception:
            pass

        # Last-resort: well-known interface names on Linux / Windows
        for candidate in ("eth0", "Ethernet", "Wi-Fi", "ens3", "enp0s3", "wlan0"):
            if _IFACE_RE.match(candidate):
                return candidate

        return None

    def build_command(
        self,
        interface: str,
        output: Path,
        duration: int,
        alert: dict,
    ) -> list[str]:
        """
        Build a safe tshark or tcpdump command list.
        All dynamic values are validated. shell=True is never used.
        """
        if not _IFACE_RE.match(interface):
            raise ValueError(f"Unsafe interface name: {interface!r}")

        if not isinstance(duration, int) or not (1 <= duration <= 3600):
            raise ValueError(f"Invalid duration: {duration!r}")

        # Extract and validate an optional IP to narrow the BPF filter
        ip: str | None = None
        for field in ("src_ip", "attacker_ip", "remote_ip", "ip", "source_ip"):
            raw = alert.get(field)
            if raw and _IP_RE.match(str(raw)):
                ip = str(raw)
                break

        tool = os.environ.get("JARVIS_PCAP_TOOL", "tshark").lower()

        if tool == "tshark":
            cmd: list[str] = [
                "tshark",
                "-i", interface,
                "-a", f"duration:{duration}",
                "-w", str(output),
            ]
            if ip:
                # ip is regex-validated; passing as separate list arg is safe (shell=False)
                cmd += ["-f", f"host {ip}"]
        else:
            cmd = [
                "tcpdump",
                "-i", interface,
                "-G", str(duration),
                "-W", "1",
                "-w", str(output),
            ]
            if ip:
                cmd += ["host", ip]

        return cmd

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _dedup_key(alert: dict) -> str:
        """Stable key for cooldown dedup based on rule + source IP."""
        rule = alert.get("rule") or alert.get("type") or "unknown"
        ip   = (alert.get("src_ip") or alert.get("attacker_ip")
                or alert.get("remote_ip") or "noip")
        return f"{rule}:{ip}"

    async def _run_capture(self, cmd: list[str], output: Path) -> dict:
        """Run the capture subprocess. Returns a result dict when complete."""
        timeout = self._duration() + 30
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            rc  = proc.returncode
            err = stderr.decode(errors="replace")[:300] if stderr else ""

            # tshark may return 1 for end-of-capture; treat 0 and 1 as success
            if rc is not None and rc not in (0, 1):
                logger.warning("PCAP: capture exited %d: %s", rc, err)
                return {"status": "error", "returncode": rc, "stderr": err}

            size = output.stat().st_size if output.exists() else 0
            logger.info("PCAP: capture complete → %s (%d bytes)", output, size)
            return {"status": "ok", "file": str(output), "bytes": size}

        except asyncio.TimeoutError:
            logger.warning("PCAP: capture timed out after %ds — killing process", timeout)
            try:
                proc.kill()
            except Exception:
                pass
            return {"status": "timeout"}
        except FileNotFoundError:
            logger.warning("PCAP: tool not found: %s", cmd[0])
            return {"status": "tool_missing", "tool": cmd[0]}
        except PermissionError as e:
            logger.warning("PCAP: permission denied: %s", e)
            return {"status": "permission_denied"}
        except Exception as e:
            logger.error("PCAP: unexpected error: %s", e)
            return {"status": "error", "detail": str(e)[:200]}


# Module-level singleton
pcap_orchestrator = PCAPCaptureOrchestrator()
