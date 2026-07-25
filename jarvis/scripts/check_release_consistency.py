"""scripts/check_release_consistency.py — V69 M61.2: the CI truth gate.

One command that fails the build when the repository stops agreeing with itself:

  * ``core.release_check.audit()``      — version derivation, model-role claims,
                                          autonomy claims, release-document merge
                                          status, README install commands;
  * ``core.dependency_authority.audit()`` — pyproject extras vs requirements
                                          profiles, duplicates, base purity, the
                                          deprecated legacy monolith.

Read-only. It opens files, it writes nothing, it runs nothing it reads, and it needs
no network, no Ollama, no service and no privilege — so it is safe as a mandatory
gate on every matrix combination.

Exit codes: ``0`` consistent, ``1`` problems found (each printed), ``2`` the checker
itself could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="M61 release + dependency consistency")
    ap.add_argument("--json", action="store_true", help="emit the machine-readable result")
    args = ap.parse_args()

    try:
        from core.dependency_authority import audit as dependency_audit
        from core.release_check import audit as release_audit
        from core.version import VERSION, MILESTONE_TAG
    except Exception as exc:  # noqa: BLE001 — a broken import IS the failure here
        print(f"FAIL: consistency checker could not load: {type(exc).__name__}: {exc}")
        return 2

    release = release_audit()
    deps = dependency_audit()
    problems = list(release["problems"]) + list(deps["problems"])

    result = {
        "version": VERSION,
        "milestone": MILESTONE_TAG,
        "release": {"ok": release["ok"], "families": release["families"]},
        "dependencies": {"ok": deps["ok"], "profiles": deps["profiles"]},
        "problems": problems,
        "verdict": "PASS" if not problems else "FAIL",
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"JARVIS {MILESTONE_TAG} ({VERSION}) - release consistency")
        print(f"[{'PASS' if release['ok'] else 'FAIL'}] release truth      "
              f"{release['families']}")
        print(f"[{'PASS' if deps['ok'] else 'FAIL'}] dependency authority "
              f"{deps['profiles']}")
        for problem in problems:
            print(f"  - {problem}")
        print(f"VERDICT: {result['verdict']}")

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
