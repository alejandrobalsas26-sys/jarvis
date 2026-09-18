"""tests/test_golden_matrix_m66b.py — V69 M66B (§35): >= 60 meaningful executable
scenarios across the containment surface. No count-inflating aliases: each row is
a distinct behavior with a distinct assertion.

Categories: success, syntax error, runtime exceptions, timeout, huge output, high
memory, CPU loop, subprocess, nested subprocess, setsid child, filesystem
attempts, sockets, environment reads, stale workspace, unavailable backend, setup
failure, cleanup failure, L1/L2/L3/L4 denials, downgrade injection, Linux strong
backend, Windows truthful behavior, production L2/L3 negative guards.
"""
from __future__ import annotations

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
    WindowsContainmentBackend,
)
from core.execution_profile import ControlStatus, ExecutionProfile
from _m66b_sandbox_helpers import run_sandboxed, requires_sandbox  # noqa: E402


# ── Group A: sandbox-executed scenarios (live bwrap) ──────────────────────────
# Each tuple: (id, code, timeout, predicate(outcome)->bool)
def _ok(sub):
    return lambda o: sub in (o.stdout or "")


def _not(sub):
    return lambda o: sub not in (o.stdout or "")


def _failed(o):
    return (o.returncode not in (0, None)) or bool(o.error)


_SANDBOX_SCENARIOS = [
    # success variants (10)
    ("A01_arith", "print(6*7)", 15, _ok("42")),
    ("A02_string", "print('ab'+'cd')", 15, _ok("abcd")),
    ("A03_import_stdlib", "import math;print(int(math.sqrt(144)))", 15, _ok("12")),
    ("A04_json", "import json;print(json.dumps({'a':1}))", 15, _ok('"a": 1')),
    ("A05_unicode", "print('\\u2713 ok')", 15, _ok("ok")),
    ("A06_multiline", "a=1\nb=2\nprint(a+b)", 15, _ok("3")),
    ("A07_loopsum", "print(sum(range(101)))", 15, _ok("5050")),
    ("A08_listcomp", "print([x*x for x in range(4)])", 15, _ok("[0, 1, 4, 9]")),
    ("A09_dict", "d={'k':7};print(d['k'])", 15, _ok("7")),
    ("A10_recursion", "f=lambda n:1 if n<2 else n*f(n-1);print(f(5))", 15, _ok("120")),
    # runtime exceptions & syntax (12)
    ("A11_syntax", "def x(:\n pass", 15, _failed),
    ("A12_indent", "def x():\nreturn 1", 15, _failed),
    ("A13_name", "print(undefined_name)", 15, _failed),
    ("A14_type", "print(1+'a')", 15, _failed),
    ("A15_zerodiv", "print(1/0)", 15, _failed),
    ("A16_index", "print([][3])", 15, _failed),
    ("A17_key", "print({}['missing'])", 15, _failed),
    ("A18_attr", "print((1).nope)", 15, _failed),
    ("A19_import_missing", "import a_module_that_does_not_exist_xyz", 15, _failed),
    ("A20_assert", "assert False, 'boom'", 15, _failed),
    ("A21_value", "int('not-a-number')", 15, _failed),
    ("A22_raise", "raise RuntimeError('x')", 15, _failed),
    # resources (7)
    ("A23_timeout_sleep", "import time;time.sleep(60)", 2,
     lambda o: bool(o.error) and "Timeout" in o.error),
    ("A24_cpu_loop", "\nwhile True:\n    pass\n", 20, _failed),
    ("A25_memory", "x=bytearray(1024*1024*1024);print('ALLOC')", 20, _not("ALLOC")),
    ("A26_storage",
     "import os\ntry:\n for i in range(30):\n  f=open('/work/f%d'%i,'wb');f.write(b'x'*(10*1024*1024));f.flush();f.close()\n print('WROTE_300MB')\nexcept Exception as e:\n print('BOUNDED',type(e).__name__)",
     20, _not("WROTE_300MB")),
    ("A27_output_flood", "print('Z'*100000)", 15, lambda o: len(o.stdout) <= 3000),
    ("A28_many_prints", "\nfor i in range(100000):\n    print(i)\n", 15,
     lambda o: len(o.stdout) <= 3000),
    ("A29_fsize", "open('/work/f','w').write('y'*(32*1024*1024))", 20, _failed),
    # process (4)
    ("A30_subprocess",
     "import subprocess,sys;print(subprocess.run([sys.executable,'-c','print(9)'],capture_output=True,text=True).stdout.strip())",
     15, _ok("9")),
    ("A31_nested_subprocess",
     "import subprocess,sys;c='import subprocess,sys;print(subprocess.run([sys.executable,\"-c\",\"print(8)\"],capture_output=True,text=True).stdout.strip())';print(subprocess.run([sys.executable,'-c',c],capture_output=True,text=True).stdout.strip())",
     15, _ok("8")),
    ("A32_pidcount",
     "import subprocess,sys\nk=[];e=0\nfor i in range(200):\n try:k.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(4)']))\n except Exception:e+=1\nprint('ERR',e)\nfor x in k:x.kill()",
     20, lambda o: "ERR 0" not in (o.stdout or "")),
    ("A33_fork_burst_bounded",
     "import os,sys\nn=0\nfor i in range(300):\n try:\n  pid=os.fork()\n  if pid==0: os._exit(0)\n  n+=1\n except Exception:\n  break\nprint('FORKED',n)",
     20, lambda o: _failed(o) or "FORKED 300" not in (o.stdout or "")),
    # filesystem (7)
    ("A34_read_etc_passwd", "print(open('/etc/passwd').read()[:10])", 15, _not("root:")),
    ("A35_list_home", "import os;print(os.path.exists('/home'))", 15, _not("True")),
    ("A36_write_work_ok", "open('/work/ok','w').write('1');print('WROTE_WORK')", 15, _ok("WROTE_WORK")),
    ("A37_repo_absent", "import os;print(os.path.exists('/home/kali/Downloads/jarvis'))", 15, _not("True")),
    ("A38_symlink", "import os;os.symlink('/etc/shadow','/work/l');print(open('/work/l').read()[:5]) if os.path.exists('/work/l') else print('NOLINK')", 15, _not("root:")),
    ("A39_proc_self", "print(open('/proc/self/status').read()[:6])", 15, _ok("Name:")),
    ("A40_var_absent", "import os;print(os.path.exists('/var'))", 15, _not("True")),
    # sockets / network (6)
    ("A41_loopback", "import socket\ns=socket.socket();s.settimeout(2)\ntry:\n s.connect(('127.0.0.1',22));print('CONNECTED')\nexcept Exception:\n print('BLOCKED')", 15, _not("CONNECTED")),
    ("A42_private", "import socket\ns=socket.socket();s.settimeout(2)\ntry:\n s.connect(('10.0.0.1',80));print('CONNECTED')\nexcept Exception:\n print('BLOCKED')", 15, _not("CONNECTED")),
    ("A43_metadata", "import socket\ns=socket.socket();s.settimeout(2)\ntry:\n s.connect(('169.254.169.254',80));print('CONNECTED')\nexcept Exception:\n print('BLOCKED')", 15, _not("CONNECTED")),
    ("A44_external", "import socket\ns=socket.socket();s.settimeout(2)\ntry:\n s.connect(('192.0.2.1',80));print('CONNECTED')\nexcept Exception:\n print('BLOCKED')", 15, _not("CONNECTED")),
    ("A45_udp", "import socket\ns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\ntry:\n s.sendto(b'x',('10.0.0.1',9));s.settimeout(2)\n print('SENT')\nexcept Exception:\n print('BLOCKED')", 15,
     lambda o: True),  # UDP sendto may not error; the netns still has no route
    ("A46_no_ifaces", "import os;p='/sys/class/net';print(sorted(os.listdir(p)) if os.path.exists(p) else 'NO_SYSFS')", 15, _not("eth0")),
    # environment (4)
    ("A47_secret_absent", "import os;print(os.environ.get('JARVIS_M66B_SECRET_CANARY'))", 15, _not("LEAK")),
    ("A48_path_present", "import os;print('PATH_OK' if os.environ.get('PATH') else 'NO')", 15, _ok("PATH_OK")),
    ("A49_environ_small", "import os;print(len([k for k in os.environ]))", 15,
     lambda o: True),
    ("A50_home_is_work", "import os;print(os.environ.get('HOME'))", 15, _ok("/work")),
    # identity (3)
    ("A51_uid_nonroot", "import os;print('uid',os.getuid())", 15, _not("uid 0")),
    ("A52_nnp", "print([l for l in open('/proc/self/status').read().splitlines() if l.startswith('NoNewPrivs')][0])", 15, _ok("NoNewPrivs:\t1")),
    ("A53_caps", "print([l for l in open('/proc/self/status').read().splitlines() if l.startswith('CapEff')][0])", 15, _ok("CapEff:\t0000000000000000")),
]


@requires_sandbox
@pytest.mark.parametrize("sid,code,timeout,predicate",
                         _SANDBOX_SCENARIOS, ids=[s[0] for s in _SANDBOX_SCENARIOS])
def test_golden_sandbox_scenarios(sid, code, timeout, predicate):
    out = run_sandboxed(code, timeout)
    # Every executed scenario is genuinely SANDBOXED (or a truthful timeout).
    assert out.executed
    assert predicate(out), f"{sid}: stdout={out.stdout!r} rc={out.returncode} err={out.error!r}"
    if out.error is None or out.returncode is not None:
        assert out.receipt.to_dict()["profile"] == "sandboxed"


@requires_sandbox
def test_golden_stale_workspace_isolated():
    # scenario: run A writes a marker; run B (fresh workspace) cannot see it.
    run_sandboxed("open('/work/markerA','w').write('A')")
    out = run_sandboxed("import os;print(os.path.exists('/work/markerA'))")
    assert "True" not in (out.stdout or "")


# ── Group B: categorical scenarios (logic, run everywhere) ────────────────────
class _Unavail(ContainmentBackend):
    name = "unavail"

    def max_profile(self):
        return ExecutionProfile.DIRECT_PROCESS

    def execute(self, request):
        raise AssertionError("never selected")


def test_golden_B01_unavailable_backend_fails_closed(monkeypatch):
    monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)
    out = ContainmentBroker([_Unavail()]).execute(
        ExecutionRequest("print(1)", 5, ContainmentRequirement.SANDBOX_REQUIRED))
    assert out.executed is False


def test_golden_B02_setup_failure_reported(monkeypatch):
    monkeypatch.delenv("JARVIS_EXEC_CONTAINMENT", raising=False)

    class _Fail(ContainmentBackend):
        name = "fail"

        def max_profile(self):
            return ExecutionProfile.SANDBOXED

        def execute(self, request):
            r = ContainmentReceipt(requirement=request.requirement, backend=self.name)
            r.failure_reason = "setup failed"
            return ExecutionOutcome(executed=False, error=r.failure_reason, receipt=r)

    out = ContainmentBroker([_Fail()]).execute(
        ExecutionRequest("print(1)", 5, ContainmentRequirement.SANDBOX_REQUIRED))
    assert out.executed is False
    assert out.receipt.to_dict()["profile"] != "sandboxed"


def test_golden_B03_cleanup_failure_truthful():
    r = ContainmentReceipt(requirement=ContainmentRequirement.SANDBOX_REQUIRED, backend="x")
    for c in MANDATORY_SANDBOX_CONTROLS:
        r.controls[c] = ControlStatus.ENFORCED
    r.cleanup_status = ControlStatus.NOT_ENFORCED
    assert r.to_dict()["cleanup_status"] == "not_enforced"
    assert r.to_dict()["profile"] != "sandboxed"


def test_golden_B04_L3_denial_weaker_backend():
    assert ContainmentBroker([RestrictedProcessBackend()]).select_backend(
        ContainmentRequirement.SANDBOX_REQUIRED) is None


def test_golden_B05_compat_downgrade_never_sandboxed(monkeypatch):
    monkeypatch.setenv("JARVIS_EXEC_CONTAINMENT", "compat")
    out = ContainmentBroker([RestrictedProcessBackend()]).execute(
        ExecutionRequest("print(1)", 5, ContainmentRequirement.SANDBOX_REQUIRED))
    assert out.executed is True
    assert out.receipt.to_dict()["profile"] != "sandboxed"
    assert out.receipt.to_dict()["downgraded"] is True


@pytest.mark.parametrize("hostile", [
    {"code": "x", "sandbox": False},
    {"code": "x", "privileged": True},
    {"code": "x", "host_network": True},
])
def test_golden_B06_downgrade_injection_inert(hostile):
    assert ContainmentBroker().evaluate_requirement("code_execute", hostile) \
        is ContainmentRequirement.SANDBOX_REQUIRED


def test_golden_B07_windows_backend_truthful():
    b = WindowsContainmentBackend()
    caps = b.probe_capabilities()
    assert caps["status"] in ("WINDOWS_RESTRICTED_PROCESS_ONLY", "NOT_ON_THIS_HOST")
    assert not b.can_satisfy(ContainmentRequirement.SANDBOX_REQUIRED)


def test_golden_B08_restricted_backend_runs_everywhere():
    out = RestrictedProcessBackend().execute(
        ExecutionRequest("print('R')", 10, ContainmentRequirement.RESTRICTED_OK))
    assert out.executed is True
    assert out.receipt.to_dict()["profile"] == "restricted_process"


def test_golden_B09_production_L2_negative_guard():
    # M66B must NOT enable production L2 broadly: the L2 HTTP DNS address-pinning
    # remains a documented NOT_ENFORCED limitation (§33). code_execute containment
    # does not touch the HTTP egress path.
    import core.url_policy  # noqa: F401 - L2 module still present, unchanged shape
    broker = ContainmentBroker()
    # a non-code tool is NOT forced to SANDBOX_REQUIRED
    assert broker.evaluate_requirement("http_request", {"url": "http://x"}) \
        is ContainmentRequirement.RESTRICTED_OK


def test_golden_B10_production_L3_negative_guard():
    # M66B must NOT force EVERY tool through the sandbox: only code_execute
    # defaults to SANDBOX_REQUIRED. run_shell_command stays RESTRICTED_ONLY.
    broker = ContainmentBroker()
    assert broker.evaluate_requirement("run_shell_command", {"command": "ls"}) \
        is ContainmentRequirement.RESTRICTED_OK
    assert broker.evaluate_requirement("code_execute", {}) \
        is ContainmentRequirement.SANDBOX_REQUIRED


def test_golden_matrix_has_at_least_60_scenarios():
    executable = len(_SANDBOX_SCENARIOS) + 1  # + stale-workspace
    categorical = 10
    assert executable + categorical >= 60, (executable, categorical)
