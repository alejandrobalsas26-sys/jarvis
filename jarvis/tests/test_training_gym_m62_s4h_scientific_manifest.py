"""V69 M62 S4H — the canonical scientific selection, and proof it is not vacuous.

WHAT THESE TESTS ARE FOR
------------------------
The control plane's recorded test baseline names ``pytest -k m62`` as its invocation.
``-k`` matches node ids, and a node id is built from the file name — so the selection is
"files whose path contains m62". Three modules assert M62 state and are named for the
milestone that wrote them:

    tests/test_training_gym_m63_s4b_control_plane.py
    tests/test_training_gym_m63_s4b_fifth_candidate.py
    tests/test_training_gym_m63_s4c_trained_state.py

``test_the_keyword_selector_really_does_deselect_the_m63_modules`` MEASURES that gap by
running collection twice rather than asserting it from memory. The number it finds is
the reason the manifest exists.

The non-vacuity tests then break the manifest six ways and require the verifier to
notice each. A manifest that passes after a scientific group has been deleted is a
manifest that protects nothing.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "jarvis"
MANIFEST_PATH = REPO_ROOT / "state" / "m62" / "scientific-suite.json"
VERIFIER_PATH = PACKAGE_ROOT / "scripts" / "verify_m62_scientific_suite.py"

sys.path.insert(0, str(PACKAGE_ROOT))

KEYWORD_INVISIBLE = (
    "tests/test_training_gym_m63_s4b_control_plane.py",
    "tests/test_training_gym_m63_s4b_fifth_candidate.py",
    "tests/test_training_gym_m63_s4c_trained_state.py",
)


def load_verifier():
    spec = importlib.util.spec_from_file_location("s4h_suite_verifier", VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier():
    return load_verifier()


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def run_verifier(verifier, payload: dict, tmp_path: Path) -> int:
    path = tmp_path / "mutated-suite.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return verifier.main(["--root", str(REPO_ROOT), "--manifest", str(path),
                          "--skip-collection"])


# ══════════════════════════════════════════════════════════════════════════════
#  §47 THE MANIFEST EXISTS AND IS THE AUTHORITY
# ══════════════════════════════════════════════════════════════════════════════
def test_the_manifest_verifies(verifier):
    assert verifier.main(["--root", str(REPO_ROOT), "--skip-collection"]) == 0


def test_the_manifest_collects_and_finds_tests(verifier):
    """Collection only. No test in the manifest is executed by this check."""
    assert verifier.main(["--root", str(REPO_ROOT)]) == 0


def test_the_authorised_invocation_names_paths_and_never_a_keyword(manifest, verifier):
    argv = verifier.pytest_argv(manifest)
    assert argv[0] == "pytest"
    assert "-k" not in argv
    assert all(a.startswith("tests/") for a in argv[1:])


def test_the_manifest_declares_every_group_s4h_requires(manifest):
    required = {"candidate_lifecycle", "training_receipts", "evaluation_receipts",
                "holdout_lifecycle", "single_spend_invariants",
                "protocol_v4_compatibility", "control_plane", "sealed_state",
                "frozen_scorer_identities", "future_instruments",
                "scientific_manifest"}
    assert required == set(manifest["required_groups"])
    assert required == {g["group"] for g in manifest["groups"]}


def test_every_group_states_why_it_is_scientifically_load_bearing(manifest):
    for group in manifest["groups"]:
        assert len(group["why"]) >= 40, group["group"]
        assert group["modules"]


def test_every_named_module_exists(manifest):
    for group in manifest["groups"]:
        for module in group["modules"]:
            assert (PACKAGE_ROOT / module).is_file(), module


def test_no_module_is_claimed_by_two_groups(manifest):
    modules = [m for g in manifest["groups"] for m in g["modules"]]
    assert len(modules) == len(set(modules))


# ══════════════════════════════════════════════════════════════════════════════
#  §64 / §86 THE KEYWORD GAP, MEASURED
# ══════════════════════════════════════════════════════════════════════════════
def test_the_keyword_selector_really_does_deselect_the_m63_modules():
    """Measured by running collection, not asserted from memory."""
    unfiltered = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         "-p", "no:cacheprovider", *KEYWORD_INVISIBLE],
        cwd=str(PACKAGE_ROOT), capture_output=True, text=True, timeout=300)
    filtered = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         "-p", "no:cacheprovider", "-k", "m62", *KEYWORD_INVISIBLE],
        cwd=str(PACKAGE_ROOT), capture_output=True, text=True, timeout=300)
    collected = sum(1 for line in unfiltered.stdout.splitlines() if "::" in line)
    selected = sum(1 for line in filtered.stdout.splitlines() if "::" in line)
    assert collected >= 150, "these modules should carry a substantial suite"
    assert selected == 0, (
        f"`-k m62` selected {selected} of {collected}; if this is no longer zero the "
        f"gap has changed and the manifest's justification needs re-reading")


@pytest.mark.parametrize("module", KEYWORD_INVISIBLE)
def test_a_keyword_invisible_module_is_named_by_the_manifest(module, manifest):
    named = {m for g in manifest["groups"] for m in g["modules"]}
    assert module in named


def test_the_keyword_selector_is_not_the_sole_authority_any_more(manifest):
    """§86: the manifest names modules `-k m62` cannot reach, so it is strictly wider."""
    named = {m for g in manifest["groups"] for m in g["modules"]}
    unreachable = {m for m in named if "m62" not in Path(m).name}
    assert unreachable >= set(KEYWORD_INVISIBLE)


# ══════════════════════════════════════════════════════════════════════════════
#  §49 NON-VACUITY — break it six ways
# ══════════════════════════════════════════════════════════════════════════════
def test_mutation_a_required_group_is_removed(verifier, manifest, tmp_path):
    mutated = {**manifest,
               "groups": [g for g in manifest["groups"]
                          if g["group"] != "sealed_state"]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_a_group_is_emptied(verifier, manifest, tmp_path):
    mutated = {**manifest,
               "groups": [{**g, "modules": []} if g["group"] == "control_plane" else g
                          for g in manifest["groups"]]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_a_keyword_invisible_module_is_dropped(verifier, manifest, tmp_path):
    """The exact regression: quietly narrowing back to what `-k m62` already saw."""
    mutated = {**manifest,
               "groups": [{**g, "modules": [m for m in g["modules"]
                                            if m not in KEYWORD_INVISIBLE]}
                          for g in manifest["groups"]]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_a_module_that_does_not_exist_is_named(verifier, manifest, tmp_path):
    mutated = {**manifest,
               "groups": [{**g, "modules": [*g["modules"], "tests/test_imaginary.py"]}
                          if g["group"] == "sealed_state" else g
                          for g in manifest["groups"]]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_a_module_is_claimed_twice(verifier, manifest, tmp_path):
    duplicated = manifest["groups"][0]["modules"][0]
    mutated = {**manifest,
               "groups": [{**g, "modules": [*g["modules"], duplicated]}
                          if g["group"] == "sealed_state" else g
                          for g in manifest["groups"]]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_the_schema_version_moves(verifier, manifest, tmp_path):
    mutated = {**manifest, "schema_version": "m62.scientific_suite.9"}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_mutation_a_group_loses_its_justification(verifier, manifest, tmp_path):
    mutated = {**manifest,
               "groups": [{k: v for k, v in g.items() if k != "why"}
                          if g["group"] == "holdout_lifecycle" else g
                          for g in manifest["groups"]]}
    assert run_verifier(verifier, mutated, tmp_path) == 1


def test_the_unmutated_manifest_still_passes_the_same_checker(verifier, manifest,
                                                              tmp_path):
    """The control: every mutation above fails against a manifest that otherwise passes."""
    assert run_verifier(verifier, manifest, tmp_path) == 0
