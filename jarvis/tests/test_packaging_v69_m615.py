"""tests/test_packaging_v69_m615.py — V69 M61.5: the build is declared, not inferred.

The flat layout (top-level ``core/``, ``tools/``, ``aura/``, ``main.py``) is exactly
the shape setuptools auto-discovery refuses: it sees several candidate top-level
packages and errors out rather than guessing. Before M61 nothing had ever built this
project, so that was undiscovered. Now the layout is declared explicitly and these
tests pin the declaration.

The second half is the part that matters more: a developer working tree contains the
operator's ``.env``, a 2.7 MB DEBUG log, SQLite stores holding conversation state, and
the HMAC signing keys the root ``.gitignore`` already refuses to commit. An sdist is
built from that tree. These tests assert the rules that keep those out, and the
scanner that verifies the rules actually worked.

Building is slow, so the artifact tests are opt-in via ``--dist``-style fixtures: the
cheap declarative checks always run, and the real build runs in CI's packaging job and
in the M61 release qualification.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from core.version import VERSION

_APP_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


# ── the layout is declared, because it cannot be inferred ────────────────────
def test_packages_are_declared_explicitly():
    """Auto-discovery cannot resolve a flat layout with several top-level packages."""
    setuptools_cfg = _pyproject()["tool"]["setuptools"]
    assert setuptools_cfg["packages"] == ["core", "tools", "aura"]
    assert setuptools_cfg["py-modules"] == ["main"]


def test_declared_packages_exist_and_are_importable_packages():
    for package in _pyproject()["tool"]["setuptools"]["packages"]:
        assert (_APP_ROOT / package / "__init__.py").is_file()


def test_console_entry_point_targets_a_real_callable():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["jarvis"] == "main:main"
    source = (_APP_ROOT / "main.py").read_text(encoding="utf-8")
    assert re.search(r"^def main\(", source, re.MULTILINE), "main:main does not exist"


def test_package_data_is_an_explicit_allowlist():
    """Anything not named here does not ship — the default would be to ship nothing."""
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert "core" in data
    assert any(pattern.endswith("*.yaml") for pattern in data["core"])


def test_security_files_are_explicitly_excluded_from_package_data():
    excluded = _pyproject()["tool"]["setuptools"]["exclude-package-data"]["core"]
    for secret in ("telemetry_keys.json", "trust_profile.json", "feed_hashes.json",
                   "integrity_baseline.json"):
        assert secret in excluded


def test_build_backend_is_declared():
    build = _pyproject()["build-system"]
    assert build["build-backend"] == "setuptools.build_meta"
    assert any(r.startswith("setuptools") for r in build["requires"])


# ── MANIFEST.in prunes what a working tree carries ──────────────────────────
def _manifest() -> str:
    return (_APP_ROOT / "MANIFEST.in").read_text(encoding="utf-8")


def test_manifest_exists():
    assert (_APP_ROOT / "MANIFEST.in").is_file()


@pytest.mark.parametrize("secret", [
    ".env", "core/telemetry_keys.json", "core/trust_profile.json",
    "core/feed_hashes.json", "core/integrity_baseline.json", "jarvis_config.yaml",
])
def test_manifest_excludes_every_secret(secret):
    assert f"exclude {secret}" in _manifest()


@pytest.mark.parametrize("tree", ["data", "logs", ".venv", "brain", "tests", ".github"])
def test_manifest_prunes_state_and_dev_trees(tree):
    assert f"prune {tree}" in _manifest()


@pytest.mark.parametrize("suffix", ["*.log", "*.db", "*.jsonl", "*.sqlite"])
def test_manifest_globally_excludes_runtime_artifacts(suffix):
    assert f"global-exclude {suffix}" in _manifest()


# ── the scanner that verifies the rules worked ──────────────────────────────
def test_scanner_module_declares_the_forbidden_set():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_m61_pkg_scan", _APP_ROOT / "scripts" / "check_package_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name in (".env", "telemetry_keys.json", "trust_profile.json",
                 "jarvis_config.yaml"):
        assert name in module.FORBIDDEN_NAMES
    for suffix in (".log", ".db", ".jsonl", ".gguf", ".safetensors"):
        assert suffix in module.FORBIDDEN_SUFFIXES
    for fragment in ("/data/", "/logs/", "/tests/", "/brain/"):
        assert fragment in module.FORBIDDEN_DIRS
    assert "main.py" in module.REQUIRED_FRAGMENTS


def test_scanner_flags_a_planted_secret():
    """The scanner must be able to fail, or a PASS means nothing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_m61_pkg_scan2", _APP_ROOT / "scripts" / "check_package_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    clean = ["jarvis-1.0/core/llm.py", "jarvis-1.0/main.py", "jarvis-1.0/tools/x.py"]
    assert module._violations(clean) == []

    for planted in ("jarvis-1.0/.env", "jarvis-1.0/core/telemetry_keys.json",
                    "jarvis-1.0/logs/jarvis.log", "jarvis-1.0/data/state.db",
                    "jarvis-1.0/tests/test_x.py"):
        assert module._violations([planted]), f"scanner missed {planted}"


def test_scanner_requires_the_runtime_modules():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_m61_pkg_scan3", _APP_ROOT / "scripts" / "check_package_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._missing(["jarvis-1.0/README.md"]), "an empty build would pass"
    assert module._missing(
        ["jarvis-1.0/core/a.py", "jarvis-1.0/tools/b.py", "jarvis-1.0/main.py"]) == []


def test_scanner_detects_a_version_mismatch():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_m61_pkg_scan4", _APP_ROOT / "scripts" / "check_package_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._declared_version(Path("jarvis-69.61.0.tar.gz")) == "69.61.0"
    assert module._declared_version(
        Path("jarvis-69.61.0-py3-none-any.whl")) == "69.61.0"
    assert module._declared_version(Path("jarvis-1.2.3.tar.gz")) != VERSION


# ── launch paths ─────────────────────────────────────────────────────────────
def test_module_launch_entrypoint_exists():
    """`python -m jarvis` runs jarvis/__main__.py from the repository root."""
    main_module = _APP_ROOT / "__main__.py"
    assert main_module.is_file()
    source = main_module.read_text(encoding="utf-8")
    assert "from main import main" in source


def test_main_module_is_not_packaged():
    """`__main__.py` is a source-tree launcher; installing it into site-packages
    would put a top-level `__main__` on the path of every other program."""
    assert "__main__" not in _pyproject()["tool"]["setuptools"]["py-modules"]


def test_conftest_is_not_packaged():
    manifest = _manifest()
    assert "exclude conftest.py" in manifest
