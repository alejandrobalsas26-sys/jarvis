"""core/ollama_endpoint.py — V69 M68B (D): THE canonical Ollama endpoint.

WHY THIS EXISTS
===============
JARVIS derived the Ollama endpoint in at least six different ways:

    core/model_router.normalize_ollama_host()   $OLLAMA_HOST, normalised  (env-aware)
    core/ollama_native.default_base_url()       delegates to the above
    core/llm.LLM.__init__                       "http://localhost:11434/v1"  HARDCODED
    core/sigma_generator                        "http://localhost:11434/v1"  HARDCODED
    core/cognitive_synthesis                    "http://127.0.0.1:11434/..." HARDCODED
    core/ai_reverser                            $JARVIS_OLLAMA_URL           a DIFFERENT var
    core/runtime_doctor                         settings.ollama_host — a field that does
                                                not exist, so always the hardcoded default

The Dockerfile ships ``ENV OLLAMA_HOST=http://host.docker.internal:11434``. In that
supported deployment the health check, the availability probe and the diagnostics
bundle all resolved ``host.docker.internal`` — and the INFERENCE client resolved
``localhost``. So "Ollama is reachable" and "inference works" were answers about
two different servers, and a green health check could sit next to every generation
failing to connect. The reverse is worse: a diagnostic that names the wrong
endpoint sends an operator to debug a machine that was never involved.

THE RULE
--------
ONE resolution path. Health, availability, diagnostics and inference resolve the
same endpoint unless a caller passes an explicit override — and an override is
recorded as ``source="explicit"`` so a report never presents it as configuration.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not change authorization or network policy. It resolves a URL and says
where the URL came from. It never widens reachability: with no configuration at
all it resolves the loopback default, exactly as before, and a configured host is
the operator's deployment decision (the same host environment that already
governs ``core.containment``'s policy — never a tool argument, never model output).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

#: The loopback default. Used when nothing is configured — never a widening.
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

#: The ONE environment variable that configures the endpoint. ``JARVIS_OLLAMA_URL``
#: (read only by ``core.ai_reverser`` before M68B) is deliberately NOT a second
#: source of truth: two variables is the defect this module exists to remove.
OLLAMA_HOST_ENV = "OLLAMA_HOST"

DEFAULT_OLLAMA_PORT = 11434

#: Schemes an Ollama base URL may use. Anything else (file:, unix:, ftp:, a bare
#: "javascript:") is a configuration error, not something to normalise into HTTP.
ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


class InvalidOllamaEndpoint(ValueError):
    """The configured Ollama endpoint cannot be used as given.

    Raised only by :func:`require_endpoint`. The tolerant resolvers never raise:
    they fall back to the loopback default and carry the reason in the returned
    :class:`OllamaEndpoint`, so a malformed ``OLLAMA_HOST`` degrades a diagnostic
    rather than preventing JARVIS from booting.
    """


@dataclass(frozen=True)
class OllamaEndpoint:
    """A resolved endpoint plus the provenance a diagnostic must report.

    ``base_url``  the endpoint that will ACTUALLY be used
    ``source``    "explicit" (caller argument) | "environment" | "default"
    ``raw``       what was read, before normalisation (None when nothing was set)
    ``valid``     False when ``raw`` was unusable and the default was substituted
    ``reason``    why it was invalid — present exactly when ``valid`` is False
    """

    base_url: str
    source: str
    raw: str | None = None
    valid: bool = True
    reason: str | None = None

    @property
    def openai_base_url(self) -> str:
        """The OpenAI-compatible base URL (``…/v1``) the inference client needs."""
        return self.base_url.rstrip("/") + "/v1"

    @property
    def configured(self) -> bool:
        """True when an operator actually configured this, rather than inheriting
        the loopback default."""
        return self.source != "default"

    def api_url(self, path: str) -> str:
        """Join a native Ollama API path (``/api/tags``) onto the base URL."""
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "openai_base_url": self.openai_base_url,
            "source": self.source,
            "configured": self.configured,
            "valid": self.valid,
            "reason": self.reason,
        }


def _normalize(value: str) -> tuple[str | None, str | None]:
    """Normalise one candidate to ``scheme://host:port``.

    Returns ``(url, None)`` on success or ``(None, reason)`` on failure. Tolerates
    the bare forms operators (and ``core.windows_hardener``) set — ``127.0.0.1``,
    ``localhost:11434`` — which would otherwise produce ``127.0.0.1/api/tags``.
    A trailing ``/`` is tolerated; any deeper path, query or fragment is not, since
    an Ollama BASE url has none and silently dropping one hides a typo.
    """
    candidate = value.strip()
    if not candidate:
        return None, "empty"
    if any(ch.isspace() for ch in candidate) or any(ord(ch) < 0x20 for ch in candidate):
        return None, "contains whitespace or control characters"
    if "://" not in candidate:
        candidate = "http://" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        return None, f"unparseable: {exc}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return None, f"scheme {scheme!r} is not one of {ALLOWED_SCHEMES}"
    try:
        host = parsed.hostname
    except ValueError as exc:
        return None, f"invalid host: {exc}"
    if not host:
        return None, "no host"
    try:
        port = parsed.port
    except ValueError as exc:
        return None, f"invalid port: {exc}"
    if port is None:
        port = DEFAULT_OLLAMA_PORT
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None, "base URL must not carry a path, query or fragment"
    # IPv6 literals must keep their brackets or the joined URL is unusable.
    rendered = f"[{host}]" if ":" in host else host
    return f"{scheme}://{rendered}:{port}", None


def resolve_endpoint(
    raw: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> OllamaEndpoint:
    """THE resolution path. Everything else in JARVIS goes through this.

    ``raw``  an explicit override (a caller's ``--host``, a test's fixture).
    ``env``  the environment mapping to read; injected so a test never mutates
             ``os.environ`` and no value leaks into the next test.
    """
    if env is None:
        import os
        env = os.environ

    if raw is not None:
        source, candidate = "explicit", raw
    else:
        candidate = env.get(OLLAMA_HOST_ENV) or ""
        source = "environment" if candidate.strip() else "default"

    if source == "default":
        return OllamaEndpoint(base_url=DEFAULT_OLLAMA_BASE_URL, source="default",
                              raw=None, valid=True, reason=None)

    url, reason = _normalize(candidate)
    if url is None:
        # Tolerant: a malformed endpoint must not stop JARVIS from starting. The
        # substitution is RECORDED, so `require_endpoint` and every diagnostic can
        # state it explicitly instead of silently pretending the default was chosen.
        return OllamaEndpoint(base_url=DEFAULT_OLLAMA_BASE_URL, source=source,
                              raw=candidate, valid=False, reason=reason)
    return OllamaEndpoint(base_url=url, source=source, raw=candidate,
                          valid=True, reason=None)


def require_endpoint(
    raw: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> OllamaEndpoint:
    """Resolve, or fail explicitly. For entry points that must not paper over a
    misconfigured deployment (CLI validation, a deployment preflight)."""
    endpoint = resolve_endpoint(raw, env=env)
    if not endpoint.valid:
        raise InvalidOllamaEndpoint(
            f"{OLLAMA_HOST_ENV}={endpoint.raw!r} is not a usable Ollama endpoint: "
            f"{endpoint.reason}")
    return endpoint


def ollama_base_url(
    raw: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """The native Ollama base URL (``scheme://host:port``)."""
    return resolve_endpoint(raw, env=env).base_url


def ollama_openai_base_url(
    raw: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """The OpenAI-compatible base URL (``scheme://host:port/v1``).

    THE accessor for every inference client. ``core.llm``, ``core.sigma_generator``
    and anything else constructing an ``AsyncOpenAI`` against Ollama calls this —
    a literal ``http://localhost:11434/v1`` in an inference path is the defect.
    """
    return resolve_endpoint(raw, env=env).openai_base_url


def endpoint_report(
    raw: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict:
    """What a diagnostic prints: the endpoint ACTUALLY in use and where it came
    from. Never a recommendation, never a hardcoded literal."""
    return resolve_endpoint(raw, env=env).to_dict()
