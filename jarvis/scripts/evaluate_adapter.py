"""scripts/evaluate_adapter.py — V69 M62 S3C: the safe front end to adapter evaluation.

WHAT THIS COMMAND DOES BY DEFAULT
---------------------------------
Reads a config, verifies the references it names, builds a plan, prints it, and stops.
It creates no output, contacts no network, downloads nothing, installs nothing, runs no
model and writes no registry — not because a flag defaults to off, but because those code
paths do not exist in this front end.

WHAT ``--execute`` DOES TODAY
-----------------------------
Refuses. A live evaluation needs a cached model at an immutable revision, a verified
adapter, the training dependencies installed, and an ``EVAL:<plan-hash>`` token for the
plan recomputed from the CURRENT state of the world. On a host where any of those is
missing it says which, and stops. There is no production flag that selects the fake
backend: tests inject it directly, which is the only way it is reachable.

OUTPUT DISCIPLINE
-----------------
Bounded, scrubbed of host paths, no credentials, no tracebacks unless asked. A non-zero
exit is a refusal with a categorised code, so a wrapper can tell "the config is wrong"
from "the weights are not here".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_CONFIG = 10
EXIT_REFERENCES = 11
EXIT_TASK_PACK = 12
EXIT_DEPENDENCY = 13
EXIT_HARDWARE = 14
EXIT_CONFIRMATION = 15
EXIT_REPLAY = 16
EXIT_MODEL_ACCESS = 17
EXIT_BACKEND = 18
EXIT_ARTIFACT = 19
EXIT_REPORT = 20
EXIT_INTERNAL = 21

DEFAULT_OUTPUT_ROOT = os.path.join(_ROOT, "evaluation")
DEFAULT_DATASET_ROOT = os.path.join(_ROOT, "training_gym_datasets")
DEFAULT_TRAINING_ROOT = os.path.join(_ROOT, "training")

MAX_OUTPUT_CHARS = 20_000


def _scrub(text: object) -> str:
    """Remove anything that identifies the host before it reaches a terminal.

    An operator pastes this output into a ticket. The home directory, the username and
    the absolute repository path have no business travelling with it.
    """
    body = str(text or "")
    for root in (_ROOT, os.path.expanduser("~")):
        if root:
            for form in (root, root.replace("\\", "/"), root.replace("\\", "\\\\")):
                if form:
                    body = body.replace(form, "<root>")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user and len(user) > 2:
        body = body.replace(user, "<user>")
    return body[:MAX_OUTPUT_CHARS]


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(_scrub(json.dumps(payload, indent=2, sort_keys=True, default=str)))
        return
    for key, value in payload.items():
        if isinstance(value, (list, tuple)):
            print(f"{key}:")
            for item in list(value)[:40]:
                print(f"  - {_scrub(item)}")
            if len(value) > 40:
                print(f"  ... and {len(value) - 40} more")
        elif isinstance(value, dict):
            print(f"{key}:")
            for name, item in sorted(value.items())[:40]:
                print(f"  {name}: {_scrub(item)}")
        else:
            print(f"{key}: {_scrub(value)}")


def _fail(message: str, *, as_json: bool, code: int = EXIT_REFUSED) -> int:
    _emit({"status": "refused", "reason": _scrub(message), "exit_code": code},
          as_json=as_json)
    return code


def _exit_codes() -> dict:
    return {name.lower()[5:]: value for name, value in sorted(globals().items())
            if name.startswith("EXIT_")}


# ══════════════════════════════════════════════════════════════════════════════
#  Loading
# ══════════════════════════════════════════════════════════════════════════════
def _load(args):
    from training_gym.evaluation.config import load_config
    return load_config(args.config)


def _references(config, args):
    """Build and verify both sides of the comparison. Reaches disk, loads no model."""
    from training_gym.evaluation.references import (
        base_reference_from_identity,
        build_adapter_reference,
        verify_reference_pair,
    )
    from training_gym.training.model_identity import probe_cache

    identity = config.baseline_model
    cache_root = args.model_cache_root or None
    if cache_root:
        status, evidence = probe_cache(identity.model_id, cache_root=cache_root)
        identity = type(identity)(
            provider=identity.provider, model_id=identity.model_id,
            revision=identity.revision, parameters_b=identity.parameters_b,
            family=identity.family, tokenizer_id=identity.tokenizer_id,
            tokenizer_revision=identity.tokenizer_revision,
            cache_status=status, cache_evidence=evidence,
            license_reference=identity.license_reference)
    baseline = base_reference_from_identity(identity)

    run_directory = os.path.join(args.training_root, "runs",
                                 config.candidate_adapter.run_id)
    adapter = build_adapter_reference(
        run_directory, lora={}, base_model_id=identity.model_id)
    pair = verify_reference_pair(baseline, adapter)
    return baseline, adapter, pair


def _check_references(args) -> int:
    try:
        config = _load(args)
        baseline, adapter, pair = _references(config, args)
    except Exception as exc:  # noqa: BLE001 — the message IS the answer
        return _fail(str(exc), as_json=args.json, code=EXIT_REFERENCES)
    _emit({
        "status": "checked",
        "evaluation_id": config.evaluation_id,
        "baseline_model_id": baseline.base_model_id,
        "baseline_revision_kind": baseline.revision_kind.value,
        "baseline_reference_hash": baseline.reference_hash(),
        "candidate_run_id": adapter.run_id,
        "candidate_reference_hash": adapter.reference_hash(),
        "adapter_artifacts_reverified": adapter.artifact_verified,
        "pair_ok": pair.ok,
        "blockers": list(pair.blockers),
        "live_execution_blockers": list(baseline.live_blockers()),
    }, as_json=args.json)
    return EXIT_OK if pair.ok else EXIT_REFERENCES


def _check_dependencies(args) -> int:
    from training_gym.training.dependencies import build_dependency_report
    from training_gym.training.config import DependencyProfile
    report = build_dependency_report(profile=DependencyProfile.TRAINING)
    _emit({"status": "checked", "profile": "training", "ready": report.ready,
           "blockers": list(report.blockers()),
           "dependency_report_hash": report.report_hash(),
           "note": "reported by probing metadata; nothing was imported and nothing "
                   "was installed"},
          as_json=args.json)
    return EXIT_OK if report.ready else EXIT_DEPENDENCY


def _check_hardware(args) -> int:
    from training_gym.training.environment import HardwareCapabilityReport
    report = HardwareCapabilityReport.detect(output_root=args.output_root)
    _emit({"status": "checked", "hardware_report_hash": report.report_hash(),
           "cuda_available": report.cuda_available,
           "supported_precisions": list(report.supported_precisions),
           "note": "a capability probe, never a benchmark"},
          as_json=args.json)
    return EXIT_OK


def _check_task_pack(args) -> int:
    """Report what the pack WOULD contain, from the dataset manifest alone.

    Deliberately does not materialise held-out records: this is a shape check, and a
    command that printed task material would put held-out prompts on a terminal.
    """
    try:
        config = _load(args)
        from training_gym.datasets.manifests import load_manifest
        manifest = load_manifest(root=args.dataset_root,
                                 dataset_id=config.dataset.dataset_id,
                                 dataset_version=config.dataset.dataset_version)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_TASK_PACK)
    counts = {split.value: len(manifest.candidate_ids_in(split))
              for split in config.splits.splits}
    missing = [s.value for s, n in counts.items() if not n]
    _emit({"status": "checked", "dataset_manifest_hash": manifest.manifest_hash(),
           "counts_by_split": counts,
           "splits_with_no_task": missing,
           "eligibility_blockers": list(config.eligibility_blockers()),
           "note": "counts only; held-out task material is never printed"},
          as_json=args.json)
    return EXIT_OK if not missing else EXIT_TASK_PACK


# ══════════════════════════════════════════════════════════════════════════════
#  Planning
# ══════════════════════════════════════════════════════════════════════════════
def _plan(args):
    """Build the plan from the current state of the world. Loads no model."""
    from training_gym.evaluation.plan import (
        EXPECTED_EVALUATION_FILES,
        EvaluationPlan,
        check_output_root,
        output_root_id,
        plan_state_sequence,
    )
    from training_gym.evaluation.runner import ORDER_POLICY_BALANCED
    from training_gym.training.config import DependencyProfile
    from training_gym.training.dependencies import build_dependency_report
    from training_gym.training.environment import HardwareCapabilityReport

    config = _load(args)
    baseline, adapter, pair = _references(config, args)
    dependencies = build_dependency_report(profile=DependencyProfile.TRAINING)
    hardware = HardwareCapabilityReport.detect(output_root=args.output_root)

    from training_gym.datasets.manifests import load_manifest
    manifest = load_manifest(root=args.dataset_root,
                             dataset_id=config.dataset.dataset_id,
                             dataset_version=config.dataset.dataset_version)
    counts = {split: len(manifest.candidate_ids_in(split))
              for split in config.splits.splits}
    task_count = sum(counts.values())

    blockers = list(pair.blockers)
    blockers.extend(config.eligibility_blockers())
    blockers.extend(check_output_root(args.output_root, config.evaluation_id,
                                      config.evaluation_generation))
    if not task_count:
        blockers.append("the selected splits contribute no task")
    warnings = list(pair.warnings)
    warnings.extend(dependencies.blockers())

    def shard(split_name: str) -> str:
        entry = next((s for s in config.splits.splits if s.value == split_name), None)
        found = manifest.shard_for(entry) if entry else None
        return found.sha256_file if found else ""

    plan = EvaluationPlan(
        evaluation_id=config.evaluation_id, generation=config.evaluation_generation,
        evaluation_config_hash=config.config_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=adapter.reference_hash(),
        tokenizer_identity_hash=baseline.tokenizer_identity_hash,
        # The pack and the store are built at execution time from the verified shards;
        # the plan binds the shard digests they would be derived from.
        task_pack_hash=hashlib.sha256(
            json.dumps({"dataset": manifest.manifest_hash(),
                        "counts": {k.value: v for k, v in counts.items()}},
                       sort_keys=True).encode("utf-8")).hexdigest(),
        hidden_target_store_hash=hashlib.sha256(
            manifest.manifest_hash().encode("utf-8")).hexdigest(),
        validation_manifest_hash=shard("validation"),
        hidden_evaluation_manifest_hash=shard("hidden_evaluation"),
        security_regression_manifest_hash=shard("security_regression"),
        adversarial_manifest_hash=shard("adversarial"),
        dataset_manifest_hash=manifest.manifest_hash(),
        generation_policy_hash=config.generation.policy_hash(),
        grader_policy_hash=config.policies.graders.policy_hash(),
        metric_policy_hash=config.policies.metrics.policy_hash(),
        statistical_policy_hash=config.policies.statistics.policy_hash(),
        gate_policy_hash=config.policies.gates.policy_hash(),
        family_policy_hash=config.policies.families.policy_hash(),
        resource_policy_hash=config.policies.resources.policy_hash(),
        dependency_report_hash=dependencies.report_hash(),
        hardware_report_hash=hardware.report_hash(),
        order_policy=ORDER_POLICY_BALANCED,
        order_assignment_hash=hashlib.sha256(
            f"{ORDER_POLICY_BALANCED}:{config.seed}".encode()).hexdigest(),
        expected_output_root_id=output_root_id(args.output_root),
        expected_task_count=max(1, task_count),
        expected_baseline_generations=max(1, task_count),
        expected_candidate_generations=max(1, task_count),
        expected_grader_executions=max(1, task_count) * 6,
        expected_files=EXPECTED_EVALUATION_FILES,
        expected_state_transitions=plan_state_sequence(awaiting_confirmation=True),
        backend_id="transformers_peft", created_at_utc=config.created_at_utc,
        warnings=tuple(dict.fromkeys(warnings)), blockers=tuple(dict.fromkeys(blockers)))
    return config, baseline, adapter, plan


def _dry_run(args) -> int:
    try:
        config, baseline, adapter, plan = _plan(args)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_CONFIG)
    _emit({
        "status": "dry_run",
        "evaluation_id": config.evaluation_id,
        "generation": config.evaluation_generation,
        "plan_hash": plan.plan_hash(),
        "is_executable": plan.is_executable,
        "expected_task_count": plan.expected_task_count,
        "blockers": list(plan.blockers),
        "warnings": list(plan.warnings),
        "expected_effects": plan.expected_effects(),
        "confirmation_required": plan.confirmation_token(),
        "note": ("nothing was executed. No model was loaded, no token was generated, "
                 "no file was written and no registry was touched"),
    }, as_json=args.json)
    return EXIT_OK if plan.is_executable else EXIT_REFUSED


def _print_plan(args) -> int:
    try:
        _, _, _, plan = _plan(args)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_CONFIG)
    _emit(plan.to_record(), as_json=args.json)
    return EXIT_OK if plan.is_executable else EXIT_REFUSED


# ══════════════════════════════════════════════════════════════════════════════
#  Execution — refuses until every gate is genuinely satisfied
# ══════════════════════════════════════════════════════════════════════════════
def _execute(args) -> int:
    """The production path. Refuses unless everything a live run needs is present.

    Deliberately verifies in this order: plan, blockers, confirmation, replay, then
    dependencies and cache. The confirmation is checked against the plan RECOMPUTED from
    the current inputs, never the one an operator was shown — that is what makes a token
    stop authorising a run whose world has changed.
    """
    from training_gym.evaluation.plan import (
        EvaluationConfirmationRejected,
        check_evaluation_confirmation,
    )
    from training_gym.evaluation.store import is_plan_consumed

    try:
        config, baseline, adapter, plan = _plan(args)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_CONFIG)

    if plan.blockers:
        return _fail(
            "this plan is not executable: " + "; ".join(plan.blockers[:5]),
            as_json=args.json, code=EXIT_REFUSED)
    try:
        check_evaluation_confirmation(args.confirm, plan)
    except EvaluationConfirmationRejected as exc:
        return _fail(str(exc), as_json=args.json, code=EXIT_CONFIRMATION)

    try:
        if is_plan_consumed(args.output_root, plan.plan_hash()):
            return _fail(
                f"plan {plan.plan_hash()[:12]} has already started an evaluation; "
                f"re-plan at a new generation to obtain a new token",
                as_json=args.json, code=EXIT_REPLAY)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_REPLAY)

    from training_gym.training.config import DependencyProfile
    from training_gym.training.dependencies import build_dependency_report
    dependencies = build_dependency_report(profile=DependencyProfile.TRAINING)
    if not dependencies.ready:
        return _fail(
            "the training profile is not installed in this environment: "
            + "; ".join(dependencies.blockers()[:5])
            + ". Install it yourself in a dedicated environment; this command never "
              "installs anything",
            as_json=args.json, code=EXIT_DEPENDENCY)

    if baseline.live_blockers():
        return _fail(
            "the baseline model is not ready for a live evaluation: "
            + "; ".join(baseline.live_blockers())
            + ". This command never downloads weights",
            as_json=args.json, code=EXIT_MODEL_ACCESS)

    # Reached only when a real cached model, a verified adapter, the installed
    # dependencies and a valid unspent token are all genuinely present. No such host
    # exists in this milestone, and this stage does not pretend otherwise.
    return _fail(
        f"the live evaluation path for plan {plan.plan_hash()[:12]} is not enabled in "
        f"this build. Every precondition this command can check has passed; running a "
        f"real base-versus-adapter comparison is the next milestone's boundary and has "
        f"never been performed on this repository. Nothing was loaded and nothing was "
        f"written",
        as_json=args.json, code=EXIT_BACKEND)


# ══════════════════════════════════════════════════════════════════════════════
#  Verification and proposal
# ══════════════════════════════════════════════════════════════════════════════
def _verify_report(args) -> int:
    from training_gym.evaluation.reports import verify_report_payload
    try:
        with open(args.verify_report, encoding="utf-8") as handle:
            payload = json.load(handle)
        record = verify_report_payload(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_REPORT)
    _emit({"status": "verified", "report_hash": record["report_hash"],
           "evaluation_id": record.get("evaluation_id"),
           "empirical_status": record.get("empirical_status"),
           "eligibility": record.get("eligibility", {}).get("eligibility"),
           "measured_pairs": record.get("measured_pairs"),
           "task_count": record.get("task_count"),
           "blockers": list(record.get("blockers", ()))[:20]},
          as_json=args.json)
    return EXIT_OK


def _verify_generation(args) -> int:
    from training_gym.evaluation.artifacts import verify_evaluation_generation
    problems = verify_evaluation_generation(args.verify_generation)
    _emit({"status": "verified" if not problems else "refused",
           "problems": list(problems)}, as_json=args.json)
    return EXIT_OK if not problems else EXIT_ARTIFACT


def _proposal(args) -> int:
    from training_gym.evaluation.registry_bridge import build_proposal
    try:
        config = _load(args)
        baseline, adapter, _ = _references(config, args)
        with open(args.proposal, encoding="utf-8") as handle:
            report_record = json.load(handle)
        proposal = build_proposal(
            report_record=report_record, adapter_reference=adapter,
            baseline_reference=baseline, created_at_utc=config.created_at_utc)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc), as_json=args.json, code=EXIT_REPORT)
    _emit({**proposal.to_record(),
           "note": "no registry was written, nothing was promoted and nothing was "
                   "activated"},
          as_json=args.json)
    return EXIT_OK if proposal.proposal_status.value == "ready_for_registry_review" \
        else EXIT_REFUSED


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.evaluate_adapter",
        description=("Compare a pinned base model against the same base model plus a "
                     "verified adapter. Dry-run by default; loads no model, downloads "
                     "nothing, installs nothing and writes no registry."))
    parser.add_argument("--config", default="",
                        help="path to an evaluation config JSON")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable output")
    parser.add_argument("--dataset-root", default=DEFAULT_DATASET_ROOT,
                        help="root holding versioned dataset manifests")
    parser.add_argument("--training-root", default=DEFAULT_TRAINING_ROOT,
                        help="root holding completed training runs")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                        help="root that would hold evaluation generations")
    parser.add_argument("--model-cache-root", default="",
                        help="a reviewed local model cache; never guessed, never fetched")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="build and summarise the plan (the default)")
    mode.add_argument("--print-plan", action="store_true",
                      help="print the full plan record and its confirmation token")
    mode.add_argument("--check-references", action="store_true",
                      help="verify the baseline identity and re-verify the adapter")
    mode.add_argument("--check-task-pack", action="store_true",
                      help="report split counts; never prints held-out material")
    mode.add_argument("--check-dependencies", action="store_true",
                      help="probe the training profile without importing it")
    mode.add_argument("--check-hardware", action="store_true",
                      help="probe hardware capability; never a benchmark")
    mode.add_argument("--execute", action="store_true",
                      help="attempt a live evaluation (refuses unless every "
                           "precondition is genuinely met)")
    mode.add_argument("--verify-report", default="",
                      help="re-derive a persisted report's digest")
    mode.add_argument("--verify-generation", default="",
                      help="re-derive every digest in a generation directory")
    mode.add_argument("--proposal", default="",
                      help="produce a non-effectful Model Registry proposal from a "
                           "report")
    parser.add_argument("--confirm", default="",
                        help="EVAL:<full-plan-hash>; required by --execute")
    parser.add_argument("--exit-codes", action="store_true",
                        help="print the exit-code vocabulary and stop")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.exit_codes:
        _emit(_exit_codes(), as_json=args.json)
        return EXIT_OK
    if args.verify_report:
        return _verify_report(args)
    if args.verify_generation:
        return _verify_generation(args)
    if not args.config:
        parser.print_usage()
        print("error: --config is required")
        return EXIT_USAGE
    if args.proposal:
        return _proposal(args)

    try:
        if args.check_references:
            return _check_references(args)
        if args.check_task_pack:
            return _check_task_pack(args)
        if args.check_dependencies:
            return _check_dependencies(args)
        if args.check_hardware:
            return _check_hardware(args)
        if args.execute:
            return _execute(args)
        if args.print_plan:
            return _print_plan(args)
        return _dry_run(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return _fail("interrupted", as_json=args.json, code=EXIT_REFUSED)
    except Exception as exc:  # noqa: BLE001 — a traceback would publish host paths
        return _fail(f"{type(exc).__name__}: {exc}", as_json=args.json,
                     code=EXIT_INTERNAL)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
