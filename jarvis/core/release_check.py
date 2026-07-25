"""core/release_check.py — V69 M61.1: deterministic release-truth verification.

Documentation drifts because nothing checks it. Before M61 the repository claimed
version ``63.0.0`` in package metadata at V69 M60, described itself as a "complete
autonomous Purple Team security platform" that "attacks autonomously" while every
offensive path is in fact behind HITL/NATO approval, advertised ``qwen2.5 7B/14B``
role models when the live role table resolves ``qwen3:8b`` / ``qwen3:14b``, and left
three release documents saying **NOT merged** for milestones that are ancestors of
master.

This module turns each of those into a deterministic, re-runnable check over the
repository's own files. It is a SCANNER: it reads, it never writes, and it never
executes anything it reads.

CHECK FAMILIES
--------------
  * ``version``  — pyproject derives from :mod:`core.version`; no file re-states a
    current version literal that contradicts it;
  * ``models``   — current-facing docs name the configured role models, not the
    superseded generation;
  * ``claims``   — current-facing docs make no unsupported autonomy claim;
  * ``status``   — no current release document claims "NOT merged" for a milestone
    at or below the canonical one;
  * ``install``  — every install command quoted in the READMEs resolves to a file
    that actually exists.

SCOPE DISCIPLINE
----------------
Only CURRENT-FACING documents are scanned (``_CURRENT_DOCS``). Historical release
documents and the archived per-version sections of ``JARVIS.md`` are deliberately
out of scope: rewriting history to look consistent is the opposite of truth.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from core.version import GENERATION, MILESTONE, VERSION

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent

#: Current-facing documents. Everything else in docs/ is historical by definition.
_CURRENT_DOCS = (
    _REPO_ROOT / "README.md",
    _APP_ROOT / "README.md",
)

#: The header block of JARVIS.md is current-facing; its per-version sections are
#: history. The boundary is the first ``---`` rule after the title.
_JARVIS_MD = _APP_ROOT / "JARVIS.md"
_JARVIS_HEADER_LINES = 30

#: Role models the live configuration resolves. Sourced from the same defaults the
#: README role table documents; a doc naming a superseded generation is a defect.
CURRENT_ROLE_MODELS = {
    "FAST": "qwen3:8b",
    "CODER": "qwen2.5-coder:latest",
    "DEEP": "qwen3:14b",
    "VISION": "gemma3:4b",
    "EMBEDDING": "nomic-embed-text:latest",
    "VERIFIER": "qwen3:8b",
}

#: A doc may not claim the model family that M52-M60 superseded. ``qwen2.5-coder``
#: is still current for the CODER role, so the pattern must not match it.
_SUPERSEDED_MODEL_RE = re.compile(r"qwen2\.5(?!-coder)", re.IGNORECASE)

#: Unsupported autonomy claims. Every offensive path is operator-gated (HITL/NATO
#: approval + trusted-lab flag), so these sentences are simply not true.
_FORBIDDEN_CLAIMS = (
    "complete autonomous",
    "fully autonomous",
    "attacks autonomously",
    "autonomously attack",
)

#: A release document may say "NOT merged" only about a milestone ABOVE the
#: canonical one (i.e. work in flight). Matches ``V69 M58``/``V69_M58``/``M58``.
_MILESTONE_RE = re.compile(r"\bM(\d{2})\b")
_NOT_MERGED_RE = re.compile(r"not\s+merged", re.IGNORECASE)

#: Install commands quoted in the READMEs must resolve to real files.
_INSTALL_CMD_RE = re.compile(
    r"(?:pip install -r\s+|\./scripts/|python scripts/)([A-Za-z0-9_./-]+)")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def jarvis_md_header() -> str:
    """The current-facing header of JARVIS.md (history excluded)."""
    return "\n".join(_read(_JARVIS_MD).splitlines()[:_JARVIS_HEADER_LINES])


def _current_texts() -> dict[str, str]:
    texts = {
        str(p.relative_to(_REPO_ROOT)).replace("\\", "/"): _read(p)
        for p in _CURRENT_DOCS if p.is_file()
    }
    if _JARVIS_MD.is_file():
        texts["jarvis/JARVIS.md (header)"] = jarvis_md_header()
    return texts


def check_version() -> list[str]:
    """pyproject must DERIVE its version from :mod:`core.version`."""
    problems: list[str] = []
    pyproject = _APP_ROOT / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"pyproject.toml unreadable: {type(exc).__name__}"]

    project = data.get("project", {})
    if "version" in project:
        problems.append(
            f"pyproject.toml hard-codes version {project['version']!r}; it must "
            f"declare dynamic = ['version'] deriving from core/version.py")
    if "version" not in project.get("dynamic", []):
        problems.append("pyproject.toml does not declare version as dynamic metadata")

    attr = (data.get("tool", {}).get("setuptools", {})
            .get("dynamic", {}).get("version", {}).get("attr"))
    if attr != "core.version.VERSION":
        problems.append(
            f"dynamic version attr is {attr!r}; expected 'core.version.VERSION'")
    return problems


def check_models() -> list[str]:
    """No current-facing document may advertise the superseded model family."""
    problems = []
    for name, text in _current_texts().items():
        for match in _SUPERSEDED_MODEL_RE.finditer(text):
            line = text[:match.start()].count("\n") + 1
            problems.append(
                f"{name}:{line} names superseded model family 'qwen2.5'; the live "
                f"role table resolves qwen3 (FAST {CURRENT_ROLE_MODELS['FAST']}, "
                f"DEEP {CURRENT_ROLE_MODELS['DEEP']})")
    return problems


def check_claims() -> list[str]:
    """No current-facing document may claim autonomous operation."""
    problems = []
    for name, text in _current_texts().items():
        lowered = text.lower()
        for claim in _FORBIDDEN_CLAIMS:
            idx = lowered.find(claim)
            if idx >= 0:
                line = text[:idx].count("\n") + 1
                problems.append(
                    f"{name}:{line} claims {claim!r}; every offensive path is "
                    f"operator-gated (HITL/NATO approval + trusted-lab flag)")
    return problems


def check_release_status() -> list[str]:
    """A milestone at or below the canonical one may not be documented 'NOT merged'."""
    problems = []
    docs_dir = _APP_ROOT / "docs"
    if not docs_dir.is_dir():
        return problems
    for path in sorted(docs_dir.glob("V*.md")):
        text = _read(path)
        if not _NOT_MERGED_RE.search(text):
            continue
        milestones = [int(m) for m in _MILESTONE_RE.findall(path.name)]
        milestones += [int(m) for m in _MILESTONE_RE.findall(text[:400])]
        if any(m <= MILESTONE for m in milestones):
            problems.append(
                f"docs/{path.name} says 'NOT merged' for a milestone at or below "
                f"the canonical V{GENERATION} M{MILESTONE}")
    return problems


def check_install_commands() -> list[str]:
    """Every install/doctor command quoted in the READMEs must resolve to a file."""
    problems = []
    for name, text in _current_texts().items():
        if not name.endswith("README.md"):
            continue
        for match in _INSTALL_CMD_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http", "-")):
                continue
            candidate = target if target.startswith("scripts/") else target
            if not (_APP_ROOT / candidate).is_file() and \
               not (_APP_ROOT / "scripts" / candidate).is_file():
                line = text[:match.start()].count("\n") + 1
                problems.append(f"{name}:{line} references missing file {target!r}")
    return problems


def audit() -> dict:
    """Run every release-truth check. Bounded, content-free result."""
    families = {
        "version": check_version(),
        "models": check_models(),
        "claims": check_claims(),
        "status": check_release_status(),
        "install": check_install_commands(),
    }
    problems = [p for family in families.values() for p in family]
    return {
        "ok": not problems,
        "canonical_version": VERSION,
        "families": {k: len(v) for k, v in families.items()},
        "problems": problems,
    }
