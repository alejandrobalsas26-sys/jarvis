"""tests/test_causal_timeline_v68.py — V68 M42 evidence-conscious causal timeline.

Proves the epistemic ladder is enforced in code, not merely documented:
  * ordering — heterogeneous facts sort into one time-ordered timeline; unknown-time
    entries sort last;
  * OBSERVED / CORRELATED / INFERRED entries carry their honest self-labels;
  * a correlation finding links to its matched events as CORRELATED and that link is
    NOT causal (causal_verdict == NOT_CAUSAL) - correlation != proof;
  * a change after a correlation on the same entity becomes a HYPOTHESIZED link, read as
    UNPROVEN - hypothesis != fact - and never silently promoted;
  * ONLY an independent verification promotes a hypothesis to VERIFIED (PROVEN) or REFUTED
    (DISPROVEN);
  * co-occurrence yields at most TEMPORALLY_ASSOCIATED, never causal;
  * bounded + ASCII narrative.

Deterministic: explicit epochs, no wall-clock, no network.
"""
from __future__ import annotations

from core.causal_timeline import (
    Band,
    Epistemic,
    TimelineEntry,
    build_timeline,
    causal_verdict,
    entry_from_drift,
    entry_from_finding,
    entry_from_verification,
    timeline_from_facts,
)

T0 = 1_752_000_000.0


def _entry(eid, at, kind, ep, entity="", refs=()):
    return TimelineEntry(entry_id=eid, at=at, at_iso=str(at), kind=kind,
                         title=f"{kind}:{eid}", epistemic=ep, entity=entity,
                         evidence_refs=tuple(refs))


# ── ordering ──────────────────────────────────────────────────────────────────
class TestOrdering:
    def test_sorted_by_time(self):
        tl = build_timeline([
            _entry("b", T0 + 10, "event", Epistemic.OBSERVED),
            _entry("a", T0, "event", Epistemic.OBSERVED),
        ])
        assert [e.entry_id for e in tl.entries] == ["a", "b"]

    def test_unknown_time_sorts_last(self):
        tl = build_timeline([
            _entry("x", None, "event", Epistemic.OBSERVED),
            _entry("a", T0, "event", Epistemic.OBSERVED),
        ])
        assert [e.entry_id for e in tl.entries] == ["a", "x"]


# ── correlation is a signal, not proof ───────────────────────────────────────────
class TestCorrelationNotProof:
    def test_finding_links_events_as_correlated(self):
        tl = build_timeline([
            _entry("e1", T0, "event", Epistemic.OBSERVED, entity="h"),
            _entry("f1", T0 + 1, "finding", Epistemic.CORRELATED, entity="h", refs=["e1"]),
        ])
        link = next(lk for lk in tl.links if lk.dst_id == "e1")
        assert link.epistemic is Epistemic.CORRELATED
        assert causal_verdict(link) == "NOT_CAUSAL"

    def test_temporal_association_is_not_causal(self):
        tl = build_timeline([
            _entry("e1", T0, "event", Epistemic.OBSERVED, entity="h"),
            _entry("e2", T0 + 5, "event", Epistemic.OBSERVED, entity="h"),
        ])
        link = next(lk for lk in tl.links
                    if lk.epistemic is Epistemic.TEMPORALLY_ASSOCIATED)
        assert causal_verdict(link) == "NOT_CAUSAL"


# ── hypothesis lifecycle (the only promotion path) ───────────────────────────────
class TestHypothesisLifecycle:
    def _fc(self):
        # a correlation on host h, then a drift (change) on h shortly after
        return [
            _entry("f1", T0, "finding", Epistemic.CORRELATED, entity="h"),
            _entry("c1", T0 + 30, "change", Epistemic.INFERRED, entity="h"),
        ]

    def test_change_after_correlation_is_hypothesized_unproven(self):
        tl = build_timeline(self._fc())
        h = next(lk for lk in tl.links if lk.epistemic is Epistemic.HYPOTHESIZED)
        assert h.src_id == "f1" and h.dst_id == "c1"
        assert causal_verdict(h) == "UNPROVEN"

    def test_verification_promotes_to_verified(self):
        entries = self._fc() + [
            entry_from_verification("h", True, at=T0 + 60, basis="probe confirmed"),
        ]
        tl = build_timeline(entries)
        h = next(lk for lk in tl.links if lk.dst_id == "c1")
        assert h.epistemic is Epistemic.VERIFIED
        assert causal_verdict(h) == "PROVEN"
        assert h.band is Band.HIGH

    def test_verification_can_refute(self):
        entries = self._fc() + [
            entry_from_verification("h", False, at=T0 + 60, basis="probe found nothing"),
        ]
        tl = build_timeline(entries)
        h = next(lk for lk in tl.links if lk.dst_id == "c1")
        assert h.epistemic is Epistemic.REFUTED
        assert causal_verdict(h) == "DISPROVEN"

    def test_no_hypothesis_without_temporal_or_entity_match(self):
        # change on a DIFFERENT entity -> no hypothesized causal link
        tl = build_timeline([
            _entry("f1", T0, "finding", Epistemic.CORRELATED, entity="h"),
            _entry("c1", T0 + 30, "change", Epistemic.INFERRED, entity="other"),
        ])
        assert not any(lk.epistemic is Epistemic.HYPOTHESIZED for lk in tl.links)


# ── adapters carry honest self-labels ─────────────────────────────────────────────
class TestAdapters:
    def test_finding_adapter_is_correlated(self):
        e = entry_from_finding({"finding_id": "f", "rule": "r", "window_end": T0,
                                "group_entity": "h", "matched_event_ids": ["e1"],
                                "severity": "high"})
        assert e.epistemic is Epistemic.CORRELATED and e.evidence_refs == ("e1",)

    def test_drift_adapter_is_inferred_change(self):
        e = entry_from_drift({"finding_id": "d", "asset": "h", "drift_type": "service_missing",
                              "timestamp": T0, "severity": "medium", "evidence_refs": []})
        assert e.epistemic is Epistemic.INFERRED and e.kind == "change"

    def test_from_facts_composes_and_resolves(self):
        tl = timeline_from_facts(
            findings=[{"finding_id": "f", "rule": "r", "window_end": T0,
                       "group_entity": "h", "matched_event_ids": []}],
            drifts=[{"finding_id": "d", "asset": "h", "drift_type": "svc",
                     "timestamp": T0 + 20, "evidence_refs": []}],
            verifications=[("h", True, T0 + 40, "confirmed")])
        h = next(lk for lk in tl.links if lk.dst_id == "d")
        assert causal_verdict(h) == "PROVEN"


# ── boundedness + ASCII ───────────────────────────────────────────────────────────
class TestBoundedAscii:
    def test_narrative_ascii_and_dict_bounded(self):
        entries = [_entry(f"e{i}", T0 + i, "event", Epistemic.OBSERVED, entity="h")
                   for i in range(300)]
        tl = build_timeline(entries)
        d = tl.to_dict()
        assert len(d["entries"]) <= 200 and len(d["links"]) <= 400
        assert tl.narrative().isascii()
        assert d["panel"] == "causal_timeline"
