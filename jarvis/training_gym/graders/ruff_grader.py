"""training_gym/graders/ruff_grader.py — V69 M62: lint as evidence, not as decoration.

WHY THIS EXISTS
---------------
Ruff is the cheapest real signal about whether a change is a fix or a mess: an unused
import, a redefinition, an undefined name and a broken f-string are all correctness
problems a passing test suite happily ignores.

The trap this grader is built around is that "ruff found nothing" and "ruff read
nothing" produce the same empty output. So the measurement is not the finding count —
it is the number of Python files this grader ENUMERATED inside the allowlist before
running anything. Zero files is INSUFFICIENT_EVIDENCE, whatever ruff said.

WHAT IT NEVER DOES
------------------
No ``--fix``, no ``--unsafe-fixes``, no configuration written or rewritten. A grader
that repaired the code it was measuring would be reporting on a file the diff does not
describe, and the attempt would be credited with a change it never made.
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

#: Most individual violations carried into a result. The count is always exact; only
#: the per-violation detail is bounded.
MAX_REPORTED_VIOLATIONS = 25
#: Ceiling on files enumerated for one run.
MAX_SCANNED_FILES = 2000


class RuffGrader(Grader):
    """Runs ruff over the task's allowlisted source paths and counts what it read."""

    grader_id = "ruff"
    grader_version = f"{GRADER_PROTOCOL_VERSION}.ruff.1"
    required_tool = "ruff"
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(TaskFamily)

    def __init__(self, max_violations: int = 0) -> None:
        #: How many violations the task tolerates before this grader fails. Zero by
        #: default: a lint finding in a change this small is a defect, not noise.
        self.max_violations = max(0, int(max_violations))

    def measure(self, ctx: GraderContext):
        blocked = self.require_workspace(ctx)
        if blocked is not None:
            return blocked
        if not ctx.source_paths:
            return insufficient(
                self, "the task declares no allowlisted source path; this grader never "
                      "lints a path it was not given")

        probe = self.availability(ctx)
        if not probe.available:
            return self.unavailable(ctx, probe)

        targets = self.allowlisted_targets(ctx, ctx.source_paths,
                                           field_name="source_path")
        files = _python_files(targets)
        if not files:
            return insufficient(
                self, f"no Python file exists under {list(ctx.source_paths)}; ruff "
                      f"reading nothing and ruff finding nothing look identical, and "
                      f"only one of them is a pass",
                tool_version=probe.version)

        argv = [probe.executable, "check", "--no-cache", "--no-fix",
                "--output-format", "json", "--quiet", *(str(t) for t in targets)]
        outcome = run_bounded(argv, cwd=ctx.workspace_root, timeout_s=ctx.timeout_s)

        if outcome.timed_out:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.MEDIUM,
                tool_version=probe.version, measured=len(files),
                evidence=(f"ruff exceeded {ctx.timeout_s}s and its process tree was "
                          f"killed",),
                findings=({"kind": "timeout", "argv": list(outcome.argv)},))
        if not outcome.started:
            return errored(self, f"ruff could not be started: {outcome.error}")

        try:
            diagnostics = json.loads(outcome.stdout or "[]")
        except (ValueError, TypeError):
            return errored(
                self, f"ruff exited {outcome.exit_code} and its JSON report could not "
                      f"be parsed: "
                      f"{ctx.sanitize(outcome.stderr or outcome.stdout, limit=300)}")
        if not isinstance(diagnostics, list):
            return errored(self, "ruff's JSON report was not a list of diagnostics")

        findings = [{
            "kind": "lint_violation",
            "code": str(d.get("code") or "?"),
            "path": _relative(ctx, str(d.get("filename") or "")),
            "line": (d.get("location") or {}).get("row", 0),
            "detail": ctx.sanitize(str(d.get("message") or ""), limit=200),
        } for d in diagnostics[:MAX_REPORTED_VIOLATIONS] if isinstance(d, dict)]

        evidence = (f"{len(files)} file(s) scanned, {len(diagnostics)} violation(s), "
                    f"exit={outcome.exit_code}",
                    f"tool: {probe.version}")
        if len(diagnostics) > self.max_violations:
            return make_result(
                self, ResultStatus.FAIL, score=0.0, severity=Severity.LOW,
                evidence=evidence, findings=findings, tool_version=probe.version,
                measured=len(files))
        return make_result(
            self, ResultStatus.PASS, score=1.0, evidence=evidence, findings=findings,
            tool_version=probe.version, measured=len(files))


def _python_files(targets: tuple) -> tuple:
    """Every ``.py`` file under the allowlisted targets. This IS the measurement."""
    found: list = []
    for target in targets:
        if target.is_file():
            if target.suffix == ".py":
                found.append(target)
            continue
        for path in sorted(target.rglob("*.py")):
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path)
            if len(found) >= MAX_SCANNED_FILES:
                return tuple(found)
    return tuple(found)


def _relative(ctx: GraderContext, absolute: str) -> str:
    """A workspace-relative path, or a sanitized one when it is somehow outside."""
    if ctx.workspace_root is not None and absolute:
        try:
            return str(_as_path(absolute).relative_to(ctx.workspace_root)).replace(
                "\\", "/")
        except (ValueError, OSError):
            pass
    return ctx.sanitize(absolute, limit=160)


def _as_path(text: str):
    from pathlib import Path
    return Path(text)


__all__ = ["MAX_REPORTED_VIOLATIONS", "MAX_SCANNED_FILES", "RuffGrader"]
