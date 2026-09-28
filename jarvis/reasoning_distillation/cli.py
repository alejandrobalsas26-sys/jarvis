"""reasoning_distillation/cli.py — V69 M67A: the stages, as commands.

CONVENTIONS THIS FOLLOWS
------------------------
The repository's scripts use ``argparse`` with a module docstring that explains the decision the
script implements, ``--json`` for machine output and a non-zero exit for a refusal. This follows
that, with subcommands for §21's stages.

WHAT ``--no-llm`` MEANS, AND WHY IT IS THE DEFAULT
--------------------------------------------------
There is no provider wired into this CLI at all, so every stage here is deterministic and offline.
``--no-llm`` is accepted and is a no-op that is REPORTED as such, rather than being rejected as an
unknown flag: a caller scripting against §21's option list should not fail, and silently accepting
a flag that would have mattered is worse than saying it did nothing.

A future milestone that adds a provider must add ``--llm`` as the opt-in. That direction is the
point: model assistance is something a run asks for, never something it gets by default.

THE EXIT CODES
--------------
  0  the stage completed and its result is clean
  1  the stage completed and its result is NOT clean — an unclean leakage audit, an indefensible
     split, a refused freeze. The work was done and the answer is no.
  2  the stage could not run — a missing corpus root, an unreadable config, a refusal.
  3  ``CORPUS_INPUT_REQUIRED``: the infrastructure is ready and no real corpus was supplied (§5).

1 and 2 are separate because "we measured and it failed" and "we could not measure" are different
facts, and this repository treats conflating them as a defect.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from training_gym.schemas import SchemaError, SensitivityClass, canonical_json

from . import adapters, benchmark, manifests, pipeline, skills, splitting, storage
from .config import DistillationConfig, default_config, load_config
from .models import Disposition
from .storage import CorpusRoot, StorageError

EXIT_OK = 0
EXIT_UNCLEAN = 1
EXIT_REFUSED = 2
EXIT_CORPUS_REQUIRED = 3

#: Printed whenever a stage runs without a real corpus. §5's contract, in one place.
CORPUS_INPUT_REQUIRED = "CORPUS_INPUT_REQUIRED"


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(canonical_json(payload))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}:")
            print("  " + canonical_json(value).replace("\n", "\n  "))
        else:
            print(f"{key}: {value}")


def _config_from(args: argparse.Namespace) -> DistillationConfig:
    config = load_config(args.config) if getattr(args, "config", None) else default_config()
    if getattr(args, "seed", None) is not None:
        config = config.with_seed(args.seed)
    return config.validated()


def _root_from(args: argparse.Namespace, *, create: bool) -> CorpusRoot:
    return CorpusRoot.prepare(getattr(args, "corpus", None), create=create)


def _source_paths(args: argparse.Namespace) -> list[Path]:
    """Every candidate file from ``--input``. Non-recursive by default (§5).

    §5: *"do NOT recursively scan my entire home directory."* A directory given to ``--input``
    yields its own entries and nothing deeper unless ``--recursive`` is passed explicitly, so a
    mistyped path cannot sweep a home directory into the pipeline.
    """
    out: list[Path] = []
    for raw in getattr(args, "input", None) or []:
        path = Path(raw).expanduser()
        if path.is_dir():
            pattern = "**/*" if getattr(args, "recursive", False) else "*"
            out.extend(sorted(p for p in path.glob(pattern) if p.is_file()))
        else:
            out.append(path)
    return out


def _sensitivity(args: argparse.Namespace) -> SensitivityClass:
    return SensitivityClass(getattr(args, "sensitivity", None) or SensitivityClass.INTERNAL.value)


# ── stages ─────────────────────────────────────────────────────────────────────────────────
def cmd_contract(args: argparse.Namespace) -> int:
    """Print the expected input contract. The answer to 'what do you need from me' (§5)."""
    _emit({"status": CORPUS_INPUT_REQUIRED,
           "what_is_needed": [
               "a corpus root path you supply explicitly, via --input",
               "sample files in one of the implemented formats, or converted to the JSON contract",
           ],
           "implemented_formats": adapters.supported()["implemented"],
           "deliberately_absent": adapters.supported()["deliberately_absent"],
           "json_contract": adapters.contract_description(),
           "transcript_labels": adapters.known_labels(),
           "default_corpus_root": str(storage.DEFAULT_CORPUS_ROOT),
           "note": "raw conversations stay local and outside Git; the root is verified against "
                   "`git check-ignore` before anything is written"},
          as_json=args.json)
    return EXIT_CORPUS_REQUIRED


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = _source_paths(args)
    if not paths:
        return cmd_contract(args)
    outcome = pipeline.ingest_paths(paths, config=_config_from(args),
                                    sensitivity=_sensitivity(args), limit=args.limit)
    _emit({"stage": "ingest", **outcome.to_dict()}, as_json=args.json)
    return EXIT_OK if outcome.conversations else EXIT_UNCLEAN


def cmd_build(args: argparse.Namespace) -> int:
    """The composed build. Backs ``normalize``, ``dedupe``, ``split``, ``distill`` and ``validate``.

    One implementation for all of them because they are one ordered pipeline (§9), and five
    entrypoints that each re-derived a prefix of it would be five chances for the order to differ.
    Each subcommand selects how far to go and what to print, never a different order.
    """
    paths = _source_paths(args)
    if not paths:
        return cmd_contract(args)
    config = _config_from(args)
    try:
        outcome = pipeline.run(paths, config=config, sensitivity=_sensitivity(args),
                              freeze_holdout=not args.no_freeze, generation=args.generation or "",
                              limit=args.limit, distil=not args.dry_run and args.stage == "distill")
    except (pipeline.PipelineError, SchemaError) as exc:
        _emit({"stage": args.stage, "status": "REFUSED", "error": str(exc)}, as_json=args.json)
        return EXIT_REFUSED

    payload: dict = {"stage": args.stage, "dry_run": bool(args.dry_run),
                     "llm_used": False,
                     "no_llm_flag": "accepted; this CLI has no provider wired, so it was a no-op",
                     "counts": outcome.counts().to_dict(),
                     "not_run": list(outcome.not_run)}
    if args.stage in ("normalize", "dedupe", "distill", "validate"):
        payload["dedupe"] = outcome.dedupe_report.to_dict()
    if args.stage in ("split", "distill", "validate"):
        payload["split"] = outcome.split.to_dict()
        payload["leakage_audit"] = outcome.audit.to_dict()
        payload["frozen_holdout"] = outcome.frozen.to_dict() if outcome.frozen else None
    if args.stage in ("distill", "validate"):
        payload["dispositions"] = [
            {"example_id": r.record.example_id, "disposition": v.disposition.value,
             "reasons": [x.value for x in v.reasons]}
            for r, v in zip(outcome.results, outcome.verdicts)]
    if args.stage == "validate":
        payload["manifest"] = outcome.manifest.to_dict() if outcome.manifest else None

    if args.write:
        root = _root_from(args, create=True)
        payload["written"] = pipeline.persist(root, outcome)

    _emit(payload, as_json=args.json)
    clean = outcome.audit.clean and outcome.split.defensible
    return EXIT_OK if clean else EXIT_UNCLEAN


def cmd_review(args: argparse.Namespace) -> int:
    """List what is waiting for a human. Reads the corpus root; renders no body."""
    root = _root_from(args, create=False)
    area = root.area("review")
    if not area.is_dir():
        _emit({"stage": "review", "status": "no review area",
               "hint": "run `distill --write` first"}, as_json=args.json)
        return EXIT_REFUSED
    items: list[dict] = []
    for path in sorted(area.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            items.append({"file": path.name, "error": f"{type(exc).__name__}"})
            continue
        verdict = payload.get("verdict") or {}
        items.append({"file": path.name,
                      "example_id": (payload.get("record") or {}).get("example_id", ""),
                      "disposition": verdict.get("disposition", ""),
                      "reasons": verdict.get("reasons", []),
                      "notes": verdict.get("notes", [])})
    _emit({"stage": "review", "pending": len(items), "items": items}, as_json=args.json)
    return EXIT_OK


def cmd_build_skills(args: argparse.Namespace) -> int:
    paths = _source_paths(args)
    if not paths:
        return cmd_contract(args)
    try:
        outcome = pipeline.run(paths, config=_config_from(args), sensitivity=_sensitivity(args),
                              freeze_holdout=not args.no_freeze, limit=args.limit, distil=True)
    except (pipeline.PipelineError, SchemaError) as exc:
        _emit({"stage": "build-skills", "status": "REFUSED", "error": str(exc)},
              as_json=args.json)
        return EXIT_REFUSED
    supported = [s for s in outcome.derived_skills if s.status is skills.SkillStatus.SUPPORTED]
    payload = {"stage": "build-skills",
               "accepted_examples": sum(1 for v in outcome.verdicts
                                        if v.disposition is Disposition.ACCEPT),
               "skills": [s.to_dict() for s in outcome.derived_skills],
               "supported": len(supported),
               "coverage": skills.coverage()}
    if args.write:
        payload["written"] = pipeline.persist(_root_from(args, create=True), outcome)
    _emit(payload, as_json=args.json)
    return EXIT_OK if supported else EXIT_UNCLEAN


def cmd_report(args: argparse.Namespace) -> int:
    """Stage versions and the declarations. Runs nothing and needs no corpus."""
    _emit({"stage": "report",
           "versions": manifests.stage_versions(),
           "environment": manifests.environment(),
           "declarations": {
               "raw_corpus_committed": False, "training_performed": False,
               "evaluation_spent": False, "production_reasoning_changed": False,
               "implies_promotion_eligibility": False},
           "partitions": [p.value for p in splitting.Partition],
           "readable_partitions": sorted(p.value for p in splitting.DEVELOPMENT_PARTITIONS)},
          as_json=args.json)
    return EXIT_OK


def cmd_benchmark_status(args: argparse.Namespace) -> int:
    """Harness readiness. Never a measurement (§16)."""
    status = benchmark.status()
    _emit({"stage": "benchmark-status", **status}, as_json=args.json)
    return EXIT_OK if status["ready"] else EXIT_UNCLEAN


# ── parser ─────────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reasoning-distillation",
        description="V69 M67A — reasoning distillation foundation. Deterministic, offline, and it "
                    "trains nothing, evaluates nothing and spends no holdout.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", action="append", metavar="PATH",
                        help="a file or directory of historical conversations. Repeatable. "
                             "REQUIRED for any stage that reads a corpus; without it the stage "
                             "prints the input contract and exits 3.")
    common.add_argument("--recursive", action="store_true",
                        help="descend into subdirectories of --input. Off by default: §5 forbids "
                             "recursively scanning a home directory.")
    common.add_argument("--corpus", metavar="PATH",
                        help=f"local corpus root (default {storage.DEFAULT_CORPUS_ROOT}). Verified "
                             f"against `git check-ignore` before anything is written.")
    common.add_argument("--config", metavar="PATH", help="JSON config overriding thresholds.")
    common.add_argument("--seed", type=int, help="split seed; overrides the config.")
    common.add_argument("--limit", type=int, default=0, help="process at most N conversations.")
    common.add_argument("--sensitivity", choices=[s.value for s in SensitivityClass],
                        help="declared sensitivity of the input (default internal). EXPORT_SAFE "
                             "requires synthetic or lab_fixture, explicitly.")
    common.add_argument("--json", action="store_true", help="machine-readable output.")
    common.add_argument("--no-llm", action="store_true",
                        help="accepted and reported as a no-op: this CLI has no provider wired, so "
                             "every stage is already deterministic and offline.")

    build_common = argparse.ArgumentParser(add_help=False, parents=[common])
    build_common.add_argument("--dry-run", action="store_true",
                              help="produce every deterministic artifact and stop before "
                                   "extraction.")
    build_common.add_argument("--write", action="store_true",
                              help="persist artifacts into the corpus root. Nothing is written "
                                   "without it.")
    build_common.add_argument("--no-freeze", action="store_true",
                              help="do not freeze the holdout partition.")
    build_common.add_argument("--generation", help="control-plane generation to record in the "
                                                  "frozen holdout record.")

    subs = parser.add_subparsers(dest="stage", required=True)
    subs.add_parser("contract", parents=[common],
                    help="print the expected input contract and exit 3.").set_defaults(
        func=cmd_contract)
    subs.add_parser("ingest", parents=[common],
                    help="read and normalize source files.").set_defaults(func=cmd_ingest)
    for name, helptext in (
            ("normalize", "ingest and report the canonical conversations."),
            ("dedupe", "group conversations into problem families."),
            ("split", "assign families to partitions and audit the firewall."),
            ("distill", "the whole pipeline, including bounded distillation."),
            ("validate", "the whole pipeline plus the build manifest.")):
        subs.add_parser(name, parents=[build_common], help=helptext).set_defaults(func=cmd_build)
    subs.add_parser("review", parents=[common],
                    help="list records waiting for a human.").set_defaults(func=cmd_review)
    subs.add_parser("build-skills", parents=[build_common],
                    help="derive reasoning skills from ACCEPT examples.").set_defaults(
        func=cmd_build_skills)
    subs.add_parser("report", parents=[common],
                    help="stage versions and declarations.").set_defaults(func=cmd_report)
    subs.add_parser("benchmark-status", parents=[common],
                    help="benchmark harness readiness.").set_defaults(func=cmd_benchmark_status)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except StorageError as exc:
        _emit({"status": "REFUSED", "error": str(exc)}, as_json=getattr(args, "json", False))
        return EXIT_REFUSED
    except SchemaError as exc:
        _emit({"status": "REFUSED", "error": str(exc)}, as_json=getattr(args, "json", False))
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
