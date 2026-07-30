"""tests/test_sbom_v69_m618.py — V69 M61.8: the SBOM describes THIS release.

A stale SBOM is worse than none: it is an authoritative-looking document about a
dependency set that no longer exists. The two things it gets wrong first are the project
version and the profile attribution, so both are pinned here, together with the
properties that make the document usable as release evidence at all —
byte-reproducibility, PEP 503 normalization, profile coverage and content safety.

The dependency-authority tests are the other half: every authoritative profile is either
represented or explicitly deferred, and nothing can be added to ``base`` without the
SBOM and the base-purity rule both noticing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import release_evidence, release_facts, sbom
from core.dependency_authority import (
    FORBIDDEN_IN_BASE,
    PROFILES,
    normalize,
    resolve_profile,
)
from core.version import VERSION

_APP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def base_sbom() -> dict:
    return sbom.build_cyclonedx("base")


@pytest.fixture(scope="module")
def mandatory_sboms() -> dict:
    return sbom.build_all(list(sbom.MANDATORY_PROFILES))


# ══════════════════════════════════════════════════════════════════════════════
#  It parses, it conforms, and the validator can refuse
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("profile", sbom.MANDATORY_PROFILES)
def test_each_mandatory_profile_generates_a_valid_sbom(profile: str):
    document = sbom.build_cyclonedx(profile)
    assert sbom.validate_sbom(document, expected_profile=profile) == []


@pytest.mark.parametrize("profile", sbom.MANDATORY_PROFILES)
def test_a_generated_sbom_round_trips_through_json(profile: str):
    document = sbom.build_cyclonedx(profile)
    reloaded = json.loads(sbom.serialize(document))
    assert sbom.validate_sbom(reloaded, expected_profile=profile) == []
    assert reloaded == document


def test_it_declares_cyclonedx_1_5(base_sbom: dict):
    assert base_sbom["bomFormat"] == "CycloneDX"
    assert base_sbom["specVersion"] == "1.5"
    assert isinstance(base_sbom["version"], int)
    assert base_sbom["serialNumber"].startswith("urn:uuid:")


def test_the_package_count_is_nonzero_and_plausible(mandatory_sboms: dict):
    for profile, document in mandatory_sboms.items():
        count = len(document["components"])
        assert count > 0, f"{profile} SBOM has no components"
        assert count == len(resolve_profile(profile)), \
            f"{profile} SBOM does not describe the whole resolved profile"


@pytest.mark.parametrize("mutation", [
    {"bomFormat": "SPDX"},
    {"specVersion": "1.4"},
    {"version": "1"},
    {"serialNumber": "not-a-urn"},
    {"components": []},
    {"dependencies": []},
])
def test_the_validator_refuses_a_broken_document(base_sbom: dict, mutation: dict):
    """Non-vacuity: a validator that accepts everything is decoration."""
    damaged = {**base_sbom, **mutation}
    assert sbom.validate_sbom(damaged) != [], f"{mutation} was accepted"


def test_the_validator_refuses_a_wrong_project_version(base_sbom: dict):
    damaged = json.loads(sbom.serialize(base_sbom))
    damaged["metadata"]["component"]["version"] = "63.0.0"
    problems = sbom.validate_sbom(damaged)
    assert any("canonical" in p for p in problems), problems


def test_the_validator_refuses_a_mislabelled_profile(base_sbom: dict):
    assert sbom.validate_sbom(base_sbom, expected_profile="soc") != []


def test_the_validator_refuses_an_unnormalized_component_name(base_sbom: dict):
    damaged = json.loads(sbom.serialize(base_sbom))
    damaged["components"][0]["name"] = "PyYAML"
    assert any("normalized" in p for p in sbom.validate_sbom(damaged))


def test_the_validator_refuses_a_duplicate_bom_ref(base_sbom: dict):
    damaged = json.loads(sbom.serialize(base_sbom))
    damaged["components"][1]["bom-ref"] = damaged["components"][0]["bom-ref"]
    assert any("duplicate" in p for p in sbom.validate_sbom(damaged))


def test_the_validator_refuses_unsorted_components(base_sbom: dict):
    damaged = json.loads(sbom.serialize(base_sbom))
    damaged["components"] = list(reversed(damaged["components"]))
    assert any("sorted" in p for p in sbom.validate_sbom(damaged))


def test_the_validator_refuses_a_dishonest_resolution_mode(base_sbom: dict):
    damaged = json.loads(sbom.serialize(base_sbom))
    for prop in damaged["metadata"]["properties"]:
        if prop["name"] == "jarvis:resolution_mode":
            prop["value"] = "fully-pinned"
    assert any("resolution_mode" in p for p in sbom.validate_sbom(damaged))


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError, match="unknown dependency profile"):
        sbom.build_cyclonedx("definitely-not-a-profile")


# ══════════════════════════════════════════════════════════════════════════════
#  The canonical version
# ══════════════════════════════════════════════════════════════════════════════
def test_the_sbom_project_version_is_the_canonical_version(mandatory_sboms: dict):
    assert VERSION == "69.61.0", "this release's canonical version changed"
    for profile, document in mandatory_sboms.items():
        assert document["metadata"]["component"]["version"] == VERSION, profile
        assert document["metadata"]["component"]["purl"].endswith(f"@{VERSION}")


def test_the_application_component_is_the_project_not_a_library(base_sbom: dict):
    component = base_sbom["metadata"]["component"]
    assert component["type"] == "application"
    assert component["name"] == "jarvis"


# ══════════════════════════════════════════════════════════════════════════════
#  Determinism
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("profile", sbom.MANDATORY_PROFILES)
def test_generation_is_byte_reproducible(profile: str):
    first = sbom.serialize(sbom.build_cyclonedx(profile))
    second = sbom.serialize(sbom.build_cyclonedx(profile))
    assert first == second


def test_the_serial_number_is_derived_not_random():
    a = sbom.build_cyclonedx("base")["serialNumber"]
    b = sbom.build_cyclonedx("base")["serialNumber"]
    c = sbom.build_cyclonedx("soc")["serialNumber"]
    assert a == b, "a regenerated SBOM must not get a new serial"
    assert a != c, "two profiles must not share a serial"


def test_no_timestamp_is_embedded_unless_the_caller_supplies_one():
    assert "timestamp" not in sbom.build_cyclonedx("base")["metadata"]
    stamped = sbom.build_cyclonedx("base", timestamp="2026-07-30T00:00:00Z")
    assert stamped["metadata"]["timestamp"] == "2026-07-30T00:00:00Z"


def test_the_module_reads_no_clock_and_no_randomness():
    source = Path(sbom.__file__).read_text(encoding="utf-8")
    for forbidden in ("uuid.uuid4", "uuid.uuid1", "random.", "time.time",
                      "datetime.now", "utcnow"):
        assert forbidden not in source, f"{forbidden} breaks reproducibility"


# ══════════════════════════════════════════════════════════════════════════════
#  Profile attribution
# ══════════════════════════════════════════════════════════════════════════════
def test_every_component_is_attributed_to_the_profile_it_came_from(
        mandatory_sboms: dict):
    for profile, document in mandatory_sboms.items():
        for component in document["components"]:
            props = {p["name"]: p["value"] for p in component["properties"]}
            assert props["jarvis:profile"] == profile


def test_direct_and_inherited_declarations_are_distinguished():
    """`soc` includes `-r base.txt`; the two kinds must be visibly different."""
    document = sbom.build_cyclonedx("soc")
    kinds = {}
    for component in document["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        kinds[component["name"]] = props["jarvis:declaration"]
    assert kinds["yara-python"] == "direct"
    assert kinds["pydantic"] == "inherited:base"
    assert {"direct"} < set(kinds.values()), "no inherited entries were labelled"


def test_the_base_profile_has_no_inherited_entries():
    document = sbom.build_cyclonedx("base")
    for component in document["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        assert props["jarvis:declaration"] == "direct"


def test_the_declared_specifier_is_carried_verbatim():
    document = sbom.build_cyclonedx("base")
    authority = resolve_profile("base")
    for component in document["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        assert props["jarvis:declared_specifier"] == authority[component["name"]]


def test_an_environment_marker_is_preserved_rather_than_dropped():
    """`all` carries a Windows-only package; silently dropping the marker would lie."""
    document = sbom.build_cyclonedx("all")
    markers = {
        component["name"]: {p["name"]: p["value"]
                            for p in component["properties"]}
        for component in document["components"]
    }
    assert "platform_system" in markers["pywintrace"]["jarvis:environment_marker"]


def test_an_unresolved_version_is_labelled_not_faked():
    """`pydantic>=2.0.0` is a bound, not a version; recording it as one would lie."""
    document = sbom.build_cyclonedx("base")
    for component in document["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        if props["jarvis:version_source"] == "unresolved":
            assert "version" not in component
            assert "@" not in component["purl"]


def test_observing_installed_versions_labels_them_and_installs_nothing():
    document = sbom.build_cyclonedx("dev", observe_installed=True)
    modes = {p["name"]: p["value"] for p in document["metadata"]["properties"]}
    assert modes["jarvis:resolution_mode"] == "declared+observed"
    for component in document["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        if "version" in component:
            assert props["jarvis:version_source"] in (
                "pinned-specifier", "observed-installed")
    source = Path(sbom.__file__).read_text(encoding="utf-8")
    for forbidden in ("pip install", "subprocess", "urlopen", "requests.",
                      "httpx.", "os.system", "shell=True"):
        assert forbidden not in source, f"the SBOM builder must not {forbidden!r}"


def test_the_closure_scope_is_stated_honestly(base_sbom: dict):
    props = {p["name"]: p["value"] for p in base_sbom["metadata"]["properties"]}
    assert props["jarvis:dependency_closure"] == "declared-only"
    assert "NOT resolved" in props["jarvis:dependency_closure_note"]


# ══════════════════════════════════════════════════════════════════════════════
#  Dependency authority coverage
# ══════════════════════════════════════════════════════════════════════════════
def test_every_authoritative_profile_is_represented_or_explicitly_deferred():
    covered = set(sbom.MANDATORY_PROFILES) | set(sbom.OPTIONAL_PROFILES)
    # `all` is deliberately deferred: it is the union of the others and would triple
    # the bundle size to say nothing new. Stated, not silently omitted.
    deferred = {"all"}
    assert covered | deferred == set(PROFILES), (
        f"unaccounted profiles: {sorted(set(PROFILES) - covered - deferred)}"
    )


def test_the_mandatory_profiles_match_the_declared_release_facts():
    assert sbom.MANDATORY_PROFILES == release_facts.SBOM_MANDATORY_PROFILES
    assert sbom.OPTIONAL_PROFILES == release_facts.SBOM_OPTIONAL_PROFILES
    assert "base" in sbom.MANDATORY_PROFILES, "the text-mode floor must be described"


@pytest.mark.parametrize("profile", ("voice", "docs", "lab", "all"))
def test_an_optional_or_deferred_profile_still_generates_cleanly(profile: str):
    """Deferred from the default bundle, not broken — a consumer can ask for it."""
    document = sbom.build_cyclonedx(profile)
    assert sbom.validate_sbom(document, expected_profile=profile) == []


def test_no_undeclared_dependency_can_reach_base():
    """The base SBOM must never contain a lab/GUI-automation package."""
    names = {normalize(n) for n in sbom.profile_component_names(
        sbom.build_cyclonedx("base"))}
    leaked = names & {normalize(p) for p in FORBIDDEN_IN_BASE}
    assert not leaked, f"base SBOM contains lab/GUI packages: {sorted(leaked)}"


def test_the_base_sbom_equals_the_dependency_authority_exactly():
    """The SBOM is generated FROM the authority, so any difference is a bug in it."""
    assert {c["name"] for c in sbom.build_cyclonedx("base")["components"]} == \
        set(resolve_profile("base"))


def test_defusedxml_is_present_in_soc_where_m617_declared_it():
    """A concrete regression: the M61.7 hardening dependency must be described."""
    names = set(sbom.profile_component_names(sbom.build_cyclonedx("soc")))
    assert "defusedxml" in names


# ══════════════════════════════════════════════════════════════════════════════
#  Content safety
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("profile", PROFILES)
def test_no_sbom_leaks_a_private_path_or_secret(profile: str):
    document = sbom.build_cyclonedx(profile, observe_installed=True)
    assert release_evidence.scan_for_private_content(document) == []


def test_no_sbom_carries_an_index_url_or_interpreter_location(base_sbom: dict):
    text = sbom.serialize(base_sbom)
    for forbidden in ("pypi.org/simple", "site-packages", "Scripts", "bin/python",
                      "--index-url", "--extra-index-url", "PYTHONPATH"):
        assert forbidden not in text, f"SBOM leaks {forbidden!r}"


# ══════════════════════════════════════════════════════════════════════════════
#  Writing
# ══════════════════════════════════════════════════════════════════════════════
def test_writing_produces_one_named_file_per_profile(tmp_path: Path,
                                                     mandatory_sboms: dict):
    written = sbom.write_sboms(tmp_path, mandatory_sboms)
    assert set(written) == set(sbom.MANDATORY_PROFILES)
    for profile, path in written.items():
        assert path.name == f"sbom-cyclonedx-{profile}.json"
        assert path.parent == tmp_path
        assert "\r\n" not in path.read_bytes().decode("utf-8")


def test_nothing_is_written_into_the_repository_by_generating(tmp_path: Path):
    before = {p.name for p in _APP_ROOT.glob("sbom-*.json")}
    sbom.write_sboms(tmp_path, sbom.build_all(["base"]))
    after = {p.name for p in _APP_ROOT.glob("sbom-*.json")}
    assert before == after == set()


def test_build_all_refuses_to_return_an_invalid_document(monkeypatch):
    def _broken(profile, **kwargs):
        return {"bomFormat": "SPDX", "components": []}
    monkeypatch.setattr(sbom, "build_cyclonedx", _broken)
    with pytest.raises(ValueError, match="is invalid"):
        sbom.build_all(["base"])
