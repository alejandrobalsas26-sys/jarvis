"""
core/agentic_loop.py — Autonomous ReAct SOC loop (v22.0).

ReAct pattern: Observe → Reason (LLM) → Act (tool dispatch) → Observe → repeat.

Constraints:
- MAX_CYCLES = 8  hard iteration cap per incident
- LOOP_TIMEOUT = 120s  global timeout per incident
- High-risk tools require NATO OTP even in autonomous mode
- Tool dispatch via ToolExecutor.execute() — never bypass it
"""

import asyncio
from datetime import datetime, timezone

MAX_CYCLES   = 8
LOOP_TIMEOUT = 120   # seconds

# High-risk tools that require NATO OTP even in autonomous mode
_HIGH_RISK_TOOLS: frozenset[str] = frozenset({
    "offensive_rpc", "run_shell_command",
    "network_scan", "forensic_capture",
})


async def run_agentic_incident(
    trigger_event: dict,
    tool_executor,       # ToolExecutor — dispatch + challenge
    broadcast_fn,
    llm_client,          # core/llm.py LLM instance (must expose decide_next_action)
) -> None:
    """
    ReAct loop: Observe → Reason (LLM) → Act (tool dispatch) → repeat.
    Triggered by high-confidence security events (canary, DPI alert, ETW).
    """
    context    = [trigger_event]
    action_log = []
    start      = asyncio.get_event_loop().time()

    await broadcast_fn({
        "type":       "agentic_loop_start",
        "trigger":    trigger_event.get("type"),
        "max_cycles": MAX_CYCLES,
    })

    cycle = 0
    for cycle in range(MAX_CYCLES):
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > LOOP_TIMEOUT:
            await broadcast_fn({"type":    "agentic_loop_timeout",
                                "elapsed": round(elapsed, 1)})
            break

        # Reason — LLM decides next action based on accumulated context
        try:
            decision = await asyncio.wait_for(
                llm_client.decide_next_action(context),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await broadcast_fn({"type":  "error",
                                "error": "LLM reasoning timeout in agentic loop"})
            break

        tool_name  = decision.get("tool")
        tool_input = decision.get("input", {})
        reasoning  = decision.get("reasoning", "")

        # Terminal state — LLM decided incident is resolved
        if tool_name == "RESOLVED":
            await broadcast_fn({
                "type":      "agentic_resolved",
                "reasoning": reasoning,
                "cycles":    cycle + 1,
            })
            break

        await broadcast_fn({
            "type":      "agentic_cycle",
            "cycle":     cycle + 1,
            "tool":      tool_name,
            "reasoning": reasoning[:200],
        })

        # NATO OTP gate for high-risk tools
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

        # Act — dispatch via ToolExecutor (applies its own security layers)
        try:
            result = await tool_executor.execute(
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

        # Observe — feed result back into context for next cycle
        context.append({
            "cycle":       cycle + 1,
            "tool":        tool_name,
            "observation": result,
        })

    # Final summary broadcast
    await broadcast_fn({
        "type":       "agentic_summary",
        "trigger":    trigger_event.get("type"),
        "cycles_run": min(cycle + 1, MAX_CYCLES),
        "action_log": action_log,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })
