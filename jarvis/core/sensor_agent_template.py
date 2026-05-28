#!/usr/bin/env python3
"""
core/sensor_agent_template.py — JARVIS sensor micro-agent template (v42.0).

This file is uploaded to lab VMs via SSH. Placeholders __JARVIS_IP__ and
__JARVIS_PORT__ are substituted by sensor_mesh._generate_agent_script
before deployment. Once running, the agent connects back to the main
JARVIS WebSocket server and streams process creation + new network
connection events. Read-only by default.
"""

import asyncio
import json
import platform
import socket
import time
import uuid

import psutil
import websockets

JARVIS_IP   = "__JARVIS_IP__"
JARVIS_PORT = __JARVIS_PORT__
AGENT_ID    = str(uuid.uuid4())[:8]
HOSTNAME    = socket.gethostname()
try:
    HOST_IP = socket.gethostbyname(HOSTNAME)
except Exception:
    HOST_IP = "127.0.0.1"
OS_INFO     = platform.system() + " " + platform.release()

SUSPICIOUS_PROCS = {"mimikatz", "meterpreter", "nc", "ncat",
                    "netcat", "powershell", "cmd"}


async def monitor(ws):
    seen_pids  = set(p.pid for p in psutil.process_iter())
    seen_conns = set()
    while True:
        await asyncio.sleep(5)
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                if proc.pid not in seen_pids:
                    seen_pids.add(proc.pid)
                    name = (proc.info.get("name") or "").lower()
                    sev = "HIGH" if any(s in name for s in SUSPICIOUS_PROCS) else "INFO"
                    await ws.send(json.dumps({
                        "type":      "sysmon_event",
                        "event_id":  1,
                        "process":   proc.info.get("name", ""),
                        "pid":       proc.pid,
                        "severity":  sev,
                        "timestamp": time.time(),
                    }))
            for conn in psutil.net_connections():
                key = (conn.laddr, conn.raddr, conn.status)
                if key not in seen_conns and conn.raddr:
                    seen_conns.add(key)
                    await ws.send(json.dumps({
                        "type":        "dpi_alert",
                        "src_ip":      conn.laddr.ip if conn.laddr else "",
                        "dst_port":    conn.raddr.port if conn.raddr else 0,
                        "attacker_ip": conn.raddr.ip if conn.raddr else "",
                        "severity":    "INFO",
                        "timestamp":   time.time(),
                    }))
        except Exception:
            pass


async def main():
    uri = f"ws://{JARVIS_IP}:{JARVIS_PORT}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "agent_id": AGENT_ID,
                    "ip":       HOST_IP,
                    "hostname": HOSTNAME,
                    "os":       OS_INFO,
                }))
                await monitor(ws)
        except Exception:
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
