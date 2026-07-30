"""scripts/build_release_evidence_m61.py — V69 M61.8: reproducible release evidence.

ONE bounded command that produces the artifact a release consumer can actually check:

    python jarvis/scripts/build_release_evidence_m61.py --output <temp-dir>

It writes, into the output directory:

  * ``release-evidence.json``        — sanitized, schema-validated evidence;
  * ``release-evidence.schema.json`` — the schema, so the bundle validates standalone;
  * ``SHA256SUMS``                   — over the wheel and sdist, verified immediately;
  * ``jarvis-<version>-py3-none-any.whl`` / ``jarvis-<version>.tar.gz``;
  * ``sbom-cyclonedx-<profile>.json`` — one per dependency profile.

SAFETY POSTURE — what this harness will NEVER do
------------------------------------------------
  * no git mutation: git is read ONLY (``rev-parse``, ``status --porcelain``);
  * no tag, no push, no merge, no GitHub Release, no package upload — nothing is
    published anywhere, by any code path;
  * no package installed onto the operator's host, and no optional dependency profile
    installed to make an SBOM look more complete;
  * no Ollama call, model download, server restart or setting change;
  * no Windows service, scheduled task, registry write or startup entry;
  * no ``shell=True``, ``os.system``, ``eval`` or ``exec``;
  * the wheel and sdist are built into a TEMPORARY directory and moved into the output
    only after their checksums verify.

HONESTY POSTURE
---------------
A gate that did not run is reported as ``SKIPPED``; a gate that could not be evaluated
is ``INSUFFICIENT_EVIDENCE``. Neither becomes ``PASS``, and a bundle containing either
in a mandatory section is written with ``evidence_completeness = "INCOMPLETE"`` and
exits non-zero (unless ``--allow-incomplete`` says the caller wants the partial record
anyway). The qualification verdict is copied verbatim from the qualification artifact —
``PASS_WITH_WARNINGS`` is never rounded up.

The whole output is assembled in a temporary staging directory and moved into
``--output`` only after validation and sanitization succeed. A failure leaves the output
directory exactly as it was.

Usage::

    # everything, using a qualification artifact produced earlier
    python jarvis/scripts/qualify_release_m61.py --full --output /tmp/qual.json
    python jarvis/scripts/build_release_evidence_m61.py --output /tmp/evidence \\
        --qualification /tmp/qual.json

    # fast structural run (no suites, no build): honest SKIPPED sections
    python jarvis/scripts/build_release_evidence_m61.py --output /tmp/evidence \\
        --skip-tests --skip-build --skip-bandit --allow-incomplete
"""
from __future__ import annotations

import argparse
import datetime as _datetime
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent
sys.path.insert(0, str(_APP_ROOT))

from core import release_artifacts, release_evidence, release_facts, sbom  # noqa: E402
from core.release_evidence import Status  # noqa: E402
from core.version import MILESTONE_TAG, VERSION  # noqa: E402

#: Bounded so no gate can turn this into an unbounded benchmark on a 15 W CPU.
_PYTEST_TIMEOUT = 2700
_BUILD_TIMEOUT = 1200
_TOOL_TIMEOUT = 900

#: ``N passed, M skipped`` / ``N failed`` from pytest's terse summary line.
_SUMMARY_RES = {
    "passed": re.compile(r"(\d+) passed"),
    "skipped": re.compile(r"(\d+) skipped"),
    "failed": re.compile(r"(\d+) (?:failed|error)"),
}


def _run(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int | None, str]:
    """Run a bounded subprocess. Returns ``(returncode, bounded tail)``.

    ``shell`` is left at its default ``False``; the argv is always a list, never a
    string, so no value here can be interpreted by a shell.
    """
    try:
        completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001 — a harness must not crash on a tool
        return None, type(exc).__name__
    tail = (completed.stdout or "")[-4000:] + (completed.stderr or "")[-2000:]
    return completed.returncode, tail.strip()


def _git(args: list[str]) -> str | None:
    """Read-only git. Every subcommand used here is an inspection command."""
    code, out = _run(["git", *args], cwd=_REPO_ROOT, timeout=30)
    return out if code == 0 else None


def _git_state() -> dict:
    commit = _git(["rev-parse", "HEAD"]) or ""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    status = _git(["status", "--porcelain"])
    return {
        "commit": commit[:12],
        "branch": branch,
        "clean": (status == "") if status is not None else False,
    }


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════════
#  Gates
# ══════════════════════════════════════════════════════════════════════════════
def _pytest_section(command: str, argv: list[str], cwd: Path, *,
                    skip: bool) -> dict:
    if skip:
        return release_evidence.pytest_result(
            Status.SKIPPED, command=command)
    code, tail = _run(argv, cwd=cwd, timeout=_PYTEST_TIMEOUT)
    if code is None:
        return release_evidence.pytest_result(
            Status.INSUFFICIENT_EVIDENCE, command=command)
    counts = {}
    for key, pattern in _SUMMARY_RES.items():
        match = pattern.search(tail)
        counts[key] = int(match.group(1)) if match else None
    # A zero exit with no parseable summary means the suite did not report — that is
    # missing evidence, not a pass.
    if counts["passed"] is None:
        return release_evidence.pytest_result(
            Status.INSUFFICIENT_EVIDENCE, command=command)
    status = Status.PASS if code == 0 and not counts["failed"] else Status.FAIL
    return release_evidence.pytest_result(
        status, command=command, passed=counts["passed"],
        skipped=counts["skipped"] or 0, failed=counts["failed"] or 0)


def _ruff_section(*, skip: bool) -> dict:
    command = release_facts.RUFF_COMMAND
    if skip:
        return release_evidence.tool_result(Status.SKIPPED, command=command)
    code, _tail = _run(["ruff", "check", "jarvis/"], cwd=_REPO_ROOT,
                       timeout=_TOOL_TIMEOUT)
    if code is None:
        return release_evidence.tool_result(
            Status.INSUFFICIENT_EVIDENCE, command=command,
            detail="ruff is not installed (dev profile)")
    return release_evidence.tool_result(
        Status.PASS if code == 0 else Status.FAIL, command=command)


def _compileall_section(*, skip: bool) -> dict:
    targets = list(release_facts.COMPILEALL_TARGETS)
    command = "python -m compileall -q " + " ".join(targets)
    if skip:
        return release_evidence.tool_result(Status.SKIPPED, command=command)
    code, _tail = _run([sys.executable, "-m", "compileall", "-q", *targets],
                       cwd=_REPO_ROOT, timeout=_TOOL_TIMEOUT)
    if code is None:
        return release_evidence.tool_result(
            Status.INSUFFICIENT_EVIDENCE, command=command)
    return release_evidence.tool_result(
        Status.PASS if code == 0 else Status.FAIL, command=command)


def _bandit_section(*, skip: bool) -> dict:
    """The EXACT CI gate, plus the Low baseline this release is accountable for."""
    command = release_facts.BANDIT_GATE_COMMAND
    base = {
        "command": command,
        "threshold": release_facts.BANDIT_GATE_THRESHOLD,
        "low_baseline": release_facts.BANDIT_LOW_BASELINE,
        "suppressions": release_facts.BANDIT_SUPPRESSION_COUNT,
    }
    if skip:
        return {**base, "status": Status.SKIPPED.value,
                "medium": 0, "high": 0, "low": 0}

    with tempfile.TemporaryDirectory(prefix="m61_bandit_") as work:
        report = Path(work) / "bandit.json"
        # No `-ll` here: the JSON report must carry the Low count too, and the
        # medium/high gate is evaluated from the same report rather than from a second
        # scan that could disagree with it.
        code, tail = _run(
            [sys.executable, "-m", "bandit", "-r", "core", "tools", "-q",
             "-f", "json", "-o", str(report)],
            cwd=_APP_ROOT, timeout=_TOOL_TIMEOUT)
        if code is None or not report.is_file():
            return {**base, "status": Status.INSUFFICIENT_EVIDENCE.value,
                    "medium": 0, "high": 0, "low": 0,
                    "detail": "bandit produced no report" if code is not None
                              else tail[:80]}
        try:
            totals = json.loads(report.read_text(encoding="utf-8"))
            metrics = totals["metrics"]["_totals"]
        except (OSError, ValueError, KeyError):
            return {**base, "status": Status.INSUFFICIENT_EVIDENCE.value,
                    "medium": 0, "high": 0, "low": 0,
                    "detail": "bandit report unparseable"}

    medium = int(metrics.get("SEVERITY.MEDIUM", 0))
    high = int(metrics.get("SEVERITY.HIGH", 0))
    low = int(metrics.get("SEVERITY.LOW", 0))
    lines = int(metrics.get("loc", 0))
    within, reason = release_facts.bandit_low_within_baseline(low)
    gate_ok = medium == 0 and high == 0
    # Non-vacuity: a clean report over a gutted scan is not a clean report.
    scanned_enough = lines >= 40_000
    status = (Status.PASS if gate_ok and within and scanned_enough
              else Status.FAIL if not (gate_ok and within)
              else Status.INSUFFICIENT_EVIDENCE)
    return {**base, "status": status.value, "medium": medium, "high": high,
            "low": low, "scanned_lines": lines, "low_baseline_detail": reason}


def _soak_section(*, skip: bool) -> dict:
    command = "python scripts/soak_stabilization_m61.py --cycles 25"
    if skip:
        return release_evidence.tool_result(Status.SKIPPED, command=command)
    code, _tail = _run(
        [sys.executable, str(_APP_ROOT / "scripts" / "soak_stabilization_m61.py"),
         "--cycles", "25"], cwd=_APP_ROOT, timeout=_TOOL_TIMEOUT)
    if code is None:
        return release_evidence.tool_result(
            Status.INSUFFICIENT_EVIDENCE, command=command)
    return release_evidence.tool_result(
        Status.PASS if code == 0 else Status.FAIL, command=command)


def _qualification_section(path: str | None) -> dict:
    """Copy the qualification verdict VERBATIM from its artifact.

    Nothing is recomputed and nothing is rounded: a ``PASS_WITH_WARNINGS`` stays
    ``PASS_WITH_WARNINGS``, and an absent artifact is ``INSUFFICIENT_EVIDENCE`` — the
    release qualification cannot be asserted by an evidence run that never saw it.
    """
    command = release_facts.QUALIFICATION_COMMAND
    if not path:
        return {"status": Status.INSUFFICIENT_EVIDENCE.value, "command": command,
                "verdict": "NOT_PROVIDED", "warnings": [], "gates": {},
                "detail": "no --qualification artifact supplied"}
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": Status.INSUFFICIENT_EVIDENCE.value, "command": command,
                "verdict": "UNREADABLE", "warnings": [], "gates": {},
                "detail": type(exc).__name__}

    verdict = str(artifact.get("verdict") or "UNKNOWN")
    warnings = [str(w) for w in (artifact.get("warnings") or ())]
    gates = {
        str(name): (Status.PASS.value if isinstance(gate, dict) and gate.get("ok")
                    else Status.FAIL.value)
        for name, gate in (artifact.get("gates") or {}).items()
        if name != "live"
    }
    missing = [g for g in release_facts.MANDATORY_GATES if g not in gates]
    if artifact.get("version") not in (None, VERSION):
        return {"status": Status.INSUFFICIENT_EVIDENCE.value, "command": command,
                "verdict": verdict, "warnings": warnings, "gates": gates,
                "detail": "qualification artifact is for a different version"}
    if missing:
        return {"status": Status.INSUFFICIENT_EVIDENCE.value, "command": command,
                "verdict": verdict, "warnings": warnings, "gates": gates,
                "detail": f"mandatory gates absent: {','.join(sorted(missing))}"}

    status = (Status.PASS if verdict == "PASS"
              else Status.PASS_WITH_WARNINGS if verdict == "PASS_WITH_WARNINGS"
              else Status.FAIL if verdict == "FAIL"
              else Status.INSUFFICIENT_EVIDENCE)
    return {"status": status.value, "command": command, "verdict": verdict,
            "warnings": warnings, "gates": gates}


def _build_artifacts(staging: Path, *, skip: bool) -> tuple[dict, dict, dict]:
    """``(packaging_section, package_scan_section, checksum_inventory)``.

    The build goes into a nested temporary directory; artifacts move into ``staging``
    only after ``SHA256SUMS`` verifies against their bytes.
    """
    if skip:
        return (
            {"status": Status.SKIPPED.value, "wheel": None, "sdist": None},
            release_evidence.tool_result(
                Status.SKIPPED,
                command="python scripts/check_package_manifest.py --dist <dir>"),
            {},
        )

    build_dir = Path(tempfile.mkdtemp(prefix="m61_build_"))
    try:
        code, tail = _run([sys.executable, "-m", "build", "--outdir", str(build_dir)],
                          cwd=_APP_ROOT, timeout=_BUILD_TIMEOUT)
        if code != 0:
            detail = ("python -m build is unavailable"
                      if code is None or "No module named build" in tail
                      else "build failed")
            return (
                {"status": Status.INSUFFICIENT_EVIDENCE.value if code is None
                           else Status.FAIL.value,
                 "wheel": None, "sdist": None, "detail": detail},
                release_evidence.tool_result(
                    Status.INSUFFICIENT_EVIDENCE,
                    command="python scripts/check_package_manifest.py --dist <dir>",
                    detail="nothing was built to scan"),
                {},
            )

        scan_code, _scan_tail = _run(
            [sys.executable, str(_APP_ROOT / "scripts" / "check_package_manifest.py"),
             "--dist", str(build_dir)], cwd=_APP_ROOT, timeout=_TOOL_TIMEOUT)
        scan_section = release_evidence.tool_result(
            Status.PASS if scan_code == 0
            else Status.INSUFFICIENT_EVIDENCE if scan_code is None
            else Status.FAIL,
            command="python scripts/check_package_manifest.py --dist <dir>")

        release_artifacts.write_checksums(build_dir)
        inventory = release_artifacts.artifact_inventory(build_dir)

        for name in (*sorted(inventory), release_facts.CHECKSUM_FILENAME):
            shutil.move(str(build_dir / name), str(staging / name))

        wheel = next((n for n in inventory if n.endswith(".whl")), None)
        sdist = next((n for n in inventory if n.endswith(".tar.gz")), None)
        packaging = {
            "status": (Status.PASS.value if wheel and sdist
                       else Status.INSUFFICIENT_EVIDENCE.value),
            "wheel": wheel, "sdist": sdist,
            "checksum_file": release_facts.CHECKSUM_FILENAME,
            "checksum_algorithm": release_artifacts.CHECKSUM_ALGORITHM,
        }
        return packaging, scan_section, inventory
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _sbom_files(staging: Path, profiles: list[str], *, skip: bool,
                observe_installed: bool, timestamp: str) -> tuple[dict, dict]:
    """``(sbom_inventory, detail)``. Written into ``staging`` and hashed."""
    if skip or not profiles:
        return {}, {"status": Status.SKIPPED.value,
                    "reason": "sbom generation not requested"}
    documents = sbom.build_all(profiles, timestamp=timestamp,
                              observe_installed=observe_installed)
    written = sbom.write_sboms(staging, documents)
    inventory: dict[str, dict] = {}
    for profile, path in sorted(written.items()):
        inventory[path.name] = {
            "sha256": release_artifacts.sha256_file(path),
            "size_bytes": path.stat().st_size,
            "kind": "cyclonedx-1.5-json",
            "profile": profile,
            "components": len(documents[profile]["components"]),
        }
    return inventory, {"status": Status.PASS.value,
                       "profiles": sorted(written),
                       "format": "CycloneDX 1.5 JSON",
                       "resolution_mode": ("declared+observed" if observe_installed
                                           else "declared")}


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="M61.8 sanitized, reproducible release evidence")
    ap.add_argument("--output", required=True, metavar="DIR",
                    help="directory the evidence bundle is written into")
    ap.add_argument("--qualification", metavar="PATH",
                    help="a qualify_release_m61.py --output artifact to copy verbatim")
    ap.add_argument("--skip-tests", action="store_true",
                    help="do not run either pytest layout (recorded as SKIPPED)")
    ap.add_argument("--skip-lint", action="store_true")
    ap.add_argument("--skip-bandit", action="store_true")
    ap.add_argument("--skip-build", action="store_true",
                    help="do not build the wheel/sdist (no checksums either)")
    ap.add_argument("--skip-sbom", action="store_true")
    ap.add_argument("--skip-soak", action="store_true")
    ap.add_argument("--sbom-profiles", default=",".join(sbom.MANDATORY_PROFILES),
                    help="comma-separated dependency profiles to describe")
    ap.add_argument("--observe-installed", action="store_true",
                    help="attach installed versions for packages present here")
    ap.add_argument("--live-status", default=release_facts.LIVE_VALIDATION_STATUS,
                    choices=["NOT_REQUESTED", "PASS", "FAIL",
                             "INSUFFICIENT_EVIDENCE"],
                    help="optional live validation outcome; never inferred")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="exit 0 even when a mandatory section has no evidence")
    args = ap.parse_args(argv)

    print("=" * 78)
    print(f"JARVIS {MILESTONE_TAG} ({VERSION}) {release_facts.RELEASE_STAGE} "
          f"- release evidence")
    print("=" * 78)

    profiles = [p.strip() for p in args.sbom_profiles.split(",") if p.strip()]
    timestamp = _utc_now()
    git = _git_state()
    print(f"git: branch={git['branch']} commit={git['commit']} clean={git['clean']}")

    staging = Path(tempfile.mkdtemp(prefix="m61_evidence_stage_"))
    try:
        sections: dict[str, dict] = {}

        sections["pytest_root_layout"] = _pytest_section(
            release_facts.PYTEST_ROOT_LAYOUT_COMMAND,
            [sys.executable, "-m", "pytest", "-q", "jarvis/tests", "tests"],
            _REPO_ROOT, skip=args.skip_tests)
        print(f"[{sections['pytest_root_layout']['status']}] pytest (root layout)")

        sections["pytest_app_layout"] = _pytest_section(
            release_facts.PYTEST_APP_LAYOUT_COMMAND,
            [sys.executable, "-m", "pytest", "-q", "tests", "../tests"],
            _APP_ROOT, skip=args.skip_tests)
        print(f"[{sections['pytest_app_layout']['status']}] pytest (app layout)")

        sections["ruff"] = _ruff_section(skip=args.skip_lint)
        print(f"[{sections['ruff']['status']}] ruff")
        sections["compileall"] = _compileall_section(skip=args.skip_lint)
        print(f"[{sections['compileall']['status']}] compileall")

        sections["bandit"] = _bandit_section(skip=args.skip_bandit)
        b = sections["bandit"]
        print(f"[{b['status']}] bandit  medium={b['medium']} high={b['high']} "
              f"low={b['low']}/{b['low_baseline']}")

        packaging, scan, checksums = _build_artifacts(staging, skip=args.skip_build)
        sections["packaging"] = packaging
        sections["package_scan"] = scan
        print(f"[{packaging['status']}] packaging  "
              f"artifacts={len(checksums)}")
        print(f"[{scan['status']}] package manifest + secret scan")

        sections["soak"] = _soak_section(skip=args.skip_soak)
        print(f"[{sections['soak']['status']}] deterministic soak")

        sections["qualification"] = _qualification_section(args.qualification)
        q = sections["qualification"]
        print(f"[{q['status']}] qualification  verdict={q['verdict']} "
              f"warnings={q['warnings']}")

        sections["live_validation"] = {
            "status": args.live_status,
            "detail": ("optional live validation was not executed for this bundle"
                       if args.live_status == "NOT_REQUESTED"
                       else "operator-supplied live outcome"),
        }
        print(f"[{args.live_status}] live validation (optional)")

        sbom_inventory, sbom_detail = _sbom_files(
            staging, profiles, skip=args.skip_sbom,
            observe_installed=args.observe_installed, timestamp=timestamp)
        print(f"[{sbom_detail['status']}] SBOM  {sbom_detail.get('profiles', [])}")

        document = release_evidence.build_evidence(
            generated_at_utc=timestamp,
            git_commit=git["commit"],
            git_branch=git["branch"],
            git_clean=git["clean"],
            python_version=platform.python_version(),
            sections=sections,
            dependency_hashes=release_evidence.dependency_profile_hashes(
                _APP_ROOT / "requirements"),
            artifact_checksums=checksums,
            sbom=sbom_inventory,
        )
        document["sbom_detail"] = sbom_detail
        document["measurement_environment_note"] = (
            release_facts.MEASUREMENT_ENVIRONMENT)

        try:
            written = release_evidence.write_evidence(document, staging)
        except release_evidence.EvidenceRejected as exc:
            print("-" * 78)
            print("EVIDENCE REJECTED - nothing was written:")
            for problem in exc.problems[:40]:
                print(f"  - {problem}")
            return 2

        # The staging directory is complete and validated; only now does anything
        # appear in the operator's output directory.
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        for entry in sorted(staging.iterdir()):
            shutil.move(str(entry), str(output / entry.name))
        print("-" * 78)
        print(f"evidence: {written.name}  "
              f"completeness={document['evidence_completeness']}  "
              f"overall={document['overall_status']}")
        if document["evidence_gaps"]:
            print(f"gaps: {document['evidence_gaps']}")
        print(f"files: {sorted(p.name for p in output.iterdir())}")
        print("=" * 78)

        if document["evidence_completeness"] != "COMPLETE" \
                and not args.allow_incomplete:
            return 1
        if document["overall_status"] == Status.FAIL.value:
            return 1
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
