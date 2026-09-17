"""scripts/check_execution_surfaces.py — V69 M66B (§26/§27): the execution
surface coverage gate and the static bypass detector.

TWO CHECKS, both BLOCKING in CI:

  1. COVERAGE (§26): every arbitrary-code-execution tool surface declared in
     ``core.execution_surface_registry`` must be covered by a containment policy,
     and the declared coverage set must equal the arbitrary-execution set. A new
     arbitrary execution path added to a tool handler without a registry entry
     fails here.

  2. STATIC BYPASS DETECTOR (§27): an AST scan of the tool handlers for
     suspicious execution primitives (`subprocess.run/Popen/call`, `os.system`,
     `os.exec*`, `os.popen`, `shell=True`, `exec(`, `eval(`) used OUTSIDE the
     approved gateways. Approved gateways and the reviewed fixed-internal launches
     are enumerated in the registry; anything else is a finding.

Read-only. No network, no service, no host mutation. Exit 0 = clean, 1 = findings.

The detector's own non-vacuity is proven by tests: a synthetic handler that
calls ``subprocess.Popen`` directly MUST be flagged (positive), and the real
approved gateways MUST NOT be (negative).
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.execution_surface_registry import (  # noqa: E402
    FIXED_INTERNAL_SURFACES,
    coverage,
)

#: Execution primitives the detector treats as suspicious in a tool handler.
_SUSPICIOUS_CALLS = {
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),
    ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
    ("os", "system"), ("os", "popen"),
    ("os", "execv"), ("os", "execve"), ("os", "execvp"), ("os", "execvpe"),
    ("os", "execl"), ("os", "execle"), ("os", "execlp"),
    ("os", "spawnv"), ("os", "spawnve"), ("os", "spawnl"),
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
}
_SUSPICIOUS_BUILTINS = {"exec", "eval"}

#: Modules whose execution primitives are the APPROVED gateway itself.
_APPROVED_GATEWAY_FILES = {
    "core/containment.py",          # the broker builds the jail
    "core/execution_profile.py",    # profile vocabulary (no exec of its own)
}


def _func_stack_name(stack: list[str]) -> str:
    """A dotted name from the enclosing class/function stack, e.g.
    ``RedTeamShellExecutor.execute`` or ``_tool_network_scan``."""
    return ".".join(stack)


def _reviewed(relpath: str, func_dotted: str, funcs: list[str]) -> bool:
    """Is (file, function) an enumerated reviewed exception?

    A launch is reviewed if the file:function key, or the file:innermost-function
    key, is in the fixed-internal registry, or the innermost function is one of
    the allowlisted-shell gateways.
    """
    innermost = funcs[-1] if funcs else ""
    keys = {f"{relpath}:{func_dotted}", f"{relpath}:{innermost}"}
    if keys & set(FIXED_INTERNAL_SURFACES):
        return True
    # The allowlisted-shell gateways in executor.py (RESTRICTED_ONLY surfaces).
    if relpath == "tools/executor.py" and innermost in {
        "_tool_run_shell_command", "execute_shell", "_classify",
        "_tool_code_execute",  # routes to the broker; no direct exec of its own
    }:
        return True
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.stack: list[str] = []
        self.findings: list[tuple[int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _flag(self, lineno: int, what: str) -> None:
        dotted = _func_stack_name([s for s in self.stack])
        funcs = list(self.stack)
        if _reviewed(self.relpath, dotted, funcs):
            return
        self.findings.append((lineno, f"{what} in {self.relpath}:{dotted or '<module>'}"))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # module.attr(...) style
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in _SUSPICIOUS_CALLS:
                self._flag(node.lineno, f"{pair[0]}.{pair[1]}()")
        # bare exec()/eval()
        if isinstance(func, ast.Name) and func.id in _SUSPICIOUS_BUILTINS:
            self._flag(node.lineno, f"{func.id}()")
        # shell=True anywhere
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is True:
                self._flag(node.lineno, "shell=True")
        self.generic_visit(node)


def scan_source(source: str, relpath: str) -> list[tuple[int, str]]:
    """Public entry point for the detector; used by the non-vacuity tests."""
    if relpath in _APPROVED_GATEWAY_FILES:
        return []
    tree = ast.parse(source)
    scanner = _Scanner(relpath)
    scanner.visit(tree)
    return scanner.findings


def _scan_file(abspath: str, relpath: str) -> list[tuple[int, str]]:
    with open(abspath, encoding="utf-8") as fh:
        return scan_source(fh.read(), relpath)


def check_coverage() -> list[str]:
    arbitrary, covered = coverage()
    problems: list[str] = []
    if arbitrary != covered:
        missing = arbitrary - covered
        extra = covered - arbitrary
        if missing:
            problems.append(
                f"arbitrary-execution surface(s) with no containment coverage: "
                f"{sorted(missing)}")
        if extra:
            problems.append(
                f"covered surface(s) not marked arbitrary-execution: {sorted(extra)}")
    return problems


def check_bypass(files: list[str]) -> list[str]:
    problems: list[str] = []
    for relpath in files:
        abspath = os.path.join(_ROOT, relpath)
        if not os.path.exists(abspath):
            continue
        for lineno, msg in _scan_file(abspath, relpath):
            problems.append(f"ungoverned execution primitive: {msg}:{lineno}")
    return problems


#: The tool-handler files the bypass detector scans.
_TOOL_FILES = [
    "tools/executor.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    problems = check_coverage() + check_bypass(_TOOL_FILES)
    if args.json:
        import json
        print(json.dumps({"problems": problems, "ok": not problems}))
    else:
        arbitrary, covered = coverage()
        print(f"arbitrary-execution surfaces: {sorted(arbitrary)}")
        print(f"containment-covered surfaces: {sorted(covered)}")
        print(f"reviewed fixed-internal launches: {len(FIXED_INTERNAL_SURFACES)}")
        if problems:
            for p in problems:
                print(f"::error::{p}")
            print(f"EXECUTION_SURFACE_COVERAGE: FAIL ({len(problems)} problem(s))")
        else:
            print("EXECUTION_SURFACE_COVERAGE: PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
