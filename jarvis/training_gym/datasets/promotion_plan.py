"""training_gym/datasets/promotion_plan.py — V69 M62 S2d: the decision to promote.

WHY A PLAN EXISTS AT ALL
------------------------
Promotion is the one irreversible operation in the subsystem. Everything before it can be
recomputed; a dataset version, once written, is immutable by design and cannot be
corrected in place. So the operator has to be able to see *exactly* what will happen —
which candidates, which splits, which files, which state transitions — and then authorise
**that**, not "promotion in general".

A :class:`PromotionPlan` is that description, and its digest is the thing being
authorised. The confirmation token is ``PROMOTE:<full plan hash>``:

  * a generic ``--yes``, ``--force`` or a caller's ``True`` authorises nothing, because it
    would have authorised any other plan equally well;
  * a short prefix is refused, because a prefix is a claim about a hash rather than the
    hash — and the operator who typed twelve characters did not read the other fifty-two;
  * the plan is RECOMPUTED from the current inputs at promotion time and the token is
    compared against the recomputed digest. A candidate edited, a split moved, a seed
    changed, a leakage report re-run, a revocation filed or a parent version added
    between planning and promotion all change the digest, and the confirmation the
    operator holds stops matching. That is the point: it is not a signature over an
    intention, it is a signature over a state of the world.

WHAT A DRY RUN IS
-----------------
:func:`plan_promotion` performs every validation and writes nothing: no directory, no
shard, no manifest, no ledger line, no state transition. If it raises, nothing happened.
If it returns, the plan it returns is exactly what :func:`promote` will do.

ORDER IS THE CONTRACT
---------------------
::

    recompute the plan   -> compare the recomputed digest to the token
    -> refuse a consumed plan
    -> write shards atomically, re-hash them from disk
    -> write the manifest last, reload it, verify it
    -> append the promotion ledger entry (the plan is now spent)
    -> transition READY_FOR_PROMOTION -> PROMOTED, append-only
    -> report

The plan is consumed only after a version exists, so a failed promotion never spends the
operator's confirmation. Candidates become ``PROMOTED`` only after the version they belong
to exists, so a record is never marked as shipped in a dataset that was never written. If
a transition fails after the version exists, the version is NOT deleted — it is immutable
and correct — and the inconsistency is reported as a failure with the evidence needed to
finish the move by hand.
"""
from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
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
from ..task_spec import require_timestamp
from .candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    require_digest,
)
from .leakage import LeakageReport
from .manifests import (
    GENESIS_PARENT,
    SHARD_SPLITS,
    TRAIN_SIDE_SPLITS,
    ManifestError,
    RevocationSnapshot,
    WrittenVersion,
    latest_manifest_hash,
    shard_filename,
    verify_parent,
    version_dir,
    write_dataset_version,
)
from .promotion import PROMOTION_POLICY_VERSION
from .split import SplitPlan

#: Bump when the plan SHAPE changes. Part of the digest, so a shape change invalidates
#: every outstanding confirmation — which is correct: they authorised a different object.
PLAN_SCHEMA_VERSION = "m62.promotion_plan.1"

#: The one legal confirmation shape. Exact case, full digest, no separator variants.
CONFIRMATION_PREFIX = "PROMOTE:"

#: The append-only record of every promotion this store has performed.
PROMOTION_LEDGER_FILE = "promotions.jsonl"
MAX_PROMOTION_LEDGER_LINES = 100_000


class PromotionError(SchemaError):
    """A promotion was refused. Never a partial version, never a spent confirmation."""


class ConfirmationRejected(PromotionError):
    """The confirmation token did not authorise this exact plan."""


class PlanAlreadyConsumed(PromotionError):
    """This plan's digest has already promoted a dataset version."""


# ── the request ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromotionRequest:
    """Everything a promotion is a function of. Planning and promoting share it.

    One object rather than two parameter lists, because the guarantee being made is that
    :func:`promote` re-derives the SAME plan from the SAME inputs. Two signatures that
    could drift apart would make the recomputation check compare two different functions.
    """

    root: str | Path
    dataset_id: str
    proposed_dataset_version: str
    candidates: tuple[DatasetCandidate, ...]
    split_plan: SplitPlan
    leakage_report: LeakageReport
    revocation: RevocationSnapshot
    created_at_utc: str
    actor: str
    parent_manifest_hash: str = ""
    allow_empty_splits: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "dataset_id", require_id(self.dataset_id, "plan.dataset_id"))
        setattr_(self, "proposed_dataset_version",
                 require_id(self.proposed_dataset_version,
                            "plan.proposed_dataset_version"))
        setattr_(self, "created_at_utc", require_timestamp(self.created_at_utc,
                                                           "plan.created_at_utc"))
        setattr_(self, "actor", require_id(self.actor, "plan.actor"))
        setattr_(self, "candidates", tuple(self.candidates))
        setattr_(self, "allow_empty_splits", tuple(sorted(set(self.allow_empty_splits))))

    def resolved_parent(self) -> str:
        """The declared parent, or the newest verifiable version's digest.

        Resolved rather than defaulted to :data:`GENESIS_PARENT`: a build that silently
        called itself the genesis whenever it could not find its parent would break the
        chain exactly when the chain matters.
        """
        if self.parent_manifest_hash:
            return self.parent_manifest_hash
        return latest_manifest_hash(root=self.root, dataset_id=self.dataset_id)

    def output_root_id(self) -> str:
        """A digest of the output root. Identifies it without publishing it.

        The plan is a document an operator pastes into a ticket. The absolute path of the
        store is their home directory, and it has no business travelling with the plan —
        but two different roots must still produce two different plans.
        """
        return sha256_text(Path(self.root).resolve().as_posix())


# ── the plan ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromotionPlan:
    """Exactly what a promotion will do, and the digest that authorises it."""

    dataset_id: str
    proposed_dataset_version: str
    parent_manifest_hash: str
    seed: str
    split_policy: dict
    split_policy_hash: str
    leakage_report_hash: str
    leakage_verdict: str
    revocation_snapshot_hash: str
    output_root_id: str
    created_at_utc: str
    entries: tuple[dict, ...] = ()
    excluded: tuple[dict, ...] = ()
    expected_shard_filenames: tuple[str, ...] = ()
    allow_empty_splits: tuple[str, ...] = ()
    plan_schema_version: str = PLAN_SCHEMA_VERSION
    promotion_policy_version: str = PROMOTION_POLICY_VERSION

    @property
    def expected_candidate_count(self) -> int:
        return len(self.entries)

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry["split"]] = counts.get(entry["split"], 0) + 1
        return dict(sorted(counts.items()))

    def expected_effects(self) -> dict:
        """The complete list of side effects, in the vocabulary an operator can check."""
        return {
            "creates_dataset_version": f"{self.dataset_id}/"
                                       f"{self.proposed_dataset_version}",
            "creates_files": list(self.expected_shard_filenames) + ["manifest.json"],
            "records_per_split": self.counts(),
            "total_records": self.expected_candidate_count,
            "candidate_transitions": [
                {"candidate_id": e["candidate_id"],
                 "from": CandidateState.READY_FOR_PROMOTION.value,
                 "to": CandidateState.PROMOTED.value} for e in self.entries],
            "appends_ledgers": sorted([PROMOTION_LEDGER_FILE, "transitions.jsonl"]),
            "excluded_candidates": [dict(e) for e in self.excluded],
            "trains_a_model": False,
            "downloads_anything": False,
            "contacts_the_network": False,
        }

    def to_dict(self) -> dict:
        """The canonical plan. Excludes ``plan_hash`` — that is computed over it."""
        return {
            SCHEMA_KEY: SCHEMA_VERSION,
            "plan_schema_version": self.plan_schema_version,
            "promotion_policy_version": self.promotion_policy_version,
            "dataset_id": self.dataset_id,
            "proposed_dataset_version": self.proposed_dataset_version,
            "parent_manifest_hash": self.parent_manifest_hash,
            "seed": self.seed,
            "split_policy": dict(sorted(self.split_policy.items())),
            "split_policy_hash": self.split_policy_hash,
            "leakage_report_hash": self.leakage_report_hash,
            "leakage_verdict": self.leakage_verdict,
            "revocation_snapshot_hash": self.revocation_snapshot_hash,
            "output_root_id": self.output_root_id,
            "created_at_utc": self.created_at_utc,
            "entries": [dict(sorted(e.items())) for e in self.entries],
            "excluded": [dict(sorted(e.items())) for e in self.excluded],
            "expected_shard_filenames": list(self.expected_shard_filenames),
            "expected_candidate_count": self.expected_candidate_count,
            "allow_empty_splits": list(self.allow_empty_splits),
        }

    def to_record(self) -> dict:
        """The plan plus its digest, its effects and the token that authorises it."""
        return {**self.to_dict(), "plan_hash": self.plan_hash(),
                "expected_effects": self.expected_effects(),
                "confirmation_required": self.confirmation_token()}

    def plan_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def confirmation_token(self) -> str:
        """The exact string that authorises this plan and no other."""
        return f"{CONFIRMATION_PREFIX}{self.plan_hash()}"

    def assignments(self) -> dict:
        return {e["candidate_id"]: e["split"] for e in self.entries}


# ── planning (the dry run) ────────────────────────────────────────────────────
def plan_promotion(request: PromotionRequest) -> PromotionPlan:
    """Validate everything and describe exactly what promotion would do. Writes nothing.

    Nothing in this function creates a directory, opens a file for writing, appends to a
    ledger or transitions a candidate. A dry run that spent any part of the decision it
    was previewing would not be a dry run.
    """
    root = request.root
    destination = version_dir(root, request.dataset_id, request.proposed_dataset_version)
    if destination.exists():
        raise PromotionError(
            f"promotion: dataset version {request.proposed_dataset_version!r} already "
            f"exists; a version is immutable and is never rewritten")

    parent = request.resolved_parent()
    verify_parent(root=root, dataset_id=request.dataset_id,
                  parent_manifest_hash=parent)

    report = request.leakage_report
    if report.blocks_finalization:
        raise PromotionError(
            f"promotion: the leakage analysis returned {report.verdict.value} and does "
            f"not permit finalization ({list(report.notes) or 'see findings'}); "
            f"insufficient evidence is refused exactly as hard as detected leakage, "
            f"because a check that could not run proved nothing")

    assignments = dict(request.split_plan.assignments)
    entries: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()

    for candidate in sorted(request.candidates, key=lambda c: c.candidate_id):
        cid = candidate.candidate_id
        if cid in seen:
            raise PromotionError(f"promotion: candidate {cid!r} was offered twice")
        seen.add(cid)

        # Revoked material is excluded automatically AND named. A silent omission would
        # leave the operator approving a record count they cannot account for.
        if request.revocation.is_revoked(candidate):
            excluded.append({"candidate_id": cid,
                             "candidate_hash": candidate.candidate_hash(),
                             "reason": "revoked"})
            continue
        if cid not in assignments:
            excluded.append({"candidate_id": cid,
                             "candidate_hash": candidate.candidate_hash(),
                             "reason": "no split assignment in the split plan"})
            continue
        if candidate.state is not CandidateState.READY_FOR_PROMOTION:
            raise PromotionError(
                f"promotion: candidate {cid!r} is {candidate.state.value}; only "
                f"ready_for_promotion may enter a plan, because every earlier state "
                f"means a gate has not yet recorded that it ran")

        split = DatasetSplit(assignments[cid])
        if split not in SHARD_SPLITS:
            raise PromotionError(
                f"promotion: candidate {cid!r} is assigned to {split.value}, which is "
                f"never written into a dataset version")
        if (candidate.evaluation_only or not candidate.dataset_eligible) \
                and split in TRAIN_SIDE_SPLITS:
            raise PromotionError(
                f"promotion: candidate {cid!r} is evaluation-only or dataset-ineligible "
                f"and is assigned to {split.value}; that is held-out material a model "
                f"would be fitted on")
        # BOTH digests are recorded. ``candidate_hash`` is the record the operator
        # reviewed, in ``ready_for_promotion``; ``promoted_hash`` is the record that will
        # be written into the immutable shard, in ``promoted``. A candidate's digest
        # covers its state, so these necessarily differ — and binding only one of them
        # would leave the other free to be something the plan never described.
        entries.append({"candidate_id": cid, "candidate_hash": candidate.candidate_hash(),
                        "promoted_hash": candidate.with_state(
                            CandidateState.PROMOTED).candidate_hash(),
                        "split": split.value, "task_hash": candidate.task_hash,
                        "attempt_hash": candidate.attempt_hash})

    if not entries:
        raise PromotionError(
            "promotion: no candidate survived planning; refusing to create a dataset "
            "version with no records rather than producing an artefact that verifies "
            "perfectly and contains nothing")

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["split"]] = counts.get(entry["split"], 0) + 1
    if not counts.get(DatasetSplit.TRAIN.value, 0) and \
            DatasetSplit.TRAIN.value not in request.allow_empty_splits:
        raise PromotionError(
            "promotion: this plan produces no TRAIN records. An empty training set "
            "passes every integrity check and teaches nothing; declare it explicitly in "
            "allow_empty_splits if that is genuinely intended")

    policy = request.split_plan.policy
    return PromotionPlan(
        dataset_id=request.dataset_id,
        proposed_dataset_version=request.proposed_dataset_version,
        parent_manifest_hash=parent,
        seed=policy.seed,
        split_policy=policy.to_dict(),
        split_policy_hash=policy.policy_hash(),
        leakage_report_hash=report.report_hash(),
        leakage_verdict=report.verdict.value,
        revocation_snapshot_hash=request.revocation.snapshot_hash(),
        output_root_id=request.output_root_id(),
        created_at_utc=request.created_at_utc,
        entries=tuple(entries),
        excluded=tuple(sorted(excluded, key=lambda e: e["candidate_id"])),
        expected_shard_filenames=tuple(shard_filename(s) for s in SHARD_SPLITS
                                       if counts.get(s.value, 0)),
        allow_empty_splits=request.allow_empty_splits)


# ── the confirmation ──────────────────────────────────────────────────────────
def check_confirmation(confirmation: object, plan: PromotionPlan) -> str:
    """Refuse anything that is not the exact token for exactly this plan.

    A ``bool`` is refused explicitly rather than stringified: ``confirm=True`` is the
    shape of every accidental automation, and turning it into ``"True"`` and then failing
    the comparison would work by luck rather than by rule. A value that looks like a file
    reference is refused too — a confirmation read out of a persisted config is a
    confirmation nobody typed, which is the one property this token exists to have.
    """
    if isinstance(confirmation, bool) or not isinstance(confirmation, str):
        raise ConfirmationRejected(
            f"promotion: confirmation must be the string "
            f"{CONFIRMATION_PREFIX}<plan-hash>, got {type(confirmation).__name__}; a "
            f"boolean authorises every plan equally and therefore authorises none")
    token = confirmation.strip()
    if token.startswith("@") or "/" in token or "\\" in token:
        raise ConfirmationRejected(
            "promotion: a confirmation that names a file is refused; the token must be "
            "supplied directly, because one read from persisted config is one nobody "
            "typed for this plan")
    if not token.startswith(CONFIRMATION_PREFIX):
        raise ConfirmationRejected(
            f"promotion: confirmation must start with {CONFIRMATION_PREFIX!r}; "
            f"{short(token)!r} authorises nothing in particular")
    digest = token[len(CONFIRMATION_PREFIX):]
    if len(digest) != 64:
        raise ConfirmationRejected(
            f"promotion: confirmation carries a {len(digest)}-character digest; the full "
            f"64 are required, because a prefix is a claim about a hash and not the hash")
    expected = plan.confirmation_token()
    if not hmac.compare_digest(token, expected):
        raise ConfirmationRejected(
            f"promotion: this confirmation authorises plan {short(digest)}, but the plan "
            f"recomputed from the current inputs is {short(plan.plan_hash())}. Something "
            f"changed since it was issued — a candidate, a split, the seed, the leakage "
            f"report, the revocation snapshot or the parent version — and the operator "
            f"approved the earlier state")
    return token


# ── the ledger ────────────────────────────────────────────────────────────────
def promotion_ledger_path(root: str | Path) -> Path:
    return Path(root) / PROMOTION_LEDGER_FILE


def promotion_entries(root: str | Path) -> list[dict]:
    try:
        return read_jsonl(promotion_ledger_path(root))
    except AtomicIOError as exc:
        raise PromotionError(str(exc)) from None


def is_plan_consumed(root: str | Path, plan_hash: str) -> bool:
    digest = require_digest(plan_hash, "promotion.plan_hash")
    return any(str(e.get("plan_hash", "")) == digest for e in promotion_entries(root))


# ── the result ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromotionResult:
    """What a promotion actually did, including the parts that did not work."""

    plan: PromotionPlan
    written: WrittenVersion | None = None
    promoted: tuple[DatasetCandidate, ...] = ()
    transitions: tuple[dict, ...] = ()
    inconsistencies: tuple[str, ...] = field(default_factory=tuple)
    residue: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """A promotion is a success only when the version exists AND every record moved.

        A version with unmoved candidates is a real, immutable, correct dataset whose
        candidate store disagrees with it. That is recoverable by hand and must not be
        reported as a clean run, because the next build would read the stale states.
        """
        return (self.written is not None and not self.inconsistencies
                and not self.residue)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "plan_hash": self.plan.plan_hash(),
                "dataset_id": self.plan.dataset_id,
                "dataset_version": self.plan.proposed_dataset_version,
                "manifest_hash": self.written.manifest_hash if self.written else "",
                "records": len(self.promoted),
                "counts": self.written.manifest.counts() if self.written else {},
                "relative_paths": (list(self.written.relative_paths) if self.written
                                   else []),
                "transitions": len(self.transitions),
                "inconsistencies": list(self.inconsistencies),
                "residue": list(self.residue)}


# ── the effective promotion ───────────────────────────────────────────────────
def promote(request: PromotionRequest, *, confirmation: object,
            store: object) -> PromotionResult:
    """Finalize one immutable dataset version, under an exact plan-bound confirmation.

    *store* must be a :class:`~training_gym.datasets.candidate_store.CandidateStore` (or
    something with the same three methods): it owns candidate records, the transition
    ledger and the promotion ledger, and routing those writes through it is what keeps
    this module from becoming a second authority over candidate state.
    """
    for name in ("write_candidate", "record_transition", "root"):
        if not hasattr(store, name):
            raise PromotionError(f"promotion: the store must provide {name}; promotion "
                                 f"never writes candidate state itself")
    root = Path(getattr(store, "root"))
    if root.resolve() != Path(request.root).resolve():
        raise PromotionError(
            "promotion: the store root and the requested output root differ; two roots "
            "means the manifest and the candidate history would describe different "
            "datasets")

    # Recomputed, never accepted from the caller. This is the check that turns the token
    # into a signature over the current state of the world rather than over an intention.
    plan = plan_promotion(request)
    check_confirmation(confirmation, plan)
    if is_plan_consumed(root, plan.plan_hash()):
        raise PlanAlreadyConsumed(
            f"promotion: plan {short(plan.plan_hash())} has already promoted a dataset "
            f"version; a confirmation authorises one promotion, and replaying it would "
            f"produce a second version nobody approved")

    # The shard holds the PROMOTED projection of each record — that is what a finalized
    # dataset contains. Building it in memory is not the same as committing it: nothing
    # reaches the candidate store until the version below exists.
    by_id = {c.candidate_id: c for c in request.candidates}
    sources = [by_id[e["candidate_id"]] for e in plan.entries]
    members = [c.with_state(CandidateState.PROMOTED) for c in sources]
    for entry, member in zip(plan.entries, members, strict=True):
        if member.candidate_hash() != entry["promoted_hash"]:  # pragma: no cover
            raise PromotionError(
                f"promotion: candidate {entry['candidate_id']} does not hash to the "
                f"record the plan described; refusing to write a version the operator "
                f"did not approve")

    written = write_dataset_version(
        root=root, dataset_id=plan.dataset_id,
        dataset_version=plan.proposed_dataset_version, candidates=members,
        assignments=plan.assignments(), parent_manifest_hash=plan.parent_manifest_hash,
        created_at_utc=plan.created_at_utc, seed=plan.seed,
        split_policy_hash=plan.split_policy_hash,
        leakage_report_hash=plan.leakage_report_hash, revocation=request.revocation,
        quarantine_audit=tuple(
            {"candidate_id": e["candidate_id"],
             "candidate_hash": e.get("candidate_hash", ""),
             "reason": e.get("reason", "")} for e in plan.excluded),
        allow_empty_splits=plan.allow_empty_splits)

    # The version exists. The plan is spent from here on, whatever else happens — a
    # confirmation that produced a real immutable artefact must never authorise a second.
    _append_promotion(root, plan=plan, written=written, actor=request.actor,
                      at=request.created_at_utc)

    promoted: list[DatasetCandidate] = []
    transitions: list[dict] = []
    inconsistencies: list[str] = []
    for source, moved in zip(sources, members, strict=True):
        try:
            store.write_candidate(moved)
            transitions.append(store.record_transition(
                moved, from_state=source.state, actor=request.actor,
                at=request.created_at_utc,
                reason=f"promoted into {plan.dataset_id}/"
                       f"{plan.proposed_dataset_version}"))
            promoted.append(moved)
        except (SchemaError, OSError) as exc:
            inconsistencies.append(
                f"{source.candidate_id}: the dataset version was written and contains "
                f"this record, but the candidate is still {source.state.value} "
                f"({exc}). The version is immutable and correct; finish the transition "
                f"by hand — do not rebuild the version")

    return PromotionResult(plan=plan, written=written, promoted=tuple(promoted),
                           transitions=tuple(transitions),
                           inconsistencies=tuple(inconsistencies),
                           residue=written.residue)


def _append_promotion(root: Path, *, plan: PromotionPlan, written: WrittenVersion,
                      actor: str, at: str) -> None:
    entry = {
        SCHEMA_KEY: SCHEMA_VERSION,
        "plan_hash": plan.plan_hash(),
        "plan_schema_version": plan.plan_schema_version,
        "promotion_policy_version": plan.promotion_policy_version,
        "dataset_id": plan.dataset_id,
        "dataset_version": plan.proposed_dataset_version,
        "manifest_hash": written.manifest_hash,
        "parent_manifest_hash": plan.parent_manifest_hash,
        "record_count": plan.expected_candidate_count,
        "leakage_report_hash": plan.leakage_report_hash,
        "revocation_snapshot_hash": plan.revocation_snapshot_hash,
        "actor": require_id(actor, "promotion.actor"),
        "at": require_timestamp(at, "promotion.at"),
    }
    try:
        append_jsonl(promotion_ledger_path(root), entry,
                     max_lines=MAX_PROMOTION_LEDGER_LINES)
    except AtomicIOError as exc:
        # The version exists but the ledger could not record it. Refusing loudly is the
        # only honest outcome: an unrecorded promotion is one whose plan can be replayed.
        raise PromotionError(
            f"promotion: dataset version {plan.proposed_dataset_version} was written but "
            f"the promotion ledger could not be appended ({exc}); the version is "
            f"immutable and intact, and the ledger must be repaired before promoting "
            f"again") from None


def describe_plan(plan: PromotionPlan) -> dict:
    """The operator-facing view: the plan, its digest, its effects and its token."""
    return plan.to_record()


def candidate_assignments(plan: SplitPlan,
                          candidates: Sequence[DatasetCandidate]) -> Mapping[str, str]:
    """Convenience for callers assembling a request from a split plan."""
    known = {c.candidate_id for c in candidates}
    return {cid: split for cid, split in plan.assignments.items() if cid in known}


__all__ = [
    "CONFIRMATION_PREFIX", "GENESIS_PARENT", "MAX_PROMOTION_LEDGER_LINES",
    "PLAN_SCHEMA_VERSION", "PROMOTION_LEDGER_FILE", "ConfirmationRejected",
    "ManifestError", "PlanAlreadyConsumed", "PromotionError", "PromotionPlan",
    "PromotionRequest", "PromotionResult", "candidate_assignments",
    "check_confirmation", "describe_plan", "is_plan_consumed", "plan_promotion",
    "promote", "promotion_entries", "promotion_ledger_path",
]
