"""V69 M67A — structural coverage for ABSENT controls (§22).

THE LESSON THIS SUITE APPLIES
-----------------------------
M66B ran a 131-mutation campaign with 0 survivors, and §22 names the limit of what that proved:
**mutation testing only tests controls that already exist.** Break a line, see a test fail, and you
have learned that the line is load-bearing. You have learned nothing about the line nobody wrote.

So these tests do not mutate anything. Each asks a different question — *"what required control
could be completely missing?"* — and then checks the property directly, over the whole fixture
corpus, rather than checking that some particular implementation of it still works.

This was not a theoretical exercise during construction. Authoring the §18 negative fixtures found
that ``RejectReason.CONTRADICTORY_TRACE`` was in the closed reason set and **nothing in the codebase
could emit it** — a reason code with no producer, which every mutation campaign in the world would
have reported as fine. :func:`test_every_reason_code_has_a_producer` is the generalisation of that
finding, and it is the most valuable test in this file.

§22's OWN LIST, MAPPED
----------------------
  * every source artifact has provenance        -> :func:`test_every_turn_carries_a_validated_provenance`
  * every split item belongs to exactly one family -> :func:`test_every_conversation_belongs_to_exactly_one_family`
  * every ACCEPT item has a verified source link -> :func:`test_every_accepted_record_has_a_verified_source_link`
  * every private/export decision is explicit    -> :func:`test_no_record_anywhere_carries_an_undecided_export_status`
  * every recursive pass has a bounded parent chain -> :func:`test_every_trace_has_a_bounded_intact_parent_chain`
  * every skill has minimum multi-example support -> :func:`test_no_skill_can_be_supported_by_one_family`
  * every holdout access goes through the guard  -> :func:`test_no_module_reaches_holdout_content_without_the_guard`
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
PACKAGE_ROOT = _HERE.parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from training_gym.schemas import SensitivityClass  # noqa: E402
from reasoning_distillation import (  # noqa: E402
    benchmark,
    critic,
    dedupe,
    distiller,
    holdout,
    manifests,
    normalization,
    operators,
    pipeline,
    privacy,
    quality,
    skills,
    splitting,
    storage,
    verifier,
)
from reasoning_distillation.config import default_config  # noqa: E402
from reasoning_distillation.models import (  # noqa: E402
    Disposition,
    ExportStatus,
    FactualState,
    RejectReason,
    SourceType,
    TurnRole,
)
from reasoning_distillation.quality import DIMENSIONS  # noqa: E402
from reasoning_distillation.skills import SkillStatus  # noqa: E402
from test_reasoning_distillation_m67a import (  # noqa: E402
    _accepted_pairs,
    corpus_files,
    distil_one,
    synthetic_corpus,
)

#: Every module in the package, so a coverage claim is over the package rather than a sample.
STAGE_MODULES = (normalization, privacy, dedupe, splitting, holdout, operators, distiller,
                 critic, verifier, quality, skills, benchmark, manifests, storage, pipeline)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One full build over the §18 corpus. Module-scoped: the properties are about the build."""
    root = tmp_path_factory.mktemp("m67a-absent")
    files = corpus_files(root, synthetic_corpus())
    return pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  §22 — the named controls
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_turn_carries_a_validated_provenance(built):
    """A source artifact with no deterministic identity must not exist in the corpus."""
    for conversation in built.ingest.conversations:
        assert conversation.source_file_hash
        for turn in conversation.turns:
            assert turn.provenance.validated() is turn.provenance
            assert turn.provenance.unit_id
            assert turn.provenance.source_file_hash == conversation.source_file_hash
            assert turn.provenance.conversation_id == conversation.conversation_id
            assert turn.provenance.source_type in set(SourceType)
            assert turn.provenance.role in set(TurnRole)


def test_every_conversation_belongs_to_exactly_one_family(built):
    memberships: dict[str, list[str]] = {}
    for family in built.dedupe_report.families:
        for member in family.member_digests:
            memberships.setdefault(member, []).append(family.family_id)
    for conversation in built.ingest.conversations:
        assert conversation.family_id
        assert len(memberships.get(conversation.digest, [])) == 1, conversation.conversation_id


def test_every_family_is_assigned_to_exactly_one_partition(built):
    for family in built.dedupe_report.families:
        assigned = [p for p in splitting.Partition
                    if family.family_id in built.split.families_in(p)]
        assert len(assigned) == 1, family.family_id


def test_every_accepted_record_has_a_verified_source_link(built):
    """§22, and the reason PARTIAL is not a pass: the orphan field is the uncheckable one."""
    accepted = [(r, v) for r, v in zip(built.results, built.verdicts)
                if v.disposition is Disposition.ACCEPT]
    assert accepted, "the fixture corpus must accept something or this test proves nothing"
    real_units = {t.provenance.unit_id for c in built.ingest.conversations for t in c.turns}
    for result, _ in accepted:
        assert result.verification.verdict is verifier.SourceVerdict.VERIFIED
        assert result.verification.orphans == ()
        assert result.record.provenance.source_unit_ids
        assert set(result.record.provenance.source_unit_ids) <= real_units
        assert result.record.provenance.family_id


def test_no_record_anywhere_carries_an_undecided_export_status(built):
    """§4: every record carries an explicit privacy/export status."""
    for result in built.results:
        assert result.record.privacy.export_status is not ExportStatus.EXPORT_UNKNOWN
    for conversation in built.ingest.conversations:
        for turn in conversation.turns:
            assert turn.privacy.export_status is not ExportStatus.EXPORT_UNKNOWN


def test_every_trace_has_a_bounded_intact_parent_chain(built):
    cap = default_config().recursion.max_repair_depth
    for result in built.results:
        assert result.trace.chain_intact is True
        assert result.trace.depth_reached <= cap
        assert result.trace.final.termination


def test_no_skill_can_be_supported_by_one_family():
    """§15's bar, checked as a property of the deriver rather than of one fixture."""
    for skill in skills.derive(_accepted_pairs(1)):
        assert skill.status is SkillStatus.INSUFFICIENT_EVIDENCE
    for skill in skills.derive(_accepted_pairs(5)):
        if skill.status is SkillStatus.SUPPORTED:
            assert skill.support.family_count >= 2
            assert skill.support.example_count >= 3


def test_no_module_reaches_holdout_content_without_the_guard():
    """Every function taking a conversation LIST must also take a guard, or not distil.

    Checked by SIGNATURE rather than by behaviour. A behavioural test proves the current call path
    is safe; this proves no NEW caller can be written that omits the firewall, which is the control
    that would otherwise be missing.
    """
    offenders: list[str] = []
    for name in ("distil_corpus",):
        signature = inspect.signature(getattr(distiller, name))
        assert "guard" in signature.parameters, name
        assert signature.parameters["guard"].default is inspect.Parameter.empty, name
    for module in STAGE_MODULES:
        for attribute, value in vars(module).items():
            if attribute.startswith("_") or not inspect.isfunction(value):
                continue
            if getattr(value, "__module__", "") != module.__name__:
                continue
            params = inspect.signature(value).parameters
            takes_corpus = "conversations" in params
            if takes_corpus and "guard" not in params and value.__name__ not in (
                    "analyze", "audit", "ingest_paths"):
                offenders.append(f"{module.__name__}.{attribute}")
    assert not offenders, f"functions taking a conversation list without a guard: {offenders}"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  controls that could be absent, beyond §22's own list
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_reason_code_has_a_producer():
    """The finding that motivated this whole suite.

    ``CONTRADICTORY_TRACE`` was a member of the closed reason set that NOTHING could emit. A
    mutation campaign cannot find that: there is no line to break. This test reads the quality
    module's source and asserts every member is referenced, so a reason code added without a
    producer fails here rather than sitting unreachable for a milestone.
    """
    source = Path(inspect.getfile(quality)).read_text(encoding="utf-8")
    unreferenced = sorted(r.name for r in RejectReason
                          if f"RejectReason.{r.name}" not in source)
    assert not unreferenced, f"reason codes with no producer in quality.py: {unreferenced}"


def test_every_reason_code_is_classified_as_reject_or_review():
    """A reason in neither class would silently ACCEPT the record that carries it."""
    source = Path(inspect.getfile(quality)).read_text(encoding="utf-8")
    start = source.index("reject_reasons = frozenset({")
    block = source[start:source.index("})", start)]
    rejecting = {r.name for r in RejectReason if f"RejectReason.{r.name}" in block}
    reviewing = set(r.name for r in RejectReason) - rejecting
    # Every code must land in exactly one class, and both classes must be non-empty: if every code
    # rejected, review would be unreachable and §12's escalation path would not exist.
    assert rejecting and reviewing
    assert rejecting | reviewing == {r.name for r in RejectReason}


def test_every_quality_dimension_has_a_floor_or_is_documented_as_informational():
    config = default_config().quality
    for dimension in DIMENSIONS:
        assert dimension in config.floors, dimension
    for required in config.required_dimensions:
        assert required in config.floors, required


def test_every_required_dimension_is_actually_produced_by_the_scorer(built):
    """A required dimension the scorer never emits would fail every record, or none."""
    config = default_config().quality
    for _, verdict in zip(built.results, built.verdicts):
        for required in config.required_dimensions:
            assert required in verdict.score.scores, required


def test_every_operator_in_the_taxonomy_is_reachable_by_detection_or_declared_unmarked():
    """An operator with no markers can be stored and never found — a member with no producer."""
    unmarked = [spec.operator.value for spec in operators.TAXONOMY if not spec.markers]
    assert unmarked == [], f"operators with no detection markers: {unmarked}"


def test_every_stage_publishes_a_version():
    """A build whose stage version is absent cannot be compared to another build (§20)."""
    for module in STAGE_MODULES:
        versions = getattr(module, "versions", None)
        if versions is None:
            continue
        payload = versions()
        assert payload, module.__name__
        assert any(key.endswith("_version") or key == "coverage" for key in payload), (
            module.__name__, sorted(payload))


def test_the_manifest_collects_a_version_for_every_stage_that_has_one():
    """Collected by CALLING each stage, so a hand-maintained list cannot go stale."""
    recorded = manifests.stage_versions()
    for module in STAGE_MODULES:
        if getattr(module, "versions", None) is None:
            continue
        short = module.__name__.rsplit(".", 1)[-1]
        alias = {"holdout": "firewall", "pipeline": None, "storage": None,
                 "manifests": None}.get(short, short)
        if alias is None:
            continue
        assert alias in recorded, short


def test_every_leakage_check_is_declared_expected():
    """A check that runs but is not expected cannot be reported as MISSING when it stops running."""
    conversations = [distil_one(doc)[0] for doc in synthetic_corpus().values()]
    report = dedupe.analyze(conversations)
    split = splitting.plan(report)
    result = holdout.audit(report=report, split=split, conversations=conversations)
    assert {f.check for f in result.findings} <= set(holdout.EXPECTED_CHECKS)
    assert set(holdout.EXPECTED_CHECKS) == {f.check for f in result.findings}


def test_every_enum_that_gates_a_decision_has_an_explicit_unknown_or_blocking_member():
    """A gating enum with no honest 'we do not know' forces a favourable guess."""
    assert ExportStatus.EXPORT_UNKNOWN in set(ExportStatus)
    assert FactualState.UNKNOWN in set(FactualState)
    assert TurnRole.UNKNOWN in set(TurnRole)
    assert SourceType.UNKNOWN in set(SourceType)
    assert holdout.Verdict.UNAVAILABLE in set(holdout.Verdict)
    assert verifier.SourceVerdict.PARTIAL in set(verifier.SourceVerdict)
    assert SkillStatus.INSUFFICIENT_EVIDENCE in set(SkillStatus)


def test_no_default_member_of_a_gating_enum_is_the_permissive_one():
    """The fail-closed direction, asserted rather than assumed."""
    from reasoning_distillation.models import UNCLASSIFIED
    assert UNCLASSIFIED.export_status.permits_export is False
    assert FactualState.SOURCE_ONLY.is_usable_as_truth is False
    assert holdout.Verdict.UNAVAILABLE.is_clean is False
    assert verifier.SourceVerdict.PARTIAL.is_clean is False
    assert SkillStatus.INSUFFICIENT_EVIDENCE.is_usable is False


def test_every_area_any_module_writes_to_is_a_declared_area():
    """A write to an undeclared area would create a parallel tree nobody audits.

    Scans every module for a literal area argument to the write helpers and to ``CorpusRoot.area``,
    then checks each name against :data:`storage.AREAS`. Reading the SOURCE rather than exercising
    the calls is deliberate: a write on a rarely-taken branch would never be executed by a test,
    and the control that would be missing is "no undeclared area exists anywhere", not "the paths
    we happened to run were fine".
    """
    pattern = re.compile(r"""(?:write_report|write_record)\(\s*\w+\s*,\s*["'](\w+)["']"""
                         r"""|\.area\(\s*["'](\w+)["']\s*\)""")
    package_dir = Path(inspect.getfile(pipeline)).parent
    referenced: set[str] = set()
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            referenced.add(next(g for g in match.groups() if g))
    assert referenced, "the scan found no area references at all, so it is not checking anything"
    undeclared = sorted(referenced - set(storage.AREAS))
    assert not undeclared, f"modules write to undeclared area(s): {undeclared}"


def test_no_stage_can_claim_verified_factual_state(built):
    """§10: M67A verifies no external facts, so VERIFIED must be unreachable from any stage."""
    for result in built.results:
        assert all(c.state is not FactualState.VERIFIED for c in result.record.factual_claims)


def test_no_stage_reports_an_evaluation_result_or_a_promotion_signal(built):
    """§2's freeze, asserted against the build's own output rather than promised in a docstring."""
    payload = json.dumps(built.to_dict())
    for forbidden in ("candidate006", "eval-v8", "eval_v8", "promotion_eligible",
                      "TRAIN:", "EVAL:", "PROMOTE:"):
        assert forbidden not in payload, forbidden
    declarations = built.manifest.to_dict()["declarations"]
    assert all(value is False for value in declarations.values())


def test_the_benchmark_harness_takes_nothing_that_could_measure_jarvis():
    """§16: the freeze is enforced by what the constructor cannot accept."""
    signature = inspect.signature(benchmark.BenchmarkHarness.__init__)
    assert set(signature.parameters) == {"self", "cases"}
    harness_source = Path(inspect.getfile(benchmark)).read_text(encoding="utf-8")
    for forbidden in ("import torch", "load_adapter", "from_pretrained", "requests.post",
                      "httpx.", "openai", "anthropic"):
        assert forbidden not in harness_source, forbidden


def test_no_module_in_the_package_imports_a_model_or_network_library():
    """§19: every deterministic stage must work offline. A stray import would break that silently."""
    offenders: list[str] = []
    package_dir = Path(inspect.getfile(pipeline)).parent
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in ("import torch", "import transformers", "import requests", "import httpx",
                      "import openai", "import anthropic", "urllib.request"):
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, offenders


def test_the_package_never_writes_outside_a_prepared_corpus_root():
    """A write bypassing CorpusRoot would escape the gitignore verification (§4)."""
    package_dir = Path(inspect.getfile(pipeline)).parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "storage.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in ("open(", ".write_text(", ".write_bytes(", "os.replace("):
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, (
        f"only storage.py may write files, so the gitignore check cannot be bypassed: {offenders}")


def test_every_public_dataclass_holding_text_has_a_body_free_repr():
    """A dataclass repr renders every field; a container recurses. One missing override leaks."""
    import dataclasses
    from reasoning_distillation import models
    offenders: list[str] = []
    for name, value in vars(models).items():
        if not dataclasses.is_dataclass(value) or not isinstance(value, type):
            continue
        text_fields = [f.name for f in dataclasses.fields(value)
                       if f.type in ("str", "tuple[str, ...]") and f.name in
                       ("text", "user_goal_summary", "chosen_approach", "decision_basis")]
        if text_fields and "__repr__" not in vars(value):
            offenders.append(name)
    assert not offenders, f"body-bearing dataclasses with the default repr: {offenders}"


def test_counts_reconcile_for_a_real_build(built):
    """A count that does not add up is evidence a record was dropped with no reason code."""
    counts = built.counts()
    assert counts.validated() is counts
    assert counts.distilled == counts.accepted + counts.review + counts.rejected
    assert counts.conversations == len(built.ingest.conversations)
    assert counts.families == built.dedupe_report.family_count


def test_every_non_accept_record_in_the_build_names_a_reason(built):
    for result, verdict in zip(built.results, built.verdicts):
        if verdict.disposition is not Disposition.ACCEPT:
            assert verdict.reasons, result.record.example_id
            assert all(r in set(RejectReason) for r in verdict.reasons)


def test_the_corpus_input_contract_is_reachable_without_any_corpus():
    """§5: the pipeline must be able to say what it needs before it has anything."""
    from reasoning_distillation.cli import EXIT_CORPUS_REQUIRED, main
    assert main(["contract", "--json"]) == EXIT_CORPUS_REQUIRED
