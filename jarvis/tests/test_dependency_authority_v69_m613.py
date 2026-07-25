"""tests/test_dependency_authority_v69_m613.py — V69 M61.3: one dependency authority.

The pre-M61 repository maintained three rival dependency lists, and they HAD drifted:
``pip install jarvis[soc]`` was missing eight packages that
``pip install -r requirements/soc.txt`` installed, and ``jarvis[lab]`` was missing
seven. These tests make ``requirements/<profile>.txt`` the authority and turn any
future divergence into a failing build.

They also pin the two properties that matter more than tidiness:

  * a **base** (text-mode) install can never reach an offensive or GUI-automation
    package — no ``keyboard`` global hotkeys, no ``mitmproxy``, no Metasploit/Sliver
    client on a workstation that only wanted a chat prompt;
  * JARVIS never installs packages onto the operator's host on its own.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from core import dependency_authority as da

_APP_ROOT = Path(__file__).resolve().parent.parent
_REQ_DIR = _APP_ROOT / "requirements"


# ── the authority audit ──────────────────────────────────────────────────────
def test_dependency_authority_audit_passes():
    result = da.audit()
    assert result["ok"], "dependency drift:\n  " + "\n  ".join(result["problems"])


def test_every_declared_profile_exists_and_resolves():
    for profile in da.PROFILES:
        assert (_REQ_DIR / f"{profile}.txt").is_file()
        assert da.resolve_profile(profile), f"{profile} resolved to nothing"


def test_no_profile_declares_a_package_twice():
    assert da.duplicate_declarations() == {}


def test_normalization_collapses_pep503_equivalents():
    assert da.normalize("python_whois") == da.normalize("python-whois")
    assert da.normalize("PyYAML") == "pyyaml"
    assert da.normalize("Pillow") == "pillow"


def test_duplicate_detection_actually_detects(tmp_path, monkeypatch):
    """The checker must be able to fail, not just pass."""
    fake = tmp_path / "requirements"
    fake.mkdir()
    (fake / "base.txt").write_text("requests>=2.0\nRequests>=2.31.0\n", encoding="utf-8")
    monkeypatch.setattr(da, "_REQ_DIR", fake)
    monkeypatch.setattr(da, "PROFILES", ("base",))
    assert da.duplicate_declarations() == {"base": ["requests"]}


# ── pyproject mirrors the profiles exactly ───────────────────────────────────
def _extras() -> dict:
    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


@pytest.mark.parametrize("profile", ["voice", "docs", "soc", "lab", "dev"])
def test_extra_mirrors_its_profile_exactly(profile):
    declared = {da.parse_requirement(r)[0] for r in _extras()[profile]}
    authority = {name for name, _ in da._read_profile(profile)}
    assert declared == authority, (
        f"extra[{profile}] != requirements/{profile}.txt; "
        f"missing={sorted(authority - declared)} extra={sorted(declared - authority)}")


def test_base_dependencies_mirror_base_profile():
    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        declared = {da.parse_requirement(r)[0]
                    for r in tomllib.load(fh)["project"]["dependencies"]}
    assert declared == {name for name, _ in da._read_profile("base")}


@pytest.mark.parametrize("package", [
    "stix2", "mmh3", "ntplib", "piexif", "ijson", "igraph", "pyserial",
])
def test_soc_extra_regained_the_packages_it_had_lost(package):
    """These were declared in requirements/soc.txt but absent from the extra."""
    assert da.normalize(package) in {da.parse_requirement(r)[0] for r in _extras()["soc"]}


@pytest.mark.parametrize("package", [
    "pymetasploit3", "grpcio", "sliver-py", "qrcode", "python-telegram-bot",
])
def test_lab_extra_regained_the_packages_it_had_lost(package):
    assert da.normalize(package) in {da.parse_requirement(r)[0] for r in _extras()["lab"]}


def test_all_extra_references_every_sibling_extra():
    refs = [r for r in _extras()["all"] if str(r).lower().startswith("jarvis[")]
    assert refs, "extra[all] must compose the siblings, not re-list them"
    inner = refs[0][refs[0].index("[") + 1:refs[0].rindex("]")]
    assert {"voice", "docs", "soc", "lab", "dev"} <= {p.strip() for p in inner.split(",")}


# ── base purity: no offensive / GUI tooling in a text-mode install ───────────
def test_base_profile_contains_no_lab_or_gui_package():
    assert da._audit_base_purity() == []


@pytest.mark.parametrize("package", [
    "keyboard", "pyautogui", "mitmproxy", "pymetasploit3", "sliver-py", "docker",
])
def test_named_offensive_package_is_absent_from_base(package):
    assert da.normalize(package) not in da.resolve_profile("base")


@pytest.mark.parametrize("package", ["keyboard", "pyautogui", "mitmproxy"])
def test_named_offensive_package_is_present_in_lab(package):
    """Absent from base is only correct if it is still installable where intended."""
    assert da.normalize(package) in da.resolve_profile("lab")


def test_voice_docs_soc_are_not_pulled_in_by_base():
    base = da.resolve_profile("base")
    for package in ("faster-whisper", "pyttsx3", "easyocr", "yara-python", "scapy"):
        assert da.normalize(package) not in base


# ── the legacy monolith is deprecated, not a rival ───────────────────────────
def test_legacy_requirements_is_a_pointer_not_a_list():
    assert da._audit_legacy() == []
    text = (_APP_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "DEPRECATED" in text
    assert "-r requirements/all.txt" in text


def test_legacy_requirements_loses_no_package():
    """Deprecating it must not silently drop anything it used to install."""
    legacy = dict(da._read_profile_path(_APP_ROOT / "requirements.txt"))
    assert set(legacy) <= set(da.resolve_profile("all"))


# ── CI constraints ───────────────────────────────────────────────────────────
def test_ci_constraints_file_parses_and_bounds_only_the_toolchain():
    path = _REQ_DIR / "constraints-ci.txt"
    assert path.is_file()
    parsed = dict(da._read_profile_path(path))
    assert parsed, "constraints file parsed to nothing"
    assert set(parsed) == {"pytest", "pytest-asyncio", "ruff", "bandit", "pip-audit"}
    for name, spec in parsed.items():
        assert "<" in spec, f"{name} constraint has no upper bound"


def test_ci_constraints_do_not_pin_platform_specific_runtime_packages():
    """Hard-pinning these without per-platform testing trades one problem for another."""
    parsed = dict(da._read_profile_path(_REQ_DIR / "constraints-ci.txt"))
    for package in ("pyaudio", "yara-python", "opencv-python", "torch-directml",
                    "pywintrace", "capstone"):
        assert da.normalize(package) not in parsed


# ── installers still resolve ─────────────────────────────────────────────────
@pytest.mark.parametrize("script", ["install.ps1", "install.sh"])
def test_installer_exists(script):
    assert (_APP_ROOT / "scripts" / script).is_file()


def test_installers_reference_only_real_profiles():
    for script in ("install.ps1", "install.sh"):
        text = (_APP_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "requirements" in text
        for profile in da.PROFILES:
            if profile in text:
                assert (_REQ_DIR / f"{profile}.txt").is_file()


def test_include_directives_cannot_escape_the_requirements_directory():
    for profile in da.PROFILES:
        for included in da.profile_includes(profile):
            assert (_REQ_DIR / f"{included}.txt").is_file()


# ── host mutation is opt-in (M61.3) ──────────────────────────────────────────
def test_host_mutation_is_off_by_default(monkeypatch):
    from core import dependency_guardian as guardian
    monkeypatch.delenv("JARVIS_AUTO_INSTALL_DEPS", raising=False)
    assert guardian.host_mutation_allowed() is False


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("", False), ("maybe", False),
])
def test_host_mutation_opt_in_parsing(monkeypatch, value, expected):
    from core import dependency_guardian as guardian
    monkeypatch.setenv("JARVIS_AUTO_INSTALL_DEPS", value)
    assert guardian.host_mutation_allowed() is expected


def test_missing_packages_reported_not_installed_by_default(monkeypatch):
    """The default path must NEVER reach a subprocess."""
    import asyncio

    from core import dependency_guardian as guardian
    monkeypatch.delenv("JARVIS_AUTO_INSTALL_DEPS", raising=False)
    monkeypatch.setattr(guardian, "missing_packages", lambda: ["mmh3", "yara-python"])

    def _forbidden(*a, **k):  # pragma: no cover — the assertion is that it is not hit
        raise AssertionError("subprocess invoked without operator authorization")

    monkeypatch.setattr(guardian.subprocess, "run", _forbidden)
    result = asyncio.run(guardian._install_missing_packages())
    assert result == ["would_install:mmh3", "would_install:yara-python"]


def test_jq_is_not_installed_without_authorization(monkeypatch):
    import asyncio

    from core import dependency_guardian as guardian
    monkeypatch.delenv("JARVIS_AUTO_INSTALL_DEPS", raising=False)
    monkeypatch.setattr(guardian.shutil, "which", lambda _n: None)

    def _forbidden(*a, **k):  # pragma: no cover
        raise AssertionError("winget invoked without operator authorization")

    monkeypatch.setattr(guardian.subprocess, "run", _forbidden)
    assert asyncio.run(guardian._ensure_jq()) == "missing_not_installed"


def test_break_system_packages_is_never_passed_to_pip():
    """That flag overrides the guard protecting an externally-managed interpreter.

    Checked over the AST, not the raw text: the module's docstring explains *why* the
    flag was removed, and that explanation must not itself trip the test.
    """
    import ast

    source = (_APP_ROOT / "core" / "dependency_guardian.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert "--break-system-packages" not in literals
