"""
core/session_manager.py — Conversation session persistence (v30.0).

Saves the last N turns to disk after each exchange.
On boot: detects a recent session file and offers to resume.
Survives restarts, power cuts, and crashes.
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger

from core.memory_router import redact_secrets

SESSION_DIR   = Path("logs/sessions")
MAX_TURNS     = 20           # turns to persist
RESUME_WINDOW = 3600         # sessions older than this (seconds) are not offered

SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _redact_turn(turn: dict) -> dict:
    """Return *turn* with its content field secret-redacted (V62.0 Phase 3).

    This crash-resume snapshot writes unconditionally every turn — unlike the
    episodic-memory write path, it previously had no secret-redaction gate at
    all, so a credential typed or returned mid-conversation persisted to disk
    in plaintext on every turn until the session file was overwritten.
    """
    content = turn.get("content")
    if isinstance(content, str) and content:
        return {**turn, "content": redact_secrets(content)}
    return turn


def save_session(history: list[dict], session_id: str = "default") -> None:
    """Persist the current conversation history to disk."""
    path = SESSION_DIR / f"{session_id}.json"
    try:
        payload = {
            "session_id": session_id,
            "saved_at":   datetime.now(timezone.utc).isoformat(),
            "timestamp":  time.time(),
            "turns":      [_redact_turn(t) for t in history[-MAX_TURNS:]],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                   default=str),
                        encoding="utf-8")
    except Exception as e:
        logger.debug(f"SESSION: save failed: {e}")


def load_session(session_id: str = "default") -> list[dict] | None:
    """
    Load a persisted session. Returns history list or None if
    no session found or session is too old.
    """
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age  = time.time() - data.get("timestamp", 0)
        if age > RESUME_WINDOW:
            return None
        turns = data.get("turns", [])
        if turns:
            saved_at = data.get("saved_at", "unknown")
            logger.info(
                f"SESSION: found recent session ({len(turns)} turns, "
                f"saved {saved_at}) — call offer_resume() to restore"
            )
        return turns or None
    except Exception as e:
        logger.debug(f"SESSION: load failed: {e}")
        return None


def offer_resume(turns: list[dict]) -> str:
    """
    Return a system message prepended to the next LLM call
    that injects the last session's context.
    """
    summary = "\n".join(
        f"[{t.get('role', '?').upper()}]: {str(t.get('content',''))[:200]}"
        for t in turns[-5:]
    )
    return (
        f"[SESSION RESUMED] The operator's previous session context "
        f"(last {len(turns)} turns):\n{summary}\n---\n"
    )


def delete_session(session_id: str = "default") -> None:
    try:
        (SESSION_DIR / f"{session_id}.json").unlink(missing_ok=True)
    except Exception:
        pass
