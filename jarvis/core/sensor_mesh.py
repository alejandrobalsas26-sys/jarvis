"""
core/sensor_mesh.py — Distributed lab sensor mesh orchestrator (v42.0).

Stages JARVIS micro-agents onto lab VMs via paramiko SSH.

V69 M61.7 — deployment is NOT automatic. Host-key verification is fail-closed
(``core.ssh_policy``: RejectPolicy, no trust-on-first-use), and by default this module
mutates no remote host at all: ``deploy_sensor_to_vm`` returns an operator action plan.
With ``JARVIS_SENSOR_DEPLOY=true`` *and* trusted-lab mode it will stage the agent file
to a unique, private, ``0600`` home-relative path — and still never installs a package
and never executes anything on the target. Starting the agent is the operator's action.

Each agent is a self-contained 80-line Python script that:
  - Monitors process creation (psutil)
  - Monitors new network connections
  - Monitors file changes in sensitive directories
  - Reports everything via WebSocket to main JARVIS

Main JARVIS runs a WebSocket server on port 9999 for agent connections.
Agents connect automatically and stream telemetry.
All connected agents appear as live nodes in the AURA 3D scene.

Voice: "JARVIS deploy sensor to 192.168.1.100" → SSH host-key check → stage (opt-in)
or refuse with a plan (default) → the operator starts the agent → stream.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_SENSOR_PORT = 9999   # WebSocket server for incoming agent connections
from core import ssh_policy
from core.managed_paths import logs_subdir


def _sensor_dir() -> Path:
    """Managed sensor-mesh artifact directory (V69 M61 RC1), created on first write."""
    return logs_subdir("sensor_mesh")

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

def _deployment_plan(host_ip: str, ssh_user: str, staged_path: str | None) -> dict:
    """The operator action plan returned instead of mutating a remote host."""
    target = staged_path or "<staged path — run with staging enabled to obtain it>"
    return {
        "target_ip": host_ip,
        "ssh_user": ssh_user,
        "staged_path": staged_path,
        "jarvis_listener": f"127.0.0.1:{_SENSOR_PORT}",
        "operator_action": [
            f"1. Confirm {host_ip} is a machine you own and have authorization to change.",
            "2. Enroll its SSH host key once, after verifying the fingerprint out of "
            "band: core.ssh_policy.enrollment_plan(host) then enroll_verified_host(...).",
            "3. Install the agent's dependencies YOURSELF on the target: "
            "python3 -m pip install --user psutil websockets",
            f"4. Start the staged agent yourself: python3 {target}",
            "5. JARVIS listens on loopback only; expose a tunnel if the VM is remote.",
        ],
        "why_not_automatic": (
            "Automatic deployment would (a) install packages on a machine JARVIS does "
            "not own, and (b) execute uploaded code there. Both are unreviewable host "
            "mutations, so they are the operator's action, not the agent's."
        ),
    }


async def deploy_sensor_to_vm(
    host_ip: str,
    ssh_user: str,
    ssh_key_path: str,
    broadcast_fn,
    tts=None,
) -> bool:
    """Stage the JARVIS micro-agent onto a VM. **Does not execute it.**

    V69 M61.7 — this function used to do four things the release gate correctly
    refuses (Bandit B507 HIGH, B108, B601):

      * ``paramiko.AutoAddPolicy()`` — trust-on-first-use, so whatever answered on
        ``host_ip`` first became permanently trusted, with the operator's private key
        doing the authenticating (B507);
      * upload to a fixed, shared, world-writable ``/tmp/jarvis_sensor.py`` — any
        local user on the target could pre-create that name as a symlink to redirect
        the write, or swap the contents between upload and execution (B108);
      * ``pip install psutil websockets`` on the remote host — automatic dependency
        installation on a machine JARVIS does not own;
      * ``nohup python3 /tmp/jarvis_sensor.py &`` — remote execution of uploaded code
        (B601), started from a path an attacker could have replaced.

    What it does now: host-key verification is fail-closed (``core.ssh_policy``), and
    the *default* is to mutate nothing at all and return an operator action plan.
    Staging is opt-in via ``JARVIS_SENSOR_DEPLOY=true`` **and** trusted-lab mode; even
    then JARVIS only writes the script to a unique, private, home-relative path with
    ``0600`` permissions, and never installs or starts anything. Returns ``False``
    when nothing was staged.
    """
    if not ssh_policy.remote_staging_enabled():
        plan = _deployment_plan(host_ip, ssh_user, None)
        logger.warning(
            f"SENSOR_MESH: automated deployment to {host_ip} is DISABLED by default "
            f"(set JARVIS_SENSOR_DEPLOY=true with trusted-lab mode to stage the agent "
            f"file; JARVIS never installs or starts it). Returning an operator plan."
        )
        await broadcast_fn({
            "type":      "sensor_deploy_requires_operator",
            "target_ip": host_ip,
            "severity":  "WARNING",
            "plan":      plan,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if tts:
            asyncio.create_task(tts.speak_async(
                f"Automated sensor deployment to {host_ip} is disabled. "
                f"An operator action plan is on the console."
            ))
        return False

    logger.info(f"SENSOR_MESH: staging agent file to {host_ip} (no remote execution)")

    loop = asyncio.get_running_loop()

    def _stage() -> tuple[bool, str | None, str | None]:
        """Returns ``(staged, remote_path, error)``. Blocking — runs in executor."""
        client = None
        sftp = None
        try:
            import paramiko

            # The IP of the JARVIS host as seen from the VM (usually the hypervisor
            # gateway). Config only — never a value from a model or a remote host.
            jarvis_ip = os.getenv("JARVIS_HOST_IP", "192.168.1.1")
            agent_code = _generate_agent_script(jarvis_ip, _SENSOR_PORT)

            client = paramiko.SSHClient()
            ssh_policy.harden_client(client)
            ssh_policy.connect_verified(
                client, host_ip, username=ssh_user, key_path=ssh_key_path, timeout=15
            )

            sftp = client.open_sftp()
            remote_path = ssh_policy.remote_staging_path(sftp, "sensor", ".py")
            with sftp.open(remote_path, "w") as handle:
                handle.write(agent_code)
            # Owner-only, and no execute bit: JARVIS does not start this file.
            sftp.chmod(remote_path, ssh_policy.REMOTE_STAGING_MODE)
            return True, remote_path, None

        except ssh_policy.HostKeyVerificationError as exc:
            return False, None, str(exc)
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"
        finally:
            for resource in (sftp, client):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    staged, remote_path, error = await loop.run_in_executor(None, _stage)

    if not staged:
        logger.warning(f"SENSOR_MESH: staging to {host_ip} failed — {error}")

    await broadcast_fn({
        "type":      "sensor_staged" if staged else "sensor_deploy_failed",
        "target_ip": host_ip,
        "severity":  "INFO" if staged else "WARNING",
        "staged_path": remote_path,
        "error":     error,
        "plan":      _deployment_plan(host_ip, ssh_user, remote_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts:
        msg = (
            f"Sensor agent staged on {host_ip}. Start it manually — "
            f"JARVIS does not execute remote code."
            if staged else
            f"Failed to stage sensor on {host_ip}."
        )
        asyncio.create_task(tts.speak_async(msg))

    return staged


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
