"""training_gym/datasets/leakage.py — V69 M62 S2d: proving the held-out set is held out.

WHY THIS EXISTS
---------------
Every other control in this milestone protects one record. This one protects the
*measurement*. A contaminated evaluation set does not fail loudly — it reports a better
number, which is precisely the outcome everybody involved was hoping for, so nobody looks
again. That asymmetry is the entire reason this module fails closed.

SIXTEEN CHECKS, IN FOUR FAMILIES
--------------------------------
Identity — the same record on both sides:

  1. candidate hash        2. prompt hash          3. target hash
  4. normalized prompt     5. normalized target    6. task hash

Lineage — different records built on the same thing:

  7. fixture digest overlap        8. lineage-group overlap
  9. task-template overlap        10. generated-variant parent overlap

Similarity — different text carrying the same example:

  11. character n-gram         12. token shingle

Exposure — the answer reaching somewhere it should not:

  13. hidden expected answer    14. teacher-packet exposure
  15. evaluation-only contamination   16. preference-pair contamination

FIVE VERDICTS, AND THE ONE THAT MATTERS
---------------------------------------
``CLEAN`` / ``WARNING`` / ``BLOCKED`` / ``INSUFFICIENT_EVIDENCE`` / ``ERROR``.

``INSUFFICIENT_EVIDENCE`` is the load-bearing one. It is what the analyzer returns when a
mandatory check could not run — an unavailable semantic backend, a comparison budget
exhausted, a malformed candidate that the identity index could not include. It is not a
softer ``CLEAN``; :meth:`LeakageReport.blocks_finalization` treats it exactly as hard as
``BLOCKED``, because "we did not look" and "we looked and it was fine" must never be the
same value in a field a build script reads.

OPTIONAL SEMANTICS, MANDATORY HONESTY
-------------------------------------
A semantic backend may be supplied. None is bundled, nothing is downloaded, and no
network is contacted. When a policy requires semantic checking and no backend is present,
the result is ``INSUFFICIENT_EVIDENCE`` — never ``CLEAN``. And the absence of a backend
never suppresses the fifteen checks that do not need one.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ..schemas import (
    SCHEMA_KEY,
    SCHEMA_VERSION,
    SchemaError,
    Severity,
    sha256_obj,
    sha256_text,
    short,
)
from .candidate import DatasetCandidate, DatasetSplit
from .similarity import (
    BLOCK_THRESHOLD,
    DEFAULT_MAX_COMPARISONS,
    SIMILARITY_VERSION,
    WARN_THRESHOLD,
    compare_groups,
    normalized_key,
    signature,
)
from .split import SplitPlan, leakage_group_key

#: Bump when a check or a threshold below changes. Recorded in every report.
LEAKAGE_VERSION = "m62.leakage.1"


class LeakageError(SchemaError):
    """The analysis itself could not be performed. Distinct from finding leakage."""


class LeakageVerdict(str, Enum):
    """The five possible outcomes. Only one of them is a pass."""

    CLEAN = "clean"
    WARNING = "warning"
    BLOCKED = "blocked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"

    @property
    def permits_finalization(self) -> bool:
        """Only ``CLEAN`` and ``WARNING`` allow a dataset version to be written.

        ``INSUFFICIENT_EVIDENCE`` is grouped with ``BLOCKED`` deliberately: a check that
        could not run proved nothing, and treating it as a pass is how an unavailable
        backend becomes a clean bill of health.
        """
        return self in (LeakageVerdict.CLEAN, LeakageVerdict.WARNING)


#: The checks that must produce an answer before a dataset may be finalized. A check
#: outside this set that cannot run downgrades the verdict to WARNING instead.
MANDATORY_CHECKS: frozenset[str] = frozenset({
    "exact_candidate_hash", "exact_prompt_hash", "exact_target_hash",
    "normalized_prompt_hash", "normalized_target_hash", "task_hash",
    "fixture_overlap", "lineage_overlap", "parent_overlap", "char_ngram_similarity",
    "token_shingle_similarity", "evaluation_only_contamination",
})


@dataclass(frozen=True)
class LeakageFinding:
    """One collision, naming both sides. Evidence, not a summary."""

    check: str
    verdict: LeakageVerdict
    severity: Severity
    left_id: str
    right_id: str
    left_split: str
    right_split: str
    detail: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"check": self.check, "verdict": self.verdict.value,
                "severity": self.severity.value, "left_id": self.left_id,
                "right_id": self.right_id, "left_split": self.left_split,
                "right_split": self.right_split, "detail": self.detail,
                "score": round(self.score, 6)}


@dataclass(frozen=True)
class LeakagePolicy:
    """What this analysis is required to prove before a dataset may be written."""

    require_semantic: bool = False
    block_threshold: float = BLOCK_THRESHOLD
    warn_threshold: float = WARN_THRESHOLD
    max_comparisons: int = DEFAULT_MAX_COMPARISONS
    #: Within-train duplication is a quality problem, not a contamination one.
    block_within_split_duplicates: bool = False

    def to_dict(self) -> dict:
        return {"require_semantic": self.require_semantic,
                "block_threshold": self.block_threshold,
                "warn_threshold": self.warn_threshold,
                "max_comparisons": self.max_comparisons,
                "block_within_split_duplicates": self.block_within_split_duplicates}


@dataclass(frozen=True)
class LeakageReport:
    """The complete verdict, with every check that ran and every one that did not."""

    version: str
    policy: LeakagePolicy
    verdict: LeakageVerdict
    findings: tuple[LeakageFinding, ...] = ()
    checks_run: tuple[str, ...] = ()
    checks_unavailable: tuple[str, ...] = ()
    comparisons: int = 0
    ceiling_reached: bool = False
    candidate_count: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocks_finalization(self) -> bool:
        return not self.verdict.permits_finalization

    @property
    def blocking_findings(self) -> tuple[LeakageFinding, ...]:
        return tuple(f for f in self.findings
                     if f.verdict is LeakageVerdict.BLOCKED)

    def to_dict(self) -> dict:
        return {SCHEMA_KEY: SCHEMA_VERSION, "version": self.version,
                "similarity_version": SIMILARITY_VERSION,
                "policy": self.policy.to_dict(), "verdict": self.verdict.value,
                "findings": [f.to_dict() for f in self.findings],
                "checks_run": list(self.checks_run),
                "checks_unavailable": list(self.checks_unavailable),
                "comparisons": self.comparisons,
                "ceiling_reached": self.ceiling_reached,
                "candidate_count": self.candidate_count,
                "blocks_finalization": self.blocks_finalization,
                "notes": list(self.notes)}

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


def _side(split: DatasetSplit) -> str:
    """Which half of the dataset a split belongs to, for contamination purposes."""
    if split in (DatasetSplit.TRAIN, DatasetSplit.VALIDATION):
        return "train"
    if split is DatasetSplit.QUARANTINE:
        return "quarantine"
    return "held_out"


def _cross_side(a: DatasetSplit, b: DatasetSplit) -> bool:
    """True when a collision between these two splits is contamination.

    Train-versus-held-out is the case that invalidates a measurement. Train-versus-train
    is duplication, which is a quality issue and reported as one.
    """
    return {_side(a), _side(b)} == {"train", "held_out"}


class LeakageAnalyzer:
    """The sixteen checks, run over one split plan. Deterministic and offline.

    *semantic_backend*, if supplied, must expose ``similar(left: str, right: str) ->
    float`` and ``identity() -> Mapping``. Nothing here will import, download or
    construct one; passing ``None`` is the normal case and is reported honestly.
    """

    def __init__(self, *, policy: LeakagePolicy | None = None,
                 semantic_backend: object | None = None) -> None:
        self.policy = policy or LeakagePolicy()
        self.semantic_backend = semantic_backend

    # -- entry point -------------------------------------------------------------
    def analyze(self, candidates: Iterable[DatasetCandidate], *,
                plan: SplitPlan) -> LeakageReport:
        records = {c.candidate_id: c for c in candidates}
        if not records:
            return LeakageReport(
                version=LEAKAGE_VERSION, policy=self.policy,
                verdict=LeakageVerdict.INSUFFICIENT_EVIDENCE,
                notes=("no candidates were supplied; an empty analysis proves nothing "
                       "about a dataset and must not read as a clean one",))

        placed = {cid: DatasetSplit(name) for cid, name in plan.assignments.items()
                  if cid in records}
        missing = sorted(set(plan.assignments) - set(records))
        if missing:
            return LeakageReport(
                version=LEAKAGE_VERSION, policy=self.policy,
                verdict=LeakageVerdict.ERROR, candidate_count=len(records),
                notes=(f"the split plan names {len(missing)} candidate(s) that were not "
                       f"supplied ({[short(m, 16) for m in missing[:5]]}); the index "
                       f"would be incomplete and its verdict meaningless",))

        findings: list[LeakageFinding] = []
        checks_run: list[str] = []
        unavailable: list[str] = []

        findings.extend(self._identity_checks(records, placed, checks_run))
        findings.extend(self._lineage_checks(records, placed, checks_run))
        similar, comparisons, ceiling = self._similarity_checks(records, placed,
                                                                checks_run)
        findings.extend(similar)
        findings.extend(self._exposure_checks(records, placed, checks_run))
        findings.extend(self._semantic_checks(records, placed, checks_run, unavailable))

        return self._verdict(findings, checks_run, unavailable, comparisons, ceiling,
                             len(records))

    # -- 1..6: identity ----------------------------------------------------------
    def _identity_checks(self, records: Mapping[str, DatasetCandidate],
                         placed: Mapping[str, DatasetSplit],
                         checks_run: list[str]) -> list[LeakageFinding]:
        indexes: dict[str, dict[str, list[str]]] = {
            "exact_candidate_hash": {}, "exact_prompt_hash": {},
            "exact_target_hash": {}, "normalized_prompt_hash": {},
            "normalized_target_hash": {}, "task_hash": {},
        }
        for cid, candidate in sorted(records.items()):
            family = candidate.task_family
            keys = {
                "exact_candidate_hash": candidate.content_key(),
                "exact_prompt_hash": sha256_text(candidate.user_prompt),
                "exact_target_hash": sha256_text(candidate.target_text),
                "normalized_prompt_hash": normalized_key(candidate.user_prompt,
                                                         family=family),
                "normalized_target_hash": normalized_key(candidate.target_text,
                                                         family=family),
                "task_hash": candidate.task_hash,
            }
            for check, key in keys.items():
                indexes[check].setdefault(key, []).append(cid)
        checks_run.extend(sorted(indexes))
        return self._collisions_from(indexes, placed, detail="identical")

    # -- 7..10: lineage ----------------------------------------------------------
    def _lineage_checks(self, records: Mapping[str, DatasetCandidate],
                        placed: Mapping[str, DatasetSplit],
                        checks_run: list[str]) -> list[LeakageFinding]:
        indexes: dict[str, dict[str, list[str]]] = {
            "fixture_overlap": {}, "lineage_overlap": {}, "parent_overlap": {},
            "template_overlap": {},
        }
        for cid, candidate in sorted(records.items()):
            for digest in candidate.input_fixture_hashes:
                indexes["fixture_overlap"].setdefault(digest, []).append(cid)
            indexes["lineage_overlap"].setdefault(
                leakage_group_key(candidate), []).append(cid)
            if candidate.parent_task_hash:
                indexes["parent_overlap"].setdefault(
                    candidate.parent_task_hash, []).append(cid)
            template = str(candidate.provenance.get("upstream_ref", "")).strip()
            if template:
                indexes["template_overlap"].setdefault(template, []).append(cid)
        checks_run.extend(sorted(indexes))
        return self._collisions_from(indexes, placed, detail="shared lineage")

    def _collisions_from(self, indexes: Mapping[str, Mapping[str, Sequence[str]]],
                         placed: Mapping[str, DatasetSplit],
                         *, detail: str) -> list[LeakageFinding]:
        """Turn each index into findings for every pair that spans two splits."""
        findings: list[LeakageFinding] = []
        for check, index in sorted(indexes.items()):
            for key, ids in sorted(index.items()):
                unique = sorted(set(ids))
                if len(unique) < 2:
                    continue
                for i, left in enumerate(unique):
                    for right in unique[i + 1:]:
                        left_split = placed.get(left)
                        right_split = placed.get(right)
                        if left_split is None or right_split is None:
                            continue
                        if left_split is right_split:
                            if not self.policy.block_within_split_duplicates:
                                findings.append(LeakageFinding(
                                    check=check, verdict=LeakageVerdict.WARNING,
                                    severity=Severity.LOW, left_id=left,
                                    right_id=right, left_split=left_split.value,
                                    right_split=right_split.value,
                                    detail=f"{detail} within one split ({short(key)})",
                                    score=1.0))
                                continue
                        cross = _cross_side(left_split, right_split)
                        findings.append(LeakageFinding(
                            check=check,
                            verdict=(LeakageVerdict.BLOCKED if cross
                                     else LeakageVerdict.WARNING),
                            severity=(Severity.BLOCKING if cross else Severity.MEDIUM),
                            left_id=left, right_id=right,
                            left_split=left_split.value,
                            right_split=right_split.value,
                            detail=f"{detail} ({short(key)})", score=1.0))
        return findings

    # -- 11..12: similarity ------------------------------------------------------
    def _similarity_checks(self, records: Mapping[str, DatasetCandidate],
                           placed: Mapping[str, DatasetSplit],
                           checks_run: list[str]
                           ) -> tuple[list[LeakageFinding], int, bool]:
        train = [cid for cid, s in sorted(placed.items()) if _side(s) == "train"]
        held = [cid for cid, s in sorted(placed.items()) if _side(s) == "held_out"]
        checks_run.extend(("char_ngram_similarity", "token_shingle_similarity"))
        if not train or not held:
            return [], 0, False

        def sigs(ids: Sequence[str]) -> list:
            return [signature(cid, records[cid].user_prompt + "\n"
                              + records[cid].target_text,
                              family=records[cid].task_family) for cid in ids]

        result = compare_groups(sigs(train), sigs(held),
                                threshold=self.policy.warn_threshold,
                                max_comparisons=self.policy.max_comparisons)
        findings: list[LeakageFinding] = []
        for hit in result.hits:
            blocking = hit.score >= self.policy.block_threshold
            check = ("char_ngram_similarity" if hit.char_score >= hit.token_score
                     else "token_shingle_similarity")
            findings.append(LeakageFinding(
                check=check,
                verdict=(LeakageVerdict.BLOCKED if blocking
                         else LeakageVerdict.WARNING),
                severity=(Severity.BLOCKING if blocking else Severity.MEDIUM),
                left_id=hit.left_key, right_id=hit.right_key,
                left_split=placed[hit.left_key].value,
                right_split=placed[hit.right_key].value,
                detail=f"near-duplicate across the train/held-out boundary "
                       f"(char {hit.char_score:.3f}, token {hit.token_score:.3f})",
                score=hit.score))
        return findings, result.comparisons, result.ceiling_reached

    # -- 13..16: exposure --------------------------------------------------------
    def _exposure_checks(self, records: Mapping[str, DatasetCandidate],
                         placed: Mapping[str, DatasetSplit],
                         checks_run: list[str]) -> list[LeakageFinding]:
        checks_run.extend(("hidden_answer_exposure", "teacher_packet_exposure",
                           "evaluation_only_contamination",
                           "preference_pair_contamination"))
        findings: list[LeakageFinding] = []
        held_targets = {sha256_text(records[cid].target_text): cid
                        for cid, s in sorted(placed.items()) if _side(s) == "held_out"}
        for cid, split in sorted(placed.items()):
            candidate = records[cid]
            if _side(split) != "train":
                continue
            # 13 — a held-out expected answer appearing inside a TRAIN prompt is the
            # answer key travelling by a route no hash comparison of targets would see.
            for digest, held_id in sorted(held_targets.items()):
                held_target = records[held_id].target_text
                if len(held_target) >= 16 and held_target in candidate.user_prompt:
                    findings.append(LeakageFinding(
                        check="hidden_answer_exposure", verdict=LeakageVerdict.BLOCKED,
                        severity=Severity.BLOCKING, left_id=cid, right_id=held_id,
                        left_split=split.value, right_split=placed[held_id].value,
                        detail=f"a held-out target ({short(digest)}) appears verbatim "
                               f"inside this training prompt", score=1.0))
            # 15 — an evaluation-only record must never have reached a trainable split.
            if candidate.evaluation_only or not candidate.dataset_eligible:
                findings.append(LeakageFinding(
                    check="evaluation_only_contamination",
                    verdict=LeakageVerdict.BLOCKED, severity=Severity.BLOCKING,
                    left_id=cid, right_id=cid, left_split=split.value,
                    right_split=split.value,
                    detail="an evaluation-only or dataset-ineligible record reached a "
                           "trainable split", score=1.0))
            # 16 — preference material carries a rejected answer; it never trains as a
            # positive target.
            if candidate.preference_eligible and split is DatasetSplit.TRAIN:
                findings.append(LeakageFinding(
                    check="preference_pair_contamination",
                    verdict=LeakageVerdict.WARNING, severity=Severity.MEDIUM,
                    left_id=cid, right_id=cid, left_split=split.value,
                    right_split=split.value,
                    detail="a preference-eligible record is also in the SFT train "
                           "split; confirm the rejected half is not exported here",
                    score=0.5))
        # 14 — a held-out record whose material may be sent to a teacher is one packet
        # away from having its expected answer read by a cloud model. That is not a
        # duplicate of any other check: nothing so far looks at the EXPORT permission of
        # material that is supposed to be measured against.
        for cid, split in sorted(placed.items()):
            candidate = records[cid]
            if _side(split) == "held_out" and candidate.sensitivity.exportable_to_teacher:
                findings.append(LeakageFinding(
                    check="teacher_packet_exposure", verdict=LeakageVerdict.WARNING,
                    severity=Severity.MEDIUM, left_id=cid, right_id=cid,
                    left_split=split.value, right_split=split.value,
                    detail=f"held-out material is marked {candidate.sensitivity.value}, "
                           f"which is exportable to a teacher; its expected answer must "
                           f"never be placed in a packet", score=0.5))
        return findings

    # -- optional semantics ------------------------------------------------------
    def _semantic_checks(self, records: Mapping[str, DatasetCandidate],
                         placed: Mapping[str, DatasetSplit], checks_run: list[str],
                         unavailable: list[str]) -> list[LeakageFinding]:
        backend = self.semantic_backend
        if backend is None:
            unavailable.append("semantic_similarity")
            return []
        similar = getattr(backend, "similar", None)
        if not callable(similar):
            unavailable.append("semantic_similarity")
            return []
        checks_run.append("semantic_similarity")
        train = [cid for cid, s in sorted(placed.items()) if _side(s) == "train"]
        held = [cid for cid, s in sorted(placed.items()) if _side(s) == "held_out"]
        findings: list[LeakageFinding] = []
        budget = self.policy.max_comparisons
        for left in train:
            for right in held:
                if budget <= 0:
                    unavailable.append("semantic_similarity")
                    return findings
                budget -= 1
                score = float(similar(records[left].user_prompt,
                                      records[right].user_prompt))
                if score >= self.policy.block_threshold:
                    findings.append(LeakageFinding(
                        check="semantic_similarity", verdict=LeakageVerdict.BLOCKED,
                        severity=Severity.BLOCKING, left_id=left, right_id=right,
                        left_split=placed[left].value, right_split=placed[right].value,
                        detail="semantically equivalent across the train/held-out "
                               "boundary", score=score))
        return findings

    # -- the verdict -------------------------------------------------------------
    def _verdict(self, findings: Sequence[LeakageFinding], checks_run: Sequence[str],
                 unavailable: Sequence[str], comparisons: int, ceiling: bool,
                 count: int) -> LeakageReport:
        notes: list[str] = []
        missing_mandatory = sorted(MANDATORY_CHECKS - set(checks_run))
        verdict = LeakageVerdict.CLEAN

        if any(f.verdict is LeakageVerdict.BLOCKED for f in findings):
            verdict = LeakageVerdict.BLOCKED
        elif any(f.verdict is LeakageVerdict.WARNING for f in findings):
            verdict = LeakageVerdict.WARNING

        if ceiling:
            notes.append(
                f"the {self.policy.max_comparisons}-comparison ceiling was reached; "
                f"pairs beyond it were never examined and this analysis cannot claim "
                f"they are clean")
            if verdict is not LeakageVerdict.BLOCKED:
                verdict = LeakageVerdict.INSUFFICIENT_EVIDENCE
        if missing_mandatory:
            notes.append(f"mandatory check(s) did not run: {missing_mandatory}")
            if verdict is not LeakageVerdict.BLOCKED:
                verdict = LeakageVerdict.INSUFFICIENT_EVIDENCE
        if self.policy.require_semantic and "semantic_similarity" in unavailable:
            notes.append(
                "the policy requires a semantic check and no embedding backend is "
                "available; nothing was downloaded and no network was contacted, so the "
                "honest result is insufficient evidence rather than clean")
            if verdict is not LeakageVerdict.BLOCKED:
                verdict = LeakageVerdict.INSUFFICIENT_EVIDENCE

        return LeakageReport(
            version=LEAKAGE_VERSION, policy=self.policy, verdict=verdict,
            findings=tuple(sorted(findings, key=lambda f: (f.check, f.left_id,
                                                           f.right_id))),
            checks_run=tuple(sorted(set(checks_run))),
            checks_unavailable=tuple(sorted(set(unavailable))),
            comparisons=comparisons, ceiling_reached=ceiling, candidate_count=count,
            notes=tuple(notes))


__all__ = [
    "LEAKAGE_VERSION", "MANDATORY_CHECKS", "LeakageAnalyzer", "LeakageError",
    "LeakageFinding", "LeakagePolicy", "LeakageReport", "LeakageVerdict",
]
