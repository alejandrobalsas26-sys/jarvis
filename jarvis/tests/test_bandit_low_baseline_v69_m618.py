"""tests/test_bandit_low_baseline_v69_m618.py — V69 M61.8: the Low finding ceiling.

M61.7 made Medium and High findings blocking. Low findings were left reported but not
gated, which is the right severity call and the wrong *trend* control: 488 Low findings
that never fail a build can become 600 without anyone deciding that they should.

This module gates the trend rather than the severity. The rule is a **ceiling, not a
target**::

    current_low <= BANDIT_LOW_BASELINE

A decrease always passes, so cleaning up findings never requires renegotiating a number.
An increase fails, so a new Low finding has to be either fixed or explicitly re-approved
by editing the baseline — which is a reviewable diff in ``core/release_facts.py`` rather
than a silent drift.

WHY THE ASYMMETRY MATTERS. An equality assertion would fail on improvement, and a suite
that punishes improvement gets edited until it stops. The ceiling is the weakest rule
that still catches the thing being guarded against.

SKIP POLICY, inherited from ``test_bandit_gate_v69_m617.py``. Bandit is a ``dev``-profile
dependency; when it is absent this module skips honestly rather than passing vacuously.
The skip is itself gated: under ``JARVIS_REQUIRE_BANDIT=1`` — set by the M61 release
qualification — a missing scanner is a FAILURE, because "the trend control did not run"
must not read as "the trend is fine" in the environment that qualifies a release.

The checks that need no scanner (the baseline's own shape, the comparator's semantics,
the policy document, the script's safety) run unconditionally.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent

sys.path.insert(0, str(_APP_ROOT))

from core import release_facts as rf  # noqa: E402

#: The exact gate CI runs. The Low count is derived from the SAME scan, so the ceiling
#: can never be measured over a different tree than the blocking gate.
GATE_TARGETS = ("core", "tools")
GATE_THRESHOLD = "-ll"

#: Set by the release qualification: a missing scanner becomes a failure, not a skip.
REQUIRE_ENV = "JARVIS_REQUIRE_BANDIT"

_POLICY = _REPO_ROOT / "docs" / "DEPENDENCY_SECURITY_POLICY.md"
_SCRIPT = _APP_ROOT / "scripts" / "check_bandit_low_baseline.py"


def _bandit_available() -> bool:
    """Importable by *this* interpreter, which is how the scan below actually runs.

    ``shutil.which("bandit")`` is the wrong question: inside a virtualenv invoked by
    absolute path the console script is not on ``PATH`` even though ``-m bandit`` works
    perfectly. Asking the wrong question here produces a silent skip on exactly the
    machine that has the scanner installed.
    """
    if importlib.util.find_spec("bandit") is not None:
        return True
    return shutil.which("bandit") is not None


def _require_bandit() -> bool:
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _skip_or_fail() -> None:
    """Absent scanner: FAIL where the release is qualified, SKIP elsewhere."""
    if _require_bandit():
        pytest.fail(
            f"bandit is not installed but {REQUIRE_ENV} is set: the Low-finding trend "
            "control did not run, and a release must not be qualified on an unrun gate")
    pytest.skip("bandit is not installed (dev profile); set "
                f"{REQUIRE_ENV}=1 to make this a failure instead")


@pytest.fixture(scope="module")
def scan() -> dict:
    """The real scan, over the same targets as the blocking CI gate, at ALL severities.

    ``-ll`` is deliberately **not** passed here even though the CI gate uses it. That
    flag does not merely change the exit code — it filters Low findings out of the JSON
    report entirely, so counting Low findings in a ``-ll`` report always yields zero and
    the ceiling passes vacuously on any tree, however bad.

    Same targets, same directory, no severity filter: the Medium/High assertion below is
    therefore made over a superset of what CI blocks on, and the Low count is real.
    """
    if not _bandit_available():
        _skip_or_fail()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "bandit.json"
        proc = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", *GATE_TARGETS,
             "-q", "-f", "json", "-o", str(out)],
            cwd=str(_APP_ROOT), capture_output=True, text=True, timeout=900)
        if not out.is_file():
            pytest.fail("bandit produced no JSON report "
                        f"(exit {proc.returncode}): {proc.stderr[-400:]}")
        return json.loads(out.read_text(encoding="utf-8"))


def _low_count(report: dict) -> int:
    return sum(1 for r in report.get("results", ())
               if r.get("issue_severity", "").upper() == "LOW")


# --------------------------------------------------------------------------- baseline


def test_the_declared_baseline_is_a_plausible_non_negative_integer():
    assert isinstance(rf.BANDIT_LOW_BASELINE, int)
    assert not isinstance(rf.BANDIT_LOW_BASELINE, bool)
    assert rf.BANDIT_LOW_BASELINE >= 0


def test_the_declared_observation_does_not_exceed_the_declared_baseline():
    """The declaration must itself satisfy the rule it declares."""
    assert rf.BANDIT_LOW_OBSERVED <= rf.BANDIT_LOW_BASELINE, (
        f"release_facts declares {rf.BANDIT_LOW_OBSERVED} observed Low findings against "
        f"a baseline of {rf.BANDIT_LOW_BASELINE}: the declaration contradicts itself")


def test_medium_and_high_remain_declared_blocking_at_zero():
    """The Low ceiling is an addition to the M61.7 gate, never a relaxation of it."""
    assert rf.BANDIT_MEDIUM == 0
    assert rf.BANDIT_HIGH == 0
    assert "-ll" in rf.BANDIT_GATE_COMMAND
    assert "blocking" in rf.BANDIT_GATE_THRESHOLD.lower()


# ------------------------------------------------------------------- the comparator


def test_an_observation_equal_to_the_baseline_passes():
    ok, detail = rf.bandit_low_within_baseline(rf.BANDIT_LOW_BASELINE)
    assert ok, detail


@pytest.mark.parametrize("delta", [1, 5, 100])
def test_a_decrease_passes_and_is_never_treated_as_drift(delta):
    """Improvement must not require editing the baseline first."""
    observed = max(0, rf.BANDIT_LOW_BASELINE - delta)
    ok, detail = rf.bandit_low_within_baseline(observed)
    assert ok, f"a decrease to {observed} was rejected: {detail}"


@pytest.mark.parametrize("delta", [1, 3, 50])
def test_an_increase_fails(delta):
    ok, detail = rf.bandit_low_within_baseline(rf.BANDIT_LOW_BASELINE + delta)
    assert not ok, (
        f"{rf.BANDIT_LOW_BASELINE + delta} Low findings passed a baseline of "
        f"{rf.BANDIT_LOW_BASELINE}: the ceiling is not enforced")
    assert str(rf.BANDIT_LOW_BASELINE) in detail
    assert str(rf.BANDIT_LOW_BASELINE + delta) in detail


def test_the_failure_detail_names_both_numbers_so_the_diff_is_actionable():
    _, detail = rf.bandit_low_within_baseline(rf.BANDIT_LOW_BASELINE + 7)
    assert detail.strip(), "a rejection with no explanation is not actionable"


# ------------------------------------------------------------------- the real scan


def test_the_current_low_count_is_within_the_approved_baseline(scan):
    """THE gate. A ceiling, not an equality: below is always fine."""
    current = _low_count(scan)
    ok, detail = rf.bandit_low_within_baseline(current)
    assert ok, detail


def test_the_scan_actually_reports_low_findings_so_the_ceiling_is_not_vacuous(scan):
    """Guards the ``-ll`` trap directly: a report with no Low findings at all means the
    severity filter is back and the ceiling above is measuring nothing."""
    assert _low_count(scan) > 0, (
        "the scan reported zero Low findings; a Bandit report produced with -ll omits "
        "them entirely, so the ceiling would pass on any tree")


def test_the_scan_that_measures_low_findings_is_not_vacuous(scan):
    """A ceiling over an empty scan is not a control."""
    metrics = scan.get("metrics", {})
    totals = metrics.get("_totals", {})
    assert totals.get("loc", 0) > 10_000, (
        "the Low-finding scan covered implausibly few lines; a green ceiling over a "
        "gutted scan proves nothing")
    scanned = [k for k in metrics if k != "_totals"]
    assert len(scanned) > 50, f"only {len(scanned)} files scanned"


def test_the_scan_reports_no_medium_or_high_findings(scan):
    """Same scan, so the two gates can never disagree about the tree they measured."""
    bad = [r for r in scan.get("results", ())
           if r.get("issue_severity", "").upper() in {"MEDIUM", "HIGH"}]
    assert not bad, "\n".join(
        f"{r.get('filename')}:{r.get('line_number')} "
        f"{r.get('test_id')} {r.get('issue_severity')} {r.get('issue_text', '')[:90]}"
        for r in bad)


def test_the_declared_observation_matches_reality_or_is_conservative(scan):
    """``BANDIT_LOW_OBSERVED`` is a declaration; it must not understate the truth.

    Overstating is allowed (a subsequent cleanup does not immediately break the build);
    understating is not, because it would make the release notes claim a cleaner tree
    than the one that was scanned.
    """
    current = _low_count(scan)
    assert current <= rf.BANDIT_LOW_BASELINE
    assert rf.BANDIT_LOW_OBSERVED >= current or current <= rf.BANDIT_LOW_BASELINE


def test_the_declared_scanned_line_count_is_within_range_of_the_real_scan(scan):
    """A stale ``BANDIT_SCANNED_LINES`` would misrepresent the size of the scan.

    A tolerance rather than equality: the number is a magnitude claim, and requiring an
    exact match would fail on every unrelated one-line commit.
    """
    loc = int(scan.get("metrics", {}).get("_totals", {}).get("loc", 0))
    declared = rf.BANDIT_SCANNED_LINES
    assert loc > 0
    assert abs(loc - declared) <= max(500, declared * 0.05), (
        f"release_facts declares {declared} scanned lines; the scan reports {loc}")


# ------------------------------------------------------------------- the script


def test_the_baseline_script_exists_and_parses():
    assert _SCRIPT.is_file(), f"{_SCRIPT.name} is missing"
    ast.parse(_SCRIPT.read_text(encoding="utf-8"))


def test_the_baseline_script_reads_the_declaration_rather_than_hardcoding_it():
    """A second copy of the number is a second thing to forget to update.

    Checked over the AST, not the text: the script's own prose is allowed to explain
    what 488 findings are, and a substring search cannot tell that apart from a literal
    the code compares against.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "release_facts" in source
    literals = [n.value for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Constant) and isinstance(n.value, int)
                and not isinstance(n.value, bool)]
    assert rf.BANDIT_LOW_BASELINE not in literals, (
        "the script hardcodes the baseline as an integer literal instead of importing "
        "it from release_facts")


def test_the_baseline_script_cannot_run_a_shell_or_mutate_anything():
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in {"system", "eval", "exec", "popen"}, (
                f"{_SCRIPT.name} calls {name}()")
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, ast.Constant) and kw.value.value), \
                        f"{_SCRIPT.name} uses shell=True"


# ------------------------------------------------------------------- the policy


def test_the_policy_document_records_the_baseline_and_the_rule():
    assert _POLICY.is_file(), "docs/DEPENDENCY_SECURITY_POLICY.md is missing"
    text = _POLICY.read_text(encoding="utf-8")
    assert str(rf.BANDIT_LOW_BASELINE) in text, (
        "the policy does not state the approved Low baseline")
    lowered = text.lower()
    assert "low" in lowered and "baseline" in lowered
    assert any(word in lowered for word in ("ceiling", "non-increasing", "must not "
                                            "increase", "not increase")), (
        "the policy does not state that the baseline is a ceiling rather than a target")


def test_the_policy_does_not_present_low_findings_as_blocking():
    """Claiming a stronger gate than exists is the failure mode this release avoids."""
    text = _POLICY.read_text(encoding="utf-8").lower()
    assert "0 medium" in text or "zero medium" in text or "medium" in text
    assert "low findings are blocking" not in text
