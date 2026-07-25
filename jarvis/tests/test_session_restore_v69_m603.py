"""V69 M60.3 — resumable conversation continuity and the session command surface.

Covers bounded restoration, partial-turn labelling, continuation invalidation, exact
command parsing, the two-step forget confirmation and the redacted export.
"""
from __future__ import annotations

import json

import pytest

from core.redaction_policy import scan_for_leaks, scan_structure
from core.session_commands import (
    DESTRUCTIVE, READ_ONLY, SessionCommand, apply_session_command,
    build_redacted_export, get_forget_confirmation, known_session_aliases,
    parse_session_command, render_sessions, reset_forget_confirmation,
    write_redacted_export,
)
from core.session_continuity import (
    PersistenceMode, SessionJournal, SessionState, TurnRecord, id_hash,
)
from core.session_restore import (
    FORBIDDEN_RESTORE_KEYS, RestoreState, assert_restore_is_clean,
    build_restored_history, render_continuity_panel, restore_session,
)
from core.turn_reconciliation import RecoveredTurnState, RecoveryReport, reconcile


def _journal(tmp_path, mode=PersistenceMode.LOCAL_REDACTED) -> SessionJournal:
    return SessionJournal(mode=mode, path=tmp_path / "continuity.db")


def _conversation(tmp_path, n: int = 3, mode=PersistenceMode.LOCAL_REDACTED):
    j = _journal(tmp_path, mode)
    j.begin_run()
    s = j.begin_session(language="es", security_policy_version="m58.1")
    for i in range(n):
        u = j.open_turn(role="user")
        j.finalize_turn(u, terminal_state="COMPLETED", content=f"pregunta {i}")
        a = j.open_turn(role="assistant")
        j.finalize_turn(a, terminal_state="COMPLETED", content=f"respuesta {i}")
    return j, s


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded history reconstruction
# ══════════════════════════════════════════════════════════════════════════════
class TestRestoredHistory:
    def test_empty_journal_yields_nothing(self):
        history, stats = build_restored_history([])
        assert history == [] and stats["restored"] == 0

    def test_completed_turns_restored_verbatim(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        history, stats = build_restored_history(j.turns(s.session_id))
        assert len(history) == 4 and stats["partial"] == 0
        assert history[0] == {"role": "user", "content": "pregunta 0"}

    def test_turn_count_is_bounded(self, tmp_path):
        j, s = _conversation(tmp_path, n=20)
        history, _ = build_restored_history(j.turns(s.session_id), max_turns=6)
        assert len(history) == 6

    def test_char_budget_is_bounded(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        for i in range(10):
            t = j.open_turn(role="assistant")
            j.finalize_turn(t, terminal_state="COMPLETED", content="x" * 300)
        history, stats = build_restored_history(j.turns(s.session_id),
                                                max_chars=1000)
        assert stats["chars"] <= 1000 + 200      # + partial labels

    def test_newest_turns_are_the_ones_kept(self, tmp_path):
        j, s = _conversation(tmp_path, n=8)
        history, _ = build_restored_history(j.turns(s.session_id), max_turns=2)
        assert "7" in history[-1]["content"]

    def test_partial_turn_is_labelled_not_presented_as_complete(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       terminal_state=RecoveredTurnState.PARTIAL_VISIBLE_RESPONSE.value,
                       content="Solo esto", content_chars=9, content_persisted=True)
        history, stats = build_restored_history([t])
        assert stats["partial"] == 1
        assert "interrumpida" in history[0]["content"]

    def test_never_finalized_turn_is_not_restored(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       terminal_state="ACTIVE", content="incierto",
                       content_chars=8, content_persisted=True)
        history, stats = build_restored_history([t])
        assert history == [] and stats["skipped"] == 1

    def test_failed_before_content_contributes_nothing(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       terminal_state=RecoveredTurnState.FAILED_BEFORE_CONTENT.value)
        history, _ = build_restored_history([t])
        assert history == []

    def test_unresolved_question_is_collected_and_marked(self):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="user",
                       terminal_state=RecoveredTurnState.UNRESOLVED_USER_MESSAGE.value,
                       content="¿y la raíz cúbica?", content_chars=18,
                       content_persisted=True)
        history, stats = build_restored_history([t])
        assert stats["unresolved"] == ["¿y la raíz cúbica?"]
        assert "sin responder" in history[0]["content"]

    @pytest.mark.parametrize("language,expected", [("en", "interrupted"),
                                                   ("es", "interrumpida")])
    def test_label_follows_active_language(self, language, expected):
        t = TurnRecord(turn_id="s#1", session_id="s", sequence=1, role="assistant",
                       terminal_state="TIMED_OUT", content="parcial",
                       content_chars=7, content_persisted=True)
        history, _ = build_restored_history([t], language=language)
        assert expected in history[0]["content"]


# ══════════════════════════════════════════════════════════════════════════════
#  restore_session
# ══════════════════════════════════════════════════════════════════════════════
class TestRestoreSession:
    def test_no_session_reports_no_session(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        assert restore_session(j).state is RestoreState.NO_SESSION

    def test_persistence_off_refuses(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.OFF)
        out = restore_session(j)
        assert out.state is RestoreState.REFUSED and "OFF" in out.notes[0]

    def test_metadata_only_restores_no_text(self, tmp_path):
        j, s = _conversation(tmp_path, n=2, mode=PersistenceMode.METADATA_ONLY)
        j.close()
        j2 = _journal(tmp_path, PersistenceMode.METADATA_ONLY)
        j2.begin_run()
        out = restore_session(j2, session_id=s.session_id)
        assert out.state is RestoreState.METADATA_ONLY
        assert out.turns == [] and out.restored_turns == 0

    def test_restore_last_session_after_clean_exit(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        j.close_session()
        j.finalize_run(clean=True)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        out = restore_session(j2)
        assert out.state is RestoreState.RESTORED
        assert out.session_id == s.session_id and out.restored_turns == 4
        assert out.language == "es"

    def test_forgotten_session_is_refused(self, tmp_path):
        j, s = _conversation(tmp_path, n=1)
        j.forget_session(s.session_id)
        out = restore_session(j, session_id=s.session_id)
        assert out.state is RestoreState.REFUSED

    def test_changed_fingerprint_invalidates_continuation_keeps_history(self,
                                                                       tmp_path):
        j, s = _conversation(tmp_path, n=2)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        out = restore_session(j2, session_id=s.session_id,
                              current_fingerprints_map={
                                  "security_policy_version": "m60.0"})
        assert out.state is RestoreState.RESTORED_WITH_INVALIDATION
        assert out.continuation_valid is False
        assert "security_policy_version" in out.invalidated
        assert out.restored_turns == 4            # history survives

    def test_matching_fingerprint_keeps_continuation_valid(self, tmp_path):
        j, s = _conversation(tmp_path, n=1)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        out = restore_session(j2, session_id=s.session_id,
                              current_fingerprints_map={
                                  "security_policy_version": "m58.1"})
        assert out.continuation_valid is True and out.invalidated == []

    def test_restore_after_crash_marks_partial(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        t1 = j.open_turn(role="user")
        j.finalize_turn(t1, terminal_state="COMPLETED", content="hola")
        t2 = j.open_turn(role="assistant")
        j.record_visible_progress(t2, visible_chars=5, content="parci" + "OCULTO")
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        reconcile(j2)
        out = restore_session(j2, session_id=s.session_id)
        assert out.partial_turns == 1
        joined = json.dumps(out.turns, ensure_ascii=False)
        assert "OCULTO" not in joined and "parci" in joined

    def test_snapshot_is_content_free(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        out = restore_session(j, session_id=s.session_id)
        raw = json.dumps(out.snapshot(), ensure_ascii=False)
        assert "respuesta" not in raw and "pregunta" not in raw
        assert scan_for_leaks(raw) == []

    def test_forbidden_keys_are_named_and_detected(self):
        assert "authority" in FORBIDDEN_RESTORE_KEYS
        assert "warmth" in FORBIDDEN_RESTORE_KEYS
        assert assert_restore_is_clean({"role": "user"}) == []
        assert assert_restore_is_clean({"otp": "tango", "warm": True}) == [
            "otp", "warm"]

    def test_restored_payload_carries_no_forbidden_key(self, tmp_path):
        j, s = _conversation(tmp_path, n=1)
        out = restore_session(j, session_id=s.session_id)
        for msg in out.turns:
            assert assert_restore_is_clean(msg) == []
        assert assert_restore_is_clean(out.snapshot()) == []


# ══════════════════════════════════════════════════════════════════════════════
#  Command parsing (exact allowlist)
# ══════════════════════════════════════════════════════════════════════════════
class TestCommandParsing:
    @pytest.mark.parametrize("alias,expected", [
        ("/session-status", SessionCommand.SESSION_STATUS),
        ("/sessions", SessionCommand.SESSIONS),
        ("/session-new", SessionCommand.SESSION_NEW),
        ("/session-resume-last", SessionCommand.SESSION_RESUME_LAST),
        ("/session-checkpoint", SessionCommand.SESSION_CHECKPOINT),
        ("/session-forget-current", SessionCommand.SESSION_FORGET_CURRENT),
        ("/session-export-redacted", SessionCommand.SESSION_EXPORT_REDACTED),
        ("/recovery-status", SessionCommand.RECOVERY_STATUS),
    ])
    def test_every_required_command_parses(self, alias, expected):
        parsed = parse_session_command(alias)
        assert parsed is not None and parsed.command is expected

    def test_case_and_whitespace_normalised(self):
        assert parse_session_command("  /SESSION-STATUS  ").command \
            is SessionCommand.SESSION_STATUS

    @pytest.mark.parametrize("text", [
        "/session-export-redacted C:\\secrets\\out.json",
        "/session-export-redacted ../../etc/passwd",
        "/session-resume-last 42",
        "/session-forget-current --force",
        "/sessions extra",
        "session-status",
        "por favor haz /session-new",
        "",
        "   ",
        "/unknown-command",
    ])
    def test_arguments_and_near_misses_are_not_commands(self, text):
        assert parse_session_command(text) is None

    def test_spanish_aliases_exist_and_are_exact(self):
        assert parse_session_command("/sesiones").command is SessionCommand.SESSIONS
        assert parse_session_command("/sesiones ya") is None

    def test_allowlist_is_closed(self):
        aliases = known_session_aliases()
        assert all(a.startswith("/") for a in aliases)
        assert len(set(aliases)) == len(aliases)

    def test_read_only_and_destructive_sets_are_disjoint(self):
        assert not (READ_ONLY & DESTRUCTIVE)
        assert SessionCommand.SESSION_FORGET_CURRENT in DESTRUCTIVE


# ══════════════════════════════════════════════════════════════════════════════
#  Command application
# ══════════════════════════════════════════════════════════════════════════════
class TestCommandApplication:
    def _apply(self, alias, journal, **kw):
        return apply_session_command(parse_session_command(alias),
                                     journal=journal, **kw)

    def test_session_status_panel(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        out = self._apply("/session-status", j)
        assert "CONTINUIDAD DE SESION" in out and "mode=LOCAL_REDACTED" in out

    def test_sessions_lists_hashed_ids_only(self, tmp_path):
        j, s = _conversation(tmp_path, n=1)
        out = render_sessions(j)
        assert id_hash(s.session_id) in out and s.session_id not in out
        assert "pregunta" not in out

    def test_session_new_closes_previous_and_opens_one(self, tmp_path):
        j, first = _conversation(tmp_path, n=1)
        out = self._apply("/session-new", j)
        assert "nueva" in out.lower()
        assert j.active_session.session_id != first.session_id
        assert j.get_session(first.session_id).state == SessionState.CLOSED.value

    def test_checkpoint_reports_duration(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        out = self._apply("/session-checkpoint", j)
        assert "control" in out.lower() and "ms" in out

    def test_checkpoint_does_not_touch_model_or_tts(self, tmp_path, monkeypatch):
        j, _ = _conversation(tmp_path, n=1)
        import core.session_commands as sc
        monkeypatch.setattr(sc, "restore_session",
                            lambda *a, **k: pytest.fail("must not restore"))
        self._apply("/session-checkpoint", j)

    def test_resume_last_restores_text_only(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        out = self._apply("/session-resume-last", j2)
        assert "reanudada" in out.lower()
        assert "no se reanudo ninguna accion" in out.lower()

    def test_resume_last_reports_invalidation(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        j.close()
        j2 = _journal(tmp_path)
        j2.begin_run()
        out = self._apply("/session-resume-last", j2,
                          fingerprints={"security_policy_version": "m60.0"})
        assert "invalidated=security_policy_version" in out

    def test_resume_last_with_no_session(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        out = self._apply("/session-resume-last", j)
        assert "no hay sesion previa" in out.lower()

    def test_recovery_status_shows_zero_replays(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        out = self._apply("/recovery-status", j, recovery=RecoveryReport())
        assert "actions_automatically_replayed=0" in out

    def test_persistence_off_reports_disabled(self, tmp_path):
        j = _journal(tmp_path, PersistenceMode.OFF)
        out = self._apply("/session-status", j)
        assert "desactivada" in out.lower()

    @pytest.mark.parametrize("alias", ["/session-status", "/sessions",
                                       "/recovery-status", "/session-checkpoint",
                                       "/session-new", "/session-resume-last"])
    def test_all_output_is_cp1252_safe(self, tmp_path, alias):
        j, _ = _conversation(tmp_path, n=1)
        out = self._apply(alias, j, recovery=RecoveryReport())
        out.encode("cp1252")             # must not raise on the live console


# ══════════════════════════════════════════════════════════════════════════════
#  Two-step forget confirmation
# ══════════════════════════════════════════════════════════════════════════════
class TestForgetConfirmation:
    def setup_method(self):
        reset_forget_confirmation()

    def test_first_command_only_arms(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        out = apply_session_command(
            parse_session_command("/session-forget-current"), journal=j)
        assert "confirm" in out.lower()
        assert len(j.turns(s.session_id)) == 4       # nothing deleted yet

    def test_confirmation_deletes_content_keeps_tombstone(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        apply_session_command(parse_session_command("/session-forget-current"),
                              journal=j)
        out = apply_session_command(
            parse_session_command("/session-forget-confirm"), journal=j)
        assert "4" in out
        assert j.turns(s.session_id) == []
        assert j.get_session(s.session_id).state == SessionState.FORGOTTEN.value

    def test_confirm_without_arm_is_refused(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        out = apply_session_command(
            parse_session_command("/session-forget-confirm"), journal=j)
        assert "no pending" in out.lower() or "no hay confirmacion" in out.lower()
        assert len(j.turns(s.session_id)) == 4

    def test_confirmation_is_single_use(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        conf = get_forget_confirmation()
        sid = j.active_session.session_id
        conf.arm(sid)
        assert conf.consume(sid) is True
        assert conf.consume(sid) is False

    def test_confirmation_is_session_scoped(self, tmp_path):
        conf = get_forget_confirmation()
        conf.arm("sess_a")
        assert conf.consume("sess_b") is False
        assert conf.is_armed_for("sess_a") is True

    def test_intervening_turn_expires_confirmation(self, tmp_path):
        conf = get_forget_confirmation()
        conf.arm("sess_a")
        conf.expire_turn()
        assert conf.is_armed_for("sess_a") is False


# ══════════════════════════════════════════════════════════════════════════════
#  Redacted export
# ══════════════════════════════════════════════════════════════════════════════
class TestRedactedExport:
    def test_export_payload_is_scanned_clean(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        payload = build_redacted_export(j, session_id=s.session_id)
        assert payload["secret_scan"] == "CLEAN"
        assert scan_structure(payload) == []

    def test_export_uses_hashed_session_id(self, tmp_path):
        j, s = _conversation(tmp_path, n=1)
        payload = build_redacted_export(j, session_id=s.session_id)
        assert payload["session_id_hash"] == id_hash(s.session_id)
        assert s.session_id not in json.dumps(payload)

    def test_export_is_size_bounded(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        s = j.begin_session()
        for _ in range(60):
            t = j.open_turn(role="assistant")
            j.finalize_turn(t, terminal_state="COMPLETED", content="y" * 2000)
        payload = build_redacted_export(j, session_id=s.session_id, max_bytes=4096)
        assert payload.get("truncated") is True
        assert len(json.dumps(payload)) < 20000

    def test_metadata_only_export_has_no_text(self, tmp_path):
        j, s = _conversation(tmp_path, n=2, mode=PersistenceMode.METADATA_ONLY)
        payload = build_redacted_export(j, session_id=s.session_id)
        assert all(t["content"] == "" for t in payload["turns"])
        assert all(t["chars"] > 0 for t in payload["turns"])

    def test_export_writes_to_managed_directory_only(self, tmp_path, monkeypatch):
        import core.session_commands as sc
        monkeypatch.setattr(sc, "exports_dir", lambda **kw: tmp_path)
        j, s = _conversation(tmp_path, n=1)
        res = write_redacted_export(j, session_id=s.session_id)
        assert res["state"] == "WRITTEN"
        written = tmp_path / res["file"]
        assert written.exists() and written.parent == tmp_path

    def test_export_leaves_no_temp_file(self, tmp_path, monkeypatch):
        import core.session_commands as sc
        monkeypatch.setattr(sc, "exports_dir", lambda **kw: tmp_path)
        j, s = _conversation(tmp_path, n=1)
        write_redacted_export(j, session_id=s.session_id)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_export_rejects_unsafe_generated_name(self, tmp_path, monkeypatch):
        import core.session_commands as sc
        monkeypatch.setattr(sc, "exports_dir", lambda **kw: tmp_path)
        j, s = _conversation(tmp_path, n=1)
        res = write_redacted_export(j, session_id=s.session_id, name="../escape")
        assert res["error"] == "unsafe_name"

    def test_export_no_session_reports_error(self, tmp_path):
        j = _journal(tmp_path)
        j.begin_run()
        assert build_redacted_export(j)["error"] == "no_session"


# ══════════════════════════════════════════════════════════════════════════════
#  Continuity panel
# ══════════════════════════════════════════════════════════════════════════════
class TestContinuityPanel:
    def test_panel_states_readiness_is_remeasured(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        panel = render_continuity_panel(j, language="en")
        assert "re-measured, never restored" in panel

    def test_panel_has_no_conversation_text(self, tmp_path):
        j, s = _conversation(tmp_path, n=2)
        restored = restore_session(j, session_id=s.session_id)
        panel = render_continuity_panel(j, restored, RecoveryReport())
        assert "respuesta" not in panel and "pregunta" not in panel
        assert scan_for_leaks(panel) == []

    def test_panel_is_ascii(self, tmp_path):
        j, _ = _conversation(tmp_path, n=1)
        assert render_continuity_panel(j).isascii()
