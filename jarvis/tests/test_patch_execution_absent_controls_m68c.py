"""V69 M68C — structural tests for ABSENT patch/mutation-execution controls.

WHY THESE ARE NOT MUTATION TESTS
--------------------------------
M68A established the rule, M68B extended it, and M68C's findings were all of
exactly the shape it describes. A mutation campaign can only break a line that
EXISTS: flip a comparison and a test fails, which proves that comparison is
load-bearing. It proves nothing about the precondition parameter that was never
in the signature, the truncation flag that was never in the result, or the
post-state check that nobody wrote. Every M68C finding was an ABSENT control:

    read_file   had no `truncated`, no digest, no path
    write_file  had no `expected_sha256`, no temp file, no post-state check
    git_query   had no truncation signal of any kind

So each test here asks two questions:

    "could the required control be entirely missing?"
    "is it enforced at the REAL composition/call site?"

The second is not decoration. `core.source_integrity.cas_write_text` can be
perfect while `_tool_write_file` still calls `open(p, "w")`; a `VALIDATION_SCOPE`
constant can say SYNTAX_ONLY while a caller treats the grader as authority. A
control that exists but is not wired is indistinguishable from an absent one.

NON-VACUITY (§10)
-----------------
A structural test that passes because it found zero relevant call sites is a
FAILED TEST DESIGN. That bites hardest here, because one of M68C's findings is
that this build has NO patch-application path at all — so a probe asserting
"every patch-application path has an applicability gate" would pass over an
empty set, forever, learning nothing.

Every detector below is therefore run TWICE: once against a SYNTHETIC fixture
that deliberately contains the violation, where it must FIRE, and once against
the real tree, where it must be silent. A broken detector fails the first half.
These are written against the AST, never against raw text, so a comment or a
docstring describing the old defect cannot satisfy them.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PACKAGE_ROOT))

EXECUTOR = PACKAGE_ROOT / "tools" / "executor.py"
SOURCE_INTEGRITY = PACKAGE_ROOT / "core" / "source_integrity.py"
GRADER = PACKAGE_ROOT / "training_gym" / "graders" / "diff_budget_grader.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _parse(src: str) -> ast.Module:
    return ast.parse(src, filename="<synthetic>")


def _functions(tree: ast.Module) -> dict:
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _named(tree: ast.Module, name: str):
    found = _functions(tree).get(name)
    if found is None:
        raise AssertionError(
            f"{name}() not found — the control it carries may be absent")
    return found


def _calls(node) -> set[str]:
    """Every callable NAME invoked inside *node*, bare or attribute."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _returned_dict_keys(node) -> set[str]:
    """Constant string keys of every dict RETURNED from *node*."""
    keys: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
            for key in child.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


# ══ DETECTOR 1 · a raw write primitive on a caller-supplied path ═══════════

#: Names that put bytes on disk without any precondition. `open` is the one that
#: actually bit (`_tool_write_file` was `open(p, "w")`); the pathlib spellings
#: are the obvious ways to reintroduce it without typing `open`.
_RAW_WRITE_CALLS = frozenset({"write_text", "write_bytes"})

#: `take_screenshot` has FileIntent.DESTINATION but writes an IMAGE it generated
#: itself through a capture library, not caller-supplied content. It has no
#: version a caller could have read, so CAS does not apply to it; it is covered
#: by the containment gate instead. Named so the exemption is explicit.
_SCREENSHOT_SINKS = frozenset({"save", "screenshot", "grab"})


def _raw_write_sites(tree: ast.Module) -> set[str]:
    """Handlers that call a raw write primitive. The detector under test."""
    offenders: set[str] = set()
    for name, node in _functions(tree).items():
        if not name.startswith("_tool_"):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr in _RAW_WRITE_CALLS:
                offenders.add(name)
            elif isinstance(func, ast.Name) and func.id == "open":
                # FAIL CLOSED on a mode this scanner cannot read. A COMPUTED
                # mode — `open(p, mode, ...)` — cannot be proven read-only, and
                # that is not hypothetical: the campaign mutation
                # `B_handler_bypasses_the_primitive` reintroduced the raw write
                # as exactly `open(p, mode, encoding="utf-8")` and SURVIVED,
                # because the old scanner read the non-literal mode as "" and
                # matched nothing. A detector that only catches the literal
                # spelling of a defect catches the defect it was written from.
                mode_node = None
                if len(child.args) > 1:
                    mode_node = child.args[1]
                for kw in child.keywords:
                    if kw.arg == "mode":
                        mode_node = kw.value
                if mode_node is None:
                    continue                      # open(p) — read, unambiguous
                if isinstance(mode_node, ast.Constant):
                    if any(flag in str(mode_node.value)
                           for flag in ("w", "a", "x", "+")):
                        offenders.add(name)
                else:
                    offenders.add(name)           # unreadable mode: suspicious
    return offenders


class TestNoMutationPathBypassesCAS:
    """No whole-file mutation path may bypass the expected-version check."""

    def test_the_detector_fires_on_an_injected_raw_write(self):
        """NON-VACUITY WITNESS. A detector that finds nothing proves nothing."""
        synthetic = _parse(
            "class E:\n"
            "    def _tool_sneaky_write(self, path, content):\n"
            "        with open(path, 'w') as fh:\n"
            "            fh.write(content)\n"
            "    def _tool_sneaky_pathlib(self, path, content):\n"
            "        Path(path).write_text(content)\n"
            "    def _tool_computed_mode(self, path, content, mode):\n"
            "        with open(path, mode, encoding='utf-8') as fh:\n"
            "            fh.write(content)\n"
            "    def _tool_innocent_read(self, path):\n"
            "        return open(path).read()\n")
        assert _raw_write_sites(synthetic) == {
            "_tool_sneaky_write", "_tool_sneaky_pathlib", "_tool_computed_mode"}

    def test_write_file_routes_through_the_CAS_primitive(self):
        """Enforced at the REAL call site, not only inside an unused helper."""
        handler = _named(_tree(EXECUTOR), "_tool_write_file")
        assert "cas_write_text" in _calls(handler), (
            "_tool_write_file no longer routes through cas_write_text — the "
            "precondition, the atomic replace and the post-state check are all "
            "inside it, so bypassing it removes all three at once")

    def test_write_file_contains_no_raw_write_primitive(self):
        handler = _named(_tree(EXECUTOR), "_tool_write_file")
        assert "_tool_write_file" not in _raw_write_sites(
            ast.Module(body=[handler], type_ignores=[])), (
            "_tool_write_file writes bytes directly again; that is the measured "
            "lost-update defect restored")

    def test_no_tool_handler_writes_a_caller_path_without_the_primitive(self):
        """The alternate-call-site sweep: EVERY handler, not just write_file.

        Three handlers legitimately put bytes on disk with no precondition, and
        each is allowed for ONE reason: the path is derived by JARVIS, never
        supplied by the caller, so there is no version a caller could have read
        and no update to lose. The allowlist records that reason as the
        `FILE_CAPABLE_TOOLS` entry it must NOT have — a handler that starts
        taking a path argument stops qualifying automatically, rather than
        staying excused because its name is written here.
        """
        from tools.executor import FILE_CAPABLE_TOOLS
        allowed = {
            # mkdtemp() + a fixed "index.html" inside it
            "_tool_desplegar_webapp",
            # a fixed append-only JSONL audit trail
            "_tool_run_shell_command",
            # the notes store, at the notes directory JARVIS owns
            "_tool_save_note",
        }
        offenders = _raw_write_sites(_tree(EXECUTOR))
        unexpected = offenders - allowed
        assert unexpected == set(), (
            f"handlers writing bytes with no precondition: {sorted(unexpected)}")

        # Non-vacuity for the allowlist itself, in BOTH directions. A stale entry
        # that no longer writes would silently widen the exemption to whatever
        # takes its place; and an entry that gained a caller-supplied path would
        # be a genuine bypass wearing an approved name.
        assert allowed <= offenders, (
            f"stale allowlist entries, they no longer write: "
            f"{sorted(allowed - offenders)}")
        for name in sorted(allowed):
            tool = name[len("_tool_"):]
            assert tool not in FILE_CAPABLE_TOOLS, (
                f"{name} now consumes a caller-supplied path ("
                f"{FILE_CAPABLE_TOOLS.get(tool)}) and no longer qualifies for "
                f"the no-precondition exemption; route it through cas_write_text")

    def test_every_file_capable_write_tool_is_covered_by_the_primitive(self):
        """The other direction: start from the POLICY table, not from the code.

        `FILE_CAPABLE_TOOLS` is the repository's own register of handlers that
        take a caller path. Every entry whose intent mutates must reach
        `cas_write_text`. Starting here rather than from the handlers is what
        catches a NEW write tool that was never wired to the control at all.
        """
        from tools.executor import FILE_CAPABLE_TOOLS, FileIntent
        tree = _tree(EXECUTOR)
        mutating = {tool for tool, (_arg, intent) in FILE_CAPABLE_TOOLS.items()
                    if intent in (FileIntent.WRITE, FileIntent.DESTINATION)}
        assert mutating, "the policy table lists no mutating file tool at all"
        unguarded = []
        for tool in sorted(mutating):
            handler = _functions(tree).get(f"_tool_{tool}")
            if handler is None:
                continue
            calls = _calls(handler)
            if "cas_write_text" not in calls and not (calls & _SCREENSHOT_SINKS):
                unguarded.append(tool)
        assert unguarded == [], (
            f"mutating file tools that reach no guarded write primitive: "
            f"{unguarded}")

    def test_the_handler_still_gates_the_path_through_the_one_resolver(self):
        """Containment is a real boundary and remains on the mutation path."""
        handler = _named(_tree(EXECUTOR), "_tool_write_file")
        assert "_resolve_within_allowed" in _calls(handler)

    @pytest.mark.parametrize("escape", [
        "/etc/m68c-should-not-exist",
        "../../../../etc/m68c-should-not-exist",
        "~/../../etc/m68c-should-not-exist",
    ])
    def test_a_write_outside_the_governed_roots_is_actually_refused(self, escape):
        """BEHAVIOURAL, not structural — and that distinction is the finding.

        The structural test above only proves the resolver is CALLED. The
        campaign mutation `D_handler_skips_containment` kept the call and
        defeated it with `_resolve_within_allowed(path) or Path(path)`, and
        survived: the call site was intact and the boundary was gone. Only
        observing the refusal catches that.
        """
        from tools.executor import ToolExecutor
        executor = ToolExecutor.__new__(ToolExecutor)
        result = executor._tool_write_file(escape, "payload\n")
        assert result.get("error_code") == "PATH_NOT_ALLOWED", result
        assert "written" not in result
        assert not Path("/etc/m68c-should-not-exist").exists()


class TestCASIsStructurallyBeforeTheMutation:
    """A stale source must not reach the mutation primitive."""

    def test_the_comparison_precedes_the_replace_in_the_source_order(self):
        """Not a preflight: compare and replace are in ONE locked function, in
        that order. A mutation that hoists the compare out is visible here."""
        critical = _named(_tree(SOURCE_INTEGRITY), "_write_locked")
        lines_compare, lines_replace = [], []
        for child in ast.walk(critical):
            if isinstance(child, ast.Compare):
                src = ast.dump(child)
                if "expected_sha256" in src:
                    lines_compare.append(child.lineno)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr == "replace":
                lines_replace.append(child.lineno)
        assert lines_compare, "no expected_sha256 comparison in the critical section"
        assert lines_replace, "no os.replace in the critical section"
        assert min(lines_compare) < min(lines_replace), (
            "the precondition comparison no longer precedes the replace")

    def test_the_critical_section_runs_under_the_advisory_lock(self):
        """MEASURED: without the lock, two writers both returned APPLIED over
        one surviving file. The lock is what makes the loser deterministic."""
        writer = _named(_tree(SOURCE_INTEGRITY), "cas_write_text")
        withs = [node for node in ast.walk(writer) if isinstance(node, ast.With)]
        guarded = [
            node for node in withs
            if any(isinstance(item.context_expr, ast.Call)
                   and isinstance(item.context_expr.func, ast.Name)
                   and item.context_expr.func.id == "_serialised_on"
                   for item in node.items)]
        assert guarded, "cas_write_text no longer takes the serialising lock"
        called_inside = set()
        for node in guarded:
            called_inside |= _calls(node)
        assert "_write_locked" in called_inside, (
            "the critical section is no longer called inside the lock")

    def test_the_write_primitive_still_uses_temp_file_plus_replace(self):
        """The ATOMICITY control, structurally.

        `B_direct_open_instead_of_replace` swapped the temp-and-replace for
        `open(target, "wb")` and survived the handler-level sweep, because that
        sweep only scans `_tool_*` functions in executor.py — the raw write had
        moved one module down. Atomicity has to be asserted where it lives.
        """
        critical = _named(_tree(SOURCE_INTEGRITY), "_write_locked")
        calls = _calls(critical)
        assert "mkstemp" in calls, (
            "the whole-file write no longer stages into a temp file; a crash "
            "or a concurrent reader can now observe a partial file")
        assert "replace" in calls, "os.replace is gone; the write is not atomic"
        assert "fsync" in calls, "the staged bytes are never flushed to the device"
        # And the target is never itself opened for writing on the whole-file
        # path — which is WHY a failure before the replace cannot damage it.
        for node in ast.walk(critical):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                assert isinstance(mode, ast.Constant) and mode.value == "a", (
                    "the critical section opens a file for writing outside the "
                    "append branch; only the temp file may be written")

    def test_the_precondition_is_not_satisfiable_by_a_missing_digest(self):
        """A None digest must never compare equal to a supplied expectation."""
        from core.source_integrity import cas_write_text, WriteStatus
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            receipt = cas_write_text(Path(tmp) / "absent.py", "x\n",
                                     expected_sha256="0" * 64)
            assert receipt.status is WriteStatus.REJECTED_STALE


# ══ DETECTOR 2 · a patch-APPLICATION call site ═════════════════════════════

#: argv-shaped patch application. `git apply`, `git am`, `patch(1)`.
_APPLY_SUBCOMMANDS = frozenset({"apply", "am"})


def _patch_application_sites(tree: ast.Module) -> set[str]:
    """Functions that hand a patch to something that applies it.

    Looks for a list/tuple literal whose elements spell a patch-applying argv —
    ``["git", "apply", ...]`` or ``["patch", ...]`` — which is the shape every
    subprocess call site in this repository uses (``shell=False`` is mandatory,
    so there are no command STRINGS to scan).
    """
    offenders: set[str] = set()
    for name, node in _functions(tree).items():
        for child in ast.walk(node):
            if not isinstance(child, (ast.List, ast.Tuple)):
                continue
            words = [el.value for el in child.elts
                     if isinstance(el, ast.Constant) and isinstance(el.value, str)]
            if not words:
                continue
            if words[0] == "patch":
                offenders.add(name)
            elif words[0] == "git" and len(words) > 1 \
                    and words[1] in _APPLY_SUBCOMMANDS:
                offenders.add(name)
    return offenders


class TestNoPatchApplicationPathExists:
    """M68C Finding D is NOT_REPRODUCIBLE, and this is how that is kept true.

    There is no `git apply`, no `patch(1)`, no hunk splicer. `git_query` is
    READ_ONLY and its diff output is a capped RENDERING. So there is no
    applicability gate to bypass — which is a fact to record and keep, not a
    reason to skip the question.
    """

    def test_the_detector_fires_on_an_injected_git_apply(self):
        """NON-VACUITY WITNESS — without this the test below is worthless.

        This is the exact failure mode §10 names: a structural probe over an
        EMPTY set passes for free. So the detector is proven to work on a
        synthetic module that really does apply a patch, before it is believed
        about the real tree.
        """
        synthetic = _parse(
            "def rogue_apply(diff_text, repo):\n"
            "    subprocess.run(['git', 'apply', '--index', '-'], input=diff_text)\n"
            "def rogue_patch(diff_text):\n"
            "    subprocess.run(['patch', '-p1'], input=diff_text)\n"
            "def rogue_am(mbox):\n"
            "    subprocess.run(['git', 'am', mbox])\n"
            "def innocent_status():\n"
            "    subprocess.run(['git', 'status'])\n")
        assert _patch_application_sites(synthetic) == {
            "rogue_apply", "rogue_patch", "rogue_am"}

    def test_the_real_tree_contains_no_patch_application_site(self):
        production = [p for p in (PACKAGE_ROOT / "core").rglob("*.py")]
        production += [p for p in (PACKAGE_ROOT / "tools").rglob("*.py")]
        production += [p for p in (PACKAGE_ROOT / "training_gym").rglob("*.py")]
        found: dict[str, set[str]] = {}
        for path in production:
            sites = _patch_application_sites(_tree(path))
            if sites:
                found[str(path.relative_to(PACKAGE_ROOT))] = sites
        assert found == {}, (
            f"a patch-application path appeared: {found}. M68C recorded that "
            f"none existed, so this one has NO applicability gate, NO "
            f"pre-mutation recheck and NO multi-file precondition ordering. "
            f"Add them with the control, not after it.")

    def test_the_recorded_fact_matches_the_scanned_reality(self):
        """The constant and the tree must agree. A stale constant is a lie."""
        from core.source_integrity import PATCH_APPLICATION_PATHS
        assert PATCH_APPLICATION_PATHS == ()

    def test_git_query_is_declared_read_only(self):
        """The only git surface the model can reach stays non-mutating."""
        from core.risk_classes import RiskClass, classify_tool
        assert classify_tool("git_query") is RiskClass.READ_ONLY


# ══ DETECTOR 3 · truncation that is not out of band ════════════════════════

class TestTruncationCannotBeInBandOnly:
    """A truncated read/transport must be detectable WITHOUT parsing content."""

    def test_the_detector_sees_a_result_that_omits_the_flag(self):
        """NON-VACUITY WITNESS for the two assertions below."""
        silent = _parse(
            "def handler(self, path):\n"
            "    return {'content': body[:8000], 'chars': len(body)}\n")
        keys = _returned_dict_keys(_named(silent, "handler"))
        assert "truncated" not in keys
        loud = _parse(
            "def handler(self, path):\n"
            "    return {'content': body[:8000], 'truncated': True}\n")
        assert "truncated" in _returned_dict_keys(_named(loud, "handler"))

    def test_read_file_returns_an_out_of_band_truncation_flag(self):
        keys = _returned_dict_keys(_named(_tree(EXECUTOR), "_tool_read_file"))
        assert "truncated" in keys, (
            "read_file reports truncation only inside `content` again; a "
            "complete file whose last line mimics the marker is then "
            "indistinguishable from a cut one (measured)")
        assert "source" in keys, "the source identity is gone from read_file"

    def test_git_query_returns_an_out_of_band_truncation_flag(self):
        keys = _returned_dict_keys(_named(_tree(EXECUTOR), "_tool_git_query"))
        assert "truncated" in keys, (
            "git_query truncates stdout silently again — measured at 31876 "
            "characters arriving as exactly 3000 with no signal")
        assert "transport" in keys

    def test_the_identity_digest_is_taken_over_the_complete_source(self):
        """Hashing the RETAINED bytes would make a cut read self-consistent."""
        import inspect
        from core import source_integrity
        src = inspect.getsource(source_integrity.identify_transport)
        assert "digest_bytes(complete_bytes)" in src, (
            "identify_transport no longer digests the COMPLETE output")

    def test_a_truncated_identity_is_never_complete(self):
        """The behavioural half: no combination of inputs yields both."""
        from core.source_integrity import identify_source
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "f.py"
            target.write_text("x" * 100)
            cut = identify_source(target, content_chars_total=100,
                                  content_chars_returned=10)
            assert cut.truncated is True and cut.complete is False


# ══ DETECTOR 4 · success without post-state evidence ═══════════════════════

class TestSuccessRequiresPostStateEvidence:
    """Every claimed successful mutation must have post-state evidence."""

    def test_the_applied_return_is_preceded_by_the_post_state_comparison(self):
        critical = _named(_tree(SOURCE_INTEGRITY), "_write_locked")
        after_compare = [
            node.lineno for node in ast.walk(critical)
            if isinstance(node, ast.Compare)
            and "intended_whole" in ast.dump(node)
            and "after" in ast.dump(node)]
        assert after_compare, (
            "the post-state digest comparison is gone; APPLIED would then mean "
            "'the replace returned', not 'the intended bytes are on disk'")

    def test_applied_is_unreachable_when_the_post_state_disagrees(self):
        """Behavioural, not structural: the comparison actually decides."""
        import tempfile
        import core.source_integrity as si
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "t.py"
            real = si.identify_source
            state = {"n": 0}

            def meddle(path, **kwargs):
                state["n"] += 1
                if state["n"] == 2:
                    Path(path).write_text("not what was asked for\n")
                return real(path, **kwargs)

            si.identify_source = meddle
            try:
                receipt = si.cas_write_text(target, "intended\n")
            finally:
                si.identify_source = real
            assert receipt.status is si.WriteStatus.PARTIAL_OR_UNKNOWN
            assert receipt.applied is False

    def test_no_status_other_than_applied_claims_a_committed_effect(self):
        from core.source_integrity import WriteStatus, external_outcome_of_write
        committed = {s for s in WriteStatus
                     if external_outcome_of_write(s).value == "PROVEN_COMMITTED"}
        assert committed == {WriteStatus.APPLIED}

    def test_the_status_mapping_is_total(self):
        """A new status with no M65D mapping must not default to anything."""
        from core.source_integrity import WriteStatus, _WRITE_STATUS_OUTCOME
        assert set(_WRITE_STATUS_OUTCOME) == set(WriteStatus)


# ══ DETECTOR 5 · a validator promoted to authority ═════════════════════════

class TestGraderScopeCannotDrift:
    """A grader called "applicable" must prove applicability, not syntax.

    The declaration lives in `core.source_integrity.PATCH_VALIDATION_SCOPES`
    rather than on each component, because `jarvis/training_gym/graders/` is
    FROZEN — byte-identical since 05c043b3, enforced by
    `test_the_graders_and_the_refusal_detector_are_untouched`. M68C's first draft
    put a `VALIDATION_SCOPE` attribute on `DiffBudgetGrader` and broke that
    freeze. A registry keeps the claim machine-checkable without editing
    preregistered measurement machinery.
    """

    def test_every_registry_key_resolves_to_a_real_component(self):
        """NON-VACUITY WITNESS. A typo'd key would make every check below pass
        over nothing — the exact failure mode §10 names."""
        import importlib
        from core.source_integrity import PATCH_VALIDATION_SCOPES
        assert PATCH_VALIDATION_SCOPES, "the scope registry is empty"
        for dotted in PATCH_VALIDATION_SCOPES:
            module_path, _, attr = dotted.rpartition(".")
            module = importlib.import_module(module_path)
            assert hasattr(module, attr), f"{dotted} does not exist"

    def test_nothing_in_the_registry_claims_applicability_proven(self):
        from core.source_integrity import (
            PatchValidationScope, PATCH_VALIDATION_SCOPES)
        claimants = [k for k, v in PATCH_VALIDATION_SCOPES.items()
                     if v is PatchValidationScope.APPLICABILITY_PROVEN]
        assert claimants == [], (
            f"{claimants} claim APPLICABILITY_PROVEN. Nothing in this build "
            f"applies a patch, so nothing can have proven one applies.")

    def test_no_component_self_declares_a_scope_beside_the_registry(self):
        """AST sweep: a second declaration is a second writable copy of the
        contract, and the one place it would most likely appear is the frozen
        grader M68C already broke once."""
        scanned = list((PACKAGE_ROOT / "core").rglob("*.py"))
        scanned += list((PACKAGE_ROOT / "tools").rglob("*.py"))
        scanned += list((PACKAGE_ROOT / "training_gym").rglob("*.py"))
        offenders = []
        for path in scanned:
            if path == SOURCE_INTEGRITY:
                continue                      # it DEFINES the registry
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "VALIDATION_SCOPE"
                        for t in node.targets):
                    offenders.append(str(path.relative_to(PACKAGE_ROOT)))
        assert offenders == [], f"competing scope declarations in {offenders}"

    def test_the_frozen_grader_trees_are_byte_identical_to_their_seal(self):
        """The freeze itself, asserted HERE too.

        The authoritative suite already enforces it, but 12 000 tests later is a
        bad place to learn that an M68C edit touched preregistered machinery —
        this run takes milliseconds and names the milestone that broke it.
        """
        import subprocess  # nosec B404 - fixed argv, shell=False
        seal = "05c043b3a89cdb675846abb8aabf1f476c6d7796"
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["git", "diff", "--name-only", seal],
            cwd=str(PACKAGE_ROOT.parent), capture_output=True, text=True,
            check=False)
        if proc.returncode != 0:  # pragma: no cover - shallow clone
            pytest.skip("the S3Q.0 seal is not reachable in this checkout")
        touched = proc.stdout.split()
        assert touched, "an empty diff would make this assertion vacuous"
        frozen = [p for p in touched
                  if p.startswith(("jarvis/training_gym/graders/",
                                   "jarvis/training_gym/training/"))]
        assert frozen == [], f"M68C touched frozen scientific machinery: {frozen}"

    def test_the_grader_exposes_no_applicability_field(self):
        """A FileDiff carries what was PARSED. Adding `applies` to it would make
        a syntax result look like an applicability result at every call site."""
        from training_gym.graders.diff_budget_grader import FileDiff
        fields = set(FileDiff.__dataclass_fields__)
        assert not fields & {"applies", "applicable", "apply_check"}
