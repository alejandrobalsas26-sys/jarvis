"""training_gym/graders/pytest_grader.py — V69 M62: the suite, and what it cost.

WHY THIS EXISTS
---------------
"The tests pass" is the single most load-bearing sentence in a coding task, and on its
own it is nearly meaningless: a suite with the failing test deleted also passes, and so
does one where collection silently found nothing. This grader therefore reports three
numbers rather than a verdict — collected, passed, failed — and treats the first one as
the measurement. Zero collected tests is INSUFFICIENT_EVIDENCE, never a pass.

WHAT IT REFUSES TO DO
---------------------
  * run anything outside the task's allowlisted test locations, each of which is
    proven to be inside the workspace by the frozen path authority before it can
    become an argv element;
  * accept extra pytest arguments from a task, an answer or an environment variable.
    The argv is built here and nowhere else, and ``-o addopts=`` neutralises whatever
    the workspace's own ini file tried to add — a fixture that ships
    ``addopts = -p my_plugin --forked`` would otherwise be choosing the grader's
    execution model for it;
  * write anything, anywhere. ``-p no:cacheprovider`` keeps the graded tree
    byte-identical to the one the diff describes, and the counts are read from
    pytest's own summary rather than from a report FILE. That last choice is a
    security decision, not a convenience one: a ``--junit-xml`` path appears in
    ``sys.argv`` of the process running the model's code, so a hostile test could
    overwrite the report this grader then parses — and an XML document supplied by
    the thing being graded is exactly the input an XML parser must never be handed.

REGRESSION EVIDENCE
-------------------
When the context carries a baseline, the comparison is explicit: the previously
failing tests must now pass, and the collected count must not have SHRUNK. That second
check is the one that makes "delete the test" a detectable failure at this layer
rather than only at the diff layer.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..schemas import ResultStatus, Severity
from ..task_spec import TaskFamily
from .base import (
    GRADER_PROTOCOL_VERSION,
    Grader,
    GraderContext,
    errored,
    insufficient,
    make_result,
    probe_tool,
    run_bounded,
)

#: Exit codes pytest documents. Only the first two describe a completed run.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_NO_TESTS_COLLECTED = 5

#: Plugins disabled unconditionally: the cache writes into the graded tree, and a
#: parallel runner makes the process tree this grader must be able to kill unbounded.
_DISABLED_PLUGINS: tuple[str, ...] = ("cacheprovider", "xdist")

_SUMMARY_RE = re.compile(
    r"(?:(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors|skipped|xfailed|"
    r"xpassed))")


@dataclass(frozen=True)
class PytestCounts:
    """What the run actually measured."""

    collected: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    parsed_from: str = ""

    def to_dict(self) -> dict:
        return {"collected": self.collected, "passed": self.passed,
                "failed": self.failed, "errors": self.errors,
                "skipped": self.skipped, "parsed_from": self.parsed_from}


def parse_summary_line(text: str) -> PytestCounts:
    """Counts recovered from pytest's own summary line.

    The summary is used rather than a report file on purpose — see the module
    docstring. It is written by pytest to a pipe this grader owns, so unlike a file at
    a path the graded code can see, it cannot be replaced by the thing being graded.
    """
    counts = {"passed": 0, "failed": 0, "error": 0, "errors": 0, "skipped": 0,
              "xfailed": 0, "xpassed": 0}
    for match in _SUMMARY_RE.finditer(text or ""):
        counts[match.group("kind")] = int(match.group("count"))
    errors = counts["error"] + counts["errors"]
    passed = counts["passed"] + counts["xpassed"]
    failed = counts["failed"] + counts["xfailed"]
    return PytestCounts(collected=passed + failed + errors + counts["skipped"],
                        passed=passed, failed=failed, errors=errors,
                        skipped=counts["skipped"], parsed_from="summary-line")


class PytestGrader(Grader):
    """Runs the task's allowlisted tests and reports what was collected, not just how
    it ended."""

    grader_id = "pytest"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.pytest.1"
    required_tool = "pytest"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset({
        TaskFamily.CODING_FIX})

    def measure(self, ctx: GraderContext):
        blocked = self.require_workspace(ctx)
        if blocked is not None:
            return blocked
        if not ctx.test_paths:
            return insufficient(
                self, "the task declares no allowlisted test location; this grader "
                      "never discovers tests on its own, because a discovered path is "
                      "an unbounded one")

        probe = self.availability(ctx)
        if not probe.available:
            return self.unavailable(ctx, probe)

        targets = self.allowlisted_targets(ctx, ctx.test_paths, field_name="test_path")
        if not targets:
            return insufficient(
                self, f"none of the allowlisted test locations "
                      f"{list(ctx.test_paths)} exists in the workspace",
                tool_version=probe.version)

        argv = self._argv(probe.executable, ctx, targets)
        outcome = run_bounded(argv, cwd=ctx.workspace_root, timeout_s=ctx.timeout_s)

        if outcome.timed_out:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                tool_version=probe.version, measured=1,
                evidence=(f"the suite exceeded {ctx.timeout_s}s and its process tree "
                          f"was killed",),
                findings=({"kind": "timeout", "argv": list(outcome.argv)},))
        if not outcome.started:
            return errored(self, f"pytest could not be started: {outcome.error}")

        counts = parse_summary_line(f"{outcome.stdout}\n{outcome.stderr}")
        if counts.collected == 0 and outcome.exit_code not in (
                PYTEST_OK, PYTEST_TESTS_FAILED, PYTEST_NO_TESTS_COLLECTED):
            # An exit code outside pytest's documented set with no parseable summary is
            # a broken invocation, not an empty suite, and must not be reported as one.
            return errored(
                self, f"pytest exited {outcome.exit_code} and produced no summary this "
                      f"grader could parse: "
                      f"{ctx.sanitize(outcome.stderr or outcome.stdout, limit=400)}")

        evidence = [
            f"exit={outcome.exit_code}, collected={counts.collected}, "
            f"passed={counts.passed}, failed={counts.failed}, "
            f"errors={counts.errors}, skipped={counts.skipped} "
            f"({counts.parsed_from})",
            ctx.sanitize(_tail(outcome.stdout, 800), limit=800),
        ]

        if counts.collected == 0:
            return insufficient(
                self, "pytest collected zero tests; a suite that ran nothing has not "
                      "demonstrated anything",
                tool_version=probe.version, measured=0,
                findings=({"kind": "no_tests_collected",
                           "exit_code": outcome.exit_code},))

        findings = list(self._baseline_findings(ctx, counts))
        if counts.failed or counts.errors:
            findings.append({"kind": "tests_failing", "failed": counts.failed,
                             "errors": counts.errors, "blocking": False})
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                evidence=tuple(evidence), findings=findings,
                tool_version=probe.version, measured=counts.collected)
        if any(f.get("blocking") for f in findings):
            return make_result(
                self, ResultStatus.FAIL, score=0.0, blocking=True,
                severity=Severity.HIGH, evidence=tuple(evidence), findings=findings,
                tool_version=probe.version, measured=counts.collected)

        return make_result(
            self, ResultStatus.PASS, score=1.0, evidence=tuple(evidence),
            findings=findings, tool_version=probe.version,
            measured=counts.collected)

    # -- argv ------------------------------------------------------------------
    def _argv(self, executable: str, ctx: GraderContext,
              targets: tuple[Path, ...]) -> list[str]:
        """The complete command. Nothing is appended from a task, an answer or an env.

        ``-o addopts=`` is a control rather than a tidiness flag: without it a fixture
        shipping its own ``addopts`` chooses this grader's plugins and execution model.
        """
        argv = [executable, "-q", "--no-header", "--tb=no", "-o", "addopts=",
                "-p", "no:randomly"]
        for plugin in _DISABLED_PLUGINS:
            argv.extend(["-p", f"no:{plugin}"])
        argv.extend(str(t) for t in targets)
        return argv

    # -- regression evidence ---------------------------------------------------
    def _baseline_findings(self, ctx: GraderContext, counts: PytestCounts) -> list[dict]:
        baseline = ctx.baseline.get("pytest") if isinstance(ctx.baseline, Mapping) else None
        if not isinstance(baseline, Mapping):
            return []
        before_collected = int(baseline.get("collected", 0) or 0)
        before_failed = int(baseline.get("failed", 0) or 0)
        findings: list[dict] = []
        if before_collected and counts.collected < before_collected:
            findings.append({
                "kind": "tests_disappeared", "blocking": True,
                "before": before_collected, "after": counts.collected,
                "detail": "fewer tests are collected than before the change; a suite "
                          "that passes because the test is gone has not been fixed"})
        if before_failed and counts.failed >= before_failed:
            findings.append({
                "kind": "no_regression_progress", "blocking": False,
                "before_failed": before_failed, "after_failed": counts.failed,
                "detail": "the previously failing tests still fail"})
        if counts.skipped > int(baseline.get("skipped", 0) or 0):
            findings.append({
                "kind": "skips_increased", "blocking": True,
                "before": int(baseline.get("skipped", 0) or 0),
                "after": counts.skipped,
                "detail": "more tests are skipped than before; a skip is not a fix"})
        return findings


def _tail(text: str, limit: int) -> str:
    raw = str(text or "")
    return raw if len(raw) <= limit else f"…{raw[-limit:]}"


def pytest_probe(cwd: Path | None = None):
    """The tool probe used by :class:`PytestGrader`, exposed for a capability report."""
    return probe_tool("pytest", cwd=cwd)


__all__ = ["PYTEST_NO_TESTS_COLLECTED", "PYTEST_OK", "PYTEST_TESTS_FAILED",
           "PytestCounts", "PytestGrader", "parse_summary_line", "pytest_probe"]
