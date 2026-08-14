"""V69 M62 S3I.1 — D34: ``m62-defensive-eval v2`` has one identity, not two.

WHAT D34 WAS
------------
:class:`~training_gym.datasets.promotion_plan.PromotionRequest` defaults
``parent_manifest_hash`` to the empty string, and ``resolved_parent()`` then falls back to
``latest_manifest_hash(root, dataset_id)`` — a **discovery over the destination root**.
``scripts/build_evaluation_corpus.py`` never set the field, so the lineage of ``v2`` was
decided by what happened to be on disk when it was built:

===============================================  ==================  ================
build                                            ``parent``          ``manifest_hash``
===============================================  ==================  ================
``v2`` into a root that already holds ``v1``     ``0970600c…``       ``82b60bfd…``
``v2`` into a clean root                         ``genesis``         ``10ad2308…``
===============================================  ==================  ================

Both are honest outputs of the same tracked generator over **byte-identical** material:
the three shards do not differ by one byte, and the only fields that move are
``parent_manifest_hash`` and the ``manifest_hash`` derived from it. The corpus's identity
was therefore a function of incidental build history rather than of the corpus, which is
the shape that spends a single-use ``EVAL:`` token and then discovers the digest it bound
is not the digest the execution root reproduces.

WHAT THESE TESTS PIN
--------------------
That ``v2`` now *declares* ``v1`` as its parent instead of discovering one, that the
declaration holds no matter what is or is not in the destination root, that a build which
cannot establish its declared parent **fails closed** rather than quietly becoming a
genesis, and that none of this moved a single byte of held-out task material.

These tests fail against the pre-fix generator: ``test_v2_in_a_clean_root_is_not_a_genesis``
and ``test_v2_identity_is_independent_of_build_order`` both observed ``10ad2308…`` /
``genesis`` before the fix.
"""
from __future__ import annotations

import collections
import hashlib
import json

import pytest

from training_gym.datasets.manifests import GENESIS_PARENT, verify_version

pytest.importorskip("scripts.build_evaluation_corpus")
from scripts import build_evaluation_corpus as _BUILDER  # noqa: E402

#: The frozen genesis identity of ``v1``. S3E.2 drew the measurement of record from it.
V1_MANIFEST = "0970600c677c89112db972c6024634aa871be92dee303db7f429c90967d3dd3b"
#: The canonical identity of ``v2`` under D34: explicitly parented on ``v1``.
V2_CANONICAL_MANIFEST = "82b60bfdbea263eef3990eb6e49c2f2ca16e9b9e26ec8ac435f314b374279d60"
#: The historical genesis-lineage identity. Legitimate history, non-canonical for future
#: eligibility. Recorded here so "these two digests differ" never gets rediscovered.
V2_HISTORICAL_GENESIS_MANIFEST = (
    "10ad2308391567eeaa043001835b0c77a02473b26d2f83c0fb54a32d885b9df0")

SHARDS = ("adversarial.jsonl", "hidden_evaluation.jsonl", "security_regression.jsonl")

#: The shard digests, which D34 must not move. Recorded from the pre-fix genesis build and
#: the pre-fix parented build alike — they were already equal across both lineages.
SHARD_DIGESTS = {
    "adversarial.jsonl":
        "8738652dce7971f6176c84919c2496d0b02d970f65726d6d8da0d62e16b11e5a",
    "hidden_evaluation.jsonl":
        "d75fb843e6cd2f1791fed0cef6a8ee90b633206806fcb03031233ff69662f74a",
    "security_regression.jsonl":
        "af1dc5bb7235bc041920883f9cb72269944ae5a1f028d77c66f1790b1151614f",
}


def _version_dir(root, version):
    return root / "datasets" / _BUILDER.DATASET_ID / version


def _manifest(root, version):
    return json.loads((_version_dir(root, version) / "manifest.json").read_text("utf-8"))


def _shard_digests(root, version):
    base = _version_dir(root, version)
    return {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in SHARDS}


@pytest.fixture(scope="module")
def clean_root_v2(tmp_path_factory):
    """``v2`` built into an otherwise empty root — the exact D34 scenario."""
    root = tmp_path_factory.mktemp("d34-clean")
    return root, _BUILDER.build(root, dataset_version="v2")


@pytest.fixture(scope="module")
def sequential_v2(tmp_path_factory):
    """``v1`` first, then ``v2`` — the lineage the historical parented build formed."""
    root = tmp_path_factory.mktemp("d34-sequential")
    _BUILDER.build(root, dataset_version="v1")
    return root, _BUILDER.build(root, dataset_version="v2")


@pytest.fixture(scope="module")
def genesis_lineage_v2(tmp_path_factory):
    """``v2`` rebuilt under the *historical* genesis lineage, for comparison only.

    Not a canonical build and never used as authority — it exists so the claim "only the
    lineage moved" can be tested against the thing it is a claim about.
    """
    root = tmp_path_factory.mktemp("d34-historical-genesis")
    original = _BUILDER.CANONICAL_LINEAGE
    _BUILDER.CANONICAL_LINEAGE = {**original, "v2": None}
    try:
        summary = _BUILDER.build(root, dataset_version="v2")
    finally:
        _BUILDER.CANONICAL_LINEAGE = original
    assert summary["manifest_hash"] == V2_HISTORICAL_GENESIS_MANIFEST
    return root


# ══════════════════════════════════════════════════════════════════════════════
#  1-2. The lineage is declared, and v1 keeps the identity it always had
# ══════════════════════════════════════════════════════════════════════════════
def test_the_canonical_lineage_is_declared_not_discovered():
    """``v1`` is a genesis on purpose; ``v2`` names the digest it descends from."""
    assert _BUILDER.canonical_parent_for("v1") is None
    assert _BUILDER.canonical_parent_for("v2") == ("v1", V1_MANIFEST)
    assert _BUILDER.CANONICAL_V1_MANIFEST == V1_MANIFEST


def test_a_version_with_no_declared_lineage_is_refused():
    """Not defaulted to genesis, not guessed from disk — refused."""
    with pytest.raises(ValueError, match="declares no canonical lineage"):
        _BUILDER.canonical_parent_for("v3")


def test_v1_identity_does_not_move(sequential_v2):
    root, _ = sequential_v2
    assert _manifest(root, "v1")["manifest_hash"] == V1_MANIFEST
    assert _manifest(root, "v1")["parent_manifest_hash"] == GENESIS_PARENT


# ══════════════════════════════════════════════════════════════════════════════
#  3-6. The regression itself: identity no longer depends on disk history
# ══════════════════════════════════════════════════════════════════════════════
def test_v2_in_a_clean_root_is_not_a_genesis(clean_root_v2):
    """**The D34 regression.** Pre-fix this was ``genesis`` / ``10ad2308…``."""
    root, summary = clean_root_v2
    manifest = _manifest(root, "v2")
    assert manifest["parent_manifest_hash"] != GENESIS_PARENT
    assert manifest["parent_manifest_hash"] == V1_MANIFEST
    assert manifest["manifest_hash"] == V2_CANONICAL_MANIFEST
    assert manifest["manifest_hash"] != V2_HISTORICAL_GENESIS_MANIFEST
    assert summary["parent_manifest_hash"] == V1_MANIFEST


def test_v2_in_a_clean_root_materialises_its_declared_parent(clean_root_v2):
    """Fail-closed means *establish the parent*, not *skip the check*."""
    root, _ = clean_root_v2
    assert _version_dir(root, "v1").is_dir()
    result = verify_version(root=root, dataset_id=_BUILDER.DATASET_ID,
                            dataset_version="v1")
    assert result.ok and result.manifest is not None
    assert result.manifest.manifest_hash() == V1_MANIFEST


def test_v2_identity_is_independent_of_build_order(clean_root_v2, sequential_v2):
    """Clean-root and v1-first builds are the same dataset version, digest included."""
    a, b = _manifest(clean_root_v2[0], "v2"), _manifest(sequential_v2[0], "v2")
    assert a["manifest_hash"] == b["manifest_hash"] == V2_CANONICAL_MANIFEST
    assert a["parent_manifest_hash"] == b["parent_manifest_hash"] == V1_MANIFEST


def test_unrelated_dataset_state_cannot_change_v2_identity(tmp_path, sequential_v2):
    """Another dataset in the same root is not this dataset's lineage."""
    root = tmp_path / "polluted"
    foreign = root / "datasets" / "some-other-dataset" / "v9"
    foreign.mkdir(parents=True)
    (foreign / "manifest.json").write_text('{"not": "a real manifest"}', "utf-8")
    summary = _BUILDER.build(root, dataset_version="v2")
    assert summary["manifest_hash"] == V2_CANONICAL_MANIFEST
    assert summary["parent_manifest_hash"] == V1_MANIFEST


def test_independent_roots_agree(tmp_path, clean_root_v2):
    """A fourth root, built from nothing, lands on the same digest."""
    summary = _BUILDER.build(tmp_path / "independent", dataset_version="v2")
    assert summary["manifest_hash"] == V2_CANONICAL_MANIFEST
    assert summary["manifest_hash"] == _manifest(clean_root_v2[0], "v2")["manifest_hash"]


# ══════════════════════════════════════════════════════════════════════════════
#  7. Fail closed — never silently degrade the lineage
# ══════════════════════════════════════════════════════════════════════════════
def test_a_parent_that_is_not_the_declared_one_is_refused(tmp_path, monkeypatch):
    """If ``v1`` on disk is not the declared parent, ``v2`` refuses to descend from it.

    The alternative behaviours are the two D34 failure modes: adopt whatever version is
    present, or fall back to genesis. Both mint a second identity for the same material.
    """
    root = tmp_path / "wrong-parent"
    _BUILDER.build(root, dataset_version="v1")
    monkeypatch.setattr(_BUILDER, "CANONICAL_LINEAGE",
                        {**_BUILDER.CANONICAL_LINEAGE, "v2": ("v1", "0" * 64)})
    with pytest.raises(RuntimeError, match="not the one it declares"):
        _BUILDER.build(root, dataset_version="v2")
    assert not _version_dir(root, "v2").exists()


def test_a_parent_that_does_not_verify_is_refused(tmp_path):
    """A corrupted parent version is not a parent."""
    root = tmp_path / "broken-parent"
    _BUILDER.build(root, dataset_version="v1")
    shard = _version_dir(root, "v1") / "adversarial.jsonl"
    shard.write_text(shard.read_text("utf-8") + '\n{"tampered": true}\n', "utf-8")
    with pytest.raises(RuntimeError, match="does not verify"):
        _BUILDER.build(root, dataset_version="v2")


# ══════════════════════════════════════════════════════════════════════════════
#  8-9. Lineage is part of identity, and content is separately observable
# ══════════════════════════════════════════════════════════════════════════════
def test_lineage_is_part_of_manifest_identity(clean_root_v2):
    """The two historical digests differ *only* because the parent field differs.

    This is what makes D34 a lineage defect rather than a corpus defect, and it is also
    why the fix could not simply drop the parent link: the field is load-bearing.
    """
    from training_gym.datasets.manifests import DatasetVersionManifest

    manifest = dict(_manifest(clean_root_v2[0], "v2"))
    assert manifest["manifest_hash"] == V2_CANONICAL_MANIFEST

    # Swap only the parent, and declare the historical genesis digest. ``from_dict``
    # refuses a mismatched digest, so it accepting this payload *is* the proof that
    # ``10ad2308…`` is this exact corpus under the other lineage, and that the parent
    # field is what separates the two.
    regressed = DatasetVersionManifest.from_dict(
        {**manifest, "parent_manifest_hash": GENESIS_PARENT,
         "manifest_hash": V2_HISTORICAL_GENESIS_MANIFEST})
    assert regressed.manifest_hash() == V2_HISTORICAL_GENESIS_MANIFEST
    assert regressed.manifest_hash() != V2_CANONICAL_MANIFEST


def test_task_shards_are_byte_identical_across_both_lineages(clean_root_v2,
                                                             sequential_v2):
    """The held-out material is the same bytes it has always been."""
    assert _shard_digests(clean_root_v2[0], "v2") == SHARD_DIGESTS
    assert _shard_digests(sequential_v2[0], "v2") == SHARD_DIGESTS


# ══════════════════════════════════════════════════════════════════════════════
#  10-12. The corpus did not change
# ══════════════════════════════════════════════════════════════════════════════
def test_the_corpus_counts_are_untouched(clean_root_v2):
    root, summary = clean_root_v2
    assert summary["candidates_built"] == summary["promoted_records"] == 36
    assert summary["by_split"] == {"HIDDEN_EVALUATION": 12, "SECURITY_REGRESSION": 12,
                                   "ADVERSARIAL": 12}
    assert summary["by_family"] == {"SAFETY_REFUSAL": 12, "STRUCTURED_REPORT": 9,
                                    "EVIDENCE_REQUEST": 9, "TOOL_CALL_SCHEMA": 6}
    candidates = _manifest(root, "v2")["candidates"]
    assert len(candidates) == 36
    assert collections.Counter(c["split"] for c in candidates) == {
        "hidden_evaluation": 12, "security_regression": 12, "adversarial": 12}
    assert {c["evaluation_only"] for c in candidates} == {True}
    assert {c["dataset_eligible"] for c in candidates} == {False}


def test_leakage_remains_clean(clean_root_v2):
    _root, summary = clean_root_v2
    assert summary["leakage_verdict"] == "clean"
    assert summary["leakage_findings"] == 0


def test_the_v2_material_is_still_derived_from_v1(clean_root_v2):
    """D34 is a lineage fix. The one deliberate S3F.2 content difference is unchanged."""
    v1_entries = _BUILDER.corpus()
    v2_entries = _BUILDER.corpus_v2()
    assert len(v1_entries) == len(v2_entries) == 36
    changed = [b for a, b in zip(v1_entries, v2_entries, strict=True) if a != b]
    assert len(changed) == 9
    assert {e[1] for e in changed} == {"STRUCTURED_REPORT"}
    for (split_a, fam_a, id_a, _p, tgt_a), (split_b, fam_b, id_b, _q, tgt_b) in zip(
            v1_entries, v2_entries, strict=True):
        assert (split_a, fam_a, id_a, tgt_a) == (split_b, fam_b, id_b, tgt_b)


def test_only_provenance_moves_in_the_task_pack(clean_root_v2, genesis_lineage_v2):
    """What the model is shown is unchanged; only the pointer to the version moved.

    The pack's identity follows the dataset version's identity, so ``pack_hash`` and
    ``task_identity_hashes`` legitimately differ between the two lineages. This test
    exists so nobody reads that as the corpus having changed: of the task-record fields,
    ``source_dataset_manifest_hash`` is the only one that moves, and ``task_hash`` — the
    digest over the task material itself — does not.
    """
    from training_gym.datasets.candidate import DatasetSplit
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset

    splits = (DatasetSplit.HIDDEN_EVALUATION, DatasetSplit.SECURITY_REGRESSION,
              DatasetSplit.ADVERSARIAL)

    def records(root):
        return build_task_pack_from_dataset(
            root=root, dataset_id=_BUILDER.DATASET_ID, dataset_version="v2",
            splits=splits, generation=1).pack.task_records()

    canonical, historical = records(clean_root_v2[0]), records(genesis_lineage_v2)
    assert len(canonical) == len(historical) == 36

    moved = {key for a, b in zip(canonical, historical, strict=True)
             for key in set(a) | set(b) if a.get(key) != b.get(key)}
    assert moved == {"source_dataset_manifest_hash"}

    for a, b in zip(canonical, historical, strict=True):
        assert a["user_prompt"] == b["user_prompt"]
        assert a["system_prompt"] == b["system_prompt"]
        assert a["task_hash"] == b["task_hash"]
        assert a["source_shard_hash"] == b["source_shard_hash"]
        assert a["expected_output_schema"] == b["expected_output_schema"]
        assert a["refusal_expected"] == b["refusal_expected"]
    assert {r["source_dataset_manifest_hash"] for r in canonical} == \
        {V2_CANONICAL_MANIFEST}


# ══════════════════════════════════════════════════════════════════════════════
#  13. Legitimate genesis semantics survive
# ══════════════════════════════════════════════════════════════════════════════
def test_a_genuine_genesis_is_still_representable(tmp_path):
    """``v1`` is a real genesis and must stay one. The defect was silent degradation of a
    version that *declares* a parent, not the existence of genesis datasets."""
    summary = _BUILDER.build(tmp_path / "genesis-ok", dataset_version="v1")
    assert summary["parent_manifest_hash"] == GENESIS_PARENT
    assert summary["manifest_hash"] == V1_MANIFEST


def test_the_generic_promotion_api_still_discovers_a_parent_when_asked(tmp_path):
    """Unrelated datasets that rely on ``latest_manifest_hash`` are not affected.

    The fix is scoped to this corpus declaring its own lineage; the generic default was
    left alone deliberately, because other datasets legitimately chain onto their newest
    version and changing that would be a dataset-subsystem rewrite, not a D34 fix.
    """
    from training_gym.datasets.manifests import latest_manifest_hash
    root = tmp_path / "generic"
    assert latest_manifest_hash(root=root, dataset_id=_BUILDER.DATASET_ID) \
        == GENESIS_PARENT
    _BUILDER.build(root, dataset_version="v1")
    assert latest_manifest_hash(root=root, dataset_id=_BUILDER.DATASET_ID) == V1_MANIFEST
