"""
core/health_watchdog.py — JARVIS V52.0 AEGIS
Self-diagnostic + task supervisor. Ensures JARVIS never silently fails. Supervises
tracked async modules (auto-restart on crash, capped with backoff), monitors the
legacy _V4X_TASKS list for crashes, and audits dependency health (YARA ruleset,
Ollama reachability, ETW/Security-log admin, pywintrace, aiohttp). Failures emit a
Sev 10.0 internal event to the correlator and push to the dashboard.
"""
from __future__ import annotations
import asyncio, logging, os, time

logger = logging.getLogger("jarvis.health_watchdog")

_INTERVAL = 30
_RESTART_BACKOFF = 10
_MAX_RESTARTS = 5
_SUP = {}            # name -> {factory, task, restarts, next}
_LEGACY = None
_correlator = None


def track(name, factory):
    """Spawn + supervise a module. factory() -> coroutine. Auto-restart on death."""
    try:
        t = asyncio.ensure_future(factory())
    except Exception as e:
        logger.error("health_watchdog: initial spawn of %s failed: %s", name, e)
        t = None
    _SUP[name] = {"factory": factory, "task": t, "restarts": 0, "next": 0.0}
    logger.info("health_watchdog: supervising %s", name)
    return t


def attach_legacy(task_list):
    global _LEGACY
    _LEGACY = task_list


async def _dispatch(event):
    if _correlator is None:
        return
    try:
        if hasattr(_correlator, "ingest_event"):
            await _correlator.ingest_event(event)
        elif hasattr(_correlator, "add_event"):
            r = _correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
    except Exception as e:
        logger.error("health_watchdog: dispatch failed: %s", e)


def _to_dashboard(event):
    try:
        from core import c2_dashboard
        c2_dashboard.push(event)
    except Exception:
        pass


def _check_yara():
    try:
        import yara  # noqa: F401
    except Exception:
        return ("yara", False, "yara-python missing")
    d = os.environ.get("JARVIS_YARA_RULES", "rules")
    ok = os.path.isdir(d) and any(f.lower().endswith((".yar", ".yara"))
                                  for _r, _dn, fs in os.walk(d) for f in fs)
    return ("yara", ok, "rules present" if ok else "no rules dir/files")


def _check_ollama():
    import urllib.request
    url = os.environ.get("JARVIS_OLLAMA_URL", "http://localhost:11434") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return ("ollama", getattr(r, "status", 200) == 200, "responsive")
    except Exception as e:
        return ("ollama", False, str(e)[:60])


def _check_admin():
    if os.name != "nt":
        return ("etw_admin", False, "non-windows")
    try:
        import ctypes
        adm = bool(ctypes.windll.shell32.IsUserAnAdmin())
        return ("etw_admin", adm, "elevated" if adm else "not elevated (ETW/Sec-log limited)")
    except Exception as e:
        return ("etw_admin", False, str(e)[:60])


def _check_import(mod, label):
    try:
        __import__(mod); return (label, True, "available")
    except Exception:
        return (label, False, "missing")


async def _supervise_once():
    for name, info in list(_SUP.items()):
        t = info["task"]
        if t is not None and not t.done():
            continue
        exc = None
        if t is not None:
            try:
                exc = t.exception()
            except Exception:
                exc = None
        if info["restarts"] >= _MAX_RESTARTS or time.monotonic() < info["next"]:
            continue
        info["restarts"] += 1
        info["next"] = time.monotonic() + _RESTART_BACKOFF * info["restarts"]
        logger.critical("HEALTH: module '%s' down (exc=%s) — restart #%d",
                        name, exc, info["restarts"])
        evt = {"source": "health_watchdog", "type": "health_alert", "severity": 10.0,
               "module": name, "detail": f"restart #{info['restarts']} (exc={exc})",
               "attck": ["T1562"], "ts": time.time()}
        await _dispatch(evt); _to_dashboard(evt)
        try:
            info["task"] = asyncio.ensure_future(info["factory"]())
        except Exception as e:
            logger.error("health_watchdog: restart of %s failed: %s", name, e)


def _legacy_dead():
    dead = 0
    if _LEGACY:
        for t in list(_LEGACY):
            try:
                if t.done() and not t.cancelled() and t.exception() is not None:
                    dead += 1
            except Exception:
                pass
    return dead


async def _audit():
    checks = [_check_yara(), _check_ollama(), _check_admin(),
              _check_import("etw", "pywintrace"), _check_import("aiohttp", "aiohttp")]
    status = {"source": "health_watchdog", "type": "health_status", "severity": 1.0,
              "checks": [{"name": n, "ok": ok, "info": info} for n, ok, info in checks],
              "supervised": {k: {"alive": not (v["task"] is None or v["task"].done()),
                                 "restarts": v["restarts"]} for k, v in _SUP.items()},
              "ts": time.time()}
    _to_dashboard(status)


async def start(correlator=None, task_list=None):
    global _correlator
    _correlator = correlator
    if task_list is not None:
        attach_legacy(task_list)
    logger.info("HEALTH_WATCHDOG: armed — %d supervised module(s), audit every %ds",
                len(_SUP), _INTERVAL)
    while True:
        try:
            await _supervise_once()
            dead = _legacy_dead()
            if dead:
                evt = {"source": "health_watchdog", "type": "health_alert", "severity": 10.0,
                       "module": "_V4X_TASKS", "detail": f"{dead} legacy task(s) crashed",
                       "attck": ["T1562"], "ts": time.time()}
                await _dispatch(evt); _to_dashboard(evt)
            await _audit()
        except Exception as e:
            logger.debug("health_watchdog: cycle error: %s", e)
        await asyncio.sleep(_INTERVAL)
