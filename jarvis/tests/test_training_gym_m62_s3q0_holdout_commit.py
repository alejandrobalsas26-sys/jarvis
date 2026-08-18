"""V69 M62 S3Q.0 — when exactly does a fresh holdout become scientifically spent?

THE PROSPECTIVE RULING BEING QUALIFIED
--------------------------------------
A fresh holdout becomes ``USED_IMMUTABLE`` when the evaluator DURABLY commits the first
held-out request to the model-facing backend boundary — after both arms' requests exist
and their parity is proved, and immediately before the first ``backend.generate``.

Deliberately a shade earlier than proof that a forward pass ran. There is no atomic
transaction between a local append and an external synchronous call, so one of the two
gaps has to be taken on faith, and the fail-closed choice is to assume the model read the
material rather than to assume it did not.

It applies PROSPECTIVELY. Candidate 001 and candidate 002 were measured before this event
existed, their ledgers are sealed, and nothing here reinterprets them.

WHAT THIS FILE PROVES
---------------------
Ordering, uniqueness, body-freedom, fail-closed append, and the failure matrix around the
boundary — every scenario driven through the REAL execution path over synthetic material.
No model, no ``eval-v4``, no live authority.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.store import (
    HOLDOUT_COMMIT_EVENT,
    HOLDOUT_COMMIT_FIELDS,
    HOLDOUT_COMMIT_RECORD_VERSION,
    EvaluationStoreError,
    HoldoutAlreadyCommitted,
    consume_plan,
    is_holdout_committed,
    is_plan_consumed,
    record_holdout_commit,
)

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0commit")
    S.build(root)
    return root


@pytest.fixture(scope="module")
def completed(dataset_root, tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0done")
    outcome = S.run_synthetic(dataset_root, root)
    return outcome, root


# ══════════════════════════════════════════════════════════════════════════════
#  F14 — the complete synthetic evaluation
# ══════════════════════════════════════════════════════════════════════════════
def test_a_complete_run_records_exactly_one_of_each_event(completed):
    outcome, root = completed
    assert outcome.state is EvaluationRunState.COMPLETED, outcome.problems
    events = [e["event"] for e in S.ledger_lines(root)]
    assert events.count("started") == 1
    assert events.count(HOLDOUT_COMMIT_EVENT) == 1
    assert events.count("completed") == 1
    assert len(events) == 3


def test_a_complete_run_reports_every_durability_fact_separately(completed):
    outcome, _root = completed
    assert outcome.plan_consumed is True
    assert outcome.holdout_committed is True
    assert outcome.terminal_recorded is True
    assert outcome.durability_problems == ()
    assert outcome.ok is True
    assert outcome.recovery_required is False
    assert outcome.rerun_permitted is False


def test_the_commit_precedes_the_terminal_line_in_the_ledger(completed):
    _outcome, root = completed
    events = [e["event"] for e in S.ledger_lines(root)]
    assert events.index("started") < events.index(HOLDOUT_COMMIT_EVENT)
    assert events.index(HOLDOUT_COMMIT_EVENT) < events.index("completed")


# ══════════════════════════════════════════════════════════════════════════════
#  The exact position — after parity, before the first backend call
# ══════════════════════════════════════════════════════════════════════════════
def test_the_commit_happens_before_any_backend_is_called(dataset_root, tmp_path):
    """The instrumented order trace, in one run.

    The property is not "the commit happened" — it is that NO generation preceded it. A
    marker written after the first call would be a marker for a call that may already
    have happened without one.
    """
    from training_gym.evaluation.runner import run_paired_evaluation

    trace: list[str] = []

    class TracingBackend(S.CanaryBackend):
        def generate(self, request):
            trace.append(f"generate:{request.role.value}")
            return super().generate(request)

    config = S.make_config()
    identity = S.pack_identity(dataset_root, config)
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)

    def marker(first: dict) -> None:
        trace.append("holdout_commit")
        trace.append(f"first_arm:{first['first_arm']}")

    run_paired_evaluation(
        built.pack, baseline_backend=TracingBackend(),
        candidate_backend=TracingBackend(), baseline_reference=baseline,
        adapter_reference=adapter, generation=config.generation, seed=config.seed,
        before_first_model_facing_invoke=marker)

    assert trace[0] == "holdout_commit"
    assert trace[1].startswith("first_arm:")
    assert trace[2].startswith("generate:")
    # And the arm the marker named is the arm that actually went first.
    assert trace[2] == f"generate:{trace[1].split(':')[1]}"
    assert trace.count("holdout_commit") == 1
    assert identity.task_count * 2 == sum(1 for e in trace if e.startswith("generate:"))


def test_the_callback_fires_once_however_many_tasks_there_are(dataset_root):
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.runner import run_paired_evaluation

    calls: list[dict] = []
    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    baseline = S.make_baseline()
    run_paired_evaluation(
        built.pack, baseline_backend=S.CanaryBackend(),
        candidate_backend=S.CanaryBackend(), baseline_reference=baseline,
        adapter_reference=S.make_adapter(baseline), generation=config.generation,
        seed=config.seed, before_first_model_facing_invoke=calls.append)
    assert len(calls) == 1
    assert len(built.pack) > 1


def test_the_callback_is_optional_so_legacy_callers_are_unaffected(dataset_root):
    """Historical S3I and S3L ran without it and the runner still works without it."""
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.runner import run_paired_evaluation

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    baseline = S.make_baseline()
    run = run_paired_evaluation(
        built.pack, baseline_backend=S.CanaryBackend(),
        candidate_backend=S.CanaryBackend(), baseline_reference=baseline,
        adapter_reference=S.make_adapter(baseline), generation=config.generation,
        seed=config.seed)
    assert len(run) == len(built.pack)


def test_the_callback_does_not_enter_the_measurement(dataset_root):
    """Wiring the marker changes no parity hash and no recorded figure."""
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.runner import run_paired_evaluation

    config = S.make_config()
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1)
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)

    def go(**extra):
        return run_paired_evaluation(
            built.pack, baseline_backend=S.CanaryBackend(),
            candidate_backend=S.CanaryBackend(), baseline_reference=baseline,
            adapter_reference=adapter, generation=config.generation, seed=config.seed,
            **extra)

    without = go()
    with_marker = go(before_first_model_facing_invoke=lambda _first: None)
    assert without.run_hash() == with_marker.run_hash()
    assert without.order_assignment_hash == with_marker.order_assignment_hash


# ══════════════════════════════════════════════════════════════════════════════
#  F8 — a commit that cannot be recorded stops the run before any model call
# ══════════════════════════════════════════════════════════════════════════════
def test_a_failed_commit_append_prevents_every_backend_call(dataset_root, tmp_path,
                                                            monkeypatch):
    seen: list[str] = []

    class CountingBackend(S.CanaryBackend):
        def generate(self, request):
            seen.append(request.task.task_id)
            return super().generate(request)

    import training_gym.evaluation.execution as X

    def refuse(*_args, **_kwargs):
        raise EvaluationStoreError("the ledger is unwritable")

    monkeypatch.setattr(X, "record_holdout_commit", refuse)
    root = tmp_path / "commitfail"
    outcome = S.run_synthetic(dataset_root, root,
                              factory=lambda _role: CountingBackend())

    assert seen == [], "no held-out task may reach a backend once the commit failed"
    assert outcome.state is EvaluationRunState.FAILED
    assert outcome.plan_consumed is True
    assert outcome.holdout_committed is False
    assert outcome.rerun_permitted is True
    assert not is_holdout_committed(root, outcome.plan_hash)
    assert is_plan_consumed(root, outcome.plan_hash)


# ══════════════════════════════════════════════════════════════════════════════
#  F9 / F11 — committed, then something goes wrong
# ══════════════════════════════════════════════════════════════════════════════
def test_a_backend_that_raises_after_the_commit_leaves_the_holdout_spent(dataset_root,
                                                                         tmp_path):
    root = tmp_path / "afterfail"
    outcome = S.run_synthetic(dataset_root, root,
                              factory=S.canary_factory(fail_first=True))
    assert outcome.holdout_committed is True
    assert outcome.rerun_permitted is False
    assert is_holdout_committed(root, outcome.plan_hash)
    # The failure is a datum: the arm is recorded, not dropped, and the run continues.
    assert outcome.task_count > 0


def test_a_second_run_of_the_same_plan_is_refused(dataset_root, tmp_path):
    root = tmp_path / "replay"
    first = S.run_synthetic(dataset_root, root)
    assert first.ok
    second = S.run_synthetic(dataset_root, root)
    assert second.state is EvaluationRunState.FAILED
    assert any("already started" in p for p in second.problems), second.problems
    assert len(S.commit_lines(root)) == 1


# ══════════════════════════════════════════════════════════════════════════════
#  F15 — uniqueness, checked at the ledger and not merely by the caller
# ══════════════════════════════════════════════════════════════════════════════
def _commit_body(**overrides):
    body = {name: "" for name in HOLDOUT_COMMIT_FIELDS}
    body.update({
        "commit_schema_version": "m62.evaluation_holdout_commit_body.1",
        "dataset_id": S.DATASET_ID, "dataset_version": S.DATASET_VERSION,
        "dataset_manifest_hash": "a" * 64, "task_pack_hash": "b" * 64,
        "hidden_target_store_hash": "c" * 64, "pack_identity_hash": "d" * 64,
        "order_policy": "balanced_by_task_hash_and_seed",
        "order_assignment_hash": "e" * 64, "task_count": 16, "target_count": 16,
        "first_task_id": "s3q0-hid-stru-000", "first_task_hash": "f" * 64,
        "first_arm": "baseline", "first_request_parity_hash": "0" * 64,
        "baseline_reference_hash": "1" * 64,
        "candidate_adapter_reference_hash": "2" * 64,
        "generation_policy_hash": "3" * 64, "backend_id": "fake_evaluation",
        "performs_inference": True,
    })
    body.update(overrides)
    return body


def _start(root, digest, *, evaluation_id="e1", generation=1):
    consume_plan(root, plan_hash=digest, evaluation_id=evaluation_id,
                 generation=generation, actor="local-operator", at=S.NOW)


def test_one_plan_may_commit_a_holdout_only_once(tmp_path):
    digest = "a" * 64
    _start(tmp_path, digest)
    record_holdout_commit(tmp_path, plan_hash=digest, evaluation_id="e1", generation=1,
                          actor="local-operator", at=S.NOW, commit=_commit_body())
    with pytest.raises(HoldoutAlreadyCommitted, match="no second crossing"):
        record_holdout_commit(tmp_path, plan_hash=digest, evaluation_id="e1",
                              generation=1, actor="local-operator", at=S.NOW,
                              commit=_commit_body())
    assert len(S.commit_lines(tmp_path)) == 1


def test_one_generation_may_not_commit_under_two_different_plans(tmp_path):
    first, second = "a" * 64, "b" * 64
    _start(tmp_path, first)
    _start(tmp_path, second)
    record_holdout_commit(tmp_path, plan_hash=first, evaluation_id="e1", generation=1,
                          actor="local-operator", at=S.NOW, commit=_commit_body())
    with pytest.raises(HoldoutAlreadyCommitted, match="already committed"):
        record_holdout_commit(tmp_path, plan_hash=second, evaluation_id="e1",
                              generation=1, actor="local-operator", at=S.NOW,
                              commit=_commit_body())


def test_a_commit_for_a_plan_that_never_started_is_refused(tmp_path):
    """Ordering, enforced by the ledger rather than trusted of the caller."""
    with pytest.raises(EvaluationStoreError, match="no start line"):
        record_holdout_commit(tmp_path, plan_hash="a" * 64, evaluation_id="e1",
                              generation=1, actor="local-operator", at=S.NOW,
                              commit=_commit_body())
    assert S.commit_lines(tmp_path) == []


def test_the_commit_body_is_a_closed_field_list(tmp_path):
    digest = "a" * 64
    _start(tmp_path, digest)
    with pytest.raises(EvaluationStoreError, match="closed body-free field list"):
        record_holdout_commit(
            tmp_path, plan_hash=digest, evaluation_id="e1", generation=1,
            actor="local-operator", at=S.NOW,
            commit=_commit_body(user_prompt="a held-out question"))


def test_an_incomplete_commit_body_is_refused(tmp_path):
    digest = "a" * 64
    _start(tmp_path, digest)
    partial = _commit_body()
    partial.pop("first_task_hash")
    with pytest.raises(EvaluationStoreError, match="omits"):
        record_holdout_commit(tmp_path, plan_hash=digest, evaluation_id="e1",
                              generation=1, actor="local-operator", at=S.NOW,
                              commit=partial)


# ══════════════════════════════════════════════════════════════════════════════
#  The event is body-free, and says what it can prove
# ══════════════════════════════════════════════════════════════════════════════
def test_the_commit_line_carries_no_canary(completed):
    _outcome, root = completed
    line = json.dumps(S.commit_lines(root)[0], sort_keys=True)
    assert S.leaked_canaries(line) == []


def test_the_commit_line_binds_the_exact_identities(dataset_root, completed):
    _outcome, root = completed
    commit = S.commit_lines(root)[0]["commit"]
    identity = S.pack_identity(dataset_root, S.make_config())
    assert commit["task_pack_hash"] == identity.pack_hash
    assert commit["hidden_target_store_hash"] == identity.hidden_target_store_hash
    assert commit["order_assignment_hash"] == identity.order_assignment_hash
    assert commit["pack_identity_hash"] == identity.identity_hash()
    assert commit["task_count"] == identity.task_count
    assert commit["dataset_id"] == S.DATASET_ID


def test_the_commit_line_names_the_first_crossing(completed):
    _outcome, root = completed
    commit = S.commit_lines(root)[0]["commit"]
    assert commit["first_task_id"]
    assert len(commit["first_task_hash"]) == 64
    assert commit["first_arm"] in {"baseline", "candidate"}
    assert len(commit["first_request_parity_hash"]) == 64


def test_the_commit_line_declares_its_own_record_version(completed):
    _outcome, root = completed
    entry = S.commit_lines(root)[0]
    assert entry["record_version"] == HOLDOUT_COMMIT_RECORD_VERSION
    assert entry["event"] == HOLDOUT_COMMIT_EVENT


def test_the_event_name_does_not_overclaim():
    """It cannot prove a forward pass ran, and it must not be named as though it can."""
    assert HOLDOUT_COMMIT_EVENT == "holdout_model_facing_committed"
    assert "model_read" != HOLDOUT_COMMIT_EVENT
    assert "exposed" not in HOLDOUT_COMMIT_EVENT


# ══════════════════════════════════════════════════════════════════════════════
#  Legacy compatibility
# ══════════════════════════════════════════════════════════════════════════════
def test_a_legacy_ledger_without_commit_lines_still_reads(tmp_path):
    """S3I and S3L predate the event. Their ledgers stay valid and unrewritten."""
    from training_gym.evaluation.store import evaluation_entries, record_terminal

    digest = "a" * 64
    _start(tmp_path, digest)
    record_terminal(tmp_path, plan_hash=digest, evaluation_id="e1", generation=1,
                    actor="local-operator", at=S.NOW,
                    state=EvaluationRunState.COMPLETED)
    assert is_plan_consumed(tmp_path, digest) is True
    assert is_holdout_committed(tmp_path, digest) is False
    assert len(evaluation_entries(tmp_path)) == 2


def test_the_new_event_does_not_change_what_consumes_a_plan(tmp_path):
    """``is_plan_consumed`` reads ``started`` and nothing else, before and after."""
    digest = "a" * 64
    _start(tmp_path, digest)
    record_holdout_commit(tmp_path, plan_hash=digest, evaluation_id="e1", generation=1,
                          actor="local-operator", at=S.NOW, commit=_commit_body())
    assert is_plan_consumed(tmp_path, digest) is True
    assert is_holdout_committed(tmp_path, digest) is True


def test_plan_consumed_without_a_commit_is_a_distinguishable_state(tmp_path):
    """Neither "nothing happened" nor "the holdout is spent". A third, real thing."""
    digest = "a" * 64
    _start(tmp_path, digest)
    assert is_plan_consumed(tmp_path, digest) is True
    assert is_holdout_committed(tmp_path, digest) is False


# ══════════════════════════════════════════════════════════════════════════════
#  F40 — no model-facing retries
# ══════════════════════════════════════════════════════════════════════════════
def test_each_task_arm_is_asked_exactly_once(dataset_root, tmp_path):
    """Verified, not assumed: a retry would measure a held-out task twice."""
    calls: list[tuple[str, str]] = []

    class CountingBackend(S.CanaryBackend):
        def generate(self, request):
            calls.append((request.role.value, request.task.task_id))
            return super().generate(request)

    outcome = S.run_synthetic(dataset_root, tmp_path / "once",
                              factory=lambda _role: CountingBackend())
    assert outcome.ok
    assert len(calls) == len(set(calls)) == outcome.task_count * 2


def test_a_backend_that_raises_is_recorded_and_not_retried(dataset_root, tmp_path):
    calls: list[str] = []

    class ExplodingBackend(S.CanaryBackend):
        def generate(self, request):
            calls.append(request.task.task_id)
            raise RuntimeError("synthetic backend failure")

    def factory(role):
        return ExplodingBackend() if role == "candidate" else S.CanaryBackend()

    outcome = S.run_synthetic(dataset_root, tmp_path / "noretry", factory=factory)
    assert len(calls) == outcome.task_count, "one call per task, never two"
    assert outcome.holdout_committed is True
