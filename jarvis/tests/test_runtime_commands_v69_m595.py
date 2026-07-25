"""V69 M59.5 — M59 operator commands. Deterministic, server-free, host-free.

Proves the surface is EXACT (no argument parsing, no prefix matching, no shell), that
the five status verbs are read-only, that the three bounded actions are lifecycle- and
governor-aware, that /compact-now runs the quality gate and never writes semantic
memory, that /qualify quick never touches Git or the host, and that every rendered
line is content-free and encodable on the live cp1252 Windows console.
"""
from __future__ import annotations

import asyncio

import pytest

from core.runtime_commands import (
    BOUNDED_ACTIONS,
    READ_ONLY,
    RuntimeCommand,
    apply_runtime_command,
    describe_compaction,
    describe_rewarm,
    known_runtime_aliases,
    parse_runtime_command,
    render_barge_status,
    render_compaction_status,
    render_prewarm_status,
    render_runtime_qualification,
    render_warmth_status,
    request_rewarm,
    run_compact_now,
    run_quick_qualification,
)

REQUIRED_COMMANDS = ("/warmth-status", "/prewarm-status", "/compaction-status",
                     "/barge-status", "/runtime-qualification",
                     "/rewarm concise", "/compact-now", "/qualify quick")


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════════
#  1. The exact allowlist
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("alias", REQUIRED_COMMANDS)
def test_every_required_command_is_reachable(alias):
    parsed = parse_runtime_command(alias)
    assert parsed is not None, f"{alias} is not recognised"
    assert parsed.alias == alias


def test_the_command_set_is_exactly_the_specified_one():
    assert set(RuntimeCommand) == {
        RuntimeCommand.WARMTH_STATUS, RuntimeCommand.PREWARM_STATUS,
        RuntimeCommand.COMPACTION_STATUS, RuntimeCommand.BARGE_STATUS,
        RuntimeCommand.RUNTIME_QUALIFICATION, RuntimeCommand.REWARM_CONCISE,
        RuntimeCommand.COMPACT_NOW, RuntimeCommand.QUALIFY_QUICK,
    }
    assert READ_ONLY | BOUNDED_ACTIONS == set(RuntimeCommand)
    assert not (READ_ONLY & BOUNDED_ACTIONS)
    assert len(READ_ONLY) == 5 and len(BOUNDED_ACTIONS) == 3


def test_matching_is_whole_line_case_insensitive_and_whitespace_tolerant():
    assert parse_runtime_command("  /Warmth-Status  ").command \
        is RuntimeCommand.WARMTH_STATUS
    assert parse_runtime_command("/rewarm   concise").command \
        is RuntimeCommand.REWARM_CONCISE


@pytest.mark.parametrize("text", [
    "/qualify quick --live", "/rewarm concise CONCISE", "/rewarm explanatory",
    "/compact-now --force", "/compact-now; rm -rf /", "/warmth-status extra",
    "warmth-status", "/qualify", "/rewarm", "/qualify quick && echo hi",
    "explicame /barge-status", "", "   ", "/", "/unknown-command",
])
def test_no_argument_no_prefix_and_no_shell_can_reach_a_command(text):
    """The exact-match allowlist IS the security argument: a verb plus anything is not
    a command, it is an ordinary user turn."""
    assert parse_runtime_command(text) is None


def test_aliases_carry_no_argument_syntax():
    for alias in known_runtime_aliases():
        assert alias.startswith("/")
        assert not any(ch in alias for ch in "|&;<>$`\\\"'*?[]{}()=")
        # at most ONE space, and only inside a two-word literal alias
        assert alias.count(" ") <= 1


def test_read_only_and_action_classification():
    for alias in ("/warmth-status", "/prewarm-status", "/compaction-status",
                  "/barge-status", "/runtime-qualification"):
        assert parse_runtime_command(alias).read_only is True
        assert parse_runtime_command(alias).bounded_action is False
    for alias in ("/rewarm concise", "/compact-now", "/qualify quick"):
        assert parse_runtime_command(alias).bounded_action is True
        assert parse_runtime_command(alias).read_only is False


def test_the_surface_never_reaches_a_shell_or_the_filesystem():
    import ast
    import inspect

    import core.runtime_commands as mod
    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name:
                called.add(name)
    assert not (imported & {"subprocess", "os", "shutil", "pathlib", "socket",
                            "requests", "httpx"})
    assert not (called & {"system", "popen", "run", "Popen", "exec", "eval",
                          "open", "remove", "rmtree", "chdir"})


# ══════════════════════════════════════════════════════════════════════════════
#  2. Rendering — bounded, content-free, console-encodable
# ══════════════════════════════════════════════════════════════════════════════
_RENDERERS = (render_warmth_status, render_prewarm_status, render_compaction_status,
              render_barge_status, render_runtime_qualification)


@pytest.mark.parametrize("renderer", _RENDERERS)
@pytest.mark.parametrize("language", ["es", "en"])
def test_every_panel_renders_in_both_languages_and_is_cp1252_encodable(renderer,
                                                                      language):
    """The live JARVIS console is cp1252; a panel it cannot encode is a crash, not a
    cosmetic issue (M58 already had to strip one non-ASCII header)."""
    out = renderer(language)
    assert isinstance(out, str) and out.strip()
    out.encode("cp1252")            # must not raise
    assert len(out.splitlines()) <= 30      # bounded


def test_panels_never_expose_prompts_answers_or_keys():
    blob = "\n".join(r("es") + r("en") for r in _RENDERERS)
    for leak in ("\x1b", "\x07", "prompt_text", "system_prompt", "api_key",
                 "password", "token="):
        assert leak not in blob


def test_warmth_panel_states_the_prewarm_honesty_rule():
    es = render_warmth_status("es")
    en = render_warmth_status("en")
    assert "PREWARMED" in es and "PREWARMED" in en
    assert "state=" in es and "reuse_state=" in es


def test_barge_panel_reports_backend_and_fallback_reason():
    out = render_barge_status("en", snapshot={
        "selected_backend": "WINDOWS_MSVCRT", "mode": "ACTIVE_CONSOLE_KEY",
        "portable_backend_available": False,
        "fallback_reason": "PROMPT_TOOLKIT_NOT_INSTALLED", "supported": True,
        "active_interruptions": 2, "command_interruptions": 1,
        "cancellation_latency_ms": 3.4, "terminal_restore_failures": 0,
        "orphan_reader_count": 0, "arm_failures": 0})
    assert "selected_backend=WINDOWS_MSVCRT" in out
    assert "fallback_reason=PROMPT_TOOLKIT_NOT_INSTALLED" in out
    assert "orphan_reader_count=0" in out


def test_panels_survive_a_completely_empty_snapshot():
    for renderer in (render_warmth_status, render_prewarm_status,
                     render_compaction_status, render_barge_status):
        out = renderer("es", {})
        assert "n/a" in out


# ══════════════════════════════════════════════════════════════════════════════
#  3. /qualify quick and /runtime-qualification
# ══════════════════════════════════════════════════════════════════════════════
def test_quick_qualification_runs_the_deterministic_matrix():
    r = run_quick_qualification()
    assert r["mode"] == "deterministic"
    assert r["total"] > 0
    assert r["verdict"] in ("PASS", "FAIL", "DEGRADED", "INSUFFICIENT_EVIDENCE")
    assert r["passed"] + r["failed"] <= r["total"]


def test_quick_qualification_touches_no_git_no_host_no_collection():
    import ast
    import inspect

    import core.runtime_commands as mod
    src = inspect.getsource(mod.run_quick_qualification)
    tree = ast.parse(src.strip())
    names = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "run_deterministic_matrix" in names and "aggregate_verdict" in names
    # nothing that could commit, merge, write a collection or change the host
    assert not (names & {"run", "check_output", "commit", "merge", "add",
                         "upsert", "delete", "write_text", "setenv"})


def test_qualification_panel_reports_the_verdict():
    out = render_runtime_qualification("en", {"verdict": "PASS", "mode":
                                              "deterministic", "total": 9,
                                              "passed": 9, "failed": 0,
                                              "failed_cases": []})
    assert "verdict=PASS" in out and "cases=9" in out


def test_qualify_quick_is_allowed_even_while_stopping(monkeypatch):
    """A READ-ONLY, in-process verdict is safe during shutdown; it starts nothing."""
    import core.runtime_commands as mod
    monkeypatch.setattr(mod, "_is_stopping", lambda: True)
    out = _run(apply_runtime_command(parse_runtime_command("/qualify quick"),
                                     language="en"))
    assert "verdict=" in out


# ══════════════════════════════════════════════════════════════════════════════
#  4. /rewarm concise
# ══════════════════════════════════════════════════════════════════════════════
def test_rewarm_command_is_refused_while_a_user_turn_is_active(monkeypatch):
    from core.session_warmth import PredictiveRewarmPolicy, SessionWarmthBaseline
    from core.warmth_runtime import WarmthRuntime, reset_warmth_runtime
    wr = WarmthRuntime(baseline=SessionWarmthBaseline(), policy=PredictiveRewarmPolicy(),
                       is_stopping=lambda: False, active_fast=lambda: True,
                       embedding_requested=lambda: False,
                       power_prewarm_allowed=lambda: True)
    reset_warmth_runtime(wr)
    try:
        result = request_rewarm("CONCISE")
        assert result["scheduled"] is False
        assert result["reason"] == "active_fast_outranks"
        assert "no" in describe_rewarm(result, language="en").lower()
    finally:
        reset_warmth_runtime(None)


def test_rewarm_command_re_arms_an_exhausted_family_but_keeps_safety_refusals():
    from core.session_warmth import PredictiveRewarmPolicy, SessionWarmthBaseline
    from core.warmth_runtime import WarmthRuntime, reset_warmth_runtime
    policy = PredictiveRewarmPolicy()
    for _ in range(5):
        policy.note_attempt("CONCISE")
    wr = WarmthRuntime(baseline=SessionWarmthBaseline(), policy=policy,
                       is_stopping=lambda: True,     # a SAFETY refusal
                       active_fast=lambda: False, embedding_requested=lambda: False,
                       power_prewarm_allowed=lambda: True)
    reset_warmth_runtime(wr)
    try:
        result = request_rewarm("CONCISE")
        # the attempt cap was re-armed by the explicit request ...
        assert policy._fam("CONCISE").attempts == 0
        # ... but STOPPING still refuses, and says so
        assert result["reason"] == "stopping" and result["scheduled"] is False
    finally:
        reset_warmth_runtime(None)


def test_rewarm_describe_is_localized_and_never_echoes_input():
    scheduled = {"scheduled": True, "family": "CONCISE", "reason": "scheduled"}
    assert "CONCISE" in describe_rewarm(scheduled, language="es")
    assert describe_rewarm(scheduled, language="en").startswith("Rewarm scheduled")
    refused = {"scheduled": False, "reason": "cooldown"}
    assert "cooldown" in describe_rewarm(refused, language="en")


# ══════════════════════════════════════════════════════════════════════════════
#  5. /compact-now
# ══════════════════════════════════════════════════════════════════════════════
def test_compact_now_is_refused_while_stopping(monkeypatch):
    import core.runtime_commands as mod
    monkeypatch.setattr(mod, "_is_stopping", lambda: True)
    result = _run(run_compact_now(None))
    assert result == {"state": "REFUSED", "reason": "stopping"}
    assert "stopping" in describe_compaction(result, language="en")


def test_compact_now_honours_every_measured_safety_gate(monkeypatch):
    """The idle TIMING gates are waived (that is what "now" means); the SAFETY gates
    are not — an active turn, HITL, an effectful tool or a battery policy refuse."""
    import core.runtime_commands as mod
    from core.compaction_scheduler import CompactionConditions

    for field, expected in (("active_user_turn", "active_user_turn"),
                            ("hitl_active", "hitl_active"),
                            ("effectful_tool_active", "effectful_tool_active"),
                            ("answer_tts_active", "answer_tts_active"),
                            ("high_priority_embedding", "high_priority_embedding")):
        monkeypatch.setattr(
            mod, "_is_stopping", lambda: False)
        import core.compaction_scheduler as sched_mod
        monkeypatch.setattr(
            sched_mod, "build_conditions_from_runtime",
            lambda history, context_budget=2048, _f=field: CompactionConditions(
                completed_turns=99, context_pressure=1.0, **{_f: True}))
        result = _run(run_compact_now(None))
        assert result == {"state": "REFUSED", "reason": expected}


def test_compact_now_waives_only_the_idle_timing_gates(monkeypatch):
    import core.compaction_scheduler as sched_mod
    import core.runtime_commands as mod
    from core.compaction_scheduler import CompactionConditions
    monkeypatch.setattr(mod, "_is_stopping", lambda: False)
    # Zero turns, zero pressure, cooldown NOT expired: the idle driver would skip.
    monkeypatch.setattr(
        sched_mod, "build_conditions_from_runtime",
        lambda history, context_budget=2048: CompactionConditions(
            completed_turns=0, context_pressure=0.0, cooldown_expired=False))
    seen: dict = {}

    class FakeSched:
        min_turns = 4
        pressure_threshold = 0.5

        async def maybe_run(self, history, conditions):
            seen["conditions"] = conditions
            return "COMPLETED"

        def snapshot(self):
            return {"candidates": 3, "accepted": 2, "rejected": 1,
                    "quality_state": "ACCEPTED", "last_duration_ms": 41.0,
                    "digest_version": 2}

    monkeypatch.setattr(sched_mod, "get_compaction_scheduler", lambda: FakeSched())
    result = _run(run_compact_now(None))
    conds = seen["conditions"]
    assert conds.completed_turns >= 4 and conds.context_pressure >= 0.5
    assert conds.cooldown_expired is True
    # safety fields untouched
    assert conds.active_user_turn is False and conds.lifecycle_operational is True
    assert result["state"] == "COMPLETED" and result["accepted"] == 2
    assert "2 accepted" in describe_compaction(result, language="en")


def test_compact_now_never_writes_semantic_memory():
    """The command has no semantic-write path, and neither does the scheduler it
    drives. Proven structurally, not by hoping."""
    import ast
    import inspect

    import core.compaction_scheduler as sched_mod
    import core.runtime_commands as mod
    for target in (mod, sched_mod):
        tree = ast.parse(inspect.getsource(target))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not (imported & {"core.semantic_memory", "core.memory",
                                "core.knowledge", "core.memory_fabric",
                                "core.episodic_memory", "core.vector_collections"})


# ══════════════════════════════════════════════════════════════════════════════
#  6. apply_runtime_command
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("alias", ["/warmth-status", "/prewarm-status",
                                   "/compaction-status", "/barge-status",
                                   "/runtime-qualification"])
def test_status_commands_render_a_panel_and_change_nothing(alias):
    out = _run(apply_runtime_command(parse_runtime_command(alias), language="es"))
    assert isinstance(out, str) and out.strip()
    out.encode("cp1252")
    # calling twice is identical in shape (read-only)
    again = _run(apply_runtime_command(parse_runtime_command(alias), language="es"))
    assert again.splitlines()[0] == out.splitlines()[0]


def test_bounded_actions_refuse_after_stopping(monkeypatch):
    import core.runtime_commands as mod
    monkeypatch.setattr(mod, "_is_stopping", lambda: True)
    for alias in ("/rewarm concise", "/compact-now"):
        out = _run(apply_runtime_command(parse_runtime_command(alias), language="en"))
        assert "stopping" in out.lower()


def test_apply_never_raises_even_when_a_subsystem_explodes(monkeypatch):
    import core.runtime_commands as mod

    def boom(*a, **k):
        raise RuntimeError("subsystem down")

    monkeypatch.setattr(mod, "render_warmth_status", boom)
    out = _run(apply_runtime_command(parse_runtime_command("/warmth-status")))
    assert "RuntimeError" in out


def test_apply_output_is_localized():
    es = _run(apply_runtime_command(parse_runtime_command("/barge-status"),
                                    language="es"))
    en = _run(apply_runtime_command(parse_runtime_command("/barge-status"),
                                    language="en"))
    assert es.splitlines()[0] != en.splitlines()[0]


def test_commands_do_not_collide_with_the_response_command_surface():
    from core.response_commands import known_aliases
    assert not (set(known_runtime_aliases()) & set(known_aliases()))
