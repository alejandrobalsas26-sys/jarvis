"""tests/test_ci_entrypoint_reality_v69_s5e.py — V69 S5E: the CI entrypoint must RUN.

WHY THIS FILE EXISTS
====================
``test_ci_workflow_v69_m612.py`` asserts a great deal about ``.github/workflows/ci.yml``
— that the gates are present, that no mandatory job swallows an exit code, that the
Bandit label matches its threshold. Every one of those assertions reads the workflow as
TEXT. Not one of them ever ran the command the workflow declares.

That gap was not theoretical. At the M65C seal the workflow's authoritative job,

    python -m pytest -q --tb=short jarvis/tests tests        (from the repository root)

exited 2 with two collection errors, while every workflow-shape test above was green and
the milestone's own focused suites — run from ``jarvis/`` — were green too. The cause was
a package shadow that only exists when the repository root is the working directory:

    <repo>/tests/__init__.py       exists -> a REGULAR package
    <repo>/jarvis/tests/          no __init__.py -> only a NAMESPACE portion

Python records a namespace portion and keeps scanning sys.path; a regular package found
later wins outright. So ``tests`` bound to ``<repo>/tests`` and ``tests.support`` — the
M65C crash harness, which lived at ``jarvis/tests/support`` — did not exist. From
``jarvis/`` as the working directory ``<repo>`` is not on sys.path at all, only the
namespace portion is found, and the same import succeeds. Same tree, opposite result,
decided entirely by the working directory.

The tests below therefore assert reality, not text: they EXECUTE the workflow's own
command in a fresh interpreter and require it to resolve. A repository can have ten
thousand tests and still be wrong about one command.

WHAT IS DELIBERATELY *NOT* ASSERTED HERE
========================================
That CI passed remotely. A test cannot know that, and pretending otherwise is the exact
class of circular fact this milestone exists to remove.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is a base dependency")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JARVIS_ROOT = _REPO_ROOT / "jarvis"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The canonical, unambiguous home of the shared test-support package.
_SUPPORT_DIR = _JARVIS_ROOT / "tests" / "_test_support"

#: Bounded so a hung child fails loudly instead of stalling the suite.
_SUBPROCESS_S = 300.0


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _authoritative_pytest_command(workflow: dict) -> str:
    """The one ``pytest`` command CI treats as the release correctness gate.

    Read out of the workflow rather than hardcoded: if someone edits the command,
    these tests must follow it, not keep validating a command CI no longer runs.
    """
    job = workflow["jobs"]["tests"]
    steps = [s for s in job.get("steps", []) if isinstance(s, dict) and "run" in s]
    commands = [str(s["run"]).strip() for s in steps]
    pytest_commands = [c for c in commands if "pytest" in c]
    assert len(pytest_commands) == 1, \
        f"expected exactly one pytest invocation in the authoritative job, got {pytest_commands!r}"
    return pytest_commands[0]


def _child_env() -> dict:
    """CI's environment for the authoritative job, minus anything pytest injects.

    ``PYTEST_CURRENT_TEST`` and friends leak the parent's identity into the child and
    have confused nested runs before; the child must look like a fresh CI shell.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.pop("PYTHONPATH", None)          # the defect hid behind an inherited PYTHONPATH
    env["JARVIS_ENV"] = "test"
    env["JARVIS_AUTO_INSTALL_DEPS"] = "false"
    env["RBAC_DEFAULT_ACTOR"] = "test-actor:Admin"
    return env


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), env=_child_env(), capture_output=True,
                          text=True, timeout=_SUBPROCESS_S)


# ── 1. the canonical support package is unambiguous ──────────────────────────

def test_the_support_package_lives_where_this_file_claims():
    assert _SUPPORT_DIR.is_dir(), f"no support package at {_SUPPORT_DIR}"
    assert (_SUPPORT_DIR / "__init__.py").is_file()
    assert (_SUPPORT_DIR / "m65c_effect_world.py").is_file()


def test_the_support_package_name_is_unique_in_the_repository():
    """Uniqueness is what makes the import independent of sys.path ORDER.

    ``support`` was not unique in any useful sense: it was reached through ``tests``,
    and ``tests`` names two different real directories. ``_test_support`` names one.
    """
    matches = [p for p in _REPO_ROOT.rglob("_test_support")
               if p.is_dir() and ".venv" not in p.parts and ".git" not in p.parts]
    assert matches == [_SUPPORT_DIR], f"the support package name is shadowed: {matches}"


def test_the_old_ambiguous_support_location_is_gone():
    assert not (_JARVIS_ROOT / "tests" / "support").exists(), \
        "tests/support is back; the `tests.` prefix is ambiguous from the repository root"


def test_no_test_imports_through_the_ambiguous_tests_namespace():
    """The specific import shape that made the authoritative job exit 2.

    ``tests`` resolves to a DIFFERENT directory depending on the working directory,
    so no test's correctness may be decided by it.
    """
    offenders = []
    for tree in (_JARVIS_ROOT / "tests", _REPO_ROOT / "tests"):
        for path in tree.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("from tests.", "import tests.")) or \
                        stripped in {"import tests", "from tests import"} or \
                        stripped.startswith("from tests import "):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {stripped}")
    assert not offenders, (
        "these imports resolve differently from the repository root than from jarvis/:\n"
        + "\n".join(offenders))


def test_the_shadow_that_caused_the_defect_still_exists():
    """Documents WHY the fix is shaped the way it is, and fails if that changes.

    The repo-level marker is load-bearing: five basenames collide across the two test
    trees, and under pytest's prepend import mode that ``__init__.py`` is what keeps
    them from claiming the same module name. Deleting it was never an option, so the
    support package had to stop travelling through ``tests`` instead. If this ever
    stops being true, the reasoning in ``_test_support/__init__.py`` needs revisiting.
    """
    assert (_REPO_ROOT / "tests" / "__init__.py").is_file(), \
        "the repo-level tests package marker vanished; re-derive the fix"
    assert not (_JARVIS_ROOT / "tests" / "__init__.py").exists(), \
        "jarvis/tests gained a package marker; module names now collide across trees"

    colliding = ({p.name for p in (_REPO_ROOT / "tests").glob("test_*.py")}
                 & {p.name for p in (_JARVIS_ROOT / "tests").glob("test_*.py")})
    assert colliding, "the basename collision is gone; the marker may no longer be needed"


# ── 2. the import resolves in a FRESH interpreter, from either directory ─────

@pytest.mark.parametrize("cwd_name", ["repo_root", "jarvis"])
def test_a_fresh_interpreter_resolves_the_support_module(cwd_name):
    """Not `is it importable here` — `is it importable in a new process, from there`.

    The parent pytest process has already mutated sys.path in ways CI's first import
    has not, so importing in-process would prove nothing about the CI entrypoint.
    """
    cwd = _REPO_ROOT if cwd_name == "repo_root" else _JARVIS_ROOT
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import _test_support.m65c_effect_world as m; print(m.__file__)" % (
            _JARVIS_ROOT / "tests")
    )
    proc = _run([sys.executable, "-c", code], cwd=cwd)
    assert proc.returncode == 0, f"fresh import failed from {cwd_name}: {proc.stderr[-2000:]}"
    resolved = Path(proc.stdout.strip()).resolve()
    assert resolved == (_SUPPORT_DIR / "m65c_effect_world.py").resolve(), \
        f"from {cwd_name} the support module resolved to {resolved}"


def test_the_spawned_worker_script_finds_the_runtime_it_imports():
    """The crash harness runs this file AS A SCRIPT in a brand-new interpreter.

    Its sys.path[0] is its own directory, which has no ``core`` in it — the harness
    supplies the runtime through PYTHONPATH and cwd. If that derivation is ever off by
    a directory the crash tests fail in a confusing place, so it is pinned here.
    """
    worker = _SUPPORT_DIR / "m65c_effect_world.py"
    jarvis_root = worker.resolve().parent.parent.parent
    assert jarvis_root == _JARVIS_ROOT.resolve(), \
        f"the harness derives the runtime root as {jarvis_root}"

    env = _child_env()
    env["PYTHONPATH"] = str(jarvis_root)
    proc = subprocess.run(
        [sys.executable, "-c", "import core.effect_journal as m; print(m.__file__)"],
        cwd=str(jarvis_root), env=env, capture_output=True, text=True,
        timeout=_SUBPROCESS_S)
    assert proc.returncode == 0, f"a worker could not import the runtime: {proc.stderr[-2000:]}"
    assert Path(proc.stdout.strip()).is_file()


# ── 3. the workflow's OWN command, executed ─────────────────────────────────

def test_the_authoritative_command_names_both_trees(workflow):
    command = _authoritative_pytest_command(workflow)
    assert "jarvis/tests" in command, f"the authoritative job drops jarvis/tests: {command}"
    tail = command.split("jarvis/tests", 1)[1]
    assert "tests" in tail, f"the authoritative job drops the repo-level tests: {command}"


def test_the_authoritative_job_does_not_set_a_working_directory(workflow):
    """Running it from ``jarvis/`` is what hid the defect for an entire milestone.

    It also silently changes what one security test asserts: the traversal fixture in
    the repo-level suite resolves ``../../etc/passwd`` relative to the CWD.
    """
    job = workflow["jobs"]["tests"]
    for step in job.get("steps", []):
        if isinstance(step, dict) and "pytest" in str(step.get("run", "")):
            assert "working-directory" not in step, \
                "the authoritative suite no longer runs from the repository root"


def test_the_authoritative_command_actually_collects(workflow):
    """THE test this milestone exists for: run CI's command and require it to resolve.

    ``--collect-only`` is the right scope here. Collection is where the defect lived —
    an unimportable module is an ERROR, not a failure — and running the full suite
    inside itself would square the runtime for no extra signal. Every other test in
    this repository is the evidence that the collected tests then pass.
    """
    command = _authoritative_pytest_command(workflow)
    argv = command.split()
    assert argv[:3] == ["python", "-m", "pytest"], f"unexpected command shape: {command}"
    proc = _run([sys.executable, "-m", "pytest", *argv[3:], "--collect-only", "-q"],
                cwd=_REPO_ROOT)
    assert proc.returncode == 0, (
        "the command .github/workflows/ci.yml calls authoritative does not collect "
        f"from the repository root (exit {proc.returncode}):\n"
        f"{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}")


@pytest.mark.parametrize("module", [
    "test_effect_journal_crash_recovery_v69_m65c.py",
    "test_durable_effect_live_v69_m65c.py",
])
def test_the_m65c_modules_import_from_the_repository_root(module):
    """These two are the modules that were unreachable in CI. Pinned individually so a
    regression names the file instead of drowning in a 10,000-test collection."""
    proc = _run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                 f"jarvis/tests/{module}"], cwd=_REPO_ROOT)
    assert proc.returncode == 0, \
        f"{module} does not import from the repository root:\n{proc.stdout[-3000:]}"


def test_both_trees_collect_together_from_the_repository_root():
    """Cross-tree resolution in one process — the condition the defect violated."""
    proc = _run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                 "jarvis/tests/test_effect_journal_crash_recovery_v69_m65c.py",
                 "tests/test_security.py"], cwd=_REPO_ROOT)
    assert proc.returncode == 0, \
        f"the two trees do not collect together:\n{proc.stdout[-3000:]}"


# ── 4. the fix must not have leaked test-only code into the distribution ────

def test_the_support_package_stays_inside_the_pruned_tests_tree():
    """``MANIFEST.in`` prunes ``tests`` and ``check_package_manifest.py`` rejects any
    ``/tests/`` path fragment. Living under ``jarvis/tests/`` is what keeps this crash
    harness out of the wheel; a sibling of ``core/`` would have escaped both rules.
    """
    assert _SUPPORT_DIR.parent == _JARVIS_ROOT / "tests", \
        f"the support package moved outside the pruned tests tree: {_SUPPORT_DIR}"

    manifest = (_JARVIS_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tests" in manifest, "MANIFEST.in no longer prunes the tests tree"

    scanner = (_JARVIS_ROOT / "scripts" / "check_package_manifest.py").read_text(
        encoding="utf-8")
    assert '"/tests/"' in scanner, "the manifest scanner no longer rejects /tests/ paths"


def test_the_support_package_is_not_a_declared_distribution_package():
    pyproject = (_JARVIS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "_test_support" not in pyproject, \
        "the test support package is declared to setuptools and would be shipped"
