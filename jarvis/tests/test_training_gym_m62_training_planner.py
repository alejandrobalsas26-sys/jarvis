"""tests/test_training_gym_m62_training_planner.py — V69 M62 S3A: the dry run is dry.

The planner's whole value is that it is safe to run. So the tests that matter most are
not about the plan's contents — they are about what running the planner *did*: no file,
no directory, no socket, no subprocess, no pip, no import of a training framework, and no
adapter that could later be mistaken for the product of a run.

Everything else follows from the same principle. Package presence is not capability, so
QLoRA on a CPU is refused rather than warned about. An explicit CUDA request is refused
rather than downgraded, because a silent fall back to the CPU turns a thirty-minute job
into a twelve-hour one without saying so. And `--execute` does not fail — it refuses,
because there is no branch in this stage that leads to a training loop.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from training_gym.schemas import SchemaError
from training_gym.training.config import (
    REQUIRED_REVISION_PLACEHOLDER,
    DevicePolicy,
    ModelDownloadPolicy,
    PrecisionPolicy,
    TrainingConfig,
    TrainingMethod,
)
from training_gym.training.dataset_reference import TrainingDatasetReference
from training_gym.training.environment import (
    AcceleratorProbe,
    Availability,
    HardwareCapabilityReport,
    SizeCategory,
    categorize_gb,
    no_accelerator_probe,
    select_device,
    select_precision,
)
from training_gym.training.feasibility import (
    RuntimeCategory,
    Verdict,
    classify_runtime,
    estimate_disk,
    estimate_memory,
)
from training_gym.training.model_identity import cache_directory_name
from training_gym.training.plan import (
    CONFIRMATION_PREFIX,
    ConfirmationRejected,
    ExecutionNotImplemented,
    check_output_root,
    check_training_confirmation,
    output_root_id,
    refuse_execution,
    training_run_directory,
)
from training_gym.training.planner import plan_training

_COMMIT = "a" * 40
_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "huggingface_hub", "tokenizers", "sentence_transformers")


# ── builders, defined locally so this module resolves from either layout ──────
def make_config(**overrides) -> TrainingConfig:
    base = dict(
        run_id="smoke-001", experiment_name="qwen3 0.6b lora smoke",
        method=TrainingMethod.SFT_LORA, base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision=REQUIRED_REVISION_PLACEHOLDER,
        base_model_parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
        tokenizer_revision=REQUIRED_REVISION_PLACEHOLDER,
        dataset_reference=TrainingDatasetReference.placeholder(
            dataset_id="gym", dataset_version="v1"),
        output_root_id="local-training", created_at_utc="2026-08-02T00:00:00Z")
    base.update(overrides)
    return TrainingConfig(**base)


def cpu_report(**overrides) -> HardwareCapabilityReport:
    base = dict(os_family="Linux", python_version="3.12.0",
                process_architecture="64bit", cpu_architecture="x86_64",
                logical_cpus=8, total_ram_gb=64,
                total_ram_category=SizeCategory.AT_LEAST_64GB,
                torch_package=Availability.AVAILABLE,
                bitsandbytes_package=Availability.UNAVAILABLE,
                output_disk_free_gb=500,
                output_disk_category=SizeCategory.AT_LEAST_64GB)
    base.update(overrides)
    return HardwareCapabilityReport(**base)


def cuda_report(*, bf16=Availability.AVAILABLE,
                bitsandbytes=Availability.AVAILABLE) -> HardwareCapabilityReport:
    return cpu_report(
        bitsandbytes_package=bitsandbytes,
        accelerator=AcceleratorProbe(
            cuda_runtime=Availability.AVAILABLE, cuda_device_count=1,
            cuda_compute_capability="8.6", cuda_bf16_supported=bf16,
            accelerator_memory_gb=24.0),
        accelerator_probe_ran=True)


def plan(tmp_path: Path, config: TrainingConfig | None = None, **kwargs):
    return plan_training(config or make_config(), dataset_root=tmp_path,
                         output_root=tmp_path, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  The dry run is dry
# ══════════════════════════════════════════════════════════════════════════════
def test_planning_creates_no_file_and_no_directory(tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    plan(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "adapters").exists()
    assert not (tmp_path / "configs").exists()


def test_planning_creates_no_adapter_and_no_checkpoint(tmp_path):
    plan(tmp_path)
    assert list(tmp_path.rglob("*.safetensors")) == []
    assert list(tmp_path.rglob("*checkpoint*")) == []
    assert list(tmp_path.rglob("adapter_*")) == []


def test_planning_opens_no_socket_and_resolves_no_name(tmp_path, monkeypatch):
    def _forbidden(*a, **k):  # pragma: no cover — the assertion is that it is not hit
        raise AssertionError("the planner reached the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden)
    assert plan(tmp_path).plan.plan_hash()


def test_planning_spawns_no_process(tmp_path, monkeypatch):
    def _forbidden(*a, **k):  # pragma: no cover
        raise AssertionError("the planner spawned a process")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "system", _forbidden)
    assert plan(tmp_path).plan.plan_hash()


def test_planning_imports_no_training_framework(tmp_path):
    """Measured AFTER a full plan, so a lazy in-function import is caught."""
    before = {m for m in _BANNED if m in sys.modules}
    plan(tmp_path)
    assert {m for m in _BANNED if m in sys.modules} == before


def test_the_planner_never_imports_torch_even_to_ask_about_cuda(tmp_path, monkeypatch):
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _guarded(name, *args, **kwargs):
        if name.split(".")[0] in _BANNED:
            raise AssertionError(f"the planner imported {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _guarded)
    assert plan(tmp_path).hardware.cuda_available is Availability.UNKNOWN


def test_the_plan_declares_that_it_did_nothing(tmp_path):
    effects = plan(tmp_path).plan.expected_effects()
    assert effects["trains_a_model"] is False
    assert effects["downloads_anything"] is False
    assert effects["installs_dependencies"] is False
    assert effects["contacts_the_network"] is False
    assert effects["creates_an_adapter"] is False
    assert effects["creates_files"] == []


def test_the_execution_stage_does_not_still_say_s3b_is_unimplemented(tmp_path):
    """S3B executes -- a real adapter has been trained from a plan of this shape.

    The key is operator-facing prose, deliberately outside ``to_dict`` and therefore
    outside the plan hash, so correcting it cannot invalidate an already-recorded run.
    """
    stage = plan(tmp_path).plan.expected_effects()["execution_stage"]
    assert "not_implemented" not in stage
    assert stage.startswith("s3b_implemented")


def test_a_plan_cannot_claim_it_trained(tmp_path):
    result = plan(tmp_path)
    from dataclasses import replace

    with pytest.raises(SchemaError, match="planning-only stage"):
        replace(result.plan, performs_training=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Nothing private leaves
# ══════════════════════════════════════════════════════════════════════════════
def test_the_dry_run_document_carries_no_absolute_path(tmp_path):
    blob = json.dumps(plan(tmp_path).to_dict())
    assert str(tmp_path) not in blob
    assert "Users" not in blob
    assert os.path.expanduser("~") not in blob


def test_the_plan_record_passes_the_private_content_scanner(tmp_path):
    assert plan(tmp_path).plan.to_record()["plan_hash"]


def test_the_output_root_appears_only_as_a_digest(tmp_path):
    result = plan(tmp_path)
    assert len(result.plan.expected_output_root_id) == 64
    assert str(tmp_path) not in json.dumps(result.plan.to_record())


def test_two_output_roots_produce_two_plans(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert output_root_id(tmp_path) != output_root_id(other)
    assert plan(tmp_path).plan.plan_hash() != plan_training(
        make_config(), dataset_root=tmp_path, output_root=other).plan.plan_hash()


def test_the_expected_files_are_relative(tmp_path):
    for name in plan(tmp_path).plan.expected_files:
        assert not name.startswith(("/", "\\"))
        assert ":" not in name


# ══════════════════════════════════════════════════════════════════════════════
#  Output root safety
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("run_id", ["../escape", "a/b", "C:\\x", "con", "run.", ""])
def test_an_unsafe_run_id_is_refused_before_any_path_join(tmp_path, run_id):
    with pytest.raises(SchemaError):
        training_run_directory(tmp_path, run_id)


def test_an_existing_run_directory_is_a_refusal(tmp_path):
    (tmp_path / "runs" / "smoke-001").mkdir(parents=True)
    problems = check_output_root(tmp_path, "smoke-001")
    assert any("already exists" in p for p in problems)


def test_a_symlinked_output_root_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")
    assert any("symlink" in p for p in check_output_root(link, "smoke-001"))


def test_checking_the_output_root_creates_nothing(tmp_path):
    check_output_root(tmp_path / "does-not-exist", "smoke-001")
    assert not (tmp_path / "does-not-exist").exists()


# ══════════════════════════════════════════════════════════════════════════════
#  Hardware: package presence is not capability
# ══════════════════════════════════════════════════════════════════════════════
def test_a_detected_report_is_produced_without_an_accelerator_probe(tmp_path):
    report = HardwareCapabilityReport.detect(output_root=tmp_path)
    assert report.accelerator_probe_ran is False
    assert report.cuda_available is Availability.UNKNOWN
    assert report.report_hash()


def test_an_installed_torch_is_never_read_as_a_working_gpu():
    report = cpu_report(torch_package=Availability.AVAILABLE)
    assert report.cuda_available is Availability.UNKNOWN
    assert "fp16" not in report.supported_precisions


def test_a_mocked_cuda_probe_is_represented_faithfully():
    report = cuda_report()
    assert report.cuda_available is Availability.AVAILABLE
    assert report.qlora_supported is Availability.AVAILABLE
    assert set(report.supported_precisions) == {"fp32", "fp16", "bf16", "int4_qlora"}
    assert report.usable_memory_gb == 24.0


def test_a_probe_that_crashes_reports_unknown_rather_than_unavailable(tmp_path):
    def _boom():
        raise RuntimeError("driver mismatch")

    report = HardwareCapabilityReport.detect(output_root=tmp_path,
                                             accelerator_probe=_boom)
    assert report.accelerator_probe_ran is False
    assert report.cuda_available is Availability.UNKNOWN


def test_the_hardware_report_records_no_host_identity(tmp_path):
    blob = json.dumps(HardwareCapabilityReport.detect(output_root=tmp_path).to_dict())
    import platform

    for secret in (platform.node(), os.path.expanduser("~"), str(tmp_path)):
        if secret and secret not in ("", "."):
            assert secret not in blob
    for key in ("hostname", "username", "user", "mac", "serial", "uuid", "environ"):
        assert key not in blob.lower()


def test_free_space_is_probed_only_at_the_supplied_root(tmp_path):
    assert HardwareCapabilityReport.detect(output_root=None).output_disk_free_gb == 0
    assert HardwareCapabilityReport.detect(output_root=tmp_path / "absent"
                                           ).output_disk_free_gb == 0


@pytest.mark.parametrize("gb,expected", [
    (0, SizeCategory.UNKNOWN), (4, SizeCategory.UNDER_8GB),
    (12, SizeCategory.FROM_8_TO_16GB), (24, SizeCategory.FROM_16_TO_32GB),
    (48, SizeCategory.FROM_32_TO_64GB), (128, SizeCategory.AT_LEAST_64GB),
])
def test_sizes_are_reported_as_categories(gb, expected):
    assert categorize_gb(gb) is expected


def test_the_default_probe_measures_nothing_and_says_so():
    assert no_accelerator_probe().cuda_runtime is Availability.UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
#  Device selection
# ══════════════════════════════════════════════════════════════════════════════
def test_auto_safe_selects_the_cpu_when_nothing_else_was_measured():
    selection = select_device(DevicePolicy.AUTO_SAFE, cpu_report())
    assert selection.selected == "cpu"
    assert selection.ok
    assert "measured" in selection.reason


def test_auto_safe_selects_cuda_when_a_probe_measured_it():
    assert select_device(DevicePolicy.AUTO_SAFE, cuda_report()).selected == "cuda"


def test_an_explicit_cuda_request_without_cuda_is_refused():
    selection = select_device(DevicePolicy.CUDA, cpu_report())
    assert selection.selected == ""
    assert not selection.ok
    assert "refusal rather than a substitution" in selection.blockers[0]


def test_there_is_no_silent_cpu_fallback():
    """The blocker names the consequence, not just the condition."""
    blockers = select_device(DevicePolicy.CUDA, cpu_report()).blockers
    assert "falling back to the CPU" in blockers[0]


def test_an_explicit_mps_request_without_metal_is_refused():
    assert not select_device(DevicePolicy.MPS, cpu_report()).ok


def test_directml_is_refused_for_training_and_the_reason_is_honest():
    selection = select_device(DevicePolicy.DIRECTML, cpu_report(
        directml_package=Availability.AVAILABLE))
    assert not selection.ok
    assert "INFERENCE" in selection.blockers[0]


def test_an_explicit_cpu_request_is_always_honoured():
    assert select_device(DevicePolicy.CPU, cpu_report()).selected == "cpu"


# ══════════════════════════════════════════════════════════════════════════════
#  Precision selection
# ══════════════════════════════════════════════════════════════════════════════
def test_auto_safe_on_the_cpu_is_fp32():
    selection = select_precision(PrecisionPolicy.AUTO_SAFE, device="cpu",
                                 report=cpu_report(), method=TrainingMethod.SFT_LORA)
    assert selection.selected == "fp32"


def test_auto_safe_on_cuda_with_measured_bf16_is_bf16():
    assert select_precision(PrecisionPolicy.AUTO_SAFE, device="cuda",
                            report=cuda_report(),
                            method=TrainingMethod.SFT_LORA).selected == "bf16"


def test_auto_safe_on_cuda_without_bf16_evidence_is_fp16():
    assert select_precision(PrecisionPolicy.AUTO_SAFE, device="cuda",
                            report=cuda_report(bf16=Availability.UNKNOWN),
                            method=TrainingMethod.SFT_LORA).selected == "fp16"


def test_fp16_on_the_cpu_is_refused_not_promoted():
    selection = select_precision(PrecisionPolicy.FP16, device="cpu",
                                 report=cpu_report(), method=TrainingMethod.SFT_LORA)
    assert not selection.ok
    assert "silent promotion to fp32" in selection.blockers[0]


def test_bf16_without_measured_support_is_refused():
    selection = select_precision(PrecisionPolicy.BF16, device="cuda",
                                 report=cuda_report(bf16=Availability.UNKNOWN),
                                 method=TrainingMethod.SFT_LORA)
    assert not selection.ok
    assert "assuming it because the hardware is recent" in selection.blockers[0]


def test_qlora_without_cuda_is_refused():
    selection = select_precision(PrecisionPolicy.AUTO_SAFE, device="cpu",
                                 report=cpu_report(), method=TrainingMethod.SFT_QLORA)
    assert not selection.ok
    assert "measured CUDA runtime AND bitsandbytes" in selection.blockers[0]


def test_qlora_with_cuda_but_without_bitsandbytes_is_refused():
    report = cuda_report(bitsandbytes=Availability.UNAVAILABLE)
    assert report.qlora_supported is Availability.UNAVAILABLE
    selection = select_precision(PrecisionPolicy.INT4_QLORA, device="cuda",
                                 report=report, method=TrainingMethod.SFT_QLORA)
    assert not selection.ok


def test_qlora_with_both_is_supported():
    assert select_precision(PrecisionPolicy.INT4_QLORA, device="cuda",
                            report=cuda_report(),
                            method=TrainingMethod.SFT_QLORA).selected == "int4_qlora"


def test_quantized_inference_support_is_not_evidence_of_quantized_training():
    selection = select_precision(PrecisionPolicy.INT8_TRAINING, device="cuda",
                                 report=cuda_report(), method=TrainingMethod.SFT_LORA)
    assert not selection.ok
    assert "int8 inference support is a different claim" in selection.blockers[0]


# ══════════════════════════════════════════════════════════════════════════════
#  Estimates
# ══════════════════════════════════════════════════════════════════════════════
def test_the_memory_estimate_is_deterministic():
    a = estimate_memory(make_config(), precision="fp32")
    b = estimate_memory(make_config(), precision="fp32")
    assert a.estimate_hash() == b.estimate_hash()


def test_the_memory_estimate_names_its_formula_and_its_assumptions():
    estimate = estimate_memory(make_config(), precision="fp32")
    assert estimate.formula_version
    assert estimate.assumptions
    assert estimate.to_dict()["is_an_estimate_not_a_measurement"] is True


def test_a_lower_precision_lowers_the_weight_term():
    assert estimate_memory(make_config(), precision="bf16").base_weights_gb < \
        estimate_memory(make_config(), precision="fp32").base_weights_gb


def test_the_estimate_includes_a_safety_margin():
    estimate = estimate_memory(make_config(), precision="fp32")
    assert estimate.safety_margin_gb > 0
    assert estimate.estimated_peak_gb > estimate.base_weights_gb


def test_dpo_carries_a_reference_model_in_the_estimate():
    dpo = make_config(method=TrainingMethod.DPO_LORA,
                      dataset_reference=TrainingDatasetReference.placeholder(
                          dataset_id="gym", dataset_version="v1"))
    assert estimate_memory(dpo, precision="fp32").base_weights_gb > \
        estimate_memory(make_config(), precision="fp32").base_weights_gb


def test_the_disk_estimate_is_deterministic_and_creates_nothing(tmp_path):
    a = estimate_disk(make_config(), weights_cached=False)
    assert a.estimate_hash() == estimate_disk(make_config(),
                                              weights_cached=False).estimate_hash()
    assert sorted(tmp_path.iterdir()) == []


def test_a_cached_model_removes_the_download_term_from_the_disk_estimate():
    assert estimate_disk(make_config(), weights_cached=True).model_cache_gb == 0.0
    assert estimate_disk(make_config(), weights_cached=False).model_cache_gb > 0


@pytest.mark.parametrize("params,device,expected", [
    (0.6, "cpu", RuntimeCategory.SLOW_LOCAL_SMOKE),
    (1.2, "cpu", RuntimeCategory.IMPRACTICAL_LOCAL),
    (8.0, "cpu", RuntimeCategory.REQUIRES_ACCELERATOR),
    (0.6, "cuda", RuntimeCategory.SHORT_SMOKE),
    (0.6, "", RuntimeCategory.UNKNOWN),
])
def test_runtime_is_a_category_never_a_duration(params, device, expected):
    from training_gym.training.config import TrainingResourcePolicy

    config = make_config(base_model_parameters_b=params,
                         resource_policy=TrainingResourcePolicy(
                             max_model_parameters_b=16.0))
    assert classify_runtime(config, device=device, record_count=100) is expected


def test_a_cpu_smoke_is_never_described_as_fast():
    from training_gym.training.config import TrainingResourcePolicy

    config = make_config(base_model_parameters_b=0.6)
    assert classify_runtime(config, device="cpu", record_count=10) is not \
        RuntimeCategory.SHORT_SMOKE
    big = make_config(base_model_parameters_b=8.0,
                      resource_policy=TrainingResourcePolicy(
                          max_model_parameters_b=16.0))
    assert classify_runtime(big, device="cpu", record_count=10) is \
        RuntimeCategory.REQUIRES_ACCELERATOR


# ══════════════════════════════════════════════════════════════════════════════
#  The plan and its digest
# ══════════════════════════════════════════════════════════════════════════════
def test_the_plan_is_deterministic(tmp_path):
    assert plan(tmp_path).plan.plan_hash() == plan(tmp_path).plan.plan_hash()


def test_the_plan_survives_serialization(tmp_path):
    record = plan(tmp_path).plan.to_record()
    assert json.loads(json.dumps(record))["plan_hash"] == record["plan_hash"]


def test_the_plan_hash_excludes_itself(tmp_path):
    result = plan(tmp_path)
    assert "plan_hash" not in result.plan.to_dict()
    assert "expected_effects" not in result.plan.to_dict()


@pytest.mark.parametrize("field,value", [
    ("seed", 7), ("epochs", 2), ("max_sequence_length", 256),
    ("base_model_revision", _COMMIT), ("precision_policy", PrecisionPolicy.FP32),
    ("device_policy", DevicePolicy.CPU),
])
def test_the_plan_hash_changes_when_a_planning_input_changes(tmp_path, field, value):
    overrides = {field: value}
    if field == "base_model_revision":
        overrides["tokenizer_revision"] = value
    assert plan(tmp_path, make_config(**overrides)).plan.plan_hash() != \
        plan(tmp_path).plan.plan_hash()


def test_the_plan_hash_changes_when_the_dataset_reference_changes(tmp_path):
    other = TrainingDatasetReference.placeholder(dataset_id="gym", dataset_version="v2")
    assert plan(tmp_path, make_config(dataset_reference=other)).plan.plan_hash() != \
        plan(tmp_path).plan.plan_hash()


def test_the_plan_hash_changes_when_the_hardware_evidence_changes(tmp_path):
    a = plan(tmp_path)
    b = plan(tmp_path, accelerator_probe=lambda: AcceleratorProbe(
        cuda_runtime=Availability.AVAILABLE, cuda_device_count=1,
        accelerator_memory_gb=24.0))
    assert a.plan.plan_hash() != b.plan.plan_hash()
    assert a.plan.hardware_report_hash != b.plan.hardware_report_hash


def test_the_plan_hash_changes_when_the_dependency_evidence_changes(tmp_path,
                                                                    monkeypatch):
    before = plan(tmp_path).plan.plan_hash()
    from training_gym.training import dependencies as deps

    monkeypatch.setitem(deps.MINIMUM_VERSIONS, "torch", "999.0.0")
    assert plan(tmp_path).plan.plan_hash() != before


def test_the_plan_names_the_states_it_reached_and_no_others(tmp_path):
    states = plan(tmp_path).plan.expected_state_transitions
    for forbidden in ("running", "completed", "evaluated", "candidate"):
        assert forbidden not in states


def test_a_placeholder_config_is_not_executable(tmp_path):
    result = plan(tmp_path)
    assert result.plan.is_executable is False
    assert result.feasibility.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert any("placeholder" in b for b in result.plan.blockers)


def test_the_plan_reports_the_future_download_requirement_without_downloading(tmp_path):
    result = plan(tmp_path)
    assert result.plan.model_download_required is True
    assert result.plan.downloads_model is False
    assert "would refuse rather than fetch" in result.download_note


def test_the_allow_download_flag_changes_only_the_report(tmp_path):
    config = make_config(model_download_policy=ModelDownloadPolicy
                         .ALLOW_WITH_EXPLICIT_FLAG)
    result = plan(tmp_path, config, allow_model_download_flag=True)
    assert result.plan.downloads_model is False
    assert "no download happened here" in result.download_note
    assert sorted(tmp_path.iterdir()) == []


def test_a_cached_model_is_represented_without_publishing_its_path(tmp_path):
    cache = tmp_path / "cache"
    entry = cache / cache_directory_name("Qwen/Qwen3-0.6B")
    entry.mkdir(parents=True)
    (entry / "refs").write_text("x", encoding="utf-8")
    result = plan_training(make_config(), dataset_root=tmp_path, output_root=tmp_path,
                           model_cache_root=cache)
    assert result.identity.cache_status.value == "present"
    assert result.plan.model_download_required is False
    assert str(cache) not in json.dumps(result.to_dict())


# ══════════════════════════════════════════════════════════════════════════════
#  Confirmation
# ══════════════════════════════════════════════════════════════════════════════
def test_the_token_is_the_prefix_plus_the_full_digest(tmp_path):
    plan_obj = plan(tmp_path).plan
    assert plan_obj.confirmation_token() == CONFIRMATION_PREFIX + plan_obj.plan_hash()
    assert len(plan_obj.plan_hash()) == 64


def test_a_valid_token_is_accepted_as_syntax(tmp_path):
    plan_obj = plan(tmp_path).plan
    assert check_training_confirmation(plan_obj.confirmation_token(), plan_obj)


@pytest.mark.parametrize("token", [
    "yes", "YES", "y", "true", "--force", "TRAIN", "TRAIN:", "smoke-001",
    "train:" + "a" * 64, "TRAIN:" + "a" * 12, "TRAIN: " + "a" * 64, "",
])
def test_a_generic_confirmation_authorises_nothing(tmp_path, token):
    with pytest.raises(ConfirmationRejected):
        check_training_confirmation(token, plan(tmp_path).plan)


@pytest.mark.parametrize("token", [True, False, 1, 0, None, ["TRAIN:x"]])
def test_a_non_string_confirmation_is_refused_by_type(tmp_path, token):
    with pytest.raises(ConfirmationRejected, match="authorises every plan equally"):
        check_training_confirmation(token, plan(tmp_path).plan)


@pytest.mark.parametrize("token", ["@/tmp/token", "tokens/t.txt", "C:\\tokens\\t.txt"])
def test_a_confirmation_that_names_a_file_is_refused(tmp_path, token):
    with pytest.raises(ConfirmationRejected, match="names a file"):
        check_training_confirmation(token, plan(tmp_path).plan)


def test_a_confirmation_for_a_different_plan_is_refused(tmp_path):
    other = plan(tmp_path, make_config(seed=99)).plan
    with pytest.raises(ConfirmationRejected, match="Something changed"):
        check_training_confirmation(other.confirmation_token(), plan(tmp_path).plan)


def test_an_environment_variable_is_never_consulted_for_a_confirmation(tmp_path,
                                                                       monkeypatch):
    plan_obj = plan(tmp_path).plan
    monkeypatch.setenv("JARVIS_TRAIN_CONFIRM", plan_obj.confirmation_token())
    monkeypatch.setenv("TRAIN_CONFIRM", plan_obj.confirmation_token())
    with pytest.raises(ConfirmationRejected):
        check_training_confirmation("", plan_obj)


def test_execution_always_refuses(tmp_path):
    with pytest.raises(ExecutionNotImplemented, match="ships in S3B"):
        refuse_execution(plan(tmp_path).plan)
