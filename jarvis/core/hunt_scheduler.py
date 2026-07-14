"""
core/hunt_scheduler.py — Autonomous threat hunt engine (v45.0).

Proactively hunts for threats using 12 ATT&CK hypotheses.
Runs every 4 hours. LLM analyzes hunt data and generates findings.
Pushes results to Telegram if findings exceed threshold.

Hunt hypotheses:
  H01 — Beaconing detection (periodic outbound connections)
  H02 — Credential access (LSASS-touching processes)
  H03 — Lateral movement (new SMB connections)
  H04 — Process injection (RWX memory allocation patterns)
  H05 — Persistence (new registry run keys)
  H06 — Defense evasion (security tool termination)
  H07 — Exfiltration (large outbound transfers)
  H08 — Command & Control (DNS high-entropy subdomains)
  H09 — Discovery (rapid network enumeration)
  H10 — Privilege escalation (token manipulation events)
  H11 — Living off the land (LOLBin execution patterns)
  H12 — Supply chain (new unsigned DLL loads)
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

from core.lifecycle import is_stopping as _lifecycle_stopping

_HUNT_INTERVAL_H = 4
_HUNT_INTERVAL_S = _HUNT_INTERVAL_H * 3600
_WARMUP_S        = 1800  # 30-minute warmup before the first sweep
_HUNT_LOG_DIR    = Path("logs/hunt_results")
_HUNT_LOG_DIR.mkdir(parents=True, exist_ok=True)

_HYPOTHESES = [
    {
        "id":          "H01",
        "name":        "Beaconing Detection",
        "technique":   "T1071",
        "query":       "periodic outbound connection beacon C2 interval",
        "description": "Hunt for connections with regular timing intervals (±jitter) suggesting C2 beaconing",
    },
    {
        "id":          "H02",
        "name":        "Credential Access via LSASS",
        "technique":   "T1003.001",
        "query":       "lsass memory access credential dump mimikatz",
        "description": "Hunt for processes accessing LSASS memory",
    },
    {
        "id":          "H03",
        "name":        "Lateral Movement via SMB",
        "technique":   "T1021.002",
        "query":       "SMB connection lateral movement new host",
        "description": "Hunt for new SMB connections to previously unseen hosts",
    },
    {
        "id":          "H04",
        "name":        "Process Injection Artifacts",
        "technique":   "T1055",
        "query":       "process injection RWX memory VirtualAllocEx WriteProcessMemory",
        "description": "Hunt for RWX page allocation and cross-process memory writes",
    },
    {
        "id":          "H05",
        "name":        "Persistence via Registry",
        "technique":   "T1547.001",
        "query":       "registry run key persistence startup modification",
        "description": "Hunt for new run key additions in HKLM and HKCU",
    },
    {
        "id":          "H06",
        "name":        "Defense Evasion — Tool Termination",
        "technique":   "T1562.001",
        "query":       "security tool termination antivirus disabled defender",
        "description": "Hunt for termination of security processes",
    },
    {
        "id":          "H07",
        "name":        "Data Exfiltration",
        "technique":   "T1041",
        "query":       "large outbound transfer exfiltration bytes sent",
        "description": "Hunt for unusually large outbound data transfers",
    },
    {
        "id":          "H08",
        "name":        "DNS Tunneling C2",
        "technique":   "T1071.004",
        "query":       "DNS high entropy subdomain tunnel exfiltration",
        "description": "Hunt for high-entropy DNS subdomain queries indicating tunneling",
    },
    {
        "id":          "H09",
        "name":        "Network Discovery",
        "technique":   "T1046",
        "query":       "network scan port scan host discovery enumeration",
        "description": "Hunt for rapid connection attempts across multiple hosts/ports",
    },
    {
        "id":          "H10",
        "name":        "Privilege Escalation",
        "technique":   "T1134",
        "query":       "token impersonation privilege escalation SeDebugPrivilege",
        "description": "Hunt for token manipulation and privilege escalation attempts",
    },
    {
        "id":          "H11",
        "name":        "Living Off the Land Binaries",
        "technique":   "T1218",
        "query":       "LOLBin certutil mshta regsvr32 rundll32 wscript suspicious",
        "description": "Hunt for LOLBin abuse: certutil, mshta, regsvr32 with unusual args",
    },
    {
        "id":          "H12",
        "name":        "Unsigned DLL Loading",
        "technique":   "T1574",
        "query":       "unsigned DLL loaded new module injection hijack",
        "description": "Hunt for unsigned or newly appeared DLL loads in trusted processes",
    },
]

_HUNT_SYSTEM = """You are a Tier 3 threat hunter at a SOC.
Given JARVIS telemetry from the last 24 hours,
analyze the provided data for the hunt hypothesis.

Respond as JSON:
{
  "findings": ["specific finding 1", "finding 2"],
  "verdict": "POSITIVE|NEGATIVE|INCONCLUSIVE",
  "confidence": 0-100,
  "recommendation": "what the operator should do next",
  "mitre_evidence": ["T-code", "T-code"]
}
Output ONLY the JSON."""


async def run_single_hunt(
    hypothesis_index: int,
    broadcast_fn=None,
    ollama_client=None,
    model: str = "",
) -> dict:
    """Run a single hunt hypothesis and return findings."""
    if hypothesis_index >= len(_HYPOTHESES):
        hypothesis_index = 0

    # V69 M54.12 — never begin a hypothesis once shutdown has started. This is the
    # exact guard the live run was missing: "HUNT: running H04" appeared after
    # shutdown had begun.
    if _lifecycle_stopping():
        logger.debug("HUNT: skipped — shutdown in progress")
        return {"hypothesis": "", "id": "", "verdict": "SKIPPED", "findings": [],
                "confidence": 0, "recommendation": "shutdown in progress"}

    hyp = _HYPOTHESES[hypothesis_index]
    logger.info(f"HUNT: running {hyp['id']} — {hyp['name']}")

    if broadcast_fn:
        await broadcast_fn({
            "type":        "hunt_started",
            "hypothesis":  hyp["id"],
            "name":        hyp["name"],
            "technique":   hyp["technique"],
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

    # Query episodic memory for relevant events
    telemetry = ""
    try:
        from core.knowledge import get_vault
        vault   = get_vault()
        results = vault.search(hyp["query"], top_k=8)
        telemetry = "\n".join(
            r.get("content", "")[:300] for r in results
        )
    except Exception:
        pass

    if not telemetry:
        result = {
            "hypothesis": hyp["name"],
            "id":         hyp["id"],
            "technique":  hyp["technique"],
            "verdict":    "INCONCLUSIVE",
            "findings":   [],
            "confidence": 0,
            "recommendation": "Insufficient telemetry — check data sources",
        }
    elif ollama_client:
        prompt = (
            f"HUNT HYPOTHESIS: {hyp['description']}\n"
            f"TECHNIQUE: {hyp['technique']}\n\n"
            f"JARVIS TELEMETRY (last 24h):\n{telemetry[:2000]}\n\n"
            "Analyze for evidence of this hypothesis:"
        )
        try:
            resp = await asyncio.wait_for(
                ollama_client.chat.completions.create(
                    model    = model,
                    messages = [
                        {"role": "system", "content": _HUNT_SYSTEM},
                        {"role": "user",   "content": prompt},
                    ],
                    stream = False,
                    extra_body = {"options": {
                        "num_ctx": 2048, "temperature": 0.1
                    }},
                ),
                timeout=45.0,
            )
            import json, re
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'\s*```$', '', text).strip()
            parsed = json.loads(text)
            result = {
                "hypothesis":    hyp["name"],
                "id":            hyp["id"],
                "technique":     hyp["technique"],
                "verdict":       parsed.get("verdict", "INCONCLUSIVE"),
                "findings":      parsed.get("findings", []),
                "confidence":    parsed.get("confidence", 0),
                "recommendation":parsed.get("recommendation", ""),
                "mitre_evidence":parsed.get("mitre_evidence", []),
            }
        except Exception as e:
            result = {
                "hypothesis": hyp["name"],
                "id":         hyp["id"],
                "technique":  hyp["technique"],
                "verdict":    "INCONCLUSIVE",
                "findings":   [],
                "confidence": 0,
                "recommendation": f"LLM error: {e}",
            }
    else:
        result = {
            "hypothesis": hyp["name"],
            "id":         hyp["id"],
            "technique":  hyp["technique"],
            "verdict":    "INCONCLUSIVE",
            "findings":   [],
            "confidence": 0,
            "recommendation": "No LLM client attached",
        }

    result["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Save hunt result
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"hunt_{hyp['id']}_{ts}.json"
    import json
    (_HUNT_LOG_DIR / fname).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )

    if broadcast_fn:
        severity = (
            "CRITICAL" if result["verdict"] == "POSITIVE"
                       and result["confidence"] >= 70
            else "HIGH" if result["verdict"] == "POSITIVE"
            else "INFO"
        )
        await broadcast_fn({
            "type":       "hunt_complete",
            "hypothesis": hyp["id"],
            "name":       hyp["name"],
            "verdict":    result["verdict"],
            "confidence": result["confidence"],
            "findings":   len(result["findings"]),
            "severity":   severity,
            "timestamp":  result["timestamp"],
        })

        # Push positive findings to Telegram
        if (result["verdict"] == "POSITIVE"
                and result["confidence"] >= 60):
            from core.telegram_bridge import push_alert
            findings_text = "\n".join(
                f"• {f[:80]}" for f in result["findings"][:3]
            )
            await push_alert(
                f"HUNT POSITIVE — {hyp['name']}",
                f"Confidence: {result['confidence']}%\n\n"
                f"{findings_text}\n\n"
                f"Rec: {result['recommendation'][:150]}",
                severity=severity,
            )

    return result


async def run_all_hunts(
    broadcast_fn,
    ollama_client,
    model: str,
) -> list[dict]:
    """Run all 12 hypotheses sequentially."""
    results = []
    logger.info("HUNT_SCHEDULER: running full hypothesis sweep")

    await broadcast_fn({
        "type":        "hunt_sweep_started",
        "hypotheses":  len(_HYPOTHESES),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    })

    for i, hyp in enumerate(_HYPOTHESES):
        # Stop the sweep immediately if shutdown began mid-sweep — no further
        # hypothesis is started after STOPPING.
        if _lifecycle_stopping():
            logger.info("HUNT_SCHEDULER: sweep aborted — shutdown in progress")
            break
        result = await run_single_hunt(
            i, broadcast_fn, ollama_client, model
        )
        results.append(result)
        await asyncio.sleep(3)   # brief pause between hunts

    positives = [r for r in results
                 if r["verdict"] == "POSITIVE"]

    await broadcast_fn({
        "type":       "hunt_sweep_complete",
        "total":      len(results),
        "positives":  len(positives),
        "techniques": [r["technique"] for r in positives],
        "severity":   "HIGH" if positives else "INFO",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })

    if positives:
        from core.telegram_bridge import push_alert
        summary = "\n".join(
            f"• {r['hypothesis']} ({r['confidence']}%)"
            for r in positives[:5]
        )
        await push_alert(
            "HUNT SWEEP COMPLETE — POSITIVE FINDINGS",
            f"{len(positives)} of {len(results)} "
            f"hypotheses positive:\n{summary}",
            "HIGH",
        )

    return results


def _current_resource_state() -> tuple["bool | str", float, float]:
    """Best-effort (battery_state, cpu_pct, ram_pct) snapshot for
    should_run_background_tasks(). Fails open (returns 0%/plugged-in) if
    psutil is unavailable — never blocks the hunt on a monitoring failure."""
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=None)
        ram_pct = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        on_battery = bool(battery is not None and not battery.power_plugged)
        return on_battery, cpu_pct, ram_pct
    except Exception:
        return False, 0.0, 0.0


async def start_hunt_scheduler(
    broadcast_fn,
    ollama_client,
    model: str,
    state=None,
) -> None:
    """
    Background scheduler. Runs full hypothesis sweep
    every 4 hours. First run after 30-minute warmup.

    ``state`` (core.assistant_state.AssistantState, V62.0 Phase 8): gates
    each sweep via core.ironman_mode.should_run_background_tasks — quiet
    modes (FOCUS/PRESENTATION/PASSIVE) and CPU/RAM/battery pressure all skip
    the sweep (rescheduled for the next interval, not cancelled outright).
    None (no state wired) fails open — runs as before.
    """
    logger.info(
        f"HUNT_SCHEDULER: active — "
        f"sweeping every {_HUNT_INTERVAL_H}h, "
        f"first run in 30min"
    )
    await asyncio.sleep(_WARMUP_S)

    while True:
        # V69 M54.12 — consult the authoritative lifecycle before EACH iteration.
        # No new sweep is scheduled once STOPPING.
        if _lifecycle_stopping():
            logger.info("HUNT_SCHEDULER: stopping — no further sweeps")
            return
        try:
            if state is not None:
                from core.ironman_mode import should_run_background_tasks
                on_battery, cpu_pct, ram_pct = _current_resource_state()
                if not should_run_background_tasks(state.mode, on_battery, cpu_pct, ram_pct):
                    logger.debug(
                        f"HUNT_SCHEDULER: skipped — mode={state.mode.value} "
                        f"cpu={cpu_pct:.0f}% ram={ram_pct:.0f}% on_battery={on_battery}"
                    )
                    await asyncio.sleep(_HUNT_INTERVAL_S)
                    continue

            from core.cancel_bus import get_active_operations
            # Don't hunt if ARES is actively running
            ops = get_active_operations()
            if "llm_stream" not in ops and "agentic_loop" not in ops:
                await run_all_hunts(broadcast_fn, ollama_client, model)
            else:
                logger.debug("HUNT_SCHEDULER: skipped — operations active")
        except Exception as e:
            logger.debug(f"HUNT_SCHEDULER: {e}")

        await asyncio.sleep(_HUNT_INTERVAL_S)
