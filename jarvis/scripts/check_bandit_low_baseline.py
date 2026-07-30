"""scripts/check_bandit_low_baseline.py — V69 M61.8: the Low findings cannot grow.

The blocking release gate is ``bandit -r core tools -ll -q`` — Medium and High. Below
that threshold sit 488 Low-severity findings. M61.7 recorded them honestly instead of
suppressing them, which was the right call and also an incomplete one: a number nobody
enforces is a number that grows.

This is the enforcement, and it is deliberately a CEILING rather than a fixed point:

  * ``observed <= baseline``  -> PASS. A reduction is progress and must not fail a build.
  * ``observed >  baseline``  -> FAIL, naming the delta. Triage the new findings, or
    raise ``BANDIT_LOW_BASELINE`` in ``core/release_facts.py`` deliberately, in a diff a
    reviewer sees.

It is NOT a claim that 488 Low findings are harmless. It is a claim that the number is
bounded and reviewed. Bounded reduction is scheduled maintenance work, described in
``docs/DEPENDENCY_SECURITY_POLICY.md``.

Read-only: it runs the scanner into a temporary report, reads counts, and writes nothing
to the repository. Exit codes: ``0`` within baseline, ``1`` grown, ``2`` could not be
evaluated (and ``2`` when ``JARVIS_REQUIRE_BANDIT`` is set and the scanner is absent).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_APP_ROOT))

from core.release_facts import (  # noqa: E402
    BANDIT_LOW_BASELINE,
    BANDIT_MEDIUM,
    BANDIT_HIGH,
    bandit_low_within_baseline,
)

REQUIRE_ENV = "JARVIS_REQUIRE_BANDIT"
_TARGETS = ("core", "tools")
_MIN_LINES = 40_000


def _required() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _scan() -> dict | None:
    """Run the scanner into a temporary JSON report and return its metrics totals."""
    with tempfile.TemporaryDirectory(prefix="m61_low_") as work:
        report = Path(work) / "bandit.json"
        try:
            subprocess.run(
                [sys.executable, "-m", "bandit", "-r", *_TARGETS, "-q",
                 "-f", "json", "-o", str(report)],
                cwd=str(_APP_ROOT), capture_output=True, text=True, timeout=900,
                check=False)
        except Exception:  # noqa: BLE001 — a missing scanner is a normal outcome here
            return None
        if not report.is_file():
            return None
        try:
            return json.loads(report.read_text(encoding="utf-8"))["metrics"]["_totals"]
        except (OSError, ValueError, KeyError):
            return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M61.8 Bandit Low baseline enforcement")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    totals = _scan()
    if totals is None:
        message = ("bandit could not be run (dev profile: "
                   "pip install -r requirements/dev.txt)")
        if args.json:
            print(json.dumps({"verdict": "INSUFFICIENT_EVIDENCE",
                              "detail": message}, indent=2))
        else:
            print(f"INSUFFICIENT_EVIDENCE: {message}")
            if _required():
                print(f"  {REQUIRE_ENV} is set, so this is a failure: a security "
                      f"baseline that did not run is not a satisfied baseline.")
        return 2

    low = int(totals.get("SEVERITY.LOW", 0))
    medium = int(totals.get("SEVERITY.MEDIUM", 0))
    high = int(totals.get("SEVERITY.HIGH", 0))
    lines = int(totals.get("loc", 0))
    ok, reason = bandit_low_within_baseline(low)

    result = {
        "low": low, "low_baseline": BANDIT_LOW_BASELINE,
        "medium": medium, "high": high,
        "expected_medium": BANDIT_MEDIUM, "expected_high": BANDIT_HIGH,
        "scanned_lines": lines,
        "non_vacuous": lines >= _MIN_LINES,
        "detail": reason,
        "verdict": "PASS" if ok else "FAIL",
    }
    # A clean count over a scan that covered nothing is not evidence of anything.
    if not result["non_vacuous"]:
        result["verdict"] = "INSUFFICIENT_EVIDENCE"
        result["detail"] = (f"only {lines} lines scanned (floor {_MIN_LINES}); the "
                           f"count proves nothing")
    if medium != BANDIT_MEDIUM or high != BANDIT_HIGH:
        result["verdict"] = "FAIL"
        result["detail"] = (f"blocking severities changed: {medium} Medium / {high} "
                            f"High, declared {BANDIT_MEDIUM} / {BANDIT_HIGH}")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"bandit Low baseline: {low} <= {BANDIT_LOW_BASELINE} ? "
              f"{result['verdict']}")
        print(f"  medium={medium} high={high} scanned_lines={lines}")
        print(f"  {result['detail']}")

    if result["verdict"] == "PASS":
        return 0
    return 1 if result["verdict"] == "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main())
