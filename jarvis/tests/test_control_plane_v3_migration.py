"""V69 M63 S4A — Control Plane V2 -> V3 migration.

The load-bearing test here is
:func:`test_round_trip_is_byte_identical_for_every_v2_snapshot`. V3 is a
REPRESENTATION change, so the only thing that makes it safe is that it means
exactly what V2 meant — byte for byte, over every generation in the chain, not
just the one that happened to be migrated.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.migrate_m62_control_plane_v3 as M
import scripts.verify_m62_control_plane as V

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = REPO / V.SNAPSHOT_DIR
RECORD_DIR = REPO / V.RECORD_DIR


def _v2_snapshots() -> list[Path]:
    out = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") == M.CONTROL_PLANE_V2_SCHEMA:
            out.append(path)
    return out


def _current_snapshot() -> dict:
    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    return json.loads(
        (REPO / current["latest_snapshot_path"]).read_text(encoding="utf-8"))


# ── the equivalence proof ────────────────────────────────────────────────────
@pytest.mark.parametrize("path", _v2_snapshots(), ids=lambda p: p.name[:9])
def test_round_trip_is_byte_identical_for_every_v2_snapshot(path):
    """V2 -> V3 -> V2 returns the ORIGINAL BYTES, for every sealed generation."""
    v2 = json.loads(path.read_text(encoding="utf-8"))
    ok, detail = M.prove_equivalence(v2)
    assert ok, f"{path.name}: {detail}"


def test_round_trip_preserves_every_defect_limitation_and_invariant():
    """No entry is dropped, reordered away or truncated by the migration."""
    for path in _v2_snapshots():
        v2 = json.loads(path.read_text(encoding="utf-8"))
        v3, records = M.to_v3(v2)
        restored = M.rehydrate(v3, records)
        for block in ("defects", "limitations", "frozen_invariants",
                      "datasets", "candidates"):
            assert restored[block] == v2[block], f"{path.name}: {block} changed"


def test_the_partition_covers_the_whole_document():
    """A key in neither list would be a key the migration silently DROPS."""
    for path in _v2_snapshots():
        M.check_partition(json.loads(path.read_text(encoding="utf-8")))


def test_record_blocks_and_inline_blocks_do_not_overlap():
    assert not (set(M.RECORD_BLOCKS) & set(M.INLINE_BLOCKS))


def test_the_verifier_and_the_migration_agree_on_the_record_blocks():
    """Two lists describing one contract is how they drift."""
    assert tuple(V.V3_RECORD_BLOCKS) == tuple(M.RECORD_BLOCKS)


# ── non-vacuity: the migration must actually be doing something ──────────────
def test_the_migration_actually_shrinks_the_snapshot():
    v2 = json.loads(_v2_snapshots()[-1].read_text(encoding="utf-8"))
    v3, _ = M.to_v3(v2)
    v2_size = len(V.canonical_bytes(v2))
    v3_size = len(V.canonical_bytes(v3))
    assert v3_size < v2_size / 2, "a migration that saves nothing is not worth its risk"


def test_v2_could_not_have_held_the_next_state():
    """The precondition for migrating at all, re-measured rather than asserted."""
    v2 = json.loads(_v2_snapshots()[-1].read_text(encoding="utf-8"))
    projected = json.loads(json.dumps(v2))
    projected["candidates"].append({
        "adapter_manifest_hash": None, "adapter_sha256": None,
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "candidate_id": "qwen3-06b-lora-quality-live-005",
        "evaluation_corpus": None, "evaluation_receipt": None,
        "evidence": "jarvis/docs/V69_M63_S4B_CANDIDATE005_SINGLE_AXIS_DESIGN.md",
        "ordinal": 5, "status": "DESIGNED_UNTRAINED",
        "training_corpus": "m62-defensive-quality-train v2", "training_receipt": None})
    headroom = V.SNAPSHOT_MAX_BYTES - len(V.canonical_bytes(projected))
    assert headroom < M.REQUIRED_HEADROOM_BYTES, (
        "V2 can still hold the next state; the migration would not be justified")


def test_the_budget_was_not_raised():
    """V3 earns headroom by architecture. Raising the line was forbidden."""
    assert V.SNAPSHOT_MAX_BYTES == 34_816


# ── the live V3 generation ───────────────────────────────────────────────────
def test_the_live_generation_is_v3_and_rehydrates():
    stored = _current_snapshot()
    assert stored["schema_version"] == V.CONTROL_PLANE_V3_SCHEMA_VERSION
    records = V.load_record_store(RECORD_DIR)
    payload, problems = V.rehydrate_v3(stored, records)
    assert not problems, problems
    assert payload["schema_version"] == V.CONTROL_PLANE_SCHEMA_VERSION
    assert set(payload) >= set(V.V3_RECORD_BLOCKS)


def test_every_record_file_is_its_own_content_address():
    for path in sorted(RECORD_DIR.glob("*.json")):
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        assert raw == V.canonical_bytes(payload), f"{path.name} is not canonical"
        assert V.sha256_bytes(raw) == path.stem, f"{path.name} is not its own digest"


def test_the_live_generation_has_room_to_grow():
    stored_bytes = (REPO / json.loads(
        (REPO / V.CURRENT_PATH).read_text(encoding="utf-8")
    )["latest_snapshot_path"]).read_bytes()
    headroom = V.SNAPSHOT_MAX_BYTES - len(stored_bytes)
    assert headroom > 20_000, "V3 must leave real room, not a new cliff"


# ── mutation: the store must be tamper-evident ───────────────────────────────
def test_a_tampered_record_stops_resolving(tmp_path):
    stored = _current_snapshot()
    records = V.load_record_store(RECORD_DIR)
    digest = next(iter(stored["records"].values()))
    tampered = dict(records)
    victim = json.loads(json.dumps(tampered[digest]))
    victim["value"] = "TAMPERED"
    tampered[digest] = victim          # same key, different content
    _payload, problems = V.rehydrate_v3(stored, tampered)
    assert problems, "an edited record must not silently change what a generation means"


def test_a_missing_record_is_a_refusal_not_a_gap():
    stored = _current_snapshot()
    records = V.load_record_store(RECORD_DIR)
    digest = next(iter(stored["records"].values()))
    without = {k: v for k, v in records.items() if k != digest}
    _payload, problems = V.rehydrate_v3(stored, without)
    assert any("missing" in p for p in problems)


def test_a_record_referenced_under_the_wrong_block_is_refused():
    stored = json.loads(json.dumps(_current_snapshot()))
    records = V.load_record_store(RECORD_DIR)
    stored["records"]["defects"] = stored["records"]["limitations"]
    _payload, problems = V.rehydrate_v3(stored, records)
    assert any("but was referenced as" in p for p in problems)


def test_a_records_map_missing_a_block_is_refused():
    stored = json.loads(json.dumps(_current_snapshot()))
    del stored["records"]["defects"]
    _payload, problems = V.rehydrate_v3(stored, V.load_record_store(RECORD_DIR))
    assert problems


def test_the_block_name_is_inside_the_hashed_bytes():
    """Two blocks that serialise identically must still address differently."""
    assert M.record_digest("defects", []) != M.record_digest("limitations", [])


# ── history is untouched ─────────────────────────────────────────────────────
def test_the_v2_history_is_still_v2_and_unrewritten():
    """Generations 1-15 keep their bytes. A migration that rewrites sealed
    history is not a migration, it is a revision."""
    v2 = _v2_snapshots()
    assert len(v2) == 15, f"expected 15 sealed V2 generations, found {len(v2)}"
    for path in v2:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == M.CONTROL_PLANE_V2_SCHEMA
        assert path.read_bytes() == V.canonical_bytes(payload)


def test_the_chain_crosses_the_version_boundary():
    """Generation 16's parent digest is the sha256 of generation 15's bytes."""
    gen15 = SNAPSHOT_DIR / "0015-m62-s3z-candidate004-hold-decision.json"
    stored = _current_snapshot()
    assert stored["state_generation"] == 16
    assert stored["parent_snapshot_sha256"] == V.sha256_bytes(gen15.read_bytes())


# ── the migration changes representation, never science ──────────────────────
def test_the_migration_moved_no_scientific_state():
    stored = _current_snapshot()
    records = V.load_record_store(RECORD_DIR)
    now, _ = V.rehydrate_v3(stored, records)
    before = json.loads(
        (SNAPSHOT_DIR / "0015-m62-s3z-candidate004-hold-decision.json")
        .read_text(encoding="utf-8"))
    for block in ("candidates", "datasets", "defects", "limitations",
                  "frozen_invariants", "base_model", "policy_identities",
                  "archive", "authority_observation"):
        assert now[block] == before[block], f"{block} moved during a representation change"


def test_candidate_004_is_still_on_hold_and_unpromoted():
    stored = _current_snapshot()
    now, _ = V.rehydrate_v3(stored, V.load_record_store(RECORD_DIR))
    c004 = [c for c in now["candidates"]
            if c["candidate_id"] == "qwen3-06b-lora-quality-live-004"]
    assert len(c004) == 1
    assert c004[0]["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert "HOLD" in now["control_plane_note"] or "HOLD" in json.dumps(now["next_milestone"])


def test_no_candidate_005_exists_yet():
    stored = _current_snapshot()
    now, _ = V.rehydrate_v3(stored, V.load_record_store(RECORD_DIR))
    assert not any("005" in c["candidate_id"] for c in now["candidates"])


def test_eval_v6_is_spent_and_eval_v5_is_retired_unspent():
    stored = _current_snapshot()
    now, _ = V.rehydrate_v3(stored, V.load_record_store(RECORD_DIR))
    by_version = {d["version"]: d for d in now["datasets"]
                  if d["dataset_id"] == "m62-defensive-eval"}
    assert by_version["v6"]["status"] == "USED_IMMUTABLE"
    assert by_version["v5"]["status"] == "FROZEN_UNUSED"
    assert by_version["v5"]["spent_by"] is None


def test_no_authority_is_observed_in_the_repository():
    stored = _current_snapshot()
    now, _ = V.rehydrate_v3(stored, V.load_record_store(RECORD_DIR))
    observation = now["authority_observation"]
    for key in ("eval", "train", "promotion"):
        assert observation[key] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False
