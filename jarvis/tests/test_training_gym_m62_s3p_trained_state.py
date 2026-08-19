"""V69 M62 S3P — the trained state: earned by evidence, never asserted.

WHAT THESE TESTS ARE FOR
------------------------
S3P spent the single live TRAIN capability the operator authorised for candidate 003.
That is irreversible: there is no second token, no retry and no resume. So the danger
this file guards against is not "the run went wrong" — the run either succeeded or it
did not, and the canonical verifiers already answered that. The danger is that the
CONTROL PLANE comes to believe a training run happened when the evidence for it does
not exist, or no longer matches.

The forbidden implementation, stated plainly so nobody reintroduces it:

    the snapshot says TRAINED_UNEVALUATED,
    a constant in the verifier says TRAINED_UNEVALUATED,
    they agree,
    therefore PASS.

That verifies nothing. Both surfaces are writable by the same hand in the same commit.
So a trained claim must additionally be backed by a tracked, root-independent RECEIPT,
and the receipt is cross-checked against the production generator, the sealed dataset
identities, the re-derived render identity and the sealed structural control. Every
mutation below is a way that could quietly stop being true; each one is required to
FAIL, because a check that cannot fail has not been tested.

Two properties are worth naming because they are easy to lose:

  * **The design must keep holding after training.** Single-axis is not a claim about a
    plan that has since been executed — it is a claim about what the weights were fitted
    under, and it starts to matter at the moment the run happens rather than stopping
    there.
  * **Training history must outlive the runtime tree.** The adapter is gitignored. A
    fresh clone has no weights and must still be able to establish that this candidate
    completed its one authorised run (S3P §49), which is exactly what the receipt is for.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS, LOADS A TOKENIZER, CREATES AN
OPTIMIZER, GENERATES A TOKEN OR CONTACTS A NETWORK. Every mutation happens in a
throwaway copy of the control plane; the real adapter and the real run are never touched.

This file reads no ``eval-v4`` task body and contains none.
"""
from __future__ import annotations

import copy
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

import scripts.build_m62_train_receipt as RECEIPT
import scripts.build_quality_training_config as QCFG
from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT

# ── the identities S3P may not move ──────────────────────────────────────────────────
#: Written independently of the artefacts under test. A test that reads its expected
#: value out of the thing it is checking proves nothing.
CANDIDATE_001_ID = "qwen3-06b-lora-quality-live-001"
CANDIDATE_002_ID = "qwen3-06b-lora-quality-live-002"
CANDIDATE_003_ID = "qwen3-06b-lora-quality-live-003"

CANDIDATE_001_ADAPTER = (
    "43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858")
CANDIDATE_002_ADAPTER = (
    "319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409")
#: The adapter S3P actually produced, re-hashed from the bytes on disk in this session.
CANDIDATE_003_ADAPTER = (
    "6ccd8fdc16c6f79d5d7965c1d30a42faecc226581a20f701c582588c76ce4ea6")

TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
CHAT_TEMPLATE_DIGEST = (
    "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8")
RENDER_POLICY_DISABLED = (
    "8619f96c5ba84dab9afe19f8a0fcf385cb452680dd50374ba0e0b9a568490db0")

#: The sealed generation-1 and generation-2 digests. S3P may not touch either.
GEN1_SHA = "a2659d1fb1031726329394f0593478eb57b273048bc0d94faf12c89225dcf2c3"
GEN2_SHA = "cdff52eea78c9763e4a04e3efc4f3d8a536305963f86fb7174e9b4eefef3b621"

#: The commit the training executed from, and the commit that designed the candidate.
TRAINING_SOURCE_COMMIT = "bac49c4a49194d84fbc7f61656662fdcd54799ca"
DESIGN_COMMIT = "c30c821616ecd890ea1bd4368341b4411a1b8701"

#: The preregistered budget, re-derived rather than restated where it can be.
EXPECTED_STEPS = 40
EXPECTED_EPOCHS = 2

RUN_DIRECTORY = REPO / "jarvis" / "training_runs" / "runs" / CANDIDATE_003_ID
LEDGER = REPO / "jarvis" / "training_runs" / "training_runs.jsonl"

#: Every control-plane surface a sandbox needs, including the two S3P added.
_SANDBOX_FILES = (
    V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.ARCHIVE_PATH, V.PROGRESS_PATH,
    V.HISTORY_INDEX_PATH, V.CURRENT_SCHEMA_PATH, V.SNAPSHOT_SCHEMA_PATH,
    V.TRAIN_RECEIPT_SCHEMA_PATH, V.CANDIDATE_003_TRAIN_RECEIPT,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures — every mutation lands in a throwaway tree
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the control plane, so a mutation never touches the real tree.

    ``_git`` keeps pointing at the REAL repository on purpose. The sandbox exists to
    make file CONTENT safe to mutate; whether a path is tracked, and whether a commit
    exists, are properties of the actual repository and are exactly what those checks
    are about. Redirecting them at an empty temporary directory would turn every
    tracking check into "no git here", which is an absence of evidence dressed up as a
    verdict — the failure mode this whole milestone is built to avoid.
    """
    for rel in _SANDBOX_FILES:
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, destination)
    for source in (REPO / V.SNAPSHOT_DIR).iterdir():
        destination = tmp_path / V.SNAPSHOT_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(V, "_git", _real_git)
    return tmp_path


def _real_git(*args: str) -> tuple[int, str]:
    """``V._git``, pinned to the real repository. Read-only plumbing, fixed argv."""
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                          text=True, check=False)
    return done.returncode, done.stdout.strip()


def _plane_from(root: Path) -> V.ControlPlane:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    snapshot_path = root / current["latest_snapshot_path"]
    snapshot_bytes = snapshot_path.read_bytes()
    return V.ControlPlane(
        current=current,
        current_bytes=(root / V.CURRENT_PATH).read_bytes(),
        snapshot=json.loads(snapshot_bytes.decode("utf-8")),
        snapshot_bytes=snapshot_bytes,
        snapshot_path=snapshot_path,
        migration=json.loads(
            (root / V.MIGRATION_MANIFEST_PATH).read_text(encoding="utf-8")))


def _categories(report: V.Report) -> set[str]:
    return {category for category, _ in report.problems}


def _rewrite(root: Path, rel: str, payload: dict) -> None:
    (root / rel).write_bytes(V.canonical_bytes(payload))


def _repoint(root: Path) -> None:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    data = (root / current["latest_snapshot_path"]).read_bytes()
    current["latest_snapshot_sha256"] = V.sha256_bytes(data)
    _rewrite(root, V.CURRENT_PATH, current)


def _entry(snapshot: dict, ordinal: int) -> dict:
    return next(c for c in snapshot["candidates"] if c["ordinal"] == ordinal)


def _trained(root: Path) -> V.ControlPlane:
    """Put ordinal 3 into the S3P trained state inside the sandbox and reload.

    Constructed here rather than read from the live snapshot on purpose: these tests are
    about the MECHANISM that checks a trained claim, and they must mean the same thing
    whether or not the control plane has yet recorded one.
    """
    plane = _plane_from(root)
    mutated = copy.deepcopy(plane.snapshot)
    entry = _entry(mutated, 3)
    entry.update(candidate_id=CANDIDATE_003_ID, status="TRAINED_UNEVALUATED",
                 training_corpus="m62-defensive-quality-train v2",
                 base_model_revision=BASE_MODEL_REVISION,
                 adapter_sha256=CANDIDATE_003_ADAPTER,
                 adapter_manifest_hash=_receipt_of(root)["adapter"]["manifest_hash"],
                 evaluation_corpus=None,
                 evidence=V.CANDIDATE_003_EVIDENCE,
                 training_receipt=V.CANDIDATE_003_TRAIN_RECEIPT)
    _rewrite(root, plane.current["latest_snapshot_path"], mutated)
    _repoint(root)
    return _plane_from(root)


def _receipt_of(root: Path) -> dict:
    return json.loads(
        (root / V.CANDIDATE_003_TRAIN_RECEIPT).read_text(encoding="utf-8"))


def _write_receipt(root: Path, receipt: dict) -> None:
    (root / V.CANDIDATE_003_TRAIN_RECEIPT).write_bytes(V.canonical_bytes(receipt))


def _mutate_receipt(root: Path, mutate) -> V.ControlPlane:
    """Apply a mutation to the sandbox receipt and return the trained plane."""
    plane = _trained(root)
    receipt = _receipt_of(root)
    mutate(receipt)
    _write_receipt(root, receipt)
    return plane


def _receipt_report(plane: V.ControlPlane) -> V.Report:
    report = V.Report()
    V.check_training_receipt(plane, report)
    return report


def _live_snapshot() -> dict:
    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    return json.loads(
        (REPO / current["latest_snapshot_path"]).read_text(encoding="utf-8"))


def _live_candidate(ordinal: int) -> dict:
    return _entry(_live_snapshot(), ordinal)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                          text=True, check=False).stdout.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  1. The design still holds AFTER training  (property 1)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    """A corpus root holding both training versions, plus a shared output root.

    Both configurations are built against the SAME roots on purpose: ``config_hash``
    binds ``output_root_id``, so comparing configurations built under different roots
    would show a difference that is about a filesystem path and not about the model.
    """
    root = tmp_path_factory.mktemp("m62-s3p-train")
    import scripts.build_training_corpus as QC

    QC.build(root, dataset_version="v1")
    QC.build(root, dataset_version="v2")
    return root, root / "runs"


def test_the_axis_is_exactly_one_key_after_training(roots):
    """The normalized semantic diff between the control and the experiment is still
    exactly ``reasoning_policy`` — asserted against the production generator, not
    against a document."""
    dataset_root, output_root = roots
    control = QCFG.build_config(QCFG.CANDIDATE_OPTION["002"], dataset_root=dataset_root,
                                output_root=output_root, candidate="002").to_dict()
    experiment = QCFG.build_config(QCFG.CANDIDATE_OPTION["003"],
                                   dataset_root=dataset_root,
                                   output_root=output_root, candidate="003").to_dict()
    identity = {"run_id", "experiment_name", "notes"}
    keys = set(control) | set(experiment)
    raw = {k for k in keys
           if control.get(k, "<ABSENT>") != experiment.get(k, "<ABSENT>")}
    assert raw - identity == {"reasoning_policy"}
    assert experiment["reasoning_policy"] == "disabled"
    assert "reasoning_policy" not in control
    assert QCFG.CANDIDATE_OPTION["003"] == QCFG.CANDIDATE_OPTION["002"]


def test_the_live_control_plane_still_re_derives_the_design():
    """``check_candidate_design`` must keep covering the candidate after it trains."""
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None
    V.check_candidate_design(loaded, report)
    assert not report.problems, report.problems


def test_the_design_check_covers_the_trained_state_not_only_the_designed_one():
    """The regression this guards: filtering on DESIGNED_UNTRAINED alone would silently
    stop checking the single-axis claim at the exact moment it began to bind weights."""
    source = (REPO / "jarvis" / "scripts"
              / "verify_m62_control_plane.py").read_text(encoding="utf-8")
    marker = 'if c.get("status") in ("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED")'
    assert marker in source


# ══════════════════════════════════════════════════════════════════════════════
#  2. The transition  (properties 2, 3, 47)
# ══════════════════════════════════════════════════════════════════════════════
def test_designed_to_trained_is_the_only_way_in():
    assert V.transition_problems("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED",
                                 V.CANDIDATE_TRANSITIONS, "candidate") == []
    assert V.CANDIDATE_TRANSITIONS["DESIGNED_UNTRAINED"]["TRAINED_UNEVALUATED"] == \
        "TRAIN_AUTHORITY_CONSUMED"


@pytest.mark.parametrize("source", ["NOT_CREATED", "EVALUATED_NOT_ELIGIBLE",
                                    "EVALUATED_QUARANTINED", "PROMOTED"])
def test_no_other_state_may_jump_to_trained(source):
    assert V.transition_problems(source, "TRAINED_UNEVALUATED",
                                 V.CANDIDATE_TRANSITIONS, "candidate") != []


def test_a_trained_candidate_may_not_jump_to_promoted():
    assert V.transition_problems("TRAINED_UNEVALUATED", "PROMOTED",
                                 V.CANDIDATE_TRANSITIONS, "candidate") != []


def test_a_trained_candidate_remains_unevaluated(sandbox):
    """Property 47. Training buys weights; it buys no measurement whatsoever."""
    plane = _trained(sandbox)
    entry = _entry(plane.snapshot, 3)
    assert entry["evaluation_corpus"] is None
    receipt = _receipt_of(sandbox)
    assert receipt["holdout"]["held_out_evaluation_runs"] == 0
    assert receipt["holdout"]["eval_authority_created"] is False
    assert receipt["holdout"]["model_response_tokens_generated"] == 0
    assert receipt["execution"]["generation_performed"] is False
    assert _receipt_report(plane).problems == []


# ══════════════════════════════════════════════════════════════════════════════
#  3. ANTI-CIRCULARITY — the heart of the milestone  (properties 4, 5)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_trained_claim_without_a_receipt_pointer_is_refused(sandbox):
    """Non-vacuity A/B. The snapshot says TRAINED_UNEVALUATED and the verifier's own
    constant agrees with it — and it must STILL fail, because no portable evidence
    exists. Two agreeing writable surfaces are a rumour with a checksum."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["training_receipt"] = None
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    reloaded = _plane_from(sandbox)

    # RESCOPED at S3Q.0.2. This asserted the verifier's own constant AGREED with the
    # sandbox, so the refusal below could not be explained away as a status mismatch.
    # The constant has since moved to EVALUATED_NOT_ELIGIBLE, so the A/B is stated the
    # way it always meant: the sandbox claims a state the verifier RECOGNISES, and is
    # still refused, because no portable evidence backs it.
    assert "TRAINED_UNEVALUATED" in V.CANDIDATE_STATES
    assert _entry(reloaded.snapshot, 3)["status"] == "TRAINED_UNEVALUATED"
    report = _receipt_report(reloaded)
    assert "TRAINING_RECEIPT" in _categories(report)
    assert any("offers nothing a reader could check" in m for _, m in report.problems)

    state = V.Report()
    V.check_candidate_state(reloaded, state)
    assert any("without training_receipt" in m for _, m in state.problems)


def test_a_trained_claim_whose_receipt_file_is_gone_is_refused(sandbox):
    """Non-vacuity A. The pointer survives, the evidence does not."""
    plane = _trained(sandbox)
    (sandbox / V.CANDIDATE_003_TRAIN_RECEIPT).unlink()
    report = _receipt_report(plane)
    assert any("is not a regular file" in m for _, m in report.problems)


def test_the_verifier_constant_alone_cannot_establish_the_trained_state(sandbox):
    """Property 5. Even with the constant, the snapshot and the state all agreeing, an
    unusable receipt is a failure — never a pass by consensus."""
    plane = _trained(sandbox)
    (sandbox / V.CANDIDATE_003_TRAIN_RECEIPT).write_text("{not json", encoding="utf-8")
    report = _receipt_report(plane)
    assert any("unreadable" in m for _, m in report.problems)


# ══════════════════════════════════════════════════════════════════════════════
#  4. The receipt must describe THIS run  (properties 6-20, 45, 46)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_receipt_for_another_candidate_is_not_evidence(sandbox):
    """Non-vacuity: property 45. A receipt is not a transferable trained-ness token."""
    plane = _mutate_receipt(
        sandbox, lambda r: r.update(candidate_id="qwen3-06b-lora-quality-live-002"))
    report = _receipt_report(plane)
    assert any("describes" in m and "not evidence about this one" in m
               for _, m in report.problems)


def test_a_receipt_naming_an_unknown_candidate_is_refused(sandbox):
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["candidate_id"] = "qwen3-06b-lora-quality-live-999"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    receipt = _receipt_of(sandbox)
    receipt["candidate_id"] = "qwen3-06b-lora-quality-live-999"
    _write_receipt(sandbox, receipt)
    report = _receipt_report(_plane_from(sandbox))
    assert any("no candidate in the production generator" in m
               for _, m in report.problems)


def test_a_base_model_mismatch_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["base_model"].update(model_id="Qwen/Qwen3-1.7B"))
    assert _receipt_report(plane).problems != []


def test_a_base_revision_mismatch_is_refused(sandbox):
    plane = _mutate_receipt(sandbox,
                            lambda r: r["base_model"].update(revision="b" * 40))
    assert any("revision" in m for _, m in _receipt_report(plane).problems)


def test_a_training_corpus_mismatch_is_refused(sandbox):
    """Non-vacuity K. The manifest is what identifies `train v2`; a different digest is
    a different corpus wearing the same version label."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["training_dataset"].update(manifest_hash="c" * 64))
    assert any("is not the sealed" in m for _, m in _receipt_report(plane).problems)


def test_a_training_corpus_version_mismatch_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["training_dataset"].update(version="v1"))
    assert _receipt_report(plane).problems != []


def test_a_reasoning_policy_mismatch_is_refused(sandbox):
    """Non-vacuity L. The single most damaging quiet edit: the run is relabelled as
    having trained under the legacy representation the experiment exists to leave."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["representation"].update(reasoning_policy="model_default"))
    assert any("the generator designs it as 'disabled'" in m
               for _, m in _receipt_report(plane).problems)


def test_a_render_identity_mismatch_is_refused(sandbox):
    """The render identity is re-derived from the snapshot's own template digest, so a
    run that did not execute the designed representation cannot claim it did."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["representation"].update(
            chat_render_policy_hash="d" * 64))
    assert any("did not execute the designed representation" in m
               for _, m in _receipt_report(plane).problems)


@pytest.mark.parametrize("creations", [0, 2, 3])
def test_an_authority_creation_count_other_than_one_is_refused(sandbox, creations):
    """Non-vacuity E. Exactly one capability was authorised."""
    plane = _mutate_receipt(sandbox,
                            lambda r: r["authority"].update(creations=creations))
    assert any("authority creation" in m for _, m in _receipt_report(plane).problems)


@pytest.mark.parametrize("consumptions", [0, 2])
def test_a_consumption_count_other_than_one_is_refused(sandbox, consumptions):
    """Non-vacuity F and G. Zero means nothing was spent; two means the single-use rule
    was broken and the candidate was trained twice."""
    plane = _mutate_receipt(sandbox,
                            lambda r: r["authority"].update(consumptions=consumptions))
    assert any("consumption" in m for _, m in _receipt_report(plane).problems)


@pytest.mark.parametrize("status", ["FAILED", "INTERRUPTED", "REFUSED"])
def test_a_non_success_terminal_status_is_refused(sandbox, status):
    """Non-vacuity D. A spent capability is not a trained candidate."""
    plane = _mutate_receipt(sandbox,
                            lambda r: r["execution"].update(terminal_status=status))
    assert any("not trained by an attempt that did not succeed" in m
               for _, m in _receipt_report(plane).problems)


def test_an_interrupted_run_is_refused(sandbox):
    plane = _mutate_receipt(sandbox,
                            lambda r: r["execution"].update(interrupted=True))
    assert any("uninterrupted" in m for _, m in _receipt_report(plane).problems)


@pytest.mark.parametrize("completed", [39, 41, 0])
def test_a_step_count_short_of_the_budget_is_refused(sandbox, completed):
    """Non-vacuity C. 39 of 40 is a different experiment, not this one."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["execution"].update(optimizer_steps_completed=completed))
    assert any("optimizer steps completed" in m
               for _, m in _receipt_report(plane).problems)


def test_a_planned_step_count_that_is_not_the_design_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["execution"].update(optimizer_steps_planned=27))
    assert any("the design declares" in m for _, m in _receipt_report(plane).problems)


def test_an_epoch_count_that_is_not_the_design_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["execution"].update(epochs_configured=3))
    assert any("epochs" in m for _, m in _receipt_report(plane).problems)


def test_a_missing_closing_validation_is_refused(sandbox):
    """D31: the closing ``trainer.evaluate()`` is wiring the repository requires."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["execution"].update(final_validation_present=False))
    assert any("closing validation" in m for _, m in _receipt_report(plane).problems)


def test_a_non_finite_metric_is_refused(sandbox):
    """Written with ``allow_nan=True`` deliberately: the canonical serialiser REFUSES to
    emit ``Infinity`` at all, so this mutation has to be forced past it to prove the
    verifier would still catch a receipt that arrived from somewhere else."""
    plane = _trained(sandbox)
    receipt = _receipt_of(sandbox)
    receipt["execution"]["train_loss"] = float("inf")
    (sandbox / V.CANDIDATE_003_TRAIN_RECEIPT).write_text(
        json.dumps(receipt, sort_keys=True, indent=2, allow_nan=True) + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError):
        V.canonical_bytes(receipt)
    assert any("not finite" in m for _, m in _receipt_report(plane).problems)


@pytest.mark.parametrize("field", ["sha256", "manifest_hash", "artifact_set_hash"])
def test_a_missing_adapter_identity_is_refused(sandbox, field):
    """Properties 16, 17, 18."""
    plane = _mutate_receipt(sandbox, lambda r: r["adapter"].update({field: ""}))
    assert _receipt_report(plane).problems != []


def test_a_snapshot_adapter_sha_mismatch_is_refused(sandbox):
    """Non-vacuity H. The two surfaces must agree, and disagreement is a failure."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["adapter_sha256"] = "e" * 64
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = _receipt_report(_plane_from(sandbox))
    assert any("is not the snapshot's" in m for _, m in report.problems)


def test_a_snapshot_adapter_manifest_mismatch_is_refused(sandbox):
    """Non-vacuity I."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["adapter_manifest_hash"] = "f" * 64
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = _receipt_report(_plane_from(sandbox))
    assert any("is not the snapshot's" in m for _, m in report.problems)


def test_a_trained_candidate_carrying_an_evaluation_corpus_is_refused(sandbox):
    """Non-vacuity N. Property 21."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["evaluation_corpus"] = "m62-defensive-eval v4"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert any("no held-out material has been spent" in m for _, m in report.problems)


def test_a_receipt_claiming_held_out_evaluation_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["holdout"].update(held_out_evaluation_runs=1))
    assert any("TRAINED_UNEVALUATED" in m for _, m in _receipt_report(plane).problems)


def test_a_receipt_claiming_an_eval_authority_is_refused(sandbox):
    """Property 23."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["holdout"].update(eval_authority_created=True))
    assert _receipt_report(plane).problems != []


@pytest.mark.parametrize("field", sorted(V.STRUCTURAL_ADAPTER_CONTROL))
def test_a_structural_adapter_mutation_is_refused(sandbox, field):
    """Non-vacuity M. Property 46.

    Candidates 002 and 003 share the architecture and the LoRA scope, so their adapters
    must be structurally identical: 28 layers x 7 projections x 2 matrices. Learned
    VALUES differ and must — that is the experiment. Structure differing means the run
    did not adapt what the design said it would.
    """
    plane = _mutate_receipt(
        sandbox,
        lambda r: r["adapter"].update({field: V.STRUCTURAL_ADAPTER_CONTROL[field] + 1}))
    assert any("structural control" in m for _, m in _receipt_report(plane).problems)


def test_a_target_module_mutation_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["adapter"].update(
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
    assert any("projection set" in m for _, m in _receipt_report(plane).problems)


def test_a_failed_completed_run_verifier_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["verification"].update(completed_run_verifier="FAIL"))
    assert any("completed-run verifier" in m for _, m in _receipt_report(plane).problems)


def test_an_invalid_adapter_verdict_is_refused(sandbox):
    """Non-vacuity J."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["verification"].update(adapter_verifier="invalid"))
    assert any("adapter verifier" in m for _, m in _receipt_report(plane).problems)


def test_a_checkpoint_tree_is_refused(sandbox):
    """D16: checkpoint directories write pickle-shaped trainer state."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["verification"].update(checkpoint_directories=1))
    assert any("checkpoint" in m for _, m in _receipt_report(plane).problems)


def test_a_receipt_naming_a_commit_this_repository_lacks_is_refused(sandbox):
    """Non-vacuity R. Property 37."""
    plane = _mutate_receipt(
        sandbox, lambda r: r.update(training_source_commit="0" * 40))
    assert any("is not a commit in this repository" in m
               for _, m in _receipt_report(plane).problems)


def test_a_receipt_bound_to_a_different_plan_than_it_ran_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["authority"].update(bound_plan_hash="1" * 64))
    assert any("bound to a different plan" in m
               for _, m in _receipt_report(plane).problems)


# ══════════════════════════════════════════════════════════════════════════════
#  5. Secrets, paths and holdout material inside the receipt  (properties 41-43)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_token_literal_inside_the_receipt_is_refused(sandbox):
    """Non-vacuity S. A receipt proves an authority was spent; it never reproduces one."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["representation"].update(
            masking_strategy="TRAIN:" + "a" * 64))
    assert any("spendable plan token" in m for _, m in _receipt_report(plane).problems)


def test_a_private_host_path_inside_the_receipt_is_refused(sandbox):
    """Non-vacuity T."""
    plane = _mutate_receipt(
        sandbox, lambda r: r["runtime"].update(device_category="/home/someone/cpu"))
    assert any("private host path" in m for _, m in _receipt_report(plane).problems)


def test_an_eval_v4_body_source_reference_inside_the_receipt_is_refused(sandbox):
    plane = _mutate_receipt(
        sandbox, lambda r: r["representation"].update(
            masking_strategy="corpus_v4_material"))
    assert any("eval-v4 body source" in m for _, m in _receipt_report(plane).problems)


def test_the_real_receipt_carries_no_token_no_path_and_no_task_material():
    text = (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_text(encoding="utf-8")
    assert V.TOKEN_LITERAL_RE.search(text) is None
    assert V.PRIVATE_PATH_RE.search(text) is None
    assert not [t for t in V.EVAL_V4_TASK_IDS if t in text]
    for symbol in V.FORBIDDEN_BODY_SYMBOLS:
        assert symbol not in text
    text.encode("ascii")


def test_no_tracked_file_carries_a_token_literal():
    """Property 41, measured over the whole tree rather than the changeset."""
    result = subprocess.run(
        ["git", "-C", str(REPO), "grep", "-I", "-n", "-E", V.TOKEN_LITERAL_PATTERN,
         "--", "."], capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout[:400]


def test_no_tracked_control_plane_surface_carries_a_private_path():
    """Property 42.

    The immutable archive is excluded and must stay excluded: it carries the elided
    literal ``/home/...`` inside prose about the redactor, it is append-never-edit, and
    the remediation for a historical record is operator review — never a quiet rewrite
    to make a scanner happy.
    """
    surfaces = [rel for rel in (*_SANDBOX_FILES, V.VERIFIER_PATH)
                if rel != V.ARCHIVE_PATH]
    assert V.ARCHIVE_PATH not in surfaces
    for rel in surfaces:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert V.PRIVATE_PATH_RE.search(text) is None, rel


# ══════════════════════════════════════════════════════════════════════════════
#  6. The receipt is DERIVED, not typed  (property 44)
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_rebuilds_to_the_same_bytes():
    """Property 44. Rebuilding from the same canonical evidence must reproduce the
    tracked bytes exactly — otherwise the receipt records an opinion rather than a
    derivation.

    Skipped only when the gitignored run tree is absent, which is the honest boundary:
    the receipt is portable, the evidence it was distilled FROM is not (S3P §49).
    """
    if not RUN_DIRECTORY.is_dir():
        pytest.skip("the gitignored candidate-003 run tree is not present on this host")
    rebuilt = RECEIPT.build_receipt(
        RUN_DIRECTORY, candidate=CANDIDATE_003_ID,
        training_source_commit=TRAINING_SOURCE_COMMIT, design_commit=DESIGN_COMMIT,
        ledger=LEDGER)
    tracked = (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_bytes()
    assert V.canonical_bytes(rebuilt) == tracked


def test_the_receipt_serializer_is_deterministic():
    """Runs everywhere: the SERIALISER's determinism does not need the run tree."""
    receipt = json.loads(
        (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_text(encoding="utf-8"))
    shuffled = dict(reversed(list(receipt.items())))
    assert V.canonical_bytes(shuffled) == V.canonical_bytes(receipt)


def test_the_receipt_carries_no_self_referential_digest():
    """A digest of itself would be either wrong or a fixed point nobody can recompute."""
    raw = (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_bytes()
    own = V.sha256_bytes(raw)
    assert own not in raw.decode("utf-8")
    receipt = json.loads(raw.decode("utf-8"))
    assert "receipt_hash" not in receipt
    assert not any("timestamp" in key or key.endswith("_at_utc") for key in receipt)


def test_the_receipt_matches_the_verified_run_it_describes():
    """The receipt's own numbers, checked against the frozen design and the budget."""
    receipt = json.loads(
        (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_text(encoding="utf-8"))
    assert receipt["candidate_id"] == CANDIDATE_003_ID
    assert receipt["training_source_commit"] == TRAINING_SOURCE_COMMIT
    assert receipt["design_commit"] == DESIGN_COMMIT
    assert receipt["base_model"]["model_id"] == BASE_MODEL_ID
    assert receipt["base_model"]["revision"] == BASE_MODEL_REVISION
    assert receipt["base_model"]["chat_template_digest"] == CHAT_TEMPLATE_DIGEST
    assert receipt["training_dataset"]["manifest_hash"] == TRAIN_V2_MANIFEST
    assert receipt["training_dataset"]["version"] == "v2"
    assert receipt["representation"]["reasoning_policy"] == "disabled"
    assert receipt["representation"]["chat_render_policy_hash"] == RENDER_POLICY_DISABLED
    assert receipt["authority"]["creations"] == 1
    assert receipt["authority"]["consumptions"] == 1
    assert receipt["authority"]["token_literal_recorded"] is False
    assert receipt["execution"]["terminal_status"] == "SUCCESS"
    assert receipt["execution"]["optimizer_steps_completed"] == EXPECTED_STEPS
    assert receipt["execution"]["optimizer_steps_planned"] == EXPECTED_STEPS
    assert receipt["execution"]["epochs_configured"] == EXPECTED_EPOCHS
    assert receipt["execution"]["truncated_records"] == 0
    assert receipt["execution"]["converted_records"] == 154
    assert math.isfinite(receipt["execution"]["train_loss"])
    assert all(math.isfinite(v) for v in receipt["execution"]["validation_losses"])
    assert receipt["adapter"]["sha256"] == CANDIDATE_003_ADAPTER
    assert receipt["verification"]["completed_run_verifier"] == "PASS"
    assert receipt["verification"]["adapter_verifier"] == "valid"
    assert receipt["ledger"]["events"] == {"completed": 1, "started": 1}


def test_the_receipt_validates_against_its_published_schema():
    receipt = json.loads(
        (REPO / V.CANDIDATE_003_TRAIN_RECEIPT).read_text(encoding="utf-8"))
    assert V.validate_against_schema(V.train_receipt_schema(), receipt) == []
    published = (REPO / V.TRAIN_RECEIPT_SCHEMA_PATH).read_bytes()
    assert published == V.canonical_bytes(V.train_receipt_schema())


def test_the_builder_refuses_a_candidate_the_generator_does_not_name():
    """Property 45, at the source: a receipt cannot be minted for an invented identity."""
    if not RUN_DIRECTORY.is_dir():
        pytest.skip("the gitignored candidate-003 run tree is not present on this host")
    with pytest.raises(ValueError, match="not a candidate the production generator"):
        RECEIPT.build_receipt(RUN_DIRECTORY, candidate="qwen3-06b-lora-quality-live-777",
                              training_source_commit=TRAINING_SOURCE_COMMIT,
                              design_commit=DESIGN_COMMIT, ledger=LEDGER)


# ══════════════════════════════════════════════════════════════════════════════
#  7. Nothing else moved  (properties 22-31, 39, 40)
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate_001_is_untouched():
    entry = _live_candidate(1)
    assert entry["candidate_id"] == CANDIDATE_001_ID
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["adapter_sha256"] == CANDIDATE_001_ADAPTER
    assert V.FROZEN_CANDIDATES[CANDIDATE_001_ID] == (
        "EVALUATED_NOT_ELIGIBLE", CANDIDATE_001_ADAPTER)


def test_candidate_002_is_untouched():
    entry = _live_candidate(2)
    assert entry["candidate_id"] == CANDIDATE_002_ID
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["adapter_sha256"] == CANDIDATE_002_ADAPTER
    assert V.FROZEN_CANDIDATES[CANDIDATE_002_ID] == (
        "EVALUATED_NOT_ELIGIBLE", CANDIDATE_002_ADAPTER)


def test_the_two_predecessor_adapters_still_hash_to_their_sealed_values():
    """The strongest form: the bytes on disk, not the recorded claim about them."""
    runs = REPO / "jarvis" / "training_runs" / "runs"
    for cid, expected in ((CANDIDATE_001_ID, CANDIDATE_001_ADAPTER),
                          (CANDIDATE_002_ID, CANDIDATE_002_ADAPTER)):
        weights = runs / cid / "adapter_model.safetensors"
        if not weights.is_file():
            pytest.skip("the gitignored predecessor run trees are not on this host")
        assert V.sha256_bytes(weights.read_bytes()) == expected, cid


def test_training_the_candidate_did_not_spend_the_holdout():
    """Property 22, and the whole point of S3P not being an evaluation.

    RESCOPED at S3Q.0.2 to generation 3, for the reason S3Q.0 pinned the gen2 -> gen3
    test: read live, this also asserted that no LATER generation had spent v4 -- true by
    coincidence until S3Q spent it, under a separate authority, which is exactly the
    sequence S3P said would be required. The property S3P owns is that TRAINING spends no
    holdout, and it is immutable where S3P recorded it.
    """
    generation_3 = next(
        path for path in sorted((REPO / V.SNAPSHOT_DIR).iterdir())
        if json.loads(path.read_text(encoding="utf-8"))["state_generation"] == 3)
    snapshot = json.loads(generation_3.read_text(encoding="utf-8"))
    assert snapshot["subject_state_milestone"] == "S3P"
    entry = next(d for d in snapshot["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None


def test_a_spent_holdout_may_never_be_relabelled_fresh():
    """Non-vacuity O, in the direction that would actually destroy evidence."""
    assert V.transition_problems("USED_IMMUTABLE", "FROZEN_UNUSED",
                                 V.DATASET_TRANSITIONS, "dataset") != []


def test_relabelling_the_spent_holdout_as_fresh_is_refused(sandbox):
    """Non-vacuity O, INVERTED at S3Q.0.2 because the available lie changed.

    Until S3Q this wrote USED_IMMUTABLE over a fresh holdout: training does not spend a
    holdout, so nothing might say it did. S3Q then spent v4 under a separate authority,
    which makes that mutation a no-op that would assert nothing -- and makes the opposite
    edit possible for the first time. Relabelling a SPENT holdout as fresh is the more
    dangerous direction by far, it has no edge in the transition table, and it must be
    refused. Same check, same non-vacuity, pointed at the lie that now exists.
    """
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    entry = next(d for d in mutated["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    entry["status"] = "FROZEN_UNUSED"
    entry["spent_by"] = None
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_dataset_state(_plane_from(sandbox), report)
    assert "DATASET_STATE" in _categories(report)


def test_a_promotion_claim_is_refused(sandbox):
    """Property 24. No promotion mechanism exists, so nothing could witness the claim."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    _entry(mutated, 3)["status"] = "PROMOTED"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert any("cannot be witnessed" in m for _, m in report.problems)
    assert "PROMOTED" in V.UNWITNESSABLE_CANDIDATE_STATES


@pytest.mark.parametrize("defect,status", [("D37", "FIXED"),
                                           ("D38", "FIXED_OBSERVABILITY_ONLY"),
                                           ("D39", "OPEN")])
def test_the_defect_statuses_are_unchanged(defect, status):
    """Properties 27, 28, 30."""
    assert V.FROZEN_DEFECT_STATUSES[defect] == status
    entry = next(d for d in _live_snapshot()["defects"] if d["id"] == defect)
    assert entry["status"] == status


def test_d38_is_still_not_a_gate():
    """Property 29. Turning the output-budget instrument into a gate needs its own
    operator decision, and S3P is not it."""
    entry = next(d for d in _live_snapshot()["defects"] if d["id"] == "D38")
    assert entry["is_gate"] is False
    from training_gym.evaluation import gates

    source = Path(gates.__file__).read_text(encoding="utf-8")
    assert "output_budget_exhaust" not in source
    assert "finish_reason" not in source


def test_the_policy_identities_are_unchanged():
    """Property 31 — re-derived by the verifier from the production classes."""
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None
    V.check_policy_identities(loaded, report)
    assert not report.problems, report.problems
    identities = _live_snapshot()["policy_identities"]
    assert identities["reasoning_policy"] == "DISABLED"
    assert identities["max_new_tokens"] == 512


def test_the_archive_is_unchanged():
    """Property 39."""
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None
    V.check_archive(loaded, report)
    assert not report.problems, report.problems


def test_authority_separation_survives_a_spent_capability():
    """Property 40. One TRAIN capability was spent; the control plane still grants none,
    and no reusable capability exists anywhere in tracked material."""
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None
    V.check_authority_separation(loaded, report)
    assert not report.problems, report.problems
    observation = _live_snapshot()["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"


def test_prose_cannot_grant_a_train_authority(sandbox):
    """Non-vacuity U. A sentence saying training is authorised is a sentence."""
    plane = _trained(sandbox)
    progress = sandbox / V.PROGRESS_PATH
    progress.write_text(progress.read_text(encoding="utf-8")
                        + "\nTRAIN authorized: true\n", encoding="utf-8")
    report = V.Report()
    V.check_authority_separation(plane, report)
    assert not report.problems
    assert any("AMBIGUOUS AUTHORITY CLAIM" in note for note in report.notes)


# ══════════════════════════════════════════════════════════════════════════════
#  8. The snapshot chain  (properties 32-38, 48-50)
# ══════════════════════════════════════════════════════════════════════════════
def test_generation_1_is_byte_for_byte_unchanged():
    """Property 32."""
    path = REPO / V.SNAPSHOT_DIR / "0001-m62-control-plane-v2-genesis.json"
    assert V.sha256_bytes(path.read_bytes()) == GEN1_SHA


def test_generation_2_is_byte_for_byte_unchanged():
    """Property 33. S3P is a NEW generation; it never revises the one it descends from."""
    path = (REPO / V.SNAPSHOT_DIR
            / "0002-m62-third-candidate-designed-untrained.json")
    assert V.sha256_bytes(path.read_bytes()) == GEN2_SHA
    assert json.loads(path.read_text(encoding="utf-8"))["state_generation"] == 2


def test_the_snapshot_chain_is_hash_linked_and_current():
    """Properties 34-36. Each generation names its parent's bytes, and the pointer names
    the newest generation's bytes."""
    snapshots = sorted((REPO / V.SNAPSHOT_DIR).iterdir())
    previous_sha = None
    for index, path in enumerate(snapshots, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state_generation"] == index, path.name
        assert payload["parent_snapshot_sha256"] == previous_sha, path.name
        previous_sha = V.sha256_bytes(path.read_bytes())

    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    assert current["latest_snapshot_path"] == str(
        snapshots[-1].relative_to(REPO)).replace("\\", "/")
    assert current["latest_snapshot_sha256"] == previous_sha
    assert current["state_generation"] == len(snapshots)


def test_the_trained_state_is_recorded_at_a_new_generation_descending_from_gen2():
    """Properties 34, 35. The trained state was RECORDED at generation 3, with generation
    2 as its parent — never by editing generation 2 in place.

    Read from generation 3 specifically rather than from whatever the newest snapshot is.
    V69 M62 S3Q.0 pinned that: reading it live meant the test also asserted that no LATER
    generation existed, which was true only until the next milestone wrote one. The
    property S3P owns is about the gen2 -> gen3 TRANSITION, and that transition is
    immutable — so it is checked where it happened, and stays checkable forever.
    """
    generation_3 = next(
        p for p in sorted((REPO / V.SNAPSHOT_DIR).iterdir())
        if json.loads(p.read_text(encoding="utf-8"))["state_generation"] == 3)
    snapshot = json.loads(generation_3.read_text(encoding="utf-8"))
    entry = _entry(snapshot, 3)
    if entry["status"] != "TRAINED_UNEVALUATED":
        pytest.skip("the control plane has not yet recorded the trained state")
    assert snapshot["state_generation"] == 3
    assert snapshot["parent_snapshot_sha256"] == GEN2_SHA
    assert snapshot["subject_state_milestone"] == "S3P"

    # RESCOPED at S3Q.0.2. This asserted the candidate was STILL trained-and-unevaluated
    # in the current generation -- which quietly asserted that no later generation had
    # measured it. S3Q did. The claim S3P owns is the gen2 -> gen3 transition above; what
    # survives here is that the candidate is the same one and that its history was not
    # rewritten underneath the later state.
    live = _live_candidate(3)
    assert live["candidate_id"] == CANDIDATE_003_ID
    assert live["adapter_sha256"] == entry["adapter_sha256"]
    assert live["training_receipt"] == entry["training_receipt"]


def test_the_subject_commit_exists_and_head_descends_from_it():
    """Properties 37, 38."""
    snapshot = _live_snapshot()
    subject = snapshot["subject_state_commit"]
    assert _git("cat-file", "-t", subject) == "commit"
    result = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", subject, "HEAD"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_the_progress_size_guard_holds():
    """Property 48."""
    progress = REPO / V.PROGRESS_PATH
    text = progress.read_text(encoding="utf-8")
    assert text.count("\n") <= V.PROGRESS_MAX_LINES
    assert progress.stat().st_size <= V.PROGRESS_MAX_BYTES


def test_the_snapshot_size_guard_holds():
    """Property 49. State carries pointers, not reports — the receipt is a POINTER."""
    current = json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))
    snapshot = REPO / current["latest_snapshot_path"]
    assert snapshot.stat().st_size <= V.SNAPSHOT_MAX_BYTES
    assert (REPO / V.CURRENT_PATH).stat().st_size <= V.CURRENT_MAX_BYTES


def test_the_one_writer_rule_is_enforced_by_the_pointer(sandbox):
    """Property 50. A snapshot the pointer does not name cannot become current by
    existing, and a pointer whose digest does not match its snapshot is a failure."""
    plane = _trained(sandbox)
    current = json.loads((sandbox / V.CURRENT_PATH).read_text(encoding="utf-8"))
    current["latest_snapshot_sha256"] = "0" * 64
    _rewrite(sandbox, V.CURRENT_PATH, current)
    report = V.Report()
    V.check_current_pointer(_plane_from(sandbox), report)
    assert "CURRENT_POINTER" in _categories(report)
    assert plane is not None


def test_a_corrupted_parent_link_is_refused(sandbox):
    """Non-vacuity P."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["parent_snapshot_sha256"] = "9" * 64
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" in _categories(report)


def test_a_skipped_generation_number_is_refused(sandbox):
    """Non-vacuity Q."""
    plane = _trained(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["state_generation"] = mutated["state_generation"] + 2
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    current = json.loads((sandbox / V.CURRENT_PATH).read_text(encoding="utf-8"))
    current["state_generation"] = mutated["state_generation"]
    _rewrite(sandbox, V.CURRENT_PATH, current)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" in _categories(report)


# ══════════════════════════════════════════════════════════════════════════════
#  9. The milestone's own evidence
# ══════════════════════════════════════════════════════════════════════════════
def test_the_deep_evidence_document_exists_and_is_tracked():
    pointer = "jarvis/docs/V69_M62_S3P_CANDIDATE003_LIVE_TRAINING.md"
    assert (REPO / pointer).is_file()
    assert _git("ls-files", "--error-unmatch", "--", pointer) == pointer


def test_the_deep_evidence_document_carries_no_secret_or_holdout_material():
    text = (REPO / "jarvis" / "docs"
            / "V69_M62_S3P_CANDIDATE003_LIVE_TRAINING.md").read_text(encoding="utf-8")
    assert V.TOKEN_LITERAL_RE.search(text) is None
    assert V.PRIVATE_PATH_RE.search(text) is None
    assert not [t for t in V.EVAL_V4_TASK_IDS if t in text]
    for symbol in V.FORBIDDEN_BODY_SYMBOLS:
        assert symbol not in text


def test_the_receipt_is_tracked_and_lives_in_the_state_tree():
    pointer = V.CANDIDATE_003_TRAIN_RECEIPT
    assert pointer.startswith(V.RECEIPT_DIR)
    assert _git("ls-files", "--error-unmatch", "--", pointer) == pointer
    assert not (REPO / pointer).is_symlink()


def test_the_runtime_adapter_is_not_tracked():
    """Weights stay out of Git. The receipt is what travels."""
    assert _git("ls-files", "--", "jarvis/training_runs") == ""
    result = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", "jarvis/training_runs"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_training_history_does_not_depend_on_the_runtime_tree(sandbox):
    """S3P §49. The distinction that makes the receipt worth having: a fresh clone has
    no adapter, and the trained state must still verify."""
    plane = _trained(sandbox)
    assert not (sandbox / "jarvis" / "training_runs").exists()
    assert _receipt_report(plane).problems == []


def test_the_verifier_reports_the_training_receipt_category():
    assert "TRAINING_RECEIPT" in V.CATEGORIES
