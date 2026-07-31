"""training_gym.sandbox — V69 M62: isolated, disposable execution for one episode.

Two backends implement one contract:

  * :class:`~training_gym.sandbox.dry_run_backend.DryRunSandboxBackend` validates
    everything and executes nothing. It never reports a success, so its results can
    be used to check a task, a policy and a launch command on a machine with no
    Docker daemon — which is how the security negative controls run in CI.
  * :class:`~training_gym.sandbox.docker_backend.DockerSandboxBackend` runs one
    disposable, unprivileged, capability-free, network-less container per episode and
    destroys it afterwards. When no daemon is reachable it says so and refuses;
    it never silently downgrades to the dry-run backend.

The security-critical code is :mod:`training_gym.sandbox.security`, which builds the
container argv and then INDEPENDENTLY audits it. The tests inspect that argv directly,
because a test that mocks the runtime and asserts on the policy object proves only
that the policy object is well-formed.

Importing this package starts no container, contacts no daemon and reads no
environment. Availability is probed only when a backend is asked for it.
"""
from __future__ import annotations

from .base import SandboxBackend, SandboxError, SandboxUnavailable, require_backend
from .docker_backend import DockerSandboxBackend, docker_available
from .dry_run_backend import DEFAULT_GYM_IMAGE, DryRunSandboxBackend
from .result import (
    ExecutionResult,
    PreparedSandbox,
    SandboxCapabilityReport,
    SandboxSecurityReport,
)
from .security import (
    CONTAINER_HOSTNAME,
    CONTAINER_WORKSPACE,
    FORBIDDEN_FLAGS,
    FORBIDDEN_VALUES,
    SandboxSecurityError,
    audit_command,
    build_environment,
    build_run_command,
    describe_command,
    validate_image,
)

#: The backends a CLI may name. ``dry_run`` is the default everywhere.
BACKENDS: dict[str, type] = {
    "dry_run": DryRunSandboxBackend,
    "docker": DockerSandboxBackend,
}


def get_backend(name: str, **kwargs: object):
    """Resolve a backend by name. An unknown name fails; it never falls back."""
    key = str(name or "").strip().lower().replace("-", "_")
    if key not in BACKENDS:
        raise SandboxError(f"unknown sandbox backend {name!r}; "
                           f"choose one of {sorted(BACKENDS)}")
    return BACKENDS[key](**kwargs)  # type: ignore[arg-type]


__all__ = [
    "BACKENDS", "CONTAINER_HOSTNAME", "CONTAINER_WORKSPACE", "DEFAULT_GYM_IMAGE",
    "FORBIDDEN_FLAGS", "FORBIDDEN_VALUES", "DockerSandboxBackend",
    "DryRunSandboxBackend", "ExecutionResult", "PreparedSandbox", "SandboxBackend",
    "SandboxCapabilityReport", "SandboxError", "SandboxSecurityError",
    "SandboxSecurityReport", "SandboxUnavailable", "audit_command",
    "build_environment", "build_run_command", "describe_command", "docker_available",
    "get_backend", "require_backend", "validate_image",
]
