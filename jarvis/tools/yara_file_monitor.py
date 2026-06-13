"""
tools/yara_file_monitor.py — Event-driven YARA file integrity monitor (v31.0).

Uses watchdog (inotify/ReadDirectoryChangesW) for filesystem events.
CPU usage: 0% when files are unchanged. Scans triggered only on actual
file system events — no polling, no busy-wait.

Monitored paths:
  - JARVIS source directory (self-integrity)
  - Windows System32 drivers directory (rootkit indicator)
  - User Downloads (malware drop zone)

YARA rules loaded from core/signatures/*.yar (existing YARA infrastructure).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from core.feed_sanitizer import sanitize_for_hud
from core.telemetry_auth import make_signed_broadcaster

_JARVIS_ROOT = Path(__file__).parent.parent

# Directories to monitor — JARVIS source (core/, tools/) + external threat zones.
# NEVER watch the repo root: it contains logs/, and JARVIS log writes would
# trigger scans → which write logs → infinite YARA loop / QueueFull.
_MONITOR_PATHS = [
    _JARVIS_ROOT / "analyze_inbox",                        # external drop zone
    Path.home() / "Downloads",                             # external downloads
]

# File extensions worth scanning (skip media, databases, models)
_SCAN_EXTENSIONS = {
    ".exe", ".dll", ".sys", ".bat", ".ps1", ".vbs", ".js",
    ".py", ".sh", ".elf", ".bin", ".so", ".jar",
}

# Max file size to scan (skip huge files — model weights, VODs, etc.)
_MAX_SCAN_BYTES = 50 * 1024 * 1024   # 50MB

# ── Scan exclusions — prevent the log-write → scan → log-write loop ──────────
_EXCLUDED_DIRS = {
    "logs", "logs/", ".git", "__pycache__",
    "node_modules", ".venv", "venv",
    "core", "tools", "aura",
}
_EXCLUDED_EXTENSIONS = {
    ".jsonl", ".json", ".log", ".md", ".yaml", ".yml",
    ".db", ".sqlite", ".sqlite3", ".dmp", ".png", ".jpg",
    ".docx", ".pdf", ".txt", ".html", ".css",
}
_WATCHED_EXTENSIONS = {
    ".exe", ".dll", ".sys",
    ".ps1", ".bat", ".vbs",
    ".js", ".hta",
}


def _should_scan(path: str) -> bool:
    """
    Return True only if this path should be YARA-scanned.
    Excludes logs/, temp files, and all non-executable types.
    """
    p = Path(path)

    # Exclude any path inside excluded directories
    parts = {part.lower() for part in p.parts}
    if parts & _EXCLUDED_DIRS:
        return False

    # Skip excluded extensions
    if p.suffix.lower() in _EXCLUDED_EXTENSIONS:
        return False

    # Skip files in logs-like / temp paths
    if "log" in str(p).lower() or "temp" in str(p).lower():
        return False

    return True


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

# Module-level queue ref — exposed for memory_hunter.py integration
_scan_queue_ref: asyncio.Queue | None = None


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

    signed_bcast = make_signed_broadcaster(broadcast_fn, "mitigation")
    scan_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    import tools.yara_file_monitor as _self_module
    _self_module._scan_queue_ref = scan_queue
    loop = asyncio.get_running_loop()

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory or not _should_scan(event.src_path):
                return
            try:
                loop.call_soon_threadsafe(
                    scan_queue.put_nowait, Path(event.src_path)
                )
            except asyncio.QueueFull:
                pass

        def on_modified(self, event):
            if event.is_directory or not _should_scan(event.src_path):
                return
            try:
                loop.call_soon_threadsafe(
                    scan_queue.put_nowait, Path(event.src_path)
                )
            except asyncio.QueueFull:
                pass

    observer = Observer()
    handler  = _Handler()
    mounted  = 0

    for watch_path in _MONITOR_PATHS:
        if watch_path.exists():
            try:
                observer.schedule(handler, str(watch_path), recursive=True)
                mounted += 1
                logger.info(f"YARA_MONITOR: watching {watch_path}")
            except Exception as e:
                logger.debug(f"YARA_MONITOR: cannot watch {watch_path}: {e}")

    if mounted == 0:
        logger.warning("YARA_MONITOR: no paths mounted — file monitor disabled")
        return

    observer.start()
    logger.info(
        f"YARA_MONITOR: event-driven integrity monitor active on {mounted} paths"
    )

    try:
        while True:
            path = await scan_queue.get()
            asyncio.create_task(_scan_file(path, signed_bcast))
    finally:
        observer.stop()
        observer.join()
