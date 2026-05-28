"""
core/ocr_engine.py — OCR-enhanced screen intelligence engine (v40.0).

Combines two approaches for maximum accuracy:
  1. easyocr — precise character extraction (hex, assembly, IPs)
  2. moondream vision (v38) — contextual understanding and analysis
"""

import asyncio, re
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_reader = None   # lazy-init easyocr reader (heavy — ~500MB model)
_reader_lock = asyncio.Lock()

_OCR_LANGUAGES = ["en"]

_ANALYSIS_SYSTEM = """You are JARVIS analyzing extracted screen content
from a cybersecurity tool. The text was OCR-extracted and may have
minor character errors. Analyze technically and precisely:
1. What tool/context is this from?
2. What security-relevant information is present?
3. IOCs visible (IPs, hashes, domains, registry keys)?
4. If assembly/disassembly: what does this code do? MITRE technique?
5. Risk verdict: CRITICAL/HIGH/MEDIUM/LOW"""


async def _get_reader():
    """Lazy-initialize easyocr reader. First call takes ~10s."""
    global _reader
    async with _reader_lock:
        if _reader is None:
            loop = asyncio.get_running_loop()
            logger.info("OCR: initializing easyocr reader (~10s first time)…")
            def _init():
                import easyocr
                return easyocr.Reader(_OCR_LANGUAGES, gpu=False, verbose=False)
            _reader = await loop.run_in_executor(None, _init)
            logger.info("OCR: reader ready")
    return _reader


def _preprocess_for_ocr(
    image_data: bytes,
    force_mode: str = "auto",
) -> bytes:
    """
    Adaptive OCR pre-processing using OpenCV.

    force_mode:
      "auto"   — detect dark/light theme and apply correct pipeline
      "dark"   — force dark terminal pipeline (invert + threshold)
      "light"  — force light background pipeline (just sharpen)
      "hex"    — specialized for hex dumps (upscale + high contrast)
      "none"   — skip preprocessing, pass through unchanged

    Auto-detection: if mean pixel value < 100 → dark theme
    Dark pipeline:  invert → CLAHE → Otsu threshold (for green/white on black)
    Light pipeline: CLAHE → sharpen (for black on white)
    Hex pipeline:   2x upscale → CLAHE → adaptive threshold
    """
    if force_mode == "none":
        return image_data

    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_data

        # ── Auto-detect theme ─────────────────────────────────────────────
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mean_brightness = float(np.mean(gray))

        if force_mode == "auto":
            if mean_brightness < 100:
                mode = "dark"
            elif mean_brightness > 200:
                mode = "light"
            else:
                mode = "light"   # medium brightness → treat as light
        else:
            mode = force_mode

        # ── CLAHE for contrast enhancement (works for both themes) ────────
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        if mode == "dark":
            # Dark terminal: invert colors first so text becomes dark on light
            inverted = cv2.bitwise_not(gray)
            enhanced = clahe.apply(inverted)
            # Otsu's binarization — optimal threshold for bimodal histograms
            _, processed = cv2.threshold(
                enhanced, 0, 255,
                cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )

        elif mode == "hex":
            # Hex dumps: upscale 2x for small font, then high contrast
            h, w = gray.shape
            upscaled = cv2.resize(gray, (w * 2, h * 2),
                                   interpolation=cv2.INTER_CUBIC)
            enhanced = clahe.apply(upscaled)
            # Adaptive threshold handles uneven lighting in hex editors
            processed = cv2.adaptiveThreshold(
                enhanced, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 11, 2,
            )

        else:  # light
            # Light background: just enhance contrast, no inversion
            enhanced  = clahe.apply(gray)
            # Mild sharpen kernel
            kernel    = np.array([[0,-1,0],[-1,5,-1],[0,-1,0]])
            processed = cv2.filter2D(enhanced, -1, kernel)

        # Encode back to PNG bytes
        _, buffer = cv2.imencode(".png", processed)
        return buffer.tobytes()

    except Exception as e:
        logger.debug(f"OCR_PREPROCESS: error: {e}")
        return image_data   # fallback: return original unchanged


async def extract_text_from_image(
    image_data: bytes,
    detail_level: int = 0,
    preprocess_mode: str = "auto",
) -> list[str]:
    """
    Extract all text from image bytes using easyocr.
    preprocess_mode: auto/dark/light/hex/none
    Returns list of detected text strings.
    """
    loop   = asyncio.get_running_loop()

    # Apply adaptive preprocessing first (CPU-bound — offload to thread)
    processed_data = await loop.run_in_executor(
        None, _preprocess_for_ocr, image_data, preprocess_mode
    )

    reader = await _get_reader()

    def _read():
        import numpy as np
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(processed_data)).convert("RGB")
        arr = np.array(img)
        return reader.readtext(arr, detail=detail_level,
                               paragraph=False,
                               batch_size=4)

    try:
        results = await loop.run_in_executor(None, _read)
        if detail_level == 0:
            return [str(r) for r in results if str(r).strip()]
        else:
            return [str(r[1]) for r in results
                    if len(r) >= 2 and str(r[1]).strip()]
    except Exception as e:
        logger.debug(f"OCR: extraction error: {e}")
        return []


async def read_screen_and_analyze(
    prompt_context: str,
    broadcast_fn,
    ollama_client,
    model: str,
    tts=None,
    monitor: int = 1,
) -> str:
    """
    Full pipeline: screenshot → OCR → LLM analysis → TTS verdict.
    Voice trigger: "JARVIS analyze code on screen"
    """
    from core.vision_engine import _capture_screen

    loop = asyncio.get_running_loop()

    await broadcast_fn({
        "type":    "ocr_started",
        "context": prompt_context[:60],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    image_data = await loop.run_in_executor(None, _capture_screen, monitor)

    # First pass: OCR with auto preprocessing to classify content type
    ocr_texts = await extract_text_from_image(
        image_data, detail_level=1, preprocess_mode="auto",
    )
    ocr_raw      = "\n".join(ocr_texts[:150])
    content_type = _classify_content(ocr_raw)

    # Refine: re-OCR with content-type-optimal preprocessing if it differs
    preprocess_map = {
        "hex_dump":        "hex",
        "assembly":        "dark",
        "disassembly":     "dark",
        "terminal_output": "dark",
        "network_capture": "auto",
        "general":         "auto",
    }
    preferred_mode = preprocess_map.get(content_type, "auto")
    if preferred_mode != "auto":
        ocr_texts = await extract_text_from_image(
            image_data, detail_level=1, preprocess_mode=preferred_mode,
        )
        ocr_raw = "\n".join(ocr_texts[:150])
        logger.info(
            f"OCR: refined extraction with '{preferred_mode}' mode "
            f"for {content_type}"
        )

    logger.info(
        f"OCR: extracted {len(ocr_texts)} text regions from screen"
    )

    from core.vision_engine import analyze_image
    vision_desc = await analyze_image(
        image_data,
        f"Describe what tool/application is shown and what data "
        f"is visible. Be precise about any code, hex, or text.",
        ollama_client,
    )

    combined_prompt = (
        f"USER CONTEXT: {prompt_context}\n"
        f"DETECTED CONTENT TYPE: {content_type}\n\n"
        f"VISION DESCRIPTION:\n{vision_desc[:500]}\n\n"
        f"OCR EXTRACTED TEXT:\n{ocr_raw[:3000]}\n\n"
        "Provide full security analysis:"
    )

    try:
        resp = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _ANALYSIS_SYSTEM},
                    {"role": "user",   "content": combined_prompt},
                ],
                stream = False,
                extra_body = {"options": {"num_ctx": 3072, "temperature": 0.1}},
            ),
            timeout=60.0,
        )
        analysis = resp.choices[0].message.content.strip()
    except Exception as e:
        analysis = f"[OCR analysis failed: {e}]"

    await broadcast_fn({
        "type":          "ocr_complete",
        "content_type":  content_type,
        "regions":       len(ocr_texts),
        "analysis":      analysis[:400],
        "severity":      _severity_from_analysis(analysis),
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    if tts and analysis:
        sentences = analysis.split(". ")[:3]
        asyncio.create_task(tts.speak_async(". ".join(sentences)))

    return analysis


def _classify_content(text: str) -> str:
    """Classify OCR content type from extracted text patterns."""
    t = text.lower()
    hex_count  = len(re.findall(r'\b[0-9a-f]{8}\b', t))
    asm_kws    = sum(1 for w in ("mov", "push", "pop", "call", "jmp",
                                  "ret", "xor", "lea", "sub", "add")
                     if re.search(rf'\b{w}\b', t))
    pcap_kws   = sum(1 for w in ("tcp", "udp", "icmp", "http", "syn",
                                  "ack", "ethernet", "frame") if w in t)
    term_kws   = sum(1 for w in ("error", "warning", "c:\\", "powershell",
                                  "cmd", "bash", "root@", "$") if w in t)

    if hex_count > 10 and asm_kws > 3:
        return "disassembly"
    elif hex_count > 20:
        return "hex_dump"
    elif pcap_kws > 3:
        return "network_capture"
    elif asm_kws > 5:
        return "assembly"
    elif term_kws > 3:
        return "terminal_output"
    else:
        return "general"


def _severity_from_analysis(analysis: str) -> str:
    a = analysis.upper()
    if "CRITICAL" in a: return "CRITICAL"
    if "HIGH"     in a: return "HIGH"
    if "MEDIUM"   in a: return "MEDIUM"
    return "INFO"
