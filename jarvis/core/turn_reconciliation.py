"""core/turn_reconciliation.py — V69 M60.2: truthful recovery after an unclean exit.

THE ONE RULE
------------
Recovery may only ever say LESS than it knows. It classifies what the journal proves,
marks everything else UNKNOWN, and REPLAYS NOTHING. There is no code path in this
module that executes a tool, retries an action, re-sends a command, reuses an
authorization or produces an assistant answer.

CLEAN VS UNCLEAN
----------------
A run is clean only when its RUN record carries an explicit ``clean_shutdown`` marker
written by the shutdown driver. Everything else — a missing marker, an unfinished
turn, an in-flight tool op, a run whose ``ended_at`` is None — is UNCLEAN.

PID existence is deliberately NOT used as proof: on Windows a PID is reused within
minutes, so "a process with that id exists" says nothing about whether it is JARVIS.
The PID is recorded and reported as advisory context only, and a stale run marker
whose PID happens to be alive is still reconciled as unclean.

TURN STATES PRODUCED
--------------------
    FAILED_BEFORE_CONTENT      opened, nothing was ever shown
    PARTIAL_VISIBLE_RESPONSE   the operator saw N characters; only those are restored
    INTERRUPTED_BY_CRASH       the process died with the journal still open
    INTERRUPTED_BY_POWER_LOSS  no shutdown marker AND no in-process failure evidence
    UNKNOWN_TERMINATION        the journal cannot distinguish; say so
    REVIEW_REQUIRED            an effectful tool outcome is uncertain

TOOL OUTCOMES PRODUCED
----------------------
    READ_ONLY_COMPLETED / READ_ONLY_INTERRUPTED
    EFFECTFUL_COMPLETED / EFFECTFUL_UNKNOWN_OUTCOME
    DENIED / FAILED / REVIEW_REQUIRED

An EFFECTFUL_UNKNOWN_OUTCOME is never rerun automatically and never presented as
"probably fine": the operator gets the intended action, the audit reference, the
uncertainty and a verification recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.session_continuity import (
    D_SESSION, D_TOOL_OP, D_TURN, MAX_RECOVERY_WARNINGS, RunRecord, SessionRecord,
    SessionState, ToolOpRecord, TurnRecord,
)


class RecoveredTurnState(str, Enum):
    """Deterministic post-crash turn classifications."""

    FAILED_BEFORE_CONTENT = "FAILED_BEFORE_CONTENT"
    PARTIAL_VISIBLE_RESPONSE = "PARTIAL_VISIBLE_RESPONSE"
    INTERRUPTED_BY_CRASH = "INTERRUPTED_BY_CRASH"
    INTERRUPTED_BY_POWER_LOSS = "INTERRUPTED_BY_POWER_LOSS"
    UNKNOWN_TERMINATION = "UNKNOWN_TERMINATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNRESOLVED_USER_MESSAGE = "UNRESOLVED_USER_MESSAGE"


class ToolOutcome(str, Enum):
    READ_ONLY_COMPLETED = "READ_ONLY_COMPLETED"
    READ_ONLY_INTERRUPTED = "READ_ONLY_INTERRUPTED"
    EFFECTFUL_COMPLETED = "EFFECTFUL_COMPLETED"
    EFFECTFUL_UNKNOWN_OUTCOME = "EFFECTFUL_UNKNOWN_OUTCOME"
    DENIED = "DENIED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class RunClassification(str, Enum):
    NO_PREVIOUS_RUN = "NO_PREVIOUS_RUN"
    CLEAN = "CLEAN"
    UNCLEAN_CRASH = "UNCLEAN_CRASH"
    UNCLEAN_POWER_LOSS = "UNCLEAN_POWER_LOSS"
    UNKNOWN = "UNKNOWN"


class RecoveryState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_REVIEW = "COMPLETED_WITH_REVIEW"
    DEGRADED = "DEGRADED"
    CANCELLED = "CANCELLED"


# States after which a turn is unusable as conversation context.
_NON_RESTORABLE = frozenset({
    RecoveredTurnState.FAILED_BEFORE_CONTENT,
    RecoveredTurnState.UNRESOLVED_USER_MESSAGE,
})


@dataclass
class ReconciledTurn:
    """One reconciled turn. Content is what the operator SAW — never more."""

    turn_id: str
    session_id: str
    sequence: int
    role: str
    state: RecoveredTurnState
    visible_chars: int = 0
    restorable_content: str = ""
    reason: str = ""
    review_required: bool = False

    def snapshot(self) -> dict:
        # Deliberately omits ``restorable_content`` — a snapshot feeds health and
        # panels, which are content-free by contract.
        return {"turn_id": self.turn_id, "sequence": self.sequence,
                "role": self.role, "state": self.state.value,
                "visible_chars": self.visible_chars, "reason": self.reason,
                "review_required": self.review_required}


@dataclass
class ReconciledToolOp:
    op_id: str
    tool_name: str
    effectful: bool
    outcome: ToolOutcome
    review_required: bool
    audit_ref: str = ""
    recommendation: str = ""
    summary: str = ""

    def snapshot(self) -> dict:
        return {"op_id": self.op_id, "tool": self.tool_name,
                "effectful": self.effectful, "outcome": self.outcome.value,
                "review_required": self.review_required,
                "audit_ref": self.audit_ref, "recommendation": self.recommendation}


@dataclass
class RecoveryReport:
    """The complete, bounded outcome of one recovery pass. Content-free snapshot."""

    run_classification: RunClassification = RunClassification.NO_PREVIOUS_RUN
    state: RecoveryState = RecoveryState.NOT_REQUIRED
    previous_run_id: str = ""
    previous_run_pid: int = 0
    turns_reconciled: int = 0
    interrupted_turns: int = 0
    unresolved_user_messages: int = 0
    tool_ops_reconciled: int = 0
    unresolved_tool_outcomes: int = 0
    actions_replayed: int = 0            # STRUCTURALLY always 0 — asserted in tests
    warnings: list[str] = field(default_factory=list)
    turns: list[ReconciledTurn] = field(default_factory=list)
    tool_ops: list[ReconciledToolOp] = field(default_factory=list)
    sessions_marked_interrupted: int = 0
    fingerprint_changes: list[str] = field(default_factory=list)

    @property
    def recovery_required(self) -> bool:
        return self.run_classification in (RunClassification.UNCLEAN_CRASH,
                                           RunClassification.UNCLEAN_POWER_LOSS,
                                           RunClassification.UNKNOWN)

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_RECOVERY_WARNINGS:
            self.warnings.append(str(message)[:120])

    def snapshot(self) -> dict:
        return {
            "run_classification": self.run_classification.value,
            "recovery_state": self.state.value,
            "recovery_required": self.recovery_required,
            "previous_run_pid": self.previous_run_pid,
            "turns_reconciled": self.turns_reconciled,
            "interrupted_turns": self.interrupted_turns,
            "unresolved_user_messages": self.unresolved_user_messages,
            "tool_ops_reconciled": self.tool_ops_reconciled,
            "unresolved_tool_outcomes": self.unresolved_tool_outcomes,
            "actions_replayed": self.actions_replayed,
            "sessions_marked_interrupted": self.sessions_marked_interrupted,
            "fingerprint_changes": list(self.fingerprint_changes),
            "warnings": list(self.warnings),
            "turns": [t.snapshot() for t in self.turns[:MAX_RECOVERY_WARNINGS]],
            "tool_ops": [o.snapshot() for o in self.tool_ops[:MAX_RECOVERY_WARNINGS]],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Pure classifiers
# ══════════════════════════════════════════════════════════════════════════════
def classify_run(previous: RunRecord | None, *,
                 had_unfinished_work: bool = False) -> RunClassification:
    """Clean/unclean from EVIDENCE, never from PID liveness.

    A run is clean only if it carries both an ``ended_at`` and a ``clean_shutdown``
    marker AND left no unfinished work behind. A run that claims a clean shutdown but
    still has an open turn is UNKNOWN — the two facts disagree, and recovery reports
    the disagreement instead of picking the convenient one.
    """
    if previous is None:
        return RunClassification.NO_PREVIOUS_RUN
    if previous.ended_at is not None and previous.clean_shutdown:
        return (RunClassification.UNKNOWN if had_unfinished_work
                else RunClassification.CLEAN)
    if previous.ended_at is not None and not previous.clean_shutdown:
        # The shutdown path ran and explicitly recorded a non-clean exit.
        return RunClassification.UNCLEAN_CRASH
    # No end marker at all: the process never reached its shutdown driver.
    # A checkpoint that landed AFTER the run started is evidence the process was
    # alive and working, i.e. it was killed rather than never having run.
    if previous.last_checkpoint_at:
        return RunClassification.UNCLEAN_CRASH
    return RunClassification.UNCLEAN_POWER_LOSS


def classify_turn(turn: TurnRecord, run_class: RunClassification) -> ReconciledTurn:
    """Classify ONE unfinished turn. Only visible content is ever restorable."""
    seen = max(0, int(turn.content_chars or 0))
    content = turn.content if turn.content_persisted else ""
    # Never restore more characters than the operator actually saw. A stream may have
    # persisted a checkpoint of N chars while the model had produced more; the extra
    # text was never rendered, so it must not reappear.
    if content and seen and len(content) > seen:
        content = content[:seen]

    if turn.role == "user":
        # A user message with no finalized assistant response stays UNRESOLVED.
        # Recovery does NOT invent an answer for it.
        return ReconciledTurn(
            turn_id=turn.turn_id, session_id=turn.session_id, sequence=turn.sequence,
            role=turn.role, state=RecoveredTurnState.UNRESOLVED_USER_MESSAGE,
            visible_chars=seen, restorable_content=content,
            reason="no finalized assistant response")

    if seen <= 0:
        return ReconciledTurn(
            turn_id=turn.turn_id, session_id=turn.session_id, sequence=turn.sequence,
            role=turn.role, state=RecoveredTurnState.FAILED_BEFORE_CONTENT,
            visible_chars=0, reason="no visible content was produced")

    if run_class is RunClassification.UNCLEAN_POWER_LOSS:
        state = RecoveredTurnState.INTERRUPTED_BY_POWER_LOSS
        reason = "power loss during generation"
    elif run_class is RunClassification.UNCLEAN_CRASH:
        state = RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE
        reason = "process terminated mid-response"
    else:
        state = RecoveredTurnState.UNKNOWN_TERMINATION
        reason = "termination cause not determinable from the journal"
    return ReconciledTurn(
        turn_id=turn.turn_id, session_id=turn.session_id, sequence=turn.sequence,
        role=turn.role, state=state, visible_chars=seen,
        restorable_content=content, reason=reason)


def classify_tool_op(op: ToolOpRecord) -> ReconciledToolOp:
    """Classify ONE tool operation. Effectful + unfinished is ALWAYS uncertain."""
    finished = op.finalized_at is not None
    declared = str(op.outcome or "").upper()

    if declared in ("DENIED", "BLOCKED"):
        return ReconciledToolOp(op.op_id, op.tool_name, op.effectful,
                                ToolOutcome.DENIED, False, op.audit_ref,
                                "no action was taken", op.summary)
    if declared in ("FAILED", "ERROR") and finished:
        return ReconciledToolOp(op.op_id, op.tool_name, op.effectful,
                                ToolOutcome.FAILED, op.effectful, op.audit_ref,
                                "verify no partial effect remains" if op.effectful
                                else "safe to retry manually", op.summary)
    if finished:
        outcome = (ToolOutcome.EFFECTFUL_COMPLETED if op.effectful
                   else ToolOutcome.READ_ONLY_COMPLETED)
        return ReconciledToolOp(op.op_id, op.tool_name, op.effectful, outcome,
                                False, op.audit_ref, "", op.summary)
    if op.effectful:
        # The single most dangerous case: an effectful action was in flight when the
        # process died. It may have completed, partially completed or never started.
        return ReconciledToolOp(
            op.op_id, op.tool_name, True, ToolOutcome.EFFECTFUL_UNKNOWN_OUTCOME, True,
            op.audit_ref,
            "verify the target state manually before repeating this action",
            op.summary)
    return ReconciledToolOp(
        op.op_id, op.tool_name, False, ToolOutcome.READ_ONLY_INTERRUPTED, False,
        op.audit_ref, "safe to run again as a fresh read-only operation", op.summary)


def compare_fingerprints(stored: SessionRecord, current: dict) -> list[str]:
    """Which stamped fingerprints no longer match the RECOMPUTED ones.

    A difference invalidates prefix/continuation state and is REPORTED; it never
    silently rewrites the stored session, and it never causes history to be deleted.
    """
    changes: list[str] = []
    for field_name, key in (
        ("authority_fingerprint", "authority_fingerprint"),
        ("scope_fingerprint", "scope_fingerprint"),
        ("security_policy_version", "security_policy_version"),
        ("prompt_fingerprint", "prompt_fingerprint"),
        ("tool_schema_fingerprint", "tool_schema_fingerprint"),
    ):
        was = str(getattr(stored, field_name, "") or "")
        now = str((current or {}).get(key, "") or "")
        if was and now and was != now:
            changes.append(field_name)
    return changes


# ══════════════════════════════════════════════════════════════════════════════
#  The reconciler
# ══════════════════════════════════════════════════════════════════════════════
def reconcile(journal, *, current_fingerprints_map: dict | None = None,
              persist: bool = True) -> RecoveryReport:
    """Run ONE bounded recovery pass over the journal.

    Never launches inference, never touches the network, never executes a tool and
    never restores an authorization. Every failure degrades the report instead of
    raising into boot.
    """
    report = RecoveryReport()
    reads_before = int(getattr(journal, "read_failures", 0))
    try:
        previous = journal.previous_run()
    except Exception as exc:  # noqa: BLE001
        report.state = RecoveryState.DEGRADED
        report.warn(f"run lookup failed: {type(exc).__name__}")
        return report

    try:
        unfinished_turns = journal.unfinished_turns()
        unfinished_ops = journal.unfinished_tool_ops()
    except Exception as exc:  # noqa: BLE001
        report.state = RecoveryState.DEGRADED
        report.warn(f"journal read failed: {type(exc).__name__}")
        return report

    # The journal swallows a failed read into an empty list so a corrupt row cannot
    # be fatal. That is right for the RUNTIME and wrong for RECOVERY: "I read nothing"
    # and "I could not read" must not produce the same verdict. If any read failed
    # during this pass, the report is DEGRADED and says so — it never claims clean.
    read_failures = int(getattr(journal, "read_failures", 0)) - reads_before
    if read_failures > 0:
        report.state = RecoveryState.DEGRADED
        report.run_classification = RunClassification.UNKNOWN
        report.warn(f"journal unreadable: {read_failures} failed read(s)")
        return report

    # Only work that does NOT belong to the live session is reconciled: the current
    # session's in-flight turn is live state, not wreckage from a previous process.
    live_session = (journal.active_session.session_id
                    if journal.active_session is not None else None)
    unfinished_turns = [t for t in unfinished_turns if t.session_id != live_session]
    unfinished_ops = [o for o in unfinished_ops if o.session_id != live_session]

    had_work = bool(unfinished_turns or unfinished_ops)
    report.run_classification = classify_run(previous, had_unfinished_work=had_work)
    if previous is not None:
        report.previous_run_id = previous.run_id
        report.previous_run_pid = int(previous.pid or 0)
        if report.run_classification is RunClassification.UNKNOWN:
            report.warn("run recorded a clean shutdown but left unfinished work")

    for turn in unfinished_turns:
        rt = classify_turn(turn, report.run_classification)
        report.turns.append(rt)
        report.turns_reconciled += 1
        if rt.state is RecoveredTurnState.UNRESOLVED_USER_MESSAGE:
            report.unresolved_user_messages += 1
        elif rt.state not in _NON_RESTORABLE:
            report.interrupted_turns += 1
        if persist:
            _persist_turn_state(journal, turn, rt, report)

    for op in unfinished_ops:
        ro = classify_tool_op(op)
        report.tool_ops.append(ro)
        report.tool_ops_reconciled += 1
        if ro.review_required:
            report.unresolved_tool_outcomes += 1
            report.warn(f"{ro.tool_name}: {ro.outcome.value}")
        if persist:
            _persist_tool_state(journal, op, ro, report)

    # Mark every touched session INTERRUPTED so /sessions tells the truth later.
    if persist and report.recovery_required:
        touched = {t.session_id for t in report.turns}
        for sid in touched:
            try:
                sess = journal.get_session(sid)
                if sess is None or sess.state == SessionState.FORGOTTEN.value:
                    continue
                sess.state = SessionState.INTERRUPTED.value
                journal.write_record(D_SESSION, sess.session_id, sess.to_dict())
                report.sessions_marked_interrupted += 1
            except Exception as exc:  # noqa: BLE001
                report.warn(f"session mark failed: {type(exc).__name__}")

    if current_fingerprints_map:
        last = journal.last_session()
        if last is not None:
            report.fingerprint_changes = compare_fingerprints(
                last, current_fingerprints_map)
            for change in report.fingerprint_changes:
                report.warn(f"fingerprint changed: {change}")

    if report.state is RecoveryState.DEGRADED:
        pass
    elif not report.recovery_required:
        report.state = RecoveryState.NOT_REQUIRED
    elif report.unresolved_tool_outcomes:
        report.state = RecoveryState.COMPLETED_WITH_REVIEW
    else:
        report.state = RecoveryState.COMPLETED

    # STRUCTURAL invariant: this function has no execution path. Asserted, not hoped.
    report.actions_replayed = 0
    return report


def _persist_turn_state(journal, turn: TurnRecord, rt: ReconciledTurn,
                        report: RecoveryReport) -> None:
    """Write the reconciled terminal state back, making the turn permanently truthful.

    The stored content is trimmed to what was seen, so a later restore cannot surface
    text the operator never read even if this process crashes again.
    """
    try:
        turn.terminal_state = rt.state.value
        turn.finalized_at = turn.finalized_at or _reconciled_stamp(turn)
        if turn.content_persisted and rt.restorable_content != turn.content:
            turn.content = rt.restorable_content
        journal.write_record(D_TURN, turn.turn_id, turn.to_dict())
    except Exception as exc:  # noqa: BLE001
        report.warn(f"turn reconcile write failed: {type(exc).__name__}")


def _persist_tool_state(journal, op: ToolOpRecord, ro: ReconciledToolOp,
                        report: RecoveryReport) -> None:
    try:
        op.outcome = ro.outcome.value
        op.review_required = ro.review_required
        op.finalized_at = op.finalized_at or _reconciled_stamp(op)
        journal.write_record(D_TOOL_OP, op.op_id, op.to_dict())
    except Exception as exc:  # noqa: BLE001
        report.warn(f"tool reconcile write failed: {type(exc).__name__}")


def _reconciled_stamp(record) -> str:
    """A finalize timestamp for a record the previous process never closed.

    Derived from the record's OWN start time, not from ``now`` — stamping recovery
    time would claim the turn ran until this boot, which is false.
    """
    return f"{getattr(record, 'started_at', '') or ''}|reconciled"


# ── Operator panel (bounded, ASCII, content-free) ────────────────────────────
def render_recovery_panel(report: RecoveryReport, *, language: str = "es") -> str:
    """The RECOVERY panel. ``actions_replayed=0`` is printed as a fact, every time."""
    english = str(language or "es").lower().startswith("en")
    title = "RECOVERY" if english else "RECUPERACION"
    rows = [
        ("previous_run", report.run_classification.value),
        ("recovery_state", report.state.value),
        ("turns_reconciled", report.turns_reconciled),
        ("interrupted_turns", report.interrupted_turns),
        ("unresolved_user_messages", report.unresolved_user_messages),
        ("tool_outcomes_reviewed", report.tool_ops_reconciled),
        ("tool_outcomes_requiring_review", report.unresolved_tool_outcomes),
        ("actions_automatically_replayed", report.actions_replayed),
    ]
    lines = [title] + [f"  {k}={v}" for k, v in rows]
    for op in report.tool_ops[:5]:
        if op.review_required:
            lines.append(f"  review: {op.tool_name} -> {op.outcome.value}")
            if op.recommendation:
                lines.append(f"    {op.recommendation}")
    if report.fingerprint_changes:
        lines.append("  invalidated=" + ",".join(report.fingerprint_changes))
    note = ("nothing was replayed; effectful outcomes need operator verification"
            if english else
            "no se repitio ninguna accion; los efectos inciertos requieren "
            "verificacion del operador")
    lines.append(f"  ({note})")
    return "\n".join(lines)
