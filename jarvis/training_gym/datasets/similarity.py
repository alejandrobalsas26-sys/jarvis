"""training_gym/datasets/similarity.py — V69 M62 S2d: bounded local near-duplicate.

WHY THIS EXISTS
---------------
Exact-duplicate detection is easy and insufficient. Two examples that differ by a
reworded sentence are, for training purposes, the same example — and if one of them is in
TRAIN and the other in HIDDEN_EVALUATION, the held-out score is measuring memorisation.
So the leakage analyzer needs a similarity number, and it needs one that works with no
model, no network and no download.

NORMALIZATION IS THE DANGEROUS PART
-----------------------------------
Every normalization step deletes information, and the question is always *whose*
information. Lowercasing and whitespace-collapsing look harmless until you notice what
they do to this repository's actual material:

  * YARA is case-sensitive without ``nocase``. ``$a = "Invoke-Mimikatz"`` and
    ``"invoke-mimikatz"`` are a working rule and a broken one; lowercasing makes them one
    example and silently drops whichever arrives second.
  * In Sigma and in Python, indentation IS the nesting. A ``return`` inside an ``if`` and
    the same ``return`` outside it — the vulnerable patch and the fix — collapse to one
    string under whitespace normalization.
  * A refusal and a jailbreak differ by a paragraph break: ``"I cannot help.\\n\\nBut
    here is how:"`` normalizes to the same text as the safe answer.

So :func:`normalize_for_similarity` is FAMILY-AWARE. For families where the characters
carry the meaning it normalizes Unicode form and line endings and stops. It never
normalizes its way to a collision between two examples that a grader would score
differently.

Normalization here is for SIMILARITY TRIAGE ONLY. The identity of a candidate is
:meth:`~training_gym.datasets.candidate.DatasetCandidate.candidate_hash`, computed over
:func:`~training_gym.schemas.canonical_json` of the untransformed record. Nothing in this
module ever feeds a dedup decision that deletes a record.

BOUNDED, AND HONEST ABOUT ITS BOUNDS
------------------------------------
All-pairs comparison is O(n²) and a dataset of 20 000 candidates is 200 million pairs.
The analyzer buckets by length and by content signature so that only plausible pairs are
compared, and it counts every comparison against
:data:`DEFAULT_MAX_COMPARISONS`. When the ceiling is reached the result says so — it does
NOT return "no duplicates found", because a search that stopped early found nothing in
the same way a search that never started found nothing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..schemas import SchemaError, sha256_text
from ..task_spec import TaskFamily

#: Bump when a normalization rule or a threshold below changes. Recorded in every report.
SIMILARITY_VERSION = "m62.similarity.1"

#: Character n-gram width. Five is small enough to survive a reworded sentence and large
#: enough that ordinary English does not collide with itself.
CHAR_NGRAM_N = 5

#: Token shingle width, for the word-level view.
TOKEN_SHINGLE_K = 3

#: Jaccard at or above this across two different splits is treated as the same example.
BLOCK_THRESHOLD = 0.80

#: Jaccard at or above this is reported, but does not block on its own.
WARN_THRESHOLD = 0.60

#: Texts whose lengths differ by more than this ratio cannot reach BLOCK_THRESHOLD, so
#: they are never compared. A pure optimisation — it changes cost, not verdicts.
LENGTH_RATIO_FLOOR = 0.55

#: Hard ceiling on pair comparisons in one analysis. Reaching it is REPORTED, never
#: silently absorbed.
DEFAULT_MAX_COMPARISONS = 2_000_000

#: Families whose material is character-exact: case, indentation and blank lines all
#: carry meaning a grader measures, so normalization stops at Unicode form and newlines.
CHARACTER_EXACT_FAMILIES: frozenset[TaskFamily] = frozenset({
    TaskFamily.SIGMA_RULE, TaskFamily.YARA_RULE, TaskFamily.SURICATA_RULE,
    TaskFamily.CODING_FIX, TaskFamily.SAFETY_REFUSAL, TaskFamily.TOOL_CALL_SCHEMA,
})

_WHITESPACE_RUN = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class SimilarityError(SchemaError):
    """A similarity computation was refused. Never a silently degraded result."""


def normalize_for_similarity(text: str, *, family: TaskFamily | None = None) -> str:
    """Deterministic, family-aware normalization for similarity triage only.

    Always applied, for every family: NFKC Unicode normalization (so two encodings of
    the same character are one character) and CRLF/CR to LF (so a file that crossed
    platforms is not a different example).

    Applied ONLY where the characters are not the meaning: horizontal whitespace runs
    collapse to one space, runs of blank lines collapse to two, trailing whitespace goes,
    and the text is casefolded. For the families in
    :data:`CHARACTER_EXACT_FAMILIES` none of that happens — see the module docstring for
    the four ways it would create a collision between a correct answer and a wrong one.
    """
    if not isinstance(text, str):
        raise SimilarityError(f"similarity: expected str, got {type(text).__name__}")
    out = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    if family is not None and family in CHARACTER_EXACT_FAMILIES:
        return out.strip("\n")
    out = _WHITESPACE_RUN.sub(" ", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    out = _BLANK_RUN.sub("\n\n", out)
    return out.casefold().strip()


def normalized_key(text: str, *, family: TaskFamily | None = None) -> str:
    """The digest of the normalized form. The normalized-duplicate index key."""
    return sha256_text(normalize_for_similarity(text, family=family))


def char_ngrams(text: str, n: int = CHAR_NGRAM_N) -> frozenset[str]:
    """Character n-grams. Language-agnostic and robust to word reordering."""
    if n < 1:
        raise SimilarityError("similarity: n-gram width must be at least 1")
    if len(text) < n:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[i:i + n] for i in range(len(text) - n + 1))


def token_shingles(text: str, k: int = TOKEN_SHINGLE_K) -> frozenset[str]:
    """Overlapping k-word shingles. Catches reordering that n-grams smear over."""
    tokens = _TOKEN.findall(text)
    if len(tokens) < k:
        return frozenset({" ".join(tokens)}) if tokens else frozenset()
    return frozenset(" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Intersection over union. Two empty sets are 0.0, not 1.0.

    Deliberate: "these two texts have no features" is not evidence that they are the same
    text, and returning 1.0 there would make every degenerate record a duplicate of every
    other one.
    """
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def comparable_lengths(a: int, b: int) -> bool:
    """True when two texts are close enough in length to possibly be near-duplicates."""
    if a <= 0 or b <= 0:
        return a == b
    return (min(a, b) / max(a, b)) >= LENGTH_RATIO_FLOOR


@dataclass(frozen=True)
class SimilaritySignature:
    """Everything needed to compare one text, computed once."""

    key: str
    length: int
    normalized_hash: str
    ngrams: frozenset[str] = field(default_factory=frozenset)
    shingles: frozenset[str] = field(default_factory=frozenset)

    @property
    def length_bucket(self) -> int:
        """Coarse length band, so only plausibly-similar texts are ever compared."""
        return self.length.bit_length()


def signature(key: str, text: str, *, family: TaskFamily | None = None
              ) -> SimilaritySignature:
    """Build the comparison signature for one text."""
    normalized = normalize_for_similarity(text, family=family)
    return SimilaritySignature(
        key=key, length=len(normalized), normalized_hash=sha256_text(normalized),
        ngrams=char_ngrams(normalized), shingles=token_shingles(normalized))


@dataclass(frozen=True)
class SimilarityHit:
    """One pair that scored above a reporting threshold. Names both sides."""

    left_key: str
    right_key: str
    char_score: float
    token_score: float

    @property
    def score(self) -> float:
        """The stronger of the two views. A near-duplicate that only one view sees is
        still a near-duplicate, and taking the max fails towards reporting."""
        return max(self.char_score, self.token_score)

    def to_dict(self) -> dict:
        return {"left_key": self.left_key, "right_key": self.right_key,
                "char_score": round(self.char_score, 6),
                "token_score": round(self.token_score, 6),
                "score": round(self.score, 6)}


@dataclass(frozen=True)
class SimilarityResult:
    """What the comparison actually managed to examine, and what it found."""

    hits: tuple[SimilarityHit, ...] = ()
    comparisons: int = 0
    skipped_by_length: int = 0
    ceiling_reached: bool = False
    max_comparisons: int = DEFAULT_MAX_COMPARISONS

    @property
    def complete(self) -> bool:
        """True only when every candidate pair was either compared or provably could
        not have scored. A ceiling hit means the answer is unknown, not clean."""
        return not self.ceiling_reached

    def to_dict(self) -> dict:
        return {"version": SIMILARITY_VERSION,
                "hits": [h.to_dict() for h in self.hits],
                "comparisons": self.comparisons,
                "skipped_by_length": self.skipped_by_length,
                "ceiling_reached": self.ceiling_reached,
                "max_comparisons": self.max_comparisons,
                "complete": self.complete}


def compare_groups(left: list[SimilaritySignature], right: list[SimilaritySignature],
                   *, threshold: float = WARN_THRESHOLD,
                   max_comparisons: int = DEFAULT_MAX_COMPARISONS) -> SimilarityResult:
    """Every cross-group pair scoring at or above *threshold*, within a hard budget.

    Bucketing by length band is the only pruning applied, and it is sound rather than
    heuristic: two texts whose lengths differ by more than
    :data:`LENGTH_RATIO_FLOOR` cannot reach :data:`BLOCK_THRESHOLD`, because their n-gram
    sets differ in size by more than the threshold permits. Skipped pairs are counted and
    reported so the number is auditable rather than asserted.
    """
    if max_comparisons < 1:
        raise SimilarityError("similarity: the comparison budget must be positive")
    hits: list[SimilarityHit] = []
    comparisons = 0
    skipped = 0
    buckets: dict[int, list[SimilaritySignature]] = {}
    for sig in right:
        buckets.setdefault(sig.length_bucket, []).append(sig)

    for a in sorted(left, key=lambda s: s.key):
        # Adjacent bands too: a bucket boundary must not hide a genuine near-duplicate.
        neighbours = [s for band in (a.length_bucket - 1, a.length_bucket,
                                     a.length_bucket + 1)
                      for s in buckets.get(band, ())]
        for b in sorted(neighbours, key=lambda s: s.key):
            if a.key == b.key:
                continue
            if not comparable_lengths(a.length, b.length):
                skipped += 1
                continue
            if comparisons >= max_comparisons:
                return SimilarityResult(
                    hits=tuple(sorted(hits, key=lambda h: (-h.score, h.left_key,
                                                           h.right_key))),
                    comparisons=comparisons, skipped_by_length=skipped,
                    ceiling_reached=True, max_comparisons=max_comparisons)
            comparisons += 1
            char_score = jaccard(a.ngrams, b.ngrams)
            token_score = jaccard(a.shingles, b.shingles)
            if max(char_score, token_score) >= threshold:
                hits.append(SimilarityHit(left_key=a.key, right_key=b.key,
                                          char_score=char_score,
                                          token_score=token_score))
    return SimilarityResult(
        hits=tuple(sorted(hits, key=lambda h: (-h.score, h.left_key, h.right_key))),
        comparisons=comparisons, skipped_by_length=skipped, ceiling_reached=False,
        max_comparisons=max_comparisons)


__all__ = [
    "BLOCK_THRESHOLD", "CHARACTER_EXACT_FAMILIES", "CHAR_NGRAM_N",
    "DEFAULT_MAX_COMPARISONS", "LENGTH_RATIO_FLOOR", "SIMILARITY_VERSION",
    "TOKEN_SHINGLE_K", "WARN_THRESHOLD", "SimilarityError", "SimilarityHit",
    "SimilarityResult", "SimilaritySignature", "char_ngrams", "comparable_lengths",
    "compare_groups", "jaccard", "normalize_for_similarity", "normalized_key",
    "signature", "token_shingles",
]
