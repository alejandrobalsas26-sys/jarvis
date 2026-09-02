"""training_gym/evaluation/instruments/stack.py — V69 M62 S4H: version pinning.

WHY PINNING IS THE POINT
------------------------
The reason candidate 005's receipt can still be verified is that every scorer it names
has a frozen digest and a fixed identity. The moment an evaluation resolves an instrument
by "whatever is current", two runs a month apart stop being comparable and no receipt can
say which instrument produced it.

So a future evaluation config does not select instruments. It PINS them, by exact version
string, and this module refuses anything else:

  * an unknown version — including one that is merely newer — fails closed;
  * the literals ``latest``, ``current``, ``newest``, ``auto`` and ``default`` are
    refused by name, because they are the specific mistake this exists to prevent;
  * a missing runtime contract fails closed, because a run with no contract is a run
    whose device and dtype are decided by a library;
  * an unpinned coverage semantics fails closed, because that is what produced the
    ``partial_live`` ambiguity.

RESOLUTION IS EXPLICIT AND HISTORICAL RECEIPTS ARE NOT AFFECTED. Nothing here reads the
"newest" registry entry, and nothing historical resolves through this module at all:
receipts sealed before S4H name their scorers directly and are verified by the frozen
policy digests, not by this registry.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...schemas import SchemaError, sha256_obj
from .calibration import INSTRUMENT_CALIBRATION_VERSION
from .coverage_v2 import COVERAGE_SEMANTICS_VERSION
from .finding import FINDING_SCHEMA_VERSION
from .refusal_v2 import REFUSAL_BEHAVIOR_VERSION
from .runtime_contract import RUNTIME_CONTRACT_VERSION, RuntimeContract
from .secret_pii_v2 import SECRET_PII_DETECTOR_VERSION
from .tool_call_v2 import TOOL_CALL_VALIDATOR_VERSION

INSTRUMENT_STACK_VERSION = "m62.instrument_stack.1"


class InstrumentStackError(SchemaError):
    """A configuration that does not fully determine which instruments will run."""


#: Every instrument slot, and every version this build can actually provide. A slot with
#: several members is one where an older version is still selectable on purpose; a slot
#: with one member is one where nothing else has ever shipped.
INSTRUMENT_REGISTRY: dict = {
    "secret_pii": frozenset({SECRET_PII_DETECTOR_VERSION}),
    "refusal": frozenset({REFUSAL_BEHAVIOR_VERSION}),
    "tool_call": frozenset({TOOL_CALL_VALIDATOR_VERSION}),
    "coverage": frozenset({COVERAGE_SEMANTICS_VERSION}),
    "finding_schema": frozenset({FINDING_SCHEMA_VERSION}),
    "runtime_contract": frozenset({RUNTIME_CONTRACT_VERSION}),
    "calibration": frozenset({INSTRUMENT_CALIBRATION_VERSION}),
}

#: Slots a future evaluation MUST pin. Omitting one is a refusal, not a default.
REQUIRED_SLOTS: tuple = ("secret_pii", "refusal", "tool_call", "coverage",
                         "finding_schema", "runtime_contract")

#: Words that mean "resolve it for me". Refused by name so the error says why.
_MOVING_TARGETS: frozenset = frozenset({"latest", "current", "newest", "auto",
                                        "default", "any", "*", ""})


@dataclass(frozen=True)
class InstrumentStack:
    """The exact instruments one future evaluation will use. Immutable and hashable."""

    pins: dict
    runtime: RuntimeContract
    stack_version: str = INSTRUMENT_STACK_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.pins, dict) or not self.pins:
            raise InstrumentStackError(
                "instrument stack: pins must be a non-empty mapping of slot -> version")
        if not isinstance(self.runtime, RuntimeContract):
            raise InstrumentStackError(
                "instrument stack: a runtime contract is required; an evaluation with "
                "no contract has no stated device, dtype or cache identity")
        for slot in REQUIRED_SLOTS:
            if slot not in self.pins:
                raise InstrumentStackError(
                    f"instrument stack: required slot {slot!r} is unpinned. Every slot "
                    f"is pinned or the run is not reproducible")
        for slot, version in sorted(self.pins.items()):
            if slot not in INSTRUMENT_REGISTRY:
                raise InstrumentStackError(
                    f"instrument stack: {slot!r} is not a known instrument slot")
            if not isinstance(version, str):
                raise InstrumentStackError(
                    f"instrument stack: slot {slot!r} must pin a version string")
            if version.strip().lower() in _MOVING_TARGETS:
                raise InstrumentStackError(
                    f"instrument stack: slot {slot!r} pins {version!r}, which resolves "
                    f"differently over time. Two runs pinned to 'latest' are not "
                    f"comparable and neither receipt can say which instrument ran")
            if version not in INSTRUMENT_REGISTRY[slot]:
                raise InstrumentStackError(
                    f"instrument stack: slot {slot!r} pins {version!r}, which this build "
                    f"does not provide. Failing closed: an unknown version silently "
                    f"falling back to a known one is how a receipt comes to name an "
                    f"instrument that never ran")
        family = {v.rsplit(".", 1)[0] for slot, v in self.pins.items()
                  if slot in ("secret_pii", "refusal", "tool_call", "coverage")}
        prefixes = {f.split(".", 1)[0] for f in family}
        if len(prefixes) > 1:
            raise InstrumentStackError(
                f"instrument stack: the scoring instruments span {sorted(prefixes)}; a "
                f"mixed-generation stack has no single meaning")

    def stack_hash(self) -> str:
        return sha256_obj({"pins": dict(sorted(self.pins.items())),
                           "runtime_contract_hash": self.runtime.contract_hash(),
                           "stack_version": self.stack_version})

    def to_dict(self) -> dict:
        return {"pins": dict(sorted(self.pins.items())),
                "runtime_contract": self.runtime.to_dict(),
                "stack_hash": self.stack_hash(),
                "stack_version": self.stack_version}

    def __repr__(self) -> str:
        return (f"InstrumentStack(slots={len(self.pins)}, "
                f"stack_hash={self.stack_hash()[:12]})")


def current_versions() -> dict:
    """What THIS build provides, for a config author to copy into an explicit pin.

    Deliberately NOT a resolver. It returns a mapping a human reads and pastes; nothing
    in the validation path calls it, so no run can end up pinned to "whatever this build
    happened to have" without someone writing the version down first.
    """
    return {"calibration": INSTRUMENT_CALIBRATION_VERSION,
            "coverage": COVERAGE_SEMANTICS_VERSION,
            "finding_schema": FINDING_SCHEMA_VERSION,
            "refusal": REFUSAL_BEHAVIOR_VERSION,
            "runtime_contract": RUNTIME_CONTRACT_VERSION,
            "secret_pii": SECRET_PII_DETECTOR_VERSION,
            "tool_call": TOOL_CALL_VALIDATOR_VERSION}


def validate_config(payload: dict) -> InstrumentStack:
    """Build a stack from a config mapping. Every refusal is a NAMED failure."""
    if not isinstance(payload, dict):
        raise InstrumentStackError("instrument config: payload must be a mapping")
    pins = payload.get("instruments")
    if not isinstance(pins, dict):
        raise InstrumentStackError(
            "instrument config: an 'instruments' mapping of slot -> pinned version is "
            "required; a config that names no instruments selects them at run time")
    runtime = payload.get("runtime_contract")
    if runtime is None:
        raise InstrumentStackError(
            "instrument config: 'runtime_contract' is required and has no default")
    contract = (runtime if isinstance(runtime, RuntimeContract)
                else RuntimeContract.from_dict(runtime))
    return InstrumentStack(pins=dict(pins), runtime=contract)


__all__ = ["INSTRUMENT_REGISTRY", "INSTRUMENT_STACK_VERSION", "InstrumentStack",
           "InstrumentStackError", "REQUIRED_SLOTS", "current_versions",
           "validate_config"]
