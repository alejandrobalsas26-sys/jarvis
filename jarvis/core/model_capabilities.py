"""core/model_capabilities.py — V67 M27: inference-surface capability model.

Separates two axes the router previously conflated:

  * **ROLE SELECTION** — which cognitive role a turn belongs to (FAST/CODER/DEEP/
    VISION/EMBEDDING/VERIFIER). Owned by :mod:`core.model_router` (``route()`` /
    ``ModelRole`` / ``resolve_role_model``). This module does NOT touch it.
  * **INFERENCE SURFACE CAPABILITY** — what a concrete Ollama model can actually
    *do* at the wire: hold a tool-use chat, accept an image, or only emit
    embedding vectors.

The live tool-use streaming path (``llm.chat_stream``) *always* passes
``tools=TOOLS`` and streams natural language. A model that can only produce
embeddings (``nomic-embed-text``) or is a vision-first / weak-tool model
(``gemma3``, ``llava``, ``moondream``) must NEVER be handed to that path — even
when the operator legitimately configured it for the VISION/EMBEDDING *role*
(``JARVIS_MODEL_VISION`` / ``JARVIS_MODEL_EMBEDDING``). Before V67 that leak was
live: ``resolve_inference_model`` returned the role model verbatim on an env
override, so an EMBEDDING-classified turn could stream chat from
``nomic-embed-text`` (embedding-only → cannot chat).

This module is the single place that knows model → capability, so the routing
logic downstream reasons over *capabilities*, never hardcoded family names (the
names live here as data, in exactly one table). Pure and dependency-free.
"""
from __future__ import annotations

from enum import Enum


class InferenceSurface(str, Enum):
    """The concrete wire surface a model is being asked to serve."""
    CHAT = "chat"            # conversational + tool-use streaming (llm.chat_stream)
    VISION = "vision"        # image understanding (vision_engine)
    EMBEDDING = "embedding"  # vector generation only


class ModelCapability(str, Enum):
    """What a model can do. A model carries a *set* of these."""
    CHAT_CAPABLE = "chat_capable"            # can hold a natural-language conversation
    TOOL_CAPABLE = "tool_capable"            # can reliably tool-call in a chat stream
    VISION_CAPABLE = "vision_capable"        # can accept image inputs
    EMBEDDING_CAPABLE = "embedding_capable"  # produces embedding vectors
    TOOL_ONLY = "tool_only"                  # (reserved) structured tool-call only


# ── Capability profiles (the ONE place model-family names appear) ─────────────
_CHAT_TOOL = frozenset({ModelCapability.CHAT_CAPABLE, ModelCapability.TOOL_CAPABLE})
_CHAT_ONLY = frozenset({ModelCapability.CHAT_CAPABLE})
_VISION_CHAT = frozenset({ModelCapability.CHAT_CAPABLE, ModelCapability.VISION_CAPABLE})
_VISION_ONLY = frozenset({ModelCapability.VISION_CAPABLE})
_EMBED_ONLY = frozenset({ModelCapability.EMBEDDING_CAPABLE})

# Longest / most-specific family prefix wins (checked in order). Everything
# downstream operates on the capability sets, never these strings.
_FAMILY_CAPABILITIES: tuple[tuple[str, frozenset[ModelCapability]], ...] = (
    # Embedding-only — must NEVER reach a chat/tool stream.
    ("nomic-embed", _EMBED_ONLY),
    ("mxbai-embed", _EMBED_ONLY),
    ("snowflake-arctic-embed", _EMBED_ONLY),
    ("all-minilm", _EMBED_ONLY),
    ("granite-embedding", _EMBED_ONLY),
    ("bge-", _EMBED_ONLY),
    ("gte-", _EMBED_ONLY),
    ("paraphrase-", _EMBED_ONLY),
    # Vision-first (image understanding; no / weak tool-use streaming).
    ("llava", _VISION_ONLY),
    ("bakllava", _VISION_ONLY),
    ("moondream", _VISION_ONLY),
    ("minicpm-v", _VISION_CHAT),
    ("llama3.2-vision", _VISION_CHAT),
    ("gemma3", _VISION_CHAT),   # multimodal chat, but not a reliable tool-caller
    ("gemma2", _CHAT_ONLY),
    ("gemma", _CHAT_ONLY),
    # Reasoning models — chat, but <think> noise + weak tool-calling on the stream.
    ("deepseek-r1", _CHAT_ONLY),
    ("phi3", _CHAT_ONLY),
    ("phi4", _CHAT_ONLY),
    # General chat + tool-calling models (the live-brain default families).
    ("qwen3", _CHAT_TOOL),
    ("qwen2.5-coder", _CHAT_TOOL),
    ("qwen2.5", _CHAT_TOOL),
    ("qwen2", _CHAT_TOOL),
    ("llama3.3", _CHAT_TOOL),
    ("llama3.1", _CHAT_TOOL),
    ("llama3", _CHAT_TOOL),
    ("mistral", _CHAT_TOOL),
    ("mixtral", _CHAT_TOOL),
    ("command-r", _CHAT_TOOL),
    ("hermes", _CHAT_TOOL),
    ("firefunction", _CHAT_TOOL),
    ("granite3", _CHAT_TOOL),
)

# Unknown model → assume a general chat+tool model, so a custom/newer model the
# operator pulls is not silently downgraded. Only the embedding/vision families
# named above are treated as unable to chat.
_DEFAULT_CAPABILITIES = _CHAT_TOOL

# Substrings that force embedding-only even for an unrecognized family
# (defense-in-depth: any "*embed*" model cannot chat).
_EMBED_MARKERS = ("embed",)


def _norm(name: str | None) -> str:
    return (name or "").strip().lower()


def capabilities_for(model_name: str | None) -> frozenset[ModelCapability]:
    """Resolve the capability set for a concrete model name (repo[:tag])."""
    n = _norm(model_name)
    if not n:
        return _DEFAULT_CAPABILITIES
    for prefix, caps in _FAMILY_CAPABILITIES:
        if n.startswith(prefix) or ("/" + prefix) in n:
            return caps
    if any(m in n for m in _EMBED_MARKERS):
        return _EMBED_ONLY
    return _DEFAULT_CAPABILITIES


_SURFACE_REQUIREMENT: dict[InferenceSurface, ModelCapability] = {
    # The chat stream always carries tools, so tool-capability is the real bar.
    InferenceSurface.CHAT: ModelCapability.TOOL_CAPABLE,
    InferenceSurface.VISION: ModelCapability.VISION_CAPABLE,
    InferenceSurface.EMBEDDING: ModelCapability.EMBEDDING_CAPABLE,
}


def supports_surface(model_name: str | None, surface: InferenceSurface) -> bool:
    """True iff *model_name* can serve *surface*."""
    return _SURFACE_REQUIREMENT[surface] in capabilities_for(model_name)


def is_chat_safe(model_name: str | None) -> bool:
    """True iff *model_name* can drive the tool-use chat-streaming path.

    Embedding-only, vision-only, and known weak-tool models return False, so the
    caller falls back to a chat-capable model for the conversational surface.
    """
    return supports_surface(model_name, InferenceSurface.CHAT)
