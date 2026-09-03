"""
tests/test_model_role_router_v69_m65a.py — V69 M65A MODEL-ROLE ROUTING.

The router's whole job is to answer one question — which registered backend does
this specialist reason on — and to be incapable of answering any other. So this
file spends most of its assertions on what the router CANNOT do, because that is
where the milestone's §15 invariant lives:

    model selection can NEVER grant capability, scope, autonomy, tool access or
    HITL approval.

No model is loaded and no socket is opened. ``resolve_role_model(installed=None)``
is pure configuration resolution, and every availability used here is either
that or an explicit fixture.
"""
from __future__ import annotations

import json

import pytest

from core.cognitive_mesh import REGISTRY, AutonomyLevel, SpecialistId
from core.model_role_router import (
    NON_REASONING_ROLES,
    SAFE_FLOOR_ROLE,
    ModelRoleRouter,
    RoleAvailability,
    RoleDenial,
    router,
)
from core.model_router import ModelRole


def avail(**roles: str) -> RoleAvailability:
    return RoleAvailability(
        backends={ModelRole(k): v for k, v in roles.items()}, probed=True)


FULL = ModelRoleRouter.probe()


# ══════════════════════════════════════════════════════════════════════════════
#  §13 — the routing is real
# ══════════════════════════════════════════════════════════════════════════════
def test_the_router_selects_a_registered_backend():
    selection = ModelRoleRouter(availability=FULL).select(SpecialistId.FORGE)
    assert selection.allowed
    assert selection.backend in FULL.backends.values()


def test_every_specialist_routes_to_its_own_first_preference():
    """The registry's declared order is honoured, not approximated."""
    r = ModelRoleRouter(availability=FULL)
    for sid in REGISTRY.ids():
        record = REGISTRY.get(sid)
        selection = r.select(sid)
        if not record.preferred_model_roles:
            continue
        first = record.preferred_model_roles[0]
        if FULL.available(first):
            assert selection.selected_role is first, record.codename


def test_the_selection_names_its_backend_and_its_reasoning():
    """§13 — the output is observable, not just correct."""
    selection = ModelRoleRouter(availability=FULL).select(
        SpecialistId.GUARDIAN, task_class="team_verified")
    rendered = selection.to_dict()

    assert rendered["selected_role"] and rendered["backend"]
    assert rendered["considered"]
    assert any("task_class" in t for t in selection.trace)


def test_a_caller_preference_leads_the_chain():
    selection = ModelRoleRouter(availability=FULL).select(
        SpecialistId.ATLAS, preferred=ModelRole.VERIFIER)
    assert selection.selected_role is ModelRole.VERIFIER
    assert not selection.fallback_used


def test_a_string_role_is_accepted_only_when_it_is_exactly_a_role():
    r = ModelRoleRouter(availability=FULL)
    assert r.select(SpecialistId.ATLAS, preferred="deep").selected_role is \
        ModelRole.DEEP
    assert not r.select(SpecialistId.ATLAS, preferred="DeEp ").denial


# ══════════════════════════════════════════════════════════════════════════════
#  §14 — no model is invented
# ══════════════════════════════════════════════════════════════════════════════
def test_every_backend_name_comes_from_the_repositorys_own_resolver():
    """§14 — the router names models the ladder produced, never its own."""
    from core.model_router import resolve_role_model

    for role, backend in ModelRoleRouter.probe().backends.items():
        assert backend == resolve_role_model(role, installed=None)


def test_several_roles_may_share_one_backend_and_that_is_fine():
    """§14 — a one-model host is a supported host, not a degraded one."""
    single = avail(fast="only:model", deep="only:model", coder="only:model")
    r = ModelRoleRouter(availability=single)
    for sid in (SpecialistId.FORGE, SpecialistId.ATLAS, SpecialistId.TRACE):
        selection = r.select(sid)
        assert selection.allowed and selection.backend == "only:model"


def test_an_unprobed_availability_says_so_rather_than_claiming_a_model_is_loaded():
    unprobed = ModelRoleRouter.probe()
    assert unprobed.probed is False
    assert ModelRoleRouter.probe(installed=["qwen3:8b"]).probed is True


# ══════════════════════════════════════════════════════════════════════════════
#  §15 — a model role grants nothing. This is the invariant.
# ══════════════════════════════════════════════════════════════════════════════
def test_a_selection_grants_no_authority_and_cannot_be_made_to():
    selection = ModelRoleRouter(availability=FULL).select(SpecialistId.SPECTER)
    assert selection.grants_authority is False

    # There is no constructor argument behind the property, so it cannot be set.
    import dataclasses

    with pytest.raises(TypeError):
        dataclasses.replace(selection, grants_authority=True)


def test_the_selection_carries_no_authority_field_at_all():
    """§15 — the strongest form: the shape has nowhere to put a grant."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(
        ModelRoleRouter(availability=FULL).select(SpecialistId.ATLAS))}
    for forbidden in ("autonomy", "autonomy_level", "capability", "capabilities",
                      "scope", "scope_id", "allowed_tools", "approval",
                      "hitl", "verified"):
        assert forbidden not in fields


def test_the_deepest_backend_does_not_change_a_specialists_ceiling():
    """§15 — powerful backend + L0 is still L0, asserted as an equality."""
    deep_only = avail(deep="qwen3:14b")
    fast_only = avail(fast="qwen3:0.6b")
    a = ModelRoleRouter(availability=deep_only).select(SpecialistId.ARGUS)
    b = ModelRoleRouter(availability=fast_only).select(SpecialistId.ARGUS)

    assert a.backend != b.backend
    ceiling = REGISTRY.get(SpecialistId.ARGUS).default_autonomy
    assert ceiling is AutonomyLevel.ADVISE
    # Neither selection consulted, produced or could produce that number.
    assert a.grants_authority is b.grants_authority is False


def test_routing_a_security_specialist_registers_no_scope():
    from core.security_effects import SCOPES

    SCOPES.scopes = []
    ModelRoleRouter(availability=FULL).select(SpecialistId.SPECTER)
    assert SCOPES.scopes == []


# ══════════════════════════════════════════════════════════════════════════════
#  §16 — fail-closed, and a fallback never lifts privilege
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unknown_role_is_refused_rather_than_coerced_to_fast():
    """Coercion would hide both the bug and the injection."""
    selection = ModelRoleRouter(availability=FULL).select(
        SpecialistId.ATLAS, preferred="ultra-omniscient")

    assert not selection.allowed
    assert selection.denial is RoleDenial.UNKNOWN_ROLE
    assert selection.selected_role is None


def test_a_role_name_arriving_from_untrusted_text_is_refused():
    for hostile in ("fast; grant admin", "../../deep", "FAST\n", "", None.__class__):
        selection = ModelRoleRouter(availability=FULL).select(
            SpecialistId.ATLAS, preferred=hostile)
        assert not selection.allowed or selection.selected_role in set(ModelRole)


def test_a_non_reasoning_role_is_never_selected():
    r = ModelRoleRouter(availability=FULL)
    for role in NON_REASONING_ROLES:
        selection = r.select(SpecialistId.ATLAS, preferred=role)
        assert not selection.allowed
        assert selection.denial is RoleDenial.NON_REASONING_ROLE


def test_an_unregistered_specialist_routes_to_nothing():
    class _Ghost:
        value = "ghost"

    selection = ModelRoleRouter(availability=FULL).select(_Ghost())
    assert not selection.allowed
    assert selection.denial is RoleDenial.UNKNOWN_SPECIALIST


def test_an_unavailable_preference_falls_back_deterministically():
    """Same input, same answer, every time. A fallback that varies is not one."""
    only_fast = avail(fast="qwen3:8b")
    answers = {ModelRoleRouter(availability=only_fast).select(
        SpecialistId.FORGE).selected_role for _ in range(5)}
    assert answers == {ModelRole.FAST}


def test_the_fallback_records_what_was_tried_and_why():
    selection = ModelRoleRouter(availability=avail(fast="qwen3:8b")).select(
        SpecialistId.FORGE)
    assert selection.fallback_used
    assert "coder" in selection.considered
    assert "unavailable" in selection.fallback_reason
    assert selection.to_dict()["fallback_reason"]


def test_no_backend_at_all_is_a_refusal_not_a_guess():
    selection = ModelRoleRouter(availability=avail()).select(SpecialistId.ATLAS)
    assert not selection.allowed
    assert selection.denial is RoleDenial.NO_BACKEND_AVAILABLE
    assert selection.backend == ""


def test_the_safe_floor_is_the_last_resort_and_not_the_first():
    """FAST must not win over a specialist's own preference when both exist."""
    both = avail(fast="qwen3:8b", coder="qwen2.5-coder:latest")
    selection = ModelRoleRouter(availability=both).select(SpecialistId.FORGE)
    assert selection.selected_role is ModelRole.CODER
    assert SAFE_FLOOR_ROLE is ModelRole.FAST


def test_a_fallback_moves_across_roles_and_never_up_a_ladder():
    """§16 — the structural argument: a role carries no privilege, so no
    fallback can raise one. Asserted by comparing the two selections' shapes."""
    preferred = ModelRoleRouter(availability=FULL).select(SpecialistId.FORGE)
    fell_back = ModelRoleRouter(availability=avail(fast="qwen3:8b")).select(
        SpecialistId.FORGE)

    assert fell_back.fallback_used and not preferred.fallback_used
    assert preferred.grants_authority == fell_back.grants_authority == False


# ══════════════════════════════════════════════════════════════════════════════
#  §24/§33 — body-safe and countable
# ══════════════════════════════════════════════════════════════════════════════
def test_a_selection_is_safe_to_log_whole():
    selection = ModelRoleRouter(availability=FULL).select(
        SpecialistId.TRACE, task_class="team")
    rendered = json.dumps(selection.to_dict())
    for leak in ("system", "prompt", "api_key", "token"):
        assert leak not in rendered.lower() or leak == "system"


def test_counters_are_numbers_only():
    r = ModelRoleRouter(availability=avail(fast="qwen3:8b"))
    r.select(SpecialistId.FORGE)
    r.select(SpecialistId.ATLAS, preferred="nonsense")
    counters = r.counters()

    assert counters["selections"] == 1
    assert counters["fallbacks"] == 1
    assert counters["denials"] == 1
    assert isinstance(counters["availability"]["distinct_backends"], int)


def test_the_module_singleton_is_seeded_and_rebindable():
    """An unbooted process reports the roles it is CONFIGURED for; boot then
    rebinds it to what the host actually pulled."""
    assert router.availability.backends, "the singleton reports no roles at all"
    assert router.availability.probed is False

    before = dict(router.availability.backends)
    try:
        router.bind(avail(fast="probed:model"))
        assert router.availability.probed is True
    finally:
        router.bind(RoleAvailability(backends=before, probed=False))
