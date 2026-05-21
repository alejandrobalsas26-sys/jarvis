"""
tools/binary_inverter.py — Automated Binary Inversion & Triage Engine (v24.0).

_re_pool is a dedicated ProcessPoolExecutor(max_workers=1).
Worker function _reverse_engineer_worker must be module-level for Windows
multiprocessing pickling compatibility.
broadcast_fn is NOT passed to the worker — only plain dicts cross the boundary.
"""

import asyncio
import math
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from loguru import logger

from core.events import make_event

_re_pool = ProcessPoolExecutor(max_workers=1)

_SUSPICIOUS_APIS: frozenset[str] = frozenset({
    "VirtualAlloc", "VirtualAllocEx", "VirtualProtect",
    "WriteProcessMemory", "ReadProcessMemory",
    "CreateRemoteThread", "CreateRemoteThreadEx",
    "NtMapViewOfSection", "NtUnmapViewOfSection",
    "NtCreateSection", "NtWriteVirtualMemory",
    "SetWindowsHookEx", "SetThreadContext",
    "GetProcAddress", "LoadLibraryA", "LoadLibraryW", "LoadLibraryExA",
    "OpenProcess", "OpenThread",
    "RegSetValueEx", "RegCreateKeyEx",
    "CreateService", "OpenSCManager",
    "ShellExecuteA", "ShellExecuteW", "WinExec",
    "URLDownloadToFile", "URLDownloadToCacheFile",
    "InternetOpen", "InternetConnect", "HttpOpenRequest",
    "WSASocket", "socket", "bind", "connect", "WSAStartup",
    "CryptEncrypt", "CryptDecrypt", "CryptImportKey",
    "IsDebuggerPresent", "CheckRemoteDebuggerPresent",
    "OutputDebugString",
})

_SUSPICIOUS_STR_RE = re.compile(
    r"(?i)("
    r"https?://"
    r"|cmd\.exe|powershell\.exe|wscript\.exe|cscript\.exe|mshta\.exe"
    r"|\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_$]"
    r"|HKEY_(LOCAL_MACHINE|CURRENT_USER|CLASSES_ROOT)"
    r"|\\Run\\|\\RunOnce\\"
    r"|meterpreter|beacon|shellcode|payload"
    r"|base64_decode|FromBase64|[A-Za-z0-9+/]{40,}={0,2}"
    r"|\\x[0-9a-fA-F]{2}(\\x[0-9a-fA-F]{2}){7,}"
    r")"
)

_ASCII_STR_RE = re.compile(rb"[ -~]{6,}")


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    from collections import Counter
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _reverse_engineer_worker(file_path: str) -> dict:
    """Runs in isolated worker process — PE parsing, Capstone disassembly, string extraction."""
    import gc
    import pefile    # type: ignore[import]
    import capstone  # type: ignore[import]

    result: dict = {
        "file_name":          Path(file_path).name,
        "entropy":            0.0,
        "packed":             False,
        "suspicious_imports": [],
        "recovered_strings":  [],
        "disasm_notes":       [],
        "section_entropy":    {},
    }

    try:
        raw = Path(file_path).read_bytes()
    except Exception as exc:
        result["error"] = f"Read error: {exc}"
        return result

    pe = None
    try:
        pe = pefile.PE(data=raw, fast_load=False)

        imports_found: list[str] = []
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_IMPORT:
                for imp in entry.imports:
                    name = imp.name.decode("utf-8", errors="replace") if imp.name else ""
                    if name in _SUSPICIOUS_APIS:
                        imports_found.append(name)

        result["suspicious_imports"] = sorted(set(imports_found))

        section_entropy: dict[str, float] = {}
        max_entropy = 0.0
        for sec in pe.sections:
            sec_name = sec.Name.rstrip(b"\x00").decode("utf-8", errors="replace")
            data     = sec.get_data()
            ent      = _shannon_entropy(data)
            section_entropy[sec_name] = round(ent, 3)
            if ent > max_entropy:
                max_entropy = ent

        result["entropy"]         = round(max_entropy, 3)
        result["section_entropy"] = section_entropy
        result["packed"]          = max_entropy > 7.0

        text_section = next(
            (s for s in pe.sections if b".text" in s.Name or b"CODE" in s.Name),
            None,
        )
        if text_section is not None:
            text_data = text_section.get_data()
            text_rva  = pe.OPTIONAL_HEADER.ImageBase + text_section.VirtualAddress
            cs = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
            cs.detail = True

            xor_count = gpa_chain = jmp_reg_count = 0
            limit = 5000

            for insn in cs.disasm(text_data, text_rva):
                limit -= 1
                if limit <= 0:
                    break
                if insn.id == capstone.x86.X86_INS_XOR:
                    ops = insn.operands
                    if len(ops) == 2 and ops[0].type != ops[1].type:
                        xor_count += 1
                if insn.id == capstone.x86.X86_INS_CALL:
                    if insn.op_str and "GetProcAddress" in insn.op_str:
                        gpa_chain += 1
                if insn.id in (capstone.x86.X86_INS_JMP, capstone.x86.X86_INS_CALL):
                    ops = insn.operands
                    if ops and ops[0].type == capstone.x86.X86_OP_REG:
                        jmp_reg_count += 1

            notes: list[str] = []
            if xor_count > 8:
                notes.append(f"XOR decryption loop detected ({xor_count} XOR reg,imm)")
            if gpa_chain > 2:
                notes.append(f"Dynamic API rebuild via GetProcAddress ({gpa_chain} calls)")
            if jmp_reg_count > 15:
                notes.append(f"Indirect dispatch pattern ({jmp_reg_count} JMP/CALL-to-register)")
            result["disasm_notes"] = notes

    except pefile.PEFormatError:
        result["disasm_notes"] = ["Not a valid PE file — raw shellcode or data file"]
    except Exception as exc:
        result["disasm_notes"] = [f"Analysis error: {exc}"]
    finally:
        if pe is not None:
            pe.close()
        del pe
        gc.collect()

    try:
        candidates = [s.decode("ascii", errors="replace") for s in _ASCII_STR_RE.findall(raw)]
        suspicious = [s for s in candidates if _SUSPICIOUS_STR_RE.search(s)]
        result["recovered_strings"] = suspicious[:40]
    except Exception as exc:
        result["recovered_strings"] = [f"String extraction error: {exc}"]

    return result


async def execute_automated_triage(file_path: str, broadcast_fn) -> None:
    """Async entry point: submit binary to _re_pool, broadcast results."""
    loop = asyncio.get_running_loop()
    await broadcast_fn(make_event(
        "binary_inversion_start",
        file_name=Path(file_path).name,
    ))
    try:
        result = await loop.run_in_executor(_re_pool, _reverse_engineer_worker, file_path)
        await broadcast_fn(make_event(
            "binary_inversion_complete",
            file_name=result["file_name"],
            entropy=result["entropy"],
            packed=result["packed"],
            suspicious_imports=result["suspicious_imports"],
            recovered_strings=result["recovered_strings"],
            disasm_notes=result.get("disasm_notes", []),
            section_entropy=result.get("section_entropy", {}),
        ))
    except Exception as exc:
        logger.error(f"Binary triage failed for {file_path}: {exc}")
        await broadcast_fn(make_event("error", error=f"Binary triage failed: {exc}"))
