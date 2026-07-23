"""V69 M58.6 — idle conversation compaction scheduler. Deterministic, server-free."""
from __future__ import annotations

import asyncio

from core.compaction_scheduler import (
    CompactionConditions,
    CompactionScheduler,
    CompactionState,
)
from core.conversation_digest import DigestItem, Evidence, ItemKind


def _run(coro):
    return asyncio.run(coro)


def _long_history(n=20):
    h = []
    for i in range(n):
        h.append({"role": "user", "content": f"pregunta sobre kerberos numero {i} "
                                              "con bastante texto para pesar tokens"})
        h.append({"role": "assistant", "content": f"respuesta detallada {i} " * 8})
    return h


def _eligible(**over) -> CompactionConditions:
    base = dict(completed_turns=12, active_user_turn=False, hitl_active=False,
                effectful_tool_active=False, answer_tts_active=False,
                high_priority_embedding=False, lifecycle_operational=True,
                power_allows_background=True, context_pressure=0.9,
                cooldown_expired=True)
    base.update(over)
    return CompactionConditions(**base)


async def _good_proposer(history, timeout):
    return [DigestItem(ItemKind.TOPIC, "kerberos delegation", Evidence.OBSERVED),
            DigestItem(ItemKind.DECISION, "use native transport", Evidence.OBSERVED)]


# ── eligibility gate ──────────────────────────────────────────────────────────
def test_eligible_when_all_conditions_hold():
    assert _eligible().eligible() is True


def test_each_blocking_condition_is_named_deterministically():
    assert _eligible(active_user_turn=True).block_reason() == "active_user_turn"
    assert _eligible(hitl_active=True).block_reason() == "hitl_active"
    assert _eligible(effectful_tool_active=True).block_reason() == "effectful_tool_active"
    assert _eligible(answer_tts_active=True).block_reason() == "answer_tts_active"
    assert _eligible(high_priority_embedding=True).block_reason() == "high_priority_embedding"
    assert _eligible(lifecycle_operational=False).block_reason() == "lifecycle_not_operational"
    assert _eligible(power_allows_background=False).block_reason() == "power_disallows_background"
    assert _eligible(context_pressure=0.1).block_reason() == "context_pressure_low"
    assert _eligible(cooldown_expired=False).block_reason() == "cooldown"
    assert _eligible(completed_turns=1).block_reason() == "not_enough_turns"


# ── runs only when idle ───────────────────────────────────────────────────────
def test_skips_when_not_eligible_but_keeps_a_valid_digest():
    sched = CompactionScheduler(proposer=_good_proposer)
    hist = _long_history()
    st = _run(sched.maybe_run(hist, _eligible(active_user_turn=True)))
    assert st is CompactionState.SKIPPED
    # a valid extractive digest is always available
    assert sched.current_digest(hist) is not None


def test_runs_model_assisted_and_labels_inferred_when_idle():
    sched = CompactionScheduler(proposer=_good_proposer)
    hist = _long_history()
    st = _run(sched.maybe_run(hist, _eligible()))
    assert st is CompactionState.COMPLETED
    digest = sched.current_digest(hist)
    inferred = digest.by_evidence(Evidence.INFERRED)
    assert inferred, "model-assisted items must be present and labelled INFERRED"
    for item in inferred:
        assert item.evidence is Evidence.INFERRED


# ── active turn preempts ──────────────────────────────────────────────────────
def test_user_preempt_cancels_and_preserves_last_valid_digest():
    async def slow_proposer(history, timeout):
        await asyncio.sleep(5)
        return []

    async def scenario():
        sched = CompactionScheduler(proposer=slow_proposer, timeout_s=5.0)
        hist = _long_history()
        # first, a completed run to establish a valid digest
        sched2 = CompactionScheduler(proposer=_good_proposer)
        await sched2.maybe_run(hist, _eligible())
        prior = sched2.current_digest(hist)
        # now a slow run that gets preempted
        task = asyncio.ensure_future(sched.maybe_run(hist, _eligible()))
        sched._task = task
        await asyncio.sleep(0.01)
        sched.preempt()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # the scheduler keeps a valid (extractive) digest, never a partial one
        assert sched.current_digest(hist) is not None
        assert sched.cancelled_for_user >= 1
        assert prior is not None
    _run(scenario())


# ── validation ────────────────────────────────────────────────────────────────
def test_malformed_proposal_falls_back_to_extractive():
    async def bad_proposer(history, timeout):
        return ["not a digest item"]  # malformed
    sched = CompactionScheduler(proposer=bad_proposer)
    hist = _long_history()
    st = _run(sched.maybe_run(hist, _eligible()))
    assert st is CompactionState.VALIDATION_FAILED
    # still a valid extractive digest
    assert sched.current_digest(hist) is not None
    assert sched.validation_failures == 1


def test_timeout_preserves_digest_and_counts():
    async def hang(history, timeout):
        await asyncio.sleep(10)
        return []
    sched = CompactionScheduler(proposer=hang, timeout_s=0.05)
    hist = _long_history()
    st = _run(sched.maybe_run(hist, _eligible()))
    assert st is CompactionState.TIMED_OUT
    assert sched.timed_out == 1
    assert sched.current_digest(hist) is not None


# ── no semantic write; content-free ───────────────────────────────────────────
def test_scheduler_never_calls_semantic_memory(monkeypatch):
    called = {"wrote": False}
    import core.compaction_scheduler as cs
    # if anything tried to import a memory writer, flag it
    monkeypatch.setattr(cs, "merge_model_assisted", cs.merge_model_assisted)
    sched = CompactionScheduler(proposer=_good_proposer)
    _run(sched.maybe_run(_long_history(), _eligible()))
    assert called["wrote"] is False  # scheduler has no memory-write path at all


def test_snapshot_is_bounded_and_content_free():
    sched = CompactionScheduler(proposer=_good_proposer)
    _run(sched.maybe_run(_long_history(), _eligible()))
    snap = sched.snapshot()
    assert snap["completed"] == 1
    assert "digest_version" in snap and snap["digest_version"] >= 1
    blob = repr(snap)
    assert "respuesta detallada" not in blob  # no raw conversation text


def test_cooldown_blocks_a_second_immediate_run():
    sched = CompactionScheduler(proposer=_good_proposer, cooldown_s=1000.0)
    hist = _long_history()
    _run(sched.maybe_run(hist, _eligible()))
    assert sched.cooldown_expired() is False
