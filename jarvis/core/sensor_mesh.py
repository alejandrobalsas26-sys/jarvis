"""
core/sensor_mesh.py — Distributed lab sensor mesh orchestrator (v42.0).

Deploys JARVIS micro-agents to lab VMs via paramiko SSH.
Each agent is a self-contained 80-line Python script that:
  - Monitors process creation (psutil)
  - Monitors new network connections
  - Monitors file changes in sensitive directories
  - Reports everything via WebSocket to main JARVIS

Main JARVIS runs a WebSocket server on port 9999 for agent connections.
Agents connect automatically and stream telemetry.
All connected agents appear as live nodes in the AURA 3D scene.

Voice: "JARVIS deploy sensor to 192.168.1.100" → SSH → deploy → stream
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_SENSOR_PORT = 9999   # WebSocket server for incoming agent connections
_SENSOR_DIR  = Path("logs/sensor_mesh")
_SENSOR_DIR.mkdir(parents=True, exist_ok=True)

# Connected agents: {agent_id: {ip, hostname, os, ws_connection}}
_connected_agents: dict[str, dict] = {}


# ── WebSocket server for agent connections ────────────────────────────────────

async def start_sensor_server(broadcast_fn) -> None:
    """
    WebSocket server that agents connect to.
    Listens on 127.0.0.1:9999 (localhost only — agents SSH-tunnel to this).
    """
    import websockets

    async def _handle_agent(websocket, *_args):
        agent_id = None
        try:
            # First message is agent registration
            reg_raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            reg     = json.loads(reg_raw)
            agent_id = reg.get("agent_id", "unknown")

            _connected_agents[agent_id] = {
                "ip":         reg.get("ip", ""),
                "hostname":   reg.get("hostname", ""),
                "os":         reg.get("os", ""),
                "connected":  datetime.now(timezone.utc).isoformat(),
                "events_received": 0,
                "last_event_at": None,
                # Trust inputs (M41). Reported as declared, never assumed verified: the
                # transport is localhost SSH-tunnel; a signature is only present if the
                # agent actually sent one.
                "transport":  "localhost-tunnel",
                "signed":     bool(reg.get("signed")),
                "capabilities": sorted(reg.get("capabilities", []))
                                if isinstance(reg.get("capabilities"), list) else [],
            }

            logger.info(
                f"SENSOR_MESH: agent connected — "
                f"{reg.get('hostname','')} ({reg.get('ip','')})"
            )

            await broadcast_fn({
                "type":      "sensor_connected",
                "agent_id":  agent_id,
                "ip":        reg.get("ip", ""),
                "hostname":  reg.get("hostname", ""),
                "os":        reg.get("os", ""),
                "severity":  "INFO",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            # Stream agent events
            async for message in websocket:
                try:
                    event = json.loads(message)
                except Exception:
                    continue
                _connected_agents[agent_id]["events_received"] += 1
                _connected_agents[agent_id]["last_event_at"] = \
                    datetime.now(timezone.utc).isoformat()
                # M39/M41: feed the telemetry engine so sensor freshness/rate/lag and the
                # derived sensor-health state are computed by the same bounded machinery.
                try:
                    from core.telemetry_intel import telemetry
                    telemetry.record(f"sensor:{agent_id}", event=event)
                except Exception:  # noqa: BLE001 — observability, never load-bearing
                    pass

                # Add agent context to event
                event["agent_id"]   = agent_id
                event["agent_ip"]   = reg.get("ip", "")
                event["agent_host"] = reg.get("hostname", "")

                # Broadcast high-severity events to JARVIS pipeline
                if event.get("severity") in ("HIGH", "CRITICAL"):
                    await broadcast_fn(event)

                # Feed into correlator
                try:
                    from core.correlator import correlator
                    asyncio.create_task(correlator.ingest(event))
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"SENSOR_MESH: agent {agent_id} disconnected: {e}")
        finally:
            if agent_id and agent_id in _connected_agents:
                _connected_agents.pop(agent_id, None)
                await broadcast_fn({
                    "type":      "sensor_disconnected",
                    "agent_id":  agent_id,
                    "severity":  "WARNING",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    server = await websockets.serve(
        _handle_agent, "127.0.0.1", _SENSOR_PORT
    )
    logger.info(
        f"SENSOR_MESH: WebSocket server listening on "
        f"127.0.0.1:{_SENSOR_PORT}"
    )
    await server.wait_closed()


# ── SSH agent deployment ──────────────────────────────────────────────────────

async def deploy_sensor_to_vm(
    host_ip: str,
    ssh_user: str,
    ssh_key_path: str,
    broadcast_fn,
    tts=None,
) -> bool:
    """
    Deploy JARVIS micro-agent to a VM via SSH.
    Agent auto-connects back to main JARVIS sensor server.
    Requires paramiko (installed in v26).
    """
    logger.info(f"SENSOR_MESH: deploying agent to {host_ip}")

    loop = asyncio.get_running_loop()

    def _deploy():
        try:
            import paramiko

            # Get host IP for agent callback (the IP of the JARVIS host
            # as seen from the VM — usually the VMware gateway)
            jarvis_ip = os.getenv(
                "JARVIS_HOST_IP", "192.168.1.1"
            )

            # Generate agent script with embedded connection details
            agent_code = _generate_agent_script(jarvis_ip, _SENSOR_PORT)

            # SSH connect
            key = paramiko.RSAKey.from_private_key_file(ssh_key_path)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host_ip, username=ssh_user, pkey=key, timeout=15)

            # Upload agent script
            sftp = client.open_sftp()
            with sftp.open("/tmp/jarvis_sensor.py", "w") as f:
                f.write(agent_code)
            sftp.close()

            # Install psutil if needed and start agent
            client.exec_command(
                "pip install psutil websockets --quiet 2>/dev/null; "
                "nohup python3 /tmp/jarvis_sensor.py &"
            )
            client.close()
            return True

        except Exception as e:
            logger.error(f"SENSOR_MESH: deploy failed: {e}")
            return False

    success = await loop.run_in_executor(None, _deploy)

    await broadcast_fn({
        "type":      "sensor_deployed" if success else "sensor_deploy_failed",
        "target_ip": host_ip,
        "severity":  "INFO" if success else "WARNING",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts:
        msg = (
            f"Sensor deployed to {host_ip}. Awaiting connection."
            if success else
            f"Failed to deploy sensor to {host_ip}."
        )
        asyncio.create_task(tts.speak_async(msg))

    return success


def _generate_agent_script(jarvis_ip: str, jarvis_port: int) -> str:
    """
    Generate the micro-agent Python script.
    Self-contained, no external files needed.
    Reads the template from sensor_agent_template.py and injects config.
    """
    try:
        template = Path(__file__).parent / "sensor_agent_template.py"
        code     = template.read_text(encoding="utf-8")
        code     = code.replace("__JARVIS_IP__", jarvis_ip)
        code     = code.replace("__JARVIS_PORT__", str(jarvis_port))
        return code
    except Exception:
        return _FALLBACK_AGENT.replace(
            "__JARVIS_IP__", jarvis_ip
        ).replace("__JARVIS_PORT__", str(jarvis_port))


_FALLBACK_AGENT = '''#!/usr/bin/env python3
import asyncio, json, platform, socket, time, uuid
import psutil, websockets

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
            for proc in psutil.process_iter(["pid","name","cmdline"]):
                if proc.pid not in seen_pids:
                    seen_pids.add(proc.pid)
                    name = (proc.info.get("name") or "").lower()
                    sev = "HIGH" if any(s in name for s in SUSPICIOUS_PROCS) else "INFO"
                    await ws.send(json.dumps({
                        "type": "sysmon_event", "event_id": 1,
                        "process": proc.info.get("name",""),
                        "pid": proc.pid, "severity": sev,
                        "timestamp": time.time(),
                    }))
            for conn in psutil.net_connections():
                key = (conn.laddr, conn.raddr, conn.status)
                if key not in seen_conns and conn.raddr:
                    seen_conns.add(key)
                    await ws.send(json.dumps({
                        "type": "dpi_alert",
                        "src_ip": conn.laddr.ip if conn.laddr else "",
                        "dst_port": conn.raddr.port if conn.raddr else 0,
                        "attacker_ip": conn.raddr.ip if conn.raddr else "",
                        "severity": "INFO", "timestamp": time.time(),
                    }))
        except Exception:
            pass

async def main():
    uri = f"ws://{JARVIS_IP}:{JARVIS_PORT}"
    while True:
        try:
            async with websockets.connect(uri, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "agent_id": AGENT_ID, "ip": HOST_IP,
                    "hostname": HOSTNAME, "os": OS_INFO,
                }))
                await monitor(ws)
        except Exception:
            await asyncio.sleep(10)

asyncio.run(main())
'''

def get_connected_agents() -> list[dict]:
    return [{"agent_id": k, **v} for k, v in _connected_agents.items()]
