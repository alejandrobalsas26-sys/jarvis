"""tests/test_role_safe_inference_v67.py — V67 M27 role-safe inference surfaces.

Locks the M27 invariant: ROLE SELECTION (which ModelRole a turn is) is separate
from INFERENCE SURFACE CAPABILITY (what a concrete model can do at the wire). A
prompt classified EMBEDDING must NEVER stream chat from nomic-embed-text, and a
VISION request must use gemma3 for image understanding while the conversational
synthesis path stays chat/tool capable.

No Ollama required — capability resolution and routing are pure. MODEL_FAST /
MODEL_DEEP resolve to their qwen3 defaults in the test process (env unset at
import), both chat-capable.
"""
from __future__ import annotations

import pytest

from core.model_capabilities import (
    InferenceSurface,
    ModelCapability,
    capabilities_for,
    is_chat_safe,
    supports_surface,
)
from core.model_router import (
    ModelRole,
    resolve_embedding_model,
    resolve_inference_model,
    resolve_vision_model,
    route,
)

_ROLE_ENV = (
    "JARVIS_MODEL_FAST", "JARVIS_MODEL_CODER", "JARVIS_MODEL_DEEP",
    "JARVIS_MODEL_VISION", "JARVIS_MODEL_EMBEDDING", "JARVIS_MODEL_VERIFIER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ROLE_ENV:
        monkeypatch.delenv(var, raising=False)
    yield


# ── Capability registry ───────────────────────────────────────────────────────
class TestCapabilities:
    def test_embedding_model_is_embedding_only(self):
        caps = capabilities_for("nomic-embed-text:latest")
        assert caps == frozenset({ModelCapability.EMBEDDING_CAPABLE})
        assert not is_chat_safe("nomic-embed-text:latest")
        assert supports_surface("nomic-embed-text:latest", InferenceSurface.EMBEDDING)
        assert not supports_surface("nomic-embed-text:latest", InferenceSurface.CHAT)

    def test_any_embed_named_model_is_embedding_only(self):
        # defense-in-depth: an unrecognized "*embed*" family is still not chattable
        assert not is_chat_safe("some-new-embed-model:v2")
        assert supports_surface("mxbai-embed-large", InferenceSurface.EMBEDDING)

    def test_vision_model_can_see_but_not_tool_chat(self):
        assert supports_surface("gemma3:4b", InferenceSurface.VISION)
        assert not is_chat_safe("gemma3:4b")  # weak tool-caller → excluded from chat stream
        assert not supports_surface("llava:latest", InferenceSurface.CHAT)
        assert supports_surface("llava:latest", InferenceSurface.VISION)
        assert not supports_surface("moondream:latest", InferenceSurface.CHAT)

    def test_general_chat_tool_models(self):
        for m in ("qwen3:8b", "qwen3:14b", "qwen2.5-coder:latest", "llama3.1:8b"):
            assert is_chat_safe(m), m
            assert supports_surface(m, InferenceSurface.CHAT)

    def test_unknown_model_assumed_chat_capable(self):
        # a custom/newer pulled model is not silently downgraded
        assert is_chat_safe("some-brand-new-model:latest")


# ── THE acceptance invariant: EMBEDDING never streams chat ────────────────────
class TestEmbeddingNeverChats:
    def test_embedding_role_with_env_override_does_not_reach_chat(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_EMBEDDING", "nomic-embed-text:latest")
        decision = route("Index these PDFs into the vector knowledge base")
        assert decision.role is ModelRole.EMBEDDING  # role selection unchanged
        model = resolve_inference_model(decision)     # CHAT surface (default)
        assert "nomic" not in model
        assert is_chat_safe(model), f"chat surface resolved a non-chat model: {model}"

    def test_vision_role_with_env_override_does_not_reach_chat(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_VISION", "gemma3:4b")
        decision = route("Take a screenshot and OCR the screen")
        assert decision.role is ModelRole.VISION
        model = resolve_inference_model(decision)
        assert model != "gemma3:4b"
        assert is_chat_safe(model)

    @pytest.mark.parametrize("prompt", [
        "hola, qué hora es?",
        "Refactor this Python function and add pytest tests",
        "Analyze this forensic memory dump for the incident root cause",
        "Index these documents into the RAG vector store",
        "Capture the screen and analyze the network diagram",
        "explain this exploit payload and the CVE",
    ])
    def test_every_routed_turn_resolves_a_chat_safe_model(self, prompt, monkeypatch):
        # even with the full operator role config set, the CHAT surface is always safe
        for k, v in {
            "JARVIS_MODEL_FAST": "qwen3:8b",
            "JARVIS_MODEL_CODER": "qwen2.5-coder:latest",
            "JARVIS_MODEL_DEEP": "qwen3:14b",
            "JARVIS_MODEL_VISION": "gemma3:4b",
            "JARVIS_MODEL_EMBEDDING": "nomic-embed-text:latest",
            "JARVIS_MODEL_VERIFIER": "qwen3:8b",
        }.items():
            monkeypatch.setenv(k, v)
        model = resolve_inference_model(route(prompt))
        assert is_chat_safe(model), f"{prompt!r} → non-chat model {model}"


# ── Dedicated surfaces resolve capability-matched models ──────────────────────
class TestDedicatedSurfaces:
    def test_vision_surface_uses_vision_model(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_VISION", "gemma3:4b")
        assert resolve_vision_model() == "gemma3:4b"
        # and resolve_inference_model with explicit VISION surface agrees
        got = resolve_inference_model(route("hi"), surface=InferenceSurface.VISION)
        assert supports_surface(got, InferenceSurface.VISION)

    def test_embedding_surface_uses_embedding_model(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_EMBEDDING", "nomic-embed-text:latest")
        assert resolve_embedding_model() == "nomic-embed-text:latest"
        got = resolve_inference_model(route("hi"), surface=InferenceSurface.EMBEDDING)
        assert supports_surface(got, InferenceSurface.EMBEDDING)

    def test_misconfigured_vision_falls_back_to_vision_default(self, monkeypatch):
        # operator points VISION role at a text model → must not return a blind model
        monkeypatch.setenv("JARVIS_MODEL_VISION", "qwen3:8b")
        got = resolve_vision_model()
        assert supports_surface(got, InferenceSurface.VISION)
        assert got == "gemma3:4b"

    def test_misconfigured_embedding_falls_back_to_embedding_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_EMBEDDING", "qwen3:8b")
        got = resolve_embedding_model()
        assert supports_surface(got, InferenceSurface.EMBEDDING)


# ── Backward compatibility: well-configured host + legacy call shape ──────────
class TestBackwardCompat:
    def test_operator_chat_roles_honored(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_CODER", "qwen2.5-coder:latest")
        monkeypatch.setenv("JARVIS_MODEL_DEEP", "qwen3:14b")
        assert resolve_inference_model(route("refactor python tests")) == "qwen2.5-coder:latest"
        assert resolve_inference_model(route("forensic incident root cause analysis")) == "qwen3:14b"

    def test_legacy_positional_call_still_nonempty(self):
        assert resolve_inference_model(route("hi")) != ""
        assert resolve_inference_model(route("refactor python code")) != ""

    def test_coder_without_env_uses_deep_model(self):
        # preserved legacy behavior: CODER with no override → deep local model
        from core.model_router import MODEL_DEEP
        assert resolve_inference_model(route("write a python class")) == MODEL_DEEP


# ── Config read-through facade (M27 investigation outcome) ────────────────────
class TestConfigRoleModelFacade:
    def test_facade_reports_central_defaults_when_unset(self):
        from core.config import settings
        got = settings.resolved_role_models()
        assert got["fast"] == "qwen3:8b"
        assert got["deep"] == "qwen3:14b"
        assert got["vision"] == "gemma3:4b"
        assert got["embedding"] == "nomic-embed-text:latest"

    def test_facade_reflects_env_override_without_second_source(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_DEEP", "qwen3:32b")
        from core.config import settings
        assert settings.resolved_role_models()["deep"] == "qwen3:32b"
