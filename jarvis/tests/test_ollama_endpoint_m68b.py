"""V69 M68B (D) — ONE canonical Ollama endpoint resolution path.

THE DEFECT THESE PIN
--------------------
The Dockerfile ships ``ENV OLLAMA_HOST=http://host.docker.internal:11434``. The
health check, the availability probe and the diagnostics bundle read it through
``normalize_ollama_host``; the INFERENCE client hardcoded
``base_url="http://localhost:11434/v1"``. In the supported container deployment
those are two different servers, so "Ollama is reachable" and "inference works"
were answers about different machines — and a diagnostic that names the wrong
endpoint sends an operator to debug a host that was never involved.

The load-bearing test is ``test_health_and_inference_resolve_the_same_host``:
everything else here supports it.

Every test injects its environment. Nothing mutates ``os.environ``, so no value
can leak into the next test — which is itself one of the audit's requirements.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.ollama_endpoint import (  # noqa: E402
    DEFAULT_OLLAMA_BASE_URL,
    OLLAMA_HOST_ENV,
    InvalidOllamaEndpoint,
    endpoint_report,
    ollama_base_url,
    ollama_openai_base_url,
    require_endpoint,
    resolve_endpoint,
)

CORE = PACKAGE_ROOT / "core"


class TestResolution:
    def test_no_configuration_resolves_the_loopback_default(self):
        ep = resolve_endpoint(env={})
        assert ep.base_url == DEFAULT_OLLAMA_BASE_URL
        assert ep.source == "default"
        assert ep.configured is False
        assert ep.valid is True

    def test_an_explicit_environment_endpoint_is_honoured(self):
        ep = resolve_endpoint(env={OLLAMA_HOST_ENV: "http://10.1.2.3:9999"})
        assert ep.base_url == "http://10.1.2.3:9999"
        assert ep.source == "environment"
        assert ep.configured is True

    @pytest.mark.parametrize("raw,expected", [
        ("127.0.0.1", "http://127.0.0.1:11434"),
        ("localhost:11434", "http://localhost:11434"),
        ("http://ollama:11434", "http://ollama:11434"),
        ("http://host.docker.internal:11434", "http://host.docker.internal:11434"),
        ("https://gpu.internal:443", "https://gpu.internal:443"),
        ("http://ollama:11434/", "http://ollama:11434"),
    ])
    def test_bare_and_docker_style_forms_normalise(self, raw, expected):
        assert ollama_base_url(env={OLLAMA_HOST_ENV: raw}) == expected

    def test_a_docker_service_hostname_survives_intact(self):
        """The whole point: a compose service name must not become localhost."""
        ep = resolve_endpoint(env={OLLAMA_HOST_ENV: "http://ollama:11434"})
        assert "localhost" not in ep.base_url and "127.0.0.1" not in ep.base_url
        assert ep.openai_base_url == "http://ollama:11434/v1"

    def test_an_injected_endpoint_is_reported_as_explicit(self):
        ep = resolve_endpoint("http://test-fixture:1234",
                              env={OLLAMA_HOST_ENV: "http://ignored:11434"})
        assert ep.base_url == "http://test-fixture:1234"
        assert ep.source == "explicit"

    def test_an_ipv6_literal_stays_usable(self):
        ep = resolve_endpoint(env={OLLAMA_HOST_ENV: "http://[::1]:11434"})
        assert ep.api_url("/api/tags") == "http://[::1]:11434/api/tags"


class TestInvalidConfiguration:
    @pytest.mark.parametrize("raw", [
        "file:///etc/passwd",
        "ftp://ollama:11434",
        "http://ollama:11434/api/generate",
        "http://ollama:99999",
        "http://ollama:11434?x=1",
        "http:// ollama:11434",
        "://",
    ])
    def test_a_malformed_endpoint_is_reported_invalid(self, raw):
        ep = resolve_endpoint(env={OLLAMA_HOST_ENV: raw})
        assert ep.valid is False, f"{raw!r} was accepted"
        assert ep.reason, "an invalid endpoint carries no reason"
        # Tolerant path: JARVIS still boots, on the loopback default.
        assert ep.base_url == DEFAULT_OLLAMA_BASE_URL

    @pytest.mark.parametrize("raw", ["file:///etc/passwd", "http://ollama:99999"])
    def test_require_endpoint_fails_explicitly(self, raw):
        with pytest.raises(InvalidOllamaEndpoint) as exc:
            require_endpoint(env={OLLAMA_HOST_ENV: raw})
        assert OLLAMA_HOST_ENV in str(exc.value)

    def test_a_valid_endpoint_passes_the_strict_gate(self):
        ep = require_endpoint(env={OLLAMA_HOST_ENV: "http://ollama:11434"})
        assert ep.base_url == "http://ollama:11434"

    def test_an_invalid_value_never_silently_becomes_a_different_host(self):
        ep = resolve_endpoint(env={OLLAMA_HOST_ENV: "ftp://evil.example:80"})
        assert "evil.example" not in ep.base_url
        assert ep.raw == "ftp://evil.example:80"   # but it is still REPORTED


class TestOneResolutionPath:
    def test_health_and_inference_resolve_the_same_host(self):
        """THE property. Health, availability and inference, one endpoint."""
        env = {OLLAMA_HOST_ENV: "http://ollama-service:11434"}
        from core.model_router import normalize_ollama_host

        inference = ollama_openai_base_url(env=env)
        health = ollama_base_url(env=env)
        assert inference.startswith(health), (
            f"inference {inference} does not target the health host {health}")
        assert inference == "http://ollama-service:11434/v1"
        # The legacy accessor is now a facade over the same resolver.
        assert normalize_ollama_host("http://ollama-service:11434") == health

    def test_the_legacy_normaliser_delegates(self):
        """``normalize_ollama_host`` must not be a SECOND implementation."""
        source = (CORE / "model_router.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "normalize_ollama_host")
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "ollama_base_url" in called or "resolve_endpoint" in called, (
            "normalize_ollama_host re-implements resolution instead of delegating")
        assert "urlparse" not in called, "a second parser survives in model_router"

    def test_native_default_base_url_agrees(self):
        from core.ollama_native import default_base_url
        assert default_base_url() == ollama_base_url()


class TestDiagnosticsReportTheRealEndpoint:
    def test_the_report_names_the_endpoint_actually_used(self):
        report = endpoint_report(env={OLLAMA_HOST_ENV: "http://ollama:11434"})
        assert report["base_url"] == "http://ollama:11434"
        assert report["openai_base_url"] == "http://ollama:11434/v1"
        assert report["source"] == "environment"
        assert report["valid"] is True

    def test_the_posture_report_no_longer_hardcodes_a_host(self):
        """``ollama_env.posture_report`` returned ``"host": "127.0.0.1"`` as a
        literal — a false statement in every non-loopback deployment."""
        source = (CORE / "ollama_env.py").read_text(encoding="utf-8")
        assert '"host": "127.0.0.1"' not in source
        from core.ollama_env import OllamaEnvTruth
        report = OllamaEnvTruth().posture_report()
        assert "endpoint" in report
        assert report["endpoint"]["base_url"] == ollama_base_url()

    def test_the_posture_report_follows_a_CONFIGURED_endpoint(self, monkeypatch):
        """With nothing configured the resolver returns the loopback default — the
        same literal the old hardcoded value held — so comparing against it proved
        nothing. The falsification campaign replaced the resolver call with that
        literal and the test stayed green. Configure a DIFFERENT host, and only a
        real resolution can follow it."""
        from core.ollama_env import OllamaEnvTruth
        monkeypatch.setenv(OLLAMA_HOST_ENV, "http://ollama-posture:11434")
        report = OllamaEnvTruth().posture_report()
        assert report["endpoint"]["base_url"] == "http://ollama-posture:11434", (
            "the posture report does not follow the configured endpoint")
        assert report["endpoint"]["source"] == "environment"
        assert report["endpoint"]["configured"] is True

    def test_an_invalid_configuration_is_visible_in_the_report(self):
        report = endpoint_report(env={OLLAMA_HOST_ENV: "ftp://nope:1"})
        assert report["valid"] is False
        assert report["reason"]


class TestNoEnvironmentLeakage:
    def test_resolution_reads_the_injected_mapping_only(self, monkeypatch):
        monkeypatch.setenv(OLLAMA_HOST_ENV, "http://process-env:11434")
        assert ollama_base_url(env={}) == DEFAULT_OLLAMA_BASE_URL
        assert ollama_base_url() == "http://process-env:11434"

    def test_the_process_environment_is_untouched_by_resolution(self, monkeypatch):
        monkeypatch.delenv(OLLAMA_HOST_ENV, raising=False)
        resolve_endpoint(env={OLLAMA_HOST_ENV: "http://x:1"})
        import os
        assert OLLAMA_HOST_ENV not in os.environ


# ── Absent control: no inference path may hardcode an endpoint ──────────────
#: Modules that talk to Ollama. Each must reach it through the canonical resolver.
_OLLAMA_CLIENT_MODULES = (
    "llm.py", "sigma_generator.py", "cognitive_synthesis.py", "ai_reverser.py",
    "runtime_doctor.py", "residency.py", "ollama_native.py", "model_router.py",
    "ollama_env.py", "diagnostics_bundle.py", "embedding_runtime.py",
    "vision_engine.py", "fast_readiness.py", "health_watchdog.py", "self_test.py",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Every docstring Constant, by identity. A docstring EXPLAINING the removed
    literal is legitimate documentation; a literal in live code is the defect, and
    only an AST walk that distinguishes them can tell."""
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            ids.add(id(first.value))
    return ids


class TestNoHardcodedEndpointSurvives:
    @pytest.mark.parametrize("module", _OLLAMA_CLIENT_MODULES)
    def test_no_module_embeds_an_ollama_url_literal(self, module):
        """An AST walk over string CONSTANTS, so a URL inside a comment or a
        docstring explaining the old defect does not satisfy the test, and a URL
        in live code cannot hide behind one."""
        path = CORE / module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if "11434" not in node.value or id(node) in docstrings:
                continue
            offenders.append((node.lineno, node.value))
        allowed = {DEFAULT_OLLAMA_BASE_URL}
        real = [(ln, v) for ln, v in offenders if v not in allowed]
        assert real == [], (
            f"core/{module} embeds an Ollama endpoint literal {real}; resolve it "
            "through core.ollama_endpoint instead")

    def test_only_the_canonical_module_owns_the_default(self):
        """Exactly one CLIENT module may hold the loopback default as a literal.

        ``core.windows_hardener`` is the documented exception and a different
        concern: it WRITES ``OLLAMA_HOST`` to bind the Ollama SERVER to loopback,
        a hardening action. Resolving its literal through the client resolver
        would be circular — it would read the value it is about to set.
        """
        owners = []
        for path in sorted(CORE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node.value == DEFAULT_OLLAMA_BASE_URL
                        and id(node) not in docstrings):
                    owners.append(path.name)
                    break
        assert owners == ["ollama_endpoint.py", "windows_hardener.py"], owners

    def test_the_hardener_literal_is_a_server_bind_target(self):
        """Pins WHY windows_hardener is exempt, so the exemption cannot drift into
        a client bypass: its literal names the address Ollama LISTENS on."""
        source = (CORE / "windows_hardener.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigns = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Constant)
                   and n.value.value == DEFAULT_OLLAMA_BASE_URL]
        assert assigns, "the hardener no longer holds a loopback bind target"
        names = {t.id for a in assigns for t in a.targets if isinstance(t, ast.Name)}
        assert names == {"_LOOPBACK"}, names

    def test_the_alternate_environment_variable_is_gone(self):
        """``core.ai_reverser`` read ``JARVIS_OLLAMA_URL`` — a second source of
        truth is the defect, not a feature."""
        source = (CORE / "ai_reverser.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "JARVIS_OLLAMA_URL" not in literals
