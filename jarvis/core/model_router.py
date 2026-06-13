"""
core/model_router.py — Dual-Model Intelligent Routing for Ollama.

Routes prompts to fast (7B Q4) or deep (14B Q4) model based on
complexity score computed from length, vocabulary density, and
depth-keyword hits. Auto-upgrades fast model to Q8 on 64GB hardware.
"""

import os
import re
from dataclasses import dataclass
from enum import Enum

import httpx
from loguru import logger

MODEL_FAST = os.getenv("JARVIS_MODEL_FAST", "qwen2.5:7b-instruct-q4_K_M")
MODEL_DEEP = os.getenv("JARVIS_MODEL_DEEP", "qwen2.5:14b-instruct-q4_K_M")
OLLAMA_URL = os.getenv("OLLAMA_HOST",       "http://localhost:11434")
COMPLEXITY_THRESHOLD = 0.6

_TECH_TERMS = {
    "analyze", "correlate", "forensic", "entropy", "injection",
    "exfiltrate", "lateral", "privilege", "escalation", "persistence",
    "hollowing", "kerberoast", "bloodhound", "volatility", "mitre",
    "yara", "exploit", "payload", "beacon", "implant", "c2",
    "shellcode", "disassemble", "reverse", "malware", "obfuscate",
    "encrypted", "certificate", "anomaly", "baseline", "detection",
    "triage", "incident", "compromise", "exfiltration", "rootkit",
}

_DEPTH_PATTERNS = [
    r"\banalyze\b", r"\bcompare\b", r"\bcorrelate\b",
    r"\bexplain\s+why\b", r"\bhow\s+.*\s+detect\b",
    r"\bincident\s+response\b", r"\broot\s+cause\b",
    r"\battack\s+chain\b", r"\blast\s+.*\s+incident\b",
]


def calculate_complexity(prompt: str) -> float:
    words = prompt.split()
    n = max(len(words), 1)

    length_score = min(len(prompt) / 2000, 1.0) * 0.4

    tech_ratio = sum(1 for w in words if w.lower() in _TECH_TERMS) / n
    tech_score = min(tech_ratio * 5, 1.0) * 0.3

    depth_hits = sum(1 for p in _DEPTH_PATTERNS
                     if re.search(p, prompt, re.IGNORECASE))
    depth_score = min(depth_hits / 3, 1.0) * 0.3

    return min(length_score + tech_score + depth_score, 1.0)


def select_model(prompt: str, force_deep: bool = False) -> str:
    if force_deep:
        return MODEL_DEEP
    return MODEL_DEEP if calculate_complexity(prompt) > COMPLEXITY_THRESHOLD \
           else MODEL_FAST


async def check_model_availability() -> dict[str, bool]:
    """Verify both models are pulled in Ollama before routing."""
    available = {MODEL_FAST: False, MODEL_DEEP: False}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code == 200:
                pulled = {m["name"] for m in r.json().get("models", [])}
                for model in available:
                    available[model] = any(
                        model in p or p.startswith(model.split(":")[0])
                        for p in pulled
                    )
    except Exception:
        pass
    return available


async def list_pulled_models() -> list[str]:
    """Names of models currently pulled in Ollama. Empty list if unreachable —
    callers distinguish 'server down' from 'model missing' by this being empty."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code == 200:
                return sorted(
                    m.get("name", "")
                    for m in r.json().get("models", [])
                    if m.get("name")
                )
    except Exception:
        pass
    return []


# ════════════════════════════════════════════════════════════════════════════
#  V60.0 — Role-based local intelligence router
#  Additive layer on top of the legacy dual-model select_model() above, which
#  llm.py still uses. route() classifies a prompt into a cognitive ROLE and
#  resolves the concrete model from env-overridable, hardware-friendly defaults.
# ════════════════════════════════════════════════════════════════════════════


class ModelRole(str, Enum):
    FAST = "fast"            # chat, simple commands, low latency
    CODER = "coder"          # Python/JS, repos, refactor, tests, debugging
    DEEP = "deep"            # architecture, threat models, DFIR, GRC, analysis
    VISION = "vision"        # screenshots, camera, OCR, diagrams
    EMBEDDING = "embedding"  # indexing, RAG, vector search
    VERIFIER = "verifier"    # review, factuality, security-sensitive validation
    CLOUD = "cloud"          # optional cloud escalation (off by default)


# Safe local-first defaults (overridable via env). Chosen to run on modest,
# CPU-bound homelab hardware — small fast model, mid coder, reasoning deep.
_ROLE_DEFAULTS: dict[ModelRole, str] = {
    ModelRole.FAST:      "qwen2.5-coder:7b",
    ModelRole.CODER:     "qwen2.5-coder:14b",
    ModelRole.DEEP:      "deepseek-r1:14b",
    ModelRole.VISION:    "moondream",
    ModelRole.EMBEDDING: "nomic-embed-text",
    ModelRole.VERIFIER:  "qwen2.5-coder:7b",
}

_ROLE_ENV: dict[ModelRole, str] = {
    ModelRole.FAST:      "JARVIS_MODEL_FAST",
    ModelRole.CODER:     "JARVIS_MODEL_CODER",
    ModelRole.DEEP:      "JARVIS_MODEL_DEEP",
    ModelRole.VISION:    "JARVIS_MODEL_VISION",
    ModelRole.EMBEDDING: "JARVIS_MODEL_EMBEDDING",
    ModelRole.VERIFIER:  "JARVIS_MODEL_VERIFIER",
}


def model_for_role(role: ModelRole) -> str:
    """Resolve the configured model name for *role* (env override → default)."""
    if role == ModelRole.CLOUD:
        return os.getenv("JARVIS_CLOUD_MODEL", "anthropic/claude-sonnet-4-6")
    env_key = _ROLE_ENV.get(role)
    default = _ROLE_DEFAULTS.get(role, _ROLE_DEFAULTS[ModelRole.FAST])
    return os.getenv(env_key, default) if env_key else default


def cloud_enabled() -> bool:
    return os.getenv("JARVIS_CLOUD_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── Bilingual (EN + ES) routing keyword sets ─────────────────────────────────
_VISION_KW = {
    "screenshot", "screen", "camera", "image", "picture", "photo", "ocr",
    "diagram", "topology", "visual", "webcam",
    "captura", "pantalla", "imagen", "foto", "cámara", "camara",
    "diagrama", "topología", "topologia", "visión", "vision",
}
_EMBEDDING_KW = {
    "index", "indexing", "rag", "vector", "vectorize", "embedding", "embeddings",
    "retrieval", "knowledge base", "ingest",
    "indexar", "indexa", "vectorizar", "vectoriza", "incrustaciones",
    "base de conocimiento", "recuperación",
}
_CODER_KW = {
    "python", "javascript", "typescript", "code", "coding", "repo", "repository",
    "refactor", "refactoring", "unit test", "tests", "pytest", "debug", "debugging",
    "function", "class", "compile", "stacktrace", "traceback", "lint", "bug",
    "código", "codigo", "programa", "programar", "depurar", "función", "funcion",
    "clase", "compilar", "prueba", "pruebas", "refactorizar",
}
_DEEP_KW = {
    "architecture", "architect", "threat model", "threat-model", "dfir", "forensic",
    "forensics", "grc", "governance", "incident", "incident response", "root cause",
    "correlate", "correlation", "kill chain", "attack chain", "deep analysis",
    "post-mortem", "postmortem", "strategy", "tradeoff", "trade-off",
    "arquitectura", "modelo de amenazas", "amenaza", "forense", "gobernanza",
    "incidente", "respuesta a incidentes", "causa raíz", "causa raiz",
    "correlacionar", "correlación", "correlacion", "análisis profundo",
    "analisis profundo", "cadena de ataque", "estrategia",
}
_VERIFIER_KW = {
    "review", "verify", "validate", "fact-check", "factuality", "audit",
    "revisar", "verificar", "validar", "auditar", "comprobar",
}
_SECURITY_KW = {
    "exploit", "payload", "malware", "ransomware", "rootkit", "backdoor",
    "privilege escalation", "credential", "exfiltrate", "exfiltration",
    "vulnerability", "cve", "shellcode", "c2", "beacon", "lateral movement",
    "exploit", "vulnerabilidad", "credencial", "malware", "escalada de privilegios",
    "exfiltrar", "exfiltración", "movimiento lateral",
}


def _kw_hits(text: str, vocab: set[str]) -> int:
    return sum(1 for kw in vocab if kw in text)


@dataclass(frozen=True)
class ModelDecision:
    """Resolved routing decision for a single prompt."""
    role: ModelRole
    provider: str          # "ollama" | "cloud"
    model: str
    complexity: float
    reason: str
    requires_verification: bool


def route(
    prompt: str,
    *,
    force_role: "ModelRole | None" = None,
    security_sensitive: bool = False,
    allow_cloud: bool = False,
) -> ModelDecision:
    """Classify *prompt* into a cognitive role and resolve its model.

    Precedence: explicit force_role → vision/embedding modality →
    coding intent → deep analysis (keyword or high complexity) → fast default.
    Cloud is only ever selected when both globally enabled AND requested for
    this call (``allow_cloud``); it never triggers implicitly.
    """
    text = (prompt or "").lower()
    complexity = calculate_complexity(prompt or "")
    sec_hits = _kw_hits(text, _SECURITY_KW)
    is_security = security_sensitive or sec_hits > 0

    if force_role is not None:
        role = force_role
        reason = f"forced:{role.value}"
    elif _kw_hits(text, _VISION_KW):
        role, reason = ModelRole.VISION, "vision keywords (image/screen/diagram)"
    elif _kw_hits(text, _EMBEDDING_KW):
        role, reason = ModelRole.EMBEDDING, "embedding/RAG keywords"
    elif _kw_hits(text, _DEEP_KW) >= 1 and _kw_hits(text, _DEEP_KW) >= _kw_hits(text, _CODER_KW):
        role, reason = ModelRole.DEEP, "deep-analysis keywords (architecture/DFIR/threat)"
    elif _kw_hits(text, _CODER_KW):
        role, reason = ModelRole.CODER, "coding keywords (python/refactor/tests/debug)"
    elif complexity > COMPLEXITY_THRESHOLD:
        role, reason = ModelRole.DEEP, f"high complexity score ({complexity:.2f})"
    else:
        role, reason = ModelRole.FAST, "simple command/chat"

    # Cloud escalation: explicit + globally enabled only.
    provider = "ollama"
    model = model_for_role(role)
    if allow_cloud and cloud_enabled():
        provider = "cloud"
        model = model_for_role(ModelRole.CLOUD)
        role = ModelRole.CLOUD
        reason += " → cloud escalation"

    requires_verification = bool(
        is_security or role in (ModelRole.DEEP, ModelRole.CLOUD)
        or _kw_hits(text, _VERIFIER_KW) > 0
    )

    return ModelDecision(
        role=role,
        provider=provider,
        model=model,
        complexity=complexity,
        reason=reason,
        requires_verification=requires_verification,
    )


# ════════════════════════════════════════════════════════════════════════════
#  V61 — Security-sensitive turn classifier (Phase 2)
#  Pure predicate used by the live LLM path to decide whether a turn needs
#  security-grade handling (forces verification, stricter trust treatment).
# ════════════════════════════════════════════════════════════════════════════

# Tools whose presence makes a turn security-sensitive. A superset of the
# executor's NATO-gated set — it also covers consent-gated multimodal surfaces
# (screen / clipboard / camera) that are HITL-exempt but whose *output* still
# warrants scrutiny, and the input-injection tools (type_text/press_hotkey).
_DANGEROUS_TOOLS: frozenset[str] = frozenset({
    "run_shell_command", "code_execute", "http_request", "write_file",
    "kill_process", "network_scan", "osint_lookup", "take_screenshot",
    "escanear_pantalla", "get_clipboard", "set_clipboard", "type_text",
    "press_hotkey", "open_application", "open_software", "packet_tracer_open",
    "whois_lookup", "estudiar_tema", "desplegar_webapp", "fetch_webpage",
    "capture_camera", "webcam_capture", "analyze_room",
})

# DFIR / incident-response / forensics vocabulary (EN + ES). Complements the
# offensive _SECURITY_KW set above with the defensive-analysis terms.
_DFIR_KW = {
    "dfir", "incident response", "incident-response", "incident handling",
    "forensic", "forensics", "threat model", "threat-model", "threat modeling",
    "memory dump", "triage", "kill chain", "kill-chain",
    "respuesta a incidentes", "manejo de incidentes", "forense", "forensia",
    "modelo de amenazas", "análisis forense", "analisis forense",
}

# Bare high-signal terms the directive flags as sensitive that are not already
# covered by _SECURITY_KW (which has "privilege escalation" but not "privilege",
# "shell", "token", "persistence", etc.). Kept narrow to avoid false positives
# (e.g. "key" alone is too broad — only credential-shaped "api key" qualifies).
_SECSENS_EXTRA_KW = {
    "shell", "powershell", "reverse shell", "token", "persistence",
    "privilege", "credential", "credentials", "password", "passphrase",
    "api key", "api-key", "secret key", "private key", "access key",
    "exfiltration", "exfiltrate", "c2", "command and control", "implant",
    "credencial", "credenciales", "contraseña", "persistencia",
    "exfiltración", "clave api", "clave privada",
}

# Code-generation intent crossed with a dangerous capability domain →
# security-sensitive (e.g. "write a script that opens a socket and runs shell").
_CODEGEN_KW = {
    "code", "script", "program", "function", "write a", "generate",
    "implement", "snippet", "write me",
    "código", "codigo", "programa", "función", "funcion",
    "genera", "implementa", "escribe un", "escríbeme", "escribeme",
}
_DANGEROUS_DOMAIN_KW = {
    "shell", "subprocess", "socket", "network", "auth", "authentication",
    "login", "password", "crypto", "encrypt", "decrypt", "delete", "remove",
    "persistence", "token", "credential", "registry", "exec", "eval",
    "autenticación", "autenticacion", "contraseña", "cifrar", "descifrar",
    "borrar", "eliminar", "persistencia", "credencial", "registro",
}

_SECSENS_ALL_KW = _SECURITY_KW | _DFIR_KW | _SECSENS_EXTRA_KW


def is_security_sensitive_turn(
    user_message: str,
    tool_names: list[str] | None = None,
) -> bool:
    """Conservative predicate: does this turn warrant security-grade handling?

    Returns True when:
      * any dangerous / consent-gated tool is in play (``tool_names``), or
      * the message hits offensive-security, DFIR/forensics, or credential/
        shell/persistence vocabulary, or
      * it asks for code that touches a dangerous capability domain
        (shell / network / auth / crypto / deletion / persistence / tokens /
        credentials).

    Pure and dependency-free. Kept conservative but usable — plain chat such as
    "what time is it?" or "tell me a joke" must NOT trip it.
    """
    if tool_names and any((t or "") in _DANGEROUS_TOOLS for t in tool_names):
        return True
    text = (user_message or "").lower()
    if not text:
        return False
    if _kw_hits(text, _SECSENS_ALL_KW):
        return True
    if _kw_hits(text, _CODEGEN_KW) and _kw_hits(text, _DANGEROUS_DOMAIN_KW):
        return True
    return False


def resolve_inference_model(decision: ModelDecision) -> str:
    """Map a routing *decision* to a concrete, tool-call-capable Ollama model.

    ``route()`` may name role-default models (qwen2.5-coder:7b, deepseek-r1:14b,
    moondream, nomic-embed-text, …) that are either not pulled on this host or
    unfit for the tool-use streaming path (vision/embedding models can't chat;
    reasoning models stream <think> noise and call tools poorly). To keep the
    live turn robust we:

      1. honor an explicit per-role env override when the operator set one
         (they opted into that exact model), else
      2. fall back to the boot-resolved dual models (``MODEL_FAST`` /
         ``MODEL_DEEP``) that the dependency guardian confirmed are available.

    Cloud is never streamed from this local client — the live path passes
    ``allow_cloud=False`` — so a CLOUD decision maps to the deep local model.
    """
    role = decision.role
    env_key = _ROLE_ENV.get(role)
    if env_key and os.getenv(env_key):
        return decision.model
    if role in (ModelRole.CODER, ModelRole.DEEP, ModelRole.CLOUD):
        return MODEL_DEEP
    return MODEL_FAST


async def configure_ollama_for_hardware(hw_profile) -> None:
    """Log optimal ollama serve flags for the operator."""
    # v46.0: parallelism must match actual recommended pools — on battery
    # pools=1 even when RAM is dual-channel, so reading pools dynamically
    # prevents the hardcoded =2 mismatch with the resolved profile.
    parallel = getattr(hw_profile, "recommended_pools",
                       getattr(hw_profile, "pools", 1))
    keep_alive = "30m" if hw_profile.is_dual_channel else "10m"
    logger.info(
        f"OLLAMA CONFIG: "
        f"OLLAMA_NUM_PARALLEL={parallel} "
        f"OLLAMA_KEEP_ALIVE={keep_alive} "
        f"OLLAMA_MAX_LOADED_MODELS={parallel} "
        f"ollama serve"
    )
