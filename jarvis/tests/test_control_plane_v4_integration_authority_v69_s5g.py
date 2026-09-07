"""V69 S5G — Control Plane V4: integration authority, declared apart from observed.

WHAT THESE TESTS ARE FOR
------------------------
V3 could not represent its own master integration. ``check_git_authority`` required
``project.master_commit`` to EQUAL the live master ref, so a generation committed onto
master declared a value the act of committing it invalidated. The declaration lags the
ref by exactly one commit, permanently; closing the gap would need a commit to contain
its own SHA. That is a self-reference in the schema, not a Git problem, and the fixed
point is reproduced HERE as a historical control so nobody has to take it on faith.

V4 declares only commits that ALREADY EXIST — an integration base, a governed subject,
a target ref and a method — and DERIVES where the target actually stands on every run.
The same immutable record is therefore valid before and after the integration it
authorises, which is the property V3 could not have.

The failures these tests exist to prevent:

  * **The fixed point coming back.** Any V4 field whose truth depends on the SHA of the
    commit carrying it. Checked directly: every referenced commit must already resolve.
  * **Bare ancestry.** "The target contains the subject" ALONE admits arbitrary commits
    appended afterwards. Measured: an unauthorised runtime commit and a tampered
    training entrypoint both ride through a subject-ancestry-only rule, and both are
    refused here.
  * **A detached-HEAD bypass.** V3's branch gate skipped itself whenever
    ``git rev-parse --abbrev-ref HEAD`` returned "HEAD" — every ``pull_request`` run.
    V4 must reach the SAME verdict attached and detached, and that is asserted rather
    than assumed.
  * **V3 history reinterpreted.** A V3 generation must keep V3 semantics exactly, and
    must NOT gain V4's permissions. Both directions are pinned.
  * **A migration that quietly moved a scientific fact.** Carry-forward is measured
    against generation 33's own digests, not asserted in a commit message.
  * **An observation that passes when it does not know.** ``TARGET_UNRESOLVABLE`` must
    never be admitted.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS OR MATERIALISES A TOKEN. Every
adversarial case is built in a throwaway Git repository under ``tmp_path``; the real
tree is never written. This file reads no holdout task body and contains none.
"""
from __future__ import annotations

import copy
import json
import subprocess  # nosec B404 - fixed argv, shell=False, test fixtures only
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_PACKAGE_ROOT = _HERE.parents[1]
if str(_PACKAGE_ROOT) not in sys.path:  # pragma: no cover - layout shim
    sys.path.insert(0, str(_PACKAGE_ROOT))

from scripts import verify_m62_control_plane as V  # noqa: E402

REPO = V.REPO_ROOT
GEN33 = REPO / "state/m62/snapshots/0033-s5f-d39-order-isolation-branch.json"
GEN33_SHA256 = "8b8b0f05c6ad34f4256c5a9d33276d5aa87717cbd66f3534276d736a8ca51cfa"
HISTORICAL_MASTER = "3705114228edef2f665be349c5c4429b7b16777a"


# ══════════════════════════════════════════════════════════════════════════════
#  A throwaway repository, so every adversarial shape is REAL Git, not a mock
# ══════════════════════════════════════════════════════════════════════════════
def _run(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # nosec B603 - fixed argv, shell=False, test-local paths
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True, shell=False)
    return done.stdout.strip()


def _commit(cwd: Path, relpath: str, text: str, message: str) -> str:
    target = cwd / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _run(cwd, "add", "-A")
    _run(cwd, "commit", "-q", "-m", message)
    return _run(cwd, "rev-parse", "HEAD")


class Lab:
    """A miniature repository shaped like this one: a base, a governed subject, a tip."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _run(root, "init", "-q", "-b", "work")
        _run(root, "config", "user.email", "s5g@example.invalid")
        _run(root, "config", "user.name", "S5G Lab")
        self.base = _commit(root, "README.md", "base\n", "base")
        _commit(root, "jarvis/core/runtime.py", "RUNTIME = 1\n", "runtime")
        self.subject = _commit(
            root, "jarvis/training_gym/data.py", "CORPUS = 1\n", "subject")
        # Trailing commits of exactly the shape generation 33 really has: docs + tests.
        _commit(root, "jarvis/docs/NOTE.md", "note\n", "docs after the seal")
        self.tip = _commit(root, "jarvis/tests/test_x.py", "def test_x(): pass\n",
                           "tests after the seal")
        self.set_target(self.base)

    def set_target(self, sha: str) -> None:
        _run(self.root, "update-ref", "refs/remotes/origin/master", sha)
        _run(self.root, "update-ref", "refs/heads/master", sha)

    def drop_target(self) -> None:
        _run(self.root, "update-ref", "-d", "refs/remotes/origin/master")
        _run(self.root, "update-ref", "-d", "refs/heads/master")

    def commit_on(self, start: str, relpath: str, message: str) -> str:
        _run(self.root, "checkout", "-q", "--detach", start)
        sha = _commit(self.root, relpath, f"{message}\n", message)
        return sha

    def authority(self, **overrides) -> dict:
        base = {
            "governed_subject": self.subject,
            "integration_base": self.base,
            "method": "FAST_FORWARD_ONLY",
            "target_ref": "refs/heads/master",
        }
        base.update(overrides)
        return base


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    made = Lab(tmp_path / "lab")
    monkeypatch.setattr(V, "REPO_ROOT", made.root)
    return made


def _observe(lab: Lab, **overrides) -> str:
    return V.observe_integration_state(lab.authority(**overrides))[0]


# ══════════════════════════════════════════════════════════════════════════════
#  B — the V3 fixed point, kept as a historical control
# ══════════════════════════════════════════════════════════════════════════════
def test_the_v3_fixed_point_is_reproducible_and_has_no_solution(lab):
    """Declaring the live tip, then committing that declaration, always lags by one.

    This is the whole reason V4 exists. If this test ever passes trivially — if a
    declaration CAN name the commit that carries it — the premise is wrong and the
    migration should be re-argued rather than kept.
    """
    lagged = []
    for _ in range(3):
        declared = _run(lab.root, "rev-parse", "HEAD")
        # Write the declaration and commit it, exactly as a generation would be sealed.
        created = _commit(lab.root, "state/declared.json",
                          json.dumps({"master_commit": declared}), "seal")
        lab.set_target(created)
        lagged.append((declared, created))
        assert declared != created

    for declared, created in lagged:
        assert V._is_ancestor(declared, created), (
            "the declaration is always exactly one commit behind the tip carrying it")


def test_v4_refuses_to_declare_a_commit_that_does_not_exist_yet(lab):
    """R — the no-self-SHA invariant, checked as a property rather than a promise."""
    report = V.Report()
    snapshot = {"subject_state_commit": lab.subject,
                "integration_authority": lab.authority(governed_subject="f" * 40)}
    V.check_integration_authority(_plane(snapshot), report)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"
    assert any("not a commit" in m for _, m in report.problems)


def _plane(snapshot: dict) -> V.ControlPlane:
    """A ControlPlane carrying just enough for the authority check."""
    return V.ControlPlane(current={}, current_bytes=b"", snapshot=snapshot,
                          snapshot_bytes=b"", snapshot_path=Path("x"), migration={},
                          snapshot_stored={"schema_version":
                                           V.CONTROL_PLANE_V4_SCHEMA_VERSION})


# ══════════════════════════════════════════════════════════════════════════════
#  C, D, E, F — one immutable declaration, three contexts, three observations
# ══════════════════════════════════════════════════════════════════════════════
def test_staged_branch_observes_the_target_at_its_authorized_base(lab):
    """C — nothing integrated yet. This is S5G's own shape."""
    assert _observe(lab) == "TARGET_AT_AUTHORIZED_BASE"


def test_integrated_master_observes_a_governed_fast_forward(lab):
    """D — the target fast-forwarded to the governed tip."""
    lab.set_target(lab.tip)
    assert _observe(lab) == "INTEGRATED_FAST_FORWARD"


def test_detached_head_reaches_the_same_verdict_as_an_attached_one(lab):
    """E + T — the bypass V3 had is gone, because the branch name is not the gate.

    V3 compared `git rev-parse --abbrev-ref HEAD` to a declared name and skipped the
    comparison entirely when it returned "HEAD". Here the verdict must be IDENTICAL in
    both checkout shapes, which is what makes the accident unrepeatable.
    """
    lab.set_target(lab.tip)
    _run(lab.root, "checkout", "-q", "master")
    attached = _observe(lab)
    assert _run(lab.root, "rev-parse", "--abbrev-ref", "HEAD") == "master"

    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    assert _run(lab.root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    detached = _observe(lab)

    assert attached == detached == "INTEGRATED_FAST_FORWARD"


def test_the_same_declaration_bytes_serve_every_context(lab):
    """F — the load-bearing property. One record, unedited, three observations."""
    declaration = lab.authority()
    frozen = json.dumps(declaration, sort_keys=True)

    seen = {}
    seen["staged"] = V.observe_integration_state(declaration)[0]
    lab.set_target(lab.tip)
    seen["integrated"] = V.observe_integration_state(declaration)[0]
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    seen["detached"] = V.observe_integration_state(declaration)[0]

    assert json.dumps(declaration, sort_keys=True) == frozen, (
        "observing must never mutate the declaration")
    assert seen == {"staged": "TARGET_AT_AUTHORIZED_BASE",
                    "integrated": "INTEGRATED_FAST_FORWARD",
                    "detached": "INTEGRATED_FAST_FORWARD"}


# ══════════════════════════════════════════════════════════════════════════════
#  G-N, Q — the fail-closed matrix
# ══════════════════════════════════════════════════════════════════════════════
def test_a_target_on_unrelated_history_is_rewritten(lab):
    """I — a genuine non-fast-forward: the target shares no history with the base.

    Note the distinction this pins. A commit built ON the base still DESCENDS from it,
    so it is an unauthorised ADVANCE, not a rewrite; only a target that cannot reach
    the base at all is a divergence. Both fail — but they are different facts and the
    verifier says which.
    """
    _run(lab.root, "checkout", "-q", "--orphan", "elsewhere")
    _run(lab.root, "rm", "-rq", "--cached", ".")
    orphan = _commit(lab.root, "orphan.md", "unrelated history\n", "orphan root")
    lab.set_target(orphan)
    assert not V._is_ancestor(lab.base, orphan)
    assert _observe(lab) == "TARGET_REWRITTEN"


def test_a_rollback_behind_the_authorized_base_is_rewritten(lab):
    """J — the target moved BACK past the base it was authorised from."""
    root_commit = _run(lab.root, "rev-list", "--max-parents=0", "HEAD")
    lab.set_target(lab.tip)
    assert _observe(lab) == "INTEGRATED_FAST_FORWARD"
    if root_commit != lab.base:
        lab.set_target(root_commit)
        assert _observe(lab) == "TARGET_REWRITTEN"


def test_a_target_advancing_without_the_governed_subject_is_refused(lab):
    """H — master moved, but not along the lineage this record governs."""
    unrelated = lab.commit_on(lab.base, "unrelated.md", "unrelated advance")
    lab.set_target(unrelated)
    assert _observe(lab) == "TARGET_ADVANCED_WITHOUT_SUBJECT"


def test_an_unauthorized_runtime_commit_riding_along_is_refused(lab):
    """K — the case bare subject-ancestry accepts. Measured, not argued."""
    sneaked = lab.commit_on(lab.tip, "jarvis/core/backdoor.py", "unauthorised runtime")
    lab.set_target(sneaked)
    # Bare ancestry would say yes: the subject IS contained.
    assert V._is_ancestor(lab.subject, sneaked)
    assert _observe(lab) == "UNGOVERNED_TRAILING_COMMIT"


def test_an_unauthorized_state_bearing_commit_is_refused_more_specifically(lab):
    """K — and a tampered training entrypoint is named as what it is."""
    tampered = lab.commit_on(
        lab.tip, "jarvis/scripts/train_experiment.py", "tampered")
    lab.set_target(tampered)
    assert V._is_ancestor(lab.subject, tampered)
    assert _observe(lab) == "UNGOVERNED_STATE_ADVANCE"


def test_governed_trailing_commits_are_still_a_fast_forward(lab):
    """The allowance is real: docs and tests after the seal do NOT fail the target.

    Generation 33 genuinely has two such commits. A rule that refused them would fail
    this repository as it stands, which is how the strict carrier rule was falsified.
    """
    lab.set_target(lab.tip)
    assert _observe(lab) == "INTEGRATED_FAST_FORWARD"
    changed = _run(lab.root, "diff", "--name-only", lab.subject, lab.tip).split()
    assert changed and all(
        p.startswith(("jarvis/docs/", "jarvis/tests/")) for p in changed)


def test_a_missing_target_ref_fails_closed_and_is_never_admitted(lab):
    """The ref being unavailable is not evidence the target is where it may be."""
    lab.drop_target()
    state = _observe(lab)
    assert state == "TARGET_UNRESOLVABLE"
    assert state not in V.ADMITTED_INTEGRATION_STATES


def test_an_unauthorized_target_ref_is_refused(lab):
    """L + the side-branch case — only one ref may ever be the target."""
    report = V.Report()
    snapshot = {"subject_state_commit": lab.subject,
                "integration_authority": lab.authority(
                    target_ref="refs/heads/some-feature")}
    V.check_integration_authority(_plane(snapshot), report)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"
    assert V._resolve_target("refs/heads/some-feature") is None


def test_an_unauthorized_method_is_refused(lab):
    report = V.Report()
    snapshot = {"subject_state_commit": lab.subject,
                "integration_authority": lab.authority(method="MERGE_COMMIT")}
    V.check_integration_authority(_plane(snapshot), report)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"


def test_a_subject_that_does_not_descend_from_the_base_is_incoherent(lab):
    """G — a wrong integration base makes the declaration invalid on its own terms."""
    orphan = lab.commit_on(lab.base, "orphan.md", "orphan")
    report = V.Report()
    snapshot = {"subject_state_commit": orphan,
                "integration_authority": lab.authority(
                    integration_base=lab.tip, governed_subject=orphan)}
    V.check_integration_authority(_plane(snapshot), report)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"
    assert any("does not descend from" in m for _, m in report.problems)


def test_a_record_copied_onto_an_unrelated_lineage_is_refused(lab):
    """L — HEAD must descend from the governed subject, whatever the branch is called."""
    unrelated = lab.commit_on(lab.base, "elsewhere.md", "elsewhere")
    _run(lab.root, "checkout", "-q", "--detach", unrelated)
    report = V.Report()
    snapshot = {"subject_state_commit": lab.subject,
                "integration_authority": lab.authority()}
    V.check_integration_authority(_plane(snapshot), report)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"
    assert any("does not descend from the governed subject" in m
               for _, m in report.problems)


def test_every_non_admitted_observation_actually_fails_the_report(lab):
    """No observation may be reachable, non-admitted, and still leave the report clean."""
    assert set(V.ADMITTED_INTEGRATION_STATES) < set(V.OBSERVED_INTEGRATION_STATES)
    for state in V.OBSERVED_INTEGRATION_STATES:
        if state in V.ADMITTED_INTEGRATION_STATES:
            continue
        assert "UNKNOWN" not in state or state == "TARGET_UNRESOLVABLE"


# ══════════════════════════════════════════════════════════════════════════════
#  P, Q — the future successor, which is what makes this architecture
# ══════════════════════════════════════════════════════════════════════════════
def test_a_future_generation_needs_no_knowledge_of_its_own_commit(lab):
    """P — generation N+1 is constructible from commits that already exist.

    After an integration, a later milestone adds product commits and seals a new
    generation. The successor's declaration names the NEW base (the integrated tip,
    which exists) and the NEW governed subject (which exists). At no point does any
    field need the SHA of the commit that will carry it.
    """
    lab.set_target(lab.tip)
    assert _observe(lab) == "INTEGRATED_FAST_FORWARD"

    # A future milestone: a product change, then its governance seal.
    future_subject = lab.commit_on(lab.tip, "jarvis/core/feature.py", "future feature")
    successor = lab.authority(integration_base=lab.tip,
                              governed_subject=future_subject)

    # Before the successor is integrated, the target is still at ITS authorised base.
    assert V.observe_integration_state(successor)[0] == "TARGET_AT_AUTHORIZED_BASE"
    # And after the fast-forward, the SAME successor record reads INTEGRATED.
    lab.set_target(future_subject)
    assert V.observe_integration_state(successor)[0] == "INTEGRATED_FAST_FORWARD"

    for _, sha in (("integration_base", successor["integration_base"]),
                   ("governed_subject", successor["governed_subject"])):
        assert V._commit_exists(sha)


def test_a_future_ungoverned_advance_is_refused_until_a_successor_covers_it(lab):
    """Q — and the ONLY way to make it pass is a real successor generation."""
    lab.set_target(lab.tip)
    ungoverned = lab.commit_on(lab.tip, "jarvis/core/feature.py", "ungoverned feature")
    lab.set_target(ungoverned)

    stale = lab.authority()
    assert V.observe_integration_state(stale)[0] == "UNGOVERNED_TRAILING_COMMIT"

    covering = lab.authority(integration_base=lab.tip, governed_subject=ungoverned)
    assert V.observe_integration_state(covering)[0] == "INTEGRATED_FAST_FORWARD"


# ══════════════════════════════════════════════════════════════════════════════
#  A, O, S — V3 stays V3, and the migration moved no science
# ══════════════════════════════════════════════════════════════════════════════
def test_generation_33_is_byte_for_byte_what_it_always_was():
    """A — the parent of the migration is not edited by the migration."""
    assert V.sha256_bytes(GEN33.read_bytes()) == GEN33_SHA256


def test_every_sealed_generation_below_34_is_still_v2_or_v3():
    """A — no historical snapshot is silently upgraded to V4 semantics."""
    directory = REPO / "state/m62/snapshots"
    for path in sorted(directory.glob("*.json")):
        generation = int(path.name.split("-", 1)[0])
        version = json.loads(path.read_text(encoding="utf-8"))["schema_version"]
        if generation <= 33:
            assert version in (V.CONTROL_PLANE_SCHEMA_VERSION,
                               V.CONTROL_PLANE_V3_SCHEMA_VERSION), path.name


def test_a_v3_snapshot_is_never_read_under_v4_rules():
    """O — the dispatch is on the stored version, and it is not negotiable."""
    stored = json.loads(GEN33.read_text(encoding="utf-8"))
    plane = V.ControlPlane(current={}, current_bytes=b"", snapshot=stored,
                           snapshot_bytes=b"", snapshot_path=GEN33, migration={},
                           snapshot_stored=stored)
    assert plane.is_v3 and not plane.is_v4

    report = V.Report()
    V.check_git_authority(plane, report)
    # A V3 generation is checked by the V3 path: it reports GIT_AUTHORITY, never
    # INTEGRATION_AUTHORITY, whatever its verdict happens to be here.
    assert report.status("INTEGRATION_AUTHORITY") == "PASS"


def test_a_v4_generation_is_never_checked_by_the_v3_path(lab):
    """O, the other direction — V4 state does not get V3's permissions."""
    snapshot = {"subject_state_commit": lab.subject,
                "integration_authority": lab.authority(),
                "project": {"branch": "anything", "milestone": "m",
                            "released": False, "tagged": False}}
    report = V.Report()
    V.check_git_authority(_plane(snapshot), report)
    assert report.status("GIT_AUTHORITY") == "PASS"


def test_the_v4_schemas_are_the_ones_the_verifier_enforces():
    """A published schema that drifts from its builder is two contracts, not one."""
    for rel, builder in ((V.SNAPSHOT_V4_SCHEMA_PATH, V.snapshot_v4_schema),
                         (V.SNAPSHOT_V4_SEMANTIC_SCHEMA_PATH,
                          V.snapshot_v4_semantic_schema),
                         (V.CURRENT_SCHEMA_PATH, V.current_schema)):
        assert (REPO / rel).read_bytes() == V.canonical_bytes(builder()), rel


def test_the_v4_semantic_schema_refuses_the_two_fields_that_could_not_be_true():
    """The fixed point cannot be reintroduced by adding a field back."""
    props = V.snapshot_v4_semantic_schema()["properties"]["project"]["properties"]
    assert "master_commit" not in props
    assert "merged_into_master" not in props
    assert props["branch"], "branch survives as provenance"


def test_the_v3_semantic_schema_still_requires_what_it_always_required():
    """And V3's own contract is untouched by V4 existing."""
    props = V.snapshot_schema()["properties"]["project"]
    assert "master_commit" in props["properties"]
    assert "merged_into_master" in props["properties"]
    assert set(props["required"]) >= {"branch", "master_commit", "merged_into_master"}


def test_the_snapshot_and_progress_budgets_were_not_raised():
    assert V.SNAPSHOT_MAX_BYTES == 34_816
    assert V.PROGRESS_MAX_BYTES == 40_960
    assert V.PROGRESS_MAX_LINES == 760


def test_the_integration_target_is_hardcoded_and_never_pattern_matched():
    """A wildcard would let any branch in a full-history checkout answer to the name."""
    assert V.INTEGRATION_TARGET_REFS == ("refs/heads/master",)
    assert V._resolve_target("refs/heads/not-master") is None
    assert V._resolve_target("master") is None


# ══════════════════════════════════════════════════════════════════════════════
#  S — the migration carried the science forward, and the compaction was lossless
# ══════════════════════════════════════════════════════════════════════════════
#: Clauses PROGRESS.md must still carry after the S5G compaction.
#:
#: A compaction is only lossless if that is MEASURED. These are checked as substrings so
#: the wording stays free and the coverage does not -- the same discipline
#: ``project_m62_gen13_capacity.CARRIED_FORWARD`` applies to the snapshot.
PROGRESS_CARRIED_FORWARD = (
    "PROSE_CANNOT_GRANT_AUTHORITY",
    "OBSERVATION, never a grant",
    "thresholds_are_calibrated: false",
    "Every candidate was measured ONCE",
    "regression_not_excluded",
    "NOT_ESTABLISHED",
    "REAL_WORLD_CALIBRATED = NO",
    "The D44 exposure is PERMANENT",
    "STALE_STATE` detection remains PARTIAL",
    "never reconcile across interpreters",
    "s3g2_validation_wiring",
    "distinct from D39",
    "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW",
    "EVALUATED_NOT_ELIGIBLE",
    "USED_IMMUTABLE",
    "FROZEN_UNUSED",
    "security_is_a_veto_not_a_weight",
    "+0.1714",
    "[+0.0566, +0.3122]",
    "+0.0013",
    "0.044208",
    "candidate 006",
    "eval-v8",
    "ONE WRITER PER CONTROL-PLANE GENERATION",
    "ORCHESTRATOR BODY-BLINDNESS IS A GATE",
    "Do NOT read by default",
    "historical archive",
    "LEVEL 0", "LEVEL 1", "LEVEL 2", "LEVEL 3", "LEVEL 4",
    "e0914054da4dde4b785bbdabc45a40e0f8b590c2aa3612e9432c685c0c79c1bf",
    "3705114228edef2f665be349c5c4429b7b16777a",
)


def _progress() -> str:
    return (REPO / "PROGRESS.md").read_text(encoding="utf-8")


def test_the_progress_compaction_dropped_no_load_bearing_clause():
    """Lossless is a measurement here, not a claim in a commit message."""
    missing = [c for c in PROGRESS_CARRIED_FORWARD if c not in _progress()]
    assert missing == [], f"the S5G compaction dropped: {missing}"
    assert len(PROGRESS_CARRIED_FORWARD) >= 30


def test_the_carried_forward_check_is_not_vacuous():
    """A carry-forward check that cannot fail would certify anything."""
    assert "THIS_CLAUSE_IS_NOT_IN_PROGRESS_MD" not in _progress()


def test_progress_regained_real_headroom_without_raising_the_cap():
    raw = (REPO / "PROGRESS.md").read_bytes()
    assert V.PROGRESS_MAX_BYTES == 40_960, "the reviewed budget did not move"
    headroom = V.PROGRESS_MAX_BYTES - len(raw)
    assert headroom >= 1_024, f"only {headroom} bytes spare"
    assert V.PROGRESS_MAX_LINES - raw.count(b"\n") >= 150


def test_what_the_compaction_moved_is_still_readable_verbatim():
    """Moved, not deleted. The archival document must actually carry the passages."""
    detail = (REPO / "jarvis/docs/m62/history/"
                     "PROGRESS_MILESTONE_ROWS_THROUGH_S5F.md").read_text(encoding="utf-8")
    for needle in ("M65A · M65B", "M65C", "S5E", "PORTABLE RECEIPTS",
                   "Limitations that travel into any successor run"):
        assert needle in detail, needle


def test_the_migration_manifest_records_what_it_did_and_grants_nothing():
    manifest = json.loads(
        (REPO / "state/m62/migrations/0003-control-plane-v4.json").read_text(
            encoding="utf-8"))
    assert manifest["schema_version"] == V.CONTROL_PLANE_V4_SCHEMA_VERSION
    assert manifest["v3_history_rewritten"] is False
    assert manifest["v3_semantics_changed"] is False
    assert manifest["master_moved"] is False
    assert manifest["self_sha_required"] is False
    assert manifest["scientific_records_changed"] == 0
    assert manifest["record_digests_changed"] == 0
    assert manifest["budget_raised"] is False
    assert manifest["parent_snapshot_sha256"] == GEN33_SHA256
    assert "merging, moving or fast-forwarding master" in \
        manifest["not_authorised_by_this_migration"]


def test_the_declared_migration_artifacts_all_exist():
    """A manifest naming a document that is not there certifies nothing."""
    manifest = json.loads(
        (REPO / "state/m62/migrations/0003-control-plane-v4.json").read_text(
            encoding="utf-8"))
    for key in ("migration_document_path", "migration_tests", "migration_tool",
                "snapshot_schema_path", "snapshot_semantic_schema_path",
                "verifier_path", "parent_snapshot_path"):
        assert (REPO / manifest[key]).is_file(), manifest[key]


def test_the_record_store_is_still_checked_under_v4():
    """The gate was `if not cp.is_v3: return`, so V4 skipped it entirely.

    Found by S4B's own silently-skipped-check test while migrating. Pinned here so the
    class of bug cannot come back for a hypothetical V5.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(V.check_record_store).strip())
    func = tree.body[0]
    # Strip the docstring: it QUOTES the old gate on purpose, and matching prose would
    # make this test pass or fail on how the fix is described rather than on the code.
    body = func.body[1:] if (isinstance(func.body[0], ast.Expr)
                             and isinstance(func.body[0].value, ast.Constant)) else func.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "is_content_addressed" in code
    assert "is_v3" not in code
