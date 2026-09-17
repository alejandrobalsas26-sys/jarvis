"""tests/test_execution_surfaces_m66b.py — V69 M66B (§26/§27): the execution
surface coverage gate and static bypass detector, plus their non-vacuity.

The detector is worthless if it cannot flag a real bypass, so this proves BOTH
directions: a synthetic handler that calls subprocess directly IS flagged
(positive), and the real approved gateways are NOT (negative).
"""
from __future__ import annotations

from scripts.check_execution_surfaces import (
    check_bypass,
    check_coverage,
    scan_source,
)
from core.execution_surface_registry import coverage


# ── Coverage invariant (§26) ──────────────────────────────────────────────────
def test_coverage_invariant_holds():
    arbitrary, covered = coverage()
    assert arbitrary == covered, (
        "ARBITRARY_CODE_EXECUTION_SURFACES != CONTAINMENT_POLICY_COVERED_SURFACES")


def test_coverage_gate_is_clean_on_the_real_tree():
    assert check_coverage() == []


def test_bypass_detector_clean_on_the_real_tree():
    assert check_bypass(["tools/executor.py"]) == []


# ── Detector non-vacuity — POSITIVE (§27) ─────────────────────────────────────
_HOSTILE_HANDLER = '''
class ToolExecutor:
    def _tool_evil(self, cmd):
        import subprocess
        return subprocess.Popen(cmd, shell=True)
'''

_HOSTILE_OS_SYSTEM = '''
class ToolExecutor:
    def _tool_evil(self, cmd):
        import os
        return os.system(cmd)
'''

_HOSTILE_EXEC = '''
class ToolExecutor:
    def _tool_evil(self, code):
        return exec(code)
'''


def test_detector_flags_direct_subprocess():
    findings = scan_source(_HOSTILE_HANDLER, "tools/executor.py")
    assert findings, "the detector missed a direct subprocess.Popen in a handler"
    joined = " ".join(m for _, m in findings)
    assert "subprocess.Popen" in joined
    assert "shell=True" in joined


def test_detector_flags_os_system():
    findings = scan_source(_HOSTILE_OS_SYSTEM, "tools/executor.py")
    assert any("os.system" in m for _, m in findings)


def test_detector_flags_exec_builtin():
    findings = scan_source(_HOSTILE_EXEC, "tools/executor.py")
    assert any("exec()" in m for _, m in findings)


# ── Detector non-vacuity — NEGATIVE (§27) ─────────────────────────────────────
_APPROVED_SHELL_GATEWAY = '''
class ToolExecutor:
    def _tool_run_shell_command(self, command):
        import subprocess
        return subprocess.run(["ls"], shell=False)
'''

_REVIEWED_FIXED = '''
class ToolExecutor:
    def _tool_network_scan(self, target):
        import subprocess
        return subprocess.run(["nmap", target], shell=False)
'''


def test_detector_allows_approved_shell_gateway():
    assert scan_source(_APPROVED_SHELL_GATEWAY, "tools/executor.py") == []


def test_detector_allows_reviewed_fixed_internal():
    assert scan_source(_REVIEWED_FIXED, "tools/executor.py") == []


def test_broker_module_is_an_approved_gateway():
    # The broker legitimately uses subprocess to build the jail; it is exempt.
    src = 'import subprocess\ndef f():\n    subprocess.Popen(["bwrap"])\n'
    assert scan_source(src, "core/containment.py") == []
