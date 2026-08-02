"""training_gym/datasets/split.py — V69 M62 S2d: deterministic, sticky splitting.

WHY THIS EXISTS
---------------
A split is the only thing standing between "the model scored 0.9 on held-out data" and
"the model memorised the held-out data". Three properties make it real, and all three are
easy to lose by accident:

**Deterministic.** Same candidates, same policy, same seed produce byte-identical
manifests, regardless of the order the candidates arrived in. Assignment is a pure
function of ``sha256(policy_version : seed : group_key)`` — no ``random`` module, no
global random state, no clock. A missing seed is an error, never a seed derived from the
time: a split nobody can reproduce cannot be audited, and a dataset nobody can audit
cannot be trusted.

**Group-first.** The unit that moves is the LEAKAGE GROUP, not the record. Fifty variants
generated from one template, two attempts at the same task, and every example built on
the same fixture are one group and land in one split. Assigning records individually and
then noticing the collision afterwards is how contamination gets discovered at evaluation
time instead of at build time.

**Sticky.** This is the control the other two invite. A caller who does not like where a
record landed can re-run with ``seed=2``, then ``seed=3``, until the record they want in
TRAIN is in TRAIN — and every individual run is perfectly deterministic and perfectly
reproducible. :class:`SplitAssignmentLedger` is an append-only record of
``group_key -> split``, consulted BEFORE the hash, so a group that has ever been held out
stays held out. Grinding the seed then produces a refusal instead of a better answer.

RATIOS ARE A REQUEST, NOT A GUARANTEE
-------------------------------------
Groups have different sizes, and a group cannot be divided. Asking for 80/10/10 from four
groups of wildly unequal size cannot be satisfied, and :class:`SplitPlan` reports the
realised proportions alongside the requested ones rather than breaking a group to hit a
number. :meth:`SplitPlan.shortfalls` names every split that came out empty or badly off
target, because an empty HIDDEN_EVALUATION split means no held-out measurement exists at
all — and that is the failure most likely to be read as success.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..atomicio import AtomicIOError, append_jsonl, read_jsonl
from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    require_id,
    sha256_obj,
    sha256_text,
    short,
)
from .candidate import CandidateState, DatasetCandidate, DatasetSplit

#: Bump when an assignment rule below changes. A change here changes every manifest.
SPLIT_ALGORITHM_VERSION = "m62.split.1"

#: Ceiling on the append-only assignment ledger.
MAX_ASSIGNMENT_LINES = 200_000

#: Splits whose contents are measured against, never optimised on. Once a group has been
#: assigned to one of these it may never move to a trainable split.
_HELD_OUT: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
    DatasetSplit.ADVERSARIAL})

#: Splits a model is fitted or steered on.
_TRAIN_SIDE: frozenset[DatasetSplit] = frozenset({
    DatasetSplit.TRAIN, DatasetSplit.VALIDATION})


class SplitError(SchemaError):
    """A split was refused. Never a partial assignment, never a silent rebalance."""


class StickyConflict(SplitError):
    """A group was asked to move out of a split it has already been held out in."""


# ── grouping ──────────────────────────────────────────────────────────────────
def leakage_group_key(candidate: DatasetCandidate) -> str:
    """The unit that moves as a whole. Everything that could leak shares one key.

    Built from, in order of authority:

      * the explicit ``lineage_group`` the candidate declares;
      * the parent task hash, when the candidate is a generated variant — so a variant
        and its parent never separate;
      * the sorted set of input fixture digests — so two unrelated-looking tasks built
        on the same alert log stay together, which is the leakage path a task-id-based
        grouping misses entirely.

    The task hash is deliberately NOT included on its own: two attempts at one task must
    group, and they do so through ``lineage_group``, but two DIFFERENT tasks sharing a
    fixture must also group, and a task-hash key would separate them.
    """
    return sha256_obj({
        "lineage_group": candidate.lineage_group,
        "parent_task_hash": candidate.parent_task_hash,
        "fixtures": sorted(candidate.input_fixture_hashes),
    })


def group_candidates(candidates: Iterable[DatasetCandidate]
                     ) -> dict[str, tuple[DatasetCandidate, ...]]:
    """Candidates keyed by leakage group, each group's members sorted by id.

    Groups that share a fixture are MERGED transitively: if A and B share fixture f1 and
    B and C share f2, all three move together. Without the merge, a chain of pairwise
    overlaps would be split into pieces that each look internally consistent.
    """
    direct: dict[str, list[DatasetCandidate]] = {}
    for candidate in candidates:
        direct.setdefault(leakage_group_key(candidate), []).append(candidate)

    # Union-find over shared fixture digests, so overlap is transitive.
    parent: dict[str, str] = {key: key for key in direct}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_fixture: dict[str, list[str]] = {}
    for key, members in direct.items():
        for member in members:
            for digest in member.input_fixture_hashes:
                by_fixture.setdefault(digest, []).append(key)
    for keys in by_fixture.values():
        first = keys[0]
        for other in keys[1:]:
            union(first, other)

    merged: dict[str, list[DatasetCandidate]] = {}
    for key, members in direct.items():
        merged.setdefault(find(key), []).extend(members)
    return {key: tuple(sorted(members, key=lambda c: c.candidate_id))
            for key, members in sorted(merged.items())}


# ── policy ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SplitPolicy:
    """What proportions are wanted, and the seed that makes the answer reproducible."""

    seed: str
    ratios: dict[str, float] = field(default_factory=dict)
    algorithm_version: str = SPLIT_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        # A missing seed is an error rather than a default. Deriving one from the clock
        # would make every build reproducible only by the machine that ran it.
        if not str(self.seed or "").strip():
            raise SplitError(
                "split.seed: required. A split with no recorded seed cannot be "
                "reproduced, and a dataset nobody can reproduce cannot be audited")
        object.__setattr__(self, "seed", require_id(self.seed, "split.seed"))
        ratios = dict(self.ratios) if self.ratios else dict(DEFAULT_RATIOS)
        clean: dict[str, float] = {}
        for name, value in ratios.items():
            split = name if isinstance(name, str) else str(name)
            DatasetSplit(split)  # raises on an unknown split name
            number = float(value)
            if not (0.0 <= number <= 1.0):
                raise SplitError(f"split.ratios[{split}]: {number} outside [0, 1]")
            clean[split] = number
        total = sum(clean.values())
        if abs(total - 1.0) > 1e-6:
            raise SplitError(f"split.ratios: must sum to 1.0, got {total:.6f}; a set "
                             f"that does not sum to one silently discards records")
        object.__setattr__(self, "ratios", dict(sorted(clean.items())))

    @property
    def assignable(self) -> tuple[DatasetSplit, ...]:
        """Splits with a nonzero share, in a fixed order. QUARANTINE is never here —
        nothing is ever assigned to quarantine by a ratio; it is a decision."""
        return tuple(DatasetSplit(name) for name, share in sorted(self.ratios.items())
                     if share > 0.0 and DatasetSplit(name) is not DatasetSplit.QUARANTINE)

    def to_dict(self) -> dict:
        return {"algorithm_version": self.algorithm_version, "seed": self.seed,
                "ratios": dict(sorted(self.ratios.items()))}

    def policy_hash(self) -> str:
        return sha256_obj(self.to_dict())


#: The default shape. Held-out splits are populated by explicit assignment rather than by
#: ratio, so their default share is zero — a security-regression set assembled by a hash
#: function is a random sample, not a regression suite.
DEFAULT_RATIOS: dict[str, float] = {
    DatasetSplit.TRAIN.value: 0.8,
    DatasetSplit.VALIDATION.value: 0.1,
    DatasetSplit.HIDDEN_EVALUATION.value: 0.1,
}


# ── the sticky ledger ─────────────────────────────────────────────────────────
class SplitAssignmentLedger:
    """The append-only record of where each leakage group has ever been.

    Consulted before the hash is even computed. This is the answer to seed grinding: a
    caller can re-run with any seed they like, and a group that has already been held out
    will either stay where it is or produce a :class:`StickyConflict` — never quietly
    relocate into TRAIN.

    Unlike the teacher store's reader, a torn final line is a hard error here. There, a
    discarded partial line costs one packet's replay protection; here it would silently
    release a group from its held-out assignment, which is the exact outcome the ledger
    exists to prevent.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def assignments(self) -> dict[str, str]:
        """``{group_key: split}``. Raises rather than tolerating a truncated ledger."""
        path = self._path
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            if raw and not raw.endswith("\n"):
                raise SplitError(
                    f"{path.name}: the final line is truncated. A split ledger may not "
                    f"discard it the way an audit log can — a dropped entry releases a "
                    f"group from its held-out assignment")
        try:
            lines = read_jsonl(path)
        except AtomicIOError as exc:
            raise SplitError(str(exc)) from None
        return {str(e.get("group_key", "")): str(e.get("split", ""))
                for e in lines if str(e.get("group_key", ""))}

    def record(self, *, group_key: str, split: DatasetSplit, dataset_version: str,
               at: str) -> None:
        """Append one assignment, refusing a move out of a held-out split."""
        existing = self.assignments().get(group_key)
        if existing is not None:
            previous = DatasetSplit(existing)
            if previous is split:
                return
            if previous in _HELD_OUT and split in _TRAIN_SIDE:
                raise StickyConflict(
                    f"group {short(group_key)} was assigned to {previous.value} and may "
                    f"not move to {split.value}; a group that has been measured against "
                    f"cannot later be trained on, whatever seed produces that answer")
        entry = {SCHEMA_KEY: SCHEMA_VERSION, "group_key": group_key,
                 "split": split.value, "dataset_version": dataset_version, "at": at}
        try:
            append_jsonl(self._path, entry, max_lines=MAX_ASSIGNMENT_LINES)
        except AtomicIOError as exc:
            raise SplitError(str(exc)) from None


class InMemorySplitAssignmentLedger:
    """A ledger for tests and dry runs. A separate class, never a mode."""

    def __init__(self, initial: Mapping[str, DatasetSplit] | None = None) -> None:
        self._entries: dict[str, str] = {k: DatasetSplit(v).value
                                         for k, v in (initial or {}).items()}

    def assignments(self) -> dict[str, str]:
        return dict(self._entries)

    def record(self, *, group_key: str, split: DatasetSplit, dataset_version: str,
               at: str) -> None:
        existing = self._entries.get(group_key)
        if existing is not None:
            previous = DatasetSplit(existing)
            if previous is split:
                return
            if previous in _HELD_OUT and split in _TRAIN_SIDE:
                raise StickyConflict(
                    f"group {short(group_key)} was assigned to {previous.value} and may "
                    f"not move to {split.value}")
        self._entries[group_key] = split.value


# ── the plan ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SplitPlan:
    """Which candidate goes where, and an honest account of how well it matched."""

    policy: SplitPolicy
    assignments: dict[str, str] = field(default_factory=dict)
    group_splits: dict[str, str] = field(default_factory=dict)
    quarantined: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    def candidates_in(self, split: DatasetSplit) -> tuple[str, ...]:
        return tuple(sorted(cid for cid, name in self.assignments.items()
                            if name == split.value))

    def counts(self) -> dict[str, int]:
        return {s.value: len(self.candidates_in(s)) for s in DatasetSplit}

    def realised_ratios(self) -> dict[str, float]:
        total = len(self.assignments)
        if not total:
            return {s.value: 0.0 for s in DatasetSplit}
        return {name: round(count / total, 6) for name, count in self.counts().items()}

    def shortfalls(self) -> tuple[str, ...]:
        """Every way this plan failed to deliver what was asked. Reported, not hidden.

        A requested split that came out EMPTY is named explicitly: an empty
        hidden-evaluation split is not a small deviation from the ratio, it is the
        absence of any held-out measurement, and it must not read as a successful build.
        """
        problems: list[str] = []
        realised = self.realised_ratios()
        for name, requested in sorted(self.policy.ratios.items()):
            if requested <= 0.0:
                continue
            actual = realised.get(name, 0.0)
            if self.counts().get(name, 0) == 0:
                problems.append(f"{name}: requested {requested:.2f} of the dataset but "
                                f"received no records; groups cannot be divided")
            elif abs(actual - requested) > 0.15:
                problems.append(f"{name}: requested {requested:.2f}, realised "
                                f"{actual:.2f}")
        return tuple(problems)

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "policy": self.policy.to_dict(),
                "assignments": dict(sorted(self.assignments.items())),
                "group_splits": dict(sorted(self.group_splits.items())),
                "quarantined": list(self.quarantined),
                "excluded": list(self.excluded),
                "counts": self.counts(),
                "realised_ratios": self.realised_ratios(),
                "shortfalls": list(self.shortfalls())}

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _assign_group(group_key: str, policy: SplitPolicy,
                  targets: Sequence[DatasetSplit]) -> DatasetSplit:
    """Map one group onto one split, deterministically and without a random module.

    The digest of ``(algorithm_version, seed, group_key)`` is read as an integer in a
    fixed 10 000-part range and compared against the cumulative ratio boundaries. Reading
    a fixed prefix rather than the whole digest keeps the arithmetic identical on every
    platform and every Python build.
    """
    digest = sha256_text(f"{policy.algorithm_version}:{policy.seed}:{group_key}")
    position = int(digest[:8], 16) % 10_000
    cumulative = 0
    for split in targets:
        cumulative += int(round(policy.ratios.get(split.value, 0.0) * 10_000))
        if position < cumulative:
            return split
    return targets[-1]


def plan_splits(candidates: Iterable[DatasetCandidate], *, policy: SplitPolicy,
                ledger: object | None = None,
                forced: Mapping[str, DatasetSplit] | None = None) -> SplitPlan:
    """Assign every candidate to exactly one split. Deterministic and order-independent.

    *forced* may only send a group to :data:`~training_gym.datasets.candidate.
    DatasetSplit.QUARANTINE` or to a held-out split. There is deliberately no way to
    force a record INTO training: an override that could do that is a hole through every
    other control in this module, and "the operator asked for it" is exactly how a
    contaminated training set gets built.
    """
    targets = policy.assignable
    if not targets:
        raise SplitError("split: the policy assigns a nonzero share to no split")

    records = list(candidates)
    quarantined: list[str] = []
    excluded: list[str] = []
    eligible: list[DatasetCandidate] = []
    for candidate in records:
        if candidate.state in (CandidateState.QUARANTINED, CandidateState.REJECTED,
                               CandidateState.FAILED, CandidateState.REVOKED):
            quarantined.append(candidate.candidate_id)
        elif candidate.evaluation_only or not candidate.dataset_eligible:
            excluded.append(candidate.candidate_id)
        else:
            eligible.append(candidate)

    for name, split in sorted((forced or {}).items()):
        target = DatasetSplit(split)
        if target in _TRAIN_SIDE:
            raise SplitError(
                f"split: refusing to force group {short(name)} into {target.value}. An "
                f"override may isolate a record; it may never place one into training, "
                f"because that is a hole through every other control here")

    groups = group_candidates(eligible)
    known = dict(getattr(ledger, "assignments", dict)()) if ledger is not None else {}
    assignments: dict[str, str] = {}
    group_splits: dict[str, str] = {}

    for group_key, members in groups.items():
        if group_key in (forced or {}):
            split = DatasetSplit(forced[group_key])
        elif group_key in known:
            # The ledger wins over the hash. This is what makes seed grinding useless.
            split = DatasetSplit(known[group_key])
        else:
            split = _assign_group(group_key, policy, targets)
        group_splits[group_key] = split.value
        for member in members:
            if split is DatasetSplit.QUARANTINE:
                quarantined.append(member.candidate_id)
            else:
                assignments[member.candidate_id] = split.value

    return SplitPlan(policy=policy, assignments=assignments,
                     group_splits=group_splits,
                     quarantined=tuple(sorted(set(quarantined))),
                     excluded=tuple(sorted(set(excluded))))


def commit_plan(plan: SplitPlan, ledger: object, *, dataset_version: str,
                at: str) -> None:
    """Write every group assignment to the sticky ledger, or raise on the first conflict.

    Effectful and separate from :func:`plan_splits` on purpose: a dry run must be able to
    compute exactly what would happen without spending the decision.
    """
    record = getattr(ledger, "record", None)
    if not callable(record):
        raise SplitError("split: the ledger must implement record(...)")
    for group_key, split in sorted(plan.group_splits.items()):
        record(group_key=group_key, split=DatasetSplit(split),
               dataset_version=dataset_version, at=at)


__all__ = [
    "DEFAULT_RATIOS", "MAX_ASSIGNMENT_LINES", "SPLIT_ALGORITHM_VERSION",
    "InMemorySplitAssignmentLedger", "SplitAssignmentLedger", "SplitError", "SplitPlan",
    "SplitPolicy", "StickyConflict", "commit_plan", "group_candidates",
    "leakage_group_key", "plan_splits",
]
