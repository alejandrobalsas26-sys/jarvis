#!/usr/bin/env python3
"""
scripts/model_doctor.py - JARVIS model/Ollama diagnostic (V60.0, Phase 4).

Checks that the models the role router expects are actually pulled, recommends
the missing `ollama pull` commands, and (when Ollama is up) fires a tiny prompt
at the FAST and VERIFIER models to confirm they respond.

Requires neither GPU, internet, nor API keys. Stdlib-only HTTP. By default it
WARNs (exit 0) when Ollama is unreachable; pass --require-ollama to make that a
hard failure (exit 1) for CI in an Ollama-backed environment.

Run from the jarvis/ directory:
    python scripts/model_doctor.py
    python scripts/model_doctor.py --require-ollama
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_JARVIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JARVIS_DIR))


def _load_env() -> None:
    """Load jarvis/.env so the doctor reads the SAME configuration as the live
    runtime (main.py calls load_dotenv() at boot; standalone scripts didn't, so
    JARVIS_MODEL_* / OLLAMA_HOST were previously invisible here). Existing real
    environment variables are never overridden — matching load_dotenv semantics.
    """
    env_path = _JARVIS_DIR / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except Exception:
        pass
    # stdlib-only fallback parser (dotenv unavailable)
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.split("#", 1)[0].strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)
    except Exception:
        pass


_load_env()

# Normalized Ollama base URL — tolerates bare hosts (e.g. 127.0.0.1) that the
# runtime's model_router also normalizes, so a bare OLLAMA_HOST from the Windows
# user environment can't produce an invalid "127.0.0.1/api/tags".
try:
    from core.model_router import normalize_ollama_host, _model_installed
    _HOST = normalize_ollama_host()
except Exception:  # pragma: no cover — never block the doctor on import issues
    _HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if "://" not in _HOST:
        _HOST = "http://" + _HOST
    _model_installed = None  # type: ignore


def _get_tags() -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{_HOST}/api/tags", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _model_present(model: str, pulled: list[str]) -> bool:
    """True iff a pulled model satisfies *model* (precise repo+tag match, shared
    with the runtime's resolver so the doctor agrees with live selection)."""
    if _model_installed is not None:
        return _model_installed(model, pulled)
    base = model.split(":")[0]
    return any(p == model or p.split(":")[0] == base for p in pulled)


# CPU inference on first use pays a model-load penalty, so the smoke timeout must
# be generous but still bounded.
_SMOKE_TIMEOUT_S = 120


def _smoke_generate(model: str, timeout: int = _SMOKE_TIMEOUT_S) -> tuple[str, str]:
    """Fire ONE tiny prompt at *model* and classify the outcome. Returns
    (status, detail) where status is one of:

      ok            — visible text generated
      empty_visible — HTTP 200 but no visible text (e.g. a thinking model that
                      emitted only hidden reasoning) — generation still works
      missing       — model not pulled (HTTP 404 / 'not found')
      timeout       — no response within the (CPU-realistic) deadline
      unreachable   — Ollama not answering
      malformed     — non-JSON / unexpected response
      error         — other HTTP error

    ``think: false`` disables qwen3-style reasoning so a thinking model returns
    a non-empty visible answer instead of spending the token budget on hidden
    <think> content (the old num_predict=5 test returned empty for qwen3).
    """
    payload = json.dumps({
        "model": model,
        "prompt": "Reply with exactly: OK",
        "stream": False,
        "think": False,
        "options": {"num_predict": 24, "temperature": 0.0},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        if e.code == 404 or "not found" in body.lower():
            return "missing", f"model not found (HTTP {e.code})"
        return "error", f"HTTP {e.code}: {body[:80]}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return "timeout", f"no response in {timeout}s (CPU load latency?)"
        return "unreachable", f"Ollama unreachable: {str(reason)[:60]}"
    except (TimeoutError, OSError) as e:
        return "timeout", f"no response in {timeout}s ({str(e)[:40]})"
    try:
        data = json.loads(raw)
    except ValueError:
        return "malformed", "non-JSON response from /api/generate"
    text = (data.get("response") or "").strip()
    if text:
        return "ok", text[:60]
    thinking = (data.get("thinking") or "").strip()
    if thinking:
        return "empty_visible", f"only hidden reasoning ({len(thinking)} chars), no visible text"
    return "empty_visible", "HTTP 200 but empty visible response"


def main() -> int:
    require_ollama = "--require-ollama" in sys.argv

    # Resolve role → configured model from the router (honors env overrides).
    try:
        from core.model_router import ModelRole, model_for_role
        roles = {
            "fast": model_for_role(ModelRole.FAST),
            "coder": model_for_role(ModelRole.CODER),
            "deep": model_for_role(ModelRole.DEEP),
            "vision": model_for_role(ModelRole.VISION),
            "embedding": model_for_role(ModelRole.EMBEDDING),
            "verifier": model_for_role(ModelRole.VERIFIER),
        }
    except Exception as e:
        print(f"  [FAIL] could not import role router: {e}")
        return 1

    print("\n  JARVIS MODEL DOCTOR\n  " + "=" * 50)
    print(f"  Ollama host: {_HOST}\n")

    # Hardware-tier recommendation (best-effort, never fatal).
    try:
        from core.hardware_model_profile import detect_model_profile
        prof = detect_model_profile()
        print(f"  Hardware tier: {prof.tier.value}  "
              f"(RAM {prof.total_ram_gb}GB | {prof.cpu_cores} cores | "
              f"GPU {prof.gpu_vendor} {prof.gpu_vram_gb}GB)")
        print("  Recommended for this tier:")
        for role, model in prof.recommended_models.items():
            print(f"    - {role:9s} {model}")
        print()
    except Exception as e:
        print(f"  [WARN] hardware tier probe skipped: {e}\n")

    print("  Configured role models:")
    for role, model in roles.items():
        print(f"    - {role:9s} {model}")
    print()

    pulled = _get_tags()
    if pulled is None:
        msg = f"Ollama not reachable at {_HOST} (start `ollama serve`)."
        if require_ollama:
            print(f"  [FAIL] {msg}\n")
            return 1
        print(f"  [WARN] {msg}")
        print("  Skipping pull/probe checks - install models with the commands below.\n")
        for cmd in _pull_commands(roles):
            print(f"    {cmd}")
        print()
        return 0

    missing = {role: m for role, m in roles.items() if not _model_present(m, pulled)}
    if missing:
        print(f"  [WARN] {len(missing)} role model(s) not pulled:")
        for role, model in missing.items():
            print(f"    - {role:9s} {model}   ->  ollama pull {model}")
        print()
    else:
        print("  [ OK ] all configured role models are pulled.\n")

    # Bounded smoke prompts against FAST + VERIFIER when present. Thinking-model
    # aware: a model that generates only hidden reasoning is reported distinctly
    # (WARN, not FAIL) from a real failure (missing / timeout / unreachable).
    rc = 0
    for role in ("fast", "verifier"):
        model = roles[role]
        if not _model_present(model, pulled):
            print(f"  [WARN] {role} smoke test skipped - {model} not pulled.")
            continue
        status, detail = _smoke_generate(model)
        if status == "ok":
            print(f"  [ OK ] {role} ({model}) responded: {detail!r}")
        elif status == "empty_visible":
            print(f"  [WARN] {role} ({model}) generated but no visible text: {detail}")
        else:
            print(f"  [{'FAIL' if require_ollama else 'WARN'}] "
                  f"{role} ({model}) {status}: {detail}")
            if require_ollama:
                rc = 1
    print()
    return rc


def _pull_commands(roles: dict[str, str]) -> list[str]:
    seen: list[str] = []
    for model in roles.values():
        cmd = f"ollama pull {model}"
        if cmd not in seen:
            seen.append(cmd)
    return seen


if __name__ == "__main__":
    raise SystemExit(main())
