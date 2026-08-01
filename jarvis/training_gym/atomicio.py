"""training_gym/atomicio.py — V69 M62: the one way the gym touches the disk.

WHY THIS EXISTS
---------------
Two subsystems now persist audit evidence: the teacher layer (packets, reviews, the
replay ledger) and the dataset layer (candidates, transitions, manifests, revocations).
Both need exactly the same three properties, and both would be wrong in exactly the same
way if they implemented them separately:

  * a write is **atomic** — a crash leaves either the old file or the whole new one, and
    never a half-written record a later reader would parse as truth;
  * a ledger is **append-only** — an interrupted append leaves a partial FINAL line,
    which a reader can discard, whereas an interrupted rewrite can lose every prior
    entry. Silent forgetting is the failure mode nobody notices, because the result of
    forgetting a consumed record is that it works;
  * a ledger is **bounded** — an unbounded audit file is an unbounded copy of everything
    the host ever saw.

Two copies of that logic drift. The copy that drifts is always the one nobody is looking
at, so this module is the single implementation and both callers delegate to it.

DISCIPLINE
----------
Pure stdlib. No clock, no environment read, no network. Every payload is encoded with
:func:`~training_gym.schemas.canonical_json`, so a record written here hashes identically
to the same record hashed anywhere else in the gym.

This module does NOT scan for secrets. Screening is the caller's responsibility and
belongs where the caller's vocabulary is — putting it here would give the illusion that
any write through this module is a screened write.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .schemas import SchemaError, canonical_json


class AtomicIOError(SchemaError):
    """A persistence operation was refused. Never a partial write, never a warning."""


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* so a reader sees either the old file or the whole new one.

    The temp file is created in the DESTINATION directory: ``os.replace`` is atomic only
    within a filesystem, and a system temp directory is frequently on another one — a
    cross-device replace fails on POSIX and, worse, some code "helpfully" falls back to a
    copy, which is not atomic at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-",
                                        suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, payload: Mapping[str, object], *, max_lines: int) -> None:
    """Append one canonical JSON line, flushed and fsynced.

    Appending rather than rewriting is the property a ledger's correctness rests on: an
    interrupted append leaves a partial final line, which a reader can discard, whereas
    an interrupted rewrite can lose every prior entry.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and count_lines(path) >= max_lines:
        raise AtomicIOError(f"{path.name}: {max_lines}-line ceiling reached; refusing to "
                            f"grow an unbounded record")
    line = canonical_json(payload)
    if "\n" in line:  # pragma: no cover — canonical_json never emits a newline
        raise AtomicIOError(f"{path.name}: refusing to append a multi-line record")
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def count_lines(path: Path) -> int:
    """Physical line count. Used only to enforce a ceiling before appending."""
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def read_jsonl(path: Path) -> list[dict]:
    """Every complete JSON object line, skipping a torn final one.

    A partial last line is the expected result of a crash mid-append; discarding it is
    correct. A partial line ANYWHERE ELSE is corruption, and is reported — a reader that
    shrugged past it would be silently dropping ledger entries in the middle of the
    record, which is exactly what an attacker who can truncate a file would want.
    """
    if not path.is_file():
        return []
    entries: list[dict] = []
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(raw_lines):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            if index == len(raw_lines) - 1:
                break
            raise AtomicIOError(f"{path.name}: corrupt entry on line "
                                f"{index + 1}") from None
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


__all__ = ["AtomicIOError", "append_jsonl", "atomic_write_text", "count_lines",
           "read_jsonl"]
