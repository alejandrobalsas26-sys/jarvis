"""
tests/test_runtime_config_unification.py — V66.1 runtime configuration unification.

Locks in the single source of truth for model roles so the split-brain that let
legacy Qwen2.5 general models leak into the runtime (while the live LLM turn used
Qwen3) can never come back:

  * explicit JARVIS_MODEL_* env override always wins (per role);
  * central role config resolves when no override is set;
  * hardware recommendation is advisory and NEVER overrides operator config;
  * the precise installed-matcher rejects the family-prefix false positive that
    matched qwen2.5:*-instruct against qwen2.5-coder;
  * dependency_guardian.resolve_models honors the unified resolver;
  * OLLAMA_HOST normalization tolerates bare hosts;
  * CPU/RAM-aware recommendations don't treat 64 GB RAM as useless.

No Ollama required — the pulled-model set is stubbed.
"""
from __future__ import annotations

import asyncio

import pytest

from core.model_router import (
    ModelRole,
    _model_installed,
    normalize_ollama_host,
    resolve_deep_model,
    resolve_fast_model,
    resolve_role_model,
)

# The operator's actual installed models on the Ryzen 5 7430U host.
INSTALLED = [
    "qwen3:8b", "qwen3:14b", "qwen2.5-coder:latest",
    "gemma3:4b", "nomic-embed-text:latest",
]

# The operator's intended role configuration.
OPERATOR_ENV = {
    "JARVIS_MODEL_FAST": "qwen3:8b",
    "JARVIS_MODEL_CODER": "qwen2.5-coder:latest",
    "JARVIS_MODEL_DEEP": "qwen3:14b",
    "JARVIS_MODEL_VISION": "gemma3:4b",
    "JARVIS_MODEL_EMBEDDING": "nomic-embed-text:latest",
    "JARVIS_MODEL_VERIFIER": "qwen3:8b",
}

_ROLE_ENV_KEYS = list(OPERATOR_ENV) + [
    "JARVIS_CLOUD_ENABLED", "JARVIS_CLOUD_MODEL", "JARVIS_VISION_MODEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ROLE_ENV_KEYS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def operator_env(monkeypatch):
    for k, v in OPERATOR_ENV.items():
        monkeypatch.setenv(k, v)
    yield


# ── Precedence level 1: explicit env override always wins ────────────────────
class TestEnvOverridePrecedence:
    @pytest.mark.parametrize("role,expected", [
        (ModelRole.FAST, "qwen3:8b"),
        (ModelRole.CODER, "qwen2.5-coder:latest"),
        (ModelRole.DEEP, "qwen3:14b"),
        (ModelRole.VISION, "gemma3:4b"),
        (ModelRole.EMBEDDING, "nomic-embed-text:latest"),
        (ModelRole.VERIFIER, "qwen3:8b"),
    ])
    def test_each_role_resolves_to_operator_config(self, operator_env, role, expected):
        assert resolve_role_model(role, installed=INSTALLED) == expected

    def test_env_override_wins_even_when_not_pulled(self, monkeypatch):
        # Operator opted into an exact model — honor it verbatim even if absent.
        monkeypatch.setenv("JARVIS_MODEL_DEEP", "qwen3:32b")
        assert resolve_role_model(ModelRole.DEEP, installed=INSTALLED) == "qwen3:32b"

    def test_fast_deep_helpers(self, operator_env):
        assert resolve_fast_model(INSTALLED) == "qwen3:8b"
        assert resolve_deep_model(INSTALLED) == "qwen3:14b"


# ── Precedence level 2-5: central config / hw hint / fallback ────────────────
class TestNonEnvPrecedence:
    def test_central_config_when_no_env(self):
        assert resolve_role_model(ModelRole.FAST, installed=INSTALLED) == "qwen3:8b"
        assert resolve_role_model(ModelRole.DEEP, installed=INSTALLED) == "qwen3:14b"
        assert resolve_role_model(ModelRole.VISION, installed=INSTALLED) == "gemma3:4b"

    def test_hardware_recommendation_never_overrides_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_FAST", "qwen3:8b")
        # A legacy hw hint must NOT win over the explicit operator override.
        got = resolve_role_model(
            ModelRole.FAST, installed=INSTALLED,
            hw_recommendation="qwen2.5:7b-instruct-q5_K_M",
        )
        assert got == "qwen3:8b"

    def test_hardware_recommendation_used_only_when_central_absent(self):
        # Central FAST (qwen3:8b) is NOT in this installed set → fall to hw hint.
        installed = ["llama3.1:8b"]
        got = resolve_role_model(
            ModelRole.FAST, installed=installed, hw_recommendation="llama3.1:8b",
        )
        assert got == "llama3.1:8b"

    def test_pure_config_resolution_without_installed(self):
        # installed=None → env → central, no Ollama query, no noise.
        assert resolve_role_model(ModelRole.DEEP) == "qwen3:14b"


# ── The prefix-bug regression (root cause) ───────────────────────────────────
class TestInstalledMatcher:
    def test_legacy_qwen25_not_matched_by_coder_prefix(self):
        # This false match is what leaked the legacy fast/deep pair.
        assert _model_installed("qwen2.5:7b-instruct-q5_K_M", INSTALLED) is False
        assert _model_installed("qwen2.5:14b-instruct-q4_K_M", INSTALLED) is False

    def test_exact_and_family_matches(self):
        assert _model_installed("qwen3:8b", INSTALLED) is True
        assert _model_installed("qwen2.5-coder:latest", INSTALLED) is True
        # untagged name matches any tag of the same repo
        assert _model_installed("qwen3", INSTALLED) is True
        # tag prefix (qwen3:8b matches an installed qwen3:8b-instruct-q4)
        assert _model_installed("qwen3:8b", ["qwen3:8b-instruct-q4_K_M"]) is True

    def test_distinct_sizes_do_not_match(self):
        assert _model_installed("qwen3:8b", ["qwen3:14b"]) is False


# ── OLLAMA_HOST normalization ────────────────────────────────────────────────
class TestHostNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("127.0.0.1", "http://127.0.0.1:11434"),
        ("127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("localhost", "http://localhost:11434"),
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://localhost:11434", "http://localhost:11434"),
        ("", "http://127.0.0.1:11434"),
        (None, "http://127.0.0.1:11434"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_ollama_host(raw) == expected


# ── dependency_guardian.resolve_models honors the unified resolver ───────────
class TestGuardianResolution:
    def test_guardian_returns_operator_config(self, operator_env, monkeypatch):
        import core.dependency_guardian as guardian

        monkeypatch.setattr(guardian, "_get_pulled_models", lambda: set(INSTALLED))

        class _HW:  # legacy hardcoded hints — must NOT override env config
            model_fast = "qwen2.5:7b-instruct-q5_K_M"
            model_deep = "qwen2.5:14b-instruct-q4_K_M"

        fast, deep = asyncio.run(guardian.resolve_models(_HW()))
        assert fast == "qwen3:8b"
        assert deep == "qwen3:14b"

    def test_guardian_without_env_uses_central_not_legacy(self, monkeypatch):
        import core.dependency_guardian as guardian
        monkeypatch.setattr(guardian, "_get_pulled_models", lambda: set(INSTALLED))

        class _HW:
            model_fast = "qwen2.5:7b-instruct-q5_K_M"
            model_deep = "qwen2.5:14b-instruct-q4_K_M"

        fast, deep = asyncio.run(guardian.resolve_models(_HW()))
        # Central config (qwen3), never the legacy hw hint.
        assert fast == "qwen3:8b"
        assert deep == "qwen3:14b"

    def test_guardian_ollama_unreachable_still_honors_env(self, operator_env, monkeypatch):
        import core.dependency_guardian as guardian
        monkeypatch.setattr(guardian, "_get_pulled_models", lambda: set())  # down
        fast, deep = asyncio.run(guardian.resolve_models(None))
        assert (fast, deep) == ("qwen3:8b", "qwen3:14b")


# ── CPU/RAM-aware recommendation (64 GB is not useless) ──────────────────────
class TestCpuRamRecommendation:
    def test_abundant_ram_gets_full_pair(self):
        from core.hardware_model_profile import recommended_models_for_cpu_ram
        rec = recommended_models_for_cpu_ram(64.0)
        assert rec["fast"] == "qwen3:8b"
        assert rec["deep"] == "qwen3:14b"
        assert rec["vision"] == "gemma3:4b"

    def test_recommendations_are_modern_no_legacy(self):
        from core.hardware_model_profile import _TIER_MODELS
        blob = " ".join(m for role_map in _TIER_MODELS.values()
                        for m in role_map.values())
        for legacy in ("deepseek-r1", "moondream", "llava"):
            assert legacy not in blob, f"legacy model {legacy} still recommended"
