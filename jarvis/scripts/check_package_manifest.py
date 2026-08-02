"""scripts/check_package_manifest.py — V69 M61.5: prove what is inside the build.

A `MANIFEST.in` rule that is quietly wrong looks exactly like one that works, so this
script does not trust it: it opens the built wheel and sdist and inspects the actual
member list.

WHAT IT REFUSES
---------------
  * secrets and signing material — ``.env``, ``telemetry_keys.json``,
    ``trust_profile.json``, ``feed_hashes.json``, ``integrity_baseline.json``,
    ``jarvis_config.yaml`` (which may hold Telegram/API tokens);
  * runtime state — SQLite stores, ``*.jsonl`` audit/task trails, log files,
    diagnostics bundles, backup archives, Chroma collections, model files;
  * developer trees — ``tests/``, ``.venv/``, ``.github/``, ``brain/``, caches.

WHAT IT REQUIRES
----------------
  * the runtime modules are present (``core/``, ``tools/``, ``main.py``);
  * the console entry point is declared;
  * the distribution version equals ``core.version.VERSION``.

Read-only over the artifacts. It never installs, executes or unpacks to a shared
location — members are listed from the archive index, not extracted.

Exit codes: ``0`` clean, ``1`` a violation was found, ``2`` nothing to inspect.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

#: Exact file names that must never appear anywhere in a distribution.
FORBIDDEN_NAMES = frozenset({
    ".env", "telemetry_keys.json", "trust_profile.json", "feed_hashes.json",
    "integrity_baseline.json", "jarvis_config.yaml", "tactic_audit.jsonl",
})

#: Suffixes that indicate runtime state rather than source.
FORBIDDEN_SUFFIXES = (
    ".log", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".jsonl",
    ".pyc", ".pyo", ".gguf", ".bin", ".safetensors", ".pkl",
)

#: Path fragments that must not be packaged (normalized to forward slashes).
FORBIDDEN_DIRS = (
    "/data/", "/logs/", "/.venv/", "/.chroma_db/", "/brain/", "/.github/",
    "/.idea/", "/.claude/", "/__pycache__/", "/.pytest_cache/", "/.ruff_cache/",
    "/tests/", "/evals/", "/training/",
    # V69 M62 S2d — generated training-gym datasets and exports. Each of these quotes
    # task material and the exact bytes a human approved for training; none of them is
    # source, and a distribution carrying one would be shipping the operator's corpus.
    "/dataset_candidates/", "/training_gym_datasets/", "/training_gym_exports/",
    "/teacher_packets/", "/training_gym_artifacts/",
)

#: Members that MUST be present for the distribution to be usable.
REQUIRED_FRAGMENTS = ("core/", "tools/", "main.py")

_VERSION_RE = re.compile(r"^jarvis-(?P<version>[^-]+)(?:-|\.tar\.gz)")


def _members(path: Path) -> list[str]:
    """List archive members without extracting anything."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tf:
            return tf.getnames()
    return []


def _violations(members: list[str]) -> list[str]:
    problems = []
    for raw in members:
        member = raw.replace("\\", "/")
        name = member.rsplit("/", 1)[-1]
        probe = f"/{member}/"
        if name in FORBIDDEN_NAMES:
            problems.append(f"secret/state file packaged: {member}")
            continue
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            problems.append(f"runtime artifact packaged: {member}")
            continue
        if any(fragment in probe for fragment in FORBIDDEN_DIRS):
            problems.append(f"excluded tree packaged: {member}")
    return problems


def _missing(members: list[str]) -> list[str]:
    joined = "\n".join(m.replace("\\", "/") for m in members)
    return [f"required content missing: {fragment}"
            for fragment in REQUIRED_FRAGMENTS if fragment not in joined]


def _declared_version(path: Path) -> str | None:
    match = _VERSION_RE.match(path.name)
    return match.group("version") if match else None


def _entry_point_declared(path: Path) -> bool:
    """The `jarvis` console script must survive into the wheel metadata."""
    if path.suffix != ".whl":
        return True  # only wheels carry entry_points.txt
    with zipfile.ZipFile(path) as zf:
        for member in zf.namelist():
            if member.endswith(".dist-info/entry_points.txt"):
                return "jarvis" in zf.read(member).decode("utf-8", "replace")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="M61.5 package manifest & secret scan")
    ap.add_argument("--dist", required=True, help="directory holding the built artifacts")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        from core.version import VERSION
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot read the canonical version: {type(exc).__name__}: {exc}")
        return 2

    dist = Path(args.dist)
    artifacts = sorted(
        [p for p in dist.glob("*.whl")] + [p for p in dist.glob("*.tar.gz")])
    if not artifacts:
        print(f"FAIL: no wheel or sdist found in {dist}")
        return 2

    report, problems = [], []
    for artifact in artifacts:
        members = _members(artifact)
        found = _violations(members) + _missing(members)

        declared = _declared_version(artifact)
        if declared and declared != VERSION:
            found.append(
                f"artifact version {declared!r} != canonical {VERSION!r}")
        if not _entry_point_declared(artifact):
            found.append("console entry point 'jarvis' missing from wheel metadata")

        report.append({
            "artifact": artifact.name,
            "members": len(members),
            "problems": found,
        })
        problems += [f"{artifact.name}: {p}" for p in found]

    result = {
        "canonical_version": VERSION,
        "artifacts": report,
        "verdict": "PASS" if not problems else "FAIL",
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"package manifest scan - canonical version {VERSION}")
        for entry in report:
            state = "PASS" if not entry["problems"] else "FAIL"
            print(f"  [{state}] {entry['artifact']} ({entry['members']} members)")
            for problem in entry["problems"]:
                print(f"      - {problem}")
        print(f"VERDICT: {result['verdict']}")

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
