"""V69 M62 S3M — the structured-output contract, pinned where the diagnosis found it.

WHAT S3I AND S3L MEASURED, TWICE, ON DIFFERENT HOLDOUTS
------------------------------------------------------
Both quality LoRA candidates scored ``json_parseable`` **7/9** and ``schema_valid``
**7/9** on the ``structured_report`` family, against a baseline that scored **9/9** on
both, under an unmoved ``gate_policy_hash e5003319…``. The holdouts share no task
instance, so the two 7/9s are not the same tasks — the invariant is at the CATEGORY
level.

S3M diagnosed it from repository evidence only: no training, no evaluation, no model
generation, no raw response body. What it found is pinned here so that a future session
does not re-derive it, and so that a change to any of it is a loud test failure rather
than a silent shift in what "structured output" means.

THE FIVE FACTS THESE TESTS PIN
------------------------------
1. **FG-2 has no independent content constraint for this family.** The response schema
   for ``STRUCTURED_REPORT`` is ``{"type": "object", "additionalProperties": true}`` —
   no ``required``, no ``enum``, no nesting. Every JSON object satisfies it. So
   ``schema_valid`` can only be False when the response did not parse at all, or parsed
   to something that is not an object.

2. **A parse failure MECHANICALLY forces a schema failure.** ``score_arm`` assigns
   ``schema_valid = False`` in the ``not json_parseable`` branch, without consulting the
   schema. FG-2's 7/9 is therefore inherited from FG-1's 7/9 whenever the failures are
   parse failures — which is what the body-free evidence recorded in both runs
   (``note_codes == ['structured_output_not_valid_json']`` on all four failing records,
   and no ``structured_output_schema_violation`` anywhere).

3. **What the parser accepts and rejects.** It tolerates exactly two things — a fenced
   block and a leading reasoning block — and nothing else. Prose on either side of the
   object, two objects, and an unclosed object are all rejected, all under the single
   code ``structured_output_not_valid_json``. It deliberately does not hunt for a
   JSON-looking substring inside prose.

4. **The supervised span reaches the end of the assistant turn.** ``build_labels``
   masks exactly the prompt prefix and supervises every remaining token — the closing
   brace and whatever turn-terminator the template appended included. The model is
   taught to stop; nothing masks the stop away.

5. **Training and evaluation do not render the same logical messages the same way.**
   The evaluation backend passes ``enable_thinking`` (the eligibility policy pins
   ``DISABLED``); the training backend passes no such keyword and has no reasoning
   policy at all, so it renders under the template's own default. Against the real
   pinned tokenizer those two renderings were measured to differ (S3F.2 addendum,
   2026-08-11: 79 chars / ``ca0259367339443e`` for the default, 98 chars /
   ``2b7898f3175013ff`` for ``DISABLED``). ``tokenizer_chat_template_hash`` digests the
   template SOURCE and is identical on both sides, so no identity in either chain
   records the difference. Pinned at source level, because that is where it lives.

WHAT THESE TESTS DELIBERATELY DO NOT DO
---------------------------------------
They contain no held-out task, no paraphrase of one, and no model output. Every string
below is synthetic and written here. They load no model, generate nothing, and assert
nothing about which held-out tasks failed.
"""
from __future__ import annotations

import inspect
import json

import pytest

from training_gym.evaluation.backends import transformers_peft as eval_backend
from training_gym.evaluation.pack_builder import (
    RESPONSE_SCHEMA_REGISTRY,
    response_schema_for,
)
from training_gym.evaluation.scoring import (
    NOTE_CODES,
    schema_satisfied,
    structured_output_detail,
)
from training_gym.evaluation.task_pack import TaskFamily
from training_gym.training.backends import transformers_peft as train_backend
from training_gym.training.dataset_conversion import (
    LOSS_IGNORE_INDEX,
    DatasetConversionError,
    build_labels,
    check_masking,
)

# The structured_report response schema, read from the registry rather than restated so
# that a change to the registry fails these tests instead of drifting past them.
STRUCTURED_SCHEMA = response_schema_for(TaskFamily.STRUCTURED_REPORT)

# One well-formed synthetic object, in the shape the structured family asks for. Written
# for this file; it is not any held-out target.
OBJECT = '{"severity":"medium","category":"policy","signals":["synthetic-a"],' \
         '"next_step":"escalate"}'


# ── 1. the schema contract ────────────────────────────────────────────────────────

def test_the_structured_family_schema_constrains_only_the_json_type():
    """FG-2 declares a type and stops. No required field, no enum, no nesting rule."""
    assert STRUCTURED_SCHEMA == {"type": "object", "additionalProperties": True}
    assert "required" not in STRUCTURED_SCHEMA
    assert "properties" not in STRUCTURED_SCHEMA
    assert "enum" not in json.dumps(STRUCTURED_SCHEMA)


@pytest.mark.parametrize("document", [
    {},
    {"anything": 1},
    {"severity": "medium", "nested": {"deep": [1, 2, {"deeper": None}]}},
    {"severity": None},
])
def test_every_json_object_satisfies_the_structured_family_schema(document):
    """So a parseable OBJECT can never fail FG-2 on content for this family."""
    satisfied, why = schema_satisfied(document, STRUCTURED_SCHEMA)
    assert satisfied is True, why


@pytest.mark.parametrize("document", [[], ["severity", "medium"], "medium", 42, None])
def test_parseable_json_that_is_not_an_object_fails_the_schema(document):
    """The one independent way FG-2 can fail: valid JSON of the wrong TYPE.

    This is the D26b failure mode, and it is still caught. It is simply not what either
    candidate did — every recorded failure carried a parse code, not a schema code.
    """
    satisfied, why = schema_satisfied(document, STRUCTURED_SCHEMA)
    assert satisfied is False
    assert why


def test_an_absent_validator_is_undecidable_and_never_a_pass():
    """Fail-closed: no jsonschema means INSUFFICIENT_EVIDENCE, never conformance."""
    satisfied, why = schema_satisfied({"a": 1}, {})
    assert satisfied is None
    assert "no expected_output_schema" in why


def test_every_family_in_the_registry_declares_a_schema():
    """A family with no schema would make FG-2 vacuous for it, D28-style."""
    for family in RESPONSE_SCHEMA_REGISTRY:
        assert response_schema_for(family)


# ── 2. FG-2 inherits FG-1 ─────────────────────────────────────────────────────────

def test_score_arm_forces_schema_invalid_when_the_response_does_not_parse():
    """The source of the inheritance, pinned at the branch that creates it.

    Asserted over the source rather than by running a full scoring pass because the
    point is the ORDER of the two assignments: ``schema_valid`` is set False in the
    not-parseable branch, before any schema is consulted. A future refactor that
    consulted the schema first would change what a 7/9 on FG-2 means.
    """
    from training_gym.evaluation import scoring

    source = inspect.getsource(scoring.score_arm)
    assert "if not json_parseable:" in source
    branch = source.split("if not json_parseable:", 1)[1]
    head = branch.split("else:", 1)[0]
    assert "schema_valid = False" in head, (
        "score_arm no longer forces schema_valid False on a parse failure; FG-2 and "
        "FG-1 are no longer mechanically linked and the S3M diagnosis must be re-read")


# ── 3. the JSON parser contract ───────────────────────────────────────────────────

ACCEPTED = [
    ("bare object", OBJECT),
    ("trailing newline", OBJECT + "\n"),
    ("surrounding whitespace", "  " + OBJECT + "  \n"),
    ("json fence", "```json\n" + OBJECT + "\n```"),
    ("bare fence", "```\n" + OBJECT + "\n```"),
    ("leading reasoning block", "<think>synthetic reasoning</think>\n" + OBJECT),
    ("pretty printed", json.dumps(json.loads(OBJECT), indent=2)),
]

REJECTED_AS_INVALID_JSON = [
    ("prose before", "Here is the report:\n" + OBJECT),
    ("prose after", OBJECT + "\nLet me know if you need more detail."),
    ("two objects", OBJECT + "\n" + OBJECT),
    ("two objects separated by a blank line", OBJECT + "\n\n" + OBJECT),
    ("two fenced blocks", "```json\n" + OBJECT + "\n```\n```json\n" + OBJECT + "\n```"),
    ("unclosed object", OBJECT[:-1]),
    ("trailing comma", '{"severity":"medium",}'),
    ("single quotes", "{'severity':'medium'}"),
]


@pytest.mark.parametrize("label,text", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_the_parser_accepts_exactly_these_shapes(label, text):
    del label
    parsed, problem, code = structured_output_detail(text)
    assert parsed is not None, problem
    assert not problem and not code
    satisfied, why = schema_satisfied(parsed, STRUCTURED_SCHEMA)
    assert satisfied is True, why


@pytest.mark.parametrize("label,text", REJECTED_AS_INVALID_JSON,
                         ids=[c[0] for c in REJECTED_AS_INVALID_JSON])
def test_the_parser_rejects_these_under_one_code(label, text):
    """All of them, including a response cut off mid-object, report the SAME code.

    This is why the body-free evidence cannot distinguish "the model wrote prose around
    the object" from "the model never finished the object": both are
    ``structured_output_not_valid_json``. The distinction has to come from the
    termination metadata instead.
    """
    del label
    parsed, problem, code = structured_output_detail(text)
    assert parsed is None
    assert problem
    assert code == "structured_output_not_valid_json"
    assert code in NOTE_CODES


def test_an_empty_answer_and_an_unterminated_reasoning_block_get_their_own_codes():
    """So a 7/9 caused by either would be visibly a DIFFERENT finding."""
    _parsed, _problem, code = structured_output_detail("")
    assert code == "structured_output_empty"
    _parsed, _problem, code = structured_output_detail("<think>synthetic</think>")
    assert code == "structured_output_never_left_reasoning_block"


def test_the_parser_does_not_hunt_for_an_object_inside_prose():
    """The property that makes 'wrapped in commentary' a contract violation."""
    parsed, _problem, _code = structured_output_detail(
        "The answer is " + OBJECT + " as shown above.")
    assert parsed is None


# ── 4. the supervised span ────────────────────────────────────────────────────────
#
# A deterministic synthetic renderer, not a tokenizer. It reproduces the ONE property
# _encode depends on: the prompt rendering is a strict token prefix of the full-turn
# rendering, and the full-turn rendering appends a terminator after the completion.

PROMPT_IDS = [1, 2, 3, 4, 5]          # ... <im_start> assistant \n
COMPLETION_IDS = [10, 11, 12, 13]     # the JSON body, closing brace last
TERMINATOR_IDS = [99]                 # <|im_end|> — the stop the model must learn


def test_the_prompt_is_fully_masked_and_the_completion_fully_supervised():
    labels = build_labels(list(PROMPT_IDS),
                          PROMPT_IDS + COMPLETION_IDS + TERMINATOR_IDS)
    assert labels[:len(PROMPT_IDS)] == [LOSS_IGNORE_INDEX] * len(PROMPT_IDS)
    assert labels[len(PROMPT_IDS):] == COMPLETION_IDS + TERMINATOR_IDS
    assert check_masking(labels, len(PROMPT_IDS)) == ()


def test_the_closing_token_of_the_object_is_supervised():
    """The last token of the completion — a JSON object's closing brace — carries loss."""
    labels = build_labels(list(PROMPT_IDS),
                          PROMPT_IDS + COMPLETION_IDS + TERMINATOR_IDS)
    closing = COMPLETION_IDS[-1]
    assert labels[len(PROMPT_IDS) + len(COMPLETION_IDS) - 1] == closing
    assert closing != LOSS_IGNORE_INDEX


def test_the_turn_terminator_is_supervised_so_stopping_is_taught():
    """If the terminator were masked, nothing would teach the model to stop.

    It is not masked: ``build_labels`` supervises every token after the prompt prefix,
    and the full-turn rendering (``add_generation_prompt=False``) is what carries the
    terminator. Termination IS in the training objective — which is what makes the
    measured termination drift a fitting effect rather than a missing target.
    """
    labels = build_labels(list(PROMPT_IDS),
                          PROMPT_IDS + COMPLETION_IDS + TERMINATOR_IDS)
    assert labels[-len(TERMINATOR_IDS):] == TERMINATOR_IDS


def test_a_prompt_that_is_not_a_prefix_is_refused_rather_than_guessed():
    with pytest.raises(DatasetConversionError):
        build_labels([1, 2, 3], [1, 2, 9, 10, 11])


def test_a_completion_of_nothing_is_refused():
    with pytest.raises(DatasetConversionError):
        build_labels(list(PROMPT_IDS), list(PROMPT_IDS))


def test_check_masking_catches_a_supervised_prompt_token():
    """Non-vacuous: the self-test fails when the mask is wrong."""
    bad = [LOSS_IGNORE_INDEX] * (len(PROMPT_IDS) - 1) + [7] + COMPLETION_IDS
    problems = check_masking(bad, len(PROMPT_IDS))
    assert problems and "not masked" in problems[0]


# ── 5. the train/eval rendering divergence ────────────────────────────────────────

def test_the_evaluation_backend_binds_a_reasoning_policy_into_the_template_call():
    source = inspect.getsource(eval_backend)
    assert 'template_kwargs["enable_thinking"] = thinking' in source
    assert "add_generation_prompt=True" in source


def test_the_training_backend_binds_no_reasoning_policy_at_all():
    """The asymmetry, pinned so that closing it is a deliberate act.

    The training backend renders with ``apply_chat_template`` and passes no
    ``enable_thinking``, so the template's own default applies. The evaluation backend
    passes one. Against the real pinned tokenizer the two renderings were measured to
    differ, so the LoRA is fitted under one generation prefix and evaluated under
    another. Nothing in the training config, plan, adapter manifest or chat-template
    digest records which rendering was used.

    If a future milestone binds a reasoning policy on the training side, this test
    fails — which is the point. It should fail loudly and be updated deliberately.
    """
    source = inspect.getsource(train_backend)
    assert "enable_thinking" not in source, (
        "the training backend now names enable_thinking; the S3M train/eval rendering "
        "asymmetry has changed and the diagnosis must be re-read")
    assert "reasoning_policy" not in source


def test_the_chat_template_digest_cannot_distinguish_the_two_renderings():
    """It digests the template SOURCE, so both sides record the same value.

    That is why ``tokenizer_chat_template_hash a55ee1b1…`` matching across S3H, S3K,
    S3I and S3L is NOT evidence that training and evaluation rendered alike.
    """
    from training_gym.training.dataset_conversion import chat_template_hash

    template = "{% for m in messages %}{{ m['role'] }}{% endfor %}"
    assert chat_template_hash(template) == chat_template_hash(template)
    assert chat_template_hash("") == ""


# ── 6. what the truncation gate actually measures ─────────────────────────────────

def test_arm_score_truncated_reports_INPUT_truncation_not_output_budget():
    """OG-3's ``truncation 0/9`` is about the PROMPT, not about the response.

    A response that ran to ``max_new_tokens`` is recorded only in ``finish_reason``.
    Reading OG-3's zero as "no response was cut off" is the misreading this pins
    against: in S3L the candidate reached the ceiling on 5 of 36 tasks while OG-3
    correctly reported 0/9 truncation.
    """
    from training_gym.evaluation import scoring

    source = inspect.getsource(scoring.score_arm)
    assert "truncated=result.input_truncated" in source
