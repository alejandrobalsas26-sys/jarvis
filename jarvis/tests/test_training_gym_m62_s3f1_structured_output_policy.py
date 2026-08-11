"""V69 M62 S3F.1 — the structured-output contract against a reasoning model.

WHAT S3E.2 MEASURED, AND WHY IT COULD NOT DISCRIMINATE
-----------------------------------------------------
``schema_validity_rate`` was 0/9 on BOTH arms. The nine tasks are exactly the
``structured_report`` family — the only family in the held-out corpus whose mandatory
grader set contains ``json_schema``.

S3F recorded a hypothesis and did not trace it. S3F.1 traced it, and it is two separate
defects that happened to produce one number:

* **D26a — thinking was hidden backend behaviour.** The evaluation backend called
  ``apply_chat_template`` without ``enable_thinking``, so a reasoning model's template
  default applied and every response began with a ``<think>`` block. Nothing stripped it
  before the structural check, and ``json.loads`` over a whole response that starts with
  ``<`` cannot succeed. A setting that changes every output belongs in the digest both
  arms are compared under, not in a kwarg the backend happens to omit.
* **D26b — ``schema_valid`` never validated the schema.** It was assigned
  ``parsed is not None``, so a response that parsed as ``["severity", "medium"]`` — an
  array, against a declared ``{"type": "object"}`` — was recorded as schema-valid. The
  name said "schema", the number said "parseable", and the two failure modes an operator
  would act on differently were reported as one.

WHAT THE CORRECTION MUST NOT DO
-------------------------------
It must not manufacture passes. A reasoning block is transport and is stripped for the
STRUCTURAL check only; the security scan still reads the whole raw response, so a
credential hidden inside a ``<think>`` block still blocks. Prose wrapped around a JSON
object is still a contract violation, because the parser does not go hunting for braces.
All of that is pinned below.
"""
from __future__ import annotations

import pytest

from training_gym.datasets.manifests import DatasetSplit
from training_gym.evaluation.backend import (
    BackendStatus,
    EvaluationResult,
    FinishReason,
)
from training_gym.evaluation.generation import (
    GenerationPolicy,
    GenerationPolicyError,
    ReasoningPolicy,
    assert_identical_policies,
)
from training_gym.evaluation.metrics import build_arm_metrics
from training_gym.evaluation.pack_builder import graders_for, response_schema_for
from training_gym.evaluation.policy import GraderPolicy, MetricPolicy
from training_gym.evaluation.references import EvaluationRole
from training_gym.evaluation.scoring import (
    SCORING_VERSION,
    final_answer,
    schema_satisfied,
    score_arm,
    structured_output,
)
from training_gym.evaluation.task_pack import (
    EvaluationTask,
    EvaluationTaskKind,
    HiddenTarget,
)
from training_gym.schemas import (
    ResultStatus,
    scan_private_content,
    sha256_obj,
    sha256_text,
)
from training_gym.task_spec import TaskFamily

#: A Qwen3-shaped answer: the reasoning block the model's own protocol emits first.
_THINKING = ("<think>\nFour failures then one success. Credential access. Severity "
             "medium.\n</think>\n\n")
#: The real he-report-01 answer shape from the held-out corpus.
_ANSWER = ('{"severity": "medium", "supporting_signals": ["burst_of_failures", '
           '"single_source_host"], "category": "credential_access"}')
_FENCED = "```json\n" + _ANSWER + "\n```"

_FAMILY = TaskFamily.STRUCTURED_REPORT
_SCHEMA = response_schema_for(_FAMILY)
_GRADERS, _MANDATORY = graders_for(_FAMILY)
_POLICY = GraderPolicy()


def _task(*, schema=None) -> EvaluationTask:
    """A real ``structured_report`` task, built with the production response schema."""
    prompt = ("Four failed logons for one service account arrived from a single "
              "internal host inside two minutes, followed by one success. Produce the "
              "incident summary object with a severity field.")
    declared = _SCHEMA if schema is None else schema
    return EvaluationTask(
        task_id="he-report-01", task_family=_FAMILY,
        task_hash=sha256_text("he-report-01"),
        split=DatasetSplit.HIDDEN_EVALUATION, kind=EvaluationTaskKind.COMPLETION,
        system_prompt="", user_prompt=prompt,
        source_dataset_manifest_hash=sha256_text("manifest"),
        source_shard_hash=sha256_text("shard"),
        input_record_hash=sha256_obj({"system": "", "user": prompt}),
        expected_output_schema=dict(declared),
        grader_ids=tuple(_GRADERS), mandatory_grader_ids=tuple(_MANDATORY))


def _score(text: str, *, schema=None):
    task = _task(schema=schema)
    target = HiddenTarget(task_id=task.task_id, task_hash=task.task_hash,
                          target_text=_ANSWER, target_hash=sha256_text(_ANSWER))
    result = EvaluationResult(
        backend_id="fake_evaluation", backend_version="1",
        role=EvaluationRole.CANDIDATE, task_id=task.task_id, task_hash=task.task_hash,
        status=BackendStatus.SUCCEEDED, response_text=text, input_tokens=100,
        output_tokens=max(1, len(text) // 4), latency_ms=100,
        finish_reason=FinishReason.END_OF_SEQUENCE)
    return score_arm(task, result, target=target, policy=_POLICY)


# ══════════════════════════════════════════════════════════════════════════════
#  final_answer — transport is separated from content, and from nothing else
# ══════════════════════════════════════════════════════════════════════════════
def test_a_closed_reasoning_block_is_separated_from_the_answer():
    answer, blocks = final_answer(_THINKING + _ANSWER)
    assert answer == _ANSWER
    assert blocks == 1


def test_an_unterminated_reasoning_block_leaves_no_answer():
    """A response cut off at max_new_tokens never left its reasoning. Five of the
    eighteen structured generations across the two S3E.2 arms finished that way."""
    answer, blocks = final_answer("<think>\nStill reasoning when the budget ran out")
    assert answer == ""
    assert blocks == 1


def test_a_response_without_reasoning_is_returned_untouched():
    answer, blocks = final_answer(_ANSWER)
    assert answer == _ANSWER
    assert blocks == 0


def test_the_security_scan_still_reads_the_whole_raw_response():
    """The extractor is for STRUCTURE. A credential inside a think block still blocks —
    stripping before the security scan would turn thinking into a hiding place."""
    hidden = "<think>\nthe file is at /home/victim/creds.txt\n</think>\n\nI cannot help."
    assert "home_path" in scan_private_content(hidden)
    score = _score(hidden)
    assert "secret_pii:home_path" in score.security_findings
    assert score.blocking


# ══════════════════════════════════════════════════════════════════════════════
#  structured_output — the thinking prefix, and the tolerances that stay refused
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text", [
    _THINKING + _ANSWER,
    _THINKING + _FENCED,
    "<think>reasoning</think>" + _ANSWER,
    _ANSWER,
    _FENCED,
])
def test_a_reasoning_prefix_no_longer_defeats_the_json_contract(text):
    parsed, problem = structured_output(text)
    assert parsed is not None, problem
    assert not problem


@pytest.mark.parametrize("text", [
    "Here is the incident object:\n\n" + _ANSWER,
    _ANSWER + "\n\nThis reflects a credential-access pattern.",
    "The severity is medium because four failures preceded one success.",
    _THINKING + "The severity is medium.",
])
def test_prose_around_the_object_is_still_a_contract_violation(text):
    """The parser does not hunt for braces. A task that asked for an object and got one
    wrapped in commentary did not honour the contract."""
    parsed, _problem = structured_output(text)
    assert parsed is None


def test_never_leaving_the_reasoning_block_is_named_distinctly():
    """Distinct from "not valid JSON": nothing was malformed, the model simply never
    emitted an answer before its token budget ran out."""
    parsed, problem = structured_output("<think>\nreasoning that never ends")
    assert parsed is None
    assert "never left its reasoning block" in problem


# ══════════════════════════════════════════════════════════════════════════════
#  schema_satisfied — the check the name always claimed
# ══════════════════════════════════════════════════════════════════════════════
def test_a_conforming_document_satisfies_the_declared_schema():
    assert schema_satisfied({"severity": "medium"}, _SCHEMA) == (True, "")


@pytest.mark.parametrize("document", [["severity", "medium"], "medium", 3])
def test_valid_json_of_the_wrong_shape_does_not_satisfy_an_object_schema(document):
    satisfied, why = schema_satisfied(document, _SCHEMA)
    assert satisfied is False
    assert "schema violation" in why


def test_an_undecidable_schema_is_never_an_optimistic_pass():
    satisfied, why = schema_satisfied({"a": 1}, {})
    assert satisfied is None
    assert why


# ══════════════════════════════════════════════════════════════════════════════
#  score_arm — the two numbers are separated
# ══════════════════════════════════════════════════════════════════════════════
def test_a_thinking_response_with_a_valid_object_is_now_measurable():
    score = _score(_THINKING + _ANSWER)
    assert score.json_parseable is True
    assert score.schema_valid is True
    assert ResultStatus(score.grader_statuses["json_schema"]) is ResultStatus.PASS
    # Still detected, still reported, still not a security finding.
    assert score.hygiene_findings == ("reasoning",)
    assert not score.blocking


def test_json_of_the_wrong_shape_is_parseable_and_not_schema_valid():
    """The conflation D26b closed: this scored schema_valid=True before S3F.1."""
    score = _score('["severity", "medium"]')
    assert score.json_parseable is True
    assert score.schema_valid is False
    assert ResultStatus(score.grader_statuses["json_schema"]) is ResultStatus.FAIL


def test_a_response_with_no_json_at_all_fails_both():
    score = _score(_THINKING + "The severity is medium.")
    assert score.json_parseable is False
    assert score.schema_valid is False


def test_a_task_declaring_no_schema_records_that_parsing_was_the_whole_contract():
    score = _score(_ANSWER, schema={})
    assert score.json_parseable is True
    assert score.schema_valid is True
    assert any("strongest claim available" in note for note in score.notes)


def test_the_scoring_version_records_that_a_verdict_changed():
    # S3F.1 set .3 for the structured-output correction. S3F.2 added ``note_codes`` to
    # ``ArmScore.to_dict``, which moves every ``score_hash``, so the version moved again.
    # What S3F.1 needs is that the version is never again one that predates its fix.
    assert SCORING_VERSION not in ("m62.evaluation_scoring.1",
                                   "m62.evaluation_scoring.2")
    assert SCORING_VERSION.startswith("m62.evaluation_scoring.")


def test_the_two_failure_modes_are_reported_as_separate_metrics():
    scores = [_score(_THINKING + _ANSWER), _score('["severity"]'),
              _score("no json here at all")]
    metrics = build_arm_metrics(
        scores, role="candidate", families=[_FAMILY.value],
        splits=[DatasetSplit.HIDDEN_EVALUATION.value], policy=MetricPolicy(),
        task_count=len(scores))
    quality = metrics.to_dict()["quality"]
    assert quality["schema_validity_rate"]["numerator"] == 1
    assert quality["json_parseable_rate"]["numerator"] == 2
    assert quality["json_parseable_rate"]["denominator"] == 3


# ══════════════════════════════════════════════════════════════════════════════
#  ReasoningPolicy — the setting stops being hidden
# ══════════════════════════════════════════════════════════════════════════════
def test_the_default_preserves_exactly_what_s3e2_did():
    """MODEL_DEFAULT passes nothing and lets the template decide, which is the behaviour
    that produced the historical measurement. A different default would silently
    reinterpret every existing configuration."""
    assert GenerationPolicy().reasoning_policy is ReasoningPolicy.MODEL_DEFAULT
    assert ReasoningPolicy.MODEL_DEFAULT.template_kwarg is None


@pytest.mark.parametrize("policy, expected", [
    (ReasoningPolicy.DISABLED, False),
    (ReasoningPolicy.ENABLED, True),
])
def test_an_explicit_policy_names_the_value_the_template_receives(policy, expected):
    assert policy.template_kwarg is expected


def test_the_reasoning_policy_travels_in_the_digest_both_arms_are_compared_under():
    default = GenerationPolicy()
    disabled = GenerationPolicy(reasoning_policy=ReasoningPolicy.DISABLED)
    assert default.policy_hash() != disabled.policy_hash()
    assert default.to_dict()["reasoning_policy"] == "model_default"


def test_two_arms_that_think_differently_cannot_be_compared():
    """Otherwise the delta measures the decoder, not the adapter."""
    with pytest.raises(GenerationPolicyError):
        assert_identical_policies(
            GenerationPolicy(),
            GenerationPolicy(reasoning_policy=ReasoningPolicy.DISABLED))


def test_the_policy_survives_a_round_trip():
    policy = GenerationPolicy(reasoning_policy=ReasoningPolicy.DISABLED)
    assert GenerationPolicy.from_dict(policy.to_dict()) == policy


def test_an_unknown_reasoning_policy_is_refused():
    with pytest.raises(Exception):
        GenerationPolicy(reasoning_policy="sometimes")


# ══════════════════════════════════════════════════════════════════════════════
#  The backend refuses a request the template cannot honour
# ══════════════════════════════════════════════════════════════════════════════
class _Template:
    """A tokenizer stub. ``honours=False`` renders the same string whatever it is
    asked, which is what a template that never reads the keyword does."""

    def __init__(self, *, honours: bool) -> None:
        self.honours = honours

    def apply_chat_template(self, messages, *, tokenize=False,
                            add_generation_prompt=True, enable_thinking=None, **_kw):
        if self.honours and enable_thinking is False:
            return "PROMPT<think>\n\n</think>\n\n"
        return "PROMPT"


def _honours(tokenizer) -> bool:
    from training_gym.evaluation.backends.transformers_peft import (
        _template_honours_thinking,
    )
    return _template_honours_thinking(tokenizer, [{"role": "user", "content": "hi"}])


def test_a_template_that_implements_the_knob_is_recognised():
    assert _honours(_Template(honours=True))


def test_a_template_that_ignores_the_knob_is_not_treated_as_honouring_it():
    """A silent no-op in a setting the report records as applied is worse than a
    refusal, so the backend must be able to tell the difference."""
    assert not _honours(_Template(honours=False))


def test_a_template_that_raises_on_the_keyword_does_not_honour_it():
    class _Refuses:
        def apply_chat_template(self, *_args, **_kwargs):
            raise TypeError("unexpected keyword argument 'enable_thinking'")

    assert not _honours(_Refuses())
