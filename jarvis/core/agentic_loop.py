"""
core/agentic_loop.py — Autonomous ReAct SOC loop (v24.0).

ReAct pattern: Observe → Reason (LLM) → Act (tool dispatch) → Observe → repeat.
Hard limits come from settings (agentic_max_cycles / agentic_loop_timeout).

V69 M68A §C/§D — what an external static audit found here, and what changed
-------------------------------------------------------------------------
1. **Shared cancellation identity.** Every run registered under the literal name
   ``"agentic_loop"`` and watched one module-global event. Two concurrent
   incidents were therefore one identity: run B's ``unregister_operation`` erased
   run A from the registry, and — worse — run B *cleared the shared event at
   start-up*, silently revoking the operator's cancellation of run A, which then
   carried on invoking ``run_shell_command``. Each run now owns an
   :class:`~core.cancel_bus.ExecutionHandle`: its own token, its own event, and no
   API by which one run can touch another's.

2. **A deadline checked only between cycles.** ``agentic_loop_timeout`` was
   compared once per iteration, so a single hung tool call or a NATO approval
   waiting on a keyboard that nobody was sitting at outlived it without bound.
   The handle now carries an ABSOLUTE deadline and every await in the run is
   bounded by what remains of it.

3. **A malformed decision that meant RESOLVED.** See
   :mod:`core.agentic_decision`. Nothing dispatches, and nothing terminates as
   *resolved*, without :func:`~core.agentic_decision.validate_decision` first.

4. **Terminal state recorded zero or twice.** A cancelled run broadcast no
   terminal event at all; a timed-out one broadcast two. There is now exactly one
   ``agentic_terminal`` per run, on every exit path, and the flag that guarantees
   it is set *before* the broadcast so a failing broadcast cannot yield a second.

5. **Uncertainty erased at the effect boundary.** A tool cancelled or timed out
   mid-flight was logged as ``{"error": ...}``, which reads as *it did not
   happen*. It now records UNKNOWN/BLOCKED_INDETERMINATE, matching what the
   durable journal independently records (§41).
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from core.config import settings
from core.events import make_event
# v35.0 — operator interrupt. V69 M68A §D — per-execution identity.
from core.cancel_bus import register_execution, unregister_execution
from core.agentic_decision import (
    SOC_ADVERTISED_TOOLS, DecisionStatus, validate_decision,
)

_HIGH_RISK_TOOLS: frozenset[str] = frozenset({
    "run_shell_command",
    "network_scan", "forensic_capture",
})

#: The tools :meth:`core.llm.JarvisLLM.decide_next_action` advertises in its own
#: system prompt. A decision naming anything else is a malformed decision, and
#: saying so here — before dispatch — is the §C contract check. Imported, never
#: restated: a second copy is a drift waiting to happen.
ADVERTISED_TOOLS: frozenset[str] = frozenset(SOC_ADVERTISED_TOOLS)

#: How long ONE reasoning call may take, before the run's remaining deadline is
#: also applied. Both bounds hold; the tighter one wins.
_REASONING_TIMEOUT_S: float = 30.0

#: Consecutive unusable decisions (INVALID_DECISION / MODEL_ERROR / UNKNOWN)
#: tolerated before the run ends truthfully. A malformed reply must not end an
#: incident as resolved (§C) — and must not spin forever either.
_MAX_UNUSABLE_DECISIONS: int = 3


class TerminalOutcome:
    """The closed set of ways a run can end. Exactly one is recorded per run."""

    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CYCLE_BUDGET_EXHAUSTED = "CYCLE_BUDGET_EXHAUSTED"
    REASONING_UNUSABLE = "REASONING_UNUSABLE"
    APPROVAL_UNAVAILABLE = "APPROVAL_UNAVAILABLE"
    FAILED = "FAILED"


class _Terminal:
    """Records the run's terminal state EXACTLY once.

    ``recorded`` is set before the broadcast is attempted, so a broadcast that
    raises cannot produce a second record — the audited failure was the mirror of
    this: several paths each broadcasting their own ending, and one path (an
    ``asyncio.CancelledError``) broadcasting none.
    """

    __slots__ = ("_broadcast", "recorded")

    def __init__(self, broadcast_fn) -> None:
        self._broadcast = broadcast_fn
        self.recorded: dict | None = None

    async def record(self, outcome: str, *, cycles_run: int, action_log: list,
                     detail: str = "", uncertain_effects: int = 0) -> bool:
        """Record the terminal state. Returns False if one was already recorded."""
        if self.recorded is not None:
            return False
        payload = make_event(
            "agentic_terminal",
            outcome=outcome,
            detail=detail[:320],
            cycles_run=cycles_run,
            uncertain_effects=uncertain_effects,
            actions=len(action_log),
        )
        self.recorded = payload
        try:
            await self._broadcast(payload)
        except Exception as e:  # noqa: BLE001 — the record stands regardless
            logger.warning(f"AGENTIC: terminal broadcast failed: {e}")
        return True


async def _bounded(awaitable, budget: float, *, what: str):
    """Await *awaitable* under *budget* seconds. Raises ``asyncio.TimeoutError``.

    A non-positive budget means the absolute deadline has already passed, so the
    call is refused WITHOUT being started — starting it would be the thing the
    deadline exists to prevent.

    V69 M68A gen 64 — the refusal CLOSES the coroutine it was handed. Python
    evaluates the argument before this function runs, so a refused call still
    produced a live coroutine object; dropping it emitted
    ``RuntimeWarning: coroutine '...' was never awaited`` on every expired
    deadline. Measured, not theorised. Nothing executed either way — the security
    property was never in question — but "never started" should leave no debris,
    and a warning the runtime emits on a correct path is noise that hides the
    warnings that matter.
    """
    if budget <= 0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        logger.warning(f"AGENTIC: {what} refused — run deadline already passed")
        raise asyncio.TimeoutError(what)
    return await asyncio.wait_for(awaitable, timeout=budget)


async def run_agentic_incident(
    trigger_event: dict,
    tool_executor,
    broadcast_fn,
    llm_client,
    cognitive_engine=None,
) -> None:
    """ReAct loop triggered by high-confidence security events (canary, DPI, ETW).

    v58.0: if a CognitiveEngine is supplied, a bounded deterministic plan is
    drafted and broadcast before the ReAct loop for traceability/triage. The
    plan is advisory only — the existing LLM-driven ReAct loop remains the
    execution path, so behavior is unchanged when no engine is provided.
    """
    context    = [trigger_event]
    action_log = []

    # v58.0 COGNITIVE CORE — optional pre-flight plan (fail-open, non-blocking).
    if cognitive_engine is not None:
        try:
            objective = (
                f"Triage and contain security event: {trigger_event.get('type', 'unknown')}"
            )
            _plan = cognitive_engine.create_plan(objective, {"trigger": trigger_event})
            await broadcast_fn(make_event(
                "agentic_plan",
                task_id=_plan.task_id,
                risk_level=_plan.risk_level.value,
                steps=[s.action for s in _plan.plan_steps],
                confidence=_plan.confidence,
            ))
        except Exception as e:
            logger.debug(f"AGENTIC: cognitive plan skipped: {e}")

    # V69 M68A §D — THIS run's cancellation identity and absolute deadline. No
    # `.clear()`: there is nothing shared left to clear, which is what made the
    # old clear able to revoke another run's cancellation.
    handle = register_execution("agentic_loop",
                               timeout=float(settings.agentic_loop_timeout))
    terminal = _Terminal(broadcast_fn)

    #: Cycles that actually ran, not `cycle + 1`. A run aborted before doing
    #: anything used to report one completed cycle.
    cycles_run = 0
    uncertain_effects = 0
    unusable = 0

    try:
        await broadcast_fn(make_event(
            "agentic_loop_start",
            trigger=trigger_event.get("type"),
            max_cycles=settings.agentic_max_cycles,
            execution=handle.name,
            deadline_s=round(float(settings.agentic_loop_timeout), 1),
        ))

        for _cycle in range(settings.agentic_max_cycles):
            # v35.0 — operator abort check before every cycle. V69 M68A §D: this
            # reads only THIS execution's state.
            if handle.cancelled():
                logger.warning(
                    f"AGENTIC: {handle.name} aborted at cycle {cycles_run} "
                    "by operator interrupt")
                await broadcast_fn({
                    "type":       "agentic_aborted",
                    "cycle":      cycles_run,
                    "reason":     "operator_interrupt",
                    "action_log": action_log,
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                })
                await terminal.record(TerminalOutcome.CANCELLED,
                                      cycles_run=cycles_run,
                                      action_log=action_log,
                                      detail="operator_interrupt",
                                      uncertain_effects=uncertain_effects)
                break

            if handle.expired():
                await broadcast_fn(make_event(
                    "agentic_loop_timeout", elapsed=round(handle.elapsed(), 1)
                ))
                await terminal.record(TerminalOutcome.DEADLINE_EXCEEDED,
                                      cycles_run=cycles_run,
                                      action_log=action_log,
                                      detail="deadline_exceeded_between_cycles",
                                      uncertain_effects=uncertain_effects)
                break

            # ── Reason ────────────────────────────────────────────────────────
            # Bounded by BOTH the per-call reasoning timeout and what is left of
            # the run's absolute deadline.
            try:
                raw_decision = await _bounded(
                    llm_client.decide_next_action(context),
                    min(_REASONING_TIMEOUT_S, handle.remaining()),
                    what="reasoning")
            except asyncio.TimeoutError:
                await broadcast_fn(make_event(
                    "error", error="LLM reasoning timeout in agentic loop"
                ))
                if handle.expired():
                    await terminal.record(TerminalOutcome.DEADLINE_EXCEEDED,
                                          cycles_run=cycles_run,
                                          action_log=action_log,
                                          detail="deadline_exceeded_while_reasoning",
                                          uncertain_effects=uncertain_effects)
                    break
                raw_decision = {"status": DecisionStatus.MODEL_ERROR.value,
                                "detail": "reasoning timeout"}
            except Exception as e:  # noqa: BLE001
                # V69 M68A §C — a transport failure is MODEL_ERROR. It used to
                # propagate out of the whole run, past the summary, leaving no
                # terminal record at all.
                logger.warning(f"AGENTIC: reasoning call failed: {e}")
                raw_decision = {"status": DecisionStatus.MODEL_ERROR.value,
                                "detail": f"{type(e).__name__}: {e}"}

            # ── V69 M68A §C: validate BEFORE dispatch ─────────────────────────
            decision = validate_decision(raw_decision,
                                         allowed_tools=ADVERTISED_TOOLS,
                                         honour_status=True)

            if decision.is_resolved:
                await broadcast_fn(make_event(
                    "agentic_resolved",
                    reasoning=decision.reasoning,
                    cycles=cycles_run + 1,
                ))
                await terminal.record(TerminalOutcome.RESOLVED,
                                      cycles_run=cycles_run + 1,
                                      action_log=action_log,
                                      uncertain_effects=uncertain_effects)
                break

            if not decision.may_dispatch:
                # INVALID_DECISION / MODEL_ERROR / UNKNOWN. Emphatically NOT
                # resolved — the incident is still open and the event log says so.
                unusable += 1
                logger.warning(
                    f"AGENTIC: unusable decision ({decision.status.value}) "
                    f"{unusable}/{_MAX_UNUSABLE_DECISIONS}: {decision.detail}")
                await broadcast_fn(make_event(
                    "agentic_decision_rejected",
                    status=decision.status.value,
                    detail=decision.detail,
                    attempt=unusable,
                ))
                context.append({"observation": (
                    "The previous decision was not usable "
                    f"({decision.status.value}: {decision.detail}). Reply with "
                    'ONLY the JSON object {"tool": ..., "input": {}, '
                    '"reasoning": ...}.')})
                if unusable >= _MAX_UNUSABLE_DECISIONS:
                    await broadcast_fn({
                        "type":       "agentic_aborted",
                        "cycle":      cycles_run,
                        "reason":     "reasoning_unusable",
                        "action_log": action_log,
                        "timestamp":  datetime.now(timezone.utc).isoformat(),
                    })
                    await terminal.record(TerminalOutcome.REASONING_UNUSABLE,
                                          cycles_run=cycles_run,
                                          action_log=action_log,
                                          detail=decision.detail,
                                          uncertain_effects=uncertain_effects)
                    break
                continue

            unusable = 0
            tool_name  = decision.tool
            tool_input = decision.tool_input
            reasoning  = decision.reasoning
            cycles_run += 1

            await broadcast_fn(make_event(
                "agentic_cycle",
                cycle=cycles_run,
                tool=tool_name,
                reasoning=reasoning[:200],
            ))

            # ── Approve ───────────────────────────────────────────────────────
            if tool_name in _HIGH_RISK_TOOLS:
                try:
                    auth_ok, _ = await _bounded(
                        tool_executor._challenge(
                            tool_name=tool_name,
                            preview=str(tool_input)[:120],
                        ),
                        handle.remaining(),
                        what="approval")
                except asyncio.TimeoutError:
                    # The deadline caught an approval nobody answered. Nothing ran
                    # — the challenge gates the call — so this is fail-closed and
                    # certain, not uncertain.
                    action_log.append({
                        "cycle":  cycles_run,
                        "tool":   tool_name,
                        "result": "DENIED — approval deadline exceeded",
                    })
                    await terminal.record(TerminalOutcome.APPROVAL_UNAVAILABLE,
                                          cycles_run=cycles_run,
                                          action_log=action_log,
                                          detail="approval_deadline_exceeded",
                                          uncertain_effects=uncertain_effects)
                    break
                if not auth_ok:
                    action_log.append({
                        "cycle":  cycles_run,
                        "tool":   tool_name,
                        "result": "DENIED — NATO challenge failed",
                    })
                    context.append({"observation": "Action denied by operator"})
                    continue

            # ── Act ───────────────────────────────────────────────────────────
            uncertain = False
            try:
                result = await _bounded(
                    tool_executor.aexecute(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        reasoning=f"[AGENTIC cycle={cycles_run}] {reasoning}",
                    ),
                    handle.remaining(),
                    what=f"tool:{tool_name}")
            except asyncio.TimeoutError:
                # V69 M68A §D — the effect MAY have started. `{"error": ...}` here
                # reads as "it did not happen", which is a claim about the external
                # world that nothing observed. The durable journal records the same
                # indeterminacy independently (§41, `_settle_cancelled`).
                uncertain = True
                uncertain_effects += 1
                result = {
                    "error": (f"`{tool_name}` exceeded the run deadline; whether "
                              "it took effect is UNKNOWN"),
                    "error_class": "deadline_exceeded",
                    "disposition": "BLOCKED_INDETERMINATE",
                    "external_outcome": "UNKNOWN",
                    "retry_authority": "BLOCKED_INDETERMINATE",
                }
                logger.warning(f"AGENTIC: {tool_name} timed out — outcome UNKNOWN")
            except asyncio.CancelledError:
                # Same physics, and the run is going away. Record the uncertainty
                # BEFORE re-raising: the old code let this path exit with no
                # terminal event and no note that an effect was in flight.
                uncertain_effects += 1
                action_log.append({
                    "cycle":  cycles_run,
                    "tool":   tool_name,
                    "input":  tool_input,
                    "result": {
                        "error": (f"`{tool_name}` was cancelled in flight; whether "
                                  "it took effect is UNKNOWN"),
                        "error_class": "cancelled_in_flight",
                        "disposition": "BLOCKED_INDETERMINATE",
                        "external_outcome": "UNKNOWN",
                        "retry_authority": "BLOCKED_INDETERMINATE",
                    },
                })
                raise
            except Exception as e:
                result = {"error": str(e)}

            action_log.append({
                "cycle":  cycles_run,
                "tool":   tool_name,
                "input":  tool_input,
                "result": result,
            })
            context.append({
                "cycle":       cycles_run,
                "tool":        tool_name,
                "observation": result,
            })

            if uncertain:
                # An effect of unknown outcome is not a foundation for the next
                # action. Stop, truthfully.
                await terminal.record(TerminalOutcome.DEADLINE_EXCEEDED,
                                      cycles_run=cycles_run,
                                      action_log=action_log,
                                      detail=f"uncertain_effect:{tool_name}",
                                      uncertain_effects=uncertain_effects)
                break

            # v35.0 — abort check after long tool execution
            if handle.cancelled():
                logger.warning(f"AGENTIC: {handle.name} aborted mid-cycle {cycles_run}")
                await terminal.record(TerminalOutcome.CANCELLED,
                                      cycles_run=cycles_run,
                                      action_log=action_log,
                                      detail="operator_interrupt_after_tool",
                                      uncertain_effects=uncertain_effects)
                break
        else:
            # The for-loop ran to completion: the cycle budget is spent.
            await terminal.record(TerminalOutcome.CYCLE_BUDGET_EXHAUSTED,
                                  cycles_run=cycles_run,
                                  action_log=action_log,
                                  detail="agentic_max_cycles reached",
                                  uncertain_effects=uncertain_effects)

        await broadcast_fn(make_event(
            "agentic_summary",
            trigger=trigger_event.get("type"),
            cycles_run=cycles_run,
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
        logger.warning(f"AGENTIC: {handle.name} CancelledError — clean exit")
        await terminal.record(TerminalOutcome.CANCELLED,
                              cycles_run=cycles_run,
                              action_log=action_log,
                              detail="task_cancelled",
                              uncertain_effects=uncertain_effects)
    except Exception as e:  # noqa: BLE001
        logger.error(f"AGENTIC: {handle.name} failed: {type(e).__name__}: {e}")
        await terminal.record(TerminalOutcome.FAILED,
                              cycles_run=cycles_run,
                              action_log=action_log,
                              detail=f"{type(e).__name__}: {e}",
                              uncertain_effects=uncertain_effects)
        raise
    finally:
        # V69 M68A §D — by TOKEN. `unregister_operation("agentic_loop")` removed
        # every run of the kind, so whichever incident finished first deregistered
        # the ones still running.
        unregister_execution(handle)
