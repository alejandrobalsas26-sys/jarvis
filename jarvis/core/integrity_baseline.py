"""
core/integrity_baseline.py — JARVIS self-integrity verification (v34.0).

Establishes SHA-256 baseline of all JARVIS .py files on first run.
On every subsequent boot: re-hashes and compares.
Modified files → CRITICAL alert in AURA.
New files not in baseline → WARNING alert.
Baseline stored in core/integrity_baseline.json (gitignored).
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_JARVIS_ROOT     = Path(__file__).parent.parent
_BASELINE_PATH   = Path(__file__).parent / "integrity_baseline.json"
_SCAN_EXTENSIONS = {".py"}
_EXCLUDE_DIRS    = {"__pycache__", ".git", "brain", "logs", "static"}


def _hash_file(path: Path) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return "ERROR"


def _scan_jarvis_files() -> dict[str, str]:
    """Scan all JARVIS .py files and return {rel_path: sha256}."""
    hashes: dict[str, str] = {}
    for path in _JARVIS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _SCAN_EXTENSIONS:
            continue
        if any(exc in path.parts for exc in _EXCLUDE_DIRS):
            continue
        rel = str(path.relative_to(_JARVIS_ROOT))
        hashes[rel] = _hash_file(path)
    return hashes


def establish_baseline() -> dict:
    """
    Create integrity baseline. Call once to initialize.
    Subsequent boots use verify_integrity() instead.
    """
    hashes = _scan_jarvis_files()
    baseline = {
        "created":    datetime.now(timezone.utc).isoformat(),
        "file_count": len(hashes),
        "hashes":     hashes,
    }
    _BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True),
        encoding="utf-8"
    )
    logger.info(
        f"INTEGRITY: baseline established — "
        f"{len(hashes)} files hashed → {_BASELINE_PATH.name}"
    )
    return baseline


def verify_integrity() -> dict:
    """
    Compare current file hashes against baseline.
    Returns dict with modified/added/missing lists.
    """
    if not _BASELINE_PATH.exists():
        logger.info("INTEGRITY: no baseline found — establishing now")
        establish_baseline()
        return {"status": "baseline_created", "modified": [],
                "added": [], "missing": [], "checked": 0,
                "timestamp": datetime.now(timezone.utc).isoformat()}

    try:
        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"INTEGRITY: baseline unreadable ({e}) — re-establishing")
        establish_baseline()
        return {"status": "baseline_recreated", "modified": [],
                "added": [], "missing": [], "checked": 0,
                "timestamp": datetime.now(timezone.utc).isoformat()}

    stored  = baseline.get("hashes", {})
    current = _scan_jarvis_files()

    modified: list[str] = []
    added:    list[str] = []
    missing:  list[str] = []

    for path, current_hash in current.items():
        if path not in stored:
            added.append(path)
        elif stored[path] != current_hash:
            modified.append(path)

    for path in stored:
        if path not in current:
            missing.append(path)

    result = {
        "status":    "clean" if not (modified or added or missing) else "ALERT",
        "modified":  modified,
        "added":     added,
        "missing":   missing,
        "checked":   len(current),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if modified:
        logger.error(
            f"INTEGRITY: {len(modified)} files MODIFIED since baseline: "
            f"{modified[:3]}"
        )
    if added and len(added) > 2:
        logger.warning(f"INTEGRITY: {len(added)} new files since baseline")
    if missing:
        logger.warning(f"INTEGRITY: {len(missing)} files removed since baseline")
    if result["status"] == "clean":
        logger.info(
            f"INTEGRITY: all {len(current)} JARVIS files verified clean"
        )

    return result


async def run_integrity_check(broadcast_fn) -> dict:
    """Run integrity check and broadcast result to AURA."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, verify_integrity)

    severity = (
        "CRITICAL" if result.get("modified") else
        "WARNING"  if result.get("added") or result.get("missing") else
        "INFO"
    )

    try:
        await broadcast_fn({
            "type":     "integrity_check",
            "severity": severity,
            **result,
        })
    except Exception as e:
        logger.debug(f"INTEGRITY: broadcast failed: {e}")

    return result
