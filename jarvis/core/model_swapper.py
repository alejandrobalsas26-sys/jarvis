"""
core/model_swapper.py — Ollama model hot-swap engine (v36.0).

Switches the active LLM model without restarting JARVIS.
Conversation history is fully preserved across swaps.

Voice triggers:
  "jarvis use deep model" → switch to 14B
  "jarvis use fast model" → switch to 7B
  "jarvis switch model"   → toggle between fast/deep
"""

from loguru import logger


_current_mode: str = "fast"   # "fast" or "deep"
_llm_ref            = None    # reference to the LLM instance


def attach(llm_instance) -> None:
    global _llm_ref
    _llm_ref = llm_instance


def get_current_mode() -> str:
    return _current_mode


async def swap_to_deep(broadcast_fn) -> bool:
    """Switch to deep (14B) model."""
    global _current_mode
    if _llm_ref is None:
        return False

    try:
        from core.hardware_profile import get_cached_profile
        hw = get_cached_profile()
    except Exception:
        hw = None
    if hw is None:
        return False

    _current_mode = "deep"

    try:
        import core.model_router as mr
        mr.MODEL_FAST = hw.model_deep   # force deep for all queries
    except Exception as e:
        logger.debug(f"MODEL_SWAP: router patch failed: {e}")

    logger.info(f"MODEL_SWAP: → {hw.model_deep} [DEEP MODE]")
    try:
        await broadcast_fn({
            "type":  "model_swapped",
            "mode":  "deep",
            "model": hw.model_deep,
        })
    except Exception:
        pass
    return True


async def swap_to_fast(broadcast_fn) -> bool:
    """Switch to fast (7B) model."""
    global _current_mode
    if _llm_ref is None:
        return False

    try:
        from core.hardware_profile import get_cached_profile
        hw = get_cached_profile()
    except Exception:
        hw = None
    if hw is None:
        return False

    _current_mode = "fast"

    try:
        import core.model_router as mr
        mr.MODEL_FAST = hw.model_fast   # restore fast model
    except Exception as e:
        logger.debug(f"MODEL_SWAP: router patch failed: {e}")

    logger.info(f"MODEL_SWAP: → {hw.model_fast} [FAST MODE]")
    try:
        await broadcast_fn({
            "type":  "model_swapped",
            "mode":  "fast",
            "model": hw.model_fast,
        })
    except Exception:
        pass
    return True


async def toggle(broadcast_fn) -> str:
    """Toggle between fast and deep model."""
    if _current_mode == "fast":
        await swap_to_deep(broadcast_fn)
        return "deep"
    else:
        await swap_to_fast(broadcast_fn)
        return "fast"
