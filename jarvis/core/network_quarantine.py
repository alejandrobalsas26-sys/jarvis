"""
core/network_quarantine.py — JARVIS V48.0 VANGUARD
XDR containment. On correlator-flagged lateral movement / scanning (sev >= 9.0)
from a local IP, isolates that host with bidirectional Windows Firewall rules
(endpoint-local) and, where infrastructure is configured, escalates to a
switchport/NAC quarantine webhook. Reversible; every action is audited.

Containment is enforced at the endpoint and (optionally) the network
infrastructure API — it does NOT manipulate the L2 state of other hosts.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from core.rbac_manager import ClearanceLevel, requires_clearance

logger = logging.getLogger("jarvis.network_quarantine")

_IS_WINDOWS = os.name == "nt"

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

# --- Config ------------------------------------------------------------------
_QUARANTINE_ENABLED = True
_AUTO_THRESHOLD = 9.0
_MAX_ACTIVE = 16
_RULE_PREFIX = "JARVIS-QUARANTINE"
_LOG_PATH = Path("logs/network_quarantine.jsonl")
_NAC_WEBHOOK = os.environ.get("JARVIS_NAC_WEBHOOK")        # optional NAC/switch API
_LAB_SUBNET = os.environ.get("JARVIS_LAB_SUBNET", "192.168.1.0/24")

_active: dict = {}
_lock = asyncio.Lock()


def _is_admin() -> bool:
    if not _IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _local_ips() -> set:
    ips = set()
    if _PSUTIL_OK:
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if getattr(a, "address", None):
                        ips.add(a.address.split("%")[0])
        except Exception:
            pass
    return ips


def _gateways() -> set:
    gws = set()
    try:
        net = ipaddress.ip_network(_LAB_SUBNET, strict=False)
        gws.add(str(next(net.hosts())))   # .1 convention — never block the gateway
    except Exception:
        pass
    return gws


def _is_protected(ip: str) -> bool:
    if ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        return True
    if ip in _local_ips() or ip in _gateways():
        return True
    try:
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_multicast or a.is_unspecified:
            return True
    except ValueError:
        return True  # not a valid IP — refuse
    return False


def _valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _run(cmd: list[str]):
    """argv-vector execution — no shell, so ip/rule-name content can never be
    reinterpreted as command syntax. Callers must validate ip with
    _valid_ip() before building cmd (defense in depth on top of that)."""
    try:
        p = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _audit(res: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, default=str) + "\n")
    except Exception as e:
        logger.debug("network_quarantine: audit write failed: %s", e)


async def _nac_isolate(ip: str, reason: str) -> bool:
    if not _NAC_WEBHOOK:
        return False
    loop = asyncio.get_running_loop()

    def _post():
        import urllib.request
        body = json.dumps({"action": "quarantine", "ip": ip, "reason": reason}).encode()
        req = urllib.request.Request(_NAC_WEBHOOK, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= getattr(r, "status", 200) < 300
        except Exception as e:
            logger.error("network_quarantine: NAC webhook failed: %s", e)
            return False

    return await loop.run_in_executor(None, _post)


async def _report(correlator, ip: str, reason: str, res: dict) -> None:
    event = {"source": "network_quarantine", "type": "host_quarantined",
             "severity": 8.0, "ip": ip, "reason": reason,
             "host_isolated": res.get("host_isolated"),
             "nac_isolated": res.get("nac_isolated"), "ts": time.time()}
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
    except Exception as e:
        logger.error("network_quarantine: report dispatch failed: %s", e)


@requires_clearance(ClearanceLevel.L3_Hunter)
async def quarantine(ip: str, *, reason: str = "manual", correlator=None) -> dict:
    res = {"ip": ip, "reason": reason, "ts": time.time(),
           "host_isolated": False, "nac_isolated": False, "skipped": None}
    if not _QUARANTINE_ENABLED:
        res["skipped"] = "disabled"; _audit(res); return res
    if not (_IS_WINDOWS and _is_admin()):
        res["skipped"] = "no admin / unsupported"; _audit(res); return res
    if not _valid_ip(ip):
        res["skipped"] = "invalid IP literal"; _audit(res); return res
    if _is_protected(ip):
        res["skipped"] = "protected infra/self IP"; _audit(res); return res

    async with _lock:
        if ip in _active:
            res["skipped"] = "already quarantined"; return res
        if len(_active) >= _MAX_ACTIVE:
            res["skipped"] = "max active quarantines reached"; _audit(res); return res
        loop = asyncio.get_running_loop()
        ok = True
        for c in (
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={_RULE_PREFIX}-{ip}-IN", "dir=in", "action=block", f"remoteip={ip}"],
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={_RULE_PREFIX}-{ip}-OUT", "dir=out", "action=block", f"remoteip={ip}"],
        ):
            rc, out = await loop.run_in_executor(None, _run, c)
            if rc != 0:
                ok = False
                logger.error("network_quarantine: netsh failed: %s | %s", c, out)
        res["host_isolated"] = ok
        if ok:
            _active[ip] = res["ts"]
        res["nac_isolated"] = await _nac_isolate(ip, reason)
        _audit(res)

    if res["host_isolated"]:
        logger.critical("NETWORK_QUARANTINE: isolated %s (%s)", ip, reason)
        if correlator is not None:
            await _report(correlator, ip, reason, res)
    return res


@requires_clearance(ClearanceLevel.L3_Hunter)
async def release(ip: str) -> dict:
    res = {"ip": ip, "released": False}
    if not _valid_ip(ip):
        res["error"] = "invalid IP literal"; return res
    if not (_IS_WINDOWS and _is_admin()):
        return res
    loop = asyncio.get_running_loop()
    for c in (
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={_RULE_PREFIX}-{ip}-IN"],
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={_RULE_PREFIX}-{ip}-OUT"],
    ):
        await loop.run_in_executor(None, _run, c)
    _active.pop(ip, None)
    res["released"] = True
    logger.info("network_quarantine: released %s", ip)
    return res


async def start(correlator=None) -> None:
    """main.py startup hook. JARVIS Watchdog Pattern: dormant if non-Windows or
    not elevated (netsh firewall + NAC actions require admin)."""
    if not _IS_WINDOWS:
        logger.warning("NETWORK_QUARANTINE: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("NETWORK_QUARANTINE: not elevated (admin required) — dormant")
        await asyncio.Event().wait(); return
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("NETWORK_QUARANTINE: log path unavailable (%s) — dormant", e)
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("network_quarantine", quarantine)
        except Exception:
            pass
    logger.info("NETWORK_QUARANTINE: armed — host-firewall containment%s",
                " + NAC webhook" if _NAC_WEBHOOK else "")
    await asyncio.Event().wait()
