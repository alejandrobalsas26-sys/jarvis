"""core/dependency_authority.py — V69 M61.3: ONE declared dependency authority.

THE PROBLEM THIS SOLVES
-----------------------
Before M61 the repository maintained THREE independent dependency lists:

  1. ``requirements/*.txt``  — seven profiles, what the installers actually run;
  2. ``pyproject.toml``      — ``dependencies`` + ``optional-dependencies`` extras;
  3. ``requirements.txt``    — a 170-line legacy monolith mixing text-mode, OCR, RAG
                               and GUI-automation/offensive packages in one flat list.

They had already drifted: the ``soc`` extra was missing eight packages the profile
installs (``stix2``, ``mmh3``, ``ntplib``, ``piexif``, ``ijson``, ``igraph``,
``pyserial``, ``pyserial-asyncio``) and the ``lab`` extra was missing seven more
(``pymetasploit3``, ``grpcio``, ``grpcio-tools``, ``sliver-py``, ``qrcode``,
``pygetwindow``, ``python-telegram-bot``). ``pip install jarvis[soc]`` and
``pip install -r requirements/soc.txt`` produced different environments.

THE RULE
--------
``requirements/<profile>.txt`` is the DECLARED AUTHORITY. ``pyproject.toml`` MIRRORS
it and is verified — never the other way round. This module is the verifier; it is
pure stdlib parsing with no network, no pip invocation and no environment mutation.

WHAT IT CHECKS
--------------
  * every profile parses and its ``-r`` includes resolve inside ``requirements/``;
  * no profile declares the same normalized package name twice;
  * each pyproject extra equals its profile exactly (name AND specifier);
  * the base ``dependencies`` list equals ``requirements/base.txt``;
  * the offensive/lab and GUI-automation packages never appear in base;
  * the legacy ``requirements.txt`` is a deprecated pointer, not a rival list.

Names are normalized per PEP 503 so ``python_whois`` / ``python-whois`` and
``PyYAML`` / ``pyyaml`` cannot hide a duplicate.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_REQ_DIR = _APP_ROOT / "requirements"
_PYPROJECT = _APP_ROOT / "pyproject.toml"
_LEGACY = _APP_ROOT / "requirements.txt"

#: The closed set of installable profiles. ``base`` is the text-mode floor.
PROFILES = ("base", "voice", "docs", "soc", "lab", "dev", "all",
            "training", "training-cuda")

#: Packages that must NEVER be reachable from a base (text-mode) install: global
#: hotkey capture, GUI automation, MITM interception, C2 and exploitation clients.
FORBIDDEN_IN_BASE = frozenset({
    "keyboard", "pyautogui", "pygetwindow", "mss", "mitmproxy",
    "pymetasploit3", "sliver-py", "shodan", "docker", "playwright",
    "opencv-python", "python-telegram-bot", "grpcio", "grpcio-tools",
    # V69 M62 S3A — the ML training stack. Multi-gigabyte, and none of it is needed to
    # run JARVIS: it exists only to fine-tune, which is an explicit operator decision.
    # `pip install jarvis` must not download a torch build.
    "torch", "transformers", "datasets", "accelerate", "peft", "trl",
    "bitsandbytes",
})

#: pyproject extras that mirror a requirements profile 1:1. ``all`` is excluded: it
#: is expressed with self-referential extras (``jarvis[voice,docs,...]``), which is
#: a different but equivalent encoding, checked separately by :func:`_audit_all`.
_MIRRORED = ("voice", "docs", "soc", "lab", "dev")

#: Profiles that mirror a requirements file 1:1 exactly like :data:`_MIRRORED`, but which
#: ``all`` deliberately does NOT compose. Kept separate so the two rules stay honest: the
#: mirror check applies to them, the "``all`` references every sibling" check does not.
#: ``jarvis[all]`` is the full workstation, not the full *training* workstation — nobody
#: installing a chat UI has consented to a multi-gigabyte torch download.
_STANDALONE_MIRRORED = ("training", "training-cuda")

_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


def normalize(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, keeping any PEP 508 environment marker."""
    return line.split("#", 1)[0].rstrip()


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Parse one requirement line into ``(normalized_name, canonical_spec)``.

    ``canonical_spec`` keeps the extras, the version specifier and any environment
    marker, whitespace-normalized and with quotes unified, so ``pywintrace>=0.2.0 ;
    platform_system == "Windows"`` compares equal to the single-quoted TOML form.
    Returns ``None`` for blanks, comments and ``-r`` / ``-c`` directives.
    """
    text = _strip_comment(line).strip()
    if not text or text.startswith("-"):
        return None
    m = _REQ_LINE_RE.match(text)
    if not m:
        return None
    name, extras, rest = m.group(1), m.group(2) or "", m.group(3) or ""
    spec = f"{name}{extras}{rest}"
    spec = re.sub(r"\s*;\s*", " ; ", spec)
    spec = re.sub(r"\s+", " ", spec).replace('"', "'").strip()
    return normalize(name), spec


def _read_profile(name: str, *, _seen: set[str] | None = None) -> list[tuple[str, str]]:
    """Direct requirements of one profile, EXCLUDING transitively included files.

    ``-r base.txt`` includes are resolved by :func:`resolve_profile`; this returns
    only what the file itself declares, which is exactly what a pyproject extra
    should mirror.
    """
    path = _REQ_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"missing dependency profile: requirements/{name}.txt")
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_requirement(line)
        if parsed is not None:
            out.append(parsed)
    return out


def profile_includes(name: str) -> list[str]:
    """The profile names pulled in by this profile's ``-r`` directives."""
    path = _REQ_DIR / f"{name}.txt"
    if not path.is_file():
        return []
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = _strip_comment(raw).strip()
        if line.startswith("-r "):
            target = line[3:].strip()
            if "/" in target or "\\" in target or ".." in target:
                raise ValueError(f"profile {name}: unsafe include {target!r}")
            found.append(target.removesuffix(".txt"))
    return found


def resolve_profile(name: str) -> dict[str, str]:
    """Fully resolved ``{normalized_name: spec}`` for a profile, includes expanded."""
    resolved: dict[str, str] = {}
    stack, seen = [name], set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(profile_includes(current))
        for pkg, spec in _read_profile(current):
            resolved.setdefault(pkg, spec)
    return resolved


def _pyproject() -> dict:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _specs(items) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        parsed = parse_requirement(str(item))
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


def duplicate_declarations() -> dict[str, list[str]]:
    """``{profile: [duplicated names]}`` — a package declared twice in one file."""
    dupes: dict[str, list[str]] = {}
    for profile in PROFILES:
        seen, repeated = set(), []
        for pkg, _spec in _read_profile(profile):
            if pkg in seen and pkg not in repeated:
                repeated.append(pkg)
            seen.add(pkg)
        if repeated:
            dupes[profile] = sorted(repeated)
    return dupes


def _audit_mirrors() -> list[str]:
    """Extras that must match their profile exactly, name AND specifier."""
    problems: list[str] = []
    data = _pyproject()
    project = data.get("project", {})
    extras = project.get("optional-dependencies", {})

    base_profile = {n: s for n, s in _read_profile("base")}
    base_meta = _specs(project.get("dependencies"))
    problems += _diff("dependencies", base_meta, base_profile, "requirements/base.txt")

    for extra in _MIRRORED + _STANDALONE_MIRRORED:
        declared = _specs(extras.get(extra))
        profile = {n: s for n, s in _read_profile(extra)}
        problems += _diff(f"extra[{extra}]", declared, profile,
                          f"requirements/{extra}.txt")
    return problems


def _diff(label: str, declared: dict[str, str], authority: dict[str, str],
          source: str) -> list[str]:
    problems = []
    for missing in sorted(set(authority) - set(declared)):
        problems.append(f"{label}: missing {missing!r} declared in {source}")
    for extra in sorted(set(declared) - set(authority)):
        problems.append(f"{label}: {extra!r} not declared in {source}")
    for pkg in sorted(set(declared) & set(authority)):
        if declared[pkg] != authority[pkg]:
            problems.append(
                f"{label}: {pkg!r} specifier {declared[pkg]!r} != {authority[pkg]!r} "
                f"in {source}")
    return problems


def _audit_all() -> list[str]:
    """``all`` uses self-referential extras; verify the union and the extra-only tail."""
    problems: list[str] = []
    extras = _pyproject().get("project", {}).get("optional-dependencies", {})
    declared_raw = [str(x) for x in extras.get("all", ())]
    self_refs = {r for r in declared_raw if r.lower().startswith("jarvis[")}
    if not self_refs:
        problems.append("extra[all]: must reference the sibling extras via jarvis[...]")
    else:
        referenced = set()
        for ref in self_refs:
            inner = ref[ref.index("[") + 1:ref.rindex("]")]
            referenced |= {p.strip() for p in inner.split(",") if p.strip()}
        for expected in _MIRRORED:
            if expected not in referenced:
                problems.append(f"extra[all]: does not reference extra {expected!r}")

    tail_declared = _specs(r for r in declared_raw if not r.lower().startswith("jarvis["))
    union = set()
    for profile in _MIRRORED + ("base",):
        union |= set(resolve_profile(profile))
    tail_authority = {n: s for n, s in _read_profile("all") if n not in union}
    problems += _diff("extra[all]", tail_declared, tail_authority,
                      "requirements/all.txt")
    return problems


def _audit_base_purity() -> list[str]:
    """No offensive / GUI-automation package may be reachable from a base install."""
    base = set(resolve_profile("base"))
    leaked = sorted(base & {normalize(p) for p in FORBIDDEN_IN_BASE})
    return [f"base profile must not contain lab/GUI package {p!r}" for p in leaked]


def _audit_legacy() -> list[str]:
    """The legacy monolith must be a deprecated POINTER, never a rival list."""
    if not _LEGACY.is_file():
        return []
    own = _read_profile_path(_LEGACY)
    if own:
        return [f"legacy requirements.txt still declares {len(own)} packages directly; "
                f"it must only `-r requirements/all.txt`"]
    includes = [
        _strip_comment(line).strip()[3:].strip()
        for line in _LEGACY.read_text(encoding="utf-8").splitlines()
        if _strip_comment(line).strip().startswith("-r ")
    ]
    if not includes:
        return ["legacy requirements.txt includes no profile"]
    return []


def _read_profile_path(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_requirement(line)
        if parsed is not None:
            out.append(parsed)
    return out


def audit() -> dict:
    """Run every dependency-authority check. Bounded, content-free result.

    Returns ``{"ok": bool, "problems": [str], "profiles": {name: count}}``. Problem
    strings name packages and files only — never a path outside the repository.
    """
    problems: list[str] = []
    try:
        for profile, dupes in duplicate_declarations().items():
            problems += [f"requirements/{profile}.txt declares {d!r} twice"
                         for d in dupes]
        problems += _audit_mirrors()
        problems += _audit_all()
        problems += _audit_base_purity()
        problems += _audit_legacy()
    except (OSError, ValueError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        problems.append(f"{type(exc).__name__}: {exc}")

    counts = {}
    for profile in PROFILES:
        try:
            counts[profile] = len(resolve_profile(profile))
        except (OSError, ValueError, FileNotFoundError):
            counts[profile] = -1
    return {"ok": not problems, "problems": problems, "profiles": counts}
