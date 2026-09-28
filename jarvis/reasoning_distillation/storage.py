"""reasoning_distillation/storage.py — V69 M67A: the local corpus root, and why it is local.

THE BOUNDARY THIS MODULE ENFORCES
---------------------------------
Historical conversations are the operator's private material. §4 lists what they may
contain — names, paths, credentials, secret-like tokens, private conversations — and draws
one hard line: **raw historical conversations stay LOCAL and OUTSIDE Git**.

This module owns that line. It does two things and refuses to do a third:

  * it lays out a corpus root with the logical areas §4 names, creating them on demand;
  * it REFUSES to operate on a root that Git would track, checked against the live
    repository rather than against a comment claiming the ignore rule exists;
  * it never reads, renders or copies a body. Reading belongs to the adapters, and every
    report written from here is body-free.

WHY THE IGNORE CHECK ASKS GIT
-----------------------------
A ``.gitignore`` line is a claim. ``git check-ignore`` is a measurement. This repository
has already been bitten by the difference — the distinction between a declared control and
an observed one runs through its whole control-plane design — so
:func:`assert_root_is_gitignored` shells out to ``git check-ignore`` and treats an
inconclusive answer as a FAILURE, not as permission. A root outside any repository is
fine (there is no Git to track it); a root inside one that Git does not ignore is refused.

WHY ``raw/`` IS WRITE-ONCE
--------------------------
§6 says source data remains immutable and repeated ingestion is idempotent. Those are the
same requirement seen from two sides: if the raw area can be rewritten, then a source hash
recorded yesterday may address different bytes today, and every derived record's
provenance becomes a claim about a file that no longer exists. :func:`store_raw` therefore
writes content-addressed and refuses to overwrite differing bytes under an existing
address.
"""
from __future__ import annotations

import os
import subprocess  # noqa: S404  # nosec B404 — git query only; see _git()
from dataclasses import dataclass
from pathlib import Path

from training_gym.schemas import SchemaError, canonical_json, sha256_text

#: The default corpus root, relative to the operator's home. Deliberately NOT inside the
#: repository: a root inside the worktree is one `git add -A` away from being committed,
#: and the whole point of §4 is that no such single mistake exists.
DEFAULT_CORPUS_ROOT = Path.home() / ".local" / "reasoning_corpus"

#: The in-repository fallback root, used only when the operator explicitly asks for one.
#: It is gitignored by `.local/` in `.gitignore`, and :func:`assert_root_is_gitignored`
#: verifies that against Git rather than trusting this comment.
REPO_RELATIVE_ROOT = ".local/reasoning_corpus"

#: The logical areas §4 requires. Order is the pipeline order, which is also the order a
#: reviewer reads them in.
AREAS: tuple[str, ...] = (
    "raw",          # immutable, content-addressed source bytes. NEVER committed.
    "normalized",   # canonical conversations, still body-bearing. NEVER committed.
    "distilled",    # typed decision records. Body-free, but not automatically exportable.
    "review",       # everything routed to a human.
    "rejected",     # everything refused, with its reason code. Kept, never deleted.
    "reports",      # body-free audit output: dedupe families, leakage, quality.
    "manifests",    # one manifest per build (§20).
    "holdout",      # the FROZEN partition. Guarded by reasoning_distillation.holdout.
)

#: Areas a development command may read freely. ``holdout`` is absent by construction and
#: its absence here is load-bearing: see :mod:`reasoning_distillation.holdout`.
DEVELOPMENT_READABLE: frozenset[str] = frozenset({
    "raw", "normalized", "distilled", "review", "rejected", "reports", "manifests"})


class StorageError(SchemaError):
    """A storage operation was refused. Never a partial write, never a silent overwrite."""


def _git(*args: str, cwd: Path) -> tuple[int, str]:
    """Run one read-only git query with a fixed argument vector.

    ``shell=False`` (the default for a list argv) is mandatory in this repository and the
    vector is built from literals plus a path, never from an interpolated string.
    """
    try:
        # nosec B603 B607 — the argv is a list of literals plus a path, so `shell=False` holds and
        # no user string is ever interpolated into a command. `git` is resolved from PATH
        # deliberately: pinning an absolute path would break on every platform whose package manager
        # installs it elsewhere, and the repository already depends on `git` being the one on PATH.
        proc = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return (127, f"{type(exc).__name__}: {exc}")
    return (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def in_git_repository(path: Path) -> bool:
    """Whether *path* sits inside a Git worktree at all."""
    probe = path if path.is_dir() else path.parent
    code, out = _git("rev-parse", "--is-inside-work-tree", cwd=probe)
    return code == 0 and out.strip() == "true"


def assert_root_is_gitignored(root: str | Path) -> None:
    """Refuse a corpus root that Git would track (§4, §17).

    Three outcomes, and only one of them proceeds:

      * the root is outside any Git worktree — nothing can track it, so it is fine;
      * the root is inside a worktree and ``git check-ignore`` matches it — fine, and the
        matching rule is what the error message would have quoted;
      * anything else — REFUSED. That includes the case where Git could not be run at all:
        "we could not check" is not "it is ignored", and the whole reason this function
        asks Git is that a ``.gitignore`` line is a claim rather than a measurement.
    """
    path = Path(root)
    probe = path if path.exists() else path.parent
    if not probe.exists():
        probe = Path.cwd()
    if not in_git_repository(probe):
        return
    code, out = _git("check-ignore", "-q", "--no-index", str(path), cwd=probe)
    if code == 0:
        return
    if code == 1:
        raise StorageError(
            f"corpus root {path} is inside a Git worktree and is NOT ignored by Git. "
            f"Raw historical conversations must stay outside Git (§4). Add the root to "
            f"`.gitignore` or choose a root outside the repository; this is refused "
            f"rather than warned about, because a single `git add -A` would commit it")
    raise StorageError(
        f"could not determine whether {path} is ignored by Git (exit {code}: "
        f"{out.strip()[:200]}). An unverifiable ignore status FAILS CLOSED: 'we could not "
        f"check' is not 'it is ignored'")


@dataclass(frozen=True)
class CorpusRoot:
    """A prepared corpus root. Holds paths only — never content, so it has no body to leak.

    ``verified_gitignored`` records that the Git check actually ran and passed for THIS
    root. It is a field rather than a re-derived property so that a caller cannot read a
    cheap truthy default as evidence; :meth:`prepare` is the only thing that sets it True.
    """

    path: Path
    verified_gitignored: bool = False

    @classmethod
    def prepare(cls, root: str | Path | None = None, *, create: bool = True
                ) -> "CorpusRoot":
        """Validate and (optionally) create the corpus root and its areas.

        The Git check runs BEFORE any directory is created. Creating first and checking
        second would leave an untracked-but-not-ignored tree behind on the failure path,
        which is the exact state the check exists to prevent.
        """
        path = Path(root).expanduser() if root is not None else DEFAULT_CORPUS_ROOT
        assert_root_is_gitignored(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
            for area in AREAS:
                (path / area).mkdir(parents=True, exist_ok=True)
        return cls(path=path, verified_gitignored=True)

    def area(self, name: str) -> Path:
        """The path of one logical area. An unknown area is refused, not created."""
        if name not in AREAS:
            raise StorageError(
                f"unknown corpus area {name!r}; known areas are {list(AREAS)}. Areas are "
                f"a closed set so that a typo cannot create a parallel tree nobody audits")
        return self.path / name

    def exists(self) -> bool:
        return self.path.is_dir()

    def to_dict(self) -> dict:
        """Body-free description. The path IS included: it is the operator's own machine
        and they asked for this root, but note that no report written to a committed
        artifact ever includes it — see :mod:`reasoning_distillation.manifests`."""
        return {
            "root": str(self.path),
            "verified_gitignored": self.verified_gitignored,
            "areas": [a for a in AREAS if (self.path / a).is_dir()],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"CorpusRoot(areas={len(AREAS)}, verified_gitignored={self.verified_gitignored})"


def store_raw(root: CorpusRoot, *, content: bytes, suffix: str = ".bin") -> tuple[Path, str]:
    """Write *content* content-addressed into ``raw/``. Idempotent, write-once.

    Returns ``(path, digest)``. A second call with the same bytes is a no-op and returns
    the same pair, which is what makes re-ingestion idempotent at the storage layer rather
    than only at the record layer (§6).

    A differing payload under an existing address would be a sha256 collision; it is
    reported as a refusal rather than resolved, because silently preferring either side
    would make every provenance record that cites the digest ambiguous.
    """
    digest = sha256_text(content.decode("utf-8", "surrogateescape"))
    safe_suffix = "".join(c for c in suffix if c.isalnum() or c == ".")[:16] or ".bin"
    target = root.area("raw") / f"{digest}{safe_suffix}"
    if target.exists():
        if target.read_bytes() != content:
            raise StorageError(
                f"raw/{target.name} already holds DIFFERENT bytes under the same digest. "
                f"Source data is immutable (§6); this is refused rather than resolved")
        return (target, digest)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_bytes(content)
    os.replace(tmp, target)
    return (target, digest)


#: Areas whose contents are TRAVELLING artifacts — read by a human, pasted into a ticket, quoted in
#: a document, potentially committed. Nothing written here may carry a body of any kind.
BODY_FREE_AREAS: frozenset[str] = frozenset({"reports", "manifests", "holdout"})

#: The area written by :func:`store_raw` alone, as content-addressed bytes. It has no
#: ``write_report`` gate because it takes no JSON payload: it stores the source file verbatim, which
#: is the one place private material is SUPPOSED to live. Named so that
#: :data:`BODY_FREE_AREAS`, :data:`RECORD_AREAS` and this constant exhaustively partition
#: :data:`AREAS` — a closure with an unaccounted-for member is not a closure.
RAW_AREAS: frozenset[str] = frozenset({"raw"})

#: Areas that hold DERIVED RECORDS. These legitimately contain spans lifted from source prose — a
#: distilled constraint IS the operator's own sentence — and they stay local and gitignored. They
#: may hold prose; they may never hold a credential. See :func:`write_record`.
RECORD_AREAS: frozenset[str] = frozenset({"distilled", "review", "rejected", "normalized"})


def _atomic_write(target: Path, payload: dict) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_text(canonical_json(payload), encoding="utf-8")
    os.replace(tmp, target)
    return target


def write_report(root: CorpusRoot, area: str, name: str, payload: dict) -> Path:
    """Write one artifact, with the gate appropriate to *area*.

    Two gates, because the two kinds of artifact have different jobs and one gate for both was
    measured wrong in each direction:

      * :data:`BODY_FREE_AREAS` — reports, manifests and the frozen record. STRICTLY body-free:
        scanned for private content AND length-capped, because these are what a human pastes
        somewhere. This is the highest-value place for a body to escape, so the scan belongs here
        rather than at every call site.
      * :data:`RECORD_AREAS` — distilled, review, rejected, normalized. These hold lifted source
        spans by design: a distilled constraint is the operator's own sentence, and a
        length-capped record would be an empty one. They are scanned for SECRETS only.

    Applying the body-free gate to a record area refused to write an entire corpus because one
    rejected record quoted a home path — losing the audit trail for every other record in the
    build. Applying the record gate to a report area would let a manifest carry a paragraph of
    private conversation. Both are refusals worth having, and they are not the same refusal.
    """
    if area in BODY_FREE_AREAS:
        from .privacy import assert_body_free_payload  # local: avoids an import cycle
        assert_body_free_payload(payload, label=f"{area}/{name}")
        return _atomic_write(root.area(area) / name, payload)
    if area in RECORD_AREAS:
        return write_record(root, area, name, payload)
    raise StorageError(
        f"area {area!r} has no declared write gate; every area is either body-free or a record "
        f"area, and an unclassified one must not be written to by default")


def write_record(root: CorpusRoot, area: str, name: str, payload: dict) -> Path:
    """Write a derived record into a local record area. Refuses a payload carrying a SECRET.

    Prose is permitted here and that is the whole difference from :func:`write_report`. What is not
    permitted is a credential: a secret copied into a derived record exists in a second place on
    disk, and the corpus root's protection is that private material lives in exactly one. The
    distiller already withholds secret-bearing turns from span lifting, so reaching this refusal
    means something upstream changed — which is precisely when a second, independent check earns
    its keep.
    """
    from .privacy import PrivacyCategory, scan  # local: avoids an import cycle

    findings = scan(canonical_json(payload))
    secrets = [f for f in findings if f.category.is_secret_like]
    if secrets:
        raise StorageError(
            f"{area}/{name}: refusing to write — the payload carries secret-like content "
            f"({sorted({f.describe() for f in secrets})}). A credential must not be copied into a "
            f"derived record; the distiller withholds secret-bearing turns from lifting, so this "
            f"refusal means an upstream stage changed")
    del PrivacyCategory
    return _atomic_write(root.area(area) / name, payload)


__all__ = [
    "AREAS", "BODY_FREE_AREAS", "DEFAULT_CORPUS_ROOT", "DEVELOPMENT_READABLE", "RAW_AREAS",
    "RECORD_AREAS",
    "REPO_RELATIVE_ROOT", "CorpusRoot", "StorageError", "assert_root_is_gitignored",
    "in_git_repository", "store_raw", "write_record", "write_report",
]
