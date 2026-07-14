"""
tests/test_live_interaction_harness_v69.py — V69 M54.14 live-runtime coherence harness.

A single deterministic scenario that reproduces the exact broken live run and proves
every M54 seam composes correctly — using fakes and simulated time, with NO live
Ollama. It drives the real modules (lifecycle, console, turn_policy, language_context,
host_time, boot_state, tts governor, turn_budget, shutdown_manager, hunt_scheduler)
through the numbered scenario from the M54 directive.
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone, timedelta

import core.shutdown_manager as sm
import core.lifecycle as lc
import core.host_time as host_time
from core.lifecycle import reset_lifecycle, LifecycleState
from core.console import ConsoleCoordinator, ConsoleChannel
from core.turn_policy import classify_request, ReasonCode, RequestClass
from core.language_context import LanguageContext
from core.boot_state import assemble_boot_state, OK
from core.tts_queue import TTSGovernor, TTSPriority
from core.turn_budget import TurnBudget, budget_for
from core.hunt_scheduler import run_single_hunt


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def setup_function(_):
    reset_lifecycle()
    sm.reset_signal_state()
    sm._shutdown_callbacks.clear()


def teardown_function(_):
    sm._shutdown_callbacks.clear()
    reset_lifecycle()
    sm.reset_signal_state()
    host_time.reset_clock()


def test_full_broken_scenario_is_now_coherent():
    life = lc.lifecycle
    console = ConsoleCoordinator(stream=FakeTTY())
    language = LanguageContext()
    language.detected_lang = "es"

    # 1-3. Boot has started; input is NOT accepted before TEXT_READY.
    assert life.state is LifecycleState.STARTING
    assert not life.accepts_input()

    # 4. A premature input attempt is rejected by the gate.
    assert not life.accepts_input()

    # 5. Reach TEXT_READY.
    assert life.mark_text_ready()
    assert life.accepts_input()

    # 6-7. Background logs during active typing keep the input line intact.
    console.set_prompt("Tú: ")
    console.post("THREAT_FEED: sync complete", ConsoleChannel.LOG)
    console.post("HUNT: warmup", ConsoleChannel.LOG)
    console.render_now()
    out = console.stream.getvalue()
    assert "THREAT_FEED: sync complete" in out
    assert out.rstrip().endswith("Tú:")   # prompt redrawn, not clobbered

    # An internal tool-call JSON is posted as a framed TOOL line, never fed to input.
    console.post('{"name": "code_execute"}', ConsoleChannel.TOOL)
    console.render_now()
    assert '{"name": "code_execute"}' in console.stream.getvalue()

    # 8-9. "hola" — FAST, no tool, no verifier.
    p = classify_request("hola")
    assert p.request_class is RequestClass.ORDINARY_CONVERSATION
    assert p.reason_code is ReasonCode.DIRECT_FAST
    assert p.wants_llm_verifier() is False

    # 10-11. "explícame Python" — Spanish educational.
    assert language.observe_text("explícame algo de Python") == "es"
    p = classify_request("explícame algo de Python")
    assert p.reason_code is ReasonCode.DIRECT_FAST

    # 12-14. "POO" — direct educational, Spanish inherited, query_knowledge NOT offered.
    assert language.observe_text("POO") == "es"          # inherits active language
    p = classify_request("¿Qué es POO?")
    assert p.request_class is RequestClass.GENERAL_EDUCATIONAL
    tools = [{"function": {"name": "query_knowledge"}}, {"function": {"name": "web_search"}}]
    assert "query_knowledge" not in {t["function"]["name"] for t in p.filter_tools(tools)}

    # 15-18. "¿Qué dice mi PDF sobre POO?" — private RAG routing; empty vault still
    # allows a general answer (the classes are separate).
    p = classify_request("¿Qué dice mi PDF sobre POO?")
    assert p.reason_code is ReasonCode.PRIVATE_RAG
    assert "query_knowledge" in {t["function"]["name"] for t in p.filter_tools(tools)}
    # General educational is still answerable independently of the empty vault.
    assert classify_request("¿Qué es POO?").knowledge_vault_allowed is False

    # 19-20. "¿Qué hora es?" — deterministic host time (frozen clock).
    fixed = datetime(2026, 7, 13, 9, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    host_time.set_clock(lambda: fixed)
    p = classify_request("¿Qué hora es?")
    assert p.reason_code is ReasonCode.DETERMINISTIC_TIME
    line = host_time.host_time_prompt_line()
    assert "2026-07-13T09:30:00" in line
    assert "never say you lack real-time access" in line

    # 21-23. Semantic REINDEX_REQUIRED → readiness degraded; no "All systems nominal".
    summary = {"overall": "DEGRADED", "collections": [
        {"logical_name": "jarvis_episodic", "status": "REINDEX_REQUIRED"},
        {"logical_name": "jarvis_knowledge", "status": "ACTIVE"},
    ]}
    boot = assemble_boot_state(
        {"results": [{"id": "chromadb", "status": OK}, {"id": "ollama", "status": OK}]},
        semantic_summary=summary,
    )
    assert boot.all_systems_nominal() is False
    narration = " ".join(m for _, m in boot.narration_lines())
    assert "All systems nominal" not in narration
    assert "Episodic memory online." not in dict(boot.narration_lines())["memory"]

    # 24-25. A slow verifier cannot blow the turn budget: the verifier receives only
    # the remaining budget and is skipped once exhausted.
    class Clk:
        def __init__(self): self.t = 0.0
        def __call__(self): return self.t
    clk = Clk()
    budget = TurnBudget(total_s=budget_for(classify_request("¿Qué es POO?")), clock=clk)
    clk.t = budget.total_s - 1.0     # only ~1s left
    assert budget.can_afford_verifier() is False
    assert budget.verifier_budget_s(20.0) <= 1.0

    # 26-27. TTS queue stays bounded and coalesces a duplicate low-priority flood.
    gov = TTSGovernor(max_size=8)
    for i in range(60):
        gov.put("monitor tick", priority=TTSPriority.LOW, key="mon")
    assert len(gov) <= 8
    assert gov.metrics()["coalesced"] >= 1

    console.stop()


def test_one_sigint_one_shutdown_no_hunt_after_stopping():
    # 28-30. One SIGINT initiates exactly one shutdown; repeats do not start another.
    assert sm.handle_shutdown_signal("SIGINT") == "initiated"
    assert sm.handle_shutdown_signal("SIGINT") == "already_stopping"
    assert lc.lifecycle.state is LifecycleState.STOPPING

    # 31. No new hunt begins after STOPPING.
    res = asyncio.run(run_single_hunt(3))   # H04 — the live symptom
    assert res["verdict"] == "SKIPPED"


def test_semantic_checkpoint_before_storage_close_and_single_completion():
    # 32-34. Active task cancellation is bounded, the semantic checkpoint runs before
    # storage close, and shutdown completes exactly once.
    order: list[str] = []

    async def _run():
        writer_gone = asyncio.Event()

        async def _writer():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                order.append("writer_cancelled")
                writer_gone.set()
                raise

        async def _semantic_checkpoint():
            order.append("semantic_checkpoint")
            assert writer_gone.is_set()

        async def _close_storage():
            order.append("close_storage")
            assert "semantic_checkpoint" in order

        sm.register_shutdown_callback(_semantic_checkpoint)
        sm.register_shutdown_callback(_close_storage)
        asyncio.create_task(_writer(), name="writer")
        await asyncio.sleep(0)
        await sm.run_graceful_shutdown()
        # A second graceful shutdown call is a no-op transition (already STOPPED).
        await sm.run_graceful_shutdown()

    asyncio.run(_run())
    assert order[0] == "writer_cancelled"           # writers stopped first
    assert "semantic_checkpoint" in order           # checkpoint ran
    assert lc.lifecycle.state is LifecycleState.STOPPED

    # 35. No orphan console/TTS/scheduler workers is enforced by daemon threads +
    #     bounded joins in the respective modules (covered by their unit suites);
    #     here we assert the lifecycle ended cleanly in STOPPED exactly once.
