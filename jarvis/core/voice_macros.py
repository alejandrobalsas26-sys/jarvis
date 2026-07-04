"""
core/voice_macros.py — YAML-defined voice command macro system (v35.0).

Matches transcribed speech against macro triggers using fuzzy substring
matching (no exact match required — handles ASR errors).
Hot-reloads macros.yaml on file change (mtime check).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_MACROS_PATH    = Path(__file__).parent / "macros.yaml"
_macros:        list[dict] = []
_loaded_mtime:  float = 0.0


def _load_macros() -> None:
    """Re-load macros if file has been modified since last load."""
    global _macros, _loaded_mtime
    try:
        import yaml
    except ImportError:
        logger.warning("VOICE_MACROS: PyYAML not installed — macros disabled")
        return
    try:
        mtime = _MACROS_PATH.stat().st_mtime
        if mtime == _loaded_mtime:
            return   # unchanged
        data = yaml.safe_load(_MACROS_PATH.read_text(encoding="utf-8"))
        _macros = data.get("macros", []) if isinstance(data, dict) else []
        _loaded_mtime = mtime
        logger.info(f"VOICE_MACROS: loaded {len(_macros)} macros")
    except Exception as e:
        logger.debug(f"VOICE_MACROS: load failed: {e}")


_load_macros()


def match_macro(text: str) -> dict | None:
    """
    Find a matching macro for the transcribed text.
    Uses fuzzy word-overlap matching to handle ASR word errors.
    Requires >= 80% of the trigger's tokens to appear in the input.
    Returns the macro dict, or None.
    """
    _load_macros()   # check for hot-reload
    text_lower = text.lower().strip()
    if not text_lower:
        return None
    text_words = set(text_lower.split())

    best_macro:   dict | None = None
    best_overlap: int = 0

    for macro in _macros:
        trigger = str(macro.get("trigger", "")).lower()
        if not trigger:
            continue
        trigger_words = set(trigger.split())
        if not trigger_words:
            continue
        overlap   = len(trigger_words & text_words)
        threshold = max(1, int(len(trigger_words) * 0.8))
        if overlap >= threshold and overlap > best_overlap:
            best_overlap = overlap
            best_macro   = macro

    return best_macro


_SCREEN_CONSENT_DENIED_MSG = "Screen access isn't enabled for this session. Say 'enable screen access' to allow it."
_CAMERA_CONSENT_DENIED_MSG = "Camera access isn't enabled for this session. Say 'enable camera access' to allow it."


def _has_consent(consent, surface: str) -> bool:
    return bool(consent is not None and getattr(consent, surface, False))


async def execute_macro(
    macro: dict,
    broadcast_fn,
    tts,
    consent=None,
) -> bool:
    """
    Execute a matched macro.
    Returns True if macro was executed, False if cancelled.

    ``consent`` (V62.0 Phase 6, core.ironman_mode.SessionConsent) gates the
    screen/camera-capturing actions below (analyze_screen, take_screenshot,
    ocr_analyze_screen, vision_room, vision_screen) — None/unset means no
    consent, fail-closed.
    """
    # Speak response acknowledgment
    response = macro.get("response", "Executing command.")
    if tts:
        try:
            asyncio.create_task(tts.speak_async(response))
        except Exception:
            pass

    # Confirmation gate (HUD-only signal — actual gate handled upstream)
    if macro.get("confirm", False):
        try:
            await broadcast_fn({
                "type":      "macro_confirm_required",
                "macro":     macro.get("trigger", ""),
                "action":    macro.get("action", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        logger.info(f"MACRO: awaiting confirmation for '{macro.get('action')}'")
        await asyncio.sleep(0.5)   # brief pause for TTS

    # Dispatch action
    action = macro.get("action", "")
    params = macro.get("params", {}) or {}

    try:
        if action == "emulate_chain":
            from core.adversary_emulator import adversary_emulator
            asyncio.create_task(
                adversary_emulator.emulate_chain(params.get("chain", ""))
            )

        elif action == "get_coverage":
            from core.attck_coverage import broadcast_coverage
            asyncio.create_task(broadcast_coverage(broadcast_fn))

        elif action == "export_stix":
            try:
                from core.correlator import correlator
                from tools.ioc_extractor import export_incident_stix
                incidents = correlator.get_active_incidents()
                if incidents:
                    asyncio.create_task(
                        export_incident_stix(incidents[0], broadcast_fn)
                    )
            except ImportError as e:
                logger.debug(f"MACRO: STIX export deps missing: {e}")

        elif action == "run_integrity_check":
            from core.integrity_baseline import run_integrity_check
            asyncio.create_task(run_integrity_check(broadcast_fn))

        elif action == "apply_hardening":
            from core.windows_hardener import apply_host_hardening
            asyncio.create_task(apply_host_hardening(broadcast_fn))

        elif action == "list_playbooks":
            try:
                from core.playbook_engine import playbook_engine
                pbs = getattr(playbook_engine, "_playbooks", []) or []
                names = [getattr(pb, "name", str(pb)) for pb in pbs]
                if tts and names:
                    asyncio.create_task(
                        tts.speak_async(f"Available playbooks: {', '.join(names)}")
                    )
            except ImportError:
                pass

        # ── v36.0 — Predictive Cognition macros ─────────────────────────────
        elif action == "swap_deep":
            try:
                from core.model_swapper import swap_to_deep
                asyncio.create_task(swap_to_deep(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: swap_deep error: {e}")

        elif action == "swap_fast":
            try:
                from core.model_swapper import swap_to_fast
                asyncio.create_task(swap_to_fast(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: swap_fast error: {e}")

        elif action == "swap_toggle":
            try:
                from core.model_swapper import toggle
                asyncio.create_task(toggle(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: swap_toggle error: {e}")

        elif action == "generate_report":
            try:
                from core.correlator        import correlator
                from core.incident_reporter import generate_incident_report
                from core.agent_orchestrator import orchestrator
                incidents = correlator.get_active_incidents()
                if incidents:
                    asyncio.create_task(generate_incident_report(
                        incidents[0], [], broadcast_fn,
                        orchestrator._ollama_client,
                        orchestrator._deep_model,
                    ))
            except Exception as e:
                logger.debug(f"MACRO: generate_report error: {e}")

        elif action == "consolidate_memory":
            try:
                from core.memory_consolidator import consolidate_memory
                from core.agent_orchestrator  import orchestrator
                asyncio.create_task(consolidate_memory(
                    broadcast_fn,
                    orchestrator._ollama_client,
                    orchestrator._deep_model,
                ))
            except Exception as e:
                logger.debug(f"MACRO: consolidate_memory error: {e}")

        elif action == "multi_agent_analyze":
            try:
                from core.agent_orchestrator import orchestrator
                from core.correlator        import correlator
                incidents = correlator.get_active_incidents()
                ctx       = incidents[0] if incidents else {}
                asyncio.create_task(orchestrator.run_task(
                    "Analyze current active incident",
                    ["ThreatIntelligence", "IncidentResponder"],
                    ctx,
                ))
            except Exception as e:
                logger.debug(f"MACRO: multi_agent_analyze error: {e}")

        # ── v37.0 — Autonomous Intelligence dispatch ────────────────────────
        elif action == "github_search":
            try:
                query = params.get("query", "") or macro.get("query", "")
                if not query:
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            "What tool are you looking for?"
                        ))
                else:
                    from core.github_explorer  import autodiscover_and_integrate
                    from core.agent_orchestrator import orchestrator
                    asyncio.create_task(autodiscover_and_integrate(
                        query, broadcast_fn,
                        orchestrator._ollama_client,
                        orchestrator._fast_model,
                    ))
            except Exception as e:
                logger.debug(f"MACRO: github_search error: {e}")

        elif action == "cve_briefing":
            try:
                from core.cve_intel import poll_nvd
                asyncio.create_task(poll_nvd(broadcast_fn, tts))
            except Exception as e:
                logger.debug(f"MACRO: cve_briefing error: {e}")

        elif action == "analyze_inbox":
            try:
                from pathlib import Path
                inbox = Path("analyze_inbox").absolute()
                if tts:
                    asyncio.create_task(tts.speak_async(
                        f"Drop files in {inbox} to analyze."
                    ))
            except Exception as e:
                logger.debug(f"MACRO: analyze_inbox error: {e}")

        elif action == "osint_enrich_recent":
            try:
                if tts:
                    asyncio.create_task(tts.speak_async(
                        "OSINT enrichment is automatic on observed IPs."
                    ))
            except Exception as e:
                logger.debug(f"MACRO: osint_enrich_recent error: {e}")

        elif action == "lab_isolate":
            try:
                from core.lab_manager import isolate_vm
                vm = params.get("vm", "victim")
                asyncio.create_task(isolate_vm(vm, broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: lab_isolate error: {e}")

        elif action == "lab_list":
            try:
                from core.lab_manager import list_vms
                asyncio.create_task(list_vms(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: lab_list error: {e}")

        elif action == "list_github_tools":
            try:
                from core.github_explorer import list_integrated_tools
                tools = list_integrated_tools()
                if tts:
                    if tools:
                        names = ", ".join(t["name"] for t in tools[:5])
                        asyncio.create_task(tts.speak_async(
                            f"{len(tools)} tools integrated: {names}"
                        ))
                    else:
                        asyncio.create_task(tts.speak_async(
                            "No GitHub tools integrated yet."
                        ))
            except Exception as e:
                logger.debug(f"MACRO: list_github_tools error: {e}")

        # ── v38.0 — Visual Intelligence dispatch ────────────────────────────
        elif action == "analyze_screen":
            if not _has_consent(consent, "screen"):
                if tts:
                    asyncio.create_task(tts.speak_async(_SCREEN_CONSENT_DENIED_MSG))
                return False
            try:
                from core.vision_engine import analyze_screen
                from core.agent_orchestrator import orchestrator
                asyncio.create_task(analyze_screen(
                    "Describe everything visible. Identify any security-relevant "
                    "elements, error messages, suspicious processes, or code.",
                    orchestrator._ollama_client, broadcast_fn, tts=tts,
                ))
            except Exception as e:
                logger.debug(f"MACRO: analyze_screen error: {e}")

        elif action == "browser_open":
            try:
                url = params.get("url", "")
                if url:
                    from tools.browser_intel import open_url_tactical
                    asyncio.create_task(open_url_tactical(url, broadcast_fn))
                else:
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            "What URL should I open?"
                        ))
            except Exception as e:
                logger.debug(f"MACRO: browser_open error: {e}")

        elif action == "research_cve_browser":
            try:
                cve = params.get("cve_id", "")
                if cve:
                    from tools.browser_intel    import research_cve
                    from core.agent_orchestrator import orchestrator
                    asyncio.create_task(research_cve(
                        cve, broadcast_fn, orchestrator._ollama_client,
                    ))
            except Exception as e:
                logger.debug(f"MACRO: research_cve_browser error: {e}")

        elif action == "generate_network_diagram":
            try:
                from tools.diagram_generator import generate_network_topology
                asyncio.create_task(
                    generate_network_topology([], broadcast_fn)
                )
            except Exception as e:
                logger.debug(f"MACRO: generate_network_diagram error: {e}")

        elif action == "generate_timeline":
            try:
                from core.correlator         import correlator
                from tools.diagram_generator import generate_attack_timeline
                incidents = correlator.get_active_incidents()
                if incidents:
                    asyncio.create_task(
                        generate_attack_timeline(incidents[0], broadcast_fn)
                    )
            except Exception as e:
                logger.debug(f"MACRO: generate_timeline error: {e}")

        elif action == "generate_qr":
            try:
                url   = params.get("url",   "https://jarvis.local/test")
                label = params.get("label", "test_payload")
                from tools.diagram_generator import generate_qr_code
                asyncio.create_task(
                    generate_qr_code(url, label, broadcast_fn)
                )
            except Exception as e:
                logger.debug(f"MACRO: generate_qr error: {e}")

        elif action == "take_screenshot":
            if not _has_consent(consent, "screen"):
                if tts:
                    asyncio.create_task(tts.speak_async(_SCREEN_CONSENT_DENIED_MSG))
                return False
            try:
                from core.vision_engine import capture_and_save
                asyncio.create_task(capture_and_save("operator_request"))
            except Exception as e:
                logger.debug(f"MACRO: take_screenshot error: {e}")

        # ── v40.0 — Omni-Vision, Ghost Hands & Forensic Reporter dispatch ──
        elif action == "ocr_analyze_screen":
            if not _has_consent(consent, "screen"):
                if tts:
                    asyncio.create_task(tts.speak_async(_SCREEN_CONSENT_DENIED_MSG))
                return False
            try:
                from core.ocr_engine import read_screen_and_analyze
                from core.agent_orchestrator import orchestrator
                asyncio.create_task(read_screen_and_analyze(
                    params.get("context", "Analyze security-relevant content on screen"),
                    broadcast_fn, orchestrator._ollama_client,
                    orchestrator._fast_model, tts=tts
                ))
            except Exception as e:
                logger.debug(f"MACRO: ocr_analyze_screen error: {e}")

        elif action == "ghost_hands_profile":
            try:
                from tools.ghost_hands import execute_lab_profile
                profile = params.get("profile", "")
                if profile:
                    asyncio.create_task(execute_lab_profile(profile, broadcast_fn, tts))
            except Exception as e:
                logger.debug(f"MACRO: ghost_hands_profile error: {e}")

        elif action == "list_lab_profiles":
            try:
                from tools.ghost_hands import list_profiles
                profiles = list_profiles()
                if tts:
                    asyncio.create_task(tts.speak_async(
                        f"Available profiles: {', '.join(profiles)}"
                    ))
            except Exception as e:
                logger.debug(f"MACRO: list_lab_profiles error: {e}")

        elif action == "generate_docx_report":
            try:
                from core.correlator import correlator
                from core.forensic_reporter import generate_forensic_report
                from core.agent_orchestrator import orchestrator
                incidents = correlator.get_active_incidents()
                if incidents:
                    asyncio.create_task(generate_forensic_report(
                        incidents[0], [], broadcast_fn,
                        orchestrator._ollama_client,
                        orchestrator._deep_model,
                    ))
                else:
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            "No active incidents to report."
                        ))
            except Exception as e:
                logger.debug(f"MACRO: generate_docx_report error: {e}")

        # ── v41.0 — Ephemeral Docker Lab Orchestrator dispatch ─────────────
        elif action == "docker_deploy":
            try:
                from tools.docker_manager import deploy_lab
                lab = params.get("lab", "")
                if lab:
                    asyncio.create_task(deploy_lab(lab, broadcast_fn, tts))
            except Exception as e:
                logger.debug(f"MACRO: docker_deploy error: {e}")

        elif action == "docker_teardown_all":
            try:
                from tools.docker_manager import teardown_all_labs
                asyncio.create_task(teardown_all_labs(broadcast_fn, tts))
            except Exception as e:
                logger.debug(f"MACRO: docker_teardown_all error: {e}")

        elif action == "docker_teardown_named":
            try:
                from tools.docker_manager import teardown_lab
                lab = params.get("lab", "")
                if lab:
                    asyncio.create_task(teardown_lab(lab, broadcast_fn, tts))
            except Exception as e:
                logger.debug(f"MACRO: docker_teardown_named error: {e}")

        elif action == "docker_list_labs":
            try:
                from tools.docker_manager import list_running_labs
                asyncio.create_task(list_running_labs(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: docker_list_labs error: {e}")

        # ── v42.0 — ARES PROTOCOL dispatch ─────────────────────────────────
        elif action == "ares_start_campaign":
            try:
                from core.red_team_operator import ares_operator
                target_ip = params.get("target", params.get("ip", ""))
                if not target_ip:
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            "What is the target IP address?"
                        ))
                else:
                    asyncio.create_task(
                        ares_operator.start_campaign(
                            target_ip, params.get("name", "")
                        )
                    )
            except Exception as e:
                logger.debug(f"MACRO: ares_start_campaign error: {e}")

        elif action == "ares_abort_campaign":
            try:
                from core.red_team_operator import ares_operator
                campaigns = ares_operator.get_active_campaigns()
                for c in campaigns:
                    asyncio.create_task(
                        ares_operator.abort_campaign(c["campaign_id"])
                    )
            except Exception as e:
                logger.debug(f"MACRO: ares_abort_campaign error: {e}")

        elif action == "ares_campaign_status":
            try:
                from core.red_team_operator import ares_operator
                campaigns = ares_operator.get_active_campaigns()
                if tts:
                    if campaigns:
                        c = campaigns[0]
                        asyncio.create_task(tts.speak_async(
                            f"Campaign {c['campaign_id']} targeting "
                            f"{c['target_ip']} — stage {c['stage']}"
                        ))
                    else:
                        asyncio.create_task(tts.speak_async(
                            "No active campaigns."
                        ))
            except Exception as e:
                logger.debug(f"MACRO: ares_campaign_status error: {e}")

        elif action == "sensor_deploy":
            try:
                import os as _os
                from core.sensor_mesh import deploy_sensor_to_vm
                host = params.get("ip", params.get("host", ""))
                key  = _os.getenv("KALI_KEY_PATH", "")
                user = _os.getenv("KALI_USER", "kali")
                if host and key:
                    asyncio.create_task(
                        deploy_sensor_to_vm(
                            host, user, key, broadcast_fn, tts
                        )
                    )
                else:
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            "Target IP and KALI_KEY_PATH required."
                        ))
            except Exception as e:
                logger.debug(f"MACRO: sensor_deploy error: {e}")

        elif action == "sensor_status":
            try:
                from core.sensor_mesh import get_connected_agents
                agents = get_connected_agents()
                if tts:
                    msg = (
                        f"{len(agents)} sensor agents connected: "
                        f"{', '.join(a.get('hostname','?') for a in agents[:3])}"
                        if agents else "No sensor agents connected."
                    )
                    asyncio.create_task(tts.speak_async(msg))
            except Exception as e:
                logger.debug(f"MACRO: sensor_status error: {e}")

        elif action == "proxy_start":
            try:
                from core.proxy_intel import start_proxy_intel
                asyncio.create_task(start_proxy_intel(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: proxy_start error: {e}")

        # ── v43.0 — BIFROST PROTOCOL dispatch ──────────────────────────────
        elif action == "bas_apt_chain":
            try:
                from tools.breach_simulator import run_full_bas_scenario
                target = params.get("target", "192.168.1.100")
                asyncio.create_task(run_full_bas_scenario(
                    target, broadcast_fn, "apt_chain",
                ))
            except Exception as e:
                logger.debug(f"MACRO: bas_apt_chain error: {e}")

        elif action == "bas_cs_beacon":
            try:
                from tools.breach_simulator import simulate_cs_beacon
                target = params.get("target", "192.168.1.100")
                asyncio.create_task(simulate_cs_beacon(target, broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: bas_cs_beacon error: {e}")

        elif action == "bas_smb_lateral":
            try:
                from tools.breach_simulator import simulate_smb_lateral
                target = params.get("target", "192.168.1.100")
                asyncio.create_task(simulate_smb_lateral(target, broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: bas_smb_lateral error: {e}")

        elif action == "bas_dns_c2":
            try:
                from tools.breach_simulator import simulate_dns_c2
                target = params.get("target", "192.168.1.100")
                asyncio.create_task(simulate_dns_c2(target, broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: bas_dns_c2 error: {e}")

        elif action == "bas_data_exfil":
            try:
                from tools.breach_simulator import simulate_data_exfil
                target = params.get("target", "192.168.1.100")
                asyncio.create_task(simulate_data_exfil(target, broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: bas_data_exfil error: {e}")

        elif action == "bas_process_inject":
            try:
                from tools.breach_simulator import simulate_process_injection_artifact
                asyncio.create_task(
                    simulate_process_injection_artifact(broadcast_fn)
                )
            except Exception as e:
                logger.debug(f"MACRO: bas_process_inject error: {e}")

        elif action == "purple_coverage_report" or action == "purple_gap_analysis":
            try:
                from core.purple_coordinator import (
                    get_coverage_matrix, get_coverage_summary,
                )
                matrix  = get_coverage_matrix()
                summary = get_coverage_summary()
                asyncio.create_task(broadcast_fn({
                    "type":      "purple_coverage_report",
                    "matrix":    matrix[:20],
                    "summary":   summary,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
                if tts and summary:
                    asyncio.create_task(tts.speak_async(
                        f"Coverage: {summary.get('coverage_pct', 0)}% of "
                        f"{summary.get('total', 0)} techniques. "
                        f"{summary.get('gaps', 0)} gaps detected. "
                        f"Mean detection time: "
                        f"{summary.get('mttd_ms', 0):.0f} milliseconds."
                    ))
            except Exception as e:
                logger.debug(f"MACRO: purple_coverage_report error: {e}")

        elif action == "detection_engineer_gaps":
            try:
                from core.purple_coordinator import get_coverage_matrix
                from core.detection_engineer import draft_rule_for_gap
                from core.agent_orchestrator import orchestrator
                gaps = [r for r in get_coverage_matrix() if r["tier"] == "GAP"]
                for gap in gaps[:3]:
                    asyncio.create_task(draft_rule_for_gap(
                        gap["technique"], broadcast_fn,
                        orchestrator._ollama_client,
                        orchestrator._deep_model,
                    ))
                if tts:
                    asyncio.create_task(tts.speak_async(
                        f"Drafting detection rules for {len(gaps)} coverage gaps."
                    ))
            except Exception as e:
                logger.debug(f"MACRO: detection_engineer_gaps error: {e}")

        # ── v44.0 — Quality of Life dispatch ───────────────────────────────
        elif action == "add_alias":
            try:
                from core.target_aliases import add_alias, broadcast_aliases
                name   = params.get("name", "")
                target = params.get("target", "")
                if name and target:
                    add_alias(name, target)
                    asyncio.create_task(broadcast_aliases(broadcast_fn))
                    if tts:
                        asyncio.create_task(tts.speak_async(
                            f"Alias saved. {name} is now {target}."
                        ))
            except Exception as e:
                logger.debug(f"MACRO: add_alias error: {e}")

        elif action == "remove_alias":
            try:
                from core.target_aliases import remove_alias, broadcast_aliases
                name = params.get("name", "")
                if name and remove_alias(name):
                    asyncio.create_task(broadcast_aliases(broadcast_fn))
                    if tts:
                        asyncio.create_task(tts.speak_async(f"Alias {name} removed."))
            except Exception as e:
                logger.debug(f"MACRO: remove_alias error: {e}")

        elif action == "show_aliases":
            try:
                from core.target_aliases import list_aliases, broadcast_aliases
                aliases = list_aliases()
                asyncio.create_task(broadcast_aliases(broadcast_fn))
                if tts:
                    if aliases:
                        lines = ", ".join(
                            f"{n} is {v}" for n, v in list(aliases.items())[:5]
                        )
                        asyncio.create_task(tts.speak_async(
                            f"Known targets: {lines}"
                        ))
                    else:
                        asyncio.create_task(tts.speak_async("No aliases saved yet."))
            except Exception as e:
                logger.debug(f"MACRO: show_aliases error: {e}")

        elif action == "daily_briefing":
            try:
                from core.daily_briefing import deliver_briefing
                asyncio.create_task(
                    deliver_briefing(broadcast_fn, tts)
                )
            except Exception as e:
                logger.debug(f"MACRO: daily_briefing error: {e}")

        elif action == "save_journal":
            try:
                from core.session_journal import write_journal
                asyncio.create_task(write_journal())
                if tts:
                    asyncio.create_task(tts.speak_async("Session journal saved."))
            except Exception as e:
                logger.debug(f"MACRO: save_journal error: {e}")

        # ── v45.0 — PROMETHEUS dispatch ────────────────────────────────────
        elif action == "run_hunt_sweep":
            try:
                from core.hunt_scheduler import run_all_hunts
                from core.agent_orchestrator import orchestrator
                asyncio.create_task(run_all_hunts(
                    broadcast_fn,
                    orchestrator._ollama_client,
                    orchestrator._deep_model,
                ))
            except Exception as e:
                logger.debug(f"MACRO: run_hunt_sweep error: {e}")

        elif action == "weekly_digest":
            try:
                from core.intel_fusion import generate_weekly_digest
                from core.agent_orchestrator import orchestrator
                asyncio.create_task(generate_weekly_digest(
                    broadcast_fn,
                    orchestrator._ollama_client,
                    orchestrator._deep_model,
                ))
            except Exception as e:
                logger.debug(f"MACRO: weekly_digest error: {e}")

        elif action == "war_room_toggle":
            try:
                await broadcast_fn({"type": "war_room_toggle_requested"})
            except Exception as e:
                logger.debug(f"MACRO: war_room_toggle error: {e}")

        elif action == "telegram_test":
            try:
                from core.telegram_bridge import push_alert
                asyncio.create_task(push_alert(
                    "JARVIS TEST",
                    "Connection confirmed. PROMETHEUS online.",
                    "INFO",
                ))
            except Exception as e:
                logger.debug(f"MACRO: telegram_test error: {e}")

        # ── v46.0 — GENESIS dispatch ───────────────────────────────────────
        elif action == "run_self_test":
            try:
                from core.self_test import run_self_test
                asyncio.create_task(run_self_test(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: run_self_test error: {e}")

        elif action == "performance_report":
            try:
                from core.performance_profiler import broadcast_stats
                asyncio.create_task(broadcast_stats(broadcast_fn))
            except Exception as e:
                logger.debug(f"MACRO: performance_report error: {e}")

        elif action == "reload_config":
            try:
                from core.config_manager import reload
                new_config = reload()
                if tts:
                    asyncio.create_task(tts.speak_async(
                        f"Configuration reloaded. {len(new_config)} sections active."
                    ))
            except Exception as e:
                logger.debug(f"MACRO: reload_config error: {e}")

        # ── v46.0 OMEGA dispatch ───────────────────────────────────────────
        elif action == "war_room_debate":
            try:
                from core.war_room_debate import run_war_room_debate
                from core.agent_orchestrator import orchestrator
                from core.correlator import correlator as _corr
                active = _corr.get_active_incidents()
                incident = active[0] if active else {
                    "kill_chain_phase": "threat analysis requested",
                    "severity_score":   7.0,
                    "involved_hosts":   set(),
                    "mitre_techniques": ["T1059"],
                }
                asyncio.create_task(run_war_room_debate(
                    incident,
                    orchestrator._ollama_client,
                    orchestrator._deep_model,
                    tts,
                    broadcast_fn,
                ))
            except Exception as e:
                logger.debug(f"MACRO: war_room_debate error: {e}")

        elif action == "punisher_enable":
            try:
                import core.punisher as _p
                _p._PUNISHER_ENABLED = True
            except Exception as e:
                logger.debug(f"MACRO: punisher_enable error: {e}")

        elif action == "punisher_disable":
            try:
                import core.punisher as _p
                _p._PUNISHER_ENABLED = False
            except Exception as e:
                logger.debug(f"MACRO: punisher_disable error: {e}")

        elif action == "vision_room":
            if not _has_consent(consent, "camera"):
                if tts:
                    asyncio.create_task(tts.speak_async(_CAMERA_CONSENT_DENIED_MSG))
                return False
            try:
                from core.vision_engine import analyze_room
                from core.agent_orchestrator import orchestrator
                async def _vr():
                    desc = await analyze_room(orchestrator._ollama_client)
                    if tts:
                        await tts.speak_async(desc[:300])
                asyncio.create_task(_vr())
            except Exception as e:
                logger.debug(f"MACRO: vision_room error: {e}")

        elif action == "vision_screen":
            if not _has_consent(consent, "screen"):
                if tts:
                    asyncio.create_task(tts.speak_async(_SCREEN_CONSENT_DENIED_MSG))
                return False
            try:
                from core.vision_engine import analyze_screen_vision
                from core.agent_orchestrator import orchestrator
                async def _vs():
                    desc = await analyze_screen_vision(orchestrator._ollama_client)
                    if tts:
                        await tts.speak_async(desc[:300])
                asyncio.create_task(_vs())
            except Exception as e:
                logger.debug(f"MACRO: vision_screen error: {e}")

        elif action == "iot_clear":
            try:
                from core.iot_bridge import alert_clear
                asyncio.create_task(alert_clear())
            except Exception as e:
                logger.debug(f"MACRO: iot_clear error: {e}")

        try:
            await broadcast_fn({
                "type":      "macro_executed",
                "trigger":   macro.get("trigger", ""),
                "action":    action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        logger.info(f"MACRO: executed '{action}' from voice command")
        return True

    except Exception as e:
        logger.error(f"MACRO: execution failed: {e}")
        return False


async def process_for_macro(
    text: str,
    broadcast_fn,
    tts,
    consent=None,
) -> bool:
    """
    Check if text matches a macro and execute it.
    Returns True if a macro was matched (caller should skip LLM).
    """
    macro = match_macro(text)
    if macro:
        logger.info(
            f"MACRO: matched '{macro.get('trigger')}' → '{macro.get('action')}'"
        )
        await execute_macro(macro, broadcast_fn, tts, consent=consent)
        return True
    return False
