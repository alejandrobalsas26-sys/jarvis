"""
tools/yara_file_monitor.py — Event-driven YARA file integrity monitor (v31.0).

Uses watchdog (inotify/ReadDirectoryChangesW) for filesystem events.
CPU usage: 0% when files are unchanged. Scans triggered only on actual
file system events — no polling, no busy-wait.

V69 M54.1.2/.3/.4 — the live boot flooded the terminal with dozens of

    ERROR:asyncio:Exception in callback Queue.put_nowait(WindowsPath(...))
    asyncio.queues.QueueFull

Three defects produced that, all fixed here without changing what this monitor is
FOR (dropping YARA-scannable content is still detected):

  1. `loop.call_soon_threadsafe(scan_queue.put_nowait, path)` guarded by an
     `except asyncio.QueueFull` that could never fire — the put runs later, on the
     loop thread, long after that frame returned. Now every enqueue goes through
     core.safe_enqueue.SafeEnqueue, which catches QueueFull INSIDE the loop
     callback and counts the drop instead of raising a traceback per event.
  2. Watch roots were hardcoded to `~/Downloads` recursive — which CONTAINS this
     repo on the target host — with no dedup, so JARVIS's own writes saturated a
     100-slot queue. Roots and ignore rules now come from core.watch_policy, which
     excludes the repo tree (except analyze_inbox) and applies the previously-dead
     `_WATCHED_EXTENSIONS` allowlist so only executables queue from Downloads.
  3. Dropped events were silently lost. Overflow now marks the root STALE and
     schedules exactly ONE bounded reconciliation (core.watch_reconcile).

SECURITY_SCAN (executables anywhere in the security root) and CODE_ANALYSIS
(source in the explicit analyze_inbox) get SEPARATE queues, so a burst of one can
never starve the other.

YARA rules loaded from core/signatures/*.yar (existing YARA infrastructure).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from core.feed_sanitizer import sanitize_for_hud
from core.safe_enqueue import SafeEnqueue
from core.telemetry_auth import make_signed_broadcaster
from core.watch_policy import WatchClass, WatchEvent, default_watch_policy
from core.watch_reconcile import WatchReconciler

# One process-wide policy (cheap, pure). Rebuilt only by reset_watch_policy().
_policy = None


def get_watch_policy():
    """The active watch policy (lazily built from core.config)."""
    global _policy
    if _policy is None:
        _policy = default_watch_policy()
    return _policy


def reset_watch_policy(policy=None) -> None:
    """Override/clear the policy (tests)."""
    global _policy
    _policy = policy

_JARVIS_ROOT = Path(__file__).parent.parent

# File extensions worth scanning (skip media, databases, models). Kept as the
# post-dequeue guard in _scan_file; the ENQUEUE gate is core.watch_policy, which
# is far narrower (executables only outside the explicit inbox).
_SCAN_EXTENSIONS = {
    ".exe", ".dll", ".sys", ".bat", ".ps1", ".vbs", ".js",
    ".py", ".sh", ".elf", ".bin", ".so", ".jar",
}

# Max file size to scan (skip huge files — model weights, VODs, etc.)
_MAX_SCAN_BYTES = 50 * 1024 * 1024   # 50MB


def _should_scan(path: str, policy=None) -> bool:
    """True only if `path` should be YARA-scanned.

    V69 M54.1.3 — this used to be an ad-hoc mix of over-broad component names
    ("core"/"tools" skipped any such folder anywhere, a real detection blind spot)
    and substring tests over the whole path ("log" in str(p) also matched
    "catalog"), while the intended `_WATCHED_EXTENSIONS` allowlist was never
    applied at all — so main.py, tests/*.py and vector_store/*.bin were enqueued
    AND scanned. It now delegates to the one explicit, path-aware policy.
    """
    if policy is None:
        policy = get_watch_policy()
    return policy.classify(path, WatchEvent.MODIFIED).accepted


def _get_yara_rules():
    """Load compiled YARA rules from core/signatures/. Cached after first load."""
    try:
        import yara
        sig_dir = Path(__file__).parent.parent / "core" / "signatures"
        yar_files = list(sig_dir.glob("*.yar")) + list(sig_dir.glob("*.yara"))
        if not yar_files:
            return None
        filepaths = {f.stem: str(f) for f in yar_files}
        return yara.compile(filepaths=filepaths)
    except Exception as e:
        logger.debug(f"YARA_MONITOR: rule load failed: {e}")
        return None


_rules = None   # lazy-load on first scan

# Module-level queue ref — exposed for memory_hunter.py integration. V69 M54.1.1:
# consumers must NOT call put_nowait on this directly (that is the QueueFull-in-a-
# callback bug); use `offer_security_path()` below, which goes through SafeEnqueue.
_scan_queue_ref: asyncio.Queue | None = None
# The SafeEnqueue gate per WatchClass, and the overflow reconciler. Published for
# memory_hunter + runtime health; cleared on teardown so a BACKOFF restart can
# never leave a stale queue bound to a closed loop.
_gates_ref: dict | None = None
_reconciler_ref = None


def offer_security_path(path) -> bool:
    """The ONE supported way for another module to submit a path for YARA scanning.

    Overflow-safe: the QueueFull is caught inside the event-loop callback, and a
    drop is counted rather than raised. Returns False when the monitor is not
    running or the event was coalesced/dropped — a False is normal backpressure,
    never an error to retry or log per item.
    """
    gates = _gates_ref
    if not gates:
        return False
    gate = gates.get(WatchClass.SECURITY_SCAN)
    if gate is None:
        return False
    from core.safe_enqueue import EventPriority
    return gate.offer(Path(path), key=str(path).lower(),
                      priority=EventPriority.HIGH)


async def _scan_file(path: Path, broadcast_fn) -> None:
    """Scan a single file with YARA. Non-blocking via run_in_executor."""
    global _rules

    if path.suffix.lower() not in _SCAN_EXTENSIONS:
        return
    if not path.exists() or not path.is_file():
        return

    loop = asyncio.get_running_loop()

    def _do_scan():
        global _rules
        if _rules is None:
            _rules = _get_yara_rules()
        if _rules is None:
            return []
        try:
            size = path.stat().st_size
            if size == 0 or size > _MAX_SCAN_BYTES:
                return []
            matches = _rules.match(str(path))
            return [str(m) for m in matches]
        except Exception:
            return []

    matches = await loop.run_in_executor(None, _do_scan)
    if matches:
        logger.warning(
            f"YARA_MONITOR: MATCH in {path.name} — "
            f"rules: {', '.join(matches[:3])}"
        )
        await broadcast_fn({
            "type":      "yara_file_match",
            "file":      sanitize_for_hud(str(path), 120),
            "matches":   matches[:5],
            "severity":  "HIGH",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def start_yara_file_monitor(broadcast_fn) -> None:
    """
    Start event-driven YARA file integrity monitor.
    Silent if watchdog or yara-python not installed.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events   import FileSystemEventHandler
    except ImportError:
        logger.debug("YARA_MONITOR: watchdog not installed — skipping")
        return

    try:
        import yara  # noqa: F401
    except ImportError:
        logger.debug("YARA_MONITOR: yara-python not installed — skipping")
        return

    from core.config import settings

    signed_bcast = make_signed_broadcaster(broadcast_fn, "mitigation")
    policy = get_watch_policy()
    loop = asyncio.get_running_loop()

    # SECURITY_SCAN and CODE_ANALYSIS get SEPARATE bounded queues so a burst of
    # one class can never starve the other (M54.1.3).
    qsize = int(getattr(settings, "watch_queue_size", 512))
    debounce_s = float(getattr(settings, "watch_debounce_ms", 1000)) / 1000.0
    security_queue: asyncio.Queue = asyncio.Queue(maxsize=qsize)
    code_queue: asyncio.Queue = asyncio.Queue(maxsize=qsize)

    reconciler = WatchReconciler(offer_path=lambda p: _offer(p, WatchEvent.CREATED))

    gates = {
        WatchClass.SECURITY_SCAN: SafeEnqueue(
            queue=security_queue, loop=loop, name="YARA_MONITOR/security",
            debounce_s=debounce_s,
            on_overflow=lambda: _on_overflow(WatchClass.SECURITY_SCAN),
        ),
        WatchClass.CODE_ANALYSIS: SafeEnqueue(
            queue=code_queue, loop=loop, name="YARA_MONITOR/code",
            debounce_s=debounce_s,
            on_overflow=lambda: _on_overflow(WatchClass.CODE_ANALYSIS),
        ),
    }

    def _on_overflow(cls: WatchClass) -> None:
        """Overflow lost events: mark every root of this class STALE and schedule
        AT MOST ONE bounded reconciliation each — never one per dropped event."""
        roots = (policy.security_roots if cls is WatchClass.SECURITY_SCAN
                 else policy.code_roots)
        for root in roots:
            reconciler.mark_overflow(root)
            reconciler.schedule_reconcile(root)

    def _offer(src_path: str, event: WatchEvent, *, is_directory: bool = False) -> None:
        """The single enqueue seam for every raw event (handlers + reconciler)."""
        decision = policy.classify(src_path, event, is_directory=is_directory)
        if not decision.accepted:
            return
        gate = gates.get(decision.cls)
        if gate is None:
            return
        gate.offer(Path(src_path), key=decision.key, priority=decision.priority)

    import tools.yara_file_monitor as _self_module
    _self_module._scan_queue_ref = security_queue
    _self_module._gates_ref = gates
    _self_module._reconciler_ref = reconciler

    class _Handler(FileSystemEventHandler):
        """Runs on the watchdog observer thread. Every path out of here goes
        through _offer -> SafeEnqueue, which is overflow-safe by construction."""

        def on_created(self, event):
            _offer(event.src_path, WatchEvent.CREATED,
                   is_directory=event.is_directory)

        def on_modified(self, event):
            _offer(event.src_path, WatchEvent.MODIFIED,
                   is_directory=event.is_directory)

        def on_deleted(self, event):
            _offer(event.src_path, WatchEvent.DELETED,
                   is_directory=event.is_directory)

        def on_moved(self, event):
            dest = getattr(event, "dest_path", None)
            if dest:
                _offer(dest, WatchEvent.MOVED, is_directory=event.is_directory)

    observer = Observer()
    handler  = _Handler()
    mounted  = 0

    for watch_path in policy.observed_roots():
        if Path(watch_path).exists():
            try:
                observer.schedule(handler, watch_path, recursive=True)
                mounted += 1
                logger.info(f"YARA_MONITOR: watching {watch_path}")
            except Exception as e:
                logger.debug(f"YARA_MONITOR: cannot watch {watch_path}: {e}")

    if mounted == 0:
        logger.warning("YARA_MONITOR: no paths mounted — file monitor disabled")
        return

    observer.start()
    logger.info(
        f"YARA_MONITOR: event-driven integrity monitor active on {mounted} paths "
        f"(queue={qsize}, debounce={debounce_s:.2f}s)"
    )

    async def _drain(queue: asyncio.Queue, label: str) -> None:
        while True:
            path = await queue.get()
            try:
                await _scan_file(path, signed_bcast)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"YARA_MONITOR: {label} scan error: {exc}")

    # Drain both classes concurrently. Scans are awaited in-line rather than
    # spawned per event: the old `create_task` per item let an unbounded fan-out
    # of YARA matches saturate the 15W CPU while the queue looked healthy.
    drainers = [
        asyncio.create_task(_drain(security_queue, "security"), name="yara-drain-security"),
        asyncio.create_task(_drain(code_queue, "code"), name="yara-drain-code"),
    ]
    try:
        await asyncio.gather(*drainers)
    finally:
        for t in drainers:
            t.cancel()
        await reconciler.aclose()
        observer.stop()
        observer.join()
        _self_module._scan_queue_ref = None
        _self_module._gates_ref = None
        _self_module._reconciler_ref = None


def watcher_metrics() -> dict:
    """Bounded backpressure snapshot for runtime health (M54.1.13). Empty when the
    monitor is not running."""
    gates = _gates_ref
    out: dict = {"running": gates is not None, "classes": {}}
    if gates:
        for cls, gate in gates.items():
            out["classes"][cls.name.lower()] = gate.metrics()
    rec = _reconciler_ref
    out["reconcile"] = rec.snapshot() if rec is not None else {
        "roots": {}, "stale_roots": 0, "reconciliations": 0,
    }
    return out
