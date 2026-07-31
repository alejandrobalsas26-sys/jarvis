"""training_gym/graders/bandit_grader.py — V69 M62: the security scan, honestly counted.

WHY THIS EXISTS
---------------
JARVIS's own security posture is expressed as a number — zero Medium and zero High
Bandit findings — and this grader is what makes that number mean something for code an
episode produced. It exists because the two failure modes of a security scan are
symmetric and both end in a green report: the scan found nothing, and the scan read
nothing.

So the measurement is the LINES OF CODE Bandit reported having analysed, taken from its
own metrics rather than inferred. Zero lines is INSUFFICIENT_EVIDENCE. A missing
Bandit is SKIPPED or INSUFFICIENT_EVIDENCE. Neither is ever a pass.

WHAT IT DOES WITH LOW FINDINGS
------------------------------
Surfaces them. Low findings do not block by default — the repository's own baseline
carries some — but they are recorded in full, because the alternative is a grader whose
report says "clean" about a file that has three. The suppression count is recorded for
the same reason: a fix that reached zero Medium by adding four ``# nosec`` comments is
a diff-budget finding, and this grader's job is to make sure the number that would
reveal it is present in the record.
"""
from __future__ import annotations

import json
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
    run_bounded,
)

#: Most individual issues carried into a result. The counts are always exact.
MAX_REPORTED_ISSUES = 25

_SEVERITIES = ("HIGH", "MEDIUM", "LOW", "UNDEFINED")


class BanditGrader(Grader):
    """Runs Bandit over allowlisted Python and enforces the task's severity ceiling."""

    grader_id = "bandit"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.bandit.1"
    required_tool = "bandit"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(TaskFamily)

    def __init__(self, *, max_high: int = 0, max_medium: int = 0,
                 max_low: int | None = None) -> None:
        #: The posture the repository already holds, expressed as a ceiling. ``None``
        #: for ``max_low`` means "surface but do not block", which is what keeps the
        #: existing approved Low baseline from turning every task red.
        self.max_high = max(0, int(max_high))
        self.max_medium = max(0, int(max_medium))
        self.max_low = None if max_low is None else max(0, int(max_low))

    def measure(self, ctx: GraderContext):
        blocked = self.require_workspace(ctx)
        if blocked is not None:
            return blocked
        if not ctx.source_paths:
            return insufficient(
                self, "the task declares no allowlisted source path; this grader never "
                      "scans a path it was not given")

        probe = self.availability(ctx)
        if not probe.available:
            return self.unavailable(ctx, probe)

        targets = self.allowlisted_targets(ctx, ctx.source_paths,
                                           field_name="source_path")
        if not targets:
            return insufficient(
                self, f"none of the allowlisted source paths {list(ctx.source_paths)} "
                      f"exists in the workspace",
                tool_version=probe.version)

        argv = [probe.executable, "--quiet", "--format", "json", "--recursive",
                *(str(t) for t in targets)]
        outcome = run_bounded(argv, cwd=ctx.workspace_root, timeout_s=ctx.timeout_s)

        if outcome.timed_out:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                tool_version=probe.version, measured=1,
                evidence=(f"bandit exceeded {ctx.timeout_s}s and its process tree was "
                          f"killed",),
                findings=({"kind": "timeout", "argv": list(outcome.argv)},))
        if not outcome.started:
            return errored(self, f"bandit could not be started: {outcome.error}")

        try:
            report = json.loads(outcome.stdout or "{}")
        except (ValueError, TypeError):
            return errored(
                self, f"bandit exited {outcome.exit_code} and its JSON report could "
                      f"not be parsed: "
                      f"{ctx.sanitize(outcome.stderr or outcome.stdout, limit=300)}")
        if not isinstance(report, dict) or "results" not in report:
            return errored(self, "bandit's JSON report has no results section")

        totals = ((report.get("metrics") or {}).get("_totals") or {})
        loc = int(totals.get("loc", 0) or 0)
        nosec = int(totals.get("nosec", 0) or 0)
        skipped_tests = int(totals.get("skipped_tests", 0) or 0)
        issues = [r for r in report.get("results") or [] if isinstance(r, dict)]
        counts = {level: sum(1 for r in issues
                             if str(r.get("issue_severity", "")).upper() == level)
                  for level in _SEVERITIES}

        if loc <= 0:
            return insufficient(
                self, "bandit analysed zero lines of code; a scan that read nothing "
                      "and a scan that found nothing produce the same empty report",
                tool_version=probe.version, measured=0,
                findings=({"kind": "no_code_scanned", "files": len(targets)},))

        findings = [{
            "kind": "bandit_issue",
            "severity": str(r.get("issue_severity", "")).upper(),
            "confidence": str(r.get("issue_confidence", "")).upper(),
            "test_id": str(r.get("test_id") or ""),
            "path": _relative(ctx, str(r.get("filename") or "")),
            "line": r.get("line_number", 0),
            "detail": ctx.sanitize(str(r.get("issue_text") or ""), limit=200),
        } for r in issues[:MAX_REPORTED_ISSUES]]
        if nosec or skipped_tests:
            # Bandit counts these separately and both matter: ``nosec`` is the BLANKET
            # form that disables every check on the line, ``skipped_tests`` the
            # id-specific form. Reporting only one would let the other grow silently.
            findings.append({
                "kind": "suppressions_present", "blanket": nosec,
                "by_test_id": skipped_tests, "count": nosec + skipped_tests,
                "blocking": False,
                "detail": "lines suppressed with '# nosec'; a change that reached zero "
                          "findings by adding these is a diff-budget failure"})

        evidence = (
            f"{loc} LOC over {len(targets)} allowlisted target(s); "
            f"high={counts['HIGH']}, medium={counts['MEDIUM']}, low={counts['LOW']}, "
            f"nosec={nosec}, skipped_tests={skipped_tests}, "
            f"exit={outcome.exit_code}",
            f"tool: {probe.version}",
        )

        over_high = counts["HIGH"] > self.max_high
        over_medium = counts["MEDIUM"] > self.max_medium
        over_low = self.max_low is not None and counts["LOW"] > self.max_low
        if over_high or over_medium:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, blocking=True,
                severity=Severity.BLOCKING, evidence=evidence, findings=findings,
                tool_version=probe.version, measured=loc)
        if over_low:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.LOW,
                evidence=evidence, findings=findings, tool_version=probe.version,
                measured=loc)
        return make_result(
            self, ResultStatus.PASS, score=1.0, evidence=evidence, findings=findings,
            tool_version=probe.version, measured=loc)


def _relative(ctx: GraderContext, absolute: str) -> str:
    from pathlib import Path
    if ctx.workspace_root is not None and absolute:
        try:
            return str(Path(absolute).relative_to(ctx.workspace_root)).replace("\\", "/")
        except (ValueError, OSError):
            pass
    return ctx.sanitize(absolute, limit=160)


__all__ = ["MAX_REPORTED_ISSUES", "BanditGrader"]
