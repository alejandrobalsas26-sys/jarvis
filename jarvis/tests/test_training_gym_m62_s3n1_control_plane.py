"""V69 M62 S3N.1 — the Control Plane V2 trust boundary, and what it refuses.

WHAT THESE TESTS ARE FOR
------------------------
Before S3N.1, "what is true about M62 right now" was a 6089-line prose document, and the
only way to act on it was to believe it. The migration moved the current facts into a
small hash-chained snapshot and put a verifier in front of them. That buys nothing unless
the verifier actually refuses things, so this file is mostly about refusal.

Five ways the new architecture could be quietly worthless, each measured here:

  * **A lossy migration.** The archive is asserted byte-identical to the pre-migration
    ``PROGRESS.md``, its digest is pinned by two independent witnesses, and every one of
    the old document's twenty-two sections is asserted present in the migration coverage
    table. No orphans.
  * **A schema that accepts anything.** Unknown keys, unknown candidate states, unknown
    dataset states, malformed digests, negative generations and missing evidence are each
    asserted *rejected*, by both the built-in strict validator and — where the library is
    present — ``jsonschema``, with the two required to agree.
  * **A state machine with no teeth.** Every illegal candidate and dataset transition is
    asserted refused, including the three that matter most:
    ``NOT_CREATED -> PROMOTED``, ``EVALUATED_NOT_ELIGIBLE -> PROMOTED`` and
    ``USED_IMMUTABLE -> FROZEN_UNUSED``.
  * **Documentation that reads as capability.** The control plane is asserted unable to
    grant TRAIN, EVAL or promotion: authority-shaped keys are refused, token-shaped values
    are refused, and a fake ``TRAIN authorized: true`` planted in prose is asserted to
    create nothing.
  * **A firewall with a hole in it.** No bootstrap surface may name an individual
    ``eval-v4`` task, cite a body-bearing source, or carry a body-shaped key or an
    over-long free-text value.

NOTHING HERE TRAINS, EVALUATES, LOADS A MODEL OR TOKENIZER, OR GENERATES A TOKEN.

This file reads no ``eval-v4`` task body and contains none. The holdout firewall is tested
through the body-free set identities and path-level rules the S3N freeze declared, never
by opening the material it protects.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_m62_control_plane as V

REPO = V.REPO_ROOT

# The pre-migration PROGRESS.md, pinned. Two independent witnesses record this digest —
# the state snapshot and the migration manifest — and the verifier compares both against
# the bytes. It is written here a third time on purpose: a test that reads the expected
# value out of the artefact it is checking proves nothing.
PRE_MIGRATION_PROGRESS_SHA256 = (
    "e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf")
PRE_MIGRATION_PROGRESS_BYTES = 516_784
PRE_MIGRATION_PROGRESS_LINES = 6_089

#: Generation 1, sealed by S3N.1 and never revised.
GENESIS_SNAPSHOT_SHA256 = (
    "a2659d1fb1031726329394f0593478eb57b273048bc0d94faf12c89225dcf2c3")
SUBJECT_STATE_COMMIT = "ec446e348995acb0c23a69b0c3efd574f821b1a0"
MASTER_COMMIT = "3705114228edef2f665be349c5c4429b7b16777a"


# ── fixtures ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def current() -> dict:
    return json.loads((REPO / V.CURRENT_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot(current) -> dict:
    return json.loads(
        (REPO / current["latest_snapshot_path"]).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((REPO / V.MIGRATION_MANIFEST_PATH).read_text(encoding="utf-8"))


@pytest.fixture()
def plane() -> V.ControlPlane:
    report = V.Report()
    loaded = V.load(report)
    assert loaded is not None and not report.problems
    return loaded


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A writable copy of the control plane, so a mutation never touches the real tree.

    ``V.REPO_ROOT`` is a module global every check reads, so pointing it at the copy is
    enough to redirect the file-reading checks. The git-backed checks are not exercised
    through this fixture; they have their own tests against the real repository.
    """
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


def _categories(report: V.Report) -> set[str]:
    return {category for category, _ in report.problems}


def _rewrite(root: Path, rel: str, payload: dict) -> None:
    (root / rel).write_bytes(V.canonical_bytes(payload))


def _repoint(root: Path) -> None:
    """Re-point current.json at the snapshot's real digest, so a mutation under test is
    isolated from the pointer check it would otherwise also trip."""
    current = json.loads((root / V.CURRENT_PATH).read_text(encoding="utf-8"))
    data = (root / current["latest_snapshot_path"]).read_bytes()
    current["latest_snapshot_sha256"] = V.sha256_bytes(data)
    _rewrite(root, V.CURRENT_PATH, current)


VERIFIER_SOURCE = (REPO / V.VERIFIER_PATH).read_text(encoding="utf-8")
VERIFIER_TREE = ast.parse(VERIFIER_SOURCE)


# ── 1. the archive is exactly the document it replaced ───────────────────────────────
def test_the_archive_is_byte_identical_to_the_pre_migration_progress():
    archive = REPO / V.ARCHIVE_PATH
    data = archive.read_bytes()
    assert len(data) == PRE_MIGRATION_PROGRESS_BYTES
    assert data.count(b"\n") == PRE_MIGRATION_PROGRESS_LINES
    assert hashlib.sha256(data).hexdigest() == PRE_MIGRATION_PROGRESS_SHA256


def test_the_archive_is_the_progress_blob_that_was_committed_at_the_subject_commit():
    """The strongest available proof: Git's own content-addressed object for the file the
    migration copied, compared against the archive's bytes rather than against a hash
    someone recorded."""
    committed = subprocess.run(  # nosec B603 B607 - fixed argv, read-only plumbing
        ["git", "-C", str(REPO), "show", f"{SUBJECT_STATE_COMMIT}:PROGRESS.md"],
        capture_output=True, check=True)
    assert hashlib.sha256(committed.stdout).hexdigest() == PRE_MIGRATION_PROGRESS_SHA256
    assert committed.stdout == (REPO / V.ARCHIVE_PATH).read_bytes()


def test_both_witnesses_pin_the_same_archive_digest(snapshot, manifest):
    assert snapshot["archive"]["sha256"] == PRE_MIGRATION_PROGRESS_SHA256
    assert manifest["archive_sha256"] == PRE_MIGRATION_PROGRESS_SHA256
    assert manifest["source_progress_sha256"] == PRE_MIGRATION_PROGRESS_SHA256
    assert manifest["archive_bytes_equal_source"] is True


def test_one_changed_byte_in_the_archive_fails_verification(sandbox):
    archive = sandbox / V.ARCHIVE_PATH
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0x20  # flip one bit of one character
    archive.write_bytes(bytes(data))
    report = V.Report()
    V.check_archive(_plane_from(sandbox), report)
    assert "ARCHIVE_INTEGRITY" in _categories(report)


def test_a_missing_archive_fails_rather_than_being_skipped(sandbox):
    (sandbox / V.ARCHIVE_PATH).unlink()
    report = V.Report()
    V.check_archive(_plane_from(sandbox), report)
    assert "ARCHIVE_INTEGRITY" in _categories(report)


def test_the_two_archive_witnesses_disagreeing_is_itself_a_failure(sandbox):
    manifest = json.loads(
        (sandbox / V.MIGRATION_MANIFEST_PATH).read_text(encoding="utf-8"))
    manifest["archive_sha256"] = "0" * 64
    _rewrite(sandbox, V.MIGRATION_MANIFEST_PATH, manifest)
    report = V.Report()
    V.check_archive(_plane_from(sandbox), report)
    assert "ARCHIVE_INTEGRITY" in _categories(report)


def test_the_archive_digest_does_not_depend_on_the_host():
    """SHA-256 over bytes, read twice, with no path, user or clock in the input."""
    first = V.sha256_file(REPO / V.ARCHIVE_PATH)
    second = V.sha256_bytes((REPO / V.ARCHIVE_PATH).read_bytes())
    assert first == second == PRE_MIGRATION_PROGRESS_SHA256


# ── 2. the schema is strict ──────────────────────────────────────────────────────────
def test_the_published_schemas_are_the_ones_the_verifier_enforces():
    assert (REPO / V.CURRENT_SCHEMA_PATH).read_bytes() == \
        V.canonical_bytes(V.current_schema())
    assert (REPO / V.SNAPSHOT_SCHEMA_PATH).read_bytes() == \
        V.canonical_bytes(V.snapshot_schema())


def test_both_live_documents_validate(current, snapshot):
    assert V.validate_against_schema(V.current_schema(), current) == []
    assert V.validate_against_schema(V.snapshot_schema(), snapshot) == []


def test_every_security_critical_object_refuses_unknown_keys():
    def walk(node, path="$"):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, \
                f"{path} would accept unknown keys"
            for key, sub in node.get("properties", {}).items():
                walk(sub, f"{path}.{key}")
        if isinstance(node, dict):
            for key in ("items",):
                if key in node:
                    walk(node[key], f"{path}[]")
            for branch in node.get("oneOf", []):
                walk(branch, path)
    walk(V.snapshot_schema())
    walk(V.current_schema())


def test_an_unknown_top_level_field_is_rejected(snapshot):
    mutated = dict(snapshot, forward_compatible_extra="hello")
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_an_unknown_candidate_state_is_rejected(snapshot):
    mutated = copy.deepcopy(snapshot)
    mutated["candidates"][0]["status"] = "PROBABLY_FINE"
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_an_unknown_dataset_state_is_rejected(snapshot):
    mutated = copy.deepcopy(snapshot)
    mutated["datasets"][0]["status"] = "READY"
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_a_missing_dataset_status_is_not_read_as_fresh(snapshot):
    mutated = copy.deepcopy(snapshot)
    del mutated["datasets"][3]["status"]
    problems = V.validate_against_schema(V.snapshot_schema(), mutated)
    assert problems != []


def test_a_malformed_digest_is_rejected(snapshot):
    for bad in ("not-a-hash", "ABC" * 21 + "D", "0" * 63, ""):
        mutated = copy.deepcopy(snapshot)
        mutated["archive"]["sha256"] = bad
        assert V.validate_against_schema(V.snapshot_schema(), mutated) != [], bad


def test_a_malformed_commit_is_rejected(snapshot):
    mutated = copy.deepcopy(snapshot)
    mutated["subject_state_commit"] = SUBJECT_STATE_COMMIT[:39]
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_a_negative_or_zero_generation_is_rejected(snapshot):
    for bad in (0, -1):
        mutated = dict(snapshot, state_generation=bad)
        assert V.validate_against_schema(V.snapshot_schema(), mutated) != [], bad


def test_a_schema_version_change_must_be_explicit(snapshot):
    mutated = dict(snapshot, schema_version="m62.control_plane.2")
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_the_snapshot_may_not_claim_a_merge_tag_or_release(snapshot):
    for key in ("merged_into_master", "tagged", "released"):
        mutated = copy.deepcopy(snapshot)
        mutated["project"][key] = True
        assert V.validate_against_schema(V.snapshot_schema(), mutated) != [], key


def test_the_builtin_validator_refuses_a_schema_keyword_it_cannot_enforce():
    """A keyword this validator does not implement must not read as satisfied."""
    problems = V.validate_against_schema({"type": "string", "format": "email"}, "x")
    assert problems and "unsupported keyword" in problems[0]


def test_the_two_validators_agree_on_the_live_documents(current, snapshot):
    jsonschema = pytest.importorskip("jsonschema")
    for schema, payload in ((V.current_schema(), current),
                            (V.snapshot_schema(), snapshot)):
        library = list(jsonschema.Draft202012Validator(schema).iter_errors(payload))
        assert library == []
        assert V.validate_against_schema(schema, payload) == []


def test_the_two_validators_agree_that_a_corrupted_snapshot_is_invalid(snapshot):
    jsonschema = pytest.importorskip("jsonschema")
    mutated = copy.deepcopy(snapshot)
    mutated["datasets"][3]["status"] = "READY"
    library = list(jsonschema.Draft202012Validator(
        V.snapshot_schema()).iter_errors(mutated))
    assert library != []
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


# ── 3. the pointer and the chain ─────────────────────────────────────────────────────
def test_the_current_pointer_digest_is_the_snapshot_bytes(current, plane):
    assert current["latest_snapshot_sha256"] == V.sha256_bytes(plane.snapshot_bytes)


def test_one_changed_byte_in_the_snapshot_breaks_the_pointer(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["control_plane_note"] = mutated["control_plane_note"] + "."
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    report = V.Report()
    V.check_current_pointer(_plane_from(sandbox), report)
    assert "CURRENT_POINTER" in _categories(report)


def test_the_snapshot_on_disk_is_already_in_canonical_form(plane):
    assert plane.snapshot_bytes == V.canonical_bytes(plane.snapshot)
    assert plane.current_bytes == V.canonical_bytes(plane.current)


def test_a_non_canonical_snapshot_is_refused(sandbox):
    plane = _plane_from(sandbox)
    (sandbox / plane.current["latest_snapshot_path"]).write_text(
        json.dumps(plane.snapshot), encoding="utf-8")
    _repoint(sandbox)
    report = V.Report()
    V.check_current_pointer(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" in _categories(report)


def test_the_canonical_serialization_is_deterministic_and_host_independent():
    a = {"b": 1, "a": [3, 2, 1], "c": {"z": None, "y": True}}
    b = {"c": {"y": True, "z": None}, "a": [3, 2, 1], "b": 1}
    assert V.canonical_json(a) == V.canonical_json(b)
    assert V.canonical_json(a) == V.canonical_json(a)
    assert V.canonical_json(a).endswith("\n")
    assert V.canonical_json(a).count("\n") == V.canonical_json(a).rstrip("\n").count(
        "\n") + 1
    assert str(REPO) not in V.canonical_json(a)


def test_the_canonical_serializer_refuses_non_finite_numbers():
    with pytest.raises(ValueError):
        V.canonical_json({"x": float("nan")})


def test_there_is_exactly_one_canonical_serializer():
    """Production and tests must not hash through two different implementations."""
    names = {node.name for node in ast.walk(VERIFIER_TREE)
             if isinstance(node, ast.FunctionDef)}
    assert "canonical_json" in names
    dumps_calls = [n for n in ast.walk(VERIFIER_TREE)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "dumps"]
    assert len(dumps_calls) == 1, "json.dumps is called somewhere other than canonical_json"


def test_the_genesis_generation_is_one_with_a_null_parent():
    """Generation 1 specifically, read by name.

    Originally written against the CURRENT snapshot, which was generation 1 at the time.
    A genesis assertion must name the genesis file, or it silently becomes an assertion
    that the control plane has never advanced.
    """
    genesis = next(p for p in sorted((REPO / V.SNAPSHOT_DIR).iterdir())
                   if p.name.startswith("0001-"))
    payload = json.loads(genesis.read_text(encoding="utf-8"))
    assert payload["state_generation"] == 1
    assert payload["parent_snapshot_sha256"] is None
    # And it is still byte-for-byte the snapshot S3N.1 sealed.
    assert V.sha256_bytes(genesis.read_bytes()) == GENESIS_SNAPSHOT_SHA256


def test_a_genesis_snapshot_claiming_a_parent_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = dict(plane.snapshot, parent_snapshot_sha256="a" * 64)
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" in _categories(report)


def test_a_skipped_generation_is_refused(sandbox):
    """1 -> 3 must not validate."""
    plane = _plane_from(sandbox)
    skipped = plane.snapshot["state_generation"] + 2
    third = dict(copy.deepcopy(plane.snapshot), state_generation=skipped,
                 parent_snapshot_sha256=V.sha256_bytes(plane.snapshot_bytes))
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/{skipped:04d}-skipped.json", third)
    current = dict(plane.current, state_generation=skipped,
                   latest_snapshot_path=f"{V.SNAPSHOT_DIR}/{skipped:04d}-skipped.json")
    _rewrite(sandbox, V.CURRENT_PATH, current)
    _repoint(sandbox)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" in _categories(report)
    assert any("increment by 1" in m for _, m in report.problems)


def test_a_duplicate_generation_is_refused_as_a_two_writer_race(sandbox):
    plane = _plane_from(sandbox)
    generation = plane.snapshot["state_generation"]
    twin = dict(copy.deepcopy(plane.snapshot),
                generation_label="M62_CONTROL_PLANE_GENERATION_TWIN")
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/{generation:04d}-twin.json", twin)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert any("ONE WRITER PER GENERATION" in m for _, m in report.problems)


def test_a_broken_parent_link_is_refused(sandbox):
    plane = _plane_from(sandbox)
    following = plane.snapshot["state_generation"] + 1
    second = dict(copy.deepcopy(plane.snapshot), state_generation=following,
                  parent_snapshot_sha256="b" * 64)
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/{following:04d}-next.json", second)
    current = dict(plane.current, state_generation=following,
                   latest_snapshot_path=f"{V.SNAPSHOT_DIR}/{following:04d}-next.json")
    _rewrite(sandbox, V.CURRENT_PATH, current)
    _repoint(sandbox)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert any("parent_snapshot_sha256" in m for _, m in report.problems)


def test_a_correct_second_generation_chains_cleanly(sandbox):
    """The chain check must be able to pass, or its failures prove nothing."""
    plane = _plane_from(sandbox)
    following = plane.snapshot["state_generation"] + 1
    second = dict(copy.deepcopy(plane.snapshot), state_generation=following,
                  generation_label="M62_SYNTHETIC_NEXT",
                  parent_snapshot_sha256=V.sha256_bytes(plane.snapshot_bytes))
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/{following:04d}-next.json", second)
    current = dict(plane.current, state_generation=following,
                   latest_snapshot_path=f"{V.SNAPSHOT_DIR}/{following:04d}-next.json")
    _rewrite(sandbox, V.CURRENT_PATH, current)
    _repoint(sandbox)
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert "SNAPSHOT_CHAIN" not in _categories(report)


def test_the_filename_generation_must_match_the_payload(sandbox):
    plane = _plane_from(sandbox)
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/0007-mislabelled.json",
             dict(copy.deepcopy(plane.snapshot), state_generation=2))
    report = V.Report()
    V.check_snapshot_chain(_plane_from(sandbox), report)
    assert any("filename says generation" in m for _, m in report.problems)


# ── 4. Git is the authority for Git facts ────────────────────────────────────────────
def test_the_subject_commit_exists_and_head_descends_from_it(plane):
    report = V.Report()
    V.check_git_authority(plane, report)
    assert "GIT_AUTHORITY" not in _categories(report)


def test_the_snapshot_names_the_subject_commit_and_master(snapshot, current):
    # The subject commit MOVES with every state-bearing milestone; what must hold is that
    # both planes name the SAME one and that Git recognises it as a real ancestor of HEAD.
    assert snapshot["subject_state_commit"] == current["subject_state_commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", snapshot["subject_state_commit"])
    assert snapshot["project"]["master_commit"] == MASTER_COMMIT
    assert snapshot["project"]["branch"] == "jarvis-v69-m62-training-gym"


def test_a_snapshot_claiming_the_wrong_master_fails_against_git(plane):
    mutated = copy.deepcopy(plane.snapshot)
    mutated["project"]["master_commit"] = "0" * 40
    report = V.Report()
    V.check_git_authority(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert "GIT_AUTHORITY" in _categories(report)


def test_a_snapshot_naming_a_commit_that_does_not_exist_fails(plane):
    mutated = dict(copy.deepcopy(plane.snapshot), subject_state_commit="f" * 40)
    report = V.Report()
    V.check_git_authority(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert "GIT_AUTHORITY" in _categories(report)


def test_no_state_bearing_production_path_changed_since_the_subject_commit(plane):
    report = V.Report()
    V.check_stale_state(plane, report)
    assert "STALE_STATE" not in _categories(report)


def test_the_stale_state_detector_names_a_real_path_set():
    for candidate in V.STATE_BEARING_PRODUCTION:
        target = REPO / candidate
        assert target.exists(), f"{candidate} is not a path in this repository"


def test_progress_and_the_pointer_cannot_drift_apart(plane):
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert plane.current["latest_snapshot_sha256"] in text
    assert plane.current["latest_snapshot_path"] in text
    assert plane.snapshot["subject_state_commit"] in text
    assert plane.snapshot["archive"]["sha256"] in text


# ── 5. candidate and dataset state ───────────────────────────────────────────────────
def test_candidate_001_is_evaluated_not_eligible(snapshot):
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == "qwen3-06b-lora-quality-live-001")
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["evaluation_corpus"] == "m62-defensive-eval v2"
    assert entry["adapter_sha256"] == \
        "43213035c15cd38928d2d6a3bdbd9af96872a954801c6bfd0a9b82a8e22ac858"


def test_candidate_002_is_evaluated_not_eligible(snapshot):
    entry = next(c for c in snapshot["candidates"]
                 if c["candidate_id"] == "qwen3-06b-lora-quality-live-002")
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["evaluation_corpus"] == "m62-defensive-eval v3"
    assert entry["adapter_sha256"] == \
        "319c252498ba51e01ed59f58fc20ae639e2d886bf67277d3aa6df2e9f9665409"


def test_candidate_003_is_measured_and_backed_by_both_receipts(snapshot):
    """S3O moved ordinal 3 NOT_CREATED -> DESIGNED_UNTRAINED, S3P -> TRAINED_UNEVALUATED,
    S3Q.0.2 -> EVALUATED_NOT_ELIGIBLE.

    RENAMED and re-pointed at S3Q.0.2, and not a weakening. This test reads the LIVE
    snapshot on purpose -- it owns "the control plane models this candidate coherently",
    not "candidate 003 has not been measured yet", which stopped being true the moment
    eval-v4 was spent.

    **What it owns is the pairing, and that got STRICTER:** an evaluated candidate must
    name the corpus that measured it AND carry a portable evaluation receipt, because a
    state the control plane cannot back is the state it must not be able to claim. Both
    receipts are asserted here; `check_evaluation_receipt` re-derives what they say.

    NOT ELIGIBLE is a result, not a promotion in waiting: the terminal state is asserted
    exactly, so a future edit to ELIGIBLE_FOR_HUMAN_REVIEW fails here.
    """
    entry = next(c for c in snapshot["candidates"] if c["ordinal"] == 3)
    assert entry["status"] == "EVALUATED_NOT_ELIGIBLE"
    assert entry["adapter_sha256"] is not None
    assert entry["adapter_manifest_hash"] is not None
    assert entry["training_receipt"] is not None
    assert entry["evaluation_receipt"] is not None
    assert entry["evaluation_corpus"] == "m62-defensive-eval v4"
    assert "v2" in entry["training_corpus"]
    assert entry["candidate_id"] == "qwen3-06b-lora-quality-live-003"


def test_claiming_candidate_003_is_trained_without_evidence_fails_verification(sandbox):
    """The property this owns survives S3P intact, and matters more now than before.

    Candidate 003 really is trained, so the mutation can no longer be "flip the status
    word" -- it is "keep the word and take the evidence away". A state that a snapshot
    can assert on its own is a state the control plane cannot be trusted about, whether
    or not the assertion happens to be true today.
    """
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    entry = next(c for c in mutated["candidates"] if c["ordinal"] == 3)
    entry["status"] = "TRAINED_UNEVALUATED"
    entry["adapter_sha256"] = None
    entry["adapter_manifest_hash"] = None
    entry["training_receipt"] = None
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert "CANDIDATE_STATE" in _categories(report)
    V.check_training_receipt(_plane_from(sandbox), report)
    assert "TRAINING_RECEIPT" in _categories(report)


def test_a_promoted_candidate_is_refused_outright(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["candidates"][0]["status"] = "PROMOTED"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert any("cannot be witnessed" in m for _, m in report.problems)


def test_an_evaluated_candidate_without_evidence_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["candidates"][0]["evidence"] = None
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert any("deep evidence pointer" in m for _, m in report.problems)


def test_eval_v4_is_spent_and_names_what_spent_it(snapshot):
    """Was ``test_eval_v4_is_frozen_unused`` until S3Q spent it.

    The transition FROZEN_UNUSED -> USED_IMMUTABLE is ONE-WAY and has no edge back, so
    the live assertion is now the other half of the same invariant: a spent holdout is
    USED_IMMUTABLE and must NAME what spent it. A USED_IMMUTABLE holdout with a null
    ``spent_by`` is refused by ``check_dataset_state``; this asserts the live plane is
    on the right side of that.
    """
    entry = next(d for d in snapshot["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert entry["status"] == "USED_IMMUTABLE"
    assert entry["spent_by"] and "S3Q" in entry["spent_by"]
    assert entry["task_count"] == 36


def test_eval_v3_is_used_immutable(snapshot):
    entry = next(d for d in snapshot["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v3")
    assert entry["status"] == "USED_IMMUTABLE"
    assert entry["spent_by"]


def test_eval_v4_manifest_parent_and_pack_are_the_frozen_ones(snapshot):
    entry = next(d for d in snapshot["datasets"]
                 if d["dataset_id"] == "m62-defensive-eval" and d["version"] == "v4")
    assert entry["manifest_hash"] == \
        "8c6871b0094bdfc75062a6352d383fa8e9750c1425182a2b3248db20500081c5"
    assert entry["parent_manifest_hash"] == \
        "7c948236163198b5de451316e39346a37efcbc1254724f921e116a6c722f75a0"
    assert entry["pack_hash"] == V.EVAL_V4_PACK_HASH


def test_relabelling_eval_v4_as_fresh_fails_verification(sandbox):
    """INVERTED at S3Q.0.2, and the inversion is the stronger direction.

    Until S3Q this mutation wrote USED_IMMUTABLE over a fresh holdout. eval-v4 is now
    genuinely spent, so that mutation is a no-op and would assert nothing -- while the
    lie that actually matters became available: relabelling a SPENT holdout as fresh.
    That is the single most damaging edit anyone could make to this state, so it is the
    one this test now makes, and it must still be refused.
    """
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    entry = next(d for d in mutated["datasets"] if d["version"] == "v4")
    entry["status"] = "FROZEN_UNUSED"
    entry["spent_by"] = None
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_dataset_state(_plane_from(sandbox), report)
    assert "DATASET_STATE" in _categories(report)


def test_relabelling_a_spent_holdout_as_fresh_fails_verification(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    entry = next(d for d in mutated["datasets"] if d["version"] == "v3")
    entry["status"] = "FROZEN_UNUSED"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_dataset_state(_plane_from(sandbox), report)
    assert "DATASET_STATE" in _categories(report)


def test_a_moved_dataset_manifest_fails_verification(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    entry = next(d for d in mutated["datasets"] if d["version"] == "v4")
    entry["manifest_hash"] = "c" * 64
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_dataset_state(_plane_from(sandbox), report)
    assert "DATASET_STATE" in _categories(report)


# ── 6. the state machines refuse illegal jumps ───────────────────────────────────────
@pytest.mark.parametrize("before,after", [
    ("NOT_CREATED", "PROMOTED"),
    ("NOT_CREATED", "TRAINED_UNEVALUATED"),
    ("NOT_CREATED", "EVALUATED_NOT_ELIGIBLE"),
    ("DESIGNED_UNTRAINED", "EVALUATED_NOT_ELIGIBLE"),
    ("DESIGNED_UNTRAINED", "PROMOTED"),
    ("TRAINED_UNEVALUATED", "PROMOTED"),
    ("EVALUATED_NOT_ELIGIBLE", "PROMOTED"),
    ("EVALUATED_NOT_ELIGIBLE", "TRAINED_UNEVALUATED"),
    ("EVALUATED_NOT_ELIGIBLE", "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"),
    ("EVALUATED_QUARANTINED", "PROMOTED"),
    ("PROMOTED", "NOT_CREATED"),
])
def test_an_illegal_candidate_transition_is_refused(before, after):
    assert V.transition_problems(before, after, V.CANDIDATE_TRANSITIONS, "candidate")


@pytest.mark.parametrize("before,after", [
    ("NOT_CREATED", "DESIGNED_UNTRAINED"),
    ("DESIGNED_UNTRAINED", "TRAINED_UNEVALUATED"),
    ("TRAINED_UNEVALUATED", "EVALUATED_NOT_ELIGIBLE"),
    ("TRAINED_UNEVALUATED", "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"),
    ("EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW", "PROMOTED"),
])
def test_a_legal_candidate_transition_is_permitted(before, after):
    assert V.transition_problems(before, after, V.CANDIDATE_TRANSITIONS,
                                 "candidate") == []


def test_an_unknown_candidate_state_has_no_transitions():
    assert V.transition_problems("MOSTLY_FINE", "PROMOTED", V.CANDIDATE_TRANSITIONS,
                                 "candidate")


def test_the_only_dataset_transition_is_fresh_to_spent():
    assert V.transition_problems("FROZEN_UNUSED", "USED_IMMUTABLE",
                                 V.DATASET_TRANSITIONS, "dataset") == []
    assert V.transition_problems("USED_IMMUTABLE", "FROZEN_UNUSED",
                                 V.DATASET_TRANSITIONS, "dataset")


def test_the_transition_tables_are_closed_over_their_vocabularies():
    assert set(V.CANDIDATE_TRANSITIONS) == set(V.CANDIDATE_STATES)
    for source, targets in V.CANDIDATE_TRANSITIONS.items():
        assert set(targets) <= set(V.CANDIDATE_STATES), source
    assert set(V.DATASET_TRANSITIONS) == set(V.DATASET_STATES)
    for source, targets in V.DATASET_TRANSITIONS.items():
        assert set(targets) <= set(V.DATASET_STATES), source


def test_the_dataset_vocabulary_keeps_the_distinction_that_matters():
    assert "FROZEN_UNUSED" in V.DATASET_STATES
    assert "USED_IMMUTABLE" in V.DATASET_STATES
    assert "UNKNOWN" not in V.DATASET_STATES
    assert "READY" not in V.DATASET_STATES
    assert "AVAILABLE" not in V.DATASET_STATES


def test_an_illegal_transition_between_two_generations_is_caught(sandbox):
    plane = _plane_from(sandbox)
    second = copy.deepcopy(plane.snapshot)
    second["state_generation"] = 2
    second["generation_label"] = "M62_SYNTHETIC_SECOND"
    second["parent_snapshot_sha256"] = V.sha256_bytes(plane.snapshot_bytes)
    entry = next(d for d in second["datasets"] if d["version"] == "v3")
    entry["status"] = "FROZEN_UNUSED"
    _rewrite(sandbox, f"{V.SNAPSHOT_DIR}/0002-next.json", second)
    current = dict(plane.current, state_generation=2,
                   latest_snapshot_path=f"{V.SNAPSHOT_DIR}/0002-next.json")
    _rewrite(sandbox, V.CURRENT_PATH, current)
    _repoint(sandbox)
    report = V.Report()
    V.check_candidate_state(_plane_from(sandbox), report)
    assert any("is not in the allowed table" in m for _, m in report.problems)


# ── 7. the policy identities are re-derived, not read ────────────────────────────────
def test_the_policy_digests_re_derive_from_the_production_classes(plane):
    report = V.Report()
    V.check_policy_identities(plane, report)
    assert "POLICY_IDENTITIES" not in _categories(report)


def test_the_recorded_policy_digests_are_the_frozen_ones(snapshot):
    policies = snapshot["policy_identities"]
    assert policies["gate_policy_hash"] == \
        "e50033194afeb7680815b1f11268cce4e0fe1549c4334c8257883603ea8f73c5"
    assert policies["metric_policy_hash"] == \
        "e07dd133419978396d7ada706bab20b35b6250982c21a0ea7933750e9cd72e1a"
    assert policies["generation_policy_hash"] == \
        "c6b0b682805898971618ae738bce3b0843484b541a66c67efc0c55aa6f37a2d7"
    assert policies["reasoning_policy"] == "DISABLED"
    assert policies["max_new_tokens"] == 512


def test_a_changed_gate_policy_digest_fails_verification(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["policy_identities"]["gate_policy_hash"] = "d" * 64
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_policy_identities(_plane_from(sandbox), report)
    assert "POLICY_IDENTITIES" in _categories(report)


def test_the_configured_and_default_generation_policies_stay_different(snapshot):
    """The S3N reconciliation: two correct digests for two different objects."""
    policies = snapshot["policy_identities"]
    assert policies["generation_policy_hash"] != \
        policies["generation_policy_constructor_default_hash"]
    assert policies["generation_policy_constructor_default_hash"] == \
        "1b4696d6cc278f7a778f5c3917f015635efb076d81e5143235778ca8f8de2fc9"


def test_d37_is_fixed_d38_is_fixed_and_d39_is_open(snapshot):
    defects = {d["id"]: d for d in snapshot["defects"]}
    assert defects["D37"]["status"] == "FIXED"
    assert defects["D38"]["status"] == "FIXED_OBSERVABILITY_ONLY"
    assert defects["D39"]["status"] == "OPEN"


def test_d38_is_not_a_gate_in_the_state_or_in_the_source(snapshot):
    defects = {d["id"]: d for d in snapshot["defects"]}
    assert defects["D38"]["is_gate"] is False
    assert all(d["is_gate"] is False for d in snapshot["defects"])
    gates = (REPO / "jarvis" / "training_gym" / "evaluation" / "gates.py").read_text(
        encoding="utf-8")
    assert "output_budget_exhaust" not in gates
    assert "finish_reason" not in gates


def test_a_defect_status_that_contradicts_the_milestone_authority_fails(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    next(d for d in mutated["defects"] if d["id"] == "D39")["status"] = "FIXED"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_policy_identities(_plane_from(sandbox), report)
    assert "POLICY_IDENTITIES" in _categories(report)


def test_the_open_defects_that_still_bind_are_all_present(snapshot):
    ids = {d["id"] for d in snapshot["defects"]}
    assert {"D28", "D29", "D33", "D37", "D38", "D39"} <= ids


# ── 8. prose and state cannot grant capability ───────────────────────────────────────
def test_the_snapshot_observes_authority_and_does_not_grant_it(snapshot):
    observation = snapshot["authority_observation"]
    assert observation["train"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["eval"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["promotion"] == "NONE_OBSERVED_IN_REPOSITORY"
    assert observation["control_plane_can_grant_authority"] is False


def test_the_observation_vocabulary_has_no_optimistic_default():
    assert "UNKNOWN" in V.AUTHORITY_OBSERVATIONS
    assert "NONE" not in V.AUTHORITY_OBSERVATIONS
    assert "CLEAN" not in V.AUTHORITY_OBSERVATIONS


def test_an_authority_shaped_key_is_refused_anywhere_in_the_state(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["authority_observation"]["train_authority"] = "GRANTED"
    report = V.Report()
    V.check_authority_separation(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert "AUTHORITY_SEPARATION" in _categories(report)


def test_a_token_shaped_value_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["control_plane_note"] = "TRAIN:" + "a" * 64
    report = V.Report()
    V.check_authority_separation(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert any("plan token" in m for _, m in report.problems)


def test_the_snapshot_declaring_it_can_grant_authority_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["authority_observation"]["control_plane_can_grant_authority"] = True
    report = V.Report()
    V.check_authority_separation(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert "AUTHORITY_SEPARATION" in _categories(report)
    assert V.validate_against_schema(V.snapshot_schema(), mutated) != []


def test_prose_claiming_authorisation_creates_no_capability(tmp_path):
    """A planted 'TRAIN authorized: true' is text. It mints nothing, and the only thing
    that could carry real capability — a token literal — is what the scan looks for."""
    planted = "TRAIN authorized: true\nEVAL authorized: yes\nPROMOTION: approved\n"
    assert V.TOKEN_LITERAL_RE.search(planted) is None
    assert V._authority_shaped({"note": planted}, "prose") == []
    real_token = "EVAL:" + "0123456789abcdef" * 4
    assert V.TOKEN_LITERAL_RE.search(real_token) is not None
    assert V._authority_shaped({"note": real_token}, "prose") != []


def test_authority_shaped_prose_is_flagged_as_ambiguous_and_still_grants_nothing(
        sandbox, monkeypatch):
    """The brief's hardest case: a planted grant must be *noticed* and *inert*."""
    progress = sandbox / V.PROGRESS_PATH
    progress.write_text(
        progress.read_text(encoding="utf-8") + "\nTRAIN authorized: true\n",
        encoding="utf-8")
    monkeypatch.setattr(V, "SCANNED_SURFACES", (V.PROGRESS_PATH,))
    report = V.Report()
    V.check_authority_separation(_plane_from(sandbox), report)
    assert any("AMBIGUOUS AUTHORITY CLAIM" in note for note in report.notes)
    # Flagged, not failed. The sandbox is not a git repository, so the tree-wide token
    # scan cannot run and says so — fail-closed, and the only problem raised here. The
    # planted prose itself contributes none.
    assert [m for _, m in report.problems] == [
        "the token-literal scan could not run, so the authority observation is UNKNOWN "
        "rather than none"]
    plane = _plane_from(sandbox)
    assert plane.snapshot["authority_observation"]["train"] == \
        "NONE_OBSERVED_IN_REPOSITORY"
    assert plane.snapshot["authority_observation"][
        "control_plane_can_grant_authority"] is False
    assert V.TOKEN_LITERAL_RE.search(progress.read_text(encoding="utf-8")) is None


def test_the_authority_claim_detector_does_not_fire_on_ordinary_prose():
    """It must survive the real documents, which talk about authority constantly."""
    for rel in V.SCANNED_SURFACES:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert V.AUTHORITY_CLAIM_RE.search(text) is None, rel
    assert V.AUTHORITY_CLAIM_RE.search("TRAIN authorized: true") is not None
    assert V.AUTHORITY_CLAIM_RE.search("EVAL authorisation granted = yes") is not None
    assert V.AUTHORITY_CLAIM_RE.search(
        "a fresh single-use TRAIN authority is required") is None


def test_no_tracked_file_carries_a_spendable_token_literal(plane):
    report = V.Report()
    V.check_authority_separation(plane, report)
    assert "AUTHORITY_SEPARATION" not in _categories(report)


def test_the_control_plane_documents_say_they_cannot_grant_authority():
    progress = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert "PROSE_CANNOT_GRANT_AUTHORITY" in progress
    assert "OBSERVATION, never a grant" in progress


# ── 9. path integrity ────────────────────────────────────────────────────────────────
def test_every_control_plane_file_is_a_regular_tracked_file(plane):
    report = V.Report()
    V.check_paths(plane, report)
    assert "PATH_INTEGRITY" not in _categories(report)


@pytest.mark.parametrize("rel_key", ["ARCHIVE_PATH", "CURRENT_PATH", "PROGRESS_PATH"])
def test_a_symlinked_control_plane_file_is_refused(sandbox, rel_key):
    rel = getattr(V, rel_key)
    target = sandbox / rel
    payload = target.read_bytes()
    decoy = sandbox / (rel + ".real")
    decoy.write_bytes(payload)
    target.unlink()
    try:
        target.symlink_to(decoy)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("this host cannot create symlinks")
    report = V.Report()
    V.check_paths(_plane_from(sandbox), report)
    assert any("symlink" in m for _, m in report.problems)


def test_a_symlinked_snapshot_is_refused(sandbox):
    plane = _plane_from(sandbox)
    target = sandbox / plane.current["latest_snapshot_path"]
    decoy = target.with_suffix(".real.json")
    decoy.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(decoy)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("this host cannot create symlinks")
    report = V.Report()
    V.check_paths(_plane_from(sandbox), report)
    assert any("symlink" in m for _, m in report.problems)


def test_a_snapshot_pointer_escaping_the_repository_is_refused(sandbox):
    current = json.loads((sandbox / V.CURRENT_PATH).read_text(encoding="utf-8"))
    current["latest_snapshot_path"] = "../outside.json"
    _rewrite(sandbox, V.CURRENT_PATH, current)
    report = V.Report()
    assert V.load(report) is None
    assert _categories(report) & {"PATH_INTEGRITY", "CURRENT_POINTER"}


def test_no_control_plane_data_file_is_executable(plane):
    report = V.Report()
    V.check_paths(plane, report)
    assert not any("executable bit" in m for _, m in report.problems)


def test_no_runtime_artifact_root_is_tracked():
    for root in V.RUNTIME_ARTIFACT_ROOTS:
        listed = subprocess.run(  # nosec B603 B607 - fixed argv, read-only plumbing
            ["git", "-C", str(REPO), "ls-files", "--", root],
            capture_output=True, text=True, check=True)
        assert listed.stdout.strip() == "", f"{root} has tracked files"


# ── 10. the holdout firewall ─────────────────────────────────────────────────────────
def test_the_firewall_passes_on_the_real_control_plane(plane):
    report = V.Report()
    V.check_holdout_firewall(plane, report)
    assert "HOLDOUT_FIREWALL" not in _categories(report)


def test_the_task_id_set_is_the_thirty_six_body_free_ids():
    assert len(V.EVAL_V4_TASK_IDS) == 36
    assert len(set(V.EVAL_V4_TASK_IDS)) == 36
    assert sum(1 for t in V.EVAL_V4_TASK_IDS if t.startswith("he4-")) == 12
    assert sum(1 for t in V.EVAL_V4_TASK_IDS if t.startswith("sr4-")) == 12
    assert sum(1 for t in V.EVAL_V4_TASK_IDS if t.startswith("adv4-")) == 12


def test_a_bootstrap_surface_naming_an_individual_holdout_task_is_refused(sandbox):
    progress = sandbox / V.PROGRESS_PATH
    progress.write_text(
        progress.read_text(encoding="utf-8") + f"\n{V.EVAL_V4_TASK_IDS[0]}\n",
        encoding="utf-8")
    report = V.Report()
    V.check_holdout_firewall(_plane_from(sandbox), report)
    assert any("names individual eval-v4 task" in m for _, m in report.problems)


def test_a_bootstrap_surface_referencing_the_body_source_is_refused(sandbox):
    progress = sandbox / V.PROGRESS_PATH
    progress.write_text(
        progress.read_text(encoding="utf-8") + f"\n{V.FORBIDDEN_BODY_SYMBOLS[0]}\n",
        encoding="utf-8")
    report = V.Report()
    V.check_holdout_firewall(_plane_from(sandbox), report)
    assert any("body source" in m for _, m in report.problems)


def test_an_evidence_pointer_into_a_body_bearing_file_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    next(d for d in mutated["datasets"]
         if d["version"] == "v4")["evidence"] = V.FORBIDDEN_BODY_SOURCES[0]
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_holdout_firewall(_plane_from(sandbox), report)
    assert any("task bodies" in m for _, m in report.problems)


def test_a_body_shaped_key_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["datasets"][3]["prompt"] = "anything at all"
    report = V.Report()
    V.check_holdout_firewall(
        V.ControlPlane(plane.current, plane.current_bytes, mutated,
                       V.canonical_bytes(mutated), plane.snapshot_path, plane.migration),
        report)
    assert any("could hold task material" in m for _, m in report.problems)


def test_a_long_free_text_value_is_refused_so_a_body_cannot_arrive_in_instalments():
    problems = V._body_shaped({"note": "x" * (V.MAX_JSON_STRING_CHARS + 1)}, "test")
    assert problems
    assert V._body_shaped({"note": "x" * V.MAX_JSON_STRING_CHARS}, "test") == []


def test_no_control_plane_surface_carries_a_private_host_path(plane):
    for rel in V.SCANNED_SURFACES:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert V.PRIVATE_PATH_RE.search(text) is None, rel


def test_the_private_path_detector_is_not_vacuous():
    assert V.PRIVATE_PATH_RE.search("see /home/someone/cache") is not None
    assert V.PRIVATE_PATH_RE.search("see /Users/someone/cache") is not None
    assert V.PRIVATE_PATH_RE.search("see state/m62/current.json") is None


def test_the_secret_scanner_is_the_repositorys_own_and_finds_nothing_here():
    pytest.importorskip("core.redaction_policy")
    for rel in V.SCANNED_SURFACES:
        assert V._scan_leaks((REPO / rel).read_text(encoding="utf-8")) == [], rel


def test_the_control_plane_json_files_are_ascii():
    for rel in (V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.CURRENT_SCHEMA_PATH,
                V.SNAPSHOT_SCHEMA_PATH):
        (REPO / rel).read_text(encoding="utf-8").encode("ascii")
    for path in (REPO / V.SNAPSHOT_DIR).iterdir():
        path.read_text(encoding="utf-8").encode("ascii")


def test_this_test_file_names_no_holdout_task_and_no_body_source():
    source = Path(__file__).read_text(encoding="utf-8")
    # The reconstruction helper and the id list are the only places a v4 id may appear,
    # and neither is a literal in this file.
    assert not [t for t in V.EVAL_V4_TASK_IDS if t in source]
    for symbol in V.FORBIDDEN_BODY_SYMBOLS:
        assert symbol not in source


# ── 11. the bootstrap surface is small and excludes history ──────────────────────────
def test_the_normal_bootstrap_set_excludes_the_historical_archive():
    assert V.ARCHIVE_PATH not in V.BOOTSTRAP_SURFACES
    assert V.CURRENT_PATH in V.BOOTSTRAP_SURFACES
    assert V.PROGRESS_PATH in V.BOOTSTRAP_SURFACES


def test_the_bootstrap_surface_is_far_smaller_than_the_old_mandatory_read(plane):
    total = sum((REPO / rel).stat().st_size for rel in V.BOOTSTRAP_SURFACES)
    total += len(plane.snapshot_bytes)
    assert total < PRE_MIGRATION_PROGRESS_BYTES / 5
    assert total < 80_000


def test_progress_states_the_bootstrap_contract_and_the_do_not_read_list():
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    for marker in ("LEVEL 0", "LEVEL 1", "LEVEL 2", "LEVEL 3", "LEVEL 4",
                   "Do NOT read by default", "historical archive"):
        assert marker in text, marker


def test_progress_points_at_the_archive_the_index_and_the_verifier():
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert V.ARCHIVE_PATH in text
    assert V.HISTORY_INDEX_PATH in text
    assert V.VERIFIER_PATH in text
    assert V.CURRENT_PATH in text


def test_the_history_index_routes_into_the_archive_and_does_not_copy_it():
    index = (REPO / V.HISTORY_INDEX_PATH).read_text(encoding="utf-8")
    assert V.ARCHIVE_PATH in index
    assert PRE_MIGRATION_PROGRESS_SHA256 in index
    for milestone in ("S3E.2", "S3G", "S3H", "S3I LIVE", "S3J", "S3K", "S3L", "S3M",
                      "S3M.1", "S3M.2", "S3N", "S3N.1"):
        assert milestone in index, milestone
    assert len(index) < len((REPO / V.ARCHIVE_PATH).read_bytes()) / 10


# ── 12. the migration lost nothing ───────────────────────────────────────────────────
def test_every_old_progress_section_is_accounted_for(manifest):
    archive = (REPO / V.ARCHIVE_PATH).read_text(encoding="utf-8")
    headings = re.findall(r"^## (\d+) — ", archive, flags=re.MULTILINE)
    assert headings, "the archive's section headings could not be located"
    covered = {entry["section"] for entry in manifest["coverage"]}
    assert set(headings) <= covered
    assert manifest["classification_summary"]["orphan_sections"] == 0
    assert manifest["classification_summary"]["old_sections_classified"] == len(covered)


def test_every_coverage_entry_names_a_destination_and_is_verified(manifest):
    for entry in manifest["coverage"]:
        assert entry["new_location"], entry["section"]
        assert entry["preserved"] is True, entry["section"]
        assert entry["verified"] is True, entry["section"]
        assert entry["classification"], entry["section"]


def test_the_migration_manifest_records_the_measured_reduction(manifest):
    reduction = manifest["reduction"]
    assert reduction["progress_byte_reduction_percent"] > 90
    assert reduction["progress_line_reduction_percent"] > 90
    assert "not tokens" in reduction["measurement"]


def test_the_migration_manifest_grants_nothing(manifest):
    assert "evidence_not_authority" in manifest
    for forbidden in ("candidate 003 design, configuration or planning", "train-v3",
                      "TRAIN authority", "EVAL authority"):
        assert any(forbidden in item
                   for item in manifest["not_authorised_by_this_migration"]), forbidden


def test_the_critical_invariants_are_listed_as_migrated(manifest):
    joined = " ".join(manifest["critical_invariants_migrated"])
    for marker in ("PROSE_CANNOT_GRANT_AUTHORITY", "ONE WRITER", "FROZEN_UNUSED",
                   "USED_IMMUTABLE", "ATTENTION_AND_MLP", "train-v2"):
        assert marker in joined, marker


def test_the_history_is_still_reachable_section_by_section():
    archive = (REPO / V.ARCHIVE_PATH).read_text(encoding="utf-8")
    for marker in ("## 13 — Defects found and fixed",
                   "## 14 — Known open issues / limitations",
                   "## 15 — Test / quality baselines",
                   "## 18 — What future sessions must NOT redo",
                   "## 19 — NEXT"):
        assert marker in archive, marker


# ── 13. size guards ──────────────────────────────────────────────────────────────────
def test_progress_is_inside_its_reviewed_budget():
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert text.count("\n") <= V.PROGRESS_MAX_LINES
    assert (REPO / V.PROGRESS_PATH).stat().st_size <= V.PROGRESS_MAX_BYTES


def test_the_progress_budget_has_headroom_but_is_not_unlimited():
    lines = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8").count("\n")
    assert V.PROGRESS_MAX_LINES >= lines + 150
    assert V.PROGRESS_MAX_LINES <= 900


def test_the_snapshot_and_pointer_stay_compact(plane):
    assert len(plane.snapshot_bytes) <= V.SNAPSHOT_MAX_BYTES
    assert len(plane.current_bytes) <= V.CURRENT_MAX_BYTES


def test_the_pointer_holds_no_history_and_no_prose(current):
    assert set(current) == {"schema_version", "state_generation",
                            "latest_snapshot_path", "latest_snapshot_sha256",
                            "subject_state_commit", "verify_command"}


def test_an_oversized_progress_fails_the_budget_check(sandbox):
    progress = sandbox / V.PROGRESS_PATH
    progress.write_text(progress.read_text(encoding="utf-8") + "\n" * 5_000,
                        encoding="utf-8")
    report = V.Report()
    V.check_budgets(_plane_from(sandbox), report)
    assert "CONTROL_PLANE_BUDGET" in _categories(report)


def test_an_oversized_snapshot_fails_the_budget_check(sandbox, monkeypatch):
    monkeypatch.setattr(V, "SNAPSHOT_MAX_BYTES", 10)
    report = V.Report()
    V.check_budgets(_plane_from(sandbox), report)
    assert "CONTROL_PLANE_BUDGET" in _categories(report)


# ── 14. the NEXT contract survived intact ────────────────────────────────────────────
def test_the_preregistered_primary_axis_is_preserved(snapshot):
    """The axis survived being implemented. S3N preregistered it; S3O bound it into a
    configuration, so the wording moved from "exactly one axis" to "already bound" -- but
    the axis itself must still read MODEL_DEFAULT -> DISABLED, unswapped."""
    nxt = snapshot["next_milestone"]
    assert "MODEL_DEFAULT" in nxt["primary_axis"]
    assert "DISABLED" in nxt["primary_axis"]
    assert nxt["primary_axis"].index("MODEL_DEFAULT") < nxt["primary_axis"].index(
        "DISABLED"), "the axis direction was reversed"


def test_the_lora_scope_is_attention_and_mlp(snapshot):
    assert snapshot["next_milestone"]["lora_scope"] == "ATTENTION_AND_MLP"


def test_train_v2_is_unchanged_and_there_is_no_train_v3(snapshot):
    nxt = snapshot["next_milestone"]
    assert "m62-defensive-quality-train v2" in nxt["training_corpus"]
    assert "No train-v3" in nxt["training_corpus"]
    assert not any(d["version"] == "v3" and
                   d["dataset_id"] == "m62-defensive-quality-train"
                   for d in snapshot["datasets"])


def test_next_offers_only_a_holdout_the_state_actually_has(snapshot):
    """Was ``test_no_fresh_holdout_is_available_and_the_spent_one_stays_unread``.

    RESCOPED at S3S, which froze ``eval-v5``. The sealed spelling asserted the literal
    string ``NONE``, which encoded a PASSING FACT -- that no fresh holdout existed at
    generation 6 -- rather than the property this test owns: **NEXT may not offer a
    holdout the state does not have.** That property is now checked against the datasets
    array instead of against a word, so it holds in both worlds and is strictly stronger
    than the string it replaces. The second half is untouched: the spent holdout is
    development evidence under D35, readable as identities and results and never as task
    bodies, and "spent" is not permission to read it.
    """
    nxt = snapshot["next_milestone"]
    offered = nxt["evaluation_holdout"]
    fresh = [d for d in snapshot["datasets"]
             if d["role"] == "EVALUATION_HOLDOUT" and d["status"] == "FROZEN_UNUSED"]
    if "NONE" in offered:
        assert fresh == [], f"NEXT says NONE while the state carries {fresh}"
    else:
        assert fresh, "NEXT offers a holdout no dataset entry carries as FROZEN_UNUSED"
        assert any(d["version"] in offered for d in fresh), offered
        assert all(d["spent_by"] is None for d in fresh)
        assert "FROZEN_UNUSED" in offered
    assert "USED_IMMUTABLE" in offered
    assert "D35" in nxt["holdout_access"]
    assert "unread" in nxt["holdout_access"]


def test_everything_ruled_out_is_still_ruled_out(snapshot):
    joined = " ".join(snapshot["next_milestone"]["ruled_out"])
    for marker in ("ATTENTION_ONLY", "learning-rate", "train-v3", "max_new_tokens",
                   "structured rows", "response schema", "refusal detector",
                   "D38 gate", "D39"):
        assert marker in joined, marker


def test_both_authorities_are_still_required(snapshot):
    joined = " ".join(snapshot["next_milestone"]["authority_required"])
    assert "TRAIN" in joined and "EVAL" in joined
    assert snapshot["next_milestone"]["requires_new_session"] is True


def test_a_next_that_swapped_the_axis_fails_verification(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["next_milestone"]["lora_scope"] = "ATTENTION_ONLY"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_next(_plane_from(sandbox), report)
    assert "CANDIDATE_STATE" in _categories(report)


# ── 15. the test baseline and its caveat ─────────────────────────────────────────────
def test_the_authoritative_focused_baseline_is_preserved(snapshot):
    baseline = snapshot["test_baseline"]
    # The counts MOVE as tests are added; pinning them here would make every milestone a
    # two-place edit and would say nothing. What must never move is zero failures and the
    # authoritative invocation.
    assert baseline["failed"] == 0
    assert baseline["passed"] >= 3076, "the suite may grow, never shrink silently"
    assert "-k m62" in baseline["invocation"]
    assert "jarvis/" in baseline["working_directory"]


def test_the_root_invocation_caveat_is_preserved_and_not_mislabelled(snapshot):
    artifact = snapshot["test_baseline"]["known_invocation_artifact"]
    assert artifact["failing_tests"] == 8
    assert "s3g2_validation_wiring" in artifact["file"]
    assert artifact["is_a_regression"] is False
    assert artifact["is_defect_d39"] is False
    assert "DISTINCT from D39" in artifact["note"]


def test_progress_records_the_caveat_and_separates_it_from_d39():
    text = (REPO / V.PROGRESS_PATH).read_text(encoding="utf-8")
    assert "8" in text and "s3g2_validation_wiring" in text
    assert "distinct from D39" in text or "It is distinct from D39" in text


def test_a_baseline_claiming_failures_are_fine_is_refused(sandbox):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["test_baseline"]["failed"] = 8
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    report = V.Report()
    V.check_next(_plane_from(sandbox), report)
    assert "CONTROL_PLANE_BUDGET" in _categories(report)


# ── 16. what the verifier itself may not do ──────────────────────────────────────────
def test_the_verifier_imports_no_network_module():
    forbidden = {"socket", "ssl", "urllib", "urllib.request", "http", "http.client",
                 "requests", "httpx", "aiohttp", "ftplib", "telnetlib", "smtplib",
                 "asyncio"}
    for node in ast.walk(VERIFIER_TREE):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden, node.module


def _verifier_identifiers() -> set[str]:
    """Every name the verifier's CODE mentions — imports, calls and attributes.

    Deliberately AST-based rather than a substring scan of the file. The module docstring
    *promises* that no torch, transformers or tokenizer is loaded, so a text search finds
    those words inside the very sentence that forbids them — the operator-ruling-H4 shape
    S3N already recorded for ``<think``. What matters is what the code references.
    """
    names: set[str] = set()
    for node in ast.walk(VERIFIER_TREE):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_the_verifier_loads_no_model_or_tokenizer():
    forbidden = {"torch", "transformers", "peft", "trl", "safetensors", "accelerate",
                 "AutoTokenizer", "AutoModel", "AutoModelForCausalLM", "from_pretrained",
                 "apply_chat_template", "PeftModel", "generate"}
    assert forbidden.isdisjoint(_verifier_identifiers())


def test_the_verifier_creates_no_train_or_eval_authority():
    forbidden = {"consume_plan", "derive_token", "issue_token", "create_generation",
                 "create_generation_directory", "execute_training", "execute_evaluation",
                 "run_paired_evaluation", "plan_training", "plan_evaluation",
                 "write_evaluation_artifacts", "train", "promote"}
    assert forbidden.isdisjoint(_verifier_identifiers())


def test_the_verifier_contains_no_write_call():
    forbidden_attributes = {"write_text", "write_bytes", "mkdir", "unlink", "rmtree",
                            "touch", "rename", "replace", "chmod", "remove", "makedirs",
                            "symlink_to", "hardlink_to"}
    for node in ast.walk(VERIFIER_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attributes, node.func.attr
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", "open() with a mode could write"


def test_the_verifier_writes_nothing_when_it_runs():
    def fingerprint() -> dict:
        seen = {}
        for rel in (V.CURRENT_PATH, V.MIGRATION_MANIFEST_PATH, V.ARCHIVE_PATH,
                    V.PROGRESS_PATH, V.HISTORY_INDEX_PATH, V.CURRENT_SCHEMA_PATH,
                    V.SNAPSHOT_SCHEMA_PATH):
            path = REPO / rel
            seen[rel] = (path.stat().st_size, V.sha256_file(path))
        for path in sorted((REPO / V.SNAPSHOT_DIR).iterdir()):
            seen[path.name] = (path.stat().st_size, V.sha256_file(path))
        return seen

    before = fingerprint()
    listing_before = sorted(p.name for p in (REPO / V.SNAPSHOT_DIR).iterdir())
    V.run()
    assert fingerprint() == before
    assert sorted(p.name for p in (REPO / V.SNAPSHOT_DIR).iterdir()) == listing_before


def test_the_verifier_only_shells_out_to_read_only_git():
    """Every git subcommand the verifier runs, and why each one cannot write.

    `rev-list` and `hash-object` joined the set at S3Q.0.2, which needs to walk a
    commit's parents and re-derive a tracked blob's oid to check that a measurement
    witness is the document its own commit carried.

    `hash-object` is the one that deserves the second assertion below: it is read-only
    ONLY without `-w`. With `-w` it writes into the object database, so a verifier is not
    permitted to pass it — and "we did not mean to" is not a control.
    """
    read_only = {"rev-parse", "cat-file", "merge-base", "ls-files", "diff", "grep",
                 "show", "rev-list", "hash-object"}
    for node in ast.walk(VERIFIER_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_git":
            first = node.args[0]
            assert isinstance(first, ast.Constant) and first.value in read_only, \
                ast.dump(first)
            if first.value == "hash-object":
                literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
                assert "-w" not in literals, ast.dump(node)
    assert "shell=False" in VERIFIER_SOURCE
    assert "shell=True" not in VERIFIER_SOURCE


def test_the_verifier_passes_on_the_repository_as_it_stands():
    report = V.run()
    assert report.problems == [], report.problems
    assert report.ok


def test_the_verifier_exits_non_zero_on_a_corrupted_control_plane(sandbox, capsys):
    plane = _plane_from(sandbox)
    mutated = copy.deepcopy(plane.snapshot)
    mutated["datasets"][3]["status"] = "USED_IMMUTABLE"
    _rewrite(sandbox, plane.current["latest_snapshot_path"], mutated)
    _repoint(sandbox)
    code = V.main(["--quiet"])
    captured = capsys.readouterr().out
    assert code == 1
    assert "M62_CONTROL_PLANE_VERIFY:\nFAIL" in captured
    assert "PROBLEMS:\n0" not in captured


def test_the_machine_readable_block_reports_every_category(capsys):
    V.main(["--quiet"])
    captured = capsys.readouterr().out
    assert "M62_CONTROL_PLANE_VERIFY:" in captured
    for category in V.CATEGORIES:
        assert f"{category}:" in captured, category
    assert "PROBLEMS:" in captured


def test_the_verifier_output_contains_no_task_material(capsys):
    V.main([])
    captured = capsys.readouterr().out
    assert not [t for t in V.EVAL_V4_TASK_IDS if t in captured]
    assert V.PRIVATE_PATH_RE.search(captured.replace(str(REPO), "")) is None


def test_the_verifier_is_deterministic():
    first = V.run()
    second = V.run()
    assert first.problems == second.problems
    assert [n for n in first.notes if "bootstrap" in n] == \
        [n for n in second.notes if "bootstrap" in n]


def test_the_verifier_records_the_stale_state_limitation_honestly():
    report = V.run()
    assert any("PARTIAL" in note for note in report.notes)


def test_the_verifier_compiles_and_declares_its_offline_guarantees():
    compile(VERIFIER_SOURCE, str(REPO / V.VERIFIER_PATH), "exec")
    for marker in ("Offline", "No model", "Read-only", "Deterministic", "Fail-closed",
                   "PROSE_CANNOT_GRANT_AUTHORITY"):
        assert marker in VERIFIER_SOURCE, marker


def test_no_control_plane_value_carries_an_absolute_path(current, snapshot, manifest):
    for payload in (current, snapshot, manifest):
        for value in V._string_values(payload):
            assert not value.startswith("/"), value
            assert str(REPO) not in value
            assert not re.match(r"^[A-Za-z]:[\\/]", value), value


def test_the_state_tree_lives_outside_every_python_package():
    """A control-plane directory inside the importable tree would change what
    ``import`` can see. It is deliberately at the repository root instead."""
    assert not (REPO / "jarvis" / "state").exists()
    assert (REPO / V.STATE_DIR).is_dir()
    assert not list((REPO / "state").rglob("*.py"))
    assert not list((REPO / "state").rglob("__init__.py"))
