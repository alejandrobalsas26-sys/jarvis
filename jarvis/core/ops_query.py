"""core/ops_query.py — V67 M32: grounded natural-language operational query runtime.

A READ-ONLY question-answering layer over the existing structured operational state
(situation engine, asset graph, incident workspace, digital twin, correlation findings,
collector fabric). It does NOT dump databases into a model; it does NOT invent assets,
incidents, services, evidence or actions. The pipeline is:

    question → typed operational intent → read-only query plan → structured retrieval
             → BOUNDED FACT BUNDLE → (optional) LLM synthesis constrained to the bundle

Grounding is the contract: every fact in a :class:`FactBundle` traces to a real object,
and the deterministic ``answer`` is composed ONLY from those facts — so the engine is
correct and testable with no LLM at all. When an LLM is used it is handed ONLY the
bundle (``to_grounding``) and must not introduce a fact outside it.

Honest empty state: "I do not have evidence of an active incident." — never
"everything is secure". Unknown is reported as unknown; absence of evidence is never
reported as proof of absence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.ops_views import _MAX_LIST, _redact

_MAX_FACTS = 12


class QueryIntent(str, Enum):
    WHAT_IS_HAPPENING = "what_is_happening"
    WHAT_CHANGED = "what_changed"
    UNHEALTHY_ASSETS = "unhealthy_assets"
    INCIDENT_IMPORTANCE = "incident_importance"
    FINDING_EVIDENCE = "finding_evidence"
    EXPOSED_SERVICES = "exposed_services"
    BLIND_SENSORS = "blind_sensors"
    WHAT_IS_UNCERTAIN = "what_is_uncertain"
    RECOMMEND_RUNBOOK = "recommend_runbook"
    WHY_RUNBOOK = "why_runbook"
    INCIDENT_TIMELINE = "incident_timeline"
    STOPPED_CONTAINER = "stopped_container"
    COLLECTOR_HEALTH = "collector_health"
    READINESS = "readiness"
    UNKNOWN = "unknown"


# Ordered (specific → general): the FIRST matching rule wins. Each rule is a set of
# lowercase substrings; ALL of a rule's `all` must be present, or ANY of its `any`.
_RULES: tuple[tuple[QueryIntent, dict], ...] = (
    (QueryIntent.WHY_RUNBOOK, {"all": ("why",), "any": ("runbook", "recommend")}),
    (QueryIntent.RECOMMEND_RUNBOOK, {"any": ("recommend a runbook", "what runbook",
                                             "which runbook", "recommended runbook",
                                             "runbook do you", "recommend runbook")}),
    (QueryIntent.INCIDENT_TIMELINE, {"any": ("timeline",)}),
    (QueryIntent.FINDING_EVIDENCE, {"any": ("evidence", "support this", "supports this")}),
    (QueryIntent.INCIDENT_IMPORTANCE, {"all": ("incident",),
                                       "any": ("why", "important", "importance", "matter")}),
    (QueryIntent.STOPPED_CONTAINER, {"any": ("container", "workload"),
                                     "any2": ("stop", "down", "fail", "which")}),
    (QueryIntent.EXPOSED_SERVICES, {"any": ("exposed", "exposure", "which services",
                                            "open port", "listening")}),
    (QueryIntent.BLIND_SENSORS, {"any": ("sensor", "blind", "coverage", "heartbeat")}),
    (QueryIntent.WHAT_IS_UNCERTAIN, {"any": ("uncertain", "unsure", "don't know",
                                             "do not know", "what is unknown")}),
    (QueryIntent.UNHEALTHY_ASSETS, {"any": ("unhealthy", "degraded", "which assets",
                                            "asset health", "sick", "broken")}),
    (QueryIntent.COLLECTOR_HEALTH, {"any": ("collector", "ingest", "feed health")}),
    (QueryIntent.READINESS, {"any": ("ready", "readiness", "field ready", "deployable")}),
    (QueryIntent.WHAT_CHANGED, {"any": ("changed", "what's new", "whats new",
                                        "last ten minutes", "recently", "since")}),
    (QueryIntent.WHAT_IS_HAPPENING, {"any": ("happening", "right now", "current status",
                                             "situation", "what is going on", "status")}),
)


def classify_intent(question: str) -> QueryIntent:
    """Deterministic keyword classification — no LLM, no network, CPU-cheap."""
    q = (question or "").strip().lower()
    if not q:
        return QueryIntent.UNKNOWN
    for intent, rule in _RULES:
        if "all" in rule and not all(s in q for s in rule["all"]):
            continue
        if "any" in rule and not any(s in q for s in rule["any"]):
            continue
        if "any2" in rule and not any(s in q for s in rule["any2"]):
            continue
        return intent
    return QueryIntent.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  Context (read-only slice of the live state, or injected fixtures)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class OperationalContext:
    situation: object | None = None          # SituationSnapshot
    graph: object | None = None              # AssetGraph
    incidents: list = field(default_factory=list)     # list[IncidentCase]
    twin_snapshot: object | None = None      # TwinSnapshot
    findings: list = field(default_factory=list)      # list[CorrelationFinding]
    sensors: dict = field(default_factory=dict)
    collectors: dict | None = None           # fabric.aura_panel() dict
    question: str = ""                       # transient: the current question text


def _d(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Fact bundle
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class FactBundle:
    """The bounded, grounded result of a query. ``answer`` is composed ONLY from
    ``facts``; ``empty`` marks an honest no-evidence answer (never 'all secure')."""
    question: str
    intent: QueryIntent
    facts: list[str]
    sources: list[str]
    answer: str
    data: dict = field(default_factory=dict)
    empty: bool = False
    grounded: bool = True

    def to_dict(self) -> dict:
        return {"question": self.question, "intent": self.intent.value,
                "facts": self.facts[:_MAX_FACTS], "sources": sorted(set(self.sources)),
                "answer": self.answer, "data": self.data, "empty": self.empty,
                "grounded": self.grounded}

    def to_grounding(self) -> dict:
        """The ONLY facts an LLM synthesizer may use. An answer must not introduce an
        operational fact outside this bundle."""
        return {
            "instruction": ("Answer the operator's question using ONLY the facts below. "
                            "Do not invent assets, incidents, services, evidence or "
                            "actions. If the facts do not support a claim, say so. "
                            "Never say 'everything is secure' — unknown is not safe."),
            "question": self.question, "intent": self.intent.value,
            "facts": self.facts[:_MAX_FACTS], "sources": sorted(set(self.sources)),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  The engine
# ══════════════════════════════════════════════════════════════════════════════
class OperationalQueryEngine:
    """Answers operator questions from the structured state. Read-only: it never
    executes a tool, mutates state, or invents a fact."""

    def answer(self, question: str, context: OperationalContext) -> FactBundle:
        intent = classify_intent(question)
        context.question = question or ""     # transient: for id-scoped intents
        handler = getattr(self, f"_h_{intent.value}", self._h_unknown)
        facts, sources, answer, data, empty = handler(context)
        return FactBundle(question=question, intent=intent, facts=facts[:_MAX_FACTS],
                          sources=sources, answer=answer, data=data, empty=empty)

    # ── handlers: each returns (facts, sources, answer, data, empty) ──────────
    def _h_what_is_happening(self, ctx):
        s = _d(ctx.situation)
        if not s:
            return ([], ["situation_engine"],
                    "I do not have a current situation snapshot.", {}, True)
        summ = s.get("summary", {})
        sev = s.get("severity", "calm")
        prios = s.get("priorities", [])
        facts = [f"situation severity: {sev}",
                 f"open incidents: {summ.get('open_incidents', 0)} "
                 f"({summ.get('critical_incidents', 0)} critical)",
                 f"active priorities: {len(prios)}"]
        for p in prios[:3]:
            facts.append(f"priority: {_redact(p.get('title', ''))} "
                         f"({p.get('severity')}, conf {p.get('confidence')})")
        empty = not prios and not summ.get("open_incidents")
        answer = (f"Situation is {sev.upper()}. "
                  + ("No active operational priorities; I do not have evidence of an "
                     "active incident." if empty
                     else f"{summ.get('open_incidents', 0)} open incident(s) and "
                          f"{len(prios)} active priorit{'y' if len(prios) == 1 else 'ies'}. "
                          f"Top: {_redact((prios[0].get('title', '') if prios else ''))}."))
        return (facts, ["situation_engine"], answer, {"severity": sev}, empty)

    def _h_what_changed(self, ctx):
        s = _d(ctx.situation)
        wc = s.get("what_changed", {})
        if wc.get("baseline"):
            return (["this is the first snapshot — no prior state to diff"],
                    ["situation_engine"],
                    "This is the first snapshot; there is no earlier state to compare.",
                    {}, True)
        new, resolved = wc.get("new", []), wc.get("resolved", [])
        facts = [f"newly appeared: {len(new)}", f"resolved: {len(resolved)}"]
        facts += [f"new: {_redact(n)}" for n in new[:5]]
        facts += [f"resolved: {_redact(r)}" for r in resolved[:5]]
        empty = not new and not resolved
        answer = ("Nothing changed since the last snapshot." if empty
                  else f"{len(new)} item(s) newly appeared and {len(resolved)} resolved.")
        return (facts, ["situation_engine"], answer, {"new": new, "resolved": resolved}, empty)

    def _h_unhealthy_assets(self, ctx):
        unhealthy: dict[str, str] = {}
        for df in _d(ctx.twin_snapshot).get("findings", []):
            if df.get("drift_type") != "state_unknown":
                unhealthy[_redact(df.get("asset", ""))] = df.get("drift_type", "drift")
        for c in ctx.incidents:
            for a in _d(c).get("affected_assets", []):
                unhealthy.setdefault(_redact(a), "incident-affected")
        for name, st in (ctx.sensors or {}).items():
            if _is_down(st):
                unhealthy.setdefault(_redact(name), f"sensor {st}")
        facts = [f"{a}: {why}" for a, why in list(unhealthy.items())[:_MAX_LIST]]
        empty = not unhealthy
        answer = ("No assets are currently observed as unhealthy (this is not proof "
                  "that none are)." if empty
                  else f"{len(unhealthy)} asset(s) look unhealthy: "
                       + ", ".join(list(unhealthy)[:6]) + ".")
        return (facts, ["digital_twin", "incident_workspace", "sensors"], answer,
                {"unhealthy": unhealthy}, empty)

    def _h_incident_importance(self, ctx):
        cases = [_d(c) for c in ctx.incidents]
        cases = [c for c in cases if c.get("status") not in ("closed", "false_positive")]
        if not cases:
            return ([], ["incident_workspace"],
                    "I do not have evidence of an active incident.", {}, True)
        top = max(cases, key=lambda c: (_SEV.get(c.get("severity", "medium"), 3),
                                        c.get("confidence", 0)))
        facts = [f"incident {top.get('incident_id')}: {_redact(top.get('title', ''))}",
                 f"severity {top.get('severity')}, confidence {top.get('confidence')}",
                 f"{len(top.get('correlation_findings', []))} correlation finding(s)",
                 f"{len(top.get('affected_assets', []))} affected asset(s)",
                 f"techniques: {', '.join(top.get('mitre_techniques', [])[:6]) or 'none'}"]
        answer = (f"Incident {top.get('incident_id')} matters most: severity "
                  f"{top.get('severity')} with {len(top.get('correlation_findings', []))} "
                  f"correlation finding(s) across {len(top.get('affected_assets', []))} "
                  f"asset(s).")
        return (facts, ["incident_workspace"], answer, {"incident": top.get("incident_id")}, False)

    def _h_finding_evidence(self, ctx):
        if not ctx.findings:
            return ([], ["correlation_v2"],
                    "There are no correlation findings, so there is no supporting "
                    "evidence to show.", {}, True)
        f = _d(ctx.findings[-1])
        facts = [f"finding {f.get('finding_id')}: rule {f.get('rule')}",
                 f"explanation: {_redact(f.get('explanation', {}).get('reason', ''))}"]
        for ev in f.get("evidence", [])[:6]:
            facts.append(f"evidence: {ev.get('source')}/{ev.get('category')} "
                         f"@ {ev.get('observed_at')} ({ev.get('event_id')})")
        answer = (f"Finding {f.get('finding_id')} ({f.get('rule')}) is supported by "
                  f"{len(f.get('evidence', []))} evidence item(s) from "
                  f"{len(f.get('matched_event_ids', []))} matched event(s).")
        return (facts, ["correlation_v2"], answer, {"finding": f.get("finding_id")}, False)

    def _h_exposed_services(self, ctx):
        services = []
        g = ctx.graph
        if g is not None and hasattr(g, "exposed_services"):
            try:
                services = g.exposed_services(only_reachable=False)
            except Exception:  # noqa: BLE001
                services = []
        rows = [s for s in services if str(s.get("exposure", "")).lower()
                in ("external", "internal")]
        facts = [f"{_redact(s.get('host', ''))}:{s.get('port')} "
                 f"({s.get('exposure')})" for s in rows[:_MAX_LIST]]
        empty = not rows
        answer = ("No externally/internally exposed services are currently observed "
                  "(this is not proof none exist)." if empty
                  else f"{len(rows)} exposed service(s) observed: "
                       + ", ".join(facts[:6]) + ".")
        return (facts, ["asset_graph"], answer, {"count": len(rows)}, empty)

    def _h_blind_sensors(self, ctx):
        blind = {_redact(n): _redact(st) for n, st in (ctx.sensors or {}).items()
                 if _is_down(st)}
        for df in _d(ctx.twin_snapshot).get("findings", []):
            if df.get("drift_type") in ("sensor_coverage_drift", "state_unknown"):
                blind.setdefault(_redact(df.get("asset", "")), df.get("drift_type"))
        facts = [f"{n}: {st}" for n, st in list(blind.items())[:_MAX_LIST]]
        empty = not blind
        answer = ("No sensor is currently reported blind; note this is coverage as "
                  "observed, not a guarantee." if empty
                  else f"{len(blind)} sensor/coverage gap(s): " + ", ".join(list(blind)[:6]) + ".")
        return (facts, ["sensors", "digital_twin"], answer, {"blind": blind}, empty)

    def _h_what_is_uncertain(self, ctx):
        unc = list(_d(ctx.situation).get("uncertainties", []))
        for df in _d(ctx.twin_snapshot).get("findings", []):
            if df.get("drift_type") == "state_unknown":
                unc.append(f"unverified: {_redact(df.get('asset', ''))}")
        unc = list(dict.fromkeys(_redact(u) for u in unc))
        facts = unc[:_MAX_LIST]
        empty = not unc
        answer = ("Nothing is currently flagged uncertain, but absence of a flag is not "
                  "certainty." if empty
                  else f"{len(unc)} uncertain item(s): " + "; ".join(unc[:6]) + ".")
        return (facts, ["situation_engine", "digital_twin"], answer, {"uncertain": unc}, empty)

    def _h_recommend_runbook(self, ctx):
        recs = _d(ctx.situation).get("recommendations", [])
        if not recs:
            return ([], ["situation_engine"],
                    "I have no runbook recommendation — no priority currently warrants "
                    "one.", {}, True)
        r = recs[0]
        facts = [f"recommended: {r.get('runbook')} (mode {r.get('mode')})",
                 f"rationale: {_redact(r.get('rationale', ''))}",
                 f"addresses: {_redact(r.get('title', ''))}"]
        answer = (f"I recommend {r.get('runbook')} ({r.get('mode')}) - "
                  f"{_redact(r.get('rationale', ''))}.")
        return (facts, ["situation_engine"], answer, {"runbook": r.get("runbook")}, False)

    def _h_why_runbook(self, ctx):
        recs = _d(ctx.situation).get("recommendations", [])
        if not recs:
            return ([], ["situation_engine"],
                    "No runbook is recommended right now, so there is nothing to justify.",
                    {}, True)
        r = recs[0]
        facts = [f"runbook: {r.get('runbook')}",
                 f"why: {_redact(r.get('rationale', ''))}",
                 f"priority: {_redact(r.get('title', ''))}",
                 f"mode: {r.get('mode')} (planning only until you approve)"]
        answer = (f"{r.get('runbook')} is recommended because {_redact(r.get('rationale', ''))}. "
                  f"It runs as {r.get('mode')} - a plan you must approve before any effect.")
        return (facts, ["situation_engine"], answer, {"runbook": r.get("runbook")}, False)

    def _h_incident_timeline(self, ctx):
        cases = [_d(c) for c in ctx.incidents]
        if not cases:
            return ([], ["incident_workspace"],
                    "There is no incident, so there is no timeline to show.", {}, True)
        wanted = _extract_incident_id_from_question(getattr(ctx, "question", ""))
        case = next((c for c in cases if c.get("incident_id") == wanted), cases[0])
        entries = case.get("timeline", [])
        facts = [f"{e.get('ts', '')} {e.get('kind', '')}: {_redact(e.get('message', ''))}"
                 for e in entries[:_MAX_LIST]]
        answer = (f"Timeline for incident {case.get('incident_id')}: "
                  f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}.")
        return (facts, ["incident_workspace"], answer,
                {"incident": case.get("incident_id")}, not entries)

    def _h_stopped_container(self, ctx):
        stopped = [df for df in _d(ctx.twin_snapshot).get("findings", [])
                   if df.get("drift_type") in ("workload_stopped", "service_missing")]
        facts = [f"{_redact(df.get('asset', ''))}: {df.get('drift_type')} "
                 f"-> {df.get('recommended_investigation')}" for df in stopped[:_MAX_LIST]]
        empty = not stopped
        answer = ("No container/workload is observed stopped." if empty
                  else f"{len(stopped)} workload(s) observed stopped: "
                       + ", ".join(_redact(df.get('asset', '')) for df in stopped[:6]) + ".")
        return (facts, ["digital_twin"], answer, {"stopped": len(stopped)}, empty)

    def _h_collector_health(self, ctx):
        panel = ctx.collectors or {}
        metrics = panel.get("metrics", {})
        cols = panel.get("collectors", [])
        degraded = [c for c in cols if c.get("status") in
                    ("degraded", "failed", "backpressure")]
        facts = [f"total {metrics.get('total', len(cols))}, active "
                 f"{metrics.get('active', 0)}, dormant {metrics.get('dormant', 0)}, "
                 f"failed {metrics.get('failed', 0)}"]
        facts += [f"{_redact(c.get('id', ''))}: {c.get('status')}" for c in degraded[:_MAX_LIST]]
        empty = not cols
        answer = ("I have no collector fabric information." if empty
                  else f"{metrics.get('active', 0)} active / {metrics.get('dormant', 0)} "
                       f"dormant collector(s); {len(degraded)} degraded/failed.")
        return (facts, ["collector_fabric"], answer, {"degraded": len(degraded)}, empty)

    def _h_readiness(self, ctx):
        s = _d(ctx.situation).get("summary", {})
        panel = ctx.collectors or {}
        m = panel.get("metrics", {})
        facts = [f"assets known: {s.get('known_assets', 0)}",
                 f"open incidents: {s.get('open_incidents', 0)}",
                 f"collectors active: {m.get('active', 0)} / dormant {m.get('dormant', 0)}",
                 f"sensors: {sum(1 for v in (ctx.sensors or {}).values() if not _is_down(v))} "
                 f"healthy / {sum(1 for v in (ctx.sensors or {}).values() if _is_down(v))} degraded",
                 f"situation: {_d(ctx.situation).get('severity', 'calm')}"]
        answer = ("Readiness snapshot: "
                  f"{s.get('known_assets', 0)} asset(s) observed, "
                  f"{s.get('open_incidents', 0)} open incident(s), "
                  f"{m.get('active', 0)} active collector(s).")
        return (facts, ["situation_engine", "collector_fabric", "sensors"], answer, {}, False)

    def _h_unknown(self, ctx):
        answer = ("I can answer about the current situation, what changed, unhealthy "
                  "assets, exposed services, blind sensors, uncertainty, incidents and "
                  "their evidence/timeline, stopped containers, collectors, and the "
                  "recommended runbook. Ask me one of those.")
        return ([answer], ["ops_query"], answer, {}, True)


_SEV = {"info": 1, "low": 2, "medium": 3, "high": 4, "critical": 5, "unknown": 0}
_ID_RE = re.compile(r"\binc[_a-z0-9]+\b", re.IGNORECASE)


def _is_down(status) -> bool:
    return any(w in str(status or "").lower()
              for w in ("down", "disconnected", "offline", "inactive", "stopped", "degraded"))


def _extract_incident_id_from_question(question: str) -> str | None:
    m = _ID_RE.search(question or "")
    return m.group(0) if m else None


# ══════════════════════════════════════════════════════════════════════════════
#  Live context + convenience
# ══════════════════════════════════════════════════════════════════════════════
def build_live_context(*, sensors: dict | None = None) -> OperationalContext:
    """Assemble a read-only context from the live V66/V67 singletons. Bounded; no I/O
    beyond the in-memory spine — safe while a DEEP inference runs."""
    ctx = OperationalContext(sensors=dict(sensors or {}))
    try:
        from core.asset_graph import graph
        ctx.graph = graph
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.incident_workspace import workspace
        ctx.incidents = workspace.open_cases()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.digital_twin import twin
        ctx.twin_snapshot = twin.compute_drift()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.correlation_v2 import correlator_v2
        ctx.findings = correlator_v2.recent(_MAX_LIST)
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.collector_fabric import fabric
        ctx.collectors = fabric.aura_panel()
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.situation_engine import engine
        ctx.situation = engine.build(
            asset_graph=ctx.graph, incidents=ctx.incidents, drift=ctx.twin_snapshot,
            correlation_findings=ctx.findings, sensor_health=ctx.sensors)
    except Exception:  # noqa: BLE001
        pass
    return ctx


_engine = OperationalQueryEngine()


def answer_question(question: str, *, context: OperationalContext | None = None,
                    sensors: dict | None = None) -> FactBundle:
    """Answer one operator question against the live (or injected) state. Read-only."""
    ctx = context or build_live_context(sensors=sensors)
    return _engine.answer(question, ctx)
