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

_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _get_tags() -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{_HOST}/api/tags", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _model_present(model: str, pulled: list[str]) -> bool:
    """A role model matches if its base name (before ':') is pulled in any tag."""
    base = model.split(":")[0]
    return any(p == model or p.split(":")[0] == base for p in pulled)


def _tiny_generate(model: str) -> tuple[bool, str]:
    payload = json.dumps({
        "model": model,
        "prompt": "Reply with the single word: OK",
        "stream": False,
        "options": {"num_predict": 5},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        return True, (data.get("response", "") or "").strip()[:40]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:60]


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

    # Tiny smoke prompts against FAST + VERIFIER when present.
    rc = 0
    for role in ("fast", "verifier"):
        model = roles[role]
        if not _model_present(model, pulled):
            print(f"  [WARN] {role} smoke test skipped - {model} not pulled.")
            continue
        ok, out = _tiny_generate(model)
        if ok:
            print(f"  [ OK ] {role} ({model}) responded: {out!r}")
        else:
            print(f"  [WARN] {role} ({model}) did not respond: {out}")
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
