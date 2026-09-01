"""V69 M62 S4E — the four exactly-once properties, tested as four separate properties.

    EVAL AUTHORITY   created once, consumed once
    HOLDOUT          spent once
    PAIRED ATTEMPT   one
    GENERATIONS      one per arm per task

They are tested separately because they FAIL separately. A run can spend its plan and
read nothing; it can record an attempt and crash before the first model call; it can
generate perfectly and lose the record of having done so. Collapsing any pair of them is
how a spent holdout gets re-spent.

Nothing here loads a model, generates a token, or spends a real holdout. Every ledger is
a temporary directory.
"""
from __future__ import annotations

import json

import pytest

from training_gym.evaluation import store_v4 as SV4
from training_gym.evaluation.plan_v4 import (
    PlanV4ConfirmationRejected,
    check_v4_confirmation,
    task_order_hash,
)
from training_gym.evaluation.store import (
    HoldoutAlreadyCommitted,
    PlanAlreadyConsumed,
    consume_plan,
    is_plan_consumed,
    record_holdout_commit,
)

_PLAN = "a" * 64
_OTHER_PLAN = "b" * 64
_PACK = "c" * 64
_AT = "2026-09-01T00:00:00Z"


def _attempt(**overrides) -> dict:
    body = {
        "protocol_version": "m62.evaluation_protocol.4",
        "pairing_hash": "1" * 64,
        "reference_arm_hash": "2" * 64,
        "candidate_arm_hash": "3" * 64,
        "reference_adapter_sha256": "4" * 64,
        "candidate_adapter_sha256": "5" * 64,
        "common_base_model_id": "Qwen/Qwen3-0.6B",
        "common_base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "dataset_id": "m62-defensive-eval",
        "dataset_version": "v7",
        "dataset_manifest_hash": "6" * 64,
        "task_pack_hash": _PACK,
        "task_order_hash": "7" * 64,
        "task_count": 36,
        "expected_total_generations": 72,
        "holdout_spends": 1,
        "runtime_report_sha256": "8" * 64,
        "evaluation_source_commit": "9" * 40,
    }
    body.update(overrides)
    return body


def _start(root, plan=_PLAN, evaluation_id="m62-s4e-live", generation=1):
    consume_plan(root, plan_hash=plan, evaluation_id=evaluation_id,
                 generation=generation, actor="test", at=_AT)


def _record(root, plan=_PLAN, evaluation_id="m62-s4e-live", generation=1, **overrides):
    return SV4.record_v4_paired_attempt(
        root, plan_hash=plan, inner_plan_hash="d" * 64, evaluation_id=evaluation_id,
        generation=generation, actor="test", at=_AT, attempt=_attempt(**overrides))


# ══════════════════════════════════════════════════════════════════════════════
#  1. EVAL AUTHORITY — created once, consumed once
# ══════════════════════════════════════════════════════════════════════════════
def test_a_plan_can_only_be_consumed_once(tmp_path):
    _start(tmp_path)
    assert is_plan_consumed(tmp_path, _PLAN)
    with pytest.raises(PlanAlreadyConsumed):
        _start(tmp_path)


def test_a_token_for_another_plan_authorises_nothing(monkeypatch):
    class _Plan:
        def plan_hash(self):
            return _PLAN

        def confirmation_token(self):
            return f"EVAL:{_PLAN}"

    with pytest.raises(PlanV4ConfirmationRejected, match="does not authorise this plan"):
        check_v4_confirmation(f"EVAL:{_OTHER_PLAN}", _Plan())


def test_a_truncated_digest_authorises_nothing():
    class _Plan:
        def plan_hash(self):
            return _PLAN

        def confirmation_token(self):
            return f"EVAL:{_PLAN}"

    with pytest.raises(PlanV4ConfirmationRejected):
        check_v4_confirmation(f"EVAL:{_PLAN[:16]}", _Plan())


def test_a_boolean_confirmation_is_refused_by_type():
    class _Plan:
        def plan_hash(self):
            return _PLAN

        def confirmation_token(self):
            return f"EVAL:{_PLAN}"

    with pytest.raises(PlanV4ConfirmationRejected, match="not a confirmation"):
        check_v4_confirmation(True, _Plan())


def test_a_train_token_can_never_authorise_an_evaluation():
    class _Plan:
        def plan_hash(self):
            return _PLAN

        def confirmation_token(self):
            return f"EVAL:{_PLAN}"

    with pytest.raises(PlanV4ConfirmationRejected, match="TRAIN token"):
        check_v4_confirmation(f"TRAIN:{_PLAN}", _Plan())


@pytest.mark.parametrize("shape", ["@/tmp/token.txt", "/tmp/token", "C:\\token"])
def test_a_confirmation_read_out_of_a_file_is_refused(shape):
    class _Plan:
        def plan_hash(self):
            return _PLAN

        def confirmation_token(self):
            return f"EVAL:{_PLAN}"

    with pytest.raises(PlanV4ConfirmationRejected, match="nobody typed"):
        check_v4_confirmation(shape, _Plan())


# ══════════════════════════════════════════════════════════════════════════════
#  2. HOLDOUT — spent once, whatever the attempt calls itself
# ══════════════════════════════════════════════════════════════════════════════
def test_a_paired_attempt_is_recorded_once(tmp_path):
    _start(tmp_path)
    _record(tmp_path)
    assert len(SV4.v4_attempt_entries(tmp_path)) == 1
    with pytest.raises(HoldoutAlreadyCommitted):
        _record(tmp_path)


def test_a_second_attempt_under_a_new_evaluation_id_is_refused(tmp_path):
    """THE GAP THIS MODULE EXISTS TO CLOSE. A rename is not a fresh holdout."""
    _start(tmp_path)
    _record(tmp_path)
    _start(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-live-take-two")
    with pytest.raises(HoldoutAlreadyCommitted, match="ONE paired attempt, ONE spend"):
        _record(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-live-take-two")


def test_a_second_attempt_under_a_new_generation_is_refused(tmp_path):
    _start(tmp_path)
    _record(tmp_path)
    _start(tmp_path, plan=_OTHER_PLAN, generation=2)
    with pytest.raises(HoldoutAlreadyCommitted):
        _record(tmp_path, plan=_OTHER_PLAN, generation=2)


def test_a_relabelled_corpus_with_the_same_pack_digest_is_refused(tmp_path):
    """Calling the same 36 tasks 'v8' does not make them unread."""
    _start(tmp_path)
    _record(tmp_path)
    _start(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-relabelled")
    with pytest.raises(HoldoutAlreadyCommitted, match="Protocol V4 paired attempt"):
        _record(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-relabelled",
                dataset_version="v8")


def test_a_prior_v1_to_v3_ledger_commit_also_blocks_a_v4_attempt(tmp_path):
    """A v1-v3 run and a V4 run on one corpus are the same event twice."""
    _start(tmp_path)
    record_holdout_commit(
        tmp_path, plan_hash=_PLAN, evaluation_id="m62-s4e-live", generation=1,
        actor="test", at=_AT,
        commit={
            "commit_schema_version": "m62.evaluation_holdout_commit_body.1",
            "dataset_id": "m62-defensive-eval", "dataset_version": "v7",
            "dataset_manifest_hash": "6" * 64, "task_pack_hash": _PACK,
            "hidden_target_store_hash": "e" * 64, "pack_identity_hash": "f" * 64,
            "order_policy": "balanced_by_task_hash_and_seed",
            "order_assignment_hash": "1" * 64, "task_count": 36, "target_count": 36,
            "first_task_id": "t-0", "first_task_hash": "2" * 64, "first_arm": "baseline",
            "first_request_parity_hash": "3" * 64, "baseline_reference_hash": "4" * 64,
            "candidate_adapter_reference_hash": "5" * 64,
            "generation_policy_hash": "6" * 64, "backend_id": "transformers_peft",
            "performs_inference": True})
    _start(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-second")
    with pytest.raises(HoldoutAlreadyCommitted, match="already committed to a model"):
        _record(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-second")


def test_an_attempt_without_a_consumed_plan_is_refused(tmp_path):
    """A spend by a run nobody recorded starting is a spend nobody authorised."""
    with pytest.raises(SV4.StoreV4Error, match="no start line"):
        _record(tmp_path)


def test_two_holdout_spends_in_one_attempt_are_refused(tmp_path):
    _start(tmp_path)
    with pytest.raises(SV4.StoreV4Error, match="exactly once"):
        _record(tmp_path, holdout_spends=2)


# ══════════════════════════════════════════════════════════════════════════════
#  3. CRASH: attempt recorded, no ledger commit, process dies
# ══════════════════════════════════════════════════════════════════════════════
def test_the_attempt_record_survives_a_crash_before_the_first_generation(tmp_path):
    """Simulated: the record is written and fsynced, then nothing else happens."""
    _start(tmp_path)
    _record(tmp_path)
    # A fresh reader — standing in for the restarted process — sees the spend.
    assert SV4.v4_attempt_exists(tmp_path, dataset_id="m62-defensive-eval",
                                 dataset_version="v7")


def test_a_restart_after_a_crash_cannot_create_a_fresh_attempt(tmp_path):
    _start(tmp_path)
    _record(tmp_path)
    _start(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-recovered")
    with pytest.raises(HoldoutAlreadyCommitted):
        _record(tmp_path, plan=_OTHER_PLAN, evaluation_id="m62-s4e-recovered")


def test_the_attempt_record_is_flushed_rather_than_buffered(tmp_path):
    """The record exists for the crash case, so it must be on disk when it returns."""
    _start(tmp_path)
    _record(tmp_path)
    raw = SV4.v4_attempt_path(tmp_path).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw.splitlines()[0])["holdout_spends"] == 1


def test_an_unparseable_attempt_line_is_a_refusal_not_a_silent_skip(tmp_path):
    """Treating a corrupt spend record as absent is how a holdout gets spent twice."""
    SV4.v4_attempt_path(tmp_path).write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(SV4.StoreV4Error, match="not JSON"):
        SV4.v4_attempt_entries(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
#  4. The record is body-free by shape
# ══════════════════════════════════════════════════════════════════════════════
def test_the_attempt_record_refuses_a_field_outside_the_closed_list(tmp_path):
    _start(tmp_path)
    with pytest.raises(SV4.StoreV4Error, match="closed body-free field list"):
        _record(tmp_path, first_prompt="Triage this alert")


def test_the_attempt_record_refuses_an_incomplete_body(tmp_path):
    _start(tmp_path)
    body = _attempt()
    body.pop("task_pack_hash")
    with pytest.raises(SV4.StoreV4Error, match="omits"):
        SV4.record_v4_paired_attempt(
            tmp_path, plan_hash=_PLAN, inner_plan_hash="d" * 64,
            evaluation_id="m62-s4e-live", generation=1, actor="test", at=_AT,
            attempt=body)


def test_no_closed_field_could_hold_a_prompt_a_target_or_a_response():
    """Checked on the NAMES: no field is shaped like somewhere free text would live."""
    forbidden = ("prompt", "target", "response", "answer", "rubric", "body_text",
                 "completion_text", "expected_output")
    for field in SV4.V4_ATTEMPT_FIELDS:
        assert not any(word in field for word in forbidden), field


def test_every_recorded_value_is_a_digest_a_count_or_a_short_identifier(tmp_path):
    """Checked on the VALUES, which is the half a name-based rule cannot see.

    A prompt or a response is long free text. Bounding every string in the record at 96
    characters means there is nowhere for one to sit even if a future field were added
    with an innocuous name.
    """
    _start(tmp_path)
    record = _record(tmp_path)
    for key, value in record.items():
        assert isinstance(value, (str, int, bool)), key
        if isinstance(value, str):
            assert len(value) <= 96, f"{key} is {len(value)} characters"


# ══════════════════════════════════════════════════════════════════════════════
#  Task order is bound, not published
# ══════════════════════════════════════════════════════════════════════════════
def test_the_task_order_hash_is_order_sensitive():
    assert task_order_hash(("a", "b", "c")) != task_order_hash(("a", "c", "b"))


def test_the_task_order_hash_refuses_a_repeated_id():
    with pytest.raises(Exception, match="repeats an id"):
        task_order_hash(("a", "b", "a"))


def test_the_task_order_hash_names_no_task():
    """It binds the sequence exactly while publishing none of it."""
    digest = task_order_hash(("he7-report-01", "he7-report-02"))
    assert "he7" not in digest and len(digest) == 64
