"""V69 M67A — bounded recursive distillation (§12).

WHAT MUST BE TRUE OF THE LOOP
-----------------------------
An unbounded critique/repair loop does not converge on truth; it converges on whatever the critic
cannot see. §12 caps the depth and this suite proves the three properties that make the cap
meaningful rather than decorative:

  * **it terminates**, by one of exactly three conditions, and which one is recorded;
  * **the cap is not an acceptance** — a record still carrying material findings at the cap is
    ``NEEDS_HUMAN_REVIEW`` with ``RECURSION_EXHAUSTED``, never ACCEPT;
  * **the chain is auditable** — every pass names its parent's artifact hash, so what a record was
    repaired FROM is recoverable. A repair nobody can diff is a repair nobody can review.

AND THE PROPERTY THAT MATTERS MOST
----------------------------------
:func:`test_repair_can_only_remove_or_weaken` is the load-bearing one. If a repair pass could ADD
a field, the loop would launder a fabrication: the critic says "this constraint is unsupported",
the repairer writes a better-supported-looking constraint, the critic passes it, and an invented
field enters the corpus with a clean audit trail attached. Every permitted action is subtractive,
and the test asserts that over the whole fixture corpus rather than trusting the action list.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
PACKAGE_ROOT = _HERE.parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from reasoning_distillation.config import RecursionConfig, default_config  # noqa: E402
from reasoning_distillation.critic import CriticError, Severity, critique  # noqa: E402
from reasoning_distillation.distiller import (  # noqa: E402
    REPAIR_ACTIONS,
    DistillationRefused,
    RecursionTrace,
    distil,
    extract,
    repair,
)
from reasoning_distillation.models import Disposition, RejectReason  # noqa: E402
from reasoning_distillation.quality import disposition  # noqa: E402
from test_reasoning_distillation_m67a import (  # noqa: E402
    GOOD_REASONING,
    conversation_doc,
    distil_one,
    grouped,
    synthetic_corpus,
)

TERMINATIONS = {"no_material_findings", "repair_converged_no_change", "depth_cap_reached"}


def _all_results():
    """One distillation result per §18 fixture."""
    return {name: distil_one(doc) for name, doc in synthetic_corpus().items()}


# ══════════════════════════════════════════════════════════════════════════════════════════
#  termination
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_fixture_terminates_by_a_named_condition():
    for name, (_, result, _) in _all_results().items():
        assert result.trace.passes, name
        assert result.trace.final.termination in TERMINATIONS, (name,
                                                                result.trace.final.termination)


def test_no_trace_exceeds_the_configured_depth():
    config = default_config()
    for name, (_, result, _) in _all_results().items():
        assert result.trace.depth_reached <= config.recursion.max_repair_depth, name
        assert result.trace.max_depth == config.recursion.max_repair_depth


def test_a_depth_cap_of_zero_still_terminates_and_records_the_cap():
    """The degenerate bound must not loop and must not silently accept."""
    import dataclasses
    config = dataclasses.replace(default_config(),
                                 recursion=RecursionConfig(max_repair_depth=0)).validated()
    conversation = grouped([synthetic_corpus()["stale_fact"]])[0][0]
    result = distil(conversation, config=config)
    assert result.trace.depth_reached == 0
    assert result.trace.final.termination in ("no_material_findings", "depth_cap_reached")


def test_a_repair_that_changes_nothing_stops_the_loop():
    """Without this, an unactionable finding burns the whole budget on every record."""
    _, result, _ = distil_one(synthetic_corpus()["stale_fact"])
    assert result.trace.final.termination == "repair_converged_no_change"
    assert result.trace.exhausted is False


def test_convergence_detection_can_be_disabled_and_the_cap_still_holds():
    """With early convergence off, an unactionable finding must hit the cap, not run forever."""
    import dataclasses
    config = dataclasses.replace(
        default_config(),
        recursion=RecursionConfig(max_repair_depth=2,
                                  stop_on_identical_artifact=False)).validated()
    conversation = grouped([synthetic_corpus()["stale_fact"]])[0][0]
    result = distil(conversation, config=config)
    assert result.trace.depth_reached <= 2
    assert result.trace.final.termination == "depth_cap_reached"
    assert result.trace.exhausted is True


def test_a_clean_record_terminates_on_the_first_pass():
    _, result, verdict = distil_one(synthetic_corpus()["good"])
    assert result.trace.final.termination == "no_material_findings"
    assert len(result.trace.passes) == 1
    assert verdict.disposition is Disposition.ACCEPT


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the cap is not an acceptance
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_an_exhausted_recursion_never_accepts():
    """§12: letting the cap act as an acceptance would make the bound a rubber stamp."""
    import dataclasses
    config = dataclasses.replace(
        default_config(),
        recursion=RecursionConfig(max_repair_depth=1,
                                  stop_on_identical_artifact=False)).validated()
    conversation = grouped([synthetic_corpus()["stale_fact"]])[0][0]
    result = distil(conversation, config=config)
    assert result.trace.exhausted is True
    verdict = disposition(result.record, conversation,
                          critique_result=critique(result.record, conversation),
                          verification=result.verification, trace=result.trace,
                          config=config.quality)
    assert verdict.disposition is not Disposition.ACCEPT
    assert RejectReason.RECURSION_EXHAUSTED in verdict.reasons


def test_an_unresolved_disagreement_routes_to_human_review():
    """§12: if unresolved disagreement persists, NEEDS_HUMAN_REVIEW."""
    _, _, verdict = distil_one(synthetic_corpus()["stale_fact"])
    assert verdict.disposition is Disposition.NEEDS_HUMAN_REVIEW
    assert verdict.reasons


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the chain is auditable
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_pass_records_what_paragraph_twelve_requires():
    """artifact hash, parent hash, stage, critic findings, repair actions, verification result."""
    for name, (_, result, _) in _all_results().items():
        for entry in result.trace.passes:
            payload = entry.to_dict()
            for key in ("artifact_hash", "parent_hash", "stage", "critique", "repair_actions",
                        "verification", "depth"):
                assert key in payload, (name, key)
            assert payload["artifact_hash"], name
            assert payload["critique"]["checks_run"], name


def test_the_parent_chain_is_intact_for_every_fixture():
    for name, (_, result, _) in _all_results().items():
        assert result.trace.chain_intact is True, name


def test_a_broken_parent_link_is_detected():
    """A trace with a gap cannot show what a record was repaired from."""
    _, result, _ = distil_one(synthetic_corpus()["good"])
    first = result.trace.passes[0]
    broken = RecursionTrace(passes=(replace(first, parent_hash="deadbeef"),),
                            max_depth=result.trace.max_depth)
    assert broken.chain_intact is False


def test_a_depth_gap_is_detected():
    _, result, _ = distil_one(synthetic_corpus()["good"])
    first = result.trace.passes[0]
    jumped = RecursionTrace(
        passes=(first, replace(first, depth=5, parent_hash=first.artifact_hash)),
        max_depth=result.trace.max_depth)
    assert jumped.chain_intact is False


def test_an_empty_trace_is_not_intact_and_has_no_final_pass():
    empty = RecursionTrace(passes=(), max_depth=3)
    assert empty.chain_intact is False
    with pytest.raises(DistillationRefused, match="empty"):
        _ = empty.final


def test_the_artifact_hash_changes_when_the_record_changes():
    """The hash is a content address, which is what makes convergence detectable."""
    _, result, _ = distil_one(synthetic_corpus()["good"])
    record = result.record
    altered = replace(record, task=replace(record.task, user_goal_summary="something else"))
    assert altered.artifact_hash != record.artifact_hash


def test_depth_and_parent_are_inside_the_artifact_hash():
    """Two passes that converge on the same content at different depths are different events."""
    _, result, _ = distil_one(synthetic_corpus()["good"])
    record = result.record
    deeper = replace(record, depth=1, parent_artifact_hash="a" * 64)
    assert deeper.artifact_hash != record.artifact_hash


def test_a_repaired_record_declares_its_parent():
    import dataclasses
    config = dataclasses.replace(
        default_config(),
        recursion=RecursionConfig(max_repair_depth=2,
                                  stop_on_identical_artifact=False)).validated()
    conversation = grouped([synthetic_corpus()["stale_fact"]])[0][0]
    result = distil(conversation, config=config)
    if result.record.depth > 0:
        assert result.record.parent_artifact_hash
        assert result.record.stage == "repair"


def test_a_record_at_depth_without_a_parent_is_refused():
    from reasoning_distillation.models import DistillationError
    from reasoning_distillation.operators import KNOWN_OPERATOR_NAMES
    _, result, _ = distil_one(synthetic_corpus()["good"])
    orphan = replace(result.record, depth=2, parent_artifact_hash="")
    with pytest.raises(DistillationError, match="no parent_artifact_hash"):
        orphan.validated(known_operators=KNOWN_OPERATOR_NAMES)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  repair is subtractive — the load-bearing property
# ══════════════════════════════════════════════════════════════════════════════════════════
def _field_sizes(record) -> dict:
    payload = record.to_dict()
    sizes: dict[str, int] = {}
    for section in ("task", "epistemic_state", "decision", "outcome"):
        for key, value in payload[section].items():
            if isinstance(value, list):
                sizes[f"{section}.{key}"] = len(value)
            elif isinstance(value, str):
                sizes[f"{section}.{key}"] = len(value)
    return sizes


def test_repair_can_only_remove_or_weaken():
    """If a repair could ADD a field, the loop would launder a fabrication into a clean record."""
    for name, doc in synthetic_corpus().items():
        conversation = grouped([doc])[0][0]
        record = extract(conversation)
        findings = critique(record, conversation).material()
        repaired, actions = repair(record, findings)
        before, after = _field_sizes(record), _field_sizes(repaired)
        for key, size in after.items():
            assert size <= before[key], (name, key, before[key], size)
        assert all(a.action in REPAIR_ACTIONS for a in actions), name


def test_every_permitted_repair_action_is_subtractive_by_name():
    """The closed list itself must contain no additive verb."""
    for action in REPAIR_ACTIONS:
        assert any(action.startswith(verb) for verb in
                   ("drop_", "clear_", "downgrade_", "reset_")), action
    assert not any("add" in a or "write" in a or "fill" in a for a in REPAIR_ACTIONS)


def test_repair_records_what_it_did_without_quoting_what_it_removed():
    conversation = grouped([synthetic_corpus()["missing_rationale"]])[0][0]
    record = extract(conversation)
    _, actions = repair(record, critique(record, conversation).material())
    for action in actions:
        payload = action.to_dict()
        assert payload["action"] and payload["field_path"]
        assert "Timsort" not in payload["reason"]


def test_dropping_an_indexed_element_removes_the_right_one():
    """Findings are applied in descending index order so an earlier drop cannot renumber a later."""
    from reasoning_distillation.critic import CriticFinding
    conversation = grouped([conversation_doc("multi-001", GOOD_REASONING)])[0][0]
    record = extract(conversation)
    original = record.decision.verification_plan
    if len(original) < 2:
        pytest.skip("fixture did not yield two verification-plan elements")
    findings = (
        CriticFinding("unsupported_text", Severity.MATERIAL, "decision.verification_plan",
                      "synthetic", index=0),
        CriticFinding("unsupported_text", Severity.MATERIAL, "decision.verification_plan",
                      "synthetic", index=len(original) - 1),
    )
    repaired, _ = repair(record, findings)
    assert repaired.decision.verification_plan == original[1:-1]


def test_invented_reasoning_is_cleared_wholesale():
    """The source has no reasoning turn, so every decision field is invented (§12)."""
    from reasoning_distillation.critic import CriticFinding
    conversation = grouped([synthetic_corpus()["missing_rationale"]])[0][0]
    record = extract(conversation)
    seeded = replace(record, decision=replace(
        record.decision, chosen_approach="a plausible reconstruction",
        subproblems=("invented step",)))
    finding = CriticFinding("reasoning_invented", Severity.BLOCKING, "decision", "synthetic")
    repaired, actions = repair(seeded, (finding,))
    assert repaired.decision.chosen_approach == ""
    assert repaired.decision.subproblems == ()
    assert any(a.action == "clear_invented_decision" for a in actions)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the critic itself
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_critic_check_that_raises_aborts_rather_than_being_skipped(monkeypatch):
    """A critique missing a check reports fewer findings and reads as a cleaner record."""
    from reasoning_distillation import critic as critic_module

    def exploding(record, conversation):
        raise RuntimeError("synthetic")

    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    monkeypatch.setattr(critic_module, "CHECKS",
                        (("exploding", exploding), *critic_module.CHECKS))
    with pytest.raises(CriticError, match="exploding"):
        critic_module.critique(result.record, conversation)


def test_the_critique_names_which_checks_ran():
    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    from reasoning_distillation.critic import CHECKS
    found = critique(result.record, conversation)
    assert set(found.checks_run) == {name for name, _ in CHECKS}


def test_advisory_findings_do_not_make_a_record_unclean():
    """A loop that chased advisory findings would never converge."""
    from reasoning_distillation.critic import CriticFinding, Critique
    advisory = Critique(findings=(CriticFinding("x", Severity.ADVISORY, "f", "d"),))
    assert advisory.clean is True
    assert advisory.material() == ()


def test_material_severities_are_configuration_not_a_constant():
    from reasoning_distillation.critic import CriticFinding, Critique
    found = Critique(findings=(CriticFinding("x", Severity.ADVISORY, "f", "d"),))
    assert found.material(("advisory",))
    assert not found.material(("blocking",))


def test_a_findings_detail_never_quotes_a_body():
    for name, (conversation, result, _) in _all_results().items():
        for finding in critique(result.record, conversation).findings:
            assert len(finding.to_dict()["detail"]) <= 320, name


def test_the_whole_trace_serialises_body_free():
    """A trace is written into the corpus, so it must survive the record write gate."""
    from reasoning_distillation.privacy import PrivacyCategory, scan
    from training_gym.schemas import canonical_json
    for name, (_, result, _) in _all_results().items():
        blob = canonical_json(result.trace.to_dict())
        secrets = [f for f in scan(blob) if f.category.is_secret_like
                   and f.category is not PrivacyCategory.RUNTIME_SCANNER]
        assert not secrets, (name, secrets)
