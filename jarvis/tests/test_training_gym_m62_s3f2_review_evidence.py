"""V69 M62 S3F.2 — the body-free review-evidence artefact.

S3E.2 produced three differential security findings and a reviewer who could not look at
any of them: ``ArmScore`` was computed and discarded, so grader statuses, security-finding
categories, refusal classes and parser outcomes — all body-free — never reached disk.

These tests pin the two halves of the correction that pull against each other. The
evidence must be rich enough to review a finding from, and it must contain no response
text, no excerpt, no hidden target and no host path. A file that satisfied only the first
would be the raw-response persistence this milestone deliberately did not build.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation import artifacts as A
from training_gym.evaluation.backend import (
    BackendStatus,
    EvaluationResult,
    EvaluationRole,
)
from training_gym.evaluation.comparison import build_comparison
from training_gym.evaluation.policy import EvaluationPolicySet
from training_gym.evaluation.runner import result_records, results_by_role
from training_gym.evaluation.score_evidence import (
    BASELINE_SCORES_FILE,
    CANDIDATE_SCORES_FILE,
    SCORE_EVIDENCE_FIELDS,
    SCORE_EVIDENCE_VERSION,
    ScoreEvidenceError,
    build_score_evidence,
    read_score_evidence,
    response_digests,
    score_evidence_record,
    verify_score_evidence,
)
from training_gym.evaluation.scoring import NOTE_CODES, score_arm
from training_gym.evaluation.backends.fake import FakeMode
from training_gym.schemas import SchemaError

from _m62_evaluation_fixtures import (
    make_pack,
    make_store,
    run_fake,
)

_NOW = "2026-08-10T00:00:00Z"
_POLICIES = EvaluationPolicySet()
_SPLITS = ["adversarial", "hidden_evaluation", "security_regression"]

#: A response that carries a real home path inside a reasoning block. Used to prove the
#: security scan still reads the whole raw body while the evidence still stays clean.
_LEAKY = ("<think>the key is under /home/analyst/.ssh/id_rsa</think>\n"
          '{"severity": "low"}')


# ══════════════════════════════════════════════════════════════════════════════
#  Building one record
# ══════════════════════════════════════════════════════════════════════════════
def _pack():
    return make_pack()


def _scored(text: str, *, role=EvaluationRole.BASELINE, index: int = 0):
    """Score one real task through the PRODUCTION scorer, not a re-implementation.

    The task carries the same structural, content-free schema the production registry
    maps ``structured_report`` to, so ``schema_valid`` is decided by a declared contract
    rather than by "it parsed".
    """
    from dataclasses import replace
    pack = _pack()
    task = replace(pack.tasks[index],
                   expected_output_schema={"type": "object",
                                           "additionalProperties": True})
    store = make_store(pack)
    target = store.lookup(task.task_id, task_hash=task.task_hash) \
        if task.task_id in store else None
    result = EvaluationResult(
        backend_id="fake_evaluation", backend_version="m62.fake_evaluation.1",
        role=role, task_id=task.task_id, task_hash=task.task_hash,
        status=BackendStatus.SUCCEEDED, response_text=text, output_tokens=32,
        latency_ms=10)
    return task, result, score_arm(task, result, target=target,
                                   policy=_POLICIES.graders)


def _record(text: str = '{"severity": "low"}', **overrides) -> dict:
    _task, result, score = _scored(text)
    payload = dict(evaluation_id="eval-001", generation=3,
                   response_sha256=result.to_dict()["response_sha256"])
    payload.update(overrides)
    return score_evidence_record(score, **payload)


def test_the_score_evidence_contains_no_response_body():
    marker = "SENTINEL-RESPONSE-TEXT-9f2a"
    record = _record(f'{{"severity": "low", "note": "{marker}"}}')
    assert marker not in json.dumps(record)


def test_the_score_evidence_carries_no_free_text_that_could_quote_the_response():
    """A schema-violation message embeds the offending value; a tool-call problem embeds
    the proposed tool's name. Both are model output, so neither is persisted."""
    _task, _result, score = _scored('["severity", "medium"]')
    assert score.schema_valid is False
    assert any("schema violation" in note for note in score.notes)
    record = _record('["severity", "medium"]')
    assert "notes" not in record
    assert record["note_codes"] == ["structured_output_schema_violation"]


def test_the_score_evidence_binds_the_response_digest():
    _task, result, _score = _scored('{"severity": "low"}')
    record = _record('{"severity": "low"}')
    assert record["response_sha256"] == result.to_dict()["response_sha256"]
    assert len(record["response_sha256"]) == 64


def test_the_score_evidence_binds_the_score_digest():
    _task, _result, score = _scored('{"severity": "low"}')
    assert _record('{"severity": "low"}')["score_hash"] == score.score_hash()


def test_the_score_evidence_preserves_the_grader_statuses():
    _task, _result, score = _scored('{"severity": "low"}')
    record = _record('{"severity": "low"}')
    assert record["grader_statuses"] == dict(sorted(score.grader_statuses.items()))
    assert record["grader_statuses"], "a record with no grader status reviews nothing"
    assert record["missing_graders"] == list(score.missing_graders)


def test_the_score_evidence_preserves_the_security_finding_categories():
    _task, _result, score = _scored(_LEAKY)
    assert "secret_pii:home_path" in score.security_findings
    record = _record(_LEAKY)
    assert record["security_findings"] == list(score.security_findings)
    assert record["blocking"] is True
    assert record["severity"] == "blocking"


def test_the_score_evidence_preserves_the_hygiene_findings():
    _task, _result, score = _scored('<think>reasoning</think>\n{"severity": "low"}')
    assert score.hygiene_findings == ("reasoning",)
    assert _record('<think>reasoning</think>\n{"severity": "low"}')[
        "hygiene_findings"] == ["reasoning"]


def test_the_score_evidence_preserves_the_refusal_classification():
    _task, _result, score = _scored("I cannot help with that request.")
    record = _record("I cannot help with that request.")
    assert record["refusal"] == score.refusal.value
    assert record["refusal"] != ""


def test_the_score_evidence_separates_json_parseable_from_schema_valid():
    parse_failure = _record("no json here at all")
    assert parse_failure["json_parseable"] is False
    assert parse_failure["schema_valid"] is False
    wrong_shape = _record('["severity", "medium"]')
    assert wrong_shape["json_parseable"] is True
    assert wrong_shape["schema_valid"] is False


def test_the_score_evidence_names_the_reasoning_block_case_distinctly():
    record = _record("<think>still thinking")
    assert record["json_parseable"] is False
    assert "structured_output_never_left_reasoning_block" in record["note_codes"]


def test_every_note_code_is_in_the_closed_vocabulary():
    for text in ('{"severity": "low"}', '["a"]', "prose", "<think>unterminated",
                 _LEAKY, '<think>x</think>\n{"a": 1}'):
        assert set(_record(text)["note_codes"]) <= NOTE_CODES


def test_a_score_carrying_an_invented_note_code_is_refused():
    from training_gym.evaluation.scoring import ArmScore, RefusalClass
    from training_gym.schemas import ResultStatus
    with pytest.raises(SchemaError, match="unknown note code"):
        ArmScore(task_id="t", task_hash="a" * 64, role="baseline", family="soc_triage",
                 split="hidden_evaluation", status=ResultStatus.PASS, reward=1.0,
                 refusal=RefusalClass.SAFE_COMPLETION,
                 note_codes=("whatever_the_model_said",))


def test_the_score_evidence_is_deterministic():
    assert _record('{"severity": "low"}') == _record('{"severity": "low"}')


def test_the_score_evidence_field_list_is_closed():
    assert set(_record()) == set(SCORE_EVIDENCE_FIELDS)
    assert _record()["evidence_version"] == SCORE_EVIDENCE_VERSION


def test_a_record_with_an_unknown_field_is_refused_on_read():
    record = {**_record(), "response_excerpt": "whatever the model said"}
    with pytest.raises(SchemaError):
        read_score_evidence(record, label="baseline-scores.jsonl[0]")


def test_no_private_absolute_path_reaches_the_score_evidence():
    body = json.dumps(_record(_LEAKY))
    assert "/home/analyst" not in body
    assert "id_rsa" not in body
    assert "C:/Users" not in body and "C:\\Users" not in body


def test_no_hidden_target_reaches_the_score_evidence():
    from _m62_evaluation_fixtures import TARGET_TEXT
    body = json.dumps(_record())
    distinctive = ("true_positive", "unsigned", "protected", "evt-1", "evt-2")
    assert all(token in TARGET_TEXT for token in distinctive)
    for token in distinctive:
        assert token not in body


# ══════════════════════════════════════════════════════════════════════════════
#  Building a whole arm's file
# ══════════════════════════════════════════════════════════════════════════════
def _summary(mode: FakeMode = FakeMode.IDENTICAL):
    pack = _pack()
    run = run_fake(mode, pack=pack)
    summary = build_comparison(run, pack=pack, targets=make_store(pack),
                               policies=_POLICIES)
    records = {
        "baseline": result_records(results_by_role(run, EvaluationRole.BASELINE)),
        "candidate": result_records(results_by_role(run, EvaluationRole.CANDIDATE)),
    }
    return pack, run, summary, records


def _evidence(role: str, summary, records, **overrides):
    payload = dict(evaluation_id="eval-001", generation=3,
                   response_digests=response_digests(records[role]))
    payload.update(overrides)
    return build_score_evidence(summary.comparisons, role=role, **payload)


def test_an_arms_evidence_is_written_in_canonical_task_order():
    _pack_, _run, summary, records = _summary()
    ids = [r["task_id"] for r in _evidence("baseline", summary, records)]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))


def test_an_arms_evidence_records_only_that_arm():
    _pack_, _run, summary, records = _summary()
    assert {r["arm"] for r in _evidence("candidate", summary, records)} == {"candidate"}


def test_evidence_for_an_unknown_arm_is_refused():
    _pack_, _run, summary, records = _summary()
    with pytest.raises(ScoreEvidenceError, match="unknown arm"):
        build_score_evidence(summary.comparisons, role="control",
                             evaluation_id="e", generation=1, response_digests={})


def test_evidence_that_binds_to_no_response_is_refused():
    _pack_, _run, summary, records = _summary()
    with pytest.raises(ScoreEvidenceError, match="no baseline response digest"):
        build_score_evidence(summary.comparisons, role="baseline",
                             evaluation_id="e", generation=1, response_digests={})


# ══════════════════════════════════════════════════════════════════════════════
#  Verification
# ══════════════════════════════════════════════════════════════════════════════
def _verify(role, summary, records, evidence=None):
    return verify_score_evidence(
        evidence if evidence is not None else _evidence(role, summary, records),
        role=role, comparison_records=summary.comparison_records(),
        result_records_=records[role], evaluation_id="eval-001", generation=3)


def test_untampered_evidence_verifies():
    _pack_, _run, summary, records = _summary()
    assert _verify("baseline", summary, records) == ()
    assert _verify("candidate", summary, records) == ()


def test_a_modified_score_line_fails_verification():
    _pack_, _run, summary, records = _summary()
    evidence = [dict(r) for r in _evidence("baseline", summary, records)]
    evidence[0]["security_findings"] = []
    evidence[0]["score_hash"] = "0" * 64
    problems = _verify("baseline", summary, records, evidence)
    assert any("has been edited since it was compared" in p for p in problems)


def test_a_rebound_response_digest_fails_verification():
    _pack_, _run, summary, records = _summary()
    evidence = [dict(r) for r in _evidence("baseline", summary, records)]
    evidence[0]["response_sha256"] = "1" * 64
    problems = _verify("baseline", summary, records, evidence)
    assert any("is about a different response" in p for p in problems)


def test_reordered_lines_fail_verification():
    _pack_, _run, summary, records = _summary()
    evidence = [dict(r) for r in _evidence("baseline", summary, records)]
    evidence.reverse()
    problems = _verify("baseline", summary, records, evidence)
    assert any("canonical task_id order" in p for p in problems)


def test_a_line_attributed_to_the_wrong_task_fails_verification():
    _pack_, _run, summary, records = _summary()
    evidence = [dict(r) for r in _evidence("baseline", summary, records)]
    evidence[0]["task_id"] = "task-that-was-never-run"
    problems = _verify("baseline", summary, records, evidence)
    assert any("not in the paired comparison" in p or "has no baseline result" in p
               for p in problems)


def test_a_swapped_arm_identity_fails_verification():
    """The candidate's evidence filed under the baseline's name is the single most
    dangerous silent failure here: it would attribute one arm's verdicts to the other."""
    _pack_, _run, summary, records = _summary(FakeMode.CANDIDATE_IMPROVED)
    swapped = _evidence("candidate", summary, records)
    problems = verify_score_evidence(
        swapped, role="baseline", comparison_records=summary.comparison_records(),
        result_records_=records["baseline"], evaluation_id="eval-001", generation=3)
    assert any("belongs to the other arm" in p for p in problems)


def test_a_dropped_line_fails_verification():
    _pack_, _run, summary, records = _summary()
    evidence = [dict(r) for r in _evidence("baseline", summary, records)][1:]
    problems = _verify("baseline", summary, records, evidence)
    assert any("has no evidence line" in p for p in problems)


def test_evidence_naming_the_wrong_generation_fails_verification():
    _pack_, _run, summary, records = _summary()
    evidence = _evidence("baseline", summary, records, generation=4)
    problems = _verify("baseline", summary, records, evidence)
    assert any("names generation 4" in p for p in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  The manifest and the tree
# ══════════════════════════════════════════════════════════════════════════════
def _write(tmp_path, **overrides):
    from test_training_gym_m62_evaluation_artifacts import _write as write
    return write(tmp_path, **overrides)


def test_the_score_files_are_allowlisted_and_manifest_bound(tmp_path):
    directory, validation, _report = _write(tmp_path)
    assert validation.ok
    names = {p.name for p in directory.iterdir()}
    assert {BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE} <= names
    manifest = json.loads(
        (directory / A.EVALUATION_MANIFEST_FILE).read_text(encoding="utf-8"))
    sealed = {f["name"] for f in manifest["files"]}
    assert {BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE} <= sealed
    for entry in manifest["files"]:
        if entry["name"] in (BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE):
            assert entry["size_bytes"] > 0
            assert entry["line_count"] > 0


def test_a_written_generation_with_score_evidence_verifies(tmp_path):
    directory, _validation, _report = _write(tmp_path)
    assert A.verify_evaluation_generation(directory) == ()


def test_tampering_with_a_score_file_fails_tree_verification(tmp_path):
    directory, _validation, _report = _write(tmp_path)
    path = directory / BASELINE_SCORES_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["blocking"] = not record["blocking"]
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    problems = A.verify_evaluation_generation(directory)
    assert any("does not match the manifest" in p for p in problems)


def test_a_missing_score_file_fails_for_a_new_schema_generation(tmp_path):
    directory, _validation, _report = _write(tmp_path)
    (directory / CANDIDATE_SCORES_FILE).unlink()
    problems = A.verify_evaluation_generation(directory)
    assert any(CANDIDATE_SCORES_FILE in p for p in problems)


def test_an_extra_unallowlisted_score_file_is_refused(tmp_path):
    directory, _validation, _report = _write(tmp_path)
    (directory / "teacher-scores.jsonl").write_text("{}\n", encoding="utf-8")
    problems = A.verify_evaluation_generation(directory)
    assert any("teacher-scores.jsonl" in p for p in problems)


def test_a_symlinked_score_file_is_refused(tmp_path):
    directory, _validation, _report = _write(tmp_path)
    target = directory / BASELINE_SCORES_FILE
    body = target.read_bytes()
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(body)
    target.unlink()
    try:
        target.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating a symlink")
    problems = A.verify_evaluation_generation(directory)
    assert any("symlink" in p for p in problems)


def test_no_pickle_bearing_name_is_allowlisted():
    for name in A.ALLOWED_EVALUATION_FILES:
        assert not name.endswith((".bin", ".pt", ".pth", ".pkl"))


def test_the_score_files_are_the_only_addition_to_the_allowlist():
    assert A.ALLOWED_EVALUATION_FILES == (
        A.REQUIRED_EVALUATION_FILES | {BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE})


# ══════════════════════════════════════════════════════════════════════════════
#  Legacy compatibility — the S3E.2 generation of record predates all of this
# ══════════════════════════════════════════════════════════════════════════════
def test_a_legacy_generation_without_score_evidence_still_verifies(tmp_path):
    """Requiring the files retroactively would mean either failing the only real
    measurement this repository has, or manufacturing evidence its run never wrote."""
    directory, _validation, _report = _write(tmp_path)
    for name in (BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE):
        (directory / name).unlink()
    path = directory / A.EVALUATION_MANIFEST_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = [f for f in manifest["files"]
                         if f["name"] not in (BASELINE_SCORES_FILE,
                                              CANDIDATE_SCORES_FILE)]
    manifest["manifest_version"] = "m62.evaluation_manifest.1"
    manifest.pop("manifest_hash")
    rebuilt = A.EvaluationManifest.from_dict(
        {**manifest, "tree_hash": A.evaluation_tree_hash(
            tuple(A.ArtifactFile(name=f["name"], sha256=f["sha256"],
                                 size_bytes=f["size_bytes"],
                                 line_count=f["line_count"])
                  for f in manifest["files"]))})
    A.write_json(path, rebuilt.to_record())
    assert rebuilt.requires_score_evidence is False
    assert A.verify_evaluation_generation(directory) == ()


def test_the_required_file_set_is_decided_by_the_manifest_version():
    legacy = A.required_evaluation_files("m62.evaluation_manifest.1")
    current = A.required_evaluation_files(A.EVALUATION_MANIFEST_VERSION)
    assert BASELINE_SCORES_FILE not in legacy
    assert {BASELINE_SCORES_FILE, CANDIDATE_SCORES_FILE} <= current
    assert legacy < current


def test_the_manifest_version_records_that_the_contract_changed():
    assert A.EVALUATION_MANIFEST_VERSION == A.SCORE_EVIDENCE_MANIFEST_VERSION
    assert A.EVALUATION_MANIFEST_VERSION != "m62.evaluation_manifest.1"


# ══════════════════════════════════════════════════════════════════════════════
#  The property that must not be traded away for reviewability
# ══════════════════════════════════════════════════════════════════════════════
def test_the_security_scan_still_reads_the_whole_raw_response():
    """The evidence is body-free. The SCAN is not: a credential is a credential wherever
    it appears, including inside a reasoning block the structural check strips."""
    _task, _result, score = _scored(_LEAKY)
    assert "secret_pii:home_path" in score.security_findings
    assert score.blocking is True
    assert score.reward == 0.0


def test_no_raw_response_field_exists_anywhere_in_the_evidence_contract():
    forbidden = ("response_text", "response", "response_excerpt", "body", "excerpt",
                 "raw", "answer", "target", "expected", "teacher")
    assert not [f for f in SCORE_EVIDENCE_FIELDS
                if any(f == bad or f.endswith(f"_{bad}") for bad in forbidden)]
