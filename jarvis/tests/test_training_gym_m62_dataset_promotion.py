"""V69 M62 S2d — plan-bound, atomic, replay-proof dataset promotion.

The claim under test is that there is no way to create a dataset version other than by
holding a confirmation token derived from the exact plan that describes it, computed from
the exact state of the world at the moment of promotion — and that a failure at any step
leaves either the previous state or a complete version, never a half-finished one that
verifies.

Nothing here trains, downloads, tokenizes or contacts a network. The tests assert that
too, because "it does not" is a property that decays silently.
"""
from __future__ import annotations

import json

import pytest

from training_gym.datasets.candidate import (
    CandidateState,
    DatasetCandidate,
    DatasetSplit,
    TargetSource,
)
from training_gym.datasets.candidate_store import CandidateStore
from training_gym.datasets.leakage import (
    LeakagePolicy,
    LeakageReport,
    LeakageVerdict,
    LEAKAGE_VERSION,
)
from training_gym.datasets.manifests import (
    GENESIS_PARENT,
    MANIFEST_FILENAME,
    RevocationSnapshot,
    verify_version,
    version_dir,
)
from training_gym.datasets.promotion_plan import (
    CONFIRMATION_PREFIX,
    ConfirmationRejected,
    PlanAlreadyConsumed,
    PromotionError,
    PromotionRequest,
    check_confirmation,
    is_plan_consumed,
    plan_promotion,
    promote,
    promotion_entries,
)
from training_gym.datasets.split import DEFAULT_RATIOS, SplitPlan, SplitPolicy
from training_gym.schemas import sha256_text
from training_gym.task_spec import TaskFamily

NOW = "2026-08-02T10:00:00Z"


def make_candidate(candidate_id: str, *,
                   state: CandidateState = CandidateState.READY_FOR_PROMOTION,
                   prompt: str = "", target: str = "", **overrides) -> DatasetCandidate:
    prompt = prompt or f"Prompt for {candidate_id}"
    target = target or f'{{"verdict": "{candidate_id}"}}'
    base = {
        "candidate_id": candidate_id,
        "task_id": overrides.pop("task_id", candidate_id),
        "task_family": TaskFamily.STRUCTURED_REPORT,
        "task_hash": sha256_text(f"task-{candidate_id}"),
        "attempt_id": f"attempt-{candidate_id}",
        "attempt_hash": sha256_text(f"attempt-{candidate_id}"),
        "deterministic_report_hash": sha256_text(f"det-{candidate_id}"),
        "consensus_report_hash": sha256_text(f"con-{candidate_id}"),
        "human_review_hash": sha256_text(f"rev-{candidate_id}"),
        "source_episode_hash": sha256_text(f"ep-{candidate_id}"),
        "student_model_id": "qwen3:8b-q4_K_M",
        "system_message": "",
        "user_prompt": prompt,
        "target_text": target,
        "target_source": TargetSource.VERIFIED_STUDENT_OUTPUT,
        "target_hash": sha256_text(target),
        "approval_hash": sha256_text(f"rev-{candidate_id}"),
        "created_at_utc": NOW,
        "lineage_group": candidate_id,
        "provenance": {"source": "handwritten"},
        "state": state,
    }
    base.update(overrides)
    return DatasetCandidate(**base)


def clean_report(**overrides) -> LeakageReport:
    base = {"version": LEAKAGE_VERSION, "policy": LeakagePolicy(),
            "verdict": LeakageVerdict.CLEAN, "candidate_count": 3}
    base.update(overrides)
    return LeakageReport(**base)


def split_plan(assignments: dict, *, seed: str = "seed-a") -> SplitPlan:
    return SplitPlan(policy=SplitPolicy(seed=seed, ratios=dict(DEFAULT_RATIOS)),
                     assignments=dict(assignments),
                     group_splits={f"g-{cid}": name
                                   for cid, name in sorted(assignments.items())})


DEFAULT_ASSIGNMENTS = {"c-1": DatasetSplit.TRAIN.value,
                       "c-2": DatasetSplit.TRAIN.value,
                       "c-3": DatasetSplit.HIDDEN_EVALUATION.value}


def make_request(root, *, candidates=None, assignments=None, version="v1",
                 **overrides) -> PromotionRequest:
    candidates = candidates if candidates is not None else [
        make_candidate("c-1"), make_candidate("c-2"), make_candidate("c-3")]
    assignments = (assignments if assignments is not None
                   else dict(DEFAULT_ASSIGNMENTS))
    kwargs = {
        "root": root, "dataset_id": "corpus", "proposed_dataset_version": version,
        "candidates": tuple(candidates),
        "split_plan": split_plan(assignments, seed=overrides.pop("seed", "seed-a")),
        "leakage_report": overrides.pop("leakage_report", clean_report()),
        "revocation": overrides.pop("revocation", RevocationSnapshot()),
        "created_at_utc": NOW, "actor": "operator-1",
    }
    kwargs.update(overrides)
    return PromotionRequest(**kwargs)


def store_for(root) -> CandidateStore:
    return CandidateStore(root)


def run_promotion(root, request=None, **overrides):
    request = request or make_request(root, **overrides)
    plan = plan_promotion(request)
    return promote(request, confirmation=plan.confirmation_token(),
                   store=store_for(root)), plan


# ── 31..33: the dry run ───────────────────────────────────────────────────────
def test_a_dry_run_writes_nothing(tmp_path):
    request = make_request(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    plan = plan_promotion(request)
    assert plan.expected_candidate_count == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "datasets").exists()
    assert not (tmp_path / "promotions.jsonl").exists()
    assert not (tmp_path / "transitions.jsonl").exists()


def test_a_dry_run_returns_the_exact_effects_it_would_have(tmp_path):
    plan = plan_promotion(make_request(tmp_path))
    effects = plan.expected_effects()
    assert effects["records_per_split"] == {"hidden_evaluation": 1, "train": 2}
    assert effects["creates_files"] == ["train.jsonl", "hidden_evaluation.jsonl",
                                        "manifest.json"]
    assert effects["total_records"] == 3
    assert [t["candidate_id"] for t in effects["candidate_transitions"]] == [
        "c-1", "c-2", "c-3"]
    assert all(t["from"] == "ready_for_promotion" and t["to"] == "promoted"
               for t in effects["candidate_transitions"])
    assert effects["trains_a_model"] is False
    assert effects["downloads_anything"] is False
    assert effects["contacts_the_network"] is False


def test_the_plan_hash_is_deterministic_and_order_independent(tmp_path):
    records = [make_candidate("c-1"), make_candidate("c-2"), make_candidate("c-3")]
    first = plan_promotion(make_request(tmp_path, candidates=records))
    second = plan_promotion(make_request(tmp_path, candidates=list(reversed(records))))
    assert first.plan_hash() == second.plan_hash()
    assert first.confirmation_token() == f"{CONFIRMATION_PREFIX}{first.plan_hash()}"


def test_the_plan_names_no_private_absolute_path(tmp_path):
    plan = plan_promotion(make_request(tmp_path))
    blob = json.dumps(plan.to_record())
    assert str(tmp_path) not in blob
    assert len(plan.output_root_id) == 64


def test_the_plan_reports_a_revoked_candidate_rather_than_omitting_it(tmp_path):
    revocation = RevocationSnapshot(entries=({"candidate_id": "c-2", "reason": "leak",
                                              "actor": "operator-1", "at": NOW},))
    plan = plan_promotion(make_request(tmp_path, revocation=revocation))
    assert plan.expected_candidate_count == 2
    assert [e["candidate_id"] for e in plan.excluded] == ["c-2"]
    assert plan.excluded[0]["reason"] == "revoked"


# ── 34..36, 53..54: the confirmation ──────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "yes", "YES", "y", "true", "--force", "--confirm", "PROMOTE", "PROMOTE:",
    "promote:" + "a" * 64, "PROMOTE: " + "a" * 64, "",
])
def test_a_generic_confirmation_authorises_nothing(tmp_path, bad):
    plan = plan_promotion(make_request(tmp_path))
    with pytest.raises(ConfirmationRejected):
        check_confirmation(bad, plan)


@pytest.mark.parametrize("bad", [True, False, 1, 0, None, ["PROMOTE:x"]])
def test_a_caller_boolean_is_refused_by_type(tmp_path, bad):
    plan = plan_promotion(make_request(tmp_path))
    with pytest.raises(ConfirmationRejected, match="must be the string"):
        check_confirmation(bad, plan)


def test_a_short_hash_is_refused(tmp_path):
    plan = plan_promotion(make_request(tmp_path))
    with pytest.raises(ConfirmationRejected, match="full 64 are required"):
        check_confirmation(f"{CONFIRMATION_PREFIX}{plan.plan_hash()[:12]}", plan)


def test_a_confirmation_read_from_a_file_is_refused(tmp_path):
    plan = plan_promotion(make_request(tmp_path))
    for path_like in ("@/tmp/token", "confirmations/token.txt", "C:\\tokens\\t.txt"):
        with pytest.raises(ConfirmationRejected, match="names a file"):
            check_confirmation(path_like, plan)


def test_a_confirmation_for_a_different_plan_is_refused(tmp_path):
    first = plan_promotion(make_request(tmp_path))
    second = plan_promotion(make_request(tmp_path, version="v2"))
    assert first.plan_hash() != second.plan_hash()
    with pytest.raises(ConfirmationRejected, match="Something changed"):
        check_confirmation(second.confirmation_token(), first)


def test_a_confirmation_for_dataset_a_cannot_promote_dataset_b(tmp_path):
    a = plan_promotion(make_request(tmp_path, dataset_id="corpus"))
    b_request = make_request(tmp_path, dataset_id="other")
    with pytest.raises(ConfirmationRejected):
        promote(b_request, confirmation=a.confirmation_token(), store=store_for(tmp_path))
    assert not (tmp_path / "datasets").exists()


# ── 37..42: the plan is a signature over the state of the world ───────────────
def _rejects_after(tmp_path, **changed):
    """A confirmation issued for the baseline must not authorise the changed request."""
    baseline = plan_promotion(make_request(tmp_path))
    changed_request = make_request(tmp_path, **changed)
    with pytest.raises(ConfirmationRejected, match="Something changed"):
        promote(changed_request, confirmation=baseline.confirmation_token(),
                store=store_for(tmp_path))
    assert not (tmp_path / "datasets").exists()
    assert not (tmp_path / "promotions.jsonl").exists()


def test_a_candidate_edited_after_planning_invalidates_the_confirmation(tmp_path):
    _rejects_after(tmp_path, candidates=[make_candidate("c-1"), make_candidate("c-2"),
                                         make_candidate("c-3", target='{"v": "edited"}')])


def test_a_split_moved_after_planning_invalidates_the_confirmation(tmp_path):
    moved = dict(DEFAULT_ASSIGNMENTS)
    moved["c-3"] = DatasetSplit.TRAIN.value
    _rejects_after(tmp_path, assignments=moved)


def test_a_seed_changed_after_planning_invalidates_the_confirmation(tmp_path):
    _rejects_after(tmp_path, seed="seed-b")


def test_a_leakage_report_re_run_after_planning_invalidates_the_confirmation(tmp_path):
    _rejects_after(tmp_path, leakage_report=clean_report(
        verdict=LeakageVerdict.WARNING, notes=("a duplicate inside train",)))


def test_a_revocation_filed_after_planning_invalidates_the_confirmation(tmp_path):
    _rejects_after(tmp_path, revocation=RevocationSnapshot(entries=(
        {"candidate_id": "unrelated", "reason": "x", "actor": "op", "at": NOW},)))


def test_a_parent_appearing_after_planning_invalidates_the_confirmation(tmp_path):
    stale = plan_promotion(make_request(tmp_path, version="v2"))
    result, _ = run_promotion(tmp_path)          # v1 now exists, so v2's parent changes
    assert result.ok
    with pytest.raises(ConfirmationRejected, match="Something changed"):
        promote(make_request(tmp_path, version="v2"),
                confirmation=stale.confirmation_token(), store=store_for(tmp_path))


# ── 43: replay ────────────────────────────────────────────────────────────────
def test_a_consumed_plan_cannot_be_replayed(tmp_path):
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    result = promote(request, confirmation=plan.confirmation_token(),
                     store=store_for(tmp_path))
    assert result.ok
    assert is_plan_consumed(tmp_path, plan.plan_hash())
    # The version now exists, so re-planning refuses before the token is even considered.
    with pytest.raises(PromotionError, match="already exists"):
        promote(request, confirmation=plan.confirmation_token(),
                store=store_for(tmp_path))


def test_a_consumed_plan_is_refused_even_after_its_version_is_deleted(tmp_path):
    """The ledger is a second, independent barrier to the version directory.

    An operator who removes a version directory has not un-promoted anything — the plan
    was spent, the records moved, and re-running the same confirmation would produce a
    second version under a digest somebody already used.
    """
    import shutil

    request = make_request(tmp_path)
    plan = plan_promotion(request)
    assert promote(request, confirmation=plan.confirmation_token(),
                   store=store_for(tmp_path)).ok
    shutil.rmtree(version_dir(tmp_path, "corpus", "v1"))
    with pytest.raises(PlanAlreadyConsumed, match="already promoted"):
        promote(request, confirmation=plan.confirmation_token(),
                store=store_for(tmp_path))
    assert not version_dir(tmp_path, "corpus", "v1").exists()


def test_the_consumed_plan_ledger_is_consulted_independently_of_the_directory(tmp_path):
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    promote(request, confirmation=plan.confirmation_token(), store=store_for(tmp_path))
    entries = promotion_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["plan_hash"] == plan.plan_hash()
    assert entries[0]["dataset_version"] == "v1"
    assert entries[0]["actor"] == "operator-1"
    assert is_plan_consumed(tmp_path, plan.plan_hash())
    assert not is_plan_consumed(tmp_path, sha256_text("some other plan"))


def test_the_promotion_ledger_is_append_only(tmp_path):
    first, _ = run_promotion(tmp_path)
    assert first.ok
    second, _ = run_promotion(tmp_path, version="v2",
                              candidates=[make_candidate("d-1")],
                              assignments={"d-1": DatasetSplit.TRAIN.value})
    assert second.ok
    entries = promotion_entries(tmp_path)
    assert [e["dataset_version"] for e in entries] == ["v1", "v2"]
    assert entries[1]["parent_manifest_hash"] == entries[0]["manifest_hash"]


# ── 44..45: candidate state ───────────────────────────────────────────────────
@pytest.mark.parametrize("state", [
    CandidateState.CREATED, CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
    CandidateState.PROVENANCE_CHECKED, CandidateState.LEAKAGE_CHECKED,
    CandidateState.PROMOTED, CandidateState.REJECTED, CandidateState.NEEDS_REVISION,
    CandidateState.QUARANTINED, CandidateState.FAILED, CandidateState.REVOKED,
])
def test_only_ready_for_promotion_may_enter_a_plan(tmp_path, state):
    request = make_request(tmp_path, candidates=[
        make_candidate("c-1"), make_candidate("c-2"), make_candidate("c-3", state=state)])
    with pytest.raises(PromotionError, match="only ready_for_promotion"):
        plan_promotion(request)


def test_a_candidate_is_not_marked_promoted_before_the_version_exists(tmp_path):
    request = make_request(tmp_path)
    plan_promotion(request)
    assert all(c.state is CandidateState.READY_FOR_PROMOTION
               for c in request.candidates)
    assert store_for(tmp_path).candidate_ids() == ()


def test_a_successful_promotion_transitions_every_record_append_only(tmp_path):
    result, plan = run_promotion(tmp_path)
    assert result.ok
    assert len(result.promoted) == 3
    assert all(c.state is CandidateState.PROMOTED for c in result.promoted)
    store = store_for(tmp_path)
    assert store.candidate_ids() == ("c-1", "c-2", "c-3")
    assert all(store.read_candidate(cid).state is CandidateState.PROMOTED
               for cid in store.candidate_ids())
    moves = store.transitions()
    assert len(moves) == 3
    assert all(m["from_state"] == "ready_for_promotion" and m["to_state"] == "promoted"
               for m in moves)
    assert all(plan.proposed_dataset_version in m["reason"] for m in moves)


# ── 46..52: the effective write ───────────────────────────────────────────────
def test_a_successful_promotion_writes_shards_and_a_manifest(tmp_path):
    result, plan = run_promotion(tmp_path)
    assert result.ok
    directory = version_dir(tmp_path, "corpus", "v1")
    assert sorted(p.name for p in directory.iterdir()) == [
        "hidden_evaluation.jsonl", MANIFEST_FILENAME, "train.jsonl"]
    assert result.written is not None
    assert result.written.manifest.parent_manifest_hash == GENESIS_PARENT
    assert result.written.manifest.seed == "seed-a"
    assert result.written.manifest.leakage_report_hash == plan.leakage_report_hash


def test_the_written_bytes_are_verified_after_the_write(tmp_path):
    result, _ = run_promotion(tmp_path)
    assert result.ok
    check = verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1")
    assert check.ok, check.problems
    # Every shard digest in the manifest is the digest of the file on disk.
    directory = version_dir(tmp_path, "corpus", "v1")
    for shard in check.manifest.shards:
        import hashlib
        actual = hashlib.sha256((directory / shard.filename).read_bytes()).hexdigest()
        assert actual == shard.sha256_file


def test_the_reported_paths_are_relative_and_carry_no_home_directory(tmp_path):
    result, _ = run_promotion(tmp_path)
    for relative in result.written.relative_paths:
        assert not relative.startswith("/")
        assert str(tmp_path) not in relative
        assert relative.startswith("datasets/corpus/v1/")


def test_a_manifest_write_failure_leaves_no_version_and_no_ledger_entry(tmp_path,
                                                                       monkeypatch):
    import training_gym.datasets.manifests as manifests

    real = manifests.atomic_write_text

    def explode(path, text):
        if path.name == MANIFEST_FILENAME:
            raise OSError("simulated manifest write failure")
        return real(path, text)

    monkeypatch.setattr(manifests, "atomic_write_text", explode)
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    with pytest.raises(OSError, match="simulated manifest write failure"):
        promote(request, confirmation=plan.confirmation_token(),
                store=store_for(tmp_path))
    assert not version_dir(tmp_path, "corpus", "v1").exists()
    assert not is_plan_consumed(tmp_path, plan.plan_hash())
    assert store_for(tmp_path).candidate_ids() == ()


def test_a_partial_shard_write_never_finalizes_a_version(tmp_path, monkeypatch):
    import training_gym.datasets.manifests as manifests

    real = manifests.atomic_write_text
    written: list[str] = []

    def explode(path, text):
        if path.name == "hidden_evaluation.jsonl":
            raise OSError("simulated shard write failure")
        written.append(path.name)
        return real(path, text)

    monkeypatch.setattr(manifests, "atomic_write_text", explode)
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    with pytest.raises(OSError, match="simulated shard write failure"):
        promote(request, confirmation=plan.confirmation_token(),
                store=store_for(tmp_path))
    assert written == ["train.jsonl"]          # the first shard did land...
    assert not version_dir(tmp_path, "corpus", "v1").exists()   # ...and was cleaned up
    assert not is_plan_consumed(tmp_path, plan.plan_hash())


def test_post_write_verification_failure_is_a_failure_not_a_success(tmp_path,
                                                                    monkeypatch):
    import training_gym.datasets.manifests as manifests

    monkeypatch.setattr(manifests, "verify_version",
                        lambda **kw: manifests.VersionVerification(
                            dataset_id=kw["dataset_id"],
                            dataset_version=kw["dataset_version"],
                            problems=("simulated post-write mismatch",)))
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    with pytest.raises(manifests.ManifestError, match="re-reading it failed"):
        promote(request, confirmation=plan.confirmation_token(),
                store=store_for(tmp_path))
    assert not version_dir(tmp_path, "corpus", "v1").exists()
    assert not is_plan_consumed(tmp_path, plan.plan_hash())


def test_a_state_transition_failure_after_finalization_is_reported_not_hidden(tmp_path):
    class BrokenStore(CandidateStore):
        def write_candidate(self, candidate):
            if candidate.candidate_id == "c-2":
                raise OSError("simulated candidate write failure")
            return super().write_candidate(candidate)

    request = make_request(tmp_path)
    plan = plan_promotion(request)
    result = promote(request, confirmation=plan.confirmation_token(),
                     store=BrokenStore(tmp_path))
    assert not result.ok
    assert len(result.inconsistencies) == 1
    assert "c-2" in result.inconsistencies[0]
    assert "do not rebuild the version" in result.inconsistencies[0]
    # The version is immutable and correct; it is NOT deleted to tidy up.
    assert verify_version(root=tmp_path, dataset_id="corpus", dataset_version="v1").ok
    assert is_plan_consumed(tmp_path, plan.plan_hash())


def test_no_temporary_residue_remains_after_a_successful_promotion(tmp_path):
    result, _ = run_promotion(tmp_path)
    assert result.residue == ()
    leftovers = [p.name for p in version_dir(tmp_path, "corpus", "v1").iterdir()
                 if p.name.startswith(".tmp-") or p.name.endswith(".part")]
    assert leftovers == []


# ── the gates that must hold at plan time ─────────────────────────────────────
@pytest.mark.parametrize("verdict", [
    LeakageVerdict.BLOCKED, LeakageVerdict.INSUFFICIENT_EVIDENCE, LeakageVerdict.ERROR])
def test_a_leakage_verdict_that_does_not_permit_finalization_blocks_planning(tmp_path,
                                                                            verdict):
    request = make_request(tmp_path, leakage_report=clean_report(verdict=verdict))
    with pytest.raises(PromotionError, match="does not permit finalization"):
        plan_promotion(request)


def test_a_plan_with_no_surviving_candidate_is_refused(tmp_path):
    revocation = RevocationSnapshot(entries=tuple(
        {"candidate_id": cid, "reason": "x", "actor": "op", "at": NOW}
        for cid in ("c-1", "c-2", "c-3")))
    with pytest.raises(PromotionError, match="no candidate survived"):
        plan_promotion(make_request(tmp_path, revocation=revocation))


def test_a_plan_with_no_train_records_must_declare_it(tmp_path):
    assignments = {"c-3": DatasetSplit.HIDDEN_EVALUATION.value}
    with pytest.raises(PromotionError, match="no TRAIN records"):
        plan_promotion(make_request(tmp_path, candidates=[make_candidate("c-3")],
                                    assignments=assignments))
    plan = plan_promotion(make_request(
        tmp_path, candidates=[make_candidate("c-3")], assignments=assignments,
        allow_empty_splits=(DatasetSplit.TRAIN.value,)))
    assert plan.counts() == {"hidden_evaluation": 1}


def test_evaluation_only_material_assigned_to_training_blocks_planning(tmp_path):
    candidate = make_candidate("c-1", evaluation_only=True, dataset_eligible=False)
    with pytest.raises(PromotionError, match="held-out material"):
        plan_promotion(make_request(tmp_path, candidates=[candidate],
                                    assignments={"c-1": DatasetSplit.TRAIN.value}))


def test_a_quarantine_assignment_never_reaches_a_version(tmp_path):
    with pytest.raises(PromotionError, match="never written into a dataset version"):
        plan_promotion(make_request(
            tmp_path, candidates=[make_candidate("c-1")],
            assignments={"c-1": DatasetSplit.QUARANTINE.value}))


def test_the_store_root_and_the_output_root_must_agree(tmp_path):
    request = make_request(tmp_path)
    plan = plan_promotion(request)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(PromotionError, match="store root and the requested output root"):
        promote(request, confirmation=plan.confirmation_token(), store=CandidateStore(other))


def test_a_store_missing_the_transition_ledger_is_refused(tmp_path):
    class HalfStore:
        root = tmp_path

        def write_candidate(self, candidate):  # pragma: no cover — never reached
            return ""

    request = make_request(tmp_path)
    plan = plan_promotion(request)
    with pytest.raises(PromotionError, match="must provide record_transition"):
        promote(request, confirmation=plan.confirmation_token(), store=HalfStore())


# ── 55: no network, no model, no training ─────────────────────────────────────
def test_promotion_touches_no_network_and_downloads_nothing(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):  # pragma: no cover — the point is it never runs
        raise AssertionError("promotion attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    result, _ = run_promotion(tmp_path)
    assert result.ok


def test_promoting_pulls_in_no_training_or_model_machinery(tmp_path):
    """A full promotion must not load a trainer, a tokenizer or a model library.

    Asserted against ``sys.modules`` AFTER a real promotion has run, rather than by
    reading the import list: a lazy import inside a function is exactly the shape this
    check exists to catch, and it is invisible to a static reading of the module.
    """
    import sys

    banned = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
              "bitsandbytes", "sentence_transformers", "huggingface_hub", "tokenizers")
    already = {name for name in banned if name in sys.modules}
    result, _ = run_promotion(tmp_path)
    assert result.ok
    assert {name for name in banned if name in sys.modules} == already
