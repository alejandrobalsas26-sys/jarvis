"""scripts/mutation_campaign_m68c.py — V69 M68C (§12): the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before
any mutation reports DETECTED for every mutation and turns the whole campaign
into a green rubber stamp. Exit 2 means "could not be evaluated".

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a property
of the patch/mutation execution path with no test behind it. Requirement:
0 survivors, 0 anchor errors, 0 vacuous mappings. There is no percentage target.
Run from `jarvis/`.

Nothing here mutates sealed historical state: every target is a file M68C wrote
or changed.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
SI = "core/source_integrity.py"
EX = "tools/executor.py"
# training_gym/graders/ is FROZEN since 05c043b3 and is NOT a mutation
# target: this campaign never writes to preregistered machinery.

# ── mapped tests ─────────────────────────────────────────────────────────────
TM = "tests/test_source_integrity_m68c.py"
AC = "tests/test_patch_execution_absent_controls_m68c.py"

A_READ = f"{TM}::TestReadIdentity"
A_TRUNC = f"{TM}::TestTruncationIsOutOfBand"
B_NORM = f"{TM}::TestNormalWrites"
B_CAS = f"{TM}::TestCompareAndSwap"
B_ATOM = f"{TM}::TestAtomicity"
C_TRAN = f"{TM}::TestTransportIdentity"
C_GIT = f"{TM}::TestGitQueryDeclaresItsTruncation"
# NOTE: there is deliberately NO mutation target for
# `tests/test_source_integrity_m68c.py::TestGraderIsSyntaxOnly`. Its subject is `DiffBudgetGrader`, which lives in
# the FROZEN tree, so the campaign cannot falsify the grader's own internal
# controls without writing to preregistered machinery. Those are verified
# behaviourally instead, and this gap is recorded as a residual rather than
# papered over with a mutation that targets something else and claims the credit.
E_REC = f"{TM}::TestReceiptTruth"
E_HAND = f"{TM}::TestHandlerSurfacesTheReceipt"
R_RACE = f"{TM}::TestRaces"
F_FAIL = f"{TM}::TestFailureBoundaries"

AC_CAS = f"{AC}::TestNoMutationPathBypassesCAS"
AC_ORD = f"{AC}::TestCASIsStructurallyBeforeTheMutation"
AC_APPLY = f"{AC}::TestNoPatchApplicationPathExists"
AC_TRUNC = f"{AC}::TestTruncationCannotBeInBandOnly"
AC_POST = f"{AC}::TestSuccessRequiresPostStateEvidence"
AC_SCOPE = f"{AC}::TestGraderScopeCannotDrift"


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: list[dict] = []

# ── A · READ IDENTITY ────────────────────────────────────────────────────────
MUTATIONS += [
    # "hide truncation": the flag stops telling the truth.
    _mut("A_truncated_always_false", "A_TRUTH", SI,
         "    truncated = bool(\n"
         "        content_chars_total is not None\n"
         "        and content_chars_returned is not None\n"
         "        and content_chars_returned < content_chars_total\n"
         "    )",
         "    truncated = False", A_TRUNC),
    # "mark truncated source complete": the two halves of `complete` decoupled.
    _mut("A_complete_ignores_truncation", "A_TRUTH", SI,
         "        return (self.exists\n"
         "                and not self.truncated\n"
         "                and self.digest_covers == DIGEST_COVERS_COMPLETE\n"
         "                and self.sha256 is not None)",
         "        return self.exists", A_TRUNC),
    _mut("A_complete_ignores_digest", "A_TRUTH", SI,
         "                and self.digest_covers == DIGEST_COVERS_COMPLETE\n"
         "                and self.sha256 is not None)",
         "                )", A_READ),  # remapped: see test_completeness_requires_a_digest
    # "remove digest": identity without a version token.
    _mut("A_digest_removed", "A_DIGEST", SI,
         "    sha = digest_file(resolved) if exists else None",
         "    sha = None", A_READ),
    # An absent file acquires an identity a precondition could match.
    _mut("A_absent_gets_a_digest", "A_DIGEST", SI,
         "    covers = DIGEST_COVERS_COMPLETE if sha is not None else DIGEST_COVERS_NOTHING",
         "    covers = DIGEST_COVERS_COMPLETE", A_READ),
    # The digest stops covering the whole file.
    _mut("A_digest_first_chunk_only", "A_DIGEST", SI,
         "                chunk = handle.read(_DIGEST_CHUNK)\n"
         "                if not chunk:\n"
         "                    break\n"
         "                digest.update(chunk)",
         "                chunk = handle.read(16)\n"
         "                if not chunk:\n"
         "                    break\n"
         "                digest.update(chunk)\n"
         "                break", A_READ),  # covered by the shared-prefix pair
    # The handler stops reporting the total, as it did before M68C.
    _mut("A_handler_total_is_the_cut_length", "A_HANDLER", EX,
         "                content_chars_total=full_chars,",
         "                content_chars_total=min(full_chars, max_chars),", A_TRUNC),
    _mut("A_handler_flag_dropped", "A_HANDLER", EX,
         '                "truncated": identity.truncated,',
         '                "truncated": False,', A_TRUNC),
    # Remapped A_TRUNC <- AC_TRUNC: the absent-control probe checks the KEY is
    # present, which an empty dict satisfies. The payload CONTENTS are the control.
    _mut("A_handler_identity_dropped", "A_HANDLER", EX,
         '                "source": identity.to_dict(),',
         '                "source": {},', A_TRUNC),
    _mut("A_size_is_chars_not_bytes", "A_HANDLER", SI,
         "        size = int(stat.st_size)",
         "        size = 0", A_READ),
]

# ── B · CAS / ATOMIC WRITE ───────────────────────────────────────────────────
MUTATIONS += [
    # "skip expected-hash comparison".
    _mut("B_precondition_never_checked", "B_CAS", SI,
         "    if expected_sha256 is not None:\n"
         "        if expected_sha256 == ABSENT:",
         "    if False:\n"
         "        if expected_sha256 == ABSENT:", B_CAS),
    # "always accept stale write".
    _mut("B_stale_accepted", "B_CAS", SI,
         "        elif before.sha256 != expected_sha256:",
         "        elif False:", B_CAS),
    # The ABSENT sentinel stops guarding create-new.
    _mut("B_absent_sentinel_ignored", "B_CAS", SI,
         "            if before.exists:\n"
         "                return _receipt(\n"
         "                    WriteStatus.REJECTED_STALE, before=before,",
         "            if False:\n"
         "                return _receipt(\n"
         "                    WriteStatus.REJECTED_STALE, before=before,", B_CAS),
    # A malformed digest becomes a silent no-op precondition.
    _mut("B_malformed_digest_accepted", "B_CAS", SI,
         "    if expected_sha256 is not None and expected_sha256 != ABSENT \\\n"
         "            and not _valid_digest(expected_sha256):",
         "    if False:", B_CAS),
    _mut("B_digest_validation_loosened", "B_CAS", SI,
         "    return len(value) == 64 and set(value) <= _HEX",
         "    return True", B_CAS),
    # "replace atomically with direct open(..., 'w')".
    _mut("B_direct_open_instead_of_replace", "B_ATOMIC", SI,
         "        handle_fd, tmp_path = tempfile.mkstemp(\n"
         "            dir=str(target.parent), prefix=f\".{target.name}.\", suffix=\".m68c-tmp\")\n"
         "        with os.fdopen(handle_fd, \"wb\") as handle:\n"
         "            handle.write(payload)\n"
         "            handle.flush()\n"
         "            os.fsync(handle.fileno())",
         "        tmp_path = None\n"
         "        with open(target, \"wb\") as handle:\n"
         "            handle.write(payload)", AC_ORD),  # atomicity lives in SI, not EX
    # The temp file leaves the target's filesystem, so replace can be a copy.
    _mut("B_temp_in_system_tmp", "B_ATOMIC", SI,
         "            dir=str(target.parent), prefix=f\".{target.name}.\", suffix=\".m68c-tmp\")",
         "            dir=None, prefix=f\".{target.name}.\", suffix=\".m68c-tmp\")", B_ATOM),
    # The temp file is never cleaned up.
    _mut("B_temp_never_unlinked", "B_CLEANUP", SI,
         "    try:\n        os.unlink(tmp_path)\n    except OSError:\n        pass",
         "    return", F_FAIL),
    # The lock's descriptor is never released: a leak that fails under load, not
    # on the first call, which is the shape §13's "no leaked locks" asks about.
    _mut("B_lock_fd_never_closed", "B_CLEANUP", SI,
         "        with contextlib.suppress(OSError):\n"
         "            fcntl.flock(fd, fcntl.LOCK_UN)\n"
         "        os.close(fd)",
         "        with contextlib.suppress(OSError):\n"
         "            fcntl.flock(fd, fcntl.LOCK_UN)", F_FAIL),
    # "enforce CAS in helper but bypass it at the real caller".
    _mut("B_handler_bypasses_the_primitive", "B_CALLSITE", EX,
         "            receipt = cas_write_text(\n"
         "                p, content, mode=mode,\n"
         "                expected_sha256=expected_sha256, dry_run=dry_run)",
         "            p.parent.mkdir(parents=True, exist_ok=True)\n"
         "            with open(p, mode, encoding=\"utf-8\") as _f:\n"
         "                _f.write(content)\n"
         "            receipt = cas_write_text(p, content, mode=mode, dry_run=True)",
         AC_CAS),
    # The handler accepts the precondition and silently drops it.
    _mut("B_handler_drops_the_precondition", "B_CALLSITE", EX,
         "                expected_sha256=expected_sha256, dry_run=dry_run)",
         "                expected_sha256=None, dry_run=dry_run)", E_HAND),
    # The lock goes away: simultaneous writers both claim APPLIED (measured).
    _mut("B_lock_is_a_noop", "B_RACE", SI,
         "    try:\n        import fcntl\n    except ImportError:  # pragma: no cover - Windows\n"
         "        yield False\n        return",
         "    try:\n        import fcntl  # noqa: F401\n    except ImportError:  # pragma: no cover\n"
         "        pass\n    yield False\n    return", R_RACE),
    # The lock is taken but the compare happens outside it.
    _mut("B_compare_outside_the_lock", "B_RACE", SI,
         "    with _serialised_on(target.parent) as serialised:\n"
         "        return _write_locked(",
         "    with _serialised_on(target.parent) as serialised:\n"
         "        pass\n"
         "    if True:\n"
         "        return _write_locked(", AC_ORD),
]

# ── C · PATCH TRANSPORT ──────────────────────────────────────────────────────
MUTATIONS += [
    # "treat truncated diff as complete".
    _mut("C_truncation_declared_away", "C_TRUTH", SI,
         "    truncated = len(retained) < len(complete)",
         "    truncated = False", C_TRAN),
    _mut("C_class_always_complete", "C_TRUTH", SI,
         "        artifact_class=(TransportArtifactClass.TRUNCATED_DISPLAY_ONLY if truncated\n"
         "                        else TransportArtifactClass.COMPLETE_PATCH),",
         "        artifact_class=TransportArtifactClass.COMPLETE_PATCH,", C_TRAN),
    # "allow display buffer as apply input".
    _mut("C_truncated_is_usable_as_patch", "C_AUTHORITY", SI,
         "        return (self.artifact_class is TransportArtifactClass.COMPLETE_PATCH\n"
         "                and not self.truncated\n"
         "                and self.sha256 is not None)",
         "        return True", C_TRAN),
    # "hash only displayed bytes" — the cut rendering becomes self-consistent.
    _mut("C_digest_over_retained_only", "C_DIGEST", SI,
         "        sha256=digest_bytes(complete_bytes),",
         "        sha256=digest_bytes(retained_bytes),", C_TRAN),
    _mut("C_total_bytes_is_retained", "C_DIGEST", SI,
         "        total_bytes=len(complete_bytes),",
         "        total_bytes=len(retained_bytes),", C_TRAN),
    # git_query stops declaring the cut, exactly as before M68C.
    _mut("C_git_query_flag_dropped", "C_HANDLER", EX,
         '                "truncated": transport.truncated,',
         '                "truncated": False,', C_GIT),
    # Remapped C_GIT <- AC_TRUNC, same reason as A_handler_identity_dropped.
    _mut("C_git_query_identity_dropped", "C_HANDLER", EX,
         '                "transport": transport.to_dict(),',
         '                "transport": {},', C_GIT),
    _mut("C_git_query_identifies_the_snippet", "C_HANDLER", EX,
         "            transport = identify_transport(\n"
         "                proc.stdout, shown, producer=f\"git {operation}\")",
         "            transport = identify_transport(\n"
         "                shown, shown, producer=f\"git {operation}\")", C_GIT),
]

# ── D · APPLICABILITY / VALIDATOR TRUTH ──────────────────────────────────────
MUTATIONS += [
    # "accept syntax-only validation" as applicability.
    #
    # NOTE: these target the REGISTRY in core/source_integrity.py, not the grader.
    # `jarvis/training_gym/graders/` is FROZEN (byte-identical since 05c043b3) and
    # this campaign will not write to it even transiently: a crash between mutate
    # and restore would leave preregistered scientific machinery modified. That is
    # a real limit on falsification coverage and is recorded as such in
    # docs/v69_M68C_PATCH_EXECUTION_INTEGRITY.md - the grader's own internal
    # controls are verified behaviourally instead (TestGraderIsSyntaxOnly).
    _mut("D_registry_claims_applicability", "D_SCOPE", SI,
         '    "training_gym.graders.diff_budget_grader.DiffBudgetGrader":\n'
         "        PatchValidationScope.SYNTAX_ONLY,",
         '    "training_gym.graders.diff_budget_grader.DiffBudgetGrader":\n'
         "        PatchValidationScope.APPLICABILITY_PROVEN,", AC_SCOPE),
    # CORRECTED. The first draft was `= {} or {...}`, which evaluates to the
    # SECOND dict because `{}` is falsy - the registry was never emptied and the
    # mutation was inert. It survived without evidence of a gap, which is exactly
    # what a survivor is for.
    _mut("D_registry_emptied", "D_SCOPE", SI,
         "PATCH_VALIDATION_SCOPES: dict[str, PatchValidationScope] = {\n"
         '    "training_gym.graders.diff_budget_grader.DiffBudgetGrader":\n'
         "        PatchValidationScope.SYNTAX_ONLY,\n"
         '    "training_gym.graders.diff_budget_grader.parse_unified_diff":\n'
         "        PatchValidationScope.SYNTAX_ONLY,\n"
         "}",
         "PATCH_VALIDATION_SCOPES: dict[str, PatchValidationScope] = {}", AC_SCOPE),
    _mut("D_registry_key_is_a_typo", "D_SCOPE", SI,
         '    "training_gym.graders.diff_budget_grader.parse_unified_diff":',
         '    "training_gym.graders.diff_budget_grader.parse_unified_diffs":',
         AC_SCOPE),
    # The recorded "no apply path" fact stops matching a scan.
    _mut("D_apply_paths_fabricated", "D_RECORD", SI,
         "PATCH_APPLICATION_PATHS: tuple[str, ...] = ()",
         'PATCH_APPLICATION_PATHS: tuple[str, ...] = ("tools/executor.py:_apply",)',
         AC_APPLY),
    # "allow ../ path": containment leaves the mutation path.
    _mut("D_handler_skips_containment", "D_PATH", EX,
         "        p = _resolve_within_allowed(path)\n"
         "        if p is None:\n"
         "            logger.warning(\"Intento de escritura bloqueado (fuera del sandbox).\")",
         "        p = _resolve_within_allowed(path) or Path(path).expanduser()\n"
         "        if p is None:\n"
         "            logger.warning(\"Intento de escritura bloqueado (fuera del sandbox).\")",
         AC_CAS),  # behavioural: test_a_write_outside_the_governed_roots
    # The freeze guard itself must be load-bearing.
    _mut("D_freeze_guard_vacuous", "D_FROZEN", AC,
         '        assert touched, "an empty diff would make this assertion vacuous"\n'
         "        frozen = [p for p in touched",
         "        touched = []\n"
         '        assert touched, "an empty diff would make this assertion vacuous"\n'
         "        frozen = [p for p in touched", AC_SCOPE),
]

# ── E · RECEIPTS ─────────────────────────────────────────────────────────────
MUTATIONS += [
    # "report APPLIED before post-state is verified".
    _mut("E_applied_without_post_state", "E_POST", SI,
         "    if after.sha256 != intended_whole:",
         "    if False:", AC_POST),
    _mut("E_post_state_is_the_intent", "E_POST", SI,
         "    after = identify_source(target)\n"
         "    if after.sha256 != intended_whole:",
         "    after = before\n"
         "    if after.sha256 != intended_whole and False:", AC_POST),
    # "omit before identity" / "omit after identity".
    # Both remapped to the serialised-form assertion added to E_REC: the old
    # test read the dataclass attribute, which `to_dict()` is free to drop.
    _mut("E_before_identity_omitted", "E_EVIDENCE", SI,
         '            "before": self.before.to_dict() if self.before else None,',
         '            "before": None,', E_REC),
    _mut("E_after_identity_omitted", "E_EVIDENCE", SI,
         '            "after": self.after.to_dict() if self.after else None,',
         '            "after": None,', E_REC),
    # "convert partial/unknown into success".
    _mut("E_unknown_becomes_committed", "E_TRUTH", SI,
         '    WriteStatus.PARTIAL_OR_UNKNOWN: "UNKNOWN",',
         '    WriteStatus.PARTIAL_OR_UNKNOWN: "PROVEN_COMMITTED",', AC_POST),
    _mut("E_unknown_becomes_no_effect", "E_TRUTH", SI,
         '    WriteStatus.PARTIAL_OR_UNKNOWN: "UNKNOWN",',
         '    WriteStatus.PARTIAL_OR_UNKNOWN: "PROVEN_NOT_EXECUTED",', E_REC),
    # "convert invalid/stale into resolved/applied".
    _mut("E_stale_becomes_committed", "E_TRUTH", SI,
         '    WriteStatus.REJECTED_STALE: "PROVEN_NOT_EXECUTED",',
         '    WriteStatus.REJECTED_STALE: "PROVEN_COMMITTED",', AC_POST),
    _mut("E_validated_becomes_applied", "E_TRUTH", SI,
         "        return _receipt(WriteStatus.VALIDATED_NOT_APPLIED, before=before,",
         "        return _receipt(WriteStatus.APPLIED, before=before,", E_REC),
    # A dry run stops being dry.
    _mut("E_dry_run_ignored", "E_TRUTH", SI,
         "    if dry_run:", "    if False:", E_REC),
    _mut("E_handler_dry_run_reports_a_write", "E_HANDLER", EX,
         '            payload.update({"validated": True, "written": None})',
         '            payload.update({"validated": True, "written": str(p)})', E_HAND),
    # The stale refusal reaches the caller as a success.
    _mut("E_handler_stale_not_surfaced", "E_HANDLER", EX,
         '                "error_code": ERR_PRECONDITION_STALE})',
         '                "error_code": ERR_WRITE_FAILED})', E_HAND),
    # The receipt starts carrying bodies.
    # CORRECTED. The first draft added `reason` to the dict, which is metadata
    # and is None on the success path — it leaked nothing and survived for that
    # reason. A body leak has to carry the body.
    _mut("E_receipt_leaks_the_body", "E_HYGIENE", SI,
         "    return _receipt(WriteStatus.APPLIED, method=MutationMethod.ATOMIC_REPLACE,\n"
         "                    before=before, after=after, written=len(payload),\n"
         "                    atomic=True, serialised=serialised)",
         "    return _receipt(WriteStatus.APPLIED, method=MutationMethod.ATOMIC_REPLACE,\n"
         "                    before=before, after=after, written=len(payload),\n"
         "                    atomic=True, serialised=serialised, reason=content)", E_REC),
    # Crash durability gets claimed by wording (§17).
    _mut("E_claims_crash_durability", "E_CLAIM", SI,
         "    crash_durability_proven: bool = False",
         "    crash_durability_proven: bool = True", B_ATOM),
    _mut("E_claims_atomicity_for_append", "E_CLAIM", SI,
         "        return _receipt(WriteStatus.APPLIED, method=MutationMethod.APPEND,\n"
         "                        before=before, after=after, written=len(payload),\n"
         "                        serialised=serialised)",
         "        return _receipt(WriteStatus.APPLIED, method=MutationMethod.APPEND,\n"
         "                        before=before, after=after, written=len(payload),\n"
         "                        serialised=serialised, atomic=True)", B_NORM),
    # The serialisation claim stops matching reality.
    # CORRECTED. The first draft mutated the DATACLASS default, which no code
    # path reads — `_receipt` always passes `serialised` explicitly — so it was
    # semantically inert and survived without evidence of a gap. The `_receipt`
    # keyword default IS read, by every return that happens before the lock.
    _mut("E_claims_serialised_always", "E_CLAIM", SI,
         "                 atomic=False, serialised=False) -> WriteReceipt:",
         "                 atomic=False, serialised=True) -> WriteReceipt:", E_REC),
]


def _pytest(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:randomly",
         target],
        cwd=_ROOT, capture_output=True, text=True, check=False)


def _preflight(targets: list[str]) -> list[str]:
    """Every mapped test must PASS, and collect at least one test, BEFORE any
    mutation. A red or skipped mapping makes every mutation look DETECTED."""
    bad: list[str] = []
    print(f"PREFLIGHT — {len(targets)} mapped test target(s)")
    for target in sorted(targets):
        proc = _pytest(target)
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else "<no output>"
        if proc.returncode != 0:
            bad.append(f"{target}: RED before mutation ({summary})")
            print(f"  [RED ] {target}")
        elif " passed" not in summary or "no tests ran" in summary:
            bad.append(f"{target}: vacuous ({summary})")
            print(f"  [VOID] {target} — {summary}")
        else:
            print(f"  [ OK ] {target} — {summary}")
    return bad


def _run() -> int:
    total = len(MUTATIONS)
    targets = sorted({m["test"] for m in MUTATIONS})
    print(f"M68C FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68C_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    survivors: list[str] = []
    anchor_errors: list[str] = []
    detected = 0
    print(f"\nMUTATING — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']:36s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:36s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                print(f"  [SURVIVOR] {m['id']:36s} ({m['cat']}) — NO TEST FAILED")
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    print(f"\n{'=' * 72}")
    print(f"mutations:      {total}")
    print(f"detected:       {detected}")
    print(f"survivors:      {len(survivors)}  {survivors if survivors else ''}")
    print(f"anchor errors:  {len(anchor_errors)}  {anchor_errors if anchor_errors else ''}")
    ok = not survivors and not anchor_errors
    print(f"M68C_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(survivors)} survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
