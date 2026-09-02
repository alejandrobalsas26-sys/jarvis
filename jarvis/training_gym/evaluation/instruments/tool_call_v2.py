"""training_gym/evaluation/instruments/tool_call_v2.py — V69 M62 S4H.

D28, STATED EXACTLY
-------------------
``scoring.review_tool_calls`` opens with::

    if not calls:
        return ToolCallReview(valid=True)

and ``scoring.score_arm`` feeds it ``result.proposed_tool_calls``. The production backend
``backends/transformers_peft.py`` constructs its ``EvaluationResult`` without ever
setting that field, because nothing between the tokenizer's output text and the score
extracts a tool call from a response. The field therefore defaults to ``()`` on every
live generation.

Compose the two and the ``tool_call_schema`` grader returns PASS for every task in every
live evaluation, whatever the model emitted. Six of eval-v7's thirty-six tasks are in
the ``tool_call_schema`` family. Six PASSes were recorded and nothing was checked.

That is the whole defect, and it is not a threshold that needs tuning: the validator was
never handed a call. Making the validator stricter would have changed nothing.

WHAT V2 CHANGES
---------------
Three things, in this order:

  1. **Extraction exists.** :func:`extract_calls` reads calls out of a response's TEXT,
     which is the only artefact a text-generation backend produces. Prose that is not a
     call is a NAMED reason code, not silence.
  2. **Absence is a result.** :func:`validate_response` takes ``calls_required`` and
     returns ``no_tool_call_emitted`` when a task that demanded a call got none. An
     empty list can no longer mean "valid".
  3. **The schema is actually applied.** Envelope, tool identity, argument presence,
     scalar types, enums, nested objects, arrays and additional-property policy are each
     checked and each have their own reason code.

Syntactically valid JSON is NOT a valid tool call, and the two are never conflated: a
document that parses but names an unknown tool fails with ``unknown_tool``, not with a
parse error.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...schemas import SchemaError, canonical_json, sha256_obj

#: Bumped when a reason code is added or a check's meaning changes.
TOOL_CALL_VALIDATOR_VERSION = "m62.tool_call_validator.2"


class ToolCallError(SchemaError):
    """A malformed catalogue or policy. Never a malformed CALL — that is a finding."""


class ReasonCode(str, Enum):
    """Why a call was rejected. Closed, body-safe, and one per distinct defect.

    Body-safe means: a code plus a path, never a value. ``wrong_scalar_type`` at
    ``arguments.host`` tells a reviewer what to fix without printing what the model put
    there — which matters because a model's tool arguments are exactly where a leaked
    credential shows up.
    """

    NO_TOOL_CALL_EMITTED = "no_tool_call_emitted"
    INVALID_JSON = "invalid_json"
    PROSE_NOT_A_TOOL_CALL = "prose_not_a_tool_call"
    ENVELOPE_NOT_OBJECT = "envelope_not_object"
    ENVELOPE_MISSING_FIELD = "envelope_missing_field"
    ENVELOPE_EXTRA_FIELD = "envelope_extra_field"
    UNKNOWN_TOOL = "unknown_tool"
    ARGUMENTS_NOT_OBJECT = "arguments_not_object"
    MISSING_REQUIRED_ARGUMENT = "missing_required_argument"
    UNKNOWN_ARGUMENT = "unknown_argument"
    WRONG_SCALAR_TYPE = "wrong_scalar_type"
    ENUM_VIOLATION = "enum_violation"
    NESTED_TYPE_VIOLATION = "nested_type_violation"
    ARRAY_ELEMENT_TYPE_VIOLATION = "array_element_type_violation"
    ARRAY_NOT_A_LIST = "array_not_a_list"
    TOO_MANY_CALLS = "too_many_calls"


#: The envelope every proposal must have. ``name`` identifies the tool; ``arguments``
#: is the object the schema is applied to. Anything else is a per-task convention and is
#: allowed by default, refused when the policy is strict.
REQUIRED_ENVELOPE_FIELDS: tuple = ("name", "arguments")

#: JSON type name -> the Python types that satisfy it. ``bool`` is excluded from the
#: numeric rows deliberately: in Python ``True`` is an ``int``, and a validator that
#: inherits that would accept ``true`` where an integer port number was required.
_JSON_TYPES: dict = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
    "null": (type(None),),
}


@dataclass(frozen=True)
class ToolSchema:
    """One tool's contract. A subset of JSON Schema, spelled out rather than delegated.

    Deliberately NOT a general JSON Schema engine: this repository has no vendored
    validator, and a partial one pretending to be general is worse than a small one that
    states its coverage. Coverage is: object properties, ``required``, scalar ``type``,
    ``enum``, nested ``object`` (recursively), ``array`` with a typed ``items``, and
    ``additionalProperties``.
    """

    name: str
    properties: dict = field(default_factory=dict)
    required: tuple = ()
    additional_properties: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ToolCallError("tool schema: name must be a non-empty identifier")
        if not isinstance(self.properties, Mapping):
            raise ToolCallError(f"tool schema {self.name!r}: properties must be a mapping")
        for key, spec in self.properties.items():
            if not isinstance(spec, Mapping) or "type" not in spec:
                raise ToolCallError(
                    f"tool schema {self.name!r}: property {key!r} declares no type; an "
                    f"untyped property is a check that silently never runs")
            if spec["type"] not in _JSON_TYPES:
                raise ToolCallError(
                    f"tool schema {self.name!r}: property {key!r} declares unknown type "
                    f"{spec['type']!r}")
        missing = [r for r in self.required if r not in self.properties]
        if missing:
            raise ToolCallError(
                f"tool schema {self.name!r}: required names {missing} that no property "
                f"declares; a required-but-undeclared argument can never be satisfied")

    def schema_hash(self) -> str:
        return sha256_obj({"additional_properties": self.additional_properties,
                           "name": self.name,
                           "properties": _sortable(self.properties),
                           "required": sorted(self.required)})


def _sortable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _sortable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_sortable(v) for v in value]
    return value


@dataclass(frozen=True)
class ToolCallPolicy:
    """How strict this evaluation is about proposals. Explicit, never inferred."""

    policy_version: str = "m62.tool_call_policy.1"
    max_calls: int = 1
    strict_envelope: bool = False
    #: Whether a task that requires a call fails when none is emitted. There is no
    #: reason to turn this off; it exists so the D28 behaviour is a NAMED setting that a
    #: config has to choose, rather than an accident of a default argument.
    absence_is_a_failure: bool = True

    def policy_hash(self) -> str:
        return sha256_obj({"absence_is_a_failure": self.absence_is_a_failure,
                           "max_calls": self.max_calls,
                           "policy_version": self.policy_version,
                           "strict_envelope": self.strict_envelope})


DEFAULT_TOOL_POLICY = ToolCallPolicy()


@dataclass(frozen=True)
class Violation:
    """One defect: a code and the path it sits at. No value, ever."""

    code: ReasonCode
    path: str = ""
    #: Closed, structural detail: an expected type name, an enum size, a field name.
    expected: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code.value, "expected": self.expected, "path": self.path}

    def __repr__(self) -> str:
        return f"Violation({self.code.value}@{self.path or '.'})"


@dataclass(frozen=True)
class ToolCallValidation:
    """The verdict on one response's proposals."""

    validator_version: str
    valid: bool
    call_count: int
    violations: tuple = ()
    policy_hash: str = ""

    @property
    def reason_codes(self) -> tuple:
        return tuple(sorted({v.code.value for v in self.violations}))

    def to_dict(self) -> dict:
        return {"call_count": self.call_count,
                "policy_hash": self.policy_hash,
                "reason_codes": list(self.reason_codes),
                "valid": self.valid,
                "validator_version": self.validator_version,
                "violations": sorted((v.to_dict() for v in self.violations),
                                     key=lambda d: (d["path"], d["code"],
                                                    d["expected"]))}

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    def __repr__(self) -> str:
        return (f"ToolCallValidation(valid={self.valid}, calls={self.call_count}, "
                f"codes={self.reason_codes})")


# ══════════════════════════════════════════════════════════════════════════════
#  Extraction — the step that did not exist
# ══════════════════════════════════════════════════════════════════════════════
_FENCED_RE = re.compile(r"```(?:json|tool_call|tool|jsonc)?\s*\n(.*?)```",
                        re.DOTALL | re.IGNORECASE)


def extract_calls(text: str) -> "tuple[tuple, tuple]":
    """Pull candidate calls out of a response's text.

    Returns ``(calls, violations)``. A response with no JSON at all yields
    ``PROSE_NOT_A_TOOL_CALL`` rather than an empty success: "the model wrote an essay"
    and "the model emitted a correct call" must never share a result.

    Three shapes are read, in order: a fenced block, a bare top-level object, a bare
    top-level array of objects. The FIRST shape that yields JSON wins, so a response
    that fences its call and then discusses it does not get parsed twice.
    """
    body = str(text or "").strip()
    if not body:
        return (), (Violation(ReasonCode.NO_TOOL_CALL_EMITTED),)

    candidates: list[str] = [m.group(1).strip() for m in _FENCED_RE.finditer(body)]
    if not candidates:
        span = _first_json_span(body)
        if span is None:
            return (), (Violation(ReasonCode.PROSE_NOT_A_TOOL_CALL),)
        if span is _UNBALANCED:
            return (), (Violation(ReasonCode.INVALID_JSON, path="call[0]"),)
        candidates = [str(span)]

    calls: list = []
    violations: list = []
    for index, blob in enumerate(candidates):
        try:
            parsed = json.loads(blob)
        except (ValueError, TypeError):
            violations.append(Violation(ReasonCode.INVALID_JSON, path=f"call[{index}]"))
            continue
        if isinstance(parsed, list):
            calls.extend(parsed)
        else:
            calls.append(parsed)
    if not calls and not violations:
        violations.append(Violation(ReasonCode.PROSE_NOT_A_TOOL_CALL))
    return tuple(calls), tuple(violations)


#: Sentinel: an opener was found and never closed. Distinct from "no JSON at all".
_UNBALANCED = object()


def _first_json_span(body: str) -> "str | object | None":
    """The first balanced ``{...}`` or ``[...]`` at the top level, or None.

    Bracket counting rather than a regex, because a regex cannot balance and a greedy
    one would swallow prose that happens to end in a brace. Quoted strings and their
    escapes are tracked so a brace inside a string value does not move the depth.
    """
    openers = sorted((body.find(o), o, c) for o, c in (("{", "}"), ("[", "]"))
                     if body.find(o) >= 0)
    for start, opener, closer in openers:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(body)):
            char = body[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return body[start:index + 1]
    # An opener that never closed IS JSON that failed to parse. Reporting it as prose
    # would tell a reviewer the model wrote an essay when it wrote a truncated object,
    # and those have different fixes (max_new_tokens versus the prompt).
    return _UNBALANCED if openers else None


# ══════════════════════════════════════════════════════════════════════════════
#  Validation
# ══════════════════════════════════════════════════════════════════════════════
def validate_call(call: Any, *, catalogue: Mapping, path: str,
                  policy: ToolCallPolicy = DEFAULT_TOOL_POLICY) -> tuple:
    """Validate ONE call against the catalogue. Returns a tuple of violations."""
    problems: list = []
    if not isinstance(call, Mapping):
        return (Violation(ReasonCode.ENVELOPE_NOT_OBJECT, path=path),)
    for required in REQUIRED_ENVELOPE_FIELDS:
        if required not in call:
            problems.append(Violation(ReasonCode.ENVELOPE_MISSING_FIELD, path=path,
                                      expected=required))
    if policy.strict_envelope:
        for key in call:
            if key not in REQUIRED_ENVELOPE_FIELDS:
                problems.append(Violation(ReasonCode.ENVELOPE_EXTRA_FIELD,
                                          path=f"{path}.{key}"))
    name = call.get("name")
    if not isinstance(name, str) or name not in catalogue:
        problems.append(Violation(ReasonCode.UNKNOWN_TOOL, path=f"{path}.name"))
        return tuple(problems)
    schema = catalogue[name]
    arguments = call.get("arguments")
    if arguments is None and "arguments" not in call:
        return tuple(problems)
    if not isinstance(arguments, Mapping):
        problems.append(Violation(ReasonCode.ARGUMENTS_NOT_OBJECT,
                                  path=f"{path}.arguments"))
        return tuple(problems)
    problems.extend(_validate_object(arguments, schema.properties,
                                     tuple(schema.required),
                                     schema.additional_properties,
                                     path=f"{path}.arguments"))
    return tuple(problems)


def _validate_object(value: Mapping, properties: Mapping, required: Sequence,
                     additional: bool, *, path: str, nested: bool = False) -> list:
    problems: list = []
    for name in required:
        if name not in value:
            problems.append(Violation(ReasonCode.MISSING_REQUIRED_ARGUMENT,
                                      path=path, expected=str(name)))
    if not additional:
        for name in value:
            if name not in properties:
                problems.append(Violation(ReasonCode.UNKNOWN_ARGUMENT,
                                          path=f"{path}.{name}"))
    for name, spec in properties.items():
        if name not in value:
            continue
        problems.extend(_validate_value(value[name], spec, path=f"{path}.{name}",
                                        nested=nested))
    return problems


def _validate_value(value: Any, spec: Mapping, *, path: str,
                    nested: bool = False) -> list:
    problems: list = []
    declared = spec.get("type")
    allowed = _JSON_TYPES.get(declared, ())
    # `bool` is a subclass of `int`; an integer/number slot must refuse it explicitly.
    if declared in ("integer", "number") and isinstance(value, bool):
        problems.append(Violation(ReasonCode.WRONG_SCALAR_TYPE, path=path,
                                  expected=str(declared)))
        return problems
    if not isinstance(value, allowed):
        code = (ReasonCode.NESTED_TYPE_VIOLATION if nested
                else ReasonCode.WRONG_SCALAR_TYPE)
        problems.append(Violation(code, path=path, expected=str(declared)))
        return problems
    if "enum" in spec:
        members = list(spec["enum"])
        if value not in members:
            problems.append(Violation(ReasonCode.ENUM_VIOLATION, path=path,
                                      expected=f"{len(members)} members"))
    if declared == "object":
        problems.extend(_validate_object(
            value, spec.get("properties", {}), tuple(spec.get("required", ())),
            bool(spec.get("additionalProperties", False)), path=path, nested=True))
    if declared == "array":
        if not isinstance(value, (list, tuple)):  # pragma: no cover - typed above
            problems.append(Violation(ReasonCode.ARRAY_NOT_A_LIST, path=path))
            return problems
        items = spec.get("items")
        if isinstance(items, Mapping):
            for index, element in enumerate(value):
                element_problems = _validate_value(element, items,
                                                   path=f"{path}[{index}]", nested=True)
                for problem in element_problems:
                    if problem.code in (ReasonCode.WRONG_SCALAR_TYPE,
                                        ReasonCode.NESTED_TYPE_VIOLATION):
                        problems.append(Violation(
                            ReasonCode.ARRAY_ELEMENT_TYPE_VIOLATION,
                            path=problem.path, expected=problem.expected))
                    else:
                        problems.append(problem)
    return problems


def build_catalogue(schemas: Sequence) -> dict:
    """Index tool schemas by name. Refuses a duplicate: two tools, one name, no answer."""
    catalogue: dict = {}
    for schema in schemas:
        if not isinstance(schema, ToolSchema):
            raise ToolCallError("catalogue: every entry must be a ToolSchema")
        if schema.name in catalogue:
            raise ToolCallError(
                f"catalogue: {schema.name!r} is declared twice; a call naming it could "
                f"be validated against either contract")
        catalogue[schema.name] = schema
    return catalogue


def validate_response(text: str, *, catalogue: Mapping, calls_required: bool,
                      policy: ToolCallPolicy = DEFAULT_TOOL_POLICY
                      ) -> ToolCallValidation:
    """The whole check on one response. THIS is what D28 never called.

    ``calls_required`` is not optional and has no default. A caller that does not know
    whether the task demanded a call cannot get a verdict from this function, which is
    the property that stops absence quietly meaning success again.
    """
    if not isinstance(calls_required, bool):
        raise ToolCallError(
            "validate_response: calls_required must be stated explicitly; the D28 "
            "defect is exactly what an implicit default here produces")
    calls, violations = extract_calls(text)
    problems = list(violations)

    if not calls:
        if calls_required and policy.absence_is_a_failure:
            if not any(v.code in (ReasonCode.NO_TOOL_CALL_EMITTED,
                                  ReasonCode.PROSE_NOT_A_TOOL_CALL,
                                  ReasonCode.INVALID_JSON) for v in problems):
                problems.append(Violation(ReasonCode.NO_TOOL_CALL_EMITTED))
            return ToolCallValidation(
                validator_version=TOOL_CALL_VALIDATOR_VERSION, valid=False,
                call_count=0, violations=tuple(problems),
                policy_hash=policy.policy_hash())
        # No call, none demanded. "Wrote prose" and "wrote nothing" are not defects
        # here, so those two codes are dropped. A MALFORMED call is still a defect: a
        # response that tried to emit one and produced broken JSON has a bug whether or
        # not the task asked for a call, and silently forgiving it is how a truncated
        # envelope goes unnoticed.
        surviving = tuple(v for v in problems
                          if v.code not in (ReasonCode.NO_TOOL_CALL_EMITTED,
                                            ReasonCode.PROSE_NOT_A_TOOL_CALL))
        return ToolCallValidation(
            validator_version=TOOL_CALL_VALIDATOR_VERSION,
            valid=not surviving, call_count=0, violations=surviving,
            policy_hash=policy.policy_hash())

    if len(calls) > policy.max_calls:
        problems.append(Violation(ReasonCode.TOO_MANY_CALLS,
                                  expected=str(policy.max_calls)))
    for index, call in enumerate(calls):
        problems.extend(validate_call(call, catalogue=catalogue,
                                      path=f"call[{index}]", policy=policy))
    return ToolCallValidation(
        validator_version=TOOL_CALL_VALIDATOR_VERSION, valid=not problems,
        call_count=len(calls), violations=tuple(problems),
        policy_hash=policy.policy_hash())


__all__ = ["DEFAULT_TOOL_POLICY", "REQUIRED_ENVELOPE_FIELDS", "ReasonCode",
           "TOOL_CALL_VALIDATOR_VERSION", "ToolCallError", "ToolCallPolicy",
           "ToolCallValidation", "ToolSchema", "Violation", "build_catalogue",
           "extract_calls", "validate_call", "validate_response"]
