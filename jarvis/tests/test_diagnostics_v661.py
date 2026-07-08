"""
tests/test_diagnostics_v661.py — V66.1 diagnostics alignment coverage.

Covers:
  * self_test operational taxonomy (OK/DORMANT/OPTIONAL/DEGRADED/FAILED);
  * self_test resolves the CONFIGURED vision role (not hardcoded moondream);
  * model_doctor reads the unified config + normalized host and its
    thinking-model-aware smoke test classifies outcomes correctly.

No Ollama required — HTTP is stubbed.
"""
from __future__ import annotations

import io
import json
import urllib.error



# ── self_test classification taxonomy ────────────────────────────────────────
class TestSelfTestClassification:
    def test_passing_subsystem_is_ok(self):
        from core.self_test import classify_result
        assert classify_result(True, "core", "5 models loaded") == "OK"

    def test_passing_but_warming_is_dormant(self):
        from core.self_test import classify_result
        assert classify_result(True, "detection", "binding in progress") == "DORMANT"

    def test_unconfigured_optional_is_optional_not_failed(self):
        from core.self_test import classify_result
        assert classify_result(False, "optional", "not configured (optional)") == "OPTIONAL"

    def test_offline_optional_is_dormant(self):
        from core.self_test import classify_result
        assert classify_result(False, "optional", "daemon offline (optional)") == "DORMANT"

    def test_broken_required_subsystem_is_failed(self):
        from core.self_test import classify_result
        assert classify_result(False, "detection", "correlator import error") == "FAILED"

    def test_admin_gated_detection_is_dormant_not_failed(self):
        from core.self_test import classify_result
        # ETW needs Administrator → 'monitor not ready' → DORMANT, never FAILED.
        assert classify_result(False, "detection", "monitor not ready") == "DORMANT"

    def test_vision_probes_configured_role(self, monkeypatch):
        # With VISION configured to gemma3:4b, the self-test must resolve gemma3,
        # never a hardcoded moondream.
        monkeypatch.setenv("JARVIS_MODEL_VISION", "gemma3:4b")
        from core.self_test import _configured_role_model
        assert _configured_role_model("vision", "fallback") == "gemma3:4b"


# ── model_doctor unified config + smoke classification ───────────────────────
class _FakeResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


class TestModelDoctor:
    def test_host_is_normalized(self):
        import scripts.model_doctor as md
        assert md._HOST.startswith("http://")
        assert md._HOST.endswith(":11434")

    def test_model_present_is_precise(self):
        import scripts.model_doctor as md
        installed = ["qwen3:8b", "qwen2.5-coder:latest"]
        assert md._model_present("qwen3:8b", installed) is True
        # legacy family-prefix false positive must be rejected
        assert md._model_present("qwen2.5:7b-instruct-q5_K_M", installed) is False

    def test_smoke_ok(self, monkeypatch):
        import scripts.model_doctor as md
        monkeypatch.setattr(md.urllib.request, "urlopen",
                            lambda *a, **k: _FakeResp({"response": "OK", "done": True}))
        status, detail = md._smoke_generate("qwen3:8b")
        assert status == "ok"
        assert "OK" in detail

    def test_smoke_empty_visible_thinking_model(self, monkeypatch):
        import scripts.model_doctor as md
        # HTTP 200 but only hidden reasoning → empty_visible (WARN, not failure).
        monkeypatch.setattr(
            md.urllib.request, "urlopen",
            lambda *a, **k: _FakeResp({"response": "", "thinking": "reasoning...", "done": True}),
        )
        status, _ = md._smoke_generate("qwen3:8b")
        assert status == "empty_visible"

    def test_smoke_missing_model(self, monkeypatch):
        import scripts.model_doctor as md

        def _raise(*a, **k):
            raise urllib.error.HTTPError(
                "http://x/api/generate", 404, "not found", {},
                io.BytesIO(b'{"error":"model not found"}'),
            )

        monkeypatch.setattr(md.urllib.request, "urlopen", _raise)
        status, _ = md._smoke_generate("nope:1b")
        assert status == "missing"

    def test_smoke_unreachable(self, monkeypatch):
        import scripts.model_doctor as md

        def _raise(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(md.urllib.request, "urlopen", _raise)
        status, _ = md._smoke_generate("qwen3:8b")
        assert status == "unreachable"
