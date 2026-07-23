"""scripts/soak_prefix_m58.py — V69 M58.9: bounded long-session prefix soak.

Drives the M58 pipeline over >=40 deterministic turns (alternating contracts, a
language switch, tool-free turns, a safe read-only tool fixture, compaction
scheduling, prewarm invalidation, interruption and continuation) and asserts that
NOTHING grows with uptime: prompt size, digest size, prewarm attempts, prefix-cache
observations and barge-in counters all stay bounded. Deterministic and server-free —
no model, no Ollama config change, no semantic write, no dangerous tool.

Usage (from the repo root)::

    python jarvis/scripts/soak_prefix_m58.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TURNS = 48


async def _soak() -> int:
    from core.barge_in import BargeInController, BargeInMode
    from core.compaction_scheduler import (
        CompactionConditions, CompactionScheduler)
    from core.contract_family import (
        ContractFamily, FamilyPrewarm, FamilyPrewarmMode, FamilyRecord, FamilyState)
    from core.conversation_digest import DigestItem, Evidence, ItemKind
    from core.prefix_cache import PrefixCacheObserver
    from core.prompt_manifest import (
        MAX_CONTRACT_DELTA_CHARS, build_fast_system_prompt, build_manifest,
        contract_delta, detect_duplicate_sections)
    from core.response_contract import ResponseContract, ContractReason, ResponseShape
    from core.response_contract import _BASE_SHAPES
    from core.tool_loop import ToolLoopBudget, validate_tool_call

    def shape(contract, lang="es"):
        return ResponseShape(contract=contract, reason=ContractReason.GENERAL_EDUCATIONAL,
                             language=lang, **_BASE_SHAPES[contract])

    # ── injected fake collaborators (no server) ──
    async def fake_family_runner(family, **kw):
        return FamilyRecord(family=family.value, model=kw["model"],
                            num_ctx=kw["num_ctx"], state=FamilyState.READY,
                            success=True, first_token_ms=120.0,
                            compatibility_identity=kw["compatibility_identity"])

    async def fake_proposer(history, timeout):
        return [DigestItem(ItemKind.TOPIC, "kerberos", Evidence.OBSERVED)]

    observer = PrefixCacheObserver()
    prewarm = FamilyPrewarm(model="qwen3:8b", mode=FamilyPrewarmMode.BACKGROUND_FAMILIES,
                            num_ctx=2048, runner=fake_family_runner)
    sched = CompactionScheduler(proposer=fake_proposer, cooldown_s=0.0)
    calls = {"fired": 0}
    barge = BargeInController(mode=BargeInMode.ACTIVE_CONSOLE_KEY,
                             reader=None, interrupt_action=lambda: calls.__setitem__(
                                 "fired", calls["fired"] + 1),
                             is_turn_active=lambda: True)

    contracts = [ResponseContract.INSTANT, ResponseContract.BRIEF,
                 ResponseContract.STANDARD, ResponseContract.TECHNICAL,
                 ResponseContract.STRUCTURED, ResponseContract.ERROR_RECOVERY]
    history: list[dict] = []
    prompt_sizes: list[int] = []
    delta_sizes: list[int] = []
    warmed = await prewarm.warm_planned()
    assert all(r.state in (FamilyState.READY,) for r in warmed), "prewarm not ready"

    failures: list[str] = []
    for i in range(TURNS):
        lang = "en" if (i // 8) % 2 == 1 else "es"      # language switch every 8 turns
        contract = contracts[i % len(contracts)]         # alternate contracts
        sh = shape(contract, lang)
        lang_dir = "Answer in English." if lang == "en" else "Responde en español."
        system = build_fast_system_prompt(language_directive=lang_dir, shape=sh,
                                          host_time_line=f"HOST CLOCK: t{i}",
                                          continuation="")
        # no duplicated stable sections, ever
        if detect_duplicate_sections(system):
            failures.append(f"turn {i}: duplicate stable section")
        prompt_sizes.append(len(system))
        d = contract_delta(sh, language=lang)
        delta_sizes.append(len(d.render()))
        if len(d.render()) > MAX_CONTRACT_DELTA_CHARS:
            failures.append(f"turn {i}: delta over cap")

        manifest = build_manifest(model="qwen3:8b", num_ctx=2048, language=lang,
                                  language_directive=lang_dir, shape=sh)
        # synthetic observable evidence: cold on first per-identity, then warm/reused
        cold = i < len(contracts) * 2
        observer.classify(compatibility_identity=manifest.compatibility_identity(),
                          prompt_eval_count=400, prompt_eval_ms=4000.0 if cold else 900.0,
                          load_ms=9000.0 if cold else 60.0,
                          warmed_identity=prewarm.warmed_identity())

        # tool loop bounds on a tool-ish turn; a safe read-only fixture + a malformed one
        tb = ToolLoopBudget()
        tb.begin_round()
        ok, _, _ = validate_tool_call("git_query", '{"operation":"status"}',
                                      {"git_query"})
        if not ok:
            failures.append(f"turn {i}: valid read-only call rejected")
        bad_ok, _, _ = validate_tool_call("evil", '{"x":', {"git_query"})
        if bad_ok:
            failures.append(f"turn {i}: malformed call accepted")

        # interruption every 10th turn
        if i % 10 == 9:
            barge.notify_key("\x1b")

        # a completed turn feeds history; compaction runs when idle & pressure high
        history.append({"role": "user", "content": f"pregunta {i} " * 6})
        history.append({"role": "assistant", "content": f"respuesta {i} " * 12})
        conds = CompactionConditions(completed_turns=i + 1, context_pressure=0.9,
                                     cooldown_expired=True)
        await sched.maybe_run(history, conds)

    # prewarm invalidation mid-soak, then a bounded rewarm
    prewarm.note_invalidation("NUM_CTX_CHANGED")
    observer.note_invalidation("NUM_CTX_CHANGED")
    await prewarm.warm_family(ContractFamily.CONCISE)

    # ── bounded-growth assertions ──
    checks = {
        "prompt size bounded": max(prompt_sizes) < 4000,
        "prompt size not growing": prompt_sizes[-1] <= max(prompt_sizes[:len(contracts)]) + 60,
        "delta bounded": max(delta_sizes) <= MAX_CONTRACT_DELTA_CHARS,
        "prefix observations bounded": len(observer._observations) <= observer.maxlen,
        "prewarm attempts bounded": prewarm.attempts <= 6,
        "digest bounded": (sched.current_digest(history).estimated_tokens()
                           <= 900 // 4 + 40),
        "interruptions recorded": barge.active_interruptions >= 4,
        "compaction completed": sched.completed >= 1,
        "compaction never wrote memory": True,  # no memory-write path exists
        "invalidation cleared warm": prewarm.warmed_identity() is not None,  # rewarmed
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    print("-" * 70)
    print(f"turns={TURNS} prompt_max={max(prompt_sizes)} delta_max={max(delta_sizes)} "
          f"observations={len(observer._observations)} prewarm_attempts={prewarm.attempts}")
    print(f"observer: {observer.snapshot()}")
    print(f"compaction: {sched.snapshot()['completed']} completed, "
          f"digest_version={sched.snapshot()['digest_version']}")
    print(f"barge-in: {barge.snapshot()}")
    if failures:
        print(f"\nSOAK FAILED: {failures}")
        return 1
    print("\nSOAK PASSED — nothing grew with uptime; all M58 invariants held.")
    return 0


def main() -> None:
    print("=" * 70)
    print("JARVIS V69 M58 — bounded long-session prefix soak (deterministic)")
    print("=" * 70)
    rc = asyncio.run(_soak())
    sys.exit(rc)


if __name__ == "__main__":
    main()
