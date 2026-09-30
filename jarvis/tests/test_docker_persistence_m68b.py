"""V69 M68B (E) — the supported Docker deployment preserves durable state.

THE DEFECT THESE PIN
--------------------
``docker-compose.yml`` declared one application volume, ``jarvis_logs:/app/logs``.
``/app/data`` — which holds the M65C/M65D durable effect journal
(``core.effect_journal.DEFAULT_JOURNAL_PATH`` is ``<app>/data/effect_journal.db``)
— was container-local. ``docker compose up --force-recreate``, the ordinary way to
apply a new image, destroyed it, and the next run replayed effects it had already
performed while every other signal said the deployment was healthy.

``/app/data`` also could not be created: the Dockerfile made and chowned only
``logs``, ``/app`` itself stays root-owned, the container runs as ``jarvis``, and
``core.managed_paths._resolve`` swallows the resulting ``OSError`` by design.

WHAT IS AND IS NOT CLAIMED
--------------------------
These tests assert that state survives CONTAINER RECREATION. Nothing here claims
crash durability — no Compose configuration can, and the assertion below pins the
wording so a later edit cannot quietly upgrade the claim.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from core.deployment_persistence import (  # noqa: E402
    CLASSIFIED_STATE,
    CONTAINER_APP_ROOT,
    StateClass,
    covered_by,
    must_not_persist_paths,
    persistence_report,
    required_persistent_paths,
)

COMPOSE = PACKAGE_ROOT / "docker-compose.yml"
DOCKERFILE = PACKAGE_ROOT / "Dockerfile"


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _jarvis_mount_targets() -> list[str]:
    """The container-side targets of every volume the jarvis service declares."""
    svc = _compose()["services"]["jarvis"]
    targets = []
    for entry in svc.get("volumes") or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2:
                targets.append(parts[1])
        elif isinstance(entry, dict) and entry.get("target"):
            targets.append(str(entry["target"]))
    return targets


# ── The classification itself ────────────────────────────────────────────────
class TestClassification:
    def test_every_class_is_used_deliberately(self):
        used = {e.classification for e in CLASSIFIED_STATE}
        assert StateClass.DURABLE_SECURITY_STATE in used
        assert StateClass.AUDIT_EVIDENCE in used
        assert StateClass.EPHEMERAL in used
        assert StateClass.CACHE in used
        assert StateClass.OPERATOR_CONFIGURED in used

    def test_every_entry_says_why(self):
        for entry in CLASSIFIED_STATE:
            assert entry.why.strip(), f"{entry.container_path} is unexplained"
            assert entry.container_path.startswith(("/app", "/tmp", "/home"))

    def test_the_effect_journal_directory_is_durable_security_state(self):
        """The load-bearing classification: M65D's retry authority reads it."""
        from core.effect_journal import DEFAULT_JOURNAL_PATH
        from core.managed_paths import app_root
        relative = DEFAULT_JOURNAL_PATH.parent.relative_to(app_root())
        container = f"{CONTAINER_APP_ROOT}/{relative.as_posix()}"
        assert container == "/app/data"
        entry = next(e for e in CLASSIFIED_STATE if e.container_path == container)
        assert entry.classification is StateClass.DURABLE_SECURITY_STATE
        assert entry.must_persist is True

    def test_every_managed_directory_is_classified(self):
        """A managed directory that nobody classified is an unanswered question,
        not an implicit EPHEMERAL."""
        from core import managed_paths
        from core.managed_paths import app_root
        classified = {e.container_path for e in CLASSIFIED_STATE}
        for name in ("data_dir", "sessions_dir", "diagnostics_dir", "backups_dir",
                     "exports_dir", "logs_dir"):
            local = getattr(managed_paths, name)(create=False)
            container = f"{CONTAINER_APP_ROOT}/{local.relative_to(app_root()).as_posix()}"
            assert covered_by(container, sorted(classified)), (
                f"{name}() -> {container} is not classified")

    def test_sandbox_workspaces_are_explicitly_harmful_to_persist(self):
        harmful = must_not_persist_paths()
        assert any("jarvis_codeexec_" in p for p in harmful)
        assert any("jarvis_sbx_" in p for p in harmful)


# ── The deployment must satisfy the classification ──────────────────────────
class TestComposeSatisfiesTheClassification:
    def test_every_required_path_is_covered_by_a_volume(self):
        report = persistence_report(_jarvis_mount_targets())
        assert report["missing"] == [], (
            f"durable state with no volume: {report['missing']}")
        assert report["satisfied"] is True

    def test_app_data_specifically_has_a_named_volume(self):
        targets = _jarvis_mount_targets()
        assert "/app/data" in targets, (
            "the effect journal directory is not persisted; container recreation "
            "loses every recorded effect")

    def test_the_named_volumes_are_declared(self):
        compose = _compose()
        declared = set((compose.get("volumes") or {}).keys())
        svc = compose["services"]["jarvis"]
        for entry in svc.get("volumes") or []:
            source = str(entry).split(":")[0]
            if source.startswith((".", "/")):
                continue          # a bind mount needs no top-level declaration
            assert source in declared, f"volume {source!r} is used but not declared"

    def test_no_sandbox_workspace_is_persisted(self):
        report = persistence_report(_jarvis_mount_targets())
        assert report["wrongly_persisted"] == []
        # Non-vacuity: an EMPTY harmful list satisfies the line above trivially,
        # which is how the falsification campaign emptied it and survived.
        assert must_not_persist_paths(), (
            "nothing is declared harmful to persist — the check above is vacuous")
        probe = persistence_report(list(_jarvis_mount_targets())
                                   + [must_not_persist_paths()[0]])
        assert probe["wrongly_persisted"], (
            "mounting a declared-harmful path is not detected")

    def test_no_secret_is_mounted_as_a_named_volume(self):
        targets = _jarvis_mount_targets()
        assert not any(t.endswith(".env") for t in targets)
        text = COMPOSE.read_text(encoding="utf-8")
        assert re.search(r"(?i)(api[_-]?key|password|secret)\s*:\s*['\"]?[A-Za-z0-9]{12,}",
                         text) is None, "a literal credential is committed in compose"


class TestDockerfilePreparesTheDirectories:
    def test_the_image_creates_and_owns_every_required_path(self):
        """A volume mounted onto a directory the runtime user cannot write is a
        volume that silently holds nothing."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        mkdir_lines = [ln for ln in text.splitlines()
                       if "mkdir" in ln and ln.strip().startswith("RUN")]
        assert mkdir_lines, "the image creates no managed directories"
        blob = "\n".join(mkdir_lines)
        for path in required_persistent_paths():
            leaf = path[len(CONTAINER_APP_ROOT) + 1:] or path
            assert leaf in blob, f"the image never creates {path}"
        chown = [ln for ln in text.splitlines() if "chown" in ln]
        assert any("data" in ln for ln in chown), "/app/data is never chowned"
        assert any("logs" in ln for ln in chown), "/app/logs is never chowned"

    def test_the_runtime_user_is_not_root(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^USER\s+jarvis\s*$", text, re.M), "no non-root USER"

    def test_the_image_sets_the_canonical_endpoint_variable(self):
        """Finding D's deployment half: the image configures OLLAMA_HOST, the one
        variable the canonical resolver reads."""
        from core.ollama_endpoint import OLLAMA_HOST_ENV
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(rf"^ENV\s+{OLLAMA_HOST_ENV}=", text, re.M)


class TestTheClaimIsNotOverstated:
    def test_the_guarantee_is_recreation_not_crash_durability(self):
        report = persistence_report(["/app/data", "/app/logs"])
        assert "crash" in report["guarantee"].lower()
        assert "NOT crash durability" in report["guarantee"]

    def test_the_module_documents_what_it_does_not_claim(self):
        source = (PACKAGE_ROOT / "core" / "deployment_persistence.py").read_text(
            encoding="utf-8")
        assert "crash durability" in source
        assert "fsync" in source


class TestCoverageSemantics:
    def test_an_ancestor_mount_covers_a_descendant(self):
        assert covered_by("/app/data/sessions", ["/app/data"]) is True

    def test_a_descendant_mount_does_not_cover_its_ancestor(self):
        assert covered_by("/app/data", ["/app/data/sessions"]) is False

    def test_a_prefix_that_is_not_a_path_boundary_does_not_count(self):
        assert covered_by("/app/database", ["/app/data"]) is False

    def test_required_paths_collapse_to_their_shallowest_ancestor(self):
        required = required_persistent_paths()
        assert "/app/data" in required
        assert "/app/data/sessions" not in required
