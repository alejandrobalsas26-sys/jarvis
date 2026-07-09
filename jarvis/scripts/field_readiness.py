#!/usr/bin/env python3
"""scripts/field_readiness.py -- V67 M36: JARVIS field readiness doctor.

A one-shot, read-only "can I deploy this right now?" report built from REAL checks
(core runtime, Ollama reachability, resolved models, collectors, assets, sensors, AURA,
persistence, Docker/VMware, authorized scope, runbook posture). No fabricated readiness.

Usage (run from the jarvis/ directory):
    python scripts/field_readiness.py               # readiness table (probes Ollama)
    python scripts/field_readiness.py --no-ollama   # skip the network probe
    python scripts/field_readiness.py --collectors  # collector + runtime health detail
    python scripts/field_readiness.py --json        # machine-readable

Exit code is non-zero when a CRITICAL readiness line has failed (CI/field-safe).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_JARVIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JARVIS_DIR))

from core.field_readiness import assess_field_readiness  # noqa: E402


def _print_collectors() -> None:
    from core.runtime_health import build_live_runtime_health
    health = build_live_runtime_health()
    print("JARVIS RUNTIME HEALTH")
    print("=====================")
    print(f"OVERALL: {health['overall'].upper()}")
    for sub in health["subsystems"]:
        print(f"  {sub['name'].ljust(16)}{sub['status'].upper():<12} {sub['detail']}")
    try:
        from core.collector_fabric import fabric
        panel = fabric.aura_panel()
        print("\nCOLLECTORS")
        print("----------")
        for row in panel.get("collectors", []):
            signed = "signed" if row.get("signed") else "unsigned"
            print(f"  {str(row.get('id', '')).ljust(20)}{str(row.get('status', '')).upper():<12}"
                  f" events={row.get('events', 0):<6} {signed}")
        if not panel.get("collectors"):
            print("  (no collectors registered)")
    except Exception as e:  # noqa: BLE001
        print(f"  collector fabric unavailable: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS field readiness doctor.")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--no-ollama", action="store_true",
                    help="skip the Ollama reachability probe")
    ap.add_argument("--collectors", action="store_true",
                    help="show collector + runtime health detail instead of the table")
    args = ap.parse_args(argv)

    if args.collectors:
        _print_collectors()
        return 0

    report = assess_field_readiness(probe_ollama=not args.no_ollama)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
