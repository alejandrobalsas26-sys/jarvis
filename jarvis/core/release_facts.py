"""core/release_facts.py — V69 M61.8: the validated release facts, declared once.

WHY THIS EXISTS
---------------
M61.1 gave the repository ONE canonical *version* (:mod:`core.version`) and a scanner
that keeps the documentation agreeing with it. It did not give the repository one
canonical set of *validated numbers*, and the drift showed immediately: the M61 report
and the changelog both state ``3481 passed / 18 skipped`` — the result of the RC that
produced them — while the merged tree measures a different total. A release claim that
no longer matches the tree it describes is exactly the class of defect M61 exists to
remove, so M61.8 gives those numbers a single home too.

Everything here is a **declared fact about the current release**, and every one of them
is checked against reality by a test rather than trusted:

  * the deterministic test counts        — :mod:`tests.test_release_closure_v69_m618`
    re-measures them with ``--collect-only`` arithmetic and the release-evidence
    generator re-runs both supported layouts;
  * the Bandit numbers                   — re-run of the exact CI gate
    (``tests/test_bandit_gate_v69_m617.py``, plus the Low baseline check here);
  * the qualification verdict / warnings  — cross-checked against the shape
    ``core.qualification.release_verdict`` can actually produce;
  * the merge state                       — cross-checked against git when git is
    available, and honestly skipped when it is not.

DISCIPLINE
----------
  * pure stdlib, no imports from the rest of ``core`` except :mod:`core.version`;
  * no I/O, no clock, no environment read — build-time constants only, so the same
    values appear in an evidence bundle produced on any host;
  * content-free: nothing here can carry a prompt, a secret or a private path;
  * **honest by construction** — ``live_not_requested`` stays a warning, and the
    verdict recorded is ``PASS_WITH_WARNINGS``, not ``PASS``.

M61.8 IS NOT A VERSION
----------------------
``69.61.0`` is the canonical package version and M61.8 does not change it. M61.8 is the
internal *closure stage* of that release: documentation truth, reproducible evidence,
checksums, SBOM, dependency policy, notes, runbook and the guards around tagging.
"""
from __future__ import annotations

from core.version import VERSION

RELEASE_FACTS_SCHEMA_VERSION = "m61.8.1"

# ── The closure stage itself ─────────────────────────────────────────────────
RELEASE_STAGE = "M61.8"
"""Internal closure stage of release :data:`core.version.VERSION`. NOT a version."""

RELEASE_STAGE_NAME = "Release Closure & Verifiable Release Evidence"

RELEASE_TAG = f"v{VERSION}"
"""The annotated tag this release WILL carry. Nothing in this repository creates it —
tagging is an operator step, documented in ``docs/RELEASE_RUNBOOK.md`` and guarded by
``scripts/check_release_tag_guard.py``."""

# ── Merge state of the M61 work ──────────────────────────────────────────────
RELEASE_STATE = "merged"
"""``in_flight`` | ``merged`` | ``published``.

``merged``   — the milestone branch is merged to ``master``; no tag, no GitHub Release.
``published``— an annotated tag and a GitHub Release exist for :data:`RELEASE_TAG`.

M61.8 deliberately stops at ``merged``: it prepares the tag and the release body and
creates neither.
"""

RELEASE_MERGE_COMMIT = "0bb1a6b"
"""Abbreviated merge commit of the M61 work on ``master``. Cross-checked against git."""

RELEASE_MERGE_SUBJECT = (
    "merge: JARVIS V69 M61 production stabilization and release engineering"
)

RELEASE_MERGED_BRANCH = "jarvis-v69-m61-production-stabilization"
"""The feature branch that was merged and then deleted remotely. Historical record."""

RELEASE_CLOSURE_BRANCH = "jarvis-v69-m61-release-closure"
"""Where M61.8 is developed. Never merged or tagged by any code in this repository."""

# ── Deterministic validation — the numbers current docs must agree with ──────
DETERMINISTIC_TESTS_PASSED = 4128
DETERMINISTIC_TESTS_SKIPPED = 33
DETERMINISTIC_TESTS_FAILED = 0

MEASUREMENT_ENVIRONMENT = "python3.12 / windows-amd64 / dev+soc+lab profiles installed"
"""Where :data:`DETERMINISTIC_TESTS_PASSED` was measured, and why it is stated.

The pass/skip split is **environment-dependent and legitimately so**: a suite whose
optional dependency is absent collapses into a single module-level skip instead of
contributing N passing tests, so a leaner environment reports fewer of both. A count
without its environment is therefore not reproducible, and quoting one as if it were is
what let the RC1 figure survive into a merged tree.

The claim that IS environment-independent — and the one the release actually rests on —
is :data:`DETERMINISTIC_TESTS_FAILED` ``== 0`` in **both** supported layouts. CI's
authoritative Python 3.11 job installs ``dev``+``soc`` and will report a different split
for the same reason; that is expected, not drift.
"""

#: The two supported invocations. Both must produce equivalent security semantics; the
#: root-layout one is AUTHORITATIVE (CI and the release qualification use it), because
#: ``tests/test_security.py`` resolves one traversal fixture relative to the CWD.
PYTEST_ROOT_LAYOUT_COMMAND = "python -m pytest -q jarvis/tests tests"
PYTEST_APP_LAYOUT_COMMAND = "python -m pytest -q tests ../tests"
PYTEST_AUTHORITATIVE_LAYOUT = "root"

# ── Static analysis ─────────────────────────────────────────────────────────
BANDIT_GATE_COMMAND = "bandit -r core tools -ll -q"
BANDIT_GATE_THRESHOLD = "medium+high blocking (-ll)"
BANDIT_MEDIUM = 0
BANDIT_HIGH = 0

BANDIT_LOW_BASELINE = 488
"""Approved ceiling for Low-severity Bandit findings.

This is a **baseline, not a claim of harmlessness**. 488 Low findings are recorded and
bounded so the number cannot grow silently; a decrease passes, an increase fails until
the baseline is deliberately raised with review. Reduction is bounded maintenance work,
not part of this release — see ``docs/DEPENDENCY_SECURITY_POLICY.md``.
"""

BANDIT_LOW_OBSERVED = 488
"""Observed at the closure commit with the same command, no exclusions."""

BANDIT_SUPPRESSION_COUNT = 2
"""Exactly two precise, id-scoped per-line Bandit suppressions, both regression-tested.

Pinned by location and id in ``tests/test_bandit_gate_v69_m617.py``. The suppression
marker itself is deliberately not spelled out in this file: that test inventories every
line in ``core/`` and ``tools/`` containing the marker, and a *description* of a
suppression must not be counted as one.
"""

BANDIT_SCANNED_LINES = 75_519
"""Non-vacuity evidence: a clean scan over an empty tree is not a clean scan."""

# ── Lint / grammar ──────────────────────────────────────────────────────────
RUFF_COMMAND = "ruff check jarvis/"
COMPILEALL_TARGETS = (
    "jarvis/core", "jarvis/tools", "jarvis/scripts", "jarvis/aura",
    "jarvis/main.py", "jarvis/__main__.py",
)

# ── Release qualification ───────────────────────────────────────────────────
QUALIFICATION_COMMAND = "python jarvis/scripts/qualify_release_m61.py --full"

MANDATORY_GATES = (
    "consistency",
    "base_import",
    "ruff",
    "compile",
    "bandit",
    "m61_deterministic",
    "regression",
    "packaging_build",
    "package_manifest",
    "soak",
)
"""The gate keys ``qualify_release_m61.py`` records and requires. Ten, all mandatory.

The harness docstring enumerates fourteen numbered *steps* (git state and the two
documentation checks fold into ``consistency``; the manifest and secret scan are one
pass over the built artifacts; the live smoke is optional). Ten is what the artifact
records and what a reader can count, so ten is what the documentation states.
"""

QUALIFICATION_VERDICT = "PASS_WITH_WARNINGS"
"""Honest verdict. Every mandatory gate is green; the sole warning is that optional
live validation was not requested. This is deliberately **not** reported as ``PASS``."""

QUALIFICATION_WARNINGS = ("live_not_requested",)

QUALIFICATION_EXIT_CODE = 0
"""``PASS_WITH_WARNINGS`` exits 0. Only ``FAIL`` exits non-zero."""

# ── Live validation ─────────────────────────────────────────────────────────
LIVE_VALIDATION_STATUS = "NOT_REQUESTED"
"""``NOT_REQUESTED`` | ``PASS`` | ``INSUFFICIENT_EVIDENCE`` | ``FAIL``.

Live validation is OPTIONAL and was not executed for this evidence set. It is never
folded into a PASS: ``--live`` on a host without Ollama yields
``INSUFFICIENT_EVIDENCE``, and omitting it yields this warning.
"""

# ── Packaging ───────────────────────────────────────────────────────────────
WHEEL_FILENAME = f"jarvis-{VERSION}-py3-none-any.whl"
SDIST_FILENAME = f"jarvis-{VERSION}.tar.gz"
CHECKSUM_FILENAME = "SHA256SUMS"
CHECKSUM_ALGORITHM = "sha256"
EVIDENCE_FILENAME = "release-evidence.json"

PACKAGING_STATUS = "PASS"
PACKAGE_SCAN_STATUS = "PASS"
SOAK_STATUS = "PASS"

# ── CI ──────────────────────────────────────────────────────────────────────
#: Actual job ids in ``.github/workflows/ci.yml`` and their display names. Required
#: status checks must be named from THIS list — an invented check name silently
#: protects nothing. Verified against the workflow by a test.
CI_MANDATORY_JOBS: dict[str, str] = {
    "consistency": "Release truth & dependency authority",
    "lint": "Ruff & compileall (3.11)",
    "tests": "Deterministic suite (3.11, authoritative)",
    "compat": "Compatibility smoke (3.12)",
    "base-install": "Base text-mode install purity",
    "packaging": "Wheel & sdist build + manifest scan",
    "security-scan": "Static security analysis (medium/high — blocking)",
}
CI_ADVISORY_JOBS: dict[str, str] = {
    "dependency-audit": "Dependency audit (advisory)",
}

# ── Supported runtimes ──────────────────────────────────────────────────────
REQUIRES_PYTHON = ">=3.11"
CI_PYTHON_VERSIONS = ("3.11", "3.12")
CI_AUTHORITATIVE_PYTHON = "3.11"

# ── Dependency authority ────────────────────────────────────────────────────
DEPENDENCY_AUTHORITY = "jarvis/requirements/<profile>.txt"
SBOM_MANDATORY_PROFILES = ("base", "dev", "soc")
SBOM_OPTIONAL_PROFILES = ("voice", "docs", "lab")

# ── Security posture claims that must stay true in the documentation ────────
#: Each entry is a posture the current-facing documentation must not contradict. The
#: scanner in :mod:`core.release_check` enforces the negative form of each.
SECURITY_POSTURE = {
    "dynamic_plugin_exec": "disabled_fail_closed",
    "network_bind_default": "loopback_first_explicit_opt_in",
    "auto_dependency_install": "off_by_default",
    "ssh_host_keys": "verified_reject_unknown",
    "cloud_backend": "opt_in_off_by_default",
    "plugin_sandbox": "does_not_exist",
}

# ── Limitations carried into the release ────────────────────────────────────
KNOWN_LIMITATIONS = (
    "live_validation_not_executed",
    "cwd_sensitive_legacy_traversal_test",
    "cwd_relative_log_modules_remain",
    "pip_audit_findings_not_triaged",
    "base_install_purity_proven_in_ci_only",
    "no_plugin_sandbox_dynamic_source_plugins_refused",
    "bandit_low_findings_baselined_not_eliminated",
)


def release_facts() -> dict:
    """Bounded, content-free snapshot of every declared release fact.

    Safe to embed verbatim in a release-evidence bundle: every value is a primitive,
    a tuple of primitives or a flat mapping of primitives.
    """
    return {
        "facts_schema_version": RELEASE_FACTS_SCHEMA_VERSION,
        "canonical_version": VERSION,
        "release_stage": RELEASE_STAGE,
        "release_stage_name": RELEASE_STAGE_NAME,
        "release_tag": RELEASE_TAG,
        "release_state": RELEASE_STATE,
        "release_merge_commit": RELEASE_MERGE_COMMIT,
        "tests_passed": DETERMINISTIC_TESTS_PASSED,
        "tests_skipped": DETERMINISTIC_TESTS_SKIPPED,
        "tests_failed": DETERMINISTIC_TESTS_FAILED,
        "bandit_medium": BANDIT_MEDIUM,
        "bandit_high": BANDIT_HIGH,
        "bandit_low_baseline": BANDIT_LOW_BASELINE,
        "bandit_low_observed": BANDIT_LOW_OBSERVED,
        "bandit_suppressions": BANDIT_SUPPRESSION_COUNT,
        "qualification_verdict": QUALIFICATION_VERDICT,
        "qualification_warnings": list(QUALIFICATION_WARNINGS),
        "mandatory_gates": list(MANDATORY_GATES),
        "live_validation_status": LIVE_VALIDATION_STATUS,
        "known_limitations": list(KNOWN_LIMITATIONS),
        "security_posture": dict(SECURITY_POSTURE),
    }


def bandit_low_within_baseline(observed: int) -> tuple[bool, str]:
    """``(ok, reason)`` for an observed Low count against :data:`BANDIT_LOW_BASELINE`.

    A DECREASE passes — the baseline is a ceiling, not a fixed point. An increase fails
    until the finding is triaged and the baseline deliberately raised.
    """
    if not isinstance(observed, int) or observed < 0:
        return False, f"observed Low count is not a non-negative integer: {observed!r}"
    if observed > BANDIT_LOW_BASELINE:
        return False, (
            f"Bandit Low findings grew to {observed}, above the approved baseline "
            f"{BANDIT_LOW_BASELINE}. Triage the new findings and raise the baseline "
            f"deliberately in core/release_facts.py, or fix them."
        )
    return True, (
        f"{observed} Low finding(s) <= approved baseline {BANDIT_LOW_BASELINE}"
    )
