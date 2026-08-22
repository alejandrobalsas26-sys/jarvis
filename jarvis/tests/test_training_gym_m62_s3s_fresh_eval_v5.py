"""V69 M62 S3S — the fresh holdout ``m62-defensive-eval v5``, frozen candidate-blind.

WHAT THESE TESTS ARE FOR
------------------------
``eval-v4`` is spent. S3Q measured candidate 003 against it and S3R then drew a body-free
termination diagnosis from its per-task results — so under the standing **D35** rule it is
development evidence, not a fresh eligibility instrument, for any fourth candidate. ``v5``
is the fifth holdout, and S3S froze it **before candidate 004 exists at all**: no
identifier, no configuration, no plan, no adapter, no ``train-v3`` and no ``TRAIN``
authority.

That creates five ways to get it wrong, and every one of them is measured here rather than
promised in a document:

  * **Pseudo-freshness.** New task ids over recycled material would look fresh and measure
    nothing new. Ids, prompts, targets, canonical task hashes, candidate hashes, prompt
    hashes and target hashes are asserted disjoint from ``v1``, ``v2``, ``v3`` AND ``v4``,
    the lexical near-duplicate comparator is run across all four, and the existing 16-check
    leakage analyser is run over both training corpora.
  * **Shaping the exam around the last result.** S3R measured a general stopping pressure —
    6 of 36 tasks at the output ceiling against the baseline's 2 — and one structured JSON
    parse failure. The tempting move is to shorten targets, add structured tasks or reward
    early stopping. The per-``(split, family)`` distribution, the decision classes, the
    grader assignment and the response schemas are asserted **identical to v4**, cell for
    cell, and the format-only contract sentence is asserted byte-identical.
  * **Moving the instrument.** D37 and D38 are frozen. The gate policy digest, the metric
    policy digest, the generation policy digest, ``max_new_tokens`` and the absence of any
    D38 gate are pinned here.
  * **Building the student in the same session.** No candidate 004 identity, configuration,
    plan, adapter or receipt may exist. Asserted over the repository.
  * **Publishing the holdout in the documents written about it.** The body-free surfaces a
    future candidate-004 session will read are scanned for v5 prompts, targets and long
    shingles of either.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN.

This file deliberately contains **no v5 prompt, target or task body**. Every assertion is
computed from the generator, so a future reader of the test suite cannot learn the holdout
from it.
"""
from __future__ import annotations

import getpass
import json
import re

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.datasets.leakage import LeakageVerdict
from training_gym.datasets.similarity import (
    BLOCK_THRESHOLD,
    WARN_THRESHOLD,
    compare_groups,
    signature,
)
from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
from training_gym.evaluation.policy import GatePolicy, MetricPolicy
from training_gym.schemas import sha256_text

pytest.importorskip("scripts.build_training_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402
from scripts import build_training_corpus as QC  # noqa: E402

# ── The identities S3S may not move ───────────────────────────────────────────
EVAL_V1_MANIFEST = (
    "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b")
EVAL_V2_MANIFEST = (
    "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60")
EVAL_V3_MANIFEST = (
    "7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0")
EVAL_V4_MANIFEST = (
    "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5")
EVAL_V4_PACK = (
    "95b4e2f6ffb495735113c236f051073449f4562b780eddfc5fe8a7f76bddf2b7")
TRAIN_V1_MANIFEST = (
    "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a")
TRAIN_V2_MANIFEST = (
    "24ceb1e0677b14aaccaea2b667e6d7388530e73f2df4d7a463368500d818fc0f")

# ── The identities S3S freezes. Recorded here so the freeze is enforced, not filed ──
EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")
EVAL_V5_PACK = (
    "287a9fb61e3feab510763d834f77a75c3a016fe27ba4d04a4ac86c588c09fed6")
#: A digest over the sorted canonical task hashes. This is the body-free set identity a
#: future candidate-design session binds ``v5`` by, WITHOUT reading a single task body.
EVAL_V5_TASK_HASH_SET = (
    "cda48cf5c599021f7298a430373e6a1c3b03df01448e2735e2c6c825203b2b0d")
EVAL_V5_PROMPT_HASH_SET = (
    "239c6402647c799b61e373c6748e5fb13bc0157dfd1d1c30a45db1c74f487bd2")
EVAL_V5_TARGET_HASH_SET = (
    "47dbb2a08b84f264686859eafa539c0b4206b410cc27008f8898436ad3064ae8")

# ── Frozen instrument identities (S3G / S3M.1 / S3M.2) ────────────────────────
GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
METRIC_POLICY_HASH = (
    "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")
GENERATION_POLICY_HASH = (
    "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")

PRIOR_VERSIONS = ("v1", "v2", "v3", "v4")
ALL_SPLITS = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
              DatasetSplit.ADVERSARIAL)

#: The body-free surfaces a candidate-004 session is expected to read. None of them may
#: carry v5 material — see :func:`test_the_body_free_surfaces_carry_no_v5_material`.
BODY_FREE_SURFACES = (
    "PROGRESS.md",
    "state/m62/current.json",
    "jarvis/docs/V69_M62_S3S_EVAL_V5_FREEZE.md",
)


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def eval_root(tmp_path_factory):
    """A root holding all five held-out versions, built by their own generator."""
    root = tmp_path_factory.mktemp("m62-s3s-eval")
    BC.build(root, dataset_version="v5")   # materialises v1 -> v2 -> v3 -> v4 -> v5
    return root


@pytest.fixture(scope="module")
def v5_rows():
    return BC.corpus_v5()


def _candidates(rows):
    return [BC.make_candidate(row) for row in rows]


def _pack(root, version="v5"):
    return build_task_pack_from_dataset(root=root, dataset_id=BC.DATASET_ID,
                                        dataset_version=version, splits=ALL_SPLITS)


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════════
#  1-4. v5 exists as a NEW version, with a declared lineage
# ══════════════════════════════════════════════════════════════════════════════
def test_v5_is_a_new_version_and_the_old_ones_still_exist():
    assert sorted(BC.CORPUS_VERSIONS) == ["v1", "v2", "v3", "v4", "v5"]
    assert BC.LATEST_DATASET_VERSION == "v5"


def test_v5_declares_an_explicit_lineage_onto_v4_rather_than_discovering_one():
    """D34: a parent is DECLARED. That v4 is spent does not change its ancestry role."""
    assert BC.canonical_parent_for("v5") == ("v4", EVAL_V4_MANIFEST)
    assert BC.CANONICAL_V4_MANIFEST == EVAL_V4_MANIFEST


def test_an_undeclared_version_is_refused_rather_than_promoted_as_a_genesis():
    with pytest.raises(ValueError, match="canonical lineage"):
        BC.canonical_parent_for("v6")


def test_v5_is_not_an_alias_for_v4():
    v4, v5 = BC.corpus_v4(), BC.corpus_v5()
    assert len(v4) == len(v5) == 36
    assert [r[2] for r in v4] != [r[2] for r in v5]


# ══════════════════════════════════════════════════════════════════════════════
#  5-8. The frozen evaluation contract, preserved exactly
# ══════════════════════════════════════════════════════════════════════════════
def test_v5_holds_thirty_six_tasks_in_the_frozen_splits(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v5")
    counts: dict[str, int] = {}
    for record in manifest.candidates:
        counts[record.split.value] = counts.get(record.split.value, 0) + 1
    assert counts == {"hidden_evaluation": 12, "security_regression": 12,
                      "adversarial": 12}
    assert sum(counts.values()) == 36
    assert "train" not in counts and "validation" not in counts


def test_v5_family_distribution_is_the_frozen_one(v5_rows):
    counts: dict[str, int] = {}
    for _split, family, _tid, _p, _t in v5_rows:
        counts[family] = counts.get(family, 0) + 1
    assert counts == {"SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
                      "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}


def test_v5_keeps_every_gate_denominator_cell_for_cell(v5_rows):
    """The contract is the per-(split, family) table, not merely its margins.

    Re-weighting inside a preserved margin would keep 12/9/9/6 while changing what QG-1
    and FG-1 count. This asserts v5 against v4's table cell by cell, derived rather than
    restated.
    """
    def table(rows):
        cells: dict[tuple[str, str], int] = {}
        for split, family, _tid, _p, _t in rows:
            cells[(split, family)] = cells.get((split, family), 0) + 1
        return cells

    assert table(v5_rows) == table(BC.corpus_v4()) == table(BC.corpus_v3())


def test_v5_decision_classes_are_twelve_six_eighteen(eval_root):
    built = _pack(eval_root)
    counts: dict[str, int] = {}
    for task in built.pack.tasks:
        counts[task.kind.value] = counts.get(task.kind.value, 0) + 1
    assert counts == {"required_refusal": 12, "required_completion": 6,
                      "completion": 18}


# ══════════════════════════════════════════════════════════════════════════════
#  9-13. Freshness — more than new task ids
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v5_task_id_is_unique(v5_rows):
    ids = [row[2] for row in v5_rows]
    assert len(set(ids)) == len(ids) == 36


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v5_task_id_prompt_or_target_appears_in_a_prior_holdout(version, v5_rows):
    prior = BC.corpus_for(version)
    assert {r[2] for r in v5_rows} & {r[2] for r in prior} == set()
    assert {r[3] for r in v5_rows} & {r[3] for r in prior} == set()
    assert {r[4] for r in v5_rows} & {r[4] for r in prior} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v5_canonical_task_hash_appears_in_a_prior_holdout(version, v5_rows):
    """The strongest identity the pipeline has: a spec hash over the whole task."""
    mine = {c.task_hash for c in _candidates(v5_rows)}
    assert len(mine) == 36
    assert mine & {c.task_hash for c in _candidates(BC.corpus_for(version))} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v5_prompt_hash_candidate_hash_or_target_hash_collides(version, v5_rows):
    mine = _candidates(v5_rows)
    theirs = _candidates(BC.corpus_for(version))
    assert ({sha256_text(c.user_prompt) for c in mine}
            & {sha256_text(c.user_prompt) for c in theirs}) == set()
    assert {c.target_hash for c in mine} & {c.target_hash for c in theirs} == set()
    assert ({c.candidate_hash() for c in mine}
            & {c.candidate_hash() for c in theirs}) == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v5_task_is_a_lexical_near_duplicate_of_a_prior_holdout_task(version,
                                                                       v5_rows):
    """Exact disjointness is not freshness: a rename-and-reword would pass it.

    The production near-duplicate comparator — the same character n-gram and token
    shingle machinery the leakage analyser runs across the train/held-out boundary — is
    run here across the holdout/holdout boundary instead, where nothing else runs it.
    NOT ONE PAIR may reach even the WARNING threshold.
    """
    def sigs(rows):
        return [signature(c.candidate_id, c.user_prompt + "\n" + c.target_text,
                          family=c.task_family) for c in _candidates(rows)]

    result = compare_groups(sigs(v5_rows), sigs(BC.corpus_for(version)),
                            threshold=WARN_THRESHOLD, max_comparisons=100_000)
    assert result.comparisons > 0
    assert result.ceiling_reached is False
    assert [(h.left_key, h.right_key, round(h.score, 3)) for h in result.hits] == []
    assert WARN_THRESHOLD < BLOCK_THRESHOLD


def test_the_body_free_set_identities_are_the_frozen_ones(v5_rows):
    """What a future candidate-design session binds v5 by, without reading a body."""
    cands = _candidates(v5_rows)
    assert sha256_text("\n".join(sorted(c.task_hash for c in cands))) \
        == EVAL_V5_TASK_HASH_SET
    assert sha256_text("\n".join(sorted(sha256_text(c.user_prompt) for c in cands))) \
        == EVAL_V5_PROMPT_HASH_SET
    assert sha256_text("\n".join(sorted(c.target_hash for c in cands))) \
        == EVAL_V5_TARGET_HASH_SET


# ══════════════════════════════════════════════════════════════════════════════
#  14-18. Training leakage
# ══════════════════════════════════════════════════════════════════════════════
def test_v5_is_one_of_the_held_out_versions_the_training_corpus_is_checked_against():
    """A version absent from this tuple is a version nothing is ever checked against."""
    assert "v5" in QC.HELD_OUT_VERSIONS
    assert sorted(QC.HELD_OUT_VERSIONS) == ["v1", "v2", "v3", "v4", "v5"]


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_neither_training_corpus_leaks_into_v5(train_version):
    """The existing analyser, unchanged, over every train-side split it already reads."""
    report = QC.leakage_against_held_out("v5", train_version=train_version)
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["finding_count"] == 0
    assert report["blocking_finding_count"] == 0
    assert report["blocks_finalization"] is False
    assert report["comparisons"] > 0
    assert report["ceiling_reached"] is False


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_no_exact_training_text_appears_anywhere_in_v5(train_version, v5_rows):
    """Exact containment, measured rather than inferred from a verdict."""
    rows = QC.curriculum_for(train_version)
    ids = {r[3] for r in rows}
    prompts = {r[4] for r in rows}
    targets = {r[5] for r in rows}
    for _split, _family, task_id, prompt, target in v5_rows:
        assert task_id not in ids
        assert prompt not in prompts
        assert target not in targets


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_semantic_leakage_is_reported_unavailable_and_never_clean(train_version):
    """Exact cleanliness is not proof of semantic independence, and must not be sold as it.

    No embedding backend is bundled and none was added for S3S — loading a model merely
    to produce a semantic claim is exactly what this milestone is forbidden to do. The
    analyser reports the check as unavailable, and this test exists so a future reader
    cannot mistake ``verdict: clean`` for a semantic result.
    """
    report = QC.leakage_against_held_out("v5", train_version=train_version)
    assert report["checks_unavailable"] == ["semantic_similarity"]
    assert "semantic_similarity" not in report["checks_run"]
    assert len(report["checks_run"]) == 16


def test_v5_leaks_no_answer_into_its_own_model_facing_material(eval_root):
    """A prompt that already carries its answer scores copying, not reasoning."""
    from training_gym.evaluation.task_pack import target_leaked_into

    built = _pack(eval_root)
    for task, (_split, _family, task_id, _p, target) in zip(
            sorted(built.pack.tasks, key=lambda t: t.task_id),
            sorted(BC.corpus_v5(), key=lambda r: r[2]), strict=True):
        assert task.task_id == task_id
        assert not target_leaked_into(task.user_prompt, target)
        assert not target_leaked_into(task.system_prompt, target)


# ══════════════════════════════════════════════════════════════════════════════
#  19-22. Sanitization, host identity, secrets, private paths
# ══════════════════════════════════════════════════════════════════════════════
def test_no_v5_row_would_be_rewritten_by_the_promotion_sanitizer(v5_rows):
    """D36's fail-closed control, over the authored material, before a byte is written."""
    assert BC.sanitization_stability_problems(
        (task_id, field, text)
        for _s, _f, task_id, prompt, target in v5_rows
        for field, text in (("prompt", prompt), ("target", target))) == []


def test_the_stability_control_is_not_vacuous_on_this_host():
    """A control that cannot fire proves nothing about the corpus it clears."""
    user = getpass.getuser()
    if len(user) < 4:
        pytest.skip("this host's account name is too short to be substituted at all")
    problems = BC.sanitization_stability_problems(
        [("probe", "target", f"a note written by {user} about the incident")])
    assert len(problems) == 1 and "D36" in problems[0]


def test_no_host_identity_buried_in_a_v5_word_can_perturb_the_corpus(monkeypatch,
                                                                    v5_rows):
    """The D36 rule, exercised against EVERY interior of EVERY long word in this corpus.

    S3N probed the first twelve interiors; this probes all of them, and states the
    boundary exactly. ``_identity_pattern`` refuses a match only where the literal is
    flanked by ASCII letters on BOTH sides — the one case where a hit cannot be an
    identity. A four-letter sequence that is ALSO an ordinary standalone word somewhere
    in the corpus is therefore still substituted, deliberately: an operator whose account
    really is named that must still be redacted. Those are separated out and reported,
    not silently skipped, so the property under test stays the D36 one.
    """
    import training_gym.teachers.sanitization as S

    text = " ".join(p + " " + t for _s, _f, _t, p, t in v5_rows)
    words = {w for w in re.findall(r"[A-Za-z]{8,}", text)}
    interiors = sorted({w[2:6].lower() for w in words if len(w) >= 8})
    assert len(interiors) >= 100, "expected the corpus to offer many probes"

    def standalone(literal: str) -> bool:
        return re.search(r"(?<![A-Za-z])" + re.escape(literal) + r"(?![A-Za-z])",
                         text, re.IGNORECASE) is not None

    interior_only = [i for i in interiors if not standalone(i)]
    assert len(interior_only) >= len(interiors) - 12

    monkeypatch.setattr(S, "_local_hostname", lambda: "")
    for interior in interior_only:
        monkeypatch.setattr(S, "_local_username", lambda i=interior: i)
        assert BC.sanitization_stability_problems(
            (task_id, field, value)
            for _s, _f, task_id, prompt, target in v5_rows
            for field, value in (("prompt", prompt), ("target", target))
        ) == [], interior


def test_v5_carries_no_secret_no_private_path_and_no_authority_token(v5_rows):
    from core.redaction_policy import scan_for_leaks

    for _split, _family, task_id, prompt, target in v5_rows:
        for name, text in (("prompt", prompt), ("target", target)):
            assert list(scan_for_leaks(text)) == [], f"{task_id} {name}"
            assert "/home/" not in text and "/Users/" not in text
            assert "TRAIN:" not in text and "EVAL:" not in text
            text.encode("ascii")


# ══════════════════════════════════════════════════════════════════════════════
#  23-27. Schema and task-family contract validation
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v5_target_is_exactly_one_json_object_on_one_line(v5_rows):
    for _split, _family, task_id, _prompt, target in v5_rows:
        assert "\n" not in target, task_id
        assert target.strip() == target, task_id
        assert not target.startswith("```"), task_id
        assert "<think" not in target, task_id
        parsed = json.loads(target)
        assert isinstance(parsed, dict), task_id


def test_every_v5_target_satisfies_the_declared_response_schema(eval_root):
    jsonschema = pytest.importorskip("jsonschema")

    built = _pack(eval_root)
    by_id = {t.task_id: t for t in built.pack.tasks}
    for _split, _family, task_id, _prompt, target in BC.corpus_v5():
        schema = by_id[task_id].expected_output_schema
        jsonschema.validate(json.loads(target), schema)


def test_v5_states_the_same_format_only_output_contract_v4_does(v5_rows):
    """Same behavioural contract, different task material — the whole point of v5."""
    assert BC.CONTRACT_FAMILIES == frozenset({"STRUCTURED_REPORT"})
    for _split, family, task_id, prompt, _target in v5_rows:
        if family in BC.CONTRACT_FAMILIES:
            assert prompt.endswith(BC.STRUCTURED_OUTPUT_CONTRACT), task_id
        else:
            assert BC.STRUCTURED_OUTPUT_CONTRACT not in prompt, task_id


def test_the_tool_call_family_is_still_uninstructed_under_d28(v5_rows):
    """D28 is NOT solved here. Instructing a format the backend cannot read is not a fix."""
    tool_prompts = [p for _s, family, _t, p, _tg in v5_rows
                    if family == "TOOL_CALL_SCHEMA"]
    assert len(tool_prompts) == 6
    assert all(BC.STRUCTURED_OUTPUT_CONTRACT not in p for p in tool_prompts)


def test_v5_refusal_targets_stay_unrecognised_by_the_detector_because_d29_is_open(
        v5_rows):
    """The D29 limitation travels into v5 unchanged, and that is deliberate.

    ``m62-defensive-eval``'s required-refusal answers are ``{"decision": "refuse", ...}``
    objects, and ``looks_like_refusal`` recognises none of them. Rewriting v5's refusal
    phrasing so that it *is* recognised would address D29 as a rider on a corpus milestone
    AND change what QG-1 and SV-5 measure between candidate 003 and any successor. If this
    test ever fails, D29 has been addressed somewhere and this corpus should be revisited
    deliberately rather than silently.
    """
    from training_gym.evaluation.scoring import looks_like_refusal

    refusals = [t for _s, family, _tid, _p, t in v5_rows if family == "SAFETY_REFUSAL"]
    assert len(refusals) == 12
    assert not any(looks_like_refusal(t) for t in refusals)


def test_no_safe_v5_target_reads_as_a_refusal(v5_rows):
    """A required-completion answer that reads as a refusal would score its own failure."""
    from training_gym.evaluation.scoring import looks_like_refusal

    safe = [(tid, t) for _s, family, tid, _p, t in v5_rows
            if family != "SAFETY_REFUSAL"]
    assert len(safe) == 24
    assert [tid for tid, t in safe if looks_like_refusal(t)] == []


# ══════════════════════════════════════════════════════════════════════════════
#  28-30. Evaluation-only, and the train-side refusal
# ══════════════════════════════════════════════════════════════════════════════
def test_v5_is_evaluation_only_and_never_dataset_eligible(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v5")
    for record in manifest.candidates:
        assert record.evaluation_only is True, record.candidate_id
        assert record.dataset_eligible is False, record.candidate_id


@pytest.mark.parametrize("destination", (DatasetSplit.TRAIN, DatasetSplit.VALIDATION))
def test_no_training_export_can_ever_include_v5(destination):
    """``plan_splits`` refuses a forced train-side destination before a hash is computed."""
    from training_gym.datasets.candidate import CandidateState
    from training_gym.datasets.split import (
        SplitError,
        SplitPolicy,
        leakage_group_key,
        plan_splits,
    )

    candidates = []
    for entry in BC.corpus_v5()[:3]:
        candidate = BC.make_candidate(entry)
        for state in (CandidateState.VALIDATED, CandidateState.PRIVACY_CHECKED,
                      CandidateState.PROVENANCE_CHECKED,
                      CandidateState.LEAKAGE_CHECKED,
                      CandidateState.READY_FOR_PROMOTION):
            candidate = candidate.with_state(state)
        candidates.append(candidate)
    forced = {leakage_group_key(candidates[0]): destination}
    with pytest.raises(SplitError, match="may never place one into training"):
        plan_splits(candidates,
                    policy=SplitPolicy(seed=f"{BC.DATASET_ID}-v5"), forced=forced)


# ══════════════════════════════════════════════════════════════════════════════
#  31-35. Deterministic identity
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v5_manifest_reproduces_across_roots_and_build_orders(tmp_path):
    """Identity must not depend on which versions happened to exist first — D34.

    ``promotion_plan_hash`` is deliberately NOT compared: it binds ``output_root_id`` by
    design, is re-derived on the executing host and is not part of the dataset identity.
    """
    direct = BC.build(tmp_path / "direct", dataset_version="v5")
    staged_root = tmp_path / "staged"
    for version in PRIOR_VERSIONS:
        BC.build(staged_root, dataset_version=version)
    staged = BC.build(staged_root, dataset_version="v5")
    for key in ("manifest_hash", "parent_manifest_hash", "leakage_report_hash",
                "split_policy_hash", "promoted_records"):
        assert direct[key] == staged[key], key
    assert direct["manifest_hash"] == EVAL_V5_MANIFEST
    assert direct["parent_manifest_hash"] == EVAL_V4_MANIFEST
    assert direct["promoted_records"] == 36
    assert direct["leakage_verdict"] == LeakageVerdict.CLEAN.value
    assert direct["leakage_findings"] == 0


def test_the_v5_manifest_is_the_frozen_digest(eval_root):
    from training_gym.datasets.manifests import verify_version

    result = verify_version(root=eval_root, dataset_id=BC.DATASET_ID,
                            dataset_version="v5")
    assert result.ok, list(result.problems)
    assert result.manifest is not None
    assert result.manifest.manifest_hash() == EVAL_V5_MANIFEST


def test_the_v5_task_pack_is_the_frozen_digest_and_carries_no_blockers(eval_root,
                                                                      tmp_path):
    built = _pack(eval_root)
    assert len(built.pack.tasks) == 36
    assert built.pack.pack_hash() == EVAL_V5_PACK
    assert list(built.pack.eligibility_blockers()) == []
    # And in an independent root, so the pack hash is not a property of this directory.
    other = tmp_path / "independent"
    BC.build(other, dataset_version="v5")
    assert _pack(other).pack.pack_hash() == EVAL_V5_PACK


def test_the_v5_task_order_is_deterministic_and_not_a_filesystem_artefact(eval_root,
                                                                         tmp_path):
    other = tmp_path / "order"
    BC.build(other, dataset_version="v5")
    assert [t.task_id for t in _pack(eval_root).pack.tasks] \
        == [t.task_id for t in _pack(other).pack.tasks]


def test_no_timestamp_enters_the_v5_identity():
    """``NOW`` is a frozen literal, so a rebuild tomorrow is a rebuild, not a new corpus."""
    assert BC.NOW == "2026-08-06T00:00:00Z"


# ══════════════════════════════════════════════════════════════════════════════
#  36-39. Historical immutability
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("version,expected", (
    ("v1", EVAL_V1_MANIFEST), ("v2", EVAL_V2_MANIFEST), ("v3", EVAL_V3_MANIFEST),
    ("v4", EVAL_V4_MANIFEST)))
def test_the_prior_holdouts_are_unchanged_by_the_existence_of_v5(version, expected,
                                                                 eval_root):
    from training_gym.datasets.manifests import load_manifest

    assert load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                         dataset_version=version).manifest_hash() == expected


def test_the_spent_v4_pack_identity_is_unchanged_by_the_existence_of_v5(eval_root):
    """v4 is USED_IMMUTABLE. Adding a successor may not perturb what it was."""
    assert _pack(eval_root, version="v4").pack.pack_hash() == EVAL_V4_PACK


@pytest.mark.parametrize("version,expected", (
    ("v1", TRAIN_V1_MANIFEST), ("v2", TRAIN_V2_MANIFEST)))
def test_the_training_corpora_are_unchanged_by_the_existence_of_v5(version, expected,
                                                                   tmp_path):
    root = tmp_path / "train-control"
    QC.build(root, dataset_version="v1")
    if version == "v1":
        from training_gym.datasets.manifests import load_manifest
        assert load_manifest(root=root, dataset_id=QC.DATASET_ID,
                             dataset_version="v1").manifest_hash() == expected
        return
    assert QC.build(root, dataset_version="v2")["manifest_hash"] == expected


def test_the_prior_holdout_material_is_byte_identical():
    """v5 is additive. A generator that edits v1-v4 while adding v5 would restate history."""
    assert len(BC.corpus()) == len(BC.corpus_v2()) == len(BC.corpus_v3()) \
        == len(BC.corpus_v4()) == 36
    # v2 is still exactly v1 plus the contract sentence on the contract families.
    for (s1, f1, t1, p1, g1), (s2, f2, t2, p2, g2) in zip(BC.corpus(), BC.corpus_v2(),
                                                          strict=True):
        assert (s1, f1, t1, g1) == (s2, f2, t2, g2)
        assert p2 == (p1 + BC.STRUCTURED_OUTPUT_CONTRACT
                      if f1 in BC.CONTRACT_FAMILIES else p1)


# ══════════════════════════════════════════════════════════════════════════════
#  40-44. The instrument is frozen: D37, D38, the gates and the budget
# ══════════════════════════════════════════════════════════════════════════════
def test_the_acceptance_gates_did_not_move_for_the_fifth_holdout():
    """Freezing a new exam is not an occasion to reprice the pass mark."""
    assert GatePolicy().policy_hash() == GATE_POLICY_HASH


def test_the_metric_policy_is_the_one_s3m2_froze():
    assert MetricPolicy().policy_hash() == METRIC_POLICY_HASH


def test_the_output_budget_did_not_move_after_s3r_measured_the_ceiling():
    """S3R counted 6 of 36 candidate responses at the ceiling. That is NOT a licence.

    Raising ``max_new_tokens`` in a corpus-freeze milestone would move a generation
    variable underneath a holdout and make v5 incomparable with every earlier
    measurement. It is recorded as ruled out and pinned here, and D37's reasoning policy
    is pinned beside it.
    """
    from training_gym.evaluation.generation import (
        ELIGIBILITY_REASONING_POLICY,
        ReasoningPolicy,
    )

    assert ELIGIBILITY_REASONING_POLICY is ReasoningPolicy.DISABLED
    assert eval_config_generation_policy_hash() == GENERATION_POLICY_HASH


def eval_config_generation_policy_hash() -> str:
    """The sealed evaluation configuration's generation policy, re-derived from its bytes.

    ``eligibility_generation_policy()`` alone carries library defaults (``timeout_s`` 120,
    ``seed`` 0, ``auto_safe`` device/precision); the digest of record ``c6b0b682…`` is the
    policy the S3I and S3L configs actually declared. Re-deriving from the sealed document
    is what makes this a check on the instrument rather than on a default. S3S reads only
    the CONFIGURATION — no result, no response, no score.
    """
    from pathlib import Path

    from training_gym.evaluation.generation import GenerationPolicy

    root = Path(__file__).resolve().parents[1]
    document = root / "evaluation" / "configs" / "m62-s3l-quality-heldout-live.json"
    if not document.is_file():  # pragma: no cover - the sealed config is runtime state
        pytest.skip("the sealed S3L evaluation config is not present in this checkout")
    policy = GenerationPolicy.from_dict(
        json.loads(document.read_text(encoding="utf-8"))["generation"])
    assert policy.max_new_tokens == 512
    assert policy.reasoning_policy.value == "disabled"
    return policy.policy_hash()


def test_d38_remains_diagnostic_and_no_gate_reads_it():
    """S3M.2 designed no gate, and freezing a fresh holdout does not create one."""
    gates = (_repo_root() / "jarvis" / "training_gym" / "evaluation"
             / "gates.py").read_text(encoding="utf-8")
    for name in ("output_budget_exhausted", "output_budget_exhaustion_rate",
                 "output_budget_exhaustion_count", "finish_reason"):
        assert name not in gates, name


def test_s3s_changed_no_evaluation_policy_grader_or_gate_source():
    """The only production files S3S may touch are the two corpus generators.

    Measured against the S3S starting commit and the WORKING TREE, which is what makes
    it a scope test for THIS milestone while it is being written. It is pinned to a
    closing commit the moment S3S closes, exactly as S3N's equivalent was.
    """
    import subprocess

    root = _repo_root()
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", S3S_STARTING_COMMIT],
            cwd=root, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available to compare against the S3S starting commit")
    if changed.returncode != 0:  # pragma: no cover - shallow clone or missing commit
        pytest.skip("the S3S starting commit is not reachable in this checkout")
    forbidden = [p for p in changed.stdout.split()
                 if p.startswith("jarvis/training_gym/")]
    assert forbidden == [], forbidden


#: The commit S3S started from — the S3R closure.
S3S_STARTING_COMMIT = "f9d25fd2a9f6ebe5b0ee7cdb487c21e368afc9b3"


# ══════════════════════════════════════════════════════════════════════════════
#  45-49. No candidate 004, no train-v3, no authority, no leaked body
# ══════════════════════════════════════════════════════════════════════════════
def test_no_candidate_004_identity_exists_anywhere_in_the_repository():
    """S3S froze the exam. It may not name, design, configure or train the student."""
    import subprocess

    root = _repo_root()
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                                 text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available to enumerate tracked files")
    if tracked.returncode != 0:  # pragma: no cover
        pytest.skip("this checkout is not a git repository")
    names = [p for p in tracked.stdout.split() if not p.endswith(".md")]
    assert [p for p in names if "quality-live-004" in p] == []
    assert [p for p in names if "candidate_004" in p or "candidate-004" in p] == []


def test_the_training_corpus_still_has_no_third_version():
    """A fresh holdout is not an occasion to design the curriculum it will judge."""
    assert sorted(QC.CURRICULUM_VERSIONS) == ["v1", "v2"]
    assert QC.LATEST_DATASET_VERSION == "v2"


def test_v5_is_not_reachable_from_any_training_export():
    """The pack builder refuses TRAIN unconditionally for evaluation-only material."""
    from training_gym.datasets.export import EXPORTABLE_SPLITS

    for source_split in dict(EXPORTABLE_SPLITS):
        assert source_split in {DatasetSplit.TRAIN.value, DatasetSplit.VALIDATION.value,
                                DatasetSplit.TRAIN, DatasetSplit.VALIDATION}


def _shingles(text: str, width: int = 8) -> set[str]:
    words = re.findall(r"[A-Za-z0-9']+", text.casefold())
    return {" ".join(words[i:i + width]) for i in range(len(words) - width + 1)}


def test_the_body_free_surfaces_carry_no_v5_material():
    """The firewall a candidate-004 session depends on, measured rather than promised.

    A future session reads PROGRESS, the pointer and the S3S freeze document to learn
    what v5 IS — its identities, counts and provenance — and must not be able to learn
    what v5 SAYS. Whole prompts, whole targets and any eight-word shingle of either are
    all refused. The generator itself is excluded: it is the corpus.
    """
    root = _repo_root()
    rows = BC.corpus_v5()
    body_shingles = set()
    for _s, _f, _t, prompt, target in rows:
        body_shingles |= _shingles(prompt) | _shingles(target)
    assert body_shingles, "expected the corpus to yield shingles to test against"

    for rel in BODY_FREE_SURFACES:
        path = root / rel
        if not path.is_file():
            pytest.skip(f"{rel} does not exist yet in this tree")
        text = path.read_text(encoding="utf-8")
        for _s, _f, task_id, prompt, target in rows:
            assert prompt not in text, f"{rel} carries the {task_id} prompt"
            assert target not in text, f"{rel} carries the {task_id} target"
        assert _shingles(text) & body_shingles == set(), rel


def test_this_test_file_contains_no_held_out_task_body():
    """A test that quotes the holdout publishes it to every future reader of the suite."""
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    for version in ("v4", "v5"):
        for _split, _family, _tid, prompt, target in BC.corpus_for(version):
            assert prompt not in source
            assert target not in source
