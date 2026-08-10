"""training_gym/evaluation/generation.py — V69 M62 S3C: how both arms are asked.

WHY A CLOSED POLICY
-------------------
A base-versus-adapter comparison is only a measurement of the adapter if the two arms
were asked identically. The easiest way to lose that property is not malice — it is a
provider default. One arm gets ``temperature=1.0`` because nothing said otherwise, the
other gets ``0.0`` because a caller passed it, and the resulting delta is decoding noise
wearing a capability claim.

So this module owns a *closed* generation policy: every knob that can change an output is
a named field, every field is inside the plan digest, and the same frozen object is handed
to both arms. There is no "backend default" left to differ, because the backend is not
permitted to supply one.

DETERMINISM IS A CONFIGURATION, NOT A GUARANTEE
-----------------------------------------------
``GREEDY_DETERMINISTIC`` sets ``do_sample=False`` and pins the seed. That removes sampling
as a source of variance. It does NOT promise bit-identical output across hosts,
accelerators or library versions — S3B already refuses to make that claim on accelerator
hardware (``deterministic_reproduction_claimed: false``), and this stage does not make a
stronger one from a stage that has run even less code.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..schemas import (
    SchemaError,
    require_bool,
    require_enum,
    require_float,
    require_int,
    require_str_tuple,
    sha256_obj,
)
from ..training.config import DevicePolicy, PrecisionPolicy

#: Bumped when the policy's shape changes, invalidating outstanding confirmations.
GENERATION_POLICY_VERSION = "m62.generation_policy.2"

#: Ceilings. Not tuned — bounded, so a malformed config cannot request an unbounded run.
MAX_NEW_TOKENS_CEILING = 8_192
MAX_INPUT_TOKENS_CEILING = 131_072
MAX_STOP_SEQUENCES = 8
MAX_STOP_SEQUENCE_CHARS = 64
MAX_BATCH_SIZE = 8
MAX_GENERATION_TIMEOUT_S = 3_600


class GenerationPolicyError(SchemaError):
    """A generation policy that would not produce a comparable measurement."""


class GenerationMode(str, Enum):
    """The two ways a response may be produced. There is no third.

    ``SEEDED_SAMPLING_DIAGNOSTIC`` exists because sampling behaviour is sometimes worth
    *looking at*. It is named "diagnostic" because a sampled run may never decide
    candidate eligibility: two draws from a distribution differ for reasons that have
    nothing to do with the adapter.
    """

    GREEDY_DETERMINISTIC = "greedy_deterministic"
    SEEDED_SAMPLING_DIAGNOSTIC = "seeded_sampling_diagnostic"

    @property
    def eligibility_grade(self) -> bool:
        """Whether a result produced under this mode may decide eligibility."""
        return self is GenerationMode.GREEDY_DETERMINISTIC


class ReasoningPolicy(str, Enum):
    """Whether the chat template is asked to let the model think before answering.

    S3E.2 passed nothing, so the template's own default decided — and for a reasoning
    model that default is "think", which put a ``<think>`` block in front of every one
    of the 72 responses. That is a setting that changes every output, and a setting that
    changes every output belongs in the digest both arms are compared under, not in a
    kwarg the backend happens to omit.
    """

    #: Pass nothing; the chat template decides. What S3E.2 actually did, and therefore
    #: the default, so no existing configuration silently changes behaviour.
    MODEL_DEFAULT = "model_default"
    #: Ask the template for ``enable_thinking=False``.
    DISABLED = "disabled"
    #: Ask the template for ``enable_thinking=True``.
    ENABLED = "enabled"

    @property
    def template_kwarg(self) -> bool | None:
        """The value to pass, or ``None`` to pass nothing at all."""
        if self is ReasoningPolicy.MODEL_DEFAULT:
            return None
        return self is ReasoningPolicy.ENABLED


class TruncationSide(str, Enum):
    """Which end of an over-long input is dropped when truncation is permitted."""

    #: Drop the oldest context. The instruction and the question survive.
    LEFT = "left"
    #: Refuse instead of truncating. The only setting that cannot silently delete
    #: the evidence a task requires the model to cite.
    REFUSE = "refuse"


#: Device policies an evaluation may name. DirectML is deliberately absent: the training
#: stack recognises it as an inference path on this repository's Windows hosts but has
#: never validated it, and an evaluation that silently ran somewhere unvalidated would be
#: a measurement of an unknown runtime.
EVALUATION_DEVICE_POLICIES: frozenset[DevicePolicy] = frozenset(
    {DevicePolicy.AUTO_SAFE, DevicePolicy.CPU, DevicePolicy.CUDA})

#: Precision policies an evaluation may name. The training-only quantised formats are
#: excluded: ``int8_training`` and ``int4_qlora`` describe how weights are *fitted*.
EVALUATION_PRECISION_POLICIES: frozenset[PrecisionPolicy] = frozenset(
    {PrecisionPolicy.AUTO_SAFE, PrecisionPolicy.FP32, PrecisionPolicy.FP16,
     PrecisionPolicy.BF16})


@dataclass(frozen=True)
class GenerationPolicy:
    """Every setting that can change an output, named once and shared by both arms."""

    mode: GenerationMode = GenerationMode.GREEDY_DETERMINISTIC
    max_new_tokens: int = 512
    max_input_tokens: int = 4_096
    do_sample: bool = False
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 0
    repetition_penalty: float = 1.0
    stop_sequences: tuple[str, ...] = ()
    seed: int = 0
    timeout_s: int = 120
    batch_size: int = 1
    truncation_side: TruncationSide = TruncationSide.REFUSE
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.MODEL_DEFAULT
    device_policy: DevicePolicy = DevicePolicy.AUTO_SAFE
    precision_policy: PrecisionPolicy = PrecisionPolicy.AUTO_SAFE
    policy_version: str = GENERATION_POLICY_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "mode", require_enum(self.mode, GenerationMode, "generation.mode"))
        set_(self, "truncation_side", require_enum(self.truncation_side, TruncationSide,
                                                   "generation.truncation_side"))
        set_(self, "reasoning_policy", require_enum(self.reasoning_policy,
                                                    ReasoningPolicy,
                                                    "generation.reasoning_policy"))
        set_(self, "device_policy", require_enum(self.device_policy, DevicePolicy,
                                                 "generation.device_policy"))
        set_(self, "precision_policy", require_enum(self.precision_policy,
                                                    PrecisionPolicy,
                                                    "generation.precision_policy"))
        set_(self, "do_sample", require_bool(self.do_sample, "generation.do_sample"))
        set_(self, "max_new_tokens",
             require_int(self.max_new_tokens, "generation.max_new_tokens",
                         minimum=1, maximum=MAX_NEW_TOKENS_CEILING))
        set_(self, "max_input_tokens",
             require_int(self.max_input_tokens, "generation.max_input_tokens",
                         minimum=1, maximum=MAX_INPUT_TOKENS_CEILING))
        set_(self, "top_k", require_int(self.top_k, "generation.top_k",
                                        minimum=0, maximum=1_000))
        set_(self, "seed", require_int(self.seed, "generation.seed",
                                       minimum=0, maximum=2**31 - 1))
        set_(self, "timeout_s",
             require_int(self.timeout_s, "generation.timeout_s",
                         minimum=1, maximum=MAX_GENERATION_TIMEOUT_S))
        set_(self, "batch_size", require_int(self.batch_size, "generation.batch_size",
                                             minimum=1, maximum=MAX_BATCH_SIZE))
        set_(self, "temperature", require_float(self.temperature,
                                                "generation.temperature",
                                                minimum=0.0, maximum=2.0))
        set_(self, "top_p", require_float(self.top_p, "generation.top_p",
                                          minimum=0.0, maximum=1.0))
        set_(self, "repetition_penalty",
             require_float(self.repetition_penalty, "generation.repetition_penalty",
                           minimum=0.5, maximum=2.0))
        set_(self, "stop_sequences", self._clean_stops())

        if self.device_policy not in EVALUATION_DEVICE_POLICIES:
            raise GenerationPolicyError(
                f"generation.device_policy: {self.device_policy.value!r} is not a "
                f"validated evaluation device on this repository; allowed "
                f"{sorted(d.value for d in EVALUATION_DEVICE_POLICIES)}")
        if self.precision_policy not in EVALUATION_PRECISION_POLICIES:
            raise GenerationPolicyError(
                f"generation.precision_policy: {self.precision_policy.value!r} describes "
                f"how weights are FITTED, not how they are read; allowed "
                f"{sorted(p.value for p in EVALUATION_PRECISION_POLICIES)}")
        self._validate_mode()

    def _clean_stops(self) -> tuple[str, ...]:
        stops = require_str_tuple(self.stop_sequences, "generation.stop_sequences",
                                  max_items=MAX_STOP_SEQUENCES)
        for stop in stops:
            if not stop:
                raise GenerationPolicyError(
                    "generation.stop_sequences: an empty stop sequence matches at "
                    "position zero, so every response would be empty")
            if len(stop) > MAX_STOP_SEQUENCE_CHARS:
                raise GenerationPolicyError(
                    f"generation.stop_sequences: {len(stop)} characters exceeds "
                    f"{MAX_STOP_SEQUENCE_CHARS}")
        if len(set(stops)) != len(stops):
            raise GenerationPolicyError(
                "generation.stop_sequences: duplicated entry; two spellings of one rule "
                "make the policy digest depend on authoring order")
        return tuple(stops)

    def _validate_mode(self) -> None:
        """Greedy means greedy. A mode that samples under a greedy label is the exact
        mislabelling that turns decoding noise into a capability claim."""
        if self.mode is GenerationMode.GREEDY_DETERMINISTIC:
            if self.do_sample:
                raise GenerationPolicyError(
                    "generation: GREEDY_DETERMINISTIC with do_sample=true is not greedy; "
                    "one of the two is a lie and the report would carry the wrong one")
            if self.temperature != 0.0:
                raise GenerationPolicyError(
                    f"generation: GREEDY_DETERMINISTIC requires temperature 0.0, got "
                    f"{self.temperature}; a non-zero temperature under a greedy label "
                    f"records a setting the run did not use")
            if self.top_k != 0 or self.top_p != 1.0:
                raise GenerationPolicyError(
                    "generation: GREEDY_DETERMINISTIC requires top_k=0 and top_p=1.0; "
                    "truncating a distribution that is never sampled from is a setting "
                    "with no meaning, and an unmeaning setting in a digest is noise")
            return
        # SEEDED_SAMPLING_DIAGNOSTIC
        if not self.do_sample:
            raise GenerationPolicyError(
                "generation: SEEDED_SAMPLING_DIAGNOSTIC with do_sample=false does not "
                "sample; name it GREEDY_DETERMINISTIC so the report says what ran")
        if self.temperature <= 0.0:
            raise GenerationPolicyError(
                "generation: sampling at temperature 0.0 is greedy decoding under "
                "another name")

    # -- derived ---------------------------------------------------------------
    @property
    def eligibility_grade(self) -> bool:
        """Whether results under this policy may decide candidate eligibility."""
        return self.mode.eligibility_grade

    @property
    def truncation_permitted(self) -> bool:
        return self.truncation_side is not TruncationSide.REFUSE

    def blockers(self) -> tuple[str, ...]:
        """Why this policy could not decide eligibility. Empty means it could."""
        problems: list[str] = []
        if not self.eligibility_grade:
            problems.append(
                f"generation mode {self.mode.value} is diagnostic only; candidate "
                f"eligibility requires {GenerationMode.GREEDY_DETERMINISTIC.value}")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "mode": self.mode.value,
            "max_new_tokens": self.max_new_tokens,
            "max_input_tokens": self.max_input_tokens,
            "do_sample": self.do_sample,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
            "stop_sequences": list(self.stop_sequences),
            "seed": self.seed,
            "timeout_s": self.timeout_s,
            "batch_size": self.batch_size,
            "truncation_side": self.truncation_side.value,
            "reasoning_policy": self.reasoning_policy.value,
            "device_policy": self.device_policy.value,
            "precision_policy": self.precision_policy.value,
        }

    def policy_hash(self) -> str:
        return sha256_obj(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> "GenerationPolicy":
        from ..schemas import reject_unknown_fields, require_mapping
        data = require_mapping(payload, "generation policy")
        allowed = set(cls().to_dict())
        reject_unknown_fields(data, allowed, label="generation policy")
        known = {k: v for k, v in data.items() if k != "policy_version"}
        return cls(**known)  # type: ignore[arg-type]


#: The one policy an eligibility-grade evaluation uses unless a config defensibly
#: overrides individual bounds. Named so a test can assert the default is greedy.
DEFAULT_GENERATION_POLICY = GenerationPolicy()


def assert_identical_policies(baseline: GenerationPolicy,
                              candidate: GenerationPolicy) -> str:
    """Refuse a comparison whose two arms were configured differently.

    Returns the shared policy hash. Deliberately compares the DIGEST rather than the
    object: two policies that differ only in a field a future version adds would still
    compare equal under ``==`` if that field were excluded from the dataclass, and the
    digest is what the plan and the report both carry.
    """
    left, right = baseline.policy_hash(), candidate.policy_hash()
    if left != right:
        raise GenerationPolicyError(
            f"evaluation: the baseline arm was configured with generation policy "
            f"{left[:12]} and the candidate arm with {right[:12]}. A comparison between "
            f"two differently-decoded arms measures the decoder, not the adapter")
    return left


__all__ = [
    "DEFAULT_GENERATION_POLICY", "EVALUATION_DEVICE_POLICIES",
    "EVALUATION_PRECISION_POLICIES", "GENERATION_POLICY_VERSION",
    "ReasoningPolicy",
    "MAX_BATCH_SIZE", "MAX_GENERATION_TIMEOUT_S", "MAX_INPUT_TOKENS_CEILING",
    "MAX_NEW_TOKENS_CEILING", "MAX_STOP_SEQUENCES", "GenerationMode",
    "GenerationPolicy", "GenerationPolicyError", "TruncationSide",
    "assert_identical_policies",
]
