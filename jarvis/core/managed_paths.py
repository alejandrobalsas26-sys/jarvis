"""core/managed_paths.py — V69 M60.0: the managed application directories.

M60 adds four kinds of on-disk artifact (session journal, redacted diagnostics
bundles, managed-state backups, redacted session exports). Every one of them must
land in a directory the application OWNS, resolved ABSOLUTELY from the module's own
location — never from the process CWD, never from a path that arrived in a command.

WHY THIS EXISTS
---------------
The pre-M60 code mixed two conventions: ``core.operational_store`` resolved
``_JARVIS_DIR / "data"`` absolutely (correct), while ``core.session_manager`` and
``core.session_journal`` used CWD-relative ``logs/...`` (breaks the moment JARVIS is
launched from anywhere but the repo root — exactly what the M60.5 startup/scheduler
deployment targets would do). This module is the single resolver.

DISCIPLINE
----------
  * every public function returns an ABSOLUTE path inside the application tree;
  * no function accepts a caller-supplied directory — only a validated leaf NAME,
    so no operator command and no model output can ever redirect a write;
  * ``safe_leaf`` rejects separators, drive letters, ``..`` and reserved Windows
    device names, so a "filename" can never escape its managed directory;
  * directory creation is lazy, idempotent and failure-tolerant (a read-only tree
    degrades to a reported error, it does not crash the runtime).
"""
from __future__ import annotations

import re
from pathlib import Path

_JARVIS_DIR = Path(__file__).resolve().parent.parent

# Managed subdirectories, relative to the application root. Closed set.
_DATA = "data"
_SESSIONS = "data/sessions"
_DIAGNOSTICS = "data/diagnostics"
_BACKUPS = "data/backups"
_EXPORTS = "data/exports"
# V69 M61.4 — the runtime log tree. Pre-M61 this was the CWD-relative string
# ``"jarvis.log"`` passed straight to ``logger.add``, which produced one 2.7 MB log
# per directory JARVIS had ever been launched from. Same discipline as the rest of
# this module: absolute, application-owned, never caller-supplied.
_LOGS = "logs"

# A leaf name may only contain these characters. Deliberately narrower than the
# filesystem allows: no dots-only names, no spaces, no unicode homoglyphs.
_SAFE_LEAF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


class UnsafeLeafName(ValueError):
    """A proposed file name is not a safe leaf inside a managed directory."""


def app_root() -> Path:
    """The absolute application root (the ``jarvis/`` package directory)."""
    return _JARVIS_DIR


def _resolve(rel: str, *, create: bool) -> Path:
    p = _JARVIS_DIR / rel
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only or full disk must not crash the runtime; the caller's
            # write will fail and be reported honestly by its own error path.
            pass
    return p


def data_dir(*, create: bool = True) -> Path:
    return _resolve(_DATA, create=create)


def sessions_dir(*, create: bool = True) -> Path:
    return _resolve(_SESSIONS, create=create)


def diagnostics_dir(*, create: bool = True) -> Path:
    return _resolve(_DIAGNOSTICS, create=create)


def backups_dir(*, create: bool = True) -> Path:
    return _resolve(_BACKUPS, create=create)


def exports_dir(*, create: bool = True) -> Path:
    return _resolve(_EXPORTS, create=create)


def logs_dir(*, create: bool = True) -> Path:
    """The managed runtime log tree (V69 M61.4). Absolute, never CWD-relative."""
    return _resolve(_LOGS, create=create)


def runtime_log_path(*, create: bool = True) -> Path:
    """The single rotating runtime log file (``logs/jarvis.log``)."""
    return logs_dir(create=create) / "jarvis.log"


def audit_log_path(*, create: bool = True) -> Path:
    """The append-only tactic/shutdown audit trail (``logs/tactic_audit.jsonl``)."""
    return logs_dir(create=create) / "tactic_audit.jsonl"


def continuity_db_path(*, create: bool = True) -> Path:
    """The session-continuity SQLite file (its own store, same proven engine)."""
    return sessions_dir(create=create) / "session_continuity.db"


def log_artifact_path(name: str, *, create: bool = True) -> Path:
    """A validated leaf inside the managed log tree (V69 M61 RC1).

    For the durable artifacts that are NOT the runtime log: append-only action
    audit trails, comparison baselines and small state databases. These were
    ``Path("logs/…")`` constants evaluated at import time, i.e. bound to whatever
    directory the process happened to start in. An audit trail with that property
    is not an audit trail — one host produces a separate file per launch directory
    — and a baseline read from the wrong directory returns "nothing recorded",
    which a drift detector reports as *clean*.

    Pass ``create=False`` on a read so that merely looking for a state file does
    not materialise a directory tree.
    """
    return managed_path(logs_dir(create=create), name)


def logs_subdir(name: str, *, create: bool = True) -> Path:
    """A validated subdirectory of the managed log tree (report/artifact folders)."""
    target = managed_path(logs_dir(create=create), name)
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Same degradation contract as _resolve: a read-only tree surfaces on
            # the caller's write, it does not abort the runtime.
            pass
    return target


def safe_leaf(name: str, *, suffix: str = "") -> str:
    """Validate a proposed leaf file name. Raises :class:`UnsafeLeafName` otherwise.

    Accepts ONLY ``[A-Za-z0-9][A-Za-z0-9._-]{0,95}``: that already excludes ``/``,
    ``\\``, ``:``, ``..`` as a whole name, absolute paths and NUL bytes. Reserved
    Windows device names are rejected on every platform so a bundle written on Linux
    cannot become unopenable on the target host.
    """
    raw = str(name or "").strip()
    if not _SAFE_LEAF_RE.match(raw):
        raise UnsafeLeafName(f"unsafe leaf name: {raw[:40]!r}")
    if raw.startswith(".") or ".." in raw:
        raise UnsafeLeafName(f"unsafe leaf name: {raw[:40]!r}")
    if raw.split(".", 1)[0].lower() in _RESERVED:
        raise UnsafeLeafName(f"reserved device name: {raw[:40]!r}")
    if suffix and not raw.endswith(suffix):
        raw = f"{raw}{suffix}"
    return raw


def managed_path(directory: Path, name: str, *, suffix: str = "") -> Path:
    """Join a validated leaf onto a MANAGED directory and prove containment.

    The containment re-check is not redundant with :func:`safe_leaf`: it is the
    defence that survives a future relaxation of the name pattern.
    """
    leaf = safe_leaf(name, suffix=suffix)
    base = directory.resolve()
    target = (base / leaf).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:  # pragma: no cover — unreachable via safe_leaf
        raise UnsafeLeafName(f"escapes managed directory: {leaf!r}") from exc
    return target


def describe() -> dict:
    """Bounded, content-free description of the managed layout (diagnostics).

    Paths are reported RELATIVE to the application root so a bundle never carries
    the operator's home directory.
    """
    return {
        "app_root_name": _JARVIS_DIR.name,
        "data": _DATA,
        "sessions": _SESSIONS,
        "diagnostics": _DIAGNOSTICS,
        "backups": _BACKUPS,
        "exports": _EXPORTS,
        "logs": _LOGS,
    }
