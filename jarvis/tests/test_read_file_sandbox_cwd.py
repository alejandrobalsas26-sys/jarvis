"""M61 RC1 — the filesystem sandbox verdict must not depend on the CWD.

Two defects are pinned here.

1. **Ambiguous fixture.** The legacy traversal regression asked ``read_file`` for
   the literal string ``../../etc/passwd``. ``..`` is resolved against the
   *process CWD*, so that one string denoted a path outside every authorized
   root when pytest ran from the repository root, and a path *inside*
   ``~/Downloads`` when it ran from the application directory — this checkout
   lives under Downloads. The guard was right both times; the test's premise was
   only true from one directory. A security regression test whose meaning is a
   function of the invocation directory cannot certify a release.

2. **Two containment implementations.** ``_resolve_within_allowed`` documented
   itself as mirroring the checks inlined in ``_tool_read_file`` and
   ``_tool_write_file`` — but the handlers never called it, so the "one hardened
   definition" existed in three copies free to drift apart. Only the screenshot
   handler used the shared one.

These tests assert the *invariant* (identical verdict from any CWD, exactly one
containment implementation), never a translated message or a host path.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

import tools.executor as executor_mod
from tools.executor import (
    ERR_FILE_NOT_FOUND,
    ERR_PATH_NOT_ALLOWED,
    ToolExecutor,
    _resolve_within_allowed,
)

SENTINEL = "M61-RC1-SANDBOX-SENTINEL"

EXECUTOR_SOURCE = Path(executor_mod.__file__)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """An authorized root, an out-of-root sentinel, and a pinned CWD."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_text(SENTINEL, encoding="utf-8")

    monkeypatch.setattr(executor_mod, "_sandbox_allowed_dirs", lambda: (root,))
    monkeypatch.chdir(root)
    return root, outside


# ── The verdict is invariant under the invocation directory ──────────────────

@pytest.mark.parametrize(
    "cwd_kind",
    ["repo_root", "app_dir", "unrelated_tmp", "allowed_root"],
    ids=["from-repo-root", "from-app-dir", "from-unrelated-tmp", "from-allowed-root"],
)
def test_escape_verdict_is_cwd_invariant(monkeypatch, tmp_path, cwd_kind):
    """An absolute path outside the authorized root is denied from every CWD.

    The path is absolute, so it carries no dependence on the CWD; the CWD is
    varied only to prove the *guard* carries none either.
    """
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text(SENTINEL, encoding="utf-8")

    app_dir = EXECUTOR_SOURCE.parent.parent
    cwds = {
        "repo_root": app_dir.parent,
        "app_dir": app_dir,
        "unrelated_tmp": tmp_path,
        "allowed_root": root,
    }

    monkeypatch.setattr(executor_mod, "_sandbox_allowed_dirs", lambda: (root,))
    monkeypatch.chdir(cwds[cwd_kind])

    result = ToolExecutor().execute("read_file", {"path": str(sentinel)})

    assert result.get("error_code") == ERR_PATH_NOT_ALLOWED
    assert SENTINEL not in repr(result)


def test_relative_traversal_denied_from_every_cwd(monkeypatch, tmp_path):
    """``../`` interpreted from inside the root always escapes and is denied.

    Each iteration re-pins the CWD to the authorized root — the only directory
    against which a relative traversal has a defined meaning — and asserts the
    verdict does not drift as the *starting* directory of the process changes.
    """
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_text(SENTINEL, encoding="utf-8")
    monkeypatch.setattr(executor_mod, "_sandbox_allowed_dirs", lambda: (root,))

    app_dir = EXECUTOR_SOURCE.parent.parent
    verdicts = set()
    original = Path.cwd()
    try:
        for start in (app_dir.parent, app_dir, tmp_path):
            os.chdir(start)
            os.chdir(root)  # the sandboxed process' working directory
            result = ToolExecutor().execute(
                "read_file", {"path": "../outside/sentinel.txt"}
            )
            assert SENTINEL not in repr(result)
            verdicts.add(result.get("error_code"))
    finally:
        os.chdir(original)

    assert verdicts == {ERR_PATH_NOT_ALLOWED}


def test_in_scope_missing_file_is_not_reported_as_traversal(sandbox):
    """Fail-closed must not mean fail-opaque: the two refusals stay distinct."""
    result = ToolExecutor().execute("read_file", {"path": "absent.txt"})
    assert result.get("error_code") == ERR_FILE_NOT_FOUND


def test_in_scope_file_still_readable(sandbox):
    """Positive control — the guard is not denying unconditionally."""
    root, _ = sandbox
    (root / "ok.md").write_text("authorized", encoding="utf-8")

    result = ToolExecutor().execute("read_file", {"path": "ok.md"})

    assert "error" not in result
    assert result["content"] == "authorized"


# ── Exactly one containment implementation (AST, not source text) ────────────

def _executor_tree() -> ast.Module:
    return ast.parse(EXECUTOR_SOURCE.read_text(encoding="utf-8"))


def _functions_calling(tree: ast.Module, attr: str) -> set[str]:
    """Names of every function whose body calls ``<something>.<attr>(...)``."""
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == attr
            ):
                hits.add(node.name)
    return hits


def test_containment_check_has_exactly_one_implementation():
    """``is_relative_to`` may only be called by the shared resolver.

    Any other caller is a second, independently drifting copy of the sandbox —
    which is how the ambiguity above survived. Scanned via AST so the assertion
    cannot be satisfied (or broken) by prose in a comment or docstring.
    """
    callers = _functions_calling(_executor_tree(), "is_relative_to")
    assert callers == {"_resolve_within_allowed"}, (
        f"containment re-implemented outside the shared resolver: "
        f"{sorted(callers - {'_resolve_within_allowed'})}"
    )


def test_path_taking_handlers_use_the_shared_resolver():
    """Negative control for the scanner above: it can actually see call sites."""
    tree = _executor_tree()
    callers = _functions_calling(tree, "expanduser")
    assert "_resolve_within_allowed" in callers, (
        "AST scan found no expanduser() call inside the resolver — the scanner "
        "is broken, so the assertion above proves nothing"
    )

    resolver_users = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "_resolve_within_allowed"
    }
    assert {"_tool_read_file", "_tool_write_file"} <= resolver_users


# ── Fail-closed shapes the resolver must reject ──────────────────────────────

@pytest.mark.parametrize(
    "bad", ["", "   ", "../escape", "sub/../../escape", "..\\escape"]
)
def test_resolver_rejects_escapes(monkeypatch, tmp_path, bad):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(executor_mod, "_sandbox_allowed_dirs", lambda: (root,))
    monkeypatch.chdir(root)

    resolved = _resolve_within_allowed(bad)

    # On POSIX a backslash is an ordinary filename character, so "..\escape"
    # stays inside the root; what must never happen is resolving OUTSIDE it.
    assert resolved is None or resolved.is_relative_to(root.resolve())
