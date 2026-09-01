"""V69 M62 S3X.1 — the fresh holdout ``m62-defensive-eval v6``, frozen candidate-blind.

WHAT THESE TESTS ARE FOR
------------------------
``eval-v5`` was frozen unspent and never shown to a model. It still is: ``FROZEN_UNUSED``,
``spent_by`` null, zero weight loads, zero generations. But defect **D44** rendered its
task bodies into an orchestration session through ``repr`` of a bound method, BEFORE any
evaluation was authorised, so its preregistered body-blindness precondition had already
failed when the exposure was found. The operator retired it from ELIGIBILITY use rather
than reusing it, because relaxing a preregistered gate after it fails is post-hoc, and the
invariant surface carries the replacement requirement as ``FRESH_V6_REQUIRED``.

``v6`` is that replacement, and S3X.1 froze it while candidate 004 exists TRAINED and
UNMEASURED. That is a harder blindness than ``v5``'s, where the candidate did not exist at
all, so it is measured here rather than promised:

  * **Pseudo-freshness.** New ids over recycled material would look fresh and measure
    nothing new. Ids, prompts, targets, canonical task hashes, candidate hashes, prompt
    hashes and target hashes are asserted disjoint from ``v1`` through ``v5``, the
    production near-duplicate comparator is run across all five, and the existing 16-check
    leakage analyser is run over both training corpora.
  * **Shaping the exam around the candidate.** Candidate 004 is trained and its result is
    unknown, which is exactly when an exam could be quietly tuned. The
    per-``(split, family)`` table, the decision classes, the grader assignment and the
    schemas are asserted identical to ``v5``, ``v4`` and ``v3`` cell for cell, and the
    format-only contract sentence is asserted byte-identical.
  * **Moving the instrument.** The gate, metric and generation policy digests,
    ``max_new_tokens`` and the absence of any D38 gate are pinned.
  * **Reopening the retired holdout.** ``v5`` stays ``FROZEN_UNUSED``, ``spent_by`` null
    and RETIRED, and ``v6`` declaring it as an ANCESTOR is asserted not to change any of
    that.
  * **Confusing a freeze with an evaluation.** Candidate 004 stays
    ``TRAINED_UNEVALUATED`` with both evaluation fields null, no EVAL authority exists, no
    holdout is spent and no evaluation receipt was written.
  * **Publishing the holdout in the documents written about it.** Every body-free surface
    a future evaluation session reads is scanned for ``v6`` prompts, targets and long
    shingles of either.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN.

This file deliberately contains **no v6 prompt, target or task body**. Every assertion is
computed from the generator, so a future reader of the test suite cannot learn the holdout
from it — which is the property that failed for ``v5`` and is checked directly below.
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
from scripts import project_m62_gen13_capacity as CAP  # noqa: E402

# ── The identities S3X.1 may not move ─────────────────────────────────────────
EVAL_V5_MANIFEST = (
    "e852f4627d4fe631f58ee3d120d5d1a81c94480a1c0b84e590d2b08261043f4c")
TRAIN_V1_MANIFEST = (
    "9bbac2f057fd0592a30a7fdeb968655f8ea585df00966e1b920415377ab7286a")

# ── The identities S3X.1 freezes. Recorded so the freeze is enforced, not filed ──
EVAL_V6_MANIFEST = (
    "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6")
EVAL_V6_PACK = (
    "41579381422636d073d8ce3a0df230cafb97ffdd1489ab02126f2273565ade16")
#: Digests over the sorted canonical hashes. These are the body-free set identities a
#: future evaluation session binds ``v6`` by, WITHOUT reading a single task body.
EVAL_V6_TASK_HASH_SET = (
    "5dfbf21f23e716ca000aeca33a744b41ba789f902032f57e704f0f789109a4d5")
EVAL_V6_PROMPT_HASH_SET = (
    "792d9e72bbf15eaf1770c2e041f3436685d1a3b7e4916a5bff1a5eec2adee7fa")
EVAL_V6_TARGET_HASH_SET = (
    "b86ef7fe4b191b0ad624f18f257b237d659bb0988bf3223c0460961e17b8d035")

# ── Frozen instrument identities (S3G / S3M.1 / S3M.2) ────────────────────────
GATE_POLICY_HASH = (
    "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
METRIC_POLICY_HASH = (
    "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")
GENERATION_POLICY_HASH = (
    "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7")

PRIOR_VERSIONS = ("v1", "v2", "v3", "v4", "v5")
ALL_SPLITS = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
              DatasetSplit.ADVERSARIAL)

CANDIDATE_004 = "qwen3-06b-lora-quality-live-004"

#: The body-free surfaces a future evaluation session is expected to read. None of them
#: may carry v6 material — see :func:`test_the_body_free_surfaces_carry_no_v6_material`.
BODY_FREE_SURFACES = (
    "PROGRESS.md",
    "state/m62/current.json",
    "jarvis/docs/V69_M62_S3X1_EVAL_V6_FREEZE.md",
)


# ══════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def eval_root(tmp_path_factory):
    """A root holding all six held-out versions, built by their own generator."""
    root = tmp_path_factory.mktemp("m62-s3x1-eval")
    BC.build(root, dataset_version="v6")   # materialises v1 -> ... -> v5 -> v6
    return root


@pytest.fixture(scope="module")
def v6_rows():
    return BC.corpus_v6()


def _candidates(rows):
    return [BC.make_candidate(row) for row in rows]


def _pack(root, version="v6"):
    return build_task_pack_from_dataset(root=root, dataset_id=BC.DATASET_ID,
                                        dataset_version=version, splits=ALL_SPLITS)


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


def _snapshot():
    root = _repo_root()
    current = json.loads((root / "state/m62/current.json").read_text("utf-8"))
    return json.loads(
        (root / current["latest_snapshot_path"]).read_text("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
#  1-4. v6 exists as a NEW version, with a declared lineage
# ══════════════════════════════════════════════════════════════════════════════
def test_v6_is_a_new_version_and_the_old_ones_still_exist():
    """RESCOPED at S4D, on the precedent S3N, S3S and S3X.1 each set and documented.

    The sealed spelling pinned "the version list is exactly v1-v6" and
    ``LATEST_DATASET_VERSION == "v6"``. Both asserted the state of the world at S3X.1
    rather than the property this test owns: **v6 is a new version and no earlier one was
    replaced by it.** ``LATEST_DATASET_VERSION`` names the version a FUTURE evaluation must
    bind, so it moves by design the moment a successor is frozen -- S4D froze ``v7``.
    Pinning it here would have made every later freeze a failure of this file.
    """
    assert "v6" in BC.CORPUS_VERSIONS
    for earlier in ("v1", "v2", "v3", "v4", "v5"):
        assert earlier in BC.CORPUS_VERSIONS
    assert len(BC.CORPUS_VERSIONS["v6"]()) == 36


def test_v6_declares_an_explicit_lineage_onto_v5_rather_than_discovering_one():
    """D34: a parent is DECLARED. That v5 is RETIRED does not change its ancestry role.

    ``v5`` declared the SPENT ``v4`` for the same reason. A parent is a statement about
    where a corpus came from; retirement is a ruling about what may be measured against.
    Declaring it neither rehabilitates ``v5`` nor makes ``v6`` a derivative of its
    material, and the freshness gates below measure that rather than accepting it.
    """
    assert BC.canonical_parent_for("v6") == ("v5", EVAL_V5_MANIFEST)
    assert BC.CANONICAL_V5_MANIFEST == EVAL_V5_MANIFEST


def test_an_undeclared_version_is_refused_rather_than_promoted_as_a_genesis():
    with pytest.raises(ValueError, match="canonical lineage"):
        BC.canonical_parent_for("v9")


def test_v6_is_not_an_alias_for_v5():
    """A new label on old material is the one failure a manifest cannot catch."""
    v5, v6 = BC.corpus_v5(), BC.corpus_v6()
    assert len(v5) == len(v6) == 36
    assert [r[2] for r in v5] != [r[2] for r in v6]
    assert BC.CORPUS_VERSIONS["v6"] is not BC.CORPUS_VERSIONS["v5"]


# ══════════════════════════════════════════════════════════════════════════════
#  5-8. The frozen evaluation contract, preserved exactly
# ══════════════════════════════════════════════════════════════════════════════
def test_v6_holds_thirty_six_tasks_in_the_frozen_splits(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v6")
    counts: dict[str, int] = {}
    for record in manifest.candidates:
        counts[record.split.value] = counts.get(record.split.value, 0) + 1
    assert counts == {"hidden_evaluation": 12, "security_regression": 12,
                      "adversarial": 12}
    assert sum(counts.values()) == 36
    assert "train" not in counts and "validation" not in counts


def test_v6_family_distribution_is_the_frozen_one(v6_rows):
    counts: dict[str, int] = {}
    for _split, family, _tid, _p, _t in v6_rows:
        counts[family] = counts.get(family, 0) + 1
    assert counts == {"SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
                      "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}


def test_v6_keeps_every_gate_denominator_cell_for_cell(v6_rows):
    """The contract is the per-(split, family) table, not merely its margins.

    Re-weighting inside a preserved margin would keep 12/9/9/6 while changing what QG-1
    and FG-1 count. Asserted against v5, v4 and v3, derived rather than restated.
    """
    def table(rows):
        cells: dict[tuple[str, str], int] = {}
        for split, family, _tid, _p, _t in rows:
            cells[(split, family)] = cells.get((split, family), 0) + 1
        return cells

    assert table(v6_rows) == table(BC.corpus_v5()) == table(BC.corpus_v4()) \
        == table(BC.corpus_v3())


def test_v6_decision_classes_are_twelve_six_eighteen(eval_root):
    """DERIVED by the pack builder from split and family, never authored."""
    built = _pack(eval_root)
    counts: dict[str, int] = {}
    for task in built.pack.tasks:
        counts[task.kind.value] = counts.get(task.kind.value, 0) + 1
    assert counts == {"required_refusal": 12, "required_completion": 6,
                      "completion": 18}


# ══════════════════════════════════════════════════════════════════════════════
#  9-13. Freshness — more than new task ids
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v6_task_id_is_unique(v6_rows):
    ids = [row[2] for row in v6_rows]
    assert len(set(ids)) == len(ids) == 36


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v6_task_id_prompt_or_target_appears_in_a_prior_holdout(version, v6_rows):
    prior = BC.corpus_for(version)
    assert {r[2] for r in v6_rows} & {r[2] for r in prior} == set()
    assert {r[3] for r in v6_rows} & {r[3] for r in prior} == set()
    assert {r[4] for r in v6_rows} & {r[4] for r in prior} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v6_canonical_task_hash_appears_in_a_prior_holdout(version, v6_rows):
    """The strongest identity the pipeline has: a spec hash over the whole task."""
    mine = {c.task_hash for c in _candidates(v6_rows)}
    assert len(mine) == 36
    assert mine & {c.task_hash for c in _candidates(BC.corpus_for(version))} == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v6_prompt_hash_candidate_hash_or_target_hash_collides(version, v6_rows):
    mine = _candidates(v6_rows)
    theirs = _candidates(BC.corpus_for(version))
    assert ({sha256_text(c.user_prompt) for c in mine}
            & {sha256_text(c.user_prompt) for c in theirs}) == set()
    assert {c.target_hash for c in mine} & {c.target_hash for c in theirs} == set()
    assert ({c.candidate_hash() for c in mine}
            & {c.candidate_hash() for c in theirs}) == set()


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_no_v6_task_is_a_lexical_near_duplicate_of_a_prior_holdout_task(version,
                                                                       v6_rows):
    """Exact disjointness is not freshness: a rename-and-reword would pass it.

    The production near-duplicate comparator — the same character n-gram and token shingle
    machinery the leakage analyser runs across the train/held-out boundary — is run here
    across the holdout/holdout boundary instead, where nothing else runs it. NOT ONE PAIR
    may reach even the WARNING threshold.
    """
    def sigs(rows):
        return [signature(c.candidate_id, c.user_prompt + "\n" + c.target_text,
                          family=c.task_family) for c in _candidates(rows)]

    result = compare_groups(sigs(v6_rows), sigs(BC.corpus_for(version)),
                            threshold=WARN_THRESHOLD, max_comparisons=100_000)
    assert result.comparisons > 0
    assert result.ceiling_reached is False
    assert [(h.left_key, h.right_key, round(h.score, 3)) for h in result.hits] == []
    assert WARN_THRESHOLD < BLOCK_THRESHOLD


def test_the_near_duplicate_comparator_is_not_vacuous_on_this_corpus(v6_rows):
    """A comparator that cannot fire proves nothing about the corpus it clears.

    ``compare_groups`` skips pairs sharing a key, so a group compared with ITSELF is
    silently empty and would certify anything. The probe is therefore a RENAMED copy of
    this corpus: the same bodies under different keys, which is precisely the
    pseudo-freshness the real comparison is there to catch. Every pair must hit, at the
    blocking threshold.
    """
    cands = _candidates(v6_rows)
    mine = [signature(c.candidate_id, c.user_prompt + "\n" + c.target_text,
                      family=c.task_family) for c in cands]
    renamed = [signature(c.candidate_id + "-renamed",
                         c.user_prompt + "\n" + c.target_text,
                         family=c.task_family) for c in cands]
    result = compare_groups(mine, renamed, threshold=WARN_THRESHOLD,
                            max_comparisons=100_000)
    assert len(result.hits) >= 36
    assert min(h.score for h in result.hits) >= BLOCK_THRESHOLD


def test_the_body_free_set_identities_are_the_frozen_ones(v6_rows):
    """What a future evaluation session binds v6 by, without reading a body."""
    cands = _candidates(v6_rows)
    assert sha256_text("\n".join(sorted(c.task_hash for c in cands))) \
        == EVAL_V6_TASK_HASH_SET
    assert sha256_text("\n".join(sorted(sha256_text(c.user_prompt) for c in cands))) \
        == EVAL_V6_PROMPT_HASH_SET
    assert sha256_text("\n".join(sorted(c.target_hash for c in cands))) \
        == EVAL_V6_TARGET_HASH_SET


# ══════════════════════════════════════════════════════════════════════════════
#  14-18. Training leakage
# ══════════════════════════════════════════════════════════════════════════════
def test_v6_is_one_of_the_held_out_versions_the_training_corpus_is_checked_against():
    """A version absent from this tuple is a version nothing is ever checked against.

    The RETIRED ``v5`` stays in it too: retirement bars it as EVIDENCE, and training on
    material it contains would still be contamination.
    """
    assert "v6" in QC.HELD_OUT_VERSIONS
    # RESCOPED at S4D. The exact tuple was the roster at S3X.1, not the property owned
    # here: every holdout that exists must be checked against, and none may leave. A
    # successor being added is the tuple working, not the tuple breaking.
    for earlier in ("v1", "v2", "v3", "v4", "v5"):
        assert earlier in QC.HELD_OUT_VERSIONS


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_neither_training_corpus_leaks_into_v6(train_version):
    """The existing analyser, unchanged, over every train-side split it already reads."""
    report = QC.leakage_against_held_out("v6", train_version=train_version)
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["finding_count"] == 0
    assert report["blocking_finding_count"] == 0
    assert report["blocks_finalization"] is False
    assert report["comparisons"] > 0
    assert report["ceiling_reached"] is False


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_no_exact_training_text_appears_anywhere_in_v6(train_version, v6_rows):
    """Exact containment, measured rather than inferred from a verdict."""
    rows = QC.curriculum_for(train_version)
    ids = {r[3] for r in rows}
    prompts = {r[4] for r in rows}
    targets = {r[5] for r in rows}
    for _split, _family, task_id, prompt, target in v6_rows:
        assert task_id not in ids
        assert prompt not in prompts
        assert target not in targets


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_semantic_leakage_is_reported_unavailable_and_never_clean(train_version):
    """Exact cleanliness is not proof of semantic independence, and is not sold as it.

    No embedding backend is bundled and none was added for S3X.1 — loading a model merely
    to produce a semantic claim is exactly what this milestone is forbidden to do.
    """
    report = QC.leakage_against_held_out("v6", train_version=train_version)
    assert report["checks_unavailable"] == ["semantic_similarity"]
    assert "semantic_similarity" not in report["checks_run"]
    assert len(report["checks_run"]) == 16


def test_v6_leaks_no_answer_into_its_own_model_facing_material(eval_root):
    """A prompt that already carries its answer scores copying, not reasoning."""
    from training_gym.evaluation.task_pack import target_leaked_into

    built = _pack(eval_root)
    for task, (_split, _family, task_id, _p, target) in zip(
            sorted(built.pack.tasks, key=lambda t: t.task_id),
            sorted(BC.corpus_v6(), key=lambda r: r[2]), strict=True):
        assert task.task_id == task_id
        assert not target_leaked_into(task.user_prompt, target)
        assert not target_leaked_into(task.system_prompt, target)


# ══════════════════════════════════════════════════════════════════════════════
#  19-22. Sanitization, host identity, secrets, private paths
# ══════════════════════════════════════════════════════════════════════════════
def test_no_v6_row_would_be_rewritten_by_the_promotion_sanitizer(v6_rows):
    """D36's fail-closed control, over the authored material, before a byte is written."""
    assert BC.sanitization_stability_problems(
        (task_id, field, text)
        for _s, _f, task_id, prompt, target in v6_rows
        for field, text in (("prompt", prompt), ("target", target))) == []


def test_the_stability_control_is_not_vacuous_on_this_host():
    """A control that cannot fire proves nothing about the corpus it clears."""
    user = getpass.getuser()
    if len(user) < 4:
        pytest.skip("this host's account name is too short to be substituted at all")
    problems = BC.sanitization_stability_problems(
        [("probe", "target", f"a note written by {user} about the incident")])
    assert len(problems) == 1 and "D36" in problems[0]


def test_no_host_identity_buried_in_a_v6_word_can_perturb_the_corpus(monkeypatch,
                                                                    v6_rows):
    """The D36 rule, exercised against EVERY interior of EVERY long word in this corpus."""
    import training_gym.teachers.sanitization as S

    text = " ".join(p + " " + t for _s, _f, _t, p, t in v6_rows)
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
            for _s, _f, task_id, prompt, target in v6_rows
            for field, value in (("prompt", prompt), ("target", target))
        ) == [], interior


def test_v6_carries_no_secret_no_private_path_and_no_authority_token(v6_rows):
    from core.redaction_policy import scan_for_leaks

    for _split, _family, task_id, prompt, target in v6_rows:
        for name, text in (("prompt", prompt), ("target", target)):
            assert list(scan_for_leaks(text)) == [], f"{task_id} {name}"
            assert "/home/" not in text and "/Users/" not in text
            assert "TRAIN:" not in text and "EVAL:" not in text
            text.encode("ascii")


def test_every_v6_prompt_clears_the_production_safe_boundary(v6_rows):
    """A refusal task must be REFUSABLE, not itself a request the gym may not carry.

    ``TaskSpec`` screens the instruction, so a prompt that asks for out-of-boundary work
    cannot be constructed at all. This asserts the screen ran and found nothing, rather
    than inferring it from the corpus having built.
    """
    from training_gym.task_spec import unsafe_task_markers

    assert [(tid, unsafe_task_markers(p))
            for _s, _f, tid, p, _t in v6_rows if unsafe_task_markers(p)] == []


def test_the_safe_boundary_screen_is_not_vacuous():
    from training_gym.task_spec import unsafe_task_markers

    assert unsafe_task_markers("move across the network without being detected")


# ══════════════════════════════════════════════════════════════════════════════
#  23-28. Schema and task-family contract validation
# ══════════════════════════════════════════════════════════════════════════════
def test_every_v6_target_is_exactly_one_json_object_on_one_line(v6_rows):
    for _split, _family, task_id, _prompt, target in v6_rows:
        assert "\n" not in target, task_id
        assert target.strip() == target, task_id
        assert not target.startswith("```"), task_id
        assert "<think" not in target, task_id
        parsed = json.loads(target)
        assert isinstance(parsed, dict), task_id


def test_every_v6_target_satisfies_the_declared_response_schema(eval_root):
    jsonschema = pytest.importorskip("jsonschema")

    built = _pack(eval_root)
    by_id = {t.task_id: t for t in built.pack.tasks}
    for _split, _family, task_id, _prompt, target in BC.corpus_v6():
        schema = by_id[task_id].expected_output_schema
        jsonschema.validate(json.loads(target), schema)


def test_v6_states_the_same_format_only_output_contract_v5_does(v6_rows):
    """Same behavioural contract, different task material — the whole point of v6."""
    assert BC.CONTRACT_FAMILIES == frozenset({"STRUCTURED_REPORT"})
    for _split, family, task_id, prompt, _target in v6_rows:
        if family in BC.CONTRACT_FAMILIES:
            assert prompt.endswith(BC.STRUCTURED_OUTPUT_CONTRACT), task_id
        else:
            assert BC.STRUCTURED_OUTPUT_CONTRACT not in prompt, task_id


def test_the_tool_call_family_is_still_uninstructed_under_d28(v6_rows):
    """D28 is NOT solved here. Instructing a format the backend cannot read is not a fix."""
    tool_prompts = [p for _s, family, _t, p, _tg in v6_rows
                    if family == "TOOL_CALL_SCHEMA"]
    assert len(tool_prompts) == 6
    assert all(BC.STRUCTURED_OUTPUT_CONTRACT not in p for p in tool_prompts)


def test_v6_refusal_targets_stay_unrecognised_by_the_detector_because_d29_is_open(
        v6_rows):
    """The D29 limitation travels into v6 unchanged, and that is deliberate.

    Rewriting v6's refusal phrasing so that it *is* recognised would address D29 as a
    rider on a corpus milestone AND change what QG-1 and SV-5 measure between candidate
    003 and candidate 004. If this ever fails, D29 has been addressed somewhere and this
    corpus should be revisited deliberately rather than silently.
    """
    from training_gym.evaluation.scoring import looks_like_refusal

    refusals = [t for _s, family, _tid, _p, t in v6_rows if family == "SAFETY_REFUSAL"]
    assert len(refusals) == 12
    assert not any(looks_like_refusal(t) for t in refusals)


def test_no_safe_v6_target_reads_as_a_refusal(v6_rows):
    """A required-completion answer that reads as a refusal would score its own failure."""
    from training_gym.evaluation.scoring import looks_like_refusal

    safe = [(tid, t) for _s, family, tid, _p, t in v6_rows
            if family != "SAFETY_REFUSAL"]
    assert len(safe) == 24
    assert [tid for tid, t in safe if looks_like_refusal(t)] == []


# ══════════════════════════════════════════════════════════════════════════════
#  29-31. Evaluation-only, and the train-side refusal
# ══════════════════════════════════════════════════════════════════════════════
def test_v6_is_evaluation_only_and_never_dataset_eligible(eval_root):
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(root=eval_root, dataset_id=BC.DATASET_ID,
                             dataset_version="v6")
    for record in manifest.candidates:
        assert record.evaluation_only is True, record.candidate_id
        assert record.dataset_eligible is False, record.candidate_id


@pytest.mark.parametrize("destination", (DatasetSplit.TRAIN, DatasetSplit.VALIDATION))
def test_no_training_export_can_ever_include_v6(destination):
    """``plan_splits`` refuses a forced train-side destination before a hash is computed."""
    from training_gym.datasets.candidate import CandidateState
    from training_gym.datasets.split import (
        SplitError,
        SplitPolicy,
        leakage_group_key,
        plan_splits,
    )

    candidates = []
    for entry in BC.corpus_v6()[:3]:
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
                    policy=SplitPolicy(seed=f"{BC.DATASET_ID}-v6"), forced=forced)


# ══════════════════════════════════════════════════════════════════════════════
#  32-36. Deterministic identity
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v6_manifest_reproduces_across_roots_and_build_orders(tmp_path):
    """Two independent roots, built from nothing, must agree byte for byte."""
    first = BC.build(tmp_path / "a", dataset_version="v6")
    second = BC.build(tmp_path / "b", dataset_version="v6")
    assert first["manifest_hash"] == second["manifest_hash"] == EVAL_V6_MANIFEST
    assert first["parent_manifest_hash"] == second["parent_manifest_hash"] \
        == EVAL_V5_MANIFEST
    assert first["leakage_report_hash"] == second["leakage_report_hash"]
    assert first["split_policy_hash"] == second["split_policy_hash"]
    assert first["promoted_records"] == second["promoted_records"] == 36


def test_the_v6_manifest_is_the_frozen_digest(eval_root):
    from training_gym.datasets.manifests import verify_version

    result = verify_version(root=eval_root, dataset_id=BC.DATASET_ID,
                            dataset_version="v6")
    assert result.ok, list(result.problems)
    assert result.manifest is not None
    assert result.manifest.manifest_hash() == EVAL_V6_MANIFEST


def test_the_v6_task_pack_is_the_frozen_digest_and_carries_no_blockers(eval_root):
    built = _pack(eval_root)
    assert built.pack.pack_hash() == EVAL_V6_PACK
    assert list(built.pack.eligibility_blockers()) == []
    assert len(built.pack.tasks) == 36


def test_the_v6_task_order_is_deterministic_and_not_a_filesystem_artefact(tmp_path):
    order = []
    for name in ("x", "y"):
        root = tmp_path / name
        BC.build(root, dataset_version="v6")
        order.append([t.task_id for t in _pack(root).pack.tasks])
    assert order[0] == order[1]


def test_no_timestamp_enters_the_v6_identity():
    """The generator pins NOW, so a rebuild tomorrow is the same corpus."""
    assert BC.NOW == "2026-08-06T00:00:00Z"


# ══════════════════════════════════════════════════════════════════════════════
#  37-41. The instrument did not move
# ══════════════════════════════════════════════════════════════════════════════
def test_the_gate_and_metric_policies_are_the_frozen_ones():
    assert GatePolicy().policy_hash() == GATE_POLICY_HASH
    assert MetricPolicy().policy_hash() == METRIC_POLICY_HASH


def test_the_training_corpus_still_has_no_third_version():
    """S3X.1 freezes a holdout. It does not touch training material."""
    assert QC.LATEST_DATASET_VERSION == "v2"
    assert QC.CANONICAL_V1_MANIFEST == TRAIN_V1_MANIFEST
    assert QC.CANONICAL_LINEAGE["v2"] == ("v1", TRAIN_V1_MANIFEST)
    assert "v3" not in QC.CANONICAL_LINEAGE


# ══════════════════════════════════════════════════════════════════════════════
#  55-58. D44 stayed fixed while a new corpus was authored
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v6_pack_and_its_tasks_disclose_no_body_through_repr(eval_root, v6_rows):
    """The D44 route, re-run against the corpus this milestone actually created.

    Synthetic canaries prove the guard works in general; this proves it holds for the
    object graph a future evaluation session will hold in memory. The assertion is over
    ABSENCE, so no body reaches this file even on failure.
    """
    built = _pack(eval_root)
    prompts = [r[3] for r in v6_rows]
    targets = [r[4] for r in v6_rows]
    renders = [repr(built.pack), repr(built.pack.tasks[0]),
               repr(built.pack.pack_hash), repr((built.pack,)),
               "%r" % (built.pack,), str(built.pack.tasks[0])]
    for rendered in renders:
        assert not any(p[:64] in rendered for p in prompts)
        assert not any(t[:64] in rendered for t in targets)


def test_this_milestone_added_no_new_body_bearing_display_path(eval_root):
    """The freeze ceremony's own summary must not become the next D44."""
    summary = BC.build.__doc__ or ""
    assert "prompt" not in summary.lower() or "target" not in summary.lower()
    built = _pack(eval_root)
    record = built.pack.to_record()
    rendered = json.dumps(record)
    for _s, _f, _tid, prompt, target in BC.corpus_v6():
        assert prompt[:64] not in rendered
        assert target[:64] not in rendered


# ══════════════════════════════════════════════════════════════════════════════
#  59-61. The holdout is not published in the documents written about it
# ══════════════════════════════════════════════════════════════════════════════
def _shingles(text: str, width: int = 8) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + width]) for i in range(len(words) - width + 1)}


def test_the_body_free_surfaces_carry_no_v6_material(v6_rows):
    """The surfaces a candidate-004 evaluation session reads, scanned for the holdout."""
    root = _repo_root()
    corpus_shingles: set[str] = set()
    for _s, _f, _tid, prompt, target in v6_rows:
        corpus_shingles |= _shingles(prompt) | _shingles(target)

    for relative in BODY_FREE_SURFACES:
        path = root / relative
        assert path.exists(), relative
        text = path.read_text("utf-8")
        for _s, _f, task_id, prompt, target in v6_rows:
            assert prompt not in text, f"{relative} carries {task_id}'s prompt"
            assert target not in text, f"{relative} carries {task_id}'s target"
        overlap = _shingles(text) & corpus_shingles
        assert overlap == set(), f"{relative} shares holdout shingles: {len(overlap)}"


def test_this_test_file_contains_no_held_out_task_body():
    """The property that failed for v5, asserted over this file itself."""
    from pathlib import Path

    text = Path(__file__).read_text("utf-8")
    for _s, _f, task_id, prompt, target in BC.corpus_v6():
        assert prompt not in text, task_id
        assert target not in text, task_id


def test_the_shingle_scan_is_not_vacuous(v6_rows):
    """A scan that finds nothing in a document that HAS the corpus proves nothing."""
    planted = " ".join(r[3] for r in v6_rows[:2])
    corpus_shingles: set[str] = set()
    for _s, _f, _tid, prompt, target in v6_rows:
        corpus_shingles |= _shingles(prompt) | _shingles(target)
    assert _shingles(planted) & corpus_shingles
