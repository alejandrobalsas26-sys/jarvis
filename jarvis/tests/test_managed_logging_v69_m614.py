"""tests/test_managed_logging_v69_m614.py — V69 M61.4: logs land where we own them.

The proof that motivated this module was sitting in the working tree: a 2.7 MB
``jarvis.log`` at the repository root AND a second one inside ``jarvis/``, because
``main.py`` configured its file sink as the CWD-relative string ``"jarvis.log"`` and
the app had been launched from both directories. Under the M60.5 startup/scheduler
deployment targets the CWD is whatever the service manager picked, so the most
content-bearing artifact the runtime produces would land somewhere nobody would look.

These tests pin four things:

  * every managed path is ABSOLUTE and inside the application tree;
  * importing the runtime from a foreign CWD creates nothing there;
  * rotation and retention are declared, and the sink redacts on the way to disk;
  * the failures that must never be silent are not silent any more.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import managed_logging, managed_paths

_APP_ROOT = Path(__file__).resolve().parent.parent


# ── managed paths are absolute and owned ─────────────────────────────────────
@pytest.mark.parametrize("resolver", [
    managed_paths.logs_dir,
    managed_paths.runtime_log_path,
    managed_paths.audit_log_path,
])
def test_managed_log_paths_are_absolute_and_inside_the_app(resolver):
    path = resolver(create=False)
    assert path.is_absolute()
    assert managed_paths.app_root() in path.parents or path.parent == managed_paths.app_root()


def test_runtime_log_is_inside_the_managed_logs_dir():
    assert managed_logging.runtime_log_file().parent == managed_paths.logs_dir(create=False)
    assert managed_logging.runtime_log_file().name == "jarvis.log"


def test_audit_log_is_inside_the_managed_logs_dir():
    assert managed_logging.audit_log_file().name == "tactic_audit.jsonl"
    assert managed_logging.audit_log_file().parent == managed_paths.logs_dir(create=False)


def test_managed_paths_are_independent_of_the_cwd(tmp_path, monkeypatch):
    """The whole point: the same paths from any working directory."""
    before = managed_logging.runtime_log_file()
    monkeypatch.chdir(tmp_path)
    assert managed_logging.runtime_log_file() == before
    assert managed_logging.audit_log_file().is_absolute()


def test_describe_reports_relative_paths_only():
    """A diagnostics bundle must not carry the operator's home directory."""
    described = managed_logging.describe()
    for key in ("logs_dir", "runtime_log", "audit_log"):
        assert not Path(described[key]).is_absolute()
        assert ":" not in described[key]
    assert described["redacted"] is True


# ── no CWD scatter ───────────────────────────────────────────────────────────
def test_importing_the_runtime_creates_nothing_in_the_cwd(tmp_path):
    """Import-time side effects were real: session_journal used to mkdir on import.

    Run in a SUBPROCESS with the temp directory as its CWD. That is the real scenario
    (a service manager launching from an arbitrary directory), and it avoids
    ``importlib.reload`` on shared core modules — reloading rebinds the module's
    classes, so a later suite catching ``managed_paths.UnsafeLeafName`` would be
    comparing against a different class object and fail for a reason that has nothing
    to do with the code under test.
    """
    import subprocess
    import sys

    script = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(_APP_ROOT)!r})\n"
        "import core.session_journal, core.session_manager\n"
        "import core.managed_logging, core.managed_paths, main\n"
        "print('\\n'.join(sorted(os.listdir('.'))))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"import failed: {result.stderr[-400:]}"
    residue = [line for line in result.stdout.splitlines() if line.strip()]
    assert residue == [], f"import scattered {residue} into the CWD"


def test_continuity_modules_no_longer_mkdir_at_import_time():
    """`Path("logs/...").mkdir()` at module scope is the pattern that caused this.

    Bounded rather than zero: 12 feature modules still do it, and migrating them is
    the broad rewrite M61 refuses. What must be true is that the modules holding
    conversation state and the shutdown audit trail are OUT of that set, and that the
    set does not grow.
    """
    offenders = managed_logging.import_time_mkdir_modules()
    for fixed in ("session_journal.py", "session_manager.py", "shutdown_manager.py"):
        assert fixed not in offenders, f"core/{fixed} still creates a directory on import"
    assert len(offenders) <= 12, f"import-time mkdir spread to: {offenders}"


@pytest.mark.parametrize("module", [
    "session_journal.py", "session_manager.py", "shutdown_manager.py",
])
def test_critical_path_module_resolves_through_managed_paths(module):
    assert managed_logging.module_uses_managed_paths(module), \
        f"core/{module} must resolve its artifacts through managed_paths"


def _string_literals(path: Path) -> list[str]:
    """Every string constant in *path* that is NOT a docstring.

    Checked over the AST because several modules now carry prose EXPLAINING the old
    CWD-relative pattern, and a description of a defect must not read as the defect.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def test_main_no_longer_passes_a_bare_filename_to_logger_add():
    literals = _string_literals(_APP_ROOT / "main.py")
    assert "jarvis.log" not in literals, "main.py still names a CWD-relative log file"
    assert "install_file_sink" in (_APP_ROOT / "main.py").read_text(encoding="utf-8")


# ── rotation, retention and redaction ────────────────────────────────────────
def test_rotation_and_retention_are_declared_once():
    assert managed_logging.ROTATION == "10 MB"
    assert managed_logging.RETENTION == "7 days"
    assert managed_logging.LEVEL == "DEBUG"


def test_file_sink_applies_rotation_and_retention(tmp_path):
    from loguru import logger

    captured = {}
    real_add = logger.add

    def _spy(sink, **kwargs):
        captured.update(kwargs)
        return real_add(sink, **kwargs)

    class _Spy:
        add = staticmethod(_spy)

    sink_id = managed_logging.install_file_sink(_Spy, path=tmp_path / "t.log")
    try:
        assert captured["rotation"] == managed_logging.ROTATION
        assert captured["retention"] == managed_logging.RETENTION
        assert captured["level"] == managed_logging.LEVEL
        assert captured["filter"] is managed_logging.redacting_filter
    finally:
        if sink_id is not None:
            logger.remove(sink_id)


def test_sink_redacts_an_otp_before_it_reaches_disk(tmp_path):
    from loguru import logger

    target = tmp_path / "redacted.log"
    sink_id = managed_logging.install_file_sink(logger, path=target)
    try:
        logger.debug("authorization code 483927 accepted")
        logger.complete()
    finally:
        logger.remove(sink_id)

    written = target.read_text(encoding="utf-8", errors="replace")
    assert "483927" not in written, "an OTP reached the log file"


def test_redacting_filter_never_drops_a_record():
    """Dropping a line would hide a failure — the message is rewritten, not the record."""
    record = {"message": "plain message"}
    assert managed_logging.redacting_filter(record) is True
    assert record["message"]


def test_redacting_filter_survives_a_redaction_failure(monkeypatch):
    import core.redaction_policy as rp

    def _boom(*a, **k):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr(rp, "redact_text", _boom)
    record = {"message": "still important"}
    assert managed_logging.redacting_filter(record) is True


# ── the residual CWD exposure is MEASURED, not assumed away ─────────────────
def test_cwd_relative_log_inventory_is_bounded_and_shrinking():
    """M61.4 fixed the critical control paths; the feature-artifact residue is pinned.

    This is deliberately a ceiling, not zero: migrating ~30 feature modules is the
    broad rewrite M61 refuses. The number may go DOWN in a later milestone; it must
    never go up without someone changing this line on purpose.
    """
    residue = managed_logging.cwd_relative_log_modules()
    assert len(residue) <= 24, f"CWD-relative log paths grew: {residue}"
    # The paths on the critical control path are OUT of the residue.
    for fixed in ("session_journal.py", "session_manager.py", "shutdown_manager.py"):
        assert fixed not in residue


# ── failures that must never be silent ───────────────────────────────────────
def _source(module: str) -> str:
    return (_APP_ROOT / "core" / module).read_text(encoding="utf-8")


def test_shutdown_audit_write_failure_is_reported():
    literals = _string_literals(_APP_ROOT / "core" / "shutdown_manager.py")
    assert "logs" not in literals, "shutdown audit still resolves against the CWD"
    assert "logs/tactic_audit.jsonl" not in literals
    assert "audit trail write FAILED" in _source("shutdown_manager.py")


def test_shutdown_signal_handler_failure_is_reported():
    source = _source("shutdown_manager.py")
    assert "SIGINT handler NOT installed" in source
    assert "could not set the shutdown event" in source


def test_watchdog_stop_failure_is_reported():
    assert "watchdog could NOT be stopped" in _source("shutdown_manager.py")


def test_crash_resume_snapshot_failure_is_reported():
    source = _source("session_manager.py")
    assert "will not be resumable" in source


def test_console_coordinator_failure_is_reported():
    source = (_APP_ROOT / "main.py").read_text(encoding="utf-8")
    assert "console coordinator unavailable" in source


@pytest.mark.parametrize("module,marker", [
    ("shutdown_manager.py", "audit trail write FAILED"),
    ("session_manager.py", "crash-resume snapshot NOT saved"),
])
def test_failure_messages_are_content_free(module, marker):
    """They may name the exception TYPE; they must not interpolate its message."""
    source = _source(module)
    index = source.index(marker)
    window = source[index - 400:index + 400]
    assert "type(e).__name__" in window or "type(exc).__name__" in window
    assert "{e}" not in window.replace("{e.__", ""), "raw exception message logged"
