"""
core/exfil_detector.py — JARVIS V51.0 SENTINEL
Behavioral exfil detection. (1) Egress-throughput anomaly via psutil (rolling
z-score on bytes_sent). (2) Archive-staging: rapid creation of large compressed
containers in user dirs. Detection-only. T1048 / T1041 / T1560 / T1074.
"""
from __future__ import annotations
import asyncio, logging, os, time
from collections import deque
from pathlib import Path

logger = logging.getLogger("jarvis.exfil_detector")

try:
    import psutil; _PSUTIL_OK = True
except Exception:
    psutil = None; _PSUTIL_OK = False
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WATCHDOG_OK = False

_POLL = 5.0
_WARMUP = 12
_Z = 4.0
_MIN_RATE = 2_000_000
_ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".iso", ".cab", ".ace"}
_ARCHIVE_MIN = 50_000_000
_DIRS = [d for d in os.environ.get("JARVIS_DLP_DIRS", str(Path.home())).split(os.pathsep) if d.strip()]


async def _dispatch(correlator, event):
    if correlator is None:
        return
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
        else:
            logger.error("exfil_detector: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("exfil_detector: dispatch failed: %s", e)


def _stats(samples):
    n = len(samples)
    if n < 2:
        return 0.0, 0.0
    mean = sum(samples) / n
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    return mean, var ** 0.5


async def _egress_loop(correlator):
    samples = deque(maxlen=120)
    last = psutil.net_io_counters().bytes_sent
    last_t = time.monotonic()
    while True:
        await asyncio.sleep(_POLL)
        try:
            cur = psutil.net_io_counters().bytes_sent
        except Exception:
            continue
        now = time.monotonic(); dt = max(0.001, now - last_t)
        rate = (cur - last) / dt
        last, last_t = cur, now
        if len(samples) >= _WARMUP:
            mean, std = _stats(samples)
            if std > 0 and rate > _MIN_RATE and (rate - mean) / std >= _Z:
                event = {"source": "exfil_detector", "type": "egress_anomaly", "severity": 8.5,
                         "rate_bps": int(rate), "baseline_bps": int(mean),
                         "sigma": round((rate - mean) / std, 1),
                         "attck": ["T1048", "T1041"], "ts": time.time()}
                logger.warning("EXFIL: egress spike %.1f MB/s (baseline %.1f)", rate / 1e6, mean / 1e6)
                await _dispatch(correlator, event)
        samples.append(rate)


class _StageHandler(FileSystemEventHandler):
    def __init__(self, loop, correlator):
        self._loop = loop; self._c = correlator
    def on_created(self, e):
        if e.is_directory:
            return
        if Path(e.src_path).suffix.lower() in _ARCHIVE_EXT:
            asyncio.run_coroutine_threadsafe(self._check(e.src_path), self._loop)
    async def _check(self, p):
        await asyncio.sleep(3)
        try:
            sz = os.path.getsize(p)
        except OSError:
            return
        if sz >= _ARCHIVE_MIN:
            event = {"source": "exfil_detector", "type": "data_staging", "severity": 8.5,
                     "path": p, "size_bytes": sz, "attck": ["T1560", "T1074"], "ts": time.time()}
            logger.warning("EXFIL: large archive staged %s (%.1f MB)", p, sz / 1e6)
            await _dispatch(self._c, event)


async def start(correlator=None):
    if not _PSUTIL_OK:
        logger.warning("EXFIL_DETECTOR: psutil unavailable — dormant")
        await asyncio.Event().wait(); return
    loop = asyncio.get_running_loop()
    observer = None
    if _WATCHDOG_OK:
        observer = Observer(); h = _StageHandler(loop, correlator); started = []
        for d in _DIRS:
            try:
                if os.path.isdir(d):
                    observer.schedule(h, d, recursive=True); started.append(d)
            except Exception:
                pass
        if started:
            observer.start()
    logger.info("EXFIL_DETECTOR: armed — egress z-score + archive staging")
    try:
        await _egress_loop(correlator)
    finally:
        if observer:
            try:
                observer.stop(); observer.join(timeout=5)
            except Exception:
                pass
