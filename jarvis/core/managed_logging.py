"""core/managed_logging.py — V69 M61.4: the runtime log sink, in a path we own.

THE DEFECT THIS FIXES
---------------------
``main.py`` configured its file sink as ``logger.add("jarvis.log", …)`` — a
CWD-relative string, evaluated wherever the process happened to start. The evidence
was sitting in the working tree: a 2.7 MB ``jarvis.log`` at the repository root AND a
second 2.7 MB ``jarvis.log`` inside ``jarvis/``, because the app had been launched
from both. Under the M60.5 startup/scheduler deployment targets the CWD is whatever
the service manager chose, so the DEBUG log — the most content-bearing artifact the
runtime produces — would land in an arbitrary directory and never be found again.

``core.managed_paths`` already solved this for the M60 artifacts. This module applies
the same resolver to logging, and adds the two things a DEBUG file sink specifically
needs:

  * **rotation and retention that are declared, not hoped for** — one bounded
    rotating file, with the size and retention exposed so a test can assert them;
  * **a redaction filter on the way to disk** — the sink reuses the M60 deterministic
    redaction pipeline (``core.redaction_policy``), so an OTP, a credential-shaped
    token or the operator's home directory cannot reach the log file even if some
    call site formats one into a message. Prompts, responses and tool arguments are
    not logged by policy; this filter is the backstop for when policy is violated by
    accident, not a licence to log them.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not own the console sink (``core.console`` does) and it does not replace
loguru.

V69 M61 RC1 — WHAT CHANGED SINCE THE PARAGRAPH ABOVE
----------------------------------------------------
M61.4 shipped with the migration deliberately unfinished: the runtime log sink and
the shutdown audit trail were fixed, and the ~30 remaining feature modules were left
resolving ``Path("logs/…")`` against the CWD, inventoried behind a ceiling so the
exposure was at least measured. RC1 finished the job. The active runtime surface —
``core/``, ``tools/``, ``aura/`` and the entry points — now holds ZERO relative
multi-segment path literals and ZERO import-time directory creations, and the
inventories below assert exactly that rather than a ceiling. They are kept, and
broadened, because an inventory that reads zero is the only kind that cannot drift
back up unnoticed.
"""
from __future__ import annotations

import ast
from pathlib import Path

from core.managed_paths import app_root, audit_log_path, logs_dir, runtime_log_path

#: Rotation size and retention for the runtime DEBUG log. Declared here so the
#: policy is one value, assertable by a test, instead of a literal repeated at each
#: ``logger.add`` call site (main.py had it twice, and they could drift).
ROTATION = "10 MB"
RETENTION = "7 days"
LEVEL = "DEBUG"

#: Relative artifact roots a module may not build for itself any more.
_CWD_RELATIVE_ROOTS = ("logs/", "logs", "data/", "data")


def runtime_log_file() -> Path:
    """The absolute path of the rotating runtime log."""
    return runtime_log_path()


def audit_log_file() -> Path:
    """The absolute path of the append-only audit trail."""
    return audit_log_path()


def redacting_filter(record) -> bool:
    """Loguru filter that redacts the message IN PLACE before it reaches the file.

    Returns True (keep the record) always: dropping a log line would hide a failure,
    which is the opposite of what M61.4 is for. The message is rewritten, not the
    decision to record it.
    """
    try:
        from core.redaction_policy import redact_text
        message = record.get("message")
        if isinstance(message, str) and message:
            cleaned, _report = redact_text(message, max_chars=8000)
            record["message"] = cleaned
    except Exception:  # noqa: BLE001
        # A redaction failure must never suppress the log line, and must never
        # raise inside loguru's dispatch. The unredacted message is still bounded
        # by the sink's own format; the risk of losing the line is worse.
        return True
    return True


def install_file_sink(logger, *, path: Path | None = None,
                      rotation: str = ROTATION, retention: str = RETENTION,
                      level: str = LEVEL) -> int | None:
    """Attach the managed rotating file sink to *logger*.

    Returns the loguru sink id, or ``None`` if the sink could not be created (a
    read-only tree or a full disk). A failure is REPORTED by the caller, never
    swallowed here — the caller decides whether running without a file log is
    acceptable, and says so on the console.
    """
    target = Path(path) if path is not None else runtime_log_file()
    return logger.add(
        str(target),
        level=level,
        rotation=rotation,
        retention=retention,
        filter=redacting_filter,
        enqueue=False,
        catch=True,
    )


def describe() -> dict:
    """Bounded, content-free description of the logging layout (diagnostics).

    Paths are RELATIVE to the application root, so a diagnostics bundle built from
    this never carries the operator's home directory.
    """
    root = app_root()
    try:
        runtime_rel = runtime_log_file().relative_to(root).as_posix()
        audit_rel = audit_log_file().relative_to(root).as_posix()
    except ValueError:  # pragma: no cover — both are constructed under root
        runtime_rel = audit_rel = "<outside app root>"
    return {
        "logs_dir": logs_dir(create=False).relative_to(root).as_posix(),
        "runtime_log": runtime_rel,
        "audit_log": audit_rel,
        "rotation": ROTATION,
        "retention": RETENTION,
        "level": LEVEL,
        "redacted": True,
    }


def cwd_relative_log_modules() -> list[str]:
    """Inventory the ``core/`` modules that still build ``Path("logs/…")`` themselves.

    M61.4 fixed the runtime log sink and the shutdown audit trail — the two paths on
    the critical control path. The remaining modules write feature artifacts (hunt
    results, reports, campaign logs) and migrating all of them is the broad rewrite
    this milestone refuses. This function makes the residue MEASURABLE: a test pins
    the count, so the exposure cannot grow silently and a future milestone can drive
    it to zero with evidence.

    Detection is over the AST, never the raw text: several modules now carry a
    docstring EXPLAINING the old ``Path("logs/…")`` pattern, and prose about a defect
    must not be counted as the defect.
    """
    found = []
    for path, tree in _core_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "Path" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                    and first.value.startswith(_CWD_RELATIVE_ROOTS):
                found.append(path.name)
                break
    return found


def import_time_mkdir_modules() -> list[str]:
    """Inventory the ``core/`` modules that create a directory AT IMPORT TIME.

    Merely importing a module should not touch the filesystem — it happens inside
    test collection, packaging tools and static analysis, in whatever directory those
    run from. ``session_journal`` and ``session_manager`` did exactly this and were
    fixed in M61.4; the rest are feature modules, inventoried here so the count is a
    measured ceiling rather than an assumption.
    """
    found = []
    for path, tree in _core_modules():
        for node in tree.body:  # module scope ONLY — a function body is not import time
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == "mkdir":
                    found.append(path.name)
                    break
    return found


def _core_modules():
    """Yield ``(path, ast)`` for every parseable module in ``core/``."""
    for path in sorted((app_root() / "core").glob("*.py")):
        try:
            yield path, ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue


#: Directories that make up the ACTIVE runtime surface, plus the entry points. Tests
#: and docs are excluded on purpose: a fixture may legitimately build a relative path
#: inside its own ``tmp_path``, and prose is not code.
_RUNTIME_TREES = ("core", "tools", "aura")


def _runtime_modules():
    """Yield ``(relative_name, ast)`` for every parseable ACTIVE runtime module."""
    root = app_root()
    paths = []
    for tree in _RUNTIME_TREES:
        paths.extend(sorted((root / tree).rglob("*.py")))
    paths.extend(p for p in (root / "main.py", root / "__main__.py") if p.exists())
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        try:
            yield path.relative_to(root).as_posix(), ast.parse(
                path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue


def runtime_relative_path_literals() -> list[str]:
    """Every ACTIVE module that still builds a multi-segment RELATIVE path literal.

    Broader than :func:`cwd_relative_log_modules` in two ways that matter: it covers
    ``tools/`` and ``aura/`` as well as ``core/``, and it is not limited to the
    ``logs/`` and ``data/`` roots — ``Path("core/sigma_rules")`` and
    ``Path("tools/external")`` were the same defect wearing a different prefix, and
    the narrower inventory reported them as clean.

    A single-segment literal (``Path("jarvis.log")``) is NOT matched here; that is
    the ``logger.add`` defect, and it has its own dedicated test.

    Returns ``"<module>:<line>"`` entries so a regression names the exact site.
    """
    found = []
    for name, tree in _runtime_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            call = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if call not in ("Path", "open", "makedirs"):
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant)
                    and isinstance(first.value, str) and first.value):
                continue
            value = first.value
            if value.startswith(("/", "~")) or (len(value) > 1 and value[1] == ":"):
                continue  # already absolute
            if "/" in value or "\\" in value:
                found.append(f"{name}:{node.lineno}")
    return found


def runtime_import_time_mkdir() -> list[str]:
    """Every ACTIVE module that creates a directory AT IMPORT TIME, tree-wide.

    Module scope only — a ``mkdir`` inside a function body is lazy creation, which is
    the pattern this milestone migrated TO.
    """
    found = []
    for name, tree in _runtime_modules():
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr in (
                        "mkdir", "makedirs"):
                    found.append(f"{name}:{node.lineno}")
    return found


def module_uses_managed_paths(module_name: str) -> bool:
    """True when ``core/<module_name>.py`` imports its paths from managed_paths."""
    path = app_root() / "core" / module_name
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "managed_paths"):
            return True
        if isinstance(node, ast.Import):
            if any(a.name.endswith("managed_paths") for a in node.names):
                return True
    return False
