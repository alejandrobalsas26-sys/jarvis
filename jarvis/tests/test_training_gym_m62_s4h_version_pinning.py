"""V69 M62 S4H — version pinning: a future config selects nothing, it pins everything.

WHAT THESE TESTS ARE FOR
------------------------
Candidate 005's receipt is still verifiable because every scorer it names has a frozen
identity. The instruments S4H adds are only useful if the same holds for them: an
evaluation that resolved "the current secret detector" would produce receipts that stop
meaning the same thing the moment a rule is added.

So the stack REFUSES rather than resolves, and each refusal is pinned here:

    latest / current / auto      a moving target by name
    an unknown version           including a NEWER one — failing closed is the point
    a missing required slot      silence is not a default
    a missing runtime contract   a run with no contract has no stated device or dtype

The last group asserts the property that makes all of this safe for HISTORY: nothing in
the historical scoring path imports this package, so a receipt sealed before S4H has no
"latest instrument" to drift onto.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_gym.evaluation import instruments as I  # noqa: E402
from training_gym.evaluation.instruments import runtime_contract as RC  # noqa: E402
from training_gym.evaluation.instruments import stack as S  # noqa: E402

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SCORER_MODULES = (
    "training_gym/evaluation/scoring.py",
    "training_gym/evaluation/gates.py",
    "training_gym/evaluation/policy.py",
    "training_gym/evaluation/statistics.py",
    "training_gym/evaluation/metrics.py",
    "training_gym/evaluation/comparison.py",
    "training_gym/evaluation/reports.py",
    "training_gym/evaluation/protocol_v4.py",
    "training_gym/evaluation/score_evidence.py",
    "training_gym/evaluation/execution.py",
    "training_gym/evaluation/execution_v4.py",
    "training_gym/evaluation/runner.py",
    "training_gym/evaluation/runner_v4.py",
    "training_gym/evaluation/backends/transformers_peft.py",
)


@pytest.fixture
def runtime() -> RC.RuntimeContract:
    return RC.local_cpu_profile(
        model_id="Qwen/Qwen3-0.6B", model_revision="c" * 40,
        tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision="c" * 40,
        cache_identity="d" * 64,
        adapter_strategy=RC.AdapterStrategy.REFERENCE_LORA)


@pytest.fixture
def config(runtime) -> dict:
    return {"instruments": I.current_versions(),
            "runtime_contract": runtime.to_dict()}


# ══════════════════════════════════════════════════════════════════════════════
#  §56 A CORRECT PIN
# ══════════════════════════════════════════════════════════════════════════════
def test_a_fully_pinned_config_resolves(config):
    stack = S.validate_config(config)
    assert set(S.REQUIRED_SLOTS) <= set(stack.pins)
    assert stack.stack_hash() == S.validate_config(config).stack_hash()


def test_every_slot_pins_a_version_this_build_actually_provides(config):
    stack = S.validate_config(config)
    for slot, version in stack.pins.items():
        assert version in S.INSTRUMENT_REGISTRY[slot]


def test_the_stack_hash_binds_the_runtime_contract_as_well_as_the_instruments(
        config, runtime):
    base = S.validate_config(config)
    moved_runtime = {**config,
                     "runtime_contract": {**runtime.to_dict(), "device": "cuda"}}
    assert S.validate_config(moved_runtime).stack_hash() != base.stack_hash()


def test_the_expected_versions_are_the_ones_s4h_declares():
    assert I.current_versions() == {
        "calibration": "m62.instrument_calibration.1",
        "coverage": "m62.coverage_semantics.2",
        "finding_schema": "m62.finding_schema.2",
        "refusal": "m62.refusal_behavior.2",
        "runtime_contract": "m62.runtime_contract.1",
        "secret_pii": "m62.secret_pii_detector.2",
        "tool_call": "m62.tool_call_validator.2"}


# ══════════════════════════════════════════════════════════════════════════════
#  §57 FAIL CLOSED
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("moving", ["latest", "current", "newest", "auto", "default",
                                    "any", "*", ""])
def test_a_moving_target_is_refused_by_name(moving, config):
    broken = {**config,
              "instruments": {**config["instruments"], "secret_pii": moving}}
    with pytest.raises(S.InstrumentStackError):
        S.validate_config(broken)


def test_an_unknown_version_fails_closed_even_when_it_looks_newer(config):
    broken = {**config, "instruments": {**config["instruments"],
                                        "secret_pii": "m62.secret_pii_detector.3"}}
    with pytest.raises(S.InstrumentStackError, match="does not provide"):
        S.validate_config(broken)


@pytest.mark.parametrize("slot", S.REQUIRED_SLOTS)
def test_a_missing_required_slot_is_refused(slot, config):
    broken = {**config,
              "instruments": {k: v for k, v in config["instruments"].items()
                              if k != slot}}
    with pytest.raises(S.InstrumentStackError, match="unpinned"):
        S.validate_config(broken)


def test_a_missing_runtime_contract_is_refused(config):
    broken = {k: v for k, v in config.items() if k != "runtime_contract"}
    with pytest.raises(S.InstrumentStackError, match="runtime_contract"):
        S.validate_config(broken)


def test_a_config_naming_no_instruments_is_refused(runtime):
    with pytest.raises(S.InstrumentStackError, match="instruments"):
        S.validate_config({"runtime_contract": runtime.to_dict()})


def test_an_unknown_slot_is_refused(config):
    broken = {**config, "instruments": {**config["instruments"],
                                        "vibes": "m62.vibes.1"}}
    with pytest.raises(S.InstrumentStackError, match="known instrument slot"):
        S.validate_config(broken)


def test_a_mixed_generation_scoring_stack_is_refused(config, runtime):
    """Instruments from two families have no single meaning together."""
    with pytest.raises(S.InstrumentStackError):
        S.InstrumentStack(pins={**config["instruments"],
                                "coverage": "m63.coverage_semantics.2"},
                          runtime=runtime)


def test_a_non_string_pin_is_refused(config):
    broken = {**config, "instruments": {**config["instruments"], "refusal": 2}}
    with pytest.raises(S.InstrumentStackError, match="version string"):
        S.validate_config(broken)


def test_current_versions_is_documentation_and_not_a_resolver(config):
    """It is never called by the validation path, so nothing pins implicitly."""
    source = Path(S.__file__).read_text(encoding="utf-8")
    validate = source.split("def validate_config", 1)[1]
    assert "current_versions(" not in validate


# ══════════════════════════════════════════════════════════════════════════════
#  §55 NO IMPLICIT LATEST FOR HISTORY
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("module", HISTORICAL_SCORER_MODULES)
def test_no_historical_scoring_module_imports_the_s4h_instruments(module):
    """The property that keeps every frozen digest and every sealed receipt intact."""
    source = (PACKAGE_ROOT / module).read_text(encoding="utf-8")
    for marker in ("instruments", "secret_pii_v2", "refusal_v2", "tool_call_v2",
                   "coverage_v2", "runtime_contract"):
        assert f"import {marker}" not in source, f"{module} imports {marker}"
        assert "from .instruments" not in source, module
        assert "from ..instruments" not in source, module


def test_the_instruments_package_is_not_reachable_from_the_evaluation_package_init():
    """Importing ``training_gym.evaluation`` must not pull the new instruments in."""
    source = (PACKAGE_ROOT / "training_gym/evaluation/__init__.py").read_text(
        encoding="utf-8")
    assert "instruments" not in source


def test_the_instrument_versions_are_separate_from_the_protocol_version():
    """§11: instrument version and evaluation-protocol version are different concepts."""
    from training_gym.evaluation.protocol_v4 import EVALUATION_PROTOCOL_V4_VERSION
    assert EVALUATION_PROTOCOL_V4_VERSION == "m62.evaluation_protocol.4"
    assert EVALUATION_PROTOCOL_V4_VERSION not in I.current_versions().values()


def test_the_package_version_is_distinct_from_every_instrument_version():
    assert I.INSTRUMENTS_PACKAGE_VERSION == "m62.evaluation_instruments.1"
    assert I.INSTRUMENTS_PACKAGE_VERSION not in I.current_versions().values()
