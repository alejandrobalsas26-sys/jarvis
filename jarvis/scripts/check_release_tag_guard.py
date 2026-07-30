"""scripts/check_release_tag_guard.py — V69 M61.8: refuse an unsafe tag or release.

Tagging and releasing are the two operations in this repository that cannot be undone
quietly. A tag pushed at the wrong commit is fetched by everyone who pulls; a GitHub
Release whose checksums do not match its artifacts is worse than no release at all,
because it teaches consumers that verification is theatre.

This script is the pre-flight. It **decides nothing and mutates nothing** — it reports,
and the operator acts. Run it immediately before the two manual commands in
``docs/RELEASE_RUNBOOK.md``.

WHAT IT REFUSES
---------------
  1. a dirty working tree — the tag would describe bytes nobody has;
  2. tagging from a feature branch instead of ``master``;
  3. ``HEAD`` differing from ``origin/master`` — including the case where the local
     remote-tracking ref is simply stale;
  4. a duplicate tag, locally or (with ``--check-remote``) on the remote;
  5. a release with no evidence bundle;
  6. a release whose ``SHA256SUMS`` does not verify against its artifacts;
  7. evidence whose recorded commit is not the commit being tagged.

WHAT IT WILL NEVER DO
--------------------
No ``git tag``, ``git push``, ``git fetch``, ``git commit``, ``git merge``, ``git
config`` or ``gh release`` — not even behind a flag. Every git invocation here is an
inspection command, and there is no code path that writes to the repository, the remote
or a package index. It does not fetch either: a stale ``origin/master`` is REPORTED so
the operator runs ``git fetch`` themselves and can see the result, rather than having
this script quietly change their refs.

Exit codes: ``0`` safe to proceed, ``1`` at least one refusal, ``2`` could not evaluate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent
sys.path.insert(0, str(_APP_ROOT))

from core import release_artifacts, release_facts  # noqa: E402
from core.version import VERSION  # noqa: E402

RELEASE_BRANCH = "master"

#: Inspection-only git invocations. `show-ref` and `for-each-ref` are used instead of
#: `git tag --list` so that no argv this module builds ever begins with a subcommand
#: that is capable of writing — the property is checked over the AST by
#: tests/test_release_closure_v69_m618.py.
_READ_ONLY = ("rev-parse", "status", "show-ref", "for-each-ref", "ls-remote",
              "merge-base", "rev-list", "log", "describe", "cat-file")


def _git(args: list[str], *, timeout: float = 30.0) -> tuple[int | None, str]:
    if not args or args[0] not in _READ_ONLY:
        raise ValueError(f"refusing a non-inspection git subcommand: {args[:1]}")
    try:
        done = subprocess.run(["git", *args], cwd=str(_REPO_ROOT),
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__
    return done.returncode, (done.stdout or "").strip()


def _local_tags() -> list[str]:
    code, out = _git(["for-each-ref", "--format=%(refname:short)", "refs/tags"])
    return sorted(out.splitlines()) if code == 0 and out else []


def _remote_tags() -> list[str] | None:
    code, out = _git(["ls-remote", "--tags", "origin"], timeout=60.0)
    if code != 0:
        return None
    names = []
    for line in out.splitlines():
        parts = line.split("refs/tags/", 1)
        if len(parts) == 2 and not parts[1].endswith("^{}"):
            names.append(parts[1].strip())
    return sorted(names)


def evaluate(*, tag: str, evidence_dir: Path | None, check_remote: bool) -> dict:
    """Every refusal, as a bounded report. Pure inspection."""
    refusals: list[str] = []
    notes: list[str] = []

    code, head = _git(["rev-parse", "HEAD"])
    if code != 0:
        return {"verdict": "INSUFFICIENT_EVIDENCE", "refusals": ["git is unavailable"],
                "notes": notes, "tag": tag}

    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch != RELEASE_BRANCH:
        refusals.append(
            f"on branch {branch!r}; a release tag must be created on "
            f"{RELEASE_BRANCH!r} (M61.8 is developed on "
            f"{release_facts.RELEASE_CLOSURE_BRANCH!r} and must NOT be tagged)")

    code, status = _git(["status", "--porcelain"])
    if code != 0:
        refusals.append("could not read the working tree state")
    elif status:
        refusals.append(
            f"working tree is dirty ({len(status.splitlines())} entr(ies)); a tag must "
            f"describe committed bytes")

    code, remote_head = _git(["rev-parse", f"origin/{RELEASE_BRANCH}"])
    if code != 0:
        refusals.append(
            f"no local origin/{RELEASE_BRANCH} ref; run 'git fetch origin' first")
    elif remote_head != head:
        refusals.append(
            f"HEAD {head[:12]} != origin/{RELEASE_BRANCH} {remote_head[:12]}; "
            f"synchronize before tagging")
    else:
        notes.append(
            f"HEAD == origin/{RELEASE_BRANCH} ({head[:12]}) according to the LOCAL "
            f"remote-tracking ref; run 'git fetch origin' yourself if it may be stale — "
            f"this guard never fetches")

    local = _local_tags()
    if tag in local:
        refusals.append(f"tag {tag!r} already exists locally")
    if check_remote:
        remote = _remote_tags()
        if remote is None:
            refusals.append("could not list remote tags (network or auth)")
        elif tag in remote:
            refusals.append(f"tag {tag!r} already exists on origin")
        else:
            notes.append(f"origin has {len(remote)} tag(s); {tag!r} is not among them")
    else:
        notes.append("remote tags not checked (pass --check-remote before pushing)")

    if evidence_dir is None:
        refusals.append(
            "no --evidence directory supplied; a release without an evidence bundle "
            "cannot be published")
    else:
        refusals += _evaluate_evidence(evidence_dir, head)

    return {
        "verdict": "PASS" if not refusals else "REFUSED",
        "tag": tag,
        "canonical_version": VERSION,
        "branch": branch,
        "head": head[:12],
        "refusals": refusals,
        "notes": notes,
    }


def _evaluate_evidence(evidence_dir: Path, head: str) -> list[str]:
    refusals: list[str] = []
    if not evidence_dir.is_dir():
        return [f"evidence directory {evidence_dir.name!r} does not exist"]

    evidence_path = evidence_dir / release_facts.EVIDENCE_FILENAME
    if not evidence_path.is_file():
        refusals.append(f"{release_facts.EVIDENCE_FILENAME} is missing")
    else:
        try:
            document = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return refusals + [
                f"{release_facts.EVIDENCE_FILENAME} unreadable "
                f"({type(exc).__name__})"]
        from core import release_evidence
        problems = release_evidence.validate_evidence(document)
        if problems:
            refusals.append(
                f"evidence does not conform to its schema "
                f"({len(problems)} problem(s), first: {problems[0]})")
        if document.get("evidence_completeness") != "COMPLETE":
            refusals.append(
                f"evidence_completeness is "
                f"{document.get('evidence_completeness')!r}; a release may not be "
                f"published on incomplete evidence")
        if document.get("overall_status") == "FAIL":
            refusals.append("evidence overall_status is FAIL")
        if document.get("canonical_version") != VERSION:
            refusals.append(
                f"evidence is for version {document.get('canonical_version')!r}, "
                f"not {VERSION!r}")
        recorded = str(document.get("git_commit") or "")
        if recorded and not head.startswith(recorded):
            refusals.append(
                f"evidence records commit {recorded} but HEAD is {head[:12]}; the "
                f"evidence describes a different tree")
        if document.get("git_clean") is not True:
            refusals.append("evidence was generated from a dirty tree")

    sums = evidence_dir / release_facts.CHECKSUM_FILENAME
    if not sums.is_file():
        refusals.append(f"{release_facts.CHECKSUM_FILENAME} is missing")
    else:
        problems = release_artifacts.verify_checksums(
            evidence_dir, filename=release_facts.CHECKSUM_FILENAME)
        refusals += [f"checksum verification: {p}" for p in problems]
    return refusals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="M61.8 pre-flight guard for tagging and releasing")
    ap.add_argument("--tag", default=release_facts.RELEASE_TAG,
                    help=f"the annotated tag to be created (default {release_facts.RELEASE_TAG})")
    ap.add_argument("--evidence", metavar="DIR",
                    help="directory holding release-evidence.json, SHA256SUMS and the "
                         "artifacts")
    ap.add_argument("--check-remote", action="store_true",
                    help="also list origin's tags (network; read-only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = evaluate(tag=args.tag,
                      evidence_dir=Path(args.evidence) if args.evidence else None,
                      check_remote=args.check_remote)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"release tag guard - {report['tag']} (version {VERSION})")
        print(f"  branch={report['branch']} head={report['head']}")
        for note in report["notes"]:
            print(f"  [note] {note}")
        for refusal in report["refusals"]:
            print(f"  [REFUSE] {refusal}")
        print(f"VERDICT: {report['verdict']}")
        if report["verdict"] == "PASS":
            print("  Safe to run the tag and release commands in "
                  "docs/RELEASE_RUNBOOK.md. This script does not run them.")

    if report["verdict"] == "PASS":
        return 0
    return 2 if report["verdict"] == "INSUFFICIENT_EVIDENCE" else 1


if __name__ == "__main__":
    sys.exit(main())
