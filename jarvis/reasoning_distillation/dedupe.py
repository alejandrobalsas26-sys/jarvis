"""reasoning_distillation/dedupe.py — V69 M67A: duplicates, and the families they form.

WHY DEDUPLICATION COMES *BEFORE* LEARNING, NOT AFTER
----------------------------------------------------
§8's title is the argument. Historical exports repeat themselves constantly — the same
conversation saved twice, a question re-asked with a typo fixed, a long thread exported both
whole and in pieces. If those reach the split stage as independent items, two things go wrong
at once and only one of them is visible:

  * the corpus is smaller than it appears, so every count in every report overstates;
  * **two copies of one problem land on opposite sides of the holdout boundary**, and the
    held-out measurement is then measuring memorisation. That one is invisible in the
    numbers: the score simply looks better than it is.

The second failure is why this module's real output is not a duplicate list but a **family
partition**. A family is the unit that moves as a whole through
:mod:`reasoning_distillation.splitting`, and it is the only thing that makes §9's
anti-leakage rule enforceable.

WHAT REPRESENTS A CONVERSATION FOR GROUPING
-------------------------------------------
The **problem**, not the answer. The grouping signature is built from the ``USER_REQUEST``
turns only. That choice is load-bearing:

  * grouping on the ANSWER would merge two unrelated problems that happened to be answered
    in a similar style, and would separate two attempts at the same problem that were
    answered differently — which is exactly backwards, because two attempts at one problem
    are precisely what must not straddle the boundary;
  * grouping on the WHOLE conversation dilutes a short question inside a long answer, so
    two conversations asking the same thing score as dissimilar because their replies differ.

A conversation with no ``USER_REQUEST`` turn cannot be grouped on its problem. It is NOT
silently grouped on something else: it becomes a SINGLETON family whose id records that the
grouping basis was absent, and :mod:`reasoning_distillation.splitting` refuses to place an
ungrouped item at all. Failing closed here is cheap; a mis-grouped family is not.

WHY THE SIMILARITY ENGINE IS IMPORTED
-------------------------------------
:mod:`training_gym.datasets.similarity` already does deterministic, offline, family-aware
near-duplicate detection with a character-n-gram view and a token-shingle view, a sound
length-bucket prune, and a comparison ceiling that is REPORTED rather than absorbed. It is
imported, with its thresholds, so M67A cannot end up with a weaker definition of
"near-duplicate" than the training-corpus pipeline already enforces. ``SIMILARITY_VERSION``
travels into the report.

Its family-awareness is used, not bypassed: a conversation whose problem statement contains a
code fence is normalized as :attr:`~training_gym.task_spec.TaskFamily.CODING_FIX`, i.e.
character-exact, because casefolding and whitespace-collapsing a code snippet would make a
vulnerable patch and its fix look like the same problem.

WHAT "DETERMINISTIC" MEANS HERE
-------------------------------
No model, no network, no clock, no iteration over an unordered set. Every loop is over a
sorted sequence and every id is a digest of sorted content, so two runs over the same corpus
produce byte-identical families and a byte-identical report — which is what §20 requires of
a deterministic stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from training_gym.datasets.similarity import (
    SIMILARITY_VERSION,
    SimilaritySignature,
    compare_groups,
    normalized_key,
    signature,
)
from training_gym.schemas import SchemaError, sha256_obj
from training_gym.task_spec import TaskFamily

from .config import DedupeConfig
from .models import CanonicalConversation, TurnRole

#: Bump when the grouping BASIS changes (which turns form the signature, or how families are
#: derived). Distinct from SIMILARITY_VERSION, which versions the scoring engine: a family
#: partition can change without the scorer moving, and a reader needs to know which did.
DEDUPE_VERSION = "m67a.dedupe.1"

#: Markers that make a problem statement character-exact for normalization purposes.
_CODE_MARKERS = ("```", "    def ", "\tdef ", "def ", "class ", "SELECT ", "#!/",
                 "Traceback (most recent call last)", "{", "};")


class DedupeError(SchemaError):
    """A grouping was refused. Never a partial or silently-merged family."""


@dataclass(frozen=True)
class ProblemSignature:
    """One conversation's comparable problem statement.

    ``text`` is a BODY — the operator's own question. It exists only inside this stage, is
    never stored on a family or a report, and the class carries a body-free ``__repr__``
    because a signature list is exactly the kind of thing that ends up in a debug print.
    """

    conversation_id: str
    conversation_digest: str
    source_file_hash: str
    text: str
    exact_key: str
    character_exact: bool
    signature: SimilaritySignature

    @property
    def comparable(self) -> bool:
        """Whether this signature has enough material to be worth comparing."""
        return bool(self.text.strip())

    def __repr__(self) -> str:  # pragma: no cover
        return (f"ProblemSignature(conversation_id={self.conversation_id!r}, "
                f"exact_key={self.exact_key[:12]!r}, character_exact={self.character_exact}, "
                f"chars={len(self.text)})")


def _looks_like_code(text: str) -> bool:
    """Whether a problem statement's characters carry meaning that must not be folded."""
    return any(marker in text for marker in _CODE_MARKERS)


def problem_text(conversation: CanonicalConversation) -> str:
    """The grouping basis: the operator's own request turns, in order.

    Joined with a record separator rather than a newline so that a two-turn question and a
    one-turn question with an embedded newline cannot normalize to the same string.
    """
    return "\x1e".join(t.text for t in conversation.turns
                       if t.role is TurnRole.USER_REQUEST)


def build_signature(conversation: CanonicalConversation) -> ProblemSignature:
    """Build one conversation's comparable signature. Pure and deterministic."""
    text = problem_text(conversation)
    character_exact = _looks_like_code(text)
    family = TaskFamily.CODING_FIX if character_exact else None
    return ProblemSignature(
        conversation_id=conversation.conversation_id,
        conversation_digest=conversation.digest,
        source_file_hash=conversation.source_file_hash,
        text=text,
        exact_key=normalized_key(text, family=family),
        character_exact=character_exact,
        signature=signature(conversation.digest, text, family=family),
    )


@dataclass(frozen=True)
class NearDuplicateEvidence:
    """Why two conversations were treated as the same problem. Auditable, body-free."""

    left_digest: str
    right_digest: str
    char_score: float
    token_score: float
    blocked: bool

    @property
    def score(self) -> float:
        return max(self.char_score, self.token_score)

    def to_dict(self) -> dict:
        return {"left": self.left_digest, "right": self.right_digest,
                "char_score": round(self.char_score, 6),
                "token_score": round(self.token_score, 6),
                "score": round(self.score, 6), "blocked": self.blocked}


@dataclass(frozen=True)
class DuplicateFamily:
    """One problem family: a canonical member plus everything that duplicates it (§8).

    ``family_id`` is a digest of the SORTED member digests, so it is stable under any
    iteration order and changes if and only if the membership changes. It is deliberately
    not the canonical member's digest: that would make the family id move when a new,
    lexicographically-smaller duplicate is imported, and every earlier report would then
    name a family that no longer exists.
    """

    family_id: str
    canonical_digest: str
    member_digests: tuple[str, ...]
    exact_duplicate_digests: tuple[str, ...] = ()
    near_duplicate_digests: tuple[str, ...] = ()
    evidence: tuple[NearDuplicateEvidence, ...] = ()
    source_file_hashes: tuple[str, ...] = ()
    #: True when the family has no problem statement to group on. Recorded, never hidden:
    #: such a family is a singleton by refusal, not by measurement.
    grouping_basis_absent: bool = False

    @property
    def size(self) -> int:
        return len(self.member_digests)

    def to_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "canonical_digest": self.canonical_digest,
            "size": self.size,
            "member_digests": list(self.member_digests),
            "exact_duplicate_digests": list(self.exact_duplicate_digests),
            "near_duplicate_digests": list(self.near_duplicate_digests),
            "evidence": [e.to_dict() for e in self.evidence],
            "source_file_hashes": list(self.source_file_hashes),
            "grouping_basis_absent": self.grouping_basis_absent,
        }


@dataclass(frozen=True)
class DedupeReport:
    """The auditable account §8 asks for. Body-free by construction."""

    families: tuple[DuplicateFamily, ...]
    assignments: dict[str, str] = field(default_factory=dict)
    comparisons: int = 0
    skipped_by_length: int = 0
    ceiling_reached: bool = False
    dedupe_version: str = DEDUPE_VERSION
    similarity_version: str = SIMILARITY_VERSION
    block_threshold: float = 0.0
    warn_threshold: float = 0.0

    @property
    def family_count(self) -> int:
        return len(self.families)

    @property
    def exact_duplicate_count(self) -> int:
        return sum(len(f.exact_duplicate_digests) for f in self.families)

    @property
    def near_duplicate_count(self) -> int:
        return sum(len(f.near_duplicate_digests) for f in self.families)

    def family_of(self, conversation_digest: str) -> str:
        """The family a conversation belongs to. Raises rather than returning a default.

        A missing assignment means the conversation was never grouped, and a caller that
        received ``""`` would place it somewhere. §17 requires source-identity uncertainty to
        fail closed, so this raises.
        """
        found = self.assignments.get(conversation_digest)
        if not found:
            raise DedupeError(
                f"conversation {conversation_digest[:12]} has no family assignment; an "
                f"ungrouped item must not be placed into a split (§9)")
        return found

    def to_dict(self) -> dict:
        return {
            "dedupe_version": self.dedupe_version,
            "similarity_version": self.similarity_version,
            "block_threshold": self.block_threshold,
            "warn_threshold": self.warn_threshold,
            "family_count": self.family_count,
            "exact_duplicate_count": self.exact_duplicate_count,
            "near_duplicate_count": self.near_duplicate_count,
            "comparisons": self.comparisons,
            "skipped_by_length": self.skipped_by_length,
            "ceiling_reached": self.ceiling_reached,
            "search_complete": not self.ceiling_reached,
            "families": [f.to_dict() for f in self.families],
            "assignments": dict(sorted(self.assignments.items())),
        }


class _Union:
    """Union-find over conversation digests, with a deterministic representative.

    The representative is always the lexicographically smallest member, so the structure's
    output does not depend on the order links were added. Without that, two runs that
    discovered the same links in a different order would produce differently-keyed families
    with identical membership.
    """

    def __init__(self, keys: "list[str]") -> None:
        self._parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        parent = self._parent
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> "dict[str, list[str]]":
        out: dict[str, list[str]] = {}
        for key in sorted(self._parent):
            out.setdefault(self.find(key), []).append(key)
        return out


def analyze(conversations: "list[CanonicalConversation]", *,
            config: DedupeConfig | None = None) -> DedupeReport:
    """Group *conversations* into problem families and report the evidence (§8).

    Three link types, applied in this order and all transitive:

      1. **identical conversation digest** — the same file imported twice. Collapsed first so
         it never consumes a similarity comparison.
      2. **identical exact key** — different bytes, same problem statement after
         normalization. This is what catches a re-asked question with a fixed typo.
      3. **near-duplicate at or above the block threshold** — scored by the imported engine.

    Links are transitive by construction: if A links B and B links C, all three are one
    family even when A and C score below the threshold against each other. A chain of
    pairwise overlaps left as three families is the same hole
    :func:`training_gym.datasets.split.group_candidates` closed with a union-find, and for the
    same reason.
    """
    cfg = config or DedupeConfig()
    if not conversations:
        return DedupeReport(families=(), block_threshold=cfg.near_duplicate_block,
                            warn_threshold=cfg.near_duplicate_warn)

    signatures = {c.digest: build_signature(c) for c in conversations}
    if len(signatures) != len({c.digest for c in conversations}):  # pragma: no cover
        raise DedupeError("internal: signature map lost a conversation digest")

    union = _Union(sorted(signatures))

    # 1 + 2: exact links. `exact_key` of an empty problem statement is the digest of the
    # empty string, which would merge EVERY ungroupable conversation into one family — the
    # worst possible outcome, since it would make unrelated problems share a split. So
    # signatures with no problem text are excluded from exact linking entirely.
    by_exact: dict[str, list[str]] = {}
    for digest, sig in sorted(signatures.items()):
        if not sig.comparable:
            continue
        by_exact.setdefault(sig.exact_key, []).append(digest)
    exact_partners: dict[str, set[str]] = {}
    for members in by_exact.values():
        if len(members) < 2:
            continue
        head = members[0]
        for other in members[1:]:
            union.union(head, other)
            exact_partners.setdefault(head, set()).add(other)
            exact_partners.setdefault(other, set()).add(head)

    # 3: near-duplicate links. Only comparable signatures long enough to carry signal.
    comparable = [sig for _, sig in sorted(signatures.items())
                  if sig.comparable and len(sig.text) >= cfg.min_chars_for_similarity]
    sig_list = [sig.signature for sig in comparable]
    evidence: list[NearDuplicateEvidence] = []
    comparisons = 0
    skipped = 0
    ceiling = False
    if len(sig_list) >= 2:
        result = compare_groups(sig_list, sig_list,
                                threshold=cfg.near_duplicate_warn,
                                max_comparisons=cfg.max_comparisons)
        comparisons, skipped, ceiling = (result.comparisons, result.skipped_by_length,
                                         result.ceiling_reached)
        seen_pairs: set[tuple[str, str]] = set()
        for hit in result.hits:
            pair = (min(hit.left_key, hit.right_key), max(hit.left_key, hit.right_key))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            blocked = hit.score >= cfg.near_duplicate_block
            evidence.append(NearDuplicateEvidence(
                left_digest=pair[0], right_digest=pair[1], char_score=hit.char_score,
                token_score=hit.token_score, blocked=blocked))
            if blocked:
                union.union(pair[0], pair[1])

    # Build families from the settled partition.
    near_partners: dict[str, set[str]] = {}
    for item in evidence:
        if item.blocked:
            near_partners.setdefault(item.left_digest, set()).add(item.right_digest)
            near_partners.setdefault(item.right_digest, set()).add(item.left_digest)

    families: list[DuplicateFamily] = []
    assignments: dict[str, str] = {}
    for _, members in sorted(union.groups().items()):
        ordered = tuple(sorted(members))
        family_id = sha256_obj({"members": list(ordered), "version": DEDUPE_VERSION})
        canonical = ordered[0]
        exact_dupes = tuple(sorted(
            d for d in ordered if d != canonical and d in exact_partners.get(canonical, set())))
        near_dupes = tuple(sorted(
            d for d in ordered if d != canonical and d not in exact_dupes))
        family_evidence = tuple(
            e for e in evidence if e.left_digest in members and e.right_digest in members)
        basis_absent = all(not signatures[d].comparable for d in ordered)
        families.append(DuplicateFamily(
            family_id=family_id,
            canonical_digest=canonical,
            member_digests=ordered,
            exact_duplicate_digests=exact_dupes,
            near_duplicate_digests=near_dupes,
            evidence=family_evidence,
            source_file_hashes=tuple(sorted({signatures[d].source_file_hash for d in ordered})),
            grouping_basis_absent=basis_absent,
        ))
        for digest in ordered:
            assignments[digest] = family_id

    return DedupeReport(
        families=tuple(sorted(families, key=lambda f: f.family_id)),
        assignments=assignments,
        comparisons=comparisons,
        skipped_by_length=skipped,
        ceiling_reached=ceiling,
        block_threshold=cfg.near_duplicate_block,
        warn_threshold=cfg.near_duplicate_warn,
    )


def versions() -> dict:
    """The version block the manifest records for this stage (§20)."""
    return {"dedupe_version": DEDUPE_VERSION, "similarity_version": SIMILARITY_VERSION,
            "grouping_basis": "USER_REQUEST turns only, joined by U+001E",
            "link_types": ["identical_conversation_digest", "identical_exact_key",
                           "near_duplicate_at_or_above_block_threshold"],
            "transitive": True}


__all__ = [
    "DEDUPE_VERSION", "DedupeError", "DedupeReport", "DuplicateFamily",
    "NearDuplicateEvidence", "ProblemSignature", "analyze", "build_signature",
    "problem_text", "versions",
]
