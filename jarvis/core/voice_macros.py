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


async def execute_macro(
    macro: dict,
    broadcast_fn,
    tts,
) -> bool:
    """
    Execute a matched macro.
    Returns True if macro was executed, False if cancelled.
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
        await execute_macro(macro, broadcast_fn, tts)
        return True
    return False
