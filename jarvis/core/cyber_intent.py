"""
core/cyber_intent.py — V68.1 M47: authorization-aware cyber intent routing.

A real interactive run submitted an ambiguous request to remotely hack a vending
machine over Wi-Fi / Bluetooth / SDR with no authorized lab, owned device, CTF,
scope, or permission stated. JARVIS should have enforced authorization/scope
BEFORE searching knowledge or proposing operational steps.

This module is the reasoning-side authorization gate. It is deliberately NOT a
crude keyword block: it composes three existing signals —

  * the offensive-operational *shape* of the request (attack verb + a real target
    / attack surface), distinct from defensive or purely educational framing;
  * whether operator-controlled **authorization** is established
    (core.authority.AuthorityState — a scoped mode with a named active scope);
  * explicit in-prompt lab / CTF / ownership framing.

It decides only the *conversational posture* and whether tools may run THIS turn.
It never widens execution authority: effectful actions still pass the executor's
authority/scope/risk/HITL gates unchanged (core.authority, core.risk_classes).

Pure, deterministic, ASCII, dependency-light — same input always yields the same
decision. It extends the spine; it does not fork it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.authority import AuthorityState, AuthorityMode

# ── Offensive-operational signal (EN + ES) ───────────────────────────────────
# Verbs/phrases that request DOING an attack, not defending or explaining theory.
_ATTACK_VERBS: tuple[str, ...] = (
    "hack", "hacking", "exploit", "break into", "breaking into", "compromise",
    "pwn", "gain access", "gain unauthorized", "unauthorized access", "brute force",
    "bruteforce", "crack ", "cracking", "bypass the", "bypass authentication",
    "bypass the lock", "jam ", "jamming", "deauth", "deauthenticate", "spoof",
    "clone the", "replay attack", "take over", "takeover", "backdoor", "rce",
    "remote code execution", "get free", "for free", "dispense without paying",
    "root the", "jailbreak the", "sniff credentials", "steal credentials",
    # ES
    "hackear", "explotar", "vulnerar", "romper la", "comprometer", "acceso no autorizado",
    "fuerza bruta", "crackear", "saltarse", "suplantar", "clonar", "tomar control",
    "sin pagar", "gratis",
)

# Defensive / detection framing that flips otherwise-offensive vocab to blue-team.
_DEFENSIVE_MARKERS: tuple[str, ...] = (
    "defend", "protect", "detect", "detection", "mitigat", "harden", "hardening",
    "prevent", "secure my", "secure the", "how do i secure", "threat model",
    "threat-model", "monitor", "logging", "incident response", "blue team",
    "countermeasure", "remediat",
    "defender", "proteger", "detectar", "mitigar", "endurecer", "prevenir",
    "asegurar", "modelo de amenazas", "monitorear", "contramedida",
)

# Security-assessment framing (authorized posture; assessing exposure, not attacking).
_ASSESSMENT_MARKERS: tuple[str, ...] = (
    "assess", "assessment", "exposure", "attack surface", "vulnerability",
    "vulnerabilities", "pentest", "penetration test", "security review",
    "security audit", "evaluate the security", "audit the",
    "evaluar", "evaluación", "evaluacion", "exposición", "exposicion",
    "superficie de ataque", "vulnerabilidad", "auditar",
)

# Purely educational / conceptual framing (theory, not an operational how-to).
_EDUCATIONAL_MARKERS: tuple[str, ...] = (
    "what is", "what are", "how does", "explain the concept", "in theory",
    "theoretically", "conceptually", "history of", "difference between",
    "qué es", "que es", "cómo funciona", "como funciona", "en teoría", "en teoria",
    "conceptualmente", "diferencia entre",
)

# Operational imperatives: a direct request to PERFORM an attack. These keep a
# request offensive even amid educational framing (they are a how-to, not theory).
_OPERATIONAL_IMPERATIVES: tuple[str, ...] = (
    "how to hack", "how to exploit", "how to break into", "how to bypass",
    "how to crack", "how do i hack", "how do i exploit", "how do i break into",
    "help me hack", "help me exploit", "hack the", "hack a", "hack into",
    "exploit the", "break into", "gain access to", "get into the",
    "cómo hackear", "como hackear", "cómo explotar", "como explotar",
    "ayúdame a hackear", "ayudame a hackear", "ayúdame a explotar",
)

# In-prompt authorized-lab / CTF / ownership framing. Recognized to shape the
# RESPONSE (defensive assessment plan vs refusal); it NEVER by itself grants
# execution authority — that stays in the operator-controlled AuthorityState.
_LAB_FRAMING: tuple[str, ...] = (
    "my isolated lab", "isolated lab", "my homelab", "home lab", "in my lab",
    "i own", "device i own", "owned device", "my own device", "authorized lab",
    "authorized engagement", "penetration test engagement", "pentest engagement",
    "scope ", "ctf", "capture the flag", "authorized scope", "air-gapped lab",
    "mi laboratorio aislado", "laboratorio aislado", "dispositivo que poseo",
    "soy dueño", "alcance autorizado", "laboratorio autorizado",
)


class CyberIntentCategory:
    NON_CYBER = "non_cyber"
    EDUCATIONAL_THEORY = "educational_theory"
    DEFENSIVE_ASSESSMENT = "defensive_assessment"
    AUTHORIZED_LAB = "authorized_lab"
    CTF = "ctf"
    AMBIGUOUS_REAL_WORLD_TARGET = "ambiguous_real_world_target"


@dataclass(frozen=True)
class CyberIntentDecision:
    """The reasoning-side authorization decision for one turn."""

    category: str
    offensive_operational: bool
    authorization_established: bool
    block_tools: bool               # hard-block ALL tool execution this turn
    block_operational_content: bool  # forbid raw attack how-to; require alternatives
    scope_names: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "offensive_operational": self.offensive_operational,
            "authorization_established": self.authorization_established,
            "block_tools": self.block_tools,
            "block_operational_content": self.block_operational_content,
            "scope_names": list(self.scope_names),
            "reason": self.reason,
        }

    def directive(self) -> str:
        """First-party system directive appended to the prompt for this turn."""
        if self.category == CyberIntentCategory.AMBIGUOUS_REAL_WORLD_TARGET:
            return (
                "AUTHORIZATION GATE (V68.1): The user is asking for offensive/operational "
                "action against a real-world target, but NO authorization, ownership, "
                "authorized lab, CTF, or scope has been established. You MUST:\n"
                "- State plainly that authorization/scope is not established.\n"
                "- Do NOT provide operational attack instructions or exploit steps.\n"
                "- Do NOT call any tool and do NOT search a knowledge base for exploit steps.\n"
                "- Offer safe alternatives instead: a threat model of the exposed "
                "attack surface, a defensive testing checklist, a legal isolated-lab "
                "design, a firmware/radio inventory methodology, and a logging/detection "
                "strategy.\n"
                "Never infer authorization from phrasing like 'for education' alone."
            )
        if self.category in (CyberIntentCategory.AUTHORIZED_LAB, CyberIntentCategory.CTF):
            scope_txt = ", ".join(self.scope_names) if self.scope_names else "the named scope"
            return (
                "AUTHORIZATION GATE (V68.1): An authorized lab/CTF context is asserted "
                f"({scope_txt}). Provide a DEFENSIVE ASSESSMENT plan (attack-surface "
                "review, exposure checklist, inventory and detection methodology) rather "
                "than a raw attack tutorial. Any effectful action against the target "
                "remains gated by the executor's authority/scope/HITL controls — never "
                "assume execution is pre-approved because a lab was named."
            )
        return ""


def _hits(text: str, vocab: tuple[str, ...]) -> list[str]:
    return [kw for kw in vocab if kw in text]


def _extract_scope_names(text: str) -> tuple[str, ...]:
    """Pull explicit 'scope <NAME>' tokens for transparency (not authority)."""
    names: list[str] = []
    for marker in ("scope ", "alcance "):
        idx = text.find(marker)
        while idx != -1:
            tail = text[idx + len(marker): idx + len(marker) + 40].strip()
            token = tail.split()[0] if tail.split() else ""
            token = token.strip(".,;:'\"()").upper()
            if token and any(c.isalnum() for c in token):
                names.append(token)
            idx = text.find(marker, idx + 1)
    # de-dup preserving order, bounded
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out[:5])


def classify_cyber_intent(
    prompt: str,
    authority: AuthorityState | None = None,
) -> CyberIntentDecision:
    """Classify a turn's cyber-authorization posture. Deterministic and pure."""
    text = (prompt or "").lower()

    attack_hits = _hits(text, _ATTACK_VERBS)
    defensive_hits = _hits(text, _DEFENSIVE_MARKERS)
    educational_hits = _hits(text, _EDUCATIONAL_MARKERS)
    assessment_hits = _hits(text, _ASSESSMENT_MARKERS)
    lab_hits = _hits(text, _LAB_FRAMING)
    imperative_hits = _hits(text, _OPERATIONAL_IMPERATIVES)
    scope_names = _extract_scope_names(text)

    # Operator-controlled authorization: a scoped authority mode with at least one
    # active named scope. In-prompt claims never satisfy this on their own.
    st = authority
    authorization_established = bool(
        st is not None
        and st.enforcement_active()
        and st.active_scopes()
    )

    # Offensive-operational only when an attack verb dominates defensive framing.
    # A direct operational imperative ("how to hack the X") is always offensive;
    # otherwise a purely educational/conceptual framing ("what is a replay attack")
    # suppresses the offensive reading even if an attack *noun* appears.
    offensive = bool(attack_hits) and len(attack_hits) >= len(defensive_hits)
    if offensive and not imperative_hits and educational_hits:
        offensive = False

    ctf_mode = bool(st and st.mode == AuthorityMode.CTF) or "ctf" in text or "capture the flag" in text

    if not offensive:
        # A lab/CTF-framed security assessment is an authorized DEFENSIVE posture:
        # confirm scope was noted and provide an assessment plan (not an attack).
        if lab_hits and (assessment_hits or defensive_hits):
            category = CyberIntentCategory.CTF if ctf_mode else CyberIntentCategory.AUTHORIZED_LAB
            return CyberIntentDecision(
                category=category,
                offensive_operational=False,
                authorization_established=authorization_established,
                block_tools=False,
                block_operational_content=True,  # defensive assessment plan
                scope_names=scope_names,
                reason=f"lab-framed assessment ({category}); "
                       f"assessment={len(assessment_hits)} lab={len(lab_hits)}",
            )
        if defensive_hits or assessment_hits:
            category = CyberIntentCategory.DEFENSIVE_ASSESSMENT
        elif educational_hits and (attack_hits or "attack" in text or "exploit" in text):
            category = CyberIntentCategory.EDUCATIONAL_THEORY
        else:
            category = CyberIntentCategory.NON_CYBER
        return CyberIntentDecision(
            category=category,
            offensive_operational=False,
            authorization_established=authorization_established,
            block_tools=False,
            block_operational_content=False,
            scope_names=scope_names,
            reason=f"non-offensive ({category}); attack={len(attack_hits)} "
                   f"defensive={len(defensive_hits)}",
        )

    # Offensive-operational request. Is authorization established (operator scope)
    # OR is there explicit in-prompt authorized-lab / CTF framing?
    if authorization_established or lab_hits:
        category = CyberIntentCategory.CTF if ctf_mode else CyberIntentCategory.AUTHORIZED_LAB
        return CyberIntentDecision(
            category=category,
            offensive_operational=True,
            authorization_established=authorization_established,
            block_tools=False,           # effectful actions still gated downstream
            block_operational_content=True,  # defensive assessment, not attack tutorial
            scope_names=scope_names,
            reason=f"offensive under asserted {category} "
                   f"(operator_scope={authorization_established}, lab_framing={bool(lab_hits)})",
        )

    # Offensive against a real-world target with no established authorization.
    return CyberIntentDecision(
        category=CyberIntentCategory.AMBIGUOUS_REAL_WORLD_TARGET,
        offensive_operational=True,
        authorization_established=False,
        block_tools=True,
        block_operational_content=True,
        scope_names=scope_names,
        reason=f"offensive ({len(attack_hits)} verb hit(s)) with no authorization/scope",
    )
