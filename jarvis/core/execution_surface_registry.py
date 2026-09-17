"""core/execution_surface_registry.py — V69 M66B (§26): the machine-readable
map of every execution-capable surface and its containment disposition.

This is the source of truth the coverage gate (`scripts/check_execution_surfaces.py`)
checks. The desired invariant is:

    ARBITRARY_CODE_EXECUTION_SURFACES  ==  CONTAINMENT_POLICY_COVERED_SURFACES

A newly added arbitrary-execution path in a tool handler that is not declared
here fails CI. The rationale for each entry is in
`docs/v69_M66B_EXECUTION_SURFACE_MAP.md`.
"""
from __future__ import annotations

# ── Dispositions ──────────────────────────────────────────────────────────────
BROKER_REQUIRED = "BROKER_REQUIRED"          # arbitrary code → ContainmentBroker
RESTRICTED_ONLY = "RESTRICTED_ONLY"          # allowlist + HITL, no full sandbox
FIXED_INTERNAL_EXEMPT = "FIXED_INTERNAL_EXEMPT"
DOCUMENT_ONLY = "DOCUMENT_ONLY"


# ── Arbitrary-code-execution surfaces (tool handlers) ─────────────────────────
#: Every tool surface that can run caller-influenced code. Each MUST carry a
#: containment declaration. This dict IS the coverage set.
ARBITRARY_CODE_SURFACES: dict[str, dict] = {
    "code_execute": {
        "handler": "_tool_code_execute",
        "file": "tools/executor.py",
        "arbitrary_code": True,
        "caller_controlled_command": True,
        "risk_class": "HIGH_IMPACT",
        "hitl": True,
        "disposition": BROKER_REQUIRED,
        "default_requirement": "SANDBOX_REQUIRED",
        "gateway": "core.containment.ContainmentBroker",
    },
    "run_shell_command": {
        "handler": "_tool_run_shell_command",
        "file": "tools/executor.py",
        "arbitrary_code": True,          # via allowlisted interpreters (python file.py)
        "caller_controlled_command": True,
        "risk_class": "HIGH_IMPACT",
        "hitl": True,
        "disposition": RESTRICTED_ONLY,
        "controls": "allowlist + shell=False + `python -c` blocked + NATO HITL",
        "gateway": "_validate_command",
    },
    "red_team_shell": {
        "handler": "RedTeamShellExecutor.execute_shell",
        "file": "tools/executor.py",
        "arbitrary_code": True,
        "caller_controlled_command": True,
        "risk_class": "HIGH_IMPACT",
        "hitl": True,
        "disposition": RESTRICTED_ONLY,
        "controls": "YARA + hard-block + trust challenge + FULL_NATO + shell=False",
        "gateway": "RedTeamShellExecutor._classify",
    },
}

#: The subset the coverage gate treats as "must be contained by policy": the
#: names whose disposition is a real containment declaration.
CONTAINMENT_COVERED_SURFACES: frozenset[str] = frozenset(
    name for name, d in ARBITRARY_CODE_SURFACES.items()
    if d["disposition"] in (BROKER_REQUIRED, RESTRICTED_ONLY)
)


# ── Reviewed fixed-internal process launches (FIXED_INTERNAL_EXEMPT) ──────────
#: Fixed-argv infrastructure/diagnostic launches. Not caller-controlled arbitrary
#: code. Reviewed and enumerated so the static detector's exceptions are auditable
#: rather than implicit. Key: "path:function"; value: the fixed command.
FIXED_INTERNAL_SURFACES: dict[str, str] = {
    "core/asset_discovery.py:_run": "docker ps / vmrun list",
    "core/auto_remediator.py:execute_mitigation": "powershell",
    "core/capabilities.py:version": "<binary> --version",
    "core/dependency_guardian.py:_check_and_start": "ollama serve",
    "core/github_explorer.py:_clone_and_install": "git clone / pip install",
    "core/hardware_profile.py:_detect_storage_type": "powershell / lscpu",
    "core/hardware_profile.py:_cpu_info": "lscpu / sysctl",
    "core/hardware_model_profile.py:_probe": "system probe",
    "core/lab_manager.py:_vmrun": "vmrun",
    "core/mitigation.py:isolate_ip": "powershell New-NetFirewallRule",
    "core/network_quarantine.py:_run": "netsh / iptables (validated IP)",
    "core/pcap_capture.py:_run_capture": "tshark / tcpdump",
    "core/persistence_hunter.py:_authenticode_batch": "powershell",
    "core/punisher.py:isolate_ip": "netsh / iptables (validated IP)",
    "core/security_auditor.py:_block_port_firewall": "powershell New-NetFirewallRule",
    "core/vss_vaccine.py:_probe": "vssadmin / powershell",
    "core/windows_hardener.py:_apply_firewall_rule": "powershell / netsh",
    "core/decoy_filesystem.py:_enable": "auditpol / powershell",
    "tools/docker_manager.py:_compose_up": "docker compose up/down",
    "tools/forensic_volatility.py:trigger_forensic_capture": "vmrun / vol",
    "tools/ghost_hands.py:_execute_step": "launch validated app path",
    "tools/rf_bridge.py:_resolve_tshark_interface": "tshark",
    "tools/rf_oob.py:_capture": "RF tooling",
    "tools/resource_sentinel.py:_critical_loop": "vmrun",
    "tools/vmware_triage_runner.py:_take_snapshot": "vmrun snapshot",
    "tools/executor.py:_tool_open_application": "launch resolved app (shell=False)",
    "tools/executor.py:_tool_open_software": "launch resolved app (shell=False)",
    "tools/executor.py:_tool_packet_tracer_open": "launch Packet Tracer",
    "tools/executor.py:_tool_check_connectivity": "ping (validated argv)",
    "tools/executor.py:_tool_whois_lookup": "whois (validated domain)",
    "tools/executor.py:_tool_git_query": "git (validated operation)",
    "tools/executor.py:_tool_network_scan": "nmap (validated target)",
    "main.py:_loop_text": "pytest (self-test)",
    "mcp_servers/packet_tracer_bridge.py:launch": "launch Packet Tracer",
}


def coverage() -> tuple[frozenset[str], frozenset[str]]:
    """``(arbitrary_execution_surfaces, containment_covered_surfaces)`` — the two
    sets §26 requires to be equal for the tool-handler execution surface."""
    arbitrary = frozenset(
        name for name, d in ARBITRARY_CODE_SURFACES.items() if d["arbitrary_code"])
    return arbitrary, CONTAINMENT_COVERED_SURFACES
