"""reasoning_distillation/holdout.py — V69 M67A: the firewall, as a boundary not a convention.

WHAT THIS PROTECTS, AND FROM WHOM
---------------------------------
§17 asks for this to be treated **like a security boundary for experimental validity**, and
the threat model is unusual: the adversary is *us*. Nobody is attacking the holdout. What
happens instead is that a developer needs one more example to debug an extractor, a report
renders a few turns to show what a family looks like, or a tuning loop wants to know why the
critic disagrees — and each of those is a small, well-intentioned read that spends a resource
which cannot be un-spent.

So the design assumption is that every caller is honest and none of them is careful. The guard
is therefore **the only way to obtain content for distillation**, and it refuses by default:

  * :meth:`HoldoutGuard.permit` answers only for families it can positively place;
  * an UNPLACED or UNKNOWN family is REFUSED, not defaulted to development (§17: source
    identity uncertainty fails closed);
  * :class:`HoldoutGuard` has no method, flag or keyword that returns holdout content. Not a
     disabled one, not one behind a config key — there is no such code path in this module, so
     there is nothing to accidentally enable. The frozen partition is reachable only as a set
     of family IDS, which cannot reconstruct anything.

WHY THE GUARD IS AN OBJECT AND NOT A CHECK
------------------------------------------
Because a check is something a caller can forget, and because this repository has already
measured what happens when a control depends on every call site remembering it. A guard that
must be CONSTRUCTED and PASSED makes the omission a signature error rather than a silent read:
:func:`reasoning_distillation.distiller.distil_corpus` cannot be called without one, so
"the distiller accessed the holdout" is not a mistake that type-checks.

The narrower version of the same idea: :meth:`HoldoutGuard.filter` returns BOTH the allowed
items and the refused ones, with reasons. A filter that silently dropped the holdout items
would be correct and useless — the caller could not tell a holdout refusal from an empty
corpus, and the distinction is the whole audit trail.

THE SEVEN CHECKS
----------------
:func:`audit` implements §17's list as seven independent, individually-reported checks. They
are independent on purpose: a single boolean "leakage: clean" is exactly the unauditable
shape this repository recorded as a defect at S4G. Each check can be absent, pass, or fail,
and "the check could not run" is a FAILURE rather than a pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from training_gym.schemas import SchemaError

from .config import DedupeConfig
from .dedupe import DedupeReport
from .models import CanonicalConversation, ExportStatus
from .splitting import FrozenHoldout, Partition, SplitPlan

#: Bump when a check is added, removed or its verdict semantics change. Recorded in the
#: manifest so a "clean" audit can be attributed to a known check set — a clean result from an
#: unknown set of checks is not evidence.
FIREWALL_VERSION = "m67a.firewall.1"


class HoldoutAccessDenied(SchemaError):
    """An attempt to reach frozen-holdout content. The hardest refusal in the package."""


class LeakageError(SchemaError):
    """A leakage audit could not be completed, so its result is UNKNOWN, not clean."""


class Verdict(str, Enum):
    """One check's outcome. ``UNAVAILABLE`` is a failure state, not a neutral one."""

    PASS = "pass"  # nosec B105 — an audit verdict, not a credential
    FAIL = "fail"
    UNAVAILABLE = "unavailable"

    @property
    def is_clean(self) -> bool:
        return self is Verdict.PASS


@dataclass(frozen=True)
class Finding:
    """One check's result, with the evidence a reviewer needs and nothing more."""

    check: str
    verdict: Verdict
    detail: str = ""
    #: Family or conversation IDS only. Never content, never a digest of content that could
    #: be matched against a rendered body.
    subjects: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"check": self.check, "verdict": self.verdict.value,
                "detail": self.detail[:320],
                "subjects": [s[:64] for s in sorted(self.subjects)[:16]]}


@dataclass(frozen=True)
class LeakageAudit:
    """The result of every §17 check. Clean only when every check individually passed."""

    findings: tuple[Finding, ...]
    firewall_version: str = FIREWALL_VERSION
    checks_expected: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.verdict is not Verdict.PASS)

    @property
    def clean(self) -> bool:
        """True only when every EXPECTED check ran and passed.

        A missing check makes the audit unclean even if nothing failed. That is the point: an
        audit that silently skipped its hardest check reports the same "no failures" as one
        that ran it, and this repository has a recorded defect from exactly that shape.
        """
        ran = {f.check for f in self.findings}
        missing = set(self.checks_expected) - ran
        return not missing and all(f.verdict.is_clean for f in self.findings)

    @property
    def missing_checks(self) -> tuple[str, ...]:
        ran = {f.check for f in self.findings}
        return tuple(sorted(set(self.checks_expected) - ran))

    def to_dict(self) -> dict:
        return {
            "firewall_version": self.firewall_version,
            "clean": self.clean,
            "checks_expected": list(self.checks_expected),
            "checks_run": sorted({f.check for f in self.findings}),
            "missing_checks": list(self.missing_checks),
            "failure_count": len(self.failures),
            "findings": [f.to_dict() for f in self.findings],
        }

    def raise_if_unclean(self) -> None:
        if self.clean:
            return
        parts = [f"{f.check}={f.verdict.value}" for f in self.failures]
        if self.missing_checks:
            parts.append(f"missing={list(self.missing_checks)}")
        raise LeakageError("leakage audit is not clean: " + "; ".join(parts[:6]))


class HoldoutGuard:
    """The only gate through which corpus content reaches a distillation stage (§17).

    Construct it once per build from the settled plan and the frozen record, then pass it. It
    holds family IDS and a partition map — no conversations, no text, nothing that could
    render a body even if something printed it.
    """

    __slots__ = ("_split", "_frozen", "_holdout_ids")

    def __init__(self, split: SplitPlan, frozen: FrozenHoldout | None = None) -> None:
        self._split = split
        self._frozen = frozen
        # The union of BOTH sources of truth. A family is holdout if the plan says so OR the
        # frozen record says so, and disagreement between them resolves toward refusal: if the
        # plan has drifted, the frozen record is the authority, and a family that either one
        # calls held out is held out.
        planned = set(split.families_in(Partition.FROZEN_HOLDOUT))
        recorded = set(frozen.family_ids) if frozen is not None else set()
        self._holdout_ids = frozenset(planned | recorded)

    @property
    def holdout_family_count(self) -> int:
        return len(self._holdout_ids)

    def is_holdout(self, family_id: str) -> bool:
        return family_id in self._holdout_ids

    def permit(self, family_id: str) -> bool:
        """Whether a development stage may read this family's content.

        Refuses three ways, and the third is the one that matters: a family the plan cannot
        place at all is refused. An unplaced family is not "probably development"; it is
        unknown, and §17 requires unknown to fail closed.
        """
        if not family_id:
            return False
        if self.is_holdout(family_id):
            return False
        try:
            partition = self._split.partition_of(family_id)
        except SchemaError:
            return False
        return partition in (Partition.DEVELOPMENT, Partition.VALIDATION)

    def assert_permitted(self, family_id: str, *, operation: str) -> None:
        """Raise :class:`HoldoutAccessDenied` unless *family_id* is readable."""
        if self.permit(family_id):
            return
        if self.is_holdout(family_id):
            raise HoldoutAccessDenied(
                f"{operation}: family {family_id[:12]} is in the FROZEN HOLDOUT. Its content "
                f"is not available to any development or model-assisted stage (§9, §17). "
                f"There is no flag that changes this")
        raise HoldoutAccessDenied(
            f"{operation}: family {family_id[:12]} has no readable partition assignment, so "
            f"whether it is held out is UNKNOWN. Unknown fails closed (§17)")

    def filter(self, conversations: "list[CanonicalConversation]",
               *, operation: str = "read") -> "tuple[tuple[CanonicalConversation, ...], tuple[Finding, ...]]":
        """Split *conversations* into readable ones and refusals-with-reasons.

        Returns both halves rather than silently dropping the refused ones: a caller that
        received only the allowed list could not tell a holdout refusal from an empty corpus,
        and that distinction is the audit trail.
        """
        allowed: list[CanonicalConversation] = []
        refused: list[Finding] = []
        for conversation in conversations:
            family = conversation.family_id
            if self.permit(family):
                allowed.append(conversation)
                continue
            reason = ("in the frozen holdout" if self.is_holdout(family)
                      else "no readable partition assignment (unknown fails closed)")
            refused.append(Finding(
                check="holdout_guard", verdict=Verdict.PASS,
                detail=f"{operation}: refused — {reason}",
                subjects=(family or "<ungrouped>",)))
        return (tuple(allowed), tuple(refused))

    def assert_no_holdout_content(self, conversations: "list[CanonicalConversation]",
                                  *, operation: str) -> None:
        """Raise if *conversations* contains anything not readable. Use before a model call.

        This is the §17 line "model-assisted distillation cannot access holdout" made
        executable. It is a separate method from :meth:`filter` because the two callers want
        opposite behaviour: a batch loader wants to skip and report, while anything about to
        send material to a provider must ABORT — a partial send has already happened by the
        time a report is written.
        """
        for conversation in conversations:
            self.assert_permitted(conversation.family_id, operation=operation)

    def readable_partitions(self) -> tuple[str, ...]:
        return tuple(sorted(p.value for p in (Partition.DEVELOPMENT, Partition.VALIDATION)))

    def to_dict(self) -> dict:
        """Body-free description. Family ids only; no content and no conversation digests."""
        return {
            "firewall_version": FIREWALL_VERSION,
            "holdout_family_count": self.holdout_family_count,
            "readable_partitions": list(self.readable_partitions()),
            "frozen_record_present": self._frozen is not None,
            "frozen_digest": self._frozen.holdout_digest if self._frozen else "",
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"HoldoutGuard(holdout_families={self.holdout_family_count}, "
                f"frozen_record_present={self._frozen is not None})")


#: The checks §17 enumerates. Named here so :class:`LeakageAudit` can tell "passed" from
#: "never ran" — a distinction a list of findings alone cannot make.
EXPECTED_CHECKS: tuple[str, ...] = (
    "single_partition_per_conversation",
    "family_never_straddles_partitions",
    "near_duplicate_never_crosses_into_holdout",
    "holdout_reachable_only_through_guard",
    "every_item_has_a_family",
    "export_safe_is_explicit",
    "frozen_record_matches_plan",
)


def audit(*, report: DedupeReport, split: SplitPlan,
          conversations: "list[CanonicalConversation]",
          frozen: FrozenHoldout | None = None,
          guard: HoldoutGuard | None = None,
          config: DedupeConfig | None = None) -> LeakageAudit:
    """Run every §17 check and report each one individually."""
    cfg = config or DedupeConfig()
    gate = guard or HoldoutGuard(split, frozen)
    findings: list[Finding] = []

    # 1. One conversation cannot exist in more than one partition.
    seen: dict[str, set[str]] = {}
    for conversation in conversations:
        try:
            partition = split.partition_of(conversation.family_id)
        except SchemaError:
            continue
        seen.setdefault(conversation.digest, set()).add(partition.value)
    straddlers = sorted(d for d, parts in seen.items() if len(parts) > 1)
    findings.append(Finding(
        check="single_partition_per_conversation",
        verdict=Verdict.FAIL if straddlers else Verdict.PASS,
        detail=(f"{len(straddlers)} conversation(s) appear in more than one partition"
                if straddlers else
                f"{len(seen)} conversation(s), each in exactly one partition"),
        subjects=tuple(straddlers[:16])))

    # 2. A duplicate family cannot straddle partitions. This is the check that makes dedupe
    #    load-bearing: a family is the unit of assignment precisely so that its members
    #    cannot be separated, and this verifies the property rather than assuming it.
    bad_families: list[str] = []
    for family in report.families:
        partitions = {split.assignments.get(family.family_id)}
        partitions.discard(None)
        if len(partitions) > 1:  # pragma: no cover - a family has one assignment by construction
            bad_families.append(family.family_id)
    member_partitions: dict[str, set[str]] = {}
    for family in report.families:
        assigned = split.assignments.get(family.family_id)
        if assigned is None:
            continue
        for member in family.member_digests:
            member_partitions.setdefault(member, set()).add(assigned)
    split_members = sorted(m for m, parts in member_partitions.items() if len(parts) > 1)
    offenders = sorted(set(bad_families) | set(split_members))
    findings.append(Finding(
        check="family_never_straddles_partitions",
        verdict=Verdict.FAIL if offenders else Verdict.PASS,
        detail=(f"{len(offenders)} family member(s) resolve to more than one partition"
                if offenders else
                f"{len(report.families)} famil(y/ies), each wholly inside one partition"),
        subjects=tuple(offenders[:16])))

    # 3. No pair at or above the configured near-duplicate threshold may cross the holdout
    #    boundary. Checked against the RECORDED EVIDENCE rather than recomputed, so the audit
    #    verifies the decision that was actually made.
    crossings: list[str] = []
    holdout_members = {
        member
        for family in report.families
        if split.assignments.get(family.family_id) == Partition.FROZEN_HOLDOUT.value
        for member in family.member_digests}
    for family in report.families:
        for item in family.evidence:
            if item.score < cfg.near_duplicate_block:
                continue
            left_in = item.left_digest in holdout_members
            right_in = item.right_digest in holdout_members
            if left_in != right_in:
                crossings.append(f"{item.left_digest[:12]}~{item.right_digest[:12]}")
    findings.append(Finding(
        check="near_duplicate_never_crosses_into_holdout",
        verdict=Verdict.FAIL if crossings else Verdict.PASS,
        detail=(f"{len(crossings)} near-duplicate pair(s) at or above "
                f"{cfg.near_duplicate_block} straddle the holdout boundary" if crossings else
                f"no pair at or above {cfg.near_duplicate_block} crosses the boundary"),
        subjects=tuple(sorted(crossings)[:16])))

    # 4. The guard refuses every holdout family. Verified by ASKING it, not by reading this
    #    module's own intent: the guard is the control, so the control is what gets tested.
    leaks = sorted(fid for fid in split.families_in(Partition.FROZEN_HOLDOUT)
                   if gate.permit(fid))
    findings.append(Finding(
        check="holdout_reachable_only_through_guard",
        verdict=Verdict.FAIL if leaks else Verdict.PASS,
        detail=(f"the guard PERMITTED {len(leaks)} holdout famil(y/ies)" if leaks else
                f"the guard refused all {gate.holdout_family_count} holdout famil(y/ies)"),
        subjects=tuple(leaks[:16])))

    # 5. Every conversation has a family. An ungrouped item is the precondition for every
    #    other leak, because nothing downstream can place it.
    ungrouped = sorted(c.digest for c in conversations if not c.family_id)
    findings.append(Finding(
        check="every_item_has_a_family",
        verdict=Verdict.FAIL if ungrouped else Verdict.PASS,
        detail=(f"{len(ungrouped)} conversation(s) carry no family_id" if ungrouped else
                f"all {len(conversations)} conversation(s) are grouped"),
        subjects=tuple(ungrouped[:16])))

    # 6. EXPORT_SAFE must be explicit. Any turn still UNKNOWN is a record without a privacy
    #    decision, which §4 forbids; any turn claiming EXPORT_SAFE with a positive finding
    #    would already have failed PrivacyAssessment.validated(), so what is checked here is
    #    the ABSENCE of a decision.
    undecided = sorted(
        c.digest for c in conversations
        if any(t.privacy.export_status is ExportStatus.EXPORT_UNKNOWN for t in c.turns))
    findings.append(Finding(
        check="export_safe_is_explicit",
        verdict=Verdict.FAIL if undecided else Verdict.PASS,
        detail=(f"{len(undecided)} conversation(s) hold a turn whose export status is still "
                f"UNKNOWN; an unclassified record has no privacy decision (§4)"
                if undecided else
                f"every turn in {len(conversations)} conversation(s) carries an explicit "
                f"export status"),
        subjects=tuple(undecided[:16])))

    # 7. The frozen record and the plan must agree. When there is no frozen record the check
    #    is UNAVAILABLE rather than PASS: "nothing has been frozen yet" is a real state, and
    #    reporting it as a pass would let an unfrozen corpus read as a protected one.
    if frozen is None:
        findings.append(Finding(
            check="frozen_record_matches_plan", verdict=Verdict.UNAVAILABLE,
            detail="no frozen holdout record exists yet, so agreement cannot be verified; "
                   "this is reported as UNAVAILABLE, never as a pass"))
    else:
        planned = set(split.families_in(Partition.FROZEN_HOLDOUT))
        recorded = set(frozen.family_ids)
        drift = sorted(planned ^ recorded)
        findings.append(Finding(
            check="frozen_record_matches_plan",
            verdict=Verdict.FAIL if drift else Verdict.PASS,
            detail=(f"{len(drift)} famil(y/ies) differ between the plan and the frozen record"
                    if drift else
                    f"the plan and the frozen record name the same {len(recorded)} famil(y/ies)"),
            subjects=tuple(d[:64] for d in drift[:16])))

    return LeakageAudit(findings=tuple(findings), checks_expected=EXPECTED_CHECKS)


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {"firewall_version": FIREWALL_VERSION, "checks": list(EXPECTED_CHECKS),
            "unavailable_is_failure": True,
            "note": "the guard exposes no method that returns frozen-holdout content; the "
                    "frozen partition is reachable only as a set of family ids"}


__all__ = [
    "EXPECTED_CHECKS", "FIREWALL_VERSION", "Finding", "HoldoutAccessDenied", "HoldoutGuard",
    "LeakageAudit", "LeakageError", "Verdict", "audit", "versions",
]
