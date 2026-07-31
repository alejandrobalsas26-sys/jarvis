"""training_gym/graders/schema_grader.py — V69 M62: is the answer actually the shape.

WHY THIS EXISTS
---------------
"The model returned JSON" is not the claim a structured-output task needs. The claim
is "the model returned an object that satisfies the contract this task declared", and
the gap between the two is where a triage report with no verdict field, a timeline with
an empty events list and a tool call with a stringified argument object all pass.

So this grader validates the PARSED document against
:attr:`~training_gym.task_spec.TaskSpec.expected_output_schema`, and it counts what it
validated. A document with zero nodes cannot pass: an empty object satisfies almost
every permissive schema ever written, and reporting that as a measurement is the
purest form of the vacuous PASS this milestone exists to make impossible.

THE VALIDATOR
-------------
``jsonschema`` is used when it is importable, and nothing is substituted for it when it
is not. Hand-rolling a "good enough" subset validator here would be worse than having
none: it would agree with the real one on the easy cases and diverge silently on the
ones that matter, while still reporting PASS. Absent, the grader reports SKIPPED, or
INSUFFICIENT_EVIDENCE when the task made it mandatory — both of which block.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from ..schemas import ResultStatus, Severity
from ..task_spec import TaskFamily
from .base import (
    GRADER_PROTOCOL_VERSION,
    Grader,
    GraderContext,
    ToolProbe,
    errored,
    insufficient,
    make_result,
)

#: Deepest structure the node counter will walk. A document deeper than this is
#: pathological rather than structured.
MAX_COUNT_DEPTH = 24
#: Most validation errors carried into a result.
MAX_SCHEMA_ERRORS = 20


def load_validator() -> tuple[Any, str]:
    """``(validator_class, version)`` for the JSON-Schema library, or ``(None, "")``.

    A module-level function so a test can prove the absent-validator path without
    uninstalling anything, and so the import cost is paid when a task needs it rather
    than when the package is imported.
    """
    try:
        import jsonschema
        from jsonschema.validators import validator_for
    except Exception:  # noqa: BLE001 — absence must be visible, not silently clean
        return None, ""
    version = getattr(jsonschema, "__version__", "")
    if not version:  # pragma: no cover — depends on the installed distribution
        try:
            from importlib.metadata import version as _dist_version
            version = _dist_version("jsonschema")
        except Exception:  # noqa: BLE001
            version = "unknown"
    return validator_for, f"jsonschema {version}"


def count_nodes(document: Any, *, _depth: int = 0) -> int:
    """How many actual VALUES a document carries. ``{}`` and ``[]`` count zero.

    This is the grader's non-vacuity measurement, so only leaves are counted. A
    container contributes exactly what is inside it and nothing for itself, which is
    what makes ``{"findings": []}`` measure zero rather than one: an object whose only
    content is an empty list has told the task nothing, and "I validated one empty
    container" is precisely the vacuous PASS this field exists to prevent.
    """
    if _depth > MAX_COUNT_DEPTH:
        return 0
    if isinstance(document, Mapping):
        return sum(count_nodes(v, _depth=_depth + 1) for v in document.values())
    if isinstance(document, (list, tuple)):
        return sum(count_nodes(v, _depth=_depth + 1) for v in document)
    return 0 if document is None else 1


class JsonSchemaGrader(Grader):
    """Validates the attempt's structured output against the task's declared schema."""

    grader_id = "json_schema"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.json_schema.1"
    #: Only the families whose answer is machine-checkable. For anything else the
    #: honest answer is NOT_APPLICABLE — and the aggregator still refuses to accept
    #: that from a grader the task declared mandatory.
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(
        f for f in TaskFamily if f.requires_structured_output)

    def availability(self, ctx: GraderContext) -> ToolProbe:
        validator_for, version = load_validator()
        if validator_for is None:
            return ToolProbe(name="jsonschema", available=False,
                             reason="the jsonschema library is not importable; the gym "
                                    "never installs a missing validator and never "
                                    "substitutes a weaker one")
        return ToolProbe(name="jsonschema", available=True, version=version)

    def measure(self, ctx: GraderContext):
        schema = dict(ctx.spec.expected_output_schema or {})
        if not schema:
            # TaskSpec already refuses to construct such a task, so reaching here means
            # the spec was mutated after validation. Refuse rather than pass.
            return insufficient(self, "the task declares no expected_output_schema, so "
                                      "there is no contract to validate against")

        probe = self.availability(ctx)
        if not probe.available:
            return self.unavailable(ctx, probe)
        validator_for, _version = load_validator()

        document, parse_error = self._parse(ctx)
        if parse_error:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                evidence=(parse_error,), tool_version=probe.version,
                measured=1,
                findings=({"kind": "malformed_json", "detail": parse_error},))

        validator_cls = validator_for(schema)
        try:
            validator_cls.check_schema(schema)
        except Exception as exc:  # noqa: BLE001 — a broken schema proves nothing
            return errored(self, f"expected_output_schema is not a valid schema: "
                                 f"{ctx.sanitize(str(exc), limit=300)}")

        nodes = count_nodes(document)
        if nodes <= 0:
            return insufficient(
                self, "the structured output contains no values; an empty document "
                      "satisfies almost any schema and measures nothing",
                tool_version=probe.version)

        errors = sorted(validator_cls(schema).iter_errors(document),
                        key=lambda e: list(e.absolute_path))
        if errors:
            findings = [{"kind": "schema_violation",
                         "path": "/".join(str(p) for p in e.absolute_path) or "<root>",
                         "detail": ctx.sanitize(str(e.message), limit=200)}
                        for e in errors[:MAX_SCHEMA_ERRORS]]
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                evidence=(f"{len(errors)} schema violation(s) over {nodes} value(s)",),
                findings=findings, tool_version=probe.version, measured=nodes)

        return make_result(
            self, ResultStatus.PASS, score=1.0, tool_version=probe.version,
            measured=nodes,
            evidence=(f"validated {nodes} value(s) against the task schema",))

    # -- parsing ---------------------------------------------------------------
    def _parse(self, ctx: GraderContext) -> tuple[Any, str]:
        """The parsed document, or ``(None, reason)``.

        A structured output supplied by the runner is preferred over re-parsing the
        answer text: re-parsing would grade a different artifact from the one the
        episode actually produced.
        """
        if ctx.structured_output is not None:
            if isinstance(ctx.structured_output, (Mapping, list, tuple, str, int,
                                                  float, bool)):
                return ctx.structured_output, ""
            return None, (f"structured output is a "
                          f"{type(ctx.structured_output).__name__}, which is not a "
                          f"JSON document")
        text = str(ctx.answer or "").strip()
        if not text:
            return None, "the attempt produced no answer to validate"
        try:
            return json.loads(text), ""
        except (ValueError, TypeError) as exc:
            return None, f"the answer is not parseable JSON: {type(exc).__name__}"


__all__ = ["MAX_SCHEMA_ERRORS", "JsonSchemaGrader", "count_nodes", "load_validator"]
