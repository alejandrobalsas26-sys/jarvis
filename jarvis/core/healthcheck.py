"""core/healthcheck.py — Runtime startup diagnostic for all JARVIS subsystems (v24.0)."""

import asyncio
import importlib

_SUBSYSTEMS: list[tuple[str, str]] = [
    # (display_name, module_path)
    ("config",                 "core.config"),
    ("events",                 "core.events"),
    ("llm",                    "core.llm"),
    ("tts",                    "core.tts"),
    ("canary",                 "core.canary"),
    ("mitigation",             "core.mitigation"),
    ("executor",               "tools.executor"),
    ("mesh_generator",         "tools.mesh_generator"),
    ("schematic_compiler",     "tools.schematic_compiler"),
    ("rf_bridge",              "tools.rf_bridge"),
    ("ad_graph_analyzer",      "tools.ad_graph_analyzer"),
    ("forensic_volatility",    "tools.forensic_volatility"),
    ("etw_monitor",            "tools.etw_monitor"),
    ("binary_inverter",        "tools.binary_inverter"),
    ("deception_orchestrator", "tools.deception_orchestrator"),
    ("environmental_intel",    "tools.environmental_intel"),
    ("threat_feed_sync",       "tools.threat_feed_sync"),
    ("resource_sentinel",      "tools.resource_sentinel"),
    ("offensive_rpc",          "tools.offensive_rpc"),
    ("zeek_dpi",               "tools.zeek_dpi"),
]


def _run_all_checks() -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for name, module_path in _SUBSYSTEMS:
        try:
            importlib.import_module(module_path)
            results.append((name, "OK", ""))
        except ImportError as exc:
            results.append((name, "MISSING_DEP", str(exc)))
        except Exception as exc:
            results.append((name, "BROKEN", str(exc)))
    return results


async def run_startup_diagnostic() -> dict:
    """Import-test all subsystems in a worker thread; classify OK / MISSING_DEP / BROKEN."""
    loop    = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _run_all_checks)

    subsystems = {
        name: {"status": status, "detail": detail}
        for name, status, detail in results
    }
    summary = {
        "ok":      sum(1 for _, s, _ in results if s == "OK"),
        "missing": sum(1 for _, s, _ in results if s == "MISSING_DEP"),
        "broken":  sum(1 for _, s, _ in results if s == "BROKEN"),
    }
    return {"subsystems": subsystems, "summary": summary}
