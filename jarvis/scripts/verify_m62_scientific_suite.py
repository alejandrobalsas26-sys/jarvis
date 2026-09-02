#!/usr/bin/env python3
"""verify_m62_scientific_suite.py — V69 M62 S4H: the canonical scientific selection.

WHY A MANIFEST REPLACES A KEYWORD
---------------------------------
The recorded test baseline names its invocation as::

    pytest -k m62 --ignore=tests/test_live_brain_v61.py

``-k`` matches against the collected test's node id, which is built from the FILE NAME
and the function name. So the selection is, in effect, "files whose path contains m62".

Three modules assert facts about M62's own state and are named for the milestone that
WROTE them rather than the milestone whose state they protect:

    tests/test_training_gym_m63_s4b_control_plane.py     the generation chain
    tests/test_training_gym_m63_s4b_fifth_candidate.py   candidate 005's identity
    tests/test_training_gym_m63_s4c_trained_state.py     candidate 005's train receipt

``-k m62`` deselects all 212 of them. Nothing was disabled and nothing is failing; they
simply never ran under the invocation the control plane records as its baseline, so the
guard that protects candidate 005's single-axis claim was reported as green by a run that
never collected it.

A filename substring is not a scientific boundary. This script makes the boundary an
explicit, reviewed list, and then refuses a list that has quietly lost something.

WHAT IT CHECKS
--------------
    STRUCTURE          the manifest parses and carries every declared field
    REQUIRED_GROUPS    every group in ``required_groups`` is present and non-empty
    MODULE_EXISTENCE   every named module is a file that exists
    COLLECTABILITY     pytest can collect each module and finds at least one test
    KEYWORD_GAP        the modules `-k m62` would deselect are named here anyway
    NO_DUPLICATES      no module is claimed by two groups

It runs no test and asserts no result. Collection only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "jarvis"
MANIFEST_PATH = "state/m62/scientific-suite.json"
SCHEMA_VERSION = "m62.scientific_suite.1"

#: The keyword selection this manifest supersedes. Kept here so the gap it leaves can be
#: MEASURED rather than remembered.
SUPERSEDED_KEYWORD = "m62"

#: Modules that are scientifically in scope and that ``-k m62`` cannot select. Pinned so
#: that removing one from the manifest is a failure rather than a silent narrowing.
KEYWORD_INVISIBLE_MODULES: tuple = (
    "tests/test_training_gym_m63_s4b_control_plane.py",
    "tests/test_training_gym_m63_s4b_fifth_candidate.py",
    "tests/test_training_gym_m63_s4c_trained_state.py",
)


class Report:
    """Named checks, each PASS or FAIL, with the problems printed once."""

    def __init__(self) -> None:
        self.checks: dict = {}
        self.problems: list = []

    def ok(self, name: str) -> None:
        self.checks.setdefault(name, "PASS")

    def fail(self, name: str, message: str) -> None:
        self.checks[name] = "FAIL"
        self.problems.append(f"[{name}] {message}")

    @property
    def passed(self) -> bool:
        return not self.problems


def load_manifest(root: Path) -> dict:
    path = root / MANIFEST_PATH
    if not path.is_file():
        raise SystemExit(f"scientific suite: {MANIFEST_PATH} is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def check_structure(manifest: dict, report: Report) -> None:
    report.ok("STRUCTURE")
    for field in ("schema_version", "suite_id", "suite_generation", "milestone",
                  "working_directory", "note", "required_groups", "groups"):
        if field not in manifest:
            report.fail("STRUCTURE", f"the manifest omits {field!r}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        report.fail("STRUCTURE",
                    f"schema_version {manifest.get('schema_version')!r} is not "
                    f"{SCHEMA_VERSION}")
    for group in manifest.get("groups", []):
        for field in ("group", "why", "modules"):
            if field not in group:
                report.fail("STRUCTURE",
                            f"a group omits {field!r}; a group with no stated reason is "
                            f"a list nobody can review")
        if not group.get("modules"):
            report.fail("STRUCTURE",
                        f"group {group.get('group')!r} names no modules; an empty group "
                        f"passes while protecting nothing")


def check_required_groups(manifest: dict, report: Report) -> None:
    report.ok("REQUIRED_GROUPS")
    present = {g.get("group") for g in manifest.get("groups", [])}
    for required in manifest.get("required_groups", []):
        if required not in present:
            report.fail("REQUIRED_GROUPS",
                        f"required group {required!r} is absent. A scientific group that "
                        f"disappears takes its guarantees with it, silently")


def check_no_duplicates(manifest: dict, report: Report) -> None:
    report.ok("NO_DUPLICATES")
    seen: dict = {}
    for group in manifest.get("groups", []):
        for module in group.get("modules", []):
            if module in seen:
                report.fail("NO_DUPLICATES",
                            f"{module} is claimed by both {seen[module]!r} and "
                            f"{group.get('group')!r}; one module, one owner")
            seen[module] = group.get("group")


def check_module_existence(manifest: dict, root: Path, report: Report) -> None:
    report.ok("MODULE_EXISTENCE")
    for module in all_modules(manifest):
        if not (root / "jarvis" / module).is_file():
            report.fail("MODULE_EXISTENCE",
                        f"{module} is named by the manifest and does not exist")


def check_keyword_gap(manifest: dict, report: Report) -> None:
    """The modules the superseded keyword cannot reach must be named here anyway."""
    report.ok("KEYWORD_GAP")
    named = set(all_modules(manifest))
    for module in KEYWORD_INVISIBLE_MODULES:
        if module not in named:
            report.fail("KEYWORD_GAP",
                        f"{module} is invisible to `-k {SUPERSEDED_KEYWORD}` and is not "
                        f"in the manifest either, so nothing selects it")
        if SUPERSEDED_KEYWORD in Path(module).name:
            report.fail("KEYWORD_GAP",
                        f"{module} is listed as keyword-invisible but its name contains "
                        f"{SUPERSEDED_KEYWORD!r}; the pin is stale")


def check_collectability(manifest: dict, root: Path, report: Report,
                         *, skip: bool) -> None:
    report.ok("COLLECTABILITY")
    if skip:
        return
    modules = all_modules(manifest)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header",
         "-p", "no:cacheprovider", *modules],
        cwd=str(root / "jarvis"), capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        tail = "; ".join(result.stdout.strip().splitlines()[-3:]) or "no output"
        report.fail("COLLECTABILITY",
                    f"pytest could not collect the manifest's modules ({tail}). A "
                    f"manifest naming a module that does not import selects nothing")
        return
    collected = sum(1 for line in result.stdout.splitlines() if "::" in line)
    if collected <= 0:
        report.fail("COLLECTABILITY",
                    "the manifest's modules collected zero tests; an empty selection "
                    "passes and protects nothing")
    report.collected = collected  # type: ignore[attr-defined]


def all_modules(manifest: dict) -> list:
    modules: list = []
    for group in manifest.get("groups", []):
        modules.extend(group.get("modules", []))
    return modules


def pytest_argv(manifest: dict) -> list:
    """The exact invocation this manifest authorises. Explicit paths, never `-k`."""
    return ["pytest", *all_modules(manifest)]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--manifest", default=None,
                        help="an alternate manifest path, for mutation testing")
    parser.add_argument("--skip-collection", action="store_true",
                        help="structure checks only; does not start pytest")
    parser.add_argument("--print-invocation", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    manifest = (json.loads(Path(args.manifest).read_text(encoding="utf-8"))
                if args.manifest else load_manifest(root))

    if args.print_invocation:
        print(" ".join(pytest_argv(manifest)))
        return 0

    report = Report()
    check_structure(manifest, report)
    check_required_groups(manifest, report)
    check_no_duplicates(manifest, report)
    check_module_existence(manifest, root, report)
    check_keyword_gap(manifest, report)
    check_collectability(manifest, root, report, skip=args.skip_collection)

    for problem in report.problems:
        print(f"PROBLEM {problem}")
    print()
    print("M62_SCIENTIFIC_SUITE_VERIFY:")
    print("PASS" if report.passed else "FAIL")
    for name in sorted(report.checks):
        print(f"{name}:")
        print(report.checks[name])
    print("GROUPS:")
    print(len(manifest.get("groups", [])))
    print("MODULES:")
    print(len(all_modules(manifest)))
    if hasattr(report, "collected"):
        print("COLLECTED:")
        print(report.collected)
    print("PROBLEMS:")
    print(len(report.problems))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
