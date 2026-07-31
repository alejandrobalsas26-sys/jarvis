"""training_gym.graders — V69 M62: the deterministic evidence layer.

Eleven checks, one protocol, one aggregation rule. Every verdict is a frozen
:class:`~training_gym.trajectory.GraderResult`; no grader returns a raw dictionary,
no grader may report PASS without saying how much it measured, and no missing
external validator can become anything other than a status that BLOCKS a mandatory
check.

:data:`DEFAULT_GRADERS` maps every id in
:data:`~training_gym.task_spec.GRADER_IDS` to its implementation, and
:func:`build_graders` resolves the set a task asked for. The mapping is verified
complete at import time: a grader id that exists in the frozen registry with nothing
behind it would mean a task could require evidence that nothing can ever produce, and
that failure must be loud at import rather than quiet at grading time.

Importing this package starts no process, reads no environment and probes no tool.
Availability is resolved when a grader is asked to run, against the workspace it was
given, so the answer describes the host the grading actually happened on.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..schemas import SchemaError
from ..task_spec import GRADER_IDS, TaskSpec
from .aggregate import (
    AGGREGATION_VERSION,
    AggregationReport,
    aggregate,
    attach_results,
    requested_graders,
    validate_result_set,
)
from .bandit_grader import BanditGrader
from .base import (
    DEFAULT_GRADER_TIMEOUT_S,
    GRADER_PROTOCOL_VERSION,
    MAX_GRADER_OUTPUT_BYTES,
    ChangedFile,
    CommandOutcome,
    Grader,
    GraderContext,
    ToolProbe,
    grader_environment,
    probe_tool,
    run_bounded,
)
from .detection_grader import DetectionRuleGrader
from .diff_budget_grader import DiffBudget, DiffBudgetGrader
from .evidence_grader import EvidenceCitationGrader
from .file_boundary_grader import FileBoundaryGrader
from .pytest_grader import PytestGrader
from .ruff_grader import RuffGrader
from .safety_grader import SafetyPolicyGrader
from .schema_grader import JsonSchemaGrader
from .secret_grader import SecretAndPIIGrader
from .tool_call_grader import ToolCallSchemaGrader, ToolContract

#: Every frozen grader id, and the class that implements it.
DEFAULT_GRADERS: dict[str, type[Grader]] = {
    "pytest": PytestGrader,
    "ruff": RuffGrader,
    "bandit": BanditGrader,
    "json_schema": JsonSchemaGrader,
    "secret_pii": SecretAndPIIGrader,
    "tool_call_schema": ToolCallSchemaGrader,
    "evidence_citation": EvidenceCitationGrader,
    "diff_budget": DiffBudgetGrader,
    "file_boundary": FileBoundaryGrader,
    "detection_rule": DetectionRuleGrader,
    "safety_policy": SafetyPolicyGrader,
}

_unimplemented = sorted(set(GRADER_IDS) - set(DEFAULT_GRADERS))
if _unimplemented:  # pragma: no cover — structural guard, asserted by a focused test
    raise SchemaError(
        f"training_gym.graders: grader id(s) {_unimplemented} are in the frozen "
        f"registry with no implementation; a task could require evidence that nothing "
        f"can produce")
_unregistered = sorted(set(DEFAULT_GRADERS) - set(GRADER_IDS))
if _unregistered:  # pragma: no cover — structural guard
    raise SchemaError(
        f"training_gym.graders: {_unregistered} implement ids no task can name")
del _unimplemented, _unregistered


def build_graders(spec: TaskSpec, *,
                  overrides: dict[str, Grader] | None = None) -> tuple[Grader, ...]:
    """The graders *spec* asked for, in a deterministic order.

    ``overrides`` lets a caller supply a pre-configured instance — a
    :class:`~training_gym.graders.diff_budget_grader.DiffBudgetGrader` with a
    task-specific budget, say — without giving it the ability to introduce a grader the
    task never requested: an override for an unrequested id is refused rather than
    quietly added.
    """
    requested = requested_graders(spec)
    supplied = dict(overrides or {})
    unknown = sorted(set(supplied) - set(requested))
    if unknown:
        raise SchemaError(f"build_graders: override(s) {unknown} were not requested by "
                          f"task {spec.task_id}")
    built: list[Grader] = []
    for grader_id in requested:
        instance = supplied.get(grader_id)
        if instance is None:
            instance = DEFAULT_GRADERS[grader_id]()
        elif instance.grader_id != grader_id:
            raise SchemaError(f"build_graders: override for {grader_id!r} reports "
                              f"grader_id {instance.grader_id!r}")
        built.append(instance)
    return tuple(built)


def run_graders(graders: Sequence[Grader], ctx: GraderContext) -> tuple:
    """Run each grader through the protocol and return its normalised result.

    Order is the order given, which :func:`build_graders` makes deterministic. No
    grader can prevent another from running: :meth:`Grader.grade` never raises.
    """
    return tuple(grader.grade(ctx) for grader in graders)


__all__ = [
    "AGGREGATION_VERSION", "DEFAULT_GRADERS", "DEFAULT_GRADER_TIMEOUT_S",
    "GRADER_PROTOCOL_VERSION", "MAX_GRADER_OUTPUT_BYTES", "AggregationReport",
    "BanditGrader", "ChangedFile", "CommandOutcome", "DetectionRuleGrader",
    "DiffBudget", "DiffBudgetGrader", "EvidenceCitationGrader", "FileBoundaryGrader",
    "Grader", "GraderContext", "JsonSchemaGrader", "PytestGrader", "RuffGrader",
    "SafetyPolicyGrader", "SecretAndPIIGrader", "ToolCallSchemaGrader",
    "ToolContract", "ToolProbe", "aggregate", "attach_results", "build_graders",
    "grader_environment", "probe_tool", "requested_graders", "run_bounded",
    "run_graders", "validate_result_set",
]
