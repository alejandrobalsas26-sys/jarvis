#!/usr/bin/env python3
"""scripts/scenario_runner.py -- V67 M30: replay end-to-end operational scenarios.

Drives deterministic fixtures through the REAL JARVIS spine (correlator -> incident
workspace -> digital twin -> situation engine -> runbook dry-run) and prints the full
detection-to-response chain. No world effect is possible: the runbook engine runs
without a ToolExecutor, so every scenario can only produce a dry-run plan; HIGH-impact
steps (e.g. an active scan) are shown as HITL-gated, never executed.

Usage (run from the jarvis/ directory):
    python scripts/scenario_runner.py --list
    python scripts/scenario_runner.py auth_sequence
    python scripts/scenario_runner.py new_service_exposure --dry-run
    python scripts/scenario_runner.py sensor_loss --json
    python scripts/scenario_runner.py all

Exit code is non-zero if any run's expectations fail (CI-friendly).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_JARVIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_JARVIS_DIR))

from core.scenario_harness import SCENARIOS, ScenarioHarness, ScenarioOutcome  # noqa: E402

_OK, _NO = "[ok]", "[!!]"   # ASCII-only: the target console is Windows cp1252


def _print_chain(out: ScenarioOutcome) -> None:
    s = SCENARIOS[out.scenario_id]
    print(f"\n=== SCENARIO {out.scenario_id} - {s.name} ===")
    print(f"  {s.description}")

    print(f"\n  [1] events -> correlation findings: {len(out.findings)}")
    for f in out.findings:
        d = f.to_dict()
        print(f"      - {d['rule']}  ({d['severity']}, conf {d['confidence']}) "
              f"on {d['group_entity']}  [{len(d['matched_event_ids'])} events]")
        print(f"        {d['explanation']['reason']}")

    print(f"\n  [2] incident cases: {len(out.incidents)}")
    for c in out.incidents:
        cd = c.to_dict()
        print(f"      - {cd['incident_id']}  {cd['status']}/{cd['severity']}  "
              f"\"{cd['title']}\"  assets={cd.get('affected_assets', [])}")

    print(f"\n  [3] digital-twin drift: {out.drift.to_dict()['drift_count']}")
    for df in out.drift.findings:
        dd = df.to_dict()
        print(f"      - {dd['drift_type']} ({dd['severity']}) on {dd['asset']} "
              f"-> recommend {dd['recommended_investigation']}"
              f"{'  [verification required]' if dd['verification_required'] else ''}")

    sit = out.situation
    print(f"\n  [4] situation: {sit.severity.value.upper()}")
    if sit.summary.top_priority:
        tp = sit.summary.top_priority
        print(f"      top priority: {tp['title']} ({tp['severity']}, conf {tp['confidence']})")
    for rec in sit.recommendations:
        print(f"      recommendation: {rec.runbook} ({rec.mode}) - {rec.rationale}")
    if sit.uncertainties:
        print(f"      uncertain: {list(sit.uncertainties)}")

    if out.plan is not None:
        pj = out.plan.to_dict()
        print(f"\n  [5] runbook dry-run: {pj['runbook']}  status={pj['status']}")
        plan = pj.get("plan") or {}
        for step in plan.get("steps", []):
            gate = "HITL" if step["requires_hitl"] else "auto"
            print(f"      - {step['id']}: {step['action'] or '(reason)'} "
                  f"[{step['risk_class']}, {gate}]  {step['description']}")
        if plan.get("requires_hitl_steps"):
            print(f"      HITL-gated steps (never auto-run): {plan['requires_hitl_steps']}")

    if out.verification is not None:
        v = out.verification
        print(f"\n  [6] re-observation -> verification: verified={v['verified']} "
              f"(drift {v['drift_before']} -> {v['drift_after']}, cleared {v['cleared_findings']})")

    print(f"\n  [7] AURA events emitted: {[e.get('type') for e in out.aura_events()]}")

    print("\n  checks:")
    for c in out.checks:
        print(f"      {_OK if c.passed else _NO} {c.name}: {c.detail}")
    print(f"\n  RESULT: {'PASS' if out.passed else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay JARVIS operational scenarios.")
    ap.add_argument("scenario", nargs="?", default=None,
                    help="scenario id, or 'all' (omit with --list to enumerate)")
    ap.add_argument("--list", action="store_true", help="list available scenarios")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op flag: scenarios are ALWAYS dry-run (no world effect)")
    args = ap.parse_args(argv)

    if args.list or args.scenario is None:
        print("Available scenarios:")
        for sid, s in SCENARIOS.items():
            print(f"  {sid:24s} {s.description}")
        return 0

    harness = ScenarioHarness()
    targets = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    if any(t not in SCENARIOS for t in targets):
        print(f"unknown scenario {args.scenario!r}; available: {sorted(SCENARIOS)}",
              file=sys.stderr)
        return 2

    outcomes = [harness.run(SCENARIOS[t]) for t in targets]
    if args.json:
        print(json.dumps({o.scenario_id: o.to_dict() for o in outcomes}, indent=2))
    else:
        for o in outcomes:
            _print_chain(o)
        passed = sum(o.passed for o in outcomes)
        print(f"\n{passed}/{len(outcomes)} scenario(s) passed.")
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
