"""training_gym/sandbox/security.py — V69 M62: the container command, and its audit.

WHY THIS EXISTS
---------------
A sandbox is only as isolated as the flags it was actually launched with. A backend
that holds a beautifully restrictive :class:`~training_gym.policies.SandboxPolicy` and
then builds ``docker run -v /:/host --privileged`` is not a sandbox, and no amount of
policy-object testing would notice — which is why the tests for this module inspect
the generated ``argv`` itself rather than mocking the runtime and asserting on intent.

So this module does two separable things:

  * :func:`build_run_command` TRANSLATES a policy into an argv list;
  * :func:`audit_command` INDEPENDENTLY re-reads that argv and refuses it if a
    forbidden flag is present.

They are deliberately not the same code path. The builder can only produce what it
knows about; the auditor rejects a denylist of everything that has ever meant "turn
the isolation off", including flags this builder never emits. A future edit that adds
``--cap-add`` to the builder still has to get past the auditor, and
:func:`build_run_command` runs the auditor on its own output before returning.

WHAT THIS BOUNDARY IS, HONESTLY
-------------------------------
This is a hardened Linux container: user namespace as configured by the daemon, all
capabilities dropped, no new privileges, read-only root filesystem, no network by
default, a tmpfs workspace, and process/memory/CPU/file-size ceilings. That is a
strong boundary against a task fixture that behaves badly and against accidental
damage to the host.

It is NOT a multi-tenant isolation boundary. A container shares the host kernel, so a
kernel vulnerability is a host compromise. Do not run material here that you would not
run on the host itself, and do not describe this as equivalent to a microVM. See
``docs/TRAINING_GYM_SECURITY.md``.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

from ..policies import (
    DETERMINISTIC_ENV,
    MountSpec,
    NetworkPolicy,
    NetworkSpec,
    ResourceBudget,
    SandboxPolicy,
    credential_shaped,
    forbidden_mount,
)
from ..schemas import SchemaError

#: The unprivileged uid:gid an episode runs as. ``65534`` is ``nobody`` on the
#: distributions used for the gym images.
SANDBOX_UID = 65534
SANDBOX_GID = 65534

#: Where the disposable workspace is mounted inside the container.
CONTAINER_WORKSPACE = "/workspace"

#: A fixed hostname, so the operator's machine name never appears in a container, in
#: captured output, or in a trajectory record.
CONTAINER_HOSTNAME = "gym"

#: Flags that mean "remove an isolation guarantee". Matched against the built argv.
#: The value is the reason, which is what a refusal message reports.
FORBIDDEN_FLAGS: dict[str, str] = {
    "--privileged": "grants all capabilities and disables most isolation",
    "--cap-add": "re-adds a Linux capability the policy dropped",
    "--device": "passes a host device through to the container",
    "--device-cgroup-rule": "widens device access",
    "--userns=host": "shares the host user namespace",
    "--uts=host": "shares the host UTS namespace",
    "--ipc=host": "shares host shared memory",
    "--pid=host": "shares the host process namespace",
    "--network=host": "shares the host network stack",
    "--net=host": "shares the host network stack",
    "--cgroupns=host": "shares the host cgroup namespace",
    "--sysctl": "mutates kernel parameters",
    "--systempaths=unconfined": "unmasks /proc and /sys",
    "--oom-kill-disable": "lets a runaway episode survive the OOM killer",
}

#: Substrings that are forbidden anywhere in the argv, whatever flag carries them.
FORBIDDEN_VALUES: dict[str, str] = {
    "seccomp=unconfined": "disables seccomp filtering",
    "apparmor=unconfined": "disables AppArmor confinement",
    "apparmor:unconfined": "disables AppArmor confinement",
    "label=disable": "disables SELinux labelling",
    "label:disable": "disables SELinux labelling",
    "docker.sock": "exposes the container control socket",
    "/var/run/docker": "exposes the container control socket",
    "//./pipe/docker": "exposes the Windows container control channel",
}

_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,199}"
                       r"(?::[A-Za-z0-9._-]{1,128})?(?:@sha256:[0-9a-f]{64})?$")


class SandboxSecurityError(SchemaError):
    """A launch command was refused. Never downgraded to a warning."""


def audit_command(argv: Sequence[str]) -> tuple[str, ...]:
    """Every reason *argv* is unsafe to run. Empty tuple means it passed.

    Reads the command as data, with no knowledge of how it was built. This is the
    control that survives a future edit to the builder.
    """
    problems: list[str] = []
    tokens = [str(t) for t in argv]
    joined = " ".join(tokens).lower()

    for flag, reason in FORBIDDEN_FLAGS.items():
        base = flag.split("=", 1)[0]
        for i, token in enumerate(tokens):
            low = token.lower()
            hit = (low == flag) if "=" in flag else (low == base or
                                                     low.startswith(base + "="))
            # ``--network host`` is the same thing as ``--network=host``, written
            # with a space; checking only the joined form would miss one of them.
            if not hit and "=" in flag and low == base:
                value = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
                hit = value == flag.split("=", 1)[1]
            if hit:
                problems.append(f"{flag}: {reason}")
                break

    for needle, reason in FORBIDDEN_VALUES.items():
        if needle in joined:
            problems.append(f"{needle}: {reason}")

    # Positive assertions: the guarantees that must be PRESENT, not merely not-absent.
    required = {
        "--rm": "the container must be removed after the episode",
        "--read-only": "the root filesystem must be read-only",
        "--cap-drop": "all capabilities must be dropped",
        "--user": "the episode must not run as root",
        "no-new-privileges": "privilege escalation must be blocked",
        "--pids-limit": "a process ceiling is required",
        "--memory": "a memory ceiling is required",
    }
    for token, reason in required.items():
        if token not in joined:
            problems.append(f"missing {token}: {reason}")

    if "--cap-drop all" not in joined and "--cap-drop=all" not in joined:
        problems.append("--cap-drop must drop ALL, not a subset")
    if f"--user {SANDBOX_UID}:{SANDBOX_GID}" not in joined and \
            f"--user={SANDBOX_UID}:{SANDBOX_GID}" not in joined:
        problems.append("--user must pin the unprivileged sandbox uid:gid")
    if "--network none" not in joined and "--network=none" not in joined \
            and "--network" not in joined:
        problems.append("missing --network: the network posture must be explicit")

    # No credential-shaped variable may be handed in, whatever the policy said.
    for i, token in enumerate(tokens):
        if token in ("-e", "--env") and i + 1 < len(tokens):
            name = tokens[i + 1].split("=", 1)[0]
            if credential_shaped(name):
                problems.append(f"--env {name}: credential-shaped variable")
        elif token.startswith("--env="):
            name = token[6:].split("=", 1)[0]
            if credential_shaped(name):
                problems.append(f"--env {name}: credential-shaped variable")

    # Every bind source, however it was spelled.
    for i, token in enumerate(tokens):
        source = ""
        if token in ("-v", "--volume") and i + 1 < len(tokens):
            source = tokens[i + 1].split(":", 1)[0]
        elif token.startswith("--mount"):
            spec = token.split("=", 1)[1] if "=" in token else (
                tokens[i + 1] if i + 1 < len(tokens) else "")
            for part in spec.split(","):
                if part.startswith(("source=", "src=")):
                    source = part.split("=", 1)[1]
        if source:
            marker = forbidden_mount(source)
            if marker:
                problems.append(f"mount source refused ({marker})")
    return tuple(dict.fromkeys(problems))


def validate_image(image: str) -> str:
    """A pinned, well-formed image reference.

    ``:latest`` is refused: an episode whose base image can change underneath it is
    not reproducible, and a trajectory that records "latest" records nothing.
    """
    text = str(image or "").strip()
    if not _IMAGE_RE.match(text):
        raise SandboxSecurityError(f"sandbox image {image!r} is not a well-formed "
                                   f"reference")
    if text.endswith(":latest") or (":" not in text and "@" not in text):
        raise SandboxSecurityError(
            f"sandbox image {image!r} is unpinned; pin a tag or a @sha256 digest so "
            f"an episode is reproducible")
    return text


def build_environment(policy: SandboxPolicy) -> dict[str, str]:
    """The complete environment an episode receives. An allowlist, then pinned values.

    Nothing is inherited from the operator's process. ``HOME`` and ``TMPDIR`` are
    pinned rather than passed through, because the operator's home path contains the
    operator's username.
    """
    env: dict[str, str] = {}
    for name in sorted(policy.env_allowlist):
        if credential_shaped(name):
            raise SandboxSecurityError(f"env allowlist contains {name!r}")
        if name in DETERMINISTIC_ENV:
            env[name] = DETERMINISTIC_ENV[name]
        elif name == "PATH":
            env[name] = "/usr/local/bin:/usr/bin:/bin"
    env.update(DETERMINISTIC_ENV)
    return {k: v for k, v in sorted(env.items()) if k in set(policy.env_allowlist)}


def build_run_command(
    *,
    image: str,
    argv: Sequence[str],
    policy: SandboxPolicy,
    resources: ResourceBudget,
    network: NetworkSpec | None = None,
    mounts: Sequence[MountSpec] = (),
    container_name: str = "",
    workspace_size_mb: int = 64,
    proxy_network: str = "",
) -> list[str]:
    """Build the container launch command, then audit it before returning it.

    Returns an argv LIST. There is no string form and no shell: the command is never
    interpolated, never passed to a shell, and never assembled from user text.
    """
    if not argv:
        raise SandboxSecurityError("sandbox: refusing to launch with an empty command")
    if any(not isinstance(a, str) for a in argv):
        raise SandboxSecurityError("sandbox: argv must be a list of strings")

    net = network or NetworkSpec()
    cmd: list[str] = ["docker", "run", "--rm"]

    if container_name:
        cmd += ["--name", container_name]

    # -- network ---------------------------------------------------------------
    if net.policy is NetworkPolicy.NONE:
        cmd += ["--network", "none"]
    else:
        if not proxy_network:
            raise SandboxSecurityError(
                "sandbox: an allowlisted network policy requires an operator-supplied "
                "egress-proxy network name; the gym will not create one implicitly")
        if proxy_network in ("host", "bridge", "none"):
            raise SandboxSecurityError(
                f"sandbox: {proxy_network!r} is not an allowlisted proxy network")
        cmd += ["--network", proxy_network]

    # -- filesystem ------------------------------------------------------------
    if policy.read_only_rootfs:
        cmd += ["--read-only"]
    if policy.tmpfs_workspace:
        size = max(1, min(int(workspace_size_mb), 512))
        cmd += ["--tmpfs", f"{CONTAINER_WORKSPACE}:rw,noexec,nosuid,nodev,size={size}m",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m"]

    for mount in mounts:
        if not isinstance(mount, MountSpec):
            raise SandboxSecurityError("sandbox: mounts must be MountSpec instances, "
                                       "not raw strings")
        flags = "ro" if mount.read_only else "rw"
        cmd += ["--mount",
                f"type=bind,source={mount.source},target={mount.target},"
                f"readonly={'true' if mount.read_only else 'false'}"]
        del flags

    # -- privileges ------------------------------------------------------------
    cmd += ["--user", f"{SANDBOX_UID}:{SANDBOX_GID}"]
    if policy.drop_all_capabilities:
        cmd += ["--cap-drop", "ALL"]
    if policy.no_new_privileges:
        cmd += ["--security-opt", "no-new-privileges:true"]
    # Docker applies its default seccomp profile when no --security-opt seccomp is
    # given. Passing "seccomp=default" is not valid syntax, so the DEFAULT profile is
    # expressed by omission; the auditor's job is to prove "unconfined" never appears.
    if policy.seccomp not in ("default", "runtime/default"):
        raise SandboxSecurityError(f"sandbox: unsupported seccomp profile "
                                   f"{policy.seccomp!r}")

    # -- resources -------------------------------------------------------------
    cmd += [
        "--pids-limit", str(resources.pids),
        "--memory", f"{resources.memory_mb}m",
        # Equal to --memory: swap is disabled, so a memory ceiling is a real ceiling
        # rather than an invitation to thrash the host's disk.
        "--memory-swap", f"{resources.memory_mb}m",
        "--cpus", f"{resources.cpus:.2f}",
        "--ulimit", f"nofile={resources.pids}:{resources.pids}",
        "--ulimit", f"fsize={resources.max_file_size_bytes}",
        "--ulimit", "core=0",
        "--stop-timeout", str(min(resources.timeout_s, 60)),
    ]

    # -- identity and environment ----------------------------------------------
    cmd += ["--hostname", CONTAINER_HOSTNAME, "--workdir", CONTAINER_WORKSPACE]
    for name, value in build_environment(policy).items():
        cmd += ["--env", f"{name}={value}"]

    cmd += [validate_image(image), *argv]

    problems = audit_command(cmd)
    if problems:
        raise SandboxSecurityError(
            "refusing to launch an unsafe container: " + "; ".join(problems))
    return cmd


def describe_command(cmd: Sequence[str]) -> str:
    """A human-readable rendering FOR DISPLAY ONLY.

    Never parsed, never executed, never round-tripped back into a command — it exists
    so a dry run can show an operator exactly what would have been launched.
    """
    return " ".join(shlex.quote(str(part)) for part in cmd)


__all__ = [
    "CONTAINER_HOSTNAME", "CONTAINER_WORKSPACE", "FORBIDDEN_FLAGS",
    "FORBIDDEN_VALUES", "SANDBOX_GID", "SANDBOX_UID", "SandboxSecurityError",
    "audit_command", "build_environment", "build_run_command", "describe_command",
    "validate_image",
]
