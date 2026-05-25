"""
core/attck_coverage.py — ATT&CK detection coverage matrix (v33.0).

Maps JARVIS detection sources to ATT&CK techniques.
Coverage levels: FULL (multiple sources), PARTIAL (one source), BLIND (none).
Broadcasts to AURA HUD for the coverage heat map visualization.
"""

from datetime import datetime, timezone

from loguru import logger

_COVERAGE: dict[str, list[str]] = {
    # Execution
    "T1059.001": ["etw", "sysmon"],
    "T1059.003": ["etw", "sysmon"],
    "T1059.005": ["sysmon"],
    "T1204.002": ["sysmon", "yara"],

    # Defense Evasion
    "T1055.001": ["etw", "sysmon"],
    "T1055.012": ["etw", "sysmon"],
    "T1055.004": ["etw"],
    "T1562.001": ["etw", "sysmon"],
    "T1070.004": ["sysmon"],
    "T1027":     ["yara"],

    # Credential Access
    "T1003.001": ["etw", "sysmon"],
    "T1003.002": ["sysmon"],
    "T1110.003": ["canary", "zeek"],
    "T1558.003": ["etw"],

    # Discovery
    "T1046":     ["canary", "zeek"],
    "T1069":     ["etw", "sysmon"],
    "T1018":     ["zeek", "canary"],

    # Lateral Movement
    "T1021.002": ["canary", "zeek"],
    "T1021.001": ["canary"],
    "T1021.006": ["etw"],
    "T1550.002": ["etw"],

    # Collection
    "T1560.001": ["sysmon", "yara"],
    "T1056.001": ["etw"],

    # Command and Control
    "T1071.001": ["zeek", "ebpf"],
    "T1071.004": ["zeek"],
    "T1048":     ["zeek", "ebpf"],
    "T1095":     ["zeek"],
    "T1573.001": ["zeek"],

    # Persistence
    "T1547.001": ["sysmon"],
    "T1053.005": ["sysmon"],
    "T1543.003": ["etw", "sysmon"],

    # Impact
    "T1486":     ["yara", "sysmon"],
    "T1489":     ["etw"],

    # BLIND
    "T1190":     [],
    "T1133":     [],
    "T1566.001": [],
    "T1195":     [],
    "T1040":     [],
    "T1557":     [],
}

_TACTICS: dict[str, list[str]] = {
    "Execution":         ["T1059.001", "T1059.003", "T1059.005", "T1204.002"],
    "Defense Evasion":   ["T1055.001", "T1055.012", "T1055.004", "T1562.001",
                          "T1070.004", "T1027"],
    "Credential Access": ["T1003.001", "T1003.002", "T1110.003", "T1558.003"],
    "Discovery":         ["T1046", "T1069", "T1018"],
    "Lateral Movement":  ["T1021.002", "T1021.001", "T1021.006", "T1550.002"],
    "Collection":        ["T1560.001", "T1056.001"],
    "Command & Control": ["T1071.001", "T1071.004", "T1048", "T1095", "T1573.001"],
    "Persistence":       ["T1547.001", "T1053.005", "T1543.003"],
    "Impact":            ["T1486", "T1489"],
    "Initial Access":    ["T1190", "T1133", "T1566.001", "T1195"],
}


def get_coverage_matrix() -> dict:
    tactics_data: list[dict] = []
    total_covered = 0
    total_partial = 0
    total_blind   = 0

    for tactic, techniques in _TACTICS.items():
        tactic_techniques: list[dict] = []
        for tech in techniques:
            detectors = _COVERAGE.get(tech, [])
            if len(detectors) >= 2:
                level = "full"
                total_covered += 1
            elif len(detectors) == 1:
                level = "partial"
                total_partial += 1
            else:
                level = "blind"
                total_blind += 1

            tactic_techniques.append({
                "id":        tech,
                "detectors": detectors,
                "level":     level,
            })

        tactics_data.append({
            "name":       tactic,
            "techniques": tactic_techniques,
        })

    total = total_covered + total_partial + total_blind
    coverage_pct = round(
        (total_covered + total_partial * 0.5) / total * 100, 1
    ) if total else 0

    return {
        "type":         "attck_coverage_matrix",
        "tactics":      tactics_data,
        "total":        total,
        "covered":      total_covered,
        "partial":      total_partial,
        "blind":        total_blind,
        "coverage_pct": coverage_pct,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }


async def broadcast_coverage(broadcast_fn) -> None:
    matrix = get_coverage_matrix()
    logger.info(
        f"ATTCK_COVERAGE: {matrix['coverage_pct']}% coverage — "
        f"full={matrix['covered']} partial={matrix['partial']} "
        f"blind={matrix['blind']}"
    )
    await broadcast_fn(matrix)
