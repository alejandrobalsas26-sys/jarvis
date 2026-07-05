"""
core/research_runtime.py — V64 Milestone 11: Trusted Research Runtime.

An **evidence-grounded, citation-disciplined** research pipeline that DRIVES the
pieces JARVIS already has rather than adding parallel infrastructure:

  query → decomposition → search plan → source discovery
        → trust classification (M10 source_trust)
        → fetch (guarded ToolExecutor path) → injection scan (M12 firewall)
        → claim extraction → evidence records (SharedBlackboard)
        → cross-source correlation → conflict detection
        → verifier (optional) → cited synthesis

Hard guarantees (mission M11):
  * **No invented citations.** A ``CitationRecord`` is only ever created from a
    source that was actually fetched (``fetched=True``); a citation can never
    point at a source the runtime did not retrieve.
  * **Bounded.** ≤ ``max_subqueries`` queries, ≤ ``max_sources`` fetched sources,
    ≤ ``max_chars`` per source, ≤ ``max_claims_per_source`` claims each — nothing
    fans out unbounded (Rule of Silicon on the 15W host).
  * **Injection-safe.** Every fetched page passes the M12 firewall; quarantined
    content is *excluded from evidence* (never becomes a claim) and its source is
    re-classified UNTRUSTED via the injection flag.
  * **Blocked sources are never fetched.** M10 BLOCKED tier short-circuits.

Deterministic + offline-testable: ``search_fn``/``fetch_fn`` are injected
(production wires them to the guarded ``ToolExecutor.aexecute`` for
``web_search``/``fetch_webpage`` — never raw ``requests``). The claim / correlate
/ conflict / synthesis stages are pure and need no LLM, so a research run is
reproducible without a live Ollama or network.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

from core.injection_firewall import TrustOrigin, apply_firewall
from core.source_trust import (
    CitationRecord,
    ClaimEvidence,
    SourcePolicy,
    SourceRecord,
    SourceSignals,
    SourceTrustTier,
    get_policy,
)
from core.specialist_runtime import Conflict, EvidenceItem, SharedBlackboard

# Search/fetch adapters: async callables the runtime drives.
SearchFn = Callable[[str], Awaitable[list]]
FetchFn = Callable[[str], Awaitable[str]]
InferFn = Callable[[str], Awaitable[str]]

_TIER_CONF: dict[SourceTrustTier, float] = {
    SourceTrustTier.PRIMARY: 0.9,
    SourceTrustTier.TRUSTED_SECONDARY: 0.75,
    SourceTrustTier.COMMUNITY: 0.5,
    SourceTrustTier.UNTRUSTED: 0.3,
    SourceTrustTier.BLOCKED: 0.0,
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NEG = re.compile(
    r"\b(not|no|never|isn't|aren't|wasn't|cannot|can't|without|"
    r"insecure|unsafe|incorrect|invalid|false|vulnerable|un(?:patched|supported))\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "as", "at", "by", "it", "this", "that", "with",
    "from", "has", "have", "had", "will", "can", "may", "which", "these", "their",
})
_MAX_QUESTION = 500


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass(frozen=True)
class FetchedSource:
    """A source we actually retrieved, with its M10 classification and M12 verdict."""

    record: SourceRecord
    content: str
    quarantined: bool
    attack_type: str = "none"

    @property
    def usable_as_evidence(self) -> bool:
        return not self.quarantined and self.record.tier.usable


@dataclass
class ResearchResult:
    """The structured, cited product of one research run."""

    query: str
    claims: list[ClaimEvidence] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    confidence: float = 0.0
    unresolved_questions: list[str] = field(default_factory=list)
    citations: list[CitationRecord] = field(default_factory=list)
    synthesis: str = ""
    verified: bool | None = None
    fetched_count: int = 0
    quarantined_count: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "claims": [
                {
                    "claim": ce.claim,
                    "best_tier": ce.best_tier.value,
                    "corroboration": ce.corroboration,
                    "sufficiently_supported": ce.sufficiently_supported,
                    "critical": ce.critical,
                    "citations": [c.source_url for c in ce.valid_citations],
                }
                for ce in self.claims
            ],
            "sources": [
                {"url": s.url, "domain": s.domain, "tier": s.tier.value,
                 "authoritative": s.is_authoritative}
                for s in self.sources
            ],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "confidence": round(self.confidence, 2),
            "unresolved_questions": list(self.unresolved_questions),
            "citations": [
                {"url": c.source_url, "tier": c.source_tier.value, "fetched": c.fetched}
                for c in self.citations
            ],
            "synthesis": self.synthesis,
            "verified": self.verified,
            "fetched_count": self.fetched_count,
            "quarantined_count": self.quarantined_count,
            "elapsed_s": round(self.elapsed_s, 3),
        }


@dataclass
class TrustedResearchRuntime:
    """Bounded, evidence-grounded research over injectable search/fetch adapters."""

    search_fn: SearchFn
    fetch_fn: FetchFn
    infer: InferFn | None = None
    verify_fn: Callable[[str, str], Awaitable[bool | None]] | None = None
    policy: SourcePolicy | None = None
    max_subqueries: int = 3
    max_sources: int = 6
    max_chars: int = 4000
    max_claims_per_source: int = 6
    fetch_timeout_s: float = 15.0
    corroboration_threshold: float = 0.6
    conflict_subject_threshold: float = 0.45

    def _policy(self) -> SourcePolicy:
        return self.policy or get_policy()

    # ── stage 1: query decomposition (deterministic) ──────────────────────────
    def decompose(self, question: str) -> list[str]:
        """Split a research question into bounded sub-queries. Deterministic — the
        whole question plus any conjunct/comparison clauses, deduped."""
        q = (question or "").strip()[:_MAX_QUESTION]
        if not q:
            return []
        parts = [q]
        for chunk in re.split(r"\s+(?:and|vs\.?|versus|;|,)\s+", q, flags=re.IGNORECASE):
            chunk = chunk.strip(" ?.")
            if chunk and len(chunk) > 8 and chunk.lower() != q.lower():
                parts.append(chunk)
        # dedupe preserving order, bound
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            k = p.lower()
            if k not in seen:
                seen.add(k)
                out.append(p)
        return out[: self.max_subqueries]

    # ── stage 2: source discovery ─────────────────────────────────────────────
    async def _discover(self, subqueries: list[str]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for sq in subqueries:
            try:
                results = await self.search_fn(sq)
            except Exception as e:  # noqa: BLE001 — a failed search must not abort research
                logger.debug(f"RESEARCH: search failed for '{sq[:40]}': {e}")
                continue
            for r in results or []:
                url = r if isinstance(r, str) else (r.get("url") if isinstance(r, dict) else None)
                url = (url or "").strip()
                if url and url not in seen:
                    seen.add(url)
                    candidates.append(url)
                if len(candidates) >= self.max_sources * 3:
                    break
        return candidates

    # ── stage 3+4: classify, fetch, injection-scan ────────────────────────────
    async def _collect(self, candidates: list[str], board: SharedBlackboard) -> list[FetchedSource]:
        policy = self._policy()
        fetched: list[FetchedSource] = []
        for url in candidates:
            if len(fetched) >= self.max_sources:
                break
            rec = policy.classify(url)
            if rec.tier is SourceTrustTier.BLOCKED:
                logger.info(f"RESEARCH: skipped BLOCKED source {rec.domain}")
                continue
            try:
                raw = await asyncio.wait_for(self.fetch_fn(url), timeout=self.fetch_timeout_s)
            except Exception as e:  # noqa: BLE001 — timeout/fetch error skips one source
                logger.debug(f"RESEARCH: fetch failed for {url}: {e}")
                continue
            content = _normalize_fetch(raw)
            if not content:
                continue
            fr = apply_firewall(content, TrustOrigin.WEB_UNTRUSTED, max_chars=self.max_chars)
            if fr.quarantined:
                flagged = policy.classify(url, SourceSignals(injection_flagged=True))
                fetched.append(FetchedSource(flagged, "", True, fr.assessment.attack_type.value))
                board.add_open_question(
                    f"Source {flagged.domain} excluded: injection ({fr.assessment.attack_type.value})"
                )
                logger.warning(f"RESEARCH: quarantined injected source {flagged.domain}")
                continue
            fetched.append(FetchedSource(rec, content[: self.max_chars], False))
        return fetched

    # ── stage 5: claim extraction (deterministic) ─────────────────────────────
    def _extract_claims(self, content: str) -> list[str]:
        claims: list[str] = []
        for sent in _SENTENCE_SPLIT.split(content or ""):
            s = sent.strip()
            if not (20 <= len(s) <= 300):
                continue
            if s.endswith("?") or ":" in s[-2:]:
                continue
            if not re.search(r"[a-zA-Z]", s) or " " not in s:
                continue
            claims.append(s)
            if len(claims) >= self.max_claims_per_source:
                break
        return claims

    # ── stage 6: cross-source correlation → ClaimEvidence ─────────────────────
    def _correlate(
        self, raw_claims: list[tuple[str, SourceRecord]], *, critical: bool, now: float
    ) -> list[ClaimEvidence]:
        groups: list[dict] = []  # {tokens, text, citations:{url:CitationRecord}}
        for text, rec in raw_claims:
            toks = _tokens(text)
            if not toks:
                continue
            match = None
            for g in groups:
                if _jaccard(toks, g["tokens"]) >= self.corroboration_threshold:
                    match = g
                    break
            cite = CitationRecord(
                source_url=rec.url, source_tier=rec.tier, snippet=text[:200],
                retrieved_at=now, content_hash=_hash(text), fetched=True,
            )
            if match is None:
                groups.append({"tokens": toks, "text": text, "citations": {rec.url: cite}})
            else:
                match["citations"].setdefault(rec.url, cite)
                match["tokens"] = match["tokens"] | toks
        return [
            ClaimEvidence(claim=g["text"], citations=tuple(g["citations"].values()), critical=critical)
            for g in groups
        ]

    # ── stage 7: conflict detection (deterministic) ───────────────────────────
    def _detect_conflicts(self, claims: list[ClaimEvidence]) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                a, b = claims[i], claims[j]
                ta, tb = _tokens(a.claim), _tokens(b.claim)
                if _jaccard(ta, tb) < self.conflict_subject_threshold:
                    continue
                neg_a, neg_b = bool(_NEG.search(a.claim)), bool(_NEG.search(b.claim))
                if neg_a == neg_b:
                    continue  # both assert or both negate ⇒ agreement, not conflict
                dom_a = a.valid_citations[0].source_url if a.valid_citations else "a"
                dom_b = b.valid_citations[0].source_url if b.valid_citations else "b"
                conflicts.append(Conflict(
                    topic=" ".join(sorted(ta & tb))[:80] or "claim",
                    verdict_a=a.claim[:120], agent_a=dom_a,
                    verdict_b=b.claim[:120], agent_b=dom_b,
                ))
        return conflicts

    # ── confidence + synthesis ────────────────────────────────────────────────
    def _confidence(self, claims: list[ClaimEvidence], conflicts: list[Conflict]) -> float:
        if not claims:
            return 0.0
        per = []
        for ce in claims:
            base = _TIER_CONF.get(ce.best_tier, 0.3)
            per.append(base * min(1.0, 0.6 + 0.2 * ce.corroboration))
        conf = sum(per) / len(per)
        if conflicts:
            conf = min(conf, 0.6)  # unresolved contradictions cap confidence
        return round(max(0.0, min(1.0, conf)), 3)

    def _synthesize(self, question: str, claims: list[ClaimEvidence],
                    citations: list[CitationRecord], conflicts: list[Conflict]) -> str:
        if not claims:
            return f"No trusted evidence was found for: {question}"
        url_index = {c.source_url: n for n, c in enumerate(_unique_citations(citations), 1)}
        lines = [f"Research summary for: {question}"]
        for ce in claims[:8]:
            marks = "".join(
                f"[{url_index[c.source_url]}]"
                for c in ce.valid_citations if c.source_url in url_index
            )
            lines.append(f"- {ce.claim} {marks}".rstrip())
        if conflicts:
            lines.append("Conflicts detected (unresolved): " + "; ".join(
                f"{c.agent_a} vs {c.agent_b}" for c in conflicts[:5]))
        lines.append("Sources:")
        for c in _unique_citations(citations):
            lines.append(f"  [{url_index[c.source_url]}] ({c.source_tier.value}) {c.source_url}")
        return "\n".join(lines)

    # ── orchestration ─────────────────────────────────────────────────────────
    async def research(
        self, question: str, *, critical: bool = False, board: SharedBlackboard | None = None,
    ) -> ResearchResult:
        """Run the full trusted-research pipeline for *question*."""
        started = time.time()
        board = board or SharedBlackboard(objective=question)
        subqueries = self.decompose(question)
        candidates = await self._discover(subqueries)
        fetched = await self._collect(candidates, board)

        raw_claims: list[tuple[str, SourceRecord]] = []
        sources: list[SourceRecord] = [fs.record for fs in fetched]
        for fs in fetched:
            if not fs.usable_as_evidence:
                continue
            for sent in self._extract_claims(fs.content):
                raw_claims.append((sent, fs.record))
                board.add_evidence(EvidenceItem(
                    content=sent, source=fs.record.domain,
                    confidence=_TIER_CONF.get(fs.record.tier, 0.3),
                    trusted=fs.record.is_authoritative, agent="research",
                ))

        now = time.time()
        claims = self._correlate(raw_claims, critical=critical, now=now)
        citations = [c for ce in claims for c in ce.valid_citations]
        conflicts = self._detect_conflicts(claims)
        for cf in conflicts:
            if cf not in board.conflicts:
                board.conflicts.append(cf)

        covered_tokens: frozenset[str] = frozenset().union(*[_tokens(ce.claim) for ce in claims]) if claims else frozenset()
        unresolved = [
            sq for sq in subqueries if _jaccard(_tokens(sq), covered_tokens) < 0.15
        ] + list(board.open_questions)

        confidence = self._confidence(claims, conflicts)
        synthesis = self._synthesize(question, claims, citations, conflicts)

        verified: bool | None = None
        if self.verify_fn is not None and claims:
            try:
                verified = await self.verify_fn(question, synthesis)
            except Exception as e:  # noqa: BLE001 — verifier failure ⇒ leave unverified
                logger.debug(f"RESEARCH: verify failed: {e}")
                verified = None

        return ResearchResult(
            query=question, claims=claims, evidence=list(board.evidence), sources=sources,
            conflicts=conflicts, confidence=confidence,
            unresolved_questions=_dedup(unresolved), citations=citations,
            synthesis=synthesis, verified=verified,
            fetched_count=sum(1 for fs in fetched if fs.usable_as_evidence),
            quarantined_count=sum(1 for fs in fetched if fs.quarantined),
            elapsed_s=time.time() - started,
        )

    # ── production factory: drive the guarded ToolExecutor ────────────────────
    @classmethod
    def from_executor(
        cls,
        tool_executor,
        *,
        infer: InferFn | None = None,
        verify_fn=None,
        policy: SourcePolicy | None = None,
        search_results: int = 5,
        **kwargs,
    ) -> "TrustedResearchRuntime":
        """Build a runtime whose fetches route through ``ToolExecutor.aexecute``
        for ``web_search`` / ``fetch_webpage`` — the guarded path (risk-class /
        HITL / SSRF / audit). Never issues raw ``requests``."""

        async def search_fn(query: str) -> list:
            res = await tool_executor.aexecute(
                "web_search", {"query": query, "max_results": search_results},
                "trusted research: source discovery",
            )
            if not isinstance(res, dict):
                return []
            return res.get("results", []) or []

        async def fetch_fn(url: str) -> str:
            res = await tool_executor.aexecute(
                "fetch_webpage", {"url": url, "max_chars": kwargs.get("max_chars", 4000)},
                "trusted research: source retrieval",
            )
            if not isinstance(res, dict):
                return ""
            return res.get("content", "") or ""

        return cls(
            search_fn=search_fn, fetch_fn=fetch_fn, infer=infer, verify_fn=verify_fn,
            policy=policy, **kwargs,
        )


# ── production singleton (attached at boot from main.py) ──────────────────────
_RUNTIME: TrustedResearchRuntime | None = None


def attach_research_runtime(
    tool_executor, *, infer: InferFn | None = None, verify_fn=None, **kwargs
) -> TrustedResearchRuntime:
    """Attach the process-wide trusted research runtime, driving the guarded
    ``ToolExecutor`` for web_search/fetch_webpage. Called once at boot."""
    global _RUNTIME
    _RUNTIME = TrustedResearchRuntime.from_executor(
        tool_executor, infer=infer, verify_fn=verify_fn, **kwargs
    )
    return _RUNTIME


def get_research_runtime() -> TrustedResearchRuntime | None:
    """The attached runtime, or None if not attached (e.g. under unit tests)."""
    return _RUNTIME


def _normalize_fetch(raw) -> str:
    """Accept either a plain string or a {'content': ...} dict from a fetch adapter."""
    if isinstance(raw, dict):
        return str(raw.get("content", "") or "")
    return str(raw or "")


def _unique_citations(citations: list[CitationRecord]) -> list[CitationRecord]:
    seen: set[str] = set()
    out: list[CitationRecord] = []
    for c in citations:
        if c.source_url not in seen:
            seen.add(c.source_url)
            out.append(c)
    return out


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        k = (i or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out
