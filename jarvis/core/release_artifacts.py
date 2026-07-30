"""core/release_artifacts.py — V69 M61.8: deterministic artifact checksums.

A release that says "verify the checksums" has to ship a checksum file that a stranger
can actually use, and a verifier that fails for the right reasons. This module is both.

FORMAT
------
``SHA256SUMS`` in the ``sha256sum`` binary-mode layout — ``<64 hex><two spaces><name>``
— one line per release artifact, sorted by name, LF endings, trailing newline. It is
therefore consumable by ``sha256sum -c SHA256SUMS`` and by
``CertUtil``/``Get-FileHash`` comparison, with no JARVIS tooling required.

WHAT THE PARSER REFUSES, AND WHY EACH ONE MATTERS
-------------------------------------------------
Every rule below exists because the failure it prevents is silent:

  * **any path separator, drive letter or ``..`` segment** — a checksum file is a list
    of basenames beside itself. ``../../etc/passwd`` or ``C:\\Windows\\...`` in a
    downloaded ``SHA256SUMS`` turns "verify the release" into "hash a file the
    publisher chose", and a naive verifier would report PASS;
  * **duplicate names** — two lines for one file mean a verifier reports PASS after
    checking whichever it saw last;
  * **a listed file that is absent** — a missing artifact must never verify;
  * **an unlisted artifact present in the directory** — otherwise a second, unsigned
    wheel can sit beside the release and the checksum file still says PASS;
  * **symlinks** — the digest would describe the target, not the artifact, and the
    target can change after publication;
  * **a resolved path outside the release directory** — the belt to the separator
    braces, catching a symlinked parent directory;
  * **MD5 and SHA-1 digest lengths** — a 32- or 40-hex digest is rejected as a
    malformed SHA-256 rather than silently accepted as a weaker one.

Read-only over the artifacts: nothing here extracts, executes, installs or publishes.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: Suffixes that constitute a release artifact for this project.
ARTIFACT_SUFFIXES: tuple[str, ...] = (".whl", ".tar.gz")

CHECKSUM_ALGORITHM = "sha256"
CHECKSUM_FILENAME = "SHA256SUMS"

#: Digest lengths of hashes this project refuses to publish, named so the error can
#: say WHICH weak hash was supplied instead of "malformed".
_WEAK_DIGEST_LENGTHS = {32: "md5", 40: "sha1"}

_LINE_RE = re.compile(r"^(?P<digest>[0-9a-fA-F]+)  (?P<name>\S.*)$")

#: Chunked so a 100 MB sdist never loads into memory.
_READ_CHUNK = 1024 * 1024


class ChecksumError(ValueError):
    """A checksum file or the directory it describes is not acceptable."""


def sha256_file(path: Path) -> str:
    """Lowercase hex SHA-256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_release_artifact(name: str) -> bool:
    return name.endswith(ARTIFACT_SUFFIXES)


def discover_artifacts(directory: Path) -> list[str]:
    """Sorted basenames of the release artifacts in ``directory`` (non-recursive)."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ChecksumError(f"not a directory: {directory.name}")
    return sorted(
        entry.name for entry in directory.iterdir()
        if entry.is_file() and is_release_artifact(entry.name)
    )


def validate_entry_name(name: str) -> None:
    """Refuse anything that is not a plain basename. Raises :class:`ChecksumError`."""
    if not name or name != name.strip():
        raise ChecksumError(f"entry name has surrounding whitespace: {name!r}")
    if "/" in name or "\\" in name:
        raise ChecksumError(f"entry name contains a path separator: {name!r}")
    if re.match(r"^[A-Za-z]:", name):
        raise ChecksumError(f"entry name is an absolute (drive) path: {name!r}")
    if name in (".", "..") or name.startswith(".."):
        raise ChecksumError(f"entry name traverses upward: {name!r}")
    if name.startswith("~"):
        raise ChecksumError(f"entry name references a home directory: {name!r}")
    if "\x00" in name:
        raise ChecksumError("entry name contains a NUL byte")


def _validate_digest(digest: str, name: str) -> str:
    lowered = digest.lower()
    if len(lowered) != 64:
        weak = _WEAK_DIGEST_LENGTHS.get(len(lowered))
        if weak:
            raise ChecksumError(
                f"{name}: {weak} digest supplied; this release publishes "
                f"{CHECKSUM_ALGORITHM} only")
        raise ChecksumError(
            f"{name}: digest is {len(lowered)} hex chars, expected 64")
    if not re.fullmatch(r"[0-9a-f]{64}", lowered):
        raise ChecksumError(f"{name}: digest is not lowercase hexadecimal")
    return lowered


def render_checksums(entries: dict[str, str]) -> str:
    """Render ``{name: digest}`` in the canonical, stable order.

    Every name and digest is validated on the way out, so a malformed checksum file is
    impossible to *produce*, not merely impossible to accept.
    """
    lines = []
    for name in sorted(entries):
        validate_entry_name(name)
        digest = _validate_digest(entries[name], name)
        lines.append(f"{digest}  {name}")
    return "\n".join(lines) + ("\n" if lines else "")


def parse_checksums(text: str) -> dict[str, str]:
    """Parse a checksum file into ``{name: digest}``, refusing every unsafe form."""
    entries: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        match = _LINE_RE.match(line)
        if not match:
            raise ChecksumError(
                f"line {number}: not '<sha256><two spaces><name>'")
        name = match.group("name")
        validate_entry_name(name)
        if name in entries:
            raise ChecksumError(f"line {number}: duplicate entry for {name!r}")
        entries[name] = _validate_digest(match.group("digest"), name)
    return entries


def build_checksums(directory: Path, *, names: list[str] | None = None
                    ) -> dict[str, str]:
    """Hash the release artifacts in ``directory``. Returns ``{name: digest}``.

    ``names`` defaults to every discovered release artifact. An empty result raises:
    a checksum file listing nothing is not evidence, it is an empty file that verifies.
    """
    directory = Path(directory)
    selected = list(names) if names is not None else discover_artifacts(directory)
    if not selected:
        raise ChecksumError(
            f"no release artifact ({', '.join(ARTIFACT_SUFFIXES)}) found to checksum")
    out: dict[str, str] = {}
    for name in selected:
        validate_entry_name(name)
        target = directory / name
        _reject_unsafe_target(directory, target, name)
        out[name] = sha256_file(target)
    return out


def write_checksums(directory: Path, *, names: list[str] | None = None,
                    filename: str = CHECKSUM_FILENAME) -> Path:
    """Write ``SHA256SUMS`` into ``directory`` and verify it immediately.

    The immediate re-verification is the point: a checksum file that has never been
    checked against the bytes it describes is an assertion, not evidence.
    """
    directory = Path(directory)
    entries = build_checksums(directory, names=names)
    path = directory / filename
    path.write_text(render_checksums(entries), encoding="utf-8", newline="\n")
    problems = verify_checksums(directory, filename=filename)
    if problems:
        path.unlink(missing_ok=True)
        raise ChecksumError(
            "freshly written checksums did not verify: " + "; ".join(problems))
    return path


def _reject_unsafe_target(directory: Path, target: Path, name: str) -> None:
    if target.is_symlink():
        raise ChecksumError(f"{name}: is a symlink; a release artifact must be a file")
    resolved_dir = directory.resolve()
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ChecksumError(f"{name}: cannot resolve ({type(exc).__name__})") from exc
    if resolved_dir not in resolved.parents:
        raise ChecksumError(f"{name}: resolves outside the release directory")


def verify_checksums(directory: Path, *, filename: str = CHECKSUM_FILENAME,
                     require_complete: bool = True) -> list[str]:
    """Verify every entry. Returns the problems found; empty means verified.

    With ``require_complete`` (the default) an artifact present in the directory but
    absent from the checksum file is a problem too — a release directory may not carry
    an unlisted distribution.
    """
    directory = Path(directory)
    sums = directory / filename
    if not sums.is_file():
        return [f"{filename} is missing"]

    try:
        entries = parse_checksums(sums.read_text(encoding="utf-8"))
    except ChecksumError as exc:
        return [f"{filename}: {exc}"]
    except OSError as exc:
        return [f"{filename}: unreadable ({type(exc).__name__})"]

    if not entries:
        return [f"{filename} lists no artifact"]

    problems: list[str] = []
    for name, expected in sorted(entries.items()):
        target = directory / name
        if not target.exists():
            problems.append(f"{name}: listed but missing")
            continue
        try:
            _reject_unsafe_target(directory, target, name)
        except ChecksumError as exc:
            problems.append(str(exc))
            continue
        if not target.is_file():
            problems.append(f"{name}: not a regular file")
            continue
        actual = sha256_file(target)
        if actual != expected:
            problems.append(f"{name}: sha256 mismatch")

    if require_complete:
        present = set(discover_artifacts(directory))
        for orphan in sorted(present - set(entries)):
            problems.append(f"{orphan}: present in the release directory but unlisted")
    return problems


def artifact_inventory(directory: Path, *, filename: str = CHECKSUM_FILENAME
                       ) -> dict[str, dict]:
    """``{name: {sha256, size_bytes, kind}}`` for the release-evidence bundle.

    Refuses to report an inventory the checksum file does not verify, so evidence can
    never quote digests that were not checked against the bytes.
    """
    directory = Path(directory)
    problems = verify_checksums(directory, filename=filename)
    if problems:
        raise ChecksumError("; ".join(problems))
    entries = parse_checksums((directory / filename).read_text(encoding="utf-8"))
    inventory: dict[str, dict] = {}
    for name, digest in sorted(entries.items()):
        inventory[name] = {
            "sha256": digest,
            "size_bytes": (directory / name).stat().st_size,
            "kind": "wheel" if name.endswith(".whl") else "sdist",
        }
    return inventory
