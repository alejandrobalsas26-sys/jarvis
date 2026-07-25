"""V69 M60.9 — runtime-health wiring and the live command-dispatch seams.

Asserts that M60 extends the ONE existing health registry (no second registry), that
its metrics are content-free, and that main.py actually reaches the new seams.
"""
from __future__ import annotations

import inspect
import json

import pytest

import core.runtime_health as rh
from core.recovery_state import (
    last_backup_snapshot, last_diagnostics_snapshot, last_recovery_snapshot,
    publish_backup, publish_diagnostics, publish_recovery, reset_recovery_state,
)
from core.redaction_policy import scan_for_leaks
from core.runtime_health import collect_runtime_health
from core.session_continuity import PersistenceMode, SessionJournal


def _journal(tmp_path, mode=PersistenceMode.LOCAL_REDACTED) -> SessionJournal:
    j = SessionJournal(mode=mode, path=tmp_path / "continuity.db")
    j.begin_run()
    j.begin_session()
    return j


# ══════════════════════════════════════════════════════════════════════════════
#  Publication seam
# ══════════════════════════════════════════════════════════════════════════════
class TestRecoveryStatePublication:
    def setup_method(self):
        reset_recovery_state()

    def test_publish_and_read_back(self):
        publish_recovery({"recovery_state": "COMPLETED", "actions_replayed": 0})
        assert last_recovery_snapshot()["recovery_state"] == "COMPLETED"

    def test_snapshots_are_isolated_copies(self):
        publish_backup({"last_backup_state": "OK"})
        snap = last_backup_snapshot()
        snap["last_backup_state"] = "TAMPERED"
        assert last_backup_snapshot()["last_backup_state"] == "OK"

    def test_publication_is_bounded(self):
        publish_diagnostics({f"k{i}": "x" * 500 for i in range(200)})
        snap = last_diagnostics_snapshot()
        assert len(snap) <= 40
        assert all(len(v) <= 200 for v in snap.values())

    def test_lists_are_bounded(self):
        publish_recovery({"warnings": [f"w{i}" for i in range(100)]})
        assert len(last_recovery_snapshot()["warnings"]) <= 12

    def test_empty_before_anything_is_published(self):
        assert last_recovery_snapshot() == {}


# ══════════════════════════════════════════════════════════════════════════════
#  Health subsystem
# ══════════════════════════════════════════════════════════════════════════════
class TestContinuityHealthSubsystem:
    def test_it_is_one_more_entry_on_the_existing_collector(self):
        names = [s.name for s in collect_runtime_health().subsystems]
        assert names.count("session_continuity") == 1
        # The pre-M60 subsystems are all still present — nothing was replaced.
        for existing in ("collectors", "resource", "tasks", "response_pipeline",
                         "prompt_cache", "residency"):
            assert existing in names

    def test_no_second_registry_was_created(self):
        src = inspect.getsource(rh.collect_runtime_health)
        assert src.count("subsystems = [") == 1

    def test_ok_when_the_journal_is_healthy(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "OK", "sessions_retained": 2},
            recovery={"recovery_state": "NOT_REQUIRED"}, supervisor={},
            backup={}, diagnostics={})
        assert sub.status.value == "ok"
        assert sub.healthy is True

    def test_persistence_off_is_dormant_not_a_failure(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "OFF", "journal_state": "DISABLED"},
            recovery={}, supervisor={}, backup={}, diagnostics={})
        assert sub.status.value == "dormant"
        assert sub.healthy is True

    def test_degraded_journal_degrades_the_subsystem(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "DEGRADED"},
            recovery={}, supervisor={}, backup={}, diagnostics={})
        assert sub.healthy is False

    def test_volatile_store_is_not_reported_as_durable(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "VOLATILE"},
            recovery={}, supervisor={}, backup={}, diagnostics={})
        assert sub.healthy is False

    def test_open_circuit_degrades(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "OK"},
            recovery={}, supervisor={"circuits_open": 1}, backup={}, diagnostics={})
        assert sub.healthy is False

    def test_unresolved_tool_outcome_warms_but_does_not_fail(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "OK"},
            recovery={"unresolved_tool_outcomes": 2}, supervisor={},
            backup={}, diagnostics={})
        assert sub.status.value == "warming"

    def test_degraded_recovery_degrades(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "OK"},
            recovery={"recovery_state": "DEGRADED"}, supervisor={},
            backup={}, diagnostics={})
        assert sub.healthy is False

    def test_every_required_metric_is_present(self):
        sub = rh._session_continuity_subsystem(
            persistence={"persistence_mode": "LOCAL_REDACTED",
                         "journal_state": "OK"},
            recovery={}, supervisor={}, backup={}, diagnostics={})
        for key in ("persistence_mode", "active_session_id_hash",
                    "sessions_retained", "turns_retained", "journal_state",
                    "last_checkpoint_ms", "checkpoint_failures",
                    "recovery_required", "recovery_state", "interrupted_turns",
                    "unresolved_tool_outcomes", "corrupt_records_quarantined",
                    "services_registered", "services_ready", "services_degraded",
                    "restart_attempts", "circuits_open", "last_restart_reason",
                    "last_backup_at", "last_backup_state", "last_backup_size",
                    "integrity_verified", "restore_plan_state",
                    "rollback_available", "last_bundle_state", "files_included",
                    "bundle_size", "secret_scan_state"):
            assert key in sub.metrics, key

    def test_metrics_are_content_free(self, tmp_path):
        j = _journal(tmp_path)
        t = j.open_turn(role="assistant")
        j.finalize_turn(t, terminal_state="COMPLETED",
                        content="mi clave es hunter2 y vivo en C:\\Users\\aleja")
        sub = rh._session_continuity_subsystem(persistence=j.health())
        raw = json.dumps(sub.to_dict(), default=str, ensure_ascii=False)
        assert "hunter2" not in raw and "aleja" not in raw
        assert scan_for_leaks(raw) == []

    def test_session_id_is_only_ever_a_hash(self, tmp_path):
        j = _journal(tmp_path)
        sub = rh._session_continuity_subsystem(persistence=j.health())
        raw = json.dumps(sub.to_dict(), default=str)
        assert j.active_session.session_id not in raw
        assert len(sub.metrics["active_session_id_hash"]) == 12

    def test_live_readers_never_raise(self):
        for reader in (rh._live_persistence, rh._live_recovery, rh._live_supervisor,
                       rh._live_backup, rh._live_diagnostics):
            assert isinstance(reader(), dict)

    def test_full_snapshot_stays_json_ready(self):
        json.dumps(collect_runtime_health().to_dict(), default=str)

    def test_summary_line_is_cp1252_safe(self):
        collect_runtime_health().summary().encode("cp1252")


# ══════════════════════════════════════════════════════════════════════════════
#  main.py dispatch wiring
# ══════════════════════════════════════════════════════════════════════════════
class TestMainWiring:
    """Source-level assertions. NOTE: these read main.py from disk, so an edit to
    main.py while the suite is running can make them fail spuriously (the same
    gotcha M58 hit with inspect.getsource)."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        from pathlib import Path
        return Path(__file__).resolve().parent.parent.joinpath(
            "main.py").read_text(encoding="utf-8")

    def test_session_commands_are_dispatched(self, source):
        assert "parse_session_command(user_input)" in source
        assert "apply_session_command(" in source

    def test_deployment_commands_are_dispatched(self, source):
        assert "parse_deployment_command(user_input)" in source
        assert "apply_deployment_command(" in source

    def test_boot_opens_a_run_and_reconciles(self, source):
        assert "begin_run(runtime_version=" in source
        assert "reconcile(_journal" in source
        assert "publish_recovery(" in source

    def test_boot_recomputes_fingerprints(self, source):
        assert "current_fingerprints(" in source

    def test_clean_shutdown_marker_is_registered(self, source):
        assert "_finalize_continuity_run" in source
        assert "finalize_run(clean=True" in source
        assert "register_shutdown_callback(_finalize_continuity_run)" in source

    def test_stale_active_turn_is_closed_at_shutdown(self, source):
        assert 'finalize_stale_active_turns(terminal_state="CANCELLED_ON_SHUTDOWN"' \
            in source

    def test_boot_never_replays(self, source):
        # The boot block reports replayed=0 as a printed fact.
        assert "replayed=0" in source


class TestLlmWiring:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        from pathlib import Path
        return Path(__file__).resolve().parent.parent.joinpath(
            "core", "llm.py").read_text(encoding="utf-8")

    def test_user_turn_is_journalled_before_generation(self, source):
        assert "_journal_turn_pair(user_message)" in source

    def test_turn_is_finalized_in_the_single_idempotent_finalizer(self, source):
        assert "_finalize_journal_turn_sync(getattr(self, \"_journal_turn\"" in source

    def test_stale_active_turn_is_replaced_not_left_open(self, source):
        assert 'finalize_stale_active_turns(terminal_state="REPLACED_BY_NEW_TURN"' \
            in source

    def test_journal_hooks_never_raise_into_a_turn(self, source):
        start = source.index("def _journal_turn_pair")
        end = source.index("def _partial_stream_message")
        block = source[start:end]
        assert block.count("except Exception:") >= 2
