"""scripts/qualify_release_m61.py — V69 M61.6: reproducible release qualification.

ONE bounded command that coordinates the whole M61 release gate and emits a single
machine-readable verdict plus a concise human report. It orchestrates existing
checkers — it never mutates:

   1. git state (READ-ONLY)
   2. version consistency          core.release_check
   3. documentation consistency    core.release_check
   4. dependency drift             core.dependency_authority
   5. base import smoke            scripts/check_base_import.py
   6. ruff
   7. compileall
   8. deterministic tests          the M61 suites, or the complete suite with --full
   9. packaging build              wheel + sdist into a TEMPORARY directory
  10. package manifest scan        scripts/check_package_manifest.py
  11. secret scan                  the same scanner, over the built artifacts
  12. bounded runtime soak         scripts/soak_stabilization_m61.py
  13. optional bounded live smoke  --live only; a missing Ollama is a WARNING

SAFETY POSTURE — what this harness will NEVER do
------------------------------------------------
  * no git commit, merge or branch mutation (git is read ONLY);
  * no Ollama setting, no model download, no server restart;
  * no semantic collection read or written; no environment variable changed;
  * no host/power configuration touched; no service or startup entry installed;
  * no package installed onto the operator's host;
  * build output goes to a TEMPORARY directory which is removed afterwards;
  * nothing is published anywhere.

The JSON artifact carries counts, enum states, milliseconds and gate names only —
never a prompt, a response, a tool argument, a secret or an absolute private path.

Usage (from the repo root)::

    python jarvis/scripts/qualify_release_m61.py --quick
    python jarvis/scripts/qualify_release_m61.py --full --output release.json
    python jarvis/scripts/qualify_release_m61.py --full --live
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.qualification import (  # noqa: E402
    QUALIFICATION_SCHEMA_VERSION,
    host_profile_snapshot,
    release_verdict,
)
from core.version import MILESTONE_TAG, VERSION  # noqa: E402

#: The focused M61 deterministic suites.
_M61_TESTS = (
    "tests/test_version_v69_m611.py",
    "tests/test_ci_workflow_v69_m612.py",
    "tests/test_dependency_authority_v69_m613.py",
    "tests/test_managed_logging_v69_m614.py",
    "tests/test_packaging_v69_m615.py",
    "tests/test_release_qualification_v69_m616.py",
)
#: M52-M60 regression the stabilization release must keep green. M61 changes the
#: logging, shutdown and continuity seams, so these are the suites that would notice.
_REGRESSION_TESTS = (
    "tests/test_session_journal_v69_m601.py",
    "tests/test_turn_reconciliation_v69_m602.py",
    "tests/test_session_restore_v69_m603.py",
    "tests/test_recovery_supervisor_v69_m604.py",
    "tests/test_diagnostics_backup_v69_m606.py",
    "tests/test_continuity_wiring_v69_m609.py",
    "tests/test_runtime_health_v67.py",
    "tests/test_session_manager_redaction.py",
    "tests/test_m541_regression_harness.py",
)
_COMPILE_TARGETS = ("core", "tools", "scripts", "aura", "main.py")


def _run(cmd, timeout=1800) -> tuple[bool, str]:
    """Run a bounded subprocess from the repo root. Returns (ok, bounded tail)."""
    try:
        out = subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True,
                             timeout=timeout)
        tail = (out.stdout or "")[-800:] + (out.stderr or "")[-400:]
        return out.returncode == 0, tail.strip()
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _git_state() -> dict:
    """Read-only git facts. No command here can modify the repository."""
    def _read(args):
        try:
            r = subprocess.run(["git", *args], cwd=_ROOT, capture_output=True,
                               text=True, timeout=10.0)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _ancestor(sha):
        try:
            r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                               cwd=_ROOT, capture_output=True, timeout=10.0)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return None

    status = _read(["status", "--porcelain"])
    return {
        "commit": (_read(["rev-parse", "HEAD"]) or "")[:12],
        "branch": _read(["rev-parse", "--abbrev-ref", "HEAD"]),
        "clean": status == "" if status is not None else None,
        # M60 must remain intact underneath M61.
        "m60_ancestor": _ancestor("df35289d"),
    }


def _pytest(paths, timeout=1800) -> tuple[bool, str]:
    args = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    return _run(args + list(paths), timeout=timeout)


def _script(name, *extra, timeout=900) -> tuple[bool, str]:
    return _run([sys.executable, os.path.join("scripts", name), *extra],
                timeout=timeout)


def _packaging_gate() -> tuple[bool, bool, dict]:
    """Build into a TEMPORARY directory and scan the artifacts. Returns
    ``(build_ok, scan_ok, detail)``. The directory is always removed."""
    outdir = tempfile.mkdtemp(prefix="m61_dist_")
    detail: dict = {"artifacts": 0}
    try:
        build_ok, _tail = _run(
            [sys.executable, "-m", "build", "--outdir", outdir], timeout=1200)
        if not build_ok:
            return False, False, detail
        detail["artifacts"] = len(
            [n for n in os.listdir(outdir) if n.endswith((".whl", ".tar.gz"))])
        scan_ok, scan_tail = _script("check_package_manifest.py", "--dist", outdir,
                                     timeout=300)
        detail["scan_verdict"] = "PASS" if scan_ok else "FAIL"
        if not scan_ok:
            detail["scan_tail"] = scan_tail[-400:]
        return True, scan_ok, detail
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _live_smoke() -> dict:
    """M61.6.2 — bounded, SAFE live checks. Never downloads, restarts or configures.

    Reachability only, plus one deterministic bypass that needs no model. Anything
    that would generate is left to the M59 runtime qualification; this gate exists to
    say honestly whether a live server was observable, not to re-measure latency.
    """
    result = {"ollama_reachable": None, "deterministic_bypass": None,
              "log_placement_ok": None, "verdict": "INSUFFICIENT_EVIDENCE"}
    t0 = time.perf_counter()
    try:
        import httpx

        from core.model_router import normalize_ollama_host
        host = normalize_ollama_host()
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"{host}/api/tags")
        result["ollama_reachable"] = response.status_code == 200
        result["reach_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as exc:  # noqa: BLE001
        result["ollama_reachable"] = False
        result["reach_error"] = type(exc).__name__

    # A deterministic bypass answers from trusted runtime data with NO model call.
    try:
        from core.deterministic_bypass import maybe_bypass
        answer = maybe_bypass("¿qué hora es?", language="es")
        result["deterministic_bypass"] = answer is not None
    except Exception as exc:  # noqa: BLE001
        result["deterministic_bypass"] = None
        result["bypass_error"] = type(exc).__name__

    # The runtime log must be inside the managed tree (M61.4).
    try:
        from core.managed_logging import runtime_log_file
        from core.managed_paths import app_root
        result["log_placement_ok"] = app_root() in runtime_log_file().parents
    except Exception as exc:  # noqa: BLE001
        result["log_placement_ok"] = False
        result["log_error"] = type(exc).__name__

    if result["ollama_reachable"] and result["log_placement_ok"]:
        result["verdict"] = "PASS"
    elif result["ollama_reachable"] is False:
        # Missing Ollama is INSUFFICIENT_EVIDENCE, never a false PASS and never a
        # FAIL: a service-free host is a supported configuration.
        result["verdict"] = "INSUFFICIENT_EVIDENCE"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="M61.6 reproducible release qualification")
    ap.add_argument("--quick", action="store_true",
                    help="focused M61 suites only (skip the complete suite)")
    ap.add_argument("--full", action="store_true",
                    help="the complete deterministic suite + regression")
    ap.add_argument("--live", action="store_true",
                    help="add the bounded, safe live smoke (M61.6.2)")
    ap.add_argument("--skip-packaging", action="store_true",
                    help="skip the build gate (it is the slowest)")
    ap.add_argument("--output", metavar="PATH", help="write the JSON result to PATH")
    args = ap.parse_args()
    full = args.full or not args.quick

    print("=" * 78)
    print(f"JARVIS {MILESTONE_TAG} ({VERSION}) - release qualification")
    print("=" * 78)
    git = _git_state()
    print(f"git: branch={git['branch']} clean={git['clean']} "
          f"m60_ancestor={git['m60_ancestor']}")

    gates: dict[str, dict] = {}
    warnings: list[str] = []

    def _gate(key: str, label: str, ok: bool, **extra) -> bool:
        gates[key] = {"ok": bool(ok), **extra}
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        return bool(ok)

    consistency_ok, _ = _script("check_release_consistency.py", timeout=180)
    _gate("consistency", "version / documentation / dependency consistency",
          consistency_ok)

    base_ok, _ = _script("check_base_import.py", timeout=300)
    _gate("base_import", "base text-mode import surface", base_ok)

    ruff_ok, _ = _run(["ruff", "check", "core", "tools", "scripts", "aura", "main.py"],
                      timeout=300)
    _gate("ruff", "ruff", ruff_ok)

    compile_ok, _ = _run([sys.executable, "-m", "compileall", "-q", *_COMPILE_TARGETS],
                         timeout=300)
    _gate("compile", "compileall", compile_ok)

    det_ok, det_tail = _pytest(_M61_TESTS, timeout=900)
    _gate("m61_deterministic", f"M61 deterministic suites ({len(_M61_TESTS)})", det_ok)
    if not det_ok:
        print(f"      {det_tail[-300:]}")

    if full:
        reg_ok, reg_tail = _pytest((), timeout=2700)   # no paths => the whole suite
        _gate("regression", "complete deterministic suite", reg_ok)
        if not reg_ok:
            print(f"      {reg_tail[-300:]}")
    else:
        reg_ok, _ = _pytest(_REGRESSION_TESTS, timeout=900)
        _gate("regression", f"M52-M60 regression ({len(_REGRESSION_TESTS)})", reg_ok)
        warnings.append("complete_suite_not_run")

    if args.skip_packaging:
        build_ok = scan_ok = True
        warnings.append("packaging_gate_skipped")
        print("[SKIP] packaging build + manifest scan")
    else:
        build_ok, scan_ok, detail = _packaging_gate()
        _gate("packaging_build", "wheel + sdist build", build_ok, **detail)
        _gate("package_manifest", "package manifest + secret scan", scan_ok)

    soak_ok, soak_tail = _script("soak_stabilization_m61.py", "--cycles", "25",
                                 timeout=900)
    _gate("soak", "bounded deterministic soak (25 cycles)", soak_ok)
    if not soak_ok:
        print(f"      {soak_tail[-300:]}")

    live_verdict = None
    if args.live:
        live = _live_smoke()
        live_verdict = live["verdict"]
        gates["live"] = live
        print(f"[{live_verdict}] bounded live smoke "
              f"(ollama_reachable={live['ollama_reachable']})")
        if live_verdict != "PASS":
            warnings.append("live_evidence_insufficient")
    else:
        warnings.append("live_not_requested")

    if git["clean"] is False:
        warnings.append("working_tree_not_clean")
    if git["m60_ancestor"] is False:
        warnings.append("m60_not_an_ancestor")

    verdict = release_verdict(
        deterministic_ok=det_ok,
        regression_ok=reg_ok,
        ruff_ok=ruff_ok,
        compile_ok=compile_ok,
        soak_ok=soak_ok,
        security_ok=scan_ok,
        bounded_ok=soak_ok,
        orphan_ok=soak_ok,
        live_verdict=live_verdict,
        warnings=warnings,
    )
    # The M61-specific gates are mandatory too; release_verdict does not know them.
    if not (consistency_ok and base_ok and build_ok):
        verdict = "FAIL"

    result = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "kind": "release_qualification",
        "milestone": MILESTONE_TAG,
        "version": VERSION,
        "timestamp": time.time(),
        "git": git,
        "host": host_profile_snapshot(),
        "mode": "full" if full else "quick",
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
            json.dump(result, fh, indent=2, default=str)

    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
