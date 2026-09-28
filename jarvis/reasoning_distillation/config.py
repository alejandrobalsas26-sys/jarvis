"""reasoning_distillation/config.py — V69 M67A: every threshold, in one place.

WHY THIS MODULE EXISTS
----------------------
M67A's job is to decide which historical reasoning traces are trustworthy enough to
build a future experiment on. Every one of those decisions is a threshold, and a
threshold buried in the function that applies it has three properties this milestone
cannot afford:

  * it is invisible in the manifest, so a corpus built last month and one built today
    can differ in what "accepted" means with nothing recording the change;
  * it is tunable by whoever is reading the failures, which is exactly the post-hoc
    weakening the repository's science rules exist to prevent;
  * it cannot be diffed. A reviewer asking "what changed about the quality bar" has to
    read every module instead of one config record.

So every number lives here, in a frozen dataclass, with a version string that is
recorded in the build manifest (§20). Changing a threshold changes the version, and the
version appears in the report — which makes the change a decision somebody made rather
than a detail somebody noticed.

WHAT IS DELIBERATELY *NOT* CONFIGURABLE
---------------------------------------
Three things are structural and are NOT exposed here, because making them adjustable
would make a safety property a preference:

  * **the holdout firewall.** There is no config key that lets development code read
    frozen-holdout content. See :mod:`reasoning_distillation.holdout`.
  * **fail-closed export status.** ``EXPORT_UNKNOWN`` is never promoted to
    ``EXPORT_SAFE`` by a threshold. A privacy decision is explicit or it is blocking.
  * **provenance completeness.** A source unit without a deterministic identity is
    refused, not scored.

THRESHOLD PROVENANCE
--------------------
The similarity thresholds are NOT new numbers. They are the ones
:mod:`training_gym.datasets.similarity` already uses for training-corpus leakage
(``BLOCK_THRESHOLD`` 0.80, ``WARN_THRESHOLD`` 0.60), imported rather than restated so
this milestone cannot drift into a weaker definition of "near-duplicate" than the
dataset pipeline already enforces.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from training_gym.datasets.similarity import BLOCK_THRESHOLD, WARN_THRESHOLD
from training_gym.schemas import SchemaError

#: Bump when any default below changes, or when a field is added or removed. Recorded in
#: every manifest, so two builds that disagree can be told apart without a diff.
CONFIG_VERSION = "m67a.config.1"

#: The pipeline as a whole. Distinct from CONFIG_VERSION: the code can change while the
#: thresholds do not, and a reviewer needs to know which moved.
PIPELINE_VERSION = "m67a.pipeline.1"


class ConfigError(SchemaError):
    """A configuration was refused. Never a silently substituted default."""


@dataclass(frozen=True)
class DedupeConfig:
    """§8 — exact and near-duplicate detection.

    ``near_duplicate_block`` is the threshold at which two items are treated as the SAME
    item for split purposes. ``near_duplicate_warn`` is reported and does not block on
    its own. Both are inherited from the dataset pipeline rather than chosen here.
    """

    near_duplicate_block: float = BLOCK_THRESHOLD
    near_duplicate_warn: float = WARN_THRESHOLD
    #: Hard ceiling on pair comparisons. Reaching it is REPORTED as an incomplete
    #: search, never absorbed as "no duplicates found".
    max_comparisons: int = 2_000_000
    #: A turn shorter than this contributes no similarity signal worth trusting; it is
    #: still hashed for exact duplication, but it is not compared for near-duplication.
    min_chars_for_similarity: int = 40


@dataclass(frozen=True)
class SplitConfig:
    """§9 — deterministic family split.

    The ratios are NOT 80/10/10. §9 forbids blindly using that, and for a corpus of
    historical conversations the binding constraint is the number of problem FAMILIES,
    not the number of examples: a 10% holdout of 40 families is 4 families, which cannot
    support any claim about anything. So the split declares a MINIMUM family count per
    partition and refuses rather than producing an indefensible holdout.
    """

    development_ratio: float = 0.70
    validation_ratio: float = 0.15
    frozen_holdout_ratio: float = 0.15
    #: The split seed. Deterministic: the same families and the same seed always produce
    #: the same partition, and the seed is recorded in the manifest.
    seed: int = 20260927
    #: Below this many families in a partition the split is REFUSED for that partition,
    #: with an explicit shortfall. A holdout of two families is not a small deviation
    #: from a ratio; it is the absence of a defensible measurement.
    min_families_per_partition: int = 8
    #: Below this many families in total, no defensible frozen holdout exists at all and
    #: the split says so rather than carving one anyway.
    min_families_for_holdout: int = 24
    #: Tolerance on the realised ratio before a shortfall is reported. Families are
    #: indivisible, so an exact ratio is usually unreachable.
    ratio_tolerance: float = 0.12


@dataclass(frozen=True)
class RecursionConfig:
    """§12 — bounded recursive distillation."""

    #: Maximum EXTRACT -> CRITIQUE -> REPAIR cycles. §12 says small, explicit and <= 3.
    max_repair_depth: int = 3
    #: Stop early when no finding at or above this severity remains. "Material" is a
    #: severity, not a judgement call made at the call site.
    material_severities: tuple[str, ...] = ("blocking", "material")
    #: A repair pass that produces a byte-identical artifact has converged; running it
    #: again cannot help and the recursion stops rather than burning the budget.
    stop_on_identical_artifact: bool = True


@dataclass(frozen=True)
class QualityConfig:
    """§14 — the multi-dimensional quality gate.

    There is no single scalar. Each dimension has its own floor, and the disposition is
    decided by which floors were missed, not by an average: averaging lets a strong
    score on ``efficiency`` pay for a missing ``verification_quality``, which is exactly
    the trade this milestone must not make.
    """

    #: Dimensions whose floor MUST be met for ACCEPT. A miss here is REJECT or REVIEW,
    #: never absorbed by a high score elsewhere.
    required_dimensions: tuple[str, ...] = (
        "task_understanding", "constraint_retention", "provenance_quality",
        "scope_adherence",
    )
    #: Per-dimension floor, 0..1. A dimension absent from this mapping has no floor and
    #: is recorded for information only.
    floors: dict[str, float] = field(default_factory=lambda: {
        "task_understanding": 0.60,
        "constraint_retention": 0.60,
        "reasoning_relevance": 0.50,
        "premise_handling": 0.40,
        "uncertainty_handling": 0.40,
        "tool_decision_quality": 0.40,
        "alternative_quality": 0.30,
        "verification_quality": 0.40,
        "self_correction_quality": 0.0,
        "factual_reliability": 0.30,
        "scope_adherence": 0.60,
        "answer_alignment": 0.50,
        "efficiency": 0.30,
        "meta_noise": 0.40,
        "provenance_quality": 1.0,
    })
    #: How many NON-required dimensions may sit below their floor and still ACCEPT. Above
    #: this the item routes to review rather than being rejected: a trace that is weak in
    #: several places may still be useful, and that is a human's call.
    max_soft_floor_misses: int = 2


@dataclass(frozen=True)
class SkillConfig:
    """§15 — the reasoning skill library.

    ``min_supporting_examples`` is the whole point. A skill derived from one anecdotal
    trace is a story, not a procedure, and the repository has a standing rule against
    treating a single observation as a pattern.
    """

    min_supporting_examples: int = 3
    #: Supporting examples must come from at least this many DISTINCT problem families,
    #: so three variations of one conversation cannot manufacture a skill.
    min_supporting_families: int = 2
    #: A skill whose support comes entirely from one source file is provisional however
    #: many examples it has: one export is one witness.
    min_supporting_source_files: int = 2


@dataclass(frozen=True)
class PrivacyConfig:
    """§4, §13 — privacy classification and meta-noise.

    ``require_scanner`` is the fail-closed switch and it defaults to True: when the
    underlying scanners cannot be imported, every record is ``EXPORT_UNKNOWN`` and no
    record is exportable. An unavailable scanner proves nothing, so it must not read as
    "clean".
    """

    require_scanner: bool = True
    #: Meta-noise detection is CONSERVATIVE by instruction (§13). A segment is only
    #: marked when this many independent markers fire, so a single "I should" does not
    #: delete a paragraph of real reasoning.
    meta_noise_min_markers: int = 2
    #: Source is never destroyed. This is not configurable to False anywhere in the
    #: pipeline; it is stated here so a reader looking for the switch finds the answer.
    preserve_source: bool = True


@dataclass(frozen=True)
class DistillationConfig:
    """The whole configuration, as one recordable object."""

    config_version: str = CONFIG_VERSION
    pipeline_version: str = PIPELINE_VERSION
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    split: SplitConfig = field(default_factory=SplitConfig)
    recursion: RecursionConfig = field(default_factory=RecursionConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    skills: SkillConfig = field(default_factory=SkillConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    def validated(self) -> "DistillationConfig":
        """Refuse a configuration that cannot mean anything. Raises, never repairs.

        A config that silently clamps an out-of-range ratio is worse than one that
        fails: the manifest would record the value that was asked for while the build
        used a different one.
        """
        s = self.split
        total = s.development_ratio + s.validation_ratio + s.frozen_holdout_ratio
        if abs(total - 1.0) > 1e-9:
            raise ConfigError(
                f"split ratios must sum to 1.0, got {total:.6f} "
                f"({s.development_ratio}/{s.validation_ratio}/{s.frozen_holdout_ratio})")
        for name, value in (("development_ratio", s.development_ratio),
                            ("validation_ratio", s.validation_ratio),
                            ("frozen_holdout_ratio", s.frozen_holdout_ratio)):
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"split.{name} must be in [0, 1], got {value}")
        if s.min_families_per_partition < 1:
            raise ConfigError("split.min_families_per_partition must be at least 1")
        d = self.dedupe
        if not 0.0 < d.near_duplicate_block <= 1.0:
            raise ConfigError(
                f"dedupe.near_duplicate_block must be in (0, 1], got {d.near_duplicate_block}")
        if d.near_duplicate_warn > d.near_duplicate_block:
            raise ConfigError(
                "dedupe.near_duplicate_warn must not exceed near_duplicate_block: a "
                "warning threshold above the blocking one reports nothing the block "
                "did not already stop")
        if d.max_comparisons < 1:
            raise ConfigError("dedupe.max_comparisons must be positive")
        r = self.recursion
        if not 0 <= r.max_repair_depth <= 3:
            raise ConfigError(
                f"recursion.max_repair_depth must be in [0, 3] (§12 caps it at 3), "
                f"got {r.max_repair_depth}")
        q = self.quality
        for dim, floor in sorted(q.floors.items()):
            if not 0.0 <= floor <= 1.0:
                raise ConfigError(f"quality.floors[{dim!r}] must be in [0, 1], got {floor}")
        missing = [dim for dim in q.required_dimensions if dim not in q.floors]
        if missing:
            raise ConfigError(
                f"quality.required_dimensions names dimension(s) with no floor: "
                f"{sorted(missing)}. A required dimension with no floor is not required")
        if q.max_soft_floor_misses < 0:
            raise ConfigError("quality.max_soft_floor_misses must not be negative")
        k = self.skills
        if k.min_supporting_examples < 2:
            raise ConfigError(
                "skills.min_supporting_examples must be at least 2: §15 forbids a skill "
                "derived from one anecdotal trace, and 1 is that trace")
        if k.min_supporting_families < 1:
            raise ConfigError("skills.min_supporting_families must be at least 1")
        p = self.privacy
        if not p.preserve_source:
            raise ConfigError(
                "privacy.preserve_source cannot be disabled: §13 requires that source is "
                "never destroyed, so there is no configuration that deletes it")
        return self

    def with_seed(self, seed: int) -> "DistillationConfig":
        """A copy with a different split seed, for the ``--seed`` CLI option."""
        return replace(self, split=replace(self.split, seed=int(seed)))


def default_config() -> DistillationConfig:
    """The validated default. Every entrypoint starts here."""
    return DistillationConfig().validated()


def load_config(path: str | Path) -> DistillationConfig:
    """Load and validate a configuration override file.

    The file need only name the keys it changes; everything else keeps its default. An
    UNKNOWN key is refused rather than ignored, because a typo in a threshold name is
    indistinguishable from a threshold that had no effect.
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"{path}: expected a JSON object, got {type(payload).__name__}")

    sections = {
        "dedupe": DedupeConfig, "split": SplitConfig, "recursion": RecursionConfig,
        "quality": QualityConfig, "skills": SkillConfig, "privacy": PrivacyConfig,
    }
    base = DistillationConfig()
    kwargs: dict = {}
    for key, value in sorted(payload.items()):
        if key in ("config_version", "pipeline_version"):
            raise ConfigError(
                f"{path}: {key!r} is derived from the code, not from a config file. A "
                f"build that could declare its own version could hide a threshold change")
        if key not in sections:
            raise ConfigError(f"{path}: unknown configuration section {key!r}; "
                              f"known sections are {sorted(sections)}")
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: section {key!r} must be an object")
        cls = sections[key]
        current = getattr(base, key)
        known = {f for f in current.__dataclass_fields__}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) in {key!r}: {unknown}; "
                              f"known keys are {sorted(known)}")
        merged = {**asdict(current), **value}
        # tuple fields arrive from JSON as lists; restore the declared type so the
        # dataclass stays hashable and comparable across builds.
        for fname, fdef in current.__dataclass_fields__.items():
            if isinstance(getattr(current, fname), tuple) and isinstance(merged.get(fname), list):
                merged[fname] = tuple(merged[fname])
            del fdef
        kwargs[key] = cls(**merged)
    return replace(base, **kwargs).validated()


__all__ = [
    "CONFIG_VERSION", "PIPELINE_VERSION", "ConfigError", "DedupeConfig", "SplitConfig",
    "RecursionConfig", "QualityConfig", "SkillConfig", "PrivacyConfig",
    "DistillationConfig", "default_config", "load_config",
]
