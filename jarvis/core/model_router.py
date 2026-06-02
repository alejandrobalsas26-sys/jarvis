"""
core/model_router.py — Dual-Model Intelligent Routing for Ollama.

Routes prompts to fast (7B Q4) or deep (14B Q4) model based on
complexity score computed from length, vocabulary density, and
depth-keyword hits. Auto-upgrades fast model to Q8 on 64GB hardware.
"""

import os
import re

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
