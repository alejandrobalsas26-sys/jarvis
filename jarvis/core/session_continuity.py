"""core/session_continuity.py — V69 M60.1: the crash-safe session journal.

WHAT WAS ACTUALLY BROKEN
------------------------
``core.session_manager`` was the only conversation persistence, and it had three
faults that a power cut turns into data loss or, worse, into a LIE:

  1. ``path.write_text(...)`` truncates the file in place. A crash between truncate
     and flush leaves a zero-length or half-written JSON — the whole session gone.
  2. It wrote ``history`` unconditionally after each completed turn, so a turn that
     was interrupted mid-stream was either absent or indistinguishable from a
     completed one. On restore the operator would be shown a partial answer as fact.
  3. There was no notion of a RUN, so nothing could tell a clean exit from a crash.

This module is the durable replacement. It does NOT introduce a second database
framework: it instantiates the proven :class:`core.operational_store.OperationalStore`
(SQLite + WAL, schema-versioned, idempotent ``put``, content-hash dedup, retention,
corrupt-row isolation) against its OWN managed file, so session state can be backed
up, exported and pruned independently of the operational domains.

THREE ENTITIES
--------------
  SESSION   identity + fingerprints + state. One per conversation.
  TURN      one user or assistant turn: sequence, timestamps, terminal state,
            response contract, continuation state, tool references and — subject to
            the persistence mode — its VISIBLE content after deterministic redaction.
  RUN       one process lifetime: start, runtime/Git version, clean-shutdown marker,
            lifecycle terminal state, last checkpoint, recovery result.

WHAT IS DELIBERATELY NOT PERSISTED
----------------------------------
Readiness, model warmth, loaded-model claims, live connections, active locks, active
tasks, queue ownership, pending TTS, raw model streams, hidden reasoning, OTPs,
credentials and raw tool arguments. Those are RE-MEASURED at boot; a journal that
restored them would be asserting something it cannot know.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from loguru import logger

from core.managed_paths import continuity_db_path
from core.redaction_policy import redact_text

JOURNAL_SCHEMA_VERSION = 1

# ── Retention bounds (M60.3.2). Deterministic; never grow with uptime. ────────
MAX_SESSIONS = 20
MAX_TURNS_PER_SESSION = 200
MAX_VISIBLE_CHARS = 4000
MAX_JOURNAL_ROWS = 5000
MAX_TOOL_OPS = 500
MAX_RECOVERY_WARNINGS = 20

# Bounds on a single journal write. The target is NVMe; 25 ms is the M60 hard
# warning threshold, 2 s is the give-up deadline for the async wrapper.
SLOW_WRITE_MS = 25.0
WRITE_TIMEOUT_S = 2.0

# Store domains.
D_SESSION = "continuity_sessions"
D_TURN = "continuity_turns"
D_RUN = "continuity_runs"
D_TOOL_OP = "continuity_tool_ops"
J_EVENT = "continuity_events"

_META_ACTIVE_RUN = "active_run"
_META_ACTIVE_SESSION = "active_session"


class PersistenceMode(str, Enum):
    """How much of a conversation may reach the disk. NEVER silently upgraded."""

    OFF = "OFF"
    METADATA_ONLY = "METADATA_ONLY"
    LOCAL_REDACTED = "LOCAL_REDACTED"
    LOCAL_FULL_EXPLICIT = "LOCAL_FULL_EXPLICIT"


DEFAULT_PERSISTENCE_MODE = PersistenceMode.LOCAL_REDACTED
# Modes that store any conversation content at all.
_CONTENT_MODES = frozenset({PersistenceMode.LOCAL_REDACTED,
                            PersistenceMode.LOCAL_FULL_EXPLICIT})


def parse_persistence_mode(value) -> PersistenceMode:
    """Coerce a configured value into a mode. An UNKNOWN value falls back to the
    default — it is never interpreted as the most permissive mode."""
    if isinstance(value, PersistenceMode):
        return value
    try:
        return PersistenceMode(str(value).strip().upper())
    except (ValueError, AttributeError):
        return DEFAULT_PERSISTENCE_MODE


class SessionState(str, Enum):
    ACTIVE = "ACTIVE"
    CHECKPOINTED = "CHECKPOINTED"
    CLOSED = "CLOSED"
    INTERRUPTED = "INTERRUPTED"
    FORGOTTEN = "FORGOTTEN"


class RunOutcome(str, Enum):
    RUNNING = "RUNNING"
    CLEAN_SHUTDOWN = "CLEAN_SHUTDOWN"
    UNCLEAN = "UNCLEAN"


class JournalState(str, Enum):
    """Truthful durability posture. A degraded journal NEVER claims success."""

    DISABLED = "DISABLED"          # persistence mode OFF
    OK = "OK"
    DEGRADED = "DEGRADED"          # writes failing / store volatile
    VOLATILE = "VOLATILE"          # in-memory store (no crash survival)
    FAILED = "FAILED"              # unusable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:32]


def id_hash(value: str) -> str:
    """A content-free 12-hex digest of an identifier — what runtime health exposes
    instead of the raw session id."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:12]


def read_git_commit() -> str:
    """The current Git commit, read from ``.git`` WITHOUT a subprocess.

    A diagnostics/backup manifest must be able to say which code produced it, but
    M60 may not shell out on the hot path. Returns "" when unavailable.
    """
    try:
        root = Path(__file__).resolve().parent.parent.parent
        head = root / ".git" / "HEAD"
        if not head.is_file():
            return ""
        raw = head.read_text(encoding="utf-8", errors="replace").strip()
        if raw.startswith("ref:"):
            ref = raw.split(":", 1)[1].strip()
            ref_file = root / ".git" / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8",
                                          errors="replace").strip()[:40]
            packed = root / ".git" / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8",
                                             errors="replace").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0].strip()[:40]
            return ""
        return raw[:40]
    except OSError:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
#  Records
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SessionRecord:
    session_id: str
    created_at: str = ""
    last_active_at: str = ""
    language: str = "es"
    response_profile: str = "AUTO"
    authority_fingerprint: str = ""
    scope_fingerprint: str = ""
    security_policy_version: str = ""
    prompt_fingerprint: str = ""
    tool_schema_fingerprint: str = ""
    state: str = SessionState.ACTIVE.value
    persistence_mode: str = DEFAULT_PERSISTENCE_MODE.value
    turn_count: int = 0
    run_id: str = ""
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        known = {k: v for k, v in (d or {}).items() if k in cls.__annotations__}
        known.setdefault("session_id", "")
        return cls(**known)


@dataclass
class TurnRecord:
    turn_id: str
    session_id: str
    sequence: int
    role: str                            # "user" | "assistant"
    started_at: str = ""
    finalized_at: str | None = None
    terminal_state: str = "ACTIVE"
    content: str = ""                    # visible content, already redacted
    content_chars: int = 0               # chars actually shown to the operator
    content_hash: str = ""
    content_persisted: bool = False      # False in OFF / METADATA_ONLY
    response_contract: str = ""
    continuation_state: str = ""
    tool_refs: list[str] = field(default_factory=list)
    redactions: int = 0
    schema_version: int = JOURNAL_SCHEMA_VERSION

    @property
    def finalized(self) -> bool:
        return self.finalized_at is not None

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "TurnRecord":
        known = {k: v for k, v in (d or {}).items() if k in cls.__annotations__}
        known.setdefault("turn_id", "")
        known.setdefault("session_id", "")
        known.setdefault("sequence", 0)
        known.setdefault("role", "assistant")
        return cls(**known)


@dataclass
class RunRecord:
    run_id: str
    started_at: str = ""
    ended_at: str | None = None
    clean_shutdown: bool = False
    runtime_version: str = ""
    git_commit: str = ""
    lifecycle_terminal_state: str = ""
    last_checkpoint_at: str | None = None
    recovery_result: str = ""
    pid: int = 0                          # advisory ONLY — never proof of liveness
    boot_token: str = ""                  # distinguishes reused PIDs across boots
    schema_version: int = JOURNAL_SCHEMA_VERSION

    @property
    def outcome(self) -> RunOutcome:
        if self.ended_at is None:
            return RunOutcome.RUNNING
        return RunOutcome.CLEAN_SHUTDOWN if self.clean_shutdown else RunOutcome.UNCLEAN

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        known = {k: v for k, v in (d or {}).items() if k in cls.__annotations__}
        known.setdefault("run_id", "")
        return cls(**known)


@dataclass
class ToolOpRecord:
    """A tool INTENT + outcome reference. Never the raw arguments.

    ``argument_digest`` is a hash, not a payload: it lets recovery say "the same
    action was attempted twice" without ever storing what the action's parameters
    were. ``audit_ref`` points at the existing ``TacticAuditLogger`` line.
    """

    op_id: str
    session_id: str
    turn_id: str
    tool_name: str
    effectful: bool = False
    started_at: str = ""
    finalized_at: str | None = None
    outcome: str = "IN_FLIGHT"
    summary: str = ""                    # redacted, bounded
    argument_digest: str = ""
    audit_ref: str = ""
    review_required: bool = False
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "ToolOpRecord":
        known = {k: v for k, v in (d or {}).items() if k in cls.__annotations__}
        known.setdefault("op_id", "")
        known.setdefault("session_id", "")
        known.setdefault("turn_id", "")
        known.setdefault("tool_name", "")
        return cls(**known)


@dataclass
class WriteResult:
    """The truthful outcome of ONE journal write. ``ok=False`` is never hidden."""

    ok: bool
    duration_ms: float = 0.0
    reason: str = ""
    deduplicated: bool = False

    def snapshot(self) -> dict:
        return {"ok": self.ok, "duration_ms": self.duration_ms,
                "reason": self.reason, "deduplicated": self.deduplicated}


# ══════════════════════════════════════════════════════════════════════════════
#  The journal
# ══════════════════════════════════════════════════════════════════════════════
class SessionJournal:
    """Bounded, versioned, crash-safe session persistence.

    Every public write returns a :class:`WriteResult`; a failure increments a counter
    and degrades :meth:`journal_state`, and NEVER raises into the caller's turn. The
    runtime stays usable without persistence — a lost journal costs continuity, not
    the ability to answer.
    """

    # ``perf_counter``, NOT ``monotonic``: on Windows monotonic ticks at ~15.6 ms, so
    # a sub-millisecond SQLite write measures as either 0.0 or 16.0 and the M60
    # thresholds (10 ms preferred / 25 ms warning) would be meaningless noise.
    def __init__(self, *, store=None, mode=None, clock=time.perf_counter,
                 path: "str | Path | None" = None,
                 max_sessions: int = MAX_SESSIONS,
                 max_turns: int = MAX_TURNS_PER_SESSION,
                 max_visible_chars: int = MAX_VISIBLE_CHARS) -> None:
        self.mode = parse_persistence_mode(
            mode if mode is not None else DEFAULT_PERSISTENCE_MODE)
        self.clock = clock
        self.max_sessions = max(1, int(max_sessions))
        self.max_turns = max(1, int(max_turns))
        self.max_visible_chars = max(0, int(max_visible_chars))
        self._writes = 0
        self._write_failures = 0
        # A read that FAILED is not the same as a read that found nothing. Recovery
        # must be able to tell those apart, or an unreadable journal would be
        # reported as "clean, nothing to recover" — the exact fabrication M60 forbids.
        self._read_failures = 0
        self._slow_writes = 0
        self._last_write_ms: float | None = None
        self._last_checkpoint_ms: float | None = None
        self._checkpoint_failures = 0
        self._pruned_sessions = 0
        self._pruned_turns = 0
        self._redactions = 0
        self._corrupt_records = 0
        self._store = store
        self._store_path = path
        self._store_error = ""
        self.active_run: RunRecord | None = None
        self.active_session: SessionRecord | None = None

    # ── store access (lazy; a failure degrades, never raises) ─────────────────
    @property
    def store(self):
        if self._store is None:
            from core.operational_store import OperationalStore
            try:
                self._store = OperationalStore(
                    self._store_path or continuity_db_path())
            except Exception as exc:  # noqa: BLE001 — persistence never blocks boot
                self._store_error = f"{type(exc).__name__}: {str(exc)[:60]}"
                self._store = OperationalStore(":memory:")
        return self._store

    @property
    def enabled(self) -> bool:
        return self.mode is not PersistenceMode.OFF

    @property
    def persists_content(self) -> bool:
        return self.mode in _CONTENT_MODES

    def journal_state(self) -> JournalState:
        if not self.enabled:
            return JournalState.DISABLED
        if self._store is None:
            return JournalState.OK          # not opened yet — no failure observed
        if not getattr(self._store, "durable", True):
            return JournalState.VOLATILE
        if self._write_failures and self._writes == 0:
            return JournalState.FAILED
        if self._write_failures:
            return JournalState.DEGRADED
        return JournalState.OK

    # ── the single guarded write primitive ───────────────────────────────────
    def _put(self, domain: str, entity_id: str, payload: dict) -> WriteResult:
        """One atomic, idempotent, content-hash-deduplicated record write.

        SQLite in autocommit mode makes each statement its own transaction, so a
        crash mid-``_put`` leaves the PREVIOUS row intact — never a half-written one.
        """
        if not self.enabled:
            return WriteResult(True, 0.0, "persistence_off")
        t0 = self.clock()
        try:
            res = self.store.put(domain, entity_id, payload)
        except Exception as exc:  # noqa: BLE001
            self._write_failures += 1
            reason = f"{type(exc).__name__}"
            logger.warning(f"CONTINUITY: journal write failed ({reason}) — "
                           f"persistence DEGRADED, runtime unaffected")
            return WriteResult(False, round((self.clock() - t0) * 1000.0, 2), reason)
        ms = round((self.clock() - t0) * 1000.0, 2)
        self._writes += 1
        self._last_write_ms = ms
        if ms > SLOW_WRITE_MS:
            self._slow_writes += 1
        return WriteResult(True, ms, "", deduplicated=not res.written)

    def write_record(self, domain: str, entity_id: str, payload: dict) -> WriteResult:
        """Public guarded write, for the reconciler and the backup/restore paths.

        Deliberately narrow: the domain is one of this module's own constants and the
        payload is an already-built record dict — no caller supplies a table name from
        free text.
        """
        if domain not in (D_SESSION, D_TURN, D_RUN, D_TOOL_OP):
            return WriteResult(False, 0.0, "unknown_domain")
        return self._put(domain, entity_id, payload)

    async def _put_async(self, domain: str, entity_id: str,
                         payload: dict) -> WriteResult:
        """The off-loop variant: bounded ``to_thread`` with an explicit deadline.

        Journal I/O must never sit on the event loop (the 15 W target is CPU-bound and
        a stalled loop is a stalled answer), and it must never hang a turn — a write
        that misses the deadline is reported as a failure, not awaited forever.
        """
        if not self.enabled:
            return WriteResult(True, 0.0, "persistence_off")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._put, domain, entity_id, payload),
                timeout=WRITE_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._write_failures += 1
            return WriteResult(False, WRITE_TIMEOUT_S * 1000.0, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._write_failures += 1
            return WriteResult(False, 0.0, type(exc).__name__)

    # ── RUN ──────────────────────────────────────────────────────────────────
    def begin_run(self, *, runtime_version: str = "", git_commit: str | None = None,
                  now_iso: str | None = None) -> RunRecord:
        """Open a RUN record and publish it as the active run.

        The active-run pointer is what makes an unclean exit detectable: the next
        boot finds a run whose ``ended_at`` is None and whose boot token differs.
        """
        run = RunRecord(
            run_id=_new_id("run"), started_at=now_iso or _now_iso(),
            runtime_version=str(runtime_version or "")[:40],
            git_commit=(git_commit if git_commit is not None else read_git_commit()),
            pid=os.getpid(), boot_token=uuid.uuid4().hex[:16],
        )
        self.active_run = run
        self._put(D_RUN, run.run_id, run.to_dict())
        try:
            self.store.checkpoint(_META_ACTIVE_RUN, run.run_id)
        except Exception:  # noqa: BLE001
            self._write_failures += 1
        return run

    def checkpoint_run(self, *, now_iso: str | None = None) -> WriteResult:
        """Stamp a last-checkpoint time on the active run. Does NOT wait for Ollama,
        TTS or any model work — it is pure local state."""
        run = self.active_run
        if run is None:
            return WriteResult(False, 0.0, "no_active_run")
        run.last_checkpoint_at = now_iso or _now_iso()
        res = self._put(D_RUN, run.run_id, run.to_dict())
        if res.ok:
            self._last_checkpoint_ms = res.duration_ms
        else:
            self._checkpoint_failures += 1
        return res

    def finalize_run(self, *, clean: bool = True, lifecycle_state: str = "",
                     recovery_result: str = "",
                     now_iso: str | None = None) -> WriteResult:
        """Write the clean-shutdown marker. Its ABSENCE is what a crash looks like."""
        run = self.active_run
        if run is None:
            return WriteResult(False, 0.0, "no_active_run")
        run.ended_at = now_iso or _now_iso()
        run.clean_shutdown = bool(clean)
        run.lifecycle_terminal_state = str(lifecycle_state or "")[:32]
        if recovery_result:
            run.recovery_result = str(recovery_result)[:120]
        return self._put(D_RUN, run.run_id, run.to_dict())

    def runs(self, *, limit: int = 20) -> list[RunRecord]:
        out = [RunRecord.from_dict(r) for r in self._safe_all(D_RUN)]
        out.sort(key=lambda r: r.started_at, reverse=True)
        return out[:max(1, limit)]

    def previous_run(self) -> RunRecord | None:
        """The most recent run that is NOT the active one."""
        active = self.active_run.run_id if self.active_run else None
        for r in self.runs(limit=self.max_sessions + 4):
            if r.run_id != active:
                return r
        return None

    # ── SESSION ──────────────────────────────────────────────────────────────
    def begin_session(self, *, language: str = "es", response_profile: str = "AUTO",
                      authority_fingerprint: str = "", scope_fingerprint: str = "",
                      security_policy_version: str = "", prompt_fingerprint: str = "",
                      tool_schema_fingerprint: str = "",
                      session_id: str | None = None,
                      now_iso: str | None = None) -> SessionRecord:
        ts = now_iso or _now_iso()
        sess = SessionRecord(
            session_id=session_id or _new_id("sess"), created_at=ts, last_active_at=ts,
            language=str(language or "es")[:8],
            response_profile=str(response_profile or "AUTO")[:16],
            authority_fingerprint=str(authority_fingerprint or "")[:32],
            scope_fingerprint=str(scope_fingerprint or "")[:32],
            security_policy_version=str(security_policy_version or "")[:32],
            prompt_fingerprint=str(prompt_fingerprint or "")[:32],
            tool_schema_fingerprint=str(tool_schema_fingerprint or "")[:32],
            persistence_mode=self.mode.value,
            run_id=self.active_run.run_id if self.active_run else "",
        )
        self.active_session = sess
        self._put(D_SESSION, sess.session_id, sess.to_dict())
        try:
            self.store.checkpoint(_META_ACTIVE_SESSION, sess.session_id)
        except Exception:  # noqa: BLE001
            self._write_failures += 1
        self.prune()
        return sess

    def touch_session(self, *, now_iso: str | None = None) -> WriteResult:
        sess = self.active_session
        if sess is None:
            return WriteResult(False, 0.0, "no_active_session")
        sess.last_active_at = now_iso or _now_iso()
        return self._put(D_SESSION, sess.session_id, sess.to_dict())

    def close_session(self, *, state: SessionState = SessionState.CLOSED,
                      now_iso: str | None = None) -> WriteResult:
        sess = self.active_session
        if sess is None:
            return WriteResult(False, 0.0, "no_active_session")
        sess.state = state.value
        sess.last_active_at = now_iso or _now_iso()
        return self._put(D_SESSION, sess.session_id, sess.to_dict())

    def sessions(self) -> list[SessionRecord]:
        out = [SessionRecord.from_dict(r) for r in self._safe_all(D_SESSION)]
        out.sort(key=lambda s: s.last_active_at or s.created_at, reverse=True)
        return out

    def get_session(self, session_id: str) -> SessionRecord | None:
        rec = self._safe_get(D_SESSION, session_id)
        return SessionRecord.from_dict(rec) if rec else None

    def last_session(self, *, exclude_active: bool = True) -> SessionRecord | None:
        active = self.active_session.session_id if self.active_session else None
        for s in self.sessions():
            if exclude_active and s.session_id == active:
                continue
            if s.state == SessionState.FORGOTTEN.value:
                continue
            return s
        return None

    # ── TURN ─────────────────────────────────────────────────────────────────
    def _turn_key(self, session_id: str, sequence: int) -> str:
        # Zero-padded so the store's ORDER BY entity_id is sequence order.
        return f"{session_id}#{int(sequence):06d}"

    def open_turn(self, *, role: str, sequence: int | None = None,
                  response_contract: str = "",
                  session_id: str | None = None,
                  now_iso: str | None = None) -> TurnRecord:
        """Open a turn in the ACTIVE terminal state.

        This write is the crash anchor: if the process dies now, recovery finds a turn
        with ``finalized_at is None`` and reconciles it as interrupted rather than
        silently dropping it or presenting it as complete.
        """
        sid = session_id or (self.active_session.session_id
                             if self.active_session else "orphan")
        seq = sequence if sequence is not None else self.next_sequence(sid)
        turn = TurnRecord(
            turn_id=self._turn_key(sid, seq), session_id=sid, sequence=int(seq),
            role=str(role or "assistant")[:16], started_at=now_iso or _now_iso(),
            response_contract=str(response_contract or "")[:32],
        )
        self._put(D_TURN, turn.turn_id, turn.to_dict())
        return turn

    def next_sequence(self, session_id: str) -> int:
        existing = self.turns(session_id)
        return (existing[-1].sequence + 1) if existing else 1

    def _apply_content(self, turn: TurnRecord, content: str | None,
                       *, visible_chars: int | None = None) -> None:
        """Set a turn's content according to the persistence mode + redaction policy.

        ``content_chars`` is recorded in EVERY mode (it is a count, not content) so a
        METADATA_ONLY journal can still say "the operator saw 412 characters" without
        storing any of them.
        """
        raw = content or ""
        turn.content_chars = int(visible_chars if visible_chars is not None
                                 else len(raw))
        turn.content_hash = _hash_text(raw) if raw else ""
        if not self.persists_content or not raw:
            turn.content = ""
            turn.content_persisted = False
            return
        # LOCAL_FULL_EXPLICIT still strips hidden reasoning, OTPs and secrets — the
        # mode widens what conversation text is kept, never what secrets are kept.
        redacted, report = redact_text(raw, max_chars=self.max_visible_chars,
                                       strip_home=True)
        turn.content = redacted
        turn.content_persisted = True
        turn.redactions = report.total
        self._redactions += report.total

    def finalize_turn(self, turn: TurnRecord, *, terminal_state: str,
                      content: str | None = None, visible_chars: int | None = None,
                      continuation_state: str = "", tool_refs=None,
                      now_iso: str | None = None) -> WriteResult:
        """Close a turn with a truthful terminal state and its VISIBLE content only."""
        turn.terminal_state = str(terminal_state or "COMPLETED")[:40]
        turn.finalized_at = now_iso or _now_iso()
        turn.continuation_state = str(continuation_state or "")[:32]
        if tool_refs:
            turn.tool_refs = [str(r)[:64] for r in list(tool_refs)[:16]]
        self._apply_content(turn, content, visible_chars=visible_chars)
        res = self._put(D_TURN, turn.turn_id, turn.to_dict())
        if res.ok and self.active_session is not None \
                and turn.session_id == self.active_session.session_id:
            self.active_session.turn_count = max(self.active_session.turn_count,
                                                 turn.sequence)
            self.touch_session(now_iso=turn.finalized_at)
            self.prune_turns(turn.session_id)
        return res

    async def finalize_turn_async(self, turn: TurnRecord, *, terminal_state: str,
                                  content: str | None = None,
                                  visible_chars: int | None = None,
                                  continuation_state: str = "",
                                  tool_refs=None) -> WriteResult:
        """Off-loop finalize — the hot-path variant used by the live turn pipeline."""
        turn.terminal_state = str(terminal_state or "COMPLETED")[:40]
        turn.finalized_at = _now_iso()
        turn.continuation_state = str(continuation_state or "")[:32]
        if tool_refs:
            turn.tool_refs = [str(r)[:64] for r in list(tool_refs)[:16]]
        self._apply_content(turn, content, visible_chars=visible_chars)
        return await self._put_async(D_TURN, turn.turn_id, turn.to_dict())

    def record_visible_progress(self, turn: TurnRecord, *, visible_chars: int,
                                content: str | None = None) -> WriteResult:
        """Persist how much the operator has ACTUALLY seen, mid-stream.

        Called at bounded checkpoints (sentence flush), not per token. It is what lets
        recovery say PARTIAL_VISIBLE_RESPONSE truthfully instead of guessing.
        """
        turn.content_chars = max(0, int(visible_chars))
        if content is not None:
            self._apply_content(turn, content, visible_chars=turn.content_chars)
        return self._put(D_TURN, turn.turn_id, turn.to_dict())

    def finalize_stale_active_turns(self, *, terminal_state: str,
                                    session_id: str | None = None) -> int:
        """Close any still-ACTIVE turn of a session with a truthful in-process state.

        Without this, a turn that failed or was cancelled somewhere the inference
        finalizer does not cover would stay unfinalized on disk, and the NEXT boot
        would report a crash that never happened. Called when a new turn replaces it
        and at clean shutdown — the two moments where "still active" is known to be
        false. It never invents content: only the terminal state changes.
        """
        sid = session_id or (self.active_session.session_id
                             if self.active_session else None)
        if sid is None or not self.enabled:
            return 0
        closed = 0
        for turn in self.turns(sid):
            if turn.finalized:
                continue
            turn.terminal_state = str(terminal_state)[:40]
            turn.finalized_at = _now_iso()
            if self._put(D_TURN, turn.turn_id, turn.to_dict()).ok:
                closed += 1
        return closed

    def turns(self, session_id: str) -> list[TurnRecord]:
        out: list[TurnRecord] = []
        prefix = f"{session_id}#"
        for rec in self._safe_all(D_TURN):
            tid = str(rec.get("turn_id", ""))
            if tid.startswith(prefix):
                out.append(TurnRecord.from_dict(rec))
        out.sort(key=lambda t: t.sequence)
        return out

    def unfinished_turns(self, session_id: str | None = None) -> list[TurnRecord]:
        """Every turn with no ``finalized_at`` — the crash signature."""
        out: list[TurnRecord] = []
        for rec in self._safe_all(D_TURN):
            t = TurnRecord.from_dict(rec)
            if session_id is not None and t.session_id != session_id:
                continue
            if not t.finalized:
                out.append(t)
        out.sort(key=lambda t: (t.session_id, t.sequence))
        return out

    # ── TOOL OPERATIONS ──────────────────────────────────────────────────────
    def open_tool_op(self, *, tool_name: str, effectful: bool,
                     turn_id: str = "", arguments=None, audit_ref: str = "",
                     session_id: str | None = None,
                     now_iso: str | None = None) -> ToolOpRecord:
        """Record the INTENT to run a tool, before it runs.

        The arguments are hashed, never stored. That is what allows recovery to say
        "an effectful action was in flight and its outcome is UNKNOWN" without ever
        putting a target, a path or a credential on disk.
        """
        op = ToolOpRecord(
            op_id=_new_id("op"),
            session_id=session_id or (self.active_session.session_id
                                      if self.active_session else "orphan"),
            turn_id=str(turn_id or "")[:64], tool_name=str(tool_name or "")[:64],
            effectful=bool(effectful), started_at=now_iso or _now_iso(),
            argument_digest=_hash_text(repr(arguments)) if arguments else "",
            audit_ref=str(audit_ref or "")[:64],
        )
        self._put(D_TOOL_OP, op.op_id, op.to_dict())
        return op

    def finalize_tool_op(self, op: ToolOpRecord, *, outcome: str, summary: str = "",
                         review_required: bool = False,
                         now_iso: str | None = None) -> WriteResult:
        op.outcome = str(outcome or "UNKNOWN")[:40]
        op.finalized_at = now_iso or _now_iso()
        op.review_required = bool(review_required)
        if summary:
            red, rep = redact_text(str(summary), max_chars=200)
            op.summary = red
            self._redactions += rep.total
        res = self._put(D_TOOL_OP, op.op_id, op.to_dict())
        self.prune_tool_ops()
        return res

    def tool_ops(self, session_id: str | None = None) -> list[ToolOpRecord]:
        out = [ToolOpRecord.from_dict(r) for r in self._safe_all(D_TOOL_OP)]
        if session_id is not None:
            out = [o for o in out if o.session_id == session_id]
        out.sort(key=lambda o: o.started_at)
        return out

    def unfinished_tool_ops(self) -> list[ToolOpRecord]:
        return [o for o in self.tool_ops() if o.finalized_at is None]

    # ── retention (deterministic; never deletes the ACTIVE session) ───────────
    def prune(self) -> dict:
        """Bound sessions, turns, tool ops and the event journal.

        The active session is EXCLUDED from session pruning by identity, so a long
        uptime can never silently delete the conversation currently in progress.
        """
        pruned = {"sessions": 0, "turns": 0, "tool_ops": 0, "events": 0}
        if not self.enabled:
            return pruned
        active = self.active_session.session_id if self.active_session else None
        all_sessions = self.sessions()
        keep = [s for s in all_sessions if s.session_id == active]
        rest = [s for s in all_sessions if s.session_id != active]
        room = max(0, self.max_sessions - len(keep))
        doomed = rest[room:]
        for s in doomed:
            for t in self.turns(s.session_id):
                self._safe_delete(D_TURN, t.turn_id)
                pruned["turns"] += 1
            self._safe_delete(D_SESSION, s.session_id)
            pruned["sessions"] += 1
        self._pruned_sessions += pruned["sessions"]
        self._pruned_turns += pruned["turns"]
        pruned["tool_ops"] = self.prune_tool_ops()
        try:
            pruned["events"] = self.store.retention(J_EVENT, MAX_JOURNAL_ROWS)
        except Exception:  # noqa: BLE001
            pass
        return pruned

    def prune_turns(self, session_id: str) -> int:
        """Keep only the most recent ``max_turns`` turns of one session."""
        turns = self.turns(session_id)
        if len(turns) <= self.max_turns:
            return 0
        doomed = turns[:len(turns) - self.max_turns]
        for t in doomed:
            self._safe_delete(D_TURN, t.turn_id)
        self._pruned_turns += len(doomed)
        return len(doomed)

    def prune_tool_ops(self) -> int:
        ops = self.tool_ops()
        if len(ops) <= MAX_TOOL_OPS:
            return 0
        doomed = ops[:len(ops) - MAX_TOOL_OPS]
        # A tool op that still needs operator review is retained for safety even
        # when it is old — retention must never erase an unresolved effectful action.
        removed = 0
        for o in doomed:
            if o.review_required or o.finalized_at is None:
                continue
            self._safe_delete(D_TOOL_OP, o.op_id)
            removed += 1
        return removed

    def forget_session(self, session_id: str) -> dict:
        """Delete one session's CONTENT, keeping a tombstone for audit safety.

        The tombstone preserves the identity, timestamps and turn count — the metadata
        the recovery/audit path needs — while removing every stored turn.
        """
        sess = self.get_session(session_id)
        removed = 0
        for t in self.turns(session_id):
            self._safe_delete(D_TURN, t.turn_id)
            removed += 1
        if sess is not None:
            sess.state = SessionState.FORGOTTEN.value
            self._put(D_SESSION, sess.session_id, sess.to_dict())
        if self.active_session is not None \
                and self.active_session.session_id == session_id:
            self.active_session.state = SessionState.FORGOTTEN.value
        return {"session_id": session_id, "turns_removed": removed,
                "tombstone": sess is not None}

    # ── health (bounded, content-free) ───────────────────────────────────────
    def health(self) -> dict:
        sessions = self.sessions() if self.enabled else []
        turns_retained = sum(len(self.turns(s.session_id)) for s in sessions[:5])
        active = self.active_session
        return {
            "persistence_mode": self.mode.value,
            "active_session_id_hash": id_hash(active.session_id) if active else None,
            "sessions_retained": len(sessions),
            "turns_retained": turns_retained,
            "journal_state": self.journal_state().value,
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "durable": bool(getattr(self._store, "durable", True))
            if self._store is not None else None,
            "writes": self._writes,
            "write_failures": self._write_failures,
            "read_failures": self._read_failures,
            "slow_writes": self._slow_writes,
            "last_write_ms": self._last_write_ms,
            "last_checkpoint_ms": self._last_checkpoint_ms,
            "checkpoint_failures": self._checkpoint_failures,
            "redactions": self._redactions,
            "pruned_sessions": self._pruned_sessions,
            "pruned_turns": self._pruned_turns,
            "corrupt_records_quarantined": self._corrupt_count(),
            "store_error": self._store_error or None,
        }

    def _corrupt_count(self) -> int:
        base = self._corrupt_records
        try:
            return base + int(self.store.health().get("corrupt_reads", 0))
        except Exception:  # noqa: BLE001
            return base

    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            except Exception:  # noqa: BLE001
                pass

    # ── guarded store reads (a corrupt row is isolated, never fatal) ──────────
    def _safe_all(self, domain: str) -> list[dict]:
        if not self.enabled:
            return []
        try:
            rows = self.store.all(domain)
        except Exception as exc:  # noqa: BLE001
            self._read_failures += 1
            logger.debug(f"CONTINUITY: read failed for {domain}: {type(exc).__name__}")
            return []
        out: list[dict] = []
        for r in rows:
            if isinstance(r, dict) and r:
                out.append(r)
            else:
                self._corrupt_records += 1
        return out

    def _safe_get(self, domain: str, entity_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            return self.store.get(domain, entity_id)
        except Exception:  # noqa: BLE001
            self._read_failures += 1
            return None

    @property
    def read_failures(self) -> int:
        """How many journal reads FAILED (not: returned nothing). Recovery consults
        this so an unreadable store degrades the report instead of looking clean."""
        return self._read_failures

    def _safe_delete(self, domain: str, entity_id: str) -> bool:
        try:
            self.store.delete(domain, entity_id)
            return True
        except Exception:  # noqa: BLE001
            self._write_failures += 1
            return False


# ══════════════════════════════════════════════════════════════════════════════
#  Live fingerprints — RECOMPUTED, never restored (M60.3)
# ══════════════════════════════════════════════════════════════════════════════
def authority_fingerprint(state=None) -> str:
    """A content-free digest of the CURRENT authority posture + active scope ids.

    Restoring an old value as truth would be reusing yesterday's authorization; this
    is recomputed at boot and COMPARED to the journalled one.
    """
    try:
        if state is None:
            return ""
        mode = getattr(getattr(state, "mode", None), "value", str(
            getattr(state, "mode", "")))
        return _hash_text(f"authority|{mode}")[:16]
    except Exception:  # noqa: BLE001
        return ""


def scope_fingerprint(state=None) -> str:
    try:
        if state is None:
            return ""
        scopes = sorted(str(getattr(s, "scope_id", "")) for s in
                        (getattr(state, "active_scopes", lambda: [])() or []))
        return _hash_text("scope|" + "|".join(scopes))[:16]
    except Exception:  # noqa: BLE001
        return ""


def current_fingerprints(authority_state=None, *, tools=None) -> dict:
    """Every fingerprint a session is stamped with, measured RIGHT NOW.

    ``tools`` is the live tool schema; it is a PARAMETER rather than an import so this
    stays cheap and side-effect-free (importing ``core.llm`` to read it would pull the
    whole inference stack into a recovery path that must not touch a model).
    """
    out = {
        "authority_fingerprint": authority_fingerprint(authority_state),
        "scope_fingerprint": scope_fingerprint(authority_state),
        "security_policy_version": "",
        "prompt_fingerprint": "",
        "tool_schema_fingerprint": "",
    }
    try:
        from core.prompt_manifest import (
            SECURITY_POLICY_VERSION, security_policy_fingerprint,
        )
        out["security_policy_version"] = (
            f"{SECURITY_POLICY_VERSION}:{security_policy_fingerprint()}")[:32]
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.prompt_manifest import stable_prefix_fingerprint
        out["prompt_fingerprint"] = stable_prefix_fingerprint()[:32]
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.tool_schema import tool_schema_fingerprint
        out["tool_schema_fingerprint"] = str(tool_schema_fingerprint(tools))[:32]
    except Exception:  # noqa: BLE001
        pass
    return out


# ── Process-global singleton ─────────────────────────────────────────────────
_journal: SessionJournal | None = None


def get_session_journal() -> SessionJournal:
    """The process session journal, seeded from operator configuration once."""
    global _journal
    if _journal is None:
        mode, bounds = DEFAULT_PERSISTENCE_MODE, {}
        try:
            from core.config import settings
            mode = parse_persistence_mode(
                getattr(settings, "session_persistence_mode",
                        DEFAULT_PERSISTENCE_MODE.value))
            bounds = {
                "max_sessions": int(getattr(settings, "session_max_sessions",
                                            MAX_SESSIONS)),
                "max_turns": int(getattr(settings, "session_max_turns",
                                         MAX_TURNS_PER_SESSION)),
                "max_visible_chars": int(getattr(settings, "session_max_turn_chars",
                                                 MAX_VISIBLE_CHARS)),
            }
        except Exception:  # noqa: BLE001
            mode, bounds = DEFAULT_PERSISTENCE_MODE, {}
        _journal = SessionJournal(mode=mode, **bounds)
    return _journal


def reset_session_journal(instance: "SessionJournal | None" = None) -> None:
    """Tests / a fresh process."""
    global _journal
    _journal = instance
