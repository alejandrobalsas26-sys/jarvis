"""
core/risk_classes.py — V62.0 Phase 7: Unified Safe Action Model risk taxonomy.

A five-tier risk classification covering every tool the agentic runtime can
invoke (local ToolExecutor tools + MCP-bridge tools), used as the single
source of truth for HITL policy in tools/executor.py's aexecute() and
aexecute_mcp() — the two gates every tool call passes through regardless of
whether it originated from a local handler or an MCP server.

This REPLACES the ad hoc binary exempt/challenge split as the live decision
mechanism, but does not weaken it: every tool's classification below was
chosen to preserve tools/executor.py's pre-existing gating behavior exactly
(see tests/test_risk_classes.py::test_classification_matches_legacy_gating_exactly).
_HITL_EXEMPT_TOOLS and _ALWAYS_HITL_TOOLS in tools/executor.py still exist —
they're the tools 5 other test files assert on directly — and this module's
own consistency check (verify_consistent_with_legacy_sets) fails loudly at
import time if the two ever drift apart.

Policies (adapted from the original spec — HITL is never removed where it
already applied; "do not weaken security controls" overrides the letter of
"REVERSIBLE auto-executes" from the spec):

  READ_ONLY    — automatic, no HITL. Never mutates state.
  LOW_IMPACT   — automatic, no HITL. Mutates only JARVIS's own local data
                 (notes, vector store) — never the OS, network, or an
                 external system. Mode-based "notify" (per the original
                 spec's "automatic or notify depending on mode") is deferred
                 to Phase 8 (behavior model) — there is no AssistantMode-
                 reading dispatcher yet for it to notify through.
  REVERSIBLE   — HITL/NATO required (same floor as before this retrofit),
                 PLUS a rollback hint is attached to the challenge broadcast
                 (ToolAuthPendingEvent) so the operator sees what would
                 change and how to undo it before authorizing.
  HIGH_IMPACT  — HITL/NATO required, unconditionally. Never exempt.
  LAB_ONLY     — requires JARVIS_TRUSTED_LAB=true AND HITL/NATO. Refused
                 outright (not merely challenged) when trusted-lab mode is
                 off — mirrors tools/executor.py's existing
                 _LAB_COMMAND_ALLOWLIST design for shell sub-binaries.
"""
from __future__ import annotations

from enum import Enum


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    LOW_IMPACT = "low_impact"
    REVERSIBLE = "reversible"
    HIGH_IMPACT = "high_impact"
    LAB_ONLY = "lab_only"


# ── Tools that only ever read/report — never mutate state ───────────────────
_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "get_datetime", "get_weather", "web_search", "fetch_webpage",
    "system_info", "list_processes", "list_directory", "read_file",
    "leer_archivo_universal", "escanear_pantalla", "analizar_codigo_sast",
    "get_clipboard", "check_connectivity", "whois_lookup",
    "consultar_base_conocimiento", "get_system_status", "query_knowledge",
    "decode_payload", "hash_file", "port_lookup", "regex_test",
    "list_notes", "git_query",
})

# ── Tools that mutate only JARVIS's own local data stores (notes, vector
#    store) — never the OS, network, or an external system ─────────────────
_LOW_IMPACT_TOOLS: frozenset[str] = frozenset({
    "save_note", "estudiar_tema", "ingest_docs",
})

# ── Tools that change local host/application state in an easily-undoable
#    way. Still HITL-gated (see module docstring) — this tier documents a
#    rollback path, it does not remove the challenge ────────────────────────
_REVERSIBLE_TOOLS: frozenset[str] = frozenset({
    "create_document",       # new file under ~/Downloads, sanitized filename
    "packet_tracer_open",    # launches a fixed, known local application
    "set_clipboard",         # already consent-gated; trivially overwritable
    "abrir_packet_tracer",   # MCP: same as packet_tracer_open
    "generar_laboratorio_red",  # MCP: writes one file, traversal-guarded (M0)
})

# ── Tools that always require HITL/NATO and can never be exempted. Includes
#    tools/executor.py's pre-existing _ALWAYS_HITL_TOOLS verbatim, plus every
#    other tool whose blast radius (process kill, network scan/exfil,
#    arbitrary-executable launch, unvalidated input injection, or an
#    overwrite-capable file write the tool's own docstring already marks
#    [HITL]) was never HITL-exempt in the first place ───────────────────────
_HIGH_IMPACT_TOOLS: frozenset[str] = frozenset({
    # tools/executor.py._ALWAYS_HITL_TOOLS — kept in exact lockstep, see
    # verify_consistent_with_legacy_sets().
    "code_execute", "run_shell_command", "http_request",
    # Previously in neither legacy set (implicitly challenged by the old
    # "not in _HITL_EXEMPT_TOOLS" catch-all) — now explicit.
    "write_file",           # sandboxed, but overwrite-capable; author-marked [HITL]
    "kill_process",         # substring-matches and kills every hit
    "network_scan",         # active reconnaissance against a target
    "open_application",     # arbitrary-executable-by-name fallback path
    "open_software",        # same fallback, plus an os.startfile() escape hatch
    "osint_lookup",         # combined WHOIS+DNS recon; author-marked [HITL]
    "desplegar_webapp",     # opens an HTTP listener bound to all interfaces
    "press_hotkey",         # unvalidated OS input injection
    "type_text",            # unvalidated OS input injection
    "take_screenshot",      # writes an unsandboxed save_path today — see
                             # tools/executor.py._tool_take_screenshot; kept
                             # HIGH_IMPACT pending a dedicated path-safety
                             # review rather than silently loosening it here
})

# No local or MCP tool name is LAB_ONLY today — that concept currently
# applies at the shell-sub-binary granularity inside run_shell_command /
# RedTeamShellExecutor (see binary_risk_class() below and
# tools/executor.py's _LAB_COMMAND_ALLOWLIST), not to a top-level tool name.
_LAB_ONLY_TOOLS: frozenset[str] = frozenset()

TOOL_RISK_CLASS: dict[str, RiskClass] = {
    **{t: RiskClass.READ_ONLY for t in _READ_ONLY_TOOLS},
    **{t: RiskClass.LOW_IMPACT for t in _LOW_IMPACT_TOOLS},
    **{t: RiskClass.REVERSIBLE for t in _REVERSIBLE_TOOLS},
    **{t: RiskClass.HIGH_IMPACT for t in _HIGH_IMPACT_TOOLS},
    **{t: RiskClass.LAB_ONLY for t in _LAB_ONLY_TOOLS},
}


def classify_tool(tool_name: str) -> RiskClass:
    """Risk class for *tool_name*. Unknown tools default to HIGH_IMPACT —
    fail-closed, matching the pre-existing 'anything not explicitly exempt
    requires a challenge' behavior for any tool added after this module."""
    return TOOL_RISK_CLASS.get(tool_name, RiskClass.HIGH_IMPACT)


def requires_hitl(risk_class: RiskClass) -> bool:
    """Whether this risk class requires a HITL/NATO challenge before executing."""
    return risk_class in (RiskClass.REVERSIBLE, RiskClass.HIGH_IMPACT, RiskClass.LAB_ONLY)


def requires_trusted_lab(risk_class: RiskClass) -> bool:
    """Whether this risk class must be refused outright (not merely
    challenged) unless trusted-lab mode is explicitly enabled."""
    return risk_class is RiskClass.LAB_ONLY


def rollback_hint(risk_class: RiskClass, tool_name: str) -> str | None:
    """A short, operator-facing note on how to undo this action, for
    REVERSIBLE tools' challenge broadcast. None for other risk classes."""
    if risk_class is not RiskClass.REVERSIBLE:
        return None
    hints = {
        "create_document": "Delete the created file from ~/Downloads.",
        "packet_tracer_open": "Close the Packet Tracer window.",
        "set_clipboard": "Copy something else to restore the previous clipboard.",
        "abrir_packet_tracer": "Close the Packet Tracer window.",
        "generar_laboratorio_red": "Delete the generated .pkt file from ~/Downloads.",
    }
    return hints.get(tool_name, "This action changes local state and can be manually undone.")


# ── Shell sub-binary classification (informational — see module docstring) ──
# run_shell_command itself is always HIGH_IMPACT (see _HIGH_IMPACT_TOOLS
# above); this classifies the BINARY actually being invoked inside it, for
# audit/HUD display only. It does not gate anything on its own —
# tools/executor.py._validate_command (allowlist) and core/trust_engine.py
# (dynamic trust floor, see tests/test_trust_floor.py) remain the sole
# authorities for shell-command execution decisions, untouched by this
# retrofit.
_READ_ONLY_BINARIES: frozenset[str] = frozenset({
    "ping", "whois", "traceroute", "tracert", "netstat", "ipconfig",
    "ifconfig", "arp", "ps", "top", "htop", "tasklist", "df", "du", "free",
    "uname", "hostname", "whoami", "id", "ls", "dir", "cat", "type", "more",
    "grep", "find", "findstr", "head", "tail", "wc", "echo",
})

_LAB_ONLY_BINARIES: frozenset[str] = frozenset({
    "masscan", "nikto", "hydra", "sqlmap", "gobuster", "ffuf", "dirb",
    "msfconsole", "msfvenom", "sliver", "tcpdump", "tshark",
    "hashcat", "john", "responder", "crackmapexec",
})


def binary_risk_class(binary: str) -> RiskClass:
    """Informational risk class for a shell-command binary (not a tool
    name). Read-only diagnostic binaries -> READ_ONLY; the explicit lab
    allowlist -> LAB_ONLY; everything else allowlisted (python, git, ssh,
    curl, ...) -> HIGH_IMPACT (can execute arbitrary code / reach the
    network / move data). Purely descriptive — see module docstring."""
    name = (binary or "").strip().lower().removesuffix(".exe")
    if name in _READ_ONLY_BINARIES:
        return RiskClass.READ_ONLY
    if name in _LAB_ONLY_BINARIES:
        return RiskClass.LAB_ONLY
    return RiskClass.HIGH_IMPACT


def verify_consistent_with_legacy_sets(
    exempt_tools: frozenset[str], always_hitl_tools: frozenset[str]
) -> None:
    """Raise AssertionError if this module's classification would change
    tools/executor.py's actual HITL gating for any tool in either legacy
    set. Called at tools/executor.py import time — a drift here is a
    security bug (a tool silently becoming stricter or looser than the
    taxonomy claims), not a style nit.
    """
    for tool in exempt_tools:
        risk = classify_tool(tool)
        if requires_hitl(risk):
            raise AssertionError(
                f"SECURITY: risk_classes.py classifies exempt tool {tool!r} as "
                f"{risk.value} (requires HITL) — legacy _HITL_EXEMPT_TOOLS says "
                "it should not. Fix the classification before this drifts live."
            )
    for tool in always_hitl_tools:
        risk = classify_tool(tool)
        if not requires_hitl(risk):
            raise AssertionError(
                f"SECURITY: risk_classes.py classifies always-HITL tool {tool!r} "
                f"as {risk.value} (no HITL) — legacy _ALWAYS_HITL_TOOLS requires "
                "one unconditionally. Fix the classification before this drifts live."
            )
