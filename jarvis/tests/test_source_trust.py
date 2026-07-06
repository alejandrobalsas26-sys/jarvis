"""
tests/test_source_trust.py — V64 M10 Trusted Source Registry.

Covers the mission-required cases: primary-source ranking, community labeling,
blocklist, operator override, unknown-source fallback — plus structural caps
(non-HTTPS, IP host, injection flag), claim-sufficiency for critical claims,
citation validity, tier algebra, and reputation.
"""
from __future__ import annotations

from core.source_trust import (
    CitationRecord,
    SourcePolicy,
    SourceReputation,
    SourceSignals,
    SourceTrustTier,
    assess_claim,
    classify_source,
    extract_domain,
    reset_policy,
)

_HTTPS = SourceSignals(https=True)


# ── tier algebra ──────────────────────────────────────────────────────────────
def test_tier_rank_ordering():
    assert SourceTrustTier.PRIMARY.rank > SourceTrustTier.TRUSTED_SECONDARY.rank
    assert SourceTrustTier.TRUSTED_SECONDARY.rank > SourceTrustTier.COMMUNITY.rank
    assert SourceTrustTier.COMMUNITY.rank > SourceTrustTier.UNTRUSTED.rank
    assert SourceTrustTier.UNTRUSTED.rank > SourceTrustTier.BLOCKED.rank


def test_tier_caps_never_promotes():
    assert SourceTrustTier.PRIMARY.caps(SourceTrustTier.COMMUNITY) is SourceTrustTier.COMMUNITY
    # caps only lowers — a lower tier is never raised toward the ceiling
    assert SourceTrustTier.UNTRUSTED.caps(SourceTrustTier.PRIMARY) is SourceTrustTier.UNTRUSTED


def test_tier_meets_and_authority():
    assert SourceTrustTier.PRIMARY.meets(SourceTrustTier.TRUSTED_SECONDARY)
    assert not SourceTrustTier.COMMUNITY.meets(SourceTrustTier.TRUSTED_SECONDARY)
    assert not SourceTrustTier.BLOCKED.meets(SourceTrustTier.UNTRUSTED)
    assert SourceTrustTier.PRIMARY.is_authoritative
    assert SourceTrustTier.TRUSTED_SECONDARY.is_authoritative
    assert not SourceTrustTier.COMMUNITY.is_authoritative
    assert not SourceTrustTier.BLOCKED.usable
    assert SourceTrustTier.UNTRUSTED.usable


# ── domain extraction ─────────────────────────────────────────────────────────
def test_extract_domain_strips_scheme_www_port():
    assert extract_domain("https://www.Python.org/3/library") == "python.org"
    assert extract_domain("docs.python.org/x") == "docs.python.org"
    assert extract_domain("http://example.com:8080") == "example.com"
    assert extract_domain("") == ""


# ── primary-source ranking ────────────────────────────────────────────────────
def test_primary_gov_tld():
    r = classify_source("https://www.cisa.gov/advisory", _HTTPS)
    assert r.tier is SourceTrustTier.PRIMARY
    assert r.is_authoritative


def test_primary_vendor_docs_and_standards():
    for url in (
        "https://docs.python.org/3/",
        "https://learn.microsoft.com/windows",
        "https://www.ietf.org/rfc/rfc793",
        "https://nvd.nist.gov/vuln/CVE-2024-1234",   # .gov TLD
        "https://arxiv.org/abs/2401.00001",
    ):
        assert classify_source(url, _HTTPS).tier is SourceTrustTier.PRIMARY, url


def test_secondary_security_research():
    r = classify_source("https://unit42.paloaltonetworks.com/report", _HTTPS)
    assert r.tier is SourceTrustTier.TRUSTED_SECONDARY
    assert r.is_authoritative


# ── community labeling ────────────────────────────────────────────────────────
def test_community_sources_labeled_not_authoritative():
    for url in (
        "https://stackoverflow.com/questions/1",
        "https://www.reddit.com/r/netsec",
        "https://en.wikipedia.org/wiki/SQL_injection",
        "https://medium.com/@someone/post",
    ):
        r = classify_source(url, _HTTPS)
        assert r.tier is SourceTrustTier.COMMUNITY, url
        assert not r.is_authoritative
        assert r.usable
        assert r.label().startswith("[COMMUNITY ")


# ── unknown-source fallback ───────────────────────────────────────────────────
def test_unknown_domain_is_untrusted_not_community():
    r = classify_source("https://random-seo-blog-xyz.example", _HTTPS)
    assert r.tier is SourceTrustTier.UNTRUSTED
    assert "unknown_domain" in r.reasons


def test_paste_site_and_shortener_untrusted():
    assert classify_source("https://pastebin.com/raw/abc", _HTTPS).tier is SourceTrustTier.UNTRUSTED
    assert classify_source("https://bit.ly/xyz", _HTTPS).tier is SourceTrustTier.UNTRUSTED


# ── blocklist / operator override ─────────────────────────────────────────────
def test_operator_blocklist_forces_blocked_over_everything():
    pol = SourcePolicy(blocklist=frozenset({"docs.python.org"}))
    r = pol.classify("https://docs.python.org/3/", _HTTPS)
    assert r.tier is SourceTrustTier.BLOCKED
    assert not r.usable
    assert r.reasons == ("operator_blocklist",)


def test_operator_allowlist_promotes_unknown_domain():
    pol = SourcePolicy(allowlist={"internal.corp": SourceTrustTier.PRIMARY})
    r = pol.classify("https://wiki.internal.corp/kb", _HTTPS)  # subdomain inherits
    assert r.tier is SourceTrustTier.PRIMARY
    assert any("operator_allowlist" in reason for reason in r.reasons)


def test_blocklist_beats_allowlist():
    pol = SourcePolicy(
        allowlist={"evil.example": SourceTrustTier.PRIMARY},
        blocklist=frozenset({"evil.example"}),
    )
    assert pol.classify("https://evil.example", _HTTPS).tier is SourceTrustTier.BLOCKED


# ── structural caps (never promote) ───────────────────────────────────────────
def test_non_https_caps_primary_to_community():
    pol = SourcePolicy(require_https=True)
    r = pol.classify("http://docs.python.org/3/", SourceSignals(https=False))
    assert r.tier is SourceTrustTier.COMMUNITY
    assert "cap:no_https" in r.reasons


def test_require_https_disabled_keeps_primary():
    pol = SourcePolicy(require_https=False)
    r = pol.classify("http://docs.python.org/3/", SourceSignals(https=False))
    assert r.tier is SourceTrustTier.PRIMARY


def test_ip_host_capped_untrusted():
    r = classify_source("https://93.184.216.34/x", _HTTPS)
    assert r.tier is SourceTrustTier.UNTRUSTED
    assert "ip_host" in r.reasons[0] or "cap:ip_host" in r.reasons


def test_injection_flag_caps_authoritative_source():
    # Even a PRIMARY domain drops to UNTRUSTED if the firewall flagged its content.
    r = classify_source("https://docs.python.org/3/", SourceSignals(https=True, injection_flagged=True))
    assert r.tier is SourceTrustTier.UNTRUSTED
    assert "cap:injection_flagged" in r.reasons


def test_recency_days_computed_from_now_ts():
    published = 1_000_000.0
    now = published + 5 * 86400
    r = classify_source("https://docs.python.org/x", SourceSignals(https=True, published_at=published), now_ts=now)
    assert r.recency_days == 5.0


# ── claim / citation evidence ─────────────────────────────────────────────────
def test_citation_validity_requires_fetched():
    good = CitationRecord("https://docs.python.org", SourceTrustTier.PRIMARY, fetched=True)
    bad = CitationRecord("https://docs.python.org", SourceTrustTier.PRIMARY, fetched=False)
    blocked = CitationRecord("https://evil", SourceTrustTier.BLOCKED, fetched=True)
    assert good.valid
    assert not bad.valid      # invented / not actually fetched
    assert not blocked.valid  # blocked source is unusable


def test_critical_claim_needs_authoritative_and_corroboration():
    prim = CitationRecord("https://docs.python.org", SourceTrustTier.PRIMARY)
    sec = CitationRecord("https://sans.org", SourceTrustTier.TRUSTED_SECONDARY)
    comm = CitationRecord("https://reddit.com/x", SourceTrustTier.COMMUNITY)

    # single primary source → supported but NOT sufficient for a critical claim
    one = assess_claim("X is true", [prim], critical=True)
    assert one.supported
    assert not one.sufficiently_supported

    # two authoritative distinct sources → sufficient
    two = assess_claim("X is true", [prim, sec], critical=True)
    assert two.sufficiently_supported
    assert two.best_tier is SourceTrustTier.PRIMARY
    assert two.corroboration == 2

    # two community sources → not sufficient for a critical claim
    comm2 = assess_claim("X is true", [comm, CitationRecord("https://news.ycombinator.com", SourceTrustTier.COMMUNITY)], critical=True)
    assert comm2.supported
    assert not comm2.sufficiently_supported

    # non-critical claim → a single community citation suffices
    assert assess_claim("Y is nice", [comm], critical=False).sufficiently_supported


def test_corroboration_counts_distinct_sources_only():
    dup = CitationRecord("https://docs.python.org", SourceTrustTier.PRIMARY)
    ev = assess_claim("Z", [dup, dup], critical=False)
    assert ev.corroboration == 1


# ── reputation (soft ranking only) ────────────────────────────────────────────
def test_reputation_neutral_prior_and_movement():
    rep = SourceReputation()
    assert rep.score("unknown.example") == 0.5
    for _ in range(5):
        rep.observe("good.example", corroborated=True)
    assert rep.score("good.example") > 0.5
    for _ in range(5):
        rep.observe("bad.example", contradicted=True)
    assert rep.score("bad.example") < 0.5


def test_reputation_roundtrip_serialization():
    rep = SourceReputation()
    rep.observe("a.example", corroborated=True)
    restored = SourceReputation.from_dict(rep.to_dict())
    assert restored.score("a.example") == rep.score("a.example")


# ── operator-config factory ───────────────────────────────────────────────────
def test_policy_from_settings_parses_csv(monkeypatch):
    class _S:
        source_trust_allowlist = "internal.corp=primary, partner.example"
        source_trust_blocklist = "spam.example, ads.example"
        source_require_https = True
        trusted_lab_mode = False

    pol = SourcePolicy.from_settings(_S())
    assert pol.allowlist["internal.corp"] is SourceTrustTier.PRIMARY
    assert pol.allowlist["partner.example"] is SourceTrustTier.TRUSTED_SECONDARY
    assert "spam.example" in pol.blocklist
    assert pol.classify("https://internal.corp/x", _HTTPS).tier is SourceTrustTier.PRIMARY
    assert pol.classify("https://spam.example/x", _HTTPS).tier is SourceTrustTier.BLOCKED


def test_trusted_lab_relaxes_https_requirement():
    class _S:
        source_trust_allowlist = ""
        source_trust_blocklist = ""
        source_require_https = True
        trusted_lab_mode = True

    pol = SourcePolicy.from_settings(_S())
    assert pol.require_https is False


def test_reset_policy_clears_singleton():
    reset_policy()  # smoke: must not raise; next get_policy rebuilds from settings
