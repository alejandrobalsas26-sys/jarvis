"""V69 M62 S3Q.0.2 — sealing a measurement that already happened.

WHAT WENT WRONG, AND WHY IT IS NOT A BUG REPORT ABOUT THE MEASUREMENT
---------------------------------------------------------------------
The one-shot S3Q LIVE held-out evaluation ran, completed, produced 72 model results over
36 tasks with zero generation errors, and was machine-classified NOT ELIGIBLE. That
measurement is finished and permanently frozen: `eval-v4` is spent, and nothing in this
milestone reruns, rescores or re-generates any part of it.

What failed was the RECEIPT. `m62.eval_receipt.2` had been qualified against synthetic
evidence, and when it met the real artefacts it refused them three times:

  A. it modelled the paired outcome as an exhaustive three-way `wins + ties + losses`
     partition. Production classifies FOUR comparable verdicts, and the fourth --
     `security_improvement` -- is deliberately not a win. 11 + 12 + 10 = 33 against 36
     measured pairs, so a correct measurement was called self-inconsistent;
  B. it required the canonical receipt text to encode as ASCII. A production gate message
     reads `schema validity fell from 1.0000 to 0.8889 (−0.1111)`, with a U+2212 MINUS
     SIGN that `gates.py` typeset correctly. The report is valid; `.2` refused its
     representation;
  C. it derived `evaluation_source_commit` from the repository HEAD at RECEIPT-BUILD
     time. That is the evaluation source only while sealing happens at the unchanged
     evaluated commit -- which is true right up until sealing fails. Repairing the
     receipt requires a commit, and after it `.2` can only name the REPAIR as the
     evaluation source or refuse the truthful assertion.

Every one is reproduced here against `.2` as it stands before being closed by `.3`. A
test that only asserted the fix would pass on a repository where the defect never
existed.

THE DIRECTION OF THE REPAIR
---------------------------
The contract moved to the evidence. Not one report was edited, not one gate message
rewritten, not one minus sign normalised, not one verdict reclassified and not one digest
moved. Several tests below exist purely to keep it that way.

WHAT IS *NOT* HERE
------------------
No eval-v4. No model. No generation. No plan consumption. No live receipt is mutated:
every mutation test works on a throwaway copy.
"""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts.build_m62_eval_receipt import (
    CANONICAL_RECEIPT_ENCODING,
    DECISION_REDERIVER,
    MEASUREMENT_WITNESS_SCHEMA_VERSION,
    RECEIPT_V2_SCHEMA_VERSION,
    RECEIPT_V2_SEAL_FAILURE_CLASSES,
    RECEIPT_V3_SCHEMA_VERSION,
    ReceiptError,
    build_measurement_witness,
    build_receipt_v2,
    build_receipt_v3,
    comparison_partitions,
    emit_receipt,
    main,
    measurement_witness_evidence,
    production_verdicts,
    read_receipt_file,
    seal,
    seal_implementation_identity,
    verify_receipt_payload,
    witness_source_identity,
)
from scripts.verify_m62_control_plane import (
    COMPARISON_VERDICTS,
    EVAL_RECEIPT_V2_SCHEMA_VERSION,
    EVAL_RECEIPT_V3_SCHEMA_PATH,
    EVAL_RECEIPT_V3_SCHEMA_VERSION,
    MEASUREMENT_WITNESS_SCHEMA_PATH,
    MODERN_EVAL_RECEIPT_VERSIONS,
    REPO_ROOT,
    UTF8_CANONICAL_RECEIPT_VERSIONS,
    canonical_bytes,
    canonical_json,
    eval_receipt_v3_schema,
    measurement_witness_schema,
    validate_against_schema,
)

import _s3q02_synthetic as R

#: The U+2212 the real S3Q gate message carries. Written as an escape so this file stays
#: ASCII and the test is about the RECEIPT's encoding rather than its own.
MINUS = "−"


# ══════════════════════════════════════════════════════════════════════════════
#  The world: one completed synthetic evaluation, sealed across a real recovery
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return R.recovery_world(tmp_path_factory)


def _build(world, **overrides):
    kwargs = {
        "training_receipt": world["training_receipt"],
        "adapter_run_directory": world["adapter"]["directory"],
        "evaluation_config": world["config_path"],
        "ledger": world["ledger"],
        "measurement_witness": world["witness_path"],
        "repo_root": world["repo_root"],
    }
    kwargs.update(overrides)
    directory = kwargs.pop("generation_directory", world["directory"])
    return seal(build_receipt_v3(directory, **kwargs))


@pytest.fixture(scope="module")
def receipt(world):
    return _build(world)


def _rehashed(receipt: dict, *path, value) -> dict:
    """Mutate one field and RECOMPUTE the digest.

    Mutating without rehashing only proves the digest works. Rehashing is what asks the
    real question: is this fact CHECKED, or merely recorded?
    """
    from scripts.build_m62_eval_receipt import receipt_hash

    mutated = copy.deepcopy(receipt)
    node = mutated
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    return mutated


def _refused(receipt: dict, *path, value) -> tuple[str, ...]:
    problems = verify_receipt_payload(_rehashed(receipt, *path, value=value))
    assert problems, f"mutating {'.'.join(map(str, path))} was not refused"
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  0 — the version moved rather than the contract being rewritten
# ══════════════════════════════════════════════════════════════════════════════
def test_v1_and_v2_are_untouched_contracts():
    """`.3` is a NEW version. `.1` and `.2` keep their identity and their verifiers."""
    from scripts.build_m62_eval_receipt import (
        RECEIPT_SCHEMA_VERSION,
        _receipt_verifiers,
        verify_receipt,
        verify_receipt_v2,
    )

    verifiers = _receipt_verifiers()
    assert RECEIPT_SCHEMA_VERSION == "m62.eval_receipt.1"
    assert RECEIPT_V2_SCHEMA_VERSION == "m62.eval_receipt.2"
    assert RECEIPT_V3_SCHEMA_VERSION == "m62.eval_receipt.3"
    assert verifiers[RECEIPT_SCHEMA_VERSION][1] is verify_receipt
    assert verifiers[RECEIPT_V2_SCHEMA_VERSION][1] is verify_receipt_v2
    assert set(verifiers) == {RECEIPT_SCHEMA_VERSION, RECEIPT_V2_SCHEMA_VERSION,
                              RECEIPT_V3_SCHEMA_VERSION}


def test_v2_still_builds_and_still_verifies_its_own_evidence(world):
    """The repair did not break the version it repaired. `.2` is still a working contract."""
    payload = seal(build_receipt_v2(
        world["directory"], training_receipt=world["training_receipt"],
        adapter_run_directory=world["adapter"]["directory"],
        evaluation_config=world["config_path"], ledger=world["ledger"],
        repo_root=world["repo_root"]))
    assert payload["schema_version"] == RECEIPT_V2_SCHEMA_VERSION
    assert verify_receipt_payload(payload) == ()


def test_only_v3_redefines_its_canonical_bytes_as_utf8():
    """The UTF-8 definition is scoped to `.3`; `.1` and `.2` keep the ASCII rule."""
    assert UTF8_CANONICAL_RECEIPT_VERSIONS == {EVAL_RECEIPT_V3_SCHEMA_VERSION}
    assert MODERN_EVAL_RECEIPT_VERSIONS == {EVAL_RECEIPT_V2_SCHEMA_VERSION,
                                            EVAL_RECEIPT_V3_SCHEMA_VERSION}


def test_the_published_v3_schema_is_the_one_the_verifier_enforces():
    """Two writable copies of one contract is how they drift."""
    assert (REPO_ROOT / EVAL_RECEIPT_V3_SCHEMA_PATH).read_bytes() == \
        canonical_bytes(eval_receipt_v3_schema())
    assert (REPO_ROOT / MEASUREMENT_WITNESS_SCHEMA_PATH).read_bytes() == \
        canonical_bytes(measurement_witness_schema())


# ══════════════════════════════════════════════════════════════════════════════
#  FINDING A — the three-way partition production never had
# ══════════════════════════════════════════════════════════════════════════════
def test_finding_a_v2_refuses_a_run_carrying_a_fourth_verdict(world):
    """REPRODUCED. `.2` calls a correct four-verdict measurement self-inconsistent.

    Built on `.2`'s own synthetic receipt so the defect is shown in `.2`'s code rather
    than described: give the results block a security-improvement class of its own and
    `wins + ties + losses` stops accounting for every pair, exactly as it does on the
    real S3Q run.
    """
    from scripts.build_m62_eval_receipt import receipt_hash, verify_receipt_v2

    payload = seal(build_receipt_v2(
        world["directory"], training_receipt=world["training_receipt"],
        adapter_run_directory=world["adapter"]["directory"],
        evaluation_config=world["config_path"], ledger=world["ledger"],
        repo_root=world["repo_root"]))
    assert verify_receipt_v2(payload) == ()

    # Three of the pairs were security improvements, so they are not wins.
    mutated = copy.deepcopy(payload)
    mutated["results"]["wins"] = mutated["results"]["wins"] - 3
    mutated.pop("receipt_hash")
    mutated["receipt_hash"] = receipt_hash(mutated)
    problems = verify_receipt_v2(mutated)
    assert any("wins, ties and losses do not account for every measured pair" in p
               for p in problems), problems


def test_finding_a_v3_partitions_over_the_production_vocabulary(receipt):
    """CLOSED. Every measured pair carries exactly one canonical verdict."""
    results = receipt["results"]
    counts = results["verdict_counts"]
    assert tuple(sorted(counts)) == production_verdicts()
    assert sum(counts.values()) == results["measured_pairs"]
    assert results["verdict_vocabulary"] == list(production_verdicts())
    assert verify_receipt_payload(receipt) == ()


def test_the_verdict_vocabulary_is_re_derived_from_production_not_restated():
    """A literal nobody re-derives is a second writable copy of a contract."""
    from training_gym.evaluation.comparison import ComparisonVerdict

    assert tuple(sorted(COMPARISON_VERDICTS)) == \
        tuple(sorted(v.value for v in ComparisonVerdict)) == production_verdicts()
    assert "security_improvement" in production_verdicts()


def test_security_improvement_is_never_folded_into_improved(receipt):
    """The fourth verdict is REPORTED, never rewarded. Folding it in would be a promotion
    of a safety fix into a quality win, which production explicitly refuses."""
    counts = receipt["results"]["verdict_counts"]
    assert receipt["results"]["wins"] == counts["improved"]
    assert counts["security_improvement"] >= 0
    assert receipt["results"]["wins"] != counts["improved"] + \
        counts["security_improvement"] or counts["security_improvement"] == 0


def test_wins_ties_losses_are_declared_partial_and_are_not_summed(receipt):
    """`.3` keeps the aliases and holds them to being aliases."""
    assert receipt["results"]["wins_ties_losses_are_a_partial_partition"] is True
    problems = _refused(receipt, "results",
                        "wins_ties_losses_are_a_partial_partition", value=False)
    # The strict schema pins it to `True` and refuses first; the semantic rule is the
    # second, independent opinion on the same claim.
    assert any("must be True" in p or "PARTIAL partition" in p
               for p in problems), problems
    from scripts.build_m62_eval_receipt import _verdict_partition_problems

    results = dict(copy.deepcopy(receipt["results"]),
                   wins_ties_losses_are_a_partial_partition=False)
    assert any("PARTIAL partition" in p
               for p in _verdict_partition_problems(results))


@pytest.mark.parametrize("alias,verdict", [("wins", "improved"), ("ties", "unchanged"),
                                           ("losses", "regressed")])
def test_each_alias_must_equal_the_verdict_it_names(receipt, alias, verdict):
    problems = _refused(receipt, "results", alias,
                        value=receipt["results"][alias] + 1)
    assert any(verdict in p for p in problems), problems


@pytest.mark.parametrize("verdict", sorted(COMPARISON_VERDICTS))
def test_every_verdict_count_is_checked_against_the_total(receipt, verdict):
    """Non-vacuity, one class at a time: moving ANY bucket breaks the sum."""
    mutated = copy.deepcopy(receipt["results"]["verdict_counts"])
    mutated[verdict] = mutated[verdict] + 1
    problems = _refused(receipt, "results", "verdict_counts", value=mutated)
    assert any("sum to" in p or "alias" in p or "is the same number" in p
               for p in problems), problems


def test_a_partition_missing_a_verdict_is_refused(receipt):
    partial = {k: v for k, v in receipt["results"]["verdict_counts"].items()
               if k != "not_comparable"}
    problems = verify_receipt_payload(_rehashed(receipt, "results", "verdict_counts",
                                                value=partial))
    assert problems


def test_a_partition_carrying_an_unknown_verdict_is_refused(receipt):
    invented = dict(receipt["results"]["verdict_counts"])
    invented["spectacularly_improved"] = 0
    problems = verify_receipt_payload(_rehashed(receipt, "results", "verdict_counts",
                                                value=invented))
    assert problems


def test_the_builder_refuses_a_partition_that_does_not_cover_the_run(world):
    """A guard at BUILD time as well as verify time: the two are different readers."""
    with pytest.raises(ReceiptError, match="account for"):
        comparison_partitions(world["directory"],
                              measured_pairs=world["witness_payload"]["results"]
                              ["measured_pairs"] + 1)


def test_the_partition_is_read_from_production_verdicts_never_reclassified(world):
    """`comparison_partitions` READS the verdict `comparison.py` already assigned.

    Asserted by construction: every count it returns is reproduced by tallying the
    `verdict` field, with no branch on rewards, statuses or security findings. A second
    classification algorithm in the receipt layer would give the repository two opinions
    about what a win is.
    """
    tallied: dict[str, int] = dict.fromkeys(production_verdicts(), 0)
    for line in (Path(world["directory"]) / "paired-comparisons.jsonl").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            tallied[json.loads(line)["verdict"]] += 1
    measured = world["witness_payload"]["results"]["measured_pairs"]
    assert comparison_partitions(world["directory"],
                                 measured_pairs=measured)["verdict_counts"] == tallied


# ══════════════════════════════════════════════════════════════════════════════
#  Section 11 — the numeric partition is a DIFFERENT partition
# ══════════════════════════════════════════════════════════════════════════════
def test_the_numeric_delta_partition_is_carried_under_its_own_name(receipt):
    numeric = receipt["results"]["numeric_delta_counts"]
    assert sorted(numeric) == ["negative", "positive", "zero"]
    assert sum(numeric.values()) == receipt["results"]["measured_pairs"]


def test_the_two_partitions_are_not_asserted_equal_bucket_for_bucket(receipt):
    """Only the TOTALS agree. A pair can improve numerically and be classified
    `security_improvement`, and one classified `unchanged` can carry a non-zero delta."""
    verdicts = receipt["results"]["verdict_counts"]
    numeric = receipt["results"]["numeric_delta_counts"]
    assert sum(verdicts.values()) == sum(numeric.values())
    # Shifting a numeric bucket while the total holds must NOT be refused by the verdict
    # rule -- the two partitions are independent.
    shifted = {"positive": numeric["positive"] + 1, "zero": numeric["zero"] - 1,
               "negative": numeric["negative"]}
    if numeric["zero"] > 0:
        assert verify_receipt_payload(
            _rehashed(receipt, "results", "numeric_delta_counts", value=shifted)) == ()


def test_the_numeric_partition_must_still_cover_every_pair(receipt):
    numeric = dict(receipt["results"]["numeric_delta_counts"])
    numeric["positive"] += 1
    problems = _refused(receipt, "results", "numeric_delta_counts", value=numeric)
    assert any("numeric delta counts sum to" in p for p in problems), problems


def test_a_numeric_partition_with_the_wrong_buckets_is_refused(receipt):
    problems = _refused(receipt, "results", "numeric_delta_counts",
                        value={"up": 1, "down": 2})
    assert problems


# ══════════════════════════════════════════════════════════════════════════════
#  FINDING B — ASCII-only refused legitimate production evidence
# ══════════════════════════════════════════════════════════════════════════════
def _with_unicode_gate(receipt: dict, message: str) -> dict:
    """Put *message* into the receipt's gate evidence and re-derive everything downstream.

    Deliberately goes through the production digest and decision functions rather than
    hand-editing: a Unicode test that bypassed them would be testing a string, not a
    receipt.
    """
    from scripts.build_m62_eval_receipt import _sha256_obj, receipt_hash
    from training_gym.evaluation.reports import decision_from_evidence

    mutated = copy.deepcopy(receipt)
    evidence = mutated["decision_evidence"]
    findings = evidence["gate_report"]["findings"]
    assert findings, "the fixture must carry at least one gate finding to retype"
    findings[0]["message"] = message
    decision = decision_from_evidence(
        gate_report=evidence["gate_report"], bootstrap=evidence["bootstrap"],
        empirical_status=evidence["empirical_status"],
        run_state=evidence["report_serialization_state"])
    evidence["canonical_decision"] = decision.to_dict()
    evidence["decision_hash"] = decision.decision_hash()
    mutated["evidence"]["gate_report_hash"] = _sha256_obj(evidence["gate_report"])
    mutated["outcome"]["gate_blockers"] = [str(b) for b in decision.blockers]
    mutated["outcome"]["gate_warnings"] = [str(w) for w in decision.warnings]
    text = canonical_json({"gate_report": evidence["gate_report"],
                           "bootstrap": evidence["bootstrap"],
                           "canonical_decision": evidence["canonical_decision"]})
    observed = sorted({f"U+{ord(ch):04X}" for ch in text if ord(ch) > 127})
    evidence["non_ascii_codepoints"] = observed
    evidence["carries_non_ascii_decision_text"] = bool(observed)
    mutated.pop("receipt_hash", None)
    mutated["receipt_hash"] = receipt_hash(mutated)
    return mutated


def test_finding_b_v2_refuses_a_receipt_carrying_a_real_gate_minus_sign(world):
    """REPRODUCED. `.2` refuses the representation of a valid report."""
    from scripts.build_m62_eval_receipt import receipt_hash, verify_receipt_v2

    payload = seal(build_receipt_v2(
        world["directory"], training_receipt=world["training_receipt"],
        adapter_run_directory=world["adapter"]["directory"],
        evaluation_config=world["config_path"], ledger=world["ledger"],
        repo_root=world["repo_root"]))
    findings = payload["decision_evidence"]["gate_report"]["findings"]
    if not findings:
        pytest.skip("the synthetic run produced no gate finding to retype")
    findings[0]["message"] = f"schema validity fell from 1.0000 to 0.8889 ({MINUS}0.1111)"
    payload.pop("receipt_hash")
    payload["receipt_hash"] = receipt_hash(payload)
    problems = verify_receipt_payload(payload)
    assert any("not ASCII" in p for p in problems), problems


def test_finding_b_v3_preserves_the_minus_sign_exactly(receipt):
    """CLOSED. The evidence survives verification unchanged, character for character."""
    message = f"schema validity fell from 1.0000 to 0.8889 ({MINUS}0.1111)"
    mutated = _with_unicode_gate(receipt, message)
    assert verify_receipt_payload(mutated) == ()
    assert mutated["decision_evidence"]["gate_report"]["findings"][0]["message"] == message
    assert MINUS in canonical_json(mutated)
    assert "U+2212" in mutated["decision_evidence"]["non_ascii_codepoints"]


def test_an_ascii_gate_message_still_passes(receipt):
    """Unicode is PERMITTED, not required."""
    mutated = _with_unicode_gate(receipt, "schema validity fell from 1.0000 to 0.8889")
    assert verify_receipt_payload(mutated) == ()
    assert mutated["decision_evidence"]["carries_non_ascii_decision_text"] is False
    assert mutated["decision_evidence"]["non_ascii_codepoints"] == []


def test_accented_unicode_in_a_gate_message_passes(receipt):
    """Permitted by the canonical contract: the rule is UTF-8, not a codepoint allowlist."""
    mutated = _with_unicode_gate(receipt, "schéma validity fell to 0.8889")
    assert verify_receipt_payload(mutated) == ()
    assert mutated["decision_evidence"]["non_ascii_codepoints"] == ["U+00E9"]


def test_a_different_unicode_string_is_a_genuinely_different_receipt(receipt):
    """Two receipts differing only in a codepoint must have different digests."""
    one = _with_unicode_gate(receipt, f"validity fell ({MINUS}0.1111)")
    two = _with_unicode_gate(receipt, "validity fell (-0.1111)")
    assert one["receipt_hash"] != two["receipt_hash"]
    assert canonical_bytes(one) != canonical_bytes(two)
    assert verify_receipt_payload(one) == verify_receipt_payload(two) == ()


def test_the_unicode_inventory_may_not_drift_from_the_evidence(receipt):
    """The receipt states which non-ASCII codepoints its decision evidence carries, and
    that statement is checked against the evidence rather than believed."""
    mutated = _with_unicode_gate(receipt, f"validity fell ({MINUS}0.1111)")
    problems = _refused(mutated, "decision_evidence", "non_ascii_codepoints", value=[])
    assert any("Unicode inventory" in p for p in problems), problems
    problems = _refused(mutated, "decision_evidence",
                        "carries_non_ascii_decision_text", value=False)
    assert problems


def test_a_malformed_byte_sequence_is_refused(tmp_path, receipt):
    """Not valid UTF-8 is not a receipt. Refused at read, before any verification."""
    target = tmp_path / "broken.json"
    target.write_bytes(canonical_bytes(receipt)[:-40] + b"\xff\xfe\x00 not json")
    with pytest.raises(ReceiptError):
        read_receipt_file(target)


def test_the_receipt_names_its_own_encoding(receipt):
    assert receipt["canonical_encoding"] == CANONICAL_RECEIPT_ENCODING
    assert "UTF-8" in CANONICAL_RECEIPT_ENCODING
    problems = _refused(receipt, "canonical_encoding", value="whatever the reader likes")
    assert any("encoding" in p for p in problems), problems


def test_unicode_support_does_not_weaken_the_token_scanner(receipt):
    """The scanners are unchanged. A plan token is a refusal in any script."""
    plan_hash = receipt["plan"]["plan_hash"]
    problems = _refused(receipt, "outcome", "limitations",
                        value=[f"EVAL:{plan_hash}"])
    assert any("spendable plan token" in p for p in problems), problems


def test_unicode_support_does_not_weaken_the_private_path_scanner(receipt):
    problems = _refused(receipt, "outcome", "limitations",
                        value=["measured under /home/someone/.cache/models"])
    assert any("private host path" in p for p in problems), problems


def test_unicode_support_does_not_weaken_the_holdout_body_scanner(receipt):
    from scripts.verify_m62_control_plane import FORBIDDEN_BODY_SYMBOLS

    symbol = sorted(FORBIDDEN_BODY_SYMBOLS)[0]
    problems = _refused(receipt, "outcome", "limitations", value=[f"see {symbol}"])
    assert any("body source" in p for p in problems), problems


# ══════════════════════════════════════════════════════════════════════════════
#  FINDING C — one field cannot hold two sources
# ══════════════════════════════════════════════════════════════════════════════
def test_finding_c_v2_binds_the_repair_commit_as_the_evaluation_source(world):
    """REPRODUCED, on the real recovery topology.

    HEAD is the repair commit. `.2`'s `source_identity` reports it as the EVALUATION
    source -- a commit that measured nothing.
    """
    from scripts.build_m62_eval_receipt import source_identity

    derived = source_identity(world["repo_root"])
    assert derived["evaluation_source_commit"] == world["seal_commit"]
    assert derived["evaluation_source_commit"] != world["evaluation_source_commit"]
    assert "seal_implementation_source_commit" not in derived


def test_finding_c_v2_refuses_the_truthful_assertion_after_a_repair(world):
    """The other half of the same defect: a caller stating the TRUE evaluation source is
    told it is wrong."""
    with pytest.raises(ReceiptError, match="this worktree is at"):
        build_receipt_v2(
            world["directory"], training_receipt=world["training_receipt"],
            adapter_run_directory=world["adapter"]["directory"],
            evaluation_config=world["config_path"], ledger=world["ledger"],
            repo_root=world["repo_root"],
            expected_evaluation_source_commit=world["evaluation_source_commit"])


def test_finding_c_v3_separates_the_two_sources(receipt, world):
    """CLOSED. Two fields, two commits, and neither derived from the other."""
    assert receipt["evaluation_source"]["evaluation_source_commit"] == \
        world["evaluation_source_commit"]
    assert receipt["seal_implementation_source"]["seal_implementation_source_commit"] == \
        world["seal_commit"]
    assert receipt["evaluation_source"]["evaluation_source_commit"] != \
        receipt["seal_implementation_source"]["seal_implementation_source_commit"]
    assert receipt["seal_implementation_source"]["differs_from_evaluation_source"] is True
    assert receipt["evaluation_source"]["derived_from"] == "measurement_witness"


def test_v3_accepts_the_truthful_assertion_v2_refused(world):
    """The assertion is now checked against the WITNESS, not against HEAD."""
    payload = _build(world,
                     expected_evaluation_source_commit=world["evaluation_source_commit"])
    assert payload["evaluation_source"]["evaluation_source_commit"] == \
        world["evaluation_source_commit"]


def test_v3_still_refuses_an_untrue_source_assertion(world):
    with pytest.raises(ReceiptError, match="pre-repair witness records"):
        _build(world, expected_evaluation_source_commit="f" * 40)


def test_the_seal_source_may_not_come_from_an_unclean_worktree(world, tmp_path):
    """A receipt built over uncommitted code names a source nobody else can obtain."""
    root = Path(world["repo_root"])
    scratch = root / "jarvis" / "training_gym" / "UNCOMMITTED.md"
    scratch.write_text("dirty\n", encoding="utf-8")
    try:
        with pytest.raises(ReceiptError, match="worktree is not clean"):
            seal_implementation_identity(root, evaluation_source_commit="0" * 40)
    finally:
        scratch.unlink()


def test_the_claim_about_whether_the_sources_differ_is_checked(receipt):
    problems = _refused(receipt, "seal_implementation_source",
                        "differs_from_evaluation_source", value=False)
    assert problems


def test_the_receipt_may_not_claim_head_produced_its_evaluation_source(receipt):
    problems = _refused(receipt, "evaluation_source", "derived_from",
                        value="repository_head")
    assert problems


def test_no_cryptographic_execution_attestation_is_claimed(receipt):
    """Section 17. Provenance is what the topology establishes; authenticity is not."""
    level = receipt["evaluation_source"]["evidence_level"]
    assert "not proof of which bytes executed" in level
    text = canonical_json(receipt).lower()
    for overclaim in ("signature", "signed by", "pki", "attestation", "certificate"):
        assert overclaim not in text, overclaim


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 8, 9, 15 — the witness, and the topology that fixes the source
# ══════════════════════════════════════════════════════════════════════════════
def test_the_witness_is_written_before_the_repair_and_parented_on_the_source(world):
    parents = subprocess.run(
        ["git", "-C", str(world["repo_root"]), "rev-list", "--parents", "-n", "1",
         world["witness_commit"]], capture_output=True, text=True, check=True
    ).stdout.split()
    assert parents[1] == world["evaluation_source_commit"]


def test_the_witness_grants_nothing(world):
    grants = world["witness_payload"]["grants"]
    assert grants == {"candidate_state": False, "promotion": False, "activation": False,
                      "registry_mutation": False, "retry_or_rerun": False,
                      "is_an_evaluation_receipt": False,
                      "note": "a witness records facts; it authorises nothing"}
    assert validate_against_schema(measurement_witness_schema(),
                                   world["witness_payload"]) == []


def test_a_witness_that_grants_something_is_refused(world):
    mutated = R.reseal_witness({**copy.deepcopy(world["witness_payload"]),
                                "grants": {**world["witness_payload"]["grants"],
                                           "candidate_state": True}})
    path = Path(world["repo_root"]) / "granting-witness.json"
    path.write_text(canonical_json(mutated), encoding="utf-8")
    try:
        with pytest.raises(ReceiptError, match="authorises nothing"):
            measurement_witness_evidence(path, repo_root=Path(world["repo_root"]))
    finally:
        path.unlink()


def test_the_witness_carries_no_body_no_token_and_no_private_path(world):
    from scripts.verify_m62_control_plane import (
        EVAL_V4_TASK_IDS,
        FORBIDDEN_BODY_SYMBOLS,
        PRIVATE_PATH_RE,
        TOKEN_LITERAL_RE,
    )

    text = canonical_json(world["witness_payload"])
    assert not TOKEN_LITERAL_RE.search(text)
    assert PRIVATE_PATH_RE.findall(text) == []
    assert [s for s in FORBIDDEN_BODY_SYMBOLS if s in text] == []
    assert sorted({t for t in EVAL_V4_TASK_IDS if t in text}) == []


def test_the_witness_records_why_a_second_evidence_form_was_needed(world):
    assert tuple(world["witness_payload"]["receipt_v2_seal_failure_classes"]) == \
        RECEIPT_V2_SEAL_FAILURE_CLASSES
    assert len(RECEIPT_V2_SEAL_FAILURE_CLASSES) == 3


def test_the_witness_is_reproducible_from_tracked_code(world):
    """Rebuilding from the same generation and the same source block reproduces the bytes."""
    rebuilt = build_measurement_witness(
        world["directory"], ledger=world["ledger"],
        training_receipt=world["training_receipt"],
        evaluation_source=world["source_identity"], repo_root=world["repo_root"])
    assert rebuilt == world["witness_payload"]
    assert canonical_json(rebuilt) == world["witness_path"].read_text(encoding="utf-8")


def test_a_witness_written_over_an_unclean_worktree_is_refused(world):
    root = Path(world["repo_root"])
    scratch = root / "jarvis" / "training_gym" / "UNCOMMITTED2.md"
    scratch.write_text("dirty\n", encoding="utf-8")
    try:
        with pytest.raises(ReceiptError, match="worktree is not clean"):
            witness_source_identity(root)
    finally:
        scratch.unlink()


def test_the_receipt_binds_the_witness_by_digest_and_by_commit(receipt, world):
    bound = receipt["measurement_witness"]
    assert bound["measurement_witness_commit"] == world["witness_commit"]
    assert bound["measurement_witness_hash"] == world["witness_payload"]["witness_hash"]
    assert bound["witness_schema_version"] == MEASUREMENT_WITNESS_SCHEMA_VERSION
    assert bound["witness_first_parent_is_evaluation_source"] is True
    assert bound["measurement_witness_sha256"] == __import__("hashlib").sha256(
        world["witness_path"].read_bytes()).hexdigest()


def _variant(world, name, **mutations):
    """A resealed witness variant, written inside the repository and gitignored."""
    payload = copy.deepcopy(world["witness_payload"])
    for path, value in mutations.items():
        node = payload
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return R.witness_variant(world, R.reseal_witness(payload), name)


def test_the_receipt_requires_the_witness_to_describe_this_measurement(world):
    """A witness for another run may not name this one's source."""
    path = _variant(world, "foreign", evaluation_id="some-other-evaluation")
    try:
        with pytest.raises(ReceiptError, match="describes"):
            _build(world, measurement_witness=path)
    finally:
        path.unlink()


def test_a_witness_for_another_candidate_is_refused(world):
    path = _variant(world, "othercandidate", candidate_id="somebody-elses-candidate")
    try:
        with pytest.raises(ReceiptError, match="candidate"):
            _build(world, measurement_witness=path)
    finally:
        path.unlink()


@pytest.mark.parametrize("field,label", [
    ("evidence.report_hash", "report hash"),
    ("evidence.evaluation_manifest_hash", "evaluation manifest hash"),
    ("evidence.evaluation_artifact_tree_hash", "artifact tree hash"),
    ("evidence.comparison_manifest_hash", "comparison manifest hash"),
    ("evidence.metrics_summary_hash", "metrics summary hash"),
    ("plan.plan_hash", "plan hash"),
])
def test_each_witness_artifact_identity_must_match_the_runtime(world, field, label):
    """Section 15: witness identity == runtime identity, for every one of them."""
    path = _variant(world, field.replace(".", "-"), **{field: "d" * 64})
    try:
        with pytest.raises(ReceiptError, match=label):
            _build(world, measurement_witness=path)
    finally:
        path.unlink()


@pytest.mark.parametrize("field,label", [
    ("ledger.plan_started_event_hash", "plan-start event hash"),
    ("ledger.holdout_commit_event_hash", "holdout-commit event hash"),
    ("ledger.terminal_event_hash", "terminal event hash"),
])
def test_each_witness_ledger_event_hash_must_match_the_runtime(world, field, label):
    path = _variant(world, field.replace(".", "-"), **{field: "e" * 64})
    try:
        with pytest.raises(ReceiptError, match=label):
            _build(world, measurement_witness=path)
    finally:
        path.unlink()


def test_a_witness_whose_own_digest_is_wrong_is_refused(world):
    broken = copy.deepcopy(world["witness_payload"])
    broken["witness_hash"] = "0" * 64
    path = R.witness_variant(world, broken, "unsealed")
    try:
        with pytest.raises(ReceiptError, match="own digest"):
            measurement_witness_evidence(path, repo_root=Path(world["repo_root"]))
    finally:
        path.unlink()


def test_a_witness_declaring_the_wrong_schema_is_refused(world):
    path = _variant(world, "wrongschema", schema_version="m62.measurement_witness.99")
    try:
        with pytest.raises(ReceiptError, match="declares schema"):
            measurement_witness_evidence(path, repo_root=Path(world["repo_root"]))
    finally:
        path.unlink()


def test_an_uncommitted_witness_cannot_bridge_a_repair(world):
    """A bridge Git does not carry cannot cross a commit."""
    path = R.witness_variant(world, world["witness_payload"], "untracked")
    try:
        with pytest.raises(ReceiptError, match="could not be derived"):
            measurement_witness_evidence(path, repo_root=Path(world["repo_root"]))
    finally:
        path.unlink()


def test_a_witness_outside_the_repository_is_refused(world, tmp_path):
    """Portable evidence may not point at a host-local file."""
    path = tmp_path / "outside-witness.json"
    path.write_text(canonical_json(world["witness_payload"]), encoding="utf-8")
    with pytest.raises(ReceiptError, match="outside the repository"):
        measurement_witness_evidence(path, repo_root=Path(world["repo_root"]))


def test_a_witness_edited_after_its_commit_is_refused(world):
    """The blob its own commit recorded, or it is not the document that was witnessed."""
    original = world["witness_path"].read_bytes()
    try:
        world["witness_path"].write_text(
            canonical_json(R.reseal_witness(
                {**copy.deepcopy(world["witness_payload"]), "milestone": "S9Z"})),
            encoding="utf-8")
        with pytest.raises(ReceiptError, match="blob its own commit recorded"):
            measurement_witness_evidence(world["witness_path"],
                                         repo_root=Path(world["repo_root"]))
    finally:
        world["witness_path"].write_bytes(original)


# ══════════════════════════════════════════════════════════════════════════════
#  Section 18 — every `.2` guarantee is carried forward
# ══════════════════════════════════════════════════════════════════════════════
def test_the_candidate_identity_is_rooted_in_the_training_receipt(receipt):
    assert receipt["candidate"]["identity_source"] == "training_receipt"
    assert receipt["training_receipt"]["candidate_id"] == \
        receipt["candidate"]["candidate_id"]
    problems = _refused(receipt, "candidate", "identity_source", value="caller")
    assert problems


@pytest.mark.parametrize("field", ["adapter_sha256", "adapter_manifest_hash",
                                   "adapter_artifact_set_hash",
                                   "adapter_reference_hash"])
def test_every_adapter_identity_is_mandatory_and_non_empty(receipt, field):
    assert len(receipt["candidate"][field]) == 64
    problems = _refused(receipt, "candidate", field, value="")
    assert problems


def test_one_plan_one_crossing_one_terminal(receipt):
    ledger = receipt["ledger"]
    assert ledger["plan_started_count"] == ledger["holdout_commit_count"] == 1
    assert ledger["terminal_count"] == ledger["unique_plan_hashes"] == 1
    for field in ("plan_started_count", "holdout_commit_count", "terminal_count",
                  "unique_plan_hashes"):
        assert verify_receipt_payload(_rehashed(receipt, "ledger", field, value=2))


def test_the_timeout_truth_is_unchanged(receipt):
    """D33 stays open and stays stated."""
    assert receipt["policies"]["timeout_enforced"] is False
    problems = _refused(receipt, "policies", "timeout_enforced", value=True)
    assert any("must be False" in p or "ENFORCED" in p for p in problems), problems


def test_the_generation_policy_is_rederivable_not_quoted(receipt):
    from training_gym.evaluation.generation import GenerationPolicy

    assert GenerationPolicy.from_dict(
        receipt["policies"]["generation_policy"]).policy_hash() == \
        receipt["policies"]["generation_policy_hash"]
    mutated = dict(receipt["policies"]["generation_policy"])
    mutated["max_new_tokens"] = mutated["max_new_tokens"] + 1
    problems = _refused(receipt, "policies", "generation_policy", value=mutated)
    assert problems


def test_the_eligibility_is_rederived_by_the_production_algorithm(receipt):
    assert receipt["decision_evidence"]["rederived_by"] == DECISION_REDERIVER
    problems = _refused(receipt, "decision_evidence", "rederived_by",
                        value="scripts.build_m62_eval_receipt.verify_receipt_v3")
    assert problems
    problems = _refused(receipt, "outcome", "eligibility", value="eligible_for_human_review")
    assert problems


def test_the_receipt_grants_nothing(receipt):
    for flag in ("promotes_model", "activates_model", "mutates_model_registry"):
        assert receipt["outcome"][flag] is False
        assert verify_receipt_payload(_rehashed(receipt, "outcome", flag, value=True))
    assert receipt["authority"]["retry_authorized"] is False
    assert receipt["authority"]["grants_no_further_authority"] is True


def test_the_receipt_carries_no_confirmation_literal(receipt):
    from scripts.verify_m62_control_plane import TOKEN_LITERAL_RE

    assert receipt["authority"]["token_literal_recorded"] is False
    assert not TOKEN_LITERAL_RE.search(canonical_json(receipt))


def test_the_receipt_names_no_individual_holdout_task(receipt):
    from scripts.verify_m62_control_plane import EVAL_V4_TASK_IDS

    text = canonical_json(receipt)
    assert sorted({t for t in EVAL_V4_TASK_IDS if t in text}) == []
    assert "first_task_id" not in receipt["holdout_commit"]


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 19, 22 — sealing is not measuring
# ══════════════════════════════════════════════════════════════════════════════
def test_the_receipt_states_that_it_sealed_an_existing_measurement(receipt):
    execution = receipt["execution"]
    assert execution["sealed_from_existing_measurement"] is True
    assert execution["model_loads_during_seal"] == 0
    assert execution["model_generations_during_seal"] == 0
    for field in ("model_loads_during_seal", "model_generations_during_seal"):
        problems = _refused(receipt, "execution", field, value=1)
        assert any("must be 0" in p or "loads no model" in p
                   for p in problems), problems


def test_sealing_consumed_no_authority(receipt):
    assert receipt["authority"]["seal_consumed_no_authority"] is True
    assert receipt["authority"]["spent_by_the_run_this_receipt_describes"] is True
    problems = _refused(receipt, "authority", "seal_consumed_no_authority", value=False)
    assert any("must be True" in p or "consumed no authority" in p
               for p in problems), problems


def test_building_a_receipt_starts_no_run_and_appends_no_ledger_line(world):
    """The strongest available statement that sealing is inert: the durable record does
    not move, and neither does the measurement."""
    ledger = Path(world["ledger"])
    before = ledger.read_bytes()
    directory = Path(world["directory"])
    artefacts = {p.name: p.read_bytes() for p in sorted(directory.iterdir())}
    _build(world)
    assert ledger.read_bytes() == before
    assert {p.name: p.read_bytes() for p in sorted(directory.iterdir())} == artefacts


def test_the_builder_only_shells_out_to_read_only_git():
    """`.3` added Git calls to the BUILDER, so the builder gets the guard the verifier has.

    `rev-list` walks parents, `hash-object` re-derives a tracked blob's oid, `rev-parse`
    resolves refs, `ls-files` lists tracked paths and `status` reports cleanliness. None
    writes -- with the single caveat that `hash-object` is read-only only WITHOUT `-w`,
    which is asserted rather than assumed.
    """
    import ast

    source = Path(__import__("scripts.build_m62_eval_receipt",
                             fromlist=["x"]).__file__).read_text(encoding="utf-8")
    read_only = {"rev-parse", "rev-list", "hash-object", "ls-files", "status",
                 "cat-file", "merge-base"}
    calls = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_git":
            # `_git(repo_root, "<subcommand>", ...)` -- the subcommand is argument two.
            subcommand = node.args[1]
            assert isinstance(subcommand, ast.Constant) and \
                subcommand.value in read_only, ast.dump(node)
            calls += 1
            if subcommand.value == "hash-object":
                literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
                assert "-w" not in literals, ast.dump(node)
    assert calls, "the guard found no git calls to guard"
    assert "shell=True" not in source
    # The AST allowlist above is the guard. A raw string scan is deliberately NOT added
    # beside it: `"commit"` is a legitimate ledger dict key in this module, and a scan
    # that has to be excepted for its own false positives teaches people to widen it.


def test_the_builder_never_imports_a_backend_or_a_model(world):
    """No generation path is reachable from a seal. Asserted by absence, in the module."""
    source = Path(__import__("scripts.build_m62_eval_receipt", fromlist=["x"]).__file__)
    text = source.read_text(encoding="utf-8")
    for forbidden in ("AutoModel", "from_pretrained", "torch.load", "generate("):
        assert forbidden not in text, forbidden


# ══════════════════════════════════════════════════════════════════════════════
#  Sections 26, 27 — standalone verification and non-vacuity
# ══════════════════════════════════════════════════════════════════════════════
def test_verification_needs_no_runtime_tree_no_adapter_and_no_holdout(receipt, tmp_path):
    """Section 26. A clean clone holds the receipt and nothing else."""
    target = tmp_path / "portable.json"
    emit_receipt(target, canonical_json(receipt))
    assert verify_receipt_payload(read_receipt_file(target)) == ()
    assert main(["verify", str(target)]) == 0


def test_an_unknown_schema_version_is_refused(receipt):
    problems = verify_receipt_payload({**copy.deepcopy(receipt),
                                       "schema_version": "m62.eval_receipt.99",
                                       "receipt_version": "m62.eval_receipt.99"})
    assert any("not one this repository knows how to verify" in p for p in problems)


def test_disagreeing_version_fields_are_refused(receipt):
    problems = verify_receipt_payload({**copy.deepcopy(receipt),
                                       "receipt_version": RECEIPT_V2_SCHEMA_VERSION})
    assert any("disagree" in p for p in problems), problems


def test_an_extra_property_is_refused(receipt):
    from scripts.build_m62_eval_receipt import receipt_hash

    mutated = copy.deepcopy(receipt)
    mutated["promoted_by"] = "nobody"
    mutated.pop("receipt_hash")
    mutated["receipt_hash"] = receipt_hash(mutated)
    problems = verify_receipt_payload(mutated)
    assert any("unknown key" in p for p in problems), problems


def test_an_edited_receipt_whose_digest_was_not_recomputed_is_refused(receipt):
    mutated = copy.deepcopy(receipt)
    mutated["results"]["measured_pairs"] = 1
    problems = verify_receipt_payload(mutated)
    assert any("receipt_hash" in p for p in problems), problems


# ── the two surfaces a mutation can be refused by, and which owns which fact ──
#
# A receipt is checked in two places, deliberately:
#
#   * `verify_receipt_payload` is STANDALONE. It reads one file and nothing else, so it
#     can only check facts the document establishes about itself. That is the point: it
#     has to run in a clean clone with no runtime tree, no adapter and no eval-v4.
#   * `_check_seal_recovery_receipt` is the CONTROL PLANE's half. It reaches outside --
#     to the tracked witness, to Git, to the production verdict vocabulary -- and owns
#     every fact whose truth lives somewhere other than the receipt.
#
# Splitting the non-vacuity table the same way is not a concession. A test that asserted
# the standalone verifier catches an external binding would be asserting the wrong
# architecture, and one that only ran the standalone verifier would leave every external
# binding untested. Both surfaces are exercised below, and between them every bound fact
# is CHECKED rather than merely recorded.

def _control_plane_problems(monkeypatch, world, receipt: dict) -> list[str]:
    """Run the control plane's external half against the synthetic repository."""
    import scripts.verify_m62_control_plane as V

    monkeypatch.setattr(V, "REPO_ROOT", Path(world["repo_root"]))
    report = V.Report()
    V._check_seal_recovery_receipt(report, receipt=receipt,
                                   cid=receipt["candidate"]["candidate_id"])
    return [message for _category, message in report.problems]


def test_the_control_plane_accepts_the_unmutated_receipt(monkeypatch, world, receipt):
    """The baseline every refusal below is measured against. Without it, a check that
    refuses everything would look like a check that refuses the right things."""
    assert _control_plane_problems(monkeypatch, world, receipt) == []


STANDALONE_NON_VACUITY = [
    (("candidate", "candidate_id"), "some-other-candidate"),
    (("candidate", "status_claim"), "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"),
    (("plan", "plan_hash"), "6" * 64),
    (("ledger", "plan_hash"), "7" * 64),
    (("ledger", "unique_plan_hashes"), 2),
    (("ledger", "plan_started_count"), 2),
    (("ledger", "terminal_count"), 2),
    (("results", "measured_pairs"), 1),
    (("results", "baseline_result_count"), 1),
    (("results", "total_model_result_count"), 1),
    (("outcome", "eligibility"), "eligible_for_human_review"),
    (("outcome", "security_blocking_count"), 7),
    (("decision_evidence", "decision_hash"), "d" * 64),
    (("decision_evidence", "rederived_by"), "somebody.else"),
    (("evidence", "gate_report_hash"), "e" * 64),
    (("canonical_encoding",), "latin-1 if you like"),
]

EXTERNAL_NON_VACUITY = [
    (("evaluation_source", "evaluation_source_commit"), "a" * 40),
    (("evaluation_source", "evaluation_source_tree_oid"), "b" * 40),
    (("evaluation_source", "evaluation_source_digest"), "1" * 64),
    (("measurement_witness", "measurement_witness_sha256"), "5" * 64),
    (("measurement_witness", "measurement_witness_hash"), "6" * 64),
    (("measurement_witness", "measurement_witness_commit"), "c" * 40),
    (("seal_implementation_source", "seal_implementation_source_commit"), "d" * 40),
    (("ledger", "plan_started_event_hash"), "8" * 64),
    (("ledger", "holdout_commit_event_hash"), "9" * 64),
    (("ledger", "terminal_event_hash"), "a" * 64),
    (("evidence", "report_hash"), "b" * 64),
    (("evidence", "evaluation_manifest_hash"), "c" * 64),
    (("evidence", "evaluation_artifact_tree_hash"), "d" * 64),
    (("evidence", "comparison_manifest_hash"), "e" * 64),
    (("evidence", "metrics_summary_hash"), "f" * 64),
    (("evidence", "pack_manifest_hash"), "2" * 64),
    (("holdout", "task_pack_hash"), "9" * 64),
    (("holdout", "hidden_target_store_hash"), "c" * 64),
    (("holdout", "dataset_manifest_hash"), "3" * 64),
    (("results", "task_count"), 1),
    (("results", "missing_pairs"), 1),
]


@pytest.mark.parametrize("path,value", STANDALONE_NON_VACUITY,
                         ids=[".".join(p) for p, _ in STANDALONE_NON_VACUITY])
def test_the_standalone_verifier_checks_every_fact_it_owns(receipt, path, value):
    """Mutate, REHASH, and require a refusal from the portable verifier alone."""
    _refused(receipt, *path, value=value)


@pytest.mark.parametrize("path,value", EXTERNAL_NON_VACUITY,
                         ids=[".".join(p) for p, _ in EXTERNAL_NON_VACUITY])
def test_the_control_plane_checks_every_externally_bound_fact(monkeypatch, world,
                                                              receipt, path, value):
    """Mutate, REHASH, and require a refusal from the surface that can actually see it."""
    problems = _control_plane_problems(monkeypatch, world,
                                       _rehashed(receipt, *path, value=value))
    assert problems, f"mutating {'.'.join(path)} was not refused by the control plane"


@pytest.mark.parametrize("verdict", sorted(COMPARISON_VERDICTS))
def test_the_control_plane_checks_each_verdict_count_against_the_witness(
        monkeypatch, world, receipt, verdict):
    mutated = dict(receipt["results"]["verdict_counts"])
    mutated[verdict] = mutated[verdict] + 1
    problems = _control_plane_problems(
        monkeypatch, world,
        _rehashed(receipt, "results", "verdict_counts", value=mutated))
    assert problems


def test_the_control_plane_checks_the_numeric_partition_against_the_witness(
        monkeypatch, world, receipt):
    numeric = dict(receipt["results"]["numeric_delta_counts"])
    numeric["zero"], numeric["positive"] = numeric["positive"], numeric["zero"]
    if numeric == receipt["results"]["numeric_delta_counts"]:
        pytest.skip("the synthetic run's numeric buckets are symmetric")
    problems = _control_plane_problems(
        monkeypatch, world,
        _rehashed(receipt, "results", "numeric_delta_counts", value=numeric))
    assert any("numeric" in p for p in problems), problems


def test_a_receipt_whose_witness_is_missing_is_refused(monkeypatch, world, receipt):
    problems = _control_plane_problems(
        monkeypatch, world,
        _rehashed(receipt, "measurement_witness", "path",
                  value="state/witnesses/never-written.json"))
    assert any("not a regular tracked file" in p for p in problems), problems


def test_the_witness_commit_topology_is_checked_not_believed(monkeypatch, world,
                                                             receipt):
    """The claim that the witness's first parent IS the evaluation source is verified
    against Git. It is the one fact a post-repair receipt cannot re-create."""
    problems = _control_plane_problems(
        monkeypatch, world,
        _rehashed(receipt, "measurement_witness", "measurement_witness_commit",
                  value=world["seal_commit"]))
    assert any("first parent" in p or "not the blob" in p for p in problems), problems


# ── the builder owns the facts that need the runtime evidence to check ────────
def test_the_builder_refuses_an_adapter_the_training_receipt_did_not_seal(world):
    """`candidate.adapter_sha256` is bound by the BUILDER against three surfaces: the
    adapter on disk, the training receipt, and the plan/report/commit references. A
    standalone reader has none of them, which is why the check lives where it does."""
    import _s3q01_synthetic as W

    forged = W.write_training_receipt(
        Path(world["repo_root"]) / "forged.variant.json", world["adapter"],
        adapter={"sha256": "1" * 64,
                 "manifest_hash": world["adapter"]["adapter_manifest_hash"],
                 "artifact_set_hash": world["adapter"]["adapter_artifact_set_hash"]})
    try:
        with pytest.raises(ReceiptError, match="not the same weights"):
            _build(world, training_receipt=forged)
    finally:
        forged.unlink()


def test_the_builder_refuses_a_training_receipt_for_another_candidate(world):
    import _s3q01_synthetic as W

    other = W.write_training_receipt(
        Path(world["repo_root"]) / "other.variant.json", world["adapter"],
        candidate_id="a-different-candidate")
    try:
        with pytest.raises(ReceiptError, match="belongs to|seals"):
            _build(world, training_receipt=other)
    finally:
        other.unlink()


def test_the_builder_refuses_an_empty_adapter_identity(world):
    """`.1` wrote `adapter_sha256: ""` and its schema permitted it."""
    import _s3q01_synthetic as W

    blank = W.write_training_receipt(
        Path(world["repo_root"]) / "blank.variant.json", world["adapter"],
        adapter={"sha256": "", "manifest_hash": "", "artifact_set_hash": ""})
    try:
        with pytest.raises(ReceiptError, match="no usable adapter_sha256"):
            _build(world, training_receipt=blank)
    finally:
        blank.unlink()


def test_the_decision_evidence_itself_is_checked(receipt):
    """Changing the gate evidence without re-deriving the decision must be refused."""
    mutated = copy.deepcopy(receipt["decision_evidence"]["gate_report"])
    mutated["blocking_count"] = mutated["blocking_count"] + 1
    problems = _refused(receipt, "decision_evidence", "gate_report", value=mutated)
    assert problems


def test_the_real_receipt_is_never_mutated_by_any_of_this(receipt, world):
    """Section 27: mutations happen on throwaway copies. The fixture must be untouched
    at the end of the module, and it must still verify."""
    assert verify_receipt_payload(receipt) == ()
    assert receipt["evaluation_source"]["evaluation_source_commit"] == \
        world["evaluation_source_commit"]
