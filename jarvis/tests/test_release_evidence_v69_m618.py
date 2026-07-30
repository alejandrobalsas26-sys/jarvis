"""tests/test_release_evidence_v69_m618.py — V69 M61.8: the evidence cannot lie.

An evidence bundle is a claim made to strangers. Three properties make it trustworthy,
and each is asserted here by BREAKING it and requiring the failure:

  * **completeness** — a gate that did not run may not read as one that passed;
  * **sanitization** — nothing that identifies the operator or the host may escape;
  * **atomicity** — a rejected bundle leaves no partial file for a later run to find.

Plus the property that makes the whole thing non-vacuous: the validator has to be able
to reject. A schema checker that accepts everything is decoration.

Nothing here runs the generator's subprocesses; the collection is exercised at the seam
where results are handed to the format.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import release_evidence as ev
from core import release_facts
from core.qualification import CaseVerdict, ReleaseVerdict
from core.release_evidence import Status
from core.version import VERSION

_APP_ROOT = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
def _affirmative_sections() -> dict:
    """Every mandatory section green. The baseline a test then damages."""
    return {
        "pytest_root_layout": ev.pytest_result(
            Status.PASS, command=release_facts.PYTEST_ROOT_LAYOUT_COMMAND,
            passed=3800, skipped=32, failed=0),
        "pytest_app_layout": ev.pytest_result(
            Status.PASS, command=release_facts.PYTEST_APP_LAYOUT_COMMAND,
            passed=3800, skipped=32, failed=0),
        "ruff": ev.tool_result(Status.PASS, command=release_facts.RUFF_COMMAND),
        "compileall": ev.tool_result(Status.PASS, command="python -m compileall -q core"),
        "bandit": {
            "status": Status.PASS.value,
            "command": release_facts.BANDIT_GATE_COMMAND,
            "threshold": release_facts.BANDIT_GATE_THRESHOLD,
            "medium": 0, "high": 0, "low": 488,
            "low_baseline": release_facts.BANDIT_LOW_BASELINE,
            "suppressions": release_facts.BANDIT_SUPPRESSION_COUNT,
        },
        "packaging": {"status": Status.PASS.value,
                      "wheel": release_facts.WHEEL_FILENAME,
                      "sdist": release_facts.SDIST_FILENAME},
        "package_scan": ev.tool_result(
            Status.PASS, command="python scripts/check_package_manifest.py --dist <dir>"),
        "soak": ev.tool_result(Status.PASS, command="python scripts/soak_...py"),
        "qualification": {
            "status": Status.PASS_WITH_WARNINGS.value,
            "command": release_facts.QUALIFICATION_COMMAND,
            "verdict": "PASS_WITH_WARNINGS",
            "warnings": ["live_not_requested"],
            "gates": {g: Status.PASS.value for g in release_facts.MANDATORY_GATES},
        },
        "live_validation": {"status": "NOT_REQUESTED", "detail": "not executed"},
    }


def _document(**overrides) -> dict:
    # A supplied `sections` REPLACES the baseline rather than merging into it, so a test
    # can prove what happens when a section is genuinely absent.
    sections = overrides.pop("sections", None)
    if sections is None:
        sections = _affirmative_sections()
    kwargs = {
        "generated_at_utc": "2026-07-30T12:00:00Z",
        "git_commit": "0bb1a6b18757",
        "git_branch": "master",
        "git_clean": True,
        "python_version": "3.11.9",
        "sections": sections,
        "dependency_hashes": {"base": "a" * 64, "dev": "b" * 64},
        "artifact_checksums": {
            release_facts.WHEEL_FILENAME: {
                "sha256": "c" * 64, "size_bytes": 123456, "kind": "wheel"},
            release_facts.SDIST_FILENAME: {
                "sha256": "d" * 64, "size_bytes": 234567, "kind": "sdist"},
        },
        "sbom": {
            "sbom-cyclonedx-base.json": {
                "sha256": "e" * 64, "size_bytes": 4096,
                "kind": "cyclonedx-1.5-json", "profile": "base"},
        },
    }
    kwargs.update(overrides)
    return ev.build_evidence(**kwargs)


@pytest.fixture()
def document() -> dict:
    return _document()


# ══════════════════════════════════════════════════════════════════════════════
#  Schema
# ══════════════════════════════════════════════════════════════════════════════
def test_a_complete_bundle_validates(document: dict):
    assert ev.validate_evidence(document) == []


def test_every_required_field_is_present(document: dict):
    for field in ev.EVIDENCE_JSON_SCHEMA["required"]:
        assert field in document, f"required field {field!r} not assembled"


def test_the_schema_is_real_json_schema_and_serializable():
    text = json.dumps(ev.EVIDENCE_JSON_SCHEMA)
    assert "json-schema.org" in ev.EVIDENCE_JSON_SCHEMA["$schema"]
    assert json.loads(text)["type"] == "object"


@pytest.mark.parametrize("field", [
    "schema_version", "product", "canonical_version", "release_stage", "git_commit",
    "git_branch", "git_clean", "generated_at_utc", "python_version",
    "platform_category", "qualification", "pytest_root_layout", "pytest_app_layout",
    "ruff", "compileall", "bandit", "packaging", "package_scan", "soak",
    "live_validation", "dependency_profile_hashes", "artifact_checksums", "sbom",
    "known_limitations", "evidence_completeness", "overall_status",
])
def test_removing_any_required_field_is_rejected(document: dict, field: str):
    """Non-vacuity: the validator must be able to say no about every required field."""
    damaged = {k: v for k, v in document.items() if k != field}
    problems = ev.validate_evidence(damaged)
    assert any(field in p for p in problems), problems


@pytest.mark.parametrize("field,value", [
    ("schema_version", "m0.0.0"),
    ("product", "not-jarvis"),
    ("canonical_version", "69.61"),
    ("git_commit", "ZZZZZZZ"),
    ("git_clean", "true"),
    ("generated_at_utc", "2026-07-30 12:00:00"),
    ("generated_at_utc", "2026-07-30T12:00:00+02:00"),
    ("python_version", "three-eleven"),
    ("platform_category", "Windows-AMD64"),
    ("overall_status", "OK"),
    ("evidence_completeness", "PARTIAL"),
    ("known_limitations", "a string, not a list"),
])
def test_a_malformed_value_is_rejected(document: dict, field: str, value):
    document[field] = value
    assert ev.validate_evidence(document) != [], f"{field}={value!r} was accepted"


@pytest.mark.parametrize("digest", ["short", "C" * 64, "z" * 64, 12345])
def test_a_malformed_artifact_digest_is_rejected(document: dict, digest):
    document["artifact_checksums"][release_facts.WHEEL_FILENAME]["sha256"] = digest
    assert ev.validate_evidence(document) != []


def test_an_unknown_status_value_is_rejected(document: dict):
    document["ruff"]["status"] = "PROBABLY_FINE"
    assert ev.validate_evidence(document) != []


def test_a_boolean_is_not_accepted_where_an_integer_is_required(document: dict):
    """In Python True is an int; in the schema it is not."""
    document["artifact_checksums"][release_facts.WHEEL_FILENAME]["size_bytes"] = True
    assert ev.validate_evidence(document) != []


def test_an_empty_dependency_hash_map_is_rejected(document: dict):
    document["dependency_profile_hashes"] = {}
    assert ev.validate_evidence(document) != []


def test_a_non_object_document_is_rejected():
    for value in ([], "evidence", 7, None):
        assert ev.validate_evidence(value) != []


# ══════════════════════════════════════════════════════════════════════════════
#  Completeness — a missing result can never become PASS
# ══════════════════════════════════════════════════════════════════════════════
def test_a_complete_bundle_is_complete(document: dict):
    assert document["evidence_completeness"] == "COMPLETE"
    assert document["evidence_gaps"] == []


@pytest.mark.parametrize("section", ev.MANDATORY_SECTIONS)
@pytest.mark.parametrize("status", [
    Status.SKIPPED, Status.INSUFFICIENT_EVIDENCE, Status.FAIL])
def test_any_unmeasured_mandatory_section_makes_the_bundle_incomplete(section, status):
    sections = _affirmative_sections()
    sections[section] = {**sections[section], "status": status.value}
    document = _document(sections=sections)
    assert document["evidence_completeness"] == "INCOMPLETE"
    assert any(section in gap for gap in document["evidence_gaps"])
    assert document["overall_status"] != Status.PASS.value
    assert document["overall_status"] != Status.PASS_WITH_WARNINGS.value


@pytest.mark.parametrize("section", ev.MANDATORY_SECTIONS)
def test_an_absent_mandatory_section_is_insufficient_evidence_not_a_pass(section):
    sections = {k: v for k, v in _affirmative_sections().items() if k != section}
    document = _document(sections=sections)
    assert ev.section_status(document[section]) == Status.INSUFFICIENT_EVIDENCE.value
    assert document["evidence_completeness"] == "INCOMPLETE"
    assert document["overall_status"] == Status.INSUFFICIENT_EVIDENCE.value


def test_a_failing_gate_makes_the_bundle_fail_not_merely_incomplete():
    sections = _affirmative_sections()
    sections["bandit"] = {**sections["bandit"], "status": Status.FAIL.value}
    assert _document(sections=sections)["overall_status"] == Status.FAIL.value


def test_a_fail_outranks_an_insufficient_evidence():
    sections = _affirmative_sections()
    sections["soak"] = {**sections["soak"], "status": Status.FAIL.value}
    sections["ruff"] = {**sections["ruff"],
                        "status": Status.INSUFFICIENT_EVIDENCE.value}
    assert _document(sections=sections)["overall_status"] == Status.FAIL.value


def test_the_optional_live_section_does_not_break_completeness(document: dict):
    """Live validation is optional; its absence is a warning, never a gap."""
    assert document["live_validation"]["status"] == "NOT_REQUESTED"
    assert document["evidence_completeness"] == "COMPLETE"
    assert "live_validation" not in " ".join(document["evidence_gaps"])


def test_an_unexecuted_live_validation_downgrades_pass_to_pass_with_warnings():
    sections = _affirmative_sections()
    sections["qualification"] = {**sections["qualification"], "warnings": []}
    document = _document(sections=sections)
    assert document["overall_status"] == Status.PASS_WITH_WARNINGS.value


def test_pass_requires_live_validation_to_have_passed():
    sections = _affirmative_sections()
    sections["qualification"] = {**sections["qualification"],
                                 "warnings": [], "verdict": "PASS",
                                 "status": Status.PASS.value}
    sections["live_validation"] = {"status": Status.PASS.value, "detail": "observed"}
    assert _document(sections=sections)["overall_status"] == Status.PASS.value


def test_live_not_requested_is_never_rewritten_as_pass(document: dict):
    assert release_facts.LIVE_VALIDATION_STATUS == "NOT_REQUESTED"
    assert release_facts.QUALIFICATION_VERDICT == "PASS_WITH_WARNINGS"
    assert "live_not_requested" in release_facts.QUALIFICATION_WARNINGS
    assert document["overall_status"] == Status.PASS_WITH_WARNINGS.value


def test_the_status_vocabulary_introduces_nothing_new():
    """Evidence reuses the M59/M61 verdict vocabulary rather than inventing a third."""
    known = {v.value for v in ReleaseVerdict} | {v.value for v in CaseVerdict}
    assert set(ev.STATUSES) <= known, sorted(set(ev.STATUSES) - known)
    assert {s.value for s in Status} == set(ev.STATUSES)


def test_an_invalid_status_cannot_be_constructed():
    with pytest.raises(ValueError, match="not a release-evidence status"):
        ev.tool_result("PROBABLY_FINE")


# ══════════════════════════════════════════════════════════════════════════════
#  Sanitization
# ══════════════════════════════════════════════════════════════════════════════
def test_a_clean_bundle_leaks_nothing(document: dict):
    assert ev.scan_for_private_content(document) == []


@pytest.mark.parametrize("leak", [
    r"C:\Users\someone\Downloads\jarvis",
    "C:/Users/someone/jarvis",
    "/home/operator/jarvis",
    "/Users/operator/jarvis",
    "/root/.ssh/id_ed25519",
    "/var/log/jarvis.log",
    "/etc/passwd",
    r"\\fileserver\share\build",
    "%USERPROFILE%\\jarvis",
    "%LOCALAPPDATA%\\Temp",
    "$HOME/jarvis",
    "~/jarvis/dist",
    "Users/operator/x",
])
def test_a_path_like_value_is_refused(document: dict, leak: str):
    document["ruff"]["detail"] = leak
    problems = ev.scan_for_private_content(document)
    assert problems, f"{leak!r} passed the sanitizer"
    assert all(leak not in p for p in problems), \
        "the sanitization report must not quote the value it found"


@pytest.mark.parametrize("leak", [
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI",
    "|1|abcdefghijklmnopqrstuvwxyz012=|Zm9vYmFy=",
    "ghp_0123456789abcdefghij",
    "github_pat_11ABCDEFG0123456789_abcdefghij",
    "sk-0123456789abcdefghij",
    "sk-ant-api03-abcdefghij",
    "xoxb-0123456789-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer eyJhbGciOiJIUzI1NiJ9",
    "api_key=supersecretvalue",
    "password: hunter2hunter2",
    "https://user:secret@example.invalid/repo.git",
])
def test_credential_shaped_material_is_refused(document: dict, leak: str):
    document["compileall"]["detail"] = leak
    problems = ev.scan_for_private_content(document)
    assert problems, f"{leak!r} passed the sanitizer"


def test_the_machine_hostname_is_refused(document: dict):
    import platform
    node = platform.node()
    if len(node) < 4:
        pytest.skip("this host has no hostname long enough to test with")
    document["ruff"]["detail"] = f"built on {node}"
    assert any("machine_hostname" in p
               for p in ev.scan_for_private_content(document))


def test_the_os_username_is_refused(document: dict):
    import getpass
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        pytest.skip("no OS username available on this host")
    if len(user) < 4:
        pytest.skip("username too short to scan for without false positives")
    document["ruff"]["detail"] = f"ran as {user}"
    assert ev.scan_for_private_content(document) != []


def test_a_sensitive_environment_value_is_refused(document: dict, monkeypatch):
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "zz-not-a-real-token-value-9911")
    document["ruff"]["detail"] = "token zz-not-a-real-token-value-9911 was used"
    problems = ev.scan_for_private_content(document)
    assert any("JARVIS_TELEGRAM_TOKEN" in p for p in problems), problems


def test_a_leak_in_a_nested_list_or_key_is_found(document: dict):
    document["known_limitations"] = ["fine", "/home/operator/leak"]
    assert ev.scan_for_private_content(document) != []
    clean = _document()
    clean["dependency_profile_hashes"]["C:/Users/x/base.txt"] = "f" * 64
    assert ev.scan_for_private_content(clean) != []


def test_the_platform_category_carries_no_machine_identity():
    import platform
    category = ev.platform_category()
    assert category.count("-") >= 1
    node = platform.node()
    if len(node) >= 4:
        assert node.lower() not in category.lower()
    assert platform.platform() != category


def test_the_evidence_never_embeds_a_prompt_or_a_log(document: dict):
    """Structural: the format has no field able to carry conversational content."""
    forbidden = {"prompt", "prompts", "response", "responses", "conversation",
                 "messages", "transcript", "log", "logs", "stdout", "stderr",
                 "environment", "env", "tool_arguments"}
    assert not (set(document) & forbidden)
    assert not (set(ev.EVIDENCE_JSON_SCHEMA["properties"]) & forbidden)


# ══════════════════════════════════════════════════════════════════════════════
#  Atomic write
# ══════════════════════════════════════════════════════════════════════════════
def test_a_valid_bundle_is_written_with_its_schema(document: dict, tmp_path: Path):
    written = ev.write_evidence(document, tmp_path)
    assert written.name == release_facts.EVIDENCE_FILENAME
    assert (tmp_path / ev.EVIDENCE_SCHEMA_FILENAME).is_file()
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert ev.validate_evidence(reloaded) == []
    assert reloaded["canonical_version"] == VERSION


def test_the_serialization_is_deterministic(document: dict):
    assert ev.serialize(document) == ev.serialize(dict(reversed(list(
        document.items()))))
    assert ev.serialize(document).endswith("\n")


def test_a_schema_rejection_writes_nothing(document: dict, tmp_path: Path):
    document["canonical_version"] = "not-a-version"
    with pytest.raises(ev.EvidenceRejected):
        ev.write_evidence(document, tmp_path)
    assert list(tmp_path.iterdir()) == [], "a rejected bundle left files behind"


def test_a_sanitization_rejection_writes_nothing(document: dict, tmp_path: Path):
    document["ruff"]["detail"] = "/home/operator/jarvis/dist"
    with pytest.raises(ev.EvidenceRejected) as excinfo:
        ev.write_evidence(document, tmp_path)
    assert any("private content" in p for p in excinfo.value.problems)
    assert list(tmp_path.iterdir()) == []


def test_a_rejection_leaves_no_temporary_residue(document: dict, tmp_path: Path):
    document["product"] = "wrong"
    with pytest.raises(ev.EvidenceRejected):
        ev.write_evidence(document, tmp_path)
    assert not list(tmp_path.rglob("*")), "temporary residue survived a rejection"


def test_an_existing_output_directory_is_untouched_by_a_rejection(
        document: dict, tmp_path: Path):
    marker = tmp_path / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    document["git_commit"] = "not-a-sha"
    with pytest.raises(ev.EvidenceRejected):
        ev.write_evidence(document, tmp_path)
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not (tmp_path / release_facts.EVIDENCE_FILENAME).exists()


def test_writing_creates_a_missing_output_directory(document: dict, tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c"
    assert ev.write_evidence(document, nested).is_file()


# ══════════════════════════════════════════════════════════════════════════════
#  Dependency profile hashes
# ══════════════════════════════════════════════════════════════════════════════
def test_every_declared_profile_is_hashed():
    from core.dependency_authority import PROFILES
    hashes = ev.dependency_profile_hashes(_APP_ROOT / "requirements")
    assert set(hashes) == set(PROFILES)
    for digest in hashes.values():
        assert len(digest) == 64


def test_profile_hashes_ignore_comments_and_order_but_not_packages():
    base = "# a comment\nrequests>=2.31.0\n\nhttpx>=0.27.0\n"
    reordered = "httpx>=0.27.0\nrequests>=2.31.0\n# different comment\n"
    changed = "requests>=2.31.0\nhttpx>=0.28.0\n"
    added = "requests>=2.31.0\nhttpx>=0.27.0\nevil>=1.0\n"
    assert ev.profile_hash(base) == ev.profile_hash(reordered)
    assert ev.profile_hash(base) != ev.profile_hash(changed)
    assert ev.profile_hash(base) != ev.profile_hash(added)


def test_profile_hashes_are_stable_across_calls():
    directory = _APP_ROOT / "requirements"
    assert ev.dependency_profile_hashes(directory) == \
        ev.dependency_profile_hashes(directory)
