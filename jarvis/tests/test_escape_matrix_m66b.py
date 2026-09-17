"""tests/test_escape_matrix_m66b.py — V69 M66B (§34): the adversarial escape
matrix. Every case tries to break OUT of the sandbox; each must fail.

IDs mirror the plan: FS-01..07, NET-01..05, PROC-01..04, ENV-01..02,
RES-01..06, PRIV-01..02, BACKEND-01..05, TRUTH-01..02.

All fixtures are LOCAL and HARMLESS. Live-sandbox cases are skipped where
bubblewrap is unavailable; BACKEND/TRUTH cases are pure logic and run everywhere.
"""
from __future__ import annotations

import socket
import subprocess  # nosec B404 - test-only teardown of a marker process
import time
import uuid

import pytest

from core.containment import (
    ContainmentBackend,
    ContainmentBroker,
    ContainmentReceipt,
    ContainmentRequirement,
    ExecutionOutcome,
    ExecutionRequest,
    MANDATORY_SANDBOX_CONTROLS,
    RestrictedProcessBackend,
)
from core.execution_profile import ControlStatus, ExecutionProfile
from _m66b_sandbox_helpers import (  # noqa: E402 - sibling test helper (bare import)
    HostLoopbackListener,
    run_sandboxed,
    requires_sandbox,
)


# ══════════════════════════ FILESYSTEM ═══════════════════════════════════════
@requires_sandbox
class TestFilesystemEscapes:
    def test_fs01_exact_host_canary_read(self, tmp_path):
        canary = tmp_path / "JARVIS_M66B_HOST_FILE_CANARY"
        canary.write_text("HOST-ONLY-SECRET-4471")
        out = run_sandboxed(
            f"print(open({str(canary)!r}).read())")
        assert "HOST-ONLY-SECRET-4471" not in out.stdout
        assert out.receipt.to_dict()["filesystem_isolation"] == "enforced"

    def test_fs02_host_path_write_denied(self, tmp_path):
        target = tmp_path / "written_by_sandbox"
        run_sandboxed(f"open({str(target)!r},'w').write('x')")
        assert not target.exists()

    def test_fs03_host_home_enumeration(self):
        out = run_sandboxed("import os;print(os.listdir('/home'))")
        assert "kali" not in out.stdout  # /home is not mounted; enumeration fails or empty

    def test_fs04_repository_enumeration(self):
        out = run_sandboxed(
            "import os;print(os.path.exists('/home/kali/Downloads/jarvis'))")
        assert "True" not in out.stdout

    def test_fs05_parent_traversal(self):
        out = run_sandboxed(
            "import os;print(os.path.exists('/work/../home/kali'))")
        assert "True" not in out.stdout

    def test_fs06_symlink_escape(self):
        out = run_sandboxed(
            "import os\n"
            "os.symlink('/etc/shadow','/work/link')\n"
            "try:\n print(open('/work/link').read()[:20])\n"
            "except Exception as e:\n print('BLOCKED',type(e).__name__)")
        # /etc/shadow does not exist inside the jail (only /usr is bound)
        assert "root:" not in out.stdout

    def test_fs07_stale_workspace_not_visible(self):
        # Run A drops a marker; run B must not see it (ephemeral workspace).
        run_sandboxed("open('/work/marker_a','w').write('A')")
        out = run_sandboxed("import os;print(os.path.exists('/work/marker_a'))")
        assert "True" not in out.stdout


# ══════════════════════════ NETWORK ══════════════════════════════════════════
@requires_sandbox
class TestNetworkEscapes:
    def test_net01_host_ipv4_loopback_unreachable(self):
        listener = HostLoopbackListener(socket.AF_INET, "127.0.0.1")
        try:
            listener.prove_live()          # non-vacuity: the fixture IS live
            before = listener.hits
            out = run_sandboxed(
                f"import socket\n"
                f"s=socket.socket();s.settimeout(3)\n"
                f"try:\n s.connect(('127.0.0.1',{listener.port}));print('CONNECTED')\n"
                f"except Exception as e:\n print('BLOCKED',type(e).__name__)")
            time.sleep(0.3)
            assert "CONNECTED" not in out.stdout
            assert listener.hits == before  # the host listener saw NO new connection
        finally:
            listener.close()

    def test_net02_host_ipv6_loopback_unreachable(self):
        if not socket.has_ipv6:
            pytest.skip("no IPv6 on this host")
        try:
            listener = HostLoopbackListener(socket.AF_INET6, "::1")
        except OSError:
            pytest.skip("IPv6 loopback bind unavailable")
        try:
            listener.prove_live()
            before = listener.hits
            out = run_sandboxed(
                f"import socket\n"
                f"s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM);s.settimeout(3)\n"
                f"try:\n s.connect(('::1',{listener.port}));print('CONNECTED')\n"
                f"except Exception as e:\n print('BLOCKED',type(e).__name__)")
            time.sleep(0.3)
            assert "CONNECTED" not in out.stdout
            assert listener.hits == before
        finally:
            listener.close()

    def test_net03_private_address_unreachable(self):
        out = run_sandboxed(
            "import socket\n"
            "s=socket.socket();s.settimeout(2)\n"
            "try:\n s.connect(('10.255.255.1',80));print('CONNECTED')\n"
            "except Exception as e:\n print('BLOCKED',type(e).__name__)")
        assert "CONNECTED" not in out.stdout

    def test_net04_metadata_destination_unreachable(self):
        out = run_sandboxed(
            "import socket\n"
            "s=socket.socket();s.settimeout(2)\n"
            "try:\n s.connect(('169.254.169.254',80));print('CONNECTED')\n"
            "except Exception as e:\n print('BLOCKED',type(e).__name__)")
        assert "CONNECTED" not in out.stdout

    def test_net05_external_shaped_destination_unreachable(self):
        # A documentation-range address (RFC5737 TEST-NET-1); no real host probed.
        out = run_sandboxed(
            "import socket\n"
            "s=socket.socket();s.settimeout(2)\n"
            "try:\n s.connect(('192.0.2.1',80));print('CONNECTED')\n"
            "except Exception as e:\n print('BLOCKED',type(e).__name__)")
        assert "CONNECTED" not in out.stdout

    def test_no_network_interface_is_up(self):
        out = run_sandboxed(
            "import os\n"
            "p='/sys/class/net'\n"
            "print(sorted(os.listdir(p)) if os.path.exists(p) else 'NO_SYSFS')")
        # a fresh netns has only a down lo; /sys is not mounted, so NO_SYSFS
        assert "eth0" not in out.stdout and "wlan" not in out.stdout


# ══════════════════════════ PROCESS ══════════════════════════════════════════
@requires_sandbox
class TestProcessEscapes:
    def _marker(self):
        return f"JARVIS_M66B_PROC_{uuid.uuid4().hex[:10]}"

    def _survivors(self, marker):
        r = subprocess.run(["pgrep", "-f", marker],  # nosec B603,B607 - test teardown
                           capture_output=True, text=True)
        return r.stdout.strip()

    def test_proc01_child_dies_on_timeout(self):
        marker = self._marker()
        code = (f"import subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)#{marker}'])\n"
                f"time.sleep(60)")
        try:
            run_sandboxed(code, timeout=2)
            time.sleep(1.0)
            assert self._survivors(marker) == "", "a child survived the timeout"
        finally:
            subprocess.run(["pkill", "-f", marker], capture_output=True)  # nosec B603,B607

    def test_proc02_grandchild_dies_on_timeout(self):
        marker = self._marker()
        code = (f"import subprocess,sys,time\n"
                f"c='import subprocess,sys,time;subprocess.Popen([sys.executable,\"-c\","
                f"\"import time;time.sleep(60)#{marker}\"]);time.sleep(60)'\n"
                f"subprocess.Popen([sys.executable,'-c',c])\n"
                f"time.sleep(60)")
        try:
            run_sandboxed(code, timeout=2)
            time.sleep(1.0)
            assert self._survivors(marker) == "", "a grandchild survived the timeout"
        finally:
            subprocess.run(["pkill", "-f", marker], capture_output=True)  # nosec B603,B607

    def test_proc03_setsid_descendant_contained(self):
        # The exact M66A.1 weakness: a descendant that calls setsid() to escape
        # the process group. Inside the PID namespace it cannot escape.
        marker = self._marker()
        code = (f"import subprocess,sys,time,os\n"
                f"subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)#{marker}'],"
                f"start_new_session=True)\n"
                f"time.sleep(60)")
        try:
            run_sandboxed(code, timeout=2)
            time.sleep(1.0)
            assert self._survivors(marker) == "", "a setsid descendant escaped containment"
        finally:
            subprocess.run(["pkill", "-f", marker], capture_output=True)  # nosec B603,B607

    def test_proc04_pid_limit_bounds_fanout(self):
        # A small deterministic burst above the configured limit — NOT a fork bomb.
        out = run_sandboxed(
            "import subprocess,sys\n"
            "n=0;err=0\n"
            "kids=[]\n"
            "for i in range(200):\n"
            "  try:\n"
            "    kids.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']));n+=1\n"
            "  except Exception:\n    err+=1\n"
            "print('spawned',n,'errors',err)\n"
            "for k in kids: k.kill()", timeout=20)
        # The RLIMIT_NPROC (64) must have bitten: not all 200 could spawn.
        assert "errors 0" not in out.stdout


# ══════════════════════════ ENVIRONMENT ══════════════════════════════════════
@requires_sandbox
class TestEnvironmentEscapes:
    def test_env01_synthetic_secret_env_not_visible(self, monkeypatch):
        monkeypatch.setenv("JARVIS_M66B_SECRET_CANARY", "LEAK-1")
        monkeypatch.setenv("OPENAI_API_KEY_CANARY", "LEAK-2")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY_CANARY", "LEAK-3")
        out = run_sandboxed(
            "import os;print([k for k in os.environ if 'CANARY' in k or 'SECRET' in k])")
        for leak in ("LEAK-1", "LEAK-2", "LEAK-3", "CANARY"):
            assert leak not in out.stdout

    def test_env_benign_allowlisted_var_still_present(self):
        # Discriminating: the sandbox is not empty by accident — PATH survives.
        out = run_sandboxed("import os;print('PATH_OK' if os.environ.get('PATH') else 'NO_PATH')")
        assert "PATH_OK" in out.stdout

    def test_env02_runtime_control_socket_not_leaked(self):
        out = run_sandboxed(
            "import os;print(os.path.exists('/run/docker.sock'),"
            "os.path.exists('/var/run/docker.sock'),os.path.exists('/run'))")
        assert "True" not in out.stdout


# ══════════════════════════ RESOURCES ════════════════════════════════════════
@requires_sandbox
class TestResourceEscapes:
    def test_res01_cpu_bounded(self):
        out = run_sandboxed("\nwhile True:\n    pass\n", timeout=20)
        # RLIMIT_CPU (5s) kills before the 20s wall timeout.
        assert out.returncode not in (0, None) or out.error

    def test_res02_memory_bounded(self):
        out = run_sandboxed("x=bytearray(1024*1024*1024);print('ALLOC')", timeout=20)
        assert "ALLOC" not in out.stdout

    def test_res03_pid_bounded(self):
        out = run_sandboxed(
            "import subprocess,sys\nk=[]\ne=0\n"
            "for i in range(200):\n"
            "  try:k.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']))\n"
            "  except Exception:e+=1\n"
            "print('errors',e)\n"
            "for x in k:x.kill()", timeout=20)
        assert "errors 0" not in out.stdout

    def test_res04_storage_bounded(self):
        out = run_sandboxed(
            "try:\n"
            "  f=open('/work/big','wb')\n"
            "  [f.write(b'x'*(1024*1024)) or f.flush() for _ in range(200)]\n"
            "  print('WROTE_200MB')\n"
            "except Exception as e:\n  print('BOUNDED',type(e).__name__)", timeout=20)
        assert "WROTE_200MB" not in out.stdout

    def test_res05_output_bounded(self):
        out = run_sandboxed("print('A'*100000)", timeout=15)
        assert len(out.stdout) <= 3000

    def test_res06_wall_timeout(self):
        out = run_sandboxed("import time;time.sleep(60)", timeout=2)
        assert out.error and "Timeout" in out.error


# ══════════════════════════ PRIVILEGE ════════════════════════════════════════
@requires_sandbox
class TestPrivilegeEscapes:
    def test_priv01_effective_identity_non_root(self):
        out = run_sandboxed("import os;print('uid',os.getuid(),'euid',os.geteuid())")
        assert "uid 0" not in out.stdout and "euid 0" not in out.stdout

    def test_priv02_no_new_privs_and_no_caps(self):
        out = run_sandboxed(
            "s=open('/proc/self/status').read()\n"
            "import re\n"
            "for k in ('NoNewPrivs','CapEff'):\n"
            "  m=[l for l in s.splitlines() if l.startswith(k)]\n"
            "  print(m[0] if m else k+': ?')")
        assert "NoNewPrivs:\t1" in out.stdout
        assert "CapEff:\t0000000000000000" in out.stdout


# ══════════════════════════ BACKEND (logic, runs everywhere) ═════════════════
class _Unavailable(ContainmentBackend):
    name = "unavailable"

    def max_profile(self):
        return ExecutionProfile.DIRECT_PROCESS

    def execute(self, request):
        raise AssertionError("must never be selected/executed")


class TestBackendEscapes:
    def test_backend01_unavailable_fails_closed(self, monkeypatch):
        monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)
        broker = ContainmentBroker([_Unavailable()])
        out = broker.execute(ExecutionRequest("print(1)", 5,
                                              ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False

    def test_backend02_setup_failure_zero_effect(self, monkeypatch):
        # A backend whose setup fails must run zero instructions and report it.
        monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)

        class _SetupFails(ContainmentBackend):
            name = "setup_fails"

            def max_profile(self):
                return ExecutionProfile.SANDBOXED  # claims it can, then fails

            def execute(self, request):
                r = ContainmentReceipt(requirement=request.requirement, backend=self.name)
                r.failure_reason = "namespace creation failed"
                return ExecutionOutcome(executed=False, error=r.failure_reason, receipt=r)

        broker = ContainmentBroker([_SetupFails()])
        out = broker.execute(ExecutionRequest("open('/tmp/x','w').write('effect')", 5,
                                              ContainmentRequirement.SANDBOX_REQUIRED))
        assert out.executed is False
        assert out.receipt.to_dict()["profile"] != "sandboxed"

    def test_backend03_partial_setup_failure_not_sandboxed(self):
        # Filesystem OK but network isolation missing ⇒ never SANDBOXED.
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="partial")
        for c in MANDATORY_SANDBOX_CONTROLS:
            r.controls[c] = ControlStatus.ENFORCED
        r.controls["network_isolation"] = ControlStatus.NOT_AVAILABLE
        assert r.to_dict()["profile"] != "sandboxed"

    def test_backend04_cleanup_failure_is_truthful(self):
        r = ContainmentReceipt(
            requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
        for c in MANDATORY_SANDBOX_CONTROLS:
            r.controls[c] = ControlStatus.ENFORCED
        r.cleanup_status = ControlStatus.NOT_ENFORCED
        d = r.to_dict()
        assert d["cleanup_status"] == "not_enforced"
        assert d["profile"] != "sandboxed"  # unobserved cleanup blocks the claim

    def test_backend05_stronger_requirement_weaker_backend_denies(self):
        broker = ContainmentBroker([RestrictedProcessBackend()])
        assert broker.select_backend(ContainmentRequirement.SANDBOX_REQUIRED) is None


# ══════════════════════════ TRUTH (logic, runs everywhere) ═══════════════════
class TestTruthEscapes:
    def test_truth01_missing_control_cannot_yield_sandboxed(self):
        for missing in MANDATORY_SANDBOX_CONTROLS:
            r = ContainmentReceipt(
                requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
            for c in MANDATORY_SANDBOX_CONTROLS:
                r.controls[c] = ControlStatus.ENFORCED
            r.controls[missing] = ControlStatus.NOT_ENFORCED
            r.cleanup_status = ControlStatus.ENFORCED
            assert r.to_dict()["profile"] != "sandboxed", missing

    def test_truth02_pre_execution_ui_does_not_claim_sandboxed(self):
        # The requirement shown before execution is SANDBOX_REQUIRED, which is a
        # DEMAND, not a claim of achievement. It is not the word "sandboxed".
        broker = ContainmentBroker()
        req = broker.evaluate_requirement("code_execute", {})
        assert req.value == "sandbox_required"
        assert req.value != ExecutionProfile.SANDBOXED.value
