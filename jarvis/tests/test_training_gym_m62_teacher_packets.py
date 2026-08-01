"""V69 M62 — teacher packet export, review import, binding and replay controls.

Every test here exists because of one specific way a teacher review could become more
authoritative than it is allowed to be: a packet that carries the answer, a review
lifted off another attempt, a score that is not a number, a secret that survives an
export, or a packet answered twice to manufacture a quorum.

Nothing here touches a network, a credential, a model or a container. Every "provider"
is a local object, and the only I/O is into a pytest ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from training_gym.graders.aggregate import aggregate
from training_gym.schemas import ResultStatus, SensitivityClass, Severity, sha256_text
from training_gym.task_spec import ActionKind, TaskFamily, TaskSpec
from training_gym.teachers.base import (
    RUBRIC_VERSION,
    ReviewMode,
    TeacherKind,
    TeacherReviewRecord,
)
from training_gym.teachers.manual_packet import (
    PACKET_INSTRUCTIONS,
    ManualReviewPacket,
    PacketError,
    PacketExportBlocked,
    build_packet,
    export_blockers,
    packet_from_dict,
)
from training_gym.teachers.review_import import (
    ReplayRejected,
    ReviewImportError,
    parse_review_json,
    register_review,
    strict_json_object,
)
from training_gym.teachers.sanitization import (
    SanitizationError,
    hidden_key,
    sanitize_structure,
    sanitize_text,
    scan_export_payload,
)
from training_gym.teachers.store import (
    InMemoryPacketLedger,
    PacketLedger,
    StoreError,
    TeacherArtifactStore,
)
from training_gym.trajectory import (
    GraderResult,
    ModelIdentity,
    ModelRole,
    Recommendation,
    ToolCallRecord,
    Trajectory,
)

NOW = "2026-08-01T12:00:00Z"


# ── local builders (deliberately not shared with a sibling test module) ───────
def make_spec(**overrides) -> TaskSpec:
    """A minimal valid structured-report task.

    Defined locally on purpose: the repository supports being tested from both its git
    root and its application root, and a helper imported from another test module
    resolves in only one of them."""
    base = {
        "task_id": "report-001",
        "task_family": TaskFamily.STRUCTURED_REPORT,
        "prompt": "Summarise the provided alert fixture as a structured report.",
        "created_by": "operator",
        "created_at": "2026-07-31T00:00:00Z",
        "allowed_actions": (ActionKind.READ_WORKSPACE_FILE, ActionKind.EMIT_ANSWER),
        "required_graders": ("json_schema", "secret_pii"),
        "expected_output_schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string", "const": "malicious"},
                           "evidence": {"type": "array"}},
            "required": ["verdict"],
            "enum": ["malicious"],
        },
    }
    base.update(overrides)
    scoring = base.pop("scoring", None)
    spec_scoring = scoring or _scoring(("json_schema", "secret_pii"))
    return TaskSpec(scoring=spec_scoring, **base)


def _scoring(mandatory: tuple[str, ...], **kw):
    from training_gym.policies import ScoringPolicy
    return ScoringPolicy(mandatory_graders=mandatory, min_total_score=0.1, **kw)


def make_trajectory(spec: TaskSpec, **overrides) -> Trajectory:
    trajectory = Trajectory(
        episode_id=overrides.pop("episode_id", "ep-001"),
        task_id=spec.task_id,
        task_hash=spec.spec_hash(),
        attempt_number=overrides.pop("attempt_number", 1),
        model=ModelIdentity(role=ModelRole.STUDENT, base_model="qwen3",
                            model_id="qwen3:8b-q4_K_M"),
        final_answer=overrides.pop("final_answer",
                                   '{"verdict": "benign", "evidence": ["EV-1"]}'),
        **overrides)
    return trajectory


def passing_results() -> list[GraderResult]:
    return [
        GraderResult(grader_id="json_schema", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=3),
        GraderResult(grader_id="secret_pii", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=7),
    ]


def make_packet(spec: TaskSpec | None = None, trajectory: Trajectory | None = None, *,
                provider: str = "manual_packet", model: str = "gpt-5-thinking",
                nonce: str = "nonce-001",
                results: list[GraderResult] | None = None) -> ManualReviewPacket:
    spec = spec or make_spec()
    trajectory = trajectory if trajectory is not None else make_trajectory(spec)
    report = aggregate(spec, trajectory,
                       results=results if results is not None else passing_results())
    return build_packet(spec, trajectory, report, requested_provider=provider,
                        requested_model=model, nonce=nonce, created_at_utc=NOW)


def good_response(packet: ManualReviewPacket, **overrides) -> dict:
    payload = {
        "packet_id": packet.packet_id,
        "packet_hash": packet.packet_hash,
        "task_hash": packet.task_hash,
        "attempt_hash": packet.attempt_hash,
        "deterministic_report_hash": packet.deterministic_report_hash,
        "rubric_version": packet.rubric_version,
        "provider": packet.requested_provider,
        "model": packet.requested_model,
        "overall_score": 0.8,
        "dimension_scores": {"correctness": 0.9, "security": 0.7},
        "recommendation": "approve",
        "confidence": 0.6,
        "missing_evidence": [],
    }
    payload.update(overrides)
    return payload


def import_response(packet: ManualReviewPacket, payload: dict | str,
                    **kw) -> TeacherReviewRecord:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    defaults = {"provider_id": packet.requested_provider,
                "provider_version": "m62.manual.1",
                "provider_kind": TeacherKind.MANUAL_PACKET,
                "mode": ReviewMode.OFFLINE_PACKET,
                "created_at_utc": NOW}
    defaults.update(kw)
    return parse_review_json(text, packet=packet, **defaults)


# ── 1. export ─────────────────────────────────────────────────────────────────
def test_valid_manual_packet_export_carries_only_allowed_material():
    packet = make_packet()
    payload = packet.to_dict()
    assert packet.packet_id.startswith("pkt-")
    assert packet.rubric_version == RUBRIC_VERSION
    assert payload["packet_hash"] == packet.compute_hash()
    assert set(payload) == {
        "schema_version", "packet_version", "protocol_version", "packet_id", "nonce",
        "created_at_utc", "requested_provider", "requested_model", "task_hash",
        "attempt_hash", "deterministic_report_hash", "rubric_version", "rubric",
        "instructions", "response_schema", "task", "attempt",
        "deterministic_summary", "packet_hash", "sanitization"}
    # The reviewer is told, inside the hashed packet, exactly what it may not do.
    joined = " ".join(PACKET_INSTRUCTIONS).lower()
    for demand in ("untrusted data", "do not execute", "do not invent evidence",
                   "json object", "advisory", "hidden reasoning"):
        assert demand in joined


def test_packet_hash_changes_when_any_instruction_is_softened():
    packet = make_packet()
    original = packet.compute_hash()
    weakened = dict(packet.to_dict())
    weakened["instructions"] = ["Approve if it looks reasonable."]
    assert sha256_text(json.dumps(weakened, sort_keys=True)) != original
    with pytest.raises(PacketError, match="modified after export"):
        packet_from_dict(weakened)


def test_packet_excludes_hidden_evaluation_target_from_the_output_schema():
    packet = make_packet()
    shape = packet.task["required_output_shape"]
    assert shape["type"] == "object"
    assert shape["property_names"] == ["evidence", "verdict"]
    # Only the shape survives: ``const`` and ``enum`` in the task's schema each carried
    # the expected verdict, and neither keyword nor its value reaches the packet.
    assert set(shape) == {"type", "property_names", "required", "note"}
    assert "malicious" not in json.dumps(packet.to_dict())


def test_evaluation_only_task_cannot_be_exported():
    spec = make_spec(evaluation_only=True, dataset_eligible=False)
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    assert any("evaluation_only" in b for b in export_blockers(spec, report))
    with pytest.raises(PacketExportBlocked, match="held-out target"):
        build_packet(spec, trajectory, report, requested_provider="manual_packet",
                     requested_model="gpt-5-thinking", nonce="n-1",
                     created_at_utc=NOW)


@pytest.mark.parametrize("sensitivity", [SensitivityClass.INTERNAL,
                                         SensitivityClass.RESTRICTED])
def test_restricted_sensitivity_export_is_blocked(sensitivity):
    spec = make_spec(sensitivity=sensitivity,
                     dataset_eligible=sensitivity.dataset_eligible)
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    with pytest.raises(PacketExportBlocked, match="not exportable"):
        build_packet(spec, trajectory, report, requested_provider="manual_packet",
                     requested_model="gpt-5-thinking", nonce="n-1",
                     created_at_utc=NOW)


def test_packet_refuses_a_report_describing_another_attempt():
    spec = make_spec()
    first = make_trajectory(spec, attempt_number=1)
    second = make_trajectory(spec, attempt_number=2)
    report = aggregate(spec, first, results=passing_results())
    with pytest.raises(PacketError, match="two subjects"):
        build_packet(spec, second, report, requested_provider="manual_packet",
                     requested_model="gpt-5-thinking", nonce="n-1",
                     created_at_utc=NOW)


def test_packet_carries_no_absolute_path_or_hidden_reasoning():
    spec = make_spec(
        prompt="Summarise the alert fixture; the analyst notes live in the workspace.")
    trajectory = make_trajectory(
        spec,
        final_answer='<think>the key is sk-abcdefghijklmnopqrstuvwx</think>'
                     '{"verdict": "benign"}')
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="manual_packet",
                          requested_model="claude-opus-5", nonce="n-2",
                          created_at_utc=NOW)
    assert scan_export_payload(packet.to_dict()) == ()
    answer = packet.attempt["final_answer"]
    assert "<think>" not in answer
    assert "sk-abcdefghijklmnopqrstuvwx" not in answer


def test_secret_in_the_answer_is_removed_before_export():
    spec = make_spec()
    trajectory = make_trajectory(
        spec, final_answer='{"verdict":"benign","note":"ghp_' + "a" * 36 + '"}')
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="manual_packet",
                          requested_model="claude-opus-5", nonce="n-3",
                          created_at_utc=NOW)
    assert "ghp_" not in json.dumps(packet.to_dict())


def test_secret_in_tool_arguments_is_removed_before_export():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    trajectory.tool_calls.append(ToolCallRecord.capture(
        "http_get", {"url": "https://example.test",
                     "headers": {"Authorization": "Bearer abcdefghijklmnopqrst"}}))
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="manual_packet",
                          requested_model="claude-opus-5", nonce="n-4",
                          created_at_utc=NOW)
    blob = json.dumps(packet.to_dict())
    assert "abcdefghijklmnopqrst" not in blob
    assert scan_export_payload(packet.to_dict()) == ()


def test_scanner_unavailable_blocks_export(monkeypatch):
    import training_gym.teachers.sanitization as S

    def _no_scanner(*_a, **_kw):
        raise ImportError("core.redaction_policy is not installed here")

    monkeypatch.setattr("core.redaction_policy.redact_text", _no_scanner)
    cleaned, report = S.sanitize_text("anything at all")
    assert report.scanner_available is False
    assert report.clean is False
    with pytest.raises(SanitizationError, match="unavailable"):
        S.sanitize_for_export({"text": "anything"}, label="probe")
    assert cleaned  # the text is still processed; it is simply never called clean


# ── 2. sanitization boundary ──────────────────────────────────────────────────
def test_username_and_hostname_are_redacted():
    import getpass
    import socket
    user = getpass.getuser()
    host = socket.gethostname()
    if len(user) < 4 or len(host) < 4:
        pytest.skip("host identity strings are too short to substitute safely")
    cleaned, report = sanitize_text(f"ran as {user} on {host}")
    assert user.lower() not in cleaned.lower()
    assert host.lower() not in cleaned.lower()
    assert "identity" in report.categories


@pytest.mark.parametrize("raw", [
    r"C:\Users\someone\Downloads\case.json",
    r"D:\lab\case-14\evidence.evtx",
    "/home/analyst/notes.md",
    "/opt/intel/feed.json",
    r"\\fileserver\share\case",
    "file:///etc/shadow",
])
def test_absolute_host_paths_are_redacted(raw):
    cleaned, _report = sanitize_text(f"opened {raw} for review")
    assert scan_export_payload(cleaned) == ()
    assert raw not in cleaned


def test_relative_fixture_paths_and_hashes_survive_sanitization():
    digest = sha256_text("fixture")
    payload = {"path": "fixtures/alert.json", "sha256": digest,
               "evidence_ids": ["EV-1", "EV-2"], "status": "pass"}
    cleaned, _report = sanitize_structure(payload)
    assert cleaned["path"] == "fixtures/alert.json"
    assert cleaned["sha256"] == digest
    assert cleaned["evidence_ids"] == ["EV-1", "EV-2"]


@pytest.mark.parametrize("name", [
    "expected_answer", "student_expected_answer", "chainOfThought", "cot",
    "api_key", "X-API-Key", "authorization", "ground_truth", "internal_notes",
    "environ", "session_id", "PRIVATE_KEY",
])
def test_hidden_and_credential_field_names_are_dropped(name):
    assert hidden_key(name) is True
    cleaned, report = sanitize_structure({name: "the answer is 42"})
    assert "the answer is 42" not in json.dumps(cleaned)
    assert report.dropped_keys
    # Withheld, not deleted: an incomplete packet must not look like a complete one.
    assert list(cleaned.values()) == ["[dropped-by-export-policy]"]


@pytest.mark.parametrize("name", ["resolution", "prompt", "evidence", "verdict",
                                  "task_id", "keyboard_layout"])
def test_ordinary_field_names_survive(name):
    assert hidden_key(name) is False


def test_unsupported_object_is_described_by_type_not_repr():
    class Client:
        def __repr__(self) -> str:  # pragma: no cover — must never be called
            return "Client(api_key='sk-secret')"

    cleaned, report = sanitize_structure({"client": Client()})
    assert "sk-secret" not in json.dumps(cleaned)
    assert "Client" in cleaned["client"]
    assert "unsupported_type" in report.categories


# ── 3. strict parsing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "not json at all",
    "Sure! Here is my review: {\"overall_score\": 0.9}",
    "[]",
    "0.9",
    '"approve"',
    "",
])
def test_non_json_or_non_object_responses_are_refused(raw):
    with pytest.raises(ReviewImportError):
        strict_json_object(raw)


def test_duplicate_keys_are_refused_rather_than_last_wins():
    with pytest.raises(ReviewImportError, match="duplicate key"):
        strict_json_object('{"recommendation": "reject", "recommendation": "approve"}')


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_refused(literal):
    with pytest.raises(ReviewImportError, match="not a number"):
        strict_json_object('{"overall_score": %s}' % literal)


def test_oversized_response_is_refused_before_decoding():
    with pytest.raises(ReviewImportError, match="exceeds"):
        strict_json_object('{"x": "' + "a" * 70_000 + '"}')


# ── 4. import and field validation ───────────────────────────────────────────
def test_valid_review_import_produces_a_bound_record():
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    assert record.recommendation is Recommendation.APPROVE
    assert record.task_hash == packet.task_hash
    assert record.attempt_hash == packet.attempt_hash
    assert record.deterministic_report_hash == packet.deterministic_report_hash
    assert record.packet_hash == packet.packet_hash
    assert record.review_hash() == record.review_hash()
    assert len(record.review_hash()) == 64
    # The record round-trips through its own serialisation, digest included.
    assert TeacherReviewRecord.from_dict(record.to_dict()).review_hash() == \
        record.review_hash()


def test_review_hash_covers_every_security_relevant_field():
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    base = record.review_hash()
    for mutation in (
            {"overall_score": 0.99},
            {"recommendation": "reject"},
            {"confidence": 0.1},
            {"dimension_scores": {"correctness": 0.1}},
            {"unsafe_behavior": ["prompt injection attempt"]},
    ):
        other = import_response(packet, good_response(packet, **mutation))
        assert other.review_hash() != base


def test_unknown_review_field_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="unknown field"):
        import_response(packet, good_response(packet, override=True))


def test_missing_required_review_field_is_refused():
    packet = make_packet()
    payload = good_response(packet)
    payload.pop("recommendation")
    with pytest.raises(ReviewImportError, match="missing required field"):
        import_response(packet, payload)


@pytest.mark.parametrize("field_name", ["task_hash", "attempt_hash",
                                        "deterministic_report_hash", "packet_hash"])
def test_binding_hash_mismatch_is_refused(field_name):
    packet = make_packet()
    payload = good_response(packet, **{field_name: "b" * 64})
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(packet, payload)


def test_packet_id_mismatch_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(packet, good_response(packet, packet_id="pkt-deadbeef"))


def test_provider_mismatch_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(packet, good_response(packet, provider="openai_cloud"))


def test_model_mismatch_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(packet, good_response(packet, model="gpt-3.5-turbo"))


def test_rubric_version_mismatch_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(packet, good_response(packet, rubric_version="m62.rubric.0"))


def test_provider_label_cannot_be_chosen_by_the_response():
    packet = make_packet(provider="mock_teacher")
    with pytest.raises(ReviewImportError, match="not a field a response gets to"):
        import_response(packet, good_response(packet),
                        provider_id="anthropic_cloud")


@pytest.mark.parametrize("field_name,value", [("provider", ""), ("model", ""),
                                              ("task_hash", "short")])
def test_a_malformed_frozen_field_is_reported_as_an_import_error(field_name, value):
    """The frozen record's validators raise the broader SchemaError; the importer must
    normalise it, or a caller catching ReviewImportError misses the failure."""
    packet = make_packet()
    with pytest.raises(ReviewImportError):
        import_response(packet, good_response(packet, **{field_name: value}))


@pytest.mark.parametrize("score", [1.5, -0.1, 2, "0.9", True, None])
def test_out_of_range_or_non_numeric_score_is_refused(score):
    packet = make_packet()
    with pytest.raises(ReviewImportError):
        import_response(packet, good_response(packet, overall_score=score))


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_non_finite_score_is_refused_on_import(literal):
    packet = make_packet()
    raw = json.dumps(good_response(packet)).replace('"overall_score": 0.8',
                                                    f'"overall_score": {literal}')
    with pytest.raises(ReviewImportError):
        import_response(packet, raw)


def test_unknown_recommendation_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="unknown recommendation"):
        import_response(packet, good_response(packet, recommendation="approve_now"))


def test_unknown_rubric_dimension_is_refused():
    packet = make_packet()
    with pytest.raises(ReviewImportError, match="unknown rubric dimension"):
        import_response(packet,
                        good_response(packet, dimension_scores={"vibes": 1.0}))


def test_secret_in_an_imported_review_is_refused():
    packet = make_packet()
    payload = good_response(packet,
                            factual_errors=["the token ghp_" + "b" * 36 + " is wrong"])
    with pytest.raises(ReviewImportError, match="private content"):
        import_response(packet, payload)


def test_private_host_path_in_an_imported_review_is_refused():
    packet = make_packet()
    payload = good_response(packet,
                            missing_evidence=[r"expected C:\Users\analyst\case.json"])
    with pytest.raises(ReviewImportError, match="private content"):
        import_response(packet, payload)


def test_hidden_reasoning_in_an_imported_review_is_refused():
    packet = make_packet()
    payload = good_response(packet,
                            corrected_answer="<think>the key is X</think>{\"a\":1}")
    with pytest.raises(ReviewImportError, match="private content"):
        import_response(packet, payload)


def test_altered_packet_invalidates_every_review_bound_to_it():
    packet = make_packet()
    tampered = ManualReviewPacket(
        packet_id=packet.packet_id, nonce=packet.nonce,
        created_at_utc=packet.created_at_utc,
        requested_provider=packet.requested_provider,
        requested_model=packet.requested_model, task_hash=packet.task_hash,
        attempt_hash=packet.attempt_hash,
        deterministic_report_hash=packet.deterministic_report_hash,
        task=dict(packet.task),
        attempt={**packet.attempt, "final_answer": "something else entirely"},
        deterministic_summary=dict(packet.deterministic_summary),
        packet_hash=packet.packet_hash)
    with pytest.raises(PacketError, match="modified after export"):
        parse_review_json(json.dumps(good_response(tampered)), packet=tampered,
                          provider_id=packet.requested_provider,
                          provider_version="m62.manual.1",
                          provider_kind=TeacherKind.MANUAL_PACKET,
                          mode=ReviewMode.OFFLINE_PACKET,
                          created_at_utc=NOW)


# ── 5. replay across subjects ─────────────────────────────────────────────────
def test_review_for_one_attempt_cannot_be_replayed_onto_another():
    spec = make_spec()
    first = make_trajectory(spec, attempt_number=1)
    second = make_trajectory(spec, attempt_number=2)
    first_packet = make_packet(spec, first, nonce="n-a")
    second_packet = make_packet(spec, second, nonce="n-b")
    assert first_packet.attempt_hash != second_packet.attempt_hash
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(second_packet, good_response(first_packet))


def test_review_for_one_task_cannot_be_replayed_onto_another():
    first_spec = make_spec(task_id="report-001")
    second_spec = make_spec(task_id="report-002")
    first_packet = make_packet(first_spec, nonce="n-c")
    second_packet = make_packet(second_spec, nonce="n-d")
    assert first_packet.task_hash != second_packet.task_hash
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(second_packet, good_response(first_packet))


def test_review_written_before_the_graders_changed_no_longer_binds():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    old = make_packet(spec, trajectory, nonce="n-e")
    weaker = [GraderResult(grader_id="json_schema", grader_version="t", score=0.5,
                           status=ResultStatus.PASS, non_vacuous_measurement=1),
              GraderResult(grader_id="secret_pii", grader_version="t", score=1.0,
                           status=ResultStatus.PASS, non_vacuous_measurement=7)]
    fresh = make_packet(spec, trajectory, nonce="n-e", results=weaker)
    assert old.deterministic_report_hash != fresh.deterministic_report_hash
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(fresh, good_response(old))


def test_a_consumed_packet_cannot_be_used_twice():
    packet = make_packet()
    ledger = InMemoryPacketLedger()
    record = import_response(packet, good_response(packet))
    register_review(record, ledger=ledger)
    assert ledger.is_consumed(packet.packet_id)
    with pytest.raises(ReplayRejected, match="one packet buys one opinion"):
        register_review(record, ledger=ledger)


def test_import_without_a_replay_ledger_is_refused():
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    with pytest.raises(ReviewImportError, match="without replay protection"):
        register_review(record, ledger=object())


def test_packet_id_is_deterministic_for_one_request_and_differs_per_nonce():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    first = make_packet(spec, trajectory, nonce="n-same")
    again = make_packet(spec, trajectory, nonce="n-same")
    other = make_packet(spec, trajectory, nonce="n-other")
    assert first.packet_id == again.packet_id
    assert first.packet_hash == again.packet_hash
    assert other.packet_id != first.packet_id


def test_two_providers_get_distinct_packets_for_one_attempt():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    a = make_packet(spec, trajectory, provider="openai_cloud", model="gpt-5-thinking",
                    nonce="n-1")
    b = make_packet(spec, trajectory, provider="anthropic_cloud",
                    model="claude-opus-5", nonce="n-1")
    assert a.packet_id != b.packet_id
    with pytest.raises(ReviewImportError, match="different subject"):
        import_response(b, good_response(a), provider_id="anthropic_cloud")


# ── 6. storage ────────────────────────────────────────────────────────────────
def test_store_writes_atomically_and_records_a_manifest(tmp_path: Path):
    store = TeacherArtifactStore(tmp_path / "teachers")
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    packet_rel = store.write_packet(packet)
    review_rel = store.write_review(record)
    assert packet_rel == f"packets/{packet.packet_id}.json"
    assert review_rel.startswith("reviews/")
    stored = json.loads((store.root / review_rel).read_text(encoding="utf-8"))
    assert stored["review_hash"] == record.review_hash()
    manifest = {entry["path"]: entry for entry in store.manifest()}
    assert manifest[packet_rel]["sha256"] == sha256_text(
        (store.root / packet_rel).read_text(encoding="utf-8"))
    # No temp file survives a successful write.
    assert not list((store.root / "reviews").glob(".tmp-*"))


def test_store_refuses_an_unsafe_identifier(tmp_path: Path):
    store = TeacherArtifactStore(tmp_path / "teachers")
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    with pytest.raises(StoreError):
        store._write_json("reviews", "../escape.json", record.to_dict())
    with pytest.raises(StoreError, match="unknown directory"):
        store._write_json("..", "x.json", record.to_dict())


def test_store_refuses_an_oversized_record(tmp_path: Path):
    store = TeacherArtifactStore(tmp_path / "teachers")
    with pytest.raises(StoreError, match="ceiling"):
        store._write_json("rejected", "big.json", {"blob": "a" * 300_000})


def test_store_refuses_to_persist_a_secret(tmp_path: Path):
    store = TeacherArtifactStore(tmp_path / "teachers")
    with pytest.raises(SanitizationError, match="private content"):
        store.write_rejected(packet_id="pkt-1",
                             reason="AKIA" + "A" * 16 + " was in the response")


def test_audit_events_are_append_only_and_scanned(tmp_path: Path):
    store = TeacherArtifactStore(tmp_path / "teachers")
    store.record_audit_event({"event": "cloud_call", "provider": "openai_cloud",
                              "authorized": False})
    store.record_audit_event({"event": "cloud_call", "provider": "openai_cloud",
                              "authorized": True})
    assert len(store.audit_events()) == 2
    with pytest.raises(SanitizationError):
        store.record_audit_event({"event": "leak",
                                  "detail": "Bearer abcdefghijklmnopqrstuvwx"})


def test_file_backed_ledger_survives_a_new_instance(tmp_path: Path):
    path = tmp_path / "consumed.jsonl"
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    register_review(record, ledger=PacketLedger(path))
    assert PacketLedger(path).is_consumed(packet.packet_id)
    with pytest.raises(ReplayRejected):
        register_review(record, ledger=PacketLedger(path))


def test_ledger_tolerates_a_torn_final_line(tmp_path: Path):
    path = tmp_path / "consumed.jsonl"
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    register_review(record, ledger=PacketLedger(path))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"packet_id": "pkt-tor')
    assert PacketLedger(path).is_consumed(packet.packet_id)


def test_import_reads_no_credential_and_opens_no_socket(monkeypatch):
    """A negative control: the import path must be pure validation.

    ``os.environ.get`` and ``socket.socket`` are replaced with functions that fail the
    test if called, which is stronger than asserting on what the code looks like."""
    import os as os_module
    import socket as socket_module

    def _forbidden_env(*_a, **_kw):
        raise AssertionError("review import read the environment")

    class _ForbiddenSocket:
        def __init__(self, *_a, **_kw) -> None:
            raise AssertionError("review import opened a socket")

    packet = make_packet()
    payload = good_response(packet)
    monkeypatch.setattr(os_module.environ, "get", _forbidden_env)
    monkeypatch.setattr(socket_module, "socket", _ForbiddenSocket)
    record = parse_review_json(
        json.dumps(payload), packet=packet,
        provider_id=packet.requested_provider, provider_version="m62.manual.1",
        provider_kind=TeacherKind.MANUAL_PACKET,
        mode=ReviewMode.OFFLINE_PACKET, created_at_utc=NOW)
    assert record.packet_id == packet.packet_id


# ── 7. a review can never outrank the evidence ───────────────────────────────
def test_a_teacher_record_never_claims_dataset_approval():
    packet = make_packet()
    record = import_response(packet, good_response(packet, recommendation="approve"))
    assert record.approves_dataset is False
    assert not hasattr(record, "approve")
    assert not hasattr(record, "promote")


def test_a_record_whose_two_halves_disagree_is_refused():
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    payload = record.to_dict()
    payload["model"] = "some-other-model"
    payload.pop("review_hash")
    with pytest.raises(Exception, match="disagree"):
        TeacherReviewRecord.from_dict(payload)


def test_a_tampered_record_digest_is_refused():
    packet = make_packet()
    record = import_response(packet, good_response(packet))
    payload = record.to_dict()
    payload["review_hash"] = "c" * 64
    with pytest.raises(Exception, match="changed after it was signed"):
        TeacherReviewRecord.from_dict(payload)


def test_blocking_deterministic_evidence_still_reaches_the_packet():
    """A reviewer must be shown the failure, not a summary that hides it."""
    spec = make_spec()
    trajectory = make_trajectory(spec)
    failing = [
        GraderResult(grader_id="json_schema", grader_version="t", score=0.0,
                     status=ResultStatus.FAIL, non_vacuous_measurement=3),
        GraderResult(grader_id="secret_pii", grader_version="t", score=0.0,
                     status=ResultStatus.FAIL, blocking=True,
                     severity=Severity.BLOCKING, non_vacuous_measurement=7),
    ]
    packet = make_packet(spec, trajectory, nonce="n-fail", results=failing)
    summary = packet.deterministic_summary
    assert summary["eligible_for_review"] is False
    assert summary["approved"] is False
    assert summary["blocked"] is True
    assert any("secret_pii" in b for b in summary["blockers"])
    statuses = {r["grader_id"]: r["status"] for r in summary["results"]}
    assert statuses == {"json_schema": "fail", "secret_pii": "fail"}
