"""core/compaction_quality.py — V69 M59.4.1: deterministic compaction quality gates.

WHY A GATE, NOT A JUDGE
-----------------------
M58 scheduled the model-assisted compaction pass and re-labelled every model
contribution ``INFERRED`` (a model can never mint EXPLICIT). That is necessary but
not sufficient: a proposer can still hallucinate a named entity that never appeared,
copy a secret out of excluded content, paste a code block, or quote the transcript
verbatim. This module is the DETERMINISTIC authority that decides which proposed
digest items survive — never another LLM. A rejected item is simply dropped; the
extractive digest is always the authoritative fallback.

WHAT IT ENFORCES (all deterministic, all content-free in its metrics)
---------------------------------------------------------------------
  * a model may only contribute TOPIC / DECISION / OPEN_QUESTION kinds;
  * a proposed item claiming EXPLICIT is rejected outright (no EXPLICIT minting);
  * bounded item length and a bounded accepted batch;
  * no raw code blocks, no excessive verbatim quotation of the transcript;
  * no secret-looking tokens (keys, bearer tokens, long hex/base64 runs);
  * SOURCE LINKAGE — an item may not introduce named entities or content tokens that
    never appeared in the conversation it summarises (invented entity / decision /
    preference rejected);
  * duplicate suppression, against both prior accepted items and EXPLICIT base items.

The metrics it returns are counts, ratios, milliseconds and reason labels — never a
prompt, an answer, an item body, or a secret. It reads the transcript ONLY to decide
linkage; it never stores it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum

from core.conversation_digest import DigestItem, Evidence, ItemKind

# Kinds a model-assisted pass may contribute (mirrors conversation_digest._MODEL_ASSISTABLE).
_ALLOWED_KINDS: frozenset[ItemKind] = frozenset({
    ItemKind.TOPIC, ItemKind.DECISION, ItemKind.OPEN_QUESTION,
})
_MAX_ITEM_CHARS = 140
_MAX_ACCEPTED = 6
_MAX_QUOTE_CHARS = 80
_MAX_QUOTE_FRACTION = 0.6
# Content tokens (len>=4) an item may introduce that never appeared in the transcript.
_MAX_NOVEL_CONTENT_TOKENS = 2

# Very small, deterministic stopword set (es+en) so linkage compares meaningful words.
_STOPWORDS: frozenset[str] = frozenset({
    "that", "this", "with", "from", "have", "will", "your", "about", "which", "there",
    "para", "como", "pero", "porque", "cuando", "donde", "esto", "esta", "estos",
    "estas", "sobre", "entre", "tiene", "hacer", "todo", "toda", "todos", "puede",
    "the", "and", "for", "are", "was", "were", "then", "than", "them", "they",
})

# Secret-shaped patterns. Conservative: designed to catch obvious credential shapes
# without flagging ordinary prose.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\b\s*[:=]\s*\S{5,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                      # long hex (hashes/keys)
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
)
# Code-shaped signatures. Combined with punctuation so natural language is not flagged.
_CODE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"```"),
    re.compile(r"\bdef\s+\w+\s*\("),
    re.compile(r"\bclass\s+\w+\s*[:\(]"),
    re.compile(r"\bimport\s+\w+"),
    re.compile(r"=>"),
    re.compile(r"[{};]\s*$"),
    re.compile(r"\bfunction\s+\w+\s*\("),
)
_QUOTE_SPAN = re.compile(r"[\"'«»](.-?[^\"'«»]{0,200})[\"'«»]")
_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,}|[A-Z]{2,})\b")
_WORD = re.compile(r"[A-Za-z0-9]+")


class RejectReason(str, Enum):
    NOT_AN_ITEM = "NOT_AN_ITEM"
    BAD_KIND = "BAD_KIND"
    MINTS_EXPLICIT = "MINTS_EXPLICIT"
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    RAW_CODE_BLOCK = "RAW_CODE_BLOCK"
    EXCESSIVE_QUOTATION = "EXCESSIVE_QUOTATION"
    SECRET_LIKE = "SECRET_LIKE"
    INVENTED_ENTITY = "INVENTED_ENTITY"
    NO_SOURCE_LINKAGE = "NO_SOURCE_LINKAGE"
    DUPLICATE = "DUPLICATE"
    OVER_BATCH = "OVER_BATCH"


class QualityState(str, Enum):
    EMPTY = "EMPTY"                  # nothing proposed
    PASS = "PASS"                    # items accepted, no security rejects
    DEGRADED = "DEGRADED"            # some rejected (incl. a security reject)
    REJECTED_ALL = "REJECTED_ALL"    # candidates existed, none survived


_SECURITY_REASONS = frozenset({RejectReason.SECRET_LIKE, RejectReason.RAW_CODE_BLOCK,
                               RejectReason.MINTS_EXPLICIT})


@dataclass
class QualityMetrics:
    """Content-free accounting for one quality-gate evaluation."""

    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    rejection_reasons: dict = field(default_factory=dict)
    duplicate_suppressions: int = 0
    source_coverage: float | None = None
    compression_ratio: float | None = None
    validation_latency_ms: float | None = None
    quality_state: str = QualityState.EMPTY.value

    def snapshot(self) -> dict:
        return {
            "candidates": self.candidates, "accepted": self.accepted,
            "rejected": self.rejected, "rejection_reasons": dict(self.rejection_reasons),
            "duplicate_suppressions": self.duplicate_suppressions,
            "source_coverage": self.source_coverage,
            "compression_ratio": self.compression_ratio,
            "validation_latency_ms": self.validation_latency_ms,
            "quality_state": self.quality_state,
        }


def _content_tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "")
            if len(t) >= 4 and t.lower() not in _STOPWORDS}


def _source_token_set(source_texts) -> set[str]:
    acc: set[str] = set()
    for s in (source_texts or []):
        acc |= {t.lower() for t in _WORD.findall(str(s or ""))}
    return acc


def _quoted_fraction(text: str) -> tuple[int, int]:
    """Return (max_span_len, total_quoted_chars) for the verbatim-quotation check."""
    max_span = 0
    total = 0
    for m in _QUOTE_SPAN.finditer(text or ""):
        span = len(m.group(1))
        max_span = max(max_span, span)
        total += span
    return max_span, total


@dataclass
class CompactionQualityGate:
    """The deterministic authority over model-proposed digest items."""

    max_item_chars: int = _MAX_ITEM_CHARS
    max_accepted: int = _MAX_ACCEPTED
    max_novel_tokens: int = _MAX_NOVEL_CONTENT_TOKENS

    def evaluate(self, proposed, *, base_digest=None, source_texts=None
                 ) -> tuple[list, QualityMetrics]:
        """Return (accepted_items, metrics). ``base_digest`` supplies EXPLICIT items for
        duplicate suppression; ``source_texts`` are the transcript contents used ONLY
        for the source-linkage decision (never stored)."""
        t0 = time.monotonic()
        metrics = QualityMetrics()
        if not isinstance(proposed, (list, tuple)):
            metrics.quality_state = QualityState.REJECTED_ALL.value
            metrics.validation_latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
            return [], metrics
        metrics.candidates = len(proposed)
        src = _source_token_set(source_texts)
        explicit_norm = set()
        if base_digest is not None:
            try:
                explicit_norm = {i.text.strip().lower()
                                 for i in base_digest.by_evidence(Evidence.EXPLICIT)}
            except Exception:  # noqa: BLE001
                explicit_norm = set()
        accepted: list[DigestItem] = []
        accepted_norm: set[str] = set()
        fully_linked = 0
        for item in proposed:
            reason = self._reject_reason(item, src=src, explicit_norm=explicit_norm,
                                         accepted_norm=accepted_norm,
                                         accepted_count=len(accepted))
            if reason is not None:
                metrics.rejected += 1
                key = reason.value
                metrics.rejection_reasons[key] = metrics.rejection_reasons.get(key, 0) + 1
                if reason is RejectReason.DUPLICATE:
                    metrics.duplicate_suppressions += 1
                continue
            accepted.append(item)
            accepted_norm.add(item.text.strip().lower())
            if src and not (_content_tokens(item.text) - src):
                fully_linked += 1
        metrics.accepted = len(accepted)
        metrics.source_coverage = (round(fully_linked / len(accepted), 3)
                                   if accepted else None)
        metrics.quality_state = self._state(metrics).value
        metrics.validation_latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return accepted, metrics

    # ── per-item decision (first failing reason wins) ─────────────────────────
    def _reject_reason(self, item, *, src, explicit_norm, accepted_norm,
                       accepted_count) -> RejectReason | None:
        if not isinstance(item, DigestItem) or not isinstance(item.kind, ItemKind):
            return RejectReason.NOT_AN_ITEM
        if item.kind not in _ALLOWED_KINDS:
            return RejectReason.BAD_KIND
        if item.evidence is Evidence.EXPLICIT:
            return RejectReason.MINTS_EXPLICIT
        text = item.text or ""
        if not text.strip():
            return RejectReason.EMPTY
        if len(text) > self.max_item_chars:
            return RejectReason.TOO_LONG
        if any(p.search(text) for p in _CODE_PATTERNS):
            return RejectReason.RAW_CODE_BLOCK
        max_span, total_quoted = _quoted_fraction(text)
        if max_span > _MAX_QUOTE_CHARS or (
                text and total_quoted / max(1, len(text)) > _MAX_QUOTE_FRACTION):
            return RejectReason.EXCESSIVE_QUOTATION
        if any(p.search(text) for p in _SECRET_PATTERNS):
            return RejectReason.SECRET_LIKE
        # Source linkage: invented proper nouns and too many novel content tokens.
        if src:
            for noun in _PROPER_NOUN.findall(text):
                if noun.lower() not in src:
                    return RejectReason.INVENTED_ENTITY
            novel = _content_tokens(text) - src
            if len(novel) > self.max_novel_tokens:
                return RejectReason.NO_SOURCE_LINKAGE
        norm = text.strip().lower()
        if norm in accepted_norm or norm in explicit_norm:
            return RejectReason.DUPLICATE
        if accepted_count >= self.max_accepted:
            return RejectReason.OVER_BATCH
        return None

    @staticmethod
    def _state(metrics: QualityMetrics) -> QualityState:
        if metrics.candidates == 0:
            return QualityState.EMPTY
        if metrics.accepted == 0:
            return QualityState.REJECTED_ALL
        if metrics.rejected > 0:
            return QualityState.DEGRADED
        return QualityState.PASS


# ── Process-global default gate ───────────────────────────────────────────────
_gate: CompactionQualityGate | None = None


def get_quality_gate() -> CompactionQualityGate:
    global _gate
    if _gate is None:
        _gate = CompactionQualityGate()
    return _gate


def reset_quality_gate(instance: CompactionQualityGate | None = None) -> None:
    """Tests / a fresh process."""
    global _gate
    _gate = instance
