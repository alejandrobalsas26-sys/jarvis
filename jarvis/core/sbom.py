"""core/sbom.py — V69 M61.8: deterministic SBOM over the dependency authority.

WHAT IT DESCRIBES, AND WHAT IT HONESTLY DOES NOT
------------------------------------------------
M61.3 made ``requirements/<profile>.txt`` the single dependency authority and gave the
repository a verifier for it. This module turns that authority into a **CycloneDX 1.5
JSON** SBOM, one per profile.

It is a **declared-authority** SBOM. That distinction is stated in the document itself
(``jarvis:resolution_mode``) rather than left for a reader to assume:

  * it enumerates exactly what the project declares it depends on, per profile,
    including which entries the profile declares itself and which it inherits through
    a ``-r`` include;
  * it does **not** resolve the transitive installation closure. Doing that means
    running a real resolver against live indexes, which is neither offline,
    deterministic, nor possible without choosing a platform and an interpreter — and a
    per-platform pinned SBOM is a different artifact with different guarantees. The
    document says so in ``jarvis:dependency_closure``;
  * when ``observe_installed`` is set, a concrete version is attached for packages that
    are importable in the *current* environment, marked per component. Nothing is
    installed to obtain it, and no optional profile is installed into the operator's
    environment to make its SBOM look more complete.

DETERMINISM
-----------
Same inputs, byte-identical output. Components are sorted by normalized name;
properties are sorted; the ``serialNumber`` is a UUIDv5 derived from the profile and
the content rather than a random UUID; the timestamp is injected by the caller, never
read from the clock here. Two runs on the same tree therefore diff to nothing, which is
what makes an SBOM usable as release evidence rather than as noise.

CONTENT SAFETY
--------------
Package names, PEP 508 specifiers, profile names and the canonical project version
only. No absolute path, no environment value, no interpreter location, no index URL,
no credential. Verified by ``tests/test_sbom_v69_m618.py`` with the same deny-scanner
the release evidence uses.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from core import release_facts
from core.dependency_authority import (
    PROFILES,
    _read_profile,
    normalize,
    profile_includes,
    resolve_profile,
)
from core.version import VERSION

SBOM_SCHEMA_VERSION = "m61.8.1"
BOM_FORMAT = "CycloneDX"
SPEC_VERSION = "1.5"
PRODUCT_NAME = "jarvis"

TOOL_VENDOR = "jarvis"
TOOL_NAME = "core.sbom"
TOOL_VERSION = SBOM_SCHEMA_VERSION

#: Profiles an M61.8 release bundle must contain. ``base`` is the text-mode floor,
#: ``dev`` is the toolchain that produced the evidence, ``soc`` is the largest
#: defensive profile — together they cover what a consumer needs to audit.
MANDATORY_PROFILES: tuple[str, ...] = release_facts.SBOM_MANDATORY_PROFILES

#: Generated when asked. Deterministic for the same reason the mandatory ones are:
#: nothing is resolved against a network. ``all`` is intentionally absent — it is the
#: union of the others and would triple the bundle size to say nothing new.
OPTIONAL_PROFILES: tuple[str, ...] = release_facts.SBOM_OPTIONAL_PROFILES

#: Fixed namespace for the deterministic serial numbers. A constant, not a secret.
_SERIAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://jarvis.invalid/sbom")

_VERSION_ONLY_RE = re.compile(r"^==\s*([A-Za-z0-9][A-Za-z0-9.\-+!]*)$")


def sbom_filename(profile: str) -> str:
    return f"sbom-cyclonedx-{profile}.json"


def _pinned_version(spec: str, name: str) -> str | None:
    """The concrete version when the specifier pins one exactly, else ``None``.

    ``pydantic==2.7.1`` yields ``2.7.1``; ``pydantic>=2.0.0`` yields ``None`` — a lower
    bound is not a version, and recording it as one would make the SBOM claim a
    resolution it never performed.
    """
    tail = spec[len(name):] if spec.lower().startswith(name.lower()) else spec
    tail = tail.split(";", 1)[0].strip()
    match = _VERSION_ONLY_RE.match(tail)
    return match.group(1) if match else None


def _observed_version(name: str) -> str | None:
    """Installed version of ``name`` in the CURRENT environment, or ``None``.

    Reads installed metadata only. It never installs, downloads, resolves or contacts
    an index, and an absent package is a normal answer rather than an error.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover — stdlib since 3.8
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — malformed third-party metadata is not our bug
        return None


def _environment_marker(spec: str) -> str | None:
    if ";" not in spec:
        return None
    return spec.split(";", 1)[1].strip() or None


def _extras(spec: str, name: str) -> str | None:
    match = re.search(r"\[([^\]]*)\]", spec[:len(name) + 64])
    return match.group(1) if match else None


def _properties(pairs: dict[str, str | None]) -> list[dict]:
    """CycloneDX properties, sorted, with empty values dropped."""
    return [{"name": key, "value": str(value)}
            for key, value in sorted(pairs.items()) if value not in (None, "")]


def build_cyclonedx(profile: str, *, timestamp: str | None = None,
                    observe_installed: bool = False) -> dict:
    """A CycloneDX 1.5 JSON SBOM for one dependency profile.

    ``timestamp`` is injected (ISO-8601 UTC) or omitted entirely — this function never
    reads the clock, so its output is reproducible.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown dependency profile: {profile!r}")

    resolved = resolve_profile(profile)
    direct = {name for name, _spec in _read_profile(profile)}
    inherited_from: dict[str, str] = {}
    for included in profile_includes(profile):
        for name in resolve_profile(included):
            inherited_from.setdefault(normalize(name), included)

    components: list[dict] = []
    for name in sorted(resolved):
        spec = resolved[name]
        pinned = _pinned_version(spec, name)
        observed = _observed_version(name) if observe_installed else None
        concrete = pinned or observed
        purl = f"pkg:pypi/{name}" + (f"@{concrete}" if concrete else "")
        component: dict = {
            "type": "library",
            "bom-ref": f"pkg:pypi/{name}",
            "name": name,
            "purl": purl,
            "scope": "required",
            "properties": _properties({
                "jarvis:profile": profile,
                "jarvis:declared_specifier": spec,
                "jarvis:declaration": (
                    "direct" if name in direct
                    else f"inherited:{inherited_from.get(name, 'unknown')}"),
                "jarvis:version_source": (
                    "pinned-specifier" if pinned
                    else "observed-installed" if observed else "unresolved"),
                "jarvis:environment_marker": _environment_marker(spec),
                "jarvis:extras": _extras(spec, name),
            }),
        }
        if concrete:
            component["version"] = concrete
        components.append(component)

    application_ref = f"pkg:pypi/{PRODUCT_NAME}@{VERSION}"
    document: dict = {
        "bomFormat": BOM_FORMAT,
        "specVersion": SPEC_VERSION,
        "version": 1,
        "metadata": {
            "tools": [{
                "vendor": TOOL_VENDOR, "name": TOOL_NAME, "version": TOOL_VERSION,
            }],
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": PRODUCT_NAME,
                "version": VERSION,
                "purl": application_ref,
                "description": (
                    "Local-first, operator-controlled AI workstation and authorized "
                    "Purple Team/SOC homelab platform."
                ),
            },
            "properties": _properties({
                "jarvis:sbom_schema_version": SBOM_SCHEMA_VERSION,
                "jarvis:profile": profile,
                "jarvis:release_stage": release_facts.RELEASE_STAGE,
                "jarvis:dependency_authority":
                    f"requirements/{profile}.txt",
                "jarvis:resolution_mode":
                    "declared+observed" if observe_installed else "declared",
                "jarvis:dependency_closure": "declared-only",
                "jarvis:dependency_closure_note": (
                    "Direct and inherited declarations from the dependency authority. "
                    "The transitive installation closure is NOT resolved: that needs a "
                    "live resolver, a fixed platform and a fixed interpreter, and is "
                    "deferred to a per-platform locked SBOM."
                ),
                "jarvis:component_count": str(len(components)),
                "jarvis:inherits_profiles":
                    ",".join(sorted(profile_includes(profile))) or None,
            }),
        },
        "components": components,
        "dependencies": [{
            "ref": application_ref,
            "dependsOn": [c["bom-ref"] for c in components],
        }],
    }
    if timestamp:
        document["metadata"]["timestamp"] = timestamp
    document["serialNumber"] = _serial_number(profile, document)
    return document


def _serial_number(profile: str, document: dict) -> str:
    """A DETERMINISTIC ``urn:uuid`` derived from the profile and the component set.

    ``uuid4()`` would make every regeneration diff, which defeats the purpose of a
    reproducible SBOM. A UUIDv5 over the content means the serial changes exactly when
    the dependency set changes.
    """
    payload = json.dumps(
        {"profile": profile, "version": VERSION,
         "components": [c["bom-ref"] for c in document.get("components", ())],
         "specifiers": [
             p["value"]
             for c in document.get("components", ())
             for p in c.get("properties", ())
             if p["name"] == "jarvis:declared_specifier"
         ]},
        sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"urn:uuid:{uuid.uuid5(_SERIAL_NAMESPACE, digest)}"


def serialize(document: dict) -> str:
    """Canonical JSON for an SBOM: sorted keys, two-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


# ══════════════════════════════════════════════════════════════════════════════
#  Validation
# ══════════════════════════════════════════════════════════════════════════════
def validate_sbom(document: object, *, expected_profile: str | None = None
                  ) -> list[str]:
    """Structural problems in an SBOM. Empty list means it conforms.

    Deliberately strict about the two things a stale SBOM gets wrong: the project
    version and the profile attribution.
    """
    problems: list[str] = []
    if not isinstance(document, dict):
        return ["$: SBOM must be a JSON object"]

    if document.get("bomFormat") != BOM_FORMAT:
        problems.append(f"$.bomFormat: expected {BOM_FORMAT!r}")
    if document.get("specVersion") != SPEC_VERSION:
        problems.append(f"$.specVersion: expected {SPEC_VERSION!r}")
    if not isinstance(document.get("version"), int):
        problems.append("$.version: must be an integer")
    serial = document.get("serialNumber")
    if not (isinstance(serial, str) and serial.startswith("urn:uuid:")):
        problems.append("$.serialNumber: must be a 'urn:uuid:' string")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        return problems + ["$.metadata: missing"]

    component = metadata.get("component")
    if not isinstance(component, dict):
        problems.append("$.metadata.component: missing")
    else:
        if component.get("name") != PRODUCT_NAME:
            problems.append(f"$.metadata.component.name: expected {PRODUCT_NAME!r}")
        if component.get("version") != VERSION:
            problems.append(
                f"$.metadata.component.version: {component.get('version')!r} != "
                f"canonical {VERSION!r}")

    props = {p.get("name"): p.get("value")
             for p in metadata.get("properties", ()) if isinstance(p, dict)}
    profile = props.get("jarvis:profile")
    if not profile:
        problems.append("$.metadata.properties: no 'jarvis:profile'")
    elif expected_profile is not None and profile != expected_profile:
        problems.append(
            f"$.metadata.properties['jarvis:profile']: {profile!r} != "
            f"{expected_profile!r}")
    if props.get("jarvis:resolution_mode") not in ("declared", "declared+observed"):
        problems.append("$.metadata.properties: no honest 'jarvis:resolution_mode'")

    components = document.get("components")
    if not isinstance(components, list) or not components:
        return problems + ["$.components: must be a non-empty array"]

    names, refs = [], set()
    for index, entry in enumerate(components):
        where = f"$.components[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be an object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"{where}.name: missing")
            continue
        if normalize(name) != name:
            problems.append(f"{where}.name: {name!r} is not PEP 503 normalized")
        names.append(name)
        ref = entry.get("bom-ref")
        if not isinstance(ref, str) or not ref:
            problems.append(f"{where}.bom-ref: missing")
        elif ref in refs:
            problems.append(f"{where}.bom-ref: duplicate {ref!r}")
        else:
            refs.add(ref)
        purl = entry.get("purl")
        if not (isinstance(purl, str) and purl.startswith("pkg:pypi/")):
            problems.append(f"{where}.purl: must start with 'pkg:pypi/'")
        entry_props = {p.get("name") for p in entry.get("properties", ())
                       if isinstance(p, dict)}
        for required in ("jarvis:profile", "jarvis:declared_specifier",
                         "jarvis:declaration"):
            if required not in entry_props:
                problems.append(f"{where}.properties: missing {required!r}")

    if names != sorted(names):
        problems.append("$.components: not sorted by name (output is not deterministic)")

    deps = document.get("dependencies")
    if not isinstance(deps, list) or not deps:
        problems.append("$.dependencies: must be a non-empty array")
    return problems


def profile_component_names(document: dict) -> list[str]:
    return [c.get("name") for c in document.get("components", ())
            if isinstance(c, dict)]


def build_all(profiles: list[str], *, timestamp: str | None = None,
              observe_installed: bool = False) -> dict[str, dict]:
    """``{profile: sbom}`` for each requested profile, validated before returning."""
    out: dict[str, dict] = {}
    for profile in profiles:
        document = build_cyclonedx(profile, timestamp=timestamp,
                                   observe_installed=observe_installed)
        problems = validate_sbom(document, expected_profile=profile)
        if problems:
            raise ValueError(
                f"generated SBOM for {profile!r} is invalid: " + "; ".join(problems))
        out[profile] = document
    return out


def write_sboms(output_dir: Path, documents: dict[str, dict]) -> dict[str, Path]:
    """Write each SBOM into ``output_dir``. Returns ``{profile: path}``."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for profile, document in sorted(documents.items()):
        path = output_dir / sbom_filename(profile)
        path.write_text(serialize(document), encoding="utf-8", newline="\n")
        written[profile] = path
    return written
