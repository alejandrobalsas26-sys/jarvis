"""
core/ai_reverser.py — JARVIS V49.0 OMNISCIENCE
Static PE triage. Parses an unverified PE (READ-ONLY — never executed) with
pefile: imports/exports/sections(entropy)/strings + imphash. Computes a
deterministic high-risk-import pre-score, then asks local Qwen (Ollama) as an RE
analyst for a JSON verdict. Malicious -> CRITICAL alert to the correlator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("jarvis.ai_reverser")

try:
    import pefile
    _PEFILE_OK = True
except Exception:
    pefile = None
    _PEFILE_OK = False

_OLLAMA_URL = os.environ.get("JARVIS_OLLAMA_URL", "http://localhost:11434")
_RE_MODEL = os.environ.get("JARVIS_RE_MODEL", "qwen2.5:7b-instruct-q4_K_M")
_MAX_STRINGS = 280
_PACK_ENTROPY = 7.2
_LLM_MAL_CONF = 60
_PRE_SCORE_MAL = 70

_HIGH_RISK = {
    "virtualallocex", "writeprocessmemory", "createremotethread",
    "ntunmapviewofsection", "queueuserapc", "setthreadcontext", "resumethread",
    "openprocess", "loadlibrarya", "loadlibraryw", "getprocaddress", "winexec",
    "shellexecutea", "shellexecutew", "urldownloadtofilea", "urldownloadtofilew",
    "internetopena", "internetopenurla", "internetreadfile", "cryptencrypt",
    "cryptdecrypt", "cryptacquirecontexta", "regsetvaluea", "regsetvaluew",
    "createservicea", "createservicew", "adjusttokenprivileges", "virtualprotect",
    "virtualprotectex", "ntprotectvirtualmemory", "createprocessa", "createprocessw",
    "wsastartup", "isdebuggerpresent", "checkremotedebuggerpresent",
}

_ASCII_RE = re.compile(rb"[\x20-\x7e]{5,}")
_U16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){5,}")
_sema = asyncio.Semaphore(1)


def _strings(data: bytes, cap: int = _MAX_STRINGS) -> list:
    out = []
    for m in _ASCII_RE.finditer(data):
        out.append(m.group().decode("ascii", "ignore"))
        if len(out) >= cap:
            return out
    for m in _U16_RE.finditer(data):
        out.append(m.group().decode("utf-16-le", "ignore"))
        if len(out) >= cap:
            break
    return out


def _extract(path: str) -> dict:
    pe = pefile.PE(path, fast_load=False)
    meta = {"path": path, "imphash": "", "imports": [], "exports": [],
            "sections": [], "suspicious_imports": [], "pre_score": 0}
    try:
        meta["imphash"] = pe.get_imphash() or ""
    except Exception:
        pass
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll = entry.dll.decode("ascii", "ignore") if entry.dll else ""
            for imp in entry.imports:
                if imp.name:
                    name = imp.name.decode("ascii", "ignore")
                    meta["imports"].append(f"{dll}:{name}")
                    if name.lower() in _HIGH_RISK:
                        meta["suspicious_imports"].append(name)
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for s in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if s.name:
                meta["exports"].append(s.name.decode("ascii", "ignore"))
    packed_sections = 0
    for sec in pe.sections:
        try:
            ent = round(float(sec.get_entropy()), 2)
        except Exception:
            ent = 0.0
        nm = sec.Name.rstrip(b"\x00").decode("ascii", "ignore")
        packed = ent >= _PACK_ENTROPY
        if packed:
            packed_sections += 1
        meta["sections"].append({"name": nm, "entropy": ent,
                                 "vsize": int(sec.Misc_VirtualSize),
                                 "rawsize": int(sec.SizeOfRawData), "packed": packed})
    with open(path, "rb") as f:
        raw = f.read()
    meta["strings"] = _strings(raw)
    pe.close()

    score = 0
    score += min(40, len(set(meta["suspicious_imports"])) * 6)
    score += min(30, packed_sections * 15)
    if 0 < len(meta["imports"]) <= 5:      # tiny import table => likely packed
        score += 20
    if not meta["imports"]:
        score += 15
    meta["pre_score"] = min(100, score)
    return meta


def _ollama_up() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{_OLLAMA_URL}/api/tags", timeout=4) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _ollama_chat(system: str, user: str, timeout: int = 150) -> str:
    import urllib.request
    payload = {"model": _RE_MODEL, "stream": False, "format": "json",
               "options": {"temperature": 0.1, "num_ctx": 4096},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    req = urllib.request.Request(f"{_OLLAMA_URL}/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return data.get("message", {}).get("content", "") or ""


_RE_SYSTEM = (
    "You are a senior malware reverse engineer performing static triage of a "
    "Windows PE from extracted metadata only. Be conservative and evidence-based. "
    "Return ONLY a JSON object with keys: verdict (the string Malicious or Benign), "
    "confidence (integer 0-100), capabilities (array of short strings), reasoning "
    "(one concise sentence). No prose outside the JSON."
)


def _build_prompt(meta: dict) -> str:
    return json.dumps({
        "imphash": meta["imphash"],
        "import_count": len(meta["imports"]),
        "suspicious_imports": sorted(set(meta["suspicious_imports"]))[:40],
        "exports": meta["exports"][:40],
        "sections": meta["sections"],
        "high_entropy_sections": [s["name"] for s in meta["sections"] if s["packed"]],
        "deterministic_pre_score": meta["pre_score"],
        "sample_strings": meta["strings"][:160],
    }, default=str)[:11000]


def _parse_verdict(content: str) -> dict:
    try:
        v = json.loads(content)
        return {"verdict": str(v.get("verdict", "Unknown")),
                "confidence": int(v.get("confidence", 0) or 0),
                "capabilities": list(v.get("capabilities", []))[:20],
                "reasoning": str(v.get("reasoning", ""))[:400]}
    except Exception:
        mal = bool(re.search(r"malicious", content, re.I))
        return {"verdict": "Malicious" if mal else "Unknown", "confidence": 0,
                "capabilities": [], "reasoning": "unparsed LLM output"}


async def _alert(correlator, meta: dict, verdict: dict, severity: float) -> None:
    event = {"source": "ai_reverser", "type": "malicious_pe", "severity": severity,
             "file_path": meta["path"], "imphash": meta["imphash"],
             "pre_score": meta["pre_score"], "llm_verdict": verdict["verdict"],
             "llm_confidence": verdict["confidence"],
             "capabilities": verdict["capabilities"],
             "suspicious_imports": sorted(set(meta["suspicious_imports"]))[:40],
             "attck": ["T1027", "T1059", "T1106"], "ts": __import__("time").time()}
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
            logger.error("ai_reverser: no correlator hook; event=%s", event)
    except Exception as e:
        logger.error("ai_reverser: alert dispatch failed: %s", e)


async def analyze(path: str, *, correlator=None) -> dict:
    res = {"path": path, "analyzed": False, "verdict": None, "error": None}
    if not _PEFILE_OK:
        res["error"] = "pefile unavailable"
        return res
    p = Path(path)
    if not p.is_file():
        res["error"] = "file not found"
        return res
    loop = asyncio.get_running_loop()
    async with _sema:
        try:
            meta = await loop.run_in_executor(None, _extract, str(p))
        except Exception as e:
            res["error"] = f"PE parse failed: {e}"
            return res
        verdict = {"verdict": "Unknown", "confidence": 0, "capabilities": [],
                   "reasoning": "LLM offline"}
        if _ollama_up():
            try:
                content = await loop.run_in_executor(
                    None, _ollama_chat, _RE_SYSTEM, _build_prompt(meta))
                verdict = _parse_verdict(content)
            except Exception as e:
                logger.warning("ai_reverser: LLM query failed: %s", e)

    res["analyzed"] = True
    res["meta"] = {"imphash": meta["imphash"], "pre_score": meta["pre_score"],
                   "suspicious_imports": sorted(set(meta["suspicious_imports"]))}
    res["verdict"] = verdict

    llm_mal = verdict["verdict"].lower() == "malicious" and verdict["confidence"] >= _LLM_MAL_CONF
    pre_mal = meta["pre_score"] >= _PRE_SCORE_MAL
    if llm_mal or pre_mal:
        sev = 9.5 if (llm_mal and pre_mal) else 9.0
        logger.warning("ai_reverser: MALICIOUS %s (pre=%d, llm=%s/%d)",
                       path, meta["pre_score"], verdict["verdict"], verdict["confidence"])
        await _alert(correlator, meta, verdict, sev)
    else:
        logger.info("ai_reverser: benign/unknown %s (pre=%d, llm=%s)",
                    path, meta["pre_score"], verdict["verdict"])
    return res


async def start(correlator=None) -> None:
    """main.py startup hook. Watchdog Pattern: dormant if pefile missing or
    Ollama unreachable. On-demand triage via analyze()."""
    if not _PEFILE_OK:
        logger.warning("AI_REVERSER: pefile unavailable — dormant")
        await asyncio.Event().wait(); return
    if not _ollama_up():
        logger.warning("AI_REVERSER: Ollama unreachable at %s — dormant", _OLLAMA_URL)
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("ai_reverser", analyze)
        except Exception:
            pass
    logger.info("AI_REVERSER: armed — static PE triage via %s", _RE_MODEL)
    await asyncio.Event().wait()
