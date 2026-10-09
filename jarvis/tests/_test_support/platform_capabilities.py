"""V69 M68D.1 — filesystem capabilities, MEASURED on the host that runs the suite.

WHY MEASURED AND NOT INFERRED
=============================
``sys.platform == "win32"`` is a label. What an H02 adversarial fixture needs is
an answer to a sharper question: *can this process replace, or delete, a path
whose descriptor it is holding open?* Those are not the same question. A POSIX
host can answer no (an exotic mount, a container policy) and a future Windows
runtime could answer yes. So every constant below is produced by PERFORMING the
operation once, in a temporary directory, and recording what the kernel said.

THE PROBE MODELS PRODUCTION (M68D.1 §8)
=======================================
``core.source_integrity.source_snapshot`` observes a source with
``os.open(path, _source_open_flags())`` — read-only plus ``O_BINARY`` where the
platform has a text mode (M68D.2 §5), and no Win32 sharing mode of its own. The
probe holds its descriptor exactly the same way, binary flag included: the
sharing semantics these capabilities measure are a property of the SHARE MODE
CPython requests, which binary mode does not change, but the probe asks for the
same flags production does so that it cannot drift into measuring a handle
production never opens. It deliberately does
NOT request ``FILE_SHARE_DELETE``: doing so would let the POSIX-shaped attack
run on Windows against a handle production never opens, and the test would then
be evidence about the probe instead of about JARVIS.

WHAT THESE CAPABILITIES ARE NOT
===============================
None of them is the security property. The property is that the CONTENT a read
returns and the DIGEST that identifies it come from ONE observation, and it is
asserted on every platform. A capability only decides WHICH ADVERSARY can be
staged against it. Where a mechanism is unavailable, the suite stages the
strongest adversary the platform does permit — an in-place rewrite through a
second handle, which no supported platform blocks — and skips only the
impossible mechanism, never the invariant.

Measured on the real Windows runner of CI run 37695660326 (windows-2025-vs2026,
CPython 3.11.9): ``os.replace`` over a held path raised ``WinError 5`` and
``os.unlink`` of one raised ``WinError 32``, in 9 of the 14 H02 failures.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from enum import Enum

__all__ = [
    "Mutation",
    "AVAILABLE_UNDER_OPEN_FD",
    "CONTINUOUS_MECHANISMS",
    "REPLACE_OVER_OPEN_PATH",
    "UNLINK_OPEN_PATH",
    "IN_PLACE_REWRITE_OF_OPEN_PATH",
    "MODE_BITS_REMOVE_READ",
    "EXEC_BIT_OBSERVABLE",
    "PROBE_DIAGNOSTIC",
    "WHY_NO_REPLACE",
    "WHY_NO_UNLINK",
    "WHY_NO_MODE_BITS",
    "WHY_NO_EXEC_BIT",
]


class Mutation(str, Enum):
    """A way to change what a path means while a descriptor on it is held."""

    #: Rename a different file over the path. The held inode is untouched, so a
    #: coherent observation stays coherent AND stays `stable`.
    ATOMIC_REPLACE = "ATOMIC_REPLACE"
    #: Remove the directory entry. The held inode outlives it.
    UNLINK = "UNLINK"
    #: Remove the entry and create a different file at the same name.
    UNLINK_RECREATE = "UNLINK_RECREATE"
    #: Overwrite the bytes of the held inode itself, through a second handle.
    #: The only one of the four that can make an observation `stable is False`,
    #: and the only one no supported platform refuses.
    IN_PLACE_REWRITE = "IN_PLACE_REWRITE"


PROBE_DIAGNOSTIC: dict[str, str] = {}


def _record(name: str, exc: "BaseException | None") -> bool:
    """Record one probe outcome. Returns True when the operation succeeded."""
    if exc is None:
        PROBE_DIAGNOSTIC[name] = "PERMITTED"
        return True
    detail = getattr(exc, "winerror", None)
    PROBE_DIAGNOSTIC[name] = (
        f"REFUSED: {type(exc).__name__} errno={getattr(exc, 'errno', None)}"
        + (f" winerror={detail}" if detail is not None else ""))
    return False


def _probe() -> None:
    """Perform each operation once. Never raises; a failure is an answer."""
    root = tempfile.mkdtemp(prefix="m68d1-cap-")
    try:
        held = os.path.join(root, "held.bin")
        with open(held, "wb") as handle:
            handle.write(b"HELD" * 8)
        other = os.path.join(root, "other.bin")
        with open(other, "wb") as handle:
            handle.write(b"OTHR" * 8)

        # Exactly production's opening: read-only and binary, no sharing mode.
        fd = os.open(held, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            try:
                with open(held, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"ZZZZ")
                globals()["IN_PLACE_REWRITE_OF_OPEN_PATH"] = _record(
                    "in_place_rewrite_of_open_path", None)
            except OSError as exc:
                globals()["IN_PLACE_REWRITE_OF_OPEN_PATH"] = _record(
                    "in_place_rewrite_of_open_path", exc)
            try:
                os.replace(other, held)
                globals()["REPLACE_OVER_OPEN_PATH"] = _record(
                    "replace_over_open_path", None)
            except OSError as exc:
                globals()["REPLACE_OVER_OPEN_PATH"] = _record(
                    "replace_over_open_path", exc)
            try:
                os.unlink(held)
                globals()["UNLINK_OPEN_PATH"] = _record("unlink_open_path", None)
            except OSError as exc:
                globals()["UNLINK_OPEN_PATH"] = _record("unlink_open_path", exc)
        finally:
            os.close(fd)

        # chmod(0o000) actually removes read access for this process. Note the
        # inversion: here the CAPABILITY is that the open FAILS.
        locked = os.path.join(root, "locked.bin")
        with open(locked, "wb") as handle:
            handle.write(b"x")
        removed = False
        try:
            os.chmod(locked, 0o000)
        except OSError as exc:                       # pragma: no cover - exotic
            PROBE_DIAGNOSTIC["mode_bits_remove_read"] = (
                f"REFUSED: chmod raised {type(exc).__name__}: {exc}")
        else:
            try:
                os.close(os.open(locked, os.O_RDONLY))
            except OSError as exc:
                removed = True
                PROBE_DIAGNOSTIC["mode_bits_remove_read"] = (
                    f"PERMITTED: open refused with {type(exc).__name__} "
                    f"errno={getattr(exc, 'errno', None)}")
            else:
                PROBE_DIAGNOSTIC["mode_bits_remove_read"] = (
                    "REFUSED: chmod(0o000) left the file readable")
            try:
                os.chmod(locked, 0o600)
            except OSError:                          # pragma: no cover - exotic
                pass
        globals()["MODE_BITS_REMOVE_READ"] = removed

        # The executable bit is OBSERVABLE: settable AND clearable.
        exe = os.path.join(root, "exe.bin")
        with open(exe, "wb") as handle:
            handle.write(b"#!/bin/sh\n")
        observable = False
        try:
            # Setting 0o755 IS the measurement here: the question is whether
            # this filesystem exposes a settable, CLEARABLE executable bit at
            # all. The file is one this probe just created inside its own
            # `mkdtemp` directory, removed in the `finally` below, and nothing
            # is ever executed from it. The justification is on these lines
            # rather than beside the directive because Bandit parses every
            # comment containing "nosec" and reads the prose as test ids.
            os.chmod(exe, 0o755)  # nosec B103
            on = os.access(exe, os.X_OK)
            os.chmod(exe, 0o644)
            off = os.access(exe, os.X_OK)
            observable = bool(on) and not off
            PROBE_DIAGNOSTIC["exec_bit_observable"] = (
                "PERMITTED" if observable else
                f"REFUSED: X_OK was {on} at 0o755 and {off} at 0o644")
        except OSError as exc:                       # pragma: no cover - exotic
            PROBE_DIAGNOSTIC["exec_bit_observable"] = \
                f"REFUSED: chmod raised {type(exc).__name__}: {exc}"
        globals()["EXEC_BIT_OBSERVABLE"] = observable
    finally:
        shutil.rmtree(root, ignore_errors=True)


#: Defaults, in case the probe itself cannot run. Conservative: an unmeasured
#: capability is reported ABSENT, so a POSIX-only fixture skips rather than
#: failing for a reason that is not about JARVIS.
REPLACE_OVER_OPEN_PATH = False
UNLINK_OPEN_PATH = False
IN_PLACE_REWRITE_OF_OPEN_PATH = False
MODE_BITS_REMOVE_READ = False
EXEC_BIT_OBSERVABLE = False

_probe()

#: The mechanisms that can actually be staged against a held descriptor here.
AVAILABLE_UNDER_OPEN_FD: "tuple[Mutation, ...]" = tuple(
    m for m, ok in (
        (Mutation.ATOMIC_REPLACE, REPLACE_OVER_OPEN_PATH),
        (Mutation.UNLINK, UNLINK_OPEN_PATH),
        (Mutation.UNLINK_RECREATE, UNLINK_OPEN_PATH),
        (Mutation.IN_PLACE_REWRITE, IN_PLACE_REWRITE_OF_OPEN_PATH),
    ) if ok)

#: The subset that leaves a readable file at the path AT EVERY INSTANT. An
#: unlink — even one immediately followed by a recreate — opens a window in
#: which the path does not resolve, so a CONCURRENT reader can fail for a
#: reason that is not about coherence. Tests with a second reader thread
#: parametrise over this set; everything else over the full one.
CONTINUOUS_MECHANISMS: "tuple[Mutation, ...]" = tuple(
    m for m in AVAILABLE_UNDER_OPEN_FD
    if m in (Mutation.ATOMIC_REPLACE, Mutation.IN_PLACE_REWRITE))

_D = PROBE_DIAGNOSTIC.get

WHY_NO_REPLACE = (
    "POSIX_CAPABILITY_TEST: this platform refuses os.replace() over a path "
    "whose descriptor this process holds open, so the rename-under-the-reader "
    "adversary cannot be staged at all. Windows opens without "
    "FILE_SHARE_DELETE and raises WinError 5; requesting that share mode here "
    "would test a handle production never opens. The coherence invariant "
    "itself is asserted on every platform by the IN_PLACE_REWRITE fixtures. "
    f"Measured: {_D('replace_over_open_path')}")

WHY_NO_UNLINK = (
    "POSIX_CAPABILITY_TEST: this platform refuses os.unlink() of a path whose "
    "descriptor this process holds open (Windows: WinError 32), so "
    "'the descriptor outlives the directory entry' has no staging mechanism. "
    "What it proves — that identity cannot change after acquisition — is "
    "asserted everywhere by the IN_PLACE_REWRITE fixtures. "
    f"Measured: {_D('unlink_open_path')}")

WHY_NO_MODE_BITS = (
    "POSIX_CAPABILITY_TEST: chmod(0o000) does not remove read access for this "
    "process, so an unreadable source cannot be constructed this way. Windows "
    "chmod honours only the read-only attribute. The fail-closed contract for "
    "an unobservable source is asserted everywhere by the open()-failure "
    f"injection instead. Measured: {_D('mode_bits_remove_read')}")

WHY_NO_EXEC_BIT = (
    "POSIX_CAPABILITY_TEST: the host filesystem does not expose a clearable "
    "executable bit, so an os.access/Git-mode DISAGREEMENT witness cannot be "
    "constructed. The control itself is the Git index mode and its tests stay "
    f"mandatory on every platform. Measured: {_D('exec_bit_observable')}")
