"""tests/test_training_gym_m62_training_config.py — V69 M62 S3A: the config fails closed.

A training configuration is the widest attack surface this milestone adds: it is a file,
it is edited by hand, and everything downstream trusts it. The claim under test is that
the things a config *cannot* express are unrepresentable rather than merely discouraged,
and that everything it can express is bounded.

The negative tests dominate on purpose. A config schema that accepts a misspelt
``trust_remote_code``, a duplicated key, a fractional epoch count or a tokenizer from a
different repository is a schema that validates cleanly and means something other than
what its reviewer read.
"""
from __future__ import annotations

import json

import pytest

from training_gym.schemas import SchemaError
from training_gym.training.config import (
    REQUIRED_REVISION_PLACEHOLDER,
    TRAINING_SCHEMA_VERSION,
    CheckpointStrategy,
    DevicePolicy,
    LoRAConfig,
    LoRATargetPolicy,
    LoggingTarget,
    ModelDownloadPolicy,
    PrecisionPolicy,
    S3A_REACHABLE_STATES,
    TrainingConfig,
    TrainingMethod,
    TrainingResourcePolicy,
    TrainingRunState,
    TrainingTransitionError,
    check_training_transition,
    is_immutable_revision,
    legacy_method,
    load_config_document,
    load_training_config,
)
from training_gym.training.dataset_reference import (
    ExportFormat,
    TrainingDatasetReference,
    is_placeholder,
)
from training_gym.training.model_identity import (
    CacheStatus,
    ModelIdentity,
    RevisionKind,
    cache_directory_name,
    classify_revision,
    identity_from_config,
    probe_cache,
)

_COMMIT = "a" * 40


# ── builders, defined locally so this module resolves from either layout ──────
def make_reference(**overrides) -> TrainingDatasetReference:
    base = dict(dataset_id="gym", dataset_version="v1")
    base.update(overrides)
    return TrainingDatasetReference.placeholder(**base)


def make_config(**overrides) -> TrainingConfig:
    base = dict(
        run_id="smoke-001", experiment_name="qwen3 0.6b lora smoke",
        method=TrainingMethod.SFT_LORA, base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision=REQUIRED_REVISION_PLACEHOLDER,
        base_model_parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
        tokenizer_revision=REQUIRED_REVISION_PLACEHOLDER,
        dataset_reference=make_reference(), output_root_id="local-training",
        created_at_utc="2026-08-02T00:00:00Z")
    base.update(overrides)
    return TrainingConfig(**base)


def make_document(**overrides) -> dict:
    doc = make_config().to_dict()
    doc.update(overrides)
    return doc


# ══════════════════════════════════════════════════════════════════════════════
#  It parses, and the parse is deterministic
# ══════════════════════════════════════════════════════════════════════════════
def test_a_valid_sft_lora_smoke_config_parses():
    config = make_config()
    assert config.method is TrainingMethod.SFT_LORA
    assert config.effective_batch_size == 8
    assert config.trust_remote_code is False


def test_the_config_hash_is_deterministic():
    assert make_config().config_hash() == make_config().config_hash()
    assert len(make_config().config_hash()) == 64


def test_key_order_does_not_change_the_config_hash():
    doc = make_document()
    reordered = dict(reversed(list(doc.items())))
    assert TrainingConfig.from_dict(reordered).config_hash() == \
        TrainingConfig.from_dict(doc).config_hash()


def test_a_config_survives_serialization_and_reload():
    config = make_config()
    reloaded = TrainingConfig.from_dict(json.loads(json.dumps(config.to_dict())))
    assert reloaded.config_hash() == config.config_hash()


@pytest.mark.parametrize("field,value", [
    ("seed", 43), ("epochs", 2), ("learning_rate", 1e-4), ("max_sequence_length", 256),
    ("batch_size", 2), ("gradient_accumulation_steps", 4), ("max_steps", 50),
])
def test_changing_any_hyperparameter_changes_the_config_hash(field, value):
    assert make_config(**{field: value}).config_hash() != make_config().config_hash()


def test_the_lora_configuration_is_inside_the_hash():
    assert make_config(lora=LoRAConfig(rank=16, alpha=32)).config_hash() != \
        make_config().config_hash()


# ══════════════════════════════════════════════════════════════════════════════
#  Unknown fields, versions, and the document reader
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unknown_config_field_is_refused():
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingConfig.from_dict(make_document(traih_remote_code=True))


def test_a_misspelt_security_field_does_not_load_with_the_safe_default_applied():
    """The danger is not the typo — it is loading anyway and looking correct."""
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingConfig.from_dict(make_document(trust_remote_codee=False))


@pytest.mark.parametrize("declared", ["", "m61.training_config.1", "m63.x.1", "1"])
def test_an_unsupported_schema_version_is_refused(declared):
    with pytest.raises(SchemaError, match="schema_version"):
        TrainingConfig.from_dict(make_document(schema_version=declared))


def test_the_current_schema_version_is_accepted():
    assert TrainingConfig.from_dict(
        make_document(schema_version=TRAINING_SCHEMA_VERSION)).schema_version == \
        TRAINING_SCHEMA_VERSION


@pytest.mark.parametrize("missing", ["run_id", "method", "base_model_id", "seed",
                                     "dataset_reference", "created_at_utc"])
def test_a_missing_required_field_is_refused(missing):
    doc = make_document()
    doc.pop(missing)
    with pytest.raises(SchemaError, match="missing"):
        TrainingConfig.from_dict(doc)


def test_a_duplicate_json_key_is_refused(tmp_path):
    """``json.loads`` keeps the LAST duplicate, so a file can state a refusal and an
    approval and be read as the approval."""
    path = tmp_path / "c.json"
    path.write_text('{"trust_remote_code": false, "trust_remote_code": true}',
                    encoding="utf-8")
    with pytest.raises(SchemaError, match="duplicate key"):
        load_config_document(path)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_json_constant_is_refused(tmp_path, literal):
    path = tmp_path / "c.json"
    path.write_text('{"learning_rate": %s}' % literal, encoding="utf-8")
    with pytest.raises(SchemaError, match="non-finite"):
        load_config_document(path)


def test_an_oversized_config_file_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"pad": "x" * 300_000}), encoding="utf-8")
    with pytest.raises(SchemaError, match="reviewable document"):
        load_config_document(path)


def test_a_config_that_is_not_a_json_object_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(SchemaError, match="JSON object"):
        load_config_document(path)


def test_malformed_json_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SchemaError, match="not valid JSON"):
        load_config_document(path)


def test_a_missing_config_file_is_refused(tmp_path):
    with pytest.raises(SchemaError, match="not a regular file"):
        load_config_document(tmp_path / "absent.json")


def test_a_symlinked_config_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(make_document()), encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    with pytest.raises(SchemaError, match="symlink"):
        load_config_document(link)


def test_a_hard_linked_config_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(make_document()), encoding="utf-8")
    other = tmp_path / "other.json"
    try:
        import os
        os.link(real, other)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this host does not permit creating hard links")
    with pytest.raises(SchemaError, match="hard link"):
        load_config_document(other)


def test_a_valid_config_file_loads_end_to_end(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(make_document()), encoding="utf-8")
    assert load_training_config(path).run_id == "smoke-001"


# ══════════════════════════════════════════════════════════════════════════════
#  Identifier and path safety
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("run_id", [
    "", "  ", "../escape", "..", "a/b", "a\\b", "C:", "C:\\Windows",
    "\\\\server\\share", ".hidden", "con", "nul.json", "with space", "run.",
    "a" * 200,
])
def test_an_unsafe_run_id_is_refused(run_id):
    with pytest.raises(SchemaError):
        make_config(run_id=run_id)


@pytest.mark.parametrize("root_id", [
    "../out", "/abs/out", "C:\\out", "\\\\srv\\out", "con", "out.", "a b",
])
def test_an_unsafe_output_root_id_is_refused(root_id):
    with pytest.raises(SchemaError):
        make_config(output_root_id=root_id)


def test_an_absolute_output_path_cannot_be_expressed_at_all():
    """There is no output-path field; only an id under an application-owned root."""
    assert "output_path" not in TrainingConfig.FIELDS
    assert "output_dir" not in TrainingConfig.FIELDS


@pytest.mark.parametrize("model_id", [
    "", "Qwen", "Qwen/Qwen3/extra", "../../etc/passwd", "http://x/model",
    "C:\\models\\m", "\\\\srv\\m", "Qwen/con", "Qwen/name.", "Qwen/na me",
    "Qwen/" + "a" * 300,
])
def test_a_malformed_model_id_is_refused(model_id):
    with pytest.raises(SchemaError):
        make_config(base_model_id=model_id, tokenizer_id=model_id)


@pytest.mark.parametrize("revision", ["", "   ", "a/b", "a\\b", "..", "x" * 200])
def test_a_malformed_revision_is_refused(revision):
    with pytest.raises(SchemaError):
        make_config(base_model_revision=revision, tokenizer_revision=revision)


def test_a_missing_model_revision_is_refused_outright():
    with pytest.raises(SchemaError, match="required"):
        make_config(base_model_revision="", tokenizer_revision="")


# ══════════════════════════════════════════════════════════════════════════════
#  The security invariants that no config may override
# ══════════════════════════════════════════════════════════════════════════════
def test_trust_remote_code_true_is_refused():
    with pytest.raises(SchemaError, match="must be false"):
        make_config(trust_remote_code=True)


@pytest.mark.parametrize("value", ["true", "TRUE"])
def test_a_string_spelling_of_trust_remote_code_is_still_refused(value):
    with pytest.raises(SchemaError, match="must be false"):
        make_config(trust_remote_code=value)


@pytest.mark.parametrize("value", ["yes", "1", 1, 0, None])
def test_a_non_boolean_is_not_coerced(value):
    with pytest.raises(SchemaError):
        make_config(trust_remote_code=value)


def test_a_tokenizer_from_a_different_repository_is_refused():
    with pytest.raises(SchemaError, match="not the base model"):
        make_config(tokenizer_id="meta-llama/Llama-3-8B")


def test_a_tokenizer_at_a_different_revision_is_refused():
    with pytest.raises(SchemaError, match="must equal base_model_revision"):
        make_config(base_model_revision=_COMMIT, tokenizer_revision="b" * 40)


def test_there_is_no_field_for_an_executable_a_module_or_extra_arguments():
    for forbidden in ("executable", "python_executable", "entrypoint", "module",
                      "extra_args", "extra_arguments", "env", "environment",
                      "command", "shell", "hf_token", "token", "cache_dir",
                      "report_to", "hub_model_id"):
        assert forbidden not in TrainingConfig.FIELDS


def test_the_only_logging_targets_are_local():
    assert {t.value for t in LoggingTarget} == {"none", "local_jsonl"}


def test_there_is_no_auto_model_download_policy():
    assert {p.value for p in ModelDownloadPolicy} == {"deny",
                                                      "allow_with_explicit_flag"}
    assert make_config().model_download_policy is ModelDownloadPolicy.DENY


# ══════════════════════════════════════════════════════════════════════════════
#  Bounded hyperparameters
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,value", [
    ("seed", -1), ("seed", 1.5), ("seed", True), ("seed", None),
    ("epochs", 0), ("epochs", -1), ("epochs", 1000), ("epochs", True),
    ("max_steps", 0), ("max_steps", 10_000_000),
    ("batch_size", 0), ("batch_size", 4096),
    ("gradient_accumulation_steps", 0), ("gradient_accumulation_steps", 100_000),
    ("learning_rate", 0.0), ("learning_rate", -1e-4), ("learning_rate", 1.0),
    ("learning_rate", float("nan")), ("learning_rate", float("inf")),
    ("weight_decay", -0.1), ("weight_decay", 2.0),
    ("warmup_ratio", -0.1), ("warmup_ratio", 0.9),
    ("max_sequence_length", 8), ("max_sequence_length", 131072),
    ("dataloader_workers", -1), ("dataloader_workers", 512),
    ("max_checkpoints", 99), ("logging_interval_steps", 0),
    ("base_model_parameters_b", 0.0), ("base_model_parameters_b", 8.0),
])
def test_a_value_outside_its_ceiling_is_refused(field, value):
    with pytest.raises(SchemaError):
        make_config(**{field: value})


def test_an_eight_billion_parameter_model_exceeds_the_default_ceiling():
    with pytest.raises(SchemaError, match="exceeds the policy ceiling"):
        make_config(base_model_parameters_b=8.0)


@pytest.mark.parametrize("field", [
    "max_model_parameters_b", "max_epochs", "max_steps", "max_batch_size",
    "max_sequence_length", "max_dataloader_workers", "max_checkpoint_count",
    "min_free_disk_bytes", "max_wall_clock_seconds",
])
def test_a_ceiling_of_zero_is_not_unlimited(field):
    with pytest.raises(SchemaError, match="not 'unlimited'"):
        TrainingResourcePolicy(**{field: 0})


def test_the_resource_policy_version_is_part_of_its_hash():
    assert TrainingResourcePolicy().policy_hash() != \
        TrainingResourcePolicy(policy_version="other.1").policy_hash()


def test_a_looser_ceiling_changes_the_config_hash():
    loose = TrainingResourcePolicy(max_epochs=9)
    assert make_config(resource_policy=loose).config_hash() != \
        make_config().config_hash()


def test_a_raised_ceiling_admits_a_value_the_default_refuses():
    """The ceiling is the authority — not a hard-coded number scattered in validators."""
    with pytest.raises(SchemaError):
        make_config(epochs=11)
    assert make_config(epochs=11,
                       resource_policy=TrainingResourcePolicy(max_epochs=12)).epochs == 11


def test_an_unknown_resource_policy_field_is_refused():
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingResourcePolicy.from_dict({"max_epochs": 3, "max_gpus": 4})


# ══════════════════════════════════════════════════════════════════════════════
#  Closed vocabularies
# ══════════════════════════════════════════════════════════════════════════════
def test_the_training_methods_are_exactly_three_adapter_methods():
    assert {m.value for m in TrainingMethod} == {"sft_lora", "sft_qlora", "dpo_lora"}


def test_full_fine_tuning_is_not_a_supported_method():
    for absent in ("sft", "full", "full_finetune", "pretrain", "rlhf", "ppo", "grpo"):
        assert absent not in {m.value for m in TrainingMethod}


@pytest.mark.parametrize("field,value", [
    ("method", "sft"), ("method", "full_finetune"), ("method", ""), ("method", None),
    ("device_policy", "tpu"), ("precision_policy", "fp8"),
    ("checkpoint_strategy", "always"), ("logging_target", "wandb"),
    ("model_download_policy", "auto"), ("dependency_profile", "training-rocm"),
])
def test_an_unknown_closed_vocabulary_value_is_refused(field, value):
    with pytest.raises(SchemaError):
        make_config(**{field: value})


def test_the_legacy_method_mapping_is_one_way_and_total():
    assert {legacy_method(m) for m in TrainingMethod} == {"lora", "qlora", "dpo"}


def test_qlora_may_not_declare_a_contradicting_precision():
    with pytest.raises(SchemaError, match="contradicts"):
        make_config(method=TrainingMethod.SFT_QLORA,
                    precision_policy=PrecisionPolicy.BF16)


def test_int4_precision_requires_a_qlora_method():
    with pytest.raises(SchemaError, match="requires a QLoRA method"):
        make_config(precision_policy=PrecisionPolicy.INT4_QLORA)


def test_qlora_with_auto_precision_is_accepted_at_the_config_layer():
    """Whether it can RUN is a hardware question, answered by the planner, not here."""
    assert make_config(method=TrainingMethod.SFT_QLORA).method is \
        TrainingMethod.SFT_QLORA


def test_steps_checkpointing_requires_an_interval():
    with pytest.raises(SchemaError, match="checkpoint_interval_steps"):
        make_config(checkpoint_strategy=CheckpointStrategy.STEPS,
                    checkpoint_interval_steps=0)


# ══════════════════════════════════════════════════════════════════════════════
#  Splits
# ══════════════════════════════════════════════════════════════════════════════
def test_validating_on_the_training_split_is_refused():
    from training_gym.datasets.candidate import DatasetSplit

    with pytest.raises(SchemaError, match="must differ from training_split"):
        make_config(validation_split=DatasetSplit.TRAIN)


@pytest.mark.parametrize("split_name", ["hidden_evaluation", "security_regression",
                                        "adversarial", "quarantine"])
def test_a_held_out_split_may_not_be_the_training_split(split_name):
    from training_gym.datasets.candidate import DatasetSplit

    with pytest.raises(SchemaError, match="train-side"):
        make_config(training_split=DatasetSplit(split_name))


def test_a_train_side_split_may_not_be_the_hidden_evaluation():
    from training_gym.datasets.candidate import DatasetSplit

    with pytest.raises(SchemaError, match="is not hidden"):
        make_config(hidden_evaluation_reference=DatasetSplit.TRAIN)


def test_the_hidden_evaluation_and_the_security_regression_are_distinct():
    from training_gym.datasets.candidate import DatasetSplit

    with pytest.raises(SchemaError, match="must differ"):
        make_config(security_regression_reference=DatasetSplit.HIDDEN_EVALUATION)


# ══════════════════════════════════════════════════════════════════════════════
#  LoRA
# ══════════════════════════════════════════════════════════════════════════════
def test_the_smoke_lora_defaults_are_the_documented_starting_point():
    lora = LoRAConfig()
    assert (lora.rank, lora.alpha, lora.dropout) == (8, 16, 0.05)
    assert lora.bias.value == "none"
    assert lora.target_policy is LoRATargetPolicy.ALL_LINEAR
    assert lora.scaling == 2.0


@pytest.mark.parametrize("field,value", [
    ("rank", 0), ("rank", -1), ("rank", 1024), ("rank", 8.5), ("rank", True),
    ("alpha", 0), ("alpha", 4096),
    ("dropout", -0.1), ("dropout", 1.0), ("dropout", 1.5),
    ("bias", "sometimes"), ("target_policy", "regex:.*"), ("task_type", "SEQ_CLS"),
    ("modules_to_save", ("a/b",)), ("modules_to_save", tuple("m" for _ in range(20))),
])
def test_an_out_of_bounds_lora_value_is_refused(field, value):
    with pytest.raises(SchemaError):
        LoRAConfig(**{field: value})


def test_the_target_module_set_is_chosen_by_policy_not_by_the_file():
    doc = LoRAConfig().to_dict()
    doc["target_modules"] = ["lm_head"]
    with pytest.raises(SchemaError, match="chosen by the policy"):
        LoRAConfig.from_dict(doc)


def test_a_lora_record_may_not_disagree_with_its_own_derived_scaling():
    doc = LoRAConfig().to_dict()
    doc["scaling"] = 99.0
    with pytest.raises(SchemaError, match="scaling"):
        LoRAConfig.from_dict(doc)


def test_every_target_policy_resolves_to_a_non_empty_module_set():
    for policy in LoRATargetPolicy:
        assert LoRAConfig(target_policy=policy).target_modules


def test_a_lora_record_round_trips():
    lora = LoRAConfig(rank=16, alpha=32, target_policy=LoRATargetPolicy.ATTENTION_ONLY)
    assert LoRAConfig.from_dict(lora.to_dict()).to_dict() == lora.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
#  Model identity
# ══════════════════════════════════════════════════════════════════════════════
def test_a_placeholder_revision_blocks_executable_readiness():
    identity = identity_from_config(make_config())
    assert identity.revision_kind is RevisionKind.PLACEHOLDER
    assert identity.is_executable_reference is False
    assert "placeholder" in identity.blockers[0]


@pytest.mark.parametrize("revision", ["main", "master", "latest", "v1.0", "a" * 39,
                                      "A" * 40])
def test_a_mutable_revision_blocks_executable_readiness(revision):
    identity = identity_from_config(make_config(base_model_revision=revision,
                                                tokenizer_revision=revision))
    assert identity.revision_kind is RevisionKind.MUTABLE_REFERENCE
    assert identity.is_executable_reference is False
    assert "not reproducible" in identity.blockers[0]


def test_a_full_commit_sha_is_an_executable_reference():
    identity = identity_from_config(make_config(base_model_revision=_COMMIT,
                                                tokenizer_revision=_COMMIT))
    assert identity.is_executable_reference is True
    assert identity.blockers == ()


def test_is_immutable_revision_accepts_only_a_full_lowercase_sha():
    assert is_immutable_revision(_COMMIT) is True
    assert is_immutable_revision(_COMMIT[:12]) is False
    assert is_immutable_revision("A" * 40) is False


def test_classify_revision_covers_every_kind():
    assert classify_revision(REQUIRED_REVISION_PLACEHOLDER) is RevisionKind.PLACEHOLDER
    assert classify_revision("main") is RevisionKind.MUTABLE_REFERENCE
    assert classify_revision(_COMMIT) is RevisionKind.IMMUTABLE_COMMIT


def test_the_identity_hash_changes_when_the_revision_changes():
    a = identity_from_config(make_config(base_model_revision=_COMMIT,
                                         tokenizer_revision=_COMMIT))
    b = identity_from_config(make_config(base_model_revision="b" * 40,
                                         tokenizer_revision="b" * 40))
    assert a.identity_hash() != b.identity_hash()
    assert a.tokenizer_identity_hash() != b.tokenizer_identity_hash()


def test_a_substituted_tokenizer_cannot_be_expressed_in_an_identity():
    with pytest.raises(SchemaError, match="not the model"):
        ModelIdentity(provider="huggingface", model_id="Qwen/Qwen3-0.6B",
                      revision=_COMMIT, parameters_b=0.6,
                      tokenizer_id="meta-llama/Llama-3-8B", tokenizer_revision=_COMMIT)


def test_an_identity_requiring_remote_code_is_refused():
    with pytest.raises(SchemaError, match="requires executing"):
        ModelIdentity(provider="huggingface", model_id="Qwen/Qwen3-0.6B",
                      revision=_COMMIT, parameters_b=0.6, requires_remote_code=True)


# ── cache probing: passive, path-free, and honest about not knowing ───────────
def test_with_no_authorised_cache_root_the_status_is_unknown_not_absent():
    assert probe_cache("Qwen/Qwen3-0.6B", cache_root=None) == (CacheStatus.UNKNOWN, "")


def test_an_empty_cache_root_reports_absent(tmp_path):
    status, evidence = probe_cache("Qwen/Qwen3-0.6B", cache_root=tmp_path)
    assert status is CacheStatus.ABSENT
    assert evidence == ""


def test_a_populated_cache_reports_present_without_publishing_the_path(tmp_path):
    entry = tmp_path / cache_directory_name("Qwen/Qwen3-0.6B")
    entry.mkdir()
    (entry / "refs").write_text("x", encoding="utf-8")
    status, evidence = probe_cache("Qwen/Qwen3-0.6B", cache_root=tmp_path)
    assert status is CacheStatus.PRESENT
    assert len(evidence) == 64
    assert str(tmp_path) not in evidence


def test_a_cache_status_is_never_fabricated_from_an_unreadable_root(tmp_path):
    missing = tmp_path / "not-there"
    assert probe_cache("Qwen/Qwen3-0.6B", cache_root=missing)[0] is CacheStatus.UNKNOWN


def test_the_identity_record_carries_no_absolute_path(tmp_path):
    entry = tmp_path / cache_directory_name("Qwen/Qwen3-0.6B")
    entry.mkdir()
    (entry / "refs").write_text("x", encoding="utf-8")
    identity = identity_from_config(make_config(), cache_root=tmp_path)
    blob = json.dumps(identity.to_dict())
    assert str(tmp_path) not in blob
    assert "Users" not in blob


def test_probing_a_cache_never_creates_it(tmp_path):
    root = tmp_path / "cache"
    probe_cache("Qwen/Qwen3-0.6B", cache_root=root)
    assert not root.exists()


# ══════════════════════════════════════════════════════════════════════════════
#  Dataset reference (the declaration; verification is tested separately)
# ══════════════════════════════════════════════════════════════════════════════
def test_a_placeholder_reference_parses_but_is_not_fully_specified():
    reference = make_reference()
    assert reference.is_fully_specified is False
    assert "dataset_manifest_hash" in reference.placeholders
    assert "record_count" in reference.placeholders


def test_a_placeholder_is_recognised_and_a_digest_is_not():
    assert is_placeholder("REQUIRED_DATASET_MANIFEST_HASH") is True
    assert is_placeholder("a" * 64) is False


@pytest.mark.parametrize("value", ["", "abc", "A" * 64, "a" * 63, "a" * 65,
                                   "required_lowercase", 12345, None])
def test_a_digest_that_is_neither_a_hash_nor_a_placeholder_is_refused(value):
    with pytest.raises(SchemaError):
        make_reference().__class__(
            dataset_id="gym", dataset_version="v1",
            export_format=ExportFormat.SFT_JSONL, dataset_manifest_hash=value,
            export_manifest_hash="a" * 64, source_dataset_content_hash="a" * 64,
            train_split_manifest_hash="a" * 64, train_shard_hash="a" * 64,
            validation_split_manifest_hash="a" * 64, validation_shard_hash="a" * 64,
            hidden_evaluation_manifest_hash="a" * 64,
            security_regression_manifest_hash="a" * 64)


def test_an_unknown_dataset_reference_field_is_refused():
    doc = make_reference().to_dict()
    doc["allow_hidden_eval"] = True
    with pytest.raises(SchemaError, match="unknown field"):
        TrainingDatasetReference.from_dict(doc)


def test_a_reference_round_trips():
    reference = make_reference()
    assert TrainingDatasetReference.from_dict(reference.to_dict()).reference_hash() == \
        reference.reference_hash()


def test_a_bridge_reference_must_name_its_bridge_version():
    with pytest.raises(SchemaError, match="bridge_version"):
        TrainingDatasetReference.from_dict(
            {**make_reference().to_dict(), "export_format": "m16_bridge"})


def test_only_a_bridge_reference_may_claim_a_bridge_version():
    with pytest.raises(SchemaError, match="bridge_version"):
        TrainingDatasetReference.from_dict(
            {**make_reference().to_dict(), "bridge_version": "m62.bridge.1"})


# ══════════════════════════════════════════════════════════════════════════════
#  The run state machine
# ══════════════════════════════════════════════════════════════════════════════
def test_s3a_may_not_reach_any_state_that_asserts_work_happened():
    for forbidden in (TrainingRunState.RUNNING, TrainingRunState.COMPLETED,
                      TrainingRunState.EVALUATED, TrainingRunState.CANDIDATE,
                      TrainingRunState.INTERRUPTED):
        assert forbidden not in S3A_REACHABLE_STATES


def test_the_planning_states_are_exactly_the_seven_s3a_may_occupy():
    assert {s.value for s in S3A_REACHABLE_STATES} == {
        "created", "config_validated", "dataset_verified", "dependencies_verified",
        "hardware_verified", "planned", "awaiting_confirmation"}


def test_the_happy_path_through_planning_is_permitted():
    order = [TrainingRunState.CREATED, TrainingRunState.CONFIG_VALIDATED,
             TrainingRunState.DATASET_VERIFIED,
             TrainingRunState.DEPENDENCIES_VERIFIED,
             TrainingRunState.HARDWARE_VERIFIED, TrainingRunState.PLANNED,
             TrainingRunState.AWAITING_CONFIRMATION]
    for current, target in zip(order, order[1:]):
        check_training_transition(current, target)


@pytest.mark.parametrize("current,target", [
    (TrainingRunState.CREATED, TrainingRunState.RUNNING),
    (TrainingRunState.CREATED, TrainingRunState.COMPLETED),
    (TrainingRunState.PLANNED, TrainingRunState.RUNNING),
    (TrainingRunState.PLANNED, TrainingRunState.COMPLETED),
    (TrainingRunState.CONFIG_VALIDATED, TrainingRunState.CANDIDATE),
    (TrainingRunState.COMPLETED, TrainingRunState.RUNNING),
    (TrainingRunState.QUARANTINED, TrainingRunState.PLANNED),
    (TrainingRunState.FAILED, TrainingRunState.COMPLETED),
])
def test_an_illegal_transition_is_refused(current, target):
    with pytest.raises(SchemaError, match="not a permitted transition"):
        check_training_transition(current, target)


def test_only_an_explicitly_confirmed_run_may_start():
    """There is exactly one edge into ``RUNNING``, and one corridor that reaches it.

    S3B inserted ``PREFLIGHT_VERIFIED`` and ``STARTING`` between the operator's
    confirmation and the training loop, so the single entrance is now ``STARTING``
    rather than ``AWAITING_CONFIRMATION``. That is a strictly stronger claim, not a
    weaker one: the corridor is still one lane wide, and it is now two gates longer —
    the plan is re-derived from the current state of the world in ``PREFLIGHT_VERIFIED``
    and spent in ``STARTING``, and neither is reachable except from the confirmation.
    """
    transitions = (__import__("training_gym.training.config", fromlist=["x"])
                   .ALLOWED_TRAINING_TRANSITIONS)
    entrances = [state for state, targets in transitions.items()
                 if TrainingRunState.RUNNING in targets]
    assert entrances == [TrainingRunState.STARTING]
    assert [s for s, t in transitions.items() if TrainingRunState.STARTING in t] == [
        TrainingRunState.PREFLIGHT_VERIFIED]
    assert [s for s, t in transitions.items()
            if TrainingRunState.PREFLIGHT_VERIFIED in t] == [
        TrainingRunState.AWAITING_CONFIRMATION]


def test_a_backend_reporting_success_cannot_by_itself_complete_a_run():
    """``RUNNING`` has no edge to ``COMPLETED``; the adapter on disk is the evidence."""
    transitions = (__import__("training_gym.training.config", fromlist=["x"])
                   .ALLOWED_TRAINING_TRANSITIONS)
    assert TrainingRunState.COMPLETED not in transitions[TrainingRunState.RUNNING]
    assert [s for s, t in transitions.items() if TrainingRunState.COMPLETED in t] == [
        TrainingRunState.ARTIFACT_VALIDATION]
    with pytest.raises(TrainingTransitionError):
        check_training_transition(TrainingRunState.RUNNING, TrainingRunState.COMPLETED)
    for terminal in (TrainingRunState.INTERRUPTED, TrainingRunState.FAILED,
                     TrainingRunState.QUARANTINED):
        with pytest.raises(TrainingTransitionError):
            check_training_transition(terminal, TrainingRunState.COMPLETED)


# ══════════════════════════════════════════════════════════════════════════════
#  No training machinery is loaded by any of this
# ══════════════════════════════════════════════════════════════════════════════
_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "huggingface_hub", "tokenizers", "sentence_transformers",
           "safetensors")


def test_building_a_config_pulls_in_no_training_or_model_machinery():
    import sys

    make_config().config_hash()
    identity_from_config(make_config())
    assert [m for m in _BANNED if m in sys.modules] == []


def test_the_config_record_carries_no_absolute_path_or_username(tmp_path):
    blob = json.dumps(make_config().to_dict())
    assert str(tmp_path) not in blob
    assert "Users" not in blob
    assert "\\\\" not in blob


def test_the_device_policy_vocabulary_is_closed():
    assert {d.value for d in DevicePolicy} == {"auto_safe", "cpu", "cuda", "mps",
                                               "directml"}
