"""V69 S5G.1 — pre-integration hardening: master-context semantics and two trust
boundaries the S5G matrix left open.

WHAT THESE TESTS ARE FOR
------------------------
An independent pre-master reconciliation found three reproducible code-side findings and
one external one. All four were reproduced before a line was changed; each is pinned
here by the reproducer that found it, not by a restatement of the fix.

**F1 — a legitimate integration turned the authoritative suite red.** Three tests pinned
``observe_integration_state(...)[0] == "TARGET_AT_AUTHORIZED_BASE"`` — the
*not-yet-integrated* observation — as a permanent invariant. Measured: with master
fast-forwarded to the branch tip and the tree otherwise byte-identical, all three FAIL
with ``assert 'INTEGRATED_FAST_FORWARD' == 'TARGET_AT_AUTHORIZED_BASE'``. ``ci.yml`` runs
on ``push: branches: [master]``, so the first run after an authorised integration was
deterministically red, and rolling master back to hide it is exactly the force-rewrite
the milestone forbids. S5G rescoped these away from the frozen ``merged_into_master``
boolean and pinned the wrong half: the invariant is membership in the ADMITTED set, not
equality with the pre-integration value.

**F2 — the checker certified its own weakening.** ``VERIFIER_PATH`` was on
``INTEGRATION_TRAILING_PATHS``, so a trailing commit could edit the checker and the
edited checker was then the thing that certified that lineage. Measured in a disposable
clone: deleting the single line ``check_git_authority(cp, report)`` from ``run()`` in the
same commit that added ``jarvis/core/s5g1_repro_runtime_engine.py`` yielded
``M62_CONTROL_PLANE_VERIFY: PASS`` / ``PROBLEMS: 0`` — with ``GIT_AUTHORITY: PASS`` and
``INTEGRATION_AUTHORITY: PASS`` printed for two checks that never executed — and the full
authoritative suite passed on that tree as well (10,662 passed, 83 skipped, exit 0).

**F3 — the CI suite could erase its own failures.** ``tests/`` was on the trailing
surface and the authoritative command names BOTH trees, so a trailing
``tests/conftest.py`` receives every ``jarvis/tests`` item in
``pytest_collection_modifyitems``. Measured: the F1 failures deselected, exit 0, control
plane PASS. Targeting only ``jarvis/tests`` never loads that file, so the effect is
invisible unless the exact CI invocation is reproduced.

**F4 — external.** ``master`` has no branch protection and no repository ruleset. S5G.1
does not fix that and does not pretend to; it is recorded as EXTERNAL_CONTROL_PENDING.

WHAT THE FIX IS
---------------
One sentence: **the governed subject is the only place executable authority may change.**
After it, a lineage may carry inert governance evidence and nothing else. The closure
gained a second dimension (on the surface AND inert), the checker and both test trees
came off the surface, a category no check claimed can no longer print PASS, and the live
generation seals the checker's digest.

WHAT THE FIX IS NOT
-------------------
It is not a proof against an attacker who edits the checker, because the self-audit is in
the checker. What it removes is the SILENT version: such a lineage is no longer governed
at all and needs a successor generation, which is a commit a human reads. The residual is
external and is stated in every report this milestone produces.

NOTHING HERE TRAINS, EVALUATES, LOADS MODEL WEIGHTS OR MATERIALISES A TOKEN. Every
adversarial case is built in a throwaway Git repository under ``tmp_path``; the real tree
is never written. This file reads no holdout task body and contains none.
"""
from __future__ import annotations

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
HISTORICAL_MASTER = "3705114228edef2f665be349c5c4429b7b16777a"

#: The admitted observations, written out LITERALLY rather than imported from the
#: verifier. A test that reads its own expectation out of the module under test moves
#: whenever that module moves, which is the one thing this set must not do: widening
#: ``ADMITTED_INTEGRATION_STATES`` is precisely the edit that would turn every refusal
#: into a pass, and it has to fail here when it happens.
ADMITTED = frozenset({"TARGET_AT_AUTHORIZED_BASE", "INTEGRATED_FAST_FORWARD"})

#: The three tests F1 was reproduced on, and the file each lives in.
F1_RESCOPED = (
    ("jarvis/tests/test_training_gym_m62_s4h_s4f_result_invariance.py",
     "test_master_is_unchanged_and_nothing_was_merged_tagged_or_released"),
    ("jarvis/tests/test_training_gym_m63_s4b_control_plane.py",
     "test_the_project_block_did_not_move_master_or_merge"),
    ("jarvis/tests/test_training_gym_m63_s4c_trained_state.py",
     "test_the_project_block_did_not_move_master"),
)


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
    """A miniature repository shaped like this one, under S5G.1 topology.

    The trailing commits are the shape a governance close really has AFTER S5G.1: state
    and documentation only. S5G's lab put a ``jarvis/tests/`` commit after the seal,
    which is exactly the shape this milestone stopped permitting.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _run(root, "init", "-q", "-b", "work")
        _run(root, "config", "user.email", "s5g1@example.invalid")
        _run(root, "config", "user.name", "S5G.1 Lab")
        # A commit BEFORE the authorised base, so "roll the target back past its base"
        # is expressible at all.
        self.pre_base = _commit(root, "SEED.md", "seed\n", "seed before the base")
        # A minimal but COHERENT control plane. `_target_state_incoherence` reads the
        # target tree's own pointer, so a lab whose "master" carries no control plane at
        # all is not a model of this repository -- it is a model of a broken one.
        (root / "state/m62/snapshots").mkdir(parents=True, exist_ok=True)
        (root / "state/m62/snapshots/0001-lab.json").write_text("{}\n", encoding="utf-8")
        (root / "state/m62/current.json").write_text(
            '{"latest_snapshot_path": "state/m62/snapshots/0001-lab.json"}\n',
            encoding="utf-8")
        (root / "PROGRESS.md").write_text("lab\n", encoding="utf-8")
        self.base = _commit(root, "README.md", "base\n", "base")
        _commit(root, "jarvis/core/runtime.py", "RUNTIME = 1\n", "runtime")
        _commit(root, "jarvis/tests/test_authority.py", "def test_a(): pass\n", "suite")
        # The governed subject carries the executable authority in FINAL form: runtime,
        # the suite and the checker. Everything after it is evidence.
        self.subject = _commit(
            root, "jarvis/training_gym/data.py", "CORPUS = 1\n", "subject")
        _commit(root, "jarvis/docs/NOTE.md", "note\n", "docs after the seal")
        # A real governance close: ADD a snapshot, then move the pointer at it. Writing
        # `{}` over `current.json` modelled a broken control plane, not a closed
        # milestone, and `_target_state_incoherence` is right to refuse that.
        (root / "state/m62/snapshots/0099-seal.json").write_text("{}\n", encoding="utf-8")
        self.tip = _commit(
            root, "state/m62/current.json",
            '{"latest_snapshot_path": "state/m62/snapshots/0099-seal.json"}\n',
            "state after the seal")
        self.set_target(self.base)

    def set_target(self, sha: str) -> None:
        _run(self.root, "update-ref", "refs/remotes/origin/master", sha)
        _run(self.root, "update-ref", "refs/heads/master", sha)

    def commit_on(self, start: str, relpath: str, message: str) -> str:
        _run(self.root, "checkout", "-q", "--detach", start)
        return _commit(self.root, relpath, f"{message}\n", message)

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


def _plane(snapshot: dict) -> V.ControlPlane:
    """A ControlPlane carrying just enough for the authority checks."""
    return V.ControlPlane(current={}, current_bytes=b"", snapshot=snapshot,
                          snapshot_bytes=b"", snapshot_path=Path("x"), migration={},
                          snapshot_stored={"schema_version":
                                           V.CONTROL_PLANE_V4_SCHEMA_VERSION})


def _observe(lab: Lab, **overrides) -> str:
    return V.observe_integration_state(lab.authority(**overrides))[0]


def _staged_report(lab: Lab, head: str) -> V.Report:
    """The authority check with the target parked at its authorised base."""
    lab.set_target(lab.base)
    _run(lab.root, "checkout", "-q", "--detach", head)
    report = V.Report()
    V.check_integration_authority(
        _plane({"subject_state_commit": lab.subject,
                "integration_authority": lab.authority()}), report)
    return report


def _integrity_report(lab: Lab, head: str) -> V.Report:
    """``check_verifier_integrity`` alone, on the lineage ``head`` is the tip of."""
    _run(lab.root, "checkout", "-q", "--detach", head)
    report = V.Report()
    V.check_verifier_integrity(
        _plane({"subject_state_commit": lab.subject, "state_generation": 34,
                "integration_authority": lab.authority()}), report)
    return report


# ══════════════════════════════════════════════════════════════════════════════
#  F1 — a legitimate integration must not turn the authoritative suite red
#
#  Reproduced before anything was changed. With HEAD at the branch tip and both
#  master refs fast-forwarded onto it — the clean commit unmodified, only the ref
#  moved — the three tests below FAILED with
#  `assert 'INTEGRATED_FAST_FORWARD' == 'TARGET_AT_AUTHORIZED_BASE'`. With master at
#  the base they passed. `ci.yml` triggers on `push: branches: [master]`, so the
#  first CI run after the authorised integration was deterministically red, and the
#  only way to make it green again was to roll master back — the exact-base rewrite
#  the milestone forbids and that GitHub does not currently prevent.
#
#  The old invariant is not merely inconvenient, it is WRONG under V4: "master must
#  remain at the integration base forever" is a claim V4 was built to stop making.
#  The invariant that survives is membership in the ADMITTED set. That set is TWO
#  states, and it is written out literally here so that widening the verifier's own
#  constant cannot widen the test with it.
# ══════════════════════════════════════════════════════════════════════════════
def test_the_admitted_set_is_exactly_the_two_states_and_nothing_else():
    """The non-vacuity anchor for every other assertion in this section.

    `assert observation in <something>` is only a test while <something> stays small.
    Pinned by EQUALITY, not by a subset relation: a subset test is satisfied by adding
    UNGOVERNED_TRAILING_COMMIT to the admitted set, which is the single edit that turns
    every refusal in this file into a pass.
    """
    assert set(V.ADMITTED_INTEGRATION_STATES) == ADMITTED
    assert ADMITTED < set(V.OBSERVED_INTEGRATION_STATES)
    assert len(V.OBSERVED_INTEGRATION_STATES) - len(ADMITTED) >= 5, (
        "the refused observations are what make the admitted ones mean something")


def test_the_staged_repository_is_admitted(lab):
    """Before integration. Every run an operator reads to DECIDE to integrate."""
    lab.set_target(lab.base)
    assert _observe(lab) == "TARGET_AT_AUTHORIZED_BASE"
    assert _observe(lab) in ADMITTED


def test_a_legitimate_fast_forward_is_admitted(lab):
    """After integration. THE F1 case: the same immutable record, one ref moved."""
    lab.set_target(lab.tip)
    assert _observe(lab) == "INTEGRATED_FAST_FORWARD"
    assert _observe(lab) in ADMITTED


def test_an_unauthorized_descendant_is_refused(lab):
    """A descendant is not automatically a governed one."""
    rogue = lab.commit_on(lab.tip, "jarvis/core/new_engine.py", "rogue runtime")
    lab.set_target(rogue)
    state = _observe(lab)
    assert state == "UNGOVERNED_TRAILING_COMMIT"
    assert state not in ADMITTED


def test_a_rollback_behind_the_authorized_base_is_refused(lab):
    """The exact-base rewrite. Detected here; PREVENTED only by repository policy."""
    lab.set_target(lab.pre_base)
    state = _observe(lab)
    assert state == "TARGET_REWRITTEN"
    assert state not in ADMITTED


def test_a_genuine_non_fast_forward_is_refused(lab):
    """master advanced on its own.

    The target descends from the authorised base but NOT from the governed subject, so
    no fast-forward from this branch can reach it — closing the gap would take a merge
    commit, whose second parent no generation governs. Distinct from a rollback and from
    unrelated history, and it is the shape a real accidental integration takes.
    """
    diverged = lab.commit_on(lab.base, "HOTFIX.md", "an independent master commit")
    lab.set_target(diverged)
    state = _observe(lab)
    assert state == "TARGET_ADVANCED_WITHOUT_SUBJECT"
    assert state not in ADMITTED
    # It really is non-fast-forwardable: the branch tip does not contain it.
    assert V._git("merge-base", "--is-ancestor", diverged, lab.tip)[0] != 0


def test_a_target_on_unrelated_history_is_refused(lab):
    """A root commit sharing no ancestry answers to the name and not to the lineage."""
    empty_tree = _run(lab.root, "hash-object", "-t", "tree", "/dev/null")
    unrelated = _run(lab.root, "commit-tree", empty_tree, "-m", "unrelated root")
    lab.set_target(unrelated)
    state = _observe(lab)
    assert state == "TARGET_REWRITTEN"
    assert state not in ADMITTED


def test_the_same_declaration_bytes_serve_both_admitted_contexts(lab):
    """The property V3 could not have, asserted over the SAME dict object."""
    authority = lab.authority()
    lab.set_target(lab.base)
    staged = V.observe_integration_state(authority)[0]
    lab.set_target(lab.tip)
    integrated = V.observe_integration_state(authority)[0]
    assert {staged, integrated} == ADMITTED, "one record, two contexts, both admitted"


# ── the three rescoped tests, pinned by SHAPE so they cannot regress ──────────────
def _test_block(relpath: str, name: str) -> str:
    """The EXECUTABLE source of one test function: no comments, no docstring.

    Three red-team rounds shaped this, and each one broke the previous version:

    1. It searched the WHOLE FILE, so a needle in a neighbouring test satisfied it.
    2. It searched raw source, so the word ``ADMITTED`` in the RESCOPED **comment** this
       milestone itself wrote satisfied it — a gutted body passed.
    3. It stripped only ``#`` lines, so moving the same words into the **docstring**
       satisfied it again. Measured: all five needles in a docstring, body ``assert
       True``, every guard green.

    Prose is evidence about intent and never about behaviour, so this parses the function
    and unparses only its statements — dropping the docstring — and it starts from the
    ``ast`` node, which carries the DECORATORS. A bare ``@pytest.mark.skip`` neutralised
    the test while leaving its body untouched, and no amount of body inspection sees that.
    """
    import ast  # noqa: PLC0415

    source = (REPO / relpath).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)
    assert node is not None, f"{relpath} no longer defines {name}"

    decorators = [ast.unparse(d) for d in node.decorator_list]
    disabling = [d for d in decorators
                 if "skip" in d or "xfail" in d or "skipif" in d]
    assert not disabling, (
        f"{name} is disabled by {disabling}; a decorator neutralises a test without "
        f"touching a line of its body")

    statements = list(node.body)
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]          # the docstring is prose, not behaviour
    assert statements, f"{name} has no executable body at all"
    return "\n".join(ast.unparse(s) for s in statements)


@pytest.mark.parametrize("relpath,name", F1_RESCOPED)
def test_no_authoritative_test_pins_the_pre_integration_observation(relpath, name):
    """The regression guard for F1 itself.

    Not "these three tests pass" — they pass in the staged context either way, which is
    exactly why F1 was invisible on the branch for a whole milestone. This asserts the
    SHAPE: no test in the authoritative suite may compare an observation for EQUALITY
    with the pre-integration value, because that comparison is a claim that integration
    has not happened, asserted as though it were permanent.
    """
    block = _test_block(relpath, name)
    assert 'state == "TARGET_AT_AUTHORIZED_BASE"' not in block, (
        f"{name} pins the pre-integration observation as a permanent invariant; a "
        f"legitimate fast-forward makes it FAIL")
    # The test must still DERIVE an observation and CHECK it. A guard satisfied by the
    # mere presence of a word is satisfied by a test that asserts nothing at all.
    assert "observe_integration_state(" in block, (
        f"{name} no longer derives the observation it is supposed to be about")
    assert "in admitted" in block, (
        f"{name} derives an observation and does not assert membership in the admitted "
        f"set; the derivation without the assertion is decoration")


@pytest.mark.parametrize("relpath,name", F1_RESCOPED)
def test_the_admitted_set_is_pinned_inside_each_rescoped_test(relpath, name):
    """Each of the three states its own literal set, IN THE TEST BODY.

    Scoped to the block, not the file: a literal elsewhere in the same module satisfied
    the file-wide version while the test itself had been gutted.
    """
    block = _test_block(relpath, name)
    # Matched against UNPARSED source, so quoting and wrapping are normalised away and
    # the assertion is about the code rather than about its formatting.
    for state in ("TARGET_AT_AUTHORIZED_BASE", "INTEGRATED_FAST_FORWARD"):
        assert state in block, f"{name} does not name {state} in its body"
    assert "set(V.ADMITTED_INTEGRATION_STATES) == admitted" in block, (
        f"{name} must pin the verifier's own set against its literal, so widening the "
        f"constant fails here too")


NEEDLES = ('# RESCOPED: admitted is {"TARGET_AT_AUTHORIZED_BASE", '
           '"INTEGRATED_FAST_FORWARD"}\n'
           "    # set(V.ADMITTED_INTEGRATION_STATES) == admitted\n"
           "    # V.observe_integration_state(authority) -- in admitted\n")
DOCSTRING_NEEDLES = ('    """RESCOPED. admitted is {"TARGET_AT_AUTHORIZED_BASE", '
                     '"INTEGRATED_FAST_FORWARD"};\n'
                     "    set(V.ADMITTED_INTEGRATION_STATES) == admitted; "
                     'observe_integration_state(a) in admitted."""\n')


@pytest.mark.parametrize("prose", [NEEDLES, DOCSTRING_NEEDLES],
                         ids=["comment", "docstring"])
def test_the_shape_guards_are_not_satisfiable_by_prose(tmp_path, monkeypatch, prose):
    """Non-vacuity for the guards themselves. TWO red-team findings, as a test.

    Round one moved the needles into a comment; round two moved them into the docstring.
    A gutted body carrying the right words in either must still fail the guard.
    """
    fake = tmp_path / "test_fake.py"
    fake.write_text("def test_the_project_block_did_not_move_master():\n"
                    + prose + "    assert True\n", encoding="utf-8")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    import test_control_plane_v41_preintegration_hardening_v69_s5g1 as me
    monkeypatch.setattr(me, "REPO", tmp_path)
    block = me._test_block("test_fake.py", "test_the_project_block_did_not_move_master")
    assert "observe_integration_state(" not in block
    assert '"TARGET_AT_AUTHORIZED_BASE", "INTEGRATED_FAST_FORWARD"' not in block
    assert "in admitted" not in block


def test_the_shape_guards_refuse_a_test_disabled_by_a_decorator(tmp_path, monkeypatch):
    """A decorator neutralises a test without touching a line of its body.

    Measured: `@pytest.mark.skip(reason="temporarily flaky")` on one of the three
    rescoped tests left every shape guard green while the test itself reported `skipped`.
    """
    fake = tmp_path / "test_fake.py"
    fake.write_text(
        'import pytest\n\n\n@pytest.mark.skip(reason="temporarily flaky")\n'
        "def test_the_project_block_did_not_move_master():\n"
        '    admitted = {"TARGET_AT_AUTHORIZED_BASE", "INTEGRATED_FAST_FORWARD"}\n'
        "    assert set(V.ADMITTED_INTEGRATION_STATES) == admitted\n"
        "    state, _, _ = V.observe_integration_state(authority)\n"
        "    assert state in admitted\n", encoding="utf-8")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    import test_control_plane_v41_preintegration_hardening_v69_s5g1 as me
    monkeypatch.setattr(me, "REPO", tmp_path)
    with pytest.raises(AssertionError, match="disabled by"):
        me._test_block("test_fake.py", "test_the_project_block_did_not_move_master")


def test_no_rescoped_test_is_disabled_on_the_live_tree():
    """And the same question asked of the repository rather than of a fixture."""
    for relpath, name in F1_RESCOPED:
        _test_block(relpath, name)          # raises if skipped/xfailed or bodiless


def test_the_live_repository_is_in_an_admitted_state_whatever_master_has_done():
    """The replacement invariant, run against the REAL repository.

    This is the test that must be green on the branch AND on master after the
    fast-forward. It says what the milestone actually requires — the target is where the
    authorisation permits it to be — and says nothing at all about whether integration
    has happened yet.
    """
    live = V.load(V.Report()).snapshot
    authority = live["integration_authority"]
    assert authority["integration_base"] == HISTORICAL_MASTER
    assert authority["method"] == "FAST_FORWARD_ONLY"
    assert authority["target_ref"] == "refs/heads/master"
    state, detail, offenders = V.observe_integration_state(authority)
    assert state in ADMITTED, f"{state}: {detail} {offenders}"
    assert live["project"]["tagged"] is False
    assert live["project"]["released"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  F2 — the checker may not certify its own weakening
#
#  Measured, in a disposable clone of the S5G source, before anything was changed:
#
#      $ sed -i '6164d' jarvis/scripts/verify_m62_control_plane.py   # the dispatch
#      $ cat > jarvis/core/s5g1_repro_runtime_engine.py              # ungoverned
#      $ git commit -am "..."                                        # ONE commit
#      $ python jarvis/scripts/verify_m62_control_plane.py --quiet
#      M62_CONTROL_PLANE_VERIFY: PASS ... GIT_AUTHORITY: PASS
#      INTEGRATION_AUTHORITY: PASS ... PROBLEMS: 0
#      $ python -m pytest -q --tb=line jarvis/tests tests
#      10662 passed, 83 skipped
#
#  Two defects, and both are fixed here because either one alone is enough:
#
#   1. `VERIFIER_PATH` was on the trailing surface, so the edit was GOVERNED.
#   2. `Report.status` returned PASS for any category with no recorded problem, so a
#      check that never ran was indistinguishable from a check that ran and found
#      nothing. The block printed PASS twice for a function that was never called.
# ══════════════════════════════════════════════════════════════════════════════
LOAD_BEARING = (
    "check_git_authority", "check_record_store", "check_current_pointer",
    "check_verifier_integrity", "check_snapshot_chain", "check_stale_state",
)


def test_every_load_bearing_check_is_really_dispatched_by_run(monkeypatch):
    """Spies, not source-grepping: this proves the CALL happens, not that a line exists.

    A source-text assertion is satisfied by a call inside `if False:`. Replacing each
    function with a recorder and running the real `run()` is not.
    """
    called: list[str] = []
    for name, _categories in V.CHECK_DISPATCH:
        original = getattr(V, name)

        def spy(cp, report, _name=name, _original=original):
            called.append(_name)
            return _original(cp, report)

        # Keep the module identity, or `check_verifier_integrity` correctly reports
        # every spied name as a check this module does not define -- a true finding
        # about the harness rather than about the repository.
        spy.__module__ = original.__module__
        monkeypatch.setattr(V, name, spy)
    V.run()
    assert called == [name for name, _ in V.CHECK_DISPATCH]
    for name in LOAD_BEARING:
        assert name in called, name


def test_a_check_that_never_ran_reports_not_run_rather_than_pass():
    """THE F2 fix, stated as the property that was false.

    Silence used to be success. It is now its own outcome, and an outcome that fails
    the report — so deleting a dispatch entry costs the attacker the PASS it used to buy
    for free.
    """
    report = V.Report()
    assert report.status("GIT_AUTHORITY") == "NOT_RUN"
    assert report.ok is False, "a report where nothing ran is not a clean report"
    report.claim("GIT_AUTHORITY")
    assert report.status("GIT_AUTHORITY") == "PASS"
    report.fail("GIT_AUTHORITY", "something")
    assert report.status("GIT_AUTHORITY") == "FAIL"


@pytest.mark.parametrize("victim", LOAD_BEARING)
def test_removing_any_load_bearing_dispatch_entry_fails_the_report(monkeypatch, victim):
    """The mutation campaign, run as a test rather than reported as a measurement.

    Each removal is the S5G attack in miniature: the check is gone from the table, so it
    never executes. Both controls must fire — the categories it owned report NOT_RUN,
    and `check_verifier_integrity` finds a function this module defines that nothing
    dispatches.
    """
    weakened = tuple(entry for entry in V.CHECK_DISPATCH if entry[0] != victim)
    assert len(weakened) == len(V.CHECK_DISPATCH) - 1, victim
    monkeypatch.setattr(V, "CHECK_DISPATCH", weakened)
    report = V.run()
    assert report.ok is False, f"removing {victim} left the report clean"
    orphaned = [c for name, cats in V.CHECK_DISPATCH if name == victim for c in cats]
    assert [c for c in orphaned if report.status(c) == "NOT_RUN"] == orphaned, (
        f"{victim}'s categories still reported PASS for a check that never ran")
    if victim != "check_verifier_integrity":
        # The structural audit names the orphan. When the orphan IS the structural
        # audit, the NOT_RUN above is the whole of the evidence -- which is the honest
        # limit of a self-check and the reason the closure exists as well.
        assert any(victim in message for _, message in report.problems), (
            f"nothing named {victim} as the check that stopped being dispatched")


def test_the_dispatch_table_names_every_check_this_module_defines():
    """The structural half, asserted here as well as inside the checker.

    Asserted in BOTH places deliberately: the in-checker copy dies with the checker, and
    this one runs in CI from a different file.
    """
    import inspect as _inspect

    defined = {name for name, obj in vars(V).items()
               if name.startswith("check_") and _inspect.isfunction(obj)
               and obj.__module__ == V.__name__}
    dispatched = {name for name, _ in V.CHECK_DISPATCH}
    assert defined - dispatched == set(V.INTERNALLY_DISPATCHED_CHECKS)
    assert dispatched <= defined, "the table names something that does not exist"
    assert V.INTERNALLY_DISPATCHED_CHECKS == ("check_integration_authority",)


def test_exactly_one_dispatch_entry_owns_each_reported_category():
    """If two entries own a category, deleting one leaves NOT_RUN unable to fire."""
    owners: dict = {}
    for _name, categories in V.CHECK_DISPATCH:
        for category in categories:
            owners[category] = owners.get(category, 0) + 1
    assert sorted(owners) == sorted(V.CATEGORIES)
    assert set(owners.values()) == {1}


def test_the_reported_categories_are_the_reviewed_ones():
    """Pins the tuple itself: deleting a category is the other way to make a failing
    check stop mattering, and it is a one-line edit too."""
    assert V.CATEGORIES == (
        "SCHEMA", "CURRENT_POINTER", "SNAPSHOT_CHAIN", "ARCHIVE_INTEGRITY",
        "GIT_AUTHORITY", "INTEGRATION_AUTHORITY", "VERIFIER_INTEGRITY",
        "DATASET_STATE", "CANDIDATE_STATE", "TRAINING_RECEIPT", "EVALUATION_RECEIPT",
        "POLICY_IDENTITIES", "AUTHORITY_SEPARATION", "HOLDOUT_FIREWALL",
        "PATH_INTEGRITY", "STALE_STATE", "RECORD_STORE", "INSTRUMENT_STACK",
        "CONTROL_PLANE_BUDGET")


def test_the_checker_is_no_longer_inside_its_own_allowance():
    """The one-line statement of the F2 closure."""
    assert V.VERIFIER_PATH not in V.INTEGRATION_TRAILING_PATHS
    assert not V._governed_by(V.VERIFIER_PATH, V.INTEGRATION_TRAILING_PATHS)
    assert V._governed_by(V.VERIFIER_PATH, V.AUTHORITY_CRITICAL_PATHS)


def test_a_trailing_commit_touching_the_checker_is_refused_by_the_closure(lab):
    """The exact S5G reproducer, through the closure."""
    head = lab.commit_on(lab.tip, V.VERIFIER_PATH, "weaken a dispatch")
    report = _staged_report(lab, head)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL"
    assert any(V.VERIFIER_PATH in message for _, message in report.problems)


def test_a_trailing_commit_touching_the_checker_is_refused_again_by_the_pin(lab):
    """And by the control that does NOT share the closure's dispatch.

    This is the half that survives the original attack: the S5G weakening deleted the
    call that reaches the closure, so a second control routed through the same call
    would have died with it.
    """
    head = lab.commit_on(lab.tip, V.VERIFIER_PATH, "weaken a dispatch")
    report = _integrity_report(lab, head)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL"
    assert any("authority-critical" in message for _, message in report.problems)


def test_the_two_f2_controls_share_no_dispatch(lab):
    """Pins the independence itself, which is the whole reason there are two."""
    import ast
    import inspect as _inspect

    tree = ast.parse(_inspect.getsource(V.check_verifier_integrity).strip())
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert called.isdisjoint({"closure_offenders", "check_integration_authority",
                              "check_git_authority", "observe_integration_state"}), called
    integrity = [c for name, cats in V.CHECK_DISPATCH if name == "check_verifier_integrity"
                 for c in cats]
    authority = [c for name, cats in V.CHECK_DISPATCH if name == "check_git_authority"
                 for c in cats]
    assert integrity == ["VERIFIER_INTEGRITY"]
    assert set(integrity).isdisjoint(authority)


def test_the_live_generation_seals_the_checker_it_is_run_by():
    """The declaration is ABOUT the checker, never BY it: no self-SHA, no fixed point.

    The checker's digest lives in the snapshot; the snapshot's digest lives in the
    pointer and in the next generation's parent field. Nothing hashes itself.
    """
    plane = V.load(V.Report())
    declared = plane.snapshot["governed_implementation"]
    assert declared["verifier_path"] == V.VERIFIER_PATH
    assert declared["verifier_sha256"] == V.sha256_file(REPO / V.VERIFIER_PATH)
    source = (REPO / V.VERIFIER_PATH).read_text(encoding="utf-8")
    assert declared["verifier_sha256"] not in source, "the checker must not name its "\
        "own digest; that is the V3 fixed point wearing a different hat"
    assert plane.snapshot["state_generation"] >= V.VERIFIER_PIN_FIRST_GENERATION


def test_a_checker_whose_bytes_moved_is_refused():
    """Non-vacuity for the pin: a wrong digest must actually fail."""
    tampered = dict(V.load(V.Report()).snapshot)
    tampered["governed_implementation"] = {
        "verifier_path": V.VERIFIER_PATH, "verifier_sha256": "0" * 64}
    report = V.Report()
    V.check_verifier_integrity(_plane(tampered), report)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL"
    assert any("is not the checker this state was written under" in message
               for _, message in report.problems)


def test_a_tree_without_the_checker_fails_closed_rather_than_raising(tmp_path, monkeypatch):
    """A missing checker is a REFUSAL, never a traceback.

    ``sha256_file`` on an absent path raises, and an exception inside a dispatched check
    aborts ``run`` mid-table: every category after it then reports NOT_RUN for the wrong
    reason, and an operator sees a crash where a verdict belongs. Measured on the first
    full-suite run of this milestone, which took down
    ``s3n1::test_the_verifier_exits_non_zero_on_a_corrupted_control_plane`` -- a sandbox
    that copies the control plane and not the checker.
    """
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    report = V.Report()
    V.check_verifier_integrity(_plane({
        "state_generation": 35, "subject_state_commit": "0" * 40,
        "governed_implementation": {"verifier_path": V.VERIFIER_PATH,
                                    "verifier_sha256": "0" * 64}}), report)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL"
    assert any("is not present in this tree" in message
               for _, message in report.problems), report.problems


def test_no_dispatched_check_may_abort_the_run(monkeypatch):
    """The general form: `run` must reach the end of its table on a hostile tree.

    Pinned because the failure mode is silent in the worst way -- a raise looks like a
    crash, not like a refusal, and the categories it skipped are indistinguishable from
    ones nobody wrote a check for.
    """
    plane = V.load(V.Report())
    monkeypatch.setattr(V, "load", lambda report: plane)
    report = V.run()
    assert set(report.ran) == set(V.CATEGORIES), sorted(set(V.CATEGORIES) - report.ran)


def test_a_generation_from_35_onward_must_seal_a_checker_at_all():
    """Omitting the field is the other way to disable the pin."""
    report = V.Report()
    V.check_verifier_integrity(
        _plane({"state_generation": 35, "subject_state_commit": "0" * 40}), report)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL"
    assert any("declares no governed_implementation" in message
               for _, message in report.problems)


def test_generations_below_35_are_not_retrofitted():
    """A sealed record is never rewritten to satisfy a control invented after it."""
    assert V.VERIFIER_PIN_FIRST_GENERATION == 35
    report = V.Report()
    V.check_verifier_integrity(
        _plane({"state_generation": 34, "subject_state_commit": "0" * 40}), report)
    assert not [m for c, m in report.problems if "governed_implementation" in m]


# ══════════════════════════════════════════════════════════════════════════════
#  F3 — the authoritative suite may not erase its own failures
#
#  Measured on the S5G source, using the EXACT command `ci.yml` runs:
#
#      master fast-forwarded to the tip  ->  3 failed, 10660 passed, exit 1
#      + a trailing `tests/conftest.py`  ->  0 failed,             exit 0
#      control plane on that lineage     ->  PASS, PROBLEMS: 0
#
#  `tests/` was on the trailing surface, and the authoritative command names BOTH
#  trees, so a `pytest_collection_modifyitems` hook in the repo-level tree receives
#  every `jarvis/tests` item too and can deselect whatever it likes. Nothing in the
#  control plane looked at it. Running only `jarvis/tests` never loads that file, so
#  the whole effect is invisible unless the exact CI invocation is reproduced — which
#  is why it survived a milestone that was explicitly about closure defects.
#
#  The fix is not a conftest-shaped rule. Executable test infrastructure and the
#  authoritative test definitions are STATE-BEARING: they decide what the gate means,
#  so they belong inside a governed subject, exactly like the checker.
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("relpath", [
    "tests/conftest.py",                    # THE measured reproducer
    "jarvis/tests/conftest.py",             # the other tree's collection hook
    "conftest.py",                          # a repository-root hook
    "jarvis/conftest.py",                   # the package-root hook that exists today
    "tests/test_security.py",               # an authoritative assertion, weakened
    "jarvis/tests/test_control_plane_v4_integration_authority_v69_s5g.py",
    "jarvis/tests/pytest_plugin_probe.py",  # a plugin registered from the suite tree
    "pyproject.toml",                       # `-p` / testpaths / marker registration
    "jarvis/pyproject.toml",                # the file that really configures pytest here
    "pytest.ini", "tox.ini", "setup.cfg",   # every other pytest configuration surface
    ".github/workflows/ci.yml",             # the command itself
])
def test_executable_test_infrastructure_is_refused_as_a_trailing_change(lab, relpath):
    """Deny by default over the whole executable surface, not a conftest special case."""
    head = lab.commit_on(lab.tip, relpath, "trailing change")
    report = _staged_report(lab, head)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL", relpath
    assert any(relpath in message for _, message in report.problems), report.problems


@pytest.mark.parametrize("relpath", [
    "tests/conftest.py", "jarvis/tests/conftest.py",
    "jarvis/tests/test_control_plane_v4_integration_authority_v69_s5g.py",
    ".github/workflows/ci.yml", "pyproject.toml",
])
def test_the_same_paths_are_refused_again_by_the_independent_pin(lab, relpath):
    """And by the control that does not route through the closure's dispatch."""
    head = lab.commit_on(lab.tip, relpath, "trailing change")
    report = _integrity_report(lab, head)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL", relpath
    assert any(relpath in message for _, message in report.problems), report.problems


def test_neither_test_tree_is_on_the_trailing_surface_any_more():
    """The one-line statement of the F3 closure."""
    for tree in ("jarvis/tests/", "tests/"):
        assert tree not in V.INTEGRATION_TRAILING_PATHS
        assert not V._governed_by(f"{tree}test_x.py", V.INTEGRATION_TRAILING_PATHS)
        assert V._governed_by(f"{tree}conftest.py", V.AUTHORITY_CRITICAL_PATHS)


def test_the_authoritative_command_is_the_one_this_boundary_assumes():
    """If CI stopped naming both trees, the reasoning above would be about a command
    nobody runs. Read from the workflow rather than restated."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m pytest -q --tb=short jarvis/tests tests" in workflow
    assert ".github/workflows/" in V.AUTHORITY_CRITICAL_PATHS


def test_a_masking_conftest_is_refused_on_the_lineage_that_carries_it(lab):
    """The measured reproducer end to end, in miniature: the hook is real code and the
    lineage carrying it is refused BEFORE pytest ever becomes the authority."""
    hook = (
        "def pytest_collection_modifyitems(config, items):\n"
        "    items[:] = [i for i in items if 'master' not in i.name]\n")
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    head = _commit(lab.root, "tests/conftest.py", hook, "a collection hook")

    staged = _staged_report(lab, head)
    assert staged.status("INTEGRATION_AUTHORITY") == "FAIL"
    # And the integrated context refuses it too, rather than only the staged one.
    lab.set_target(head)
    state, _detail, offenders = V.observe_integration_state(lab.authority())
    assert state == "UNGOVERNED_TRAILING_COMMIT"
    assert state not in ADMITTED
    assert "tests/conftest.py" in offenders


# ══════════════════════════════════════════════════════════════════════════════
#  The closure's second dimension, and what stays permitted
#
#  §8 of the milestone: documentation-only trailing change may remain permitted only
#  if it is PROVABLY non-executable. Being on the permitted surface is no longer a
#  proof of that — `jarvis/docs/conftest.py` is on the surface and is code — so the
#  suffix is checked as well, from a closed allowlist. Unknown suffix, like unknown
#  path: DENY.
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("relpath", [
    "jarvis/docs/V69_S5G1_PREINTEGRATION_HARDENING.md",
    "PROGRESS.md",
    "state/m62/snapshots/0099-future.json",
    "state/m62/current.json",
])
def test_inert_governance_evidence_is_still_permitted(lab, relpath):
    """The closure is narrow, not merely strict: a governance close must still work."""
    head = lab.commit_on(lab.tip, relpath, "governed trailing evidence")
    assert _staged_report(lab, head).status("INTEGRATION_AUTHORITY") == "PASS", relpath
    assert _integrity_report(lab, head).status("VERIFIER_INTEGRITY") == "PASS", relpath


@pytest.mark.parametrize("relpath", [
    "jarvis/docs/conftest.py",           # code on the documentation surface
    "jarvis/docs/generate.sh",           # a shell script nobody classified
    "state/m62/sitecustomize.py",        # imported by every interpreter start
    "jarvis/docs/notes.txt",             # inert, but NOT on the allowlist: deny
    "state/m62/records/payload.yaml",    # a format the allowlist does not name
    "jarvis/docs/diagram.svg",           # markup that executes in a browser
])
def test_an_executable_or_unclassified_file_on_the_surface_is_still_refused(lab, relpath):
    """Deny by default, one level below the path: unknown suffix is not harmless."""
    head = lab.commit_on(lab.tip, relpath, "in-surface but not inert")
    report = _staged_report(lab, head)
    assert report.status("INTEGRATION_AUTHORITY") == "FAIL", relpath
    assert any("are not provably inert" in message
               for _, message in report.problems), report.problems


def test_the_closure_classifies_three_ways_and_denies_by_default():
    """Pins the polarity and the three classes together."""
    state_bearing, executable, ungoverned = V.closure_offenders([
        "jarvis/docs/a.md", "state/m62/b.json",          # permitted
        "jarvis/docs/c.py", "state/m62/d.sh",            # on-surface, executable
        "jarvis/core/e.py", "totally/unheard/of/f.bin",  # off-surface
        "jarvis/training_gym/g.py",                      # state-bearing
    ])
    assert state_bearing == ["jarvis/training_gym/g.py"]
    assert executable == ["jarvis/docs/c.py", "state/m62/d.sh"]
    assert ungoverned == ["jarvis/core/e.py", "jarvis/training_gym/g.py",
                          "totally/unheard/of/f.bin"]
    assert V.closure_offenders([]) == ([], [], [])


def test_the_inert_suffix_allowlist_is_closed_and_small():
    """Widening this tuple is a control-plane decision. Pinned so it is a visible one."""
    assert V.TRAILING_INERT_SUFFIXES == (".md", ".json")
    assert V._is_inert("jarvis/docs/x.md") and V._is_inert("state/m62/x.json")
    for path in ("x.py", "x.sh", "x.yml", "x.yaml", "x.toml", "x.cfg", "x.ini",
                 "x.so", "x.pth", "x.txt", "x.svg", "x", "x.MD", "x.JSON"):
        assert not V._is_inert(path), path


def test_the_permitted_surface_holds_only_what_the_allowlist_admits():
    """The allowlist was DERIVED from the repository, not guessed at.

    If a tracked file on the permitted surface has a suffix the allowlist refuses, then
    the next governance close cannot touch it and the tuple is wrong. Measured here on
    the real tree rather than asserted from a count taken once.
    """
    tracked = subprocess.run(  # nosec B603 - fixed argv, shell=False
        ["git", "-C", str(REPO), "ls-files", "state/m62", "jarvis/docs"],
        capture_output=True, text=True, check=True, shell=False).stdout.split()
    assert tracked, "expected the governance surface to be tracked at all"
    assert [p for p in tracked if not V._is_inert(p)] == []


# ══════════════════════════════════════════════════════════════════════════════
#  The new boundary must not recreate the fixed point it was built to escape
#
#  Tightening a closure is easy; tightening it into an unusable state is easier. If
#  "executable authority changes only inside a governed subject" made the NEXT
#  generation impossible to write, S5G.1 would have traded V3's fixed point for a
#  deadlock. It does not, and the whole cycle is exercised here rather than argued:
#
#      current generation -> a future governed subject (executable change)
#                         -> a successor generation over it (evidence only)
#                         -> integration
#
#  Nothing in the cycle needs a commit to know its own SHA.
# ══════════════════════════════════════════════════════════════════════════════
def test_an_executable_change_needs_a_successor_and_is_refused_until_it_gets_one(lab):
    """Step 1. The tightening is real: the future work is refused under today's record."""
    future_subject = lab.commit_on(lab.tip, "jarvis/core/runtime.py", "next milestone")
    assert _staged_report(lab, future_subject).status("INTEGRATION_AUTHORITY") == "FAIL"
    lab.set_target(future_subject)
    assert _observe(lab) not in ADMITTED


def test_a_successor_generation_governs_the_work_the_current_one_refuses(lab):
    """Steps 2 and 3. The successor is written AFTER its subject exists, so every commit
    it names already has a SHA and no field is a self-reference."""
    future_subject = lab.commit_on(lab.tip, "jarvis/core/runtime.py", "next milestone")
    successor = lab.authority(governed_subject=future_subject)
    # The successor's own governance commit: evidence only, exactly as S5G.1's is.
    future_tip = lab.commit_on(future_subject, "state/m62/snapshots/0099-next.json",
                               "the successor generation")

    lab.set_target(lab.base)
    staged = V.Report()
    V.check_integration_authority(
        _plane({"subject_state_commit": future_subject,
                "integration_authority": successor}), staged)
    assert staged.status("INTEGRATION_AUTHORITY") == "PASS", staged.problems
    assert V.observe_integration_state(successor)[0] == "TARGET_AT_AUTHORIZED_BASE"

    lab.set_target(future_tip)
    assert V.observe_integration_state(successor)[0] == "INTEGRATED_FAST_FORWARD"
    assert V.observe_integration_state(successor)[0] in ADMITTED


def test_the_successor_cycle_contains_no_self_reference(lab):
    """The V3 fixed point, checked for directly rather than assumed absent.

    Every commit a successor names must already resolve when the successor is written,
    and no field may hold the SHA of the commit that carries it.
    """
    future_subject = lab.commit_on(lab.tip, "jarvis/core/runtime.py", "next milestone")
    successor = lab.authority(governed_subject=future_subject)
    future_tip = lab.commit_on(future_subject, "state/m62/snapshots/0099-next.json",
                               "the successor generation")
    for field_name, sha in successor.items():
        if V.COMMIT_RE.fullmatch(str(sha)):
            assert V._commit_exists(sha), field_name
            assert sha != future_tip, (
                f"{field_name} names the commit that carries the record; that is the "
                f"V3 fixed point")


def test_the_checker_pin_does_not_become_a_fixed_point_either():
    """A generation seals the checker's digest; the checker never seals its own.

    The ordering that makes this work — finalise the checker, commit it as the subject,
    digest it, then write the generation — is the same discipline `integration_authority`
    already follows, and it is asserted on the live record rather than described.
    """
    source = (REPO / V.VERIFIER_PATH).read_text(encoding="utf-8")
    live = V.load(V.Report()).snapshot
    digest = live["governed_implementation"]["verifier_sha256"]
    assert digest not in source
    assert live["subject_state_commit"] not in source
    assert live["integration_authority"]["governed_subject"] not in source


def test_a_governance_only_close_still_verifies_after_the_tightening(lab):
    """The practicality gate. If this fails, no milestone can ever close again."""
    for relpath in ("state/m62/snapshots/0099-next.json", "state/m62/current.json",
                    "PROGRESS.md", "jarvis/docs/V69_NEXT.md"):
        head = lab.commit_on(lab.tip, relpath, "a governance close")
        assert _staged_report(lab, head).status("INTEGRATION_AUTHORITY") == "PASS", relpath


# ══════════════════════════════════════════════════════════════════════════════
#  The three findings are recorded as defects, and their statuses are frozen
#
#  Each of D49, D50 and D51 PASSED every gate this repository had while it was
#  live — the control plane, the focused control-plane suites and the full
#  authoritative suite. "The suite would catch a regression" is precisely the
#  assumption that already failed here, three times, so the statuses are pinned in
#  the checker as well as recorded in the generation.
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("defect,status", [("D49", "FIXED"), ("D50", "FIXED"),
                                           ("D51", "FIXED")])
def test_the_s5g1_defects_are_recorded_and_frozen(defect, status):
    assert V.FROZEN_DEFECT_STATUSES[defect] == status
    live = V.load(V.Report()).snapshot
    entry = next(d for d in live["defects"] if d["id"] == defect)
    assert entry["status"] == status
    assert entry["is_gate"] is False, "a closure is not an evaluation gate"


def test_each_s5g1_defect_names_the_reproducer_that_found_it():
    """A defect summary that does not say how to reproduce it is a claim, not a record."""
    live = V.load(V.Report()).snapshot
    summaries = {d["id"]: d["summary"] for d in live["defects"]}
    assert "INTEGRATED_FAST_FORWARD" in summaries["D49"]
    assert "check_git_authority" in summaries["D50"]
    assert "conftest" in summaries["D51"]


def test_the_scientific_state_did_not_move():
    """S5G.1 is GOVERNANCE ONLY. Asserted, because a milestone that touches the
    checker is exactly where a quiet scientific edit would hide."""
    live = V.load(V.Report()).snapshot
    candidates = {c["candidate_id"]: c for c in live["candidates"]}
    held = candidates["qwen3-06b-lora-quality-live-004"]
    assert held["status"] == "EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert candidates["qwen3-06b-lora-quality-live-005"]["status"] == (
        "EVALUATED_NOT_ELIGIBLE")
    assert "qwen3-06b-lora-quality-live-006" not in candidates
    datasets = {(d["dataset_id"], d["version"]): d for d in live["datasets"]}
    assert datasets[("m62-defensive-eval", "v7")]["status"] == "USED_IMMUTABLE"
    assert ("m62-defensive-eval", "v8") not in datasets, "no eval-v8 exists"
    assert datasets[("m62-defensive-quality-train", "v2")]["status"] == "USED_IMMUTABLE"
    assert ("m62-defensive-quality-train", "v3") not in datasets, "no train-v3 exists"
    observation = live["authority_observation"]
    assert observation["control_plane_can_grant_authority"] is False
    for key in ("train", "eval", "promotion"):
        assert observation[key] == "NONE_OBSERVED_IN_REPOSITORY"
    assert all(c["status"] != "PROMOTED" for c in live["candidates"])


# ══════════════════════════════════════════════════════════════════════════════
#  D52 / D53 — what an independent red team measured on S5G.1's FIRST attempt
#
#  Both were reproduced on that tree before either was repaired, and both falsified
#  a claim this milestone had already written down. They are kept here as the
#  reproducers, because the failure they describe is the failure this whole
#  milestone is about: a control that reads closed and is not.
#
#  D52 — the checker was never in CI. `.github/workflows/ci.yml` did not run it;
#  it reached CI only through a handful of pytest tests, and the authoritative
#  command names BOTH trees, so a trailing `tests/conftest.py` could deselect
#  exactly those tests. Measured: verifier FAIL on a tree with an ungoverned
#  runtime module, `pytest -q jarvis/tests tests` exit 0 on the same tree. The
#  documented claim — "the verifier/closure rejects such a lineage BEFORE pytest
#  can become the authority" — described a sequencing that did not exist.
#
#  D53 — the digest pin's reference value was editable beside it. ONE trailing
#  commit that weakened the checker AND rewrote
#  `governed_implementation.verifier_sha256` in the live snapshot — a `.json` on
#  the permitted surface — verified PASS / PROBLEMS: 0 with GIT_AUTHORITY,
#  INTEGRATION_AUTHORITY and VERIFIER_INTEGRITY all green. The same class of edit
#  rewrote an evaluation receipt's measured delta, gutted `scientific-suite.json`
#  from 54 modules to 12, and rewrote the append-never-edit archive while
#  ARCHIVE_INTEGRITY still read PASS.
# ══════════════════════════════════════════════════════════════════════════════
def test_the_control_plane_job_installs_nothing():
    """It must stay pure stdlib: a resolution failure elsewhere cannot silence the gate.

    That is also why the collection check cannot live here -- it needs pytest, and putting
    it beside the verifier failed real CI with `No module named pytest`.
    """
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    assert "pip install" not in job, "the checker's own job must not depend on a resolve"
    assert "python -m pytest" not in job, "the checker's own job must not need pytest"


def test_the_checker_runs_in_ci_as_a_step_not_as_a_test():
    """D52. A collection hook can deselect a test. It cannot deselect a step."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python jarvis/scripts/verify_m62_control_plane.py" in workflow, (
        "no CI job runs the control-plane verifier, so the only thing that enforces the "
        "governance closure is reachable only through tests a trailing conftest removes")
    # And it must have the history the checker needs, or it fails closed on every run.
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    assert "fetch-depth: 0" in job, "the verifier resolves refs; depth-1 fails it closed"
    assert "python jarvis/scripts/verify_m62_control_plane.py" in job


def test_the_verifier_step_is_not_reachable_only_through_pytest():
    """D52, stated as the property rather than the fix.

    If the ONLY invocations live under a test tree, the suite is the checker's gate and
    the checker is the suite's gate, and one trailing conftest ends both.
    """
    import subprocess  # noqa: PLC0415

    tracked = subprocess.run(  # nosec B603 - fixed argv, shell=False
        ["git", "-C", str(REPO), "grep", "-l", "verify_m62_control_plane.py",
         "--", ".github/"],
        capture_output=True, text=True, check=False, shell=False).stdout.split()
    assert tracked, "no workflow file invokes the verifier"


@pytest.mark.parametrize("relpath", [
    "state/m62/snapshots/0035-s5g1-preintegration-hardening.json",
    "state/m62/scientific-suite.json",
    "jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md",
])
def test_a_sealed_artefact_may_not_be_rewritten_by_a_trailing_commit(lab, relpath):
    """D53. Append-only is what the control plane always claimed for these paths."""
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    target = lab.root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    _run(lab.root, "add", "-A")
    _run(lab.root, "commit", "-q", "-m", "seal the artefact inside the subject")
    sealed = _run(lab.root, "rev-parse", "HEAD")
    target.write_text('{"rewritten": true}\n', encoding="utf-8")
    _run(lab.root, "add", "-A")
    _run(lab.root, "commit", "-q", "-m", "rewrite it in a trailing commit")
    head = _run(lab.root, "rev-parse", "HEAD")

    report = V.Report()
    V.check_verifier_integrity(
        _plane({"subject_state_commit": sealed, "state_generation": 34,
                "integration_authority": lab.authority(governed_subject=sealed)}), report)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL", relpath
    assert any("APPEND-ONLY" in message for _, message in report.problems), report.problems

    # And the target lineage refuses it too, with its own observation.
    lab.set_target(head)
    state, _detail, offenders = V.observe_integration_state(
        lab.authority(governed_subject=sealed))
    assert state == "SEALED_STATE_REWRITTEN"
    assert state not in ADMITTED
    assert any(relpath in o for o in offenders), offenders


def test_the_pointer_is_the_one_sealed_path_that_may_move(lab):
    """A generation that could not move current.json could not be published at all."""
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    (lab.root / "state/m62/current.json").write_text('{"g": 2}\n', encoding="utf-8")
    (lab.root / "state/m62/snapshots").mkdir(parents=True, exist_ok=True)
    (lab.root / "state/m62/snapshots/0099-next.json").write_text("{}\n", encoding="utf-8")
    _run(lab.root, "add", "-A")
    _run(lab.root, "commit", "-q", "-m", "a real governance close: add a snapshot, move the pointer")
    head = _run(lab.root, "rev-parse", "HEAD")
    assert _staged_report(lab, head).status("INTEGRATION_AUTHORITY") == "PASS"
    assert _integrity_report(lab, head).status("VERIFIER_INTEGRITY") == "PASS"


def test_sealed_rewrites_reads_status_not_names():
    """`--name-only` cannot tell an addition from a rewrite, and that blindness IS D53."""
    assert V.sealed_rewrites(["A\tstate/m62/snapshots/0036-x.json"]) == []
    assert V.sealed_rewrites(["M\tstate/m62/snapshots/0035-x.json"]) == [
        "M state/m62/snapshots/0035-x.json"]
    assert V.sealed_rewrites(["D\tstate/m62/records/abc.json"]) == [
        "D state/m62/records/abc.json"]
    assert V.sealed_rewrites(["M\tstate/m62/current.json"]) == []
    assert V.sealed_rewrites(["M\tPROGRESS.md"]) == []
    assert V.sealed_rewrites(["M\tjarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md"]) == [
        "M jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md"]
    assert V.SEALED_TRAILING_ROOTS == ("state/m62/", "jarvis/docs/m62/history/")


def test_every_lineage_diff_that_decides_immutability_uses_name_status():
    """Pins the flag. `--name-only` here is the whole of D53."""
    import ast  # noqa: PLC0415
    import inspect as _inspect  # noqa: PLC0415

    for func in (V._observe_one, V.check_verifier_integrity):
        source = ast.parse(_inspect.getsource(func).strip())
        calls = [n for n in ast.walk(source)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_git"]
        status_diffs = [c for c in calls
                        if [a.value for a in c.args if isinstance(a, ast.Constant)][:1]
                        == ["diff"]
                        and "--name-status" in [a.value for a in c.args
                                                if isinstance(a, ast.Constant)]]
        assert status_diffs, f"{func.__name__} decides immutability without --name-status"
        for call in status_diffs:
            args = [a.value for a in call.args if isinstance(a, ast.Constant)]
            assert "--no-renames" in args, func.__name__


def test_progress_may_not_lose_the_standing_conclusion():
    """D53's quietest variant: delete the milestone's own conclusion, verifier still green."""
    assert V.PROGRESS_REQUIRED_CLAUSES == ("EXTERNAL_CONTROL_PENDING",
                                           "PROSE_CANNOT_GRANT_AUTHORITY")
    live = (REPO / "PROGRESS.md").read_text(encoding="utf-8")
    for clause in V.PROGRESS_REQUIRED_CLAUSES:
        assert clause in live, clause


def test_the_required_progress_clause_check_is_not_vacuous(tmp_path, monkeypatch):
    """Prove the refusal fires, rather than trusting that it would."""
    (tmp_path / "PROGRESS.md").write_text("nothing load-bearing here\n", encoding="utf-8")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    report = V.Report()
    V.check_verifier_integrity(
        _plane({"state_generation": 35, "subject_state_commit": "0" * 40}), report)
    assert report.status("VERIFIER_INTEGRITY") == "FAIL"
    assert any("EXTERNAL_CONTROL_PENDING" in m for _, m in report.problems), report.problems


@pytest.mark.parametrize("defect,status", [("D52", "FIXED"), ("D53", "FIXED")])
def test_the_red_team_defects_are_recorded_and_frozen(defect, status):
    """The two findings that falsified this milestone's own first draft."""
    assert V.FROZEN_DEFECT_STATUSES[defect] == status
    live = V.load(V.Report()).snapshot
    entry = next(d for d in live["defects"] if d["id"] == defect)
    assert entry["status"] == status
    assert entry["is_gate"] is False


def test_the_red_team_defects_name_their_reproducers():
    live = V.load(V.Report()).snapshot
    summaries = {d["id"]: d["summary"] for d in live["defects"]}
    assert "ci.yml" in summaries["D52"]
    assert "verifier_sha256" in summaries["D53"]


def test_ci_guards_collection_hooks_without_consulting_the_checker():
    """The one control in this milestone that no constant of the checker can disarm.

    MEASURED, and it is why it is written in shell rather than in Python. The maximal
    one-commit attack — empty `AUTHORITY_CRITICAL_PATHS`, `SEALED_TRAILING_ROOTS`,
    `PROGRESS_REQUIRED_CLAUSES`, re-widen `INTEGRATION_TRAILING_PATHS` and
    `TRAILING_INERT_SUFFIXES`, then reseal `verifier_sha256` — makes the checker report
    ``PASS / PROBLEMS: 0`` about itself. It is the residual §3 of the milestone document
    names and it cannot be closed from inside the checker.

    What it CANNOT do is stop `git ls-files` listing a file. This step depends on nothing
    the checker owns, and the masking route needs a third collection hook.

    The authoritative suite is the other half: the same maximal attack fails 33 tests in
    the two control-plane modules, because they pin these constants literally. Silencing
    those needs a conftest — which is what this step refuses.
    """
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    assert "git ls-files" in job, (
        "the collection-surface guard is gone; every remaining control routes through a "
        "checker that a single commit can make agree with itself")
    # Every file pytest reads as configuration, not just conftests: a red team took the
    # whole gate green with a ROOT pyproject.toml whose addopts --ignored the two
    # control-plane modules, which a conftest-only guard never looks at.
    for surface in ("*conftest.py", "*pyproject.toml", "*pytest.ini", "*tox.ini",
                    "*setup.cfg"):
        assert surface in job, f"the guard does not enumerate {surface}"
    # The guard must run BEFORE the verifier: a checker that has already said PASS is
    # not improved by a later objection.
    assert job.index("git ls-files") < job.index("verify_m62_control_plane.py")
    # The semantic half is its OWN mandatory job. It cannot sit beside the verifier --
    # that job installs nothing and it failed real CI with `No module named pytest` -- and
    # it cannot sit inside `tests`, because S5E requires exactly one pytest invocation
    # there so that which command is authoritative is never ambiguous.
    collect = workflow[workflow.index("  collection-integrity:"):]
    collect = collect[:collect.index("\n  compat:")]
    assert "--collect-only" in collect and "test_control_plane_v4" in collect, (
        "nothing checks that the control-plane modules survive collection")
    assert "jarvis/tests tests" in collect, (
        "the check must use the AUTHORITATIVE argument list: --ignore does not apply to "
        "a path named explicitly, so naming the modules collects them on the very tree "
        "this exists to catch")
    assert "pip install" in collect, "the suite must be importable to be collectable"


def test_the_hook_allowlist_is_exactly_what_the_repository_has():
    """A guard listing a hook the tree does not have would pass while enforcing nothing;
    one missing a hook the tree does have would fail every honest run."""
    import subprocess  # noqa: PLC0415

    tracked = subprocess.run(  # nosec B603 - fixed argv, shell=False
        ["git", "-C", str(REPO), "ls-files", "*conftest.py", "*pyproject.toml",
         "*pytest.ini", "*tox.ini", "*setup.cfg"],
        capture_output=True, text=True, check=True, shell=False).stdout.split()
    assert sorted(tracked) == ["jarvis/conftest.py", "jarvis/pyproject.toml",
                               "jarvis/tests/conftest.py"]
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    for hook in tracked:
        assert hook in job, hook


# ══════════════════════════════════════════════════════════════════════════════
#  D54 — what a SECOND red team found, against the fix for D52/D53
#
#  (a) The masking route needed no third conftest. The conceded §3 residual also
#      empties AUTHORITY_CRITICAL_PATHS, which is the only thing naming
#      `pyproject.toml` — so the same one commit could add a tracked ROOT
#      `pyproject.toml` whose `[tool.pytest.ini_options] addopts` `--ignore`d the
#      two control-plane modules. Measured on the branch: collection fell from
#      10,830 to 10,659, ZERO control-plane tests ran, suite exit 0, and the shell
#      guard — which greps for conftests — reported "allowlisted only".
#  (b) A target carrying a forged `current.json`, or one that simply deleted
#      PROGRESS.md, was observed as "a governed fast-forward". The closure
#      classifies filenames; `current.json` is exempt from append-only by
#      construction; nothing read what the target's control plane now SAYS.
#  (c) The shape guard was satisfiable by a docstring and evaded by a decorator.
#  (d) The guard's `x=$(git ls-files ...)` is fail-open: command substitution does
#      not trip `set -e`, so outside a repository it reported "only the allowlisted
#      ones" having enumerated nothing.
# ══════════════════════════════════════════════════════════════════════════════
def test_the_ci_guard_covers_every_surface_pytest_reads_as_configuration():
    """D54(a). A conftest-only guard does not see `pyproject.toml`."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    for surface in ("*conftest.py", "*pyproject.toml", "*pytest.ini", "*tox.ini",
                    "*setup.cfg"):
        assert surface in job, surface
    assert "jarvis/pyproject.toml" in job, "the live pytest configfile must be allowlisted"


def test_the_ci_guard_enumerates_before_it_allowlists():
    """D54(d). `x=$(git ...)` is fail-open under `set -e`; a redirect is not."""
    workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  control-plane:"):]
    job = job[:job.index("\n  lint:")]
    assert "surfaces=$(git ls-files" not in job, (
        "command substitution hides git's exit status from `set -e`, so a failure to "
        "enumerate reports an empty list as a clean one")
    assert "> pytest-surfaces.txt" in job


def test_a_target_whose_control_plane_does_not_read_as_one_is_refused(lab):
    """D54(b). The observation is about the TARGET's state, not only its filenames."""
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    (lab.root / "state/m62/current.json").write_text('{"pointer": "forged"}\n',
                                                     encoding="utf-8")
    _run(lab.root, "add", "-A")
    _run(lab.root, "commit", "-q", "-m", "repoint the control plane at nothing")
    forged = _run(lab.root, "rev-parse", "HEAD")
    lab.set_target(forged)
    state, detail, _ = V.observe_integration_state(lab.authority())
    assert state == "TARGET_STATE_INCOHERENT", detail
    assert state not in ADMITTED
    assert "names no snapshot" in detail


def test_a_target_that_deleted_progress_is_refused(lab):
    """The quietest variant: delete the standing conclusion on the target itself."""
    _run(lab.root, "checkout", "-q", "--detach", lab.tip)
    _run(lab.root, "rm", "-q", "PROGRESS.md")
    _run(lab.root, "commit", "-q", "-m", "delete PROGRESS.md on the target")
    gone = _run(lab.root, "rev-parse", "HEAD")
    lab.set_target(gone)
    state, detail, _ = V.observe_integration_state(lab.authority())
    assert state == "TARGET_STATE_INCOHERENT", detail
    assert state not in ADMITTED
    assert "PROGRESS.md is missing from the target tree" in detail


def test_target_coherence_reads_the_target_not_the_working_tree(lab):
    """A healthy checkout says nothing about a ref somebody else can write to."""
    import ast  # noqa: PLC0415
    import inspect as _inspect  # noqa: PLC0415

    tree = ast.parse(_inspect.getsource(V._target_state_incoherence).strip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_git"]
    assert calls, "the coherence check consults Git not at all"
    for call in calls:
        args = [a.value for a in call.args if isinstance(a, ast.Constant)]
        assert args and args[0] in {"cat-file", "show"}, args
    body = ast.unparse(tree)
    assert "REPO_ROOT" not in body, (
        "the target's coherence must be read out of the target tree, never from here")
    # And the healthy lab target still passes, so the refusals above are not vacuous.
    assert V._target_state_incoherence(lab.tip) == ""


def test_a_missing_progress_is_refused_rather_than_raised(tmp_path, monkeypatch):
    """D54 / MINOR. `check_stale_state` read PROGRESS.md unguarded and raised."""
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    report = V.Report()
    V.check_stale_state(_plane({"subject_state_commit": "0" * 40}), report)
    assert report.status("STALE_STATE") == "FAIL"
    assert any("is missing" in message for _, message in report.problems)


def test_the_verifier_says_out_loud_when_the_checker_changed():
    """D54 / MAJOR. The two-commit route ended with the verifier affirming
    "checker pinned" and never mentioning that the checker itself had moved."""
    notes = "\n".join(V.run().notes)
    assert ("the checker is UNCHANGED since generation" in notes
            or "THE CHECKER CHANGED between generation" in notes
            or "is the first to pin one" in notes), notes


def test_the_checker_delta_note_is_not_vacuous():
    """It must actually distinguish the two cases."""
    live = V.load(V.Report()).snapshot
    generation = live["state_generation"]
    actual = live["governed_implementation"]["verifier_sha256"]
    assert "UNCHANGED" in V._checker_generation_delta(generation, actual) or \
        "first to pin one" in V._checker_generation_delta(generation, actual)
    changed = V._checker_generation_delta(generation, "0" * 64)
    assert "THE CHECKER CHANGED" in changed or "first to pin one" in changed


def test_d54_is_recorded_and_frozen():
    assert V.FROZEN_DEFECT_STATUSES["D54"] == "FIXED"
    live = V.load(V.Report()).snapshot
    entry = next(d for d in live["defects"] if d["id"] == "D54")
    assert entry["status"] == "FIXED"
    assert entry["is_gate"] is False
    assert "pyproject" in entry["summary"]
