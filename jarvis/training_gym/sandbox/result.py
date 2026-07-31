"""training_gym/sandbox/result.py — V69 M62: what a sandbox reports back.

Three records, and one rule that shapes all of them: a sandbox must never be able to
report a success it did not observe.

:class:`ExecutionResult` therefore has no boolean ``ok`` that a caller could set
optimistically. ``succeeded`` is DERIVED from an exit code that was actually
collected, and a result whose command never ran has ``exit_code=None``, which is not
zero and never compares equal to it. :class:`SandboxCapabilityReport` distinguishes
"this backend can do X" from "this backend pretended to do X", which is what lets a
dry run be useful without being a lie.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import SCHEMA_KEY, SCHEMA_VERSION, SchemaError, require_int


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of one command inside the sandbox.

    ``exit_code=None`` means the command did not run to completion — it was never
    started, it timed out, or the backend simulated it. That is deliberately NOT
    ``0``: a caller writing ``if result.exit_code == 0`` must not see a success that
    never happened.
    """

    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated_output: bool = False
    executed: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        require_int(self.duration_ms, "result.duration_ms", minimum=0,
                    maximum=86_400_000)
        if self.executed and self.exit_code is None and not self.timed_out:
            raise SchemaError("result: marked executed but collected no exit code")
        if not self.executed and self.exit_code is not None:
            raise SchemaError("result: reported an exit code for a command that was "
                              "never executed")

    @property
    def succeeded(self) -> bool:
        """True only for a command that RAN and returned zero."""
        return self.executed and not self.timed_out and self.exit_code == 0

    def to_dict(self) -> dict:
        return {"argv": list(self.argv), "exit_code": self.exit_code,
                "duration_ms": self.duration_ms, "timed_out": self.timed_out,
                "truncated_output": self.truncated_output,
                "executed": self.executed, "succeeded": self.succeeded,
                "error": self.error,
                "stdout_bytes": len(self.stdout.encode("utf-8", "surrogatepass")),
                "stderr_bytes": len(self.stderr.encode("utf-8", "surrogatepass"))}


@dataclass(frozen=True)
class SandboxCapabilityReport:
    """What this backend can actually do on THIS host, right now.

    ``executes_commands`` is the honesty flag. A backend that returns ``False`` here
    may still be useful — a dry run validates a task, resolves a policy and reports
    the exact command that would run — but nothing downstream may treat its output as
    evidence that code was executed.
    """

    backend: str
    version: str = ""
    available: bool = False
    executes_commands: bool = False
    enforces_isolation: bool = False
    supports_network_policy: bool = True
    unavailable_reason: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "backend": self.backend,
                "version": self.version, "available": self.available,
                "executes_commands": self.executes_commands,
                "enforces_isolation": self.enforces_isolation,
                "supports_network_policy": self.supports_network_policy,
                "unavailable_reason": self.unavailable_reason,
                "notes": list(self.notes)}


@dataclass(frozen=True)
class SandboxSecurityReport:
    """The posture a prepared sandbox is actually running under.

    ``command_audit`` holds the result of re-reading the generated launch command.
    An empty tuple means the auditor found nothing; it is recorded either way, so a
    trajectory can prove the audit ran rather than asserting it did.
    """

    backend: str
    isolation_boundary: str
    network_policy: str = "none"
    read_only_rootfs: bool = True
    capabilities_dropped: bool = True
    no_new_privileges: bool = True
    runs_as_root: bool = False
    mounts: tuple[dict, ...] = ()
    command_audit: tuple[str, ...] = ()
    audited: bool = False
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.audited and self.command_audit:
            raise SchemaError(f"security report: audit found {list(self.command_audit)} "
                              f"but the sandbox is marked audited-clean")

    @property
    def safe(self) -> bool:
        return self.audited and not self.command_audit and not self.runs_as_root

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "backend": self.backend,
                "isolation_boundary": self.isolation_boundary,
                "network_policy": self.network_policy,
                "read_only_rootfs": self.read_only_rootfs,
                "capabilities_dropped": self.capabilities_dropped,
                "no_new_privileges": self.no_new_privileges,
                "runs_as_root": self.runs_as_root,
                "mounts": [dict(m) for m in self.mounts],
                "command_audit": list(self.command_audit),
                "audited": self.audited, "safe": self.safe,
                "caveats": list(self.caveats)}


@dataclass
class PreparedSandbox:
    """A sandbox that has been set up and is ready to execute."""

    episode_id: str
    backend: str
    workspace_label: str
    launch_command: tuple[str, ...] = ()
    input_hashes: dict[str, str] = field(default_factory=dict)
    security: SandboxSecurityReport | None = None
    ready: bool = False

    def to_dict(self) -> dict:
        return {"episode_id": self.episode_id, "backend": self.backend,
                "workspace_label": self.workspace_label,
                "launch_command": list(self.launch_command),
                "input_hashes": dict(sorted(self.input_hashes.items())),
                "security": self.security.to_dict() if self.security else None,
                "ready": self.ready}


__all__ = ["ExecutionResult", "PreparedSandbox", "SandboxCapabilityReport",
           "SandboxSecurityReport"]
