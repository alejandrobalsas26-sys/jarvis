"""training_gym/evaluation/instruments/runtime_contract.py — V69 M62 S4H.

WHAT WAS IMPLICIT
-----------------
The generation policy that S4E ran under declares ``device_policy: cpu`` and
``precision_policy: fp32``, and both are in the policy hash and the receipt. The loader
that actually ran is ``backends/transformers_peft.py::_generate``, and its two
``from_pretrained`` calls pass ``revision``, ``trust_remote_code``,
``local_files_only`` and ``cache_dir`` — and neither ``device_map`` nor a dtype.

So the placement and the numeric format were the library's defaults. On the machine that
ran eval-v7 those defaults happened to BE cpu and fp32, which is why the run is sound.
But "the report says fp32" and "the load was told fp32" were two different facts, and
nothing in the pipeline compared them. A host with a visible accelerator, or a library
release that changes a default, moves the second without moving the first, and the
receipt would still say fp32 because the receipt reads the policy.

A contract that is recorded but never handed to the thing it constrains is documentation.

WHAT THIS ADDS
--------------
One object that carries every identity a load depends on, a rendering of it into the
EXPLICIT keyword arguments a loader must receive, and an enforcement step that compares
the contract against what was actually observed and FAILS CLOSED on any difference.
There is no fallback branch: a mismatch raises, and an unknown observation is a mismatch.

NO MODEL IS INVOLVED. This module imports no framework, loads nothing, and generates
nothing. Enforcement is tested against recorded observations and spies, which is the
whole point — a runtime guarantee that can only be checked by running a model is a
guarantee nobody checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...schemas import SchemaError, canonical_json, sha256_obj

#: Bumped when a bound field is added or its meaning changes.
RUNTIME_CONTRACT_VERSION = "m62.runtime_contract.1"


class RuntimeContractError(SchemaError):
    """A contract that does not fully determine a load."""


class RuntimeContractViolation(SchemaError):
    """The observed runtime is not the contracted one. Always fatal, never a warning."""


class Device(str, Enum):
    """Where the weights go. There is no AUTO: 'wherever it lands' is the defect."""

    CPU = "cpu"
    CUDA = "cuda"
    MPS = "mps"


class DType(str, Enum):
    """The numeric format. Explicit, because a silent downcast changes every output."""

    FP32 = "float32"
    FP16 = "float16"
    BF16 = "bfloat16"


class AdapterStrategy(str, Enum):
    """What the arm attaches. ``NONE`` is a claim, not an omission."""

    NONE = "no_adapter"
    SINGLE_LORA = "single_lora"
    REFERENCE_LORA = "reference_lora"


class LoadStrategy(str, Enum):
    """How the two arms obtain their weights. Mirrors the backend's own vocabulary."""

    ISOLATED_LOADS = "isolated_loads"
    SHARED_BASE_VERIFIED = "shared_base_verified"


@dataclass(frozen=True)
class RuntimeContract:
    """Everything a load depends on, named once, in one place.

    Every field is required and none has a permissive default. A contract that could be
    built without saying which device it wants would let the same omission back in
    through a keyword argument nobody passed.
    """

    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    device: Device
    dtype: DType
    adapter_strategy: AdapterStrategy
    load_strategy: LoadStrategy
    #: The reviewed cache root's IDENTITY — a digest of the path plus its contents
    #: manifest — never the host path itself. A machine path is private content, and a
    #: contract is a thing receipts carry.
    cache_identity: str
    seed: int
    max_new_tokens: int
    do_sample: bool = False
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 0
    repetition_penalty: float = 1.0
    trust_remote_code: bool = False
    local_files_only: bool = True
    schema_version: str = RUNTIME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("model_id", "model_revision", "tokenizer_id",
                     "tokenizer_revision", "cache_identity"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeContractError(
                    f"runtime contract: {name} must be stated; a load that resolves it "
                    f"at run time is a load nobody pinned")
            if value.strip().lower() in ("latest", "main", "head", "auto", "default"):
                raise RuntimeContractError(
                    f"runtime contract: {name}={value!r} is a moving target. A contract "
                    f"that names 'latest' pins nothing and re-reads differently tomorrow")
        for name, enum_type in (("device", Device), ("dtype", DType),
                                ("adapter_strategy", AdapterStrategy),
                                ("load_strategy", LoadStrategy)):
            if not isinstance(getattr(self, name), enum_type):
                raise RuntimeContractError(
                    f"runtime contract: {name} must be a {enum_type.__name__}")
        for name in ("seed", "max_new_tokens", "top_k"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RuntimeContractError(
                    f"runtime contract: {name} must be a non-negative integer")
        if self.max_new_tokens <= 0:
            raise RuntimeContractError(
                "runtime contract: max_new_tokens must be positive; a budget of zero "
                "produces an empty response that scores as a refusal")
        if self.trust_remote_code:
            raise RuntimeContractError(
                "runtime contract: trust_remote_code must be false; executing repository "
                "code fetched with the weights is not a runtime setting, it is a new "
                "trust boundary")

    # -- serialization ----------------------------------------------------------
    def to_dict(self) -> dict:
        return {"adapter_strategy": self.adapter_strategy.value,
                "cache_identity": self.cache_identity,
                "device": self.device.value,
                "do_sample": self.do_sample,
                "dtype": self.dtype.value,
                "load_strategy": self.load_strategy.value,
                "local_files_only": self.local_files_only,
                "max_new_tokens": self.max_new_tokens,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "repetition_penalty": self.repetition_penalty,
                "schema_version": self.schema_version,
                "seed": self.seed,
                "temperature": self.temperature,
                "tokenizer_id": self.tokenizer_id,
                "tokenizer_revision": self.tokenizer_revision,
                "top_k": self.top_k,
                "top_p": self.top_p,
                "trust_remote_code": self.trust_remote_code}

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    def contract_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "RuntimeContract":
        if not isinstance(payload, dict):
            raise RuntimeContractError("runtime contract: payload must be a mapping")
        version = payload.get("schema_version")
        if version != RUNTIME_CONTRACT_VERSION:
            raise RuntimeContractError(
                f"runtime contract: schema_version {version!r} is not "
                f"{RUNTIME_CONTRACT_VERSION}")
        try:
            return cls(
                model_id=payload["model_id"],
                model_revision=payload["model_revision"],
                tokenizer_id=payload["tokenizer_id"],
                tokenizer_revision=payload["tokenizer_revision"],
                device=Device(payload["device"]),
                dtype=DType(payload["dtype"]),
                adapter_strategy=AdapterStrategy(payload["adapter_strategy"]),
                load_strategy=LoadStrategy(payload["load_strategy"]),
                cache_identity=payload["cache_identity"],
                seed=payload["seed"],
                max_new_tokens=payload["max_new_tokens"],
                do_sample=bool(payload["do_sample"]),
                temperature=float(payload["temperature"]),
                top_p=float(payload["top_p"]),
                top_k=int(payload["top_k"]),
                repetition_penalty=float(payload["repetition_penalty"]),
                trust_remote_code=bool(payload["trust_remote_code"]),
                local_files_only=bool(payload["local_files_only"]))
        except KeyError as exc:
            raise RuntimeContractError(
                f"runtime contract: payload omits {exc.args[0]!r}") from None
        except ValueError as exc:
            raise RuntimeContractError(f"runtime contract: {exc}") from None

    # -- the part the loader must actually receive ------------------------------
    def model_loader_kwargs(self) -> dict:
        """The EXPLICIT keyword arguments a weight load must be given.

        ``device_map`` and ``dtype`` are present unconditionally. That is the whole
        correction: a loader handed this dict cannot inherit a library default for
        either, and a loader NOT handed it fails :func:`enforce_observed_runtime`.
        """
        return {"revision": self.model_revision,
                "dtype": self.dtype.value,
                "device_map": self.device.value,
                "local_files_only": self.local_files_only,
                "trust_remote_code": self.trust_remote_code}

    def tokenizer_loader_kwargs(self) -> dict:
        return {"revision": self.tokenizer_revision,
                "local_files_only": self.local_files_only,
                "trust_remote_code": self.trust_remote_code}

    def generation_kwargs(self) -> dict:
        kwargs: dict = {"do_sample": self.do_sample,
                        "max_new_tokens": self.max_new_tokens,
                        "repetition_penalty": self.repetition_penalty}
        if self.do_sample:
            kwargs["temperature"] = self.temperature
            kwargs["top_p"] = self.top_p
            if self.top_k:
                kwargs["top_k"] = self.top_k
        else:
            kwargs["num_beams"] = 1
        return kwargs


@dataclass(frozen=True)
class ObservedRuntime:
    """What a load actually did, as reported by a loader, a spy or a recorded run.

    Every field defaults to the empty string rather than to the contract's value. An
    observation that omits a field is therefore a MISMATCH, never a silent agreement,
    which is the direction that catches a loader that forgot to pass something.
    """

    model_id: str = ""
    model_revision: str = ""
    tokenizer_id: str = ""
    tokenizer_revision: str = ""
    device: str = ""
    dtype: str = ""
    adapter_strategy: str = ""
    load_strategy: str = ""
    cache_identity: str = ""
    seed: "int | None" = None
    max_new_tokens: "int | None" = None
    trust_remote_code: "bool | None" = None
    local_files_only: "bool | None" = None

    def to_dict(self) -> dict:
        return {"adapter_strategy": self.adapter_strategy,
                "cache_identity": self.cache_identity,
                "device": self.device,
                "dtype": self.dtype,
                "load_strategy": self.load_strategy,
                "local_files_only": self.local_files_only,
                "max_new_tokens": self.max_new_tokens,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "seed": self.seed,
                "tokenizer_id": self.tokenizer_id,
                "tokenizer_revision": self.tokenizer_revision,
                "trust_remote_code": self.trust_remote_code}


#: Every field compared, and the contract attribute it is compared against. Written as
#: data so a new contract field cannot be added without a reviewer deciding whether it
#: is enforced — an unlisted field is enforced by nothing.
_ENFORCED: tuple = (
    ("model_id", "model_id"),
    ("model_revision", "model_revision"),
    ("tokenizer_id", "tokenizer_id"),
    ("tokenizer_revision", "tokenizer_revision"),
    ("device", "device"),
    ("dtype", "dtype"),
    ("adapter_strategy", "adapter_strategy"),
    ("load_strategy", "load_strategy"),
    ("cache_identity", "cache_identity"),
    ("seed", "seed"),
    ("max_new_tokens", "max_new_tokens"),
    ("trust_remote_code", "trust_remote_code"),
    ("local_files_only", "local_files_only"),
)


def compare_runtime(contract: RuntimeContract,
                    observed: ObservedRuntime) -> "tuple[str, ...]":
    """The names of every field that does not match. Empty means the contract held.

    Returns FIELD NAMES only. The values are the model's identity and a cache identity,
    which are not secret — but a comparison report is a thing that gets pasted into
    tickets, and a habit of printing observed values is how a host path eventually
    appears in one.
    """
    if not isinstance(contract, RuntimeContract):
        raise RuntimeContractError("compare_runtime: contract must be a RuntimeContract")
    if not isinstance(observed, ObservedRuntime):
        raise RuntimeContractError("compare_runtime: observed must be an ObservedRuntime")
    mismatched: list[str] = []
    for observed_name, contract_name in _ENFORCED:
        expected = getattr(contract, contract_name)
        expected = expected.value if isinstance(expected, Enum) else expected
        actual = getattr(observed, observed_name)
        if actual is None or actual == "":
            mismatched.append(observed_name)
            continue
        if actual != expected:
            mismatched.append(observed_name)
    return tuple(mismatched)


def enforce_observed_runtime(contract: RuntimeContract,
                             observed: ObservedRuntime) -> None:
    """Fail closed on any difference. There is no permissive branch to reach."""
    mismatched = compare_runtime(contract, observed)
    if mismatched:
        raise RuntimeContractViolation(
            f"runtime contract {contract.contract_hash()[:12]}: "
            f"{len(mismatched)} field(s) do not match what was contracted "
            f"({', '.join(mismatched)}). A measurement taken under settings the report "
            f"does not describe is not the measurement the report describes")


def observe_from_loader_kwargs(contract_free_kwargs: dict, *, model_id: str,
                               tokenizer_id: str, adapter_strategy: str,
                               load_strategy: str, cache_identity: str,
                               tokenizer_revision: str, seed: int,
                               max_new_tokens: int) -> ObservedRuntime:
    """Build an observation from the kwargs a loader was ACTUALLY called with.

    This is the bridge a test (or a future backend) uses: record the dict that reached
    ``from_pretrained``, hand it here, and enforce. A loader that omitted ``device_map``
    or ``dtype`` produces an observation missing those fields, and enforcement fails —
    which is precisely the case the historical loader would not have survived.
    """
    return ObservedRuntime(
        model_id=model_id,
        model_revision=str(contract_free_kwargs.get("revision", "")),
        tokenizer_id=tokenizer_id,
        tokenizer_revision=tokenizer_revision,
        device=str(contract_free_kwargs.get("device_map", "")),
        dtype=str(contract_free_kwargs.get("dtype", "")),
        adapter_strategy=adapter_strategy,
        load_strategy=load_strategy,
        cache_identity=cache_identity,
        seed=seed,
        max_new_tokens=max_new_tokens,
        trust_remote_code=contract_free_kwargs.get("trust_remote_code"),
        local_files_only=contract_free_kwargs.get("local_files_only"))


#: The profile the local evaluation host actually runs, stated explicitly so a future
#: config can cite it instead of re-deriving it. It is a TEMPLATE, not an authority:
#: instantiating it loads nothing and permits nothing.
def local_cpu_profile(*, model_id: str, model_revision: str, tokenizer_id: str,
                      tokenizer_revision: str, cache_identity: str,
                      adapter_strategy: AdapterStrategy, seed: int = 11,
                      max_new_tokens: int = 512) -> RuntimeContract:
    """CPU + float32, greedy, offline, isolated loads. Every value named, none inferred."""
    return RuntimeContract(
        model_id=model_id, model_revision=model_revision,
        tokenizer_id=tokenizer_id, tokenizer_revision=tokenizer_revision,
        device=Device.CPU, dtype=DType.FP32,
        adapter_strategy=adapter_strategy,
        load_strategy=LoadStrategy.ISOLATED_LOADS,
        cache_identity=cache_identity, seed=seed, max_new_tokens=max_new_tokens,
        do_sample=False, temperature=0.0, top_p=1.0, top_k=0,
        repetition_penalty=1.0, trust_remote_code=False, local_files_only=True)


__all__ = ["AdapterStrategy", "DType", "Device", "LoadStrategy", "ObservedRuntime",
           "RUNTIME_CONTRACT_VERSION", "RuntimeContract", "RuntimeContractError",
           "RuntimeContractViolation", "compare_runtime", "enforce_observed_runtime",
           "local_cpu_profile", "observe_from_loader_kwargs"]
