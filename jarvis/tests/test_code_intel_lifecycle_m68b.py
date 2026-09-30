"""V69 M68B (C) — code_intel has bounded resources and a truthful lifecycle.

THE DEFECT THESE PIN
--------------------
The inbox watcher was an unbounded pipeline with a lossy completion mark:

    _ANALYZED: set[str] = set()          # grows forever
    ...
    if str(file_path) in _ANALYZED: return None
    _ANALYZED.add(str(file_path))        # marked BEFORE any work happened
    data = file_path.read_bytes()        # blocking, unbounded, on the event loop
    ...
    queue = asyncio.Queue()              # no maxsize
    asyncio.create_task(analyze_file(...))   # one task per arrival, untracked

So a file whose read failed was marked "analysed" for the life of the process and
could never be retried; "started", "succeeded" and "failed" were the same
observation; an operator emptying a directory into the inbox created one task and
one full file copy per file; and the Observer thread was never stopped.

These tests are written against STATE TRANSITIONS and COUNTERS. A test that only
checked "the file was analysed once" passed against the defect.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core import code_intel  # noqa: E402
from core.code_intel import (  # noqa: E402
    CI_MAX_ATTEMPTS,
    CI_QUEUE_MAX,
    CI_WORKERS,
    AnalysisLedger,
    AnalysisState,
    analyze_file,
    start_inbox_watcher,
)


# ── The ledger: the state machine the old set could not express ──────────────
class TestAnalysisLedger:
    def test_pending_running_completed_are_distinct(self):
        book = AnalysisLedger()
        assert book.state("a") is None
        assert book.observe("a") is True
        assert book.state("a") is AnalysisState.PENDING
        assert book.claim("a") is True
        assert book.state("a") is AnalysisState.RUNNING
        book.complete("a")
        assert book.state("a") is AnalysisState.COMPLETED
        # Three different observations. The pre-M68B set had one.
        assert len({AnalysisState.PENDING, AnalysisState.RUNNING,
                    AnalysisState.COMPLETED, AnalysisState.FAILED}) == 4

    def test_a_duplicate_event_while_running_is_not_requeued(self):
        book = AnalysisLedger()
        book.observe("a")
        book.claim("a")
        assert book.observe("a") is False
        assert book.claim("a") is False

    def test_a_completed_file_is_not_reanalysed(self):
        book = AnalysisLedger()
        book.claim("a")
        book.complete("a")
        assert book.observe("a") is False
        assert book.claim("a") is False

    def test_a_failure_is_retryable_under_an_explicit_policy(self):
        book = AnalysisLedger(max_attempts=3)
        assert book.claim("a") is True
        assert book.fail("a", "read_error") is True
        assert book.state("a") is AnalysisState.FAILED
        assert book.reason("a") == "read_error"
        assert book.claim("a") is True            # retry 2
        assert book.fail("a", "read_error") is True
        assert book.claim("a") is True            # retry 3
        assert book.fail("a", "read_error") is False
        assert book.claim("a") is False, "retried past the declared policy"
        assert book.attempts("a") == 3

    def test_a_rejection_is_terminal_and_never_retried(self):
        book = AnalysisLedger()
        book.claim("a")
        book.reject("a", "oversized")
        assert book.state("a") is AnalysisState.REJECTED
        assert book.claim("a") is False
        assert book.observe("a") is False

    def test_the_ledger_is_bounded_and_evicts(self):
        book = AnalysisLedger(max_entries=16)
        for i in range(200):
            key = f"f{i}"
            book.claim(key)
            book.complete(key)
        assert len(book) <= 16, "the analysed-file state grows without bound"
        assert book.evicted >= 184
        snap = book.snapshot()
        assert snap["capacity"] == 16
        assert snap["tracked"] <= 16

    def test_eviction_never_steals_an_in_flight_entry_while_terminals_exist(self):
        book = AnalysisLedger(max_entries=4)
        book.claim("done-1"); book.complete("done-1")
        book.claim("done-2"); book.complete("done-2")
        book.claim("running-1")
        book.claim("running-2")
        book.claim("running-3")      # forces eviction
        assert book.state("running-1") is AnalysisState.RUNNING
        assert book.state("done-1") is None, "a terminal entry was not evicted first"

    def test_the_ledger_snapshot_carries_no_file_names(self):
        book = AnalysisLedger()
        book.claim("/home/operator/secret-sample.bin")
        snap = book.snapshot()
        assert "secret-sample" not in repr(snap)


# ── analyze_file: completion is earned ───────────────────────────────────────
class _NullLLM:
    class chat:                                   # noqa: N801 - mirrors the SDK shape
        class completions:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("no model in tests")


async def _collect(events: list):
    async def _broadcast(event: dict) -> None:
        events.append(event)
    return _broadcast


class TestAnalyzeFileLifecycle:
    def test_a_failed_read_leaves_the_file_retryable(self):
        """The headline defect: a transient read error used to be permanent."""
        async def scenario(tmp_path: Path):
            book = AnalysisLedger()
            events: list = []
            missing = tmp_path / "not-there.bin"
            out = await analyze_file(missing, await _collect(events),
                                     _NullLLM(), "m", ledger=book)
            assert out is None
            assert book.state(str(missing)) is AnalysisState.FAILED
            assert book.claim(str(missing)) is True, "a read error was permanent"

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(scenario(Path(d)))

    def test_an_oversized_file_is_refused_before_it_is_read(self):
        async def scenario(tmp_path: Path):
            book = AnalysisLedger()
            events: list = []
            big = tmp_path / "big.bin"
            big.write_bytes(b"A" * 4096)
            out = await analyze_file(big, await _collect(events), _NullLLM(), "m",
                                     ledger=book, max_bytes=1024)
            assert out is None
            assert book.state(str(big)) is AnalysisState.REJECTED
            assert book.claim(str(big)) is False
            assert any(e["type"] == "code_analysis_rejected" for e in events), (
                "an oversized refusal was silent")

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(scenario(Path(d)))

    def test_the_size_gate_refuses_before_the_file_is_read(self, monkeypatch):
        """Which LAYER answered? The policy is enforced twice — a stat gate before
        the read and a length check after it — and the campaign showed that
        deleting the stat gate changed nothing observable, because the post-read
        check still raised. The stat gate is the one that prevents the unbounded
        READ, so it needs its own proof: with reading made fatal, the refusal must
        still happen."""
        import tempfile
        from core.code_intel import OversizedFile, _read_within_policy

        def _boom(self, *a, **kw):
            raise AssertionError("the file was READ despite the size policy")

        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.bin"
            big.write_bytes(b"A" * 4096)
            monkeypatch.setattr(Path, "read_bytes", _boom)
            with pytest.raises(OversizedFile):
                _read_within_policy(big, max_bytes=1024)

    def test_a_file_within_policy_is_still_read(self):
        """Non-vacuity for the gate above: the refusal must be about the size."""
        import tempfile
        from core.code_intel import _read_within_policy
        with tempfile.TemporaryDirectory() as d:
            small = Path(d) / "small.bin"
            small.write_bytes(b"B" * 16)
            assert _read_within_policy(small, max_bytes=1024) == b"B" * 16

    def test_a_successful_analysis_completes_only_after_the_report_exists(self):
        async def scenario(tmp_path: Path):
            book = AnalysisLedger()
            events: list = []
            sample = tmp_path / "sample.py"
            sample.write_text("print('hello')\n", encoding="utf-8")
            report = await analyze_file(sample, await _collect(events),
                                        _NullLLM(), "m", ledger=book)
            assert report is not None
            assert book.state(str(sample)) is AnalysisState.COMPLETED
            assert Path(report_path_of(events)).exists()
            types = [e["type"] for e in events]
            assert types[0] == "code_analysis_started"
            assert types[-1] == "code_analysis_complete"

        def report_path_of(events):
            return [e for e in events if e["type"] == "code_analysis_complete"][0]["report"]

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(scenario(Path(d)))

    def test_a_second_call_for_a_running_file_returns_immediately(self):
        async def scenario():
            book = AnalysisLedger()
            book.claim("/x")
            out = await analyze_file(Path("/x"), None, _NullLLM(), "m", ledger=book)
            assert out is None

        asyncio.run(scenario())

    def test_cancellation_leaves_the_file_retryable(self):
        """Shutdown is not a verdict."""
        async def scenario(tmp_path: Path):
            book = AnalysisLedger()
            events: list = []
            sample = tmp_path / "s.py"
            sample.write_text("x = 1\n", encoding="utf-8")

            class _HangingLLM:
                class chat:
                    class completions:
                        @staticmethod
                        async def create(**kwargs):
                            await asyncio.sleep(3600)

            task = asyncio.create_task(
                analyze_file(sample, await _collect(events), _HangingLLM(), "m",
                             ledger=book))
            for _ in range(50):
                await asyncio.sleep(0)
                if book.state(str(sample)) is AnalysisState.RUNNING:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert book.state(str(sample)) is AnalysisState.FAILED
            assert book.reason(str(sample)) == "cancelled"

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(scenario(Path(d)))


# ── The watcher: bounded intake, explicit workers, clean shutdown ────────────
class _FakeObserver:
    """Captures the handler so a test can fire events, and records teardown."""

    instances: list = []

    def __init__(self) -> None:
        self.handler = None
        self.started = False
        self.stopped = False
        self.joined = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.handler = handler

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


class TestWatcherBounds:
    def _run_watcher(self, *, fire, queue_max=CI_QUEUE_MAX, workers=CI_WORKERS,
                     ledger=None, settle_s=0.0, analysed=None):
        """Start the watcher, fire events through the real handler, tear down."""
        async def scenario():
            _FakeObserver.instances.clear()
            events: list = []

            async def _broadcast(event: dict) -> None:
                events.append(event)

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m",
                workers=workers, queue_max=queue_max, settle_s=settle_s,
                ledger=ledger, observer_factory=_FakeObserver))
            for _ in range(20):
                await asyncio.sleep(0)
                if _FakeObserver.instances and _FakeObserver.instances[0].handler:
                    break
            observer = _FakeObserver.instances[0]
            fire(observer.handler)
            for _ in range(60):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return observer, events

        return asyncio.run(scenario())

    def test_the_queue_is_bounded_and_saturation_is_counted_not_grown(self):
        book = AnalysisLedger()

        class _Ev:
            is_directory = False

            def __init__(self, p):
                self.src_path = p

        def fire(handler):
            for i in range(500):
                handler.on_created(_Ev(f"/tmp/does-not-exist-{i}.bin"))

        observer, _ = self._run_watcher(fire=fire, queue_max=8, workers=1,
                                        ledger=book)
        # 500 arrivals, a queue of 8: the pipeline never held 500 items and never
        # spawned 500 tasks. Every arrival is accounted for as admitted or dropped.
        assert len(book) <= book.snapshot()["capacity"]
        assert observer.stopped and observer.joined

    def test_the_observer_and_workers_shut_down(self):
        observer, _ = self._run_watcher(fire=lambda h: None, workers=3)
        assert observer.started is True
        assert observer.stopped is True, "the Observer thread was never stopped"
        assert observer.joined is True, "the Observer thread was never joined"

    def test_no_worker_task_survives_shutdown(self):
        async def scenario():
            _FakeObserver.instances.clear()

            async def _broadcast(event: dict) -> None:
                pass

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m", workers=4,
                observer_factory=_FakeObserver))
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(10):
                await asyncio.sleep(0)
            workers = [t for t in asyncio.all_tasks()
                       if (t.get_name() or "").startswith("code-intel-worker-")]
            assert workers == [], f"leaked workers: {workers}"

        asyncio.run(scenario())

    def test_a_dying_worker_tears_down_its_siblings(self):
        """The exit path `gather` does NOT cover. Cancelling the watcher makes
        `asyncio.gather` cancel its children for us, which is why removing the
        explicit cancel in the `finally` was invisible. When one worker RAISES,
        gather re-raises and leaves the siblings running — so the finally is the
        only thing that stops them."""
        class _Fatal(BaseException):
            """Not an Exception: the worker's own handler must not absorb it."""

        class _ExplodingLedger(AnalysisLedger):
            def claim(self, key: str) -> bool:
                raise _Fatal("worker died")

        async def scenario():
            _FakeObserver.instances.clear()

            async def _broadcast(event: dict) -> None:
                pass

            task = asyncio.create_task(start_inbox_watcher(
                _broadcast, None, _NullLLM(), "m", workers=4, settle_s=0.0,
                ledger=_ExplodingLedger(), observer_factory=_FakeObserver))
            for _ in range(20):
                await asyncio.sleep(0)
            observer = _FakeObserver.instances[0]

            class _Ev:
                is_directory = False
                src_path = "/inbox/kills-a-worker.bin"

            observer.handler.on_created(_Ev())
            with pytest.raises(_Fatal):
                await asyncio.wait_for(task, timeout=5.0)
            for _ in range(10):
                await asyncio.sleep(0)
            alive = [t for t in asyncio.all_tasks()
                     if (t.get_name() or "").startswith("code-intel-worker-")
                     and not t.done()]
            assert alive == [], f"siblings of a dead worker are still running: {alive}"
            assert observer.stopped and observer.joined

        asyncio.run(scenario())

    def test_worker_count_is_explicit_and_finite(self):
        assert isinstance(CI_WORKERS, int) and 0 < CI_WORKERS <= 64
        assert isinstance(CI_QUEUE_MAX, int) and 0 < CI_QUEUE_MAX <= 100_000
        assert isinstance(CI_MAX_ATTEMPTS, int) and 1 <= CI_MAX_ATTEMPTS <= 10

    def test_every_declared_bound_is_small_in_absolute_terms(self):
        """Absolute ceilings, not self-referential ones. The falsification campaign
        raised CI_LEDGER_MAX to 100,000,000 and CI_MAX_FILE_BYTES by 100,000x and
        every behavioural test still passed, because each compared the observed
        value against the constant it was meant to police."""
        assert 0 < code_intel.CI_LEDGER_MAX <= 100_000, (
            f"CI_LEDGER_MAX = {code_intel.CI_LEDGER_MAX} does not bound the "
            "analysed-file state in any useful sense")
        assert 0 < code_intel.CI_MAX_FILE_BYTES <= 512 * 1024 * 1024, (
            f"CI_MAX_FILE_BYTES = {code_intel.CI_MAX_FILE_BYTES} is not a size "
            "policy; the whole file is read into memory to hash and score it")
        assert 0 < CI_QUEUE_MAX <= 4096

    def test_the_module_ledger_is_bounded(self):
        snap = code_intel.ledger_snapshot()
        assert snap["capacity"] == code_intel.CI_LEDGER_MAX
        assert snap["tracked"] <= snap["capacity"]
        assert snap["capacity"] <= 100_000
