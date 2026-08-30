"""tests/test_runtime_doctor_stdlib_import.py — V69 M63 S4B.

THE DEFECT THIS PINS
--------------------
``core/runtime_doctor.py`` documents itself as "the only one that checks the thing that
actually breaks homelab installs: an interpreter that does not match the environment its
packages were installed into" — and then imported ``loguru`` at module scope. A minimal
or broken environment is exactly the one that has no ``loguru``, so the diagnostic could
not be imported in the environment it exists to diagnose. It was found while qualifying
the M62 training runtime, where the venv holds the pinned training backend and nothing
else.

The fix is a fallback to :mod:`logging`, not a removal: the warning still happens.
These tests assert the property that made the fallback necessary, so a later edit that
reintroduces a hard third-party import at module scope fails here rather than in an
operator's broken environment months later.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "core" / "runtime_doctor.py"

#: Everything `runtime_doctor` may import at module scope without a guard. Stdlib only:
#: the module's whole contract is that it runs where nothing else does.
STDLIB_ONLY = {
    "__future__", "importlib", "importlib.util", "os", "platform", "re", "shutil",
    "sys", "dataclasses", "enum", "pathlib", "logging", "json", "socket", "subprocess",
    "typing", "collections", "collections.abc", "functools", "time",
}


def _module_scope_imports(tree: ast.Module) -> set[str]:
    """Top-level imports only — a guarded import lives inside a `Try` and is skipped."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_doctor_imports_only_the_standard_library_unguarded():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    third_party = _module_scope_imports(tree) - STDLIB_ONLY
    assert not third_party, (
        f"core/runtime_doctor.py imports {sorted(third_party)} at module scope. The "
        f"diagnostic must import in the environment it diagnoses; put the import "
        f"behind a try/except with a standard-library fallback")


def test_the_loguru_import_is_guarded_and_falls_back():
    """The guard is structural, not a comment: a `try` holding the loguru import."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    guarded = [
        node for node in tree.body
        if isinstance(node, ast.Try)
        and any("loguru" in getattr(sub, "module", "") or ""
                for sub in ast.walk(node) if isinstance(sub, ast.ImportFrom))
    ]
    assert guarded, "the loguru import is not inside a try/except"
    handlers = [h for node in guarded for h in node.handlers]
    assert handlers, "the guarded import has no fallback handler"
    assert any(
        isinstance(sub, ast.Name) and sub.id == "logging"
        for h in handlers for sub in ast.walk(h)), (
        "the fallback does not bind a standard-library logger")


def test_the_doctor_runs_with_loguru_unavailable():
    """The property itself, in a child process where `loguru` cannot be imported."""
    program = (
        "import sys\n"
        "class _Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'loguru' or name.startswith('loguru.'):\n"
        "            raise ModuleNotFoundError(\"No module named 'loguru'\")\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "sys.modules.pop('loguru', None)\n"
        f"sys.path.insert(0, {str(MODULE.parents[1])!r})\n"
        "from core.runtime_doctor import run_diagnostics\n"
        "report = run_diagnostics(include_network=False)\n"
        "payload = report.to_dict()\n"
        "assert payload['findings'], 'the doctor produced no findings'\n"
        "print('OK', len(payload['findings']))\n"
    )
    result = subprocess.run([sys.executable, "-c", program],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.startswith("OK")


def test_the_doctor_still_repairs_nothing():
    """The fallback changed how the module logs, not what the module is allowed to do."""
    from core.runtime_doctor import run_diagnostics

    payload = run_diagnostics(include_network=False).to_dict()
    assert payload.get("auto_repair_performed") is False


@pytest.mark.parametrize("forbidden", ["subprocess", "os.system", "os.execv", "os.popen"])
def test_the_doctor_calls_no_installer(forbidden):
    """It may TELL a human to run pip; it may never run one itself.

    Asserted over the CALL GRAPH, not over the file's text: the remediation strings
    legitimately contain the command an operator should type, and a substring search
    over those would forbid the module from doing the job it exists to do.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    called = {ast.unparse(node.func) for node in ast.walk(tree)
              if isinstance(node, ast.Call)}
    offenders = sorted(c for c in called if c.startswith(forbidden))
    assert not offenders, (
        f"core/runtime_doctor.py calls {offenders}; the doctor diagnoses and never "
        f"repairs")
