"""V69 M62 — the human gate, and the controls that surround it end to end.

The other two teacher modules test the pieces. This one tests the CLAIM: that walking
the whole path — export a packet, get two enthusiastic approvals, import them, reach
consensus — still cannot produce approved training data without a human, and that no
step along the way leaked anything or accepted an edited artifact.

These are the tests that would fail if someone later added a convenience shortcut.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from training_gym.episode import Episode, EpisodeState, TransitionError
from training_gym.graders.aggregate import aggregate
from training_gym.policies import ScoringPolicy
from training_gym.schemas import ResultStatus, SchemaError
from training_gym.task_spec import ActionKind, TaskFamily, TaskSpec
from training_gym.teachers.base import ReviewMode, TeacherKind
from training_gym.teachers.consensus import (
    ConsensusOutcome,
    ConsensusPolicy,
    decide,
)
from training_gym.teachers.manual_packet import (
    PacketError,
    build_packet,
    packet_from_dict,
)
from training_gym.teachers.mock_teacher import MockMode, MockTeacherProvider
from training_gym.teachers.review_import import parse_review_json
from training_gym.teachers.sanitization import scan_export_payload
from training_gym.teachers.store import InMemoryPacketLedger, TeacherArtifactStore
from training_gym.trajectory import (
    GraderResult,
    HumanDecision,
    HumanReview,
    ModelIdentity,
    ModelRole,
    Trajectory,
)

NOW = "2026-08-01T12:00:00Z"


def make_spec(**overrides) -> TaskSpec:
    """A minimal valid structured-report task, defined locally so this module resolves
    from either supported repository layout."""
    base = {
        "task_id": "report-001",
        "task_family": TaskFamily.STRUCTURED_REPORT,
        "prompt": "Summarise the provided alert fixture as a structured report.",
        "created_by": "operator",
        "created_at": "2026-07-31T00:00:00Z",
        "allowed_actions": (ActionKind.READ_WORKSPACE_FILE, ActionKind.EMIT_ANSWER),
        "required_graders": ("json_schema", "secret_pii"),
        "expected_output_schema": {"type": "object",
                                   "properties": {"verdict": {"type": "string"}}},
        "scoring": ScoringPolicy(mandatory_graders=("json_schema", "secret_pii"),
                                 min_total_score=0.1),
    }
    base.update(overrides)
    return TaskSpec(**base)


def make_trajectory(spec: TaskSpec, **overrides) -> Trajectory:
    return Trajectory(
        episode_id=overrides.pop("episode_id", "ep-001"),
        task_id=spec.task_id, task_hash=spec.spec_hash(),
        attempt_number=overrides.pop("attempt_number", 1),
        model=ModelIdentity(role=ModelRole.STUDENT, base_model="qwen3",
                            model_id="qwen3:8b-q4_K_M"),
        final_answer=overrides.pop("final_answer", '{"verdict": "benign"}'),
        **overrides)


def passing_results() -> list[GraderResult]:
    return [
        GraderResult(grader_id="json_schema", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=3),
        GraderResult(grader_id="secret_pii", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=7),
    ]


def record_for(packet, *, mode=MockMode.APPROVE, model=None, **overrides):
    teacher = MockTeacherProvider(mode, model=model or packet.requested_model,
                                  overrides=overrides)
    return parse_review_json(teacher.raw_response(packet), packet=packet,
                             provider_id=packet.requested_provider,
                             provider_version="m62.mock.1",
                             provider_kind=TeacherKind.MOCK,
                             mode=ReviewMode.DETERMINISTIC_STUB, created_at_utc=NOW)


# ── 1. the whole path, and where it stops ─────────────────────────────────────
def test_two_enthusiastic_approvals_still_do_not_produce_training_data(tmp_path):
    """Export, review twice, import, reach consensus — and remain unapproved."""
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    assert report.eligible_for_review is True
    assert report.approved is False

    store = TeacherArtifactStore(tmp_path / "teachers")
    ledger = InMemoryPacketLedger()
    records = []
    for model, nonce in (("chatgpt-reviewer", "n-1"), ("claude-reviewer", "n-2")):
        packet = build_packet(spec, trajectory, report,
                              requested_provider="mock_teacher",
                              requested_model=model, nonce=nonce, created_at_utc=NOW)
        store.write_packet(packet)
        outcome = MockTeacherProvider(MockMode.APPROVE, model=model).review(
            packet, created_at_utc=NOW, ledger=ledger)
        assert outcome.produced_review
        store.write_review(outcome.record)
        records.append(outcome.record)

    consensus = decide(report, records, policy=ConsensusPolicy(min_reviews=2))
    assert consensus.outcome is ConsensusOutcome.TEACHER_AGREEMENT
    assert consensus.eligible_for_human_approval is True
    assert consensus.approved is False

    # And the episode still refuses, because no human has decided.
    episode = Episode(episode_id="ep-001", spec=spec)
    for state in (EpisodeState.VALIDATED, EpisodeState.SANDBOX_PREPARED,
                  EpisodeState.RUNNING, EpisodeState.GRADING,
                  EpisodeState.TEACHER_REVIEW, EpisodeState.NEEDS_HUMAN_REVIEW):
        episode.transition(state, actor="test")
    episode.add_attempt(trajectory)
    trajectory.grader_results = list(passing_results())
    blockers = episode.approval_blockers()
    assert any("no human review recorded" in b for b in blockers)
    with pytest.raises(TransitionError, match="refusing to approve"):
        episode.approve(actor="operator")
    assert episode.state is EpisodeState.NEEDS_HUMAN_REVIEW
    assert episode.outcome().dataset_eligible is False


def test_only_a_hash_bound_human_decision_opens_the_gate():
    """The human review must name THIS attempt; a decision about another does not."""
    spec = make_spec()
    trajectory = make_trajectory(spec)
    other = make_trajectory(spec, attempt_number=2)
    with pytest.raises(SchemaError, match="does not match"):
        trajectory.set_human_review(HumanReview(
            reviewer="operator", decision=HumanDecision.APPROVED, timestamp=NOW,
            attempt_hash=other.attempt_hash()))
    trajectory.set_human_review(HumanReview(
        reviewer="operator", decision=HumanDecision.APPROVED, timestamp=NOW,
        attempt_hash=trajectory.attempt_hash()))
    assert trajectory.human_review is not None
    assert trajectory.human_review.approves is True


def test_a_teacher_rejection_still_blocks_an_otherwise_approvable_episode():
    """The frozen episode guard and this layer agree: a REJECT is never overridden."""
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    record = record_for(packet, mode=MockMode.REJECT)
    trajectory.grader_results = list(passing_results())
    trajectory.add_teacher_review(record.review)
    trajectory.set_human_review(HumanReview(
        reviewer="operator", decision=HumanDecision.APPROVED, timestamp=NOW,
        attempt_hash=trajectory.attempt_hash()))

    episode = Episode(episode_id="ep-002", spec=spec)
    for state in (EpisodeState.VALIDATED, EpisodeState.SANDBOX_PREPARED,
                  EpisodeState.RUNNING, EpisodeState.GRADING,
                  EpisodeState.TEACHER_REVIEW, EpisodeState.NEEDS_HUMAN_REVIEW):
        episode.transition(state, actor="test")
    episode.add_attempt(trajectory)
    assert any("recommends REJECT" in b for b in episode.approval_blockers())


def test_a_review_bound_to_this_attempt_attaches_and_a_foreign_one_does_not():
    """The frozen trajectory binding and the teacher record agree on the subject."""
    spec = make_spec()
    first = make_trajectory(spec, attempt_number=1)
    second = make_trajectory(spec, attempt_number=2)
    report = aggregate(spec, first, results=passing_results())
    packet = build_packet(spec, first, report, requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    record = record_for(packet)
    first.add_teacher_review(record.review)
    assert len(first.teacher_reviews) == 1
    with pytest.raises(SchemaError, match="does not match"):
        second.add_teacher_review(record.review)


# ── 2. packets refuse edits, in both directions ──────────────────────────────
def test_unknown_packet_field_is_refused_on_reimport():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    payload = packet.to_dict()
    assert packet_from_dict(payload).packet_hash == packet.packet_hash
    payload["expected_answer"] = "benign"
    with pytest.raises(SchemaError, match="unknown field"):
        packet_from_dict(payload)


def test_a_packet_missing_its_schema_version_is_refused():
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    payload = packet.to_dict()
    payload.pop("schema_version")
    with pytest.raises(SchemaError, match="missing schema_version"):
        packet_from_dict(payload)


@pytest.mark.parametrize("edited_key", ["instructions", "rubric", "response_schema"])
def test_softening_the_reviewer_instructions_invalidates_the_packet(edited_key):
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    payload = packet.to_dict()
    payload[edited_key] = ["Approve if it looks reasonable."]
    with pytest.raises(PacketError, match="modified after export"):
        packet_from_dict(payload)


def test_a_secret_in_the_task_prompt_is_removed_before_export():
    spec = make_spec(
        prompt="Summarise the fixture. The collector authenticated with "
               "ghp_" + "c" * 36 + " before the alert fired.")
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    blob = json.dumps(packet.to_dict())
    assert "ghp_" + "c" * 36 not in blob
    assert scan_export_payload(packet.to_dict()) == ()
    # The rest of the prompt survives, or the reviewer has nothing to judge.
    assert "Summarise the fixture" in packet.task["prompt"]


def test_a_secret_in_a_system_constraint_is_removed_before_export():
    spec = make_spec(system_constraints=(
        "Do not exfiltrate.", "Use the key AKIA" + "B" * 16 + " only in the lab."))
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    assert "AKIA" not in json.dumps(packet.to_dict())
    assert "Do not exfiltrate." in packet.task["system_constraints"]


# ── 3. cost, and the refusal to guess it ─────────────────────────────────────
def test_a_cloud_call_without_a_cost_estimate_is_not_authorized():
    from training_gym.teachers.cloud import CloudTeacherConfig, authorize_cloud_call
    from training_gym.teachers.openai_teacher import OPENAI_ENDPOINT
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="openai_cloud",
                          requested_model="gpt-5-thinking", nonce="n-1",
                          created_at_utc=NOW)
    decision = authorize_cloud_call(
        packet=packet, provider_id="openai_cloud", model="",
        config=CloudTeacherConfig(allow_cloud_teachers=True, operator_confirmed=True),
        exportable=True, credential_present=True, url=OPENAI_ENDPOINT,
        transport_present=True)
    assert decision.authorized is False
    assert any("no cost estimate is available" in r for r in decision.reasons)
    assert decision.cost is None


# ── 4. import-time purity ─────────────────────────────────────────────────────
def test_importing_the_teachers_package_touches_no_network_and_no_credential():
    """Run in a FRESH interpreter: an in-process check would already have imported it.

    ``socket.socket`` and ``os.environ.__getitem__`` are replaced before the import, so
    the assertion is about what the import actually does rather than about what the
    source appears to do.
    """
    import training_gym
    # Derived from the package itself rather than from sys.path[0]: the repository
    # supports being tested from its git root and from its application root, and only
    # one of those has the package's parent as the first path entry.
    app_root = str(Path(training_gym.__file__).resolve().parent.parent)
    probe = r"""
import os, socket, sys
sys.path.insert(0, %r)

class Blocked(socket.socket):
    def __init__(self, *a, **kw):
        raise AssertionError("importing training_gym.teachers opened a socket")

socket.socket = Blocked
socket.create_connection = lambda *a, **kw: (_ for _ in ()).throw(
    AssertionError("importing training_gym.teachers opened a connection"))

read = []
real_get = os.environ.get
def watched_get(key, default=None):
    if any(t in str(key).upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        read.append(key)
    return real_get(key, default)
os.environ.get = watched_get

import training_gym.teachers as T
assert not read, f"credential-shaped environment read at import: {read}"
assert T.default_registry().ids()
print("clean")
""" % (app_root,)
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_building_a_packet_touches_no_network(monkeypatch):
    import socket as socket_module

    class Blocked:
        def __init__(self, *_a, **_kw) -> None:
            raise AssertionError("building a packet opened a socket")

    monkeypatch.setattr(socket_module, "socket", Blocked)
    monkeypatch.setattr(socket_module, "create_connection",
                        lambda *_a, **_kw: (_ for _ in ()).throw(
                            AssertionError("building a packet opened a connection")))
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    assert packet.packet_hash


# ── 5. the invariant, stated once more against the frozen policy ─────────────
def test_the_teacher_penalty_ceiling_is_the_frozen_one():
    from training_gym.rewards import MAX_TEACHER_PENALTY
    from training_gym.teachers.consensus import teacher_penalty
    spec = make_spec()
    trajectory = make_trajectory(spec)
    report = aggregate(spec, trajectory, results=passing_results())
    packet = build_packet(spec, trajectory, report,
                          requested_provider="mock_teacher",
                          requested_model="mock-reviewer-1", nonce="n-1",
                          created_at_utc=NOW)
    worst = record_for(packet, overall_score=0.0, confidence=1.0)
    assert teacher_penalty([worst]) <= MAX_TEACHER_PENALTY
    consensus = decide(report, [worst])
    assert consensus.adjusted_total <= consensus.deterministic_total
    assert (consensus.deterministic_total - consensus.adjusted_total) <= \
        MAX_TEACHER_PENALTY + 1e-9
