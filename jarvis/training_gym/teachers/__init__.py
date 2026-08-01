"""training_gym.teachers — V69 M62: opinions, bounded so they stay opinions.

A teacher is a model asked what it thinks of an attempt the deterministic graders have
already measured. This package exists to make sure that is *all* it ever is.

THE AUTHORITY A TEACHER HOLDS
-----------------------------
None, except the authority to lower confidence and to ask for a human. Concretely, and
enforced by the types rather than by convention:

  * a teacher cannot turn a deterministic FAIL into anything else — the consensus engine
    checks the deterministic report FIRST and returns ``DETERMINISTIC_REJECT`` before a
    review is even read;
  * a teacher cannot raise a score. :func:`~training_gym.teachers.consensus.
    teacher_adjusted_total` can only subtract, and only up to the frozen
    :data:`~training_gym.rewards.MAX_TEACHER_PENALTY`;
  * a teacher cannot approve a dataset. ``approves_dataset`` on a record and
    ``approved`` on a consensus report are both hardcoded ``False``, and
    :class:`~training_gym.episode.Episode` still needs a hash-bound human decision;
  * a teacher cannot execute anything. :class:`~training_gym.teachers.base.
    TeacherProvider` has no method that runs a command, reads a workspace or touches a
    tool, and providers are handed a sanitized packet and nothing else.

THE DEFAULT PATH IS A HUMAN CARRYING A FILE
-------------------------------------------
:mod:`~training_gym.teachers.manual_packet` exports a sanitized, hash-bound packet the
operator pastes into ChatGPT or Claude; :mod:`~training_gym.teachers.review_import`
validates what comes back. No credential exists on the host for this path, no automated
egress exists, and the human sees the exact bytes that leave. The cloud adapters are
optional, disabled by default, and gated on ten explicit conditions before a single
request is allowed to be built.

REPLAY IS THE THREAT MODEL
--------------------------
A well-formed favourable review is valuable to anyone who wants an attempt approved, so
every record binds to FOUR subjects — task, attempt, deterministic report, and the exact
packet — plus provider, model and rubric version, and a packet is consumed exactly once
through an append-only ledger. Timestamps are recorded but never used as a defence: a
reviewer writes its own timestamp, and a defence an attacker can type is not one.

Importing this package contacts no network, reads no credential, starts no provider and
performs no I/O.
"""
from __future__ import annotations

from .anthropic_teacher import (
    ANTHROPIC_CREDENTIAL_ENV,
    ANTHROPIC_ENDPOINT,
    OptionalAnthropicTeacherProvider,
)
from .base import (
    DEFAULT_TEACHER_TIMEOUT_S,
    MAX_TEACHER_PROMPT_BYTES,
    MAX_TEACHER_RESPONSE_BYTES,
    MAX_TEACHER_TIMEOUT_S,
    RUBRIC_DIMENSIONS,
    RUBRIC_VERSION,
    TEACHER_PROTOCOL_VERSION,
    CostEstimate,
    ProviderResponse,
    ReviewMode,
    TeacherAvailability,
    TeacherCapability,
    TeacherError,
    TeacherKind,
    TeacherNotAuthorized,
    TeacherOutcome,
    TeacherProvider,
    TeacherReviewRecord,
    TeacherUnavailable,
)
from .cloud import (
    ALLOWED_CLOUD_HOSTS,
    PRICE_TABLE_VERSION,
    AuthorizationDecision,
    CloudRequest,
    CloudResponse,
    CloudTeacherConfig,
    CloudTransport,
    CloudTransportError,
    audit_event,
    authorize_cloud_call,
    environment_credential,
    estimate_cost,
)
from .consensus import (
    CONSENSUS_VERSION,
    ConsensusOutcome,
    ConsensusPolicy,
    ConsensusReport,
    Disagreement,
    decide,
    teacher_adjusted_total,
    teacher_penalty,
)
from .manual_packet import (
    PACKET_INSTRUCTIONS,
    PACKET_VERSION,
    RESPONSE_SCHEMA,
    RUBRIC,
    ManualReviewPacket,
    PacketError,
    PacketExportBlocked,
    build_packet,
    export_blockers,
    packet_from_dict,
)
from .mock_teacher import MockMode, MockTeacherProvider, mock_review_text
from .openai_teacher import (
    OPENAI_CREDENTIAL_ENV,
    OPENAI_ENDPOINT,
    OptionalOpenAITeacherProvider,
)
from .registry import (
    TEACHER_PROVIDER_IDS,
    ProviderEntry,
    RegistryError,
    TeacherRegistry,
    default_registry,
)
from .review_import import (
    REQUIRED_RESPONSE_FIELDS,
    TEACHER_RESPONSE_FIELDS,
    ReplayRejected,
    ReviewImportError,
    parse_review_json,
    parse_teacher_response,
    register_review,
    strict_json_object,
)
from .sanitization import (
    HIDDEN_KEY_TOKENS,
    SanitizationError,
    SanitizationReport,
    assert_clean,
    hidden_key,
    sanitize_for_export,
    sanitize_structure,
    sanitize_text,
    scan_export_payload,
)
from .store import (
    ConsumedPacket,
    InMemoryPacketLedger,
    PacketLedger,
    StoreError,
    TeacherArtifactStore,
)
from .verifier_teacher import (
    DEFAULT_VERIFIER_TIMEOUT_S,
    VerifierTeacherProvider,
    VerifyFn,
    verifier_from_seam,
)

__all__ = [
    "ALLOWED_CLOUD_HOSTS", "ANTHROPIC_CREDENTIAL_ENV", "ANTHROPIC_ENDPOINT",
    "CONSENSUS_VERSION", "DEFAULT_TEACHER_TIMEOUT_S", "DEFAULT_VERIFIER_TIMEOUT_S",
    "HIDDEN_KEY_TOKENS", "MAX_TEACHER_PROMPT_BYTES", "MAX_TEACHER_RESPONSE_BYTES",
    "MAX_TEACHER_TIMEOUT_S", "OPENAI_CREDENTIAL_ENV", "OPENAI_ENDPOINT",
    "PACKET_INSTRUCTIONS", "PACKET_VERSION", "PRICE_TABLE_VERSION",
    "REQUIRED_RESPONSE_FIELDS", "RESPONSE_SCHEMA", "RUBRIC", "RUBRIC_DIMENSIONS",
    "RUBRIC_VERSION", "TEACHER_PROTOCOL_VERSION", "TEACHER_PROVIDER_IDS",
    "TEACHER_RESPONSE_FIELDS", "AuthorizationDecision", "CloudRequest",
    "CloudResponse", "CloudTeacherConfig", "CloudTransport", "CloudTransportError",
    "ConsensusOutcome", "ConsensusPolicy", "ConsensusReport", "ConsumedPacket",
    "CostEstimate", "Disagreement", "InMemoryPacketLedger", "ManualReviewPacket",
    "MockMode", "MockTeacherProvider", "OptionalAnthropicTeacherProvider",
    "OptionalOpenAITeacherProvider", "PacketError", "PacketExportBlocked",
    "PacketLedger", "ProviderEntry", "ProviderResponse", "RegistryError",
    "ReplayRejected", "ReviewImportError", "ReviewMode", "SanitizationError",
    "SanitizationReport", "StoreError", "TeacherArtifactStore",
    "TeacherAvailability", "TeacherCapability", "TeacherError", "TeacherKind",
    "TeacherNotAuthorized", "TeacherOutcome", "TeacherProvider", "TeacherRegistry",
    "TeacherReviewRecord", "TeacherUnavailable", "VerifierTeacherProvider",
    "VerifyFn", "assert_clean", "audit_event", "authorize_cloud_call",
    "build_packet", "decide", "default_registry", "environment_credential",
    "estimate_cost", "export_blockers", "hidden_key", "mock_review_text",
    "packet_from_dict", "parse_review_json", "parse_teacher_response",
    "register_review", "sanitize_for_export", "sanitize_structure", "sanitize_text",
    "scan_export_payload", "strict_json_object", "teacher_adjusted_total",
    "teacher_penalty", "verifier_from_seam",
]
