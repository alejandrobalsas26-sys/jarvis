"""V69 M62 — sandbox negative controls, asserted against the real generated argv.

A test that mocks the container runtime and then asserts on the policy object proves
only that the policy object is well-formed. These tests therefore build the actual
launch command and inspect it, and separately feed hostile commands to the auditor
that re-reads them.

The dry-run backend makes all of this runnable with no Docker daemon, no GPU and no
network, which is the point: the security controls must hold in CI, not only on a
developer machine that happens to have Docker installed.
"""
from __future__ import annotations

import pytest

from training_gym import policies as P
from training_gym.sandbox import (
    DockerSandboxBackend,
    DryRunSandboxBackend,
    ExecutionResult,
    SandboxError,
    SandboxSecurityError,
    SandboxUnavailable,
    audit_command,
    build_environment,
    build_run_command,
    docker_available,
    get_backend,
    require_backend,
    validate_image,
)
from training_gym.sandbox.security import SANDBOX_GID, SANDBOX_UID
from training_gym.schemas import SchemaError
from tests.test_training_gym_m62_foundation import make_spec

IMAGE = "python:3.12-slim-bookworm"


def build(**kw):
    defaults = {
        "image": IMAGE,
        "argv": ("python", "-c", "pass"),
        "policy": P.SandboxPolicy(),
        "resources": P.ResourceBudget(),
    }
    defaults.update(kw)
    return build_run_command(**defaults)


# ── group 1: the generated command carries every guarantee ────────────────────
def test_the_generated_command_is_an_argv_list_not_a_shell_string():
    cmd = build()
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)
    assert cmd[:3] == ["docker", "run", "--rm"]


@pytest.mark.parametrize("flag,value", [
    ("--network", "none"),
    ("--user", f"{SANDBOX_UID}:{SANDBOX_GID}"),
    ("--cap-drop", "ALL"),
    ("--security-opt", "no-new-privileges:true"),
    ("--hostname", "gym"),
    ("--workdir", "/workspace"),
])
def test_every_required_isolation_flag_is_present_with_its_value(flag, value):
    cmd = build()
    assert flag in cmd, f"{flag} missing from the generated command"
    assert cmd[cmd.index(flag) + 1] == value


def test_the_root_filesystem_is_read_only_and_the_workspace_is_tmpfs():
    cmd = build()
    assert "--read-only" in cmd
    tmpfs = [cmd[i + 1] for i, t in enumerate(cmd) if t == "--tmpfs"]
    assert any(m.startswith("/workspace:") for m in tmpfs)
    for mount in tmpfs:
        assert "noexec" in mount and "nosuid" in mount and "nodev" in mount


def test_resource_ceilings_reach_the_command_line():
    resources = P.ResourceBudget(memory_mb=512, cpus=1.5, pids=64,
                                 max_file_size_bytes=2_000_000)
    cmd = build(resources=resources)
    assert cmd[cmd.index("--memory") + 1] == "512m"
    # Swap equal to memory: a memory ceiling that can spill to swap is not a ceiling.
    assert cmd[cmd.index("--memory-swap") + 1] == "512m"
    assert cmd[cmd.index("--cpus") + 1] == "1.50"
    assert cmd[cmd.index("--pids-limit") + 1] == "64"
    assert "fsize=2000000" in cmd
    assert "core=0" in cmd


def test_the_hostname_is_fixed_so_the_host_machine_name_never_leaks():
    assert build()[build().index("--hostname") + 1] == "gym"


def test_the_environment_is_pinned_and_carries_no_operator_identity():
    env = build_environment(P.SandboxPolicy())
    assert env["HOME"] == "/home/gym"
    assert env["TZ"] == "UTC"
    assert env["PYTHONHASHSEED"] == "0"
    blob = " ".join(f"{k}={v}" for k, v in env.items())
    assert "Users" not in blob and "aleja" not in blob
    for name in env:
        assert not P.credential_shaped(name)


def test_only_allowlisted_environment_variables_are_passed():
    cmd = build(policy=P.SandboxPolicy(env_allowlist=("PATH", "TZ")))
    passed = {cmd[i + 1].split("=", 1)[0] for i, t in enumerate(cmd) if t == "--env"}
    assert passed <= {"PATH", "TZ"}


# ── group 2: the auditor refuses every isolation-removing flag ────────────────
@pytest.mark.parametrize("hostile", [
    ["docker", "run", "--privileged"],
    ["docker", "run", "--cap-add", "SYS_ADMIN"],
    ["docker", "run", "--cap-add=NET_ADMIN"],
    ["docker", "run", "--device", "/dev/mem"],
    ["docker", "run", "--pid=host"],
    ["docker", "run", "--pid", "host"],
    ["docker", "run", "--network=host"],
    ["docker", "run", "--network", "host"],
    ["docker", "run", "--net=host"],
    ["docker", "run", "--ipc=host"],
    ["docker", "run", "--uts=host"],
    ["docker", "run", "--userns=host"],
    ["docker", "run", "--cgroupns=host"],
    ["docker", "run", "--sysctl", "net.ipv4.ip_forward=1"],
    ["docker", "run", "--oom-kill-disable"],
    ["docker", "run", "--security-opt", "seccomp=unconfined"],
    ["docker", "run", "--security-opt", "apparmor=unconfined"],
    ["docker", "run", "--security-opt", "label=disable"],
    ["docker", "run", "-v", "/var/run/docker.sock:/var/run/docker.sock"],
    ["docker", "run", "-v", "//./pipe/docker_engine://./pipe/docker_engine"],
    ["docker", "run", "-v", "/:/host"],
    ["docker", "run", "-v", "/home/alice:/data"],
    ["docker", "run", "-v", "C:/Users/aleja:/data"],
    ["docker", "run", "--mount", "type=bind,source=/root/.ssh,target=/keys"],
    ["docker", "run", "--mount", "type=bind,src=/etc,target=/etc"],
    ["docker", "run", "--env", "AWS_SECRET_ACCESS_KEY=abc"],
    ["docker", "run", "-e", "GITHUB_TOKEN=abc"],
])
def test_the_auditor_refuses_isolation_removing_commands(hostile):
    problems = audit_command(hostile)
    assert problems, f"auditor accepted {hostile}"


def test_the_auditor_requires_the_positive_guarantees_not_just_their_absence():
    """A command with no forbidden flag is still refused if it omits a guarantee."""
    problems = audit_command(["docker", "run", "alpine", "sh"])
    joined = " ".join(problems)
    for expected in ("--rm", "--read-only", "--cap-drop", "--user",
                     "no-new-privileges", "--pids-limit", "--memory"):
        assert expected in joined, expected


def test_a_generated_command_passes_its_own_audit():
    assert audit_command(build()) == ()


def test_the_builder_audits_its_own_output_before_returning_it(monkeypatch):
    """The audit is on the RETURN path, not merely available beside it.

    This is what makes the auditor a control rather than a convenience: a future edit
    that teaches the builder a new flag still has to satisfy the independent reader.
    Simulating a finding proves the builder consults it and refuses."""
    import training_gym.sandbox.security as sec
    monkeypatch.setattr(sec, "audit_command", lambda cmd: ("simulated finding",))
    with pytest.raises(SandboxSecurityError, match="unsafe container"):
        sec.build_run_command(image=IMAGE, argv=("python", "-c", "pass"),
                              policy=P.SandboxPolicy(),
                              resources=P.ResourceBudget())


def test_raw_mount_strings_are_refused_by_type():
    with pytest.raises(SandboxSecurityError, match="MountSpec"):
        build(mounts=("/opt/fx:/workspace",))


def test_a_validated_mount_is_accepted_and_marked_read_only():
    cmd = build(mounts=(P.MountSpec(source="/opt/fx", target="/fixtures"),))
    spec = cmd[cmd.index("--mount") + 1]
    assert "source=/opt/fx" in spec and "target=/fixtures" in spec
    assert "readonly=true" in spec


# ── group 3: images must be pinned ────────────────────────────────────────────
@pytest.mark.parametrize("image", ["python", "python:latest", "", "UPPER:1",
                                   "../evil:1", "a" * 300])
def test_unpinned_or_malformed_images_are_refused(image):
    with pytest.raises(SandboxSecurityError):
        validate_image(image)


def test_a_pinned_image_is_accepted():
    assert validate_image("python:3.12-slim-bookworm")
    assert validate_image("python@sha256:" + "a" * 64)


# ── group 4: the network posture cannot be widened ────────────────────────────
def test_the_default_command_has_no_network():
    cmd = build()
    assert cmd[cmd.index("--network") + 1] == "none"


def test_an_allowlist_policy_refuses_to_run_without_an_operator_proxy_network():
    """The gym will not invent an egress path; the operator must supply one."""
    net = P.NetworkSpec(policy=P.NetworkPolicy.ALLOWLIST, reason="fetch rules",
                        destinations=("rules.example.com",))
    with pytest.raises(SandboxSecurityError, match="operator-supplied"):
        build(network=net)


@pytest.mark.parametrize("bad_network", ["host", "bridge", "none"])
def test_a_default_docker_network_is_not_an_allowlisted_proxy(bad_network):
    net = P.NetworkSpec(policy=P.NetworkPolicy.ALLOWLIST, reason="fetch rules",
                        destinations=("rules.example.com",))
    with pytest.raises(SandboxSecurityError, match="not an allowlisted proxy"):
        build(network=net, proxy_network=bad_network)


def test_an_operator_supplied_proxy_network_is_used_verbatim():
    net = P.NetworkSpec(policy=P.NetworkPolicy.ALLOWLIST, reason="fetch rules",
                        destinations=("rules.example.com",))
    cmd = build(network=net, proxy_network="gym-egress")
    assert cmd[cmd.index("--network") + 1] == "gym-egress"


# ── group 5: a dry run never fabricates a success ─────────────────────────────
def test_a_dry_run_reports_that_it_executes_nothing():
    report = DryRunSandboxBackend().capability_report()
    assert report.available
    assert report.executes_commands is False
    assert report.enforces_isolation is False


def test_a_dry_run_execution_is_never_a_success():
    backend = DryRunSandboxBackend()
    backend.prepare(make_spec())
    result = backend.execute(["pytest", "-q"])
    assert result.executed is False
    assert result.exit_code is None, "a plan must not report exit code 0"
    assert result.succeeded is False
    backend.cleanup()


def test_a_dry_run_still_builds_and_audits_the_real_command():
    backend = DryRunSandboxBackend()
    prepared = backend.prepare(make_spec())
    assert prepared.launch_command[:3] == ("docker", "run", "--rm")
    assert audit_command(prepared.launch_command) == ()
    assert prepared.security is not None and prepared.security.safe
    plan = backend.execution_plan()
    assert plan["would_execute"] is False
    assert plan["command_audit"] == []
    backend.cleanup()


def test_a_dry_run_plan_leaks_no_host_path_or_username():
    backend = DryRunSandboxBackend()
    backend.prepare(make_spec())
    from training_gym.schemas import canonical_json
    blob = canonical_json(backend.execution_plan())
    assert "Users" not in blob and "AppData" not in blob
    backend.cleanup()


def test_a_dry_run_collects_no_artifacts():
    backend = DryRunSandboxBackend()
    backend.prepare(make_spec())
    assert backend.collect_artifacts() == {}
    backend.cleanup()


def test_execute_before_prepare_is_refused():
    with pytest.raises(SandboxError, match="prepare"):
        DryRunSandboxBackend().execute(["pytest"])


def test_cleanup_is_idempotent_and_destroys_the_workspace():
    backend = DryRunSandboxBackend()
    backend.prepare(make_spec())
    root = backend._workspace.root  # noqa: SLF001 — asserting destruction
    assert root.exists()
    backend.cleanup()
    backend.cleanup()
    assert not root.exists()


# ── group 6: an unavailable backend says so and never substitutes ─────────────
def test_docker_reports_unavailability_honestly():
    available, _version, reason = docker_available()
    report = DockerSandboxBackend().capability_report()
    assert report.available is available
    assert report.executes_commands is available
    if not available:
        assert reason and report.unavailable_reason


def test_an_unavailable_docker_backend_refuses_rather_than_downgrading():
    available, _v, _r = docker_available()
    if available:
        pytest.skip("a Docker daemon is reachable on this host")
    with pytest.raises(SandboxUnavailable, match="dry-run"):
        DockerSandboxBackend().prepare(make_spec())


def test_the_docker_security_report_does_not_claim_microvm_isolation():
    """Honesty control: the boundary must be described as what it is.

    Checked for AFFIRMATIVE overclaims. A naive substring scan would flag the honest
    sentence "not equivalent to a microVM or a multi-tenant boundary" for containing
    the very phrase it is disclaiming."""
    report = DockerSandboxBackend().security_report()
    text = (report.isolation_boundary + " " + " ".join(report.caveats)).lower()
    assert "not equivalent to a microvm" in text or "not a microvm" in text
    assert "shares the host kernel" in text
    assert "kernel vulnerability defeats this boundary" in text
    # Only phrases that cannot occur inside a disclaimer. "equivalent to a microVM"
    # is deliberately absent: it is a substring of the honest denial asserted above.
    for overclaim in ("is a multi-tenant", "provides multi-tenant", "fully isolated",
                      "unbreakable", "cannot be escaped", "is equivalent to a microvm"):
        assert overclaim not in text, overclaim


# ── group 7: the backend contract is complete ─────────────────────────────────
@pytest.mark.parametrize("backend", [DryRunSandboxBackend(), DockerSandboxBackend()])
def test_both_backends_implement_the_whole_contract(backend):
    assert require_backend(backend) is backend


def test_a_partial_backend_is_refused():
    class Partial:
        name = "partial"

        def prepare(self, task):
            return None

    with pytest.raises(SandboxError, match="missing"):
        require_backend(Partial())


def test_an_unknown_backend_name_fails_rather_than_falling_back():
    assert isinstance(get_backend("dry_run"), DryRunSandboxBackend)
    assert isinstance(get_backend("dry-run"), DryRunSandboxBackend)
    with pytest.raises(SandboxError, match="unknown sandbox backend"):
        get_backend("none")


# ── group 8: a result cannot claim what it did not observe ────────────────────
def test_a_result_cannot_report_an_exit_code_it_never_collected():
    with pytest.raises(SchemaError, match="never executed"):
        ExecutionResult(executed=False, exit_code=0)


def test_a_result_cannot_be_executed_without_an_exit_code():
    with pytest.raises(SchemaError, match="collected no exit code"):
        ExecutionResult(executed=True, exit_code=None)


def test_only_a_real_zero_exit_counts_as_success():
    assert ExecutionResult(executed=True, exit_code=0).succeeded
    assert not ExecutionResult(executed=True, exit_code=1).succeeded
    assert not ExecutionResult(executed=False, exit_code=None).succeeded
    assert not ExecutionResult(executed=False, exit_code=None, timed_out=True).succeeded


def test_a_security_report_cannot_be_clean_while_the_audit_found_problems():
    from training_gym.sandbox import SandboxSecurityReport
    with pytest.raises(SchemaError, match="audited-clean"):
        SandboxSecurityReport(backend="x", isolation_boundary="y",
                              command_audit=("--privileged: bad",), audited=True)
