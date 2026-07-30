"""tests/test_managed_path_migration_v69_m61_rc1.py — V69 M61 RC1.2.

M61.4 fixed the two paths on the critical control path (the runtime log sink and the
shutdown audit trail) and left ~30 feature modules resolving ``Path("logs/…")``
against the process CWD, behind a measured ceiling. RC1.2 finished the migration.

WHY THE RESIDUE WAS NOT MERELY UNTIDY
-------------------------------------
Three of the migrated paths were actively lying to the operator:

  * ``persistence_hunter`` kept its baseline of already-alerted autoruns in a
    CWD-relative file. Read from the wrong directory it came back EMPTY, and an
    empty baseline is indistinguishable from a clean sweep;
  * ``daily_briefing``, ``detection_engineer`` and ``sigma_generator`` each built
    their own path to the SAME Sigma rule directory, two CWD-relative and one
    anchored on ``__file__``. A briefing run from anywhere but the repository root
    reported "0 drafts awaiting approval" while drafts were waiting;
  * ``self_test`` and ``intel_fusion`` did the same for the intel database, so the
    self-test reported "will create on first ingest" about a database already
    holding the operator's whole intel history.

The tests below prove the properties that make those failures impossible, and — the
part that matters for a release gate — they assert ZERO rather than a ceiling. A
ceiling above the real number is not a test: 24 kept passing after every module was
fixed, and would have gone on passing while new ones regressed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core import managed_logging, managed_paths

_APP_ROOT = Path(__file__).resolve().parent.parent

#: The modules RC1.2 migrated, plus the M61.4 ones they must not regress. Imported
#: individually in a subprocess so a side effect is attributed to its own module.
_MIGRATED_MODULES = [
    "core.managed_paths", "core.managed_logging",
    # M61.4 / earlier RC1 checkpoint
    "core.punisher", "core.mobile_c2", "core.vss_vaccine",
    "core.network_quarantine", "core.decoy_service", "core.tarpit_deception",
    "core.persistence_hunter", "core.target_aliases", "core.intel_fusion",
    "core.self_test", "core.auto_remediator", "core.forensic_reporter",
    "core.incident_reporter",
    # RC1.2
    "core.code_intel", "core.detection_engineer", "core.github_explorer",
    "core.hunt_scheduler", "core.proxy_intel", "core.red_team_operator",
    "core.sensor_mesh", "core.sigma_generator", "core.ir_reporter",
    "core.coverage_reporter", "core.detection_harness",
    "core.industrial_asset_guard", "core.vision_engine", "core.daily_briefing",
    "tools.browser_intel", "tools.diagram_generator", "tools.docker_manager",
    "tools.ioc_extractor", "tools.memory_hunter",
]


def _function_source(module: str, function: str) -> str:
    """Exact source of ONE function, bounded by the AST rather than by ``split``.

    Splitting on ``"\\ndef "`` silently runs past the end of the function when the
    next definition is an ``async def`` — which made an earlier version of the
    punisher assertion read a ``try/except`` belonging to a different function.
    """
    import ast

    path = _APP_ROOT / "core" / module
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == function:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"core/{module} no longer defines {function}()")


def _import_in(cwd: Path, *modules: str) -> subprocess.CompletedProcess:
    """Import *modules* in a subprocess whose CWD is *cwd*. Never reloads in-process.

    ``importlib.reload`` on a shared ``core`` module rebinds its classes, so a later
    test catching ``managed_paths.UnsafeLeafName`` would compare against a different
    class object and fail for a reason unrelated to the code under test.
    """
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_APP_ROOT)!r})\n"
        + "".join(f"import {m}\n" for m in modules)
    )
    return subprocess.run([sys.executable, "-c", script], cwd=str(cwd),
                          capture_output=True, text=True, timeout=180)


# ── 1. importing an active module must not touch the filesystem ──────────────
@pytest.mark.parametrize("module", _MIGRATED_MODULES)
def test_importing_a_migrated_module_creates_nothing_in_the_cwd(module, tmp_path):
    """Import is not a licence to write. This ran during test collection and packaging.

    ``auto_remediator`` created ``logs/mitigations``, ``github_explorer`` created
    ``tools/external``, and ``aura.server``'s ``static/meshes`` mkdir was load-bearing
    for its own ``StaticFiles`` mount — an accident, in whatever directory the
    importer happened to be standing.
    """
    workdir = tmp_path / module.replace(".", "_")
    workdir.mkdir()
    result = _import_in(workdir, module)
    assert result.returncode == 0, f"import failed: {result.stderr[-500:]}"
    residue = sorted(p.name for p in workdir.iterdir())
    assert residue == [], f"importing {module} scattered {residue} into the CWD"


def test_importing_the_whole_migrated_surface_at_once_is_clean(tmp_path):
    """The modules also must not create anything COLLECTIVELY (import order effects)."""
    result = _import_in(tmp_path, *_MIGRATED_MODULES)
    assert result.returncode == 0, f"import failed: {result.stderr[-500:]}"
    residue = sorted(p.name for p in tmp_path.iterdir())
    assert residue == [], f"the migrated surface scattered {residue} into the CWD"


# ── 2. two different CWDs resolve to the SAME artifact ───────────────────────
_RESOLVERS = [
    "managed_paths.runtime_log_path", "managed_paths.audit_log_path",
    "managed_paths.continuity_db_path", "managed_paths.sigma_rules_dir",
    "core.intel_fusion.db_path", "core.code_intel.inbox_dir",
]


@pytest.mark.parametrize("resolver", _RESOLVERS)
def test_two_different_cwds_resolve_to_the_same_artifact(resolver, tmp_path):
    """The property the whole migration exists for. Two CWDs, one file."""
    module, _, attr = resolver.rpartition(".")
    module = module if module.startswith("core") else f"core.{module}"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_APP_ROOT)!r})\n"
        f"import {module} as m\n"
        f"print(m.{attr}(create=False))\n"
    )
    seen = set()
    for name in ("alpha", "beta"):
        workdir = tmp_path / name
        workdir.mkdir()
        result = subprocess.run([sys.executable, "-c", script], cwd=str(workdir),
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, f"{resolver} failed: {result.stderr[-400:]}"
        seen.add(result.stdout.strip())
    assert len(seen) == 1, f"{resolver} resolved differently per CWD: {seen}"


# ── 3. every managed path stays under the managed root ───────────────────────
@pytest.mark.parametrize("factory", [
    lambda: managed_paths.logs_subdir("hunt_results", create=False),
    lambda: managed_paths.logs_subdir("visuals", "browser", create=False),
    lambda: managed_paths.log_artifact_path("topology.json", create=False),
    lambda: managed_paths.app_subdir("analyze_inbox", create=False),
    lambda: managed_paths.sigma_rules_dir(create=False),
])
def test_managed_paths_stay_under_the_application_root(factory):
    target = factory()
    assert target.is_absolute()
    assert target.resolve().is_relative_to(managed_paths.app_root().resolve())


@pytest.mark.parametrize("evil", [
    "../escape", "..", "/etc/passwd", "a/b", "a\\b", ".hidden", "con", "nul.txt", "",
])
def test_the_new_accessors_reject_an_escaping_segment(evil):
    """``logs_subdir`` and ``app_subdir`` take VARIADIC segments as of RC1.

    Each segment is validated individually, so accepting more than one of them did
    not open a hole: there is no point at which a separator is joined into a string.
    """
    with pytest.raises(managed_paths.UnsafeLeafName):
        managed_paths.logs_subdir(evil, create=False)
    with pytest.raises(managed_paths.UnsafeLeafName):
        managed_paths.app_subdir(evil, create=False)
    with pytest.raises(managed_paths.UnsafeLeafName):
        managed_paths.logs_subdir("visuals", evil, create=False)


def test_the_new_accessors_reject_an_empty_segment_list():
    with pytest.raises(managed_paths.UnsafeLeafName):
        managed_paths.logs_subdir(create=False)
    with pytest.raises(managed_paths.UnsafeLeafName):
        managed_paths.app_subdir(create=False)


# ── 4. create=False creates nothing; 5. writes create lazily ─────────────────
@pytest.mark.parametrize("name", ["rc1_probe_dir", "rc1_probe_dir_two"])
def test_create_false_performs_no_directory_creation(name):
    """A READ must never materialise a tree. ``persistence_hunter`` reads a baseline
    that usually does not exist yet; looking for it must not create ``logs/``."""
    target = managed_paths.logs_subdir(name, create=False)
    assert not target.exists(), f"create=False materialised {target}"
    nested = managed_paths.logs_subdir(name, "deeper", create=False)
    assert not nested.exists()


def test_write_paths_create_their_directory_lazily(tmp_path, monkeypatch):
    """Creation happens on the write, in a managed root relocated for the test."""
    monkeypatch.setattr(managed_paths, "_JARVIS_DIR", tmp_path)
    assert not (tmp_path / "logs").exists()
    target = managed_paths.logs_subdir("lazy_probe")
    assert target.is_dir(), "the write path did not create its directory"
    assert target.parent == tmp_path / "logs"
    artifact = managed_paths.log_artifact_path("lazy_probe.json")
    artifact.write_text("{}", encoding="utf-8")
    assert artifact.is_file()


def test_a_read_only_managed_root_degrades_instead_of_crashing(tmp_path, monkeypatch):
    """A full disk or read-only tree must surface on the caller's write, not on import.

    V69 M61.7 — the invariant under test is "``logs_subdir`` does not raise", and that
    is asserted on every platform. The follow-on "and therefore created nothing" is
    only meaningful where the read-only mode actually took effect: ``os.chmod`` on
    Windows toggles a file's read-only attribute and does **not** stop subdirectory
    creation, so on Windows the directory legitimately does get created and the
    original unconditional assertion failed for a reason that had nothing to do with
    the code under test. The check is now conditioned on observed reality rather than
    on an assumed POSIX filesystem — the same platform-determinism discipline M61
    applied to the traversal tests.
    """
    root = tmp_path / "ro"
    root.mkdir()
    monkeypatch.setattr(managed_paths, "_JARVIS_DIR", root)
    root.chmod(0o500)
    try:
        # Probe whether this filesystem honours the mode bits at all.
        probe = root / "_probe"
        try:
            probe.mkdir()
            read_only_enforced = False
            probe.rmdir()
        except OSError:
            read_only_enforced = True

        target = managed_paths.logs_subdir("blocked")  # must NOT raise, ever
        if read_only_enforced:
            assert not target.exists(), "creation must fail silently, not partially"
    finally:
        root.chmod(0o700)


# ── 6 & 7. redaction happens BEFORE the bytes reach disk ─────────────────────
_SECRETS = [
    ("otp", "NATO authorization code 837462"),
    ("api_key", "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLL"),
    ("bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"),
    ("password", "password=hunter2CorrectHorseBattery"),
]


def test_an_anthropic_api_key_is_redacted_not_just_an_openai_one():
    """RC1 regression. The pattern was ``\\bsk-[A-Za-z0-9]{16,}\\b``, commented
    "OpenAI-style", and that is precisely what it covered: ``sk-ant-api03-…`` has a
    hyphen four characters in, so the key format THIS runtime actually holds was the
    one format that survived redaction. Both vendors are asserted so a future
    narrowing of the class cannot pass by covering only one."""
    from core.redaction_policy import redact_text, scan_for_leaks

    for key in ("sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKK",
                "sk-AAAABBBBCCCCDDDDEEEEFFFFGGGG"):
        cleaned, report = redact_text(f"key {key} end")
        assert key not in cleaned, f"{key[:12]}… survived redaction"
        assert "secret" in report.categories
        # The verification gate must agree with the redactor, or a bundle passes
        # its leak scan while still holding the credential.
        assert "secret" in scan_for_leaks(f"key {key} end")


@pytest.mark.parametrize("label,secret", _SECRETS, ids=[s[0] for s in _SECRETS])
def test_the_managed_sink_redacts_a_secret_before_it_reaches_disk(
        label, secret, tmp_path):
    """The bytes ON DISK are the assertion. An in-memory check would prove nothing."""
    from loguru import logger

    sink_path = tmp_path / "sink.log"
    sink_id = managed_logging.install_file_sink(logger, path=sink_path)
    assert sink_id is not None
    try:
        logger.error(f"RC1 redaction probe — {secret}")
    finally:
        logger.remove(sink_id)
    written = sink_path.read_text(encoding="utf-8", errors="replace")
    assert "RC1 redaction probe" in written, "the record itself must survive"
    payload = secret.split("=")[-1].split()[-1]
    assert payload not in written, f"{label} reached disk unredacted"


def test_redaction_runs_before_the_write_not_after(tmp_path):
    """Ordering, stated as a property: no intermediate file ever holds the secret.

    The filter rewrites ``record["message"]`` on loguru's dispatch path, so the sink
    only ever formats the cleaned text — there is no window in which the raw value
    exists in the file and is scrubbed afterwards.
    """
    record = {"message": "OTP 998877 for operator"}
    assert managed_logging.redacting_filter(record) is True
    assert "998877" not in record["message"]


# ── 8. critical logging does not depend on the CWD ───────────────────────────
def test_the_runtime_log_target_is_identical_from_two_cwds(tmp_path):
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_APP_ROOT)!r})\n"
        "from core import managed_logging as m\n"
        "print(m.runtime_log_file()); print(m.audit_log_file())\n"
    )
    outputs = []
    for name in ("one", "two"):
        workdir = tmp_path / name
        workdir.mkdir()
        result = subprocess.run([sys.executable, "-c", script], cwd=str(workdir),
                                capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stderr[-400:]
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1], f"critical log target moved with the CWD: {outputs}"


# ── 9. the inventory cannot increase silently ────────────────────────────────
def test_no_active_module_builds_a_relative_multi_segment_path():
    """RC1.2's headline invariant, tree-wide (``core/``, ``tools/``, ``aura/``, entry).

    Deliberately NOT a ceiling. The previous inventory was capped at 24 while the
    real number was already lower, which meant it could not fail.
    """
    residue = managed_logging.runtime_relative_path_literals()
    assert residue == [], (
        "a CWD-relative path literal returned to the active runtime surface: "
        f"{residue}"
    )


def test_no_active_module_creates_a_directory_at_import_time():
    offenders = managed_logging.runtime_import_time_mkdir()
    assert offenders == [], f"import-time directory creation returned: {offenders}"


def test_the_broad_inventory_actually_detects_a_regression(tmp_path, monkeypatch):
    """A negative control. An inventory nobody has seen fail is not evidence.

    Plant both defects in a fake runtime tree and require the inventory to name them;
    without this, ``assert residue == []`` is equally satisfied by a scanner that
    silently walks nothing at all.
    """
    fake_root = tmp_path / "app"
    (fake_root / "core").mkdir(parents=True)
    (fake_root / "tools").mkdir()
    (fake_root / "aura").mkdir()
    (fake_root / "core" / "regressed.py").write_text(
        'from pathlib import Path\n_D = Path("logs/regressed")\n_D.mkdir(exist_ok=True)\n',
        encoding="utf-8")
    (fake_root / "tools" / "also_regressed.py").write_text(
        'from pathlib import Path\n_D = Path("logs/visuals/x")\n', encoding="utf-8")
    monkeypatch.setattr(managed_logging, "app_root", lambda: fake_root)

    residue = managed_logging.runtime_relative_path_literals()
    assert "core/regressed.py:2" in residue
    assert "tools/also_regressed.py:2" in residue
    assert managed_logging.runtime_import_time_mkdir() == ["core/regressed.py:3"]


def test_the_inventory_does_not_count_prose_as_a_defect(tmp_path, monkeypatch):
    """Several migrated modules carry a docstring EXPLAINING the old pattern.

    Detection is over the AST for exactly this reason: a module that documents the
    defect it no longer has must not be reported as still having it.
    """
    fake_root = tmp_path / "app"
    (fake_root / "core").mkdir(parents=True)
    (fake_root / "tools").mkdir()
    (fake_root / "aura").mkdir()
    (fake_root / "core" / "documented.py").write_text(
        '"""This module used to build Path("logs/thing") relative to the CWD."""\n'
        "# and os.makedirs('logs/thing') at import time\n",
        encoding="utf-8")
    monkeypatch.setattr(managed_logging, "app_root", lambda: fake_root)
    assert managed_logging.runtime_relative_path_literals() == []
    assert managed_logging.runtime_import_time_mkdir() == []


# ── 10. an optional subsystem must degrade, not kill text mode ───────────────
def test_the_hud_server_no_longer_depends_on_an_import_side_effect():
    """``aura.server`` mounted ``StaticFiles`` on a directory that only existed
    because an unrelated ``_MESHES_DIR.mkdir()`` had run at import. With the side
    effect removed the mount must be CONDITIONAL — an absent asset tree degrades to
    a HUD without ``/static``, never to an import-time crash."""
    import ast

    source = (_APP_ROOT / "aura" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    # AST, not substring: the module now carries a COMMENT naming the removed
    # constant, and a comment explaining a defect must not read as the defect.
    assert "_MESHES_DIR" not in assigned, "the dead constant is still assigned"
    assert "if _STATIC_DIR.is_dir():" in source, "the static mount is unguarded"
    assert 'app.mount("/static"' in source


def test_optional_subsystem_import_failure_is_visible_not_silent():
    """Every migrated optional collector reports its degradation through a log call.

    The invariant is not "it does not crash" — a subsystem that dies quietly is worse
    than one that crashes loudly. It must go dormant AND say so.
    """
    for module, marker in (
        ("network_quarantine.py", "dormant"),
        ("ir_reporter.py", "dormant"),
    ):
        source = (_APP_ROOT / "core" / module).read_text(encoding="utf-8")
        assert marker in source, f"core/{module} lost its dormancy report"
        assert "logger.warning" in source, f"core/{module} degrades silently"


# ── 11. security-critical persistence failure is not swallowed ───────────────
def test_punisher_action_log_failure_is_not_swallowed():
    """``punisher`` records executed defensive actions. If that write fails the call
    must RAISE — a containment action with no record is the one outcome an audit
    trail exists to prevent. The write is deliberately not wrapped in try/except."""
    body = _function_source("punisher.py", "_log_action")
    assert "with open(_log_path()" in body, "punisher stopped using the managed path"
    assert "except" not in body, (
        "punisher._log_action swallowed its audit-write failure"
    )


def test_persistence_baseline_save_failure_is_reported():
    """``persistence_hunter`` may keep running with a stale baseline, but the failure
    must reach the log — a lost baseline re-alerts every known-good autorun."""
    body = _function_source("persistence_hunter.py", "_save_state")
    assert "logger" in body, "persistence_hunter._save_state fails silently"


def test_migrated_modules_resolve_through_managed_paths(request):
    """No module reintroduces a private path constant next to the managed accessor."""
    for module in ("punisher.py", "persistence_hunter.py", "target_aliases.py",
                   "intel_fusion.py", "hunt_scheduler.py", "github_explorer.py",
                   "detection_harness.py", "ir_reporter.py", "code_intel.py",
                   "proxy_intel.py", "red_team_operator.py", "sensor_mesh.py",
                   "coverage_reporter.py", "sigma_generator.py",
                   "detection_engineer.py"):
        assert managed_logging.module_uses_managed_paths(module), (
            f"core/{module} no longer resolves through managed_paths"
        )
