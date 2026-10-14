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
Group A cannot be reproduced natively on a host with no text mode. So the C
runtime is SIMULATED there — and the simulation is driven by exactly ONE
injected attribute, ``os.O_BINARY``, from which production derives its own
flags through ``source_integrity._source_open_flags()``. Nothing here
re-implements that derivation, so a passing result is a statement about
production rather than about this harness.
:class:`TestTheTextRuntimeReallyTranslates` proves an unrepaired read IS
corrupted, which is what makes the repaired result mean anything.

M68D.3 — SIMULATE ONLY WHAT THE HOST DOES NOT ALREADY PROVIDE
=============================================================
The first version of that simulator was unconditional, and the real Windows
runner of CI run **38007992655** on candidate ``a0c6f08`` returned
376 passed / 20 failed / 17 skipped — **all 20 inside
:class:`TestBinaryByteIdentity`, and not one of them a production defect.**
The shim re-opened every descriptor with ``flags & ~O_BINARY`` and delegated to
the real ``os.open``. On a host with no text mode that is bookkeeping. On
Windows it handed the native CRT a TEXT-mode descriptor: the CRT collapsed
CRLF and stopped at the first Ctrl-Z, and a production read that had correctly
asked for binary mode observed **26 of 10240 bytes**. The simulator corrupted
the very property it was written to defend.

So the branch is now chosen by MEASUREMENT — see
:func:`measure_native_text_runtime`, which probes a disposable fixture through
the host's own low-level descriptors and never consults a platform name. On a
host that translates, ``binary_available=True`` is a **no-op** and the host
supplies both behaviours; on one that does not, the shim supplies them. Four
proofs, two per host kind, each with a non-vacuity assertion, and
:class:`TestTheRuntimeIsNotDoubleSimulated` RUNS the defective shim on an
emulated translating host and shows it reproducing the real runner's 26-byte
observation and its ``b858da80d8a5…`` digest.

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
import shutil
import sys
import tempfile
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

#: Captured at import, BEFORE any test, any fixture or any probe can patch
#: them. Every measurement below goes through THESE, so a shim installed by an
#: earlier test can never become the thing a capability probe measures.
_REAL_OPEN, _REAL_READ, _REAL_CLOSE = os.open, os.read, os.close

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


#: The union of every ``O_*`` flag bit this host publishes. Measured, because
#: the synthetic binary-mode bit injected below is passed to the host's own
#: ``open`` by production and must not ALIAS a flag the host acts on (§10).
_HOST_FLAG_BITS = 0
for _name, _value in vars(os).items():
    if _name.startswith("O_") and isinstance(_value, int):
        _HOST_FLAG_BITS |= _value


def _free_flag_bit(start: int = 0x8000) -> int:
    """The lowest bit at or above *start* that this host uses for no ``O_*``."""
    bit = start
    while bit & _HOST_FLAG_BITS:
        bit <<= 1
    return bit


#: The value injected as ``os.O_BINARY`` where the host publishes none. On
#: Windows ``_O_BINARY`` really is ``0x8000``; on a host that already spends
#: that bit the probe moves up rather than aliasing it.
SIMULATED_O_BINARY = _free_flag_bit()

#: The binary-mode bit IN FORCE for this run: the host's real ``O_BINARY``
#: where there is one, and the synthetic stand-in where there is not. Tests
#: inspect production's derived flags against THIS, so the flag assertions
#: mean the same thing on both kinds of host.
WIN_O_BINARY = getattr(os, "O_BINARY", SIMULATED_O_BINARY)

#: The disposable fixture the capability probe measures with: one CRLF to
#: collapse and one Ctrl-Z to stop at — the two behaviours M68D.2 measured on
#: the real runner of CI 37994281455.
NATIVE_PROBE_BYTES = b"A\r\nB\r\n\x1aC"

#: Probe verdicts. ``BYTE_EXACT`` and ``NATIVE_TEXT`` are measurements;
#: anything beginning ``FAILED`` is the fail-closed answer and is never
#: resolved into either of the other two (§8).
BYTE_EXACT = "BYTE_EXACT"
NATIVE_TEXT = "NATIVE_TEXT"


@dataclass(frozen=True)
class NativeTextRuntime:
    """What the HOST's own low-level descriptors were measured to do.

    Produced by :func:`measure_native_text_runtime`, never by a platform name:
    ``os.name``, ``sys.platform`` and ``platform.system()`` are labels, and
    `TestTheGovernanceControlsThemselvesAreIntact` proves this file contains
    none of them. The question that matters is behavioural — *does an unflagged
    descriptor on this host return the bytes that are on the disk?* — and a
    label cannot answer it for a future runtime, an emulator, or a mount.
    """

    #: ``BYTE_EXACT``, ``NATIVE_TEXT``, or ``FAILED: <reason>``.
    outcome: str
    #: What the unflagged descriptor actually returned.
    unflagged: "bytes | None" = None
    #: What the explicitly-binary descriptor returned, where one was asked for.
    binary: "bytes | None" = None

    @property
    def translates(self) -> bool:
        """The host itself collapses CRLF / stops at Ctrl-Z. MEASURED."""
        return self.outcome == NATIVE_TEXT

    @property
    def measured(self) -> bool:
        return self.outcome in (BYTE_EXACT, NATIVE_TEXT)


def _read_whole(path: Path, flags: int) -> bytes:
    """Everything *path* yields through the host's REAL ``os.open``/``os.read``.

    Deliberately low level and deliberately unshimmed: this is the only thing
    in the file that is allowed to answer "what does this host do", so it must
    not be able to measure a simulator.
    """
    fd = _REAL_OPEN(str(path), flags)
    try:
        out: "list[bytes]" = []
        while True:
            chunk = _REAL_READ(fd, 4096)
            if not chunk:
                break
            out.append(chunk)
        return b"".join(out)
    finally:
        os.close(fd)


def measure_native_text_runtime() -> NativeTextRuntime:
    """Measure, right now, whether unflagged descriptors here translate (§8).

    A function and not a frozen constant, so the measurement can be re-taken
    against an emulated host — which is what proves it IS a measurement rather
    than a value that happens to be right on two platforms
    (:meth:`TestTheRuntimeIsNotDoubleSimulated.test_the_probe_is_a_measurement_and_not_a_constant`).

    FAIL-CLOSED. A probe that cannot complete yields ``FAILED: ...``, which is
    neither "native" nor "byte-exact": :func:`windows_text_runtime` refuses to
    run at all on one, so an unmeasured host can never produce a pass. The
    optimistic reading — "no exception, assume POSIX" — is exactly how the
    M68D.2 shim came to strip ``O_BINARY`` on a host that has one.
    """
    root = tempfile.mkdtemp(prefix="m68d3-crt-")
    try:
        target = Path(root) / "probe.bin"
        target.write_bytes(NATIVE_PROBE_BYTES)
        if target.read_bytes() != NATIVE_PROBE_BYTES:
            return NativeTextRuntime(
                "FAILED: the probe fixture itself could not be written "
                "byte-exactly, so nothing measured through it would mean "
                "anything")
        unflagged = _read_whole(target, os.O_RDONLY)
        if unflagged == NATIVE_PROBE_BYTES:
            return NativeTextRuntime(BYTE_EXACT, unflagged=unflagged)
        # Translation observed. The host must then ALSO offer the explicit
        # binary descriptor production depends on, and that descriptor must be
        # exact — otherwise this is some third runtime M68D.2 never repaired
        # and no amount of simulation would make the result honest.
        bit = getattr(os, "O_BINARY", None)
        if bit is None:
            return NativeTextRuntime(
                "FAILED: unflagged reads translate but the host publishes no "
                "O_BINARY, so production has no binary mode to ask for",
                unflagged=unflagged)
        binary = _read_whole(target, os.O_RDONLY | bit)
        if binary != NATIVE_PROBE_BYTES:
            return NativeTextRuntime(
                "FAILED: the explicitly-binary descriptor is not byte-exact "
                f"either ({len(binary)} of {len(NATIVE_PROBE_BYTES)} bytes)",
                unflagged=unflagged, binary=binary)
        return NativeTextRuntime(NATIVE_TEXT, unflagged=unflagged,
                                 binary=binary)
    except OSError as exc:
        # EXPLICIT (§8). Not a quiet `except Exception: pass` that would hand
        # back an optimistic default.
        return NativeTextRuntime(
            f"FAILED: {type(exc).__name__} errno={getattr(exc, 'errno', None)}"
            f" winerror={getattr(exc, 'winerror', None)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


#: The host this run is on, measured once at import in its own temporary
#: directory, before any fixture exists and before anything can be patched.
NATIVE = measure_native_text_runtime()

#: The §27 report line. A bool derived from the measurement, never from a name.
NATIVE_TEXT_TRANSLATION_OBSERVED = NATIVE.translates


@dataclass
class TextRuntime:
    """What :func:`windows_text_runtime` ACTUALLY did. The non-vacuity record.

    §11 — "a test that does not actually exercise its selected branch does not
    count". A runtime that silently became a no-op on the wrong host is the
    M68D.2 defect seen from the other side, so every test below asserts which
    branch it got before it asserts anything about production.
    """

    #: The host's own C runtime supplied the text behaviour. Nothing patched.
    native: bool
    #: A shim supplied it, because the host has no text mode to borrow.
    synthetic: bool
    #: Whether ``os.O_BINARY`` was in force inside the block.
    binary_available: bool
    #: Descriptors the shim opened. Zero in native mode, by construction.
    opens: int = 0
    #: How many of those were translating descriptors.
    text_descriptors: int = 0
    #: Shim reads that CHANGED the bytes. The proof a simulation did work.
    translations: int = 0

    def assert_the_branch_is_live(self) -> None:
        """NON-VACUITY: the branch this test selected really is the one running."""
        assert self.native != self.synthetic, \
            "the runtime is neither native nor synthetic, or claims to be both"
        if self.native:
            assert NATIVE.translates, \
                "the native branch ran on a host measured as byte-exact"
            assert self.opens == 0 and self.translations == 0, \
                "the native branch installed a shim, which is double simulation"
            assert os.open is _REAL_OPEN and os.read is _REAL_READ, \
                "the native branch patched the os module"
        else:
            assert not NATIVE.translates, \
                "the synthetic branch ran on a host with its own text mode"
            assert os.open is not _REAL_OPEN, "the shim is not installed"
            assert os.read is not _REAL_READ, "the shim is not installed"
        if self.binary_available:
            assert getattr(os, "O_BINARY", None) == WIN_O_BINARY, \
                "binary mode was requested but no O_BINARY is in force"
        else:
            assert not hasattr(os, "O_BINARY"), \
                "the no-binary-mode branch left os.O_BINARY in place"
        # Either way, production must be deriving its flags from what is in
        # force — never from a copy of the derivation kept in this file.
        expected = os.O_RDONLY | (WIN_O_BINARY if self.binary_available else 0)
        assert si._source_open_flags() == expected, \
            "production is not deriving its flags from the runtime in force"

    def assert_a_translation_happened(self) -> None:
        """NON-VACUITY for a NEGATIVE control: something really was corrupted."""
        if self.synthetic:
            assert self.translations >= 1, \
                "the shim never changed a byte, so it proves nothing"
            assert self.text_descriptors >= 1, \
                "the shim never opened a translating descriptor"


@contextlib.contextmanager
def windows_text_runtime(*, binary_available: bool):
    """The Windows C runtime's TEXT-mode descriptors — natively or simulated.

    THE CENTRAL RULE (§7): **simulate only what the host does not already
    provide.** CI run 38007992655 is what happens when that rule is broken.
    The M68D.2 shim unconditionally re-opened every descriptor with
    ``flags & ~O_BINARY`` and delegated to the real ``os.open``. On Linux that
    is harmless bookkeeping. On the real Windows runner it opened an ACTUAL
    text-mode descriptor, the native CRT collapsed CRLF and stopped at Ctrl-Z,
    and a correct 10240-byte binary production read was observed as **26
    bytes** — the simulator corrupting the very property it was written to
    defend. 20 tests in :class:`TestBinaryByteIdentity` failed for that one
    reason, and none of them was a production defect.

    So the branch is chosen by MEASUREMENT (:data:`NATIVE`):

    NATIVE host (its descriptors translate on their own)
        ``binary_available=True`` is a **no-op**. Production's own
        ``_source_open_flags()`` reads the real ``os.O_BINARY`` and asks for
        binary mode; the host honours it; nothing here is allowed to
        intervene. ``binary_available=False`` removes the attribute so
        production derives a text-mode flag set and the **real** CRT does the
        corrupting. No synthetic transform in either case, so nothing is
        modelled twice.

    BYTE-EXACT host (POSIX: no text mode to borrow)
        A shim supplies both behaviours, driven by exactly ONE injected
        attribute — ``os.O_BINARY`` — from which production derives its own
        flags. Nothing here re-implements that derivation, so a pass is a
        statement about production rather than about this harness.

    Either way ``binary_available`` is the experiment: ``True`` is the
    repaired platform, ``False`` is the regression.
    """
    if not NATIVE.measured:
        # FAIL-CLOSED (§8). An unmeasured host gets no simulator and no pass.
        raise RuntimeError(
            "the native text-mode capability was not measured, so neither "
            f"branch can be entered honestly: {NATIVE.outcome}")

    if NATIVE.translates:
        mode = TextRuntime(native=True, synthetic=False,
                           binary_available=binary_available)
        had = hasattr(os, "O_BINARY")
        previous = getattr(os, "O_BINARY", None)
        try:
            if not binary_available and had:
                # The NEGATIVE control, taken natively: production derives
                # `O_RDONLY` alone and the host's own runtime translates.
                del os.O_BINARY
            yield mode
        finally:
            if had:
                os.O_BINARY = previous
            elif hasattr(os, "O_BINARY"):
                del os.O_BINARY
        return

    mode = TextRuntime(native=False, synthetic=True,
                       binary_available=binary_available)
    real_open, real_read, real_close = os.open, os.read, os.close
    text_fds: "set[int]" = set()
    had = hasattr(os, "O_BINARY")
    previous = getattr(os, "O_BINARY", None)

    def win_open(path, flags, *args, **kwargs):
        mode.opens += 1
        # Stripping is safe here and ONLY here: this branch runs on a host
        # measured to spend no `O_*` flag on this bit (`SIMULATED_O_BINARY` is
        # derived from `_HOST_FLAG_BITS`), so the bit removed is one the host
        # would ignore. On a native host this line is never reached.
        fd = real_open(path, flags & ~WIN_O_BINARY, *args, **kwargs)
        if not flags & WIN_O_BINARY:
            text_fds.add(fd)
            mode.text_descriptors += 1
        return fd

    def win_read(fd, size):
        raw = real_read(fd, size)
        if fd not in text_fds:
            return raw
        data = raw
        cut = data.find(b"\x1a")
        if cut != -1:
            data = data[:cut]
        data = data.replace(b"\r\n", b"\n")
        if data != raw:
            mode.translations += 1
        return data

    def win_close(fd):
        # DESCRIPTOR LIFECYCLE. The kernel reissues the LOWEST free descriptor
        # number, so a text fd that is merely closed and forgotten leaves its
        # number marked — and the very next open, whatever flags it asked for,
        # inherits the translation. Measured while building this repair: the
        # capability probe's unflagged read closed fd N, the explicit-binary
        # read that followed got fd N back, and the probe reported its own
        # binary descriptor as translating (4 of 8 bytes). The M68D.2 shim had
        # the same hole; on Linux its two reads happened never to collide.
        text_fds.discard(fd)
        return real_close(fd)

    try:
        # Inside the `try`, so a failure part-way through SETUP still restores
        # (§13): a half-installed simulator left behind would silently repair,
        # or silently corrupt, every unrelated test that followed it.
        if binary_available:
            os.O_BINARY = WIN_O_BINARY
        elif had:
            del os.O_BINARY
        os.open, os.read, os.close = win_open, win_read, win_close
        yield mode
    finally:
        os.open, os.read, os.close = real_open, real_read, real_close
        if had:
            os.O_BINARY = previous
        elif hasattr(os, "O_BINARY"):
            del os.O_BINARY


@dataclass
class CrtEmulatorLedger:
    """What a host-level emulator ACTUALLY did. The §7 non-vacuity record.

    :class:`TextRuntime` records the same thing one layer down, for
    :func:`windows_text_runtime`. This records it for the layer that replaces
    the **host** — and it exists because M68D.3 had no way to ask the question.
    ``entries`` moves on every use; ``installs`` moves only when a shim was
    really put in place. A measured no-op is therefore ``entries`` without
    ``installs``, which is a BEHAVIOURAL fact a future edit cannot fake by
    keeping the early-return branch and patching anyway.
    """

    #: Times the context manager was entered, whatever it then decided.
    entries: int = 0
    #: Times it installed a shim. Zero on a host that already has the
    #: behaviour, by construction.
    installs: int = 0
    #: Times it stood down because the host already provides the behaviour.
    noops: int = 0
    #: Times it refused to run because the host could not be measured.
    refusals: int = 0


#: The ledger for :func:`host_with_a_text_crt`.
CRT_EMULATOR = CrtEmulatorLedger()

#: The ledger for :func:`host_with_a_byte_exact_crt`, its §10 inverse.
BYTE_EXACT_EMULATOR = CrtEmulatorLedger()


@contextlib.contextmanager
def host_with_a_text_crt():
    """Give THIS process a host whose own descriptors translate (§14).

    Not a second simulator: it replaces the **host**, at the layer
    :func:`measure_native_text_runtime` measures, and then RE-MEASURES and
    installs whatever that measurement says. So the capability stays measured
    rather than asserted, and a Linux runner can exercise the native branch —
    and the defective shims that branch exists to forbid — for real.

    This is what reproduces CI 38007992655 off Windows. Verified: the M68D.2
    shim, run inside this block, observes ``bytes(range(256)) * 40`` as 26
    bytes with digest ``b858da80d8a5…`` — the digest the real runner reported.

    M68D.4 — THE NO-OP RULE IS RECURSIVE
    ====================================
    M68D.3 repaired :func:`windows_text_runtime` by measurement and then broke
    the real Windows runner with the SAME error one layer up. This function
    unconditionally installed its own CRT, and its open did:

        fd = host_open(path, flags & ~bit, *a, **kw)

    On Linux ``host_open`` is POSIX and ``bit`` is a bit this host spends on no
    ``O_*`` flag, so that line is bookkeeping. **On the real Windows runner
    ``host_open`` IS the native ``os.open`` and ``bit`` IS the real
    ``os.O_BINARY``**, so the emulator stripped it and the capability probe
    measured its own explicitly-binary descriptor as translating:

        FAILED: the explicitly-binary descriptor is not byte-exact either
        (4 of 8 bytes)

    The fail-closed refusal was CORRECT; the emulator was wrong. CI run
    **38160730836** on candidate ``530d2ea`` failed 9 tests, all in
    :class:`TestTheRuntimeIsNotDoubleSimulated`, for that one reason — and
    5 of the 9 were the refusal itself, raised as
    ``RuntimeError: ... was not measured``.

    So the rule of §7 — *simulate only what the host does not already
    provide* — applies to **every** layer that imitates a platform capability,
    including the one that exists so another layer's branch can be tested.
    Fixing layer 1 by measurement does not protect layer 2; layer 2 needs its
    own measured guard. This function now MEASURES FIRST and:

    the host already translates (real Windows)
        **Stands down completely.** No shim, no ``os.open``/``os.read``/
        ``os.close`` replacement, no ``_REAL_*`` replacement, and the host's
        own ``O_BINARY`` is left exactly as it is. The native proofs below then
        run against the REAL CRT — genuine PROOF A/B rather than an emulation
        of it.

    the host is byte-exact (POSIX)
        Installs the controlled synthetic CRT, exactly as before, and requires
        the re-probe to come back ``NATIVE_TEXT`` before yielding.

    the host cannot be measured
        **Refuses to enter.** Never a yielded optimistic verdict (§5).

    Reproduce the M68D.3 defect in three lines by NESTING this function — the
    inner one then runs on a host that already translates, which is what
    Windows is. :meth:`TestTheRuntimeIsNotDoubleSimulated.test_a_nested_emulator_is_a_true_noop_on_a_translating_host`
    is that reproduction, and it fails against the generation-71 body.
    """
    global _REAL_OPEN, _REAL_READ, _REAL_CLOSE
    global NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED

    CRT_EMULATOR.entries += 1
    # MEASURE FIRST (§5). Nothing is patched before the verdict is known, so
    # the measurement can never be a measurement of this function's own shim.
    before = measure_native_text_runtime()
    if not before.measured:
        # FAIL-CLOSED (§5). A host that cannot be measured gets no emulator
        # and no yielded verdict — not an optimistic "assume POSIX", which is
        # the family of mistake this whole milestone is about.
        CRT_EMULATOR.refusals += 1
        raise RuntimeError(
            "the host's own text-mode capability was not measured, so no "
            f"translating host can be presented honestly: {before.outcome}")

    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)
    if before.translates:
        # THE NO-OP BRANCH (M68D.4). The host IS the translating host this
        # function exists to provide. Imitating it here is what corrupted the
        # probe's binary descriptor on the real runner.
        CRT_EMULATOR.noops += 1
        try:
            NATIVE = before
            NATIVE_TEXT_TRANSLATION_OBSERVED = before.translates
            yield before
        finally:
            NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict
        return

    bit = getattr(os, "O_BINARY", SIMULATED_O_BINARY)
    host_open, host_read, host_close = _REAL_OPEN, _REAL_READ, _REAL_CLOSE
    crt_text_fds: "set[int]" = set()

    def crt_open(path, flags, *args, **kwargs):
        # Reached ONLY on a host measured byte-exact, so the bit removed here
        # is one this host was measured to act on in no way at all. On a
        # translating host this function is never installed.
        fd = host_open(path, flags & ~bit, *args, **kwargs)
        if not flags & bit:
            crt_text_fds.add(fd)
        return fd

    def crt_read(fd, size):
        data = host_read(fd, size)
        if fd not in crt_text_fds:
            return data
        cut = data.find(b"\x1a")
        if cut != -1:
            data = data[:cut]
        return data.replace(b"\r\n", b"\n")

    def crt_close(fd):
        crt_text_fds.discard(fd)       # see `win_close`: numbers are reissued
        return host_close(fd)

    saved = (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ, _REAL_CLOSE,
             hasattr(os, "O_BINARY"), getattr(os, "O_BINARY", None))
    try:
        CRT_EMULATOR.installs += 1
        os.open, os.read, os.close = crt_open, crt_read, crt_close
        _REAL_OPEN, _REAL_READ, _REAL_CLOSE = crt_open, crt_read, crt_close
        os.O_BINARY = bit
        # MEASURED, not asserted: the emulated host is put through the same
        # probe as a real one, and whatever it answers is what gets installed.
        NATIVE = measure_native_text_runtime()
        NATIVE_TEXT_TRANSLATION_OBSERVED = NATIVE.translates
        if not NATIVE.translates:
            # The synthetic CRT did not produce the host it promised. Refusing
            # is the only honest answer; yielding would hand every native
            # proof below a host that is not the one it says it is.
            raise RuntimeError(
                "the synthetic CRT did not produce a host measured as "
                f"translating: {NATIVE.outcome}")
        yield NATIVE
    finally:
        (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ, _REAL_CLOSE,
         had, previous) = saved
        NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict
        if had:
            os.O_BINARY = previous
        elif hasattr(os, "O_BINARY"):
            del os.O_BINARY


@contextlib.contextmanager
def host_with_a_byte_exact_crt():
    """Present a BYTE-EXACT host to the same probe — the §10 inverse control.

    WHY THIS EXISTS.
    :meth:`TestTheRuntimeIsNotDoubleSimulated.test_the_probe_is_a_measurement_and_not_a_constant`
    proves :func:`measure_native_text_runtime` MEASURES rather than returns a
    value that happens to be right on two platforms. It did that by contrasting
    the real host against :func:`host_with_a_text_crt`. Once that function
    correctly becomes a no-op on a translating host, **both** readings on real
    Windows are ``NATIVE_TEXT`` and the contrast is gone — the test would still
    pass while proving nothing. So the contrast is restored from the other
    side: this presents a host whose descriptors are byte-exact, and the SAME
    probe must return ``BYTE_EXACT`` for it.

    Exactly one of the two fixtures does real work on any given host, and the
    other is a measured no-op: on POSIX the text fixture installs and this one
    stands down; on Windows this one installs and the text fixture stands down.
    Either way the probe answers ``NATIVE_TEXT`` for one and ``BYTE_EXACT``
    for the other **in the same process**, which a constant cannot do.

    DELIBERATELY NOT A GENERAL-PURPOSE OS EMULATOR (§10). It patches ``os.open``
    and nothing else, it is used only to feed the capability probe, and
    :func:`windows_text_runtime` is never entered inside it. One meaningful
    negative control and a measured change of verdict — not a framework.

    The no-op rule is recursive here too: on a host already measured
    byte-exact this installs nothing.
    """
    global _REAL_OPEN
    global NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED

    BYTE_EXACT_EMULATOR.entries += 1
    before = measure_native_text_runtime()
    if not before.measured:
        BYTE_EXACT_EMULATOR.refusals += 1
        raise RuntimeError(
            "the host's own text-mode capability was not measured, so no "
            f"byte-exact host can be presented honestly: {before.outcome}")

    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)
    if not before.translates:
        # THE NO-OP BRANCH. This host's descriptors are already byte-exact.
        BYTE_EXACT_EMULATOR.noops += 1
        try:
            NATIVE = before
            NATIVE_TEXT_TRANSLATION_OBSERVED = before.translates
            yield before
        finally:
            NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict
        return

    # A translating host that PASSED the probe necessarily publishes an
    # `O_BINARY` whose descriptors were measured byte-exact — the probe
    # returns `FAILED` otherwise, so this attribute access cannot be reached
    # on a host that lacks it.
    bit = os.O_BINARY
    host_open = _REAL_OPEN

    def exact_open(path, flags, *args, **kwargs):
        # Every descriptor becomes a BINARY descriptor, so the host's own text
        # translation never engages. Built out of the capability the probe just
        # measured as exact, which is why this is a presentation of a real
        # behaviour rather than a synthetic transform.
        return host_open(path, flags | bit, *args, **kwargs)

    saved = (os.open, _REAL_OPEN)
    try:
        BYTE_EXACT_EMULATOR.installs += 1
        os.open = exact_open
        _REAL_OPEN = exact_open
        NATIVE = measure_native_text_runtime()
        NATIVE_TEXT_TRANSLATION_OBSERVED = NATIVE.translates
        if NATIVE.outcome != BYTE_EXACT:
            raise RuntimeError(
                "the byte-exact host fixture did not produce a host measured "
                f"as byte-exact: {NATIVE.outcome}")
        yield NATIVE
    finally:
        os.open, _REAL_OPEN = saved
        NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict


@contextlib.contextmanager
def defective_shim(kind: str):
    """The M68D.2 shim and its neighbours, for the absent-controls of §14.

    Each *kind* is one way a future edit breaks the central rule. None of them
    is reachable from :func:`windows_text_runtime`; they exist so the detector
    that forbids them has something real to catch, because "a textual grep by
    itself is not enough to prove behaviour".
    """
    bit = getattr(os, "O_BINARY", SIMULATED_O_BINARY)
    real_open, real_read = os.open, os.read

    def strip_open(path, flags, *args, **kwargs):
        # THE M68D.2 DEFECT, verbatim: the binary bit is removed before the
        # host sees it, so a native CRT hands back a TEXT descriptor. Nothing
        # else is done — the corruption comes from the HOST, which is the
        # whole point.
        return real_open(path, flags & ~bit, *args, **kwargs)

    def double_read(fd, size):
        # Transform bytes the host has ALREADY transformed.
        data = real_read(fd, size)
        cut = data.find(b"\x1a")
        if cut != -1:
            data = data[:cut]
        return data.replace(b"\r\n", b"\n")

    try:
        if kind == "strips_the_binary_flag":
            os.open = strip_open
        elif kind == "double_transforms":
            os.read = double_read
        elif kind == "both_translations_active":
            os.open, os.read = strip_open, double_read
        else:  # pragma: no cover - a typo must never be a silent pass
            raise AssertionError(f"unknown defective shim: {kind!r}")
        yield
    finally:
        os.open, os.read = real_open, real_read


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


def _test_body(name: str) -> str:
    """The STRINGS-DROPPED body of the test function called *name*, in THIS file.

    Why per-test and not file-wide: many assertions here are written the same
    way in several places — ``assert snap.payload() == body`` appears in half a
    dozen tests. A file-wide scan for one of them stays green while the single
    occurrence that carries the invariant is deleted, which is how four
    assertion-deleting mutations survived the first run of the M68D.3 campaign.
    Raises rather than returning empty: a renamed test must break the control
    that guards it, not silently satisfy it.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return _no_strings_of(ast.unparse(node))
    raise AssertionError(f"no test named {name!r} in this file")


def _handler_records_the_failure(handler: ast.ExceptHandler) -> bool:
    """Does *handler* leave a trace a later assertion can see? §12.

    Four ways count. Three are the original ones — appending to a witness
    list, re-raising, and failing through ``pytest``. The fourth is M68D.3's
    capability probe (§8), which converts an ``OSError`` into an explicit
    ``FAILED``-tagged :class:`NativeTextRuntime`. That is the opposite of
    swallowing: :func:`windows_text_runtime` REFUSES to enter either branch on
    such a verdict, so no test can pass through an unmeasured host. The
    exemption is deliberately narrow — the returned verdict must carry the
    ``FAILED`` tag, so an ``except`` that hands back an optimistic capability
    is still reported.
    """
    for statement in handler.body:
        rendered = ast.unparse(statement)
        if ("append" in rendered or "raise" in rendered
                or "pytest" in rendered):
            return True
        if (isinstance(statement, ast.Return)
                and "NativeTextRuntime" in rendered
                and "FAILED" in rendered):
            return True
    return False


def _stage(tmp_path: Path, name: str, body: bytes) -> Path:
    """Write *body* BYTE-EXPLICITLY. Never a translating handle (§6)."""
    target = tmp_path / f"{name}.bin"
    target.write_bytes(body)
    assert target.read_bytes() == body, "the fixture itself was translated"
    return target


# ══ 1 · GROUP A — THE BYTES ON DISK ARE THE BYTES CAPTURED ══════════════════

class TestBinaryByteIdentity:
    """§5 — SOURCE_BYTES_ON_DISK == BYTES_CAPTURED_BY_SNAPSHOT.

    PROOF A on a host whose descriptors translate natively, PROOF C on one
    whose do not (§11). The assertions are identical; only who supplies the
    text-mode runtime differs, and :meth:`TextRuntime.assert_the_branch_is_live`
    records which it was so neither result can be reported as the other.
    """

    def test_production_asks_the_platform_for_binary_mode(self):
        """The flag set, derived by production, with a binary mode in force.

        Calls ``_source_open_flags()`` itself rather than re-deriving it, so a
        future edit that stops consulting ``os.O_BINARY`` fails here.
        """
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        """With no binary mode in force the derivation must be a bare no-op."""
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
            assert si._source_open_flags() == os.O_RDONLY, \
                "the repair changed the no-text-mode flag set"

    @pytest.mark.parametrize("name", list(BYTE_FIXTURES))
    def test_every_fixture_survives_a_text_translating_runtime(self, tmp_path,
                                                               name):
        """CROSS_PLATFORM_INVARIANT — payload, size and digest, all four ways."""
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        """The ``capture_bytes=False`` path, which failed on Ctrl-Z for real.

        ``stream()`` duplicates the HELD descriptor, so on a native host it
        inherits that descriptor's binary mode and this covers the served
        bytes too — the path that came back 26 bytes long on the real runner.
        """
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        real runner of CI 37994281455 digested 26 of them and reported success,
        and the M68D.2 SIMULATOR reproduced the same 26 on the real runner of
        CI 38007992655 — from the other direction (§2).
        """
        body = bytes(range(256)) * 40
        target = _stage(tmp_path, "ctrlz", body)
        assert body.index(b"\x1a") == 26, "the fixture lost its Ctrl-Z"
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.size_bytes == 10240, \
                    f"the read stopped at a Ctrl-Z: {snap.size_bytes} bytes"
                assert snap.sha256 == hashlib.sha256(body).hexdigest()
                assert snap.size_bytes != 26, \
                    "a 26-byte read was accepted as a complete 10240-byte source"

    def test_a_crlf_source_is_never_rejected_for_being_crlf(self, tmp_path):
        """POSITIVE CONTROL (§11) — and a second, quieter Group A symptom.

        Unrepaired, ``total`` came back SHORTER than ``st_size`` for every CRLF
        file, so the metadata witness declared it unstable: on Windows, every
        CRLF source was ALSO denied an identity. The three reported Group A
        failures masked this by asserting the payload first.
        """
        target = _stage(tmp_path, "crlf_only", b"line one\r\nline two\r\n")
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
            assert si.digest_file(crlf) != si.digest_file(lf), \
                "digest_file normalises newlines"
            assert si.digest_file(crlf) == digest_bytes(b"a\r\nb\r\n")
            assert digest_bytes(b"a\r\nb\r\n") != digest_bytes(b"a\nb\n")

    def test_a_cas_precondition_taken_under_translation_still_matches(self,
                                                                      tmp_path):
        """END TO END — observe, then write against the digest just observed."""
        body = b"first\r\nsecond\r\n"
        target = _stage(tmp_path, "cas", body)
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target) as snap:
                precondition = identify_snapshot(snap).sha256
            assert precondition == digest_bytes(body)
            receipt = cas_write_text(target, "third\r\nfourth\r\n",
                                     expected_sha256=precondition)
        assert receipt.status is WriteStatus.APPLIED, \
            f"a coherent precondition was refused: {receipt.status}"
        assert target.read_bytes() == b"third\r\nfourth\r\n", \
            "the CAS write rewrote the caller's line endings"


class TestTheTextRuntimeReallyTranslates:
    """PROOF B / PROOF D — the NEGATIVE controls (§11, §12).

    Zero structural matches is never successful evidence. Without a measured
    demonstration that an unrepaired source open DOES corrupt the observation,
    every assertion in :class:`TestBinaryByteIdentity` is satisfied by a
    runtime that does nothing at all — which is exactly the failure mode
    CI 38007992655 inverted.

    ``binary_available=False`` is the regression itself: no binary mode is in
    force, so production derives a translating flag set. On a native host the
    host's own CRT corrupts the read (PROOF B); on a byte-exact host the shim
    does (PROOF D). Same assertion, both ways.

    WHAT THIS IS NOT (§9). A negative control is a statement about an
    UNREPAIRED open, never a licence for ``source_snapshot`` to return
    translated bytes. :class:`TestTheRuntimeIsNotDoubleSimulated` pins that
    reading: no negative control may be entered with a binary mode in force.
    """

    #: The fixtures that have something for a text runtime to do. ``lone_cr``,
    #: ``lf`` and friends are deliberately absent: they are the control that
    #: proves the translation is surgical, asserted separately below.
    TRANSLATABLE = [
        "crlf", "mixed_lf_crlf", "utf8_unicode", "nul_bytes", "ctrl_z_early",
        "binary_all_bytes", "crlf_on_chunk_boundary", "multibyte_on_boundary",
    ]

    @pytest.mark.parametrize("name", TRANSLATABLE)
    def test_without_a_binary_mode_the_observation_really_is_corrupted(
            self, tmp_path, name):
        """NON-VACUITY. If this passed clean, the suite above would prove nothing."""
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target) as snap:
                observed = snap.payload()
                assert observed != body, \
                    "the runtime did not translate, so it proves nothing"
                assert snap.size_bytes < len(body), \
                    "the runtime did not shorten the read"
                assert snap.sha256 != hashlib.sha256(body).hexdigest()
            runtime.assert_a_translation_happened()
        # And the file on disk was never touched by any of it.
        assert target.read_bytes() == body, \
            "the negative control mutated the fixture"

    @pytest.mark.parametrize("name", TRANSLATABLE)
    def test_the_unrepaired_digest_does_not_describe_the_disk(self, tmp_path,
                                                             name):
        """The stream-mode half of the negative control.

        Only the DIGEST is asserted. ``stream()`` hands back a duplicate of the
        held descriptor and is read through ``io``, not through ``os.read``, so
        a synthetic shim cannot reach it — and the digest is the authority
        anyway: it is what ``identify_snapshot`` issues as a precondition.
        """
        body = BYTE_FIXTURES[name]
        target = _stage(tmp_path, name, body)
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.sha256 != hashlib.sha256(body).hexdigest(), \
                    "the unrepaired stream digest still described the disk"
                assert snap.size_bytes < len(body)
            runtime.assert_a_translation_happened()

    def test_the_ctrl_z_fixture_really_is_truncated_at_twenty_six(self,
                                                                  tmp_path):
        """CTRL_Z_PROOF, negative half. The measured number, both ways.

        10240 bytes in, a Ctrl-Z at offset 26, and ``sha256`` of the first 26
        bytes is ``b858da80d8a5…`` — the digest the real runner reported in
        CI 37994281455 *and* in CI 38007992655.
        """
        body = bytes(range(256)) * 40
        target = _stage(tmp_path, "ctrlz_neg", body)
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.size_bytes == 26, \
                    f"the text runtime did not stop at the Ctrl-Z: {snap.size_bytes}"
                assert snap.sha256 == hashlib.sha256(body[:26]).hexdigest()
                assert snap.sha256.startswith("b858da80d8a5"), \
                    "this is not the digest the real runner measured"
            runtime.assert_a_translation_happened()

    def test_a_crlf_source_is_the_crlf_proof_in_both_directions(self, tmp_path):
        """CRLF_PROOF — collapsed without a binary mode, intact with one."""
        body = b"line one\r\nline two\r\n"
        target = _stage(tmp_path, "crlf_both", body)
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target) as snap:
                assert snap.payload() == b"line one\nline two\n", \
                    "the CRLF was not collapsed by the text runtime"
            runtime.assert_a_translation_happened()
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
            with source_snapshot(target) as snap:
                assert snap.payload() == body, "the repaired read lost the CRLF"

    def test_the_untranslatable_fixtures_are_unaffected_either_way(self, tmp_path):
        """The runtime must be surgical: no \\r\\n and no Ctrl-Z, no change."""
        for name in ("lf", "lone_cr", "cr_at_end", "empty", "bare_lf",
                     "no_final_newline"):
            body = BYTE_FIXTURES[name]
            target = _stage(tmp_path, f"quiet_{name}", body)
            with windows_text_runtime(binary_available=False) as runtime:
                runtime.assert_the_branch_is_live()
                with source_snapshot(target) as snap:
                    assert snap.payload() == body, \
                        f"the runtime corrupted {name}, which has nothing to translate"

    def test_the_runtime_leaves_the_os_module_as_it_found_it(self):
        """A leaked ``os.O_BINARY`` would silently repair every later test."""
        before = hasattr(os, "O_BINARY")
        value = getattr(os, "O_BINARY", None)
        with windows_text_runtime(binary_available=True):
            assert hasattr(os, "O_BINARY")
        assert hasattr(os, "O_BINARY") is before, "os.O_BINARY leaked"
        assert getattr(os, "O_BINARY", None) == value, "os.O_BINARY changed value"
        with windows_text_runtime(binary_available=False):
            assert not hasattr(os, "O_BINARY")
        assert hasattr(os, "O_BINARY") is before, "os.O_BINARY was lost"
        assert getattr(os, "O_BINARY", None) == value, "os.O_BINARY changed value"
        # And `os.open`/`os.read` themselves are back, or every test after this
        # one would be reading through a simulator without knowing it.
        assert os.open is _REAL_OPEN, "os.open was left patched"
        assert os.read is _REAL_READ, "os.read was left patched"


class TestTheRuntimeIsNotDoubleSimulated:
    """§14 — the ABSENT CONTROLS for the failure that produced CI 38007992655.

    THE DEFECT. The M68D.2 shim replaced ``os.open`` unconditionally and
    re-opened every descriptor with ``flags & ~O_BINARY`` before delegating to
    the real one. On a host with no text mode that is bookkeeping. On the real
    Windows runner it handed the native CRT a TEXT-mode descriptor, the CRT
    collapsed CRLF and stopped at the first Ctrl-Z, and a production read that
    had explicitly asked for binary mode — correctly, and verified correct by
    the same runner — observed 26 of 10240 bytes.

    THE RULE. Simulate only what the host does not already provide (§7).

    Every test here measures rather than greps: :func:`host_with_a_text_crt`
    installs a real translating runtime at the layer the probe measures, so
    this Linux host can exercise the native branch, and each defective shim is
    RUN and shown to break something a passing suite must not tolerate.
    """

    FIXTURE = bytes(range(256)) * 40

    def test_the_probe_is_a_measurement_and_not_a_constant(self, tmp_path):
        """§8 — behaviour decides, never a platform name.

        The same probe, on the same process, returns different answers for
        different hosts. A constant — or a name check — could not.

        M68D.4 — BOTH HOSTS ARE NOW PRESENTED (§10). The original contrast was
        "the real host" against :func:`host_with_a_text_crt`. That was a real
        contrast on POSIX, where the real host is byte-exact. On real Windows
        it is not: the host already translates, the emulator correctly stands
        down, and BOTH readings are ``NATIVE_TEXT`` — the test would keep
        passing while distinguishing nothing. Weakening or deleting it would
        lose the only proof that the verdict is measured, so the missing half
        is supplied instead: :func:`host_with_a_byte_exact_crt` presents the
        inverse capability to the SAME function, and the two verdicts must
        differ. On either platform exactly one fixture does real work and the
        other stands down, and both facts are asserted from the ledgers.
        """
        del tmp_path
        here = measure_native_text_runtime()
        assert here.measured, f"the probe could not measure this host: {here.outcome}"
        installs = (CRT_EMULATOR.installs, BYTE_EXACT_EMULATOR.installs)
        noops = (CRT_EMULATOR.noops, BYTE_EXACT_EMULATOR.noops)
        with host_with_a_text_crt() as emulated:
            assert emulated.outcome == NATIVE_TEXT, \
                f"the probe did not see the emulated CRT: {emulated.outcome}"
            assert emulated.translates is True
            assert emulated.unflagged != NATIVE_PROBE_BYTES, \
                "the probe recorded an unflagged read it did not actually see"
            assert emulated.binary == NATIVE_PROBE_BYTES, \
                "the probe did not verify the explicit-binary descriptor"
        with host_with_a_byte_exact_crt() as exact:
            assert exact.outcome == BYTE_EXACT, \
                f"the probe did not see the byte-exact host: {exact.outcome}"
            assert exact.translates is False and exact.measured is True
            assert exact.unflagged == NATIVE_PROBE_BYTES, (
                "the probe called a host byte-exact without reading the "
                "bytes back from it")
        # THE CONTRAST, independent of which platform this is.
        assert emulated.outcome != exact.outcome, (
            "the SAME probe returned the SAME verdict for a translating host "
            "and a byte-exact one, so it is measuring neither")
        assert (emulated.outcome, exact.outcome) == (NATIVE_TEXT, BYTE_EXACT)
        # NON-VACUITY FOR THE CONTRAST: a verdict that differs because one
        # fixture quietly did nothing on BOTH sides would prove nothing, so
        # exactly one install and exactly one stand-down are required.
        installed = ((CRT_EMULATOR.installs - installs[0])
                     + (BYTE_EXACT_EMULATOR.installs - installs[1]))
        stood_down = ((CRT_EMULATOR.noops - noops[0])
                      + (BYTE_EXACT_EMULATOR.noops - noops[1]))
        assert installed == 1, (
            f"{installed} of the two capability fixtures presented a host; "
            "exactly one must, because the other is the host already")
        assert stood_down == 1, (
            f"{stood_down} of the two capability fixtures stood down; exactly "
            "one must, or the recursive no-op rule is not exercised here")
        after = measure_native_text_runtime()
        assert after.outcome == here.outcome, \
            "the emulated host leaked into the real measurement"

    def test_a_probe_that_cannot_run_reports_failure_and_not_a_capability(
            self, monkeypatch):
        """§8 — an OSError during measurement must not become an answer.

        FOUND BY THE M68D.3 CAMPAIGN (``E_probe_failure_becomes_byte_exact``).
        Asserting the refusal from a hand-built ``FAILED`` verdict left the
        handler that PRODUCES one untested, so replacing it with an optimistic
        ``BYTE_EXACT`` survived.
        """
        def refuse(path, flags, *args, **kwargs):
            raise OSError(13, "injected EACCES")

        monkeypatch.setattr(os, "open", refuse)
        monkeypatch.setattr(sys.modules[__name__], "_REAL_OPEN", refuse)
        verdict = measure_native_text_runtime()
        assert verdict.outcome.startswith("FAILED"), (
            "a probe that could not open its own fixture reported "
            f"{verdict.outcome}")
        assert verdict.measured is False and verdict.translates is False
        # `OSError(13, ...)` is raised as its `PermissionError` subclass, so
        # the verdict records the CONCRETE type and the errno rather than the
        # name of the clause that caught it.
        assert "PermissionError" in verdict.outcome, \
            "the fail-closed verdict does not name what was raised"
        assert "errno=13" in verdict.outcome, \
            "the fail-closed verdict does not carry the errno"

    def test_a_host_whose_binary_mode_is_also_broken_is_refused(self):
        """§8 — translation observed is not enough; binary must be EXACT.

        FOUND BY THE M68D.3 CAMPAIGN (``E_probe_skips_the_binary_descriptor_check``).
        A host that translates even with ``O_BINARY`` set is not the runtime
        M68D.2 repaired, and simulating against it would produce a result about
        the harness. It must be refused, not accommodated.
        """
        with host_with_a_text_crt() as healthy:
            assert healthy.outcome == NATIVE_TEXT, "the emulated host is not sane"
            # Now break the binary descriptor too: translate unconditionally.
            broken_open, broken_read = os.open, os.read
            monkey = sys.modules[__name__]
            saved = (monkey._REAL_OPEN, monkey._REAL_READ, os.open, os.read)
            try:
                def always_text_read(fd, size):
                    data = broken_read(fd, size)
                    cut = data.find(b"\x1a")
                    if cut != -1:
                        data = data[:cut]
                    return data.replace(b"\r\n", b"\n")

                def always_text_open(path, flags, *a, **k):
                    return broken_open(path, flags & ~WIN_O_BINARY, *a, **k)

                monkey._REAL_OPEN = os.open = always_text_open
                monkey._REAL_READ = os.read = always_text_read
                verdict = measure_native_text_runtime()
            finally:
                (monkey._REAL_OPEN, monkey._REAL_READ,
                 os.open, os.read) = saved
            assert verdict.outcome.startswith("FAILED"), (
                "a host whose explicit-binary descriptor also translates was "
                f"reported as {verdict.outcome}")
            assert verdict.translates is False and verdict.measured is False
            assert "binary" in verdict.outcome, \
                "the verdict does not name the binary descriptor as the problem"

    def test_a_host_that_cannot_write_the_fixture_exactly_is_refused(self,
                                                                     monkeypatch):
        """§8 — an unusable fixture invalidates everything measured through it.

        FOUND BY THE M68D.3 CAMPAIGN (``E_probe_fixture_is_not_verified``).
        """
        real_write_bytes = Path.write_bytes

        def translating_write(self, data):
            return real_write_bytes(self, data.replace(b"\r\n", b"\n"))

        monkeypatch.setattr(Path, "write_bytes", translating_write)
        verdict = measure_native_text_runtime()
        assert verdict.outcome.startswith("FAILED"), (
            "a host whose write path translated the probe fixture was "
            f"reported as {verdict.outcome}")
        assert "fixture" in verdict.outcome
        assert verdict.measured is False

    def test_a_reissued_descriptor_number_does_not_inherit_translation(self,
                                                                       tmp_path):
        """The descriptor-lifecycle hole, on the SYNTHETIC branch.

        FOUND BY THE M68D.3 CAMPAIGN (``F_leak_the_text_descriptor_marking``) —
        and MEASURED once for real while building this repair: the capability
        probe's unflagged read closed fd N, the explicit-binary read that
        followed was given fd N back, and the probe reported its own binary
        descriptor as translating (4 of 8 bytes). The kernel reissues the
        lowest free number, so forgetting to unmark a closed descriptor
        corrupts whatever opens next, whatever flags it asked for.

        Staged by opening a TEXT descriptor and a BINARY one in sequence inside
        one runtime, which is exactly the sequence the probe performs.
        """
        body = b"reuse\r\nme\r\n" + b"q" * 32
        target = _stage(tmp_path, "reuse", body)
        if NATIVE.translates:
            # The native branch keeps no marking of its own; the host owns the
            # descriptor's mode and a reissued number comes back clean.
            observed = _read_whole(target, os.O_RDONLY)
            assert observed != body, "NON-VACUITY: this host does not translate"
            assert _read_whole(target, os.O_RDONLY | WIN_O_BINARY) == body, \
                "a reissued descriptor number inherited the host's text mode"
            return
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
            # A text descriptor first, so its number is marked and then freed.
            fd = os.open(str(target), os.O_RDONLY)
            try:
                first = os.read(fd, len(body))
            finally:
                os.close(fd)
            assert first != body, \
                "NON-VACUITY: the shim did not translate the text descriptor"
            assert runtime.text_descriptors >= 1
            # Now the binary read, which will very likely be given fd back.
            with source_snapshot(target) as snap:
                assert snap.payload() == body, (
                    "a reissued descriptor number inherited the previous "
                    "descriptor's text marking")
                assert snap.size_bytes == len(body)

    def test_the_native_branch_keeps_the_production_read_byte_exact(self,
                                                                    tmp_path):
        """PROOF A, exercised off Windows. THE regression test for the 20.

        On a host whose descriptors translate, ``binary_available=True`` must
        be a NO-OP: production's own flags select binary mode and the host
        honours them. 10240 bytes in, 10240 bytes out.
        """
        target = _stage(tmp_path, "native_pos", self.FIXTURE)
        with host_with_a_text_crt():
            with windows_text_runtime(binary_available=True) as runtime:
                assert runtime.native is True and runtime.synthetic is False, \
                    "a shim was installed on a host that has its own text mode"
                runtime.assert_the_branch_is_live()
                with source_snapshot(target) as snap:
                    assert snap.size_bytes == 10240, \
                        f"the native branch truncated the read: {snap.size_bytes}"
                    assert snap.payload() == self.FIXTURE
                    assert snap.sha256 == hashlib.sha256(self.FIXTURE).hexdigest()
                    assert snap.stable is True
                with source_snapshot(target, capture_bytes=False) as snap:
                    assert snap.size_bytes == 10240
                    assert snap.sha256 == hashlib.sha256(self.FIXTURE).hexdigest()

    def test_the_native_branch_still_shows_the_text_mode_behaviour(self,
                                                                   tmp_path):
        """PROOF B, exercised off Windows — and its NON-VACUITY.

        The negative control on a native host must come from the HOST, not from
        a transform layered on top of it. So the shim counters must stay at
        zero while the observation is still corrupted.
        """
        target = _stage(tmp_path, "native_neg", self.FIXTURE)
        with host_with_a_text_crt():
            with windows_text_runtime(binary_available=False) as runtime:
                runtime.assert_the_branch_is_live()
                with source_snapshot(target) as snap:
                    assert snap.size_bytes == 26, \
                        f"the native CRT did not truncate: {snap.size_bytes}"
                    assert snap.sha256.startswith("b858da80d8a5")
                assert runtime.translations == 0 and runtime.opens == 0, \
                    "the native negative control layered a synthetic transform"

    def test_a_shim_that_strips_the_binary_flag_is_caught(self, tmp_path):
        """NON-VACUITY FOR THIS WHOLE CLASS — the M68D.2 defect, run and caught.

        This is CI 38007992655 reproduced off Windows: the exact shim, on a
        host that translates natively, observing 26 bytes and the digest
        ``b858da80d8a5…`` that the real runner printed.
        """
        target = _stage(tmp_path, "defect", self.FIXTURE)
        with host_with_a_text_crt():
            with defective_shim("strips_the_binary_flag"):
                with source_snapshot(target, capture_bytes=False) as snap:
                    assert snap.size_bytes == 26, (
                        "the defective shim did NOT corrupt the read, so this "
                        "detector proves nothing")
                    assert snap.sha256.startswith("b858da80d8a5"), \
                        "this is not the digest CI 38007992655 reported"
            # The repaired runtime, same host, same fixture, same block.
            with windows_text_runtime(binary_available=True):
                with source_snapshot(target, capture_bytes=False) as snap:
                    assert snap.size_bytes == 10240, \
                        "the repaired runtime corrupts what the defect corrupts"

    @pytest.mark.parametrize("kind", ["double_transforms",
                                      "both_translations_active"])
    def test_double_modelling_the_host_is_caught(self, tmp_path, kind):
        """A synthetic transform on bytes the host already transformed."""
        body = b"keep\r\nthis\r\n" + b"z" * 64
        target = _stage(tmp_path, f"double_{kind}", body)
        with host_with_a_text_crt():
            with defective_shim(kind):
                with source_snapshot(target) as snap:
                    assert snap.payload() != body, (
                        f"the {kind} shim changed nothing, so this detector "
                        "proves nothing")
            with windows_text_runtime(binary_available=True):
                with source_snapshot(target) as snap:
                    assert snap.payload() == body, \
                        "the repaired runtime double-models the host"

    def test_a_leaked_patch_is_caught_by_the_next_measurement(self, tmp_path):
        """A shim left installed must not survive into an unrelated test."""
        target = _stage(tmp_path, "leak", self.FIXTURE)
        with host_with_a_text_crt():
            with defective_shim("strips_the_binary_flag"):
                leaked = os.open
            assert os.open is not leaked, "the defective shim leaked os.open"
            with source_snapshot(target, capture_bytes=False) as snap:
                assert snap.size_bytes == 10240, \
                    "a leaked shim is still corrupting reads after its block"
        assert os.open is _REAL_OPEN, "os.open was left patched"
        assert os.read is _REAL_READ, "os.read was left patched"

    def test_the_runtime_restores_itself_through_an_exception(self, tmp_path):
        """§13 — restoration on EVERY exit, not just the clean one."""
        sentinel = RuntimeError("staged")
        for binary in (True, False):
            for host in (contextlib.nullcontext, host_with_a_text_crt):
                with host():
                    before = (os.open, os.read, hasattr(os, "O_BINARY"),
                              getattr(os, "O_BINARY", None))
                    with pytest.raises(RuntimeError) as caught:
                        with windows_text_runtime(binary_available=binary):
                            raise sentinel
                    assert caught.value is sentinel
                    assert (os.open, os.read, hasattr(os, "O_BINARY"),
                            getattr(os, "O_BINARY", None)) == before, \
                        "an exception inside the runtime leaked global state"

    def test_a_nested_runtime_unwinds_in_order(self):
        """A nested block must restore the OUTER runtime, not the pristine one."""
        with windows_text_runtime(binary_available=True):
            outer = (os.open, os.read, getattr(os, "O_BINARY", None))
            with windows_text_runtime(binary_available=False):
                pass
            assert (os.open, os.read, getattr(os, "O_BINARY", None)) == outer, \
                "the inner runtime did not restore the outer one"
        assert os.open is _REAL_OPEN and os.read is _REAL_READ

    @staticmethod
    def _lowest_free_descriptor(tmp_path) -> int:
        """The lowest descriptor number this process can be given right now.

        A portable descriptor-leak detector: the kernel hands back the lowest
        free number, so if a context manager leaked descriptors this rises.
        Avoids ``/proc/self/fd``, which does not exist on every host this
        suite has to run on, and avoids naming a platform to decide that.
        """
        probe = tmp_path / "fd-probe"
        probe.write_bytes(b"x")
        fd = _REAL_OPEN(str(probe), os.O_RDONLY)
        try:
            return fd
        finally:
            os.close(fd)

    def test_a_nested_emulator_is_a_true_noop_on_a_translating_host(self,
                                                                    tmp_path):
        """§7 — THE M68D.4 REGRESSION. The no-op rule is RECURSIVE.

        This is the test that fails against the generation-71 body of
        :func:`host_with_a_text_crt` and passes against the repair, on BOTH
        platforms. It is the local reproduction of CI run **38160730836**.

        M68D.3 taught :func:`windows_text_runtime` to stand down on a host that
        already translates, and then shipped a HOST-level emulator that did
        not. On Linux that emulator's ``flags & ~bit`` is bookkeeping against a
        free bit. On real Windows it stripped the native ``os.O_BINARY``, so
        the capability probe measured its own explicitly-binary descriptor as
        translating and reported ``FAILED: the explicitly-binary descriptor is
        not byte-exact either (4 of 8 bytes)``. Nine tests died on that one
        line, five of them on the fail-closed refusal it correctly triggered.

        NESTING is what reproduces it off Windows: the inner emulator then runs
        on a host that already translates, which is exactly what Windows is.
        Against generation 71 the inner verdict here is that ``FAILED`` string
        verbatim; against the repair it is ``NATIVE_TEXT`` and nothing is
        patched twice.

        The same nesting is also a REAL case on Windows, where the OUTER
        emulator is itself the no-op and the host supplies every behaviour,
        so this runs unskipped on both and means something different on each.
        """
        target = _stage(tmp_path, "nested_noop", self.FIXTURE)
        # A fixture no IDEMPOTENT transform can hide in: one pass of
        # CRLF -> LF leaves `b"A\\r\\nB"`, two passes leave `b"A\\nB"`. This is
        # what separates "the host translated once" from "something translated
        # on top of the host", which the probe fixture cannot show because
        # collapsing it twice gives the same answer as collapsing it once.
        witness = _stage(tmp_path, "nested_once", b"A\r\r\nB")

        # ── 1 · the pristine runtime, recorded before anything is entered ──
        pristine = (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                    _REAL_CLOSE, NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED,
                    hasattr(os, "O_BINARY"), getattr(os, "O_BINARY", None))
        pristine_native = NATIVE
        free_fd = self._lowest_free_descriptor(tmp_path)
        entries, installs = CRT_EMULATOR.entries, CRT_EMULATOR.installs

        # ── 2 · the OUTER translating host ──────────────────────────────────
        with host_with_a_text_crt() as outer:
            # ── 3 · it really does present a translating host ───────────────
            assert outer.outcome == NATIVE_TEXT, \
                f"the outer layer is not a translating host: {outer.outcome}"
            assert outer.translates is True and outer.measured is True
            assert outer.unflagged == b"A\nB\n", (
                "the outer host did not collapse the probe's CRLF and stop at "
                f"its Ctrl-Z: {outer.unflagged!r}")
            assert outer.binary == NATIVE_PROBE_BYTES, (
                "the outer host's explicitly-binary descriptor is not "
                f"byte-exact: {outer.binary!r}")
            assert NATIVE_TEXT_TRANSLATION_OBSERVED is True
            outer_native = NATIVE
            assert _read_whole(witness, os.O_RDONLY) == b"A\r\nB", (
                "NON-VACUITY: the outer host is not translating exactly once "
                f"before the inner layer is entered: "
                f"{_read_whole(witness, os.O_RDONLY)!r}")
            # Precisely the state the inner layer must leave untouched.
            outer_state = (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                           _REAL_CLOSE, os.O_BINARY)
            outer_installs, outer_noops = (CRT_EMULATOR.installs,
                                           CRT_EMULATOR.noops)

            # ── 4 · a SECOND emulator, on a host that already translates ────
            with host_with_a_text_crt() as inner:
                # ── 5 · a TRUE no-op: measured, and nothing installed ───────
                assert inner.outcome == NATIVE_TEXT, (
                    "the inner emulator re-modelled a host that already "
                    f"translates: {inner.outcome}")
                assert inner.measured is True and inner.translates is True
                assert CRT_EMULATOR.installs == outer_installs, \
                    "the inner emulator installed a second CRT shim"
                assert CRT_EMULATOR.noops == outer_noops + 1, \
                    "the inner emulator did not take the measured no-op branch"
                assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                        _REAL_CLOSE, os.O_BINARY) == outer_state, (
                    "the inner emulator replaced the runtime it was handed, "
                    "which is the M68D.3 defect")
                # ── 6 · binary reads stay byte-exact THROUGH the inner layer ─
                assert _read_whole(target, os.O_RDONLY | WIN_O_BINARY) \
                    == self.FIXTURE, (
                    "the nested emulator corrupted an explicitly-binary "
                    "descriptor — the 4-of-8-bytes defect")
                with windows_text_runtime(binary_available=True) as runtime:
                    runtime.assert_the_branch_is_live()
                    with source_snapshot(target) as snap:
                        assert snap.size_bytes == 10240, (
                            "the nested emulator truncated a production read: "
                            f"{snap.size_bytes}")
                        assert snap.payload() == self.FIXTURE
                        assert snap.sha256 == hashlib.sha256(
                            self.FIXTURE).hexdigest()
                        assert snap.stable is True
                # ── 7 · ONE translation layer, not two ─────────────────────
                assert _read_whole(witness, os.O_RDONLY) == b"A\r\nB", (
                    "the nested emulator translated on top of the host: "
                    f"{_read_whole(witness, os.O_RDONLY)!r} (a second pass "
                    "would give b'A\\nB')")

            # ── 8/9 · the outer layer survived the inner one ────────────────
            assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                    _REAL_CLOSE, os.O_BINARY) == outer_state, \
                "the inner context did not restore the outer one"
            assert NATIVE.outcome == NATIVE_TEXT, \
                "the outer measurement did not survive the inner context"
            # IDENTITY, not equality. `NativeTextRuntime` is a frozen
            # dataclass, so an inner context that restored NOTHING would leave
            # its OWN measurement in place and compare EQUAL to the outer one
            # — same verdict, same bytes, different object. Measured: the
            # mutation that empties the stand-down's `finally` is invisible to
            # `==` and caught only here.
            assert NATIVE is outer_native, (
                "the inner context did not hand back the OUTER measurement "
                "object, so its restoration is only coincidentally correct")
            assert _read_whole(witness, os.O_RDONLY) == b"A\r\nB", \
                "the outer host stopped translating once the inner one exited"
            assert _read_whole(target, os.O_RDONLY | WIN_O_BINARY) \
                == self.FIXTURE
            # And the outer host still corrupts an unflagged production read,
            # so what survived is the translating host rather than a dead one.
            with windows_text_runtime(binary_available=False) as runtime:
                runtime.assert_the_branch_is_live()
                with source_snapshot(target) as snap:
                    assert snap.size_bytes == 26, (
                        "the outer translating host no longer truncates: "
                        f"{snap.size_bytes}")
                    assert snap.sha256.startswith("b858da80d8a5")

        # ── 10/11 · everything restored ────────────────────────────────────
        assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                _REAL_CLOSE, NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED,
                hasattr(os, "O_BINARY"),
                getattr(os, "O_BINARY", None)) == pristine, \
            "the nested emulators did not restore the pristine runtime"
        assert NATIVE is pristine_native, \
            "the pristine measurement object was replaced by an equal one"
        assert self._lowest_free_descriptor(tmp_path) == free_fd, \
            "a descriptor leaked out of the nested emulators"
        assert CRT_EMULATOR.entries == entries + 2, \
            "the ledger did not record both entries"
        # The CRUX, stated as a number: two entries, and AT MOST one install.
        # On a byte-exact host the outer installs and the inner stands down; on
        # a translating host neither installs anything at all.
        assert CRT_EMULATOR.installs - installs <= 1, (
            f"{CRT_EMULATOR.installs - installs} CRT shims were installed by "
            "two nested entries; the inner one must always stand down")

    def test_the_host_emulator_refuses_a_host_it_cannot_measure(self,
                                                                monkeypatch):
        """§5/§9 — a failed capability measurement is never made optimistic.

        The emulator must refuse to ENTER, so a host whose capability is
        unknown cannot produce a native verdict, a synthetic verdict, or a
        pass. Both fixtures, because both branch on the same measurement.
        """
        def refuse(path, flags, *args, **kwargs):
            raise OSError(13, "injected EACCES")

        here = sys.modules[__name__]
        pristine = (os.read, os.close, NATIVE,
                    NATIVE_TEXT_TRANSLATION_OBSERVED)
        monkeypatch.setattr(os, "open", refuse)
        monkeypatch.setattr(here, "_REAL_OPEN", refuse)
        for fixture in (host_with_a_text_crt, host_with_a_byte_exact_crt):
            refusals = (CRT_EMULATOR.refusals, BYTE_EXACT_EMULATOR.refusals)
            with pytest.raises(RuntimeError, match="was not measured"):
                with fixture():
                    pytest.fail(
                        f"{fixture.__name__} entered on a host it could not "
                        "measure")
            assert (CRT_EMULATOR.refusals, BYTE_EXACT_EMULATOR.refusals) \
                != refusals, "the refusal was not recorded"
            # NOT patched on the way out of a refusal, and the module's own
            # verdict is untouched: a refusal leaves nothing behind.
            assert os.open is refuse, \
                "the refusing fixture replaced os.open anyway"
            assert (os.read, os.close, NATIVE,
                    NATIVE_TEXT_TRANSLATION_OBSERVED) == pristine, \
                "a refused entry still moved global state"

    def test_the_host_emulator_restores_itself_through_an_exception(self,
                                                                    tmp_path):
        """§9 — restoration on EVERY exit of the HOST layer, nested included.

        The companion to
        :meth:`test_the_runtime_restores_itself_through_an_exception`, one
        layer up: an exception raised inside a host emulator — and inside a
        NESTED one, where the no-op branch has nothing to restore and must
        still restore the right thing — may not leak a patched runtime.
        """
        sentinel = RuntimeError("staged at the host layer")
        for fixture in (host_with_a_text_crt, host_with_a_byte_exact_crt):
            before = (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                      _REAL_CLOSE, NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED,
                      hasattr(os, "O_BINARY"), getattr(os, "O_BINARY", None))
            free_fd = self._lowest_free_descriptor(tmp_path)
            with pytest.raises(RuntimeError) as caught:
                with fixture():
                    raise sentinel
            assert caught.value is sentinel, \
                "the host emulator did not propagate the exception unchanged"
            assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                    _REAL_CLOSE, NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED,
                    hasattr(os, "O_BINARY"),
                    getattr(os, "O_BINARY", None)) == before, (
                f"an exception inside {fixture.__name__} leaked global state")
            assert self._lowest_free_descriptor(tmp_path) == free_fd, \
                f"an exception inside {fixture.__name__} leaked a descriptor"
            # And again NESTED, so the inner no-op branch's restore path is
            # the thing under test rather than the installing one.
            with host_with_a_text_crt():
                nested_before = (os.open, os.read, os.close, _REAL_OPEN,
                                 _REAL_READ, _REAL_CLOSE, NATIVE,
                                 NATIVE_TEXT_TRANSLATION_OBSERVED,
                                 os.O_BINARY)
                nested_native = NATIVE
                with pytest.raises(RuntimeError) as caught:
                    with fixture():
                        raise sentinel
                assert caught.value is sentinel
                assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                        _REAL_CLOSE, NATIVE,
                        NATIVE_TEXT_TRANSLATION_OBSERVED,
                        os.O_BINARY) == nested_before, (
                    f"a nested {fixture.__name__} did not restore the host "
                    "emulator it was entered inside")
                # IDENTITY: see the nested regression. An equal verdict left
                # behind by a `finally` that restores nothing is not a restore.
                assert NATIVE is nested_native, (
                    f"a nested {fixture.__name__} left its OWN measurement in "
                    "place; an equal verdict is not a restored one")
            assert (os.open, os.read, os.close, _REAL_OPEN, _REAL_READ,
                    _REAL_CLOSE, NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED,
                    hasattr(os, "O_BINARY"),
                    getattr(os, "O_BINARY", None)) == before

    def test_an_unmeasured_host_refuses_to_run_at_all(self):
        """§8 — a probe failure is never resolved into an optimistic default."""
        global NATIVE
        saved = NATIVE
        try:
            NATIVE = NativeTextRuntime("FAILED: staged for this test")
            assert NATIVE.measured is False and NATIVE.translates is False
            for binary in (True, False):
                with pytest.raises(RuntimeError, match="not measured"):
                    with windows_text_runtime(binary_available=binary):
                        pytest.fail("an unmeasured host produced a runtime")
        finally:
            NATIVE = saved
        assert NATIVE.measured, "the real measurement was not restored"

    def test_the_simulated_binary_bit_cannot_alias_a_host_flag(self):
        """§10 — the injected bit must be one the host spends on nothing."""
        assert SIMULATED_O_BINARY & _HOST_FLAG_BITS == 0, (
            f"the simulated O_BINARY 0x{SIMULATED_O_BINARY:x} aliases a real "
            f"host flag (host bits 0x{_HOST_FLAG_BITS:x})")
        assert SIMULATED_O_BINARY >= 0x8000
        # NON-VACUITY: the derivation really does move off an occupied bit.
        occupied = _HOST_FLAG_BITS & -_HOST_FLAG_BITS  # lowest bit in use
        assert occupied and _free_flag_bit(occupied) != occupied, \
            "the free-bit search returns bits the host is already using"

    def test_the_binary_bit_in_force_is_the_hosts_own_where_there_is_one(self):
        """WIN_O_BINARY is the REAL flag on a host that publishes one."""
        published = getattr(os, "O_BINARY", None)
        if published is None:
            assert WIN_O_BINARY == SIMULATED_O_BINARY, \
                "a host with no O_BINARY is not using the simulated bit"
        else:
            assert WIN_O_BINARY == published, \
                "the suite is inspecting a synthetic bit on a host that has a real one"

    def test_the_positive_binary_proof_is_never_skipped_for_being_native(self):
        """§14 — "skips the positive binary test when the native capability is
        detected". Every positive case must be COLLECTED unconditionally."""
        collected = collected_node_ids(
            "tests/test_cross_platform_source_integrity_m68d2.py")
        positives = [n for n in collected
                     if "TestBinaryByteIdentity::test_every_fixture_survives" in n]
        assert len(positives) == len(BYTE_FIXTURES), (
            f"{len(positives)} of {len(BYTE_FIXTURES)} binary positive controls "
            "are collected on this host")
        assert any("TestTheRuntimeIsNotDoubleSimulated::"
                   "test_the_native_branch_keeps_the_production_read_byte_exact" in n
                   for n in collected), "the native positive proof is not collected"

    def test_no_negative_control_is_taken_with_a_binary_mode_in_force(self):
        """§9 — a negative control must never become a positive requirement.

        Structural, over the parsed tree: every ``binary_available=False``
        block in this file must assert INEQUALITY against the fixture, and no
        ``binary_available=True`` block may assert that a payload differs.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        offenders: "list[str]" = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            opened = {kw.arg: kw.value for item in node.items
                      if isinstance(item.context_expr, ast.Call)
                      for kw in item.context_expr.keywords}
            flag = opened.get("binary_available")
            if not isinstance(flag, ast.Constant) or flag.value is not True:
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Compare)
                        and any(isinstance(op, ast.NotEq) for op in sub.ops)
                        and "payload" in ast.unparse(sub.left)):
                    offenders.append(ast.unparse(sub)[:70])
        assert not offenders, (
            "a repaired read is asserted to DIFFER from its fixture, which "
            f"makes the negative control a requirement: {offenders}")
        # NON-VACUITY: the scan sees a real offender.
        bad = ast.parse(
            "with windows_text_runtime(binary_available=True):\n"
            "    assert snap.payload() != body\n")
        found = [ast.unparse(sub) for node in ast.walk(bad)
                 if isinstance(node, ast.With)
                 for sub in ast.walk(node)
                 if isinstance(sub, ast.Compare)
                 and any(isinstance(op, ast.NotEq) for op in sub.ops)
                 and "payload" in ast.unparse(sub.left)]
        assert found, "the negative-control scan is blind"


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

    def test_an_accounting_fault_in_the_witness_is_refused(self, tmp_path,
                                                            monkeypatch):
        """M68D.3 §17 — the LENGTH conjunct, rejecting on its own.

        WHY THIS EXISTS. ``B_drop_the_verified_length`` survived the M68D.2
        campaign, registered as explained: "the digest comparison strictly
        implies the length comparison". That argument does not hold. SHA-256 is
        a fixed-size mapping over variable-length inputs, so *digests agreeing*
        does not entail *lengths agreeing* — and more to the point, the two
        values are not independent observations of the file. They are two
        fields of ONE return value from :func:`_descriptor_content`, and an
        accounting fault in that helper can move the length while leaving the
        digest correct. The M68D.2 comment named exactly that risk ("a future
        bug in ``_descriptor_content``'s own accounting") and then had no test
        for it.

        So the fault is INJECTED rather than argued about: the witness returns
        the right digest and a length that disagrees with the capture. No
        filesystem bytes are touched and no hash collision is needed. With the
        conjunct present the observation must be refused; with it dropped — the
        mutation — ``stable`` goes true and this test is what says so.
        """
        body = b"accounting\r\nfault\r\n"
        target = _stage(tmp_path, "acct", body)
        real_descriptor_content = si._descriptor_content
        #: What the patched witness actually HANDED BACK, which is the only
        #: thing production saw. Recording the values computed before the
        #: injection would let a corruption of the returned digest hide here —
        #: MEASURED: the M68D.3 campaign's
        #: ``G_accounting_fault_corrupts_the_digest_too`` survived the first
        #: version of this test for exactly that reason.
        returned: "list[tuple[str, int]]" = []

        def miscounting_witness(fd: int) -> "tuple[str, int]":
            digest, total = real_descriptor_content(fd)
            assert total == len(body), \
                "the real witness did not agree with the capture to begin with"
            # The digest is UNTOUCHED and correct. Only the accounting moves.
            answer = (digest, total + 1)
            returned.append(answer)
            return answer

        monkeypatch.setattr(si, "_descriptor_content", miscounting_witness)
        with source_snapshot(target) as snap:
            # NON-VACUITY, in both directions.
            assert returned, "the witness was never called, so nothing was injected"
            assert returned[0][0] == hashlib.sha256(body).hexdigest(), \
                "the injected digest is wrong, so the digest conjunct would " \
                "have rejected this and the length conjunct proves nothing"
            assert returned[0][1] == len(body) + 1, \
                "the length handed back was not actually inconsistent"
            assert snap.size_bytes == len(body), "the capture itself was damaged"
            assert snap.sha256 == hashlib.sha256(body).hexdigest(), \
                "the capture digest moved, so this is not a length-only fault"
            # THE PROPERTY: a coherent digest plus an incoherent length is not
            # a stable observation.
            assert snap.stable is False, (
                "a witness whose length disagreed with the capture was "
                "accepted as stable; only the length conjunct can reject this")
            assert identify_snapshot(snap).sha256 is None, \
                "an incoherent observation still issued a write precondition"
            assert identify_snapshot(snap).complete is False

    def test_an_accounting_fault_is_also_refused_by_recheck(self, tmp_path,
                                                            monkeypatch):
        """The derived-reader path has the same conjunct and the same duty."""
        body = b"recheck\r\naccounting\r\n"
        target = _stage(tmp_path, "acct_recheck", body)
        with source_snapshot(target) as snap:
            assert snap.stable is True, "the clean observation was not stable"
            real_descriptor_content = si._descriptor_content
            calls: "list[int]" = []

            def miscounting_witness(fd: int) -> "tuple[str, int]":
                digest, total = real_descriptor_content(fd)
                calls.append(total)
                return digest, total - 1

            monkeypatch.setattr(si, "_descriptor_content", miscounting_witness)
            assert snap.recheck() is False, (
                "recheck accepted a witness length that disagreed with the "
                "size it had published")
            assert calls == [len(body)], \
                "NON-VACUITY: the injected witness was not the one consulted"
            assert snap.stable is False, "the verdict was not remembered"

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
        with windows_text_runtime(binary_available=False) as runtime:
            runtime.assert_the_branch_is_live()
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
        with windows_text_runtime(binary_available=True) as runtime:
            runtime.assert_the_branch_is_live()
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
        swallowing = [ast.unparse(h)[:70] for h in handlers
                      if not _handler_records_the_failure(h)]
        assert not swallowing, f"an exception handler swallows: {swallowing}"
        assert handlers, "NON-VACUITY: the scan found no handlers at all"
        # NON-VACUITY: the refined scan still catches a genuine swallow, and
        # the FAILED-verdict exemption (M68D.3 §8) cannot excuse one. Both
        # shapes below are rejected.
        for gutted in ("try:\n    pass\nexcept OSError:\n    pass\n",
                       "try:\n    pass\nexcept OSError:\n    return None\n",
                       "try:\n    pass\nexcept OSError:\n"
                       "    return NativeTextRuntime(BYTE_EXACT)\n"):
            caught = [h for h in ast.walk(ast.parse(gutted))
                      if isinstance(h, ast.ExceptHandler)
                      and not _handler_records_the_failure(h)]
            assert caught, f"the swallow scan accepts {gutted!r}"
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
        # M68D.3 — the NEGATIVE controls. Without these the four above are
        # satisfied by a runtime that translates nothing (§11, PROOF B/D).
        "TestTheTextRuntimeReallyTranslates::test_without_a_binary_mode_the_observation_really_is_corrupted",
        "TestTheTextRuntimeReallyTranslates::test_the_ctrl_z_fixture_really_is_truncated_at_twenty_six",
        # M68D.3 — and the detector that forbids CI 38007992655 recurring: the
        # native positive proof, plus the defective shim it is measured against.
        "TestTheRuntimeIsNotDoubleSimulated::test_the_native_branch_keeps_the_production_read_byte_exact",
        "TestTheRuntimeIsNotDoubleSimulated::test_a_shim_that_strips_the_binary_flag_is_caught",
        "TestTheRuntimeIsNotDoubleSimulated::test_the_probe_is_a_measurement_and_not_a_constant",
        # M68D.4 — the RECURSIVE no-op rule. CI 38160730836 proved that
        # repairing the simulator does not protect the host emulator above it,
        # and this is the proof that the inner layer stands down. It must run
        # on Windows, where the OUTER layer is the no-op and the real CRT does
        # the work, as well as on POSIX, where the nesting is the only way to
        # put an emulator on a translating host at all.
        "TestTheRuntimeIsNotDoubleSimulated::test_a_nested_emulator_is_a_true_noop_on_a_translating_host",
        "TestTheRuntimeIsNotDoubleSimulated::test_the_host_emulator_refuses_a_host_it_cannot_measure",
        "TestTheRuntimeIsNotDoubleSimulated::test_the_host_emulator_restores_itself_through_an_exception",
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


class TestTheSimulatorControlsThemselvesAreIntact:
    """M68D.3 §14/§18 — absent-controls for the HARNESS's own assertions.

    FOUND BY THE M68D.3 CAMPAIGN. Eleven mutations survived its first run, and
    not one of them touched production: they deleted assertions and inventory
    entries inside this very file. Removing ``assert snap.payload() == body``
    makes nothing fail, because until now nothing checked that the assertion
    was there. That is the same lesson
    :class:`TestTheGovernanceControlsThemselvesAreIntact` records one milestone
    earlier, and it applies with more force here: these are the controls that
    decide whether a 26-byte observation can be called a 10240-byte source.

    Every scan uses the STRINGS-DROPPED view (:func:`_no_strings`), because
    this class names each literal it looks for. Read as raw text it matches its
    own search patterns and passes on a file whose controls have been gutted.
    """

    @pytest.fixture(scope="class")
    def code(self):
        return _no_strings(Path(__file__))

    def test_the_byte_equality_assertion_is_still_there(self):
        """The whole of Group A, in one line. §12.

        PER TEST, not file-wide. ``assert snap.payload() == body`` appears in
        half a dozen places here, so a file-wide scan stays green while the
        occurrence that matters is deleted — MEASURED: that is how
        ``G_remove_the_byte_equality_assertion`` survived the first run of the
        M68D.3 campaign.
        """
        body = _test_body(
            "test_every_fixture_survives_a_text_translating_runtime")
        assert "assert snap . payload ( ) == body" in body, \
            "the binary positive control no longer compares the bytes"
        assert "assert snap . size_bytes == len ( body )" in body, \
            "the binary positive control no longer compares the byte count"
        assert ("assert snap . sha256 == hashlib . sha256 ( body ) . hexdigest ( )"
                in body), \
            "the binary positive control no longer compares the digest"
        stream = _test_body("test_stream_mode_digests_the_bytes_on_disk")
        assert ("assert snap . sha256 == hashlib . sha256 ( body ) . hexdigest ( )"
                in stream), "the stream-mode positive control lost its digest"
        assert "assert served == body" in stream, \
            "the stream-mode positive control no longer compares served bytes"

    def test_twenty_six_is_still_refused_as_a_complete_source(self):
        """§14 — "assumes a 26-byte truncated read is a valid complete snapshot"."""
        positive = _test_body("test_the_ctrl_z_truncation_is_closed")
        assert "assert snap . size_bytes == 10240" in positive, \
            "the Ctrl-Z invariant no longer pins the full length"
        assert "assert snap . size_bytes != 26" in positive, \
            "a 26-byte read is no longer explicitly refused"
        negative = _test_body(
            "test_the_ctrl_z_fixture_really_is_truncated_at_twenty_six")
        assert "assert snap . size_bytes == 26" in negative, (
            "the NEGATIVE control no longer pins the measured truncation, so "
            "the positive one could pass against a runtime that does nothing")
        # Asserted against the RAW text: the digest is a string literal, so
        # the stripped view this class otherwise uses cannot see it.
        assert "b858da80d8a5" in Path(__file__).read_text(encoding="utf-8"), \
            "the digest the real runner measured is no longer pinned anywhere"

    def test_the_branch_liveness_record_still_asserts(self, code):
        """A runtime that took no branch must not be able to report success."""
        for needle, lost in (
                ("assert self . native != self . synthetic",
                 "a runtime may now be both native and synthetic, or neither"),
                ("assert self . opens == 0 and self . translations == 0",
                 "the native branch may now install a shim"),
                ("assert self . translations >= 1",
                 "a negative control need no longer translate anything"),
                ("assert self . text_descriptors >= 1",
                 "a negative control need no longer open a text descriptor"),
                ("assert si . _source_open_flags ( ) == expected",
                 "the runtime no longer checks production derives its own flags")):
            assert needle in code, f"NON-VACUITY LOST: {lost}"

    def test_every_proof_consults_the_branch_liveness_record(self):
        """Each of the four proofs must say which branch it actually ran.

        Structural and per-test, so deleting ONE call is caught. A proof that
        does not consult the record is a proof that passes on a host where its
        branch was never entered.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        unwitnessed: "list[str]" = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            # STRINGS-DROPPED, like every other scan in this file: the
            # negative-control detector below NAMES this call inside its own
            # non-vacuity literal without ever entering a runtime.
            body = _no_strings_of(ast.unparse(node))
            if "windows_text_runtime ( binary_available =" not in body:
                continue
            # Exempt by DESIGN, each for a stated reason: the restoration and
            # nesting tests are about the context manager's own lifetime and
            # deliberately assert on `os` rather than on a verdict, and the
            # fail-closed test asserts the runtime REFUSES to produce one.
            if node.name in ("test_the_runtime_leaves_the_os_module_as_it_found_it",
                             "test_the_runtime_restores_itself_through_an_exception",
                             "test_a_nested_runtime_unwinds_in_order",
                             "test_an_unmeasured_host_refuses_to_run_at_all",
                             "test_a_leaked_patch_is_caught_by_the_next_measurement",
                             "test_a_shim_that_strips_the_binary_flag_is_caught",
                             "test_double_modelling_the_host_is_caught",
                             "test_a_reissued_descriptor_number_does_not_inherit_translation"):
                continue
            if ("assert_the_branch_is_live" not in body
                    and "runtime . native is True" not in body):
                unwitnessed.append(node.name)
        assert not unwitnessed, (
            "these tests enter a text runtime without recording which branch "
            f"ran, so they pass vacuously on one of the two hosts: {unwitnessed}")
        # NON-VACUITY: the scan sees a real offender.
        offender = ast.parse(
            "def test_x():\n"
            "    with windows_text_runtime(binary_available=True):\n"
            "        pass\n")
        found = [n.name for n in ast.walk(offender)
                 if isinstance(n, ast.FunctionDef)
                 and "windows_text_runtime ( binary_available ="
                 in _no_strings_of(ast.unparse(n))
                 and "assert_the_branch_is_live"
                 not in _no_strings_of(ast.unparse(n))]
        assert found, "the branch-liveness scan is blind"

    def test_the_negative_controls_still_assert_incoherence(self):
        """A text-mode observation must be shown NOT to describe the disk.

        PER TEST, for the reason given above: the capture-mode and stream-mode
        negative controls assert the same inequality, so a file-wide scan lets
        either one be deleted in silence.
        """
        capture = _test_body(
            "test_without_a_binary_mode_the_observation_really_is_corrupted")
        assert "assert observed != body" in capture, \
            "the unrepaired payload is no longer required to differ"
        assert "assert snap . size_bytes < len ( body )" in capture, \
            "the unrepaired read is no longer required to be short"
        assert ("assert snap . sha256 != hashlib . sha256 ( body ) . hexdigest ( )"
                in capture), \
            "the unrepaired capture digest no longer has to differ from the disk"
        stream = _test_body("test_the_unrepaired_digest_does_not_describe_the_disk")
        assert ("assert snap . sha256 != hashlib . sha256 ( body ) . hexdigest ( )"
                in stream), \
            "the unrepaired stream digest no longer has to differ from the disk"
        assert "assert snap . size_bytes < len ( body )" in stream, \
            "the unrepaired stream read is no longer required to be short"

    def test_the_accounting_fault_injection_is_still_a_length_only_fault(self,
                                                                         code):
        """§17 — if the injected DIGEST moves too, the test proves nothing.

        The length conjunct is only falsifiable while the digest handed back is
        the correct one: otherwise the digest conjunct does the rejecting and
        ``B_drop_the_verified_length`` survives again, this time invisibly.
        """
        del code
        inject = _test_body("test_an_accounting_fault_in_the_witness_is_refused")
        assert "answer = ( digest , total + 1 )" in inject, \
            "the witness accounting fault is no longer injected"
        assert "returned . append ( answer )" in inject, (
            "the test no longer records what the witness HANDED BACK, so a "
            "corruption of the returned digest would hide here")
        assert ("assert returned [ 0 ] [ 0 ] == hashlib . sha256 ( body ) . hexdigest ( )"
                in inject), (
            "the RETURNED digest is no longer asserted CORRECT, so the digest "
            "conjunct could be the thing doing the rejecting")
        assert "assert returned [ 0 ] [ 1 ] == len ( body ) + 1" in inject, \
            "the returned length is no longer required to be inconsistent"
        assert "assert snap . stable is False" in inject, \
            "the accounting fault is no longer required to be refused"
        recheck = _test_body("test_an_accounting_fault_is_also_refused_by_recheck")
        # `ast.unparse` parenthesises a tuple return, so this is the shape
        # the stripped view actually sees.
        assert "return ( digest , total - 1 )" in recheck, \
            "the recheck half of the accounting fault is gone"
        assert "assert snap . recheck ( ) is False" in recheck, \
            "recheck is no longer required to refuse the accounting fault"

    def test_the_probe_is_still_fail_closed(self, code):
        """§8 — the three ways a measurement can fail, each still refused."""
        assert "if not NATIVE . measured :" in code, \
            "an unmeasured host is no longer refused a runtime"
        assert "if target . read_bytes ( ) != NATIVE_PROBE_BYTES :" in code, \
            "the probe no longer verifies its own fixture"
        assert "if binary != NATIVE_PROBE_BYTES :" in code, \
            "the probe no longer verifies the explicit-binary descriptor"
        assert "if bit is None :" in code, \
            "a translating host with no O_BINARY is no longer refused"
        assert "text_fds . discard ( fd )" in code, \
            "the shim no longer forgets a closed descriptor, so a reissued " \
            "number inherits its text marking"

    def test_the_m68d3_invariants_are_in_the_mandatory_inventory(self):
        """§16 — the new proofs must RUN on Windows, not merely exist.

        The inventory is what the Windows job's collection is checked against.
        An entry removed here is a proof that stops being required on the only
        platform where the native branch can be taken at all.
        """
        mine = MANDATORY_CROSS_PLATFORM_INVARIANTS[
            "tests/test_cross_platform_source_integrity_m68d2.py"]
        joined = " ".join(mine)
        for category, needle in (
                ("native positive proof (PROOF A)",
                 "test_the_native_branch_keeps_the_production_read_byte_exact"),
                ("defective-shim detector",
                 "test_a_shim_that_strips_the_binary_flag_is_caught"),
                ("capability measurement",
                 "test_the_probe_is_a_measurement_and_not_a_constant"),
                ("negative control (PROOF B/D)",
                 "test_without_a_binary_mode_the_observation_really_is_corrupted"),
                ("Ctrl-Z negative control",
                 "test_the_ctrl_z_fixture_really_is_truncated_at_twenty_six")):
            assert needle in joined, \
                f"the mandatory inventory lost its {category}"
        assert len(mine) >= 16, \
            f"the M68D.2/M68D.3 mandatory inventory shrank to {len(mine)} entries"
        # Every name in the inventory must be a test that EXISTS here, or the
        # inventory could be satisfied by entries nothing can collect.
        collected = " ".join(collected_node_ids(
            "tests/test_cross_platform_source_integrity_m68d2.py"))
        missing = [name for name in mine if name not in collected]
        assert not missing, f"the inventory names uncollectable tests: {missing}"

    def test_the_host_emulator_is_still_native_aware(self):
        """§11 — the M68D.4 absent-controls, over the HOST emulator.

        The behavioural proof is
        :meth:`TestTheRuntimeIsNotDoubleSimulated.test_a_nested_emulator_is_a_true_noop_on_a_translating_host`,
        which FAILS against the generation-71 body with the real runner's
        verdict string. These are the structural controls for the edits a
        behavioural test on THIS host cannot see — on a byte-exact host the
        native branch is the one that never runs, and on a translating host
        the synthetic one is, so each platform is blind to half of it.
        """
        emulator = _test_body("host_with_a_text_crt")
        # "makes a failed capability measurement optimistic".
        assert "if not before . measured :" in emulator, \
            "the host emulator no longer refuses a host it cannot measure"
        assert "raise RuntimeError" in emulator, \
            "the host emulator no longer refuses anything"
        # "skips the second capability measurement": the host it was given,
        # and the host it then presented.
        assert emulator.count("measure_native_text_runtime ( )") >= 2, (
            "the host emulator no longer measures BOTH the host it was given "
            "and the host it then presented")
        # "always installs the text CRT emulator": the stand-down, recorded.
        assert "if before . translates :" in emulator, \
            "the host emulator no longer branches on the measured host"
        assert "CRT_EMULATOR . noops += 1" in emulator, \
            "the host emulator's stand-down is no longer recorded"
        assert "CRT_EMULATOR . installs += 1" in emulator, \
            "an installed CRT shim is no longer recorded"
        # "strips the host's real O_BINARY": the line that caused CI
        # 38160730836 must stay BELOW the stand-down, never above it.
        assert emulator.index("if before . translates :") \
            < emulator.index("flags & ~ bit"), (
            "the binary flag is stripped before the host is measured, which "
            "is CI 38160730836 exactly")
        # "removes the native-host early branch": the stand-down must RETURN,
        # or control falls through into the shim it exists to avoid.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "host_with_a_text_crt")
        stand_down = [n for n in fn.body if isinstance(n, ast.If)
                      and ast.unparse(n.test) == "before.translates"]
        assert len(stand_down) == 1, \
            "the host emulator has no single measured stand-down branch"
        assert any(isinstance(s, ast.Return) for s in stand_down[0].body), (
            "the native stand-down does not return, so control falls through "
            "into the shim construction below it")
        # "ignores descriptor-number reuse", at the host layer too.
        assert "crt_text_fds . discard ( fd )" in emulator, (
            "the host emulator no longer forgets a closed descriptor, so a "
            "reissued number inherits its text marking")
        # The §10 inverse fixture stays measured, and stays SMALL: it may not
        # grow into the general-purpose OS emulator §10 forbids.
        inverse = _test_body("host_with_a_byte_exact_crt")
        assert "if not before . translates :" in inverse, \
            "the inverse fixture no longer stands down on a byte-exact host"
        assert "flags | bit" in inverse, \
            "the inverse fixture no longer presents a byte-exact descriptor"
        assert "if NATIVE . outcome != BYTE_EXACT :" in inverse, \
            "the inverse fixture no longer verifies the host it presented"
        assert "if not before . measured :" in inverse, \
            "the inverse fixture no longer refuses an unmeasurable host"
        for overreach in ("os . read =", "os . close ="):
            assert overreach not in inverse, (
                f"the inverse capability fixture patches {overreach.strip()}, "
                "which makes it an OS emulator rather than one control")
        # NON-VACUITY: the SAME checks, against the generation-71 shape — an
        # emulator that patches before it measures — must REJECT it.
        gen71 = _no_strings_of(
            "def host_with_a_text_crt():\n"
            "    bit = getattr(os, 'O_BINARY', SIMULATED_O_BINARY)\n"
            "    def crt_open(path, flags, *a, **k):\n"
            "        return host_open(path, flags & ~bit, *a, **k)\n"
            "    os.open = crt_open\n"
            "    NATIVE = measure_native_text_runtime()\n"
            "    yield NATIVE\n")
        assert "flags & ~ bit" in gen71, "the sample is not the defective shape"
        assert "if before . translates :" not in gen71, (
            "the native-awareness check cannot tell the repaired emulator "
            "from the generation-71 one")
        assert gen71.count("measure_native_text_runtime ( )") < 2, \
            "the second-measurement check is blind"

    def test_the_nested_regression_still_asserts_byte_identity(self):
        """§11 — "removes the nested emulator regression" / "removes binary
        byte-equality assertions" / "negative control made vacuous".

        PER TEST and per ASSERTION, because the nested proof is the only thing
        between a future edit and a rerun of CI 38160730836, and a file-wide
        scan would let any single conjunct go in silence.
        """
        name = "test_a_nested_emulator_is_a_true_noop_on_a_translating_host"
        nested = _test_body(name)
        # The inner layer is required to be a MEASURED no-op.
        assert "assert inner . outcome == NATIVE_TEXT" in nested, \
            "the nested regression no longer requires the inner verdict"
        assert "assert CRT_EMULATOR . installs == outer_installs" in nested, \
            "the nested regression no longer forbids a second CRT shim"
        assert "assert CRT_EMULATOR . noops == outer_noops + 1" in nested, \
            "the nested regression no longer requires the no-op branch"
        assert "_REAL_CLOSE , os . O_BINARY ) == outer_state" in nested, (
            "the nested regression no longer requires the inner layer to "
            "leave the runtime it was handed untouched")
        # Binary byte-equality through the nested layer: the raw descriptor
        # read AND the production read.
        binary_reads = nested.count(
            "assert _read_whole ( target , os . O_RDONLY | WIN_O_BINARY ) "
            "== self . FIXTURE")
        assert binary_reads >= 2, (
            "the byte-exact binary read must be required INSIDE the inner "
            "context AND again after it; a single surviving copy satisfies a "
            f"membership test while the load-bearing one is deleted, and only "
            f"{binary_reads} remain")
        assert "assert snap . size_bytes == 10240" in nested, \
            "the nested regression no longer requires the full production read"
        assert "assert snap . payload ( ) == self . FIXTURE" in nested, \
            "the nested regression no longer compares the payload to the disk"
        assert ("assert snap . sha256 == hashlib . sha256 ( self . FIXTURE ) "
                ". hexdigest ( )" in nested), \
            "the nested regression no longer pins the digest to the disk"
        # The negative control that proves the outer host is still live.
        assert "assert snap . size_bytes == 26" in nested, \
            "the nested regression no longer shows the outer host translating"
        # Restoration and descriptor hygiene (§9).
        assert "== pristine" in nested, \
            "the nested regression no longer requires the pristine runtime back"
        assert "_lowest_free_descriptor ( tmp_path ) == free_fd" in nested, \
            "the nested regression no longer detects a leaked descriptor"
        # THE SINGLE-TRANSLATION WITNESS, over the parsed tree. The byte
        # literal that carries this invariant is a STRING token, so the
        # stripped view cannot see it — and a raw-text scan would match this
        # detector's own copy. Comparing AST constant VALUES has neither
        # problem: `b"A\r\r\nB"` collapses to `b"A\r\nB"` after ONE pass and
        # to `b"A\nB"` after two, so the expected value IS the proof that the
        # inner layer did not translate on top of the host.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == name)
        witnessed = [node.comparators[0].value for node in ast.walk(fn)
                     if isinstance(node, ast.Compare)
                     and any(isinstance(op, ast.Eq) for op in node.ops)
                     and ast.unparse(node.left).startswith("_read_whole(witness")
                     and isinstance(node.comparators[0], ast.Constant)]
        assert len(witnessed) >= 3, (
            "the host must be shown to translate exactly once BEFORE, INSIDE "
            f"and AFTER the inner context: {len(witnessed)} witness reads")
        assert set(witnessed) == {b"A\r\nB"}, (
            "the single-translation witness no longer requires the one-pass "
            f"result, so a second translation would pass: {witnessed}")
        # And the fail-closed / restoration neighbours must still be there.
        refuses = _test_body(
            "test_the_host_emulator_refuses_a_host_it_cannot_measure")
        assert "pytest . raises ( RuntimeError , match =" in refuses, \
            "the host emulator is no longer required to refuse"
        assert "host_with_a_byte_exact_crt" in refuses, \
            "the inverse fixture is no longer required to refuse"
        assert "assert os . open is refuse" in refuses, \
            "a refusing fixture may now patch the runtime on its way out"
        restores = _test_body(
            "test_the_host_emulator_restores_itself_through_an_exception")
        assert "caught . value is sentinel" in restores, \
            "the host emulator may now swallow or replace the exception"
        assert "== nested_before" in restores, \
            "a nested host emulator need no longer restore the outer one"
        assert "_lowest_free_descriptor ( tmp_path ) == free_fd" in restores, \
            "an exception path may now leak a descriptor"

    def test_the_probe_contrast_cannot_become_one_sided(self):
        """§10 — the probe test must keep BOTH hosts and demand they differ.

        The §10 hazard, stated exactly: on real Windows the host already
        translates, so the original contrast — the real host against the
        emulator — collapses into ``NATIVE_TEXT`` against ``NATIVE_TEXT`` and
        the test would pass while distinguishing nothing. The repair is the
        inverse fixture; this stops a future edit dropping it and restoring
        the vacuous version.
        """
        probe = _test_body("test_the_probe_is_a_measurement_and_not_a_constant")
        assert "host_with_a_text_crt" in probe, \
            "the probe test no longer presents a translating host"
        assert "host_with_a_byte_exact_crt" in probe, \
            "the probe test no longer presents a byte-exact host"
        assert "assert emulated . outcome != exact . outcome" in probe, (
            "the probe test no longer requires the SAME probe to return "
            "DIFFERENT verdicts for the two hosts")
        assert ("assert ( emulated . outcome , exact . outcome ) == "
                "( NATIVE_TEXT , BYTE_EXACT )" in probe), \
            "the probe test no longer pins which verdict belongs to which host"
        assert "assert exact . unflagged == NATIVE_PROBE_BYTES" in probe, \
            "the byte-exact verdict is no longer backed by the bytes read"
        # The contrast's own non-vacuity: one fixture really worked and one
        # really stood down, whichever host this is.
        assert "assert installed == 1" in probe, (
            "the probe contrast no longer proves a host was actually "
            "presented, so two no-ops would satisfy it")
        assert "assert stood_down == 1" in probe, \
            "the probe contrast no longer exercises the recursive no-op rule"

    def test_the_m68d4_invariants_are_in_the_mandatory_inventory(self):
        """§7 — "Register it as a required cross-platform invariant."

        And §11's "removes the mandatory nested invariant from inventory" and
        "changes required Windows tests into optional tests". An entry removed
        here is a proof that stops being required on the only platform where
        the native branch is real.
        """
        mine = MANDATORY_CROSS_PLATFORM_INVARIANTS[
            "tests/test_cross_platform_source_integrity_m68d2.py"]
        joined = " ".join(mine)
        for category, needle in (
                ("nested no-op regression",
                 "test_a_nested_emulator_is_a_true_noop_on_a_translating_host"),
                ("host-emulator fail-closed control",
                 "test_the_host_emulator_refuses_a_host_it_cannot_measure"),
                ("host-emulator restoration control",
                 "test_the_host_emulator_restores_itself_through_an_exception")):
            assert needle in joined, \
                f"the mandatory inventory lost its {category}"
        assert len(mine) >= 19, \
            f"the mandatory inventory shrank to {len(mine)} entries"
        # Not hidden behind a skip either (§7): the node has to be COLLECTED
        # on whatever host is running right now.
        collected = collected_node_ids(
            "tests/test_cross_platform_source_integrity_m68d2.py")
        assert any("test_a_nested_emulator_is_a_true_noop_on_a_translating_host"
                   in node for node in collected), \
            "the nested regression is not collected on this host"

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
