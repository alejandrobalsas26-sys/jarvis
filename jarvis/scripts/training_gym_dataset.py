"""scripts/training_gym_dataset.py — V69 M62 S2d: the minimal dataset entry points.

WHAT THIS IS AND IS NOT
-----------------------
The smallest surface that lets an operator exercise the S2d closure from a shell:
verify a dataset version, preview a promotion, perform one under an exact confirmation,
export SFT rows, and check a written export. It is NOT the M62 CLI milestone — there is
no listing, no editing, no review workflow and no training command, because each of
those is a decision this milestone has not made yet.

SAFETY PROPERTIES, ASSERTED BY TESTS
------------------------------------
  * importing this module performs no I/O and contacts no network; every effect is
    inside ``main``;
  * ``promote`` without ``--confirm`` is a DRY RUN and writes nothing — there is no flag
    that makes promotion the default;
  * ``--confirm`` takes the literal ``PROMOTE:<plan-hash>`` token on the command line.
    It is never read from a file or an environment variable: a confirmation nobody typed
    for this exact plan is the one thing the token exists to prevent;
  * output carries no absolute path and no secret. Paths are store-relative, so a status
    report pasted into an issue does not paste the operator's home directory with it;
  * every failure exits nonzero. Nothing here trains, downloads or tokenizes.

Exit codes: ``0`` success, ``1`` refused or verification failed, ``2`` bad invocation.
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


def _verify_version(args) -> int:
    from training_gym.datasets.manifests import verify_version

    result = verify_version(root=args.root, dataset_id=args.dataset_id,
                            dataset_version=args.version)
    _emit(result.to_dict(), as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _verify_export(args) -> int:
    from training_gym.datasets.export import verify_sft_export

    result = verify_sft_export(out_root=args.out_root or args.root,
                               dataset_id=args.dataset_id, dataset_version=args.version)
    _emit(result.to_dict(), as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _build_request(args):
    """Assemble a promotion request from the store. Read-only; writes nothing.

    The candidate set is whatever the store holds in ``ready_for_promotion`` — the CLI
    does not let a caller name candidates, because a hand-picked list is a hand-picked
    dataset and the split algebra is the thing that decides where records go.
    """
    from training_gym.datasets.candidate import CandidateState
    from training_gym.datasets.candidate_store import CandidateStore
    from training_gym.datasets.leakage import LeakageAnalyzer, LeakagePolicy
    from training_gym.datasets.manifests import RevocationSnapshot
    from training_gym.datasets.promotion_plan import (
        PromotionRequest,
        split_ledger_path,
    )
    from training_gym.datasets.split import (
        DEFAULT_RATIOS,
        SplitAssignmentLedger,
        SplitPolicy,
        plan_splits,
    )

    store = CandidateStore(args.root)
    candidates = [store.read_candidate(cid) for cid in store.candidate_ids()]
    ready = [c for c in candidates if c.state is CandidateState.READY_FOR_PROMOTION]
    if not ready:
        raise SystemExit(_fail("no candidate in the store is ready_for_promotion",
                               as_json=args.json))

    policy = SplitPolicy(seed=args.seed, ratios=dict(DEFAULT_RATIOS))
    plan = plan_splits(ready, policy=policy,
                       ledger=SplitAssignmentLedger(split_ledger_path(args.root)))
    report = LeakageAnalyzer(policy=LeakagePolicy()).analyze(ready, plan=plan)
    request = PromotionRequest(
        root=args.root, dataset_id=args.dataset_id,
        proposed_dataset_version=args.version, candidates=tuple(ready),
        split_plan=plan, leakage_report=report,
        revocation=RevocationSnapshot.from_store(store),
        created_at_utc=args.at, actor=args.actor,
        allow_empty_splits=tuple(args.allow_empty_split or ()))
    return request, store


def _fail(message: str, *, as_json: bool) -> int:
    _emit({"ok": False, "error": message}, as_json=as_json)
    return EXIT_REFUSED


def _promote(args) -> int:
    from training_gym.datasets.promotion_plan import describe_plan, plan_promotion, promote

    request, store = _build_request(args)
    plan = plan_promotion(request)
    if not args.confirm:
        payload = describe_plan(plan)
        payload["dry_run"] = True
        payload["wrote_anything"] = False
        _emit(payload, as_json=args.json)
        return EXIT_OK
    result = promote(request, confirmation=args.confirm, store=store)
    _emit(result.to_dict(), as_json=args.json)
    return EXIT_OK if result.ok else EXIT_REFUSED


def _export_sft(args) -> int:
    from training_gym.datasets.candidate_store import CandidateStore
    from training_gym.datasets.export import export_sft
    from training_gym.datasets.manifests import RevocationSnapshot

    export = export_sft(root=args.root, dataset_id=args.dataset_id,
                        dataset_version=args.version,
                        revocation=RevocationSnapshot.from_store(
                            CandidateStore(args.root)),
                        created_at_utc=args.at, out_root=args.out_root or None)
    _emit(export.to_dict(), as_json=args.json)
    return EXIT_OK


def _export_preference(args) -> int:
    """Preference export is deliberately not a one-shot command.

    Every pair needs its own :class:`PreferenceHumanReview` bound to both digests, and
    there is no honest way for a CLI flag to stand in for that. The command reports what
    is missing rather than pretending a corpus-wide export is available.
    """
    from training_gym.datasets.preference import verify_preference_export

    problems = verify_preference_export(out_root=args.out_root or args.root,
                                        dataset_id=args.dataset_id,
                                        dataset_version=args.version)
    _emit({"ok": not problems, "problems": list(problems),
           "note": "preference pairs are assembled programmatically: each one requires "
                   "a PreferenceHumanReview bound to both the chosen and the rejected "
                   "digest, and no command-line flag can stand in for that decision"},
          as_json=args.json)
    return EXIT_OK if not problems else EXIT_REFUSED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="training_gym_dataset",
        description="V69 M62 S2d — verify, promote and export training-gym datasets. "
                    "Never trains, downloads or contacts a network.")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, needs_version=True):
        p.add_argument("--root", required=True,
                       help="the candidate/dataset store root (gitignored)")
        p.add_argument("--dataset-id", required=True, help="the dataset identifier")
        if needs_version:
            p.add_argument("--version", required=True, help="the dataset version id")
        return p

    verify = sub.add_parser("verify-version",
                            help="re-hash a dataset version's shards against its "
                                 "manifest")
    common(verify)
    verify.set_defaults(handler=_verify_version)

    manifest = sub.add_parser("manifest-verify",
                              help="alias of verify-version")
    common(manifest)
    manifest.set_defaults(handler=_verify_version)

    promote_cmd = sub.add_parser(
        "promote",
        help="preview a promotion (default) or perform one with an exact confirmation")
    common(promote_cmd)
    promote_cmd.add_argument("--seed", required=True,
                             help="the split seed; a split with no recorded seed cannot "
                                  "be reproduced")
    promote_cmd.add_argument("--actor", required=True,
                             help="the operator identifier recorded in the ledgers")
    promote_cmd.add_argument("--at", required=True,
                             help="an explicit UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    promote_cmd.add_argument("--allow-empty-split", action="append", default=[],
                             help="declare a split that is permitted to have no records")
    promote_cmd.add_argument("--dry-run", action="store_true",
                             help="explicit no-op flag; a promotion without --confirm is "
                                  "a dry run whether or not this is passed")
    promote_cmd.add_argument("--confirm", default="",
                             help="the exact PROMOTE:<plan-hash> token for the plan a "
                                  "dry run printed; never read from a file")
    promote_cmd.set_defaults(handler=_promote)

    sft = sub.add_parser("export-sft", help="write the SFT export for a verified version")
    common(sft)
    sft.add_argument("--at", required=True, help="an explicit UTC timestamp")
    sft.add_argument("--out-root", default="",
                     help="where the export lands (defaults to the store root)")
    sft.set_defaults(handler=_export_sft)

    verify_export = sub.add_parser("verify-export",
                                   help="re-hash a written SFT export")
    common(verify_export)
    verify_export.add_argument("--out-root", default="")
    verify_export.set_defaults(handler=_verify_export)

    preference = sub.add_parser("export-preference",
                                help="verify a written preference export and explain why "
                                     "building one is not a CLI operation")
    common(preference)
    preference.add_argument("--out-root", default="")
    preference.set_defaults(handler=_export_preference)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Every effect this script has is inside here. Importing it does nothing."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "dry_run", False) and getattr(args, "confirm", ""):
        return _fail("--dry-run and --confirm are contradictory; pass one",
                     as_json=args.json)
    try:
        return int(args.handler(args))
    except SystemExit as exc:  # raised by _build_request for an operator-level refusal
        return int(exc.code or EXIT_REFUSED)
    except Exception as exc:  # noqa: BLE001 — a refusal is a message, never a traceback
        return _fail(f"{type(exc).__name__}: {exc}", as_json=getattr(args, "json", False))


if __name__ == "__main__":  # pragma: no cover — exercised via main() in tests
    raise SystemExit(main())
