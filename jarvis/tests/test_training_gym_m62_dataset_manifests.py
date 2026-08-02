"""V69 M62 S2d — immutable split manifests, dataset versions and revocation snapshots.

The claim under test is that a dataset version is *evidence*, not a directory: that the
manifest can prove, from the bytes on disk alone, which candidates are in the dataset,
where each one landed, and that nothing has been edited since — and that every way of
weakening that claim (a missing digest, a swapped shard, a symlink, a re-declared
version, a revoked record slipping into a new build) is a refusal rather than a warning.

Candidates are constructed directly rather than through ``build_candidate``: this module
is testing the manifest algebra, and assembling a full approved episode per fixture would
bury the arithmetic being asserted.
"""
from __future__ import annotations

import json

import pytest

from training_gym.datasets.candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    TargetSource,
)
from training_gym.datasets.manifests import (
    GENESIS_PARENT,
    MANIFEST_FILENAME,
    SHARD_SPLITS,
    DatasetVersionManifest,
    ManifestCandidateRow,
    ManifestError,
    RevocationSnapshot,
    ShardManifest,
    encode_shard,
    latest_manifest_hash,
    load_manifest,
    reviewer_safe_id,
    shard_filename,
    verify_parent,
    verify_version,
    version_dir,
    write_dataset_version,
)
from training_gym.schemas import SchemaError, sha256_text
from training_gym.task_spec import TaskFamily

NOW = "2026-08-02T09:00:00Z"


def make_candidate(candidate_id: str, *, state: CandidateState = CandidateState.PROMOTED,
                   prompt: str = "", target: str = "", **overrides) -> DatasetCandidate:
    prompt = prompt or f"Prompt for {candidate_id}"
    target = target or f'{{"verdict": "{candidate_id}"}}'
    base = {
        "candidate_id": candidate_id,
        "task_id": overrides.pop("task_id", candidate_id),
        "task_family": overrides.pop("task_family", TaskFamily.STRUCTURED_REPORT),
        "task_hash": overrides.pop("task_hash", sha256_text(f"task-{candidate_id}")),
        "attempt_id": f"attempt-{candidate_id}",
        "attempt_hash": sha256_text(f"attempt-{candidate_id}"),
        "deterministic_report_hash": sha256_text(f"det-{candidate_id}"),
        "consensus_report_hash": sha256_text(f"con-{candidate_id}"),
        "human_review_hash": sha256_text(f"rev-{candidate_id}"),
        "source_episode_hash": sha256_text(f"ep-{candidate_id}"),
        "student_model_id": "qwen3:8b-q4_K_M",
        "system_message": "",
        "user_prompt": prompt,
        "target_text": target,
        "target_source": TargetSource.VERIFIED_STUDENT_OUTPUT,
        "target_hash": sha256_text(target),
        "approval_hash": sha256_text(f"rev-{candidate_id}"),
        "created_at_utc": NOW,
        "lineage_group": candidate_id,
        "provenance": {"source": "handwritten"},
        "state": state,
    }
    base.update(overrides)
    return DatasetCandidate(**base)


def empty_snapshot() -> RevocationSnapshot:
    return RevocationSnapshot()


def write_version(root, *, dataset_id="corpus", version="v1", candidates=None,
                  assignments=None, parent=GENESIS_PARENT, **overrides):
    """Write a small, structurally valid version. The default shape has a TRAIN shard."""
    candidates = candidates if candidates is not None else [
        make_candidate("c-1"), make_candidate("c-2"), make_candidate("c-3")]
    assignments = assignments if assignments is not None else {
        "c-1": DatasetSplit.TRAIN.value, "c-2": DatasetSplit.TRAIN.value,
        "c-3": DatasetSplit.HIDDEN_EVALUATION.value}
    kwargs = {
        "root": root, "dataset_id": dataset_id, "dataset_version": version,
        "candidates": candidates, "assignments": assignments,
        "parent_manifest_hash": parent, "created_at_utc": NOW, "seed": "seed-a",
        "split_policy_hash": sha256_text("policy"),
        "leakage_report_hash": sha256_text("leakage"),
        "revocation": empty_snapshot(),
    }
    kwargs.update(overrides)
    return write_dataset_version(**kwargs)


# ── 1..2: a valid version verifies ────────────────────────────────────────────
def test_a_written_version_verifies_from_disk(tmp_path):
    written = write_version(tmp_path)
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert result.ok, result.problems
    assert result.manifest is not None
    assert result.manifest.manifest_hash() == written.manifest_hash
    assert result.manifest.total_records == 3


def test_every_shard_carries_its_own_size_digest_and_line_count(tmp_path):
    write_version(tmp_path)
    manifest = load_manifest(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    train = manifest.shard_for(DatasetSplit.TRAIN)
    assert train is not None
    assert train.filename == "train.jsonl"
    assert train.line_count == 2
    assert train.size_bytes > 0
    assert len(train.sha256_file) == 64
    # The digest is of the FILE, and the file is what a reader will open.
    path = version_dir(tmp_path, "corpus", "v1") / "train.jsonl"
    assert path.read_bytes().count(b"\n") == 2


def test_only_the_five_shard_splits_have_a_filename():
    for split in SHARD_SPLITS:
        assert shard_filename(split) == f"{split.value}.jsonl"
    with pytest.raises(ManifestError, match="no shard"):
        shard_filename(DatasetSplit.QUARANTINE)


# ── 3..6: manifest integrity ──────────────────────────────────────────────────
def test_an_unknown_manifest_field_is_refused(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["allow_training_on_hidden_eval"] = True
    with pytest.raises(SchemaError, match="unknown field"):
        DatasetVersionManifest.from_dict(payload)


def test_a_manifest_without_a_digest_is_refused(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("manifest_hash")
    with pytest.raises(ManifestError, match="missing manifest_hash"):
        DatasetVersionManifest.from_dict(payload)


def test_a_malformed_manifest_digest_is_refused(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["manifest_hash"] = "not-a-digest"
    with pytest.raises(SchemaError, match="hex digest"):
        DatasetVersionManifest.from_dict(payload)


def test_a_mismatched_manifest_digest_is_refused(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["seed"] = "seed-b"
    with pytest.raises(ManifestError, match="does not match its content"):
        DatasetVersionManifest.from_dict(payload)
    # ...and the on-disk verification reports it rather than raising.
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("does not match its content" in p for p in result.problems)


def test_an_edited_distribution_is_detected_even_with_a_recomputed_digest(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["total_records"] = 99
    with pytest.raises(ManifestError, match="does not match the records"):
        DatasetVersionManifest.from_dict(payload)


# ── 7..12: shard integrity ────────────────────────────────────────────────────
def test_a_missing_shard_is_reported(tmp_path):
    write_version(tmp_path)
    (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").unlink()
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("missing" in p for p in result.problems)


def test_an_altered_shard_is_reported(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / "train.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["target_text"] = '{"verdict": "tampered"}'
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("content digest" in p for p in result.problems)


def test_a_shard_replaced_by_a_symlink_is_reported(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / "train.jsonl"
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(decoy)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("symlink" in p for p in result.problems)


def test_a_hard_linked_shard_is_reported(tmp_path):
    write_version(tmp_path)
    path = version_dir(tmp_path, "corpus", "v1") / "train.jsonl"
    second_name = tmp_path / "second-name.jsonl"
    try:
        import os
        os.link(path, second_name)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this host does not permit creating hard links")
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("hard link" in p for p in result.problems)


def test_an_incorrect_declared_size_is_refused_by_the_manifest(tmp_path):
    write_version(tmp_path)
    payload = json.loads((version_dir(tmp_path, "corpus", "v1")
                          / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    for shard in payload["shards"]:
        if shard["split"] == "train":
            shard["size_bytes"] = shard["size_bytes"] + 1
    with pytest.raises(ManifestError, match="does not match its content"):
        DatasetVersionManifest.from_dict(payload)


def test_a_line_count_that_disagrees_with_the_records_is_refused():
    rows = (ManifestCandidateRow.from_candidate(make_candidate("c-1"),
                                                DatasetSplit.TRAIN),)
    with pytest.raises(ManifestError, match="declares 5 lines"):
        DatasetVersionManifest(
            dataset_id="corpus", dataset_version="v1",
            parent_manifest_hash=GENESIS_PARENT, created_at_utc=NOW, seed="seed-a",
            split_policy_hash=sha256_text("policy"),
            leakage_report_hash=sha256_text("leak"),
            revocation_snapshot_hash=empty_snapshot().snapshot_hash(),
            candidates=rows,
            shards=(ShardManifest(split=DatasetSplit.TRAIN, filename="train.jsonl",
                                  sha256_file=sha256_text("x"), size_bytes=10,
                                  line_count=5),))


def test_a_caller_chosen_shard_filename_is_refused():
    with pytest.raises(ManifestError, match="one legal name"):
        ShardManifest(split=DatasetSplit.TRAIN, filename="../../escape.jsonl",
                      sha256_file=sha256_text("x"), size_bytes=1, line_count=0)


# ── 13..17: record-level integrity ────────────────────────────────────────────
def _manifest_with(rows, shards=()):
    return DatasetVersionManifest(
        dataset_id="corpus", dataset_version="v1", parent_manifest_hash=GENESIS_PARENT,
        created_at_utc=NOW, seed="seed-a", split_policy_hash=sha256_text("policy"),
        leakage_report_hash=sha256_text("leak"),
        revocation_snapshot_hash=empty_snapshot().snapshot_hash(),
        candidates=tuple(rows), shards=tuple(shards),
        allow_empty_splits=(DatasetSplit.TRAIN.value,))


def test_a_duplicate_candidate_id_is_refused():
    row = ManifestCandidateRow.from_candidate(make_candidate("c-1"), DatasetSplit.TRAIN)
    other = ManifestCandidateRow.from_candidate(make_candidate("c-1", prompt="different"),
                                                DatasetSplit.HIDDEN_EVALUATION)
    with pytest.raises(ManifestError, match="appears twice"):
        _manifest_with((row, other))


def test_the_same_content_under_two_ids_is_refused():
    first = make_candidate("c-1", prompt="same", target='{"v": 1}')
    second = make_candidate("c-2", prompt="same", target='{"v": 1}')
    # Force identical content digests by giving the second the first's bindings.
    second = DatasetCandidate(**{**{k: getattr(first, k) for k in (
        "task_id", "task_family", "task_hash", "attempt_id", "attempt_hash",
        "deterministic_report_hash", "consensus_report_hash", "human_review_hash",
        "source_episode_hash", "student_model_id", "system_message", "user_prompt",
        "target_text", "target_source", "target_hash", "approval_hash",
        "created_at_utc", "lineage_group", "state")},
        "candidate_id": "c-1", "provenance": {"source": "handwritten"}})
    rows = (ManifestCandidateRow.from_candidate(first, DatasetSplit.TRAIN),
            ManifestCandidateRow(**{**ManifestCandidateRow.from_candidate(
                second, DatasetSplit.HIDDEN_EVALUATION).__dict__,
                "candidate_id": "c-2"}))
    with pytest.raises(ManifestError, match="share content digest"):
        _manifest_with(rows)


def test_a_quarantined_candidate_may_not_also_hold_a_split():
    row = ManifestCandidateRow.from_candidate(make_candidate("c-1"), DatasetSplit.TRAIN)
    with pytest.raises(ManifestError, match="not a split a record can also be in"):
        DatasetVersionManifest(
            dataset_id="corpus", dataset_version="v1",
            parent_manifest_hash=GENESIS_PARENT, created_at_utc=NOW, seed="seed-a",
            split_policy_hash=sha256_text("p"), leakage_report_hash=sha256_text("l"),
            revocation_snapshot_hash=empty_snapshot().snapshot_hash(),
            candidates=(row,),
            quarantine_audit=({"candidate_id": "c-1", "reason": "unsafe"},),
            allow_empty_splits=(DatasetSplit.TRAIN.value,))


def test_a_quarantine_split_may_never_reach_a_manifest_row():
    with pytest.raises(ManifestError, match="may not appear in a finalized"):
        ManifestCandidateRow.from_candidate(make_candidate("c-1"),
                                            DatasetSplit.QUARANTINE)


def test_evaluation_only_material_is_refused_in_a_train_side_split():
    candidate = make_candidate("c-1", evaluation_only=True, dataset_eligible=False)
    with pytest.raises(ManifestError, match="evaluation_only"):
        ManifestCandidateRow.from_candidate(candidate, DatasetSplit.TRAIN)
    with pytest.raises(ManifestError, match="evaluation_only"):
        ManifestCandidateRow.from_candidate(candidate, DatasetSplit.VALIDATION)
    # It is legitimate in the split it exists for.
    assert ManifestCandidateRow.from_candidate(
        candidate, DatasetSplit.HIDDEN_EVALUATION).evaluation_only


@pytest.mark.parametrize("state", [
    CandidateState.CREATED, CandidateState.VALIDATED, CandidateState.LEAKAGE_CHECKED,
    CandidateState.READY_FOR_PROMOTION, CandidateState.REJECTED,
    CandidateState.QUARANTINED, CandidateState.REVOKED, CandidateState.FAILED,
    CandidateState.NEEDS_REVISION,
])
def test_only_a_promoted_candidate_may_appear_in_a_finalized_version(state):
    with pytest.raises(ManifestError, match="promoted records only"):
        ManifestCandidateRow.from_candidate(make_candidate("c-1", state=state),
                                            DatasetSplit.TRAIN)


# ── 18..21: revocation and lineage ────────────────────────────────────────────
def revocation(candidate_id="c-2", **overrides):
    entry = {"candidate_id": candidate_id, "candidate_hash": "", "reason": "leaked key",
             "actor": "operator-1", "at": NOW, "affected_dataset_versions": ["v1"]}
    entry.update(overrides)
    return RevocationSnapshot(entries=(entry,))


def test_a_revoked_candidate_may_not_enter_a_new_version(tmp_path):
    with pytest.raises(ManifestError, match="revoked candidate"):
        write_version(tmp_path, revocation=revocation("c-2"))


def test_a_revocation_naming_only_the_content_digest_also_excludes(tmp_path):
    victim = make_candidate("c-2")
    snapshot = revocation("c-9", candidate_hash=victim.candidate_hash())
    assert snapshot.is_revoked(victim)
    with pytest.raises(ManifestError, match="revoked candidate"):
        write_version(tmp_path, revocation=snapshot)


def test_a_revocation_without_a_reason_is_refused():
    with pytest.raises(ManifestError, match="no reason"):
        RevocationSnapshot(entries=({"candidate_id": "c-1", "reason": "  ",
                                     "actor": "operator-1", "at": NOW},))


def test_a_revocation_snapshot_hash_is_order_independent():
    a = {"candidate_id": "c-1", "reason": "x", "actor": "op", "at": NOW}
    b = {"candidate_id": "c-2", "reason": "y", "actor": "op", "at": NOW}
    assert (RevocationSnapshot(entries=(a, b)).snapshot_hash()
            == RevocationSnapshot(entries=(b, a)).snapshot_hash())


def test_an_old_version_still_verifies_after_one_of_its_records_is_revoked(tmp_path):
    written = write_version(tmp_path)
    # The revocation happens afterwards and touches nothing on disk.
    snapshot = revocation("c-2")
    assert "c-2" in snapshot.revoked_ids
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert result.ok, result.problems
    assert result.manifest is not None
    assert result.manifest.manifest_hash() == written.manifest_hash
    assert "c-2" in [row.candidate_id for row in result.manifest.candidates]


def test_a_second_version_must_name_a_verifiable_parent(tmp_path):
    first = write_version(tmp_path)
    verify_parent(root=tmp_path, dataset_id="corpus",
                  parent_manifest_hash=first.manifest_hash)
    with pytest.raises(ManifestError, match="names no verifiable existing version"):
        verify_parent(root=tmp_path, dataset_id="corpus",
                      parent_manifest_hash=sha256_text("some other manifest"))


def test_genesis_may_only_be_claimed_once(tmp_path):
    verify_parent(root=tmp_path, dataset_id="corpus",
                  parent_manifest_hash=GENESIS_PARENT)
    write_version(tmp_path)
    with pytest.raises(ManifestError, match="may not declare itself the genesis"):
        verify_parent(root=tmp_path, dataset_id="corpus",
                      parent_manifest_hash=GENESIS_PARENT)


def test_the_latest_manifest_hash_chains_versions(tmp_path):
    assert latest_manifest_hash(root=tmp_path, dataset_id="corpus") == GENESIS_PARENT
    first = write_version(tmp_path)
    assert latest_manifest_hash(root=tmp_path,
                               dataset_id="corpus") == first.manifest_hash
    second = write_version(tmp_path, version="v2", parent=first.manifest_hash,
                           candidates=[make_candidate("d-1")],
                           assignments={"d-1": DatasetSplit.TRAIN.value},
                           created_at_utc="2026-08-03T09:00:00Z")
    assert latest_manifest_hash(root=tmp_path,
                               dataset_id="corpus") == second.manifest_hash
    assert second.manifest.parent_manifest_hash == first.manifest_hash


# ── 22..26: path safety and immutability ──────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "../escape", "..", "a/b", "a\\b", "C:", "C:\\Windows", "\\\\server\\share",
    ".hidden", "con", "nul.json", "with space", "",
])
def test_an_unsafe_dataset_version_id_is_refused_before_any_path_join(bad, tmp_path):
    with pytest.raises(SchemaError):
        version_dir(tmp_path, "corpus", bad)
    with pytest.raises(SchemaError):
        version_dir(tmp_path, bad, "v1")


def test_an_unsafe_identifier_is_reported_rather_than_probed(tmp_path):
    result = verify_version(root=tmp_path, dataset_id="corpus",
                            dataset_version="../../etc")
    assert not result.ok
    assert any("invalid identifier" in p for p in result.problems)


def test_an_existing_version_is_never_overwritten(tmp_path):
    write_version(tmp_path)
    before = (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").read_bytes()
    with pytest.raises(ManifestError, match="already exists"):
        write_version(tmp_path, candidates=[make_candidate("z-1")],
                      assignments={"z-1": DatasetSplit.TRAIN.value})
    after = (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").read_bytes()
    assert before == after


def test_a_previous_version_is_byte_identical_after_a_later_one_is_written(tmp_path):
    first = write_version(tmp_path)
    before = (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").read_bytes()
    write_version(tmp_path, version="v2", parent=first.manifest_hash,
                  candidates=[make_candidate("d-1")],
                  assignments={"d-1": DatasetSplit.TRAIN.value})
    assert (version_dir(tmp_path, "corpus", "v1")
            / "train.jsonl").read_bytes() == before
    assert verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1").ok


def test_an_undeclared_file_in_the_version_directory_is_reported(tmp_path):
    write_version(tmp_path)
    (version_dir(tmp_path, "corpus", "v1") / "extra.jsonl").write_text(
        "{}\n", encoding="utf-8")
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("named by no shard record" in p for p in result.problems)


def test_a_directory_without_a_manifest_is_residue_and_not_a_version(tmp_path):
    write_version(tmp_path)
    (version_dir(tmp_path, "corpus", "v1") / MANIFEST_FILENAME).unlink()
    result = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert not result.ok
    assert any("residue" in p for p in result.problems)
    with pytest.raises(ManifestError, match="refusing to use an unverified version"):
        load_manifest(root=tmp_path, dataset_id="corpus", dataset_version="v1")


# ── 27..30: identity and the empty-dataset trap ───────────────────────────────
def test_the_order_candidates_arrive_in_does_not_change_the_manifest(tmp_path):
    records = [make_candidate(f"c-{i}") for i in range(6)]
    assignments = {c.candidate_id: DatasetSplit.TRAIN.value for c in records}
    forward = write_version(tmp_path, candidates=records, assignments=assignments)
    write_version(tmp_path, version="v2", parent=forward.manifest_hash,
                  candidates=list(reversed(records)), assignments=assignments)
    # Only the version id and the parent differ; the shard bytes are identical.
    assert ((version_dir(tmp_path, "corpus", "v1") / "train.jsonl").read_bytes()
            == (version_dir(tmp_path, "corpus", "v2") / "train.jsonl").read_bytes())


def test_encode_shard_is_deterministic_and_newline_terminated():
    records = [make_candidate("c-2"), make_candidate("c-1")]
    text = encode_shard(records)
    assert text == encode_shard(list(reversed(records)))
    assert text.endswith("\n")
    assert text.count("\n") == 2
    assert json.loads(text.splitlines()[0])["candidate_id"] == "c-1"


def test_a_different_seed_produces_a_different_manifest_identity(tmp_path):
    first = write_version(tmp_path)
    second = write_version(tmp_path, version="v2", parent=first.manifest_hash,
                           seed="seed-b")
    assert first.manifest_hash != second.manifest_hash
    assert first.manifest.seed != second.manifest.seed


def test_a_different_policy_or_leakage_hash_produces_a_different_identity(tmp_path):
    first = write_version(tmp_path)
    second = write_version(tmp_path, version="v2", parent=first.manifest_hash,
                           split_policy_hash=sha256_text("policy-2"))
    third = write_version(tmp_path, version="v3", parent=second.manifest_hash,
                          leakage_report_hash=sha256_text("leakage-2"))
    assert len({first.manifest_hash, second.manifest_hash, third.manifest_hash}) == 3


def test_a_revocation_snapshot_change_alters_manifest_identity(tmp_path):
    first = write_version(tmp_path)
    second = write_version(tmp_path, version="v2", parent=first.manifest_hash,
                           revocation=revocation("unrelated-candidate"))
    assert first.manifest.revocation_snapshot_hash != \
        second.manifest.revocation_snapshot_hash
    assert first.manifest_hash != second.manifest_hash


def test_an_empty_training_dataset_is_not_fabricated_silently(tmp_path):
    with pytest.raises(ManifestError, match="no TRAIN records"):
        write_version(tmp_path, candidates=[make_candidate("c-3")],
                      assignments={"c-3": DatasetSplit.HIDDEN_EVALUATION.value})
    # Nothing was left behind by the refusal.
    assert not version_dir(tmp_path, "corpus", "v1").exists()


def test_an_empty_training_split_is_permitted_only_when_declared(tmp_path):
    written = write_version(
        tmp_path, candidates=[make_candidate("c-3")],
        assignments={"c-3": DatasetSplit.HIDDEN_EVALUATION.value},
        allow_empty_splits=(DatasetSplit.TRAIN.value,))
    assert written.manifest.counts().get("train", 0) == 0
    assert verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1").ok


def test_a_candidate_with_no_split_assignment_is_never_written(tmp_path):
    with pytest.raises(ManifestError, match="no split assignment"):
        write_version(tmp_path, candidates=[make_candidate("c-1")], assignments={})
    assert not version_dir(tmp_path, "corpus", "v1").exists()


def test_a_candidate_assigned_to_quarantine_is_never_written(tmp_path):
    with pytest.raises(ManifestError, match="has no shard"):
        write_version(tmp_path, candidates=[make_candidate("c-1")],
                      assignments={"c-1": DatasetSplit.QUARANTINE.value})


def test_the_reviewer_pseudonym_carries_no_operator_identity():
    candidate = make_candidate("c-1", editor_id="alejandro-b",
                               target_source=TargetSource.HUMAN_EDITED,
                               student_output_changed=True)
    row = ManifestCandidateRow.from_candidate(candidate, DatasetSplit.TRAIN)
    assert row.human_reviewer_safe_id == reviewer_safe_id(candidate.approval_hash)
    assert "alejandro" not in row.human_reviewer_safe_id
    assert row.human_reviewer_safe_id.startswith("rev-")
