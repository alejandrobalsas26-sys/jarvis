"""
tools/breach_simulator.py — Breach & Attack Simulation engine (v43.0).

Uses Scapy to craft realistic attack traffic for detection testing.
All synthetic traffic stays on the lab network — never external IPs.
Each simulation registers with purple_coordinator before firing.

BAS modules:
  simulate_cs_beacon()                   — Cobalt Strike HTTP beacon pattern
  simulate_dns_c2()                      — DNS tunneling C2 communication
  simulate_smb_lateral()                 — SMB lateral movement traffic
  simulate_data_exfil()                  — HTTPS exfiltration simulation
  simulate_process_injection_artifact()  — Host-side injection artifact

Requires Administrator on Windows for raw socket access.
Falls back to plain TCP connect for telemetry generation otherwise.
"""

import asyncio
import os
import random
import socket
import struct
import time
import uuid
from datetime import datetime, timezone

from loguru import logger

_DETECTION_TIMEOUT_S = 30.0

_PRIVATE_NETS = [
    ("10.0.0.0",    0xFF000000, 0x0A000000),
    ("172.16.0.0",  0xFFF00000, 0xAC100000),
    ("192.168.0.0", 0xFFFF0000, 0xC0A80000),
    ("127.0.0.0",   0xFF000000, 0x7F000000),
]


def _is_private(ip: str) -> bool:
    try:
        packed = struct.unpack("!I", socket.inet_aton(ip))[0]
        return any(
            (packed & mask) == net
            for _, mask, net in _PRIVATE_NETS
        )
    except Exception:
        return False


def _safety_check(target_ip: str) -> bool:
    if not _is_private(target_ip):
        logger.error(
            f"BAS: SAFETY BLOCK — {target_ip} is not a private IP. "
            "BAS only operates on RFC 1918 lab networks."
        )
        return False
    return True


def _socket_fallback(target_ip: str, port: int) -> bool:
    """
    Fallback when raw sockets unavailable (non-admin).
    Uses regular TCP connect — less realistic but still generates telemetry.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect((target_ip, port))
        s.close()
        return True
    except Exception:
        return False


async def simulate_cs_beacon(
    target_ip: str,
    broadcast_fn,
    jitter_pct: int = 15,
) -> bool:
    """Simulate Cobalt Strike HTTP C2 beacon traffic (T1071.001)."""
    if not _safety_check(target_ip):
        return False

    event_id  = str(uuid.uuid4())[:8]
    technique = "T1071.001"

    try:
        await broadcast_fn({
            "type":      "bas_simulation_started",
            "module":    "cs_beacon",
            "target":    target_ip,
            "technique": technique,
            "event_id":  event_id,
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    from core.purple_coordinator import register_attack_event
    await register_attack_event(
        technique, event_id, "bas_cs_beacon", broadcast_fn
    )

    loop = asyncio.get_running_loop()

    def _craft_and_send():
        try:
            from scapy.layers.inet import IP, TCP
            from scapy.all import send

            cs_uris = [
                "/jquery-3.3.1.min.js",
                "/updates.rss",
                "/load",
                "/pixel.gif",
                "/cm",
                "/submit.php",
                "/en_US/all.js",
            ]
            cookie = "__cfduid=" + "".join(random.choices(
                "abcdefghijklmnopqrstuvwxyz0123456789", k=43
            ))
            uri = random.choice(cs_uris)
            beacon_payload = (
                f"GET {uri} HTTP/1.1\r\n"
                f"Host: {target_ip}\r\n"
                f"Accept: */*\r\n"
                f"Cookie: {cookie}\r\n"
                f"User-Agent: Mozilla/5.0 (compatible; MSIE 9.0; "
                f"Windows NT 6.1; WOW64; Trident/5.0)\r\n"
                f"Connection: Keep-Alive\r\n\r\n"
            ).encode()

            raw_pkt = IP(dst=target_ip) / TCP(
                dport=80,
                sport=random.randint(49152, 65535),
                flags="PA",
            ) / beacon_payload

            send(raw_pkt, verbose=False)
            return True
        except PermissionError:
            logger.warning(
                "BAS: raw socket requires Administrator — "
                "falling back to socket simulation"
            )
            return _socket_fallback(target_ip, 80)
        except ImportError:
            logger.warning("BAS: Scapy not available for raw packets")
            return _socket_fallback(target_ip, 80)
        except Exception as e:
            logger.debug(f"BAS: cs_beacon error: {e}")
            return False

    success = await loop.run_in_executor(None, _craft_and_send)

    try:
        await broadcast_fn({
            "type":      "bas_simulation_complete",
            "module":    "cs_beacon",
            "target":    target_ip,
            "technique": technique,
            "success":   success,
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return success


async def simulate_dns_c2(
    target_ip: str,
    broadcast_fn,
    domain: str = "c2.lab.local",
) -> bool:
    """Simulate DNS tunneling C2 communication (T1071.004)."""
    if not _safety_check(target_ip):
        return False

    event_id  = str(uuid.uuid4())[:8]
    technique = "T1071.004"

    from core.purple_coordinator import register_attack_event
    await register_attack_event(
        technique, event_id, "bas_dns_c2", broadcast_fn
    )

    loop = asyncio.get_running_loop()

    def _dns_queries():
        try:
            from scapy.layers.inet import IP, UDP
            from scapy.layers.dns import DNS, DNSQR
            from scapy.all import send

            for _ in range(5):
                entropy_sub = "".join(random.choices(
                    "abcdefghijklmnopqrstuvwxyz234567", k=32
                ))
                fqdn = f"{entropy_sub}.{domain}."
                pkt = (
                    IP(dst=target_ip) /
                    UDP(dport=53) /
                    DNS(rd=1, qd=DNSQR(qname=fqdn))
                )
                send(pkt, verbose=False)
                time.sleep(0.5)
            return True
        except PermissionError:
            return _socket_fallback(target_ip, 53)
        except ImportError:
            return _socket_fallback(target_ip, 53)
        except Exception as e:
            logger.debug(f"BAS: dns_c2 error: {e}")
            return False

    success = await loop.run_in_executor(None, _dns_queries)

    try:
        await broadcast_fn({
            "type":      "bas_simulation_complete",
            "module":    "dns_c2",
            "target":    target_ip,
            "technique": technique,
            "success":   success,
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return success


async def simulate_smb_lateral(
    target_ip: str,
    broadcast_fn,
) -> bool:
    """Simulate SMB lateral movement traffic (T1021.002)."""
    if not _safety_check(target_ip):
        return False

    event_id  = str(uuid.uuid4())[:8]
    technique = "T1021.002"

    from core.purple_coordinator import register_attack_event
    await register_attack_event(
        technique, event_id, "bas_smb_lateral", broadcast_fn
    )

    loop = asyncio.get_running_loop()

    def _smb_probe():
        try:
            from scapy.layers.inet import IP, TCP
            from scapy.all import send, RandShort

            smb_negotiate = bytes([
                0x00, 0x00, 0x00, 0x85,
                0xff, 0x53, 0x4d, 0x42,
                0x72, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00,
            ])
            pkt = (
                IP(dst=target_ip) /
                TCP(dport=445, sport=RandShort(), flags="PA") /
                smb_negotiate
            )
            send(pkt, verbose=False)
            return True
        except PermissionError:
            return _socket_fallback(target_ip, 445)
        except ImportError:
            return _socket_fallback(target_ip, 445)
        except Exception as e:
            logger.debug(f"BAS: smb_lateral error: {e}")
            return False

    success = await loop.run_in_executor(None, _smb_probe)

    try:
        await broadcast_fn({
            "type":      "bas_simulation_complete",
            "module":    "smb_lateral",
            "target":    target_ip,
            "technique": technique,
            "success":   success,
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return success


async def simulate_data_exfil(
    target_ip: str,
    broadcast_fn,
    data_size_kb: int = 50,
) -> bool:
    """Simulate data exfiltration over HTTPS (T1041)."""
    if not _safety_check(target_ip):
        return False

    event_id  = str(uuid.uuid4())[:8]
    technique = "T1041"

    from core.purple_coordinator import register_attack_event
    await register_attack_event(
        technique, event_id, "bas_data_exfil", broadcast_fn
    )

    loop = asyncio.get_running_loop()

    def _exfil():
        try:
            from scapy.layers.inet import IP, TCP
            from scapy.all import send, RandShort

            chunk_size   = 1024
            total_chunks = (data_size_kb * 1024) // chunk_size
            for _ in range(min(total_chunks, 10)):
                payload = os.urandom(chunk_size)
                pkt = (
                    IP(dst=target_ip) /
                    TCP(dport=443, sport=RandShort(), flags="PA") /
                    payload
                )
                send(pkt, verbose=False)
                time.sleep(0.1)
            return True
        except PermissionError:
            return _socket_fallback(target_ip, 443)
        except ImportError:
            return _socket_fallback(target_ip, 443)
        except Exception as e:
            logger.debug(f"BAS: data_exfil error: {e}")
            return False

    success = await loop.run_in_executor(None, _exfil)

    try:
        await broadcast_fn({
            "type":         "bas_simulation_complete",
            "module":       "data_exfil",
            "target":       target_ip,
            "technique":    technique,
            "data_size_kb": data_size_kb,
            "success":      success,
            "severity":     "HIGH",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return success


async def simulate_process_injection_artifact(
    broadcast_fn,
) -> bool:
    """
    Simulate process injection artifacts on the local host (T1055.012).
    Allocates an RWX page on the current process — high-signal for ETW.
    No code is written or executed.
    """
    event_id  = str(uuid.uuid4())[:8]
    technique = "T1055.012"

    from core.purple_coordinator import register_attack_event
    await register_attack_event(
        technique, event_id, "bas_proc_inject", broadcast_fn
    )

    loop = asyncio.get_running_loop()

    def _artifact():
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.VirtualAlloc.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t,
                ctypes.c_uint32, ctypes.c_uint32,
            ]
            k32.VirtualAlloc.restype = ctypes.c_void_p
            k32.VirtualFree.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
            ]
            k32.VirtualFree.restype = ctypes.c_bool

            addr = k32.VirtualAlloc(
                None, 4096,
                0x3000,
                0x40,
            )
            if addr:
                k32.VirtualFree(addr, 0, 0x8000)
            return True
        except Exception as e:
            logger.debug(f"BAS: injection artifact error: {e}")
            return False

    success = await loop.run_in_executor(None, _artifact)

    try:
        await broadcast_fn({
            "type":      "bas_simulation_complete",
            "module":    "process_injection_artifact",
            "technique": technique,
            "success":   success,
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return success


async def run_full_bas_scenario(
    target_ip: str,
    broadcast_fn,
    scenario: str = "apt_chain",
) -> dict:
    """
    Run a complete BAS scenario against a target.
    Scenarios: apt_chain, ransomware_precursor, insider_threat
    Returns coverage results after all simulations complete.
    """
    scenarios = {
        "apt_chain": [
            ("cs_beacon",         {"target_ip": target_ip}),
            ("dns_c2",            {"target_ip": target_ip}),
            ("smb_lateral",       {"target_ip": target_ip}),
            ("process_injection", {}),
            ("data_exfil",        {"target_ip": target_ip, "data_size_kb": 100}),
        ],
        "ransomware_precursor": [
            ("cs_beacon",         {"target_ip": target_ip}),
            ("smb_lateral",       {"target_ip": target_ip}),
            ("process_injection", {}),
        ],
        "insider_threat": [
            ("data_exfil", {"target_ip": target_ip, "data_size_kb": 500}),
            ("dns_c2",     {"target_ip": target_ip}),
        ],
    }

    steps = scenarios.get(scenario, scenarios["apt_chain"])

    try:
        await broadcast_fn({
            "type":      "bas_scenario_started",
            "scenario":  scenario,
            "target":    target_ip,
            "steps":     len(steps),
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    func_map = {
        "cs_beacon":         simulate_cs_beacon,
        "dns_c2":            simulate_dns_c2,
        "smb_lateral":       simulate_smb_lateral,
        "data_exfil":        simulate_data_exfil,
        "process_injection": simulate_process_injection_artifact,
    }

    results: dict[str, bool] = {}
    for module_name, kwargs in steps:
        func = func_map.get(module_name)
        if not func:
            continue
        if module_name == "process_injection":
            ok = await func(broadcast_fn)
        else:
            ok = await func(broadcast_fn=broadcast_fn, **kwargs)
        results[module_name] = ok
        await asyncio.sleep(3)

    # Wait for purple coordinator to measure all detections
    await asyncio.sleep(_DETECTION_TIMEOUT_S + 2)

    from core.purple_coordinator import get_coverage_summary
    summary = get_coverage_summary()

    try:
        await broadcast_fn({
            "type":      "bas_scenario_complete",
            "scenario":  scenario,
            "results":   results,
            "coverage":  summary,
            "severity":  "INFO",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    return {"simulation_results": results, "coverage": summary}
