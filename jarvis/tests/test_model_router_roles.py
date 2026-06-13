"""
tests/test_model_router_roles.py — V60.0 role-based router tests.

No Ollama required — route() is pure classification + env lookup. Verifies the
documented precedence, bilingual keyword routing, safe defaults, cloud-off-by-
default behavior, and backward compatibility with the legacy select_model API.
"""
from __future__ import annotations

import pytest

from core.model_router import (
    ModelDecision,
    ModelRole,
    calculate_complexity,
    cloud_enabled,
    model_for_role,
    route,
    select_model,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "JARVIS_MODEL_FAST", "JARVIS_MODEL_CODER", "JARVIS_MODEL_DEEP",
        "JARVIS_MODEL_VISION", "JARVIS_MODEL_EMBEDDING", "JARVIS_MODEL_VERIFIER",
        "JARVIS_CLOUD_ENABLED", "JARVIS_CLOUD_PROVIDER", "JARVIS_CLOUD_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


class TestRouting:
    def test_simple_prompt_routes_fast(self):
        d = route("hola, qué hora es?")
        assert d.role is ModelRole.FAST
        assert d.provider == "ollama"

    def test_coding_prompt_routes_coder(self):
        d = route("Refactor this Python function and add pytest unit tests")
        assert d.role is ModelRole.CODER

    def test_forensic_prompt_routes_deep(self):
        d = route("Analyze this forensic memory dump for the incident root cause")
        assert d.role is ModelRole.DEEP
        assert d.requires_verification is True

    def test_spanish_architecture_routes_deep(self):
        d = route("Diseña la arquitectura y el modelo de amenazas del sistema")
        assert d.role is ModelRole.DEEP

    def test_image_prompt_routes_vision(self):
        d = route("Take a screenshot and OCR the screen")
        assert d.role is ModelRole.VISION

    def test_spanish_vision(self):
        d = route("Captura la pantalla y analiza el diagrama de red")
        assert d.role is ModelRole.VISION

    def test_rag_prompt_routes_embedding(self):
        d = route("Index these PDFs into the vector knowledge base")
        assert d.role is ModelRole.EMBEDDING


class TestModelResolution:
    def test_default_models(self):
        assert model_for_role(ModelRole.FAST) == "qwen2.5-coder:7b"
        assert model_for_role(ModelRole.CODER) == "qwen2.5-coder:14b"
        assert model_for_role(ModelRole.DEEP) == "deepseek-r1:14b"
        assert model_for_role(ModelRole.VISION) == "moondream"
        assert model_for_role(ModelRole.EMBEDDING) == "nomic-embed-text"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_CODER", "qwen2.5-coder:32b")
        assert model_for_role(ModelRole.CODER) == "qwen2.5-coder:32b"

    def test_decision_is_dataclass(self):
        d = route("hello")
        assert isinstance(d, ModelDecision)
        assert 0.0 <= d.complexity <= 1.0


class TestCloud:
    def test_cloud_disabled_by_default(self):
        assert cloud_enabled() is False
        d = route("Design a complex distributed architecture", allow_cloud=True)
        assert d.provider == "ollama"
        assert d.role is not ModelRole.CLOUD

    def test_cloud_requires_explicit_enable_and_allow(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CLOUD_ENABLED", "true")
        d = route("Design a complex architecture", allow_cloud=True)
        assert d.provider == "cloud"
        assert d.role is ModelRole.CLOUD

    def test_cloud_enabled_but_not_allowed_stays_local(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CLOUD_ENABLED", "true")
        d = route("Design a complex architecture", allow_cloud=False)
        assert d.provider == "ollama"


class TestVerifierTrigger:
    def test_security_sensitive_requires_verification(self):
        d = route("write a quick note", security_sensitive=True)
        assert d.requires_verification is True

    def test_security_keywords_trigger_verification(self):
        d = route("explain this exploit payload and the CVE")
        assert d.requires_verification is True

    def test_force_role(self):
        d = route("anything", force_role=ModelRole.VERIFIER)
        assert d.role is ModelRole.VERIFIER


class TestBackwardCompat:
    def test_select_model_still_works(self):
        m = select_model("hello")
        assert isinstance(m, str) and m

    def test_calculate_complexity_bounded(self):
        assert 0.0 <= calculate_complexity("forensic incident analysis") <= 1.0
