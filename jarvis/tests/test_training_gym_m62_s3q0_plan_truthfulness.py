"""V69 M62 S3Q.0 — does the plan an operator approves describe what will execute?

WHAT IS BEING QUALIFIED
-----------------------
``EVAL:<plan-hash>`` is the only thing that authorises a live evaluation, so the plan the
digest covers has to be a truthful description of the run. Independent review suspected
it was not, in four separate places, and every suspicion is reproduced here from source
before it is fixed:

  * ``task_pack_hash`` digested the dataset manifest and the split counts, not the pack;
  * ``hidden_target_store_hash`` digested the dataset manifest, not the answer key;
  * ``order_assignment_hash`` digested the policy name and the seed, while the runner
    derived the real assignment from every task hash;
  * ``performs_inference`` was ``false`` on the exact object ``--execute`` hands to a
    function that loads weights.

The first three meant one confirmation authorised a family of possible packs. The fourth
meant the effect list an operator read before approving was wrong about the only effect
that matters.

WHAT THIS FILE DOES NOT TOUCH
-----------------------------
No model, no ``eval-v4``, and no confirmation string for any real plan. The plans here
are built over the S3Q.0 synthetic corpus, and the only tokens that appear are the ones
those synthetic plans generate.
"""
from __future__ import annotations

import pytest

from training_gym.evaluation.plan import (
    CONFIRMATION_PREFIX,
    EVALUATION_PLAN_SCHEMA_VERSION,
    EvaluationPlanError,
)
from training_gym.evaluation.preflight import (
    CONFIRMATION_LITERAL_RE,
    PackIdentity,
    assert_token_silent,
    confirmation_literals,
    derive_pack_identity,
    prepare_pack_identity,
    preflight_report,
)

import _s3q0_synthetic as S


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s3q0plan")
    S.build(root)
    return root


@pytest.fixture(scope="module")
def world(dataset_root):
    config = S.make_config()
    baseline = S.make_baseline()
    adapter = S.make_adapter(baseline)
    identity = S.pack_identity(dataset_root, config)
    return config, baseline, adapter, identity


# ══════════════════════════════════════════════════════════════════════════════
#  The identity is the pack's own, not a proxy for it
# ══════════════════════════════════════════════════════════════════════════════
def test_the_bound_pack_hash_is_the_pack_s_own_digest(dataset_root, world):
    """Not a digest of the manifest and the counts — the pack's ``pack_hash()``."""
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

    config, _baseline, _adapter, identity = world
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation)
    assert identity.pack_hash == built.pack.pack_hash()
    assert identity.hidden_target_store_hash == built.targets.store_hash()


def test_the_bound_order_assignment_is_the_runner_s_own(dataset_root, world):
    """The per-task assignment digest, derived from every task hash and the seed."""
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.runner import order_assignment_hash

    config, _baseline, _adapter, identity = world
    built = build_task_pack_from_dataset(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation)
    assert identity.order_assignment_hash == order_assignment_hash(built.pack,
                                                                  seed=config.seed)


def test_the_superseded_proxies_are_not_what_is_bound(dataset_root, world):
    """The three digests the pre-S3Q.0 planner computed are reproduced and rejected.

    Non-vacuity: without this, "the plan binds the pack hash" would still pass if the
    two formulas happened to agree. They must not, and this proves they do not.
    """
    import hashlib
    import json

    from training_gym.datasets.manifests import load_manifest
    from training_gym.evaluation.runner import ORDER_POLICY_BALANCED

    config, _baseline, _adapter, identity = world
    manifest = load_manifest(root=dataset_root, dataset_id=config.dataset.dataset_id,
                             dataset_version=config.dataset.dataset_version)
    counts = {split: len(manifest.candidate_ids_in(split))
              for split in config.splits.splits}

    legacy_pack = hashlib.sha256(json.dumps(
        {"dataset": manifest.manifest_hash(),
         "counts": {k.value: v for k, v in counts.items()}},
        sort_keys=True).encode("utf-8")).hexdigest()
    legacy_store = hashlib.sha256(
        manifest.manifest_hash().encode("utf-8")).hexdigest()
    legacy_order = hashlib.sha256(
        f"{ORDER_POLICY_BALANCED}:{config.seed}".encode()).hexdigest()

    assert identity.pack_hash != legacy_pack
    assert identity.hidden_target_store_hash != legacy_store
    assert identity.order_assignment_hash != legacy_order


def test_a_pack_identity_is_reproducible_from_the_same_material(dataset_root, world):
    config, _baseline, _adapter, identity = world
    again = S.pack_identity(dataset_root, config)
    assert again.identity_hash() == identity.identity_hash()


def test_a_different_seed_moves_the_order_assignment_and_nothing_else(dataset_root):
    """The seed decides who answers first and decides nothing about the material."""
    config = S.make_config(seed=11, generation={"seed": 11})
    other = S.make_config(seed=12, generation={"seed": 12})
    first = S.pack_identity(dataset_root, config)
    second = S.pack_identity(dataset_root, other)
    assert first.order_assignment_hash != second.order_assignment_hash
    assert first.pack_hash == second.pack_hash
    assert first.hidden_target_store_hash == second.hidden_target_store_hash


# ══════════════════════════════════════════════════════════════════════════════
#  An executable plan must be able to name what it authorises
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field", ["task_pack_hash", "hidden_target_store_hash",
                                   "order_assignment_hash"])
def test_an_unblocked_plan_may_not_carry_a_placeholder_identity(world, field):
    config, baseline, adapter, identity = world
    with pytest.raises(EvaluationPlanError, match="EXACT material"):
        S.make_plan(config, baseline, adapter, identity, **{field: ""})


@pytest.mark.parametrize("bogus", ["", "not-a-digest", "abc", "z" * 64])
def test_a_non_digest_identity_is_refused(world, bogus):
    config, baseline, adapter, identity = world
    with pytest.raises(EvaluationPlanError):
        S.make_plan(config, baseline, adapter, identity, task_pack_hash=bogus)


def test_a_blocked_plan_may_state_that_it_cannot_name_the_pack(world):
    """MISSING is not FALSE: a plan that cannot bind says so and is unexecutable."""
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity, task_pack_hash="",
                       hidden_target_store_hash="", order_assignment_hash="",
                       blockers=("the exact task pack could not be prepared",))
    assert not plan.is_executable
    assert plan.blockers


# ══════════════════════════════════════════════════════════════════════════════
#  The self-description
# ══════════════════════════════════════════════════════════════════════════════
def test_the_plan_schema_version_moved_so_old_confirmations_cannot_survive():
    """The fields mean something different now; a version-1 token approved a proxy."""
    assert EVALUATION_PLAN_SCHEMA_VERSION == "m62.evaluation_plan.2"


def test_the_effect_list_still_refuses_every_impossible_claim(world):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    effects = plan.expected_effects()
    for key in ("contacts_the_network", "downloads_anything",
                "installs_dependencies", "executes_model_proposed_tool_calls",
                "promotes_a_model", "activates_a_model", "writes_the_model_registry"):
        assert effects[key] is False, key
    assert effects["strongest_possible_verdict"] == "eligible_for_human_review"


def test_a_plan_may_not_claim_an_impossible_effect(world):
    config, baseline, adapter, identity = world
    with pytest.raises(EvaluationPlanError, match="may not be true"):
        S.make_plan(config, baseline, adapter, identity, promotes_model=True)


def test_a_plan_that_would_run_a_model_may_say_so(world):
    """``performs_inference`` is the one flag that may legitimately be true."""
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity, performs_inference=True)
    assert plan.performs_inference is True
    assert plan.expected_effects()["runs_a_model"] is True
    assert plan.contacts_network is False
    assert plan.downloads_model is False
    assert plan.promotes_model is False


def test_inference_truthfulness_moves_the_plan_hash(world):
    """Correcting the claim changes the digest, and that is the correct outcome.

    A hash preserved across a truthfulness fix would mean a token issued against the
    misleading description still authorised the run.
    """
    config, baseline, adapter, identity = world
    quiet = S.make_plan(config, baseline, adapter, identity, performs_inference=False)
    honest = S.make_plan(config, baseline, adapter, identity, performs_inference=True)
    assert quiet.plan_hash() != honest.plan_hash()
    assert quiet.confirmation_token() != honest.confirmation_token()


def test_the_production_planner_declares_inference():
    """Read from source: the object handed to ``--execute`` says it runs a model."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts"
              / "evaluate_adapter.py").read_text(encoding="utf-8")
    assert "performs_inference=True" in source
    # And the superseded proxy formulas are gone from the planner entirely.
    assert 'hashlib.sha256(\n            manifest.manifest_hash()' not in source
    assert f'"{{ORDER_POLICY_BALANCED}}:{{config.seed}}"' not in source


# ══════════════════════════════════════════════════════════════════════════════
#  Token-emitting versus token-silent surfaces
# ══════════════════════════════════════════════════════════════════════════════
def test_the_confirmation_detector_needs_the_full_digest():
    """The bare prefix is a schema constant and a documentation string, not a token."""
    assert confirmation_literals({"note": f"the form is {CONFIRMATION_PREFIX}<hash>"}) \
        == ()
    assert confirmation_literals("EVAL:" + "a" * 12) == ()
    assert confirmation_literals("EVAL:" + "a" * 64) == ("EVAL:" + "a" * 64,)


def test_to_record_is_a_token_emitting_surface(world):
    """Documented, not fixed: ``--print-plan`` prints this and it carries the token."""
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    record = plan.to_record()
    assert record["confirmation_required"] == plan.confirmation_token()
    assert confirmation_literals(record) == (plan.confirmation_token(),)


def test_confirmation_token_is_a_token_emitting_surface(world):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    assert CONFIRMATION_LITERAL_RE.fullmatch(plan.confirmation_token())


def test_the_live_preflight_publishes_the_plan_hash_and_not_the_token(world):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    payload = preflight_report(plan=plan, identity=identity, dependency_ready=True,
                               generation_policy=config.generation)
    assert payload["plan_hash"] == plan.plan_hash()
    assert confirmation_literals(payload) == ()
    assert "confirmation_required" not in payload
    assert "confirmation_token" not in payload
    assert payload["token_silent"] is True


def test_the_preflight_carries_every_exact_identity(world):
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    payload = preflight_report(plan=plan, identity=identity, dependency_ready=True,
                               generation_policy=config.generation)
    assert payload["task_pack_hash"] == identity.pack_hash
    assert payload["hidden_target_store_hash"] == identity.hidden_target_store_hash
    assert payload["order_assignment_hash"] == identity.order_assignment_hash
    assert payload["task_count"] == identity.task_count
    assert payload["generation_policy_hash"] == config.generation.policy_hash()


def test_the_preflight_restates_that_the_timeout_is_not_enforced(world):
    """D33 stays OPEN, and every surface that reports the ceiling says so."""
    config, baseline, adapter, identity = world
    plan = S.make_plan(config, baseline, adapter, identity)
    payload = preflight_report(plan=plan, identity=identity, dependency_ready=True,
                               generation_policy=config.generation)
    assert payload["configured_timeout_s"] == config.generation.timeout_s
    assert payload["timeout_enforced"] is False


def test_token_silence_is_asserted_and_not_merely_intended():
    """The assertion refuses a payload that materialised a token, whatever built it."""
    from training_gym.evaluation.preflight import PreflightError

    with pytest.raises(PreflightError, match="token-silent"):
        assert_token_silent({"leaked": "EVAL:" + "b" * 64})


def test_token_silence_is_hygiene_and_the_module_says_so():
    """No test and no docstring may claim the token is secret or unpredictable."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "training_gym" / "evaluation"
              / "preflight.py").read_text(encoding="utf-8")
    assert "not cryptography" in source.lower() or "NOT CRYPTOGRAPHY" in source
    assert "deterministically derivable" in source


# ══════════════════════════════════════════════════════════════════════════════
#  The identity type cannot carry a body
# ══════════════════════════════════════════════════════════════════════════════
def test_a_pack_identity_has_no_field_that_could_hold_material():
    fields = set(PackIdentity.__dataclass_fields__)
    for forbidden in ("tasks", "prompts", "targets", "records", "user_prompt",
                      "system_prompt", "target_text", "pack", "store"):
        assert forbidden not in fields, forbidden


def test_preparing_an_identity_returns_no_pack_and_no_store(dataset_root):
    config = S.make_config()
    identity = prepare_pack_identity(
        root=dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=1, seed=config.seed)
    assert isinstance(identity, PackIdentity)
    assert not hasattr(identity, "pack")
    assert not hasattr(identity, "targets")


def test_derive_refuses_anything_that_is_not_a_built_pack():
    from training_gym.evaluation.preflight import PreflightError

    with pytest.raises(PreflightError, match="BuiltPack"):
        derive_pack_identity({"pack": "not one"}, seed=11)
