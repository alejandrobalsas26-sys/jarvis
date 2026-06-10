"""
core/agentic_loop.py — Autonomous ReAct SOC loop (v24.0).

ReAct pattern: Observe → Reason (LLM) → Act (tool dispatch) → Observe → repeat.
Hard limits come from settings (agentic_max_cycles / agentic_loop_timeout).
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from core.config import settings
from core.events import make_event
# v35.0 — operator interrupt
from core.cancel_bus import register_operation, unregister_operation
import core.cancel_bus as _cancel_bus

_HIGH_RISK_TOOLS: frozenset[str] = frozenset({
    "offensive_rpc", "run_shell_command",
    "network_scan", "forensic_capture",
})


async def run_agentic_incident(
    trigger_event: dict,
    tool_executor,
    broadcast_fn,
    llm_client,
) -> None:
    """ReAct loop triggered by high-confidence security events (canary, DPI, ETW)."""
    context    = [trigger_event]
    action_log = []
    start      = asyncio.get_event_loop().time()

    # v35.0 — register with cancel bus and clear prior abort flag
    register_operation("agentic_loop")
    if _cancel_bus.agentic_loop_cancel is not None:
        _cancel_bus.agentic_loop_cancel.clear()

    cycle = 0
    try:
        await broadcast_fn(make_event(
            "agentic_loop_start",
            trigger=trigger_event.get("type"),
            max_cycles=settings.agentic_max_cycles,
        ))

        for cycle in range(settings.agentic_max_cycles):
            # v35.0 — operator abort check before every cycle
            if (_cancel_bus.agentic_loop_cancel is not None
                    and _cancel_bus.agentic_loop_cancel.is_set()):
                logger.warning(
                    f"AGENTIC: loop aborted at cycle {cycle} by operator interrupt"
                )
                await broadcast_fn({
                    "type":       "agentic_aborted",
                    "cycle":      cycle,
                    "reason":     "operator_interrupt",
                    "action_log": action_log,
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                })
                break

            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > settings.agentic_loop_timeout:
                await broadcast_fn(make_event(
                    "agentic_loop_timeout", elapsed=round(elapsed, 1)
                ))
                break

            try:
                decision = await asyncio.wait_for(
                    llm_client.decide_next_action(context),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                await broadcast_fn(make_event(
                    "error", error="LLM reasoning timeout in agentic loop"
                ))
                break

            tool_name  = decision.get("tool")
            tool_input = decision.get("input", {})
            reasoning  = decision.get("reasoning", "")

            if tool_name == "RESOLVED":
                await broadcast_fn(make_event(
                    "agentic_resolved",
                    reasoning=reasoning,
                    cycles=cycle + 1,
                ))
                break

            await broadcast_fn(make_event(
                "agentic_cycle",
                cycle=cycle + 1,
                tool=tool_name,
                reasoning=reasoning[:200],
            ))

            if tool_name in _HIGH_RISK_TOOLS:
                auth_ok, _ = await tool_executor._challenge(
                    tool_name=tool_name,
                    preview=str(tool_input)[:120],
                )
                if not auth_ok:
                    action_log.append({
                        "cycle":  cycle + 1,
                        "tool":   tool_name,
                        "result": "DENIED — NATO challenge failed",
                    })
                    context.append({"observation": "Action denied by operator"})
                    continue

            try:
                result = await tool_executor.aexecute(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    reasoning=f"[AGENTIC cycle={cycle+1}] {reasoning}",
                )
            except Exception as e:
                result = {"error": str(e)}

            action_log.append({
                "cycle":  cycle + 1,
                "tool":   tool_name,
                "input":  tool_input,
                "result": result,
            })
            context.append({
                "cycle":       cycle + 1,
                "tool":        tool_name,
                "observation": result,
            })

            # v35.0 — abort check after long tool execution
            if (_cancel_bus.agentic_loop_cancel is not None
                    and _cancel_bus.agentic_loop_cancel.is_set()):
                logger.warning(f"AGENTIC: aborted mid-cycle {cycle + 1}")
                break

        await broadcast_fn(make_event(
            "agentic_summary",
            trigger=trigger_event.get("type"),
            cycles_run=min(cycle + 1, settings.agentic_max_cycles),
            action_log=action_log,
        ))

        # Store incident in episodic memory for future RAG context injection
        try:
            from core.episodic_memory import store_episode
            asyncio.create_task(store_episode(
                str(action_log),
                "agentic_incident",
                severity="HIGH",
                source="internal",
            ))
        except Exception:
            pass

    except asyncio.CancelledError:
        logger.warning("AGENTIC: CancelledError — clean exit")
    finally:
        unregister_operation("agentic_loop")
