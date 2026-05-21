"""core/yara_analyzer.py — Single-pass YARA rule compiler + async command scanner."""

import asyncio
import functools
from pathlib import Path

from loguru import logger


@functools.lru_cache(maxsize=1)
def _compile_rules():
    """Compile all .yar rule files once at startup; result is cached for the process lifetime."""
    try:
        import yara
    except ImportError:
        logger.warning("yara_analyzer: yara-python not installed — scanner disabled. pip install yara-python")
        return None

    sig_dir = Path(__file__).parent / "signatures"
    sig_dir.mkdir(exist_ok=True)

    yar_files = sorted(sig_dir.glob("*.yar"))
    if not yar_files:
        logger.warning("yara_analyzer: no .yar files found in core/signatures/ — scanner inactive")
        return None

    try:
        filepaths = {p.stem: str(p) for p in yar_files}
        rules = yara.compile(filepaths=filepaths)
        logger.info(f"yara_analyzer: compiled {len(yar_files)} rule file(s): {[p.stem for p in yar_files]}")
        return rules
    except Exception as exc:
        logger.warning(f"yara_analyzer: rule compilation failed — {exc}")
        return None


# Public alias — allows threat_feed_sync to call cache_clear() without touching the private name
get_compiled_rules = _compile_rules


async def scan_command(command_parts: list[str]) -> list[dict]:
    """Scan a tokenized command against compiled YARA rules.

    Offloads the C-heap match to the default thread-pool executor so the event
    loop is never blocked by YARA string matching.

    Returns a list of serializable match dicts: {rule, namespace, tags}.
    """
    rules = _compile_rules()
    if rules is None:
        return []

    loop = asyncio.get_running_loop()
    data = " ".join(command_parts).encode()

    try:
        matches = await loop.run_in_executor(None, lambda: rules.match(data=data))
        return [
            {
                "rule": m.rule,
                "namespace": m.namespace,
                "tags": list(m.tags),
            }
            for m in matches
        ]
    except Exception as exc:
        logger.warning(f"yara_analyzer: scan error — {exc}")
        return []
