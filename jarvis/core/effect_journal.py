"""
core/effect_journal.py — V69 M65C: the durable effect journal.

WHAT THIS MODULE IS FOR
=======================
M64.1 gave ``ToolExecutor`` an in-memory effect ledger; M65B added an in-flight
reservation so two concurrent callers in ONE process produce one effect. Both
live in RAM, and RAM does not survive an interpreter crash, a SIGKILL, a machine
restart or a worker replacement.

This module is the durable half. It owns, on disk, the identity and lifecycle of
every effect the runtime has reserved, and it is the only component that can
answer "did this already happen?" after the process that asked is gone.

WHAT IT DELIBERATELY CANNOT DO
==============================
It cannot make an arbitrary external effect exactly-once. One window makes that
impossible:

        the effect happened  ->  X process dies  ->  no COMMITTED row

After a restart that row reads ``EXECUTING``. So does a row whose owner died
BEFORE it ever invoked the tool. Those two realities are indistinguishable from
local state alone, because the evidence that separates them is in the external
system, not here.

So this module never converts "no COMMITTED row" into "it did not happen". An
owner that reached :attr:`EffectState.EXECUTING` and never came back becomes
:attr:`EffectState.INDETERMINATE`, and what may happen next is decided by the
tool's :class:`EffectDurabilityClass` — never by the fact that a lease expired.

STORAGE
=======
stdlib ``sqlite3``, one file on the local NVMe, WAL. The engine and the
conventions (``jarvis/data/``, a ``meta`` table carrying ``schema_version``,
forward-only migration) come from :mod:`core.operational_store`; the discipline
does not, and that is why this is a separate module. The operational store fails
OPEN — it degrades to an in-memory database and keeps going — which for an
effect journal would mean silently losing the protection while claiming to have
it. This one fails CLOSED (:class:`JournalUnhealthy`).

BODY-SAFE BY CONSTRUCTION
=========================
Every column is an id, an enum, a timestamp, a counter or a domain-separated
digest. No raw argument, no raw tool result, no secret and no prompt is ever
written, so the confidentiality of the file is not load-bearing. Recovery after
a restart therefore returns a truthful ENVELOPE (identity, disposition, receipt
digest, commit time), not the original response body — retaining the body would
make this database a copy of every tool output it has ever seen.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

try:
    from loguru import logger
except ModuleNotFoundError:  # pragma: no cover — mirrors runtime_doctor's fallback
    import logging

    logger = logging.getLogger("effect_journal")

#: Bumped only for a schema change that needs a migration step. A journal
#: written by a NEWER schema than this code understands is refused, never
#: rewritten (§21) — a forward-compatible read of an unknown layout is a guess.
SCHEMA_VERSION = 1

_JARVIS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_PATH = _JARVIS_DIR / "data" / "effect_journal.db"

#: Env overrides, read through here rather than at each call site so the paths
#: and timings are configurable and stable and no developer machine path is ever
#: hardcoded (§28).
_PATH_ENV = "JARVIS_EFFECT_JOURNAL_PATH"
_ENABLED_ENV = "JARVIS_EFFECT_JOURNAL"
_LEASE_ENV = "JARVIS_EFFECT_LEASE_S"
_LEASE_GRACE_ENV = "JARVIS_EFFECT_LEASE_GRACE_S"

#: How long a reservation stays this owner's before another process may consider
#: the owner gone. Generous relative to a tool call, because an owner declared
#: dead while it is merely slow is the one mistake that manufactures duplicates.
DEFAULT_LEASE_S = 900.0

#: Added to every expiry comparison. A lease is expired only when the clock is
#: past ``lease_expires_at + grace``, so a modest forward clock jump cannot turn
#: a live owner into a reclaimable one (§40).
DEFAULT_LEASE_GRACE_S = 60.0

#: Bounded, per §24. SQLite raises ``database is locked`` immediately without
#: it; an unbounded wait would let one stuck process hang every other one.
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: Owner-only permissions on the database file. The journal holds no secret, so
#: this is defence in depth rather than the thing that keeps a secret safe (§27).
_FILE_MODE = 0o600


class JournalUnhealthy(RuntimeError):
    """The journal cannot be trusted, so effectful execution must not proceed.

    Raised for an unopenable file, a failed integrity check, a schema written by
    a newer version, or a structurally missing table. Never recovered from by
    deleting or recreating the database (§25): a journal that heals itself by
    forgetting is a journal that authorises a replay.
    """


class InvalidTransition(RuntimeError):
    """A state change that the state machine does not allow (§7)."""


class EffectJournalRefused(RuntimeError):
    """The journal would not record an effect that was about to run.

    Raised at the one point where the external effect is about to become real.
    A tool invoked without a durable EXECUTING row is a tool whose crash cannot
    be recovered from, so the call is aborted instead.
    """


class EffectState(str, Enum):
    """Durable lifecycle of one effect identity.

    ``FAILED_BEFORE_EFFECT`` and ``INDETERMINATE`` are NOT synonyms and the
    distinction is the point of the milestone: the first is proven pre-effect,
    the second is unprovable either way.
    """

    RESERVED = "RESERVED"
    EXECUTING = "EXECUTING"
    COMMITTED = "COMMITTED"
    #: Proven pre-effect: a gate refused, a preflight failed, or the caller
    #: cancelled before EXECUTING was durably committed. Retry is legitimate.
    FAILED_BEFORE_EFFECT = "FAILED_BEFORE_EFFECT"
    #: The handler returned an error or raised and THIS PROCESS observed it.
    #: Distinct from INDETERMINATE, where the owner never came back at all.
    FAILED_OBSERVED = "FAILED_OBSERVED"
    #: An owner reached EXECUTING and is gone. The P2/P3 window.
    INDETERMINATE = "INDETERMINATE"
    RECONCILED_COMMITTED = "RECONCILED_COMMITTED"
    RECONCILED_NOT_EXECUTED = "RECONCILED_NOT_EXECUTED"


#: States from which nothing further happens without reconciliation.
_TERMINAL: frozenset[EffectState] = frozenset({
    EffectState.COMMITTED, EffectState.FAILED_BEFORE_EFFECT,
    EffectState.FAILED_OBSERVED, EffectState.RECONCILED_COMMITTED,
    EffectState.RECONCILED_NOT_EXECUTED,
})

#: States that mean "the external world contains this effect".
_PROVEN_COMMITTED: frozenset[EffectState] = frozenset({
    EffectState.COMMITTED, EffectState.RECONCILED_COMMITTED,
})

#: The ONLY edges the journal will write. Anything else raises
#: InvalidTransition, so a caller cannot invent a path through the machine —
#: notably there is no edge from EXECUTING back to FAILED_BEFORE_EFFECT, because
#: once EXECUTING is durable nothing can prove the effect did not start.
_ALLOWED_EDGES: frozenset[tuple[EffectState, EffectState]] = frozenset({
    (EffectState.RESERVED, EffectState.RESERVED),          # reclaim, attempt+1
    (EffectState.RESERVED, EffectState.EXECUTING),
    (EffectState.RESERVED, EffectState.FAILED_BEFORE_EFFECT),
    (EffectState.RESERVED, EffectState.INDETERMINATE),      # see below
    (EffectState.EXECUTING, EffectState.COMMITTED),
    (EffectState.EXECUTING, EffectState.FAILED_OBSERVED),
    (EffectState.EXECUTING, EffectState.INDETERMINATE),
    (EffectState.EXECUTING, EffectState.EXECUTING),         # re-entry, attempt+1
    # Authorised replay of an ambiguous effect. Guarded in `_take_over`: only a
    # class in _REPLAYABLE_AFTER_AMBIGUITY may traverse it, so the presence of
    # the edge is not on its own permission to re-run a NON_REPLAYABLE effect.
    (EffectState.EXECUTING, EffectState.RESERVED),
    (EffectState.INDETERMINATE, EffectState.RECONCILED_COMMITTED),
    (EffectState.INDETERMINATE, EffectState.RECONCILED_NOT_EXECUTED),
    (EffectState.INDETERMINATE, EffectState.EXECUTING),     # authorised replay
    (EffectState.RECONCILED_NOT_EXECUTED, EffectState.RESERVED),
    (EffectState.FAILED_BEFORE_EFFECT, EffectState.RESERVED),
    (EffectState.FAILED_OBSERVED, EffectState.RESERVED),
})
# RESERVED -> INDETERMINATE exists for one case only: a reservation whose owner
# is gone AND whose durability class forbids assuming anything. It is never
# taken on the ordinary pre-effect path, which uses FAILED_BEFORE_EFFECT.


class EffectDurabilityClass(str, Enum):
    """What may safely happen to an effect whose outcome is unknown (§13).

    An unknown effectful tool is NON_REPLAYABLE. That default is the fail-closed
    one: it costs an operator a manual reconciliation, where the opposite
    default costs them a duplicate irreversible action.
    """

    READ_ONLY = "READ_ONLY"
    #: Repeating the semantically identical action converges to the same state.
    IDEMPOTENT = "IDEMPOTENT"
    #: The external system honours an idempotency key, so a replay with the same
    #: key is deduplicated THERE and produces one external effect.
    IDEMPOTENT_WITH_KEY = "IDEMPOTENT_WITH_KEY"
    #: The external system can be asked whether the effect happened.
    RECONCILABLE = "RECONCILABLE"
    #: Cannot be retried after an ambiguous crash, and cannot be asked.
    NON_REPLAYABLE = "NON_REPLAYABLE"


#: Classes whose ambiguous (P2/P3) outcome may be resolved by re-running.
_REPLAYABLE_AFTER_AMBIGUITY: frozenset[EffectDurabilityClass] = frozenset({
    EffectDurabilityClass.IDEMPOTENT,
    EffectDurabilityClass.IDEMPOTENT_WITH_KEY,
})


class ReconciliationVerdict(str, Enum):
    """The bounded answer a RECONCILABLE tool gives about one effect (§15).

    ``UNKNOWN`` is a first-class answer and is never rounded to either
    certainty. It leaves the effect INDETERMINATE.
    """

    CONFIRMED_COMMITTED = "CONFIRMED_COMMITTED"
    CONFIRMED_NOT_EXECUTED = "CONFIRMED_NOT_EXECUTED"
    UNKNOWN = "UNKNOWN"


class ExecutionDisposition(str, Enum):
    """What a call to the effect protocol actually DID (§18).

    Explicit, never inferred. M65A tried to derive this by sampling a counter
    before and after, which is correct sequentially and wrong the instant two
    callers overlap; M65C keeps it a stated fact from the component that took
    the branch.
    """

    EXECUTED_NOW = "EXECUTED_NOW"
    DEDUPLICATED_IN_PROCESS = "DEDUPLICATED_IN_PROCESS"
    DEDUPLICATED_DURABLE = "DEDUPLICATED_DURABLE"
    RECOVERED_COMMITTED = "RECOVERED_COMMITTED"
    RECONCILED_COMMITTED = "RECONCILED_COMMITTED"
    FAILED_BEFORE_EFFECT = "FAILED_BEFORE_EFFECT"
    BLOCKED_INDETERMINATE = "BLOCKED_INDETERMINATE"
    #: A live owner in another process holds the reservation and did not finish
    #: within this caller's bounded wait. Nothing was executed here.
    BLOCKED_OWNED_ELSEWHERE = "BLOCKED_OWNED_ELSEWHERE"


class ReservationOutcome(str, Enum):
    """What :meth:`DurableEffectJournal.reserve` decided."""

    OWNED = "OWNED"                       # fresh reservation, this caller owns it
    RECLAIMED = "RECLAIMED"               # stale PRE-EFFECT owner taken over
    ALREADY_COMMITTED = "ALREADY_COMMITTED"
    OWNED_ELSEWHERE = "OWNED_ELSEWHERE"   # a live owner in another process
    INDETERMINATE = "INDETERMINATE"       # blocked; needs reconciliation
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


# ── identity ────────────────────────────────────────────────────────────────
#: Domain separation. Two digests computed over different things must never
#: collide just because the bytes line up, so each prefix names its own domain.
_D_EFFECT = "jarvis.m65c.effect.v1"
_D_ARGS = "jarvis.m65c.args.v1"
_D_ACTION = "jarvis.m65c.action.v1"
_D_IDEM = "jarvis.m65c.idempotency.v1"
_D_RECEIPT = "jarvis.m65c.receipt.v1"
_D_OPAQUE = "jarvis.m65c.opaque.v1"


def canonical_json(payload) -> str:
    """Stable serialisation for identity (§53).

    Sorted keys, so argument ORDER cannot manufacture a second identity for the
    same call, and ``ensure_ascii=False`` so an accented string is one sequence
    rather than two spellings. Deliberately NOT normalising values: ``"1"`` and
    ``1`` are different executable requests and must stay different identities.
    """
    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(payload)


def _digest(domain: str, *parts: str) -> str:
    h = hashlib.sha256()
    h.update(domain.encode("utf-8"))
    for part in parts:
        h.update(b"\x1f")
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def args_digest(tool_input) -> str:
    """Body-safe identity of a call's arguments. Never reversible to the body."""
    return _digest(_D_ARGS, canonical_json(tool_input))


def action_digest(surface: str, tool_id: str) -> str:
    return _digest(_D_ACTION, surface, tool_id)


def opaque_digest(value) -> str:
    """Body-safe stand-in for any value the journal must reference but not keep.

    Used for authority, scope and approval identities and for a tool result. It
    is a plain SHA-256 over a canonical form: it detects that two things differ,
    and it is NOT an authentication tag — nothing here is keyed, so it proves
    nothing against an attacker who can write the file (§26).
    """
    return _digest(_D_OPAQUE, canonical_json(value))


def receipt_digest(result) -> str:
    return _digest(_D_RECEIPT, canonical_json(result))


def compute_effect_id(*, surface: str, tool_id: str, identity_scope: str,
                      tool_input) -> str:
    """The durable identity of one effect.

    ``identity_scope`` is the caller's declaration of WHICH request this is —
    the effect epoch, which for a mesh or team task is that task's durable id.
    It is part of the identity on purpose: two separate operator requests to
    block the same address are two effects and both must be able to run, so the
    journal must not fuse them merely because the arguments match. The
    consequence, stated plainly, is that durable dedupe reaches across a restart
    exactly as far as the caller's scope does.
    """
    return _digest(_D_EFFECT, surface, tool_id, identity_scope,
                   canonical_json(tool_input))


def derive_idempotency_key(effect_id: str) -> str:
    """The key handed to an IDEMPOTENT_WITH_KEY tool (§14).

    Derived from the canonical effect identity and nothing else, so it is the
    same on every attempt and the same after a restart, and it differs whenever
    the executable request differs. It contains no timestamp and no random
    component — either would defeat the external system's deduplication at
    exactly the moment it is needed.

    It is also not user-selectable: an argument literally named
    ``idempotency_key`` feeds the identity like any other argument and can never
    BE the key, so model-authored text cannot steer two different actions onto
    one key or one action onto two.
    """
    return _digest(_D_IDEM, effect_id)


# ── runtime instance identity ───────────────────────────────────────────────
_INSTANCE_LOCK = threading.Lock()
_INSTANCE_ID: str = ""


def runtime_instance_id() -> str:
    """A stable, process-unique owner identity (§10).

    A PID alone is NOT usable as a durable identity: PIDs are reused, so a
    reservation owned by pid 4242 could be silently "confirmed alive" by an
    unrelated process that inherited the number after a restart. This is a
    random per-process UUID with the pid appended as human-readable metadata
    only; uniqueness comes entirely from the UUID.
    """
    global _INSTANCE_ID
    with _INSTANCE_LOCK:
        if not _INSTANCE_ID:
            _INSTANCE_ID = f"{uuid.uuid4().hex}.{os.getpid()}"
        return _INSTANCE_ID


def _reset_runtime_instance_id_for_tests() -> str:
    """Force a NEW instance identity. Used by tests that simulate a restart in
    the same interpreter; production never calls it."""
    global _INSTANCE_ID
    with _INSTANCE_LOCK:
        _INSTANCE_ID = ""
    return runtime_instance_id()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class EffectRecord:
    """One durable effect row. Every field is body-safe."""

    effect_id: str
    tool_id: str
    surface: str
    durability_class: EffectDurabilityClass
    state: EffectState
    owner_instance_id: str
    owner_attempt: int
    canonical_action_digest: str
    canonical_args_digest: str
    authority_digest: str
    scope_digest: str
    approval_digest: str
    plan_id: str
    task_id: str
    idempotency_key: str
    reservation_created_at: str
    lease_expires_at: str
    state_changed_at: str
    committed_at: str
    receipt_digest: str
    failure_class: str
    recovery_note: str

    @property
    def proven_committed(self) -> bool:
        return self.state in _PROVEN_COMMITTED

    def to_dict(self) -> dict:
        return {
            "effect_id": self.effect_id, "tool_id": self.tool_id,
            "surface": self.surface,
            "durability_class": self.durability_class.value,
            "state": self.state.value,
            "owner_instance_id": self.owner_instance_id,
            "owner_attempt": self.owner_attempt,
            "canonical_args_digest": self.canonical_args_digest,
            "idempotency_key": self.idempotency_key,
            "reservation_created_at": self.reservation_created_at,
            "lease_expires_at": self.lease_expires_at,
            "committed_at": self.committed_at,
            "receipt_digest": self.receipt_digest,
            "failure_class": self.failure_class,
            "recovery_note": self.recovery_note,
        }


@dataclass(frozen=True)
class Reservation:
    """What the caller got back from :meth:`DurableEffectJournal.reserve`."""

    outcome: ReservationOutcome
    record: EffectRecord
    #: True only when this caller may proceed to run the tool.
    owned: bool

    @property
    def effect_id(self) -> str:
        return self.record.effect_id


#: Executed statement-by-statement inside ONE transaction, never via
#: ``executescript`` — that helper commits any open transaction before it
#: runs, which would let a second process starting at the same time observe a
#: half-built schema (§22).
_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS effects (
    effect_id               TEXT PRIMARY KEY,
    schema_version          INTEGER NOT NULL,
    tool_id                 TEXT NOT NULL,
    surface                 TEXT NOT NULL,
    durability_class        TEXT NOT NULL,
    canonical_action_digest TEXT NOT NULL,
    canonical_args_digest   TEXT NOT NULL,
    authority_digest        TEXT NOT NULL DEFAULT '',
    scope_digest            TEXT NOT NULL DEFAULT '',
    approval_digest         TEXT NOT NULL DEFAULT '',
    plan_id                 TEXT NOT NULL DEFAULT '',
    task_id                 TEXT NOT NULL DEFAULT '',
    idempotency_key         TEXT NOT NULL DEFAULT '',
    owner_instance_id       TEXT NOT NULL,
    owner_attempt           INTEGER NOT NULL,
    state                   TEXT NOT NULL,
    reservation_created_at  TEXT NOT NULL,
    lease_expires_at        TEXT NOT NULL,
    state_changed_at        TEXT NOT NULL,
    committed_at            TEXT NOT NULL DEFAULT '',
    receipt_digest          TEXT NOT NULL DEFAULT '',
    failure_class           TEXT NOT NULL DEFAULT '',
    recovery_note           TEXT NOT NULL DEFAULT ''
)""",
    """CREATE INDEX IF NOT EXISTS idx_effects_state ON effects(state)""",
    """CREATE TABLE IF NOT EXISTS transitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id         TEXT NOT NULL REFERENCES effects(effect_id) ON DELETE CASCADE,
    from_state        TEXT NOT NULL,
    to_state          TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL,
    owner_attempt     INTEGER NOT NULL,
    at                TEXT NOT NULL,
    note              TEXT NOT NULL DEFAULT ''
)""",
    """CREATE INDEX IF NOT EXISTS idx_transitions_effect ON transitions(effect_id, id)""",
)

_COLUMNS = (
    "effect_id, tool_id, surface, durability_class, state, owner_instance_id, "
    "owner_attempt, canonical_action_digest, canonical_args_digest, "
    "authority_digest, scope_digest, approval_digest, plan_id, task_id, "
    "idempotency_key, reservation_created_at, lease_expires_at, "
    "state_changed_at, committed_at, receipt_digest, failure_class, "
    "recovery_note"
)


def journal_enabled() -> bool:
    """Whether the durable journal is active for this process.

    Default ON. The switch exists so an operator whose journal is unhealthy has
    a visible, deliberate way to keep working (the Runtime Doctor reports it) —
    not so a code path can quietly opt out of durability.
    """
    raw = os.environ.get(_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def configured_journal_path() -> Path:
    raw = os.environ.get(_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_JOURNAL_PATH


def _configured_seconds(name: str, default: float) -> float:
    """A non-negative float from the environment, or *default*.

    A malformed or negative value falls back rather than failing boot: the
    consequence of a bad lease setting is a timing change, and refusing to start
    over one would be worse than ignoring it. It IS logged, so a setting that
    silently did nothing is visible.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"EFFECT_JOURNAL: {name}={raw!r} is not a number; "
                       f"using {default}")
        return default
    if value < 0:
        logger.warning(f"EFFECT_JOURNAL: {name}={raw!r} is negative; "
                       f"using {default}")
        return default
    return value


def configured_lease_s() -> float:
    return _configured_seconds(_LEASE_ENV, DEFAULT_LEASE_S)


def configured_lease_grace_s() -> float:
    return _configured_seconds(_LEASE_GRACE_ENV, DEFAULT_LEASE_GRACE_S)


class DurableEffectJournal:
    """Durable ownership and lifecycle for effect identities, across processes.

    Every write goes through a short ``BEGIN IMMEDIATE`` transaction. None of
    them spans a tool invocation (§9/§23) — the journal is left in ``EXECUTING``
    with no transaction held while the external call runs, which is precisely
    why a crash there is recoverable at all.
    """

    def __init__(self, path: "str | Path | None" = None, *, clock=None,
                 lease_s: "float | None" = None,
                 lease_grace_s: "float | None" = None,
                 busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 instance_id: "str | None" = None) -> None:
        self._path = Path(path) if path is not None else configured_journal_path()
        self._clock = clock if clock is not None else _utcnow
        self._lease_s = float(lease_s if lease_s is not None
                              else configured_lease_s())
        self._lease_grace_s = float(lease_grace_s if lease_grace_s is not None
                                    else configured_lease_grace_s())
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._instance_id = instance_id or runtime_instance_id()
        #: sqlite3 connections are not safe to share across threads without
        #: care, and the executor runs handlers in a thread pool. One lock, held
        #: only for the microseconds of a transaction.
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}
        self._db = self._open()
        self._init_schema()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def _open(self) -> sqlite3.Connection:
        try:
            if str(self._path) != ":memory:":
                self._path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self._path), check_same_thread=False,
                                 isolation_level=None, timeout=self._busy_timeout_ms / 1000.0)
        except Exception as exc:  # noqa: BLE001 — see JournalUnhealthy
            # NOT an in-memory fallback. A journal we cannot open is a journal
            # that cannot protect anything, and pretending otherwise is how a
            # duplicate irreversible effect gets authorised (§25).
            raise JournalUnhealthy(
                f"cannot open the effect journal at {self._path}: "
                f"{type(exc).__name__}") from exc
        db.row_factory = sqlite3.Row
        if str(self._path) != ":memory:":
            try:
                os.chmod(self._path, _FILE_MODE)
            except OSError:  # pragma: no cover — a filesystem without chmod
                logger.warning("EFFECT_JOURNAL: could not restrict permissions on "
                               f"{self._path}")
        cur = db.cursor()
        # Bounded contention (§24), set FIRST because everything below it can
        # collide with another process opening the same file at the same
        # instant. Never unbounded: one stuck process would otherwise hang every
        # other one indefinitely.
        cur.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        # WAL: a reader (recovery inspection, the doctor) must not block the
        # writer reserving an effect. A persistent property of the file.
        self._ensure_wal(cur)
        # THE load-bearing pragma. Under WAL the default NORMAL does not fsync
        # on commit, so a power loss can lose a COMMITTED row for an effect that
        # really happened — manufacturing the exact P3 ambiguity this module
        # exists to prevent. Paid on single-digit writes per turn, never on a
        # hot path (§63).
        cur.execute("PRAGMA synchronous=FULL")
        # The transition audit references the effect row; an orphan transition
        # is a corrupt audit trail.
        cur.execute("PRAGMA foreign_keys=ON")
        return db

    def _ensure_wal(self, cur) -> None:
        """Put the file in WAL, tolerating another process doing it at once.

        ``PRAGMA journal_mode=WAL`` needs a brief exclusive lock, and unlike an
        ordinary write SQLite does not reliably route it through the busy
        handler — so two processes opening a brand-new journal at the same
        instant can make one of them fail outright with "database is locked".

        Measured: the §22 initialisation-race test reproduced exactly that, and
        it is not a test artefact — two JARVIS processes starting together would
        hit it. WAL is a persistent property of the FILE rather than of a
        connection, so the loser only has to wait for the winner and read the
        mode back. Bounded by the same busy timeout as every other wait here.
        """
        deadline = time.monotonic() + (self._busy_timeout_ms / 1000.0)
        detail = ""
        while True:
            try:
                row = cur.execute("PRAGMA journal_mode=WAL").fetchone()
                if row and str(row[0]).lower() == "wal":
                    return
                detail = f"mode is {row[0] if row else 'unknown'}"
            except sqlite3.OperationalError as exc:
                detail = str(exc)
            try:
                row = cur.execute("PRAGMA journal_mode").fetchone()
                if row and str(row[0]).lower() == "wal":
                    return          # another process established it first
            except sqlite3.OperationalError:
                pass
            if time.monotonic() >= deadline:
                raise JournalUnhealthy(
                    f"the effect journal at {self._path} could not be put into "
                    f"WAL mode within {self._busy_timeout_ms}ms ({detail}); "
                    f"refusing to run without it")
            time.sleep(0.01)

    def _init_schema(self) -> None:
        """Create or validate the schema. Two processes may race here (§22).

        Both run the same ``CREATE TABLE IF NOT EXISTS`` script inside one
        transaction, so the loser sees a complete schema rather than a partial
        one, and the version row is claimed with ``INSERT OR IGNORE``.
        """
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    self._db.execute(statement)
                self._db.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
                self._db.execute("COMMIT")
            except sqlite3.Error as exc:
                self._rollback_quietly()
                raise JournalUnhealthy(
                    f"the effect journal schema could not be initialised: "
                    f"{type(exc).__name__}: {exc}") from exc
        self._check_schema_version()

    def _check_schema_version(self) -> None:
        row = self._db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            raise JournalUnhealthy("the effect journal has no schema_version row")
        try:
            found = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise JournalUnhealthy(
                f"the effect journal schema_version is not an integer: "
                f"{row['value']!r}") from exc
        if found > SCHEMA_VERSION:
            # Fail closed (§21). A newer layout may carry states this code does
            # not know how to honour, and guessing at one is how an
            # INDETERMINATE effect gets read as a safe retry.
            raise JournalUnhealthy(
                f"the effect journal was written by schema v{found}, newer than "
                f"this build's v{SCHEMA_VERSION}; refusing to read it")
        if found < SCHEMA_VERSION:
            self._migrate(found, SCHEMA_VERSION)

    def _migrate(self, frm: int, to: int) -> None:
        """Forward-only, deterministic, bounded (§21).

        v1 is the baseline, so there is no step to run yet. A future version
        adds an explicit, ordered step here; there is deliberately no generic
        "recreate the table" path, because the destructive repair is the bug.
        """
        applied = 0
        for step in range(frm, to):
            handler = _MIGRATIONS.get(step)
            if handler is None:
                raise JournalUnhealthy(
                    f"no migration from effect-journal schema v{step} to "
                    f"v{step + 1}; refusing to guess")
            with self._lock:
                try:
                    self._db.execute("BEGIN IMMEDIATE")
                    handler(self._db)
                    self._db.execute(
                        "UPDATE meta SET value=? WHERE key='schema_version'",
                        (str(step + 1),))
                    self._db.execute("COMMIT")
                except sqlite3.Error as exc:
                    self._rollback_quietly()
                    raise JournalUnhealthy(
                        f"effect-journal migration v{step}->v{step + 1} failed: "
                        f"{type(exc).__name__}") from exc
            applied += 1
        logger.info(f"EFFECT_JOURNAL: migrated schema v{frm} -> v{to} "
                    f"({applied} step(s))")

    def _rollback_quietly(self) -> None:
        try:
            self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    @property
    def path(self) -> Path:
        return self._path

    @property
    def instance_id(self) -> str:
        return self._instance_id

    # ── health ──────────────────────────────────────────────────────────────
    def integrity_check(self) -> str:
        """SQLite's own structural check. ``"ok"`` or the first problem found.

        Detects corruption. It does NOT detect tampering: nothing here is
        authenticated with a key an attacker cannot also write, so a local
        writer who can edit the file can also make it self-consistent (§26).
        """
        try:
            row = self._db.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return f"unreadable: {type(exc).__name__}"
        return str(row[0]) if row else "unreadable: no result"

    def assert_healthy(self) -> None:
        """Raise :class:`JournalUnhealthy` unless the journal is usable.

        The remedy for a failure is an operator decision, never an automatic
        delete-and-recreate: losing the committed identities reopens every
        replay this module exists to close (§25/§29).
        """
        verdict = self.integrity_check()
        if verdict != "ok":
            raise JournalUnhealthy(
                f"the effect journal at {self._path} failed its integrity "
                f"check: {verdict}. It has NOT been modified — recover it from "
                f"a managed backup or move it aside deliberately.")
        for table in ("meta", "effects", "transitions"):
            try:
                self._db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise JournalUnhealthy(
                    f"the effect journal is missing table '{table}': "
                    f"{type(exc).__name__}") from exc
        self._check_schema_version()

    def _bump(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    @property
    def counters(self) -> dict:
        return dict(self._counters)

    # ── reading ─────────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            effect_id=row["effect_id"], tool_id=row["tool_id"],
            surface=row["surface"],
            durability_class=EffectDurabilityClass(row["durability_class"]),
            state=EffectState(row["state"]),
            owner_instance_id=row["owner_instance_id"],
            owner_attempt=int(row["owner_attempt"]),
            canonical_action_digest=row["canonical_action_digest"],
            canonical_args_digest=row["canonical_args_digest"],
            authority_digest=row["authority_digest"],
            scope_digest=row["scope_digest"],
            approval_digest=row["approval_digest"],
            plan_id=row["plan_id"], task_id=row["task_id"],
            idempotency_key=row["idempotency_key"],
            reservation_created_at=row["reservation_created_at"],
            lease_expires_at=row["lease_expires_at"],
            state_changed_at=row["state_changed_at"],
            committed_at=row["committed_at"],
            receipt_digest=row["receipt_digest"],
            failure_class=row["failure_class"],
            recovery_note=row["recovery_note"])

    def get(self, effect_id: str) -> "EffectRecord | None":
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?",
            (effect_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def transitions(self, effect_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT from_state, to_state, owner_instance_id, owner_attempt, at, "
            "note FROM transitions WHERE effect_id=? ORDER BY id", (effect_id,))
        return [dict(r) for r in rows]

    def lease_expired(self, record: EffectRecord, now: "datetime | None" = None) -> bool:
        """Whether *record*'s owner may be treated as gone.

        Conservative on purpose (§40). Expiry requires the clock to be past the
        lease PLUS a grace, so a modest forward jump does not evict a live
        owner; a backward jump makes ``now`` smaller and can only ever say "not
        expired". A malformed timestamp is treated as NOT expired, because the
        failure mode of guessing wrong here is a duplicate effect.
        """
        try:
            expires = _parse_iso(record.lease_expires_at)
        except (TypeError, ValueError):
            return False
        moment = now if now is not None else self._clock()
        return moment > expires + timedelta(seconds=self._lease_grace_s)

    # ── writing ─────────────────────────────────────────────────────────────
    def _record_transition(self, effect_id: str, frm: EffectState,
                           to: EffectState, owner: str, attempt: int,
                           at: str, note: str = "") -> None:
        """Append the edge, and REFUSE one the state machine does not allow.

        This is the only place an edge is validated. Every path that changes a
        row calls it — `reserve`, `_take_over` and `_transition_locked` — so one
        check covers all of them, and the transaction it runs inside means a
        refusal rolls the row change back with it.
        """
        if (frm, to) not in _ALLOWED_EDGES:
            raise InvalidTransition(
                f"effect journal refuses {frm.value} -> {to.value}")
        self._db.execute(
            "INSERT INTO transitions(effect_id, from_state, to_state, "
            "owner_instance_id, owner_attempt, at, note) VALUES(?,?,?,?,?,?,?)",
            (effect_id, frm.value, to.value, owner, attempt, at, note[:200]))

    def reserve(self, *, effect_id: str, tool_id: str, surface: str,
                durability_class: EffectDurabilityClass, tool_input,
                authority_digest: str = "", scope_digest: str = "",
                approval_digest: str = "", plan_id: str = "",
                task_id: str = "") -> Reservation:
        """Atomically claim *effect_id*, or report who already has it (§8).

        The claim is a single ``INSERT ... ON CONFLICT DO NOTHING`` inside
        ``BEGIN IMMEDIATE``. There is deliberately no "SELECT, decide, INSERT"
        anywhere in this method: that shape is exactly the M65B bug, and writing
        it against a database would only move the same race onto disk.

        Reclaiming a stale owner is likewise a single conditional ``UPDATE``
        guarded on the state, the expiry AND the ``owner_attempt`` this caller
        read — a compare-and-swap, so two processes that both see the same stale
        row still produce one winner.
        """
        now = self._clock()
        now_iso = _iso(now)
        lease_iso = _iso(now + timedelta(seconds=self._lease_s))
        idem = derive_idempotency_key(effect_id)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # The busy timeout elapsed. Bounded and reported, never retried
                # forever (§24).
                self._bump("db_busy")
                raise JournalUnhealthy(
                    f"the effect journal was locked longer than "
                    f"{self._busy_timeout_ms}ms: {exc}") from exc
            try:
                cur = self._db.execute(
                    "INSERT INTO effects(effect_id, schema_version, tool_id, "
                    "surface, durability_class, canonical_action_digest, "
                    "canonical_args_digest, authority_digest, scope_digest, "
                    "approval_digest, plan_id, task_id, idempotency_key, "
                    "owner_instance_id, owner_attempt, state, "
                    "reservation_created_at, lease_expires_at, state_changed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?) "
                    "ON CONFLICT(effect_id) DO NOTHING",
                    (effect_id, SCHEMA_VERSION, tool_id, surface,
                     durability_class.value, action_digest(surface, tool_id),
                     args_digest(tool_input), authority_digest, scope_digest,
                     approval_digest, plan_id, task_id, idem,
                     self._instance_id, EffectState.RESERVED.value,
                     now_iso, lease_iso, now_iso))
                if cur.rowcount == 1:
                    self._record_transition(
                        effect_id, EffectState.RESERVED, EffectState.RESERVED,
                        self._instance_id, 1, now_iso, "reserved")
                    self._db.execute("COMMIT")
                    self._bump("reservations")
                    self._bump("ownership_wins")
                    record = self.get(effect_id)
                    return Reservation(ReservationOutcome.OWNED, record, True)

                row = self._db.execute(
                    f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?",
                    (effect_id,)).fetchone()
                if row is None:  # pragma: no cover — the row we just conflicted on
                    self._db.execute("ROLLBACK")
                    raise JournalUnhealthy(
                        f"effect {effect_id[:12]} conflicted on insert but does "
                        f"not exist")
                existing = self._row_to_record(row)
                outcome = self._resolve_existing(existing, now, now_iso,
                                                 lease_iso, approval_digest,
                                                 authority_digest, scope_digest)
                self._db.execute("COMMIT")
            except (sqlite3.Error, InvalidTransition):
                self._rollback_quietly()
                raise
        return outcome

    def _resolve_existing(self, existing: EffectRecord, now: datetime,
                          now_iso: str, lease_iso: str, approval_digest: str,
                          authority_digest: str, scope_digest: str) -> Reservation:
        """Decide what an ALREADY-PRESENT row means for this caller.

        Runs inside the caller's write transaction. Every branch here is the
        heart of the milestone, so each one says which crash window it answers.
        """
        state = existing.state
        eid = existing.effect_id

        # P4/P5 — the effect is proven done. Recovery, never re-execution.
        if state in _PROVEN_COMMITTED:
            self._bump("durable_dedupe_hits")
            return Reservation(ReservationOutcome.ALREADY_COMMITTED, existing, False)

        # Already resolved as never-having-happened: a fresh attempt is
        # legitimate and takes ownership again.
        if state in (EffectState.FAILED_BEFORE_EFFECT,
                     EffectState.FAILED_OBSERVED,
                     EffectState.RECONCILED_NOT_EXECUTED):
            return self._take_over(existing, state, now_iso, lease_iso,
                                   approval_digest, authority_digest,
                                   scope_digest, ReservationOutcome.OWNED,
                                   "retry after a non-effect outcome")

        # The P2/P3 window, already classified by someone. Blocked until
        # reconciliation says otherwise — a caller asking again is not evidence.
        if state is EffectState.INDETERMINATE:
            self._bump("indeterminate_blocked")
            outcome = (ReservationOutcome.RECONCILE_REQUIRED
                       if existing.durability_class is EffectDurabilityClass.RECONCILABLE
                       else ReservationOutcome.INDETERMINATE)
            return Reservation(outcome, existing, False)

        expired = self.lease_expired(existing, now)
        if not expired:
            # A live owner holds it — in this process or another one. Nothing to
            # decide; the caller waits or reports.
            self._bump("reservation_conflicts")
            return Reservation(ReservationOutcome.OWNED_ELSEWHERE, existing, False)

        # ── from here: the owner's lease has expired ────────────────────────
        if state is EffectState.RESERVED:
            # P1. The owner never reached EXECUTING, and EXECUTING is committed
            # durably BEFORE the handler runs, so no external effect can have
            # started. Safe to reclaim, for every durability class.
            self._bump("stale_reservations_reclaimed")
            return self._take_over(existing, state, now_iso, lease_iso,
                                   approval_digest, authority_digest,
                                   scope_digest, ReservationOutcome.RECLAIMED,
                                   "reclaimed a stale pre-effect reservation")

        # state is EXECUTING and the owner is gone: the P2/P3 window, live.
        # Local state CANNOT distinguish "died before the call" from "died after
        # the effect landed". An expired lease is therefore never, on its own,
        # permission to run the tool again (§11/§39).
        self._bump("stale_executing_owners")
        if existing.durability_class in _REPLAYABLE_AFTER_AMBIGUITY:
            # IDEMPOTENT converges; IDEMPOTENT_WITH_KEY replays under the SAME
            # derived key and is deduplicated by the external system. Both are
            # safe to re-run, and both keep the identity's key stable.
            return self._take_over(existing, state, now_iso, lease_iso,
                                   approval_digest, authority_digest,
                                   scope_digest, ReservationOutcome.RECLAIMED,
                                   f"replay permitted: "
                                   f"{existing.durability_class.value}")

        # RECONCILABLE and NON_REPLAYABLE: the effect's occurrence is unproven
        # and this module will not guess. Both become INDETERMINATE; only the
        # RECONCILABLE one has anywhere further to go.
        self._transition_locked(
            existing, EffectState.INDETERMINATE, now_iso,
            failure_class="owner_lost_while_executing",
            recovery_note=("owner lease expired while EXECUTING; the effect may "
                           "or may not have happened"))
        self._bump("indeterminate_effects")
        updated = self._row_to_record(self._db.execute(
            f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?", (eid,)).fetchone())
        outcome = (ReservationOutcome.RECONCILE_REQUIRED
                   if existing.durability_class is EffectDurabilityClass.RECONCILABLE
                   else ReservationOutcome.INDETERMINATE)
        return Reservation(outcome, updated, False)

    def _take_over(self, existing: EffectRecord, frm: EffectState, now_iso: str,
                   lease_iso: str, approval_digest: str, authority_digest: str,
                   scope_digest: str, outcome: ReservationOutcome,
                   note: str) -> Reservation:
        """Compare-and-swap this caller into ownership of an existing row.

        The ``owner_attempt=?`` and ``state=?`` guards are what make two
        simultaneous reclaimers resolve to one winner: the loser's UPDATE
        matches zero rows because the winner already bumped the attempt.

        The authority, scope and approval digests are OVERWRITTEN with the new
        attempt's, never inherited. A durable row must not be able to lend a
        later attempt an approval it did not obtain (§43/§44).
        """
        if (frm is EffectState.EXECUTING
                and existing.durability_class not in _REPLAYABLE_AFTER_AMBIGUITY):
            # Defence in depth against a future caller reaching this helper by a
            # path that skipped the class check. Taking over an EXECUTING row is
            # re-running an effect whose occurrence is unknown; only a class that
            # is safe to repeat may do it (§12/§39).
            raise InvalidTransition(
                f"refusing to reclaim an EXECUTING "
                f"{existing.durability_class.value} effect: its outcome is "
                f"unknown and a replay could duplicate it")
        cur = self._db.execute(
            "UPDATE effects SET owner_instance_id=?, owner_attempt=owner_attempt+1, "
            "state=?, lease_expires_at=?, state_changed_at=?, failure_class='', "
            "recovery_note=?, approval_digest=?, authority_digest=?, scope_digest=? "
            "WHERE effect_id=? AND state=? AND owner_attempt=?",
            (self._instance_id, EffectState.RESERVED.value, lease_iso, now_iso,
             note[:200], approval_digest, authority_digest, scope_digest,
             existing.effect_id, frm.value, existing.owner_attempt))
        if cur.rowcount != 1:
            # Someone else won the reclaim between our read and this UPDATE.
            self._bump("reservation_conflicts")
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?",
                (existing.effect_id,)).fetchone()
            current = self._row_to_record(row)
            return Reservation(ReservationOutcome.OWNED_ELSEWHERE, current, False)
        self._record_transition(existing.effect_id, frm, EffectState.RESERVED,
                                self._instance_id, existing.owner_attempt + 1,
                                now_iso, note)
        self._bump("reservations")
        self._bump("ownership_wins")
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?",
            (existing.effect_id,)).fetchone()
        return Reservation(outcome, self._row_to_record(row), True)

    def _transition_locked(self, existing: EffectRecord, to: EffectState,
                           now_iso: str, *, failure_class: str = "",
                           recovery_note: str = "", committed_at: str = "",
                           receipt: str = "", expect_owner: "str | None" = None,
                           note: str = "") -> bool:
        """Move *existing* to *to*. Caller holds the write transaction.

        ``expect_owner`` guards the update so a process that has since LOST
        ownership cannot write over the new owner's work — the classic
        late-waking-owner bug. It is deliberately optional: classifying an
        abandoned row as INDETERMINATE is done BY a non-owner, which is the
        whole point of that transition.
        """
        # The edge is NOT validated here. It is validated in
        # `_record_transition`, which is the single append point every path
        # funnels through — including `reserve` and `_take_over`, which call it
        # directly. A mutation deleting this copy survived the whole campaign
        # precisely because the other one caught everything, and a guard that
        # cannot fail is not defence in depth: it is a second place to keep
        # correct. The UPDATE below runs first and is rolled back with the
        # transaction when the append refuses.
        clauses = ["effect_id=?", "state=?", "owner_attempt=?"]
        if expect_owner is not None:
            clauses.append("owner_instance_id=?")
        cur = self._db.execute(
            "UPDATE effects SET state=?, state_changed_at=?, failure_class=?, "
            "recovery_note=?, committed_at=CASE WHEN ?='' THEN committed_at ELSE ? END, "
            "receipt_digest=CASE WHEN ?='' THEN receipt_digest ELSE ? END "
            f"WHERE {' AND '.join(clauses)}",
            (to.value, now_iso, failure_class[:80], recovery_note[:200],
             committed_at, committed_at, receipt, receipt,
             existing.effect_id, existing.state.value, existing.owner_attempt,
             *([expect_owner] if expect_owner is not None else [])))
        if cur.rowcount != 1:
            return False
        self._record_transition(existing.effect_id, existing.state, to,
                                expect_owner or self._instance_id,
                                existing.owner_attempt, now_iso,
                                note or recovery_note)
        return True

    def _apply(self, effect_id: str, to: EffectState, *, expect_owner: "str | None",
               failure_class: str = "", recovery_note: str = "",
               receipt: str = "", stamp_committed: bool = False,
               note: str = "") -> bool:
        """Open a short write transaction and apply one transition."""
        now_iso = _iso(self._clock())
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                self._bump("db_busy")
                raise JournalUnhealthy(
                    f"the effect journal was locked longer than "
                    f"{self._busy_timeout_ms}ms: {exc}") from exc
            try:
                row = self._db.execute(
                    f"SELECT {_COLUMNS} FROM effects WHERE effect_id=?",
                    (effect_id,)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    return False
                existing = self._row_to_record(row)
                if (expect_owner is not None
                        and existing.owner_instance_id != expect_owner):
                    # This caller has LOST the reservation — another process
                    # reclaimed it while this one was inside the tool. Its write
                    # is a no-op rather than an error: the late-waking owner is a
                    # race, not a programming mistake, and raising here would
                    # surface as a failure of a call whose effect already
                    # happened. The InvalidTransition below stays for the real
                    # owner attempting an edge that does not exist.
                    self._db.execute("ROLLBACK")
                    self._bump("lost_ownership_writes")
                    return False
                ok = self._transition_locked(
                    existing, to, now_iso, failure_class=failure_class,
                    recovery_note=recovery_note,
                    committed_at=now_iso if stamp_committed else "",
                    receipt=receipt, expect_owner=expect_owner, note=note)
                self._db.execute("COMMIT" if ok else "ROLLBACK")
                return ok
            except (sqlite3.Error, InvalidTransition):
                self._rollback_quietly()
                raise

    def mark_executing(self, effect_id: str) -> bool:
        """RESERVED -> EXECUTING, committed durably BEFORE the tool is invoked.

        The ordering is the entire recovery story. Because this row is on disk
        and fsynced before the handler is called, a process that dies during the
        call leaves EXECUTING behind, and a process that dies before the call
        leaves RESERVED. Writing it AFTER the call instead would make every
        crash look like P1 and would authorise a blind retry of a completed
        effect.
        """
        applied = self._apply(effect_id, EffectState.EXECUTING,
                              expect_owner=self._instance_id,
                              note="about to invoke the tool")
        if applied:
            self._bump("executions_started")
        return applied

    def commit(self, effect_id: str, *, receipt) -> bool:
        """EXECUTING -> COMMITTED. The effect is proven to have happened.

        Only the receipt DIGEST is stored. The body is not retained, so recovery
        after a restart returns a truthful envelope rather than a replayed
        response — keeping bodies would make this file a copy of every tool
        result the runtime has ever produced (§52).
        """
        applied = self._apply(effect_id, EffectState.COMMITTED,
                              expect_owner=self._instance_id,
                              receipt=receipt_digest(receipt),
                              stamp_committed=True, note="committed")
        if applied:
            self._bump("commits")
        return applied

    def fail_before_effect(self, effect_id: str, failure_class: str) -> bool:
        """RESERVED -> FAILED_BEFORE_EFFECT. Proven pre-effect; retry is fine.

        Reachable ONLY from RESERVED. There is no edge from EXECUTING, because
        once EXECUTING is durable nothing local can prove the effect did not
        start, and an edge that let a caller say otherwise would be the single
        most dangerous line in this module.
        """
        applied = self._apply(effect_id, EffectState.FAILED_BEFORE_EFFECT,
                              expect_owner=self._instance_id,
                              failure_class=failure_class,
                              recovery_note="refused or aborted before the effect",
                              note="pre-effect failure")
        if applied:
            self._bump("failed_before_effect")
        return applied

    def fail_observed(self, effect_id: str, failure_class: str) -> bool:
        """EXECUTING -> FAILED_OBSERVED: the call returned an error and we saw it.

        NOT the same as FAILED_BEFORE_EFFECT and NOT the same as INDETERMINATE.
        The retry policy attached to this state is inherited unchanged from
        M64.1 and is a named limitation, not a proof — see the milestone
        document.
        """
        applied = self._apply(effect_id, EffectState.FAILED_OBSERVED,
                              expect_owner=self._instance_id,
                              failure_class=failure_class,
                              recovery_note="the tool returned an error to a live caller",
                              note="observed failure")
        if applied:
            self._bump("failed_observed")
        return applied

    def mark_indeterminate(self, effect_id: str, reason: str,
                           *, expect_owner: "str | None" = None) -> bool:
        """Record that the outcome is unknown. Never a synonym for "not done"."""
        applied = self._apply(effect_id, EffectState.INDETERMINATE,
                              expect_owner=expect_owner,
                              failure_class="indeterminate",
                              recovery_note=reason, note="indeterminate")
        if applied:
            self._bump("indeterminate_effects")
        return applied

    def apply_reconciliation(self, effect_id: str,
                             verdict: ReconciliationVerdict) -> bool:
        """Record a reconciliation answer (§15).

        ``UNKNOWN`` writes nothing and returns False: uncertainty is not an
        outcome to persist over an outcome, and turning it into either
        certainty is the failure this whole module is built to avoid.
        """
        if verdict is ReconciliationVerdict.UNKNOWN:
            self._bump("reconciliations_unknown")
            return False
        target = (EffectState.RECONCILED_COMMITTED
                  if verdict is ReconciliationVerdict.CONFIRMED_COMMITTED
                  else EffectState.RECONCILED_NOT_EXECUTED)
        applied = self._apply(
            effect_id, target, expect_owner=None,
            recovery_note=f"reconciliation said {verdict.value}",
            stamp_committed=(verdict is ReconciliationVerdict.CONFIRMED_COMMITTED),
            note="reconciled")
        if applied:
            self._bump("reconciliations")
        return applied

    def release_expired_lease(self, effect_id: str) -> None:
        """Shorten this owner's lease so another process may act sooner.

        Used when an owner is cancelled cleanly. It only ever moves the expiry
        EARLIER and only for a row this instance owns, so it cannot be used to
        evict somebody else.
        """
        now_iso = _iso(self._clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE effects SET lease_expires_at=? WHERE effect_id=? "
                    "AND owner_instance_id=? AND lease_expires_at > ?",
                    (now_iso, effect_id, self._instance_id, now_iso))
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._rollback_quietly()
                raise

    # ── recovery / inspection ───────────────────────────────────────────────
    #: Startup inspection never walks an unbounded journal (§50). A journal
    #: larger than this reports DEGRADED with what it did see rather than
    #: blocking boot behind a full scan.
    MAX_STARTUP_SCAN = 500

    def stale_reservations(self, *, limit: int = MAX_STARTUP_SCAN) -> list[EffectRecord]:
        """Open rows whose owner's lease has expired. Bounded, read-only."""
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM effects WHERE state IN (?,?) "
            "ORDER BY state_changed_at LIMIT ?",
            (EffectState.RESERVED.value, EffectState.EXECUTING.value, limit))
        now = self._clock()
        return [r for r in (self._row_to_record(x) for x in rows)
                if self.lease_expired(r, now)]

    def indeterminate_effects(self, *, limit: int = MAX_STARTUP_SCAN) -> list[EffectRecord]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM effects WHERE state=? "
            "ORDER BY state_changed_at LIMIT ?",
            (EffectState.INDETERMINATE.value, limit))
        return [self._row_to_record(r) for r in rows]

    def startup_recovery(self, *, limit: int = MAX_STARTUP_SCAN) -> dict:
        """Classify what a previous process left behind. CLASSIFY, never run (§49).

        This method executes NOTHING. It reads bounded pages of open rows, moves
        abandoned EXECUTING rows of non-replayable classes into INDETERMINATE so
        an operator can see them, and reports the rest. A startup path that
        re-ran stale effects would turn every crash into a duplicate.
        """
        report = {
            "scanned": 0, "reclaimable_pre_effect": 0,
            "classified_indeterminate": 0, "already_indeterminate": 0,
            "replayable_pending": 0, "truncated": False,
        }
        try:
            stale = self.stale_reservations(limit=limit)
        except sqlite3.Error as exc:
            raise JournalUnhealthy(
                f"startup recovery could not read the journal: "
                f"{type(exc).__name__}") from exc
        report["scanned"] = len(stale)
        report["truncated"] = len(stale) >= limit
        for record in stale:
            if record.state is EffectState.RESERVED:
                # P1: EXECUTING is durable before the tool runs, so this owner
                # provably never invoked anything. Left in place — the next
                # caller for this identity reclaims it. Nothing is executed here.
                report["reclaimable_pre_effect"] += 1
                continue
            if record.durability_class in _REPLAYABLE_AFTER_AMBIGUITY:
                # Safe to re-run when someone asks again; still not run HERE.
                report["replayable_pending"] += 1
                continue
            if self.mark_indeterminate(
                    record.effect_id,
                    "owner did not return; outcome unknown across a restart"):
                report["classified_indeterminate"] += 1
        report["already_indeterminate"] = len(
            self.indeterminate_effects(limit=limit))
        return report

    def status(self) -> dict:
        """BODY-SAFE health and growth (§30/§48/§51). No payload, ever."""
        out: dict = {
            "path": str(self._path),
            "schema_version": SCHEMA_VERSION,
            "instance_id": self._instance_id,
            "lease_s": self._lease_s,
            "busy_timeout_ms": self._busy_timeout_ms,
        }
        try:
            rows = self._db.execute(
                "SELECT state, COUNT(*) AS n FROM effects GROUP BY state")
            by_state = {r["state"]: int(r["n"]) for r in rows}
        except sqlite3.Error as exc:
            out.update({"healthy": False,
                        "integrity": f"unreadable: {type(exc).__name__}"})
            return out
        out["by_state"] = by_state
        out["total"] = sum(by_state.values())
        out["committed"] = (by_state.get(EffectState.COMMITTED.value, 0)
                            + by_state.get(EffectState.RECONCILED_COMMITTED.value, 0))
        out["reserved"] = by_state.get(EffectState.RESERVED.value, 0)
        out["executing"] = by_state.get(EffectState.EXECUTING.value, 0)
        out["indeterminate"] = by_state.get(EffectState.INDETERMINATE.value, 0)
        try:
            out["stale_reservations"] = len(self.stale_reservations())
        except sqlite3.Error:
            out["stale_reservations"] = -1
        integrity = self.integrity_check()
        out["integrity"] = integrity
        out["healthy"] = integrity == "ok"
        out["recovery_required"] = out["indeterminate"] > 0
        try:
            db_bytes = self._path.stat().st_size if self._path.exists() else 0
            for suffix in ("-wal", "-shm"):
                side = Path(str(self._path) + suffix)
                if side.exists():
                    db_bytes += side.stat().st_size
        except OSError:
            db_bytes = -1
        out["db_bytes"] = db_bytes
        out["counters"] = dict(self._counters)
        return out


#: Ordered, explicit migration steps: key N upgrades schema vN -> v(N+1). v1 is
#: the baseline so this is empty; a future version adds a step rather than a
#: "drop and recreate", which would silently discard committed identities.
_MIGRATIONS: dict = {}


# ── per-tool durability policy (§13) ────────────────────────────────────────
#: Audited against the reachable tool surface. Every effectful tool JARVIS can
#: actually dispatch is listed, and anything absent defaults to NON_REPLAYABLE.
#:
#: The audit was deliberately pessimistic. `set_clipboard` is the only local
#: tool whose repeat provably converges (`pyperclip.copy(text)` with the same
#: text leaves the same clipboard, and the text is part of the identity).
#: `write_file` looks idempotent and is NOT: it accepts `mode="a"`, and an
#: append repeated is an append duplicated. A per-tool table has to hold for
#: every argument the tool accepts, so it is NON_REPLAYABLE.
#:
#: No production tool declares IDEMPOTENT_WITH_KEY or RECONCILABLE today —
#: neither protocol has a real external system behind it in this build. Both are
#: implemented and proven against test-owned synthetic tools registered through
#: `register_durability`, which is the same entry point a real tool would use.
_TOOL_DURABILITY: dict[str, EffectDurabilityClass] = {
    "set_clipboard": EffectDurabilityClass.IDEMPOTENT,
}

#: Runtime registrations (tests, plugins). Separate from the audited table so a
#: registration can never silently rewrite an audited classification.
_REGISTERED_DURABILITY: dict[str, EffectDurabilityClass] = {}

#: Reconciliation probes for RECONCILABLE tools, by tool id.
_RECONCILERS: dict = {}


def register_durability(tool_id: str, cls: EffectDurabilityClass) -> None:
    """Declare a tool's durability class. Fails closed on an unknown class."""
    if not isinstance(cls, EffectDurabilityClass):
        raise TypeError(f"{cls!r} is not an EffectDurabilityClass")
    _REGISTERED_DURABILITY[tool_id] = cls


def register_reconciler(tool_id: str, probe) -> None:
    """Attach a bounded 'did this effect happen?' probe to a RECONCILABLE tool.

    *probe* is called as ``probe(effect_id, idempotency_key)`` and must return a
    :class:`ReconciliationVerdict`. Anything else — including an exception — is
    read as ``UNKNOWN``, never as either certainty.
    """
    _RECONCILERS[tool_id] = probe


def unregister_durability(tool_id: str) -> None:
    _REGISTERED_DURABILITY.pop(tool_id, None)
    _RECONCILERS.pop(tool_id, None)


def durability_class(tool_id: str, risk_class=None) -> EffectDurabilityClass:
    """The durability class for *tool_id*.

    Read-only tools are READ_ONLY and are never journalled. Every other tool
    that is not explicitly classified is NON_REPLAYABLE — the fail-closed
    default, because misclassifying an irreversible action as replayable costs
    an operator a duplicate action, while the opposite costs them a manual
    reconciliation (§13).
    """
    if risk_class is not None and getattr(risk_class, "value", None) == "read_only":
        return EffectDurabilityClass.READ_ONLY
    found = _REGISTERED_DURABILITY.get(tool_id)
    if found is not None:
        return found
    return _TOOL_DURABILITY.get(tool_id, EffectDurabilityClass.NON_REPLAYABLE)


def reconcile(tool_id: str, effect_id: str, idempotency_key: str) -> ReconciliationVerdict:
    """Ask the tool's probe whether the effect happened. Bounded, fail-UNKNOWN."""
    probe = _RECONCILERS.get(tool_id)
    if probe is None:
        return ReconciliationVerdict.UNKNOWN
    try:
        verdict = probe(effect_id, idempotency_key)
    except Exception as exc:  # noqa: BLE001 — a broken probe proves nothing
        logger.warning(f"EFFECT_JOURNAL: reconciler for '{tool_id}' raised "
                       f"{type(exc).__name__}; treating as UNKNOWN")
        return ReconciliationVerdict.UNKNOWN
    if isinstance(verdict, ReconciliationVerdict):
        return verdict
    logger.warning(f"EFFECT_JOURNAL: reconciler for '{tool_id}' returned "
                   f"{type(verdict).__name__}; treating as UNKNOWN")
    return ReconciliationVerdict.UNKNOWN


def may_auto_retry(state: EffectState, cls: EffectDurabilityClass) -> bool:
    """Whether an automatic retry is permitted for a *state*/*class* pair (§12).

    INDETERMINATE is not a synonym for SAFE_TO_RETRY. A retry is allowed only
    where the effect is proven not to have started, or the class makes a repeat
    safe on its own terms. RECONCILABLE is deliberately excluded: its route out
    of ambiguity is a reconciliation answer, not a retry.
    """
    if state in _PROVEN_COMMITTED:
        return False
    if state in (EffectState.FAILED_BEFORE_EFFECT,
                 EffectState.RECONCILED_NOT_EXECUTED):
        return True
    if state is EffectState.INDETERMINATE:
        return cls in _REPLAYABLE_AFTER_AMBIGUITY
    return False


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS", "DEFAULT_JOURNAL_PATH", "DEFAULT_LEASE_GRACE_S",
    "DEFAULT_LEASE_S", "SCHEMA_VERSION",
    "DurableEffectJournal", "EffectDurabilityClass", "EffectRecord",
    "EffectState", "ExecutionDisposition", "InvalidTransition",
    "EffectJournalRefused", "JournalUnhealthy", "ReconciliationVerdict",
    "Reservation",
    "ReservationOutcome",
    "action_digest", "args_digest", "canonical_json", "compute_effect_id",
    "configured_journal_path", "configured_lease_grace_s",
    "configured_lease_s", "derive_idempotency_key", "durability_class",
    "journal_enabled", "may_auto_retry", "opaque_digest", "receipt_digest",
    "reconcile", "register_durability", "register_reconciler",
    "runtime_instance_id", "unregister_durability",
]
