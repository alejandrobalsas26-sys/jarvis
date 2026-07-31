"""training_gym/sandbox/dry_run_backend.py — V69 M62: plan everything, run nothing.

WHY THIS EXISTS
---------------
Most of what can go wrong in an episode goes wrong before any code executes: a task
whose policy is malformed, an artifact allowlist that would collect the workspace, a
fixture path that escapes, a launch command that quietly dropped a security flag. All
of that is checkable without a container, and checking it without one is what lets the
gym's negative controls run in CI on a machine with no Docker daemon, no GPU and no
network.

THE ONE RULE
------------
This backend NEVER fabricates a success. :meth:`execute` returns a result with
``executed=False`` and ``exit_code=None``, so :attr:`ExecutionResult.succeeded` is
``False`` by construction — there is no argument, no flag and no task configuration
that makes a dry run report that a command passed. Its capability report says
``executes_commands=False``, and any grader reading it must record SKIPPED or
INSUFFICIENT_EVIDENCE rather than PASS.

What it DOES produce is real: the fully-built container argv, the resolved
environment, the staged input hashes, and the security audit of that command. Those
are genuine measurements of the plan, and they are the ones the sandbox negative
controls assert against.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..policies import MountSpec
from ..task_spec import TaskSpec
from ..workspace import EpisodeWorkspace
from .base import SandboxError
from .result import (
    ExecutionResult,
    PreparedSandbox,
    SandboxCapabilityReport,
    SandboxSecurityReport,
)
from .security import (
    CONTAINER_WORKSPACE,
    audit_command,
    build_environment,
    build_run_command,
    describe_command,
)

DEFAULT_GYM_IMAGE = "python:3.12-slim-bookworm"


class DryRunSandboxBackend:
    """Validates a task and reports exactly what would run. Executes nothing."""

    name = "dry_run"

    def __init__(self, *, image: str = DEFAULT_GYM_IMAGE,
                 entry_argv: Sequence[str] = ("python", "-c", "pass"),
                 mounts: Sequence[MountSpec] = ()) -> None:
        self.image = image
        self.entry_argv = tuple(entry_argv)
        self.mounts = tuple(mounts)
        self._task: TaskSpec | None = None
        self._workspace: EpisodeWorkspace | None = None
        self._command: tuple[str, ...] = ()
        self._audit: tuple[str, ...] = ()
        self._planned: list[tuple[str, ...]] = []

    # -- contract ---------------------------------------------------------------
    def prepare(self, task: TaskSpec) -> PreparedSandbox:
        """Everything a real run would do, up to but not including execution."""
        self._task = task
        self._workspace = EpisodeWorkspace(label=task.task_id, artifacts=task.artifacts)
        self._workspace.create()
        command = build_run_command(
            image=self.image,
            argv=self.entry_argv,
            policy=task.sandbox,
            resources=task.resources,
            network=task.network,
            mounts=self.mounts,
            container_name=f"gym-{task.task_id}",
        )
        self._command = tuple(command)
        # Re-audit the command we just built, and RECORD the result rather than
        # asserting it: a security report that cannot show its working is a claim.
        self._audit = audit_command(command)
        return PreparedSandbox(
            episode_id=task.task_id,
            backend=self.name,
            workspace_label=self._workspace.label,
            launch_command=self._command,
            input_hashes={f.path: f.sha256 for f in task.fixtures},
            security=self.security_report(),
            ready=not self._audit,
        )

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        """Record the intent. Run nothing. Report, unambiguously, that nothing ran."""
        if self._task is None:
            raise SandboxError("dry run: prepare() has not been called")
        if not argv or any(not isinstance(a, str) for a in argv):
            raise SandboxError("dry run: argv must be a non-empty list of strings")
        planned = tuple(str(a) for a in argv)
        self._planned.append(planned)
        return ExecutionResult(
            argv=planned,
            exit_code=None,          # never 0 — a plan is not a pass
            executed=False,
            stdout="",
            stderr="",
            error="dry run: no command was executed",
        )

    def collect_artifacts(self) -> dict[str, str]:
        """A dry run produced no outputs, and says so with an empty mapping."""
        return {}

    def terminate(self) -> None:
        return None

    def cleanup(self) -> None:
        if self._workspace is not None:
            self._workspace.destroy()
            self._workspace = None

    def capability_report(self) -> SandboxCapabilityReport:
        return SandboxCapabilityReport(
            backend=self.name,
            version="m62.1",
            available=True,
            executes_commands=False,
            enforces_isolation=False,
            unavailable_reason="",
            notes=("validates task, policy, workspace and launch command",
                   "executes nothing; results may never be read as evidence of "
                   "execution",
                   "usable in CI without a Docker daemon"),
        )

    def security_report(self) -> SandboxSecurityReport:
        task = self._task
        return SandboxSecurityReport(
            backend=self.name,
            isolation_boundary="none — nothing is executed",
            network_policy=task.network.policy.value if task else "none",
            read_only_rootfs=task.sandbox.read_only_rootfs if task else True,
            capabilities_dropped=task.sandbox.drop_all_capabilities if task else True,
            no_new_privileges=task.sandbox.no_new_privileges if task else True,
            runs_as_root=False,
            mounts=tuple(m.to_dict() for m in self.mounts),
            command_audit=self._audit,
            audited=bool(self._command) and not self._audit,
            caveats=("no isolation is in force because no process is started",
                     "the audited command is the one a Docker run WOULD use"),
        )

    # -- dry-run specific --------------------------------------------------------
    def execution_plan(self) -> dict:
        """The exact plan, for an operator to read before authorising a real run."""
        task = self._task
        if task is None:
            raise SandboxError("dry run: prepare() has not been called")
        return {
            "task_id": task.task_id,
            "task_hash": task.spec_hash(),
            "image": self.image,
            "launch_command": list(self._command),
            "launch_command_display": describe_command(self._command),
            "container_workspace": CONTAINER_WORKSPACE,
            "environment": build_environment(task.sandbox),
            "network_policy": task.network.policy.value,
            "network_destinations": list(task.network.destinations),
            "resources": task.resources.to_dict(),
            "artifact_patterns": list(task.artifacts.patterns),
            "planned_commands": [list(c) for c in self._planned],
            "mounts": [m.to_dict() for m in self.mounts],
            "command_audit": list(self._audit),
            "would_execute": False,
        }


__all__ = ["DEFAULT_GYM_IMAGE", "DryRunSandboxBackend"]
