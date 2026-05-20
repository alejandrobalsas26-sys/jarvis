"""
core/governance.py — Forensic Audit Logger for AI Governance (Jarvis v14.0).

Thread-safe, non-blocking JSONL writer. Uses a background queue so the main
asyncio event loop is never stalled by disk I/O.

Schema: timestamp, tool, command, resolved_path, binary_status,
        auth_audit (OTP details), thinking, result.
"""

import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "tactic_audit.jsonl"


class TacticAuditLogger:
    def __init__(self) -> None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._drain, daemon=True, name="audit-logger"
        )
        self._worker.start()
        logger.info(f"TacticAuditLogger: activo → {_LOG_FILE}")

    def _drain(self) -> None:
        """Background worker: consumes records from the queue and appends to JSONL."""
        while True:
            record = self._queue.get()
            if record is None:
                break
            try:
                with _LOG_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"TacticAuditLogger write error: {e}")
            finally:
                self._queue.task_done()

    def log_action(
        self,
        tool_name: str,
        reasoning: str,
        auth_audit: str,
        status: str,
        result: str,
        command: str = "",
        resolved_path: str = "",
        binary_status: str = "",
    ) -> None:
        """Enqueue an audit record (non-blocking).

        Args:
            tool_name: Name of the tool invoked.
            reasoning: [THINKING] block captured from the LLM turn.
            auth_audit: Detail string, e.g. "vocal:nato:Bravo:0.94:granted".
            status: "success" | "error" | "blocked".
            result: First 200 chars of the tool output.
            command: Raw command string (shell tools only).
            resolved_path: Canonicalized path from Layer 2 check.
            binary_status: "allowlist_ok" | "blocked" | "" (shell tools only).
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user": "Alejandro",
            "tool": tool_name,
            "command": command,
            "resolved_path": resolved_path,
            "binary_status": binary_status,
            "auth_audit": auth_audit,
            "thinking": reasoning,
            "result": result[:200] if result else "",
        }
        self._queue.put_nowait(record)

    @property
    def log_path(self) -> Path:
        return _LOG_FILE

    def is_writable(self) -> bool:
        """Check that the audit log file is accessible for writing."""
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _LOG_FILE.open("a", encoding="utf-8"):
                pass
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Flush the queue and stop the background worker."""
        self._queue.put(None)
        self._worker.join(timeout=5.0)
