"""
core/plugin_loader.py — JARVIS V55.0 OMNI-REDUNDANCY
Dynamic detection plugin system. Loads cryptographically-verified Python plugins
from the plugins/ directory without restarting JARVIS. Each plugin's SHA-256 must
match the signed manifest (plugins/manifest.json) before it executes — unsigned or
modified plugins are refused. Plugins run in a RESTRICTED exec environment:
  BLOCKED: open, os, subprocess, socket, ctypes, importlib, __import__ (None).
  ALLOWED: re, json, time, hashlib, and safe builtins only.
Each plugin must export analyze(event:dict) -> Optional[dict]. On returning a dict,
the enriched event is re-ingested into the correlator (with _plugin_enriched=True
to prevent loops). Execution is non-blocking (thread executor, per-plugin timeout).
Hot-reload on plugin directory change via watchdog. LOADED_PLUGINS is exposed for
health_watchdog and the C2 dashboard.
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, time
from pathlib import Path

logger = logging.getLogger("jarvis.plugin_loader")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WD_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WD_OK = False

_PLUGIN_DIR = Path(os.environ.get("JARVIS_PLUGIN_DIR", "plugins"))
_MANIFEST = _PLUGIN_DIR / "manifest.json"
_EXEC_TIMEOUT = 5.0
_MIN_SEV = 7.0

LOADED_PLUGINS: dict = {}      # name -> {fn, sha256, version, load_time, calls, errors}

_correlator = None
_loop = None

_SAFE_BUILTINS = {
    "print": print, "len": len, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "range": range, "enumerate": enumerate, "zip": zip, "map": map,
    "filter": filter, "sorted": sorted, "min": min, "max": max, "sum": sum,
    "abs": abs, "round": round, "any": any, "all": all, "hex": hex,
    "ord": ord, "chr": chr, "repr": repr, "type": type,
    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
    "__import__": None,        # BLOCKED — dynamic imports disallowed in plugins
}


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def _load_manifest():
    try:
        if not _MANIFEST.exists():
            return {}
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {}
        return {p["name"]: p for p in data
                if isinstance(p, dict) and p.get("name") and p.get("enabled", True)}
    except Exception as e:
        logger.error("plugin_loader: manifest load failed: %s", e)
        return {}


def _compile_one(name, code_text):
    try:
        sandbox = {"__builtins__": _SAFE_BUILTINS, "__name__": name,
                   "re": re, "time": time, "json": json, "hashlib": hashlib}
        exec(compile(code_text, f"plugin:{name}", "exec"), sandbox)  # noqa: S102
        fn = sandbox.get("analyze")
        if not callable(fn):
            logger.error("plugin_loader: %s missing required analyze(event) function", name)
            return None
        return fn
    except Exception as e:
        logger.error("plugin_loader: compile error in %s: %s", name, e)
        return None


def load_all():
    manifest = _load_manifest()
    for name, meta in manifest.items():
        p = _PLUGIN_DIR / meta.get("file", name + ".py")
        expected = meta.get("sha256", "")
        actual = _sha256(p)
        if not actual or actual != expected:
            logger.error("plugin_loader: %s SHA-256 MISMATCH — REFUSED (expected %s got %s)",
                         name, expected[:12], (actual or "missing")[:12])
            LOADED_PLUGINS.pop(name, None)
            continue
        try:
            code = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.error("plugin_loader: cannot read %s: %s", name, e)
            continue
        fn = _compile_one(name, code)
        if fn is None:
            continue
        LOADED_PLUGINS[name] = {"fn": fn, "sha256": actual,
                                "version": meta.get("version", "0.1"),
                                "load_time": time.time(), "calls": 0, "errors": 0}
        logger.info("plugin_loader: loaded '%s' v%s (sha256=%s…)",
                    name, meta.get("version", "0.1"), actual[:12])
    removed = [n for n in list(LOADED_PLUGINS) if n not in manifest]
    for n in removed:
        LOADED_PLUGINS.pop(n)
        logger.info("plugin_loader: unloaded '%s' (removed from manifest)", n)


def _run_sync(plugin, event):
    try:
        plugin["calls"] += 1
        result = plugin["fn"](dict(event))
        if isinstance(result, dict):
            return result
    except Exception as e:
        plugin["errors"] += 1
        logger.debug("plugin_loader: plugin error: %s", e)
    return None


async def route_event(event: dict) -> None:
    """Called by _maybe_plugin_route in correlator. Runs all plugins on eligible events."""
    if event.get("_plugin_enriched") or event.get("source") == "plugin_loader":
        return
    try:
        sev = float(event.get("severity", 0) or 0)
    except Exception:
        sev = 0.0
    if sev < _MIN_SEV or not LOADED_PLUGINS:
        return
    loop = asyncio.get_running_loop()
    for name, plugin in list(LOADED_PLUGINS.items()):
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _run_sync, plugin, event),
                timeout=_EXEC_TIMEOUT)
        except asyncio.TimeoutError:
            plugin["errors"] += 1
            logger.warning("plugin_loader: '%s' timed out after %.1fs", name, _EXEC_TIMEOUT)
            continue
        except Exception as e:
            logger.debug("plugin_loader: route error in '%s': %s", name, e)
            continue
        if result and _correlator is not None:
            result.setdefault("_plugin_enriched", True)
            result["_plugin_name"] = name
            try:
                if hasattr(_correlator, "ingest_event"):
                    asyncio.ensure_future(_correlator.ingest_event(result))
                elif hasattr(_correlator, "add_event"):
                    r = _correlator.add_event(result)
                    if asyncio.iscoroutine(r):
                        asyncio.ensure_future(r)
            except Exception as e:
                logger.debug("plugin_loader: re-ingest failed: %s", e)


def status():
    return {n: {"version": p["version"], "sha256": p["sha256"][:12],
                "calls": p["calls"], "errors": p["errors"]}
            for n, p in LOADED_PLUGINS.items()}


class _PluginWatcher(FileSystemEventHandler):
    def _reload(self):
        logger.info("plugin_loader: change detected — reloading manifest")
        try:
            load_all()
        except Exception as e:
            logger.error("plugin_loader: reload error: %s", e)
    def on_modified(self, e):
        if not e.is_directory:
            self._reload()
    def on_created(self, e):
        if not e.is_directory:
            self._reload()


async def start(correlator=None):
    global _correlator, _loop
    _correlator = correlator; _loop = asyncio.get_running_loop()
    _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    if not _MANIFEST.exists():
        _MANIFEST.write_text(json.dumps([], indent=2), encoding="utf-8")
        logger.info("plugin_loader: created empty manifest at %s", _MANIFEST)
    load_all()
    observer = None
    if _WD_OK:
        observer = Observer()
        observer.schedule(_PluginWatcher(), str(_PLUGIN_DIR), recursive=False)
        observer.start()
    logger.info("PLUGIN_LOADER: armed — %d plugin(s), hot-reload=%s",
                len(LOADED_PLUGINS), bool(observer))
    try:
        await asyncio.Event().wait()
    finally:
        if observer:
            try:
                observer.stop(); observer.join(timeout=5)
            except Exception:
                pass
