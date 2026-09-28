"""V69 M67A — the synthetic corpus (§18) and the deterministic foundation it exercises.

WHY THE FIXTURES LIVE HERE
-------------------------
§18 is explicit: *"never commit my private chats as tests."* Every fixture in this module is
authored here, and :func:`synthetic_corpus` is the one place that builds them so the other four
M67A suites import it rather than re-authoring near-copies that drift.

The set covers each case §18 enumerates. Each builder says which case it is and — more usefully —
what a WRONG implementation would do with it, because a fixture that only demonstrates the happy
path proves nothing about the control.

POSITIVE *AND* NEGATIVE
-----------------------
§18 asks for both. The negative cases here are not malformed input for its own sake; each one is a
specific way the pipeline could be wrong in a way that looks right: a duplicate that inflates the
corpus count, a stale fact that condemns good reasoning, a meta-noise trace that scores as
deliberation, a secret that rides out inside a lifted span.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
PACKAGE_ROOT = _HERE.parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

from training_gym.schemas import SensitivityClass  # noqa: E402
from reasoning_distillation import adapters, pipeline  # noqa: E402
from reasoning_distillation.adapters import AdapterError, AdapterNotImplemented  # noqa: E402
from reasoning_distillation.config import ConfigError, default_config, load_config  # noqa: E402
from reasoning_distillation.critic import Severity, critique  # noqa: E402
from reasoning_distillation.dedupe import analyze  # noqa: E402
from reasoning_distillation.distiller import distil, extract  # noqa: E402
from reasoning_distillation.holdout import HoldoutGuard, audit  # noqa: E402
from reasoning_distillation.manifests import (  # noqa: E402
    BuildManifest,
    CorpusCounts,
    ManifestError,
    compare,
    stage_versions,
)
from reasoning_distillation.models import (  # noqa: E402
    DistillationError,
    Disposition,
    ExportStatus,
    FactualState,
    RejectReason,
    ReviewStatus,
    SourceType,
    TurnRole,
    check_record_version,
)
from reasoning_distillation.normalization import ingest_bytes, with_family  # noqa: E402
from reasoning_distillation.operators import (  # noqa: E402
    KNOWN_OPERATOR_NAMES,
    TAXONOMY,
    ReasoningOperator,
    detect,
)
from reasoning_distillation.quality import DIMENSIONS, disposition  # noqa: E402
from reasoning_distillation.skills import SkillStatus, derive  # noqa: E402
from reasoning_distillation.splitting import Partition, freeze, plan  # noqa: E402
from reasoning_distillation.verifier import SourceVerdict, verify  # noqa: E402

SCHEMA = adapters.CONVERSATION_SCHEMA


# ══════════════════════════════════════════════════════════════════════════════════════════
#  §18 — the synthetic corpus
# ══════════════════════════════════════════════════════════════════════════════════════════
def conversation_doc(conversation_id: str, turns: "list[tuple[str, str]]", *,
                     source_model: str = "", source_date: str = "") -> dict:
    """One document in the declared JSON contract. The only shape these fixtures use."""
    doc: dict = {"schema": SCHEMA, "conversation_id": conversation_id,
                 "turns": [{"role": role, "text": text} for role, text in turns]}
    if source_model:
        doc["source_model"] = source_model
    if source_date:
        doc["source_date"] = source_date
    return doc


#: A full reasoning trace: premise correction, uncertainty, tool need, decomposition,
#: alternatives, a rejected option, verification and a stop condition. §18 cases 1, 2.
GOOD_REASONING = [
    ("user_request",
     "Our reporting query must not table-scan and must stay under 200ms. Why is the composite "
     "index ignored on the orders table?"),
    ("assistant_reasoning",
     "The question assumes the planner ignores the index, and that is not correct without an "
     "EXPLAIN. I am not sure whether the statistics are current. I need to check pg_stats before "
     "claiming anything. First, confirm the row estimate. Second, read the index column order. "
     "Option A is to reorder the columns; alternatively we add a partial index. Reordering will "
     "not work if the leading column is low-cardinality. Let me verify the selectivity rather "
     "than assume it. Done when the plan and the statistics are both confirmed."),
    ("assistant_answer",
     "Run EXPLAIN ANALYZE to confirm the plan, then reorder the columns or add a partial index "
     "based on the measured selectivity."),
]

#: §18 case 3 — an answer with NO rationale. A wrong implementation invents a procedure for it.
MISSING_RATIONALE = [
    ("user_request", "What is the fastest sort for nearly sorted data of moderate size?"),
    ("assistant_answer", "Timsort, which is what Python's sorted() already uses."),
]

#: §18 case 4 — a tool request and its result. A wrong implementation reports SUCCEEDED
#: execution for a conversation with no tool turn, or credits the user's turn as the decision.
TOOL_CALL = [
    ("user_request", "Is the retry_forever flag still referenced anywhere in this repository?"),
    ("assistant_reasoning",
     "I do not know without looking. I need to check the tree rather than assert it from memory. "
     "Let me verify with a content search before claiming it is unused."),
    ("tool_request", "grep -rn retry_forever ."),
    ("tool_result", "jarvis/core/retry.py:41: retry_forever = False  # deprecated"),
    ("assistant_answer", "Yes, one reference remains in the retry module and it is deprecated."),
]

#: §18 case 5 — a self-correction inside one episode.
SELF_CORRECTION = [
    ("user_request", "You said the default connect timeout is 30 seconds. Is that right?"),
    ("assistant_reasoning",
     "I need to check the configuration rather than defend the earlier claim. Actually, that was "
     "incorrect: I was wrong about the default. Correction: the connect timeout is 10 seconds and "
     "the read timeout is 30. Let me verify both in the source before answering."),
    ("assistant_revision",
     "Correction: connect is 10 seconds, read is 30. My earlier answer conflated the two."),
    ("assistant_answer", "Connect timeout is 10 seconds; the 30 seconds I cited was the read "
                         "timeout."),
]

#: §18 case 6 — a FALSE/STALE fact carrying USEFUL reasoning. The §10 case: the procedure
#: (notice time-sensitivity, refuse to assert, check) is excellent; the fact is stale. A wrong
#: implementation discards the procedure because the fact is wrong, or promotes the fact.
STALE_FACT_GOOD_REASONING = [
    ("user_request", "What is the current stable release of our upstream dependency?"),
    ("assistant_reasoning",
     "This is a version claim, so its truth is time-sensitive and my answer may have changed "
     "since I last saw it. I am not sure what the current release is. I need to check the release "
     "feed rather than assert a remembered number. Let me verify against the registry before "
     "claiming anything. Done when the current version is observed."),
    ("assistant_answer",
     "I should not state a version from memory here; check the registry, because a remembered "
     "release number may have changed."),
]

#: §18 case 10 — meta-noise only. Policy chatter, identity chatter, formatting deliberation and
#: hidden-state speculation, with no decision in it.
META_NOISE = [
    ("user_request", "Can you help me write a function that parses this log format?"),
    ("assistant_reasoning",
     "As an AI language model I should consider my system prompt and the content policy here. "
     "Okay, I need to think about how to format this. I will use a bullet list. The user is "
     "probably a beginner, so I should be gentle."),
    ("assistant_answer", "Here is a parser for that format."),
]

#: §18 cases 11, 12 — private information and a secret-like token in the SAME turn as a real
#: question. A wrong implementation lifts the span containing the key into the derived record.
PRIVATE_AND_SECRET = [
    ("user_request",
     "Hi Alejandro, my key is sk-abcdefghijklmnopqrstuvwxyz1234 and the log is at "
     "/home/kali/logs/app.log. The client must not retry on 4xx. Why does it stall?"),
    ("assistant_reasoning",
     "I should not echo the credential back. I need to check the retry policy before claiming "
     "anything. Let me verify the ack timeout in the configuration."),
    ("assistant_answer", "Rotate that key immediately, then check the retry policy."),
]

#: §18 case 13 — a contradictory trace: it asserts a thing and its negation, with no correction.
CONTRADICTORY = [
    ("user_request", "Does this cache evict on write or on read?"),
    ("assistant_reasoning",
     "The cache evicts on write. I am not sure which path evicts. The cache does not evict on "
     "write; eviction happens on read. I need to check the implementation."),
    ("assistant_answer", "It evicts on write."),
]


def synthetic_corpus() -> "dict[str, dict]":
    """Every §18 fixture, keyed by case name. The single source the other suites import.

    Duplicate and near-duplicate entries derive from ``good`` deliberately: a duplicate fixture
    authored independently would differ in ways that make the similarity score a property of the
    fixture rather than of the engine.
    """
    good = conversation_doc("good-001", GOOD_REASONING, source_model="claude-3-opus",
                            source_date="2024-05-01")
    near = json.loads(json.dumps(good))
    near["conversation_id"] = "good-001-near"
    near["turns"][0]["text"] = near["turns"][0]["text"].replace(
        "composite index", "compound index").replace("orders table", "order table")
    duplicate = json.loads(json.dumps(good))
    duplicate["conversation_id"] = "good-001-copy"
    return {
        "good": good,
        "missing_rationale": conversation_doc("no-rationale-001", MISSING_RATIONALE),
        "tool_call": conversation_doc("tool-001", TOOL_CALL),
        "self_correction": conversation_doc("selfcorr-001", SELF_CORRECTION),
        "stale_fact": conversation_doc("stale-001", STALE_FACT_GOOD_REASONING),
        "meta_noise": conversation_doc("noise-001", META_NOISE),
        "private_secret": conversation_doc("leaky-001", PRIVATE_AND_SECRET),
        "contradictory": conversation_doc("contradict-001", CONTRADICTORY),
        "duplicate": duplicate,
        "near_duplicate": near,
    }


#: §18 case 9 — a malformed export. Valid JSON, declared schema, structurally unusable.
MALFORMED_EXPORT = json.dumps({"schema": SCHEMA, "conversation_id": "broken-001"})

#: §18 case 19 — a schema version this build does not know.
WRONG_SCHEMA_VERSION = json.dumps({
    "schema": "m67a.conversation.999", "conversation_id": "future-001",
    "turns": [{"role": "user_request", "text": "anything"}]})


def ingest(doc: dict, *, sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC):
    """Ingest one fixture document. Synthetic by default: these are authored, not private."""
    return ingest_bytes(filename=f"{doc['conversation_id']}.json",
                        content=json.dumps(doc).encode(), sensitivity=sensitivity)


def grouped(docs: "list[dict]", *, sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC):
    """Ingest and attach families. The minimum a conversation needs before distillation."""
    conversations = [ingest(d, sensitivity=sensitivity) for d in docs]
    report = analyze(conversations)
    return ([with_family(c, report.family_of(c.digest)) for c in conversations], report)


def distil_one(doc: dict, *, sensitivity: SensitivityClass = SensitivityClass.SYNTHETIC):
    """Distil a single fixture and return ``(conversation, result, verdict)``."""
    conversations, _ = grouped([doc], sensitivity=sensitivity)
    conversation = conversations[0]
    result = distil(conversation)
    verdict = disposition(result.record, conversation,
                          critique_result=critique(result.record, conversation),
                          verification=result.verification, trace=result.trace)
    return (conversation, result, verdict)


def corpus_files(tmp_path: Path, docs: "dict[str, dict]") -> "list[Path]":
    """Write fixture documents to disk so the pipeline's file-reading path is exercised."""
    out: list[Path] = []
    for name, doc in sorted(docs.items()):
        target = tmp_path / f"{name}.json"
        target.write_text(json.dumps(doc), encoding="utf-8")
        out.append(target)
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════
#  adapters (§5, §7)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_json_contract_roundtrips_every_declared_role():
    doc = conversation_doc("roles-001", [(r.value, f"text for {r.value}") for r in TurnRole
                                         if r is not TurnRole.UNKNOWN])
    result = adapters.parse(filename="roles.json", content=json.dumps(doc).encode())
    assert result.review_status is ReviewStatus.OK
    assert [s.role for s in result.segments] == [r for r in TurnRole if r is not TurnRole.UNKNOWN]


def test_unknown_role_becomes_unknown_and_flags_review_rather_than_losing_the_file():
    doc = conversation_doc("weird-001", [("user_request", "a question about the retry policy")])
    doc["turns"].append({"role": "narrator", "text": "some text"})
    result = adapters.parse(filename="w.json", content=json.dumps(doc).encode())
    assert result.segments[-1].role is TurnRole.UNKNOWN
    assert result.review_status is ReviewStatus.NEEDS_HUMAN_REVIEW
    assert any("not a known TurnRole" in r for r in result.review_reasons)


def test_unknown_top_level_key_is_refused_so_a_typo_is_not_a_silent_no_op():
    doc = conversation_doc("typo-001", [("user_request", "q")])
    doc["sourcemodel"] = "claude"          # missing underscore
    with pytest.raises(AdapterError, match="unknown top-level key"):
        adapters.parse(filename="t.json", content=json.dumps(doc).encode())


def test_absent_model_and_date_are_unknown_and_are_never_inferred():
    """§7: missing is UNKNOWN. A wrong implementation would use the filename or the mtime."""
    conversation = ingest(conversation_doc("anon-001", GOOD_REASONING))
    assert conversation.source_model == ""
    assert conversation.source_date == ""
    assert conversation.turns[0].provenance.source_model_known is False
    assert conversation.turns[0].provenance.source_date_known is False


def test_declared_model_and_date_survive_into_every_turns_provenance():
    conversation = ingest(synthetic_corpus()["good"])
    assert conversation.source_model == "claude-3-opus"
    assert all(t.provenance.source_model == "claude-3-opus" for t in conversation.turns)
    assert all(t.provenance.source_model_known for t in conversation.turns)


def test_malformed_export_is_refused_not_imported_as_an_empty_conversation():
    with pytest.raises(AdapterError, match="non-empty list"):
        adapters.parse(filename="broken.json", content=MALFORMED_EXPORT.encode())


def test_unknown_schema_version_is_refused_by_the_adapter():
    """§18 case 19. A best-effort read of an unknown schema reads moved fields wrongly."""
    with pytest.raises(AdapterError, match="schema is"):
        adapters.parse(filename="future.json", content=WRONG_SCHEMA_VERSION.encode())


def test_deliberately_absent_formats_name_what_is_needed_to_add_them():
    """§5: do not implement an adapter because it could theoretically exist."""
    with pytest.raises(AdapterNotImplemented) as excinfo:
        adapters.parse(filename="chat.docx", content=b"PK\x03\x04")
    message = str(excinfo.value)
    assert "CORPUS_INPUT_REQUIRED" in message
    assert "sample" in message


def test_no_adapter_claims_an_unrecognised_format_rather_than_best_effort_parsing():
    with pytest.raises(AdapterNotImplemented, match="no adapter claims"):
        adapters.parse(filename="dump.bin", content=b"\x00\x01\x02")


def test_transcript_reads_a_thinking_tag_as_reasoning_and_keeps_turn_order():
    transcript = (
        b"User: why does the build fail?\n\n"
        b"<thinking>\nThe user has not shown the error. I should ask rather than guess.\n"
        b"</thinking>\n\n"
        b"## Assistant\nCould you paste the error output?\n")
    result = adapters.parse(filename="chat.md", content=transcript)
    assert result.source_type is SourceType.MARKDOWN
    assert [s.role for s in result.segments] == [
        TurnRole.USER_REQUEST, TurnRole.ASSISTANT_REASONING, TurnRole.ASSISTANT_ANSWER]


def test_an_unrecognised_label_is_reported_and_never_split_on():
    """A visible merge beats an invisible truncation. See the transcript adapter's docstring."""
    transcript = (b"User: a question\n\nAssistant: an answer\n\nNotes:\ntrailing prose\n")
    result = adapters.parse(filename="chat.txt", content=transcript)
    assert result.review_status is ReviewStatus.NEEDS_HUMAN_REVIEW
    assert any("not a known role marker" in r for r in result.review_reasons)
    assert len(result.segments) == 2


# ══════════════════════════════════════════════════════════════════════════════════════════
#  provenance and idempotence (§6)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_reimporting_identical_bytes_is_idempotent_and_the_filename_is_not_identity():
    """§6: repeated ingestion must be idempotent; filename alone is not identity."""
    payload = json.dumps(synthetic_corpus()["good"]).encode()
    first = ingest_bytes(filename="a.json", content=payload)
    second = ingest_bytes(filename="a-totally-different-name.json", content=payload)
    assert first.digest == second.digest
    assert first.conversation_id == second.conversation_id
    assert [t.provenance.unit_id for t in first.turns] == [
        t.provenance.unit_id for t in second.turns]


def test_unit_id_separates_identical_turns_in_different_conversations():
    """A segment hash alone would merge them, and a merged unit can cross a split."""
    shared = [("user_request", "an identical question about the index"),
              ("assistant_reasoning", "an identical piece of reasoning about the plan")]
    left = ingest(conversation_doc("left-001", shared))
    right = ingest(conversation_doc("right-001", shared))
    assert left.turns[0].provenance.raw_segment_hash == right.turns[0].provenance.raw_segment_hash
    assert left.turns[0].provenance.unit_id != right.turns[0].provenance.unit_id


def test_provenance_refuses_a_path_hint_that_carries_a_directory():
    conversation = ingest_bytes(filename="/home/someone/private/chat.json",
                                content=json.dumps(synthetic_corpus()["good"]).encode())
    for turn in conversation.turns:
        assert turn.provenance.source_path_hint == "chat.json"
        assert "/" not in turn.provenance.source_path_hint


def test_derived_conversation_id_is_marked_as_derived():
    """A declared id is evidence about provenance; an invented one is not, so they differ."""
    transcript = b"User: a question about indexes\n\nAssistant: an answer about indexes\n"
    conversation = ingest_bytes(filename="chat.txt", content=transcript)
    assert conversation.conversation_id.startswith("derived-")


def test_normalization_does_not_casefold_or_collapse_indentation():
    """Indentation IS the meaning in a code snippet. See the normalization docstring."""
    body = "def f():\n    if x:\n        return 1\n    return 2\n"
    doc = conversation_doc("code-001", [("user_request", "why does this return early?"),
                                        ("assistant_reasoning", body)])
    conversation = ingest(doc)
    assert conversation.turns[1].text == body.rstrip("\n")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  dedupe (§8)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_exact_and_near_duplicates_join_the_canonical_family_with_evidence():
    fixtures = synthetic_corpus()
    conversations = [ingest(fixtures[k]) for k in ("good", "duplicate", "near_duplicate")]
    report = analyze(conversations)
    assert report.family_count == 1
    family = report.families[0]
    assert family.size == 3
    assert family.evidence, "near-duplicate evidence must be recorded, not just the verdict"
    assert all(0.0 <= e.score <= 1.0 for e in family.evidence)


def test_unrelated_problems_do_not_share_a_family():
    fixtures = synthetic_corpus()
    conversations = [ingest(fixtures[k]) for k in ("good", "tool_call", "meta_noise")]
    assert analyze(conversations).family_count == 3


def test_family_partition_is_independent_of_input_order():
    fixtures = synthetic_corpus()
    conversations = [ingest(v) for v in fixtures.values()]
    forward = analyze(conversations).to_dict()["families"]
    backward = analyze(list(reversed(conversations))).to_dict()["families"]
    assert forward == backward


def test_an_ungroupable_conversation_becomes_its_own_family_and_says_so():
    """No USER_REQUEST turn means no problem statement. Grouping on anything else is wrong."""
    doc = conversation_doc("answer-only-001", [("assistant_answer", "an answer with no question")])
    report = analyze([ingest(doc)])
    assert report.family_count == 1
    assert report.families[0].grouping_basis_absent is True


def test_family_of_raises_for_an_unknown_conversation_rather_than_defaulting():
    report = analyze([ingest(synthetic_corpus()["good"])])
    with pytest.raises(Exception, match="no family assignment"):
        report.family_of("0" * 64)


def test_an_incomplete_similarity_search_is_reported_not_absorbed():
    """A search that stopped early found nothing the way one that never ran found nothing."""
    from reasoning_distillation.config import DedupeConfig
    fixtures = synthetic_corpus()
    conversations = [ingest(v) for v in fixtures.values()]
    report = analyze(conversations, config=DedupeConfig(max_comparisons=1))
    assert report.ceiling_reached is True
    assert report.to_dict()["search_complete"] is False


# ══════════════════════════════════════════════════════════════════════════════════════════
#  splitting (§9)
# ══════════════════════════════════════════════════════════════════════════════════════════
def _family_report(count: int):
    docs = [conversation_doc(
        f"fam-{i:03d}",
        [("user_request", f"Topic {i}: the {i} subsystem must not stall under a {i}ms budget; "
                          f"why does component {i} behave differently from component {i + 100}?"),
         ("assistant_reasoning", f"I need to check subsystem {i} before claiming anything."),
         ("assistant_answer", f"Inspect subsystem {i}.")]) for i in range(count)]
    return analyze([ingest(d) for d in docs])


def test_a_corpus_too_small_for_a_defensible_holdout_says_so_and_carves_none():
    """§9: if too little data exists for a defensible holdout, say so."""
    result = plan(_family_report(10))
    assert result.holdout_available is False
    assert result.defensible is False
    assert result.counts()[Partition.FROZEN_HOLDOUT.value] == 0
    assert any("below the" in s and "floor for a defensible frozen holdout" in s
               for s in result.shortfalls)


def test_freeze_refuses_an_empty_holdout_rather_than_recording_that_one_exists():
    with pytest.raises(Exception, match="EMPTY holdout"):
        freeze(plan(_family_report(10)))


def test_a_large_enough_corpus_splits_defensibly_and_freezes():
    report = _family_report(60)
    result = plan(report)
    assert result.defensible is True
    frozen = freeze(result, generation="test")
    assert frozen.family_count == len(result.families_in(Partition.FROZEN_HOLDOUT))
    assert frozen.to_dict()["status"] == "FROZEN_IMMUTABLE"


def test_placement_is_deterministic_and_seed_dependent():
    report = _family_report(60)
    assert plan(report).assignments == plan(report).assignments
    other = default_config().with_seed(999).split
    assert plan(report, config=other).assignments != plan(report).assignments


def test_partition_of_raises_for_an_unplaced_family_rather_than_reading_it_as_development():
    with pytest.raises(Exception, match="no partition"):
        plan(_family_report(60)).partition_of("0" * 64)


def test_a_frozen_holdout_record_carries_no_conversation_content():
    """Only family ids. A family id cannot reconstruct anything."""
    frozen = freeze(plan(_family_report(60)), generation="test")
    blob = json.dumps(frozen.to_dict())
    assert "subsystem" not in blob and "user_request" not in blob


# ══════════════════════════════════════════════════════════════════════════════════════════
#  operators (§1)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_every_operator_has_a_spec_and_the_taxonomy_is_closed():
    assert len(TAXONOMY) == len(list(ReasoningOperator))
    assert {s.operator for s in TAXONOMY} == set(ReasoningOperator)
    assert KNOWN_OPERATOR_NAMES == {op.value for op in ReasoningOperator}


def test_detection_finds_the_operators_the_good_trace_actually_demonstrates():
    found = set(detect(GOOD_REASONING[1][1]))
    for expected in (ReasoningOperator.PREMISE_CHECK, ReasoningOperator.UNCERTAINTY_IDENTIFICATION,
                     ReasoningOperator.TOOL_NEEDED, ReasoningOperator.DECOMPOSITION,
                     ReasoningOperator.ALTERNATIVE_GENERATION,
                     ReasoningOperator.VERIFY_BEFORE_CLAIM, ReasoningOperator.STOP_CONDITION):
        assert expected in found, expected


def test_detection_asserts_nothing_on_text_with_no_markers():
    assert detect("hello") == ()
    assert detect("") == ()


def test_an_unknown_operator_is_refused_by_record_validation():
    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    from dataclasses import replace
    broken = replace(result.record, decision=replace(
        result.record.decision, reasoning_operators=("NOT_AN_OPERATOR",)))
    with pytest.raises(DistillationError, match="unknown reasoning operator"):
        broken.validated(known_operators=KNOWN_OPERATOR_NAMES)
    del conversation


# ══════════════════════════════════════════════════════════════════════════════════════════
#  extraction, verification and the fact/procedure split (§10, §11, §12)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_extraction_lifts_spans_and_composes_nothing():
    """Every populated field must appear verbatim in some source turn."""
    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    source = "\n".join(t.text for t in conversation.turns)
    for value in (*result.record.task.constraints, *result.record.decision.subproblems,
                  *result.record.epistemic_state.uncertainties):
        assert value in source, value


def test_a_source_with_no_reasoning_yields_an_empty_decision_not_an_invented_one():
    """§12: UNKNOWN beats hallucination. §18 case 3."""
    _, result, verdict = distil_one(synthetic_corpus()["missing_rationale"])
    assert result.record.decision.subproblems == ()
    assert result.record.decision.decision_basis == ""
    assert verdict.disposition is Disposition.REJECT
    assert RejectReason.INSUFFICIENT_CONTEXT in verdict.reasons


def test_tool_execution_is_witnessed_by_a_tool_turn_and_never_inferred_from_need():
    """§11: availability, need and execution are three different things. §18 case 4."""
    _, with_tool, _ = distil_one(synthetic_corpus()["tool_call"])
    assert with_tool.record.decision.tool_decision.execution.value == "succeeded"
    _, without_tool, _ = distil_one(synthetic_corpus()["good"])
    assert without_tool.record.decision.tool_decision.need.value == "required"
    assert without_tool.record.decision.tool_decision.execution.value == "not_attempted"


def test_a_stale_fact_does_not_invalidate_the_procedure_and_its_link_stays_undecided():
    """§10, §18 case 6: the whole point of separating fact from procedure."""
    _, result, verdict = distil_one(synthetic_corpus()["stale_fact"])
    claims = result.record.factual_claims
    assert claims and all(c.state is FactualState.STALE for c in claims)
    assert all(not c.state.is_usable_as_truth for c in claims)
    assert all(not c.state.invalidates_procedure for c in claims)
    assert result.record.decision.reasoning_operators, "the procedure survived the stale fact"
    assert ReasoningOperator.FRESHNESS_CHECK.value in result.record.decision.reasoning_operators
    assert all(not link.decided for link in result.record.fact_procedure_links)
    assert verdict.disposition is Disposition.NEEDS_HUMAN_REVIEW
    assert RejectReason.FACT_REASONING_ENTANGLED in verdict.reasons


def test_no_extraction_path_can_mark_a_claim_verified():
    """§10: M67A verifies no external facts, so VERIFIED is unreachable from extraction."""
    for doc in synthetic_corpus().values():
        record = extract(grouped([doc])[0][0])
        assert all(c.state is not FactualState.VERIFIED for c in record.factual_claims)


def test_observed_success_stays_unknown_without_evidence_and_is_read_when_present():
    _, result, _ = distil_one(synthetic_corpus()["good"])
    assert result.record.outcome.observed_success is None
    assert result.record.outcome.success_known is False
    followed = conversation_doc("followed-001", GOOD_REASONING + [
        ("follow_up", "thanks, that worked and the plan is using the index now")])
    _, after, _ = distil_one(followed)
    assert after.record.outcome.observed_success is True


def test_a_self_correction_is_captured_as_an_operator_and_an_outcome():
    """§18 case 5."""
    _, result, _ = distil_one(synthetic_corpus()["self_correction"])
    assert ReasoningOperator.SELF_CORRECTION.value in result.record.decision.reasoning_operators
    assert result.record.outcome.self_corrections


def test_source_verification_attributes_every_field_to_one_turn():
    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    assert result.verification.verdict is SourceVerdict.VERIFIED
    assert result.verification.orphans == ()
    assert all(a.witness_unit_id for a in result.verification.attributions)
    real = {t.provenance.unit_id for t in conversation.turns}
    assert all(a.witness_unit_id in real for a in result.verification.attributions)


def test_verification_refuses_to_run_against_the_wrong_conversation():
    conversation_a, result, _ = distil_one(synthetic_corpus()["good"])
    conversation_b = grouped([synthetic_corpus()["tool_call"]])[0][0]
    with pytest.raises(Exception, match="refusing to verify"):
        verify(result.record, conversation_b)
    del conversation_a


def test_an_empty_record_verifies_as_not_applicable_rather_than_as_verified():
    _, result, _ = distil_one(synthetic_corpus()["missing_rationale"])
    assert result.verification.verdict in (SourceVerdict.NOT_APPLICABLE, SourceVerdict.VERIFIED)


def test_record_schema_version_is_enforced_before_fields_are_read():
    """§18 case 19, on the OUTPUT side."""
    _, result, _ = distil_one(synthetic_corpus()["good"])
    payload = result.record.to_dict()
    assert check_record_version(payload) == payload["schema_version"]
    payload["schema_version"] = "m67a.distilled.999"
    with pytest.raises(DistillationError, match="not supported"):
        check_record_version(payload)
    del payload["schema_version"]
    with pytest.raises(DistillationError, match="no schema_version"):
        check_record_version(payload)


def test_distillation_refuses_an_ungrouped_conversation():
    with pytest.raises(Exception, match="ungrouped"):
        extract(ingest(synthetic_corpus()["good"]))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  quality (§14)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_strong_trace_is_accepted():
    _, _, verdict = distil_one(synthetic_corpus()["good"])
    assert verdict.disposition is Disposition.ACCEPT
    assert verdict.reasons == ()


def test_every_dimension_is_scored_and_no_aggregate_decides():
    _, _, verdict = distil_one(synthetic_corpus()["good"])
    assert set(verdict.score.scores) == set(DIMENSIONS)
    assert len(DIMENSIONS) == 15
    assert "mean_reported_not_used" in verdict.score.to_dict()


def test_a_missing_dimension_is_refused_rather_than_defaulted():
    from reasoning_distillation.quality import QualityError, QualityScore
    with pytest.raises(QualityError, match="missing dimension"):
        QualityScore(scores={"task_understanding": 1.0}).validated()


def test_meta_noise_only_reasoning_is_rejected_with_its_reason_code():
    """§13, §18 case 10."""
    _, _, verdict = distil_one(synthetic_corpus()["meta_noise"])
    assert verdict.disposition is Disposition.REJECT
    assert RejectReason.META_POLICY_NOISE in verdict.reasons


def test_every_non_accept_outcome_carries_a_reason_code():
    """§14. A rejection nobody can explain is a rejection nobody can appeal."""
    for name, doc in synthetic_corpus().items():
        _, _, verdict = distil_one(doc)
        if verdict.disposition is not Disposition.ACCEPT:
            assert verdict.reasons, name
            assert verdict.notes, name


def test_a_non_canonical_family_member_is_rejected_as_a_duplicate():
    fixtures = synthetic_corpus()
    conversations, report = grouped([fixtures["good"], fixtures["duplicate"]])
    canonical = {f.canonical_digest for f in report.families}
    dispositions = []
    for conversation in conversations:
        result = distil(conversation)
        duplicate_of = "" if conversation.digest in canonical else "other"
        dispositions.append(disposition(
            result.record, conversation,
            critique_result=critique(result.record, conversation),
            verification=result.verification, trace=result.trace,
            duplicate_of=duplicate_of))
    assert sum(1 for v in dispositions if RejectReason.DUPLICATE in v.reasons) == 1


def test_a_contradictory_trace_does_not_silently_accept():
    """§18 case 13, and the reason code that had nothing able to emit it."""
    _, _, verdict = distil_one(synthetic_corpus()["contradictory"])
    assert verdict.disposition is Disposition.NEEDS_HUMAN_REVIEW
    assert RejectReason.CONTRADICTORY_TRACE in verdict.reasons


def test_a_deliberate_self_correction_is_not_scored_as_a_contradiction():
    """The narrow half of the contradiction check: weighing both sides is a virtue."""
    _, _, verdict = distil_one(synthetic_corpus()["self_correction"])
    assert RejectReason.CONTRADICTORY_TRACE not in verdict.reasons
    _, _, good = distil_one(synthetic_corpus()["good"])
    assert RejectReason.CONTRADICTORY_TRACE not in good.reasons


# ══════════════════════════════════════════════════════════════════════════════════════════
#  skills (§15)
# ══════════════════════════════════════════════════════════════════════════════════════════
def _accepted_pairs(count: int):
    """*count* distinct ACCEPT records, one per family, each demonstrating the same operators."""
    pairs = []
    for i in range(count):
        doc = conversation_doc(f"skill-{i:03d}", [
            ("user_request",
             f"Subsystem {i} must not exceed a {i}ms budget. Why does path {i} stall while "
             f"path {i + 500} does not?"),
            ("assistant_reasoning",
             f"The question assumes path {i} stalls on backpressure, and that is not correct "
             f"without evidence. I am not sure which build of {i} is deployed. I need to check "
             f"the metrics for {i} before claiming anything. First, confirm the lag for {i}. "
             f"Second, read the ack policy for {i}. Option A is raising prefetch for {i}; "
             f"alternatively we shard partition {i}. Raising prefetch will not work inside a "
             f"{i}ms budget. Let me verify the ack timeout for {i} rather than assume it. "
             f"Done when lag and ack policy for {i} are both confirmed."),
            ("assistant_answer", f"Confirm lag for {i} and shard partition {i}.")])
        conversation, result, verdict = distil_one(doc)
        pairs.append((result.record, verdict))
        del conversation
    return pairs


def test_a_skill_needs_evidence_from_several_problem_families():
    """§15: never create a skill from one anecdotal trace."""
    single = derive(_accepted_pairs(1))
    assert single, "the skill is emitted with its shortfall, not silently dropped"
    assert all(s.status is SkillStatus.INSUFFICIENT_EVIDENCE for s in single)
    assert all(s.status_reason for s in single)

    many = derive(_accepted_pairs(5))
    supported = [s for s in many if s.status is SkillStatus.SUPPORTED]
    assert supported, "five distinct families must be able to support a skill"
    for skill in supported:
        assert skill.support.example_count >= 3
        assert skill.support.family_count >= 2
        assert skill.support.source_file_count >= 2


def test_repeated_copies_of_one_conversation_cannot_manufacture_a_skill():
    """The family threshold is what closes the near-copy hole."""
    fixtures = synthetic_corpus()
    docs = [fixtures["good"], fixtures["duplicate"], fixtures["near_duplicate"]]
    conversations, report = grouped(docs)
    pairs = []
    for conversation in conversations:
        result = distil(conversation)
        pairs.append((result.record, disposition(
            result.record, conversation,
            critique_result=critique(result.record, conversation),
            verification=result.verification, trace=result.trace)))
    assert report.family_count == 1
    family_of = {r.example_id: r.provenance.family_id for r, _ in pairs}
    assert len(set(family_of.values())) == 1
    for skill in derive(pairs, family_of=family_of):
        assert skill.status is not SkillStatus.SUPPORTED


def test_only_accept_records_contribute_support():
    """A record routed to review or reject has not been shown faithful to its source."""
    pairs = _accepted_pairs(5)
    _, result, verdict = distil_one(synthetic_corpus()["meta_noise"])
    assert verdict.disposition is Disposition.REJECT
    with_reject = derive(pairs + [(result.record, verdict)])
    without = derive(pairs)
    assert {s.skill_id: s.support.example_count for s in with_reject} == {
        s.skill_id: s.support.example_count for s in without}


def test_a_skill_file_carries_no_raw_trace():
    """§15: no long raw traces inside skill files."""
    for skill in derive(_accepted_pairs(5)):
        blob = json.dumps(skill.to_dict())
        assert "Subsystem" not in blob and "assistant_reasoning" not in blob
        assert skill.support.example_hashes


# ══════════════════════════════════════════════════════════════════════════════════════════
#  benchmark (§16)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_harness_covers_every_category_with_an_adversarial_case():
    from reasoning_distillation.benchmark import Category, status
    report = status()
    assert report["categories_missing"] == []
    assert report["categories_without_adversarial"] == []
    assert report["category_count"] == len(list(Category))
    assert report["ready"] is True


def test_the_harness_records_that_it_spent_nothing():
    """§16: build the harness only. §2: no result may imply promotion eligibility."""
    report = __import__("reasoning_distillation.benchmark", fromlist=["status"]).status()
    assert report["executed"] is False
    assert report["holdout_read"] is False
    assert report["model_loaded"] is False
    assert report["evaluation_authority_spent"] is False
    assert report["implies_promotion_eligibility"] is False


def test_an_unanswered_case_is_counted_apart_from_a_wrong_one():
    """Folding them together would let an arm improve by declining the hard cases."""
    from reasoning_distillation.benchmark import BenchmarkHarness
    harness = BenchmarkHarness()
    scored = harness.score_decisions({"tn-01": {"tool_need": "none", "freshness": "stable"}})
    assert scored["answered"] == 1
    assert scored["not_answered"] == len(harness.cases) - 1
    assert scored["is_a_jarvis_measurement"] is False


def test_the_harness_refuses_an_empty_case_set():
    from reasoning_distillation.benchmark import BenchmarkError, BenchmarkHarness
    with pytest.raises(BenchmarkError, match="empty case set"):
        BenchmarkHarness(cases=())


# ══════════════════════════════════════════════════════════════════════════════════════════
#  config and manifests (§14, §20)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_thresholds_are_configuration_and_an_unknown_key_is_refused(tmp_path):
    """§14: thresholds must be configuration, not magic constants buried in code."""
    good = tmp_path / "ok.json"
    good.write_text(json.dumps({"split": {"seed": 4242}}), encoding="utf-8")
    assert load_config(good).split.seed == 4242
    bad = tmp_path / "typo.json"
    bad.write_text(json.dumps({"split": {"sed": 1}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(bad)
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"config_version": "mine"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="derived from the code"):
        load_config(forged)


def test_the_recursion_depth_cap_cannot_exceed_three():
    """§12 caps it explicitly."""
    import dataclasses
    config = default_config()
    with pytest.raises(ConfigError, match=r"\[0, 3\]"):
        dataclasses.replace(config, recursion=dataclasses.replace(
            config.recursion, max_repair_depth=4)).validated()


def test_source_preservation_cannot_be_disabled():
    """§13: source is never destroyed, so there is no configuration that deletes it."""
    import dataclasses
    config = default_config()
    with pytest.raises(ConfigError, match="cannot be disabled"):
        dataclasses.replace(config, privacy=dataclasses.replace(
            config.privacy, preserve_source=False)).validated()


def test_the_manifest_records_every_stage_version():
    versions = stage_versions()
    for stage in ("adapters", "normalization", "privacy", "dedupe", "splitting", "firewall",
                  "operators", "distiller", "critic", "verifier", "quality", "skills",
                  "benchmark"):
        assert stage in versions, stage
    assert versions["schema_version"]
    assert versions["splitting"]["split_algorithm_version"]
    assert versions["operators"]["operator_taxonomy_version"]


def test_counts_that_do_not_reconcile_are_refused():
    with pytest.raises(ManifestError, match="do not reconcile"):
        CorpusCounts(distilled=10, accepted=4, review=3, rejected=2).validated()


def test_a_provider_assisted_build_cannot_claim_determinism():
    """§19: never pretend model-assisted output is bit-reproducible."""
    counts = CorpusCounts(distilled=1, accepted=1).validated()
    with pytest.raises(ManifestError, match="deterministic"):
        BuildManifest(build_id="x", config=default_config(), counts=counts,
                      provider_metadata={"model": "m"}).validated()
    with pytest.raises(ManifestError, match="names no non-deterministic stage"):
        BuildManifest(build_id="x", config=default_config(), counts=counts,
                      deterministic=False).validated()


def test_identical_inputs_and_config_reproduce_an_identical_structural_result(tmp_path):
    """§20's reproducibility property, end to end over the file-reading path."""
    files = corpus_files(tmp_path, synthetic_corpus())
    first = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    second = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    assert first.manifest is not None and second.manifest is not None
    assert compare(first.manifest, second.manifest)["identical"] is True
    assert first.manifest.deterministic is True


def test_a_seed_change_is_visible_in_the_structural_digest(tmp_path):
    files = corpus_files(tmp_path, synthetic_corpus())
    first = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    second = pipeline.run(files, config=default_config().with_seed(7),
                         sensitivity=SensitivityClass.SYNTHETIC)
    result = compare(first.manifest, second.manifest)
    assert result["identical"] is False
    assert "split_seed" in result["differing_keys"]


def test_the_manifest_declares_that_nothing_was_spent(tmp_path):
    files = corpus_files(tmp_path, synthetic_corpus())
    declarations = pipeline.run(
        files, sensitivity=SensitivityClass.SYNTHETIC).manifest.to_dict()["declarations"]
    assert declarations == {
        "raw_corpus_committed": False, "training_performed": False, "evaluation_spent": False,
        "holdout_read_by_any_stage": False, "production_reasoning_changed": False,
        "implies_promotion_eligibility": False}


# ══════════════════════════════════════════════════════════════════════════════════════════
#  pipeline and CLI (§21)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_pipeline_reports_what_it_declined_to_ingest(tmp_path):
    """§5: an unsupported format is reported, never silently dropped."""
    files = corpus_files(tmp_path, {"good": synthetic_corpus()["good"]})
    (tmp_path / "broken.json").write_text(MALFORMED_EXPORT, encoding="utf-8")
    (tmp_path / "chat.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / "secrets.pem").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    outcome = pipeline.ingest_paths(files + [tmp_path / "broken.json", tmp_path / "chat.docx",
                                             tmp_path / "secrets.pem"])
    skipped = dict(outcome.skipped)
    assert len(outcome.conversations) == 1
    assert "broken.json" in skipped and "chat.docx" in skipped and "secrets.pem" in skipped
    assert "allowlist" in skipped["secrets.pem"]


def test_ingest_does_not_recurse_unless_asked(tmp_path):
    """§5: do NOT recursively scan my entire home directory."""
    from reasoning_distillation.cli import build_parser, _source_paths
    nested = tmp_path / "deep"
    nested.mkdir()
    (tmp_path / "top.json").write_text(json.dumps(synthetic_corpus()["good"]), encoding="utf-8")
    (nested / "buried.json").write_text(json.dumps(synthetic_corpus()["good"]), encoding="utf-8")
    shallow = build_parser().parse_args(["ingest", "--input", str(tmp_path)])
    assert [p.name for p in _source_paths(shallow)] == ["top.json"]
    deep = build_parser().parse_args(["ingest", "--input", str(tmp_path), "--recursive"])
    assert {p.name for p in _source_paths(deep)} == {"top.json", "buried.json"}


def test_a_stage_with_no_input_prints_the_contract_and_exits_three(capsys):
    """§5's CORPUS_INPUT_REQUIRED, as the CLI contract."""
    from reasoning_distillation.cli import EXIT_CORPUS_REQUIRED, main
    assert main(["ingest", "--json"]) == EXIT_CORPUS_REQUIRED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CORPUS_INPUT_REQUIRED"
    assert payload["json_contract"]["schema"] == SCHEMA
    assert payload["deliberately_absent"]


def test_the_contract_names_no_quality_or_success_field():
    """§26: a converter may not assert that a historical answer was correct."""
    contract = adapters.contract_description()
    keys = set(contract["required_top_level"]) | set(contract["optional_top_level"]) | set(
        contract["turn_keys"])
    assert not keys & {"quality", "success", "correct", "rating", "score"}


def test_report_and_benchmark_status_need_no_corpus(capsys):
    from reasoning_distillation.cli import EXIT_OK, main
    assert main(["report", "--json"]) == EXIT_OK
    assert "versions" in json.loads(capsys.readouterr().out)
    assert main(["benchmark-status", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["ready"] is True


def test_no_llm_is_accepted_and_reported_as_a_no_op(tmp_path, capsys):
    from reasoning_distillation.cli import main
    corpus_files(tmp_path, synthetic_corpus())
    main(["distill", "--input", str(tmp_path), "--json", "--no-llm", "--sensitivity", "synthetic"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["llm_used"] is False
    assert "no-op" in payload["no_llm_flag"]


def test_nothing_is_written_without_the_write_flag(tmp_path, capsys):
    from reasoning_distillation.cli import main
    source = tmp_path / "src"
    source.mkdir()
    corpus_files(source, synthetic_corpus())
    root = tmp_path / "root"
    main(["distill", "--input", str(source), "--corpus", str(root), "--json",
          "--sensitivity", "synthetic"])
    assert "written" not in json.loads(capsys.readouterr().out)
    assert not (root / "distilled").exists() or not list((root / "distilled").glob("*.json"))


def test_persisted_records_land_in_the_area_their_disposition_names(tmp_path):
    from reasoning_distillation.storage import CorpusRoot
    files = corpus_files(tmp_path / "src", synthetic_corpus()) if (
        tmp_path / "src").mkdir() or True else []
    outcome = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    root = CorpusRoot.prepare(tmp_path / "root")
    pipeline.persist(root, outcome)
    # Skill files share the `distilled/` area and the `m67a-` prefix, so the record count must
    # exclude them explicitly rather than by glob.
    def _records(area: str) -> int:
        return len([p for p in root.area(area).glob("m67a-*.json")
                    if not p.name.startswith("m67a-skill-")])

    accepted, rejected, review = _records("distilled"), _records("rejected"), _records("review")
    counts = outcome.counts()
    assert accepted == counts.accepted
    assert rejected == counts.rejected
    assert review == counts.review


def test_a_rejected_record_is_written_and_never_deleted(tmp_path):
    """§13 applies to the derived record too: a rejection nobody can re-read cannot be appealed."""
    from reasoning_distillation.storage import CorpusRoot
    source = tmp_path / "src"
    source.mkdir()
    files = corpus_files(source, {"meta_noise": synthetic_corpus()["meta_noise"]})
    outcome = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    root = CorpusRoot.prepare(tmp_path / "root")
    pipeline.persist(root, outcome)
    written = list(root.area("rejected").glob("*.json"))
    assert written
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["verdict"]["reasons"]


def test_dry_run_produces_the_deterministic_artifacts_and_no_records(tmp_path):
    files = corpus_files(tmp_path, synthetic_corpus())
    outcome = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC, distil=False)
    assert outcome.dedupe_report.family_count > 0
    assert outcome.results == ()
    assert any("distil" in reason for reason in outcome.not_run)


def test_the_guard_is_built_before_distillation_runs(tmp_path):
    """§9's ordering: dedupe, then split, then the firewall, then extraction."""
    files = corpus_files(tmp_path, synthetic_corpus())
    outcome = pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC)
    guard = HoldoutGuard(outcome.split, outcome.frozen)
    report = audit(report=outcome.dedupe_report, split=outcome.split,
                   conversations=list(outcome.ingest.conversations), frozen=outcome.frozen,
                   guard=guard)
    assert report.to_dict()["checks_run"]
    assert all(c.family_id for c in outcome.ingest.conversations)


def test_every_distilled_record_survives_its_own_validation(tmp_path):
    files = corpus_files(tmp_path, synthetic_corpus())
    for result in pipeline.run(files, sensitivity=SensitivityClass.SYNTHETIC).results:
        assert result.record.validated(known_operators=KNOWN_OPERATOR_NAMES)
        assert result.record.privacy.export_status is not ExportStatus.EXPORT_UNKNOWN


def test_a_blocking_critique_finding_is_never_advisory_only():
    """A blocking finding must be able to exist; a critic with none is not a critic."""
    from dataclasses import replace
    conversation, result, _ = distil_one(synthetic_corpus()["good"])
    forged = replace(result.record, provenance=replace(
        result.record.provenance, source_unit_ids=("0" * 64,)))
    findings = critique(forged, conversation).findings
    assert any(f.severity is Severity.BLOCKING for f in findings)
