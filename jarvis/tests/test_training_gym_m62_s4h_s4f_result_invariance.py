"""V69 M62 S4H — S4F's result did not move, and this milestone could not have moved it.

WHAT THESE TESTS ARE FOR
------------------------
S4H is prospective work on a repository whose most valuable property is that an old
measurement still says exactly what it said. The risk is not that someone rewrites a
receipt; it is that a milestone about instruments quietly changes a state file, a status
string or a count, and the change is only noticed when someone asks what the veto was.

So every fact S4F sealed is asserted here directly, from the tracked state, with no model
and no re-derivation of any score:

    candidate 004   EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW, human decision HOLD, promoted NO
    candidate 005   EVALUATED_NOT_ELIGIBLE, promoted NO
    eval-v7         USED_IMMUTABLE, spent_by the S4E paired attempt
    accounting      1 paired attempt, 1 holdout spend, 36 + 36 = 72 generations
    production      unchanged

``test_no_s4h_module_can_reach_the_state_directory`` is the structural half: the
instruments cannot write state even by accident, because none of them knows where it is.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE = REPO_ROOT / "state" / "m62"
INSTRUMENTS = REPO_ROOT / "jarvis" / "training_gym" / "evaluation" / "instruments"

CANDIDATE004 = "qwen3-06b-lora-quality-live-004"
CANDIDATE005 = "qwen3-06b-lora-quality-live-005"


def pointer() -> dict:
    return json.loads((STATE / "current.json").read_text(encoding="utf-8"))


def snapshot() -> dict:
    return json.loads((REPO_ROOT / pointer()["latest_snapshot_path"]).read_text(
        encoding="utf-8"))


def record(block: str) -> object:
    digest = snapshot()["records"][block]
    payload = json.loads((STATE / "records" / f"{digest}.json").read_text(
        encoding="utf-8"))
    return payload["value"]


def candidate(candidate_id: str) -> dict:
    matches = [c for c in record("candidates") if c["candidate_id"] == candidate_id]
    assert len(matches) == 1, candidate_id
    return matches[0]


def dataset(dataset_id: str, version: str) -> dict:
    matches = [d for d in record("datasets")
               if d["dataset_id"] == dataset_id and d["version"] == version]
    assert len(matches) == 1, f"{dataset_id} {version}"
    return matches[0]


def eval_receipt(candidate_id: str) -> dict:
    return json.loads((STATE / "receipts" / f"{candidate_id}.eval.json").read_text(
        encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  §54 THE SEALED FACTS
# ══════════════════════════════════════════════════════════════════════════════
def test_candidate005_state_is_unchanged():
    entry = candidate(CANDIDATE005)
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["ordinal"] == 5
    assert entry["evaluation_corpus"] == "m62-defensive-eval v7"
    assert entry["evaluation_receipt"] == (
        f"state/m62/receipts/{CANDIDATE005}.eval.json")


def test_candidate005_is_not_promoted():
    receipt = eval_receipt(CANDIDATE005)
    assert receipt["outcome"]["eligibility"] == "not_eligible"
    assert receipt["outcome"]["promotes_model"] is False
    assert receipt["outcome"]["activates_model"] is False
    assert receipt["outcome"]["mutates_model_registry"] is False


def test_candidate005_receipt_still_records_the_security_veto():
    """The blockers are the reason. They are recorded, and none is edited by S4H."""
    blockers = eval_receipt(CANDIDATE005)["decision_evidence"][
        "canonical_decision"]["blockers"]
    assert any(b.startswith("new_security_regression") for b in blockers)
    assert any(b.startswith("new_secret_leaks") for b in blockers)


def test_candidate004_keeps_its_hold():
    entry = candidate(CANDIDATE004)
    assert entry["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert entry["evaluation_corpus"] == "m62-defensive-eval v6"
    receipt = eval_receipt(CANDIDATE004)
    assert receipt["outcome"]["human_review_required"] is True
    assert receipt["outcome"]["promotes_model"] is False


def test_eval_v7_is_used_immutable_and_names_who_spent_it():
    entry = dataset("m62-defensive-eval", "v7")
    assert entry["status"] == "USED_IMMUTABLE"
    assert entry["spent_by"] is not None
    assert "S4E LIVE" in entry["spent_by"]
    assert entry["task_count"] == 36
    assert entry["manifest_hash"] == (
        "e80cc46fa0b2c1ec020ed02f9565d778772d8e76dd208f2ba49349ab199b369a")
    assert entry["pack_hash"] == (
        "e6d8d0b28aa0c5e6c9d186ccc9f2c52371617ee46133199f73e25cbaf1750838")


def test_the_spent_by_string_is_unchanged():
    assert dataset("m62-defensive-eval", "v7")["spent_by"] == (
        "S4E LIVE, candidate 005 vs reference candidate 004 (Protocol V4, evaluation "
        "m62-s4e-reference-pair-live gen-1, plan 54488fb3, report d13fc339)")


def test_the_historical_generation_accounting_is_seventy_two():
    receipt = eval_receipt(CANDIDATE005)
    results = receipt["results"]
    assert results["expected_task_count"] == 36
    assert results["baseline_result_count"] == 36
    assert results["candidate_result_count"] == 36
    assert results["total_model_result_count"] == 72
    assert receipt["pairing"]["expected_generations"] == 72
    assert receipt["pairing"]["generations_per_task"] == 2


def test_one_paired_attempt_spent_one_holdout_exactly_once():
    ledger = eval_receipt(CANDIDATE005)["ledger"]
    assert ledger["plan_started_count"] == 1
    assert ledger["holdout_commit_count"] == 1
    assert ledger["terminal_count"] == 1
    assert ledger["unique_plan_hashes"] == 1
    assert eval_receipt(CANDIDATE005)["pairing"]["holdout_spends"] == 1
    assert eval_receipt(CANDIDATE005)["pairing"]["retry_authorized"] is False


def test_the_historical_measured_pairs_and_status_are_untouched():
    """S4F said 35 and partial_live. Coverage V2 corrects the FUTURE, not this record."""
    receipt = eval_receipt(CANDIDATE005)
    assert receipt["results"]["measured_pairs"] == 35
    assert receipt["execution"]["empirical_status"] == "partial_live"
    assert receipt["results"]["verdict_counts"]["security_regression"] == 1
    assert receipt["results"]["verdict_counts"]["security_improvement"] == 6


def test_the_sealing_session_measured_nothing():
    execution = eval_receipt(CANDIDATE005)["execution"]
    assert execution["model_loads_during_seal"] == 0
    assert execution["model_generations_during_seal"] == 0
    assert execution["sealed_from_existing_measurement"] is True


def test_no_sixth_candidate_and_no_eighth_holdout_exist():
    ids = {c["candidate_id"] for c in record("candidates")}
    assert f"{CANDIDATE005[:-3]}006" not in ids
    assert len(ids) == 5
    versions = {d["version"] for d in record("datasets")
                if d["dataset_id"] == "m62-defensive-eval"}
    assert "v8" not in versions


def test_no_holdout_became_available_and_eval_v5_is_still_frozen_unused():
    entry = dataset("m62-defensive-eval", "v5")
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None
    used = {d["version"] for d in record("datasets")
            if d["dataset_id"] == "m62-defensive-eval"
            and d["status"] == "USED_IMMUTABLE"}
    assert used == {"v1", "v2", "v3", "v4", "v6", "v7"}


def test_the_production_assignment_is_unchanged():
    """No candidate is active, and the control plane still cannot grant that."""
    observation = snapshot()["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert all(c["status"] != "PROMOTED" for c in record("candidates"))


def test_master_is_unchanged_and_nothing_was_merged_tagged_or_released():
    project = snapshot()["project"]
    assert project["master_commit"] == (
        "3705114228edef2f665be349c5c4429b7b16777a")
    assert project["merged_into_master"] is False
    assert project["tagged"] is False
    assert project["released"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  §68 THE STRUCTURAL HALF — S4H could not have moved any of it
# ══════════════════════════════════════════════════════════════════════════════
def code_names(module: str) -> tuple:
    """Every name the module's CODE references, docstrings and comments excluded.

    Grepping the raw source would fail on this package's own documentation: these
    modules describe the historical loader they replace, so ``from_pretrained`` and
    ``state/m62`` appear in prose. What matters is whether the module CALLS or IMPORTS
    any of it, and an AST answers that question and not the other one.
    """
    tree = ast.parse((INSTRUMENTS / module).read_text(encoding="utf-8"))
    names: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    return tuple(names)


def code_strings(module: str) -> tuple:
    """String literals the code evaluates, docstrings excluded."""
    tree = ast.parse((INSTRUMENTS / module).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return tuple(n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value not in docstrings)


INSTRUMENT_MODULES = sorted(p.name for p in INSTRUMENTS.glob("*.py"))


@pytest.mark.parametrize("module", INSTRUMENT_MODULES)
def test_no_s4h_instrument_module_can_reach_the_filesystem_or_the_state(module):
    """An instrument that cannot open a file cannot edit a receipt by accident."""
    names = code_names(module)
    for forbidden in ("open", "Path", "write_text", "read_text", "subprocess",
                      "environ", "getenv", "pathlib", "os", "shutil", "sys"):
        assert forbidden not in names, f"{module} uses {forbidden}"
    for literal in code_strings(module):
        assert "state/m62" not in literal, f"{module} names the state directory"


@pytest.mark.parametrize("module", INSTRUMENT_MODULES)
def test_no_s4h_instrument_module_loads_a_model_or_spends_anything(module):
    names = code_names(module)
    for forbidden in ("torch", "transformers", "peft", "from_pretrained", "generate",
                      "requests", "urllib", "socket"):
        assert forbidden not in names, f"{module} uses {forbidden}"
    for literal in code_strings(module):
        for forbidden in ("EVAL:", "TRAIN:", "--execute"):
            assert forbidden not in literal, f"{module} carries {forbidden}"


@pytest.mark.parametrize("module", INSTRUMENT_MODULES)
def test_no_s4h_instrument_module_defines_an_authority_verb(module):
    """A description is never permission. There is no function here that grants one."""
    tree = ast.parse((INSTRUMENTS / module).read_text(encoding="utf-8"))
    defined = {node.name for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for verb in ("promote", "activate", "spend", "authorise", "authorize", "execute",
                 "train", "evaluate", "run_evaluation"):
        assert verb not in defined, f"{module} defines {verb}()"


def test_the_instruments_package_has_no_module_outside_the_declared_slots():
    """A new module appearing here without review is itself the finding."""
    assert set(INSTRUMENT_MODULES) == {
        "__init__.py", "calibration.py", "coverage_v2.py", "finding.py",
        "refusal_v2.py", "runtime_contract.py", "secret_pii_v2.py", "stack.py",
        "tool_call_v2.py"}
