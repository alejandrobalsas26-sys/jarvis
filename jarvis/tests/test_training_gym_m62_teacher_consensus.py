"""V69 M62 — teacher providers, cloud authorization gates and consensus routing.

The claim under test throughout: a teacher can lower confidence and ask for a human, and
nothing else. So these tests drive the real providers, the real gate and the real
consensus engine, and assert on what they refuse — a fabricated review when a runtime is
absent, a paid call without every condition, a score that went up, an approval that was
never a human's.

No test here reaches a network, reads a real credential or performs a paid call. The
cloud providers are exercised through a transport double that FAILS the test if it is
ever asked to send something the gate should have refused.
"""
from __future__ import annotations

import json

import pytest

from training_gym.graders.aggregate import aggregate
from training_gym.rewards import MAX_TEACHER_PENALTY
from training_gym.schemas import ResultStatus, Severity
from training_gym.task_spec import ActionKind, TaskFamily, TaskSpec
from training_gym.teachers.anthropic_teacher import (
    ANTHROPIC_CREDENTIAL_ENV,
    ANTHROPIC_ENDPOINT,
    OptionalAnthropicTeacherProvider,
)
from training_gym.teachers.base import (
    ReviewMode,
    TeacherError,
    TeacherKind,
    TeacherProvider,
)
from training_gym.teachers.cloud import (
    ALLOWED_CLOUD_HOSTS,
    CloudRequest,
    CloudResponse,
    CloudTeacherConfig,
    CloudTransport,
    audit_event,
    authorize_cloud_call,
    estimate_cost,
)
from training_gym.teachers.consensus import (
    ConsensusOutcome,
    ConsensusPolicy,
    decide,
    teacher_adjusted_total,
    teacher_penalty,
)
from training_gym.teachers.manual_packet import build_packet
from training_gym.teachers.mock_teacher import MockMode, MockTeacherProvider
from training_gym.teachers.openai_teacher import (
    OPENAI_CREDENTIAL_ENV,
    OPENAI_ENDPOINT,
    OptionalOpenAITeacherProvider,
)
from training_gym.teachers.registry import (
    TEACHER_PROVIDER_IDS,
    ProviderEntry,
    RegistryError,
    TeacherRegistry,
    default_registry,
)
from training_gym.teachers.review_import import parse_review_json
from training_gym.teachers.store import InMemoryPacketLedger
from training_gym.teachers.verifier_teacher import VerifierTeacherProvider
from training_gym.trajectory import (
    GraderResult,
    ModelIdentity,
    ModelRole,
    Recommendation,
    Trajectory,
)

NOW = "2026-08-01T12:00:00Z"


# ── local builders ────────────────────────────────────────────────────────────
def make_spec(**overrides) -> TaskSpec:
    """A minimal valid structured-report task, defined locally so this module resolves
    from either supported repository layout."""
    from training_gym.policies import ScoringPolicy
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


def blocking_results() -> list[GraderResult]:
    return [
        GraderResult(grader_id="json_schema", grader_version="t", score=1.0,
                     status=ResultStatus.PASS, non_vacuous_measurement=3),
        GraderResult(grader_id="secret_pii", grader_version="t", score=0.0,
                     status=ResultStatus.FAIL, blocking=True,
                     severity=Severity.BLOCKING, non_vacuous_measurement=7),
    ]


def make_report(results=None, spec=None, trajectory=None):
    spec = spec or make_spec()
    trajectory = trajectory if trajectory is not None else make_trajectory(spec)
    return spec, trajectory, aggregate(
        spec, trajectory, results=results if results is not None else passing_results())


def make_packet(*, provider="mock_teacher", model="mock-reviewer-1", nonce="n-1",
                results=None, spec=None, trajectory=None):
    spec, trajectory, report = make_report(results=results, spec=spec,
                                           trajectory=trajectory)
    return build_packet(spec, trajectory, report, requested_provider=provider,
                        requested_model=model, nonce=nonce, created_at_utc=NOW)


def record_from(packet, *, mode=MockMode.APPROVE, provider="mock_teacher",
                model="mock-reviewer-1", **overrides):
    """Produce a real record through the real provider path."""
    teacher = MockTeacherProvider(mode, model=model, overrides=overrides)
    return parse_review_json(teacher.raw_response(packet), packet=packet,
                             provider_id=provider, provider_version="m62.mock.1",
                             provider_kind=TeacherKind.MOCK,
                             mode=ReviewMode.DETERMINISTIC_STUB, created_at_utc=NOW)


# ── transport doubles ─────────────────────────────────────────────────────────
class ForbiddenTransport(CloudTransport):
    """Fails the test if it is ever asked to send. The default for every gate test."""

    def send(self, request: CloudRequest, *, api_key: str) -> CloudResponse:
        raise AssertionError(f"a live call to {request.url} was attempted; the gate "
                             f"should have refused it")


class RecordingTransport(CloudTransport):
    """Records what would have been sent and returns a canned envelope."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.requests: list[CloudRequest] = []
        self.keys_seen: list[str] = []

    def send(self, request: CloudRequest, *, api_key: str) -> CloudResponse:
        self.requests.append(request)
        self.keys_seen.append(api_key)
        return CloudResponse(status=200, text=self.body)


def enabled_config(**kw) -> CloudTeacherConfig:
    base = {"allow_cloud_teachers": True, "operator_confirmed": True,
            "max_cost_usd": 5.0}
    base.update(kw)
    return CloudTeacherConfig(**base)


# ── 1. the mock provider, and its confinement ────────────────────────────────
def test_mock_provider_is_deterministic_and_needs_an_explicit_mode():
    packet = make_packet()
    first = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW)
    second = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW)
    assert first.produced_review and second.produced_review
    assert first.record.review_hash() == second.record.review_hash()
    with pytest.raises(TypeError):
        MockTeacherProvider()  # type: ignore[call-arg]
    with pytest.raises(TeacherError, match="must be a MockMode"):
        MockTeacherProvider("approve")  # type: ignore[arg-type]


def test_seeded_variation_is_a_pure_function_of_the_seed():
    packet = make_packet()
    a = MockTeacherProvider(MockMode.SEEDED_VARIATION, seed=1).review(
        packet, created_at_utc=NOW)
    b = MockTeacherProvider(MockMode.SEEDED_VARIATION, seed=1).review(
        packet, created_at_utc=NOW)
    c = MockTeacherProvider(MockMode.SEEDED_VARIATION, seed=2).review(
        packet, created_at_utc=NOW)
    assert a.record.review.overall_score == b.record.review.overall_score
    assert a.record.review.overall_score != c.record.review.overall_score


@pytest.mark.parametrize("mode,status", [
    (MockMode.MALFORMED_JSON, ResultStatus.ERROR),
    (MockMode.UNKNOWN_FIELD, ResultStatus.ERROR),
    (MockMode.WRONG_TASK, ResultStatus.ERROR),
    (MockMode.WRONG_ATTEMPT, ResultStatus.ERROR),
    (MockMode.STALE_REPORT, ResultStatus.ERROR),
    (MockMode.OUT_OF_RANGE, ResultStatus.ERROR),
    (MockMode.NOT_FINITE, ResultStatus.ERROR),
    (MockMode.UNKNOWN_RECOMMENDATION, ResultStatus.ERROR),
    (MockMode.SECRET_BEARING, ResultStatus.ERROR),
    (MockMode.TIMEOUT, ResultStatus.SKIPPED),
    (MockMode.UNAVAILABLE, ResultStatus.SKIPPED),
])
def test_every_misbehaviour_produces_a_non_answer_never_a_review(mode, status):
    packet = make_packet()
    outcome = MockTeacherProvider(mode).review(packet, created_at_utc=NOW)
    assert outcome.status is status
    assert outcome.record is None
    assert outcome.produced_review is False
    assert outcome.reason


def test_a_wrong_model_answer_is_refused_rather_than_relabelled():
    packet = make_packet()
    outcome = MockTeacherProvider(MockMode.WRONG_MODEL).review(packet,
                                                               created_at_utc=NOW)
    assert outcome.status is ResultStatus.ERROR
    assert "different subject" in outcome.reason


def test_a_secret_bearing_response_is_refused_and_never_stored_raw():
    packet = make_packet()
    outcome = MockTeacherProvider(MockMode.SECRET_BEARING).review(packet,
                                                                  created_at_utc=NOW)
    assert outcome.status is ResultStatus.ERROR
    assert "ghp_" not in outcome.response_excerpt
    assert "ghp_" not in json.dumps(outcome.to_dict())
    # The digest of what came back is kept; the content is not.
    assert len(outcome.response_sha256) == 64


def test_a_provider_refuses_a_packet_addressed_to_another_provider():
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    outcome = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW)
    assert outcome.status is ResultStatus.SKIPPED
    assert "addressed to another provider" in outcome.reason


def test_a_provider_refuses_an_unsupported_review_mode():
    packet = make_packet()
    outcome = MockTeacherProvider(MockMode.APPROVE).review(
        packet, created_at_utc=NOW, mode=ReviewMode.CLOUD_LIVE)
    assert outcome.status is ResultStatus.SKIPPED
    assert "does not support review mode" in outcome.reason


def test_an_outcome_records_whether_replay_protection_was_in_effect():
    """Importing without a ledger is legitimate; leaving it ambiguous is not."""
    packet = make_packet()
    without = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW)
    assert without.audit["replay_ledger"] is False
    fresh = make_packet(nonce="n-2")
    with_ledger = MockTeacherProvider(MockMode.APPROVE).review(
        fresh, created_at_utc=NOW, ledger=InMemoryPacketLedger())
    assert with_ledger.audit["replay_ledger"] is True


def test_a_refusal_reason_never_carries_a_token_the_response_chose():
    """A validation message quotes the offending FIELD NAME, and a field name is
    attacker-chosen — so the reason is sanitized before it reaches an outcome."""
    packet = make_packet()
    leaking_field = "ghp_" + "d" * 36
    outcome = MockTeacherProvider(
        MockMode.APPROVE, overrides={leaking_field: 1}).review(packet,
                                                               created_at_utc=NOW)
    assert outcome.status is ResultStatus.ERROR
    assert leaking_field not in outcome.reason
    assert leaking_field not in json.dumps(outcome.to_dict())


def test_the_provider_path_consumes_a_packet_exactly_once():
    packet = make_packet()
    ledger = InMemoryPacketLedger()
    first = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW,
                                                         ledger=ledger)
    assert first.produced_review
    second = MockTeacherProvider(MockMode.APPROVE).review(packet, created_at_utc=NOW,
                                                          ledger=ledger)
    assert second.status is ResultStatus.ERROR
    assert "already answered" in second.reason


# ── 2. the local verifier ─────────────────────────────────────────────────────
def test_verifier_is_unavailable_without_a_wired_runtime():
    provider = VerifierTeacherProvider(model="qwen3:8b")
    state = provider.availability()
    assert state.available is False
    assert "never starts one" in state.reason
    packet = make_packet(provider="verifier_local", model="qwen3:8b")
    outcome = provider.review(packet, created_at_utc=NOW)
    assert outcome.status is ResultStatus.SKIPPED
    assert outcome.record is None


def test_verifier_requires_a_resolved_model_not_an_alias():
    with pytest.raises(TeacherError, match="resolved model id"):
        VerifierTeacherProvider(model="")


def test_verifier_produces_a_review_through_the_injected_seam():
    packet = make_packet(provider="verifier_local", model="qwen3:8b")
    body = MockTeacherProvider(MockMode.APPROVE).raw_response(packet)
    seen: list[tuple[str, int]] = []

    def seam(prompt: str, timeout_s: int) -> str:
        seen.append((prompt[:40], timeout_s))
        return body

    provider = VerifierTeacherProvider(model="qwen3:8b", verify_fn=seam)
    outcome = provider.review(packet, created_at_utc=NOW, mode=ReviewMode.LOCAL_LIVE)
    assert outcome.produced_review
    assert outcome.record.provider == "verifier_local"
    assert seen and seen[0][1] <= 90


def test_malformed_verifier_output_is_an_error_not_a_review():
    packet = make_packet(provider="verifier_local", model="qwen3:8b")
    provider = VerifierTeacherProvider(
        model="qwen3:8b", verify_fn=lambda _p, _t: "I think it is fine.")
    outcome = provider.review(packet, created_at_utc=NOW, mode=ReviewMode.LOCAL_LIVE)
    assert outcome.status is ResultStatus.ERROR
    assert outcome.record is None


def test_a_crashing_verifier_never_becomes_a_review():
    packet = make_packet(provider="verifier_local", model="qwen3:8b")

    def exploding(_prompt: str, _timeout: int) -> str:
        raise RuntimeError("ollama is not running")

    outcome = VerifierTeacherProvider(model="qwen3:8b", verify_fn=exploding).review(
        packet, created_at_utc=NOW, mode=ReviewMode.LOCAL_LIVE)
    assert outcome.status is ResultStatus.SKIPPED
    assert "local verifier failed" in outcome.reason


def test_a_verifier_that_returns_a_dict_is_refused():
    packet = make_packet(provider="verifier_local", model="qwen3:8b")
    outcome = VerifierTeacherProvider(
        model="qwen3:8b",
        verify_fn=lambda _p, _t: {"recommendation": "approve"},  # type: ignore[return-value]
    ).review(packet, created_at_utc=NOW, mode=ReviewMode.LOCAL_LIVE)
    assert outcome.status is ResultStatus.ERROR
    assert "must arrive as text" in outcome.reason


# ── 3. cloud: off by default, and every gate ─────────────────────────────────
@pytest.mark.parametrize("provider_cls,env_name,endpoint", [
    (OptionalOpenAITeacherProvider, OPENAI_CREDENTIAL_ENV, OPENAI_ENDPOINT),
    (OptionalAnthropicTeacherProvider, ANTHROPIC_CREDENTIAL_ENV, ANTHROPIC_ENDPOINT),
])
def test_cloud_providers_are_disabled_by_default(provider_cls, env_name, endpoint):
    provider = provider_cls(model="some-model", transport=ForbiddenTransport(),
                            credential_lookup=lambda _n: "unused")
    state = provider.availability()
    assert state.available is False
    assert "disabled by default" in state.reason
    assert endpoint.startswith("https://")
    packet = make_packet(provider=provider.provider_id, model="some-model")
    outcome = provider.review(packet, created_at_utc=NOW, mode=ReviewMode.CLOUD_LIVE)
    assert outcome.status is ResultStatus.SKIPPED
    assert outcome.record is None


@pytest.mark.parametrize("provider_cls,env_name", [
    (OptionalOpenAITeacherProvider, OPENAI_CREDENTIAL_ENV),
    (OptionalAnthropicTeacherProvider, ANTHROPIC_CREDENTIAL_ENV),
])
def test_cloud_provider_is_unavailable_without_a_credential(provider_cls, env_name):
    provider = provider_cls(model="some-model", config=enabled_config(),
                            transport=ForbiddenTransport(),
                            credential_lookup=lambda _n: None)
    state = provider.availability()
    assert state.available is False
    assert env_name in state.reason
    packet = make_packet(provider=provider.provider_id, model="some-model")
    assert provider.review(packet, created_at_utc=NOW,
                           mode=ReviewMode.CLOUD_LIVE).record is None


@pytest.mark.parametrize("provider_cls", [OptionalOpenAITeacherProvider,
                                          OptionalAnthropicTeacherProvider])
def test_cloud_provider_is_unavailable_without_a_transport(provider_cls):
    provider = provider_cls(model="some-model", config=enabled_config(),
                            transport=None, credential_lookup=lambda _n: "k")
    state = provider.availability()
    assert state.available is False
    assert "ships none" in state.reason


@pytest.mark.parametrize("provider_cls", [OptionalOpenAITeacherProvider,
                                          OptionalAnthropicTeacherProvider])
def test_cloud_provider_requires_an_explicit_model(provider_cls):
    with pytest.raises(TeacherError, match="explicit model is required"):
        provider_cls(model="")


def test_authorization_collects_every_unmet_condition():
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    decision = authorize_cloud_call(
        packet=packet, provider_id="openai_cloud", model="gpt-5-thinking",
        config=CloudTeacherConfig(), exportable=True, credential_present=False,
        url=OPENAI_ENDPOINT, transport_present=False)
    assert decision.authorized is False
    joined = " ".join(decision.reasons)
    assert "--allow-cloud-teachers" in joined
    assert "not confirmed by the operator" in joined
    assert "no API credential is reachable" in joined
    assert "no HTTPS transport" in joined


def test_authorization_passes_only_when_every_condition_holds():
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    decision = authorize_cloud_call(
        packet=packet, provider_id="openai_cloud", model="gpt-5-thinking",
        config=enabled_config(), exportable=True, credential_present=True,
        url=OPENAI_ENDPOINT, transport_present=True)
    assert decision.authorized is True
    assert decision.cost is not None and decision.cost.usd > 0
    for gate in ("cloud_flag", "operator_confirmation", "credential_available",
                 "destination_allowlisted", "cost_estimate",
                 "sanitization_and_secret_scan"):
        assert gate in decision.satisfied


def test_authorization_refuses_a_destination_outside_the_allowlist():
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    decision = authorize_cloud_call(
        packet=packet, provider_id="openai_cloud", model="gpt-5-thinking",
        config=enabled_config(), exportable=True, credential_present=True,
        url="https://evil.test/v1/chat/completions", transport_present=True)
    assert decision.authorized is False
    assert any("allowlisted" in r for r in decision.reasons)
    assert "api.openai.com" in ALLOWED_CLOUD_HOSTS


def test_authorization_refuses_an_estimate_over_the_preapproved_ceiling():
    packet = make_packet(provider="anthropic_cloud", model="claude-opus-5")
    decision = authorize_cloud_call(
        packet=packet, provider_id="anthropic_cloud", model="claude-opus-5",
        config=enabled_config(max_cost_usd=0.0001), exportable=True,
        credential_present=True, url=ANTHROPIC_ENDPOINT, transport_present=True)
    assert decision.authorized is False
    assert any("exceeds the pre-approved" in r for r in decision.reasons)


def test_a_cost_estimate_prices_an_unknown_model_at_the_most_expensive_row():
    known = estimate_cost("claude-haiku-4-5", 4_000)
    unknown = estimate_cost("some-model-nobody-listed", 4_000)
    assert unknown.usd > known.usd
    assert unknown.approximate is True
    assert unknown.price_table_version


def test_a_request_may_only_be_built_for_an_allowlisted_https_endpoint():
    from training_gym.teachers.base import TeacherNotAuthorized
    with pytest.raises(TeacherNotAuthorized, match="HTTPS"):
        CloudRequest(url="http://api.openai.com/v1/chat/completions", model="m",
                     payload={}, timeout_s=10, provider_id="openai_cloud")
    with pytest.raises(TeacherNotAuthorized, match="allowlist"):
        CloudRequest(url="https://evil.test/v1", model="m", payload={},
                     timeout_s=10, provider_id="openai_cloud")


@pytest.mark.parametrize("provider_cls,env_name", [
    (OptionalOpenAITeacherProvider, OPENAI_CREDENTIAL_ENV),
    (OptionalAnthropicTeacherProvider, ANTHROPIC_CREDENTIAL_ENV),
])
def test_the_request_body_grants_no_tool_and_no_computer_use(provider_cls, env_name):
    provider = provider_cls(model="gpt-5-thinking" if "openai" in env_name.lower()
                            else "claude-opus-5",
                            config=enabled_config(),
                            transport=ForbiddenTransport(),
                            credential_lookup=lambda _n: "k")
    packet = make_packet(provider=provider.provider_id, model=provider.model)
    payload = dict(provider.build_request(packet).payload)
    # Asserted on the request KEYS, not on the serialised blob: the packet prompt
    # legitimately contains the word "tools" in the instruction forbidding their use.
    granted = {str(k).lower() for k in payload}
    for capability in ("tools", "tool_choice", "functions", "function_call",
                       "computer", "bash", "code_interpreter", "file_search",
                       "browsing", "stream"):
        assert capability not in granted
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 1500


def test_the_credential_never_appears_in_a_request_or_an_audit_event():
    provider = OptionalOpenAITeacherProvider(
        model="gpt-5-thinking", config=enabled_config(),
        transport=ForbiddenTransport(), credential_lookup=lambda _n: "sk-do-not-leak")
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    request = provider.build_request(packet)
    assert "sk-do-not-leak" not in json.dumps(dict(request.payload))
    event = audit_event(provider_id="openai_cloud", model="gpt-5-thinking",
                        decision=provider.authorization(packet),
                        packet_id=packet.packet_id, response_sha256="a" * 64,
                        status=200)
    assert "sk-do-not-leak" not in json.dumps(event)
    assert event["authorized"] is True
    assert "api_key" not in event and "headers" not in event


def test_anthropic_headers_carry_the_version_but_never_the_credential():
    provider = OptionalAnthropicTeacherProvider(
        model="claude-opus-5", config=enabled_config(),
        transport=ForbiddenTransport(), credential_lookup=lambda _n: "sk-ant-secret")
    headers = provider.request_headers()
    assert headers["anthropic-version"]
    assert not any("sk-ant-secret" in v for v in headers.values())
    assert not any("key" in k.lower() or "auth" in k.lower() for k in headers)


def test_an_authorized_cloud_call_uses_the_transport_double_only():
    packet = make_packet(provider="openai_cloud", model="gpt-5-thinking")
    review_text = MockTeacherProvider(MockMode.APPROVE).raw_response(packet)
    transport = RecordingTransport(
        json.dumps({"choices": [{"message": {"content": review_text}}]}))
    provider = OptionalOpenAITeacherProvider(
        model="gpt-5-thinking", config=enabled_config(), transport=transport,
        credential_lookup=lambda _n: "sk-test-only")
    outcome = provider.review(packet, created_at_utc=NOW, mode=ReviewMode.CLOUD_LIVE)
    assert outcome.produced_review
    assert outcome.record.provider == "openai_cloud"
    assert len(transport.requests) == 1
    assert transport.requests[0].url == OPENAI_ENDPOINT
    assert outcome.cost is not None and outcome.cost.usd >= 0


def test_a_non_success_status_is_not_retried_and_never_switches_model():
    class Failing(CloudTransport):
        def __init__(self) -> None:
            self.calls = 0

        def send(self, request: CloudRequest, *, api_key: str) -> CloudResponse:
            self.calls += 1
            return CloudResponse(status=429, text="rate limited")

    transport = Failing()
    provider = OptionalAnthropicTeacherProvider(
        model="claude-opus-5", config=enabled_config(max_retries=2),
        transport=transport, credential_lookup=lambda _n: "k")
    packet = make_packet(provider="anthropic_cloud", model="claude-opus-5")
    outcome = provider.review(packet, created_at_utc=NOW, mode=ReviewMode.CLOUD_LIVE)
    assert outcome.status is ResultStatus.ERROR
    assert transport.calls == 1
    assert outcome.record is None


def test_no_provider_falls_back_to_another_when_it_cannot_run():
    """A negative control over the whole set: an unavailable provider produces a
    non-answer FROM ITSELF, never a review carrying a different provider's name."""
    packet_for = {
        "verifier_local": make_packet(provider="verifier_local", model="qwen3:8b"),
        "openai_cloud": make_packet(provider="openai_cloud", model="gpt-5-thinking"),
        "anthropic_cloud": make_packet(provider="anthropic_cloud",
                                       model="claude-opus-5"),
    }
    providers = [
        VerifierTeacherProvider(model="qwen3:8b"),
        OptionalOpenAITeacherProvider(model="gpt-5-thinking",
                                      transport=ForbiddenTransport()),
        OptionalAnthropicTeacherProvider(model="claude-opus-5",
                                         transport=ForbiddenTransport()),
    ]
    for provider in providers:
        outcome = provider.review(packet_for[provider.provider_id],
                                  created_at_utc=NOW,
                                  mode=provider.supported_modes[0])
        assert outcome.record is None
        assert outcome.provider == provider.provider_id
        assert outcome.status is ResultStatus.SKIPPED


# ── 4. the registry ───────────────────────────────────────────────────────────
def test_default_registry_is_closed_and_constructs_nothing_on_import():
    registry = default_registry()
    assert set(registry.ids()) == set(TEACHER_PROVIDER_IDS)
    described = {row["provider_id"]: row for row in registry.describe()}
    assert described["openai_cloud"]["is_cloud"] is True
    assert described["openai_cloud"]["cost_bearing"] is True
    assert described["verifier_local"]["is_cloud"] is False
    assert described["mock_teacher"]["test_only"] is True
    assert described["manual_packet"]["constructible"] is False


def test_registry_rejects_a_duplicate_id():
    registry = default_registry()
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(ProviderEntry(provider_id="openai_cloud",
                                        provider_class=OptionalOpenAITeacherProvider))


def test_registry_rejects_an_unknown_id():
    registry = TeacherRegistry()
    with pytest.raises(RegistryError, match="unknown provider id"):
        registry.register(ProviderEntry(provider_id="gemini-cloud",
                                        provider_class=None))
    with pytest.raises(RegistryError, match="no provider"):
        default_registry().entry("mistral_cloud")


def test_registry_refuses_the_mock_provider_in_normal_operation():
    registry = default_registry()
    with pytest.raises(RegistryError, match="test double"):
        registry.create("mock_teacher", mode=MockMode.APPROVE)
    provider = registry.create("mock_teacher", allow_test_providers=True,
                               mode=MockMode.APPROVE)
    assert provider.provider_id == "mock_teacher"


def test_registry_refuses_to_construct_the_manual_workflow():
    with pytest.raises(RegistryError, match="Export a packet instead"):
        default_registry().create("manual_packet")


def test_no_provider_exposes_an_execution_or_approval_surface():
    """The authority limit, asserted structurally rather than by reading the code."""
    forbidden = ("run", "execute", "exec", "call_tool", "invoke_tool", "shell",
                 "read_file", "write_file", "approve", "promote", "activate",
                 "write_dataset", "mutate", "train")
    for cls in (MockTeacherProvider, VerifierTeacherProvider,
                OptionalOpenAITeacherProvider, OptionalAnthropicTeacherProvider,
                TeacherProvider):
        for name in forbidden:
            assert not hasattr(cls, name), f"{cls.__name__} exposes {name}"


# ── 5. consensus ──────────────────────────────────────────────────────────────
def test_deterministic_failure_dominates_every_teacher_approval():
    spec, trajectory, report = make_report(results=blocking_results())
    packet = build_packet(
        spec, trajectory, aggregate(spec, trajectory, results=passing_results()),
        requested_provider="mock_teacher", requested_model="mock-reviewer-1",
        nonce="n-x", created_at_utc=NOW)
    approvals = [record_from(packet, mode=MockMode.APPROVE)]
    outcome = decide(report, approvals)
    assert outcome.outcome is ConsensusOutcome.DETERMINISTIC_REJECT
    assert outcome.approved is False
    assert outcome.eligible_for_human_approval is False
    assert outcome.adjusted_total == 0.0
    assert any("no teacher opinion is consulted" in r for r in outcome.reasons)


def test_a_review_bound_to_another_subject_quarantines_rather_than_counts():
    spec = make_spec()
    first = make_trajectory(spec, attempt_number=1)
    second = make_trajectory(spec, attempt_number=2)
    packet = make_packet(spec=spec, trajectory=first, nonce="n-1")
    other_report = aggregate(spec, second, results=passing_results())
    record = record_from(packet)
    outcome = decide(other_report, [record])
    assert outcome.outcome is ConsensusOutcome.QUARANTINED
    assert outcome.human_review_required is True
    assert outcome.eligible_for_human_approval is False


def test_missing_required_review_is_incomplete_not_favourable():
    _spec, _trajectory, report = make_report()
    outcome = decide(report, [],
                     policy=ConsensusPolicy(required_providers=("openai_cloud",
                                                                "anthropic_cloud")))
    assert outcome.outcome is ConsensusOutcome.TEACHER_REVIEW_INCOMPLETE
    assert outcome.missing_providers == ("anthropic_cloud", "openai_cloud")
    assert outcome.eligible_for_human_approval is False
    assert any("absent opinion is not a favourable one" in r for r in outcome.reasons)


def test_no_review_at_all_is_incomplete_even_with_clean_evidence():
    _spec, _trajectory, report = make_report()
    outcome = decide(report, [])
    assert outcome.outcome is ConsensusOutcome.TEACHER_REVIEW_INCOMPLETE
    assert outcome.eligible_for_human_approval is False


def test_two_approvals_lead_only_to_human_review_eligibility():
    spec, trajectory, report = make_report()
    packets = [make_packet(spec=spec, trajectory=trajectory, provider="mock_teacher",
                           model=model, nonce=nonce)
               for model, nonce in (("mock-reviewer-1", "n-1"),
                                    ("mock-reviewer-2", "n-2"))]
    # Two distinct reviewers, both approving.
    records = [record_from(p, model=p.requested_model) for p in packets]
    outcome = decide(report, records, policy=ConsensusPolicy(min_reviews=2))
    assert outcome.outcome is ConsensusOutcome.TEACHER_AGREEMENT
    assert outcome.eligible_for_human_approval is True
    assert outcome.human_review_required is True
    # Eligibility is not approval, in any spelling.
    assert outcome.approved is False
    assert outcome.to_dict()["approved"] is False


def test_disagreement_is_recorded_in_full_and_never_averaged():
    spec, trajectory, report = make_report()
    approve_packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    reject_packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-b",
                                model="mock-reviewer-2")
    approving = record_from(approve_packet, mode=MockMode.APPROVE, confidence=0.95)
    rejecting = record_from(reject_packet, mode=MockMode.REJECT,
                            model="mock-reviewer-2", confidence=0.05,
                            dimension_scores={"correctness": 0.05, "security": 0.9})
    outcome = decide(report, [approving, rejecting])
    assert outcome.outcome is ConsensusOutcome.TEACHER_DISAGREEMENT
    assert outcome.human_review_required is True
    detail = outcome.disagreement
    assert detail.present is True
    assert {rec for _p, rec in detail.recommendations} == {"approve", "reject"}
    assert detail.max_confidence_gap == pytest.approx(0.9, abs=1e-6)
    assert "correctness" in detail.dimensions_in_conflict
    # No averaged number anywhere claims the two reviewers roughly agreed.
    assert "average" not in json.dumps(outcome.to_dict())


def test_two_revisions_require_revision_and_two_rejections_do_not_approve():
    spec, trajectory, report = make_report()
    a = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    b = make_packet(spec=spec, trajectory=trajectory, nonce="n-b",
                    model="mock-reviewer-2")
    revising = decide(report, [record_from(a, mode=MockMode.REVISE),
                               record_from(b, mode=MockMode.REVISE,
                                           model="mock-reviewer-2")])
    assert revising.outcome is ConsensusOutcome.REVISION_REQUIRED
    assert revising.eligible_for_human_approval is False

    rejecting = decide(report, [record_from(a, mode=MockMode.REJECT),
                                record_from(b, mode=MockMode.REJECT,
                                            model="mock-reviewer-2")])
    assert rejecting.outcome is ConsensusOutcome.REJECTED
    assert rejecting.approved is False
    assert rejecting.eligible_for_human_approval is False
    assert rejecting.human_review_required is True  # escalation is the default


def test_all_deferrals_route_to_a_human():
    spec, trajectory, report = make_report()
    packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    outcome = decide(report, [record_from(packet, mode=MockMode.NEEDS_HUMAN)])
    assert outcome.outcome is ConsensusOutcome.ELIGIBLE_FOR_HUMAN_REVIEW
    assert outcome.human_review_required is True


def test_a_teacher_safety_flag_quarantines_rather_than_lowering_a_score():
    spec, trajectory, report = make_report()
    packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    flagged = record_from(packet, mode=MockMode.APPROVE,
                          unsafe_behavior=["proposed a destructive command"])
    outcome = decide(report, [flagged])
    assert outcome.outcome is ConsensusOutcome.QUARANTINED
    assert outcome.human_review_required is True
    assert any("never scored away" in r for r in outcome.reasons)


# ── 6. teachers only subtract ─────────────────────────────────────────────────
def test_teacher_penalty_is_bounded_and_driven_by_the_worst_review():
    spec, trajectory, _report = make_report()
    a = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    b = make_packet(spec=spec, trajectory=trajectory, nonce="n-b",
                    model="mock-reviewer-2")
    generous = record_from(a, overall_score=1.0)
    harsh = record_from(b, model="mock-reviewer-2", overall_score=0.0)
    assert teacher_penalty([generous]) == 0.0
    assert teacher_penalty([harsh]) == pytest.approx(MAX_TEACHER_PENALTY)
    # The mean of 1.0 and 0.0 would be 0.5; the worst review drives the penalty.
    assert teacher_penalty([generous, harsh]) == pytest.approx(MAX_TEACHER_PENALTY)


@pytest.mark.parametrize("score", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_a_teacher_score_can_never_raise_the_deterministic_total(score):
    spec, trajectory, _report = make_report()
    packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    record = record_from(packet, overall_score=score, confidence=1.0)
    for deterministic in (0.0, 0.4, 1.0):
        adjusted = teacher_adjusted_total(deterministic, [record])
        assert adjusted <= deterministic
        assert adjusted >= 0.0


def test_consensus_refuses_a_report_whose_adjusted_total_would_exceed_the_evidence():
    from training_gym.teachers.consensus import ConsensusReport
    with pytest.raises(TeacherError, match="teachers may only subtract"):
        ConsensusReport(
            version="x", task_id="t", attempt_id="a" * 64,
            deterministic_report_hash="b" * 64,
            outcome=ConsensusOutcome.TEACHER_AGREEMENT, human_review_required=True,
            deterministic_total=0.5, teacher_penalty=0.0, adjusted_total=0.9,
            reasons=("test",))


def test_consensus_never_mutates_the_deterministic_report():
    spec, trajectory, report = make_report()
    before = json.dumps(report.to_dict(), sort_keys=True)
    packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    decide(report, [record_from(packet, mode=MockMode.REJECT)])
    assert json.dumps(report.to_dict(), sort_keys=True) == before
    assert report.report_hash()  # still computable, still the same evidence


def test_consensus_refuses_a_raw_dictionary_as_a_review():
    _spec, _trajectory, report = make_report()
    with pytest.raises(TeacherError, match="never an authoritative review"):
        decide(report, [{"recommendation": "approve"}])  # type: ignore[list-item]


def test_a_consensus_report_is_deterministically_hashable():
    spec, trajectory, report = make_report()
    packet = make_packet(spec=spec, trajectory=trajectory, nonce="n-a")
    record = record_from(packet)
    first = decide(report, [record])
    second = decide(report, [record])
    assert first.report_hash() == second.report_hash()
    assert Recommendation.APPROVE.value in json.dumps(first.to_dict())
