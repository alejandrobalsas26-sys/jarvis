"""
core/task_memory.py — V58.0 COGNITIVE CORE persistent task memory.

Lightweight append-only JSONL store of completed cognitive tasks under
jarvis/logs/task_memory.jsonl. No vector DB: similarity is a cheap token-overlap
heuristic. Writes are guarded by a process-local lock plus best-effort Windows
file locking (msvcrt) so concurrent appends do not interleave partial lines.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

try:  # Windows advisory file locking — best effort only.
    import msvcrt  # type: ignore
except Exception:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "task_memory.jsonl"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 2}


class TaskMemory:
    """Append-only JSONL memory of cognitive task outcomes."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ── Write ─────────────────────────────────────────────────────────────────

    def record_task(self, plan, traces=None, reflection=None) -> None:
        """Append one task record (plan + traces + reflection) as a JSONL line."""
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "plan": plan.to_dict() if hasattr(plan, "to_dict") else plan,
            "traces": [
                t.to_dict() if hasattr(t, "to_dict") else t
                for t in (traces or [])
            ],
            "reflection": (
                reflection.to_dict() if hasattr(reflection, "to_dict") else reflection
            ),
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    if msvcrt is not None:
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        except Exception:
                            pass
                    fh.write(line + "\n")
                    fh.flush()
                    if msvcrt is not None:
                        try:
                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
            except Exception:
                # Memory is best-effort; never crash the agentic path on I/O error.
                pass

    # ── Read ──────────────────────────────────────────────────────────────────

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue  # tolerate a partially-written tail line
        except Exception:
            return out
        return out

    def recent_tasks(self, limit: int = 20) -> list[dict]:
        """Return the most recently recorded tasks (newest first)."""
        records = self._read_all()
        return list(reversed(records[-limit:]))

    def find_similar_tasks(self, query: str, limit: int = 5) -> list[dict]:
        """Token-overlap search over recorded objectives (cheap, no embeddings)."""
        q = _tokens(query)
        if not q:
            return []
        scored: list[tuple[float, dict]] = []
        for rec in self._read_all():
            plan = rec.get("plan", {}) or {}
            objective = str(plan.get("objective", ""))
            overlap = _tokens(objective) & q
            if overlap:
                score = len(overlap) / len(q)
                scored.append((score, rec))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [rec for _, rec in scored[:limit]]
