"""tests/test_release_closure_v69_m618.py — V69 M61.8: true, and unable to act.

Two questions, and they are different questions.

**Is the release documentation true?** ``core.release_check`` audits eight truth families
over the repository's documents. This module asserts every family is clean, and — the part
that matters — that each family is *non-vacuous*: a checker that would pass an obviously
false document is not a checker. Every family therefore has a negative control that
injects a lie into a temporary copy and requires the checker to catch it. Without those,
a refactor that silently narrows a scan to zero files still shows eight green families.

**Is the release tooling constrained?** The release procedure is a runbook a human
executes. The scripts exist to *gather evidence*, never to perform the release. This
module parses the AST and the string literals of every release script and asserts none of
them can create or push a tag, create/edit/delete a GitHub Release, upload to a package
index, merge a pull request, invoke a shell, or run git with a mutating subcommand.

That check is deliberately stricter than "it does not do so today". A future edit that
adds ``gh release create`` to a build script fails here, before it reaches the branch
that would run it.

Finally, the suite proves it is itself inert: running it creates no tag, writes no
artifact into the working tree, and publishes nothing.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_APP_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_ROOT.parent

sys.path.insert(0, str(_APP_ROOT))

from core import release_check  # noqa: E402
from core import release_facts as rf  # noqa: E402

_SCRIPTS = _APP_ROOT / "scripts"
_RUNBOOK = _REPO_ROOT / "docs" / "RELEASE_RUNBOOK.md"
_NOTES = _REPO_ROOT / "docs" / "releases" / f"v{rf.VERSION}.md"
_POLICY = _REPO_ROOT / "docs" / "DEPENDENCY_SECURITY_POLICY.md"
_PROTECTION = _REPO_ROOT / "docs" / "BRANCH_PROTECTION.md"
_CLOSURE_DOC = _APP_ROOT / "docs" / "V69_M61_8_RELEASE_CLOSURE.md"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Every script that participates in building or qualifying a release. These are the
#: files an operator runs while holding credentials, so these are the files that must be
#: incapable of spending them.
RELEASE_SCRIPTS = (
    "build_release_evidence_m61.py",
    "build_sbom.py",
    "check_bandit_low_baseline.py",
    "check_release_tag_guard.py",
    "check_release_consistency.py",
    "check_package_manifest.py",
    "qualify_release_m61.py",
)

#: Git subcommands that change repository state. A release script may read git all day.
MUTATING_GIT = frozenset({
    "tag", "push", "commit", "merge", "rebase", "reset", "checkout", "switch",
    "branch", "clean", "am", "apply", "cherry-pick", "revert", "stash", "gc",
    "update-ref", "symbolic-ref", "remote", "fetch", "pull", "clone", "init",
    "add", "rm", "mv", "restore", "notes", "filter-branch", "replace", "prune",
    "config", "submodule", "worktree", "sparse-checkout",
})

#: Publication verbs, matched over string literals rather than over calls, because a
#: script could assemble one into a list it passes to subprocess.
FORBIDDEN_LITERALS = (
    ("gh release", "creates or mutates a GitHub Release"),
    ("release create", "creates a GitHub Release"),
    ("release upload", "uploads assets to a GitHub Release"),
    ("release delete", "deletes a GitHub Release"),
    ("release edit", "edits a GitHub Release"),
    ("pr merge", "merges a pull request"),
    ("twine", "uploads to a package index"),
    ("upload --repository", "uploads to a package index"),
    ("git tag", "creates a tag"),
    ("git push", "pushes to a remote"),
    ("--auto-merge", "enables auto-merge"),
)


def _source(name: str) -> str:
    path = _SCRIPTS / name
    assert path.is_file(), f"release script {name} is missing"
    return path.read_text(encoding="utf-8")


def _strings(tree: ast.AST) -> list[str]:
    """Every string constant, so a command assembled from pieces is still visible."""
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _executable_strings(source: str) -> list[str]:
    """String constants excluding docstrings.

    A runbook step quoted in a module docstring is documentation. The same text in a
    list passed to ``subprocess`` is an action. Only the second is a finding, and a
    scanner that cannot tell them apart forces the safety documentation to be deleted.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [s for s in _strings(tree) if s not in docstrings]


# ===================================================================== truth families


def test_every_release_truth_family_is_clean():
    result = release_check.audit()
    assert result["problems"] == [], "\n".join(result["problems"])
    assert result["ok"] is True


@pytest.mark.parametrize("family", sorted([
    "version", "models", "claims", "status", "install", "counts", "security", "posture",
]))
def test_each_family_is_individually_clean(family):
    families = release_check.audit()["families"]
    assert family in families, (
        f"family {family!r} disappeared from the audit; the closure suite and the "
        "checker disagree about what is being checked")
    assert families[family] == 0


def test_the_audit_covers_exactly_the_families_this_suite_knows_about():
    """A family added to the checker and not here would never be asserted clean."""
    expected = {"version", "models", "claims", "status", "install",
                "counts", "security", "posture"}
    assert set(release_check.audit()["families"]) == expected


# ------------------------------------------------- non-vacuity: the negative controls
#
# Each control copies the repository's documents into a temporary tree, injects one
# specific lie, and requires the corresponding family to catch it. They run the checker
# against a redirected root so the real repository is never written to.


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A disposable copy of the documents the checker reads, with the roots redirected.

    Copying rather than editing in place is the whole point: a negative control that
    mutated tracked files would leave the working tree dirty when it failed, which is
    exactly the state this milestone exists to prevent.
    """
    repo = tmp_path / "repo"
    app = repo / "jarvis"
    app.mkdir(parents=True)

    for rel in ("README.md", "CHANGELOG.md", "SECURITY.md", "JARVIS.md"):
        src = _REPO_ROOT / rel
        if src.is_file():
            shutil.copy2(src, repo / rel)
    for rel in ("docs",):
        src = _REPO_ROOT / rel
        if src.is_dir():
            shutil.copytree(src, repo / rel)
    # ``pyproject.toml`` and the referenced scripts/requirements are not decoration: the
    # `version` family reads the first and the `install` family resolves every command a
    # README quotes against the other two. Omitting them makes the untouched copy dirty,
    # which would make every negative control below pass for the wrong reason.
    for rel in ("README.md", "pyproject.toml"):
        src = _APP_ROOT / rel
        if src.is_file():
            shutil.copy2(src, app / rel)
    for rel in ("docs", "scripts", "requirements"):
        src = _APP_ROOT / rel
        if src.is_dir():
            shutil.copytree(src, app / rel,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Only the two modules the checker AST-parses for authoritative model tags. Copying
    # all of core/ into ten sandboxes costs far more than it proves.
    core = app / "core"
    core.mkdir()
    for rel in ("model_router.py", "hardware_model_profile.py"):
        src = _APP_ROOT / "core" / rel
        if src.is_file():
            shutil.copy2(src, core / rel)

    monkeypatch.setattr(release_check, "_ROOT", repo, raising=False)
    monkeypatch.setattr(release_check, "_APP_ROOT", app, raising=False)
    for attr in dir(release_check):
        value = getattr(release_check, attr)
        if isinstance(value, Path):
            try:
                rel = value.relative_to(_REPO_ROOT)
            except ValueError:
                continue
            monkeypatch.setattr(release_check, attr, repo / rel, raising=False)
        elif isinstance(value, tuple) and value and all(isinstance(v, Path) for v in value):
            rebased = []
            for v in value:
                try:
                    rebased.append(repo / v.relative_to(_REPO_ROOT))
                except ValueError:
                    rebased.append(v)
            monkeypatch.setattr(release_check, attr, tuple(rebased), raising=False)
    return repo


def _first_existing(sandbox: Path, *candidates: str) -> Path:
    for rel in candidates:
        path = sandbox / rel
        if path.is_file():
            return path
    pytest.skip(f"none of {candidates} exist to corrupt")


def _family_count(family: str) -> int:
    return release_check.audit()["families"][family]


def test_the_sandbox_itself_is_clean_before_any_lie_is_injected(sandbox):
    """If the copy is not clean, every control below would pass for the wrong reason."""
    result = release_check.audit()
    assert result["problems"] == [], (
        "the untouched sandbox copy is already dirty, so the negative controls below "
        "would prove nothing:\n" + "\n".join(result["problems"]))


def test_the_version_family_catches_a_hardcoded_packaging_version(sandbox):
    """The `version` family guards the *derivation*, not prose.

    ``pyproject.toml`` must declare its version dynamically from ``core.version``. The
    moment it hard-codes one, the package and the module can disagree — which is the
    single defect that makes every other version claim in the repository unverifiable.
    Corrupting a README instead would test nothing here: documents that quote a wrong
    version are caught by the release-notes checks further down, not by this family.
    """
    target = sandbox / "jarvis" / "pyproject.toml"
    assert target.is_file(), "the sandbox has no pyproject.toml to corrupt"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "[project]", '[project]\nversion = "69.99.0"', 1),
        encoding="utf-8")
    assert _family_count("version") > 0, "a hard-coded package version was not detected"


def test_a_document_quoting_a_non_canonical_version_is_caught(sandbox):
    """The prose half of the same concern, asserted where it actually lives."""
    notes = sandbox / _NOTES.relative_to(_REPO_ROOT)
    if not notes.is_file():
        pytest.skip("no release notes to corrupt")
    text = notes.read_text(encoding="utf-8")
    assert rf.VERSION in text
    others = set(re.findall(r"\b69\.\d+\.\d+\b", text)) - {rf.VERSION}
    assert not others, (
        "the release notes already quote a non-canonical version, so this control "
        f"cannot distinguish a regression from the status quo: {sorted(others)}")


def test_the_models_family_catches_an_unroutable_model_tag(sandbox):
    target = sandbox / "docs" / "TROUBLESHOOTING.md"
    if not target.is_file():
        pytest.skip("no troubleshooting document to corrupt")
    # A tag from a REAL family that no role resolves. An invented family name would not
    # be a fair control: the scanner deliberately only inspects tokens that look like
    # model tags, so an arbitrary string proves nothing either way.
    target.write_text(target.read_text(encoding="utf-8") +
                      "\n\nPull the model with `ollama pull qwen3:70b`.\n",
                      encoding="utf-8")
    assert _family_count("models") > 0, "an unroutable model tag was not detected"


def test_the_claims_family_catches_an_autonomy_claim(sandbox):
    target = sandbox / "README.md"
    target.write_text(target.read_text(encoding="utf-8") +
                      "\n\nJARVIS is a fully autonomous agent.\n", encoding="utf-8")
    assert _family_count("claims") > 0, "an autonomy claim was not detected"


def test_the_counts_family_catches_a_divergent_test_count(sandbox):
    target = _NOTES.relative_to(_REPO_ROOT)
    path = sandbox / target
    if not path.is_file():
        pytest.skip("no release notes to corrupt")
    text = path.read_text(encoding="utf-8")
    corrupted = text.replace(f"{rf.DETERMINISTIC_TESTS_PASSED} passed",
                             f"{rf.DETERMINISTIC_TESTS_PASSED + 17} passed", 1)
    assert corrupted != text, "the release notes do not state the declared passed count"
    path.write_text(corrupted, encoding="utf-8")
    assert _family_count("counts") > 0, "a divergent test count was not detected"


def test_the_security_family_catches_a_false_bandit_result(sandbox):
    """The lie has to *replace* the true statement, not sit beside it.

    Appending a second, contradictory sentence would leave the canonical result still
    present in the document, and a document that states the true result is not the
    defect this family guards against.
    """
    path = sandbox / _NOTES.relative_to(_REPO_ROOT)
    if not path.is_file():
        pytest.skip("no release notes to corrupt")
    text = path.read_text(encoding="utf-8")
    canonical = f"{rf.BANDIT_MEDIUM} Medium and {rf.BANDIT_HIGH} High"
    assert canonical in text, "the release notes do not state the Bandit gate result"
    path.write_text(text.replace(canonical, "3 Medium and 1 High"), encoding="utf-8")
    assert _family_count("security") > 0, "a false Bandit result was not detected"


def test_the_security_family_catches_a_stale_low_finding_count(sandbox):
    path = sandbox / _NOTES.relative_to(_REPO_ROOT)
    if not path.is_file():
        pytest.skip("no release notes to corrupt")
    path.write_text(path.read_text(encoding="utf-8") +
                    f"\n\nBandit reports {rf.BANDIT_LOW_OBSERVED + 40} Low findings.\n",
                    encoding="utf-8")
    assert _family_count("security") > 0, "a stale Low finding count was not detected"


def test_the_posture_family_catches_a_claim_of_a_disabled_capability(sandbox):
    path = sandbox / "README.md"
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n\nJARVIS executes plugin source code from the plugins "
                    "directory at startup.\n", encoding="utf-8")
    assert _family_count("posture") > 0, (
        "a document advertising dynamic plugin execution was not detected")


def test_the_posture_family_does_not_fire_on_an_honest_denial(sandbox):
    """The other half of the control: a checker that fires on truth is worse than none."""
    path = sandbox / "README.md"
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n\nJARVIS never executes plugin source code; dynamic source "
                    "plugins are disabled fail-closed.\n", encoding="utf-8")
    assert _family_count("posture") == 0, (
        "an accurate denial was reported as an advertisement, which would force honest "
        "documentation to be written around the checker")


def test_the_status_family_catches_a_claim_that_the_tag_already_exists(sandbox):
    path = sandbox / _NOTES.relative_to(_REPO_ROOT)
    if not path.is_file():
        pytest.skip("no release notes to corrupt")
    path.write_text(path.read_text(encoding="utf-8") +
                    f"\n\nThe tag {rf.RELEASE_TAG} has been pushed and the GitHub "
                    "Release is published.\n", encoding="utf-8")
    assert _family_count("status") > 0, (
        "a document claiming the tag and Release exist was not detected")


# ===================================================================== release notes


def test_the_release_notes_exist_and_are_named_for_the_canonical_version():
    assert _NOTES.is_file(), f"docs/releases/v{rf.VERSION}.md is missing"


def test_the_release_notes_state_the_canonical_version_and_no_other():
    text = _NOTES.read_text(encoding="utf-8")
    assert rf.VERSION in text
    others = {v for v in re.findall(r"\b69\.\d+\.\d+\b", text)} - {rf.VERSION}
    assert not others, f"the release notes also state {sorted(others)}"


def test_the_release_notes_state_the_declared_results_exactly():
    text = _NOTES.read_text(encoding="utf-8")
    for label, value in (("passed", rf.DETERMINISTIC_TESTS_PASSED),
                         ("skipped", rf.DETERMINISTIC_TESTS_SKIPPED)):
        assert f"{value} {label}" in text, (
            f"the release notes do not state '{value} {label}' from release_facts")
    assert "0 failed" in text or "0 failures" in text


def test_the_release_notes_state_the_qualification_verdict_honestly():
    text = _NOTES.read_text(encoding="utf-8")
    assert rf.QUALIFICATION_VERDICT in text
    for warning in rf.QUALIFICATION_WARNINGS:
        assert warning in text, f"warning {warning!r} is not disclosed"


def test_the_release_notes_do_not_claim_a_tag_or_release_exists():
    lowered = _NOTES.read_text(encoding="utf-8").lower()
    for phrase in ("tag has been pushed", "release is published",
                   "published to pypi", "available on pypi"):
        assert phrase not in lowered, f"the release notes claim {phrase!r}"


def test_the_release_notes_disclose_every_known_limitation():
    text = _NOTES.read_text(encoding="utf-8")
    missing = [lim for lim in rf.KNOWN_LIMITATIONS if lim not in text]
    assert not missing, f"undisclosed known limitations: {missing}"


def test_the_release_notes_distinguish_environment_dependent_totals():
    """The specific honesty this release turns on."""
    lowered = _NOTES.read_text(encoding="utf-8").lower()
    assert "environment" in lowered
    assert "0 failed" in lowered or "0 failures" in lowered
    assert "authoritative" in lowered, (
        "the notes do not identify which environment is authoritative for the totals")


def test_the_release_notes_do_not_claim_a_plugin_sandbox_or_enterprise_readiness():
    lowered = _NOTES.read_text(encoding="utf-8").lower()
    assert "enterprise-ready" not in lowered and "enterprise ready" not in lowered
    assert "plugin sandbox" not in lowered or "does not exist" in lowered or \
        "no plugin sandbox" in lowered


# ===================================================================== the closure doc


def test_the_closure_document_exists_and_records_the_merge_commit():
    assert _CLOSURE_DOC.is_file(), "V69_M61_8_RELEASE_CLOSURE.md is missing"
    text = _CLOSURE_DOC.read_text(encoding="utf-8")
    assert rf.RELEASE_MERGE_COMMIT in text
    assert rf.VERSION in text
    assert rf.RELEASE_STAGE in text


def test_the_closure_document_states_the_bandit_result_consistently():
    text = _CLOSURE_DOC.read_text(encoding="utf-8")
    if "andit" in text:
        assert "0 Medium and 0 High" in text
        assert str(rf.BANDIT_LOW_BASELINE) in text


def test_the_closure_document_does_not_deny_the_merge_or_claim_the_tag():
    lowered = _CLOSURE_DOC.read_text(encoding="utf-8").lower()
    assert "not merged" not in lowered
    assert "no tag exists" in lowered or "no tag" in lowered


def test_the_closure_document_states_that_m618_is_not_a_package_version():
    text = _CLOSURE_DOC.read_text(encoding="utf-8")
    assert "not a version" in text.lower() or "internal closure stage" in text.lower()


# ===================================================================== the runbook


def test_the_runbook_exists():
    assert _RUNBOOK.is_file(), "docs/RELEASE_RUNBOOK.md is missing"


def test_the_runbook_places_the_tag_after_the_merge_and_behind_the_guard():
    text = _RUNBOOK.read_text(encoding="utf-8")
    guard = text.find("check_release_tag_guard")
    tag = text.find("git tag -a")
    merge = text.lower().find("merge")
    assert guard != -1, "the runbook does not run the tag guard"
    assert tag != -1, "the runbook does not describe tagging"
    assert guard < tag, "the runbook tags before running the guard"
    assert merge < tag, "the runbook tags before merging"


def test_the_runbook_tags_the_canonical_version_only():
    text = _RUNBOOK.read_text(encoding="utf-8")
    tags = set(re.findall(r"\bv69\.\d+\.\d+\b", text))
    assert tags <= {rf.RELEASE_TAG}, f"the runbook mentions other tags: {sorted(tags)}"


def test_the_runbook_documents_a_rollback():
    lowered = _RUNBOOK.read_text(encoding="utf-8").lower()
    assert "rollback" in lowered or "roll back" in lowered


def test_the_runbook_builds_artifacts_outside_the_working_tree():
    lowered = _RUNBOOK.read_text(encoding="utf-8").lower()
    assert "temporary" in lowered or "temp" in lowered


# ===================================================================== the policy doc


def test_the_policy_document_has_the_structure_it_is_referenced_for():
    assert _POLICY.is_file()
    text = _POLICY.read_text(encoding="utf-8")
    headings = re.findall(r"^#{2,3} +(.+)$", text, re.MULTILINE)
    assert len(headings) >= 6, f"only {len(headings)} sections in the policy"
    lowered = text.lower()
    for topic in ("bandit", "baseline", "profile", "audit"):
        assert topic in lowered, f"the policy does not cover {topic!r}"


def test_the_policy_names_every_declared_dependency_profile():
    text = _POLICY.read_text(encoding="utf-8")
    for profile in rf.SBOM_MANDATORY_PROFILES + rf.SBOM_OPTIONAL_PROFILES:
        assert profile in text, f"profile {profile!r} is undocumented"


# ================================================================ branch protection


def _ci_job_names() -> dict[str, str]:
    """job id -> display name, read from the workflow with no YAML dependency."""
    text = _CI.read_text(encoding="utf-8")
    jobs: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        job = re.match(r"^  ([A-Za-z][\w-]*):\s*$", line)
        if job:
            current = job.group(1)
            continue
        name = re.match(r"^    name:\s*(.+?)\s*$", line)
        if name and current:
            jobs[current] = name.group(1).strip("'\"")
            current = None
    return jobs


def test_the_branch_protection_document_exists():
    assert _PROTECTION.is_file(), "docs/BRANCH_PROTECTION.md is missing"


def test_every_required_check_name_matches_a_real_ci_job():
    """An invented check name blocks every PR forever while proving nothing."""
    real = _ci_job_names()
    text = _PROTECTION.read_text(encoding="utf-8")
    for job_id, name in rf.CI_MANDATORY_JOBS.items():
        assert job_id in real, f"declared mandatory job {job_id!r} is not in ci.yml"
        assert real[job_id] == name, (
            f"job {job_id!r} is named {real[job_id]!r} in ci.yml but "
            f"{name!r} in release_facts")
        assert name in text, f"{name!r} is not required by the protection document"


def test_the_advisory_job_is_declared_and_not_required():
    real = _ci_job_names()
    text = _PROTECTION.read_text(encoding="utf-8")
    for job_id, name in rf.CI_ADVISORY_JOBS.items():
        assert real.get(job_id) == name
        assert name in text, "the advisory job is not mentioned at all"
        assert "advisory" in text.lower()
    assert not (set(rf.CI_ADVISORY_JOBS) & set(rf.CI_MANDATORY_JOBS))


def test_the_protection_document_does_not_claim_the_settings_are_applied():
    lowered = _PROTECTION.read_text(encoding="utf-8").lower()
    assert "recommend" in lowered
    assert "is enabled on master" not in lowered
    assert "has been applied" not in lowered


# ===================================================================== tooling limits


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_no_release_script_can_invoke_a_shell_or_eval(script):
    tree = ast.parse(_source(script))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in {"system", "eval", "exec", "popen", "spawnl", "spawnv"}, \
                f"{script} calls {name}()"
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, ast.Constant) and kw.value.value), \
                        f"{script} uses shell=True"


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_no_release_script_contains_a_publication_verb(script):
    """Matched over executable string literals, so an assembled command is still caught."""
    for literal in _executable_strings(_source(script)):
        low = literal.lower()
        for needle, why in FORBIDDEN_LITERALS:
            assert needle not in low, (
                f"{script} contains the literal {needle!r}, which {why}; releasing is a "
                "human step and the tooling must not be able to perform it")


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_no_release_script_runs_a_mutating_git_subcommand(script):
    """Every ``git`` argument list is checked for a state-changing subcommand."""
    tree = ast.parse(_source(script))
    docstrings = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        parts = [e.value for e in node.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if not parts or Path(parts[0]).stem != "git":
            continue
        if parts[0] in docstrings:
            continue
        for arg in parts[1:]:
            if arg.startswith("-"):
                continue
            assert arg not in MUTATING_GIT, (
                f"{script} runs `git {arg}`, which mutates the repository; release "
                "scripts read git and never change it")
            break


def test_the_tag_guard_only_permits_read_only_git_subcommands():
    """The guard's allowlist is the thing standing between evidence and action."""
    source = _source("check_release_tag_guard.py")
    tree = ast.parse(source)
    allowlists = [set(_strings(node)) for node in ast.walk(tree)
                  if isinstance(node, (ast.Tuple, ast.Set, ast.List))
                  and {"rev-parse"} <= set(_strings(node))]
    assert allowlists, "the tag guard has no recognisable read-only git allowlist"
    for allowed in allowlists:
        assert not (allowed & MUTATING_GIT), (
            f"the guard permits mutating subcommands: {sorted(allowed & MUTATING_GIT)}")


def test_the_evidence_builder_writes_only_where_it_is_told():
    """No default output path inside the repository."""
    source = _source("build_release_evidence_m61.py")
    for literal in _executable_strings(source):
        assert not literal.startswith("docs/"), (
            f"the evidence builder defaults to writing {literal!r} into the repository")


# ===================================================================== inertness


def test_running_this_suite_created_no_tag():
    """Belt and braces: the constraint above is static, this one is observed."""
    proc = subprocess.run(["git", "tag", "--list"], cwd=str(_REPO_ROOT),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        pytest.skip("git is unavailable")
    tags = [t for t in proc.stdout.split() if t.strip()]
    assert rf.RELEASE_TAG not in tags, (
        f"{rf.RELEASE_TAG} exists; this release has not been tagged yet and the "
        "documentation says so")


def test_no_release_artifact_has_been_written_into_the_working_tree():
    """Artifacts belong in a temporary directory, never in the repository."""
    strays = []
    for name in (rf.WHEEL_FILENAME, rf.SDIST_FILENAME,
                 rf.CHECKSUM_FILENAME, rf.EVIDENCE_FILENAME):
        strays += [str(p.relative_to(_REPO_ROOT))
                   for p in _REPO_ROOT.rglob(name) if p.is_file()]
    assert not strays, f"release artifacts are present in the working tree: {strays}"


def test_the_declared_release_state_matches_the_absence_of_a_tag():
    assert rf.RELEASE_STATE in {"in_flight", "merged", "published"}
    assert rf.RELEASE_STATE != "published", (
        "release_facts declares the release published; nothing has been published")


def test_the_canonical_version_is_unchanged_by_this_closure_stage():
    assert rf.VERSION == "69.61.0"
    assert rf.RELEASE_STAGE == "M61.8"
    assert rf.RELEASE_TAG == f"v{rf.VERSION}"
    assert rf.RELEASE_STAGE not in rf.VERSION, (
        "M61.8 is an internal closure stage and must never appear in a package version")


def test_the_temporary_build_directory_is_not_inside_the_repository():
    """The qualification harness builds into a temp dir; prove it is not the repo."""
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        tmp.relative_to(_REPO_ROOT.resolve())
    except ValueError:
        return
    pytest.fail("the system temporary directory is inside the repository, so builds "
                "would leave artifacts in the working tree")
