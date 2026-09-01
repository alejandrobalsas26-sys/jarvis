#!/usr/bin/env python3
"""evaluate_reference_pair.py — V69 M62 S4E: the Protocol V4 command.

WHAT EACH MODE DOES, AND WHAT NONE OF THEM DO
----------------------------------------------
``--check-artifacts``   re-hashes both adapters from bytes and proves the common base
``--runtime-report``    the body-free runtime identity and its digest
``--print-plan``        derives the V4 plan and prints its hash. NOT the token.
``--derive-plan``       derives the plan N times in N processes and compares bytes
``--live-preflight``    everything needed to decide, and nothing that authorises
``--execute``           requires ``--confirm EVAL:<full-plan-hash>``

NOTHING BUT ``--execute`` CAN LOAD A WEIGHT OR GENERATE A TOKEN. Every other mode is
metadata: it reads digests, counts and manifests, and the model cache is probed for
PRESENCE only. There is no warmup, no smoke generation and no hello-model check
anywhere in this file.

WHY THE REFERENCE ADAPTER IS AN ARGUMENT AND NOT A CONFIG FIELD
----------------------------------------------------------------
``EvaluationConfig`` is shared with the v1-v3 protocol and names exactly one adapter.
Adding a second to it would change a structure three sealed evaluations were planned
against. So the reference arm is named on the command line and bound into the PLAN
HASH through the pairing, which is built from that adapter's own sealed training
receipt rather than from what the caller typed. Naming a different reference produces a
different plan hash, so an operator's token authorises exactly one pair.

THE TOKEN IS NEVER MATERIALISED BY A PRE-GO SURFACE
----------------------------------------------------
``--print-plan`` and ``--live-preflight`` publish ``plan_hash`` and deliberately never
print ``EVAL:<hash>``. The confirmation string is a pure function of the plan hash and
is neither secret nor unpredictable; withholding it is ceremony hygiene, so that the
string an operator types is one they assembled deliberately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_ARTIFACTS = 3
EXIT_PLAN = 4
EXIT_CONFIRMATION = 5

BACKEND_ID = "transformers_peft"
DEFAULT_DATASET_ROOT = str(_ROOT / "training_gym_datasets")
DEFAULT_TRAINING_ROOT = str(_ROOT / "training_runs")
DEFAULT_OUTPUT_ROOT = str(_ROOT / "evaluation")

#: Path CLASSES, not host paths. The plan binds these so a digest never depends on
#: whose home directory the repository happens to sit in.
RECEIPT_PATH_CLASS = "state/m62/receipts/<candidate_id>.eval.json"
ARTIFACT_PATH_CLASS = "jarvis/evaluation/evaluations/<evaluation_id>/gen-<n>/"


def _scrub(text: object) -> str:
    """Strip the home directory and the username from anything printed."""
    value = str(text)
    home = os.path.expanduser("~")
    if home and home != "/":
        value = value.replace(home, "~")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    if user and len(user) > 2:
        value = value.replace(user, "<user>")
    return value


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=_scrub))


def _fail(message: str, *, code: int = EXIT_REFUSED) -> int:
    _emit({"status": "refused", "error": _scrub(message)})
    return code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  Artefact integrity — bytes, not claims
# ══════════════════════════════════════════════════════════════════════════════
def artifact_facts(training_root: str, run_id: str, receipt_path: Path) -> dict:
    """Re-hash the adapter and read its sealed receipt. Loads no model."""
    directory = Path(training_root) / "runs" / run_id
    weights = directory / "adapter_model.safetensors"
    manifest = directory / "adapter-manifest.json"
    facts: dict = {"run_id": run_id, "directory_present": directory.is_dir(),
                   "is_symlink": directory.is_symlink()}
    if not weights.is_file():
        facts["error"] = "adapter_model.safetensors is missing"
        return facts
    facts["adapter_sha256"] = sha256_file(weights)
    facts["adapter_bytes"] = weights.stat().st_size
    facts["manifest_present"] = manifest.is_file()
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        facts["adapter_manifest_hash"] = payload.get("manifest_hash", "")
        facts["base_model_id"] = payload.get("base_model_id", "")
        facts["base_model_revision"] = payload.get("base_model_revision", "")
        facts["tokenizer_chat_template_hash"] = payload.get(
            "tokenizer_chat_template_hash", "")
    facts["training_receipt_present"] = receipt_path.is_file()
    if receipt_path.is_file():
        facts["training_receipt_sha256"] = sha256_file(receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        facts["receipt_candidate_id"] = receipt.get("candidate_id", "")
        facts["receipt_adapter_sha256"] = receipt.get("adapter", {}).get("sha256", "")
        facts["receipt_manifest_hash"] = receipt.get("adapter", {}).get(
            "manifest_hash", "")
        facts["receipt_artifact_set_hash"] = receipt.get("adapter", {}).get(
            "artifact_set_hash", "")
        base = receipt.get("base_model", {})
        facts["receipt_base_model_id"] = base.get("model_id", "")
        facts["receipt_base_revision"] = base.get("revision", "")
        facts["receipt_base_identity_hash"] = base.get("identity_hash", "")
        facts["receipt_tokenizer_chat_template_hash"] = base.get(
            "chat_template_digest", "")
    return facts


def artifact_blockers(facts: dict, *, expected_adapter: str,
                      expected_manifest: str) -> list[str]:
    """Every way the bytes on disk could fail to be the artefact that was declared."""
    problems: list[str] = []
    if facts.get("error"):
        return [f"{facts['run_id']}: {facts['error']}"]
    if facts.get("is_symlink"):
        problems.append(f"{facts['run_id']}: the run directory is a symlink; the "
                        f"artefacts verified and the artefacts loaded must be the same "
                        f"files")
    if expected_adapter and facts.get("adapter_sha256") != expected_adapter:
        problems.append(
            f"{facts['run_id']}: adapter bytes hash to "
            f"{facts.get('adapter_sha256', '')[:12]}, the control plane declares "
            f"{expected_adapter[:12]}")
    if expected_manifest and facts.get("adapter_manifest_hash") != expected_manifest:
        problems.append(
            f"{facts['run_id']}: manifest hash {facts.get('adapter_manifest_hash','')[:12]}"
            f" != declared {expected_manifest[:12]}")
    if facts.get("receipt_adapter_sha256") and \
            facts["receipt_adapter_sha256"] != facts.get("adapter_sha256"):
        problems.append(
            f"{facts['run_id']}: the sealed training receipt names a different adapter "
            f"than the bytes on disk")
    if not facts.get("training_receipt_present"):
        problems.append(f"{facts['run_id']}: no sealed training receipt; an arm whose "
                        f"identity cannot be read from a receipt is one nobody verified")
    return problems


def common_base_blockers(reference: dict, candidate: dict) -> list[str]:
    """The single-axis precondition, checked field by field rather than in aggregate."""
    problems: list[str] = []
    for field in ("receipt_base_model_id", "receipt_base_revision",
                  "receipt_base_identity_hash",
                  "receipt_tokenizer_chat_template_hash"):
        left, right = reference.get(field), candidate.get(field)
        if not left or not right:
            problems.append(f"COMMON_BASE_MISMATCH: {field} is missing on one arm")
        elif left != right:
            problems.append(
                f"COMMON_BASE_MISMATCH: {field} differs between the arms "
                f"({str(left)[:16]} vs {str(right)[:16]}); the measured delta would be "
                f"a function of two variables")
    if reference.get("adapter_sha256") == candidate.get("adapter_sha256"):
        problems.append(
            "COMMON_BASE_MISMATCH: both arms resolve to the same adapter bytes; a "
            "comparison of an adapter with itself measures nothing")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
#  The plan
# ══════════════════════════════════════════════════════════════════════════════
def build_v4_plan(args):
    """Build the complete V4 plan from the state of the world. Loads no model.

    Returns ``(config, baseline, reference_adapter, candidate_adapter, plan, identity)``.
    Every digest inside the plan is derived here, from disk, in one place — so planning,
    execution and receipt verification cannot disagree about what was bound.
    """
    from training_gym.evaluation.config import load_config
    from training_gym.evaluation.plan import (
        EXPECTED_EVALUATION_FILES,
        EvaluationPlan,
        check_output_root,
        output_root_id,
        plan_state_sequence,
    )
    from training_gym.evaluation.plan_v4 import V4EvaluationPlan, task_order_hash
    from training_gym.evaluation.preflight import prepare_pack_identity
    from training_gym.evaluation.protocol_v4 import (
        EvaluationArmRole,
        PairedSpendPlan,
        ReferenceAdapterPairing,
        arm_from_training_receipt,
    )
    from training_gym.evaluation.pack_builder import build_task_pack_from_dataset
    from training_gym.evaluation.references import (
        base_reference_from_identity,
        build_adapter_reference,
        verify_reference_pair,
    )
    from training_gym.evaluation.runner import ORDER_POLICY_BALANCED
    from training_gym.training.environment import HardwareCapabilityReport
    from training_gym.training.model_identity import probe_cache
    from training_gym.datasets.manifests import load_manifest

    config = load_config(args.config)

    identity_model = config.baseline_model
    if args.model_cache_root:
        status, evidence = probe_cache(identity_model.model_id,
                                       cache_root=args.model_cache_root)
        identity_model = type(identity_model)(
            provider=identity_model.provider, model_id=identity_model.model_id,
            revision=identity_model.revision,
            parameters_b=identity_model.parameters_b, family=identity_model.family,
            tokenizer_id=identity_model.tokenizer_id,
            tokenizer_revision=identity_model.tokenizer_revision,
            cache_status=status, cache_evidence=evidence,
            license_reference=identity_model.license_reference)
    baseline = base_reference_from_identity(identity_model)

    reference_run = args.reference_run_id
    candidate_run = config.candidate_adapter.run_id
    reference_dir = Path(args.training_root) / "runs" / reference_run
    candidate_dir = Path(args.training_root) / "runs" / candidate_run
    reference_adapter = build_adapter_reference(
        str(reference_dir), lora={}, base_model_id=identity_model.model_id)
    candidate_adapter = build_adapter_reference(
        str(candidate_dir), lora={}, base_model_id=identity_model.model_id)

    blockers: list[str] = []
    warnings: list[str] = []
    for adapter in (reference_adapter, candidate_adapter):
        pair = verify_reference_pair(baseline, adapter)
        blockers.extend(pair.blockers)
        warnings.extend(pair.warnings)

    # The arms come from the SEALED TRAINING RECEIPTS, never from the caller: a caller
    # who may name the adapter is a caller who may name the wrong one.
    receipts = REPO_ROOT / "state" / "m62" / "receipts"
    reference_receipt = receipts / f"{reference_run}.train.json"
    candidate_receipt = receipts / f"{candidate_run}.train.json"
    for path in (reference_receipt, candidate_receipt):
        if not path.is_file():
            blockers.append(f"no sealed training receipt at {path.name}")
    pairing = None
    if reference_receipt.is_file() and candidate_receipt.is_file():
        pairing = ReferenceAdapterPairing(
            reference=arm_from_training_receipt(
                json.loads(reference_receipt.read_text(encoding="utf-8")),
                role=EvaluationArmRole.REFERENCE, run_id=reference_run,
                training_receipt_sha256=sha256_file(reference_receipt)),
            candidate=arm_from_training_receipt(
                json.loads(candidate_receipt.read_text(encoding="utf-8")),
                role=EvaluationArmRole.CANDIDATE, run_id=candidate_run,
                training_receipt_sha256=sha256_file(candidate_receipt)))

    dependencies = _dependency_report()
    hardware = HardwareCapabilityReport.detect(output_root=args.output_root)
    manifest = load_manifest(root=args.dataset_root,
                             dataset_id=config.dataset.dataset_id,
                             dataset_version=config.dataset.dataset_version)
    blockers.extend(config.eligibility_blockers())
    blockers.extend(check_output_root(args.output_root, config.evaluation_id,
                                      config.evaluation_generation))
    blockers.extend(dependencies.blockers())

    identity = prepare_pack_identity(
        root=args.dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation,
        seed=config.seed)
    built = build_task_pack_from_dataset(
        root=args.dataset_root, dataset_id=config.dataset.dataset_id,
        dataset_version=config.dataset.dataset_version,
        splits=config.splits.splits, generation=config.evaluation_generation)
    order = tuple(t.task_id for t in built.pack.tasks)

    def shard(split_name: str) -> str:
        entry = next((s for s in config.splits.splits if s.value == split_name), None)
        found = manifest.shard_for(entry) if entry else None
        return found.sha256_file if found else ""

    inner = EvaluationPlan(
        evaluation_id=config.evaluation_id,
        generation=config.evaluation_generation,
        evaluation_config_hash=config.config_hash(),
        baseline_reference_hash=baseline.reference_hash(),
        candidate_adapter_reference_hash=candidate_adapter.reference_hash(),
        tokenizer_identity_hash=baseline.tokenizer_identity_hash,
        task_pack_hash=identity.pack_hash,
        hidden_target_store_hash=identity.hidden_target_store_hash,
        validation_manifest_hash="",
        hidden_evaluation_manifest_hash=shard("hidden_evaluation"),
        security_regression_manifest_hash=shard("security_regression"),
        adversarial_manifest_hash=shard("adversarial"),
        dataset_manifest_hash=identity.dataset_manifest_hash,
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
        order_assignment_hash=identity.order_assignment_hash,
        expected_output_root_id=output_root_id(args.output_root),
        expected_task_count=identity.task_count,
        expected_baseline_generations=identity.task_count,
        expected_candidate_generations=identity.task_count,
        expected_grader_executions=identity.task_count * 6,
        expected_files=EXPECTED_EVALUATION_FILES,
        expected_state_transitions=plan_state_sequence(awaiting_confirmation=True),
        backend_id=BACKEND_ID, created_at_utc=config.created_at_utc,
        performs_inference=True,
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)))

    plan = None
    if pairing is not None:
        plan = V4EvaluationPlan(
            inner=inner, pairing=pairing,
            spend=PairedSpendPlan(pairing=pairing, task_count=identity.task_count),
            reference_adapter_reference_hash=reference_adapter.reference_hash(),
            candidate_adapter_reference_hash=candidate_adapter.reference_hash(),
            task_order_hash=task_order_hash(order),
            arm_order_policy=ORDER_POLICY_BALANCED,
            arm_order_assignment_hash=identity.order_assignment_hash,
            runtime_report_sha256=args.runtime_report_sha256,
            evaluation_source_commit=args.source_head,
            holdout_dataset_id=config.dataset.dataset_id,
            holdout_dataset_version=config.dataset.dataset_version,
            holdout_manifest_hash=identity.dataset_manifest_hash,
            holdout_pack_hash=identity.pack_hash,
            holdout_preregistration_sha256=args.preregistration_sha256,
            receipt_path_class=RECEIPT_PATH_CLASS,
            artifact_path_class=ARTIFACT_PATH_CLASS,
            blockers=tuple(dict.fromkeys(blockers)))
    return config, baseline, reference_adapter, candidate_adapter, plan, identity


def _dependency_report():
    """Probe what THIS backend needs. Never called without a backend.

    Called exactly as ``evaluate_adapter`` calls it, so the V4 plan's
    ``dependency_report_hash`` is comparable with the v1-v3 plans' rather than being a
    second, differently-derived number that happens to have the same name.
    """
    from training_gym.evaluation.backends import backend_required_packages
    from training_gym.training.config import DependencyProfile
    from training_gym.training.dependencies import build_dependency_report

    return build_dependency_report(
        profile=DependencyProfile.TRAINING,
        required_packages=backend_required_packages(BACKEND_ID))


# ══════════════════════════════════════════════════════════════════════════════
#  Modes
# ══════════════════════════════════════════════════════════════════════════════
def _check_artifacts(args) -> int:
    receipts = REPO_ROOT / "state" / "m62" / "receipts"
    reference = artifact_facts(args.training_root, args.reference_run_id,
                               receipts / f"{args.reference_run_id}.train.json")
    from training_gym.evaluation.config import load_config
    candidate_run = load_config(args.config).candidate_adapter.run_id
    candidate = artifact_facts(args.training_root, candidate_run,
                               receipts / f"{candidate_run}.train.json")
    problems = artifact_blockers(reference, expected_adapter=args.expect_reference_adapter,
                                 expected_manifest=args.expect_reference_manifest)
    problems += artifact_blockers(candidate,
                                  expected_adapter=args.expect_candidate_adapter,
                                  expected_manifest=args.expect_candidate_manifest)
    problems += common_base_blockers(reference, candidate)
    _emit({
        "status": "checked" if not problems else "refused",
        "reference": reference, "candidate": candidate,
        "common_base_model_id": reference.get("receipt_base_model_id", ""),
        "common_base_revision": reference.get("receipt_base_revision", ""),
        "single_axis_confirmed": not problems,
        "model_loads": 0, "generations": 0,
        "problems": problems,
    })
    return EXIT_OK if not problems else EXIT_ARTIFACTS


def _print_plan(args) -> int:
    _, _, reference, candidate, plan, identity = build_v4_plan(args)
    if plan is None:
        return _fail("no pairing could be built; both arms need a sealed training receipt",
                     code=EXIT_PLAN)
    _emit({
        "status": "planned",
        "plan_hash": plan.plan_hash(),
        "inner_plan_hash": plan.inner.plan_hash(),
        "pairing_hash": plan.pairing.pairing_hash(),
        "reference_arm_hash": plan.pairing.reference.arm_hash(),
        "candidate_arm_hash": plan.pairing.candidate.arm_hash(),
        "task_order_hash": plan.task_order_hash,
        "task_count": plan.spend.task_count,
        "expected_total_generations": plan.expected_total_generations,
        "holdout_spends": plan.spend.holdout_spends,
        "is_executable": plan.is_executable,
        "blockers": list(plan.blockers),
        # DELIBERATELY ABSENT: the confirmation token.
        "confirmation_required": "EVAL:<plan_hash>, assembled by the operator",
        "model_loads": 0, "generations": 0,
    })
    return EXIT_OK if plan.is_executable else EXIT_PLAN


def _derive_plan(args) -> int:
    """Derive the plan N times IN N PROCESSES and require byte-identical serialization."""
    hashes: list[str] = []
    payloads: list[str] = []
    for _ in range(max(1, args.derivations)):
        done = _subprocess_plan(args)
        if done is None:
            return _fail("a derivation process failed", code=EXIT_PLAN)
        hashes.append(done["plan_hash"])
        payloads.append(done["canonical"])
    identical = len(set(payloads)) == 1 and len(set(hashes)) == 1

    # serialize -> deserialize -> serialize must be a fixed point.
    roundtrip = json.dumps(json.loads(payloads[0]), sort_keys=True,
                           separators=(",", ":")) == payloads[0]
    _emit({
        "status": "derived" if identical and roundtrip else "refused",
        "derivations": len(hashes),
        "plan_hash": hashes[0] if hashes else "",
        "byte_identical": identical,
        "roundtrip_stable": roundtrip,
        "distinct_hashes": sorted(set(hashes)),
        "model_loads": 0, "generations": 0,
    })
    return EXIT_OK if identical and roundtrip else EXIT_PLAN


def _subprocess_plan(args) -> dict | None:
    """One derivation, in its own interpreter, so shared state cannot make it agree."""
    import subprocess  # nosec B404 — fixed argv, shell=False

    program = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(_ROOT)!r})\n"
        f"sys.path.insert(0, {str(_ROOT / 'scripts')!r})\n"
        "from scripts.evaluate_reference_pair import build_v4_plan, _Args\n"
        f"args=_Args(**{_args_dict(args)!r})\n"
        "_,_,_,_,plan,_=build_v4_plan(args)\n"
        "print(json.dumps({'plan_hash':plan.plan_hash(),"
        "'canonical':json.dumps(plan.to_dict(),sort_keys=True,separators=(',',':'))}))\n")
    done = subprocess.run(  # nosec B603 — fixed argv, shell=False
        [sys.executable, "-c", program], capture_output=True, text=True,
        timeout=900, check=False)
    if done.returncode != 0:
        print(_scrub(done.stderr.strip()[-1500:]), file=sys.stderr)
        return None
    return json.loads(done.stdout.strip().splitlines()[-1])


class _Args:
    """A plain carrier so a derivation subprocess can rebuild the same arguments."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _args_dict(args) -> dict:
    return {
        "config": args.config, "dataset_root": args.dataset_root,
        "training_root": args.training_root, "output_root": args.output_root,
        "model_cache_root": args.model_cache_root,
        "reference_run_id": args.reference_run_id,
        "runtime_report_sha256": args.runtime_report_sha256,
        "source_head": args.source_head,
        "preregistration_sha256": args.preregistration_sha256,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  --execute : the ONLY mode that may load a weight
# ══════════════════════════════════════════════════════════════════════════════
def _control_plane_blockers(dataset_id: str, dataset_version: str) -> list[str]:
    """Refuse unless the CONTROL PLANE still says this holdout is unspent.

    The ledger guard in ``store_v4`` protects against a second attempt in the same
    output root. This is the independent second opinion: the recorded scientific state
    of the repository. If the control plane says the corpus is spent, no ledger anywhere
    makes it unspent, and the run stops before an authority is consumed.
    """
    from scripts.verify_m62_control_plane import load_semantic_snapshot

    problems: list[str] = []
    snapshot = load_semantic_snapshot(REPO_ROOT)
    entry = next((d for d in snapshot.get("datasets", ())
                  if d.get("dataset_id") == dataset_id
                  and d.get("version") == dataset_version), None)
    if entry is None:
        return [f"the control plane does not record {dataset_id} {dataset_version}; a "
                f"holdout the state machine has never heard of is not one to spend"]
    if entry.get("status") != "FROZEN_UNUSED":
        problems.append(
            f"the control plane records {dataset_id} {dataset_version} as "
            f"{entry.get('status')!r}, not FROZEN_UNUSED. A spent holdout is never "
            f"re-spent")
    if entry.get("spent_by") is not None:
        problems.append(
            f"{dataset_id} {dataset_version} already names a spender: "
            f"{entry.get('spent_by')!r}")
    return problems


def _progress(body: dict) -> None:
    """BODY-FREE progress. Prints five fields, none of which can hold task material."""
    print(f"TASK {body['task_index']:02d}/{body['task_count']:02d} "
          f"{body['arm'].upper():9s} {body['status']:9s} "
          f"{body['latency_ms'] // 1000:4d}s", flush=True)


def _execute(args) -> int:
    """Consume ONE authority and run ONE paired attempt. Everything else has refused."""
    from training_gym.evaluation.execution_v4 import (
        V4ExecutionRequest,
        execute_v4_evaluation,
    )
    from training_gym.evaluation.execution import production_backend_factory
    from training_gym.evaluation.plan_v4 import check_v4_confirmation

    config, baseline, reference, candidate, plan, _identity = build_v4_plan(args)
    if plan is None:
        return _fail("no pairing could be built; both arms need a sealed training receipt",
                     code=EXIT_PLAN)
    if not plan.is_executable:
        return _fail(f"the plan carries blockers and authorises nothing: "
                     f"{list(plan.blockers) or list(plan.inner.blockers)}", code=EXIT_PLAN)

    # THE AUTHORITY GATE. Checked before anything else reaches for a model, and against
    # the plan just re-derived from the state of the world rather than a remembered one.
    try:
        check_v4_confirmation(args.confirm, plan)
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer
        return _fail(f"{type(exc).__name__}: {exc}", code=EXIT_CONFIRMATION)

    blockers = _control_plane_blockers(plan.holdout_dataset_id,
                                       plan.holdout_dataset_version)
    if blockers:
        return _fail("; ".join(blockers), code=EXIT_PLAN)

    print(f"EVAL_AUTHORITY_CONSUMED plan={plan.plan_hash()[:12]} "
          f"arms=2 tasks={plan.spend.task_count} "
          f"generations={plan.expected_total_generations} spends=1", flush=True)

    outcome = execute_v4_evaluation(V4ExecutionRequest(
        config=config, plan=plan, baseline=baseline,
        reference_adapter=reference, candidate_adapter=candidate,
        reference_adapter_directory=Path(args.training_root) / "runs"
        / args.reference_run_id,
        candidate_adapter_directory=Path(args.training_root) / "runs"
        / config.candidate_adapter.run_id,
        output_root=Path(args.output_root), dataset_root=Path(args.dataset_root),
        backend_factory=production_backend_factory(BACKEND_ID),
        model_cache_root=Path(args.model_cache_root) if args.model_cache_root else None,
        actor=args.actor, at=_utc_now(), backend_version=_backend_version(),
        limitations=config.limitations,
        on_arm_complete=_progress))

    _emit({
        "status": "completed" if outcome.ok else outcome.state.value,
        "plan_hash": plan.plan_hash(),
        **outcome.to_dict(),
        "generation_directory": (outcome.directory.name if outcome.directory else ""),
    })
    return EXIT_OK if outcome.ok else EXIT_REFUSED


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backend_version() -> str:
    import importlib.metadata as meta
    parts = []
    for package in ("transformers", "peft", "torch"):
        try:
            parts.append(f"{package}={meta.version(package)}")
        except Exception:  # noqa: BLE001
            parts.append(f"{package}=absent")
    return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Protocol V4 reference-adapter paired evaluation.")
    parser.add_argument("--config", default="")
    parser.add_argument("--dataset-root", default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--training-root", default=DEFAULT_TRAINING_ROOT)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--model-cache-root", default="")
    parser.add_argument("--reference-run-id", default="")
    parser.add_argument("--runtime-report-sha256", default="")
    parser.add_argument("--source-head", default="")
    parser.add_argument("--preregistration-sha256", default="")
    parser.add_argument("--derivations", type=int, default=3)
    parser.add_argument("--expect-reference-adapter", default="")
    parser.add_argument("--expect-reference-manifest", default="")
    parser.add_argument("--expect-candidate-adapter", default="")
    parser.add_argument("--expect-candidate-manifest", default="")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-artifacts", action="store_true")
    mode.add_argument("--print-plan", action="store_true")
    mode.add_argument("--derive-plan", action="store_true")
    mode.add_argument("--execute", action="store_true",
                      help="consume ONE authority and run ONE paired attempt")
    parser.add_argument("--actor", default="local-operator")
    parser.add_argument("--confirm", default="",
                        help="EVAL:<full-plan-hash>; required by --execute")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check_artifacts:
            return _check_artifacts(args)
        if args.print_plan:
            return _print_plan(args)
        if args.derive_plan:
            return _derive_plan(args)
        if args.execute:
            return _execute(args)
    except Exception as exc:  # noqa: BLE001 — the refusal IS the answer, never a traceback
        return _fail(f"{type(exc).__name__}: {exc}")
    return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
