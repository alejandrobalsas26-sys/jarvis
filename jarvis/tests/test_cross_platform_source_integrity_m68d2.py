"""V69 M68D.2 — cross-platform source integrity: binary-exact reads, race truth.

WHY THIS SUITE EXISTS
=====================
Two real Windows CI executions found what Linux-only testing could not. The
latest, run **37994281455** on candidate ``42c0d0c336f1c6a5e6aa730161b5613f36dbfa81``,
reported 254 passed / 6 failed / 17 skipped, and **two of the six were production
integrity defects**, not test portability:

GROUP A — the source descriptor was opened in TEXT mode.
    ``os.open(path, os.O_RDONLY)`` gets a text-mode descriptor from the Windows C
    runtime, and ``os.read`` on one collapses ``\\r\\n`` to ``\\n`` and stops at the
    first Ctrl-Z. ``b"a\\r\\nb\\r\\n"`` came back as ``b"a\\nb\\n"``;
    ``bytes(range(256)) * 40`` came back **26 bytes** long. The snapshot digested
    whatever it got, so ``sha256`` named a file state that never existed.

GROUP B — the stability witness was metadata-only.
    ``st_ino``, ``st_dev``, ``st_size`` and ``st_mtime_ns``, compared across the
    read. A same-size in-place rewrite of the held inode moves none of the first
    three, and ``st_mtime_ns`` carries nanosecond UNITS over roughly 15 ms of
    RESOLUTION, so a rewrite inside one clock tick moves nothing at all. The
    runner published a payload **torn across two versions** as ``stable``, with
    ``complete: true`` and a write precondition on top of it.

HOW THE WINDOWS PATH IS TESTED FROM A POSIX HOST
================================================
Group A cannot be reproduced natively: Linux has no text mode. So the C runtime
is SIMULATED — and the simulation is driven by exactly ONE injected attribute,
``os.O_BINARY``, from which production derives its own flags through
``source_integrity._source_open_flags()``. Nothing here re-implements that
derivation, so a passing result is a statement about production rather than
about this harness. :class:`TestTheSimulatorIsFaithful` proves the simulator
corrupts an unrepaired read, which is what makes the repaired result mean
anything.

Group B needs no simulation. The Windows condition — a same-size rewrite whose
mtime does not visibly advance — is staged deterministically on any platform by
RESTORING the mtime with ``os.utime`` after the write, which is what a coarse
filesystem clock does for free. That reproduction returns the **same torn
payload and the same digest** the real runner produced.

WHAT THIS SUITE DOES NOT CLAIM
==============================
Not a transactional snapshot. See ``TestTheThreatModelIsStated`` and
``docs/v69_M68D2_CROSS_PLATFORM_SOURCE_INTEGRITY.md``: the byte witness detects
a mutation still visible when it runs, and provably cannot exclude an ABA
rewrite within one clock tick, or one that lands after the witness. Those are
declared, not engineered away.

NO CAPABILITY GATE LIVES HERE
=============================
Every test in this file runs on every platform. Where a mechanism may be
refused (an atomic replace or an unlink over a held path, which Windows denies),
the test stages it and asserts the correct invariant in BOTH branches rather
than skipping — and a refused adversary is reported as CAPABILITY_UNAVAILABLE,
never counted as adversarial coverage (§12).
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import os
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import core.source_integrity as si
from core.source_integrity import (
    DIGEST_COVERS_COMPLETE,
    DIGEST_COVERS_NOTHING,
    SourceSnapshotError,
    WriteStatus,
    cas_write_text,
    digest_bytes,
    identify_snapshot,
    source_snapshot,
)
from _test_support.platform_capabilities import (
    REPLACE_OVER_OPEN_PATH,
    UNLINK_OPEN_PATH,
)
from _test_support import platform_capabilities as capability_reasons
from _test_support.suite_census import (
    _SKIP_LINE,
    SuiteCensus,
    census,
    collected_node_ids,
    ratio_is_satisfiable,
    skip_budget,
)

_JARVIS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _JARVIS_ROOT.parent

#: The real Windows value of ``_O_BINARY``. Used ONLY to simulate the platform
#: that has one; production reads it off ``os`` and never sees this constant.
WIN_O_BINARY = 0x8000

#: Byte fixtures the snapshot must reproduce EXACTLY. Each one targets a
#: specific way a text-mode descriptor loses or invents bytes.
BYTE_FIXTURES: "dict[str, bytes]" = {
    # Nothing to translate: the control that must behave identically either way.
    "lf": b"a\nb\n",
    # The headline Group A failure, verbatim from the runner.
    "crlf": b"a\r\nb\r\n",
    "mixed_lf_crlf": b"a\r\nb\nc\r\n",
    # A CR with no LF after it. Text mode leaves it alone, so this is the
    # fixture that proves the repair is not just "strip every \r".
    "lone_cr": b"a\rb\r",
    "cr_at_end": b"ab\r",
    "empty": b"",
    "bare_lf": b"\n",
    "no_final_newline": b"no final newline",
    # Multi-byte UTF-8 immediately before a CRLF.
    "utf8_unicode": "ñ→\U0001f642\r\n".encode(),
    # NUL is not a terminator for a byte-exact read, and must not become one.
    "nul_bytes": b"\x00\x01\x00\r\n",
    # 0x1A is Ctrl-Z. A text-mode read STOPS here, which is why the stream-mode
    # digest came back 26 bytes long on the real runner.
    "ctrl_z_early": b"abc\x1adef\r\nghi",
    "binary_all_bytes": bytes(range(256)),
    # Long enough to span several chunks at the production chunk size used by
    # the race tests, with the interesting bytes ON the boundaries.
    "crlf_on_chunk_boundary": b"x" * 7 + b"\r\n" + b"y" * 7 + b"\r\n",
    "multibyte_on_boundary": b"x" * 7 + "ñ→".encode() + b"\r\n" + b"z" * 9,
}


@contextlib.contextmanager
def windows_text_runtime(*, binary_available: bool):
    """Simulate the Windows C runtime's TEXT-mode descriptors.

    A descriptor opened WITHOUT ``_O_BINARY`` translates ``\\r\\n`` to ``\\n`` on
    read and treats Ctrl-Z as end-of-file — the two behaviours measured on the
    real runner. Everything else is left alone.

    ``binary_available`` is the whole experiment. ``True`` gives ``os`` the
    attribute a Windows CPython has, so production's own
    ``_source_open_flags()`` asks for binary mode and the translation never
    applies to it. ``False`` removes the attribute, which is both the POSIX
    reality and the exact shape of the regression this suite guards: production
    then opens a translating descriptor, and the simulator corrupts it.
    """
    real_open, real_read = os.open, os.read
    text_fds: "set[int]" = set()
    had = hasattr(os, "O_BINARY")
    previous = getattr(os, "O_BINARY", None)

    def win_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags & ~WIN_O_BINARY, *args, **kwargs)
        if not flags & WIN_O_BINARY:
            text_fds.add(fd)
        return fd

    def win_read(fd, size):
        data = real_read(fd, size)
        if fd not in text_fds:
            return data
        cut = data.find(b"\x1a")
        if cut != -1:
            data = data[:cut]
        return data.replace(b"\r\n", b"\n")

    if binary_available:
        os.O_BINARY = WIN_O_BINARY
    elif had:
        del os.O_BINARY
    os.open, os.read = win_open, win_read
    try:
        yield
    finally:
        os.open, os.read = real_open, real_read
        if had:
            os.O_BINARY = previous
        elif hasattr(os, "O_BINARY"):
            del os.O_BINARY


#: Captured at import, BEFORE any test can patch them, so the leak check below
#: compares against the real thing rather than against whatever is installed.
_REAL_OPEN, _REAL_READ = os.open, os.read


def _no_strings_of(source: str) -> str:
    """*source* with every COMMENT and STRING token dropped.

    The view a detector must use on a file that may be its own: a search
    pattern is a STRING token while a decorator, an attribute access and a call
    are NAME/OP tokens, so dropping strings separates the detector from what it
    detects. Without this, a scan for a token this file NAMES matches its own
    search literal and reports a violation that is not there.
    """
    out: "list[str]" = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if tok.string.strip():
            out.append(tok.string)
    return " ".join(out)


def _no_strings(path: Path) -> str:
    """:func:`_no_strings_of` for a file on disk."""
    return _no_strings_of(path.read_text(encoding="utf-8"))


def _stage(tmp_path: Path, name: str, body: bytes) -> Path:
    """Write *body* BYTE-EXPLICITLY. Never a translating handle (§6)."""
    target = tmp_path / f"{name}.bin"
    target.write_bytes(body)
    assert target.read_bytes() == body, "the fixture itself was translated"
    return target


# ══ 1 · GROUP A — THE BYTES ON DISK ARE THE BYTES CAPTURED ══════════════════

class TestBinaryByteIdentity:
    """§5 — SOURCE_BYTES_ON_DISK == BYTES_CAPTURED_BY_SNAPSHOT."""

    def test_production_asks_the_platform_for_binary_mode(self):
        """The flag set, derived by production, on a platform that HAS the flag.

        Calls ``_source_open_flags()`` itself rather than re-deriving it, so a
        future edit that stops consulting ``os.O_BINARY`` fails here.
        """
        with windows_text_runtime(binary_available=True):
            flags = si._source_open_flags()
            assert flags & WIN_O_BINARY, \
                "the source open does not request binary mode on Windows"
            assert flags & os.O_RDONLY == os.O_RDONLY, "the open is not read-only"
            for forbidden in (os.O_WRONLY, os.O_RDWR, getattr(os, "O_CREAT", 0),
                              getattr(os, "O_TRUNC", 0), getattr(os, "O_APPEND", 0)):
                if forbidden:
                    assert not flags & forbidden, \
                        "identifying a source must never open it for writing"

    def test_the_flag_set_is_unchanged_where_there_is_no_text_mode(self):
        """On POSIX the repair must be a byte-for-byte no-op."""
        with windows_text_runtime(binary_available=False):
            assert si._source_open_flags() == os.O_RDONLY, \
                "the repair changed the POSIX flag set"

    @pytest.mark.parametrize("name", list(BYTE_FIXTURES))
    def test_every_fixture_survives_a_text_translating_runtime(self, tmp_path,
                                                               name):
        """CROSS_PLATFORM_INVARIANT — payload, size and digest, all four ways."""
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target) as snap:
                assert snap.payload() == body, "the snapshot changed the bytes"
                assert snap.size_bytes == len(body), "the byte count was shortened"
                assert snap.sha256 == hashlib.sha256(body).hexdigest(), \
                    "the digest is not over the bytes on disk"
                assert snap.stable is True, \
                    "a quiet file was reported unstable"
                assert snap.recheck() is True

    @pytest.mark.parametrize("name", list(BYTE_FIXTURES))
    def test_stream_mode_digests_the_bytes_on_disk(self, tmp_path, name):
        """The ``capture_bytes=False`` path, which failed on Ctrl-Z for real."""
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.sha256 == hashlib.sha256(body).hexdigest()
                assert snap.size_bytes == len(body)
                with snap.stream() as handle:
                    served = handle.read()
                assert served == body, "the stream served translated bytes"
                assert snap.recheck() is True

    def test_the_ctrl_z_truncation_is_closed(self, tmp_path):
        """The measured stream-mode failure, isolated.

        ``bytes(range(256)) * 40`` is 10240 bytes with a 0x1A at offset 26. The
        real runner digested 26 of them and reported success.
        """
        body = bytes(range(256)) * 40
        target = _stage(tmp_path, "ctrlz", body)
        assert body.index(b"\x1a") == 26, "the fixture lost its Ctrl-Z"
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.size_bytes == 10240, \
                    f"the read stopped at a Ctrl-Z: {snap.size_bytes} bytes"
                assert snap.sha256 == hashlib.sha256(body).hexdigest()

    def test_a_crlf_source_is_never_rejected_for_being_crlf(self, tmp_path):
        """POSITIVE CONTROL (§11) — and a second, quieter Group A symptom.

        Unrepaired, ``total`` came back SHORTER than ``st_size`` for every CRLF
        file, so the metadata witness declared it unstable: on Windows, every
        CRLF source was ALSO denied an identity. The three reported Group A
        failures masked this by asserting the payload first.
        """
        target = _stage(tmp_path, "crlf_only", b"line one\r\nline two\r\n")
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target) as snap:
                assert snap.stable is True, \
                    "a CRLF file was rejected purely for its line endings"
                identity = identify_snapshot(snap)
                assert identity.complete is True
                assert identity.digest_covers == DIGEST_COVERS_COMPLETE
                assert identity.sha256 == digest_bytes(b"line one\r\nline two\r\n")

    def test_rendering_may_decode_but_only_after_the_bytes_are_fixed(self,
                                                                     tmp_path):
        """§5 — transformation is allowed DOWNSTREAM of the byte observation."""
        body = b"x = 1\r\ny = 2\r\n"
        target = _stage(tmp_path, "render", body)
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target) as snap:
                assert snap.payload() == body
                assert snap.text() == "x = 1\r\ny = 2\r\n", \
                    "text() is a decode of the captured bytes, not a rewrite"
                # A caller is free to normalise its OWN copy. The authority is
                # the digest, and it still describes the disk.
                assert snap.text().replace("\r\n", "\n") == "x = 1\ny = 2\n"
                assert snap.sha256 == digest_bytes(body)

    def test_the_digest_helpers_never_normalise(self, tmp_path):
        """§7's forbidden fix, asserted behaviourally on both helpers."""
        crlf = _stage(tmp_path, "h_crlf", b"a\r\nb\r\n")
        lf = _stage(tmp_path, "h_lf", b"a\nb\n")
        with windows_text_runtime(binary_available=True):
            assert si.digest_file(crlf) != si.digest_file(lf), \
                "digest_file normalises newlines"
            assert si.digest_file(crlf) == digest_bytes(b"a\r\nb\r\n")
            assert digest_bytes(b"a\r\nb\r\n") != digest_bytes(b"a\nb\n")

    def test_a_cas_precondition_taken_under_translation_still_matches(self,
                                                                      tmp_path):
        """END TO END — observe, then write against the digest just observed."""
        body = b"first\r\nsecond\r\n"
        target = _stage(tmp_path, "cas", body)
        with windows_text_runtime(binary_available=True):
            with source_snapshot(target) as snap:
                precondition = identify_snapshot(snap).sha256
            assert precondition == digest_bytes(body)
            receipt = cas_write_text(target, "third\r\nfourth\r\n",
                                     expected_sha256=precondition)
        assert receipt.status is WriteStatus.APPLIED, \
            f"a coherent precondition was refused: {receipt.status}"
        assert target.read_bytes() == b"third\r\nfourth\r\n", \
            "the CAS write rewrote the caller's line endings"


class TestTheSimulatorIsFaithful:
    """§12 — zero structural matches is never successful evidence."""

    @pytest.mark.parametrize("name", [
        "crlf", "mixed_lf_crlf", "utf8_unicode", "nul_bytes", "ctrl_z_early",
        "binary_all_bytes", "crlf_on_chunk_boundary", "multibyte_on_boundary",
    ])
    def test_without_the_flag_the_simulator_really_does_corrupt(self, tmp_path,
                                                                name):
        """NON-VACUITY. If this passed clean, the suite above would prove nothing.

        ``binary_available=False`` is precisely the regression: the platform
        offers no binary mode, so production opens a translating descriptor.
        Every fixture here must then come back WRONG.
        """
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=False):
            with source_snapshot(target) as snap:
                assert snap.payload() != body, \
                    "the simulator did not translate, so it proves nothing"
                assert snap.size_bytes < len(body), \
                    "the simulator did not shorten the read"
                assert snap.sha256 != hashlib.sha256(body).hexdigest()

    def test_the_untranslatable_fixtures_are_unaffected_either_way(self, tmp_path):
        """The simulator must be surgical: no \\r\\n and no Ctrl-Z, no change."""
        for name in ("lf", "lone_cr", "cr_at_end", "empty", "bare_lf",
                     "no_final_newline"):
            body = BYTE_FIXTURES[name]
            target = _stage(tmp_path, f"quiet_{name}", body)
            with windows_text_runtime(binary_available=False):
                with source_snapshot(target) as snap:
                    assert snap.payload() == body, \
                        f"the simulator corrupted {name}, which has nothing to translate"

    def test_the_simulator_leaves_the_os_module_as_it_found_it(self):
        """A leaked ``os.O_BINARY`` would silently repair every later test."""
        before = hasattr(os, "O_BINARY")
        with windows_text_runtime(binary_available=True):
            assert hasattr(os, "O_BINARY")
        assert hasattr(os, "O_BINARY") is before, "os.O_BINARY leaked"
        with windows_text_runtime(binary_available=False):
            pass
        assert hasattr(os, "O_BINARY") is before, "os.O_BINARY was lost"
        # And `os.open`/`os.read` themselves are back, or every test after this
        # one would be reading through the simulator without knowing it.
        assert os.open is _REAL_OPEN, "os.open was left patched"
        assert os.read is _REAL_READ, "os.read was left patched"


# ══ 2 · GROUP B — A STABILITY VERDICT WITNESSED AT THE BYTE LEVEL ═══════════

@dataclass
class RaceWitness:
    """§12 — proof that the adversarial mutation ACTUALLY occurred.

    A concurrency test that cannot show its writer ran is not evidence of
    anything: it passes just as well when the mutation landed before the
    observation started, or never landed at all. Every race below fills one of
    these and asserts on it BEFORE it asserts on the reader's verdict.
    """

    #: The writer was reached and tried.
    writer_attempted: bool = False
    #: It was not refused by the platform.
    writer_succeeded: bool = False
    #: The bytes on disk really did change.
    mutation_observed: bool = False
    #: Anything the writer raised. A dead thread must not become a pass.
    writer_errors: "list[BaseException]" = field(default_factory=list)
    #: What the reader ended up with.
    reader_payload: "bytes | None" = None
    #: The snapshot's own verdict.
    stability_verdict: "bool | None" = None

    def assert_adversary_was_staged(self) -> None:
        assert self.writer_attempted, "NON-VACUITY: the writer was never reached"
        assert not self.writer_errors, (
            "the adversary was REFUSED, so this race proves nothing: "
            f"{self.writer_errors!r} — report CAPABILITY_UNAVAILABLE instead")
        assert self.writer_succeeded, "NON-VACUITY: the writer did not complete"
        assert self.mutation_observed, \
            "NON-VACUITY: the bytes on disk never changed"


def _race_during_read(target: Path, mutate, *, was: bytes, chunk: int = 8,
                      monkeypatch) -> "tuple[dict, RaceWitness]":
    """Run ``source_snapshot`` with *mutate* firing inside the FIRST read.

    Deterministic by construction and with no sleeps anywhere: the mutation is
    invoked from inside ``os.read`` itself, so it is guaranteed to land after
    the observation has begun and before it has finished. ``chunk`` forces a
    multi-chunk read so there is a mid-read window to land in at all.

    ``was`` is the content the race started from, so the witness can state
    that the bytes on disk actually changed rather than assuming it.

    Returns the snapshot's own observations (payload, digest, stability) read
    while the context is still open, plus the witness.
    """
    witness = RaceWitness()
    real_read = os.read
    fired: "list[int]" = []

    def racing_read(fd, size):
        data = real_read(fd, size)
        if not fired:
            fired.append(1)
            witness.writer_attempted = True
            try:
                mutate()
                witness.writer_succeeded = True
            except BaseException as exc:          # noqa: BLE001 - reported
                witness.writer_errors.append(exc)
        return data

    monkeypatch.setattr(si, "_DIGEST_CHUNK", chunk)
    monkeypatch.setattr(os, "read", racing_read)
    with source_snapshot(target) as snap:
        monkeypatch.setattr(os, "read", real_read)
        with contextlib.suppress(OSError):
            witness.mutation_observed = target.read_bytes() != was
        witness.reader_payload = snap.payload()
        witness.stability_verdict = snap.stable
        observed = {
            "payload": snap.payload(),
            "sha256": snap.sha256,
            "size_bytes": snap.size_bytes,
            "stable": snap.stable,
            "recheck": snap.recheck(),
            "identity": identify_snapshot(snap),
        }
    return observed, witness


class TestByteLevelStability:
    """§8–§10 — metadata-only stability is insufficient, and is no longer used."""

    def test_the_measured_windows_race_is_reported_unstable(self, tmp_path,
                                                            monkeypatch):
        """THE REPRODUCTION. Same size, same inode, same visibly-unchanged mtime.

        This is the real runner's failure, staged on any platform: the mtime is
        RESTORED after the write, which is what a filesystem clock with ~15 ms
        of resolution does by itself. Unrepaired, this produced the payload
        ``b"AAAA-veran-wrote-this\\n"`` — torn across both versions — and the
        digest ``bc29f2ad74f53c8e1bedd45f12b0e20ff7d2b78ae811eb85fc7aa9cdd12676cc``,
        byte-identical to the one CI run 37994281455 reported, with
        ``stable is True`` and that digest offered as a write precondition.
        """
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-human-wrote-this\n"
        assert len(v1) == len(v2), "same-size rewrite"
        target = _stage(tmp_path, "measured_race", v1)
        pre = os.stat(target)

        def mutate():
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(v2)
                handle.flush()
                os.fsync(handle.fileno())
            # The Windows condition, made deterministic.
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=v1,
                                              monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()

        # NON-VACUITY: the four metadata fields really are all unchanged, so
        # the old witness really would have accepted this.
        post = os.stat(target)
        assert (pre.st_ino, pre.st_dev, pre.st_size, pre.st_mtime_ns) == \
            (post.st_ino, post.st_dev, post.st_size, post.st_mtime_ns), \
            "the metadata moved, so this is not the race that was measured"
        assert observed["payload"] not in (v1, v2), \
            "NON-VACUITY: the read was not torn, so nothing was at stake"

        assert observed["stable"] is False, \
            "a torn observation was published as stable"
        assert observed["recheck"] is False, "recheck re-blessed a torn read"
        identity = observed["identity"]
        assert identity.sha256 is None, \
            "a raced observation handed out a write precondition"
        assert identity.digest_covers == DIGEST_COVERS_NOTHING
        assert identity.complete is False

    def test_that_race_cannot_authorise_a_write(self, tmp_path, monkeypatch):
        """§10 — no usable precondition, and the human's bytes survive."""
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-human-wrote-this\n"
        target = _stage(tmp_path, "no_authority", v1)
        pre = os.stat(target)

        def mutate():
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(v2)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=v1,
                                              monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()
        assert observed["identity"].sha256 is None, \
            "a raced observation handed out a write precondition"

        # The digest it DID compute describes a mixture. Offered as a
        # precondition it must lose, because the file holds the human's edit.
        receipt = cas_write_text(target, "CCCC-derived-from-V1-CCC\n",
                                 expected_sha256=observed["sha256"])
        assert receipt.status is WriteStatus.REJECTED_STALE, \
            f"a torn digest authorised a write: {receipt.status}"
        assert receipt.applied is False
        assert receipt.bytes_written == 0
        assert target.read_bytes() == v2, "the unread human edit was destroyed"

    def test_a_mutation_after_the_payload_but_before_publication_is_caught(
            self, tmp_path, monkeypatch):
        """§11.4 — the window between acquiring bytes and naming them.

        The mutation fires from the LAST read — the one that returns b"" — so
        the payload is already complete and only the witness can still see it.
        """
        body = b"Q" * 64
        after = b"R" * 64
        target = _stage(tmp_path, "post_payload", body)
        witness = RaceWitness()
        pre = os.stat(target)
        real_read = os.read
        seen: "list[bytes]" = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            seen.append(data)
            if not data and not witness.writer_attempted:
                witness.writer_attempted = True
                try:
                    with open(target, "r+b") as handle:
                        handle.seek(0)
                        handle.write(after)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))
                    witness.writer_succeeded = True
                except BaseException as exc:      # noqa: BLE001 - reported
                    witness.writer_errors.append(exc)
            return data

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 16)
        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            witness.mutation_observed = target.read_bytes() == after
            witness.assert_adversary_was_staged()
            assert snap.payload() == body, \
                "NON-VACUITY: the payload was already whole when the race fired"
            assert snap.stable is False, \
                "a mutation between acquisition and publication was not seen"
            assert identify_snapshot(snap).sha256 is None

    def test_a_mutation_during_multi_chunk_reading_is_caught(self, tmp_path,
                                                             monkeypatch):
        """§11.5 — the mutation lands BEHIND the reader, between two chunks.

        The rewrite hits a region the reader has already consumed, so the
        capture keeps the old bytes there while the file holds the new ones:
        a torn pair, and the byte witness sees it.
        """
        body = b"M" * 4096
        target = _stage(tmp_path, "multichunk", body)
        pre = os.stat(target)

        def mutate():
            with open(target, "r+b") as handle:
                handle.seek(0)                    # already read by chunk 1
                handle.write(b"N" * 512)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=body,
                                              chunk=512, monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()
        assert observed["size_bytes"] == 4096, "the size changed, not the bytes"
        assert observed["payload"] == body, \
            "NON-VACUITY: the capture kept the pre-race bytes it had read"
        assert target.read_bytes() != body, "NON-VACUITY: the disk did change"
        assert observed["stable"] is False, \
            "a mid-read mutation behind the reader went unnoticed"
        assert observed["identity"].sha256 is None

    def test_a_mutation_ahead_of_the_reader_yields_a_coherent_observation(
            self, tmp_path, monkeypatch):
        """The OTHER half of §11.5, and a limit worth stating precisely.

        FOUND BY THIS SUITE while writing it, and it corrected the author's
        assumption. When the rewrite lands on a region the reader has not
        reached yet, the reader goes on to read the NEW bytes there, so the
        capture ends up byte-identical to the file's final state. Re-reading
        the descriptor then agrees — because there is genuinely nothing left to
        disagree with — and with the mtime restored the metadata layer is blind
        too, so the observation is reported STABLE.

        That is the correct outcome, and it is why §9 insists on keeping
        property B apart from property D. Everything the snapshot actually
        CLAIMS still holds: the digest describes the bytes returned (A), those
        bytes are the bytes on disk, and a CAS write keyed on them targets
        exactly the state that is there (C). What does NOT hold is "no writer
        touched this file while I read it" — which is property D, and which
        this mechanism never promised. See
        ``docs/v69_M68D2_CROSS_PLATFORM_SOURCE_INTEGRITY.md``.
        """
        body = b"M" * 4096
        target = _stage(tmp_path, "ahead", body)
        pre = os.stat(target)

        def mutate():
            with open(target, "r+b") as handle:
                handle.seek(2048)                 # not yet reached
                handle.write(b"N" * 2048)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=body,
                                              chunk=512, monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()
        expected = b"M" * 2048 + b"N" * 2048
        assert observed["payload"] == expected, \
            "NON-VACUITY: the reader did not pick up the new bytes"
        assert target.read_bytes() == expected, "the disk and the capture differ"
        # A — the digest describes what was returned.
        assert observed["sha256"] == digest_bytes(expected)
        # C — and a write keyed on it addresses the state that is really there.
        receipt = cas_write_text(target, "replacement\n",
                                 expected_sha256=observed["sha256"])
        assert receipt.status is WriteStatus.APPLIED, \
            "a coherent observation of the current state was refused"
        assert target.read_bytes() == b"replacement\n"

    def test_a_mutation_during_a_derived_parser_read_is_caught(self, tmp_path,
                                                               monkeypatch):
        """§11.6 — stream mode, where ``recheck()`` is the only witness left.

        The digest pass and the parser's pass are two reads of ONE descriptor.
        A rewrite between them must make ``recheck()`` false, or the rendering
        a caller was shown and the digest that identifies it describe different
        bytes.
        """
        body = b"<pdf>" + b"D" * 1024
        target = _stage(tmp_path, "derived", body)
        pre = os.stat(target)
        with source_snapshot(target, capture_bytes=False) as snap:
            assert snap.stable is True, "the quiet acquisition was rejected"
            with snap.stream() as handle:
                assert handle.read() == body, "the parser saw other bytes"
            # The adversary lands AFTER the parser read and BEFORE the recheck.
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(b"<evil>" + b"E" * 1023)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))
            post = os.stat(target)
            assert (pre.st_size, pre.st_mtime_ns, pre.st_ino) == \
                (post.st_size, post.st_mtime_ns, post.st_ino), \
                "the metadata moved, so this is not the masked race"
            assert target.read_bytes() != body, "NON-VACUITY: nothing changed"
            assert snap.recheck() is False, \
                "a rewrite between the digest and the parser was not detected"
            assert identify_snapshot(snap).sha256 is None

    def test_an_atomic_replacement_is_handled_per_measured_capability(self,
                                                                      tmp_path):
        """§11.7 — staged where the platform permits it, asserted either way.

        No skip: both outcomes are a real assertion. Where the replacement is
        PERMITTED (POSIX), the held descriptor keeps the original inode, so the
        observation stays coherent AND stays stable — a rename cannot disturb
        bytes we are already holding. Where it is REFUSED (Windows denies
        ``os.replace`` over a held path with WinError 5), the refusal is the
        assertion, and this is reported CAPABILITY_UNAVAILABLE rather than
        counted as adversarial coverage (§12).
        """
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-human-wrote-this\n"
        target = _stage(tmp_path, "atomic", v1)
        other = _stage(tmp_path, "atomic_new", v2)
        witness = RaceWitness()
        with source_snapshot(target) as snap:
            witness.writer_attempted = True
            try:
                os.replace(other, target)
                witness.writer_succeeded = True
            except OSError as exc:
                witness.writer_errors.append(exc)
            if REPLACE_OVER_OPEN_PATH:
                assert witness.writer_succeeded, \
                    "the capability probe said replace is permitted, but it failed"
                assert target.read_bytes() == v2, "NON-VACUITY: no replacement"
                assert snap.payload() == v1, \
                    "the held descriptor followed the path instead of the inode"
                assert snap.stable is True, \
                    "a replacement the descriptor is immune to was called unstable"
                assert identify_snapshot(snap).sha256 == digest_bytes(v1)
            else:
                assert witness.writer_errors, \
                    "the probe said replace is refused, but it succeeded"
                assert snap.payload() == v1, "the observation was disturbed anyway"
                assert snap.stable is True

        # CROSS-PLATFORM CONSEQUENCE, asserted on EVERY platform: whatever the
        # mechanism did, a precondition from the held observation must not
        # overwrite content it never read.
        if REPLACE_OVER_OPEN_PATH:
            receipt = cas_write_text(target, "CCCC-derived-from-V1-CCC\n",
                                     expected_sha256=digest_bytes(v1))
            assert receipt.status is WriteStatus.REJECTED_STALE
            assert target.read_bytes() == v2, "the unread edit was destroyed"

    def test_an_unlink_and_recreate_is_handled_per_measured_capability(self,
                                                                       tmp_path):
        """§11.8 — the same shape for unlink, which Windows refuses with 32."""
        v1 = b"first-version--------\n"
        v2 = b"second-but-different-\n"
        target = _stage(tmp_path, "unlinked", v1)
        witness = RaceWitness()
        with source_snapshot(target) as snap:
            witness.writer_attempted = True
            try:
                target.unlink()
                target.write_bytes(v2)
                witness.writer_succeeded = True
            except OSError as exc:
                witness.writer_errors.append(exc)
            if UNLINK_OPEN_PATH:
                assert witness.writer_succeeded
                assert target.read_bytes() == v2, "NON-VACUITY: no recreate"
                assert snap.payload() == v1, \
                    "the observation followed the new directory entry"
                assert snap.sha256 == digest_bytes(v1)
                assert snap.stable is True, \
                    "the held inode was reported changed by a new inode appearing"
            else:
                assert witness.writer_errors, \
                    "the probe said unlink is refused, but it succeeded"
                assert snap.payload() == v1

    def test_the_witness_reads_the_held_inode_and_not_the_path(self, tmp_path,
                                                               monkeypatch):
        """§7/§10 — identity must never be re-derived from a mutable path.

        FOUND BY THE MUTATION CAMPAIGN: `B_reopen_the_path_for_final_identity`
        survived every other race here, because they all rewrite the SAME inode
        through a second handle — and there, reopening the path and reading the
        held descriptor return identical bytes, so the two are
        indistinguishable.

        An atomic replacement separates them exactly. The held descriptor keeps
        the original inode and must still describe it; a witness that reopened
        the path would see the REPLACEMENT, decide the observation was unstable
        and refuse a perfectly coherent read. Staged DURING the read, so the
        verification pass runs after the replacement has landed.
        """
        v1 = b"AAAA-version-one-AAAA\n"
        v2 = b"BBBB-human-wrote-this\n"
        target = _stage(tmp_path, "replaced_mid_read", v1)
        other = _stage(tmp_path, "replacement_source", v2)

        if not REPLACE_OVER_OPEN_PATH:
            # CAPABILITY_UNAVAILABLE — this platform refuses the mechanism, so
            # the adversary cannot be staged and is NOT counted as adversarial
            # coverage (§12). The refusal itself is the assertion.
            with source_snapshot(target) as snap:
                with pytest.raises(OSError):
                    os.replace(other, target)
                assert snap.payload() == v1
                assert snap.stable is True
            return

        observed, witness = _race_during_read(
            target, lambda: os.replace(other, target), was=v1,
            monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()
        assert target.read_bytes() == v2, (
            "NON-VACUITY: the path still points at the original file")
        assert observed["payload"] == v1, (
            "the observation followed the path instead of the held inode")
        assert observed["sha256"] == digest_bytes(v1)
        assert observed["stable"] is True, (
            "a replacement the held descriptor is immune to was called unstable")
        assert observed["recheck"] is True, (
            "the recheck re-derived identity from the path")
        assert observed["identity"].sha256 == digest_bytes(v1), (
            "a coherent observation of the held inode was denied an identity")

    def test_a_cas_write_from_an_obsolete_observation_loses(self, tmp_path):
        """§11.9 — the plain staleness path, with no race at all."""
        v1 = b"observed-this--------\n"
        target = _stage(tmp_path, "obsolete", v1)
        with source_snapshot(target) as snap:
            precondition = identify_snapshot(snap).sha256
        assert precondition == digest_bytes(v1), "the quiet read was refused"
        # Time passes; somebody else edits the file.
        target.write_bytes(b"somebody-else-wrote--\n")
        receipt = cas_write_text(target, "derived-from-v1------\n",
                                 expected_sha256=precondition)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.applied is False
        assert receipt.bytes_written == 0
        assert target.read_bytes() == b"somebody-else-wrote--\n"

    def test_an_unstable_observation_can_never_issue_a_precondition(self,
                                                                    tmp_path):
        """§11.10 — the property, stated directly and without a race."""
        target = _stage(tmp_path, "declared_unstable", b"x\n")
        with source_snapshot(target) as snap:
            snap.stable = False
            identity = identify_snapshot(snap)
            assert identity.sha256 is None
            assert identity.digest_covers == DIGEST_COVERS_NOTHING
            assert identity.complete is False
            receipt = cas_write_text(target, "y\n",
                                     expected_sha256=identity.sha256 or "")
            assert receipt.applied is False
            assert receipt.bytes_written == 0
            assert target.read_bytes() == b"x\n"


class TestPositiveControls:
    """§11 — a witness that rejects everything is not a witness."""

    @pytest.mark.parametrize("name", list(BYTE_FIXTURES))
    def test_a_quiet_file_is_stable_and_complete(self, tmp_path, name):
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with source_snapshot(target) as snap:
            assert snap.stable is True, "a file nobody touched was called unstable"
            assert snap.recheck() is True, "a second look rejected a quiet file"
            identity = identify_snapshot(snap)
            assert identity.complete is True
            assert identity.sha256 == digest_bytes(body)
            assert identity.digest_covers == DIGEST_COVERS_COMPLETE

    def test_a_quiet_file_in_stream_mode_is_stable_and_complete(self, tmp_path):
        body = bytes(range(256)) * 40
        target = _stage(tmp_path, "quiet_stream", body)
        with source_snapshot(target, capture_bytes=False) as snap:
            assert snap.stable is True
            with snap.stream() as handle:
                assert handle.read() == body
            assert snap.recheck() is True
            assert identify_snapshot(snap).sha256 == digest_bytes(body)

    def test_read_then_cas_succeeds_on_an_untouched_file(self, tmp_path):
        """The ordinary, overwhelmingly common path must still work."""
        target = _stage(tmp_path, "ordinary", b"before\n")
        with source_snapshot(target) as snap:
            precondition = identify_snapshot(snap).sha256
        receipt = cas_write_text(target, "after\n",
                                 expected_sha256=precondition)
        assert receipt.status is WriteStatus.APPLIED, \
            f"the normal read-then-write path broke: {receipt.status}"
        assert receipt.applied is True
        assert target.read_bytes() == b"after\n"

    def test_repeated_rechecks_of_a_quiet_file_never_drift(self, tmp_path):
        """The witness re-reads the descriptor; doing so must be idempotent."""
        body = b"stable" * 500
        target = _stage(tmp_path, "idempotent", body)
        with source_snapshot(target) as snap:
            for _ in range(5):
                assert snap.recheck() is True
            assert snap.payload() == body, "a recheck disturbed the capture"
            assert snap.sha256 == digest_bytes(body)

    def test_a_derived_read_of_a_quiet_file_is_not_refused(self, tmp_path):
        """Two passes over one descriptor, nothing racing: must succeed."""
        body = b"<pdf>" + bytes(range(256))
        target = _stage(tmp_path, "quiet_derived", body)
        with source_snapshot(target, capture_bytes=False) as snap:
            with snap.stream() as first:
                assert first.read() == body
            assert snap.recheck() is True
            with snap.stream() as second:
                assert second.read() == body, "the second stream was not rewound"
            assert snap.recheck() is True


class TestTheThreatModelIsStated:
    """§9 — four properties, kept apart, with the unreachable one declared.

    These tests assert the DOCUMENT and the CODE agree about what is claimed.
    An integrity guarantee nobody wrote down is a guarantee nobody can audit,
    and the specific failure this milestone exists to prevent is a stronger
    claim than the mechanism supports.
    """

    DOC = "docs/v69_M68D2_CROSS_PLATFORM_SOURCE_INTEGRITY.md"

    @pytest.fixture(scope="class")
    def doc(self):
        path = _REPO_ROOT / self.DOC
        assert path.exists(), f"{self.DOC} is missing"
        return path.read_text(encoding="utf-8")

    def test_property_a_content_digest_coherence(self, tmp_path):
        """A — the digest describes the bytes the observation returned."""
        for name, body in BYTE_FIXTURES.items():
            target = _stage(tmp_path, f"a_{name}", body)
            with source_snapshot(target) as snap:
                assert snap.sha256 == hashlib.sha256(snap.payload()).hexdigest(), \
                    f"{name}: the digest is not over the returned bytes"

    def test_property_b_observation_stability(self, tmp_path, monkeypatch):
        """B — a detectable overlapping mutation is not accepted as stable."""
        target = _stage(tmp_path, "b_prop", b"B" * 64)
        pre = os.stat(target)

        def mutate():
            with open(target, "r+b") as handle:
                handle.seek(0)
                handle.write(b"C" * 64)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=b"B" * 64,
                                              monkeypatch=monkeypatch)
        witness.assert_adversary_was_staged()
        assert observed["stable"] is False

    def test_property_c_cas_safety(self, tmp_path):
        """C — an older observation cannot overwrite changed content."""
        target = _stage(tmp_path, "c_prop", b"old\n")
        with source_snapshot(target) as snap:
            stale = identify_snapshot(snap).sha256
        target.write_bytes(b"new-and-longer\n")
        receipt = cas_write_text(target, "from-old\n", expected_sha256=stale)
        assert receipt.applied is False
        assert target.read_bytes() == b"new-and-longer\n"

    def test_property_d_transactional_snapshot_is_explicitly_not_claimed(self,
                                                                         doc):
        """D — and the honest statement that it is NOT provided (§9).

        If a later change claims an atomic point-in-time image of an arbitrary
        file under hostile concurrent writes, this fails. That claim cannot be
        made with the APIs in use: it needs cooperating writers, OS-level
        mandatory locking, or a filesystem snapshot authority.
        """
        lowered = doc.lower()
        assert "not a transactional snapshot" in lowered, \
            "the document does not state that property D is unavailable"
        for required in ("aba", "cooperating writer"):
            assert required in lowered, \
                f"the document does not name the {required!r} residual"
        for forbidden in ("atomic point-in-time snapshot of any file",
                          "race-free under all schedules",
                          "guarantees no concurrent writer"):
            assert forbidden not in lowered, \
                f"the document makes the unprovable claim {forbidden!r}"

    def test_the_aba_residual_is_real_and_recorded(self, tmp_path, monkeypatch):
        """HONESTY, demonstrated: the witness genuinely cannot see this one.

        A rewrite that restores the original bytes AND the original mtime is
        invisible to both layers. This test asserts the LIMITATION, so the
        documentation cannot drift away from the mechanism: if a future change
        did detect it, this fails and the residual gets rewritten rather than
        quietly surviving as folklore.
        """
        body = b"A" * 64
        target = _stage(tmp_path, "aba_residual", body)
        pre = os.stat(target)

        def mutate():
            for payload in (b"Z" * 64, body):
                with open(target, "r+b") as handle:
                    handle.seek(0)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.utime(target, ns=(pre.st_atime_ns, pre.st_mtime_ns))

        observed, witness = _race_during_read(target, mutate, was=body,
                                              monkeypatch=monkeypatch)
        assert witness.writer_attempted and witness.writer_succeeded
        assert not witness.writer_errors
        assert target.read_bytes() == body, "the ABA did not restore the bytes"
        # DETERMINISTIC, not sampled: the whole A->Z->A sequence completes
        # inside the first `os.read` hook, so every later chunk reads restored
        # bytes and the capture is NOT torn. That is the non-vacuity half —
        # there is genuinely nothing left for either witness to compare.
        assert observed["payload"] == body, \
            "the capture was torn, so this is not the ABA residual"
        assert observed["stable"] is True, (
            "the witness now detects a fully-restored ABA rewrite. That is an "
            "IMPROVEMENT, not a failure — but the residual recorded in "
            f"{self.DOC} is now wrong and must be rewritten before this test "
            "is changed to match.")


# ══ 3 · §21 — NO FAILURE BECOMES SUCCESS ════════════════════════════════════

class TestFailureInjection:
    """Every OSError on the observation path, and what it must NOT become.

    The bar: no injected failure may yield ``stable``, ``complete``, a write
    precondition, or an APPLIED receipt.
    """

    def test_an_open_failure_raises_rather_than_describing_nothing(self,
                                                                   tmp_path,
                                                                   monkeypatch):
        target = _stage(tmp_path, "open_fails", b"x\n")
        monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(
            OSError(13, "injected")))
        with pytest.raises(SourceSnapshotError):
            with source_snapshot(target):
                pass

    def test_a_missing_source_raises(self, tmp_path):
        with pytest.raises(SourceSnapshotError):
            with source_snapshot(tmp_path / "nope.txt"):
                pass

    def test_an_invalid_source_path_raises(self, tmp_path):
        target = _stage(tmp_path, "notadir", b"x\n")
        with pytest.raises(SourceSnapshotError):
            with source_snapshot(target / "through-a-file"):
                pass

    def test_a_read_failure_propagates(self, tmp_path, monkeypatch):
        """An OSError mid-read must not produce a short "complete" observation."""
        target = _stage(tmp_path, "read_fails", b"x" * 4096)
        real_read = os.read
        calls: "list[int]" = []

        def failing_read(fd, size):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(5, "injected EIO")
            return real_read(fd, min(size, 16))

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 16)
        monkeypatch.setattr(os, "read", failing_read)
        with pytest.raises(OSError):
            with source_snapshot(target):
                pass
        assert len(calls) >= 2, "NON-VACUITY: the failure was never reached"

    def test_a_failure_in_the_verification_pass_is_not_silently_stable(
            self, tmp_path, monkeypatch):
        """The witness itself failing must never read as "nothing changed"."""
        target = _stage(tmp_path, "verify_fails", b"v" * 64)
        real_read = os.read
        state = {"captured": False}

        def failing_read(fd, size):
            data = real_read(fd, size)
            if not data and not state["captured"]:
                state["captured"] = True          # capture pass just finished
                return data
            if state["captured"] and data:
                raise OSError(5, "injected EIO during verification")
            return data

        monkeypatch.setattr(si, "_DIGEST_CHUNK", 16)
        monkeypatch.setattr(os, "read", failing_read)
        with pytest.raises(OSError):
            with source_snapshot(target):
                pass

    def test_a_short_read_is_not_a_whole_observation(self, tmp_path, monkeypatch):
        target = _stage(tmp_path, "short", b"X" * 4096)
        real_read = os.read
        calls: "list[int]" = []

        def short_read(fd, size):
            calls.append(1)
            if len(calls) == 1:
                return real_read(fd, 16)
            return b""

        monkeypatch.setattr(os, "read", short_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            assert snap.size_bytes == 16, "NON-VACUITY: the read was not short"
            assert snap.stable is False
            assert identify_snapshot(snap).sha256 is None

    def test_an_fstat_failure_is_not_stability(self, tmp_path, monkeypatch):
        target = _stage(tmp_path, "fstat_fails", b"f\n")
        with source_snapshot(target) as snap:
            assert snap.stable is True
            monkeypatch.setattr(os, "fstat", lambda fd: (_ for _ in ()).throw(
                OSError(9, "injected EBADF")))
            assert snap.recheck() is False, \
                "a failed fstat was read as \"nothing changed\""

    def test_a_descriptor_duplication_failure_is_not_a_served_stream(
            self, tmp_path, monkeypatch):
        target = _stage(tmp_path, "dup_fails", b"d" * 32)
        with source_snapshot(target, capture_bytes=False) as snap:
            monkeypatch.setattr(os, "dup", lambda fd: (_ for _ in ()).throw(
                OSError(24, "injected EMFILE")))
            with pytest.raises(OSError):
                snap.stream()

    def test_the_binary_flag_being_unavailable_does_not_crash(self, tmp_path):
        """§21 — "binary-mode flag unavailable" is the POSIX case, and is fine."""
        target = _stage(tmp_path, "noflag", b"n\n")
        with windows_text_runtime(binary_available=False):
            with source_snapshot(target) as snap:
                assert snap.payload() == b"n\n"
                assert snap.stable is True

    def test_the_descriptor_is_closed_on_every_exception(self, tmp_path,
                                                         monkeypatch):
        """§21 — resource cleanup on every path, not just the happy one."""
        target = _stage(tmp_path, "cleanup", b"c" * 64)
        closed: "list[int]" = []
        real_close, real_read = os.close, os.read
        monkeypatch.setattr(os, "close",
                            lambda fd: (closed.append(fd), real_close(fd))[1])

        def boom(fd, size):
            raise OSError(5, "injected")

        monkeypatch.setattr(os, "read", boom)
        with pytest.raises(OSError):
            with source_snapshot(target):
                pass
        assert closed, "the descriptor leaked when the read failed"

        closed.clear()
        monkeypatch.setattr(os, "read", real_read)
        with pytest.raises(RuntimeError):
            with source_snapshot(target):
                raise RuntimeError("caller blew up")
        assert closed, "the descriptor leaked when the CALLER raised"

    def test_a_close_failure_does_not_mask_the_observation(self, tmp_path,
                                                           monkeypatch):
        """``os.close`` raising must not turn a good observation into an error."""
        target = _stage(tmp_path, "close_fails", b"q\n")
        monkeypatch.setattr(os, "close", lambda fd: (_ for _ in ()).throw(
            OSError(9, "injected EBADF on close")))
        with source_snapshot(target) as snap:
            assert snap.payload() == b"q\n"

    def test_a_stale_expected_hash_never_applies(self, tmp_path):
        target = _stage(tmp_path, "stale_cas", b"real\n")
        receipt = cas_write_text(target, "nope\n", expected_sha256="0" * 64)
        assert receipt.applied is False
        assert receipt.bytes_written == 0
        assert target.read_bytes() == b"real\n"

    def test_a_missing_digest_never_matches_a_precondition(self, tmp_path):
        """``digest_file`` returns None for the unreadable; None must not match."""
        assert si.digest_file(tmp_path / "absent") is None
        target = _stage(tmp_path, "nomatch", b"here\n")
        receipt = cas_write_text(target, "x\n", expected_sha256="")
        assert receipt.applied is False


# ══ 4 · §20 — ABSENT CONTROLS ═══════════════════════════════════════════════

def _production_code() -> str:
    """``source_integrity`` with comments and docstrings removed.

    A structural detector must not match the very prose that explains what it
    forbids — every rule below is NAMED in a comment a few lines from the code
    that implements it.
    """
    src = (_JARVIS_ROOT / "core/source_integrity.py").read_text(encoding="utf-8")
    out: "list[str]" = []
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
            out.append(tok.string)
        prev = tok.type
    return " ".join(out)


class TestAbsentControls:
    """Tests that fail if a later developer undoes a specific repair (§20)."""

    @pytest.fixture(scope="class")
    def code(self):
        return _production_code()

    def test_the_source_open_still_consults_the_binary_flag(self, code):
        assert '"O_BINARY"' in code or "_O_BINARY_FLAG" in code, \
            "the source open no longer asks the platform for binary mode"
        assert "def _source_open_flags" in code.replace(" (", "("), \
            "the flag derivation production and the tests share is gone"
        assert "os . open ( str ( resolved ) , _source_open_flags ( ) )" in code, \
            "source_snapshot no longer opens through _source_open_flags()"

    def test_the_binary_flag_is_not_hardcoded_to_zero(self, code):
        """A "repair" that always yields 0 would pass every POSIX test."""
        for dead in ("_O_BINARY_FLAG = \"\"", "getattr ( os , _O_BINARY_FLAG , 0 ) * 0"):
            assert dead not in code
        with windows_text_runtime(binary_available=True):
            assert si._source_open_flags() & WIN_O_BINARY, \
                "the derivation ignores a platform that offers binary mode"

    def test_the_digest_never_normalises_newlines(self, code):
        for forbidden in ('replace ( b"\\r\\n" , b"\\n" )',
                          'replace ( "\\r\\n" , "\\n" )',
                          "splitlines ( ) )",
                          "newline = None"):
            assert forbidden not in code, \
                f"source_integrity normalises newlines: {forbidden}"

    def test_the_source_digest_is_not_computed_from_decoded_text(self, code):
        assert "sha256 ( payload . decode" not in code
        assert "digest . update ( payload . decode" not in code
        assert "digest . update ( payload )" in code, \
            "the capture digest is no longer over the captured bytes"

    def test_the_stability_witness_is_not_metadata_only(self, code):
        """THE Group B absent control."""
        assert "verified_sha , verified_total = _descriptor_content ( fd )" in code, \
            "the byte-level stability witness was removed from acquisition"
        assert "and verified_sha == digest . hexdigest ( )" in code, \
            "the acquisition no longer compares the re-read bytes"
        assert "and verified_total == total" in code
        assert "or verified_sha != self . sha256" in code, \
            "recheck() went back to being metadata-only"
        assert "or verified_total != self . size_bytes" in code

    def test_the_metadata_witness_is_also_still_there(self, code):
        """Neither layer may be dropped: ABA needs the mtime, §9.

        `test_an_aba_rewrite_that_restores_the_bytes_is_still_reported_unstable`
        in the H02 suite is the behavioural half of this.
        """
        for field_ in ("st_ino", "st_dev", "st_mtime_ns", "st_size"):
            assert field_ in code, f"the metadata witness lost {field_}"
        assert "before . st_mtime_ns == after . st_mtime_ns" in code

    def test_the_witness_reads_the_held_descriptor_and_not_the_path(self, code):
        """§7/§10 — identity must never come from reopening a mutable path."""
        assert "os . lseek ( fd , 0 , os . SEEK_SET )" in code, \
            "the verification pass no longer rewinds the HELD descriptor"
        body = code.split("def _descriptor_content")[1].split("def ")[0]
        for reopen in ("open (", "os . open ("):
            assert reopen not in body, (
                f"the verification pass reopens a path: {reopen}")
        # And `source_snapshot` itself opens the path ONCE and never again. The
        # mutation campaign showed the reopen can be moved up HERE, where a
        # scan of the helper alone would not see it.
        head = code.split("def source_snapshot")[1].split("def ")[0]
        assert head.count("os . open (") == 1, (
            "source_snapshot opens the path more than once")
        assert "open ( str ( resolved ) ," not in head.replace(
            "os . open ( str ( resolved ) ,", ""), (
            "source_snapshot reopens the resolved path for identity")

    def test_the_verification_pass_cannot_be_switched_off(self, code):
        """§10 — no new optimistic success branch."""
        head = code.split("def source_snapshot")[1].split("def ")[0]
        for flag in ("verify =", "skip_verify", "fast =", "trust_metadata"):
            assert flag not in head, \
                f"source_snapshot grew an opt-out: {flag}"

    def test_an_unstable_observation_still_cannot_state_a_precondition(self, code):
        assert "snapshot . sha256 if snapshot . stable else None" in code, \
            "identify_snapshot hands out a digest for an unstable observation"

    def test_the_snapshot_opens_the_path_exactly_once(self, code):
        head = code.split("def source_snapshot")[1].split("def ")[0]
        assert head.count("os . open (") == 1, \
            "the snapshot opens the path more than once"
        assert head.count("os . fstat ( fd )") >= 2, \
            "one fstat cannot bracket an observation"

    def test_no_writer_exception_is_swallowed_in_this_suite(self):
        """§12 — a dead adversary must not become an invisible pass.

        Every ``except BaseException`` in this file must APPEND to a witness
        list, and every race must then assert that list empty.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        swallowing = [
            ast.unparse(h)[:70] for h in handlers
            if not any("append" in ast.unparse(s) or "raise" in ast.unparse(s)
                       or "pytest" in ast.unparse(s) for s in h.body)]
        assert not swallowing, f"an exception handler swallows: {swallowing}"
        assert handlers, "NON-VACUITY: the scan found no handlers at all"
        # The STRIPPED view, because this test names every literal it looks
        # for: scanned as raw text it matches its own search patterns and
        # passes on a file where the witness has been gutted. MEASURED — the
        # mutation campaign walked straight through the first version of this.
        code = _no_strings(Path(__file__))
        assert code.count("writer_errors . append") >= 2, \
            "the witness no longer collects the writer's exception"
        assert "assert not self . writer_errors" in code, \
            "the witness no longer asserts the adversary was not refused"
        assert "assert self . mutation_observed" in code, \
            "the witness no longer asserts the bytes on disk actually changed"
        assert "assert self . writer_succeeded" in code, \
            "the witness no longer asserts the writer completed"
        assert "assert self . writer_attempted" in code, \
            "the witness no longer asserts the writer was reached"

    def test_no_race_in_this_suite_passes_without_a_witness(self):
        """Every test that stages a mutation must consult a witness."""
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        staged, witnessed = [], []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if not node.name.startswith("test_"):
                continue
            if "_race_during_read" in body or 'open(target, "r+b")' in body:
                staged.append(node.name)
                if ("assert_adversary_was_staged" in body
                        or "witness.writer_succeeded" in body
                        or "NON-VACUITY" in body):
                    witnessed.append(node.name)
        assert staged, "NON-VACUITY: no staged mutation was found at all"
        assert set(staged) == set(witnessed), \
            f"these races assert nothing about their adversary: " \
            f"{sorted(set(staged) - set(witnessed))}"


# ══ 5 · §15 — SKIP GOVERNANCE THAT CAN ACTUALLY BE SATISFIED ════════════════

#: Invariants that must EXECUTE on every platform, named individually rather
#: than defended by a ratio. Each entry is a node id substring that has to be
#: present in the suite's collection, so a platform that quietly stopped
#: collecting one — the failure mode a pass/skip ratio cannot see at all — is a
#: red test rather than a smaller number.
MANDATORY_CROSS_PLATFORM_INVARIANTS: "dict[str, tuple[str, ...]]" = {
    "tests/test_cross_platform_source_integrity_m68d2.py": (
        # byte identity
        "TestBinaryByteIdentity::test_every_fixture_survives_a_text_translating_runtime",
        "TestBinaryByteIdentity::test_stream_mode_digests_the_bytes_on_disk",
        "TestBinaryByteIdentity::test_the_ctrl_z_truncation_is_closed",
        "TestBinaryByteIdentity::test_a_crlf_source_is_never_rejected_for_being_crlf",
        # in-place rewrite stability
        "TestByteLevelStability::test_the_measured_windows_race_is_reported_unstable",
        "TestByteLevelStability::test_a_mutation_during_multi_chunk_reading_is_caught",
        "TestByteLevelStability::test_a_mutation_during_a_derived_parser_read_is_caught",
        # CAS safety
        "TestByteLevelStability::test_that_race_cannot_authorise_a_write",
        "TestByteLevelStability::test_a_cas_write_from_an_obsolete_observation_loses",
        "TestByteLevelStability::test_an_unstable_observation_can_never_issue_a_precondition",
        # positive controls, so a witness that refuses everything is caught
        "TestPositiveControls::test_read_then_cas_succeeds_on_an_untouched_file",
    ),
    "tests/test_trust_boundary_m68d_h02_source_identity.py": (
        "TestSnapshotRaces::test_an_in_place_rewrite_during_the_observation_is_reported_unstable",
        "TestSnapshotRaces::test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit",
        "TestSnapshotRaces::test_an_aba_rewrite_that_restores_the_bytes_is_still_reported_unstable",
        "TestSnapshotCoherence::test_the_digest_is_over_exactly_the_captured_bytes",
    ),
    "tests/test_trust_boundary_m68d_h05_portability.py": (
        # Git-mode authority and the NEWLINE_POLICY gate, both of which the
        # Windows runner has to execute rather than declare portable.
        "TestGitModeIsTheAuthority",
        "TestNewlinePinning::test_each_named_byte_sealed_artifact_is_newline_pinned",
        "TestNewlinePinning::test_the_newline_policy_check_is_dispatched_and_owns_its_category",
        "TestNewlinePinning::test_the_newline_policy_requires_the_policy_file_to_be_tracked",
    ),
}

#: The ONLY two reasons a case in a Windows suite may be skipped, each of which
#: names an unavailable primitive a reader can go and check (§15):
#:
#: 1. a MEASURED filesystem capability the platform refuses, whose reason
#:    carries the probe marker AND the kernel verdict that produced it;
#: 2. an optional dependency that is not installed, named by `importorskip`.
#:
#: Anything else is skip-washing, whatever the numbers say. Measured on this
#: host: the H02 suite's two skips are category 2 (`docx`), because every
#: filesystem capability is available here — so the contract has to admit both
#: or it would be red on a POSIX runner for the wrong reason.
CAPABILITY_SKIP_MARKER = "POSIX_CAPABILITY_TEST:"
CAPABILITY_SKIP_EVIDENCE = "Measured:"
DEPENDENCY_SKIP_MARKER = "could not import"


class TestSkipGovernance:
    """§14–§15 — the contract that replaced an impossible ratio."""

    def test_the_old_arithmetic_was_unsatisfiable_at_the_measured_collection(self):
        """§14, stated as arithmetic and checked, not asserted in prose.

        55 collected, 9 skipped: the assertion demanded 72 passes from a run
        that could produce at most 46. This is the non-vacuity proof that the
        contract had to change — had the old form been merely tight, relaxing
        it would have been a weakening of the standard instead of a repair.
        """
        collected, skipped, failed = 55, 9, 5      # CI run 37994281455
        assert collected - skipped - failed == 41, "not the measured result"
        assert not ratio_is_satisfiable(collected, 8, skipped)
        assert 8 * skipped == 72 > collected - skipped == 46, \
            "the impossibility has to be arithmetic, not opinion"

    def test_the_replacement_budget_is_satisfiable_everywhere(self):
        """A bound that can exceed the collection is not a bound (§15)."""
        for collected in range(0, 200):
            budget = skip_budget(collected)
            assert 0 <= budget <= max(collected, 0), \
                f"the budget {budget} is outside 0..{collected}"
            if collected:
                assert budget < collected or collected == 1, \
                    "the budget would permit skipping the entire suite"

    def test_the_budget_still_bites(self):
        """And it must not be so generous that hollowing a suite out passes."""
        assert skip_budget(100) == 25
        assert skip_budget(55) == 13
        assert skip_budget(55) < 55 // 2, "half a suite behind gates would pass"

    @pytest.mark.parametrize("rel", sorted(MANDATORY_CROSS_PLATFORM_INVARIANTS))
    def test_every_mandatory_invariant_is_collected_on_this_platform(self, rel):
        """The inventory, checked against real pytest collection (§15).

        Collection rather than a run, because this suite is one of the targets:
        running itself in a subprocess would reach this test again and recurse.
        """
        node_ids = collected_node_ids(rel)
        assert node_ids, f"{rel} collected nothing at all"
        joined = "\n".join(node_ids)
        missing = [name for name in MANDATORY_CROSS_PLATFORM_INVARIANTS[rel]
                   if name not in joined]
        assert not missing, (
            f"{rel} does not collect these mandatory cross-platform "
            f"invariants on this platform: {missing}")

    def test_the_collection_detector_would_notice_an_absence(self):
        """NON-VACUITY: the check above has to be able to fail."""
        node_ids = collected_node_ids(
            "tests/test_cross_platform_source_integrity_m68d2.py")
        joined = "\n".join(node_ids)
        assert "test_a_name_no_test_in_this_file_has" not in joined, \
            "the collection scan matches names that do not exist"
        assert "TestBinaryByteIdentity" in joined, \
            "the collection scan cannot see a test that IS there"

    def test_every_h02_skip_traces_to_a_measured_primitive(self):
        """§15 — a skip reason has to name the capability and the verdict."""
        result = census("tests/test_trust_boundary_m68d_h02_source_identity.py")
        assert result.failed == 0 and result.errors == 0, \
            f"the H02 suite is not green here: {result.summary}"
        assert result.collected > 0, f"nothing collected: {result.summary}"
        for reason, count in result.skip_reasons.items():
            if DEPENDENCY_SKIP_MARKER in reason:
                continue                      # category 2: a named module
            assert CAPABILITY_SKIP_MARKER in reason, (
                f"{count} case(s) skip for an untraceable reason: {reason!r}")
            assert CAPABILITY_SKIP_EVIDENCE in reason, (
                f"{count} case(s) skip without citing the measurement that "
                f"produced the verdict: {reason!r}")
        assert result.skipped == sum(result.skip_reasons.values()), (
            "some skips reported no reason at all: "
            f"{result.skipped} skipped, {sum(result.skip_reasons.values())} "
            f"explained — {result.summary}")

    @staticmethod
    def _ratio_assertions_in(source: str) -> "list[str]":
        """Every ``passed >= N * skipped``-shaped comparison in *source*.

        ONE implementation, called by both the scan and its non-vacuity proof.
        Two copies would mean blinding the real one leaves the proof green —
        which is exactly what the mutation campaign demonstrated.
        """
        found: "list[str]" = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            if "skip" not in rendered or "pass" not in rendered:
                continue
            if "skip_budget" in rendered:
                continue              # bounded by the collection: allowed
            if any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult)
                   for sub in ast.walk(node)):
                found.append(rendered[:70])
        return found

    def test_no_windows_suite_asserts_an_unbounded_pass_skip_ratio(self):
        """§20 — "replaces a precise skip inventory with a broad ratio".

        STRUCTURAL, and it has to be: the forbidden assertion
        ``passed >= 8 * skipped`` PASSES on a host with few capability skips,
        so only a scan can catch it being reintroduced. A skip budget must be
        bounded by the COLLECTION, never by a multiple of the skip count — the
        latter can demand more passes than there are cases, which is precisely
        how the real Windows runner was blocked (§14).
        """
        offenders: "list[str]" = []
        for rel in (*MANDATORY_CROSS_PLATFORM_INVARIANTS,
                    "tests/test_windows_portability_closure_m68d1.py"):
            path = _JARVIS_ROOT / rel
            if not path.exists():
                continue
            offenders += [f"{rel}: {hit}" for hit in
                          self._ratio_assertions_in(
                              path.read_text(encoding="utf-8"))]
        assert not offenders, (
            "a pass/skip RATIO is back; the budget must be bounded by the "
            f"collection instead: {offenders}")

    def test_the_ratio_detector_sees_a_real_offender(self):
        """NON-VACUITY for the scan above, through the SAME detector."""
        assert self._ratio_assertions_in(
            "assert passed >= 8 * skipped\n"), \
            "the detector cannot see the exact assertion that blocked CI"
        assert self._ratio_assertions_in(
            "assert result.passed >= 8 * result.skipped\n"), \
            "the detector cannot see the attribute form of that assertion"
        assert not self._ratio_assertions_in(
            "assert skipped <= skip_budget(collected)\n"), \
            "the detector flags the bounded form it is supposed to permit"
        assert not self._ratio_assertions_in("assert passed > 0\n"), \
            "the detector flags a plain non-vacuity assertion"

    def test_the_mandatory_inventory_covers_every_required_category(self):
        """§15 — the inventory is the contract, so it cannot quietly shrink.

        A ratio could be satisfied by running any 46 of 55 cases. An inventory
        can only be satisfied by running THESE. That only holds while the
        inventory still names every category the milestone owes, so the
        categories are asserted here rather than left to a reviewer's memory.
        """
        mine = MANDATORY_CROSS_PLATFORM_INVARIANTS[
            "tests/test_cross_platform_source_integrity_m68d2.py"]
        h02 = MANDATORY_CROSS_PLATFORM_INVARIANTS[
            "tests/test_trust_boundary_m68d_h02_source_identity.py"]
        joined = " ".join(mine)
        for category, needle in (
                ("byte identity", "survives_a_text_translating_runtime"),
                ("CRLF non-rejection", "never_rejected_for_being_crlf"),
                ("Ctrl-Z truncation", "ctrl_z_truncation_is_closed"),
                ("stream-mode identity", "stream_mode_digests_the_bytes_on_disk"),
                ("same-size race", "measured_windows_race_is_reported_unstable"),
                ("derived-reader race", "derived_parser_read_is_caught"),
                ("CAS safety", "cannot_authorise_a_write"),
                ("CAS staleness", "obsolete_observation_loses"),
                ("positive control", "succeeds_on_an_untouched_file")):
            assert needle in joined, \
                f"the mandatory inventory lost its {category} invariant"
        assert len(mine) >= 11, \
            f"the M68D.2 mandatory inventory shrank to {len(mine)} entries"
        assert len(h02) >= 4, \
            f"the H02 mandatory inventory shrank to {len(h02)} entries"
        assert any("aba" in name.lower() for name in h02), \
            "the ABA invariant left the mandatory inventory"
        h05 = MANDATORY_CROSS_PLATFORM_INVARIANTS[
            "tests/test_trust_boundary_m68d_h05_portability.py"]
        assert any("GitMode" in name for name in h05), \
            "the Git-mode authority invariant left the inventory"
        assert any("newline_policy" in name for name in h05), \
            "the NEWLINE_POLICY gate left the inventory"
        assert set(MANDATORY_CROSS_PLATFORM_INVARIANTS) <= set(
            rel for rel in MANDATORY_CROSS_PLATFORM_INVARIANTS
            if (_JARVIS_ROOT / rel).exists()), \
            "the inventory names a suite that does not exist"

    def test_this_suite_itself_is_ungated(self):
        """Structural: no test here may be behind a capability gate at all."""
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        gated: "list[str]" = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            for deco in node.decorator_list:
                rendered = ast.unparse(deco)
                if "skip" in rendered or "needs_" in rendered:
                    gated.append(f"{node.name}: {rendered[:50]}")
        assert not gated, f"this suite gates tests behind a skip: {gated}"
        # NON-VACUITY: the scan DOES see the gates in the H02 suite.
        h02 = ast.parse((_JARVIS_ROOT / "tests"
                         / "test_trust_boundary_m68d_h02_source_identity.py")
                        .read_text(encoding="utf-8"))
        found = [n.name for n in ast.walk(h02)
                 if isinstance(n, ast.FunctionDef)
                 and any("needs_" in ast.unparse(d) for d in n.decorator_list)]
        assert found, "the gate scan cannot see a real capability gate"

    def test_no_module_level_skip_hides_this_suite(self):
        """A module-level skip would hide every invariant above it.

        Scanned WITHOUT string literals: this file names the token it forbids,
        so a plain substring search matches its own search pattern and fails on
        a clean file (measured — that is exactly what it did first).
        """
        assert "pytestmark" not in _no_strings(Path(__file__)), \
            "this suite declares a module-level mark"
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                assert "skip" not in ast.unparse(node.value.func).lower(), \
                    f"module-level skip: {ast.unparse(node)[:60]}"
        # NON-VACUITY: the stripped view still SEES a real module-level mark.
        assert "pytestmark" in _no_strings_of(
            "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n"), \
            "the stripped view cannot see a real module-level mark"

    def test_this_suite_decides_nothing_from_a_platform_name(self):
        """Measured capability only — never a label (M68D.1 §13)."""
        joined = _no_strings(Path(__file__))
        for inferred in ("sys . platform", "platform . system", "os . name"):
            assert inferred not in joined, \
                f"this suite branches on {inferred} instead of a capability"
        # NON-VACUITY: the scan sees a real platform-name branch.
        assert "sys . platform" in _no_strings_of(
            "import sys\nif sys.platform == 'win32':\n    pass\n"), \
            "the platform-name scan is blind"


class TestTheGovernanceControlsThemselvesAreIntact:
    """§20 — the controls that live in TEST code need absent-controls too.

    FOUND BY THE MUTATION CAMPAIGN. Seven mutations survived the first run of
    this suite not because production was unguarded, but because the guards in
    test code and in the shared census helper had nothing above them. A control
    nobody checks is as decorative as a deleted one — and these are the ones
    that decide whether a RED Windows runner can be turned green by editing an
    assertion.

    Every scan here uses the STRINGS-DROPPED view, because this class names
    each literal it looks for. Read as raw text it matches its own search
    patterns and passes on a file whose controls have been gutted; that is
    precisely how the first version of these checks was walked through.
    """

    CLOSURE = "tests/test_windows_portability_closure_m68d1.py"

    @pytest.fixture(scope="class")
    def closure(self):
        return _no_strings(_JARVIS_ROOT / self.CLOSURE)

    def test_the_closure_suite_still_refuses_failures(self, closure):
        """``failed == 0`` is what the old ratio was really reaching for."""
        assert "assert result . failed == 0 and result . errors == 0" in closure, (
            "the H02 contract no longer refuses failures, so a failing test "
            "could be read as a skip again")

    def test_the_closure_suite_accounts_for_every_collected_case(self, closure):
        assert ("assert result . passed + result . skipped == result . collected"
                in closure), (
            "the H02 contract no longer accounts for every collected case")

    def test_the_closure_suite_bounds_skips_by_the_collection(self, closure):
        assert "budget = skip_budget ( result . collected )" in closure, (
            "the skip budget is no longer derived from the collection")
        assert "assert result . skipped <= budget" in closure, (
            "the skip budget is no longer enforced")
        assert "assert budget < result . collected" in closure, (
            "the budget is no longer required to be a real bound")

    def test_the_closure_suite_proves_the_old_ratio_impossible(self, closure):
        assert "ratio_is_satisfiable" in closure, (
            "the non-vacuity proof that the old arithmetic was unsatisfiable "
            "is gone, so relaxing it would look like a free choice")

    def test_the_census_accounts_for_failures_and_errors(self):
        """``collected`` must cover every outcome, not just the happy ones.

        A ``collected`` that silently omits failures would make the accounting
        assertion above pass on a run that had them.
        """
        sample = SuiteCensus(passed=10, failed=2, skipped=3, errors=1,
                             xfailed=1, xpassed=1)
        assert sample.collected == 18, (
            f"collected={sample.collected} omits outcomes; passed, failed, "
            "skipped, errors, xfailed and xpassed all have to count")
        assert sample.ran == 15, "ran must be the collection minus the skips"
        assert SuiteCensus().collected == 0

    def test_the_census_parses_a_real_skip_line(self):
        """The reason contract is vacuous if the parser matches nothing.

        MEASURED: the first form of this pattern required ``": "`` immediately
        after the path, and so never matched pytest's ``<path>:<lineno>:``
        output at all. Every suite then reported zero skip reasons, and the
        reason contract checked nothing whatsoever.
        """
        line = ("SKIPPED [2] tests/test_x.py:935: POSIX_CAPABILITY_TEST: "
                "nope. Measured: REFUSED")
        match = _SKIP_LINE.match(line)
        assert match, "the skip-line parser cannot read pytest's own output"
        assert match.group(1) == "2"
        assert match.group(2) == "tests/test_x.py"
        assert match.group(3) == "935"
        assert match.group(4).startswith(CAPABILITY_SKIP_MARKER)
        assert CAPABILITY_SKIP_EVIDENCE in match.group(4)

    def test_every_capability_skip_reason_carries_its_evidence(self):
        """§15 — STRUCTURAL, because a reason only shows when its gate fires.

        On this host every filesystem capability is available, so none of these
        reasons appears in a run: the behavioural check in
        :class:`TestSkipGovernance` cannot see them being gutted. They are
        asserted directly instead, which is where a Windows runner's skips come
        from.
        """
        names = [n for n in dir(capability_reasons) if n.startswith("WHY_NO_")]
        assert len(names) >= 4, f"the capability reasons vanished: {names}"
        for name in names:
            reason = getattr(capability_reasons, name)
            assert CAPABILITY_SKIP_MARKER in reason, (
                f"{name} no longer marks itself a measured capability skip")
            assert CAPABILITY_SKIP_EVIDENCE in reason, (
                f"{name} no longer cites the measurement that produced it")
            assert len(reason) > 120, (
                f"{name} was reduced to a label: {reason!r}")

    def test_the_append_defect_pin_is_still_enforced(self):
        """§16 — the pin itself, so the deferral cannot lapse unnoticed."""
        code = _no_strings(Path(__file__))
        assert "assert appends ," in code, (
            "the deferred append defect is no longer pinned, so repairing it "
            "without authorisation would pass silently")
        assert "assert receipt . bytes_written == len (" in code, (
            "the whole-file write no longer checks its own byte accounting")


# ══ 7 · §16 — THE DEFERRED APPEND DEFECT STAYS OPEN ═════════════════════════

class TestTheAppendDefectIsStillOpen:
    """§16 — the read-side repair must not be mistaken for closing this.

    ``cas_write_text(..., mode="a")`` stages an append through a TEXT handle, so
    on Windows ``"\\n"`` becomes ``"\\r\\n"`` and the ``bytes_written`` receipt
    under-reports what landed; a caller's ``"\\r\\n"`` can become ``"\\r\\r\\n"``.
    Discovered during M68D.1 and DEFERRED: fixing it reaches into four places
    and needs its own authorisation.

    These tests exist so the defect cannot be silently reclassified as fixed
    just because the READ path now opens binary descriptors. They pin the
    defect's SHAPE, not a passing behaviour.
    """

    def test_the_append_branch_is_still_a_text_mode_write(self):
        """STRUCTURAL — the known defect is still exactly where it was."""
        source = (_JARVIS_ROOT / "core/source_integrity.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        appends: "list[int]" = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", ""))
            if name not in ("open", "fdopen"):
                continue
            mode = next((a.value for a in node.args
                         if isinstance(a, ast.Constant)
                         and isinstance(a.value, str)
                         and set(a.value) <= set("rwxab+t") and a.value), None)
            if mode and "a" in mode and "b" not in mode \
                    and not any(kw.arg == "newline" for kw in node.keywords):
                appends.append(node.lineno)
        assert appends, (
            "the text-mode append is gone. If it was REPAIRED, that is out of "
            "M68D.2's scope (§16) and needs its own authorisation plus the "
            "byte-accounting tests this docstring describes; update this test "
            "deliberately rather than deleting it")

    def test_the_read_side_repair_did_not_touch_the_write_side(self):
        """The two paths must stay independent, so neither hides the other."""
        source = (_JARVIS_ROOT / "core/source_integrity.py").read_text(
            encoding="utf-8")
        read_side = source.split("def cas_write_text")[0]
        write_side = source.split("def cas_write_text")[1]
        assert "_source_open_flags" in read_side, \
            "the binary-mode repair left the read side"
        assert "_source_open_flags" not in write_side, (
            "the write path now shares the read path's flag derivation — §16 "
            "requires STOPPING and reporting that dependency, not broadening "
            "scope into the append defect")

    def test_a_whole_file_cas_write_is_byte_exact_either_way(self, tmp_path):
        """The IN-SCOPE half: ``mode="w"`` was already portable and stays so."""
        target = _stage(tmp_path, "whole", b"old\n")
        mixed = "a\r\nb\nc\r\n"
        receipt = cas_write_text(target, mixed,
                                 expected_sha256=digest_bytes(b"old\n"))
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_bytes() == mixed.encode(), \
            "the whole-file write translated the caller's line endings"
        assert receipt.bytes_written == len(mixed.encode()), \
            "the receipt does not describe the bytes that landed"
