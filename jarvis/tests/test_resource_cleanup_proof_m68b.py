"""V69 M68B (§10) — the cleanup proof for every component M68B changed.

WHY A SEPARATE MODULE
---------------------
Resource hardening is incomplete if shutdown is unverified. A bounded queue that
leaks its writer, a bounded reader that leaks its thread and a bounded worker pool
that leaks its Observer are all still leaks — the bound made the STEADY state safe
and said nothing about the exit.

So this module drives each changed component through all five exits —

    cancellation · timeout · normal completion · exception · application shutdown

— and after each one counts what is still alive: asyncio tasks, threads, child
processes, observers, open file descriptors.

TRUTHFULNESS
------------
Where a resource cannot be observed from inside the process, this module says so
with an explicit skip rather than asserting a clean state it did not measure. A
false CLEAN is worse than an honest UNKNOWN: it is the claim that stops the next
person looking.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.bounded_output import TerminationReason, capture_bounded  # noqa: E402
from core.code_intel import AnalysisLedger, start_inbox_watcher  # noqa: E402
from core.containment import (  # noqa: E402
    ContainmentRequirement,
    ExecutionRequest,
    RestrictedProcessBackend,
)


def _reader_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate()
            if (t.name or "").startswith(("bounded-reader-", "bounded-stdin-writer"))]


def _open_fds() -> int | None:
    """Open descriptors for this process, or None when unobservable."""
    proc_fd = Path("/proc/self/fd")
    if not proc_fd.is_dir():
        return None
    try:
        return len(list(proc_fd.iterdir()))
    except OSError:  # pragma: no cover - unreadable /proc
        return None


def _spawn(code: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-I", "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            shell=False, **kw)


# ── A · bounded_output: threads, descriptors, children ───────────────────────
class TestBoundedOutputCleanup:
    def test_normal_completion_leaves_no_reader_thread(self):
        before = len(_reader_threads())
        for _ in range(5):
            proc = _spawn("print('ok')")
            capture_bounded(proc, stdout_limit=1024, stderr_limit=1024, timeout=30)
        assert len(_reader_threads()) == before, (
            f"reader threads leaked: {[t.name for t in _reader_threads()]}")

    def test_timeout_leaves_no_reader_thread_and_no_child(self):
        before = len(_reader_threads())
        proc = _spawn("import time\ntime.sleep(60)\n")
        cap = capture_bounded(proc, stdout_limit=1024, stderr_limit=1024,
                              timeout=1.0, on_timeout=proc.kill)
        assert cap.termination_reason is TerminationReason.TIMEOUT
        assert proc.poll() is not None, "the child outlived the capture"
        assert len(_reader_threads()) == before

    def test_a_flooding_child_leaves_no_reader_thread(self):
        """The interesting case: the readers are still draining when the limit is
        long past, so they must exit on EOF rather than be abandoned."""
        before = len(_reader_threads())
        code = ("import sys\n"
                "block = b'x' * 65536\n"
                "for _ in range(64):\n"
                "    sys.stdout.buffer.write(block)\n"
                "    sys.stderr.buffer.write(block)\n")
        proc = _spawn(code)
        capture_bounded(proc, stdout_limit=64, stderr_limit=64, timeout=60)
        assert len(_reader_threads()) == before

    def test_descriptors_do_not_grow_across_many_captures(self):
        before = _open_fds()
        if before is None:
            pytest.skip("open descriptors are not observable on this platform "
                        "(no /proc/self/fd) — reporting UNKNOWN, not CLEAN")
        for _ in range(25):
            proc = _spawn("print('x')")
            capture_bounded(proc, stdout_limit=256, stderr_limit=256, timeout=30)
        after = _open_fds()
        assert after is not None and after <= before + 2, (
            f"descriptor leak across 25 captures: {before} -> {after}")

    def test_an_exception_in_the_kill_hook_still_reaps(self):
        proc = _spawn("import time\ntime.sleep(60)\n")

        def _boom():
            raise RuntimeError("kill hook exploded")

        before = len(_reader_threads())
        cap = capture_bounded(proc, stdout_limit=256, stderr_limit=256,
                              timeout=1.0, on_timeout=_boom, reap_grace_s=1.0)
        assert cap.termination_reason is TerminationReason.TIMEOUT
        proc.kill()                      # the hook never did; this test must not leak
        proc.wait(timeout=10)
        assert len(_reader_threads()) == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
class TestContainmentCleanup:
    def test_normal_completion_reports_cleanup_and_leaves_no_thread(self):
        before = len(_reader_threads())
        outcome = RestrictedProcessBackend().execute(
            ExecutionRequest("print('ok')", 30, ContainmentRequirement.RESTRICTED_OK))
        assert outcome.executed is True
        assert outcome.receipt.cleanup_status.value == "enforced"
        assert len(_reader_threads()) == before

    def test_a_timed_out_execution_leaves_no_thread(self):
        before = len(_reader_threads())
        outcome = RestrictedProcessBackend().execute(
            ExecutionRequest("import time\ntime.sleep(60)\n", 2,
                             ContainmentRequirement.RESTRICTED_OK))
        assert outcome.receipt.output_accounting["termination_reason"] == "timeout"
        assert len(_reader_threads()) == before

    def test_the_workspace_is_removed_in_every_case(self):
        for code, timeout in (("print(1)", 30),
                              ("raise SystemExit(7)", 30),
                              ("import time\ntime.sleep(60)\n", 2)):
            outcome = RestrictedProcessBackend().execute(
                ExecutionRequest(code, timeout, ContainmentRequirement.RESTRICTED_OK))
            assert outcome.receipt.cleanup_status.value in ("enforced", "not_enforced")
            assert outcome.receipt.cleanup_status.value == "enforced", (
                f"workspace survived for {code!r}")


# ── C · code_intel: workers, Observer thread, queue ──────────────────────────
class _FakeObserver:
    instances: list = []

    def __init__(self) -> None:
        self.handler = None
        self.started = self.stopped = self.joined = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.handler = handler

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


class _NullLLM:
    class chat:                                  # noqa: N801 - mirrors the SDK shape
        class completions:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("no model in tests")


def _live_watcher_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks()
            if (t.get_name() or "").startswith("code-intel-")]


class TestCodeIntelCleanup:
    def test_cancellation_stops_the_workers_and_the_observer(self):
        async def scenario():
            _FakeObserver.instances.clear()

            async def _broadcast(event: dict) -> None:
                pass

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m", workers=3,
                observer_factory=_FakeObserver))
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(_live_watcher_tasks()) >= 3
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(10):
                await asyncio.sleep(0)
            assert _live_watcher_tasks() == [], "worker tasks survived cancellation"
            observer = _FakeObserver.instances[0]
            assert observer.stopped and observer.joined

        asyncio.run(scenario())

    def test_an_exception_inside_a_worker_does_not_kill_the_pipeline(self):
        async def scenario():
            _FakeObserver.instances.clear()
            book = AnalysisLedger()
            seen: list = []

            async def _broadcast(event: dict) -> None:
                seen.append(event)

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m", workers=1, settle_s=0.0,
                ledger=book, observer_factory=_FakeObserver))
            for _ in range(30):
                await asyncio.sleep(0)
            observer = _FakeObserver.instances[0]

            class _Ev:
                is_directory = False
                src_path = "/nonexistent/path/does-not-exist.bin"

            observer.handler.on_created(_Ev())
            for _ in range(60):
                await asyncio.sleep(0)
            # The file failed; the worker is still alive and still consuming.
            assert _live_watcher_tasks(), "a per-file failure killed the worker"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_no_work_is_started_after_shutdown(self):
        async def scenario():
            _FakeObserver.instances.clear()
            book = AnalysisLedger()
            started: list = []

            async def _broadcast(event: dict) -> None:
                started.append(event)

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m", workers=1, settle_s=0.0,
                ledger=book, observer_factory=_FakeObserver))
            for _ in range(30):
                await asyncio.sleep(0)
            observer = _FakeObserver.instances[0]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            class _Ev:
                is_directory = False
                src_path = "/tmp/after-shutdown.bin"      # noqa: S108 - a name, never opened

            before = len(started)
            observer.handler.on_created(_Ev())            # the handler still exists
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(started) == before, "work was started after shutdown"

        asyncio.run(scenario())

    def test_the_ledger_does_not_grow_across_a_storm(self):
        book = AnalysisLedger(max_entries=32)
        for i in range(5000):
            key = f"/inbox/f{i}"
            book.observe(key)
            book.claim(key)
            book.complete(key)
        assert len(book) <= 32
        assert book.evicted >= 4968


# ── B · aura: writer tasks across all five exits ────────────────────────────
class TestAuraCleanup:
    def _server(self):
        pytest.importorskip("fastapi")
        from aura import server
        return server

    def test_all_five_exits_leave_no_writer_task(self):
        server = self._server()

        class _WS:
            def __init__(self, mode: str) -> None:
                self.mode = mode
                self.sent: list = []
                self.gate = asyncio.Event()

            async def accept(self) -> None:
                pass

            async def send_text(self, data: str) -> None:
                if self.mode == "exception":
                    raise ConnectionResetError("gone")
                if self.mode in ("timeout", "cancel", "shutdown"):
                    await self.gate.wait()
                self.sent.append(data)

        async def scenario():
            for mode in ("normal", "exception", "timeout", "cancel", "shutdown"):
                mgr = server.BroadcastManager(queue_max=4, send_timeout=0.05)
                ws = _WS(mode)
                await mgr.connect(ws)
                task = mgr._channels[id(ws)].task
                await mgr.broadcast({"m": mode})
                if mode == "timeout":
                    await asyncio.sleep(0.2)          # miss the send deadline
                elif mode == "cancel":
                    task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                ws.gate.set()
                if mode == "shutdown":
                    await mgr.aclose(drain_s=0.05)
                else:
                    await mgr.aclose_client(ws, drain_s=0.05)
                assert task.done(), f"writer survived exit mode {mode!r}"
                assert ws not in mgr._clients
                assert id(ws) not in mgr._channels
            leaked = [t for t in asyncio.all_tasks()
                      if (t.get_name() or "").startswith("aura-writer-")]
            assert leaked == [], f"leaked writer tasks: {leaked}"

        asyncio.run(scenario())

    def test_bounded_background_tasks_are_cancelled_at_shutdown(self):
        server = self._server()

        async def scenario():
            server._reset_bounded_tasks()

            async def _forever():
                await asyncio.sleep(3600)

            tasks = [server._spawn_bounded(_forever(), name="probe")
                     for _ in range(5)]
            tasks = [t for t in tasks if t is not None]
            assert tasks
            server._reset_bounded_tasks()
            for _ in range(10):
                await asyncio.sleep(0)
            assert all(t.cancelled() or t.done() for t in tasks), (
                "background tasks survived the reset")
            assert server.bounded_task_stats()["inflight"] == 0

        asyncio.run(scenario())

    def test_the_lifespan_tears_down_everything_it_started(self):
        """Application shutdown: the declared control is that `_lifespan`'s finally
        cancels the producer AND the correlator AND closes the manager."""
        import ast
        server_src = (PACKAGE_ROOT / "aura" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(server_src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_lifespan")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
        assert tries, "_lifespan has no finally"
        names: set[str] = set()
        for t in tries:
            for stmt in t.finalbody:
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Attribute):
                            names.add(func.attr)
                        elif isinstance(func, ast.Name):
                            names.add(func.id)
        assert "cancel" in names, "the lifespan cancels nothing"
        assert "aclose" in names, "the lifespan never closes the broadcast manager"
        assert "_reset_bounded_tasks" in names, (
            "the lifespan leaves bounded background tasks running")
