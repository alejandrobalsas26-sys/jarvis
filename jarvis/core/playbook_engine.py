"""
core/playbook_engine.py — Deterministic SOAR Playbook Engine (v28.0).

Executes YAML-defined incident response playbooks in sub-milliseconds.
Runs BEFORE the agentic loop for known incident patterns.
Supports hot-reload via watchdog file monitoring.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except ImportError:
    Observer = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore
    _WATCHDOG_OK = False

PLAYBOOKS_DIR = Path(__file__).parent / "playbooks"


# ── Playbook dataclass ────────────────────────────────────────────────────────

class Playbook:
    def __init__(self, data: dict, path: Path):
        self.name           = data["name"]
        self.description    = data.get("description", "")
        self.auto_authorize = data.get("auto_authorize", False)
        self.trigger        = data["trigger"]
        self.steps          = data["steps"]
        self.path           = path

    def matches(self, incident: dict) -> bool:
        t = self.trigger
        if t.get("incident_type") and t["incident_type"] != incident.get("rule"):
            return False
        if incident.get("severity_score", 0) < t.get("severity_min", 0):
            return False
        if t.get("mitre_any"):
            inc_techniques = set(incident.get("mitre_techniques", []))
            if not inc_techniques & set(t["mitre_any"]):
                return False
        return True


# ── Variable interpolation ────────────────────────────────────────────────────

_INTERP_RE = re.compile(r"\{\{(.*?)\}\}")


def _interpolate(value: str, incident: dict, config: dict) -> str:
    """Replace {{variable}} placeholders in step params."""
    def replacer(match):
        path = match.group(1).strip()
        parts = path.split(".")
        obj: Any = {"incident": incident, "config": config}
        cur: Any = obj
        for part in parts:
            if part == "first":
                if isinstance(cur, (list, set, tuple)):
                    cur = next(iter(cur), "")
                elif isinstance(cur, dict):
                    cur = next(iter(cur.values()), "")
                else:
                    cur = ""
            elif isinstance(cur, dict):
                cur = cur.get(part, "")
            elif isinstance(cur, list) and part.isdigit():
                idx = int(part)
                cur = cur[idx] if idx < len(cur) else ""
            else:
                cur = ""
        return str(cur)
    return _INTERP_RE.sub(replacer, str(value))


def _interpolate_params(params: dict, incident: dict, config: dict) -> dict:
    return {
        k: _interpolate(v, incident, config) if isinstance(v, str) else v
        for k, v in params.items()
    }


# ── Step executor ─────────────────────────────────────────────────────────────

async def _execute_step(
    step: dict,
    incident: dict,
    config: dict,
    broadcast_fn,
    tool_executor,
) -> dict:
    """Execute a single playbook step. Returns result dict."""
    action = step.get("action", "")
    params = _interpolate_params(step.get("params", {}), incident, config)
    result = {"action": action, "status": "ok", "params": params}

    try:
        if action == "broadcast_alert":
            await broadcast_fn({
                "type":        "playbook_alert",
                "message":     params.get("message", ""),
                "severity":    params.get("severity", "INFO"),
                "incident_id": incident.get("incident_id", ""),
            })

        elif action == "isolate_ip":
            from core.mitigation import isolate_ip
            ip  = params.get("ip", "")
            ttl = int(params.get("ttl_minutes", 60))
            if ip:
                asyncio.create_task(
                    asyncio.shield(isolate_ip(ip, broadcast_fn, ttl))
                )

        elif action == "snapshot_vm":
            from tools.forensic_volatility import trigger_forensic_capture
            vmx = params.get("vmx", "")
            if vmx:
                asyncio.create_task(
                    trigger_forensic_capture(vmx, broadcast_fn)
                )

        elif action == "run_volatility":
            from tools.forensic_volatility import trigger_forensic_capture
            vmx = config.get("VOLATILITY_TARGET_VMX", "")
            if vmx:
                asyncio.create_task(
                    trigger_forensic_capture(vmx, broadcast_fn)
                )

        elif action == "run_nmap":
            target = params.get("target", "")
            if target and tool_executor:
                asyncio.create_task(
                    tool_executor.execute(
                        tool_name="network_scan",
                        tool_input={"target": target, "scan_type": "-sV --top-ports 100"},
                        reasoning=f"Playbook-triggered scan: {incident.get('incident_id')}",
                    )
                )

        elif action == "run_binary_inversion":
            note = params.get("note", "")
            await broadcast_fn({
                "type":        "playbook_note",
                "message":     f"Binary inversion queued: {note}",
                "incident_id": incident.get("incident_id", ""),
            })

        elif action == "store_episode":
            from core.episodic_memory import store_episode
            asyncio.create_task(store_episode(
                params.get("content", str(incident)),
                "playbook_execution",
                severity=params.get("severity", "HIGH"),
                mitre_tags=incident.get("mitre_techniques", []),
            ))

        else:
            result["status"] = "unknown_action"
            result["error"]  = f"No handler for action '{action}'"

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)
        logger.error(f"PLAYBOOK: step '{action}' failed: {e}")

    return result


# ── Playbook engine ───────────────────────────────────────────────────────────

class PlaybookEngine:
    def __init__(self):
        self._playbooks:     list[Playbook]    = []
        self._broadcast_fn                     = None
        self._tool_executor                    = None
        self._config:        dict              = {}
        self._observer                         = None

    def attach(self, broadcast_fn, tool_executor, config: dict | None = None) -> None:
        self._broadcast_fn  = broadcast_fn
        self._tool_executor = tool_executor
        self._config        = config or {}

    def load_playbooks(self) -> None:
        PLAYBOOKS_DIR.mkdir(exist_ok=True)
        self._playbooks = []
        for path in sorted(PLAYBOOKS_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self._playbooks.append(Playbook(data, path))
                logger.info(f"PLAYBOOK: loaded '{data['name']}' from {path.name}")
            except Exception as e:
                logger.error(f"PLAYBOOK: failed to load {path.name}: {e}")

    def start_hot_reload(self) -> None:
        """Watch playbooks directory for changes and reload automatically."""
        if not _WATCHDOG_OK:
            logger.warning("PLAYBOOK: watchdog not installed — hot-reload disabled")
            return

        engine = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                src = getattr(event, "src_path", "") or ""
                if src.endswith(".yaml"):
                    logger.info("PLAYBOOK: file change detected — reloading")
                    engine.load_playbooks()

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), str(PLAYBOOKS_DIR), recursive=False)
            self._observer.start()
        except Exception as e:
            logger.warning(f"PLAYBOOK: hot-reload init failed: {e}")
            self._observer = None

    async def evaluate(self, incident: dict) -> None:
        """
        Evaluate all playbooks against a compound incident.
        Fires matching playbooks concurrently.
        Called by correlator BEFORE agentic loop.
        """
        if self._broadcast_fn is None:
            return

        matching = [pb for pb in self._playbooks if pb.matches(incident)]
        if not matching:
            return

        logger.info(
            f"PLAYBOOK: {len(matching)} playbook(s) matched "
            f"incident {incident.get('incident_id')} "
            f"(severity={incident.get('severity_score', 0):.1f})"
        )

        await asyncio.gather(*[
            self._run_playbook(pb, incident)
            for pb in matching
        ], return_exceptions=True)

    async def _run_playbook(self, pb: Playbook, incident: dict) -> None:
        start = time.monotonic()
        results = []

        await self._broadcast_fn({
            "type":        "playbook_started",
            "playbook":    pb.name,
            "incident_id": incident.get("incident_id", ""),
            "step_count":  len(pb.steps),
        })

        for step in pb.steps:
            result = await _execute_step(
                step, incident, self._config,
                self._broadcast_fn, self._tool_executor,
            )
            results.append(result)

        elapsed = round((time.monotonic() - start) * 1000, 1)

        await self._broadcast_fn({
            "type":        "playbook_completed",
            "playbook":    pb.name,
            "incident_id": incident.get("incident_id", ""),
            "elapsed_ms":  elapsed,
            "steps_ok":    sum(1 for r in results if r["status"] == "ok"),
            "steps_err":   sum(1 for r in results if r["status"] == "error"),
        })

        logger.info(f"PLAYBOOK: '{pb.name}' completed in {elapsed}ms")


# Module singleton
playbook_engine = PlaybookEngine()
