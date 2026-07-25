"""V69 M60.6 — redacted diagnostics bundles, managed backup and dry-run restore."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import core.managed_backup as mb
from core.diagnostics_bundle import (
    BundleMode, BundleState, build_bundle, render_diagnostics_panel, write_bundle,
)
from core.managed_backup import (
    BACKUP_SCHEMA_VERSION, MANIFEST_NAME, BackupState, RestoreState, apply_restore,
    create_backup, eligible_artifacts, list_backups, plan_restore,
    render_backup_panel, verify_backup,
)
from core.redaction_policy import scan_structure
from core.session_continuity import PersistenceMode, SessionJournal


def _journal(tmp_path) -> SessionJournal:
    j = SessionJournal(mode=PersistenceMode.LOCAL_REDACTED,
                       path=tmp_path / "continuity.db")
    j.begin_run()
    j.begin_session()
    t = j.open_turn(role="user")
    j.finalize_turn(t, terminal_state="COMPLETED",
                    content="mi contraseña secreta es hunter2")
    return j


def _fake_root(tmp_path) -> Path:
    """A managed tree with the eligible artifacts present."""
    (tmp_path / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "sessions" / "session_continuity.db").write_bytes(b"SQLITE-A")
    (tmp_path / "data" / "operational_state.db").write_bytes(b"SQLITE-B")
    (tmp_path / "data" / "alias_registry.json").write_text('{"a":1}',
                                                           encoding="utf-8")
    return tmp_path


# ══════════════════════════════════════════════════════════════════════════════
#  Diagnostics bundle
# ══════════════════════════════════════════════════════════════════════════════
class TestDiagnosticsBundle:
    def test_preview_writes_nothing(self, tmp_path, monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "diagnostics_dir", lambda **kw: tmp_path)
        res = write_bundle(BundleMode.PREVIEW)
        assert res.state is BundleState.PREVIEW
        assert list(tmp_path.iterdir()) == []

    def test_preview_and_redacted_share_the_same_sections(self):
        preview = build_bundle(BundleMode.PREVIEW)
        redacted = build_bundle(BundleMode.REDACTED)
        assert preview.sections == redacted.sections

    def test_bundle_has_no_full_mode(self):
        assert not any(m.value == "FULL" for m in BundleMode)
        with pytest.raises(ValueError):
            BundleMode("FULL")

    def test_bundle_contains_no_conversation_content(self, tmp_path):
        j = _journal(tmp_path)
        res = build_bundle(BundleMode.REDACTED, journal=j)
        raw = json.dumps(res.payload, ensure_ascii=False)
        assert "hunter2" not in raw and "contraseña" not in raw

    def test_bundle_secret_scan_is_clean(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        assert res.secret_scan == "CLEAN"
        assert scan_structure(res.payload) == []

    def test_bundle_has_no_home_path(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        raw = json.dumps(res.payload, ensure_ascii=False)
        assert "\\Users\\" not in raw and "/home/" not in raw

    def test_session_metadata_mode_uses_hashed_ids_only(self, tmp_path):
        j = _journal(tmp_path)
        res = build_bundle(BundleMode.REDACTED_WITH_SESSION_METADATA, journal=j)
        meta = res.payload.get("session_metadata", {})
        rows = meta.get("sessions", [])
        assert rows and all(len(r["id_hash"]) == 12 for r in rows)
        assert j.active_session.session_id not in json.dumps(res.payload)
        assert all("content" not in r for r in rows)

    def test_redacted_mode_omits_session_metadata(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        assert "session_metadata" not in res.payload

    def test_config_section_is_allowlisted(self):
        # Credential-SHAPED names, not the bare word "token": a token BUDGET
        # (response_max_output_tokens) is a number, not a credential.
        res = build_bundle(BundleMode.REDACTED)
        cfg = res.payload.get("config", {})
        assert cfg
        for key in cfg:
            low = key.lower()
            for shape in ("api_key", "_key", "secret", "password", "passwd",
                          "auth_token", "access_token", "credential"):
                assert shape not in low, key

    def test_config_section_cannot_grow_by_forgetting(self):
        # The allowlist is closed: a new setting is invisible until named.
        import core.diagnostics_bundle as db
        src = db._section_config.__doc__ or ""
        assert "allowlist" in src.lower()
        cfg = build_bundle(BundleMode.REDACTED).payload.get("config", {})
        assert set(cfg).issubset({
            "assistant_name", "whisper_model", "whisper_language", "fast_context",
            "response_profile", "response_max_output_tokens",
            "session_persistence_mode", "session_max_sessions",
            "session_max_turns", "recovery_supervisor_enabled"})

    def test_error_classes_carry_no_message(self):
        res = build_bundle(BundleMode.REDACTED,
                           errors=["ValueError: password=hunter2 rejected"])
        raw = json.dumps(res.payload["error_classes"])
        assert "hunter2" not in raw and "ValueError" in raw

    def test_file_manifest_has_hashes_not_contents(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        for entry in res.payload.get("file_manifest") or []:
            assert set(entry) == {"area", "name", "size", "sha256_16"}

    def test_size_ceiling_refuses(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path),
                           max_bytes=10)
        assert res.state is BundleState.REFUSED_TOO_LARGE
        assert res.bundle_size > 10

    def test_secret_scan_hit_refuses_instead_of_patching(self, tmp_path,
                                                         monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "_section_models",
                            lambda: {"roles": {"FAST": "sk-abcdefghijklmnop1234"}})
        res = build_bundle(BundleMode.REDACTED)
        assert res.state is BundleState.REFUSED_SECRET_SCAN
        assert "secret" in res.leak_categories

    def test_refused_bundle_is_never_written(self, tmp_path, monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "diagnostics_dir", lambda **kw: tmp_path)
        monkeypatch.setattr(db, "_section_models",
                            lambda: {"roles": {"X": "sk-abcdefghijklmnop1234"}})
        res = write_bundle(BundleMode.REDACTED)
        assert res.state is BundleState.REFUSED_SECRET_SCAN
        assert list(tmp_path.iterdir()) == []

    def test_write_is_atomic_and_leaves_no_temp(self, tmp_path, monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "diagnostics_dir", lambda **kw: tmp_path)
        res = write_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        assert res.state is BundleState.WRITTEN
        assert (tmp_path / res.file_name).is_file()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_write_failure_cleans_up(self, tmp_path, monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "diagnostics_dir", lambda **kw: tmp_path)

        original = Path.replace

        def _boom(self, target):
            raise OSError("disk full")
        monkeypatch.setattr(Path, "replace", _boom)
        res = write_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        monkeypatch.setattr(Path, "replace", original)
        assert res.state is BundleState.WRITE_FAILED
        assert list(tmp_path.glob("*.tmp")) == []

    def test_broken_subsystem_does_not_break_the_bundle(self, monkeypatch):
        import core.diagnostics_bundle as db
        monkeypatch.setattr(db, "_section_lifecycle",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        res = build_bundle(BundleMode.REDACTED)
        assert res.payload["lifecycle"] == {"error": "RuntimeError"}
        assert res.state is BundleState.PREVIEW

    def test_deployment_plan_is_embedded_redacted(self):
        from core.deployment_planner import (
            DeploymentTarget, EnvironmentFacts, build_plan,
        )
        facts = EnvironmentFacts(python_executable="C:\\Users\\aleja\\p.exe",
                                 repo_exists=True, entrypoint_exists=True)
        plan = build_plan(DeploymentTarget.STARTUP_APPLICATION, facts)
        res = build_bundle(BundleMode.REDACTED, deployment_plan=plan)
        assert "aleja" not in json.dumps(res.payload)

    @pytest.mark.parametrize("language", ["es", "en"])
    def test_panel_is_cp1252_safe_and_states_the_guarantee(self, language):
        res = build_bundle(BundleMode.PREVIEW)
        panel = render_diagnostics_panel(res, language=language)
        panel.encode("cp1252")
        assert "secret" in panel.lower()

    def test_result_snapshot_is_content_free(self, tmp_path):
        res = build_bundle(BundleMode.REDACTED, journal=_journal(tmp_path))
        assert "hunter2" not in json.dumps(res.snapshot(), ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
#  Backup
# ══════════════════════════════════════════════════════════════════════════════
class TestBackup:
    def test_eligible_artifacts_are_the_managed_ones(self, tmp_path):
        root = _fake_root(tmp_path)
        labels = {label for label, _ in eligible_artifacts(root)}
        assert labels == {"session_journal", "operational_store", "alias_registry"}

    def test_semantic_collections_are_never_eligible(self, tmp_path):
        root = _fake_root(tmp_path)
        (root / "data" / "chroma").mkdir(parents=True, exist_ok=True)
        (root / "data" / "chroma" / "chroma.sqlite3").write_bytes(b"X")
        paths = [str(p) for _, p in eligible_artifacts(root)]
        assert not any("chroma" in p for p in paths)

    def test_backup_is_created_and_verified(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest)
        assert res.state is BackupState.OK
        assert res.integrity_verified is True
        assert set(res.members) == {"session_journal", "operational_store",
                                    "alias_registry"}
        assert (dest / res.file_name).is_file()

    def test_backup_manifest_records_schema_and_commit(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest)
        with zipfile.ZipFile(dest / res.file_name) as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME))
        assert manifest["schema_version"] == BACKUP_SCHEMA_VERSION
        assert "created_at" in manifest and "git_commit" in manifest
        assert all("sha256" in m for m in manifest["members"])

    def test_backup_leaves_no_temp_file(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        create_backup(root=root, destination=dest)
        assert list(dest.glob("*.tmp")) == []

    def test_empty_tree_reports_empty_not_success(self, tmp_path):
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=tmp_path / "nothing", destination=dest)
        assert res.state is BackupState.EMPTY and res.file_name == ""

    def test_backup_size_bound_fails_cleanly(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest, max_bytes=1)
        assert res.state is BackupState.FAILED
        assert list(dest.glob("*.zip")) == []
        assert list(dest.glob("*.tmp")) == []

    def test_unsafe_name_is_refused(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest, name="../escape")
        assert res.state is BackupState.REFUSED

    def test_corrupt_backup_fails_verification(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest)
        target = dest / res.file_name
        target.write_bytes(b"not a zip at all")
        assert verify_backup(target)["valid"] is False

    def test_tampered_member_fails_hash_check(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest)
        src = dest / res.file_name
        rebuilt = dest / "tampered.zip"
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(rebuilt, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith("operational_store/"):
                    data = b"TAMPERED"
                zout.writestr(item, data)
        info = verify_backup(rebuilt)
        assert info["valid"] is False and "hash_mismatch" in info["error"]

    def test_list_backups_is_bounded_and_content_free(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        for i in range(3):
            create_backup(root=root, destination=dest, name=f"backup_{i}")
        rows = list_backups(dest)
        assert len(rows) == 3
        assert all(set(r) == {"name", "size", "valid", "members", "created_at"}
                   for r in rows)


# ══════════════════════════════════════════════════════════════════════════════
#  Restore
# ══════════════════════════════════════════════════════════════════════════════
class TestRestore:
    def _backup(self, tmp_path):
        root = _fake_root(tmp_path / "app")
        dest = tmp_path / "backups"
        dest.mkdir()
        res = create_backup(root=root, destination=dest)
        return root, dest / res.file_name, dest

    def test_plan_is_dry_run_and_changes_nothing(self, tmp_path):
        root, archive, _ = self._backup(tmp_path)
        before = (root / "data" / "operational_state.db").read_bytes()
        plan = plan_restore(archive, root=root)
        assert plan.state is RestoreState.PLANNED and plan.dry_run is True
        assert plan.compatible is True and plan.integrity_verified is True
        assert set(plan.would_replace) == {"session_journal", "operational_store",
                                           "alias_registry"}
        assert (root / "data" / "operational_state.db").read_bytes() == before

    def test_plan_reports_zero_jobs(self, tmp_path):
        root, archive, _ = self._backup(tmp_path)
        assert plan_restore(archive, root=root).jobs_launched == 0

    def test_corrupt_archive_is_incompatible(self, tmp_path):
        root, archive, _ = self._backup(tmp_path)
        archive.write_bytes(b"junk")
        plan = plan_restore(archive, root=root)
        assert plan.state is RestoreState.INCOMPATIBLE
        assert plan.compatible is False

    def test_schema_mismatch_is_incompatible(self, tmp_path):
        root, archive, dest = self._backup(tmp_path)
        rebuilt = dest / "future.zip"
        with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(rebuilt, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == MANIFEST_NAME:
                    manifest = json.loads(data)
                    manifest["schema_version"] = 99
                    data = json.dumps(manifest).encode("utf-8")
                zout.writestr(item, data)
        plan = plan_restore(rebuilt, root=root)
        assert plan.state is RestoreState.INCOMPATIBLE
        assert any("schema" in i for i in plan.incompatibilities)

    def test_apply_without_approval_is_refused(self, tmp_path):
        root, archive, _ = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        out = apply_restore(archive, plan, "", root=root)
        assert out.state is RestoreState.REFUSED
        assert out.error == "approval_required"

    def test_apply_on_an_incompatible_plan_is_refused(self, tmp_path):
        root, archive, _ = self._backup(tmp_path)
        archive_bad = archive.with_name("bad.zip")
        archive_bad.write_bytes(b"junk")
        plan = plan_restore(archive_bad, root=root)
        out = apply_restore(archive_bad, plan, "OPERATOR_APPROVED", root=root)
        assert out.state is RestoreState.REFUSED

    def test_apply_replaces_state_and_keeps_a_rollback(self, tmp_path):
        root, archive, dest = self._backup(tmp_path)
        (root / "data" / "operational_state.db").write_bytes(b"MODIFIED-LATER")
        plan = plan_restore(archive, root=root)
        out = apply_restore(archive, plan, "OPERATOR_APPROVED", root=root,
                            destination=dest)
        assert out.state is RestoreState.APPLIED and out.dry_run is False
        assert (root / "data" / "operational_state.db").read_bytes() == b"SQLITE-B"
        assert len(list(dest.glob("*.zip"))) >= 2      # original + rollback

    def test_apply_launches_no_job(self, tmp_path):
        root, archive, dest = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        out = apply_restore(archive, plan, "OPERATOR_APPROVED", root=root,
                            destination=dest)
        assert out.jobs_launched == 0

    def test_apply_leaves_no_temp_file(self, tmp_path):
        root, archive, dest = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        apply_restore(archive, plan, "OPERATOR_APPROVED", root=root,
                      destination=dest)
        assert list((root / "data").glob("*.restore.tmp")) == []
        assert list((root / "data" / "sessions").glob("*.restore.tmp")) == []

    def test_interrupted_restore_reports_rolled_back_and_cleans_up(
            self, tmp_path, monkeypatch):
        root, archive, dest = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        real_shutil = mb.shutil
        calls = {"n": 0}

        class _FailingShutil:
            """Shim bound to the module under test only. Patching the real shutil
            module attribute would also break zipfile's own writer, which is how
            the rollback backup would fail instead of the extraction."""

            @staticmethod
            def copyfileobj(src, dst, length=None):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("power loss")
                return real_shutil.copyfileobj(src, dst, length=length)

        monkeypatch.setattr(mb, "shutil", _FailingShutil)
        out = apply_restore(archive, plan, "OPERATOR_APPROVED", root=root,
                            destination=dest)
        assert out.state is RestoreState.ROLLED_BACK
        assert out.rollback_available is True
        assert list((root / "data").glob("*.restore.tmp")) == []
        assert calls["n"] >= 2

    def test_failed_rollback_backup_refuses_the_restore(self, tmp_path,
                                                        monkeypatch):
        # If the CURRENT state cannot be backed up, replacing it would be
        # irreversible — so the restore is refused before anything is touched.
        root, archive, dest = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        before = (root / "data" / "operational_state.db").read_bytes()
        monkeypatch.setattr(mb, "create_backup",
                            lambda **kw: mb.BackupResult(state=BackupState.FAILED))
        out = apply_restore(archive, plan, "OPERATOR_APPROVED", root=root,
                            destination=dest)
        assert out.state is RestoreState.REFUSED
        assert out.error == "rollback_backup_failed"
        assert (root / "data" / "operational_state.db").read_bytes() == before

    def test_no_token_or_authorization_is_restored(self, tmp_path):
        root, archive, dest = self._backup(tmp_path)
        plan = plan_restore(archive, root=root)
        raw = json.dumps(plan.snapshot())
        for token in ("otp", "hitl", "approval_token", "credential"):
            assert token not in raw.lower()

    @pytest.mark.parametrize("language", ["es", "en"])
    def test_backup_panel_is_cp1252_safe(self, tmp_path, language):
        root, archive, dest = self._backup(tmp_path)
        backup = create_backup(root=root, destination=dest, name="panel_test")
        plan = plan_restore(archive, root=root)
        panel = render_backup_panel(backup, plan, language=language)
        panel.encode("cp1252")
        assert "jobs_launched=0" in panel

    def test_managed_state_paths_have_no_home(self, tmp_path):
        out = mb.managed_state_paths()
        assert "\\Users\\" not in json.dumps(out)
        assert "session_journal" in out["eligible"]
