"""V69 M62 S3Q.0 — a one-shot measurement whose evidence is missing is not a success.

THE TWO DEFECTS REPRODUCED HERE
-------------------------------
1. TERMINAL LEDGER. ``_record`` caught every exception from ``record_terminal``, appended
   a sentence to ``problems``, and returned. ``ExecutionOutcome.ok`` was
   ``state is COMPLETED`` and nothing else, so the CLI printed the artefacts and exited
   zero. An evaluation that can happen exactly once had no durable record of how it
   ended, and the operator was told it went fine.

2. PLAN LEDGER. ``consume_plan`` was called outside every ``try`` in
   ``execute_evaluation``, one line after the generation directory was created. An append
   failure escaped as a bare exception, the plan stayed unspent, and the directory left
   behind made every later attempt at that generation refuse — a self-inflicted deadlock
   reachable from a full disk.

WHAT SUCCESS NOW REQUIRES
-------------------------
``ok`` means the measurement completed AND the holdout commit is durable AND the terminal
line is durable AND no durability-critical obligation failed. Diagnostics and gate
blockers are unaffected: they belong in the report, and a warning is not a lost guarantee.

RECOVERY IS NOT RERUN
---------------------
After a durability failure the artefacts are retained, the plan stays spent, the holdout
stays spent, and the command reports a recovery condition. Nothing here retries, repairs
or deletes anything.
"""
from __future__ import annotations

import pytest

from training_gym.evaluation.config import EvaluationRunState
from training_gym.evaluation.store import (
    EvaluationStoreError,
    is_holdout_committed,
    is_plan_consumed,
)

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0durab")
    S.build(root)
    return root


# ══════════════════════════════════════════════════════════════════════════════
#  F13 — terminal ledger failure after a valid measurement
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def terminal_failed(dataset_root, tmp_path, monkeypatch):
    import training_gym.evaluation.execution as X

    def refuse(*_args, **_kwargs):
        raise EvaluationStoreError("the ledger could not be appended")

    monkeypatch.setattr(X, "record_terminal", refuse)
    root = tmp_path / "terminalfail"
    return S.run_synthetic(dataset_root, root), root


def test_the_measurement_itself_completed(terminal_failed):
    outcome, _root = terminal_failed
    assert outcome.state is EvaluationRunState.COMPLETED
    assert outcome.measured_pairs > 0


def test_a_lost_terminal_line_is_not_a_clean_success(terminal_failed):
    """The old behaviour: ``ok`` was true here and the CLI exited zero."""
    outcome, _root = terminal_failed
    assert outcome.terminal_recorded is False
    assert outcome.ok is False
    assert outcome.durability_problems


def test_a_lost_terminal_line_is_reported_as_recovery_and_not_as_failure(
        terminal_failed):
    outcome, _root = terminal_failed
    assert outcome.recovery_required is True
    assert any("RECOVERY REQUIRED" in p for p in outcome.problems), outcome.problems


def test_a_lost_terminal_line_never_permits_a_rerun(terminal_failed):
    outcome, root = terminal_failed
    assert outcome.holdout_committed is True
    assert outcome.rerun_permitted is False
    assert is_holdout_committed(root, outcome.plan_hash)
    assert is_plan_consumed(root, outcome.plan_hash)


def test_the_artifacts_of_a_lost_terminal_line_are_retained(terminal_failed):
    """Valid evidence is never discarded because its ledger line went missing."""
    outcome, _root = terminal_failed
    assert outcome.directory is not None and outcome.directory.is_dir()
    assert (outcome.directory / "evaluation-report.json").is_file()
    assert outcome.quarantine_path is None


def test_a_durability_problem_is_not_confused_with_a_diagnostic(dataset_root, tmp_path):
    """A clean run has gate blockers and limitations, and no durability problem."""
    outcome = S.run_synthetic(dataset_root, tmp_path / "clean")
    assert outcome.ok is True
    assert outcome.durability_problems == ()
    # The synthetic corpus is smaller than the policy minimum, so it legitimately
    # carries blockers — and those do not touch `ok`.
    assert outcome.blockers


# ══════════════════════════════════════════════════════════════════════════════
#  F3 — the plan ledger fails after the directory exists
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def start_failed(dataset_root, tmp_path, monkeypatch):
    import training_gym.evaluation.execution as X

    def refuse(*_args, **_kwargs):
        raise EvaluationStoreError("the run ledger could not be appended")

    monkeypatch.setattr(X, "consume_plan", refuse)
    root = tmp_path / "startfail"
    return S.run_synthetic(dataset_root, root), root


def test_a_plan_ledger_failure_is_a_structured_outcome(start_failed):
    """It used to escape as a bare exception after mutating the filesystem."""
    outcome, _root = start_failed
    assert outcome.state is EvaluationRunState.FAILED
    assert any("could not be spent" in p for p in outcome.problems), outcome.problems


def test_a_plan_ledger_failure_spends_nothing(start_failed):
    outcome, root = start_failed
    assert outcome.plan_consumed is False
    assert outcome.holdout_committed is False
    assert outcome.rerun_permitted is True
    assert not is_plan_consumed(root, outcome.plan_hash)
    assert not is_holdout_committed(root, outcome.plan_hash)


def test_a_plan_ledger_failure_calls_no_backend(dataset_root, tmp_path, monkeypatch):
    import training_gym.evaluation.execution as X

    seen: list[str] = []

    class CountingBackend(S.CanaryBackend):
        def generate(self, request):
            seen.append(request.task.task_id)
            return super().generate(request)

    def refuse(*_args, **_kwargs):
        raise EvaluationStoreError("the run ledger could not be appended")

    monkeypatch.setattr(X, "consume_plan", refuse)
    S.run_synthetic(dataset_root, tmp_path / "nocalls",
                    factory=lambda _role: CountingBackend())
    assert seen == []


def test_a_plan_ledger_failure_withdraws_the_empty_directory_it_created(start_failed):
    """The orphan that used to block every later attempt at this generation."""
    outcome, root = start_failed
    assert outcome.directory is None
    assert any("removed" in p for p in outcome.problems), outcome.problems
    generation = root / "evaluations" / "s3q0-synthetic-ceremony" / "gen-1"
    assert not generation.exists()


def test_the_generation_can_still_be_attempted_after_a_withdrawn_directory(
        dataset_root, tmp_path, monkeypatch):
    """The deadlock is gone: a failed start does not consume the generation number."""
    import training_gym.evaluation.execution as X

    root = tmp_path / "recoverable"
    real = X.consume_plan
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EvaluationStoreError("the run ledger could not be appended")
        return real(*args, **kwargs)

    monkeypatch.setattr(X, "consume_plan", flaky)
    first = S.run_synthetic(dataset_root, root)
    assert first.state is EvaluationRunState.FAILED
    assert first.plan_consumed is False

    # A deliberate operator-initiated second attempt at the SAME generation, which the
    # orphan directory used to make impossible. Nothing here retried automatically.
    second = S.run_synthetic(dataset_root, root)
    assert second.ok, second.problems
    assert second.holdout_committed is True


def test_a_non_empty_directory_is_never_deleted(dataset_root, tmp_path, monkeypatch):
    """``rmdir`` only. Anything with a file in it might be somebody's evidence."""
    import training_gym.evaluation.execution as X

    root = tmp_path / "notempty"
    generation = root / "evaluations" / "s3q0-synthetic-ceremony" / "gen-1"

    def refuse(*_args, **_kwargs):
        # Something wrote into the fresh directory before the ledger failed.
        (generation / "stray.json").write_text("{}", encoding="utf-8")
        raise EvaluationStoreError("the run ledger could not be appended")

    monkeypatch.setattr(X, "consume_plan", refuse)
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.state is EvaluationRunState.FAILED
    assert (generation / "stray.json").is_file(), "no file may be deleted here"
    assert any("not empty" in p for p in outcome.problems), outcome.problems


def test_the_withdrawal_is_rmdir_and_never_a_recursive_delete():
    """Read from source: an evaluation subsystem must not be able to delete a tree."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "training_gym" / "evaluation"
              / "execution.py").read_text(encoding="utf-8")
    assert "rmdir()" in source
    for forbidden in ("shutil.rmtree", "rmtree(", "unlink(", "os.remove"):
        assert forbidden not in source, forbidden


# ══════════════════════════════════════════════════════════════════════════════
#  F2 — the directory cannot be created at all
# ══════════════════════════════════════════════════════════════════════════════
def test_a_generation_that_already_exists_spends_nothing(dataset_root, tmp_path):
    root = tmp_path / "taken"
    (root / "evaluations" / "s3q0-synthetic-ceremony" / "gen-1").mkdir(parents=True)
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.state is EvaluationRunState.FAILED
    assert outcome.plan_consumed is False
    assert outcome.holdout_committed is False
    assert S.ledger_lines(root) == []


# ══════════════════════════════════════════════════════════════════════════════
#  F12 — artifact validation failure
# ══════════════════════════════════════════════════════════════════════════════
def test_a_generation_whose_artifacts_do_not_verify_is_quarantined_and_stays_spent(
        dataset_root, tmp_path, monkeypatch):
    import training_gym.evaluation.execution as X

    monkeypatch.setattr(X, "verify_evaluation_generation",
                        lambda *_a, **_k: ("a synthetic verification failure",))
    root = tmp_path / "badartifacts"
    outcome = S.run_synthetic(dataset_root, root)
    assert outcome.state is EvaluationRunState.QUARANTINED
    assert outcome.ok is False
    assert outcome.holdout_committed is True
    assert outcome.rerun_permitted is False
    assert outcome.quarantine_path is not None
