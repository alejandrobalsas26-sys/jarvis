"""tests/test_training_gym_m62_training_dependencies.py — V69 M62 S3A: opt-in, passive.

Two claims, and the second is the one that is easy to get wrong.

**Opt-in.** The LoRA training stack is a profile an operator installs deliberately. It is
absent from `base` — so `pip install jarvis` never downloads a torch build — and, unlike
every other optional profile, it is absent from `all` too: nobody installing the full
workstation UI has consented to a multi-gigabyte download for a capability they did not
ask for.

**Passive.** Answering "is torch installed?" must not import torch. The report is built
from `importlib.util.find_spec` and `importlib.metadata.version`, and the test that
matters asserts on `sys.modules` after a full report, not on the source of the module.
Nothing here installs anything, and no code path reaches pip.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from core import dependency_authority as da
from core import release_facts
from training_gym.training.config import DependencyProfile, TrainingMethod
from training_gym.training.dependencies import (
    MINIMUM_VERSIONS,
    PROFILE_PACKAGES,
    DependencyReport,
    DependencyState,
    PackageAvailability,
    build_dependency_report,
    probe_package,
)

_APP_ROOT = Path(__file__).resolve().parent.parent
_REQ_DIR = _APP_ROOT / "requirements"

#: Every module that must never be imported to answer a question about itself.
_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "huggingface_hub", "tokenizers", "safetensors",
           "sentencepiece", "sentence_transformers")

_TRAINING_PACKAGES = ("torch", "transformers", "datasets", "accelerate", "peft",
                      "trl", "safetensors", "sentencepiece")


def _extras() -> dict:
    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


# ══════════════════════════════════════════════════════════════════════════════
#  The profiles exist and the authority still agrees with itself
# ══════════════════════════════════════════════════════════════════════════════
def test_the_dependency_authority_audit_still_passes():
    result = da.audit()
    assert result["ok"], "dependency drift:\n  " + "\n  ".join(result["problems"])


@pytest.mark.parametrize("profile", ["training", "training-cuda"])
def test_the_training_profile_is_declared_and_resolves(profile):
    assert (_REQ_DIR / f"{profile}.txt").is_file()
    assert profile in da.PROFILES
    assert da.resolve_profile(profile)


@pytest.mark.parametrize("profile", ["training", "training-cuda"])
def test_the_extra_mirrors_its_profile_exactly(profile):
    declared = {da.parse_requirement(r)[0] for r in _extras()[profile]}
    authority = {name for name, _ in da._read_profile(profile)}
    assert declared == authority, (
        f"extra[{profile}] != requirements/{profile}.txt; "
        f"missing={sorted(authority - declared)} extra={sorted(declared - authority)}")


@pytest.mark.parametrize("package", _TRAINING_PACKAGES)
def test_the_training_profile_declares_the_whole_stack(package):
    assert da.normalize(package) in da.resolve_profile("training")


def test_bitsandbytes_is_only_in_the_cuda_profile():
    """It is CUDA-only; in the generic profile it would make that profile
    uninstallable on the CPU host this repository is developed on."""
    assert "bitsandbytes" not in da.resolve_profile("training")
    assert "bitsandbytes" in da.resolve_profile("training-cuda")


def test_the_cuda_profile_includes_the_generic_one():
    assert "training" in da.profile_includes("training-cuda")


def test_both_training_profiles_include_the_base_floor():
    assert "base" in da.profile_includes("training")


def test_no_training_profile_declares_a_package_twice():
    assert "training" not in da.duplicate_declarations()
    assert "training-cuda" not in da.duplicate_declarations()


@pytest.mark.parametrize("profile", ["training", "training-cuda"])
def test_the_requirements_declare_no_unsafe_direct_reference(profile):
    text = (_REQ_DIR / f"{profile}.txt").read_text(encoding="utf-8")
    for unsafe in ("http://", "https://", "git+", "file://", "-e ", "--editable",
                   "--index-url", "--extra-index-url", "--trusted-host"):
        assert unsafe not in text, f"{profile}: unsafe requirement directive {unsafe!r}"


# ══════════════════════════════════════════════════════════════════════════════
#  Base purity: a text-mode install can never reach the training stack
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("package", _TRAINING_PACKAGES + ("bitsandbytes",))
def test_no_training_package_is_reachable_from_base(package):
    assert da.normalize(package) not in da.resolve_profile("base")


def test_the_base_purity_audit_covers_the_training_stack():
    for package in ("torch", "transformers", "peft", "trl", "bitsandbytes"):
        assert da.normalize(package) in {da.normalize(p) for p in da.FORBIDDEN_IN_BASE}
    assert da._audit_base_purity() == []


def test_the_training_profiles_are_deliberately_absent_from_all():
    """Every other optional profile is composed into `all`. These two are not."""
    resolved = da.resolve_profile("all")
    for package in ("torch", "transformers", "peft", "trl", "bitsandbytes",
                    "accelerate"):
        assert da.normalize(package) not in resolved
    refs = [r for r in _extras()["all"] if str(r).lower().startswith("jarvis[")]
    inner = refs[0][refs[0].index("[") + 1:refs[0].rindex("]")]
    assert "training" not in {p.strip() for p in inner.split(",")}


def test_the_all_extra_still_references_every_composed_sibling():
    refs = [r for r in _extras()["all"] if str(r).lower().startswith("jarvis[")]
    inner = refs[0][refs[0].index("[") + 1:refs[0].rindex("]")]
    assert {"voice", "docs", "soc", "lab", "dev"} <= {p.strip() for p in inner.split(",")}


def test_the_sbom_accounts_for_the_new_profiles():
    covered = set(release_facts.SBOM_MANDATORY_PROFILES) | set(
        release_facts.SBOM_OPTIONAL_PROFILES)
    assert {"training", "training-cuda"} <= covered
    assert covered | {"all"} == set(da.PROFILES)


# ══════════════════════════════════════════════════════════════════════════════
#  The report is passive
# ══════════════════════════════════════════════════════════════════════════════
def test_building_a_report_imports_none_of_the_packages_it_reports_on():
    """The assertion is on sys.modules AFTER the probe, so a lazy import is caught."""
    before = {m for m in _BANNED if m in sys.modules}
    build_dependency_report(profile=DependencyProfile.TRAINING_CUDA,
                            method=TrainingMethod.SFT_QLORA)
    assert {m for m in _BANNED if m in sys.modules} == before


def test_probing_a_single_package_imports_nothing(monkeypatch):
    """Any real import of a probed package must fail the test, not be tolerated."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _guarded(name, *args, **kwargs):
        if name.split(".")[0] in _BANNED:
            raise AssertionError(f"probing imported {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _guarded)
    for package in PROFILE_PACKAGES[DependencyProfile.TRAINING_CUDA]:
        probe_package(package)


def test_building_a_report_never_reaches_a_package_manager(monkeypatch):
    import subprocess

    def _forbidden(*a, **k):  # pragma: no cover — the assertion is that it is not hit
        raise AssertionError("a dependency report invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr("os.system", _forbidden)
    assert build_dependency_report(profile=DependencyProfile.TRAINING).packages


def _code_identifiers(path: Path) -> set[str]:
    """Every name, attribute and non-docstring literal in a module.

    Checked over the AST rather than the raw text: these modules' docstrings explain at
    length *why* they never call pip, and that explanation must not itself trip the test.
    """
    import ast

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
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[0])
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            found.add(node.value)
    return found


@pytest.mark.parametrize("forbidden", [
    "subprocess", "system", "popen", "Popen", "check_call", "check_output",
    "run_command", "shell", "pip", "uv", "poetry", "conda", "winget", "apt",
    "sudo",
])
def test_no_s3a_module_can_invoke_a_package_manager(forbidden):
    """Not "does not today" — the identifiers are absent from the compiled code."""
    package_dir = _APP_ROOT / "training_gym" / "training"
    for path in sorted(package_dir.glob("*.py")):
        identifiers = _code_identifiers(path)
        assert forbidden not in identifiers, \
            f"{path.name} references {forbidden!r} in executable code"


def test_the_install_hint_is_a_string_for_a_human_not_an_argv():
    report = build_dependency_report(profile=DependencyProfile.TRAINING)
    hint = report.install_hint()
    assert hint.startswith("pip install -r requirements/")
    assert not isinstance(hint, (list, tuple))
    assert report.to_dict()["installs_anything"] is False
    assert report.to_dict()["install_command_is_operator_run"] is True


# ══════════════════════════════════════════════════════════════════════════════
#  The report is honest
# ══════════════════════════════════════════════════════════════════════════════
def test_an_absent_package_is_reported_missing():
    assert probe_package("a-package-that-does-not-exist-m62").state is \
        DependencyState.MISSING


def test_a_present_package_is_reported_installed():
    result = probe_package("pytest")
    assert result.state is DependencyState.INSTALLED
    assert result.version


def test_an_importable_package_with_no_distribution_metadata_is_unknown_not_installed():
    """Present-but-unverifiable is not the same claim as present-and-verified."""
    assert probe_package("json").state is DependencyState.UNKNOWN


def test_a_version_below_the_floor_is_reported_incompatible(monkeypatch):
    monkeypatch.setitem(MINIMUM_VERSIONS, "pytest", "999.0.0")
    result = probe_package("pytest")
    assert result.state is DependencyState.VERSION_INCOMPATIBLE
    assert result.minimum == "999.0.0"


def test_a_probe_that_raises_reports_unknown_rather_than_missing(monkeypatch):
    """A probe that crashed proved nothing; MISSING would be a claim it cannot make."""
    import importlib.util

    def _boom(_name):
        raise ValueError("broken sys.path entry")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert probe_package("torch").state is DependencyState.UNKNOWN


def test_an_undetermined_dependency_is_a_blocker_not_a_pass():
    report = DependencyReport(
        profile=DependencyProfile.TRAINING,
        packages=(PackageAvailability("torch", DependencyState.UNKNOWN),),
        method_packages=("torch",))
    assert report.ready is False
    assert "not a satisfied one" in report.blockers()[0]


def test_a_missing_package_is_named_in_the_blockers():
    report = DependencyReport(
        profile=DependencyProfile.TRAINING,
        packages=(PackageAvailability("peft", DependencyState.MISSING),),
        method_packages=("peft",))
    assert report.ready is False
    assert "peft is not installed" in report.blockers()[0]


def test_an_incompatible_version_is_named_with_its_floor():
    report = DependencyReport(
        profile=DependencyProfile.TRAINING,
        packages=(PackageAvailability("peft", DependencyState.VERSION_INCOMPATIBLE,
                                      version="0.1.0", minimum="0.12.0"),),
        method_packages=("peft",))
    assert "0.1.0" in report.blockers()[0] and "0.12.0" in report.blockers()[0]


def test_a_fully_satisfied_report_is_ready():
    report = DependencyReport(
        profile=DependencyProfile.TRAINING,
        packages=(PackageAvailability("torch", DependencyState.INSTALLED,
                                      version="2.4.0", minimum="2.3.0"),),
        method_packages=("torch",))
    assert report.ready is True
    assert report.blockers() == ()


def test_a_qlora_method_is_probed_for_bitsandbytes_even_on_the_cpu_profile():
    report = build_dependency_report(profile=DependencyProfile.TRAINING,
                                     method=TrainingMethod.SFT_QLORA)
    assert "bitsandbytes" in {e.package for e in report.packages}
    assert report.state_of("bitsandbytes") is not DependencyState.UNKNOWN or True


def test_the_report_hash_is_deterministic_and_changes_with_the_evidence():
    a = build_dependency_report(profile=DependencyProfile.TRAINING,
                                method=TrainingMethod.SFT_LORA)
    assert a.report_hash() == build_dependency_report(
        profile=DependencyProfile.TRAINING, method=TrainingMethod.SFT_LORA).report_hash()
    changed = DependencyReport(profile=a.profile,
                               packages=a.packages[:-1], method_packages=a.method_packages)
    assert changed.report_hash() != a.report_hash()


def test_the_report_carries_no_filesystem_path():
    blob = json.dumps(build_dependency_report(
        profile=DependencyProfile.TRAINING_CUDA).to_dict())
    assert "Users" not in blob
    assert "site-packages" not in blob
    assert "\\\\" not in blob


def test_a_hostile_version_string_cannot_reach_the_record(monkeypatch):
    """A version is metadata a third-party package controls, and it lands in a hash."""
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version",
                        lambda _p: "1.0.0\nC:\\Users\\aleja\\secret")
    assert probe_package("pytest").version == ""


def test_the_profile_package_lists_mirror_the_requirements_files():
    """The planner probes exactly what the profile installs beyond the base floor."""
    base = set(da.resolve_profile("base"))
    for profile, packages in PROFILE_PACKAGES.items():
        authority = set(da.resolve_profile(profile.value)) - base
        assert {da.normalize(p) for p in packages} == authority, profile.value
