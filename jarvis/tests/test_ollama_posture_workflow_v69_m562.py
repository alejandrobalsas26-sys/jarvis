"""tests/test_ollama_posture_workflow_v69_m562.py — V69 M56.2 operator-gated posture.

These tests exist to make the DANGEROUS half unreachable by accident. They lock:

  * status / plan / dry-run never mutate and never need authority;
  * apply and rollback are refused without an approval covering the EXACT target;
  * only three variables may ever be written, with strict integer/duration bounds;
  * shell metacharacters are rejected even though no shell exists on this path;
  * no server is ever restarted, and verify cannot succeed from configuration alone;
  * the rollback journal is durable, bounded and restores previous values verbatim.
"""
from __future__ import annotations

import json

import pytest

from core.ollama_posture import (
    AuthorizationRequired,
    OperatorAuthorization,
    PostureAction,
    PostureController,
    PostureJournal,
    PostureScope,
    ValidationError,
    VerifyState,
    build_plan,
    parse_posture_command,
    recommend_posture,
    validate_target,
    validate_value,
    validate_variable,
)
from core.ollama_process import (
    DiscoveryState,
    LaunchMode,
    OllamaProcessTruth,
    PostureSource,
    ProcessCandidate,
)

RECOMMENDED = {"OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "2",
               "OLLAMA_KEEP_ALIVE": "30m"}


# ── fixtures / doubles ───────────────────────────────────────────────────────
class _Writer:
    """Records writes instead of touching the registry."""

    def __init__(self, ok=True, err=None):
        self.calls: list[dict] = []
        self._ok, self._err = ok, err

    def __call__(self, values):
        self.calls.append(dict(values))
        return self._ok, self._err


def _truth(*, server_env=None, env_readable=False, user_env=None, candidates=True,
           create_time=1000.0):
    cands = ()
    if candidates:
        cands = (ProcessCandidate(pid=42, name="ollama.exe", exe=r"C:\ollama.exe",
                                  create_time=create_time, parent_name="ollama app.exe",
                                  env_readable=env_readable, env=dict(server_env or {})),)
    return OllamaProcessTruth(
        discovered_at=1.0,
        state=DiscoveryState.SINGLE_CANDIDATE if candidates else DiscoveryState.NO_PROCESS_FOUND,
        candidates=cands, primary_pid=42 if candidates else None,
        launch_mode=LaunchMode.STARTUP_APP if candidates else LaunchMode.UNKNOWN,
        user_env=dict(user_env or {}), machine_env={},
        server_env_readable=bool(candidates and env_readable),
    )


def _controller(truth=None, writer=None, tmp_path=None, clock=None):
    truth = truth if truth is not None else _truth()
    journal = PostureJournal(path=(tmp_path / "journal.jsonl")) if tmp_path else PostureJournal()
    t = [5000.0]

    def _clock():
        return t[0]

    return PostureController(truth_provider=lambda **kw: truth,
                             writer=writer or _Writer(), journal=journal,
                             clock=clock or _clock)


def _auth(action, target, **kw):
    return OperatorAuthorization(granted=True, action=action, approved_target=dict(target),
                                 operator="op", reason="test", granted_at=1.0, **kw)


# ── validation: the closed grammar ───────────────────────────────────────────
def test_only_allowlisted_variables_are_accepted():
    for var in RECOMMENDED:
        assert validate_variable(var.lower()) == var
    for bad in ("PATH", "OLLAMA_HOST", "OLLAMA_MODELS", "", "OLLAMA_NUM_PARALLEL_X"):
        with pytest.raises(ValidationError):
            validate_variable(bad)


def test_integer_bounds_are_enforced():
    assert validate_value("OLLAMA_NUM_PARALLEL", 1) == "1"
    assert validate_value("OLLAMA_MAX_LOADED_MODELS", "6") == "6"
    for bad in (0, 5, -1, 99, "1.5", "one", ""):
        with pytest.raises(ValidationError):
            validate_value("OLLAMA_NUM_PARALLEL", bad)
    with pytest.raises(ValidationError):
        validate_value("OLLAMA_MAX_LOADED_MODELS", 7)


def test_duration_grammar_is_anchored():
    for good in ("30m", "2h", "900s", "0s"):
        assert validate_value("OLLAMA_KEEP_ALIVE", good) == good
    for bad in ("30", "30 m", "m30", "30min", "-5m", "30m; echo", "1000000m"):
        with pytest.raises(ValidationError):
            validate_value("OLLAMA_KEEP_ALIVE", bad)


@pytest.mark.parametrize("payload", [
    "2; shutdown /r", "2 && del *", "2 | more", "$(whoami)", "`whoami`", "2\nrm",
    "2%PATH%", "2^X", "2>file", "../../x", "2'", '2"',
])
def test_shell_metacharacters_are_rejected(payload):
    with pytest.raises(ValidationError):
        validate_value("OLLAMA_MAX_LOADED_MODELS", payload)
    with pytest.raises(ValidationError):
        validate_value("OLLAMA_KEEP_ALIVE", payload)


def test_target_validation_is_all_or_nothing():
    with pytest.raises(ValidationError):
        validate_target({"OLLAMA_NUM_PARALLEL": "1", "PATH": "/tmp"})
    with pytest.raises(ValidationError):
        validate_target({})
    assert validate_target({"OLLAMA_NUM_PARALLEL": "1"}) == {"OLLAMA_NUM_PARALLEL": "1"}


def test_recommendation_keeps_room_for_fast_and_embedding():
    rec = recommend_posture()
    assert rec["OLLAMA_NUM_PARALLEL"] == "1"
    assert int(rec["OLLAMA_MAX_LOADED_MODELS"]) >= 2
    assert recommend_posture(profile="BATTERY_SAVER")["OLLAMA_KEEP_ALIVE"] == "5m"


# ── read-only actions never mutate ───────────────────────────────────────────
def test_status_is_read_only(tmp_path):
    writer = _Writer()
    res = _controller(writer=writer, tmp_path=tmp_path).status()
    assert res.ok and res.mutated is False
    assert writer.calls == []
    assert res.detail["restart_required"] is True


def test_plan_and_dry_run_are_read_only(tmp_path):
    writer = _Writer()
    ctl = _controller(writer=writer, tmp_path=tmp_path)
    for res in (ctl.plan(), ctl.dry_run()):
        assert res.ok and res.mutated is False
    assert writer.calls == [], "planning must never write"


def test_dry_run_preview_shows_scope_and_restart(tmp_path):
    res = _controller(tmp_path=tmp_path).dry_run()
    preview = res.detail["preview"]
    assert "no changes applied" in preview
    assert "WINDOWS_USER_ENV" in preview
    assert "restart required: True" in preview
    assert "OLLAMA_MAX_LOADED_MODELS" in preview


def test_dry_run_rejects_an_invalid_target_without_writing(tmp_path):
    writer = _Writer()
    res = _controller(writer=writer, tmp_path=tmp_path).dry_run(
        target={"OLLAMA_MAX_LOADED_MODELS": "99"})
    assert res.ok is False and res.mutated is False
    assert "out of bounds" in res.detail["reason"]
    assert writer.calls == []


def test_plan_current_value_is_unknown_when_server_env_unreadable(tmp_path):
    """The Windows env holds the target value, but the server's block is unreadable —
    the plan must NOT claim the change is unnecessary."""
    truth = _truth(env_readable=False, user_env=RECOMMENDED)
    plan = build_plan(truth=truth)
    for change in plan.changes:
        assert change.current_source is PostureSource.UNKNOWN
        assert change.changes is True
    assert plan.is_noop() is False


def test_plan_is_noop_only_against_verified_current_values():
    truth = _truth(env_readable=True, server_env=dict(RECOMMENDED))
    plan = build_plan(truth=truth)
    assert plan.is_noop() is True
    for change in plan.changes:
        assert change.current_source is PostureSource.SERVER_INHERITANCE_VERIFIED


# ── apply: authorization is mandatory ────────────────────────────────────────
def test_apply_without_authorization_is_refused_and_writes_nothing(tmp_path):
    writer = _Writer()
    res = _controller(writer=writer, tmp_path=tmp_path).apply()
    assert res.ok is False and res.mutated is False
    assert "authorization required" in res.message
    assert writer.calls == []


def test_apply_with_ungranted_authorization_is_refused(tmp_path):
    writer = _Writer()
    auth = OperatorAuthorization(granted=False, action=PostureAction.APPLY,
                                 approved_target=RECOMMENDED)
    res = _controller(writer=writer, tmp_path=tmp_path).apply(authorization=auth)
    assert res.ok is False and writer.calls == []


def test_authorization_for_a_different_target_cannot_be_replayed(tmp_path):
    """An approval for {parallel=1, max=2} must not authorize {max=6}."""
    writer = _Writer()
    auth = _auth(PostureAction.APPLY, RECOMMENDED)
    res = _controller(writer=writer, tmp_path=tmp_path).apply(
        target={"OLLAMA_MAX_LOADED_MODELS": "6"}, authorization=auth)
    assert res.ok is False and writer.calls == []
    assert res.detail["authorization"] == "missing_or_mismatched"


def test_rollback_authorization_cannot_be_used_for_apply(tmp_path):
    writer = _Writer()
    auth = _auth(PostureAction.ROLLBACK, RECOMMENDED)
    res = _controller(writer=writer, tmp_path=tmp_path).apply(authorization=auth)
    assert res.ok is False and writer.calls == []


def test_apply_with_matching_authorization_writes_only_allowlisted_vars(tmp_path):
    writer = _Writer()
    ctl = _controller(writer=writer, tmp_path=tmp_path)
    res = ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    assert res.ok is True and res.mutated is True
    assert writer.calls == [RECOMMENDED]
    assert set(writer.calls[0]) <= set(RECOMMENDED)
    # The server is never restarted by JARVIS.
    assert res.detail["server_restarted"] is False
    assert res.detail["restart_required"] is True


def test_apply_never_targets_machine_scope(tmp_path):
    res = _controller(tmp_path=tmp_path).plan()
    assert res.detail["scope"] == PostureScope.WINDOWS_USER_ENV.value


def test_apply_is_a_noop_when_posture_is_already_verified(tmp_path):
    writer = _Writer()
    ctl = _controller(truth=_truth(env_readable=True, server_env=dict(RECOMMENDED)),
                      writer=writer, tmp_path=tmp_path)
    res = ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    assert res.ok is True and res.mutated is False
    assert writer.calls == []


# ── journal + rollback ───────────────────────────────────────────────────────
def test_apply_journals_previous_values_verbatim(tmp_path):
    prev = {"OLLAMA_NUM_PARALLEL": "4", "OLLAMA_KEEP_ALIVE": "5m"}
    ctl = _controller(truth=_truth(user_env=prev), tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    entries = PostureJournal(path=tmp_path / "journal.jsonl").entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "apply"
    assert entries[0]["previous"]["OLLAMA_NUM_PARALLEL"] == "4"
    # An unset variable is recorded as None so rollback DELETES it, not guesses it.
    assert entries[0]["previous"]["OLLAMA_MAX_LOADED_MODELS"] is None
    assert entries[0]["target"] == RECOMMENDED


def test_journal_is_durable_and_machine_readable(tmp_path):
    path = tmp_path / "journal.jsonl"
    ctl = _controller(tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    raw = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 1
    assert json.loads(raw[0])["target"] == RECOMMENDED


def test_journal_survives_a_corrupt_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    j = PostureJournal(path=path)
    assert j.entries() == []
    assert j.last_applied() is None


def test_rollback_requires_authorization(tmp_path):
    writer = _Writer()
    ctl = _controller(truth=_truth(user_env={"OLLAMA_NUM_PARALLEL": "4"}),
                      writer=writer, tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    writer.calls.clear()
    res = ctl.rollback()
    assert res.ok is False and res.mutated is False
    assert writer.calls == []


def test_rollback_restores_previous_values_and_deletes_unset_ones(tmp_path):
    writer = _Writer()
    ctl = _controller(truth=_truth(user_env={"OLLAMA_NUM_PARALLEL": "4"}),
                      writer=writer, tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    writer.calls.clear()
    restore_target = {"OLLAMA_NUM_PARALLEL": "4"}
    res = ctl.rollback(authorization=_auth(PostureAction.ROLLBACK, restore_target))
    assert res.ok is True and res.mutated is True
    written = writer.calls[0]
    assert written["OLLAMA_NUM_PARALLEL"] == "4"
    assert written["OLLAMA_MAX_LOADED_MODELS"] is None   # delete = restore "unset"
    assert res.detail["server_restarted"] is False


def test_rollback_with_nothing_applied_is_refused(tmp_path):
    res = _controller(tmp_path=tmp_path).rollback(
        authorization=_auth(PostureAction.ROLLBACK, {}))
    assert res.ok is False
    assert res.detail["state"] == VerifyState.NO_MANAGED_STATE.value


def test_corrupted_journal_cannot_become_a_write_primitive(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps({
        "action": "apply", "at": 1.0, "scope": "WINDOWS_USER_ENV",
        "target": {"OLLAMA_NUM_PARALLEL": "1"},
        "previous": {"PATH": "C:\\evil"},
    }) + "\n", encoding="utf-8")
    writer = _Writer()
    ctl = PostureController(truth_provider=lambda **kw: _truth(), writer=writer,
                            journal=PostureJournal(path=path), clock=lambda: 1.0)
    res = ctl.rollback(authorization=_auth(PostureAction.ROLLBACK, {"PATH": "C:\\evil"}))
    assert res.ok is False and writer.calls == []
    assert "rejected" in res.message


# ── verify: never succeeds from configuration alone ──────────────────────────
def test_verify_without_managed_state(tmp_path):
    res = _controller(tmp_path=tmp_path).verify()
    assert res.detail["state"] == VerifyState.NO_MANAGED_STATE.value


def test_verify_is_unverifiable_when_server_env_unreadable(tmp_path):
    writer = _Writer()
    ctl = _controller(truth=_truth(env_readable=False, user_env=RECOMMENDED),
                      writer=writer, tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    res = ctl.verify()
    assert res.detail["state"] == VerifyState.UNVERIFIABLE.value
    assert res.mutated is False


def test_verify_reports_restart_pending_when_server_predates_the_apply(tmp_path):
    """Values written at t=5000 cannot be in a server that started at t=1000."""
    ctl = _controller(truth=_truth(env_readable=True, server_env={}, create_time=1000.0),
                      tmp_path=tmp_path)
    ctl.apply(authorization=_auth(PostureAction.APPLY, RECOMMENDED))
    res = ctl.verify()
    assert res.detail["state"] == VerifyState.RESTART_PENDING.value
    assert res.detail["server_predates_apply"] is True


def test_verify_confirms_only_from_the_server_process_environment(tmp_path):
    ctl = _controller(truth=_truth(env_readable=True, server_env=dict(RECOMMENDED),
                                   create_time=9000.0),
                      tmp_path=tmp_path)
    # Force a real apply by making the plan non-noop through an explicit target.
    target = {"OLLAMA_NUM_PARALLEL": "1"}
    ctl2 = _controller(truth=_truth(env_readable=False), tmp_path=tmp_path)
    ctl2.apply(target=target, authorization=_auth(PostureAction.APPLY, target))
    res = ctl.verify()
    assert res.detail["state"] == VerifyState.VERIFIED_APPLIED.value
    assert res.detail["source"] == PostureSource.SERVER_INHERITANCE_VERIFIED.value


def test_verify_detects_a_server_that_ignored_the_change(tmp_path):
    ctl = _controller(truth=_truth(env_readable=False), tmp_path=tmp_path)
    target = {"OLLAMA_MAX_LOADED_MODELS": "2"}
    ctl.apply(target=target, authorization=_auth(PostureAction.APPLY, target))
    # A NEWER server (started after the apply) whose block lacks the value.
    later = _controller(truth=_truth(env_readable=True, server_env={"OLLAMA_HOST": "x"},
                                     create_time=9000.0),
                        tmp_path=tmp_path)
    res = later.verify()
    assert res.detail["state"] == VerifyState.VERIFIED_NOT_APPLIED.value
    assert "OLLAMA_MAX_LOADED_MODELS" in res.detail["mismatches"]


# ── dispatcher + command surface ─────────────────────────────────────────────
def test_dispatch_read_only_actions_never_mutate(tmp_path):
    writer = _Writer()
    ctl = _controller(writer=writer, tmp_path=tmp_path)
    for action in ("status", "plan", "dry-run", "verify"):
        res = ctl.dispatch(action)
        assert res.mutated is False
    assert writer.calls == []


def test_dispatch_rejects_an_unknown_action(tmp_path):
    res = _controller(tmp_path=tmp_path).dispatch("restart-server")
    assert res.ok is False
    assert "unknown posture action" in res.message


def test_command_parser_accepts_only_the_six_verbs():
    assert parse_posture_command("ollama-posture-status") is PostureAction.STATUS
    assert parse_posture_command("/ollama-posture-dry-run") is PostureAction.DRY_RUN
    assert parse_posture_command("  OLLAMA-POSTURE-APPLY ") is PostureAction.APPLY
    assert parse_posture_command("/posture-rollback") is PostureAction.ROLLBACK
    for bad in ("", "hola", "ollama-posture-restart", "ollama posture status",
                "ollama-posture-apply --force", "x" * 100):
        assert parse_posture_command(bad) is None


def test_command_parser_takes_no_arguments_from_free_text():
    """No variable name, value, PID, path or scope may arrive through the command."""
    assert parse_posture_command("ollama-posture-apply OLLAMA_NUM_PARALLEL=4") is None
    assert parse_posture_command("ollama-posture-apply pid=1234") is None


def test_authorization_required_exception_exists_for_callers():
    assert issubclass(AuthorizationRequired, Exception)
