"""
core/source_integrity.py — V69 M68C: what was read, what was expected, what changed.

WHAT THIS MODULE IS FOR
=======================
JARVIS can edit code. The claim "I safely applied that change" is only worth
something if the runtime can say, afterwards, WHAT IT READ, WHAT IT EXPECTED,
WHAT IT VALIDATED, WHAT IT ACTUALLY MUTATED and WHAT STATE EXISTS NOW. Before
M68C it could say none of those five things:

* ``_tool_read_file`` truncated at ``max_chars``, appended a marker INSIDE the
  content string, and reported ``chars`` as the length of the truncated text.
  Measured: a 22 890-character file came back as ``chars: 8028`` with no
  ``truncated`` flag, no digest and no path — and a COMPLETE file whose last
  line happens to read ``[...truncado a 8000 chars]`` was byte-indistinguishable
  from it. A caller holding that dict cannot tell a whole file from a tenth of one.

* ``_tool_write_file`` was ``open(p, "w")``. No precondition, no temp file, no
  replace. Measured: JARVIS read ``VERSION = 1``, a human changed the file to
  ``VERSION = 2``, JARVIS wrote its edit, and the human's line was gone with a
  success receipt on top of it. That is a lost update, and the receipt called it
  ``{"written": ..., "bytes": 13}``.

* ``_tool_git_query`` cut ``stdout`` at 3 000 characters with NO marker at all.
  Measured: 31 876 characters of ``git log`` came back as exactly 3 000, and
  nothing in the result said so.

THE RULE THIS MODULE EXISTS TO ENFORCE
======================================
::

    TRUNCATED INPUT  !=  COMPLETE INPUT
    A DISPLAY SNIPPET  !=  A PATCH ARTIFACT
    THE RECEIPT DESCRIBES WHAT HAPPENED; IT DOES NOT GRANT AUTHORITY

Display truncation stays allowed — it is what keeps a terminal readable. What is
refused is MUTATION AUTHORITY derived from a truncated observation. So the split
this module draws everywhere is between the *rendering*, which may be cut, and
the *identity*, which is always computed over the COMPLETE bytes:

    :attr:`SourceIdentity.sha256` is the digest of the whole file, even when the
    text handed back was truncated to 8 000 characters. ``truncated`` then
    describes the rendering, never the digest, and
    :attr:`SourceIdentity.digest_covers` says so in one word.

WHAT IT DELIBERATELY DOES NOT CLAIM (§17)
=========================================
:func:`cas_write_text` performs an ATOMIC REPLACEMENT for whole-file writes:
``os.replace`` is atomic on POSIX and on Windows, so no observer sees a
half-written file. That is the entire claim. It is NOT:

* **crash durability.** The temp file and its directory are fsynced, which is the
  right thing to do, but this repository has never power-cycled a host to prove
  the result. :attr:`WriteReceipt.crash_durability_proven` is therefore ``False``
  and no caller may upgrade it.
* **cross-file transactionality.** One call mutates one path. A caller that
  changes two files makes two independent effects, and nothing here makes them
  one. There is no multi-file apply path in this build (see
  :data:`PATCH_APPLICATION_PATHS`), so there is no cross-file guarantee to make.
* **rollback.** A failed replace leaves the ORIGINAL in place, because the
  original is never opened for writing — that is a property of the temp-and-replace
  shape, and it is tested. It is not a general undo: once a replace succeeds, the
  previous content is gone and only its digest survives in the receipt.

HOW IT RELATES TO M65C/M65D
===========================
The effect journal answers "did this already happen?" — identity and lifecycle
across process death. This module answers a DIFFERENT question: "is the file
still the version I based this edit on?". They compose and do not overlap: the
journal dedupes a repeated call, CAS refuses a stale one.

Rather than inventing a second truth model, the terminal statuses here map onto
M65D's :class:`~core.effect_journal.ExternalOutcome` — the one vocabulary this
repository already uses to say whether an effect provably happened. See
:data:`_WRITE_STATUS_OUTCOME` and :func:`external_outcome_of_write`.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "ABSENT",
    "DIGEST_ALGORITHM",
    "DIGEST_COVERS_COMPLETE",
    "DIGEST_COVERS_NOTHING",
    "MutationMethod",
    "PATCH_APPLICATION_PATHS",
    "PATCH_VALIDATION_SCOPES",
    "PatchValidationScope",
    "SourceIdentity",
    "TransportArtifactClass",
    "TransportIdentity",
    "WriteReceipt",
    "WriteStatus",
    "cas_write_text",
    "digest_bytes",
    "digest_file",
    "external_outcome_of_write",
    "identify_source",
    "identify_transport",
]

#: The one digest this module speaks. Named so a receipt never has to be guessed
#: at, and so a test can assert the algorithm did not quietly become a 64-bit hash.
DIGEST_ALGORITHM = "sha256"

#: Values for :attr:`SourceIdentity.digest_covers`. This field is the whole point
#: of the dataclass: it is the difference between "this digest is the file" and
#: "there is no digest, do not derive authority from me".
DIGEST_COVERS_COMPLETE = "complete_file"
DIGEST_COVERS_NOTHING = "nothing"

#: Precondition sentinel for :func:`cas_write_text`'s ``expected_sha256``: the
#: caller expects NO file at the path. A create-new that finds a file already
#: there is a stale precondition, not a successful overwrite — "the destination
#: unexpectedly appears" is the create-vs-create race, and it has to lose.
ABSENT = "ABSENT"

#: The repository root, used only to render a repo-relative path for receipts.
#: `core/` -> `jarvis/` -> the repo. Matches `core.release_check`'s convention.
_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent

#: How many bytes to digest at a time. A file read for its identity is read for
#: that and nothing else: the bytes are never retained.
_DIGEST_CHUNK = 1 << 20


class PatchValidationScope(str, Enum):
    """What a diff/patch check actually proves. §9.

    The two are not degrees of the same thing. ``SYNTAX_ONLY`` says a text
    resembles a unified diff; it says NOTHING about whether that diff applies to
    any particular tree. A syntax-only check is allowed to exist — it is cheap
    and it catches real garbage — but it may never be reported as, or promoted
    to, execution authority.

    A component claiming :attr:`APPLICABILITY_PROVEN` must have validated the
    patch against a real source state. Nothing in this build does, because
    nothing in this build applies patches; see :data:`PATCH_APPLICATION_PATHS`.
    """

    #: "this text parses as a unified diff." Advisory. Never authority.
    SYNTAX_ONLY = "SYNTAX_ONLY"
    #: "this patch was checked against the declared source state and applies."
    APPLICABILITY_PROVEN = "APPLICABILITY_PROVEN"


#: What each diff/patch-consuming component PROVES. §9.
#:
#: This is a REGISTRY and not a per-component self-declaration, for a reason that
#: is a repository fact rather than a design preference: everything under
#: `jarvis/training_gym/graders/` and `jarvis/training_gym/training/` is FROZEN
#: scientific machinery, byte-identical since 05c043b3, and
#: `test_the_graders_and_the_refusal_detector_are_untouched` enforces that. M68C's
#: first draft added a `VALIDATION_SCOPE` attribute to `DiffBudgetGrader` and broke
#: the freeze — a preregistered grader may not be edited to make a later milestone
#: tidier, however benign the edit looks.
#:
#: So the scope is declared HERE, where it is still one machine-checkable place
#: that no component can quietly disagree with. Keys are dotted import paths; the
#: absent-control suite resolves every one of them, so a typo cannot make the
#: check pass over nothing.
#:
#: `DiffBudgetGrader` reads a unified diff and screens it for budget and tampering.
#: It never checks the diff against a source tree, so it cannot answer "does this
#: apply?" — and its own docstring has always said so in prose. The registry makes
#: that prose checkable without touching the frozen file.
PATCH_VALIDATION_SCOPES: dict[str, PatchValidationScope] = {
    "training_gym.graders.diff_budget_grader.DiffBudgetGrader":
        PatchValidationScope.SYNTAX_ONLY,
    "training_gym.graders.diff_budget_grader.parse_unified_diff":
        PatchValidationScope.SYNTAX_ONLY,
}


#: The patch-APPLICATION paths this build has: none.
#:
#: This is a recorded fact, not an aspiration, and it is what makes M68C's
#: applicability finding NOT_REPRODUCIBLE rather than fixed. JARVIS has exactly
#: one whole-file mutation primitive (``_tool_write_file`` -> :func:`cas_write_text`)
#: and no `git apply`, `patch(1)` or hunk-splicing path at all. ``git_query`` is
#: READ_ONLY and its diff output is a RENDERING (see :class:`TransportIdentity`).
#:
#: An empty tuple is a dangerous thing to assert, because a structural test over
#: zero call sites passes for free (§10). The absent-control suite therefore
#: proves its detector FINDS an injected `git apply` in a synthetic fixture
#: before it reports that the real tree contains none.
PATCH_APPLICATION_PATHS: tuple[str, ...] = ()


class MutationMethod(str, Enum):
    """How the bytes reached the file. Recorded because the guarantees differ."""

    #: temp file in the same directory -> flush -> fsync -> ``os.replace``.
    #: Atomic VISIBILITY. Not crash durability. Not a transaction.
    ATOMIC_REPLACE = "ATOMIC_REPLACE"
    #: ``open(path, "a")``. An append is not a whole-file replacement and cannot
    #: be made one: there is no temp file whose replace would preserve a
    #: concurrent appender's bytes. Declared, never disguised.
    APPEND = "APPEND"
    #: No bytes were written.
    NONE = "NONE"


class WriteStatus(str, Enum):
    """The terminal status of one mutation attempt.

    These separate the three things a naive ``{"written": ...}`` conflated: a
    refusal before the mutation boundary, a mutation that provably landed, and a
    mutation whose outcome is not known. The last one is the only honest answer
    to some failures, and it must stay available — converting it to either
    success or no-effect is a lie in whichever direction it is told.
    """

    #: Preconditions held, bytes verified in place afterwards.
    APPLIED = "APPLIED"
    #: Everything was checked and NOTHING was written, because the caller asked
    #: for a dry run. A validated patch is not an applied patch.
    VALIDATED_NOT_APPLIED = "VALIDATED_NOT_APPLIED"
    #: The file is no longer the version the caller expected. No bytes written.
    REJECTED_STALE = "REJECTED_STALE"
    #: The request itself does not make sense (bad mode, malformed digest).
    REJECTED_INVALID = "REJECTED_INVALID"
    #: Refused by policy — the path is outside the governed roots.
    REJECTED_POLICY = "REJECTED_POLICY"
    #: Failed after validation but strictly BEFORE the mutation boundary: the
    #: temp file could not be created or written. The target is untouched.
    FAILED_BEFORE_MUTATION = "FAILED_BEFORE_MUTATION"
    #: The mutation boundary was crossed and the result cannot be established.
    #: Never collapsed into success or into no-effect.
    PARTIAL_OR_UNKNOWN = "PARTIAL_OR_UNKNOWN"


#: M68C status -> M65D external-outcome vocabulary. One truth model, not two.
#:
#: Every REJECTED_* and FAILED_BEFORE_MUTATION is PROVEN_NOT_EXECUTED because the
#: mutation boundary was demonstrably not crossed: the only writer is a temp file
#: that is unlinked, and the target was never opened for writing.
#: PARTIAL_OR_UNKNOWN is UNKNOWN, which is M65D's whole point — the state after a
#: failure inside the boundary is not knowable from local state alone.
_WRITE_STATUS_OUTCOME: dict[WriteStatus, str] = {
    WriteStatus.APPLIED: "PROVEN_COMMITTED",
    WriteStatus.VALIDATED_NOT_APPLIED: "PROVEN_NOT_EXECUTED",
    WriteStatus.REJECTED_STALE: "PROVEN_NOT_EXECUTED",
    WriteStatus.REJECTED_INVALID: "PROVEN_NOT_EXECUTED",
    WriteStatus.REJECTED_POLICY: "PROVEN_NOT_EXECUTED",
    WriteStatus.FAILED_BEFORE_MUTATION: "PROVEN_NOT_EXECUTED",
    WriteStatus.PARTIAL_OR_UNKNOWN: "UNKNOWN",
}


def external_outcome_of_write(status: "WriteStatus | str"):
    """The M65D :class:`~core.effect_journal.ExternalOutcome` for *status*.

    Imported lazily so this module stays usable (and importable) without the
    journal's sqlite machinery; the mapping itself is a module-level constant so
    a test can assert its totality without importing anything.
    """
    from core.effect_journal import ExternalOutcome

    key = WriteStatus(status)
    return ExternalOutcome(_WRITE_STATUS_OUTCOME[key])


def digest_bytes(payload: bytes) -> str:
    """``sha256`` of *payload*, hex."""
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: "Path | str") -> "str | None":
    """``sha256`` of the COMPLETE file at *path*, or ``None`` if unreadable.

    Chunked, so identifying a large file never holds it in memory. ``None`` means
    "no identity available" and is never a value a precondition can match — a
    missing digest must not silently compare equal to anything.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_DIGEST_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _repo_relative(path: Path) -> "str | None":
    """*path* rendered relative to the repository root, or ``None`` if outside.

    Receipts want the canonical in-repo name; a path outside the repo has none,
    and inventing one would be worse than omitting it.
    """
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class SourceIdentity:
    """Everything a mutation decision is allowed to be based on. §4.

    The invariant that matters: :attr:`sha256` is over the COMPLETE file whenever
    :attr:`digest_covers` is :data:`DIGEST_COVERS_COMPLETE`, regardless of
    whether the text a caller was shown got truncated. So a truncated *display*
    never costs a caller its ability to state a precondition — and it never lets
    it pretend it saw the whole file either, because :attr:`truncated` is a
    separate, out-of-band boolean that no content string can forge.
    """

    #: Absolute, resolved, symlink-followed.
    path: str
    #: Canonical repository-relative path, or ``None`` when outside the repo.
    repo_relative: "str | None"
    exists: bool
    #: Total bytes on disk, or ``None`` when not knowable (absent/unreadable).
    size_bytes: "int | None"
    #: ``sha256`` of the complete file, or ``None`` when there is no identity.
    sha256: "str | None"
    #: :data:`DIGEST_COVERS_COMPLETE` or :data:`DIGEST_COVERS_NOTHING`.
    digest_covers: str
    #: Characters in the full derived text, when the caller rendered one.
    content_chars_total: "int | None" = None
    #: Characters actually handed to the caller.
    content_chars_returned: "int | None" = None
    #: ``True`` when the rendering was cut. OUT OF BAND — this is the field a
    #: mutation path must branch on, never a marker inside the content.
    truncated: bool = False
    #: ``True`` when the text is EXTRACTED (pdf, docx, OCR) rather than the file's
    #: own bytes. Then the digest identifies the file and the text does not, so
    #: "I read this" and "this is what the file says" are different claims.
    content_derived: bool = False
    #: Set when the file type is not supported for reading as text.
    unsupported_reason: "str | None" = None

    @property
    def complete(self) -> bool:
        """``True`` only when the whole source was observed AND identified.

        Both halves are required. A complete read with no digest cannot state a
        precondition, and a digest over a truncated rendering would be a digest
        of the wrong thing — so neither counts as completeness on its own.
        """
        return (self.exists
                and not self.truncated
                and self.digest_covers == DIGEST_COVERS_COMPLETE
                and self.sha256 is not None)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "repo_relative": self.repo_relative,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "digest_algorithm": DIGEST_ALGORITHM,
            "digest_covers": self.digest_covers,
            "content_chars_total": self.content_chars_total,
            "content_chars_returned": self.content_chars_returned,
            "truncated": self.truncated,
            "content_derived": self.content_derived,
            "unsupported_reason": self.unsupported_reason,
            "complete": self.complete,
        }


def identify_source(
    path: "Path | str",
    *,
    content_chars_total: "int | None" = None,
    content_chars_returned: "int | None" = None,
    content_derived: bool = False,
    unsupported_reason: "str | None" = None,
) -> SourceIdentity:
    """Identify the file at *path* — existence, size, complete-file digest.

    Never raises for an absent or unreadable file: that is a state to REPORT, and
    a reader that throws here would push callers back onto guessing. An absent
    file gets ``exists=False``, ``sha256=None`` and
    :data:`DIGEST_COVERS_NOTHING`, which no precondition can match by accident.

    ``truncated`` is DERIVED, never passed in: it is
    ``content_chars_returned < content_chars_total``. A caller cannot hand in a
    ``truncated=False`` that disagrees with the two counts it also handed in.
    """
    target = Path(path)
    try:
        resolved = target.expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        resolved = target

    size: "int | None" = None
    exists = False
    try:
        stat = resolved.stat()
        exists = True
        size = int(stat.st_size)
    except (OSError, ValueError):
        exists = False

    sha = digest_file(resolved) if exists else None
    covers = DIGEST_COVERS_COMPLETE if sha is not None else DIGEST_COVERS_NOTHING

    truncated = bool(
        content_chars_total is not None
        and content_chars_returned is not None
        and content_chars_returned < content_chars_total
    )
    return SourceIdentity(
        path=str(resolved),
        repo_relative=_repo_relative(resolved),
        exists=exists,
        size_bytes=size,
        sha256=sha,
        digest_covers=covers,
        content_chars_total=content_chars_total,
        content_chars_returned=content_chars_returned,
        truncated=truncated,
        content_derived=content_derived,
        unsupported_reason=unsupported_reason,
    )


class TransportArtifactClass(str, Enum):
    """What a block of transported text IS. §6.

    ``git_query`` renders a diff into a result dict with a 3 000-character cap.
    Before M68C that cap was silent, so a 31 876-character diff arrived as 3 000
    characters that looked exactly like a whole one. The fix is not a bigger cap
    — a cap always exists somewhere — it is making the class of the artefact part
    of the artefact.
    """

    #: Every byte the producer emitted is present.
    COMPLETE_PATCH = "COMPLETE_PATCH"
    #: Cut for rendering. Readable by a human, NOT an input to anything that
    #: mutates. There is no path in this build that would accept it as one, and
    #: the absent-control suite is what keeps that true.
    TRUNCATED_DISPLAY_ONLY = "TRUNCATED_DISPLAY_ONLY"


@dataclass(frozen=True)
class TransportIdentity:
    """Identity of transported text, so a snippet can never pass as a patch.

    :attr:`sha256` is over the COMPLETE producer output — the same split as
    :class:`SourceIdentity`. That is deliberate and it is the property a
    mutation would most like to remove: hashing only the retained bytes would
    make a truncated rendering self-consistent, and therefore undetectable.
    """

    #: Bytes the producer actually emitted, or ``None`` if not knowable.
    total_bytes: "int | None"
    #: Bytes retained in the rendering.
    retained_bytes: int
    truncated: bool
    #: ``sha256`` over the COMPLETE output.
    sha256: "str | None"
    #: Where it came from, e.g. ``"git diff"``.
    producer: str
    artifact_class: TransportArtifactClass

    @property
    def usable_as_patch_artifact(self) -> bool:
        """``True`` only for a complete, digested artefact.

        Nothing in this build applies patches, so today this gates nothing. It
        exists so that the day something does, the question is already asked in
        the right place and with the right default.
        """
        return (self.artifact_class is TransportArtifactClass.COMPLETE_PATCH
                and not self.truncated
                and self.sha256 is not None)

    def to_dict(self) -> dict:
        return {
            "total_bytes": self.total_bytes,
            "retained_bytes": self.retained_bytes,
            "truncated": self.truncated,
            "sha256": self.sha256,
            "digest_algorithm": DIGEST_ALGORITHM,
            "digest_covers": (DIGEST_COVERS_COMPLETE if self.sha256
                              else DIGEST_COVERS_NOTHING),
            "producer": self.producer,
            "artifact_class": self.artifact_class.value,
            "usable_as_patch_artifact": self.usable_as_patch_artifact,
        }


def identify_transport(complete: str, retained: str, *,
                       producer: str) -> TransportIdentity:
    """Identify a rendering of *complete* that kept only *retained*.

    ``truncated`` is derived from the two strings, so a caller cannot declare a
    cut rendering complete. The digest is over *complete*.
    """
    complete_bytes = complete.encode("utf-8", errors="replace")
    retained_bytes = retained.encode("utf-8", errors="replace")
    truncated = len(retained) < len(complete)
    return TransportIdentity(
        total_bytes=len(complete_bytes),
        retained_bytes=len(retained_bytes),
        truncated=truncated,
        sha256=digest_bytes(complete_bytes),
        producer=producer,
        artifact_class=(TransportArtifactClass.TRUNCATED_DISPLAY_ONLY if truncated
                        else TransportArtifactClass.COMPLETE_PATCH),
    )


@dataclass
class WriteReceipt:
    """What happened to one path. §8.

    The receipt DESCRIBES; it does not authorise. Nothing reads a receipt to
    decide whether it may write — the preconditions do that, at the mutation
    boundary — so a forged or stale receipt buys an attacker nothing.

    :attr:`before` and :attr:`after` are both present for an
    :attr:`WriteStatus.APPLIED`: success requires evidence of the POST state,
    which means the file is re-identified after the replace and the digest is
    compared against the digest of the bytes that were supposed to land. A
    mismatch is :attr:`WriteStatus.PARTIAL_OR_UNKNOWN`, never success.

    Bodies are never stored. The digests are the proof and they are 64 bytes each;
    keeping the source text would turn every receipt into a copy of the file it
    describes, which is both a secret-spill surface and pointless.
    """

    operation_id: str
    status: WriteStatus
    path: str
    repo_relative: "str | None"
    mutation_method: MutationMethod
    #: Pre-mutation identity. ``None`` only when the path was refused by policy
    #: before it could be identified at all.
    before: "SourceIdentity | None" = None
    #: Post-mutation identity. Present whenever the boundary was crossed.
    after: "SourceIdentity | None" = None
    #: The precondition the caller supplied, verbatim, plus whether it was
    #: re-checked at the boundary (it always is — recorded so a mutation that
    #: moves the check earlier is visible in the receipt as well as in a test).
    precondition: dict = field(default_factory=dict)
    #: Digest of the bytes the caller asked to have on disk, for whole-file
    #: writes. ``None`` for an append, where the intended final content is a
    #: function of bytes this call never saw.
    intended_sha256: "str | None" = None
    bytes_written: int = 0
    #: Atomic VISIBILITY only. See the module docstring, §17.
    atomic_visibility: bool = False
    #: Always ``False``. No power-loss test has ever been run in this repository.
    crash_durability_proven: bool = False
    #: ``True`` when the compare->replace->verify sequence ran under the advisory
    #: directory lock, so a simultaneous writer could not overtake it. ``False``
    #: on a host without ``fcntl`` (Windows) or a filesystem without ``flock``,
    #: where the residual window stands and the receipt says so rather than
    #: implying a serialisation that did not happen.
    serialised: bool = False
    #: Why a non-APPLIED status happened, or why an outcome is unknown.
    reason: "str | None" = None

    @property
    def external_outcome(self) -> str:
        """M65D vocabulary for this status, as a plain string."""
        return _WRITE_STATUS_OUTCOME[self.status]

    @property
    def applied(self) -> bool:
        return self.status is WriteStatus.APPLIED

    def to_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "status": self.status.value,
            "external_outcome": self.external_outcome,
            "path": self.path,
            "repo_relative": self.repo_relative,
            "mutation_method": self.mutation_method.value,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "precondition": dict(self.precondition),
            "intended_sha256": self.intended_sha256,
            "bytes_written": self.bytes_written,
            "atomic_visibility": self.atomic_visibility,
            "crash_durability_proven": self.crash_durability_proven,
            "serialised": self.serialised,
            "reason": self.reason,
        }


_HEX = frozenset("0123456789abcdef")


def _valid_digest(value: str) -> bool:
    """A 64-character lowercase hex string. Nothing else is a sha256.

    Strict on purpose. A loose check would let ``expected_sha256="x"`` through to
    a comparison that can only ever fail, which looks like a stale rejection and
    is actually a malformed request — two findings a caller must be able to tell
    apart.
    """
    return len(value) == 64 and set(value) <= _HEX


def cas_write_text(
    path: "Path | str",
    content: str,
    *,
    mode: str = "w",
    expected_sha256: "str | None" = None,
    dry_run: bool = False,
    encoding: str = "utf-8",
) -> WriteReceipt:
    """Write *content* to *path*, refusing to clobber an unexpected version.

    THE PRECONDITION
    ----------------
    When *expected_sha256* is supplied, the file's CURRENT complete-file digest
    must equal it, or nothing is written and the status is
    :attr:`WriteStatus.REJECTED_STALE`. :data:`ABSENT` means "I expect no file
    here" and loses to a file that appeared. When it is ``None`` the historic
    last-writer-wins behaviour is preserved — the parameter is additive, so no
    existing caller changes meaning — but a supplied precondition is ENFORCED.

    THE TOCTOU WINDOW, AND WHY THIS IS NOT A PREFLIGHT
    --------------------------------------------------
    A check that runs, returns, and is followed later by a mutation is a race
    with a comfortable name. So the digest is read HERE, in this function, with
    no I/O between the comparison and the ``os.replace`` except writing a temp
    file that is not the target. The window is not zero — POSIX offers no
    compare-and-swap on a path — and the residual is recorded honestly rather
    than described away: a writer that changes the file in the microseconds
    between the comparison and the replace still loses its change. What is closed
    is the window that actually bit, which is an edit based on a read from
    seconds or minutes ago.

    ATOMICITY
    ---------
    ``mode="w"`` writes a temp file in the TARGET'S OWN DIRECTORY — same
    filesystem, so ``os.replace`` is a rename and not a copy — flushes, fsyncs,
    and replaces. No observer sees a partial file and a failure leaves the
    original untouched. ``mode="a"`` appends in place and says so
    (:attr:`MutationMethod.APPEND`); it is not dressed up as atomic.

    POST-STATE
    ----------
    After a whole-file replace the file is re-identified and its digest compared
    to the digest of the bytes that were meant to land. Only a match returns
    :attr:`WriteStatus.APPLIED`. Anything else is
    :attr:`WriteStatus.PARTIAL_OR_UNKNOWN`, because the boundary was crossed and
    the result is not what was intended — which is precisely the state that must
    not be reported as either success or no-effect.
    """
    target = Path(path)
    operation_id = uuid.uuid4().hex
    payload = content.encode(encoding)
    intended_whole = digest_bytes(payload) if mode == "w" else None

    def _receipt(status: WriteStatus, *, method=MutationMethod.NONE,
                 before=None, after=None, reason=None, written=0,
                 atomic=False, serialised=False) -> WriteReceipt:
        return WriteReceipt(
            operation_id=operation_id,
            status=status,
            path=str(target),
            repo_relative=_repo_relative(target),
            mutation_method=method,
            before=before,
            after=after,
            precondition={
                "supplied": expected_sha256 is not None,
                "expected_sha256": expected_sha256,
                "digest_algorithm": DIGEST_ALGORITHM,
                # The check is performed inside this function, immediately
                # before the replace. A mutation that hoists it earlier flips
                # this to False and the absent-control suite fails.
                "checked_at_mutation_boundary": expected_sha256 is not None,
            },
            intended_sha256=intended_whole,
            bytes_written=written,
            atomic_visibility=atomic,
            serialised=serialised,
            reason=reason,
        )

    if mode not in ("w", "a"):
        return _receipt(WriteStatus.REJECTED_INVALID,
                        reason=f"mode must be 'w' or 'a', got {mode!r}")
    if expected_sha256 is not None and expected_sha256 != ABSENT \
            and not _valid_digest(expected_sha256):
        return _receipt(WriteStatus.REJECTED_INVALID,
                        reason="expected_sha256 is not a 64-char lowercase "
                               "sha256 hex digest")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _receipt(WriteStatus.FAILED_BEFORE_MUTATION,
                        before=identify_source(target),
                        after=identify_source(target),
                        reason=f"parent directory: {exc}")

    # Everything from the precondition read to the post-state verification runs
    # under one advisory lock. That ORDER is the control: identifying the file
    # before taking the lock would reopen the window the lock exists to close,
    # because the digest could go stale while waiting for it.
    with _serialised_on(target.parent) as serialised:
        return _write_locked(
            target, content, payload, mode, expected_sha256, dry_run, encoding,
            intended_whole, serialised, _receipt)


def _write_locked(target, content, payload, mode, expected_sha256, dry_run,
                  encoding, intended_whole, serialised, _receipt):
    """The critical section: compare -> write temp -> replace -> verify.

    Split out so the lock's scope is one `with` statement and cannot be widened
    or narrowed by accident, and so every `return` below is inside it.
    """
    before = identify_source(target)

    # ── the precondition, at the boundary ────────────────────────────────────
    if expected_sha256 is not None:
        if expected_sha256 == ABSENT:
            if before.exists:
                return _receipt(
                    WriteStatus.REJECTED_STALE, before=before,
                    after=before, serialised=serialised,
                    reason="expected no file at the path; one exists with "
                           f"digest {before.sha256}")
        elif before.sha256 != expected_sha256:
            return _receipt(
                WriteStatus.REJECTED_STALE, before=before, after=before,
                serialised=serialised,
                reason=f"expected {expected_sha256}, found "
                       f"{before.sha256 or '<no file>'}")

    if dry_run:
        # Validated and deliberately NOT applied. The distinction §8 insists on:
        # a dry run that returned APPLIED would make every preflight a lie.
        return _receipt(WriteStatus.VALIDATED_NOT_APPLIED, before=before,
                        after=before, serialised=serialised,
                        reason="dry_run=True: preconditions validated, no bytes "
                               "written")

    if mode == "a":
        # An append cannot be atomic and is not claimed to be. If it raises, the
        # boundary may already have been crossed for part of the payload, so the
        # honest answer is PARTIAL_OR_UNKNOWN and not "nothing happened".
        try:
            with open(target, "a", encoding=encoding) as handle:
                handle.write(content)
        except OSError as exc:
            after = identify_source(target)
            crossed = after.sha256 != before.sha256
            return _receipt(
                WriteStatus.PARTIAL_OR_UNKNOWN if crossed
                else WriteStatus.FAILED_BEFORE_MUTATION,
                method=MutationMethod.APPEND, before=before, after=after,
                serialised=serialised, reason=f"append failed: {exc}")
        after = identify_source(target)
        return _receipt(WriteStatus.APPLIED, method=MutationMethod.APPEND,
                        before=before, after=after, written=len(payload),
                        serialised=serialised)

    # ── whole-file replacement ───────────────────────────────────────────────
    # Everything up to os.replace is PRE-boundary: the target is never opened for
    # writing, so a failure here provably cannot have touched it.
    tmp_path: "str | None" = None
    try:
        handle_fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".m68c-tmp")
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _unlink(tmp_path)
        return _receipt(WriteStatus.FAILED_BEFORE_MUTATION, before=before,
                        after=identify_source(target), serialised=serialised,
                        reason=f"temp file: {exc}")

    try:
        os.replace(tmp_path, str(target))
        tmp_path = None
    except OSError as exc:
        _unlink(tmp_path)
        return _receipt(WriteStatus.FAILED_BEFORE_MUTATION, before=before,
                        after=identify_source(target), serialised=serialised,
                        reason=f"atomic replace failed: {exc}")

    # Directory fsync: the right thing to do for durability, and NOT evidence of
    # it — see crash_durability_proven. Best-effort because some filesystems
    # refuse an fsync on a directory fd and that must not fail a landed write.
    _fsync_dir(target.parent)

    # ── post-state evidence ─────────────────────────────────────────────────
    after = identify_source(target)
    if after.sha256 != intended_whole:
        return _receipt(
            WriteStatus.PARTIAL_OR_UNKNOWN, method=MutationMethod.ATOMIC_REPLACE,
            before=before, after=after, written=len(payload), atomic=True,
            serialised=serialised,
            reason="post-state digest does not match the intended content; the "
                   "mutation boundary was crossed and the result is unverified")
    return _receipt(WriteStatus.APPLIED, method=MutationMethod.ATOMIC_REPLACE,
                    before=before, after=after, written=len(payload),
                    atomic=True, serialised=serialised)


@contextlib.contextmanager
def _serialised_on(directory: Path):
    """Advisory inter-process lock over *directory*, for compare->replace->verify.

    WHY A LOCK IS NEEDED AT ALL
    ---------------------------
    The digest comparison and the ``os.replace`` are two syscalls, and POSIX
    offers no compare-and-swap on a path. Without a lock, two writers that both
    read the SAME expected digest both pass the comparison and both replace, and
    each one's post-state check can run before the other's replace — so both
    return APPLIED while the file holds one of them. Measured exactly that:
    two threads, one barrier, two APPLIED receipts, one surviving file. An
    APPLIED receipt describing a state that no longer exists is the lie this
    module exists to prevent, so the window is closed rather than documented.

    ``flock`` on the DIRECTORY's own descriptor is used, not a lock FILE: there
    is no artefact to leak, nothing to clean up after a crash, and no race over
    unlinking a lock whose inode another waiter already holds. It is coarse — one
    writer at a time per directory — which is the right trade for a tool that
    writes one file per call.

    HONEST DEGRADATION
    ------------------
    ``fcntl`` does not exist on Windows. There the lock is a no-op and
    :attr:`WriteReceipt.serialised` is ``False``, so the receipt states plainly
    that simultaneous writers were not serialised instead of implying they were.
    The lock is ADVISORY: it binds every writer that goes through this function,
    which is every mutation path JARVIS has, and binds nothing else — a foreign
    process writing the same path is a race no local lock can win.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield False
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:  # pragma: no cover - filesystem without flock
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _unlink(tmp_path: "str | None") -> None:
    """Remove a temp file if one is still ours. Leaked temp files are a finding."""
    if not tmp_path:
        return
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename is on stable storage. Best-effort."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
