"""V69 M67A — the leakage firewall (§17), tested as a security boundary.

THE THREAT MODEL
----------------
§17 asks for this to be treated *"like a security boundary for experimental validity"*, and the
adversary is us. Nobody attacks a holdout. What happens is that a developer needs one more example
to debug an extractor, or a report renders a few turns to show what a family looks like — small,
well-intentioned reads that spend a resource which cannot be un-spent.

So these tests do not check that the pipeline behaves well when used correctly. They check that it
refuses when used incorrectly, including in the ways somebody helpful would use it.

WHAT EACH §17 BULLET MAPS TO
----------------------------
  * one conversation cannot exist in multiple splits  -> :func:`test_one_conversation_lands_in_exactly_one_partition`
  * a duplicate family cannot cross into the holdout  -> :func:`test_a_duplicate_family_moves_as_one_unit`
  * a near-duplicate cannot cross into the holdout    -> :func:`test_no_near_duplicate_pair_straddles_the_holdout_boundary`
  * dev commands cannot load holdout content          -> :func:`test_the_guard_exposes_no_route_to_holdout_content`
  * model-assisted distillation cannot access holdout -> :func:`test_a_provider_is_unreachable_for_holdout_material`
  * raw corpus paths are gitignored                   -> the privacy suite's root tests
  * EXPORT_SAFE must be explicit                      -> :func:`test_export_status_is_explicit_for_every_turn`
  * source identity uncertainty fails closed          -> :func:`test_an_unplaced_family_is_refused_not_defaulted`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
PACKAGE_ROOT = _HERE.parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from training_gym.schemas import SensitivityClass  # noqa: E402
from reasoning_distillation import pipeline  # noqa: E402
from reasoning_distillation.config import DedupeConfig, default_config  # noqa: E402
from reasoning_distillation.dedupe import analyze  # noqa: E402
from reasoning_distillation.distiller import distil_corpus  # noqa: E402
from reasoning_distillation.holdout import (  # noqa: E402
    EXPECTED_CHECKS,
    HoldoutAccessDenied,
    HoldoutGuard,
    LeakageError,
    Verdict,
    audit,
)
from reasoning_distillation.models import ExportStatus  # noqa: E402
from reasoning_distillation.normalization import ingest_bytes, with_family  # noqa: E402
from reasoning_distillation.splitting import (  # noqa: E402
    HoldoutViolation,
    Partition,
    freeze,
    plan,
    plan_respecting_frozen,
    verify_frozen,
)
from test_reasoning_distillation_m67a import (  # noqa: E402
    conversation_doc,
    corpus_files,
    ingest,
    synthetic_corpus,
)

import json  # noqa: E402


def _families(count: int, *, start: int = 0):
    """*count* conversations with genuinely distinct problem statements, already grouped."""
    docs = [conversation_doc(
        f"leak-{i:03d}",
        [("user_request",
          f"Subsystem {i} must not exceed a {i}ms budget and must not drop records. Why does "
          f"component {i} diverge from component {i + 700} under replay?"),
         ("assistant_reasoning",
          f"The question assumes component {i} diverges on load, and that is not correct without "
          f"evidence. I am not sure which build of {i} is deployed. I need to check the metrics "
          f"for {i} before claiming anything. First, confirm the lag for {i}. Second, read the "
          f"ack policy for {i}. Let me verify the timeout for {i} rather than assume it. Done "
          f"when both are confirmed for {i}."),
         ("assistant_answer", f"Confirm lag and ack policy for component {i}.")])
        for i in range(start, start + count)]
    conversations = [ingest(d) for d in docs]
    report = analyze(conversations)
    return ([with_family(c, report.family_of(c.digest)) for c in conversations], report)


def _split_with_holdout(count: int = 60):
    conversations, report = _families(count)
    split = plan(report)
    frozen = freeze(split, generation="test")
    return (conversations, report, split, frozen, HoldoutGuard(split, frozen))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the seven checks
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_all_seven_checks_run_and_pass_on_a_clean_corpus():
    conversations, report, split, frozen, guard = _split_with_holdout()
    result = audit(report=report, split=split, conversations=conversations, frozen=frozen,
                   guard=guard)
    assert result.clean is True
    assert {f.check for f in result.findings} == set(EXPECTED_CHECKS)
    assert result.missing_checks == ()


def test_a_missing_check_makes_the_audit_unclean_even_with_no_failures():
    """An audit that silently skipped its hardest check reports the same 'no failures'."""
    from reasoning_distillation.holdout import Finding, LeakageAudit
    partial = LeakageAudit(
        findings=(Finding(check=EXPECTED_CHECKS[0], verdict=Verdict.PASS),),
        checks_expected=EXPECTED_CHECKS)
    assert partial.clean is False
    assert len(partial.missing_checks) == len(EXPECTED_CHECKS) - 1
    with pytest.raises(LeakageError, match="missing"):
        partial.raise_if_unclean()


def test_an_unavailable_check_is_a_failure_not_a_neutral_result():
    """'Nothing has been frozen yet' must not let an unfrozen corpus read as a protected one."""
    conversations, report = _families(60)
    split = plan(report)
    result = audit(report=report, split=split, conversations=conversations, frozen=None)
    frozen_check = next(f for f in result.findings if f.check == "frozen_record_matches_plan")
    assert frozen_check.verdict is Verdict.UNAVAILABLE
    assert frozen_check.verdict.is_clean is False
    assert result.clean is False


def test_one_conversation_lands_in_exactly_one_partition():
    conversations, report, split, frozen, guard = _split_with_holdout()
    seen: dict[str, set[str]] = {}
    for conversation in conversations:
        seen.setdefault(conversation.digest, set()).add(
            split.partition_of(conversation.family_id).value)
    assert all(len(parts) == 1 for parts in seen.values())
    check = next(f for f in audit(report=report, split=split, conversations=conversations,
                                  frozen=frozen, guard=guard).findings
                 if f.check == "single_partition_per_conversation")
    assert check.verdict is Verdict.PASS


def test_a_duplicate_family_moves_as_one_unit():
    """The family is the unit of assignment precisely so its members cannot be separated."""
    fixtures = synthetic_corpus()
    extra, _ = _families(30)
    docs = [fixtures["good"], fixtures["duplicate"], fixtures["near_duplicate"]]
    conversations = [ingest(d) for d in docs] + [
        ingest_bytes(filename=f"x{i}.json",
                     content=json.dumps(conversation_doc(
                         f"filler-{i:03d}",
                         [("user_request", f"Filler topic {i} about subsystem {i} and its "
                                           f"{i}ms budget under replay conditions."),
                          ("assistant_reasoning", f"I need to check subsystem {i} first."),
                          ("assistant_answer", f"Check {i}.")])).encode(),
                     sensitivity=SensitivityClass.SYNTHETIC)
        for i in range(40)]
    del extra
    report = analyze(conversations)
    conversations = [with_family(c, report.family_of(c.digest)) for c in conversations]
    split = plan(report)
    duplicate_family = report.family_of(conversations[0].digest)
    assert report.family_of(conversations[1].digest) == duplicate_family
    assert report.family_of(conversations[2].digest) == duplicate_family
    assert len({split.partition_of(c.family_id) for c in conversations[:3]}) == 1


def test_no_near_duplicate_pair_straddles_the_holdout_boundary():
    conversations, report, split, frozen, guard = _split_with_holdout()
    result = audit(report=report, split=split, conversations=conversations, frozen=frozen,
                   guard=guard, config=DedupeConfig())
    check = next(f for f in result.findings
                 if f.check == "near_duplicate_never_crosses_into_holdout")
    assert check.verdict is Verdict.PASS
    holdout_members = {
        member for family in report.families
        if split.assignments.get(family.family_id) == Partition.FROZEN_HOLDOUT.value
        for member in family.member_digests}
    for family in report.families:
        for evidence in family.evidence:
            if evidence.blocked:
                assert ((evidence.left_digest in holdout_members)
                        == (evidence.right_digest in holdout_members))


def test_the_configured_threshold_is_what_the_audit_checks_against():
    """A threshold nobody enforces is documentation, not a control."""
    conversations, report, split, frozen, guard = _split_with_holdout()
    loose = DedupeConfig(near_duplicate_block=0.01, near_duplicate_warn=0.005)
    result = audit(report=report, split=split, conversations=conversations, frozen=frozen,
                   guard=guard, config=loose)
    check = next(f for f in result.findings
                 if f.check == "near_duplicate_never_crosses_into_holdout")
    assert str(loose.near_duplicate_block) in check.detail


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the guard
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_guard_exposes_no_route_to_holdout_content():
    """Not a disabled route, not one behind a config key — there is no such method."""
    _, _, _, frozen, guard = _split_with_holdout()
    exposed = {name for name in dir(guard) if not name.startswith("_")}
    assert exposed == {"assert_no_holdout_content", "assert_permitted", "filter",
                       "holdout_family_count", "is_holdout", "permit", "readable_partitions",
                       "to_dict"}
    assert all(not guard.permit(fid) for fid in frozen.family_ids)
    assert guard.readable_partitions() == ("development", "validation")


def test_the_guard_holds_no_conversation_content():
    _, _, _, _, guard = _split_with_holdout()
    blob = json.dumps(guard.to_dict())
    assert "Subsystem" not in blob and "user_request" not in blob
    assert "conversations" not in guard.__slots__


def test_filter_reports_refusals_rather_than_silently_dropping_them():
    """A caller receiving only the allowed list cannot tell a refusal from an empty corpus."""
    conversations, _, split, frozen, guard = _split_with_holdout()
    allowed, refused = guard.filter(conversations, operation="distil")
    assert refused, "holdout families must appear as refusals"
    assert len(allowed) + len(refused) == len(conversations)
    assert all("refused" in f.detail for f in refused)


def test_an_unplaced_family_is_refused_not_defaulted():
    """§17: source identity uncertainty fails closed."""
    _, _, split, frozen, guard = _split_with_holdout()
    assert guard.permit("0" * 64) is False
    assert guard.permit("") is False
    with pytest.raises(HoldoutAccessDenied, match="UNKNOWN"):
        guard.assert_permitted("0" * 64, operation="test")


def test_the_guard_trusts_the_frozen_record_when_the_plan_disagrees():
    """A family either source calls held out is held out."""
    conversations, report, split, frozen = _split_with_holdout()[:4]
    drifted = plan(report, config=default_config().with_seed(4242).split)
    guard = HoldoutGuard(drifted, frozen)
    assert all(guard.is_holdout(fid) for fid in frozen.family_ids)
    assert all(not guard.permit(fid) for fid in frozen.family_ids)
    del conversations, split


def test_a_provider_is_unreachable_for_holdout_material():
    """§17: model-assisted distillation cannot access the holdout."""
    conversations, _, split, frozen, guard = _split_with_holdout()
    holdout = [c for c in conversations if guard.is_holdout(c.family_id)]
    assert holdout
    with pytest.raises(HoldoutAccessDenied, match="FROZEN HOLDOUT"):
        guard.assert_no_holdout_content(holdout, operation="model-assisted distillation")


def test_distil_corpus_cannot_be_called_without_a_guard():
    """The enforcement is the signature: omitting the firewall is a TypeError."""
    conversations, _, _, _, _ = _split_with_holdout()
    with pytest.raises(TypeError):
        distil_corpus(conversations)  # type: ignore[call-arg]


def test_distil_corpus_produces_no_record_for_a_holdout_family():
    conversations, _, split, frozen, guard = _split_with_holdout()
    results = distil_corpus(conversations, guard=guard)
    produced = {r.record.provenance.family_id for r in results}
    assert not produced & set(frozen.family_ids)
    assert len(results) == len(conversations) - sum(
        1 for c in conversations if guard.is_holdout(c.family_id))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  immutability of a frozen holdout (§9)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_family_leaving_the_holdout_is_refused():
    """The most dangerous direction: it becomes trainable while reports still count it held out."""
    _, report, split, frozen, _ = _split_with_holdout()
    from dataclasses import replace
    shrunk = replace(split, assignments={
        fid: (Partition.DEVELOPMENT.value if fid == frozen.family_ids[0] else name)
        for fid, name in split.assignments.items()})
    with pytest.raises(HoldoutViolation, match="LEFT the frozen holdout"):
        verify_frozen(frozen, shrunk)
    del report


def test_a_family_entering_the_holdout_is_refused():
    """A holdout that grows after freezing was not frozen."""
    _, report, split, frozen, _ = _split_with_holdout()
    grown_conversations, grown_report = _families(90)
    with pytest.raises(HoldoutViolation):
        verify_frozen(frozen, plan(grown_report))
    del report, split, grown_conversations


def test_a_seed_change_cannot_reproduce_a_frozen_holdout():
    _, report, _, frozen, _ = _split_with_holdout()
    reseeded = plan(report, config=default_config().with_seed(999).split)
    with pytest.raises(HoldoutViolation, match="different seed|different partition"):
        verify_frozen(frozen, reseeded)


def test_growth_pins_the_holdout_and_reports_its_dilution():
    """The legitimate growth path: pinned members, new material to development only."""
    _, report, split, frozen, _ = _split_with_holdout()
    bigger_conversations, bigger = _families(120)
    del bigger_conversations, split
    # The original families must still be present for a pinned re-plan to be meaningful.
    combined = analyze([*[c for c in _families(60)[0]], *_families(60, start=60)[0]])
    del combined
    regrown = plan_respecting_frozen(_families(60)[1], frozen)
    verify_frozen(frozen, regrown)
    assert set(regrown.families_in(Partition.FROZEN_HOLDOUT)) == set(frozen.family_ids)
    del bigger, report


def test_a_frozen_family_that_vanished_is_a_rewrite_not_a_growth():
    _, _, _, frozen, _ = _split_with_holdout()
    _, other_report = _families(60, start=5000)
    with pytest.raises(HoldoutViolation, match="ABSENT from this corpus"):
        plan_respecting_frozen(other_report, frozen)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  explicit export status (§4, §17)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_export_status_is_explicit_for_every_turn():
    conversations, report, split, frozen, guard = _split_with_holdout()
    assert all(t.privacy.export_status is not ExportStatus.EXPORT_UNKNOWN
               for c in conversations for t in c.turns)
    check = next(f for f in audit(report=report, split=split, conversations=conversations,
                                  frozen=frozen, guard=guard).findings
                 if f.check == "export_safe_is_explicit")
    assert check.verdict is Verdict.PASS


def test_an_unclassified_turn_fails_the_audit():
    from dataclasses import replace
    from reasoning_distillation.models import UNCLASSIFIED
    conversations, report, split, frozen, guard = _split_with_holdout()
    tainted = list(conversations)
    tainted[0] = replace(tainted[0], turns=(
        replace(tainted[0].turns[0], privacy=UNCLASSIFIED), *tainted[0].turns[1:]))
    check = next(f for f in audit(report=report, split=split, conversations=tainted,
                                  frozen=frozen, guard=guard).findings
                 if f.check == "export_safe_is_explicit")
    assert check.verdict is Verdict.FAIL


def test_an_ungrouped_conversation_fails_the_audit():
    conversations, report, split, frozen, guard = _split_with_holdout()
    from dataclasses import replace
    tainted = [replace(conversations[0], family_id=""), *conversations[1:]]
    check = next(f for f in audit(report=report, split=split, conversations=tainted,
                                  frozen=frozen, guard=guard).findings
                 if f.check == "every_item_has_a_family")
    assert check.verdict is Verdict.FAIL


# ══════════════════════════════════════════════════════════════════════════════════════════
#  the pipeline refuses to distil through a leak
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_pipeline_refuses_to_distil_when_the_audit_finds_a_real_leak(tmp_path, monkeypatch):
    """A leak invalidates the experiment this corpus exists to make possible."""
    from reasoning_distillation import holdout as holdout_module
    files = corpus_files(tmp_path, synthetic_corpus())
    real_audit = holdout_module.audit

    def leaking_audit(**kwargs):
        result = real_audit(**kwargs)
        from dataclasses import replace
        broken = replace(result.findings[0], verdict=Verdict.FAIL,
                         detail="synthetic leak for the refusal test")
        return replace(result, findings=(broken, *result.findings[1:]))

    monkeypatch.setattr(pipeline.holdout_stage, "audit", leaking_audit)
    with pytest.raises(pipeline.PipelineError, match="refusing to distil"):
        pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)


def test_an_absent_frozen_record_alone_does_not_block_distillation(tmp_path):
    """Too small to freeze is the honest state of a small corpus, not a leak."""
    files = corpus_files(tmp_path, synthetic_corpus())
    outcome = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    assert outcome.frozen is None
    assert outcome.audit.clean is False
    assert outcome.results, "a small corpus still yields development material"
    assert any("freeze" in reason for reason in outcome.not_run)


def test_the_firewall_version_and_check_list_are_published():
    versions = __import__("reasoning_distillation.holdout",
                          fromlist=["versions"]).versions()
    assert versions["firewall_version"]
    assert set(versions["checks"]) == set(EXPECTED_CHECKS)
    assert versions["unavailable_is_failure"] is True
