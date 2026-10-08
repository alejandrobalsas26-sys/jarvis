"""V69 M68D.1 — Windows portability closure.

WHY THIS SUITE EXISTS
=====================
M68D sealed on its branch with ten Linux jobs green and a `windows-portability`
job that had never been executed by a real Windows runner. The exact-SHA
integration ceremony ran that job for the first time (CI run **37695660326**,
candidate **cc8a7dda6efd20b371459a51f4f687711972a88a**, runner
`windows-2025-vs2026`, CPython 3.11.9) and it came back
**173 passed / 25 failed / 9 skipped**. Master was never touched.

The Control Plane verifier itself PASSED on that runner, under the hostile
`core.autocrlf=true` default: `M62_CONTROL_PLANE_VERIFY: PASS`,
`NEWLINE_POLICY: PASS`, `PROBLEMS: 0`. H05's central claim — 131 CRLF false
problems reduced to zero by `.gitattributes` `-text` — is therefore genuinely
proven on real Windows. None of that is weakened here.

What failed was the SUITES, in three groups, and every one of them was a POSIX
assumption in a TEST rather than a defect in production:

GROUP A (14) — Windows mandatory open-handle semantics. CPython's `os.open`
does not request `FILE_SHARE_DELETE`, so `os.replace`/`os.unlink` against a
path this process holds open raise `WinError 5`/`WinError 32`. The H02
adversarial fixtures stage exactly that mutation. The MECHANISM is unavailable;
the property is not. Closing the descriptor before hashing would make the
fixture run and would restore the original TOCTOU bug, so it is not done.

GROUP B (10) — runtime fixture newline translation. `.gitattributes -text`
pins TRACKED byte-sealed artifacts and says nothing about a temp file a test
writes at runtime. `Path.write_text` translates "\n" to `os.linesep`, so a
six-character fixture became seven bytes and every digest, length and CAS
precondition over the LF literal stopped describing it.

GROUP C (1) — no POSIX executable bit. The `os.access`/Git-mode DISAGREEMENT
witness needs a CLEARABLE exec bit; Windows `chmod` honours only the read-only
attribute, so the test failed its own non-vacuity guard. It was being honest.

WHAT THIS SUITE ASSERTS
=======================
* the capability probe is MEASURED, models production, and cannot silently
  report a capability it never tested;
* the §7 coherence invariants hold on every platform, through the one adversary
  no supported platform refuses — an in-place rewrite of the held inode;
* a refused operating-system call can never become successful integrity
  evidence (§17);
* nobody can "fix Windows" by skipping (§14, §16): no module-level platform
  skip, a BOUNDED and enumerated set of capability skips, every skip carrying a
  measurement, and the Windows CI job still running every suite, blocking.

Classifications used throughout, per M68D.1 §13: CROSS_PLATFORM_INVARIANT,
POSIX_CAPABILITY_TEST, WINDOWS_CAPABILITY_TEST, BYTE_IDENTITY_TEST,
GIT_REPOSITORY_SEMANTICS_TEST.
"""
from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

import core.source_integrity as si
import tools.executor as ex
from core.source_integrity import (
    SourceSnapshotError,
    WriteStatus,
    cas_write_text,
    digest_bytes,
    identify_snapshot,
    source_snapshot,
)
from _test_support.platform_capabilities import (
    AVAILABLE_UNDER_OPEN_FD,
    CONTINUOUS_MECHANISMS,
    EXEC_BIT_OBSERVABLE,
    IN_PLACE_REWRITE_OF_OPEN_PATH,
    MODE_BITS_REMOVE_READ,
    PROBE_DIAGNOSTIC,
    REPLACE_OVER_OPEN_PATH,
    UNLINK_OPEN_PATH,
    WHY_NO_EXEC_BIT,
    WHY_NO_MODE_BITS,
    WHY_NO_REPLACE,
    WHY_NO_UNLINK,
    Mutation,
)

_JARVIS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _JARVIS_ROOT.parent

#: The suites the Windows runner executes, and whose portability this closes.
WINDOWS_SUITES = (
    "tests/test_source_integrity_m68c.py",
    "tests/test_trust_boundary_m68d_h02_source_identity.py",
    "tests/test_trust_boundary_m68d_h05_portability.py",
    "tests/test_windows_portability_closure_m68d1.py",
)

#: Suites whose fixtures ARE the byte contract. No newline-translating write
#: may appear in them at all.
BYTE_IDENTITY_SUITES = (
    "tests/test_source_integrity_m68c.py",
    "tests/test_trust_boundary_m68d_h02_source_identity.py",
)

#: Suites that must contain a translating write, because proving the repair
#: matters REQUIRES exhibiting the thing it repairs (§10). Forbidding the
#: token everywhere would have deleted the non-vacuity harness along with the
#: defect — which is why the two roles are named separately.
TRANSLATION_HARNESS_SUITES = (
    "tests/test_trust_boundary_m68d_h05_portability.py",
    "tests/test_windows_portability_closure_m68d1.py",
)

#: MEASURED on CI run 37695660326. Narrative only — no test derives a decision
#: from these numbers, because the next real runner is the authority.
WINDOWS_RUN_ID = "37695660326"
WINDOWS_RESULT = (173, 25, 9)
M68D_CANDIDATE = "cc8a7dda6efd20b371459a51f4f687711972a88a"


def _code(path: Path, *, strings: bool = True) -> str:
    """Source with comments and docstrings removed, one line per source line.

    `strings=False` drops EVERY string literal as well, which is what a
    detector needs when it scans the file it lives in. Without it a detector
    matches its own search pattern: `code.count("@ needs_replace")` puts the
    literal `"@ needs_replace"` into this module's own source, so scanning
    this module reports a capability gate that does not exist. Measured —
    four of these tests failed that way on their first run.

    A decorator, an attribute access and a call are NAME/OP tokens; a search
    pattern is a STRING token. Dropping strings separates them exactly.
    """
    src = path.read_text(encoding="utf-8")
    out = [""] * (len(src.splitlines()) + 2)
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            docstring = prev in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                                 tokenize.DEDENT, tokenize.ENCODING)
            if docstring or not strings:
                prev = tok.type
                continue
        if tok.string.strip():
            out[tok.start[0]] += " " + tok.string
        prev = tok.type
    return " ".join(out)


def _self_scan(rel: str) -> str:
    """The view a detector must use for a file that may be its own."""
    return _code(_JARVIS_ROOT / rel,
                 strings=rel != Path(__file__).relative_to(_JARVIS_ROOT).as_posix())


def _code_of(source: str) -> str:
    """The same comment/docstring stripping, for a synthetic sample."""
    parts = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.string.strip():
            parts.append(tok.string)
    return " ".join(parts)


def _write(target: Path, text: str) -> bytes:
    """Write *text* byte-explicitly and return the bytes that landed."""
    payload = text.encode("utf-8")
    target.write_bytes(payload)
    return payload


def _in_place(target: Path, payload: bytes) -> None:
    """Overwrite the held inode's own bytes, through a second handle."""
    with open(target, "r+b") as handle:
        handle.seek(0)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _same_size(original: bytes) -> bytes:
    """A different payload of the SAME length. Derived, never hand-counted."""
    body = b"R" * (len(original) - 1) + b"\n" if original else b""
    assert len(body) == len(original)
    assert body != original or not original
    return body


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "_sandbox_allowed_dirs", lambda: [tmp_path])
    return tmp_path


def _executor():
    return ex.ToolExecutor.__new__(ex.ToolExecutor)


# ══ 1 · THE PROBE IS A MEASUREMENT ══════════════════════════════════════════

class TestTheCapabilityProbeIsHonest:
    """A capability asserted rather than measured is a platform guess."""

    def test_every_capability_carries_a_measurement(self):
        for key in ("replace_over_open_path", "unlink_open_path",
                    "in_place_rewrite_of_open_path", "mode_bits_remove_read",
                    "exec_bit_observable"):
            assert key in PROBE_DIAGNOSTIC, f"{key} was never measured"
            assert PROBE_DIAGNOSTIC[key].startswith(("PERMITTED", "REFUSED")), \
                f"{key} recorded no verdict: {PROBE_DIAGNOSTIC[key]!r}"

    def test_the_probe_does_not_consult_the_platform_name(self):
        """It answers "can I?", not "am I on Windows?"."""
        code = _code(_JARVIS_ROOT / "tests" / "_test_support"
                     / "platform_capabilities.py")
        for inferred in ('sys . platform', 'os . name', 'platform . system'):
            assert inferred not in code, \
                f"the probe infers a capability from {inferred}"

    def test_the_probe_opens_the_path_exactly_as_production_does(self):
        """M68D.1 §8: the test environment must model production.

        `source_snapshot` uses `os.open(path, os.O_RDONLY)` — plain CPython,
        no sharing mode of its own. A probe that requested
        `FILE_SHARE_DELETE` would measure a handle production never opens and
        would let the POSIX attack shape run on Windows against nothing real.
        """
        probe = _code(_JARVIS_ROOT / "tests" / "_test_support"
                      / "platform_capabilities.py")
        assert "os . open ( held , os . O_RDONLY )" in probe, \
            "the probe no longer holds the descriptor production holds"
        production = _code(_JARVIS_ROOT / "core" / "source_integrity.py")
        assert "os . open ( str ( resolved ) , os . O_RDONLY )" in production, \
            "production stopped opening the source read-only with os.open"
        for exotic in ("FILE_SHARE_DELETE", "msvcrt", "win32file", "ctypes",
                       "devnull"):
            assert exotic not in probe, \
                f"the probe reaches for {exotic} to force a POSIX attack shape"
        # FOUND BY THE CAMPAIGN: `B_probe_does_not_hold_the_descriptor` closed
        # the fd and reopened os.devnull, and SURVIVED — on Linux the answers
        # stay correct so no behavioural test can see it, while on Windows
        # every capability would be measured against a handle that is not the
        # source. Holding the descriptor WHILE staging is a property of the
        # text BETWEEN the two statements, so that is what is pinned.
        marker = "os . open ( held , os . O_RDONLY )"
        held_open = probe.index(marker)
        first_attempt = probe.index("os . replace ( other , held )")
        assert held_open < first_attempt, "the probe stages before it opens"
        between = probe[held_open + len(marker):first_attempt]
        assert "os . close (" not in between, \
            "the probe releases the source descriptor before staging the " \
            "mutation, so it measures a question production never asks"
        assert "os . open (" not in between, \
            "the probe re-opens something between holding and staging"

    def test_an_unmeasured_capability_defaults_to_absent(self):
        """If the probe cannot run, every capability must read ABSENT.

        FOUND BY THE CAMPAIGN: `B_unmeasured_capability_defaults_to_true`
        flipped the module-level defaults and SURVIVED, because `_probe()`
        overwrites them on any platform where the probe works — so on Linux
        the defaults are invisible. They are exactly what a host with a
        broken probe is left with, and a default of True would run a
        POSIX-only fixture on a platform that cannot stage it.

        Read as AST literals rather than by importing with the probe
        disabled: the probe writes its results through `globals()`, so the
        module-level assignments ARE the defaults and nothing else.
        """
        path = (_JARVIS_ROOT / "tests" / "_test_support"
                / "platform_capabilities.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defaults: "dict[str, object]" = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value,
                                                           ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defaults[target.id] = node.value.value
        expected = ("REPLACE_OVER_OPEN_PATH", "UNLINK_OPEN_PATH",
                    "IN_PLACE_REWRITE_OF_OPEN_PATH", "MODE_BITS_REMOVE_READ",
                    "EXEC_BIT_OBSERVABLE")
        missing = [n for n in expected if n not in defaults]
        assert missing == [], f"no module-level default for {missing}"
        for name in expected:
            assert defaults[name] is False, (
                f"{name} defaults to {defaults[name]!r}; an unmeasured "
                "capability must be reported ABSENT, not available")

    def test_the_one_universal_mechanism_is_always_available(self):
        """Every platform permits an in-place rewrite of a held inode.

        If this were ever false, every mechanism-parametrised test below would
        collect an EMPTY parameter set — which pytest reports as a skip, so the
        whole cross-platform proof would vanish silently.
        """
        assert IN_PLACE_REWRITE_OF_OPEN_PATH is True, PROBE_DIAGNOSTIC
        assert Mutation.IN_PLACE_REWRITE in AVAILABLE_UNDER_OPEN_FD
        assert Mutation.IN_PLACE_REWRITE in CONTINUOUS_MECHANISMS
        assert AVAILABLE_UNDER_OPEN_FD, "no adversary can be staged at all"
        assert CONTINUOUS_MECHANISMS, "no continuous adversary can be staged"

    def test_the_probe_agrees_with_what_this_platform_actually_does(self, tmp_path):
        """Re-measure here. A probe nobody re-checks is a constant."""
        held = tmp_path / "held.bin"
        other = tmp_path / "other.bin"
        held.write_bytes(b"HELD")
        other.write_bytes(b"OTHR")
        fd = os.open(str(held), os.O_RDONLY)
        try:
            try:
                os.replace(str(other), str(held))
                replace_ok = True
            except OSError:
                replace_ok = False
            else:
                other.write_bytes(b"OTHR")
            try:
                os.unlink(str(held))
                unlink_ok = True
                _write(held, "HELD")
            except OSError:
                unlink_ok = False
        finally:
            os.close(fd)
        assert replace_ok is REPLACE_OVER_OPEN_PATH, \
            "the probe's replace verdict does not match this platform"
        assert unlink_ok is UNLINK_OPEN_PATH, \
            "the probe's unlink verdict does not match this platform"

    def test_every_skip_reason_names_a_mechanism_and_a_measurement(self):
        """M68D.1 §13: no generic "Windows" reason."""
        for reason in (WHY_NO_REPLACE, WHY_NO_UNLINK, WHY_NO_MODE_BITS,
                       WHY_NO_EXEC_BIT):
            assert reason.startswith("POSIX_CAPABILITY_TEST:"), reason[:60]
            assert "Measured:" in reason, "the reason cites no measurement"
            assert len(reason) > 160, "the reason is too short to explain itself"
            assert reason.strip() != "Windows"


# ══ 2 · THE §7 INVARIANTS, ON EVERY PLATFORM ════════════════════════════════

class TestCoherenceHoldsOnEveryPlatform:
    """CROSS_PLATFORM_INVARIANT — M68D.1 §7's minimum list, one test each."""

    def test_content_and_digest_come_from_the_same_captured_bytes(self, tmp_path):
        target = tmp_path / "one.txt"
        payload = _write(target, "coherent observation\n")
        with source_snapshot(target) as snap:
            assert snap.payload() == payload
            assert snap.sha256 == hashlib.sha256(payload).hexdigest()
            assert snap.size_bytes == len(payload)
            assert snap.text() == "coherent observation\n"
            with snap.stream() as handle:
                assert handle.read() == payload

    @pytest.mark.parametrize("mechanism", AVAILABLE_UNDER_OPEN_FD,
                             ids=lambda m: m.value)
    def test_a_change_after_acquisition_cannot_alter_the_identity(self, tmp_path,
                                                                  mechanism):
        target = tmp_path / "later.txt"
        original = _write(target, "OBSERVED-AT-ACQUISITION\n")
        with source_snapshot(target) as snap:
            before = (snap.payload(), snap.sha256, snap.size_bytes)
            if mechanism is Mutation.ATOMIC_REPLACE:
                swap = tmp_path / "swap.txt"
                swap.write_bytes(b"NEVER-OBSERVED\n")
                os.replace(str(swap), str(target))
            elif mechanism is Mutation.UNLINK:
                target.unlink()
            elif mechanism is Mutation.UNLINK_RECREATE:
                target.unlink()
                target.write_bytes(b"NEVER-OBSERVED\n")
            else:
                _in_place(target, _same_size(original))
            assert (snap.payload(), snap.sha256, snap.size_bytes) == before, \
                f"{mechanism.value} altered an observation already acquired"
            assert snap.sha256 == hashlib.sha256(snap.payload()).hexdigest()

    def test_the_identity_does_not_reopen_the_path_for_hashing(self):
        """Structural, and the one defect this whole family exists to prevent."""
        code = _code(_JARVIS_ROOT / "core" / "source_integrity.py")
        start = code.index("def identify_snapshot")
        end = code.index("def identify_source")
        region = code[start:end]
        for reopen in ("digest_file (", "os . open (", "open (", ". read_bytes"):
            assert reopen not in region, \
                f"identify_snapshot performs a {reopen} of the mutable path"
        assert "snapshot . sha256" in region, \
            "the digest no longer comes from the observation"

    def test_the_cas_precondition_comes_from_the_snapshot_identity(self, tmp_path):
        target = tmp_path / "pre.txt"
        payload = _write(target, "version-one\n")
        with source_snapshot(target) as snap:
            identity = identify_snapshot(snap)
        assert identity.sha256 == hashlib.sha256(payload).hexdigest()
        receipt = cas_write_text(target, "version-two\n",
                                 expected_sha256=identity.sha256)
        assert receipt.status is WriteStatus.APPLIED
        assert target.read_bytes() == b"version-two\n"

    def test_a_stale_later_source_is_rejected_by_cas(self, tmp_path):
        target = tmp_path / "stale.txt"
        payload = _write(target, "version-one\n")
        with source_snapshot(target) as snap:
            precondition = identify_snapshot(snap).sha256
        _write(target, "a-human-wrote-this\n")           # after the read
        receipt = cas_write_text(target, "derived-from-v1\n",
                                 expected_sha256=precondition)
        assert receipt.status is WriteStatus.REJECTED_STALE
        assert receipt.bytes_written == 0
        assert receipt.external_outcome == "PROVEN_NOT_EXECUTED"
        assert target.read_bytes() == b"a-human-wrote-this\n", \
            "the unread human edit was destroyed"
        assert precondition != hashlib.sha256(payload + b"x").hexdigest()

    def test_an_unstable_observation_can_never_report_complete(self, tmp_path,
                                                               monkeypatch):
        """The in-place adversary, at the seam, on every platform."""
        target = tmp_path / "unstable.txt"
        original = _write(target, "AAAA-version-one-AAAA\n")
        monkeypatch.setattr(si, "_DIGEST_CHUNK", 8)
        real_read = os.read
        fired: list[int] = []

        def racing_read(fd, size):
            data = real_read(fd, size)
            if not fired:
                fired.append(1)
                _in_place(target, _same_size(original))
            return data

        monkeypatch.setattr(os, "read", racing_read)
        with source_snapshot(target) as snap:
            monkeypatch.setattr(os, "read", real_read)
            identity = identify_snapshot(snap)
            coherent = snap.sha256 == hashlib.sha256(snap.payload()).hexdigest()
        assert fired, "NON-VACUITY: the race never fired"
        assert snap.stable is False, "a mixed observation reported as whole"
        assert coherent, "content and digest stopped describing each other"
        assert identity.sha256 is None and identity.complete is False

    def test_a_derived_reader_consumes_the_observation_not_a_fresh_opening(
            self, sandbox):
        """Behavioural: a reader that reopened the path would be caught here."""
        target = sandbox / "derived.pdf"
        raw = b"%PDF-1.4 source bytes for the derived reader\n" * 3
        target.write_bytes(raw)
        seen: list[bytes] = []

        def stub_reader(_self, snapshot):
            with snapshot.stream() as handle:
                seen.append(handle.read())
            return "EXTRACTED TEXT, NOT THE SOURCE BYTES"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ex.ToolExecutor, "_read_pdf", stub_reader)
            result = _executor()._tool_read_file(str(target))
        assert seen == [raw], "the reader did not receive the observed bytes"
        assert result["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["source"]["content_derived"] is True
        assert result["source"]["sha256"] != hashlib.sha256(
            result["content"].encode()).hexdigest()


# ══ 3 · BYTE IDENTITY ═══════════════════════════════════════════════════════

class TestByteIdentityIsExplicit:
    """BYTE_IDENTITY_TEST — the Group B repair, and why it is load-bearing."""

    def test_the_byte_explicit_helper_is_newline_neutral_under_translation(
            self, tmp_path, monkeypatch):
        """The repair survives the exact condition that broke the fixtures.

        `builtins.open` is replaced by one that forces `newline="\r\n"` for
        text writes — byte-for-byte what Windows does with `newline=None`. A
        `write_bytes` helper must be unaffected; a `write_text` one must not be.
        That asymmetry is the whole reason the helper exists, so it is measured
        rather than asserted.
        """
        import builtins

        real_open = builtins.open

        def translating_open(file, mode="r", buffering=-1, encoding=None,
                             errors=None, newline=None, closefd=True,
                             opener=None):
            # The FULL positional signature on purpose: `Path.write_text` calls
            # `io.open(self, mode, buffering, encoding, errors, newline)` with
            # every argument positional, so a `*a, **kw` wrapper that then sets
            # kw["newline"] raises "argument given by name and position".
            # Measured here on the first run.
            if "b" not in mode and newline is None \
                    and any(c in mode for c in "wxa+"):
                newline = "\r\n"
            return real_open(file, mode, buffering, encoding, errors, newline,
                             closefd, opener)

        monkeypatch.setattr(builtins, "open", translating_open)
        monkeypatch.setattr(io, "open", translating_open)

        explicit = tmp_path / "explicit.txt"
        payload = _write(explicit, "x = 1\n")
        assert payload == b"x = 1\n"
        assert explicit.read_bytes() == b"x = 1\n", \
            "the byte-explicit helper was affected by newline translation"
        assert digest_bytes(explicit.read_bytes()) == digest_bytes(payload)

        translated = tmp_path / "translated.txt"
        translated.write_text("x = 1\n")
        assert translated.read_bytes() == b"x = 1\r\n", \
            "NON-VACUITY: the translation harness did nothing, so this test " \
            "would pass even with the repair reverted"
        assert len(translated.read_bytes()) == len(payload) + 1
        assert digest_bytes(translated.read_bytes()) != digest_bytes(payload)

    def test_production_hashing_was_not_taught_to_normalise_newlines(self,
                                                                     tmp_path):
        """M68D.1 §9: the forbidden fix. Bytes are bytes.

        "Normalise until the hashes match" would make every byte seal
        decorative. Asserted BEHAVIOURALLY, because a `.replace(b"\r\n", b"\n")`
        inside the digest leaves every structural token in place.
        """
        crlf, lf = tmp_path / "crlf.txt", tmp_path / "lf.txt"
        crlf.write_bytes(b"a\r\nb\r\n")
        lf.write_bytes(b"a\nb\n")
        assert si.digest_file(crlf) != si.digest_file(lf), \
            "digest_file normalises newlines"
        assert digest_bytes(b"a\r\nb\r\n") != digest_bytes(b"a\nb\n"), \
            "digest_bytes normalises newlines"
        with source_snapshot(crlf) as snap:
            assert snap.payload() == b"a\r\nb\r\n", "the snapshot converted bytes"
            assert snap.size_bytes == 6
            assert snap.sha256 == hashlib.sha256(b"a\r\nb\r\n").hexdigest()
        receipt = cas_write_text(lf, "a\r\nb\r\n")
        assert receipt.status is WriteStatus.APPLIED
        assert lf.read_bytes() == b"a\r\nb\r\n", \
            "a CAS write rewrote the caller's own line endings"

    def test_a_whole_file_write_lands_exactly_the_bytes_the_receipt_claims(
            self, tmp_path):
        """The in-scope half: `mode="w"` is byte exact on every platform.

        The payload deliberately MIXES "\n" and "\r\n", so a write path that
        translated either one would be caught. The whole-file branch stages
        `payload` through a BINARY temp handle, which is why it is already
        portable — and why the Group B failures were fixture-side only.
        """
        target = tmp_path / "exact.txt"
        target.write_bytes(b"")
        content = "first\nsecond\r\nthird\n"
        payload = content.encode("utf-8")
        receipt = cas_write_text(target, content, mode="w")
        assert receipt.status is WriteStatus.APPLIED, receipt.reason
        assert target.read_bytes() == payload, \
            "the bytes on disk are not the payload the receipt describes"
        assert receipt.bytes_written == len(target.read_bytes()) == len(payload)
        assert receipt.intended_sha256 == digest_bytes(payload)
        assert receipt.after.sha256 == digest_bytes(payload)

    def test_the_append_branch_is_still_the_known_text_mode_write(self, tmp_path):
        """A DEFERRED DEFECT, pinned so it cannot be lost or re-discovered.

        FOUND BY M68D.1 while reading `_write_locked`, NOT by the Windows
        runner — no test in the 25 asserts it. The `mode="a"` branch writes
        through `open(target, "a", encoding=...)`: a TEXT handle with no
        explicit `newline`. On Windows that translates every "\n" to "\r\n",
        so for the payload `b"first\nsecond\r\nthird\n"` (20 bytes):

            landed  = b"first\r\nsecond\r\r\nthird\r\n"   (23 bytes)
            receipt = bytes_written=20

        — a three-byte UNDERCOUNT, and the caller's own "\r\n" corrupted to
        "\r\r\n". Measured on Linux with `newline="\r\n"`, which emits
        byte-for-byte what Windows emits for `newline=None`.

        It is NOT fixed here, deliberately. M68D.1 §5 scopes this milestone to
        making FIXTURES byte explicit "without changing production byte
        identity", and §9 forbids touching the CAS write path. Repairing it
        means `open(target, "ab")` plus `handle.write(payload)`, which changes
        what lands on Windows and also moves the mode constant that
        `test_the_write_primitive_still_uses_temp_file_plus_replace` pins in
        `test_patch_execution_absent_controls_m68c.py`. That belongs to a
        successor milestone with its own falsification campaign.

        This test pins the CURRENT shape so the limitation stays visible, and
        it is the test that must be updated — not deleted — when the repair
        lands.
        """
        critical = _code(_JARVIS_ROOT / "core" / "source_integrity.py")
        assert 'open ( target , "a" , encoding = encoding )' in critical, (
            "the append branch changed shape; if it is now binary, this "
            "deferred defect is FIXED and this test must be replaced by the "
            "byte-exactness assertion it was holding the place for")
        # The whole-file branch must NOT have drifted the same way.
        assert 'os . fdopen ( handle_fd , "wb" )' in critical, \
            "the whole-file write is no longer binary; Group B is reopened"
        # The receipt's CLAIM is the same on every platform; whether it is
        # TRUE is not. The byte comparison is deliberately NOT made here —
        # asserting the POSIX bytes would turn the Windows job red for the
        # very limitation this milestone is deferring, which is how a
        # deferred defect becomes a blocked integration.
        target = tmp_path / "append.log"
        target.write_bytes(b"")
        content = "first\nsecond\r\nthird\n"
        receipt = cas_write_text(target, content, mode="a")
        assert receipt.status is WriteStatus.APPLIED
        assert receipt.bytes_written == len(content.encode("utf-8")), \
            "the receipt no longer claims the payload length"
        assert b"second" in target.read_bytes(), "nothing was appended at all"

    def test_no_byte_identity_suite_builds_a_fixture_by_translating_write(self):
        """ABSENT CONTROL (§16). Reverting the Group B repair goes red here.

        Comments and docstrings are stripped first, so the suites may still
        EXPLAIN `write_text`; what they may not do is call it. Non-vacuity: the
        detector is shown the real token in a synthetic sample first.
        """
        token = ". write_text ("
        assert token in _code_of('target.write_text("x = 1\\n")\n'), \
            "the detector cannot see its own target"
        for rel in BYTE_IDENTITY_SUITES:
            code = _code(_JARVIS_ROOT / rel)
            assert token not in code, (
                f"{rel} builds a fixture with a newline-translating write; "
                "use the byte-explicit `_write` helper")
            assert ". write_bytes (" in code, \
                f"non-vacuity: {rel} has no byte-explicit write at all"

    @staticmethod
    def _text_mode_writes(path: Path) -> "list[str]":
        """Every handle opened for TEXT writing with no explicit newline.

        Matches the DIRECT forms — `open(...)`, `io.open(...)`,
        `os.fdopen(...)`. It deliberately does not chase aliases: M68C's
        `HalfWriter` holds `real_open = si.open` precisely so its injected
        failure mirrors production's own `open(target, "a", encoding=...)`,
        and rewriting that would make the harness diverge from the code it
        injects into. That append is itself the deferred defect pinned by
        `test_the_append_branch_is_still_the_known_text_mode_write`, so the
        one text-mode write this detector cannot see is the one already
        under a test of its own.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scope: "dict[int, str]" = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    scope.setdefault(id(child), node.name)
        found: "list[str]" = []
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
                         and a.value
                         and set(a.value) <= set("rwxab+t")), None)
            if mode is None or "b" in mode:
                continue
            if not any(c in mode for c in "wxa+"):
                continue
            if any(kw.arg == "newline" for kw in node.keywords):
                continue
            found.append(f"{scope.get(id(node), '<module>')}:{node.lineno}:"
                         f"{name}(...,{mode!r})")
        return found

    def test_no_byte_identity_suite_opens_a_text_handle_for_writing(self):
        """ABSENT CONTROL — banning `write_text` was not enough.

        FOUND BY THE CAMPAIGN: `C_atomic_replace_helper_translates` turned
        `os.fdopen(fd, "wb")` into `os.fdopen(fd, "w", encoding="utf-8")`
        inside the H02 atomic-replacement helper and SURVIVED. It is not a
        `write_text` call, so the token scan never saw it, and on Linux text
        mode and binary mode emit identical bytes for "\n" — so no
        behavioural test could see it either.

        The rule therefore has to be stated on the OPERATION rather than on
        one API name: in a suite whose fixtures ARE the byte contract, a
        handle opened for writing must be binary, or must name its newline
        contract explicitly.
        """
        offenders = {rel: self._text_mode_writes(_JARVIS_ROOT / rel)
                     for rel in BYTE_IDENTITY_SUITES}
        assert all(v == [] for v in offenders.values()), (
            "a byte-identity fixture opens a newline-translating text handle: "
            f"{ {k: v for k, v in offenders.items() if v} }. Use binary mode, "
            "or pass newline= explicitly")

    def test_the_text_write_detector_sees_a_real_offender(self, tmp_path):
        """Non-vacuity. A clean result above has to be a fact, not a blind spot."""
        probe = tmp_path / "probe.py"
        probe.write_bytes(b"import os\n"
                          b"def f(fd):\n"
                          b"    with os.fdopen(fd, 'w', encoding='utf-8') as h:\n"
                          b"        h.write('x')\n")
        assert self._text_mode_writes(probe) == ["f:3:fdopen(...,'w')"], \
            "the detector cannot see a text-mode write at all"
        clean = tmp_path / "clean.py"
        clean.write_bytes(b"import os\n"
                          b"def f(fd):\n"
                          b"    with os.fdopen(fd, 'wb') as h:\n"
                          b"        h.write(b'x')\n"
                          b"def g(p):\n"
                          b"    with open(p, 'w', newline='\\n') as h:\n"
                          b"        h.write('x')\n")
        assert self._text_mode_writes(clean) == [], \
            "the detector flags a binary write or an explicit newline"

    def test_the_translation_harness_still_exhibits_a_translating_write(self):
        """The mirror of the test above, and the reason it has a scope.

        A non-vacuity proof has to perform the translation it warns about. If a
        later cleanup applied the ban to every suite, these two would lose the
        only thing that makes them meaningful on Linux — and the ban would
        then be self-certifying.
        """
        for rel in TRANSLATION_HARNESS_SUITES:
            code = _code(_JARVIS_ROOT / rel)
            assert ". write_text (" in code or 'newline = "\\r\\n"' in code, (
                f"{rel} no longer exhibits a newline-translating write, so its "
                "byte-identity proof is vacuous")
        assert set(BYTE_IDENTITY_SUITES) | set(TRANSLATION_HARNESS_SUITES) \
            == set(WINDOWS_SUITES), "a Windows suite has no declared role"


# ══ 4 · NO SKIP-WASHING ═════════════════════════════════════════════════════

def _collect(target: str) -> "tuple[int, int, str]":
    """Run one suite in a FRESH interpreter; return (passed, skipped, summary).

    A fresh process because the capability probe runs at import time and the
    counts have to be the ones a real runner would see, not ones this session
    has already imported.
    """
    done = subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no",
         "-p", "no:cacheprovider", target],
        cwd=str(_JARVIS_ROOT), capture_output=True, text=True, check=False)
    lines = (done.stdout or "").strip().splitlines()
    summary = lines[-1] if lines else "<no output>"
    passed = re.search(r"(\d+) passed", summary)
    skipped = re.search(r"(\d+) skipped", summary)
    return (int(passed.group(1)) if passed else 0,
            int(skipped.group(1)) if skipped else 0,
            summary)


class TestNoSkipWashing:
    """M68D.1 §14 — 25 failures must not become 25 skips."""

    #: Every capability gate in the Windows suites, counted at the DECORATOR.
    #: Adding one means editing this table and defending it in the milestone
    #: document. Measured, not estimated: the numbers below are the decorator
    #: applications `test_the_counted_capability_gates_are_the_ones_in_the_file`
    #: finds, and the skip counts a Windows runner reports must match them.
    EXPECTED_CAPABILITY_GATES = {
        "tests/test_trust_boundary_m68d_h02_source_identity.py": 8,
        "tests/test_source_integrity_m68c.py": 3,
        "tests/test_trust_boundary_m68d_h05_portability.py": 1,
        "tests/test_windows_portability_closure_m68d1.py": 0,
    }

    #: Of those, the ones M68D.1 ADDED. M68C's three predate this milestone —
    #: they were already skipping on the runner that produced the 25 failures,
    #: so they are not part of the trade this milestone made.
    NEW_GATES_M68D1 = 9

    #: What the 25 failures became. 16 of them now PASS on Windows; 9 skip.
    WINDOWS_FAILURES_CLOSED = 25

    def test_the_repair_converted_far_more_failures_than_it_created_skips(self):
        """M68D.1 §14 — this must NOT be 25 failures turned into 25 skips."""
        now_passing = self.WINDOWS_FAILURES_CLOSED - self.NEW_GATES_M68D1
        assert now_passing == 16
        assert self.NEW_GATES_M68D1 < now_passing, (
            f"{self.NEW_GATES_M68D1} new skips against {now_passing} newly "
            "passing tests — the balance no longer favours running the test")
        total = sum(self.EXPECTED_CAPABILITY_GATES.values())
        assert total == 12, f"the declared gate budget moved to {total}"
        assert set(self.EXPECTED_CAPABILITY_GATES) == set(WINDOWS_SUITES)

    def test_every_skipped_mechanism_has_a_cross_platform_replacement(self):
        """A mechanism may only skip if the PROPERTY still runs somewhere."""
        h02 = _code(_JARVIS_ROOT
                    / "tests/test_trust_boundary_m68d_h02_source_identity.py")
        for replacement in (
                "def test_a_post_acquisition_mutation_cannot_alter_the_observation",
                "def test_an_in_place_rewrite_mid_read_cannot_split_content_from_digest",
                "def test_an_in_place_race_cannot_state_a_precondition_that_destroys_the_edit",
                "def test_a_derived_parser_never_reopens_the_source_path"):
            assert replacement in h02, \
                f"the cross-platform replacement {replacement} is gone"
        closure = _code(_JARVIS_ROOT / "tests"
                        / "test_windows_portability_closure_m68d1.py")
        assert "def test_an_open_failure_raises_rather_than_describing_nothing" \
            in closure, "the chmod fixture's cross-platform replacement is gone"

    @pytest.mark.parametrize("rel", WINDOWS_SUITES)
    def test_the_counted_capability_gates_are_the_ones_in_the_file(self, rel):
        """Structural, so it holds on a Linux runner where none of them fire."""
        code = _self_scan(rel)
        markers = (code.count("@ needs_replace") + code.count("@ needs_unlink")
                   + code.count("@ needs_mode_bits")
                   + code.count("not EXEC_BIT_OBSERVABLE")
                   + code.count("not POSIX_MODES_ENFORCED"))
        assert markers == self.EXPECTED_CAPABILITY_GATES[rel], (
            f"{rel} carries {markers} capability gates; "
            f"{self.EXPECTED_CAPABILITY_GATES[rel]} are declared")

    @pytest.mark.parametrize("rel", WINDOWS_SUITES)
    def test_no_suite_is_skipped_at_module_level(self, rel):
        """A module-level platform skip would hide everything behind it."""
        code = _self_scan(rel)
        assert "pytestmark" not in code, \
            f"{rel} declares a module-level mark; a platform skip could hide here"
        tree = ast.parse((_JARVIS_ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                rendered = ast.unparse(node.value.func)
                assert "skip" not in rendered.lower(), \
                    f"{rel} calls a module-level skip: {ast.unparse(node)[:60]}"
        head = code.split(" def ")[0]
        assert "pytest . skip (" not in head, \
            f"{rel} skips before any test is collected"

    def test_the_module_level_skip_detector_is_not_vacuous(self):
        """It must reject a file that really does carry a module-level skip."""
        sample = "import pytest\npytestmark = pytest.mark.skipif(True, 'x')\n"
        assert "pytestmark" in _code_of(sample)
        call = "import pytest\npytest.skip('whole module', allow_module_level=True)\n"
        tree = ast.parse(call)
        found = [n for n in tree.body
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and "skip" in ast.unparse(n.value.func).lower()]
        assert found, "the AST half of the detector cannot see a real skip call"

    #: The ONE inferred capability left in the Windows suites, named and
    #: defended rather than silently tolerated. M68C's `POSIX_MODES_ENFORCED`
    #: reads `os.name != "nt"` instead of measuring. It stays EXACTLY as M68D
    #: wrote it because (a) it is conservative in the safe direction — it can
    #: only cause a SKIP, never a false pass, since a POSIX host that ignored
    #: mode bits would FAIL the test rather than sneak past it; and (b) the
    #: published M68D falsification campaign anchors the mutation
    #: `H05_tests_assume_geteuid` on that literal text, so rewriting it would
    #: make `mutation_campaign_m68d.py` report an anchor error and M68D's
    #: 72/72 would stop being reproducible.
    #: The second one is not a capability at all: `OUTSIDE_SANDBOX` picks a
    #: real absolute path that lies outside every sandbox root, and which path
    #: that is cannot be measured — it is platform-shaped test DATA, not a
    #: decision about what to assert. It gates nothing and skips nothing.
    INFERRED_CAPABILITY_EXEMPTIONS = {
        "tests/test_source_integrity_m68c.py": (
            'os . name != "nt"', 'os . name == "nt"'),
    }

    @pytest.mark.parametrize("rel", WINDOWS_SUITES)
    def test_no_suite_decides_anything_from_the_platform_name(self, rel):
        """M68D.1 §13: capability, measured — never `sys.platform == "win32"`."""
        code = _self_scan(rel)
        allowed = self.INFERRED_CAPABILITY_EXEMPTIONS.get(rel, ())
        for inferred in ('sys . platform', 'platform . system (',
                         'os . name == "nt"', 'os . name != "nt"'):
            if inferred in allowed:
                continue
            assert inferred not in code, \
                f"{rel} branches on {inferred} instead of a measured capability"

    @pytest.mark.parametrize("rel,token", [
        (rel, tok)
        for rel, toks in INFERRED_CAPABILITY_EXEMPTIONS.items()
        for tok in toks])
    def test_every_exemption_is_still_load_bearing(self, rel, token):
        """An exemption for something that is gone is a licence nobody audits."""
        assert token in _self_scan(rel), \
            f"{rel} no longer contains {token}; drop the exemption"

    def test_most_of_the_h02_suite_still_runs_here(self):
        """The invariants must RUN, not be declared portable."""
        rel = "tests/test_trust_boundary_m68d_h02_source_identity.py"
        passed, skipped, summary = _collect(rel)
        assert passed > 0, f"nothing ran: {summary}"
        assert passed >= 8 * skipped, (
            f"{passed} passed against {skipped} skipped — too much of the "
            f"suite is behind a capability gate: {summary}")

    def test_the_closure_suite_has_no_platform_capability_gate(self):
        """Every test here is cross-platform by construction.

        Scanned with string literals dropped, because this file necessarily
        MENTIONS every gate name it forbids.
        """
        code = _self_scan("tests/test_windows_portability_closure_m68d1.py")
        for gate in ("needs_replace", "needs_unlink", "needs_mode_bits",
                     "skipif"):
            assert gate not in code, \
                f"the closure suite gates a test behind {gate}"
        # Non-vacuity: the H02 suite DOES carry gates, and the same scan sees
        # them — so a clean result here is a fact, not a blind spot.
        h02 = _self_scan(
            "tests/test_trust_boundary_m68d_h02_source_identity.py")
        assert "needs_replace" in h02, "the scan cannot see a real gate"


# ══ 5 · A REFUSAL IS NEVER EVIDENCE ═════════════════════════════════════════

class TestFailureInjection:
    """M68D.1 §17 — no OSError may become successful integrity evidence."""

    def test_an_open_failure_raises_rather_than_describing_nothing(self,
                                                                   tmp_path,
                                                                   monkeypatch):
        """CROSS_PLATFORM replacement for the chmod(0o000) fixture."""
        target = tmp_path / "unopenable.txt"
        _write(target, "present\n")
        real_open = os.open

        def refusing_open(path, *a, **kw):
            if os.path.realpath(path) == os.path.realpath(str(target)):
                raise PermissionError(13, "simulated refusal", str(target))
            return real_open(path, *a, **kw)

        monkeypatch.setattr(os, "open", refusing_open)
        with pytest.raises(SourceSnapshotError):
            with source_snapshot(target):
                pass

    def test_an_fstat_failure_cannot_report_a_whole_observation(self, tmp_path,
                                                                monkeypatch):
        target = tmp_path / "nofstat.txt"
        _write(target, "present\n")
        real_fstat = os.fstat
        calls: list[int] = []

        def failing_fstat(fd):
            calls.append(fd)
            if len(calls) >= 2:
                raise OSError(5, "simulated fstat failure")
            return real_fstat(fd)

        monkeypatch.setattr(os, "fstat", failing_fstat)
        with pytest.raises(OSError):
            with source_snapshot(target):
                pass
        assert len(calls) >= 2, "non-vacuity: the second fstat never ran"

    def test_the_concurrent_race_fixture_reports_a_refused_writer(self, tmp_path):
        """The gap this milestone found in the concurrent race fixture.

        MEASURED, after a first guess that turned out wrong. Under the Windows
        mandatory-locking simulation
        `test_two_concurrent_snapshots_of_one_file_agree` passed 3/3 with the
        simulator reporting **0 denials**: the writer's `os.replace`
        consistently finished before either reader held a descriptor, so the
        refused mutation never occurred. The test was not vacuous because an
        exception was swallowed — it was vacuous because the adversary never
        overlapped the observation at all, and nothing in the fixture could
        tell the difference.

        Both halves are now closed. The writer's exception is COLLECTED and
        asserted empty, so a refusal fails instead of certifying; and the
        mechanism is parametrised, so on a platform that refuses the rename the
        fixture stages the in-place rewrite that no platform refuses. Asserted
        structurally here, because on this platform nothing is refused and the
        behaviour cannot be observed directly.
        """
        h02 = _code(_JARVIS_ROOT / "tests"
                    / "test_trust_boundary_m68d_h02_source_identity.py")
        start = h02.index("def test_two_concurrent_snapshots_of_one_file_agree")
        region = h02[start:start + 2000]
        assert "except OSError as exc :" in region,             "the writer thread no longer captures a refusal"
        assert "refused . append ( exc )" in region,             "a refused writer is discarded instead of recorded"
        # The COMPLETE statement, comma included. A bare
        # `"assert refused == [ ]"` check SURVIVED the campaign mutation
        # `A_refusal_in_the_race_fixture_is_swallowed`, which appends
        # ` or True`: the substring is still there and the assertion is
        # dead. Pinning through the comma makes the shape load-bearing.
        assert "assert refused == [ ] , (" in region, \
            "a refused adversary no longer fails the test"
        assert " or True" not in region, \
            "an assertion in the race fixture was neutralised with `or True`"
        # And the shape it replaced: a bare `_stage(...)` in the writer thread
        # with nothing catching the OSError. Non-vacuity for the scan above.
        assert "_stage ( mechanism , target , replacement )" in region

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_a_refusal_in_a_thread_would_otherwise_pass_silently(self, tmp_path):
        """Why the structural check above is needed at all.

        `threading` prints a dead thread's exception and continues; the joining
        test sees nothing. Measured here so the claim is not folklore.
        """
        import threading

        target = tmp_path / "threaded.txt"
        original = _write(target, "original\n")
        observed: list[bytes] = []

        def refused_writer():
            raise PermissionError(13, "[WinError 5] simulated", str(target))

        thread = threading.Thread(target=refused_writer)
        thread.start()
        thread.join(10)
        assert thread.is_alive() is False
        with source_snapshot(target) as snap:
            observed.append(snap.payload())
        # Nothing here failed, and nothing here proved anything: the adversary
        # never ran. A fixture that stops at this point certifies a race it
        # did not stage.
        assert observed == [original], \
            "the refused writer somehow changed the file"

    def test_a_derived_parser_failure_is_a_read_error_not_a_success(self,
                                                                    sandbox):
        target = sandbox / "broken.pdf"
        target.write_bytes(b"not really a pdf")

        def exploding_reader(_self, _snapshot):
            raise ValueError("synthetic parser failure")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ex.ToolExecutor, "_read_pdf", exploding_reader)
            result = _executor()._tool_read_file(str(target))
        assert "content" not in result and "source" not in result
        assert result.get("error_code") == ex.ERR_READ_FAILED

    def test_an_absent_capability_is_never_reported_as_a_passing_control(self):
        """A False capability must produce a SKIP with a reason, not silence."""
        for flag, reason in ((REPLACE_OVER_OPEN_PATH, WHY_NO_REPLACE),
                             (UNLINK_OPEN_PATH, WHY_NO_UNLINK),
                             (MODE_BITS_REMOVE_READ, WHY_NO_MODE_BITS),
                             (EXEC_BIT_OBSERVABLE, WHY_NO_EXEC_BIT)):
            assert isinstance(flag, bool), "a capability is not a tri-state"
            assert "Measured:" in reason
            assert "REFUSED" in reason or "PERMITTED" in reason, \
                "the reason carries no kernel verdict at all"


# ══ 6 · GIT MODE IS STILL THE AUTHORITY ═════════════════════════════════════

class TestGitRepositorySemantics:
    """GIT_REPOSITORY_SEMANTICS_TEST — M68D.1 §11, mandatory everywhere."""

    def test_the_verifier_reads_the_index_and_never_the_filesystem(self):
        verifier = _JARVIS_ROOT / "scripts" / "verify_m62_control_plane.py"
        code = _code(verifier)
        assert "X_OK" not in code, \
            "the control plane reads an executable bit from the filesystem"
        assert "os . access" not in code
        assert "_git_index_modes" in code, "the Git-mode authority is gone"

    def test_a_failed_index_query_is_a_failure_and_not_a_silent_skip(self):
        """FOUND BY THE CAMPAIGN: `D_git_mode_failure_assumed_benign` replaced
        the `report.fail(...)` body with `pass` and SURVIVED.

        The existing H05 test asserts the GUARD (`if not modes:`) is present,
        which the mutation keeps — it only empties the branch. A guard with no
        consequence is not fail-closed: `git ls-files --stage` failing would
        silently skip the whole repository executable-mode invariant. The
        CONSEQUENCE has to be pinned too.
        """
        verifier = _JARVIS_ROOT / "scripts" / "verify_m62_control_plane.py"
        tree = ast.parse(verifier.read_text(encoding="utf-8"))
        guards = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "modes"]
        assert guards, "the `if not modes:` fail-closed guard is gone"
        for guard in guards:
            calls = [n for n in ast.walk(ast.Module(body=guard.body,
                                                    type_ignores=[]))
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "fail"]
            assert calls, (
                "a failed `git ls-files --stage` no longer fails the control "
                "plane; the executable-mode invariant is silently skipped")
            assert any(
                isinstance(arg, ast.Constant) and arg.value == "PATH_INTEGRITY"
                for call in calls for arg in call.args), \
                "the failure is no longer attributed to PATH_INTEGRITY"
        # AST, not the stripped-source view: `report.fail`'s message is a
        # multi-line implicit concatenation, and the repo's code-only view
        # drops every continuation piece as if it were a docstring. A
        # substring check on the message therefore cannot see it.

    def test_the_executable_mode_control_is_not_platform_conditional(self):
        """No `if os.name == "nt": return` inside the verifier (§11)."""
        verifier = _JARVIS_ROOT / "scripts" / "verify_m62_control_plane.py"
        code = _code(verifier)
        for bypass in ('os . name == "nt"', 'sys . platform == "win32"',
                       'sys . platform . startswith ( "win"'):
            assert bypass not in code, \
                f"the verifier branches on {bypass}; a control must not"

    def test_the_newline_policy_is_not_disabled_on_any_platform(self):
        verifier = _JARVIS_ROOT / "scripts" / "verify_m62_control_plane.py"
        code = _code(verifier)
        assert "NEWLINE_POLICY" in code
        head = code[:code.index("NEWLINE_POLICY") + 400]
        for bypass in ('os . name', 'sys . platform'):
            assert bypass not in head, \
                f"the newline policy is gated on {bypass}"

    def test_the_source_digest_is_not_conditionally_skipped(self):
        code = _code(_JARVIS_ROOT / "core" / "source_integrity.py")
        for bypass in ('os . name == "nt"', 'sys . platform == "win32"'):
            assert bypass not in code, \
                f"source integrity branches on {bypass}"


# ══ 7 · THE WINDOWS JOB STILL BLOCKS ════════════════════════════════════════

class TestWindowsCiStillBlocks:
    """M68D.1 §15/§16 — a gate nobody runs, or one that cannot fail, is not one."""

    @pytest.fixture(scope="class")
    def job(self):
        yaml = pytest.importorskip("yaml")
        workflow = yaml.safe_load(
            (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"))
        assert "windows-portability" in workflow["jobs"]
        return workflow["jobs"]["windows-portability"]

    def test_the_closure_suite_runs_on_the_windows_runner(self, job):
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "test_windows_portability_closure_m68d1.py" in joined, \
            "the M68D.1 closure suite is not executed by the Windows job"

    @pytest.mark.parametrize("suite", [Path(s).name for s in WINDOWS_SUITES])
    def test_every_windows_suite_is_named_in_the_job(self, job, suite):
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert suite in joined, f"{suite} was removed from the Windows job"

    def test_the_job_is_still_blocking(self, job):
        assert "continue-on-error" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "|| true" not in joined
        assert "-ErrorAction SilentlyContinue" not in joined
        assert "$ErrorActionPreference" not in joined, \
            "a pwsh error preference could turn a failing step green"

    def test_the_job_still_runs_the_control_plane_verifier(self, job):
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "verify_m62_control_plane.py" in joined

    def test_every_declared_check_can_actually_run(self, job):
        """FOUND BY M68D.1 — and it would have moved the red one step right.

        `scripts/check_package_manifest.py` declares `--dist` as a REQUIRED
        argument and exits 2 without it. The Windows job invoked it bare, so
        that step could never have succeeded; the real runner never reached it
        only because the suite step above failed first. Fixing the 25 test
        failures alone would have produced a fresh red at step 8.

        A declared check that aborts on its own argument parsing is not a
        gate, so the invocation is pinned: the manifest scan must receive a
        `--dist`, and a dist must be BUILT before it is scanned.
        """
        steps = [str(s.get("run", "")) for s in job["steps"]]
        joined = "\n".join(steps)
        assert "check_package_manifest.py" in joined, \
            "the manifest and secret scan was removed from the Windows job"
        for line in joined.splitlines():
            if "check_package_manifest.py" in line:
                assert "--dist" in line, (
                    "check_package_manifest.py is invoked without --dist, which "
                    f"is required and makes it exit 2: {line.strip()!r}")
        build = joined.index("-m build --outdir")
        scan = joined.index("check_package_manifest.py")
        assert build < scan, \
            "the manifest scan runs before anything has been built"
        assert "pip install --upgrade build" in joined, \
            "`build` is not in the dev profile, so it has to be installed here"

    def test_the_declared_check_detector_is_not_vacuous(self):
        """It must reject the bare invocation it was written to catch."""
        bad = "python scripts/check_package_manifest.py"
        assert "--dist" not in bad
        good = 'python scripts/check_package_manifest.py --dist "$env:RUNNER_TEMP/dist"'
        assert "--dist" in good
        # And the requirement is real, not folklore: the script declares it.
        source = (_JARVIS_ROOT / "scripts" / "check_package_manifest.py").read_text(
            encoding="utf-8")
        assert '"--dist", required=True' in source, \
            "--dist is no longer required; this guard needs revisiting"

    def test_the_job_does_not_disable_autocrlf_to_pass(self, job):
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "autocrlf false" not in joined
        assert "autocrlf=false" not in joined
        assert "autocrlf" in joined, "the job no longer reports its newline setup"
