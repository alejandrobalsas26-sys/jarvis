"""training_gym/sandbox/docker_backend.py — V69 M62: the real isolated backend.

WHAT THIS DEFENDS AGAINST, AND WHAT IT DOES NOT
-----------------------------------------------
Every command runs in a fresh container that is destroyed afterwards, as an
unprivileged user, with all Linux capabilities dropped, ``no-new-privileges`` set, a
read-only root filesystem, a tmpfs workspace, no network by default, and hard
process/memory/CPU/file-size/wall-clock ceilings. Fixtures are COPIED into a
disposable workspace, never mounted from the repository. Nothing from the operator's
home directory, credential stores, SSH agent, cloud metadata or Docker control socket
is reachable.

That is a strong boundary against a badly-behaved fixture, a runaway process and
accidental damage to the host.

It is NOT a multi-tenant isolation boundary. A container shares the host kernel; a
kernel or runtime vulnerability is a host compromise. This is a locked-down container,
not a microVM, and ``docs/TRAINING_GYM_SECURITY.md`` says so in the same words.

HONEST UNAVAILABILITY
---------------------
When no Docker daemon is reachable this backend reports
``available=False`` with the reason, and :meth:`prepare` raises
:class:`~training_gym.sandbox.base.SandboxUnavailable`. It never silently downgrades
to a dry run and never returns a fabricated success — a caller that wants a dry run
must ask for one, because a substituted backend is how "the tests passed" comes to
mean "the tests never ran".
"""
from __future__ import annotations

import shutil
# subprocess is used exclusively with argv LISTS and shell=False; see _run() below.
import subprocess  # nosec B404
import time
import uuid
from collections.abc import Sequence

from ..policies import MountSpec
from ..schemas import sha256_text
from ..task_spec import TaskSpec
from ..workspace import EpisodeWorkspace
from .base import SandboxError, SandboxUnavailable
from .result import (
    ExecutionResult,
    PreparedSandbox,
    SandboxCapabilityReport,
    SandboxSecurityReport,
)
from .security import (
    CONTAINER_WORKSPACE,
    audit_command,
    build_run_command,
)

DEFAULT_GYM_IMAGE = "python:3.12-slim-bookworm"

#: How long the availability probe may take before the daemon is declared absent.
PROBE_TIMEOUT_S = 8


def _run(argv: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run an argv LIST with no shell, ever.

    ``shell=False`` is the default and is passed explicitly so the property is visible
    at every call site. Nothing here is built by string interpolation: the container
    command comes from :func:`~training_gym.sandbox.security.build_run_command`, which
    audits its own output before returning it.
    """
    # argv is always a list, shell=False is explicit, and no element is built by
    # string interpolation: the container command comes from build_run_command(),
    # which audits its own output. Asserted by the sandbox argv negative controls.
    return subprocess.run(  # nosec B603
        list(argv), capture_output=True, text=True, timeout=timeout,
        shell=False, check=False, encoding="utf-8", errors="replace")


def docker_available() -> tuple[bool, str, str]:
    """``(available, version, reason_if_not)``. Never raises, never assumes."""
    executable = shutil.which("docker")
    if not executable:
        return False, "", "docker executable not found on PATH"
    try:
        probe = _run([executable, "version", "--format", "{{.Server.Version}}"],
                     timeout=PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, "", f"docker probe failed: {type(exc).__name__}"
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        return False, "", f"docker daemon unreachable: {detail[0] if detail else '?'}"
    return True, (probe.stdout or "").strip(), ""


class DockerSandboxBackend:
    """One disposable container per episode. Destroyed unconditionally afterwards."""

    name = "docker"

    def __init__(self, *, image: str = DEFAULT_GYM_IMAGE,
                 mounts: Sequence[MountSpec] = (),
                 proxy_network: str = "") -> None:
        self.image = image
        self.mounts = tuple(mounts)
        self.proxy_network = proxy_network
        self._task: TaskSpec | None = None
        self._workspace: EpisodeWorkspace | None = None
        self._container: str = ""
        self._command: tuple[str, ...] = ()
        self._audit: tuple[str, ...] = ()
        self._started = False

    # -- contract ---------------------------------------------------------------
    def prepare(self, task: TaskSpec) -> PreparedSandbox:
        available, _version, reason = docker_available()
        if not available:
            raise SandboxUnavailable(
                f"docker sandbox unavailable: {reason}. Run with --sandbox dry-run to "
                f"validate the task without executing it; results from a dry run are "
                f"never evidence that a command succeeded.")

        self._task = task
        self._container = f"gym-{task.task_id}-{uuid.uuid4().hex[:8]}"
        self._workspace = EpisodeWorkspace(label=task.task_id, artifacts=task.artifacts)
        self._workspace.create()

        command = build_run_command(
            image=self.image,
            argv=("sleep", str(task.resources.timeout_s)),
            policy=task.sandbox,
            resources=task.resources,
            network=task.network,
            mounts=self.mounts,
            container_name=self._container,
            proxy_network=self.proxy_network,
        )
        self._command = tuple(command)
        self._audit = audit_command(command)
        if self._audit:
            self.cleanup()
            raise SandboxError("refusing to start an unsafe container: "
                               + "; ".join(self._audit))
        return PreparedSandbox(
            episode_id=task.task_id,
            backend=self.name,
            workspace_label=self._workspace.label,
            launch_command=self._command,
            input_hashes={f.path: f.sha256 for f in task.fixtures},
            security=self.security_report(),
            ready=True,
        )

    def execute(self, argv: Sequence[str]) -> ExecutionResult:
        """Run one bounded command inside the prepared container."""
        if self._task is None or not self._command:
            raise SandboxError("docker sandbox: prepare() has not been called")
        if not argv or any(not isinstance(a, str) for a in argv):
            raise SandboxError("docker sandbox: argv must be a list of strings")

        budget = self._task.resources
        exec_argv = ["docker", "exec", "--user",
                     f"{_SANDBOX_USER}", "--workdir", CONTAINER_WORKSPACE,
                     self._container, *argv]
        started = time.monotonic()
        try:
            completed = _run(exec_argv, timeout=budget.command_timeout_s)
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                argv=tuple(argv), exit_code=None, executed=False, timed_out=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"command exceeded {budget.command_timeout_s}s")
        except OSError as exc:
            return ExecutionResult(
                argv=tuple(argv), exit_code=None, executed=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: could not start the command")

        stdout, out_clipped = _clip(completed.stdout, budget.max_output_bytes)
        stderr, err_clipped = _clip(completed.stderr, budget.max_output_bytes)
        return ExecutionResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            executed=True,
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated_output=out_clipped or err_clipped,
        )

    def collect_artifacts(self) -> dict[str, str]:
        if self._workspace is None:
            return {}
        return self._workspace.collect_artifacts()

    def terminate(self) -> None:
        """Stop the container. Idempotent and never raises."""
        if not self._container:
            return
        try:
            _run(["docker", "rm", "--force", self._container], timeout=PROBE_TIMEOUT_S)
        except (subprocess.SubprocessError, OSError):
            pass          # cleanup must not mask the episode's own failure
        finally:
            self._started = False

    def cleanup(self) -> None:
        """Destroy container and workspace. Unconditional, idempotent, never raises."""
        self.terminate()
        self._container = ""
        if self._workspace is not None:
            self._workspace.destroy()
            self._workspace = None

    def capability_report(self) -> SandboxCapabilityReport:
        available, version, reason = docker_available()
        return SandboxCapabilityReport(
            backend=self.name,
            version=version,
            available=available,
            executes_commands=available,
            enforces_isolation=available,
            supports_network_policy=True,
            unavailable_reason=reason,
            notes=("one disposable container per episode",
                   "container isolation, NOT a multi-tenant or microVM boundary",
                   "fixtures are copied into a tmpfs workspace, never mounted from "
                   "the repository"),
        )

    def security_report(self) -> SandboxSecurityReport:
        task = self._task
        return SandboxSecurityReport(
            backend=self.name,
            isolation_boundary=(
                "Linux container: all capabilities dropped, no-new-privileges, "
                "read-only rootfs, tmpfs workspace, unprivileged uid. Shares the host "
                "kernel — not equivalent to a microVM or a multi-tenant boundary."),
            network_policy=task.network.policy.value if task else "none",
            read_only_rootfs=task.sandbox.read_only_rootfs if task else True,
            capabilities_dropped=task.sandbox.drop_all_capabilities if task else True,
            no_new_privileges=task.sandbox.no_new_privileges if task else True,
            runs_as_root=False,
            mounts=tuple(m.to_dict() for m in self.mounts),
            command_audit=self._audit,
            audited=bool(self._command) and not self._audit,
            caveats=("a host kernel vulnerability defeats this boundary",
                     "seccomp uses the runtime default profile",
                     "AppArmor/SELinux confinement depends on the host"),
        )


_SANDBOX_USER = "65534:65534"


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Bound captured output. The full text is never retained."""
    raw = text or ""
    encoded = raw.encode("utf-8", "surrogatepass")
    if len(encoded) <= limit:
        return raw, False
    clipped = encoded[:limit].decode("utf-8", "ignore")
    return (f"{clipped}\n[output truncated at {limit} bytes; "
            f"full-output sha256={sha256_text(raw)[:16]}]"), True


__all__ = ["DEFAULT_GYM_IMAGE", "PROBE_TIMEOUT_S", "DockerSandboxBackend",
           "docker_available"]
