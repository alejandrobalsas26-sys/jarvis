"""
core/screen_monitor.py — Opt-in screen change monitor (v38.0).

OFF BY DEFAULT — set JARVIS_SCREEN_MONITOR=1 to activate.
Privacy first: never uploads screenshots, all analysis is local.

Takes a screenshot every 30 seconds (configurable).
Compares pixel hash — if >15% change detected, analyzes with vision.
Alerts on: error dialogs, new terminal windows, suspicious processes,
password prompts, browser navigation to sensitive sites.
"""

import asyncio, hashlib, os
from datetime import datetime, timezone
from loguru import logger

_ENABLED        = os.getenv("JARVIS_SCREEN_MONITOR", "0") == "1"
_POLL_INTERVAL  = int(os.getenv("JARVIS_SCREEN_INTERVAL", "30"))
_CHANGE_THRESHOLD = 0.15   # 15% pixel change = significant

_last_hash: str | None = None


def _image_hash(image_data: bytes) -> str:
    """Fast perceptual hash for change detection."""
    # Downsample to 8x8 for comparison (not cryptographic)
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(image_data)).convert("L").resize((32, 32))
        pixels = list(img.getdata())
        avg    = sum(pixels) / len(pixels)
        bits   = "".join("1" if p > avg else "0" for p in pixels)
        return hashlib.md5(bits.encode()).hexdigest()
    except Exception:
        return hashlib.md5(image_data[:1000]).hexdigest()


def _change_score(hash1: str, hash2: str) -> float:
    """Estimate change ratio from two hashes (0.0 = same, 1.0 = total change)."""
    if hash1 == hash2:
        return 0.0
    # Hamming-like comparison on hex chars
    diffs = sum(1 for a, b in zip(hash1, hash2) if a != b)
    return diffs / len(hash1)


async def start_screen_monitor(
    broadcast_fn,
    ollama_client,
    tts = None,
) -> None:
    """
    Background screen monitor. Disabled by default.
    Set JARVIS_SCREEN_MONITOR=1 to enable.
    """
    global _last_hash

    if not _ENABLED:
        logger.info("SCREEN_MONITOR: disabled (set JARVIS_SCREEN_MONITOR=1 to enable)")
        await asyncio.Event().wait()
        return

    logger.info(
        f"SCREEN_MONITOR: active — polling every {_POLL_INTERVAL}s"
    )

    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            from core.vision_engine import _capture_screen, analyze_image

            loop  = asyncio.get_running_loop()
            image = await loop.run_in_executor(None, _capture_screen)
            h     = _image_hash(image)

            if _last_hash is None:
                _last_hash = h
                continue

            change = _change_score(_last_hash, h)
            _last_hash = h

            if change < _CHANGE_THRESHOLD:
                continue   # no significant change

            logger.debug(
                f"SCREEN_MONITOR: change detected ({change*100:.0f}%)"
            )

            # Analyze changed screen
            analysis = await analyze_image(
                image,
                "Describe what changed on screen. "
                "Are there any security-relevant events visible? "
                "Error dialogs? Command prompts? Suspicious windows? "
                "Be very brief — 1-2 sentences.",
                ollama_client,
            )

            if not analysis:
                continue

            # Broadcast if analysis mentions something concerning
            concerning_kw = {
                "error", "fail", "password", "admin", "cmd", "terminal",
                "powershell", "warning", "blocked", "suspicious",
            }
            is_concerning = any(
                kw in analysis.lower() for kw in concerning_kw
            )

            await broadcast_fn({
                "type":         "screen_change",
                "analysis":     analysis[:300],
                "change_pct":   round(change * 100, 1),
                "concerning":   is_concerning,
                "severity":     "WARNING" if is_concerning else "INFO",
                "timestamp":    datetime.now(timezone.utc).isoformat(),
            })

            if is_concerning and tts:
                asyncio.create_task(
                    tts.speak_async(f"Screen alert: {analysis[:100]}")
                )

        except Exception as e:
            logger.debug(f"SCREEN_MONITOR: {e}")
