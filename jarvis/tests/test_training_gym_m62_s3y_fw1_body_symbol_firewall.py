"""V69 M62 S3Y.FW1 — the body-symbol firewall reaches eval-v6, and both halves matter.

WHAT WAS OPEN
-------------
``FORBIDDEN_BODY_SYMBOLS`` is the canonical list of names a scanned control-plane surface
may not cite, because citing one is how a body-bearing builder gets pulled into a
body-free document. It named the eval-v4 and eval-v5 builders. It did not name eval-v6's,
so the live holdout was guarded by the task-id scan and the free-text cap alone — never by
a symbol scan. S3Y.FW1 appends the two missing entries and nothing else.

WHY BOTH ENTRIES, NOT ONE
-------------------------
Each generation of the corpus is reachable through TWO surfaces: the function that holds
the material, and the wrapper that returns it. Protecting only the first leaves the second
a working citation, which is why every historical generation occupies two slots and why
:func:`test_the_wrapper_symbol_probe_goes_quiet_without_its_own_entry` exists separately
from its material twin.

APPEND-ONLY
-----------
The registry carries a standing contract — *appended to, never reordered* — because sealed
suites index it positionally. This suite asserts the historical prefix is still the
historical prefix, in order, and that the only delta is a two-entry suffix. The expected
prefix is written out here rather than sliced off the live tuple: a test that reads its
expectation out of the object under test cannot notice that object moving.

NON-VACUITY
-----------
"No leak was reported" also describes a scanner that reports nothing. Each positive probe
therefore has a mutation twin that removes ONE new entry from the registry, in memory, and
requires the SAME probe to fall silent. A contributor who drops either entry gets a red
test rather than a quiet hole.

BODY-FREE BY CONSTRUCTION
-------------------------
Every probe string here is synthetic. This file never opens the body-bearing corpus
builder — not to read it and not to parse it, because parsing it is reading it — imports
no corpus builder, materialises no pack, and enumerates no real evaluation task
identifier. The two v6 symbol strings it does carry are FUNCTION NAMES, which is precisely
what the registry protects and precisely what a test of the registry must be able to say.

A registry test does not need to open the exam to prove the registry is right, and a
regression that opened it to prove a name exists would defeat the thing it guards. Real
held-out identifiers stay the sole responsibility of the separate, canonical task-id
firewall; this suite neither restates that suite nor reads the id table in order to.

NOTHING HERE TRAINS, EVALUATES, LOADS WEIGHTS OR GENERATES A TOKEN. NO HOLDOUT IS SPENT.
"""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT
#: The generation S3Y.FW1 closed at. Pinned, not read live: see the
#: milestone-state test for why reading it through current.json overclaimed.
GEN13_SNAPSHOT_PATH = "state/m62/snapshots/0013-m62-s3x1-fresh-eval-v6-frozen.json"

#: The registry exactly as it stood before S3Y.FW1, written out on purpose.
HISTORICAL_PREFIX = (
    "corpus_v4_material", "corpus_v4(",
    "corpus_v5_material", "corpus_v5(",
)
#: What S3Y.FW1 appended, and all it appended.
APPENDED_SUFFIX = (
    "corpus_v6_material",
    "corpus_v6(",
)
V6_MATERIAL_SYMBOL, V6_WRAPPER_SYMBOL = APPENDED_SUFFIX

#: A path string only. This suite never opens it, by any mechanism: the registry is
#: checked against the registry, never against the exam it protects.
BODY_BEARING_SOURCE = "jarvis/scripts/build_evaluation_corpus.py"


# ── fixtures ─────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the control plane, so a probe never touches the real tree."""
    for rel in (V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.ARCHIVE_PATH,
                V.PROGRESS_PATH, V.HISTORY_INDEX_PATH, V.CURRENT_SCHEMA_PATH,
                V.SNAPSHOT_SCHEMA_PATH):
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, destination)
    for source in (REPO / V.SNAPSHOT_DIR).iterdir():
        destination = tmp_path / V.SNAPSHOT_DIR / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    return tmp_path


def _plane_from(root: Path) -> V.ControlPlane:
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    snapshot_path = root / current["latest_snapshot_path"]
    snapshot_bytes = snapshot_path.read_bytes()
    migration_path = root / V.MIGRATION_MANIFEST_PATH
    return V.ControlPlane(
        current=current,
        current_bytes=(root / V.CURRENT_PATH).read_bytes(),
        snapshot=json.loads(snapshot_bytes.decode("utf-8")),
        snapshot_bytes=snapshot_bytes,
        snapshot_path=snapshot_path,
        migration=json.loads(migration_path.read_text(encoding="utf-8")))


def _body_source_problems(root: Path, planted: str) -> list[str]:
    """Plant a SYNTHETIC line on a scanned surface and return the body-source refusals."""
    progress = root / V.PROGRESS_PATH
    progress.write_text(
        progress.read_text(encoding="utf-8") + f"\n{planted}\n", encoding="utf-8")
    report = V.Report()
    V.check_holdout_firewall(_plane_from(root), report)
    return [m for _, m in report.problems if "body source" in m]


# ══════════════════════════════════════════════════════════════════════════════
#  1. the registry extension is append-only
# ══════════════════════════════════════════════════════════════════════════════
def test_the_registry_still_opens_with_its_historical_prefix_in_order():
    """Sealed suites index this tuple positionally; the prefix may never move."""
    assert tuple(V.FORBIDDEN_BODY_SYMBOLS[:len(HISTORICAL_PREFIX)]) == HISTORICAL_PREFIX


def test_the_first_slot_is_unchanged_because_a_sealed_suite_indexes_it():
    assert V.FORBIDDEN_BODY_SYMBOLS[0] == "corpus_v4_material"


def test_the_appended_suffix_is_exactly_the_two_v6_symbols():
    assert tuple(V.FORBIDDEN_BODY_SYMBOLS[len(HISTORICAL_PREFIX):]) == APPENDED_SUFFIX


def test_the_registry_grew_by_two_entries_and_by_nothing_else():
    assert len(V.FORBIDDEN_BODY_SYMBOLS) == len(HISTORICAL_PREFIX) + len(APPENDED_SUFFIX)
    assert V.FORBIDDEN_BODY_SYMBOLS == HISTORICAL_PREFIX + APPENDED_SUFFIX


def test_the_registry_is_still_an_immutable_tuple_with_no_duplicates():
    assert isinstance(V.FORBIDDEN_BODY_SYMBOLS, tuple)
    assert len(set(V.FORBIDDEN_BODY_SYMBOLS)) == len(V.FORBIDDEN_BODY_SYMBOLS)


def test_every_generation_occupies_a_material_slot_and_a_wrapper_slot():
    """The shape that makes one-sided protection visible as a gap."""
    materials = [s for s in V.FORBIDDEN_BODY_SYMBOLS if s.endswith("_material")]
    wrappers = [s for s in V.FORBIDDEN_BODY_SYMBOLS if s.endswith("(")]
    assert len(materials) == len(wrappers) == 3
    assert ({V.body_symbol_version(s) for s in materials}
            == {V.body_symbol_version(s) for s in wrappers}
            == {"v4", "v5", "v6"})


def test_each_new_symbol_resolves_to_the_v6_generation():
    assert V.body_symbol_version(V6_MATERIAL_SYMBOL) == "v6"
    assert V.body_symbol_version(V6_WRAPPER_SYMBOL) == "v6"


def test_the_body_source_is_still_declared_body_bearing():
    assert BODY_BEARING_SOURCE in V.FORBIDDEN_BODY_SOURCES


# ══════════════════════════════════════════════════════════════════════════════
#  2. the scanner refuses both v6 surfaces
# ══════════════════════════════════════════════════════════════════════════════
def test_a_scanned_surface_citing_the_v6_material_symbol_is_refused(sandbox):
    problems = _body_source_problems(sandbox, V6_MATERIAL_SYMBOL)
    assert [m for m in problems if "eval-v6" in m]


def test_a_scanned_surface_citing_the_v6_wrapper_symbol_is_refused(sandbox):
    """Protecting the material alone would leave this citation working."""
    problems = _body_source_problems(sandbox, V6_WRAPPER_SYMBOL)
    assert [m for m in problems if "eval-v6" in m]


def test_the_refusal_names_the_synthetic_symbol_it_refused(sandbox):
    """A refusal must be actionable, so it names the symbol that triggered it.

    The symbol is synthetic and is a function name. Whether a refusal could ever carry a
    real held-out identifier is the canonical task-id firewall's question, and it is not
    re-asked here: asking it would mean enumerating the identifiers to look for.
    """
    problems = _body_source_problems(sandbox, V6_MATERIAL_SYMBOL)
    assert any(V6_MATERIAL_SYMBOL in m for m in problems)


# ══════════════════════════════════════════════════════════════════════════════
#  3. non-vacuity: each new entry is load-bearing on its own
# ══════════════════════════════════════════════════════════════════════════════
def test_the_material_symbol_probe_goes_quiet_without_its_own_entry(sandbox, monkeypatch):
    monkeypatch.setattr(
        V, "FORBIDDEN_BODY_SYMBOLS",
        tuple(s for s in V.FORBIDDEN_BODY_SYMBOLS if s != V6_MATERIAL_SYMBOL))
    assert _body_source_problems(sandbox, V6_MATERIAL_SYMBOL) == []


def test_the_wrapper_symbol_probe_goes_quiet_without_its_own_entry(sandbox, monkeypatch):
    monkeypatch.setattr(
        V, "FORBIDDEN_BODY_SYMBOLS",
        tuple(s for s in V.FORBIDDEN_BODY_SYMBOLS if s != V6_WRAPPER_SYMBOL))
    assert _body_source_problems(sandbox, V6_WRAPPER_SYMBOL) == []


def test_removing_the_material_entry_leaves_the_wrapper_entry_working(sandbox,
                                                                     monkeypatch):
    """The two entries are independent, so neither can be inferred from the other."""
    monkeypatch.setattr(
        V, "FORBIDDEN_BODY_SYMBOLS",
        tuple(s for s in V.FORBIDDEN_BODY_SYMBOLS if s != V6_MATERIAL_SYMBOL))
    assert [m for m in _body_source_problems(sandbox, V6_WRAPPER_SYMBOL) if "eval-v6" in m]


def test_removing_the_wrapper_entry_leaves_the_material_entry_working(sandbox,
                                                                     monkeypatch):
    monkeypatch.setattr(
        V, "FORBIDDEN_BODY_SYMBOLS",
        tuple(s for s in V.FORBIDDEN_BODY_SYMBOLS if s != V6_WRAPPER_SYMBOL))
    assert [m for m in _body_source_problems(sandbox, V6_MATERIAL_SYMBOL)
            if "eval-v6" in m]


def test_the_historical_generations_are_still_refused_too(sandbox):
    """The append must not have displaced what was already protected."""
    problems = _body_source_problems(sandbox, HISTORICAL_PREFIX[0])
    assert [m for m in problems if "eval-v4" in m]


# ══════════════════════════════════════════════════════════════════════════════
#  4. negative control, at the scanner's OWN semantics
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("harmless", [
    "corpus_v6",                 # the stem alone is neither entry
    "corpus_v7_material",        # a generation that does not exist
    "build_corpus_v6_manifest",  # a name that merely contains the stem
    "corpus_v6 (spaced)",        # the stem without the call punctuation
])
def test_a_neighbouring_synthetic_string_is_not_a_body_source(sandbox, harmless):
    assert _body_source_problems(sandbox, harmless) == []


def test_the_scanner_matches_by_substring_and_this_suite_does_not_change_that(sandbox):
    """Recorded, not redesigned: an embedding string IS a citation, by design.

    ``corpus_v6_materialised`` contains the protected name, so the scanner refuses it.
    That is the existing containment rule every generation has been held to, and the
    negative controls above are chosen to respect it rather than to soften it.
    """
    assert [m for m in _body_source_problems(sandbox, "corpus_v6_materialised")
            if "eval-v6" in m]


# ══════════════════════════════════════════════════════════════════════════════
#  5. this suite depends on no real exam material
# ══════════════════════════════════════════════════════════════════════════════
def test_this_suite_carries_no_authority_token_and_no_private_path():
    """Applied with the control plane's OWN patterns, never a weaker second opinion.

    Whether any tracked file names a real held-out task identifier is the canonical
    task-id firewall's question and stays there. A regression that answered it here would
    have to iterate the identifiers to search for them, which is the disclosure it claims
    to be preventing, and a failure would print the ones it found.
    """
    text = Path(__file__).read_text(encoding="utf-8")
    assert V.TOKEN_LITERAL_RE.search(text) is None
    assert not V.PRIVATE_PATH_RE.findall(text)


def test_this_suite_imports_no_corpus_builder_and_materialises_no_pack():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    assert not [name for name in imported
                if "build_evaluation_corpus" in name or "task_pack" in name]
    assert not [name for name in imported
                if name.split(".")[0] in {"torch", "transformers", "peft", "trl"}]


def test_this_milestone_spends_nothing_and_measures_nothing():
    """S3Y.FW1 is security hardening: the scientific state must be exactly as it was.

    RESCOPED AT S3Y. Read through the SEALED generation 13 rather than `current.json`.
    FW1 changed nothing scientific, and that is permanently a claim about generation 13;
    read live it also asserted, silently, that no later generation existed, which S3Y's
    authorised spend made false without making FW1 any less hardening-only.
    """
    snapshot = json.loads(
        (REPO / GEN13_SNAPSHOT_PATH).read_text(encoding="utf-8"))
    assert snapshot["state_generation"] == 13
    candidate = next(c for c in snapshot["candidates"]
                     if c["candidate_id"] == V.CANDIDATE_004_ID)
    assert candidate["status"] == "TRAINED_UNEVALUATED"
    assert candidate["evaluation_corpus"] is None
    assert candidate["evaluation_receipt"] is None
    for version in ("v5", "v6"):
        entry = next(d for d in snapshot["datasets"]
                     if d["dataset_id"] == "m62-defensive-eval"
                     and d["version"] == version)
        assert entry["status"] == "FROZEN_UNUSED"
        assert entry["spent_by"] is None
