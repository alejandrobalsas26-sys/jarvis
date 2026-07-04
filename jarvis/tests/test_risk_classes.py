"""
tests/test_risk_classes.py — V62.0 Phase 7: Unified Safe Action Model taxonomy.

core/risk_classes.py's RiskClass now drives the live HITL gating decision in
tools/executor.py's aexecute()/aexecute_mcp(), replacing the ad hoc binary
_HITL_EXEMPT_TOOLS/_ALWAYS_HITL_TOOLS split as the actual mechanism. The
single most important property this module must prove is that the
replacement changes NOTHING about existing gating behavior: every tool
already classified by the legacy sets must produce the exact same
requires_hitl() outcome as the old `tool in _ALWAYS_HITL_TOOLS or tool not
in _HITL_EXEMPT_TOOLS` check tools/executor.py used before this retrofit.
"""
from __future__ import annotations

import pytest

import tools.executor as ex_mod
from core.risk_classes import (
    RiskClass,
    TOOL_RISK_CLASS,
    classify_tool,
    requires_hitl,
    requires_trusted_lab,
    rollback_hint,
    binary_risk_class,
    verify_consistent_with_legacy_sets,
)


# ── classify_tool ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", [
    "get_datetime", "read_file", "web_search", "list_directory", "whois_lookup",
])
def test_classify_read_only_tools(tool):
    assert classify_tool(tool) is RiskClass.READ_ONLY


@pytest.mark.parametrize("tool", ["save_note", "estudiar_tema", "ingest_docs"])
def test_classify_low_impact_tools(tool):
    assert classify_tool(tool) is RiskClass.LOW_IMPACT


@pytest.mark.parametrize("tool", [
    "create_document", "packet_tracer_open", "set_clipboard",
    "abrir_packet_tracer", "generar_laboratorio_red",
])
def test_classify_reversible_tools(tool):
    assert classify_tool(tool) is RiskClass.REVERSIBLE


@pytest.mark.parametrize("tool", [
    "code_execute", "run_shell_command", "http_request", "write_file",
    "kill_process", "network_scan", "open_application", "open_software",
    "osint_lookup", "desplegar_webapp", "press_hotkey", "type_text",
    "take_screenshot",
])
def test_classify_high_impact_tools(tool):
    assert classify_tool(tool) is RiskClass.HIGH_IMPACT


def test_unknown_tool_defaults_to_high_impact():
    """Fail-closed: a tool added after this module without an explicit
    classification must still require HITL, not silently auto-execute."""
    assert classify_tool("some_brand_new_tool_nobody_classified_yet") is RiskClass.HIGH_IMPACT


# ── policy functions ──────────────────────────────────────────────────────────

def test_requires_hitl_truth_table():
    assert requires_hitl(RiskClass.READ_ONLY) is False
    assert requires_hitl(RiskClass.LOW_IMPACT) is False
    assert requires_hitl(RiskClass.REVERSIBLE) is True
    assert requires_hitl(RiskClass.HIGH_IMPACT) is True
    assert requires_hitl(RiskClass.LAB_ONLY) is True


def test_requires_trusted_lab_only_for_lab_only():
    for rc in RiskClass:
        expected = rc is RiskClass.LAB_ONLY
        assert requires_trusted_lab(rc) is expected


def test_rollback_hint_only_for_reversible():
    assert rollback_hint(RiskClass.READ_ONLY, "read_file") is None
    assert rollback_hint(RiskClass.HIGH_IMPACT, "kill_process") is None
    hint = rollback_hint(RiskClass.REVERSIBLE, "create_document")
    assert isinstance(hint, str) and hint


def test_rollback_hint_has_a_fallback_for_unmapped_reversible_tools():
    hint = rollback_hint(RiskClass.REVERSIBLE, "some_future_reversible_tool")
    assert isinstance(hint, str) and hint


# ── binary_risk_class (shell sub-binary, informational) ──────────────────────

@pytest.mark.parametrize("binary", ["ping", "cat", "ls", "whoami", "PING", "ping.exe"])
def test_binary_risk_class_read_only(binary):
    assert binary_risk_class(binary) is RiskClass.READ_ONLY


@pytest.mark.parametrize("binary", ["masscan", "hydra", "sqlmap", "msfconsole"])
def test_binary_risk_class_lab_only(binary):
    assert binary_risk_class(binary) is RiskClass.LAB_ONLY


@pytest.mark.parametrize("binary", ["python", "git", "ssh", "curl", "nmap", "unknown_binary"])
def test_binary_risk_class_defaults_high_impact(binary):
    assert binary_risk_class(binary) is RiskClass.HIGH_IMPACT


# ── verify_consistent_with_legacy_sets ────────────────────────────────────────

def test_real_legacy_sets_are_consistent():
    """The actual sets in tools/executor.py — this is exactly what runs at
    tools/executor.py import time. Must not raise."""
    verify_consistent_with_legacy_sets(
        ex_mod._HITL_EXEMPT_TOOLS, ex_mod._ALWAYS_HITL_TOOLS,
    )


def test_detects_an_exempt_tool_wrongly_classified_high_impact():
    with pytest.raises(AssertionError, match="risk_classes.py classifies exempt tool"):
        verify_consistent_with_legacy_sets(
            frozenset({"code_execute"}),  # HIGH_IMPACT tool wrongly marked exempt
            frozenset(),
        )


def test_detects_an_always_hitl_tool_wrongly_classified_no_hitl():
    with pytest.raises(AssertionError, match="risk_classes.py classifies always-HITL tool"):
        verify_consistent_with_legacy_sets(
            frozenset(),
            frozenset({"get_datetime"}),  # READ_ONLY tool wrongly marked always-HITL
        )


# ── The equivalence proof: zero behavior change vs. the pre-retrofit gate ────

def _legacy_must_challenge(tool_name: str) -> bool:
    """tools/executor.py's exact pre-retrofit aexecute() computation."""
    return tool_name in ex_mod._ALWAYS_HITL_TOOLS or tool_name not in ex_mod._HITL_EXEMPT_TOOLS


@pytest.mark.parametrize("tool_name", sorted(TOOL_RISK_CLASS.keys()))
def test_every_classified_tool_matches_legacy_gating_exactly(tool_name):
    """For every tool this module knows about, the new risk-class-driven
    must_challenge outcome must equal the old binary-set outcome exactly.
    MCP-only tool names (not in either legacy set) are exempt from this
    specific comparison — they never had legacy-set membership to compare
    against; their gating is proven separately in test_mcp_gateway.py."""
    if tool_name not in ex_mod._HITL_EXEMPT_TOOLS and tool_name not in ex_mod._ALWAYS_HITL_TOOLS:
        pytest.skip(f"{tool_name!r} has no legacy-set membership to compare (MCP-only tool)")
    new_outcome = requires_hitl(classify_tool(tool_name))
    old_outcome = _legacy_must_challenge(tool_name)
    assert new_outcome == old_outcome, (
        f"{tool_name!r}: risk-class retrofit changed HITL behavior "
        f"(new={new_outcome}, old={old_outcome})"
    )


def test_all_local_tool_handlers_have_an_explicit_classification():
    """Every _tool_* method on ToolExecutor must appear in TOOL_RISK_CLASS —
    an unclassified tool silently defaults to HIGH_IMPACT (safe), but this
    test ensures that's a deliberate, reviewed choice, not an oversight."""
    handler_names = {
        name[len("_tool_"):]
        for name in dir(ex_mod.ToolExecutor)
        if name.startswith("_tool_")
    }
    unclassified = handler_names - set(TOOL_RISK_CLASS.keys())
    assert not unclassified, f"tools with no explicit risk class: {sorted(unclassified)}"
