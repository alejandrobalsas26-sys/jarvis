"""core/embedding_runtime.py — V69 M52: the ONE role-safe embedding runtime.

JARVIS historically had an embedding split-brain: the configured EMBEDDING role
(``nomic-embed-text`` via Ollama) was never used for vectors, while the Knowledge
Vault and ``VectorMemory`` each imported ``sentence-transformers`` /
``all-MiniLM-L6-v2`` / ``torch`` directly. That duplication caused the live
``torch infer_schema`` fault V68.1 isolated. This module removes the split-brain
by giving every semantic consumer a single boundary that resolves the configured
EMBEDDING role, calls the right provider, normalizes the result, and returns a
plain-Python :class:`EmbeddingResult` — never a ``torch.Tensor``, numpy array,
``SentenceTransformer`` object, or HTTP/Chroma internal.

Provider policy (no silent switching):
  * PRIMARY   — configured EMBEDDING role model through Ollama (nomic-embed-text).
  * FALLBACK  — sentence-transformers / all-MiniLM-L6-v2, used ONLY when the
    operator explicitly enables it (``JARVIS_EMBEDDING_FALLBACK`` /
    ``settings.embedding_fallback_enabled``) AND the dependency imports cleanly.
    The active provider is always reported back to the caller — a fallback vector
    is never mistaken for a primary one (they carry different fingerprints).

The runtime is dependency-injectable: providers are passed in for tests, so the
whole surface is exercisable WITHOUT a live Ollama or torch install.

Hardware discipline (Ryzen 5 7430U, CPU-only, OLLAMA_NUM_PARALLEL=1): bounded
batch sizes, an explicit per-call timeout, cooperative cancellation between
batch chunks, and no unbounded cache (the LRU lives in the consumer, keyed by
the active fingerprint).
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from loguru import logger

# Bump when the vector *encoding contract* changes (normalization scheme,
# provider request shape) in a way that must invalidate existing collections.
EMBEDDING_SCHEMA_VERSION = 1

# Hardware-safe bounds (overridable via settings). A single embed_batch never
# ships more than _MAX_BATCH texts to the backend at once.
_DEFAULT_BATCH_SIZE = 16
_HARD_MAX_BATCH = 128
_DEFAULT_TIMEOUT_S = 30.0

# Provider identifiers (stable — they feed the fingerprint pre-image).
PROVIDER_OLLAMA = "ollama"
PROVIDER_SENTENCE_TRANSFORMERS = "sentence-transformers"


class EmbeddingError(RuntimeError):
    """Internal, classified embedding fault. Carries a *safe* message that never
    exposes dependency internals (torch.Tensor / infer_schema / HTTP / Chroma).
    """

    def __init__(self, error_class: str, safe_message: str) -> None:
        self.error_class = error_class
        self.safe_message = safe_message
        super().__init__(safe_message)


def _classify_provider_error(exc: Exception) -> tuple[str, str]:
    """Map a raw provider/dependency exception to (error_class, safe_message).

    Mirrors core.knowledge._classify_backend_error so the same honest, internal
    -free vocabulary is used everywhere. Raw ``torch`` / HTTP text NEVER escapes.
    """
    low = str(exc).lower()
    if "infer_schema" in low or ("torch" in low and "unsupported type" in low):
        return (
            "dependency_incompatibility",
            "Embedding provider offline: dependency version mismatch "
            "(torch/transformers).",
        )
    if "timeout" in low or "timed out" in low:
        return ("timeout", "Embedding provider timed out.")
    if "connect" in low or "connection" in low or "refused" in low:
        return ("provider_unreachable", "Embedding provider is unreachable.")
    if isinstance(exc, ModuleNotFoundError) or "no module named" in low:
        return ("dependency_missing", "Embedding dependency is not installed.")
    if isinstance(exc, ImportError):
        return ("dependency_import_failed", "Embedding dependency failed to import.")
    return ("provider_error", "Embedding provider failed to produce a vector.")


# ── Public boundary types (plain Python only) ────────────────────────────────
@dataclass(frozen=True)
class EmbeddingResult:
    """One embedding at the plain-Python boundary. No backend object crosses it."""

    status: str                         # ok | empty_input | unavailable | error | timeout | cancelled
    vector: list[float] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    dimension: int = 0
    fingerprint: str = ""
    latency_ms: float = 0.0
    normalized: bool = True
    error_class: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class BatchEmbeddingResult:
    """A batch of embeddings sharing one model/provider/dimension/fingerprint."""

    status: str
    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    dimension: int = 0
    fingerprint: str = ""
    latency_ms: float = 0.0
    normalized: bool = True
    count: int = 0
    error_class: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class EmbeddingHealth:
    """Read-only, internal-free health of the active embedding runtime."""

    available: bool
    provider: str
    model: str
    dimension: int
    fingerprint: str
    schema_version: int
    fallback_active: bool
    message: str
    error_class: str | None = None


# ── Fingerprint ──────────────────────────────────────────────────────────────
def compute_fingerprint(
    provider: str, model: str, *, normalized: bool = True,
    schema_version: int = EMBEDDING_SCHEMA_VERSION,
) -> str:
    """Deterministic, stable identity for a (provider, model, encoding) triple.

    Two runtimes agree iff they would produce comparable vectors. Dimension is a
    function of the model, tracked separately and cross-checked; it is not folded
    into the pre-image so a fingerprint can be computed before the first embed.
    """
    pre = f"{provider}|{model}|norm={int(bool(normalized))}|v{schema_version}"
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()[:16]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _coerce_vector(raw) -> list[float]:
    """Coerce a provider row to a pure ``list[float]``; reject anything tensor-ish.

    Guarantees no ``torch.Tensor`` / numpy array object crosses the boundary:
    every element is turned into a native python ``float`` (numpy/torch scalars
    coerce via ``__float__``), while strings, bools, and ``None`` are rejected as
    malformed. Raises :class:`EmbeddingError` on a malformed / empty row.
    """
    try:
        iterator = iter(raw)
    except TypeError as e:
        raise EmbeddingError("malformed_vector", "Provider returned a non-vector row.") from e
    out: list[float] = []
    for x in iterator:
        if isinstance(x, (bool, str, bytes)) or x is None:
            raise EmbeddingError("malformed_vector", "Provider returned a non-numeric vector.")
        try:
            out.append(float(x))
        except (TypeError, ValueError) as e:
            raise EmbeddingError("malformed_vector", "Provider returned a non-numeric vector.") from e
    if not out:
        raise EmbeddingError("malformed_vector", "Provider returned an empty vector.")
    return out


# ── Provider protocol + concrete providers ───────────────────────────────────
@runtime_checkable
class EmbeddingProvider(Protocol):
    provider_name: str
    model: str

    def is_available(self) -> bool: ...

    def embed(
        self, texts: list[str], *, timeout: float,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[list[float]]:
        """Return one raw (un-normalized) list[float] per input text. Same length
        as ``texts`` and same order. Raises :class:`EmbeddingError` on failure."""
        ...


class OllamaEmbeddingProvider:
    """PRIMARY provider — configured EMBEDDING role via the Ollama REST API.

    Uses the ``/api/embed`` batch endpoint. Embedding-only models (nomic-embed
    -text) are correct here and, by construction, can never leak into the chat
    stream: chat resolution goes through core.model_router.resolve_inference_model
    which rejects non-chat models. This provider is the only place the embedding
    role is turned into vectors.
    """

    provider_name = PROVIDER_OLLAMA

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        from core.model_router import normalize_ollama_host, resolve_embedding_model

        self.model = model or resolve_embedding_model()
        self._host = normalize_ollama_host(host)

    def is_available(self) -> bool:
        try:
            import httpx

            with httpx.Client(timeout=3.0) as client:
                r = client.get(f"{self._host}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    def embed(
        self, texts: list[str], *, timeout: float,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[list[float]]:
        import httpx

        if should_cancel and should_cancel():
            raise EmbeddingError("cancelled", "Embedding was cancelled.")
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(
                    f"{self._host}/api/embed",
                    json={"model": self.model, "input": texts},
                )
        except httpx.TimeoutException as e:
            raise EmbeddingError("timeout", "Embedding provider timed out.") from e
        except Exception as e:  # noqa: BLE001 — classify, never propagate raw
            raise EmbeddingError(*_classify_provider_error(e)) from e

        if r.status_code != 200:
            raise EmbeddingError(
                "provider_error",
                f"Embedding provider returned HTTP {r.status_code}.",
            )
        try:
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError("malformed_response", "Embedding response was not valid JSON.") from e

        rows = payload.get("embeddings")
        if rows is None and "embedding" in payload:   # legacy single-vector shape
            rows = [payload["embedding"]]
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingError(
                "malformed_response",
                "Embedding response shape did not match the request.",
            )
        # Rows are returned raw; the runtime coerces every row to list[float] at
        # the single boundary-purity choke point.
        return rows


class SentenceTransformerProvider:
    """OPTIONAL fallback — sentence-transformers / all-MiniLM-L6-v2 (CPU-only).

    Only constructed when the operator explicitly enables fallback. The model is
    loaded lazily on first embed so merely *configuring* it never imports torch.
    Carries its own fingerprint (different provider + model) so its 384-dim
    vectors are never mixed with the primary provider's in one collection.
    """

    provider_name = PROVIDER_SENTENCE_TRANSFORMERS

    def __init__(self, model: str = "all-MiniLM-L6-v2") -> None:
        self.model = model
        self._st = None

    def _ensure_model(self):
        if self._st is None:
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as e:  # noqa: BLE001
                raise EmbeddingError(*_classify_provider_error(e)) from e
            self._st = SentenceTransformer(self.model)
        return self._st

    def is_available(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("sentence_transformers") is not None
        except Exception:
            return False

    def embed(
        self, texts: list[str], *, timeout: float,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[list[float]]:
        if should_cancel and should_cancel():
            raise EmbeddingError("cancelled", "Embedding was cancelled.")
        try:
            model = self._ensure_model()
            # .tolist() guarantees a plain list crosses the boundary — never a
            # torch.Tensor / numpy array.
            raw = model.encode(list(texts)).tolist()
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(*_classify_provider_error(e)) from e
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingError("malformed_response", "Fallback embedder returned an unexpected shape.")
        return raw


# ── The runtime ──────────────────────────────────────────────────────────────
class EmbeddingRuntime:
    """The single embedding boundary every semantic consumer resolves through."""

    def __init__(
        self,
        primary: EmbeddingProvider | None = None,
        fallback: EmbeddingProvider | None = None,
        *,
        normalize: bool = True,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        schema_version: int = EMBEDDING_SCHEMA_VERSION,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._normalize = normalize
        self._batch_size = max(1, min(int(batch_size), _HARD_MAX_BATCH))
        self._timeout_s = float(timeout_s)
        self._schema_version = int(schema_version)
        # Discovered on first successful embed; part of the compatibility contract.
        self._dimension: int = 0
        self._fallback_active = False

    # ── provider selection (explicit, never silent) ──────────────────────────
    def _active_provider(self) -> EmbeddingProvider:
        if self._primary is not None and self._primary.is_available():
            self._fallback_active = False
            return self._primary
        if self._fallback is not None and self._fallback.is_available():
            if not self._fallback_active:
                logger.warning(
                    "EMBEDDING_RUNTIME: primary unavailable — using explicitly "
                    "configured fallback provider (different fingerprint/dimension)."
                )
            self._fallback_active = True
            return self._fallback
        # Nothing available.
        if self._primary is None:
            raise EmbeddingError("not_configured", "No embedding provider is configured.")
        raise EmbeddingError("provider_unreachable", "The embedding provider is unavailable.")

    def _provider_identity(self, provider: EmbeddingProvider) -> tuple[str, str, str]:
        prov = provider.provider_name
        model = provider.model
        fp = compute_fingerprint(
            prov, model, normalized=self._normalize, schema_version=self._schema_version,
        )
        return prov, model, fp

    # ── single ───────────────────────────────────────────────────────────────
    def embed_text(
        self, text: str, *, should_cancel: Callable[[], bool] | None = None,
    ) -> EmbeddingResult:
        if not isinstance(text, str) or not text.strip():
            return EmbeddingResult(
                status="empty_input", error_class="empty_input",
                message="Text to embed must be a non-empty string.",
                normalized=self._normalize,
            )
        batch = self.embed_batch([text], should_cancel=should_cancel)
        if not batch.ok:
            return EmbeddingResult(
                status=batch.status, model=batch.model, provider=batch.provider,
                dimension=batch.dimension, fingerprint=batch.fingerprint,
                latency_ms=batch.latency_ms, normalized=self._normalize,
                error_class=batch.error_class, message=batch.message,
            )
        return EmbeddingResult(
            status="ok", vector=batch.vectors[0], model=batch.model,
            provider=batch.provider, dimension=batch.dimension,
            fingerprint=batch.fingerprint, latency_ms=batch.latency_ms,
            normalized=self._normalize,
        )

    # ── batch ──────────────────────────────────────────────────────────────
    def embed_batch(
        self, texts: list[str], *, should_cancel: Callable[[], bool] | None = None,
    ) -> BatchEmbeddingResult:
        if not isinstance(texts, (list, tuple)) or len(texts) == 0:
            return BatchEmbeddingResult(
                status="empty_input", error_class="empty_input",
                message="No texts to embed.", normalized=self._normalize,
            )
        if any((not isinstance(t, str) or not t.strip()) for t in texts):
            return BatchEmbeddingResult(
                status="empty_input", error_class="empty_input",
                message="Every text to embed must be a non-empty string.",
                normalized=self._normalize,
            )

        start = time.perf_counter()
        try:
            provider = self._active_provider()
        except EmbeddingError as e:
            return BatchEmbeddingResult(
                status="unavailable", error_class=e.error_class, message=e.safe_message,
                normalized=self._normalize,
            )
        prov, model, fp = self._provider_identity(provider)

        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), self._batch_size):
                if should_cancel and should_cancel():
                    return BatchEmbeddingResult(
                        status="cancelled", error_class="cancelled",
                        message="Embedding was cancelled.", provider=prov, model=model,
                        fingerprint=fp, normalized=self._normalize,
                    )
                chunk = list(texts[i : i + self._batch_size])
                raw_rows = provider.embed(
                    chunk, timeout=self._timeout_s, should_cancel=should_cancel,
                )
                # Boundary purity is enforced HERE, for every provider (including
                # injected ones): coerce each row to a plain list[float] so no
                # torch.Tensor / numpy array / non-numeric object can cross.
                for row in raw_rows:
                    pure = _coerce_vector(row)
                    vectors.append(_l2_normalize(pure) if self._normalize else pure)
        except EmbeddingError as e:
            status = "timeout" if e.error_class == "timeout" else (
                "cancelled" if e.error_class == "cancelled" else "error"
            )
            return BatchEmbeddingResult(
                status=status, error_class=e.error_class, message=e.safe_message,
                provider=prov, model=model, fingerprint=fp, normalized=self._normalize,
            )
        except Exception as e:  # noqa: BLE001 — never leak a raw stack trace
            ec, msg = _classify_provider_error(e)
            return BatchEmbeddingResult(
                status="error", error_class=ec, message=msg,
                provider=prov, model=model, fingerprint=fp, normalized=self._normalize,
            )

        dim = len(vectors[0]) if vectors else 0
        # Dimension stability: every row must share one dimension (no mixing).
        if any(len(v) != dim for v in vectors):
            return BatchEmbeddingResult(
                status="error", error_class="dimension_mismatch",
                message="Provider returned vectors of inconsistent dimension.",
                provider=prov, model=model, fingerprint=fp, normalized=self._normalize,
            )
        self._dimension = dim
        latency = (time.perf_counter() - start) * 1000.0
        return BatchEmbeddingResult(
            status="ok", vectors=vectors, model=model, provider=prov,
            dimension=dim, fingerprint=fp, latency_ms=latency,
            normalized=self._normalize, count=len(vectors),
        )

    # ── health ───────────────────────────────────────────────────────────────
    def health(self) -> EmbeddingHealth:
        """Probe the active provider with a tiny input. Internal-free."""
        try:
            provider = self._active_provider()
        except EmbeddingError as e:
            return EmbeddingHealth(
                available=False, provider=(self._primary.provider_name if self._primary else ""),
                model=(self._primary.model if self._primary else ""), dimension=0,
                fingerprint="", schema_version=self._schema_version,
                fallback_active=False, message=e.safe_message, error_class=e.error_class,
            )
        prov, model, fp = self._provider_identity(provider)
        probe = self.embed_text("health")
        if not probe.ok:
            return EmbeddingHealth(
                available=False, provider=prov, model=model, dimension=0,
                fingerprint=fp, schema_version=self._schema_version,
                fallback_active=self._fallback_active,
                message=probe.message or "Embedding probe failed.",
                error_class=probe.error_class,
            )
        return EmbeddingHealth(
            available=True, provider=prov, model=model, dimension=probe.dimension,
            fingerprint=fp, schema_version=self._schema_version,
            fallback_active=self._fallback_active, message="ready",
        )

    # ── introspection (no probe) ─────────────────────────────────────────────
    def active_fingerprint(self) -> str:
        """Fingerprint of the provider that WOULD serve now, without embedding."""
        try:
            _, _, fp = self._provider_identity(self._active_provider())
            return fp
        except EmbeddingError:
            return ""


# ── Production singleton ──────────────────────────────────────────────────────
_runtime_singleton: EmbeddingRuntime | None = None


def build_default_runtime() -> EmbeddingRuntime:
    """Construct the production runtime from settings + resolved role model.

    Primary is always Ollama (configured EMBEDDING role). The sentence
    -transformers fallback is wired ONLY when the operator explicitly enabled it,
    so there is no silent provider switch and no torch import by default.
    """
    from core.config import settings

    fallback: EmbeddingProvider | None = None
    if getattr(settings, "embedding_fallback_enabled", False):
        fallback = SentenceTransformerProvider()
        logger.info("EMBEDDING_RUNTIME: sentence-transformers fallback explicitly enabled.")
    return EmbeddingRuntime(
        primary=OllamaEmbeddingProvider(),
        fallback=fallback,
        batch_size=int(getattr(settings, "embedding_batch_size", _DEFAULT_BATCH_SIZE)),
        timeout_s=float(getattr(settings, "embedding_timeout_s", _DEFAULT_TIMEOUT_S)),
    )


def get_runtime() -> EmbeddingRuntime:
    """Lazy singleton accessor for the production embedding runtime."""
    global _runtime_singleton
    if _runtime_singleton is None:
        _runtime_singleton = build_default_runtime()
    return _runtime_singleton


def reset_runtime() -> None:
    """Drop the cached runtime (tests / config reload)."""
    global _runtime_singleton
    _runtime_singleton = None
