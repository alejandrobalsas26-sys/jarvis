"""
core/vision_engine.py — JARVIS visual intelligence engine (v38.0).

Uses Ollama multimodal API with moondream:latest (1.8B params).
Fast enough for real-time analysis on Ryzen 5 7430U.
Fully offline — no external API calls.

Capabilities:
  - Analyze any image file (PNG, JPG, BMP)
  - Analyze live screenshots of desktop
  - Analyze web page captures from Playwright
  - Describe network diagrams, terminal output, malware samples
  - OCR-like text extraction from screenshots

Ollama multimodal message format:
  {"role": "user", "content": [
      {"type": "image_url",
       "image_url": {"url": "data:image/png;base64,<b64>"}},
      {"type": "text", "text": "<prompt>"}
  ]}
"""

import asyncio, base64, os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from loguru import logger

_VISION_MODEL  = os.getenv("JARVIS_VISION_MODEL", "moondream:latest")
_VISION_SYSTEM = (
    "You are JARVIS's visual cortex — an expert security analyst "
    "analyzing images. Be precise and technical. Extract all text "
    "visible in the image. Identify security-relevant artifacts: "
    "IP addresses, error messages, code, network diagrams, malware "
    "indicators. Respond concisely."
)


def _encode_image(image_data: bytes) -> str:
    """Base64-encode image bytes for Ollama multimodal API."""
    return base64.b64encode(image_data).decode("utf-8")


def _capture_screen(monitor: int = 1) -> bytes:
    """
    Capture full screen or specific monitor as PNG bytes.
    mss is ~10ms per screenshot — negligible overhead.
    """
    import mss
    import mss.tools
    with mss.mss() as sct:
        monitors = sct.monitors
        mon = monitors[min(monitor, len(monitors)-1)]
        screenshot = sct.grab(mon)
        # Convert to PNG bytes
        png_bytes = mss.tools.to_png(screenshot.rgb, screenshot.size)
        return png_bytes


async def analyze_image(
    image_data: bytes,
    prompt: str,
    ollama_client,
    broadcast_fn = None,
) -> str:
    """
    Analyze an image using the local vision model.
    Returns analysis string.
    """
    b64 = _encode_image(image_data)

    messages = [{
        "role": "user",
        "content": [
            {
                "type":      "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
            {
                "type": "text",
                "text": _VISION_SYSTEM + "\n\n" + prompt,
            },
        ],
    }]

    if broadcast_fn:
        await broadcast_fn({
            "type":      "vision_analyzing",
            "prompt":    prompt[:80],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = _VISION_MODEL,
                messages = messages,
                stream   = False,
                extra_body = {"options": {"num_ctx": 1024, "temperature": 0.1}},
            ),
            timeout=45.0,
        )
        analysis = response.choices[0].message.content.strip()

        if broadcast_fn:
            await broadcast_fn({
                "type":     "vision_complete",
                "analysis": analysis[:400],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        logger.info(f"VISION: analysis complete ({len(analysis)} chars)")
        return analysis

    except asyncio.TimeoutError:
        logger.warning("VISION: analysis timeout")
        return "[Vision: timeout — model may not be loaded]"
    except Exception as e:
        logger.debug(f"VISION: {e}")
        return f"[Vision: error — {e}]"


async def analyze_screen(
    prompt: str,
    ollama_client,
    broadcast_fn,
    monitor: int = 1,
    tts = None,
) -> str:
    """
    Capture desktop screenshot and analyze it.
    Voice command: "JARVIS what do you see"
    """
    loop = asyncio.get_running_loop()
    image_data = await loop.run_in_executor(
        None, _capture_screen, monitor
    )

    logger.info("VISION: captured desktop screenshot for analysis")

    # Save screenshot to logs/visuals/
    _save_screenshot(image_data, "screen_capture")

    analysis = await analyze_image(
        image_data, prompt, ollama_client, broadcast_fn
    )

    if tts and analysis:
        # Speak first 2 sentences only
        sentences = analysis.split(". ")[:2]
        asyncio.create_task(tts.speak_async(". ".join(sentences)))

    return analysis


async def analyze_image_file(
    file_path: Path,
    prompt: str,
    ollama_client,
    broadcast_fn,
) -> str:
    """Analyze an image file from disk."""
    try:
        image_data = file_path.read_bytes()
        return await analyze_image(
            image_data, prompt, ollama_client, broadcast_fn
        )
    except Exception as e:
        logger.debug(f"VISION: file read error: {e}")
        return f"[Vision: could not read {file_path.name}]"


def _save_screenshot(image_data: bytes, label: str) -> Path:
    """Save screenshot to logs/visuals/ with timestamp."""
    visuals_dir = Path("logs/visuals")
    visuals_dir.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{label}_{ts}.png"
    path     = visuals_dir / filename
    path.write_bytes(image_data)
    return path


async def capture_and_save(
    label: str = "capture",
    monitor: int = 1,
) -> Path:
    """
    Capture screenshot and save. Returns path.
    Used by incident reporter for visual evidence.
    """
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _capture_screen, monitor)
    return _save_screenshot(data, label)


# ── v46.0 OMEGA — Webcam + Moondream vision ────────────────────────────────

async def capture_webcam_frame() -> bytes | None:
    """
    Capture a single frame from the default webcam.
    Returns JPEG bytes or None if no webcam available.
    """
    try:
        import cv2
        loop = asyncio.get_event_loop()

        def _grab():
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return None
            # Warm up — discard first 3 frames (exposure adjustment)
            for _ in range(3):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if not ret or frame is None:
                return None
            _, buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, 85]
            )
            return buf.tobytes()

        return await loop.run_in_executor(None, _grab)

    except ImportError:
        logger.warning("VISION: opencv not installed — pip install opencv-python")
        return None
    except Exception as e:
        logger.warning(f"VISION: webcam capture error: {e}")
        return None


async def analyze_room(
    ollama_client,
    model_vision: str = "moondream:latest",
    query: str = "Describe everything you see in detail.",
) -> str:
    """
    Capture webcam frame and analyze with Moondream.
    Returns natural language description of the room/environment.
    """
    import base64, aiohttp, time

    frame_bytes = await capture_webcam_frame()
    if not frame_bytes:
        return "No webcam detected or unable to capture frame."

    img_b64 = base64.b64encode(frame_bytes).decode()

    # Save for reference
    ts = int(time.time())
    snap_path = Path("logs/visuals") / f"room_{ts}.jpg"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_bytes(frame_bytes)

    try:
        ollama_host = "http://127.0.0.1:11434"
        payload = {
            "model": model_vision,
            "messages": [{
                "role": "user",
                "content": query,
                "images": [img_b64],
            }],
            "stream": False,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{ollama_host}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                desc = (data.get("message", {})
                           .get("content", "No description returned."))
                logger.info(f"VISION: room analysis complete — {len(desc)} chars")
                return desc

    except Exception as e:
        logger.warning(f"VISION: moondream error: {e}")
        return f"Vision analysis failed: {e}"


async def analyze_screen_vision(
    ollama_client,
    model_vision: str = "moondream:latest",
    query: str = "Describe what you see on this screen in detail.",
) -> str:
    """
    Capture screen and analyze with Moondream.
    Returns natural language description of screen content.
    """
    import base64, aiohttp, io

    try:
        png_bytes = _capture_screen()
        if not png_bytes:
            return "Screen capture unavailable."

        # Re-encode PNG to JPEG for Moondream
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            img_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            img_b64 = base64.b64encode(png_bytes).decode()

        payload = {
            "model": model_vision,
            "messages": [{
                "role": "user",
                "content": query,
                "images": [img_b64],
            }],
            "stream": False,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:11434/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
                return (data.get("message", {})
                           .get("content", "No description returned."))
    except Exception as e:
        return f"Screen analysis failed: {e}"
