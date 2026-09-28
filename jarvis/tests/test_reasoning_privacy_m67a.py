"""V69 M67A — the private-corpus boundary (§4) and meta-noise (§13).

WHAT THESE TESTS DEFEND
-----------------------
§4 draws one line: raw historical conversations stay LOCAL and OUTSIDE Git, no private
conversation content is committed, and **every record carries an explicit privacy/export status**.
The third clause is the one with teeth, because the first two are satisfiable by accident and the
third is not: a record nobody classified must not read as a clean one.

The hardest case in this file is
:func:`test_a_secret_never_reaches_a_derived_record_through_a_lifted_span`. Extraction is
subtractive — every populated field is a span lifted verbatim from a source turn — so a
conversation containing an API key will produce a record containing that API key unless something
stops it. Measured during construction: it did, and the write gate refused the whole corpus as a
result. The fix moved the exclusion to the point of lifting, and this test pins it there.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
PACKAGE_ROOT = _HERE.parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from training_gym.schemas import SensitivityClass  # noqa: E402
from reasoning_distillation import privacy  # noqa: E402
from reasoning_distillation.config import PrivacyConfig  # noqa: E402
from reasoning_distillation.models import ExportStatus, PrivacyAssessment  # noqa: E402
from reasoning_distillation.privacy import (  # noqa: E402
    PrivacyCategory,
    PrivacyError,
    PrivacyFinding,
    assert_body_free_payload,
    body_free_problems,
    classify,
    meta_noise_markers,
    redacted_preview,
    scan,
)
from reasoning_distillation.storage import (  # noqa: E402
    BODY_FREE_AREAS,
    RECORD_AREAS,
    CorpusRoot,
    StorageError,
    assert_root_is_gitignored,
    store_raw,
    write_report,
)
from test_reasoning_distillation_m67a import (  # noqa: E402
    conversation_doc,
    distil_one,
    ingest,
    synthetic_corpus,
)

SECRET = "sk-abcdefghijklmnopqrstuvwxyz1234"
HOME_PATH = "/home/kali/logs/app.log"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  explicit status, and the fail-closed default (§4)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_an_unclassified_record_is_export_unknown_and_blocks():
    from reasoning_distillation.models import UNCLASSIFIED
    assert UNCLASSIFIED.export_status is ExportStatus.EXPORT_UNKNOWN
    assert UNCLASSIFIED.export_status.permits_export is False


def test_only_export_safe_permits_export():
    permitting = [s for s in ExportStatus if s.permits_export]
    assert permitting == [ExportStatus.EXPORT_SAFE]


def test_a_clean_scan_of_internal_material_is_still_not_export_safe():
    """§4: do NOT silently classify derived content as anonymous.

    Absence of evidence of a secret is not a declaration that material is shareable. EXPORT_SAFE
    requires a caller to declare SYNTHETIC or LAB_FIXTURE explicitly.
    """
    assessment = classify("A perfectly ordinary sentence about sorting algorithms.")
    assert assessment.contains_personal_data is False
    assert assessment.contains_secret_like_data is False
    assert assessment.export_status is ExportStatus.EXPORT_BLOCKED


def test_a_clean_scan_of_synthetic_material_is_export_safe():
    assessment = classify("A perfectly ordinary sentence about sorting algorithms.",
                          sensitivity=SensitivityClass.SYNTHETIC)
    assert assessment.export_status is ExportStatus.EXPORT_SAFE


def test_a_restricted_class_overrules_a_clean_scan():
    assessment = classify("Nothing sensitive here at all.",
                          sensitivity=SensitivityClass.RESTRICTED)
    assert assessment.export_status is ExportStatus.EXPORT_BLOCKED


def test_export_safe_cannot_coexist_with_a_positive_finding():
    with pytest.raises(Exception, match="contradictory"):
        PrivacyAssessment(contains_personal_data=True, contains_secret_like_data=False,
                          export_status=ExportStatus.EXPORT_SAFE).validated()


def test_export_safe_cannot_be_claimed_when_the_scanner_did_not_run():
    """An absent scanner proves nothing, so it must not read as clean."""
    with pytest.raises(Exception, match="scanner was\\s+unavailable|scanner was unavailable"):
        PrivacyAssessment(contains_personal_data=False, contains_secret_like_data=False,
                          export_status=ExportStatus.EXPORT_SAFE,
                          scanner_available=False).validated()


def test_a_scanner_failure_yields_export_unknown_rather_than_clean(monkeypatch):
    monkeypatch.setattr(privacy, "scan_private_content", lambda payload: ("scanner_unavailable",))
    assessment = classify("anything at all", sensitivity=SensitivityClass.SYNTHETIC)
    assert assessment.scanner_available is False
    assert assessment.export_status is ExportStatus.EXPORT_UNKNOWN
    assert PrivacyCategory.SCANNER_UNAVAILABLE.value in assessment.categories


# ══════════════════════════════════════════════════════════════════════════════════════════
#  auditable findings — the S4G lesson
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_finding_names_its_category_its_count_and_its_position():
    """A reviewer handed only 'PRIVACY_RISK' must either paste the secret somewhere or guess."""
    findings = scan(f"Hi Alejandro, my key is {SECRET} and the log is at {HOME_PATH}")
    assert findings
    for finding in findings:
        assert finding.category in set(PrivacyCategory)
        assert finding.count >= 1
        assert len(finding.first_span) == 2
        assert finding.describe()


def test_a_runtime_scanner_category_keeps_its_own_name():
    """Collapsing them into indistinguishable `runtime_scanner` entries is the S4G failure."""
    findings = scan(f"my key is {SECRET} at {HOME_PATH}")
    runtime = [f for f in findings if f.category is PrivacyCategory.RUNTIME_SCANNER]
    assert runtime, "the repository scanner should have fired on this"
    assert all(f.detail for f in runtime), "each runtime finding must name its own category"
    assert len({f.detail for f in runtime}) == len(runtime)


def test_a_finding_detail_cannot_smuggle_a_body():
    with pytest.raises(PrivacyError, match="short label"):
        PrivacyFinding(PrivacyCategory.RUNTIME_SCANNER, 1, (0, 0),
                       detail="this is a whole sentence of private conversation content")


def test_no_finding_ever_carries_the_matched_text():
    assessment = classify(f"key {SECRET} path {HOME_PATH}",
                          sensitivity=SensitivityClass.SYNTHETIC)
    blob = json.dumps(assessment.to_dict())
    assert SECRET not in blob
    assert HOME_PATH not in blob


def test_there_is_no_preview_that_returns_real_characters():
    """The name is claimed and made safe so nobody implements a helpful version."""
    preview = redacted_preview(f"a secret {SECRET} in here")
    assert SECRET not in preview
    assert "chars" in preview and "sha256:" in preview


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the secret must not reach a derived record (§4)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_secret_never_reaches_a_derived_record_through_a_lifted_span():
    """The measured leak. Extraction lifts spans verbatim, so the exclusion must be at lifting."""
    _, result, _ = distil_one(synthetic_corpus()["private_secret"])
    blob = json.dumps(result.record.to_dict())
    assert SECRET not in blob
    assert "/home/kali" not in blob
    assert "Alejandro" not in blob


def test_the_record_still_records_that_the_source_held_a_secret():
    """Excluding the span must not erase the finding: the record stays honestly non-exportable."""
    _, result, _ = distil_one(synthetic_corpus()["private_secret"])
    assert result.record.privacy.contains_secret_like_data is True
    assert result.record.privacy.export_status is ExportStatus.EXPORT_BLOCKED
    assert any("secret_bearing_turns_excluded" in label
               for label in result.record.outcome.quality_labels)


def test_the_source_turn_itself_is_preserved_not_destroyed():
    """§13: source is never destroyed. The exclusion withholds it from lifting, nothing more."""
    conversation = ingest(synthetic_corpus()["private_secret"])
    assert SECRET in conversation.turns[0].text
    assert conversation.turns[0].privacy.contains_secret_like_data is True


def test_a_derived_record_can_never_be_safer_than_its_source():
    from dataclasses import replace
    from reasoning_distillation.critic import critique
    conversation, result, _ = distil_one(synthetic_corpus()["private_secret"])
    assert conversation.export_status is not ExportStatus.EXPORT_SAFE
    forged = replace(result.record, privacy=PrivacyAssessment(
        contains_personal_data=False, contains_secret_like_data=False,
        export_status=ExportStatus.EXPORT_SAFE, sensitivity=SensitivityClass.SYNTHETIC))
    checks = {f.check for f in critique(forged, conversation).findings}
    assert "privacy_weaker_than_source" in checks


def test_a_conversation_is_only_as_exportable_as_its_weakest_turn():
    doc = conversation_doc("mixed-001", [
        ("user_request", "an entirely ordinary question about retry policy"),
        ("assistant_reasoning", f"the credential is {SECRET} which I should not echo"),
        ("assistant_answer", "rotate it")])
    conversation = ingest(doc)
    assert conversation.turns[0].privacy.export_status is ExportStatus.EXPORT_SAFE
    assert conversation.export_status is ExportStatus.EXPORT_BLOCKED


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the two write gates
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_report_may_not_carry_a_body_even_a_clean_one():
    """The length check is what a scanner-only gate misses."""
    problems = body_free_problems({"turn": "x" * 4000}, label="reports/x")
    assert problems and "exceeds" in problems[0]
    with pytest.raises(PrivacyError, match="refusing to write"):
        assert_body_free_payload({"turn": "x" * 4000})


def test_a_report_may_not_carry_private_content():
    with pytest.raises(PrivacyError, match="private content"):
        assert_body_free_payload({"note": f"the key is {SECRET}"})


def test_a_record_area_permits_prose_but_refuses_a_secret(tmp_path):
    """The two gates are not the same refusal, and one gate for both was wrong in each direction."""
    root = CorpusRoot.prepare(tmp_path / "root")
    prose = {"constraint": "The reporting query must not table-scan and must stay under 200ms, "
                           "which is a perfectly ordinary sentence of the operator's own words "
                           "that a distilled record legitimately carries verbatim."}
    assert write_report(root, "distilled", "ok.json", prose).is_file()
    with pytest.raises(StorageError, match="secret-like"):
        write_report(root, "distilled", "bad.json", {"constraint": f"the key is {SECRET}"})


def test_the_body_free_gate_still_applies_to_reports_and_manifests(tmp_path):
    root = CorpusRoot.prepare(tmp_path / "root")
    for area in sorted(BODY_FREE_AREAS):
        with pytest.raises(PrivacyError):
            write_report(root, area, "x.json", {"body": "y" * 4000})


def test_every_area_has_a_declared_write_gate():
    """The three gate classes must exhaustively partition the areas, with no overlap.

    An area in none of them would fall through to a default, and the whole point of the gates is
    that there is no default.
    """
    from reasoning_distillation.storage import AREAS, RAW_AREAS
    assert BODY_FREE_AREAS | RECORD_AREAS | RAW_AREAS == set(AREAS)
    assert not BODY_FREE_AREAS & RECORD_AREAS
    assert not RAW_AREAS & (BODY_FREE_AREAS | RECORD_AREAS)


def test_an_unclassified_area_cannot_be_written_to(tmp_path):
    root = CorpusRoot.prepare(tmp_path / "root")
    with pytest.raises(StorageError, match="no declared write gate"):
        write_report(root, "somewhere_new", "x.json", {"a": 1})
    # `raw` is a real area with its own writer, and it still has no JSON gate.
    with pytest.raises(StorageError, match="no declared write gate"):
        write_report(root, "raw", "x.json", {"a": 1})


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the corpus root stays outside Git (§4, §17)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_in_repo_fallback_root_is_actually_gitignored():
    """Asks Git, not the .gitignore text. A line is a claim; check-ignore is a measurement."""
    from reasoning_distillation.storage import REPO_RELATIVE_ROOT
    assert_root_is_gitignored(PACKAGE_ROOT.parent / REPO_RELATIVE_ROOT)


def test_a_root_inside_the_repository_that_git_would_track_is_refused():
    with pytest.raises(StorageError, match="NOT ignored by Git"):
        assert_root_is_gitignored(PACKAGE_ROOT / "core")


def test_a_root_outside_any_repository_is_permitted(tmp_path):
    assert_root_is_gitignored(tmp_path / "corpus")


def test_prepare_checks_git_before_creating_anything():
    """Creating first would leave an untracked-but-not-ignored tree on the failure path."""
    target = PACKAGE_ROOT / "core" / "m67a_should_never_exist"
    with pytest.raises(StorageError):
        CorpusRoot.prepare(target)
    assert not target.exists()


def test_a_prepared_root_records_that_the_check_actually_ran(tmp_path):
    root = CorpusRoot.prepare(tmp_path / "root")
    assert root.verified_gitignored is True
    assert root.to_dict()["verified_gitignored"] is True


def test_raw_storage_is_content_addressed_and_write_once(tmp_path):
    """§6: source data remains immutable; repeated ingestion is idempotent."""
    root = CorpusRoot.prepare(tmp_path / "root")
    payload = b"some raw conversation bytes"
    first_path, first_digest = store_raw(root, content=payload, suffix=".json")
    second_path, second_digest = store_raw(root, content=payload, suffix=".json")
    assert (first_path, first_digest) == (second_path, second_digest)
    first_path.write_bytes(b"different")
    with pytest.raises(StorageError, match="immutable"):
        store_raw(root, content=payload, suffix=".json")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  body blindness in representations
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_no_body_bearing_object_renders_a_body_in_its_repr():
    """A dataclass repr renders every field, and a container recurses into its elements."""
    conversation = ingest(synthetic_corpus()["private_secret"])
    turn = conversation.turns[0]
    for rendered in (repr(turn), repr(conversation), repr([turn]), repr({"c": conversation}),
                     f"{conversation!r}", "%r" % (turn,)):
        assert SECRET not in rendered
        assert "Alejandro" not in rendered


def test_a_bound_methods_repr_cannot_render_a_body():
    """The measured route: `repr(obj.method)` interpolates `repr(obj.__self__)`."""
    conversation = ingest(synthetic_corpus()["private_secret"])
    assert SECRET not in repr(conversation.roles_present)
    assert SECRET not in repr(conversation.to_body_free_dict)


def test_the_body_free_dict_carries_digests_not_text():
    conversation = ingest(synthetic_corpus()["private_secret"])
    blob = json.dumps(conversation.to_body_free_dict())
    assert SECRET not in blob and "Alejandro" not in blob
    assert conversation.turns[0].text_digest in json.dumps(
        conversation.turns[0].to_body_free_dict())


# ══════════════════════════════════════════════════════════════════════════════════════════
#  meta-noise (§13)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_meta_noise_needs_more_than_one_marker():
    """§13: be conservative. One 'I should' inside real reasoning is a sentence, not self-talk."""
    assert meta_noise_markers("As an AI language model I can help with that.") == ()
    markers = meta_noise_markers(
        "As an AI language model I should consider my system prompt. Okay, I need to think.")
    assert len(markers) >= 2


def test_the_marker_threshold_is_configuration():
    text = "As an AI language model I can help."
    assert meta_noise_markers(text, config=PrivacyConfig(meta_noise_min_markers=1))


def test_real_reasoning_is_not_marked_as_meta_noise():
    for key in ("good", "tool_call", "stale_fact"):
        conversation = ingest(synthetic_corpus()[key])
        assert not any(t.is_meta_noise for t in conversation.turns), key


def test_meta_noise_is_marked_and_the_turn_is_kept():
    """§13: source is never destroyed; the turn is marked and withheld from distillation input."""
    conversation = ingest(synthetic_corpus()["meta_noise"])
    noisy = [t for t in conversation.turns if t.is_meta_noise]
    assert noisy
    assert all(t.text for t in noisy), "the turn text is preserved, not deleted"


def test_a_marked_turn_contributes_no_decision_fields():
    _, result, _ = distil_one(synthetic_corpus()["meta_noise"])
    assert result.record.decision.subproblems == ()
    assert result.record.decision.decision_basis == ""


def test_the_privacy_module_publishes_its_decision_surface():
    """A verdict must be attributable to an exact pattern set."""
    versions = privacy.versions()
    assert versions["privacy_version"] and versions["meta_noise_version"]
    assert set(versions["categories"]) == {c.value for c in PrivacyCategory}
    assert versions["meta_markers"]
