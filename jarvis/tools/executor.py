"""
tools/executor.py — Hardened Executor v14.0 (Async + NATO Vocal MFA).

Security layers:
  Layer 1 — Allowlist + flag blocking: only approved binaries; rejects
             -EncodedCommand (PS) and python -c (inline eval).
  Layer 2 — Path canonicalization: Path.resolve() on every token; blocks
             paths under C:/Windows, System32, or root /.
  Layer 3 — Async NATO OTP: _challenge() awaits an asyncio.Queue fed by a
             background STT thread via loop.call_soon_threadsafe. 30s timeout.
  Layer 4 — Execution in run_in_executor: subprocess.run never blocks the
             event loop.
  Layer 5 — shell=False always. No exceptions.
  Layer 6 — Regex validation on all network inputs (domains, IPs).
"""

import asyncio
import json
import math
import random
import re
import sys
import os
import shlex
import subprocess
import platform
import shutil
import http.server
import socketserver
import threading
import webbrowser
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import psutil
import requests
from loguru import logger
import whois
import dns.resolver

# ── NATO Vocal MFA ───────────────────────────────────────────────────────────
_NATO_ALPHABET: tuple[str, ...] = (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
    "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima",
    "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo",
    "Sierra", "Tango", "Uniform", "Victor", "Whiskey", "Xray",
    "Yankee", "Zulu",
)
_VOCAL_CONFIRM_WORDS: frozenset[str] = frozenset({
    "hazlo", "sí", "si", "yes", "confirmar", "autorizar", "ejecutar", "adelante", "execute",
})
_CONFIDENCE_THRESHOLD = 0.75

# ── Layer 1: Allowlist de ejecutables permitidos ──────────────────────────────
COMMAND_ALLOWLIST: frozenset[str] = frozenset({
    # Networking (diagnóstico / reconocimiento)
    "ping", "nmap", "whois", "traceroute", "tracert",
    "netstat", "ipconfig", "ifconfig", "arp",
    "curl", "wget",
    # Información del sistema (lectura)
    "ps", "top", "htop", "tasklist",
    "df", "du", "free",
    "uname", "hostname", "whoami", "id",
    # Dev tools
    "python", "python3", "pip", "pip3",
    "git", "node", "npm",
    "gcc", "make",
    # Navegación de archivos (lectura)
    "ls", "dir", "cat", "type", "more",
    "grep", "find", "findstr",
    "head", "tail", "wc",
    # Miscelánea segura
    "echo", "ssh", "scp", "openssl",
})

# ── RedTeamShellExecutor: hard-block patterns (no OTP override) ───────────────
_CRITICAL_BLOCK: list[str] = [
    "rm -rf /",
    "del /f /s /q c:\\windows",
    "format c:",
    "dd if=/dev/zero",
    "> /dev/sda",
]

# Patterns that elevate a command to requires_challenge=True
_SUSPICIOUS_PATTERNS: list[str] = [
    "-encodedcommand", "-enc",
    "invoke-expression", "iex",
    "virtualalloc", "writeprocessmemory",
    "net user", "net localgroup",
]

# ── Layer 1: Flags bloqueados (evasión de policy) ────────────────────────────
_BLOCKED_FLAGS: frozenset[str] = frozenset({
    "-encodedcommand", "-enc",  # PowerShell encoded command execution
    "--encoded",
})
_PYTHON_EXECUTABLES: frozenset[str] = frozenset({"python", "python3"})

# ── Layer 2: Directorios del sistema bloqueados ───────────────────────────────
def _build_system_dirs() -> frozenset[Path]:
    dirs: set[Path] = {Path("/").resolve()}
    if os.name == "nt":
        sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        dirs.add(sysroot.resolve())
        dirs.add((sysroot / "System32").resolve())
        dirs.add(Path(r"C:\Windows").resolve())
        dirs.add(Path(r"C:\Windows\System32").resolve())
    else:
        dirs.update({
            Path("/etc").resolve(),
            Path("/sys").resolve(),
            Path("/proc").resolve(),
            Path("/boot").resolve(),
        })
    return frozenset(dirs)

_SYSTEM_DIRS: frozenset[Path] = _build_system_dirs()

# Metacaracteres de shell: si aparecen en el input, el comando se rechaza
_FORBIDDEN_CHARS_RE = re.compile(r'[;&|`$<>()\{\}!\\\n\r]')

# Regex para hosts válidos: IP, CIDR, hostname (sin metacaracteres)
_SAFE_HOST_RE = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*'
    r'[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?'
    r'(?:/\d{1,2})?$'
)

# Regex para dominios: solo alfanuméricos, puntos y guiones
_SAFE_DOMAIN_RE = re.compile(r'^[a-zA-Z0-9._-]{1,253}$')

_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'none';\">"
)

# Tools cuya seguridad es read-only/benign — no requieren desafío HITL.
# run_shell_command fue removido: ahora pasa por el desafío NATO vocal.
_HITL_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "get_datetime",
    "get_weather",
    "web_search",
    "fetch_webpage",
    "system_info",
    "list_processes",
    "list_directory",
    "read_file",
    "leer_archivo_universal",
    "escanear_pantalla",
    "analizar_codigo_sast",
    "get_clipboard",
    "check_connectivity",
    "whois_lookup",
    "consultar_base_conocimiento",
    "estudiar_tema",
    "get_system_status",
    "ingest_docs",
    "query_knowledge",
})

# Patrones SAST precompilados para _tool_analizar_codigo_sast
_SAST_PATTERNS: list[tuple] = [
    (re.compile(r'eval\('),                                            "eval() detectado"),
    (re.compile(r'exec\('),                                            "exec() detectado"),
    (re.compile(r'os\.system\('),                                      "os.system() detectado"),
    (re.compile(r'(?i)(password|api_key|secret|token)\s*=\s*["\']'),  "Credencial hardcodeada"),
    (re.compile(r'(?i)SELECT\s+.+\s+WHERE\s+.+=\s*%s'),               "Concatenación SQL raw"),
]

# Map app_name → executable: el input del usuario solo se usa como clave de lookup,
# nunca se interpola en el comando. shell=False siempre.
_APP_SOFTWARE_MAP: dict[str, str] = {
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "access": "msaccess",
    "outlook": "outlook",
    "autocad": "acad",
    "packet tracer": "PacketTracer",
    "blender": "blender",
    "chrome": "chrome",
    "firefox": "firefox",
    "vscode": "code",
    "notepad": "notepad",
    "calculator": "calc",
    "paint": "mspaint",
    "vlc": "vlc",
    "obs": "obs64",
    "spotify": "Spotify",
    "discord": "Discord",
    "teams": "Teams",
    "zoom": "Zoom",
    "gimp": "gimp-2.10",
    "inkscape": "inkscape",
    "wireshark": "Wireshark",
    "burpsuite": "burpsuite",
    "virtualbox": "VirtualBox",
    "vmware": "vmware",
}


# ── Guardrail: forbidden destructive patterns on system directories ───────────
_GUARDRAIL_SYSTEM_WRITE_RE = re.compile(
    r'(?i)(?:'
    r'(?:del|rm|erase|rd|rmdir)\s+.*?(?:C:[/\\]Windows|System32)'
    r'|(?:copy|move|xcopy|robocopy|cp|mv)\s+.*?\s+(?:C:[/\\]Windows|System32)'
    r'|reg\s+(?:add|delete|import)\b'
    r')'
)

_GUARDRAIL_ROOT_DELETE_RE = re.compile(
    r'(?i)(?:'
    r'rm\s+-[frRFR]+\s+[/\\]\s*(?:\s|$)'
    r'|del\s+(?:/[sqSQ]\s+)*C:[/\\]\s*(?:\s|$)'
    r'|rd\s+(?:/[sqSQ]\s+)*C:[/\\]\s*(?:\s|$)'
    r'|rmdir\s+(?:/[sqSQ]\s+)*C:[/\\]\s*(?:\s|$)'
    r'|format\s+[cCdD]:'
    r')'
)

# ── PII detection in tool outputs ─────────────────────────────────────────────
_PII_OUTPUT_RE = re.compile(
    r'(?i)(?:'
    r'(?:password|passwd|contraseña)\s*[:=]\s*\S{4,}'
    r'|(?:api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]\s*[A-Za-z0-9_\-]{16,}'
    r'|(?:bearer\s+token|auth[_-]?token)\s*[:=]\s*[A-Za-z0-9_\-\.]{20,}'
    r')'
)


def _validate_command(command: str) -> tuple[bool, str, list[str], str, str]:
    """
    Valida y parsea un comando contra la allowlist (Layers 1 & 2).

    Returns:
        (is_valid, error_message, argv, resolved_path, binary_status)
        Si is_valid es False, argv es [] y resolved_path/binary_status son "".
    """
    if not command.strip():
        return False, "Comando vacío.", [], "", ""

    # Layer 1a: Bloquear metacaracteres de shell antes de parsear
    if _FORBIDDEN_CHARS_RE.search(command):
        return (
            False,
            "Comando rechazado: contiene metacaracteres de shell prohibidos "
            f"({_FORBIDDEN_CHARS_RE.pattern}).",
            [], "", "",
        )

    # Layer 1b: Parseo seguro con shlex
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return False, f"Comando malformado: {e}", [], "", ""

    if not argv:
        return False, "Comando vacío tras parseo.", [], "", ""

    # Layer 1c: Normalizar el nombre del ejecutable (quitar ruta y .exe en Windows)
    executable = Path(argv[0]).name.lower().removesuffix(".exe")

    # Layer 1d: Verificar contra la allowlist
    if executable not in COMMAND_ALLOWLIST:
        return (
            False,
            f"Ejecutable '{executable}' no está en la allowlist.\n"
            f"Permitidos: {', '.join(sorted(COMMAND_ALLOWLIST))}",
            [], "", "blocked",
        )
    binary_status = "allowlist_ok"

    # Layer 1e: Bloquear flags de evasión (-EncodedCommand, python -c, etc.)
    for arg in argv[1:]:
        if arg.lower() in _BLOCKED_FLAGS:
            return (
                False,
                f"Flag '{arg}' bloqueado — evasión de política de ejecución.",
                [], "", "blocked",
            )
        if executable in _PYTHON_EXECUTABLES and arg == "-c":
            return (
                False,
                "python -c bloqueado — ejecución de código inline no permitida.",
                [], "", "blocked",
            )

    # Layer 2: Canonicalización de rutas — bloquear acceso a directorios del sistema
    resolved_path = ""
    for token in argv[1:]:
        is_path_like = "/" in token or "\\" in token or (
            len(token) >= 3 and token[1:3] in (":/", ":\\")
        )
        if not is_path_like:
            continue
        try:
            resolved = Path(token).resolve()
            resolved_path = str(resolved)
            for sys_dir in _SYSTEM_DIRS:
                if resolved == sys_dir or sys_dir in resolved.parents:
                    return (
                        False,
                        f"Ruta bloqueada: '{resolved}' apunta a un directorio del sistema.",
                        [], resolved_path, "blocked",
                    )
        except Exception:
            pass

    return True, "", argv, resolved_path, binary_status


async def _aura_broadcast(event: dict) -> None:
    """Fire-and-forget telemetry to the AURA WebSocket pipeline.

    Imported lazily so executor.py has no hard dependency on the AURA module —
    Jarvis works in text mode even when FastAPI/uvicorn are not installed.
    """
    try:
        from aura.server import broadcast
        await broadcast(event)
    except Exception:
        pass


class ToolExecutor:
    def __init__(
        self,
        stt_queue: "asyncio.Queue | None" = None,
        stt_listener=None,
    ) -> None:
        self._active_web_server: socketserver.TCPServer | None = None
        self._active_web_thread: threading.Thread | None = None
        self._stt_queue = stt_queue        # asyncio.Queue[(str, float)] | None
        self._stt_listener = stt_listener  # HighPrioritySTTListener | None
        from core.governance import TacticAuditLogger
        self._audit = TacticAuditLogger()

    # ── Layer 3: NATO Vocal MFA ───────────────────────────────────────────────

    def _evaluate_nato_response(
        self, text: str, confidence: float, challenge_word: str
    ) -> bool:
        """Return True if the spoken response matches the NATO challenge word."""
        if confidence < _CONFIDENCE_THRESHOLD:
            logger.warning(f"VAP NATO: confianza baja ({confidence:.2%}) — denegado.")
            return False
        words = set(
            text.lower()
            .replace(",", "").replace(".", "").replace("¡", "").replace("!", "")
            .split()
        )
        if challenge_word.lower() in words:
            logger.info(f"VAP NATO: '{challenge_word}' confirmado (conf={confidence:.2%}).")
            return True
        if words & _VOCAL_CONFIRM_WORDS:
            logger.info(f"VAP: Confirmación general (conf={confidence:.2%}).")
            return True
        logger.warning(f"VAP NATO: Respuesta inválida '{text}' para '{challenge_word}'.")
        return False

    async def _challenge(self, tool_name: str, preview: str) -> tuple[bool, str]:
        """
        Layer 3 — Async NATO OTP gate.

        Displays a random NATO word challenge, starts a background audio-capture
        thread that pushes (text, confidence) into self._stt_queue via
        loop.call_soon_threadsafe, then awaits the result with a 30-second
        timeout. Falls back to keyboard via run_in_executor on timeout or when
        STT is unavailable.

        Returns:
            (granted: bool, auth_audit: str)
        """
        challenge_word = random.choice(_NATO_ALPHABET)
        bar = "=" * 62
        print(f"\n{bar}")
        print(f"  [!] AUTORIZACIÓN NATO REQUERIDA")
        print(f"      Tool      : {tool_name.upper()}")
        print(f"      Parámetros: {preview}")
        print(f"  >> DESAFÍO: Di la palabra NATO [{challenge_word.upper()}] para autorizar <<")
        print(f"  (o presiona 'y' en el teclado | timeout: 30s)")
        print(f"{bar}")
        sys.stdout.flush()

        loop = asyncio.get_running_loop()

        # Broadcast the challenge so the AURA UI can display it
        await _aura_broadcast({
            "type": "challenge",
            "tool": tool_name,
            "challenge_word": challenge_word,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if self._stt_queue is not None and self._stt_listener is not None:
            def _capture_and_push() -> None:
                try:
                    audio = self._stt_listener.record()
                    text, confidence = self._stt_listener.transcribe_with_confidence(audio)
                    loop.call_soon_threadsafe(
                        self._stt_queue.put_nowait, (text, confidence)
                    )
                except Exception as exc:
                    logger.warning(f"VAP: Error de captura — {exc}")
                    loop.call_soon_threadsafe(
                        self._stt_queue.put_nowait, ("", 0.0)
                    )

            threading.Thread(
                target=_capture_and_push, daemon=True, name="vap-capture"
            ).start()

            try:
                text, confidence = await asyncio.wait_for(
                    self._stt_queue.get(), timeout=30.0
                )
                granted = self._evaluate_nato_response(text, confidence, challenge_word)
                auth_audit = (
                    f"vocal:nato:{challenge_word}:{confidence:.2f}:"
                    f"{'granted' if granted else 'denied'}"
                )
                if not granted:
                    logger.warning(f"VAP NATO: Denegado — texto='{text}'")
                return granted, auth_audit
            except asyncio.TimeoutError:
                logger.info("VAP: Timeout vocal — fallback a teclado.")

        # Keyboard fallback — runs in thread pool so event loop stays free
        auth = await loop.run_in_executor(
            None, lambda: input("  ¿Autorizar ejecución? (y/N): ")
        )
        granted = auth.strip().lower() == "y"
        return granted, f"keyboard:{'granted' if granted else 'denied'}"

    # ── Async executor gate ───────────────────────────────────────────────────

    async def execute(self, tool_name: str, tool_input: dict, reasoning: str = "") -> Any:
        """
        Fully async execution gate:
          1. Look up handler.
          2. Apply guardrails.
          3. NATO vocal challenge (Layer 3) for non-exempt tools.
          4. Run handler in thread-pool executor (Layer 4 — no event-loop blocking).
        """
        loop = asyncio.get_running_loop()
        self._loop = loop          # expose to sync handlers for fire-and-forget broadcasts
        handler = getattr(self, f"_tool_{tool_name}", None)

        if handler is None:
            self._audit.log_action(
                tool_name, reasoning, "unknown", "error", "Tool no implementada"
            )
            return {"error": f"Tool '{tool_name}' no implementada."}

        guardrail_block = self._validate_guardrails(tool_name, tool_input)
        if guardrail_block:
            self._audit.log_action(
                tool_name, reasoning, "blocked:guardrail", "blocked",
                guardrail_block.get("error", "")[:200],
            )
            return guardrail_block

        auth_audit = "hitl_exempt"
        if tool_name not in _HITL_EXEMPT_TOOLS:
            preview = str(tool_input)
            if len(preview) > 200:
                preview = preview[:200] + "…"
            granted, auth_audit = await self._challenge(tool_name, preview)
            if not granted:
                logger.warning(f"HITL: '{tool_name}' denegada.")
                self._audit.log_action(
                    tool_name, reasoning, auth_audit, "blocked", "Ejecución cancelada"
                )
                return {"error": "Ejecución cancelada por el usuario."}

        # Broadcast: tool is about to execute
        await _aura_broadcast({
            "type": "tool_invoked",
            "tool": tool_name,
            "auth_audit": auth_audit,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        try:
            # Layer 4: run synchronous tool handler in thread pool
            result = await loop.run_in_executor(None, lambda: handler(**tool_input))
            status = "error" if isinstance(result, dict) and "error" in result else "success"
            output_summary = json.dumps(result, ensure_ascii=False, default=str)[:200]
            result = self._check_pii_output(result)
            self._audit.log_action(tool_name, reasoning, auth_audit, status, output_summary)

            # Broadcast: tool result
            await _aura_broadcast({
                "type": "tool_result",
                "tool": tool_name,
                "status": status,
                "auth_audit": auth_audit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": output_summary[:150],
            })

            return result
        except Exception as e:
            logger.error(f"Error en tool '{tool_name}': {e}")
            self._audit.log_action(tool_name, reasoning, auth_audit, "error", str(e)[:200])
            await _aura_broadcast({
                "type": "error",
                "tool": tool_name,
                "message": str(e)[:200],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return {"error": str(e)}

    def _validate_guardrails(self, tool_name: str, tool_input: dict) -> dict | None:
        """Returns an error dict if the action violates a security guardrail, else None."""
        if tool_input.get("FORCE_OVERRIDE"):
            return None

        combined = " ".join(str(v) for v in tool_input.values())

        if _GUARDRAIL_ROOT_DELETE_RE.search(combined):
            logger.warning(
                f"Guardrail: eliminación de raíz bloqueada — tool='{tool_name}' "
                f"input={combined[:80]!r}"
            )
            return {
                "error": (
                    "GUARDRAIL: operación bloqueada — intento de eliminar un directorio raíz "
                    "detectado. Pasa FORCE_OVERRIDE=true para anular (solo uso autorizado)."
                )
            }

        if _GUARDRAIL_SYSTEM_WRITE_RE.search(combined):
            logger.warning(
                f"Guardrail: escritura en ruta del sistema bloqueada — tool='{tool_name}' "
                f"input={combined[:80]!r}"
            )
            return {
                "error": (
                    "GUARDRAIL: operación bloqueada — modificación de C:\\Windows o System32 "
                    "no permitida. Pasa FORCE_OVERRIDE=true para anular (solo uso autorizado)."
                )
            }

        return None

    def _check_pii_output(self, result: Any) -> Any:
        """Inject a PII warning key if the result contains cleartext credentials."""
        if not isinstance(result, dict):
            return result
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        if _PII_OUTPUT_RE.search(result_str):
            logger.warning("PII detectado en output de tool — advertencia inyectada.")
            result["_pii_warning"] = (
                "ADVERTENCIA DE SEGURIDAD: Este resultado puede contener credenciales "
                "en texto claro (contraseñas, API keys o tokens). "
                "Revisa antes de mostrar o registrar en cualquier sistema externo."
            )
        return result

    def _cleanup_web_server(self) -> None:
        if self._active_web_server is not None:
            self._active_web_server.shutdown()
            self._active_web_server.server_close()
            if self._active_web_thread is not None:
                self._active_web_thread.join(timeout=3.0)
            self._active_web_server = None
            self._active_web_thread = None

    # ── Tiempo / Clima ────────────────────────────────────────────────────────

    def _tool_get_datetime(self) -> dict:
        now = datetime.now()
        return {
            "date": now.strftime("%A, %d de %B de %Y"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
        }

    def _tool_get_weather(self, city: str) -> dict:
        try:
            resp = requests.get(
                f"https://wttr.in/{requests.utils.quote(city)}?format=j1&lang=es",
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            current = data["current_condition"][0]
            return {
                "city": city,
                "temp_c": current["temp_C"],
                "feels_like_c": current["FeelsLikeC"],
                "humidity": current["humidity"],
                "description": current["weatherDesc"][0]["value"],
                "wind_kmph": current["windspeedKmph"],
            }
        except Exception as e:
            return {"error": str(e)}

    # ── Lectura de archivos ───────────────────────────────────────────────────

    def _tool_read_file(self, path: str, max_chars: int = 8000) -> dict:
        """[SANDBOXED] Lee archivos solo dentro de Downloads, Documents o el proyecto."""
        p = Path(path).expanduser().resolve()
        allowed_dirs = [
            Path.home() / "Downloads",
            Path.home() / "Documents",
            Path.cwd(),
        ]
        is_allowed = any(p.is_relative_to(a.resolve()) for a in allowed_dirs)
        if not is_allowed:
            logger.warning(f"Intento de lectura bloqueado: {p}")
            return {"error": "Seguridad: No tengo permiso para leer fuera de Downloads o Documents."}
        if not p.exists():
            return {"error": f"Archivo no encontrado: {path}"}

        ext = p.suffix.lower()
        try:
            if ext == ".pdf":
                content = self._read_pdf(p)
            elif ext in (".docx", ".doc"):
                content = self._read_docx(p)
            elif ext in (".xlsx", ".xls"):
                content = self._read_xlsx(p)
            elif ext in (".pptx", ".ppt"):
                content = self._read_pptx(p)
            elif ext in (
                ".txt", ".md", ".py", ".js", ".sh", ".yaml", ".yml",
                ".json", ".xml", ".html", ".css", ".log", ".c",
                ".cpp", ".h", ".java", ".go", ".rs", ".env", ".csv",
            ):
                content = p.read_text(encoding="utf-8", errors="ignore")
            elif ext == ".rtf":
                content = self._read_rtf(p)
            elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"):
                content = self._read_image_ocr(p)
            else:
                return {"error": f"Extensión '{ext}' no soportada."}

            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n[...truncado a {max_chars} chars]"

            return {
                "file": str(p.name),
                "extension": ext,
                "size_kb": round(p.stat().st_size / 1024, 2),
                "content": content,
                "chars": len(content),
            }
        except Exception as e:
            return {"error": f"Error leyendo {path}: {e}"}

    def _read_pdf(self, path: Path) -> str:
        import pdfplumber
        parts = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if text:
                    parts.append(f"[Pag {i}]\n{text}")
                for table in page.extract_tables():
                    rows = [" | ".join(str(c) for c in row if c) for row in table if row]
                    parts.append("[Tabla]\n" + "\n".join(rows))
        return "\n\n".join(parts)

    def _read_docx(self, path: Path) -> str:
        from docx import Document
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)

    def _read_xlsx(self, path: Path) -> str:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for name in wb.sheetnames:
            parts.append(f"[Hoja: {name}]")
            for row in wb[name].iter_rows(values_only=True):
                if any(c is not None for c in row):
                    parts.append(" | ".join(str(c) if c is not None else "" for c in row))
        return "\n".join(parts)

    def _read_pptx(self, path: Path) -> str:
        from pptx import Presentation
        prs = Presentation(str(path))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [
                s.text.strip()
                for s in slide.shapes
                if hasattr(s, "text") and s.text.strip()
            ]
            if texts:
                parts.append(f"[Slide {i}]\n" + "\n".join(texts))
        return "\n\n".join(parts)

    def _read_rtf(self, path: Path) -> str:
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(path.read_text(errors="ignore"))

    def _read_image_ocr(self, path: Path) -> str:
        try:
            import pytesseract
            from PIL import Image
            return pytesseract.image_to_string(Image.open(str(path)), lang="spa+eng")
        except Exception:
            pass
        try:
            import easyocr
            reader = easyocr.Reader(["es", "en"], gpu=False)
            return " ".join(reader.readtext(str(path), detail=0))
        except Exception as e:
            return f"OCR no disponible: {e}"

    def _tool_list_directory(self, path: str = ".", pattern: str = "*") -> dict:
        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"Directorio no encontrado: {path}"}
        files = [
            {
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "ext": item.suffix.lower(),
                "size_kb": round(item.stat().st_size / 1024, 2) if item.is_file() else 0,
            }
            for item in sorted(p.glob(pattern))
        ]
        return {"path": str(p.resolve()), "items": files, "count": len(files)}

    # ── Lectura Universal y SAST ──────────────────────────────────────────────

    def _tool_leer_archivo_universal(self, filepath: str) -> dict:
        """Lee archivos multiformato con truncamiento estricto de 4000 chars (Single Channel VRAM)."""
        _MAX_CHARS = 4000
        p = Path(filepath).expanduser().resolve()
        if not p.exists():
            return {"error": f"Archivo no encontrado: {filepath}"}

        ext = p.suffix.lower()
        try:
            if ext == ".pdf":
                import pdfplumber
                parts = []
                with pdfplumber.open(p) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text() or ""
                        if text:
                            parts.append(text)
                content = "\n\n".join(parts)
            elif ext in (".docx", ".doc"):
                from docx import Document
                doc = Document(str(p))
                content = "\n".join(para.text for para in doc.paragraphs if para.text.strip())
            elif ext in (".csv", ".xlsx", ".xls"):
                import pandas as pd
                if ext == ".csv":
                    df = pd.read_csv(p, nrows=50)
                else:
                    df = pd.read_excel(p, nrows=50)
                content = df.to_string(index=False)
            else:
                content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return {"error": f"Error leyendo {filepath}: {e}"}

        result: dict = {
            "file": p.name,
            "extension": ext,
            "chars": min(len(content), _MAX_CHARS),
            "content": content[:_MAX_CHARS],
        }
        if len(content) > _MAX_CHARS:
            result["aviso"] = (
                f"Texto truncado a {_MAX_CHARS} caracteres para proteger la memoria (VRAM)."
            )
        return result

    def _tool_analizar_codigo_sast(self, filepath: str) -> dict:
        """Análisis estático ligero (regex-based SAST) sin dependencias externas."""
        _MAX_FINDINGS = 15
        p = Path(filepath).expanduser().resolve()
        if not p.exists():
            return {"error": f"Archivo no encontrado: {filepath}"}

        findings: list[dict] = []
        possibly_more = False
        try:
            with p.open(encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    for pattern, label in _SAST_PATTERNS:
                        if pattern.search(line):
                            findings.append({
                                "linea": lineno,
                                "tipo": label,
                                "codigo": line.rstrip(),
                            })
                            break
                    if len(findings) >= _MAX_FINDINGS:
                        possibly_more = True
                        break
        except Exception as e:
            return {"error": f"Error analizando {filepath}: {e}"}

        if not findings:
            return {"status": "limpio", "archivo": p.name, "hallazgos": 0}

        result: dict = {"archivo": p.name, "hallazgos": len(findings), "resultados": findings}
        if possibly_more:
            result["aviso"] = "Límite de 15 hallazgos alcanzado; puede haber más en el resto del archivo."
        return result

    # ── Búsqueda Web ──────────────────────────────────────────────────────────

    def _tool_web_search(self, query: str, max_results: int = 5) -> dict:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddg:
                results = list(ddg.text(query, max_results=max_results))
            return {
                "query": query,
                "results": [
                    {"title": r["title"], "url": r["href"], "snippet": r["body"]}
                    for r in results
                ],
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_fetch_webpage(self, url: str, max_chars: int = 5000) -> dict:
        try:
            from bs4 import BeautifulSoup
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            return {"url": url, "content": text[:max_chars]}
        except Exception as e:
            return {"error": str(e)}

    # ── Sistema ───────────────────────────────────────────────────────────────

    def _tool_system_info(self) -> dict:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
        return {
            "cpu_percent": cpu,
            "ram_total_gb": round(ram.total / 1e9, 2),
            "ram_used_gb": round(ram.used / 1e9, 2),
            "ram_percent": ram.percent,
            "disk_total_gb": round(disk.total / 1e9, 2),
            "disk_percent": disk.percent,
            "uptime_hours": round(uptime.total_seconds() / 3600, 1),
            "os": platform.system(),
            "hostname": platform.node(),
        }

    def _tool_list_processes(self, filter_name: str = "") -> dict:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                if filter_name.lower() in p.info["name"].lower():
                    procs.append(p.info)
            except Exception:
                pass
        procs.sort(key=lambda x: x.get("cpu_percent", 0), reverse=True)
        return {"processes": procs[:30]}

    def _tool_kill_process(self, name: str) -> dict:
        import psutil
        killed = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if name.lower() in p.info["name"].lower():
                    p.kill()
                    killed.append(p.info["name"])
            except Exception:
                pass
        return {"killed": killed}

    def _tool_run_shell_command(self, command: str) -> dict:
        """
        [ALLOWLIST + Layer1 flags + Layer2 canonicalization + shell=False]

        Authorization is handled by the outer async _challenge() NATO gate.
        This method validates the command and executes it — no blocking input().
        """
        is_valid, error_msg, argv, resolved_path, binary_status = _validate_command(command)
        if not is_valid:
            logger.warning(f"Comando bloqueado: {command!r} — {error_msg}")
            self._audit.log_action(
                "run_shell_command", "", "blocked:validation", "blocked", error_msg[:200],
                command=command, resolved_path=resolved_path, binary_status=binary_status,
            )
            # Static forensic triage — zero execution, analysis only
            try:
                from core.triage import analyze_neutralized, write_manifest

                # Layer 0: YARA scan — compiled rules are lru_cache'd (one-time cost)
                yara_hits: list[dict] = []
                try:
                    from core.yara_analyzer import _compile_rules
                    _rules = _compile_rules()
                    if _rules:
                        yara_hits = [
                            {"rule": m.rule, "namespace": m.namespace, "tags": list(m.tags)}
                            for m in _rules.match(data=command.encode())
                        ]
                        if yara_hits:
                            logger.warning(
                                f"YARA: {len(yara_hits)} hit(s) — "
                                f"{[h['rule'] for h in yara_hits]}"
                            )
                except Exception as _ye:
                    logger.warning(f"YARA scan error: {_ye}")

                triage_result  = analyze_neutralized(command, error_msg, yara_hits=yara_hits)
                manifest_path  = write_manifest(triage_result, command)
                with open("tactic_audit.jsonl", "a", encoding="utf-8") as _af:
                    _af.write(json.dumps({
                        "status":    "neutralized",
                        "command":   command,
                        "triage":    triage_result,
                        "manifest":  manifest_path.name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, ensure_ascii=False) + "\n")
                _loop = getattr(self, "_loop", None)
                if _loop and not _loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        _aura_broadcast({
                            "type":          "neutralized",
                            "command":       command,
                            "triage":        triage_result,
                            "manifest_file": manifest_path.name,
                            "timestamp":     datetime.now(timezone.utc).isoformat(),
                        }),
                        _loop,
                    )

                # SOAR: isolate public IPs that clear the entropy + MITRE AND-gate
                try:
                    from core.mitigation import should_isolate, isolate_ip
                    target_ips = should_isolate(triage_result)
                    if target_ips and _loop and not _loop.is_closed():
                        for _ip in target_ips:
                            logger.warning(f"SOAR: scheduling firewall isolation for {_ip}")

                            async def _run_isolation(_i=_ip):
                                asyncio.create_task(
                                    asyncio.shield(isolate_ip(_i, _aura_broadcast))
                                )

                            asyncio.run_coroutine_threadsafe(_run_isolation(), _loop)
                except Exception as _soar_exc:
                    logger.warning(f"SOAR pipeline error: {_soar_exc}")

                logger.info(f"Triage manifest written: {manifest_path.name}")
            except Exception as _triage_exc:
                logger.warning(f"Triage pipeline error: {_triage_exc}")
            return {"error": f"Comando bloqueado por política de seguridad: {error_msg}"}

        # Show the canonicalized argv for operator transparency
        print(f"\n    [EXEC] argv={argv}  resolved_path={resolved_path or 'n/a'}")
        sys.stdout.flush()

        try:
            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except FileNotFoundError:
            return {"error": f"Ejecutable '{argv[0]}' no encontrado en PATH."}
        except subprocess.TimeoutExpired:
            return {"error": "Timeout: el comando tardó más de 30 segundos."}
        except Exception as e:
            return {"error": str(e)}

    # ── Aplicaciones ──────────────────────────────────────────────────────────

    def _tool_open_application(self, app: str) -> dict:
        OS = platform.system()
        # Todos los valores son listas pre-definidas — el input del usuario
        # solo se usa como clave de búsqueda, nunca se interpola en el comando.
        APP_MAP: dict[str, dict[str, list[str]]] = {
            "packet tracer": {
                "Windows": [r"C:\Program Files\Cisco\Cisco Packet Tracer 8.2\PacketTracer.exe"],
                "Linux": ["packettracer"],
            },
            "wireshark":  {"Windows": ["wireshark"],    "Linux": ["wireshark"]},
            "burpsuite":  {"Windows": ["burpsuite"],    "Linux": ["burpsuite"]},
            "vscode":     {"Windows": ["code"],         "Linux": ["code"]},
            "chrome":     {"Windows": ["chrome"],       "Linux": ["google-chrome"]},
            "firefox":    {"Windows": ["firefox"],      "Linux": ["firefox"]},
            "terminal":   {"Windows": ["cmd.exe"],      "Linux": ["x-terminal-emulator"]},
            "calculator": {"Windows": ["calc.exe"],     "Linux": ["gnome-calculator"]},
            "excel":      {"Windows": ["excel.exe"],    "Linux": ["libreoffice", "--calc"]},
            "word":       {"Windows": ["winword.exe"],  "Linux": ["libreoffice", "--writer"]},
        }

        key = app.lower().strip()
        if len(key) > 64:
            return {"error": "Nombre de aplicación demasiado largo."}

        cmd_vector: list[str] | None = None
        for k, v in APP_MAP.items():
            if key in k or k in key:
                cmd_vector = v.get(OS, v.get("Linux"))
                break

        if cmd_vector is None:
            # Fuera del mapa: solo permitir ejecutables sin metacaracteres ni espacios
            if not re.match(r'^[a-zA-Z0-9._-]+$', key):
                return {"error": "Nombre de aplicación contiene caracteres no permitidos."}
            cmd_vector = [key]

        try:
            subprocess.Popen(
                cmd_vector,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"status": "opened", "app": app}
        except FileNotFoundError:
            return {"error": f"Aplicación '{cmd_vector[0]}' no encontrada en PATH."}
        except Exception as e:
            return {"error": str(e)}

    def _tool_open_software(self, app_name: str) -> dict:
        """
        [HITL] Lanza una aplicación usando un mapa pre-definido de ejecutables.
        El input del usuario solo se usa como clave de búsqueda — nunca se interpola.
        Fallback seguro: os.startfile() para aliases registrados en Windows.
        shell=False siempre.
        """
        key = app_name.lower().strip()
        if len(key) > 64:
            return {"error": "Nombre de aplicación demasiado largo."}

        # Exact match primero, luego partial match — el resultado es siempre un valor pre-definido
        executable = _APP_SOFTWARE_MAP.get(key)
        if executable is None:
            for k, v in _APP_SOFTWARE_MAP.items():
                if key in k or k in key:
                    executable = v
                    break

        # Si no está en el mapa, validar que no tenga metacaracteres antes de usarlo
        if executable is None:
            if not re.match(r'^[a-zA-Z0-9._-]+$', key):
                return {"error": "Nombre de aplicación contiene caracteres no permitidos."}
            executable = key

        try:
            subprocess.Popen(
                [executable],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"status": "launched", "app": app_name, "executable": executable}
        except FileNotFoundError:
            pass
        except Exception as e:
            return {"error": str(e)}

        # Fallback seguro para Windows: ShellExecute vía os.startfile()
        if platform.system() == "Windows":
            try:
                os.startfile(executable)
                return {"status": "launched_via_startfile", "app": app_name, "executable": executable}
            except Exception as e:
                return {"error": f"Aplicación '{executable}' no encontrada. {e}"}

        return {"error": f"Aplicación '{executable}' no encontrada en el PATH."}

    def _tool_create_document(self, doc_type: str, title: str, content: str | list) -> dict:
        """
        [HITL] Genera un archivo .docx o .pptx en ~/Downloads.
        Para pptx, fragmenta el contenido en slides de máximo 5 bullets para
        evitar overflow de texto. Retorna la ruta absoluta del archivo creado.
        """
        slug = re.sub(r'[^\w\s-]', '', title).strip()
        slug = re.sub(r'[\s-]+', '_', slug)[:60] or "documento"

        out_dir = Path.home() / "Downloads"
        out_dir.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        if isinstance(content, str):
            lines = [ln for ln in content.split('\n') if ln.strip()]
        elif isinstance(content, list):
            lines = [str(item) for item in content if str(item).strip()]

        if doc_type == "docx":
            from docx import Document
            doc = Document()
            doc.add_heading(title, level=1)
            for line in lines:
                doc.add_paragraph(line)
            out_path = out_dir / f"{slug}.docx"
            doc.save(str(out_path))
            return {"status": "created", "path": str(out_path.resolve()), "format": "docx"}

        elif doc_type == "pptx":
            from pptx import Presentation
            prs = Presentation()

            # Slide de título
            title_slide = prs.slides.add_slide(prs.slide_layouts[0])
            title_slide.shapes.title.text = title
            try:
                title_slide.placeholders[1].text = ""
            except (KeyError, IndexError):
                pass

            # Fragmentar contenido: máx 5 bullets por slide
            CHUNK = 5
            chunks = [lines[i:i + CHUNK] for i in range(0, len(lines), CHUNK)] if lines else [[]]
            content_layout = prs.slide_layouts[1]
            for idx, chunk in enumerate(chunks, 1):
                cslide = prs.slides.add_slide(content_layout)
                cslide.shapes.title.text = title if idx == 1 else f"{title} ({idx})"
                try:
                    tf = cslide.placeholders[1].text_frame
                    tf.clear()
                    for j, bullet in enumerate(chunk):
                        if j == 0:
                            tf.paragraphs[0].text = bullet
                        else:
                            tf.add_paragraph().text = bullet
                except (KeyError, IndexError):
                    pass

            out_path = out_dir / f"{slug}.pptx"
            prs.save(str(out_path))
            return {"status": "created", "path": str(out_path.resolve()), "format": "pptx"}

        else:
            return {"error": f"doc_type '{doc_type}' no soportado. Usa 'docx' o 'pptx'."}

    def _tool_desplegar_webapp(self, html_code: str) -> dict:
        self._cleanup_web_server()

        if re.search(r'<head\b', html_code, re.IGNORECASE):
            html_code = re.sub(
                r'(<head[^>]*>)',
                rf'\1\n  {_CSP_META}',
                html_code,
                flags=re.IGNORECASE,
            )
        else:
            html_code = _CSP_META + "\n" + html_code

        temp_dir = tempfile.mkdtemp(prefix="jarvis_ui_")
        (Path(temp_dir) / "index.html").write_text(html_code, encoding="utf-8")

        class JailedHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=temp_dir, **kwargs)

            def log_message(self, format, *args):
                pass

        httpd = socketserver.TCPServer(("", 0), JailedHTTPRequestHandler)
        port = httpd.server_address[1]

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        self._active_web_server = httpd
        self._active_web_thread = thread

        url = f"http://localhost:{port}/index.html"
        webbrowser.open(url)

        return {"url": url, "port": port, "temp_dir": temp_dir}

    def _tool_take_screenshot(
        self,
        save_path: str = "",
        analyze: bool = False,
        analizar_topologia: bool = False,
    ) -> dict:
        import pyautogui

        screenshot = pyautogui.screenshot()
        if not save_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = str(Path.home() / f"jarvis_{ts}.png")
        screenshot.save(save_path)
        result: dict = {"saved": save_path}
        if analyze:
            result["ocr"] = self._read_image_ocr(Path(save_path))[:2000]
        if analizar_topologia:
            result["topology_analysis"] = self._analyze_topology_vlm(Path(save_path))
        return result

    def _analyze_topology_vlm(self, image_path: Path) -> str:
        """Envía la imagen al VLM llava de Ollama para análisis de diagramas de red."""
        import base64

        try:
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")

            payload = {
                "model": "llava",
                "prompt": (
                    "Analiza este diagrama de red en detalle. "
                    "Identifica y lista: routers (con IPs si son visibles), switches, "
                    "hosts/PCs, conexiones entre dispositivos, VLANs, y la topología general. "
                    "Si hay configuraciones de red visibles (máscaras, gateways, protocolos), "
                    "inclúyelas. Responde en español de forma concisa y estructurada."
                ),
                "images": [image_b64],
                "stream": False,
            }
            resp = requests.post(
                "http://localhost:11434/api/generate",
                json=payload,
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json().get("response", "Sin análisis disponible.")
        except Exception as e:
            return f"Error en análisis VLM: {e}"

    def _tool_escanear_pantalla(self) -> dict:
        """[OCR] Captura la pantalla y extrae el texto visible vía pytesseract (CPU-only)."""
        _MAX_CHARS = 3000
        try:
            from PIL import ImageGrab
        except ImportError:
            return {"error": "Pillow (PIL.ImageGrab) no instalado. pip install Pillow"}
        try:
            import pytesseract
        except ImportError:
            return {"error": "pytesseract no instalado. pip install pytesseract (requiere tesseract en el OS)"}

        try:
            img = ImageGrab.grab()
        except Exception as e:
            return {"error": f"No se pudo capturar la pantalla: {e}"}

        try:
            raw_text = pytesseract.image_to_string(img, lang="spa+eng")
        except pytesseract.TesseractNotFoundError:
            return {"error": "Binario tesseract no encontrado en PATH. Instala tesseract-ocr en el sistema."}
        except Exception as e:
            return {"error": f"OCR falló: {e}"}

        cleaned_lines = [
            "".join(c for c in line if c.isprintable()).strip()
            for line in raw_text.splitlines()
        ]
        cleaned = "\n".join(line for line in cleaned_lines if line)

        if not cleaned:
            return {"status": "ilegible", "aviso": "No se detectó texto en la pantalla."}

        truncated = len(cleaned) > _MAX_CHARS
        if truncated:
            cleaned = cleaned[:_MAX_CHARS]

        result: dict = {
            "status": "ok",
            "chars": len(cleaned),
            "texto": cleaned,
        }
        if truncated:
            result["aviso"] = f"Texto truncado a {_MAX_CHARS} caracteres para proteger el KV Cache."
        return result

    def _tool_press_hotkey(self, keys: list) -> dict:
        import pyautogui
        pyautogui.hotkey(*keys)
        return {"pressed": "+".join(keys)}

    def _tool_type_text(self, text: str) -> dict:
        import pyautogui
        pyautogui.write(text, interval=0.05)
        return {"typed": len(text)}

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _tool_get_clipboard(self) -> dict:
        import pyperclip
        return {"clipboard": pyperclip.paste()}

    def _tool_set_clipboard(self, text: str) -> dict:
        import pyperclip
        pyperclip.copy(text)
        return {"status": "copied", "length": len(text)}

    # ── Packet Tracer / Networking ────────────────────────────────────────────

    def _tool_packet_tracer_open(self, file_path: str = "") -> dict:
        OS = platform.system()
        candidates: dict[str, list[str]] = {
            "Windows": [
                r"C:\Program Files\Cisco\Cisco Packet Tracer 8.2\PacketTracer.exe",
                r"C:\Program Files\Cisco\Cisco Packet Tracer 8.1\PacketTracer.exe",
            ],
            "Linux": ["packettracer", "/opt/pt/bin/PacketTracer"],
        }
        pt_cmd: str | None = None
        for c in candidates.get(OS, candidates["Linux"]):
            if shutil.which(c) or Path(c).exists():
                pt_cmd = c
                break
        if not pt_cmd:
            return {"error": "Packet Tracer no encontrado en el PATH."}
        args = [pt_cmd] + ([file_path] if file_path else [])
        subprocess.Popen(args, shell=False)
        return {"status": "launched", "file": file_path or "nuevo proyecto"}

    def _tool_network_scan(self, target: str, scan_type: str = "-sS -sV") -> dict:
        """[VALIDATED] Target and scan_type validated before passing to python-nmap."""
        if not _SAFE_HOST_RE.match(target):
            return {"error": "Invalid target. Use a valid IP, CIDR range, or hostname."}

        if _FORBIDDEN_CHARS_RE.search(scan_type):
            return {"error": "Invalid scan_type: contains forbidden shell metacharacters."}

        try:
            import nmap
        except ImportError:
            return {"error": "python-nmap not installed. Run: pip install python-nmap"}

        try:
            nm = nmap.PortScanner()
            nm.scan(hosts=target, arguments=scan_type)

            results: dict = {}
            for host in nm.all_hosts():
                host_data: dict = {
                    "state": nm[host].state(),
                    "protocols": {},
                }
                for proto in nm[host].all_protocols():
                    ports: dict = {}
                    for port in sorted(nm[host][proto].keys()):
                        port_info = nm[host][proto][port]
                        ports[port] = {
                            "state": port_info.get("state", ""),
                            "name": port_info.get("name", ""),
                            "product": port_info.get("product", ""),
                            "version": port_info.get("version", ""),
                        }
                    host_data["protocols"][proto] = ports
                results[host] = host_data

            return {"target": target, "scan_type": scan_type, "hosts": results}
        except nmap.PortScannerError as e:
            return {
                "error": (
                    "Nmap executable not found on host OS. "
                    f"Install it from https://nmap.org/download.html — Details: {e}"
                )
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_check_connectivity(self, host: str, port: int = 0) -> dict:
        """[VALIDATED] Host validado antes de usarlo en subprocess."""
        import socket

        if not _SAFE_HOST_RE.match(host):
            return {"error": "Host inválido. Solo IPs o hostnames válidos."}

        if port == 0:
            cmd_vector = (
                ["ping", "-c", "1", host]
                if platform.system() != "Windows"
                else ["ping", "-n", "1", host]
            )
            try:
                r = subprocess.run(
                    cmd_vector, shell=False, capture_output=True, text=True, timeout=10
                )
                return {"host": host, "reachable": r.returncode == 0}
            except Exception as e:
                return {"error": str(e)}

        try:
            socket.create_connection((host, port), timeout=5).close()
            return {"host": host, "port": port, "open": True}
        except Exception as e:
            return {"host": host, "port": port, "open": False, "error": str(e)}

    def _tool_estudiar_tema(self, url: str) -> dict:
        """Descarga una URL, limpia el HTML y vectoriza el contenido en ChromaDB."""
        import hashlib
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"error": "beautifulsoup4 no instalado. Ejecuta: pip install beautifulsoup4 lxml"}

        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        except Exception as e:
            return {"error": f"No se pudo descargar {url}: {e}"}

        if not text.strip():
            return {"error": "No se pudo extraer texto de la URL."}

        if not hasattr(self, "_memory"):
            try:
                from core.memory import VectorMemory
                self._memory = VectorMemory()
            except Exception as e:
                return {"error": f"No se pudo inicializar la memoria vectorial: {e}"}

        words = text.split()
        chunk_size, overlap = 400, 50
        chunks_indexed = 0
        i = 0
        while i < len(words):
            chunk = " ".join(words[i : i + chunk_size])
            chunk_id = hashlib.md5(f"{url}:{i}".encode()).hexdigest()
            self._memory.add(chunk, chunk_id, {"source": url, "chunk": chunks_indexed})
            chunks_indexed += 1
            i += chunk_size - overlap

        return {
            "url": url,
            "chars_extraidos": len(text),
            "chunks_indexados": chunks_indexed,
            "status": "Listo. Usa consultar_base_conocimiento para recuperar el contenido.",
        }

    def _tool_consultar_base_conocimiento(self, query: str) -> dict:
        """Vectoriza la pregunta y recupera los 3 fragmentos más relevantes de ChromaDB."""
        if not hasattr(self, "_memory"):
            try:
                from core.memory import VectorMemory
                self._memory = VectorMemory()
            except Exception as e:
                return {"error": f"No se pudo inicializar la memoria vectorial: {e}"}
        return self._memory.query(query)

    def _tool_whois_lookup(self, domain: str) -> dict:
        """[VALIDATED] Dominio validado contra regex antes de pasarlo a whois."""
        if not _SAFE_DOMAIN_RE.match(domain):
            return {
                "error": "Dominio inválido. Solo alfanuméricos, puntos y guiones (1-253 chars)."
            }
        try:
            r = subprocess.run(
                ["whois", domain], shell=False, capture_output=True, text=True, timeout=15
            )
            return {"domain": domain, "whois": r.stdout[:3000]}
        except FileNotFoundError:
            return {"error": "whois no encontrado. Instálalo con: apt install whois"}
        except Exception as e:
            return {"error": str(e)}

    # ── Knowledge Vault (RAG local) ───────────────────────────────────────────

    def _get_vault(self):
        if not hasattr(self, "_vault"):
            from core.knowledge import KnowledgeVault
            self._vault = KnowledgeVault()
        return self._vault

    def _tool_ingest_docs(self, folder_path: str = "") -> dict:
        """Index PDFs and TXTs from a local folder into the Knowledge Vault."""
        return self._get_vault().ingest_docs(folder_path)

    def _tool_query_knowledge(self, query: str) -> dict:
        """Retrieve the top-3 most relevant chunks from the Knowledge Vault."""
        text = self._get_vault().query_knowledge(query)
        return {"result": text}

    # ── System Health Monitor ─────────────────────────────────────────────────

    def _tool_get_system_status(self) -> dict:
        """CPU, RAM, battery and theoretical memory bandwidth saturation report."""
        cpu_pct = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()

        # Theoretical estimate: RAM occupancy weighted with CPU pressure.
        bw_saturation = round(min(ram.percent * 0.70 + cpu_pct * 0.30, 100.0), 1)

        report: dict = {
            "cpu_usage_pct": cpu_pct,
            "ram": {
                "total_gb": round(ram.total / 1e9, 2),
                "used_gb": round(ram.used / 1e9, 2),
                "available_gb": round(ram.available / 1e9, 2),
                "usage_pct": ram.percent,
            },
            "memory_bandwidth_saturation_estimate_pct": bw_saturation,
        }

        if ram.percent > 85:
            report["warning"] = (
                "RAM usage crítica (>85%). "
                "Considera cerrar Chrome, Blender u otras apps secundarias."
            )

        try:
            battery = psutil.sensors_battery()
            if battery:
                secsleft = battery.secsleft
                report["battery"] = {
                    "percent": battery.percent,
                    "plugged_in": battery.power_plugged,
                    "time_left_min": round(secsleft / 60, 1) if secsleft > 0 else "N/A",
                }
            else:
                report["battery"] = {"status": "No detectada (posiblemente desktop)"}
        except Exception:
            report["battery"] = {"status": "No disponible en este sistema"}

        governance_ok = self._audit.is_writable()
        report["governance_check"] = {
            "audit_logger_active": True,
            "audit_log_writable": governance_ok,
            "audit_log_path": str(self._audit.log_path),
            "status": "OPERATIONAL" if governance_ok else "DEGRADED — log file not writable",
        }

        return report

    def _tool_osint_lookup(self, domain: str) -> dict:
        """[HITL + VALIDATED] WHOIS + DNS recon via python-whois and dnspython."""
        # Strip protocol prefix and www
        domain = re.sub(r'^https?://', '', domain)
        domain = re.sub(r'^www\.', '', domain)
        domain = domain.split('/')[0].strip()

        if not _SAFE_DOMAIN_RE.match(domain):
            return {"error": "Dominio inválido. Solo alfanuméricos, puntos y guiones (1-253 chars)."}

        result: dict = {"domain": domain}

        try:
            w = whois.whois(domain)
            result["whois"] = {
                "registrar": w.registrar,
                "creation_date": str(w.creation_date),
                "expiration_date": str(w.expiration_date),
                "name_servers": list(w.name_servers) if w.name_servers else None,
            }
        except Exception as e:
            result["whois"] = {"error": str(e)}

        dns_records: dict = {}
        for record_type in ("A", "MX", "TXT"):
            try:
                answers = dns.resolver.resolve(domain, record_type)
                dns_records[record_type] = [str(r) for r in answers]
            except Exception as e:
                dns_records[record_type] = {"error": str(e)}
        result["dns"] = dns_records

        return result


# ── RedTeamShellExecutor ──────────────────────────────────────────────────────

class RedTeamShellExecutor:
    """Permissive shell executor for Red Team operator use.

    Sits alongside ToolExecutor without modifying it.
    Authorization gate: NATO OTP via ToolExecutor._challenge().

    Security layers:
      Layer 0 — YARA scan of the raw command string.
      Hard filter — _CRITICAL_BLOCK patterns are unconditional; no OTP override.
      Layer 1 — _classify(): binary must exist on OS; unlisted binaries escalate.
      Layer 2 — NATO OTP challenge for any command that requires_challenge.
      Layer 3 — subprocess.run with shell=False always.
    Full audit trail via ToolExecutor._audit.log_action().
    """

    def __init__(self, tool_executor: "ToolExecutor") -> None:
        self._te = tool_executor  # borrow _challenge() and _audit from ToolExecutor

    def _classify(self, command: str, yara_hits: list) -> tuple[str, bool]:
        tokens = shlex.split(command, posix=False)
        if not tokens:
            raise ValueError("Empty command")

        binary = Path(tokens[0]).name.lower().removesuffix(".exe")

        if shutil.which(binary) is None:
            raise ValueError(f"[BLOCKED] Binary '{binary}' not found on OS")

        in_allowlist = binary in COMMAND_ALLOWLIST
        suspicious   = bool(yara_hits) or any(
            p in command.lower() for p in _SUSPICIOUS_PATTERNS
        )

        if not in_allowlist:
            return ("unlisted_escalation", True)
        return ("allowlisted", suspicious)

    async def execute_shell(self, command: str, reasoning: str = "") -> dict:
        result: dict = {
            "command":    command,
            "authorized": False,
            "stdout":     "",
            "stderr":     "",
            "error":      None,
        }
        binary_status = "unknown"
        auth_word     = ""

        try:
            # Layer 0 — YARA scan (scan_command expects list[str])
            from core.yara_analyzer import scan_command
            yara_hits = await scan_command(shlex.split(command))

            # Hard filter — unconditional, no OTP override
            cmd_low = command.lower()
            for pattern in _CRITICAL_BLOCK:
                if pattern in cmd_low:
                    raise ValueError(f"[HARD BLOCK] OS-destructive pattern: '{pattern}'")

            # Layer 1 — classify binary
            binary_status, requires_challenge = self._classify(command, yara_hits)

            # Layer 2 — NATO OTP if required
            if requires_challenge:
                auth_ok, auth_word = await self._te._challenge(
                    tool_name="run_shell_command",
                    preview=command[:120],
                )
                if not auth_ok:
                    raise ValueError(f"[DENIED] NATO challenge failed: {auth_word}")

            # Layer 3 — execute, shell=False always
            result["authorized"] = True
            loop = asyncio.get_running_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    shlex.split(command),
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                ),
            )
            result["stdout"] = proc.stdout
            result["stderr"] = proc.stderr

        except ValueError as e:
            result["error"] = str(e)

        finally:
            self._te._audit.log_action(
                "run_shell_command",
                reasoning,
                auth_word,
                "authorized" if result["authorized"] else "denied",
                f"RedTeamShell | binary_status={binary_status} | cmd={command[:80]}",
            )

        return result
