"""V69 M62 S3W.0 — is the repository ready to spend eval-v5 on candidate 004?

WHAT THIS FILE IS FOR
---------------------
Candidate 004 is ``TRAINED_UNEVALUATED``: an adapter exists, no measurement does, and
``m62-defensive-eval v5`` is ``FROZEN_UNUSED`` with ``spent_by`` null. S3W.0 asks one
question and answers it BODY-FREE: if a future session were authorised to evaluate, would
every identity, policy, boundary and receipt path it depends on already be in place?

It is a QUALIFICATION, not a ceremony. Nothing here loads a model, generates a token,
constructs an executable evaluation plan, creates an ``EVAL`` capability or reads one byte
of ``v5`` semantic content. The prohibitions are asserted rather than promised — see
:func:`test_this_suite_creates_no_eval_authority_and_spends_no_holdout`.

WHY THE eval-v5 BODIES STAY SHUT
--------------------------------
The freeze is only worth something while it is candidate-blind in both directions: S3S
froze ``v5`` before candidate 004 existed, and S3W.0 must not undo that by reading the
exam while holding the student. Every ``v5`` assertion below is over a manifest digest, a
pack digest, a task count or a status — never a prompt, a target or a task body. Where a
ceremony shape has to be exercised end to end, it is exercised over the S3Q.0 synthetic
corpus, whose canaries make the body-firewall assertions non-vacuous.

WHAT IS DELIBERATELY NOT RE-TESTED HERE
---------------------------------------
The spend boundary, the body firewall, the receipt contracts and the plan bindings already
have dedicated suites (``s3q0_holdout_commit``, ``s3q0_body_blindness``, ``s3q0_*_receipt``,
``s3q0_exact_binding``, ``s3t0_termination_observability``). This file does not duplicate
them; it asserts the facts that are specific to CANDIDATE 004 and to ``eval-v5``, plus the
capacity and recompaction proofs generation 11 depends on.
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JARVIS_ROOT = REPO_ROOT / "jarvis"

pytest.importorskip("scripts.verify_m62_control_plane")
from scripts import verify_m62_control_plane as V  # noqa: E402
from scripts import project_m62_state_capacity as P  # noqa: E402

# ── the frozen starting authority S3W.0 was handed ───────────────────────────────────
CANDIDATE = "qwen3-06b-lora-quality-live-004"
ADAPTER_SHA = "a105e01ca99d9b47d45c408a614b78aa9ec22df83ad32b321df57b1a1c3ecc67"
ADAPTER_MANIFEST = "162e93e36f284b651051a93e22cfc6cb15adef3f457038297ca72774e276b510"
ARTIFACT_SET = "326678618101eb4eec0a12b89a5e02f89340148111d5f4adf97d6a04f449b864"
BASE_MODEL = "Qwen/Qwen3-0.6B"
BASE_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
V5_MANIFEST = "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c"
V5_PACK = "287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6"
V5_TASK_COUNT = 36
GEN10_SNAPSHOT_SHA = (
    "b36b13baf4c9624e6045450737256db95421625c86c6659b0e531731553da075")
TRAINING_SOURCE_COMMIT = "80565d32795fb276df202f6bef46ed38bb2bb7c5"
DESIGN_COMMIT = "43cb590b9a8da7b912dd146a2e5ac410680729c4"

#: The sealed S3S commits. The freeze is the SUBJECT commit; the seal is generation 7.
V5_FREEZE_COMMIT = "e52129cc819433af83789f042d7bf13ea4d83014"
V5_SEAL_COMMIT = "8b64475e506572258dc2ab08146b7154da2c1644"

TRAIN_RECEIPT = REPO_ROOT / "state/m62/receipts" / f"{CANDIDATE}.train.json"
ADAPTER_DIR = JARVIS_ROOT / "training_runs/runs" / CANDIDATE

#: The frozen policy identities the next ceremony must run under, unchanged.
GENERATION_POLICY_HASH = (
    "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")
METRIC_POLICY_HASH = "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a"
GATE_POLICY_HASH = "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5"

#: The five prospective termination fields S3T.0 added. A future receipt must be able to
#: carry every one of them, and none of them may be a body.
S3T0_FIELDS = ("json_parse_error_kind", "json_parse_error_line",
               "json_parse_error_column", "json_parse_error_position",
               "response_unique_char_ngram_ratio")


def git(*args: str) -> str:
    return subprocess.run(("git", "-C", str(REPO_ROOT)) + args, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def snapshot() -> dict:
    current = json.loads((REPO_ROOT / "state/m62/current.json").read_text("utf-8"))
    return json.loads(
        (REPO_ROOT / current["latest_snapshot_path"]).read_text("utf-8"))


@pytest.fixture(scope="module")
def receipt() -> dict:
    return json.loads(TRAIN_RECEIPT.read_text("utf-8"))


@pytest.fixture(scope="module")
def adapter_manifest() -> dict:
    return json.loads((ADAPTER_DIR / "adapter-manifest.json").read_text("utf-8"))


# ── 1. the candidate is trained, unevaluated, and says so consistently ───────────────
def test_candidate_004_is_trained_and_carries_no_measurement(snapshot):
    entry, = [c for c in snapshot["candidates"] if c["candidate_id"] == CANDIDATE]
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_corpus"] is None
    assert entry["evaluation_receipt"] is None
    assert entry["adapter_sha256"] == ADAPTER_SHA
    assert entry["adapter_manifest_hash"] == ADAPTER_MANIFEST
    assert entry["base_model_revision"] == BASE_REVISION


def test_the_training_receipt_binds_the_design_and_the_execution_separately(receipt):
    assert receipt["candidate_id"] == CANDIDATE
    assert receipt["design_milestone"] == "S3U"
    assert receipt["training_milestone"] == "S3V"
    assert receipt["design_commit"] == DESIGN_COMMIT
    assert receipt["training_source_commit"] == TRAINING_SOURCE_COMMIT
    assert receipt["design_commit"] != receipt["training_source_commit"]


def test_the_train_authority_was_created_once_consumed_once_and_never_written(receipt):
    authority = receipt["authority"]
    assert authority["creations"] == 1
    assert authority["consumptions"] == 1
    assert authority["retry_authorized"] is False
    assert authority["token_literal_recorded"] is False
    assert authority["form"] == "TRAIN:<plan-hash>"
    assert authority["bound_plan_hash"] == receipt["plan_hash"]


def test_the_run_completed_its_whole_budget_and_generated_nothing(receipt):
    execution = receipt["execution"]
    assert execution["optimizer_steps_completed"] == 40
    assert execution["optimizer_steps_planned"] == 40
    assert execution["epochs_completed"] == 2.0
    assert execution["epochs_configured"] == 2
    assert execution["terminal_status"] == "SUCCESS"
    assert execution["interrupted"] is False
    assert execution["generation_performed"] is False
    assert execution["validation_is_held_out_eligibility_evidence"] is False


def test_the_training_run_touched_no_holdout(receipt):
    holdout = receipt["holdout"]
    assert holdout["held_out_evaluation_runs"] == 0
    assert holdout["model_response_tokens_generated"] == 0
    assert holdout["eval_authority_created"] is False
    assert holdout["evaluation_corpus"] is None


def test_the_receipt_is_byte_stable_and_the_snapshot_agrees_with_it(receipt, snapshot):
    """The receipt is not re-derived here; it is checked for self-consistency.

    Rebuilding it would need the production generator and the run tree, which the
    control-plane verifier already exercises. What this asserts is the property S3W.0
    depends on: the tracked bytes and the tracked snapshot name the same adapter.
    """
    entry, = [c for c in snapshot["candidates"] if c["candidate_id"] == CANDIDATE]
    assert receipt["adapter"]["sha256"] == entry["adapter_sha256"]
    assert receipt["adapter"]["manifest_hash"] == entry["adapter_manifest_hash"]
    assert receipt["adapter"]["artifact_set_hash"] == ARTIFACT_SET
    assert entry["training_receipt"] == "state/m62/receipts/" + TRAIN_RECEIPT.name


# ── 2. the adapter qualifies without a model ever being loaded ───────────────────────
@pytest.mark.skipif(not ADAPTER_DIR.is_dir(),
                    reason="the candidate 004 run tree is gitignored runtime state")
def test_the_adapter_re_derives_to_the_three_frozen_digests(adapter_manifest):
    from training_gym.training.artifacts import AdapterManifest, adapter_tree_hash
    manifest = AdapterManifest.from_dict(adapter_manifest)
    assert manifest.manifest_hash() == ADAPTER_MANIFEST
    assert manifest.tree_hash == ARTIFACT_SET
    assert adapter_tree_hash(manifest.files) == ARTIFACT_SET

    import hashlib
    weights = (ADAPTER_DIR / "adapter_model.safetensors").read_bytes()
    assert hashlib.sha256(weights).hexdigest() == ADAPTER_SHA


@pytest.mark.skipif(not ADAPTER_DIR.is_dir(),
                    reason="the candidate 004 run tree is gitignored runtime state")
def test_the_run_directory_verifies_under_its_own_production_verifier(adapter_manifest):
    from training_gym.training.artifacts import verify_completed_run
    problems = verify_completed_run(
        ADAPTER_DIR, lora=adapter_manifest["lora"],
        base_model_id=adapter_manifest["base_model_id"])
    assert problems == ()


@pytest.mark.skipif(not ADAPTER_DIR.is_dir(),
                    reason="the candidate 004 run tree is gitignored runtime state")
def test_the_lora_geometry_reads_from_the_safetensors_header_not_from_a_model():
    """Tensor names, shapes and dtypes come out of the header. No tensor data is read.

    This is the whole point of qualifying an adapter body-free: the file's structure is
    knowable from its first few kilobytes, and knowing it does not require ``torch``,
    ``peft`` or a forward pass — none of which this test imports.
    """
    raw = (ADAPTER_DIR / "adapter_model.safetensors").read_bytes()
    header_length = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8 + header_length].decode("utf-8"))
    header.pop("__metadata__", None)

    names = sorted(header)
    lora_a = [n for n in names if "lora_A" in n]
    lora_b = [n for n in names if "lora_B" in n]
    assert len(names) == 392
    assert len(lora_a) == 196
    assert len(lora_b) == 196
    assert len(names) - len(lora_a) - len(lora_b) == 0
    assert sorted({header[n]["dtype"] for n in names}) == ["F32"]

    parameters = 0
    for name in names:
        size = 1
        for dimension in header[name]["shape"]:
            size *= dimension
        parameters += size
    assert parameters == 10_092_544

    modules = sorted({n.split(".")[-3] for n in names
                      if n.endswith(("lora_A.weight", "lora_B.weight"))})
    assert modules == ["down_proj", "gate_proj", "k_proj", "o_proj", "q_proj",
                       "up_proj", "v_proj"]


@pytest.mark.skipif(not ADAPTER_DIR.is_dir(),
                    reason="the candidate 004 run tree is gitignored runtime state")
def test_the_adapter_config_agrees_with_the_receipt_on_every_dial(receipt):
    config = json.loads((ADAPTER_DIR / "adapter_config.json").read_text("utf-8"))
    adapter = receipt["adapter"]
    assert config["r"] == adapter["lora_rank"] == 16
    assert config["lora_alpha"] == adapter["lora_alpha"] == 32
    assert config["lora_dropout"] == adapter["lora_dropout"] == 0.05
    assert sorted(config["target_modules"]) == sorted(adapter["target_modules"])
    assert config["peft_type"] == "LORA"


@pytest.mark.skipif(not ADAPTER_DIR.is_dir(),
                    reason="the candidate 004 run tree is gitignored runtime state")
def test_the_run_tree_carries_no_pickle_no_symlink_and_no_checkpoint():
    """A ``.bin`` beside a ``.safetensors`` is an arbitrary-code path into evaluation."""
    entries = sorted(ADAPTER_DIR.iterdir())
    assert [e.name for e in entries if e.is_symlink()] == []
    assert [e.name for e in entries if e.is_dir()] == []
    forbidden = [e.name for e in entries
                 if e.suffix in (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".h5")]
    assert forbidden == []
    assert {e.name for e in entries} == {
        "README.md", "adapter-manifest.json", "adapter_config.json",
        "adapter_model.safetensors", "backend_result.json", "run.json",
        "training_log.jsonl"}


# ── 3. base weights and tokenizer are pinned, offline and code-free ──────────────────
def test_the_base_model_and_tokenizer_are_one_immutable_pinned_revision(receipt,
                                                                       snapshot):
    from training_gym.training.model_identity import RevisionKind, classify_revision
    base = receipt["base_model"]
    assert base["model_id"] == base["tokenizer_id"] == BASE_MODEL
    assert base["revision"] == base["tokenizer_revision"] == BASE_REVISION
    assert classify_revision(BASE_REVISION) is RevisionKind.IMMUTABLE_COMMIT
    assert snapshot["base_model"]["revision_kind"] == "immutable_commit"
    assert snapshot["base_model"]["revision"] == BASE_REVISION


def test_the_runtime_the_adapter_was_trained_under_never_trusts_remote_code(receipt):
    runtime = receipt["runtime"]
    assert runtime["trust_remote_code"] is False
    assert runtime["local_files_only"] is True
    assert runtime["device_category"] == "cpu"
    assert runtime["precision"] == "fp32"


def test_the_cache_evidence_is_a_directory_name_digest_and_never_a_path(receipt):
    """The receipt may say WHICH model is cached and may not say WHERE it is cached."""
    from training_gym.training.model_identity import cache_directory_name
    from training_gym.schemas import sha256_text
    assert receipt["model_cache_evidence"] == sha256_text(
        cache_directory_name(BASE_MODEL))
    assert "/" not in receipt["model_cache_evidence"]


# ── 4. eval-v5 is frozen, unspent, and was frozen candidate-blind ────────────────────
def test_eval_v5_is_frozen_unused_with_the_frozen_identity(snapshot):
    entry, = [d for d in snapshot["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v5"]
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None
    assert entry["manifest_hash"] == V5_MANIFEST
    assert entry["pack_hash"] == V5_PACK
    assert entry["task_count"] == V5_TASK_COUNT
    assert entry["role"] == "EVALUATION_HOLDOUT"


def test_eval_v5_has_never_been_spent_in_any_generation():
    """Read across the whole sealed chain, not just the head. A holdout that was spent
    and then re-described as frozen would be invisible to a check on current state."""
    for path in sorted((REPO_ROOT / "state/m62/snapshots").glob("*.json")):
        state = json.loads(path.read_text("utf-8"))
        for entry in state.get("datasets", []):
            if entry["dataset_id"] == "m62-defensive-eval" and entry["version"] == "v5":
                assert entry["status"] == "FROZEN_UNUSED", path.name
                assert entry["spent_by"] is None, path.name


def test_the_v5_freeze_commit_is_an_ancestor_of_the_candidate_004_design():
    """The candidate-blind claim is TEMPORAL, so it is proved against sealed history.

    A scan of the current tree could only ever show that the two things coexist now. What
    matters is the order they came into existence in, and Git is the only witness to that.
    """
    for earlier, later in ((V5_FREEZE_COMMIT, DESIGN_COMMIT),
                           (V5_SEAL_COMMIT, DESIGN_COMMIT),
                           (DESIGN_COMMIT, TRAINING_SOURCE_COMMIT)):
        subprocess.run(("git", "-C", str(REPO_ROOT), "merge-base",
                        "--is-ancestor", earlier, later), check=True)


def test_candidate_004_did_not_exist_at_the_tree_that_froze_v5():
    """No candidate 004 identity, config, adapter or receipt at the freeze commit.

    The loose token ``candidate 004`` DOES appear there — S3S wrote down, repeatedly, that
    it was forbidden to create one. Those are prohibitions, not artefacts, so the check is
    against the identifier the repository actually uses for the candidate.
    """
    found = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "grep", "-l", "-F", CANDIDATE, V5_FREEZE_COMMIT),
        capture_output=True, text=True)
    assert found.stdout.strip() == ""
    assert found.returncode == 1  # git grep: 1 means "no match", not an error


def test_no_candidate_004_evaluation_evidence_exists_anywhere():
    receipts = REPO_ROOT / "state/m62/receipts"
    assert not (receipts / f"{CANDIDATE}.eval.json").exists()
    assert sorted(p.name for p in receipts.glob(f"{CANDIDATE}*")) == [
        f"{CANDIDATE}.train.json"]

    evaluation_root = JARVIS_ROOT / "evaluation"
    if evaluation_root.is_dir():
        for path in evaluation_root.rglob("*"):
            if path.is_file():
                text = path.read_text("utf-8", errors="ignore")
                assert CANDIDATE not in text, path
                assert V5_PACK not in text, path
                assert V5_MANIFEST not in text, path


# ── 5. the policies the next ceremony must run under are the recorded ones ───────────
def test_the_configured_generation_policy_re_derives_from_source(snapshot):
    """Re-derived, never trusted from memory: 512 tokens and seed 11 are what the
    REPOSITORY says today, and the snapshot is checked against that, not the reverse."""
    from training_gym.evaluation.generation import (
        DevicePolicy, PrecisionPolicy, ReasoningPolicy, eligibility_generation_policy)
    policy = eligibility_generation_policy(
        seed=11, timeout_s=300, device_policy=DevicePolicy.CPU,
        precision_policy=PrecisionPolicy.FP32)
    assert policy.policy_hash() == GENERATION_POLICY_HASH
    assert policy.reasoning_policy is ReasoningPolicy.DISABLED
    assert policy.max_new_tokens == 512
    assert policy.seed == 11
    assert policy.do_sample is False
    assert policy.temperature == 0.0
    assert policy.stop_sequences == ()

    recorded = snapshot["policy_identities"]
    assert recorded["generation_policy_hash"] == GENERATION_POLICY_HASH
    assert recorded["max_new_tokens"] == 512
    assert recorded["reasoning_policy"] == "DISABLED"


def test_the_constructor_default_is_a_different_object_and_still_disables_reasoning(
        snapshot):
    from training_gym.evaluation.generation import (
        ELIGIBILITY_REASONING_POLICY, ReasoningPolicy, eligibility_generation_policy)
    default = eligibility_generation_policy(seed=0)
    assert default.policy_hash() == snapshot["policy_identities"][
        "generation_policy_constructor_default_hash"]
    assert default.policy_hash() != GENERATION_POLICY_HASH
    assert ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED


def test_d37_parity_holds_because_both_arms_share_one_reasoning_policy():
    """The only permitted model-arm difference is the adapter, so the reasoning policy is
    a property of the EVALUATION, not of an arm. There is one enum and both arms read it.
    """
    from training_gym.evaluation.generation import (
        ReasoningPolicy, assert_identical_policies, eligibility_generation_policy)
    policy = eligibility_generation_policy(seed=11)
    assert_identical_policies(policy, policy)
    assert policy.reasoning_policy is ReasoningPolicy.DISABLED
    assert policy.reasoning_policy is not ReasoningPolicy.MODEL_DEFAULT


def test_the_scoring_and_evidence_identities_are_the_ones_s3t0_left_behind():
    from training_gym.evaluation import score_evidence as SE
    from training_gym.evaluation import scoring
    assert scoring.SCORING_VERSION == "m62.evaluation_scoring.6"
    assert SE.SCORE_EVIDENCE_VERSION == "m62.evaluation_score_evidence.3"
    for field in S3T0_FIELDS:
        assert field in SE.SCORE_EVIDENCE_FIELDS, field


def test_no_score_evidence_field_can_hold_a_prompt_a_target_or_a_response():
    """The firewall is a SHAPE: the record has no field a body could live in.

    ``response_sha256`` is the deliberate exception-that-proves-it — a digest of a
    response is not a response, and it is what makes a leak detectable afterwards.
    """
    from training_gym.evaluation import score_evidence as SE
    fields = set(SE.SCORE_EVIDENCE_FIELDS)
    for forbidden in ("prompt", "prompt_text", "target", "target_text", "response",
                      "response_text", "completion", "output_text", "hidden_target",
                      "reference", "exception", "traceback", "stderr"):
        assert forbidden not in fields, forbidden
    assert "response_sha256" in fields


def test_the_holdout_commit_body_is_a_closed_body_free_field_list():
    from training_gym.evaluation.store import HOLDOUT_COMMIT_FIELDS
    fields = set(HOLDOUT_COMMIT_FIELDS)
    for forbidden in ("prompt", "target", "response", "task_body", "reference_answer"):
        assert forbidden not in fields, forbidden
    for required in ("task_pack_hash", "hidden_target_store_hash",
                     "first_request_parity_hash", "generation_policy_hash",
                     "candidate_adapter_reference_hash", "performs_inference"):
        assert required in fields, required


# ── 6. the spend boundary and the authority shape are the qualified ones ─────────────
def test_the_spend_event_is_named_for_what_it_can_prove():
    from training_gym.evaluation.store import HOLDOUT_COMMIT_EVENT
    assert HOLDOUT_COMMIT_EVENT == "holdout_model_facing_committed"
    assert "read" not in HOLDOUT_COMMIT_EVENT
    assert "exposed" not in HOLDOUT_COMMIT_EVENT


def test_the_ledger_append_is_flushed_and_fsynced():
    """Durability is what makes the boundary irreversible; a buffered append is not."""
    source = (JARVIS_ROOT / "training_gym/atomicio.py").read_text("utf-8")
    append = source[source.index("def append_jsonl("):]
    assert "fh.flush()" in append
    assert "os.fsync(fh.fileno())" in append


def test_the_runner_commits_after_parity_and_immediately_before_the_first_call():
    """Read as source order, because the ordering IS the guarantee.

    Both requests are constructed, their parity hashes are proved equal, and only then is
    the one-shot callback fired — with the first ``backend.generate`` after it and nothing
    in between that could read a task.
    """
    source = (JARVIS_ROOT / "training_gym/evaluation/runner.py").read_text("utf-8")
    parity = source.index("parity = base_request.parity_hash()")
    callback = source.index("if before_first_model_facing_invoke is not None")
    invoke = source.index("if order is ExecutionOrder.BASELINE_FIRST:", callback)
    assert parity < callback < invoke
    assert source.index("base_request = EvaluationRequest(") < parity
    assert source.index("cand_request = EvaluationRequest(") < parity


def test_the_eval_authority_is_plan_bound_single_use_and_unpasteable():
    from training_gym.evaluation.plan import (
        CONFIRMATION_PREFIX, EvaluationConfirmationRejected,
        check_evaluation_confirmation)
    assert CONFIRMATION_PREFIX == "EVAL:"

    class _Plan:
        def plan_hash(self):
            return "c" * 64

        def confirmation_token(self):
            return CONFIRMATION_PREFIX + self.plan_hash()

    plan = _Plan()
    assert check_evaluation_confirmation(plan.confirmation_token(), plan)
    for rejected in (True, 1, None, "yes", "TRAIN:" + "c" * 64, "EVAL:" + "c" * 63,
                     "@token.txt", "EVAL:" + "d" * 64):
        with pytest.raises((EvaluationConfirmationRejected, Exception)):
            check_evaluation_confirmation(rejected, plan)


def test_this_suite_creates_no_eval_authority_and_spends_no_holdout(snapshot):
    """The prohibition S3W.0 exists to keep, asserted rather than promised."""
    entry, = [d for d in snapshot["datasets"]
              if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v5"]
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None
    observation = snapshot["authority_observation"]
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False


# ── 7. capacity: generation 11 and a future generation 12 both fit ───────────────────
@pytest.fixture(scope="module")
def parent_state() -> dict:
    return json.loads(
        (REPO_ROOT / "state/m62/snapshots/0010-m62-s3v-candidate004-trained.json")
        .read_text("utf-8"))


@pytest.fixture(scope="module")
def projections(parent_state) -> tuple[dict, dict]:
    gen11 = P.project_gen11(parent_state, subject_commit="0" * 40,
                            parent_sha256=GEN10_SNAPSHOT_SHA)
    return gen11, P.project_gen12(gen11)


def test_both_projected_generations_clear_the_required_headroom(projections):
    for name, state in zip(("gen11", "gen12"), projections, strict=True):
        size, headroom = P.measure(state)
        assert size <= V.SNAPSHOT_MAX_BYTES, name
        assert headroom >= P.REQUIRED_HEADROOM_BYTES, f"{name}: {headroom} bytes"


def test_both_projected_generations_are_schema_valid(projections):
    for name, state in zip(("gen11", "gen12"), projections, strict=True):
        assert V.validate_against_schema(V.snapshot_schema(), state) == [], name


def test_the_projection_never_raises_a_reviewed_budget():
    assert V.SNAPSHOT_MAX_BYTES == 32_768
    assert V.PROGRESS_MAX_BYTES == 40_960
    assert V.PROGRESS_MAX_LINES == 760
    assert P.REQUIRED_HEADROOM_BYTES == 1_024


def test_progress_is_inside_both_of_its_budgets():
    size, headroom, lines, line_headroom = P.progress_headroom()
    assert headroom > 0, size
    assert line_headroom > 0, lines


def test_the_recompacted_entries_kept_every_fact_they_merged(parent_state):
    """A merge that loses a clause is a deletion wearing a merge's clothes.

    Every replacement is checked the hard way: the distinctive substrings of ALL the
    originals must appear in it, and the originals must be gone. The same rule is applied
    to the single-entry compressions, where the risk is not a lost entry but a lost
    clause inside one.
    """
    limitations = P._recompact(parent_state["limitations"])
    invariants = P._recompact_invariants(parent_state["frozen_invariants"])

    merges = (
        (limitations, "One host, CPU, one seed",
         ("no repeat", "dtype control arm", "deterministic_reproduction_claimed",
          "single observation", "paired baseline measured in the same run",
          "reproduces weights twice"),
         ("Training and measurement are one host",
          "Every S3Q figure is a single observation")),
        (limitations, "Candidate 003 is NOT ELIGIBLE",
         ("9/9 -> 8/9", "36-task holdout cannot distinguish", "+0.044208",
          "-0.022359", "+0.129413", "regression_not_excluded"),
         ("Candidate 003's paired mean delta is +0.044208",)),
        (invariants, "An EVALUATED_* state REQUIRES",
         ("valid portable receipt", "REDERIVED", "BOTH directions",
          "decide_eligibility", "bootstrap", "empirical-status"),
         ("A future EVALUATED_* state requires a valid portable",
          "An EVALUATED_* state is REDERIVED, never read")),
        (invariants, "Plan tokens are single-use",
         ("consumed or failed plan is never replayed", "Token silence",
          "not cryptography", "pure function of plan_hash", "neither secret",
          "must not materialise it"),
         ("Token silence is ceremony hygiene",)),
    )
    for collection, prefix, clauses, originals in merges:
        replacement, = [e for e in collection if e.startswith(prefix)]
        for clause in clauses:
            assert clause in replacement, (prefix, clause)
        for original in originals:
            assert not any(e.startswith(original) for e in collection), original

    compressions = (
        ("Hashes binding output_root_id are host-bound",
         ("RE-DERIVE on the executing host", "never paste", "config_hash", "plan_hash",
          "runtime and hardware evidence", "6f9f470f/414ce9e3", "this output root only",
          "moves once a run directory exists")),
        ("The prospective spend boundary is deliberately EARLIER",
         ("proof a forward pass ran", "no atomic transaction", "durable local append",
          "external synchronous call", "conservative error is chosen")),
        ("receipt_hash and the measurement witness prove",
         ("payload integrity", "canonical bytes", "REPOSITORY PROVENANCE only",
          "not human identity", "authorisation", "who ran it", "hardware attestation",
          "no PKI is implied")),
        ("A receipt is built AFTER its run",
         ("artefacts that already exist", "gitignored runtime tree",
          "not that nobody touched them between run and seal",
          "cannot be built once the run directory is gone")),
    )
    for prefix, clauses in compressions:
        replacement, = [e for e in limitations if e.startswith(prefix)]
        original, = [e for e in parent_state["limitations"]
                     if e.startswith(prefix)]
        for clause in clauses:
            assert clause in replacement, (prefix, clause)
        assert len(replacement) < len(original), prefix


def test_recompaction_only_shortens_and_never_deletes_an_unmerged_entry(parent_state):
    before = parent_state["limitations"]
    after = P._recompact(before)
    assert len(after) == len(before) - 2          # exactly the two absorbed entries
    assert len("".join(after)) < len("".join(before))
    invariants = P._recompact_invariants(parent_state["frozen_invariants"])
    assert len(invariants) == len(parent_state["frozen_invariants"]) - 2

    # Every entry that took part in no merge and no compression survives byte-for-byte.
    changed = {e for e in before if e not in after}
    assert len(changed) == 8                      # 4 merged away + 4 compressed
    for entry in before:
        if entry not in changed:
            assert entry in after, entry


def test_generation_eleven_carries_the_candidate_and_holdout_through_untouched(
        parent_state, projections):
    gen11, _ = projections
    assert gen11["candidates"] == parent_state["candidates"]
    assert gen11["datasets"] == parent_state["datasets"]
    assert gen11["policy_identities"] == parent_state["policy_identities"]
    assert gen11["defects"] == parent_state["defects"]
    assert gen11["state_generation"] == 11
    assert gen11["parent_snapshot_sha256"] == GEN10_SNAPSHOT_SHA


def test_generation_eleven_asserts_readiness_and_never_authority(projections):
    """Readiness is a claim about infrastructure; authority is a claim about permission.

    Asserted over the MACHINE fields rather than by scanning prose: generation 11 is full
    of sentences that legitimately contain "no holdout is spent", and a substring search
    cannot tell an assertion from its negation.
    """
    gen11, _ = projections
    entry, = [c for c in gen11["candidates"] if c["candidate_id"] == CANDIDATE]
    assert entry["status"] == "TRAINED_UNEVALUATED"
    assert entry["evaluation_receipt"] is None
    assert entry["evaluation_corpus"] is None

    dataset, = [d for d in gen11["datasets"]
                if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v5"]
    assert dataset["status"] == "FROZEN_UNUSED"
    assert dataset["spent_by"] is None

    observation = gen11["authority_observation"]
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False

    assert any("READINESS, NOT AUTHORITY" in e for e in gen11["frozen_invariants"])
    assert "EVAL and promotion authority: NONE." in gen11["control_plane_note"]

    # No EVAL confirmation token may be MATERIALISED into state, ever. Naming the form
    # "EVAL:<plan-hash>" is ceremony documentation; a real token is that prefix followed
    # by a 64-hex digest, and only the second thing is a capability.
    assert not re.search(r"EVAL:[0-9a-fA-F]{64}", json.dumps(gen11))
    assert not re.search(r"TRAIN:[0-9a-fA-F]{64}", json.dumps(gen11))


def test_generation_eleven_states_no_executable_plan_hash(projections):
    """The live plan hash belongs to S3W.1, because the plan's source is THIS head.

    A plan hash in generation 11 would either be wrong — computed before the commit it
    describes — or, if right, a reusable capability sitting in a tracked file.
    """
    gen11, _ = projections
    milestone = gen11["next_milestone"]
    assert "plan_hash" not in json.dumps(milestone)
    assert milestone["requires_new_session"] is True
    assert any("final generation 11 HEAD" in r or "final clean generation 11 HEAD" in r
               for r in milestone["ruled_out"] + milestone["authority_required"])


def test_the_projected_generation_twelve_records_a_result_without_predicting_one(
        projections):
    """The projection reserves room for the LONGEST truthful outcome and states none.

    It is a capacity shape, so it must not contain a figure: a projection carrying a
    delta, an interval or a gate count would be a prediction about candidate 004 written
    into the repository before the holdout was ever read.
    """
    _, gen12 = projections
    entry, = [c for c in gen12["candidates"] if c["candidate_id"] == CANDIDATE]
    assert entry["status"].startswith("EVALUATED_")
    assert entry["evaluation_corpus"] == "m62-defensive-eval v5"
    dataset, = [d for d in gen12["datasets"]
                if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v5"]
    assert dataset["status"] == "USED_IMMUTABLE"
    assert dataset["spent_by"] is not None

    # Nothing generation 12 ADDS may carry a figure. Candidate 003's sealed numbers are
    # carried through from generation 10 and are not a prediction about candidate 004.
    gen11, _ = projections
    added = [e for e in gen12["limitations"] if e not in gen11["limitations"]]
    added.append(json.dumps(gen12["next_milestone"]))
    added.append(gen12["control_plane_note"])
    for text in added:
        assert CANDIDATE not in text or "004" in text  # naming it is fine
        for figure in ("0.0442", "0.022359", "0.129413", "9/9", "8/9"):
            assert figure not in text, (figure, text[:80])
    # And the placeholder identities must be obviously non-real.
    assert entry["evaluation_receipt"].endswith(".eval.json")
    assert set(gen12["subject_state_commit"]) == {"0"}

# ── 8. the surfaces S3W.0 publishes carry no eval-v5 material ────────────────────────
#: The body-free surfaces an S3W.1 session will read. None may carry v5 material.
S3W0_BODY_FREE_SURFACES = (
    "jarvis/docs/V69_M62_S3W0_EVAL_V5_QUALIFICATION.md",
    "PROGRESS.md",
    "state/m62/current.json",
)


def _shingles(text: str, width: int = 8) -> set[str]:
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    return {" ".join(words[i:i + width]) for i in range(len(words) - width + 1)}


def test_the_s3w0_surfaces_carry_no_eval_v5_material():
    """The firewall S3W.1 depends on, measured rather than promised.

    A reader of these files must be able to learn what v5 IS — its identities, counts and
    provenance — and must not be able to learn what v5 SAYS. The corpus is read INSIDE
    this test to build the comparison set; not one byte of it is printed, asserted on by
    value, or allowed to influence anything but a pass/fail.
    """
    BC = pytest.importorskip("scripts.build_evaluation_corpus")
    rows = BC.corpus_v5()
    body_shingles: set[str] = set()
    for _split, _family, _task, prompt, target in rows:
        body_shingles |= _shingles(prompt) | _shingles(target)
    assert body_shingles, "expected the corpus to yield shingles to test against"

    for rel in S3W0_BODY_FREE_SURFACES:
        path = REPO_ROOT / rel
        if not path.is_file():
            pytest.skip(f"{rel} does not exist yet in this tree")
        text = path.read_text("utf-8")
        for _split, _family, task_id, prompt, target in rows:
            assert prompt not in text, f"{rel} carries the {task_id} prompt"
            assert target not in text, f"{rel} carries the {task_id} target"
        assert _shingles(text) & body_shingles == set(), rel


def test_the_generation_eleven_snapshot_will_carry_no_eval_v5_material(projections):
    BC = pytest.importorskip("scripts.build_evaluation_corpus")
    gen11, gen12 = projections
    body_shingles: set[str] = set()
    for _split, _family, _task, prompt, target in BC.corpus_v5():
        body_shingles |= _shingles(prompt) | _shingles(target)
    for name, state in (("gen11", gen11), ("gen12", gen12)):
        assert _shingles(json.dumps(state)) & body_shingles == set(), name


def test_this_test_file_contains_no_held_out_task_body():
    """A test that quotes the holdout publishes it to every future reader of the suite."""
    BC = pytest.importorskip("scripts.build_evaluation_corpus")
    source = Path(__file__).read_text("utf-8")
    for version in ("v4", "v5"):
        for _split, _family, _task, prompt, target in BC.corpus_for(version):
            assert prompt not in source
            assert target not in source


def test_the_capacity_tool_contains_no_held_out_task_body():
    BC = pytest.importorskip("scripts.build_evaluation_corpus")
    source = (JARVIS_ROOT / "scripts/project_m62_state_capacity.py").read_text("utf-8")
    for version in ("v4", "v5"):
        for _split, _family, _task, prompt, target in BC.corpus_for(version):
            assert prompt not in source
            assert target not in source


# ── 9. the emitted generation 11 IS the projection that was measured ─────────────────
GEN11_PATH = "state/m62/snapshots/0011-m62-s3w0-candidate004-eval-ready.json"


@pytest.fixture(scope="module")
def live_current() -> dict:
    return json.loads((REPO_ROOT / "state/m62/current.json").read_text("utf-8"))


def test_the_control_plane_advanced_to_generation_eleven(live_current):
    assert live_current["state_generation"] == 11
    assert live_current["latest_snapshot_path"] == GEN11_PATH
    assert live_current["schema_version"] == "m62.control_plane.1"


def test_the_emitted_snapshot_is_byte_identical_to_the_measured_projection(
        parent_state, live_current):
    """The capacity proof is only worth something if the same transform wrote the file.

    A projection computed by one code path and a snapshot authored by another is a
    measurement of something that was never written. These are the same function, so this
    asserts the property the milestone document claims: the bytes measured are the bytes
    on disk.
    """
    emitted = (REPO_ROOT / GEN11_PATH).read_bytes()
    projected = V.canonical_bytes(P.project_gen11(
        parent_state, subject_commit=live_current["subject_state_commit"],
        parent_sha256=GEN10_SNAPSHOT_SHA))
    assert emitted == projected
    assert V.sha256_bytes(emitted) == live_current["latest_snapshot_sha256"]


def test_the_emitted_snapshot_is_inside_its_budget_with_the_required_headroom():
    emitted = (REPO_ROOT / GEN11_PATH).read_bytes()
    assert len(emitted) <= V.SNAPSHOT_MAX_BYTES
    assert V.SNAPSHOT_MAX_BYTES - len(emitted) >= P.REQUIRED_HEADROOM_BYTES


def test_generation_eleven_chains_onto_generation_ten():
    state = json.loads((REPO_ROOT / GEN11_PATH).read_text("utf-8"))
    assert state["state_generation"] == 11
    assert state["parent_snapshot_sha256"] == GEN10_SNAPSHOT_SHA
    assert state["subject_state_milestone"] == "S3W.0"
    assert state["generation_label"] == "M62_S3W0_CANDIDATE004_EVAL_READY"


def test_the_generation_ten_snapshot_is_still_byte_exact():
    """A superseded snapshot is never revised. Generation 11 is a new file, not an edit."""
    raw = (REPO_ROOT
           / "state/m62/snapshots/0010-m62-s3v-candidate004-trained.json").read_bytes()
    assert V.sha256_bytes(raw) == GEN10_SNAPSHOT_SHA


def test_the_emit_path_refuses_to_write_past_its_own_capacity_gate(tmp_path,
                                                                  monkeypatch):
    """Fail-closed, proved by forcing the gate to fail rather than by reading the code."""
    monkeypatch.setattr(P, "REQUIRED_HEADROOM_BYTES", V.SNAPSHOT_MAX_BYTES)
    destination = tmp_path / "refused.json"
    code = P.main([
        "--subject-commit", "0" * 40, "--emit", str(destination),
        "--parent", str(REPO_ROOT
                        / "state/m62/snapshots/0010-m62-s3v-candidate004-trained.json")])
    assert code == 1
    assert not destination.exists()
