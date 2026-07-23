"""scripts/qualify_release_m59.py — V69 M59.6: reproducible release qualification.

ONE bounded command that coordinates the whole M59 release gate and emits a single
machine-readable verdict plus a concise human report. It orchestrates — it never
mutates:

  1. git-state verification (read-only)
  2. focused M59 deterministic tests
  3. relevant M55-M58 regression suites
  4. ruff
  5. compileall
  6. deterministic soak (bounded)
  7. optional bounded live qualification (only with --live)
  8. a machine-readable JSON result + a human report

SAFETY POSTURE — what this harness will NEVER do
------------------------------------------------
  * no git commit, no merge, no branch mutation (git is read ONLY);
  * no Ollama setting, no model download, no restart;
  * no semantic collection read/written; no environment variable changed;
  * no host/power configuration touched;
  * temporary files, if any, live under an explicit output dir and are cleaned.

Usage (from the repo root)::

    python jarvis/scripts/qualify_release_m59.py --quick
    python jarvis/scripts/qualify_release_m59.py --full --live --output rel.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.qualification import (  # noqa: E402
    QUALIFICATION_SCHEMA_VERSION,
    host_profile_snapshot,
    release_verdict,
)

# The bounded, focused M59 deterministic suites.
_M59_TESTS = (
    "tests/test_inference_profile_v69_m591.py",
    "tests/test_session_warmth_v69_m592.py",
    "tests/test_qualification_v69_m593.py",
    "tests/test_compaction_quality_v69_m594.py",
)
# Touched M55-M58 regression the release must keep green.
_REGRESSION_TESTS = (
    "tests/test_contract_family_v69_m584.py",
    "tests/test_prefix_cache_v69_m585.py",
    "tests/test_prompt_manifest_v69_m581.py",
    "tests/test_compaction_scheduler_v69_m586.py",
    "tests/test_residency_governor_v69_m565.py",
    "tests/test_prompt_cache_health_v69_m589.py",
    "tests/test_runtime_health_v67.py",
)
_COMPILE_TARGETS = ("core", "tools", "scripts", "main.py")


def _run(cmd, timeout=900) -> tuple[bool, str]:
    """Run a bounded subprocess from the repo root. Returns (ok, tail)."""
    try:
        out = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True,
                             timeout=timeout)
        tail = (out.stdout or "")[-600:] + (out.stderr or "")[-300:]
        return out.returncode == 0, tail.strip()
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _git_state() -> dict:
    def _read(args):
        try:
            r = subprocess.run(["git", *args], cwd=_ROOT, capture_output=True,
                               text=True, timeout=5.0)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None
    status = _read(["status", "--porcelain"])
    return {"commit": _read(["rev-parse", "HEAD"]),
            "branch": _read(["rev-parse", "--abbrev-ref", "HEAD"]),
            "clean": status == "" if status is not None else None,
            "m58_ancestor": _read(["merge-base", "--is-ancestor", "58b52dd", "HEAD"])
            is not None}


def _pytest(paths, timeout=900) -> tuple[bool, str]:
    return _run([sys.executable, "-m", "pytest", "-q", *paths], timeout=timeout)


def _live_qualification(quick: bool) -> str | None:
    """Run the bounded live prefix qualification and return its verdict, or None on a
    harness error. Never fatal to the release run."""
    args = [sys.executable, "scripts/qualify_runtime_m59.py",
            "--live", "--json", "--quick" if quick else "--full"]
    ok, tail = _run(args, timeout=600)
    try:
        # The artifact is the last JSON object printed.
        start = tail.rfind("{")
        return json.loads(tail[start:]).get("verdict") if start >= 0 else None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="M59.6 reproducible release qualification")
    ap.add_argument("--quick", action="store_true", help="focused suites only")
    ap.add_argument("--full", action="store_true", help="full regression too")
    ap.add_argument("--live", action="store_true", help="add bounded live qualification")
    ap.add_argument("--output", metavar="PATH", help="write the JSON result to PATH")
    args = ap.parse_args()

    print("=" * 78)
    print("JARVIS V69 M59.6 - reproducible release qualification")
    print("=" * 78)
    git = _git_state()
    print(f"git: branch={git['branch']} clean={git['clean']} "
          f"m58_ancestor={git['m58_ancestor']}")

    gates: dict[str, dict] = {}

    det_ok, det_tail = _pytest(_M59_TESTS)
    gates["m59_deterministic"] = {"ok": det_ok}
    print(f"[{'PASS' if det_ok else 'FAIL'}] M59 deterministic tests")

    reg_paths = _REGRESSION_TESTS if (args.full or not args.quick) else _REGRESSION_TESTS[:3]
    reg_ok, reg_tail = _pytest(reg_paths)
    gates["regression"] = {"ok": reg_ok}
    print(f"[{'PASS' if reg_ok else 'FAIL'}] regression suites ({len(reg_paths)})")

    ruff_ok, ruff_tail = _run(["ruff", "check", "core", "tools", "scripts"], timeout=180)
    gates["ruff"] = {"ok": ruff_ok}
    print(f"[{'PASS' if ruff_ok else 'FAIL'}] ruff")

    comp_ok, comp_tail = _run([sys.executable, "-m", "compileall", "-q", *_COMPILE_TARGETS],
                              timeout=180)
    gates["compile"] = {"ok": comp_ok}
    print(f"[{'PASS' if comp_ok else 'FAIL'}] compileall")

    # Deterministic soak: the bounded long-session prefix soak (server-free).
    soak_ok, soak_tail = _pytest(("tests/test_qualification_v69_m593.py",))
    gates["soak"] = {"ok": soak_ok}
    print(f"[{'PASS' if soak_ok else 'FAIL'}] deterministic soak")

    live_verdict = None
    warnings = []
    if args.live:
        live_verdict = _live_qualification(args.quick)
        gates["live"] = {"verdict": live_verdict}
        print(f"[{live_verdict or 'INSUFFICIENT_EVIDENCE'}] live qualification")
        if live_verdict in (None, "INSUFFICIENT_EVIDENCE"):
            warnings.append("live_evidence_missing_or_insufficient")
    else:
        warnings.append("live_not_requested")

    verdict = release_verdict(
        deterministic_ok=det_ok, regression_ok=reg_ok, ruff_ok=ruff_ok,
        compile_ok=comp_ok, soak_ok=soak_ok, live_verdict=live_verdict,
        warnings=warnings)

    result = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "kind": "release_qualification",
        "timestamp": time.time(),
        "git": git,
        "host": host_profile_snapshot(),
        "mode": "quick" if args.quick else "full",
        "gates": gates,
        "warnings": warnings,
        "verdict": verdict,
    }
    print("-" * 78)
    print(f"RELEASE VERDICT: {verdict}")
    if warnings:
        print(f"warnings: {warnings}")
    print("=" * 78)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    sys.exit(1 if verdict == "FAIL" else 0)


if __name__ == "__main__":
    main()
