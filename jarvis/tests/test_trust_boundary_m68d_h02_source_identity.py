"""V69 M68D — H02: source read identity.

The invariant: the CONTENT a read returns and the DIGEST that identifies it must
come from ONE source observation.

Before M68D they came from two. ``_tool_read_file`` rendered the text from one
opening of the path and then called ``identify_source(p)``, which stat'd and
hashed a LATER, INDEPENDENT opening. MEASURED: a reader took
``AAAA-version-one-AAAA``; a concurrent writer atomically replaced the file with
a SAME-SIZE ``BBBB-version-two-BBBB``; the returned dict carried version one's
text, version TWO's digest and ``complete: true``. ``cas_write_text`` then
accepted that digest as the precondition for an edit derived from bytes nobody
had read, and the human's version two was gone under an APPLIED receipt.

Races here are driven by injected hooks at the real seams — no sleeps.

V69 M68D.1 — WINDOWS PORTABILITY
--------------------------------
The first real Windows runner (CI run 37695660326, windows-2025-vs2026,
CPython 3.11.9) failed 14 tests in this file. NONE of them was a
source-identity defect. They were POSIX assumptions in the FIXTURES:

* `os.replace` / `os.unlink` against a path whose descriptor this process
  holds open. CPython opens without ``FILE_SHARE_DELETE``, so Windows
  refuses the mutation (``WinError 5`` / ``WinError 32``) and the
  rename-under-the-reader adversary cannot be STAGED at all;
* ``chmod(0o000)``, which on Windows does not remove read access;
* text-mode fixture writes, which on Windows emit CRLF — so a fixture
  written as ``"x = 1\n"`` is SEVEN bytes and a digest over
  ``b"x = 1\n"`` no longer describes it.

The POSIX rename/unlink attack is the FIXTURE, not the property. Closing
the descriptor before hashing would make it runnable on Windows and would
restore the original TOCTOU bug, so it is not done. Instead:

* every byte-identity fixture is byte explicit (``write_bytes`` / an
  explicit ``newline``), so it is the same file on every platform;
* the coherence invariant is asserted on EVERY platform through the one
  adversary no supported platform refuses — an in-place rewrite of the
  held inode, which is also the STRONGER attack, since an atomic
  replacement cannot disturb a held descriptor at all;
* only the impossible MECHANISM skips, against a MEASURED capability
  (see ``_test_support.platform_capabilities``), never against
  ``sys.platform``, and never by catching ``PermissionError``.

Classification of every capability-gated test below, per M68D.1 §13:
``POSIX_CAPABILITY_TEST`` — the attack mechanism does not exist elsewhere;
``CROSS_PLATFORM_INVARIANT`` — the property itself, asserted everywhere.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import tokenize
from pathlib import Path

import pytest

import core.source_integrity as si
import tools.executor as ex
from core.source_integrity import (
    DIGEST_COVERS_COMPLETE,
    DIGEST_COVERS_NOTHING,
    SourceSnapshotError,
    WriteStatus,
    cas_write_text,
    digest_file,
    identify_snapshot,
    source_snapshot,
)
from _test_support.platform_capabilities import (
    AVAILABLE_UNDER_OPEN_FD,
    MODE_BITS_REMOVE_READ,
    REPLACE_OVER_OPEN_PATH,
    CONTINUOUS_MECHANISMS,
    UNLINK_OPEN_PATH,
    WHY_NO_MODE_BITS,
    WHY_NO_REPLACE,
    WHY_NO_UNLINK,
    Mutation,
)

_SRC = Path(ex.__file__).read_text(encoding="utf-8")
_LINES = _SRC.splitlines()


def _code_lines(src: str) -> list[str]:
    out = [""] * (len(src.splitlines()) + 2)
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING):
            prev = tok.type
            continue
        if tok.string.strip():
            out[tok.start[0]] += " " + tok.string
        prev = tok.type
    return out


_CODE_LINES = _code_lines(_SRC)


def code_region(header: str) -> str:
    """Code-only text of the block whose first line contains *header*."""
    starts = [i for i, line in enumerate(_LINES) if header in line]
    assert starts, f"region header not found: {header!r}"
    start = starts[0]
    indent = len(_LINES[start]) - len(_LINES[start].lstrip())
    end = len(_LINES)
    for i in range(start + 1, len(_LINES)):
        line = _LINES[i]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and line.lstrip().startswith(("def ", "class ", "@", "#")):
            end = i
            break
    return " ".join(_CODE_LINES[start + 1:end + 1])


def _executor():
    return ex.ToolExecutor.__new__(ex.ToolExecutor)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An allowed root that is a real temporary directory."""
    monkeypatch.setattr(ex, "_sandbox_allowed_dirs", lambda: [tmp_path])
    return tmp_path


def _write(target: Path, text: str) -> bytes:
    """Write *text* BYTE-EXPLICITLY and return the bytes that landed.

    BYTE_IDENTITY_TEST helper. ``Path.write_text`` writes in text mode with
    ``newline=None``, which translates ``"\n"`` to ``os.linesep`` — so on
    Windows a six-character fixture becomes a SEVEN-byte file and every
    digest, size and CAS precondition taken over ``text.encode()`` stops
    describing it (measured: 10 failures in the M68C suite and several
    here). Callers assert against the RETURN VALUE rather than re-encoding
    the literal, so the expectation cannot drift from the fixture.
    """
    payload = text.encode("utf-8")
    target.write_bytes(payload)
    return payload


def _atomic_replace(target: Path, text: str) -> None:
    """Rename a different file over *target*. BYTE-EXPLICIT: binary mode."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent))
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
    os.replace(tmp, target)


def _in_place_rewrite(target: Path, payload: bytes) -> None:
    """Overwrite the bytes of the HELD INODE itself, through a second handle.

    CROSS_PLATFORM. ``source_snapshot`` opens with ``os.open(O_RDONLY)``,
    which on Windows requests ``FILE_SHARE_WRITE``, so a second opening for
    writing succeeds where a rename or an unlink does not. It is the
    STRONGER adversary: an atomic replacement cannot disturb a held fd at
    all, while this changes the very bytes the observation is reading.
    """
    with open(target, "r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _stage(mechanism: Mutation, target: Path, replacement: bytes) -> None:
    """Change what *target* means by *mechanism*, with its descriptor held.

    One dispatch so a test can be parametrised over the mechanisms THIS
    platform actually permits instead of skipping wholesale. It never
    swallows an error: a mechanism listed as available and then refused is
    a measurement failure and must surface.
    """
    if mechanism is Mutation.ATOMIC_REPLACE:
        _atomic_replace(target, replacement.decode("utf-8"))
    elif mechanism is Mutation.UNLINK:
        target.unlink()
    elif mechanism is Mutation.UNLINK_RECREATE:
        target.unlink()
        target.write_bytes(replacement)
    elif mechanism is Mutation.IN_PLACE_REWRITE:
        _in_place_rewrite(target, replacement)
    else:                                        # pragma: no cover
        raise AssertionError(f"unknown mechanism {mechanism!r}")


#: Only the mechanism skips. The invariant never does.
needs_replace = pytest.mark.skipif(not REPLACE_OVER_OPEN_PATH,
                                   reason=WHY_NO_REPLACE)
needs_unlink = pytest.mark.skipif(not UNLINK_OPEN_PATH, reason=WHY_NO_UNLINK)
needs_mode_bits = pytest.mark.skipif(not MODE_BITS_REMOVE_READ,
                                     reason=WHY_NO_MODE_BITS)


# ── the snapshot itself ──────────────────────────────────────────────────────

class TestSnapshotCoherence:
    @pytest.mark.parametrize("body", [
        "", "plain text\n", "no final newline", "ñ→🙂 unicode\n",
        "a\nb\nc\n", "\n", "line\r\nwindows\r\n",
    ])
    def test_the_digest_is_over_exactly_the_captured_bytes(self, tmp_path, body):
        target = tmp_path / "f.txt"
        target.write_bytes(body.encode("utf-8"))
        with source_snapshot(target) as snap:
            assert snap.payload() == body.encode("utf-8")
            assert snap.sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
            assert snap.size_bytes == len(body.encode("utf-8"))
            assert snap.stable is True
            assert snap.text() == body

    def test_an_absent_file_raises_rather_than_describing_nothing(self, tmp_path):
        with pytest.raises(SourceSnapshotError):
            with source_snapshot(tmp_path / "missing.txt"):
                pass

    @needs_mode_bits
    def test_an_unreadable_file_raises(self, tmp_path):
        """POSIX_CAPABILITY_TEST — CONSTRUCTING an unreadable file needs chmod.

        The CONTRACT (a source that cannot be observed raises rather than being
        described) is asserted on every platform by the M68D.1 closure suite,
        which injects the `os.open` failure directly instead of building a file
        the platform refuses to make unreadable. The root case is covered by the
        same capability: as root, `chmod(0o000)` leaves the file readable, so
        the probe measures the capability ABSENT and this skips — one measured
        condition instead of a platform guess plus a euid guess.
        """
        target = tmp_path / "locked.txt"
        _write(target, "x\n")
        target.chmod(0o000)
        try:
            with pytest.raises(SourceSnapshotError):
                with source_snapshot(target):
                    pass
        finally:
            target.chmod(0o600)

    @needs_unlink
    def test_a_deletion_during_the_observation_does_not_corrupt_it(self, tmp_path):
        """POSIX_CAPABILITY_TEST — the descriptor outlives the directory entry.

        Staging this needs `unlink` of an open path, which Windows refuses with
        WinError 32. The PROPERTY — identity cannot change after acquisition —
        runs on every platform in
        `test_a_post_acquisition_mutation_cannot_alter_the_observation`.
        """
        target = tmp_path / "doomed.txt"
        payload = _write(target, "still here\n")
        with source_snapshot(target) as snap:
            target.unlink()
            assert snap.payload() == payload
            assert snap.text() == "still here\n"
            assert snap.sha256 == hashlib.sha256(payload).hexdigest()
            assert snap.recheck() is True

    @needs_unlink
    def test_recreating_the_path_does_not_change_the_observation(self, tmp_path):
        """POSIX_CAPABILITY_TEST — unlink-then-recreate under a held descriptor."""
        target = tmp_path / "r.txt"
        payload = _write(target, "first\n")
        with source_snapshot(target) as snap:
            target.unlink()
            _write(target, "second-but-longer\n")
            assert snap.payload() == payload
            assert snap.sha256 == hashlib.sha256(payload).hexdigest()

    def test_stream_mode_digests_the_same_bytes_it_serves(self, tmp_path):
        target = tmp_path / "s.bin"
        body = bytes(range(256)) * 40
        target.write_bytes(body)
        with source_snapshot(target, capture_bytes=False) as snap:
            assert snap.sha256 == hashlib.sha256(body).hexdigest()
            with snap.stream() as handle:
                assert handle.read() == body
            assert snap.recheck() is True

    def test_stream_mode_refuses_to_pretend_it_captured_text(self, tmp_path):
        target = tmp_path / "s.bin"
        target.write_bytes(b"x")
        with source_snapshot(target, capture_bytes=False) as snap:
            with pytest.raises(SourceSnapshotError):
                snap.text()
            with pytest.raises(SourceSnapshotError):
                snap.payload()

    def test_a_second_stream_is_independent_and_rewound(self, tmp_path):
        target = tmp_path / "two.bin"
        target.write_bytes(b"abcdef")
        with source_snapshot(target, capture_bytes=False) as snap:
            with snap.stream() as a:
                assert a.read(3) == b"abc"
            with snap.stream() as b:
                assert b.read() == b"abcdef"


class TestSnapshotRaces:
    @needs_replace
    def test_an_atomic_replacement_mid_read_cannot_split_content_from_digest(
            self, tmp_path, monkeypatch):
        """THE regression, at the real seam. POSIX_CAPABILITY_TEST.

        The replacement fires between two chunk reads of the snapshot's own
        descriptor. The held fd pins the original inode, so the remaining chunks
        are still version one's — and the digest is over those same bytes.

        `os.replace` over a held path is the staging mechanism and Windows
        refuses it (WinError 5). The same seam is attacked on every platform by
        `test_an_in_place_rewrite_mid_read_cannot_split_content_from_digest`,
        with the adversary Windows does permit.
        """
        target = tmp_path / "notes.txt"
        v1 = "AAAA-version-one-AAAA\n"
        v2 = "BBBB-version-two-BBBB\n"
        assert len(v1.encode()) == len(v2.encode()), "same-size replacement"
        _write(target, v1)

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _atomic_replace(target, v2)
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            observed_text = snap.text()
            observed_sha = snap.sha256
            stable = snap.stable
        assert fired, "NON-VACUITY: the race never fired"
        assert target.read_text() == v2, "the writer did not actually win on disk"
        assert observed_text == v1
        assert observed_sha == hashlib.sha256(v1.encode()).hexdigest()
        assert observed_sha != hashlib.sha256(v2.encode()).hexdigest()
        assert stable is True, \
            "an atomic replacement does not make the held observation incoherent"

    @needs_replace
    def test_the_digest_from_that_race_cannot_overwrite_the_unread_edit(
            self, tmp_path, monkeypatch):
        """The consequence the finding was really about. POSIX_CAPABILITY_TEST.

        A CAS write using the snapshot's digest must be REJECTED, because the
        file on disk is the version the reader never saw. Before M68D the digest
        described that unread version, so the write was APPLIED and the human's
        edit was destroyed.

        The same consequence is proven on every platform by
        `test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit`.
        """
        target = tmp_path / "notes.txt"
        v1, v2 = "AAAA-version-one-AAAA\n", "BBBB-version-two-BBBB\n"
        _write(target, v1)

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _atomic_replace(target, v2)
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            precondition = snap.sha256
        assert fired
        receipt = cas_write_text(target, "CCCC-derived-from-V1-CCCC\n",
                                 expected_sha256=precondition)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.applied is False
        assert receipt.bytes_written == 0
        assert target.read_text() == v2, "the unread human edit was destroyed"

    def test_an_in_place_rewrite_during_the_observation_is_reported_unstable(
            self, tmp_path, monkeypatch):
        """Option B of §14: refuse, rather than present mixed bytes as whole ones."""
        target = tmp_path / "inplace.txt"
        target.write_bytes(b"A" * 64)

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                with open(target, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"Z" * 64)
                    handle.flush()
                    os.fsync(handle.fileno())
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            assert fired, "NON-VACUITY: the in-place rewrite never fired"
            assert snap.stable is False
            identity = identify_snapshot(snap, content_chars_total=64,
                                         content_chars_returned=64)
            assert identity.sha256 is None
            assert identity.digest_covers == DIGEST_COVERS_NOTHING
            assert identity.complete is False

    def test_a_short_read_is_not_reported_as_a_whole_observation(self, tmp_path,
                                                                   monkeypatch):
        """Failure injection: the read ends early and the stat still agrees.

        `st_size` before and after can be identical while the bytes actually
        captured are fewer — a truncated read, a signal, a filesystem that
        returns a short count. The captured LENGTH has to be part of the
        stability test, or the digest covers less than it claims to.
        """
        target = tmp_path / "short.txt"
        target.write_bytes(b"X" * 4096)
        real_read = os.read
        calls: list[int] = []

        def short_read(fd, size):
            calls.append(1)
            if len(calls) == 1:
                return real_read(fd, 16)      # a deliberately short first chunk
            return b""                        # then claim end-of-file

        monkeypatch.setattr(os, "read", short_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            assert snap.size_bytes == 16, "non-vacuity: the read was not short"
            assert snap.stable is False, \
                "a 16-byte observation of a 4096-byte file reported as whole"
            identity = identify_snapshot(snap)
            assert identity.sha256 is None
            assert identity.complete is False

    def test_an_unstable_snapshot_can_never_state_a_precondition(self, tmp_path):
        target = tmp_path / "u.txt"
        _write(target, "x\n")
        with source_snapshot(target) as snap:
            snap.stable = False
            identity = identify_snapshot(snap)
            assert identity.sha256 is None and identity.complete is False
            receipt = cas_write_text(target, "y\n",
                                     expected_sha256=identity.sha256 or "")
            assert receipt.applied is False

    def test_recheck_notices_an_in_place_rewrite_after_a_derived_parse(self, tmp_path):
        target = tmp_path / "d.bin"
        target.write_bytes(b"A" * 32)
        with source_snapshot(target, capture_bytes=False) as snap:
            assert snap.recheck() is True
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(b"Z" * 32)
                handle.flush()
                os.fsync(handle.fileno())
            assert snap.recheck() is False
            assert snap.stable is False

    # ── V69 M68D.1: the same properties, on every platform ──────────────────

    @pytest.mark.parametrize("mechanism", AVAILABLE_UNDER_OPEN_FD,
                             ids=lambda m: m.value)
    def test_a_post_acquisition_mutation_cannot_alter_the_observation(
            self, tmp_path, mechanism):
        """CROSS_PLATFORM_INVARIANT — §7 bullets 1, 2 and 3 in one test.

        Once the snapshot exists, NOTHING done to the path may change the bytes
        it returns or the digest that identifies them, and the two must keep
        describing each other. Parametrised over the mechanisms this host
        measured as available, so the proof is as strong as the platform allows
        and never weaker than the one adversary every platform permits.
        """
        target = tmp_path / "post.txt"
        original = _write(target, "ORIGINAL-OBSERVED-BYTES\n")
        if mechanism is Mutation.IN_PLACE_REWRITE:
            # DERIVED, not hand-counted. An in-place rewrite shorter than the
            # original is a PARTIAL overwrite, which would leave a tail of the
            # old bytes and hide a torn read behind the length difference. Two
            # hand-counted literals here drifted on the first run and the
            # non-vacuity guard caught it, so the length is computed.
            replacement = b"R" * (len(original) - 1) + b"\n"
            assert len(replacement) == len(original), "same-size rewrite"
        else:
            replacement = b"REPLACEMENT-NEVER-OBSERVED\n"
        assert replacement != original, "non-vacuity: the mutation changes nothing"
        with source_snapshot(target) as snap:
            first_payload, first_sha, first_size = (
                snap.payload(), snap.sha256, snap.size_bytes)
            assert first_payload == original, "non-vacuity: wrong bytes captured"
            _stage(mechanism, target, replacement)
            assert snap.payload() == first_payload, \
                "the observation changed after it was acquired"
            assert snap.sha256 == first_sha
            assert snap.size_bytes == first_size
            assert snap.sha256 == hashlib.sha256(snap.payload()).hexdigest(), \
                "content and digest stopped describing the same bytes"
            with snap.stream() as handle:
                assert handle.read() == first_payload, \
                    "stream() served something other than the observation"
        if mechanism in (Mutation.ATOMIC_REPLACE, Mutation.UNLINK_RECREATE,
                         Mutation.IN_PLACE_REWRITE):
            assert target.read_bytes() == replacement, \
                "non-vacuity: the mutation did not actually land on disk"
            assert digest_file(target) != first_sha, \
                "non-vacuity: the mutation produced the same bytes"
        else:
            assert not target.exists(), "non-vacuity: the unlink did not happen"

    def test_an_in_place_rewrite_mid_read_cannot_split_content_from_digest(
            self, tmp_path, monkeypatch):
        """CROSS_PLATFORM_INVARIANT — the Windows-capable form of THE regression.

        The adversary Windows DOES permit, fired at the same seam: between two
        chunk reads of the snapshot's own descriptor, through a second handle on
        the SAME inode. It is strictly stronger than the rename, which a held fd
        is immune to by construction. Two things must hold: the digest still
        covers exactly the bytes handed back, and the mixture is reported as
        UNSTABLE rather than presented as a whole observation.
        """
        target = tmp_path / "inplace-race.txt"
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-version-two-BBBB\n"
        assert len(v1) == len(v2), "same-size rewrite"
        target.write_bytes(v1)

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _in_place_rewrite(target, v2)
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            observed, observed_sha, stable = snap.payload(), snap.sha256, snap.stable
            identity = identify_snapshot(snap)
        assert fired, "NON-VACUITY: the race never fired"
        assert target.read_bytes() == v2, "the writer did not actually win on disk"
        assert observed_sha == hashlib.sha256(observed).hexdigest(), \
            "content and digest describe different bytes"
        assert stable is False, \
            "an in-place rewrite under the descriptor was reported as whole"
        assert identity.sha256 is None and identity.complete is False, \
            "an unstable observation still offered an identity"

    def test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit(
            self, tmp_path, monkeypatch):
        """CROSS_PLATFORM_INVARIANT — the CONSEQUENCE, on every platform.

        The Windows-capable form of
        `test_the_digest_from_that_race_cannot_overwrite_the_unread_edit`: a
        reader raced by an in-place rewrite must not be able to produce a
        precondition under which the unread edit is overwritten. It is refused
        one step earlier than in the rename case — the observation has no
        digest at all — and the human's bytes survive either way.
        """
        target = tmp_path / "unread-edit.txt"
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-human-wrote-this\n"
        assert len(v1) == len(v2), "same-size rewrite"
        target.write_bytes(v1)

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _in_place_rewrite(target, v2)
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            precondition = identify_snapshot(snap).sha256
        assert fired, "NON-VACUITY: the race never fired"
        assert precondition is None, \
            "a raced observation handed out a write precondition"
        # And the digest it DID compute is refused as a precondition too: it
        # describes a mixture, and the file on disk is the human's version.
        receipt = cas_write_text(target, b"CCCC-derived-from-V1-CCCC\n".decode(),
                                 expected_sha256=snap.sha256)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.applied is False
        assert receipt.bytes_written == 0
        assert target.read_bytes() == v2, "the unread human edit was destroyed"

    @pytest.mark.parametrize("mechanism", CONTINUOUS_MECHANISMS,
                             ids=lambda m: m.value)
    def test_two_concurrent_snapshots_of_one_file_agree(self, tmp_path, mechanism):
        """CROSS_PLATFORM_INVARIANT, staged by every mechanism this host permits.

        No reader may return a TORN PAIR — bytes from one version with a digest
        of another. That property holds under EVERY interleaving, which is what
        makes the test sound even though the interleaving is sampled rather
        than imposed: the barrier aligns the three threads' START, and nothing
        can force the writer's mutation to land while a descriptor is held.

        MEASURED, and not what was first assumed. Under the Windows
        mandatory-locking simulation this test passed 3/3 with the simulator
        reporting **0 denials** — the writer's `os.replace` consistently
        completed before either reader opened its descriptor, so the refused
        mutation never happened. That is consistent with it NOT being among the
        14 H02 failures on the real runner.

        The repair is still needed, for the reason the measurement exposes: if
        the writer HAD been refused, nothing would have noticed. `threading`
        turns a dead thread's exception into output and the joining test reads
        `out`, which the readers filled from an untouched file. So the writer's
        exception is now COLLECTED and asserted empty — a mechanism this
        platform measured as available and then refused FAILS here instead of
        certifying a race it never staged (M68D.1 §17).

        `stable` stays mechanism-specific on purpose: an atomic replacement
        leaves the held inode untouched and must report True, while an in-place
        rewrite changes the bytes under the descriptor and must be free to
        report False. Asserting True for both would make the honest answer red.
        """
        target = tmp_path / "c.txt"
        original = _write(target, "shared content\n")
        if mechanism is Mutation.IN_PLACE_REWRITE:
            # Same size, or the rewrite is a partial overwrite and a torn pair
            # would be hidden by the length difference rather than detected.
            # DERIVED: see the sibling test — the hand-counted literal drifted.
            replacement = b"R" * (len(original) - 1) + b"\n"
            assert len(replacement) == len(original), "same-size rewrite"
        else:
            replacement = b"replaced content!\n"
        assert replacement != original, "non-vacuity: the mutation changes nothing"
        barrier = threading.Barrier(3)
        out: list[tuple] = []
        refused: list[BaseException] = []
        lock = threading.Lock()

        def reader():
            barrier.wait(timeout=10)
            with source_snapshot(target) as snap:
                with lock:
                    out.append((snap.payload(), snap.sha256, snap.stable))

        def writer():
            barrier.wait(timeout=10)
            try:
                _stage(mechanism, target, replacement)
            except OSError as exc:
                with lock:
                    refused.append(exc)

        threads = [threading.Thread(target=reader), threading.Thread(target=reader),
                   threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert refused == [], (
            f"the {mechanism.value} writer was refused: {refused!r}. A measured "
            "capability was claimed and then denied, so this run proves nothing")
        assert len(out) == 2
        for payload, sha, stable in out:
            assert sha == hashlib.sha256(payload).hexdigest(), \
                "a reader returned bytes and a digest of different bytes"
            assert payload in (original, replacement), \
                f"a reader returned a mixture of the two versions: {payload!r}"
            if mechanism is Mutation.ATOMIC_REPLACE:
                assert stable is True, \
                    "an atomic replacement made a held observation incoherent"


# ── read_file end to end ─────────────────────────────────────────────────────

class TestReadFileIdentity:
    def test_a_normal_text_read_is_coherent_and_complete(self, sandbox):
        """BYTE_IDENTITY_TEST. MEASURED: with `write_text` this failed on the
        real Windows runner — the fixture was seven bytes and the digest was
        over six, because text mode translated the newline."""
        target = sandbox / "a.py"
        payload = _write(target, "x = 1\n")
        result = _executor()._tool_read_file(str(target))
        assert result["content"] == "x = 1\n"
        assert result["source"]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["source"]["size_bytes"] == len(payload)
        assert result["source"]["complete"] is True
        assert result["source"]["digest_covers"] == DIGEST_COVERS_COMPLETE
        assert result["truncated"] is False
        assert result["size_kb"] == round(len(payload) / 1024, 2)

    def test_an_empty_file_reads_as_empty_and_still_has_an_identity(self, sandbox):
        target = sandbox / "empty.txt"
        target.write_bytes(b"")
        result = _executor()._tool_read_file(str(target))
        assert result["content"] == ""
        assert result["source"]["size_bytes"] == 0
        assert result["source"]["sha256"] == hashlib.sha256(b"").hexdigest()
        assert result["source"]["complete"] is True

    @pytest.mark.parametrize("total,max_chars,truncated", [
        (100, 100, False), (101, 100, True), (99, 100, False),
    ])
    def test_the_truncation_boundary_is_unchanged(self, sandbox, total, max_chars,
                                                  truncated):
        target = sandbox / "t.txt"
        body = "a" * total
        _write(target, body)
        result = _executor()._tool_read_file(str(target), max_chars=max_chars)
        assert result["truncated"] is truncated
        assert result["source"]["content_chars_total"] == total
        assert result["source"]["content_chars_returned"] == min(total, max_chars)
        # The digest still covers the COMPLETE file either way.
        assert result["source"]["sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert result["source"]["digest_covers"] == DIGEST_COVERS_COMPLETE
        assert result["source"]["complete"] is not truncated

    def test_an_unsupported_extension_is_refused_without_a_read(self, sandbox):
        target = sandbox / "x.bin"
        target.write_bytes(b"\x00\x01")
        result = _executor()._tool_read_file(str(target))
        assert result["error_code"] == ex.ERR_UNSUPPORTED_EXTENSION
        assert "content" not in result and "source" not in result

    def test_a_missing_file_is_refused(self, sandbox):
        result = _executor()._tool_read_file(str(sandbox / "nope.txt"))
        assert result["error_code"] == ex.ERR_FILE_NOT_FOUND

    def test_an_in_place_rewrite_mid_read_yields_source_unstable(self, sandbox,
                                                                 monkeypatch):
        target = sandbox / "race.txt"
        target.write_bytes(b"A" * 64)
        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                with open(target, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"Z" * 64)
                    handle.flush()
                    os.fsync(handle.fileno())
            return data

        monkeypatch.setattr(os, "read", racing_read)
        try:
            result = _executor()._tool_read_file(str(target))
        finally:
            monkeypatch.setattr(os, "read", real_read)
        assert fired, "NON-VACUITY: the rewrite never fired"
        assert result["error_code"] == ex.ERR_SOURCE_UNSTABLE
        assert "content" not in result and "source" not in result

    @needs_replace
    def test_an_atomic_replacement_mid_read_keeps_the_result_coherent(
            self, sandbox, monkeypatch):
        """The executor-level version of the race. POSIX_CAPABILITY_TEST.

        The seam is inside the snapshot's own read loop, so this exercises the
        whole of `_tool_read_file` rather than the snapshot alone: the content it
        returns and the digest it reports must describe the same bytes even
        though the file on disk has moved on.

        Windows refuses the staging rename (WinError 5), and the handler wraps
        every exception into ERR_READ_FAILED, so there the failure arrived as a
        missing "content" key rather than as a race. The executor is attacked on
        every platform by `test_an_in_place_rewrite_mid_read_yields_source_unstable`
        directly above.
        """
        target = sandbox / "race.txt"
        v1 = "AAAA-version-one-AAAA\n"
        v2 = "BBBB-version-two-BBBB\n"
        _write(target, v1)
        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _atomic_replace(target, v2)
            return data

        monkeypatch.setattr(os, "read", racing_read)
        try:
            result = _executor()._tool_read_file(str(target))
        finally:
            monkeypatch.setattr(os, "read", real_read)
        assert fired, "NON-VACUITY: the race never fired"
        assert target.read_text() == v2, "the writer did not win on disk"
        assert result["content"] == v1
        assert result["source"]["sha256"] == hashlib.sha256(v1.encode()).hexdigest()
        assert result["source"]["sha256"] != hashlib.sha256(v2.encode()).hexdigest(), \
            "the digest describes bytes the caller never saw"
        assert result["source"]["size_bytes"] == len(v1.encode())
        # And the precondition it hands back correctly refuses to overwrite v2.
        receipt = cas_write_text(target, "derived\n",
                                 expected_sha256=result["source"]["sha256"])
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert target.read_text() == v2

    def test_a_post_read_mutation_does_not_retroactively_change_the_identity(
            self, sandbox):
        """BYTE_IDENTITY_TEST — `content` is the DECODED SOURCE BYTES.

        `snapshot.text()` decodes what was captured and performs no newline
        translation (correctly — that is what makes the digest describe the
        content). So a CRLF fixture makes `content` CRLF too, and the measured
        Windows failure here was `assert 'before\r\n' == 'before\n'`.
        """
        target = sandbox / "after.txt"
        _write(target, "before\n")
        result = _executor()._tool_read_file(str(target))
        before_sha = result["source"]["sha256"]
        _write(target, "after!!\n")
        assert result["source"]["sha256"] == before_sha
        assert result["content"] == "before\n"
        # And the identity is now honestly STALE relative to disk, which is what
        # makes the CAS precondition work.
        assert digest_file(target) != before_sha
        receipt = cas_write_text(target, "x\n", expected_sha256=before_sha)
        assert receipt.status is WriteStatus.REJECTED_STALE

    def test_a_read_then_write_round_trip_still_works_unraced(self, sandbox):
        target = sandbox / "rt.txt"
        _write(target, "one\n")
        result = _executor()._tool_read_file(str(target))
        receipt = cas_write_text(target, "two\n",
                                 expected_sha256=result["source"]["sha256"])
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_text() == "two\n"

    def test_a_truncated_read_can_still_state_a_write_precondition(self, sandbox):
        target = sandbox / "big.txt"
        body = "z" * 500
        _write(target, body)
        result = _executor()._tool_read_file(str(target), max_chars=10)
        assert result["truncated"] is True
        receipt = cas_write_text(target, "replacement\n",
                                 expected_sha256=result["source"]["sha256"])
        assert receipt.status is WriteStatus.APPLIED


class TestDerivedFormats:
    def test_a_derived_read_digests_the_source_file_not_the_rendering(self, sandbox):
        docx = pytest.importorskip("docx")
        target = sandbox / "doc.docx"
        document = docx.Document()
        document.add_paragraph("HELLO FROM THE DOCX")
        document.save(str(target))
        raw = target.read_bytes()

        result = _executor()._tool_read_file(str(target))
        assert "HELLO FROM THE DOCX" in result["content"]
        assert result["source"]["content_derived"] is True
        assert result["source"]["sha256"] == hashlib.sha256(raw).hexdigest(), \
            "the digest must identify the SOURCE FILE, not the extracted text"
        assert result["source"]["sha256"] != hashlib.sha256(
            result["content"].encode()).hexdigest()
        assert result["source"]["size_bytes"] == len(raw)

    @needs_replace
    def test_the_derived_parser_reads_the_snapshot_not_the_path(self, sandbox,
                                                               monkeypatch):
        """Replace the file on disk before the parser runs; the rendering must
        still come from the observed bytes. POSIX_CAPABILITY_TEST (the swap)."""
        docx = pytest.importorskip("docx")
        target = sandbox / "swap.docx"
        first = docx.Document()
        first.add_paragraph("ORIGINAL CONTENT")
        first.save(str(target))

        other = sandbox / "other.docx"
        second = docx.Document()
        second.add_paragraph("SWAPPED CONTENT")
        second.save(str(other))

        executor = _executor()
        real_reader = ex.ToolExecutor._read_docx
        fired: list[int] = []

        def swapping_reader(self, snapshot):
            if not fired:
                fired.append(1)
                os.replace(other, target)      # atomic swap before the parse
            return real_reader(self, snapshot)

        monkeypatch.setattr(ex.ToolExecutor, "_read_docx", swapping_reader)
        result = executor._tool_read_file(str(target))
        assert fired, "NON-VACUITY: the swap never fired"
        assert "ORIGINAL CONTENT" in result["content"], \
            "the parser re-opened the mutable path instead of the snapshot"
        assert "SWAPPED CONTENT" not in result["content"]

    def test_a_derived_read_digests_the_source_bytes_dependency_free(
            self, sandbox, monkeypatch):
        """The same property as the .docx test, without needing a parser.

        `python-docx` / `pdfplumber` are optional, so the two importorskip tests
        above do not run everywhere. This one exercises the executor's own
        composition — which is where the finding lived — using a stub reader that
        reads the snapshot it is handed.
        """
        target = sandbox / "doc.pdf"
        raw = b"%PDF-1.4 synthetic source bytes\n" * 3
        target.write_bytes(raw)
        seen: list[bytes] = []

        def stub_reader(self, snapshot):
            with snapshot.stream() as handle:
                seen.append(handle.read())
            return "EXTRACTED TEXT, NOT THE SOURCE BYTES"

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", stub_reader)
        result = _executor()._tool_read_file(str(target))
        assert seen == [raw], "the reader did not receive the observed bytes"
        assert result["content"] == "EXTRACTED TEXT, NOT THE SOURCE BYTES"
        assert result["source"]["content_derived"] is True
        assert result["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["source"]["sha256"] != hashlib.sha256(
            result["content"].encode()).hexdigest()
        assert result["source"]["size_bytes"] == len(raw)

    @needs_replace
    def test_a_derived_parser_cannot_see_a_post_observation_swap(
            self, sandbox, monkeypatch):
        """Dependency-free version of the atomic-swap test. POSIX_CAPABILITY_TEST.

        The in-place analogue, which runs everywhere, is
        `test_a_derived_parser_cannot_see_a_post_observation_in_place_rewrite`.
        """
        target = sandbox / "swap.pdf"
        other = sandbox / "other.pdf"
        target.write_bytes(b"ORIGINAL-SOURCE")
        other.write_bytes(b"SWAPPED-SOURCE")
        seen: list[bytes] = []

        def swapping_reader(self, snapshot):
            os.replace(other, target)          # atomic swap before the parse
            with snapshot.stream() as handle:
                seen.append(handle.read())
            return "rendered"

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", swapping_reader)
        result = _executor()._tool_read_file(str(target))
        assert seen == [b"ORIGINAL-SOURCE"],             "the parser read the swapped path instead of the snapshot"
        assert target.read_bytes() == b"SWAPPED-SOURCE", "the swap did not happen"
        assert result["source"]["sha256"] == hashlib.sha256(
            b"ORIGINAL-SOURCE").hexdigest()

    def test_a_derived_parse_over_changed_bytes_is_refused_not_returned(
            self, sandbox, monkeypatch):
        """CROSS_PLATFORM_INVARIANT — and a property worth stating plainly.

        MEASURED WHILE WRITING THIS TEST. A derived read uses
        `capture_bytes=False`, so `stream()` is a `dup` of the held descriptor
        and NOT a byte copy: it addresses the same INODE. An in-place rewrite
        therefore genuinely does change what the parser reads — the first
        version of this test asserted the parser still saw the original bytes
        and failed, correctly.

        That is not a defect. It is exactly why `capture_bytes=False` REQUIRES
        `recheck()`. The invariant is not "the parser cannot see the change";
        it is "a rendering and a digest over different bytes are never returned
        as one result". So: the parser sees the new bytes, the digest covers the
        old ones, they provably disagree, and the whole read is REFUSED.

        The stronger property — that the parser addresses the observation
        rather than re-resolving the path — needs the path to MEAN something
        else, which is the POSIX-only rename. It is asserted by the
        capability-gated swap tests above and, behaviourally and on every
        platform, by `test_a_derived_parser_never_reopens_the_source_path`.
        """
        target = sandbox / "shift-derived.pdf"
        original = b"ORIGINAL-SOURCE-BYTES-0123456789"
        rewritten = b"SWAPPED!-SOURCE-BYTES-0123456789"
        assert len(original) == len(rewritten), "same-size rewrite"
        target.write_bytes(original)
        seen: list[bytes] = []

        def rewriting_reader(self, snapshot):
            _in_place_rewrite(target, rewritten)
            with snapshot.stream() as handle:
                seen.append(handle.read())
            return "rendered"

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", rewriting_reader)
        result = _executor()._tool_read_file(str(target))
        assert target.read_bytes() == rewritten, "NON-VACUITY: no rewrite landed"
        assert seen == [rewritten], \
            "NON-VACUITY: the parser did not observe the rewrite at all"
        assert hashlib.sha256(seen[0]).hexdigest() \
            != hashlib.sha256(original).hexdigest(), \
            "non-vacuity: the two versions hash the same"
        assert result.get("error_code") == ex.ERR_SOURCE_UNSTABLE, \
            "a rendering and a digest over different bytes were returned as one"
        assert "content" not in result and "source" not in result

    def test_a_derived_parser_never_reopens_the_source_path(self, sandbox,
                                                            monkeypatch):
        """CROSS_PLATFORM_INVARIANT — BEHAVIOURAL, where the siblings are structural.

        Every other proof that a derived reader consumes the observation is
        either a source-text scan (`TestAbsentControls`) or needs the POSIX
        rename. This one watches the real openers: once the snapshot has been
        acquired, any `open()` or `os.open()` of the source path during the
        parse is recorded. A reader that re-resolved the path would be caught on
        Windows exactly as on Linux.
        """
        import builtins

        target = sandbox / "watched.pdf"
        raw = b"%PDF-1.4 watched source bytes\n" * 4
        target.write_bytes(raw)
        resolved = os.path.realpath(str(target))
        reopened: list[str] = []
        seen: list[bytes] = []
        real_builtin_open, real_os_open = builtins.open, os.open

        def _is_source(candidate) -> bool:
            if isinstance(candidate, int):
                return False                 # a descriptor, not a path
            try:
                return os.path.realpath(os.fspath(candidate)) == resolved
            except (TypeError, ValueError):
                return False

        def watching_reader(self, snapshot):
            def guarded_builtin_open(file, *a, **kw):
                if _is_source(file):
                    reopened.append(f"open({file!r})")
                return real_builtin_open(file, *a, **kw)

            def guarded_os_open(path, *a, **kw):
                if _is_source(path):
                    reopened.append(f"os.open({path!r})")
                return real_os_open(path, *a, **kw)

            monkeypatch.setattr(builtins, "open", guarded_builtin_open)
            monkeypatch.setattr(os, "open", guarded_os_open)
            try:
                # Non-vacuity: the bare name now resolves to the guard, so a
                # path opening really is recorded. Without this the test would
                # pass just as happily with both guards installed backwards.
                with open(str(target), "rb") as handle:
                    handle.read(1)
                assert reopened, "non-vacuity: the open guard never fired"
                reopened.clear()
                with snapshot.stream() as handle:
                    seen.append(handle.read())
            finally:
                monkeypatch.setattr(builtins, "open", real_builtin_open)
                monkeypatch.setattr(os, "open", real_os_open)
            return "rendered"

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", watching_reader)
        result = _executor()._tool_read_file(str(target))
        assert seen == [raw], "the stream did not serve the observed bytes"
        assert reopened == [], \
            f"the parse re-resolved the source path: {reopened}"
        assert result["source"]["sha256"] == hashlib.sha256(raw).hexdigest()

    def test_an_in_place_rewrite_during_a_derived_parse_is_reported_unstable(
            self, sandbox, monkeypatch):
        """The derived path's two passes over one descriptor need `recheck()`.

        An atomic replacement cannot disturb a held fd, but an in-place rewrite
        between the digest pass and the parser's pass can — and then the
        rendering and the digest would describe different bytes.
        """
        target = sandbox / "shift.pdf"
        target.write_bytes(b"A" * 64)

        def rewriting_reader(self, snapshot):
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(b"Z" * 64)
                handle.flush()
                os.fsync(handle.fileno())
            return "rendered from shifting bytes"

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", rewriting_reader)
        result = _executor()._tool_read_file(str(target))
        assert result["error_code"] == ex.ERR_SOURCE_UNSTABLE
        assert "content" not in result and "source" not in result

    def test_a_derived_parser_failure_is_a_read_error_not_a_success(
            self, sandbox, monkeypatch):
        target = sandbox / "broken.pdf"
        target.write_bytes(b"not really a pdf")

        def exploding_reader(self, snapshot):
            raise ValueError("synthetic parser failure")

        monkeypatch.setattr(ex.ToolExecutor, "_read_pdf", exploding_reader)
        result = _executor()._tool_read_file(str(target))
        assert result["error_code"] == ex.ERR_READ_FAILED
        assert "content" not in result and "source" not in result

    def test_every_derived_reader_takes_a_snapshot_not_a_path(self):
        """Absent-control: a reader whose parameter is a Path WILL reopen it."""
        import inspect

        readers = ("_read_pdf", "_read_docx", "_read_xlsx", "_read_pptx",
                   "_read_rtf", "_read_image_ocr")
        assert readers                                   # non-vacuity
        for name in readers:
            fn = getattr(ex.ToolExecutor, name)
            params = list(inspect.signature(fn).parameters)
            assert params == ["self", "snapshot"], \
                f"{name} takes {params}; it must take a snapshot"
            body = code_region(f"def {name}(self, snapshot)")
            assert "snapshot . stream ( )" in body, \
                f"{name} does not read from the snapshot's stream"
            for reopen in ("str ( path )", "path . read_text", "open ( path"):
                assert reopen not in body, f"{name} reopens the path"

    def test_the_derived_dispatch_covers_every_declared_derived_extension(self):
        """Exhaustive, not fall-through.

        The original chain ended in an unconditional ``return
        self._read_image_ocr(snapshot)``, so declaring a new derived extension
        silently made it an OCR attempt. The table has to name every one.
        """
        table = ex.ToolExecutor._DERIVED_READERS
        assert set(table) == ex._DERIVED_SOURCE_EXTENSIONS, (
            f"declared but unreadable: "
            f"{sorted(ex._DERIVED_SOURCE_EXTENSIONS - set(table))}; "
            f"readable but undeclared: {sorted(set(table) - ex._DERIVED_SOURCE_EXTENSIONS)}")
        for ext, reader in table.items():
            assert hasattr(ex.ToolExecutor, reader), f"{ext} names a missing {reader}"

    def test_an_undeclared_derived_extension_fails_closed(self):
        with pytest.raises(ValueError):
            ex.ToolExecutor.__new__(ex.ToolExecutor)._read_derived(".nope", object())

    def test_the_extension_sets_are_disjoint_and_complete(self):
        assert not (ex._TEXT_SOURCE_EXTENSIONS & ex._DERIVED_SOURCE_EXTENSIONS)
        assert ex._READABLE_EXTENSIONS == (
            ex._TEXT_SOURCE_EXTENSIONS | ex._DERIVED_SOURCE_EXTENSIONS)
        assert ex._READABLE_EXTENSIONS                   # non-vacuity


# ── absent-control ───────────────────────────────────────────────────────────

class TestAbsentControls:
    def test_read_file_does_not_use_the_old_reopening_composition(self):
        body = code_region("def _tool_read_file(self, path: str, max_chars")
        assert "source_snapshot (" in body, "read_file no longer takes a snapshot"
        assert "identify_snapshot (" in body
        assert "identify_source (" not in body, \
            "read_file hashes a LATER reopening of the path again"

    def test_read_file_never_reads_or_stats_the_path_a_second_time(self):
        body = code_region("def _tool_read_file(self, path: str, max_chars")
        for reopen in ("p . read_text", "p . read_bytes", "p . stat ( )",
                       "open ( p ,", "digest_file ("):
            assert reopen not in body, f"read_file performs a second {reopen}"
        assert "snapshot . size_bytes" in body, \
            "size_kb must come from the snapshot, not a fresh stat"

    def test_the_cas_precondition_is_sourced_from_the_snapshot_identity(self):
        body = code_region("def _tool_read_file(self, path: str, max_chars")
        assert "identity = identify_snapshot (" in body
        assert 'identity . to_dict ( )' in body

    def test_an_unstable_snapshot_short_circuits_before_any_content_is_built(self):
        body = code_region("def _tool_read_file(self, path: str, max_chars")
        unstable = body.index("ERR_SOURCE_UNSTABLE")
        built = body.index("full_chars = len ( content )")
        assert unstable < built, \
            "the stability check runs after the content is assembled"

    def test_identify_snapshot_cannot_describe_a_file_it_did_not_observe(self):
        """It takes a snapshot, never a path — so there is no reopening to do."""
        import inspect

        params = list(inspect.signature(identify_snapshot).parameters)
        assert params[0] == "snapshot"
        source = inspect.getsource(identify_snapshot)
        for reopen in ("digest_file(", "os.open(", ".stat()", "open("):
            assert reopen not in source, f"identify_snapshot performs a {reopen}"

    def test_the_snapshot_is_the_only_thing_read_file_opens(self):
        """Non-vacuity: the detector sees the real opener, then asserts the count."""
        body = code_region("def _tool_read_file(self, path: str, max_chars")
        assert body.count("source_snapshot (") == 1

    def test_source_snapshot_opens_the_path_exactly_once(self):
        import inspect

        source = inspect.getsource(si.source_snapshot)
        assert source.count("os.open(") == 1, \
            "the snapshot opens the path more than once"
        assert "os.fstat(fd)" in source, \
            "stability must be checked on the HELD descriptor, not by path"
        assert source.count("os.fstat(fd)") >= 2, \
            "one fstat cannot detect a change during the observation"
