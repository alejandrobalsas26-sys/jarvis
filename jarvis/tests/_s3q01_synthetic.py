"""Synthetic scaffolding for the V69 M62 S3Q.0.1 portable-evidence closure.

WHAT S3Q.0 ALREADY SUPPLIES, AND WHAT THIS ADDS
-----------------------------------------------
``_s3q0_synthetic`` authors a corpus, drives the REAL execution path over it and leaves a
completed generation directory plus a durable ledger. That was everything the ``.1``
receipt bound.

The ``.2`` receipt binds four things that did not exist as synthetic evidence:

  * an ADAPTER on disk -- real ``safetensors`` bytes, a real ``adapter-manifest.json``,
    re-verified by the production ``verify_completed_run``. Without one there is no
    ``adapter_sha256`` to require, and FINDING A could only be tested by asserting that
    a blank is a blank;
  * a TRAINING RECEIPT sealing exactly those adapter bytes, so the candidate identity has
    a root that is not a caller string;
  * an evaluation CONFIG, so the direct policy values in the receipt are re-derivable
    into the generation policy hash the report recorded rather than quoted beside it;
  * a GIT repository, so ``evaluation_source_commit`` can be derived from a real HEAD and
    an assertion against the wrong commit can actually be refused.

Everything here is synthetic. No eval-v4, no model, no weights are loaded: the adapter is
a structurally valid safetensors file holding four tiny LoRA tensors, which is what the
artefact verifier reads and what a receipt hashes. Nothing deserialises it.
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("scripts.build_m62_eval_receipt")

import _s3q0_synthetic as S  # noqa: E402

#: The synthetic candidate. Deliberately not the id of any real M62 candidate, so a test
#: that accidentally reached live evidence would name something impossible.
CANDIDATE_ID = "s3q01-synthetic-candidate"

#: The synthetic base model, matching the S3Q.0 fixture so the two worlds agree.
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_REVISION = S.REV
NOW = S.NOW

LORA = {"rank": 8, "alpha": 16, "dropout": 0.05,
        "target_modules": ["q_proj", "v_proj"]}

#: The eligibility-grade policy V69 M62 operator ruling H6a approved, and the one the
#: live S3Q run will use: reasoning disabled, 512 new tokens, seed 11, CPU, FP32, and a
#: CONFIGURED 300s timeout that nothing enforces (D33, open and unchanged).
LIVE_POLICY_OVERRIDES = {"seed": 11, "timeout_s": 300}


def _safetensors(path: Path) -> None:
    """A structurally valid safetensors file holding LoRA tensors and nothing else.

    Written by hand rather than by ``torch``: the artefact verifier reads the
    length-prefixed JSON index and never deserialises the payload, so producing the index
    is producing exactly the evidence it checks -- without importing a framework that
    could execute anything.
    """
    tensors = {
        "base_model.model.layers.0.self_attn.q_proj.lora_A.weight":
            {"dtype": "F32", "shape": [8, 16], "data_offsets": [0, 512]},
        "base_model.model.layers.0.self_attn.q_proj.lora_B.weight":
            {"dtype": "F32", "shape": [16, 8], "data_offsets": [512, 1024]},
        "base_model.model.layers.0.self_attn.v_proj.lora_A.weight":
            {"dtype": "F32", "shape": [8, 16], "data_offsets": [1024, 1536]},
        "base_model.model.layers.0.self_attn.v_proj.lora_B.weight":
            {"dtype": "F32", "shape": [16, 8], "data_offsets": [1536, 2048]},
    }
    header = json.dumps(tensors, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header)) + header + bytes(2048))


def build_adapter(root: Path, *, candidate_id: str = CANDIDATE_ID) -> dict:
    """A complete, verifiable adapter run directory. Returns its sealed identities."""
    from training_gym.training.artifacts import (
        ADAPTER_MANIFEST_FILE,
        ADAPTER_WEIGHTS_FILE,
        AdapterManifest,
        validate_adapter_directory,
    )
    from training_gym.training.model_identity import canonical_identity_hash

    directory = Path(root) / candidate_id
    directory.mkdir(parents=True)
    _safetensors(directory / ADAPTER_WEIGHTS_FILE)
    (directory / "adapter_config.json").write_text(json.dumps({
        "peft_type": "LORA", "base_model_name_or_path": BASE_MODEL_ID,
        "r": LORA["rank"], "lora_alpha": LORA["alpha"],
        "lora_dropout": LORA["dropout"], "target_modules": LORA["target_modules"],
        "task_type": "CAUSAL_LM"}, indent=2, sort_keys=True), encoding="utf-8")

    validation = validate_adapter_directory(directory, lora=LORA,
                                            base_model_id=BASE_MODEL_ID)
    if not validation.ok:
        raise RuntimeError(f"the synthetic adapter does not validate: "
                           f"{list(validation.problems)[:3]}")
    identity = canonical_identity_hash(
        model_id=BASE_MODEL_ID, revision=BASE_REVISION,
        tokenizer_id=BASE_MODEL_ID, tokenizer_revision=BASE_REVISION)
    manifest = AdapterManifest(
        run_id=candidate_id, run_state="completed", plan_hash="1" * 64,
        training_config_hash="2" * 64, method="sft_lora",
        backend_id="synthetic_training", backend_version="s3q01-1",
        base_model_id=BASE_MODEL_ID, base_model_revision=BASE_REVISION,
        base_model_identity_hash=identity, tokenizer_id=BASE_MODEL_ID,
        tokenizer_revision=BASE_REVISION, tokenizer_identity_hash="3" * 64,
        tokenizer_chat_template_hash="4" * 64,
        dataset_id="s3q01-synthetic-train", dataset_version="v1",
        dataset_reference_hash="5" * 64, dataset_manifest_hash="6" * 64,
        train_shard_hash="7" * 64, validation_shard_hash="8" * 64,
        hidden_evaluation_hash="9" * 64, security_regression_hash="a" * 64,
        export_manifest_hash="b" * 64, split_algorithm_version="m62.split.1",
        seed=42, lora=dict(LORA), assistant_only_loss={"enabled": True},
        package_versions={"peft": "0.20.0"}, device_category="cpu", precision="fp32",
        requested_steps=40, completed_steps=40, epochs_completed=2.0,
        train_loss=3.4, eval_loss=3.17, truncated_records=0, duration_seconds=1.0,
        files=validation.files, total_bytes=validation.total_bytes,
        tree_hash=validation.tree_hash, created_at_utc=NOW,
        chat_render_policy_hash="c" * 64)
    (directory / ADAPTER_MANIFEST_FILE).write_text(
        json.dumps(manifest.to_record(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    weights = next(f for f in manifest.files if f.name == ADAPTER_WEIGHTS_FILE)
    return {"directory": directory, "manifest": manifest,
            "candidate_id": candidate_id,
            "adapter_sha256": weights.sha256,
            "adapter_manifest_hash": manifest.manifest_hash(),
            "adapter_artifact_set_hash": manifest.tree_hash}


def adapter_reference(adapter: dict):
    """The evaluation reference for that adapter, through the production constructor."""
    from training_gym.evaluation.references import reference_from_manifest
    return reference_from_manifest(adapter["manifest"], artifact_verified=True)


def write_training_receipt(path: Path, evidence: dict, *,
                           candidate_id: str = "", **overrides) -> Path:
    """A tracked-shaped ``m62.train_receipt.1`` sealing exactly these adapter bytes.

    ``**overrides`` replaces whole top-level blocks, ``adapter`` included, which is how
    the cross-binding tests seal a digest that does not match the bytes on disk.
    """
    payload = {
        "schema_version": "m62.train_receipt.1",
        "candidate_id": candidate_id or evidence["candidate_id"],
        "plan_hash": "1" * 64,
        "training_source_commit": "e" * 40,
        "training_milestone": "S3P",
        "adapter": {
            "sha256": evidence["adapter_sha256"],
            "manifest_hash": evidence["adapter_manifest_hash"],
            "artifact_set_hash": evidence["adapter_artifact_set_hash"],
        },
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def live_generation_policy():
    """The eligibility-grade policy V69 M62 ruling H6a approved for the live S3Q run."""
    from training_gym.evaluation.generation import (
        DevicePolicy,
        PrecisionPolicy,
        eligibility_generation_policy,
    )
    return eligibility_generation_policy(
        device_policy=DevicePolicy.CPU, precision_policy=PrecisionPolicy.FP32,
        **LIVE_POLICY_OVERRIDES)


def live_config_payload(**overrides) -> dict:
    """The AUTHORING form of the config -- what an operator passes to ``--config``.

    Not ``EvaluationConfig.to_dict()``: that is the canonical hashed projection and it
    carries derived members the loader refuses. The receipt binds the file the run was
    planned from, so the fixture must produce that file and not a serialisation of the
    object it became.
    """
    payload = {
        "schema_version": "m62.1",
        "evaluation_id": "s3q0-synthetic-ceremony",
        "evaluation_generation": 1,
        "baseline_model": {
            "model_id": BASE_MODEL_ID, "revision": BASE_REVISION, "parameters_b": 0.6,
            "tokenizer_id": BASE_MODEL_ID, "tokenizer_revision": BASE_REVISION,
        },
        "candidate_adapter": {"run_id": "run-s3q0"},
        "dataset": {"dataset_id": S.DATASET_ID, "dataset_version": S.DATASET_VERSION},
        "splits": {"splits": list(S.SPLITS), "diagnostic_splits": []},
        "created_at_utc": NOW, "seed": 11,
        "generation": live_generation_policy().to_dict(),
    }
    payload.update(overrides)
    return payload


def live_config(**overrides):
    """The loaded config, through the production loader the live run uses."""
    from training_gym.evaluation.config import config_from_dict
    return config_from_dict(live_config_payload(**overrides))


def write_config(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def make_repository(root: Path) -> str:
    """A real git repository, so a derived HEAD is a derived HEAD and not a fixture."""
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(  # noqa: E731 - one-line local helper
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "s3q01@example.invalid")
    run("config", "user.name", "s3q01")
    (root / "SOURCE.md").write_text("S3Q.0.1 synthetic source tree\n", encoding="utf-8")
    run("add", "SOURCE.md")
    run("commit", "-q", "-m", "s3q01 synthetic source")
    return run("rev-parse", "HEAD").stdout.strip()


def evaluated_world(tmp_path_factory) -> dict:
    """One completed synthetic evaluation of one real synthetic adapter.

    The plan and the report bind the adapter's OWN reference hash, because the reference
    is built from the manifest rather than invented: that is what makes the receipt's
    adapter cross-binding a real check instead of two fixtures agreeing.
    """
    base = tmp_path_factory.mktemp("s3q01world")
    data = base / "data"
    S.build(data)
    adapter_evidence = build_adapter(base / "runs")
    reference = adapter_reference(adapter_evidence)

    payload = live_config_payload()
    config = live_config()
    baseline = S.make_baseline()
    identity = S.pack_identity(data, config)
    plan = S.make_plan(config, baseline, reference, identity, performs_inference=True)
    output = base / "run"
    output.mkdir()
    outcome = S.run_synthetic(data, output, config=config, plan=plan, identity=identity,
                              adapter=reference, baseline=baseline)
    if not outcome.ok:
        raise RuntimeError(f"the synthetic evaluation did not complete: "
                           f"{list(outcome.problems)[:3]}")
    return {
        "base": base, "dataset_root": data, "output_root": output,
        "directory": outcome.directory,
        "ledger": output / "evaluation_runs.jsonl",
        "adapter": adapter_evidence, "reference": reference,
        "config": config,
        "config_payload": payload,
        "config_path": write_config(base / "evaluation-config.json", payload),
        "training_receipt": write_training_receipt(
            base / "state" / "receipts" / f"{CANDIDATE_ID}.train.json",
            adapter_evidence),
        "repo_root": base,
        "head": make_repository(base),
    }
