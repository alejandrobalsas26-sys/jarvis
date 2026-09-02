"""V69 M62 S4H — Tool Call V2: the validator that is actually handed a call.

WHAT THESE TESTS ARE FOR
------------------------
D28's shape, restated so it can be pinned: the production backend never populated
``proposed_tool_calls``, ``review_tool_calls`` returned ``valid=True`` for an empty
sequence, and the ``tool_call_schema`` grader therefore passed on all six of eval-v7's
tool-call tasks without inspecting anything.

Two of these tests exist specifically to stop that returning:

  * ``test_a_required_call_that_never_arrived_is_a_failure`` — absence is a result.
  * ``test_validate_response_refuses_to_guess_whether_a_call_was_required`` — the
    parameter has no default, so a caller cannot get a verdict without saying.

The rest are the schema checks the historical path did not reach, each with its own
reason code so a report says WHICH contract was broken.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_gym.evaluation.instruments import tool_call_v2 as T  # noqa: E402


@pytest.fixture
def catalogue():
    return T.build_catalogue([
        T.ToolSchema(name="heartbeat", properties={}, required=()),
        T.ToolSchema(name="lookup_host",
                     properties={"host": {"type": "string"}}, required=("host",)),
        T.ToolSchema(
            name="scan_host",
            properties={
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "mode": {"type": "string", "enum": ["quick", "deep"]},
                "options": {"type": "object",
                            "properties": {"timeout_s": {"type": "integer"},
                                           "verbose": {"type": "boolean"}},
                            "required": ("timeout_s",)},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("host", "port")),
    ])


def check(text, catalogue, *, required=True):
    return T.validate_response(text, catalogue=catalogue, calls_required=required)


# ══════════════════════════════════════════════════════════════════════════════
#  §32 VALID CASES
# ══════════════════════════════════════════════════════════════════════════════
VALID = [
    ("no_arg_tool", '{"name": "heartbeat", "arguments": {}}'),
    ("one_required_string", '{"name": "lookup_host", "arguments": {"host": "h1.lab"}}'),
    ("multiple_typed_args",
     '{"name": "scan_host", "arguments": {"host": "h1.lab", "port": 22}}'),
    ("enum_member",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, "mode": "deep"}}'),
    ("nested_object",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, '
     '"options": {"timeout_s": 5, "verbose": true}}}'),
    ("array_of_strings",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, '
     '"tags": ["a", "b"]}}'),
    ("fenced_block",
     '```json\n{"name": "lookup_host", "arguments": {"host": "h1.lab"}}\n```'),
    ("call_after_prose",
     'I will look the host up.\n{"name": "lookup_host", "arguments": {"host": "h"}}'),
]


@pytest.mark.parametrize("label,text", VALID, ids=[c[0] for c in VALID])
def test_a_well_formed_call_validates(label, text, catalogue):
    result = check(text, catalogue)
    assert result.valid, f"{label}: {result.reason_codes}"
    assert result.call_count == 1


def test_key_ordering_inside_the_envelope_does_not_matter(catalogue):
    forward = '{"name": "lookup_host", "arguments": {"host": "h"}}'
    reversed_ = '{"arguments": {"host": "h"}, "name": "lookup_host"}'
    assert check(forward, catalogue).valid and check(reversed_, catalogue).valid


def test_argument_ordering_does_not_matter(catalogue):
    one = '{"name": "scan_host", "arguments": {"host": "h", "port": 1}}'
    other = '{"name": "scan_host", "arguments": {"port": 1, "host": "h"}}'
    assert check(one, catalogue).valid and check(other, catalogue).valid


# ══════════════════════════════════════════════════════════════════════════════
#  §33 INVALID CASES — each with its own reason code
# ══════════════════════════════════════════════════════════════════════════════
INVALID = [
    ("nonexistent_tool", '{"name": "rm_rf", "arguments": {}}', "unknown_tool"),
    ("malformed_envelope", '{"arguments": {"host": "h"}}', "envelope_missing_field"),
    ("invalid_json", '{"name": "lookup_host", "arguments": {', "invalid_json"),
    ("missing_required_argument", '{"name": "lookup_host", "arguments": {}}',
     "missing_required_argument"),
    ("wrong_scalar_type",
     '{"name": "scan_host", "arguments": {"host": "h", "port": "22"}}',
     "wrong_scalar_type"),
    ("wrong_enum",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, "mode": "sideways"}}',
     "enum_violation"),
    ("invalid_nested_type",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, '
     '"options": {"timeout_s": "soon"}}}', "nested_type_violation"),
    ("extra_forbidden_arg",
     '{"name": "lookup_host", "arguments": {"host": "h", "sudo": true}}',
     "unknown_argument"),
    ("array_element_wrong_type",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, "tags": [1, 2]}}',
     "array_element_type_violation"),
    ("prose_masquerading_as_a_call", "I would call scan_host on h1.lab with port 22.",
     "prose_not_a_tool_call"),
    ("arguments_not_an_object", '{"name": "lookup_host", "arguments": "h"}',
     "arguments_not_object"),
    ("nested_missing_required",
     '{"name": "scan_host", "arguments": {"host": "h", "port": 1, "options": {}}}',
     "missing_required_argument"),
    ("boolean_where_an_integer_belongs",
     '{"name": "scan_host", "arguments": {"host": "h", "port": true}}',
     "wrong_scalar_type"),
    ("array_where_a_scalar_belongs",
     '{"name": "lookup_host", "arguments": {"host": ["h"]}}', "wrong_scalar_type"),
]


@pytest.mark.parametrize("label,text,code", INVALID, ids=[c[0] for c in INVALID])
def test_a_malformed_call_is_rejected_with_a_precise_reason(label, text, code,
                                                            catalogue):
    result = check(text, catalogue)
    assert not result.valid, f"{label}: accepted"
    assert code in result.reason_codes, (
        f"{label}: expected {code}, got {result.reason_codes}")


def test_multiple_calls_where_one_is_allowed_are_rejected(catalogue):
    text = ('[{"name": "heartbeat", "arguments": {}}, '
            '{"name": "heartbeat", "arguments": {}}]')
    result = check(text, catalogue)
    assert not result.valid
    assert "too_many_calls" in result.reason_codes
    assert result.call_count == 2


def test_a_reason_names_a_path_and_never_an_argument_value(catalogue):
    """A validation report gets pasted into tickets; a value must not travel with it."""
    secret = "sk-s4hsynthetic-never-in-a-report"
    text = json.dumps({"name": "lookup_host", "arguments": {"host": 1,
                                                            "token": secret}})
    result = check(text, catalogue)
    payload = json.dumps(result.to_dict())
    assert not result.valid
    assert secret not in payload and secret not in repr(result)
    assert "call[0].arguments.host" in payload


# ══════════════════════════════════════════════════════════════════════════════
#  D28 — absence is a result
# ══════════════════════════════════════════════════════════════════════════════
def test_a_required_call_that_never_arrived_is_a_failure(catalogue):
    """The exact historical shape: a text-only response on a tool-call task."""
    result = check("I have completed the lookup for you.", catalogue, required=True)
    assert not result.valid
    assert result.call_count == 0
    assert result.reason_codes


def test_an_empty_response_on_a_required_call_task_is_a_failure(catalogue):
    result = check("", catalogue, required=True)
    assert not result.valid
    assert "no_tool_call_emitted" in result.reason_codes


def test_no_call_where_none_was_required_is_the_only_shape_of_empty_pass(catalogue):
    result = check("", catalogue, required=False)
    assert result.valid and result.call_count == 0


def test_validate_response_refuses_to_guess_whether_a_call_was_required(catalogue):
    """No default. A caller that does not know cannot obtain a verdict."""
    with pytest.raises(TypeError):
        T.validate_response("{}", catalogue=catalogue)
    with pytest.raises(T.ToolCallError, match="explicitly"):
        T.validate_response("{}", catalogue=catalogue, calls_required=None)


def test_valid_json_is_not_by_itself_a_valid_tool_call(catalogue):
    """§31: the two must never be conflated."""
    result = check('{"summary": "the host is clean", "confidence": 0.9}', catalogue)
    assert not result.valid
    assert "unknown_tool" in result.reason_codes
    assert "invalid_json" not in result.reason_codes


# ══════════════════════════════════════════════════════════════════════════════
#  §34 MUTATIONS
# ══════════════════════════════════════════════════════════════════════════════
def _invalid_texts():
    return [text for _, text, _ in INVALID]


def test_mutation_validator_always_returns_true_is_caught(monkeypatch, catalogue):
    monkeypatch.setattr(T, "validate_call", lambda *a, **k: ())
    survivors = [t for t in _invalid_texts() if check(t, catalogue).valid]
    assert survivors, "an always-true validator must let a malformed call through"


def test_mutation_type_validation_disabled_is_caught(monkeypatch, catalogue):
    monkeypatch.setattr(T, "_JSON_TYPES",
                        {k: (object,) for k in T._JSON_TYPES})
    text = '{"name": "scan_host", "arguments": {"host": "h", "port": "22"}}'
    assert check(text, catalogue).valid


def test_mutation_required_arguments_ignored_is_caught(monkeypatch, catalogue):
    original = T._validate_object
    monkeypatch.setattr(
        T, "_validate_object",
        lambda value, properties, required, additional, *, path, nested=False:
            original(value, properties, (), additional, path=path, nested=nested))
    assert check('{"name": "lookup_host", "arguments": {}}', catalogue).valid


def test_mutation_enum_checking_disabled_is_caught(monkeypatch, catalogue):
    original = T._validate_value
    monkeypatch.setattr(
        T, "_validate_value",
        lambda value, spec, *, path, nested=False: original(
            value, {k: v for k, v in spec.items() if k != "enum"},
            path=path, nested=nested))
    text = ('{"name": "scan_host", "arguments": '
            '{"host": "h", "port": 1, "mode": "sideways"}}')
    assert check(text, catalogue).valid


def test_mutation_unknown_tools_accepted_is_caught(catalogue):
    permissive = T.build_catalogue([
        *catalogue.values(),
        T.ToolSchema(name="rm_rf", properties={}, required=(),
                     additional_properties=True)])
    assert check('{"name": "rm_rf", "arguments": {}}', permissive).valid


def test_mutation_malformed_json_accepted_is_caught(monkeypatch, catalogue):
    monkeypatch.setattr(
        T, "extract_calls",
        lambda text: (({"name": "heartbeat", "arguments": {}},), ()))
    assert check('{"name": "lookup_host", "arguments": {', catalogue).valid


def test_mutation_absence_treated_as_valid_reproduces_d28_and_is_caught(catalogue):
    """``absence_is_a_failure`` is load-bearing, and its default is the safe one.

    Turning it off IS D28: a task that demanded a call, a response that emitted none,
    and a PASS. The setting exists so that behaviour has to be chosen by name in a
    config rather than arriving as the default of a function argument, and this test
    pins both directions — the default refuses, the mutation does not.
    """
    prose = "I ran the lookup and the host is clean."
    strict = T.validate_response(prose, catalogue=catalogue, calls_required=True)
    assert not strict.valid, "the default policy must refuse a missing required call"

    lax = T.ToolCallPolicy(absence_is_a_failure=False)
    mutated = T.validate_response(prose, catalogue=catalogue, calls_required=True,
                                  policy=lax)
    assert mutated.valid, (
        "the mutation must actually change behaviour, or the setting is decorative")
    assert strict.valid != mutated.valid


def test_mutation_additional_properties_allowed_is_caught(catalogue):
    permissive = T.build_catalogue([
        T.ToolSchema(name="lookup_host", properties={"host": {"type": "string"}},
                     required=("host",), additional_properties=True)])
    text = '{"name": "lookup_host", "arguments": {"host": "h", "sudo": true}}'
    assert check(text, permissive).valid


# ══════════════════════════════════════════════════════════════════════════════
#  Catalogue and shape
# ══════════════════════════════════════════════════════════════════════════════
def test_a_schema_with_an_untyped_property_is_refused():
    with pytest.raises(T.ToolCallError, match="declares no type"):
        T.ToolSchema(name="t", properties={"x": {}}, required=())


def test_a_schema_requiring_an_undeclared_argument_is_refused():
    with pytest.raises(T.ToolCallError, match="never be satisfied"):
        T.ToolSchema(name="t", properties={}, required=("x",))


def test_a_catalogue_with_two_tools_of_one_name_is_refused():
    with pytest.raises(T.ToolCallError, match="declared twice"):
        T.build_catalogue([T.ToolSchema(name="t"), T.ToolSchema(name="t")])


def test_validation_serializes_deterministically(catalogue):
    text = '{"name": "scan_host", "arguments": {"host": "h", "port": "22"}}'
    assert (check(text, catalogue).canonical_bytes()
            == check(text, catalogue).canonical_bytes())


def test_the_validator_version_is_pinned(catalogue):
    assert T.TOOL_CALL_VALIDATOR_VERSION == "m62.tool_call_validator.2"
    assert check("{}", catalogue).validator_version == T.TOOL_CALL_VALIDATOR_VERSION
