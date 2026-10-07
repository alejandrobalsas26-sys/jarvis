"""V69 M68C — source identity, atomic CAS writes and truthful write receipts.

Each class below is mapped by `scripts/mutation_campaign_m68c.py`, so a control
that stops working has a named test that goes red rather than a percentage that
drifts.

The reproductions these lock in were all MEASURED on the pristine branch first:

* a 22 890-character file read back as ``chars: 8028`` with no truncation flag,
  no digest and no path — and a COMPLETE file ending in the literal text
  ``[...truncado a 8000 chars]`` produced an indistinguishable result;
* ``VERSION = 1`` read, a human's ``VERSION = 2`` written over it by JARVIS, the
  human's line gone, ``{"written": ..., "bytes": 13}`` returned;
* 31 876 characters of ``git log`` returned as exactly 3 000 with nothing in the
  result saying so.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

# ── V69 M68D (H05): platform capabilities, declared rather than assumed ──────
# An external Windows run reported ten failures in THIS file. None of them were
# source-integrity defects: they were POSIX assumptions. `os.geteuid` does not
# exist on Windows, `chmod` there does not remove read or write access, and
# `fcntl.flock` — which `_serialised_on` needs — is absent, so the receipt
# honestly reports `serialised=False` and an assertion of `True` fails.
#
# These are CAPABILITIES, not skips of the guarantee. The POSIX guarantees are
# still asserted wherever they hold, the honest-degradation contract is asserted
# everywhere (see the M68D H05 suite), and nothing here is weakened to make a
# platform pass.

#: `chmod` actually removes access for this process.
POSIX_MODES_ENFORCED = (
    os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0)

#: `fcntl.flock` is importable, so `cas_write_text` can serialise writers.
FLOCK_AVAILABLE = importlib.util.find_spec("fcntl") is not None

#: A real path that is outside every sandbox root on THIS platform.
OUTSIDE_SANDBOX = ("C:/Windows/System32/drivers/etc/hosts" if os.name == "nt"
                   else "/etc/hostname")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.source_integrity import (  # noqa: E402
    ABSENT,
    SourceIdentity,
    DIGEST_ALGORITHM,
    DIGEST_COVERS_COMPLETE,
    DIGEST_COVERS_NOTHING,
    MutationMethod,
    PatchValidationScope,
    TransportArtifactClass,
    WriteStatus,
    cas_write_text,
    digest_bytes,
    digest_file,
    external_outcome_of_write,
    identify_source,
    identify_transport,
)

READ_CAP = 8000


def _executor():
    """A ToolExecutor with no __init__ side effects — the handlers are pure."""
    from tools.executor import ToolExecutor
    return ToolExecutor.__new__(ToolExecutor)


@pytest.fixture
def sandbox_dir():
    """A directory the handlers will actually accept.

    `tmp_path` lives under /tmp, which `_resolve_within_allowed` refuses — and
    rightly so. A handler-level test therefore has to work inside a real allowed
    root, so it exercises the same containment decision production does instead
    of a weakened one. Everything created here is removed afterwards.
    """
    root = Path.home() / "Downloads" / "m68c-fixture"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            else:
                child.rmdir()
        root.rmdir()


# ══ FINDING A · SOURCE READ COMPLETENESS & IDENTITY ═════════════════════════

class TestReadIdentity:
    """A source read exposes enough identity to prove what was observed."""

    def test_a_small_complete_file_is_identified_as_complete(self, tmp_path):
        target = tmp_path / "small.py"
        target.write_text("x = 1\n")
        ident = identify_source(target, content_chars_total=6,
                               content_chars_returned=6)
        assert ident.exists
        assert ident.truncated is False
        assert ident.complete is True
        assert ident.size_bytes == 6
        assert ident.sha256 == digest_bytes(b"x = 1\n")
        assert ident.digest_covers == DIGEST_COVERS_COMPLETE

    def test_an_empty_file_is_complete_and_has_the_empty_digest(self, tmp_path):
        target = tmp_path / "empty.py"
        target.write_text("")
        ident = identify_source(target, content_chars_total=0,
                               content_chars_returned=0)
        # An empty file EXISTS and has an identity. Treating it as absent is the
        # bug that makes "create if missing" clobber a deliberately blank file.
        assert ident.exists and ident.size_bytes == 0
        assert ident.complete is True
        assert ident.sha256 == digest_bytes(b"")

    def test_an_absent_file_has_no_digest_and_is_not_complete(self, tmp_path):
        ident = identify_source(tmp_path / "nope.py")
        assert ident.exists is False
        assert ident.sha256 is None
        assert ident.digest_covers == DIGEST_COVERS_NOTHING
        assert ident.complete is False

    def test_a_file_deleted_after_it_was_read_loses_its_identity(self, tmp_path):
        target = tmp_path / "gone.py"
        target.write_text("y = 2\n")
        before = identify_source(target)
        target.unlink()
        after = identify_source(target)
        assert before.complete is True
        assert after.exists is False and after.sha256 is None
        assert after.sha256 != before.sha256

    def test_a_file_changed_after_it_was_read_changes_digest(self, tmp_path):
        target = tmp_path / "moved.py"
        target.write_text("v = 1\n")
        first = identify_source(target)
        target.write_text("v = 2\n")
        second = identify_source(target)
        assert first.sha256 != second.sha256

    def test_unicode_and_multibyte_content_is_sized_in_bytes(self, tmp_path):
        target = tmp_path / "uni.py"
        text = "s = 'ñ→🙂'\n"
        target.write_bytes(text.encode("utf-8"))
        ident = identify_source(target)
        # The size is BYTES and the char count is CHARS. Conflating them is how a
        # boundary check passes on ASCII and fails on anything else.
        assert ident.size_bytes == len(text.encode("utf-8"))
        assert ident.size_bytes > len(text)
        assert ident.sha256 == digest_bytes(text.encode("utf-8"))

    def test_a_final_newline_changes_the_digest(self, tmp_path):
        with_nl, without = tmp_path / "a.py", tmp_path / "b.py"
        with_nl.write_bytes(b"z = 1\n")
        without.write_bytes(b"z = 1")
        assert identify_source(with_nl).sha256 != identify_source(without).sha256

    def test_the_digest_is_sha256_named_in_the_payload(self, tmp_path):
        target = tmp_path / "n.py"
        target.write_text("q = 1\n")
        payload = identify_source(target).to_dict()
        assert payload["digest_algorithm"] == DIGEST_ALGORITHM == "sha256"
        assert len(payload["sha256"]) == 64

    @pytest.mark.skipif(not POSIX_MODES_ENFORCED,
                        reason="chmod does not remove read access here "
                               "(Windows, or running as root)")
    def test_an_unreadable_file_yields_no_digest(self, tmp_path):
        target = tmp_path / "locked.py"
        target.write_text("secret = 1\n")
        target.chmod(0o000)
        try:
            assert digest_file(target) is None
        finally:
            target.chmod(0o600)

    def test_completeness_requires_a_digest_and_not_merely_existence(self):
        """A file that EXISTS and was read whole is still not `complete` without
        a digest. Constructed directly, because the only natural way to reach
        this state is an unreadable file — and the two halves of the property
        must be independently load-bearing, or dropping either one is invisible.
        """
        no_digest = SourceIdentity(
            path="/x", repo_relative=None, exists=True, size_bytes=10,
            sha256=None, digest_covers=DIGEST_COVERS_NOTHING,
            content_chars_total=10, content_chars_returned=10)
        assert no_digest.truncated is False and no_digest.exists is True
        assert no_digest.complete is False, \
            "completeness no longer requires an identity to compare against"
        # and the mirror: a digest with a TRUNCATED rendering is not complete
        cut = SourceIdentity(
            path="/x", repo_relative=None, exists=True, size_bytes=10,
            sha256="a" * 64, digest_covers=DIGEST_COVERS_COMPLETE,
            content_chars_total=10, content_chars_returned=4, truncated=True)
        assert cut.complete is False

    def test_the_digest_covers_the_whole_file_not_its_first_chunk(self, tmp_path):
        """Two files sharing a long prefix must not share a digest.

        A digest that stops after the first read would be identical for both,
        and every small-file test in this class would still pass — the files are
        shorter than any plausible chunk.
        """
        import hashlib
        shared = b"# identical prefix, long enough to fill any first read\n" * 64
        first, second = tmp_path / "p1.py", tmp_path / "p2.py"
        first.write_bytes(shared + b"TAIL_A\n")
        second.write_bytes(shared + b"TAIL_B\n")
        assert digest_file(first) != digest_file(second), \
            "the digest does not cover the end of the file"
        assert digest_file(first) == hashlib.sha256(first.read_bytes()).hexdigest()

    def test_a_file_larger_than_one_digest_chunk_hashes_correctly(self, tmp_path):
        """Crosses the chunk loop more than once."""
        import hashlib
        from core.source_integrity import _DIGEST_CHUNK
        target = tmp_path / "multi.bin"
        body = os.urandom(_DIGEST_CHUNK * 2 + 7)
        target.write_bytes(body)
        assert digest_file(target) == hashlib.sha256(body).hexdigest()

    def test_a_repo_path_renders_repo_relative_and_an_outside_one_does_not(
            self, tmp_path):
        inside = identify_source(PACKAGE_ROOT / "core" / "source_integrity.py")
        assert inside.repo_relative == "jarvis/core/source_integrity.py"
        outside = tmp_path / "outside.py"
        outside.write_text("")
        assert identify_source(outside).repo_relative is None


class TestTruncationIsOutOfBand:
    """TRUNCATED INPUT != COMPLETE INPUT, and no content string decides which."""

    def test_truncation_is_derived_from_the_counts_not_declared(self, tmp_path):
        target = tmp_path / "t.py"
        target.write_text("a" * 100)
        cut = identify_source(target, content_chars_total=100,
                              content_chars_returned=40)
        whole = identify_source(target, content_chars_total=100,
                                content_chars_returned=100)
        assert cut.truncated is True and cut.complete is False
        assert whole.truncated is False and whole.complete is True

    def test_a_truncated_read_still_digests_the_COMPLETE_file(self, tmp_path):
        """The property that lets a cut display still state a precondition."""
        target = tmp_path / "big.py"
        body = "b" * 50_000
        target.write_text(body)
        cut = identify_source(target, content_chars_total=50_000,
                              content_chars_returned=READ_CAP)
        assert cut.truncated is True
        assert cut.digest_covers == DIGEST_COVERS_COMPLETE
        assert cut.sha256 == digest_bytes(body.encode())

    def test_exactly_at_the_read_boundary_is_not_truncated(self, sandbox_dir):
        target = sandbox_dir / "edge.py"
        target.write_text("c" * READ_CAP)
        result = _executor()._tool_read_file(str(target), max_chars=READ_CAP)
        assert result["truncated"] is False
        assert result["source"]["complete"] is True

    def test_one_byte_over_the_boundary_is_truncated(self, sandbox_dir):
        target = sandbox_dir / "edge1.py"
        target.write_text("c" * (READ_CAP + 1))
        result = _executor()._tool_read_file(str(target), max_chars=READ_CAP)
        assert result["truncated"] is True
        assert result["source"]["complete"] is False
        assert result["source"]["content_chars_total"] == READ_CAP + 1
        assert result["source"]["content_chars_returned"] == READ_CAP

    def test_the_handler_identity_payload_is_populated_not_an_empty_dict(
            self, sandbox_dir):
        """An empty `source` key satisfies a key-presence check and tells a
        caller nothing. The CONTENTS are the control."""
        target = sandbox_dir / "payload.py"
        target.write_text("value = 1\n")
        source = _executor()._tool_read_file(str(target))["source"]
        assert source["sha256"] == digest_bytes(b"value = 1\n")
        assert source["size_bytes"] == 10
        assert source["exists"] is True
        assert source["complete"] is True
        assert source["digest_algorithm"] == "sha256"
        assert source["digest_covers"] == DIGEST_COVERS_COMPLETE
        assert source["path"] == str(target)
        assert source["truncated"] is False

    def test_the_handler_reports_the_total_not_only_what_it_returned(
            self, sandbox_dir):
        """The measured defect: `chars: 8028` for a 22890-character file."""
        target = sandbox_dir / "huge.py"
        body = "".join(f"# line {i}\n" for i in range(2000))
        target.write_text(body)
        result = _executor()._tool_read_file(str(target))
        assert result["chars"] < len(body)                 # the rendering is cut
        assert result["source"]["content_chars_total"] == len(body)
        assert result["truncated"] is True

    def test_a_complete_file_mimicking_the_marker_is_still_complete(
            self, sandbox_dir):
        """The in-band marker is forgeable; the out-of-band flag is not."""
        target = sandbox_dir / "mimic.py"
        target.write_text("x = 1\n\n[...truncado a 8000 chars]")
        result = _executor()._tool_read_file(str(target))
        assert result["content"].rstrip().endswith("chars]")   # looks truncated
        assert result["truncated"] is False                    # and is not
        assert result["source"]["complete"] is True

    def test_an_unsupported_extension_is_refused_not_guessed(self, sandbox_dir):
        target = sandbox_dir / "thing.bin"
        target.write_bytes(b"\x00\x01\x02")
        result = _executor()._tool_read_file(str(target))
        # Explicit refusal is the acceptable answer for binary (§4). What is not
        # acceptable is inventing text and presenting it as the source.
        assert result.get("error_code") == "UNSUPPORTED_EXTENSION"
        assert "content" not in result

    def test_extracted_text_is_marked_as_derived(self, tmp_path):
        """For a pdf/docx/image the text is EXTRACTED; the digest is the file."""
        ident = identify_source(tmp_path, content_derived=True)
        assert ident.content_derived is True
        assert ident.to_dict()["content_derived"] is True

    def test_a_path_outside_the_sandbox_is_refused_before_any_read(self):
        result = _executor()._tool_read_file(OUTSIDE_SANDBOX)
        assert result["error_code"] == "PATH_NOT_ALLOWED"
        assert "source" not in result and "content" not in result

    def test_a_symlink_escaping_the_sandbox_is_refused(self, tmp_path):
        link = Path.home() / "Downloads" / "m68c-escape-link"
        try:
            link.symlink_to(OUTSIDE_SANDBOX)
        except (OSError, FileExistsError):  # pragma: no cover
            pytest.skip("cannot create the symlink fixture")
        try:
            result = _executor()._tool_read_file(str(link))
            assert result["error_code"] == "PATH_NOT_ALLOWED"
        finally:
            link.unlink(missing_ok=True)


# ══ FINDING B · ATOMIC WRITE + OPTIMISTIC CONCURRENCY (CAS) ════════════════

class TestNormalWrites:
    """The ordinary paths still work, and say how the bytes got there."""

    def test_create_new(self, tmp_path):
        target = tmp_path / "new.py"
        receipt = cas_write_text(target, "fresh\n")
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "fresh\n"
        assert receipt.before.exists is False
        assert receipt.after.sha256 == digest_bytes(b"fresh\n")
        assert receipt.mutation_method is MutationMethod.ATOMIC_REPLACE

    def test_replace_existing(self, tmp_path):
        target = tmp_path / "old.py"
        target.write_text("before\n")
        receipt = cas_write_text(target, "after\n")
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "after\n"
        assert receipt.before.sha256 == digest_bytes(b"before\n")
        assert receipt.after.sha256 == digest_bytes(b"after\n")

    def test_an_identical_content_rewrite_still_applies(self, tmp_path):
        target = tmp_path / "same.py"
        target.write_text("same\n")
        receipt = cas_write_text(target, "same\n",
                                 expected_sha256=digest_bytes(b"same\n"))
        # A no-op write is still a write: the receipt must not claim a rejection
        # just because the digests match on both sides.
        assert receipt.status is WriteStatus.APPLIED
        assert receipt.before.sha256 == receipt.after.sha256

    def test_a_zero_byte_write(self, tmp_path):
        target = tmp_path / "zero.py"
        target.write_text("content\n")
        receipt = cas_write_text(target, "")
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_bytes() == b""
        assert receipt.after.size_bytes == 0
        assert receipt.bytes_written == 0

    def test_append_is_declared_as_append_and_not_as_atomic(self, tmp_path):
        target = tmp_path / "log.txt"
        target.write_text("one\n")
        receipt = cas_write_text(target, "two\n", mode="a")
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "one\ntwo\n"
        assert receipt.mutation_method is MutationMethod.APPEND
        # §17: an append is not a whole-file replacement and is not dressed up
        # as one. `intended_sha256` is None because the final content is a
        # function of bytes this call never saw.
        assert receipt.atomic_visibility is False
        assert receipt.intended_sha256 is None

    def test_an_invalid_mode_is_rejected_as_invalid_not_as_stale(self, tmp_path):
        receipt = cas_write_text(tmp_path / "x", "y", mode="x")
        assert receipt.status is WriteStatus.REJECTED_INVALID
        assert receipt.mutation_method is MutationMethod.NONE


class TestCompareAndSwap:
    """READ X + EXPECT X + MUTATE ONLY IF STILL X."""

    def test_a_matching_precondition_applies(self, tmp_path):
        target = tmp_path / "cas.py"
        target.write_text("v1\n")
        observed = identify_source(target).sha256
        receipt = cas_write_text(target, "v2\n", expected_sha256=observed)
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "v2\n"

    def test_a_mismatched_precondition_writes_NOTHING(self, tmp_path):
        """The measured lost update, now refused."""
        target = tmp_path / "shared.py"
        target.write_text("VERSION = 1\n")
        observed = identify_source(target).sha256          # JARVIS reads v1
        target.write_text("VERSION = 2  # a human edited it\n")   # someone else
        receipt = cas_write_text(target, "VERSION = 1b\n",
                                 expected_sha256=observed)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"
        assert "human" in target.read_text(), "the other edit was destroyed"
        assert receipt.bytes_written == 0

    def test_a_file_deleted_between_read_and_mutation_is_stale(self, tmp_path):
        target = tmp_path / "vanished.py"
        target.write_text("here\n")
        observed = identify_source(target).sha256
        target.unlink()
        receipt = cas_write_text(target, "back\n", expected_sha256=observed)
        # A deleted target does NOT satisfy a content precondition, and must not
        # be silently recreated: the caller's edit was based on a file that is
        # no longer there, which is a conflict a human has to see.
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert target.exists() is False

    def test_an_unexpectedly_present_destination_loses(self, tmp_path):
        """create-vs-create: ABSENT is a precondition and it is enforced."""
        target = tmp_path / "raced.py"
        target.write_text("someone got here first\n")
        receipt = cas_write_text(target, "mine\n", expected_sha256=ABSENT)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert target.read_text() == "someone got here first\n"

    def test_ABSENT_applies_when_the_path_really_is_free(self, tmp_path):
        target = tmp_path / "free.py"
        receipt = cas_write_text(target, "mine\n", expected_sha256=ABSENT)
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "mine\n"

    def test_a_malformed_digest_is_invalid_not_stale(self, tmp_path):
        target = tmp_path / "m.py"
        target.write_text("a\n")
        for bad in ("", "x", "Z" * 64, "abc123", "A" * 64):
            receipt = cas_write_text(target, "b\n", expected_sha256=bad)
            assert receipt.status is WriteStatus.REJECTED_INVALID, bad
        assert target.read_text() == "a\n"

    def test_no_precondition_preserves_last_writer_wins(self, tmp_path):
        """Additive: an existing caller that supplies nothing is unchanged."""
        target = tmp_path / "legacy.py"
        target.write_text("old\n")
        receipt = cas_write_text(target, "new\n")
        assert receipt.status is WriteStatus.APPLIED
        assert receipt.precondition["supplied"] is False
        assert target.read_text() == "new\n"

    def test_a_supplied_precondition_is_recorded_as_boundary_checked(self, tmp_path):
        target = tmp_path / "rec.py"
        target.write_text("a\n")
        receipt = cas_write_text(target, "b\n",
                                 expected_sha256=digest_bytes(b"a\n"))
        assert receipt.precondition["checked_at_mutation_boundary"] is True
        assert receipt.precondition["expected_sha256"] == digest_bytes(b"a\n")


class TestAtomicity:
    """Atomic VISIBILITY, and only that (§17)."""

    def test_a_replace_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "clean.py"
        target.write_text("a\n")
        cas_write_text(target, "b\n")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "clean.py"]
        assert leftovers == [], f"leaked temp files: {leftovers}"

    def test_the_temp_file_is_created_in_the_targets_own_directory(self, tmp_path):
        """Same filesystem, so os.replace is a rename and cannot be a partial copy."""
        import core.source_integrity as si
        seen: list[str] = []
        real = si.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir", ""))
            return real(*args, **kwargs)

        si.tempfile.mkstemp = spy
        try:
            cas_write_text(tmp_path / "sub" / "deep.py", "x\n")
        finally:
            si.tempfile.mkstemp = real
        assert seen == [str(tmp_path / "sub")]

    def test_crash_durability_is_never_claimed(self, tmp_path):
        receipt = cas_write_text(tmp_path / "d.py", "x\n")
        assert receipt.atomic_visibility is True
        assert receipt.crash_durability_proven is False
        assert receipt.to_dict()["crash_durability_proven"] is False


# ══ FINDING C · DIFF / PATCH TRANSPORT COMPLETENESS ════════════════════════

class TestTransportIdentity:
    """A DISPLAY SNIPPET IS NOT A PATCH ARTIFACT."""

    def test_a_complete_patch_is_a_complete_artifact(self):
        patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        ident = identify_transport(patch, patch, producer="git diff")
        assert ident.truncated is False
        assert ident.artifact_class is TransportArtifactClass.COMPLETE_PATCH
        assert ident.usable_as_patch_artifact is True
        assert ident.sha256 == digest_bytes(patch.encode())

    def test_a_truncated_patch_is_display_only_and_unusable(self):
        patch = "diff --git a/x b/x\n" + ("+line\n" * 5000)
        ident = identify_transport(patch, patch[:3000], producer="git diff")
        assert ident.truncated is True
        assert ident.artifact_class is TransportArtifactClass.TRUNCATED_DISPLAY_ONLY
        assert ident.usable_as_patch_artifact is False

    def test_the_digest_covers_the_COMPLETE_output_not_the_retained_bytes(self):
        """Hashing only what was shown would make a cut rendering self-consistent."""
        patch = "x" * 10_000
        ident = identify_transport(patch, patch[:3000], producer="git diff")
        assert ident.sha256 == digest_bytes(patch.encode())
        assert ident.sha256 != digest_bytes(patch[:3000].encode())
        assert ident.total_bytes == 10_000
        assert ident.retained_bytes == 3000

    def test_the_digest_changes_if_any_byte_changes(self):
        a = "@@ -1 +1 @@\n-a\n+b\n"
        b = "@@ -1 +1 @@\n-a\n+c\n"
        assert (identify_transport(a, a, producer="g").sha256
                != identify_transport(b, b, producer="g").sha256)

    def test_exactly_at_the_display_boundary_is_complete(self):
        patch = "y" * 3000
        ident = identify_transport(patch, patch[:3000], producer="git diff")
        assert ident.truncated is False
        assert ident.usable_as_patch_artifact is True

    def test_one_byte_past_the_display_boundary_is_truncated(self):
        patch = "y" * 3001
        ident = identify_transport(patch, patch[:3000], producer="git diff")
        assert ident.truncated is True
        assert ident.usable_as_patch_artifact is False

    def test_display_truncation_cannot_alter_the_artifact_digest(self):
        """The same patch rendered at three widths keeps ONE identity."""
        patch = "z" * 9000
        digests = {identify_transport(patch, patch[:n], producer="g").sha256
                   for n in (10, 3000, 9000)}
        assert len(digests) == 1

    def test_a_truncation_cannot_be_declared_away(self):
        """`truncated` is derived from the two strings, never passed in."""
        patch = "w" * 5000
        assert identify_transport(patch, patch[:100], producer="g").truncated is True

    @pytest.mark.parametrize("shape", [
        "diff --git a/a b/a\nnew file mode 100644\n--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+x\n",
        "diff --git a/a b/a\ndeleted file mode 100644\n--- a/a\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
        "diff --git a/a b/b\nrename from a\nrename to b\n",
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n\\ No newline at end of file\n",
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n" + "@@ -1 +1 @@\n-x\n+y\n" * 200,
    ], ids=["create", "delete", "rename", "no-newline", "many-hunks"])
    def test_every_patch_shape_keeps_a_complete_identity(self, shape):
        ident = identify_transport(shape, shape, producer="git diff")
        assert ident.usable_as_patch_artifact is True
        assert ident.total_bytes == len(shape.encode())

    def test_a_malformed_final_hunk_is_still_identified_honestly(self):
        """Identity is not validity: a broken patch gets a truthful digest and
        is still not applicable, which §9 keeps as separate questions."""
        broken = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+"
        ident = identify_transport(broken, broken, producer="git diff")
        assert ident.usable_as_patch_artifact is True   # complete transport
        # and nothing here claims it APPLIES — see TestGraderIsSyntaxOnly.


class TestGitQueryDeclaresItsTruncation:
    """The measured defect: a 3 000-char cap with no marker at all."""

    def test_a_short_output_is_not_truncated(self):
        result = _executor()._tool_git_query("branch")
        assert result.get("error") is None, result
        assert result["truncated"] is False
        assert result["transport"]["artifact_class"] == "COMPLETE_PATCH"

    def test_a_long_output_declares_the_cut_out_of_band(self):
        result = _executor()._tool_git_query("log", "-n 400")
        assert result.get("error") is None, result
        assert len(result["stdout"]) == 3000
        assert result["truncated"] is True
        assert result["transport"]["total_bytes"] > 3000
        assert result["transport"]["usable_as_patch_artifact"] is False

    def test_the_transport_payload_is_populated_not_an_empty_dict(self):
        """An empty `transport` key passes a key-presence check and says nothing."""
        result = _executor()._tool_git_query("log", "-n 400")
        transport = result["transport"]
        assert transport["truncated"] is True
        # The cap is 3000 CHARACTERS and these counts are BYTES, so the retained
        # figure is >= 3000 whenever the output contains anything multibyte (this
        # repository's commit subjects use em-dashes, which is how the two were
        # first measured apart: 3000 chars == 3056 bytes). Conflating the two is
        # how a boundary check passes on ASCII and lies on everything else.
        assert transport["retained_bytes"] >= 3000
        assert transport["retained_bytes"] == len(result["stdout"].encode("utf-8"))
        assert transport["total_bytes"] > transport["retained_bytes"]
        assert len(transport["sha256"]) == 64
        assert transport["artifact_class"] == "TRUNCATED_DISPLAY_ONLY"
        assert transport["usable_as_patch_artifact"] is False
        assert transport["producer"] == "git log"

    def test_the_declared_digest_is_over_the_whole_output(self):
        import subprocess  # nosec B404 - fixed argv, shell=False
        result = _executor()._tool_git_query("log", "-n 400")
        full = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["git", "log", "--oneline", "-n", "400"],
            capture_output=True, text=True, check=False,
            cwd=str(PACKAGE_ROOT.parent)).stdout
        assert result["transport"]["sha256"] == digest_bytes(full.encode())


# ══ FINDING E · TRUTHFUL RECEIPTS ══════════════════════════════════════════

class TestReceiptTruth:
    """THE RECEIPT DESCRIBES WHAT HAPPENED. IT DOES NOT GRANT AUTHORITY."""

    def test_a_dry_run_is_validated_and_NOT_applied(self, tmp_path):
        target = tmp_path / "dry.py"
        target.write_text("untouched\n")
        receipt = cas_write_text(target, "would be this\n",
                                 expected_sha256=digest_bytes(b"untouched\n"),
                                 dry_run=True)
        assert receipt.status is WriteStatus.VALIDATED_NOT_APPLIED
        assert receipt.applied is False
        assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"
        assert target.read_text() == "untouched\n"
        assert receipt.bytes_written == 0
        assert receipt.mutation_method is MutationMethod.NONE

    def test_a_successful_write_carries_BOTH_before_and_after_identity(self, tmp_path):
        target = tmp_path / "ev.py"
        target.write_text("pre\n")
        receipt = cas_write_text(target, "post\n")
        assert receipt.before is not None and receipt.after is not None
        assert receipt.before.sha256 == digest_bytes(b"pre\n")
        assert receipt.after.sha256 == digest_bytes(b"post\n")
        assert receipt.after.sha256 == receipt.intended_sha256
        # And in the SERIALISED form, which is what a caller actually receives.
        # Asserting only on the dataclass leaves `to_dict()` free to drop them.
        payload = receipt.to_dict()
        assert payload["before"] is not None, "the receipt dict lost before-state"
        assert payload["after"] is not None, "the receipt dict lost after-state"
        assert payload["before"]["sha256"] == digest_bytes(b"pre\n")
        assert payload["after"]["sha256"] == digest_bytes(b"post\n")

    def test_a_stale_rejection_reports_no_effect(self, tmp_path):
        target = tmp_path / "st.py"
        target.write_text("a\n")
        receipt = cas_write_text(target, "b\n", expected_sha256=digest_bytes(b"ZZ"))
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.bytes_written == 0
        assert receipt.mutation_method is MutationMethod.NONE
        assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"

    def test_a_post_state_mismatch_cannot_return_success(self, tmp_path, monkeypatch):
        """Success requires evidence of the POST state, not of the intent."""
        import core.source_integrity as si
        target = tmp_path / "tamper.py"
        real = si.identify_source
        calls = {"n": 0}

        def meddling(path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:        # the POST-mutation identification
                Path(path).write_text("something else entirely\n")
            return real(path, **kwargs)

        monkeypatch.setattr(si, "identify_source", meddling)
        receipt = si.cas_write_text(target, "intended\n")
        assert receipt.status is WriteStatus.PARTIAL_OR_UNKNOWN
        assert receipt.external_outcome == "UNKNOWN"
        assert receipt.applied is False

    def test_a_failure_before_the_boundary_claims_no_effect(self, tmp_path):
        """A directory where a file should go: the replace fails, nothing moves.

        Asserted exactly, not as "one of two acceptable statuses" — a test that
        tolerates either answer cannot tell a correct refusal from a wrong one.
        Measured: FAILED_BEFORE_MUTATION / PROVEN_NOT_EXECUTED, 0 bytes, and the
        staged temp file cleaned up.
        """
        target = tmp_path / "adir"
        target.mkdir()
        receipt = cas_write_text(target, "x\n")
        assert receipt.status is WriteStatus.FAILED_BEFORE_MUTATION
        assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"
        assert receipt.bytes_written == 0
        assert sorted(p.name for p in tmp_path.iterdir()) == ["adir"], \
            "a staged temp file was left behind by a failed replace"
        assert list(target.iterdir()) == [], "the directory was written into"

    def test_the_receipt_never_stores_the_source_body(self, tmp_path):
        """Hashes + metadata, never bodies (§8)."""
        target = tmp_path / "secret.py"
        target.write_text("API_KEY = 'before-secret-value'\n")
        payload = cas_write_text(
            target, "API_KEY = 'after-secret-value'\n").to_dict()
        blob = repr(payload)
        assert "before-secret-value" not in blob
        assert "after-secret-value" not in blob

    def test_a_receipt_from_before_the_lock_does_not_claim_serialisation(self):
        """A refusal that never reached the lock must not say it was serialised.

        An invalid mode is rejected before `_serialised_on` is entered, so this
        receipt is built from `_receipt`'s own keyword default — the one the
        campaign mutation `E_claims_serialised_always` flips. Nothing else reads
        that default, which is why mutating the dataclass field was inert.
        """
        receipt = cas_write_text(Path("/tmp/m68c-never-written"), "x", mode="q")
        assert receipt.status is WriteStatus.REJECTED_INVALID
        assert receipt.serialised is False, \
            "a receipt built before the lock was taken claims serialisation"
        assert receipt.to_dict()["serialised"] is False

    def test_every_status_maps_onto_the_M65D_vocabulary(self):
        """One truth model. A new status with no mapping is a KeyError here."""
        from core.effect_journal import ExternalOutcome
        for status in WriteStatus:
            outcome = external_outcome_of_write(status)
            assert isinstance(outcome, ExternalOutcome)
        assert external_outcome_of_write(WriteStatus.APPLIED) \
            is ExternalOutcome.PROVEN_COMMITTED
        assert external_outcome_of_write(WriteStatus.PARTIAL_OR_UNKNOWN) \
            is ExternalOutcome.UNKNOWN

    def test_uncertainty_is_never_collapsed_in_either_direction(self):
        """PARTIAL_OR_UNKNOWN is the only status that maps to UNKNOWN, and it
        maps to neither PROVEN_COMMITTED nor PROVEN_NOT_EXECUTED."""
        unknown = {s for s in WriteStatus
                   if external_outcome_of_write(s).value == "UNKNOWN"}
        assert unknown == {WriteStatus.PARTIAL_OR_UNKNOWN}

    def test_the_operation_id_is_unique_per_attempt(self, tmp_path):
        ids = {cas_write_text(tmp_path / f"o{i}.py", "x\n").operation_id
               for i in range(5)}
        assert len(ids) == 5

    def test_the_receipt_serialises_to_a_json_safe_dict(self, tmp_path):
        import json
        target = tmp_path / "j.py"
        target.write_text("a\n")
        payload = cas_write_text(target, "b\n").to_dict()
        round_tripped = json.loads(json.dumps(payload))
        assert round_tripped["status"] == "APPLIED"
        assert round_tripped["external_outcome"] == "PROVEN_COMMITTED"


class TestHandlerSurfacesTheReceipt:
    """The receipt reaches the real tool result, not just the helper."""

    def _in_sandbox(self, tmp_path):
        root = Path.home() / "Downloads" / "m68c-handler-fixture"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_a_stale_write_through_the_handler_is_refused(self, tmp_path):
        root = self._in_sandbox(tmp_path)
        target = root / "staged.py"
        try:
            target.write_text("VERSION = 1\n")
            observed = _executor()._tool_read_file(str(target))["source"]["sha256"]
            target.write_text("VERSION = 2  # human\n")
            result = _executor()._tool_write_file(
                str(target), "VERSION = 1b\n", expected_sha256=observed)
            assert result["error_code"] == "PRECONDITION_STALE"
            assert result["receipt"]["status"] == "REJECTED_STALE"
            assert "written" not in result
            assert "human" in target.read_text()
        finally:
            target.unlink(missing_ok=True)

    def test_a_fresh_write_through_the_handler_keeps_the_legacy_shape(self, tmp_path):
        root = self._in_sandbox(tmp_path)
        target = root / "ok.py"
        try:
            result = _executor()._tool_write_file(str(target), "hello\n")
            assert result["written"] == str(target)
            assert result["bytes"] == 6
            assert result["mode"] == "w"
            assert result["receipt"]["status"] == "APPLIED"
            assert result["receipt"]["external_outcome"] == "PROVEN_COMMITTED"
        finally:
            target.unlink(missing_ok=True)

    def test_a_dry_run_through_the_handler_reports_no_write(self, tmp_path):
        root = self._in_sandbox(tmp_path)
        target = root / "dry.py"
        try:
            target.write_text("keep\n")
            result = _executor()._tool_write_file(
                str(target), "discard\n", dry_run=True)
            assert result["validated"] is True
            assert result["written"] is None
            assert result["receipt"]["status"] == "VALIDATED_NOT_APPLIED"
            assert target.read_text() == "keep\n"
        finally:
            target.unlink(missing_ok=True)

    def test_a_round_trip_read_then_write_uses_the_read_digest(self, tmp_path):
        """The end-to-end shape the tool schema tells the model to use."""
        root = self._in_sandbox(tmp_path)
        target = root / "rt.py"
        try:
            target.write_text("counter = 0\n")
            read = _executor()._tool_read_file(str(target))
            result = _executor()._tool_write_file(
                str(target), "counter = 1\n",
                expected_sha256=read["source"]["sha256"])
            assert result["receipt"]["status"] == "APPLIED"
            assert target.read_text() == "counter = 1\n"
        finally:
            target.unlink(missing_ok=True)


# ══ §11 · DETERMINISTIC RACES ══════════════════════════════════════════════

class TestRaces:
    """Real orderings, forced with barriers and events — never with sleeps.

    A sleep-based race test is a timing lottery that passes on a fast host and
    flakes on a loaded one. Every ordering below is imposed by a
    :class:`threading.Event` or :class:`threading.Barrier`, so the outcome is a
    property of the code and not of the scheduler.
    """

    def test_a_stale_writer_loses_and_the_winner_remains_intact(self, tmp_path):
        """Reader A observes X; writer B makes it Y; A's write must be refused."""
        target = tmp_path / "race.py"
        target.write_text("X\n")
        observed_by_a = identify_source(target).sha256

        b_done = threading.Event()
        results: dict[str, object] = {}

        def writer_b():
            results["b"] = cas_write_text(target, "Y\n",
                                          expected_sha256=observed_by_a)
            b_done.set()

        def writer_a():
            b_done.wait(timeout=10)          # B goes first, deterministically
            results["a"] = cas_write_text(target, "A-edit\n",
                                          expected_sha256=observed_by_a)

        tb, ta = threading.Thread(target=writer_b), threading.Thread(target=writer_a)
        tb.start(); tb.join(10)
        ta.start(); ta.join(10)

        assert results["b"].status is WriteStatus.APPLIED
        assert results["a"].status is WriteStatus.REJECTED_STALE
        assert target.read_text() == "Y\n", "the winning writer was overwritten"

    @pytest.mark.skipif(
        not FLOCK_AVAILABLE,
        reason="no fcntl.flock on this platform: `_serialised_on` degrades to a "
               "no-op and the receipt says serialised=False, which the M68D H05 "
               "suite asserts as the honest contract instead")
    def test_two_cas_writers_from_one_read_produce_exactly_one_winner(self, tmp_path):
        """Both hold the same expected digest; exactly one may apply."""
        target = tmp_path / "both.py"
        target.write_text("start\n")
        observed = identify_source(target).sha256

        barrier = threading.Barrier(2)
        out: list = []
        lock = threading.Lock()

        def attempt(tag: str):
            barrier.wait(timeout=10)   # both arrive together
            receipt = cas_write_text(target, f"{tag}\n", expected_sha256=observed)
            with lock:
                out.append(receipt)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in ("p", "q")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        # MEASURED REGRESSION. Before the advisory directory lock, this exact
        # harness produced TWO APPLIED receipts over one surviving file: both
        # writers passed the digest comparison, both replaced, and each one's
        # post-state check ran before the other's replace. One of those two
        # receipts described a state that no longer existed.
        applied = [r for r in out if r.status is WriteStatus.APPLIED]
        assert len(applied) == 1, \
            f"{len(applied)} writers claimed APPLIED; exactly one may"
        assert applied[0].serialised is True, \
            "the winner did not run under the lock, so the result is luck"
        # The loser lost the comparison, because the lock made the comparison
        # and the replace one indivisible step. DETERMINISTIC: it is refused, it
        # writes nothing, and it says so in M65D's vocabulary.
        loser = [r for r in out if r.status is not WriteStatus.APPLIED]
        assert len(loser) == 1
        assert loser[0].status is WriteStatus.REJECTED_STALE
        assert loser[0].external_outcome == "PROVEN_NOT_EXECUTED"
        assert loser[0].bytes_written == 0
        # The winner's own post-state evidence matches what is actually on disk.
        assert applied[0].after.sha256 == digest_file(target)
        assert target.read_text().strip() in ("p", "q")

    def test_a_create_vs_create_race_has_one_winner(self, tmp_path):
        target = tmp_path / "newfile.py"
        barrier = threading.Barrier(2)
        out: list = []
        lock = threading.Lock()

        def attempt(tag: str):
            barrier.wait(timeout=10)
            receipt = cas_write_text(target, f"{tag}\n", expected_sha256=ABSENT)
            with lock:
                out.append(receipt)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in ("m", "n")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        applied = [r for r in out if r.status is WriteStatus.APPLIED]
        assert len(applied) == 1, "two create-new writers both claimed success"

    def test_a_delete_vs_modify_race_refuses_the_modify(self, tmp_path):
        target = tmp_path / "dm.py"
        target.write_text("live\n")
        observed = identify_source(target).sha256

        deleted = threading.Event()

        def deleter():
            target.unlink()
            deleted.set()

        t = threading.Thread(target=deleter)
        t.start(); t.join(10)
        deleted.wait(timeout=10)

        receipt = cas_write_text(target, "modified\n", expected_sha256=observed)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert target.exists() is False

    def test_without_a_precondition_nothing_is_ever_refused_as_stale(
            self, tmp_path):
        """NON-VACUITY WITNESS for the CAS races above.

        The same orderings, the same harness, the precondition removed. No
        writer is refused, because there is no expectation to violate — so the
        rejections in the tests above are produced by the CONTROL and not by the
        barrier, the thread count or the filesystem. Without this, a CAS test
        that passed because the second write happened to fail would look
        identical to one that passed because CAS worked.
        """
        target = tmp_path / "nocas.py"
        target.write_text("start\n")
        barrier = threading.Barrier(2)
        out: list = []
        lock = threading.Lock()

        def attempt(tag: str):
            barrier.wait(timeout=10)
            receipt = cas_write_text(target, f"{tag}\n")     # no precondition
            with lock:
                out.append(receipt)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in ("p", "q")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(out) == 2
        assert not any(r.status is WriteStatus.REJECTED_STALE for r in out), \
            "a write with no precondition was refused as stale"
        assert all(r.precondition["supplied"] is False for r in out)


# ══ §13 · CLEANUP / FAILURE-BOUNDARY PROOF ═════════════════════════════════

class TestFailureBoundaries:
    """Every failure mode, and what it must and must not claim."""

    @pytest.mark.skipif(not POSIX_MODES_ENFORCED,
                        reason="chmod does not remove directory write access here "
                               "(Windows, or running as root)")
    def test_a_permission_failure_leaves_the_original_intact(self, tmp_path):
        directory = tmp_path / "ro"
        directory.mkdir()
        target = directory / "guarded.py"
        target.write_text("original\n")
        directory.chmod(0o500)                       # no write on the DIRECTORY
        try:
            receipt = cas_write_text(target, "replacement\n")
            assert receipt.status is WriteStatus.FAILED_BEFORE_MUTATION
            assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"
            assert target.read_text() == "original\n"
        finally:
            directory.chmod(0o700)

    @pytest.mark.skipif(not POSIX_MODES_ENFORCED,
                        reason="chmod does not remove directory write access here "
                               "(Windows, or running as root)")
    def test_a_failed_temp_write_leaks_no_temp_file(self, tmp_path):
        directory = tmp_path / "ro2"
        directory.mkdir()
        target = directory / "x.py"
        target.write_text("keep\n")
        directory.chmod(0o500)
        try:
            cas_write_text(target, "no\n")
            directory.chmod(0o700)
            assert [p.name for p in directory.iterdir()] == ["x.py"]
        finally:
            directory.chmod(0o700)

    def test_an_exception_inside_the_replace_leaves_no_temp_file(
            self, tmp_path, monkeypatch):
        import core.source_integrity as si
        target = tmp_path / "boom.py"
        target.write_text("before\n")

        def exploding_replace(*_args, **_kwargs):
            raise OSError("synthetic replace failure")

        monkeypatch.setattr(si.os, "replace", exploding_replace)
        receipt = si.cas_write_text(target, "after\n")
        assert receipt.status is WriteStatus.FAILED_BEFORE_MUTATION
        assert target.read_text() == "before\n", "the original was damaged"
        leftovers = sorted(p.name for p in tmp_path.iterdir())
        assert leftovers == ["boom.py"], f"leaked temp files: {leftovers}"

    def test_a_rejected_precondition_creates_nothing_at_all(self, tmp_path):
        """No mutation occurs on a rejected preflight — not even a temp file."""
        target = tmp_path / "absent.py"
        receipt = cas_write_text(target, "x\n",
                                 expected_sha256=digest_bytes(b"whatever"))
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert list(tmp_path.iterdir()) == []

    def test_a_dry_run_creates_nothing_at_all(self, tmp_path):
        target = tmp_path / "nothing.py"
        cas_write_text(target, "x\n", dry_run=True)
        assert list(tmp_path.iterdir()) == []

    def test_an_append_failure_does_not_claim_a_clean_no_effect_blindly(
            self, tmp_path, monkeypatch):
        """If the append raises AFTER bytes landed, the answer is UNKNOWN."""
        import core.source_integrity as si
        target = tmp_path / "ap.log"
        target.write_text("one\n")
        real_open = si.open if hasattr(si, "open") else open

        class HalfWriter:
            def __init__(self, path):
                self._handle = real_open(path, "a", encoding="utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

            def write(self, data):
                self._handle.write(data)
                self._handle.flush()
                raise OSError("synthetic mid-append failure")

        monkeypatch.setattr(si, "open",
                            lambda path, mode="r", **kw: HalfWriter(path)
                            if mode == "a" else real_open(path, mode, **kw),
                            raising=False)
        receipt = si.cas_write_text(target, "two\n", mode="a")
        assert receipt.status is WriteStatus.PARTIAL_OR_UNKNOWN
        assert receipt.external_outcome == "UNKNOWN"

    def test_the_advisory_lock_leaks_no_descriptors(self, tmp_path):
        """§13: no leaked locks. The lock is a directory fd, so a missing
        `os.close` would exhaust the process's descriptors under load rather
        than failing visibly on the first call."""
        if not Path("/proc/self/fd").exists():  # pragma: no cover - non-Linux
            pytest.skip("/proc/self/fd is needed to count descriptors")
        target = tmp_path / "churn.py"
        cas_write_text(target, "warm\n")          # exclude one-time setup
        before = len(os.listdir("/proc/self/fd"))
        for i in range(200):
            cas_write_text(target, f"v{i}\n")
        after = len(os.listdir("/proc/self/fd"))
        assert after == before, f"leaked {after - before} descriptors over 200 writes"

    def test_a_rejected_write_leaks_no_descriptors_either(self, tmp_path):
        """The refusal path returns from inside the `with`, so it must unwind it."""
        if not Path("/proc/self/fd").exists():  # pragma: no cover
            pytest.skip("/proc/self/fd is needed to count descriptors")
        target = tmp_path / "refused.py"
        target.write_text("a\n")
        cas_write_text(target, "b\n", expected_sha256=digest_bytes(b"nope"))
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(200):
            cas_write_text(target, "b\n", expected_sha256=digest_bytes(b"nope"))
        after = len(os.listdir("/proc/self/fd"))
        assert after == before, f"leaked {after - before} descriptors over 200 refusals"

# ══ §9 · PATCH GRADER / VALIDATOR TRUTH ════════════════════════════════════

class TestGraderIsSyntaxOnly:
    """A grader called "valid" must not be read as "applicable".

    ``DiffBudgetGrader`` screens a unified diff for budget and tampering. It has
    always documented that it makes no semantic claim, and M68C does not change
    what it does — it makes the SCOPE machine-readable, so the distinction
    cannot be blurred by a future reader, a rename, or a caller in a hurry.
    """

    def test_the_registry_declares_the_grader_as_syntax_only(self):
        """The scope lives in a registry, NOT on the grader.

        `jarvis/training_gym/graders/` is FROZEN scientific machinery, byte-identical
        since 05c043b3. M68C's first draft added a `VALIDATION_SCOPE` attribute to
        `DiffBudgetGrader` and broke that freeze — caught by
        `test_the_graders_and_the_refusal_detector_are_untouched` in the full suite.
        A preregistered grader may not be edited to make a later milestone tidier.
        """
        from core.source_integrity import PATCH_VALIDATION_SCOPES
        assert PATCH_VALIDATION_SCOPES[
            "training_gym.graders.diff_budget_grader.DiffBudgetGrader"] \
            is PatchValidationScope.SYNTAX_ONLY

    def test_the_frozen_grader_carries_no_competing_self_declaration(self):
        """One place says what a component proves. Two places can disagree."""
        from training_gym.graders.diff_budget_grader import DiffBudgetGrader
        assert not hasattr(DiffBudgetGrader, "VALIDATION_SCOPE"), (
            "the frozen grader self-declares a scope again; that edit breaks the "
            "S3Q.0 freeze and creates a second writable copy of the contract")

    def test_no_component_declares_applicability_proven(self):
        """Nothing in this build applies patches, so nothing may claim it has
        proven one applies. The day something does, this test is the gate."""
        from core.source_integrity import (
            PATCH_APPLICATION_PATHS, PATCH_VALIDATION_SCOPES)
        assert PATCH_APPLICATION_PATHS == ()
        assert PatchValidationScope.APPLICABILITY_PROVEN \
            not in PATCH_VALIDATION_SCOPES.values()

    def test_a_syntactically_valid_diff_against_wrong_context_is_not_applicable(self):
        """SYNTAX_VALID but NOT_APPLICABLE — §9's worked example.

        The diff below parses perfectly: one file, one hunk, well-formed
        headers. Its context line does not exist in the tree it names. The
        parser is happy, which is exactly the point: parsing is not applying,
        and a parser's PASS must never be spent as applicability evidence.
        """
        from training_gym.graders.diff_budget_grader import parse_unified_diff
        diff = ("diff --git a/real.py b/real.py\n"
                "--- a/real.py\n"
                "+++ b/real.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-this line is not in the file\n"
                "+replacement\n")
        files, rejected = parse_unified_diff(diff)
        assert rejected == ()
        assert len(files) == 1 and files[0].path == "real.py"
        # The parser produced a clean parse of a patch that cannot apply. It
        # exposes no "applies" field at all, and that absence is deliberate.
        assert not hasattr(files[0], "applies")
        assert not hasattr(files[0], "applicable")

    def test_a_hostile_path_in_a_diff_is_reported_not_parsed(self):
        from training_gym.graders.diff_budget_grader import parse_unified_diff
        for hostile in ("../../etc/passwd", "/etc/passwd"):
            diff = (f"diff --git a/{hostile} b/{hostile}\n"
                    f"--- a/{hostile}\n+++ b/{hostile}\n@@ -1 +1 @@\n-a\n+b\n")
            files, rejected = parse_unified_diff(diff)
            assert rejected, f"{hostile} was parsed instead of reported"
            assert files == ()
