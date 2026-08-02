"""V69 M62 S2d — the M16 bridge and the minimal CLI seams.

Two claims. First: an M62 record can be represented as an M16 ``TrainingExample`` without
anything being weakened on the way — the approval, the state and the provenance all
survive, and a field M16 cannot carry is a refusal rather than a silent drop. Second: the
CLI cannot promote anything without the exact plan-bound token, writes nothing on a dry
run, and never emits an absolute path.

The bridge tests run M16's own ``write_dataset`` and ``verify_dataset`` over the bridged
rows, because a mapping that only satisfies the tests that describe it has proved nothing
about the pipeline it is meant to feed.
"""
from __future__ import annotations

import json

import pytest

from core.dataset_pipeline import (
    CandidateStatus,
    ExampleProvenance,
    load_dataset,
    write_dataset,
)
from scripts import training_gym_dataset as cli
from training_gym.datasets.bridge import (
    BRIDGE_VERSION,
    BridgeError,
    bridge_blockers,
    bridge_dataset_version,
    to_training_example,
    verify_bridged,
)
from training_gym.datasets.candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    TargetSource,
)
from training_gym.datasets.candidate_store import CandidateStore
from training_gym.datasets.manifests import (
    GENESIS_PARENT,
    RevocationSnapshot,
    version_dir,
    write_dataset_version,
)
from training_gym.schemas import sha256_text
from training_gym.task_spec import TaskFamily

NOW = "2026-08-02T12:00:00Z"


def make_candidate(candidate_id: str, *, state: CandidateState = CandidateState.PROMOTED,
                   **overrides) -> DatasetCandidate:
    prompt = overrides.pop("prompt", f"Triage the alert for {candidate_id}")
    target = overrides.pop("target", f'{{"verdict": "benign", "id": "{candidate_id}"}}')
    base = {
        "candidate_id": candidate_id,
        "task_id": overrides.pop("task_id", candidate_id),
        "task_family": overrides.pop("task_family", TaskFamily.SOC_TRIAGE),
        "task_hash": sha256_text(f"task-{candidate_id}"),
        "attempt_id": f"attempt-{candidate_id}",
        "attempt_hash": sha256_text(f"attempt-{candidate_id}"),
        "deterministic_report_hash": sha256_text(f"det-{candidate_id}"),
        "consensus_report_hash": sha256_text(f"con-{candidate_id}"),
        "human_review_hash": sha256_text(f"rev-{candidate_id}"),
        "source_episode_hash": sha256_text(f"ep-{candidate_id}"),
        "student_model_id": "qwen3:8b-q4_K_M",
        "system_message": "You are a SOC analyst.",
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


def build_version(root, *, candidates=None, assignments=None, version="v1", **overrides):
    candidates = candidates if candidates is not None else [
        make_candidate("c-1"), make_candidate("c-2"), make_candidate("c-3")]
    assignments = assignments if assignments is not None else {
        "c-1": DatasetSplit.TRAIN.value, "c-2": DatasetSplit.TRAIN.value,
        "c-3": DatasetSplit.HIDDEN_EVALUATION.value}
    kwargs = {"root": root, "dataset_id": "corpus", "dataset_version": version,
              "candidates": candidates, "assignments": assignments,
              "parent_manifest_hash": GENESIS_PARENT, "created_at_utc": NOW,
              "seed": "seed-a", "split_policy_hash": sha256_text("policy"),
              "leakage_report_hash": sha256_text("leakage"),
              "revocation": RevocationSnapshot()}
    kwargs.update(overrides)
    return write_dataset_version(**kwargs)


def bridge(root, **overrides):
    kwargs = {"root": root, "dataset_id": "corpus", "dataset_version": "v1",
              "revocation": RevocationSnapshot()}
    kwargs.update(overrides)
    return bridge_dataset_version(**kwargs)


# ── 89..91: the mapping ───────────────────────────────────────────────────────
def test_a_promoted_candidate_maps_onto_an_m16_training_example(tmp_path):
    written = build_version(tmp_path)
    examples, report = bridge(tmp_path)
    assert len(examples) == 2
    assert report.example_count == 2
    example = examples[0]
    assert example.status is CandidateStatus.APPROVED
    assert example.prompt == "Triage the alert for c-1"
    assert example.ideal_output == '{"verdict": "benign", "id": "c-1"}'
    assert example.domain == TaskFamily.SOC_TRIAGE.value
    assert example.version == "v1"
    assert report.dataset_manifest_hash == written.manifest_hash
    assert report.to_dict()["writes_a_dataset"] is False


def test_the_m62_manifest_and_approval_provenance_is_retained(tmp_path):
    written = build_version(tmp_path)
    examples, _ = bridge(tmp_path)
    refs = examples[0].source_refs
    assert f"m62:manifest:{written.manifest_hash}" in refs
    assert "m62:dataset:corpus@v1" in refs
    assert any(r.startswith("m62:candidate:") for r in refs)
    assert any(r.startswith("m62:review:") for r in refs)
    assert examples[0].review["m62_review_hash"] == sha256_text("rev-c-1")
    assert examples[0].review["bridge_version"] == BRIDGE_VERSION
    assert verify_bridged(examples) == ()


def test_a_model_authored_target_stays_model_generated_in_m16(tmp_path):
    """Approval lives in the status, not in a relabelled provenance."""
    build_version(tmp_path)
    examples, _ = bridge(tmp_path)
    assert examples[0].provenance == ExampleProvenance.MODEL_GENERATED.value
    assert not ExampleProvenance(examples[0].provenance).is_ground_truth
    assert examples[0].status is CandidateStatus.APPROVED


def test_a_human_authored_target_maps_to_the_human_provenance(tmp_path):
    human = make_candidate("c-1", target_source=TargetSource.HUMAN_AUTHORED,
                           editor_id="operator-1")
    build_version(tmp_path, candidates=[human],
                  assignments={"c-1": DatasetSplit.TRAIN.value})
    examples, _ = bridge(tmp_path)
    assert examples[0].provenance == ExampleProvenance.HUMAN.value


def test_a_teacher_proposed_target_is_never_relabelled_as_ground_truth(tmp_path):
    teacher = make_candidate("c-1",
                             target_source=TargetSource.TEACHER_PROPOSED_HUMAN_APPROVED,
                             editor_id="operator-1")
    build_version(tmp_path, candidates=[teacher],
                  assignments={"c-1": DatasetSplit.TRAIN.value})
    examples, _ = bridge(tmp_path)
    assert examples[0].provenance == ExampleProvenance.MODEL_GENERATED.value


# ── 92..94: what may not cross ────────────────────────────────────────────────
@pytest.mark.parametrize("state", [
    CandidateState.READY_FOR_PROMOTION, CandidateState.LEAKAGE_CHECKED,
    CandidateState.REJECTED, CandidateState.QUARANTINED, CandidateState.REVOKED,
    CandidateState.NEEDS_REVISION, CandidateState.FAILED])
def test_only_a_promoted_record_crosses_into_m16(state):
    blockers = bridge_blockers(make_candidate("c-1", state=state),
                               split=DatasetSplit.TRAIN,
                               revocation=RevocationSnapshot())
    assert any("only a promoted record" in b for b in blockers)


@pytest.mark.parametrize("split", [
    DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
    DatasetSplit.ADVERSARIAL, DatasetSplit.VALIDATION, DatasetSplit.QUARANTINE])
def test_no_split_but_train_becomes_m16_training_data(split):
    blockers = bridge_blockers(make_candidate("c-1"), split=split,
                               revocation=RevocationSnapshot())
    assert any("only train material" in b for b in blockers)


def test_hidden_evaluation_records_are_never_bridged(tmp_path):
    build_version(tmp_path)
    examples, _ = bridge(tmp_path)
    assert [e.failure_ref for e in examples] == ["c-1", "c-2"]
    assert all("c-3" not in json.dumps(e.to_dict()) for e in examples)


def test_a_revoked_record_is_never_bridged(tmp_path):
    build_version(tmp_path)
    revocation = RevocationSnapshot(entries=({"candidate_id": "c-1", "reason": "recall",
                                              "actor": "op", "at": NOW},))
    with pytest.raises(BridgeError, match="has been revoked"):
        bridge(tmp_path, revocation=revocation)
    examples, report = bridge(tmp_path, revocation=revocation, strict=False)
    assert [e.failure_ref for e in examples] == ["c-2"]
    assert report.excluded[0]["candidate_id"] == "c-1"


def test_a_field_m16_cannot_represent_fails_closed(tmp_path):
    with_tools = make_candidate("c-1", tool_calls=(
        {"name": "nmap_scan", "arguments": {"target": "10.0.0.1"}},))
    build_version(tmp_path, candidates=[with_tools],
                  assignments={"c-1": DatasetSplit.TRAIN.value})
    with pytest.raises(BridgeError, match="structured tool call"):
        bridge(tmp_path)
    with pytest.raises(BridgeError, match="structured tool call"):
        to_training_example(with_tools, split=DatasetSplit.TRAIN,
                            revocation=RevocationSnapshot(), dataset_id="corpus",
                            dataset_version="v1",
                            dataset_manifest_hash=sha256_text("m"))


def test_an_unverified_version_cannot_be_bridged(tmp_path):
    build_version(tmp_path)
    (version_dir(tmp_path, "corpus", "v1") / "train.jsonl").write_text(
        "tampered\n", encoding="utf-8")
    with pytest.raises(Exception, match="refusing to use an unverified version"):
        bridge(tmp_path)


# ── 95..96: determinism and M16 compatibility ─────────────────────────────────
def test_the_bridge_output_is_deterministic(tmp_path):
    build_version(tmp_path)
    first, first_report = bridge(tmp_path)
    second, second_report = bridge(tmp_path)
    assert [e.to_dict() for e in first] == [e.to_dict() for e in second]
    assert first_report.report_hash() == second_report.report_hash()


def test_bridged_examples_survive_m16_write_and_verify(tmp_path):
    build_version(tmp_path)
    examples, _ = bridge(tmp_path)
    out = tmp_path / "m16"
    manifest = write_dataset(list(examples), out, version="v1", now_ts=0.0)
    assert manifest.count == 2
    assert manifest.skipped_unapproved == 0
    reloaded = load_dataset(out / "v1")
    assert [e.id for e in reloaded] == sorted(e.id for e in examples)
    assert all(e.status is CandidateStatus.APPROVED for e in reloaded)

    from core.training_pipeline import DatasetReference, TrainingPipeline

    gate = TrainingPipeline().verify_dataset(
        DatasetReference(version="v1", path=str(out / "v1"),
                         expected_hash=manifest.content_sha256), min_samples=1)
    assert gate.ok, gate.issues
    assert gate.example_count == 2
    assert gate.content_hash == manifest.content_sha256


def test_the_bridge_itself_never_writes_a_dataset(tmp_path):
    build_version(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    bridge(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_bridging_a_version_with_no_eligible_record_is_refused(tmp_path):
    build_version(tmp_path)
    revocation = RevocationSnapshot(entries=tuple(
        {"candidate_id": cid, "reason": "recall", "actor": "op", "at": NOW}
        for cid in ("c-1", "c-2")))
    with pytest.raises(BridgeError, match="produced no M16 examples"):
        bridge(tmp_path, revocation=revocation, strict=False)


# ── 97..101: the CLI ──────────────────────────────────────────────────────────
def seed_store(root, *, count: int = 6) -> CandidateStore:
    """A store holding candidates the CLI can plan a promotion from."""
    store = CandidateStore(root)
    for index in range(count):
        candidate = make_candidate(f"c-{index}",
                                   state=CandidateState.READY_FOR_PROMOTION)
        store.write_candidate(candidate)
    return store


def run_cli(argv, capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_importing_the_cli_performs_no_io_and_contacts_nothing(tmp_path, monkeypatch):
    import importlib
    import socket

    def forbidden(*a, **k):  # pragma: no cover — the point is it never runs
        raise AssertionError("the CLI module contacted the network on import")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.chdir(tmp_path)
    importlib.reload(cli)
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert callable(cli.main)


def test_the_cli_dry_run_writes_nothing(tmp_path, capsys):
    seed_store(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    code, out = run_cli(["--json", "promote", "--root", str(tmp_path), "--dataset-id",
                         "corpus", "--version", "v1", "--seed", "seed-a", "--actor",
                         "operator-1", "--at", NOW], capsys)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["wrote_anything"] is False
    assert payload["confirmation_required"].startswith("PROMOTE:")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "datasets").exists()
    assert not (tmp_path / "promotions.jsonl").exists()


def test_the_cli_refuses_a_wrong_confirmation_with_a_nonzero_exit(tmp_path, capsys):
    seed_store(tmp_path)
    for bad in ("yes", "true", "PROMOTE:" + "0" * 64, "PROMOTE:deadbeef"):
        code, out = run_cli(["--json", "promote", "--root", str(tmp_path),
                             "--dataset-id", "corpus", "--version", "v1", "--seed",
                             "seed-a", "--actor", "operator-1", "--at", NOW,
                             "--confirm", bad], capsys)
        assert code == cli.EXIT_REFUSED
        assert json.loads(out)["ok"] is False
    assert not (tmp_path / "datasets").exists()


def test_there_is_no_force_flag_that_promotes_without_a_plan_token(tmp_path, capsys):
    """The refusal here is argparse's: no such option exists to be found."""
    seed_store(tmp_path)
    for invented in ("--force", "--yes", "--no-confirm"):
        with pytest.raises(SystemExit) as raised:
            cli.main(["promote", "--root", str(tmp_path), "--dataset-id", "corpus",
                      "--version", "v1", "--seed", "seed-a", "--actor", "operator-1",
                      "--at", NOW, invented])
        assert raised.value.code == cli.EXIT_USAGE
    capsys.readouterr()
    assert not (tmp_path / "datasets").exists()


def test_dry_run_and_confirm_together_are_refused(tmp_path, capsys):
    seed_store(tmp_path)
    code, out = run_cli(["--json", "promote", "--root", str(tmp_path), "--dataset-id",
                         "corpus", "--version", "v1", "--seed", "seed-a", "--actor",
                         "operator-1", "--at", NOW, "--dry-run", "--confirm",
                         "PROMOTE:" + "a" * 64], capsys)
    assert code == cli.EXIT_REFUSED
    assert "contradictory" in json.loads(out)["error"]


def test_the_cli_promotes_only_with_the_exact_plan_token(tmp_path, capsys):
    seed_store(tmp_path)
    argv = ["--json", "promote", "--root", str(tmp_path), "--dataset-id", "corpus",
            "--version", "v1", "--seed", "seed-a", "--actor", "operator-1", "--at", NOW]
    _, out = run_cli(argv, capsys)
    token = json.loads(out)["confirmation_required"]

    code, out = run_cli([*argv, "--confirm", token], capsys)
    assert code == cli.EXIT_OK
    result = json.loads(out)
    assert result["ok"] is True
    assert result["records"] >= 1
    assert all(p.startswith("datasets/corpus/v1/") for p in result["relative_paths"])

    code, out = run_cli(["--json", "verify-version", "--root", str(tmp_path),
                         "--dataset-id", "corpus", "--version", "v1"], capsys)
    assert code == cli.EXIT_OK
    assert json.loads(out)["ok"] is True


def test_the_cli_output_carries_no_absolute_path(tmp_path, capsys):
    seed_store(tmp_path)
    argv = ["--json", "promote", "--root", str(tmp_path), "--dataset-id", "corpus",
            "--version", "v1", "--seed", "seed-a", "--actor", "operator-1", "--at", NOW]
    _, plan_out = run_cli(argv, capsys)
    token = json.loads(plan_out)["confirmation_required"]
    _, promote_out = run_cli([*argv, "--confirm", token], capsys)
    _, verify_out = run_cli(["--json", "verify-version", "--root", str(tmp_path),
                             "--dataset-id", "corpus", "--version", "v1"], capsys)
    for body in (plan_out, promote_out, verify_out):
        assert str(tmp_path) not in body
        assert "Users" not in body


def test_the_cli_verifies_a_missing_version_with_a_nonzero_exit(tmp_path, capsys):
    code, out = run_cli(["--json", "verify-version", "--root", str(tmp_path),
                         "--dataset-id", "corpus", "--version", "v1"], capsys)
    assert code == cli.EXIT_REFUSED
    assert json.loads(out)["ok"] is False


def test_the_cli_exports_sft_and_verifies_it(tmp_path, capsys):
    seed_store(tmp_path)
    argv = ["--json", "promote", "--root", str(tmp_path), "--dataset-id", "corpus",
            "--version", "v1", "--seed", "seed-a", "--actor", "operator-1", "--at", NOW]
    _, out = run_cli(argv, capsys)
    run_cli([*argv, "--confirm", json.loads(out)["confirmation_required"]], capsys)

    code, out = run_cli(["--json", "export-sft", "--root", str(tmp_path),
                         "--dataset-id", "corpus", "--version", "v1", "--at", NOW],
                        capsys)
    assert code == cli.EXIT_OK
    export = json.loads(out)
    assert export["record_count"] >= 1
    assert all(not p.startswith("/") for p in export["relative_paths"])

    code, out = run_cli(["--json", "verify-export", "--root", str(tmp_path),
                         "--dataset-id", "corpus", "--version", "v1"], capsys)
    assert code == cli.EXIT_OK
    assert json.loads(out)["ok"] is True


def test_the_cli_refuses_to_pretend_preference_pairs_are_a_flag(tmp_path, capsys):
    code, out = run_cli(["--json", "export-preference", "--root", str(tmp_path),
                         "--dataset-id", "corpus", "--version", "v1"], capsys)
    assert code == cli.EXIT_REFUSED
    payload = json.loads(out)
    assert "PreferenceHumanReview" in payload["note"]


def test_an_empty_store_is_reported_rather_than_promoted(tmp_path, capsys):
    code, out = run_cli(["--json", "promote", "--root", str(tmp_path), "--dataset-id",
                         "corpus", "--version", "v1", "--seed", "seed-a", "--actor",
                         "operator-1", "--at", NOW], capsys)
    assert code == cli.EXIT_REFUSED
    assert "ready_for_promotion" in json.loads(out)["error"]


def test_the_cli_never_loads_training_machinery(tmp_path, capsys):
    import sys
    banned = ("torch", "transformers", "peft", "trl", "accelerate", "tokenizers")
    already = {n for n in banned if n in sys.modules}
    seed_store(tmp_path)
    run_cli(["--json", "promote", "--root", str(tmp_path), "--dataset-id", "corpus",
             "--version", "v1", "--seed", "seed-a", "--actor", "operator-1", "--at",
             NOW], capsys)
    assert {n for n in banned if n in sys.modules} == already


# ── 101..102: generated artifacts stay out of git and out of the package ──────
def test_the_generated_dataset_roots_are_gitignored():
    """Checked against the NEAREST .gitignore, not the union of every one above it.

    A rule that exists only in an outer file is shadowed for any store created beside
    the package, and "it is ignored somewhere up the tree" is precisely the reasoning
    that leaves a corpus of approved training targets staged for commit.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    nearest = next((parent / ".gitignore" for parent in here.parents
                    if (parent / ".gitignore").is_file()), None)
    assert nearest is not None, "no .gitignore governs the test tree"
    body = nearest.read_text(encoding="utf-8")
    for rule in ("dataset_candidates/", "training_gym_datasets/",
                 "training_gym_exports/", "teacher_packets/"):
        assert rule in body, f"{rule} must be gitignored in {nearest.name}"


def test_the_package_manifest_excludes_generated_datasets():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    for rule in ("prune dataset_candidates", "prune training_gym_datasets",
                 "prune training_gym_exports"):
        assert rule in manifest, f"MANIFEST.in must {rule}"

    from scripts.check_package_manifest import FORBIDDEN_DIRS

    for fragment in ("/dataset_candidates/", "/training_gym_datasets/",
                     "/training_gym_exports/"):
        assert fragment in FORBIDDEN_DIRS
