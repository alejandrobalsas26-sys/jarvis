"""training_gym/graders/base.py — V69 M62: the deterministic evidence contract.

WHY THIS EXISTS
---------------
A grader is the only thing in the gym that produces *evidence*. Teachers produce
opinions and humans produce decisions; a grader produces a measurement, and the whole
milestone rests on that measurement being real. So the protocol below is built around
one question a reviewer must always be able to answer: **what, exactly, did this check
look at?**

That question is why :attr:`~training_gym.trajectory.GraderResult.
non_vacuous_measurement` exists and why every grader here is routed through
:meth:`Grader.grade` rather than being called directly. ``grade`` is a template
method, and it is the place where the four ways a check can be theatre are closed:

  1. *It never ran.* An exception becomes ``ERROR`` with a sanitized reason, never a
     silent absence and never a default-pass.
  2. *It ran against nothing.* ``PASS`` with zero measurements is rewritten to
     ``INSUFFICIENT_EVIDENCE`` before it can reach the aggregator. The frozen
     ``GraderResult`` already refuses to construct such a record; this is the second
     lock, because the first one lives in a different file.
  3. *It answered about the wrong subject.* A result whose ``grader_id`` does not
     match the grader that produced it is refused.
  4. *Its tool was missing.* Absence is reported as ``SKIPPED`` or
     ``INSUFFICIENT_EVIDENCE`` — statuses that BLOCK a mandatory check — and never as
     ``PASS``.

WHAT A GRADER IS GIVEN, AND WHAT IT IS DENIED
---------------------------------------------
:class:`GraderContext` is a closed set of fields. There is deliberately no field for
the process environment, no field for a host path other than the workspace root it
must have in order to read anything, and no field for credentials. A grader that
wanted to read ``os.environ`` would have to reach for it explicitly, in a diff a
reviewer sees, rather than receiving it as an ambient gift.

EXTERNAL COMMANDS
-----------------
:func:`run_bounded` is the ONLY way a grader in this package starts a process. It
takes an argv list, never a string; ``shell=False`` is explicit; the working directory
is explicit; the timeout is mandatory; output is bounded on the way in rather than
trimmed after the fact; and on timeout it kills the process TREE, because a pytest run
that spawned children is not stopped by killing pytest. The environment is
constructed, not inherited: ``PATH`` and a fixed deterministic set, with no proxy
variable, no ``PYTHONPATH`` and no credential-shaped name able to reach the child.
"""
from __future__ import annotations

import os
import platform
import shutil
# subprocess is used exclusively through run_bounded(), which takes argv LISTS and
# passes shell=False explicitly. No command is ever built by string interpolation.
import subprocess  # nosec B404
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from ..policies import credential_shaped
from ..schemas import (
    ResultStatus,
    SchemaError,
    Severity,
    require_id,
    require_int,
)
from ..task_spec import GRADER_IDS, TaskFamily, TaskSpec
from ..trajectory import GraderResult, OutputSummary
from ..workspace import canonicalize_within, safe_relative_path

#: Bump when the protocol below changes shape. Recorded by every grader version
#: string so a verdict can be attributed to the contract that produced it.
GRADER_PROTOCOL_VERSION = "m62.grader.1"

#: Hard ceiling on captured stdout+stderr per command. A grader that needs more than
#: this to make its point is reporting a log, not a measurement.
MAX_GRADER_OUTPUT_BYTES = 262_144
#: Longest sanitized excerpt kept in a result's evidence.
MAX_EVIDENCE_CHARS = 2_000
#: Ceiling on structured findings carried in one result.
MAX_FINDINGS = 50
#: Default per-command wall clock. Never unbounded, and never inherited from a task
#: that could have asked for more than the gym's own ceiling.
DEFAULT_GRADER_TIMEOUT_S = 120
#: How long a ``--version`` probe may take before the tool is declared unusable.
PROBE_TIMEOUT_S = 10

_IS_WINDOWS = platform.system() == "Windows"


# ── external tooling ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolProbe:
    """Whether an external validator is usable HERE, and which one it is.

    ``available=False`` is a first-class answer, not an error. The rule the whole
    package obeys is that it can never become ``PASS``: a scanner that is not
    installed has proven nothing about the code it did not read.
    """

    name: str
    available: bool = False
    version: str = ""
    executable: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "available": self.available,
                "version": self.version, "reason": self.reason}


@dataclass(frozen=True)
class CommandOutcome:
    """One bounded external command, summarised rather than captured.

    ``exit_code=None`` means the command did not complete — it never started, or it
    was killed on timeout. That is deliberately not ``0``.
    """

    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    started: bool = False
    error: str = ""

    @property
    def succeeded(self) -> bool:
        """True only for a command that RAN and returned zero."""
        return self.started and not self.timed_out and self.exit_code == 0

    def to_dict(self) -> dict:
        return {"argv": list(self.argv), "exit_code": self.exit_code,
                "duration_ms": self.duration_ms, "timed_out": self.timed_out,
                "truncated": self.truncated, "started": self.started,
                "succeeded": self.succeeded, "error": self.error}


def grader_environment() -> dict[str, str]:
    """The environment an external grader command receives. Constructed, not inherited.

    ``PATH`` is carried over because a tool that cannot be found cannot be run, and
    ``SYSTEMROOT``/``COMSPEC`` because process creation on Windows fails without them.
    Everything else is a fixed deterministic value. Nothing credential-shaped, no
    ``PYTHONPATH`` (an import-hijack channel), and no ``*_PROXY`` (an egress channel)
    can reach the child, because neither is copied in the first place.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "NO_COLOR": "1",
        "PY_COLORS": "0",
    }
    for name in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    leaked = sorted(k for k in env if credential_shaped(k))
    if leaked:  # pragma: no cover — structural guard over a literal dict
        raise SchemaError(f"grader environment: credential-shaped name(s) {leaked}")
    return env


def _kill_tree(process: "subprocess.Popen") -> None:
    """Kill a timed-out command and everything it started.

    ``Popen.kill`` signals only the direct child. A pytest run that forked workers, or
    a linter that spawned a subprocess of its own, survives it — and a survivor holds
    the workspace open and keeps consuming the host. On POSIX the child is started in
    its own session so the whole group can be signalled; on Windows ``taskkill /T``
    walks the tree, and its path is resolved rather than trusted to ``PATH``.
    """
    if process.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            taskkill = shutil.which("taskkill")
            if taskkill:
                # Resolved absolute path, argv list, shell=False, fixed flags and a
                # numeric pid — nothing here is caller-controlled text.
                subprocess.run(  # nosec B603
                    [taskkill, "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True, timeout=PROBE_TIMEOUT_S, shell=False,
                    check=False)
            else:  # pragma: no cover — taskkill is present on every supported host
                process.kill()
        else:
            os.killpg(os.getpgid(process.pid), 9)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        try:
            process.kill()
        except OSError:  # pragma: no cover — the process is already gone
            pass


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Bound captured output. The full text is never retained by a grader."""
    raw = text or ""
    encoded = raw.encode("utf-8", "surrogatepass")
    if len(encoded) <= limit:
        return raw, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def run_bounded(argv: Sequence[str], *, cwd: str | Path,
                timeout_s: int = DEFAULT_GRADER_TIMEOUT_S,
                max_output_bytes: int = MAX_GRADER_OUTPUT_BYTES) -> CommandOutcome:
    """Run one external grader command under every bound this package requires.

    argv list, ``shell=False``, explicit working directory, mandatory timeout, bounded
    output, constructed environment, process-tree kill on timeout. A command that
    could not start is reported with ``started=False`` and ``exit_code=None`` — never
    as an exit code the caller might compare against zero.
    """
    if isinstance(argv, (str, bytes)):
        # ``list("ruff check .")`` is a list of single characters, every one of which
        # is a string — so the element check below would have accepted a shell string
        # as a well-formed argv. The type is rejected before it is iterated.
        raise SchemaError("grader command: argv must be a non-empty list of strings "
                          "(a shell string is never accepted)")
    items = list(argv)
    if not items or any(not isinstance(a, str) for a in items):
        raise SchemaError("grader command: argv must be a non-empty list of strings "
                          "(a shell string is never accepted)")
    require_int(timeout_s, "grader command: timeout_s", minimum=1, maximum=900)
    require_int(max_output_bytes, "grader command: max_output_bytes",
                minimum=1024, maximum=MAX_GRADER_OUTPUT_BYTES)
    workdir = Path(cwd)
    if not workdir.is_dir():
        raise SchemaError(f"grader command: working directory does not exist "
                          f"({workdir.name!r})")

    creation: dict[str, object] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                   if _IS_WINDOWS else {"start_new_session": True})
    started_at = time.monotonic()
    try:
        # argv is a validated list of strings, shell=False is explicit, the executable
        # is resolved by the caller through probe_tool(), the cwd is the episode
        # workspace and the environment is built by grader_environment(). Asserted by
        # the grader argv negative controls.
        process = subprocess.Popen(  # nosec B603
            items, cwd=str(workdir), env=grader_environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", shell=False,
            **creation)  # type: ignore[arg-type]
    except (OSError, ValueError) as exc:
        return CommandOutcome(argv=tuple(items), started=False,
                              duration_ms=int((time.monotonic() - started_at) * 1000),
                              error=f"{type(exc).__name__}: command could not start")

    timed_out = False
    try:
        out, err = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        try:
            out, err = process.communicate(timeout=PROBE_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out, err = "", ""
    except (OSError, ValueError) as exc:  # pragma: no cover — pipe failure
        _kill_tree(process)
        return CommandOutcome(argv=tuple(items), started=True,
                              duration_ms=int((time.monotonic() - started_at) * 1000),
                              error=f"{type(exc).__name__}: output could not be read")

    stdout, out_clipped = _clip(out or "", max_output_bytes)
    stderr, err_clipped = _clip(err or "", max_output_bytes)
    return CommandOutcome(
        argv=tuple(items),
        exit_code=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        timed_out=timed_out,
        truncated=out_clipped or err_clipped,
        started=True,
        error=f"command exceeded {timeout_s}s and its process tree was killed"
              if timed_out else "",
    )


def probe_tool(name: str, *, version_argv: Sequence[str] = ("--version",),
               cwd: str | Path | None = None) -> ToolProbe:
    """Resolve an external validator and record its version, or report its absence.

    Never installs, never downloads, never falls back to a different tool. The three
    outcomes are: present and identified, present but unusable, absent — and only the
    first may lead to a PASS.
    """
    tool = str(name or "").strip()
    if not tool:
        raise SchemaError("probe_tool: a tool name is required")
    executable = shutil.which(tool)
    if not executable:
        return ToolProbe(name=tool, available=False,
                         reason=f"{tool} was not found on PATH; the gym never installs "
                                f"a missing validator")
    workdir = Path(cwd) if cwd else Path.cwd()
    try:
        outcome = run_bounded([executable, *version_argv], cwd=workdir,
                              timeout_s=PROBE_TIMEOUT_S, max_output_bytes=8192)
    except SchemaError as exc:  # pragma: no cover — workdir is validated by callers
        return ToolProbe(name=tool, available=False, reason=str(exc))
    if not outcome.started or outcome.timed_out or outcome.exit_code != 0:
        return ToolProbe(name=tool, available=False, executable=executable,
                         reason=f"{tool} is installed but did not answer a version "
                                f"probe")
    banner = (outcome.stdout or outcome.stderr or "").strip().splitlines()
    return ToolProbe(name=tool, available=True, executable=executable,
                     version=(banner[0][:120] if banner else ""))


# ── what a grader may see ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ChangedFile:
    """One file an attempt touched, described in workspace-relative terms only."""

    path: str
    status: str = "modified"          # added | modified | deleted | renamed
    added_lines: int = 0
    removed_lines: int = 0

    _STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"added", "modified", "deleted", "renamed"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "path",
                           safe_relative_path(self.path, "changed_file.path").as_posix())
        status = str(self.status or "").strip().lower()
        if status not in self._STATUSES:
            raise SchemaError(f"changed_file.status: unknown status {self.status!r}; "
                              f"expected one of {sorted(self._STATUSES)}")
        object.__setattr__(self, "status", status)
        require_int(self.added_lines, "changed_file.added_lines",
                    minimum=0, maximum=10_000_000)
        require_int(self.removed_lines, "changed_file.removed_lines",
                    minimum=0, maximum=10_000_000)

    @property
    def churn(self) -> int:
        return self.added_lines + self.removed_lines

    def to_dict(self) -> dict:
        return {"path": self.path, "status": self.status,
                "added_lines": self.added_lines, "removed_lines": self.removed_lines}


@dataclass(frozen=True)
class GraderContext:
    """Everything a grader is allowed to see, and nothing else.

    The field list IS the access-control policy. There is no environment mapping, no
    operator path other than the workspace root a grader must have in order to read
    the files it grades, and no credential store. :meth:`to_public_dict` — the form
    that may appear in a record — omits the root entirely, so a context can be
    described in an exportable artifact without describing the host.
    """

    spec: TaskSpec
    attempt_id: str
    workspace_root: Path | None = None
    answer: str = ""
    structured_output: object | None = None
    proposed_tool_calls: tuple[Mapping[str, object], ...] = ()
    input_hashes: Mapping[str, str] = field(default_factory=dict)
    output_hashes: Mapping[str, str] = field(default_factory=dict)
    changed_files: tuple[ChangedFile, ...] = ()
    diff_text: str = ""
    evidence_ids: frozenset[str] = frozenset()
    baseline: Mapping[str, object] = field(default_factory=dict)
    tool_catalog: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    #: Workspace-relative directories the pytest grader may collect from.
    test_paths: tuple[str, ...] = ()
    #: Workspace-relative directories the lint / security scanners may read.
    source_paths: tuple[str, ...] = ()
    #: Workspace-relative paths an attempt is permitted to modify.
    editable_paths: tuple[str, ...] = ()
    refused: bool = False
    timeout_s: int = DEFAULT_GRADER_TIMEOUT_S

    def __post_init__(self) -> None:
        if not isinstance(self.spec, TaskSpec):
            raise SchemaError("grader context: spec must be a TaskSpec")
        require_id(self.attempt_id, "grader context: attempt_id")
        if self.workspace_root is not None:
            root = Path(self.workspace_root)
            if not root.is_absolute():
                root = root.resolve()
            object.__setattr__(self, "workspace_root", root)
        for name in ("test_paths", "source_paths", "editable_paths"):
            values = tuple(getattr(self, name))
            for value in values:
                safe_relative_path(value, f"grader context: {name}[]")
            object.__setattr__(self, name, values)
        for call in self.proposed_tool_calls:
            if not isinstance(call, Mapping):
                raise SchemaError("grader context: every proposed tool call must be a "
                                  "structured object, never a free-text blob")
        object.__setattr__(self, "evidence_ids",
                           frozenset(str(e) for e in self.evidence_ids))
        require_int(self.timeout_s, "grader context: timeout_s", minimum=1, maximum=900)

    # -- derived ---------------------------------------------------------------
    @property
    def family(self) -> TaskFamily:
        return self.spec.task_family

    @property
    def mandatory_graders(self) -> frozenset[str]:
        return frozenset(self.spec.scoring.mandatory_graders)

    def is_mandatory(self, grader_id: str) -> bool:
        return grader_id in self.mandatory_graders

    def has_workspace(self) -> bool:
        return self.workspace_root is not None and self.workspace_root.is_dir()

    def resolve(self, relative: str, field_name: str = "path") -> Path:
        """Resolve a workspace-relative path and PROVE it stayed inside.

        Delegates to the frozen workspace authority rather than re-implementing
        containment — a second, weaker path validator is exactly how a boundary
        develops a hole."""
        if self.workspace_root is None:
            raise SchemaError(f"{field_name}: no workspace is attached to this context")
        return canonicalize_within(self.workspace_root, relative, field_name)

    def sanitize(self, text: str, *, limit: int = MAX_EVIDENCE_CHARS) -> str:
        """Strip the workspace root, then redact, then bound.

        The root is removed FIRST because it is the one private path guaranteed to
        appear in tool output — a pytest failure report names the directory it ran in
        — and the generic redactor has no way to know which temporary directory
        belonged to this episode."""
        raw = str(text or "")
        if self.workspace_root is not None:
            for form in _root_spellings(self.workspace_root):
                if form:
                    raw = raw.replace(form, "<workspace>")
        return OutputSummary.capture(raw, limit=limit).excerpt

    def to_public_dict(self) -> dict:
        """A description of this context that carries no host path."""
        return {
            "task_id": self.spec.task_id,
            "task_family": self.spec.task_family.value,
            "attempt_id": self.attempt_id,
            "has_workspace": self.has_workspace(),
            "answer_chars": len(self.answer),
            "structured_output": self.structured_output is not None,
            "proposed_tool_calls": len(self.proposed_tool_calls),
            "input_files": sorted(self.input_hashes),
            "output_files": sorted(self.output_hashes),
            "changed_files": [c.to_dict() for c in self.changed_files],
            "diff_chars": len(self.diff_text),
            "evidence_ids": sorted(self.evidence_ids),
            "test_paths": list(self.test_paths),
            "source_paths": list(self.source_paths),
            "editable_paths": list(self.editable_paths),
            "refused": self.refused,
        }


def _root_spellings(root: Path) -> tuple[str, ...]:
    """Every textual form a workspace root can appear as in captured output."""
    forms: list[str] = []
    for candidate in (root, root.resolve()):
        text = str(candidate)
        forms.extend((text, text.replace("\\", "/"), text.replace("\\", "\\\\")))
    # Longest first: replacing a short prefix first would leave the longer form
    # partially rewritten and therefore unmatched.
    return tuple(sorted({f for f in forms if f}, key=len, reverse=True))


# ── result constructors ───────────────────────────────────────────────────────
def make_result(grader: "Grader", status: ResultStatus, *, score: float = 0.0,
                blocking: bool = False, severity: Severity = Severity.INFO,
                evidence: Sequence[str] = (), findings: Sequence[Mapping] = (),
                tool_version: str = "", measured: int = 0,
                error: str = "") -> GraderResult:
    """Build a normalised :class:`GraderResult` for *grader*.

    Every grader in this package returns through here, so no grader can invent a
    different record shape and no grader can forget its own id or version.
    """
    return GraderResult(
        grader_id=grader.grader_id,
        grader_version=grader.grader_version,
        status=status,
        score=float(score),
        blocking=bool(blocking),
        severity=severity,
        evidence=tuple(str(e) for e in evidence)[:MAX_FINDINGS],
        findings=tuple(dict(f) for f in findings)[:MAX_FINDINGS],
        tool_version=tool_version,
        non_vacuous_measurement=int(measured),
        error=error,
    )


def skipped(grader: "Grader", reason: str, *, tool_version: str = "") -> GraderResult:
    """A check that could not run. Blocks a mandatory grader; never a pass."""
    return make_result(grader, ResultStatus.SKIPPED, evidence=(reason,),
                       tool_version=tool_version, measured=0)


def insufficient(grader: "Grader", reason: str, *, measured: int = 0,
                 tool_version: str = "",
                 findings: Sequence[Mapping] = ()) -> GraderResult:
    """A check that ran but measured nothing conclusive. Never a pass."""
    return make_result(grader, ResultStatus.INSUFFICIENT_EVIDENCE, evidence=(reason,),
                       findings=findings, tool_version=tool_version, measured=measured)


def errored(grader: "Grader", reason: str) -> GraderResult:
    """A check that failed to produce a verdict. Never a pass."""
    return make_result(grader, ResultStatus.ERROR, evidence=(reason,),
                       error=reason or "grader failed without a reason")


def not_applicable(grader: "Grader", reason: str) -> GraderResult:
    """The one honest non-measurement: this rule does not apply to this task."""
    return make_result(grader, ResultStatus.NOT_APPLICABLE, evidence=(reason,))


def blocking_failure(grader: "Grader", reason: str, *, measured: int,
                     findings: Sequence[Mapping] = (),
                     severity: Severity = Severity.BLOCKING) -> GraderResult:
    """A security failure that must dominate every positive dimension."""
    return make_result(grader, ResultStatus.FAIL, score=0.0, blocking=True,
                       severity=severity, evidence=(reason,), findings=findings,
                       measured=measured)


# ── the protocol ──────────────────────────────────────────────────────────────
class Grader(ABC):
    """One deterministic check. Subclasses implement :meth:`measure`, never ``grade``.

    The class attributes are the declaration a task and an aggregator read:

      ``grader_id``          one of the frozen :data:`~training_gym.task_spec.
                             GRADER_IDS`; validated when the subclass is defined, so a
                             misspelt id is an import error rather than a check that
                             silently never applies.
      ``grader_version``     recorded in every result.
      ``supported_families`` the task families this check means something for.
                             Anything else is ``NOT_APPLICABLE``, which is honest —
                             and which the aggregator still refuses to treat as
                             evidence when the task made the grader mandatory.
      ``required_tool``      the external validator this grader needs, or ``""``.
    """

    grader_id: ClassVar[str] = ""
    grader_version: ClassVar[str] = GRADER_PROTOCOL_VERSION
    supported_families: ClassVar[frozenset[TaskFamily]] = frozenset(TaskFamily)
    required_tool: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        if cls.grader_id not in GRADER_IDS:
            raise SchemaError(
                f"{cls.__name__}: grader_id {cls.grader_id!r} is not one of the frozen "
                f"ids {list(GRADER_IDS)}; a grader nobody can request is a check that "
                f"silently never runs")
        if not str(cls.grader_version or "").strip():
            raise SchemaError(f"{cls.__name__}: grader_version is required")
        if not cls.supported_families:
            raise SchemaError(f"{cls.__name__}: supported_families is empty; a grader "
                              f"that applies to nothing is not a grader")

    # -- declaration -----------------------------------------------------------
    def availability(self, ctx: GraderContext) -> ToolProbe:
        """Whether this grader's external validator is usable on this host."""
        if not self.required_tool:
            return ToolProbe(name="", available=True,
                             version=self.grader_version, reason="")
        cwd = ctx.workspace_root if ctx.has_workspace() else None
        return probe_tool(self.required_tool, cwd=cwd)

    def applies_to(self, ctx: GraderContext) -> bool:
        return ctx.family in self.supported_families

    # -- measurement -----------------------------------------------------------
    @abstractmethod
    def measure(self, ctx: GraderContext) -> GraderResult:
        """Produce the verdict. Implemented by every concrete grader."""

    def grade(self, ctx: GraderContext) -> GraderResult:
        """Run :meth:`measure` under the protocol's guarantees. Never overridden.

        This is where a grader stops being trusted to be honest and starts being
        *made* honest: an exception is an ERROR, an unmeasured PASS is downgraded to
        INSUFFICIENT_EVIDENCE, and a result about a different grader is refused.
        """
        if not isinstance(ctx, GraderContext):
            raise SchemaError(f"{type(self).__name__}.grade: expected a GraderContext")
        if not self.applies_to(ctx):
            return not_applicable(
                self, f"{self.grader_id} does not apply to task family "
                      f"{ctx.family.value}")
        started = time.monotonic()
        try:
            result = self.measure(ctx)
        except SchemaError as exc:
            result = errored(self, f"{self.grader_id}: {ctx.sanitize(str(exc), limit=400)}")
        except Exception as exc:  # noqa: BLE001 — a crashed grader proved nothing
            result = errored(self, f"{self.grader_id}: {type(exc).__name__} during "
                                   f"measurement")
        if not isinstance(result, GraderResult):
            return errored(self, f"{self.grader_id}: returned "
                                 f"{type(result).__name__}, not a GraderResult; a raw "
                                 f"dictionary is never an authoritative verdict")
        if result.grader_id != self.grader_id:
            return errored(self, f"{self.grader_id}: produced a result labelled "
                                 f"{result.grader_id!r}; refusing a verdict about a "
                                 f"different subject")
        if result.status is ResultStatus.PASS and result.non_vacuous_measurement <= 0:
            # Unreachable through GraderResult's own constructor, which already
            # refuses this. Kept because that guard lives in another module and this
            # one is the last thing between a grader and the aggregator.
            result = replace(  # pragma: no cover
                result, status=ResultStatus.INSUFFICIENT_EVIDENCE, score=0.0,
                evidence=(*result.evidence, "downgraded: PASS with zero measurements"))
        return replace(result,
                       duration_ms=max(result.duration_ms,
                                       int((time.monotonic() - started) * 1000)))

    # -- shared helpers --------------------------------------------------------
    def unavailable(self, ctx: GraderContext, probe: ToolProbe) -> GraderResult:
        """The result for a missing validator: SKIPPED, or blocking when mandatory.

        Both are non-affirmative and both block a mandatory check. The distinction is
        for the operator reading the report: a task that made this grader mandatory
        gets an INSUFFICIENT_EVIDENCE saying the required evidence is unobtainable
        here, rather than a SKIPPED that reads like a choice.
        """
        reason = probe.reason or f"{self.required_tool or 'validator'} is unavailable"
        if ctx.is_mandatory(self.grader_id):
            return insufficient(
                self, f"{reason}; this task requires {self.grader_id} and the evidence "
                      f"cannot be produced on this host")
        return skipped(self, reason)

    def require_workspace(self, ctx: GraderContext) -> GraderResult | None:
        """``None`` when a workspace is present, or the fail-closed result when not."""
        if ctx.has_workspace():
            return None
        return insufficient(self, f"{self.grader_id}: no workspace is attached, so no "
                                  f"file was examined")

    def allowlisted_targets(self, ctx: GraderContext, relatives: Sequence[str], *,
                            field_name: str) -> tuple[Path, ...]:
        """Resolve an allowlist of workspace-relative paths that actually exist.

        Every entry is proven to be inside the workspace by the frozen authority
        before it can become an argv element, so a task-supplied path can never turn
        a scanner loose on the operator's filesystem.
        """
        resolved: list[Path] = []
        for rel in relatives:
            target = ctx.resolve(rel, field_name)
            if target.exists():
                resolved.append(target)
        return tuple(resolved)


__all__ = [
    "DEFAULT_GRADER_TIMEOUT_S", "GRADER_PROTOCOL_VERSION", "MAX_EVIDENCE_CHARS",
    "MAX_FINDINGS", "MAX_GRADER_OUTPUT_BYTES", "PROBE_TIMEOUT_S", "ChangedFile",
    "CommandOutcome", "Grader", "GraderContext", "ToolProbe", "blocking_failure",
    "errored", "grader_environment", "insufficient", "make_result", "not_applicable",
    "probe_tool", "run_bounded", "skipped",
]
