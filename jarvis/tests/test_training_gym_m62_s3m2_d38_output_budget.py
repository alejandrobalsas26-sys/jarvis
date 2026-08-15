"""V69 M62 S3M.2 — D38: output-budget exhaustion as a first-class BODY-FREE metric.

WHAT D38 WAS
------------
The instrument exposed ``ArmScore.truncated`` and ``metrics.truncation_rate``, and both
mean **INPUT** truncation: ``score_arm`` assigns ``truncated=result.input_truncated``,
and the backend sets ``input_truncated`` when the PROMPT hits ``max_input_tokens``. So
OG-3's "truncation 0/9" was, correctly, about the prompt.

A response that consumes ``max_new_tokens`` is a different event. It was already recorded
body-free, in ``EvaluationResult.finish_reason``, and it had **no first-class metric and
no gate**. In S3L the candidate reached the ceiling on 5 of 36 tasks — including both
structured-output failures — while OG-3 correctly reported 0/9. The single most
diagnostic fact about that run was in the artefacts and absent from every number.

WHAT S3M.2 CHANGED, AND WHAT IT DID NOT
---------------------------------------
It added a **diagnostic** metric derived from termination metadata that already existed.
It did not change generation, ``max_new_tokens``, prompts, rendering, stopping criteria,
graders, parsers, thresholds or any gate, and it did **not** create a D38 gate. Reaching
the output ceiling is not a failure by itself — S3M measured three ceiling endings whose
graders passed.

THE PROPERTIES THESE TESTS PIN
------------------------------
1. **Three events stay three events.** ``truncated`` is input truncation, ``timed_out``
   is a wall-clock timeout (D33), ``output_budget_exhausted`` is the output ceiling. Any
   two of them can be true at once, and none of them implies another.
2. **One authority.** ``FinishReason.output_budget_exhausted`` is the only place that
   decides what a termination state means. Nothing else compares a finish reason to a
   literal.
3. **It fails closed.** An error, a blocked arm and an unclassified state are
   ``None`` — UNMEASURED, excluded from the denominator and visible as ``excluded`` —
   never an optimistic ``False`` that would make the metric read clean.
4. **No gate reads it**, and OG-3 still reads input truncation.
5. **Metric-policy identity moves and gate-policy identity does not.**
6. **Historical artefacts still verify**, and the historical body-free records reproduce
   the recorded S3I/S3L termination counts through the production authority.

WHAT THESE TESTS DELIBERATELY DO NOT DO
---------------------------------------
No model is loaded, nothing is generated, no held-out prompt or response body appears,
and no eligibility is recomputed. Every fixture below is synthetic and written here.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from training_gym.evaluation import gates as gates_module
from training_gym.evaluation.backend import (
    BackendErrorCategory,
    BackendStatus,
    CleanupStatus,
    EvaluationResult,
    FinishReason,
    output_budget_consistency_problems,
)
from training_gym.evaluation.comparison import (
    ComparisonVerdict,
    PairedComparison,
    output_budget_exhaustion_matrix,
)
from training_gym.evaluation.metrics import METRICS_VERSION, MetricsError, build_arm_metrics
from training_gym.evaluation.policy import (
    CANONICAL_METRIC_NAMES,
    METRIC_SET_VERSION,
    GatePolicy,
    MetricPolicy,
)
from training_gym.evaluation.score_evidence import (
    SCORE_EVIDENCE_FIELDS,
    ScoreEvidenceError,
    read_score_evidence,
    score_evidence_record,
)
from training_gym.evaluation.scoring import ArmScore, RefusalClass, ScoringError, score_arm
from training_gym.schemas import ResultStatus, SchemaError

#: The digest the S3I and S3L plans and reports both sealed, before this milestone.
HISTORICAL_METRIC_POLICY_HASH = (
    "2d0830103bc11f280fc2a25e5ac8f0f79bd3e6a1ad589046d238e9fc5d9cfd87")
#: The gate policy S3G predeclared. It must not move for an instrumentation change.
GATE_POLICY_HASH = "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5"
#: The generation policy both live runs used, and the budget it declared.
MAX_NEW_TOKENS = 512

#: Where the sealed generations live, if this checkout has them. They are gitignored
#: runtime artefacts, so the tests that read them SKIP rather than fail when absent —
#: they never degrade into a silent pass.
GENERATIONS = pathlib.Path(__file__).resolve().parents[1] / "evaluation" / "evaluations"
SEALED_RUNS = {
    "S3I": ("m62-s3i-quality-heldout-live", {"baseline": 0, "candidate": 1}),
    "S3L": ("m62-s3l-quality-heldout-live", {"baseline": 0, "candidate": 5}),
}


# ══════════════════════════════════════════════════════════════════════════════
#  helpers — synthetic, body-free
# ══════════════════════════════════════════════════════════════════════════════
def _result(*, finish: FinishReason = FinishReason.END_OF_SEQUENCE,
            status: BackendStatus = BackendStatus.SUCCEEDED,
            input_truncated: bool = False, output_tokens: int = 42,
            timed_out: bool = False, task_id: str = "synthetic-task",
            error: BackendErrorCategory = BackendErrorCategory.NONE) -> EvaluationResult:
    return EvaluationResult(
        backend_id="synthetic", backend_version="0", role="candidate", task_id=task_id,
        task_hash="a" * 64, status=status,
        response_text="{}" if status is BackendStatus.SUCCEEDED else "",
        input_tokens=10, output_tokens=output_tokens,
        input_truncated=input_truncated, truncated_tokens=1 if input_truncated else 0,
        finish_reason=finish, timed_out=timed_out, error_category=error,
        cleanup_status=CleanupStatus.NOT_REQUIRED)


def _score(task_id: str, *, family: str = "structured_report",
           exhausted: bool | None = False, truncated: bool = False,
           role: str = "candidate", split: str = "hidden_evaluation") -> ArmScore:
    return ArmScore(
        task_id=task_id, task_hash="b" * 64, role=role, family=family, split=split,
        status=ResultStatus.PASS, reward=1.0, refusal=RefusalClass.SAFE_COMPLETION,
        output_tokens=10, truncated=truncated, output_budget_exhausted=exhausted)


def _metrics(scores, *, task_count=None, families=("structured_report",),
             splits=("hidden_evaluation",), role="candidate"):
    return build_arm_metrics(
        list(scores), role=role, families=list(families), splits=list(splits),
        policy=MetricPolicy(),
        task_count=len(list(scores)) if task_count is None else task_count)


def _operational(scores, **kwargs) -> dict:
    return _metrics(scores, **kwargs).to_dict()["operational"]


def _sealed(run_key: str) -> pathlib.Path:
    name, _ = SEALED_RUNS[run_key]
    directory = GENERATIONS / name / "gen-1"
    if not directory.is_dir():
        pytest.skip(f"the sealed {run_key} generation is not present in this checkout; "
                    f"it is a gitignored runtime artefact")
    return directory


def _records(directory: pathlib.Path, arm: str) -> list[dict]:
    path = directory / f"{arm}-results.jsonl"
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ══════════════════════════════════════════════════════════════════════════════
#  1. the legacy signal is untouched
# ══════════════════════════════════════════════════════════════════════════════
def test_arm_score_truncated_is_still_input_truncation():
    """§43.1 — the legacy field keeps its INPUT semantics, at the source.

    Re-pointing it at the response would silently rewrite what every historical report
    means, which is exactly what D38 forbade.
    """
    assert "truncated=result.input_truncated" in inspect.getsource(score_arm)


def test_input_truncation_alone_does_not_report_output_exhaustion():
    """§43.4 — a clipped PROMPT says nothing about whether the RESPONSE ran out."""
    score = score_arm.__wrapped__ if hasattr(score_arm, "__wrapped__") else score_arm
    del score
    result = _result(input_truncated=True, finish=FinishReason.END_OF_SEQUENCE)
    assert result.input_truncated is True
    assert result.output_budget_exhausted is False


def test_input_and_output_truncation_can_both_be_true():
    """§43.5 — they are independent events and both must be expressible at once."""
    result = _result(input_truncated=True, finish=FinishReason.MAX_NEW_TOKENS,
                     output_tokens=MAX_NEW_TOKENS)
    assert result.input_truncated is True
    assert result.output_budget_exhausted is True


def test_the_legacy_metric_name_survives_and_keeps_its_meaning():
    """§22 — ``truncation_rate`` is not deleted and still counts ``ArmScore.truncated``."""
    scores = [_score("t1", truncated=True), _score("t2", truncated=False)]
    operational = _operational(scores)
    assert operational["truncation_rate"]["name"] == "truncation_rate"
    assert operational["truncation_rate"]["numerator"] == 1
    assert operational["truncation_rate"]["denominator"] == 2


def test_the_input_truncation_alias_cannot_drift_from_the_legacy_metric():
    """§22 — an alias must be the same measurement, not a second computation."""
    scores = [_score("t1", truncated=True), _score("t2", truncated=False),
              _score("t3", truncated=True)]
    operational = _operational(scores)
    legacy = dict(operational["truncation_rate"])
    alias = dict(operational["input_truncation_rate"])
    assert legacy.pop("name") == "truncation_rate"
    assert alias.pop("name") == "input_truncation_rate"
    assert legacy == alias, "the alias must differ from the legacy metric by NAME only"


# ══════════════════════════════════════════════════════════════════════════════
#  2. the canonical termination semantics
# ══════════════════════════════════════════════════════════════════════════════
def test_normal_end_of_sequence_is_not_output_budget_exhaustion():
    """§43.2 — a model that stopped by itself did not run out of budget."""
    assert FinishReason.END_OF_SEQUENCE.output_budget_exhausted is False
    assert _result(finish=FinishReason.END_OF_SEQUENCE).output_budget_exhausted is False


def test_the_output_ceiling_is_output_budget_exhaustion():
    """§43.3 — the canonical state, and the only one that is True."""
    assert FinishReason.MAX_NEW_TOKENS.output_budget_exhausted is True
    assert _result(finish=FinishReason.MAX_NEW_TOKENS,
                   output_tokens=MAX_NEW_TOKENS).output_budget_exhausted is True


def test_a_stop_sequence_ending_is_not_output_budget_exhaustion():
    """A response the policy cut at a stop string still terminated inside its budget."""
    assert FinishReason.STOP_SEQUENCE.output_budget_exhausted is False


def test_timeout_is_not_output_budget_exhaustion():
    """§43.6 / §8 — D33 governs the clock. D38 must not absorb it.

    Both halves matter: the finish reason is classified ``False`` (a timeout is a
    different event, not an unmeasured one), and a timed-out RESULT is unmeasured,
    because an arm that produced no output demonstrated nothing about its budget.
    """
    assert FinishReason.TIMEOUT.output_budget_exhausted is False
    timed = _result(status=BackendStatus.TIMED_OUT, timed_out=True,
                    finish=FinishReason.TIMEOUT,
                    error=BackendErrorCategory.TIMEOUT)
    assert timed.timed_out is True
    assert timed.output_budget_exhausted is None


def test_a_generation_error_is_not_a_clean_non_exhausted_completion():
    """§26 / §43.7 — an error is UNMEASURED, never ``False``."""
    assert FinishReason.ERROR.output_budget_exhausted is None
    failed = _result(status=BackendStatus.FAILED, finish=FinishReason.ERROR,
                     error=BackendErrorCategory.BACKEND, output_tokens=-1)
    assert failed.output_budget_exhausted is None


def test_an_unknown_finish_reason_fails_closed():
    """§25 / §43.8 — an unclassified state must not read as "the budget was fine"."""
    assert FinishReason.UNKNOWN.output_budget_exhausted is None
    assert _result(finish=FinishReason.UNKNOWN).output_budget_exhausted is None


def test_every_finish_reason_is_classified_and_an_unclassified_one_raises():
    """§6 / §31 — the closed set is exhaustive, and a new member fails closed.

    The table is checked by asking every member, so adding a ``FinishReason`` without
    classifying it breaks here rather than defaulting to a clean answer.
    """
    from training_gym.evaluation import backend as backend_module

    for reason in FinishReason:
        assert reason.output_budget_exhausted in (True, False, None)
    table = backend_module._OUTPUT_BUDGET_EXHAUSTION
    assert set(table) == set(FinishReason)
    saved = table.pop(FinishReason.STOP_SEQUENCE)
    try:
        with pytest.raises(Exception, match="output-budget classification"):
            _ = FinishReason.STOP_SEQUENCE.output_budget_exhausted
    finally:
        table[FinishReason.STOP_SEQUENCE] = saved


def test_there_is_exactly_one_authority_for_the_semantics():
    """§31 — nobody re-implements ``finish_reason == MAX_NEW_TOKENS``.

    The comparison belongs in two places only: the enum table that defines it, and the
    backend that PRODUCES the finish reason in the first place.
    """
    from training_gym.evaluation import comparison, metrics, reports, scoring

    for module in (scoring, metrics, comparison, reports):
        source = inspect.getsource(module)
        assert "MAX_NEW_TOKENS" not in source, (
            f"{module.__name__} interprets a finish reason itself; the single authority "
            f"is FinishReason.output_budget_exhausted")


# ══════════════════════════════════════════════════════════════════════════════
#  3. the consistency check (§24)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_ceiling_ending_is_consistent_with_its_token_count():
    """The relationship read from the production backend is ``>=``, not ``==``."""
    exact = _result(finish=FinishReason.MAX_NEW_TOKENS, output_tokens=MAX_NEW_TOKENS)
    over = _result(finish=FinishReason.MAX_NEW_TOKENS, output_tokens=MAX_NEW_TOKENS + 1)
    assert output_budget_consistency_problems(exact, max_new_tokens=MAX_NEW_TOKENS) == ()
    assert output_budget_consistency_problems(over, max_new_tokens=MAX_NEW_TOKENS) == ()


def test_a_ceiling_claim_below_the_budget_is_reported_not_relabelled():
    """§24 — a mismatch is diagnostic and fail-closed, never silently corrected."""
    bad = _result(finish=FinishReason.MAX_NEW_TOKENS, output_tokens=10)
    problems = output_budget_consistency_problems(bad, max_new_tokens=MAX_NEW_TOKENS)
    assert len(problems) == 1
    assert "10 of 512" in problems[0]
    # The classification itself is NOT changed by the inconsistency.
    assert bad.output_budget_exhausted is True


def test_a_full_budget_reported_as_a_clean_ending_is_reported():
    """The other direction: reaching the ceiling and claiming to have stopped early."""
    bad = _result(finish=FinishReason.END_OF_SEQUENCE, output_tokens=MAX_NEW_TOKENS)
    problems = output_budget_consistency_problems(bad, max_new_tokens=MAX_NEW_TOKENS)
    assert len(problems) == 1
    assert "counted as clean" in problems[0]


def test_the_consistency_check_claims_nothing_without_a_token_count():
    """A backend that did not count tokens must not produce a false finding."""
    uncounted = _result(finish=FinishReason.MAX_NEW_TOKENS, output_tokens=-1)
    assert output_budget_consistency_problems(
        uncounted, max_new_tokens=MAX_NEW_TOKENS) == ()


# ══════════════════════════════════════════════════════════════════════════════
#  4. the score model
# ══════════════════════════════════════════════════════════════════════════════
def test_score_arm_carries_the_result_s_termination_verdict():
    """The score is where metrics read from, so the verdict has to reach it."""
    source = inspect.getsource(score_arm)
    assert source.count("output_budget_exhausted=result.output_budget_exhausted") == 2, (
        "both the error path and the scored path must carry it, or an errored arm "
        "silently defaults to 'not exhausted'")


def test_arm_score_refuses_a_non_boolean_termination_verdict():
    """A truthy string would aggregate as an exhausted generation."""
    with pytest.raises(ScoringError, match="output_budget_exhausted"):
        _score("t1", exhausted="yes")  # type: ignore[arg-type]


def test_the_score_dict_publishes_input_truncation_and_output_exhaustion_separately():
    """§21 — the critical invariant is semantic separation."""
    payload = _score("t1", exhausted=True, truncated=False).to_dict()
    assert payload["truncated"] is False
    assert payload["output_budget_exhausted"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  5. the metric
# ══════════════════════════════════════════════════════════════════════════════
def test_the_per_arm_count_and_rate_are_correct():
    """§43.9, §43.10 — count and rate over the same authority."""
    scores = [_score("t1", exhausted=True), _score("t2", exhausted=True),
              _score("t3", exhausted=False), _score("t4", exhausted=False)]
    operational = _operational(scores)
    rate = operational["output_budget_exhaustion_rate"]
    count = operational["output_budget_exhaustion_count"]
    assert rate["numerator"] == 2
    assert rate["value"] == 0.5
    assert count["count"] == 2
    assert count["count"] == rate["numerator"], "the two must share one authority"
    assert count["over_tasks"] == rate["denominator"]
    assert sorted(count["detail"]) == ["t1", "t2"]


def test_the_denominator_is_every_generation_that_produced_output():
    """§12 — a normal complete 36-task arm has denominator 36."""
    scores = [_score(f"t{i}", exhausted=(i == 0)) for i in range(36)]
    rate = _operational(scores)["output_budget_exhaustion_rate"]
    assert rate["denominator"] == 36
    assert rate["excluded"] == 0
    assert rate["numerator"] == 1


def test_an_unmeasured_generation_leaves_the_denominator_and_is_reported():
    """§26 — errors do not silently become clean completions, and it shows."""
    scores = [_score("t1", exhausted=True), _score("t2", exhausted=False),
              _score("t3", exhausted=None)]
    rate = _operational(scores)["output_budget_exhaustion_rate"]
    assert rate["denominator"] == 2
    assert rate["excluded"] == 1
    assert rate["numerator"] == 1
    assert any("UNMEASURED" in note for note in rate["limitations"])


def test_an_arm_whose_generations_all_failed_reports_insufficient_evidence():
    """A rate over zero measurements is ``None``, never ``0.0``."""
    scores = [_score("t1", exhausted=None), _score("t2", exhausted=None)]
    rate = _operational(scores)["output_budget_exhaustion_rate"]
    assert rate["denominator"] == 0
    assert rate["value"] is None
    assert rate["evidence_quality"] == "insufficient_evidence"


def test_the_metric_is_lower_is_better_and_carries_its_missing_count():
    """Every rate carries its composition; this one is no exception."""
    scores = [_score("t1", exhausted=True)]
    rate = _operational(scores, task_count=3)["output_budget_exhaustion_rate"]
    assert rate["higher_is_better"] is False
    assert rate["missing"] == 2


def test_the_per_family_breakdown_is_correct():
    """§13, §43.12 — the S3M defect was family-sensitive in its consequence."""
    scores = [_score("s1", family="structured_report", exhausted=True),
              _score("s2", family="structured_report", exhausted=False),
              _score("r1", family="safety_refusal", exhausted=False),
              _score("e1", family="evidence_request", exhausted=True)]
    families = ("structured_report", "safety_refusal", "evidence_request",
                "tool_call_schema")
    by_family = _operational(scores, families=families)[
        "output_budget_exhaustion_rate"]["by_family"]
    assert by_family["structured_report"] == {"numerator": 1, "denominator": 2,
                                              "value": 0.5}
    assert by_family["safety_refusal"] == {"numerator": 0, "denominator": 1,
                                           "value": 0.0}
    assert by_family["evidence_request"] == {"numerator": 1, "denominator": 1,
                                             "value": 1.0}
    # A family with no task reports None, not a perfect zero.
    assert by_family["tool_call_schema"] == {"numerator": 0, "denominator": 0,
                                            "value": None}


def test_no_new_task_family_was_created():
    """§13 — the metric reports over the families the pack already has."""
    from training_gym.task_spec import TaskFamily

    assert {"safety_refusal", "structured_report", "evidence_request",
            "tool_call_schema"} <= {f.value for f in TaskFamily}


def test_the_metric_treats_the_two_arms_symmetrically():
    """§43.14 / C6 — identical input, identical number, whichever arm asks."""
    shape = [("t1", True), ("t2", False), ("t3", False)]
    baseline = _metrics([_score(t, exhausted=e, role="baseline") for t, e in shape],
                        role="baseline").to_dict()["operational"]
    candidate = _metrics([_score(t, exhausted=e, role="candidate") for t, e in shape],
                         role="candidate").to_dict()["operational"]
    assert (baseline["output_budget_exhaustion_rate"]
            == candidate["output_budget_exhaustion_rate"])
    assert (baseline["output_budget_exhaustion_count"]
            == candidate["output_budget_exhaustion_count"])


# ══════════════════════════════════════════════════════════════════════════════
#  6. the paired diagnostic (§11)
# ══════════════════════════════════════════════════════════════════════════════
def _pair(task_id: str, baseline: bool | None, candidate: bool | None,
          family: str = "structured_report") -> PairedComparison:
    from training_gym.evaluation.runner import PairedStatus

    return PairedComparison(
        task_id=task_id, task_hash="c" * 64, family=family,
        split="hidden_evaluation", execution_order="baseline_first",
        paired_status=PairedStatus.BOTH_MEASURED, verdict=ComparisonVerdict.UNCHANGED,
        baseline_score=_score(task_id, family=family, exhausted=baseline,
                              role="baseline"),
        candidate_score=_score(task_id, family=family, exhausted=candidate,
                               role="candidate"))


def test_the_paired_matrix_counts_all_four_cells():
    """§43.13 — the 2×2, plus its own bucket for what was not measured."""
    comparisons = [
        _pair("both", True, True),
        _pair("cand", False, True),
        _pair("base", True, False),
        _pair("none", False, False),
        _pair("unmeasured", None, False),
    ]
    matrix = output_budget_exhaustion_matrix(comparisons)
    assert matrix["both_exhausted"] == 1
    assert matrix["candidate_only"] == 1
    assert matrix["baseline_only"] == 1
    assert matrix["neither_exhausted"] == 1
    assert matrix["unmeasured"] == 1
    assert matrix["paired_tasks"] == 5
    assert matrix["candidate_only_tasks"] == ["cand"]
    assert matrix["baseline_only_tasks"] == ["base"]
    assert matrix["both_exhausted_tasks"] == ["both"]


def test_the_paired_diagnostic_makes_no_statistical_claim_and_is_not_a_gate():
    """§11, §20 — no sign test, no bootstrap, no PASS/FAIL, no verdict."""
    matrix = output_budget_exhaustion_matrix([_pair("t1", False, True)])
    assert matrix["is_a_gate"] is False
    forbidden = ("p_value", "verdict", "passed", "significant", "ci_low", "ci_high")
    assert not set(matrix) & set(forbidden)
    assert any("no statistical claim" in note for note in matrix["limitations"])


def test_an_unmeasured_arm_is_never_folded_into_neither_exhausted():
    """"this arm errored" and "this arm finished inside its budget" are different."""
    matrix = output_budget_exhaustion_matrix([_pair("t1", None, None)])
    assert matrix["unmeasured"] == 1
    assert matrix["neither_exhausted"] == 0


# ══════════════════════════════════════════════════════════════════════════════
#  7. gate non-interference (§15)
# ══════════════════════════════════════════════════════════════════════════════
def test_no_gate_reads_the_new_metric():
    """§15, §43.15 — ``OUTPUT_BUDGET_EXHAUSTION_GATE_REFERENCES: 0``."""
    source = inspect.getsource(gates_module)
    for name in ("output_budget_exhausted", "output_budget_exhaustion_rate",
                 "output_budget_exhaustion_count", "finish_reason"):
        assert name not in source, f"a gate now reads {name}; S3M.2 designs no D38 gate"


def test_og3_still_measures_input_truncation():
    """§15, §43.16 — OG-3's subject is unchanged and unrenamed."""
    from training_gym.evaluation import metrics as metrics_module

    source = inspect.getsource(metrics_module.build_arm_metrics)
    assert '"truncation_rate": input_truncation.to_dict(policy)' in source
    assert "lambda s: s.truncated" in source


def test_the_gate_policy_identity_did_not_move():
    """§39, §43.17 — thresholds, denominators and veto semantics are untouched."""
    assert GatePolicy().policy_hash() == GATE_POLICY_HASH


def test_the_gate_policy_does_not_bind_the_metric_policy():
    """Why the gate identity is not even transitively affected."""
    payload = GatePolicy().to_dict()
    assert "canonical_metrics" not in payload
    assert "metric_set_version" not in payload
    assert set(payload) == {
        "max_overall_reward_regression", "max_critical_family_regression",
        "max_family_collapse", "max_schema_validity_regression",
        "max_evidence_validity_regression", "max_tool_call_validity_regression",
        "max_unnecessary_refusal_increase", "max_unsafe_acceptance_increase",
        "max_timeout_rate_increase", "min_hidden_evaluation_improvement",
        "policy_version", "latency_regression_warning_ratio",
        "security_gates_have_no_margin", "thresholds_are_calibrated"}


def test_no_security_veto_was_added():
    """The brief forbids one, and a diagnostic must never become a veto."""
    source = inspect.getsource(gates_module.security_findings)
    assert "budget" not in source
    assert "exhaust" not in source


# ══════════════════════════════════════════════════════════════════════════════
#  8. policy identity (§16, §48)
# ══════════════════════════════════════════════════════════════════════════════
def test_the_metric_policy_identity_moved():
    """§43.18 / C12 — a new canonical metric must re-identify the instrument."""
    assert MetricPolicy().policy_hash() != HISTORICAL_METRIC_POLICY_HASH


def test_the_metric_policy_binds_the_metric_set():
    """§48 — the reason it moved, stated as a property rather than a hash."""
    payload = MetricPolicy().to_dict()
    assert payload["metric_set_version"] == METRIC_SET_VERSION
    assert payload["canonical_metrics"]["operational"] == sorted(
        CANONICAL_METRIC_NAMES["operational"])
    assert "output_budget_exhaustion_rate" in payload["canonical_metrics"]["operational"]
    assert "output_budget_exhaustion_count" in payload["canonical_metrics"]["operational"]


def test_the_metric_policy_identity_does_not_depend_on_host_state():
    """§43.19 — D34/D36's rule: an identity that moves with the machine is not one."""
    payload = json.dumps(MetricPolicy().to_dict())
    for marker in ("/home", "/Users", "C:\\", "hostname", "output_root",
                   "created_at", "timestamp", "cache"):
        assert marker not in payload
    assert MetricPolicy().policy_hash() == MetricPolicy().policy_hash()


def test_the_metrics_version_has_exactly_one_definition():
    """Two constants that could disagree are one more place to drift."""
    assert METRICS_VERSION is METRIC_SET_VERSION


def test_the_declared_and_computed_metric_sets_must_agree():
    """§48 — the fail-closed control, in both directions."""
    scores = [_score("t1", exhausted=False)]
    saved = CANONICAL_METRIC_NAMES["operational"]
    CANONICAL_METRIC_NAMES["operational"] = saved + ("a_metric_nobody_computes",)
    try:
        with pytest.raises(MetricsError, match="was not computed"):
            _metrics(scores)
    finally:
        CANONICAL_METRIC_NAMES["operational"] = saved
    CANONICAL_METRIC_NAMES["operational"] = tuple(
        n for n in saved if n != "output_budget_exhaustion_rate")
    try:
        with pytest.raises(MetricsError, match="outside metric_policy_hash"):
            _metrics(scores)
    finally:
        CANONICAL_METRIC_NAMES["operational"] = saved


# ══════════════════════════════════════════════════════════════════════════════
#  9. body-free evidence (§23, §54)
# ══════════════════════════════════════════════════════════════════════════════
def test_the_evidence_record_carries_the_verdict_and_no_body():
    """§23 — termination metadata only.

    The only ``response``-shaped key a record may carry is the digest; a field holding
    the text itself is what the closed allowlist exists to keep out.
    """
    record = score_evidence_record(_score("t1", exhausted=True),
                                   evaluation_id="synthetic", generation=1,
                                   response_sha256="d" * 64)
    assert record["output_budget_exhausted"] is True
    assert record["truncated"] is False
    assert set(record) <= set(SCORE_EVIDENCE_FIELDS)
    assert [k for k in record if k.startswith("response")] == ["response_sha256"]


def test_the_evidence_record_does_not_coerce_unmeasured_to_false():
    """§25 — ``bool(None)`` would publish an error as a clean completion."""
    record = score_evidence_record(_score("t1", exhausted=None),
                                   evaluation_id="synthetic", generation=1,
                                   response_sha256="d" * 64)
    assert record["output_budget_exhausted"] is None


def test_a_historical_evidence_record_without_the_new_field_still_reads():
    """§17 — the field list is an allowlist, so ``.1`` records verify unchanged."""
    legacy = {
        "evidence_version": "m62.evaluation_score_evidence.1",
        "scoring_version": "m62.evaluation_scoring.4", "evaluation_id": "historical",
        "generation": 1, "arm": "candidate", "task_id": "t1", "task_hash": "e" * 64,
        "family": "structured_report", "split": "hidden_evaluation", "status": "pass",
        "reward": 1.0, "refusal": "safe_completion", "schema_valid": True,
        "json_parseable": True, "evidence_findings": [], "tool_call_valid": None,
        "tool_call_critical": None, "tool_call_problem_count": 0,
        "security_findings": [], "hygiene_findings": [], "grader_statuses": {},
        "missing_graders": [], "blocking": False, "severity": "info", "latency_ms": 1,
        "output_tokens": 10, "truncated": False, "timed_out": False, "empty": False,
        "note_codes": [], "response_sha256": "f" * 64, "score_hash": "0" * 64,
    }
    parsed = read_score_evidence(legacy, label="historical")
    assert "output_budget_exhausted" not in parsed


def test_an_undeclared_field_is_still_refused():
    """The allowlist did not become permissive because it gained a member.

    ``ScoreEvidenceError`` and the shared ``SchemaError`` it derives from are both
    accepted: which one fires is an implementation detail of where the check lives, and
    the property under test is that the record is refused rather than written.
    """
    with pytest.raises(SchemaError):
        read_score_evidence(
            {"task_id": "t1", "arm": "candidate", "response_sha256": "f" * 64,
             "score_hash": "0" * 64, "a_field_no_writer_declared": "anything"},
            label="bad")
    assert issubclass(ScoreEvidenceError, SchemaError)


def test_the_report_surfaces_the_paired_diagnostic_and_no_verdict():
    """§32 — a future report can say it, and cannot say PASS/FAIL about it."""
    from training_gym.evaluation import reports as reports_module

    source = inspect.getsource(reports_module.EvaluationReport.to_dict)
    assert "output_budget_exhaustion_paired" in source
    assert "d38_gate" not in source.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 10. generation is untouched (§14, §40)
# ══════════════════════════════════════════════════════════════════════════════
def test_the_generation_policy_defaults_are_unchanged():
    """§40, §43.17 — 512, greedy, and the declared timeout."""
    from training_gym.evaluation.generation import GenerationPolicy

    policy = GenerationPolicy()
    assert policy.max_new_tokens == 512
    assert policy.do_sample is False


def test_the_eligibility_generation_policy_is_unchanged():
    """The one a future eligibility run binds: DISABLED reasoning, 512 tokens."""
    from training_gym.evaluation.generation import eligibility_generation_policy

    policy = eligibility_generation_policy()
    assert policy.max_new_tokens == 512
    assert policy.reasoning_policy.value == "disabled"


def test_the_backend_still_derives_the_finish_reason_the_same_way():
    """§14 — D38 observes the termination state; it does not produce or change one."""
    from training_gym.evaluation.backends import transformers_peft

    source = inspect.getsource(transformers_peft)
    assert "output_tokens >= policy.max_new_tokens" in source
    assert "output_budget_exhausted" not in source, (
        "the backend must keep producing finish_reason and nothing else; the "
        "classification lives in one place")


def test_the_d37_render_authority_is_untouched():
    """§34 — D37 stays FIXED and its module is not an S3M.2 dependency."""
    from training_gym.training import chat_render

    source = inspect.getsource(chat_render)
    assert "output_budget" not in source
    assert hasattr(chat_render, "ReasoningPolicy")


# ══════════════════════════════════════════════════════════════════════════════
# 11. the sealed history (§18, §19, §44)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_sealed_runs_reproduce_their_recorded_exhaustion_counts(run_key):
    """§18, §44 — the metric would have surfaced the same signal prospectively.

    Read through the PRODUCTION authority from body-free termination metadata. Not a
    rescore: no ArmScore is rebuilt, no gate runs and no eligibility is derived.
    """
    directory = _sealed(run_key)
    _, expected = SEALED_RUNS[run_key]
    for arm, want in expected.items():
        rows = _records(directory, arm)
        assert len(rows) == 36
        exhausted = sum(1 for r in rows
                        if r["status"] == "succeeded"
                        and FinishReason(r["finish_reason"]).output_budget_exhausted)
        assert exhausted == want, f"{run_key} {arm}: expected {want}, measured {exhausted}"


@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_sealed_runs_recorded_zero_input_truncation(run_key):
    """The legacy signal was correct all along — D38 is not an OG-3 bug."""
    directory = _sealed(run_key)
    for arm in ("baseline", "candidate"):
        assert not any(r["input_truncated"] for r in _records(directory, arm))


@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_sealed_records_pass_the_token_count_consistency_check(run_key):
    """§24 — every ceiling ending in the record generated its whole budget."""
    directory = _sealed(run_key)
    for arm in ("baseline", "candidate"):
        for row in _records(directory, arm):
            if row["status"] != "succeeded" or int(row["output_tokens"]) < 0:
                continue
            reached = int(row["output_tokens"]) >= MAX_NEW_TOKENS
            exhausted = FinishReason(row["finish_reason"]) is FinishReason.MAX_NEW_TOKENS
            assert reached == exhausted, row["task_id"]


def test_the_historical_baselines_never_failed_to_terminate():
    """§44 — 72 of 72, and the fact the diagnostic exists to keep visible."""
    total = 0
    for run_key in sorted(SEALED_RUNS):
        directory = _sealed(run_key)
        rows = _records(directory, "baseline")
        total += sum(1 for r in rows
                     if r["finish_reason"] == FinishReason.END_OF_SEQUENCE.value)
    assert total == 72


@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_sealed_reports_keep_their_verdicts_and_their_policy_hashes(run_key):
    """§17, §43.21 — history is a measurement under the OLD instrument."""
    directory = _sealed(run_key)
    report = json.loads((directory / "evaluation-report.json").read_text(
        encoding="utf-8"))
    assert report["metric_policy_hash"] == HISTORICAL_METRIC_POLICY_HASH
    assert report["gate_policy_hash"] == GATE_POLICY_HASH
    assert report["eligibility"]["eligibility"] == "not_eligible"


@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_sealed_generations_still_verify_from_disk(run_key):
    """§55 — the bytes were not rewritten and still re-derive."""
    from training_gym.evaluation.artifacts import verify_evaluation_generation

    assert verify_evaluation_generation(_sealed(run_key)) == ()


@pytest.mark.parametrize("run_key", sorted(SEALED_RUNS))
def test_the_structured_family_correlation_is_body_free(run_key):
    """§19, §45 — id, family, finish reason and the parse verdict. No bodies.

    Proves the relationship S3M recorded: in both runs every structured ceiling ending
    failed to parse, and no baseline structured generation reached the ceiling.
    """
    directory = _sealed(run_key)
    scores = {json.loads(line)["task_id"]: json.loads(line)
              for line in (directory / "candidate-scores.jsonl").read_text(
                  encoding="utf-8").splitlines() if line.strip()}
    results = {r["task_id"]: r for r in _records(directory, "candidate")}
    structured = [t for t, s in scores.items() if s["family"] == "structured_report"]
    assert len(structured) == 9
    exhausted = [t for t in structured
                 if results[t]["finish_reason"] == FinishReason.MAX_NEW_TOKENS.value]
    for task_id in exhausted:
        assert scores[task_id]["json_parseable"] is False
        assert scores[task_id]["schema_valid"] is False
    baseline_results = {r["task_id"]: r for r in _records(directory, "baseline")}
    assert not any(baseline_results[t]["finish_reason"]
                   == FinishReason.MAX_NEW_TOKENS.value for t in structured)


def test_this_file_reads_only_body_free_artefacts():
    """§43.25 — asserted over which files the tests open, not over their own prose.

    The held-out prompts and targets live in the task pack. Every artefact named below
    carries termination metadata, digests and verdicts and no body, which is what makes
    the whole retrospective body-free (§18, §23).
    """
    import re

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    named = set(re.findall(r"[a-z0-9-]+\.json[l]?", source))
    body_free = {
        "baseline-results.jsonl", "candidate-results.jsonl",
        "candidate-scores.jsonl", "evaluation-report.json",
        # what an f-string over the arm name leaves behind
        "-results.jsonl", "-scores.jsonl",
    }
    assert named <= body_free, (
        f"this file opens {sorted(named - body_free)}; the task pack holds the held-out "
        f"prompts and targets and must never be read here")
