"""V69 M62 S3Q.0 — portable evaluation evidence, built before the irreversible act.

WHY THIS IS A PRE-LIVE MILESTONE
--------------------------------
S3P had to invent portable training evidence after the training run, which meant the
evidence form was designed by somebody who already knew the answer. A held-out evaluation
is the same problem with no second chance: it happens once, its artefacts live entirely in
gitignored runtime trees, and a fresh clone has none of them. So the receipt machinery is
built and qualified against SYNTHETIC evidence now, while nothing is at stake.

THE PROPERTY THAT MATTERS
-------------------------
A receipt is EVIDENCE OF AN OPERATION and never AUTHORITY FOR ANOTHER. It proves what
happened; it permits nothing. Every test below that says "grants nothing" is testing the
sentence that separates this repository from one where a document can authorise a run.

ANTI-CIRCULARITY
----------------
The control plane may not establish an ``EVALUATED_*`` state from its own snapshot. Two
writable surfaces agreeing is a rumour with a checksum, and here the rumour would concern
the one irreversible act in the milestone. Both directions are proved: a snapshot claiming
a candidate failed and a snapshot claiming it passed are BOTH refused without a receipt.
"""
from __future__ import annotations

import json

import pytest

from scripts.build_m62_eval_receipt import (
    ELIGIBILITY_TO_CANDIDATE_STATE,
    RECEIPT_SCHEMA_VERSION,
    ReceiptError,
    build_receipt,
    receipt_hash,
    seal,
    verify_receipt,
)
from scripts.verify_m62_control_plane import (
    EVAL_RECEIPT_SCHEMA_VERSION,
    canonical_json,
    eval_receipt_schema,
    validate_against_schema,
)

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0receiptdata")
    S.build(root)
    return root


@pytest.fixture(scope="module")
def evaluated(dataset_root, tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0receiptrun")
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.ok, outcome.problems
    return outcome, root


@pytest.fixture(scope="module")
def receipt(evaluated):
    outcome, root = evaluated
    return seal(build_receipt(
        outcome.directory, candidate="s3q0-synthetic-candidate",
        evaluation_source_commit="a" * 40,
        ledger=root / "evaluation_runs.jsonl"))


# ══════════════════════════════════════════════════════════════════════════════
#  It verifies, and it is deterministic
# ══════════════════════════════════════════════════════════════════════════════
def test_a_receipt_from_a_complete_evaluation_verifies(receipt):
    assert verify_receipt(receipt) == ()


def test_a_receipt_validates_against_the_tracked_schema(receipt):
    assert validate_against_schema(eval_receipt_schema(), receipt) == []


def test_the_schema_version_is_the_one_the_verifier_enforces(receipt):
    assert receipt["schema_version"] == EVAL_RECEIPT_SCHEMA_VERSION
    assert RECEIPT_SCHEMA_VERSION == EVAL_RECEIPT_SCHEMA_VERSION


def test_the_same_evidence_produces_the_same_bytes(evaluated, receipt):
    outcome, root = evaluated
    again = seal(build_receipt(
        outcome.directory, candidate="s3q0-synthetic-candidate",
        evaluation_source_commit="a" * 40,
        ledger=root / "evaluation_runs.jsonl"))
    assert canonical_json(again) == canonical_json(receipt)
    assert again["receipt_hash"] == receipt["receipt_hash"]


def test_the_receipt_carries_no_timestamp_of_its_own(receipt):
    """Its identity is its bytes. A clock would make two honest builds disagree."""
    text = canonical_json(receipt)
    assert "created_at" not in text
    assert "generated_at" not in text


@pytest.mark.parametrize("path,value", [
    (("holdout", "task_pack_hash"), "9" * 64),
    (("holdout", "hidden_target_store_hash"), "9" * 64),
    (("plan", "plan_hash"), "9" * 64),
    (("plan", "order_assignment_hash"), "9" * 64),
    (("policies", "gate_policy_hash"), "9" * 64),
    (("evidence", "report_hash"), "9" * 64),
    (("outcome", "eligibility"), "eligible_for_human_review"),
    (("ledger", "holdout_commit_count"), 2),
])
def test_changing_one_bound_fact_breaks_verification(receipt, path, value):
    """Non-vacuity: every bound fact is really bound."""
    import copy

    tampered = copy.deepcopy(receipt)
    section, key = path
    tampered[section][key] = value
    assert receipt_hash(tampered) != tampered["receipt_hash"]
    assert verify_receipt(tampered)


def test_the_digest_covers_everything_except_itself(receipt):
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    from scripts.build_m62_eval_receipt import _sha256_bytes
    assert receipt["receipt_hash"] == _sha256_bytes(
        canonical_json(body).encode("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  It is body-free
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_carries_no_canary(receipt):
    assert S.leaked_canaries(canonical_json(receipt)) == []


def test_the_receipt_carries_no_confirmation_literal(receipt):
    from training_gym.evaluation.preflight import confirmation_literals

    assert confirmation_literals(receipt) == ()
    assert receipt["authority"]["form"] == "EVAL:<plan-hash>"
    assert receipt["authority"]["token_literal_recorded"] is False


def test_the_receipt_carries_no_body_bearing_field_name(receipt):
    from training_gym.evaluation.preflight import body_free_problems

    assert body_free_problems(receipt) == ()


def test_the_receipt_is_ascii_so_its_bytes_do_not_depend_on_an_encoding(receipt):
    canonical_json(receipt).encode("ascii")


def test_the_receipt_binds_the_body_bearing_pack_by_digest_only(receipt):
    """The pack file is hashed, never quoted. Body-opaque verification, in one field."""
    files = receipt["evidence"]["files"]
    assert "task-pack.jsonl" in files
    assert set(files["task-pack.jsonl"]) == {"sha256", "bytes", "record_count"}
    assert S.leaked_canaries(json.dumps(files, sort_keys=True)) == []


# ══════════════════════════════════════════════════════════════════════════════
#  It binds the three durable events
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_binds_exactly_one_of_each_event(receipt):
    ledger = receipt["ledger"]
    assert ledger["plan_started_count"] == 1
    assert ledger["holdout_commit_count"] == 1
    assert ledger["terminal_count"] == 1
    assert ledger["terminal_event"] == "completed"


def test_the_receipt_binds_the_model_facing_commit_identity(receipt, dataset_root):
    identity = S.pack_identity(dataset_root, S.make_config())
    commit = receipt["holdout_commit"]
    assert commit["pack_identity_hash"] == identity.identity_hash()
    assert commit["order_assignment_hash"] == identity.order_assignment_hash
    assert commit["first_arm"] in {"baseline", "candidate"}
    assert commit["task_count"] == identity.task_count


def test_the_receipt_binds_the_exact_pack_and_target_identities(receipt, dataset_root):
    identity = S.pack_identity(dataset_root, S.make_config())
    assert receipt["holdout"]["task_pack_hash"] == identity.pack_hash
    assert receipt["holdout"]["hidden_target_store_hash"] == \
        identity.hidden_target_store_hash
    assert receipt["plan"]["binds_exact_pack_identity"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  F16, F17, F18 — a receipt refuses to describe an incomplete ceremony
# ══════════════════════════════════════════════════════════════════════════════
def test_a_run_with_no_model_facing_commit_produces_no_receipt(dataset_root, tmp_path,
                                                               monkeypatch):
    """F16. Terminal completion without a commit is not a measurement anyone can cite."""
    import training_gym.evaluation.store as store_module

    root = tmp_path / "nocommit"
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.ok

    # Rewrite the ledger WITHOUT the commit line — the shape a pre-S3Q.0 run has.
    ledger = root / "evaluation_runs.jsonl"
    kept = [line for line in ledger.read_text("utf-8").splitlines()
            if line.strip()
            and json.loads(line).get("event") != store_module.HOLDOUT_COMMIT_EVENT]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")

    with pytest.raises(ReceiptError, match="model-facing commit"):
        build_receipt(outcome.directory, candidate="s3q0-synthetic-candidate",
                      evaluation_source_commit="a" * 40, ledger=ledger)


def test_a_run_with_no_terminal_event_produces_no_receipt(dataset_root, tmp_path):
    """F17."""
    root = tmp_path / "noterminal"
    outcome = S.run_synthetic(dataset_root, root)
    ledger = root / "evaluation_runs.jsonl"
    kept = [line for line in ledger.read_text("utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") != "completed"]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="no terminal line"):
        build_receipt(outcome.directory, candidate="s3q0-synthetic-candidate",
                      evaluation_source_commit="a" * 40, ledger=ledger)


def test_a_run_with_no_start_event_produces_no_receipt(dataset_root, tmp_path):
    """F18."""
    root = tmp_path / "nostart"
    outcome = S.run_synthetic(dataset_root, root)
    ledger = root / "evaluation_runs.jsonl"
    kept = [line for line in ledger.read_text("utf-8").splitlines()
            if line.strip() and json.loads(line).get("event") != "started"]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="start line"):
        build_receipt(outcome.directory, candidate="s3q0-synthetic-candidate",
                      evaluation_source_commit="a" * 40, ledger=ledger)


def test_a_commit_naming_another_pack_produces_no_receipt(dataset_root, tmp_path):
    root = tmp_path / "wrongpack"
    outcome = S.run_synthetic(dataset_root, root)
    ledger = root / "evaluation_runs.jsonl"
    lines = []
    for line in ledger.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("commit"):
            entry["commit"]["task_pack_hash"] = "9" * 64
        lines.append(json.dumps(entry, sort_keys=True))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="different task pack"):
        build_receipt(outcome.directory, candidate="s3q0-synthetic-candidate",
                      evaluation_source_commit="a" * 40, ledger=ledger)


# ══════════════════════════════════════════════════════════════════════════════
#  A receipt grants nothing
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_declares_that_it_authorises_nothing(receipt):
    authority = receipt["authority"]
    assert authority["retry_authorized"] is False
    assert authority["grants_no_further_authority"] is True
    assert receipt["outcome"]["promotes_model"] is False
    assert receipt["outcome"]["activates_model"] is False
    assert receipt["outcome"]["mutates_model_registry"] is False


def test_a_receipt_claiming_an_authority_is_refused(receipt):
    import copy

    for flag in ("retry_authorized", "token_literal_recorded"):
        tampered = copy.deepcopy(receipt)
        tampered["authority"][flag] = True
        assert verify_receipt(seal(
            {k: v for k, v in tampered.items() if k != "receipt_hash"}))


def test_a_synthetic_run_can_never_claim_eligibility(receipt):
    """The property that makes it safe to run this many synthetic ceremonies."""
    assert receipt["execution"]["empirical_status"] == "synthetic_only"
    assert receipt["outcome"]["eligibility"] != "eligible_for_human_review"


def test_an_unrecognised_eligibility_supports_no_evaluated_state():
    """UNKNOWN is not a pass: a verdict the map does not name supports nothing."""
    from scripts.build_m62_eval_receipt import _status_claim

    assert _status_claim("something_new") == ""
    assert _status_claim("") == ""
    assert set(ELIGIBILITY_TO_CANDIDATE_STATE.values()) == {
        "EVALUATED_NOT_ELIGIBLE", "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
        "EVALUATED_NEEDS_MORE_EVIDENCE", "EVALUATED_QUARANTINED"}


# ══════════════════════════════════════════════════════════════════════════════
#  The CLI
# ══════════════════════════════════════════════════════════════════════════════
# S3Q.0.1 MOVED THIS CONTRACT, ON PURPOSE.
#
# The `.1` CLI marked `--generation-directory`, `--candidate` and
# `--evaluation-source-commit` REQUIRED in every mode, so verifying an existing receipt
# meant inventing three build arguments the verifier then ignored (S3Q.0.1 FINDING D).
# A read-only check that demands write-mode arguments teaches operators to supply
# fiction, so the flat parser was replaced by `build` and `verify` subcommands.
#
# `.1` receipts are still READ by the standalone verifier — they are history and history
# stays verifiable — and the two tests below now assert that, rather than asserting an
# invocation the repository deliberately no longer offers.
def test_the_standalone_verifier_still_reads_a_legacy_receipt(receipt, tmp_path,
                                                              capsys):
    from scripts.build_m62_eval_receipt import main
    from scripts.verify_m62_control_plane import canonical_json

    destination = tmp_path / "receipt.json"
    destination.write_text(canonical_json(receipt), encoding="utf-8")
    assert main(["verify", str(destination)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["receipt_version"] == RECEIPT_SCHEMA_VERSION


def test_the_legacy_flat_invocation_is_gone(tmp_path):
    """Verifying no longer requires three arguments the verifier does not use."""
    from scripts.build_m62_eval_receipt import main

    with pytest.raises(SystemExit):
        main(["--generation-directory", str(tmp_path / "absent"),
              "--candidate", "s3q0-synthetic-candidate",
              "--evaluation-source-commit", "a" * 40])


def test_the_builder_cli_refuses_without_a_traceback(tmp_path, capsys):
    from scripts.build_m62_eval_receipt import main

    code = main(["build",
                 "--generation-directory", str(tmp_path / "absent"),
                 "--training-receipt", str(tmp_path / "absent.train.json"),
                 "--adapter-run-directory", str(tmp_path / "absent-adapter"),
                 "--evaluation-config", str(tmp_path / "absent-config.json")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "refused"
    assert "Traceback" not in payload["error"]
