"""
core/model_role_router.py — V69 M65A: model-role routing that actually decides.

M64 gave every specialist a ``preferred_model_roles`` tuple and said so in its
own docstring: *"which model runs -> core.model_router.ModelRole (advisory
only)"*. It was true. The field reached ``MeshRoute.preferred_model_role``, was
copied into ``MeshTrace.model_roles`` for telemetry, and nothing anywhere read
it to choose a backend. A preference nothing consults is documentation.

This module makes it executable and observable, and does so WITHOUT inventing a
second model registry. Every concrete model name still comes from
``core.model_router.resolve_role_model`` — the one precedence ladder
(env override -> central config -> hardware hint -> installed-compatible ->
central default) that every live consumer already resolves through.

WHAT THIS DECIDES, AND WHAT IT CANNOT
=====================================
It decides ONE thing: which :class:`~core.model_router.ModelRole`, and therefore
which registered backend, a specialist's reasoning runs on. It answers no other
question, and :class:`RoleSelection` has no field that could.

    a role is not a capability   -- capability is SpecialistRecord's
    a role is not a scope        -- scope is AuthorizedSecurityScope's
    a role is not an autonomy    -- autonomy is AutonomyLevel's
    a role is not a tool grant   -- tools are ToolBroker's and ToolExecutor's
    a role is not an approval    -- approval is a human's

:attr:`RoleSelection.grants_authority` is a property that returns ``False`` and
takes no argument, deliberately mirroring
``VerifierVerdict.grants_authority``: the question has one answer, and no
constructor argument can change it. §15 of the M65A directive is therefore not
an assertion made in prose about this module; it is a property of its types.

FAIL-CLOSED, AND FALLBACK NEVER LIFTS
=====================================
Three failure shapes, three deterministic answers (§16):

  * **Unknown role.** A role string outside ``ModelRole`` is refused. It is not
    coerced to FAST, because a request naming a role that does not exist is a
    bug or an injection, and answering it with the general-purpose backend would
    hide both.
  * **Unavailable role.** The declared preferences are walked in the record's own
    order, then the configured safe floor. Every step is recorded.
  * **Nothing available.** Refused. There is no "run it anyway on whatever is
    loaded" branch.

A fallback can only move ACROSS roles. Since a role carries no privilege, no
fallback can raise one — which is why the escalation test in this milestone can
be written as an equality on autonomy before and after a fallback, rather than
as a hopeful comment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core.cognitive_mesh import REGISTRY, SpecialistId
from core.model_router import ModelRole, resolve_role_model

logger = logging.getLogger("jarvis.model_role_router")

#: The role used when a specialist declares no preference at all. FAST is the
#: repository's own general-purpose role and the one every host that runs JARVIS
#: at all can serve.
SAFE_FLOOR_ROLE: ModelRole = ModelRole.FAST

#: Roles that are never a reasoning backend for a specialist. EMBEDDING produces
#: vectors, not text; CLOUD leaves the host and is opt-in through
#: ``model_router.cloud_enabled()`` rather than through a routing preference.
NON_REASONING_ROLES: frozenset[ModelRole] = frozenset({
    ModelRole.EMBEDDING, ModelRole.CLOUD,
})

MAX_TRACE = 8
MAX_REASON = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RoleDenial(str, Enum):
    """Why a selection failed. Every member is a refusal, never a downgrade."""

    UNKNOWN_ROLE = "unknown_role"
    NON_REASONING_ROLE = "non_reasoning_role"
    NO_BACKEND_AVAILABLE = "no_backend_available"
    UNKNOWN_SPECIALIST = "unknown_specialist"


@dataclass(frozen=True)
class RoleAvailability:
    """Which roles this host can actually serve, and on what.

    Built by :meth:`ModelRoleRouter.probe` from the models a backend reports as
    installed, or handed in directly by a caller that already knows (the live
    turn does — ``LLM`` resolved its models at boot). ``None`` for *installed*
    means "config resolution only": every role resolves to its configured model
    and is considered available, which is the correct answer for a host that has
    not been probed rather than a claim that the model is loaded.
    """

    backends: "dict[ModelRole, str]" = field(default_factory=dict)
    probed: bool = False

    def available(self, role: ModelRole) -> bool:
        return bool(self.backends.get(role))

    def backend(self, role: ModelRole) -> str:
        return self.backends.get(role, "")

    def to_dict(self) -> dict:
        return {
            "probed": self.probed,
            "roles": sorted(r.value for r in self.backends),
            "backends": {r.value: b for r, b in sorted(
                self.backends.items(), key=lambda kv: kv[0].value)},
            "distinct_backends": len({b for b in self.backends.values() if b}),
        }


@dataclass(frozen=True)
class RoleSelection:
    """One routing decision, and everything an operator needs to audit it.

    Body-safe by construction: it names roles, backends and reasons. It carries
    no prompt, no completion and no model internals, so it can be logged whole.
    """

    specialist_id: SpecialistId
    requested_role: "ModelRole | None"
    selected_role: "ModelRole | None"
    backend: str
    allowed: bool
    fallback_used: bool = False
    fallback_reason: str = ""
    denial: "RoleDenial | None" = None
    considered: "tuple[str, ...]" = ()
    trace: "tuple[str, ...]" = ()
    timestamp: str = field(default_factory=_now_iso)

    @property
    def grants_authority(self) -> bool:
        """Always ``False``.

        Choosing a more capable model does not choose a more capable specialist.
        There is no constructor argument that makes this True, which is what
        lets §15 be a type property rather than a promise.
        """
        return False

    def to_dict(self) -> dict:
        return {
            "specialist_id": self.specialist_id.value,
            "requested_role": self.requested_role.value if self.requested_role else None,
            "selected_role": self.selected_role.value if self.selected_role else None,
            "backend": self.backend,
            "allowed": self.allowed,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason[:MAX_REASON],
            "denial": self.denial.value if self.denial else None,
            "considered": list(self.considered),
            "trace": list(self.trace),
            "grants_authority": self.grants_authority,
            "timestamp": self.timestamp,
        }


def _coerce_role(value) -> "ModelRole | None":
    """A ModelRole, or None. Never a guess.

    Accepts a ``ModelRole`` or its exact string value. Anything else — a role
    name a model emitted, a typo, a string arriving from tool output — is None,
    and every caller treats None as a refusal.
    """
    if isinstance(value, ModelRole):
        return value
    if isinstance(value, str):
        try:
            return ModelRole(value.strip().lower())
        except ValueError:
            return None
    return None


class ModelRoleRouter:
    """Selects the backend a specialist reasons on. Selects nothing else.

    Stateless apart from bounded counters, so two callers cannot disagree about
    a decision and there is no cache in which a stale availability could outlive
    the host it described.
    """

    def __init__(self, *, availability: RoleAvailability | None = None,
                 floor: ModelRole = SAFE_FLOOR_ROLE) -> None:
        self._availability = availability or RoleAvailability()
        self._floor = floor
        self.selections = 0
        self.fallbacks = 0
        self.denials = 0

    # ── availability ────────────────────────────────────────────────────────
    @staticmethod
    def probe(installed=None, *, hw_recommendation: str | None = None
              ) -> RoleAvailability:
        """Resolve every reasoning role against what is installed.

        Delegates to ``resolve_role_model`` per role, so the operator's
        ``JARVIS_MODEL_*`` overrides win here exactly as they win everywhere
        else. Nothing is invented: a role resolves to a name that ladder
        produced, or it does not appear.
        """
        backends: dict[ModelRole, str] = {}
        for role in ModelRole:
            if role in NON_REASONING_ROLES:
                continue
            try:
                name = resolve_role_model(role, installed=installed,
                                          hw_recommendation=hw_recommendation)
            except Exception as exc:  # noqa: BLE001 — a role that will not
                # resolve is simply absent; it is never a crash and never a
                # silent substitution.
                logger.debug("MODEL_ROLE: %s did not resolve (%s)", role.value, exc)
                continue
            if name:
                backends[role] = name
        return RoleAvailability(backends=backends, probed=installed is not None)

    @property
    def availability(self) -> RoleAvailability:
        return self._availability

    def bind(self, availability: RoleAvailability) -> None:
        """Rebind availability (boot, or after the operator pulls a model)."""
        self._availability = availability

    # ── the decision ────────────────────────────────────────────────────────
    def select(self, specialist_id: SpecialistId, *,
               preferred: "ModelRole | str | None" = None,
               task_class: str = "",
               availability: RoleAvailability | None = None) -> RoleSelection:
        """Choose the role and backend for one specialist execution.

        Order (§17's least-machinery principle applied to models): the caller's
        explicit preference, then the specialist's own declared preferences in
        registry order, then the safe floor. The first AVAILABLE step wins and
        every step is recorded in ``trace`` whether it won or not.
        """
        avail = availability or self._availability
        trace: list[str] = []
        considered: list[str] = []

        try:
            record = REGISTRY.get(specialist_id)
        except Exception:  # noqa: BLE001 — an unregistered specialist routes to
            # nothing. It cannot be given the general-purpose backend, because a
            # caller naming a specialist that does not exist has a bug the
            # fallback would hide.
            self.denials += 1
            return RoleSelection(
                specialist_id=specialist_id, requested_role=None,
                selected_role=None, backend="", allowed=False,
                denial=RoleDenial.UNKNOWN_SPECIALIST,
                fallback_reason=f"'{specialist_id}' is not in the specialist registry",
                trace=("registry lookup failed",))

        requested = _coerce_role(preferred) if preferred is not None else None
        if preferred is not None and requested is None:
            # §16 — an unknown role is refused, not coerced.
            self.denials += 1
            return RoleSelection(
                specialist_id=specialist_id, requested_role=None,
                selected_role=None, backend="", allowed=False,
                denial=RoleDenial.UNKNOWN_ROLE,
                fallback_reason=(f"'{preferred}' is not a registered ModelRole; "
                                 f"refused rather than defaulted"),
                trace=(f"requested={preferred!r} unrecognised",))

        if requested is not None and requested in NON_REASONING_ROLES:
            self.denials += 1
            return RoleSelection(
                specialist_id=specialist_id, requested_role=requested,
                selected_role=None, backend="", allowed=False,
                denial=RoleDenial.NON_REASONING_ROLE,
                fallback_reason=(f"role '{requested.value}' is not a reasoning "
                                 f"backend and is never selected for a specialist"),
                trace=(f"requested={requested.value} is non-reasoning",))

        # The ordered candidate chain. Deduplicated while preserving order, so a
        # specialist whose first preference equals the caller's is not walked
        # twice and the trace stays legible.
        chain: list[ModelRole] = []
        if requested is not None:
            chain.append(requested)
            trace.append(f"caller preference: {requested.value}")
        for role in record.preferred_model_roles:
            if role not in chain and role not in NON_REASONING_ROLES:
                chain.append(role)
        if record.preferred_model_roles:
            trace.append("registry preferences: " + ",".join(
                r.value for r in record.preferred_model_roles))
        if self._floor not in chain:
            chain.append(self._floor)
            trace.append(f"safe floor: {self._floor.value}")
        if task_class:
            trace.append(f"task_class={task_class[:40]}")

        for index, role in enumerate(chain):
            considered.append(role.value)
            if not avail.available(role):
                trace.append(f"{role.value}: no backend")
                continue
            backend = avail.backend(role)
            fell_back = index > 0
            reason = ""
            if fell_back:
                missing = ",".join(considered[:index])
                reason = (f"preferred role(s) {missing} unavailable on this host; "
                          f"fell back to {role.value}")
                self.fallbacks += 1
            trace.append(f"{role.value}: selected on '{backend}'")
            self.selections += 1
            return RoleSelection(
                specialist_id=specialist_id,
                requested_role=requested or (
                    record.preferred_model_roles[0]
                    if record.preferred_model_roles else self._floor),
                selected_role=role, backend=backend, allowed=True,
                fallback_used=fell_back, fallback_reason=reason,
                considered=tuple(considered), trace=tuple(trace[:MAX_TRACE]))

        # §16 — nothing available is a refusal. There is no "use whatever is
        # loaded" branch, because a specialist running on an unknown backend is
        # exactly the unobservable state this router exists to remove.
        self.denials += 1
        trace.append("no role in the chain has a backend")
        return RoleSelection(
            specialist_id=specialist_id, requested_role=requested,
            selected_role=None, backend="", allowed=False,
            denial=RoleDenial.NO_BACKEND_AVAILABLE,
            fallback_reason=("no configured backend for any role this specialist "
                             "may use; the execution is refused rather than run "
                             "on an unidentified model"),
            considered=tuple(considered), trace=tuple(trace[:MAX_TRACE]))

    def counters(self) -> dict:
        """Body-safe counters (§33). Numbers only."""
        return {
            "selections": self.selections,
            "fallbacks": self.fallbacks,
            "denials": self.denials,
            "availability": self._availability.to_dict(),
        }


#: The module singleton every runtime component routes through. A second
#: instance is constructible in a test and grants nothing, exactly as
#: ``SpecialistRegistry`` intends: enforcement reads this one.
#:
#: Seeded with CONFIG-ONLY availability (``installed=None``), which touches no
#: socket and pulls nothing: every role resolves through the same env-override
#: ladder the rest of the runtime uses, and ``probed`` stays False to say so.
#: Boot replaces it via :meth:`ModelRoleRouter.bind` with a probed availability
#: once the host's pulled models are known. Seeding rather than starting empty
#: matters for the doctor: an unbooted process should report the roles it is
#: configured for, not claim that none resolve.
router = ModelRoleRouter(availability=ModelRoleRouter.probe())


__all__ = [
    "NON_REASONING_ROLES", "SAFE_FLOOR_ROLE", "ModelRoleRouter", "RoleAvailability",
    "RoleDenial", "RoleSelection", "router",
]
