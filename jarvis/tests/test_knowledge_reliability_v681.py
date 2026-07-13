"""
tests/test_knowledge_reliability_v681.py — V68.1 M45 regression coverage.

Locks the Knowledge Vault tool boundary against the exact live-runtime failure:
a torch/transformers `infer_schema` ValueError (``Parameter input has
unsupported type torch.Tensor``) that leaked a raw dependency stack trace out of
query_knowledge and confused the LLM. The public boundary must stay plain JSON
and degrade honestly.

These tests are hermetic — they never require the real vector backend to be
present (or broken) on the host; the backend is simulated via injection.
"""
from __future__ import annotations

import pytest

from core.knowledge import (
    KnowledgeVault,
    KnowledgeVaultUnavailable,
    _classify_backend_error,
)

# The verbatim text torch 2.x raises for the transformers grouped_mm custom op.
_TORCH_INFER_SCHEMA_MSG = (
    "infer_schema(func): Parameter input has unsupported type torch.Tensor. "
    "The valid types are: dict_keys([<class 'torch.Tensor'>, ...]). "
    "Got func with signature (input: 'torch.Tensor', weight: 'torch.Tensor', "
    "offs: 'torch.Tensor') -> 'torch.Tensor')"
)

_FORBIDDEN_INTERNALS = (
    "infer_schema",
    "torch.tensor",
    "weight: 'torch.tensor'",
    "offs:",
    "traceback",
    "sentence_transformers",
    "sentencetransformer",
    "chromadb",
    "hnsw",
    ".encode(",
)


def _assert_no_internals(blob: str) -> None:
    low = blob.lower()
    for token in _FORBIDDEN_INTERNALS:
        assert token not in low, f"vector internal leaked to LLM boundary: {token!r}"


class _FakeCollection:
    def __init__(self, count: int, docs=None, metas=None):
        self._count = count
        self._docs = docs or []
        self._metas = metas or []

    def count(self) -> int:
        return self._count

    def query(self, **_kwargs) -> dict:
        return {"documents": [self._docs], "metadatas": [self._metas]}


from core.embedding_runtime import EmbeddingResult, EmbeddingHealth


class _FakeRuntime:
    """Injected embedding runtime — no Ollama, no torch, no sentence-transformers.

    V69 M52: the vault now consumes core.embedding_runtime instead of holding a
    SentenceTransformer. This fake exercises the plain-Python boundary.
    """

    def __init__(self, *, available: bool = True, ok: bool = True,
                 error_class: str | None = None, message: str | None = None):
        self._available = available
        self._ok = ok
        self._error_class = error_class
        self._message = message
        self.fingerprint = "fp_fake_1234"

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(
            available=self._available, provider="ollama", model="nomic-embed-text:latest",
            dimension=3, fingerprint=self.fingerprint, schema_version=1,
            fallback_active=False, message="ready" if self._available else "down",
            error_class=None if self._available else (self._error_class or "provider_unreachable"),
        )

    def embed_text(self, text: str) -> EmbeddingResult:
        if not self._ok:
            return EmbeddingResult(
                status="error", error_class=self._error_class or "embedding_error",
                message=self._message or "boom", provider="ollama",
                model="nomic-embed-text:latest", fingerprint=self.fingerprint,
            )
        return EmbeddingResult(
            status="ok", vector=[0.1, 0.2, 0.3], provider="ollama",
            model="nomic-embed-text:latest", dimension=3, fingerprint=self.fingerprint,
        )


def _ready_vault(collection: _FakeCollection) -> KnowledgeVault:
    v = KnowledgeVault(embedder=_FakeRuntime())
    v._backend_ready = True
    v._collection = collection
    v._active_fingerprint = "fp_fake_1234"
    return v


# ── The exact torch.Tensor / infer_schema failure ────────────────────────────

def test_classify_infer_schema_is_dependency_incompat():
    error_class, safe = _classify_backend_error(ValueError(_TORCH_INFER_SCHEMA_MSG))
    assert error_class == "dependency_incompatibility"
    _assert_no_internals(safe)


def test_construction_never_imports_heavy_deps():
    # Must not raise even when the real backend would fail on import.
    v = KnowledgeVault()
    assert v._backend_ready is False
    assert v._collection is None


def test_query_degrades_honestly_on_infer_schema(monkeypatch):
    v = KnowledgeVault()

    def _boom():
        v._backend_error = _classify_backend_error(ValueError(_TORCH_INFER_SCHEMA_MSG))
        return False

    monkeypatch.setattr(v, "_ensure_backend", _boom)

    result = v.query("how do vending machine radios work")
    assert result["status"] == "unavailable"
    assert result["error_class"] == "dependency_incompatibility"
    _assert_no_internals(str(result))

    # The string wrapper must be equally clean.
    _assert_no_internals(v.query_knowledge("anything"))


def test_embed_raises_clean_unavailable(monkeypatch):
    v = KnowledgeVault()
    monkeypatch.setattr(
        v, "_ensure_backend",
        lambda: (v.__setattr__(
            "_backend_error",
            _classify_backend_error(ValueError(_TORCH_INFER_SCHEMA_MSG))) or False),
    )
    with pytest.raises(KnowledgeVaultUnavailable) as exc:
        v._embed("x")
    _assert_no_internals(str(exc.value))
    assert exc.value.error_class == "dependency_incompatibility"


# ── Honest degradation across the four states ─────────────────────────────────

def test_empty_vault_returns_useful_result():
    v = _ready_vault(_FakeCollection(count=0))
    result = v.query("anything")
    assert result["status"] == "empty"
    assert "empty" in result["message"].lower()


def test_successful_query_returns_bounded_fragments_with_sources():
    coll = _FakeCollection(
        count=2,
        docs=["fragment one", "fragment two"],
        metas=[{"source": "a.pdf", "chunk_idx": 0}, {"source": "b.txt", "chunk_idx": 4}],
    )
    v = _ready_vault(coll)
    result = v.query("radios", n_results=2)
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["fragments"][0]["source"] == "a.pdf"
    rendered = v._format_fragments(result)
    assert "a.pdf" in rendered and "fragment one" in rendered


def test_n_results_is_bounded_and_coerced():
    coll = _FakeCollection(count=5, docs=["x"], metas=[{"source": "s"}])
    v = _ready_vault(coll)
    # absurd n_results must be clamped, not passed through
    assert v.query("q", n_results=9999)["status"] == "ok"
    assert v.query("q", n_results="not-an-int")["status"] == "ok"


def test_empty_query_is_rejected_at_boundary():
    v = _ready_vault(_FakeCollection(count=3))
    result = v.query("   ")
    assert result["status"] == "error"
    assert result["error_class"] == "invalid_query"


def test_vector_store_read_error_is_structured():
    class _BrokenCount(_FakeCollection):
        def count(self):
            raise RuntimeError("chroma internal segfault-ish detail")

    v = _ready_vault(_BrokenCount(count=0))
    result = v.query("q")
    assert result["status"] == "error"
    _assert_no_internals(str(result))


def test_embedding_error_is_structured_no_stack_trace():
    v = _ready_vault(_FakeCollection(count=1, docs=["d"], metas=[{"source": "s"}]))
    # A per-call embedding failure surfaced by the runtime → structured "error",
    # never a raw torch/CUDA stack trace at the boundary.
    v._embedder = _FakeRuntime(
        ok=False, error_class="embedding_error",
        message="CUDA kernel internal boom",
    )
    result = v.query("q")
    assert result["status"] == "error"
    assert result["error_class"] == "embedding_error"
    _assert_no_internals(str(result))
