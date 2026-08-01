"""training_gym/datasets/candidate_store.py — V69 M62 S2d: bounded candidate storage.

WHY THIS EXISTS
---------------
A dataset candidate is the most sensitive artefact the gym produces. It quotes a task
prompt, a model's answer, and the exact bytes a human decided to train on — and unlike a
teacher packet, it is meant to be kept. So the store is the place where "we sanitized it
earlier" stops being an assurance and has to be re-checked: every record is scanned
BEFORE it reaches the disk, because a store is exactly where content stops being
reviewable by a human and starts being an input to a pipeline.

THE TRANSITION LEDGER IS THE POINT
----------------------------------
Candidates are frozen; a state change produces a NEW candidate with a new digest. The
ledger records ``(candidate_id, from_state, to_state, candidate_hash)`` for every move, so
the history of a record is a verifiable chain rather than a field whose current value is
the only thing anyone can see. Append-only, for the reason every ledger in this milestone
is append-only: a rewrite that lost entries would look exactly like a record that had
never moved.

WHAT IS REFUSED BEFORE ANY BYTE IS WRITTEN
------------------------------------------
Unsafe ids, path separators, traversal, drive letters, UNC prefixes, Windows device
names, symlinked targets, hard-linked targets, oversized records, a directory already at
its file ceiling, a store already at its byte ceiling, and any payload the export scanner
objects to. Every one of those is a refusal, never a repair.

Generated data lives under a gitignored root. Nothing written here is ever committed.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, atomic_write_text, read_jsonl
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    canonical_json,
    require_id,
    sha256_text,
    short,
)
from ..task_spec import require_timestamp
from ..teachers.sanitization import assert_clean
from .candidate import CandidateError, CandidateState, DatasetCandidate

#: Directory names under the store root. Fixed, so no caller supplies a path segment.
CANDIDATE_DIR = "candidates"
DATASET_DIR = "datasets"

#: Ledger and manifest filenames. Also fixed.
TRANSITION_FILE = "transitions.jsonl"
MANIFEST_FILE = "manifest.jsonl"
REVIEW_LEDGER_FILE = "reviews.jsonl"
REVOCATION_FILE = "revocations.jsonl"

#: Ceilings. A candidate store is a reviewable working set, not an archive.
MAX_CANDIDATE_BYTES = 262_144
MAX_CANDIDATES = 20_000
MAX_STORE_BYTES = 512 * 1024 * 1024
MAX_LEDGER_LINES = 200_000


class CandidateStoreError(CandidateError):
    """A store operation was refused. Never a partial write, never a warning."""


def _refuse_linked(target: Path) -> None:
    """Refuse a destination that is a symlink or a hard link to something else.

    A symlink planted at ``candidates/<id>.json`` turns an atomic replace into a write
    somewhere the store never chose — and ``os.replace`` onto a symlink replaces the LINK,
    which is the benign half; the dangerous half is a reader following it back out. A hard
    link is the same attack without the marker: two names for one inode, so editing the
    other name edits a record whose manifest digest still says it is intact.
    """
    if target.is_symlink():
        raise CandidateStoreError(
            f"store: {target.name!r} is a symlink; a store never follows one, because a "
            f"link is a caller-chosen destination wearing a store-chosen name")
    if target.exists():
        try:
            links = target.stat().st_nlink
        except OSError:  # pragma: no cover — stat failure is itself a refusal
            raise CandidateStoreError(f"store: cannot stat {target.name!r}") from None
        if links > 1:
            raise CandidateStoreError(
                f"store: {target.name!r} has {links} hard links; a second name for the "
                f"same inode makes a record editable behind its own manifest digest")


class CandidateStore:
    """Bounded, atomic, scanned storage for dataset candidates and their history."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- writes ------------------------------------------------------------------
    def write_candidate(self, candidate: DatasetCandidate) -> str:
        """Persist a candidate atomically. Returns its store-RELATIVE path.

        Relative, because an operator pasting a status report into an issue must not be
        pasting their home directory with it.
        """
        if not isinstance(candidate, DatasetCandidate):
            raise CandidateStoreError(
                f"store: expected a DatasetCandidate, got {type(candidate).__name__}; a "
                f"raw dictionary is never an authoritative candidate")
        record = candidate.to_record()
        return self._write_json(CANDIDATE_DIR, f"{candidate.candidate_id}.json", record)

    def record_transition(self, candidate: DatasetCandidate, *,
                          from_state: CandidateState, actor: str, at: str,
                          reason: str = "") -> dict:
        """Append one transition to the ledger. The candidate's history, verifiably."""
        entry = {
            SCHEMA_KEY: SCHEMA_VERSION,
            "candidate_id": candidate.candidate_id,
            "from_state": require_id(from_state.value, "transition.from_state"),
            "to_state": require_id(candidate.state.value, "transition.to_state"),
            "candidate_hash": candidate.candidate_hash(),
            "actor": require_id(actor, "transition.actor"),
            "at": require_timestamp(at, "transition.at"),
            "reason": str(reason)[:1_000],
        }
        self._append(TRANSITION_FILE, entry)
        return entry

    def record_revocation(self, *, candidate_id: str, candidate_hash: str, reason: str,
                          actor: str, at: str, dataset_versions: tuple[str, ...] = ()
                          ) -> dict:
        """Append one revocation. History is never rewritten; it is annotated.

        The dataset versions that already contain the record are named here rather than
        edited: those manifests stay byte-identical and therefore stay verifiable, and a
        reader learns the record is revoked by consulting the ledger — which is the only
        design in which "still verifies" and "no longer trusted" can both be true.
        """
        if not str(reason or "").strip():
            raise CandidateStoreError("revocation: a revocation must state a reason")
        entry = {
            SCHEMA_KEY: SCHEMA_VERSION,
            "candidate_id": require_id(candidate_id, "revocation.candidate_id"),
            "candidate_hash": str(candidate_hash).strip().lower(),
            "reason": str(reason)[:1_000],
            "actor": require_id(actor, "revocation.actor"),
            "at": require_timestamp(at, "revocation.at"),
            "affected_dataset_versions": sorted(set(dataset_versions)),
        }
        self._append(REVOCATION_FILE, entry)
        return entry

    # -- reads -------------------------------------------------------------------
    def read_candidate(self, candidate_id: str) -> DatasetCandidate:
        """Load one candidate, verifying its stored digest.

        Integrity is checked on the way IN, not only on the way out: a caller that read a
        tampered record and only noticed at export time would already have based a split
        decision on it.
        """
        name = f"{require_id(candidate_id, 'store.candidate_id')}.json"
        path = self._root / CANDIDATE_DIR / name
        if path.is_symlink() or not path.is_file():
            raise CandidateStoreError(f"store: no candidate record {candidate_id!r}")
        import json
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            raise CandidateStoreError(f"store: {name} is unreadable ({exc})") from None
        return DatasetCandidate.from_dict(payload)

    def candidate_ids(self) -> tuple[str, ...]:
        """Every stored candidate id, sorted. Symlinked entries are ignored."""
        directory = self._root / CANDIDATE_DIR
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.json")
                            if p.is_file() and not p.is_symlink()))

    def transitions(self) -> list[dict]:
        return self._read(TRANSITION_FILE)

    def revocations(self) -> list[dict]:
        return self._read(REVOCATION_FILE)

    def revoked_ids(self) -> frozenset[str]:
        """Candidate ids a revocation names. Consulted by every future dataset build."""
        return frozenset(str(e.get("candidate_id", "")) for e in self.revocations()
                         if str(e.get("candidate_id", "")))

    def manifest(self) -> list[dict]:
        return self._read(MANIFEST_FILE)

    def review_ledger_path(self) -> Path:
        """Where :class:`~training_gym.datasets.human_review.HumanReviewLedger` lives."""
        return self._root / REVIEW_LEDGER_FILE

    def verify(self) -> tuple[str, ...]:
        """Re-hash everything the manifest claims. Empty tuple = the store is intact.

        Reports rather than raises, so a CLI can show an operator every damaged record at
        once instead of the first one. A missing file is reported as loudly as a changed
        one — "the record is gone" is not a weaker finding than "the record is different".
        """
        problems: list[str] = []
        for entry in self.manifest():
            relative = str(entry.get("path", ""))
            expected = str(entry.get("sha256", "")).strip().lower()
            path = self._root / relative
            if path.is_symlink():
                problems.append(f"{relative}: became a symlink after it was written")
                continue
            if not path.is_file():
                problems.append(f"{relative}: recorded in the manifest but missing")
                continue
            actual = sha256_text(path.read_text(encoding="utf-8"))
            if actual != expected:
                problems.append(f"{relative}: content digest {short(actual)} does not "
                                f"match the manifest's {short(expected)}")
        return tuple(problems)

    # -- internals ---------------------------------------------------------------
    def _write_json(self, directory: str, filename: str,
                    payload: Mapping[str, object]) -> str:
        # ``require_id`` on every id already forbids separators, traversal, drive
        # letters and device names; this is the second lock, in the one place a path is
        # actually assembled.
        if directory not in (CANDIDATE_DIR, DATASET_DIR):
            raise CandidateStoreError(f"store: unknown directory {directory!r}")
        if ("/" in filename or "\\" in filename or filename.startswith(".")
                or ":" in filename):
            raise CandidateStoreError(f"store: unsafe filename {filename!r}")
        text = canonical_json(payload)
        size = len(text.encode("utf-8", "surrogatepass"))
        if size > MAX_CANDIDATE_BYTES:
            raise CandidateStoreError(
                f"store: {filename} is {size} bytes, over the {MAX_CANDIDATE_BYTES}-byte "
                f"ceiling; a candidate that large is carrying a transcript, not an "
                f"example")
        assert_clean(payload, label=f"dataset store {filename}")

        target = self._root / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = [p for p in target.parent.glob("*.json") if p.is_file()]
        if len(existing) >= MAX_CANDIDATES and not target.exists():
            raise CandidateStoreError(f"store: {directory} already holds "
                                      f"{len(existing)} records (ceiling "
                                      f"{MAX_CANDIDATES})")
        total = sum(p.stat().st_size for p in existing)
        if total + size > MAX_STORE_BYTES:
            raise CandidateStoreError(
                f"store: writing {filename} would take {directory} past the "
                f"{MAX_STORE_BYTES}-byte ceiling")
        _refuse_linked(target)
        atomic_write_text(target, text)

        relative = f"{directory}/{filename}"
        self._append(MANIFEST_FILE, {SCHEMA_KEY: SCHEMA_VERSION, "path": relative,
                                     "sha256": sha256_text(text), "size_bytes": size})
        return relative

    def _append(self, filename: str, entry: Mapping[str, object]) -> None:
        try:
            append_jsonl(self._root / filename, entry, max_lines=MAX_LEDGER_LINES)
        except AtomicIOError as exc:
            raise CandidateStoreError(str(exc)) from None

    def _read(self, filename: str) -> list[dict]:
        try:
            return read_jsonl(self._root / filename)
        except AtomicIOError as exc:
            raise CandidateStoreError(str(exc)) from None


__all__ = [
    "CANDIDATE_DIR", "DATASET_DIR", "MANIFEST_FILE", "MAX_CANDIDATES",
    "MAX_CANDIDATE_BYTES", "MAX_LEDGER_LINES", "MAX_STORE_BYTES", "REVIEW_LEDGER_FILE",
    "REVOCATION_FILE", "TRANSITION_FILE", "CandidateStore", "CandidateStoreError",
]
