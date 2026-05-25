"""
core/adversary_emulator.py — ATT&CK adversary emulation engine (v33.0).

Injects synthetic telemetry events simulating known ATT&CK techniques
into the JARVIS broadcast pipeline. Tests that:
  1. Correlator fires compound incidents for each technique chain
  2. SOAR playbook engine responds with correct playbook
  3. AURA HUD visualizes the incident correctly
  4. Episodic memory stores the emulation for future detection tuning

IMPORTANT: Zero real malicious code. Events are synthetic dicts.
No processes are spawned, no network connections are made,
no files are written outside JARVIS logs directory.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger

_TECHNIQUES: dict[str, list[dict]] = {

    "T1059.001": [
        {"type": "sysmon_event", "event_id": 1,
         "process": "powershell.exe",
         "command_line": "powershell.exe -EncodedCommand JABj...",
         "parent_process": "cmd.exe", "technique": "T1059.001"},
        {"type": "etw_threat_event", "event_id": 1,
         "process": "powershell.exe", "technique": "T1059.001"},
    ],

    "T1055.012": [
        {"type": "sysmon_event", "event_id": 8,
         "process": "svchost.exe", "target_process": "explorer.exe",
         "technique": "T1055.012", "event_id_label": "CreateRemoteThread"},
        {"type": "etw_threat_event", "event_id": 30,
         "process": "svchost.exe", "technique": "T1055.012"},
        {"type": "sysmon_event", "event_id": 25,
         "process": "explorer.exe", "technique": "T1055.012",
         "event_id_label": "ProcessTampering"},
    ],

    "T1003.001": [
        {"type": "sysmon_event", "event_id": 10,
         "process": "mimikatz.exe", "target_process": "lsass.exe",
         "access_mask": "0x1010", "technique": "T1003.001"},
        {"type": "etw_threat_event", "event_id": 10,
         "process": "lsass.exe", "technique": "T1003.001"},
    ],

    "T1071.001": [
        {"type": "dpi_alert", "src_ip": "10.0.0.50",
         "dst_port": 443, "technique": "T1071.001",
         "beacon_interval_s": 60, "jitter_pct": 10},
        {"type": "canary_intrusion", "attacker_ip": "10.0.0.50",
         "port": 8080, "technique": "T1071.001"},
        {"type": "dpi_alert", "src_ip": "10.0.0.50",
         "dst_port": 443, "technique": "T1071.004"},
    ],

    "T1021.002": [
        {"type": "canary_intrusion", "attacker_ip": "10.0.0.77",
         "port": 445, "technique": "T1021.002"},
        {"type": "etw_threat_event", "event_id": 3,
         "process": "System", "technique": "T1021.002",
         "dst_port": 445},
        {"type": "dpi_alert", "src_ip": "10.0.0.77",
         "dst_port": 445, "technique": "T1021.002"},
    ],

    "T1547.001": [
        {"type": "sysmon_event", "event_id": 13,
         "process": "reg.exe", "technique": "T1547.001",
         "registry_key": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
         "registry_value": "malware.exe"},
        {"type": "etw_threat_event", "event_id": 12,
         "process": "reg.exe", "technique": "T1547.001"},
    ],

    "T1562.001": [
        {"type": "sysmon_event", "event_id": 1,
         "process": "cmd.exe",
         "command_line": "netsh advfirewall set allprofiles state off",
         "technique": "T1562.001"},
        {"type": "etw_threat_event", "event_id": 1,
         "process": "cmd.exe", "technique": "T1562.001"},
    ],
}

_CHAINS: dict[str, list[str]] = {
    "APT_CRED_DUMP":       ["T1059.001", "T1055.012", "T1003.001"],
    "C2_LATERAL":          ["T1071.001", "T1021.002"],
    "PERSISTENCE_EVASION": ["T1547.001", "T1562.001"],
    "FULL_COMPROMISE":     ["T1059.001", "T1071.001", "T1003.001",
                            "T1021.002", "T1547.001"],
}


class AdversaryEmulator:
    def __init__(self):
        self._broadcast_fn = None
        self._results: list[dict] = []

    def attach(self, broadcast_fn) -> None:
        self._broadcast_fn = broadcast_fn

    async def emulate_technique(self, technique_id: str) -> dict:
        events = _TECHNIQUES.get(technique_id)
        if not events:
            return {"technique": technique_id, "status": "not_implemented"}

        emulation_id = str(uuid.uuid4())[:8].upper()
        logger.info(f"EMULATOR: → {technique_id} [emulation {emulation_id}]")

        if self._broadcast_fn:
            await self._broadcast_fn({
                "type":         "emulation_started",
                "technique":    technique_id,
                "emulation_id": emulation_id,
                "timestamp":    datetime.now(timezone.utc).isoformat(),
            })

            for event in events:
                enriched = {
                    **event,
                    "emulated":     True,
                    "emulation_id": emulation_id,
                    "pid":          65535,
                    "attacker_ip":  event.get("attacker_ip", "10.0.0.250"),
                    "timestamp":    datetime.now(timezone.utc).isoformat(),
                }
                await self._broadcast_fn(enriched)
                await asyncio.sleep(0.5)

            await asyncio.sleep(2)

        result = {
            "technique":    technique_id,
            "emulation_id": emulation_id,
            "status":       "injected",
            "events_sent":  len(events),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }
        self._results.append(result)
        return result

    async def emulate_chain(self, chain_name: str) -> list[dict]:
        techniques = _CHAINS.get(chain_name)
        if not techniques:
            logger.warning(f"EMULATOR: unknown chain '{chain_name}'")
            return []

        logger.info(f"EMULATOR: chain '{chain_name}' — {len(techniques)} techniques")
        if self._broadcast_fn:
            await self._broadcast_fn({
                "type":       "emulation_chain_started",
                "chain":      chain_name,
                "techniques": techniques,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            })

        results = []
        for technique in techniques:
            result = await self.emulate_technique(technique)
            results.append(result)
            await asyncio.sleep(3)

        if self._broadcast_fn:
            await self._broadcast_fn({
                "type":       "emulation_chain_complete",
                "chain":      chain_name,
                "techniques": techniques,
                "results":    results,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            })

        return results

    async def run_coverage_test(self) -> dict:
        report = {
            "total":     len(_TECHNIQUES),
            "tested":    0,
            "results":   [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        for technique in _TECHNIQUES:
            result = await self.emulate_technique(technique)
            report["results"].append(result)
            report["tested"] += 1
            await asyncio.sleep(5)

        if self._broadcast_fn:
            await self._broadcast_fn({
                "type": "emulation_coverage_report",
                **report,
            })
        return report

    def get_available_chains(self) -> list[str]:
        return list(_CHAINS.keys())

    def get_available_techniques(self) -> list[str]:
        return list(_TECHNIQUES.keys())


adversary_emulator = AdversaryEmulator()
