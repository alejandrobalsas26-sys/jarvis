"""core/causal_timeline.py — V68 M42: evidence-conscious causal & change intelligence.

Assembles heterogeneous operational facts — canonical events, correlation findings,
digital-twin drift, and verification outcomes — into ONE ordered timeline, and annotates
every *link* between facts with its epistemic strength. The whole point is to make the
system's certainty legible and to forbid silent promotion of a weak link into a strong one.

The epistemic ladder (weak -> resolved), each a hard, separate claim:

  OBSERVED               a fact JARVIS directly saw (an event happened)
  TEMPORALLY_ASSOCIATED  two facts merely near each other in time (co-occurrence)
  CORRELATED             a correlation rule linked their events (a SIGNAL, not proof)
  INFERRED               a deterministic rule derived a change (expected vs observed)
  HYPOTHESIZED           a candidate causal explanation, explicitly UNPROVEN
  VERIFIED               a hypothesis confirmed by an independent verification step
  REFUTED                a hypothesis disproven by evidence

Invariants this module enforces in code, not prose:
  * Correlation != proof. Co-occurrence != cause. A CORRELATED / TEMPORALLY_ASSOCIATED
    link is NEVER a causal claim (:func:`causal_verdict` returns NOT_CAUSAL).
  * Hypothesis != fact. A link reaches VERIFIED only when an actual verification entry
    resolves it — there is no code path that promotes HYPOTHESIZED to VERIFIED otherwise.
  * No observation != healthy; unknown stays unknown. Absence produces no link, never a
    fabricated one.

Deterministic: epochs are explicit / parsed from the facts; no wall-clock in the built
timeline. Bounded (Rule of Silicon). ASCII narrative (Windows console safe).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

_MAX_ENTRIES = 200
_MAX_LINKS = 400
_ASSOC_WINDOW_S = 120.0        # default co-occurrence window for TEMPORALLY_ASSOCIATED
_HYPOTHESIS_WINDOW_S = 300.0   # a drift after a finding, within this, is a candidate cause


class Epistemic(str, Enum):
    OBSERVED = "observed"
    TEMPORALLY_ASSOCIATED = "temporally_associated"
    CORRELATED = "correlated"
    INFERRED = "inferred"
    HYPOTHESIZED = "hypothesized"
    VERIFIED = "verified"
    REFUTED = "refuted"


# Rank only orders *display* strength; it is NOT a promotion path. Nothing in this module
# raises a link's epistemic label except an explicit verification resolving a hypothesis.
_RANK = {Epistemic.TEMPORALLY_ASSOCIATED: 0, Epistemic.CORRELATED: 1,
         Epistemic.INFERRED: 2, Epistemic.HYPOTHESIZED: 3, Epistemic.OBSERVED: 4,
         Epistemic.VERIFIED: 5, Epistemic.REFUTED: 5}

# The only labels that constitute a causal claim about the world.
_CAUSAL = {Epistemic.HYPOTHESIZED, Epistemic.VERIFIED, Epistemic.REFUTED}


class Band(str, Enum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


def _band(confidence: float | None) -> Band:
    """Ordinal band — no fake precision. None/unknown -> LOW (never silently HIGH)."""
    if confidence is None:
        return Band.LOW
    if confidence >= 0.75:
        return Band.HIGH
    if confidence >= 0.4:
        return Band.MED
    return Band.LOW


def _epoch(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, AttributeError):
        return None


def _get(obj, key, default=None):
    """Read from a dict or an object attribute — accept both shapes."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _hid(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  Timeline entries & links
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TimelineEntry:
    entry_id: str
    at: float | None                 # epoch (None -> unordered/unknown time, sorts last)
    kind: str                        # event | finding | change | verification
    title: str
    epistemic: Epistemic
    entity: str = ""
    severity: str = ""
    evidence_refs: tuple[str, ...] = ()
    at_iso: str = ""

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "at": self.at, "at_iso": self.at_iso,
                "kind": self.kind, "title": self.title[:160],
                "epistemic": self.epistemic.value, "entity": self.entity,
                "severity": self.severity, "evidence_refs": list(self.evidence_refs)[:8]}


@dataclass
class CausalLink:
    src_id: str
    dst_id: str
    epistemic: Epistemic
    basis: str                       # ascii explanation of WHY this link, at this strength
    band: Band = Band.LOW

    def to_dict(self) -> dict:
        return {"src": self.src_id, "dst": self.dst_id,
                "epistemic": self.epistemic.value, "basis": self.basis[:160],
                "band": self.band.value, "causal": self.epistemic in _CAUSAL,
                "verdict": causal_verdict(self)}


def causal_verdict(link: CausalLink) -> str:
    """The ONLY sanctioned reading of a link's causal meaning. Encodes the invariants:
    correlation/co-occurrence/inference are NOT causal; a hypothesis is UNPROVEN until an
    independent verification makes it PROVEN or DISPROVEN."""
    e = link.epistemic
    if e is Epistemic.VERIFIED:
        return "PROVEN"
    if e is Epistemic.REFUTED:
        return "DISPROVEN"
    if e is Epistemic.HYPOTHESIZED:
        return "UNPROVEN"
    return "NOT_CAUSAL"


# ── entry constructors (accept the real objects OR their to_dict projections) ────
def entry_from_event(ev) -> TimelineEntry:
    at_iso = _get(ev, "observed_at") or _get(ev, "timestamp") or ""
    eid = _get(ev, "event_id") or _hid("event", at_iso, _get(ev, "category"))
    entity = _get(ev, "host") or _get(ev, "service") or _get(ev, "src_ip") or ""
    cat = _get(ev, "category")
    cat = getattr(cat, "value", cat) or "event"
    sev = _get(ev, "severity")
    sev = getattr(sev, "value", sev) or ""
    return TimelineEntry(
        entry_id=str(eid), at=_epoch(at_iso), at_iso=str(at_iso), kind="event",
        title=f"{cat}: {_get(ev, 'signature') or _get(ev, 'rule_id') or cat}",
        epistemic=Epistemic.OBSERVED, entity=str(entity), severity=str(sev),
        evidence_refs=tuple(str(x) for x in (_get(ev, "raw_reference") or [],) if x))


def entry_from_finding(f) -> TimelineEntry:
    fid = _get(f, "finding_id") or _hid("finding", _get(f, "rule"))
    at_iso = _get(f, "window_end") or _get(f, "created_at") or ""
    matched = tuple(str(x) for x in (_get(f, "matched_event_ids") or ()))
    # A correlation finding is a CORRELATED-level assertion over its events — a signal,
    # explicitly not proof. Its own existence is observed, but its claim is CORRELATED.
    return TimelineEntry(
        entry_id=str(fid), at=_epoch(at_iso), at_iso=str(at_iso), kind="finding",
        title=f"correlation: {_get(f, 'rule')}", epistemic=Epistemic.CORRELATED,
        entity=str(_get(f, "group_entity") or ""), severity=str(_get(f, "severity") or ""),
        evidence_refs=matched)


def entry_from_drift(d) -> TimelineEntry:
    did = _get(d, "finding_id") or _hid("drift", _get(d, "asset"), _get(d, "drift_type"))
    at_iso = _get(d, "timestamp") or ""
    dt = _get(d, "drift_type")
    dt = getattr(dt, "value", dt) or "drift"
    sev = _get(d, "severity")
    sev = getattr(sev, "value", sev) or ""
    # Drift is an INFERRED change: a deterministic rule over expected vs observed state.
    return TimelineEntry(
        entry_id=str(did), at=_epoch(at_iso), at_iso=str(at_iso), kind="change",
        title=f"drift: {dt} on {_get(d, 'asset')}", epistemic=Epistemic.INFERRED,
        entity=str(_get(d, "asset") or ""), severity=str(sev),
        evidence_refs=tuple(str(x) for x in (_get(d, "evidence_refs") or ())))


def entry_from_verification(entity: str, verified: bool, *, at, basis: str = "",
                            target_id: str = "") -> TimelineEntry:
    at_iso = str(at or "")
    vid = target_id or _hid("verify", entity, at_iso)
    return TimelineEntry(
        entry_id=str(vid), at=_epoch(at), at_iso=at_iso, kind="verification",
        title=f"verification: {entity} {'confirmed' if verified else 'refuted'}",
        epistemic=Epistemic.VERIFIED if verified else Epistemic.REFUTED,
        entity=str(entity), severity="", evidence_refs=(basis[:80],) if basis else ())


# ══════════════════════════════════════════════════════════════════════════════
#  The timeline
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class CausalTimeline:
    entries: list[TimelineEntry] = field(default_factory=list)
    links: list[CausalLink] = field(default_factory=list)

    def narrative(self) -> str:
        lines = ["OPERATIONAL TIMELINE (epistemically labeled)"]
        for e in self.entries:
            when = e.at_iso or "unknown-time"
            lines.append(f"[{e.epistemic.value.upper()}] {when}  {e.title}")
        if self.links:
            lines.append("")
            lines.append("LINKS:")
            for lk in self.links:
                lines.append(f"  {lk.src_id} -> {lk.dst_id}  "
                             f"[{lk.epistemic.value.upper()}/{causal_verdict(lk)}] {lk.basis}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        causal = [lk for lk in self.links if lk.epistemic in _CAUSAL]
        return {
            "panel": "causal_timeline",
            "entries": [e.to_dict() for e in self.entries[:_MAX_ENTRIES]],
            "links": [lk.to_dict() for lk in self.links[:_MAX_LINKS]],
            "counts": {
                "entries": len(self.entries),
                "hypotheses": sum(1 for lk in causal if lk.epistemic is Epistemic.HYPOTHESIZED),
                "verified": sum(1 for lk in causal if lk.epistemic is Epistemic.VERIFIED),
                "refuted": sum(1 for lk in causal if lk.epistemic is Epistemic.REFUTED),
            },
        }


def build_timeline(entries: list[TimelineEntry], *,
                   assoc_window_s: float = _ASSOC_WINDOW_S,
                   hypothesis_window_s: float = _HYPOTHESIS_WINDOW_S) -> CausalTimeline:
    """Order the entries by time and derive epistemically-honest links between them.

    Link derivation, weakest first, with NO silent promotion:
      * CORRELATED — a finding entry to each event it matched (evidence-backed signal).
      * TEMPORALLY_ASSOCIATED — same-entity entries inside *assoc_window_s* with no
        stronger link (co-occurrence only; explicitly not causal).
      * HYPOTHESIZED — a change (drift) on an entity shortly after a correlation on the
        same entity: a candidate cause, marked UNPROVEN.
      * VERIFIED / REFUTED — a verification entry resolves any hypothesis it targets.
    """
    ordered = sorted([e for e in entries][:_MAX_ENTRIES],
                     key=lambda e: (e.at is None, e.at if e.at is not None else 0.0))
    by_id = {e.entry_id: e for e in ordered}
    links: list[CausalLink] = []
    linked_pairs: set[tuple[str, str]] = set()

    def _add(src: str, dst: str, ep: Epistemic, basis: str, band: Band) -> None:
        key = (src, dst)
        if key in linked_pairs or src == dst or len(links) >= _MAX_LINKS:
            return
        linked_pairs.add(key)
        links.append(CausalLink(src, dst, ep, basis, band))

    findings = [e for e in ordered if e.kind == "finding"]
    changes = [e for e in ordered if e.kind == "change"]
    verifications = [e for e in ordered if e.kind == "verification"]

    # 1) CORRELATED: finding -> each matched event (an evidence link, never proof).
    for f in findings:
        for ev_id in f.evidence_refs:
            if ev_id in by_id:
                _add(f.entry_id, ev_id, Epistemic.CORRELATED,
                     "correlation rule matched this event (signal, not proof)", Band.MED)

    # 2) HYPOTHESIZED: correlation then a change on the same entity, within window.
    hypotheses: list[CausalLink] = []
    for f in findings:
        for ch in changes:
            if len(links) >= _MAX_LINKS:
                break
            if not f.entity or ch.entity != f.entity:
                continue
            if f.at is None or ch.at is None or not (0 <= ch.at - f.at <= hypothesis_window_s):
                continue
            lk = CausalLink(f.entry_id, ch.entry_id, Epistemic.HYPOTHESIZED,
                            "change followed a correlation on the same entity - candidate "
                            "cause, UNPROVEN", Band.LOW)
            if (lk.src_id, lk.dst_id) not in linked_pairs:
                linked_pairs.add((lk.src_id, lk.dst_id))
                links.append(lk)
                hypotheses.append(lk)

    # 3) VERIFIED / REFUTED: a verification entry resolves hypotheses touching its entity.
    for v in verifications:
        verdict = Epistemic.VERIFIED if v.epistemic is Epistemic.VERIFIED else Epistemic.REFUTED
        for h in hypotheses:
            dst = by_id.get(h.dst_id)
            src = by_id.get(h.src_id)
            if v.entity and (getattr(dst, "entity", "") == v.entity or
                             getattr(src, "entity", "") == v.entity):
                # Resolve in place — the ONLY promotion path out of HYPOTHESIZED.
                h.epistemic = verdict
                h.basis = ("verification confirmed the hypothesized cause"
                           if verdict is Epistemic.VERIFIED
                           else "verification refuted the hypothesized cause")
                h.band = Band.HIGH if verdict is Epistemic.VERIFIED else Band.LOW

    # 4) TEMPORALLY_ASSOCIATED: same-entity neighbors with no stronger link (last resort).
    for i, a in enumerate(ordered):
        if a.at is None:
            continue
        for b in ordered[i + 1:]:
            if b.at is None:
                continue
            if b.at - a.at > assoc_window_s:
                break                                   # ordered by time -> no closer b left
            if a.entity and b.entity == a.entity \
                    and (a.entry_id, b.entry_id) not in linked_pairs \
                    and (b.entry_id, a.entry_id) not in linked_pairs:
                _add(a.entry_id, b.entry_id, Epistemic.TEMPORALLY_ASSOCIATED,
                     "co-occurrence on the same entity (not causal)", Band.LOW)

    return CausalTimeline(entries=ordered, links=links)


def timeline_from_facts(*, events=(), findings=(), drifts=(),
                        verifications=(), **kw) -> CausalTimeline:
    """Compose a causal timeline directly from spine facts (objects or their to_dict
    projections). ``verifications`` is a list of ``(entity, verified_bool, at_iso)`` or
    ``(entity, verified_bool, at_iso, basis)`` tuples. This is the integration surface
    the cognitive layer (M40) and AURA (M37) consume."""
    entries: list[TimelineEntry] = []
    entries += [entry_from_event(e) for e in events]
    entries += [entry_from_finding(f) for f in findings]
    entries += [entry_from_drift(d) for d in drifts]
    for v in verifications:
        entity, verified, at = v[0], bool(v[1]), v[2]
        basis = v[3] if len(v) > 3 else ""
        entries.append(entry_from_verification(entity, verified, at=at, basis=basis))
    return build_timeline(entries, **kw)


def build_live_causal_timeline() -> dict:
    """Bounded, HUD/CLI-safe live causal timeline assembled from the live spine (recent
    correlation findings + digital-twin drift). Read-only; guarded — any missing source
    degrades to fewer facts, never an error. No verifications are asserted from thin air."""
    findings: list = []
    drifts: list = []
    try:
        from core.ops_query import build_live_context
        ctx = build_live_context()
        findings = [f.to_dict() if hasattr(f, "to_dict") else f for f in (ctx.findings or [])]
        snap = ctx.twin_snapshot
        if snap is not None:
            snap_findings = getattr(snap, "findings", None) or []
            drifts = [d.to_dict() if hasattr(d, "to_dict") else d for d in snap_findings]
    except Exception:  # noqa: BLE001
        pass
    return timeline_from_facts(findings=findings, drifts=drifts).to_dict()
