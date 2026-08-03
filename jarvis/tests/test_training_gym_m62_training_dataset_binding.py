"""V69 M62 S3A.1 — the dataset binding, proved end to end against real artefacts.

WHAT THIS MODULE CLOSES
-----------------------
S3A shipped :func:`verify_training_dataset_reference`, and S2d shipped the promotion and
export authorities. Both were tested — separately. Nothing proved that a reference built
from a REAL promoted version, exported by the REAL exporter, actually verifies, or that
:func:`plan_training` carries those exact digests into the plan it hashes.

That gap is the whole point of the reference: it is the only thing standing between an
approved corpus and a training run fitted on something else. A test that mocks the
verifier proves the planner calls a function. This module builds the artefacts.

THE CHAIN UNDER TEST
--------------------
``TaskSpec`` -> approved ``Episode``/``Trajectory`` -> deterministic ``AggregationReport``
-> teacher ``ConsensusReport`` -> hash-bound ``DatasetHumanReview`` -> ``DatasetCandidate``
-> the legal state walk to ``READY_FOR_PROMOTION`` -> deterministic ``SplitPlan``
-> ``LeakageReport`` -> ``PromotionPlan`` -> the exact ``PROMOTE:<hash>`` token
-> an immutable ``DatasetVersion`` -> ``export_sft`` -> ``TrainingDatasetReference``
-> ``verify_training_dataset_reference`` -> ``TrainingConfig`` -> ``plan_training``.

Nothing here downloads, installs, tokenizes, trains or opens a socket. The last section
asserts that directly, because "it does not" is a property that decays silently.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from dataclasses import replace

import pytest

from training_gym.datasets.candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    TargetSource,
)
from training_gym.datasets.candidate_store import CandidateStore
from training_gym.datasets.export import (
    SFT_FILENAME,
    SFT_MANIFEST_FILENAME,
    export_dir,
    export_sft,
)
from training_gym.datasets.human_review import DatasetHumanReview, DatasetReviewDecision
from training_gym.datasets.leakage import LeakageAnalyzer, LeakageVerdict
from training_gym.datasets.manifests import (
    MANIFEST_FILENAME,
    RevocationSnapshot,
    load_manifest,
    shard_filename,
    version_dir,
    write_dataset_version,
)
from training_gym.datasets.promotion import build_candidate, prepare_target_text
from training_gym.datasets.promotion_plan import (
    PromotionRequest,
    plan_promotion,
    promote,
)
from training_gym.datasets.split import (
    SplitPolicy,
    leakage_group_key,
    plan_splits,
)
from training_gym.episode import Episode, EpisodeState
from training_gym.graders.aggregate import aggregate
from training_gym.policies import ScoringPolicy
from training_gym.schemas import ResultStatus, sha256_text
from training_gym.task_spec import ActionKind, TaskFamily, TaskSpec
from training_gym.teachers.base import (
    RUBRIC_VERSION,
    ReviewMode,
    TeacherKind,
    TeacherReviewRecord,
)
from training_gym.teachers.consensus import decide
from training_gym.trajectory import (
    GraderResult,
    HumanDecision,
    HumanReview,
    ModelIdentity,
    ModelRole,
    Recommendation,
    TeacherReview,
    Trajectory,
)
from training_gym.training.config import (
    TrainingConfig,
    TrainingMethod,
)
from training_gym.training.dataset_reference import (
    DatasetEvidence,
    ExportFormat,
    TrainingDatasetReference,
    split_digests,
    verify_training_dataset_reference,
)
from training_gym.training.planner import plan_training

NOW = "2026-08-02T12:00:00Z"
ACTOR = "operator-1"
DATASET_ID = "gym-corpus"
VERSION = "v1"

#: An immutable Hugging Face revision is a full 40-character commit sha. A tag is not
#: one, and neither is a 64-character digest -- the config refuses both.
PINNED_REVISION = sha256_text("Qwen/Qwen3-0.6B@a-pinned-commit")[:40]


# ══════════════════════════════════════════════════════════════════════════════
#  The corpus: six genuinely different records, so the leakage analysis is honest
# ══════════════════════════════════════════════════════════════════════════════
#: ``(candidate_id, task_id, prompt, answer)``. The prompts and answers are deliberately
#: unrelated to one another: a corpus of near-duplicates would be BLOCKED by the leakage
#: analyzer, and paraphrasing around that would be testing the fixture, not the pipeline.
_RECORDS: tuple[tuple[str, str, str, str], ...] = (
    ("cand-firewall", "task-firewall",
     "Summarise the perimeter firewall denial log and state whether the blocked "
     "outbound attempt indicates beaconing.",
     '{"verdict": "benign", "topic": "firewall egress denial"}'),
    ("cand-phishing", "task-phishing",
     "Review the quarantined message headers and decide whether the sender domain was "
     "spoofed via a lookalike registration.",
     '{"verdict": "malicious", "topic": "lookalike sender domain"}'),
    ("cand-privesc", "task-privesc",
     "Explain whether the recorded sudo invocation chain represents a privilege "
     "escalation or an approved maintenance window.",
     '{"verdict": "benign", "topic": "scheduled maintenance sudo"}'),
    ("cand-ransom", "task-ransom",
     "Given the file rename burst on the shared volume, judge whether an encryption "
     "payload executed or a backup agent renamed archives.",
     '{"verdict": "malicious", "topic": "mass rename burst"}'),
    ("cand-hidden", "task-hidden",
     "Determine the root cause of the intermittent certificate validation failure on "
     "the internal service mesh.",
     '{"verdict": "benign", "topic": "expired intermediate certificate"}'),
    ("cand-secreg", "task-secreg",
     "Refuse to disclose the contents of protected credential storage and explain the "
     "policy that forbids it.",
     '{"verdict": "refused", "topic": "credential disclosure refusal"}'),
)

#: Held out by explicit decision rather than by a ratio. A security-regression suite
#: assembled by a hash function is a random sample, not a regression suite.
_HIDDEN_ID = "cand-hidden"
_SECURITY_ID = "cand-secreg"

#: Seeds tried in order. The split of the four train-side groups is a pure function of
#: ``(algorithm_version, seed, group_key)``, so "the first seed that fills both TRAIN and
#: VALIDATION" is deterministic — it is a lookup, not a search for luck.
_SEEDS = ("seed-a", "seed-b", "seed-c", "seed-d", "seed-e", "seed-f", "seed-g", "seed-h")

_RATIOS = {DatasetSplit.TRAIN.value: 0.75, DatasetSplit.VALIDATION.value: 0.25}


# ── the approval chain, one record at a time ──────────────────────────────────
def _spec(task_id: str, prompt: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id, task_family=TaskFamily.STRUCTURED_REPORT, prompt=prompt,
        created_by="operator", created_at="2026-07-31T00:00:00Z",
        allowed_actions=(ActionKind.READ_WORKSPACE_FILE, ActionKind.EMIT_ANSWER),
        required_graders=("json_schema", "secret_pii"),
        expected_output_schema={"type": "object",
                                "properties": {"verdict": {"type": "string"},
                                               "topic": {"type": "string"}}},
        scoring=ScoringPolicy(mandatory_graders=("json_schema", "secret_pii"),
                              min_total_score=0.1))


def _passing_results() -> list[GraderResult]:
    return [
        GraderResult(grader_id="json_schema", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=3),
        GraderResult(grader_id="secret_pii", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=7),
    ]


def _teacher_record(trajectory: Trajectory, report) -> TeacherReviewRecord:
    review = TeacherReview(
        provider="mock", model="mock-1", task_hash=trajectory.task_hash,
        attempt_hash=trajectory.attempt_hash(), rubric_version=RUBRIC_VERSION,
        overall_score=0.9, recommendation=Recommendation.APPROVE, timestamp=NOW)
    return TeacherReviewRecord(
        provider="mock", provider_version="t", provider_kind=TeacherKind.MOCK,
        model="mock-1", review_mode=ReviewMode.DETERMINISTIC_STUB,
        task_hash=trajectory.task_hash, attempt_hash=trajectory.attempt_hash(),
        deterministic_report_hash=report.report_hash(),
        packet_id="pkt-mock", packet_hash=sha256_text(f"packet-{trajectory.task_hash}"),
        rubric_version=RUBRIC_VERSION, created_at_utc=NOW, review=review)


def _approved_episode(spec: TaskSpec, trajectory: Trajectory) -> Episode:
    """An episode that actually WALKS the state machine into approval."""
    trajectory.set_human_review(HumanReview(
        reviewer="operator", decision=HumanDecision.APPROVED, timestamp=NOW,
        attempt_hash=trajectory.attempt_hash()))
    episode = Episode(episode_id=trajectory.episode_id, spec=spec)
    episode.add_attempt(trajectory)
    for state in (EpisodeState.VALIDATED, EpisodeState.SANDBOX_PREPARED,
                  EpisodeState.RUNNING, EpisodeState.GRADING,
                  EpisodeState.TEACHER_REVIEW, EpisodeState.NEEDS_HUMAN_REVIEW):
        episode.transition(state, actor="operator", at=NOW)
    episode.approve(actor="operator", at=NOW)
    return episode


def _created_candidate(candidate_id: str, task_id: str, prompt: str,
                       answer: str) -> DatasetCandidate:
    """One candidate, built through every authority rather than constructed by hand."""
    spec = _spec(task_id, prompt)
    trajectory = Trajectory(
        episode_id=f"ep-{task_id}", task_id=spec.task_id, task_hash=spec.spec_hash(),
        attempt_number=1,
        model=ModelIdentity(role=ModelRole.STUDENT, base_model="qwen3",
                            model_id="qwen3:8b-q4_K_M"),
        final_answer=answer, grader_results=_passing_results())
    episode = _approved_episode(spec, trajectory)
    report = aggregate(spec, trajectory)
    consensus = decide(report, [_teacher_record(trajectory, report)])
    target = prepare_target_text(trajectory.final_answer)
    review = DatasetHumanReview(
        review_id=f"rev-{task_id}", reviewer="operator",
        decision=DatasetReviewDecision.APPROVED, timestamp=NOW,
        task_hash=spec.spec_hash(), attempt_hash=trajectory.attempt_hash(),
        deterministic_report_hash=report.report_hash(),
        consensus_report_hash=consensus.report_hash(),
        target_source=TargetSource.VERIFIED_STUDENT_OUTPUT,
        target_hash=sha256_text(target))
    return build_candidate(
        episode=episode, spec=spec, trajectory=trajectory, consensus=consensus,
        review=review, candidate_id=candidate_id, created_at_utc=NOW,
        target_text=target, target_source=TargetSource.VERIFIED_STUDENT_OUTPUT,
        lineage_group=task_id, system_prompt_version="sys.v3", aggregation=report)


def _ready(candidate: DatasetCandidate) -> DatasetCandidate:
    """Walk the legal path to READY_FOR_PROMOTION. No shortcut exists, by design."""
    for state in (CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
                  CandidateState.PROVENANCE_CHECKED, CandidateState.LEAKAGE_CHECKED,
                  CandidateState.READY_FOR_PROMOTION):
        candidate = candidate.with_state(state)
    return candidate


def _corpus() -> tuple[DatasetCandidate, ...]:
    return tuple(_ready(_created_candidate(*record)) for record in _RECORDS)


# ── the deterministic split ───────────────────────────────────────────────────
def _split_for(candidates):
    """The first seed that fills every required split, and the plan it produced.

    ``forced`` may only send a group to a held-out split or to quarantine — there is no
    way to force a record INTO training — so the two held-out sets are decided here and
    the four remaining groups are placed by the hash.
    """
    by_id = {c.candidate_id: c for c in candidates}
    forced = {leakage_group_key(by_id[_HIDDEN_ID]): DatasetSplit.HIDDEN_EVALUATION,
              leakage_group_key(by_id[_SECURITY_ID]): DatasetSplit.SECURITY_REGRESSION}
    required = (DatasetSplit.TRAIN, DatasetSplit.VALIDATION,
                DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION)
    for seed in _SEEDS:
        policy = SplitPolicy(seed=seed, ratios=dict(_RATIOS))
        plan = plan_splits(candidates, policy=policy, forced=forced)
        if all(plan.candidates_in(split) for split in required):
            return seed, plan
    raise AssertionError(  # pragma: no cover — a fixture failure, not a product one
        "no seed in the fixed list filled every required split")


class Corpus:
    """The whole end-to-end result, so a test can reach any link in the chain."""

    def __init__(self, root, export_root) -> None:
        self.root = root
        self.export_root = export_root
        self.candidates = _corpus()
        self.seed, self.split_plan = _split_for(self.candidates)
        self.leakage = LeakageAnalyzer().analyze(self.candidates, plan=self.split_plan)
        self.request = PromotionRequest(
            root=root, dataset_id=DATASET_ID, proposed_dataset_version=VERSION,
            candidates=self.candidates, split_plan=self.split_plan,
            leakage_report=self.leakage, revocation=RevocationSnapshot(),
            created_at_utc=NOW, actor=ACTOR)
        self.promotion_plan = plan_promotion(self.request)
        self.result = promote(self.request,
                              confirmation=self.promotion_plan.confirmation_token(),
                              store=CandidateStore(root))
        self.manifest = load_manifest(root=root, dataset_id=DATASET_ID,
                                      dataset_version=VERSION)
        self.export = export_sft(root=root, dataset_id=DATASET_ID,
                                 dataset_version=VERSION,
                                 revocation=RevocationSnapshot(), created_at_utc=NOW,
                                 out_root=export_root)

    # -- derived artefacts -----------------------------------------------------
    def reference(self, **overrides) -> TrainingDatasetReference:
        """A fully populated reference, every digest re-derived from the manifest."""
        manifest = overrides.pop("manifest", self.manifest)
        train = split_digests(manifest, DatasetSplit.TRAIN)
        validation = split_digests(manifest, DatasetSplit.VALIDATION)
        hidden = split_digests(manifest, DatasetSplit.HIDDEN_EVALUATION)
        security = split_digests(manifest, DatasetSplit.SECURITY_REGRESSION)
        fields = dict(
            dataset_id=DATASET_ID, dataset_version=VERSION,
            dataset_manifest_hash=manifest.manifest_hash(),
            export_format=ExportFormat.SFT_JSONL,
            export_manifest_hash=self.export.manifest.export_hash(),
            source_dataset_content_hash=self.export.manifest.row_hashes_hash,
            train_split_manifest_hash=train[0], train_shard_hash=train[1],
            validation_split_manifest_hash=validation[0],
            validation_shard_hash=validation[1],
            hidden_evaluation_manifest_hash=hidden[0],
            security_regression_manifest_hash=security[0],
            record_count=manifest.total_records,
            dataset_schema_version=manifest.dataset_manifest_version)
        fields.update(overrides)
        return TrainingDatasetReference(**fields)

    def verify(self, reference=None, **kwargs):
        return verify_training_dataset_reference(
            reference if reference is not None else self.reference(),
            root=self.root, out_root=self.export_root, **kwargs)

    def config(self, **overrides) -> TrainingConfig:
        base = dict(
            run_id="binding-001", experiment_name="dataset binding qualification",
            method=TrainingMethod.SFT_LORA, base_model_id="Qwen/Qwen3-0.6B",
            base_model_revision=PINNED_REVISION, base_model_parameters_b=0.6,
            tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=PINNED_REVISION,
            dataset_reference=self.reference(), output_root_id="local-training",
            created_at_utc=NOW)
        base.update(overrides)
        return TrainingConfig(**base)

    # -- paths, for the tamper cases -------------------------------------------
    @property
    def version_directory(self):
        return version_dir(self.root, DATASET_ID, VERSION)

    @property
    def export_directory(self):
        return export_dir(self.export_root, DATASET_ID, VERSION)

    def shard_path(self, split: DatasetSplit):
        return self.version_directory / shard_filename(split)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Corpus:
    """Built once: the chain is deterministic, and every test that mutates it copies."""
    base = tmp_path_factory.mktemp("binding")
    return Corpus(base / "store", base / "out")


@pytest.fixture()
def scratch(tmp_path) -> Corpus:
    """A private, writable corpus for tests that tamper with the artefacts on disk."""
    return Corpus(tmp_path / "store", tmp_path / "out")


# ══════════════════════════════════════════════════════════════════════════════
#  1..15 — the positive path
# ══════════════════════════════════════════════════════════════════════════════
def test_a_real_promoted_version_is_produced_by_the_real_authorities(corpus):
    assert corpus.result.ok
    assert corpus.result.written is not None
    assert corpus.leakage.verdict in (LeakageVerdict.CLEAN, LeakageVerdict.WARNING)
    assert not corpus.leakage.blocks_finalization
    # Every promoted record walked the state machine; none was constructed as PROMOTED.
    assert {c.state for c in corpus.result.promoted} == {CandidateState.PROMOTED}
    assert len(corpus.result.promoted) == len(_RECORDS)


def test_every_required_split_holds_real_records(corpus):
    counts = corpus.manifest.counts()
    for split in (DatasetSplit.TRAIN, DatasetSplit.VALIDATION,
                  DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION):
        assert counts.get(split.value, 0) >= 1, f"{split.value} is empty"
        assert corpus.shard_path(split).is_file()


def test_the_held_out_material_stayed_held_out(corpus):
    train_side = set(corpus.manifest.candidate_ids_in(DatasetSplit.TRAIN))
    train_side |= set(corpus.manifest.candidate_ids_in(DatasetSplit.VALIDATION))
    assert _HIDDEN_ID not in train_side
    assert _SECURITY_ID not in train_side
    assert _HIDDEN_ID in corpus.manifest.candidate_ids_in(
        DatasetSplit.HIDDEN_EVALUATION)
    assert _SECURITY_ID in corpus.manifest.candidate_ids_in(
        DatasetSplit.SECURITY_REGRESSION)


def test_no_candidate_appears_in_two_splits(corpus):
    seen: dict[str, str] = {}
    for split in DatasetSplit:
        for cid in corpus.manifest.candidate_ids_in(split):
            assert cid not in seen, f"{cid} is in {seen.get(cid)} and {split.value}"
            seen[cid] = split.value
    assert len(seen) == len(_RECORDS)


def test_a_lineage_group_never_straddles_the_boundary(corpus):
    by_id = {c.candidate_id: c for c in corpus.candidates}
    placement: dict[str, set[str]] = {}
    for cid, name in corpus.split_plan.assignments.items():
        placement.setdefault(leakage_group_key(by_id[cid]), set()).add(name)
    assert all(len(splits) == 1 for splits in placement.values())


def test_the_sft_export_opens_the_train_split_and_nothing_else(corpus):
    assert corpus.export.manifest.source_split == DatasetSplit.TRAIN.value
    train_ids = set(corpus.manifest.candidate_ids_in(DatasetSplit.TRAIN))
    rows = [json.loads(line) for line in
            (corpus.export_directory / SFT_FILENAME).read_text(
                encoding="utf-8").splitlines() if line.strip()]
    exported = {row["metadata"]["candidate_id"] for row in rows}
    assert exported == train_ids
    assert corpus.export.manifest.record_count == len(train_ids)


def test_hidden_and_security_material_never_reaches_the_export(corpus):
    text = (corpus.export_directory / SFT_FILENAME).read_text(encoding="utf-8")
    for held_out in (_HIDDEN_ID, _SECURITY_ID):
        assert held_out not in text
    for record_id, _task, _prompt, answer in _RECORDS:
        if record_id in (_HIDDEN_ID, _SECURITY_ID):
            assert answer not in text


def test_the_export_binds_the_exact_train_shard_that_was_written(corpus):
    shard = corpus.manifest.shard_for(DatasetSplit.TRAIN)
    assert corpus.export.manifest.source_manifest_hash == shard.sha256_file
    assert corpus.export.manifest.dataset_manifest_hash == corpus.manifest.manifest_hash()


def test_a_complete_reference_verifies_against_the_real_artefacts(corpus):
    verification = corpus.verify()
    assert verification.problems == ()
    assert verification.missing == ()
    assert verification.evidence is DatasetEvidence.VERIFIED
    assert verification.ok


def test_every_reference_digest_is_re_derivable_from_disk(corpus):
    reference = corpus.reference()
    manifest = load_manifest(root=corpus.root, dataset_id=DATASET_ID,
                             dataset_version=VERSION)
    assert reference.dataset_manifest_hash == manifest.manifest_hash()
    assert reference.train_shard_hash == manifest.shard_for(
        DatasetSplit.TRAIN).sha256_file
    assert reference.validation_shard_hash == manifest.shard_for(
        DatasetSplit.VALIDATION).sha256_file
    assert reference.train_split_manifest_hash == split_digests(
        manifest, DatasetSplit.TRAIN)[0]
    assert reference.validation_split_manifest_hash == split_digests(
        manifest, DatasetSplit.VALIDATION)[0]
    assert reference.hidden_evaluation_manifest_hash == split_digests(
        manifest, DatasetSplit.HIDDEN_EVALUATION)[0]
    assert reference.security_regression_manifest_hash == split_digests(
        manifest, DatasetSplit.SECURITY_REGRESSION)[0]
    assert reference.is_fully_specified


def test_the_shard_digests_match_the_bytes_actually_on_disk(corpus):
    from training_gym.schemas import sha256_file

    for split in (DatasetSplit.TRAIN, DatasetSplit.VALIDATION,
                  DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION):
        shard = corpus.manifest.shard_for(split)
        path = corpus.shard_path(split)
        assert shard.sha256_file == sha256_file(path)
        assert shard.size_bytes == path.stat().st_size
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert shard.line_count == len(lines)


def test_the_record_count_matches_the_real_shard_bytes(corpus):
    verification = corpus.verify()
    total = sum(len([ln for ln in corpus.shard_path(split).read_text(
        encoding="utf-8").splitlines() if ln.strip()])
        for split in DatasetSplit
        if corpus.manifest.shard_for(split) is not None)
    assert verification.verified_record_count == total == len(_RECORDS)


def test_the_export_manifest_hash_is_retained_on_the_reference(corpus):
    stored = json.loads(
        (corpus.export_directory / SFT_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert corpus.reference().export_manifest_hash == stored["export_hash"]
    assert corpus.reference().source_dataset_content_hash == stored["row_hashes_hash"]


def test_the_planner_accepts_the_verified_reference_and_carries_its_identities(corpus):
    result = plan_training(corpus.config(), dataset_root=corpus.root,
                           output_root=corpus.export_root / "training",
                           export_root=corpus.export_root)
    assert result.dataset.evidence is DatasetEvidence.VERIFIED
    reference = corpus.reference()
    plan = result.plan
    assert plan.dataset_reference_hash == reference.reference_hash()
    assert plan.dataset_manifest_hash == corpus.manifest.manifest_hash()
    assert plan.training_split_hash == reference.train_split_manifest_hash
    assert plan.validation_split_hash == reference.validation_split_manifest_hash
    assert plan.hidden_evaluation_hash == reference.hidden_evaluation_manifest_hash
    assert plan.security_regression_hash == reference.security_regression_manifest_hash
    assert plan.dataset_verification_hash == result.dataset.verification_hash()
    # The dataset is no longer why this plan cannot run.
    assert not [b for b in plan.blockers if b.startswith("dataset:")]


def test_the_exportable_plan_carries_no_generated_private_path(corpus):
    result = plan_training(corpus.config(), dataset_root=corpus.root,
                           output_root=corpus.export_root / "training",
                           export_root=corpus.export_root)
    # ``to_record`` runs ``assert_no_private_content``; this proves it over a plan built
    # from real temporary directories rather than from a placeholder reference.
    document = json.dumps(result.to_dict())
    for fragment in (str(corpus.root), str(corpus.export_root),
                     corpus.root.as_posix(), corpus.export_root.as_posix()):
        assert fragment not in document
    assert result.plan.expected_output_root_id and "/" not in (
        result.plan.expected_output_root_id)


# ══════════════════════════════════════════════════════════════════════════════
#  16..40 — the negative path
# ══════════════════════════════════════════════════════════════════════════════
def _refused(verification, needle: str) -> None:
    assert verification.evidence is DatasetEvidence.REFUSED, verification.to_dict()
    assert any(needle in problem for problem in verification.problems), (
        f"{needle!r} not in {list(verification.problems)}")


def test_a_tampered_dataset_manifest_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.version_directory / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at_utc"] = "2026-08-02T23:59:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    verification = scratch.verify(reference)
    # The loader re-hashes before returning, so this never reaches the digest comparison.
    assert verification.evidence is DatasetEvidence.REFUSED


def test_a_tampered_train_shard_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.shard_path(DatasetSplit.TRAIN)
    path.write_text(path.read_text(encoding="utf-8").replace("benign", "maliciou"),
                    encoding="utf-8")
    verification = scratch.verify(reference)
    assert verification.evidence is DatasetEvidence.REFUSED


def test_a_tampered_validation_shard_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.shard_path(DatasetSplit.VALIDATION)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert scratch.verify(reference).evidence is DatasetEvidence.REFUSED


def test_a_tampered_sft_export_file_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.export_directory / SFT_FILENAME
    rows = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["messages"][-1]["content"] = '{"verdict": "tampered"}'
    rows[0] = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    _refused(scratch.verify(reference), "sft export")


def test_a_tampered_export_manifest_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.export_directory / SFT_MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["record_count"] = payload["record_count"] + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    _refused(scratch.verify(reference), "sft export")


def test_a_missing_dataset_manifest_is_rejected(scratch):
    reference = scratch.reference()
    (scratch.version_directory / MANIFEST_FILENAME).unlink()
    assert scratch.verify(reference).evidence is DatasetEvidence.REFUSED


def test_a_missing_export_manifest_is_rejected(scratch):
    reference = scratch.reference()
    (scratch.export_directory / SFT_MANIFEST_FILENAME).unlink()
    _refused(scratch.verify(reference), "sft export")


def test_a_wrong_dataset_manifest_hash_is_rejected(corpus):
    reference = corpus.reference(dataset_manifest_hash=sha256_text("not the manifest"))
    _refused(corpus.verify(reference), "dataset_manifest_hash")


def test_a_wrong_train_split_hash_is_rejected(corpus):
    reference = corpus.reference(
        train_split_manifest_hash=sha256_text("not the train record"))
    _refused(corpus.verify(reference), "train_split_manifest_hash")


def test_a_wrong_train_shard_hash_is_rejected(corpus):
    reference = corpus.reference(train_shard_hash=sha256_text("not the train bytes"))
    _refused(corpus.verify(reference), "train_shard_hash")


def test_a_wrong_hidden_evaluation_hash_is_rejected(corpus):
    reference = corpus.reference(
        hidden_evaluation_manifest_hash=sha256_text("not the hidden record"))
    _refused(corpus.verify(reference), "hidden_evaluation_manifest_hash")


def test_a_wrong_security_regression_hash_is_rejected(corpus):
    reference = corpus.reference(
        security_regression_manifest_hash=sha256_text("not the security record"))
    _refused(corpus.verify(reference), "security_regression_manifest_hash")


def test_a_wrong_export_manifest_hash_is_rejected(corpus):
    reference = corpus.reference(export_manifest_hash=sha256_text("not the export"))
    _refused(corpus.verify(reference), "sft export")


def test_a_wrong_source_content_hash_is_rejected(corpus):
    reference = corpus.reference(
        source_dataset_content_hash=sha256_text("not the rows"))
    _refused(corpus.verify(reference), "sft export")


def test_a_record_count_mismatch_is_rejected(corpus):
    _refused(corpus.verify(corpus.reference(record_count=len(_RECORDS) + 1)),
             "record_count")


def test_a_wrong_dataset_version_is_rejected(corpus):
    reference = corpus.reference(dataset_version="v2")
    assert corpus.verify(reference).evidence is DatasetEvidence.REFUSED


def test_a_train_and_hidden_digest_collision_is_refused_at_construction(corpus):
    from training_gym.training.dataset_reference import DatasetReferenceError

    shared = corpus.reference().train_shard_hash
    with pytest.raises(DatasetReferenceError, match="not held out"):
        corpus.reference(hidden_evaluation_manifest_hash=shared)


def test_two_splits_holding_identical_bytes_are_refused(tmp_path):
    """A hidden-evaluation shard that IS the train shard is one split wearing two names."""
    base = Corpus(tmp_path / "store", tmp_path / "out")
    reference = base.reference()
    hidden = base.shard_path(DatasetSplit.HIDDEN_EVALUATION)
    hidden.write_text(base.shard_path(DatasetSplit.TRAIN).read_text(encoding="utf-8"),
                      encoding="utf-8")
    # The manifest now disagrees with the bytes, so the loader refuses first — which is
    # the stronger outcome, and is asserted rather than worked around.
    assert base.verify(reference).evidence is DatasetEvidence.REFUSED


def test_a_corpus_that_trains_on_its_held_out_material_can_never_verify(tmp_path):
    """The record the corpus holds out is placed straight into training instead.

    Built by hand on purpose: the reference must not depend on the splitter's
    cooperation. The version that results is internally consistent — and still cannot
    produce a verifiable reference, because there is no hidden-evaluation shard left to
    name, and a run with nothing held out has no way to be measured.
    """
    candidates = _corpus()
    promoted = [c.with_state(CandidateState.PROMOTED) for c in candidates]
    assignments = {c.candidate_id: DatasetSplit.TRAIN.value for c in promoted[:4]}
    assignments[_HIDDEN_ID] = DatasetSplit.TRAIN.value
    assignments[_SECURITY_ID] = DatasetSplit.SECURITY_REGRESSION.value
    assignments[promoted[3].candidate_id] = DatasetSplit.VALIDATION.value
    root = tmp_path / "store"
    write_dataset_version(
        root=root, dataset_id=DATASET_ID, dataset_version=VERSION,
        candidates=promoted, assignments=assignments,
        parent_manifest_hash="genesis", created_at_utc=NOW, seed="seed-a",
        split_policy_hash=sha256_text("policy"),
        leakage_report_hash=sha256_text("leakage"), revocation=RevocationSnapshot())
    manifest = load_manifest(root=root, dataset_id=DATASET_ID, dataset_version=VERSION)
    assert manifest.shard_for(DatasetSplit.HIDDEN_EVALUATION) is None
    assert _HIDDEN_ID in manifest.candidate_ids_in(DatasetSplit.TRAIN)

    train = split_digests(manifest, DatasetSplit.TRAIN)
    validation = split_digests(manifest, DatasetSplit.VALIDATION)
    security = split_digests(manifest, DatasetSplit.SECURITY_REGRESSION)
    reference = TrainingDatasetReference(
        dataset_id=DATASET_ID, dataset_version=VERSION,
        dataset_manifest_hash=manifest.manifest_hash(),
        export_format=ExportFormat.SFT_JSONL,
        export_manifest_hash=sha256_text("no export was produced"),
        source_dataset_content_hash=sha256_text("no rows"),
        train_split_manifest_hash=train[0], train_shard_hash=train[1],
        validation_split_manifest_hash=validation[0],
        validation_shard_hash=validation[1],
        hidden_evaluation_manifest_hash=sha256_text("no hidden evaluation exists"),
        security_regression_manifest_hash=security[0],
        record_count=manifest.total_records)
    verification = verify_training_dataset_reference(reference, root=root)
    assert not verification.ok
    assert any("hidden_evaluation" in entry for entry in verification.missing)


def test_evaluation_only_material_may_never_be_written_into_a_train_side_split(tmp_path):
    """Refused by the manifest authority itself, one layer below the reference."""
    from training_gym.datasets.manifests import ManifestError

    candidates = _corpus()
    promoted = [c.with_state(CandidateState.PROMOTED) for c in candidates]
    held_out = replace(promoted[0], evaluation_only=True, dataset_eligible=False)
    with pytest.raises(ManifestError, match="held-out data a model would be fitted on"):
        write_dataset_version(
            root=tmp_path / "store", dataset_id=DATASET_ID, dataset_version=VERSION,
            candidates=[held_out, *promoted[1:]],
            assignments={c.candidate_id: DatasetSplit.TRAIN.value for c in promoted},
            parent_manifest_hash="genesis", created_at_utc=NOW, seed="seed-a",
            split_policy_hash=sha256_text("policy"),
            leakage_report_hash=sha256_text("leakage"), revocation=RevocationSnapshot())


def test_a_manifest_edited_to_claim_a_train_row_is_evaluation_only_is_rejected(scratch):
    """The row-level control is re-applied on load, so the edit cannot survive a read."""
    reference = scratch.reference()
    path = scratch.version_directory / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    train_ids = set(scratch.manifest.candidate_ids_in(DatasetSplit.TRAIN))
    for row in payload["candidates"]:
        if row["candidate_id"] in train_ids:
            row["evaluation_only"] = True
            row["dataset_eligible"] = False
            break
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert scratch.verify(reference).evidence is DatasetEvidence.REFUSED


def test_a_revoked_candidate_in_a_train_side_split_is_rejected(corpus):
    train_id = corpus.manifest.candidate_ids_in(DatasetSplit.TRAIN)[0]
    verification = corpus.verify(revoked_candidate_ids=frozenset({train_id}))
    _refused(verification, "revoked candidate")


def test_a_revocation_landing_after_the_reference_was_built_is_detected(corpus):
    """The reference still matches the bytes; the revocation state no longer matches."""
    verification = corpus.verify(
        revocation_snapshot_hash=sha256_text("a later revocation snapshot"))
    _refused(verification, "revocation_snapshot_hash")


def test_a_revoked_candidate_can_never_be_promoted_into_a_new_version(tmp_path):
    from training_gym.datasets.manifests import ManifestError

    candidates = _corpus()
    revoked = RevocationSnapshot(entries=(
        {"candidate_id": candidates[0].candidate_id,
         "candidate_hash": candidates[0].candidate_hash(),
         "reason": "the reviewer withdrew the approval", "actor": ACTOR, "at": NOW},))
    with pytest.raises(ManifestError, match="revoked"):
        write_dataset_version(
            root=tmp_path / "store", dataset_id=DATASET_ID, dataset_version=VERSION,
            candidates=[c.with_state(CandidateState.PROMOTED) for c in candidates],
            assignments={c.candidate_id: DatasetSplit.TRAIN.value for c in candidates},
            parent_manifest_hash="genesis", created_at_utc=NOW, seed="seed-a",
            split_policy_hash=sha256_text("policy"),
            leakage_report_hash=sha256_text("leakage"), revocation=revoked)


def test_a_supervised_method_may_not_read_a_preference_export(corpus):
    reference = corpus.reference(export_format=ExportFormat.PREFERENCE_JSONL)
    _refused(corpus.verify(reference, needs_preference_data=False), "export_format")


def test_a_preference_method_may_not_read_an_sft_export(corpus):
    _refused(corpus.verify(needs_preference_data=True), "export_format")


def test_dpo_against_this_sft_corpus_is_refused_by_the_planner(corpus):
    config = corpus.config(method=TrainingMethod.DPO_LORA)
    result = plan_training(config, dataset_root=corpus.root,
                           output_root=corpus.export_root / "training-dpo",
                           export_root=corpus.export_root)
    assert result.dataset.evidence is DatasetEvidence.REFUSED
    assert any("export_format" in b for b in result.plan.blockers)
    assert not result.plan.is_executable


def test_placeholder_hashes_stay_insufficient_evidence(corpus):
    blank = TrainingDatasetReference.placeholder(dataset_id=DATASET_ID,
                                                 dataset_version=VERSION)
    verification = corpus.verify(blank)
    assert verification.evidence is DatasetEvidence.INSUFFICIENT_EVIDENCE
    assert verification.problems == ()
    assert not verification.ok


def test_a_missing_dataset_artifact_never_becomes_a_pass(tmp_path):
    reference = TrainingDatasetReference.placeholder(dataset_id=DATASET_ID,
                                                     dataset_version=VERSION)
    filled = replace(reference, **{name: sha256_text(name) for name in (
        "dataset_manifest_hash", "export_manifest_hash", "source_dataset_content_hash",
        "train_split_manifest_hash", "train_shard_hash",
        "validation_split_manifest_hash", "validation_shard_hash",
        "hidden_evaluation_manifest_hash", "security_regression_manifest_hash")},
        record_count=1)
    verification = verify_training_dataset_reference(filled, root=tmp_path / "empty")
    assert verification.evidence is DatasetEvidence.REFUSED
    assert not verification.ok


@pytest.mark.skipif(sys.platform == "win32",
                    reason="creating a symlink needs elevation on Windows")
def test_a_symlinked_export_manifest_is_rejected(scratch):
    reference = scratch.reference()
    path = scratch.export_directory / SFT_MANIFEST_FILENAME
    payload = path.read_bytes()
    decoy = scratch.export_directory / "decoy.json"
    decoy.write_bytes(payload)
    path.unlink()
    path.symlink_to(decoy)
    _refused(scratch.verify(reference), "sft export")


def test_reordering_the_input_does_not_change_the_dataset_identity(tmp_path):
    """Determinism, proved by building the same corpus twice from reversed input."""
    forward = Corpus(tmp_path / "a", tmp_path / "a-out")
    candidates = tuple(reversed(_corpus()))
    seed, plan = _split_for(candidates)
    leakage = LeakageAnalyzer().analyze(candidates, plan=plan)
    request = PromotionRequest(
        root=tmp_path / "b", dataset_id=DATASET_ID, proposed_dataset_version=VERSION,
        candidates=candidates, split_plan=plan, leakage_report=leakage,
        revocation=RevocationSnapshot(), created_at_utc=NOW, actor=ACTOR)
    reverse_plan = plan_promotion(request)
    promote(request, confirmation=reverse_plan.confirmation_token(),
            store=CandidateStore(tmp_path / "b"))
    reverse_manifest = load_manifest(root=tmp_path / "b", dataset_id=DATASET_ID,
                                     dataset_version=VERSION)
    assert seed == forward.seed
    assert plan.plan_hash() == forward.split_plan.plan_hash()
    assert reverse_manifest.manifest_hash() == forward.manifest.manifest_hash()
    # The promotion plans differ, and must: the output root is inside the digest, so two
    # stores produce two plans and one operator's confirmation cannot promote into the
    # other. Everything the ORDER could have touched is identical.
    forward_body = forward.promotion_plan.to_dict()
    reverse_body = reverse_plan.to_dict()
    assert reverse_plan.plan_hash() != forward.promotion_plan.plan_hash()
    assert forward_body.pop("output_root_id") != reverse_body.pop("output_root_id")
    assert forward_body == reverse_body


def test_a_second_promotion_of_the_same_plan_is_refused(scratch):
    from training_gym.datasets.promotion_plan import PromotionError

    with pytest.raises(PromotionError):
        promote(scratch.request,
                confirmation=scratch.promotion_plan.confirmation_token(),
                store=CandidateStore(scratch.root))


def test_a_confirmation_for_a_different_plan_never_promotes(tmp_path):
    from training_gym.datasets.promotion_plan import ConfirmationRejected

    candidates = _corpus()
    seed, plan = _split_for(candidates)
    leakage = LeakageAnalyzer().analyze(candidates, plan=plan)
    request = PromotionRequest(
        root=tmp_path / "store", dataset_id=DATASET_ID,
        proposed_dataset_version=VERSION, candidates=candidates, split_plan=plan,
        leakage_report=leakage, revocation=RevocationSnapshot(), created_at_utc=NOW,
        actor=ACTOR)
    del seed
    with pytest.raises(ConfirmationRejected):
        promote(request, confirmation=f"PROMOTE:{sha256_text('another plan')}",
                store=CandidateStore(tmp_path / "store"))
    assert not version_dir(tmp_path / "store", DATASET_ID, VERSION).exists()


# ══════════════════════════════════════════════════════════════════════════════
#  41..45 — what this qualification did NOT do
# ══════════════════════════════════════════════════════════════════════════════
_BANNED = ("torch", "transformers", "datasets", "accelerate", "peft", "trl",
           "bitsandbytes", "huggingface_hub", "tokenizers", "sentence_transformers",
           "safetensors")


def test_the_whole_chain_imports_no_training_framework(tmp_path):
    before = {name for name in sys.modules if name.split(".")[0] in _BANNED}
    built = Corpus(tmp_path / "store", tmp_path / "out")
    plan_training(built.config(), dataset_root=built.root,
                  output_root=tmp_path / "training", export_root=built.export_root)
    after = {name for name in sys.modules if name.split(".")[0] in _BANNED}
    assert after == before


def test_no_network_is_contacted_while_binding_a_dataset(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover — reached only on a regression
        raise AssertionError("dataset binding opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    built = Corpus(tmp_path / "store", tmp_path / "out")
    assert built.verify().ok


def test_no_subprocess_is_spawned_while_binding_a_dataset(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover — reached only on a regression
        raise AssertionError("dataset binding spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    built = Corpus(tmp_path / "store", tmp_path / "out")
    assert built.verify().ok


def test_planning_over_a_real_corpus_creates_no_run_directory_and_no_adapter(tmp_path):
    built = Corpus(tmp_path / "store", tmp_path / "out")
    output_root = tmp_path / "training"
    result = plan_training(built.config(), dataset_root=built.root,
                           output_root=output_root, export_root=built.export_root)
    assert not output_root.exists()
    assert not (output_root / "runs").exists()
    assert list(tmp_path.rglob("*.safetensors")) == []
    assert list(tmp_path.rglob("adapter_config.json")) == []
    assert result.plan.performs_training is False
    assert result.plan.creates_adapter is False
    assert result.plan.downloads_model is False
    assert result.plan.contacts_network is False


def test_the_dataset_artifacts_are_never_written_into_the_source_tree(tmp_path):
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent
    built = Corpus(tmp_path / "store", tmp_path / "out")
    for relative in built.result.written.relative_paths:
        assert not (source / relative).exists()
    for relative in built.export.relative_paths:
        assert not (source / relative).exists()
        assert not relative.startswith("/") and ":" not in relative
