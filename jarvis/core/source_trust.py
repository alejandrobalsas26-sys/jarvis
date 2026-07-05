"""
core/source_trust.py — V64 Milestone 10: Trusted Source Registry.

Content trust is a *separate* axis from the two trust systems JARVIS already has:

  * ``core.trust_engine``  — trust in *executing a command* (binary reputation →
    NATO challenge level). Nothing to do with knowledge provenance.
  * ``core.authority``     — the operator's *authority to act on a target*.

This module answers a third question the research/RAG fabric needs: **how much
should a retrieved source be trusted as evidence?** It classifies a fetched URL
into a ``SourceTrustTier`` from structural + operator signals, tracks per-domain
reputation, and models the claim→citation→evidence chain the Trusted Research
Runtime (M11) and Trusted RAG build on.

Design invariants (mirroring the mission's non-negotiables):
  * **Operator-authoritative.** Allowlist / blocklist come only from operator
    config (never LLM/tool input, like ``trusted_lab_mode``). A blocklisted
    domain is always ``BLOCKED``; nothing overrides that.
  * **Unknown = untrusted.** An unrecognized domain is ``UNTRUSTED`` — never
    ``COMMUNITY`` — so unknown content can never drift into authoritative use.
  * **Caps never promote.** Structural risk signals (non-HTTPS, IP host, an
    injection flag raised by the M12 firewall) can only *lower* a tier, never
    raise it. Reputation is a soft ranking score and likewise never promotes a
    tier for authority decisions.
  * **Deterministic + pure.** ``classify`` is a pure function of (url, signals,
    policy). Tests inject a fake ``now_ts``; no hidden clock, no I/O.
  * **Critical claims prefer PRIMARY / TRUSTED_SECONDARY.** Community sources may
    corroborate but are always labeled and never sufficient alone for a claim
    marked critical.

Nothing here fetches, embeds, or executes — it is a scoring/provenance layer.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit


class SourceTrustTier(str, Enum):
    """How authoritative a source may be treated as. Ordered by ``rank``."""

    PRIMARY = "primary"                     # official docs, standards, gov, CVE/NVD, original papers
    TRUSTED_SECONDARY = "trusted_secondary"  # established security research / reputable publishers
    COMMUNITY = "community"                  # forums, Q&A, wikis, personal blogs (labeled, supplementary)
    UNTRUSTED = "untrusted"                  # unknown/anon/paste/shortener/low-confidence
    BLOCKED = "blocked"                      # operator-denied or known-bad — never usable

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]

    def caps(self, ceiling: "SourceTrustTier") -> "SourceTrustTier":
        """Return the *lower* of self and *ceiling* (a demotion; never promotes)."""
        return self if self.rank <= ceiling.rank else ceiling

    def meets(self, minimum: "SourceTrustTier") -> bool:
        """True if this tier is at least *minimum* and not BLOCKED."""
        return self is not SourceTrustTier.BLOCKED and self.rank >= minimum.rank

    @property
    def is_authoritative(self) -> bool:
        """PRIMARY / TRUSTED_SECONDARY may back authoritative claims."""
        return self in (SourceTrustTier.PRIMARY, SourceTrustTier.TRUSTED_SECONDARY)

    @property
    def usable(self) -> bool:
        """Anything but BLOCKED may be used (untrusted content stays *labeled* data)."""
        return self is not SourceTrustTier.BLOCKED


# Higher rank = more trustworthy. BLOCKED sits below UNTRUSTED (unusable).
_TIER_RANK: dict[SourceTrustTier, int] = {
    SourceTrustTier.BLOCKED: 0,
    SourceTrustTier.UNTRUSTED: 1,
    SourceTrustTier.COMMUNITY: 2,
    SourceTrustTier.TRUSTED_SECONDARY: 3,
    SourceTrustTier.PRIMARY: 4,
}


# ── Curated, EXTENSIBLE default domain tables ────────────────────────────────
# These are defaults, not the final word: every entry is overridable by operator
# allowlist/blocklist, and classification always combines them with structural
# signals (HTTPS, IP host, injection flag). Suffix-matched (host == d or endswith
# ".d").  Kept intentionally conservative — when in doubt a domain is UNTRUSTED.

# Top-level domains that denote government / military / advisory bodies.
_PRIMARY_TLDS: tuple[str, ...] = (".gov", ".mil", ".int")

_PRIMARY_DOMAINS: frozenset[str] = frozenset({
    # Standards bodies
    "ietf.org", "rfc-editor.org", "iso.org", "w3.org", "whatwg.org", "iana.org",
    "unicode.org", "ieee.org", "iec.ch", "ecma-international.org", "oasis-open.org",
    # Security advisories / vuln authorities
    "mitre.org", "attack.mitre.org", "cve.org", "cve.mitre.org", "first.org",
    "cert.org", "kb.cert.org",
    # Official vendor / language / platform documentation
    "python.org", "docs.python.org", "peps.python.org",
    "docs.microsoft.com", "learn.microsoft.com", "developer.mozilla.org",
    "docs.oracle.com", "docs.aws.amazon.com", "cloud.google.com",
    "kubernetes.io", "docs.docker.com", "nodejs.org", "go.dev", "pkg.go.dev",
    "rust-lang.org", "doc.rust-lang.org", "postgresql.org", "dev.mysql.com",
    "redis.io", "nginx.org", "httpd.apache.org", "apache.org", "kernel.org",
    "openssl.org", "curl.se", "gnu.org", "debian.org", "ubuntu.com",
    "access.redhat.com", "openssf.org",
    # Original research / academic primary
    "arxiv.org", "usenix.org", "dl.acm.org", "ieeexplore.ieee.org",
    "ncbi.nlm.nih.gov", "nature.com", "science.org", "doi.org",
})

_SECONDARY_DOMAINS: frozenset[str] = frozenset({
    # Established security research / threat intelligence
    "mandiant.com", "crowdstrike.com", "paloaltonetworks.com",
    "unit42.paloaltonetworks.com", "talosintelligence.com", "securelist.com",
    "kaspersky.com", "welivesecurity.com", "sentinelone.com", "trellix.com",
    "sophos.com", "trendmicro.com", "recordedfuture.com", "rapid7.com",
    "tenable.com", "snyk.io", "portswigger.net", "sans.org", "isc.sans.edu",
    "googleprojectzero.blogspot.com", "projectzero.google", "malwarebytes.com",
    "thehackernews.com", "bleepingcomputer.com", "krebsonsecurity.com",
    # Reputable technical publishers / engineering references
    "oreilly.com", "martinfowler.com", "infoq.com",
})

_COMMUNITY_DOMAINS: frozenset[str] = frozenset({
    "stackoverflow.com", "stackexchange.com", "superuser.com", "serverfault.com",
    "askubuntu.com", "reddit.com", "news.ycombinator.com", "quora.com",
    "medium.com", "dev.to", "hashnode.dev", "substack.com", "wordpress.com",
    "blogspot.com", "github.io", "gitlab.io", "wikipedia.org", "wikimedia.org",
    "youtube.com", "discord.com", "gist.github.com", "github.com", "gitlab.com",
})

# Anonymous paste sites, URL shorteners and anon file hosts — always UNTRUSTED.
_UNTRUSTED_DOMAINS: frozenset[str] = frozenset({
    "pastebin.com", "ghostbin.com", "hastebin.com", "paste.ee", "0bin.net",
    "controlc.com", "rentry.co", "justpaste.it", "dpaste.com", "pastie.org",
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "cutt.ly", "rb.gy", "shorturl.at", "anonfiles.com", "transfer.sh",
})


def _norm_domain(domain: str) -> str:
    """Lowercase, trim, strip a single leading ``www.`` (prefix — not chars)."""
    d = (domain or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


@dataclass(frozen=True)
class SourceSignals:
    """Structural/provenance signals about a fetched source (all optional)."""

    https: bool | None = None            # None → inferred from url scheme
    publisher: str | None = None
    author: str | None = None
    published_at: float | None = None    # epoch seconds
    retrieved_at: float | None = None    # epoch seconds
    source_type: str | None = None       # advisory hint only ("vendor_docs", "forum", …)
    corroboration_count: int = 0
    injection_flagged: bool = False      # raised by the M12 firewall — caps tier at UNTRUSTED
    content_hash: str | None = None


@dataclass(frozen=True)
class SourceRecord:
    """A classified source with the reasons behind its tier. Immutable evidence."""

    url: str
    domain: str
    tier: SourceTrustTier
    https: bool
    reasons: tuple[str, ...]
    publisher: str | None = None
    author: str | None = None
    published_at: float | None = None
    retrieved_at: float | None = None
    recency_days: float | None = None
    corroboration_count: int = 0
    content_hash: str | None = None

    @property
    def is_authoritative(self) -> bool:
        return self.tier.is_authoritative

    @property
    def usable(self) -> bool:
        return self.tier.usable

    def label(self) -> str:
        """Short human/prompt label, e.g. ``[PRIMARY docs.python.org]``."""
        return f"[{self.tier.value.upper()} {self.domain}]"


@dataclass(frozen=True)
class CitationRecord:
    """A claim's link to an *actually fetched* source. ``fetched`` MUST be true
    for the citation to be valid — the research runtime never emits a citation to
    a source it did not retrieve (no invented citations)."""

    source_url: str
    source_tier: SourceTrustTier
    snippet: str = ""
    retrieved_at: float | None = None
    content_hash: str | None = None
    fetched: bool = True

    @property
    def valid(self) -> bool:
        return self.fetched and bool(self.source_url) and self.source_tier.usable


@dataclass(frozen=True)
class ClaimEvidence:
    """A claim and the citations that support it, with a sufficiency verdict."""

    claim: str
    citations: tuple[CitationRecord, ...] = ()
    critical: bool = False

    @property
    def valid_citations(self) -> tuple[CitationRecord, ...]:
        return tuple(c for c in self.citations if c.valid)

    @property
    def best_tier(self) -> SourceTrustTier:
        best = SourceTrustTier.UNTRUSTED
        for c in self.valid_citations:
            if c.source_tier.rank > best.rank:
                best = c.source_tier
        return best

    @property
    def corroboration(self) -> int:
        """Number of *distinct* sources backing the claim."""
        return len({c.source_url for c in self.valid_citations})

    @property
    def supported(self) -> bool:
        return self.corroboration >= 1

    @property
    def sufficiently_supported(self) -> bool:
        """Critical claims require an authoritative (PRIMARY/TRUSTED_SECONDARY)
        source AND at least two distinct corroborating sources. Non-critical
        claims need at least one usable citation."""
        if not self.supported:
            return False
        if self.critical:
            return self.best_tier.is_authoritative and self.corroboration >= 2
        return True


@dataclass
class SourceReputation:
    """Per-domain historical reliability. A *soft* ranking signal only — it never
    promotes a domain's tier for authority decisions (unknown stays untrusted).
    Persist via ``to_dict``/``from_dict`` at the caller's discretion (no hot-path
    I/O)."""

    _stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def observe(self, domain: str, *, corroborated: bool = False,
                contradicted: bool = False) -> None:
        d = _norm_domain(domain)
        if not d:
            return
        e = self._stats.setdefault(d, {"seen": 0, "corroborated": 0, "contradicted": 0})
        e["seen"] += 1
        if corroborated:
            e["corroborated"] += 1
        if contradicted:
            e["contradicted"] += 1

    def score(self, domain: str) -> float:
        """0.0 (no history / net-negative) .. 1.0 (consistently corroborated)."""
        e = self._stats.get(_norm_domain(domain))
        if not e or e["seen"] == 0:
            return 0.5  # neutral prior — unknown history is neither trusted nor damning
        net = e["corroborated"] - e["contradicted"]
        return max(0.0, min(1.0, 0.5 + net / (2.0 * e["seen"])))

    def to_dict(self) -> dict:
        return {"stats": self._stats}

    @classmethod
    def from_dict(cls, data: dict) -> "SourceReputation":
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        rep = cls()
        if isinstance(stats, dict):
            for k, v in stats.items():
                if isinstance(v, dict):
                    rep._stats[str(k)] = {
                        "seen": int(v.get("seen", 0)),
                        "corroborated": int(v.get("corroborated", 0)),
                        "contradicted": int(v.get("contradicted", 0)),
                    }
        return rep


def extract_domain(url: str) -> str:
    """Registrable-ish host of *url*, lowercased, ``www.`` stripped. Best-effort
    and tolerant of scheme-less inputs (``example.com/x``)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw
    host = urlsplit(raw).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _suffix_match(host: str, domains: frozenset[str]) -> str | None:
    """Return the matched domain if *host* equals or is a subdomain of one."""
    for d in domains:
        d = d.strip()
        if host == d or host.endswith("." + d):
            return d
    return None


@dataclass
class SourcePolicy:
    """Operator-tunable source-trust policy. ``classify`` is pure."""

    allowlist: dict[str, SourceTrustTier] = field(default_factory=dict)
    blocklist: frozenset[str] = frozenset()
    require_https: bool = True
    primary_domains: frozenset[str] = _PRIMARY_DOMAINS
    primary_tlds: tuple[str, ...] = _PRIMARY_TLDS
    secondary_domains: frozenset[str] = _SECONDARY_DOMAINS
    community_domains: frozenset[str] = _COMMUNITY_DOMAINS
    untrusted_domains: frozenset[str] = _UNTRUSTED_DOMAINS

    # ── base (domain-only) tier ───────────────────────────────────────────────
    def _base_tier(self, host: str) -> tuple[SourceTrustTier, str]:
        forced = self.allowlist.get(host)
        if forced is None:  # allow subdomain inheritance of an allowlisted parent
            for d, t in self.allowlist.items():
                if host == d or host.endswith("." + d):
                    forced = t
                    break
        if forced is not None:
            return forced, f"operator_allowlist:{forced.value}"
        if _host_is_ip(host):
            return SourceTrustTier.UNTRUSTED, "ip_host"
        if _suffix_match(host, self.untrusted_domains):
            return SourceTrustTier.UNTRUSTED, "paste_or_shortener"
        if host.endswith(self.primary_tlds) or _suffix_match(host, self.primary_domains):
            return SourceTrustTier.PRIMARY, "primary_domain"
        if _suffix_match(host, self.secondary_domains):
            return SourceTrustTier.TRUSTED_SECONDARY, "secondary_domain"
        if _suffix_match(host, self.community_domains):
            return SourceTrustTier.COMMUNITY, "community_domain"
        return SourceTrustTier.UNTRUSTED, "unknown_domain"

    def classify(
        self,
        url: str,
        signals: SourceSignals | None = None,
        *,
        now_ts: float | None = None,
    ) -> SourceRecord:
        """Classify *url* into a ``SourceRecord``. Pure; deterministic given
        (url, signals, policy, now_ts)."""
        sig = signals or SourceSignals()
        host = extract_domain(url)
        https = sig.https
        if https is None:
            https = (url or "").strip().lower().startswith("https://")

        reasons: list[str] = []

        # 1) Operator blocklist is absolute (fail-closed, wins over everything).
        if host and (host in self.blocklist or _suffix_match(host, self.blocklist)):
            return SourceRecord(
                url=url, domain=host, tier=SourceTrustTier.BLOCKED, https=bool(https),
                reasons=("operator_blocklist",), publisher=sig.publisher, author=sig.author,
                published_at=sig.published_at, retrieved_at=sig.retrieved_at,
                corroboration_count=sig.corroboration_count, content_hash=sig.content_hash,
            )

        tier, base_reason = self._base_tier(host)
        reasons.append(base_reason)

        # 2) Structural caps — may only *lower* the tier, never raise it.
        if _host_is_ip(host):
            capped = tier.caps(SourceTrustTier.UNTRUSTED)
            if capped is not tier:
                reasons.append("cap:ip_host")
            tier = capped
        if self.require_https and not https:
            capped = tier.caps(SourceTrustTier.COMMUNITY)
            if capped is not tier:
                reasons.append("cap:no_https")
            tier = capped
        if sig.injection_flagged:
            capped = tier.caps(SourceTrustTier.UNTRUSTED)
            if capped is not tier:
                reasons.append("cap:injection_flagged")
            tier = capped

        recency_days: float | None = None
        if sig.published_at:
            now = now_ts if now_ts is not None else time.time()
            recency_days = max(0.0, (now - sig.published_at) / 86400.0)

        return SourceRecord(
            url=url, domain=host, tier=tier, https=bool(https), reasons=tuple(reasons),
            publisher=sig.publisher, author=sig.author, published_at=sig.published_at,
            retrieved_at=sig.retrieved_at, recency_days=recency_days,
            corroboration_count=sig.corroboration_count, content_hash=sig.content_hash,
        )

    # ── operator-config factory ───────────────────────────────────────────────
    @classmethod
    def from_settings(cls, s=None) -> "SourcePolicy":
        """Build from the config singleton's operator-only trust knobs. Allowlist
        entries are ``domain`` (→ TRUSTED_SECONDARY) or ``domain=tier``."""
        if s is None:
            from core.config import settings as s  # lazy — avoids import cycle at module load
        allow: dict[str, SourceTrustTier] = {}
        for item in _split_csv(getattr(s, "source_trust_allowlist", "")):
            dom, _, tier_s = item.partition("=")
            dom = _norm_domain(dom)
            if not dom:
                continue
            tier = _parse_tier(tier_s) or SourceTrustTier.TRUSTED_SECONDARY
            allow[dom] = tier
        block = frozenset(
            _norm_domain(d)
            for d in _split_csv(getattr(s, "source_trust_blocklist", "")) if d.strip()
        )
        require_https = bool(getattr(s, "source_require_https", True))
        if getattr(s, "trusted_lab_mode", False):
            require_https = False  # isolated homelab may legitimately serve http
        return cls(allowlist=allow, blocklist=block, require_https=require_https)


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _parse_tier(raw: str) -> SourceTrustTier | None:
    raw = (raw or "").strip().lower()
    for t in SourceTrustTier:
        if raw == t.value or raw == t.name.lower():
            return t
    return None


# ── production singleton + convenience helpers ────────────────────────────────
_POLICY: SourcePolicy | None = None


def get_policy() -> SourcePolicy:
    """Process-wide policy built from operator config (cached)."""
    global _POLICY
    if _POLICY is None:
        _POLICY = SourcePolicy.from_settings()
    return _POLICY


def reset_policy() -> None:
    """Drop the cached policy (call after operator config hot-reload)."""
    global _POLICY
    _POLICY = None


def classify_source(
    url: str,
    signals: SourceSignals | None = None,
    *,
    policy: SourcePolicy | None = None,
    now_ts: float | None = None,
) -> SourceRecord:
    """Convenience wrapper over ``get_policy().classify`` (or an injected policy)."""
    return (policy or get_policy()).classify(url, signals, now_ts=now_ts)


def assess_claim(
    claim: str,
    citations: list[CitationRecord] | tuple[CitationRecord, ...],
    *,
    critical: bool = False,
) -> ClaimEvidence:
    """Bundle a claim with its citations into an evidence verdict."""
    return ClaimEvidence(claim=claim, citations=tuple(citations), critical=critical)
