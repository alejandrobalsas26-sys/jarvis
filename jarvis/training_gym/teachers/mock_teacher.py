"""training_gym/teachers/mock_teacher.py — V69 M62: the provider that tests the gates.

WHY THIS EXISTS
---------------
Every negative control in the teacher layer needs a provider that misbehaves in one
exact, chosen way — a malformed answer, a review bound to the wrong attempt, a response
carrying a secret, a timeout, a second submission of the same review. Building those by
patching a real adapter would test the patch. Building them here, as declared modes,
tests the gates.

WHY IT CANNOT REACH PRODUCTION BY ACCIDENT
------------------------------------------
The mode is a REQUIRED constructor argument with no default, and the class is not
registered in the closed provider registry's default set. There is no configuration
value that selects it, no environment variable that enables it, and no code path in
which an unavailable real provider falls back to it — a fallback to a mock is a
fabricated review with a provider label on it.

DETERMINISM
-----------
No clock, no network, no model and no randomness. ``MockMode.SEEDED_VARIATION`` exists
for the one case where a test needs two different-looking reviews, and even that is a
pure function of an explicit integer seed.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum

from ..schemas import require_int, sha256_text
from .base import (
    RUBRIC_VERSION,
    ProviderResponse,
    ReviewMode,
    TeacherAvailability,
    TeacherError,
    TeacherKind,
    TeacherProvider,
    TeacherUnavailable,
)


class MockMode(str, Enum):
    """Exactly one way the mock behaves. Closed, and never inferred."""

    APPROVE = "approve"                    # a clean, well-formed approval
    REVISE = "revise"
    REJECT = "reject"
    NEEDS_HUMAN = "needs_human"
    SEEDED_VARIATION = "seeded_variation"  # deterministic scores from an explicit seed
    MALFORMED_JSON = "malformed_json"      # prose instead of an object
    UNKNOWN_FIELD = "unknown_field"        # a field nobody asked for
    WRONG_TASK = "wrong_task"              # bound to another task
    WRONG_ATTEMPT = "wrong_attempt"        # bound to another attempt
    STALE_REPORT = "stale_report"          # written before the graders changed
    WRONG_MODEL = "wrong_model"            # a cheap model answering as an expensive one
    OUT_OF_RANGE = "out_of_range"          # a score outside [0, 1]
    NOT_FINITE = "not_finite"              # NaN
    UNKNOWN_RECOMMENDATION = "unknown_recommendation"
    # The NAME of a failure mode, not a credential. Asserted by
    # test_a_secret_bearing_response_is_refused_and_never_stored_raw.
    SECRET_BEARING = "secret_bearing"  # nosec B105 — echoes a credential back
    TIMEOUT = "timeout"                    # the provider never answers
    UNAVAILABLE = "unavailable"            # the provider cannot run at all


#: The digest of a value that only ever exists inside this module, so a scanner test
#: does not require a real credential to be typed anywhere.
_FAKE_TOKEN = "ghp_" + "0" * 36


class MockTimeout(TeacherUnavailable):
    """The mock's timeout mode fired. A distinct type so a test can assert on it."""


class MockTeacherProvider(TeacherProvider):
    """A deterministic test double. Never active without an explicit mode."""

    provider_id = "mock_teacher"
    provider_version = "m62.mock.1"
    provider_kind = TeacherKind.MOCK
    is_cloud = False
    cost_bearing = False
    deterministic = True
    supported_modes = (ReviewMode.DETERMINISTIC_STUB,)

    def __init__(self, mode: MockMode, *, model: str = "mock-reviewer-1",
                 seed: int = 0, overrides: Mapping[str, object] | None = None) -> None:
        # No default mode: a mock that behaves "normally" unless told otherwise is a
        # mock that can be constructed by accident and believed.
        if not isinstance(mode, MockMode):
            raise TeacherError(f"MockTeacherProvider: mode must be a MockMode, got "
                               f"{type(mode).__name__}")
        self._mode = mode
        self._model = str(model)
        self._seed = require_int(seed, "mock.seed", minimum=0, maximum=2**31 - 1)
        self._overrides = dict(overrides or {})

    @property
    def mode(self) -> MockMode:
        return self._mode

    @property
    def model(self) -> str:
        return self._model

    def availability(self) -> TeacherAvailability:
        if self._mode is MockMode.UNAVAILABLE:
            return TeacherAvailability(
                available=False,
                reason="mock provider configured as unavailable",
                live_capable=False)
        return TeacherAvailability(available=True, live_capable=False,
                                   reason="deterministic test double",
                                   detail={"mode": self._mode.value})

    def _produce(self, packet: object, *, mode: ReviewMode,
                 timeout_s: int) -> ProviderResponse:
        if self._mode is MockMode.TIMEOUT:
            raise MockTimeout(
                f"mock provider timed out after {timeout_s}s without answering")
        return ProviderResponse(text=self.raw_response(packet),
                                audit={"mock_mode": self._mode.value})

    # -- payload construction --------------------------------------------------
    def _payload(self, packet: object) -> dict:
        """The response body for this mode. Every deviation is exactly one field."""
        recommendation = {
            MockMode.REVISE: "revise",
            MockMode.REJECT: "reject",
            MockMode.NEEDS_HUMAN: "needs_human_review",
        }.get(self._mode, "approve")
        score = 0.82
        if self._mode is MockMode.SEEDED_VARIATION:
            # A pure function of the seed: two seeds give two stable, different
            # reviews, and the same seed always gives the same one.
            score = round((int(sha256_text(str(self._seed))[:4], 16) % 1000) / 1000, 3)
        payload: dict[str, object] = {
            "packet_id": packet.packet_id,
            "packet_hash": packet.packet_hash,
            "task_hash": packet.task_hash,
            "attempt_hash": packet.attempt_hash,
            "deterministic_report_hash": packet.deterministic_report_hash,
            "rubric_version": packet.rubric_version,
            "provider": packet.requested_provider,
            "model": packet.requested_model,
            "overall_score": score,
            "dimension_scores": {"correctness": score, "security": score},
            "recommendation": recommendation,
            "confidence": 0.7,
        }
        if self._mode is MockMode.UNKNOWN_FIELD:
            payload["override"] = True
        elif self._mode is MockMode.WRONG_TASK:
            payload["task_hash"] = "a" * 64
        elif self._mode is MockMode.WRONG_ATTEMPT:
            payload["attempt_hash"] = "b" * 64
        elif self._mode is MockMode.STALE_REPORT:
            payload["deterministic_report_hash"] = "c" * 64
        elif self._mode is MockMode.WRONG_MODEL:
            payload["model"] = "some-cheaper-model"
        elif self._mode is MockMode.OUT_OF_RANGE:
            payload["overall_score"] = 1.4
        elif self._mode is MockMode.UNKNOWN_RECOMMENDATION:
            payload["recommendation"] = "approve_and_train"
        elif self._mode is MockMode.SECRET_BEARING:
            payload["factual_errors"] = [f"the token {_FAKE_TOKEN} was wrong"]
        payload.update(self._overrides)
        return payload

    def raw_response(self, packet: object) -> str:
        """The exact text this mock would return. For tests that inspect the bytes.

        ``NOT_FINITE`` is produced here rather than through ``json.dumps``, because
        Python's encoder emits a bare ``NaN`` that a strict reader must refuse — which
        is the whole point of the mode.
        """
        if self._mode is MockMode.MALFORMED_JSON:
            return "Sure! Overall this attempt looks reasonable to me."
        if self._mode is MockMode.NOT_FINITE:
            payload = self._payload(packet)
            payload["overall_score"] = 0.0
            return json.dumps(payload).replace('"overall_score": 0.0',
                                               '"overall_score": NaN')
        return json.dumps(self._payload(packet))


def mock_review_text(packet: object, *, mode: MockMode = MockMode.APPROVE,
                     rubric_version: str = RUBRIC_VERSION, **overrides: object) -> str:
    """A response body for a test that wants the TEXT rather than a provider.

    Useful where the subject under test is the importer rather than the provider
    protocol: the two paths must accept exactly the same bytes, and a test that builds
    its own JSON by hand tends to drift from what a provider actually emits.
    """
    provider = MockTeacherProvider(mode, overrides={"rubric_version": rubric_version,
                                                    **overrides})
    return provider.raw_response(packet)


__all__ = ["MockMode", "MockTeacherProvider", "MockTimeout", "mock_review_text"]
