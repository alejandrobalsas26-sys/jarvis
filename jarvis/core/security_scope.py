"""
core/security_scope.py — V69 M64: the authorized-security-scope envelope.

M63 and earlier already answer *"may this action touch that target?"*:
:class:`core.authority.ScopePolicy` matches a target against exact hosts, CIDRs
and domains, and :func:`core.authority.authorize_action` is called fail-closed
inside ``ToolExecutor.aexecute``. **None of that is replaced here.** A second
target matcher would be a second truth, and the first thing that would rot.

What M64 adds is the part the target matcher never modelled: *which kind of
security activity* is authorized, *in what kind of environment*, *up to what
risk*, and *until when* — the envelope an internal red or purple specialist must
present before anything active happens. An :class:`AuthorizedSecurityScope`
therefore **composes** a ``ScopePolicy`` instead of reimplementing one:

    AuthorizedSecurityScope
      ├─ policy: ScopePolicy        → WHICH targets  (reused, expiry included)
      ├─ environment_type           → WHAT KIND of environment
      ├─ permitted_activity_classes → WHAT MAY BE DONE
      ├─ maximum_risk: RiskClass    → HOW FAR        (reused taxonomy)
      └─ issued_at / reference      → WHO SAID SO

Invariants this module exists to enforce:

  * **No active offensive operation without a valid scope.** The default answer
    is DENY. An empty registry denies, an expired scope denies, a missing target
    denies, an unrecognised activity denies.
  * **A scope is operator data, never model data.** :class:`SecurityScopeRegistry`
    is mutated only by explicit operator registration, exactly like
    ``AuthorityState``. Nothing in a prompt, a handoff, a blackboard entry, a
    memory record, telemetry or a tool result has a path to it.
  * **Reasoning is never scope-gated.** Explaining an exploit, reading malware,
    mapping ATT&CK and designing a detection are unaffected — this module gates
    *acting on a target*, matching ``core.authority``'s own stated boundary.
  * **Escalation is proved, not assumed.** :class:`RedTeamLevel` is a monotone
    ladder and :func:`next_level_justified` refuses to skip a rung, or to climb
    one without evidence from the rung below.

Pure, deterministic and offline: no socket, no model, no subprocess, and no
clock other than an injectable ``now``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, IntEnum

from core.authority import AuthorityMode, ScopePolicy

# ── bounds ───────────────────────────────────────────────────────────────────
MAX_SCOPES = 16
MAX_APPLICATIONS = 64
MAX_REFERENCE_CHARS = 512


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class EnvironmentType(str, Enum):
    """The kind of environment a scope authorizes work in. There is deliberately
    no member meaning "production", and none meaning "the internet"."""

    LAB = "lab"                                        # owned homelab / VM range
    CTF = "ctf"                                        # authorized competition range
    OWNED_INFRA = "owned_infra"                        # infrastructure the operator owns
    THIRD_PARTY_AUTHORIZED = "third_party_authorized"  # written engagement authority


#: Which operator authority posture each environment corresponds to. The posture
#: is what ``ToolExecutor`` already reads, so this map keeps two vocabularies in
#: step rather than introducing a third.
ENVIRONMENT_AUTHORITY_MODE: dict[EnvironmentType, AuthorityMode] = {
    EnvironmentType.LAB: AuthorityMode.TRUSTED_LAB,
    EnvironmentType.CTF: AuthorityMode.CTF,
    EnvironmentType.OWNED_INFRA: AuthorityMode.ADMIN_LOCAL,
    EnvironmentType.THIRD_PARTY_AUTHORIZED: AuthorityMode.PURPLE_TEAM,
}


class ActivityClass(str, Enum):
    """What a security specialist is actually about to do.

    Deliberately finer-grained than "pentest": a scope grants *classes*, so an
    operator who authorized enumeration has not thereby authorized exploitation.
    """

    PASSIVE_RECON = "passive_recon"
    READ_ONLY_ENUMERATION = "read_only_enumeration"
    ACTIVE_SERVICE_VALIDATION = "active_service_validation"
    WEB_VULNERABILITY_VALIDATION = "web_vulnerability_validation"
    AUTH_CONTROL_VALIDATION = "auth_control_validation"
    PRIVILEGE_PATH_VALIDATION = "privilege_path_validation"
    EXPLOIT_PROOF_MINIMAL = "exploit_proof_minimal"
    PURPLE_EMULATION = "purple_emulation"


#: Activity classes that reach out and touch the target. ``PASSIVE_RECON`` is the
#: only member that does not — and it is still scope-gated when it names one.
ACTIVE_ACTIVITY_CLASSES: frozenset[ActivityClass] = frozenset(
    set(ActivityClass) - {ActivityClass.PASSIVE_RECON}
)


class RedTeamLevel(IntEnum):
    """SPECTER's progressive-escalation ladder (§21). Monotone and gapless."""

    UNDERSTAND = 1              # read the target's description; touch nothing
    READ_ONLY_ENUMERATION = 2   # enumerate what is exposed
    ACTIVE_SAFE_VALIDATION = 3  # confirm a hypothesis without an effect
    MINIMUM_EXPLOIT_PROOF = 4   # the smallest proof that settles the question


#: The rung each activity class sits on. An activity may never run under a scope
#: that has not reached its rung.
ACTIVITY_LEVEL: dict[ActivityClass, RedTeamLevel] = {
    ActivityClass.PASSIVE_RECON: RedTeamLevel.UNDERSTAND,
    ActivityClass.READ_ONLY_ENUMERATION: RedTeamLevel.READ_ONLY_ENUMERATION,
    ActivityClass.ACTIVE_SERVICE_VALIDATION: RedTeamLevel.ACTIVE_SAFE_VALIDATION,
    ActivityClass.WEB_VULNERABILITY_VALIDATION: RedTeamLevel.ACTIVE_SAFE_VALIDATION,
    ActivityClass.AUTH_CONTROL_VALIDATION: RedTeamLevel.ACTIVE_SAFE_VALIDATION,
    ActivityClass.PURPLE_EMULATION: RedTeamLevel.ACTIVE_SAFE_VALIDATION,
    ActivityClass.PRIVILEGE_PATH_VALIDATION: RedTeamLevel.MINIMUM_EXPLOIT_PROOF,
    ActivityClass.EXPLOIT_PROOF_MINIMAL: RedTeamLevel.MINIMUM_EXPLOIT_PROOF,
}


#: Activities no scope may ever grant. They are refused before any scope is read,
#: so an over-broad scope never gets the question and a malformed one cannot
#: smuggle them in. They are plain strings precisely because they are NOT members
#: of :class:`ActivityClass`: there is no vocabulary for them, by design.
FORBIDDEN_ACTIVITIES: frozenset[str] = frozenset({
    "destructive_wipe", "ransomware", "data_exfiltration", "mass_exploitation",
    "internet_wide_scanning", "covert_persistence", "credential_theft",
    "denial_of_service", "detection_evasion", "supply_chain_compromise",
})


class ScopeDenial(str, Enum):
    """Why an activity was refused. Every member is a DENY; there is no
    ``UNKNOWN`` a caller could read as permission."""

    NO_SCOPE_REGISTERED = "no_scope_registered"
    FORBIDDEN_ACTIVITY = "forbidden_activity"
    UNRECOGNISED_ACTIVITY = "unrecognised_activity"
    MISSING_TARGET = "missing_target"
    TARGET_OUT_OF_SCOPE = "target_out_of_scope"
    ACTIVITY_NOT_PERMITTED = "activity_not_permitted"
    ACTIVITY_PROHIBITED = "activity_prohibited"
    SCOPE_EXPIRED = "scope_expired"
    RISK_ABOVE_SCOPE_MAXIMUM = "risk_above_scope_maximum"
    LEVEL_NOT_REACHED = "level_not_reached"
    APPLICATION_OUT_OF_SCOPE = "application_out_of_scope"


#: Risk ordering for ``maximum_risk`` comparisons. ``core.risk_classes`` owns the
#: classes and their HITL policy and deliberately does not order them, so the
#: order lives here, next to the only check that needs one.
_RISK_ORDER: dict[str, int] = {
    "read_only": 0, "low_impact": 1, "reversible": 2, "high_impact": 3, "lab_only": 4,
}


def risk_rank(risk) -> int:
    """Rank of *risk*. An unknown value ranks above every known one, so an
    unrecognised risk can never compare as "within" a maximum."""
    return _RISK_ORDER.get(getattr(risk, "value", str(risk)),
                           max(_RISK_ORDER.values()) + 1)


@dataclass(frozen=True)
class AuthorizedSecurityScope:
    """A bounded authorization envelope for active security work.

    ``policy`` is the reused :class:`~core.authority.ScopePolicy`: it owns target
    membership *and* expiry, so the system has exactly one matcher and exactly
    one clock. Everything else here is the M64 envelope around it.
    """

    scope_id: str
    environment_type: EnvironmentType
    policy: ScopePolicy
    permitted_activity_classes: frozenset[ActivityClass] = frozenset()
    prohibited_activity_classes: frozenset[ActivityClass] = frozenset()
    authorized_applications: frozenset[str] = frozenset()
    maximum_risk: str = "read_only"      # a ``RiskClass`` value
    issued_at: str = ""
    issued_by: str = "operator"
    reference: str = ""                  # ticket / engagement letter / lab note

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference",
                           (self.reference or "")[:MAX_REFERENCE_CHARS])
        object.__setattr__(
            self, "authorized_applications",
            frozenset(sorted(self.authorized_applications)[:MAX_APPLICATIONS]),
        )

    # ── expiry is the policy's, never a second one ───────────────────────────
    @property
    def expires_at(self) -> str | None:
        return self.policy.expires_at

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.policy.is_expired(now)

    @property
    def authority_mode(self) -> AuthorityMode:
        return ENVIRONMENT_AUTHORITY_MODE[self.environment_type]

    @property
    def max_level(self) -> RedTeamLevel:
        """The highest rung this scope reaches: the ladder position of its most
        permissive *granted* activity. A scope that grants nothing reaches only
        UNDERSTAND, which touches no target."""
        granted = self.permitted_activity_classes - self.prohibited_activity_classes
        if not granted:
            return RedTeamLevel.UNDERSTAND
        return max(ACTIVITY_LEVEL[a] for a in granted)

    def permits_activity(self, activity: ActivityClass) -> bool:
        """Prohibition beats permission, always. A scope that both grants and
        prohibits a class denies it: the operator's narrower statement governs."""
        if activity in self.prohibited_activity_classes:
            return False
        return activity in self.permitted_activity_classes

    def contains_target(self, target: str, *, now: datetime | None = None) -> bool:
        return self.policy.contains(target, now=now)

    def permits_application(self, application: str | None) -> bool:
        """An application constraint is opt-in: a scope naming no application
        constrains none; a scope naming some constrains to exactly those."""
        if not self.authorized_applications:
            return True
        return (application or "").strip().lower() in {
            a.strip().lower() for a in self.authorized_applications
        }

    def to_dict(self) -> dict:
        return {
            "scope_id": self.scope_id,
            "environment_type": self.environment_type.value,
            "authority_mode": self.authority_mode.value,
            "policy": self.policy.to_dict(),
            "permitted_activity_classes":
                sorted(a.value for a in self.permitted_activity_classes),
            "prohibited_activity_classes":
                sorted(a.value for a in self.prohibited_activity_classes),
            "authorized_applications": sorted(self.authorized_applications),
            "maximum_risk": self.maximum_risk,
            "max_level": int(self.max_level),
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "expires_at": self.expires_at,
            "reference": self.reference,
        }


@dataclass(frozen=True)
class SecurityScopeDecision:
    """The answer to one scope question. ``allowed`` is the only field a caller
    may branch on, and it is ``False`` unless every check passed."""

    allowed: bool
    activity: str
    target: str | None = None
    scope_id: str | None = None
    denial: ScopeDenial | None = None
    reason: str = ""
    level: RedTeamLevel | None = None

    @classmethod
    def deny(cls, denial: ScopeDenial, activity: str, reason: str, *,
             target: str | None = None, scope_id: str | None = None
             ) -> "SecurityScopeDecision":
        return cls(allowed=False, activity=activity, target=target,
                   scope_id=scope_id, denial=denial, reason=reason)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "activity": self.activity,
            "target": self.target, "scope_id": self.scope_id,
            "denial": self.denial.value if self.denial else None,
            "reason": self.reason,
            "level": int(self.level) if self.level is not None else None,
        }


@dataclass
class SecurityScopeRegistry:
    """Operator-controlled set of active scopes.

    Mutated ONLY by :meth:`register` and :meth:`revoke`, which the operator
    command surface calls. No specialist, handoff, blackboard entry, memory
    record, world-state observation, telemetry event or tool result has a path
    here — that absence is the control, and it is asserted by test.
    """

    scopes: list[AuthorizedSecurityScope] = field(default_factory=list)

    def register(self, scope: AuthorizedSecurityScope) -> bool:
        """Register (or replace) *scope*. Returns ``False`` when the bound is
        full rather than evicting a scope the operator still believes active."""
        remaining = [s for s in self.scopes if s.scope_id != scope.scope_id]
        if len(remaining) >= MAX_SCOPES:
            return False
        remaining.append(scope)
        self.scopes = remaining
        return True

    def revoke(self, scope_id: str) -> bool:
        before = len(self.scopes)
        self.scopes = [s for s in self.scopes if s.scope_id != scope_id]
        return len(self.scopes) != before

    def active(self, now: datetime | None = None) -> list[AuthorizedSecurityScope]:
        now = now or _now()
        return [s for s in self.scopes if not s.is_expired(now)]

    def to_dict(self) -> dict:
        return {"scopes": [s.to_dict() for s in self.scopes], "count": len(self.scopes)}


def authorize_security_activity(
    registry: SecurityScopeRegistry | None,
    *,
    activity: "ActivityClass | str",
    target: str | None,
    risk: str = "read_only",
    application: str | None = None,
    reached_level: RedTeamLevel | None = None,
    now: datetime | None = None,
) -> SecurityScopeDecision:
    """Decide whether one security activity against one target is authorized.

    Fail-closed at every branch, and the order of the branches is deliberate:
    what is *never* permitted is refused before any scope is read, so an
    over-broad or malformed scope never even gets the question.
    """
    now = now or _now()
    label = getattr(activity, "value", str(activity))

    # 1. Categorically forbidden — no scope can grant these.
    if label in FORBIDDEN_ACTIVITIES:
        return SecurityScopeDecision.deny(
            ScopeDenial.FORBIDDEN_ACTIVITY, label,
            f"'{label}' is categorically forbidden and no scope can authorize it",
            target=target)

    # 2. A vocabulary we do not recognise is not a permission we can check.
    try:
        act = activity if isinstance(activity, ActivityClass) else ActivityClass(label)
    except ValueError:
        return SecurityScopeDecision.deny(
            ScopeDenial.UNRECOGNISED_ACTIVITY, label,
            f"'{label}' is not a recognised activity class — refused (fail-closed)",
            target=target)

    # 3. An activity that names no target has undecidable scope membership.
    clean_target = (target or "").strip()
    if not clean_target:
        return SecurityScopeDecision.deny(
            ScopeDenial.MISSING_TARGET, label,
            f"'{label}' names no target, so scope membership is undecidable — refused")

    if registry is None or not registry.scopes:
        return SecurityScopeDecision.deny(
            ScopeDenial.NO_SCOPE_REGISTERED, label,
            "no AuthorizedSecurityScope is registered; active security work is "
            "refused by default", target=clean_target)

    live = registry.active(now)
    if not live:
        return SecurityScopeDecision.deny(
            ScopeDenial.SCOPE_EXPIRED, label,
            "every registered scope has expired; an expired authorization is not "
            "an authorization", target=clean_target)

    # 4. Walk the live scopes, keeping the most specific denial seen so the
    #    operator is told what was actually wrong instead of a generic refusal.
    best: SecurityScopeDecision | None = None
    for scope in live:
        if not scope.contains_target(clean_target, now=now):
            best = best or SecurityScopeDecision.deny(
                ScopeDenial.TARGET_OUT_OF_SCOPE, label,
                f"target '{clean_target}' is outside every authorized scope",
                target=clean_target)
            continue
        if act in scope.prohibited_activity_classes:
            best = SecurityScopeDecision.deny(
                ScopeDenial.ACTIVITY_PROHIBITED, label,
                f"scope '{scope.scope_id}' explicitly prohibits {label}",
                target=clean_target, scope_id=scope.scope_id)
            continue
        if not scope.permits_activity(act):
            best = SecurityScopeDecision.deny(
                ScopeDenial.ACTIVITY_NOT_PERMITTED, label,
                f"scope '{scope.scope_id}' covers this target but does not grant "
                f"{label}; authorizing enumeration is not authorizing exploitation",
                target=clean_target, scope_id=scope.scope_id)
            continue
        if not scope.permits_application(application):
            best = SecurityScopeDecision.deny(
                ScopeDenial.APPLICATION_OUT_OF_SCOPE, label,
                f"application '{application}' is not among the applications scope "
                f"'{scope.scope_id}' authorizes",
                target=clean_target, scope_id=scope.scope_id)
            continue
        if risk_rank(risk) > risk_rank(scope.maximum_risk):
            best = SecurityScopeDecision.deny(
                ScopeDenial.RISK_ABOVE_SCOPE_MAXIMUM, label,
                f"risk '{getattr(risk, 'value', risk)}' exceeds scope "
                f"'{scope.scope_id}' maximum '{scope.maximum_risk}'",
                target=clean_target, scope_id=scope.scope_id)
            continue

        required = ACTIVITY_LEVEL[act]
        if reached_level is not None and int(reached_level) < int(required) - 1:
            best = SecurityScopeDecision.deny(
                ScopeDenial.LEVEL_NOT_REACHED, label,
                f"{label} sits at level {int(required)} and the operation has only "
                f"reached level {int(reached_level)}; rungs are not skipped",
                target=clean_target, scope_id=scope.scope_id)
            continue

        return SecurityScopeDecision(
            allowed=True, activity=label, target=clean_target,
            scope_id=scope.scope_id, level=required,
            reason=f"{label} authorized against '{clean_target}' by scope "
                   f"'{scope.scope_id}' ({scope.environment_type.value})")

    return best or SecurityScopeDecision.deny(
        ScopeDenial.TARGET_OUT_OF_SCOPE, label,
        f"target '{clean_target}' is outside every authorized scope",
        target=clean_target)


def next_level_justified(
    current: RedTeamLevel,
    proposed: RedTeamLevel,
    *,
    evidence_count: int,
    hypothesis: str = "",
) -> tuple[bool, str]:
    """Whether climbing from *current* to *proposed* is justified (§21).

    Three refusals: a rung is never skipped; a rung is never climbed without
    evidence from the one below; a rung is never climbed without a hypothesis it
    would test. Descending or standing still is always fine — the ladder
    constrains escalation, not caution.
    """
    if int(proposed) <= int(current):
        return True, "no escalation requested"
    if int(proposed) > int(current) + 1:
        return False, (f"level {int(current)} -> {int(proposed)} skips a rung; "
                       f"escalation is one level at a time")
    if evidence_count <= 0:
        return False, (f"level {int(proposed)} requires evidence gathered at level "
                       f"{int(current)}; none was recorded")
    if not (hypothesis or "").strip():
        return False, (f"level {int(proposed)} requires a stated hypothesis it "
                       f"would test; 'go further' is not one")
    return True, (f"level {int(current)} -> {int(proposed)} justified by "
                  f"{evidence_count} item(s) of evidence and a stated hypothesis")


def lab_scope(
    scope_id: str,
    *,
    targets: "frozenset[str] | set[str] | tuple[str, ...]" = (),
    cidrs: tuple[str, ...] = (),
    activities: "frozenset[ActivityClass] | set[ActivityClass]" = frozenset(),
    maximum_risk: str = "read_only",
    expires_at: str | None = None,
    reference: str = "",
    environment: EnvironmentType = EnvironmentType.LAB,
    now: datetime | None = None,
) -> AuthorizedSecurityScope:
    """Build a scope for an owned lab — a convenience for the operator command
    surface and for tests. It grants nothing its arguments do not name."""
    policy = ScopePolicy(
        scope_id=scope_id, name=scope_id,
        mode=ENVIRONMENT_AUTHORITY_MODE[environment],
        targets=frozenset(targets), cidrs=tuple(cidrs),
        expires_at=expires_at, created_by="operator", notes=reference[:200],
    )
    return AuthorizedSecurityScope(
        scope_id=scope_id, environment_type=environment, policy=policy,
        permitted_activity_classes=frozenset(activities),
        maximum_risk=maximum_risk, issued_at=_iso(now or _now()),
        reference=reference,
    )


def expire(scope: AuthorizedSecurityScope, at: str) -> AuthorizedSecurityScope:
    """Return *scope* with its expiry moved to *at*. Rewrites the reused policy's
    expiry, never a second field, so the one clock stays the one clock."""
    return replace(scope, policy=replace(scope.policy, expires_at=at))
