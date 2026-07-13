"""tests/test_alias_registry_v69.py — V69 M53: durable alias registry."""
from __future__ import annotations

import json

from core.alias_registry import REGISTRY_SCHEMA_VERSION, AliasEntry, AliasRegistry


def _entry(logical="jarvis_episodic", active="jarvis_episodic__v1__395d63bb",
           previous="", rollback=False) -> AliasEntry:
    return AliasEntry(
        logical_name=logical, active_physical_collection=active,
        provider="ollama", model="nomic-embed-text:latest", dimension=768,
        fingerprint="395d63bbee28d585", embedding_schema_version=1,
        activated_at="2026-07-13T00:00:00Z", migration_id="mig_1",
        previous_physical_collection=previous, rollback_available=rollback,
    )


def test_initial_creation_is_empty(tmp_path):
    reg = AliasRegistry(tmp_path / "alias.json")
    assert reg.all() == {}
    assert reg.resolve("jarvis_episodic") is None


def test_set_active_persists_atomically(tmp_path):
    path = tmp_path / "alias.json"
    reg = AliasRegistry(path)
    reg.set_active(_entry())
    assert path.exists()
    # A fresh registry reads the same active physical collection back.
    reg2 = AliasRegistry(path)
    assert reg2.resolve("jarvis_episodic") == "jarvis_episodic__v1__395d63bb"
    assert reg2.get("jarvis_episodic").dimension == 768


def test_activation_retains_previous_and_enables_rollback(tmp_path):
    path = tmp_path / "alias.json"
    reg = AliasRegistry(path)
    reg.set_active(_entry(active="c__v1__aaaa"))
    reg.set_active(_entry(active="c__v1__bbbb", previous="c__v1__aaaa", rollback=True,
                          logical="jarvis_episodic"))
    restored = reg.rollback("jarvis_episodic", activated_at="2026-07-13T01:00:00Z")
    assert restored is not None
    assert restored.active_physical_collection == "c__v1__aaaa"
    # The newer collection is retained as the new "previous" (re-rollback possible),
    # never deleted here.
    assert restored.previous_physical_collection == "c__v1__bbbb"
    assert restored.rollback_available


def test_rollback_without_previous_is_noop(tmp_path):
    reg = AliasRegistry(tmp_path / "alias.json")
    reg.set_active(_entry(rollback=False))
    assert reg.rollback("jarvis_episodic", activated_at="t") is None


def test_backup_written_before_mutation(tmp_path):
    path = tmp_path / "alias.json"
    reg = AliasRegistry(path)
    reg.set_active(_entry(active="c__v1__aaaa"))
    reg.set_active(_entry(active="c__v1__bbbb"))
    bak = path.with_suffix(path.suffix + ".bak")
    assert bak.exists()
    # .bak holds the prior state (first active), primary holds the latest.
    assert "c__v1__aaaa" in bak.read_text(encoding="utf-8")
    assert AliasRegistry(path).resolve("jarvis_episodic") == "c__v1__bbbb"


def test_malformed_registry_is_quarantined_and_recovered_from_bak(tmp_path):
    path = tmp_path / "alias.json"
    reg = AliasRegistry(path)
    reg.set_active(_entry(active="c__v1__good"))
    reg.set_active(_entry(active="c__v1__good2"))   # ensures a .bak exists
    # Corrupt the primary file.
    path.write_text("{ this is not json", encoding="utf-8")
    reg2 = AliasRegistry(path)
    # Recovered from .bak; corrupt primary quarantined.
    assert reg2.resolve("jarvis_episodic") is not None
    assert path.with_suffix(path.suffix + ".corrupt").exists()


def test_malformed_without_backup_starts_empty_not_crash(tmp_path):
    path = tmp_path / "alias.json"
    path.write_text("garbage{", encoding="utf-8")
    reg = AliasRegistry(path)   # must not raise
    assert reg.all() == {}
    assert path.with_suffix(path.suffix + ".corrupt").exists()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "alias.json"
    path.write_text(json.dumps({
        "registry_schema_version": REGISTRY_SCHEMA_VERSION + 5,
        "aliases": {"jarvis_episodic": _entry().to_dict()},
    }), encoding="utf-8")
    reg = AliasRegistry(path)   # fail-closed: do not load a future schema
    assert reg.all() == {}


def test_no_secret_fields_present(tmp_path):
    path = tmp_path / "alias.json"
    reg = AliasRegistry(path)
    reg.set_active(_entry())
    blob = path.read_text(encoding="utf-8").lower()
    for token in ("password", "token", "api_key", "secret", "private key"):
        assert token not in blob
