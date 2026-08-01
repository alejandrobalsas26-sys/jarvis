"""training_gym/teachers/store.py — V69 M62: where packets and reviews are kept.

WHY THIS EXISTS
---------------
Replay protection is only real if "this packet has already been used" survives the
process that decided it. A set in memory forgets on restart, and a store that a caller
can pass a hand-made object into is a set in memory with extra steps. So the ledger is a
file, and the file is append-only.

Append-only matters more than it sounds. A ledger that rewrote itself could be made to
"forget" a consumed packet by any crash, any concurrent write, or any bug in a
compaction routine — and the failure mode is silent re-approval, which is the one
failure nobody notices. Appending a line, flushing it and fsyncing it is the whole
mechanism; recovery is re-reading the lines.

WHAT IS WRITTEN, AND WHAT IS REFUSED
------------------------------------
  * every filename comes from an id that passed :func:`~training_gym.schemas.
    require_id` — no traversal, no device name, no caller-controlled path segment;
  * every payload is secret-scanned BEFORE it reaches the disk, because a store is
    exactly where an exported artifact stops being reviewable by a human;
  * every write is atomic (temp file in the same directory, ``os.replace``), so a
    crash leaves either the old file or the new one and never a half-written record a
    later reader would parse as truth;
  * a manifest records the SHA-256 of everything written, so tampering with a stored
    review afterwards is detectable;
  * file counts and sizes are bounded — an unbounded review store is an unbounded copy
    of everything ever shown to a teacher.

WHAT IS NEVER WRITTEN
---------------------
API credentials, authorization headers, raw provider responses, hidden reasoning, and
absolute host paths inside exportable metadata. The audit trail records that a call
happened, to which provider, at what estimated cost, and the digest of what came back —
never the material itself.

Generated artifacts live under a gitignored directory. Nothing here is ever committed.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, atomic_write_text, read_jsonl
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    canonical_json,
    require_id,
    sha256_text,
)
from ..task_spec import require_timestamp
from .base import TeacherError, TeacherReviewRecord
from .review_import import ReplayRejected
from .sanitization import assert_clean

#: Directory names under the store root. Fixed, so no caller supplies a path segment.
PACKET_DIR = "packets"
REVIEW_DIR = "reviews"
REJECTED_DIR = "rejected"
QUARANTINE_DIR = "quarantined"
LEDGER_FILE = "consumed_packets.jsonl"
AUDIT_FILE = "provider_audit.jsonl"
MANIFEST_FILE = "manifest.jsonl"

#: Ceilings. A teacher store is an audit trail, not an archive.
MAX_RECORD_BYTES = 262_144
MAX_FILES_PER_DIR = 4_096
MAX_LEDGER_LINES = 100_000
MAX_AUDIT_LINES = 100_000


class StoreError(TeacherError):
    """A store operation was refused. Never a partial write, never a warning."""


# The atomic-write and append-only-ledger primitives live in
# :mod:`training_gym.atomicio`, because the dataset layer needs exactly the same three
# guarantees and a second copy of them would be the one that drifts. These thin wrappers
# keep this module's error type — a caller catching :class:`StoreError` must not have to
# also know about the layer underneath.
def _atomic_write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _append_line(path: Path, payload: Mapping[str, object], *, max_lines: int) -> None:
    try:
        append_jsonl(path, payload, max_lines=max_lines)
    except AtomicIOError as exc:
        raise StoreError(str(exc)) from None


def _read_lines(path: Path) -> list[dict]:
    try:
        return read_jsonl(path)
    except AtomicIOError as exc:
        raise StoreError(str(exc)) from None


# ── the replay ledger ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConsumedPacket:
    """One packet that has been answered. Append-only; never deleted, never edited."""

    packet_id: str
    review_hash: str
    provider: str
    created_at_utc: str

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "packet_id": self.packet_id,
                "review_hash": self.review_hash, "provider": self.provider,
                "created_at_utc": self.created_at_utc}


class PacketLedger:
    """The append-only record of which packets have been answered.

    One packet buys one opinion. Enforced here rather than by a caller, because a caller
    that forgot the check would produce a two-provider "quorum" out of one review filed
    twice — and the record would look perfectly consistent.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def consumed(self) -> dict[str, dict]:
        """Every consumed packet id, mapped to the entry that consumed it."""
        return {str(entry.get("packet_id", "")): entry
                for entry in _read_lines(self._path)
                if str(entry.get("packet_id", ""))}

    def is_consumed(self, packet_id: str) -> bool:
        return require_id(packet_id, "ledger.packet_id") in self.consumed()

    def consume(self, *, packet_id: str, review_hash: str, provider: str,
                created_at_utc: str) -> ConsumedPacket:
        """Record a first use, or raise :class:`ReplayRejected` on a second one."""
        entry = ConsumedPacket(
            packet_id=require_id(packet_id, "ledger.packet_id"),
            review_hash=str(review_hash or "").strip().lower(),
            provider=require_id(provider, "ledger.provider"),
            created_at_utc=require_timestamp(created_at_utc, "ledger.created_at_utc"),
        )
        if len(entry.review_hash) != 64:
            raise StoreError("ledger.review_hash: expected a 64-character digest")
        existing = self.consumed().get(entry.packet_id)
        if existing is not None:
            raise ReplayRejected(
                f"packet {entry.packet_id} was already answered by "
                f"{existing.get('provider', 'an earlier import')!r} at "
                f"{existing.get('created_at_utc', 'an earlier time')}; one packet buys "
                f"one opinion, and a second import would let the same review satisfy a "
                f"two-provider quorum")
        _append_line(self._path, entry.to_dict(), max_lines=MAX_LEDGER_LINES)
        return entry


class InMemoryPacketLedger:
    """A ledger for tests and dry runs. Deliberately NOT the default.

    Exists so a test suite needs no production database and no temp-directory dance,
    and it is separate from :class:`PacketLedger` rather than a mode of it: a store that
    could silently be in-memory is a store whose replay protection could silently be
    nothing.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ConsumedPacket] = {}

    def consumed(self) -> dict[str, dict]:
        return {k: v.to_dict() for k, v in self._entries.items()}

    def is_consumed(self, packet_id: str) -> bool:
        return require_id(packet_id, "ledger.packet_id") in self._entries

    def consume(self, *, packet_id: str, review_hash: str, provider: str,
                created_at_utc: str) -> ConsumedPacket:
        entry = ConsumedPacket(
            packet_id=require_id(packet_id, "ledger.packet_id"),
            review_hash=str(review_hash or "").strip().lower(),
            provider=require_id(provider, "ledger.provider"),
            created_at_utc=require_timestamp(created_at_utc, "ledger.created_at_utc"),
        )
        if entry.packet_id in self._entries:
            raise ReplayRejected(
                f"packet {entry.packet_id} was already answered by "
                f"{self._entries[entry.packet_id].provider!r}; one packet buys one "
                f"opinion, and a second import would let the same review satisfy a "
                f"two-provider quorum")
        self._entries[entry.packet_id] = entry
        return entry


# ── the artifact store ────────────────────────────────────────────────────────
class TeacherArtifactStore:
    """Bounded, atomic, scanned storage for everything the teacher layer produces.

    The root is created on demand and is expected to be gitignored. Metadata returned to
    a caller carries store-RELATIVE paths only: an operator pasting a status report into
    an issue must not be pasting their home directory with it.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def ledger(self) -> PacketLedger:
        return PacketLedger(self._root / LEDGER_FILE)

    # -- writes ----------------------------------------------------------------
    def write_packet(self, packet: object) -> str:
        """Persist an exported packet. Returns its store-relative path."""
        packet_id = require_id(getattr(packet, "packet_id", ""), "packet.packet_id")
        to_dict = getattr(packet, "to_dict", None)
        if not callable(to_dict):
            raise StoreError("store: a packet must implement to_dict(); a raw "
                             "dictionary is never an authoritative packet")
        return self._write_json(PACKET_DIR, f"{packet_id}.json", to_dict())

    def write_review(self, record: TeacherReviewRecord) -> str:
        """Persist an accepted review, keyed by packet and provider."""
        if not isinstance(record, TeacherReviewRecord):
            raise StoreError(f"store: expected a TeacherReviewRecord, got "
                             f"{type(record).__name__}")
        name = f"{record.packet_id}.{require_id(record.provider, 'record.provider')}.json"
        return self._write_json(REVIEW_DIR, name, record.to_dict())

    def write_rejected(self, *, packet_id: str, reason: str,
                       detail: Mapping[str, object] | None = None) -> str:
        """Record a refused response, with its reason and no raw content."""
        return self._write_json(
            REJECTED_DIR, f"{require_id(packet_id, 'packet_id')}.json",
            {"packet_id": packet_id, "reason": str(reason)[:2_000],
             "detail": dict(detail or {})})

    def write_quarantined(self, *, packet_id: str, reason: str,
                          detail: Mapping[str, object] | None = None) -> str:
        """Isolate a review that must never inform a decision."""
        return self._write_json(
            QUARANTINE_DIR, f"{require_id(packet_id, 'packet_id')}.json",
            {"packet_id": packet_id, "reason": str(reason)[:2_000],
             "detail": dict(detail or {})})

    def record_audit_event(self, event: Mapping[str, object]) -> None:
        """Append one provider audit event: that a call happened, never what it said.

        Scanned like every other write. An audit trail is the last place anyone looks for
        a leak, which makes it the first place one survives.
        """
        payload = {SCHEMA_KEY: SCHEMA_VERSION, **{str(k): v for k, v in event.items()}}
        assert_clean(payload, label="teacher audit event")
        _append_line(self._root / AUDIT_FILE, payload, max_lines=MAX_AUDIT_LINES)

    def audit_events(self) -> list[dict]:
        return _read_lines(self._root / AUDIT_FILE)

    def manifest(self) -> list[dict]:
        return _read_lines(self._root / MANIFEST_FILE)

    # -- internals -------------------------------------------------------------
    def _write_json(self, directory: str, filename: str,
                    payload: Mapping[str, object]) -> str:
        # ``require_id`` on every id above already forbids separators and traversal;
        # this is the second lock, in the one place a path is actually assembled.
        if directory not in (PACKET_DIR, REVIEW_DIR, REJECTED_DIR, QUARANTINE_DIR):
            raise StoreError(f"store: unknown directory {directory!r}")
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise StoreError(f"store: unsafe filename {filename!r}")
        text = canonical_json(payload)
        size = len(text.encode("utf-8", "surrogatepass"))
        if size > MAX_RECORD_BYTES:
            raise StoreError(f"store: {filename} is {size} bytes, over the "
                             f"{MAX_RECORD_BYTES}-byte ceiling; a teacher record that "
                             f"large is carrying content, not a verdict")
        assert_clean(payload, label=f"teacher store {filename}")
        target = self._root / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = sum(1 for _ in target.parent.glob("*.json"))
        if existing >= MAX_FILES_PER_DIR and not target.exists():
            raise StoreError(f"store: {directory} already holds {existing} records "
                             f"(ceiling {MAX_FILES_PER_DIR})")
        _atomic_write_text(target, text)
        relative = f"{directory}/{filename}"
        _append_line(self._root / MANIFEST_FILE,
                     {SCHEMA_KEY: SCHEMA_VERSION, "path": relative,
                      "sha256": sha256_text(text), "size_bytes": size},
                     max_lines=MAX_LEDGER_LINES)
        return relative


__all__ = [
    "AUDIT_FILE", "LEDGER_FILE", "MANIFEST_FILE", "MAX_AUDIT_LINES",
    "MAX_FILES_PER_DIR", "MAX_LEDGER_LINES", "MAX_RECORD_BYTES", "PACKET_DIR",
    "QUARANTINE_DIR", "REJECTED_DIR", "REVIEW_DIR", "ConsumedPacket",
    "InMemoryPacketLedger", "PacketLedger", "StoreError", "TeacherArtifactStore",
]
