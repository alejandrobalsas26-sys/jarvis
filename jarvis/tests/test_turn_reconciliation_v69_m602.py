"""V69 M60.2 — interrupted-turn and tool-outcome reconciliation.

Proves the recovery invariants that matter after a crash: no fabricated completion,
no unseen text, no automatic replay, no reused authorization, honest uncertainty.
"""
from __future__ import annotations

import json

import pytest

from core.session_continuity import (
    PersistenceMode, RunRecord, SessionJournal, SessionRecord, SessionState,
    ToolOpRecord, TurnRecord,
)
from core.turn_reconciliation import (
    RecoveredTurnState, RecoveryState, RunClassification, ToolOutcome,
    classify_run, classify_tool_op, classify_turn, compare_fingerprints, reconcile,
    render_recovery_panel,
)


def _journal(tmp_path, mode=PersistenceMode.LOCAL_REDACTED) -> SessionJournal:
    return SessionJournal(mode=mode, path=tmp_path / "continuity.db")


def _crashed(tmp_path, *, visible: str = "Respuesta parcial",
             unseen_tail: str = "") -> tuple[SessionJournal, str]:
    """Simulate a process that finished one turn then died mid-second turn."""
    j = _journal(tmp_path)
    j.begin_run()
    s = j.begin_session(language="es")
    t1 = j.open_turn(role="user")
    j.finalize_turn(t1, terminal_state="COMPLETED", content="pregunta uno")
    t2 = j.open_turn(role="assistant")
    if visible:
        j.record_visible_progress(t2, visible_chars=len(visible),
                                  content=visible + unseen_tail)
    j.close()                                   # no finalize_run -> unclean
    j2 = _journal(tmp_path)
    j2.begin_run()
    return j2, s.session_id


# ══════════════════════════════════════════════════════════════════════════════
#  Run classification
# ══════════════════════════════════════════════════════════════════════════════
class TestRunClassification:
    def test_no_previous_run(self):
        assert classify_run(None) is RunClassification.NO_PREVIOUS_RUN

    def test_clean_run(self):
        r = RunRecord(run_id="r", started_at="t0", ended_at="t1", clean_shutdown=True)
        assert classify_run(r) is RunClassification.CLEAN

    def test_clean_marker_with_unfinished_work_is_unknown_not_clean(self):
        r = RunRecord(run_id="r", started_at="t0", ended_at="t1", clean_shutdown=True)
        assert classify_run(r, had_unfinished_work=True) is RunClassification.UNKNOWN

    def test_explicit_non_clean_end_is_crash(self):
        r = RunRecord(run_id="r", started_at="t0", ended_at="t1", clean_shutdown=False)
        assert classify_run(r) is RunClassification.UNCLEAN_CRASH

    def test_no_end_marker_with_checkpoint_is_crash(self):
        r = RunRecord(run_id="r", started_at="t0", last_checkpoint_at="t0.5")
        assert classify_run(r) is RunClassification.UNCLEAN_CRASH

    def test_no_end_marker_no_checkpoint_is_power_loss(self):
        r = RunRecord(run_id="r", started_at="t0")
        assert classify_run(r) is RunClassification.UNCLEAN_POWER_LOSS

    def test_live_pid_does_not_make_a_run_clean(self):
        import os
        r = RunRecord(run_id="r", started_at="t0", pid=os.getpid())
        # The PID is this very process and therefore "alive" — still UNCLEAN.
        assert classify_run(r) is RunClassification.UNCLEAN_POWER_LOSS


# ══════════════════════════════════════════════════════════════════════════════
#  Turn classification
# ══════════════════════════════════════════════════════════════════════════════
class TestTurnClassification:
    def test_no_content_is_failed_before_content(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant")
        r = classify_turn(t, RunClassification.UNCLEAN_CRASH)
        assert r.state is RecoveredTurnState.FAILED_BEFORE_CONTENT
        assert r.restorable_content == ""

    def test_partial_visible_after_crash(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       content="Hola mund", content_chars=9, content_persisted=True)
        r = classify_turn(t, RunClassification.UNCLEAN_CRASH)
        assert r.state is RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE
        assert r.restorable_content == "Hola mund"

    def test_power_loss_state(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       content="abc", content_chars=3, content_persisted=True)
        r = classify_turn(t, RunClassification.UNCLEAN_POWER_LOSS)
        assert r.state is RecoveredTurnState.INTERRUPTED_BY_POWER_LOSS

    def test_unknown_termination_when_run_class_is_unknown(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       content="abc", content_chars=3, content_persisted=True)
        r = classify_turn(t, RunClassification.UNKNOWN)
        assert r.state is RecoveredTurnState.UNKNOWN_TERMINATION

    def test_unseen_text_is_never_restorable(self):
        # 30 chars persisted but only 9 were ever rendered.
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       content="Hola mundo entero y mas texto", content_chars=9,
                       content_persisted=True)
        r = classify_turn(t, RunClassification.UNCLEAN_CRASH)
        assert r.restorable_content == "Hola mund"
        assert "entero" not in r.restorable_content

    def test_user_turn_without_answer_stays_unresolved(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="user",
                       content="¿cuál es la raíz cúbica de 27?", content_chars=30,
                       content_persisted=True)
        r = classify_turn(t, RunClassification.UNCLEAN_CRASH)
        assert r.state is RecoveredTurnState.UNRESOLVED_USER_MESSAGE
        assert "no finalized assistant response" in r.reason

    def test_snapshot_carries_no_content(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       content="secreto visible", content_chars=15,
                       content_persisted=True)
        snap = classify_turn(t, RunClassification.UNCLEAN_CRASH).snapshot()
        assert "secreto" not in json.dumps(snap, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
#  Tool-outcome classification
# ══════════════════════════════════════════════════════════════════════════════
class TestToolOutcomes:
    def _op(self, **kw) -> ToolOpRecord:
        base = dict(op_id="op1", session_id="s", turn_id="s#1", tool_name="t")
        base.update(kw)
        return ToolOpRecord(**base)

    def test_read_only_completed(self):
        r = classify_tool_op(self._op(effectful=False, finalized_at="t1"))
        assert r.outcome is ToolOutcome.READ_ONLY_COMPLETED
        assert r.review_required is False

    def test_read_only_interrupted_is_safe_to_rerun_manually(self):
        r = classify_tool_op(self._op(effectful=False))
        assert r.outcome is ToolOutcome.READ_ONLY_INTERRUPTED
        assert r.review_required is False and "fresh read-only" in r.recommendation

    def test_effectful_completed(self):
        r = classify_tool_op(self._op(effectful=True, finalized_at="t1"))
        assert r.outcome is ToolOutcome.EFFECTFUL_COMPLETED

    def test_effectful_interrupted_is_unknown_and_needs_review(self):
        r = classify_tool_op(self._op(effectful=True, tool_name="write_file"))
        assert r.outcome is ToolOutcome.EFFECTFUL_UNKNOWN_OUTCOME
        assert r.review_required is True
        assert "verify" in r.recommendation.lower()

    def test_denied_never_needs_review(self):
        r = classify_tool_op(self._op(effectful=True, finalized_at="t1",
                                      outcome="DENIED"))
        assert r.outcome is ToolOutcome.DENIED and r.review_required is False

    def test_failed_effectful_still_needs_verification(self):
        r = classify_tool_op(self._op(effectful=True, finalized_at="t1",
                                      outcome="FAILED"))
        assert r.outcome is ToolOutcome.FAILED and r.review_required is True

    def test_no_authorization_field_is_ever_carried_forward(self):
        op = self._op(effectful=True, audit_ref="audit-42")
        r = classify_tool_op(op)
        raw = json.dumps(r.snapshot())
        for token in ("otp", "hitl", "granted", "approved", "authorization"):
            assert token not in raw.lower()


# ══════════════════════════════════════════════════════════════════════════════
#  Full reconciliation
# ══════════════════════════════════════════════════════════════════════════════
class TestReconcile:
    def test_clean_run_requires_no_recovery(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        t = j.open_turn(role="user")
        j.finalize_turn(t, terminal_state="COMPLETED", content="hola")
        j.close_session()
        j.finalize_run(clean=True, lifecycle_state="STOPPED")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        rep = reconcile(j2)
        assert rep.run_classification is RunClassification.CLEAN
        assert rep.state is RecoveryState.NOT_REQUIRED
        assert rep.recovery_required is False
        assert j2.turns(s.session_id)[0].terminal_state == "COMPLETED"

    def test_unclean_run_is_detected(self, tmp_path):
        j2, _ = _crashed(tmp_path)
        rep = reconcile(j2)
        assert rep.recovery_required is True
        assert rep.run_classification in (RunClassification.UNCLEAN_CRASH,
                                          RunClassification.UNCLEAN_POWER_LOSS)

    def test_completed_turn_preserved_partial_turn_marked(self, tmp_path):
        j2, sid = _crashed(tmp_path, visible="Respuesta parcial")
        reconcile(j2)
        turns = j2.turns(sid)
        assert turns[0].terminal_state == "COMPLETED"
        assert turns[1].terminal_state in (
            RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE.value,
            RecoveredTurnState.INTERRUPTED_BY_POWER_LOSS.value)

    def test_unseen_text_never_appears_after_recovery(self, tmp_path):
        j2, sid = _crashed(tmp_path, visible="Visto", unseen_tail="NUNCA_VISTO")
        reconcile(j2)
        stored = j2.turns(sid)[1]
        assert "NUNCA_VISTO" not in stored.content
        assert stored.content == "Visto"

    def test_no_action_is_ever_replayed(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.open_tool_op(tool_name="delete_files", effectful=True,
                       arguments={"path": "x"}, turn_id=t.turn_id)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        rep = reconcile(j2)
        assert rep.actions_replayed == 0
        assert rep.unresolved_tool_outcomes == 1
        assert rep.state is RecoveryState.COMPLETED_WITH_REVIEW

    def test_effectful_unknown_is_persisted_for_review(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        j.open_tool_op(tool_name="nmap_scan", effectful=True)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        reconcile(j2)
        ops = j2.tool_ops()
        assert ops[0].outcome == ToolOutcome.EFFECTFUL_UNKNOWN_OUTCOME.value
        assert ops[0].review_required is True

    def test_session_marked_interrupted(self, tmp_path):
        j2, sid = _crashed(tmp_path)
        rep = reconcile(j2)
        assert rep.sessions_marked_interrupted >= 1
        assert j2.get_session(sid).state == SessionState.INTERRUPTED.value

    def test_reconcile_is_idempotent(self, tmp_path):
        j2, sid = _crashed(tmp_path)
        first = reconcile(j2)
        second = reconcile(j2)
        # After the first pass the turns are finalized, so nothing remains to reconcile.
        assert first.turns_reconciled >= 1
        assert second.turns_reconciled == 0
        assert second.actions_replayed == 0

    def test_reconcile_never_touches_the_live_session(self, tmp_path):
        j2, _ = _crashed(tmp_path)
        live = j2.begin_session()
        live_turn = j2.open_turn(role="assistant")
        reconcile(j2)
        assert not j2.turns(live.session_id)[0].finalized
        assert j2.turns(live.session_id)[0].turn_id == live_turn.turn_id

    def test_degraded_journal_degrades_report_not_boot(self, tmp_path):
        j = _journal(tmp_path)

        class _Broken:
            durable = True

            def all(self, *a, **k):
                raise RuntimeError("disk failure")

            def get(self, *a, **k):
                return None

            def health(self):
                return {"corrupt_reads": 0}
        j._store = _Broken()
        j.active_run = RunRecord(run_id="r")
        rep = reconcile(j)
        assert rep.state is RecoveryState.DEGRADED
        assert rep.warnings and rep.actions_replayed == 0

    def test_warnings_are_bounded(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        for i in range(60):
            j.open_tool_op(tool_name=f"tool_{i}", effectful=True)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        rep = reconcile(j2)
        assert len(rep.warnings) <= 20

    def test_report_snapshot_is_content_free(self, tmp_path):
        j2, _ = _crashed(tmp_path, visible="texto secreto visible")
        rep = reconcile(j2)
        assert "secreto" not in json.dumps(rep.snapshot(), ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
#  Fingerprint invalidation
# ══════════════════════════════════════════════════════════════════════════════
class TestFingerprintComparison:
    def test_identical_fingerprints_report_no_change(self):
        s = SessionRecord(session_id="s", authority_fingerprint="a",
                          scope_fingerprint="b", security_policy_version="c")
        assert compare_fingerprints(s, {"authority_fingerprint": "a",
                                        "scope_fingerprint": "b",
                                        "security_policy_version": "c"}) == []

    def test_changed_security_policy_detected(self):
        s = SessionRecord(session_id="s", security_policy_version="m58.1")
        out = compare_fingerprints(s, {"security_policy_version": "m60.0"})
        assert out == ["security_policy_version"]

    def test_changed_scope_detected(self):
        s = SessionRecord(session_id="s", scope_fingerprint="old")
        assert "scope_fingerprint" in compare_fingerprints(
            s, {"scope_fingerprint": "new"})

    def test_missing_value_is_not_a_change(self):
        s = SessionRecord(session_id="s", authority_fingerprint="")
        assert compare_fingerprints(s, {"authority_fingerprint": "x"}) == []

    def test_reconcile_reports_fingerprint_changes(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session(security_policy_version="m58.1")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        rep = reconcile(j2, current_fingerprints_map={
            "security_policy_version": "m60.0"})
        assert "security_policy_version" in rep.fingerprint_changes


# ══════════════════════════════════════════════════════════════════════════════
#  Operator panel
# ══════════════════════════════════════════════════════════════════════════════
class TestRecoveryPanel:
    def test_panel_states_zero_replays(self, tmp_path):
        j2, _ = _crashed(tmp_path)
        panel = render_recovery_panel(reconcile(j2))
        assert "actions_automatically_replayed=0" in panel

    @pytest.mark.parametrize("language", ["es", "en"])
    def test_panel_is_cp1252_safe(self, tmp_path, language):
        j2, _ = _crashed(tmp_path)
        panel = render_recovery_panel(reconcile(j2), language=language)
        panel.encode("cp1252")               # must not raise
        assert panel.isascii()

    def test_panel_shows_review_recommendation(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        j.open_tool_op(tool_name="write_file", effectful=True)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        panel = render_recovery_panel(reconcile(j2))
        assert "review: write_file" in panel
        assert "EFFECTFUL_UNKNOWN_OUTCOME" in panel
