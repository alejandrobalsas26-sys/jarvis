"""reasoning_distillation/splitting.py — V69 M67A: split by family, before distillation.

THE ONE RULE THIS MODULE EXISTS FOR
-----------------------------------
§9: **split by conversation / problem family BEFORE model-assisted distillation, and never
split individual reasoning fragments independently.**

Both halves are anti-leakage, and they fail in different ways:

  * splitting FRAGMENTS lets a conversation's reasoning turn land in development while its
    answer turn lands in the holdout. Everything then looks fine — the counts are right, no
    item appears twice — and the held-out measurement is scoring a model on material it was
    shaped by. So the unit of assignment here is the FAMILY from
    :mod:`reasoning_distillation.dedupe`, never a turn and never a conversation.
  * splitting AFTER distillation means the extraction prompt, the taxonomy and the critic
     thresholds were all tuned while holdout content was visible. The holdout is then spent
     before it was ever used, and no later discipline recovers it. So the split runs on
     canonical conversations, and :mod:`reasoning_distillation.holdout` is what stops the
     distiller from ever seeing the frozen partition.

WHY THE RATIOS ARE NOT 80/10/10
-------------------------------
§9 says do not blindly use 80/10/10, and for this corpus the reason is arithmetic rather
than taste. The meaningful denominator is FAMILIES, not examples: 400 distilled records
drawn from 30 problem families give a 10% holdout of **three families**. Three families
cannot distinguish a real improvement from which three problems happened to be held out.

So this module does two things a ratio-only splitter does not:

  * it declares :attr:`~reasoning_distillation.config.SplitConfig.min_families_per_partition`
    and :attr:`~reasoning_distillation.config.SplitConfig.min_families_for_holdout`, and
  * when the corpus cannot meet them it **says so** — :attr:`SplitPlan.defensible` is False
    and :attr:`SplitPlan.shortfalls` names each failure. §9 asks for exactly this: *"if too
    little data exists for a defensible holdout: say so."*

An undersized corpus therefore produces a plan with an EMPTY frozen holdout and an explicit
statement, not a holdout of two families that reads like a measurement.

WHY THE ASSIGNMENT IS A HASH AND NOT A SHUFFLE
----------------------------------------------
Each family is placed by a digest of ``(family_id, seed)``. Three properties follow, and all
three are requirements rather than conveniences:

  * **deterministic** — the same families and seed always produce the same partition, on any
    interpreter, with no dependence on ``PYTHONHASHSEED``, dict order or ``random``'s
    internal version. ``random.shuffle`` fails the last of those: its algorithm is not a
    compatibility guarantee across Python versions.
  * **stable under growth** — importing a new family does not move the families already
    placed. A shuffle re-permutes everything, which would silently move material OUT of a
    frozen holdout on the next import. That is the single most dangerous thing a splitter
    could do, because it un-freezes the holdout while every report still says FROZEN.
  * **auditable** — the placement of any family can be recomputed from its id and the seed
    alone, with no stored state.

ONCE FROZEN, IMMUTABLE
----------------------
§9 says holdout ids and hashes are immutable once frozen. :func:`freeze` produces a
:class:`FrozenHoldout` whose ``holdout_digest`` binds the member set, and
:func:`verify_frozen` refuses any later plan that would change it. A plan that moves a family
out of the holdout is not a rebalance; it is a spend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from training_gym.schemas import SchemaError, sha256_obj, sha256_text

from .config import SplitConfig
from .dedupe import DedupeReport

#: Bump when the placement ALGORITHM changes. A change here re-partitions every corpus, so it
#: is recorded in the manifest and is a deliberate act — see :func:`verify_frozen`.
SPLIT_ALGORITHM_VERSION = "m67a.split.1"


class SplitError(SchemaError):
    """A split was refused. Never a partial assignment, never a silent rebalance."""


class HoldoutViolation(SplitError):
    """A frozen holdout would have changed. The strongest refusal in this module."""


class Partition(str, Enum):
    """The three partitions. Deliberately named, not numbered.

    ``FROZEN_HOLDOUT`` carries the word FROZEN in the member value so that it appears in every
    log line, report and error message that mentions it. A partition called ``test`` invites
    being used for a quick check.
    """

    DEVELOPMENT = "development"
    VALIDATION = "validation"
    FROZEN_HOLDOUT = "frozen_holdout"


#: Partitions a development command may read. ``FROZEN_HOLDOUT`` is absent, and that absence
#: is enforced by :mod:`reasoning_distillation.holdout` rather than only documented here.
DEVELOPMENT_PARTITIONS: frozenset[Partition] = frozenset({
    Partition.DEVELOPMENT, Partition.VALIDATION})


def placement_score(family_id: str, *, seed: int) -> float:
    """A stable point in ``[0, 1)`` for one family under one seed.

    Derived from a sha256 of the family id, the seed and the algorithm version. The version is
    inside the hash on purpose: if the placement algorithm ever changes, every score changes,
    so a frozen holdout built under the old algorithm cannot be silently "reproduced" under
    the new one — :func:`verify_frozen` will see the mismatch.
    """
    digest = sha256_text(f"{SPLIT_ALGORITHM_VERSION}\x1f{seed}\x1f{family_id}")
    # 52 bits: comfortably inside float's exact-integer range, so the division is exact and
    # the same on every platform.
    return int(digest[:13], 16) / float(1 << 52)


@dataclass(frozen=True)
class SplitPlan:
    """Which family goes where, and an honest account of how well it matched.

    Modelled on :class:`training_gym.datasets.split.SplitPlan`, including its insistence that
    shortfalls are REPORTED rather than hidden. The addition here is :attr:`defensible`, which
    is the §9 answer to "too little data": a plan can be internally consistent and still not
    support a measurement, and those are different facts.
    """

    assignments: dict[str, str] = field(default_factory=dict)
    seed: int = 0
    algorithm_version: str = SPLIT_ALGORITHM_VERSION
    requested_ratios: dict[str, float] = field(default_factory=dict)
    shortfalls: tuple[str, ...] = ()
    defensible: bool = False
    holdout_available: bool = False

    def families_in(self, partition: Partition) -> tuple[str, ...]:
        return tuple(sorted(fid for fid, name in self.assignments.items()
                            if name == partition.value))

    def counts(self) -> dict[str, int]:
        return {p.value: len(self.families_in(p)) for p in Partition}

    def realised_ratios(self) -> dict[str, float]:
        total = len(self.assignments)
        if not total:
            return {p.value: 0.0 for p in Partition}
        return {name: round(count / total, 6) for name, count in self.counts().items()}

    def partition_of(self, family_id: str) -> Partition:
        """The partition a family belongs to. Raises rather than defaulting.

        An unplaced family returning ``DEVELOPMENT`` by default would put unknown material on
        the readable side of the firewall, which is the wrong direction to fail.
        """
        name = self.assignments.get(family_id)
        if not name:
            raise SplitError(
                f"family {family_id[:12]} has no partition; an unplaced family must not be "
                f"read as development material (§17: uncertainty fails closed)")
        return Partition(name)

    def to_dict(self) -> dict:
        return {
            "algorithm_version": self.algorithm_version,
            "seed": self.seed,
            "requested_ratios": dict(sorted(self.requested_ratios.items())),
            "realised_ratios": self.realised_ratios(),
            "counts": self.counts(),
            "shortfalls": list(self.shortfalls),
            "defensible": self.defensible,
            "holdout_available": self.holdout_available,
            "assignments": dict(sorted(self.assignments.items())),
        }


@dataclass(frozen=True)
class FrozenHoldout:
    """An immutable record of a frozen partition (§9).

    ``holdout_digest`` binds the member family ids, the seed and the algorithm version. Any
    later plan whose holdout differs — a family added, removed, or the same families under a
    different seed — produces a different digest and is refused by :func:`verify_frozen`.

    This object holds NO conversation content and no conversation digests, only family ids.
    That is deliberate: the frozen record is the thing most likely to be copied into a report,
    and a family id cannot be used to reconstruct anything.
    """

    family_ids: tuple[str, ...]
    seed: int
    algorithm_version: str = SPLIT_ALGORITHM_VERSION
    frozen_at_generation: str = ""

    @property
    def holdout_digest(self) -> str:
        return sha256_obj({
            "family_ids": sorted(self.family_ids),
            "seed": self.seed,
            "algorithm_version": self.algorithm_version,
        })

    @property
    def family_count(self) -> int:
        return len(self.family_ids)

    def contains(self, family_id: str) -> bool:
        return family_id in set(self.family_ids)

    def to_dict(self) -> dict:
        return {
            "holdout_digest": self.holdout_digest,
            "family_count": self.family_count,
            "family_ids": sorted(self.family_ids),
            "seed": self.seed,
            "algorithm_version": self.algorithm_version,
            "frozen_at_generation": self.frozen_at_generation,
            "status": "FROZEN_IMMUTABLE",
        }


def plan(report: DedupeReport, *, config: SplitConfig | None = None) -> SplitPlan:
    """Assign every family to a partition, deterministically (§9).

    The assignment is a two-step so that the ratios are honoured as closely as indivisible
    families allow AND the result stays stable under growth:

      1. every family gets its :func:`placement_score`;
      2. families are placed by sorting on that score and cutting at the requested ratio
         boundaries.

    Step 2 is what makes small corpora behave: a pure per-family threshold test ("score < 0.7
    means development") gives wildly wrong ratios at n=20, because 20 samples of a uniform
    distribution do not land 14/3/3. Sorting and cutting gives the closest achievable split at
    any n, and because the SORT KEY is the stable score, adding a family shifts at most the
    families adjacent to a cut rather than re-permuting everything.

    A family with no grouping basis (:attr:`~reasoning_distillation.dedupe.DuplicateFamily.
    grouping_basis_absent`) is placed in DEVELOPMENT regardless of its score. It cannot be
    compared to anything, so it cannot be shown NOT to duplicate a holdout family, and an
    uncomparable item belongs on the side where a false negative costs nothing.
    """
    cfg = config or SplitConfig()
    requested = {
        Partition.DEVELOPMENT.value: cfg.development_ratio,
        Partition.VALIDATION.value: cfg.validation_ratio,
        Partition.FROZEN_HOLDOUT.value: cfg.frozen_holdout_ratio,
    }
    if not report.families:
        return SplitPlan(assignments={}, seed=cfg.seed, requested_ratios=requested,
                         shortfalls=("the corpus is empty: no family to place, and no "
                                     "defensible holdout exists",),
                         defensible=False, holdout_available=False)

    forced_development = sorted(f.family_id for f in report.families
                                if f.grouping_basis_absent)
    placeable = sorted(f.family_id for f in report.families if not f.grouping_basis_absent)

    assignments: dict[str, str] = {fid: Partition.DEVELOPMENT.value
                                   for fid in forced_development}
    shortfalls: list[str] = []
    if forced_development:
        shortfalls.append(
            f"{len(forced_development)} famil(y/ies) have no problem statement to group on "
            f"and were placed in DEVELOPMENT unconditionally: an uncomparable family cannot "
            f"be shown not to duplicate a holdout family")

    total_families = len(report.families)
    holdout_possible = total_families >= cfg.min_families_for_holdout
    if not holdout_possible:
        shortfalls.append(
            f"{total_families} problem famil(y/ies) is below the "
            f"{cfg.min_families_for_holdout}-family floor for a defensible frozen holdout; "
            f"NO holdout was carved. A holdout this small measures which problems were held "
            f"out, not whether anything improved (§9)")

    ordered = sorted(placeable, key=lambda fid: (placement_score(fid, seed=cfg.seed), fid))
    n = len(ordered)
    if holdout_possible:
        dev_cut = round(n * cfg.development_ratio)
        val_cut = dev_cut + round(n * cfg.validation_ratio)
        buckets = ((Partition.DEVELOPMENT, ordered[:dev_cut]),
                   (Partition.VALIDATION, ordered[dev_cut:val_cut]),
                   (Partition.FROZEN_HOLDOUT, ordered[val_cut:]))
    else:
        # No holdout. The validation split is still carved — it is a development-side
        # partition and costs nothing — so that thresholds can be tuned somewhere other than
        # the material they are tuned against.
        dev_share = cfg.development_ratio + cfg.frozen_holdout_ratio
        total_share = dev_share + cfg.validation_ratio
        dev_cut = round(n * (dev_share / total_share)) if total_share else n
        buckets = ((Partition.DEVELOPMENT, ordered[:dev_cut]),
                   (Partition.VALIDATION, ordered[dev_cut:]),
                   (Partition.FROZEN_HOLDOUT, []))

    for partition, members in buckets:
        for fid in members:
            assignments[fid] = partition.value

    provisional = SplitPlan(assignments=assignments, seed=cfg.seed,
                            requested_ratios=requested)
    counts = provisional.counts()
    realised = provisional.realised_ratios()

    for partition in Partition:
        want = requested[partition.value]
        if want <= 0.0:
            continue
        if partition is Partition.FROZEN_HOLDOUT and not holdout_possible:
            continue
        have = counts[partition.value]
        if have == 0:
            shortfalls.append(
                f"{partition.value}: requested {want:.2f} of the corpus but received no "
                f"families; families are indivisible and cannot be split to fill a ratio")
        elif have < cfg.min_families_per_partition:
            shortfalls.append(
                f"{partition.value}: {have} famil(y/ies), below the "
                f"{cfg.min_families_per_partition}-family floor")
        elif abs(realised[partition.value] - want) > cfg.ratio_tolerance:
            shortfalls.append(
                f"{partition.value}: requested {want:.2f}, realised "
                f"{realised[partition.value]:.2f}; families are indivisible")

    holdout_count = counts[Partition.FROZEN_HOLDOUT.value]
    holdout_available = (holdout_possible
                         and holdout_count >= cfg.min_families_per_partition)
    defensible = holdout_available and not any(
        s.startswith(("development:", "validation:", "frozen_holdout:")) for s in shortfalls)

    return SplitPlan(assignments=assignments, seed=cfg.seed, requested_ratios=requested,
                     shortfalls=tuple(shortfalls), defensible=defensible,
                     holdout_available=holdout_available)


def plan_respecting_frozen(report: DedupeReport, frozen: FrozenHoldout, *,
                           config: SplitConfig | None = None) -> SplitPlan:
    """Re-plan a GROWN corpus without touching an already-frozen holdout (§9).

    This is the legitimate growth path, and it exists because the strict one is not usable on
    its own. :func:`plan` re-derives every cut from the corpus size, so importing new material
    and re-planning moves families across the holdout boundary in both directions —
    :func:`verify_frozen` then refuses, correctly, and the corpus can never grow again.

    The resolution is not to relax the check. It is to make growth mean something narrower:

      * every family in *frozen* stays in ``FROZEN_HOLDOUT``, pinned, whatever its score;
      * every family NOT in *frozen* is placed among DEVELOPMENT and VALIDATION only;
      * **no new family ever enters the holdout.**

    That last line is the whole point, and it is a real cost, stated rather than hidden: the
    holdout does not grow with the corpus, so its share shrinks as material is added. A
    shrinking holdout share is recorded as a shortfall. The alternative — topping the holdout
    up with new families — would mean the frozen set is a moving target, and every measurement
    taken against an earlier version of it becomes incomparable.

    A corpus that has outgrown its holdout needs a NEW holdout, authored by a session that
    will not run it, which is this repository's standing rule for held-out material. That is a
    decision for an operator, not a rebalance this function may perform.
    """
    cfg = config or SplitConfig()
    if split_seed_mismatch := (cfg.seed != frozen.seed):
        del split_seed_mismatch
        raise HoldoutViolation(
            f"cannot re-plan against a holdout frozen with seed {frozen.seed} using seed "
            f"{cfg.seed}: the pinned families would keep their place while every other "
            f"family moved, which is neither the old partition nor a new one")
    requested = {
        Partition.DEVELOPMENT.value: cfg.development_ratio,
        Partition.VALIDATION.value: cfg.validation_ratio,
        Partition.FROZEN_HOLDOUT.value: cfg.frozen_holdout_ratio,
    }
    known = {f.family_id for f in report.families}
    missing = sorted(set(frozen.family_ids) - known)
    if missing:
        raise HoldoutViolation(
            f"{len(missing)} frozen holdout famil(y/ies) are ABSENT from this corpus "
            f"({[f[:12] for f in missing[:3]]}). A frozen family that can no longer be found "
            f"has been deleted or re-grouped; that is a rewrite of frozen state, not a growth")

    assignments: dict[str, str] = {fid: Partition.FROZEN_HOLDOUT.value
                                   for fid in frozen.family_ids}
    shortfalls: list[str] = []

    forced = sorted(f.family_id for f in report.families
                    if f.grouping_basis_absent and f.family_id not in assignments)
    for fid in forced:
        assignments[fid] = Partition.DEVELOPMENT.value
    if forced:
        shortfalls.append(
            f"{len(forced)} famil(y/ies) have no problem statement to group on and were "
            f"placed in DEVELOPMENT unconditionally")

    remaining = sorted(fid for fid in known if fid not in assignments)
    ordered = sorted(remaining, key=lambda fid: (placement_score(fid, seed=cfg.seed), fid))
    dev_share = cfg.development_ratio
    total_share = cfg.development_ratio + cfg.validation_ratio
    dev_cut = round(len(ordered) * (dev_share / total_share)) if total_share else len(ordered)
    for fid in ordered[:dev_cut]:
        assignments[fid] = Partition.DEVELOPMENT.value
    for fid in ordered[dev_cut:]:
        assignments[fid] = Partition.VALIDATION.value

    provisional = SplitPlan(assignments=assignments, seed=cfg.seed, requested_ratios=requested)
    counts = provisional.counts()
    realised = provisional.realised_ratios()
    holdout_share = realised[Partition.FROZEN_HOLDOUT.value]
    # NOT `cfg.ratio_tolerance`. That tolerance exists to absorb INDIVISIBILITY — families
    # cannot be cut, so a requested 0.15 may realise as 0.14 or 0.17 and neither is a finding.
    # Dilution of a PINNED holdout is a different phenomenon: it is monotone, it grows with
    # every import, and it is exactly what an operator needs told. Measured at 90 families
    # against a 9-family frozen set, the 0.12 indivisibility tolerance silently absorbed a
    # drift from 0.15 to 0.10 — a third of the holdout's share, reported as no shortfall at
    # all. The margin here is therefore one family's worth of share, floored at 2 points, so
    # jitter stays quiet and real dilution does not.
    dilution = cfg.frozen_holdout_ratio - holdout_share
    total_families = max(1, len(assignments))
    if dilution > max(1.0 / total_families, 0.02):
        shortfalls.append(
            f"frozen_holdout: pinned at {counts[Partition.FROZEN_HOLDOUT.value]} famil(y/ies) "
            f"= {holdout_share:.2f} of a corpus that has grown, {dilution:.2f} below the "
            f"requested {cfg.frozen_holdout_ratio:.2f}. The holdout was NOT topped up, because a frozen "
            f"set that grows is not frozen. A larger corpus needs a NEW holdout, authored by a "
            f"session that will not run it")
    for partition in (Partition.DEVELOPMENT, Partition.VALIDATION):
        have = counts[partition.value]
        if have < cfg.min_families_per_partition:
            shortfalls.append(f"{partition.value}: {have} famil(y/ies), below the "
                              f"{cfg.min_families_per_partition}-family floor")

    holdout_available = counts[Partition.FROZEN_HOLDOUT.value] >= cfg.min_families_per_partition
    defensible = holdout_available and not any(
        s.startswith(("development:", "validation:")) for s in shortfalls)
    return SplitPlan(assignments=assignments, seed=cfg.seed, requested_ratios=requested,
                     shortfalls=tuple(shortfalls), defensible=defensible,
                     holdout_available=holdout_available)


def freeze(split: SplitPlan, *, generation: str = "") -> FrozenHoldout:
    """Freeze the holdout partition of *split* (§9).

    Refuses to freeze an empty or undersized holdout. A ``FrozenHoldout`` with no members
    would satisfy every later immutability check trivially while recording, in the manifest,
    that a holdout was frozen — which is a false statement about the corpus's readiness.
    """
    members = split.families_in(Partition.FROZEN_HOLDOUT)
    if not members:
        raise SplitError(
            "refusing to freeze an EMPTY holdout. The plan reports "
            f"holdout_available={split.holdout_available} and shortfalls "
            f"{list(split.shortfalls[:2])}; freezing nothing would record that a holdout "
            f"exists when none does")
    if not split.holdout_available:
        raise SplitError(
            f"refusing to freeze an undersized holdout of {len(members)} famil(y/ies): the "
            f"plan itself reports it is not available. Raise the corpus size, not the "
            f"threshold")
    return FrozenHoldout(family_ids=members, seed=split.seed,
                         frozen_at_generation=generation)


def verify_frozen(frozen: FrozenHoldout, split: SplitPlan) -> None:
    """Refuse a plan that would change an already-frozen holdout (§9, §17).

    Checks membership in BOTH directions and the seed, because the three ways a holdout can be
    violated are different acts with different motives:

      * a family LEFT the holdout — the most dangerous: it becomes trainable material while
        every earlier report still counts it as held out;
      * a family ENTERED the holdout — less dangerous but still a change, and a holdout that
        grows after freezing was not frozen;
      * the SEED moved — every placement changes, so the holdout is a different holdout that
        happens to overlap.
    """
    current = set(split.families_in(Partition.FROZEN_HOLDOUT))
    original = set(frozen.family_ids)
    if split.seed != frozen.seed:
        raise HoldoutViolation(
            f"the frozen holdout was built with seed {frozen.seed} and this plan uses "
            f"{split.seed}. A different seed is a different partition, not a reproduction")
    if split.algorithm_version != frozen.algorithm_version:
        raise HoldoutViolation(
            f"the frozen holdout was built by {frozen.algorithm_version} and this plan by "
            f"{split.algorithm_version}. A placement-algorithm change re-partitions every "
            f"corpus and cannot reproduce a holdout frozen under the old one")
    escaped = sorted(original - current)
    if escaped:
        raise HoldoutViolation(
            f"{len(escaped)} famil(y/ies) LEFT the frozen holdout ({[f[:12] for f in escaped[:3]]}). "
            f"They would become development material while every earlier report counts them "
            f"as held out. A holdout family never moves out (§9)")
    entered = sorted(current - original)
    if entered:
        raise HoldoutViolation(
            f"{len(entered)} famil(y/ies) ENTERED the frozen holdout "
            f"({[f[:12] for f in entered[:3]]}). A holdout that grows after freezing was not "
            f"frozen; new material goes to development or to a NEW holdout")


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {
        "split_algorithm_version": SPLIT_ALGORITHM_VERSION,
        "unit_of_assignment": "problem family (never a conversation, never a fragment)",
        "placement": "sha256(algorithm_version, seed, family_id) -> sort -> cut at ratios",
        "stability": "adding a family moves at most the families adjacent to a cut",
        "partitions": [p.value for p in Partition],
    }


__all__ = [
    "DEVELOPMENT_PARTITIONS", "SPLIT_ALGORITHM_VERSION", "FrozenHoldout", "HoldoutViolation",
    "Partition", "SplitError", "SplitPlan", "freeze", "placement_score", "plan",
    "plan_respecting_frozen", "verify_frozen", "versions",
]
