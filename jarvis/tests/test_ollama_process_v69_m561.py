"""tests/test_ollama_process_v69_m561.py — V69 M56.1 process/service discovery truth.

M55.5 could only say "UNKNOWN" about the server's OLLAMA_NUM_PARALLEL /
OLLAMA_MAX_LOADED_MODELS. M56.1 adds the one honest source — the server PROCESS's own
environment block — and these tests lock the boundary that makes it honest:

  * a value in the Windows user/machine environment is NEVER inheritance proof;
  * an unreadable server environment stays UNKNOWN even when the values match;
  * a read server environment verifies both presence AND absence;
  * discovery is bounded, read-only, permission-tolerant and leaks no command line.
"""
from __future__ import annotations

from core.ollama_process import (
    POSTURE_VARS,
    DiscoveryState,
    LaunchMode,
    OllamaProcessTruth,
    PostureSource,
    ProcessCandidate,
    classify_launch_mode,
    collect_process_truth,
    get_process_truth,
    reset_process_truth,
)


def teardown_function(_):
    reset_process_truth()


def _src(candidates, state):
    return lambda: (list(candidates), state)


def _empty_env_reader(_scope):
    return {}, None


def _proc(pid=4242, *, env=None, env_readable=False, parent="explorer.exe",
          create=1000.0, exe=r"C:\Users\op\AppData\Local\Programs\Ollama\ollama.exe"):
    return ProcessCandidate(
        pid=pid, name="ollama.exe", exe=exe, create_time=create,
        parent_name=parent, arg_count=2, env_readable=env_readable,
        env=dict(env or {}),
    )


# ── discovery states ─────────────────────────────────────────────────────────
def test_no_server_process_found_is_reported_not_guessed():
    truth = collect_process_truth(process_source=_src([], DiscoveryState.NO_PROCESS_FOUND),
                                  env_reader=_empty_env_reader, process_env={})
    assert truth.state is DiscoveryState.NO_PROCESS_FOUND
    assert truth.primary is None
    assert truth.launch_mode is LaunchMode.UNKNOWN
    # With no running server, a persistent change needs no restart to take effect.
    assert truth.restart_required() is False


def test_single_candidate_is_primary():
    truth = collect_process_truth(
        process_source=_src([_proc(pid=17)], DiscoveryState.SINGLE_CANDIDATE),
        env_reader=_empty_env_reader, process_env={})
    assert truth.state is DiscoveryState.SINGLE_CANDIDATE
    assert truth.primary_pid == 17
    assert truth.restart_required() is True


def test_multiple_candidates_pick_the_oldest_readable_one():
    young = _proc(pid=2, create=9000.0, env_readable=True, env={"OLLAMA_NUM_PARALLEL": "4"})
    old_unreadable = _proc(pid=1, create=100.0, env_readable=False)
    truth = collect_process_truth(
        process_source=_src([young, old_unreadable], DiscoveryState.MULTIPLE_CANDIDATES),
        env_reader=_empty_env_reader, process_env={})
    assert truth.state is DiscoveryState.MULTIPLE_CANDIDATES
    # A readable environment wins over mere age — it is the only verifiable source.
    assert truth.primary_pid == 2
    assert truth.server_env_readable is True


def test_multiple_candidates_all_unreadable_pick_oldest():
    truth = collect_process_truth(
        process_source=_src([_proc(pid=2, create=9000.0), _proc(pid=1, create=100.0)],
                            DiscoveryState.MULTIPLE_CANDIDATES),
        env_reader=_empty_env_reader, process_env={})
    assert truth.primary_pid == 1
    assert truth.server_env_readable is False


def test_permission_denied_is_tolerated_and_labelled():
    denied = ProcessCandidate(pid=9, name="ollama.exe", exe=None, create_time=None,
                              parent_name=None, env_readable=False, error="AccessDenied")
    truth = collect_process_truth(
        process_source=_src([denied], DiscoveryState.PERMISSION_DENIED),
        env_reader=_empty_env_reader, process_env={})
    assert truth.state is DiscoveryState.PERMISSION_DENIED
    assert truth.server_env_readable is False
    assert truth.snapshot()["candidates"][0]["error"] == "AccessDenied"


def test_unsupported_when_process_source_raises():
    def boom():
        raise RuntimeError("psutil exploded")

    truth = collect_process_truth(process_source=boom, env_reader=_empty_env_reader,
                                  process_env={})
    assert truth.state is DiscoveryState.UNSUPPORTED
    assert truth.candidates == ()


# ── launch-mode classification ───────────────────────────────────────────────
def test_launch_mode_classification_matrix():
    assert classify_launch_mode(_proc(parent="services.exe")) is LaunchMode.WINDOWS_SERVICE
    assert classify_launch_mode(_proc(parent="taskeng.exe")) is LaunchMode.SCHEDULED_TASK
    assert classify_launch_mode(_proc(parent="svchost.exe")) is LaunchMode.SCHEDULED_TASK
    # Observed on the live target host: the server is a child of the Ollama tray app,
    # which is a per-user logon item -> STARTUP_APP, not WINDOWS_SERVICE.
    assert classify_launch_mode(_proc(parent="ollama app.exe")) is LaunchMode.STARTUP_APP
    assert classify_launch_mode(_proc(parent="Ollama App.exe")) is LaunchMode.STARTUP_APP
    assert classify_launch_mode(_proc(parent="explorer.exe")) is LaunchMode.MANUAL
    assert classify_launch_mode(_proc(parent="powershell.exe")) is LaunchMode.MANUAL
    assert classify_launch_mode(_proc(parent="something_else.exe")) is LaunchMode.UNKNOWN
    assert classify_launch_mode(_proc(parent=None)) is LaunchMode.UNKNOWN
    assert classify_launch_mode(None) is LaunchMode.UNKNOWN


# ── the central truthfulness rule ────────────────────────────────────────────
def test_windows_env_match_is_not_inheritance_proof():
    """The exact value sits in BOTH Windows scopes and in JARVIS's own process, but the
    server's environment could not be read. That must stay UNKNOWN."""
    def reader(scope):
        return {"OLLAMA_MAX_LOADED_MODELS": "2"}, None

    truth = collect_process_truth(
        process_source=_src([_proc(env_readable=False)], DiscoveryState.SINGLE_CANDIDATE),
        env_reader=reader,
        process_env={"OLLAMA_MAX_LOADED_MODELS": "2"})
    resolved = truth.resolve("OLLAMA_MAX_LOADED_MODELS")
    assert resolved.source is PostureSource.UNKNOWN
    assert resolved.value is None
    assert resolved.verified is False
    # ...while the SAME value is truthfully reported as future-launch inheritance.
    future = truth.future_inheritance("OLLAMA_MAX_LOADED_MODELS")
    assert future.source is PostureSource.WINDOWS_USER_ENV
    assert future.value == "2"
    assert future.verified is False


def test_server_process_environment_verifies_presence():
    truth = collect_process_truth(
        process_source=_src([_proc(env_readable=True,
                                   env={"OLLAMA_NUM_PARALLEL": "1",
                                        "OLLAMA_MAX_LOADED_MODELS": "2"})],
                            DiscoveryState.SINGLE_CANDIDATE),
        env_reader=_empty_env_reader, process_env={})
    par = truth.resolve("OLLAMA_NUM_PARALLEL")
    assert par.source is PostureSource.SERVER_INHERITANCE_VERIFIED
    assert par.value == "1"
    assert par.verified is True


def test_server_process_environment_verifies_absence():
    """Reading the block and NOT finding the variable is a verified absence — a
    strictly stronger statement than 'unknown'."""
    truth = collect_process_truth(
        process_source=_src([_proc(env_readable=True, env={"OLLAMA_NUM_PARALLEL": "1"})],
                            DiscoveryState.SINGLE_CANDIDATE),
        env_reader=_empty_env_reader, process_env={})
    keep = truth.resolve("OLLAMA_KEEP_ALIVE")
    assert keep.source is PostureSource.SERVER_INHERITANCE_VERIFIED
    assert keep.value is None
    assert keep.verified is True
    assert "absent" in keep.detail


def test_unknown_variable_is_rejected_not_resolved():
    truth = collect_process_truth(process_source=_src([], DiscoveryState.NO_PROCESS_FOUND),
                                  env_reader=_empty_env_reader, process_env={})
    bad = truth.resolve("PATH")
    assert bad.source is PostureSource.UNKNOWN
    assert bad.value is None


def test_user_scope_wins_over_machine_scope_for_future_launches():
    def reader(scope):
        if scope == "user":
            return {"OLLAMA_KEEP_ALIVE": "30m"}, None
        return {"OLLAMA_KEEP_ALIVE": "5m"}, None

    truth = collect_process_truth(process_source=_src([], DiscoveryState.NO_PROCESS_FOUND),
                                  env_reader=reader, process_env={})
    fut = truth.future_inheritance("OLLAMA_KEEP_ALIVE")
    assert fut.source is PostureSource.WINDOWS_USER_ENV
    assert fut.value == "30m"


def test_registry_permission_error_is_recorded_not_raised():
    def reader(scope):
        return {}, "PermissionError"

    truth = collect_process_truth(process_source=_src([], DiscoveryState.NO_PROCESS_FOUND),
                                  env_reader=reader, process_env={})
    assert truth.user_env_error == "PermissionError"
    assert truth.machine_env_error == "PermissionError"
    assert truth.user_env == {}


# ── safety / boundedness ─────────────────────────────────────────────────────
def test_snapshot_never_exposes_a_command_line():
    truth = collect_process_truth(
        process_source=_src([_proc()], DiscoveryState.SINGLE_CANDIDATE),
        env_reader=_empty_env_reader, process_env={})
    snap = truth.snapshot()
    flat = repr(snap)
    assert "cmdline" not in flat and "cmd_line" not in flat
    assert snap["candidates"][0]["arg_count"] == 2


def test_snapshot_reports_all_posture_vars_in_both_categories():
    truth = collect_process_truth(process_source=_src([], DiscoveryState.NO_PROCESS_FOUND),
                                  env_reader=_empty_env_reader, process_env={})
    snap = truth.snapshot()
    for var in POSTURE_VARS:
        assert var in snap["resolved"]
        assert var in snap["future_inheritance"]
    assert snap["resolved"]["OLLAMA_NUM_PARALLEL"]["verified"] is False


def test_cache_avoids_rescanning_and_refresh_forces_one():
    calls = {"n": 0}

    def counting_source():
        calls["n"] += 1
        return [_proc()], DiscoveryState.SINGLE_CANDIDATE

    t = [1000.0]
    clock = lambda: t[0]  # noqa: E731
    get_process_truth(clock=clock, process_source=counting_source,
                      env_reader=_empty_env_reader, process_env={})
    assert calls["n"] == 1
    t[0] = 1005.0
    get_process_truth(clock=clock, process_source=counting_source,
                      env_reader=_empty_env_reader, process_env={})
    assert calls["n"] == 1, "within TTL the cached truth must be reused"
    t[0] = 1100.0
    get_process_truth(clock=clock, process_source=counting_source,
                      env_reader=_empty_env_reader, process_env={})
    assert calls["n"] == 2, "past TTL a fresh pass is expected"
    get_process_truth(clock=clock, refresh=True, process_source=counting_source,
                      env_reader=_empty_env_reader, process_env={})
    assert calls["n"] == 3, "refresh=True must force a pass"


def test_summary_is_ascii_and_compact():
    truth = collect_process_truth(
        process_source=_src([_proc()], DiscoveryState.SINGLE_CANDIDATE),
        env_reader=_empty_env_reader, process_env={})
    s = truth.summary()
    assert s.isascii() and "\n" not in s
    assert "OLLAMA PROCESS:" in s


def test_live_discovery_is_read_only_and_never_raises():
    """The real psutil/winreg path on this host: whatever it finds, it must not raise
    and must not mutate anything."""
    truth = collect_process_truth()
    assert isinstance(truth, OllamaProcessTruth)
    assert truth.state in set(DiscoveryState)
    # Nothing verified unless the server env was genuinely readable.
    if not truth.server_env_readable:
        assert all(not truth.resolve(v).verified for v in POSTURE_VARS)
