"""tests/test_version_v69_m611.py — V69 M61.1: ONE canonical version, verified.

Before M61 nothing checked the release identity, so ``pyproject.toml`` sat at
``63.0.0`` through six milestones and four release documents kept claiming their work
was unmerged after it had landed on master. These tests are what makes that class of
drift a build failure instead of a discovery.

They assert the *mechanism* (pyproject derives, nothing re-states a literal) as well as
the *content* (docs agree with the canonical constants), so a future milestone bump is
one edit in ``core/version.py``.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from core import release_check, version

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent


# ── the canonical source itself ──────────────────────────────────────────────
def test_version_string_is_derived_from_the_three_constants():
    assert version.VERSION == f"{version.GENERATION}.{version.MILESTONE}.{version.PATCH}"
    assert version.__version__ == version.VERSION


def test_canonical_version_is_v69_m61():
    assert version.GENERATION == 69
    assert version.MILESTONE == 61
    assert version.version_tuple() == (69, 61, 0)
    assert version.milestone_tag() == "V69 M61"


def test_version_is_pep440_parseable():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.VERSION)


def test_release_identity_is_bounded_and_content_free():
    identity = version.release_identity()
    assert identity["version"] == version.VERSION
    assert identity["milestone_tag"] == "V69 M61"
    # M61 is explicitly a stabilization release — it adds no capability subsystem.
    assert identity["release_kind"] == "stabilization"
    # Every value must be a primitive: a diagnostics bundle embeds this verbatim.
    assert all(isinstance(v, (str, int)) for v in identity.values())


def test_version_module_imports_nothing_from_core():
    """It must be importable before dependencies exist (build backends import it)."""
    source = (_APP_ROOT / "core" / "version.py").read_text(encoding="utf-8")
    assert "from core" not in source
    assert "import core" not in source


# ── pyproject derives, it does not re-state ──────────────────────────────────
def _pyproject() -> dict:
    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_pyproject_declares_version_dynamic():
    project = _pyproject()["project"]
    assert "version" not in project, "pyproject must not hard-code a version literal"
    assert "version" in project.get("dynamic", [])


def test_pyproject_dynamic_version_points_at_the_canonical_attribute():
    attr = _pyproject()["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "core.version.VERSION"


def test_no_second_version_literal_in_packaging_or_scripts():
    """No file may declare a rival current-version string."""
    stale = re.compile(r"^\s*(?:version|__version__)\s*=\s*['\"]\d+\.\d+")
    offenders = []
    for path in [_APP_ROOT / "pyproject.toml", *(_APP_ROOT / "scripts").glob("*.py")]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if stale.match(line):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, f"rival version literals: {offenders}"


# ── documentation agrees with the canonical source ───────────────────────────
def test_release_truth_audit_passes():
    result = release_check.audit()
    assert result["ok"], "release-truth problems:\n  " + "\n  ".join(result["problems"])
    assert result["canonical_version"] == version.VERSION


@pytest.mark.parametrize("family", ["version", "models", "claims", "status", "install"])
def test_each_release_truth_family_is_clean(family):
    assert release_check.audit()["families"][family] == 0


def test_current_docs_name_the_current_release():
    for doc in (_REPO_ROOT / "README.md", _APP_ROOT / "README.md"):
        text = doc.read_text(encoding="utf-8")
        assert version.MILESTONE_TAG in text, f"{doc.name} does not state V69 M61"


def test_jarvis_md_header_states_the_current_role_models():
    header = release_check.jarvis_md_header()
    for role in ("FAST", "DEEP", "EMBEDDING"):
        assert release_check.CURRENT_ROLE_MODELS[role] in header, \
            f"JARVIS.md header does not name the {role} role model"


def test_no_current_doc_claims_autonomy():
    assert release_check.check_claims() == []


def test_no_merged_milestone_is_documented_as_unmerged():
    assert release_check.check_release_status() == []


def test_install_commands_in_readmes_resolve_to_real_files():
    assert release_check.check_install_commands() == []


# ── the checker must actually be able to fail ────────────────────────────────
def test_claim_detector_rejects_an_autonomy_claim(monkeypatch):
    monkeypatch.setattr(
        release_check, "_current_texts",
        lambda: {"fake.md": "JARVIS attacks autonomously across the lab."})
    problems = release_check.check_claims()
    assert len(problems) == 1 and "operator-gated" in problems[0]


def test_model_detector_rejects_the_superseded_family(monkeypatch):
    monkeypatch.setattr(
        release_check, "_current_texts",
        lambda: {"fake.md": "Reasons via local Ollama (qwen2.5 7B/14B)"})
    assert len(release_check.check_models()) == 1


def test_model_detector_accepts_the_current_coder_model(monkeypatch):
    """`qwen2.5-coder` is still the live CODER role — it must not be flagged."""
    monkeypatch.setattr(
        release_check, "_current_texts",
        lambda: {"fake.md": "CODER role resolves qwen2.5-coder:latest"})
    assert release_check.check_models() == []


# ── the status detector: negative controls ───────────────────────────────────
# The first implementation read milestone numbers out of the document BODY as well
# as the filename. A "corrected in V69 M61.1" note inside the M60 document then made
# it look like an M61 document, exempting it — the checker silently stopped detecting
# exactly the drift it exists for. These controls are why that was caught.
@pytest.mark.parametrize("doc,shipped_marker", [
    ("V69_M60_CRASH_SAFE_SESSION_CONTINUITY.md", "**merged to `master`** as `df35289`"),
    ("V69_M58_PROMPT_PREFIX_PARITY.md", "**merged to `master`** as `8c0a390`"),
    ("V69_M55_NATIVE_FAST_PATH.md", "**merged to `master`** as `d101a83`"),
])
def test_status_detector_catches_a_shipped_milestone_claiming_unmerged(
        doc, shipped_marker, tmp_path, monkeypatch):
    """Plant the stale claim back and require the checker to find it."""
    source = _APP_ROOT / "docs" / doc
    original = source.read_text(encoding="utf-8")
    assert shipped_marker in original, f"{doc} no longer records its merge commit"

    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()
    (fake_docs / doc).write_text(
        original.replace(shipped_marker, "NOT merged"), encoding="utf-8")
    monkeypatch.setattr(release_check, "_APP_ROOT", tmp_path)

    problems = release_check.check_release_status()
    assert len(problems) == 1
    assert doc in problems[0]
    assert "already shipped" in problems[0]


def test_status_detector_exempts_the_current_in_flight_milestone(tmp_path, monkeypatch):
    """M61's own document legitimately says it is awaiting review."""
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()
    (fake_docs / "V69_M61_PRODUCTION_STABILIZATION.md").write_text(
        "# JARVIS V69 M61\n\n**Status:** complete, pushed. Not merged automatically.\n",
        encoding="utf-8")
    monkeypatch.setattr(release_check, "_APP_ROOT", tmp_path)
    assert release_check.check_release_status() == []


def test_status_detector_exempts_a_future_milestone(tmp_path, monkeypatch):
    fake_docs = tmp_path / "docs"
    fake_docs.mkdir()
    (fake_docs / "V69_M62_SOMETHING.md").write_text(
        "# JARVIS V69 M62\n\n**Status:** NOT merged\n", encoding="utf-8")
    monkeypatch.setattr(release_check, "_APP_ROOT", tmp_path)
    assert release_check.check_release_status() == []
