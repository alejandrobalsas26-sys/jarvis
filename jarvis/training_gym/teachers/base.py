"""training_gym/teachers/base.py — V69 M62: the teacher contract, and its limits.

WHY THIS EXISTS
---------------
A teacher is a model asked for an OPINION about an attempt the deterministic graders
have already measured. That makes it the single most dangerous input in the milestone,
for two reasons that pull in opposite directions:

  * its output arrives from outside this process — a cloud API, or a human pasting a
    chat window's reply into a file — so it is hostile external data;
  * it is *persuasive*. A well-formed, confident ``"recommendation": "approve"`` is
    exactly what a tired operator wants to see, and if the code lets that string do any
    work beyond "route this to a human", the deterministic evidence has been demoted.

So this module fixes the authority a teacher can ever hold, in the type system rather
than in a convention:

  * :class:`TeacherProvider` declares capability and produces a *normalised* record.
    There is no method that executes anything, reads a file, mutates an episode,
    approves a dataset or promotes a model, and no provider is handed a workspace, an
    environment or a credential value.
  * :class:`TeacherReviewRecord` is the ONLY authoritative shape. A provider that
    returned a raw dictionary is refused, because a dictionary is whatever the
    strongest downstream reader wants it to be.
  * every record binds to FOUR subjects — task, attempt, deterministic report and the
    exact packet the reviewer was shown — so a favourable review cannot be lifted onto
    another attempt, another task, or the same attempt after the graders changed.
  * a provider that cannot run says so (``SKIPPED`` / ``INSUFFICIENT_EVIDENCE``).
    There is no fabricated review, no substituted model and no fallback provider.

WHY THE FROZEN ``TeacherReview`` IS WRAPPED RATHER THAN EDITED
-------------------------------------------------------------
:class:`~training_gym.trajectory.TeacherReview` is frozen S1 vocabulary and already
owns the opinion itself plus its task/attempt binding. The teacher layer needs three
further facts that the opinion does not carry: WHICH deterministic report the reviewer
was shown, WHICH build of the provider adapter produced the record, and WHICH packet
the reviewer answered. :class:`TeacherReviewRecord` adds exactly those, keeps the
frozen review verbatim inside itself, and cross-checks the two so they can never
disagree. Nothing in S1 is modified.

DISCIPLINE
----------
  * no clock — every timestamp is supplied by the caller, so a record hashes
    identically wherever it is produced and a test needs no freezing;
  * no environment read and no network at import;
  * no credential value ever enters a field, an argument or a log line; providers
    receive a *lookup seam*, never a secret;
  * fail closed: an unknown provider, an unsupported mode, an unbound record or an
    unavailable scanner is a refusal, never a default.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from ..schemas import (
    body_free_repr,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    ResultStatus,
    SchemaError,
    check_schema_version,
    reject_unknown_fields,
    require_enum,
    require_float,
    require_id,
    require_int,
    require_mapping,
    sha256_obj,
    short,
)
from ..task_spec import require_timestamp
from ..trajectory import Recommendation, TeacherReview

#: Bump when the contract below changes shape. Recorded in every provider version and
#: every persisted record, so a review can be attributed to the code that accepted it.
TEACHER_PROTOCOL_VERSION = "m62.teacher.1"

#: The rubric a teacher is asked to score against. Bumping it invalidates every review
#: produced under the previous one — by design: a score against different questions is
#: not comparable, and :meth:`TeacherReviewRecord.verify_binding` refuses the mismatch.
RUBRIC_VERSION = "m62.rubric.1"

#: The only dimension names a review may score. An unknown key is refused rather than
#: ignored: a teacher inventing ``"vibes": 1.0`` must not have it silently accepted and
#: then averaged into something by a later consumer.
RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "correctness", "security", "evidence_support", "schema_validity",
    "tool_safety", "minimality", "clarity",
)

#: Ceilings on what a teacher exchange may move. A reviewer that needs more than this
#: is being handed a log, not an attempt.
MAX_TEACHER_PROMPT_BYTES = 131_072
MAX_TEACHER_RESPONSE_BYTES = 65_536
#: Longest sanitized excerpt of a malformed response kept for the operator.
MAX_RESPONSE_EXCERPT_CHARS = 400
DEFAULT_TEACHER_TIMEOUT_S = 120
MAX_TEACHER_TIMEOUT_S = 300


class TeacherError(SchemaError):
    """A teacher exchange failed. Fail-closed: never a warning, never a coercion."""


class TeacherUnavailable(TeacherError):
    """A provider cannot run here. Reported honestly; never fabricated around."""


class TeacherNotAuthorized(TeacherError):
    """A gate a live call requires was not satisfied. Never downgraded, never routed
    to a different provider — a silent substitution is how an operator ends up
    believing a review came from a model that never saw the packet."""


# ── vocabulary ────────────────────────────────────────────────────────────────
class TeacherKind(str, Enum):
    """What kind of thing produces the review. Closed: an unknown kind is refused."""

    MANUAL_PACKET = "manual_packet"      # exported, pasted into a chat, imported back
    LOCAL_VERIFIER = "local_verifier"    # the local VERIFIER-role model
    MOCK = "mock"                        # deterministic test double
    CLOUD_OPENAI = "cloud_openai"
    CLOUD_ANTHROPIC = "cloud_anthropic"

    @property
    def is_cloud(self) -> bool:
        return self in (TeacherKind.CLOUD_OPENAI, TeacherKind.CLOUD_ANTHROPIC)


class ReviewMode(str, Enum):
    """How a review is obtained. A provider declares the modes it supports and
    :meth:`TeacherProvider.review` refuses any other — so a cloud adapter can never be
    driven through the offline path and quietly report a packet as "reviewed"."""

    OFFLINE_PACKET = "offline_packet"          # no call: a human carries the packet
    LOCAL_LIVE = "local_live"                  # local model, no egress
    CLOUD_LIVE = "cloud_live"                  # egress, paid, explicitly authorised
    DETERMINISTIC_STUB = "deterministic_stub"  # tests only


#: The recommendations a review may carry. Re-exported from the frozen vocabulary so
#: this layer cannot drift into accepting a fifth one.
ALLOWED_RECOMMENDATIONS: tuple[str, ...] = tuple(r.value for r in Recommendation)


# ── availability and capability ───────────────────────────────────────────────
@dataclass(frozen=True)
class TeacherAvailability:
    """Whether a provider is usable HERE, and why not when it is not.

    ``available=False`` is a first-class answer. The rule the whole package obeys is
    that it can never become a review: a provider that did not run has produced no
    opinion, and an absent opinion is not an approval.
    """

    available: bool = False
    reason: str = ""
    live_capable: bool = False
    detail: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason,
                "live_capable": self.live_capable,
                "detail": {str(k): v for k, v in sorted(self.detail.items())}}


@dataclass(frozen=True)
class CostEstimate:
    """What a live call is expected to cost, BEFORE it is made.

    Mandatory for a cloud provider: an operator authorising egress is entitled to know
    the order of magnitude first, and a provider that cannot estimate is not authorised
    to call. The numbers are estimates and say so — ``price_table_version`` records
    which table produced them so a stale estimate is identifiable rather than merely
    wrong.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    price_table_version: str = ""
    approximate: bool = True

    def __post_init__(self) -> None:
        require_int(self.prompt_tokens, "cost.prompt_tokens", minimum=0,
                    maximum=10_000_000)
        require_int(self.completion_tokens, "cost.completion_tokens", minimum=0,
                    maximum=10_000_000)
        require_float(self.usd, "cost.usd", minimum=0.0, maximum=1_000.0)
        if not str(self.price_table_version or "").strip():
            raise TeacherError("cost estimate: price_table_version is required; an "
                               "estimate whose table is unknown cannot be audited")

    def to_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "usd": round(self.usd, 6),
                "price_table_version": self.price_table_version,
                "approximate": self.approximate}


@dataclass(frozen=True)
class TeacherCapability:
    """A provider described without instantiating it or touching a credential."""

    provider_id: str
    provider_version: str
    provider_kind: TeacherKind
    model: str
    is_cloud: bool
    cost_bearing: bool
    live_capable: bool
    review_modes: tuple[ReviewMode, ...]
    deterministic: bool
    available: bool = False
    availability_reason: str = ""

    def to_dict(self) -> dict:
        return {"provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "provider_kind": self.provider_kind.value,
                "model": self.model,
                "is_cloud": self.is_cloud,
                "cost_bearing": self.cost_bearing,
                "live_capable": self.live_capable,
                "review_modes": [m.value for m in self.review_modes],
                "deterministic": self.deterministic,
                "available": self.available,
                "availability_reason": self.availability_reason}


# ── the normalised record ─────────────────────────────────────────────────────
_RECORD_FIELDS: tuple[str, ...] = (
    "schema_version", "protocol_version", "provider", "provider_version",
    "provider_kind", "model", "review_mode", "task_hash", "attempt_hash",
    "deterministic_report_hash", "packet_id", "packet_hash", "rubric_version",
    "created_at_utc", "review", "review_hash",
)


@dataclass(frozen=True)
class TeacherReviewRecord:
    """One teacher's opinion, bound to everything it must not be detachable from.

    The frozen :class:`~training_gym.trajectory.TeacherReview` is kept verbatim in
    ``review`` and is the only place an opinion lives. This wrapper adds the three
    bindings the opinion cannot carry — the deterministic report the reviewer was
    shown, the packet they answered, and the adapter build that accepted the answer —
    and then CROSS-CHECKS them against the review, so the two halves of a record can
    never disagree about who reviewed what.
    """

    provider: str
    provider_version: str
    provider_kind: TeacherKind
    model: str
    review_mode: ReviewMode
    task_hash: str
    attempt_hash: str
    deterministic_report_hash: str
    packet_id: str
    packet_hash: str
    rubric_version: str
    created_at_utc: str
    review: TeacherReview

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", require_id(self.provider,
                                                        "record.provider"))
        object.__setattr__(self, "provider_kind",
                           require_enum(self.provider_kind, TeacherKind,
                                        "record.provider_kind"))
        object.__setattr__(self, "review_mode",
                           require_enum(self.review_mode, ReviewMode,
                                        "record.review_mode"))
        if not str(self.provider_version or "").strip():
            raise TeacherError("record.provider_version: required (which build of the "
                               "adapter accepted this review)")
        if not str(self.model or "").strip():
            raise TeacherError("record.model: required (which model reviewed this)")
        require_id(self.packet_id, "record.packet_id")
        require_timestamp(self.created_at_utc, "record.created_at_utc")
        for name in ("task_hash", "attempt_hash", "deterministic_report_hash",
                     "packet_hash"):
            digest = str(getattr(self, name) or "").strip().lower()
            if len(digest) != 64:
                raise TeacherError(
                    f"record.{name}: expected a 64-character digest; a review that "
                    f"names no {name.removesuffix('_hash')} can be replayed onto "
                    f"another one")
            object.__setattr__(self, name, digest)
        if not isinstance(self.review, TeacherReview):
            raise TeacherError("record.review: expected a TeacherReview; a raw "
                               "dictionary is never an authoritative review")
        if not str(self.rubric_version or "").strip():
            raise TeacherError("record.rubric_version: required (a score against "
                               "unknown questions is not comparable)")
        self._cross_check()

    def _cross_check(self) -> None:
        """The wrapper and the frozen review must describe the SAME review.

        Without this a record could bind to attempt A while the opinion inside it named
        attempt B, and each half would validate perfectly on its own.
        """
        mismatches = [
            name for name, mine, theirs in (
                ("provider", self.provider, self.review.provider),
                ("model", self.model, self.review.model),
                ("task_hash", self.task_hash, self.review.task_hash.lower()),
                ("attempt_hash", self.attempt_hash, self.review.attempt_hash.lower()),
                ("rubric_version", self.rubric_version, self.review.rubric_version),
            ) if str(mine) != str(theirs)
        ]
        if mismatches:
            raise TeacherError(
                f"teacher record: the envelope and the review disagree on "
                f"{mismatches}; refusing a record whose two halves describe different "
                f"reviews")
        unknown = sorted(set(self.review.dimension_scores) - set(RUBRIC_DIMENSIONS))
        if unknown:
            raise TeacherError(
                f"teacher record: unknown rubric dimension(s) {unknown}; allowed "
                f"{list(RUBRIC_DIMENSIONS)}")

    # -- authority limits -------------------------------------------------------
    @property
    def recommendation(self) -> Recommendation:
        return self.review.recommendation

    @property
    def flags_unsafe(self) -> bool:
        return self.review.flags_unsafe

    @property
    def approves_dataset(self) -> bool:
        """Always ``False``. A teacher's APPROVE routes an attempt to a human.

        Present so a caller reaching for "did the teacher approve it" finds an explicit
        refusal rather than an :class:`AttributeError` it might work around with
        ``recommendation == APPROVE``, which is not the same claim.
        """
        return False

    # -- identity ---------------------------------------------------------------
    def review_hash(self) -> str:
        """The deterministic digest of this record.

        Computed over every field EXCEPT the digest itself, so no security-relevant
        field is outside it: change the provider, the model, a score, a
        recommendation, a binding hash or the rubric, and the digest changes.
        """
        payload = self.to_dict()
        payload.pop("review_hash", None)
        return sha256_obj(payload)

    def verify_binding(self, *, task_hash: str, attempt_hash: str,
                       deterministic_report_hash: str, packet_id: str,
                       packet_hash: str, provider: str, model: str,
                       rubric_version: str) -> None:
        """Refuse this record unless it answers exactly THIS packet.

        Every argument is a separate replay vector, so every one is checked: the task
        (review for task A filed against task B), the attempt (attempt 1 against
        attempt 2), the deterministic report (a review written before the graders
        changed), the packet (a review generated from an edited packet), and the
        provider/model pair (a cheap model's answer submitted as an expensive one's).
        """
        expectations = (
            ("task_hash", self.task_hash, str(task_hash).strip().lower()),
            ("attempt_hash", self.attempt_hash, str(attempt_hash).strip().lower()),
            ("deterministic_report_hash", self.deterministic_report_hash,
             str(deterministic_report_hash).strip().lower()),
            ("packet_hash", self.packet_hash, str(packet_hash).strip().lower()),
            ("packet_id", self.packet_id, str(packet_id).strip()),
            ("provider", self.provider, str(provider).strip()),
            ("model", self.model, str(model).strip()),
            ("rubric_version", self.rubric_version, str(rubric_version).strip()),
        )
        for name, declared, expected in expectations:
            if not expected:
                raise TeacherError(f"teacher record: cannot verify {name} against an "
                                   f"empty expectation; refusing an unbound check")
            if declared != expected:
                raise TeacherError(
                    f"teacher record from {self.provider}: {name}="
                    f"{short(declared)!r} does not match the packet's "
                    f"{short(expected)!r}; refusing a review bound to a different "
                    f"subject")

    def to_dict(self) -> dict:
        payload = {
            SCHEMA_KEY: SCHEMA_VERSION,
            "protocol_version": TEACHER_PROTOCOL_VERSION,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "provider_kind": self.provider_kind.value,
            "model": self.model,
            "review_mode": self.review_mode.value,
            "task_hash": self.task_hash,
            "attempt_hash": self.attempt_hash,
            "deterministic_report_hash": self.deterministic_report_hash,
            "packet_id": self.packet_id,
            "packet_hash": self.packet_hash,
            "rubric_version": self.rubric_version,
            "created_at_utc": self.created_at_utc,
            "review": self.review.to_dict(),
        }
        payload["review_hash"] = sha256_obj(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "TeacherReviewRecord":
        """Rebuild a record, refusing an unknown field and a tampered digest."""
        data = require_mapping(payload, "teacher record")
        check_schema_version(data, label="teacher record")
        reject_unknown_fields(data, _RECORD_FIELDS, label="teacher record")
        declared_protocol = str(data.get("protocol_version", "")).strip()
        if declared_protocol and declared_protocol != TEACHER_PROTOCOL_VERSION:
            raise TeacherError(f"teacher record: protocol_version "
                               f"{declared_protocol!r} is not "
                               f"{TEACHER_PROTOCOL_VERSION!r}")
        record = cls(
            provider=str(data.get("provider", "")),
            provider_version=str(data.get("provider_version", "")),
            provider_kind=require_enum(data.get("provider_kind"), TeacherKind,
                                       "record.provider_kind"),
            model=str(data.get("model", "")),
            review_mode=require_enum(data.get("review_mode"), ReviewMode,
                                     "record.review_mode"),
            task_hash=str(data.get("task_hash", "")),
            attempt_hash=str(data.get("attempt_hash", "")),
            deterministic_report_hash=str(data.get("deterministic_report_hash", "")),
            packet_id=str(data.get("packet_id", "")),
            packet_hash=str(data.get("packet_hash", "")),
            rubric_version=str(data.get("rubric_version", "")),
            created_at_utc=str(data.get("created_at_utc", "")),
            review=TeacherReview.from_dict(data.get("review")),
        )
        declared_hash = str(data.get("review_hash", "")).strip().lower()
        if declared_hash and declared_hash != record.review_hash():
            raise TeacherError(
                "teacher record: review_hash does not match the record it labels; "
                "refusing a record whose contents were changed after it was signed")
        return record


# ── what a provider returns before normalisation ──────────────────────────────
@dataclass(frozen=True)
class ProviderResponse:
    """The raw answer a provider obtained, bounded and never persisted verbatim.

    ``text`` is held only long enough to be parsed. Nothing downstream stores it: the
    record keeps the normalised review, and a failure keeps a HASH plus a sanitized
    excerpt. A raw cloud response is untrusted content of unknown length that may echo
    the packet back — storing it wholesale is how a "reviewed" artifact grows a copy of
    everything the reviewer was shown.
    """

    text: str = ""
    cost: CostEstimate | None = None
    audit: Mapping[str, object] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Accounting only — never the generated ``text``."""
        return body_free_repr(self, "cost", text_len=len(self.text or ""))

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TeacherError("provider response: expected text, got "
                               f"{type(self.text).__name__}")
        size = len(self.text.encode("utf-8", "surrogatepass"))
        if size > MAX_TEACHER_RESPONSE_BYTES:
            raise TeacherError(f"provider response: {size} bytes exceeds the "
                               f"{MAX_TEACHER_RESPONSE_BYTES}-byte ceiling")


@dataclass(frozen=True)
class TeacherOutcome:
    """One attempt to obtain a review, whether or not it produced one.

    ``status`` reuses the frozen :class:`~training_gym.schemas.ResultStatus` so a
    teacher's non-answer is spelled with the same words as a grader's, and inherits the
    same rule: only ``PASS`` is affirmative, and nothing else may be read as consent.
    """

    provider: str
    model: str
    status: ResultStatus
    record: TeacherReviewRecord | None = None
    reason: str = ""
    response_sha256: str = ""
    response_excerpt: str = ""
    duration_ms: int = 0
    cost: CostEstimate | None = None
    audit: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", require_enum(self.status, ResultStatus,
                                                        "outcome.status"))
        if self.status is ResultStatus.PASS and self.record is None:
            raise TeacherError("teacher outcome: PASS without a record; a review that "
                               "does not exist has not been obtained")
        if self.status is not ResultStatus.PASS and self.record is not None:
            raise TeacherError("teacher outcome: a non-affirmative status must not "
                               "carry a record")
        if self.status is not ResultStatus.PASS and not str(self.reason or "").strip():
            raise TeacherError("teacher outcome: a non-answer must state why")

    @property
    def produced_review(self) -> bool:
        return self.status is ResultStatus.PASS and self.record is not None

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "status": self.status.value,
                "produced_review": self.produced_review,
                "reason": self.reason,
                "response_sha256": self.response_sha256,
                "response_excerpt": self.response_excerpt,
                "duration_ms": self.duration_ms,
                "cost": self.cost.to_dict() if self.cost else None,
                "record": self.record.to_dict() if self.record else None,
                "audit": {str(k): v for k, v in sorted(self.audit.items())}}


# ── the protocol ──────────────────────────────────────────────────────────────
class TeacherProvider(ABC):
    """One source of teacher opinion. Subclasses implement :meth:`_produce`.

    The class attributes ARE the declaration a registry and an operator read, and they
    are validated when the subclass is defined so a mis-declared provider is an import
    error rather than a surprise at review time.

    Deliberately absent from this interface: any method that runs a command, reads a
    workspace file, mutates an episode, writes a dataset, promotes a model, or receives
    a credential VALUE. A provider that wanted any of those would have to add it in a
    diff a reviewer sees.
    """

    provider_id: ClassVar[str] = ""
    provider_version: ClassVar[str] = TEACHER_PROTOCOL_VERSION
    provider_kind: ClassVar[TeacherKind] = TeacherKind.MOCK
    #: True when this provider is paid and leaves the host. Both facts, one flag each,
    #: because "cloud" and "costs money" are not the same question.
    is_cloud: ClassVar[bool] = False
    cost_bearing: ClassVar[bool] = False
    #: True when identical inputs must produce an identical review.
    deterministic: ClassVar[bool] = False
    supported_modes: ClassVar[tuple[ReviewMode, ...]] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        require_id(cls.provider_id, f"{cls.__name__}.provider_id")
        if not str(cls.provider_version or "").strip():
            raise TeacherError(f"{cls.__name__}: provider_version is required")
        if not isinstance(cls.provider_kind, TeacherKind):
            raise TeacherError(f"{cls.__name__}: provider_kind must be a TeacherKind")
        if not cls.supported_modes:
            raise TeacherError(f"{cls.__name__}: supported_modes is empty; a provider "
                               f"that supports no review mode is not a provider")
        if cls.provider_kind.is_cloud and not cls.is_cloud:
            raise TeacherError(f"{cls.__name__}: a cloud provider kind must declare "
                               f"is_cloud=True")
        if cls.is_cloud and ReviewMode.CLOUD_LIVE not in cls.supported_modes:
            raise TeacherError(f"{cls.__name__}: a cloud provider must declare the "
                               f"cloud_live review mode")
        if not cls.is_cloud and ReviewMode.CLOUD_LIVE in cls.supported_modes:
            raise TeacherError(f"{cls.__name__}: a non-cloud provider may not declare "
                               f"the cloud_live review mode")

    # -- declaration -----------------------------------------------------------
    @property
    @abstractmethod
    def model(self) -> str:
        """The resolved model identifier, never a role alias.

        A review attributed to "whatever the fast model pointed at that day" is not
        reproducible and must not become part of an approval record.
        """

    @abstractmethod
    def availability(self) -> TeacherAvailability:
        """Whether this provider can produce a review here, and why not when it cannot.

        Must not perform the review, must not open a network connection, and must not
        read a credential VALUE — only whether one is reachable.
        """

    def capability(self) -> TeacherCapability:
        """A description safe to print, log and persist. Touches no secret."""
        state = self.availability()
        return TeacherCapability(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_kind=self.provider_kind,
            model=self.model,
            is_cloud=self.is_cloud,
            cost_bearing=self.cost_bearing,
            live_capable=state.live_capable,
            review_modes=tuple(self.supported_modes),
            deterministic=self.deterministic,
            available=state.available,
            availability_reason=state.reason,
        )

    def supports(self, mode: ReviewMode) -> bool:
        return mode in self.supported_modes

    # -- production ------------------------------------------------------------
    @abstractmethod
    def _produce(self, packet: object, *, mode: ReviewMode,
                 timeout_s: int) -> ProviderResponse:
        """Obtain the raw answer. Implemented by every concrete provider.

        Receives the sanitized packet and nothing else — no workspace, no environment,
        no trajectory and no credential.
        """

    def review(self, packet: object, *, created_at_utc: str,
               mode: ReviewMode | None = None,
               timeout_s: int = DEFAULT_TEACHER_TIMEOUT_S,
               ledger: object | None = None) -> TeacherOutcome:
        """Obtain and NORMALISE one review. Never overridden.

        This is where a provider stops being trusted to behave and starts being made
        to. Every path out of here is either a validated, bound
        :class:`TeacherReviewRecord` or a non-affirmative outcome that states why —
        there is no path on which a crash, a timeout, a malformed answer or an
        unavailable provider becomes an opinion.
        """
        from .manual_packet import ManualReviewPacket
        from .review_import import parse_teacher_response

        if not isinstance(packet, ManualReviewPacket):
            raise TeacherError(f"{self.provider_id}.review: expected a "
                               f"ManualReviewPacket, got {type(packet).__name__}")
        require_timestamp(created_at_utc, f"{self.provider_id}.review: created_at_utc")
        require_int(timeout_s, f"{self.provider_id}.review: timeout_s",
                    minimum=1, maximum=MAX_TEACHER_TIMEOUT_S)

        effective = mode or self.supported_modes[0]
        if not self.supports(effective):
            return self._refused(ResultStatus.SKIPPED,
                                 f"{self.provider_id} does not support review mode "
                                 f"{effective.value}")
        # A packet whose recorded digest no longer matches its content was edited after
        # export. Every binding below is computed FROM the packet, so a tampered packet
        # would otherwise validate a review against the tampered values.
        if packet.packet_hash != packet.compute_hash():
            return self._refused(ResultStatus.ERROR,
                                 "packet integrity check failed: its recorded hash "
                                 "does not match its content")
        if packet.requested_provider != self.provider_id:
            return self._refused(
                ResultStatus.SKIPPED,
                f"packet {packet.packet_id} requests provider "
                f"{packet.requested_provider!r}; {self.provider_id} refuses to answer "
                f"a packet addressed to another provider")
        if packet.requested_model != self.model:
            return self._refused(
                ResultStatus.SKIPPED,
                f"packet {packet.packet_id} requests model "
                f"{packet.requested_model!r} but this provider resolves to "
                f"{self.model!r}; substituting a model is never automatic")

        state = self.availability()
        if not state.available:
            return self._refused(ResultStatus.SKIPPED,
                                 state.reason or f"{self.provider_id} is unavailable")

        try:
            response = self._produce(packet, mode=effective, timeout_s=timeout_s)
        except TeacherNotAuthorized as exc:
            return self._refused(ResultStatus.SKIPPED, f"not authorized: {exc}")
        except TeacherUnavailable as exc:
            return self._refused(ResultStatus.SKIPPED, str(exc))
        except TeacherError as exc:
            return self._refused(ResultStatus.ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 — a crashed provider produced nothing
            return self._refused(ResultStatus.ERROR,
                                 f"{type(exc).__name__} while obtaining a review")

        if not isinstance(response, ProviderResponse):
            return self._refused(
                ResultStatus.ERROR,
                f"{self.provider_id} returned {type(response).__name__}; a raw value "
                f"is never an authoritative review")
        try:
            record = parse_teacher_response(
                response.text, packet=packet, provider=self,
                created_at_utc=created_at_utc, mode=effective, ledger=ledger)
        except TeacherError as exc:
            return self._refused(ResultStatus.ERROR, str(exc),
                                 response=response, packet=packet)
        return TeacherOutcome(
            provider=self.provider_id, model=self.model, status=ResultStatus.PASS,
            record=record, response_sha256=_digest(response.text),
            cost=response.cost,
            # Whether single-use was actually enforced is recorded, not assumed. A
            # caller may legitimately import without consuming the packet, but a record
            # that does not say which happened invites a reader to assume the safer one.
            audit={**dict(response.audit), "replay_ledger": ledger is not None})

    # -- helpers ---------------------------------------------------------------
    def _refused(self, status: ResultStatus, reason: str, *,
                 response: ProviderResponse | None = None,
                 packet: object | None = None) -> TeacherOutcome:
        """A non-answer, with a bounded sanitized trace and no raw response stored."""
        from .sanitization import sanitize_text
        excerpt = ""
        if response is not None and response.text:
            excerpt, _report = sanitize_text(response.text,
                                             limit=MAX_RESPONSE_EXCERPT_CHARS)
        # The reason is sanitized too. Most reasons are this module's own text, but a
        # validation failure quotes the offending FIELD NAME back — and a field name is
        # attacker-chosen, so it is a place a token can ride out of an untrusted
        # response into a stored outcome.
        clean_reason, _reason_report = sanitize_text(reason, limit=2_000)
        cost = response.cost if response is not None else None
        audit = dict(response.audit) if response is not None else {}
        if packet is not None:
            audit.setdefault("packet_id", getattr(packet, "packet_id", ""))
        return TeacherOutcome(
            provider=self.provider_id, model=self.model, status=status,
            reason=clean_reason or reason,
            response_sha256=_digest(response.text) if response is not None else "",
            response_excerpt=excerpt, cost=cost, audit=audit)


def _digest(text: str) -> str:
    from ..schemas import sha256_text
    return sha256_text(text or "")


__all__ = [
    "ALLOWED_RECOMMENDATIONS", "DEFAULT_TEACHER_TIMEOUT_S",
    "MAX_RESPONSE_EXCERPT_CHARS", "MAX_TEACHER_PROMPT_BYTES",
    "MAX_TEACHER_RESPONSE_BYTES", "MAX_TEACHER_TIMEOUT_S", "RUBRIC_DIMENSIONS",
    "RUBRIC_VERSION", "TEACHER_PROTOCOL_VERSION", "CostEstimate", "ProviderResponse",
    "ReviewMode", "TeacherAvailability", "TeacherCapability", "TeacherError",
    "TeacherKind", "TeacherNotAuthorized", "TeacherOutcome", "TeacherProvider",
    "TeacherReviewRecord", "TeacherUnavailable",
]
