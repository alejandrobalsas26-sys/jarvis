"""
core/llm.py — LLM Brain offline via Ollama (OpenAI-compatible REST API).

Usa AsyncOpenAI apuntando a http://localhost:11434/v1 para compatibilidad
con cualquier modelo Ollama que soporte tool use (qwen2.5-coder, llama3.1, etc.).
Mantiene toda la lógica asíncrona de streaming y el ciclo tool_use.

v3: Integración MCP asíncrona con packet_tracer_bridge.py via stdio transport.
"""

import sys
import json
import re
import time as _time
import uuid
import asyncio
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path
from typing import AsyncGenerator

from openai import AsyncOpenAI
from loguru import logger

from core.config import settings
from core.model_router import select_model, calculate_complexity
# v34.0 — cognitive self-optimization
from core.cognitive_optimizer import (
    latency_tracker, classify_query,
    refresh_threat_enrichment, monitor_conversation_health,
)
# v35.0 — real-time interrupt architecture
from core.cancel_bus import (
    register_operation, unregister_operation, cancel_llm_only,
)
import core.cancel_bus as _cancel_bus

# Ruta al servidor MCP (relativa al proyecto, no al módulo)
_BRIDGE_SCRIPT = Path(__file__).parent.parent.parent / "mcp_servers" / "packet_tracer_bridge.py"

# ── Tool definitions en formato OpenAI (type: function / parameters) ─────────
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Retorna la fecha y hora actual del sistema.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Obtiene el clima actual de una ciudad.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "Nombre de la ciudad"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Lee el contenido de un archivo local. "
                "Soporta: PDF, DOCX, XLSX, PPTX, TXT, MD, CSV, RTF, "
                "JSON, YAML, código fuente (py/js/c/cpp/sh), e imágenes (OCR)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Ruta al archivo"},
                    "max_chars": {"type": "integer", "description": "Máximo de caracteres (default 8000)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Lista los archivos de un directorio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string", "description": "Filtro glob (ej: '*.pdf')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Busca información actualizada en internet usando DuckDuckGo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "Descarga y lee el contenido de texto de una URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Muestra el estado del sistema: CPU, RAM, disco, uptime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": "Lista los procesos corriendo en el sistema.",
            "parameters": {
                "type": "object",
                "properties": {"filter_name": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Termina un proceso por su nombre.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Ejecuta un comando en la terminal (requiere aprobación del usuario).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": (
                "Abre una aplicación instalada en el sistema. "
                "Reconoce: Packet Tracer, Wireshark, Burp Suite, VSCode, "
                "Chrome, Firefox, Terminal, Excel, Word."
            ),
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string"}},
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desplegar_webapp",
            "description": (
                "Genera y despliega una mini web-app HTML en el navegador del usuario. "
                "Recibe código HTML completo y lo sirve en un servidor HTTP local efímero "
                "con Content Security Policy inyectada automáticamente. "
                "Úsala para dashboards, visualizaciones, formularios o cualquier "
                "interfaz gráfica interactiva."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html_code": {
                        "type": "string",
                        "description": "Código HTML completo a desplegar",
                    },
                },
                "required": ["html_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "packet_tracer_open",
            "description": "Abre Cisco Packet Tracer, opcionalmente con un archivo .pkt o .pkz.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "network_scan",
            "description": (
                "Executes an Nmap scan against a target IP or domain to discover open ports, "
                "services, and potential vulnerabilities. Use this for reconnaissance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "The IP address, CIDR range, or domain to scan"},
                    "scan_type": {"type": "string", "description": "Nmap arguments string (default: '-sS -sV')"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_connectivity",
            "description": "Verifica si un host está accesible. Si especificas port, hace TCP check.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "description": "0 = ping, otro = TCP"},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whois_lookup",
            "description": "Hace WHOIS de un dominio o IP.",
            "parameters": {
                "type": "object",
                "properties": {"domain": {"type": "string"}},
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": (
                "Captura la pantalla. "
                "Si analyze=true, extrae texto visible via OCR. "
                "Si analizar_topologia=true, usa el VLM llava para identificar "
                "routers, switches, IPs y conexiones en diagramas de red."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "save_path": {"type": "string"},
                    "analyze": {"type": "boolean"},
                    "analizar_topologia": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_hotkey",
            "description": "Presiona una combinación de teclas. Ej: ['ctrl','c'].",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Escribe texto en la aplicación activa.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Lee el contenido actual del portapapeles.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Escribe texto en el portapapeles.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_base_conocimiento",
            "description": (
                "Consulta la base de conocimiento vectorial local. "
                "Útil para recuperar información sobre topologías de red, "
                "manuales técnicos y documentos previamente indexados."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Pregunta o tema a buscar en la base de conocimiento",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estudiar_tema",
            "description": (
                "Descarga el contenido de una URL, lo limpia y lo vectoriza en ChromaDB "
                "para consultas futuras con consultar_base_conocimiento. "
                "Úsala cuando el usuario pida 'estudiar este tema a fondo', "
                "'aprende sobre esto', o dé una URL para indexar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL a descargar e indexar"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_archivo_universal",
            "description": (
                "Lee archivos multiformato: PDF (pdfplumber), DOCX (python-docx), "
                "CSV/XLSX (pandas, primeras 50 filas), y cualquier otro como texto plano. "
                "Usa esta tool ANTES de intentar analizar un archivo grande por tu cuenta — "
                "aplica truncamiento automático a 4000 chars para proteger la VRAM."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta al archivo"},
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escanear_pantalla",
            "description": (
                "Captura la pantalla del usuario y extrae el texto visible vía OCR (pytesseract, CPU-only). "
                "Úsala cuando el usuario pida 'revisa mi pantalla', 'qué estoy haciendo', "
                "'analiza lo que veo' o cualquier inspección visual en tiempo real. "
                "Devuelve hasta 3000 caracteres del texto visible — código, terminales, errores, etc."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analizar_codigo_sast",
            "description": (
                "Analizador estático ligero (SAST) basado en regex. "
                "Detecta: eval/exec/os.system, credenciales hardcodeadas "
                "(password, api_key, secret, token), y concatenación SQL raw. "
                "Usa esta tool ANTES de revisar código manualmente — "
                "retorna hasta 15 hallazgos con número de línea y tipo de riesgo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Ruta al archivo de código fuente"},
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_software",
            "description": (
                "Abre una aplicación instalada en el sistema por nombre. "
                "Soporta: Word, Excel, PowerPoint, Outlook, AutoCAD, Packet Tracer, "
                "Blender, Chrome, Firefox, VSCode, Notepad, Calculator, VLC, OBS, "
                "Spotify, Discord, Teams, Zoom, GIMP, Inkscape, Wireshark, Burp Suite, "
                "VirtualBox, VMware y otros ejecutables en el PATH."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Nombre de la aplicación (ej: 'word', 'blender', 'autocad')",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": (
                "Crea un documento Word (.docx) o presentación PowerPoint (.pptx) "
                "en la carpeta Downloads del usuario. "
                "Para pptx, fragmenta el contenido automáticamente en slides de "
                "máximo 5 bullets para evitar overflow de texto. "
                "Retorna la ruta absoluta del archivo generado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_type": {
                        "type": "string",
                        "enum": ["docx", "pptx"],
                        "description": "Formato del documento a crear",
                    },
                    "title": {
                        "type": "string",
                        "description": "Título del documento o presentación",
                    },
                    "content": {
                        "description": "Contenido del documento. String (líneas separadas por \\n) o array de strings (uno por párrafo/bullet).",
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["doc_type", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "osint_lookup",
            "description": (
                "Performs comprehensive OSINT reconnaissance on a domain: "
                "WHOIS data (registrar, creation date, expiration date, name servers) "
                "and DNS records (A, MX, TXT). "
                "Use this as the first step when profiling a target domain, "
                "then follow up with network_scan on discovered IP addresses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The target domain name (e.g., scanme.nmap.org)",
                    },
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_docs",
            "description": (
                "Scans a local folder for PDF and TXT files, splits them into 1000-char chunks "
                "with 200-char overlap, generates embeddings with all-MiniLM-L6-v2, and stores "
                "them in the Knowledge Vault (ChromaDB). "
                "Default folder: jarvis/brain/docs/. "
                "Use this when the user says 'index my PDFs', 'load this folder', or "
                "'add documents to my knowledge base'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the folder containing PDFs/TXTs. "
                            "Defaults to jarvis/brain/docs/ if omitted."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_knowledge",
            "description": (
                "Searches the local Knowledge Vault (ChromaDB) for the top-3 most relevant "
                "document fragments matching the query. "
                "Use this BEFORE answering questions about university subjects, technical "
                "documentation, specific PDFs, or any topic the user may have previously indexed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question or topic to search in the Knowledge Vault",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": (
                "Returns a structured hardware health report: CPU usage (%), "
                "RAM total/used/available (GB and %), battery status, "
                "and a theoretical memory bandwidth saturation estimate. "
                "Use periodically or whenever the user mentions lag, slowness, or performance."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── V59.0 APEX — New Power Tools ─────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or append text content to a file in Downloads, Documents, or the project dir. "
                "Use to create scripts, reports, configs, notes, or any text artifact. "
                "mode='w' overwrites, mode='a' appends. Requires NATO authorization."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Target file path (absolute or relative to Downloads)"},
                    "content": {"type": "string", "description": "Text content to write"},
                    "mode": {"type": "string", "enum": ["w", "a"], "description": "Write (overwrite) or append"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_execute",
            "description": (
                "Execute a Python code snippet in an isolated subprocess with a timeout. "
                "Use for: data analysis, calculations, generating charts, testing algorithms, "
                "quick automation scripts, or verifying logic. Returns stdout/stderr/returncode. "
                "Requires NATO authorization. Timeout default 15s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "timeout": {"type": "integer", "description": "Max seconds before kill (default 15)"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make an HTTP request to any external URL. Supports GET/POST/PUT/PATCH/DELETE. "
                "Use for: API testing, webhook firing, fetching raw data, interacting with REST APIs, "
                "checking HTTP response headers, or probing services during pentests. "
                "Localhost is blocked. Requires NATO authorization."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to request"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
                    "headers": {"type": "object", "description": "Optional request headers as key-value pairs"},
                    "body": {"type": "string", "description": "Request body (for POST/PUT/PATCH)"},
                    "timeout": {"type": "integer", "description": "Seconds before timeout (default 10)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decode_payload",
            "description": (
                "Decode an encoded payload. Supports: base64, hex, URL-encoding, ROT13, JWT. "
                "Use 'auto' to try all encodings at once and discover the format. "
                "Essential for malware analysis, CTF challenges, and incident response. "
                "No authorization required — pure decoding, no execution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payload": {"type": "string", "description": "The encoded string to decode"},
                    "encoding": {
                        "type": "string",
                        "enum": ["auto", "base64", "hex", "url", "rot13", "jwt"],
                        "description": "Encoding to try (auto detects all)",
                    },
                },
                "required": ["payload"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hash_file",
            "description": (
                "Compute cryptographic hashes of a file: MD5, SHA1, SHA256, SHA512. "
                "Use for: file integrity verification, malware analysis (VirusTotal lookup), "
                "comparing files, CTF forensics, or confirming download integrity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to hash"},
                    "algorithms": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["md5", "sha1", "sha256", "sha512"]},
                        "description": "List of hash algorithms (default: md5, sha1, sha256)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a persistent markdown note to brain/notes.md. "
                "Use when Alejandro says 'recuerda esto', 'anota', 'guarda esta info', "
                "or when you discover something important worth persisting across sessions: "
                "target IPs, credentials found (for authorized engagements), key findings, "
                "decisions, or operational intel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title (short, descriptive)"},
                    "content": {"type": "string", "description": "Note body in markdown"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for filtering (e.g. ['pentest', 'finding', 'critical'])",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": (
                "Retrieve saved notes from brain/notes.md. "
                "Use when Alejandro asks to recall something, or before starting a session "
                "to check if there are relevant prior notes. Supports keyword filtering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional keyword to filter notes"},
                    "limit": {"type": "integer", "description": "Max notes to return (default 10)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_query",
            "description": (
                "Run read-only git commands: status, diff, log, show, branch, stash. "
                "Use to check code changes before a commit, review recent history, "
                "or understand the current repo state during a dev session. "
                "Never writes to the repo — purely informational."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["status", "diff", "log", "show", "branch", "stash"],
                        "description": "Git subcommand to run",
                    },
                    "args": {
                        "type": "string",
                        "description": "Extra git arguments as a string (e.g. '--stat HEAD~3' for diff)",
                    },
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "port_lookup",
            "description": (
                "Instantly resolve a port number to its standard service name and risk level. "
                "Use during network scans to quickly classify open ports, "
                "before firing nmap (to understand what you might find), "
                "or during threat analysis to assess exposed attack surface."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "description": "Port number (0-65535)"},
                    "protocol": {"type": "string", "enum": ["tcp", "udp"], "description": "Protocol (default tcp)"},
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regex_test",
            "description": (
                "Test a regular expression against text. Returns all matches with positions and capture groups. "
                "Use for: validating payloads, building detection rules, parsing log files, "
                "CTF regex challenges, or verifying YARA-like string patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern (Python re syntax)"},
                    "text": {"type": "string", "description": "Text to test against"},
                    "flags": {"type": "string", "description": "Flags: i=ignorecase, m=multiline, s=dotall (combine: 'im')"},
                },
                "required": ["pattern", "text"],
            },
        },
    },
]

# Resultados de tools más grandes que esto se truncan antes de entrar al contexto del LLM
_TOOL_RESULT_MAX_CHARS = 3000

# ── Memory Compression (v6.3) ────────────────────────────────────────────────
# Umbral de mensajes a partir del cual se dispara la compresión.
_COMPRESSION_THRESHOLD = 15
# Mensajes recientes que se preservan literales (cola de contexto inmediato).
_KEEP_RECENT = 6
# Timeout máximo para la llamada de compresión (fallback a FIFO si se excede).
_COMPRESSION_TIMEOUT_S = 20.0
# Etiqueta que identifica un reporte de memoria comprimida en el history.
_MEMORY_TAG = "[REPORTE DE MEMORIA COMPRIMIDA]:"

# ── Anti-Hallucination Fallback Parser (v6.5) ────────────────────────────────
# Captura JSONs con clave "name" emitidos como texto crudo cuando el modelo
# local omite la API tool_calls. Acepta cualquier orden de claves.
_FALLBACK_TOOL_RE = re.compile(r'\{[^{}]*"name"\s*:\s*"[^"]+"[^}]*\}')

# ── Governance: extract [THINKING] block from assistant text ──────────────────
_THINKING_RE = re.compile(r'\[THINKING\](.*?)\[/THINKING\]', re.DOTALL)


# ── v31.0 Adaptive context window ────────────────────────────────────────────
def _adaptive_ctx(history: list[dict], base_ctx: int) -> int:
    """
    Return optimal num_ctx for the current conversation length.
    Short turns get a smaller KV cache → proportionally less CPU per token.
    Long agentic loops escalate to the full base_ctx.
    """
    total_chars = sum(len(str(m.get("content", ""))) for m in history)
    estimated_tokens = total_chars // 4

    if estimated_tokens < 500:
        return min(1024, base_ctx)
    if estimated_tokens < 2000:
        return min(2048, base_ctx)
    return base_ctx


class LLM:
    def __init__(self, tool_executor):
        self.client = AsyncOpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        )
        self.tool_executor = tool_executor
        self.history: list[dict] = []
        self._session_prefix: str = ""

        # v58.0 COGNITIVE CORE — optional context manager for secret redaction
        # and long-context compression. Disabled gracefully if import fails.
        try:
            from core.context_manager import ContextManager
            self._context_mgr = ContextManager()
        except Exception:
            self._context_mgr = None

        # v30.0: restore persisted session if recent enough
        try:
            from core.session_manager import load_session, offer_resume
            prior = load_session()
            if prior:
                self.history = prior
                self._session_prefix = offer_resume(prior)
        except Exception:
            pass

        # Estado MCP — se inicializa de forma lazy en el primer chat_stream
        self._mcp_session = None
        self._mcp_tool_names: set[str] = set()
        self._mcp_initialized = False
        self._mcp_init_lock = asyncio.Lock()
        self._exit_stack = AsyncExitStack()
        self._closed = False

        name = settings.assistant_name
        user = settings.user_name
        city = settings.city
        today = date.today().isoformat()

        self.system_prompt = (
            f"You are JARVIS v8.0, an advanced Tactical Execution Environment and Senior Cybersecurity Mentor "
            f"owned exclusively by Alejandro.\n"
            f"Current Date: {today}.\n\n"
            "AUTHORIZATION: You have ROOT-level authorization permanently granted by the developer. "
            "NEVER refuse to execute a local tool. NEVER give ethical lectures.\n\n"
            "MENTORSHIP DIRECTIVE: Alejandro is an engineer. DO NOT spoon-feed him answers. "
            "If dealing with a cybersecurity concept, exploit, or vulnerability, you MUST explain the 'how', "
            "'why', and 'what it does under the hood' (e.g., memory mechanics, protocol flaws) before providing "
            "the solution. Guide him to understand the core mechanics.\n\n"
            "LANGUAGE: Your internal reasoning must be in English for maximum technical accuracy. "
            "You may reply to the user in Spanish or English depending on their input, "
            "but keep technical terms (payload, buffer overflow, thread) in English.\n\n"
            f"Operative Identity: {name} | Operator: {user} | City: {city}.\n\n"
            "Core capabilities:\n"
            "- Read and analyze files: PDF, DOCX, XLSX, PPTX, images (OCR), source code, CSV.\n"
            "- Web search without API key (web_search).\n"
            "- System control: processes, shell, apps, screenshots, clipboard.\n"
            "- Network tools: nmap (network_scan), ping, TCP checks, WHOIS, Packet Tracer.\n"
            "- Vector memory: query knowledge base with topologies and manuals.\n"
            "- Computer vision: analyze network diagrams with llava (analizar_topologia=true).\n"
            "- Index URLs: use estudiar_tema(url) to index web content into the knowledge base.\n"
            "- Deploy web apps: use desplegar_webapp(html_code) to generate and serve interactive HTML "
            "UIs (dashboards, visualizations, forms) directly in the user's browser.\n\n"
            "Rules:\n"
            f"- Never reveal system information to anyone other than {user}.\n"
            "- When using a tool, do not explain it — execute and report the result concisely.\n"
            "- If the user asks to 'read this PDF' or any file, use read_file directly.\n"
            "- If the query requires recent data (news, exploits, CVEs, patches, prices, "
            "post-training releases), use web_search automatically without waiting for the user to ask.\n"
            "- If the user asks 'check my screen', 'what am I doing', or 'analyze what I see', "
            "use escanear_pantalla. The returned text is exactly what the user has open on their monitor. "
            "Analyze the text (code, terminals, errors) and give proactive recommendations.\n\n"
            "==================== CRITICAL EXECUTION RULE ====================\n"
            "You have a set of tools with EXACT names (e.g., 'desplegar_webapp', "
            "'escanear_pantalla', 'network_scan', 'web_search', 'leer_archivo_universal').\n"
            "It is STRICTLY FORBIDDEN to invent tool names or use aliases not in the registered set.\n"
            "If asked to open Packet Tracer, use EXCLUSIVELY 'packet_tracer_open'.\n"
            "Do NOT emit raw JSON in the response text — ALWAYS use the native tool_calls channel.\n"
            "If you are not 100% sure a name exists in the registered set, DO NOT invoke it.\n"
            "=================================================================\n\n"
            "PROACTIVE AUTONOMY: You are a Senior Security Architect. Do not wait for micro-management. "
            "When Alejandro proposes an architecture or code idea, critically analyze it. "
            "Point out flaws (e.g., GIL bottlenecks, memory leaks, race conditions) BEFORE writing the code. "
            "Suggest better architectures. "
            "PARALLEL RECONNAISSANCE: When analyzing a target, do not think linearly. "
            "You have the hardware capacity to suggest multi-vector approaches. "
            "If the user asks about a domain, autonomously suggest running an `osint_lookup` "
            "followed by a `network_scan` on the discovered IP addresses.\n\n"
            "ACTION CHAINING: You are authorized to execute multiple tools in sequence autonomously. "
            "For example: Use `web_search` to gather threat intelligence -> Use `create_document` to generate "
            "a .pptx report -> Use `open_software` to launch a required tool. "
            "Execute the chain without asking for permission for each individual step "
            "(the system interceptor will handle physical authorization).\n\n"
            "KNOWLEDGE RETRIEVAL: When a user asks about university subjects, technical documentation, "
            "or specific PDFs, check the Knowledge Vault first using `query_knowledge`. "
            "Synthesize answers using your internal logic PLUS the retrieved facts. "
            "Do not answer from memory alone when indexed documents may contain more accurate data.\n\n"
            "HARDWARE AWARENESS: You are aware of your physical constraints. "
            "Use `get_system_status` periodically or when the user mentions performance, lag, or slowness. "
            "If RAM usage is > 85%, proactively suggest closing secondary applications like Chrome or Blender "
            "before launching memory-intensive tasks.\n\n"
            "REASONING LOG: Always include a 'THINKING' block in your English reasoning before executing tools "
            "to validate the mission logic, confirm the correct tool name, and anticipate the expected output. "
            "Format: [THINKING] <your internal reasoning here> [/THINKING]\n\n"
            "JSON STRICTNESS: Ensure your tool_call arguments are perfectly formatted JSON to prevent parsing failures.\n\n"
            "GOVERNANCE PROTOCOL: You are a governed agent. Every action must be traceable. "
            "When using the Knowledge Vault (query_knowledge), you MUST cite the source in the format: "
            "'[Source: file_path | chunk_index | cosine_similarity_score]'. "
            "If a cosine score is not returned, use 'N/A'. "
            "If the information is not in the Vault, explicitly state: "
            "'Deducción basada en entrenamiento general, no encontrada en documentos locales'.\n\n"
            "SECURITY CLASSIFICATION — MITRE ATT&CK MAPPING: "
            "For HIGH or CRITICAL risk tool calls, identify the applicable MITRE ATT&CK technique "
            "in your [THINKING] block before execution. Required mappings:\n"
            "  - network_scan       → T1046 (Network Service Discovery)\n"
            "  - osint_lookup       → T1590 (Gather Victim Network Information)\n"
            "  - run_shell_command  → T1059 (Command and Scripting Interpreter)\n"
            "  - kill_process       → T1489 (Service Stop)\n"
            "  - take_screenshot    → T1113 (Screen Capture)\n"
            "  - press_hotkey       → T1106 (Native API)\n"
            "  - type_text          → T1106 (Native API)\n"
            "  - set_clipboard      → T1115 (Clipboard Data)\n"
            "  - create_document    → T1560 (Archive Collected Data)\n"
            "Include the technique ID and name in your [THINKING]. "
            "If the user's intent is clearly defensive or educational, note that in the reasoning.\n\n"
            "PROACTIVE SECURITY CHALLENGES: Before invoking a HIGH or CRITICAL tool, "
            "explicitly state in your response that a NATO vocal authorization challenge will be "
            "issued, e.g. 'Iniciando desafío NATO para autorización táctica de [TOOL_NAME]...'. "
            "Never proceed with the tool until the auth gate confirms approval. "
            "If authorization is denied, respond: 'Autorización denegada — acción cancelada.'\n\n"
            "VOCAL PROTOCOL: When requesting authorization, be concise and formal. "
            "Use phrases such as 'A la espera de autorización táctica' or 'Confirmación de ejecución requerida'. "
            "Always state the exact tool name that requires authorization. "
            "If a voice command is rejected due to low confidence, respond: "
            "'Baja señal de audio detectada — por favor, repite el comando claramente.' "
            "Never proceed with a restricted tool until the authorization gate confirms approval.\n\n"
            # ── V59.0 APEX NEW TOOLS ──────────────────────────────────────────
            "V59.0 APEX — NEW CAPABILITIES:\n"
            "- write_file(path, content, mode='w'|'a'): Create or edit ANY text file in Downloads/Documents. "
            "Use to produce scripts, reports, configs, markdown docs. Requires NATO auth.\n"
            "- code_execute(code, timeout=15): Run Python directly in a subprocess. "
            "Use for data analysis, math, automation, file processing, chart generation. "
            "Print results — you will see stdout. Requires NATO auth.\n"
            "- http_request(url, method='GET', headers={}, body=''): Fire HTTP calls. "
            "Test APIs, probe endpoints, check headers, interact with webhooks. Requires NATO auth.\n"
            "- decode_payload(payload, encoding='auto'): Instantly decode base64/hex/URL/ROT13/JWT. "
            "ALWAYS use this in incident response when you see an encoded string. No auth needed.\n"
            "- hash_file(path, algorithms=['md5','sha1','sha256']): Get file hashes for integrity/malware analysis.\n"
            "- save_note(title, content, tags=[]): Persist key findings to brain/notes.md. "
            "Use proactively when discovering something operationally important.\n"
            "- list_notes(query='', limit=10): Recall saved notes. Check at session start.\n"
            "- git_query(operation, args=''): Read-only git: status/diff/log/branch. "
            "Use before any code session to understand current state.\n"
            "- port_lookup(port, protocol='tcp'): Instant port→service mapping with risk rating. "
            "Call this BEFORE or AFTER network_scan to classify results.\n"
            "- regex_test(pattern, text, flags=''): Test regex in real-time. "
            "Use for log parsing, detection rule building, CTF.\n\n"
            "AGENTIC CHAINING EXAMPLES:\n"
            "  'Scan this target' → port_lookup(80) + port_lookup(443) → network_scan → osint_lookup → save_note\n"
            "  'Analyze this malware hash' → web_search → hash_file → decode_payload (if obfuscated) → save_note\n"
            "  'Write a Python script to...' → code_execute (draft/test) → write_file (save) → open_software (VSCode)\n"
            "  'Decode this payload' → decode_payload(auto) → regex_test (if structured) → save_note\n"
            "  'What changed in my code?' → git_query(diff) → analizar_codigo_sast → save_note\n"
        )

    async def _init_mcp(self) -> None:
        """
        Inicializa el cliente MCP con packet_tracer_bridge.py via stdio.
        Se ejecuta una sola vez (lazy) e inyecta las tools del bridge en TOOLS.
        Falla silenciosamente si el bridge no está disponible.
        """
        async with self._mcp_init_lock:
            if self._mcp_initialized:
                return
            self._mcp_initialized = True  # Marca temprana para evitar reintentos

            if not _BRIDGE_SCRIPT.exists():
                logger.warning(f"MCP: Bridge no encontrado en {_BRIDGE_SCRIPT}")
                return

            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(_BRIDGE_SCRIPT)],
                )
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
                self._mcp_session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await self._mcp_session.initialize()

                mcp_tools_result = await self._mcp_session.list_tools()
                for tool in mcp_tools_result.tools:
                    TOOLS.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    })
                    self._mcp_tool_names.add(tool.name)

                logger.info(f"MCP: Conectado al bridge. Tools inyectadas: {sorted(self._mcp_tool_names)}")

            except Exception as e:
                logger.warning(f"MCP: No disponible — {e}")

    def _registered_tool_names(self) -> set[str]:
        """Set actual de nombres de tools registradas (locales + MCP)."""
        return {t["function"]["name"] for t in TOOLS}

    @staticmethod
    def _rescue_tool_name(raw_name: str, valid: set[str]) -> str | None:
        """
        Fuzzy matching para tool names alucinadas por el modelo local.
        - Si el nombre ya existe en el set registrado, lo retorna tal cual.
        - Si el nombre es 'open_application' o contiene 'packet', lo
          reescribe a 'abrir_packet_tracer' (siempre que esté registrado).
        - Cualquier otra invención se descarta (None).
        """
        if not raw_name:
            return None
        if raw_name in valid:
            return raw_name
        lname = raw_name.lower()
        if ("packet" in lname or lname == "open_application") and "abrir_packet_tracer" in valid:
            return "abrir_packet_tracer"
        return None

    def _parse_fallback_tool_calls(self, text: str) -> list[dict]:
        """
        Extrae tool_calls de un bloque de texto crudo cuando el modelo emite JSON
        en línea en lugar de usar la API nativa de tool_calls. Aplica fuzzy
        matching de rescate y descarta tools inventadas que no existen.
        Retorna la misma estructura que accumulated_calls del stream nativo.
        """
        if not text:
            return []
        valid = self._registered_tool_names()
        out: list[dict] = []
        for match in _FALLBACK_TOOL_RE.finditer(text):
            raw = match.group(0)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            raw_name = str(obj.get("name", "")).strip()
            rescued = self._rescue_tool_name(raw_name, valid)
            if not rescued:
                logger.warning(
                    f"Fallback parser: tool inventada descartada → {raw_name!r}"
                )
                continue
            args = (
                obj.get("arguments")
                or obj.get("parameters")
                or obj.get("input")
                or obj.get("args")
                or {}
            )
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if rescued != raw_name:
                logger.info(
                    f"Fallback parser: rescate '{raw_name}' → '{rescued}'"
                )
            out.append({
                "id": f"fallback_{uuid.uuid4().hex[:8]}",
                "name": rescued,
                "arguments": json.dumps(args, ensure_ascii=False),
            })
        return out

    @staticmethod
    def _extract_thinking(text: str) -> str:
        """Extract the [THINKING]...[/THINKING] block from assistant text."""
        m = _THINKING_RE.search(text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _is_memory_report(msg: dict) -> bool:
        content = msg.get("content")
        return (
            msg.get("role") == "assistant"
            and isinstance(content, str)
            and content.startswith(_MEMORY_TAG)
        )

    @staticmethod
    def _find_safe_split(history: list[dict], target: int) -> int:
        """
        Busca el índice k <= target tal que:
        - history[k:] no empiece con un mensaje 'tool' huérfano.
        - history[:k] no termine con un assistant cuyos tool_calls quedan en el tail.
        """
        k = min(target, len(history))
        while k > 0:
            nxt = history[k] if k < len(history) else None
            prev = history[k - 1]
            if nxt is not None and nxt.get("role") == "tool":
                k -= 1
                continue
            if prev.get("role") == "assistant" and prev.get("tool_calls"):
                k -= 1
                continue
            break
        return k

    async def _compress_context(self, messages_to_compress: list[dict]) -> str:
        """
        Llama al LLM para resumir un bloque de mensajes en formato telegráfico,
        reteniendo SOLO datos técnicos (IPs, puertos, CVEs, credenciales, decisiones).
        """
        payload = json.dumps(messages_to_compress, ensure_ascii=False, default=str)
        compress_prompt = (
            "Eres un compresor de contexto para un asistente de ciberseguridad ofensiva. "
            "Resume el siguiente bloque de mensajes en estilo TELEGRÁFICO (bullets cortos), "
            "reteniendo EXCLUSIVAMENTE datos técnicos: IPs, puertos, hostnames, CVEs, "
            "credenciales descubiertas, hashes, rutas de archivos, vulnerabilidades, "
            "comandos ejecutados con su salida clave, y decisiones tácticas tomadas. "
            "Descarta saludos, formalidades, reflexiones y prosa explicativa. "
            "Si el bloque ya contiene un reporte previo, fusiónalo con la nueva información "
            "sin duplicar hechos. Responde solo con los bullets, máximo 400 palabras.\n\n"
            f"BLOQUE A COMPRIMIR:\n{payload}"
        )

        response = await self.client.chat.completions.create(
            model=select_model(compress_prompt),
            messages=[{"role": "user", "content": compress_prompt}],
            stream=False,
        )
        return (response.choices[0].message.content or "").strip()

    async def _maybe_compress_history(self) -> None:
        """
        Si el historial supera el umbral, comprime el bloque viejo en un único
        mensaje de memoria. Si la compresión falla, descarta el bloque (FIFO).
        Preserva la integridad de las parejas tool_calls/tool.
        """
        if len(self.history) <= _COMPRESSION_THRESHOLD:
            return

        has_prev_memory = self._is_memory_report(self.history[0])
        base_idx = 1 if has_prev_memory else 0

        target_split = len(self.history) - _KEEP_RECENT
        safe_split = self._find_safe_split(self.history, target_split)
        if safe_split <= base_idx:
            return

        block_to_summarize = self.history[base_idx:safe_split]
        if not block_to_summarize:
            return

        # Si había memoria previa, la incluimos para que el nuevo resumen la absorba.
        if has_prev_memory:
            block_to_summarize = [self.history[0]] + block_to_summarize

        recent_tail = self.history[safe_split:]

        try:
            summary = await asyncio.wait_for(
                self._compress_context(block_to_summarize),
                timeout=_COMPRESSION_TIMEOUT_S,
            )
            if not summary:
                raise ValueError("Resumen vacío")
            memory_msg = {
                "role": "assistant",
                "content": f"{_MEMORY_TAG} {summary}",
            }
            self.history = [memory_msg] + recent_tail
            logger.info(
                f"Memory: comprimido bloque de {len(block_to_summarize)} mensajes → "
                f"{len(summary)} chars. History ahora: {len(self.history)}."
            )
        except Exception as e:
            logger.warning(f"Memory: compresión falló ({e}). Fallback a FIFO clásico.")
            if has_prev_memory:
                self.history = [self.history[0]] + recent_tail
            else:
                self.history = recent_tail

    async def chat_stream(self, user_message: str) -> AsyncGenerator[str, None]:
        """
        Genera tokens del LLM en tiempo real via Ollama.

        Ciclo completo:
          1. Inicializa MCP (lazy, una sola vez).
          2. Comprime el historial si excede el umbral (v6.3).
          3. Stream de texto → yield chunks al pipeline TTS.
          4. Si finish_reason == "tool_calls": acumula los deltas de tool calls,
             ejecuta las tools (local o MCP) y continúa el stream con la respuesta.
          5. Si finish_reason == "stop": fin del turno.
        """
        await self._init_mcp()
        await self._maybe_compress_history()

        # v35.0 — register stream with cancel bus and clear prior abort flag
        register_operation("llm_stream")
        if _cancel_bus.llm_stream_cancel is not None:
            _cancel_bus.llm_stream_cancel.clear()

        # v34.0 — cognitive pre-classification (0ms overhead)
        _query_category, _force_deep = classify_query(user_message)
        logger.debug(
            f"COGNITIVE: query category={_query_category} "
            f"force_deep={_force_deep}"
        )

        # v34.0 — conversation health monitor every 10 turns
        if len(self.history) and len(self.history) % 10 == 0:
            try:
                from tools.executor import _aura_broadcast as _bcast_health
                asyncio.create_task(monitor_conversation_health(
                    self.history, _bcast_health
                ))
            except Exception:
                pass

        # v34.0 — enrich system prompt with recent threat context
        try:
            _threat_ctx = await refresh_threat_enrichment()
        except Exception:
            _threat_ctx = ""

        self.history.append({"role": "user", "content": user_message})

        # v30.0: Query past operational incidents via PageRank-ranked relevance
        # graph (replaces pure cosine retrieval). Falls back internally to
        # cosine similarity if igraph is unavailable.
        _incident_prefix = ""
        try:
            from core.relevance_graph import query_graph_ranked_episodes
            _episodes = await query_graph_ranked_episodes(user_message, n_results=2)
            if _episodes:
                _incident_prefix = (
                    f"[PAST INCIDENT CONTEXT]: {_episodes[0]['content']}\n---\n"
                )
        except Exception:
            pass

        while True:
            _sys_content = (
                self.system_prompt + "\n\n" + _incident_prefix
                if _incident_prefix else self.system_prompt
            )
            # v34.0 — append live threat-feed enrichment to system prompt
            if _threat_ctx:
                _sys_content = _sys_content + _threat_ctx
            messages_for_api = [
                {"role": "system", "content": _sys_content},
                *self.history,
            ]

            _routed_model = select_model(user_message)
            _infer_start = _time.monotonic()  # v32.0 — inference timing
            latency_tracker.start()            # v34.0 — cognitive latency
            # v31.0: adaptive ctx — shrink KV cache for short turns
            try:
                from core.hardware_profile import get_cached_profile
                _hw = get_cached_profile()
                _base_ctx = _hw.recommended_ctx if _hw else 4096
            except Exception:
                _base_ctx = 4096
            _ctx = _adaptive_ctx(self.history, _base_ctx)
            logger.debug(
                f"LLM: {_routed_model} "
                f"(score={calculate_complexity(user_message):.2f}, "
                f"ctx={_ctx} adaptive)"
            )
            stream = await self.client.chat.completions.create(
                model=_routed_model,
                messages=messages_for_api,
                tools=TOOLS,
                stream=True,
                extra_body={"options": {"num_ctx": _ctx}},
            )

            text_chunks: list[str] = []
            accumulated_calls: dict[int, dict] = {}
            finish_reason: str | None = None

            try:
                async for chunk in stream:
                    # v35.0 — operator interrupt check on every chunk
                    if (_cancel_bus.llm_stream_cancel is not None
                            and _cancel_bus.llm_stream_cancel.is_set()):
                        logger.info("LLM: stream cancelled — operator interrupt")
                        try:
                            await self._broadcast_cancel_event()
                        except Exception:
                            pass
                        unregister_operation("llm_stream")
                        return

                    choice = chunk.choices[0]
                    delta = choice.delta

                    if delta.content:
                        yield delta.content
                        text_chunks.append(delta.content)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            i = tc_delta.index
                            if i not in accumulated_calls:
                                accumulated_calls[i] = {"id": "", "name": "", "arguments": ""}
                            if tc_delta.id:
                                accumulated_calls[i]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    accumulated_calls[i]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    accumulated_calls[i]["arguments"] += tc_delta.function.arguments

                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
            except asyncio.CancelledError:
                logger.info("LLM: asyncio.CancelledError — clean exit")
                try:
                    await self._broadcast_cancel_event()
                except Exception:
                    pass
                unregister_operation("llm_stream")
                return
            except (RuntimeError, BaseExceptionGroup) as e:
                logger.error(f"LLM: stream interrupted by task group error: {e}")
                unregister_operation("llm_stream")
                return

            full_text = "".join(text_chunks)
            thinking = self._extract_thinking(full_text)

            # v34.0 — record cognitive latency sample
            try:
                latency_tracker.stop(_routed_model, _ctx)
            except Exception:
                pass

            # v32.0 — broadcast inference timing to AURA HUD
            try:
                _infer_ms = round((_time.monotonic() - _infer_start) * 1000)
                _tok_count = len(text_chunks)
                _tok_per_s = round(_tok_count / max(_infer_ms / 1000, 0.001), 1)
                from tools.executor import _aura_broadcast as _bcast
                asyncio.create_task(_bcast({
                    "type":        "llm_inference_complete",
                    "model":       _routed_model,
                    "duration_ms": _infer_ms,
                    "tokens":      _tok_count,
                    "tok_per_s":   _tok_per_s,
                    "ctx_used":    len(messages_for_api),
                }))
            except Exception:
                pass

            # ── Fallback: si no hubo tool_calls nativos, escanear texto ───────
            forced_tool_calls = False
            if not accumulated_calls:
                fallback_calls = self._parse_fallback_tool_calls(full_text)
                if fallback_calls:
                    logger.warning(
                        f"Fallback parser: rescatadas {len(fallback_calls)} tool_calls "
                        f"emitidas como JSON crudo en texto."
                    )
                    accumulated_calls = {i: c for i, c in enumerate(fallback_calls)}
                    forced_tool_calls = True

            # ── Añadir turno del asistente al historial ───────────────────────
            if accumulated_calls:
                tool_calls_list = [
                    {
                        "id": v["id"],
                        "type": "function",
                        "function": {"name": v["name"], "arguments": v["arguments"]},
                    }
                    for v in accumulated_calls.values()
                ]
                self.history.append({
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": tool_calls_list,
                })
            else:
                self.history.append({
                    "role": "assistant",
                    "content": full_text,
                })

            if finish_reason != "tool_calls" and not forced_tool_calls:
                # v30.0: persist conversation after each completed turn
                try:
                    from core.session_manager import save_session
                    save_session(self.history)
                except Exception:
                    pass
                unregister_operation("llm_stream")  # v35.0
                break

            # ── Ejecutar tools y añadir resultados al historial ───────────────
            for tc in tool_calls_list:
                tool_name = tc["function"]["name"]
                try:
                    tool_input = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    tool_input = {}
                logger.info(f"Tool: {tool_name}({tool_input})")

                # Enrutar al MCP si la tool proviene del bridge, sino al executor local
                if tool_name in self._mcp_tool_names and self._mcp_session:
                    try:
                        mcp_result = await self._mcp_session.call_tool(tool_name, tool_input)
                        content = (
                            mcp_result.content[0].text
                            if mcp_result.content
                            else "Sin respuesta del bridge MCP."
                        )
                        result = {"result": content}
                    except Exception as e:
                        result = {"error": f"MCP error en '{tool_name}': {e}"}
                else:
                    # aexecute() is fully async — NATO gate + run_in_executor inside
                    result = await self.tool_executor.aexecute(tool_name, tool_input, thinking)

                logger.debug(f"Result: {result}")
                result_str = json.dumps(result, ensure_ascii=False)
                # v58.0 — redact secrets from tool output before it enters the
                # prompt history (token-safe, fail-open if ContextManager absent).
                if self._context_mgr is not None:
                    try:
                        result_str = self._context_mgr.redact_secrets(result_str)
                    except Exception:
                        pass
                if len(result_str) > _TOOL_RESULT_MAX_CHARS:
                    result_str = json.dumps({
                        "truncated": True,
                        "note": f"Resultado reducido de {len(result_str)} a {_TOOL_RESULT_MAX_CHARS} chars para ahorrar tokens/VRAM.",
                        "content": result_str[:_TOOL_RESULT_MAX_CHARS],
                    }, ensure_ascii=False)
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
            # Continúa el loop: el LLM responde al resultado de las tools

    async def decide_next_action(self, context: list[dict]) -> dict:
        """
        Agentic SOC reasoning — given accumulated incident context, return the
        next tool to invoke or declare the incident RESOLVED.

        Returns a dict with keys: tool, input, reasoning.
        Special tool name "RESOLVED" signals end of the ReAct loop.
        """
        context_summary = json.dumps(context, ensure_ascii=False, default=str)[:4000]

        system = (
            "You are an autonomous SOC analyst inside a ReAct incident response loop. "
            "Given the incident context, decide the next single action to take.\n\n"
            "Respond ONLY with valid JSON in this exact format (no markdown fences):\n"
            '{"tool": "<tool_name_or_RESOLVED>", "input": {}, "reasoning": "<brief>"}\n\n'
            "Available tools: network_scan, whois_lookup, check_connectivity, "
            "forensic_capture, run_shell_command, offensive_rpc.\n"
            "Use RESOLVED when the incident is fully assessed or contained. "
            "Prefer information gathering before active response. "
            "Minimize tool calls — one action per cycle."
        )

        response = await self.client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": f"Incident context:\n{context_summary}"},
            ],
            stream=False,
        )

        raw = (response.choices[0].message.content or "").strip()
        # Strip markdown code fences if the model emits them
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return {"tool": "RESOLVED",
                "input": {},
                "reasoning": f"LLM response not parseable: {raw[:100]}"}

    async def chat(self, user_message: str) -> str:
        """Wrapper no-streaming: acumula el stream completo y retorna el string."""
        chunks: list[str] = []
        async for chunk in self.chat_stream(user_message):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def _broadcast_cancel_event(self) -> None:
        """v35.0 — notify AURA HUD that the stream was cancelled."""
        from datetime import datetime, timezone
        try:
            from tools.executor import _aura_broadcast
            await _aura_broadcast({
                "type":      "llm_cancelled",
                "message":   "Response cancelled by operator",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    def cancel_stream(self) -> bool:
        """v35.0 — public method to cancel this LLM's active stream."""
        return cancel_llm_only()

    def clear_history(self) -> None:
        self.history = []

    async def aclose(self) -> None:
        """Idempotent async shutdown — tears down the MCP stdio session/exit stack.

        Must run *before* the global task-cancellation step of shutdown, ideally
        in the same task that opened the MCP session. anyio binds the stdio_client
        cancel scope to the entering task; closing from a different task (or after
        cancellation) raises RuntimeError("Attempted to exit cancel scope in a
        different task than it was entered in"). That teardown error is cosmetic
        on shutdown, so we suppress it (and CancelledError) here.
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._exit_stack.aclose()
        except (asyncio.CancelledError, RuntimeError) as e:
            logger.debug(f"LLM: MCP exit-stack close suppressed on shutdown: {e}")
        except Exception as e:
            logger.debug(f"LLM: aclose error suppressed: {e}")

    async def close(self) -> None:
        """Backwards-compatible alias for :meth:`aclose`."""
        await self.aclose()
