"""V69 M60.7 — crash, power-loss and corruption qualification.

Deterministic fault injection only. Every test uses a temporary managed state under
``tmp_path``; the child-process harness runs a small worker script that imports the
real journal but NO inference — no Ollama call is made anywhere in this file, and the
real user runtime is never killed.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.recovery_supervisor import (
    RecoveryPolicy, RecoverySupervisor, RestartDecision, ServiceClass,
)
from core.session_continuity import (
    D_TURN, PersistenceMode, SessionJournal, SessionState,
)
from core.session_restore import restore_session
from core.turn_reconciliation import (
    RecoveredTurnState, RecoveryState, RunClassification, ToolOutcome, reconcile,
)

_REPO = Path(__file__).resolve().parent.parent


def _journal(tmp_path, mode=PersistenceMode.LOCAL_REDACTED) -> SessionJournal:
    return SessionJournal(mode=mode, path=tmp_path / "continuity.db")


# ══════════════════════════════════════════════════════════════════════════════
#  Child-process crash harness (M60.7.1)
# ══════════════════════════════════════════════════════════════════════════════
_WORKER = textwrap.dedent(
    '''
    import sys, os, json
    sys.path.insert(0, sys.argv[1])
    from core.session_continuity import PersistenceMode, SessionJournal

    db, mode, action = sys.argv[2], sys.argv[3], sys.argv[4]
    j = SessionJournal(mode=PersistenceMode(mode), path=db)
    run = j.begin_run(runtime_version="worker")
    sess = j.begin_session(language="es", security_policy_version="m60.0")

    # One COMPLETED turn pair.
    u = j.open_turn(role="user")
    j.finalize_turn(u, terminal_state="COMPLETED", content="pregunta completa")
    a = j.open_turn(role="assistant")
    j.finalize_turn(a, terminal_state="COMPLETED", content="respuesta completa")

    if action in ("crash_partial", "crash_effectful"):
        # Begin a second turn and emit PARTIAL visible content. The tail after the
        # visible marker was generated but never rendered.
        u2 = j.open_turn(role="user")
        j.finalize_turn(u2, terminal_state="COMPLETED", content="segunda pregunta")
        a2 = j.open_turn(role="assistant")
        j.record_visible_progress(a2, visible_chars=10,
                                  content="VISIBLE_10" + "NUNCA_VISTO_TAIL")
        if action == "crash_effectful":
            j.open_tool_op(tool_name="write_config", effectful=True,
                           turn_id=a2.turn_id, arguments={"path": "x"})
    elif action == "crash_before_content":
        u2 = j.open_turn(role="user")
        j.finalize_turn(u2, terminal_state="COMPLETED", content="tercera pregunta")
        j.open_turn(role="assistant")
    elif action == "clean":
        j.close_session()
        j.finalize_run(clean=True, lifecycle_state="STOPPED")

    print(json.dumps({"session_id": sess.session_id, "run_id": run.run_id}),
          flush=True)
    if action == "clean":
        sys.exit(0)
    os._exit(9)   # abrupt: no atexit, no flush, no shutdown callback
    '''
).strip()


def _run_worker(tmp_path, action: str, *,
                mode: str = "LOCAL_REDACTED") -> tuple[dict, int]:
    """Launch the worker child, let it act, and return (ids, exit code)."""
    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    db = str(tmp_path / "continuity.db")
    proc = subprocess.run(
        [sys.executable, str(script), str(_REPO), db, mode, action],
        capture_output=True, text=True, timeout=90,
        cwd=str(_REPO),
    )
    line = (proc.stdout or "").strip().splitlines()
    ids = json.loads(line[-1]) if line else {}
    return ids, proc.returncode


class TestChildProcessCrashHarness:
    """The full M60.7.1 sequence against a REAL abruptly-terminated child process.

    Each worker launch is bounded by the subprocess timeout in :func:`_run_worker`;
    pytest-timeout is not a dependency of this repo, so the bound lives there.
    """

    def test_clean_child_leaves_a_clean_marker(self, tmp_path):
        ids, code = _run_worker(tmp_path, "clean")
        assert code == 0 and ids.get("session_id")
        j = _journal(tmp_path)
        j.begin_run()
        report = reconcile(j)
        assert report.run_classification is RunClassification.CLEAN
        assert report.state is RecoveryState.NOT_REQUIRED

    def test_killed_child_is_detected_as_unclean(self, tmp_path):
        ids, code = _run_worker(tmp_path, "crash_partial")
        assert code != 0
        j = _journal(tmp_path)
        j.begin_run()
        report = reconcile(j)
        assert report.recovery_required is True
        assert report.run_classification in (RunClassification.UNCLEAN_CRASH,
                                             RunClassification.UNCLEAN_POWER_LOSS)

    def test_completed_turn_survives_and_partial_is_marked(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        turns = j.turns(ids["session_id"])
        assert turns[0].content == "pregunta completa"
        assert turns[1].content == "respuesta completa"
        assert turns[-1].terminal_state in (
            RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE.value,
            RecoveredTurnState.INTERRUPTED_BY_POWER_LOSS.value,
            RecoveredTurnState.INTERRUPTED_BY_CRASH.value)

    def test_unseen_text_is_absent_after_recovery(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        blob = json.dumps([t.to_dict() for t in j.turns(ids["session_id"])],
                          ensure_ascii=False)
        assert "NUNCA_VISTO_TAIL" not in blob
        assert "VISIBLE_10" in blob

    def test_no_action_is_replayed_after_an_effectful_crash(self, tmp_path):
        _run_worker(tmp_path, "crash_effectful")
        j = _journal(tmp_path)
        j.begin_run()
        report = reconcile(j)
        assert report.actions_replayed == 0
        assert report.unresolved_tool_outcomes == 1
        assert j.tool_ops()[0].outcome == ToolOutcome.EFFECTFUL_UNKNOWN_OUTCOME.value

    def test_crash_before_content_is_failed_before_content(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_before_content")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        assert j.turns(ids["session_id"])[-1].terminal_state == \
            RecoveredTurnState.FAILED_BEFORE_CONTENT.value

    def test_a_clean_new_turn_succeeds_after_recovery(self, tmp_path):
        _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        fresh = j.begin_session(language="es")
        u = j.open_turn(role="user")
        j.finalize_turn(u, terminal_state="COMPLETED", content="nueva pregunta")
        a = j.open_turn(role="assistant")
        res = j.finalize_turn(a, terminal_state="COMPLETED", content="nueva respuesta")
        assert res.ok is True
        assert len(j.turns(fresh.session_id)) == 2

    def test_clean_shutdown_marker_is_written_after_recovery(self, tmp_path):
        _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        j.finalize_run(clean=True, lifecycle_state="STOPPED")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        assert reconcile(j2).run_classification is RunClassification.CLEAN

    def test_previous_session_remains_readable_and_restorable(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        restored = restore_session(j, session_id=ids["session_id"])
        assert restored.restored_turns >= 2
        joined = json.dumps(restored.turns, ensure_ascii=False)
        assert "NUNCA_VISTO_TAIL" not in joined
        assert restored.partial_turns >= 1

    def test_session_is_marked_interrupted(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        assert j.get_session(ids["session_id"]).state == \
            SessionState.INTERRUPTED.value

    def test_no_temp_file_or_lock_remains(self, tmp_path):
        _run_worker(tmp_path, "crash_partial")
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.suffix in (".tmp", ".lock")]
        assert leftovers == []

    def test_metadata_only_child_leaves_no_content(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial", mode="METADATA_ONLY")
        j = _journal(tmp_path, PersistenceMode.METADATA_ONLY)
        j.begin_run()
        reconcile(j)
        turns = j.turns(ids["session_id"])
        assert turns and all(t.content == "" for t in turns)
        assert any(t.content_chars > 0 for t in turns)


# ══════════════════════════════════════════════════════════════════════════════
#  Storage faults
# ══════════════════════════════════════════════════════════════════════════════
class TestStorageFaults:
    def test_termination_between_transaction_steps_keeps_the_previous_row(
            self, tmp_path):
        # Autocommit means each statement is its own transaction: a failure during
        # the SECOND write leaves the FIRST row intact, never a half-written one.
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED", content="primera version")
        original_put = j.store.put

        def _explode(*a, **k):
            raise sqlite3.OperationalError("killed mid-write")
        j.store.put = _explode
        res = j.write_record(D_TURN, t.turn_id, {"turn_id": t.turn_id,
                                                 "content": "segunda"})
        j.store.put = original_put
        assert res.ok is False
        assert j.turns(j.active_session.session_id)[0].content == "primera version"

    def test_corrupt_record_is_quarantined_and_the_rest_survives(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        for i in range(3):
            t = j.open_turn(role="user")
            j.finalize_turn(t, terminal_state="COMPLETED", content=f"turno {i}")
        victim = j.turns(s.session_id)[1]
        j.close()
        db = sqlite3.connect(str(tmp_path / "continuity.db"))
        db.execute("UPDATE records SET payload='{{{ corrupt' "
                   "WHERE domain=? AND entity_id=?", (D_TURN, victim.turn_id))
        db.commit()
        db.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        turns = j2.turns(s.session_id)
        assert len(turns) == 2
        assert j2.health()["corrupt_records_quarantined"] >= 1

    def test_corrupt_sqlite_file_degrades_without_crashing(self, tmp_path):
        (tmp_path / "continuity.db").write_bytes(b"this is not a database" * 100)
        j = _journal(tmp_path)
        # Opening/reading a garbage file must not raise into the runtime.
        assert isinstance(j.sessions(), list)
        assert isinstance(j.health(), dict)

    def test_database_locked_degrades_honestly(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()

        class _Locked:
            durable = True

            def put(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

            def all(self, *a, **k):
                return []

            def get(self, *a, **k):
                return None

            def checkpoint(self, *a, **k):
                pass

            def health(self):
                return {"corrupt_reads": 0}
        j._store = _Locked()
        res = j.write_record(D_TURN, "x", {"a": 1})
        assert res.ok is False and "OperationalError" in res.reason
        assert j.health()["journal_state"] in ("DEGRADED", "FAILED")

    def test_disk_write_failure_never_reports_success(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()

        class _Full:
            durable = True

            def put(self, *a, **k):
                raise OSError(28, "No space left on device")

            def all(self, *a, **k):
                return []

            def get(self, *a, **k):
                return None

            def checkpoint(self, *a, **k):
                pass

            def health(self):
                return {"corrupt_reads": 0}
        j._store = _Full()
        assert j.write_record(D_TURN, "x", {"a": 1}).ok is False
        assert j.health()["write_failures"] >= 1

    def test_unreadable_journal_never_reports_clean(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()

        class _Unreadable:
            durable = True

            def all(self, *a, **k):
                raise sqlite3.DatabaseError("disk I/O error")

            def get(self, *a, **k):
                return None

            def health(self):
                return {"corrupt_reads": 0}
        j._store = _Unreadable()
        report = reconcile(j)
        assert report.state is RecoveryState.DEGRADED
        assert report.run_classification is not RunClassification.CLEAN

    def test_journal_does_not_grow_without_bound(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.LOCAL_REDACTED)
        j.max_turns = 25
        j.begin_run()
        s = j.begin_session()
        for i in range(200):
            t = j.open_turn(role="user")
            j.finalize_turn(t, terminal_state="COMPLETED", content=f"turno {i}")
        assert len(j.turns(s.session_id)) <= 25
        size = (tmp_path / "continuity.db").stat().st_size
        assert size < 4 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════════
#  Recovery under cancellation / shutdown / policy change
# ══════════════════════════════════════════════════════════════════════════════
class TestRecoveryUnderStress:
    def test_recovery_launches_no_inference(self, tmp_path, monkeypatch):
        _run_worker(tmp_path, "crash_partial")
        import core.turn_reconciliation as tr
        src = __import__("inspect").getsource(tr)
        for banned in ("chat_stream", "ollama", "AsyncOpenAI", "httpx",
                       "generate(", "embed("):
            assert banned not in src, banned

    def test_recovery_is_bounded_in_time(self, tmp_path):
        import time
        j = _journal(tmp_path)
        j.begin_run()
        for k in range(5):
            j.begin_session(session_id=f"sess_{k}")
            for i in range(20):
                t = j.open_turn(role="user")
                if i % 3:
                    j.finalize_turn(t, terminal_state="COMPLETED", content=f"m{i}")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        t0 = time.perf_counter()
        report = reconcile(j2)
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, f"recovery took {elapsed:.2f}s"
        assert report.turns_reconciled > 0

    def test_shutdown_during_recovery_leaves_the_journal_readable(self, tmp_path):
        _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        report = reconcile(j, persist=False)     # simulate: classified, not written
        j.close()
        j2 = _journal(tmp_path)
        assert len(j2.sessions()) >= 1
        assert report.turns_reconciled >= 1

    def test_changed_security_fingerprint_invalidates_continuation(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j, current_fingerprints_map={"security_policy_version": "m99.9"})
        restored = restore_session(
            j, session_id=ids["session_id"],
            current_fingerprints_map={"security_policy_version": "m99.9"})
        assert restored.continuation_valid is False
        assert restored.restored_turns >= 2      # history is preserved

    def test_changed_scope_fingerprint_is_reported(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session(scope_fingerprint="scope_a")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        report = reconcile(j2, current_fingerprints_map={
            "scope_fingerprint": "scope_b"})
        assert "scope_fingerprint" in report.fingerprint_changes

    def test_no_stale_readiness_or_warmth_is_restored(self, tmp_path):
        ids, _ = _run_worker(tmp_path, "crash_partial")
        j = _journal(tmp_path)
        j.begin_run()
        reconcile(j)
        restored = restore_session(j, session_id=ids["session_id"])
        blob = json.dumps(restored.snapshot()).lower()
        for banned in ("warm", "prewarm", "loaded", "ready_model", "tts_pending",
                       "queue_owner", "lock"):
            assert banned not in blob, banned

    def test_stale_run_marker_with_a_live_pid_is_still_unclean(self, tmp_path):
        j = _journal(tmp_path)
        run = j.begin_run()
        run.pid = os.getpid()                    # a genuinely live PID
        j.write_record("continuity_runs", run.run_id, run.to_dict())
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.record_visible_progress(t, visible_chars=4, content="text")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        assert reconcile(j2).recovery_required is True


# ══════════════════════════════════════════════════════════════════════════════
#  Supervisor faults
# ══════════════════════════════════════════════════════════════════════════════
class TestSupervisorFaults:
    def test_restart_storm_is_bounded_by_the_circuit_breaker(self):
        ticks = {"t": 0.0}
        sup = RecoverySupervisor(clock=lambda: ticks["t"],
                                 is_stopping=lambda: False)
        sup.register("storm", policy=RecoveryPolicy(max_restarts=3, window_s=300,
                                                    base_backoff_s=0.0))
        for _ in range(100):
            sup.note_failure("storm", "x")
            if sup.evaluate("storm")[0] is RestartDecision.RESTART:
                sup.commit_restart("storm")
            ticks["t"] += 1.0
        assert sup.get("storm").restart_attempts <= 3
        assert sup.snapshot()["circuits_open"] == 1

    def test_no_restart_after_stopping_even_mid_storm(self):
        stopping = {"v": False}
        ticks = {"t": 0.0}
        sup = RecoverySupervisor(clock=lambda: ticks["t"],
                                 is_stopping=lambda: stopping["v"])
        sup.register("svc", policy=RecoveryPolicy(base_backoff_s=0.0))
        sup.note_failure("svc", "x")
        assert sup.evaluate("svc")[0] is RestartDecision.RESTART
        stopping["v"] = True
        assert sup.evaluate("svc")[0] is RestartDecision.REFUSED_STOPPING

    def test_effectful_task_is_never_restarted_during_recovery(self):
        sup = RecoverySupervisor(is_stopping=lambda: False)
        sup.register("runbook", service_class=ServiceClass.BACKGROUND)
        sup.note_failure("runbook", "x")
        assert sup.evaluate("runbook", operation="effectful_runbook_step")[0] \
            is RestartDecision.REFUSED_EFFECTFUL

    def test_restarted_service_gets_a_fresh_generation(self):
        ticks = {"t": 0.0}
        sup = RecoverySupervisor(clock=lambda: ticks["t"], is_stopping=lambda: False)
        sup.register("collector", policy=RecoveryPolicy(base_backoff_s=0.0))
        generations = []
        for _ in range(3):
            sup.note_failure("collector", "x")
            if sup.evaluate("collector")[0] is RestartDecision.RESTART:
                generations.append(sup.commit_restart("collector").generation)
            ticks["t"] += 400.0
        assert generations == sorted(set(generations))
        assert len(set(generations)) == len(generations)


# ══════════════════════════════════════════════════════════════════════════════
#  Aggregate crash soak
# ══════════════════════════════════════════════════════════════════════════════
class TestCrashSoak:
    @pytest.mark.parametrize("action", ["clean", "crash_partial",
                                        "crash_before_content",
                                        "crash_effectful"])
    def test_every_scenario_recovers_truthfully_and_replays_nothing(
            self, tmp_path, action):
        _run_worker(tmp_path, action)
        j = _journal(tmp_path)
        j.begin_run()
        report = reconcile(j)
        assert report.actions_replayed == 0
        if action == "clean":
            assert report.state is RecoveryState.NOT_REQUIRED
        else:
            assert report.recovery_required is True
            assert report.state in (RecoveryState.COMPLETED,
                                    RecoveryState.COMPLETED_WITH_REVIEW)
        # A second pass must be a no-op: recovery is idempotent.
        assert reconcile(j).turns_reconciled == 0

    def test_repeated_crash_cycles_do_not_grow_the_journal(self, tmp_path):
        sizes = []
        for _ in range(3):
            _run_worker(tmp_path, "crash_partial")
            j = _journal(tmp_path)
            j.begin_run()
            reconcile(j)
            j.prune()
            j.close()
            sizes.append((tmp_path / "continuity.db").stat().st_size)
        assert sizes[-1] < 2 * 1024 * 1024

    def test_no_secret_survives_a_crash_cycle(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.record_visible_progress(
            t, visible_chars=60,
            content="<think>plan</think>clave sk-abcdefghijklmnop1234 y "
                    "codigo: 998877 listo")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        reconcile(j2)
        blob = (tmp_path / "continuity.db").read_bytes().decode("utf-8", "replace")
        assert "sk-abcdefghijklmnop1234" not in blob
        assert "998877" not in blob
        assert "<think>" not in blob


def test_signal_module_available_for_the_harness():
    """The harness uses os._exit rather than a signal, so no signal support is
    required on Windows — asserted so the choice is explicit, not accidental."""
    assert hasattr(signal, "SIGTERM")
    assert "os._exit" in _WORKER
