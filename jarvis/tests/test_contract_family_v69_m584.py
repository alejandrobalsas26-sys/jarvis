"""V69 M58.4 — contract-family prefix prewarm. Deterministic, server-free.

Uses an injected runner so the FamilyPrewarm state machine, governor priority,
once-per-identity guard, cancellation and no-boot-storm properties are all provable
without a live Ollama server.
"""
from __future__ import annotations

import asyncio


from core.contract_family import (
    ContractFamily,
    FamilyPrewarm,
    FamilyPrewarmMode,
    FamilyRecord,
    FamilyState,
    PREWARMABLE_FAMILIES,
    family_of,
    family_prewarm_messages,
    representative_contract,
)
from core.response_contract import ResponseContract


def _run(coro):
    return asyncio.run(coro)


def _fake_runner(record_calls):
    async def runner(family, *, model, num_ctx, keep_alive, timeout_s, language,
                     language_directive, compatibility_identity, prompt_fingerprint,
                     power_profile="UNKNOWN", cancellation=None, client=None):
        record_calls.append({"family": family, "model": model, "num_ctx": num_ctx,
                             "identity": compatibility_identity})
        return FamilyRecord(family=family.value, model=model, num_ctx=num_ctx,
                            state=FamilyState.READY, success=True,
                            first_token_ms=120.0, prompt_eval_ms=200.0,
                            compatibility_identity=compatibility_identity,
                            prompt_fingerprint=prompt_fingerprint)
    return runner


def _pw(mode=FamilyPrewarmMode.BACKGROUND_FAMILIES, calls=None, **kw):
    calls = [] if calls is None else calls
    return FamilyPrewarm(model="qwen3:8b", mode=mode, num_ctx=2048,
                         runner=_fake_runner(calls), **kw), calls


# ── family grouping ───────────────────────────────────────────────────────────
def test_families_group_the_ten_contracts():
    assert family_of(ResponseContract.INSTANT) is ContractFamily.CONCISE
    assert family_of(ResponseContract.BRIEF) is ContractFamily.CONCISE
    assert family_of(ResponseContract.ERROR_RECOVERY) is ContractFamily.CONCISE
    assert family_of(ResponseContract.STANDARD) is ContractFamily.EXPLANATORY
    assert family_of(ResponseContract.CODE) is ContractFamily.SPECIALIZED
    assert family_of(ResponseContract.DEEP) is ContractFamily.SPECIALIZED
    # every contract maps somewhere
    for c in ResponseContract:
        assert isinstance(family_of(c), ContractFamily)


def test_only_native_fast_families_are_prewarmable():
    assert set(PREWARMABLE_FAMILIES) == {ContractFamily.CONCISE,
                                         ContractFamily.EXPLANATORY}
    assert ContractFamily.SPECIALIZED not in PREWARMABLE_FAMILIES


def test_family_prewarm_uses_the_real_stable_prefix_not_ok():
    msgs = family_prewarm_messages(ContractFamily.CONCISE,
                                   language_directive="Responde en español.")
    system = msgs[0]["content"]
    assert msgs[0]["role"] == "system"
    # the real stable prefix + delta, never the meaningless "ok"
    assert "local AI assistant" in system
    assert "[RESPONSE_CONTRACT]" in system
    assert representative_contract(ContractFamily.CONCISE).value in system
    # the dynamic host clock is NOT part of the warmed prefix
    assert "HOST CLOCK" not in system
    assert msgs[1]["content"] != "ok"


# ── no boot storm; concise first ──────────────────────────────────────────────
async def _co_test_background_families_warms_concise_then_explanatory_not_ten():
    pw, calls = _pw(FamilyPrewarmMode.BACKGROUND_FAMILIES)
    recs = await pw.warm_planned()
    assert [c["family"] for c in calls] == [ContractFamily.CONCISE,
                                            ContractFamily.EXPLANATORY]
    assert len(calls) == 2  # two families, NOT ten contracts
    assert all(r.state is FamilyState.READY for r in recs)


async def _co_test_concise_only_mode_warms_a_single_family():
    pw, calls = _pw(FamilyPrewarmMode.CONCISE_ONLY)
    await pw.warm_planned()
    assert [c["family"] for c in calls] == [ContractFamily.CONCISE]


async def _co_test_off_mode_warms_nothing():
    pw, calls = _pw(FamilyPrewarmMode.OFF)
    recs = await pw.warm_planned()
    assert calls == []
    assert recs == []


# ── exact production config ───────────────────────────────────────────────────
async def _co_test_prewarm_uses_exact_model_and_num_ctx():
    pw, calls = _pw()
    await pw.warm_family(ContractFamily.CONCISE)
    assert calls[0]["model"] == "qwen3:8b"
    assert calls[0]["num_ctx"] == 2048


# ── once-per-identity guard ───────────────────────────────────────────────────
async def _co_test_second_warm_of_same_identity_is_skipped():
    pw, calls = _pw()
    await pw.warm_family(ContractFamily.CONCISE)
    await pw.warm_family(ContractFamily.CONCISE)
    assert len(calls) == 1  # guarded — a restart loop cannot stack warm loads


async def _co_test_warmed_identity_is_exposed_for_the_observer():
    pw, calls = _pw()
    rec = await pw.warm_family(ContractFamily.CONCISE)
    assert pw.warmed_identity() == rec.compatibility_identity
    assert pw.warmed_identity()  # non-empty


# ── invalidation re-arms ──────────────────────────────────────────────────────
async def _co_test_invalidation_marks_stale_and_allows_rewarm():
    pw, calls = _pw()
    await pw.warm_family(ContractFamily.CONCISE)
    pw.note_invalidation("NUM_CTX_CHANGED")
    assert pw.warmed_identity() is None
    assert pw.states[ContractFamily.CONCISE] is FamilyState.INVALIDATED
    await pw.warm_family(ContractFamily.CONCISE)
    assert len(calls) == 2  # rewarm allowed after invalidation


async def _co_test_config_change_invalidates_warm_state():
    pw, calls = _pw()
    await pw.warm_family(ContractFamily.CONCISE)
    pw.note_config(num_ctx=1024)
    assert pw.warmed_identity() is None
    await pw.warm_family(ContractFamily.CONCISE)
    assert calls[-1]["num_ctx"] == 1024


# ── stopping preempts ─────────────────────────────────────────────────────────
async def _co_test_no_prewarm_after_stopping():
    stopping = {"v": True}
    pw, calls = _pw(is_stopping=lambda: stopping["v"])
    rec = await pw.warm_family(ContractFamily.CONCISE)
    assert calls == []
    assert rec.state is FamilyState.CANCELLED


# ── cancellation ──────────────────────────────────────────────────────────────
async def _co_test_cancel_tears_down_background_task():
    async def slow_runner(family, **kw):
        await asyncio.sleep(10)
        return FamilyRecord(family=family.value, state=FamilyState.READY, success=True)
    pw = FamilyPrewarm(model="qwen3:8b", mode=FamilyPrewarmMode.BACKGROUND_FAMILIES,
                       num_ctx=2048, runner=slow_runner)
    pw.start_background()
    await asyncio.sleep(0.01)
    await pw.cancel()  # bounded, must return
    assert pw._task.done()


# ── snapshot is content-free ──────────────────────────────────────────────────
async def _co_test_snapshot_is_bounded_and_content_free():
    pw, calls = _pw()
    await pw.warm_planned()
    snap = pw.snapshot()
    assert snap["successes"] == 2
    assert set(snap["family_states"]) == {"CONCISE", "EXPLANATORY"}
    blob = repr(snap)
    assert "local AI assistant" not in blob and "hola" not in blob


def test_background_families_warms_concise_then_explanatory_not_ten():
    _run(_co_test_background_families_warms_concise_then_explanatory_not_ten())

def test_concise_only_mode_warms_a_single_family():
    _run(_co_test_concise_only_mode_warms_a_single_family())

def test_off_mode_warms_nothing():
    _run(_co_test_off_mode_warms_nothing())

def test_prewarm_uses_exact_model_and_num_ctx():
    _run(_co_test_prewarm_uses_exact_model_and_num_ctx())

def test_second_warm_of_same_identity_is_skipped():
    _run(_co_test_second_warm_of_same_identity_is_skipped())

def test_warmed_identity_is_exposed_for_the_observer():
    _run(_co_test_warmed_identity_is_exposed_for_the_observer())

def test_invalidation_marks_stale_and_allows_rewarm():
    _run(_co_test_invalidation_marks_stale_and_allows_rewarm())

def test_config_change_invalidates_warm_state():
    _run(_co_test_config_change_invalidates_warm_state())

def test_no_prewarm_after_stopping():
    _run(_co_test_no_prewarm_after_stopping())

def test_cancel_tears_down_background_task():
    _run(_co_test_cancel_tears_down_background_task())

def test_snapshot_is_bounded_and_content_free():
    _run(_co_test_snapshot_is_bounded_and_content_free())
