"""training_gym/workspace.py — V69 M62: the disposable filesystem an episode gets.

WHY THIS EXISTS
---------------
An episode needs somewhere to write. That directory is the single place where a task
specification (data, possibly authored elsewhere) meets the operator's real
filesystem, so it is the place where a path string stops being text and starts being
a filesystem operation. Every escape this milestone must prevent passes through here:

  * ``../../outside`` and ``..\\..\\outside`` — POSIX and Windows traversal;
  * ``/etc/passwd`` and ``C:\\Windows\\System32`` — absolute paths;
  * ``\\\\server\\share`` — UNC paths, which reach the network without a URL;
  * ``C:notes.txt`` — drive-RELATIVE paths, which resolve against a per-drive CWD;
  * ``report.txt:hidden`` — NTFS alternate data streams, invisible to a directory
    listing and to most scanners;
  * ``CON``, ``NUL``, ``COM1`` — Windows reserved device names, which open a device
    rather than a file no matter what directory they appear in;
  * ``report.txt.`` and ``report.txt `` — Windows silently strips the trailing dot or
    space, so these are aliases for a *different* validated name;
  * a symlink inside the workspace pointing at the operator's home directory.

The rule is: a relative path is validated as TEXT first (:func:`safe_relative_path`,
which never touches the disk and therefore cannot be raced), and only a validated
path is then resolved and proven to be inside the workspace root
(:func:`canonicalize_within`, which resolves symlinks and re-checks containment).
Validation before resolution, and containment after resolution — both, always.

DISCIPLINE
----------
  * the Windows rules are enforced on every platform. A task authored on Linux must
    not be able to produce a fixture that only becomes dangerous on the operator's
    Windows host, and the negative controls must be meaningful in CI on Linux;
  * a workspace is disposable: :meth:`EpisodeWorkspace.destroy` is unconditional and
    idempotent, and the context-manager form destroys on exception too;
  * nothing here reports an absolute host path. :class:`WorkspaceReport` carries a
    label, not a location, because reports are exportable and the operator's home
    directory layout is not.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from .policies import MAX_WORKSPACE_FILES, ArtifactPolicy
from .schemas import RESERVED_DEVICE_NAMES, SchemaError, sha256_file

# ── hostile path vocabulary ───────────────────────────────────────────────────
# ``RESERVED_DEVICE_NAMES`` is owned by :mod:`training_gym.schemas` so identifiers and
# paths cannot drift apart on what counts as a Windows device name.

#: Longest single relative path a fixture or artifact may declare.
MAX_PATH_CHARS = 255
MAX_PATH_SEGMENTS = 16

#: Ceiling for a single staged fixture or collected artifact, mirroring the per-file
#: budget in :mod:`training_gym.policies`.
MAX_FIXTURE_BYTES = 8_388_608

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def safe_relative_path(value: object, field_name: str = "path") -> PurePosixPath:
    """Validate *value* as a relative in-workspace path, or raise :class:`SchemaError`.

    Pure text analysis: no ``stat``, no ``resolve``, no filesystem access at all, so
    the verdict cannot be changed by anything happening on disk between the check and
    the use. Returns the normalised POSIX form.
    """
    if not isinstance(value, (str, PurePosixPath)):
        raise SchemaError(f"{field_name}: expected a path string, got "
                          f"{type(value).__name__}")
    text = str(value)
    if not text.strip():
        raise SchemaError(f"{field_name}: empty path")
    # Deliberately NOT stripped before analysis. Stripping the whole string would
    # erase a trailing space on the FINAL segment, and Windows silently drops that
    # space when opening the file — so ``"report.txt "`` would pass validation and
    # then resolve to the different, already-validated ``"report.txt"``.
    if text != text.strip():
        raise SchemaError(f"{field_name}: path {text!r} has leading or trailing "
                          f"whitespace")
    if len(text) > MAX_PATH_CHARS:
        raise SchemaError(f"{field_name}: path too long ({len(text)} > {MAX_PATH_CHARS})")
    if _CONTROL_RE.search(text):
        raise SchemaError(f"{field_name}: control character in path {text!r}")

    norm = text.replace("\\", "/")
    if norm.startswith("//"):
        raise SchemaError(f"{field_name}: UNC path is refused ({text!r})")
    if _WIN_DRIVE_RE.match(norm):
        # Covers both ``C:/x`` (absolute) and ``C:x`` (drive-relative, which resolves
        # against a hidden per-drive current directory).
        raise SchemaError(f"{field_name}: drive-qualified path is refused ({text!r})")
    if norm.startswith("/"):
        raise SchemaError(f"{field_name}: absolute path is refused ({text!r})")
    if ":" in norm:
        # No legitimate in-workspace relative path contains a colon; an NTFS alternate
        # data stream (``file.txt:hidden``) is the reason this is absolute.
        raise SchemaError(f"{field_name}: colon (drive or alternate data stream) "
                          f"is refused ({text!r})")
    if norm.endswith("/"):
        raise SchemaError(f"{field_name}: path must name a file, not a directory "
                          f"({text!r})")

    segments = norm.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise SchemaError(f"{field_name}: too many path segments "
                          f"({len(segments)} > {MAX_PATH_SEGMENTS})")
    for seg in segments:
        if seg == "":
            raise SchemaError(f"{field_name}: empty path segment in {text!r}")
        if seg in (".", ".."):
            raise SchemaError(f"{field_name}: traversal segment {seg!r} in {text!r}")
        if seg != seg.strip():
            raise SchemaError(f"{field_name}: segment {seg!r} has leading/trailing "
                              f"whitespace (Windows strips it, changing the target)")
        if seg.endswith("."):
            raise SchemaError(f"{field_name}: segment {seg!r} ends with a dot "
                              f"(Windows strips it, changing the target)")
        if seg.split(".", 1)[0].lower() in RESERVED_DEVICE_NAMES:
            raise SchemaError(f"{field_name}: segment {seg!r} is a Windows reserved "
                              f"device name")
    return PurePosixPath(norm)


def canonicalize_within(root: str | Path, relative: object,
                        field_name: str = "path") -> Path:
    """Resolve a validated relative path under *root* and PROVE it stayed inside.

    :func:`safe_relative_path` is applied first, so the input cannot be absolute or a
    traversal; this then resolves symlinks and re-checks containment, which is what
    catches a *link* inside the workspace pointing outside it. Fail-closed: any
    resolution error is a rejection, never a fallback to the unresolved path.
    """
    rel = safe_relative_path(relative, field_name)
    try:
        base = Path(root).resolve(strict=False)
        target = (base / Path(*rel.parts)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SchemaError(f"{field_name}: cannot resolve {rel.as_posix()!r} "
                          f"({type(exc).__name__})") from None
    try:
        target.relative_to(base)
    except ValueError:
        raise SchemaError(f"{field_name}: {rel.as_posix()!r} resolves outside the "
                          f"workspace (symlink or junction escape)") from None
    return target


def matches_allowlist(rel_posix: str, policy: ArtifactPolicy) -> bool:
    """True when a produced file matches at least one artifact allowlist pattern."""
    return any(fnmatch(rel_posix, pattern) for pattern in policy.patterns)


# ── the workspace itself ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class WorkspaceReport:
    """What a workspace contained, in a form that is safe to export.

    ``label`` is a task-scoped name, never a host path: this record ends up in
    trajectories and teacher packets, and the operator's directory layout is private.
    """

    label: str
    input_hashes: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, str] = field(default_factory=dict)
    rejected: tuple[str, ...] = ()
    destroyed: bool = False

    @property
    def input_count(self) -> int:
        return len(self.input_hashes)

    @property
    def output_count(self) -> int:
        return len(self.output_hashes)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "input_hashes": dict(sorted(self.input_hashes.items())),
            "output_hashes": dict(sorted(self.output_hashes.items())),
            "rejected": list(self.rejected),
            "destroyed": self.destroyed,
            "input_count": self.input_count,
            "output_count": self.output_count,
        }


class EpisodeWorkspace:
    """A fresh, disposable directory for exactly one episode attempt.

    Fixtures are COPIED in (never mounted, never referenced in place) so nothing an
    episode does can reach the original repository. Only files matching the task's
    artifact allowlist are collected out. The directory is destroyed unconditionally.
    """

    def __init__(self, *, label: str, artifacts: ArtifactPolicy,
                 parent_dir: str | Path | None = None) -> None:
        self.label = str(label or "episode")
        self.artifacts = artifacts
        self._parent = Path(parent_dir) if parent_dir else None
        self._root: Path | None = None
        self._input_hashes: dict[str, str] = {}
        self._rejected: list[str] = []
        self._destroyed = False

    # -- lifecycle ------------------------------------------------------------
    @property
    def root(self) -> Path:
        if self._root is None:
            raise SchemaError("workspace: not created (call create() first)")
        return self._root

    @property
    def created(self) -> bool:
        return self._root is not None

    def create(self) -> Path:
        """Make the disposable directory. Idempotent within one instance."""
        if self._destroyed:
            raise SchemaError("workspace: already destroyed; create a new one")
        if self._root is None:
            parent = self._parent
            if parent is not None:
                parent.mkdir(parents=True, exist_ok=True)
            self._root = Path(tempfile.mkdtemp(
                prefix=f"gym-{self.label}-", dir=str(parent) if parent else None))
            (self._root / "output").mkdir(parents=True, exist_ok=True)
        return self._root

    def destroy(self) -> None:
        """Remove the directory. Unconditional, idempotent, never raises."""
        root, self._root = self._root, None
        self._destroyed = True
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def __enter__(self) -> "EpisodeWorkspace":
        self.create()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.destroy()

    # -- input ----------------------------------------------------------------
    def stage_fixtures(self, fixtures: Mapping[str, str | Path]) -> dict[str, str]:
        """Copy ``{relative_workspace_path: source_file}`` in and hash each one.

        A source that is a symlink, a directory, missing, or larger than the artifact
        size ceiling is REJECTED and recorded — never copied and never silently
        skipped, because a fixture that did not arrive changes what the task means.
        """
        root = self.create()
        if len(fixtures) > MAX_WORKSPACE_FILES:
            raise SchemaError(f"workspace: {len(fixtures)} fixtures exceeds the "
                              f"ceiling of {MAX_WORKSPACE_FILES}")
        for rel, source in fixtures.items():
            dest = canonicalize_within(root, rel, "fixture")
            src = Path(source)
            if src.is_symlink():
                self._rejected.append(f"{rel}: source is a symlink")
                continue
            if not src.is_file():
                self._rejected.append(f"{rel}: source is not a regular file")
                continue
            try:
                size = src.stat().st_size
            except OSError:
                self._rejected.append(f"{rel}: source is unreadable")
                continue
            if size > MAX_FIXTURE_BYTES:
                self._rejected.append(f"{rel}: source exceeds {MAX_FIXTURE_BYTES} bytes")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)          # copyfile, not copy2: no mode/owner
            self._input_hashes[rel] = sha256_file(dest)
        return dict(self._input_hashes)

    # -- output ---------------------------------------------------------------
    def collect_artifacts(self) -> dict[str, str]:
        """``{relative posix path: sha256}`` for files the allowlist permits out.

        Everything else the episode wrote stays in the workspace and dies with it.
        A file that matches the allowlist but breaks a rule (symlink, oversized,
        hostile name) is recorded as rejected rather than collected.
        """
        if self._root is None:
            return {}
        collected: dict[str, str] = {}
        for path in sorted(self._root.rglob("*"),
                           key=lambda p: p.relative_to(self._root).as_posix()):
            rel = path.relative_to(self._root).as_posix()
            if not path.is_file():
                continue
            if rel in self._input_hashes:
                continue                        # an unmodified input is not an output
            if not matches_allowlist(rel, self.artifacts):
                continue
            if path.is_symlink():
                self._rejected.append(f"{rel}: artifact is a symlink")
                continue
            try:
                safe_relative_path(rel, "artifact")
            except SchemaError as exc:
                self._rejected.append(f"{rel}: {exc}")
                continue
            try:
                size = path.stat().st_size
            except OSError:
                self._rejected.append(f"{rel}: artifact is unreadable")
                continue
            if size > MAX_FIXTURE_BYTES:
                self._rejected.append(f"{rel}: artifact exceeds {MAX_FIXTURE_BYTES} bytes")
                continue
            if len(collected) >= self.artifacts.max_count:
                self._rejected.append(f"{rel}: artifact count ceiling "
                                      f"({self.artifacts.max_count}) reached")
                continue
            collected[rel] = sha256_file(path)
        return collected

    def report(self, *, outputs: dict[str, str] | None = None) -> WorkspaceReport:
        """A sanitized description of this workspace. Contains no host path."""
        return WorkspaceReport(
            label=self.label,
            input_hashes=dict(self._input_hashes),
            output_hashes=dict(outputs if outputs is not None else {}),
            rejected=tuple(self._rejected),
            destroyed=self._destroyed,
        )


__all__ = [
    "MAX_FIXTURE_BYTES", "MAX_PATH_CHARS", "MAX_PATH_SEGMENTS", "RESERVED_DEVICE_NAMES",
    "EpisodeWorkspace", "WorkspaceReport", "canonicalize_within",
    "matches_allowlist", "safe_relative_path",
]
