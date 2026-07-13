"""tests/test_embedding_runtime_v69.py — V69 M52: unified embedding runtime.

Deterministic, hermetic coverage of the ONE role-safe embedding runtime. NO live
Ollama, torch, or sentence-transformers is required — providers are injected
fakes. The invariants under test:

  * successful single + batch embedding via the (fake) Ollama primary
  * configured EMBEDDING role resolution → embedding-only, never chat-safe
  * stable dimension + stable fingerprint
  * malformed provider response / timeout / cancellation degrade honestly
  * empty input rejected
  * bounded batch size (large batch is chunked, not shipped whole)
  * NO tensor/ndarray object ever crosses the plain-Python boundary
  * NO vector-internal text ever appears in a boundary error message
  * primary unavailable → unavailable, unless an EXPLICIT fallback is wired
"""
from __future__ import annotations

import math


from core.embedding_runtime import (
    BatchEmbeddingResult,
    EmbeddingResult,
    EmbeddingRuntime,
    PROVIDER_OLLAMA,
    PROVIDER_SENTENCE_TRANSFORMERS,
    compute_fingerprint,
)

_FORBIDDEN_INTERNALS = (
    "torch", "tensor", "infer_schema", "sentence_transformers", "sentencetransformer",
    "httpx", "chromadb", "traceback", "ndarray",
)


def _assert_no_internals(blob: str) -> None:
    low = blob.lower()
    for token in _FORBIDDEN_INTERNALS:
        assert token not in low, f"internal leaked to boundary: {token!r}"


# ── Fake providers (no external deps) ─────────────────────────────────────────
class FakeProvider:
    def __init__(self, provider_name=PROVIDER_OLLAMA, model="nomic-embed-text:latest",
                 dim=8, available=True):
        self.provider_name = provider_name
        self.model = model
        self._dim = dim
        self._available = available
        self.calls = 0
        self.max_chunk = 0

    def is_available(self) -> bool:
        return self._available

    def embed(self, texts, *, timeout, should_cancel=None):
        self.calls += 1
        self.max_chunk = max(self.max_chunk, len(texts))
        # Deterministic pseudo-vectors derived from text length (no randomness).
        return [[float((len(t) + i) % 7) + 1.0 for i in range(self._dim)] for t in texts]


class BoomProvider(FakeProvider):
    def __init__(self, error, **kw):
        super().__init__(**kw)
        self._error = error

    def embed(self, texts, *, timeout, should_cancel=None):
        from core.embedding_runtime import EmbeddingError

        raise EmbeddingError(*self._error)


# ── Happy path ────────────────────────────────────────────────────────────────
def test_single_embedding_ok_and_normalized():
    rt = EmbeddingRuntime(primary=FakeProvider())
    res = rt.embed_text("hello world")
    assert isinstance(res, EmbeddingResult)
    assert res.ok and res.status == "ok"
    assert res.provider == PROVIDER_OLLAMA
    assert res.dimension == 8 and len(res.vector) == 8
    # L2-normalized → unit length
    assert math.isclose(math.sqrt(sum(x * x for x in res.vector)), 1.0, rel_tol=1e-9)
    assert res.fingerprint == compute_fingerprint(PROVIDER_OLLAMA, "nomic-embed-text:latest")


def test_batch_embedding_ok():
    rt = EmbeddingRuntime(primary=FakeProvider())
    res = rt.embed_batch(["a", "bb", "ccc"])
    assert isinstance(res, BatchEmbeddingResult)
    assert res.ok and res.count == 3
    assert all(len(v) == res.dimension for v in res.vectors)


def test_dimension_and_fingerprint_are_stable():
    rt = EmbeddingRuntime(primary=FakeProvider())
    a = rt.embed_text("query one")
    b = rt.embed_text("a completely different query with more words")
    assert a.dimension == b.dimension
    assert a.fingerprint == b.fingerprint  # fingerprint is input-independent


def test_fingerprint_changes_with_provider_or_model():
    assert compute_fingerprint("ollama", "nomic-embed-text:latest") != \
        compute_fingerprint("sentence-transformers", "all-MiniLM-L6-v2")
    assert compute_fingerprint("ollama", "nomic-embed-text:latest") != \
        compute_fingerprint("ollama", "mxbai-embed-large:latest")


# ── Boundary purity: no tensors, no leaks ─────────────────────────────────────
def test_no_tensor_object_crosses_boundary():
    class _NumpyLikeScalar:
        """Mimics a numpy/torch float scalar: coerces via __float__, not a float."""
        def __init__(self, v):
            self._v = v
        def __float__(self):
            return float(self._v)

    class TensorProvider(FakeProvider):
        def embed(self, texts, *, timeout, should_cancel=None):
            return [[_NumpyLikeScalar(1), _NumpyLikeScalar(2)] for _ in texts]

    rt = EmbeddingRuntime(primary=TensorProvider(), normalize=False)
    res = rt.embed_text("x")
    # The runtime coerces every element to a PLAIN python float — the tensor/
    # numpy-like objects never survive to the boundary.
    assert res.ok
    assert type(res.vector) is list
    for x in res.vector:
        assert type(x) is float
    _assert_no_internals(str(res))


def test_vector_of_non_numbers_is_rejected():
    class StrProvider(FakeProvider):
        def embed(self, texts, *, timeout, should_cancel=None):
            return [["not", "a", "number"] for _ in texts]

    res = EmbeddingRuntime(primary=StrProvider()).embed_text("x")
    assert not res.ok
    assert res.error_class == "malformed_vector"


# ── Failure modes degrade honestly ────────────────────────────────────────────
def test_malformed_response_is_structured():
    res = EmbeddingRuntime(
        primary=BoomProvider(("malformed_response", "Embedding response shape mismatch."))
    ).embed_text("x")
    assert res.status == "error"
    assert res.error_class == "malformed_response"
    _assert_no_internals(str(res))


def test_timeout_is_structured():
    res = EmbeddingRuntime(
        primary=BoomProvider(("timeout", "Embedding provider timed out."))
    ).embed_text("x")
    assert res.status == "timeout"
    _assert_no_internals(str(res))


def test_cancellation_between_chunks():
    prov = FakeProvider(dim=4)
    rt = EmbeddingRuntime(primary=prov, batch_size=2)
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 1   # allow first chunk, cancel before second

    res = rt.embed_batch(["a", "b", "c", "d"], should_cancel=cancel)
    assert res.status == "cancelled"
    _assert_no_internals(str(res))


def test_empty_input_rejected():
    rt = EmbeddingRuntime(primary=FakeProvider())
    assert rt.embed_text("").error_class == "empty_input"
    assert rt.embed_text("   ").error_class == "empty_input"
    assert rt.embed_batch([]).error_class == "empty_input"
    assert rt.embed_batch(["ok", " "]).error_class == "empty_input"


# ── Bounded batch size ────────────────────────────────────────────────────────
def test_batch_is_chunked_to_bounded_size():
    prov = FakeProvider(dim=4)
    rt = EmbeddingRuntime(primary=prov, batch_size=3)
    res = rt.embed_batch([f"t{i}" for i in range(10)])
    assert res.ok and res.count == 10
    assert prov.max_chunk <= 3          # never shipped a chunk larger than the bound
    assert prov.calls == 4              # ceil(10/3)


def test_hard_cap_on_batch_size():
    rt = EmbeddingRuntime(primary=FakeProvider(), batch_size=10_000)
    assert rt._batch_size <= 128


# ── Provider policy: no silent switching ──────────────────────────────────────
def test_primary_unavailable_no_fallback_is_unavailable():
    rt = EmbeddingRuntime(primary=FakeProvider(available=False))
    res = rt.embed_text("x")
    assert res.status == "unavailable"
    assert res.error_class == "provider_unreachable"


def test_explicit_fallback_used_when_primary_down():
    primary = FakeProvider(available=False)
    fallback = FakeProvider(
        provider_name=PROVIDER_SENTENCE_TRANSFORMERS, model="all-MiniLM-L6-v2",
        dim=6, available=True,
    )
    rt = EmbeddingRuntime(primary=primary, fallback=fallback)
    res = rt.embed_text("x")
    assert res.ok
    assert res.provider == PROVIDER_SENTENCE_TRANSFORMERS
    # Fallback carries its OWN fingerprint — never mistaken for the primary's.
    assert res.fingerprint == compute_fingerprint(
        PROVIDER_SENTENCE_TRANSFORMERS, "all-MiniLM-L6-v2"
    )
    assert res.fingerprint != compute_fingerprint(PROVIDER_OLLAMA, "nomic-embed-text:latest")


def test_fallback_disabled_by_default_when_primary_down():
    rt = EmbeddingRuntime(primary=FakeProvider(available=False), fallback=None)
    assert rt.embed_text("x").status == "unavailable"


def test_health_reports_active_provider():
    rt = EmbeddingRuntime(primary=FakeProvider())
    h = rt.health()
    assert h.available and h.provider == PROVIDER_OLLAMA
    assert h.dimension == 8 and h.fingerprint
    assert not h.fallback_active


def test_health_down_when_nothing_available():
    rt = EmbeddingRuntime(primary=FakeProvider(available=False))
    h = rt.health()
    assert not h.available
    _assert_no_internals(h.message)


# ── Role safety: the embedding model must never be chat/tool capable ──────────
def test_configured_embedding_model_is_embedding_only_never_chat(monkeypatch):
    monkeypatch.setenv("JARVIS_MODEL_EMBEDDING", "nomic-embed-text:latest")
    from core.model_capabilities import InferenceSurface, is_chat_safe, supports_surface
    from core.model_router import resolve_embedding_model

    model = resolve_embedding_model()
    assert supports_surface(model, InferenceSurface.EMBEDDING)
    assert not is_chat_safe(model), "embedding-only model must never be chat-safe"
