"""scripts/train_experiment.py — V69 M62 S3A: the training launcher, planning only.

WHAT THIS IS AND IS NOT
-----------------------
This is the module `core.training_pipeline.TransformersPeftBackend.generate_command`
has named since V65 M17 and which, until now, did not exist — the argv referenced a
launcher nobody had written. It exists now as a **planner**: it validates a config,
verifies the dataset it names, reports which packages and which hardware are actually
present, produces a deterministic plan and prints the token that would authorise that
plan and no other.

Since M62 S3B it can also EXECUTE one bounded SFT-LoRA run — but only when the optional
training packages are installed, the dataset re-verifies byte for byte, the model
revision is immutable, the weights are already cached (or an explicit download
authorisation is supplied) and the operator passes the exact ``TRAIN:<plan-hash>`` token
that this config's plan prints. The default remains a dry run, and no live smoke has ever
been performed against a real model.

SAFETY PROPERTIES, ASSERTED BY TESTS
------------------------------------
  * importing this module performs no I/O and contacts no network; every effect is
    inside ``main``;
  * the default is a dry run. There is no flag that makes execution the default, and
    ``--execute`` without ``--confirm`` is refused ABOVE every effect — before the
    backend registry is consulted, so nothing that could import a framework is loaded;
  * there is no flag naming a backend module, class or executable. A configurable
    backend is an arbitrary-code-execution primitive wearing a settings file;
  * ``--allow-model-download`` changes only what the report says a *future* execution
    would require. Nothing here downloads anything under any combination of flags;
  * ``--confirm`` takes the literal ``TRAIN:<plan-hash>`` token on the command line. It
    is never read from a file or an environment variable: a confirmation nobody typed
    for this exact plan is the one thing the token exists to prevent;
  * no output directory, run directory, config file or adapter is created — including on
    the failure paths;
  * output carries no absolute path and no secret, and the error path is screened too,
    because ``OSError`` stringifies with its filename;
  * nothing here imports torch, transformers, peft, trl, datasets, accelerate,
    bitsandbytes, huggingface_hub or tokenizers, invokes pip, spawns a subprocess or
    opens a socket.

Exit codes: ``0`` success, ``1`` refused or verification failed, ``2`` bad invocation,
and a distinct code per failure category (see ``_exit_codes``) so a wrapper can tell "your
dataset moved" from "your GPU is missing" without parsing prose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:  # pragma: no cover — layout shim, exercised by both layouts
    sys.path.insert(0, _ROOT)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
#: Distinct codes so a wrapper can tell "your dataset moved" from "your GPU is missing"
#: without parsing prose. Every one of them means nothing was completed.
EXIT_CONFIG = 10
EXIT_DATASET = 11
EXIT_DEPENDENCY = 12
EXIT_HARDWARE = 13
EXIT_CONFIRMATION = 14
EXIT_REPLAY = 15
EXIT_UNSUPPORTED = 16
EXIT_MODEL_ACCESS = 17
EXIT_BACKEND = 18
EXIT_INTERRUPTED = 19
EXIT_ARTIFACT = 20
EXIT_INTERNAL = 21

#: Defaults chosen by application policy, never by the config file. A config names an
#: output ROOT ID; the root itself is an operator/deployment decision.
DEFAULT_OUTPUT_ROOT = os.path.join(_ROOT, "training")
DEFAULT_DATASET_ROOT = os.path.join(_ROOT, "training_gym_datasets")


def _scrub(text: object) -> str:
    """Remove anything that looks like a local path from a message.

    The success paths carry only ids and digests by construction. The *error* path does
    not: an ``OSError`` stringifies with its ``filename``, so a refusal could print the
    operator's home directory even though every report is scrubbed. Screen it here.
    """
    message = str(text)
    for root in (DEFAULT_OUTPUT_ROOT, DEFAULT_DATASET_ROOT, _ROOT):
        message = message.replace(root, "<app-root>")
    try:
        home = os.path.expanduser("~")
        if home and home != "~":
            message = message.replace(home, "<home>")
    except (OSError, RuntimeError):  # pragma: no cover — defensive
        pass
    return message


def _emit(payload: dict, *, as_json: bool) -> None:
    """Print a result. JSON when asked, a bounded human summary otherwise."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key, value in sorted(payload.items()):
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _fail(message: str, *, as_json: bool) -> int:
    _emit({"ok": False, "error": _scrub(message), "trained_anything": False,
           "wrote_anything": False}, as_json=as_json)
    return EXIT_REFUSED


def _exit_codes() -> dict:
    """Map an error category onto its exit code. Imported lazily, like everything else."""
    from training_gym.training.run_store import ErrorCategory as _C

    return {
        _C.CONFIGURATION: EXIT_CONFIG, _C.DATASET: EXIT_DATASET,
        _C.DEPENDENCY: EXIT_DEPENDENCY, _C.HARDWARE: EXIT_HARDWARE,
        _C.STALE_PLAN: EXIT_CONFIRMATION, _C.CONFIRMATION: EXIT_CONFIRMATION,
        _C.REPLAY: EXIT_REPLAY, _C.UNSUPPORTED_METHOD: EXIT_UNSUPPORTED,
        _C.MODEL_ACCESS: EXIT_MODEL_ACCESS, _C.TOKENIZER: EXIT_BACKEND,
        _C.BACKEND: EXIT_BACKEND, _C.OUT_OF_MEMORY: EXIT_BACKEND,
        _C.DISK_FULL: EXIT_BACKEND, _C.INTERRUPTED: EXIT_INTERRUPTED,
        _C.INVALID_METRICS: EXIT_ARTIFACT, _C.ARTIFACT: EXIT_ARTIFACT,
        _C.INTERNAL: EXIT_INTERNAL, _C.NONE: EXIT_REFUSED,
    }


def _load(args):
    """Load and validate the config. Lazy import so importing this module does nothing."""
    from training_gym.training.config import load_training_config

    config = load_training_config(args.config)
    if args.method and args.method != _legacy_name(config):
        raise ValueError(
            f"--method {args.method!r} does not match the config's "
            f"{_legacy_name(config)!r}; the method is a property of the reviewed config, "
            f"not of the command line")
    return config


def _legacy_name(config) -> str:
    from training_gym.training.config import legacy_method

    return legacy_method(config.method)


def _plan(args):
    from training_gym.training.planner import plan_training

    config = _load(args)
    result = plan_training(
        config, dataset_root=args.dataset_root, output_root=args.output_root,
        model_cache_root=args.model_cache_root or None,
        export_root=args.export_root or None,
        allow_model_download_flag=bool(args.allow_model_download))
    return config, result


def _dry_run(args) -> int:
    _config, result = _plan(args)
    payload = result.to_dict()
    if args.output:
        payload["output_flag"] = (
            "the --output flag is accepted for compatibility with the argv "
            "core.training_pipeline has generated since V65 M17; the output root is "
            "governed by --output-root and by the config's output_root_id, and this "
            "stage writes nothing under either")
    _emit(payload, as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _print_plan(args) -> int:
    _config, result = _plan(args)
    _emit(result.plan.to_record(), as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _check_dependencies(args) -> int:
    from training_gym.training.dependencies import build_dependency_report

    config = _load(args)
    report = build_dependency_report(profile=config.dependency_profile,
                                     method=config.method)
    payload = report.to_dict()
    payload["blockers"] = list(report.blockers())
    payload["operator_install_command"] = report.install_hint()
    payload["installed_anything"] = False
    _emit(payload, as_json=args.json)
    return EXIT_OK if report.ready else EXIT_REFUSED


def _check_dataset(args) -> int:
    from training_gym.training.dataset_reference import (
        verify_training_dataset_reference,
    )

    config = _load(args)
    result = verify_training_dataset_reference(
        config.dataset_reference, root=args.dataset_root,
        needs_preference_data=config.method.needs_preference_data,
        training_split=config.training_split,
        validation_split=config.validation_split,
        hidden_evaluation_split=config.hidden_evaluation_reference,
        security_regression_split=config.security_regression_reference,
        out_root=args.export_root or None)
    _emit(result.to_dict(), as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _check_hardware(args) -> int:
    from training_gym.training.environment import HardwareCapabilityReport

    report = HardwareCapabilityReport.detect(output_root=args.output_root)
    payload = report.to_dict()
    payload["accelerator_probe_note"] = (
        "no accelerator probe was run: the library that could answer whether CUDA works "
        "is the library that could also start training, so a probe is never implicit. "
        "cuda_available reports 'unknown' rather than 'unavailable', because a probe "
        "that did not run measured nothing")
    _emit(payload, as_json=args.json)
    return EXIT_OK


def _execute(args) -> int:
    """The real execution path. Refuses before anything is spent, or runs one job.

    ``--execute`` without ``--confirm`` is refused HERE, before the backend registry is
    consulted and therefore before any module that could import a framework is loaded.
    That ordering is the reason ``--execute --json`` still creates nothing and loads
    nothing: the refusal happens above every effect, not inside one.
    """
    if not args.confirm:
        return _fail(
            "--execute requires the exact TRAIN:<plan-hash> token this config's plan "
            "prints. Run without --execute to see the plan, read it, then pass the "
            "token it names; nothing was started, created or downloaded",
            as_json=args.json)

    from training_gym.training.execution import execute_training

    config = _load(args)
    backend = _backend_for(args)
    outcome = execute_training(
        config, dataset_root=args.dataset_root, output_root=args.output_root,
        confirmation=args.confirm, backend=backend,
        train_file=_train_file(args, config),
        validation_file=_validation_file(args, config), actor=_safe_actor(),
        at=_utc_now(), nonce=_nonce(),
        export_root=args.export_root or args.dataset_root,
        model_cache_root=args.model_cache_root or None,
        allow_model_download=bool(args.allow_model_download))
    payload = outcome.to_dict()
    payload["ok"] = outcome.ok
    payload["problems"] = [_scrub(p) for p in outcome.problems]
    payload["trained_anything"] = outcome.ok
    payload["downloaded_anything"] = False
    payload["created_adapter"] = outcome.ok
    _emit(payload, as_json=args.json)
    return EXIT_OK if outcome.ok else _exit_codes().get(outcome.error_category,
                                                        EXIT_REFUSED)


def _backend_for(args):
    """Resolve the production backend. There is no flag that names a different one.

    A configurable backend is an arbitrary-code-execution primitive wearing a settings
    file: whoever can write the config chooses what runs inside the process that just
    loaded a verified dataset and holds the operator's approval. Tests inject their
    double by calling ``execute_training`` directly.
    """
    del args
    from training_gym.training.backends import get_backend

    return get_backend("transformers_peft")


def _train_file(args, config) -> str:
    """Where the verified SFT export for this config's dataset version lives."""
    from training_gym.datasets.export import SFT_FILENAME, export_dir

    reference = config.dataset_reference
    root = args.export_root or args.dataset_root
    return str(export_dir(root, reference.dataset_id, reference.dataset_version)
               / SFT_FILENAME)


def _validation_file(args, config) -> str | None:
    """Where the verified validation export lives, or ``None``.

    The CONFIG decides, not the command line: there is no flag that supplies a validation
    corpus to a config that did not ask for one, and none that suppresses the corpus for a
    config that did. Both directions are refused by the backend's readiness check, before
    the plan is spent — which is the property a flag here would destroy.
    """
    from training_gym.datasets.export import SFT_VALIDATION_FILENAME, export_dir

    if not config.train_time_validation_enabled:
        return None
    reference = config.dataset_reference
    root = args.export_root or args.dataset_root
    return str(export_dir(root, reference.dataset_id, reference.dataset_version)
               / SFT_VALIDATION_FILENAME)


def _safe_actor() -> str:
    """A constant, never the OS username. The ledger records that a local operator ran
    this; who they are belongs to the shell's own audit trail, not to a file that gets
    pasted into tickets."""
    return "local-operator"


def _nonce() -> str:
    """A short, safe, unique suffix for a quarantine directory name."""
    import secrets

    return secrets.token_hex(4)


def _utc_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="train_experiment",
        description="V69 M62 — plan and run a LoRA fine-tuning experiment. Validates, "
                    "verifies and reports; never installs and never downloads without "
                    "explicit authorisation.",
        epilog="The default is a dry run. --execute requires the exact "
               "TRAIN:<plan-hash> token the plan prints, and spends it exactly once.")
    parser.add_argument("--config", required=True,
                        help="path to a training configuration JSON document")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--dataset-root", default=DEFAULT_DATASET_ROOT,
                        help="the M62 dataset store root (gitignored)")
    parser.add_argument("--export-root", default="",
                        help="where the SFT/preference export lives, when it is not the "
                             "dataset root")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                        help="the training tree a future run would write under; nothing "
                             "is created here by this stage")
    parser.add_argument("--model-cache-root", default="",
                        help="an explicitly authorised model cache to inspect for "
                             "locally present weights; never written, never fetched")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="explicit no-op flag; this command is a dry run whether or "
                           "not it is passed")
    mode.add_argument("--print-plan", action="store_true",
                      help="emit only the plan record and its confirmation token")
    mode.add_argument("--check-dependencies", action="store_true",
                      help="report which training packages are installed; installs "
                           "nothing and imports none of them")
    mode.add_argument("--check-dataset", action="store_true",
                      help="verify the dataset version and export the config names")
    mode.add_argument("--check-hardware", action="store_true",
                      help="report measured host capability; runs no accelerator probe")
    mode.add_argument("--execute", action="store_true",
                      help="run the experiment; requires --confirm TRAIN:<plan-hash>")

    parser.add_argument("--allow-model-download", action="store_true",
                        help="report what a FUTURE execution would need in order to "
                             "fetch weights; downloads nothing here")
    parser.add_argument("--confirm", default="",
                        help="the exact TRAIN:<plan-hash> token a dry run printed; "
                             "never read from a file or an environment variable")
    # Compatibility with the argv core.training_pipeline has generated since V65 M17.
    parser.add_argument("--method", default="",
                        help="legacy compatibility flag; must agree with the config")
    parser.add_argument("--output", default="",
                        help="legacy compatibility flag; the output root is governed by "
                             "--output-root and nothing is written by this stage")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Every effect this script has is inside here. Importing it does nothing."""
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(args.json)

    if args.confirm and not args.execute:
        return _fail("--confirm authorises an execution; pass --execute alongside it, "
                     "or run without either to see the plan and its token",
                     as_json=as_json)
    if args.dry_run and args.confirm:
        return _fail("--dry-run and --confirm are contradictory; pass one",
                     as_json=as_json)

    handlers = [
        (args.execute, _execute),
        (args.check_dependencies, _check_dependencies),
        (args.check_dataset, _check_dataset),
        (args.check_hardware, _check_hardware),
        (args.print_plan, _print_plan),
    ]
    handler = next((fn for flag, fn in handlers if flag), _dry_run)
    try:
        return int(handler(args))
    except Exception as exc:  # noqa: BLE001 — a refusal is a message, never a traceback
        return _fail(f"{type(exc).__name__}: {exc}", as_json=as_json)


if __name__ == "__main__":  # pragma: no cover — exercised via main() in tests
    raise SystemExit(main())
