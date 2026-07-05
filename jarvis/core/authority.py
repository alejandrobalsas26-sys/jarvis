"""
core/authority.py — V63: operator authority model + authorized scope policy.

The design principle is **Reasoning Freedom ≠ Execution Authority**. This module
governs only the *execution of actions against targets* — it never touches what
JARVIS may read, explain, analyze, or reason about. Malware analysis, exploit
explanation, offensive-technique reasoning, DFIR, and code review are all
unaffected; only *acting on a specific asset* (an active scan, a request to a
host, etc.) is scope-gated.

Two operator-controlled concepts:

  * :class:`AuthorityMode` — the current operating authority posture
    (STANDARD / ADMIN_LOCAL / RESEARCH / CTF / TRUSTED_LAB / PURPLE_TEAM /
    INCIDENT_RESPONSE). Set ONLY by the operator (like SessionConsent /
    AssistantState). A model-generated tool argument can never change it —
    :func:`authorize_action` never reads authority hints from tool input.

  * :class:`ScopePolicy` — a bounded authorization envelope: which targets /
    CIDRs / domains / hostnames / VM / container ids are in-bounds, with an
    optional expiry and provenance.

Enforcement (fail-closed, additive — it *precedes* and never replaces the
existing risk-class / HITL / audit gate):

  * A tool that does not act on a target is never scope-gated here.
  * When enforcement is inactive (STANDARD posture and no scopes configured),
    target tools pass through unchanged — the pre-existing gate still applies,
    so default behavior is byte-identical.
  * When enforcement is active (a scoped mode, or any configured scope), a target
    action is REFUSED unless the target falls inside an active (non-expired)
    scope. A missing/malformed target fails closed. An expired scope is ignored.
    An in-scope action then proceeds to the normal risk/HITL checks.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlparse


class AuthorityMode(str, Enum):
    """Operator-selected execution-authority posture for the session."""
    STANDARD = "standard"                    # default: no elevated action authority
    ADMIN_LOCAL = "admin_local"              # owned local machine administration
    RESEARCH = "research"                    # reasoning-heavy; no special action authority
    CTF = "ctf"                              # authorized CTF target range
    TRUSTED_LAB = "trusted_lab"              # owned homelab / VM range
    PURPLE_TEAM = "purple_team"              # authorized purple-team exercise scope
    INCIDENT_RESPONSE = "incident_response"  # explicit IR asset set


# Modes that, on their own, assert scoped action authority (and therefore turn on
# fail-closed scope enforcement even before any scope is registered).
_SCOPED_MODES: frozenset[AuthorityMode] = frozenset({
    AuthorityMode.CTF, AuthorityMode.TRUSTED_LAB, AuthorityMode.PURPLE_TEAM,
    AuthorityMode.INCIDENT_RESPONSE, AuthorityMode.ADMIN_LOCAL,
})


def parse_mode(value: "str | AuthorityMode | None") -> AuthorityMode:
    """Coerce *value* into an AuthorityMode, defaulting safely to STANDARD."""
    if isinstance(value, AuthorityMode):
        return value
    try:
        return AuthorityMode(str(value).strip().lower())
    except (ValueError, AttributeError):
        return AuthorityMode.STANDARD


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        # An unparseable expiry is treated as already-expired (fail-closed).
        return _now()


def _host_of(target: str) -> str:
    """Extract a bare host from a URL or host[:port]; return the input otherwise."""
    t = (target or "").strip()
    if "://" in t:
        parsed = urlparse(t)
        return (parsed.hostname or "").strip().lower()
    # strip a trailing :port if present and it's not part of an IPv6 literal
    if t.count(":") == 1 and "[" not in t:
        t = t.split(":", 1)[0]
    return t.strip().lower()


@dataclass
class ScopePolicy:
    """A bounded authorization envelope. All membership checks are exact or
    subnet/subdomain — never a substring, so 'evil-example.com' does not match
    an 'example.com' scope."""
    scope_id: str
    name: str = ""
    mode: AuthorityMode = AuthorityMode.STANDARD
    targets: frozenset[str] = field(default_factory=frozenset)      # exact IPs/hosts
    cidrs: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()                                   # example.com ⊇ a.example.com
    hostnames: frozenset[str] = field(default_factory=frozenset)
    vm_ids: frozenset[str] = field(default_factory=frozenset)
    container_ids: frozenset[str] = field(default_factory=frozenset)
    lab_networks: tuple[str, ...] = ()                             # additional CIDRs
    expires_at: str | None = None
    created_by: str = "operator"
    notes: str = ""

    def is_expired(self, now: datetime | None = None) -> bool:
        exp = _parse_ts(self.expires_at)
        if exp is None:
            return False
        return (now or _now()) >= exp

    def _matches_ip(self, target: str) -> bool:
        try:
            ip = ipaddress.ip_address(target)
        except ValueError:
            return False
        for net in (*self.cidrs, *self.lab_networks):
            try:
                if ip in ipaddress.ip_network(net, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def _matches_domain(self, host: str) -> bool:
        for d in self.domains:
            d = d.strip().lower().lstrip(".")
            if host == d or host.endswith("." + d):
                return True
        return False

    def contains(self, target: str, *, now: datetime | None = None) -> bool:
        """Whether *target* falls inside this (non-expired) scope."""
        if not target or self.is_expired(now):
            return False
        host = _host_of(target)
        exact = {t.strip().lower() for t in (*self.targets, *self.hostnames,
                                             *self.vm_ids, *self.container_ids)}
        if host in exact or target.strip().lower() in exact:
            return True
        if self._matches_ip(host) or self._matches_ip(target.strip()):
            return True
        return self._matches_domain(host)

    def to_dict(self) -> dict:
        return {
            "scope_id": self.scope_id, "name": self.name, "mode": self.mode.value,
            "targets": sorted(self.targets), "cidrs": list(self.cidrs),
            "domains": list(self.domains), "hostnames": sorted(self.hostnames),
            "vm_ids": sorted(self.vm_ids), "container_ids": sorted(self.container_ids),
            "lab_networks": list(self.lab_networks), "expires_at": self.expires_at,
            "created_by": self.created_by, "notes": self.notes,
        }


@dataclass
class AuthorityState:
    """Session-scoped, operator-controlled authority posture + active scopes.
    Threaded through the runtime like SessionConsent / AssistantState; mutated
    ONLY by explicit operator commands, never from model/tool input."""
    mode: AuthorityMode = AuthorityMode.STANDARD
    scopes: list[ScopePolicy] = field(default_factory=list)

    def set_mode(self, mode: "str | AuthorityMode") -> bool:
        new = parse_mode(mode)
        if new == self.mode:
            return False
        self.mode = new
        return True

    def add_scope(self, scope: ScopePolicy) -> None:
        self.scopes = [s for s in self.scopes if s.scope_id != scope.scope_id]
        self.scopes.append(scope)

    def remove_scope(self, scope_id: str) -> bool:
        before = len(self.scopes)
        self.scopes = [s for s in self.scopes if s.scope_id != scope_id]
        return len(self.scopes) != before

    def active_scopes(self, now: datetime | None = None) -> list[ScopePolicy]:
        now = now or _now()
        return [s for s in self.scopes if not s.is_expired(now)]

    def enforcement_active(self, now: datetime | None = None) -> bool:
        """Scope enforcement engages when the operator has declared a scoped
        authority mode OR configured any active scope. STANDARD/RESEARCH with no
        scopes → inactive (existing gate governs, no behavior change)."""
        return self.mode in _SCOPED_MODES or bool(self.active_scopes(now))

    def is_in_scope(self, target: str, now: datetime | None = None) -> bool:
        now = now or _now()
        return any(s.contains(target, now=now) for s in self.active_scopes(now))


def default_authority() -> AuthorityState:
    """A fresh STANDARD authority state with no scopes (enforcement inactive)."""
    return AuthorityState()


# ── Which tools act on an external target, and where the target lives ────────
# Only these are ever scope-gated. Everything else (read/reasoning/local tools)
# is never touched by authority enforcement.
_SCOPE_BOUND_TOOLS: dict[str, tuple[str, ...]] = {
    "network_scan": ("target",),
    "check_connectivity": ("host", "target"),
    "whois_lookup": ("domain",),
    "osint_lookup": ("target", "domain"),
    "http_request": ("url",),
    "desplegar_webapp": ("host",),
}


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    reason: str
    requires_scope: bool = False
    target: str | None = None
    in_scope: bool | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "reason": self.reason,
            "requires_scope": self.requires_scope, "target": self.target,
            "in_scope": self.in_scope,
        }


def _extract_target(tool_name: str, tool_input: dict) -> str | None:
    for field_name in _SCOPE_BOUND_TOOLS.get(tool_name, ()):
        val = (tool_input or {}).get(field_name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def authorize_action(
    state: AuthorityState | None,
    tool_name: str,
    tool_input: dict,
    *,
    now: datetime | None = None,
) -> AuthorityDecision:
    """Scope decision for one action. Never reads authority hints from
    ``tool_input`` — authority is server-side only, so untrusted content can
    never widen it. Reasoning/non-target tools are always allowed here."""
    if tool_name not in _SCOPE_BOUND_TOOLS:
        return AuthorityDecision(True, "not a scope-bound action", requires_scope=False)

    st = state or default_authority()
    now = now or _now()
    if not st.enforcement_active(now):
        # No scoped mode and no active scope → defer entirely to the existing
        # risk/HITL gate (no behavior change from before this module).
        return AuthorityDecision(
            True, "scope enforcement inactive (STANDARD posture, no scopes)",
            requires_scope=True, target=_extract_target(tool_name, tool_input),
        )

    target = _extract_target(tool_name, tool_input)
    if not target:
        # Enforcement is active but we cannot identify the target → fail closed.
        return AuthorityDecision(
            False, f"'{tool_name}' target missing/malformed under active authority "
                   f"'{st.mode.value}' — refused (fail-closed)",
            requires_scope=True, target=None, in_scope=False,
        )
    if st.is_in_scope(target, now):
        return AuthorityDecision(
            True, f"target in active authorized scope ({st.mode.value})",
            requires_scope=True, target=target, in_scope=True,
        )
    return AuthorityDecision(
        False, f"target '{target}' outside authorized scope "
               f"({st.mode.value}) — refused (fail-closed)",
        requires_scope=True, target=target, in_scope=False,
    )
