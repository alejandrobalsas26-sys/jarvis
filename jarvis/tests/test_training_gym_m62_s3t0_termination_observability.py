"""V69 M62 S3T.0 — prospective BODY-FREE termination observability.

WHAT S3S.1 ESTABLISHED
----------------------
The spent eval-v4 response bodies were never persisted, and that privacy property is
deliberate and stays. But the evaluation-time code already HELD, transiently, enough
body-free information to answer the question the postmortem could not:
``structured_output_detail`` catches ``json.JSONDecodeError`` and reads ``exc.msg`` and
``exc.lineno`` into ``ArmScore.notes`` — and ``notes`` is never persisted. Only the
closed note code ``structured_output_not_valid_json`` reached disk.

So for candidate003's one eligibility-deciding structured failure the persisted evidence
for

    A. the first JSON document never became valid or closed

and

    B. a valid first JSON document completed and further output followed

was IDENTICAL, save for a response digest whose body nobody kept. That distinction is
what S3T.0 makes observable, PROSPECTIVELY.

WHAT THIS MILESTONE IS NOT
--------------------------
It does not recover candidate003 and does not reinterpret it. Nothing is backfilled onto
a historical record. No response body, excerpt, shingle, token id or parser snippet is
persisted, and ``RAW_RESPONSE_PERSISTED`` stays false. No grader, metric, statistic,
gate, threshold or eligibility rule reads any field added here: they are diagnostics, and
a diagnostic that could change a verdict would be a gate wearing a different name.

THE PROPERTIES THESE TESTS PIN
------------------------------
1. **EXTRA_DATA is directly observable** and is a different persisted fact from every
   class that means "the document never closed".
2. **The vocabulary is closed and fails safe.** ``exc.msg`` is never persisted — on a
   build without the C accelerator it QUOTES the offending character — and a message
   this build has never seen becomes ``other_json_parse_error``.
3. **One parser, one parse.** The class comes from the JSONDecodeError the already
   existing attempt raised; nothing re-parses and nothing second-guesses it.
4. **The repetition statistic separates degeneracy from novel continuation** by two
   orders of magnitude, and defines no threshold.
5. **Nothing scientific moves**: every policy identity, every grader status, every
   reward and every gate input is byte-identical on synthetic fixtures.
6. **Historical evidence still reads**, and gains no invented values.

Every fixture below is synthetic and written here. No model is loaded, nothing is
generated, and no held-out prompt, target or response body appears.
"""
from __future__ import annotations

import inspect
import json

import pytest

from training_gym.evaluation import gates as gates_module
from training_gym.evaluation import metrics as metrics_module
from training_gym.evaluation.backend import (
    BackendStatus,
    EvaluationResult,
    EvaluationRole,
)
from training_gym.evaluation.generation import GenerationPolicy
from training_gym.evaluation.policy import (
    EvaluationPolicySet,
    GatePolicy,
    GraderPolicy,
    MetricPolicy,
    ResourceCeilings,
    StatisticalPolicy,
    TaskFamilyPolicy,
)
from training_gym.evaluation.score_evidence import (
    SCORE_EVIDENCE_FIELDS,
    SCORE_EVIDENCE_VERSION,
    read_score_evidence,
    score_evidence_record,
)
from training_gym.evaluation.scoring import (
    RESPONSE_NGRAM_WINDOW,
    ArmScore,
    JsonParseDiagnosis,
    JsonParseErrorKind,
    RefusalClass,
    ScoringError,
    classify_json_parse_error,
    score_arm,
    structured_output,
    structured_output_detail,
    structured_output_diagnosis,
    unique_char_ngram_ratio,
)
from training_gym.schemas import ResultStatus, SchemaError

from _m62_evaluation_fixtures import make_pack, make_store

_POLICIES = EvaluationPolicySet()

#: The policy identities as they stood at S3S (generation 7). S3T.0 adds observability
#: and must move NONE of them.
_GENERATION_POLICY_HASH = (
    "e63cf7ed0e940eed6a0c69d69ee5f01b06aa371f05d7576423e402e098f7b216")
_GRADER_POLICY_HASH = (
    "2059579278f42d159447b3f281df2fa5b34e058d03cf944f7f0b8547763447b2")
_METRIC_POLICY_HASH = (
    "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")
_GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
_STATISTICAL_POLICY_HASH = (
    "663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18")
_FAMILY_POLICY_HASH = (
    "580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1")
_RESOURCE_POLICY_HASH = (
    "0486300a3bca61717b0dd119721915709a4f34dd403f5ecdd45eb209bef65834")
_POLICY_SET_HASH = (
    "eae948cc059fd639597107a0e76587681545e579376e96af87781c127eb3302e")

#: The persisted fields S3T.0 adds. Everything else in the record predates it.
_NEW_FIELDS = ("json_parse_error_kind", "json_parse_error_line",
               "json_parse_error_column", "json_parse_error_position",
               "response_unique_char_ngram_ratio")

#: The evidence record exactly as ``.2`` wrote it — the field list before S3T.0.
_PRE_S3T0_FIELDS = tuple(f for f in SCORE_EVIDENCE_FIELDS if f not in _NEW_FIELDS)


# ══════════════════════════════════════════════════════════════════════════════
#  Scoring one synthetic response through the PRODUCTION path
# ══════════════════════════════════════════════════════════════════════════════
def _code_of(function) -> str:
    """A function's source with its docstring removed.

    The scans below assert that certain WORDS do not appear in an implementation.
    The docstrings deliberately discuss tokenisers, hashing and thresholds in order
    to explain why none of them is used, so a scan that read the prose would fail
    on the explanation rather than on the code.
    """
    source = inspect.getsource(function)
    if not inspect.getdoc(function):
        return source
    marker = chr(34) * 3
    head, sep, rest = source.partition(marker)
    if not sep:
        return source
    _body, _sep, tail = rest.partition(marker)
    return head + tail


def _scored(text: str):
    """Score one synthetic response through ``score_arm``, not a re-implementation."""
    from dataclasses import replace
    pack = make_pack()
    task = replace(pack.tasks[0],
                   expected_output_schema={"type": "object",
                                           "additionalProperties": True})
    store = make_store(pack)
    target = (store.lookup(task.task_id, task_hash=task.task_hash)
              if task.task_id in store else None)
    result = EvaluationResult(
        backend_id="fake_evaluation", backend_version="m62.fake_evaluation.1",
        role=EvaluationRole.BASELINE, task_id=task.task_id, task_hash=task.task_hash,
        status=BackendStatus.SUCCEEDED, response_text=text, output_tokens=32,
        latency_ms=10)
    return score_arm(task, result, target=target, policy=_POLICIES.graders), result


def _record(text: str) -> dict:
    score, result = _scored(text)
    return score_evidence_record(score, evaluation_id="eval-s3t0", generation=8,
                                 response_sha256=result.to_dict()["response_sha256"])


# ══════════════════════════════════════════════════════════════════════════════
#  §5 — the gap this milestone closes
# ══════════════════════════════════════════════════════════════════════════════
def test_the_discriminator_was_computed_before_and_is_now_persisted():
    """The parser always knew; the artefact never did."""
    _value, problem, code, diagnosis = structured_output_diagnosis('{"a": 1} trailing')
    # It was COMPUTED before S3T.0 — the prose has always quoted exc.msg — and the prose
    # has always stayed in memory.
    assert "Extra data" in problem
    assert code == "structured_output_not_valid_json"
    record = _record('{"a": 1} trailing')
    assert "notes" not in record
    assert "Extra data" not in json.dumps(record)
    # And it is now PERSISTED, as a class rather than as the sentence.
    assert diagnosis is not None
    assert record["json_parse_error_kind"] == JsonParseErrorKind.EXTRA_DATA.value


def test_the_two_hypotheses_s3r_could_not_separate_are_now_different_records():
    """A. the document never closed  vs  B. it closed and output continued."""
    never_closed = _record('{"alpha": {"beta": [1, 2, 3]')
    closed_then_more = _record('{"alpha": {"beta": [1, 2, 3]}} and then commentary')
    assert never_closed["json_parse_error_kind"] != \
        closed_then_more["json_parse_error_kind"]
    assert closed_then_more["json_parse_error_kind"] == "extra_data"
    assert never_closed["json_parse_error_kind"] != "extra_data"
    # Before S3T.0 the two records differed in NOTHING a reviewer could read. Both
    # opaque digests are excluded here: ``response_sha256`` covers a body that was
    # never persisted, and ``score_hash`` now covers the new diagnostics
    # transitively — a digest that differs still tells a reviewer only THAT
    # something differs, never which of the two hypotheses happened.
    opaque = {"response_sha256", "score_hash"}
    legacy_a = {k: never_closed[k] for k in _PRE_S3T0_FIELDS if k not in opaque}
    legacy_b = {k: closed_then_more[k] for k in _PRE_S3T0_FIELDS if k not in opaque}
    assert legacy_a == legacy_b, (
        "the pre-S3T.0 readable record is expected to be blind to this "
        "distinction; if it is not, this test is measuring the wrong thing")
    assert never_closed["score_hash"] != closed_then_more["score_hash"]


# ══════════════════════════════════════════════════════════════════════════════
#  §15 — JSON non-vacuity. Substantially different literals, not one string mutated.
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text", [
    '{"severity": "low"}',
    '{"host": "sensor-12", "events": [{"id": 4}, {"id": 9}], "confidence": 0.75}',
    '[]',
    '{"nested": {"deep": {"deeper": [true, false, null, -3.5e2]}}}',
    '  \n  {"padded": "with whitespace"}   \n ',
    '```json\n{"fenced": ["a", "b"]}\n```',
])
def test_valid_json_reports_no_parse_error(text):
    """§15 — valid JSON: no parse error, canonical not-applicable location."""
    value, _problem, code, diagnosis = structured_output_diagnosis(text)
    assert value is not None
    assert code == ""
    assert diagnosis is None
    record = _record(text)
    assert record["json_parseable"] is True
    for field in ("json_parse_error_kind", "json_parse_error_line",
                  "json_parse_error_column", "json_parse_error_position"):
        assert record[field] is None, f"{field} must be null for a document that parsed"


@pytest.mark.parametrize("text", [
    '{"severity": "low"',
    '{"host": "sensor-12", "events": [{"id": 4}, {"id": 9}]',
    '{"a": 1, "b": 2',
    '[1, 2, 3',
])
def test_a_truncated_container_is_a_non_extra_data_class(text):
    """§15 — a container that never closed must NOT look like a completed document."""
    _v, _p, _c, diagnosis = structured_output_diagnosis(text)
    assert diagnosis is not None
    assert diagnosis.kind is not JsonParseErrorKind.EXTRA_DATA
    assert diagnosis.kind in (JsonParseErrorKind.EXPECTING_DELIMITER,
                              JsonParseErrorKind.EXPECTING_VALUE)


@pytest.mark.parametrize("text", [
    '{"severity": "lo',
    '{"host": "sensor-12", "summary": "the process spawned a child and then',
    '["first entry", "second entry which never end',
])
def test_a_truncated_string_is_named_as_such(text):
    """§15 — Python exposes this exact class, so the vocabulary carries it."""
    _v, _p, _c, diagnosis = structured_output_diagnosis(text)
    assert diagnosis is not None
    assert diagnosis.kind is JsonParseErrorKind.UNTERMINATED_STRING


@pytest.mark.parametrize("text", [
    '{"severity": "low"} and then a paragraph of commentary about the alert',
    '{"a": 1}{"a": 2}',
    '[1, 2, 3]\n\nHere is my reasoning about the above.',
    '{"a":1},' * 40,
])
def test_a_completed_document_followed_by_more_output_is_extra_data(text):
    """§8 — the discriminator. A whole document parsed, and output continued."""
    _v, _p, _c, diagnosis = structured_output_diagnosis(text)
    assert diagnosis is not None
    assert diagnosis.kind is JsonParseErrorKind.EXTRA_DATA


@pytest.mark.parametrize(("text", "kind"), [
    ('{"severity" "low"}', JsonParseErrorKind.EXPECTING_DELIMITER),
    ('{"a": 1 "b": 2}', JsonParseErrorKind.EXPECTING_DELIMITER),
    ('{severity: "low"}', JsonParseErrorKind.EXPECTING_PROPERTY_NAME),
    ('{"a": 1, 2: 3}', JsonParseErrorKind.EXPECTING_PROPERTY_NAME),
    ('not json at all, just a sentence', JsonParseErrorKind.EXPECTING_VALUE),
    ('{"a": }', JsonParseErrorKind.EXPECTING_VALUE),
    ('{"a": "back\\qslash"}', JsonParseErrorKind.INVALID_ESCAPE),
    ('{"a": "\\uZZZZ"}', JsonParseErrorKind.INVALID_ESCAPE),
    ('{"a": "line\nbreak"}', JsonParseErrorKind.INVALID_CONTROL_CHARACTER),
])
def test_each_malformation_lands_on_its_canonical_class(text, kind):
    """§6, §15 — the vocabulary is derived from what this parser actually raises."""
    _v, _p, _c, diagnosis = structured_output_diagnosis(text)
    assert diagnosis is not None
    assert diagnosis.kind is kind


def test_the_location_is_deterministic_and_correct():
    """§7, §15 — 1-based line, 1-based column, 0-based offset, checked against Python."""
    for text in ('{"a": 1} tail', '{"a": 1', '{\n  "a": 1,\n  "b" 2\n}',
                 '["x",\n "y"\n "z"]'):
        _v, _p, _c, diagnosis = structured_output_diagnosis(text)
        assert diagnosis is not None
        with pytest.raises(json.JSONDecodeError) as caught:
            json.loads(text)
        assert (diagnosis.line, diagnosis.column, diagnosis.position) == \
            (caught.value.lineno, caught.value.colno, caught.value.pos)
        assert structured_output_diagnosis(text)[3] == diagnosis  # deterministic


def test_a_multiline_response_reports_the_line_it_failed_on():
    """A location that always said line 1 would measure nothing."""
    _v, _p, _c, diagnosis = structured_output_diagnosis(
        '{\n  "first": 1,\n  "second": 2,\n  "third" 3\n}')
    assert diagnosis is not None
    assert diagnosis.line == 4
    assert diagnosis.column > 1
    assert diagnosis.position > 0


# ══════════════════════════════════════════════════════════════════════════════
#  §6 — the vocabulary is closed, derived, and fails safe
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unknown_parser_message_falls_back_without_storing_it():
    """§6 — unknown messages map fail-safely and never carry body text."""
    canary = "SYNTHETIC-CANARY-PARSER-MESSAGE-4b71"
    assert classify_json_parse_error(f"Some future CPython message {canary}") is \
        JsonParseErrorKind.OTHER_JSON_PARSE_ERROR
    assert classify_json_parse_error("") is JsonParseErrorKind.OTHER_JSON_PARSE_ERROR
    kind = classify_json_parse_error(f"Some future CPython message {canary}")
    assert canary not in kind.value


def test_the_message_variants_that_quote_the_response_collapse_to_one_class():
    """§6 — CPython's PURE-PYTHON decoder formats the offending character INTO msg.

    ``"Invalid control character {0!r} at"`` and ``"Invalid \\escape: {0!r}"`` embed one
    character of the response; the C accelerator omits it. Whether a build quotes the
    body is a runtime property, which is exactly why the message is classified and
    dropped rather than persisted.
    """
    assert classify_json_parse_error("Invalid control character '\\n' at") is \
        JsonParseErrorKind.INVALID_CONTROL_CHARACTER
    assert classify_json_parse_error("Invalid control character at") is \
        JsonParseErrorKind.INVALID_CONTROL_CHARACTER
    assert classify_json_parse_error("Invalid \\escape: 'q'") is \
        JsonParseErrorKind.INVALID_ESCAPE
    assert classify_json_parse_error("Invalid \\escape") is \
        JsonParseErrorKind.INVALID_ESCAPE


def test_every_class_this_parser_can_raise_is_in_the_vocabulary():
    """§6 — derived from the parser, not guessed. OTHER is a fallback, not a dumping
    ground for inputs this repository routinely produces."""
    routine = ('{"a": 1} x', '{"a": "u', '{"a": 1', 'prose', '{a: 1}', '{"a": 1,}',
               '[1,]', '{"a": "\\q"}', '{"a": "\\uZZZZ"}', '{"a": "l\nw"}', '{"a" 1}')
    kinds = set()
    for text in routine:
        _v, _p, _c, diagnosis = structured_output_diagnosis(text)
        assert diagnosis is not None
        assert diagnosis.kind is not JsonParseErrorKind.OTHER_JSON_PARSE_ERROR, text
        kinds.add(diagnosis.kind)
    assert len(kinds) >= 7, "a vocabulary this coarse would not discriminate anything"


def test_the_persisted_kind_is_always_a_member_of_the_closed_vocabulary():
    values = {k.value for k in JsonParseErrorKind}
    for text in ('{"a": 1}', '{"a": 1', '{"a": 1} x', '', '<think>thinking',
                 'prose', '{"a": "\\q"}'):
        kind = _record(text)["json_parse_error_kind"]
        assert kind is None or kind in values


# ══════════════════════════════════════════════════════════════════════════════
#  §8 — one parser, one parse
# ══════════════════════════════════════════════════════════════════════════════
def test_the_diagnosis_comes_from_the_existing_parse_attempt():
    """§8 — no second JSON parser, and no second call to the first one."""
    source = _code_of(structured_output_diagnosis)
    assert source.count("json.loads") == 1
    assert "JSONDecodeError" in source
    assert "JsonParseDiagnosis.from_error(exc)" in source
    # The two older entry points project from this one rather than re-deciding.
    for projection in (structured_output, structured_output_detail):
        assert "structured_output_diagnosis" in inspect.getsource(projection)


def test_the_three_entry_points_can_never_disagree():
    for text in ('{"a": 1}', '{"a": 1', '{"a": 1} x', '', '<think>x'):
        value2, problem2 = structured_output(text)
        value3, problem3, code3 = structured_output_detail(text)
        value4, problem4, code4, _d = structured_output_diagnosis(text)
        assert (value2, problem2) == (value4, problem4)
        assert (value3, problem3, code3) == (value4, problem4, code4)


# ══════════════════════════════════════════════════════════════════════════════
#  §9, §17 — the generic runaway diagnostic
# ══════════════════════════════════════════════════════════════════════════════
def test_a_repeated_emission_scores_far_below_a_novel_continuation():
    """§17 — non-vacuity. Two orders of magnitude apart, with no threshold defined."""
    repeated = "the alert fired on host seven at midnight. " * 200
    novel = " ".join(f"observation {i} names sensor {i * 7 % 97} at offset {i * 3}"
                     for i in range(200))
    repeated_ratio = unique_char_ngram_ratio(repeated)
    novel_ratio = unique_char_ngram_ratio(novel)
    assert repeated_ratio is not None and novel_ratio is not None
    assert repeated_ratio < 0.05
    assert novel_ratio > 0.5
    assert novel_ratio > repeated_ratio * 20


def test_it_catches_the_degenerate_shapes_a_word_tokeniser_would_miss():
    """§9 — this evaluation's runaways are often whitespace-free."""
    one_token = "a" * 4000
    structured_loop = '{"a":1},' * 500
    for text in (one_token, structured_loop):
        ratio = unique_char_ngram_ratio(text)
        assert ratio is not None and ratio < 0.01, text[:20]
    assert unique_char_ngram_ratio(
        " ".join(f"unique-fragment-{i}" for i in range(400))) > 0.5


def test_the_ratio_is_deterministic_across_calls():
    """§9 — deterministic, and not built on per-process hash randomisation."""
    text = "repeating segment " * 90 + "then something new entirely at the very end"
    assert len({unique_char_ngram_ratio(text) for _ in range(8)}) == 1
    assert "hash(" not in _code_of(unique_char_ngram_ratio)


def test_the_ratio_loads_no_model_and_no_tokeniser():
    """§9 — constraint 4. It reads a string and counts substrings."""
    source = _code_of(unique_char_ngram_ratio)
    for forbidden in ("tokenizer", "tokeniser", "AutoTokenizer", "torch", "transformers",
                      "encode(", "from_pretrained"):
        assert forbidden not in source


def test_a_response_too_short_to_measure_is_unmeasured_not_zero():
    """A canonical null, never an optimistic or pessimistic number."""
    assert unique_char_ngram_ratio("") is None
    assert unique_char_ngram_ratio("short") is None
    assert unique_char_ngram_ratio("x" * (RESPONSE_NGRAM_WINDOW - 1)) is None
    assert unique_char_ngram_ratio("x" * RESPONSE_NGRAM_WINDOW) == 1.0
    assert _record("")["response_unique_char_ngram_ratio"] is None


def test_the_ratio_is_measured_over_the_raw_response_including_reasoning():
    """A runaway that loops inside ``<think>`` is still a runaway."""
    looping_reasoning = "<think>" + ("I should check the host again. " * 120) + "</think>"
    assert _record(looping_reasoning)["response_unique_char_ngram_ratio"] < 0.05


def test_no_threshold_is_defined_anywhere_in_the_diagnostic():
    """§17 — classifying a ratio as 'repetition' belongs to later analysis."""
    for function in (unique_char_ngram_ratio,):
        source = _code_of(function)
        for word in ("threshold", "REPETITION_LIMIT", "is_repetitive", "degenerate_if"):
            assert word not in source
    assert "response_unique_char_ngram_ratio" not in inspect.getsource(gates_module)


# ══════════════════════════════════════════════════════════════════════════════
#  §10, §16 — privacy non-vacuity
# ══════════════════════════════════════════════════════════════════════════════
_CANARY = "ZORPHAXQ-SYNTHETIC-CANARY-8811"
_CANARY_RESPONSE = (
    f'{{"summary": "the {_CANARY} beacon contacted a controller", '
    f'"host": "{_CANARY}-workstation", "note": "unterminated from here on'
)


def test_the_record_contains_no_response_text_at_all():
    """§16 — 0 raw response string, 0 unique synthetic canary."""
    record = _record(_CANARY_RESPONSE)
    blob = json.dumps(record)
    assert _CANARY not in blob
    assert "beacon" not in blob
    assert "workstation" not in blob
    # And the diagnosis really did fire, so this is not vacuously clean.
    assert record["json_parse_error_kind"] == "unterminated_string"


def test_the_record_contains_no_eight_word_shingle_of_the_response():
    """§16 — 0 eight-word response shingles."""
    words = _CANARY_RESPONSE.split()
    blob = json.dumps(_record(_CANARY_RESPONSE))
    shingles = [" ".join(words[i:i + 8]) for i in range(max(1, len(words) - 7))]
    assert shingles, "the fixture must be long enough to shingle"
    for shingle in shingles:
        assert shingle not in blob


def test_the_record_contains_no_parser_source_snippet_or_exception_prose():
    """§16 — 0 parser source snippets; the message never reaches the artefact."""
    record = _record(_CANARY_RESPONSE)
    blob = json.dumps(record)
    for fragment in ("Unterminated string starting at", "Expecting", "Invalid",
                     "Extra data", "JSONDecodeError", "line 1", "char "):
        assert fragment not in blob


def test_the_record_carries_no_token_ids():
    """§16 — 0 token IDs. Every added field is a class name or a small integer."""
    record = _record(_CANARY_RESPONSE)
    for field in _NEW_FIELDS:
        value = record[field]
        assert value is None or isinstance(value, (str, int, float))
        assert not isinstance(value, (list, tuple, dict))


def test_the_new_fields_pass_the_body_free_scanner():
    """§16 — the production private-content scanner, over the whole record."""
    from training_gym.schemas import assert_no_private_content
    leaky = ("<think>the key is under /home/analyst/.ssh/id_rsa</think>\n"
             '{"severity": "low"} and then more output')
    for text in (_CANARY_RESPONSE, leaky, "a" * 4000, '{"a":1},' * 500):
        record = _record(text)
        assert_no_private_content(record, label="s3t0 score evidence")


def test_the_location_offsets_cannot_exceed_what_is_already_published():
    """§10 — an offset discloses strictly less than ``response_chars``, which the
    results artefact has always written."""
    for text in ('{"a": 1} tail', '{"alpha": "beta', '{"a" 1}'):
        score, result = _scored(text)
        assert score.json_parse_error_position <= result.to_dict()["response_chars"]


# ══════════════════════════════════════════════════════════════════════════════
#  §11 — diagnostic only
# ══════════════════════════════════════════════════════════════════════════════
def test_no_gate_reads_any_new_field():
    """§11 — ``OBSERVABILITY_IS_GATE: NO``."""
    source = inspect.getsource(gates_module)
    for name in (*_NEW_FIELDS, "JsonParseErrorKind", "json_parse_error",
                 "unique_char_ngram_ratio"):
        assert name not in source, f"a gate now reads {name}; S3T.0 designs no gate"


def test_no_metric_reads_any_new_field():
    """§11 — ``SCORING_CHANGED: NO`` at the aggregate layer too."""
    source = inspect.getsource(metrics_module)
    for name in (*_NEW_FIELDS, "json_parse_error", "unique_char_ngram_ratio"):
        assert name not in source, f"a metric now reads {name}; these are diagnostics"


def test_the_verdict_does_not_depend_on_the_diagnostics():
    """§11 — the same score with and without a diagnosis decides the same thing."""
    from dataclasses import replace
    base = ArmScore(
        task_id="t", task_hash="a" * 64, role="baseline", family="soc_triage",
        split="hidden_evaluation", status=ResultStatus.PASS, reward=1.0,
        refusal=RefusalClass.SAFE_COMPLETION)
    diagnosed = replace(
        base, json_parse_error_kind=JsonParseErrorKind.EXTRA_DATA,
        json_parse_error_line=1, json_parse_error_column=9, json_parse_error_position=8,
        response_unique_char_ngram_ratio=0.004)
    assert (base.passed, base.measured, base.status, base.reward, base.blocking) == \
        (diagnosed.passed, diagnosed.measured, diagnosed.status, diagnosed.reward,
         diagnosed.blocking)


def test_a_parse_failure_still_fails_the_structural_grader_the_same_way():
    """§11 — ``ELIGIBILITY_CHANGED: NO``: the classes are new, the verdict is not."""
    for text in ('{"a": 1', '{"a": 1} tail', '{"a": "u', 'prose'):
        record = _record(text)
        assert record["json_parseable"] is False
        assert record["schema_valid"] is False
        assert record["grader_statuses"]["json_schema"] == "fail"
        assert record["note_codes"].count("structured_output_not_valid_json") <= 1


def test_extra_data_is_not_forgiven_now_that_it_is_named():
    """Naming a failure is not excusing it: EXTRA_DATA is still not a parsed document."""
    record = _record('{"severity": "low"} and then commentary')
    assert record["json_parse_error_kind"] == "extra_data"
    assert record["json_parseable"] is False
    assert record["status"] != ResultStatus.PASS.value or record["blocking"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  §14 — policy identities
# ══════════════════════════════════════════════════════════════════════════════
def test_every_policy_identity_is_unchanged():
    """§14 — generation, grader, metric, gate, statistical, family and resource."""
    assert GenerationPolicy().policy_hash() == _GENERATION_POLICY_HASH
    assert GraderPolicy().policy_hash() == _GRADER_POLICY_HASH
    assert MetricPolicy().policy_hash() == _METRIC_POLICY_HASH
    assert GatePolicy().policy_hash() == _GATE_POLICY_HASH
    assert StatisticalPolicy().policy_hash() == _STATISTICAL_POLICY_HASH
    assert TaskFamilyPolicy().policy_hash() == _FAMILY_POLICY_HASH
    assert ResourceCeilings().policy_hash() == _RESOURCE_POLICY_HASH
    assert EvaluationPolicySet().policy_hash() == _POLICY_SET_HASH


def test_no_policy_binds_the_observability_schema():
    """§14 — an artefact's shape is not a policy, so a policy hash must not move with
    it. If this ever stops holding, the identity must move and be reported, not pinned."""
    for module_policy in (GraderPolicy, MetricPolicy, GatePolicy):
        source = inspect.getsource(module_policy)
        assert "SCORE_EVIDENCE_VERSION" not in source
        assert "SCORING_VERSION" not in source


# ══════════════════════════════════════════════════════════════════════════════
#  §13 — versioning and §12 — historical immutability
# ══════════════════════════════════════════════════════════════════════════════
def test_the_evidence_schema_version_moved_and_names_the_new_fields():
    """§13 — the artefact that OWNS the diagnostics is the one that is bumped."""
    assert SCORE_EVIDENCE_VERSION == "m62.evaluation_score_evidence.3"
    assert _record('{"a": 1}')["evidence_version"] == SCORE_EVIDENCE_VERSION
    assert set(_record('{"a": 1}')) == set(SCORE_EVIDENCE_FIELDS)
    for field in _NEW_FIELDS:
        assert field in SCORE_EVIDENCE_FIELDS


def test_the_scoring_version_moved_because_the_score_dict_changed():
    """§13 — ``score_hash`` covers the diagnostics, so the scoring identity moves too."""
    from training_gym.evaluation.scoring import SCORING_VERSION
    assert SCORING_VERSION == "m62.evaluation_scoring.6"
    score, _result = _scored('{"a": 1} tail')
    payload = score.to_dict()
    for field in _NEW_FIELDS:
        assert field in payload


def test_a_historical_record_written_before_s3t0_still_reads():
    """§12, §13 — the field list is an ALLOWLIST, so an older record verifies unchanged
    and is NOT backfilled with a diagnosis nobody computed."""
    current = _record('{"a": 1} tail')
    legacy_2 = {k: v for k, v in current.items() if k not in _NEW_FIELDS}
    legacy_2["evidence_version"] = "m62.evaluation_score_evidence.2"
    read = read_score_evidence(legacy_2, label="legacy[0]")
    assert read == legacy_2
    for field in _NEW_FIELDS:
        assert field not in read, "a historical record must gain no invented value"
    legacy_1 = {k: v for k, v in legacy_2.items() if k != "output_budget_exhausted"}
    legacy_1["evidence_version"] = "m62.evaluation_score_evidence.1"
    assert read_score_evidence(legacy_1, label="legacy[1]") == legacy_1


def test_an_undeclared_field_is_still_refused_on_read():
    """The allowlist is still an allowlist; it was widened, not opened."""
    with pytest.raises(SchemaError):
        read_score_evidence({**_record('{"a": 1}'), "response_excerpt": "text"},
                            label="record")


# ══════════════════════════════════════════════════════════════════════════════
#  §18 — behavioural equivalence on the preexisting fields
# ══════════════════════════════════════════════════════════════════════════════
_EQUIVALENCE_FIXTURES = (
    '{"severity": "low"}', '["severity", "medium"]', "no json here at all",
    '{"severity": "low"', '{"severity": "lo',
    '{"severity": "low"} and then some trailing commentary',
    '{"severity" "low"}', '{severity: "low"}', "", "<think>still thinking",
    '<think>reasoning</think>\n{"severity": "low"}', '```json\n{"a": 1}\n```',
    "I cannot help with that request.", "the alert fired again. " * 200,
)


@pytest.mark.parametrize("text", _EQUIVALENCE_FIXTURES)
def test_only_the_new_fields_appear_and_nothing_else_moved(text):
    """§18 — every preexisting field keeps its pre-S3T.0 shape and value semantics."""
    record = _record(text)
    assert set(record) - set(_PRE_S3T0_FIELDS) == set(_NEW_FIELDS)
    score, result = _scored(text)
    # Each preexisting persisted field is still exactly the ArmScore value it always was.
    assert record["status"] == score.status.value
    assert record["reward"] == score.reward
    assert record["refusal"] == score.refusal.value
    assert record["json_parseable"] == score.json_parseable
    assert record["schema_valid"] == score.schema_valid
    assert record["blocking"] is bool(score.blocking)
    assert record["severity"] == score.severity.value
    assert record["grader_statuses"] == dict(sorted(score.grader_statuses.items()))
    assert record["note_codes"] == list(score.note_codes)
    assert record["security_findings"] == list(score.security_findings)
    assert record["hygiene_findings"] == list(score.hygiene_findings)
    assert record["output_budget_exhausted"] == score.output_budget_exhausted
    assert record["truncated"] is bool(score.truncated)
    # And the RESULT artefact — the generation-side record — is untouched by S3T.0.
    assert set(result.to_record()) == {
        "protocol_version", "backend_id", "backend_version", "role", "task_id",
        "task_hash", "status", "response_sha256", "response_chars",
        "proposed_tool_calls", "input_tokens", "output_tokens", "input_truncated",
        "truncated_tokens", "latency_ms", "peak_memory_category", "finish_reason",
        "timed_out", "interrupted", "error_category", "error_message",
        "cleanup_status", "warnings", "request_parity_hash", "result_hash"}


def test_the_record_is_still_deterministic():
    assert _record('{"a": 1} tail') == _record('{"a": 1} tail')


# ══════════════════════════════════════════════════════════════════════════════
#  Internal consistency of the new state
# ══════════════════════════════════════════════════════════════════════════════
def test_a_half_present_parse_diagnosis_is_refused():
    """A class with no location describes a parse attempt that did not happen."""
    common = dict(task_id="t", task_hash="a" * 64, role="baseline", family="soc_triage",
                  split="hidden_evaluation", status=ResultStatus.PASS, reward=1.0,
                  refusal=RefusalClass.SAFE_COMPLETION)
    with pytest.raises(ScoringError, match="half"):
        ArmScore(**common, json_parse_error_kind=JsonParseErrorKind.EXTRA_DATA)
    with pytest.raises(ScoringError, match="half"):
        ArmScore(**common, json_parse_error_line=1, json_parse_error_column=1,
                 json_parse_error_position=0)


def test_an_impossible_ratio_is_refused():
    common = dict(task_id="t", task_hash="a" * 64, role="baseline", family="soc_triage",
                  split="hidden_evaluation", status=ResultStatus.PASS, reward=1.0,
                  refusal=RefusalClass.SAFE_COMPLETION)
    for bad in (0.0, -0.5, 1.5, True, "0.5"):
        with pytest.raises(ScoringError, match="response_unique_char_ngram_ratio"):
            ArmScore(**common, response_unique_char_ngram_ratio=bad)
    assert ArmScore(**common,
                    response_unique_char_ngram_ratio=1).response_unique_char_ngram_ratio \
        == 1.0


def test_an_invented_parse_class_is_refused():
    with pytest.raises(ValueError):
        ArmScore(task_id="t", task_hash="a" * 64, role="baseline", family="soc_triage",
                 split="hidden_evaluation", status=ResultStatus.PASS, reward=1.0,
                 refusal=RefusalClass.SAFE_COMPLETION,
                 json_parse_error_kind="whatever_the_model_said",
                 json_parse_error_line=1, json_parse_error_column=1,
                 json_parse_error_position=0)


def test_the_diagnosis_dataclass_is_frozen_and_body_free():
    diagnosis = JsonParseDiagnosis(kind=JsonParseErrorKind.EXTRA_DATA, line=1, column=9,
                                   position=8)
    with pytest.raises(Exception):
        diagnosis.line = 2  # type: ignore[misc]
    assert set(vars(diagnosis)) == {"kind", "line", "column", "position"}
