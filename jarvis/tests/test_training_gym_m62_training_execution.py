"""V69 M62 S3B — the execution lifecycle, driven end to end by a fake trainer.

The claims under test are that no run starts without an exact plan-bound confirmation
recomputed from the current state of the world, that a confirmation authorises exactly one
run whatever that run's ending, that a backend reporting success cannot by itself produce
a completed adapter, and that every bad ending — failure, divergence, interruption,
refused artifacts — leaves a directory nobody can mistake for a finished one.

The corpus is the real one: promoted through the real authorities and exported by the real
exporter, so the dataset the executor verifies is a dataset and not a fixture. The trainer
is fake, because every property worth testing here is a property of a trainer
misbehaving, and no real trainer misbehaves on demand.

Nothing here imports a machine-learning framework, downloads a model or opens a socket.
The last section asserts that directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from test_training_gym_m62_training_dataset_binding import (  # noqa: E402
    NOW,
    PINNED_REVISION,
    Corpus,
)
from training_gym.datasets.export import SFT_FILENAME
from training_gym.schemas import sha256_file, sha256_text
from training_gym.training.artifacts import (
    ADAPTER_MANIFEST_FILE,
    AdapterManifest,
    validate_adapter_directory,
    verify_completed_run,
)
from training_gym.training.backend import (
    BackendError,
    BackendResult,
    BackendStatus,
    CancellationToken,
    require_backend,
)
from training_gym.training.backends import available_backend_ids, get_backend
from training_gym.training.backends.fake import FakeMode, FakeTrainingBackend
from training_gym.training.model_identity import cache_directory_name
from training_gym.training.config import (
    TrainingMethod,
    TrainingRunState,
)
from training_gym.training.dataset_conversion import (
    DatasetConversionError,
    build_labels,
    check_masking,
    convert_sft_export,
    LOSS_IGNORE_INDEX,
)
from training_gym.training.execution import (
    execute_training,
    run_preflight,
)
from training_gym.training.plan import CONFIRMATION_PREFIX
from training_gym.training.planner import plan_training
from training_gym.training.run_store import (
    ErrorCategory,
    RunEventKind,
    RunRecord,
    RunStoreError,
    consume_plan,
    create_run_directory,
    is_plan_consumed,
    training_entries,
    training_ledger_path,
)

ACTOR = "operator-1"
NONCE = "0a1b2c3d"


@pytest.fixture(autouse=True)
def declared_dependencies_present(monkeypatch):
    """Stand in for a host where the optional training profile IS installed.

    Nothing is installed and nothing is imported: ``probe_package`` answers by reading
    distribution metadata, and this replaces that reading. Simulating the *presence* of
    the packages is what lets the execution lifecycle be tested at all on a machine that
    deliberately does not have them.

    Returns the real probe, so the test that asserts the opposite direction — a missing
    dependency refuses before the plan is spent — can put it back.
    """
    import training_gym.training.dependencies as deps

    real = deps.probe_package

    def installed(package: str):
        return deps.PackageAvailability(
            package, deps.DependencyState.INSTALLED,
            version=deps.MINIMUM_VERSIONS.get(package, "1.0.0"),
            minimum=deps.MINIMUM_VERSIONS.get(package, ""))

    monkeypatch.setattr(deps, "probe_package", installed)
    return real


# ══════════════════════════════════════════════════════════════════════════════
#  The world one run happens in
# ══════════════════════════════════════════════════════════════════════════════
class World:
    """A real promoted corpus, a real export, and somewhere to train into."""

    def __init__(self, base: Path) -> None:
        self.corpus = Corpus(base / "store", base / "exports")
        self.output_root = base / "training"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.train_file = self.corpus.export_directory / SFT_FILENAME
        # An inert stand-in for a populated hub cache. Nothing was downloaded: the
        # planner asks whether a directory named for this model exists and holds
        # anything, and that is a question a placeholder file answers honestly.
        self.model_cache_root = base / "model-cache"
        cached = self.model_cache_root / cache_directory_name("Qwen/Qwen3-0.6B")
        cached.mkdir(parents=True)
        (cached / "refs").write_text("placeholder", encoding="utf-8")

    def config(self, **overrides):
        return self.corpus.config(**overrides)

    def planning(self, config=None, **kwargs):
        kwargs.setdefault("model_cache_root", self.model_cache_root)
        return plan_training(config or self.config(), dataset_root=self.corpus.root,
                             output_root=self.output_root,
                             export_root=self.corpus.export_root, **kwargs)

    def token(self, config=None) -> str:
        return self.planning(config).plan.confirmation_token()

    def run(self, *, backend=None, config=None, confirmation=None, **overrides):
        config = config or self.config()
        kwargs = dict(
            dataset_root=self.corpus.root, output_root=self.output_root,
            confirmation=confirmation if confirmation is not None else self.token(config),
            backend=backend or FakeTrainingBackend(),
            train_file=self.train_file, actor=ACTOR, at=NOW, nonce=NONCE,
            export_root=self.corpus.export_root,
            model_cache_root=self.model_cache_root,
            expected_export_sha256=sha256_file(self.train_file),
            expected_record_count=self.corpus.export.manifest.record_count)
        kwargs.update(overrides)
        return execute_training(config, **kwargs)

    @property
    def run_directory(self) -> Path:
        return self.output_root / "runs" / "binding-001"


@pytest.fixture()
def world(tmp_path) -> World:
    return World(tmp_path)


def _completed(world: World, **overrides):
    outcome = world.run(**overrides)
    assert outcome.ok, outcome.problems
    return outcome


# ══════════════════════════════════════════════════════════════════════════════
#  1..12 — preflight
# ══════════════════════════════════════════════════════════════════════════════
def test_a_token_does_not_expire_because_free_disk_or_free_ram_moved(world):
    """The volatile measurements are reported in full and excluded from the identity.

    A raw gigabyte count crosses an integer boundary whenever anything else on the host
    writes a file. If that invalidated the plan, an operator's token would expire between
    being printed and being typed — and an operator whose token randomly stops working
    learns to re-plan and paste the new one WITHOUT READING IT, which is exactly the
    control the token exists to be.
    """
    from dataclasses import replace as _replace

    from training_gym.training.environment import HardwareCapabilityReport, SizeCategory

    report = HardwareCapabilityReport.detect(output_root=world.output_root)
    drifted = _replace(report, available_ram_gb=report.available_ram_gb + 3,
                       output_disk_free_gb=report.output_disk_free_gb + 7)
    assert drifted.report_hash() == report.report_hash()
    assert drifted.to_dict() != report.to_dict()
    # A change of CATEGORY is a real change and still invalidates the plan.
    recategorised = _replace(report, total_ram_category=SizeCategory.UNDER_8GB)
    assert recategorised.report_hash() != report.report_hash()


def test_a_valid_plan_recomputes_identically_and_its_token_is_accepted(world):
    first = world.planning().plan
    second = world.planning().plan
    assert first.plan_hash() == second.plan_hash()
    assert run_preflight(world.config(), dataset_root=world.corpus.root,
                         output_root=world.output_root,
                         confirmation=first.confirmation_token(),
                         export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root).ok


@pytest.mark.parametrize("overrides", [
    {"seed": 99}, {"epochs": 2}, {"max_steps": 11}, {"batch_size": 2},
    {"learning_rate": 1e-5}, {"max_sequence_length": 256},
    {"experiment_name": "a different experiment"},
])
def test_a_changed_config_invalidates_the_confirmation(world, overrides):
    stale = world.token()
    preflight = run_preflight(world.config(**overrides),
                              dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation=stale,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION


def test_a_changed_dataset_invalidates_the_confirmation(world):
    stale = world.token()
    shard = world.corpus.shard_path(
        __import__("training_gym.datasets.candidate", fromlist=["x"])
        .DatasetSplit.TRAIN)
    shard.write_text(shard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    preflight = run_preflight(world.config(), dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation=stale,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.DATASET


def test_a_changed_export_invalidates_the_confirmation(world):
    stale = world.token()
    rows = world.train_file.read_text(encoding="utf-8").splitlines()
    world.train_file.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
    preflight = run_preflight(world.config(), dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation=stale,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.DATASET


def test_a_changed_model_revision_invalidates_the_confirmation(world):
    stale = world.token()
    other = sha256_text("a different pinned commit")[:40]
    preflight = run_preflight(
        world.config(base_model_revision=other, tokenizer_revision=other),
        dataset_root=world.corpus.root, output_root=world.output_root,
        confirmation=stale, export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION


def test_a_changed_hardware_report_invalidates_the_confirmation(world):
    from training_gym.training.environment import AcceleratorProbe, Availability

    stale = world.token()
    preflight = run_preflight(
        world.config(), dataset_root=world.corpus.root,
        output_root=world.output_root, confirmation=stale,
        export_root=world.corpus.export_root,
        accelerator_probe=lambda: AcceleratorProbe(
            cuda_runtime=Availability.AVAILABLE, cuda_device_count=1,
            accelerator_memory_gb=24.0))
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION


def test_a_different_output_root_invalidates_the_confirmation(world, tmp_path):
    stale = world.token()
    elsewhere = tmp_path / "other-training"
    elsewhere.mkdir()
    preflight = run_preflight(world.config(), dataset_root=world.corpus.root,
                              output_root=elsewhere, confirmation=stale,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION


@pytest.mark.parametrize("bad", [
    "yes", "y", "true", True, False, None, 1, "",
    "TRAIN:", f"{CONFIRMATION_PREFIX}abc123",
    f"{CONFIRMATION_PREFIX}{'a' * 63}", f"{CONFIRMATION_PREFIX}{'A' * 64}",
    f"@/tmp/{CONFIRMATION_PREFIX}token", f"{CONFIRMATION_PREFIX}{'0' * 64}",
])
def test_anything_that_is_not_the_exact_token_is_refused(world, bad):
    preflight = run_preflight(world.config(), dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation=bad,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION
    assert not world.run_directory.exists()


def test_a_token_from_another_plan_is_refused(world):
    other = world.token(world.config(seed=1234))
    preflight = run_preflight(world.config(), dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation=other,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.CONFIRMATION


def test_dpo_execution_is_refused_before_anything_is_planned(world):
    preflight = run_preflight(world.config(method=TrainingMethod.DPO_LORA),
                              dataset_root=world.corpus.root,
                              output_root=world.output_root, confirmation="anything",
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category is ErrorCategory.UNSUPPORTED_METHOD
    assert any("WRONG answer" in p for p in preflight.problems)
    assert not world.run_directory.exists()
    assert not training_ledger_path(world.output_root).exists()


def test_qlora_without_measured_cuda_never_reaches_a_backend(world):
    config = world.config(method=TrainingMethod.SFT_QLORA)
    preflight = run_preflight(config, dataset_root=world.corpus.root,
                              output_root=world.output_root,
                              confirmation="TRAIN:" + "0" * 64,
                              export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert not preflight.ok
    assert preflight.category in (ErrorCategory.HARDWARE, ErrorCategory.DEPENDENCY)
    assert not world.run_directory.exists()


def test_an_existing_run_directory_refuses_before_the_plan_is_spent(world):
    world.run_directory.mkdir(parents=True)
    outcome = world.run()
    assert not outcome.ok
    assert outcome.error_category is ErrorCategory.CONFIGURATION
    assert not outcome.plan_consumed
    assert not is_plan_consumed(world.output_root, outcome.plan_hash)


# ══════════════════════════════════════════════════════════════════════════════
#  13..22 — replay protection
# ══════════════════════════════════════════════════════════════════════════════
def test_a_completed_run_consumes_its_plan_exactly_once(world):
    outcome = _completed(world)
    assert outcome.plan_consumed
    assert is_plan_consumed(world.output_root, outcome.plan_hash)
    entries = training_entries(world.output_root)
    assert [e["event"] for e in entries] == ["started", "completed"]
    assert all(e["plan_hash"] == outcome.plan_hash for e in entries)


def test_the_same_token_cannot_start_a_second_run(world):
    first = _completed(world)
    second = world.run(confirmation=CONFIRMATION_PREFIX + first.plan_hash)
    assert not second.ok
    assert second.error_category is ErrorCategory.REPLAY


def test_deleting_the_run_directory_does_not_un_consume_the_plan(world):
    first = _completed(world)
    for child in world.run_directory.iterdir():
        child.unlink()
    world.run_directory.rmdir()
    assert not world.run_directory.exists()
    assert is_plan_consumed(world.output_root, first.plan_hash)
    second = world.run(confirmation=CONFIRMATION_PREFIX + first.plan_hash)
    assert not second.ok
    assert second.error_category is ErrorCategory.REPLAY


def test_an_interrupted_run_leaves_its_plan_consumed(world):
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.INTERRUPTION))
    assert outcome.state is TrainingRunState.INTERRUPTED
    assert outcome.plan_consumed
    assert is_plan_consumed(world.output_root, outcome.plan_hash)
    assert [e["event"] for e in training_entries(world.output_root)] == [
        "started", "interrupted"]


def test_a_failed_run_leaves_its_plan_consumed(world):
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.EXCEPTION))
    assert outcome.state is TrainingRunState.FAILED
    assert is_plan_consumed(world.output_root, outcome.plan_hash)


def test_a_preflight_failure_never_consumes_the_plan(world):
    outcome = world.run(confirmation="TRAIN:" + "b" * 64)
    assert not outcome.plan_consumed
    assert not training_ledger_path(world.output_root).exists()


def test_the_ledger_refuses_to_be_read_or_written_through_a_link(world, tmp_path):
    _completed(world)
    ledger = training_ledger_path(world.output_root)
    decoy = tmp_path / "decoy.jsonl"
    decoy.write_text(ledger.read_text(encoding="utf-8"), encoding="utf-8")
    ledger.unlink()
    try:
        ledger.symlink_to(decoy)
    except (OSError, NotImplementedError):
        pytest.skip("creating a symlink needs elevation on Windows")
    with pytest.raises(RunStoreError, match="symlink"):
        training_entries(world.output_root)


def test_a_plan_hash_that_is_not_a_digest_is_refused(world):
    with pytest.raises(RunStoreError):
        is_plan_consumed(world.output_root, "not-a-digest")
    with pytest.raises(RunStoreError):
        consume_plan(world.output_root, plan_hash="short", run_id="r-1", actor=ACTOR,
                     at=NOW, method="sft_lora")


def test_two_runs_race_one_mkdir_and_exactly_one_wins(world):
    create_run_directory(world.output_root, "binding-001")
    with pytest.raises(RunStoreError, match="already exists"):
        create_run_directory(world.output_root, "binding-001")


def test_no_ledger_entry_carries_a_private_path_or_a_username(world):
    _completed(world)
    text = training_ledger_path(world.output_root).read_text(encoding="utf-8")
    assert str(world.output_root) not in text
    assert world.output_root.as_posix() not in text


# ══════════════════════════════════════════════════════════════════════════════
#  23..34 — the state machine and interruption
# ══════════════════════════════════════════════════════════════════════════════
def test_a_completed_run_walked_every_state_in_order(world):
    _completed(world)
    record = json.loads((world.run_directory / "run.json").read_text(encoding="utf-8"))
    assert record["state"] == "completed"
    assert record["completed"] is True
    walk = [step["to"] for step in record["history"]]
    assert walk == ["config_validated", "dataset_verified", "dependencies_verified",
                    "hardware_verified", "planned", "awaiting_confirmation",
                    "preflight_verified", "starting", "running",
                    "artifact_validation", "completed"]


def test_a_run_record_whose_state_was_set_rather_than_reached_is_detectable():
    record = RunRecord(run_id="r-1", plan_hash="a" * 64, training_config_hash="b" * 64,
                       method="sft_lora", created_at_utc=NOW)
    assert record.history_supports_state()
    record.state = TrainingRunState.COMPLETED
    assert not record.history_supports_state()


def test_an_illegal_transition_is_refused():
    from training_gym.training.config import TrainingTransitionError

    record = RunRecord(run_id="r-1", plan_hash="a" * 64, training_config_hash="b" * 64,
                       method="sft_lora", created_at_utc=NOW)
    with pytest.raises(TrainingTransitionError):
        record.transition(TrainingRunState.RUNNING, at=NOW)
    with pytest.raises(TrainingTransitionError):
        record.transition(TrainingRunState.COMPLETED, at=NOW)


def test_a_backend_that_is_cancelled_produces_an_interrupted_run_and_no_manifest(world):
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.INTERRUPTION))
    assert outcome.state is TrainingRunState.INTERRUPTED
    assert outcome.manifest_hash == ""
    assert not world.run_directory.exists()
    quarantined = world.output_root / outcome.quarantined_as
    assert quarantined.is_dir()
    assert not (quarantined / ADAPTER_MANIFEST_FILE).exists()
    assert not (quarantined / "adapter_model.safetensors").exists()


def test_a_partial_safetensors_is_never_left_where_it_could_read_as_an_adapter(world):
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.INTERRUPTION))
    quarantined = world.output_root / outcome.quarantined_as
    assert list(quarantined.rglob("*.safetensors")) == []
    assert "adapter_model.safetensors" in outcome.adapter_files


def test_a_keyboard_interrupt_becomes_an_interrupted_run(world):
    class Interrupting(FakeTrainingBackend):
        def execute(self, request, *, cancellation):
            raise KeyboardInterrupt()

    outcome = world.run(backend=Interrupting())
    assert outcome.state is TrainingRunState.INTERRUPTED
    assert outcome.error_category is ErrorCategory.INTERRUPTED
    assert not world.run_directory.exists()


@pytest.mark.parametrize("mode,category", [
    (FakeMode.EXCEPTION, ErrorCategory.BACKEND),
    (FakeMode.OUT_OF_MEMORY, ErrorCategory.OUT_OF_MEMORY),
    (FakeMode.DISK_FULL, ErrorCategory.DISK_FULL),
    (FakeMode.ZERO_STEPS, ErrorCategory.INVALID_METRICS),
])
def test_every_backend_failure_becomes_a_failed_run_with_a_bounded_reason(
        world, mode, category):
    outcome = world.run(backend=FakeTrainingBackend(mode=mode))
    assert outcome.state is TrainingRunState.FAILED
    assert outcome.error_category is category
    assert not outcome.ok
    assert outcome.manifest_hash == ""
    assert all(len(p) < 2000 for p in outcome.problems)


@pytest.mark.parametrize("mode", [FakeMode.NAN_LOSS, FakeMode.INFINITE_LOSS])
def test_a_non_finite_loss_cannot_even_be_represented_as_a_result(world, mode):
    outcome = world.run(backend=FakeTrainingBackend(mode=mode))
    assert outcome.state is TrainingRunState.FAILED
    assert outcome.error_category is ErrorCategory.INVALID_METRICS
    assert not (world.run_directory / ADAPTER_MANIFEST_FILE).exists()


def test_a_backend_result_refuses_impossible_numbers():
    for kwargs in ({"train_loss": float("nan")}, {"eval_loss": float("inf")},
                   {"steps_attempted": 2, "steps_completed": 5},
                   {"duration_seconds": -1.0}, {"steps_completed": -1}):
        with pytest.raises(BackendError):
            BackendResult(backend_id="x", backend_version="1",
                          status=BackendStatus.SUCCEEDED, **kwargs)


def test_a_backend_may_not_report_a_path_as_an_output_file():
    with pytest.raises(BackendError, match="is a path"):
        BackendResult(backend_id="x", backend_version="1",
                      status=BackendStatus.SUCCEEDED,
                      output_files=("/etc/passwd",))


def test_a_failed_run_is_never_reopened_as_completed(world):
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.EXCEPTION))
    quarantined = world.output_root / outcome.quarantined_as
    record = json.loads((quarantined / "run.json").read_text(encoding="utf-8"))
    assert record["state"] == "failed"
    assert record["completed"] is False
    assert verify_completed_run(quarantined, lora=world.config().lora.to_dict(),
                                base_model_id=world.config().base_model_id)


def test_no_event_carries_a_prompt_a_secret_or_a_private_path(world):
    _completed(world)
    text = (world.run_directory / "training_log.jsonl").read_text(encoding="utf-8")
    assert str(world.output_root) not in text
    for _cid, _task, prompt, answer in __import__(
            "test_training_gym_m62_training_dataset_binding", fromlist=["x"])._RECORDS:
        assert prompt not in text
        assert answer not in text
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert {e["event"] for e in events} <= {k.value for k in RunEventKind}
    assert all(len(e.get("message", "")) <= 300 for e in events)


# ══════════════════════════════════════════════════════════════════════════════
#  35..48 — artifacts
# ══════════════════════════════════════════════════════════════════════════════
def test_a_valid_adapter_produces_a_manifest_that_re_derives(world):
    outcome = _completed(world)
    problems = verify_completed_run(world.run_directory,
                                    lora=world.config().lora.to_dict(),
                                    base_model_id=world.config().base_model_id)
    assert problems == ()
    stored = json.loads(
        (world.run_directory / ADAPTER_MANIFEST_FILE).read_text(encoding="utf-8"))
    manifest = AdapterManifest.from_dict(stored)
    assert manifest.manifest_hash() == outcome.manifest_hash
    assert manifest.completed is True
    assert manifest.run_state == "completed"
    assert manifest.interrupted is False


def test_the_manifest_binds_the_run_to_the_exact_dataset_and_model(world):
    outcome = _completed(world)
    stored = json.loads(
        (world.run_directory / ADAPTER_MANIFEST_FILE).read_text(encoding="utf-8"))
    reference = world.config().dataset_reference
    assert stored["dataset_reference_hash"] == reference.reference_hash()
    assert stored["dataset_manifest_hash"] == reference.dataset_manifest_hash
    assert stored["train_shard_hash"] == reference.train_shard_hash
    assert stored["validation_shard_hash"] == reference.validation_shard_hash
    assert stored["hidden_evaluation_hash"] == (
        reference.hidden_evaluation_manifest_hash)
    assert stored["security_regression_hash"] == (
        reference.security_regression_manifest_hash)
    assert stored["export_manifest_hash"] == reference.export_manifest_hash
    assert stored["base_model_revision"] == PINNED_REVISION
    assert stored["plan_hash"] == outcome.plan_hash


def test_the_manifest_carries_no_private_path_username_or_credential(world):
    _completed(world)
    text = (world.run_directory / ADAPTER_MANIFEST_FILE).read_text(encoding="utf-8")
    for fragment in (str(world.output_root), world.output_root.as_posix(),
                     str(Path.home()), str(world.corpus.root)):
        assert fragment not in text
    assert "runs/" not in text


def test_a_tampered_manifest_is_detected_on_reload(world):
    _completed(world)
    path = world.run_directory / ADAPTER_MANIFEST_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["train_loss"] = 0.001
    path.write_text(json.dumps(payload), encoding="utf-8")
    problems = verify_completed_run(world.run_directory,
                                    lora=world.config().lora.to_dict(),
                                    base_model_id=world.config().base_model_id)
    assert any("does not match" in p for p in problems)


def test_a_tampered_artifact_file_is_detected(world):
    _completed(world)
    weights = world.run_directory / "adapter_model.safetensors"
    weights.write_bytes(weights.read_bytes() + b"\x00")
    problems = verify_completed_run(world.run_directory,
                                    lora=world.config().lora.to_dict(),
                                    base_model_id=world.config().base_model_id)
    assert problems


def test_a_file_appearing_after_completion_is_detected(world):
    _completed(world)
    (world.run_directory / "special_tokens_map.json").write_text("{}", encoding="utf-8")
    problems = verify_completed_run(world.run_directory,
                                    lora=world.config().lora.to_dict(),
                                    base_model_id=world.config().base_model_id)
    assert any("not named by the manifest" in p for p in problems)


@pytest.mark.parametrize("mode,needle", [
    (FakeMode.EMPTY_ADAPTER, "empty"),
    (FakeMode.MISSING_ADAPTER_CONFIG, "required and absent"),
    (FakeMode.MALFORMED_ADAPTER_CONFIG, "strict JSON"),
    (FakeMode.UNSAFE_BIN_OUTPUT, "forbidden extension"),
    (FakeMode.PARTIAL_SAFETENSORS, "truncated"),
    (FakeMode.ZERO_TENSORS, "no tensors"),
    (FakeMode.BASE_MODEL_TENSORS, "not LoRA"),
    (FakeMode.UNEXPECTED_FILE, "allowlist"),
    (FakeMode.BASE_MODEL_MISMATCH, "fitted on"),
    (FakeMode.LORA_CONFIG_MISMATCH, "the plan approved"),
])
def test_every_unsafe_artifact_shape_refuses_completion(world, mode, needle):
    outcome = world.run(backend=FakeTrainingBackend(mode=mode))
    assert outcome.state is TrainingRunState.FAILED
    assert outcome.error_category is ErrorCategory.ARTIFACT
    assert any(needle in problem for problem in outcome.problems), outcome.problems
    assert not world.run_directory.exists()
    quarantined = world.output_root / outcome.quarantined_as
    assert not (quarantined / ADAPTER_MANIFEST_FILE).exists()


def test_an_oversized_adapter_is_refused(world, monkeypatch):
    import training_gym.training.artifacts as artifacts

    monkeypatch.setattr(artifacts, "MAX_ADAPTER_FILE_BYTES", 64)
    outcome = world.run(backend=FakeTrainingBackend(mode=FakeMode.OVERSIZED_ARTIFACT))
    assert outcome.state is TrainingRunState.FAILED
    assert any("ceiling" in p for p in outcome.problems)


def test_a_backend_writing_outside_its_run_directory_is_refused(world):
    outcome = world.run(backend=FakeTrainingBackend(
        mode=FakeMode.WRITE_OUTSIDE_WORKSPACE))
    assert outcome.state is TrainingRunState.FAILED
    assert any("outside its run directory" in p for p in outcome.problems)


def test_a_manifest_may_not_claim_completion_with_zero_steps():
    from training_gym.training.artifacts import AdapterArtifactError

    base = _manifest_kwargs()
    with pytest.raises(AdapterArtifactError, match="zero completed steps"):
        AdapterManifest(**{**base, "completed_steps": 0})
    with pytest.raises(AdapterArtifactError, match="both completed and interrupted"):
        AdapterManifest(**{**base, "interrupted": True})
    with pytest.raises(AdapterArtifactError, match="must agree"):
        AdapterManifest(**{**base, "run_state": "running"})
    with pytest.raises(AdapterArtifactError, match="exceeds"):
        AdapterManifest(**{**base, "completed_steps": 999})


def _manifest_kwargs() -> dict:
    digest = "a" * 64
    return dict(
        run_id="r-1", run_state="completed", plan_hash=digest,
        training_config_hash=digest, method="sft_lora", backend_id="fake",
        backend_version="1", base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision=digest, base_model_identity_hash=digest,
        tokenizer_id="Qwen/Qwen3-0.6B", tokenizer_revision=digest,
        tokenizer_identity_hash=digest, tokenizer_chat_template_hash=digest,
        dataset_id="gym", dataset_version="v1", dataset_reference_hash=digest,
        dataset_manifest_hash=digest, train_shard_hash=digest,
        validation_shard_hash=digest, hidden_evaluation_hash=digest,
        security_regression_hash=digest, export_manifest_hash=digest,
        split_algorithm_version="m62.split.1", seed=42, lora={"rank": 8},
        assistant_only_loss={"verified": True}, package_versions={},
        device_category="cpu", precision="fp32", requested_steps=10,
        completed_steps=8, epochs_completed=1.0, train_loss=1.2, eval_loss=1.3,
        truncated_records=0, duration_seconds=1.0, files=(), total_bytes=0,
        tree_hash=digest, created_at_utc=NOW)


def test_an_adapter_directory_holding_a_manifest_before_validation_is_refused(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / ADAPTER_MANIFEST_FILE).write_text("{}", encoding="utf-8")
    result = validate_adapter_directory(directory, lora={}, base_model_id="a/b")
    assert not result.ok
    assert any("written last" in p for p in result.problems)


# ══════════════════════════════════════════════════════════════════════════════
#  49..60 — dataset conversion and assistant-only loss
# ══════════════════════════════════════════════════════════════════════════════
def test_the_verified_export_converts_deterministically(world):
    first = convert_sft_export(world.train_file, max_sequence_length=512)
    second = convert_sft_export(world.train_file, max_sequence_length=512)
    assert first.content_hash() == second.content_hash()
    assert len(first) == world.corpus.export.manifest.record_count
    assert all(record.completion.strip() for record in first.records)
    assert all(record.prompt_messages[-1]["role"] == "user"
               for record in first.records)


def test_a_corpus_whose_digest_changed_is_refused(world):
    digest = sha256_file(world.train_file)
    with pytest.raises(DatasetConversionError, match="changed after the plan"):
        convert_sft_export(world.train_file, max_sequence_length=512,
                           expected_sha256=sha256_text("something else"))
    assert convert_sft_export(world.train_file, max_sequence_length=512,
                              expected_sha256=digest)


@pytest.mark.parametrize("rows,needle", [
    ([{"messages": [{"role": "user", "content": "hi"}], "metadata": {"candidate_id": "c"}}],
     "role order"),
    ([{"messages": [{"role": "assistant", "content": "a"},
                    {"role": "user", "content": "u"}],
       "metadata": {"candidate_id": "c"}}], "role order"),
    ([{"messages": [{"role": "tool", "content": "x"},
                    {"role": "assistant", "content": "a"}],
       "metadata": {"candidate_id": "c"}}], "role"),
    ([{"messages": [{"role": "user", "content": "u"},
                    {"role": "assistant", "content": "   "}],
       "metadata": {"candidate_id": "c"}}], "assistant target is empty"),
    ([{"messages": [{"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"}],
       "metadata": {"candidate_id": "c"}, "reasoning": "hidden"}], "hidden reasoning"),
    ([{"messages": [{"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"}], "metadata": {}}],
     "names no candidate"),
    ([{"messages": [{"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"}],
       "metadata": {"candidate_id": "c"}},
      {"messages": [{"role": "user", "content": "v"},
                    {"role": "assistant", "content": "b"}],
       "metadata": {"candidate_id": "c"}}], "appears twice"),
])
def test_a_malformed_row_is_refused_and_never_silently_dropped(tmp_path, rows, needle):
    path = tmp_path / "corpus.jsonl"
    path.write_text("".join(f"{json.dumps(r)}\n" for r in rows), encoding="utf-8")
    with pytest.raises(DatasetConversionError, match=needle):
        convert_sft_export(path, max_sequence_length=512)


def test_an_empty_corpus_is_refused(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(DatasetConversionError, match="no rows"):
        convert_sft_export(path, max_sequence_length=512)


def test_a_revoked_or_held_out_record_can_never_be_converted(world):
    first = convert_sft_export(world.train_file, max_sequence_length=512)
    victim = first.records[0].candidate_id
    with pytest.raises(DatasetConversionError, match="revoked"):
        convert_sft_export(world.train_file, max_sequence_length=512,
                           revoked_candidate_ids=frozenset({victim}))
    with pytest.raises(DatasetConversionError, match="held-out"):
        convert_sft_export(world.train_file, max_sequence_length=512,
                           held_out_candidate_ids=frozenset({victim}))


def test_a_record_count_that_disagrees_with_the_export_manifest_is_refused(world):
    with pytest.raises(DatasetConversionError, match="recorded"):
        convert_sft_export(world.train_file, max_sequence_length=512,
                           expected_record_count=999)


def test_assistant_only_loss_masks_exactly_the_prompt():
    prompt = [1, 2, 3, 4]
    full = [1, 2, 3, 4, 9, 8, 7]
    labels = build_labels(prompt, full)
    assert labels == [LOSS_IGNORE_INDEX] * 4 + [9, 8, 7]
    assert check_masking(labels, len(prompt)) == ()


def test_a_prompt_that_is_not_a_prefix_of_the_conversation_blocks_the_run():
    with pytest.raises(DatasetConversionError, match="prefix"):
        build_labels([1, 2, 3], [1, 2, 9, 4, 5])
    with pytest.raises(DatasetConversionError, match="no completion"):
        build_labels([1, 2, 3], [1, 2, 3])
    with pytest.raises(DatasetConversionError, match="tokenized to nothing"):
        build_labels([], [1, 2, 3])


def test_an_unmasked_prompt_token_is_reported_rather_than_trained_on():
    labels = [LOSS_IGNORE_INDEX, 5, LOSS_IGNORE_INDEX, 9]
    problems = check_masking(labels, 3)
    assert any("not masked" in p for p in problems)
    assert check_masking([LOSS_IGNORE_INDEX] * 4, 2)[0].startswith("no completion")


def test_the_chat_template_hash_is_empty_when_no_template_exists():
    from training_gym.training.dataset_conversion import chat_template_hash

    assert chat_template_hash(None) == ""
    assert chat_template_hash("  ") == ""
    assert len(chat_template_hash("{% for m in messages %}{{ m }}{% endfor %}")) == 64


# ══════════════════════════════════════════════════════════════════════════════
#  61..70 — what execution did NOT do
# ══════════════════════════════════════════════════════════════════════════════
_BANNED = ("torch", "transformers", "datasets", "accelerate", "peft", "trl",
           "bitsandbytes", "huggingface_hub", "tokenizers", "sentence_transformers")


def _ml_modules() -> set[str]:
    return {name for name in sys.modules if name.split(".")[0] in _BANNED}


def test_a_complete_fake_backed_run_imports_no_training_framework(world):
    before = _ml_modules()
    _completed(world)
    assert _ml_modules() == before


def test_preflight_imports_no_training_framework(world):
    before = _ml_modules()
    run_preflight(world.config(), dataset_root=world.corpus.root,
                  output_root=world.output_root, confirmation=world.token(),
                  export_root=world.corpus.export_root,
                              model_cache_root=world.model_cache_root)
    assert _ml_modules() == before


def test_importing_the_execution_modules_loads_no_training_framework():
    import importlib

    before = _ml_modules()
    for name in ("execution", "run_store", "artifacts", "backend",
                 "dataset_conversion"):
        importlib.import_module(f"training_gym.training.{name}")
    importlib.import_module("training_gym.training.backends")
    assert _ml_modules() == before


def test_no_run_spawns_a_process_or_opens_a_socket(world, monkeypatch):
    import socket
    import subprocess

    def refuse(*args, **kwargs):  # pragma: no cover — reached only on a regression
        raise AssertionError("execution reached the network or a process")

    for module, name in ((socket, "socket"), (socket, "create_connection"),
                         (subprocess, "Popen"), (subprocess, "run")):
        monkeypatch.setattr(module, name, refuse)
    assert _completed(world).ok


def test_the_fake_backend_is_not_reachable_from_the_production_registry():
    from training_gym.training.backend import BackendError as _BackendError

    assert available_backend_ids() == ("transformers_peft",)
    for name in ("fake", "fake_deterministic", "mock", "", "../fake"):
        with pytest.raises(_BackendError):
            get_backend(name)


def test_the_fake_backend_still_satisfies_the_real_protocol():
    backend = require_backend(FakeTrainingBackend())
    assert backend.supports(TrainingMethod.SFT_LORA)
    assert not backend.supports(TrainingMethod.DPO_LORA)
    assert backend.readiness.__call__ is not None


def test_a_cancellation_token_is_the_only_stop_channel():
    token = CancellationToken()
    assert not token.requested
    token.raise_if_requested()
    token.request("operator asked")
    assert token.requested
    with pytest.raises(Exception, match="stop requested"):
        token.raise_if_requested()


def test_execution_creates_nothing_outside_the_output_root(world, tmp_path):
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    _completed(world)
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    for created in after - before:
        assert world.output_root in created.parents or created.parent == (
            world.output_root)


def test_the_dataset_and_the_source_tree_are_never_modified(world):
    corpus_before = {p: p.read_bytes() for p in world.corpus.root.rglob("*")
                     if p.is_file()}
    export_before = world.train_file.read_bytes()
    _completed(world)
    assert {p: p.read_bytes() for p in world.corpus.root.rglob("*")
            if p.is_file()} == corpus_before
    assert world.train_file.read_bytes() == export_before
    source = Path(__file__).resolve().parent.parent
    assert not (source / "runs").exists()
    assert not (source / "training_runs.jsonl").exists()


def test_the_outcome_document_is_screened_and_bounded(world):
    outcome = _completed(world)
    document = json.dumps(outcome.to_dict())
    assert str(world.output_root) not in document
    assert world.output_root.as_posix() not in document
    assert outcome.to_dict()["completed"] is True
    failed = world.run(config=world.config(run_id="binding-002"),
                       backend=FakeTrainingBackend(mode=FakeMode.EXCEPTION))
    assert json.dumps(failed.to_dict())
    assert failed.to_dict()["completed"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  71..80 — the production backend, verified structurally
# ══════════════════════════════════════════════════════════════════════════════
#  It has never been run against a model, so "it trains" is not asserted here — that is
#  a claim only a live run can support. What IS asserted is every property that must hold
#  whether or not it works: that it cannot reach a hub, cannot enable remote code, cannot
#  fall back to a pickle, and cannot execute DPO or QLoRA. These are read off the source,
#  because importing the module's dependencies is exactly what this milestone will not do.
def _backend_source() -> str:
    import training_gym.training.backends.transformers_peft as production

    return Path(production.__file__).read_text(encoding="utf-8")


def _backend_code() -> str:
    """The module's executable code, with every docstring and comment removed.

    Reconstructed from the AST rather than read as text. The same exemption the
    repository's own purity scanner makes, and for the same reason: this module explains
    at length why ``report_to="all"``, ``DataCollatorForCompletionOnlyLM`` and a pickle
    fallback are avoided, and a scan that could not tell prose from code would forbid
    documenting the decision — which is the opposite of what it is for.
    """
    import ast

    tree = ast.parse(_backend_source())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_production_backend_imports_no_framework_at_module_level():
    before = _ml_modules()
    import training_gym.training.backends.transformers_peft as production

    assert _ml_modules() == before
    assert production.build_backend().backend_id == "transformers_peft"
    assert _ml_modules() == before


def test_the_production_backend_satisfies_the_protocol_without_a_framework():
    backend = require_backend(get_backend("transformers_peft"))
    assert backend.supports(TrainingMethod.SFT_LORA)
    assert not backend.supports(TrainingMethod.SFT_QLORA)
    assert not backend.supports(TrainingMethod.DPO_LORA)
    assert backend.version()


def test_the_production_backend_refuses_qlora_and_dpo_before_importing_anything(world):
    backend = get_backend("transformers_peft")
    before = _ml_modules()
    for method in (TrainingMethod.SFT_QLORA, TrainingMethod.DPO_LORA):
        request = _bare_request(world, method)
        problems = backend.readiness(request)
        assert problems, method
    assert _ml_modules() == before


def _bare_request(world: World, method: TrainingMethod):
    from training_gym.training.backend import ExecutionRequest

    config = world.config(method=method)
    planning = world.planning(config)
    return ExecutionRequest(
        config=config, plan=planning.plan, run_directory=world.output_root,
        train_file=world.train_file, validation_file=None, device="cpu",
        precision="fp32")


@pytest.mark.parametrize("forbidden", [
    "push_to_hub=True", "wandb", "mlflow", "tensorboard", "comet_ml", "neptune",
    "report_to=\"all\"", "trust_remote_code=True", "safe_serialization=False",
    "subprocess", "shell=True", "os.system", "pip install", "urlopen", "requests.get",
    "DPOTrainer", "DataCollatorForCompletionOnlyLM", "torch.load", "pickle",
])
def test_the_production_backend_contains_no_forbidden_construct(forbidden):
    assert forbidden not in _backend_code(), forbidden


@pytest.mark.parametrize("required", [
    "trust_remote_code=TRUST_REMOTE_CODE", "local_files_only=local_only",
    "safe_serialization=True", "report_to=[]", "push_to_hub=False",
    "revision=config.base_model_revision", "revision=config.tokenizer_revision",
    "output_dir=str(request.run_directory)",
])
def test_the_production_backend_pins_every_safety_critical_argument(required):
    assert required in _backend_code(), required


def test_remote_code_is_a_module_constant_and_is_false():
    import training_gym.training.backends.transformers_peft as production

    assert production.TRUST_REMOTE_CODE is False


def test_local_files_only_is_the_default_and_only_a_flag_widens_it():
    source = _backend_code()
    assert "local_only = not request.allow_model_download" in source
    # Both loaders must consult it; one that did not would fetch silently.
    assert source.count("local_files_only=local_only") == 2


def test_the_all_linear_policy_is_unwrapped_to_the_string_peft_expects():
    """``("all-linear",)`` makes PEFT look for a module literally named ``all-linear``."""
    from training_gym.training.backends.transformers_peft import _target_modules
    from training_gym.training.config import LoRATargetPolicy

    assert _target_modules(LoRATargetPolicy.ALL_LINEAR) == "all-linear"
    assert _target_modules(LoRATargetPolicy.ATTENTION_ONLY) == [
        "q_proj", "k_proj", "v_proj", "o_proj"]


def test_a_missing_framework_is_a_named_refusal_and_never_an_install():
    from training_gym.training.backends.transformers_peft import (
        RUNTIME_PACKAGES,
        RuntimeUnavailable,
        _runtime,
    )

    assert set(RUNTIME_PACKAGES) == {"torch", "transformers", "peft"}
    try:
        _runtime()
    except RuntimeUnavailable as exc:
        assert "Install the optional profile yourself" in str(exc)
    else:  # pragma: no cover — only on a host that has the packages
        pytest.skip("the optional training profile is installed on this host")


def test_the_masking_self_test_cannot_pass_by_having_nothing_to_check():
    from training_gym.training.backends.transformers_peft import TransformersPeftBackend

    backend = TransformersPeftBackend()
    assert not backend._masking_self_test([]).verified
    unmasked = [{"labels": [1, 2, 3, 4], "prompt_length": 2}]
    assert not backend._masking_self_test(unmasked).verified
    nothing_supervised = [{"labels": [LOSS_IGNORE_INDEX] * 4, "prompt_length": 2}]
    assert not backend._masking_self_test(nothing_supervised).verified
    good = [{"labels": [LOSS_IGNORE_INDEX, LOSS_IGNORE_INDEX, 7, 8],
             "prompt_length": 2}]
    evidence = backend._masking_self_test(good)
    assert evidence.verified
    assert evidence.probe_prompt_tokens == 2
    assert evidence.probe_completion_tokens == 2
