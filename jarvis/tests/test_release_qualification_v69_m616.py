"""tests/test_release_qualification_v69_m616.py — V69 M61.6: the gate is honest.

A qualification harness is only worth running if it cannot report a pass it did not
earn. These tests pin the two properties that make ``qualify_release_m61.py`` and the
deterministic soak trustworthy:

  * **it cannot mutate anything** — no git write, no pip install, no Ollama setting,
    no model download, no service install. Asserted over the source, because a
    harness that can mutate will eventually mutate;
  * **it cannot pass vacuously** — a missing subsystem, an absent Ollama or a skipped
    gate produces a warning or a FAIL, never a silent PASS. The soak's first run
    proved this matters: an ImportError made two checks report success without either
    having executed.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from core.qualification import release_verdict

_APP_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _APP_ROOT / "scripts"
_QUALIFY = _SCRIPTS / "qualify_release_m61.py"
_SOAK = _SCRIPTS / "soak_stabilization_m61.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_targets(path: Path) -> list[str]:
    """Every dotted call target in the module, e.g. ``subprocess.run``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            targets.append(f"{func.value.id}.{func.attr}")
        elif isinstance(func, ast.Name):
            targets.append(func.id)
    return targets


def _string_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)
    }
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


# ── the scripts exist and are importable ────────────────────────────────────
@pytest.mark.parametrize("script", ["qualify_release_m61.py",
                                    "soak_stabilization_m61.py"])
def test_script_exists_and_parses(script):
    path = _SCRIPTS / script
    assert path.is_file()
    ast.parse(path.read_text(encoding="utf-8"))


def test_qualification_targets_the_canonical_version():
    from core.version import MILESTONE_TAG, VERSION
    module = _load(_QUALIFY, "_m61_qualify")
    assert module.VERSION == VERSION
    assert module.MILESTONE_TAG == MILESTONE_TAG


def test_qualification_references_only_real_test_files():
    module = _load(_QUALIFY, "_m61_qualify2")
    for relative in module._M61_TESTS + module._REGRESSION_TESTS:
        assert (_APP_ROOT / relative).is_file(), f"missing suite: {relative}"


def test_qualification_pins_m60_as_the_baseline():
    """M52-M60 must remain intact underneath a stabilization release."""
    assert "df35289d" in "".join(_string_literals(_QUALIFY))


# ── it cannot mutate ────────────────────────────────────────────────────────
#: git subcommands that write. None may appear in any argv the harness builds.
MUTATING_GIT_SUBCOMMANDS = frozenset({
    "commit", "merge", "push", "pull", "fetch", "checkout", "reset", "switch",
    "branch", "tag", "add", "rm", "mv", "rebase", "cherry-pick", "stash", "clean",
    "apply", "restore", "gc", "prune", "worktree", "remote", "config", "init",
})
#: The inspection subcommands the harness is allowed to run.
READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "rev-parse", "merge-base", "log", "show", "diff", "describe",
    "rev-list", "cat-file", "ls-files",
})


def _argv_lists(path: Path) -> list[list[str]]:
    """Every list literal of string constants — the shape of an argv vector.

    Dict KEYS are not argv, which is why this looks at list literals specifically:
    ``{"branch": ...}`` is a result field name, not a git subcommand, and a naive
    whole-file string scan reads it as one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            items = [e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if items:
                out.append(items)
    return out


@pytest.mark.parametrize("script", [_QUALIFY, _SOAK])
def test_harness_never_builds_a_mutating_git_argv(script):
    for argv in _argv_lists(script):
        if "git" not in argv and not any(
                a in READ_ONLY_GIT_SUBCOMMANDS for a in argv):
            continue
        for token in argv:
            assert token not in MUTATING_GIT_SUBCOMMANDS, \
                f"{script.name} builds a mutating git argv: {argv}"


def test_qualification_git_argv_are_inspection_only():
    """The harness's git calls are `["git", *args]`; check the *args* vectors."""
    source = _QUALIFY.read_text(encoding="utf-8")
    assert '["git", *args]' in source, "git argv construction changed shape"

    found_read_only = False
    for argv in _argv_lists(_QUALIFY):
        head = argv[0]
        if head in READ_ONLY_GIT_SUBCOMMANDS:
            found_read_only = True
        elif head in MUTATING_GIT_SUBCOMMANDS:
            pytest.fail(f"mutating git argv: {argv}")
    assert found_read_only, "no git inspection command found — did the gate vanish?"


@pytest.mark.parametrize("script", [_QUALIFY, _SOAK])
def test_harness_never_installs_or_configures(script):
    literals = " ".join(_string_literals(script))
    for forbidden in ("pip install", "ollama pull", "ollama serve", "winget",
                      "sc create", "schtasks", "New-Service", "--break-system-packages"):
        assert forbidden not in literals, f"{script.name} can run {forbidden!r}"


@pytest.mark.parametrize("script", [_QUALIFY, _SOAK])
def test_harness_uses_no_shell_and_no_eval(script):
    targets = _call_targets(script)
    assert "os.system" not in targets
    assert "eval" not in targets
    assert "exec" not in targets
    source = script.read_text(encoding="utf-8")
    assert "shell=True" not in source


def test_packaging_gate_builds_into_a_temporary_directory():
    source = _QUALIFY.read_text(encoding="utf-8")
    assert "tempfile.mkdtemp" in source
    assert "shutil.rmtree" in source, "the build directory is never removed"


# ── it cannot pass vacuously ────────────────────────────────────────────────
def test_missing_live_server_is_a_warning_not_a_pass():
    green = dict(deterministic_ok=True, regression_ok=True, ruff_ok=True,
                 compile_ok=True, soak_ok=True)
    assert release_verdict(**green, live_verdict="INSUFFICIENT_EVIDENCE") == \
        "PASS_WITH_WARNINGS"
    assert release_verdict(**green, live_verdict="FAIL") == "FAIL"
    assert release_verdict(**green, warnings=["live_not_requested"]) == \
        "PASS_WITH_WARNINGS"
    assert release_verdict(**green) == "PASS"


@pytest.mark.parametrize("failing", [
    "deterministic_ok", "regression_ok", "ruff_ok", "compile_ok", "soak_ok",
    "security_ok", "bounded_ok", "orphan_ok",
])
def test_any_mandatory_gate_red_is_a_fail(failing):
    gates = dict(deterministic_ok=True, regression_ok=True, ruff_ok=True,
                 compile_ok=True, soak_ok=True, security_ok=True, bounded_ok=True,
                 orphan_ok=True)
    gates[failing] = False
    assert release_verdict(**gates) == "FAIL"


def test_m61_specific_gates_also_force_a_fail():
    """release_verdict does not know about consistency/base-import/build/bandit.

    V69 M61.7 added ``bandit_ok`` to this conjunction: the qualification must not be
    able to report PASS while the blocking CI security gate would fail.
    """
    source = _QUALIFY.read_text(encoding="utf-8")
    assert (
        "if not (consistency_ok and base_ok and build_ok and bandit_ok):" in source
    )
    assert 'verdict = "FAIL"' in source


def test_the_bandit_gate_is_the_exact_ci_command():
    """The qualification must run the gate CI runs, not a weaker variant."""
    source = _QUALIFY.read_text(encoding="utf-8")
    assert '"-m", "bandit", "-r", "core", "tools", "-ll", "-q"' in source
    assert 'command="bandit -r core tools -ll -q"' in source


def test_a_missing_scanner_cannot_silently_pass_qualification():
    """JARVIS_REQUIRE_BANDIT turns "bandit absent" into a failure, not a skip."""
    source = _QUALIFY.read_text(encoding="utf-8")
    assert 'os.environ["JARVIS_REQUIRE_BANDIT"] = "1"' in source


def test_the_m617_security_suites_are_in_the_focused_set():
    """Otherwise a --quick qualification would not exercise the M61.7 remediations."""
    source = _QUALIFY.read_text(encoding="utf-8")
    for suite in (
        "test_bandit_gate_v69_m617.py",
        "test_security_hashes_v69_m617.py",
        "test_ssh_hostkeys_v69_m617.py",
        "test_plugin_exec_v69_m617.py",
        "test_url_and_xml_v69_m617.py",
        "test_network_binding_v69_m617.py",
    ):
        assert suite in source, suite
        assert (_APP_ROOT / "tests" / suite).is_file(), f"{suite} does not exist"


def test_skipping_a_gate_records_a_warning():
    source = _QUALIFY.read_text(encoding="utf-8")
    for warning in ("packaging_gate_skipped", "complete_suite_not_run",
                    "live_not_requested", "working_tree_not_clean"):
        assert warning in source


# ── the soak's assertions are real ──────────────────────────────────────────
def test_soak_growth_ceilings_are_declared():
    module = _load(_SOAK, "_m61_soak")
    assert module.MAX_TASK_GROWTH == 0, "an orphan task is a leak by definition"
    assert module.MAX_TEMPFILE_GROWTH == 0
    assert module.MAX_THREAD_GROWTH <= 2
    assert module.MAX_RSS_GROWTH_MB <= 128


def test_soak_fails_on_a_thread_leak():
    module = _load(_SOAK, "_m61_soak2")
    before = {"rss_mb": 10.0, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
              "journal_write_failures": 0, "journal_read_failures": 0,
              "restart_history_depth": 0, "temp_files": 0}
    after = dict(before, threads=40, journal_writes=10)
    checks = module._assertions(before, after, [{"actions_replayed": 0,
                                                 "recovered": True,
                                                 "unfinished_before_recovery": 1}],
                                {"diagnostics_built": True, "diagnostics_leaks": []})
    assert checks["no_thread_leak"] is False


def test_soak_fails_on_an_orphan_task():
    module = _load(_SOAK, "_m61_soak3")
    before = {"rss_mb": None, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
              "journal_write_failures": 0, "journal_read_failures": 0,
              "restart_history_depth": 0, "temp_files": 0}
    after = dict(before, asyncio_tasks=9, journal_writes=5)
    checks = module._assertions(before, after, [{"actions_replayed": 0,
                                                 "recovered": True,
                                                 "unfinished_before_recovery": 1}],
                                {"diagnostics_built": True, "diagnostics_leaks": []})
    assert checks["no_orphan_task"] is False


def test_soak_fails_when_an_effectful_action_is_replayed():
    """The M60 invariant: reconciliation has no execution path."""
    module = _load(_SOAK, "_m61_soak4")
    base = {"rss_mb": None, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
            "journal_write_failures": 0, "journal_read_failures": 0,
            "restart_history_depth": 0, "temp_files": 0}
    checks = module._assertions(base, dict(base, journal_writes=5),
                                [{"actions_replayed": 1, "recovered": True,
                                  "unfinished_before_recovery": 1}],
                                {"diagnostics_built": True, "diagnostics_leaks": []})
    assert checks["no_effectful_replay"] is False


def test_soak_diagnostics_check_cannot_pass_without_building():
    """The exact vacuous pass found on the soak's first run."""
    module = _load(_SOAK, "_m61_soak5")
    base = {"rss_mb": None, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
            "journal_write_failures": 0, "journal_read_failures": 0,
            "restart_history_depth": 0, "temp_files": 0}
    events = [{"actions_replayed": 0, "recovered": True,
               "unfinished_before_recovery": 1}]

    # An ImportError leaves no leaks recorded — which must NOT read as clean.
    broken = module._assertions(base, dict(base, journal_writes=5), events,
                                {"diagnostics_built": False,
                                 "diagnostics_error": "ImportError"})
    assert broken["diagnostics_built"] is False
    assert broken["diagnostics_content_free"] is False

    working = module._assertions(base, dict(base, journal_writes=5), events,
                                 {"diagnostics_built": True, "diagnostics_leaks": []})
    assert working["diagnostics_content_free"] is True


def test_soak_empty_backup_is_unproven_not_passed():
    module = _load(_SOAK, "_m61_soak6")
    base = {"rss_mb": None, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
            "journal_write_failures": 0, "journal_read_failures": 0,
            "restart_history_depth": 0, "temp_files": 0}
    events = [{"actions_replayed": 0, "recovered": True,
               "unfinished_before_recovery": 1}]
    checks = module._assertions(base, dict(base, journal_writes=5), events,
                                {"diagnostics_built": True, "diagnostics_leaks": [],
                                 "backup_verified": None})
    assert checks["backup_integrity_proven"] is False


def test_soak_fails_on_a_journal_write_failure():
    module = _load(_SOAK, "_m61_soak7")
    base = {"rss_mb": None, "threads": 4, "asyncio_tasks": 1, "journal_writes": 0,
            "journal_write_failures": 0, "journal_read_failures": 0,
            "restart_history_depth": 0, "temp_files": 0}
    after = dict(base, journal_writes=5, journal_write_failures=1)
    checks = module._assertions(base, after, [{"actions_replayed": 0,
                                               "recovered": True,
                                               "unfinished_before_recovery": 1}],
                                {"diagnostics_built": True, "diagnostics_leaks": []})
    assert checks["no_write_failure"] is False


def test_soak_cycle_count_is_bounded():
    source = _SOAK.read_text(encoding="utf-8")
    assert "min(int(args.cycles), 500)" in source, "the soak is not bounded"
