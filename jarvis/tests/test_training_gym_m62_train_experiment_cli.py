"""tests/test_training_gym_m62_train_experiment_cli.py — V69 M62 S3A: the launcher.

`core.training_pipeline` has generated the argv `python -m scripts.train_experiment
--config ... --method ... --output ...` since V65 M17, and until this milestone the
module it named did not exist. It exists now, and it plans.

The claim under test is that running it is safe under every combination of flags. Not
"safe when used correctly" — safe when someone passes `--execute --confirm <token>
--allow-model-download` because they read that sequence in a ticket and expected it to
train something. That invocation must refuse, exit nonzero, create nothing, download
nothing, consume nothing, and say so.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import train_experiment as cli
from training_gym.training.config import TrainingConfig, TrainingMethod
from training_gym.training.dataset_reference import TrainingDatasetReference

_APP_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE = _APP_ROOT / "training" / "configs" / "qwen3-0.6b-lora-smoke.json"
_BANNED = ("torch", "transformers", "peft", "trl", "datasets", "accelerate",
           "bitsandbytes", "huggingface_hub", "tokenizers")


def make_document(**overrides) -> dict:
    config = TrainingConfig(
        run_id="smoke-001", experiment_name="qwen3 0.6b lora smoke",
        method=TrainingMethod.SFT_LORA, base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="REQUIRED_IMMUTABLE_REVISION",
        base_model_parameters_b=0.6, tokenizer_id="Qwen/Qwen3-0.6B",
        tokenizer_revision="REQUIRED_IMMUTABLE_REVISION",
        dataset_reference=TrainingDatasetReference.placeholder(
            dataset_id="gym", dataset_version="v1"),
        output_root_id="local-training", created_at_utc="2026-08-02T00:00:00Z")
    document = config.to_dict()
    document.update(overrides)
    return document


@pytest.fixture()
def config_path(tmp_path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(make_document()), encoding="utf-8")
    return path


def run(config_path: Path, tmp_path: Path, *flags, capsys=None):
    """Invoke main() with the roots pinned inside the test's own directory."""
    argv = ["--config", str(config_path), "--dataset-root", str(tmp_path),
            "--output-root", str(tmp_path), *flags]
    code = cli.main(argv)
    return code


def payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ══════════════════════════════════════════════════════════════════════════════
#  Import safety
# ══════════════════════════════════════════════════════════════════════════════
def test_importing_the_module_performs_no_io_and_contacts_nothing(monkeypatch):
    def _forbidden(*a, **k):  # pragma: no cover
        raise AssertionError("importing the launcher had an effect")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    import importlib

    importlib.reload(cli)
    assert cli.EXIT_OK == 0


def test_importing_the_launcher_loads_no_training_framework():
    assert [m for m in _BANNED if m in sys.modules] == []


def test_the_parser_builds_without_side_effects(tmp_path):
    parser = cli.build_parser()
    assert parser.prog == "train_experiment"
    assert sorted(tmp_path.iterdir()) == []


def test_the_help_text_states_that_execution_is_refused():
    text = cli.build_parser().format_help()
    assert "never trains" in text
    assert "S3B" in text
    assert "--dry-run" in text and "--execute" in text and "--confirm" in text


# ══════════════════════════════════════════════════════════════════════════════
#  The default is a dry run, and it is dry
# ══════════════════════════════════════════════════════════════════════════════
def test_the_default_invocation_is_a_dry_run(config_path, tmp_path, capsys):
    run(config_path, tmp_path, "--json")
    body = payload(capsys)
    assert body["dry_run"] is True
    assert body["wrote_anything"] is False
    assert body["trained_anything"] is False


def test_a_dry_run_creates_nothing(config_path, tmp_path, capsys):
    before = sorted(p.name for p in tmp_path.iterdir())
    run(config_path, tmp_path, "--json")
    capsys.readouterr()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("flags", [
    ("--json",), ("--dry-run", "--json"), ("--print-plan", "--json"),
    ("--check-dependencies", "--json"), ("--check-dataset", "--json"),
    ("--check-hardware", "--json"), ("--execute", "--json"),
    ("--allow-model-download", "--json"),
    ("--execute", "--allow-model-download", "--json"),
])
def test_no_invocation_creates_a_run_directory_or_an_adapter(config_path, tmp_path,
                                                             capsys, flags):
    run(config_path, tmp_path, *flags)
    capsys.readouterr()
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "adapters").exists()
    assert not (tmp_path / "configs").exists()
    assert list(tmp_path.rglob("*.safetensors")) == []


def test_no_invocation_reaches_the_network(config_path, tmp_path, capsys, monkeypatch):
    def _forbidden(*a, **k):  # pragma: no cover
        raise AssertionError("the launcher reached the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    for flags in (("--json",), ("--execute", "--json"), ("--check-hardware", "--json")):
        run(config_path, tmp_path, *flags)
        capsys.readouterr()


def test_no_invocation_spawns_a_process_or_calls_pip(config_path, tmp_path, capsys,
                                                     monkeypatch):
    def _forbidden(*a, **k):  # pragma: no cover
        raise AssertionError("the launcher spawned a process")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(os, "system", _forbidden)
    for flags in (("--json",), ("--check-dependencies", "--json"),
                  ("--execute", "--json")):
        run(config_path, tmp_path, *flags)
        capsys.readouterr()


def test_no_invocation_imports_a_training_framework(config_path, tmp_path, capsys):
    before = {m for m in _BANNED if m in sys.modules}
    for flags in (("--json",), ("--check-dependencies", "--json"),
                  ("--check-hardware", "--json"), ("--execute", "--json")):
        run(config_path, tmp_path, *flags)
        capsys.readouterr()
    assert {m for m in _BANNED if m in sys.modules} == before


# ══════════════════════════════════════════════════════════════════════════════
#  Execution is refused
# ══════════════════════════════════════════════════════════════════════════════
def test_execute_exits_nonzero_and_explains(config_path, tmp_path, capsys):
    assert run(config_path, tmp_path, "--execute", "--json") == cli.EXIT_REFUSED
    body = payload(capsys)
    assert body["ok"] is False
    assert body["execution_backend"] == "not_implemented_until_s3b"
    assert body["trained_anything"] is False
    assert body["created_adapter"] is False
    assert body["downloaded_anything"] is False
    assert "S3B" in body["error"]


def test_execute_with_a_valid_confirmation_still_refuses_and_does_not_consume_it(
        config_path, tmp_path, capsys):
    from training_gym.training.config import load_training_config
    from training_gym.training.planner import plan_training

    config = load_training_config(config_path)
    token = plan_training(config, dataset_root=tmp_path,
                          output_root=tmp_path).plan.confirmation_token()
    assert run(config_path, tmp_path, "--execute", "--confirm", token,
               "--json") == cli.EXIT_REFUSED
    body = payload(capsys)
    assert body["confirmation_consumed"] is False
    assert "NOT consumed" in body["confirmation"]
    assert body["trained_anything"] is False


@pytest.mark.parametrize("token", ["yes", "true", "TRAIN:" + "a" * 12, "@/tmp/tok",
                                   "TRAIN:" + "b" * 64])
def test_execute_with_a_bad_confirmation_reports_the_rejection_and_still_refuses(
        config_path, tmp_path, capsys, token):
    assert run(config_path, tmp_path, "--execute", "--confirm", token,
               "--json") == cli.EXIT_REFUSED
    body = payload(capsys)
    assert "rejected" in body["confirmation"]
    assert body["confirmation_consumed"] is False


def test_a_confirmation_without_execute_is_refused(config_path, tmp_path, capsys):
    assert run(config_path, tmp_path, "--confirm", "TRAIN:" + "a" * 64,
               "--json") == cli.EXIT_REFUSED
    assert "has none to authorise" in payload(capsys)["error"]


def test_dry_run_and_confirm_are_contradictory(config_path, tmp_path, capsys):
    """A token alongside an explicit no-op is refused rather than quietly ignored."""
    assert run(config_path, tmp_path, "--dry-run", "--confirm", "TRAIN:" + "a" * 64,
               "--json") == cli.EXIT_REFUSED
    assert "--confirm" in payload(capsys)["error"]


@pytest.mark.parametrize("flag", ["--force", "--yes", "--no-confirm", "--skip-gates",
                                  "--train", "--really-execute"])
def test_there_is_no_flag_that_trains_without_the_plan_token(flag):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--config", "x.json", flag])
    assert excinfo.value.code == cli.EXIT_USAGE


def test_execute_is_mutually_exclusive_with_the_inspection_modes():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--config", "x.json", "--execute", "--dry-run"])
    assert excinfo.value.code == cli.EXIT_USAGE


# ══════════════════════════════════════════════════════════════════════════════
#  --allow-model-download changes a report, not the world
# ══════════════════════════════════════════════════════════════════════════════
def test_allow_model_download_downloads_nothing(config_path, tmp_path, capsys):
    before = sorted(p.name for p in tmp_path.iterdir())
    run(config_path, tmp_path, "--allow-model-download", "--json")
    body = payload(capsys)
    assert body["plan"]["downloads_model"] is False
    assert body["plan"]["expected_effects"]["downloads_anything"] is False
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_plan_states_whether_weights_appear_cached(config_path, tmp_path, capsys):
    run(config_path, tmp_path, "--json")
    body = payload(capsys)
    assert body["plan"]["model_download_required"] is True
    assert "cached" in body["model_download"]


# ══════════════════════════════════════════════════════════════════════════════
#  Output hygiene
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("flags", [
    ("--json",), ("--print-plan", "--json"), ("--check-hardware", "--json"),
    ("--check-dependencies", "--json"), ("--execute", "--json"),
])
def test_no_output_carries_an_absolute_path_or_a_username(config_path, tmp_path,
                                                          capsys, flags):
    run(config_path, tmp_path, *flags)
    body = capsys.readouterr().out
    assert str(tmp_path) not in body
    assert "Users" not in body
    assert os.path.expanduser("~") not in body


def test_the_error_path_is_screened_too(tmp_path, capsys):
    """An OSError stringifies with its filename; a refusal must not leak the path."""
    missing = tmp_path / "no-such-config.json"
    assert cli.main(["--config", str(missing), "--json"]) == cli.EXIT_REFUSED
    body = capsys.readouterr().out
    assert "Users" not in body
    assert os.path.expanduser("~") not in body


def test_the_scrubber_replaces_the_home_directory():
    home = os.path.expanduser("~")
    assert home not in cli._scrub(f"failed to read {home}/secret.json")


def test_no_output_carries_an_environment_dump(config_path, tmp_path, capsys):
    run(config_path, tmp_path, "--json")
    body = capsys.readouterr().out.lower()
    for key in ("hf_token", "api_key", "environ", "ld_preload", "pythonpath",
                "hostname"):
        assert key not in body


def test_a_secret_in_the_environment_never_reaches_the_output(config_path, tmp_path,
                                                              capsys, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_thisisnotarealtokenbutlooksliketone")
    monkeypatch.setenv("WANDB_API_KEY", "wandb-secret-value-m62")
    run(config_path, tmp_path, "--json")
    body = capsys.readouterr().out
    assert "hf_thisisnotarealtokenbutlooksliketone" not in body
    assert "wandb-secret-value-m62" not in body


# ══════════════════════════════════════════════════════════════════════════════
#  Inspection modes
# ══════════════════════════════════════════════════════════════════════════════
def test_check_hardware_succeeds_and_runs_no_accelerator_probe(config_path, tmp_path,
                                                               capsys):
    assert run(config_path, tmp_path, "--check-hardware", "--json") == cli.EXIT_OK
    body = payload(capsys)
    assert body["accelerator_probe_ran"] is False
    assert body["cuda_available"] == "unknown"
    assert "did not run measured nothing" in body["accelerator_probe_note"]


def test_check_dependencies_reports_honestly_and_installs_nothing(config_path,
                                                                  tmp_path, capsys):
    code = run(config_path, tmp_path, "--check-dependencies", "--json")
    body = payload(capsys)
    assert body["installed_anything"] is False
    assert body["installs_anything"] is False
    assert body["operator_install_command"].startswith("pip install -r requirements/")
    assert code == (cli.EXIT_OK if body["ready"] else cli.EXIT_REFUSED)


def test_check_dataset_refuses_a_placeholder_reference(config_path, tmp_path, capsys):
    assert run(config_path, tmp_path, "--check-dataset", "--json") == cli.EXIT_REFUSED
    body = payload(capsys)
    assert body["evidence"] == "insufficient_evidence"
    assert body["missing"]


def test_print_plan_emits_the_plan_and_its_token(config_path, tmp_path, capsys):
    run(config_path, tmp_path, "--print-plan", "--json")
    body = payload(capsys)
    assert len(body["plan_hash"]) == 64
    assert body["confirmation_required"] == "TRAIN:" + body["plan_hash"]
    assert body["performs_training"] is False


def test_the_human_output_is_bounded_and_readable(config_path, tmp_path, capsys):
    run(config_path, tmp_path, "--check-hardware")
    out = capsys.readouterr().out
    assert "os_family:" in out
    assert len(out) < 20_000


# ══════════════════════════════════════════════════════════════════════════════
#  Config-file handling through the CLI
# ══════════════════════════════════════════════════════════════════════════════
def test_an_unknown_field_in_the_file_is_refused(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(make_document(traih_remote_code=True)), encoding="utf-8")
    assert run(path, tmp_path, "--json") == cli.EXIT_REFUSED
    assert "unknown field" in payload(capsys)["error"]


def test_a_duplicate_key_in_the_file_is_refused(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text('{"trust_remote_code": false, "trust_remote_code": true}',
                    encoding="utf-8")
    assert run(path, tmp_path, "--json") == cli.EXIT_REFUSED
    assert "duplicate key" in payload(capsys)["error"]


def test_trust_remote_code_in_the_file_is_refused(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(make_document(trust_remote_code=True)), encoding="utf-8")
    assert run(path, tmp_path, "--json") == cli.EXIT_REFUSED
    assert "must be false" in payload(capsys)["error"]


# ══════════════════════════════════════════════════════════════════════════════
#  The V65 M17 generated argv still resolves
# ══════════════════════════════════════════════════════════════════════════════
def test_the_pipeline_generated_argv_names_this_module():
    from core.training_pipeline import (
        BaseModelReference,
        DatasetReference,
        TrainingConfig as M17Config,
        TrainingMethod as M17Method,
        TransformersPeftBackend,
    )

    argv = TransformersPeftBackend().generate_command(M17Config(
        run_id="exp-001",
        base_model=BaseModelReference("qwen3-0.6b", "qwen", 0.6, 4096),
        dataset=DatasetReference(version="v1", path="training/datasets/v1"),
        training_method=M17Method.LORA))
    assert argv[:3] == ["python", "-m", "scripts.train_experiment"]
    assert "--config" in argv and "--method" in argv and "--output" in argv


def test_the_generated_argv_flags_are_all_accepted_by_this_parser(config_path,
                                                                  tmp_path):
    """The launcher must accept the argv M17 has been generating since V65."""
    args = cli.build_parser().parse_args(
        ["--config", str(config_path), "--method", "lora",
         "--output", str(tmp_path / "adapters" / "exp-001")])
    assert args.method == "lora"
    assert args.output


def test_the_legacy_output_flag_is_acknowledged_and_writes_nothing(config_path,
                                                                   tmp_path, capsys):
    target = tmp_path / "adapters" / "exp-001"
    run(config_path, tmp_path, "--method", "lora", "--output", str(target), "--json")
    body = payload(capsys)
    assert "output_flag" in body
    assert not target.exists()


def test_a_method_flag_that_contradicts_the_config_is_refused(config_path, tmp_path,
                                                              capsys):
    assert run(config_path, tmp_path, "--method", "qlora",
               "--json") == cli.EXIT_REFUSED
    assert "does not match the config" in payload(capsys)["error"]


# ══════════════════════════════════════════════════════════════════════════════
#  The tracked sample configuration
# ══════════════════════════════════════════════════════════════════════════════
def test_the_sample_config_exists_and_is_tracked_shaped():
    assert _SAMPLE.is_file()
    assert _SAMPLE.stat().st_size < 32_000


def test_the_sample_config_loads():
    from training_gym.training.config import load_training_config

    config = load_training_config(_SAMPLE)
    assert config.method is TrainingMethod.SFT_LORA
    assert config.base_model_id == "Qwen/Qwen3-0.6B"
    assert (config.epochs, config.batch_size, config.gradient_accumulation_steps) == \
        (1, 1, 8)
    assert config.max_sequence_length == 512
    assert config.learning_rate == 0.0002
    assert config.seed == 42
    assert (config.lora.rank, config.lora.alpha, config.lora.dropout) == (8, 16, 0.05)
    assert config.trust_remote_code is False
    assert config.model_download_policy.value == "deny"


def test_the_sample_config_carries_placeholders_and_no_plausible_fake_digest():
    body = json.loads(_SAMPLE.read_text(encoding="utf-8"))
    assert body["base_model_revision"] == "REQUIRED_IMMUTABLE_REVISION"
    for key, value in body["dataset_reference"].items():
        if key.endswith("hash"):
            assert value.startswith("REQUIRED_"), f"{key} looks like a real digest"


def test_the_sample_config_is_not_executable(tmp_path, capsys):
    """A template must report insufficient evidence, not plan a run."""
    assert cli.main(["--config", str(_SAMPLE), "--dataset-root", str(tmp_path),
                     "--output-root", str(tmp_path), "--json"]) == cli.EXIT_REFUSED
    body = payload(capsys)
    assert body["feasibility"]["verdict"] == "insufficient_evidence"
    assert body["plan"]["is_executable"] is False


def test_the_sample_config_contains_no_absolute_path_or_secret():
    text = _SAMPLE.read_text(encoding="utf-8")
    assert "Users" not in text
    assert "hf_" not in text
    assert "C:\\" not in text
    assert "/home/" not in text


def test_generated_training_output_is_gitignored():
    """A run directory or an adapter must never become a tracked file."""
    ignore = _APP_ROOT / "training" / ".gitignore"
    rules = ignore.read_text(encoding="utf-8")
    for pattern in ("runs/", "adapters/", "logs/", "configs/*.json"):
        assert pattern in rules
    assert "!configs/qwen3-0.6b-lora-smoke.json" in rules


def test_the_training_gym_package_is_not_shipped_in_the_distribution():
    """The planner is repository tooling; it is not part of `pip install jarvis`."""
    import tomllib

    with (_APP_ROOT / "pyproject.toml").open("rb") as fh:
        packages = tomllib.load(fh)["tool"]["setuptools"]["packages"]
    assert "training_gym" not in packages
    assert "scripts" not in packages
