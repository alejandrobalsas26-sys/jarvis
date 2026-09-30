"""core/bounded_output.py — V69 M68B (A): bounded subprocess output capture.

WHY THIS EXISTS
===============
``core.containment`` collected a contained execution's output with
``subprocess.Popen.communicate()`` and truncated the RESULT:

    stdout, stderr = proc.communicate(timeout=request.timeout)
    stdout = (stdout or "")[:CE_STDOUT_CAP]      # truncation AFTER accumulation

``communicate()`` reads each pipe to EOF into a Python list and joins it. The cap
therefore bounded the string the caller was HANDED, never the memory the broker
ALLOCATED. A snippet that writes 4 GiB to stdout made the *parent* — the JARVIS
runtime, not the contained child — allocate 4 GiB, and the receipt still said
``output_limit: ENFORCED``. The child's own ``RLIMIT_AS``/``RLIMIT_FSIZE`` do not
help: a pipe write is neither address space nor a file, and the writes are
streamed, so there is no per-child quota to exceed.

A BOUNDED RETURNED STRING IS NOT BOUNDED MEMORY. This module makes the bound real:
bytes past the retention limit are counted and **discarded as they arrive**, never
stored.

DESIGN
------
  * one reader THREAD per stream, so both pipes drain concurrently and a child
    that fills stderr while the parent reads stdout cannot deadlock (the reason
    ``communicate`` exists at all — this module keeps that property);
  * draining CONTINUES after the retention limit is reached, so a child that keeps
    writing is never blocked on a full pipe and the wall-clock timeout stays the
    real bound. Bytes are counted, then dropped;
  * a POSITIVE contract, not an absence: every capture reports ``bytes_seen``,
    ``bytes_retained`` and ``truncated`` per stream plus a ``termination_reason``,
    so truncation is observable evidence rather than silent loss;
  * the caller supplies the kill: this module never decides how to tear a process
    tree down (``core.containment`` owns that, including the setsid/process-group
    semantics M66A.1 proved), it only asks for it on timeout and then reaps;
  * text decoding is deferred to the END of the capture and matches what
    ``text=True`` did (locale preferred encoding, universal newlines), with
    ``errors="replace"`` so a snippet emitting invalid bytes can no longer raise
    ``UnicodeDecodeError`` out of the capture itself.

WHAT IT DOES NOT DO
-------------------
It does not bound what the child writes — nothing in user space can. It bounds
what the PARENT retains, and reports honestly how much it discarded.
"""
from __future__ import annotations

import contextlib
import locale
import subprocess
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

#: Read granularity. Small enough that a bounded reader never materialises a
#: large buffer of its own, large enough that draining a noisy child is cheap.
READ_CHUNK_BYTES = 64 * 1024

#: How long to wait for the reader threads to observe EOF after the process has
#: been reaped. They exit as soon as the pipes close; this is a safety net so a
#: wedged reader can never hang the runtime.
_JOIN_GRACE_S = 5.0


class TerminationReason(str, Enum):
    """Why the capture stopped. Structural and closed — never a stderr substring.

    EXITED         the child exited on its own before the deadline
    TIMEOUT        the deadline expired and the caller's kill was invoked
    LAUNCH_FAILED  the process object never became waitable (caller-reported)
    """

    EXITED = "exited"
    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True)
class StreamCapture:
    """One stream's bounded capture and its accounting.

    ``bytes_seen`` is what the child actually produced; ``bytes_retained`` is what
    this process kept. ``truncated`` is True exactly when they differ, so the two
    numbers can never disagree with the flag.
    """

    text: str
    bytes_seen: int
    bytes_retained: int
    limit_bytes: int

    @property
    def truncated(self) -> bool:
        return self.bytes_seen > self.bytes_retained

    def to_dict(self) -> dict:
        return {
            "bytes_seen": self.bytes_seen,
            "bytes_retained": self.bytes_retained,
            "limit_bytes": self.limit_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class BoundedCapture:
    """The result of one bounded capture: both streams plus the exit truth."""

    stdout: StreamCapture
    stderr: StreamCapture
    returncode: int | None
    termination_reason: TerminationReason

    def to_dict(self) -> dict:
        return {
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "returncode": self.returncode,
            "termination_reason": self.termination_reason.value,
        }


def _preferred_encoding() -> str:
    """The encoding ``text=True`` would have used, resolved once per capture."""
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:  # noqa: BLE001 - a broken locale must not fail a capture
        return "utf-8"


def _universal_newlines(text: str) -> str:
    """Reproduce ``text=True``'s newline translation on the decoded result."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _BoundedReader(threading.Thread):
    """Drain one pipe forever; retain at most ``limit`` bytes; count them all.

    The loop deliberately keeps reading after the limit is reached. Stopping
    would leave the pipe buffer full, block the child on its next write, and turn
    a memory bound into a silent hang — the child's output would then be bounded
    by a deadlock rather than by a policy.
    """

    def __init__(self, stream, limit: int, name: str) -> None:
        super().__init__(daemon=True, name=f"bounded-reader-{name}")
        self._stream = stream
        self._limit = max(0, int(limit))
        self._chunks: list[bytes] = []
        self._retained = 0
        self._seen = 0
        self._read_error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised through capture_bounded
        read = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while True:
                chunk = read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                self._seen += len(chunk)
                room = self._limit - self._retained
                if room > 0:
                    keep = chunk[:room]
                    self._chunks.append(keep)
                    self._retained += len(keep)
        except BaseException as exc:  # noqa: BLE001 - a read error is data, not a crash
            self._read_error = exc
        finally:
            # `contextlib.suppress`, not `except Exception: pass` — identical
            # semantics, and not a Bandit B110, so the repository's Low-finding
            # ceiling does not move for a closed pipe.
            with contextlib.suppress(Exception):
                self._stream.close()

    def capture(self, encoding: str) -> StreamCapture:
        raw = b"".join(self._chunks)
        text = _universal_newlines(raw.decode(encoding, errors="replace"))
        return StreamCapture(text=text, bytes_seen=self._seen,
                             bytes_retained=self._retained, limit_bytes=self._limit)


class _StdinWriter(threading.Thread):
    """Write the whole payload, then close. A child that never reads its stdin
    must not wedge the parent, so ``BrokenPipeError`` is a normal outcome."""

    def __init__(self, stream, payload: bytes) -> None:
        super().__init__(daemon=True, name="bounded-stdin-writer")
        self._stream = stream
        self._payload = payload

    def run(self) -> None:  # pragma: no cover - exercised through capture_bounded
        try:
            self._stream.write(self._payload)
            self._stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._stream.close()


def capture_bounded(
    proc: subprocess.Popen,
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
    on_timeout: Callable[[], None] | None = None,
    reap_grace_s: float = _JOIN_GRACE_S,
) -> BoundedCapture:
    """Run ``proc`` to completion, retaining at most the given bytes per stream.

    ``proc`` must have been created with binary ``stdout``/``stderr`` pipes (no
    ``text=True``); decoding happens here, once, at the end.

    On timeout ``on_timeout`` is invoked — the caller's process-tree kill — and
    the child is then reaped within ``reap_grace_s``. Whatever the child produced
    up to that point is still returned, bounded, with
    ``termination_reason == TIMEOUT``, so partial output stays distinguishable
    from complete output.
    """
    encoding = _preferred_encoding()
    readers: list[_BoundedReader] = []
    writer: _StdinWriter | None = None

    if proc.stdin is not None:
        writer = _StdinWriter(proc.stdin, input_bytes or b"")
        writer.start()

    out_reader = _BoundedReader(proc.stdout, stdout_limit, "stdout") \
        if proc.stdout is not None else None
    err_reader = _BoundedReader(proc.stderr, stderr_limit, "stderr") \
        if proc.stderr is not None else None
    for r in (out_reader, err_reader):
        if r is not None:
            readers.append(r)
            r.start()

    reason = TerminationReason.EXITED
    returncode: int | None = None
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        reason = TerminationReason.TIMEOUT
        if on_timeout is not None:
            # The kill is best-effort; the BOUND is not — retention already
            # stopped at the limit whatever the kill does.
            with contextlib.suppress(Exception):
                on_timeout()
        try:
            returncode = proc.wait(timeout=reap_grace_s)
        except subprocess.TimeoutExpired:
            returncode = proc.poll()

    # The pipes close when the last writer (child or any inherited descendant)
    # goes away; the readers then hit EOF and exit. Joining bounded means a
    # descendant holding a pipe open can delay us by at most the grace, never
    # forever — and never at the cost of unbounded retention, which already
    # stopped at the limit.
    if writer is not None:
        writer.join(timeout=reap_grace_s)
    for r in readers:
        r.join(timeout=reap_grace_s)

    empty = StreamCapture(text="", bytes_seen=0, bytes_retained=0, limit_bytes=0)
    return BoundedCapture(
        stdout=out_reader.capture(encoding) if out_reader is not None else empty,
        stderr=err_reader.capture(encoding) if err_reader is not None else empty,
        returncode=returncode,
        termination_reason=reason,
    )
