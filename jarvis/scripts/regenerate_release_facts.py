"""scripts/regenerate_release_facts.py — V69 S5E: measured facts must be derived.

WHY THIS EXISTS
===============
``core/release_facts.py`` holds two kinds of number, and until S5E nothing in the
repository distinguished them:

  MEASURED   what a scanner reports about the tree that is checked out right now —
             ``BANDIT_MEDIUM``, ``BANDIT_HIGH``, ``BANDIT_LOW_OBSERVED``,
             ``BANDIT_SCANNED_LINES``. These are enforced against the CURRENT tree by
             ``tests/test_bandit_low_baseline_v69_m618.py``, so they go stale the moment
             the tree grows and nobody re-measures.

  APPROVED   a human decision that a reviewer signed off on — ``BANDIT_LOW_BASELINE``.
             It is a ceiling, and raising it is exactly the judgement a reviewer must
             make in a diff they can see.

This script regenerates the MEASURED group and refuses to touch the APPROVED one. That
boundary is the whole point: a generator that could quietly raise its own ceiling would
be a generator that can hide a security regression, which is precisely the failure mode
``BANDIT_LOW_BASELINE`` exists to prevent.

HOW IT MEASURES
===============
It does not run the scanner itself. It shells out to ``check_bandit_low_baseline.py
--json``, which is the same measurement path the drift monitor and the test suite use.
Sharing one path is deliberate: two independent scan implementations would eventually
disagree, and then neither would be evidence.

USAGE
=====
    python scripts/regenerate_release_facts.py --check    # exit 1 if a fact is stale
    python scripts/regenerate_release_facts.py            # rewrite the measured facts

``--check`` is what the test suite runs, so a stale fact fails a build rather than
surviving into a release note. Running the script twice in a row must produce no second
diff; ``tests/test_release_fact_reality_v69_s5e.py`` asserts that idempotence directly.

Exit codes: ``0`` facts current (or written), ``1`` facts stale (``--check`` only),
``2`` the measurement could not be taken and nothing was written.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_FACTS = _APP_ROOT / "core" / "release_facts.py"
_MONITOR = _APP_ROOT / "scripts" / "check_bandit_low_baseline.py"

#: measured fact -> key in the monitor's JSON. ``BANDIT_LOW_BASELINE`` is deliberately
#: absent and must stay absent; see the module docstring.
_DERIVED: dict[str, str] = {
    "BANDIT_MEDIUM": "medium",
    "BANDIT_HIGH": "high",
    "BANDIT_LOW_OBSERVED": "low",
    "BANDIT_SCANNED_LINES": "scanned_lines",
}

#: Written with underscore separators past this magnitude, matching the file's style.
_GROUPED_ABOVE = 10_000

#: ``BANDIT_SCANNED_LINES`` is a MAGNITUDE claim, not a count: it exists so a clean
#: scan over a gutted tree cannot pass as evidence. Its owning test allows 5% for that
#: reason ("requiring an exact match would fail on every unrelated one-line commit"), so
#: this script uses the SAME tolerance. A generator stricter than the test it feeds
#: would report drift nobody needs to act on, and a stale-fact alarm that cries wolf is
#: one people learn to ignore. The finding counts have no tolerance: they are exact.
_TOLERANT = {"BANDIT_SCANNED_LINES": (0.05, 500)}


def _is_stale(name: str, declared: str, measured: int) -> bool:
    """Whether a declared fact is wrong, under that fact's own comparison rule."""
    if name not in _TOLERANT:
        return declared != _format(name, measured)
    try:
        current = int(declared.replace("_", ""))
    except ValueError:
        return True
    fraction, floor = _TOLERANT[name]
    return abs(measured - current) > max(floor, measured * fraction)


def _format(name: str, value: int) -> str:
    if value >= _GROUPED_ABOVE:
        return f"{value:_}"
    return str(value)


def measure() -> dict | None:
    """The monitor's own JSON verdict, or None if the scanner could not run."""
    proc = subprocess.run([sys.executable, str(_MONITOR), "--json"],
                          cwd=str(_APP_ROOT), capture_output=True, text=True,
                          timeout=1800, check=False)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        return None
    if result.get("verdict") == "INSUFFICIENT_EVIDENCE":
        return None
    return result


def rewrite(text: str, measured: dict) -> tuple[str, list[str]]:
    """Return the updated source and a description of every fact that changed."""
    changes: list[str] = []
    for name, key in _DERIVED.items():
        if key not in measured:
            continue
        want = _format(name, int(measured[key]))
        pattern = re.compile(rf"^{name} = (.+)$", re.MULTILINE)
        match = pattern.search(text)
        if match is None:
            changes.append(f"{name}: DECLARATION MISSING from core/release_facts.py")
            continue
        if _is_stale(name, match.group(1), int(measured[key])):
            changes.append(f"{name}: {match.group(1)} -> {want}")
            text = pattern.sub(f"{name} = {want}", text, count=1)
    return text, changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate the measured release facts")
    ap.add_argument("--check", action="store_true",
                    help="report staleness and write nothing (exit 1 if stale)")
    args = ap.parse_args(argv)

    measured = measure()
    if measured is None:
        print("INSUFFICIENT_EVIDENCE: the scanner could not be run; nothing written "
              "(pip install -r requirements/dev.txt)")
        return 2

    original = _FACTS.read_text(encoding="utf-8")
    updated, changes = rewrite(original, measured)

    if not changes:
        print("release facts are current:")
        for name, key in _DERIVED.items():
            print(f"  {name} = {_format(name, int(measured[key]))}")
        return 0

    if args.check:
        print("STALE — these measured facts no longer match the tree:")
        for change in changes:
            print(f"  {change}")
        print("run: python scripts/regenerate_release_facts.py")
        return 1

    _FACTS.write_text(updated, encoding="utf-8")
    print("rewrote core/release_facts.py:")
    for change in changes:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
