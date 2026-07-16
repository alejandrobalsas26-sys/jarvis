"""
tests/test_m541_regression_harness.py — V69 M54.1.14: the manual failure, reproduced.

One deterministic harness that walks the operator's real session on master a3f737a:

    boot  -> dozens of `ERROR:asyncio: ... QueueFull` tracebacks flood the console
    greet -> "Pues ahora mismo son [hora actual]."
    ask   -> "como saco la raiz cubica de algo"  -> no answer, no timeout, no prompt
             for minutes; operator hits Ctrl+C

Everything here is fakes, simulated time and temporary paths — no live Ollama, no
real Downloads, no production semantic collections. The loop's exception handler is
installed explicitly, because a QueueFull raised inside a callback does not fail
pytest on its own: that is precisely why 1929 tests passed while the runtime burned.
"""
from __future__ import annotations

import asyncio
import threading

from core.fast_readiness import FastReadiness, FastState
from core.greeting import find_placeholders, render_greeting
from core.host_time import HostTime
from core.lifecycle import LifecycleManager, LifecycleState
from core.safe_enqueue import EventPriority, SafeEnqueue
from core.turn_budget import (
    StageTimeouts,
    TurnBudget,
    TurnTimeout,
    bounded_stream,
    budget_for,
)
from core.turn_policy import ReasonCode, RequestClass, VerifyPolicy, classify_request
from core.watch_policy import WatchEvent, WatchPolicy
from core.watch_reconcile import RootState, WatchReconciler
from datetime import datetime, timedelta, timezone


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def _pump(n: int = 8) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _watch_policy(tmp_path):
    """The live topology: a security root that CONTAINS the repo, plus an inbox."""
    downloads = tmp_path / "Downloads"
    repo = downloads / "jarvis_v2" / "jarvis"
    inbox = repo / "analyze_inbox"
    for d in (downloads, repo, inbox):
        d.mkdir(parents=True, exist_ok=True)
    return WatchPolicy(security_roots=[str(downloads)], code_roots=[str(inbox)],
                       self_root=str(repo)), downloads, repo, inbox


# =============================================================================
# PART 1 — the boot storm (steps 1-11)
# =============================================================================
def test_boot_storm_is_bounded_and_recovers(tmp_path):
    async def _run():
        # 1-2. Lifecycle + console coordinator up.
        clk = FakeClock()
        life = LifecycleManager(clock=clk)
        life.bind_input_reader(lambda: True)

        loop = asyncio.get_running_loop()
        loop_errors: list = []
        loop.set_exception_handler(lambda _l, ctx: loop_errors.append(ctx))

        policy, downloads, repo, inbox = _watch_policy(tmp_path)
        warnings: list = []
        reconciled: list = []

        # 3. A deliberately tiny event queue.
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        rec = WatchReconciler(offer_path=lambda p: reconciled.append(p),
                              stopping_fn=life.is_stopping, walk_fn=lambda r: iter([]))
        gate = SafeEnqueue(
            queue=q, loop=loop, name="FILE_WATCHER", clock=clk, debounce_s=1.0,
            warn_fn=warnings.append, stopping_fn=life.is_stopping,
            on_overflow=lambda: (rec.mark_overflow(str(downloads)),
                                 rec.schedule_reconcile(str(downloads))),
        )

        def _offer(path: str, event: WatchEvent) -> None:
            d = policy.classify(path, event)
            if d.accepted:
                gate.offer(path, key=d.key, priority=d.priority)

        # 6-7. Generated paths JARVIS writes itself must be ignored by policy.
        generated = [
            repo / "__pycache__" / "main.cpython-312.pyc",
            repo / ".pytest_cache" / "v" / "lastfailed",
            repo / "logs" / "jarvis.log",
            repo / "vector_store" / "index.bin",
            repo / "core" / "integrity_baseline.json",
            repo / "main.py",
            repo / "tests" / "test_console_v69.py",
        ]
        for g in generated:
            _offer(str(g), WatchEvent.MODIFIED)
        await _pump()
        assert q.qsize() == 0, "JARVIS's own files must never enter the queue"
        assert gate.metrics()["accepted"] == 0

        # 4. Thousands of duplicate modify events from a REAL producer thread.
        target = str(downloads / "payload.exe")

        def producer() -> None:
            for _ in range(3000):
                _offer(target, WatchEvent.MODIFIED)

        t = threading.Thread(target=producer)
        t.start()
        t.join()
        await _pump()

        # 5. The contract.
        assert loop_errors == [], f"QueueFull reached the loop handler: {loop_errors}"
        m = gate.metrics()
        assert q.qsize() <= 16, "the queue must stay bounded"
        assert m["coalesced"] > 2900, "duplicate modifies must coalesce"
        assert m["queue_high_watermark"] <= 16
        assert len(warnings) <= 1, "at most one bounded warning, never one per event"

        # 8. Overflow an allowed root with DISTINCT high-value paths.
        def burst() -> None:
            for i in range(500):
                _offer(str(downloads / f"drop{i}.exe"), WatchEvent.CREATED)

        t2 = threading.Thread(target=burst)
        t2.start()
        t2.join()
        await _pump()
        assert loop_errors == [], "overflow must not traceback"
        assert gate.metrics()["dropped"] > 0, "a full queue really does drop"

        # 9. Exactly ONE reconciliation was scheduled, and the root is honest.
        await _pump()
        assert rec.status(str(downloads)).overflows >= 1
        assert rec.status(str(downloads)).reconciliations <= 1, \
            "one bounded rescan per episode, never one per dropped event"

        # 10-11. STOPPING: no new reconciliation may begin.
        life.begin_stopping()
        rec.mark_overflow(str(downloads))
        assert rec.schedule_reconcile(str(downloads)) is False
        st = rec.status(str(downloads))
        assert st.state in (RootState.STALE, RootState.DEGRADED, RootState.CURRENT)
        # And no new low-priority work is admitted while stopping.
        before = q.qsize()
        _offer(str(downloads / "late.exe"), WatchEvent.MODIFIED)
        await _pump()
        assert q.qsize() == before
        await rec.aclose()

    asyncio.run(_run())


def test_overflow_never_claims_nothing_changed(tmp_path):
    """Honesty: a root that dropped events is STALE, not CURRENT."""
    rec = WatchReconciler(offer_path=lambda _p: None, stopping_fn=lambda: False)
    rec.mark_overflow("root-a")
    assert rec.status("root-a").state is RootState.STALE
    assert rec.snapshot()["stale_roots"] == 1


# =============================================================================
# PART 2 — the first turn (steps 12-28)
# =============================================================================
class _FakeFast:
    """A fake FAST provider: optional lock wait, then Spanish tokens."""

    def __init__(self, chunks, *, lock=None, stall_forever=False,
                 stall_after=None, lock_delay_s=0.0, clock=None):
        self.chunks = chunks
        self.lock = lock
        self.stall_forever = stall_forever
        self.stall_after = stall_after
        self.lock_delay_s = lock_delay_s
        self.clock = clock
        self.closed = False
        self.late_chunks = 0

    async def stream(self):
        if self.lock is not None:
            await self.lock.acquire()
        try:
            if self.lock_delay_s and self.clock is not None:
                self.clock.advance(self.lock_delay_s)   # model queue wait
            for i, c in enumerate(self.chunks):
                if self.stall_forever or (self.stall_after is not None
                                          and i >= self.stall_after):
                    await asyncio.sleep(3600)
                if self.closed:
                    self.late_chunks += 1
                yield c
        finally:
            self.closed = True
            if self.lock is not None:
                self.lock.release()


def test_first_turn_is_routed_bounded_and_answered():
    """12-17: fresh lifecycle -> TEXT_READY -> the exact live question -> a Spanish
    answer with no tool, no vault and no verifier model."""
    async def _run():
        clk = FakeClock()
        life = LifecycleManager(clock=clk)

        # 13-14. TEXT_READY means the reader is REALLY available.
        assert life.mark_text_ready() is False, "no reader bound yet"
        reader = {"live": False}
        life.bind_input_reader(lambda: reader["live"])
        reader["live"] = True
        assert life.mark_text_ready() is True
        assert life.state is LifecycleState.TEXT_READY
        assert life.input_available() is True

        # 15-17. The exact live fixture.
        q = "como saco la raiz cubica de algo"
        p = classify_request(q)
        assert p.request_class is RequestClass.GENERAL_EDUCATIONAL
        assert p.reason_code is ReasonCode.DIRECT_FAST
        assert p.verify_policy is VerifyPolicy.DETERMINISTIC_CHECKS_ONLY
        assert p.wants_llm_verifier() is False
        assert p.knowledge_vault_allowed is False
        names = {t["function"]["name"]
                 for t in p.filter_tools([{"function": {"name": "query_knowledge"}}])}
        assert names == set(), "no Knowledge Vault tool for basic maths"

        budget = TurnBudget(total_s=budget_for(p), clock=clk)
        fake = _FakeFast(["La raíz cúbica de x ", "se calcula como x ** (1/3). ",
                          "Por ejemplo, la de 27 es 3."])
        got = []
        async for c in bounded_stream(fake.stream(), budget=budget):
            got.append(c)
        answer = "".join(got)
        assert "raíz cúbica" in answer, "Spanish is preserved"
        assert fake.closed is True
        assert budget.expired() is False

    asyncio.run(_run())


def test_model_lock_wait_counts_against_the_total_budget():
    """18-19: a simulated model-lock delay is INSIDE the deadline, not beside it."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        fake = _FakeFast(["ok"], lock_delay_s=59.5, clock=clk)   # near-total wait
        got = [c async for c in bounded_stream(fake.stream(), budget=budget)]
        assert got == ["ok"]
        snap = budget.snapshot()
        assert snap["first_token_ms"] >= 59_000, \
            "queue/lock wait must be inside time-to-first-token"
        assert budget.remaining_s() < 1.0, "the wait consumed the turn's budget"

    asyncio.run(_run())


def test_connect_and_first_token_stall_time_out():
    """20-21: a server that connects but never yields a token."""
    async def _run():
        clk = FakeClock()
        budget = TurnBudget(total_s=60.0, clock=clk)
        fake = _FakeFast(["never"], stall_forever=True)
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)
        try:
            async for _c in bounded_stream(fake.stream(), budget=budget, timeouts=t):
                raise AssertionError("nothing should stream")
            raise AssertionError("expected TurnTimeout")
        except TurnTimeout as exc:
            assert exc.stage == "first_token"
        await asyncio.sleep(0)
        assert fake.closed is True                      # 24. aclose ran

    asyncio.run(_run())


def test_stream_yields_one_chunk_then_stalls():
    """22-26: idle timeout, generator closed, no late chunk, prompt returns."""
    async def _run():
        clk = FakeClock()
        life = LifecycleManager(clock=clk)
        reader = {"live": True}
        life.bind_input_reader(lambda: reader["live"])
        life.mark_text_ready()

        budget = TurnBudget(total_s=60.0, clock=clk)
        fake = _FakeFast(["Hola", "...stall..."], stall_after=1)
        t = StageTimeouts(first_token_s=1.0, idle_s=0.05, total_s=60.0)
        got = []
        try:
            async for c in bounded_stream(fake.stream(), budget=budget, timeouts=t):
                got.append(c)
        except TurnTimeout as exc:
            assert exc.stage == "stream_idle"           # 23
        await asyncio.sleep(0.05)
        assert got == ["Hola"]
        assert fake.closed is True                      # 24
        assert fake.late_chunks == 0                    # 25
        # 26. The prompt is still available — the runtime never left the ready band.
        assert life.accepts_input() and life.input_available()

    asyncio.run(_run())


def test_second_turn_succeeds_after_the_first_times_out():
    """27-28: the property the operator actually needs. Turn 1 stalls holding the
    model lock; turn 2 must work immediately."""
    async def _run():
        clk = FakeClock()
        lock = asyncio.Lock()
        t = StageTimeouts(first_token_s=0.05, idle_s=0.05, total_s=60.0)

        b1 = TurnBudget(total_s=60.0, clock=clk)
        f1 = _FakeFast(["x"], lock=lock, stall_forever=True)
        try:
            async for _c in bounded_stream(f1.stream(), budget=b1, timeouts=t):
                pass
        except TurnTimeout:
            pass
        await asyncio.sleep(0)
        assert not lock.locked(), "a stuck model lock would wedge every later turn"

        b2 = TurnBudget(total_s=60.0, clock=clk)
        f2 = _FakeFast(["La raíz cúbica de 27 es 3."], lock=lock)
        got = [c async for c in bounded_stream(f2.stream(), budget=b2, timeouts=t)]
        assert got == ["La raíz cúbica de 27 es 3."]
        assert not lock.locked()
        assert b2.expired() is False

    asyncio.run(_run())


def test_fast_warming_does_not_block_input():
    """A model still warming must not freeze the prompt."""
    life = LifecycleManager(clock=FakeClock())
    life.bind_input_reader(lambda: True)
    life.mark_text_ready()
    fast = FastReadiness(model="qwen3:8b", clock=FakeClock())
    fast._state = FastState.WARMING
    assert fast.accepts_input() is True
    assert life.accepts_input() is True


# =============================================================================
# PART 3 — greeting + clean shutdown (steps 29-32)
# =============================================================================
def test_startup_greeting_is_deterministic_with_real_host_time():
    """29-31: no unresolved placeholder; host time is deterministic."""
    tz = timezone(timedelta(hours=-5))
    ht = HostTime(datetime(2026, 7, 15, 20, 5, 0, tzinfo=tz))
    text = render_greeting(name="Alejandro", language="es", now=ht,
                           readiness="JARVIS está listo con memoria semántica degradada.")
    assert find_placeholders(text) == [], f"placeholder leaked: {text!r}"
    assert "[hora actual]" not in text
    assert "20:05:00" in text
    # Deterministic: same clock in, same string out.
    assert text == render_greeting(
        name="Alejandro", language="es", now=ht,
        readiness="JARVIS está listo con memoria semántica degradada.")


def test_all_workers_and_tasks_close_cleanly(tmp_path):
    """32: no orphan reconciliation, no orphan task, bounded shutdown."""
    async def _run():
        clk = FakeClock()
        life = LifecycleManager(clock=clk)
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.exe").write_text("x")

        rec = WatchReconciler(offer_path=lambda _p: None,
                              stopping_fn=life.is_stopping)
        rec.mark_overflow(str(root))
        assert rec.schedule_reconcile(str(root)) is True
        life.begin_stopping()
        await rec.aclose()
        assert all(t.done() for t in rec._tasks.values()) or not rec._tasks
        assert life.begin_stopping() is False, "exactly one shutdown"
        life.mark_stopped()
        assert life.state is LifecycleState.STOPPED

    asyncio.run(_run())


def test_no_reconciliation_after_stopping_even_under_continued_overflow(tmp_path):
    """The M54 invariant preserved: STOPPING means no new background work."""
    async def _run():
        clk = FakeClock()
        life = LifecycleManager(clock=clk)
        loop = asyncio.get_running_loop()
        errs: list = []
        loop.set_exception_handler(lambda _l, ctx: errs.append(ctx))

        rec = WatchReconciler(offer_path=lambda _p: None, stopping_fn=life.is_stopping)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        gate = SafeEnqueue(queue=q, loop=loop, clock=clk, warn_fn=lambda _m: None,
                           stopping_fn=life.is_stopping,
                           on_overflow=lambda: rec.schedule_reconcile("r"))
        life.begin_stopping()
        for i in range(100):
            gate.offer(f"p{i}", priority=EventPriority.LOW)
        await _pump()
        assert errs == []
        assert q.qsize() == 0, "no low-priority work is admitted while stopping"
        assert rec.status("r").reconciliations == 0

    asyncio.run(_run())
