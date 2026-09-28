"""reasoning_distillation/pipeline.py — V69 M67A: the stages, in the only order that is safe.

THE ORDER IS THE CONTROL
------------------------
Every stage in this file could be called directly. The reason they are composed here instead is
that two of the orderings are load-bearing and neither is obvious from the individual signatures:

  * **dedupe before split.** A split over ungrouped conversations puts two copies of one problem on
    opposite sides of the holdout boundary, and the resulting measurement scores memorisation. §8's
    title is literally "deduplication before learning".
  * **split before distil.** §9: *split by conversation / problem family BEFORE model-assisted
    distillation.* If distillation runs first, then every prompt, threshold and taxonomy decision
    was made while holdout content was visible, and the holdout is spent before anyone used it.

:func:`run` is therefore the entrypoint, and it does not take a parameter that would let a caller
reorder those steps. A `--no-llm` flag changes whether a provider is consulted; nothing changes
whether the firewall is built before the distiller runs.

WHAT "IDEMPOTENT" MEANS AT THIS LEVEL
-------------------------------------
Re-running :func:`run` over the same inputs with the same config produces a
:class:`~reasoning_distillation.manifests.BuildManifest` with the same
``structural_digest`` — that is §20's reproducibility property, and
:func:`reasoning_distillation.manifests.compare` is how it is checked. Nothing is appended, no
counter advances, and the ``raw/`` store's content addressing means re-ingesting the same bytes
writes nothing new (§6).

WHAT THIS MODULE REFUSES TO DO
------------------------------
It will not distil when the leakage audit is unclean, and it will not silently proceed when the
split is not defensible. The first is a refusal; the second is a REPORTED shortfall that still
produces a corpus — and the difference is deliberate. A leak invalidates the experiment, so it
stops. An undersized holdout does not invalidate the development material, so the material is
built and the absence of a defensible holdout is stated (§9's *"if too little data exists for a
defensible holdout: say so"*).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from training_gym.schemas import SchemaError, SensitivityClass, sha256_obj

from . import dedupe as dedupe_stage
from . import holdout as holdout_stage
from . import skills as skills_stage
from . import splitting as splitting_stage
from .adapters import AdapterError
from .benchmark import BenchmarkHarness
from .config import DistillationConfig, default_config
from .critic import critique
from .distiller import DistillationProvider, DistillationResult, distil_corpus
from .manifests import BuildManifest, CorpusCounts, build_id_for
from .models import CanonicalConversation, Disposition
from .normalization import ingest_bytes, with_family
from .quality import QualityVerdict, disposition
from .skills import SkillStatus
from .storage import CorpusRoot

#: Files larger than this are refused rather than read. A historical chat export is prose; a
#: 200 MB file under a corpus root is a database, a model or a mistake, and reading it into memory
#: to discover that is the wrong way to find out.
MAX_SOURCE_BYTES = 32 * 1024 * 1024

#: Extensions the ingest stage will OPEN. An allowlist (§29): a corpus directory that happens to
#: contain a `.sqlite` or a `.pem` must not be swept into the pipeline because it was in the folder.
INGESTIBLE_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".markdown", ".json", ".jsonl")


class PipelineError(SchemaError):
    """A stage refused. Never a partially-built corpus presented as a whole one."""


@dataclass(frozen=True)
class IngestOutcome:
    """What the ingest stage read, and what it declined. Both halves are reported."""

    conversations: tuple[CanonicalConversation, ...] = ()
    #: ``(basename, reason)`` for every file not ingested. Basenames only, never full paths.
    skipped: tuple[tuple[str, str], ...] = ()
    source_file_hashes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "conversation_count": len(self.conversations),
            "turn_count": sum(c.turn_count for c in self.conversations),
            "source_file_count": len(set(self.source_file_hashes)),
            "skipped_count": len(self.skipped),
            "skipped": [{"file": name, "reason": reason[:320]}
                        for name, reason in sorted(self.skipped)],
            "source_file_hashes": sorted(set(self.source_file_hashes)),
        }


@dataclass(frozen=True)
class BuildOutcome:
    """Everything one build produced. Body-free in every field that leaves the process."""

    ingest: IngestOutcome
    dedupe_report: dedupe_stage.DedupeReport
    split: splitting_stage.SplitPlan
    frozen: splitting_stage.FrozenHoldout | None
    audit: holdout_stage.LeakageAudit
    results: tuple[DistillationResult, ...] = ()
    verdicts: tuple[QualityVerdict, ...] = ()
    derived_skills: tuple[skills_stage.ReasoningSkill, ...] = ()
    manifest: BuildManifest | None = None
    #: Stages that did not run, and why. A stage skipped by a refusal must be visible.
    not_run: tuple[str, ...] = field(default_factory=tuple)

    def counts(self) -> CorpusCounts:
        accepted = sum(1 for v in self.verdicts if v.disposition is Disposition.ACCEPT)
        review = sum(1 for v in self.verdicts
                     if v.disposition is Disposition.NEEDS_HUMAN_REVIEW)
        rejected = sum(1 for v in self.verdicts if v.disposition is Disposition.REJECT)
        return CorpusCounts(
            source_files=len(set(self.ingest.source_file_hashes)),
            conversations=len(self.ingest.conversations),
            turns=sum(c.turn_count for c in self.ingest.conversations),
            families=self.dedupe_report.family_count,
            exact_duplicates=self.dedupe_report.exact_duplicate_count,
            near_duplicates=self.dedupe_report.near_duplicate_count,
            development_families=len(self.split.families_in(splitting_stage.Partition.DEVELOPMENT)),
            validation_families=len(self.split.families_in(splitting_stage.Partition.VALIDATION)),
            frozen_holdout_families=len(
                self.split.families_in(splitting_stage.Partition.FROZEN_HOLDOUT)),
            distilled=len(self.results),
            accepted=accepted, review=review, rejected=rejected,
            skills_supported=sum(1 for s in self.derived_skills
                                 if s.status is SkillStatus.SUPPORTED),
            skills_provisional=sum(1 for s in self.derived_skills
                                   if s.status is SkillStatus.PROVISIONAL),
            skills_insufficient=sum(1 for s in self.derived_skills
                                    if s.status is SkillStatus.INSUFFICIENT_EVIDENCE),
        )

    def to_dict(self) -> dict:
        return {
            "ingest": self.ingest.to_dict(),
            "dedupe": self.dedupe_report.to_dict(),
            "split": self.split.to_dict(),
            "frozen_holdout": self.frozen.to_dict() if self.frozen else None,
            "leakage_audit": self.audit.to_dict(),
            "counts": self.counts().to_dict(),
            "dispositions": [
                {"example_id": r.record.example_id, **v.to_dict()}
                for r, v in zip(self.results, self.verdicts)],
            "skills": [s.to_dict() for s in self.derived_skills],
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "not_run": list(self.not_run),
        }


def ingest_paths(paths: "list[Path]", *, config: DistillationConfig | None = None,
                 sensitivity: SensitivityClass = SensitivityClass.INTERNAL,
                 limit: int = 0) -> IngestOutcome:
    """Read and normalize every ingestible file. Reports what it declined and why (§5, §6).

    Files are processed in sorted order so the outcome does not depend on directory iteration
    order, which is not stable across filesystems and would make the build unreproducible for a
    reason nobody would think to look for.
    """
    cfg = config or default_config()
    conversations: list[CanonicalConversation] = []
    skipped: list[tuple[str, str]] = []
    hashes: list[str] = []

    for path in sorted(paths, key=lambda p: (p.name, str(p))):
        name = path.name
        if not path.is_file():
            skipped.append((name, "not a regular file"))
            continue
        if path.suffix.lower() not in INGESTIBLE_SUFFIXES:
            skipped.append((name, f"suffix {path.suffix!r} is not on the ingest allowlist "
                                  f"{list(INGESTIBLE_SUFFIXES)}"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append((name, f"could not stat: {type(exc).__name__}"))
            continue
        if size > MAX_SOURCE_BYTES:
            skipped.append((name, f"{size} bytes exceeds the {MAX_SOURCE_BYTES}-byte ceiling"))
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            skipped.append((name, f"could not read: {type(exc).__name__}"))
            continue
        try:
            conversation = ingest_bytes(filename=name, content=content,
                                        config=cfg.privacy, sensitivity=sensitivity)
        except AdapterError as exc:
            # An adapter refusal is EXPECTED traffic, not a crash: §5 says an unsupported format is
            # reported, and the message already names what would be needed to support it.
            skipped.append((name, str(exc)[:320]))
            continue
        conversations.append(conversation)
        hashes.append(conversation.source_file_hash)
        if limit and len(conversations) >= limit:
            break

    return IngestOutcome(conversations=tuple(conversations), skipped=tuple(skipped),
                         source_file_hashes=tuple(hashes))


def run(paths: "list[Path]", *, config: DistillationConfig | None = None,
        provider: DistillationProvider | None = None,
        sensitivity: SensitivityClass = SensitivityClass.INTERNAL,
        freeze_holdout: bool = True, generation: str = "", limit: int = 0,
        distil: bool = True) -> BuildOutcome:
    """The whole build, in the order the module docstring justifies.

    ``distil=False`` stops after the firewall audit, which is what ``--dry-run`` uses: it produces
    every deterministic artifact and every count except the distilled records, so an operator can
    see the split and the leakage verdict before any extraction happens.
    """
    cfg = (config or default_config()).validated()
    not_run: list[str] = []

    ingest = ingest_paths(paths, config=cfg, sensitivity=sensitivity, limit=limit)

    # 1. dedupe -> families. Before the split, for the reason in the module docstring.
    report = dedupe_stage.analyze(list(ingest.conversations), config=cfg.dedupe)
    grouped = tuple(with_family(c, report.family_of(c.digest)) for c in ingest.conversations)
    ingest = IngestOutcome(conversations=grouped, skipped=ingest.skipped,
                           source_file_hashes=ingest.source_file_hashes)

    # 2. split -> partitions. Before distillation, for the reason in the module docstring.
    split = splitting_stage.plan(report, config=cfg.split)
    frozen: splitting_stage.FrozenHoldout | None = None
    if freeze_holdout and split.holdout_available:
        frozen = splitting_stage.freeze(split, generation=generation)
    elif freeze_holdout:
        not_run.append("freeze: the plan reports no available holdout, so nothing was frozen; "
                       "freezing an empty holdout would record that one exists")

    # 3. the firewall, built from the settled plan.
    guard = holdout_stage.HoldoutGuard(split, frozen)
    audit = holdout_stage.audit(report=report, split=split, conversations=list(grouped),
                                frozen=frozen, guard=guard, config=cfg.dedupe)

    results: tuple[DistillationResult, ...] = ()
    verdicts: tuple[QualityVerdict, ...] = ()
    derived: tuple[skills_stage.ReasoningSkill, ...] = ()

    # A leak invalidates the experiment, so distillation stops. An unclean audit caused only by the
    # absence of a frozen record is NOT a leak — it is the honest state of a corpus too small to
    # freeze — so it is distinguished here rather than treated as one.
    hard_failures = tuple(f for f in audit.failures
                          if f.check != "frozen_record_matches_plan"
                          or f.verdict is holdout_stage.Verdict.FAIL)
    if not distil:
        not_run.append("distil: not requested (dry run)")
    elif hard_failures:
        raise PipelineError(
            "refusing to distil: the leakage audit is not clean — "
            + "; ".join(f"{f.check}={f.verdict.value}" for f in hard_failures[:4])
            + ". A leak invalidates the experiment this corpus exists to make possible (§17)")
    else:
        by_digest = {c.digest: c for c in grouped}
        results = distil_corpus(list(grouped), guard=guard, config=cfg, provider=provider)
        verdict_list: list[QualityVerdict] = []
        canonical = {f.canonical_digest for f in report.families}
        for result in results:
            conversation = next(
                (c for c in by_digest.values()
                 if c.conversation_id == result.record.provenance.conversation_id), None)
            if conversation is None:  # pragma: no cover - results derive from `grouped`
                raise PipelineError(
                    f"{result.record.example_id}: its source conversation is not in this build; a "
                    f"record cannot be dispositioned against a conversation nobody has")
            duplicate_of = ""
            if conversation.digest not in canonical:
                family = next((f for f in report.families
                               if conversation.digest in f.member_digests), None)
                duplicate_of = family.canonical_digest if family else ""
            verdict_list.append(disposition(
                result.record, conversation,
                critique_result=critique(result.record, conversation),
                verification=result.verification, trace=result.trace,
                config=cfg.quality, duplicate_of=duplicate_of))
        verdicts = tuple(verdict_list)

        family_of = {r.record.example_id: r.record.provenance.family_id for r in results}
        derived = skills_stage.derive(list(zip((r.record for r in results), verdicts)),
                                      family_of=family_of, config=cfg.skills)

    provider_metadata = results[0].trace.provider_metadata if results else {}
    manifest = BuildManifest(
        build_id=build_id_for(source_file_hashes=tuple(set(ingest.source_file_hashes)),
                             seed=cfg.split.seed),
        config=cfg,
        counts=BuildOutcome(ingest=ingest, dedupe_report=report, split=split, frozen=frozen,
                            audit=audit, results=results, verdicts=verdicts,
                            derived_skills=derived).counts(),
        source_file_hashes=tuple(set(ingest.source_file_hashes)),
        dedupe_report_digest=sha256_obj(report.to_dict()),
        split_plan_digest=sha256_obj(split.to_dict()),
        frozen_holdout_digest=frozen.holdout_digest if frozen else "",
        leakage_audit_digest=sha256_obj(audit.to_dict()),
        skill_library_digest=skills_stage.library_digest(derived) if derived else "",
        benchmark_case_set_digest=BenchmarkHarness().case_set_digest(),
        provider_metadata=provider_metadata,
        deterministic=not provider_metadata,
        nondeterministic_stages=("distil.suggest",) if provider_metadata else (),
        leakage_clean=audit.clean,
        split_defensible=split.defensible,
        holdout_available=split.holdout_available,
    ).validated()

    return BuildOutcome(ingest=ingest, dedupe_report=report, split=split, frozen=frozen,
                        audit=audit, results=results, verdicts=verdicts,
                        derived_skills=derived, manifest=manifest, not_run=tuple(not_run))


def persist(root: CorpusRoot, outcome: BuildOutcome) -> dict:
    """Write every body-free artifact into the corpus root. Returns the paths written.

    Distilled records go to ``distilled/``, ``review/`` or ``rejected/`` by disposition, so the
    review queue IS the set of files in one directory rather than a field somebody has to filter
    on. Rejected records are WRITTEN, never deleted: §13's "source is never destroyed" applies to
    the derived record too, and a rejection nobody can re-read cannot be appealed.
    """
    from .manifests import write as write_manifest
    from .storage import write_report

    written: dict[str, str] = {}
    written["dedupe"] = str(write_report(root, "reports", "dedupe.json",
                                         outcome.dedupe_report.to_dict()))
    written["split"] = str(write_report(root, "reports", "split.json", outcome.split.to_dict()))
    written["leakage"] = str(write_report(root, "reports", "leakage.json",
                                          outcome.audit.to_dict()))
    written["ingest"] = str(write_report(root, "reports", "ingest.json",
                                         outcome.ingest.to_dict()))
    if outcome.frozen is not None:
        written["frozen"] = str(write_report(root, "holdout", "frozen.json",
                                             outcome.frozen.to_dict()))
    area_for = {Disposition.ACCEPT: "distilled",
                Disposition.NEEDS_HUMAN_REVIEW: "review",
                Disposition.REJECT: "rejected"}
    for result, verdict in zip(outcome.results, outcome.verdicts):
        payload = {"record": result.record.to_dict(), "verdict": verdict.to_dict(),
                   "trace": result.trace.to_dict(),
                   "verification": result.verification.to_dict()}
        write_report(root, area_for[verdict.disposition],
                     f"{result.record.example_id}.json", payload)
    for skill in outcome.derived_skills:
        write_report(root, "distilled", f"{skill.skill_id}.json", skill.to_dict())
    if outcome.manifest is not None:
        written["manifest"] = str(write_manifest(root, outcome.manifest))
    return written


__all__ = [
    "INGESTIBLE_SUFFIXES", "MAX_SOURCE_BYTES", "BuildOutcome", "IngestOutcome", "PipelineError",
    "ingest_paths", "persist", "run",
]
