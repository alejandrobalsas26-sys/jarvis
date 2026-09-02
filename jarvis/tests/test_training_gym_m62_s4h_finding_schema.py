"""V69 M62 S4H — the body-safe finding: shape, determinism, and what it cannot carry.

WHAT THESE TESTS ARE FOR
------------------------
The finding is the artefact that leaves the instrument and enters a receipt, a report and
eventually a ticket. Two properties have to hold for that to be safe, and neither is
self-evident from reading the dataclass:

  * **It cannot carry a value.** Not in a field, not in a repr, not in an exception, not
    through a counter. Tested by construction and by attempting each route.
  * **It serializes identically every time.** A record whose bytes move between runs
    cannot be digested, compared or round-tripped, and a "changed" finding that only
    changed its timestamp is a false alarm a reviewer learns to ignore.

The digest tests pin the DOMAIN SEPARATION rather than a literal digest: what matters is
that a plain ``sha256(value)`` cannot be used against the published digest, and that two
occurrences in different contexts do not correlate.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training_gym.evaluation.instruments.finding import (  # noqa: E402
    FINDING_SCHEMA_VERSION,
    ContextClass,
    EvidenceClass,
    FindingError,
    InstrumentFinding,
    SemanticClass,
    Severity,
    evidence_digest,
    findings_to_payload,
)

FAKE_SECRET = "sk-s4h-synthetic-not-a-real-credential-000"


def a_finding(**overrides) -> InstrumentFinding:
    base = dict(
        detector_version="m62.secret_pii_detector.2",
        rule_id="credential.api_key", rule_class="credential_label",
        family="secret_pii", category="credential",
        evidence_class=EvidenceClass.LABELLED_VALUE,
        context_class=ContextClass.JSON_OBJECT,
        semantic_class=SemanticClass.ACTUAL_DISCLOSURE,
        severity=Severity.CRITICAL, source_component="answer",
        match_length=len(FAKE_SECRET), sensitive_value_present=True)
    base.update(overrides)
    return InstrumentFinding(**base)


# ── shape ────────────────────────────────────────────────────────────────────
def test_the_schema_version_is_pinned_and_separate_from_the_detector_version():
    finding = a_finding()
    assert finding.schema_version == FINDING_SCHEMA_VERSION == "m62.finding_schema.2"
    assert finding.detector_version != finding.schema_version


def test_every_vocabulary_field_is_a_closed_enum_and_a_string_is_refused():
    for field in ("evidence_class", "context_class", "semantic_class", "severity",
                  "placeholder_state", "redaction_state"):
        with pytest.raises(FindingError):
            a_finding(**{field: "whatever_the_caller_felt_like"})


def test_an_identifier_long_enough_to_be_a_body_is_refused():
    with pytest.raises(FindingError, match="body wearing a field name"):
        a_finding(rule_id="x" * 121)


def test_counters_take_integers_only_so_no_string_smuggles_a_value_through():
    assert a_finding(counters={"occurrences": 3}).counters == {"occurrences": 3}
    with pytest.raises(FindingError, match="body-shaped hole"):
        a_finding(counters={"matched": FAKE_SECRET})


def test_a_disclosure_flag_that_contradicts_the_semantic_class_is_refused():
    """Two fields answering one question must not be allowed to disagree."""
    with pytest.raises(FindingError, match="must agree"):
        a_finding(semantic_class=SemanticClass.REDACTED_PLACEHOLDER,
                  sensitive_value_present=True)


def test_the_semantic_vocabulary_covers_every_class_s4h_requires():
    required = {"actual_disclosure", "field_mention_only", "redacted_placeholder",
                "withheld_value", "synthetic_public_example",
                "credential_shaped_disclosure", "pii_disclosure",
                "unknown_sensitive_shape"}
    assert required <= {member.value for member in SemanticClass}


def test_only_disclosure_classes_assert_a_value_reached_the_output():
    assert SemanticClass.ACTUAL_DISCLOSURE.is_disclosure
    assert SemanticClass.CREDENTIAL_SHAPED_DISCLOSURE.is_disclosure
    assert SemanticClass.PII_DISCLOSURE.is_disclosure
    for benign in (SemanticClass.FIELD_MENTION_ONLY,
                   SemanticClass.REDACTED_PLACEHOLDER,
                   SemanticClass.WITHHELD_VALUE,
                   SemanticClass.SYNTHETIC_PUBLIC_EXAMPLE,
                   SemanticClass.UNKNOWN_SENSITIVE_SHAPE):
        assert not benign.is_disclosure


# ── determinism ──────────────────────────────────────────────────────────────
def test_serialization_round_trips_to_identical_bytes():
    """object -> bytes -> object -> bytes, and the two byte strings are equal."""
    finding = a_finding(counters={"occurrences": 2, "line": 7})
    first = finding.canonical_bytes()
    rebuilt = InstrumentFinding.from_dict(json.loads(first))
    assert rebuilt.canonical_bytes() == first
    assert InstrumentFinding.from_dict(rebuilt.to_dict()).canonical_bytes() == first


def test_the_serialization_carries_no_timestamp():
    """A content hash that moved with the clock would make every finding look new."""
    payload = a_finding().to_dict()
    assert not any("time" in key or "date" in key or key.endswith("_at")
                   for key in payload)


def test_two_equal_findings_produce_one_finding_id_and_two_unequal_ones_do_not():
    assert a_finding().finding_id == a_finding().finding_id
    assert a_finding().finding_id != a_finding(rule_id="credential.token").finding_id


def test_a_payload_list_is_sorted_so_two_equal_sets_serialize_identically():
    one = (a_finding(rule_id="credential.token"), a_finding())
    other = (a_finding(), a_finding(rule_id="credential.token"))
    assert findings_to_payload(one) == findings_to_payload(other)


def test_a_reader_refuses_a_schema_version_it_does_not_know():
    payload = {**a_finding().to_dict(), "schema_version": "m62.finding_schema.9"}
    with pytest.raises(FindingError, match="schema_version"):
        InstrumentFinding.from_dict(payload)


def test_a_reader_names_the_field_a_truncated_record_omits():
    payload = {k: v for k, v in a_finding().to_dict().items() if k != "rule_id"}
    with pytest.raises(FindingError, match="rule_id"):
        InstrumentFinding.from_dict(payload)


# ── the leak surfaces ────────────────────────────────────────────────────────
def test_no_representation_of_a_finding_contains_the_value_it_describes():
    """§21: the test process knows the secret; every serialized form must not."""
    finding = a_finding(evidence_digest=evidence_digest(
        rule_id="credential.api_key",
        detector_version="m62.secret_pii_detector.2",
        source_component="answer", context_class="json_object",
        raw_value=FAKE_SECRET))
    surfaces = (repr(finding), str(finding), json.dumps(finding.to_dict()),
                finding.canonical_bytes().decode("utf-8"), finding.finding_id,
                repr(findings_to_payload((finding,))))
    for surface in surfaces:
        assert FAKE_SECRET not in surface
        assert "sk-s4h" not in surface


def test_an_exception_from_the_finding_layer_never_quotes_the_value():
    """§22: 'rule credential.api_key detected ACTUAL_DISCLOSURE', never the match."""
    with pytest.raises(FindingError) as caught:
        a_finding(counters={"matched": FAKE_SECRET})
    assert FAKE_SECRET not in str(caught.value)


def test_the_repr_renders_a_fixed_field_list_rather_than_whatever_exists():
    """A default repr renders fields a future contributor adds. This one does not."""
    text = repr(a_finding())
    assert text.startswith("InstrumentFinding(")
    assert "rule_id=" in text and "semantic_class=" in text
    assert "evidence_digest=" not in text


# ── the digest's threat model ────────────────────────────────────────────────
def test_the_digest_is_not_a_plain_sha256_of_the_value():
    """A rainbow table for bare SHA-256 must not resolve the published digest."""
    digest = evidence_digest(rule_id="credential.api_key",
                             detector_version="m62.secret_pii_detector.2",
                             source_component="answer", context_class="prose",
                             raw_value=FAKE_SECRET)
    assert digest != hashlib.sha256(FAKE_SECRET.encode()).hexdigest()
    assert len(digest) == 64


def test_the_same_value_in_two_contexts_does_not_correlate():
    """Context binding is what stops a digest linking occurrences across components."""
    common = dict(rule_id="credential.api_key",
                  detector_version="m62.secret_pii_detector.2",
                  raw_value=FAKE_SECRET)
    answer = evidence_digest(source_component="answer", context_class="prose", **common)
    tool = evidence_digest(source_component="tool_result[0]", context_class="prose",
                           **common)
    fenced = evidence_digest(source_component="answer", context_class="code_fence",
                             **common)
    assert len({answer, tool, fenced}) == 3


def test_two_different_findings_cannot_be_made_to_share_a_preimage():
    """Concatenation ambiguity: ('ab','c') and ('a','bc') must not collide."""
    common = dict(detector_version="v", source_component="answer",
                  context_class="prose", raw_value=FAKE_SECRET)
    assert evidence_digest(rule_id="ab", **common) != evidence_digest(rule_id="a",
                                                                      **common)


def test_a_field_carrying_the_separator_is_refused_rather_than_silently_joined():
    with pytest.raises(FindingError, match="separator"):
        evidence_digest(rule_id="cred\x1fential", detector_version="v",
                        source_component="answer", context_class="prose",
                        raw_value=FAKE_SECRET)


def test_the_digest_is_optional_and_absent_by_default():
    """The strongest protection is not storing one at all."""
    assert a_finding().evidence_digest == ""
    assert a_finding().to_dict()["evidence_digest"] == ""


def test_a_digest_field_that_is_not_a_digest_is_refused():
    with pytest.raises(FindingError, match="digest's clothing"):
        a_finding(evidence_digest=FAKE_SECRET)
