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
"""

import asyncio, hashlib, math, re
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_INBOX_DIR   = Path("analyze_inbox")
_REPORTS_DIR = Path("logs/code_analysis")
_INBOX_DIR.mkdir(exist_ok=True)
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_ANALYZED: set[str] = set()   # prevent double-analysis

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
    """Calculate Shannon entropy of byte data."""
    if not data:
        return 0.0
    freq = {}
    for byte in data:
        freq[byte] = freq.get(byte, 0) + 1
    n = len(data)
    entropy = -sum(
        (count/n) * math.log2(count/n)
        for count in freq.values()
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


async def analyze_file(
    file_path: Path,
    broadcast_fn,
    ollama_client,
    model: str,
) -> dict | None:
    """
    Full analysis pipeline for a dropped file.
    Returns analysis report dict.
    """
    if str(file_path) in _ANALYZED:
        return None
    _ANALYZED.add(str(file_path))

    logger.info(f"CODE_INTEL: analyzing {file_path.name}")

    await broadcast_fn({
        "type":     "code_analysis_started",
        "filename": file_path.name,
        "size":     file_path.stat().st_size,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Read file
    try:
        data = file_path.read_bytes()
    except Exception as e:
        logger.debug(f"CODE_INTEL: read error: {e}")
        return None

    # File hash
    sha256 = hashlib.sha256(data).hexdigest()
    md5    = hashlib.md5(data).hexdigest()

    # Entropy
    entropy = _shannon_entropy(data)
    is_packed = entropy > 7.0   # very high entropy = likely packed

    # String extraction + IOC classification
    strings = _extract_strings(data)
    iocs    = _classify_strings(strings)

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

    # YARA scan
    yara_hits = []
    try:
        from core.yara_analyzer import scan_command
        hits = scan_command(file_path.name + " " + text_content[:500])
        yara_hits = [str(h) for h in hits]
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
    report_path = _REPORTS_DIR / report_name
    report_md   = f"""# Code Intelligence Report: {file_path.name}

**SHA256:** `{sha256}`
**MD5:** `{md5}`
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
    report_path.write_text(report_md, encoding="utf-8")

    severity = (
        "CRITICAL" if yara_hits or (is_packed and iocs["suspicious"])
        else "HIGH" if is_packed or iocs["ips"] or iocs["urls"]
        else "MEDIUM"
    )

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


async def start_inbox_watcher(
    broadcast_fn, tts, ollama_client, model
) -> None:
    """
    Watch analyze_inbox/ for new files.
    Auto-analyzes everything that appears.
    """
    logger.info(f"CODE_INTEL: watching {_INBOX_DIR.absolute()} for files")

    try:
        from watchdog.observers import Observer
        from watchdog.events   import FileSystemEventHandler

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    loop.call_soon_threadsafe(
                        queue.put_nowait, Path(event.src_path)
                    )

        observer = Observer()
        observer.schedule(_Handler(), str(_INBOX_DIR), recursive=False)
        observer.start()

        if tts:
            asyncio.create_task(
                tts.speak_async("Code analysis inbox active. Drop any file to analyze.")
            )

        while True:
            path = await queue.get()
            await asyncio.sleep(0.5)   # let file finish writing
            asyncio.create_task(
                analyze_file(path, broadcast_fn, ollama_client, model)
            )

    except ImportError:
        logger.warning("CODE_INTEL: watchdog not installed — inbox watcher disabled")
    except Exception as e:
        logger.error(f"CODE_INTEL: watcher error: {e}")
