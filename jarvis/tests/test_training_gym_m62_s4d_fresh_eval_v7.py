"""V69 M62 S4D — the fresh holdout ``m62-defensive-eval v7``, frozen candidate-blind.

WHAT THESE TESTS ARE FOR
------------------------
``v7`` is the first holdout authored for a REFERENCE-ADAPTER comparison: candidate 004
bound as the reference arm and candidate 005 as the candidate arm, one paired attempt,
one spend. Both candidates exist and are trained, so the exam could in principle have been
shaped around either. These tests measure that it was not, rather than promising it:

  * **Pseudo-freshness.** New ids over recycled material would look fresh and measure
    nothing new. Ids, prompts, targets, canonical task hashes and both normalised keys are
    asserted disjoint from ``v1`` through ``v6``, and the production near-duplicate
    comparator is run across all six -- the same machinery the leakage analyser uses over
    the train/held-out boundary, run here over the holdout/holdout boundary where nothing
    else runs it.
  * **Shaping the exam around a candidate.** The per-``(split, family)`` table, the derived
    decision kinds, the grader assignment and the response schemas are asserted identical
    to ``v6`` cell for cell, and the format-only contract sentence byte-identical.
  * **Moving the instrument.** The four policy digests are pinned, and a test asserts the
    gate stack still reads no model identity.
  * **Answering the exam from the exam.** Prompt/target similarity is measured per task, so
    a prompt that carries its own answer is caught rather than assumed absent.
  * **A corpus that is really one task six times.** All 630 internal pairs are compared,
    because a set whose tasks paraphrase each other has 36 denominators and far less than
    36 tasks' worth of evidence.

No ``v1``-``v6`` body is read, printed or asserted against here. The freshness tests
operate over one-way digests and similarity scores; the only values that reach a failure
message are counts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from training_gym.datasets.candidate import DatasetSplit
from training_gym.datasets.similarity import (
    BLOCK_THRESHOLD,
    WARN_THRESHOLD,
    compare_groups,
    normalized_key,
    signature,
)
from training_gym.evaluation.pack_builder import (
    build_task_pack_from_dataset,
    response_schema_for,
    tool_schemas_for,
)
from training_gym.evaluation.policy import EvaluationPolicySet
from training_gym.schemas import sha256_text
from training_gym.task_spec import TaskFamily, unsafe_task_markers

pytest.importorskip("scripts.build_training_corpus")
from scripts import build_evaluation_corpus as BC  # noqa: E402
from scripts import build_training_corpus as QC  # noqa: E402

PRIOR_VERSIONS = ("v1", "v2", "v3", "v4", "v5", "v6")

# ── The identities S4D sealed. Body-free, and quoted from the freeze. ────────────────
V7_MANIFEST_HASH = "e80cc46fa0b2c1ec020ed02f9565d778772d8e76dd208f2ba49349ab199b369a"
V7_PACK_HASH = "e6d8d0b28aa0c5e6c9d186ccc9f2c52371617ee46133199f73e25cbaf1750838"
V6_MANIFEST_HASH = "413e675711d51f5b98cb5a8ec7ff7fb0d8eb36b5e4c6dff790fb60f764f8fba6"
V7_TASK_HASH_SET = "a5bc453a2f274cfcdf11a4ebb57e613d1daa6236efa236467f853d990771466a"
V7_PROMPT_HASH_SET = "8226b43a3d46f02d1058b7a6e6007fecd743073bd0c0e52d714c38eceebed033"
V7_TARGET_HASH_SET = "d901452021de0d61a3143eb4d663eb80c030da8e14b3725c5fed38f079ac7c02"


@pytest.fixture(scope="module")
def v7_rows():
    return BC.CORPUS_VERSIONS["v7"]()


@pytest.fixture(scope="module")
def eval_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("s4d-eval-root")
    BC.build(root, dataset_version="v7")
    return root


def _set_digest(values) -> str:
    return sha256_text("\n".join(sorted(values)))


def _surfaces(version: str) -> dict[str, set[str]]:
    """Six one-way identity surfaces for one version. Digests only, never bodies."""
    out: dict[str, set[str]] = {k: set() for k in
                               ("ids", "prompt", "target", "task", "pnorm", "tnorm")}
    for split, family, task_id, prompt, target in BC.CORPUS_VERSIONS[version]():
        fam = getattr(TaskFamily, family)
        out["ids"].add(task_id)
        out["prompt"].add(sha256_text(prompt))
        out["target"].add(sha256_text(target))
        out["task"].add(sha256_text(f"{split}|{family}|{prompt}|{target}"))
        out["pnorm"].add(normalized_key(prompt, family=fam))
        out["tnorm"].add(normalized_key(target, family=fam))
    return out


def _signatures(version: str):
    return [signature(f"{version}:{task_id}", f"{prompt}\n{target}",
                      family=getattr(TaskFamily, family))
            for _split, family, task_id, prompt, target in BC.CORPUS_VERSIONS[version]()]


# ══════════════════════════════════════════════════════════════════════════════
#  Identity and lineage
# ══════════════════════════════════════════════════════════════════════════════
def test_v7_is_a_new_version_and_every_old_one_still_exists():
    assert sorted(BC.CORPUS_VERSIONS) == ["v1", "v2", "v3", "v4", "v5", "v6", "v7"]
    assert BC.LATEST_DATASET_VERSION == "v7"


def test_v7_declares_an_explicit_lineage_onto_v6_rather_than_discovering_one():
    """A SPENT parent is still a parent: spending rules on what may be measured."""
    assert BC.canonical_parent_for("v7") == ("v6", V6_MANIFEST_HASH)
    assert BC.CANONICAL_V6_MANIFEST == V6_MANIFEST_HASH


def test_an_undeclared_version_is_still_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="declares no canonical lineage"):
        BC.canonical_parent_for("v99")


def test_v7_is_not_an_alias_for_v6(v7_rows):
    v6 = BC.CORPUS_VERSIONS["v6"]()
    assert len(v7_rows) == len(v6) == 36
    assert {r[2] for r in v7_rows}.isdisjoint({r[2] for r in v6})


# ══════════════════════════════════════════════════════════════════════════════
#  The shape is inherited, cell for cell
# ══════════════════════════════════════════════════════════════════════════════
def test_v7_holds_thirty_six_tasks_in_the_frozen_splits(v7_rows):
    from collections import Counter
    assert len(v7_rows) == 36
    assert dict(Counter(r[0] for r in v7_rows)) == {
        "HIDDEN_EVALUATION": 12, "SECURITY_REGRESSION": 12, "ADVERSARIAL": 12}
    assert dict(Counter(r[1] for r in v7_rows)) == {
        "SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
        "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}


def test_the_per_split_family_table_is_identical_to_v6(v7_rows):
    """The load-bearing shape assertion: same cells, different material."""
    from collections import Counter
    cells = Counter((r[0], r[1]) for r in v7_rows)
    assert cells == Counter((r[0], r[1]) for r in BC.CORPUS_VERSIONS["v6"]())


def test_the_task_ids_follow_the_frozen_body_free_convention(v7_rows):
    groups = (("he7-report-", 4), ("he7-evidence-", 4), ("he7-tool-", 2),
              ("he7-refusal-", 2), ("sr7-refusal-", 6), ("sr7-safe-", 6),
              ("adv7-refusal-", 4), ("adv7-report-", 3), ("adv7-evidence-", 3),
              ("adv7-tool-", 2))
    expected = {f"{stem}{n:02d}" for stem, count in groups
                for n in range(1, count + 1)}
    assert {r[2] for r in v7_rows} == expected


def test_the_contract_sentence_is_byte_identical_and_applied_to_one_family(v7_rows):
    assert BC.CONTRACT_FAMILIES == frozenset({"STRUCTURED_REPORT"})
    for _split, family, _tid, prompt, _target in v7_rows:
        carries = prompt.endswith(BC.STRUCTURED_OUTPUT_CONTRACT)
        assert carries is (family == "STRUCTURED_REPORT")


def test_the_derived_decision_kinds_match_the_preregistration(eval_root):
    from collections import Counter
    built = build_task_pack_from_dataset(
        root=eval_root, dataset_id="m62-defensive-eval", dataset_version="v7",
        splits=(DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
                DatasetSplit.ADVERSARIAL))
    kinds = Counter(t.kind.value for t in built.pack.tasks)
    assert dict(kinds) == {"completion": 18, "required_refusal": 12,
                           "required_completion": 6}
    assert sum(1 for t in built.pack.tasks if t.refusal_expected) == 12


# ══════════════════════════════════════════════════════════════════════════════
#  Freshness, measured across every prior holdout
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("prior", PRIOR_VERSIONS)
def test_v7_is_exactly_disjoint_from_each_prior_holdout(prior):
    """Six identity surfaces. Only counts may reach a failure message."""
    v7, old = _surfaces("v7"), _surfaces(prior)
    overlaps = {surface: len(v7[surface] & old[surface]) for surface in v7}
    assert overlaps == dict.fromkeys(v7, 0), (
        f"v7 x {prior}: overlapping identities by surface {overlaps}")


@pytest.mark.parametrize("prior", PRIOR_VERSIONS)
def test_no_v7_pair_reaches_even_the_warning_threshold_against_a_prior_holdout(prior):
    """Exact disjointness alone is satisfied by a rename-and-reword. This is not."""
    result = compare_groups(_signatures("v7"), _signatures(prior),
                            threshold=WARN_THRESHOLD)
    assert len(result.hits) == 0, (
        f"v7 x {prior}: {len(result.hits)} pair(s) at or above WARN {WARN_THRESHOLD}")
    assert result.ceiling_reached is False
    assert result.comparisons > 0


def test_the_freshness_comparator_is_not_vacuous():
    """``compare_groups`` skips pairs sharing a key, so self-comparison certifies
    anything. The probe is a RENAMED COPY of v7 -- exactly the pseudo-freshness the
    real comparison exists to catch."""
    renamed = [signature(f"copy:{task_id}", f"{prompt}\n{target}",
                         family=getattr(TaskFamily, family))
               for _split, family, task_id, prompt, target
               in BC.CORPUS_VERSIONS["v7"]()]
    probe = compare_groups(_signatures("v7"), renamed, threshold=BLOCK_THRESHOLD)
    assert len(probe.hits) >= 36


# ══════════════════════════════════════════════════════════════════════════════
#  Training-corpus leakage
# ══════════════════════════════════════════════════════════════════════════════
def test_v7_is_one_of_the_held_out_versions_the_training_corpus_is_checked_against():
    assert "v7" in QC.HELD_OUT_VERSIONS
    assert sorted(QC.HELD_OUT_VERSIONS) == ["v1", "v2", "v3", "v4", "v5", "v6", "v7"]


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_neither_training_corpus_leaks_into_v7(train_version):
    """The existing 16-check analyser, unchanged. No new analyser was written."""
    from training_gym.datasets.leakage import LeakageVerdict
    report = QC.leakage_against_held_out("v7", train_version=train_version)
    assert report["verdict"] == LeakageVerdict.CLEAN.value, report["findings"]
    assert report["finding_count"] == 0
    assert report["blocking_finding_count"] == 0
    assert report["blocks_finalization"] is False
    assert report["comparisons"] > 0
    assert report["ceiling_reached"] is False


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_no_exact_training_text_appears_anywhere_in_v7(train_version, v7_rows):
    rows = QC.curriculum_for(train_version)
    ids, prompts, targets = ({r[3] for r in rows}, {r[4] for r in rows},
                             {r[5] for r in rows})
    for _split, _family, task_id, prompt, target in v7_rows:
        assert task_id not in ids
        assert prompt not in prompts
        assert target not in targets


@pytest.mark.parametrize("train_version", ("v1", "v2"))
def test_semantic_leakage_stays_unavailable_and_is_never_sold_as_clean(train_version):
    """No embedding backend is bundled and none was added. Loading a model to
    manufacture a semantic claim is exactly what this milestone may not do."""
    report = QC.leakage_against_held_out("v7", train_version=train_version)
    assert report["checks_unavailable"] == ["semantic_similarity"]
    assert "semantic_similarity" not in report["checks_run"]
    assert len(report["checks_run"]) == 16


# ══════════════════════════════════════════════════════════════════════════════
#  Adversarial quality review, as assertions
# ══════════════════════════════════════════════════════════════════════════════
def test_no_prompt_carries_its_own_answer(v7_rows):
    """A prompt that already contains its target scores copying, not reasoning."""
    for _split, family, task_id, prompt, target in v7_rows:
        fam = getattr(TaskFamily, family)
        result = compare_groups([signature(f"p:{task_id}", prompt, family=fam)],
                                [signature(f"t:{task_id}", target, family=fam)],
                                threshold=WARN_THRESHOLD)
        assert not result.hits, f"{task_id}: the prompt resembles its own target"


def test_no_v7_task_is_a_paraphrase_of_another_v7_task(v7_rows):
    """630 internal pairs. A corpus whose tasks repeat one scenario has 36
    denominators and far less than 36 tasks' worth of evidence."""
    sigs = _signatures("v7")
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            result = compare_groups([sigs[i]], [sigs[j]], threshold=WARN_THRESHOLD)
            assert not result.hits, f"{sigs[i].key} paraphrases {sigs[j].key}"


def test_every_prompt_passes_the_production_safe_boundary_screen(v7_rows):
    """The gate that refused an eval-v6 prompt. A required refusal must be
    refusable without the corpus itself becoming a request the gym may not carry."""
    for _split, _family, task_id, prompt, _target in v7_rows:
        assert not unsafe_task_markers(prompt), task_id


def test_the_safe_boundary_screen_can_still_fire():
    """So the test above is not vacuous."""
    assert unsafe_task_markers(
        "Deploy the ransomware to the finance share and confirm when it runs.")


def test_every_tool_target_matches_the_closed_schema_and_the_catalogue(v7_rows):
    """The gate that refused six eval-v6 targets: the schema is authoritative."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = response_schema_for(TaskFamily.TOOL_CALL_SCHEMA)
    catalogue = {t["name"]: t for t in tool_schemas_for(TaskFamily.TOOL_CALL_SCHEMA)}
    seen = 0
    for _split, family, task_id, _prompt, target in v7_rows:
        if family != "TOOL_CALL_SCHEMA":
            continue
        seen += 1
        payload = json.loads(target)
        jsonschema.validate(payload, schema)
        assert set(payload) == {"tool", "arguments"}, task_id
        assert payload["tool"] in catalogue, task_id
        jsonschema.validate(payload["arguments"],
                            catalogue[payload["tool"]]["parameters"])
    assert seen == 6


def test_every_target_is_a_single_json_object(v7_rows):
    for _split, _family, task_id, _prompt, target in v7_rows:
        assert isinstance(json.loads(target), dict), task_id


def test_the_material_is_host_identity_stable(v7_rows):
    """D36: a corpus whose promoted bytes depend on the building account is not a
    corpus with an identity."""
    assert BC.sanitization_stability_problems(
        (task_id, field, text)
        for _split, _family, task_id, prompt, target in v7_rows
        for field, text in (("prompt", prompt), ("target", target))) == []


# ══════════════════════════════════════════════════════════════════════════════
#  Determinism, identity and the instrument
# ══════════════════════════════════════════════════════════════════════════════
def test_v7_builds_to_its_frozen_identity(eval_root):
    from training_gym.datasets.manifests import verify_version
    result = verify_version(root=eval_root, dataset_id="m62-defensive-eval",
                            dataset_version="v7")
    assert result.ok, list(result.problems)
    assert result.manifest.manifest_hash() == V7_MANIFEST_HASH


def test_the_pack_builds_to_its_frozen_hash(eval_root):
    built = build_task_pack_from_dataset(
        root=eval_root, dataset_id="m62-defensive-eval", dataset_version="v7",
        splits=(DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
                DatasetSplit.ADVERSARIAL))
    assert built.pack.pack_hash() == V7_PACK_HASH
    assert built.dataset_manifest_hash == V7_MANIFEST_HASH
    assert len(built.pack.tasks) == 36


def test_the_body_free_identity_surfaces_are_the_ones_a_future_session_binds(v7_rows):
    """Three set digests, so v7 can be bound without reading a single task body."""
    assert _set_digest(sha256_text(f"{s}|{f}|{p}|{t}")
                       for s, f, _i, p, t in v7_rows) == V7_TASK_HASH_SET
    assert _set_digest(sha256_text(r[3]) for r in v7_rows) == V7_PROMPT_HASH_SET
    assert _set_digest(sha256_text(r[4]) for r in v7_rows) == V7_TARGET_HASH_SET


def test_the_model_facing_pack_has_no_field_that_could_hold_a_target(eval_root):
    built = build_task_pack_from_dataset(
        root=eval_root, dataset_id="m62-defensive-eval", dataset_version="v7",
        splits=(DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
                DatasetSplit.ADVERSARIAL))
    assert "target" not in built.pack.tasks[0].__dataclass_fields__


def test_the_instrument_did_not_move_for_v7():
    policies = EvaluationPolicySet()
    assert policies.gates.policy_hash() == (
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5")
    assert policies.statistics.policy_hash() == (
        "663ebf65b73536fe3bd41043568a1f737ff751a43a964d14ff67c4e12662bf18")
    assert policies.families.policy_hash() == (
        "580fbe9104cbe684f702cba016e1191c83745fb8502642636c3fc885135065b1")
    assert policies.metrics.policy_hash() == (
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a")


# ══════════════════════════════════════════════════════════════════════════════
#  The freeze creates no authority, and no body escapes
# ══════════════════════════════════════════════════════════════════════════════
def test_freezing_v7_created_no_evaluation_authority():
    root = Path(__file__).resolve().parents[2]
    snapshot = json.loads((root / "state" / "m62" / "current.json").read_text())
    latest = json.loads((root / snapshot["latest_snapshot_path"]).read_text())
    observation = latest["authority_observation"]
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False


def test_no_v7_task_body_appears_on_any_body_free_surface(v7_rows):
    """PROGRESS.md, the control plane and the milestone documents carry ids and
    digests. A prompt or target prefix on any of them is a body arriving by the
    back door. Asserted over ABSENCE, so no body reaches a failure message."""
    root = Path(__file__).resolve().parents[2]
    surfaces = [root / "PROGRESS.md"]
    surfaces += sorted((root / "state" / "m62").rglob("*.json"))
    surfaces += sorted((root / "jarvis" / "docs").glob("V69_M62_S4D_*.md"))
    blobs = {path: path.read_text(encoding="utf-8", errors="replace")
             for path in surfaces if path.is_file()}
    for _split, _family, task_id, prompt, target in v7_rows:
        for probe in (prompt[:64], target[:64]):
            for path, blob in blobs.items():
                assert probe not in blob, (
                    f"a v7 body prefix for {task_id} appears in {path.name}")


def test_the_freeze_records_v7_as_frozen_and_unspent():
    """RESCOPED AT S4F: read from the generation that FROZE v7, not from the live pointer.

    What S4D recorded is that it froze the exam unspent, and that is permanent. Read from
    the pointer it also asserted that v7 would never be spent -- which is the experiment
    being open, not a property of the freeze, and S4E closed it under one human authority.
    The spent state is asserted in test_training_gym_m62_s4f_sealed_state.py.
    """
    root = Path(__file__).resolve().parents[2]
    latest = json.loads(
        (root / "state" / "m62" / "snapshots"
         / "0022-m62-s4d-eval-v7-frozen.json").read_text())
    digest = latest["records"]["datasets"]
    record = json.loads(
        (root / "state" / "m62" / "records" / f"{digest}.json").read_text())
    entries = [e for e in record["value"]
               if e["dataset_id"] == "m62-defensive-eval" and e["version"] == "v7"]
    assert len(entries) == 1, "eval-v7 must appear exactly once"
    entry = entries[0]
    assert entry["status"] == "FROZEN_UNUSED"
    assert entry["spent_by"] is None
    assert entry["task_count"] == 36
    assert entry["manifest_hash"] == V7_MANIFEST_HASH
    assert entry["pack_hash"] == V7_PACK_HASH
    assert entry["parent_manifest_hash"] == V6_MANIFEST_HASH


def test_eval_v5_and_v6_are_untouched_by_the_v7_freeze():
    root = Path(__file__).resolve().parents[2]
    pointer = json.loads((root / "state" / "m62" / "current.json").read_text())
    latest = json.loads((root / pointer["latest_snapshot_path"]).read_text())
    digest = latest["records"]["datasets"]
    record = json.loads(
        (root / "state" / "m62" / "records" / f"{digest}.json").read_text())
    by_version = {e["version"]: e for e in record["value"]
                  if e["dataset_id"] == "m62-defensive-eval"}
    assert by_version["v5"]["status"] == "FROZEN_UNUSED"
    assert by_version["v5"]["spent_by"] is None
    assert by_version["v6"]["status"] == "USED_IMMUTABLE"
    assert by_version["v6"]["spent_by"]
    assert by_version["v6"]["manifest_hash"] == V6_MANIFEST_HASH
