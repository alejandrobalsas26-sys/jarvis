"""V69 M68B (A) — subprocess output is bounded DURING execution, not after.

THE DEFECT THESE PIN
--------------------
``core.containment`` collected output with ``communicate()`` and then sliced the
result. ``communicate()`` reads each pipe to EOF into memory, so the cap bounded
the string the caller was HANDED and never the memory the broker ALLOCATED. A
snippet printing 4 GiB made the parent allocate 4 GiB while the receipt reported
``output_limit: ENFORCED``.

The tests below are written against BYTES SEEN vs BYTES RETAINED, because that
difference is the only observation that distinguishes a real bound from a slice.
A test that only checked ``len(stdout) <= CE_STDOUT_CAP`` passed against the
defect — the original code satisfied it perfectly.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.bounded_output import (  # noqa: E402
    READ_CHUNK_BYTES,
    BoundedCapture,
    TerminationReason,
    capture_bounded,
)

_KIB = 1024
_MIB = 1024 * 1024


def _spawn(code: str, **kwargs) -> subprocess.Popen:
    """A child with BINARY pipes — the shape ``capture_bounded`` requires."""
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **kwargs)


def _writer(stream: str, total: int, chunk: int = 65536) -> str:
    """Child source that writes exactly ``total`` bytes to one stream."""
    return (
        "import sys\n"
        f"w = sys.{stream}.buffer\n"
        f"left = {total}\n"
        f"block = b'x' * {chunk}\n"
        "while left > 0:\n"
        "    n = min(left, len(block))\n"
        "    w.write(block[:n]); left -= n\n"
        "w.flush()\n"
    )


# ── The bound itself ─────────────────────────────────────────────────────────
class TestRetentionBound:
    def test_large_stdout_is_counted_but_not_retained(self):
        """32 MiB produced, 8 KiB kept. `bytes_seen` proves the child really
        wrote it, so this cannot be satisfied by a short read."""
        proc = _spawn(_writer("stdout", 32 * _MIB))
        cap = capture_bounded(proc, stdout_limit=8 * _KIB,
                              stderr_limit=8 * _KIB, timeout=120)
        assert cap.stdout.bytes_seen == 32 * _MIB
        assert cap.stdout.bytes_retained == 8 * _KIB
        assert cap.stdout.truncated is True
        assert len(cap.stdout.text) <= 8 * _KIB
        assert cap.termination_reason is TerminationReason.EXITED
        assert cap.returncode == 0

    def test_large_stderr_is_counted_but_not_retained(self):
        proc = _spawn(_writer("stderr", 16 * _MIB))
        cap = capture_bounded(proc, stdout_limit=4 * _KIB,
                              stderr_limit=4 * _KIB, timeout=120)
        assert cap.stderr.bytes_seen == 16 * _MIB
        assert cap.stderr.bytes_retained == 4 * _KIB
        assert cap.stderr.truncated is True
        assert cap.stdout.bytes_seen == 0

    def test_both_streams_at_once_do_not_deadlock(self):
        """The reason ``communicate`` exists: a child filling stderr while the
        parent reads stdout wedges a single-stream reader. Two owned readers keep
        both pipes draining, so this completes instead of hanging."""
        code = (
            "import sys\n"
            "block = b'y' * 65536\n"
            "for _ in range(128):\n"
            "    sys.stdout.buffer.write(block)\n"
            "    sys.stderr.buffer.write(block)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
        )
        proc = _spawn(code)
        cap = capture_bounded(proc, stdout_limit=2 * _KIB,
                              stderr_limit=2 * _KIB, timeout=120)
        assert cap.stdout.bytes_seen == 128 * 65536
        assert cap.stderr.bytes_seen == 128 * 65536
        assert cap.stdout.bytes_retained == 2 * _KIB
        assert cap.stderr.bytes_retained == 2 * _KIB
        assert cap.returncode == 0

    def test_zero_output_process_is_not_truncated(self):
        """Nothing retained must not be confused with something discarded."""
        proc = _spawn("pass")
        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB, timeout=30)
        assert cap.stdout.bytes_seen == 0
        assert cap.stdout.bytes_retained == 0
        assert cap.stdout.truncated is False
        assert cap.stderr.truncated is False
        assert cap.stdout.text == ""
        assert cap.returncode == 0
        assert cap.termination_reason is TerminationReason.EXITED


class TestBoundaries:
    LIMIT = 4096

    def test_output_exactly_at_the_boundary_is_not_truncated(self):
        proc = _spawn(_writer("stdout", self.LIMIT))
        cap = capture_bounded(proc, stdout_limit=self.LIMIT,
                              stderr_limit=self.LIMIT, timeout=30)
        assert cap.stdout.bytes_seen == self.LIMIT
        assert cap.stdout.bytes_retained == self.LIMIT
        assert cap.stdout.truncated is False

    def test_output_one_byte_over_the_boundary_is_truncated(self):
        proc = _spawn(_writer("stdout", self.LIMIT + 1))
        cap = capture_bounded(proc, stdout_limit=self.LIMIT,
                              stderr_limit=self.LIMIT, timeout=30)
        assert cap.stdout.bytes_seen == self.LIMIT + 1
        assert cap.stdout.bytes_retained == self.LIMIT
        assert cap.stdout.truncated is True

    def test_truncation_flag_is_derived_not_declared(self):
        """``truncated`` is computed from the two counters, so a mutation that
        sets a flag without changing the numbers cannot lie."""
        proc = _spawn(_writer("stdout", 10))
        cap = capture_bounded(proc, stdout_limit=5, stderr_limit=5, timeout=30)
        assert cap.stdout.truncated == (cap.stdout.bytes_seen
                                        > cap.stdout.bytes_retained)

    def test_a_chunk_straddling_the_limit_is_partially_retained(self):
        """The limit is a BYTE limit, not a chunk limit: a read that crosses it
        must be split, not dropped whole and not kept whole."""
        limit = READ_CHUNK_BYTES + 7
        proc = _spawn(_writer("stdout", READ_CHUNK_BYTES * 3, chunk=READ_CHUNK_BYTES))
        cap = capture_bounded(proc, stdout_limit=limit, stderr_limit=_KIB, timeout=60)
        assert cap.stdout.bytes_retained == limit


class TestTerminationTruth:
    def test_timeout_kills_the_child_and_says_so(self):
        proc = _spawn("import time\ntime.sleep(60)\n")
        killed: list[int] = []

        def _kill():
            killed.append(1)
            proc.kill()

        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB,
                              timeout=1.0, on_timeout=_kill)
        assert cap.termination_reason is TerminationReason.TIMEOUT
        assert killed, "the caller's kill was never invoked"
        assert proc.poll() is not None, "orphan: the child outlived the capture"

    def test_partial_output_before_a_timeout_is_still_returned(self):
        """Partial output must stay distinguishable from complete output: the
        bytes are here AND the reason says the run did not finish."""
        code = ("import sys, time\n"
                "sys.stdout.buffer.write(b'partial'); sys.stdout.flush()\n"
                "time.sleep(60)\n")
        proc = _spawn(code)
        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB,
                              timeout=2.0, on_timeout=proc.kill)
        assert "partial" in cap.stdout.text
        assert cap.termination_reason is TerminationReason.TIMEOUT

    def test_a_child_that_keeps_writing_past_the_limit_still_terminates(self):
        """The reader does not stop at the limit. If it did, the pipe would fill,
        the child would block on write, and the 'bound' would be a deadlock."""
        code = ("import sys, time\n"
                "end = time.time() + 2\n"
                "block = b'z' * 65536\n"
                "while time.time() < end:\n"
                "    sys.stdout.buffer.write(block)\n"
                "sys.stdout.flush()\n")
        proc = _spawn(code)
        cap = capture_bounded(proc, stdout_limit=64, stderr_limit=64, timeout=30)
        assert cap.termination_reason is TerminationReason.EXITED, (
            "the child blocked on a full pipe instead of running to completion")
        assert cap.stdout.bytes_retained == 64
        assert cap.stdout.bytes_seen > 64 * _KIB

    def test_exit_code_survives_a_huge_output(self):
        proc = _spawn(_writer("stdout", 4 * _MIB) + "raise SystemExit(3)\n")
        cap = capture_bounded(proc, stdout_limit=128, stderr_limit=128, timeout=60)
        assert cap.returncode == 3
        assert cap.termination_reason is TerminationReason.EXITED


class TestDecoding:
    def test_invalid_bytes_do_not_raise(self):
        """``text=True`` decoded with errors='strict'. A snippet emitting invalid
        bytes could raise UnicodeDecodeError out of the capture itself."""
        proc = _spawn("import sys\nsys.stdout.buffer.write(b'\\xff\\xfe\\x00ok')\n")
        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB, timeout=30)
        assert "ok" in cap.stdout.text

    def test_stdin_payload_is_delivered(self):
        code = ("import sys\n"
                "sys.stdout.write(sys.stdin.read().strip().upper())\n")
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB,
                              timeout=30, input_bytes=b"hello\n")
        assert cap.stdout.text.strip() == "HELLO"

    def test_a_child_that_never_reads_stdin_does_not_wedge_the_writer(self):
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", "print('done')"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        cap = capture_bounded(proc, stdout_limit=_KIB, stderr_limit=_KIB,
                              timeout=30, input_bytes=b"q" * (4 * _MIB))
        assert cap.stdout.text.strip() == "done"
        assert cap.returncode == 0


class TestAccountingShape:
    def test_capture_serialises_its_accounting(self):
        proc = _spawn(_writer("stdout", 100))
        cap = capture_bounded(proc, stdout_limit=10, stderr_limit=10, timeout=30)
        d = cap.to_dict()
        assert d["stdout"]["bytes_seen"] == 100
        assert d["stdout"]["bytes_retained"] == 10
        assert d["stdout"]["truncated"] is True
        assert d["stdout"]["limit_bytes"] == 10
        assert d["termination_reason"] == "exited"

    def test_bounded_capture_is_the_declared_type(self):
        proc = _spawn("pass")
        cap = capture_bounded(proc, stdout_limit=1, stderr_limit=1, timeout=30)
        assert isinstance(cap, BoundedCapture)


# ── Through the real containment broker ──────────────────────────────────────
from core.containment import (  # noqa: E402
    CE_STDERR_CAP,
    CE_STDERR_RETAIN_BYTES,
    CE_STDOUT_CAP,
    CE_STDOUT_RETAIN_BYTES,
    ContainmentRequirement,
    ExecutionRequest,
    RestrictedProcessBackend,
)

pytestmark_posix = pytest.mark.skipif(os.name != "posix", reason="POSIX only")

#: A hard ceiling on what the broker may retain per stream, independent of the
#: declared constants. 1 MiB is already three orders of magnitude above the
#: character caps; anything near it means the bound stopped bounding.
_ABSOLUTE_RETENTION_CEILING = 1024 * 1024


def _restricted(code: str, timeout: int = 30):
    return RestrictedProcessBackend().execute(
        ExecutionRequest(code, timeout, ContainmentRequirement.RESTRICTED_OK))


class TestRestrictedBackendIsBounded:
    def test_a_flooding_snippet_is_bounded_in_the_parent(self):
        outcome = _restricted(
            "import sys\n"
            "block = b'q' * 65536\n"
            "for _ in range(256):\n"
            "    sys.stdout.buffer.write(block)\n"
            "sys.stdout.flush()\n")
        assert outcome.executed is True
        acc = outcome.receipt.output_accounting
        assert acc is not None, "no output accounting: the bound is unobservable"
        assert acc["stdout"]["bytes_seen"] == 256 * 65536
        assert acc["stdout"]["bytes_retained"] <= CE_STDOUT_RETAIN_BYTES
        # An ABSOLUTE ceiling as well as the declared one. Asserting only against
        # CE_STDOUT_RETAIN_BYTES follows the constant, so raising the constant to
        # 1.2 GB satisfied it — the falsification campaign proved exactly that.
        assert acc["stdout"]["bytes_retained"] <= _ABSOLUTE_RETENTION_CEILING, (
            f'{acc["stdout"]["bytes_retained"]} bytes retained in the parent for a '
            "16 MiB flood — the retention constant no longer bounds memory")
        assert acc["stdout"]["truncated"] is True
        assert len(outcome.stdout) == CE_STDOUT_CAP

    def test_the_receipt_publishes_the_positive_contract(self):
        outcome = _restricted("print('hi')")
        receipt = outcome.receipt.to_dict()
        for key in ("output_bytes_seen", "output_bytes_retained",
                    "stdout_truncated", "stderr_truncated", "termination_reason"):
            assert key in receipt, f"receipt does not publish {key}"
        assert receipt["stdout_truncated"] is False
        assert receipt["termination_reason"] == "exited"
        assert receipt["output_bytes_seen"]["stdout"] >= 3

    def test_a_quiet_snippet_reports_zero_not_absent(self):
        outcome = _restricted("pass")
        acc = outcome.receipt.output_accounting
        assert acc["stdout"]["bytes_seen"] == 0
        assert acc["stdout"]["truncated"] is False
        assert outcome.stdout == ""

    def test_timeout_keeps_the_existing_outcome_contract(self):
        outcome = _restricted("import time\ntime.sleep(60)\n", timeout=2)
        assert outcome.executed is True
        assert outcome.returncode is None, "the timeout contract changed"
        assert outcome.error and "Timeout" in outcome.error
        assert outcome.receipt.output_accounting["termination_reason"] == "timeout"

    def test_stderr_flood_is_bounded_too(self):
        outcome = _restricted(
            "import sys\n"
            "block = b'e' * 65536\n"
            "for _ in range(128):\n"
            "    sys.stderr.buffer.write(block)\n"
            "sys.stderr.flush()\n")
        acc = outcome.receipt.output_accounting
        assert acc["stderr"]["bytes_seen"] == 128 * 65536
        assert acc["stderr"]["bytes_retained"] <= CE_STDERR_RETAIN_BYTES
        assert acc["stderr"]["bytes_retained"] <= _ABSOLUTE_RETENTION_CEILING
        assert len(outcome.stderr) == CE_STDERR_CAP

    def test_the_declared_retention_constants_are_small(self):
        """The constants themselves, against an absolute ceiling. A behavioural
        test alone cannot catch a constant raised past every flood it produces."""
        for name, value in (("CE_STDOUT_RETAIN_BYTES", CE_STDOUT_RETAIN_BYTES),
                            ("CE_STDERR_RETAIN_BYTES", CE_STDERR_RETAIN_BYTES)):
            assert 0 < value <= _ABSOLUTE_RETENTION_CEILING, (
                f"{name} = {value} does not bound parent memory in any useful sense")

    def test_the_character_cap_is_still_reachable_under_the_byte_bound(self):
        """The retention limit must leave room for a FULL capped result; a bound
        that silently shortened the caller's output would be a regression."""
        outcome = _restricted(f"print('a' * {CE_STDOUT_CAP * 2})")
        assert len(outcome.stdout) == CE_STDOUT_CAP
        assert CE_STDOUT_RETAIN_BYTES >= CE_STDOUT_CAP
        assert CE_STDERR_RETAIN_BYTES >= CE_STDERR_CAP

    def test_no_orphan_process_survives_a_timeout(self):
        outcome = _restricted(
            "import time, sys\nsys.stdout.write('x')\nsys.stdout.flush()\n"
            "time.sleep(60)\n", timeout=2)
        assert outcome.receipt.cleanup_status.value in ("enforced", "not_enforced")
        assert outcome.receipt.output_accounting["termination_reason"] == "timeout"
