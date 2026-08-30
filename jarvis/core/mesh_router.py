"""
core/mesh_router.py — V69 M64: evidence-aware routing into the specialist mesh.

Routing answers four questions and produces one typed object:

    who owns this problem?          -> primary specialist
    who else is needed?             -> supporting specialists (bounded)
    must it be verified?            -> ARGUS required
    how far may it act?             -> autonomy ceiling

It is **deterministic**: the same request with the same signals always yields the
same route. No model call happens here, so routing cannot be talked into a
different answer, and the gauntlet can assert on it exactly.

Reuse, not replacement. ``core.task_domain.classify_domain`` already classifies a
turn into a ``TaskDomain`` with a confidence and a deterministic tie-break, and
``core.agent_runtime.assemble_task_decision`` already composes the per-turn
``TaskDecision`` (model role, complexity, security sensitivity, planning, tools).
This module consumes both. It adds only what neither models: which *expert* owns
the problem, and how far that expert may go.

Two safety properties are structural rather than advisory:

  * **Ambiguity never routes into an offensive path.** ``SPECTER`` and ``VIOLET``
    are reachable only when the request carries an explicit authorization signal
    *and* names a target. §12's "do not confidently route ambiguous requests into
    an offensive path" is implemented as: without that signal the offensive
    intent is recognised, the route lands on ``GUARDIAN`` or ``ATLAS``, and the
    reason says a scope is missing.

  * **The ceiling is the minimum of everything.** The route's autonomy ceiling is
    the lowest of the primary's registry default, what the task's risk warrants,
    and — for security work — what a scope would allow. A specialist can never be
    routed above its own registry ceiling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.agent_runtime import TaskDecision, assemble_task_decision
from core.cognitive_mesh import (
    DEFAULT_BUDGET,
    FAST_PATH_BUDGET,
    REGISTRY,
    AutonomyLevel,
    MeshBudget,
    SpecialistId,
)
from core.model_router import ModelRole
from core.security_scope import ActivityClass
from core.task_domain import TaskDomain

_S = SpecialistId

# ── routing thresholds. Named, so a change to one is a visible change. ───────
LOW_CONFIDENCE = 0.45          # below this, ask rather than assume
FAST_PATH_COMPLEXITY = 0.35    # above this, the fast path is not appropriate
TEAM_COMPLEXITY = 0.60         # above this, a supporting specialist is warranted
COMPLEX_COMPLEXITY = 0.75      # above this, a second support plus the verifier


#: Primaries whose work is inherently multi-disciplinary: an incident needs
#: forensics and intelligence, an emulation needs both sides. These get two
#: supports regardless of the complexity score, which measures prose, not stakes.
_SECURITY_PRIMARY: frozenset[SpecialistId] = frozenset({
    _S.GUARDIAN, _S.TRACE, _S.SPECTER, _S.VIOLET,
})


class RouteMode(str, Enum):
    """How much machinery this request actually deserves (§38)."""

    FAST_PATH = "fast_path"          # one specialist, no tools, no verifier
    SINGLE = "single"                # one specialist, tools allowed
    TEAM = "team"                    # primary + support
    TEAM_VERIFIED = "team_verified"  # primary + support + ARGUS
    CLARIFY = "clarify"              # too ambiguous to route; ask one question


# ── the domain a specialist owns. One direction only: a TaskDomain maps to at
#    most one primary, so routing cannot become a negotiation. ───────────────
_DOMAIN_PRIMARY: dict[TaskDomain, SpecialistId] = {
    TaskDomain.GENERAL: _S.ATLAS,
    TaskDomain.LANGUAGE: _S.ATLAS,
    TaskDomain.MATHEMATICS: _S.ATLAS,
    TaskDomain.PLANNER: _S.ATLAS,
    TaskDomain.CODER: _S.FORGE,
    TaskDomain.ARCHITECT: _S.FORGE,
    TaskDomain.VISION: _S.ATLAS,
    TaskDomain.RESEARCH: _S.ARCHIVIST,
    TaskDomain.CYBER_BLUE: _S.GUARDIAN,
    TaskDomain.CYBER_PURPLE: _S.SPECTER,
    TaskDomain.DFIR: _S.TRACE,
    TaskDomain.GRC: _S.ARCHIVIST,
    TaskDomain.CRITIC: _S.ARGUS,
    TaskDomain.VERIFIER: _S.ARGUS,
}

#: Who a primary naturally pulls in, in priority order. Consulted only when the
#: budget and complexity actually warrant support — a list is not a summons.
_SUPPORT: dict[SpecialistId, tuple[SpecialistId, ...]] = {
    _S.ATLAS: (_S.ARCHIVIST,),
    _S.FORGE: (_S.HELIOS, _S.ARCHIVIST),
    _S.HELIOS: (_S.MESH, _S.FORGE),
    _S.MESH: (_S.HELIOS,),
    _S.GUARDIAN: (_S.TRACE, _S.ORACLE),
    _S.TRACE: (_S.GUARDIAN, _S.ORACLE),
    _S.ORACLE: (_S.GUARDIAN,),
    _S.SPECTER: (_S.FORGE, _S.VIOLET),
    _S.VIOLET: (_S.SPECTER, _S.GUARDIAN),
    _S.CIRRUS: (_S.HELIOS, _S.MESH),
    _S.CIRCUIT: (_S.FORGE,),
    _S.ARCHIVIST: (_S.ORACLE,),
    _S.ARGUS: (),
}

#: Vocabulary that shifts a route within a domain, or introduces a specialist the
#: domain classifier has no member for (HELIOS, MESH, CIRRUS, CIRCUIT). Bilingual,
#: matching ``core.task_domain``'s own EN+ES convention.
_SIGNALS: dict[SpecialistId, frozenset[str]] = {
    _S.HELIOS: frozenset({
        "systemd", "service", "servicio", "daemon", "container", "contenedor",
        "docker", "podman", "kubernetes", "k8s", "proxmox", "vm ", "hypervisor",
        "disk", "disco", "cpu", "memory", "memoria", "swap", "oom", "uptime",
        "restart", "reiniciar", "crash", "systemctl", "journalctl", "process",
        "proceso", "load average", "filesystem", "mount", "deployment", "deploy",
    }),
    _S.MESH: frozenset({
        "dns", "dhcp", "vlan", "subnet", "subred", "arp", "stp", "routing",
        "enrutamiento", "route ", "gateway", "nat", "firewall", "cortafuegos",
        "vpn", "tcp", "udp", "packet", "paquete", "ping", "traceroute", "mtu",
        "switch", "router", "interface", "interfaz", "ip address", "resolve",
        "resolver", "connectivity", "conectividad", "unreachable", "latency",
    }),
    _S.CIRRUS: frozenset({
        "aws", "azure", "gcp", "s3 bucket", "ec2", "lambda", "iam", "cloudtrail",
        "security group", "vpc", "cloud", "nube", "terraform", "cloudformation",
        "eks", "aks", "gke",
    }),
    _S.CIRCUIT: frozenset({
        "arduino", "esp32", "esp8266", "raspberry", "mcu", "microcontroller",
        "microcontrolador", "firmware", "gpio", "i2c", "spi", "uart", "serial",
        "ble", "bluetooth", "lora", "zigbee", "sensor", "embedded", "empotrado",
    }),
    _S.TRACE: frozenset({
        "forensic", "forense", "timeline", "cronología", "chain of custody",
        "memory dump", "volcado", "artifact", "artefacto", "prefetch",
        "shimcache", "amcache", "$mft", "evtx", "carve", "acquisition",
    }),
    _S.VIOLET: frozenset({
        "purple team", "equipo púrpura", "detection coverage", "did we detect",
        "did our detection", "detection gap", "atomic red team", "caldera",
        "emulation plan", "retest", "detección detectó",
    }),
    _S.ORACLE: frozenset({
        "cve", "ioc", "iocs", "threat actor", "actor de amenaza", "apt",
        "campaign", "campaña", "malware family", "familia de malware", "osint",
        "attribution", "atribución", "indicator", "indicador",
    }),
    _S.GUARDIAN: frozenset({
        "sysmon", "wazuh", "zeek", "suricata", "siem", "edr", "xdr", "soc",
        "alert", "alerta", "detection", "detección", "deteccion", "sigma",
        "triage", "triaje", "suspicious", "sospechoso", "sospechosa",
        "threat hunt", "hunting", "containment", "contención", "false positive",
        "falso positivo", "att&ck", "mitre", "log source", "correlation",
        "infected", "infectado", "infectada", "compromised", "comprometido",
        "endpoint", "malware", "ransomware", "beacon", "c2", "intrusion",
        "breach", "brecha", "phishing", "exfiltration", "exfiltración",
    }),
    _S.SPECTER: frozenset({
        "vulnerable", "vulnerability", "vulnerabilidad", "recon",
        "reconnaissance", "reconocimiento", "enumeration", "enumeración",
        "attack surface", "superficie de ataque", "security test",
        "prueba de seguridad", "red team", "equipo rojo", "adversary emulation",
        "emulación de adversario", "proof of concept", "poc",
    }),
    _S.FORGE: frozenset({
        "traceback", "stacktrace", "exception", "excepción", "pytest",
        "unit test", "prueba unitaria", "refactor", "refactorizar", "compile",
        "compilar", "segfault", "regression", "regresión", "api endpoint",
    }),
}

#: Deterministic tie-break when two specialists score the same number of signal
#: hits. More specific disciplines win over broader ones, and MESH deliberately
#: outranks HELIOS: a question that names both a host and a network layer ("why
#: can't this VM reach DNS") is a network question, and MESH's own contract sends
#: it to HELIOS if the link turns out to be fine.
_SIGNAL_PRIORITY: tuple[SpecialistId, ...] = (
    _S.TRACE, _S.VIOLET, _S.ORACLE, _S.SPECTER, _S.GUARDIAN, _S.MESH,
    _S.CIRRUS, _S.CIRCUIT, _S.FORGE, _S.HELIOS,
)

#: Explicit authorization vocabulary. Presence is NECESSARY for an offensive
#: route; it is never SUFFICIENT — a real ``AuthorizedSecurityScope`` still has
#: to exist and cover the target, which ``core.security_scope`` decides. This set
#: only distinguishes "the operator is talking about their own lab" from "the
#: operator said the word exploit".
_AUTHORIZATION_SIGNALS: frozenset[str] = frozenset({
    "my lab", "mi laboratorio", "our lab", "nuestro laboratorio", "homelab",
    "authorized", "autorizado", "autorizada", "i own", "we own", "soy dueño",
    "ctf", "hack the box", "hackthebox", "tryhackme", "vulnhub", "dvwa",
    "juice shop", "metasploitable", "test environment", "entorno de pruebas",
    "engagement", "pentest engagement", "scope id", "in scope", "en alcance",
    "localhost", "127.0.0.1", "::1",
})

#: Offensive intent, independent of authorization. Used to recognise that a
#: request WOULD be SPECTER's, so an unauthorized one can be refused explicitly
#: instead of silently answered by a defensive specialist.
_OFFENSIVE_SIGNALS: frozenset[str] = frozenset({
    "exploit", "explotar", "pentest", "pen test", "penetration test",
    "attack", "atacar", "ataque", "brute force", "fuerza bruta", "payload",
    "reverse shell", "privilege escalation", "escalada de privilegios",
    "sql injection", "inyección sql", "xss", "rce", "scan the", "escanear",
    "nmap", "enumerate", "enumerar", "crack", "bypass the", "get a shell",
})

#: Requests that ask for an effect on the world rather than an explanation.
_EFFECT_SIGNALS: frozenset[str] = frozenset({
    "restart", "reiniciar", "kill ", "matar", "delete", "borrar", "eliminar",
    "block ", "bloquear", "isolate", "aislar", "quarantine", "cuarentena",
    "disable", "deshabilitar", "shut down", "apagar", "revoke", "revocar",
    "contain", "contener", "remediate", "remediar", "apply the fix", "deploy",
})


#: One compiled boundary pattern per keyword across every vocabulary in this
#: module, built once at import. A keyword matches only where it starts and ends
#: on a word boundary, so ``ble`` no longer fires inside *vulnerable*.
_BOUNDARY: dict[str, "re.Pattern[str]"] = {
    kw: re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)")
    for vocab in (*_SIGNALS.values(), _AUTHORIZATION_SIGNALS, _OFFENSIVE_SIGNALS,
                  _EFFECT_SIGNALS)
    for kw in vocab
}


def _hits(text: str, vocab: "frozenset[str]") -> tuple[str, ...]:
    """Keywords from *vocab* present in *text* at a word boundary.

    Plain substring matching -- ``core.task_domain``'s convention -- is wrong for
    a vocabulary this size: CIRCUIT's ``"ble"`` matched *vulnerable* and routed an
    authorized web-lab request to the embedded specialist. Boundaries are checked
    here rather than by shortening the vocabulary, because the useful short
    tokens (``ble``, ``rce``, ``xss``, ``apt``, ``vm``) are exactly the ones a
    length rule would delete.
    """
    return tuple(sorted(k for k in vocab if _BOUNDARY[k].search(text)))


@dataclass(frozen=True)
class MeshRoute:
    """The typed routing decision (§10). Deterministic and side-effect free."""

    task_id: str
    goal: str
    mode: RouteMode
    primary: SpecialistId
    supporting: tuple[SpecialistId, ...]
    verifier_required: bool
    autonomy_ceiling: AutonomyLevel
    #: What the ceiling becomes IF an operator-registered scope actually covers
    #: the target and grants the activity. The router is pure and holds no
    #: scopes, so it reports the possibility; ``authorize_security_activity``
    #: decides, and the orchestrator applies. Equal to ``autonomy_ceiling``
    #: wherever no lift exists.
    scoped_autonomy_ceiling: AutonomyLevel
    confidence: float
    domains: tuple[TaskDomain, ...]
    complexity: float
    urgency: str
    effectful: bool
    risk: str
    security_sensitive: bool
    offensive_intent: bool
    authorization_signalled: bool
    target_scope: tuple[str, ...]
    requested_activities: tuple[ActivityClass, ...]
    required_evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    budget: MeshBudget
    preferred_model_role: ModelRole
    clarifying_question: str
    reason: str
    signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def specialists(self) -> tuple[SpecialistId, ...]:
        team = (self.primary, *self.supporting)
        if self.verifier_required and _S.ARGUS not in team:
            team = (*team, _S.ARGUS)
        return team

    @property
    def specialist_count(self) -> int:
        return len(self.specialists)

    def telemetry(self) -> dict:
        return {
            "task_id": self.task_id, "mode": self.mode.value,
            "primary_specialist": self.primary.value,
            "consulted_specialists": [s.value for s in self.supporting],
            "verifier_required": self.verifier_required,
            "autonomy_ceiling": int(self.autonomy_ceiling),
            "scoped_autonomy_ceiling": int(self.scoped_autonomy_ceiling),
            "routing_confidence": round(self.confidence, 2),
            "domains": [d.value for d in self.domains],
            "complexity": round(self.complexity, 2),
            "effectful": self.effectful, "risk": self.risk,
            "offensive_intent": self.offensive_intent,
            "authorization_signalled": self.authorization_signalled,
            "specialist_count": self.specialist_count,
            "preferred_model_role": self.preferred_model_role.value,
            "reason": self.reason,
        }


def _urgency(text: str, security_sensitive: bool) -> str:
    if any(k in text for k in ("right now", "urgent", "urgente", "immediately",
                               "inmediatamente", "production is down", "outage",
                               "actively", "in progress", "ongoing attack")):
        return "high"
    return "elevated" if security_sensitive else "routine"


def _extract_targets(text: str) -> tuple[str, ...]:
    """Tokens that look like a host, IP or URL the operator named.

    Extraction is for *routing shape only* — whether a target was named at all.
    Whether that target is authorized is ``core.security_scope``'s question, and
    nothing here grants anything.
    """
    out: list[str] = []
    for raw in (text or "").replace(",", " ").split():
        token = raw.strip("()[]<>\"'`.;:!?")
        if not token or len(token) > 128:
            continue
        if token.startswith(("http://", "https://")):
            out.append(token)
        elif token.count(".") == 3 and all(p.isdigit() for p in token.split(".")):
            out.append(token)
        elif "/" in token and token.split("/")[0].count(".") == 3:
            out.append(token)          # CIDR
        elif token in ("localhost", "::1"):
            out.append(token)
    return tuple(dict.fromkeys(out))[:8]


def _signal_primary(text: str) -> tuple[SpecialistId | None, tuple[str, ...], int]:
    """Best specialist by keyword signal, with a deterministic tie-break.

    Ties break by :class:`SpecialistId` declaration order so the same text always
    resolves the same way, matching ``task_domain``'s fixed tie-break contract.
    """
    best: SpecialistId | None = None
    best_hits: tuple[str, ...] = ()
    for sid in _SIGNAL_PRIORITY:
        hits = _hits(text, _SIGNALS.get(sid, frozenset()))
        if len(hits) > len(best_hits):
            best, best_hits = sid, hits
    return best, best_hits, len(best_hits)


def route_task(
    user_message: str,
    *,
    task_id: str = "",
    tool_names: list[str] | None = None,
    task_decision: TaskDecision | None = None,
    budget: MeshBudget | None = None,
) -> MeshRoute:
    """Route one request into the mesh. Pure, deterministic, no model call."""
    text = (user_message or "").lower()
    td = task_decision or assemble_task_decision(user_message, tool_names=tool_names)
    budget = budget or DEFAULT_BUDGET

    domain = td.domain
    complexity = float(td.complexity)
    security_sensitive = bool(td.security_sensitive)

    offensive_hits = _hits(text, _OFFENSIVE_SIGNALS)
    auth_hits = _hits(text, _AUTHORIZATION_SIGNALS)
    effect_hits = _hits(text, _EFFECT_SIGNALS)
    targets = _extract_targets(user_message)
    signal_primary, signal_hits, signal_strength = _signal_primary(text)

    # Offensive intent is EXPLICIT vocabulary, never a guess. The distinction
    # matters: an explicit unscoped attack request is not ambiguous, so SPECTER
    # owns it and its execution is denied for want of a scope (§42.13). An
    # *ambiguous* request is a different thing and must never reach SPECTER at
    # all (§12) -- which is why `_OFFENSIVE_SIGNALS` and the SPECTER signal set
    # are the only two ways in.
    offensive_intent = bool(offensive_hits) or domain is TaskDomain.CYBER_PURPLE or (
        signal_primary is _S.SPECTER and signal_strength >= 1)
    authorized_shape = bool(auth_hits) and bool(targets)

    # ── primary ──────────────────────────────────────────────────────────────
    primary = _DOMAIN_PRIMARY.get(domain, _S.ATLAS)
    reason_bits = [f"domain={domain.value}({td.domain_confidence:.2f})"]

    # A keyword signal outranks the domain classifier when the domain is the
    # GENERAL fallback (no opinion), or when the signal names a specialist the
    # 14 TaskDomains have no member for at all -- HELIOS, MESH, CIRRUS and
    # CIRCUIT exist only in this vocabulary.
    _DOMAINLESS = (_S.HELIOS, _S.MESH, _S.CIRRUS, _S.CIRCUIT)
    if signal_primary is not None and signal_strength >= 1:
        if domain is TaskDomain.GENERAL or signal_primary in _DOMAINLESS:
            primary = signal_primary
            reason_bits.append(f"signal={signal_primary.value}({signal_strength})")
        elif signal_strength >= 2 and signal_primary is not primary:
            primary = signal_primary
            reason_bits.append(f"signal={signal_primary.value}({signal_strength}) "
                               f"outweighs the domain")

    # ── the offensive gate (§12, §42.13) ─────────────────────────────────────
    clarifying = ""
    if offensive_intent:
        primary = _S.VIOLET if (signal_primary is _S.VIOLET) else _S.SPECTER
        if authorized_shape:
            reason_bits.append("explicit offensive intent with an authorization "
                               "signal and a named target")
        else:
            reason_bits.append(
                "explicit offensive intent WITHOUT an authorization signal and a "
                "named target -> SPECTER owns it at L0 ADVISE; every active step "
                "is denied for want of a scope")
            clarifying = (
                "That is an active security request. I can plan and explain it, but "
                "nothing will run against a target without an authorized scope: "
                "which environment is this, and which target and activity does your "
                "authorization actually cover?")
    elif primary in (_S.SPECTER, _S.VIOLET):
        # Reached without explicit offensive vocabulary -- ambiguity, not intent.
        primary = _S.GUARDIAN
        reason_bits.append("red/purple primary reached without explicit offensive "
                           "intent -> GUARDIAN (ambiguity never routes offensive)")

    record = REGISTRY.get(primary)

    # ── confidence ───────────────────────────────────────────────────────────
    confidence = float(td.domain_confidence)
    if signal_strength:
        confidence = min(1.0, confidence + 0.15 * min(signal_strength, 3))
    if domain is TaskDomain.GENERAL and not signal_strength:
        confidence = min(confidence, 0.4)
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    # ── mode and team (§13, §38) ─────────────────────────────────────────────
    effectful = bool(effect_hits)
    risk = "high_impact" if effectful else (
        "reversible" if security_sensitive else "read_only")
    verifier_required = bool(
        td.requires_verification or security_sensitive or effectful
        or offensive_intent or complexity >= COMPLEX_COMPLEXITY
        or primary in (_S.FORGE, _S.GUARDIAN, _S.TRACE, _S.SPECTER, _S.VIOLET, _S.CIRRUS)
    )

    # The fast path is ATLAS's alone (§38). Every other specialist's completion
    # contract begins with an observation -- World State, code, an artefact, a
    # source -- so "answer directly with no tools" is not a shape they have. A
    # diagnostic question that merely looks cheap is still a diagnosis.
    fast_eligible = (
        primary is _S.ATLAS
        and record.evidence_policy.value == "none_required"
        and complexity < FAST_PATH_COMPLEXITY
        and not security_sensitive and not effectful and not offensive_intent
        and not tool_names and not td.requires_planning and not verifier_required
    )
    # Ambiguity is only worth a question when the stakes or the complexity make a
    # wrong guess expensive. "What is 2+2?" routes to ATLAS with low confidence
    # and that is the correct, complete answer -- asking there would be theatre.
    ambiguous = (
        confidence < LOW_CONFIDENCE and not signal_strength and not offensive_intent
        and (complexity >= FAST_PATH_COMPLEXITY or effectful or security_sensitive
             or bool(tool_names))
    )

    supporting: tuple[SpecialistId, ...] = ()
    if ambiguous:
        mode = RouteMode.CLARIFY
        primary = _S.ATLAS
        record = REGISTRY.get(primary)
        verifier_required = False
        clarifying = clarifying or (
            "I can take this several ways. What outcome do you want — an "
            "explanation, a fix, or a change to something that is running?")
        reason_bits.append(f"routing confidence {confidence:.2f} below "
                           f"{LOW_CONFIDENCE} on a non-trivial request -> "
                           f"one clarifying question")
    elif fast_eligible:
        mode = RouteMode.FAST_PATH
        reason_bits.append("fast path: generalist, low complexity, no tools, no risk")
    else:
        want = 0
        if complexity >= COMPLEX_COMPLEXITY or offensive_intent or _SECURITY_PRIMARY \
                .__contains__(primary):
            want = 2
        elif complexity >= TEAM_COMPLEXITY or security_sensitive or effectful:
            want = 1
        room = max(0, budget.max_specialists - 1 - (1 if verifier_required else 0))
        supporting = tuple(
            s for s in _SUPPORT.get(primary, ())
            if s != primary and REGISTRY.handoff_allowed(primary, s)
        )[:min(want, room)]
        mode = (RouteMode.TEAM_VERIFIED if verifier_required and supporting else
                RouteMode.TEAM if supporting else RouteMode.SINGLE)
        reason_bits.append(f"{len(supporting)} support, verifier={verifier_required}")

    # ── autonomy ceiling: the minimum of every constraint ────────────────────
    ceiling = record.default_autonomy
    scoped_ceiling = record.ceiling_with_scope(True)
    if mode is RouteMode.FAST_PATH:
        ceiling = min(ceiling, AutonomyLevel.ADVISE, key=int)
        scoped_ceiling = ceiling
    if record.requires_security_scope and not authorized_shape:
        # No authorization signal and no named target: not even the possibility
        # of a lift, so the two ceilings collapse to ADVISE together.
        ceiling = AutonomyLevel.ADVISE
        scoped_ceiling = AutonomyLevel.ADVISE

    activities: tuple[ActivityClass, ...] = ()
    if primary in (_S.SPECTER, _S.VIOLET) and authorized_shape:
        activities = (ActivityClass.PASSIVE_RECON, ActivityClass.READ_ONLY_ENUMERATION)

    required_evidence = _required_evidence(primary, effectful, offensive_intent)
    uncertainty: list[str] = []
    if confidence < LOW_CONFIDENCE:
        uncertainty.append(f"routing confidence {confidence:.2f} is low")
    if offensive_intent and not authorized_shape:
        uncertainty.append("active security work requested with no authorized scope")
    if effectful:
        uncertainty.append("the request asks for a world effect, which needs approval")

    return MeshRoute(
        task_id=task_id or _task_id(user_message),
        goal=(user_message or "").strip()[:400],
        mode=mode, primary=primary, supporting=supporting,
        verifier_required=verifier_required, autonomy_ceiling=ceiling,
        scoped_autonomy_ceiling=scoped_ceiling,
        confidence=confidence, domains=(domain,), complexity=round(complexity, 2),
        urgency=_urgency(text, security_sensitive), effectful=effectful, risk=risk,
        security_sensitive=security_sensitive, offensive_intent=offensive_intent,
        authorization_signalled=bool(auth_hits), target_scope=targets,
        requested_activities=activities, required_evidence=required_evidence,
        uncertainty=tuple(uncertainty),
        budget=FAST_PATH_BUDGET if mode is RouteMode.FAST_PATH else budget,
        preferred_model_role=(record.preferred_model_roles or (ModelRole.FAST,))[0],
        clarifying_question=clarifying, reason="; ".join(reason_bits),
        signals=tuple(sorted({*offensive_hits, *auth_hits, *effect_hits, *signal_hits}))[:12],
    )


def _required_evidence(primary: SpecialistId, effectful: bool,
                       offensive: bool) -> tuple[str, ...]:
    """What this route must produce before its conclusion counts (§16)."""
    need: list[str] = []
    record = REGISTRY.get(primary)
    if record.evidence_policy.value == "evidence_required":
        need.append("every finding cites a corroborating reference")
    if primary in (_S.HELIOS, _S.MESH):
        need.append("World State consulted before any new observation")
    if primary is _S.GUARDIAN:
        need.append("severity and confidence reported separately")
    if primary is _S.TRACE:
        need.append("evidence preserved before any modifying step")
    if offensive:
        need.append("scope decision recorded for every active step")
    if effectful:
        need.append("a rollback plan for the proposed effect")
    return tuple(need)


def _task_id(message: str) -> str:
    import hashlib
    return "t" + hashlib.sha256((message or "").encode()).hexdigest()[:12]
