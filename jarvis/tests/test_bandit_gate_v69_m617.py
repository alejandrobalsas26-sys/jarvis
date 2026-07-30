"""tests/test_bandit_gate_v69_m617.py — V69 M61.7: the Bandit gate, proven locally.

CI runs ``bandit -r core tools -ll -q`` and trusts its exit code. That is the right
gate, but an exit code alone cannot distinguish "clean" from "scanned nothing": an
accidental ``exclude_dirs``, a broken path, a global skip list or a creeping pile of
``# nosec`` comments all produce a green run over an empty or gutted scan.

So this module re-runs the same scan in JSON mode and asserts the things the exit code
cannot:

  * zero Medium and zero High findings, itemised when it fails;
  * the scan actually covered a substantial number of files and lines (non-vacuity);
  * no broad exclusion is configured — no ``exclude_dirs``, no repo-wide skip list, no
    Bandit config file quietly narrowing the scan;
  * suppressions are bounded, individually justified, and cannot grow silently: the
    inventory of ``# nosec`` lines is pinned by exact location and Bandit id;
  * no blanket ``# nosec`` (one with no test id) exists anywhere.

SKIP POLICY. Bandit is a ``dev``-profile dependency. When it is absent the module skips
honestly rather than passing vacuously — but the skip is itself gated: with
``JARVIS_REQUIRE_BANDIT=1`` (set by the M61 release qualification) a missing Bandit is a
FAILURE, because "the security gate did not run" must not be a silent pass in the
environment that qualifies a release.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_APP_ROOT = Path(__file__).resolve().parent.parent

#: The exact gate CI runs, minus the output formatting.
GATE_TARGETS = ("core", "tools")
GATE_THRESHOLD = "-ll"

#: Set by the release qualification: a missing scanner becomes a failure, not a skip.
REQUIRE_ENV = "JARVIS_REQUIRE_BANDIT"

#: Every permitted suppression in the scanned tree, pinned by file and Bandit id.
#: Adding one requires editing this dict, which is the review checkpoint. Each entry
#: is justified beside the line in the source and by a dedicated regression test.
ALLOWED_SUPPRESSIONS: dict[str, tuple[str, ...]] = {
    # A compile-time constant command; paramiko has no non-shell exec primitive.
    # Proven constant over the AST by tests/test_ssh_hostkeys_v69_m617.py.
    "tools/ebpf_bridge.py": ("B601",),
    # A named constant declaration that binds nothing; B104 matches the literal
    # itself, so no spelling avoids it. Proven by tests/test_network_binding_v69_m617.py.
    "core/net_binding.py": ("B104",),
}

#: Non-vacuity floors. Deliberately far below the real numbers (~74k lines, ~250 files)
#: so ordinary growth or pruning never trips them, while a gutted scan always does.
MIN_LINES_SCANNED = 40_000
MIN_FILES_SCANNED = 100


def _bandit_available() -> bool:
    try:
        import bandit  # noqa: F401
    except ImportError:
        return shutil.which("bandit") is not None
    return True


def _require_bandit() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _skip_or_fail() -> None:
    message = (
        "bandit is not installed (dev profile: pip install -r requirements/dev.txt)"
    )
    if _require_bandit():
        pytest.fail(
            f"{REQUIRE_ENV} is set, so the Bandit gate is mandatory here, but {message}. "
            f"A security gate that did not run is not a passing security gate."
        )
    pytest.skip(message)


@pytest.fixture(scope="module")
def bandit_report() -> dict:
    """Run the CI gate in JSON mode and return the parsed report."""
    if not _bandit_available():
        _skip_or_fail()

    with tempfile.TemporaryDirectory() as work:
        # Temporary by construction: the report must never be committed.
        output = Path(work) / "bandit.json"
        completed = subprocess.run(
            [
                sys.executable, "-m", "bandit", "-r", *GATE_TARGETS,
                GATE_THRESHOLD, "-f", "json", "-o", str(output),
            ],
            cwd=_APP_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if not output.is_file():
            pytest.fail(
                "bandit produced no JSON report — the gate could not be evaluated.\n"
                f"exit={completed.returncode}\nstderr:\n{completed.stderr[-2000:]}"
            )
        return json.loads(output.read_text(encoding="utf-8"))


# ── the gate itself ─────────────────────────────────────────────────────────
def test_no_medium_or_high_findings(bandit_report: dict):
    """The release gate. Itemised on failure so the report is actionable."""
    findings = [
        f"{r['test_id']} {r['issue_severity']} {r['filename']}:{r['line_number']}"
        f" — {r['issue_text'][:100]}"
        for r in bandit_report["results"]
        if r["issue_severity"] in ("MEDIUM", "HIGH")
    ]
    assert findings == [], "Bandit medium/high findings:\n  " + "\n  ".join(findings)


def test_severity_totals_are_zero(bandit_report: dict):
    totals = bandit_report["metrics"]["_totals"]
    assert totals["SEVERITY.MEDIUM"] == 0
    assert totals["SEVERITY.HIGH"] == 0
    assert totals["SEVERITY.UNDEFINED"] == 0


def test_low_findings_are_recorded_not_hidden(bandit_report: dict):
    """Low severity is outside the release gate, but it is counted, not ignored.

    Asserts only that the number is reportable — pinning it would turn every
    unrelated code addition into a failure here.
    """
    low = bandit_report["metrics"]["_totals"]["SEVERITY.LOW"]
    assert isinstance(low, int) and low >= 0


# ── non-vacuity: a green run must mean something was scanned ────────────────
def test_the_scan_covered_a_substantial_number_of_lines(bandit_report: dict):
    lines = bandit_report["metrics"]["_totals"]["loc"]
    assert lines >= MIN_LINES_SCANNED, (
        f"only {lines} lines scanned (floor {MIN_LINES_SCANNED}) — the scan is not "
        f"covering the tree, so a clean result proves nothing"
    )


def test_the_scan_covered_a_substantial_number_of_files(bandit_report: dict):
    scanned = [name for name in bandit_report["metrics"] if name != "_totals"]
    assert len(scanned) >= MIN_FILES_SCANNED, (
        f"only {len(scanned)} files scanned (floor {MIN_FILES_SCANNED})"
    )


def test_no_file_was_skipped(bandit_report: dict):
    """A syntax error or read failure would silently remove a file from the gate."""
    assert bandit_report.get("errors", []) == []


def test_both_gate_targets_were_actually_scanned(bandit_report: dict):
    """`core` and `tools` must each contribute files, not just one of them."""
    scanned = [name for name in bandit_report["metrics"] if name != "_totals"]
    for target in GATE_TARGETS:
        matching = [
            name for name in scanned
            if Path(name).as_posix().startswith(f"{target}/")
            or f"/{target}/" in Path(name).as_posix()
        ]
        assert matching, f"no files scanned under {target}/"


def test_the_scanner_still_detects_a_planted_finding(tmp_path: Path):
    """The strongest non-vacuity check: prove the scanner is not simply inert.

    Scans a deliberately vulnerable throwaway file at the SAME threshold. If this
    comes back clean, every other assertion in this module is meaningless.
    """
    if not _bandit_available():
        _skip_or_fail()

    planted = tmp_path / "planted_finding.py"
    planted.write_text(
        "import subprocess\n"
        "def run(cmd):\n"
        "    return subprocess.call(cmd, shell=True)\n",
        encoding="utf-8",
    )
    output = tmp_path / "planted.json"
    subprocess.run(
        [sys.executable, "-m", "bandit", str(planted), GATE_THRESHOLD,
         "-f", "json", "-o", str(output)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    ids = {r["test_id"] for r in report["results"]}
    assert "B602" in ids, f"the scanner missed a planted shell=True finding: {ids}"


# ── no broad exclusions narrow the scan ─────────────────────────────────────
def test_no_bandit_configuration_file_narrows_the_scan():
    """A .bandit / bandit.yaml could exclude directories or skip tests globally."""
    for candidate in (".bandit", "bandit.yaml", "bandit.yml", ".bandit.yml", ".bandit.yaml"):
        for root in (_APP_ROOT, _APP_ROOT.parent):
            assert not (root / candidate).exists(), (
                f"{candidate} in {root} can silently narrow the gate"
            )


def test_pyproject_declares_no_bandit_exclusions():
    import tomllib

    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        config = tomllib.load(fh)
    bandit_config = config.get("tool", {}).get("bandit")
    if bandit_config is None:
        return
    for narrowing in ("exclude_dirs", "exclude", "skips", "tests"):
        assert narrowing not in bandit_config, (
            f"[tool.bandit] {narrowing} narrows the release gate"
        )


def test_the_workflow_passes_no_exclusion_or_skip_flag():
    workflow = (
        _APP_ROOT.parent / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    for line in workflow.splitlines():
        if "bandit -r" not in line:
            continue
        for flag in ("--exclude", "-x ", "--skip", "-s ", "--configfile", "-c "):
            assert flag not in line, f"the gate command narrows itself: {line.strip()}"


def test_no_test_was_globally_disabled(bandit_report: dict):
    """`skipped_tests` counts precise per-line suppressions, not a global skip list."""
    totals = bandit_report["metrics"]["_totals"]
    assert totals["nosec"] == 0, (
        f"{totals['nosec']} blanket `# nosec` line(s) — a suppression must name its id"
    )


# ── suppressions are bounded and individually justified ────────────────────
def _suppression_inventory() -> dict[str, list[tuple[int, str]]]:
    """``{relative path: [(line number, comment text)]}`` for every `# nosec` line."""
    inventory: dict[str, list[tuple[int, str]]] = {}
    for target in GATE_TARGETS:
        for path in sorted((_APP_ROOT / target).rglob("*.py")):
            hits = [
                (number, line.strip())
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                )
                if "nosec" in line
            ]
            if hits:
                inventory[path.relative_to(_APP_ROOT).as_posix()] = hits
    return inventory


def test_the_suppression_inventory_matches_the_allowlist_exactly():
    """Suppressions cannot grow silently — adding one must edit this test's allowlist."""
    inventory = _suppression_inventory()
    assert set(inventory) == set(ALLOWED_SUPPRESSIONS), (
        f"suppression inventory changed.\n"
        f"  found:   {sorted(inventory)}\n"
        f"  allowed: {sorted(ALLOWED_SUPPRESSIONS)}"
    )


def test_every_suppression_names_exactly_its_allowed_bandit_ids():
    for relative_path, allowed_ids in ALLOWED_SUPPRESSIONS.items():
        hits = _suppression_inventory()[relative_path]
        assert len(hits) == len(allowed_ids), (
            f"{relative_path}: expected {len(allowed_ids)} suppression(s), found {hits}"
        )
        for (number, text), expected_id in zip(hits, allowed_ids):
            assert f"nosec {expected_id}" in text, (
                f"{relative_path}:{number} does not name {expected_id}: {text!r}"
            )


def test_no_blanket_suppression_exists_anywhere():
    """A bare `# nosec` disables every test on its line."""
    import re

    for relative_path, hits in _suppression_inventory().items():
        for number, text in hits:
            assert re.search(r"#\s*nosec\s+B\d{3}", text), (
                f"{relative_path}:{number} is a blanket suppression: {text!r}"
            )


def test_the_number_of_suppressions_is_bounded():
    """A hard ceiling, so "just one more" cannot become fifty."""
    total = sum(len(hits) for hits in _suppression_inventory().values())
    assert total <= 4, f"{total} suppressions in the scanned tree — justify or remove"


def test_bandit_counted_the_same_suppressions_we_allow(bandit_report: dict):
    """Cross-check: our text scan and Bandit's own accounting must agree.

    If they disagree, one of them is wrong about what is being suppressed — which is
    exactly the condition an allowlist is supposed to make impossible.
    """
    expected = sum(len(ids) for ids in ALLOWED_SUPPRESSIONS.values())
    assert bandit_report["metrics"]["_totals"]["skipped_tests"] == expected


# ── the skip policy is itself honest ───────────────────────────────────────
# pytest's Failed/Skipped derive from BaseException, not Exception, so these must be
# caught as BaseException — `pytest.raises(Exception)` would let them through.
def test_the_require_env_makes_a_missing_scanner_a_failure(monkeypatch):
    """Proven by driving the helper directly, not by uninstalling Bandit."""
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(BaseException) as excinfo:
        _skip_or_fail()
    assert type(excinfo.value).__name__ == "Failed", type(excinfo.value).__name__
    assert "not a passing security gate" in str(excinfo.value)


def test_without_the_require_env_a_missing_scanner_skips(monkeypatch):
    monkeypatch.delenv(REQUIRE_ENV, raising=False)
    with pytest.raises(BaseException) as excinfo:
        _skip_or_fail()
    assert type(excinfo.value).__name__ == "Skipped", type(excinfo.value).__name__


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_every_truthy_require_value_is_honoured(monkeypatch, value: str):
    monkeypatch.setenv(REQUIRE_ENV, value)
    assert _require_bandit() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_non_truthy_require_values_leave_the_skip_in_place(monkeypatch, value: str):
    monkeypatch.setenv(REQUIRE_ENV, value)
    assert _require_bandit() is False
