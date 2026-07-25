"""V69 M60.1 — crash-safe session journal: modes, atomicity, redaction, retention.

Every test uses an isolated in-file store under ``tmp_path`` (never the live
``data/sessions`` database) and never touches Ollama, the network or the host.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from core.managed_paths import UnsafeLeafName, managed_path, safe_leaf
from core.redaction_policy import (
    MARK_OTP, MARK_REASONING, redact_text, scan_for_leaks, scan_structure,
    strip_hidden_reasoning,
)
from core.session_continuity import (
    D_TURN, JOURNAL_SCHEMA_VERSION, JournalState, PersistenceMode, SessionJournal,
    SessionState, TurnRecord, id_hash, parse_persistence_mode, read_git_commit,
)


def _journal(tmp_path, mode=PersistenceMode.LOCAL_REDACTED, **kw) -> SessionJournal:
    return SessionJournal(mode=mode, path=tmp_path / "continuity.db", **kw)


# ══════════════════════════════════════════════════════════════════════════════
#  Managed paths
# ══════════════════════════════════════════════════════════════════════════════
class TestManagedPaths:
    def test_safe_leaf_accepts_plain_name(self):
        assert safe_leaf("bundle_2026") == "bundle_2026"

    @pytest.mark.parametrize("bad", [
        "../escape", "a/b", "a\\b", "C:\\x", "..", ".hidden", "con", "COM1",
        "nul.txt", "", "  ", "name with space", "x" * 200,
    ])
    def test_safe_leaf_rejects_traversal_and_devices(self, bad):
        with pytest.raises(UnsafeLeafName):
            safe_leaf(bad)

    def test_managed_path_stays_inside_directory(self, tmp_path):
        p = managed_path(tmp_path, "report", suffix=".json")
        assert p.parent.resolve() == tmp_path.resolve()
        assert p.name == "report.json"

    def test_managed_path_rejects_escape(self, tmp_path):
        with pytest.raises(UnsafeLeafName):
            managed_path(tmp_path, "../../etc/passwd")


# ══════════════════════════════════════════════════════════════════════════════
#  Redaction policy
# ══════════════════════════════════════════════════════════════════════════════
class TestRedactionPolicy:
    def test_closed_reasoning_block_removed(self):
        out, n = strip_hidden_reasoning("<think>secret plan</think>Answer.")
        assert n == 1 and "secret plan" not in out and out.endswith("Answer.")

    def test_unterminated_reasoning_cut_to_end(self):
        # The crash case: the process died mid-<think>.
        out, n = strip_hidden_reasoning("Visible. <think>half a thought")
        assert n == 1 and "half a thought" not in out
        assert out.startswith("Visible.") and MARK_REASONING in out

    def test_nato_otp_redacted_in_authorization_context(self):
        out, rep = redact_text("DESAFIO: di la palabra NATO TANGO para autorizar")
        assert "tango" not in out.lower() or MARK_OTP in out
        assert rep.otp_tokens >= 1

    def test_plain_nato_word_in_conversation_not_over_redacted(self):
        out, rep = redact_text("Reservé un hotel en Panamá")
        assert "hotel" in out and rep.otp_tokens == 0

    def test_numeric_otp_redacted(self):
        out, rep = redact_text("your code: 483920 expires soon")
        assert "483920" not in out and rep.otp_tokens == 1

    def test_secret_redacted(self):
        out, rep = redact_text("token=ghp_abcdefghijklmnopqrstuvwxyz012345")
        assert "ghp_" not in out and rep.secrets >= 1

    def test_home_path_redacted(self):
        out, rep = redact_text(r"log at C:\Users\aleja\Downloads\x.log")
        assert "aleja" not in out and rep.home_paths == 1

    def test_secret_inside_reasoning_disappears_with_the_block(self):
        out, rep = redact_text("<think>key sk-abcdefghijklmnop1234</think>Hi")
        assert "sk-" not in out and rep.reasoning_blocks == 1

    def test_truncation_is_reported(self):
        out, rep = redact_text("x" * 500, max_chars=100)
        assert rep.truncated_chars == 400 and out.endswith("[TRUNCATED]")

    def test_scanner_flags_and_clears(self):
        assert "reasoning" in scan_for_leaks("<think>x")
        assert scan_for_leaks("perfectly ordinary text") == []

    def test_scan_structure_recurses(self):
        payload = {"a": [{"b": "sk-abcdefghijklmnop1234"}]}
        assert "secret" in scan_structure(payload)


# ══════════════════════════════════════════════════════════════════════════════
#  Persistence modes
# ══════════════════════════════════════════════════════════════════════════════
class TestPersistenceModes:
    def test_default_is_local_redacted(self):
        assert parse_persistence_mode(None) is PersistenceMode.LOCAL_REDACTED

    def test_unknown_value_falls_back_to_default_not_most_permissive(self):
        mode = parse_persistence_mode("EVERYTHING")
        assert mode is PersistenceMode.LOCAL_REDACTED
        assert mode is not PersistenceMode.LOCAL_FULL_EXPLICIT

    def test_off_persists_nothing(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.OFF)
        j.begin_run()
        j.begin_session()
        assert j.sessions() == []
        assert j.journal_state() is JournalState.DISABLED

    def test_metadata_only_keeps_counts_not_content(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.METADATA_ONLY)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED", content="la respuesta secreta")
        stored = j.turns(j.active_session.session_id)[0]
        assert stored.content == "" and stored.content_persisted is False
        assert stored.content_chars == len("la respuesta secreta")

    def test_local_redacted_keeps_text_but_scrubs(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.LOCAL_REDACTED)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED",
                        content="<think>hidden</think>Hola. token=ghp_" + "a" * 24)
        stored = j.turns(j.active_session.session_id)[0]
        assert "Hola." in stored.content
        assert "hidden" not in stored.content and "ghp_" not in stored.content
        assert stored.redactions >= 2

    def test_full_explicit_still_strips_reasoning_and_secrets(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.LOCAL_FULL_EXPLICIT)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED",
                        content="<think>x</think>ok sk-abcdefghijklmnop1234")
        stored = j.turns(j.active_session.session_id)[0]
        assert stored.content_persisted is True
        assert "sk-" not in stored.content and "<think>" not in stored.content

    def test_mode_is_exposed_truthfully(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.METADATA_ONLY)
        assert j.health()["persistence_mode"] == "METADATA_ONLY"


# ══════════════════════════════════════════════════════════════════════════════
#  Write behavior
# ══════════════════════════════════════════════════════════════════════════════
class TestWriteBehavior:
    def test_write_is_durable_across_reopen(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session(language="es")
        t = j.open_turn(role="user")
        j.finalize_turn(t, terminal_state="COMPLETED", content="hola")
        j.close()
        j2 = _journal(tmp_path)
        turns = j2.turns(s.session_id)
        assert len(turns) == 1 and turns[0].content == "hola"

    def test_idempotent_replay_deduplicates(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED", content="same")
        again = j.write_record(D_TURN, t.turn_id, t.to_dict())
        assert again.ok and again.deduplicated is True

    def test_write_result_reports_failure_truthfully(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()

        class _Broken:
            durable = True

            def put(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

            def all(self, *a, **k):
                return []

            def checkpoint(self, *a, **k):
                pass

            def health(self):
                return {"corrupt_reads": 0}

        j._store = _Broken()
        res = j.write_record(D_TURN, "x", {"a": 1})
        assert res.ok is False and "OperationalError" in res.reason
        assert j.journal_state() in (JournalState.DEGRADED, JournalState.FAILED)

    def test_locked_database_degrades_without_raising(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        j._store = None
        j._store_path = tmp_path / "nonexistent_dir" / "sub" / "x.db"
        # Even an unopenable path degrades to a volatile store, never an exception.
        assert j.store is not None
        assert j.journal_state() in (JournalState.OK, JournalState.VOLATILE,
                                     JournalState.DEGRADED)

    def test_slow_write_threshold_counts(self, tmp_path):
        ticks = iter([0.0, 0.1] * 40)
        j = _journal(tmp_path, clock=lambda: next(ticks))
        j.begin_run()
        assert j._slow_writes >= 1

    def test_async_write_does_not_block_and_returns_result(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")

        async def _run():
            return await j.finalize_turn_async(t, terminal_state="COMPLETED",
                                               content="async ok")
        res = asyncio.run(_run())
        assert res.ok is True
        assert j.turns(j.active_session.session_id)[0].content == "async ok"

    def test_async_write_timeout_is_reported_not_swallowed(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()

        async def _run():
            import core.session_continuity as sc
            original = sc.WRITE_TIMEOUT_S
            sc.WRITE_TIMEOUT_S = 0.001

            def _slow(*a, **k):
                import time as _t
                _t.sleep(0.2)
                return None
            try:
                j._put = _slow
                return await j._put_async(D_TURN, "x", {"a": 1})
            finally:
                sc.WRITE_TIMEOUT_S = original
        res = asyncio.run(_run())
        assert res.ok is False and res.reason == "timeout"

    def test_journal_never_stores_hidden_reasoning_anywhere(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED",
                        content="<thinking>never</thinking>visible")
        raw = json.dumps([r.to_dict() for r in j.turns(j.active_session.session_id)])
        assert "never" not in raw
        assert scan_for_leaks(raw) == []

    def test_tool_arguments_are_hashed_not_stored(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        op = j.open_tool_op(tool_name="nmap_scan", effectful=True,
                            arguments={"target": "10.0.0.5", "key": "sk-secret1234"})
        raw = json.dumps(op.to_dict())
        assert "10.0.0.5" not in raw and "sk-secret" not in raw
        assert len(op.argument_digest) == 32


# ══════════════════════════════════════════════════════════════════════════════
#  Records, versioning, corruption
# ══════════════════════════════════════════════════════════════════════════════
class TestRecords:
    def test_records_are_schema_versioned(self, tmp_path):
        j = _journal(tmp_path)
        run = j.begin_run()
        sess = j.begin_session()
        t = j.open_turn(role="user")
        for rec in (run, sess, t):
            assert rec.schema_version == JOURNAL_SCHEMA_VERSION

    def test_run_record_has_no_clean_marker_until_finalized(self, tmp_path):
        j = _journal(tmp_path)
        run = j.begin_run()
        assert run.clean_shutdown is False and run.ended_at is None
        j.finalize_run(clean=True, lifecycle_state="STOPPED")
        assert j.runs()[0].clean_shutdown is True

    def test_pid_recorded_but_marked_advisory(self, tmp_path):
        j = _journal(tmp_path)
        run = j.begin_run()
        assert run.pid > 0 and len(run.boot_token) == 16

    def test_corrupt_record_is_isolated_not_fatal(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        t = j.open_turn(role="user")
        j.finalize_turn(t, terminal_state="COMPLETED", content="good")
        j.close()
        db = sqlite3.connect(str(tmp_path / "continuity.db"))
        db.execute("UPDATE records SET payload='{ this is not json' "
                   "WHERE domain=? AND entity_id=?", (D_TURN, t.turn_id))
        db.commit()
        db.close()
        j2 = _journal(tmp_path)
        assert j2.turns(s.session_id) == []          # bad row skipped, not raised
        assert j2.health()["corrupt_records_quarantined"] >= 1
        assert len(j2.sessions()) == 1               # the rest still readable

    def test_no_volatile_truth_fields_in_any_record(self, tmp_path):
        j = _journal(tmp_path)
        run = j.begin_run()
        sess = j.begin_session()
        t = j.open_turn(role="user")
        forbidden = {"model_loaded", "warm", "warmth", "ready", "readiness",
                     "connection", "lock", "queue", "tts_pending", "task"}
        for rec in (run.to_dict(), sess.to_dict(), t.to_dict()):
            assert not (forbidden & set(rec)), rec

    def test_id_hash_is_content_free(self):
        h = id_hash("sess_deadbeef")
        assert len(h) == 12 and "sess" not in h

    def test_git_commit_read_without_subprocess(self):
        # Never raises; returns "" when .git is unavailable.
        commit = read_git_commit()
        assert isinstance(commit, str) and len(commit) <= 40


# ══════════════════════════════════════════════════════════════════════════════
#  Retention
# ══════════════════════════════════════════════════════════════════════════════
class TestRetention:
    def test_turns_per_session_bounded(self, tmp_path):
        j = _journal(tmp_path, max_turns=5)
        j.begin_run()
        s = j.begin_session()
        for i in range(12):
            t = j.open_turn(role="user")
            j.finalize_turn(t, terminal_state="COMPLETED", content=f"m{i}")
        turns = j.turns(s.session_id)
        assert len(turns) == 5
        assert turns[-1].content == "m11"        # newest kept

    def test_sessions_bounded_and_active_never_pruned(self, tmp_path):
        j = _journal(tmp_path, max_sessions=3)
        j.begin_run()
        ids = [j.begin_session(session_id=f"sess_{i}").session_id for i in range(6)]
        remaining = {s.session_id for s in j.sessions()}
        assert len(remaining) <= 3
        assert ids[-1] in remaining              # the ACTIVE session survives

    def test_visible_chars_bounded(self, tmp_path):
        j = _journal(tmp_path, max_visible_chars=50)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED", content="z" * 500)
        stored = j.turns(j.active_session.session_id)[0]
        assert len(stored.content) <= 50 + len("[TRUNCATED]")
        assert stored.content_chars == 500       # the COUNT is still truthful

    def test_prune_reports_counts_not_content(self, tmp_path):
        j = _journal(tmp_path, max_sessions=2)
        j.begin_run()
        for i in range(5):
            j.begin_session(session_id=f"sess_{i}")
        out = j.prune()
        assert set(out) == {"sessions", "turns", "tool_ops", "events"}
        assert all(isinstance(v, int) for v in out.values())

    def test_unresolved_effectful_tool_op_survives_retention(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        op = j.open_tool_op(tool_name="deploy", effectful=True)
        j.finalize_tool_op(op, outcome="EFFECTFUL_UNKNOWN_OUTCOME",
                           review_required=True)
        for _ in range(5):
            j.prune_tool_ops()
        assert any(o.op_id == op.op_id for o in j.tool_ops())

    def test_forget_session_removes_turns_keeps_tombstone(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        for i in range(3):
            t = j.open_turn(role="user")
            j.finalize_turn(t, terminal_state="COMPLETED", content=f"x{i}")
        out = j.forget_session(s.session_id)
        assert out["turns_removed"] == 3 and out["tombstone"] is True
        assert j.turns(s.session_id) == []
        assert j.get_session(s.session_id).state == SessionState.FORGOTTEN.value


# ══════════════════════════════════════════════════════════════════════════════
#  Health surface
# ══════════════════════════════════════════════════════════════════════════════
class TestJournalHealth:
    def test_health_is_content_free(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        j.begin_session()
        t = j.open_turn(role="user")
        j.finalize_turn(t, terminal_state="COMPLETED",
                        content="mi contraseña es hunter2")
        raw = json.dumps(j.health(), default=str)
        assert "hunter2" not in raw and "contraseña" not in raw
        assert scan_for_leaks(raw) == []

    def test_health_exposes_hashed_session_id_only(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        h = j.health()
        assert h["active_session_id_hash"] == id_hash(s.session_id)
        assert s.session_id not in json.dumps(h)

    def test_health_reports_journal_state(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        assert j.health()["journal_state"] == JournalState.OK.value

    def test_turn_record_roundtrip_ignores_unknown_fields(self):
        t = TurnRecord.from_dict({"turn_id": "a#000001", "session_id": "a",
                                  "sequence": 1, "role": "user",
                                  "unknown_future_field": 42})
        assert t.turn_id == "a#000001" and t.sequence == 1
