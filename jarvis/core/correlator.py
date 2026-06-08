"""
core/correlator.py — Temporal cross-stream event correlation engine (v27.0).

Maintains a 90-second sliding event buffer across all telemetry sources.
Correlation rules match events by shared attributes (IP, PID) within a time window.
When a compound pattern fires, emits a CompoundIncident with kill chain, severity,
and MITRE mapping.
"""

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from loguru import logger


# ── Compound Incident ─────────────────────────────────────────────────────────

@dataclass
class CompoundIncident:
    incident_id:      str
    sub_events:       list[dict]
    first_seen:       float
    last_seen:        float
    kill_chain_phase: str
    severity_score:   float          # 0.0–10.0
    mitre_techniques: list[str]
    involved_hosts:   set[str]
    involved_pids:    set[int]
    status:           str = "ACTIVE" # ACTIVE | RESOLVED | ESCALATED

    def to_dict(self) -> dict:
        return {
            "incident_id":      self.incident_id,
            "sub_event_count":  len(self.sub_events),
            "first_seen":       datetime.fromtimestamp(
                                    self.first_seen, timezone.utc).isoformat(),
            "last_seen":        datetime.fromtimestamp(
                                    self.last_seen,  timezone.utc).isoformat(),
            "kill_chain_phase": self.kill_chain_phase,
            "severity_score":   round(self.severity_score, 2),
            "mitre_techniques": self.mitre_techniques,
            "involved_hosts":   list(self.involved_hosts),
            "involved_pids":    list(self.involved_pids),
            "status":           self.status,
        }


# ── Kill chain phase inference ────────────────────────────────────────────────

_KILL_CHAIN: dict[frozenset, str] = {
    frozenset({"T1046", "T1595", "T1018"}):         "Reconnaissance",
    frozenset({"T1059", "T1059.001", "T1204"}):      "Execution",
    frozenset({"T1055", "T1055.001", "T1055.012"}):  "Defense Evasion",
    frozenset({"T1003", "T1003.001"}):               "Credential Access",
    frozenset({"T1071", "T1071.004", "T1048"}):      "Command & Control",
    frozenset({"T1041", "T1567"}):                   "Exfiltration",
    frozenset({"T1547", "T1053", "T1562.001"}):      "Persistence",
}


def _infer_kill_chain(techniques: list[str]) -> str:
    t_set = set(techniques)
    for chain_set, phase in _KILL_CHAIN.items():
        if t_set & chain_set:
            return phase
    return "Unknown"


# ── Severity scoring ──────────────────────────────────────────────────────────

_SEVERITY_WEIGHTS: dict[str, float] = {
    "etw_threat_event":          3.0,
    "sysmon_event":              2.5,
    "canary_intrusion":          2.0,
    "dpi_alert":                 2.0,
    "ebpf_alert":                2.5,
    "neutralized":               1.5,
    "deception_tripped":         4.0,
    "kernel_anomaly_detected":   4.5,
    "binary_inversion_complete": 2.0,
}


def _base_severity(sub_events: list[dict]) -> float:
    total = sum(
        _SEVERITY_WEIGHTS.get(e.get("type", ""), 0.5)
        for e in sub_events
    )
    return min(total, 10.0)


# ── Correlation rules ─────────────────────────────────────────────────────────

@dataclass
class CorrelationRule:
    name:          str
    window_sec:    float
    min_events:    int
    match_attrs:   list[str]
    source_types:  set[str]
    mitre_hints:   list[str]
    severity_mult: float = 1.0


_RULES: list[CorrelationRule] = [
    CorrelationRule(
        name          = "lateral_movement_chain",
        window_sec    = 60,
        min_events    = 2,
        match_attrs   = ["attacker_ip", "src_ip"],
        source_types  = {"canary_intrusion", "dpi_alert", "etw_threat_event"},
        mitre_hints   = ["T1021", "T1046", "T1071"],
        severity_mult = 1.8,
    ),
    CorrelationRule(
        name          = "process_injection_sequence",
        window_sec    = 30,
        min_events    = 2,
        match_attrs   = ["pid", "process"],
        source_types  = {"sysmon_event", "etw_threat_event", "kernel_anomaly_detected"},
        mitre_hints   = ["T1055", "T1055.012"],
        severity_mult = 2.2,
    ),
    CorrelationRule(
        name          = "c2_beacon_detected",
        window_sec    = 90,
        min_events    = 3,
        match_attrs   = ["src_ip", "attacker_ip"],
        source_types  = {"dpi_alert", "canary_intrusion", "ebpf_alert"},
        mitre_hints   = ["T1071", "T1071.004", "T1048"],
        severity_mult = 2.5,
    ),
    CorrelationRule(
        name          = "credential_harvesting",
        window_sec    = 45,
        min_events    = 2,
        match_attrs   = ["process", "pid"],
        source_types  = {"sysmon_event", "etw_threat_event"},
        mitre_hints   = ["T1003", "T1003.001"],
        severity_mult = 3.0,
    ),
    CorrelationRule(
        name          = "deception_plus_network",
        window_sec    = 30,
        min_events    = 2,
        match_attrs   = ["source_pid", "attacker_ip"],
        source_types  = {"deception_tripped", "canary_intrusion", "dpi_alert"},
        mitre_hints   = ["T1036", "T1056"],
        severity_mult = 3.5,
    ),
]


# ── Correlator ────────────────────────────────────────────────────────────────

class TemporalCorrelator:
    """
    Sliding-window cross-stream event correlator.
    Maintains a 90-second rolling buffer and evaluates correlation rules on every ingest.
    """

    WINDOW_SEC     = 90
    PRUNE_INTERVAL = 15

    def __init__(self) -> None:
        self._buffer:      deque[dict]                  = deque()
        self._active:      dict[str, CompoundIncident]  = {}
        self._broadcast_fn                              = None
        self._prune_task:  asyncio.Task | None          = None
        # v36.0 — references for autonomous narration & predictions
        self._tts_ref       = None
        self._ollama_client = None
        self._fast_model    = "qwen2.5:7b-instruct-q5_K_M"
        self._deep_model    = "qwen2.5:14b-instruct-q4_K_M"

    def attach(self, broadcast_fn) -> None:
        self._broadcast_fn = broadcast_fn

    def attach_llm(self, tts, ollama_client, fast_model: str, deep_model: str) -> None:
        """v36.0 — wire LLM/TTS refs for autonomous prediction + narration."""
        self._tts_ref       = tts
        self._ollama_client = ollama_client
        self._fast_model    = fast_model
        self._deep_model    = deep_model

    async def start(self) -> None:
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = asyncio.create_task(
                self._prune_loop(), name="correlator-prune"
            )

    async def ingest_event(self, event):
        import asyncio as _a
        r = self.ingest(event)
        if _a.iscoroutine(r):
            return await r
        return r

    def add_event(self, event):
        return self.ingest(event)

    async def ingest(self, event: dict) -> None:
        now    = time.monotonic()
        stamped = {**event, "__mono_ts": now}
        self._buffer.append(stamped)
        for rule in _RULES:
            await self._evaluate_rule(rule, now)
        self._maybe_trigger_ram_hunt(event)
        self._maybe_quarantine(event)
        self._maybe_soar_then_report(event)
        self._maybe_dashboard(event)
        self._maybe_reverse(event)
        self._maybe_ntdll(event)
        self._maybe_mobile_alert(event)
        self._maybe_plugin_route(event)

    def _maybe_plugin_route(self, event: dict) -> None:
        """V55.0: route eligible high-severity events through loaded plugins."""
        try:
            if event.get("_plugin_enriched"):
                return
            sev = float(event.get("severity", 0) or 0)
            if sev < 7.0:
                return
            from core import plugin_loader
            if plugin_loader.LOADED_PLUGINS:
                import asyncio
                asyncio.create_task(plugin_loader.route_event(event))
        except Exception:
            pass

    def _maybe_dashboard(self, event: dict) -> None:
        """V52.0: stream events to the local AEGIS C2 dashboard (sev>5 filtered there)."""
        try:
            from core import c2_dashboard
            c2_dashboard.push(event)
        except Exception:
            pass

    def _maybe_mobile_alert(self, event: dict) -> None:
        """V54.0: push sev>=8 events to the operator's phone (filter inside mobile_c2.push)."""
        try:
            from core import mobile_c2
            mobile_c2.push(event)
        except Exception:
            pass

    def _maybe_reverse(self, event: dict) -> None:
        """V49.0: static-triage any unverified PE referenced by an event."""
        try:
            if event.get("source") == "ai_reverser":
                return
            path = (event.get("file_path") or event.get("path")
                    or event.get("decoy"))
            if not path:
                return
            if not str(path).lower().endswith((".exe", ".dll", ".sys", ".scr", ".cpl")):
                return
            import asyncio
            from core import ai_reverser
            asyncio.create_task(ai_reverser.analyze(str(path), correlator=self))
        except Exception:
            pass

    def _maybe_ntdll(self, event: dict) -> None:
        """V49.0: on high-severity injection events, check the process for
        ntdll unhooking/inline hooks."""
        try:
            from core import detection_harness as _dh
            if event.get("simulation") or getattr(_dh, "SIM_ACTIVE", False):
                return
        except Exception:
            if event.get("simulation"):
                return
        try:
            if event.get("source") in ("ntdll_monitor", "ram_hunter"):
                return
            sev = float(event.get("severity", 0) or 0)
            etype = str(event.get("type", "")).lower()
            attck = event.get("attck") or event.get("technique") or []
            if isinstance(attck, str):
                attck = [attck]
            pid = event.get("pid")
            inj = ("inject" in etype or
                   any(str(t).upper().startswith("T1055") for t in attck))
            if pid and sev >= 8.0 and inj:
                import asyncio
                from core import ntdll_monitor
                asyncio.create_task(ntdll_monitor.scan_pid(
                    int(pid), reason=f"correlator:{etype}", correlator=self))
        except Exception:
            pass

    def _maybe_quarantine(self, event: dict) -> None:
        """V48.0: contain a malicious local host on high-severity lateral
        movement / scanning. Endpoint + (optional) NAC isolation only."""
        try:
            from core import detection_harness as _dh
            if event.get("simulation") or getattr(_dh, "SIM_ACTIVE", False):
                return
        except Exception:
            if event.get("simulation"):
                return
        try:
            if event.get("source") in ("network_quarantine", "ir_reporter", "ram_hunter"):
                return
            sev = float(event.get("severity", 0) or 0)
            etype = str(event.get("type", "")).lower()
            attck = event.get("attck") or event.get("technique") or []
            if isinstance(attck, str):
                attck = [attck]
            ip = (event.get("src_ip") or event.get("source_ip")
                  or event.get("remote_ip") or event.get("ip"))
            is_net = ("lateral" in etype or "scan" in etype or
                      any(str(t).upper().split(".")[0] in ("T1021", "T1046", "T1018")
                          for t in attck))
            if ip and sev >= 9.0 and is_net:
                import asyncio
                from core import network_quarantine
                asyncio.create_task(network_quarantine.quarantine(
                    str(ip), reason=f"correlator:{etype}", correlator=self))
        except Exception:
            pass

    def _maybe_soar_then_report(self, event: dict) -> None:
        """V50.0: enrich criticals with external CTI, THEN emit IR report."""
        try:
            if event.get("source") == "ir_reporter":
                return
            sev = float(event.get("severity", 0) or 0)
            mitigated = bool(event.get("mitigated") or event.get("killed_pids")
                             or event.get("host_isolated"))
            if not (sev >= 9.0 or mitigated):
                return
            import asyncio
            asyncio.create_task(self._soar_report_task(event))
        except Exception:
            pass

    async def _soar_report_task(self, event: dict) -> None:
        try:
            from core import soar_enrichment
            await soar_enrichment.enrich(event)   # mutates event in place
        except Exception:
            pass
        try:
            from core import ir_reporter
            await ir_reporter.generate_report(event, correlator=self)
        except Exception:
            pass

    def _maybe_ir_report(self, event: dict) -> None:
        """V48.0: emit a compliance IR report for mitigated/critical incidents."""
        try:
            if event.get("source") == "ir_reporter":
                return
            sev = float(event.get("severity", 0) or 0)
            mitigated = bool(event.get("mitigated") or event.get("killed_pids")
                             or event.get("host_isolated"))
            if sev >= 9.0 or mitigated:
                import asyncio
                from core import ir_reporter
                asyncio.create_task(ir_reporter.generate_report(event, correlator=self))
        except Exception:
            pass

    def _maybe_trigger_ram_hunt(self, event: dict) -> None:
        """V47.0: on high-severity injection events, scan the live process
        memory via ram_hunter. Re-entry guarded against ram_hunter's own
        feedback events to avoid scan loops."""
        try:
            if event.get("source") == "ram_hunter":
                return
            sev = float(event.get("severity", 0) or 0)
            etype = str(event.get("type", "")).lower()
            attck = event.get("attck") or event.get("technique") or []
            if isinstance(attck, str):
                attck = [attck]
            pid = event.get("pid")
            is_injection = ("inject" in etype or
                            any(str(t).upper().startswith("T1055") for t in attck))
            if pid and sev >= 8.0 and is_injection:
                import asyncio
                from core import ram_hunter
                asyncio.create_task(ram_hunter.hunt(
                    int(pid), reason=f"correlator:{etype}", correlator=self))
        except Exception:
            # Never let the responder break correlation.
            pass

    async def _evaluate_rule(self, rule: CorrelationRule, now: float) -> None:
        window_start = now - rule.window_sec
        candidates = [
            e for e in self._buffer
            if e.get("__mono_ts", 0) >= window_start
            and e.get("type", "") in rule.source_types
        ]
        if len(candidates) < rule.min_events:
            return

        # Group events by VALUE across all match_attrs (attr names may differ per source)
        val_groups: dict[str, list[dict]] = {}
        for e in candidates:
            for attr in rule.match_attrs:
                val = e.get(attr)
                if val is not None:
                    key = str(val)
                    bucket = val_groups.setdefault(key, [])
                    if e not in bucket:
                        bucket.append(e)

        for group_val, group_events in val_groups.items():
            if len(group_events) < rule.min_events:
                continue

            incident_key = f"{rule.name}:{group_val}"

            if incident_key in self._active:
                inc = self._active[incident_key]
                new_events = [e for e in group_events if e not in inc.sub_events]
                inc.sub_events.extend(new_events)
                inc.last_seen = now
                inc.severity_score = min(
                    _base_severity(inc.sub_events) * rule.severity_mult, 10.0
                )
                for attr in ("attacker_ip", "src_ip"):
                    v = group_events[0].get(attr)
                    if v:
                        inc.involved_hosts.add(v)
            else:
                techniques = list(rule.mitre_hints)
                for e in group_events:
                    t = e.get("technique", "") or e.get("mitre_technique", "")
                    if t:
                        techniques.append(t)
                techniques = list(dict.fromkeys(techniques))

                inc = CompoundIncident(
                    incident_id      = str(uuid.uuid4())[:8].upper(),
                    sub_events       = list(group_events),
                    first_seen       = min(
                        e.get("__mono_ts", now) for e in group_events
                    ),
                    last_seen        = now,
                    kill_chain_phase = _infer_kill_chain(techniques),
                    severity_score   = min(
                        _base_severity(group_events) * rule.severity_mult, 10.0
                    ),
                    mitre_techniques = techniques,
                    involved_hosts   = {
                        e.get("attacker_ip") or e.get("src_ip") or ""
                        for e in group_events
                        if e.get("attacker_ip") or e.get("src_ip")
                    },
                    involved_pids    = {
                        e["pid"]
                        for e in group_events
                        if isinstance(e.get("pid"), int)
                    },
                )
                self._active[incident_key] = inc

                logger.warning(
                    f"CORRELATOR: compound incident {inc.incident_id} "
                    f"[{rule.name}] phase={inc.kill_chain_phase} "
                    f"severity={inc.severity_score:.1f}"
                )

                try:
                    from core.episodic_memory import store_episode
                    asyncio.create_task(store_episode(
                        str(inc.to_dict()),
                        "compound_incident",
                        severity="CRITICAL" if inc.severity_score >= 7 else "HIGH",
                        mitre_tags=inc.mitre_techniques,
                    ))
                except Exception:
                    pass

                # v33.0 — auto-generate Sigma rule + export STIX IOCs (fire-and-forget)
                if self._broadcast_fn:
                    _enriched_inc = {**inc.to_dict(), "sub_events": list(inc.sub_events)}
                    try:
                        from core.sigma_generator import generate_sigma_rule
                        asyncio.create_task(generate_sigma_rule(
                            _enriched_inc, self._broadcast_fn
                        ))
                    except Exception:
                        pass
                    try:
                        from tools.ioc_extractor import export_incident_stix
                        asyncio.create_task(export_incident_stix(
                            _enriched_inc, self._broadcast_fn
                        ))
                    except Exception:
                        pass

            if self._broadcast_fn:
                await self._broadcast_fn({
                    "type": "compound_incident",
                    "rule": rule.name,
                    **inc.to_dict(),
                })

                # v43.0 — feed purple coordinator: every technique resolved
                # by the correlator counts as a successful detection
                try:
                    from core.purple_coordinator import register_detection_event
                    for technique in inc.mitre_techniques:
                        asyncio.create_task(register_detection_event(
                            technique, "correlator", self._broadcast_fn,
                        ))
                except Exception as e:
                    logger.debug(f"CORRELATOR: purple register failed: {e}")

                # v36.0 — Predictive cognition + autonomous narration
                try:
                    from core.threat_predictor  import analyze_and_predict
                    from core.tactical_narrator import narrate_incident

                    async def _predict_and_narrate(_inc_dict, _self=self):
                        preds = await analyze_and_predict(
                            _inc_dict, _self._broadcast_fn
                        )
                        if _self._ollama_client is not None:
                            await narrate_incident(
                                _inc_dict, preds,
                                _self._tts_ref,
                                _self._broadcast_fn,
                                _self._ollama_client,
                                _self._fast_model,
                            )

                    asyncio.create_task(
                        _predict_and_narrate(inc.to_dict()),
                        name=f"v36-predict-{inc.incident_id}",
                    )
                except Exception as e:
                    logger.debug(f"CORRELATOR: v36 prediction dispatch failed: {e}")

            # v28.0 — fire SOAR playbook engine before agentic loop
            try:
                from core.playbook_engine import playbook_engine
                asyncio.create_task(playbook_engine.evaluate({
                    "rule": rule.name,
                    **inc.to_dict(),
                }))
            except Exception as e:
                logger.debug(f"CORRELATOR: playbook engine dispatch failed: {e}")

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self.PRUNE_INTERVAL)
            cutoff = time.monotonic() - self.WINDOW_SEC
            while self._buffer and self._buffer[0].get("__mono_ts", 0) < cutoff:
                self._buffer.popleft()

            resolve_cutoff = time.monotonic() - 120
            for key, inc in list(self._active.items()):
                if inc.last_seen < resolve_cutoff and inc.status == "ACTIVE":
                    inc.status = "RESOLVED"
                    if self._broadcast_fn:
                        await self._broadcast_fn({
                            "type":        "compound_incident_resolved",
                            "incident_id": inc.incident_id,
                            "duration_s":  round(inc.last_seen - inc.first_seen, 1),
                        })
                    del self._active[key]

    def get_active_incidents(self) -> list[dict]:
        return [inc.to_dict() for inc in self._active.values()]


# Module-level singleton
correlator = TemporalCorrelator()
