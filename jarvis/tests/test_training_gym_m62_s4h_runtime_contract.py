"""V69 M62 S4H — the explicit runtime contract, proved without loading a model.

WHAT THESE TESTS ARE FOR
------------------------
The generation policy that ran eval-v7 declares ``cpu`` and ``fp32``, and the loader that
executed it passes neither to ``from_pretrained``. Placement and numeric format were the
library's defaults; the receipt recorded the policy. Those are two different facts and
nothing compared them.

``test_a_load_that_omits_device_and_dtype_is_refused`` reproduces exactly the historical
loader's keyword set and requires enforcement to reject it. That is the regression this
milestone exists to prevent, and it is proved with a recorded dict — no framework, no
weights, no generation.

REAL_MODEL_LOADS = 0 for this entire file, by construction: nothing here imports torch,
transformers or peft, and a spy records kwargs instead of acting on them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_gym.evaluation.instruments import runtime_contract as RC  # noqa: E402

MODEL_ID = "Qwen/Qwen3-0.6B"
REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
CACHE_IDENTITY = "9" * 64


@pytest.fixture
def contract() -> RC.RuntimeContract:
    return RC.local_cpu_profile(
        model_id=MODEL_ID, model_revision=REVISION,
        tokenizer_id=MODEL_ID, tokenizer_revision=REVISION,
        cache_identity=CACHE_IDENTITY,
        adapter_strategy=RC.AdapterStrategy.REFERENCE_LORA)


class LoaderSpy:
    """Records the kwargs a load WOULD have received. Loads nothing, imports nothing."""

    def __init__(self) -> None:
        self.calls: list = []

    def from_pretrained(self, model_id, **kwargs):
        self.calls.append({"model_id": model_id, **kwargs})
        return object()


def observe(contract: RC.RuntimeContract, kwargs: dict) -> RC.ObservedRuntime:
    return RC.observe_from_loader_kwargs(
        kwargs, model_id=contract.model_id, tokenizer_id=contract.tokenizer_id,
        adapter_strategy=contract.adapter_strategy.value,
        load_strategy=contract.load_strategy.value,
        cache_identity=contract.cache_identity,
        tokenizer_revision=contract.tokenizer_revision,
        seed=contract.seed, max_new_tokens=contract.max_new_tokens)


# ══════════════════════════════════════════════════════════════════════════════
#  §40 EXPLICIT BINDING
# ══════════════════════════════════════════════════════════════════════════════
def test_the_contract_binds_every_identity_a_load_depends_on(contract):
    payload = contract.to_dict()
    for field in ("model_id", "model_revision", "tokenizer_id", "tokenizer_revision",
                  "device", "dtype", "adapter_strategy", "load_strategy",
                  "cache_identity", "seed", "max_new_tokens", "do_sample",
                  "temperature", "top_p", "top_k", "repetition_penalty",
                  "trust_remote_code", "local_files_only"):
        assert field in payload, field


def test_the_local_cpu_profile_states_cpu_and_float32_explicitly(contract):
    assert contract.device is RC.Device.CPU
    assert contract.dtype is RC.DType.FP32
    assert contract.to_dict()["device"] == "cpu"
    assert contract.to_dict()["dtype"] == "float32"


def test_there_is_no_auto_member_to_select(contract):
    """'Wherever it lands' is the defect, so the vocabulary cannot express it."""
    assert "auto" not in {m.value for m in RC.Device}
    assert "auto" not in {m.value for m in RC.DType}
    assert "auto_safe" not in {m.value for m in RC.Device}


def test_a_moving_revision_is_refused(contract):
    for moving in ("latest", "main", "HEAD", "auto", "default"):
        with pytest.raises(RC.RuntimeContractError, match="moving target"):
            RC.RuntimeContract.from_dict({**contract.to_dict(),
                                          "model_revision": moving})


def test_a_contract_cannot_be_built_without_a_cache_identity(contract):
    with pytest.raises(RC.RuntimeContractError, match="cache_identity"):
        RC.RuntimeContract.from_dict({**contract.to_dict(), "cache_identity": "  "})


def test_trust_remote_code_cannot_be_turned_on(contract):
    with pytest.raises(RC.RuntimeContractError, match="trust boundary"):
        RC.RuntimeContract.from_dict({**contract.to_dict(),
                                      "trust_remote_code": True})


def test_a_zero_token_budget_is_refused(contract):
    with pytest.raises(RC.RuntimeContractError, match="max_new_tokens"):
        RC.RuntimeContract.from_dict({**contract.to_dict(), "max_new_tokens": 0})


# ══════════════════════════════════════════════════════════════════════════════
#  §42 ENFORCEMENT — fails closed, with no model
# ══════════════════════════════════════════════════════════════════════════════
def test_the_production_loader_receives_explicit_parameters(contract):
    spy = LoaderSpy()
    spy.from_pretrained(contract.model_id, **contract.model_loader_kwargs())
    recorded = spy.calls[0]
    assert recorded["device_map"] == "cpu"
    assert recorded["dtype"] == "float32"
    assert recorded["revision"] == REVISION
    assert recorded["local_files_only"] is True
    assert recorded["trust_remote_code"] is False


def test_a_contracted_load_is_accepted(contract):
    RC.enforce_observed_runtime(contract, observe(contract,
                                                  contract.model_loader_kwargs()))


def test_a_load_that_omits_device_and_dtype_is_refused(contract):
    """The HISTORICAL loader's keyword set, replayed. It must not pass."""
    historical = {k: v for k, v in contract.model_loader_kwargs().items()
                  if k not in ("device_map", "dtype")}
    with pytest.raises(RC.RuntimeContractViolation) as caught:
        RC.enforce_observed_runtime(contract, observe(contract, historical))
    assert "device" in str(caught.value) and "dtype" in str(caught.value)


MISMATCHES = [
    ("contract_cpu_loader_cuda", "device", "cuda"),
    ("contract_cpu_loader_mps", "device", "mps"),
    ("fp32_contract_fp16_load", "dtype", "float16"),
    ("fp32_contract_bf16_load", "dtype", "bfloat16"),
    ("wrong_model_revision", "model_revision", "0" * 40),
    ("wrong_tokenizer_revision", "tokenizer_revision", "0" * 40),
    ("wrong_cache_identity", "cache_identity", "0" * 64),
    ("wrong_adapter_strategy", "adapter_strategy", "no_adapter"),
    ("wrong_load_strategy", "load_strategy", "shared_base_verified"),
    ("wrong_model_id", "model_id", "Qwen/Qwen3-1.7B"),
    ("wrong_seed", "seed", 12),
    ("wrong_token_budget", "max_new_tokens", 256),
    ("remote_code_enabled_at_load", "trust_remote_code", True),
    ("network_enabled_at_load", "local_files_only", False),
]


@pytest.mark.parametrize("label,field,value", MISMATCHES,
                         ids=[c[0] for c in MISMATCHES])
def test_every_enforced_field_fails_closed_on_a_mismatch(label, field, value,
                                                         contract):
    good = observe(contract, contract.model_loader_kwargs())
    bad = RC.ObservedRuntime(**{**good.to_dict(), field: value})
    with pytest.raises(RC.RuntimeContractViolation):
        RC.enforce_observed_runtime(contract, bad)
    assert field in RC.compare_runtime(contract, bad)


@pytest.mark.parametrize("field", [name for name, _ in RC._ENFORCED])
def test_an_unreported_field_is_a_mismatch_and_never_a_silent_agreement(field,
                                                                        contract):
    """An observation that omits a field must not be read as agreeing with it."""
    good = observe(contract, contract.model_loader_kwargs())
    blank = RC.ObservedRuntime(**{**good.to_dict(), field: None})
    assert field in RC.compare_runtime(contract, blank)


def test_there_is_no_fallback_branch_that_accepts_a_mismatch(contract):
    """Enforcement raises; it never downgrades to a warning or a partial pass."""
    empty = RC.ObservedRuntime()
    assert len(RC.compare_runtime(contract, empty)) == len(RC._ENFORCED)
    with pytest.raises(RC.RuntimeContractViolation):
        RC.enforce_observed_runtime(contract, empty)


def test_a_violation_names_fields_and_never_a_host_path(contract):
    bad = RC.ObservedRuntime(**{**observe(contract,
                                          contract.model_loader_kwargs()).to_dict(),
                                "cache_identity": "0" * 64})
    with pytest.raises(RC.RuntimeContractViolation) as caught:
        RC.enforce_observed_runtime(contract, bad)
    message = str(caught.value)
    assert "cache_identity" in message
    assert "/home" not in message and "0" * 64 not in message


# ══════════════════════════════════════════════════════════════════════════════
#  §43 SERIALIZATION
# ══════════════════════════════════════════════════════════════════════════════
def test_serialize_deserialize_serialize_produces_identical_bytes(contract):
    once = contract.canonical_bytes()
    twice = RC.RuntimeContract.from_dict(contract.to_dict()).canonical_bytes()
    thrice = RC.RuntimeContract.from_dict(
        RC.RuntimeContract.from_dict(contract.to_dict()).to_dict()).canonical_bytes()
    assert once == twice == thrice


def test_the_contract_hash_is_stable_and_moves_when_a_bound_field_moves(contract):
    assert contract.contract_hash() == contract.contract_hash()
    moved = RC.RuntimeContract.from_dict({**contract.to_dict(), "device": "cuda"})
    assert moved.contract_hash() != contract.contract_hash()


def test_generation_kwargs_are_greedy_when_sampling_is_off(contract):
    kwargs = contract.generation_kwargs()
    assert kwargs["do_sample"] is False
    assert kwargs["num_beams"] == 1
    assert "temperature" not in kwargs and "top_p" not in kwargs


# ══════════════════════════════════════════════════════════════════════════════
#  §41 NO REAL MODEL
# ══════════════════════════════════════════════════════════════════════════════
def test_the_contract_module_names_no_framework_and_calls_no_loader():
    """A runtime guarantee that needs a model to verify is one nobody verifies.

    Asserted against the SOURCE rather than ``sys.modules``: another test module in the
    same session may legitimately have imported a framework, so module presence proves
    nothing about this file.
    """
    source = Path(RC.__file__).read_text(encoding="utf-8")
    for forbidden in ("import torch", "import transformers", "import peft",
                      "from_pretrained(", "AutoModel", "PeftModel"):
        assert forbidden not in source, forbidden


def test_the_contract_version_is_pinned():
    assert RC.RUNTIME_CONTRACT_VERSION == "m62.runtime_contract.1"
