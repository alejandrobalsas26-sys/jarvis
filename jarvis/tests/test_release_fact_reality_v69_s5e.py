"""tests/test_release_fact_reality_v69_s5e.py — V69 S5E: facts must resolve to reality.

WHY THIS FILE EXISTS
====================
``scripts/check_release_consistency.py`` is a good checker of one thing and was mistaken
for a checker of another. It verifies that the release DOCUMENTS quote the constants in
``core/release_facts.py``. It does not verify that either matches the repository. S5E
measured that gap by mutating a clone and re-running the checker:

    BANDIT_LOW_BASELINE 488 -> 100000  (the ceiling silently removed)   -> PASS
    delete docs/releases/<version>.md entirely                          -> PASS
    stale test count, in facts AND in every document that quotes it     -> PASS

The first two are fixed here and in ``core/release_check.py``. The third is not a defect
but a misreading, and the constants now say so: the deterministic counts describe the
v69.61.0 release commit, not ``HEAD``.

Two claims in ``release_facts.py`` were prose with nothing behind them:

    "the deterministic test counts — tests.test_release_closure_v69_m618 re-measures
     them with ``--collect-only`` arithmetic"       — that module contains no such code.
    "the merge state — cross-checked against git"   — nothing git-checked it; the only
     consumer asserted the value appears in the documents, which is the same
     declaration on both sides of the comparison.

The first was corrected in the docstring. The second is made TRUE below, by actually
asking git.

SCOPE
=====
These tests assert what is checkable from a checkout. They do NOT assert that CI passed
remotely, and they do not re-run the suite to re-derive a pass count: that is a
measurement, it belongs in a run, and inventing an in-suite number that the suite itself
would have to agree with is how circular facts get made in the first place.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import release_facts as rf

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent
_GENERATOR = _APP_ROOT / "scripts" / "regenerate_release_facts.py"
_FACTS = _APP_ROOT / "core" / "release_facts.py"

#: Bandit over the whole runtime is not fast. One scan, reused by every test here.
_SCAN_TIMEOUT_S = 1800.0


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(_REPO_ROOT), capture_output=True,
                          text=True, timeout=120)


def _in_a_checkout() -> bool:
    return shutil.which("git") is not None and (_REPO_ROOT / ".git").exists()


requires_git = pytest.mark.skipif(not _in_a_checkout(),
                                  reason="not a git checkout (sdist or exported tree)")


# ── the merge-state claim, actually asked of git ────────────────────────────

@requires_git
def test_the_declared_merge_commit_exists_in_this_repository():
    """"Cross-checked against git" used to mean "quoted in a document"."""
    commit = rf.RELEASE_MERGE_COMMIT
    proc = _git("cat-file", "-e", f"{commit}^{{commit}}")
    assert proc.returncode == 0, (
        f"core/release_facts.py declares RELEASE_MERGE_COMMIT={commit!r}, which is not "
        f"a commit in this repository")


@requires_git
def test_the_declared_merge_commit_is_an_ancestor_of_master():
    """A merge commit for a `merged` release that master does not contain is not one."""
    if rf.RELEASE_STATE == "in_flight":
        pytest.skip("the release is in_flight; there is no merge commit to verify yet")
    if _git("rev-parse", "--verify", "master").returncode != 0:
        pytest.skip("no local master ref (a shallow or single-branch checkout)")
    proc = _git("merge-base", "--is-ancestor", rf.RELEASE_MERGE_COMMIT, "master")
    assert proc.returncode == 0, (
        f"{rf.RELEASE_MERGE_COMMIT} is not an ancestor of master, but "
        f"RELEASE_STATE={rf.RELEASE_STATE!r} claims the work is merged")


# ── the measured facts have a generator, and it is a fixpoint ───────────────

def test_the_measured_facts_have_a_canonical_generator():
    assert _GENERATOR.is_file(), (
        "the Bandit facts are enforced against the current tree but nothing regenerates "
        "them; a derived fact with no generator is a fact that rots")


def test_the_generator_refuses_to_touch_the_approved_ceiling():
    """A generator that could raise its own ceiling could hide a security regression.

    ``BANDIT_LOW_BASELINE`` is a human decision. Asserted over the source rather than by
    running it, because the interesting case is the one where a future edit ADDS it to
    the derived set — that would pass a behavioural test on a tree where the measured
    and approved values happen to be equal, which is exactly today's tree.
    """
    source = _GENERATOR.read_text(encoding="utf-8")
    derived = source.split("_DERIVED", 1)[1].split("}", 1)[0]
    assert "BANDIT_LOW_BASELINE" not in derived, (
        "regenerate_release_facts.py lists the approved ceiling among the facts it "
        "rewrites; raising a security baseline must stay a reviewed decision")


@pytest.mark.slow
def test_the_generator_reports_the_facts_as_current():
    """The facts in the tree are the facts the scanner reports. This is the gate that
    would have caught the Low baseline drifting 488 -> 489 for five weeks."""
    proc = subprocess.run([sys.executable, str(_GENERATOR), "--check"],
                          cwd=str(_APP_ROOT), capture_output=True, text=True,
                          timeout=_SCAN_TIMEOUT_S)
    if proc.returncode == 2:
        pytest.skip("bandit is not installed (dev profile)")
    assert proc.returncode == 0, (
        f"the declared release facts no longer match the tree:\n{proc.stdout}")


@pytest.mark.slow
def test_regeneration_is_idempotent():
    """Generated truth must be deterministic for the same repository state.

    Runs the generator against a COPY of the facts file, twice, and requires the second
    run to produce no further change. Nothing in the real tree is written: a test that
    regenerates the repository to prove regeneration works would be its own evidence.
    """
    proc = subprocess.run([sys.executable, str(_GENERATOR), "--check"],
                          cwd=str(_APP_ROOT), capture_output=True, text=True,
                          timeout=_SCAN_TIMEOUT_S)
    if proc.returncode == 2:
        pytest.skip("bandit is not installed (dev profile)")

    from scripts import regenerate_release_facts as gen

    measured = gen.measure()
    assert measured is not None, "the scanner ran for --check but not here"

    original = _FACTS.read_text(encoding="utf-8")
    once, first_changes = gen.rewrite(original, measured)
    twice, second_changes = gen.rewrite(once, measured)
    assert second_changes == [], (
        f"regenerating twice produced a second diff, so the generator is not a "
        f"fixpoint: {second_changes}")
    assert twice == once
    assert first_changes == [], (
        f"the committed facts are stale; run the generator: {first_changes}")


# ── the consistency checker is not vacuous ─────────────────────────────────

def test_a_declared_release_document_that_is_missing_is_a_problem():
    """The mutation "delete the release notes" used to be accepted as PASS.

    ``_release_texts`` skips a file that is not there, so deleting the document did not
    fail the checker — it emptied the `counts` and `security` families, which then
    reported no problems over no documents.
    """
    from core import release_check

    assert hasattr(release_check, "check_release_documents_exist"), \
        "the missing-document hole is open again"
    assert release_check.check_release_documents_exist() == [], \
        "a declared release document is missing from this checkout"

    # Non-vacuity: the check must actually fire when a document is absent.
    real = release_check._RELEASE_DOCS
    try:
        release_check._RELEASE_DOCS = (*real, _REPO_ROOT / "docs" / "no-such-file.md")
        assert release_check.check_release_documents_exist(), \
            "check_release_documents_exist accepted a document that does not exist"
    finally:
        release_check._RELEASE_DOCS = real


def test_the_low_ceiling_cannot_be_removed_without_the_scan_noticing():
    """The ceiling is only a control while something compares it to a real scan.

    ``check_release_consistency.py`` never did: raising BANDIT_LOW_BASELINE to 100000
    passed it. What makes the ceiling real is the live scan in
    ``test_bandit_low_baseline_v69_m618.py``, so this asserts that scan still exists and
    is wired to this constant rather than to a literal of its own.
    """
    owner = _APP_ROOT / "tests" / "test_bandit_low_baseline_v69_m618.py"
    source = owner.read_text(encoding="utf-8")
    assert "bandit_low_within_baseline" in source
    assert "-f" in source and "json" in source, \
        "the Low baseline is no longer checked against a real Bandit report"
    assert str(rf.BANDIT_LOW_BASELINE) not in source, \
        "the owning test hardcodes the baseline instead of importing the declaration"


def test_the_declared_observation_is_not_below_the_ceiling_it_is_checked_against():
    assert rf.BANDIT_LOW_OBSERVED <= rf.BANDIT_LOW_BASELINE
    assert rf.BANDIT_MEDIUM == 0 and rf.BANDIT_HIGH == 0, \
        "the blocking severities are no longer declared at zero"


def test_the_release_facts_docstring_does_not_claim_evidence_it_lacks():
    """It claimed `test_release_closure_v69_m618` re-measures the counts with
    ``--collect-only``. It does not, and never did."""
    closure = (_APP_ROOT / "tests" / "test_release_closure_v69_m618.py").read_text(
        encoding="utf-8")
    facts = _FACTS.read_text(encoding="utf-8")
    if "--collect-only" not in closure:
        assert "re-measures them with ``--collect-only`` arithmetic" not in facts, (
            "core/release_facts.py still credits test_release_closure_v69_m618 with a "
            "--collect-only re-measurement that module does not contain")


def test_the_historical_counts_are_scoped_to_the_release_they_describe():
    """4128 is the v69.61.0 measurement, not a claim about HEAD. Whoever deletes that
    scoping should have to delete this test too."""
    facts = _FACTS.read_text(encoding="utf-8")
    assert "DESCRIBE THE v69.61.0 RELEASE COMMIT, NOT ``HEAD``" in facts, \
        "the deterministic counts lost the scoping that makes them honest"
    assert rf.DETERMINISTIC_TESTS_FAILED == 0
