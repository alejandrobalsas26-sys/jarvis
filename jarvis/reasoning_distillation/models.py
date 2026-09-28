"""reasoning_distillation/models.py — V69 M67A: the typed shapes, and their limits.

WHAT THIS MODULE IS
-------------------
Three layers of type, in order of distance from the source:

  1. **provenance** (§6) — the deterministic identity of a source unit. Never a filename.
  2. **the canonical conversation** (§7) — a historical export, normalized into typed
     turns, with every field that was absent recorded as absent.
  3. **the distilled decision** (§11) — a reusable PROCEDURE extracted from those turns.

THE RULE THAT SHAPES EVERY FIELD
--------------------------------
A historical model trace is **evidence of a reasoning attempt, not proof that the
reasoning was optimal**, and it is certainly not a fact. So every optional field here has
an explicit absent value and none of them defaults to something favourable:

  * a missing model name is ``""``, which means UNKNOWN, and ``source_model_known`` is
    ``False``. It is never guessed from the filename or the prose style.
  * a factual claim starts at :attr:`FactualState.SOURCE_ONLY`. There is no code path
    that promotes it to ``VERIFIED`` without an external check, and ``SOURCE_ONLY``
    never reads as true.
  * an export decision starts at :attr:`ExportStatus.EXPORT_UNKNOWN`, which blocks
    export. "Nobody has classified this" and "this is safe" are different states, and
    conflating them is how private material leaves a machine.
  * a distilled field the source does not support is ``UNKNOWN`` or empty. §12 is
    explicit: never fill missing reasoning to make the schema look complete.

WHY THE VOCABULARY IS BORROWED AND NOT INVENTED
-----------------------------------------------
JARVIS already has a closed vocabulary for exactly these dimensions, built and hardened
at M66A: :class:`core.epistemic_deliberation.IntentGoal` for what the operator wanted,
``FreshnessRequirement`` for time-sensitivity, ``PremiseState`` for how well a premise is
supported, ``ToolNeed`` for whether a tool was NEEDED (as distinct from available),
``DeliberationMode`` for how much decomposition the turn deserved, and
``VerificationStatus`` for what verification actually concluded. ``core.mesh_contracts``
owns ``Provenance`` and ``ClaimStatus``.

Those enums are imported, not re-declared. §3 is the reason: the distilled corpus has to
map into the decision architecture that already exists, so that a future experiment can
compare a distilled procedure against what JARVIS's own deliberation would have decided.
A parallel taxonomy would make that comparison a translation exercise, and a translation
layer is where a mismatch hides.

The one place M67A adds vocabulary is where JARVIS has no equivalent concept: the
*historical-source* dimensions (:class:`SourceType`, :class:`TurnRole`,
:class:`FactualState`, :class:`ReviewStatus`, :class:`RejectReason`) and the
*distillation-process* dimensions (:class:`DistillationStage`, :class:`Disposition`).

BODY BLINDNESS IS ARCHITECTURAL HERE
------------------------------------
Every class in this module that holds source text installs a body-free ``__repr__`` built
by :func:`training_gym.schemas.body_free_repr`. That is not defensive style; it is the
control that closes a measured leak class in this repository: a ``@dataclass`` renders
every field, a container recurses into its elements, and ``repr`` of a BOUND METHOD
interpolates ``repr(__self__)`` — so merely displaying ``conversation.digest`` WITHOUT
CALLING IT would otherwise render every turn of a private conversation. There is no
``__repr__`` on a method object to override, so the container's own repr must already be
body-free. See the long note in :mod:`training_gym.schemas`.

The consequence for M67A specifically: raw historical chats stay outside Git (§4) AND
outside every log line, traceback and debug display inside the process.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.epistemic_deliberation import (
    DeliberationMode,
    FreshnessRequirement,
    IntentGoal,
    PremiseState,
    ToolExecutionState,
    ToolNeed,
    VerificationStatus,
)
from core.mesh_contracts import ClaimStatus, Provenance
from training_gym.schemas import (
    SchemaError,
    SensitivityClass,
    body_free_repr,
    canonical_json,
    sha256_obj,
    sha256_text,
)

#: Bump when a field is added, removed or re-meaning'd in any schema below. A distilled
#: record carries it, and :func:`check_record_version` refuses one it does not know — a
#: schema version is only a control if the reader enforces it.
SCHEMA_VERSION = "m67a.distilled.1"

#: Versions a reader in THIS build accepts. A record from an unknown version is refused
#: rather than best-effort parsed: a field that moved meaning between versions would be
#: read confidently and wrongly.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({SCHEMA_VERSION})

#: Bump when normalization changes what a canonical turn looks like for identical input.
NORMALIZATION_VERSION = "m67a.normalization.1"

#: The longest single string any record will store. Historical exports contain pasted
#: files and stack traces; a record is a decision, not an archive, and the archive is the
#: immutable source on disk.
MAX_FIELD_CHARS = 20_000

#: Bound on list-shaped fields. Nothing here grows unbounded.
MAX_ITEMS = 64


class DistillationError(SchemaError):
    """A distillation step was refused. Never a partially-built record."""


# ══════════════════════════════════════════════════════════════════════════════════════
#  §6 — source identity
# ══════════════════════════════════════════════════════════════════════════════════════
class SourceType(str, Enum):
    """The concrete export format a source unit came from.

    ``UNKNOWN`` exists and is not a failure state on its own: an adapter that recognised
    the bytes but not the dialect produces a unit whose type is UNKNOWN and whose review
    status is ``NEEDS_HUMAN_REVIEW``. The two are recorded separately because "we do not
    know the format" and "we could not read it" are different findings.
    """

    TXT = "txt"
    MARKDOWN = "markdown"
    JSON_EXPORT = "json_export"
    DOCX = "docx"
    HTML = "html"
    SYNTHETIC_FIXTURE = "synthetic_fixture"
    UNKNOWN = "unknown"


class TurnRole(str, Enum):
    """What one turn IS. §7's canonical distinctions, plus an explicit unknown.

    ``ASSISTANT_REASONING`` and ``ASSISTANT_ANSWER`` are deliberately separate members:
    the whole milestone rests on being able to extract the procedure from the former
    without treating the latter as ground truth.
    """

    SYSTEM_CONTEXT = "system_context"
    USER_REQUEST = "user_request"
    ASSISTANT_REASONING = "assistant_reasoning"
    ASSISTANT_ANSWER = "assistant_answer"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    ASSISTANT_REVISION = "assistant_revision"
    FOLLOW_UP = "follow_up"
    UNKNOWN = "unknown"


#: Roles whose content is the assistant's own process rather than the operator's material.
ASSISTANT_PROCESS_ROLES: frozenset[TurnRole] = frozenset({
    TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_REVISION})


@dataclass(frozen=True)
class SourceProvenance:
    """The deterministic identity of one source segment (§6).

    ``source_file_hash`` is the digest of the file's BYTES, and ``raw_segment_hash`` the
    digest of this segment's exact text. Together with ``conversation_id`` and
    ``turn_index`` they make repeated ingestion idempotent: the same bytes always produce
    the same identity, so a second import updates nothing and adds nothing.

    ``source_path_hint`` is a BASENAME kept for human orientation and is explicitly NOT
    identity. §6 says so directly, and the reason is concrete: two exports of the same
    conversation downloaded twice have different filenames and identical content, while
    two different conversations can be saved under the same name in different folders.
    A full path is not stored at all — it would put the operator's home directory into
    every derived record.
    """

    source_file_hash: str
    source_type: SourceType
    conversation_id: str
    turn_index: int
    role: TurnRole
    raw_segment_hash: str
    #: "" means UNKNOWN. Never inferred from prose style or filename.
    source_model: str = ""
    #: ISO-8601 date or "". "" means UNKNOWN; a file mtime is NOT a conversation date.
    source_date: str = ""
    source_path_hint: str = ""

    @property
    def source_model_known(self) -> bool:
        """Explicit accessor, so a caller cannot read ``""`` as a model name."""
        return bool(self.source_model)

    @property
    def source_date_known(self) -> bool:
        return bool(self.source_date)

    @property
    def unit_id(self) -> str:
        """The stable address of this segment. Derived, never supplied.

        Includes the file hash AND the segment hash AND the position. A segment hash
        alone would merge two identical turns in different conversations, which is
        exactly the merge that would let a holdout family bleed into development.
        """
        return sha256_obj({
            "source_file_hash": self.source_file_hash,
            "conversation_id": self.conversation_id,
            "turn_index": self.turn_index,
            "raw_segment_hash": self.raw_segment_hash,
        })

    def to_dict(self) -> dict:
        return {
            "source_file_hash": self.source_file_hash,
            "source_type": self.source_type.value,
            "conversation_id": self.conversation_id,
            "turn_index": self.turn_index,
            "role": self.role.value,
            "raw_segment_hash": self.raw_segment_hash,
            "source_model": self.source_model,
            "source_model_known": self.source_model_known,
            "source_date": self.source_date,
            "source_date_known": self.source_date_known,
            "source_path_hint": self.source_path_hint,
            "unit_id": self.unit_id,
        }

    def validated(self) -> "SourceProvenance":
        """Refuse an identity that is not an identity. §17: uncertainty fails CLOSED."""
        for name in ("source_file_hash", "raw_segment_hash"):
            value = getattr(self, name)
            if not (isinstance(value, str) and len(value) == 64
                    and all(c in "0123456789abcdef" for c in value)):
                raise DistillationError(
                    f"provenance.{name} must be a 64-char lowercase sha256 digest; a "
                    f"source unit whose identity is uncertain is refused, not imported")
        if not self.conversation_id:
            raise DistillationError(
                "provenance.conversation_id is empty; a segment with no conversation "
                "cannot be family-grouped, and an ungrouped segment can cross a split")
        if self.turn_index < 0:
            raise DistillationError(
                f"provenance.turn_index must not be negative, got {self.turn_index}")
        if "/" in self.source_path_hint or "\\" in self.source_path_hint:
            raise DistillationError(
                "provenance.source_path_hint must be a basename: a full path carries the "
                "operator's directory layout into every derived record")
        return self

    def __repr__(self) -> str:  # pragma: no cover - exercised by the privacy tests
        return body_free_repr(self, "source_type", "conversation_id", "turn_index",
                              "role", "source_file_hash", "raw_segment_hash")


# ══════════════════════════════════════════════════════════════════════════════════════
#  §4 — privacy and export
# ══════════════════════════════════════════════════════════════════════════════════════
class ExportStatus(str, Enum):
    """Whether a record may leave the local corpus root.

    ``EXPORT_UNKNOWN`` is the DEFAULT and it blocks. §4 requires that every record carry
    an explicit privacy/export status and that derived content is never silently
    classified as anonymous; a default of ``EXPORT_SAFE`` would be exactly that silent
    classification, applied to every record nobody looked at.
    """

    EXPORT_SAFE = "export_safe"
    EXPORT_BLOCKED = "export_blocked"
    EXPORT_UNKNOWN = "export_unknown"

    @property
    def permits_export(self) -> bool:
        """Only one member permits export, and it has to be set deliberately."""
        return self is ExportStatus.EXPORT_SAFE


@dataclass(frozen=True)
class PrivacyAssessment:
    """The explicit privacy decision for one record (§4).

    ``scanner_available`` is a field rather than an assumption. When the underlying
    scanners cannot be imported, the assessment is ``EXPORT_UNKNOWN`` with
    ``scanner_available=False`` — an unavailable scanner proves nothing, and the S4G
    lesson recorded in this repository is that an unauditable detector must at least be
    able to say it did not run.
    """

    contains_personal_data: bool
    contains_secret_like_data: bool
    export_status: ExportStatus
    categories: tuple[str, ...] = ()
    scanner_available: bool = True
    sensitivity: SensitivityClass = SensitivityClass.INTERNAL
    #: Free-text reason, bounded. Never contains the matched material itself.
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "contains_personal_data": self.contains_personal_data,
            "contains_secret_like_data": self.contains_secret_like_data,
            "export_status": self.export_status.value,
            "categories": list(self.categories[:MAX_ITEMS]),
            "scanner_available": self.scanner_available,
            "sensitivity": self.sensitivity.value,
            "note": self.note[:320],
        }

    def validated(self) -> "PrivacyAssessment":
        """A positive finding can never coexist with EXPORT_SAFE."""
        if self.export_status.permits_export and (
                self.contains_personal_data or self.contains_secret_like_data):
            raise DistillationError(
                "privacy: EXPORT_SAFE with a positive personal-data or secret-like "
                "finding is contradictory; the finding wins")
        if self.export_status.permits_export and not self.scanner_available:
            raise DistillationError(
                "privacy: EXPORT_SAFE cannot be claimed when the scanner was "
                "unavailable — an absent scanner is not a clean result")
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return body_free_repr(self, "export_status", "contains_personal_data",
                              "contains_secret_like_data", "scanner_available",
                              "sensitivity", categories=len(self.categories))


#: The fail-closed assessment. Used whenever classification has not happened yet, so an
#: unclassified record is never accidentally exportable.
UNCLASSIFIED = PrivacyAssessment(
    contains_personal_data=False, contains_secret_like_data=False,
    export_status=ExportStatus.EXPORT_UNKNOWN, categories=("unclassified",),
    scanner_available=False, sensitivity=SensitivityClass.INTERNAL,
    note="not yet classified; export blocked by default")


# ══════════════════════════════════════════════════════════════════════════════════════
#  §7 — the canonical conversation
# ══════════════════════════════════════════════════════════════════════════════════════
class ReviewStatus(str, Enum):
    """Whether a human still has to look at this.

    ``NEEDS_HUMAN_REVIEW`` is not a soft warning; it removes the record from every
    automatic downstream path until a human resolves it (§7, §12, §13).
    """

    OK = "ok"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class CanonicalTurn:
    """One turn of a historical conversation, typed (§7).

    ``text`` is a BODY. It is never rendered by ``__repr__``, never written to a report,
    and never included in a manifest — only its digest travels.

    ``meta_noise_markers`` records §13 findings WITHOUT deleting anything: source is
    never destroyed, so a noisy turn is marked and excluded from distillation input,
    while the turn itself stays exactly as it arrived.
    """

    provenance: SourceProvenance
    role: TurnRole
    text: str
    privacy: PrivacyAssessment = UNCLASSIFIED
    review_status: ReviewStatus = ReviewStatus.OK
    review_reasons: tuple[str, ...] = ()
    meta_noise_markers: tuple[str, ...] = ()
    normalization_version: str = NORMALIZATION_VERSION

    @property
    def text_digest(self) -> str:
        return sha256_text(self.text)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_meta_noise(self) -> bool:
        return bool(self.meta_noise_markers)

    def to_body_free_dict(self) -> dict:
        """Everything about this turn EXCEPT the body. The only form that may be written
        to a report, a manifest or a log."""
        return {
            "provenance": self.provenance.to_dict(),
            "role": self.role.value,
            "text_digest": self.text_digest,
            "char_count": self.char_count,
            "privacy": self.privacy.to_dict(),
            "review_status": self.review_status.value,
            "review_reasons": list(self.review_reasons[:MAX_ITEMS]),
            "meta_noise_markers": list(self.meta_noise_markers[:MAX_ITEMS]),
            "normalization_version": self.normalization_version,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return body_free_repr(self, "role", "review_status", "normalization_version",
                              unit_id=self.provenance.unit_id,
                              text_digest=self.text_digest, chars=self.char_count)


@dataclass(frozen=True)
class CanonicalConversation:
    """A whole historical conversation, normalized (§7).

    ``family_id`` is assigned later by :mod:`reasoning_distillation.dedupe`; it is a field
    on this object rather than a computed property because grouping is TRANSITIVE — two
    conversations join a family through a third — so no single conversation can compute
    its own family from its own content.
    """

    conversation_id: str
    source_file_hash: str
    source_type: SourceType
    turns: tuple[CanonicalTurn, ...]
    review_status: ReviewStatus = ReviewStatus.OK
    review_reasons: tuple[str, ...] = ()
    source_model: str = ""
    source_date: str = ""
    family_id: str = ""
    normalization_version: str = NORMALIZATION_VERSION

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def digest(self) -> str:
        """Content identity of the whole conversation, body-free in its OUTPUT.

        Built from each turn's digest and role, so two exports of the same conversation
        collapse to one identity while two conversations that merely share a turn do not.
        """
        return sha256_obj({
            "conversation_id": self.conversation_id,
            "source_file_hash": self.source_file_hash,
            "turns": [{"role": t.role.value, "digest": t.text_digest}
                      for t in self.turns],
        })

    @property
    def export_status(self) -> ExportStatus:
        """The WEAKEST status among the turns. One blocked turn blocks the conversation.

        Deliberately not the majority or the modal value: a conversation is exportable
        only if every part of it is, and the aggregate of "safe, safe, blocked" is
        blocked.
        """
        statuses = {t.privacy.export_status for t in self.turns}
        if not statuses or ExportStatus.EXPORT_BLOCKED in statuses:
            return ExportStatus.EXPORT_BLOCKED
        if ExportStatus.EXPORT_UNKNOWN in statuses:
            return ExportStatus.EXPORT_UNKNOWN
        return ExportStatus.EXPORT_SAFE

    def roles_present(self) -> tuple[TurnRole, ...]:
        seen: list[TurnRole] = []
        for turn in self.turns:
            if turn.role not in seen:
                seen.append(turn.role)
        return tuple(seen)

    def has_reasoning(self) -> bool:
        """Whether any turn carries the assistant's own process.

        A conversation without one is not an error — §7 says missing is UNKNOWN, not
        invalid — but it cannot yield a decision procedure, and the quality gate says so
        with ``INSUFFICIENT_CONTEXT`` rather than inventing the reasoning.
        """
        return any(t.role in ASSISTANT_PROCESS_ROLES for t in self.turns)

    def to_body_free_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "source_file_hash": self.source_file_hash,
            "source_type": self.source_type.value,
            "digest": self.digest,
            "turn_count": self.turn_count,
            "roles_present": [r.value for r in self.roles_present()],
            "has_reasoning": self.has_reasoning(),
            "review_status": self.review_status.value,
            "review_reasons": list(self.review_reasons[:MAX_ITEMS]),
            "source_model": self.source_model,
            "source_model_known": bool(self.source_model),
            "source_date": self.source_date,
            "source_date_known": bool(self.source_date),
            "family_id": self.family_id,
            "export_status": self.export_status.value,
            "normalization_version": self.normalization_version,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return body_free_repr(self, "conversation_id", "source_type", "review_status",
                              "family_id", digest=self.digest, turns=self.turn_count)


# ══════════════════════════════════════════════════════════════════════════════════════
#  §10 — fact is not procedure
# ══════════════════════════════════════════════════════════════════════════════════════
class FactualState(str, Enum):
    """The truth status of a CLAIM, held entirely apart from the procedure around it.

    ``SOURCE_ONLY`` is the default and the honest answer for anything a historical trace
    asserted and nothing corroborates. §10 is emphatic: M67A does not verify the internet,
    but SOURCE_ONLY content must never silently become ground truth. There is no member
    meaning "probably true".
    """

    SOURCE_ONLY = "source_only"
    VERIFIED = "verified"
    STALE = "stale"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_usable_as_truth(self) -> bool:
        """Only one member is. Everything else is material, not authority."""
        return self is FactualState.VERIFIED

    @property
    def invalidates_procedure(self) -> bool:
        """Whether this factual state also condemns the reasoning around it.

        NOTHING here does, and that is the §10 rule made executable: "a stale fact does
        not automatically invalidate a useful reasoning procedure". A ``CONTRADICTED``
        claim is a reason to gate the CLAIM and to look at whether the procedure DEPENDED
        on it — which is :class:`FactProcedureLink`'s job — never a reason to discard the
        procedure by default.
        """
        return False


@dataclass(frozen=True)
class FactualClaim:
    """One assertion a historical trace made, with its status kept separate.

    ``claim_provenance`` reuses :class:`core.mesh_contracts.Provenance`, where
    ``MODEL_ASSERTED`` is deliberately outside ``CORROBORATING_PROVENANCE``: a model
    asserting its own conclusion is not evidence for it. Almost every claim in a
    historical assistant turn is ``MODEL_ASSERTED``, which is precisely why this type
    exists.
    """

    statement_digest: str
    state: FactualState = FactualState.SOURCE_ONLY
    claim_provenance: Provenance = Provenance.MODEL_ASSERTED
    claim_status: ClaimStatus = ClaimStatus.UNVERIFIED
    #: Short, bounded description. NOT the statement itself — the statement lives in the
    #: local corpus and only its digest travels into a record.
    topic: str = ""
    freshness_sensitive: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "statement_digest": self.statement_digest,
            "state": self.state.value,
            "claim_provenance": self.claim_provenance.value,
            "claim_status": self.claim_status.value,
            "topic": self.topic[:320],
            "freshness_sensitive": self.freshness_sensitive,
            "note": self.note[:320],
        }


@dataclass(frozen=True)
class FactProcedureLink:
    """Whether a procedure's validity DEPENDS on a claim being true (§10).

    This is the field that makes "fact != procedure" operational rather than slogan. Two
    cases the pipeline must tell apart:

      * a trace that says "Python 3.9 is current" and then correctly reasons *"a version
        claim is freshness-sensitive, so I must check rather than assert"*. The fact is
        stale; the procedure is excellent and ``procedure_depends_on_claim`` is False.
      * a trace that says "this API has no rate limit" and then builds a plan whose only
        justification is that claim. Here the procedure inherits the claim's status, and
        ``procedure_depends_on_claim`` is True.

    When the link cannot be decided from the source, it is ``UNKNOWN`` and the item
    routes to review. Guessing here would silently import the second case as the first.
    """

    claim_digest: str
    procedure_depends_on_claim: bool | None = None
    basis: str = ""

    @property
    def decided(self) -> bool:
        return self.procedure_depends_on_claim is not None

    def to_dict(self) -> dict:
        return {
            "claim_digest": self.claim_digest,
            "procedure_depends_on_claim": self.procedure_depends_on_claim,
            "decided": self.decided,
            "basis": self.basis[:320],
        }


# ══════════════════════════════════════════════════════════════════════════════════════
#  §11 — the distilled decision
# ══════════════════════════════════════════════════════════════════════════════════════
class Disposition(str, Enum):
    """The terminal quality decision for a distilled record (§14)."""

    ACCEPT = "accept"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECT = "reject"


class RejectReason(str, Enum):
    """Why a record is not ACCEPT (§14). A closed set, so a reason is auditable.

    ``UNKNOWN_SOURCE`` is the fail-closed member: a record whose provenance cannot be
    established is refused with a named reason rather than being quietly dropped, because
    a silent drop is indistinguishable from a record that never arrived.
    """

    DUPLICATE = "duplicate"
    LOW_INFORMATION = "low_information"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    MALFORMED_EXPORT = "malformed_export"
    META_POLICY_NOISE = "meta_policy_noise"
    UNSUPPORTED_DECISION = "unsupported_decision"
    FACT_REASONING_ENTANGLED = "fact_reasoning_entangled"
    PRIVACY_RISK = "privacy_risk"
    CONTRADICTORY_TRACE = "contradictory_trace"
    VERIFICATION_MISSING = "verification_missing"
    UNKNOWN_SOURCE = "unknown_source"
    CRITIC_DISAGREEMENT = "critic_disagreement"
    RECURSION_EXHAUSTED = "recursion_exhausted"
    QUALITY_FLOOR = "quality_floor"


@dataclass(frozen=True)
class TaskFrame:
    """§11 ``task`` — what the operator wanted, in JARVIS's own vocabulary.

    ``goal`` is an :class:`~core.epistemic_deliberation.IntentGoal`, so a distilled
    example and a live JARVIS turn describe their objective with the same 12 members.
    """

    domain: str
    goal: IntentGoal
    user_goal_summary: str = ""
    constraints: tuple[str, ...] = ()
    context_dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "domain": self.domain[:320],
            "goal": self.goal.value,
            "user_goal_summary": self.user_goal_summary[:MAX_FIELD_CHARS],
            "constraints": [c[:320] for c in self.constraints[:MAX_ITEMS]],
            "context_dependencies": [c[:320] for c in self.context_dependencies[:MAX_ITEMS]],
        }


@dataclass(frozen=True)
class EpistemicStateFrame:
    """§11 ``epistemic_state`` — what the trace knew, and what it did not.

    ``unknown`` and ``uncertainties`` being EMPTY is meaningful and is scored: a trace
    that lists nothing it was unsure about either had a trivial task or was overconfident,
    and the quality gate's ``uncertainty_handling`` dimension distinguishes those by
    looking at whether the task was freshness-sensitive.
    """

    known: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    freshness: FreshnessRequirement = FreshnessRequirement.STABLE
    premise_state: PremiseState = PremiseState.UNKNOWN

    @property
    def freshness_sensitive(self) -> bool:
        return self.freshness is not FreshnessRequirement.STABLE

    def to_dict(self) -> dict:
        return {
            "known": [k[:320] for k in self.known[:MAX_ITEMS]],
            "unknown": [u[:320] for u in self.unknown[:MAX_ITEMS]],
            "assumptions": [a[:320] for a in self.assumptions[:MAX_ITEMS]],
            "uncertainties": [u[:320] for u in self.uncertainties[:MAX_ITEMS]],
            "freshness": self.freshness.value,
            "freshness_sensitive": self.freshness_sensitive,
            "premise_state": self.premise_state.value,
        }


@dataclass(frozen=True)
class ToolDecisionFrame:
    """§11 ``decision.tool_decision`` — need, selection and what actually happened.

    ``need`` and ``execution`` are separate because M66A already established that
    availability, need and execution are three different things, and a historical trace
    conflates them constantly: "I have a search tool" is not "this turn needs search" is
    not "search ran and returned".
    """

    need: ToolNeed = ToolNeed.NONE
    execution: ToolExecutionState = ToolExecutionState.NOT_ATTEMPTED
    selected: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    basis: str = ""

    def to_dict(self) -> dict:
        return {
            "need": self.need.value,
            "execution": self.execution.value,
            "selected": [s[:320] for s in self.selected[:MAX_ITEMS]],
            "rejected": [s[:320] for s in self.rejected[:MAX_ITEMS]],
            "basis": self.basis[:320],
        }


@dataclass(frozen=True)
class DecisionFrame:
    """§11 ``decision`` — the reusable procedure itself.

    ``reasoning_operators`` names members of :mod:`reasoning_distillation.operators`; an
    unrecognised operator is refused by :meth:`validated` rather than stored as free text,
    because a taxonomy whose members are whatever the extractor wrote is not a taxonomy.
    """

    reasoning_operators: tuple[str, ...] = ()
    subproblems: tuple[str, ...] = ()
    candidate_approaches: tuple[str, ...] = ()
    rejected_approaches: tuple[str, ...] = ()
    chosen_approach: str = ""
    decision_basis: str = ""
    tool_decision: ToolDecisionFrame = field(default_factory=ToolDecisionFrame)
    verification_plan: tuple[str, ...] = ()
    verification_status: VerificationStatus = VerificationStatus.NOT_REQUIRED
    stop_condition: str = ""
    deliberation_mode: DeliberationMode = DeliberationMode.DIRECT

    def to_dict(self) -> dict:
        return {
            "reasoning_operators": list(self.reasoning_operators[:MAX_ITEMS]),
            "subproblems": [s[:320] for s in self.subproblems[:MAX_ITEMS]],
            "candidate_approaches": [a[:320] for a in self.candidate_approaches[:MAX_ITEMS]],
            "rejected_approaches": [a[:320] for a in self.rejected_approaches[:MAX_ITEMS]],
            "chosen_approach": self.chosen_approach[:MAX_FIELD_CHARS],
            "decision_basis": self.decision_basis[:MAX_FIELD_CHARS],
            "tool_decision": self.tool_decision.to_dict(),
            "verification_plan": [v[:320] for v in self.verification_plan[:MAX_ITEMS]],
            "verification_status": self.verification_status.value,
            "stop_condition": self.stop_condition[:320],
            "deliberation_mode": self.deliberation_mode.value,
        }


@dataclass(frozen=True)
class OutcomeFrame:
    """§11 ``outcome`` — what the trace produced, by reference.

    ``final_answer_ref`` is a DIGEST, never the answer. The answer is historical model
    output; quoting it into a committed artifact would both leak private conversation
    content and invite exactly the token-for-token imitation §1 forbids.

    ``observed_success`` is a tri-state (``None`` = UNKNOWN), because a historical export
    almost never records whether the answer actually worked. Defaulting it to True would
    manufacture a success label out of nothing, and those labels are what a future
    experiment would train on.
    """

    final_answer_ref: str = ""
    self_corrections: tuple[str, ...] = ()
    observed_success: bool | None = None
    quality_labels: tuple[str, ...] = ()

    @property
    def success_known(self) -> bool:
        return self.observed_success is not None

    def to_dict(self) -> dict:
        return {
            "final_answer_ref": self.final_answer_ref,
            "self_corrections": [c[:320] for c in self.self_corrections[:MAX_ITEMS]],
            "observed_success": self.observed_success,
            "success_known": self.success_known,
            "quality_labels": list(self.quality_labels[:MAX_ITEMS]),
        }


@dataclass(frozen=True)
class DistilledProvenance:
    """§11 ``provenance`` for a distilled record: which source segments it came from.

    ``source_unit_ids`` is the verified link back to immutable source. §22's absent-control
    test asserts that every ACCEPT item has one, because a distilled record whose source
    cannot be re-read is unfalsifiable: nobody can ever check whether the extraction was
    faithful.
    """

    conversation_id: str
    family_id: str
    source_file_hashes: tuple[str, ...]
    source_unit_ids: tuple[str, ...]
    source_model: str = ""
    source_date: str = ""
    source_type: SourceType = SourceType.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "family_id": self.family_id,
            "source_file_hashes": sorted(set(self.source_file_hashes))[:MAX_ITEMS],
            "source_unit_ids": sorted(set(self.source_unit_ids))[:MAX_ITEMS],
            "source_model": self.source_model,
            "source_model_known": bool(self.source_model),
            "source_date": self.source_date,
            "source_date_known": bool(self.source_date),
            "source_type": self.source_type.value,
        }


@dataclass(frozen=True)
class DistilledDecision:
    """One distilled reasoning procedure (§11). The unit a future experiment would use.

    Immutable. A repair pass produces a NEW record whose ``parent_artifact_hash`` points
    at the one it replaced, so the recursion is a chain that can be walked rather than a
    field that was overwritten (§12).
    """

    example_id: str
    provenance: DistilledProvenance
    task: TaskFrame
    epistemic_state: EpistemicStateFrame
    decision: DecisionFrame
    outcome: OutcomeFrame
    privacy: PrivacyAssessment = UNCLASSIFIED
    factual_claims: tuple[FactualClaim, ...] = ()
    fact_procedure_links: tuple[FactProcedureLink, ...] = ()
    schema_version: str = SCHEMA_VERSION
    #: The extraction stage that produced this artifact, and its parent in the chain.
    stage: str = "extract"
    parent_artifact_hash: str = ""
    depth: int = 0

    @property
    def artifact_hash(self) -> str:
        """Content address of this artifact. Derived from the record, never supplied.

        ``parent_artifact_hash`` and ``depth`` are INSIDE the hash: two repair passes that
        happen to converge on the same content at different depths are different events in
        the audit chain, and collapsing them would make the recursion look shorter than it
        was.
        """
        return sha256_obj(self.to_dict())

    def to_dict(self) -> dict:
        """The canonical, body-free serialisation. This is what gets hashed and written."""
        return {
            "schema_version": self.schema_version,
            "example_id": self.example_id,
            "provenance": self.provenance.to_dict(),
            "task": self.task.to_dict(),
            "epistemic_state": self.epistemic_state.to_dict(),
            "decision": self.decision.to_dict(),
            "outcome": self.outcome.to_dict(),
            "privacy": self.privacy.to_dict(),
            "factual_claims": [c.to_dict() for c in self.factual_claims[:MAX_ITEMS]],
            "fact_procedure_links": [
                link.to_dict() for link in self.fact_procedure_links[:MAX_ITEMS]],
            "stage": self.stage,
            "parent_artifact_hash": self.parent_artifact_hash,
            "depth": self.depth,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def validated(self, *, known_operators: frozenset[str]) -> "DistilledDecision":
        """Structural validation. Raises; never repairs a record into looking valid."""
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise DistillationError(
                f"distilled record declares schema_version {self.schema_version!r}, which "
                f"this build does not support ({sorted(SUPPORTED_SCHEMA_VERSIONS)}); a "
                f"best-effort read of an unknown schema reads moved fields confidently "
                f"and wrongly")
        if not self.example_id:
            raise DistillationError("distilled record has no example_id")
        if not self.provenance.source_unit_ids:
            raise DistillationError(
                f"{self.example_id}: no source_unit_ids. A distilled record whose source "
                f"cannot be re-read is unfalsifiable, so it is refused rather than stored")
        if not self.provenance.family_id:
            raise DistillationError(
                f"{self.example_id}: no family_id; an ungrouped record can cross a split")
        unknown_ops = sorted(set(self.decision.reasoning_operators) - known_operators)
        if unknown_ops:
            raise DistillationError(
                f"{self.example_id}: unknown reasoning operator(s) {unknown_ops}; the "
                f"taxonomy is closed, and free text in this field is not an operator")
        if self.depth < 0:
            raise DistillationError(f"{self.example_id}: depth must not be negative")
        if self.depth > 0 and not self.parent_artifact_hash:
            raise DistillationError(
                f"{self.example_id}: depth {self.depth} with no parent_artifact_hash; a "
                f"repair pass with no parent has an unwalkable chain (§12)")
        claim_digests = {c.statement_digest for c in self.factual_claims}
        orphan_links = sorted({link.claim_digest for link in self.fact_procedure_links}
                              - claim_digests)
        if orphan_links:
            raise DistillationError(
                f"{self.example_id}: fact_procedure_links reference claim(s) not in "
                f"factual_claims: {orphan_links[:4]}")
        self.privacy.validated()
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return body_free_repr(self, "example_id", "schema_version", "stage", "depth",
                              family_id=self.provenance.family_id,
                              artifact_hash=self.artifact_hash)


def check_record_version(payload: dict, *, label: str = "distilled record") -> str:
    """Refuse a payload whose schema version this build does not know.

    Separate from :meth:`DistilledDecision.validated` on purpose: this runs BEFORE the
    fields are read, so an unknown version never reaches code that assumes a field means
    what it meant in this version.
    """
    if not isinstance(payload, dict):
        raise DistillationError(f"{label}: expected an object, got {type(payload).__name__}")
    version = payload.get("schema_version")
    if not isinstance(version, str) or not version:
        raise DistillationError(
            f"{label}: no schema_version. An unversioned record cannot be migrated and "
            f"cannot be refused later; it is refused now")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DistillationError(
            f"{label}: schema_version {version!r} is not supported by this build "
            f"({sorted(SUPPORTED_SCHEMA_VERSIONS)})")
    return version


__all__ = [
    "ASSISTANT_PROCESS_ROLES", "MAX_FIELD_CHARS", "MAX_ITEMS", "NORMALIZATION_VERSION",
    "SCHEMA_VERSION", "SUPPORTED_SCHEMA_VERSIONS", "UNCLASSIFIED",
    "CanonicalConversation", "CanonicalTurn", "DecisionFrame", "DistilledDecision",
    "DistilledProvenance", "Disposition", "DistillationError", "EpistemicStateFrame",
    "ExportStatus", "FactProcedureLink", "FactualClaim", "FactualState",
    "OutcomeFrame", "PrivacyAssessment", "RejectReason", "ReviewStatus",
    "SourceProvenance", "SourceType", "TaskFrame", "ToolDecisionFrame", "TurnRole",
    "check_record_version",
]
