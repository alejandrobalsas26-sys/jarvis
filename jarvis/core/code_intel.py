"""
core/code_intel.py — Autonomous code intelligence analyzer (v37.0).

Watches the analyze_inbox/ drop folder for new files.
On new file detection:
  1. YARA scan (existing engine)
  2. Entropy analysis (high entropy = packed/encrypted)
  3. String extraction (URLs, IPs, registry keys, API calls)
  4. LLM code analysis (explain functionality, extract IOCs, grade risk)
  5. Generate YARA rule candidate
  6. Save full report to logs/code_analysis/

Supported: .py .ps1 .vbs .bat .js .c .cpp .asm .sh + binary files
Drop folder: jarvis/analyze_inbox/

V69 M68B (C) — RESOURCE & LIFECYCLE BOUNDS
==========================================
The watcher was an unbounded pipeline with a lossy, irreversible completion mark:

  * ``asyncio.Queue()`` with no ``maxsize`` — an operator emptying a directory
    into the inbox queued every file, and the queue grew until memory did;
  * ``asyncio.create_task(analyze_file(...))`` per event — concurrency equal to
    the arrival rate, each task holding a whole file in memory and a 90 s LLM
    call, none of them tracked (asyncio holds only a weak reference, so a task
    nobody keeps can be collected mid-await);
  * ``_ANALYZED: set[str]`` — a module-level set that only ever grew, and was
    written BEFORE the work: a file whose read failed was marked analysed for the
    life of the process and could never be retried. "Started" and "succeeded"
    were the same observation;
  * ``file_path.read_bytes()`` plus a pure-Python per-byte entropy loop ON THE
    EVENT LOOP, with no size policy — one large drop froze every other coroutine,
    including the AURA telemetry the operator was watching it through;
  * the ``Observer`` thread was started and never stopped, and the ``while True``
    loop had no cancellation path, so a shutdown left both behind.

What replaces it: a bounded queue fed through ``core.safe_enqueue.SafeEnqueue``
(the repository's one thread→loop seam, which coalesces duplicate events and
counts drops instead of raising into the loop), an explicit number of workers, a
bounded LRU ledger with distinct PENDING/RUNNING/COMPLETED/FAILED/REJECTED states
and an explicit retry policy, an explicit maximum file size, all blocking work in
``asyncio.to_thread``, and a ``finally`` that cancels the workers and joins the
Observer.
"""

import asyncio, hashlib, math, re, threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from loguru import logger

from core.managed_paths import app_subdir, logs_subdir
from core.safe_enqueue import EventPriority, SafeEnqueue


def inbox_dir(*, create: bool = True) -> Path:
    """The operator drop folder, anchored on the INSTALLATION (V69 M61 RC1).

    Public because ``main.py`` announces this path to the operator at boot: when it
    printed ``Path("analyze_inbox").absolute()`` while the watcher observed a
    different CWD-relative folder, the banner named a directory nothing was
    watching. One accessor, one folder.
    """
    return app_subdir("analyze_inbox", create=create)


def _reports_dir() -> Path:
    """The managed code-analysis report directory, created on first report."""
    return logs_subdir("code_analysis")


# ── Bounded lifecycle configuration (V69 M68B) ───────────────────────────────
#: Files admitted to the pipeline at once. Beyond this the enqueue seam drops and
#: COUNTS, which is an observable policy; growing without limit is not.
CI_QUEUE_MAX = 64
#: Concurrent analyses. Each holds one file plus one LLM call, so this is the real
#: memory and inference-pressure knob. Two, on a 15 W CPU-bound host.
CI_WORKERS = 2
#: Refuse to read anything larger. An inbox is operator-fed, not trusted to be
#: small, and the analysis reads the WHOLE file to hash and score it.
CI_MAX_FILE_BYTES = 32 * 1024 * 1024
#: Attempts per file before it stops being retried.
CI_MAX_ATTEMPTS = 3
#: Upper bound on the ledger. Oldest terminal entries are evicted first.
CI_LEDGER_MAX = 2048
#: Let a file finish being written before reading it. In the WORKER, not in the
#: intake loop — in the intake loop it serialised every arrival behind a sleep.
CI_SETTLE_S = 0.5


class AnalysisState(str, Enum):
    """Where one file is in the pipeline. The states are deliberately distinct:
    the pre-M68B code could not tell "queued" from "running" from "finished" from
    "failed", because all four were "present in a set"."""

    PENDING = "pending"        # observed, admitted to the queue, not yet started
    RUNNING = "running"        # a worker owns it right now
    COMPLETED = "completed"    # analysed successfully, report written
    FAILED = "failed"          # failed; retryable while attempts < CI_MAX_ATTEMPTS
    REJECTED = "rejected"      # terminal by policy (oversized); never retried


_TERMINAL = (AnalysisState.COMPLETED, AnalysisState.REJECTED)


@dataclass
class _LedgerEntry:
    state: AnalysisState
    attempts: int = 0
    reason: str | None = None


class AnalysisLedger:
    """Bounded, evictable record of every file the pipeline has seen.

    Thread-safe: ``observe`` is called from the watchdog Observer thread while the
    worker states are written on the event loop.

    Eviction drops the oldest TERMINAL entry first, so a completed file may be
    re-analysed after 2048 other files have gone by (correct — the ledger is a
    duplicate-suppressor, not a permanent archive; the reports on disk are), while
    an in-flight RUNNING entry is never evicted out from under its worker.
    """

    def __init__(self, *, max_entries: int = CI_LEDGER_MAX,
                 max_attempts: int = CI_MAX_ATTEMPTS) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_attempts = max(1, int(max_attempts))
        self._entries: "OrderedDict[str, _LedgerEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self.evicted = 0

    # -- queries --------------------------------------------------------------
    def state(self, key: str) -> AnalysisState | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.state if entry is not None else None

    def attempts(self, key: str) -> int:
        with self._lock:
            entry = self._entries.get(key)
            return entry.attempts if entry is not None else 0

    def reason(self, key: str) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.reason if entry is not None else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def snapshot(self) -> dict:
        with self._lock:
            counts: dict = {}
            for entry in self._entries.values():
                counts[entry.state.value] = counts.get(entry.state.value, 0) + 1
            return {"tracked": len(self._entries), "capacity": self._max_entries,
                    "evicted": self.evicted, "max_attempts": self._max_attempts,
                    "by_state": counts}

    # -- transitions ----------------------------------------------------------
    def observe(self, key: str) -> bool:
        """Record an arrival. Returns True when the file should be queued.

        Called from the Observer thread. A file already PENDING or RUNNING is a
        duplicate event and is not queued again.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.state in (AnalysisState.PENDING, AnalysisState.RUNNING):
                    return False
                if entry.state in _TERMINAL:
                    return False
                if entry.attempts >= self._max_attempts:
                    return False
                entry.state = AnalysisState.PENDING
                self._entries.move_to_end(key)
                return True
            self._entries[key] = _LedgerEntry(state=AnalysisState.PENDING)
            self._evict_locked()
            return True

    def claim(self, key: str) -> bool:
        """A worker takes ownership. Returns False when it must not run.

        This is the transition the pre-M68B code got wrong: it marked the file
        before the work AND treated that mark as success, so a transient read
        error became a permanent verdict.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LedgerEntry(state=AnalysisState.PENDING)
                self._entries[key] = entry
                self._evict_locked()
            if entry.state in _TERMINAL:
                return False
            if entry.state is AnalysisState.RUNNING:
                return False
            if entry.attempts >= self._max_attempts:
                return False
            entry.state = AnalysisState.RUNNING
            entry.attempts += 1
            entry.reason = None
            self._entries.move_to_end(key)
            return True

    def complete(self, key: str) -> None:
        """Only after the report exists. COMPLETED is earned, never assumed."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LedgerEntry(state=AnalysisState.COMPLETED)
                self._entries[key] = entry
            entry.state = AnalysisState.COMPLETED
            entry.reason = None
            self._entries.move_to_end(key)
            self._evict_locked()

    def fail(self, key: str, reason: str) -> bool:
        """Record a failure. Returns True when the file may still be retried."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LedgerEntry(state=AnalysisState.FAILED, attempts=1)
                self._entries[key] = entry
            entry.state = AnalysisState.FAILED
            entry.reason = reason
            self._entries.move_to_end(key)
            self._evict_locked()
            return entry.attempts < self._max_attempts

    def reject(self, key: str, reason: str) -> None:
        """Terminal by policy. Retrying an oversized file produces the same
        refusal, so it is not a failure to retry."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LedgerEntry(state=AnalysisState.REJECTED)
                self._entries[key] = entry
            entry.state = AnalysisState.REJECTED
            entry.reason = reason
            self._entries.move_to_end(key)
            self._evict_locked()

    def _evict_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            victim = None
            for key, entry in self._entries.items():
                if entry.state in _TERMINAL or entry.state is AnalysisState.FAILED:
                    victim = key
                    break
            if victim is None:
                # Everything in flight: evict the oldest rather than grow.
                victim = next(iter(self._entries))
            del self._entries[victim]
            self.evicted += 1


#: The process-wide ledger. Replaces the unbounded ``_ANALYZED`` set.
_LEDGER = AnalysisLedger()


def ledger_snapshot() -> dict:
    """Bounded counters for runtime health. Never file names — a path an operator
    dropped is operator data."""
    return _LEDGER.snapshot()


_CODE_ANALYST_SYSTEM = """You are an elite malware reverse engineer.
Analyze the provided code/binary content. Be technically precise.
Structure your analysis:
1. FUNCTIONALITY — what does this code do?
2. RISK LEVEL — CRITICAL/HIGH/MEDIUM/LOW with justification
3. TECHNIQUE — most likely MITRE ATT&CK technique(s)
4. IOCs — extract all: IPs, domains, URLs, registry keys, mutex names
5. YARA RULE — write a detection rule for this sample
6. VERDICT — malicious/suspicious/benign with confidence %"""


def _shannon_entropy(data: bytes) -> float:
    """Calculate Shannon entropy of byte data.

    ``Counter`` rather than a Python-level accumulation loop: identical result,
    but the counting happens in C, so a 32 MiB sample costs a fraction of a
    second in the worker thread instead of tens of seconds.
    """
    if not data:
        return 0.0
    n = len(data)
    entropy = -sum(
        (count/n) * math.log2(count/n)
        for count in Counter(data).values()
    )
    return round(entropy, 3)


def _extract_strings(data: bytes, min_len: int = 6) -> list[str]:
    """Extract printable ASCII strings from binary data."""
    pattern = re.compile(
        rb"[\x20-\x7e]{" + str(min_len).encode() + rb",}"
    )
    strings = [m.group().decode("ascii", errors="replace")
               for m in pattern.finditer(data)]
    return strings[:200]   # cap at 200 strings


def _classify_strings(strings: list[str]) -> dict:
    """Classify extracted strings into categories."""
    iocs = {
        "urls":       [],
        "ips":        [],
        "domains":    [],
        "registry":   [],
        "api_calls":  [],
        "suspicious": [],
    }
    url_re  = re.compile(r"https?://[^\s<>\"']{8,}")
    ip_re   = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
    reg_re  = re.compile(r"HK(EY|LM|CU|CR|U)\\[^\s]{4,}")
    susp_kw = {
        "CreateRemoteThread", "VirtualAllocEx", "WriteProcessMemory",
        "LoadLibraryA", "GetProcAddress", "WScript.Shell",
        "powershell", "cmd.exe", "wget", "curl", "base64",
        "invoke-expression", "downloadstring", "net.webclient",
    }

    for s in strings:
        if url_re.search(s):
            iocs["urls"].append(s[:100])
        elif ip_re.search(s):
            iocs["ips"].append(s[:45])
        elif reg_re.search(s):
            iocs["registry"].append(s[:100])
        for kw in susp_kw:
            if kw.lower() in s.lower():
                iocs["suspicious"].append(s[:80])
                break

    return {k: list(set(v))[:20] for k, v in iocs.items()}


class OversizedFile(Exception):
    """The drop exceeds ``CI_MAX_FILE_BYTES``. Terminal by policy, not a failure
    to retry — the next attempt would read the same bytes and refuse again."""


def _read_within_policy(file_path: Path, *, max_bytes: int) -> bytes:
    """Stat, enforce the size policy, THEN read. Runs in a worker thread.

    The stat is not merely an optimisation: reading first and checking afterwards
    is the unbounded read the policy exists to prevent.
    """
    size = file_path.stat().st_size
    if size > max_bytes:
        raise OversizedFile(f"{size} bytes exceeds the {max_bytes}-byte policy")
    data = file_path.read_bytes()
    if len(data) > max_bytes:          # grew between the stat and the read
        raise OversizedFile(f"{len(data)} bytes exceeds the {max_bytes}-byte policy")
    return data


def _static_scan(data: bytes) -> dict:
    """Every CPU-bound step in one place, so ONE ``to_thread`` covers them all."""
    strings = _extract_strings(data)
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        # V69 M61.7 (Bandit B324): MD5 stays, but strictly as a legacy **IOC lookup
        # key**. Threat-intel corpora (and ``core.soar_enrichment``, which reads the
        # "md5" key) are still indexed by MD5, so dropping it would silently lose
        # malware-analysis pivots. ``usedforsecurity=False`` states the contract the
        # code already honours — this use is explicitly NON-SECURITY: the digest is a
        # corpus index, never authentication, never integrity, never a signature, and
        # no severity/verdict path reads it.
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "entropy": _shannon_entropy(data),
        "strings": strings,
        "iocs": _classify_strings(strings),
    }


async def analyze_file(
    file_path: Path,
    broadcast_fn,
    ollama_client,
    model: str,
    *,
    ledger: AnalysisLedger | None = None,
    max_bytes: int = CI_MAX_FILE_BYTES,
) -> dict | None:
    """
    Full analysis pipeline for a dropped file.
    Returns analysis report dict.

    This is the LIFECYCLE GUARD; ``_analyze_claimed`` does the work. The split
    exists because of a defect M68B's own tests found: the first draft caught
    ``CancelledError`` only around the LLM call, so a cancellation during the
    threaded read left the entry RUNNING — and a RUNNING entry is never claimable
    again, which poisons the file for the life of the process in exactly the way
    the old ``_ANALYZED`` set did. The invariant is therefore enforced here, over
    EVERY exit path rather than at chosen await points:

        no return from this function may leave a file RUNNING.
    """
    book = ledger if ledger is not None else _LEDGER
    key = str(file_path)
    if not book.claim(key):
        return None
    try:
        return await _analyze_claimed(
            file_path, broadcast_fn, ollama_client, model,
            book=book, key=key, max_bytes=max_bytes)
    except asyncio.CancelledError:
        book.fail(key, "cancelled")     # shutdown is not a verdict
        raise
    except Exception as e:              # noqa: BLE001 - one file, not the worker
        logger.warning(f"CODE_INTEL: {file_path.name} failed: {e}")
        book.fail(key, "unexpected_error")
        return None
    finally:
        # The net. Anything that slipped past both handlers — a BaseException, a
        # branch that forgot to record — still leaves the file retryable.
        if book.state(key) is AnalysisState.RUNNING:
            book.fail(key, "interrupted")


async def _analyze_claimed(
    file_path: Path,
    broadcast_fn,
    ollama_client,
    model: str,
    *,
    book: AnalysisLedger,
    key: str,
    max_bytes: int,
) -> dict | None:
    """The analysis itself, for a file this process has already claimed.

    ``book.complete`` is called only after the report exists on disk. Every early
    return records WHY, so a transient failure is retryable and a policy refusal
    is not.
    """
    logger.info(f"CODE_INTEL: analyzing {file_path.name}")

    # Read under the size policy. Both the stat and the read are blocking, so
    # they belong in a thread: a 32 MiB read on the event loop stalls the AURA
    # telemetry the operator is watching this through.
    try:
        data = await asyncio.to_thread(
            _read_within_policy, file_path, max_bytes=max_bytes)
    except OversizedFile as e:
        logger.warning(f"CODE_INTEL: refusing {file_path.name}: {e}")
        book.reject(key, "oversized")
        await broadcast_fn({
            "type":     "code_analysis_rejected",
            "filename": file_path.name,
            "reason":   "oversized",
            "limit_bytes": max_bytes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return None
    except Exception as e:
        logger.debug(f"CODE_INTEL: read error: {e}")
        book.fail(key, "read_error")
        return None

    await broadcast_fn({
        "type":     "code_analysis_started",
        "filename": file_path.name,
        "size":     len(data),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # File hashes.
    #
    # SHA-256 is THE identity/integrity hash for this report: it is the value
    # broadcast, the value an operator compares, and the only digest any trust
    # decision may read. The MD5 IOC key and the CPU-bound scan are computed
    # together in one worker thread (see ``_static_scan``).
    try:
        scan = await asyncio.to_thread(_static_scan, data)
    except Exception as e:
        logger.warning(f"CODE_INTEL: static scan failed for {file_path.name}: {e}")
        book.fail(key, "static_scan_error")
        return None

    sha256 = scan["sha256"]
    md5    = scan["md5"]

    # Entropy
    entropy = scan["entropy"]
    is_packed = entropy > 7.0   # very high entropy = likely packed

    # String extraction + IOC classification
    iocs    = scan["iocs"]

    # Deep PE analysis for executables
    pe_analysis = {}
    if file_path.suffix.lower() in {".exe", ".dll", ".sys"}:
        try:
            from tools.binary_inverter import deep_disassemble
            pe_analysis = await deep_disassemble(
                file_path, broadcast_fn, ollama_client, model
            )
        except Exception:
            pass

    # Try to read as text for LLM analysis
    try:
        text_content = data.decode("utf-8", errors="replace")[:4000]
    except Exception:
        text_content = f"[Binary file, {len(data)} bytes, entropy={entropy}]"

    # YARA scan.
    #
    # V69 M68B (C): this was `hits = scan_command(...)` — an `async def` called
    # WITHOUT await, and handed a single string where it takes a token list. The
    # coroutine was never run, iterating it raised, and the bare `except` swallowed
    # it, so `yara_hits` was ALWAYS empty. That silently degraded the severity
    # grade below, which reads `yara_hits` first. Awaited, and tokenized as the
    # callee documents.
    yara_hits = []
    try:
        from core.yara_analyzer import scan_command
        hits = await scan_command(
            [file_path.name, *text_content[:500].split()])
        yara_hits = [str(h) for h in hits]
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    # LLM analysis
    llm_analysis = ""
    prompt = (
        f"File: {file_path.name}\n"
        f"SHA256: {sha256}\n"
        f"Size: {len(data)} bytes\n"
        f"Entropy: {entropy} ({'PACKED/ENCRYPTED' if is_packed else 'normal'})\n"
        f"YARA hits: {yara_hits or 'none'}\n\n"
        f"SUSPICIOUS STRINGS: {iocs['suspicious'][:10]}\n"
        f"URLS: {iocs['urls'][:5]}\n"
        f"IPs: {iocs['ips'][:5]}\n\n"
        f"FILE CONTENT (first 3000 chars):\n{text_content[:3000]}\n\n"
        "Provide full analysis:"
    )

    # Add PE analysis to LLM prompt
    if pe_analysis:
        prompt += (
            f"\n\nPE DISASSEMBLY ANALYSIS:\n"
            f"Suspicious APIs: {pe_analysis.get('suspicious_apis', {})}\n"
            f"Entry point ASM (first 20):\n"
            + "\n".join(pe_analysis.get("entry_asm", [])[:20])
        )

    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _CODE_ANALYST_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream = False,
                extra_body = {"options": {"num_ctx": 4096, "temperature": 0.1}},
            ),
            timeout=90.0,
        )
        llm_analysis = response.choices[0].message.content.strip()
    except asyncio.CancelledError:
        raise                          # recorded by the guard in analyze_file
    except Exception as e:
        llm_analysis = f"LLM analysis failed: {e}"

    # Compile report
    report = {
        "filename":   file_path.name,
        "sha256":     sha256,
        "md5":        md5,
        "size_bytes": len(data),
        "entropy":    entropy,
        "is_packed":  is_packed,
        "yara_hits":  yara_hits,
        "iocs":       iocs,
        "analysis":   llm_analysis,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }

    # Save report
    report_name = f"{file_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    report_path = _reports_dir() / report_name
    report_md   = f"""# Code Intelligence Report: {file_path.name}

**SHA256:** `{sha256}`  *(identity / integrity)*
**MD5:** `{md5}`  *(legacy IOC lookup key — NOT an integrity check)*
**Size:** {len(data)} bytes
**Entropy:** {entropy} {'⚠ PACKED/ENCRYPTED' if is_packed else '✓ normal'}
**YARA Hits:** {', '.join(yara_hits) if yara_hits else 'none'}

## IOCs Extracted
- URLs: {iocs['urls'][:5]}
- IPs: {iocs['ips'][:5]}
- Suspicious: {iocs['suspicious'][:5]}

## LLM Analysis
{llm_analysis}

---
*Generated by JARVIS v37.0 Code Intelligence Engine*
*{datetime.now(timezone.utc).isoformat()}*
"""
    try:
        await asyncio.to_thread(report_path.write_text, report_md, "utf-8")
    except Exception as e:
        logger.warning(f"CODE_INTEL: report write failed for {file_path.name}: {e}")
        book.fail(key, "report_write_error")
        return None

    severity = (
        "CRITICAL" if yara_hits or (is_packed and iocs["suspicious"])
        else "HIGH" if is_packed or iocs["ips"] or iocs["urls"]
        else "MEDIUM"
    )

    # The report exists on disk; only now is this file COMPLETED.
    book.complete(key)

    await broadcast_fn({
        "type":       "code_analysis_complete",
        "filename":   file_path.name,
        "sha256":     sha256[:16] + "…",
        "entropy":    entropy,
        "is_packed":  is_packed,
        "yara_hits":  len(yara_hits),
        "ioc_count":  sum(len(v) for v in iocs.values()),
        "report":     str(report_path),
        "severity":   severity,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })

    logger.info(
        f"CODE_INTEL: {file_path.name} analyzed — "
        f"entropy={entropy} packed={is_packed} "
        f"yara_hits={len(yara_hits)} severity={severity}"
    )

    return report


async def _inbox_worker(
    queue: asyncio.Queue,
    ledger: AnalysisLedger,
    *,
    broadcast_fn,
    ollama_client,
    model,
    settle_s: float,
    max_bytes: int,
) -> None:
    """One bounded consumer. Exactly ``CI_WORKERS`` of these exist.

    Cancellation propagates: a worker never swallows ``CancelledError``, so
    shutdown is deterministic rather than best-effort.
    """
    while True:
        path = await queue.get()
        try:
            if settle_s > 0:
                await asyncio.sleep(settle_s)   # let the file finish being written
            await analyze_file(path, broadcast_fn, ollama_client, model,
                               ledger=ledger, max_bytes=max_bytes)
        except asyncio.CancelledError:
            raise
        except Exception as e:                  # noqa: BLE001 - one file, not the worker
            logger.warning(f"CODE_INTEL: analysis error: {e}")
            ledger.fail(str(path), "worker_error")
        finally:
            queue.task_done()


async def start_inbox_watcher(
    broadcast_fn, tts, ollama_client, model,
    *,
    workers: int = CI_WORKERS,
    queue_max: int = CI_QUEUE_MAX,
    max_bytes: int = CI_MAX_FILE_BYTES,
    settle_s: float = CI_SETTLE_S,
    ledger: AnalysisLedger | None = None,
    observer_factory=None,
) -> None:
    """
    Watch analyze_inbox/ for new files.
    Auto-analyzes everything that appears, under an explicit bound.

    Shutdown is part of the contract: cancelling this coroutine cancels every
    worker, stops the Observer and joins its thread. Nothing runs after it
    returns.
    """
    logger.info(f"CODE_INTEL: watching {inbox_dir()} for files")

    book = ledger if ledger is not None else _LEDGER
    observer = None
    worker_tasks: list[asyncio.Task] = []
    greeting: asyncio.Task | None = None
    try:
        from watchdog.events import FileSystemEventHandler
        if observer_factory is None:
            from watchdog.observers import Observer
            observer_factory = Observer

        queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(queue_max)))
        # The one thread→loop seam (M54.1.1): QueueFull is caught INSIDE the loop
        # callback, duplicates coalesce on the producer thread, and drops are
        # counted rather than raised into the loop's default exception handler.
        enqueue = SafeEnqueue(queue=queue, name="code-intel-inbox",
                              warn_fn=lambda msg: logger.warning(f"CODE_INTEL: {msg}"))

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    return
                src = str(event.src_path)
                # A file already PENDING/RUNNING/terminal is not queued again.
                if not book.observe(src):
                    return
                enqueue.offer(Path(src), key=src, priority=EventPriority.NORMAL)

        observer = observer_factory()
        observer.schedule(_Handler(), str(inbox_dir()), recursive=False)
        observer.start()

        if tts:
            greeting = asyncio.create_task(
                tts.speak_async("Code analysis inbox active. Drop any file to analyze."),
                name="code-intel-greeting")

        worker_tasks = [
            asyncio.create_task(
                _inbox_worker(queue, book, broadcast_fn=broadcast_fn,
                              ollama_client=ollama_client, model=model,
                              settle_s=settle_s, max_bytes=max_bytes),
                name=f"code-intel-worker-{i}")
            for i in range(max(1, int(workers)))
        ]
        # Workers run until cancelled. If one dies, the gather re-raises and the
        # finally below tears the rest down — a half-dead pipeline is not a state
        # this watcher stays in.
        await asyncio.gather(*worker_tasks)

    except ImportError:
        logger.warning("CODE_INTEL: watchdog not installed — inbox watcher disabled")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"CODE_INTEL: watcher error: {e}")
    finally:
        for task in worker_tasks:
            task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        if greeting is not None and not greeting.done():
            greeting.cancel()
        if observer is not None:
            try:
                observer.stop()
                observer.join(5.0)
            except Exception as e:      # noqa: BLE001 - teardown reports, never raises
                logger.warning(f"CODE_INTEL: observer shutdown: {e}")
