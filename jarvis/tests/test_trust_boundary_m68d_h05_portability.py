"""V69 M68D — H05: control-plane portability without weakening integrity.

The invariant: the repository identity VERIFIED must be the repository's
semantics ON THAT PLATFORM.

An external Windows run on master reported 147 control-plane problems on an
ordinary checkout (`core.autocrlf=true`, the Windows default), 16 with it off,
and ten failures in the M68C source-integrity suite. Reproduced locally with two
temporary clones of ONE commit: **0 problems with `autocrlf=false` and 131 with
it true**. The repository was not corrupt; three assumptions were POSIX-only.

    A. `str(Path.relative_to(...))` renders `state\\m62\\…` on Windows while
       `git ls-files` prints `state/m62/…`, so every tracked control-plane file
       reported as untracked.
    B. `os.access(path, os.X_OK)` answers a FILESYSTEM question, not a Git one.
       On Windows it is true for any readable file. It is also wrong on POSIX in
       BOTH directions — measured here — which makes it a bypassable control
       rather than merely an unportable one.
    C. Checkout newline conversion rewrites the bytes a byte seal covers.

None of this is fixed by normalising before comparison or by recomputing a
sealed digest. A byte seal stays a byte seal; what changed is that the bytes are
PINNED and the pin is itself verified.

These tests run on Linux. They are development evidence; the Windows job in
`.github/workflows/ci.yml` is what witnesses the real platform.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / "jarvis" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import verify_m62_control_plane as cpv  # noqa: E402

_SRC = Path(cpv.__file__).read_text(encoding="utf-8")
_LINES = _SRC.splitlines()


def _code_lines(src: str) -> list[str]:
    out = [""] * (len(src.splitlines()) + 2)
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING):
            prev = tok.type
            continue
        if tok.string.strip():
            out[tok.start[0]] += " " + tok.string
        prev = tok.type
    return out


_CODE_LINES = _code_lines(_SRC)
_CODE = " ".join(_CODE_LINES)


def code_region(header: str) -> str:
    starts = [i for i, line in enumerate(_LINES) if header in line]
    assert starts, f"region header not found: {header!r}"
    start = starts[0]
    indent = len(_LINES[start]) - len(_LINES[start].lstrip())
    end = len(_LINES)
    for i in range(start + 1, len(_LINES)):
        line = _LINES[i]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and line.lstrip().startswith(("def ", "class ", "@", "#")):
            end = i
            break
    return " ".join(_CODE_LINES[start + 1:end + 1])


def _git(*args, cwd=None):
    done = subprocess.run(["git", *args], cwd=str(cwd or _REPO_ROOT),
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


@pytest.fixture
def tiny_repo(tmp_path):
    """A disposable Git repository. Nothing here touches the real worktree."""
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "t@example.test", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    return tmp_path


def _index_mode(repo: Path, name: str) -> "str | None":
    code, out = _git("ls-files", "--stage", "--", name, cwd=repo)
    if code != 0 or not out.strip():
        return None
    return out.split()[0]


# ── A. repository paths ──────────────────────────────────────────────────────

class TestRepositoryPathRendering:
    @pytest.mark.parametrize("rel", [
        "state/m62/current.json",
        "state/m62/snapshots/0067-m67a1-corpus-qualification.json",
        "jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md",
        "PROGRESS.md",
        "a/b/c/d/e/f.json",
    ])
    def test_a_repository_path_is_always_posix(self, rel):
        assert cpv.repo_path(rel) == rel
        assert "\\" not in cpv.repo_path(rel)

    def test_a_windows_rendered_path_is_normalised_back(self):
        """THE measured failure, as a regression.

        `str(PureWindowsPath("state/m62/x.json"))` is what
        `str(Path.relative_to(...))` produces on Windows. Compared against
        `git ls-files` output it is simply absent from the set.
        """
        rel = "state/m62/snapshots/0067-m67a1-corpus-qualification.json"
        windows_rendered = str(PureWindowsPath(rel))
        assert "\\" in windows_rendered, "non-vacuity: the fixture is not Windows-shaped"
        code, out = _git("ls-files", "-z", "--", rel)
        tracked = {entry for entry in out.split("\0") if entry.strip()}
        assert rel in tracked, "non-vacuity: the fixture path is not tracked"
        assert windows_rendered not in tracked, \
            "non-vacuity: the native rendering is not actually different"
        # And the normaliser closes the gap.
        assert cpv.repo_path(PureWindowsPath(rel).as_posix()) in tracked

    def test_an_absolute_path_inside_the_repo_becomes_relative(self):
        absolute = _REPO_ROOT / "state" / "m62" / "current.json"
        assert cpv.repo_path(absolute) == "state/m62/current.json"

    def test_an_absolute_path_outside_the_repo_is_not_invented(self):
        out = cpv.repo_path(Path("/tmp/not-in-the-repo/x.json"))
        assert out.startswith("/tmp/"), \
            "a path outside the repo must not be given a fake repository name"

    def test_a_posix_path_object_round_trips(self):
        assert cpv.repo_path(PurePosixPath("state/m62/current.json")) == \
            "state/m62/current.json"

    def test_the_tracked_set_comparison_uses_repository_paths(self):
        """Absent-control with a non-vacuity witness."""
        body = code_region("def check_paths(cp: ControlPlane, report: Report)")
        assert "repo_path ( cp . snapshot_path )" in body, \
            "the snapshot path is not normalised before the tracked-set test"
        assert "str ( cp . snapshot_path . relative_to ( REPO_ROOT ) )" not in body, \
            "the native rendering is back"
        # Non-vacuity: the forbidden shape is recognisable to the detector.
        sample = " ".join(_code_lines(
            "x = str(cp.snapshot_path.relative_to(REPO_ROOT))\n"))
        assert "str ( cp . snapshot_path . relative_to ( REPO_ROOT ) )" in sample

    def test_no_control_plane_check_compares_a_native_rendering(self):
        assert "str ( cp . snapshot_path . relative_to ( REPO_ROOT ) )" not in _CODE

    def test_repo_path_is_implemented_with_as_posix(self):
        """A STRUCTURAL control, because the behaviour is platform-conditional.

        On POSIX `str(PurePosixPath(x))` and `x.as_posix()` are the same string,
        so replacing one with the other changes nothing a Linux test can see
        (measured: that mutation survived the first campaign run). The separator
        rendering is decided by the method name, so the method name is what gets
        asserted — and the Windows CI job is what witnesses the behaviour.
        """
        body = code_region("def repo_path(path: \"Path | str\") -> str:")
        assert body.count("as_posix ( )") == 2, \
            "repo_path must render BOTH its branches with as_posix()"
        assert "return str ( candidate )" not in body, \
            "repo_path renders a native path string"


# ── B. the executable bit ────────────────────────────────────────────────────

class TestGitModeIsTheAuthority:
    def test_index_mode_is_read_for_a_normal_file(self):
        modes = cpv._git_index_modes("state/m62/current.json")
        assert modes.get("state/m62/current.json") == "100644"

    def test_index_mode_is_read_for_several_files_at_once(self):
        modes = cpv._git_index_modes("PROGRESS.md", "state/m62/current.json")
        assert set(modes) == {"PROGRESS.md", "state/m62/current.json"}

    def test_an_untracked_path_is_absent_rather_than_assumed_non_executable(self,
                                                                           tmp_path):
        stray = tmp_path / "stray.json"
        stray.write_text("{}\n")
        modes = cpv._git_index_modes(cpv.repo_path(stray))
        assert cpv.repo_path(stray) not in modes, \
            "silence must not be reported as mode 100644"

    def test_an_executable_mode_is_recognised(self, tiny_repo):
        target = tiny_repo / "script.sh"
        target.write_text("#!/bin/sh\n")
        _git("add", "script.sh", cwd=tiny_repo)
        _git("update-index", "--chmod=+x", "script.sh", cwd=tiny_repo)
        assert _index_mode(tiny_repo, "script.sh") == cpv.GIT_MODE_EXECUTABLE

    def test_os_access_disagrees_with_git_when_the_worktree_bit_is_set(self,
                                                                      tiny_repo):
        """FALSE POSITIVE, measured.

        Git says 100644, the filesystem says executable, and the old check
        failed a legitimate data file. On Windows this is the normal case for
        every readable file, which is where the 131-problem flood came from.
        """
        target = tiny_repo / "data.json"
        target.write_text("{}\n")
        _git("add", "data.json", cwd=tiny_repo)
        _git("commit", "-qm", "add", cwd=tiny_repo)
        target.chmod(0o755)
        if not os.access(target, os.X_OK):           # pragma: no cover
            pytest.skip("this filesystem does not honour the executable bit")
        assert _index_mode(tiny_repo, "data.json") == "100644"
        assert os.access(target, os.X_OK) is True
        assert _index_mode(tiny_repo, "data.json") != cpv.GIT_MODE_EXECUTABLE

    def test_os_access_disagrees_with_git_when_the_worktree_bit_is_cleared(self,
                                                                          tiny_repo):
        """FALSE NEGATIVE, measured — and this one is a BYPASS, not a nuisance.

        A file committed 100755 with its working-tree bit cleared passes an
        `os.access` check while Git calls it executable. The invariant the
        control plane states is about the repository, so reading the filesystem
        made the control avoidable by anyone who could commit a mode.
        """
        target = tiny_repo / "data.json"
        target.write_text("{}\n")
        _git("add", "data.json", cwd=tiny_repo)
        _git("commit", "-qm", "add", cwd=tiny_repo)
        _git("update-index", "--chmod=+x", "data.json", cwd=tiny_repo)
        target.chmod(0o644)
        assert _index_mode(tiny_repo, "data.json") == cpv.GIT_MODE_EXECUTABLE
        assert os.access(target, os.X_OK) is False, \
            "non-vacuity: the working-tree bit was not actually cleared"

    def test_an_ordinary_json_is_not_executable_because_os_access_behaves_oddly(
            self, tiny_repo, monkeypatch):
        """The Windows behaviour, simulated: `os.access` true for everything."""
        monkeypatch.setattr(os, "access", lambda *a, **kw: True)
        target = tiny_repo / "data.json"
        target.write_text("{}\n")
        _git("add", "data.json", cwd=tiny_repo)
        assert os.access(target, os.X_OK) is True
        assert _index_mode(tiny_repo, "data.json") == "100644", \
            "the Git answer must not depend on os.access at all"

    def test_no_repository_executable_invariant_uses_os_access(self):
        """Absent-control. Non-vacuity: the detector is shown the real token."""
        token = "os . access"
        sample = " ".join(_code_lines("if os.access(p, os.X_OK):\n    pass\n"))
        assert token in sample, "detector is vacuous"
        assert "X_OK" not in _CODE, \
            "the control plane still reads an executable bit from the filesystem"
        assert token not in _CODE

    def test_the_executable_check_fails_closed_when_git_cannot_answer(self):
        body = code_region("def check_paths(cp: ControlPlane, report: Report)")
        assert "_git_index_modes ( * mode_targets )" in body
        assert "if not modes :" in body, \
            "a failed ls-files --stage silently skips the invariant"

    def test_the_record_store_checks_modes_from_the_index_too(self):
        body = code_region("def check_record_store(cp: ControlPlane, report: Report)")
        assert "_git_index_modes ( * rel_paths )" in body
        assert "GIT_MODE_EXECUTABLE" in body


# ── C. newline pinning ───────────────────────────────────────────────────────

class TestNewlinePinning:
    def test_the_gitattributes_file_exists_and_is_tracked(self):
        attributes = _REPO_ROOT / ".gitattributes"
        assert attributes.is_file()
        code, out = _git("ls-files", "--error-unmatch", "--", ".gitattributes")
        assert code == 0 and out.strip(), ".gitattributes is untracked"

    @pytest.mark.parametrize("rel", [
        "PROGRESS.md",
        "state/m62/current.json",
        "state/m62/scientific-suite.json",
        "jarvis/docs/m62/HISTORY_INDEX.md",
        "jarvis/docs/m62/history/PROGRESS_THROUGH_S3N.md",
        "jarvis/scripts/verify_m62_control_plane.py",
    ])
    def test_each_named_byte_sealed_artifact_is_newline_pinned(self, rel):
        answers = cpv._newline_pinned(rel)
        assert answers.get(rel) == "unset", \
            f"{rel} is byte-sealed but Git may convert its newlines"

    def test_every_tracked_byte_sealed_path_is_pinned(self):
        code, out = _git("ls-files", "-z", "--",
                         *cpv.BYTE_PINNED_TREES, *cpv.BYTE_PINNED_FILES)
        assert code == 0
        paths = sorted(entry for entry in out.split("\0") if entry.strip())
        assert len(paths) > 50, \
            f"non-vacuity: only {len(paths)} byte-sealed paths found"
        answers = cpv._newline_pinned(*paths)
        unpinned = [rel for rel in paths if answers.get(rel) != "unset"]
        assert unpinned == [], f"{len(unpinned)} unpinned: {unpinned[:5]}"

    def test_the_pin_is_unset_not_merely_lf(self):
        """`eol=lf` would still CONVERT. Only `-text` means "do not touch"."""
        attributes = (_REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        body = "\n".join(line for line in attributes.splitlines()
                         if line.strip() and not line.strip().startswith("#"))
        assert body.strip(), "non-vacuity: .gitattributes declares nothing"
        assert "-text" in body
        assert "text=auto" not in body, "text=auto re-enables conversion"
        assert "eol=" not in body, "eol= converts; the seal needs no conversion"

    def test_an_unpinned_byte_sealed_tree_is_a_control_plane_failure(self,
                                                                    monkeypatch):
        """The check must actually fail when the pin lapses.

        Simulated by asking about a tracked path the policy does NOT cover, which
        is exactly what adding a sealed artifact outside the pinned trees would
        produce.
        """
        answers = cpv._newline_pinned("jarvis/core/source_integrity.py")
        assert answers.get("jarvis/core/source_integrity.py") != "unset", \
            "non-vacuity: an unpinned path must be distinguishable"

    def test_the_newline_policy_check_is_dispatched_and_owns_its_category(self):
        assert "NEWLINE_POLICY" in cpv.CATEGORIES
        owners = [entry for entry in cpv.CHECK_DISPATCH
                  if "NEWLINE_POLICY" in entry[1]]
        assert len(owners) == 1, "NEWLINE_POLICY has no single owning check"
        assert owners[0][0] == "check_newline_policy"
        assert hasattr(cpv, "check_newline_policy")

    def test_the_newline_policy_check_refuses_an_empty_artifact_set(self):
        """Non-vacuity, enforced inside the checker itself."""
        body = code_region("def check_newline_policy(cp: ControlPlane, report: Report)")
        assert "if not paths :" in body
        assert "EMPTY" in _SRC.split("def check_newline_policy", 1)[1][:4000], \
            "the checker does not refuse a vacuous pass"

    def test_the_newline_policy_requires_the_policy_file_to_be_tracked(self):
        body = code_region("def check_newline_policy(cp: ControlPlane, report: Report)")
        assert "GITATTRIBUTES_PATH" in body
        assert "ls-files" in body

    def test_the_byte_pinned_set_is_declared_as_trees_not_a_file_list(self):
        """So a new snapshot or record is covered the moment it is added."""
        assert cpv.BYTE_PINNED_TREES == ("state/m62",)
        assert "PROGRESS.md" in cpv.BYTE_PINNED_FILES
        assert "jarvis/docs/m62" in cpv.BYTE_PINNED_FILES
        assert "jarvis/scripts/verify_m62_control_plane.py" in cpv.BYTE_PINNED_FILES

    def test_no_sealed_digest_is_recomputed_from_converted_bytes(self):
        """The forbidden 'fix': normalise, then compare.

        Non-vacuity: the detector is shown each forbidden shape first.
        """
        forbidden = ("replace ( b'\\r\\n' , b'\\n' )", "splitlines ( )",
                     "universal_newlines", "decode ( ) . replace")
        samples = {
            "replace ( b'\\r\\n' , b'\\n' )":
                " ".join(_code_lines("raw = raw.replace(b'\\r\\n', b'\\n')\n")),
        }
        for token, sample in samples.items():
            assert token in sample, f"detector for {token!r} is vacuous"
        digest_region = code_region("def sha256_file(path: Path) -> str:")
        for token in forbidden:
            assert token not in digest_region, \
                f"the digest normalises bytes before hashing ({token})"


# ── D. platform capabilities ─────────────────────────────────────────────────

class TestPlatformCapabilities:
    def test_the_source_integrity_suite_declares_its_capabilities(self):
        # Loaded BY PATH. A dotted import of `tests.…` resolves against whichever
        # `tests` package wins on sys.path, which is not necessarily this one.
        path = _REPO_ROOT / "jarvis" / "tests" / "test_source_integrity_m68c.py"
        spec = importlib.util.spec_from_file_location("_m68c_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.POSIX_MODES_ENFORCED in (True, False)
        assert module.FLOCK_AVAILABLE in (True, False)
        assert isinstance(module.OUTSIDE_SANDBOX, str) and module.OUTSIDE_SANDBOX

    def test_no_test_in_the_source_integrity_suite_assumes_os_geteuid(self):
        src = (_REPO_ROOT / "jarvis" / "tests"
               / "test_source_integrity_m68c.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        # `hasattr(os, "geteuid")` is the ONE permitted mention: it is the guard.
        bare = [line for line in code.splitlines()
                if "geteuid" in line and "hasattr" not in line]
        assert bare == [], f"unguarded os.geteuid: {bare}"

    def test_no_test_in_the_source_integrity_suite_assumes_flock(self):
        src = (_REPO_ROOT / "jarvis" / "tests"
               / "test_source_integrity_m68c.py").read_text(encoding="utf-8")
        assert "FLOCK_AVAILABLE" in src
        # The single-winner guarantee is asserted only where the lock exists.
        region = src.split("def test_two_cas_writers_from_one_read", 1)[0]
        assert "skipif(\n        not FLOCK_AVAILABLE" in region, \
            "the serialisation guarantee is asserted on platforms without flock"

    def test_the_serialised_false_contract_holds_without_fcntl(self, tmp_path,
                                                               monkeypatch):
        """M68C's honest degradation, witnessed HERE rather than only on Windows.

        `_serialised_on` has no lock without `fcntl`, and the receipt must say so
        instead of implying writers were serialised. Simulated by poisoning the
        module cache, which makes `import fcntl` raise exactly as it does on
        Windows — so the contract is covered on every platform this suite runs on.
        """
        import core.source_integrity as si

        target = tmp_path / "nolock.py"
        target.write_text("before\n")
        monkeypatch.setitem(sys.modules, "fcntl", None)
        with si._serialised_on(tmp_path) as serialised:
            assert serialised is False, \
                "the lock claimed to be held with no fcntl available"
        receipt = si.cas_write_text(target, "after\n")
        assert receipt.serialised is False, \
            "the receipt implies serialisation that did not happen"
        assert receipt.status is si.WriteStatus.APPLIED
        assert target.read_text() == "after\n", \
            "honest degradation must still perform the write"

    def test_the_serialised_true_contract_holds_with_fcntl(self, tmp_path):
        import core.source_integrity as si

        if importlib.util.find_spec("fcntl") is None:    # pragma: no cover
            pytest.skip("no fcntl on this platform")
        with si._serialised_on(tmp_path) as serialised:
            assert serialised is True

    def test_a_missing_lock_directory_degrades_rather_than_raising(self, tmp_path):
        import core.source_integrity as si

        with si._serialised_on(tmp_path / "does-not-exist") as serialised:
            assert serialised is False

    def test_fixture_newline_expectations_are_platform_neutral(self, tmp_path):
        """A fixture written with `write_text` and read with `read_text` agrees
        on every platform; one written in binary and read as text does not."""
        target = tmp_path / "n.txt"
        target.write_text("a\nb\n")
        assert target.read_text() == "a\nb\n"


# ── the control plane is not weakened ────────────────────────────────────────

class TestIntegrityIsNotWeakened:
    def test_the_canonical_serialization_is_still_byte_exact(self):
        body = code_region("def check_record_store(cp: ControlPlane, report: Report)")
        assert "raw != canonical_bytes ( payload )" in body, \
            "the record store no longer requires the canonical serialization"

    def test_the_snapshot_digest_is_still_over_raw_bytes(self):
        assert "def sha256_file" in _SRC
        region = code_region("def sha256_file(path: Path) -> str:")
        assert "path . read_bytes ( )" in region
        assert "read_text" not in region, \
            "the digest reads text, so it would depend on newline translation"

    def test_the_digest_does_not_normalise_crlf_before_hashing(self, tmp_path):
        """BEHAVIOURAL, because a structural check missed it.

        "normalise the bytes until the hashes match" is the forbidden fix (§34),
        and a `.replace(b"\r\n", b"\n")` inside the digest keeps `read_bytes()`
        in the source — so the structural assertion above passed while the seal
        had stopped being a byte seal (measured, by the mutation campaign).
        """
        import hashlib

        target = tmp_path / "crlf.json"
        raw = b'{\r\n  "a": 1\r\n}\r\n'
        target.write_bytes(raw)
        assert b"\r\n" in target.read_bytes(), "non-vacuity: the fixture has no CRLF"
        assert cpv.sha256_file(target) == hashlib.sha256(raw).hexdigest()
        # And it must differ from the LF rendering, or no conversion is detectable.
        lf = raw.replace(b"\r\n", b"\n")
        assert cpv.sha256_file(target) != hashlib.sha256(lf).hexdigest(), \
            "the digest normalises newlines, so a converted checkout hashes equal"

    def test_the_verifier_still_pins_its_own_digest(self):
        assert "VERIFIER_INTEGRITY" in cpv.CATEGORIES
        owners = [e for e in cpv.CHECK_DISPATCH if "VERIFIER_INTEGRITY" in e[1]]
        assert len(owners) == 1

    def test_every_category_still_has_exactly_one_owning_check(self):
        owned: dict[str, int] = {}
        for name, categories in cpv.CHECK_DISPATCH:
            for category in categories:
                owned[category] = owned.get(category, 0) + 1
        for category in cpv.CATEGORIES:
            assert owned.get(category) == 1, \
                f"{category} is owned by {owned.get(category, 0)} checks"

    def test_no_whitespace_or_unicode_canonicalisation_was_introduced(self):
        for token in ("casefold ( )", "unicodedata . normalize", "strip ( ) =="):
            assert token not in _CODE, f"{token} canonicalises before comparison"


# ── the Windows runner ───────────────────────────────────────────────────────

class TestWindowsCiJob:
    @pytest.fixture(scope="class")
    def workflow(self):
        yaml = pytest.importorskip("yaml")
        return yaml.safe_load(
            (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"))

    def test_a_windows_job_exists(self, workflow):
        assert "windows-portability" in workflow["jobs"]

    def test_it_runs_on_a_windows_runner(self, workflow):
        assert workflow["jobs"]["windows-portability"]["runs-on"] == "windows-latest"

    def test_it_uses_the_authoritative_python_version(self, workflow):
        steps = workflow["jobs"]["windows-portability"]["steps"]
        versions = [str((s.get("with") or {}).get("python-version"))
                    for s in steps if (s.get("with") or {}).get("python-version")]
        assert versions == ["3.11"]

    def test_it_uses_a_windows_appropriate_shell(self, workflow):
        job = workflow["jobs"]["windows-portability"]
        assert job.get("defaults", {}).get("run", {}).get("shell") == "pwsh"

    def test_it_runs_the_control_plane_verifier(self, workflow):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert "verify_m62_control_plane.py" in joined

    @pytest.mark.parametrize("suite", [
        "test_source_integrity_m68c.py",
        "test_trust_boundary_m68d_h05_portability.py",
        "test_trust_boundary_m68d_h02_source_identity.py",
    ])
    def test_it_runs_the_portability_relevant_suites(self, workflow, suite):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert suite in joined

    def test_it_runs_the_dependency_authority_checks(self, workflow):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert "check_release_consistency.py" in joined
        assert "check_package_manifest.py" in joined

    def test_it_runs_an_import_smoke(self, workflow):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert "import ok" in joined

    def test_it_checks_out_full_history_for_the_verifier(self, workflow):
        steps = workflow["jobs"]["windows-portability"]["steps"]
        checkout = [s for s in steps
                    if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert checkout, "no checkout step"
        assert str((checkout[0].get("with") or {}).get("fetch-depth")) == "0"

    def test_it_does_not_disable_autocrlf_to_make_itself_pass(self, workflow):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert "core.autocrlf false" not in joined
        assert "core.autocrlf=false" not in joined
        assert "autocrlf" in joined, \
            "the job should at least REPORT the newline configuration it ran under"

    def test_it_is_not_marked_advisory(self, workflow):
        job = workflow["jobs"]["windows-portability"]
        assert "continue-on-error" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
        joined = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "|| true" not in joined
        assert "continue" not in joined.lower() or "|| true" not in joined

    def test_it_requires_no_live_service_or_privilege(self, workflow):
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        for forbidden in ("ollama", "docker", "sudo ", "runas",
                          "JARVIS_TRUSTED_LAB"):
            assert forbidden not in joined.lower()

    def test_it_does_not_convert_the_whole_suite_into_a_windows_gate(self, workflow):
        """Focused by design (§36): no repository evidence justifies a second
        full 12k run, and the skip baselines are Linux-measured."""
        joined = "\n".join(str(s.get("run", ""))
                           for s in workflow["jobs"]["windows-portability"]["steps"])
        assert "jarvis/tests tests" not in joined
