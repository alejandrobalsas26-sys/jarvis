"""training_gym/sandbox/base.py — V69 M62: the contract every sandbox must satisfy.

A backend is anything that can prepare an isolated workspace, run bounded commands in
it, hand back artifacts and then destroy itself. The protocol is deliberately small,
and two of its seven methods exist purely so a backend cannot overstate itself:
:meth:`capability_report` says what it can actually do on this host, and
:meth:`security_report` says what posture it is actually enforcing — including an
audit of the launch command it built, not a restatement of the policy it was given.

``cleanup`` is separate from ``terminate`` on purpose. Terminating stops execution;
cleanup destroys the workspace. An episode that failed must still be cleaned, so
cleanup is idempotent, never raises, and is safe to call on a sandbox that was never
prepared.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..schemas import SchemaError
from ..task_spec import TaskSpec
from .result import (
    ExecutionResult,
    PreparedSandbox,
    SandboxCapabilityReport,
    SandboxSecurityReport,
)


class SandboxError(SchemaError):
    """A sandbox refused to do something. Fail-closed, never a warning."""


class SandboxUnavailable(SandboxError):
    """The backend cannot run here. Reported honestly; never silently substituted."""


@runtime_checkable
class SandboxBackend(Protocol):
    """The seven-method contract. Implementations live beside this file."""

    name: str

    def prepare(self, task: TaskSpec) -> PreparedSandbox:
        """Validate the task, create the workspace, stage fixtures, build the command."""
        ...

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        """Run one bounded command. Never a shell string, never interpolated."""
        ...

    def collect_artifacts(self) -> dict[str, str]:
        """``{relative path: sha256}`` for files the allowlist permits out."""
        ...

    def terminate(self) -> None:
        """Stop execution. Idempotent."""
        ...

    def cleanup(self) -> None:
        """Destroy the workspace. Idempotent, unconditional, never raises."""
        ...

    def capability_report(self) -> SandboxCapabilityReport:
        """What this backend can actually do on this host."""
        ...

    def security_report(self) -> SandboxSecurityReport:
        """The posture actually in force, including the launch-command audit."""
        ...


def require_backend(candidate: object) -> SandboxBackend:
    """Refuse anything that does not implement the whole contract.

    A partial backend is worse than none: the missing method is discovered at the
    moment it is needed, which is usually mid-episode with a workspace already open.
    """
    missing = [m for m in ("prepare", "execute", "collect_artifacts", "terminate",
                           "cleanup", "capability_report", "security_report")
               if not callable(getattr(candidate, m, None))]
    if missing:
        raise SandboxError(f"{type(candidate).__name__} is not a sandbox backend; "
                           f"missing {missing}")
    return candidate  # type: ignore[return-value]


__all__ = ["SandboxBackend", "SandboxError", "SandboxUnavailable", "require_backend"]
