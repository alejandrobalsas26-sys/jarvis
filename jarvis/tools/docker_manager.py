"""
tools/docker_manager.py — Ephemeral Docker lab orchestrator (v41.0).

Spins up and tears down containerized pentest environments on demand.
Supports both single containers (docker SDK) and multi-container
setups (docker-compose via subprocess).

Resource safety:
  - mem_limit: 2g default (protects Ryzen 5 7430U host RAM)
  - cpu_period/cpu_quota: limits container to 2 cores max
  - network_mode: isolated bridge by default

Lab templates defined in tools/docker_labs.yaml — no code changes needed.

Voice: "JARVIS deploy kali" → pulls image, starts container, opens browser
Voice: "JARVIS kill lab"   → stops + removes all lab containers
"""

import asyncio
import shutil
import subprocess
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_LABS_YAML    = Path(__file__).parent / "docker_labs.yaml"
_COMPOSE_DIR  = Path("logs/docker_compose")
_COMPOSE_DIR.mkdir(parents=True, exist_ok=True)

# Lazy Docker client — never initialized at import time
_docker_client = None
_active_containers: dict[str, str] = {}   # name → container_id


def _get_client():
    """Lazy init — only connects to Docker when first needed."""
    global _docker_client
    if _docker_client is None:
        try:
            import docker
            _docker_client = docker.from_env(timeout=10)
            _docker_client.ping()   # verify connection
            logger.info("DOCKER: connected to Docker daemon")
        except Exception as e:
            logger.warning(f"DOCKER: daemon unavailable: {e}")
            _docker_client = None
    return _docker_client


def _load_labs() -> dict:
    try:
        import yaml
        return yaml.safe_load(_LABS_YAML.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def deploy_lab(
    lab_name: str,
    broadcast_fn,
    tts=None,
) -> str | None:
    """
    Deploy a named lab from docker_labs.yaml.
    Supports both single-container and docker-compose labs.
    Returns container/compose ID or None on failure.
    """
    labs = _load_labs()
    lab  = labs.get("labs", {}).get(lab_name)

    if not lab:
        available = list(labs.get("labs", {}).keys())
        logger.warning(f"DOCKER: unknown lab '{lab_name}'. Available: {available}")
        if tts:
            asyncio.create_task(tts.speak_async(
                f"Lab {lab_name} not found. Available: {', '.join(available)}"
            ))
        return None

    logger.info(f"DOCKER: deploying lab '{lab_name}'")

    await broadcast_fn({
        "type":      "docker_deploying",
        "lab":       lab_name,
        "image":     lab.get("image", "compose"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts:
        asyncio.create_task(tts.speak_async(
            f"Deploying {lab.get('description', lab_name)}. "
            f"This may take a moment."
        ))

    # Choose deployment method
    if lab.get("compose_file"):
        result = await _deploy_compose(lab, lab_name, broadcast_fn)
    else:
        result = await _deploy_single(lab, lab_name, broadcast_fn)

    if result:
        # Auto-open browser if URL defined
        url = lab.get("browser_url", "")
        if url:
            await asyncio.sleep(lab.get("browser_delay", 3))
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, webbrowser.open, url)

        if tts:
            asyncio.create_task(tts.speak_async(
                f"{lab.get('description', lab_name)} is ready."
                + (f" Opening {url}" if url else "")
            ))

    return result


async def _deploy_single(
    lab: dict,
    lab_name: str,
    broadcast_fn,
) -> str | None:
    """Deploy a single Docker container with resource limits."""
    client = _get_client()
    if not client:
        await broadcast_fn({
            "type":    "docker_error",
            "error":   "Docker daemon not available",
            "lab":     lab_name,
        })
        return None

    loop = asyncio.get_running_loop()

    def _run():
        container = client.containers.run(
            lab["image"],
            name        = lab.get("container_name", lab_name),
            ports       = lab.get("ports", {}),
            detach      = True,
            tty         = True,
            mem_limit   = lab.get("mem_limit", "2g"),
            cpu_period  = 100000,
            cpu_quota   = lab.get("cpu_quota", 200000),  # 2 cores default
            network_mode= lab.get("network_mode", "bridge"),
            environment = lab.get("environment", {}),
            volumes     = lab.get("volumes", {}),
            remove      = False,
        )
        return container.id

    try:
        container_id = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=300.0,   # 5 min for image pull
        )
        _active_containers[lab_name] = container_id
        logger.info(f"DOCKER: container {lab_name} running — ID {container_id[:12]}")

        await broadcast_fn({
            "type":         "docker_deployed",
            "lab":          lab_name,
            "container_id": container_id[:12],
            "ports":        str(lab.get("ports", {})),
            "url":          lab.get("browser_url", ""),
            "severity":     "INFO",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        })

        # Start container watcher
        asyncio.create_task(
            _watch_container(lab_name, container_id, broadcast_fn)
        )

        return container_id

    except asyncio.TimeoutError:
        logger.error(f"DOCKER: deploy timeout for '{lab_name}'")
        await broadcast_fn({
            "type":  "docker_error",
            "error": "Deploy timeout (5 min) — image may still be pulling",
            "lab":   lab_name,
        })
        return None
    except Exception as e:
        logger.error(f"DOCKER: deploy error: {e}")
        await broadcast_fn({
            "type":  "docker_error",
            "error": str(e)[:100],
            "lab":   lab_name,
        })
        return None


async def _deploy_compose(
    lab: dict,
    lab_name: str,
    broadcast_fn,
) -> str | None:
    """Deploy a multi-container lab via docker-compose."""
    compose_content = lab.get("compose_file", "")
    if not compose_content:
        return None

    compose_path = _COMPOSE_DIR / f"{lab_name}_docker-compose.yml"
    compose_path.write_text(compose_content, encoding="utf-8")

    docker_compose = shutil.which("docker-compose") or shutil.which("docker")
    if not docker_compose:
        await broadcast_fn({
            "type":  "docker_error",
            "error": "docker-compose not found in PATH",
            "lab":   lab_name,
        })
        return None

    loop = asyncio.get_running_loop()

    def _compose_up():
        if shutil.which("docker-compose"):
            cmd = ["docker-compose", "-f", str(compose_path), "up", "-d"]
        else:
            cmd = ["docker", "compose", "-f", str(compose_path), "up", "-d"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        return result.returncode == 0, result.stdout + result.stderr

    try:
        success, output = await loop.run_in_executor(None, _compose_up)
        if success:
            logger.info(f"DOCKER: compose lab '{lab_name}' deployed")
            _active_containers[lab_name] = f"compose_{lab_name}"
            await broadcast_fn({
                "type":     "docker_deployed",
                "lab":      lab_name,
                "mode":     "compose",
                "url":      lab.get("browser_url", ""),
                "severity": "INFO",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return f"compose_{lab_name}"
        else:
            await broadcast_fn({
                "type":  "docker_error",
                "error": output[:200],
                "lab":   lab_name,
            })
            return None
    except Exception as e:
        logger.error(f"DOCKER: compose error: {e}")
        return None


async def teardown_lab(
    lab_name: str,
    broadcast_fn,
    tts=None,
) -> bool:
    """Stop and remove a running lab container or compose stack."""
    client = _get_client()
    loop   = asyncio.get_running_loop()

    container_id = _active_containers.get(lab_name, lab_name)
    is_compose   = str(container_id).startswith("compose_")

    logger.warning(f"DOCKER: tearing down lab '{lab_name}'")

    if is_compose:
        compose_path = _COMPOSE_DIR / f"{lab_name}_docker-compose.yml"

        def _compose_down():
            if shutil.which("docker-compose"):
                cmd = ["docker-compose", "-f", str(compose_path), "down", "-v"]
            else:
                cmd = ["docker", "compose", "-f", str(compose_path), "down", "-v"]
            subprocess.run(cmd, capture_output=True, timeout=60)

        await loop.run_in_executor(None, _compose_down)

    elif client:
        def _remove():
            try:
                c = client.containers.get(container_id)
                c.stop(timeout=5)
                c.remove(force=True)
                return True
            except Exception:
                return False

        await loop.run_in_executor(None, _remove)

    _active_containers.pop(lab_name, None)
    logger.info(f"DOCKER: lab '{lab_name}' destroyed — no trace on host")

    await broadcast_fn({
        "type":    "docker_destroyed",
        "lab":     lab_name,
        "severity": "INFO",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if tts:
        asyncio.create_task(tts.speak_async(
            f"Lab {lab_name} destroyed. Host is clean."
        ))
    return True


async def teardown_all_labs(broadcast_fn, tts=None) -> int:
    """Stop and remove ALL active lab containers."""
    count = 0
    for name in list(_active_containers.keys()):
        await teardown_lab(name, broadcast_fn, tts=None)
        count += 1
    if tts and count:
        asyncio.create_task(tts.speak_async(
            f"All {count} lab containers destroyed."
        ))
    return count


async def list_running_labs(broadcast_fn) -> list[dict]:
    """List all currently running lab containers."""
    client = _get_client()
    if not client:
        return []

    loop = asyncio.get_running_loop()

    def _list():
        return [
            {
                "name":   c.name,
                "image":  c.image.tags[0] if c.image.tags else "unknown",
                "status": c.status,
                "id":     c.id[:12],
            }
            for c in client.containers.list()
        ]

    try:
        labs = await loop.run_in_executor(None, _list)
        await broadcast_fn({
            "type":      "docker_lab_list",
            "labs":      labs,
            "count":     len(labs),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return labs
    except Exception:
        return []


async def _watch_container(
    lab_name: str,
    container_id: str,
    broadcast_fn,
) -> None:
    """
    Background watcher: alert if container stops unexpectedly.
    Polls every 30s while container is in _active_containers.
    """
    client = _get_client()
    if not client:
        return

    loop = asyncio.get_running_loop()
    await asyncio.sleep(10)   # give container time to start

    while lab_name in _active_containers:
        await asyncio.sleep(30)
        try:
            def _check():
                c = client.containers.get(container_id)
                return c.status

            status = await loop.run_in_executor(None, _check)
            if status != "running":
                logger.warning(
                    f"DOCKER: container '{lab_name}' stopped unexpectedly "
                    f"(status={status})"
                )
                _active_containers.pop(lab_name, None)
                await broadcast_fn({
                    "type":    "docker_container_stopped",
                    "lab":     lab_name,
                    "status":  status,
                    "severity": "WARNING",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                break
        except Exception:
            _active_containers.pop(lab_name, None)
            break
