"""tests/test_ssh_hostkeys_v69_m617.py — V69 M61.7: SSH is fail-closed.

Bandit reported five findings across ``core/sensor_mesh.py`` and
``tools/ebpf_bridge.py``: B507 (HIGH, x2), B108, B601 (x2). They were symptoms of one
posture: **JARVIS trusted whatever answered.**

``paramiko.AutoAddPolicy()`` is trust-on-first-use with nobody in the loop. On the
flat lab networks these two bridges point at — precisely where an attacker who can
answer ARP or DNS lives — the first machine to respond on the configured address
became permanently trusted, and the operator's own private key did the
authenticating. Then ``deploy_sensor_to_vm`` uploaded executable Python to a fixed,
shared, world-writable ``/tmp/jarvis_sensor.py``, ``pip install``ed onto the remote
host, and ran the uploaded file.

The posture now:
  * ``RejectPolicy`` — unknown key raises, mismatched key raises, no exceptions;
  * no trust-on-first-use, *including* no silent opt-in: enrollment is two explicit
    calls with an out-of-band-verified fingerprint in between;
  * remote staging is off by default and requires trusted-lab mode when on;
  * JARVIS never installs a package on a remote host and never executes remote code;
  * staged paths are unique, private (0600) and home-relative — no ``/tmp``.

Paramiko is a ``soc``-profile dependency, so every test here drives a **fake**
paramiko injected into ``sys.modules``. That is not a convenience: it lets the tests
assert the rejection path deterministically, with no network and no real host key.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

from core import ssh_policy

_APP_ROOT = Path(__file__).resolve().parent.parent


# ── fake paramiko ───────────────────────────────────────────────────────────
class _FakeKey:
    def __init__(self, blob: bytes = b"ssh-rsa-fake-key", name: str = "ssh-rsa"):
        self._blob = blob
        self._name = name

    def asbytes(self) -> bytes:
        return self._blob

    def get_name(self) -> str:
        return self._name


class _SSHException(Exception):
    pass


class _BadHostKeyException(_SSHException):
    def __init__(self, hostname, key, expected_key):
        super().__init__(f"bad host key for {hostname}")
        self.hostname, self.key, self.expected_key = hostname, key, expected_key


class _RejectPolicy:
    """Stand-in for the real fail-closed policy."""


class _AutoAddPolicy:
    """Present only so a test can prove the code never selects it."""


class _WarningPolicy:
    """Present only so a test can prove the code never selects it."""


class _FakeSFTP:
    def __init__(self):
        self.home = "/home/labuser"
        self.written: dict[str, str] = {}
        self.chmods: dict[str, int] = {}
        self.closed = False

    def normalize(self, path):
        return self.home

    def open(self, path, mode="r"):
        sftp = self

        class _Handle:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def write(self_inner, data):
                sftp.written[path] = data

        return _Handle()

    def chmod(self, path, mode):
        self.chmods[path] = mode

    def close(self):
        self.closed = True


class _FakeSSHClient:
    """Records every security-relevant decision made against it."""

    #: Set by a test to control what ``connect`` does.
    behaviour = "ok"

    def __init__(self):
        self.policy = None
        self.loaded_system = False
        self.loaded_files: list[str] = []
        self.connected: dict | None = None
        self.commands: list[str] = []
        self.host_keys: dict = {}
        self.closed = False
        self.sftp = _FakeSFTP()

    def load_system_host_keys(self, filename=None):
        self.loaded_system = True

    def load_host_keys(self, filename):
        self.loaded_files.append(str(filename))

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self.host_keys

    def connect(self, host, **kwargs):
        if self.behaviour == "unknown":
            raise _SSHException(f"Server {host!r} not found in known_hosts")
        if self.behaviour == "mismatch":
            raise _BadHostKeyException(host, _FakeKey(b"attacker"), _FakeKey(b"expected"))
        self.connected = {"host": host, **kwargs}

    def exec_command(self, command, get_pty=False):
        self.commands.append(command)
        return None, iter(()), None

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


def _fake_paramiko(behaviour: str = "ok") -> types.ModuleType:
    module = types.ModuleType("paramiko")
    _FakeSSHClient.behaviour = behaviour
    module.SSHClient = _FakeSSHClient
    module.RejectPolicy = _RejectPolicy
    module.AutoAddPolicy = _AutoAddPolicy
    module.WarningPolicy = _WarningPolicy
    module.SSHException = _SSHException
    module.BadHostKeyException = _BadHostKeyException
    module.RSAKey = types.SimpleNamespace(from_private_key_file=lambda path: _FakeKey())
    module.HostKeys = _FakeHostKeys
    module.Transport = _FakeTransport
    return module


class _FakeHostKeys:
    saved: dict[str, list] = {}

    def __init__(self, filename=None):
        self.filename = filename
        self.entries: list[tuple[str, str, object]] = []

    def add(self, host, keytype, key):
        self.entries.append((host, keytype, key))

    def save(self, filename):
        _FakeHostKeys.saved[str(filename)] = list(self.entries)
        Path(filename).write_text("fake known_hosts\n", encoding="utf-8")

    def check(self, host, key):
        return False

    def __len__(self):
        return len(self.entries)


class _FakeTransport:
    offered_key = _FakeKey(b"offered-key")

    def __init__(self, address):
        self.address = address

    def start_client(self, timeout=None):
        return None

    def get_remote_server_key(self):
        return type(self).offered_key

    def close(self):
        return None


@pytest.fixture
def paramiko_ok(monkeypatch):
    module = _fake_paramiko("ok")
    monkeypatch.setitem(sys.modules, "paramiko", module)
    return module


@pytest.fixture
def paramiko_unknown(monkeypatch):
    module = _fake_paramiko("unknown")
    monkeypatch.setitem(sys.modules, "paramiko", module)
    return module


@pytest.fixture
def paramiko_mismatch(monkeypatch):
    module = _fake_paramiko("mismatch")
    monkeypatch.setitem(sys.modules, "paramiko", module)
    return module


@pytest.fixture
def isolated_stores(monkeypatch, tmp_path):
    """Point both host-key stores at an empty temporary tree.

    Without this, a developer's real ``~/.ssh/known_hosts`` would leak into the
    assertions and the tests would pass or fail depending on whose machine ran them.
    """
    monkeypatch.setenv(ssh_policy.KNOWN_HOSTS_ENV, str(tmp_path / "known_hosts"))
    monkeypatch.setattr(ssh_policy, "system_known_hosts_path", lambda: tmp_path / "system")
    return tmp_path


# ── the policy itself ───────────────────────────────────────────────────────
def test_harden_client_installs_reject_policy(paramiko_ok, isolated_stores):
    client = paramiko_ok.SSHClient()
    posture = ssh_policy.harden_client(client)
    assert isinstance(client.policy, _RejectPolicy)
    assert posture.policy == "RejectPolicy"
    assert posture.auto_add is False


def test_harden_client_never_installs_an_auto_trust_policy(paramiko_ok, isolated_stores):
    """The whole point: one automatic-trust mechanism must not replace another."""
    client = paramiko_ok.SSHClient()
    ssh_policy.harden_client(client)
    assert not isinstance(client.policy, (_AutoAddPolicy, _WarningPolicy))


def test_unknown_host_key_is_rejected(paramiko_unknown, isolated_stores, tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("fake", encoding="utf-8")
    client = paramiko_unknown.SSHClient()
    ssh_policy.harden_client(client)
    with pytest.raises(ssh_policy.HostKeyVerificationError) as excinfo:
        ssh_policy.connect_verified(client, "10.0.0.5", username="lab", key_path=str(key))
    assert "does not trust on first use" in str(excinfo.value)
    assert client.connected is None


def test_mismatched_host_key_is_rejected_with_both_fingerprints(
    paramiko_mismatch, isolated_stores, tmp_path
):
    key = tmp_path / "id_rsa"
    key.write_text("fake", encoding="utf-8")
    client = paramiko_mismatch.SSHClient()
    ssh_policy.harden_client(client)
    with pytest.raises(ssh_policy.HostKeyVerificationError) as excinfo:
        ssh_policy.connect_verified(client, "10.0.0.5", username="lab", key_path=str(key))
    message = str(excinfo.value)
    assert "MISMATCH" in message
    assert "impersonation" in message
    assert client.connected is None


def test_a_known_host_connects(paramiko_ok, isolated_stores, tmp_path):
    """Fail-closed must not mean fail-always."""
    key = tmp_path / "id_rsa"
    key.write_text("fake", encoding="utf-8")
    client = paramiko_ok.SSHClient()
    ssh_policy.harden_client(client)
    ssh_policy.connect_verified(client, "10.0.0.5", username="lab", key_path=str(key))
    assert client.connected["host"] == "10.0.0.5"
    assert client.connected["username"] == "lab"


def test_both_host_key_stores_are_loaded_when_present(paramiko_ok, isolated_stores):
    (isolated_stores / "system").write_text("sys\n", encoding="utf-8")
    (isolated_stores / "known_hosts").write_text("managed\n", encoding="utf-8")
    client = paramiko_ok.SSHClient()
    posture = ssh_policy.harden_client(client)
    assert client.loaded_system is True
    assert str(isolated_stores / "known_hosts") in client.loaded_files
    assert set(posture.stores) == {"system", "managed"}


def test_missing_stores_fail_more_closed_not_less(paramiko_ok, isolated_stores):
    client = paramiko_ok.SSHClient()
    posture = ssh_policy.harden_client(client)
    assert posture.stores == ()
    assert posture.loaded_host_count == 0
    assert isinstance(client.policy, _RejectPolicy)   # still fail-closed


def test_managed_store_is_overridable_and_defaults_inside_the_managed_tree(monkeypatch):
    monkeypatch.delenv(ssh_policy.KNOWN_HOSTS_ENV, raising=False)
    default = ssh_policy.managed_known_hosts_path()
    assert default.is_absolute()
    assert default.parts[-3:] == ("data", "ssh", "known_hosts")
    monkeypatch.setenv(ssh_policy.KNOWN_HOSTS_ENV, "/opt/lab/known_hosts")
    assert ssh_policy.managed_known_hosts_path() == Path("/opt/lab/known_hosts")


def test_fingerprint_is_the_openssh_sha256_form():
    printed = ssh_policy.fingerprint(_FakeKey(b"abc"))
    assert printed.startswith("SHA256:")
    assert not printed.endswith("=")          # base64 padding stripped, as OpenSSH does
    assert printed != ssh_policy.fingerprint(_FakeKey(b"abd"))


# ── enrollment is explicit, two-step, and never automatic ───────────────────
def test_enrollment_plan_writes_nothing(paramiko_ok, isolated_stores):
    plan = ssh_policy.enrollment_plan("10.0.0.5")
    assert plan["enrolled"] is False
    assert plan["fingerprint"].startswith("SHA256:")
    assert plan["operator_action"], "the operator must be told what to verify"
    assert not (isolated_stores / "known_hosts").exists()


def test_enrollment_requires_the_operator_approved_fingerprint(paramiko_ok, isolated_stores):
    result = ssh_policy.enroll_verified_host("10.0.0.5", "SHA256:something-else")
    assert result["enrolled"] is False
    assert "mismatch" in result["error"]
    assert "Nothing was written" in result["error"]
    assert not (isolated_stores / "known_hosts").exists()


def test_enrollment_succeeds_only_for_the_verified_key(paramiko_ok, isolated_stores):
    approved = ssh_policy.fingerprint(_FakeTransport.offered_key)
    result = ssh_policy.enroll_verified_host("10.0.0.5", approved)
    assert result["enrolled"] is True
    assert result["fingerprint"] == approved
    assert (isolated_stores / "known_hosts").exists()


def test_a_key_swap_between_verification_and_enrollment_is_refused(
    paramiko_ok, isolated_stores, monkeypatch
):
    """The race the two-step design exists to close."""
    plan = ssh_policy.enrollment_plan("10.0.0.5")
    monkeypatch.setattr(_FakeTransport, "offered_key", _FakeKey(b"attacker-key"))
    result = ssh_policy.enroll_verified_host("10.0.0.5", plan["fingerprint"])
    assert result["enrolled"] is False
    assert not (isolated_stores / "known_hosts").exists()


# ── remote staging paths (B108) ─────────────────────────────────────────────
def test_staging_path_is_not_in_a_shared_temp_directory():
    sftp = _FakeSFTP()
    path = ssh_policy.remote_staging_path(sftp, "sensor", ".py")
    for shared in ("/tmp/", "/var/tmp/", "/dev/shm/"):
        assert not path.startswith(shared), path
    assert path.startswith(sftp.home + "/")


def test_staging_paths_are_unique_per_call():
    sftp = _FakeSFTP()
    paths = {ssh_policy.remote_staging_path(sftp, "sensor") for _ in range(64)}
    assert len(paths) == 64, "a reused remote filename is a symlink/race target"


def test_staging_path_carries_enough_randomness_to_be_unguessable():
    sftp = _FakeSFTP()
    leaf = ssh_policy.remote_staging_path(sftp, "sensor", ".py").rsplit("/", 1)[-1]
    token = leaf[len(".jarvis-sensor-") : -len(".py")]
    assert len(token) == 32          # 16 bytes hex = 128 bits
    assert all(char in "0123456789abcdef" for char in token)


def test_staging_path_leaks_no_secret_and_no_operator_text():
    sftp = _FakeSFTP()
    path = ssh_policy.remote_staging_path(sftp, "sensor", ".py")
    for secret in ("KALI", "key", "passwd", "token", "192.168"):
        assert secret.lower() not in path.lower().replace("jarvis-sensor", "")


def test_staging_mode_is_owner_only_and_not_executable():
    assert ssh_policy.REMOTE_STAGING_MODE == 0o600
    assert not ssh_policy.REMOTE_STAGING_MODE & 0o111, "JARVIS does not start this file"
    assert not ssh_policy.REMOTE_STAGING_MODE & 0o077, "no group/other access"


# ── deployment is disabled by default (B601 / remote mutation) ──────────────
def test_remote_staging_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv(ssh_policy.REMOTE_STAGING_ENV, raising=False)
    monkeypatch.delenv("JARVIS_TRUSTED_LAB", raising=False)
    assert ssh_policy.remote_staging_enabled() is False


def test_remote_staging_needs_both_opt_in_and_trusted_lab(monkeypatch):
    monkeypatch.setattr(ssh_policy, "trusted_lab_enabled", lambda: False)
    monkeypatch.setenv(ssh_policy.REMOTE_STAGING_ENV, "true")
    assert ssh_policy.remote_staging_enabled() is False, "opt-in alone is not enough"

    monkeypatch.setattr(ssh_policy, "trusted_lab_enabled", lambda: True)
    monkeypatch.delenv(ssh_policy.REMOTE_STAGING_ENV, raising=False)
    assert ssh_policy.remote_staging_enabled() is False, "lab mode alone is not enough"

    monkeypatch.setenv(ssh_policy.REMOTE_STAGING_ENV, "true")
    assert ssh_policy.remote_staging_enabled() is True


def _recorder(events: list[dict]):
    """An async ``broadcast_fn`` that records the events it is handed."""

    async def _broadcast(event):
        events.append(event)

    return _broadcast


def test_default_deploy_mutates_nothing_and_returns_a_plan(monkeypatch, paramiko_ok):
    from core import sensor_mesh

    monkeypatch.setattr(sensor_mesh.ssh_policy, "remote_staging_enabled", lambda: False)
    events: list[dict] = []

    result = asyncio.run(
        sensor_mesh.deploy_sensor_to_vm(
            "10.0.0.5", "lab", "/nonexistent/key", _recorder(events)
        )
    )
    assert result is False
    assert events and events[0]["type"] == "sensor_deploy_requires_operator"
    plan = events[0]["plan"]
    assert plan["operator_action"] and plan["why_not_automatic"]
    assert plan["staged_path"] is None
    # Nothing was written to and nothing was run on the remote host.
    assert paramiko_ok.SSHClient().commands == []
    assert paramiko_ok.SSHClient().sftp.written == {}


def test_opt_in_staging_writes_the_file_but_executes_nothing(monkeypatch, paramiko_ok):
    from core import sensor_mesh

    monkeypatch.setattr(sensor_mesh.ssh_policy, "remote_staging_enabled", lambda: True)
    monkeypatch.setattr(sensor_mesh, "_generate_agent_script", lambda ip, port: "print(1)\n")
    captured: dict = {}

    def _fake_client_factory():
        client = _FakeSSHClient()
        captured["client"] = client
        return client

    monkeypatch.setattr(paramiko_ok, "SSHClient", _fake_client_factory)
    monkeypatch.setattr(sensor_mesh.ssh_policy, "harden_client", lambda c: None)
    monkeypatch.setattr(sensor_mesh.ssh_policy, "connect_verified", lambda c, h, **kw: c)

    events: list[dict] = []
    result = asyncio.run(
        sensor_mesh.deploy_sensor_to_vm("10.0.0.5", "lab", "/key", _recorder(events))
    )

    assert result is True
    client = captured["client"]
    assert client.commands == [], "staging must never execute a remote command"
    staged = list(client.sftp.written)
    assert len(staged) == 1
    assert not staged[0].startswith("/tmp/")
    assert client.sftp.chmods[staged[0]] == 0o600
    assert client.sftp.closed and client.closed, "connections must be cleaned up"
    assert events[-1]["type"] == "sensor_staged"


def test_staging_failure_is_reported_not_swallowed(monkeypatch, paramiko_unknown):
    """A rejected host key during staging must surface as a failed deployment."""
    from core import sensor_mesh

    monkeypatch.setattr(sensor_mesh.ssh_policy, "remote_staging_enabled", lambda: True)
    monkeypatch.setattr(sensor_mesh, "_generate_agent_script", lambda ip, port: "print(1)\n")
    events: list[dict] = []

    result = asyncio.run(
        sensor_mesh.deploy_sensor_to_vm("10.0.0.5", "lab", "/key", _recorder(events))
    )
    assert result is False
    assert events[-1]["type"] == "sensor_deploy_failed"
    assert events[-1]["severity"] == "WARNING"
    assert events[-1]["error"], "the reason must be reported, not dropped"


# ── static guarantees over the two call sites ──────────────────────────────
_SSH_CALL_SITES = ("core/sensor_mesh.py", "tools/ebpf_bridge.py")


@pytest.mark.parametrize("relative_path", _SSH_CALL_SITES)
def test_no_auto_add_or_warning_policy_anywhere(relative_path: str):
    source = (_APP_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "AutoAddPolicy",
            "WarningPolicy",
        }:
            pytest.fail(f"{relative_path} still references paramiko.{node.attr}")


@pytest.mark.parametrize("relative_path", _SSH_CALL_SITES)
def test_no_hardcoded_shared_temp_paths(relative_path: str):
    source = (_APP_ROOT / relative_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for shared in ("/tmp/", "/var/tmp/", "/dev/shm/"):
                assert not node.value.startswith(shared), (
                    f"{relative_path}:{node.lineno} hardcodes {node.value!r}"
                )


def _remote_exec_arguments() -> list[tuple[str, int, ast.AST]]:
    """Every first argument passed to a paramiko ``exec_command`` in core/ and tools/."""
    found: list[tuple[str, int, ast.AST]] = []
    for directory in ("core", "tools"):
        for path in sorted((_APP_ROOT / directory).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "exec_command"
                    and node.args
                ):
                    found.append((f"{directory}/{path.name}", node.lineno, node.args[0]))
    return found


def test_no_remote_command_installs_a_package_or_backgrounds_a_process():
    """The remote ``pip install`` and ``nohup ... &`` are gone and cannot come back.

    Checked against what is actually *sent to a remote shell* — the first argument of
    every ``exec_command`` in the tree — not against any string that mentions pip.
    A docstring explaining the removed behaviour is documentation, not a command.
    """
    offenders: list[str] = []
    for location, lineno, argument in _remote_exec_arguments():
        rendered = " ".join(
            node.value.lower()
            for node in ast.walk(argument)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        # A Name argument is resolved by the FALCO_CMD constant tests below.
        if isinstance(argument, ast.Name):
            module = ast.parse((_APP_ROOT / location).read_text(encoding="utf-8"))
            for node in ast.walk(module):
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == argument.id
                        for t in node.targets
                    )
                    and isinstance(node.value, ast.Constant)
                ):
                    rendered = str(node.value.value).lower()
        for banned in ("pip install", "nohup", "apt-get install", "curl "):
            if banned in rendered:
                offenders.append(f"{location}:{lineno} sends {banned!r}")
    assert offenders == [], f"remote host mutation reintroduced: {offenders}"


def test_remote_exec_sites_are_an_exhaustive_known_set():
    """Non-vacuity guard for the scan above: a new remote-exec site is a review event."""
    sites = {location for location, _lineno, _argument in _remote_exec_arguments()}
    assert sites == {"tools/ebpf_bridge.py"}, (
        f"unexpected remote command execution site(s): {sites}"
    )


def test_sensor_mesh_never_calls_exec_command():
    """The B601 finding in sensor_mesh is removed structurally, not suppressed."""
    tree = ast.parse((_APP_ROOT / "core" / "sensor_mesh.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "exec_command", (
                f"core/sensor_mesh.py:{node.lineno} executes a remote command"
            )
    source = (_APP_ROOT / "core" / "sensor_mesh.py").read_text(encoding="utf-8")
    assert "nosec" not in source, "sensor_mesh needs no suppression"


# ── the one B601 suppression is justified: FALCO_CMD is a true constant ────
def test_falco_command_is_a_compile_time_constant():
    """The property that licenses the single ``# nosec B601`` in the tree.

    Asserted over the AST, so it holds against the code rather than against a
    comment: the assignment must be a plain string literal (or a concatenation of
    string literals), with no f-string, no ``%``, no ``.format()`` and no name
    reference anywhere inside it.
    """
    tree = ast.parse((_APP_ROOT / "tools" / "ebpf_bridge.py").read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "FALCO_CMD" for t in node.targets)
    ]
    assert len(assignments) == 1, "FALCO_CMD must be assigned exactly once, at module level"
    for node in ast.walk(assignments[0].value):
        assert not isinstance(node, ast.JoinedStr), "FALCO_CMD uses an f-string"
        assert not isinstance(node, ast.Name), "FALCO_CMD references a variable"
        assert not isinstance(node, ast.Call), "FALCO_CMD calls something (.format?)"
        if isinstance(node, ast.BinOp):
            assert isinstance(node.op, ast.Add), "FALCO_CMD uses %-formatting"
    # And it really is the value passed to exec_command.
    assert isinstance(ast.literal_eval(assignments[0].value), str)


def test_exec_command_in_ebpf_bridge_receives_only_that_constant():
    tree = ast.parse((_APP_ROOT / "tools" / "ebpf_bridge.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exec_command"
    ]
    assert len(calls) == 1, "exactly one remote exec site is expected here"
    first_argument = calls[0].args[0]
    assert isinstance(first_argument, ast.Name) and first_argument.id == "FALCO_CMD"


def test_the_suppression_is_precise_and_documented():
    """A suppression must name its test id and explain itself beside the line."""
    lines = (_APP_ROOT / "tools" / "ebpf_bridge.py").read_text(encoding="utf-8").splitlines()
    suppressed = [(number, line) for number, line in enumerate(lines, 1) if "nosec" in line]
    assert len(suppressed) == 1, f"expected exactly one suppression, got {suppressed}"
    number, line = suppressed[0]
    assert "nosec B601" in line, "the suppression must name the exact Bandit id"
    assert "exec_command" in line, "it must sit on the flagged line, not above it"
    context = "\n".join(lines[max(0, number - 12) : number])
    assert "B601 SUPPRESSION JUSTIFICATION" in context
    assert "CONSTANT" in context


def test_hostile_values_cannot_reach_the_remote_command(paramiko_ok, isolated_stores, tmp_path):
    """End-to-end: poison every input ebpf_bridge reads and inspect what ran."""
    from tools import ebpf_bridge

    key = tmp_path / "id_rsa"
    key.write_text("fake", encoding="utf-8")
    hostile = "10.0.0.5; rm -rf / #"
    captured: dict = {}

    def _factory():
        client = _FakeSSHClient()
        captured["client"] = client
        return client

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(paramiko_ok, "SSHClient", _factory)
        monkey.setattr(ebpf_bridge, "KALI_HOST", hostile)
        monkey.setattr(ebpf_bridge, "KALI_USER", "$(id)")
        monkey.setattr(ebpf_bridge, "KALI_KEY_PATH", str(key))
        monkey.setattr(ssh_policy, "connect_verified", lambda c, h, **kw: c)
        client, _stdout = ebpf_bridge._connect_falco()
    finally:
        monkey.undo()

    assert client is not None
    executed = captured["client"].commands
    assert executed == [ebpf_bridge.FALCO_CMD]
    for poison in ("rm -rf", "$(id)", hostile):
        assert poison not in executed[0]


def test_no_other_module_uses_an_auto_trust_ssh_policy():
    """Tree-wide: the fix must not be undone somewhere else later."""
    offenders: list[str] = []
    for directory in ("core", "tools"):
        for path in sorted((_APP_ROOT / directory).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "AutoAddPolicy",
                    "WarningPolicy",
                }:
                    offenders.append(f"{directory}/{path.name}:{node.lineno}")
    assert offenders == [], f"automatic host-key trust reintroduced: {offenders}"


def test_ssh_policy_never_logs_key_material():
    """No code path may put a private key, password or passphrase into a log."""
    source = (_APP_ROOT / "core" / "ssh_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (
            isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"
        ):
            continue
        rendered = ast.dump(node)
        for forbidden in ("read_text", "read_bytes", "asbytes", "get_private_key"):
            assert forbidden not in rendered, (
                f"core/ssh_policy.py:{node.lineno} logs key material via {forbidden}"
            )
