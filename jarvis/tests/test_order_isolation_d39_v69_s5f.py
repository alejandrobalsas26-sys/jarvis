"""V69 S5F — D39: order-independence of the export test surface.

THE DEFECT
----------
``test_training_gym_m62_s3g2_validation_wiring.py`` used to assert that importing the
export authority pulls in no ML framework by calling::

    importlib.reload(importlib.import_module("training_gym.datasets.export"))

``ExportError`` is DEFINED in that module. A reload re-executes it and binds a NEW class
object under the same ``__module__`` and ``__qualname__`` — so the exception still
*prints* identically, which is what hid this for so long. Every test module that had
already done ``from training_gym.datasets.export import ExportError`` kept a reference to
the OLD class. Production then raised the NEW one, and four
``pytest.raises(ExportError, ...)`` blocks in
``test_training_gym_m62_dataset_exports.py`` stopped catching it:

    test_every_record_being_revoked_is_reported_honestly
    test_an_altered_export_manifest_is_detected
    test_an_export_is_never_overwritten
    test_a_surviving_export_manifest_alone_still_blocks_a_re_export

Collection order decided everything. Alphabetically ``dataset_exports`` sorts before
``s3g2_validation_wiring``, so the authoritative run executed all four victims BEFORE the
reload and was green — a pass that depended on filename sort order, not on isolation.
Reversed, the same four failed. No recorded figure was ever wrong; the suite was simply
not order-independent.

V69 M61 RC1 had already forbidden this exact pattern on shared ``core`` modules, in
``test_managed_path_migration_v69_m61_rc1.py`` and ``test_managed_logging_v69_m614.py``,
with the reason stated verbatim: reloading "rebinds the module's classes, so a later test
catching ``managed_paths.UnsafeLeafName`` would compare against a different class object".
The export authority was the one place still doing it. The repair applies M61's own
prescription — measure import purity in a SUBPROCESS.

WHAT THIS FILE GUARDS
---------------------
Two independent sentinels, because either alone could rot:

1. BEHAVIOURAL — both known orders must pass in fresh interpreters. This fails on the
   pre-S5F source.
2. STRUCTURAL — no test may reload a module whose classes another test module (or it)
   binds at import time. This is the root cause stated as an invariant, so the defect
   cannot return through a different module. It also fails on the pre-S5F source.

The structural check deliberately permits ``importlib.reload`` where it is safe. The
evaluation-runner reloads ``training_gym.evaluation.backends`` while
``EvaluationBackendError`` is defined in ``training_gym.evaluation.backend`` — a different
module, so no identity churn reaches it. That call is not a latent D39 and is not flagged.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_APP_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _APP_ROOT.parent

#: The historically order-sensitive pair, victim first (the order CI happens to collect).
_VICTIM = "jarvis/tests/test_training_gym_m62_dataset_exports.py"
_CONTAMINATOR = "jarvis/tests/test_training_gym_m62_s3g2_validation_wiring.py"


def _run_pytest(*rel_paths: str) -> subprocess.CompletedProcess:
    """Run pytest over *rel_paths* in a FRESH interpreter, from the repository root.

    The repository root is the authoritative CI working directory (V69 S5E); running
    anywhere else changes what ``tests`` resolves to.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short",
         "-p", "no:cacheprovider", *rel_paths],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=900)


@pytest.mark.parametrize("first,second", [
    pytest.param(_VICTIM, _CONTAMINATOR, id="exports_then_wiring"),
    pytest.param(_CONTAMINATOR, _VICTIM, id="wiring_then_exports"),
])
def test_both_known_orders_pass_in_a_fresh_process(first, second):
    """D39's two orders. The second one failed with exactly 4 failures before S5F.

    Asserting on the RETURN CODE, not on stdout: a sentinel that greps for "passed"
    would be satisfied by a run that collected nothing.
    """
    for rel in (first, second):
        assert (_REPO_ROOT / rel).is_file(), f"sentinel is stale: {rel} is gone"
    result = _run_pytest(first, second)
    assert result.returncode == 0, (
        f"order {Path(first).stem} -> {Path(second).stem} is not isolated\n"
        f"{result.stdout[-3000:]}\n{result.stderr[-1000:]}")


# ── the root cause, stated as an invariant ───────────────────────────────────
def _reloaded_modules(tree: ast.AST) -> set[str]:
    """Module names passed to ``importlib.reload`` in *tree*, best-effort.

    Resolves the two forms the test tree actually uses: a literal
    ``importlib.reload(importlib.import_module("x.y"))`` and ``importlib.reload(alias)``
    where *alias* came from an ``import x.y as alias``.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name

    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reload" and node.args):
            continue
        arg = node.args[0]
        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "import_module" and arg.args
                and isinstance(arg.args[0], ast.Constant)
                and isinstance(arg.args[0].value, str)):
            found.add(arg.args[0].value)
        elif isinstance(arg, ast.Name) and arg.id in aliases:
            found.add(aliases[arg.id])
    return found


def _module_source(dotted: str) -> Path | None:
    base = _APP_ROOT / Path(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _classes_defined_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef)}


def _module_scope_imports(tree: ast.AST) -> set[tuple[str, str]]:
    """``(module, name)`` pairs bound by top-level ``from module import name``."""
    return {(n.module, a.name) for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
            for a in n.names}


def test_no_test_reloads_a_module_whose_classes_are_bound_elsewhere():
    """The D39 mechanism as a rule: reload rebinds classes; a held reference goes stale.

    Reloading is only safe when nothing holds a class from the reloaded module across
    the reload. This walks every test module, finds each ``importlib.reload`` target,
    and reports any class that target DEFINES which some test module also imports by
    name at module scope — the precise condition under which ``pytest.raises`` and
    ``isinstance`` silently stop matching.
    """
    sources = sorted(_TESTS_DIR.glob("test_*.py"))
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in sources}
    imports = {path: _module_scope_imports(tree) for path, tree in trees.items()}

    violations: list[str] = []
    for path, tree in trees.items():
        for dotted in sorted(_reloaded_modules(tree)):
            source = _module_source(dotted)
            if source is None:  # not a first-party module we can inspect
                continue
            defined = _classes_defined_in(source)
            if not defined:
                continue
            for holder, pairs in imports.items():
                stale = sorted(name for module, name in pairs
                               if module == dotted and name in defined)
                if stale:
                    violations.append(
                        f"{path.name} reloads {dotted}, but {holder.name} binds "
                        f"{', '.join(stale)} from it at import time")
    assert violations == [], (
        "in-process importlib.reload of a module whose classes are held elsewhere — "
        "this is D39. Measure the property in a subprocess instead (see V69 M61 RC1 "
        "and jarvis/docs/V69_S5F_D39_ORDER_ISOLATION.md):\n  "
        + "\n  ".join(violations))
