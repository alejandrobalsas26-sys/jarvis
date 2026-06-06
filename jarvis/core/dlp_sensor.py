"""
core/dlp_sensor.py — JARVIS V51.0 SENTINEL
Host DLP / sensitive-data classification. Watches configured dirs (and optionally
clipboard) for PII, PAN (Luhn-validated), secrets/keys, credentials. Findings are
REDACTED (type + count + token hash only — never the raw value) and mapped to
GDPR / Panama Ley 81 / PCI-DSS. T1552 / T1005.
"""
from __future__ import annotations
import asyncio, hashlib, logging, os, re, time
from pathlib import Path

logger = logging.getLogger("jarvis.dlp_sensor")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_OK = True
except Exception:
    Observer = None; FileSystemEventHandler = object; _WATCHDOG_OK = False

_DIRS = [d for d in os.environ.get("JARVIS_DLP_DIRS", str(Path.home() / "Documents")).split(os.pathsep) if d.strip()]
_CLIPBOARD = os.environ.get("JARVIS_DLP_CLIPBOARD", "0") == "1"
_MAX_BYTES = 5_000_000
_SCAN_EXT = {".txt", ".csv", ".log", ".json", ".xml", ".ini", ".cfg", ".env", ".md",
             ".yaml", ".yml", ".sql", ".html", ".htm", ".py", ".ps1", ".bat", ".docx", ".pdf"}

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVKEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*\S{6,}")


def _luhn(num: str) -> bool:
    d = [int(c) for c in num if c.isdigit()]
    if not (13 <= len(d) <= 19):
        return False
    s = 0
    for i, x in enumerate(reversed(d)):
        if i % 2 == 1:
            x *= 2
            if x > 9:
                x -= 9
        s += x
    return s % 10 == 0


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()[:12]


def classify(text: str, source: str = "inline") -> list:
    out = []
    def add(kind, token, fw, ctrl):
        out.append({"type": kind, "token_hash": _h(token), "framework": fw, "control": ctrl})
    for m in _PAN.finditer(text):
        if _luhn(m.group(0)):
            add("payment_card", m.group(0), "PCI-DSS / Ley81", "Cardholder data")
    for m in _PRIVKEY.finditer(text):
        add("private_key", m.group(0), "Secrets", "Cryptographic key exposure")
    for m in _AWS.finditer(text):
        add("cloud_key", m.group(0), "Secrets", "Cloud credential exposure")
    for m in _JWT.finditer(text):
        add("jwt", m.group(0), "Secrets", "Bearer/session token exposure")
    for m in _SECRET.finditer(text):
        add("credential", m.group(0), "Secrets", "Plaintext credential")
    for e in list({m.group(0) for m in _EMAIL.finditer(text)})[:50]:
        add("pii_email", e, "GDPR / Ley81", "Personal data identifier")
    return out


def _scan_file(path: str) -> list:
    p = Path(path)
    try:
        if p.suffix.lower() not in _SCAN_EXT or p.stat().st_size > _MAX_BYTES:
            return []
        data = p.read_bytes()
    except Exception:
        return []
    return classify(data.decode("utf-8", "ignore"), source=str(p))


def _summarize(findings):
    counts = {}
    for f in findings:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
    sev = 9.0 if any(f["type"] in ("payment_card", "private_key", "cloud_key", "credential", "jwt")
                     for f in findings) else 6.0
    seen, comp = set(), []
    for f in findings:
        k = (f["framework"], f["control"])
        if k not in seen:
            seen.add(k); comp.append({"framework": f["framework"], "control": f["control"]})
    return counts, comp, sev


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
            logger.error("dlp_sensor: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("dlp_sensor: dispatch failed: %s", e)


async def _alert(correlator, source, findings):
    counts, comp, sev = _summarize(findings)
    event = {"source": "dlp_sensor", "type": "sensitive_data_exposure", "severity": sev,
             "data_source": source, "categories": counts, "finding_count": len(findings),
             "compliance": comp, "attck": ["T1552", "T1005"], "ts": time.time()}
    logger.warning("DLP: %d sensitive item(s) in %s — %s", len(findings), source, counts)
    await _dispatch(correlator, event)


class _DLPHandler(FileSystemEventHandler):
    def __init__(self, loop, correlator):
        self._loop = loop; self._c = correlator
    def _go(self, path):
        try:
            f = _scan_file(path)
            if f:
                asyncio.run_coroutine_threadsafe(_alert(self._c, path, f), self._loop)
        except Exception as e:
            logger.debug("dlp handler: %s", e)
    def on_created(self, e):
        if not e.is_directory:
            self._go(e.src_path)
    def on_modified(self, e):
        if not e.is_directory:
            self._go(e.src_path)


async def _clipboard_loop(correlator):
    try:
        import pyperclip
    except Exception:
        logger.info("dlp: pyperclip unavailable — clipboard scan off")
        await asyncio.Event().wait(); return
    last = None
    while True:
        await asyncio.sleep(2.0)
        try:
            cur = pyperclip.paste()
        except Exception:
            continue
        if cur and cur != last:
            last = cur
            f = classify(cur, source="clipboard")
            if f:
                await _alert(correlator, "clipboard", f)


async def start(correlator=None):
    if not _WATCHDOG_OK and not _CLIPBOARD:
        logger.warning("DLP_SENSOR: watchdog unavailable and clipboard off — dormant")
        await asyncio.Event().wait(); return
    loop = asyncio.get_running_loop()
    observer = None; started = []
    if _WATCHDOG_OK:
        observer = Observer(); h = _DLPHandler(loop, correlator)
        for d in _DIRS:
            try:
                if os.path.isdir(d):
                    observer.schedule(h, d, recursive=True); started.append(d)
            except Exception:
                pass
        if started:
            observer.start()
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("dlp_sensor", lambda text, **k: classify(text))
        except Exception:
            pass
    logger.info("DLP_SENSOR: armed — dirs=%s clipboard=%s", started or "none", _CLIPBOARD)
    try:
        if _CLIPBOARD:
            await _clipboard_loop(correlator)
        else:
            await asyncio.Event().wait()
    finally:
        if observer:
            try:
                observer.stop(); observer.join(timeout=5)
            except Exception:
                pass
