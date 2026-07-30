"""
core/plugin_loader.py — detection plugin inventory. **Execution is DISABLED.**

V69 M61.7 — dynamic source-code plugin execution is refused, fail-closed. This module
now reads and integrity-checks the plugin manifest and reports what it found; it does
not run plugin code. ``LOADED_PLUGINS`` therefore stays empty and ``REFUSED_PLUGINS``
carries the honest inventory.

WHY — the previous docstring described a security boundary that did not exist
--------------------------------------------------------------------------------
It claimed "cryptographically-verified", "signed manifest" and a "RESTRICTED exec
environment" with ``open``, ``os``, ``subprocess``, ``socket``, ``ctypes`` and
``importlib`` BLOCKED. Bandit flagged the ``exec`` (B102). Every one of those claims
was false, and the gap was not theoretical:

  1. **The sandbox was not a sandbox.** A ``globals()`` dict with a trimmed
     ``__builtins__`` restricts nothing in CPython, because the plugin still reaches
     live objects. The exec environment handed plugins the ``re``, ``json``, ``time``
     and ``hashlib`` *module objects*, and any function on them carries
     ``__globals__['__builtins__']`` — the real builtins, ``__import__`` included.
     Three lines inside the "sandbox" were verified to import ``os``, read arbitrary
     files and spawn a subprocess. ``().__class__.__base__.__subclasses__()`` is a
     second, independent route to the same place. A Python ``exec`` globals dict is a
     namespace, not a privilege boundary, and it cannot be made into one.
  2. **The manifest was never signed.** Integrity was a SHA-256 in
     ``plugins/manifest.json``, sitting in the same directory as the plugin. Anyone
     who could write the plugin could write its expected hash, so the check detected
     accidental corruption and nothing adversarial.
  3. **The directory was redirectable.** ``JARVIS_PLUGIN_DIR`` moved the whole
     plugin+manifest pair to any path in the environment.
  4. **No operator was in the loop.** ``start()`` loaded everything at boot and a
     watchdog hot-reloaded on any file change, so a dropped file executed with no
     approval, no prompt and no audit decision.

Taken together that is arbitrary code execution inside the JARVIS process, with the
process's own credentials and network reach, gated only by write access to a
directory. M61 is a stabilization release, so the fix is the smallest safe one:
**refuse to execute**, and say so honestly.

WHAT STILL WORKS
----------------
Manifest parsing, SHA-256 verification and reporting are intact, so an operator can
still see which plugins exist, whether their hashes match and why each was refused.
``route_event`` remains wired to the correlator and is a no-op while nothing is
loaded. Nothing silently pretends to work.

MIGRATION
---------
There is deliberately no environment variable that turns ``exec`` back on: an opt-in
flag would be the same vulnerability behind a different default. A plugin that needs
to run must become one of:
  * a normal reviewed module inside ``core``/``tools``, shipped and tested with the
    application; or
  * an installed, allowlisted Python entry point (a real package, versioned and
    auditable); or
  * an out-of-process worker with a bounded timeout, no inherited secrets, a minimal
    environment, no shell and an explicit protocol.
Each of those is a reviewable boundary. ``exec`` on operator-writable source is not.
"""
from __future__ import annotations
# ``re``/``time`` are gone with the sandbox: they were only ever injected INTO the
# exec globals, and every function on them exposed __globals__['__builtins__'] — one
# of the two escape routes described above.
import asyncio, hashlib, json, logging, os
from pathlib import Path

logger = logging.getLogger("jarvis.plugin_loader")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WD_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WD_OK = False

# Anchored to the package root so plugins load regardless of the CWD JARVIS
# was launched from (repo root vs jarvis/). JARVIS_PLUGIN_DIR still overrides.
_PKG_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = Path(os.environ.get("JARVIS_PLUGIN_DIR", str(_PKG_ROOT / "plugins")))
_MANIFEST = _PLUGIN_DIR / "manifest.json"
_EXEC_TIMEOUT = 5.0
_MIN_SEV = 7.0

#: Whether this build can execute plugin source. Structurally False — there is no
#: code path and no environment variable that makes it True. Read by ``status()``,
#: the health watchdog and the release-qualification tests.
DYNAMIC_EXEC_SUPPORTED = False

#: Name -> plugin record. Stays EMPTY while dynamic execution is disabled; kept so
#: ``correlator`` and the C2 dashboard keep working unchanged.
LOADED_PLUGINS: dict = {}

#: Name -> {file, expected_sha256, actual_sha256, integrity, reason}. The honest
#: inventory: what exists on disk, whether it verified, and why it did not run.
REFUSED_PLUGINS: dict = {}

#: Reported verbatim to the operator, once per refused plugin.
REFUSAL_REASON = (
    "dynamic source-code plugin execution is disabled (V69 M61.7): an exec globals "
    "dict is not a privilege boundary, and the manifest hash is not a signature. "
    "Port the plugin to a reviewed core/tools module, an installed allowlisted entry "
    "point, or an out-of-process worker."
)

_correlator = None
_loop = None


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
    """Refuse to execute plugin source. Always returns ``None``.

    Deliberately kept as the single choke point rather than deleted: every caller
    already routes through it, so there is exactly one place where execution could be
    reintroduced, and ``tests/test_plugin_exec_v69_m617.py`` asserts that this module
    contains no ``exec``/``compile``/``eval`` node at all.

    ``code_text`` is accepted and dropped unread. It is never compiled, so a malicious
    plugin's syntax, imports and payload are all inert.
    """
    logger.error("plugin_loader: REFUSED '%s' — %s", name, REFUSAL_REASON)
    return None


def load_all():
    """Inventory the manifest, verify integrity, execute nothing."""
    manifest = _load_manifest()
    REFUSED_PLUGINS.clear()
    for name, meta in manifest.items():
        p = _PLUGIN_DIR / meta.get("file", name + ".py")
        expected = meta.get("sha256", "")
        actual = _sha256(p)
        integrity = "match" if (actual and actual == expected) else "mismatch"
        if integrity != "match":
            # Still reported: a hash mismatch is useful operator signal even though
            # a matching hash would not have caused execution either.
            logger.error("plugin_loader: %s SHA-256 MISMATCH (expected %s got %s)",
                         name, expected[:12], (actual or "missing")[:12])
            LOADED_PLUGINS.pop(name, None)
        REFUSED_PLUGINS[name] = {
            "file": str(p.name),
            "expected_sha256": expected[:12],
            "actual_sha256": (actual or "missing")[:12],
            "integrity": integrity,
            "version": meta.get("version", "0.1"),
            "reason": REFUSAL_REASON,
        }
        # The source is never read, so nothing about it can influence this process.
        _compile_one(name, None)

    removed = [n for n in list(LOADED_PLUGINS) if n not in manifest]
    for n in removed:
        LOADED_PLUGINS.pop(n)
        logger.info("plugin_loader: unloaded '%s' (removed from manifest)", n)

    if REFUSED_PLUGINS:
        logger.warning(
            "PLUGIN_LOADER: %d plugin(s) present and NOT executed — dynamic source "
            "execution is disabled. See core/plugin_loader.py for the migration path.",
            len(REFUSED_PLUGINS),
        )


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
    """Honest health payload: what ran, what did not, and why.

    ``dynamic_exec_supported`` is reported so a dashboard cannot show "0 plugins" and
    leave an operator believing the plugin directory was empty.
    """
    return {
        "dynamic_exec_supported": DYNAMIC_EXEC_SUPPORTED,
        "loaded": {n: {"version": p["version"], "sha256": p["sha256"][:12],
                       "calls": p["calls"], "errors": p["errors"]}
                   for n, p in LOADED_PLUGINS.items()},
        "refused": dict(REFUSED_PLUGINS),
    }


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
    # Hot-reload now only refreshes the refusal inventory: a file dropped into the
    # plugin directory can no longer cause code to run, with or without a watcher.
    logger.info(
        "PLUGIN_LOADER: inventory only — execution DISABLED, %d loaded, %d refused, "
        "watcher=%s", len(LOADED_PLUGINS), len(REFUSED_PLUGINS), bool(observer),
    )
    try:
        await asyncio.Event().wait()
    finally:
        if observer:
            try:
                observer.stop(); observer.join(timeout=5)
            except Exception:
                pass
