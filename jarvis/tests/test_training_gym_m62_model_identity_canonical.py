"""V69 M62 S3E.1 — durable model identity, separated from descriptive annotation.

WHAT THIS FILE IS DEFENDING
---------------------------
``ModelIdentity.identity_hash`` covers the whole record, and three of the fields in that
record say nothing about which weights load: ``cache_status`` and ``cache_evidence`` are
*host state*, and ``license_reference`` is *documentation*. Hashing them together with
the model id and the commit sha produced a digest that changed when an operator moved
their model cache or reworded a licence note.

That is not a theoretical defect. The adapter this milestone actually trained recorded
``base_model_identity_hash`` under one set of annotations; the shipped evaluation config
template describes the same bytes with a different licence sentence. The two digests
differ, so the pairing check refused a comparison between a model and an adapter fitted
onto exactly those weights.

The correction is additive. The legacy digest is untouched — every manifest already on
disk still verifies against it — and a separately versioned canonical digest covers only
what an execution resolves. These tests pin both halves of that: annotations must not
move the canonical digest, and nothing about the weights may hide from it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from training_gym.evaluation import references as R
from training_gym.schemas import SchemaError
from training_gym.training import model_identity as MI

REVISION = "c" * 40
OTHER_REVISION = "d" * 40


def identity(**overrides) -> MI.ModelIdentity:
    """A well-formed identity. Overrides express exactly one difference at a time."""
    fields = {
        "provider": "huggingface",
        "model_id": "Qwen/Qwen3-0.6B",
        "revision": REVISION,
        "parameters_b": 0.6,
        "family": "qwen3",
        "tokenizer_id": "Qwen/Qwen3-0.6B",
        "tokenizer_revision": REVISION,
    }
    fields.update(overrides)
    return MI.ModelIdentity(**fields)


# ── annotations must not participate ──────────────────────────────────────────
@pytest.mark.parametrize("annotation", [
    {"license_reference": "see the model card on the hub"},
    {"license_reference": "Apache-2.0, per the repository LICENSE file"},
    {"cache_status": MI.CacheStatus.PRESENT, "cache_evidence": "a" * 64},
    {"cache_status": MI.CacheStatus.ABSENT},
    {"parameters_b": 0.7},
    {"family": "qwen"},
    {"provider": "some-other-mirror"},
])
def test_an_annotation_never_changes_the_canonical_identity(annotation):
    """Host state and prose describe the model; they do not select it."""
    assert identity(**annotation).canonical_identity_hash() == \
        identity().canonical_identity_hash()


def test_the_legacy_hash_still_moves_under_an_annotation():
    """The defect is documented by test, not quietly repaired underneath old records.

    Every manifest on disk was written against this behaviour. If this assertion ever
    fails, the legacy digest changed meaning and those manifests stopped verifying.
    """
    assert identity(license_reference="x").identity_hash() != identity().identity_hash()


def test_the_two_digests_are_never_the_same_value():
    """Separately versioned, so one can never be mistaken for the other."""
    ident = identity()
    assert ident.canonical_identity_hash() != ident.identity_hash()


def test_the_canonical_record_carries_no_annotation_field():
    record = identity(license_reference="anything",
                      cache_status=MI.CacheStatus.PRESENT,
                      cache_evidence="b" * 64).canonical_identity()
    for absent in ("license_reference", "cache_status", "cache_evidence",
                   "parameters_b", "family", "provider"):
        assert absent not in record, f"{absent} is an annotation and must not bind bytes"
    assert record["canonical_version"] == MI.CANONICAL_MODEL_IDENTITY_VERSION


# ── everything that selects weights must participate ──────────────────────────
def test_a_different_revision_is_a_different_identity():
    assert identity(revision=OTHER_REVISION,
                    tokenizer_revision=OTHER_REVISION).canonical_identity_hash() != \
        identity().canonical_identity_hash()


def test_a_different_model_id_is_a_different_identity():
    assert identity(model_id="Qwen/Qwen3-1.7B",
                    tokenizer_id="Qwen/Qwen3-1.7B").canonical_identity_hash() != \
        identity().canonical_identity_hash()


def test_a_substituted_tokenizer_cannot_be_expressed_at_all():
    """Stronger than "changes the hash": the identity refuses to exist.

    A tokenizer from a different repository changes what every token id means and
    produces no error anywhere downstream, so this is refused rather than recorded.
    """
    with pytest.raises(SchemaError):
        MI.canonical_identity_fields(model_id="Qwen/Qwen3-0.6B", revision=REVISION,
                                     tokenizer_id="mistralai/Mistral-7B-v0.1",
                                     tokenizer_revision=REVISION)


def test_a_tokenizer_pinned_to_another_revision_is_refused():
    with pytest.raises(SchemaError):
        MI.canonical_identity_fields(model_id="Qwen/Qwen3-0.6B", revision=REVISION,
                                     tokenizer_revision=OTHER_REVISION)


def test_remote_code_is_refused_rather_than_hashed():
    with pytest.raises(SchemaError):
        MI.canonical_identity_fields(model_id="Qwen/Qwen3-0.6B", revision=REVISION,
                                     requires_remote_code=True)


@pytest.mark.parametrize("revision", ["", "   ", "main", "v1.0", "refs/pr/3", "c" * 39])
def test_an_empty_or_mutable_revision_never_yields_an_executable_identity(revision):
    """A branch name is well-formed and not executable; an empty one is neither."""
    try:
        fields = MI.canonical_identity_fields(model_id="Qwen/Qwen3-0.6B",
                                              revision=revision)
    except SchemaError:
        return  # refused outright, which is the stronger answer
    assert fields["revision_kind"] != MI.RevisionKind.IMMUTABLE_COMMIT.value


def test_the_bridge_and_the_method_agree():
    """The compatibility bridge must not be a second, weaker opinion about identity."""
    ident = identity(license_reference="ignored", cache_status=MI.CacheStatus.PRESENT,
                     cache_evidence="c" * 64)
    assert MI.canonical_identity_hash(
        model_id=ident.model_id, revision=ident.revision,
        tokenizer_id=ident.tokenizer_id,
        tokenizer_revision=ident.tokenizer_revision) == ident.canonical_identity_hash()


# ── pairing, which is where the defect actually bit ───────────────────────────
ADAPTER_RUN = Path(__file__).resolve().parents[1] / "logs" / "training_runs" / \
    "runs" / "qwen3-06b-lora-smoke-live-004"


def base_reference(**overrides) -> R.BaseModelEvaluationReference:
    return R.base_reference_from_identity(identity(**overrides))


def test_two_identical_models_pair_whatever_their_annotations_say():
    """The exact failure that blocked the first live evaluation attempt."""
    trained_under = base_reference(cache_status=MI.CacheStatus.PRESENT,
                                   cache_evidence="a" * 64)
    described_as = base_reference(license_reference="see the model card on the hub")
    assert trained_under.base_model_identity_hash != \
        described_as.base_model_identity_hash, "the legacy digests must still differ"
    assert trained_under.base_model_canonical_identity_hash == \
        described_as.base_model_canonical_identity_hash


def synthetic_adapter(**overrides) -> R.AdapterEvaluationReference:
    """A structurally valid adapter reference, for the paths a real run cannot reach."""
    from training_gym.training.config import TrainingMethod, TrainingRunState
    fields = {
        "run_id": "synthetic-run", "adapter_manifest_hash": "1" * 64,
        "adapter_artifact_tree_hash": "2" * 64, "plan_hash": "3" * 64,
        "training_config_hash": "4" * 64,
        "base_model_identity_hash": identity().identity_hash(),
        "base_model_canonical_identity_hash": identity().canonical_identity_hash(),
        "tokenizer_identity_hash": identity().tokenizer_identity_hash(),
        "tokenizer_chat_template_hash": "5" * 64, "dataset_reference_hash": "6" * 64,
        "dataset_manifest_hash": "7" * 64, "train_shard_hash": "8" * 64,
        "validation_shard_hash": "9" * 64, "hidden_evaluation_hash": "a" * 64,
        "security_regression_hash": "b" * 64, "method": TrainingMethod.SFT_LORA,
        "lora": {}, "run_state": TrainingRunState.COMPLETED.value,
        "artifact_verified": True,
    }
    fields.update(overrides)
    return R.AdapterEvaluationReference(**fields)


def test_a_pair_missing_a_canonical_digest_falls_back_to_the_strict_legacy_check():
    """Absent is not "matches". The fallback refuses more, never less."""
    legacy_base = R.BaseModelEvaluationReference(
        **{**{f: getattr(base_reference(), f) for f in (
            "base_model_id", "base_model_revision", "base_model_identity_hash",
            "tokenizer_id", "tokenizer_revision", "tokenizer_identity_hash")},
           "base_model_canonical_identity_hash": ""})
    matching = synthetic_adapter(base_model_canonical_identity_hash="")
    assert R.pairing_blockers(legacy_base, matching) == (), \
        "identical legacy digests must still pair when no canonical digest exists"
    differing = synthetic_adapter(base_model_canonical_identity_hash="",
                                  base_model_identity_hash="f" * 64)
    assert R.pairing_blockers(legacy_base, differing), \
        "without a canonical digest a legacy mismatch must still refuse"


def test_a_canonical_mismatch_refuses_even_when_the_legacy_digests_agree():
    """The durable check may not be softened by an agreeing annotation blob."""
    base = base_reference()
    impostor = synthetic_adapter(
        base_model_identity_hash=base.base_model_identity_hash,
        base_model_canonical_identity_hash="e" * 64)
    assert R.pairing_blockers(base, impostor)


@pytest.mark.skipif(not ADAPTER_RUN.is_dir(),
                    reason="the live smoke adapter is a runtime artefact, not tracked")
class TestTheRecordedAdapter:
    """The real run-004 artefacts, which no test may rewrite."""

    @pytest.fixture(scope="class")
    def adapter(self) -> R.AdapterEvaluationReference:
        return R.build_adapter_reference(ADAPTER_RUN, lora={},
                                         base_model_id="Qwen/Qwen3-0.6B")

    def test_the_legacy_manifest_digest_is_carried_through_untouched(self, adapter):
        recorded = json.loads((ADAPTER_RUN / "adapter-manifest.json")
                              .read_text(encoding="utf-8"))
        assert adapter.base_model_identity_hash == recorded["base_model_identity_hash"]

    def test_its_durable_identity_is_re_derived_not_read_back(self, adapter):
        recorded = json.loads((ADAPTER_RUN / "adapter-manifest.json")
                              .read_text(encoding="utf-8"))
        assert adapter.base_model_canonical_identity_hash == MI.canonical_identity_hash(
            model_id=recorded["base_model_id"],
            revision=recorded["base_model_revision"],
            tokenizer_id=recorded["tokenizer_id"],
            tokenizer_revision=recorded["tokenizer_revision"])

    def test_it_pairs_with_the_baseline_the_shipped_template_describes(self, adapter):
        """No config string had to be edited to make this pass."""
        template = json.loads(
            (Path(__file__).resolve().parents[1] / "evaluation" / "configs" /
             "qwen3-0.6b-adapter-eval.json").read_text(encoding="utf-8"))
        declared = template["baseline_model"]
        recorded = json.loads((ADAPTER_RUN / "adapter-manifest.json")
                              .read_text(encoding="utf-8"))
        baseline = R.base_reference_from_identity(MI.ModelIdentity(
            provider=declared["provider"], model_id=declared["model_id"],
            revision=recorded["base_model_revision"],
            parameters_b=declared["parameters_b"], family=declared["family"],
            tokenizer_id=declared["tokenizer_id"],
            tokenizer_revision=recorded["tokenizer_revision"],
            cache_status=MI.CacheStatus.PRESENT, cache_evidence="e" * 64,
            license_reference=declared["license_reference"]))
        assert baseline.base_model_identity_hash != adapter.base_model_identity_hash, \
            "if these ever match, this test has stopped exercising the defect"
        assert R.pairing_blockers(baseline, adapter) == ()

    def test_a_genuinely_different_model_is_still_refused(self, adapter):
        wrong = R.base_reference_from_identity(identity(
            model_id="Qwen/Qwen3-1.7B", tokenizer_id="Qwen/Qwen3-1.7B",
            cache_status=MI.CacheStatus.PRESENT))
        assert R.pairing_blockers(wrong, adapter), \
            "a different base model must never pair, whatever its annotations say"
