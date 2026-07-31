"""training_gym.graders — V69 M62: the deterministic evidence layer.

Eleven checks, one protocol, one aggregation rule. Every verdict is a frozen
:class:`~training_gym.trajectory.GraderResult`; no grader returns a raw dictionary,
no grader may report PASS without saying how much it measured, and no missing
external validator can become anything other than a status that BLOCKS a mandatory
check.

Importing this package starts no process, reads no environment and probes no tool.
Availability is resolved when a grader is asked to run, against the workspace it was
given, so the answer describes the host the grading actually happened on.
"""
from __future__ import annotations

from .aggregate import (
    AGGREGATION_VERSION,
    AggregationReport,
    aggregate,
    attach_results,
    requested_graders,
    validate_result_set,
)
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

__all__ = [
    "AGGREGATION_VERSION", "DEFAULT_GRADER_TIMEOUT_S", "GRADER_PROTOCOL_VERSION",
    "MAX_GRADER_OUTPUT_BYTES", "AggregationReport", "ChangedFile", "CommandOutcome",
    "Grader", "GraderContext", "ToolProbe", "aggregate", "attach_results",
    "grader_environment", "probe_tool", "requested_graders", "run_bounded",
    "validate_result_set",
]
