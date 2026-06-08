"""
core/self_integrity.py — JARVIS V55.0 OMNI-REDUNDANCY
Autonomous Memory Integrity Guard. Establishes a cryptographic baseline of four
independent integrity planes and checks them every 60 seconds:
  (1) Interpreter .text — SHA-256 of python.exe's .text section in own memory.
      Detects native rootkit injection / code patching of the Python interpreter.
  (2) JARVIS module source — SHA-256 of all loaded core.* / tools.* .py files.
      Detects runtime modification of JARVIS's own Python code on disk.
  (3) Canary buffer — fixed bytes allocated at startup in own heap.
      Any unexpected write to our address space invalidates this.
  (4) Private executable pages — VirtualQuery walk for MEM_PRIVATE+EXECUTE regions.
      New pages since baseline = reflective injection / shellcode artifact.
Pushes a health status event to the C2 dashboard on every check. On violation,
dispatches a Sev 10.0 correlator alert (T1014 / T1055 / T1036).
"""
from __future__ import annotations
import asyncio, ctypes, hashlib, logging, os, struct, sys, time
from pathlib import Path

logger = logging.getLogger("jarvis.self_integrity")

_IS_WINDOWS = os.name == "nt"
_CHECK_SECS = 60
_BASELINE_DELAY = 8         # seconds after start before baselining (let imports settle)
_MIN_INJECT_BYTES = 4096    # ignore new exec pages < 4KB (small extension stubs)
_correlator = None

# Canary — written once at import time; never mutated by JARVIS
_CANARY = b"JARVIS-INTEGRITY-CANARY-V55-\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE\xFF\x00"
_canary_buf = ctypes.create_string_buffer(_CANARY)

_baseline_text: str | None = None
_baseline_mods: dict = {}
_baseline_exec_pages: set = set()

if _IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _is_admin():
    if not _IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _text_hash():
    """Hash python.exe's .text section from own process memory (no ReadProcessMemory)."""
    if not _IS_WINDOWS:
        return None
    try:
        base = int(_k32.GetModuleHandleW(None))
        if base == 0:
            return None
        if ctypes.string_at(base, 2) != b"MZ":
            return None
        e_lfanew = struct.unpack_from("<I", ctypes.string_at(base + 0x3C, 4))[0]
        if ctypes.string_at(base + e_lfanew, 4) != b"PE\x00\x00":
            return None
        num_sec = struct.unpack_from("<H", ctypes.string_at(base + e_lfanew + 6, 2))[0]
        opt_sz = struct.unpack_from("<H", ctypes.string_at(base + e_lfanew + 20, 2))[0]
        sec_off = e_lfanew + 24 + opt_sz
        for i in range(min(num_sec, 32)):
            sec = ctypes.string_at(base + sec_off + i * 40, 40)
            name = sec[:8].rstrip(b"\x00")
            if name == b".text":
                va = struct.unpack_from("<I", sec, 12)[0]
                sz = struct.unpack_from("<I", sec, 16)[0]
                if sz == 0 or sz > 0x8000000:
                    return None
                raw = ctypes.string_at(base + va, sz)
                return hashlib.sha256(raw).hexdigest()
    except Exception as e:
        logger.debug("self_integrity: text_hash error: %s", e)
    return None


def _module_hashes():
    h = {}
    for name, mod in list(sys.modules.items()):
        if not (name.startswith("core.") or name.startswith("tools.")):
            continue
        src = getattr(mod, "__file__", None)
        if not src or not src.endswith(".py"):
            continue
        try:
            h[name] = hashlib.sha256(Path(src).read_bytes()).hexdigest()
        except Exception:
            pass
    return h


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong)]


def _exec_pages():
    if not _IS_WINDOWS:
        return set()
    MEM_COMMIT = 0x1000; MEM_PRIVATE = 0x20000
    EXEC = {0x10, 0x20, 0x40, 0x80}
    found = set(); mbi = _MBI(); addr = 0
    for _ in range(200_000):
        if _k32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi),
                             ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and
                mbi.Protect in EXEC and mbi.RegionSize >= _MIN_INJECT_BYTES):
            found.add(int(mbi.BaseAddress or addr))
        try:
            nxt = int(mbi.BaseAddress or addr) + mbi.RegionSize
        except Exception:
            break
        if nxt <= addr:
            break
        addr = nxt
    return found


def _check_canary():
    try:
        return ctypes.string_at(ctypes.addressof(_canary_buf), len(_CANARY)) == _CANARY
    except Exception:
        return False


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
        logger.error("self_integrity: dispatch failed: %s", e)


def _to_dashboard(event):
    try:
        from core import c2_dashboard
        c2_dashboard.push(event)
    except Exception:
        pass


async def _check_loop():
    global _baseline_exec_pages
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_CHECK_SECS)
        findings = []
        try:
            cur_text = await loop.run_in_executor(None, _text_hash)
            if _baseline_text and cur_text and cur_text != _baseline_text:
                findings.append({"check": "interpreter_text",
                                 "detail": "python.exe .text MODIFIED — native rootkit injection suspected"})
            cur_mods = await loop.run_in_executor(None, _module_hashes)
            for name, h in cur_mods.items():
                bh = _baseline_mods.get(name)
                if bh and h != bh:
                    findings.append({"check": "module_source",
                                     "detail": f"module {name} source hash changed at runtime"})
            if not _check_canary():
                findings.append({"check": "canary",
                                 "detail": "canary buffer OVERWRITTEN — arbitrary write in JARVIS address space"})
            cur_pages = await loop.run_in_executor(None, _exec_pages)
            new_pages = cur_pages - _baseline_exec_pages
            if new_pages:
                findings.append({"check": "exec_memory",
                                 "detail": f"{len(new_pages)} new private executable region(s) — "
                                           f"reflective injection artifact (bases={[hex(p) for p in list(new_pages)[:4]]})"})
                _baseline_exec_pages = cur_pages   # absorb after alerting (extensions legitimately grow)
        except Exception as e:
            logger.debug("self_integrity: check error: %s", e)
        healthy = len(findings) == 0
        status = {"source": "self_integrity", "type": "self_integrity_status",
                  "severity": 1.0, "healthy": healthy,
                  "clean_planes": 4 - len(findings), "ts": time.time()}
        _to_dashboard(status)
        if findings:
            event = {"source": "self_integrity", "type": "self_integrity_violation",
                     "severity": 10.0, "findings": findings,
                     "attck": ["T1014", "T1055", "T1036"], "ts": time.time()}
            logger.critical("SELF_INTEGRITY VIOLATION: %s", findings)
            await _dispatch(event)
        else:
            logger.debug("self_integrity: all 4 integrity planes clean")


async def start(correlator=None):
    global _correlator, _baseline_text, _baseline_mods, _baseline_exec_pages
    _correlator = correlator
    if not _IS_WINDOWS:
        logger.warning("SELF_INTEGRITY: non-Windows — dormant")
        await asyncio.Event().wait(); return
    loop = asyncio.get_running_loop()
    await asyncio.sleep(_BASELINE_DELAY)          # let imports stabilize first
    _baseline_text = await loop.run_in_executor(None, _text_hash)
    _baseline_mods = await loop.run_in_executor(None, _module_hashes)
    _baseline_exec_pages = await loop.run_in_executor(None, _exec_pages)
    _to_dashboard({"source": "self_integrity", "type": "self_integrity_status",
                   "severity": 1.0, "healthy": True, "clean_planes": 4, "ts": time.time()})
    logger.info("SELF_INTEGRITY: armed — interpreter .text=%s, %d JARVIS modules, "
                "%d baseline exec pages, canary armed",
                (_baseline_text or "N/A")[:12], len(_baseline_mods), len(_baseline_exec_pages))
    await _check_loop()
