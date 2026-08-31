"""
core/mesh_live.py — V69 M64.1: the mesh ON the live operator turn.

M64 built the Cognitive Specialist Mesh and deliberately stopped short of wiring
it, recording ``MESH_WIRED_TO_LIVE_TURN = NO`` rather than leaving the gap
undiscovered. This module closes it.

WHAT THIS IS NOT
================
It is not a second orchestrator, router, verifier, blackboard, context compiler
or executive. Every one of those already exists and is imported here unchanged.
This module is the ADAPTER: it translates between what ``LLM.chat_stream``
already computes and what ``CognitiveOrchestrator`` already expects, and it owns
exactly one piece of policy of its own — how a mesh turn maps onto ONE
generation.

THE ONE-GENERATION RULE
=======================
The dangerous way to wire a mesh onto a streaming assistant is to let the mesh
produce an answer and let the old path also produce an answer. That is two
reasoning passes, two token bills, and two chances to contradict each other.

So the mesh does not generate. It DECIDES, then the existing single generation
runs under its decision:

    assemble_task_decision()      the one per-turn decision (unchanged)
        |
    plan(task_decision=...)       the one route, pure Python, no model, no I/O
        |
    context_for(primary)          the primary specialist's compiled context,
        |                         prepended to the system prompt of the ONE
        |                         generation that was going to happen anyway
    [ the existing stream ]       tokens reach the operator exactly as before
        |
    tool outcomes -> evidence     recorded from calls that ACTUALLY ran
        |
    finish() -> MeshAnswer        ARGUS runs here when policy requires it
        |
    verdict caveat as a suffix    via the repository's existing post-stream
                                  augmentation pattern

The streamed text IS the primary specialist's result. Nothing is generated
twice, nothing is buffered that was not already buffered, and the operator sees
first tokens at the same point in the turn as before.

WHAT THE MESH MAY NOT DO HERE
=============================
It executes no tools. ``ToolExecutor`` remains the one effect path and the live
tool loop remains its one live caller, so there is no second execution surface
and therefore no way for a mesh turn to duplicate an effect. Evidence is built
from outcomes that already happened; :func:`record_tool_outcome` takes the real
status and cannot be told a call succeeded when it did not.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from core.cognitive_mesh import REGISTRY, AutonomyLevel
from core.mesh_contracts import (
    EvidenceGraph,
    EvidenceRef,
    Provenance,
    ResultStatus,
    SpecialistResult,
    ToolCallStatus,
)
from core.mesh_orchestrator import MeshAnswer, evidence_from_tool, orchestrator
from core.mesh_router import MeshRoute, RouteMode

logger = logging.getLogger("jarvis.mesh_live")

#: How much of a specialist's compiled context may be prepended to the live
#: system prompt. The compiler already bounds itself by ``MeshBudget``; this is
#: the turn's own ceiling so a mesh directive can never dominate the context
#: window the answer needs.
MAX_DIRECTIVE_CHARS = 2400

#: Bounded memory hand-off. The live turn retrieves at most a couple of episodes
#: already; this caps what reaches a specialist regardless.
MAX_MEMORY_ITEMS = 3
MAX_MEMORY_CHARS = 480


@dataclass
class MeshTurn:
    """One live turn's mesh state. Created per turn, discarded with it."""

    route: MeshRoute
    graph: EvidenceGraph = field(default_factory=EvidenceGraph)
    started_at: float = field(default_factory=time.monotonic)
    task_id: str = ""
    #: Outcomes of tool calls that ACTUALLY ran this turn, in order.
    tool_outcomes: list = field(default_factory=list)
    #: Supporting specialist results, when a team ran. Empty on the fast path.
    support_results: list = field(default_factory=list)
    world_state_consulted: bool = False
    memory_items_used: int = 0
    directive_chars: int = 0
    fallback_used: bool = False
    fallback_reason: str = ""
    answer: MeshAnswer | None = None

    @property
    def mode(self) -> str:
        return self.route.mode.value

    @property
    def is_fast(self) -> bool:
        return self.route.mode is RouteMode.FAST_PATH

    @property
    def verifier_required(self) -> bool:
        return bool(self.route.verifier_required)

    def telemetry(self) -> dict:
        """Bounded, secret-free turn telemetry (§57)."""
        answer = self.answer
        return {
            "task_id": self.task_id or self.route.task_id,
            "route": self.route.mode.value,
            "primary_specialist": self.route.primary.value,
            "support_specialists": [s.value for s in self.route.supporting],
            "specialist_count": self.route.specialist_count,
            "verifier_required": self.route.verifier_required,
            "argus_verdict": (answer.verifier_status.value
                              if answer and answer.verifier_status else None),
            "tools_executed": sum(1 for o in self.tool_outcomes
                                  if o.status is ToolCallStatus.SUCCESS),
            "tools_denied": sum(1 for o in self.tool_outcomes
                                if o.status is ToolCallStatus.DENIED),
            "tools_failed": sum(1 for o in self.tool_outcomes
                                if o.status in (ToolCallStatus.FAILURE,
                                                ToolCallStatus.TIMEOUT)),
            "evidence_count": self.graph.evidence_count,
            "world_state_consulted": self.world_state_consulted,
            "memory_items": self.memory_items_used,
            "directive_chars": self.directive_chars,
            "autonomy_ceiling": int(self.route.autonomy_ceiling),
            "security_intent": self.route.security_intent.value,
            "offensive_intent": self.route.offensive_intent,
            "target_scope": list(self.route.target_scope),
            "fallback_used": self.fallback_used,
            "latency_ms": round((time.monotonic() - self.started_at) * 1000.0, 1),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Plan — runs on EVERY turn, before anything is generated
# ══════════════════════════════════════════════════════════════════════════════
def plan_turn(user_message: str, *, task_decision=None,
              tool_names: "list[str] | None" = None,
              task_id: str = "") -> "MeshTurn | None":
    """Route this turn. Pure, deterministic, no model call and no I/O.

    Returns ``None`` only if routing itself fails, which is the signal for the
    caller to proceed on the pre-mesh path unchanged — a routing bug must
    degrade the turn's *judgement*, never its ability to answer at all.
    """
    try:
        route = orchestrator.plan(user_message, tool_names=tool_names,
                                  task_id=task_id, task_decision=task_decision)
    except Exception as exc:  # noqa: BLE001 — routing never breaks a turn
        logger.warning(f"MESH_LIVE: routing failed ({exc}) — turn proceeds unrouted")
        return None
    turn = MeshTurn(route=route, task_id=task_id or route.task_id)
    logger.debug(
        "MESH_LIVE: route=%s primary=%s support=%s verifier=%s ceiling=L%d "
        "confidence=%.2f", route.mode.value, route.primary.value,
        [s.value for s in route.supporting], route.verifier_required,
        int(route.autonomy_ceiling), route.confidence)
    return turn


# ══════════════════════════════════════════════════════════════════════════════
#  Context — what the primary specialist is shown, and screened from
# ══════════════════════════════════════════════════════════════════════════════
def specialist_directive(turn: MeshTurn, *, memory_items: "tuple[str, ...]" = (),
                         blackboard_digest: str = "") -> str:
    """Compile the primary specialist's context into a system-prompt directive.

    This is where World State and Memory actually reach a specialist on a live
    turn. Both are consulted through the compiler, which honours the
    specialist's own ``preferred_context`` and ``memory_scope``: a slice the
    role does not declare is not assembled at all, and untrusted content is
    screened by the injection firewall before it is written.
    """
    if turn.is_fast:
        # §38 — the fast path stays fast. ATLAS answering "2+2" needs no
        # compiled role context, and paying for one would be the exact
        # regression the fast path exists to prevent.
        return ""
    try:
        world = _world_state()
        compiled = orchestrator.context_for(
            turn.route.primary, turn.route, turn.graph,
            memory_items=_bounded_memory(memory_items),
            blackboard_digest=blackboard_digest)
        turn.world_state_consulted = bool(
            getattr(compiled, "world_state_consulted", False))
        text = (compiled.text or "").strip()
        # Report what the compiler ASSEMBLED, not what was offered. Only four
        # roles declare ContextSlice.MEMORY, so a role that does not want memory
        # receives none — and telemetry claiming otherwise would misdescribe the
        # very context bound the mesh exists to enforce.
        turn.memory_items_used = (
            len(_bounded_memory(memory_items)) if "RELEVANT MEMORY" in text else 0)
        if len(text) > MAX_DIRECTIVE_CHARS:
            text = text[:MAX_DIRECTIVE_CHARS]
        turn.directive_chars = len(text)
        if world is None:
            logger.debug("MESH_LIVE: world state unavailable; specialist told so")
        return text
    except Exception as exc:  # noqa: BLE001 — a directive never breaks a turn
        logger.warning(f"MESH_LIVE: context compilation failed ({exc})")
        turn.fallback_used = True
        turn.fallback_reason = f"context compilation failed: {exc}"
        return ""


def _bounded_memory(items: "tuple[str, ...]") -> "tuple[str, ...]":
    """Cap what memory may contribute. Memory informs; it never authorizes."""
    return tuple(str(m)[:MAX_MEMORY_CHARS] for m in items[:MAX_MEMORY_ITEMS] if m)


def _world_state():
    """The live World State singleton, or None when it cannot be reached.

    Imported lazily and defensively: World State is EVIDENCE, so its absence
    must degrade a specialist's confidence, never the turn.
    """
    try:
        from core.world_state import world
        return world
    except Exception:  # noqa: BLE001
        return None


def attach_live_runtime(*, team_runtime=None, world_state=None, scopes=None) -> None:
    """Bind the live singletons to the ONE orchestrator at boot.

    Called from ``main`` once. Nothing here registers a scope: scope
    registration is an operator act and the specialist path never performs one.
    """
    if team_runtime is not None:
        orchestrator._team = team_runtime
    if world_state is not None:
        orchestrator._world = world_state
    if scopes is not None:
        orchestrator._scopes = scopes
    logger.info("MESH_LIVE: orchestrator bound (team=%s world=%s scopes=%d)",
                team_runtime is not None, world_state is not None,
                len(getattr(scopes, "scopes", []) or []))


# ══════════════════════════════════════════════════════════════════════════════
#  Evidence — recorded from what ACTUALLY happened
# ══════════════════════════════════════════════════════════════════════════════
def record_tool_outcome(turn: MeshTurn, tool: str, *, status: ToolCallStatus,
                        output: str, elapsed_s: float = 0.0) -> None:
    """Bind one real tool call into the evidence graph (§47).

    ``status`` is required and has no default, so there is no way to record a
    call as successful without saying so explicitly, and a DENIED or TIMEOUT
    call produces a reference that is RECORDED but not corroborating. That is
    what makes "the command showed..." impossible for a command that did not
    run: ARGUS reads the outcome, not the prose.
    """
    try:
        ref = evidence_from_tool(tool, status, output or "",
                                 specialist=turn.route.primary,
                                 elapsed_s=elapsed_s)
        turn.graph.add_evidence(ref)
        if ref.tool_outcome is not None:
            turn.tool_outcomes.append(ref.tool_outcome)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"MESH_LIVE: evidence binding skipped ({exc})")


def record_operator_request(turn: MeshTurn, user_message: str) -> None:
    """The operator's own words are evidence, at OPERATOR provenance."""
    try:
        turn.graph.add_evidence(EvidenceRef(
            content=user_message[:1200], provenance=Provenance.OPERATOR,
            source="operator", specialist=turn.route.primary))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"MESH_LIVE: operator evidence skipped ({exc})")


# ══════════════════════════════════════════════════════════════════════════════
#  Finish — ARGUS, then one answer
# ══════════════════════════════════════════════════════════════════════════════
def finish_turn(turn: MeshTurn, answer_text: str, *,
                status: ResultStatus = ResultStatus.COMPLETE,
                confidence: float = 0.7) -> "MeshAnswer | None":
    """Fold the streamed answer into a MeshAnswer and run ARGUS if required.

    The streamed text becomes the primary specialist's ``summary``: it IS what
    the specialist produced, so treating it as anything else would be recording
    a second, fictional result. ``finish`` then runs the verifier when
    ``route.verifier_required`` — the orchestrator does that itself, so ARGUS
    cannot be skipped by a caller that forgets.
    """
    try:
        primary = SpecialistResult(
            status=status,
            specialist_id=turn.route.primary,
            task_id=turn.task_id or turn.route.task_id,
            summary=answer_text or "",
            confidence=confidence,
            tool_outcomes=tuple(turn.tool_outcomes),
            uncertainty=turn.route.uncertainty,
        )
        results = (primary, *turn.support_results)
        answer = orchestrator.finish(
            turn.route, turn.graph, results, started_at=turn.started_at)
        turn.answer = answer
        return answer
    except Exception as exc:  # noqa: BLE001 — synthesis never breaks a turn
        logger.warning(f"MESH_LIVE: finish failed ({exc}) — streamed answer stands")
        turn.fallback_used = True
        turn.fallback_reason = f"finish failed: {exc}"
        return None


def verdict_suffix(answer: "MeshAnswer | None", streamed: str) -> str:
    """What must be APPENDED to an already-streamed answer, or "".

    The repository already has this shape: ``_maybe_verify_final_answer`` streams
    a draft and then appends a correction delta. ARGUS uses the same contract,
    so a verdict never requires buffering the answer and never contradicts text
    the operator has already read — it qualifies it.

    A passing verdict adds nothing. Silence is what "verified" looks like.
    """
    if answer is None or answer.verifier_status is None:
        return ""
    from core.mesh_contracts import Verdict

    if answer.verifier_status is Verdict.VERIFIED:
        return ""
    body = (answer.answer or "").strip()
    caveat = body[:len(body) - len(streamed.strip())].strip() if (
        streamed.strip() and body.endswith(streamed.strip())) else ""
    if not caveat:
        caveat = _fallback_caveat(answer)
    parts = [caveat]
    if answer.unresolved_questions:
        parts.append("Unresolved: " + "; ".join(answer.unresolved_questions[:3]))
    return "\n\n" + "\n\n".join(p for p in parts if p)


def _fallback_caveat(answer: MeshAnswer) -> str:
    from core.mesh_orchestrator import _CAVEAT

    return _CAVEAT.get(answer.verifier_status, "This result is unverified.")


def should_run_llm_verifier(turn: "MeshTurn | None") -> bool:
    """Whether the pre-existing model verifier should ALSO run.

    It should not, on a turn ARGUS verified. Two independently authoritative
    verification passes over one answer is exactly the duplicated-authority
    shape M64.1 exists to remove, and it would also pay for a second model swap
    on the turns that can least afford one.
    """
    return not (turn is not None and turn.verifier_required)


def effect_epoch(turn: "MeshTurn | None", fallback: str) -> str:
    """The identity of this turn for the ToolExecutor effect ledger (§17)."""
    if turn is not None and (turn.task_id or turn.route.task_id):
        return f"turn:{turn.task_id or turn.route.task_id}"
    return f"turn:{fallback}"


def autonomy_summary(turn: MeshTurn) -> str:
    """One line for the log: who owns this turn and how far they may go."""
    record = REGISTRY.get(turn.route.primary)
    return (f"{record.codename} L{int(turn.route.autonomy_ceiling)}"
            f"{'' if turn.route.autonomy_ceiling is not AutonomyLevel.ADVISE else ' (advise only)'}")
