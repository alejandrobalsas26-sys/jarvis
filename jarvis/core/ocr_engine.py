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


async def extract_text_from_image(
    image_data: bytes,
    detail_level: int = 0,
) -> list[str]:
    """
    Extract all text from image bytes using easyocr.
    Returns list of detected text strings.
    """
    reader = await _get_reader()
    loop   = asyncio.get_running_loop()

    def _read():
        import numpy as np
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(image_data)).convert("RGB")
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

    ocr_texts = await extract_text_from_image(image_data, detail_level=1)
    ocr_raw   = "\n".join(ocr_texts[:150])

    logger.info(
        f"OCR: extracted {len(ocr_texts)} text regions from screen"
    )

    content_type = _classify_content(ocr_raw)

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
