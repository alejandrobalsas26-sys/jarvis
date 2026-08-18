"""V69 M62 S3Q.0 — the approved plan and the measured material must be the same thing.

THE GAP THIS CLOSES
-------------------
A confirmation is checked against the plan recomputed from the world as it stands at
``--execute`` time, which stops a token authorising a run whose *inputs* changed. It did
not stop a run whose *pack* changed, because until S3Q.0 the plan bound a digest of the
dataset manifest rather than the pack, and execution compared the two to nothing at all.
Two different packs from one manifest — a builder change, a grader-registry change, a
schema change — produced the same confirmation and no complaint.

Execution now re-derives the pack, the answer key, the execution order, both references
and the generation policy immediately before the boundary, and refuses on any mismatch
while the holdout is still unread.

NON-VACUITY
-----------
Every field is mutated independently and the refusal is required to name it. A single
test that mutated one field would pass against an implementation that checked only that
field, which is exactly the kind of comparison this milestone exists to distrust.
"""
from __future__ import annotations

import pytest

from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.preflight import (
    execution_binding_mismatches,
    plan_binding_mismatches,
)
from training_gym.evaluation.store import is_holdout_committed, is_plan_consumed

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0bind")
    S.build(root)
    return root


@pytest.fixture(scope="module")
def world(dataset_root):
    config = S.make_config()
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)
    identity = S.pack_identity(dataset_root, config)
    return config, baseline, adapter, identity


# ══════════════════════════════════════════════════════════════════════════════
#  The comparison itself
# ══════════════════════════════════════════════════════════════════════════════
def test_a_matching_plan_reports_no_mismatch(world):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    assert plan_binding_mismatches(plan, identity) == ()
    assert execution_binding_mismatches(
        plan=plan, identity=identity, baseline=baseline, adapter=adapter,
        generation_policy=config.generation) == ()


@pytest.mark.parametrize("field,replacement", [
    ("task_pack_hash", "9" * 64),
    ("hidden_target_store_hash", "9" * 64),
    ("order_assignment_hash", "9" * 64),
    ("dataset_manifest_hash", "9" * 64),
    ("expected_task_count", 999),
])
def test_every_bound_pack_identity_is_compared_independently(world, field,
                                                             replacement):
    config, baseline, adapter, identity = world
    overrides = {field: replacement}
    if field == "expected_task_count":
        overrides.update({"expected_baseline_generations": replacement,
                          "expected_candidate_generations": replacement,
                          "expected_grader_executions": replacement * 6})
    plan = S.make_plan(config, baseline, adapter, identity, **overrides)
    problems = plan_binding_mismatches(plan, identity)
    assert problems, f"{field} was not compared"
    assert any(field in p for p in problems), problems


@pytest.mark.parametrize("field,replacement", [
    ("baseline_reference_hash", "9" * 64),
    ("candidate_adapter_reference_hash", "9" * 64),
    ("tokenizer_identity_hash", "9" * 64),
    ("generation_policy_hash", "9" * 64),
])
def test_every_bound_reference_and_policy_is_compared_independently(world, field,
                                                                    replacement):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity, **{field: replacement})
    problems = execution_binding_mismatches(
        plan=plan, identity=identity, baseline=baseline, adapter=adapter,
        generation_policy=config.generation)
    assert any(field in p for p in problems), problems


def test_a_moved_adapter_is_caught_even_when_the_pack_is_identical(world):
    """The one thing a paired evaluation is measuring may not change silently."""
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    moved = S.make_adapter(baseline, adapter_manifest_hash="c" * 64)
    problems = execution_binding_mismatches(
        plan=plan, identity=identity, baseline=baseline, adapter=moved,
        generation_policy=config.generation)
    assert any("candidate_adapter_reference_hash" in p for p in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  F4-F7 — the refusal happens through the real execution path, before the model
# ══════════════════════════════════════════════════════════════════════════════
def _drift_run(dataset_root, root, world, **overrides):
    config, baseline, adapter, identity = world
    seen: list[str] = []

    class CountingBackend(S.CanaryBackend):
        def generate(self, request):
            seen.append(request.task.task_id)
            return super().generate(request)

    plan = S.make_plan(config, baseline, adapter, identity, **overrides)
    outcome = S.run_synthetic(dataset_root, root, config=config, plan=plan,
                              baseline=baseline, adapter=adapter,
                              factory=lambda _role: CountingBackend())
    return outcome, seen


@pytest.mark.parametrize("field", [
    "task_pack_hash", "hidden_target_store_hash", "order_assignment_hash",
    "dataset_manifest_hash",
])
def test_a_drifted_identity_refuses_before_the_holdout_is_committed(
        dataset_root, tmp_path, world, field):
    outcome, seen = _drift_run(dataset_root, tmp_path / field, world,
                               **{field: "9" * 64})
    assert outcome.state is EvaluationRunState.FAILED
    assert seen == [], "no held-out task may reach a model after a binding mismatch"
    assert outcome.plan_consumed is True
    assert outcome.holdout_committed is False
    assert outcome.rerun_permitted is True
    assert any("does not describe the material" in p for p in outcome.problems), \
        outcome.problems


def test_a_drifted_baseline_reference_refuses_before_the_commit(dataset_root, tmp_path,
                                                                world):
    outcome, seen = _drift_run(dataset_root, tmp_path / "baseline", world,
                               baseline_reference_hash="9" * 64)
    assert outcome.state is EvaluationRunState.FAILED
    assert seen == []
    assert outcome.holdout_committed is False


def test_a_drifted_generation_policy_refuses_before_the_commit(dataset_root, tmp_path,
                                                               world):
    """A decoding policy nobody approved would measure a different model behaviour."""
    outcome, seen = _drift_run(dataset_root, tmp_path / "genpolicy", world,
                               generation_policy_hash="9" * 64)
    assert outcome.state is EvaluationRunState.FAILED
    assert seen == []
    assert outcome.holdout_committed is False


def test_a_refused_binding_leaves_the_plan_spent_and_the_holdout_unspent(
        dataset_root, tmp_path, world):
    """The honest middle state: the approval is gone, the exam is untouched."""
    root = tmp_path / "middle"
    outcome, _seen = _drift_run(dataset_root, root, world, task_pack_hash="9" * 64)
    assert is_plan_consumed(root, outcome.plan_hash)
    assert not is_holdout_committed(root, outcome.plan_hash)
    assert S.commit_lines(root) == []


def test_nothing_retries_after_a_refused_binding(dataset_root, tmp_path, world):
    root = tmp_path / "noretry"
    _outcome, seen = _drift_run(dataset_root, root, world, task_pack_hash="9" * 64)
    assert seen == []
    # One start line and one terminal line: the run was attempted once and stopped.
    events = [e["event"] for e in S.ledger_lines(root)]
    assert events.count("started") == 1
    assert events.count("failed") == 1


# ══════════════════════════════════════════════════════════════════════════════
#  F4 — the pack cannot be rebuilt at all, after the plan is spent
# ══════════════════════════════════════════════════════════════════════════════
def test_a_pack_that_cannot_be_rebuilt_spends_the_plan_and_not_the_holdout(
        dataset_root, tmp_path, monkeypatch):
    import training_gym.evaluation.execution as X

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the shard could not be read")

    monkeypatch.setattr(X, "build_task_pack_from_dataset", refuse)
    root = tmp_path / "nopack"
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.state is EvaluationRunState.FAILED
    assert outcome.plan_consumed is True
    assert outcome.holdout_committed is False
    assert outcome.rerun_permitted is True
    assert S.commit_lines(root) == []


# ══════════════════════════════════════════════════════════════════════════════
#  One implementation authority
# ══════════════════════════════════════════════════════════════════════════════
def test_planning_and_execution_derive_the_identity_through_one_function():
    """Two formulas for one identity is the defect, not the fix for it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    planner = (root / "scripts" / "evaluate_adapter.py").read_text(encoding="utf-8")
    execution = (root / "training_gym" / "evaluation"
                 / "execution.py").read_text(encoding="utf-8")
    assert "prepare_pack_identity" in planner
    assert "derive_pack_identity" in execution
    # Neither recomputes a pack, store or order digest of its own.
    for source in (planner, execution):
        assert "hashlib.sha256" not in source


def test_the_execution_check_runs_before_the_runner_is_called():
    """Read from source: the ordering is structural, not a comment."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "training_gym" / "evaluation"
              / "execution.py").read_text(encoding="utf-8")
    assert source.index("execution_binding_mismatches") < \
        source.index("run_paired_evaluation(")
