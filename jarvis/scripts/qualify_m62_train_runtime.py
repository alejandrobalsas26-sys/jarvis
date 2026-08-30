"""scripts/qualify_m62_train_runtime.py — V69 M63 S4B: the runtime, and the plan.

WHAT THIS IS
------------
Two deterministic, body-free reports and nothing else:

``--runtime-report``
    WHAT this machine would train in — interpreter identity, the canonical dependency
    versions actually importable, platform and architecture, where the packages live
    (classified, never as an absolute path), and the material identities the run would
    bind. Hashed, so a later session can ask "is this the runtime that produced that
    artefact?" and get an answer rather than a story.

``--plan``
    The immutable TRAIN PLAN: the frozen source commit, the candidate and its parent, the
    ruling and its digest, the single axis and both its values, every hyperparameter that
    is NOT allowed to move, the dataset and base-model identities, the runtime digest, the
    configuration digest, the output root, the expected artefact rules, and the canonical
    ``plan_hash`` a ``TRAIN:`` token would bind.

WHAT IT NEVER DOES
------------------
  * **It is TOKEN-SILENT.** It reads ``plan_hash()`` and never calls
    ``TrainingPlan.to_record()``, ``expected_effects()`` or ``confirmation_token()``, so
    no ``TRAIN:<hash>`` string is ever constructed, printed or written. The generator's
    ``--plan`` path has the same property; the executor's ``--dry-run`` and
    ``--print-plan`` deliberately do NOT, which is exactly why this exists.
  * **It does not train, evaluate or load model weights.** It reads dependency VERSIONS
    from installed distribution metadata. It constructs no model, no tokenizer, no
    optimizer and no adapter, and it reads no checkpoint.
  * **It creates no authority and consumes none.** It writes no ledger line, no receipt
    and no run directory, and it increments no attempt counter.
  * **It writes nothing unless asked.** Output goes to stdout; ``--emit`` writes one file
    to a path the caller names, which by invariant is a gitignored runtime location.
  * **It records no private path.** Machine-local roots are normalised to symbolic
    labels, so the report is portable and the digest is not a fact about one home
    directory.

WHY IT IS NOT A STATE-BEARING PRODUCTION PATH
---------------------------------------------
It configures nothing. The configuration a plan is derived from comes from
``build_quality_training_config.py``, which IS state-bearing; this module reads that
configuration and reports on it. Changing this file cannot change what would be trained,
so it does not oblige a new control-plane generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

SCHEMA_RUNTIME = "m62.train_runtime_report.1"
SCHEMA_PLAN = "m62.train_plan.1"

#: The packages whose versions identify the training backend. The first three are the
#: ones candidate 004's sealed receipt records, so they are the ones an equality against
#: history is available for; the rest are recorded because S3J.1 resolved them and a
#: silently different one is a silently different backend.
CANONICAL_PACKAGES = (
    "torch", "transformers", "peft", "datasets", "trl", "accelerate",
    "safetensors", "tokenizers", "sentencepiece", "numpy", "jsonschema",
    "huggingface_hub",
)

#: Where the pins live. Read as EVIDENCE of what was reviewed, never installed from here.
PINNED_REQUIREMENTS = "jarvis/requirements/training-m62-pinned.txt"

#: The tracked ruling candidate 005's design rests on.
OPERATOR_RULING = "state/m62/rulings/0002-s4b-candidate005-learning-rate.json"


def canonical_json(payload: object) -> str:
    """The one serialisation these digests are taken over — the control plane's."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2,
                      allow_nan=False) + "\n"


def digest(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def normalise(path: "Path | str", repo_root: Path, *, resolve: bool = True) -> str:
    """A machine-local path as a SYMBOLIC location, never as a private absolute path.

    A digest taken over ``/home/<someone>/...`` is a fact about one home directory and
    reproduces nowhere; it is also exactly the shape the control plane's private-path
    scan refuses. Classification is enough for the question these reports answer — is
    this environment inside the project, and does the interpreter own its packages — and
    it is all they are allowed to carry.
    """
    target = Path(path).resolve() if resolve else Path(path).absolute()
    try:
        return f"<REPO>/{target.relative_to(repo_root).as_posix()}"
    except ValueError:
        return f"<OUTSIDE_REPO>/{target.name}"


def package_versions() -> dict:
    """Version strings only, from distribution metadata. Imports no framework."""
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str] = {}
    for name in CANONICAL_PACKAGES:
        try:
            out[name] = version(name.replace("_", "-"))
        except PackageNotFoundError:
            out[name] = "ABSENT"
    return out


def pinned_versions(repo_root: Path) -> dict:
    path = repo_root / PINNED_REQUIREMENTS
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, value = line.split("==", 1)
        out[name.strip().replace("-", "_")] = value.strip()
    return out


def interpreter_identity(repo_root: Path) -> dict:
    """Who is running, and whether the packages on the path belong to them."""
    site_packages = sorted({p for p in sys.path if "site-packages" in p})
    tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    prefix = Path(sys.prefix).resolve()
    return {
        # NOT resolved: `.venv/bin/python` is a symlink to the base interpreter, and
        # resolving it would report the environment as OUTSIDE_REPO while `sys.prefix`
        # said otherwise. Both facts are recorded, separately, because they are two
        # different facts -- which environment is running, and which interpreter it was
        # built from.
        "executable": normalise(sys.executable, repo_root, resolve=False),
        "base_interpreter": normalise(sys.executable, repo_root),
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "implementation": platform.python_implementation(),
        "prefix": normalise(prefix, repo_root),
        "base_prefix": normalise(sys.base_prefix, repo_root),
        "in_virtualenv": sys.prefix != sys.base_prefix,
        "environment_class": (
            "PROJECT_LOCAL_VENV"
            if sys.prefix != sys.base_prefix and prefix.is_relative_to(repo_root)
            else "OTHER"),
        "site_packages": [normalise(p, repo_root) for p in site_packages],
        # THE drift check, and the one that actually breaks installs: packages installed
        # for 3.13 are invisible to a 3.14 interpreter, and a venv whose interpreter was
        # replaced underneath it looks healthy until the first import fails.
        "interpreter_owns_site_packages": bool(site_packages) and all(
            tag in p for p in site_packages),
    }


def runtime_report(repo_root: Path, dataset_root: Path, candidate: str) -> dict:
    from scripts import build_quality_training_config as generator
    from training_gym.datasets.manifests import load_manifest

    manifest = load_manifest(
        root=dataset_root, dataset_id=generator.TRAINING_DATASET_ID,
        dataset_version=generator.CANDIDATES[candidate]["dataset_version"])
    installed = package_versions()
    pinned = pinned_versions(repo_root)
    return {
        "schema_version": SCHEMA_RUNTIME,
        "candidate": generator.CANDIDATES[candidate]["run_id"],
        "python": interpreter_identity(repo_root),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
        "packages": {
            "installed": installed,
            "pinned": pinned,
            "pins_satisfied": {
                name: installed.get(name) == value for name, value in pinned.items()},
            "all_pins_satisfied": bool(pinned) and all(
                installed.get(name) == value for name, value in pinned.items()),
            "pin_source": PINNED_REQUIREMENTS,
        },
        "base_model": {
            "model_id": generator.BASE_MODEL_ID,
            "revision": generator.BASE_MODEL_REVISION,
            "tokenizer_id": generator.BASE_MODEL_ID,
            "tokenizer_revision": generator.BASE_MODEL_REVISION,
            "revision_kind": "immutable_commit",
        },
        "dataset": {
            "dataset_id": generator.TRAINING_DATASET_ID,
            "version": generator.CANDIDATES[candidate]["dataset_version"],
            "manifest_hash": manifest.manifest_hash(),
            "record_count": manifest.total_records,
            "counts": dict(sorted(manifest.counts().items())),
        },
        "effects": {
            "model_weight_loads": 0,
            "optimizer_starts": 0,
            "training_steps": 0,
            "adapter_artifacts": 0,
            "installed_anything": False,
            "token_materialised": False,
        },
    }


def train_plan(repo_root: Path, dataset_root: Path, output_root: Path,
               model_cache_root: "Path | None", candidate: str,
               source_head: str) -> dict:
    """The immutable plan. Token-silent: ``plan_hash()`` only, never ``to_record()``."""
    from scripts import build_quality_training_config as generator

    reference_key, declared = generator.CANDIDATE_SINGLE_AXIS[candidate]
    option = generator.CANDIDATE_OPTION[candidate]
    reference_option = generator.CANDIDATE_OPTION[reference_key]
    axis = sorted(declared)[0]

    config, result = generator.plan_for(
        option, dataset_root=dataset_root, output_root=output_root,
        model_cache_root=model_cache_root, candidate=candidate)
    plan = result.plan

    ruling = json.loads((repo_root / OPERATOR_RULING).read_text(encoding="utf-8"))
    runtime = runtime_report(repo_root, dataset_root, candidate)

    # Every dial that is NOT the axis, asserted equal to the reference's rather than
    # merely listed. A plan that recorded an "unchanged" dial which had in fact moved
    # would be binding the wrong experiment under the right name.
    unchanged = {dial: generator.OPTIONS[option][dial]
                 for dial in generator.OPTION_DIALS if dial != axis}
    drifted = [d for d, v in unchanged.items()
               if generator.OPTIONS[reference_option][d] != v]
    if drifted:
        raise ValueError(
            f"dial(s) {sorted(drifted)} differ from candidate {reference_key}'s and are "
            f"not the declared axis; refusing to build a plan for a second axis")

    run_id = generator.CANDIDATES[candidate]["run_id"]
    return {
        "schema_version": SCHEMA_PLAN,
        "train_plan_source_head": source_head,
        "candidate": run_id,
        "parent": generator.CANDIDATES[reference_key]["run_id"],
        "human_ruling": {
            "ruling_id": ruling["ruling_id"],
            "ruling_phrase_sha256": ruling["ruling_phrase_sha256"],
            "scope": ruling["scope"],
            "record": OPERATOR_RULING,
        },
        "science": {
            "primary_axis": axis,
            "reference_value": generator.format_learning_rate(
                generator.OPTIONS[reference_option][axis]),
            "ruled_value": generator.format_learning_rate(
                generator.OPTIONS[option][axis]),
            "scientific_diff_count": len(generator.single_axis_diff(candidate)),
            "unchanged_dials": dict(sorted(unchanged.items())),
            "seed": config.seed,
            "reasoning_policy": config.reasoning_policy.value,
            "validation_strategy": config.validation_strategy.value,
            "validation_is_held_out_eligibility_evidence": False,
            "precision": config.precision_policy.value,
            "device": config.device_policy.value,
            "lora": {
                "rank": config.lora.rank, "alpha": config.lora.alpha,
                "dropout": config.lora.dropout,
                "target_policy": config.lora.target_policy.value,
            },
        },
        "material": {
            "dataset_id": runtime["dataset"]["dataset_id"],
            "dataset_version": runtime["dataset"]["version"],
            "dataset_manifest_hash": runtime["dataset"]["manifest_hash"],
            "dataset_record_count": runtime["dataset"]["record_count"],
            "dataset_reference_hash": config.dataset_reference.reference_hash(),
            "train_shard_hash": config.dataset_reference.train_shard_hash,
            "validation_shard_hash": config.dataset_reference.validation_shard_hash,
            "base_model_id": config.base_model_id,
            "base_model_revision": config.base_model_revision,
            "tokenizer_id": config.tokenizer_id,
            "tokenizer_revision": config.tokenizer_revision,
        },
        "runtime": {
            "report_sha256": digest(runtime),
            "python_version": runtime["python"]["version"],
            "packages": runtime["packages"]["installed"],
            "all_pins_satisfied": runtime["packages"]["all_pins_satisfied"],
            "interpreter_owns_site_packages":
                runtime["python"]["interpreter_owns_site_packages"],
        },
        "config": {
            "config_hash": config.config_hash(),
            "config_hash_is_root_bound": True,
            "output_root": normalise(output_root, repo_root),
            "output_root_id": config.output_root_id,
        },
        "execution": {
            # The canonical hash. This is the value a `TRAIN:` token binds and the value
            # the executor recomputes; it is printed, and the token is NOT derived here.
            "plan_hash": plan.plan_hash(),
            "plan_is_executable": plan.is_executable,
            "plan_blockers": list(plan.blockers),
            "plan_warnings": list(plan.warnings),
            "selected_device": plan.selected_device,
            "selected_precision": plan.selected_precision,
            "optimizer_steps": config.max_steps,
            "epochs": config.epochs,
            "effective_batch_size": config.effective_batch_size,
            "dependency_ready": result.dependencies.ready,
            "dependency_blockers": list(result.dependencies.blockers()),
            "model_cache_status": result.identity.cache_status.value,
            "dataset_problems": list(result.dataset.problems),
        },
        "expected_artifacts": {
            "run_directory": f"<OUTPUT_ROOT>/runs/{run_id}",
            "adapter_weights": "adapter_model.safetensors",
            "adapter_manifest": "adapter-manifest.json",
            "no_pickle": True,
            "checkpoint_directories": 0,
            "expected_receipt": f"state/m62/receipts/{run_id}.train.json",
        },
        "authority": {
            "form": "TRAIN:<plan-hash>",
            "single_use": True,
            "created_here": False,
            "consumed_here": False,
            "token_materialised": False,
        },
    }


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify the M62 training runtime and derive the immutable train "
                    "plan. Never trains, never loads weights, never materialises a "
                    "TRAIN token.")
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--model-cache-root", default="")
    parser.add_argument("--candidate", default="005")
    parser.add_argument("--source-head", default="",
                        help="the frozen commit the plan is derived from")
    parser.add_argument("--runtime-report", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--emit", default="",
                        help="write the report to this path; a runtime artefact, so it "
                             "must stay outside the repository")
    args = parser.parse_args(argv)

    repo_root = _ROOT.parent
    try:
        dataset_root = Path(args.dataset_root)
        output_root = Path(args.output_root)
        cache_root = Path(args.model_cache_root) if args.model_cache_root else None
        if args.plan:
            payload = train_plan(repo_root, dataset_root, output_root, cache_root,
                                 args.candidate, args.source_head)
            payload["plan_document_sha256"] = digest(payload)
        elif args.runtime_report:
            payload = runtime_report(repo_root, dataset_root, args.candidate)
            payload["runtime_report_sha256"] = digest(payload)
        else:
            parser.error("choose --runtime-report or --plan")
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        print(json.dumps({"status": "refused",
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1

    text = canonical_json({"status": "ok", **payload})
    if args.emit:
        destination = Path(args.emit)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
