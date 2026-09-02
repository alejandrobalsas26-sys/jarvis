"""training_gym/evaluation/instruments/calibration.py — V69 M62 S4H.

WHY A HARNESS AND NOT A NUMBER
------------------------------
S4H builds instruments. It does not get to claim they are calibrated. The distinction
matters because the two words are routinely fused:

    FUNCTIONAL   the instrument does what its specification says on inputs someone wrote
                 for it. Provable here, and proved.
    CALIBRATED   the instrument's error rates on the REAL distribution of model outputs
                 are known. Not provable here, and not claimed.

Every case this harness scores was written by the same milestone that wrote the detector
it scores. That correlation is not a small caveat: a synthetic suite measures whether the
author's model of the problem is self-consistent, which is worth having and is not
evidence about a model nobody has run. So the report carries
:attr:`CalibrationReport.calibration_class` = ``SYNTHETIC_CALIBRATION`` in its serialized
form, and :attr:`CalibrationReport.real_world_calibrated` is a constant ``False`` that
this module offers no way to set.

The gate policy is untouched. This produces numbers for a reviewer, never a threshold.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from ...schemas import SchemaError, canonical_json

INSTRUMENT_CALIBRATION_VERSION = "m62.instrument_calibration.1"

#: The ONLY class of calibration this milestone can produce. Stated as a constant so a
#: future milestone that earns a stronger claim has to add a member rather than edit a
#: string, and so a grep for the weaker claim finds every place it is made.
SYNTHETIC_CALIBRATION = "SYNTHETIC_CALIBRATION"


class CalibrationError(SchemaError):
    """A calibration run that could not produce an honest number."""


class Label(str, Enum):
    """What a case is KNOWN to be, declared by whoever wrote it."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class CalibrationCase:
    """One labelled input. The payload is synthetic and the label is the author's."""

    case_id: str
    label: Label
    payload: object
    #: Free-form only in the sense that the AUTHOR wrote it; never response text.
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise CalibrationError("calibration: case_id must identify the case")
        if not isinstance(self.label, Label):
            raise CalibrationError("calibration: label must be a Label")


@dataclass(frozen=True)
class CalibrationReport:
    """Counts and rates, with the honest name of what they are."""

    instrument: str
    instrument_version: str
    known_positives: int
    known_negatives: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    #: Not settable. The one field this module refuses to let a caller decide.
    calibration_class: str = SYNTHETIC_CALIBRATION

    def __post_init__(self) -> None:
        if self.calibration_class != SYNTHETIC_CALIBRATION:
            raise CalibrationError(
                "calibration: this harness can only produce SYNTHETIC_CALIBRATION; a "
                "stronger claim needs evidence this milestone does not have")
        if self.true_positives + self.false_negatives != self.known_positives:
            raise CalibrationError(
                "calibration: TP + FN != known positives; every positive case must land "
                "in exactly one outcome")
        if self.true_negatives + self.false_positives != self.known_negatives:
            raise CalibrationError(
                "calibration: TN + FP != known negatives")

    @property
    def real_world_calibrated(self) -> bool:
        """Always false. There is no argument that sets it, by design."""
        return False

    @property
    def precision(self) -> "float | None":
        denominator = self.true_positives + self.false_positives
        return round(self.true_positives / denominator, 6) if denominator else None

    @property
    def recall(self) -> "float | None":
        denominator = self.true_positives + self.false_negatives
        return round(self.true_positives / denominator, 6) if denominator else None

    @property
    def specificity(self) -> "float | None":
        denominator = self.true_negatives + self.false_positives
        return round(self.true_negatives / denominator, 6) if denominator else None

    @property
    def false_positive_rate(self) -> "float | None":
        denominator = self.false_positives + self.true_negatives
        return round(self.false_positives / denominator, 6) if denominator else None

    @property
    def false_negative_rate(self) -> "float | None":
        denominator = self.false_negatives + self.true_positives
        return round(self.false_negatives / denominator, 6) if denominator else None

    def to_dict(self) -> dict:
        return {"calibration_class": self.calibration_class,
                "false_negative_rate": self.false_negative_rate,
                "false_negatives": self.false_negatives,
                "false_positive_rate": self.false_positive_rate,
                "false_positives": self.false_positives,
                "instrument": self.instrument,
                "instrument_version": self.instrument_version,
                "known_negatives": self.known_negatives,
                "known_positives": self.known_positives,
                "precision": self.precision,
                "real_world_calibrated": self.real_world_calibrated,
                "recall": self.recall,
                "specificity": self.specificity,
                "true_negatives": self.true_negatives,
                "true_positives": self.true_positives}

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")

    def __repr__(self) -> str:
        return (f"CalibrationReport({self.instrument}@{self.instrument_version}, "
                f"TP={self.true_positives} FP={self.false_positives} "
                f"TN={self.true_negatives} FN={self.false_negatives}, "
                f"class={self.calibration_class})")


def run_calibration(cases: Sequence[CalibrationCase], *, instrument: str,
                    instrument_version: str,
                    predicate: Callable) -> CalibrationReport:
    """Score *predicate* over labelled cases. ``predicate(payload) -> bool`` = "positive"."""
    if not cases:
        raise CalibrationError(
            "calibration: an empty case set produces rates with no denominator, and a "
            "report of 'no errors observed' over zero cases is not a measurement")
    seen: set = set()
    tp = fp = tn = fn = 0
    for case in cases:
        if case.case_id in seen:
            raise CalibrationError(
                f"calibration: case_id {case.case_id!r} appears twice; a duplicated case "
                f"weights one input twice and moves every rate")
        seen.add(case.case_id)
        predicted = bool(predicate(case.payload))
        if case.label is Label.POSITIVE:
            if predicted:
                tp += 1
            else:
                fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    return CalibrationReport(
        instrument=instrument, instrument_version=instrument_version,
        known_positives=tp + fn, known_negatives=tn + fp,
        true_positives=tp, false_positives=fp,
        true_negatives=tn, false_negatives=fn)


__all__ = ["CalibrationCase", "CalibrationError", "CalibrationReport",
           "INSTRUMENT_CALIBRATION_VERSION", "Label", "SYNTHETIC_CALIBRATION",
           "run_calibration"]
