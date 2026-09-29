"""V69 M58.7 — bounded tool-enabled generation. Deterministic, server-free."""
from __future__ import annotations

from core.tool_loop import (
    ToolLoopBudget,
    ToolTurnState,
    validate_tool_call,
)


# ── malformed / ineligible calls never execute ────────────────────────────────
def test_valid_call_passes():
    ok, args, reason = validate_tool_call("web_search", '{"query":"x"}',
                                          {"web_search"})
    assert ok is True and args == {"query": "x"} and reason == ""


def test_malformed_json_is_rejected():
    ok, args, reason = validate_tool_call("web_search", '{"query": ', {"web_search"})
    assert ok is False and args == {} and reason == "malformed_json"


def test_partial_truncated_json_never_validates():
    # A tool call truncated mid-object must never be treated as executable.
    ok, _, reason = validate_tool_call("run_shell_command",
                                       '{"command": "rm -rf', {"run_shell_command"})
    assert ok is False and reason == "malformed_json"


def test_ineligible_tool_name_is_rejected():
    ok, _, reason = validate_tool_call("query_knowledge", "{}", {"web_search"})
    assert ok is False and reason == "tool_not_eligible"


def test_hallucinated_tool_name_is_rejected():
    ok, _, reason = validate_tool_call("definitely_not_a_tool", "{}", {"web_search"})
    assert ok is False and reason == "tool_not_eligible"


def test_empty_name_is_rejected():
    ok, _, reason = validate_tool_call("", "{}", {"web_search"})
    assert ok is False and reason == "empty_name"


def test_non_object_arguments_rejected():
    ok, _, reason = validate_tool_call("web_search", "[1,2,3]", {"web_search"})
    assert ok is False and reason == "arguments_not_object"


def test_empty_args_allowed_for_no_param_tool():
    ok, args, reason = validate_tool_call("get_datetime", "", {"get_datetime"})
    assert ok is True and args == {}


def test_absent_eligible_set_is_refused_not_treated_as_unrestricted():
    """V69 M68A §B — REPLACES `test_none_eligible_set_allows_any_wellformed_name`,
    which asserted the exact behaviour an external audit later identified as a
    fail-open: it pinned `None` to mean "only structural validation applies", i.e.
    unrestricted eligibility. `core/llm.py` reached that value by normalising an
    EXHAUSTED tool set (`{...} or None`), so the moment the round budget dropped
    every tool, every tool name validated. An absent eligibility set is now a
    refusal: the caller must state what is eligible, and "nothing" is a statement.
    """
    ok, args, reason = validate_tool_call("anything", '{"a":1}', None)
    assert ok is False and args == {} and reason == "no_eligible_tools"


def test_empty_eligible_set_is_refused():
    ok, args, reason = validate_tool_call("web_search", '{"q":"x"}', set())
    assert ok is False and args == {} and reason == "no_eligible_tools"


# ── round / retry / repair bounds ─────────────────────────────────────────────
def test_rounds_force_final_after_the_budget():
    b = ToolLoopBudget(max_rounds=3)
    assert not b.force_final()
    b.begin_round()
    b.begin_round()
    assert not b.force_final()  # 2 rounds < 3
    b.begin_round()
    assert b.force_final()  # 3rd round hit → drop tools, force a final answer


def test_malformed_repairs_are_bounded():
    b = ToolLoopBudget(max_repairs=2)
    assert b.note_malformed() is True   # repair 1
    assert b.note_malformed() is True   # repair 2
    assert b.note_malformed() is False  # exhausted
    assert b.malformed_calls == 3


def test_retries_are_bounded():
    b = ToolLoopBudget(max_retries=1)
    assert b.note_retry() is True
    assert b.note_retry() is False


def test_denied_and_used_counters():
    b = ToolLoopBudget()
    b.note_denied()
    b.note_tool_used()
    b.note_tool_used()
    assert b.denied_calls == 1 and b.tools_used == 2


def test_snapshot_is_bounded_and_content_free():
    b = ToolLoopBudget()
    b.begin_round()
    b.note_tool_used()
    b.final_response_tokens = 96
    b.state = ToolTurnState.FINAL_RESPONSE_COMPLETE
    snap = b.snapshot()
    assert snap["tool_rounds"] == 1
    assert snap["final_response_tokens"] == 96
    assert snap["state"] == "FINAL_RESPONSE_COMPLETE"
    blob = repr(snap)
    assert "rm -rf" not in blob  # never any argument content
