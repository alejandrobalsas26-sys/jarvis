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
from urllib.parse import urlparse

import httpx
from loguru import logger

# ── Central role-model configuration (precedence level 2) ────────────────────
# Modern, CPU/RAM-friendly local defaults for the Ryzen-class homelab host.
# Explicit ``JARVIS_MODEL_*`` env overrides (level 1) always win. Kept as plain
# strings so the legacy ``MODEL_FAST``/``MODEL_DEEP`` globals and the
# ``ModelRole`` map below share ONE source of truth (no drift).
_DEFAULT_FAST      = "qwen3:8b"
_DEFAULT_CODER     = "qwen2.5-coder:latest"
_DEFAULT_DEEP      = "qwen3:14b"
_DEFAULT_VISION    = "gemma3:4b"
_DEFAULT_EMBEDDING = "nomic-embed-text:latest"
_DEFAULT_VERIFIER  = "qwen3:8b"

_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


def normalize_ollama_host(raw: str | None = None) -> str:
    """Return a well-formed ``scheme://host:port`` Ollama base URL.

    Tolerates the bare forms operators (and ``windows_hardener``) sometimes set,
    e.g. ``127.0.0.1`` or ``localhost:11434``, which would otherwise produce an
    invalid ``127.0.0.1/api/tags`` when a caller does ``f"{host}/api/tags"``.
    Missing scheme defaults to ``http``; missing port defaults to ``11434``.
    """
    val = (raw if raw is not None else os.getenv("OLLAMA_HOST", "") or "").strip()
    if not val:
        return _DEFAULT_OLLAMA_HOST
    if "://" not in val:
        val = "http://" + val
    parsed = urlparse(val)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 11434
    return f"{scheme}://{host}:{port}"


MODEL_FAST = os.getenv("JARVIS_MODEL_FAST", _DEFAULT_FAST)
MODEL_DEEP = os.getenv("JARVIS_MODEL_DEEP", _DEFAULT_DEEP)
OLLAMA_URL = normalize_ollama_host()
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
# CPU-bound homelab hardware with ample system RAM — small fast model, dedicated
# coder, reasoning deep, small VLM. Single source of truth = the _DEFAULT_*
# constants near the top of this module (shared with MODEL_FAST/MODEL_DEEP).
_ROLE_DEFAULTS: dict[ModelRole, str] = {
    ModelRole.FAST:      _DEFAULT_FAST,
    ModelRole.CODER:     _DEFAULT_CODER,
    ModelRole.DEEP:      _DEFAULT_DEEP,
    ModelRole.VISION:    _DEFAULT_VISION,
    ModelRole.EMBEDDING: _DEFAULT_EMBEDDING,
    ModelRole.VERIFIER:  _DEFAULT_VERIFIER,
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


# ════════════════════════════════════════════════════════════════════════════
#  V66.1 — Unified role-model resolution (single source of truth)
#  One precedence ladder every live consumer resolves through, so the operator's
#  explicit config always wins and hardware profiling stays advisory.
# ════════════════════════════════════════════════════════════════════════════


def _norm_installed(installed) -> set[str]:
    return {str(m).strip() for m in (installed or []) if str(m).strip()}


def _model_installed(name: str, installed) -> bool:
    """True iff *name* is satisfied by a pulled model in *installed*.

    Precise ``repo`` + ``tag`` match — deliberately stricter than the historical
    ``p.startswith(name.split(':')[0])`` heuristic, which wrongly reported e.g.
    ``qwen2.5:7b-instruct-q5_K_M`` as present merely because the unrelated
    ``qwen2.5-coder:latest`` shares the ``qwen2.5`` family prefix. That false
    match is the root cause of the legacy Qwen2.5 fast/deep pair leaking into the
    runtime, so it must never come back.
    """
    if not name:
        return False
    inst = _norm_installed(installed)
    if name in inst:
        return True
    repo, _, tag = name.partition(":")
    for p in inst:
        p_repo, _, p_tag = p.partition(":")
        if p_repo != repo:
            continue
        if not tag:                      # untagged name → any tag of this repo counts
            return True
        if p_tag == tag or p_tag.startswith(tag + "-") or p_tag.startswith(tag + "."):
            return True
        if "latest" in (p_tag, tag):     # 'latest' is treated as compatible
            return True
    return False


def resolve_role_model(
    role: "ModelRole | str",
    *,
    installed=None,
    hw_recommendation: str | None = None,
) -> str:
    """Resolve the concrete model for *role* under the unified precedence:

      1. explicit ``JARVIS_MODEL_*`` env override — operator opted in, so it
         always wins (even if not currently pulled; a warning is logged when
         *installed* is known and it is absent).
      2. central role-model configuration (``_ROLE_DEFAULTS``).
      3. hardware-aware recommendation (advisory; TDP / VRAM tier hint).
      4. installed-compatible fallback (first pulled model in the role family).
      5. safe final fallback (the central default).

    *installed* (list of pulled model names) gates levels 2-4 so a model the host
    cannot run is never handed back — EXCEPT an explicit env override, honored
    verbatim. Pass ``installed=None`` for pure config resolution (no Ollama
    query), which resolves env → central and never emits noise.
    """
    role = role if isinstance(role, ModelRole) else ModelRole(role)
    inst = _norm_installed(installed) if installed is not None else None

    # 1) explicit env override — always wins.
    env_key = _ROLE_ENV.get(role)
    env_val = (os.getenv(env_key) or "").strip() if env_key else ""
    if env_val:
        if inst is not None and not _model_installed(env_val, inst):
            logger.warning(
                f"MODEL_RESOLVE: {role.value} override '{env_val}' is not pulled — "
                f"honoring operator config; run: ollama pull {env_val}"
            )
        return env_val

    central = _ROLE_DEFAULTS.get(role, _DEFAULT_FAST)

    # No installed set → pure config resolution (env → central).
    if inst is None:
        return central

    # 2) central config, if actually installed.
    if _model_installed(central, inst):
        return central
    # 3) hardware recommendation, if installed.
    if hw_recommendation and _model_installed(hw_recommendation, inst):
        return hw_recommendation
    # 4) installed-compatible fallback: first pulled model sharing the role family.
    fam = central.partition(":")[0]
    for p in sorted(inst):
        if p.partition(":")[0] == fam:
            return p
    # 5) safe final fallback — central default (guardian surfaces the pull hint).
    logger.warning(
        f"MODEL_RESOLVE: no pulled model for {role.value} (central='{central}') — "
        f"falling back to central default"
    )
    return central


def resolve_fast_model(installed=None, hw_recommendation: str | None = None) -> str:
    """FAST-role model via the unified precedence (see ``resolve_role_model``)."""
    return resolve_role_model(ModelRole.FAST, installed=installed,
                              hw_recommendation=hw_recommendation)


def resolve_deep_model(installed=None, hw_recommendation: str | None = None) -> str:
    """DEEP-role model via the unified precedence (see ``resolve_role_model``)."""
    return resolve_role_model(ModelRole.DEEP, installed=installed,
                              hw_recommendation=hw_recommendation)


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
