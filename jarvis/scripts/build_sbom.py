"""scripts/build_sbom.py — V69 M61.8: deterministic SBOM generation.

    python jarvis/scripts/build_sbom.py --output <dir>
    python jarvis/scripts/build_sbom.py --output <dir> --profiles base,dev,soc,voice
    python jarvis/scripts/build_sbom.py --output <dir> --observe-installed
    python jarvis/scripts/build_sbom.py --validate <dir>

One CycloneDX 1.5 JSON document per dependency profile, generated from the M61.3
dependency authority (``requirements/<profile>.txt``) by :mod:`core.sbom`.

WHAT IT WILL NEVER DO
--------------------
  * install anything — no profile is installed to make its SBOM look more complete, so
    running this cannot change the operator's environment;
  * contact an index, a registry or an advisory feed: resolution is offline and
    declarative, which is what makes the output byte-reproducible;
  * emit an absolute path, an environment value, an interpreter location or a
    credential. The bundle is scanned with the same deny-scanner the release evidence
    uses before it is written.

SPDX is deliberately not emitted. There is no SPDX tooling in this repository that
could produce a document cleanly, and hand-rolling a second format would mean two
descriptions of one dependency set that can disagree. CycloneDX is the format the
release publishes; SPDX is deferred to whenever a maintained converter is adopted.

Exit codes: ``0`` written/valid, ``1`` invalid or unsafe output, ``2`` bad invocation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_APP_ROOT))

from core import release_evidence, sbom  # noqa: E402
from core.dependency_authority import PROFILES  # noqa: E402
from core.version import VERSION  # noqa: E402


def _generate(output: Path, profiles: list[str], *, observe_installed: bool,
              timestamp: str | None) -> int:
    unknown = [p for p in profiles if p not in PROFILES]
    if unknown:
        print(f"FAIL: unknown profile(s) {unknown}; known: {sorted(PROFILES)}")
        return 2

    try:
        documents = sbom.build_all(profiles, timestamp=timestamp,
                                   observe_installed=observe_installed)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1

    # Content safety before anything reaches disk.
    leaks: list[str] = []
    for profile, document in sorted(documents.items()):
        leaks += [f"{sbom.sbom_filename(profile)}: {p}"
                  for p in release_evidence.scan_for_private_content(document)]
    if leaks:
        print("FAIL: SBOM would leak private content - nothing written:")
        for leak in leaks[:40]:
            print(f"  - {leak}")
        return 1

    written = sbom.write_sboms(output, documents)
    print(f"SBOM - CycloneDX {sbom.SPEC_VERSION} - jarvis {VERSION}")
    for profile, path in sorted(written.items()):
        print(f"  [OK] {path.name:34s} {len(documents[profile]['components']):3d} "
              f"components  profile={profile}")
    mode = "declared+observed" if observe_installed else "declared"
    print(f"resolution mode: {mode} (transitive installation closure NOT resolved)")
    return 0


def _validate(directory: Path) -> int:
    found = sorted(directory.glob("sbom-cyclonedx-*.json"))
    if not found:
        print(f"FAIL: no sbom-cyclonedx-*.json in {directory.name}")
        return 2
    problems: list[str] = []
    for path in found:
        profile = path.stem.removeprefix("sbom-cyclonedx-")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: unreadable ({type(exc).__name__})")
            continue
        found_problems = sbom.validate_sbom(document, expected_profile=profile)
        found_problems += [f"private content: {p}" for p in
                           release_evidence.scan_for_private_content(document)]
        state = "OK" if not found_problems else "FAIL"
        print(f"  [{state}] {path.name} "
              f"({len(document.get('components', ()))} components)")
        problems += [f"{path.name}: {p}" for p in found_problems]
    for problem in problems:
        print(f"  - {problem}")
    print(f"VERDICT: {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M61.8 deterministic SBOM generation")
    ap.add_argument("--output", metavar="DIR", help="write the SBOMs into DIR")
    ap.add_argument("--validate", metavar="DIR",
                    help="validate the SBOMs already in DIR instead of generating")
    ap.add_argument("--profiles", default=",".join(sbom.MANDATORY_PROFILES),
                    help="comma-separated dependency profiles "
                         f"(mandatory: {','.join(sbom.MANDATORY_PROFILES)}; "
                         f"optional: {','.join(sbom.OPTIONAL_PROFILES)})")
    ap.add_argument("--observe-installed", action="store_true",
                    help="attach the installed version for packages present in THIS "
                         "environment; installs nothing")
    ap.add_argument("--timestamp", metavar="ISO8601",
                    help="UTC generation timestamp to embed; omitted by default so the "
                         "output is byte-reproducible")
    args = ap.parse_args(argv)

    if args.validate:
        return _validate(Path(args.validate))
    if not args.output:
        ap.error("one of --output or --validate is required")
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    return _generate(Path(args.output), profiles,
                     observe_installed=args.observe_installed,
                     timestamp=args.timestamp)


if __name__ == "__main__":
    sys.exit(main())
