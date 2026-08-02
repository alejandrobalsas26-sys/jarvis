"""V69 M62 S2d — SFT and preference exports.

An export is the point at which the store's controls stop applying and a file starts
existing. The claims under test are that the exporter reads exactly one shard of exactly
one verified version, that every exclusion is counted rather than skipped, and that a
preference pair cannot be assembled from an approval nobody gave for that pair.

Nothing here trains, tokenizes or runs DPO — the last test in each half asserts that
directly, because "it does not" is a property that decays silently.
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
from training_gym.datasets.export import (
    SFT_FILENAME,
    SFT_MANIFEST_FILENAME,
    ExportError,
    SFTExportManifest,
    export_dir,
    export_sft,
    row_hash,
    sft_row,
    verify_sft_export,
)
from training_gym.datasets.manifests import (
    GENESIS_PARENT,
    RevocationSnapshot,
    version_dir,
    write_dataset_version,
)
from training_gym.datasets.preference import (
    PAIR_SIMILARITY_CEILING,
    PREFERENCE_FILENAME,
    PreferenceDecision,
    PreferenceError,
    PreferenceHumanReview,
    RejectedResponse,
    RejectionReason,
    build_preference_pair,
    export_preference,
    pair_blockers,
    verify_preference_export,
)
from training_gym.schemas import SchemaError, SensitivityClass, sha256_text
from training_gym.task_spec import TaskFamily

NOW = "2026-08-02T11:00:00Z"


def make_candidate(candidate_id: str, *, state: CandidateState = CandidateState.PROMOTED,
                   prompt: str = "", target: str = "", **overrides) -> DatasetCandidate:
    prompt = prompt or f"Analyse the alert for {candidate_id}"
    target = target or f'{{"verdict": "benign", "case": "{candidate_id}"}}'
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
        "system_message": "You are a SOC analyst.",
        "system_prompt_version": "sys.v3",
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


def build_version(root, *, candidates=None, assignments=None, dataset_id="corpus",
                  version="v1", **overrides):
    candidates = candidates if candidates is not None else [
        make_candidate("c-1"), make_candidate("c-2"),
        make_candidate("c-3", task_family=TaskFamily.SOC_TRIAGE)]
    assignments = assignments if assignments is not None else {
        "c-1": DatasetSplit.TRAIN.value, "c-2": DatasetSplit.TRAIN.value,
        "c-3": DatasetSplit.HIDDEN_EVALUATION.value}
    kwargs = {"root": root, "dataset_id": dataset_id, "dataset_version": version,
              "candidates": candidates, "assignments": assignments,
              "parent_manifest_hash": GENESIS_PARENT, "created_at_utc": NOW,
              "seed": "seed-a", "split_policy_hash": sha256_text("policy"),
              "leakage_report_hash": sha256_text("leakage"),
              "revocation": RevocationSnapshot()}
    kwargs.update(overrides)
    return write_dataset_version(**kwargs)


def run_export(root, **overrides):
    kwargs = {"root": root, "dataset_id": "corpus", "dataset_version": "v1",
              "revocation": RevocationSnapshot(), "created_at_utc": NOW}
    kwargs.update(overrides)
    return export_sft(**kwargs)


# ── 56..57: source discipline ─────────────────────────────────────────────────
def test_the_sft_export_reads_the_train_split_and_nothing_else(tmp_path):
    build_version(tmp_path)
    export = run_export(tmp_path)
    assert export.manifest.record_count == 2
    assert export.manifest.source_split == DatasetSplit.TRAIN.value
    path = export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME
    ids = [json.loads(line)["metadata"]["candidate_id"]
           for line in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["c-1", "c-2"]
    assert "c-3" not in path.read_text(encoding="utf-8")


def test_the_source_manifest_hash_is_the_train_shard_digest(tmp_path):
    written = build_version(tmp_path)
    export = run_export(tmp_path)
    shard = written.manifest.shard_for(DatasetSplit.TRAIN)
    assert export.manifest.source_manifest_hash == shard.sha256_file
    assert export.manifest.dataset_manifest_hash == written.manifest_hash


def test_an_unverified_dataset_version_cannot_be_exported(tmp_path):
    build_version(tmp_path)
    (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").write_text(
        "tampered\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="refusing to use an unverified version"):
        run_export(tmp_path)


def test_a_missing_dataset_version_cannot_be_exported(tmp_path):
    with pytest.raises(SchemaError, match="refusing to use an unverified version"):
        run_export(tmp_path)


# ── 58..61: exclusions, counted rather than skipped ───────────────────────────
def test_a_revoked_candidate_is_excluded_and_counted(tmp_path):
    build_version(tmp_path)
    revocation = RevocationSnapshot(entries=({"candidate_id": "c-2", "reason": "leak",
                                              "actor": "operator-1", "at": NOW},))
    export = run_export(tmp_path, revocation=revocation)
    assert export.manifest.record_count == 1
    assert export.manifest.excluded_counts == {"revoked": 1}


def test_hidden_evaluation_material_can_never_reach_an_sft_row(tmp_path):
    build_version(tmp_path)
    export = run_export(tmp_path)
    path = export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME
    body = path.read_text(encoding="utf-8")
    assert "c-3" not in body
    assert export.manifest.record_count == 2
    # The held-out shard exists and was never opened by the exporter.
    assert (version_dir(tmp_path, "corpus", "v1") / "hidden_evaluation.jsonl").is_file()


def test_an_evaluation_only_record_is_never_exported(tmp_path):
    """It cannot even reach a TRAIN shard, and the exporter re-checks anyway."""
    evaluation_only = make_candidate("c-4", evaluation_only=True, dataset_eligible=False)
    with pytest.raises(SchemaError, match="evaluation_only"):
        build_version(tmp_path, candidates=[make_candidate("c-1"), evaluation_only],
                      assignments={"c-1": DatasetSplit.TRAIN.value,
                                   "c-4": DatasetSplit.TRAIN.value})


def test_quarantined_material_has_no_shard_to_be_exported_from(tmp_path):
    with pytest.raises(SchemaError, match="no shard"):
        build_version(tmp_path, candidates=[make_candidate("c-1")],
                      assignments={"c-1": DatasetSplit.QUARANTINE.value})


def test_every_record_being_revoked_is_reported_honestly(tmp_path):
    build_version(tmp_path)
    revocation = RevocationSnapshot(entries=tuple(
        {"candidate_id": cid, "reason": "recall", "actor": "op", "at": NOW}
        for cid in ("c-1", "c-2")))
    with pytest.raises(ExportError, match="no eligible train records"):
        run_export(tmp_path, revocation=revocation)
    assert not (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).exists()


# ── 62..66: determinism and integrity ─────────────────────────────────────────
def test_the_export_bytes_are_deterministic(tmp_path):
    build_version(tmp_path)
    first = run_export(tmp_path)
    build_version(tmp_path, version="v2", parent_manifest_hash=GENESIS_PARENT)
    second = export_sft(root=tmp_path, dataset_id="corpus", dataset_version="v2",
                        revocation=RevocationSnapshot(), created_at_utc=NOW)
    a = (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).read_text(encoding="utf-8")
    b = (export_dir(tmp_path, "corpus", "v2") / SFT_FILENAME).read_text(encoding="utf-8")
    # Only the recorded dataset_version differs between the two files.
    assert a.replace('"dataset_version":"v1"', "X") == b.replace(
        '"dataset_version":"v2"', "X")
    assert first.manifest.record_count == second.manifest.record_count


def test_the_export_manifest_verifies_the_written_file(tmp_path):
    build_version(tmp_path)
    run_export(tmp_path)
    result = verify_sft_export(out_root=tmp_path, dataset_id="corpus",
                               dataset_version="v1")
    assert result.ok, result.problems


def test_an_altered_export_is_detected(tmp_path):
    build_version(tmp_path)
    run_export(tmp_path)
    path = export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["messages"][-1]["content"] = '{"verdict": "malicious"}'
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_sft_export(out_root=tmp_path, dataset_id="corpus",
                               dataset_version="v1")
    assert not result.ok
    assert any("content digest" in p for p in result.problems)


def test_an_altered_export_manifest_is_detected(tmp_path):
    build_version(tmp_path)
    run_export(tmp_path)
    path = export_dir(tmp_path, "corpus", "v1") / SFT_MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["record_count"] = 99
    with pytest.raises(ExportError, match="does not match its content"):
        SFTExportManifest.from_dict(payload)


def test_an_export_is_never_overwritten(tmp_path):
    build_version(tmp_path)
    run_export(tmp_path)
    before = (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).read_bytes()
    with pytest.raises(ExportError, match="already exists"):
        run_export(tmp_path)
    assert (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).read_bytes() == before


def test_a_surviving_export_manifest_alone_still_blocks_a_re_export(tmp_path):
    """Deleting only the data file must not let a new one land under an old manifest."""
    build_version(tmp_path)
    run_export(tmp_path)
    (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).unlink()
    with pytest.raises(ExportError, match="already exists"):
        run_export(tmp_path)
    assert not (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).exists()


# ── 67..71: what a row may and may not carry ──────────────────────────────────
def test_a_row_carries_the_prompt_the_target_and_its_provenance(tmp_path):
    candidate = make_candidate("c-1")
    row = sft_row(candidate, dataset_version="v1",
                  source_manifest_hash=sha256_text("shard"))
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert row["messages"][-1]["content"] == candidate.target_text
    assert row["metadata"]["candidate_hash"] == candidate.candidate_hash()
    assert row["metadata"]["target_source"] == "verified_student_output"
    assert row["metadata"]["system_prompt_version"] == "sys.v3"
    assert row_hash(row) == row_hash(sft_row(candidate, dataset_version="v1",
                                             source_manifest_hash=sha256_text("shard")))


def test_an_absent_system_message_produces_no_empty_system_turn():
    row = sft_row(make_candidate("c-1", system_message=""), dataset_version="v1",
                  source_manifest_hash=sha256_text("shard"))
    assert [m["role"] for m in row["messages"]] == ["user", "assistant"]


def test_tool_calls_keep_their_strict_structure_outside_the_messages():
    candidate = make_candidate("c-1", tool_calls=(
        {"name": "nmap_scan", "arguments": {"target": "10.0.0.1", "flags": "-sV"}},))
    row = sft_row(candidate, dataset_version="v1",
                  source_manifest_hash=sha256_text("shard"))
    assert row["tool_calls"] == [{"name": "nmap_scan",
                                  "arguments": {"flags": "-sV", "target": "10.0.0.1"}}]
    assert "tool_calls" not in row["messages"][-1]


def test_no_exported_row_carries_a_private_path_or_a_secret(tmp_path):
    build_version(tmp_path)
    run_export(tmp_path)
    body = (export_dir(tmp_path, "corpus", "v1") / SFT_FILENAME).read_text(
        encoding="utf-8")
    assert str(tmp_path) not in body
    for marker in ("C:\\Users", "/home/", "api_key", "BEGIN PRIVATE KEY"):
        assert marker not in body


def test_the_reported_export_paths_are_relative(tmp_path):
    build_version(tmp_path)
    export = run_export(tmp_path)
    assert export.relative_paths == (
        f"exports/corpus/v1/{SFT_FILENAME}", f"exports/corpus/v1/{SFT_MANIFEST_FILENAME}")
    assert all(str(tmp_path) not in p for p in export.relative_paths)


def test_the_sft_export_does_not_tokenize_or_load_a_model(tmp_path):
    import sys
    banned = ("torch", "transformers", "tokenizers", "peft", "trl", "datasets")
    already = {n for n in banned if n in sys.modules}
    build_version(tmp_path)
    run_export(tmp_path)
    assert {n for n in banned if n in sys.modules} == already


# ── preference: 73..88 ────────────────────────────────────────────────────────
def rejected_response(chosen: DatasetCandidate, *, text: str = "", **overrides
                      ) -> RejectedResponse:
    base = {
        "rejected_id": overrides.pop("rejected_id", f"rej-{chosen.candidate_id}"),
        "task_hash": chosen.task_hash,
        "attempt_hash": sha256_text(f"other-attempt-{chosen.candidate_id}"),
        "text": text or "The host is definitely compromised; wipe it immediately.",
        "reasons": (RejectionReason.HALLUCINATED_EVIDENCE,),
        "evidence_refs": (f"grader:evidence:{sha256_text('ev')[:16]}",),
        "source_model_id": "qwen3:8b-q4_K_M",
        "created_at_utc": NOW,
        "provenance": "gym_episode",
        "safe_to_retain": True,
    }
    base.update(overrides)
    return RejectedResponse(**base)


def pair_review(chosen: DatasetCandidate, rejected: RejectedResponse,
                **overrides) -> PreferenceHumanReview:
    base = {
        "review_id": overrides.pop("review_id", f"pr-{chosen.candidate_id}"),
        "reviewer": "operator-1",
        "decision": PreferenceDecision.APPROVED,
        "timestamp": NOW,
        "task_hash": chosen.task_hash,
        "chosen_hash": sha256_text(chosen.target_text),
        "rejected_hash": rejected.text_hash,
    }
    base.update(overrides)
    return PreferenceHumanReview(**base)


def make_pair(pair_id="pair-1", *, chosen=None, rejected=None, review=None,
              split=DatasetSplit.TRAIN, revocation=None, **overrides):
    chosen = chosen or make_candidate("c-1")
    rejected = rejected if rejected is not None else rejected_response(chosen)
    review = review if review is not None else pair_review(chosen, rejected)
    return build_preference_pair(
        pair_id=pair_id, chosen=chosen, rejected=rejected, review=review, split=split,
        revocation=revocation or RevocationSnapshot(), created_at_utc=NOW, **overrides)


def test_a_valid_pair_binds_both_sides_and_the_approval():
    pair = make_pair()
    assert pair.review.approves
    assert pair.pair_hash() == make_pair().pair_hash()
    row = pair.to_row()
    assert row["chosen"] == pair.chosen.target_text
    assert row["rejected"] == pair.rejected.text
    assert row["metadata"]["review_hash"] == pair.review.review_hash()
    assert row["metadata"]["rejection_reasons"] == ["hallucinated_evidence"]


def test_the_chosen_side_must_be_a_promoted_candidate():
    chosen = make_candidate("c-1", state=CandidateState.READY_FOR_PROMOTION)
    with pytest.raises(PreferenceError, match="only a promoted candidate"):
        make_pair(chosen=chosen)


def test_the_rejected_side_needs_structured_reasons_and_evidence():
    chosen = make_candidate("c-1")
    with pytest.raises(PreferenceError, match="at least one structured reason"):
        rejected_response(chosen, reasons=())
    with pytest.raises(PreferenceError, match="evidence_refs: required"):
        rejected_response(chosen, evidence_refs=())
    with pytest.raises(PreferenceError, match="provenance: required"):
        rejected_response(chosen, provenance="")


def test_both_sides_must_answer_the_same_task():
    chosen = make_candidate("c-1")
    other = rejected_response(chosen, task_hash=sha256_text("a different task"))
    review = pair_review(chosen, other, task_hash=other.task_hash)
    with pytest.raises(PreferenceError, match="answer different tasks"):
        make_pair(chosen=chosen, rejected=other, review=review)


def test_an_identical_pair_is_refused():
    """Two ways, because the first one makes the pair unbuildable before it is judged."""
    chosen = make_candidate("c-1")
    identical = rejected_response(chosen, text=chosen.target_text)
    # A correctly-bound approval for two identical texts cannot even be written down.
    with pytest.raises(PreferenceError, match="approves one text over itself"):
        pair_review(chosen, identical)
    # And a forged approval naming two different digests still fails on the content.
    forged = pair_review(chosen, identical,
                         rejected_hash=sha256_text("a different answer entirely"))
    problems = pair_blockers(chosen=chosen, rejected=identical, review=forged,
                             split=DatasetSplit.TRAIN, revocation=RevocationSnapshot())
    assert any("byte-identical" in p for p in problems)
    assert any("not for this pair" in p for p in problems)


def test_a_pair_differing_only_in_whitespace_is_refused():
    chosen = make_candidate("c-1")
    spaced = rejected_response(chosen, text=f"  {chosen.target_text}  \n")
    with pytest.raises(PreferenceError, match="normalize to the same text"):
        make_pair(chosen=chosen, rejected=spaced, review=pair_review(chosen, spaced))


def test_a_near_identical_pair_is_refused():
    target = ("The alert is a false positive because the parent process is the "
              "package manager and the hash is on the allowlist.")
    chosen = make_candidate("c-1", target=target)
    near = rejected_response(
        chosen, text=target.replace("false positive", "false-positive") + " x")
    with pytest.raises(PreferenceError, match="ceiling"):
        make_pair(chosen=chosen, rejected=near, review=pair_review(chosen, near))
    assert PAIR_SIMILARITY_CEILING > 0.5


def test_a_pair_approval_must_exist_and_be_approved():
    chosen = make_candidate("c-1")
    rejected = rejected_response(chosen)
    refused = pair_review(chosen, rejected, decision=PreferenceDecision.REJECTED,
                          reason="the rejected side is not actually worse")
    with pytest.raises(PreferenceError, match="pair review decision is rejected"):
        make_pair(chosen=chosen, rejected=rejected, review=refused)


def test_a_pair_approval_is_bound_to_both_hashes():
    chosen = make_candidate("c-1")
    rejected = rejected_response(chosen)
    other = rejected_response(chosen, rejected_id="rej-other",
                              text="Escalate to tier 3 without evidence.")
    # An approval issued for a DIFFERENT rejected side does not authorise this pair.
    stale = pair_review(chosen, other)
    with pytest.raises(PreferenceError, match="not for this pair"):
        make_pair(chosen=chosen, rejected=rejected, review=stale)
    # ...and one issued for a different chosen side does not either.
    elsewhere = make_candidate("c-2", task_hash=chosen.task_hash)
    lifted = pair_review(elsewhere, rejected)
    with pytest.raises(PreferenceError, match="not for this pair"):
        make_pair(chosen=chosen, rejected=rejected, review=lifted)


def test_a_candidate_review_cannot_stand_in_for_a_pair_review():
    """The chosen candidate's own approval is not a pair approval, by type and by hash."""
    chosen = make_candidate("c-1")
    rejected = rejected_response(chosen)
    with pytest.raises((PreferenceError, AttributeError, TypeError)):
        build_preference_pair(pair_id="pair-1", chosen=chosen, rejected=rejected,
                              review=chosen.approval_hash, split=DatasetSplit.TRAIN,
                              revocation=RevocationSnapshot(), created_at_utc=NOW)


def test_a_review_approving_one_text_over_itself_is_refused():
    chosen = make_candidate("c-1")
    digest = sha256_text(chosen.target_text)
    with pytest.raises(PreferenceError, match="approves one text over itself"):
        PreferenceHumanReview(review_id="pr-1", reviewer="operator-1",
                              decision=PreferenceDecision.APPROVED, timestamp=NOW,
                              task_hash=chosen.task_hash, chosen_hash=digest,
                              rejected_hash=digest)


def test_a_rejected_response_that_is_unsafe_to_retain_is_blocked():
    chosen = make_candidate("c-1")
    unsafe = rejected_response(chosen, safe_to_retain=False)
    with pytest.raises(PreferenceError, match="safe_to_retain is false"):
        make_pair(chosen=chosen, rejected=unsafe, review=pair_review(chosen, unsafe))


def test_restricted_material_is_blocked_on_the_rejected_side():
    chosen = make_candidate("c-1")
    restricted = rejected_response(chosen, sensitivity=SensitivityClass.RESTRICTED)
    with pytest.raises(PreferenceError, match="never stored in a trainable artefact"):
        make_pair(chosen=chosen, rejected=restricted,
                  review=pair_review(chosen, restricted))


def test_a_secret_on_either_side_blocks_the_pair():
    secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF0000111122223333444455556666"
    chosen = make_candidate("c-1")
    leaky = rejected_response(chosen, text=f"Use these credentials: {secret}")
    problems = pair_blockers(chosen=chosen, rejected=leaky,
                             review=pair_review(chosen, leaky),
                             split=DatasetSplit.TRAIN, revocation=RevocationSnapshot())
    assert any("private content" in p for p in problems)

    leaky_chosen = make_candidate("c-2", target=f"Answer: {secret}")
    other = rejected_response(leaky_chosen)
    problems = pair_blockers(chosen=leaky_chosen, rejected=other,
                             review=pair_review(leaky_chosen, other),
                             split=DatasetSplit.TRAIN, revocation=RevocationSnapshot())
    assert any("private content" in p for p in problems)


@pytest.mark.parametrize("split", [
    DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
    DatasetSplit.ADVERSARIAL, DatasetSplit.QUARANTINE])
def test_held_out_and_quarantined_material_never_becomes_a_pair(split):
    with pytest.raises(PreferenceError, match="may never appear in a preference pair"):
        make_pair(split=split)


def test_an_evaluation_only_chosen_side_is_blocked():
    chosen = make_candidate("c-1", evaluation_only=True, dataset_eligible=False)
    with pytest.raises(PreferenceError, match="evaluation-only"):
        make_pair(chosen=chosen)


def test_a_revoked_chosen_side_is_blocked():
    chosen = make_candidate("c-1")
    revocation = RevocationSnapshot(entries=({"candidate_id": "c-1", "reason": "recall",
                                              "actor": "op", "at": NOW},))
    with pytest.raises(PreferenceError, match="chosen side has been revoked"):
        make_pair(chosen=chosen, revocation=revocation)


def test_a_tampered_rejected_response_is_detected_on_reload():
    chosen = make_candidate("c-1")
    rejected = rejected_response(chosen)
    payload = rejected.to_dict()
    payload["text"] = "A completely different rejected answer."
    with pytest.raises(PreferenceError, match="edited since it was judged"):
        RejectedResponse.from_dict(payload)


def test_a_tampered_pair_review_is_detected_on_reload():
    chosen = make_candidate("c-1")
    rejected = rejected_response(chosen)
    payload = pair_review(chosen, rejected).to_record()
    payload["reviewer"] = "someone-else"
    with pytest.raises(PreferenceError, match="does not match its content"):
        PreferenceHumanReview.from_dict(payload)


# ── the preference export ─────────────────────────────────────────────────────
def test_the_preference_export_writes_and_verifies(tmp_path):
    written = build_version(tmp_path)
    pairs = [make_pair("pair-1"),
             make_pair("pair-2", chosen=make_candidate("c-2"),
                       rejected=rejected_response(make_candidate("c-2"),
                                                  rejected_id="rej-c-2"),
                       review=pair_review(make_candidate("c-2"),
                                          rejected_response(make_candidate("c-2"),
                                                            rejected_id="rej-c-2"),
                                          review_id="pr-c-2"))]
    export = export_preference(pairs=pairs, root=tmp_path, dataset_id="corpus",
                               dataset_version="v1",
                               dataset_manifest_hash=written.manifest_hash,
                               created_at_utc=NOW)
    assert export.manifest.pair_count == 2
    assert verify_preference_export(out_root=tmp_path, dataset_id="corpus",
                                    dataset_version="v1") == ()
    assert export.to_dict()["runs_dpo"] is False


def test_an_empty_preference_export_is_refused(tmp_path):
    written = build_version(tmp_path)
    with pytest.raises(PreferenceError, match="no approved pairs"):
        export_preference(pairs=[], root=tmp_path, dataset_id="corpus",
                          dataset_version="v1",
                          dataset_manifest_hash=written.manifest_hash,
                          created_at_utc=NOW)


def test_an_altered_preference_file_is_detected(tmp_path):
    written = build_version(tmp_path)
    export_preference(pairs=[make_pair()], root=tmp_path, dataset_id="corpus",
                      dataset_version="v1",
                      dataset_manifest_hash=written.manifest_hash, created_at_utc=NOW)
    path = export_dir(tmp_path, "corpus", "v1") / PREFERENCE_FILENAME
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["chosen"], row["rejected"] = row["rejected"], row["chosen"]
    path.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    problems = verify_preference_export(out_root=tmp_path, dataset_id="corpus",
                                        dataset_version="v1")
    assert any("content digest" in p for p in problems)


def test_the_preference_export_runs_no_preference_optimisation(tmp_path):
    import sys
    banned = ("torch", "transformers", "trl", "peft", "datasets", "accelerate")
    already = {n for n in banned if n in sys.modules}
    written = build_version(tmp_path)
    export_preference(pairs=[make_pair()], root=tmp_path, dataset_id="corpus",
                      dataset_version="v1",
                      dataset_manifest_hash=written.manifest_hash, created_at_utc=NOW)
    assert {n for n in banned if n in sys.modules} == already
