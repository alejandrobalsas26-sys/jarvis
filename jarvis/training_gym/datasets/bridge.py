"""training_gym/datasets/bridge.py — V69 M62 S2d: M62 datasets into the M16 pipeline.

WHY A BRIDGE AND NOT A REPLACEMENT
----------------------------------
:mod:`core.dataset_pipeline` (M16) already owns what a *written dataset* is for this
application: ``TrainingExample``, ``DatasetManifest``, ``write_dataset`` — and
:meth:`core.training_pipeline.TrainingPipeline.verify_dataset` gates every experiment on
exactly that shape. M62 owns something narrower and stricter: how an attempt becomes an
approved training example in the first place.

Two writers for one artefact is the failure this module exists to avoid. So the bridge
**translates and never writes**. It returns ``TrainingExample`` objects; the caller hands
them to M16's own ``write_dataset``. There is deliberately no ``bridge_and_write``: the
moment this module opened a dataset directory there would be two authorities over the
same bytes, and the one that drifted would be the one nobody was looking at.

THE STRICTER READING ALWAYS WINS
--------------------------------
Where the two vocabularies disagree, the bridge picks the interpretation that grants
less:

  * ``VERIFIED_STUDENT_OUTPUT`` and ``TEACHER_PROPOSED_HUMAN_APPROVED`` both map to M16's
    ``MODEL_GENERATED``, whose ``is_ground_truth`` is ``False``. A model wrote those bytes.
    A human approved them — and approval is carried by the STATUS and the review record,
    which is where M16 keeps it, not by relabelling the provenance as human-authored.
  * only ``PROMOTED`` maps to ``APPROVED``. Every other M62 state raises rather than
    mapping to ``PENDING_REVIEW``, because M16's ``approve()`` would then be one call away
    from turning a rejected candidate into training data.
  * a field M62 carries that M16 cannot represent — a structured tool call — is a
    refusal, not a silent drop. An example whose tool call vanished on the way through is
    an example that teaches prose where a schema was required.

WHAT IS CARRIED ACROSS
----------------------
``source_refs`` retains the candidate digest, the dataset id and version, and the dataset
manifest hash, so an M16 row can be traced back to the immutable M62 version that
produced it. The review block records the M62 approval digest and the bridge version. No
timestamp is invented: M62 records are stamped by their author, and a clock read here
would make the output non-deterministic for no benefit.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..schemas import SchemaError, sha256_obj
from .candidate import CandidateState, DatasetCandidate, DatasetSplit, TargetSource
from .manifests import (
    RevocationSnapshot,
    load_manifest,
    read_shard,
    reviewer_safe_id,
    shard_filename,
    version_dir,
)

#: Bump when the mapping below changes. Recorded on every bridged row.
BRIDGE_VERSION = "m62.bridge.1"

#: The only split that becomes M16 training data.
BRIDGE_SOURCE_SPLIT = DatasetSplit.TRAIN


class BridgeError(SchemaError):
    """A record could not be represented in M16 without weakening it. Never coerced."""


#: M62 target source -> M16 provenance, choosing the reading that grants less trust.
#:
#: ``ExampleProvenance.HUMAN`` is used only where a named human personally wrote or
#: altered the bytes. Everything a model produced stays ``MODEL_GENERATED`` even though a
#: human approved it, because M16's provenance answers "who wrote this" and its
#: ``is_ground_truth`` property is read by gates that must not be told a model is a human.
_PROVENANCE_MAP: dict[TargetSource, str] = {
    TargetSource.HUMAN_AUTHORED: "human",
    TargetSource.HUMAN_EDITED: "human",
    TargetSource.VERIFIED_STUDENT_OUTPUT: "model_generated",
    TargetSource.TEACHER_PROPOSED_HUMAN_APPROVED: "model_generated",
}


def _m16():
    """Import M16 lazily.

    The gym's vocabulary must stay importable where ``core`` is not on the path — the
    training virtualenv and a base text-mode install both lack it — so the dependency
    lives inside the one function that genuinely needs it.
    """
    try:
        from core.dataset_pipeline import (
            CandidateStatus,
            ExampleProvenance,
            GateResult,
            TrainingCandidate,
            TrainingExample,
        )
    except Exception as exc:  # noqa: BLE001 — absence must be explicit, never "clean"
        raise BridgeError(
            f"bridge: the M16 dataset pipeline is not importable ({exc}); refusing to "
            f"invent a compatible shape rather than using the one the application "
            f"already gates on") from None
    return (CandidateStatus, ExampleProvenance, GateResult, TrainingCandidate,
            TrainingExample)


def bridge_blockers(candidate: DatasetCandidate, *, split: DatasetSplit,
                    revocation: RevocationSnapshot) -> tuple[str, ...]:
    """Every reason this candidate may not cross into M16. Empty tuple = representable."""
    problems: list[str] = []
    if candidate.state is not CandidateState.PROMOTED:
        problems.append(
            f"state is {candidate.state.value}; only a promoted record maps to M16 "
            f"APPROVED, and mapping anything else would leave it one approve() call "
            f"away from becoming training data")
    if split is not BRIDGE_SOURCE_SPLIT:
        problems.append(f"split is {split.value}; only train material becomes M16 "
                        f"training data")
    if revocation.is_revoked(candidate):
        problems.append("the record has been revoked")
    if candidate.evaluation_only or not candidate.dataset_eligible:
        problems.append("the record is evaluation-only or dataset-ineligible")
    if not candidate.sensitivity.dataset_eligible:
        problems.append(f"sensitivity {candidate.sensitivity.value} is never trainable")
    if candidate.tool_calls:
        problems.append(
            f"the record carries {len(candidate.tool_calls)} structured tool call(s), "
            f"which M16's TrainingExample cannot represent; failing closed rather than "
            f"dropping them, because an example whose tool call vanished teaches prose "
            f"where a schema was required")
    if candidate.target_source not in _PROVENANCE_MAP:  # pragma: no cover — exhaustive
        problems.append(f"target source {candidate.target_source.value} has no M16 "
                        f"provenance")
    if not candidate.user_prompt.strip() or not candidate.target_text.strip():
        problems.append("an empty prompt or target is refused by M16's own gate")
    return tuple(problems)


def to_training_example(candidate: DatasetCandidate, *, split: DatasetSplit,
                        revocation: RevocationSnapshot, dataset_id: str,
                        dataset_version: str, dataset_manifest_hash: str):
    """One M62 candidate as an M16 ``TrainingExample``, or raise with every blocker.

    The ``gates`` tuple is not decoration and is not invented: each entry names a control
    that ACTUALLY ran in M62 and passed for this record. Writing a synthetic passing gate
    for a check nobody performed would be the exact lie M16's audit trail exists to
    prevent.
    """
    problems = bridge_blockers(candidate, split=split, revocation=revocation)
    if problems:
        raise BridgeError(f"bridge: {candidate.candidate_id} cannot cross into M16 — "
                          + "; ".join(problems))
    (CandidateStatus, ExampleProvenance, GateResult, TrainingCandidate,
     TrainingExample) = _m16()

    domain = candidate.task_family.value
    m16_candidate = TrainingCandidate(
        prompt=candidate.user_prompt, ideal_output=candidate.target_text, domain=domain,
        provenance=ExampleProvenance(_PROVENANCE_MAP[candidate.target_source]))
    gates = (
        GateResult("m62_human_review", True, reason=f"dataset review "
                                                    f"{candidate.approval_hash[:12]}"),
        GateResult("m62_candidate_state", True, reason=candidate.state.value),
        GateResult("m62_split", True, reason=split.value),
        GateResult("m62_revocation", True, reason="not revoked at bridge time"),
        GateResult("m62_manifest", True,
                   reason=f"verified version {dataset_manifest_hash[:12]}"),
    )
    return TrainingExample(
        id=m16_candidate.content_key(),
        prompt=candidate.user_prompt,
        ideal_output=candidate.target_text,
        domain=domain,
        provenance=_PROVENANCE_MAP[candidate.target_source],
        status=CandidateStatus.APPROVED,
        gates=gates,
        source_refs=(
            f"m62:candidate:{candidate.candidate_hash()}",
            f"m62:dataset:{dataset_id}@{dataset_version}",
            f"m62:manifest:{dataset_manifest_hash}",
            f"m62:review:{candidate.approval_hash}",
        ),
        failure_ref=candidate.task_id,
        failure_reason="",
        tags=tuple(sorted({f"task_family:{domain}",
                           f"target_source:{candidate.target_source.value}",
                           f"split:{split.value}",
                           f"bridge:{BRIDGE_VERSION}"})),
        review={"approver": reviewer_safe_id(candidate.approval_hash),
                "decision": "approved",
                "approved_at_utc": candidate.created_at_utc,
                "m62_review_hash": candidate.approval_hash,
                "m62_candidate_hash": candidate.candidate_hash(),
                "bridge_version": BRIDGE_VERSION},
        version=dataset_version,
        # No clock is read. M62 records are stamped by their author, and inventing a
        # timestamp here would make the bridge non-deterministic for no benefit.
        created_ts=0.0,
    )


@dataclass(frozen=True)
class BridgeReport:
    """What crossed, what did not, and the provenance that came with it."""

    dataset_id: str
    dataset_version: str
    dataset_manifest_hash: str
    example_count: int
    example_ids: tuple[str, ...]
    excluded: tuple[dict, ...] = ()
    bridge_version: str = BRIDGE_VERSION

    def to_dict(self) -> dict:
        return {"bridge_version": self.bridge_version, "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
                "dataset_manifest_hash": self.dataset_manifest_hash,
                "example_count": self.example_count,
                "example_ids": list(self.example_ids),
                "excluded": [dict(sorted(e.items())) for e in self.excluded],
                "writes_a_dataset": False}

    def report_hash(self) -> str:
        return sha256_obj(self.to_dict())


def bridge_dataset_version(*, root: str | Path, dataset_id: str, dataset_version: str,
                           revocation: RevocationSnapshot,
                           strict: bool = True) -> tuple[list, BridgeReport]:
    """Translate one verified dataset version's TRAIN shard into M16 examples.

    Returns ``(examples, report)`` and writes nothing. Hand the examples to M16's own
    ``write_dataset`` — this module never opens a dataset directory for writing, because
    two authorities over one artefact is the failure it exists to prevent.

    With *strict* (the default) a single unrepresentable record fails the whole bridge.
    Set it to ``False`` only when the caller intends to promote a subset and will read
    ``report.excluded``; the exclusions are always named either way.
    """
    manifest = load_manifest(root=root, dataset_id=dataset_id,
                             dataset_version=dataset_version)
    shard = manifest.shard_for(BRIDGE_SOURCE_SPLIT)
    if shard is None:
        raise BridgeError(f"bridge: dataset {dataset_id}/{dataset_version} has no train "
                          f"shard")
    records = read_shard(version_dir(root, dataset_id, dataset_version)
                         / shard_filename(BRIDGE_SOURCE_SPLIT))
    by_id = {row.candidate_id: row for row in manifest.candidates}

    examples: list = []
    excluded: list[dict] = []
    for candidate in sorted(records, key=lambda c: c.candidate_id):
        row = by_id.get(candidate.candidate_id)
        split = row.split if row is not None else BRIDGE_SOURCE_SPLIT
        problems = bridge_blockers(candidate, split=split, revocation=revocation)
        if problems:
            if strict:
                raise BridgeError(f"bridge: {candidate.candidate_id} cannot cross into "
                                  f"M16 — " + "; ".join(problems))
            excluded.append({"candidate_id": candidate.candidate_id,
                             "reasons": list(problems)})
            continue
        examples.append(to_training_example(
            candidate, split=split, revocation=revocation, dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_manifest_hash=manifest.manifest_hash()))

    if not examples:
        raise BridgeError(
            f"bridge: dataset {dataset_id}/{dataset_version} produced no M16 examples "
            f"(excluded: {[e['candidate_id'] for e in excluded]}); refusing to hand an "
            f"empty list to a dataset writer")
    report = BridgeReport(
        dataset_id=dataset_id, dataset_version=dataset_version,
        dataset_manifest_hash=manifest.manifest_hash(), example_count=len(examples),
        example_ids=tuple(e.id for e in examples),
        excluded=tuple(sorted(excluded, key=lambda e: e["candidate_id"])))
    return examples, report


def verify_bridged(examples: Sequence[object]) -> tuple[str, ...]:
    """Re-check bridged examples against M16's own expectations. Empty tuple = clean.

    Run after the bridge and before the write. It asserts the properties M16's
    ``verify_dataset`` will assert later, so a mismatch is found here — where the M62
    record is still in hand and the diagnosis is possible — rather than at experiment
    time as an opaque gate failure.
    """
    CandidateStatus = _m16()[0]
    problems: list[str] = []
    seen: set[str] = set()
    for example in examples:
        identifier = getattr(example, "id", "")
        if identifier in seen:
            problems.append(f"{identifier}: appears twice")
        seen.add(identifier)
        if getattr(example, "status", None) is not CandidateStatus.APPROVED:
            problems.append(f"{identifier}: is not APPROVED")
        if not str(getattr(example, "prompt", "")).strip():
            problems.append(f"{identifier}: empty prompt")
        if not str(getattr(example, "ideal_output", "")).strip():
            problems.append(f"{identifier}: empty target")
        refs = tuple(getattr(example, "source_refs", ()))
        if not any(r.startswith("m62:manifest:") for r in refs):
            problems.append(f"{identifier}: carries no M62 manifest provenance")
        if not any(r.startswith("m62:review:") for r in refs):
            problems.append(f"{identifier}: carries no M62 approval provenance")
    return tuple(problems)


__all__ = [
    "BRIDGE_SOURCE_SPLIT", "BRIDGE_VERSION", "BridgeError", "BridgeReport",
    "bridge_blockers", "bridge_dataset_version", "to_training_example",
    "verify_bridged",
]
