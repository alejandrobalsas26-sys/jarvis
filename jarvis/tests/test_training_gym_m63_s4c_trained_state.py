"""V69 M63 S4C — candidate 005 trained once, and the evidence that says so.

WHAT THESE TESTS ARE FOR
------------------------
A `TRAINED_UNEVALUATED` claim is the easiest claim in this repository to fake: the
adapter, the run directory and the ledger are all gitignored, so a fresh clone has none
of them, and a control plane resting on them could never be audited anywhere else. The
portable receipt is the part that travels, and these tests are about it being TRUE
rather than merely present.

The failures they exist to prevent:

  * **A receipt about a different run.** It must bind the plan hash the authority named,
    the config the design built, the corpus the design declared, and the adapter that is
    actually on disk.
  * **An inherited artefact.** Candidate 005 must not carry candidate 004's weights
    digest, manifest, artifact-set hash or receipt.
  * **A second axis arriving with the weights.** Training is when a design stops being a
    claim and starts describing real tensors; the LoRA geometry in the adapter must still
    be the reference's.
  * **A replayable authority.** One creation, one consumption, one plan hash, no retry,
    and no token literal anywhere in tracked evidence.
  * **An exam nobody authorised.** A trained candidate that quietly acquired an
    evaluation corpus, an eval receipt or generated tokens has done something else.
  * **A verdict smuggled in as a metric.** Train and validation loss are diagnostic and
    the receipt must say so.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS OR MATERIALISES A TOKEN. The adapter
is read as BYTES and as a safetensors HEADER; no tensor is ever materialised.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import scripts.build_quality_training_config as QCFG
from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT

CANDIDATE_004_ID = "qwen3-06b-lora-quality-live-004"
CANDIDATE_005_ID = "qwen3-06b-lora-quality-live-005"

#: Written independently of the artefacts under test.
RECEIPT = f"state/m62/receipts/{CANDIDATE_005_ID}.train.json"
RECEIPT_004 = f"state/m62/receipts/{CANDIDATE_004_ID}.train.json"
EVIDENCE_DOC = "jarvis/docs/V69_M63_S4C_CANDIDATE005_LIVE_TRAINING.md"

AUTHORIZED_PLAN_HASH = (
    "5a786af98351e14ad231c138549b9db0206a5bd96263b7b0a94b4e625e403423")
TRAIN_CONFIG_HASH = (
    "5e37d615382cd2cc7ab0eabdc6eae2046f9fc778d38afac938f95e476e677703")
ADAPTER_SHA256 = (
    "52d6da26dca20dce93de8845fa08e0b3e452d86472fd6e06d756a30e52688f2a")
ADAPTER_MANIFEST_HASH = (
    "7442246c3d85f1007fe6885714ffbdbe7c53c6bfd251e3c36ca29ab7b489f78f")
ARTIFACT_SET_HASH = (
    "ce5f757cf0cc6d3e998aab8809b45ebb66edefdfb7ecaf0c2811840ca7ac79d9")
TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
RULED_LEARNING_RATE = 2.5e-5

#: Candidate 004's, which candidate 005 must NOT have inherited.
ADAPTER_SHA256_004 = (
    "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67")
PLAN_HASH_004 = (
    "0130fb5aac21ef79a20719c9d3e3d5e15b9f322d758cf6891792d6d2b815d498")

RUN_DIR = REPO / "jarvis" / "training_runs" / "runs" / CANDIDATE_005_ID


@pytest.fixture(scope="module")
def receipt() -> dict:
    return json.loads((REPO / RECEIPT).read_text(encoding="utf-8"))


def _tracked(rel: str) -> bool:
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                          cwd=REPO, capture_output=True).returncode == 0


# ══════════════════════════════════════════════════════════════════════════════
#  1. The receipt exists, travels, and is canonical
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_is_tracked_and_canonical(receipt):
    assert (REPO / RECEIPT).is_file()
    assert not (REPO / RECEIPT).is_symlink()
    assert _tracked(RECEIPT)
    raw = (REPO / RECEIPT).read_bytes()
    assert raw == V.canonical_bytes(receipt)


def test_the_receipt_is_root_independent(receipt):
    """It must mean the same thing in a clone that has no run tree."""
    text = (REPO / RECEIPT).read_text(encoding="utf-8")
    assert not V.PRIVATE_PATH_RE.findall(text)
    assert text.isascii()
    for absolute in ("/home/", "/Users/", "/tmp/", ".venv"):
        assert absolute not in text


def test_the_receipt_identifies_the_right_candidate_and_milestones(receipt):
    assert receipt["candidate_id"] == CANDIDATE_005_ID
    assert receipt["design_milestone"] == "S4B"
    assert receipt["training_milestone"] == "S4C"
    assert receipt["schema_version"] == "m62.train_receipt.1"


def test_the_design_and_training_commits_are_two_different_facts(receipt):
    """D42: collapsing them made truthful post-run sealing impossible once already."""
    assert V.COMMIT_RE.match(receipt["design_commit"])
    assert V.COMMIT_RE.match(receipt["training_source_commit"])
    assert receipt["design_commit"] != receipt["training_source_commit"]


# ══════════════════════════════════════════════════════════════════════════════
#  2. It binds the authorised plan, and nothing else
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_binds_the_authorised_plan(receipt):
    assert receipt["plan_hash"] == AUTHORIZED_PLAN_HASH
    assert receipt["authority"]["bound_plan_hash"] == AUTHORIZED_PLAN_HASH
    assert receipt["ledger"]["plan_hashes"] == [AUTHORIZED_PLAN_HASH]


def test_the_authority_was_created_once_consumed_once_and_is_not_replayable(receipt):
    authority = receipt["authority"]
    assert authority["form"] == "TRAIN:<plan-hash>"
    assert authority["creations"] == 1
    assert authority["consumptions"] == 1
    assert authority["retry_authorized"] is False
    assert authority["token_literal_recorded"] is False


def test_the_ledger_records_exactly_one_run(receipt):
    assert receipt["ledger"]["events"] == {"started": 1, "completed": 1}


def test_no_tracked_evidence_carries_a_spendable_token():
    for rel in (RECEIPT, EVIDENCE_DOC):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert not V.TOKEN_LITERAL_RE.search(text), rel


def test_the_receipt_binds_the_configuration_the_design_built(receipt):
    assert receipt["training_config_hash"] == TRAIN_CONFIG_HASH
    assert receipt["training_config_hash_is_root_bound"] is True


def test_it_is_not_candidate_004s_run(receipt):
    assert receipt["plan_hash"] != PLAN_HASH_004
    assert receipt["adapter"]["sha256"] != ADAPTER_SHA256_004
    other = json.loads((REPO / RECEIPT_004).read_text(encoding="utf-8"))
    for field in ("candidate_id", "plan_hash", "training_config_hash",
                  "design_commit", "training_source_commit", "training_milestone"):
        assert receipt[field] != other[field], field


# ══════════════════════════════════════════════════════════════════════════════
#  3. The artefact, measured from bytes
# ══════════════════════════════════════════════════════════════════════════════
def test_the_recorded_adapter_digest_is_the_bytes_on_disk(receipt):
    """The one assertion a snapshot cannot satisfy by agreeing with itself.

    Skipped rather than faked when the gitignored run tree is absent: the receipt is the
    portable evidence and a clone legitimately has no weights.
    """
    weights = RUN_DIR / "adapter_model.safetensors"
    if not weights.is_file():
        pytest.skip("runtime tree absent; the receipt is the portable evidence")
    assert hashlib.sha256(weights.read_bytes()).hexdigest() == ADAPTER_SHA256
    assert receipt["adapter"]["sha256"] == ADAPTER_SHA256
    assert weights.stat().st_size == receipt["adapter"]["bytes"]


def test_the_adapter_digests_are_the_sealed_ones(receipt):
    adapter = receipt["adapter"]
    assert adapter["sha256"] == ADAPTER_SHA256
    assert adapter["manifest_hash"] == ADAPTER_MANIFEST_HASH
    assert adapter["artifact_set_hash"] == ARTIFACT_SET_HASH
    assert V.FROZEN_ADAPTER_ARTIFACT_SETS[CANDIDATE_005_ID] == ARTIFACT_SET_HASH


def test_the_adapter_is_safetensors_only_and_carries_no_pickle(receipt):
    adapter = receipt["adapter"]
    assert adapter["dtypes"] == ["F32"]
    assert adapter["non_lora_tensors"] == 0
    for name in adapter["file_names"]:
        assert not name.endswith((".bin", ".pt", ".pth", ".pkl")), name
    assert "adapter_model.safetensors" in adapter["file_names"]


def test_the_lora_geometry_is_still_the_references(receipt):
    """Training is when the single-axis claim starts describing real tensors."""
    adapter = receipt["adapter"]
    option = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["005"]]
    reference = QCFG.OPTIONS[QCFG.CANDIDATE_OPTION["004"]]
    assert adapter["lora_rank"] == option["lora_rank"] == reference["lora_rank"] == 16
    assert adapter["lora_alpha"] == option["lora_alpha"] == reference["lora_alpha"] == 32
    assert adapter["lora_dropout"] == option["lora_dropout"] == reference["lora_dropout"]
    assert adapter["lora_alpha"] / adapter["lora_rank"] == 2.0
    assert set(adapter["target_modules"]) == set(V.STRUCTURAL_ADAPTER_TARGET_MODULES)
    assert adapter["lora_a_tensors"] == adapter["lora_b_tensors"] == 196
    assert adapter["lora_tensor_count"] == 392


def test_the_artefact_set_is_clean(receipt):
    verification = receipt["verification"]
    assert verification["adapter_verifier"] == "valid"
    assert verification["adapter_problems"] == []
    assert verification["completed_run_verifier"] == "PASS"
    assert verification["completed_run_problems"] == []
    assert verification["symlinks"] == 0
    assert verification["checkpoint_directories"] == 0
    assert verification["nested_directories"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  4. The run: one, complete, and unmeasured
# ══════════════════════════════════════════════════════════════════════════════
def test_the_run_completed_exactly_as_planned(receipt):
    execution = receipt["execution"]
    assert execution["terminal_status"] == "SUCCESS"
    assert execution["run_state"] == "completed"
    assert execution["completed"] is True
    assert execution["interrupted"] is False
    assert execution["error_category"] == "none"
    assert execution["optimizer_steps_completed"] == \
        execution["optimizer_steps_planned"] == 40
    assert execution["epochs_completed"] == 2.0
    assert execution["converted_records"] == 154
    assert execution["truncated_records"] == 0
    assert execution["seed"] == 42


def test_the_run_generated_nothing_and_spent_no_holdout(receipt):
    holdout = receipt["holdout"]
    assert holdout["evaluation_corpus"] is None
    assert holdout["eval_authority_created"] is False
    assert holdout["held_out_evaluation_runs"] == 0
    assert holdout["model_response_tokens_generated"] == 0
    assert receipt["execution"]["generation_performed"] is False


def test_the_losses_are_diagnostic_and_the_receipt_says_so(receipt):
    """A number that fell is not a verdict, and validation is not eligibility evidence."""
    execution = receipt["execution"]
    assert execution["validation_is_held_out_eligibility_evidence"] is False
    assert execution["validation_contributes_gradients"] is False
    assert execution["early_stopping"] is False
    assert execution["load_best_model_at_end"] is False
    assert execution["validation_rows"] == 12
    assert isinstance(execution["train_loss"], float)
    assert len(execution["validation_losses"]) == execution["validation_evaluations"]


def test_the_receipt_makes_no_eligibility_claim(receipt):
    text = json.dumps(receipt).lower()
    for verdict in ("eligible", "not_eligible", "promoted", "eligibility_verdict",
                    "decide_eligibility", "better", "superior"):
        assert verdict not in text, verdict


# ══════════════════════════════════════════════════════════════════════════════
#  5. Material and runtime, as executed
# ══════════════════════════════════════════════════════════════════════════════
def test_the_corpus_is_train_v2_unchanged(receipt):
    dataset = receipt["training_dataset"]
    assert dataset["dataset_id"] == "m62-defensive-quality-train"
    assert dataset["version"] == "v2"
    assert dataset["manifest_hash"] == TRAIN_V2_MANIFEST
    other = json.loads((REPO / RECEIPT_004).read_text(encoding="utf-8"))
    assert dataset == other["training_dataset"], (
        "candidate 005 trained on material that is not byte-identical to candidate "
        "004's; the corpus is the control, not an axis")


def test_the_base_model_and_tokenizer_are_the_pinned_ones(receipt):
    base = receipt["base_model"]
    assert base["model_id"] == "Qwen/Qwen3-0.6B"
    assert base["revision"] == BASE_MODEL_REVISION
    assert base["tokenizer_revision"] == BASE_MODEL_REVISION
    other = json.loads((REPO / RECEIPT_004).read_text(encoding="utf-8"))
    assert base == other["base_model"], "the base model is the control"


def test_train_eval_render_parity_is_inherited_not_re_decided(receipt):
    """D37 stays fixed: the render identity is candidate 004's, byte for byte."""
    other = json.loads((REPO / RECEIPT_004).read_text(encoding="utf-8"))
    assert receipt["representation"] == other["representation"]
    assert receipt["representation"]["reasoning_policy"] == "disabled"
    assert receipt["representation"]["assistant_only_loss"] is True


def test_the_runtime_is_the_reviewed_backend(receipt):
    runtime = receipt["runtime"]
    assert runtime["package_versions"] == {
        "torch": "2.13.0+cpu", "transformers": "5.14.1", "peft": "0.20.0"}
    assert runtime["device_category"] == "cpu"
    assert runtime["precision"] == "fp32"
    assert runtime["local_files_only"] is True
    assert runtime["trust_remote_code"] is False
    assert runtime["deterministic_reproduction_claimed"] is False


def test_the_backend_versions_match_candidate_004s(receipt):
    """The runtime moved interpreter, not backend. The comparison survives that."""
    other = json.loads((REPO / RECEIPT_004).read_text(encoding="utf-8"))
    assert receipt["runtime"]["package_versions"] == \
        other["runtime"]["package_versions"]


# ══════════════════════════════════════════════════════════════════════════════
#  6. Determinism and the fail-closed guard
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_carries_no_timestamp_and_no_self_digest(receipt):
    """Its identity is its bytes; the snapshot that points at it records the digest."""
    text = json.dumps(receipt)
    assert "receipt_sha256" not in receipt
    assert "created_at" not in receipt
    assert "generated_at" not in receipt
    assert "2026-08-30T" not in text


def test_the_training_milestone_is_recorded_and_not_inherited():
    """The builder refuses a candidate whose milestone nobody recorded."""
    from scripts import build_m62_train_receipt as B

    assert B.TRAINING_MILESTONES["005"] == "S4C"
    assert B.TRAINING_MILESTONES["004"] == "S3V"
    assert B.TRAINING_MILESTONES["005"] != B.TRAINING_MILESTONES["004"]
    assert "006" not in B.TRAINING_MILESTONES


def test_the_evidence_document_is_tracked_and_body_free():
    assert (REPO / EVIDENCE_DOC).is_file()
    assert _tracked(EVIDENCE_DOC)
    text = (REPO / EVIDENCE_DOC).read_text(encoding="utf-8")
    for version, task_ids in V.HELD_OUT_TASK_IDS.items():
        named = sorted({tid for tid in task_ids if tid in text})
        assert not named, f"the document names eval-{version} task(s) {named[:4]}"
    assert not V.PRIVATE_PATH_RE.findall(text)
    assert not V.TOKEN_LITERAL_RE.search(text)


def test_the_evidence_document_preserves_the_standing_conclusions():
    text = (REPO / EVIDENCE_DOC).read_text(encoding="utf-8")
    assert "TOOLING" in text
    assert "TRAINING_EXPERIMENTALLY_ALLOWED_NOT_PROVEN_NECESSARY" in text
    assert "does not create a verdict" in text or "not create a verdict" in text
    assert "HOLD" in text


def test_no_new_development_surface_carries_assistant_attribution():
    vendor = "Cla" + "ude"
    forbidden = (f"Co-Authored-By: {vendor}", f"{vendor}-Session:",
                 f"Generated by {vendor}", "Generated by " + "AI")
    for rel in (EVIDENCE_DOC, RECEIPT, str(Path(__file__).relative_to(REPO))):
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{rel} carries {needle!r}"


# ══════════════════════════════════════════════════════════════════════════════
#  7. Nothing else moved
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate_004s_receipts_are_untouched():
    """Byte-for-byte: adding a fifth candidate rewrites no sealed evidence."""
    code, out = subprocess.run(
        ["git", "diff", "--name-only", "436119b1128827273a4e6f9fbf970d13bee87221",
         "--", "state/m62/receipts/"], cwd=REPO, capture_output=True,
        text=True).returncode, subprocess.run(
        ["git", "diff", "--name-only", "436119b1128827273a4e6f9fbf970d13bee87221",
         "--", "state/m62/receipts/"], cwd=REPO, capture_output=True, text=True).stdout
    assert code == 0
    changed = [line for line in out.splitlines() if line.strip()]
    assert changed == [RECEIPT], (
        f"receipts other than candidate 005's changed since the milestone base: "
        f"{changed}")


def test_no_evaluation_receipt_exists_for_candidate_005():
    assert not (REPO / f"state/m62/receipts/{CANDIDATE_005_ID}.eval.json").exists()
    receipts = sorted(p.name for p in (REPO / "state/m62/receipts").glob("*.json"))
    assert f"{CANDIDATE_005_ID}.eval.json" not in receipts


def test_no_eval_v7_was_created():
    datasets = REPO / "jarvis" / "training_gym_datasets" / "datasets"
    for path in datasets.rglob("*"):
        assert "v7" not in path.name, f"{path} looks like a seventh holdout version"


def test_the_run_ledger_names_candidate_005_exactly_twice():
    """One started, one completed. A third line would be a second run."""
    ledger = REPO / "jarvis" / "training_runs" / "training_runs.jsonl"
    if not ledger.is_file():
        pytest.skip("runtime ledger absent; the receipt is the portable evidence")
    lines = [json.loads(line) for line in
             ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    mine = [r for r in lines if r.get("run_id") == CANDIDATE_005_ID]
    assert len(mine) == 2
    assert sorted(r["event"] for r in mine) == ["completed", "started"]
    assert {r["plan_hash"] for r in mine} == {AUTHORIZED_PLAN_HASH}


def test_no_candidate_006_and_no_second_run_directory():
    runs = REPO / "jarvis" / "training_runs" / "runs"
    if runs.is_dir():
        names = sorted(p.name for p in runs.iterdir() if p.is_dir())
        assert not [n for n in names if n.endswith("-006")]
        assert names.count(CANDIDATE_005_ID) <= 1
        assert not [n for n in names if n.startswith(CANDIDATE_005_ID) and
                    n != CANDIDATE_005_ID]
    assert "006" not in QCFG.CANDIDATES


# ══════════════════════════════════════════════════════════════════════════════
#  8. The control plane at generation 18
# ══════════════════════════════════════════════════════════════════════════════
EXPECTED_GENERATION = 18
S4B_SNAPSHOT = "0017-m63-s4b-candidate005-designed.json"

#: The generation S4C wrote, addressed BY PATH rather than by following the live pointer.
#:
#: RESCOPED AT S5A, whose governance-only generation 19 declared the M64 branch and moved
#: no science. Two assertions below are about what S4C ITSELF recorded -- that it landed at
#: generation 18 chaining to 17, and the branch it declared. Read from the live pointer they
#: also asserted, silently, that no later generation exists, which was true by coincidence
#: until generation 19 was written. Every other assertion in this file is about candidate
#: 005's state, which a later generation genuinely must not move, and deliberately still
#: follows the pointer -- that is exactly what makes them worth running today.
S4C_SNAPSHOT = "0018-m63-s4c-candidate005-trained.json"


@pytest.fixture(scope="module")
def snapshot() -> dict:
    plane = V.load(V.Report())
    assert plane is not None
    return plane.snapshot


def _s4c_snapshot() -> dict:
    stored = json.loads(
        (REPO / V.SNAPSHOT_DIR / S4C_SNAPSHOT).read_text(encoding="utf-8"))
    payload, problems = V.rehydrate_v3(
        stored, V.load_record_store(REPO / V.RECORD_DIR))
    assert not problems, problems
    return payload


def test_the_s4c_generation_is_eighteen_and_chains_to_seventeen():
    """RESCOPED AT S5A: which generation S4C wrote is S4C's own property, not a
    claim that nothing has been written since. The chain is still verified
    forward from here by check_snapshot_chain on every run."""
    s4c = _s4c_snapshot()
    parent = (REPO / V.SNAPSHOT_DIR / S4B_SNAPSHOT).read_bytes()
    assert s4c["state_generation"] == EXPECTED_GENERATION
    assert s4c["parent_snapshot_sha256"] == V.sha256_bytes(parent)
    assert s4c["subject_state_milestone"] == "S4C"
    assert V.load(V.Report()).snapshot["state_generation"] >= EXPECTED_GENERATION


def test_the_whole_verifier_passes_with_no_problems():
    """The one assertion that cannot be satisfied by agreeing with itself."""
    report = V.run()
    assert report.problems == [], " | ".join(m for _, m in report.problems)


def test_candidate_005_is_trained_and_unevaluated(snapshot):
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_005_ID)
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["adapter_sha256"] == ADAPTER_SHA256
    assert entry["adapter_manifest_hash"] == ADAPTER_MANIFEST_HASH
    assert entry["training_receipt"] == RECEIPT
    assert entry["training_corpus"] == "m62-defensive-quality-train v2"
    assert entry["base_model_revision"] == BASE_MODEL_REVISION
    assert entry["evidence"] == EVIDENCE_DOC


def test_a_trained_candidate_names_no_exam(snapshot):
    """The state's whole content is that no held-out material has been spent on it."""
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_005_ID)
    assert entry["evaluation_corpus"] is None
    assert entry["evaluation_receipt"] is None


def test_the_sealed_pair_moved_forward_exactly_once(snapshot):
    assert V.FROZEN_CANDIDATES[CANDIDATE_005_ID] == (
        "TRAINED_UNEVALUATED", ADAPTER_SHA256)
    assert V.FROZEN_CANDIDATES[CANDIDATE_004_ID][1] == ADAPTER_SHA256_004


def test_only_the_fifth_candidate_moved(snapshot):
    """Generations advance one candidate. The other four are byte-identical."""
    stored = json.loads(
        (REPO / V.SNAPSHOT_DIR / S4B_SNAPSHOT).read_text(encoding="utf-8"))
    before, problems = V.rehydrate_v3(
        stored, V.load_record_store(REPO / V.RECORD_DIR))
    assert not problems
    assert before["candidates"][:4] == snapshot["candidates"][:4]
    assert len(snapshot["candidates"]) == len(before["candidates"]) == 5
    for block in ("datasets", "defects", "limitations", "frozen_invariants",
                  "base_model", "policy_identities", "archive",
                  "authority_observation"):
        assert snapshot[block] == before[block], f"{block} moved during a training run"


def test_candidate_004_keeps_its_hold(snapshot):
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == CANDIDATE_004_ID)
    assert entry["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert entry["adapter_sha256"] == ADAPTER_SHA256_004
    assert "HOLD" in json.dumps(snapshot["next_milestone"]) or \
        "HOLD" in snapshot["control_plane_note"] or \
        "HOLD" in " ".join(str(v) for v in snapshot.values() if isinstance(v, str))


def test_the_holdouts_did_not_move(snapshot):
    datasets = {f"{d['dataset_id']} {d['version']}": d for d in snapshot["datasets"]}
    assert datasets["m62-defensive-eval v6"]["status"] == "USED_IMMUTABLE"
    v5 = datasets["m62-defensive-eval v5"]
    assert v5["status"] == "FROZEN_UNUSED"
    assert v5["spent_by"] is None
    assert "m62-defensive-eval v7" not in datasets


def test_the_training_corpus_was_reused_not_re_versioned(snapshot):
    training = [d for d in snapshot["datasets"] if d["role"] == "TRAINING_CORPUS"]
    assert sorted(d["version"] for d in training) == ["v1", "v2"]
    v2 = next(d for d in training if d["version"] == "v2")
    assert v2["manifest_hash"] == TRAIN_V2_MANIFEST
    assert v2["task_count"] == 182


def test_no_authority_is_observed(snapshot):
    observation = snapshot["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for kind in ("train", "eval", "promotion"):
        assert observation[kind] == "NONE_OBSERVED_IN_REPOSITORY"


def test_next_still_bars_everything_it_is_not_superseding(snapshot):
    ruled_out = " | ".join(snapshot["next_milestone"]["ruled_out"])
    for subject in V.REQUIRED_RULED_OUT_SUBJECTS:
        assert subject in ruled_out, subject
    for subject in ("eval-v7", "candidate 006", "second seed", "candidate 005b",
                    "not an infrastructure failure"):
        assert subject in ruled_out, subject
    # The spent authority is recorded where a reader looks for authority, not in the
    # prohibition list, so it is asserted against the whole NEXT block.
    assert "never replayed" in json.dumps(snapshot["next_milestone"])


def test_next_bars_reading_a_training_loss_as_evidence(snapshot):
    ruled_out = " | ".join(snapshot["next_milestone"]["ruled_out"])
    assert "eligibility evidence" in ruled_out
    assert "diagnostic" in ruled_out
    assert "never a verdict" in ruled_out


def test_next_says_there_is_no_holdout_and_asks_for_a_fresh_session(snapshot):
    nxt = snapshot["next_milestone"]
    assert nxt["requires_new_session"] is True
    assert "NONE AVAILABLE" in nxt["evaluation_holdout"]
    assert "HOLDOUT AUTHOR IS NEVER ITS EVALUATOR" in nxt["holdout_access"]
    assert "D35" in nxt["holdout_access"]
    assert "unread" in nxt["holdout_access"]


def test_the_recorded_axis_still_names_the_dial_and_both_ends(snapshot):
    axis = snapshot["next_milestone"]["primary_axis"]
    assert "learning_rate" in axis
    assert QCFG.format_learning_rate(5e-5) in axis
    assert QCFG.format_learning_rate(RULED_LEARNING_RATE) in axis


def test_the_project_block_did_not_move_master(snapshot):
    """The branch is S4C's own declaration (see S4C_SNAPSHOT); master staying
    untouched, unmerged, untagged and unreleased is an invariant every later
    generation inherits, so that half is asserted on the LIVE snapshot."""
    assert _s4c_snapshot()["project"]["branch"] == "jarvis-v69-m63-world-state"
    project = snapshot["project"]
    assert project["master_commit"] == "3705114228edef2f665be349c5c4429b7b16777a"
    assert project["merged_into_master"] is False
    assert project["released"] is False
    assert project["tagged"] is False


def test_the_generation_stays_far_inside_its_budget():
    plane = V.load(V.Report())
    size = len(plane.snapshot_bytes)
    assert size <= V.SNAPSHOT_MAX_BYTES
    assert V.SNAPSHOT_MAX_BYTES - size >= 1024
    assert V.SNAPSHOT_MAX_BYTES == 34_816, "the reviewed budget did not move"
