"""
core/watch_policy.py — V69 M54.1.3: explicit, path-aware filesystem watch policy.

The live boot proved the watcher had no real policy:

  * `_MONITOR_PATHS` (yara_file_monitor.py:30) hardcoded `Path.home()/"Downloads"`
    recursive. On this host the repo IS `~/Downloads/jarvis_v2/jarvis` — strictly
    inside that root. The module docstring swore "NEVER watch the repo root ...
    infinite YARA loop / QueueFull"; watching Downloads defeated that transitively.
    JARVIS watched itself, so its own writes (vector_store, integrity baseline,
    logs, test artifacts) fed the scanner that made JARVIS write more.
  * `_WATCHED_EXTENSIONS` — the intended narrow executable allowlist — was DEAD
    CODE: one repo-wide hit, its own definition. `_should_scan` only did
    exclusions, so `main.py`, `tests/test_console_v69.py` and `vector_store/*.bin`
    all passed the gate and were really scanned.
  * exclusions used SUBSTRING tests over the whole path (`"log" in str(p).lower()`),
    which both over-matches (any path containing "log" — "catalog", "login") and
    under-matches ("tmp" was never checked at all).

This module replaces that with one explicit, path-aware policy:

  * every comparison is COMPONENT-wise on a normalized (absolute, normcase'd) path,
    so `C:\\foo-bar` never matches root `C:\\foo` and case never matters on Windows;
  * two distinct event classes that must never share one tiny queue blindly —
    SECURITY_SCAN (executables dropped anywhere in an observed security root) and
    CODE_ANALYSIS (source in the explicit `analyze_inbox` drop zone);
  * the repo tree is excluded from observation EXCEPT its `analyze_inbox`, so
    JARVIS editing itself can never start an analysis storm;
  * operator-configurable through core.config (watch_include / watch_exclude /
    watch_queue_size / watch_debounce_ms / watch_security_root).

Pure and dependency-light: no I/O, no watchdog import, fully unit-testable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from core.safe_enqueue import EventPriority

_JARVIS_ROOT = Path(__file__).resolve().parent.parent


class WatchClass(IntEnum):
    """What a filesystem event is FOR. Distinct classes get distinct queues so a
    burst of source-code noise can never starve malware detection."""

    IGNORE = 0
    CODE_ANALYSIS = 1   # source dropped into the explicit inbox
    SECURITY_SCAN = 2   # executable content in an observed security root


class WatchEvent(IntEnum):
    """Normalized filesystem event kind."""

    CREATED = 0
    MODIFIED = 1
    DELETED = 2
    MOVED = 3


# Executable/scriptable content worth a YARA scan. This is the previously-dead
# `_WATCHED_EXTENSIONS` allowlist, now actually applied at the enqueue gate.
_SECURITY_EXTENSIONS = frozenset({
    ".exe", ".dll", ".sys", ".scr", ".com", ".pif", ".msi", ".cpl",
    ".ps1", ".bat", ".cmd", ".vbs", ".js", ".jse", ".hta", ".wsf",
    ".jar", ".lnk",
})

# Source worth analyzing when explicitly dropped into analyze_inbox.
_CODE_EXTENSIONS = frozenset({
    ".py", ".sh", ".ps1", ".js", ".ts", ".go", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".java", ".rb", ".php", ".pl", ".sql",
})

# Directory NAMES excluded wherever they appear inside an observed root. Matched
# per path COMPONENT (never as a substring), so "catalog" != "logs".
_EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox",
    "node_modules", ".venv", "venv", "env", "site-packages",
    ".idea", ".vscode", ".vs",
    "logs", "log",
    "dist", "build", ".eggs", "htmlcov", ".coverage",
    # JARVIS's own generated state — writing these must never feed the watcher.
    "vector_store", "brain", "chroma", "chroma_db", "reports", "sessions",
    "backups", "checkpoints", "migrations",
})

# File suffixes that are never interesting (generated, editor scratch, bytecode).
_EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd", ".swp", ".swo", ".swn", ".bak", ".orig", ".rej",
    ".tmp", ".temp", ".part", ".crdownload", ".partial", ".lock", ".pid",
    ".log", ".jsonl", ".sqlite", ".sqlite3", ".db", ".db-journal", ".db-wal",
    ".db-shm", ".parquet", ".index", ".faiss", ".npy", ".npz", ".pack",
})

# Exact file NAMES JARVIS itself writes — self-output suppression.
_EXCLUDED_FILE_NAMES = frozenset({
    "integrity_baseline.json", "chroma.sqlite3", "alias_registry.json",
    "migration_journal.jsonl", "console.log", "session.log",
})

# Editor scratch patterns matched structurally, not by substring.
_EDITOR_SCRATCH_PREFIXES = ("~$", ".#", "#")
_EDITOR_SCRATCH_SUFFIXES = ("~",)


def _norm(p) -> str:
    """Absolute + normcase'd path. On Windows normcase lowercases AND converts
    '/' to '\\', so comparisons are case- and separator-insensitive."""
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return os.path.normcase(str(p))


def _is_within(child: str, parent: str) -> bool:
    """True if normalized `child` is `parent` or lives under it. Boundary-aware:
    'C:\\foo-bar' is NOT within 'C:\\foo' (a substring check would say it is)."""
    if not parent:
        return False
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _split_roots(raw: str) -> list[str]:
    """Parse an operator-supplied root list. Accepts ',' and os.pathsep."""
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(os.pathsep, ",").split(","):
        chunk = chunk.strip().strip('"').strip("'")
        if chunk:
            parts.append(chunk)
    return parts


@dataclass(frozen=True)
class WatchDecision:
    """The verdict for one raw filesystem event."""

    cls: WatchClass
    priority: EventPriority = EventPriority.LOW
    reason: str = ""
    key: str = ""          # normalized coalescing key (path-derived)

    @property
    def accepted(self) -> bool:
        return self.cls is not WatchClass.IGNORE


@dataclass
class WatchPolicy:
    """Explicit watch roots + ignore rules + event classification.

    Construct via `default_watch_policy()` (reads core.config) or directly with
    explicit roots in tests. Pure: `classify()` performs no I/O.
    """

    security_roots: list[str] = field(default_factory=list)
    code_roots: list[str] = field(default_factory=list)
    excluded_dir_names: frozenset = _EXCLUDED_DIR_NAMES
    excluded_roots: list[str] = field(default_factory=list)
    # The repo tree is excluded from observation (except code_roots inside it) so
    # JARVIS modifying its own files can never start a storm.
    self_root: str | None = None

    def __post_init__(self) -> None:
        self.security_roots = [_norm(r) for r in self.security_roots]
        self.code_roots = [_norm(r) for r in self.code_roots]
        self.excluded_roots = [_norm(r) for r in self.excluded_roots]
        if self.self_root is not None:
            self.self_root = _norm(self.self_root)

    # -- Roots ----------------------------------------------------------------
    def observed_roots(self) -> list[str]:
        """Every root the observer should mount (deduped, order-stable)."""
        seen: set[str] = set()
        out: list[str] = []
        for r in [*self.code_roots, *self.security_roots]:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def root_for(self, path: str) -> str | None:
        """The observed root a path belongs to (most specific first), or None."""
        n = _norm(path)
        best: str | None = None
        for r in self.observed_roots():
            if _is_within(n, r) and (best is None or len(r) > len(best)):
                best = r
        return best

    # -- Classification -------------------------------------------------------
    def classify(self, path: str, event: WatchEvent = WatchEvent.MODIFIED,
                 *, is_directory: bool = False) -> WatchDecision:
        """Decide what (if anything) this raw event is worth. Never raises."""
        try:
            return self._classify(path, event, is_directory)
        except Exception:
            return WatchDecision(WatchClass.IGNORE, reason="classify_error")

    def _classify(self, path: str, event: WatchEvent,
                  is_directory: bool) -> WatchDecision:
        n = _norm(path)
        p = Path(n)

        # Directory-only noise is ignored unless it is a create/delete, which can
        # be meaningful (a dropped folder of payloads / a removed tree).
        if is_directory and event in (WatchEvent.MODIFIED,):
            return WatchDecision(WatchClass.IGNORE, reason="directory_noise", key=n)

        # Operator-declared exclusions win over everything.
        for ex in self.excluded_roots:
            if _is_within(n, ex):
                return WatchDecision(WatchClass.IGNORE, reason="excluded_root", key=n)

        # Which observed root owns this path? Code roots are checked FIRST so an
        # analyze_inbox nested inside the (excluded) repo tree still works.
        code_root = self._match(n, self.code_roots)
        sec_root = self._match(n, self.security_roots)
        if code_root is None and sec_root is None:
            return WatchDecision(WatchClass.IGNORE, reason="outside_roots", key=n)

        # Component-wise directory exclusions, evaluated RELATIVE to the owning
        # root: a noisy component in the root's own prefix (e.g. a user whose
        # home is under a folder named "build") must not disable the whole root.
        owner = code_root or sec_root
        if self._has_excluded_component(n, owner):
            return WatchDecision(WatchClass.IGNORE, reason="excluded_dir", key=n)

        # JARVIS's own tree: ignored unless the path is in an explicit code root
        # (analyze_inbox). This is what stops the self-modification storm.
        if (self.self_root is not None and _is_within(n, self.self_root)
                and code_root is None):
            return WatchDecision(WatchClass.IGNORE, reason="self_tree", key=n)

        if not is_directory and self._is_noise_file(p):
            return WatchDecision(WatchClass.IGNORE, reason="noise_file", key=n)

        # create/delete/move are operator-meaningful; repeated modify is not.
        priority = (EventPriority.LOW if event is WatchEvent.MODIFIED
                    else EventPriority.HIGH)
        suffix = p.suffix.lower()

        if code_root is not None and (is_directory or suffix in _CODE_EXTENSIONS):
            return WatchDecision(WatchClass.CODE_ANALYSIS, priority,
                                 reason="code_inbox", key=n)
        if sec_root is not None and suffix in _SECURITY_EXTENSIONS:
            return WatchDecision(WatchClass.SECURITY_SCAN, priority,
                                 reason="security_extension", key=n)
        return WatchDecision(WatchClass.IGNORE, reason="uninteresting_extension", key=n)

    @staticmethod
    def _match(n: str, roots: list[str]) -> str | None:
        best: str | None = None
        for r in roots:
            if _is_within(n, r) and (best is None or len(r) > len(best)):
                best = r
        return best

    def _has_excluded_component(self, n: str, owner: str | None) -> bool:
        """Component-wise exclusion check, relative to the owning root."""
        rel = n
        if owner and _is_within(n, owner):
            rel = n[len(owner.rstrip(os.sep)):]
        parts = {c for c in Path(rel).parts if c not in ("\\", "/", os.sep)}
        return bool({c.lower() for c in parts} & self.excluded_dir_names)

    @staticmethod
    def _is_noise_file(p: Path) -> bool:
        name = p.name.lower()
        if name in _EXCLUDED_FILE_NAMES:
            return True
        if p.suffix.lower() in _EXCLUDED_SUFFIXES:
            return True
        if name.startswith(_EDITOR_SCRATCH_PREFIXES):
            return True
        if name.endswith(_EDITOR_SCRATCH_SUFFIXES):
            return True
        return False


def default_watch_policy(settings=None) -> WatchPolicy:
    """Build the policy from configuration (core.config is the single source of
    truth — never os.getenv here).

    Secure default:
      * `analyze_inbox` IS observed (the explicit drop zone) as CODE_ANALYSIS;
      * `~/Downloads` is observed ONLY as a SECURITY_SCAN root for executables —
        it is NOT treated as a source-code inbox, so ordinary files there (and the
        whole jarvis repo, which lives under it on this host) never queue;
      * the JARVIS repo tree is excluded from observation except analyze_inbox.
    """
    if settings is None:
        from core.config import settings as _s
        settings = _s

    code_roots = [str(_JARVIS_ROOT / "analyze_inbox")]
    security_roots: list[str] = []
    if getattr(settings, "watch_security_root", True):
        security_roots.append(str(Path.home() / "Downloads"))
    code_roots.extend(_split_roots(getattr(settings, "watch_include", "") or ""))

    excluded_roots: list[str] = []
    extra_dir_names: set[str] = set()
    for item in _split_roots(getattr(settings, "watch_exclude", "") or ""):
        # A bare name excludes that directory everywhere; a path excludes a tree.
        if os.sep in item or (os.altsep and os.altsep in item) or ":" in item:
            excluded_roots.append(item)
        else:
            extra_dir_names.add(item.lower())

    return WatchPolicy(
        security_roots=security_roots,
        code_roots=code_roots,
        excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES | extra_dir_names),
        excluded_roots=excluded_roots,
        self_root=str(_JARVIS_ROOT),
    )
