"""scripts/mutation_campaign_m68d1.py — V69 M68D.1: the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before
any mutation reports DETECTED for every mutation and turns the whole campaign
into a green rubber stamp. Exit 2 means "could not be evaluated".

WHAT IS UNDER MUTATION HERE, AND WHY IT IS MOSTLY TESTS
=======================================================
M68D.1 is a portability closure. Its controls are, in order of load-bearing-
ness: the MEASURED capability probe, the CROSS_PLATFORM fixtures that keep the
coherence invariant running where the POSIX attack cannot be staged, the
byte-explicit fixture writers, and the Windows CI job that executes all of it.
Three of those four live in test code, so that is where the mutations go — a
capability probe that lies, a fixture that swallows a refusal, or a job that
does not run are all exactly as decorative as a production control that has
been deleted.

Production is mutated where M68D.1 actually depends on it: the single coherent
opening, the digest over the captured bytes, the `recheck()` that makes the
derived path safe, and the Git-index executable-mode authority.

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a property
with no test behind it. Requirement: 0 UNEXPLAINED survivors, 0 anchor errors.
A survivor may be registered in :data:`EXPLAINED_SURVIVORS` only with a
MEASURED reason — never to make the number look better, and never by deleting
the mutation.

Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
SI = "core/source_integrity.py"
EX = "tools/executor.py"
CPV = "scripts/verify_m62_control_plane.py"
CAP = "tests/_test_support/platform_capabilities.py"
T_H02 = "tests/test_trust_boundary_m68d_h02_source_identity.py"
T_M68C = "tests/test_source_integrity_m68c.py"
T_H05 = "tests/test_trust_boundary_m68d_h05_portability.py"
T_CLOSE = "tests/test_windows_portability_closure_m68d1.py"
WORKFLOW = "../.github/workflows/ci.yml"

# ── mapped tests ─────────────────────────────────────────────────────────────
C_PROBE = f"{T_CLOSE}::TestTheCapabilityProbeIsHonest"
C_COHERE = f"{T_CLOSE}::TestCoherenceHoldsOnEveryPlatform"
C_BYTES = f"{T_CLOSE}::TestByteIdentityIsExplicit"
C_SKIP = f"{T_CLOSE}::TestNoSkipWashing"
C_FAIL = f"{T_CLOSE}::TestFailureInjection"
C_GIT = f"{T_CLOSE}::TestGitRepositorySemantics"
C_CI = f"{T_CLOSE}::TestWindowsCiStillBlocks"

H02_SNAP = f"{T_H02}::TestSnapshotCoherence"
H02_RACE = f"{T_H02}::TestSnapshotRaces"
H02_READ = f"{T_H02}::TestReadFileIdentity"
H02_DERIVED = f"{T_H02}::TestDerivedFormats"
H02_ABSENT = f"{T_H02}::TestAbsentControls"

H05_MODE = f"{T_H05}::TestGitModeIsTheAuthority"
H05_NEWLINE = f"{T_H05}::TestNewlinePinning"
H05_CAP = f"{T_H05}::TestPlatformCapabilities"

#: Survivors with a MEASURED explanation. Empty is the expected state.
EXPLAINED_SURVIVORS: "dict[str, str]" = {}


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: "list[dict]" = []

# ══ A · H02 PORTABILITY ═════════════════════════════════════════════════════
MUTATIONS += [
    # The descriptor is released before the digest, so the hash describes a
    # path and not an observation. This is the original TOCTOU bug, restored.
    _mut("A_close_the_descriptor_before_digesting", "A_COHERENCE", SI,
         "        after = os.fstat(fd)\n"
         "        stable = (",
         "        after = os.fstat(fd)\n"
         "        os.close(fd)\n"
         "        stable = (", H02_RACE),
    # The digest comes from a LATER reopening of the mutable path.
    _mut("A_digest_by_reopening_the_path", "A_COHERENCE", SI,
         "            payload = b\"\".join(chunks)\n"
         "            digest.update(payload)",
         "            payload = b\"\".join(chunks)\n"
         "            with open(str(resolved), \"rb\") as _re:\n"
         "                digest.update(_re.read())", H02_RACE),
    # `recheck()` becomes a no-op, so the derived path's two passes over one
    # descriptor can disagree without anybody noticing.
    _mut("A_recheck_always_agrees", "A_COHERENCE", SI,
         "        if (now.st_size != self._stat.st_size",
         "        if False and (now.st_size != self._stat.st_size", H02_DERIVED),
    # The handler stops acting on the recheck, which is the Windows-capable
    # control for a derived read.
    _mut("A_handler_ignores_the_recheck", "A_COHERENCE", EX,
         "                    if not snapshot.recheck():",
         "                    if False and not snapshot.recheck():", H02_DERIVED),
    # The stream reopens the mutable path instead of duplicating the descriptor.
    _mut("A_stream_reopens_the_path", "A_COHERENCE", SI,
         "        dup = os.dup(self._fd)\n"
         "        handle = os.fdopen(dup, \"rb\")",
         "        handle = open(self.path, \"rb\")  # noqa: SIM115\n"
         "        dup = -1", H02_DERIVED),
    # An unstable observation hands out a precondition again.
    _mut("A_unstable_still_states_a_precondition", "A_COHERENCE", SI,
         "    sha = snapshot.sha256 if snapshot.stable else None",
         "    sha = snapshot.sha256", C_COHERE),
    # THE SHAPE M68D.1 §17 FORBIDS: a refused adversary becomes success.
    _mut("A_refusal_in_the_race_fixture_is_swallowed", "A_VACUITY", T_H02,
         "        assert refused == [], (",
         "        assert refused == [] or True, (", C_FAIL),
    _mut("A_race_fixture_stops_recording_refusals", "A_VACUITY", T_H02,
         "            except OSError as exc:\n"
         "                with lock:\n"
         "                    refused.append(exc)",
         "            except OSError:\n"
         "                pass", C_FAIL),
    # The whole H02 class is skipped on Windows by platform NAME.
    _mut("A_module_level_windows_skip", "A_SKIPWASH", T_H02,
         "_SRC = Path(ex.__file__).read_text(encoding=\"utf-8\")",
         "pytestmark = pytest.mark.skipif(os.name == \"nt\", reason=\"Windows\")\n"
         "_SRC = Path(ex.__file__).read_text(encoding=\"utf-8\")", C_SKIP),
    # The cross-platform replacements are deleted, leaving only POSIX proofs.
    _mut("A_delete_the_cross_platform_replacement", "A_SKIPWASH", T_H02,
         "    def test_a_post_acquisition_mutation_cannot_alter_the_observation(",
         "    def _disabled_a_post_acquisition_mutation_cannot_alter_the_observation(",
         C_SKIP),
    _mut("A_delete_the_behavioural_reopen_proof", "A_SKIPWASH", T_H02,
         "    def test_a_derived_parser_never_reopens_the_source_path(self, sandbox,",
         "    def _disabled_derived_parser_never_reopens_the_source_path(self, sandbox,",
         C_SKIP),
]

# ══ B · THE CAPABILITY PROBE ════════════════════════════════════════════════
#
# M68D.2 COMPATIBILITY REPAIR (M68D.2 §23). The probe's own `os.open` now asks
# for `O_BINARY` too, because it must keep opening the descriptor production
# opens (M68D.2 §5) — so the TWO mutations below that quote that line were
# RE-TARGETED to its new text, in the four places they quote it (each one's
# `find` and the head of its `replace`). Nothing else changed: same mutations,
# same mapped tests, same intended detection property. The `os.devnull` line in
# `B_probe_does_not_hold_the_descriptor`'s replacement is synthetic and stayed
# exactly as M68D.1 published it. The sharing semantics these
# capabilities measure are a property of the share mode CPython requests, which
# binary mode does not alter, so the measurements are unaffected.
MUTATIONS += [
    # The probe INFERS instead of measuring — the single thing §13 forbids.
    _mut("B_probe_infers_from_the_platform_name", "B_PROBE", CAP,
         "        fd = os.open(held, os.O_RDONLY | getattr(os, \"O_BINARY\", 0))",
         "        if sys.platform == \"win32\":\n"
         "            return\n"
         "        fd = os.open(held, os.O_RDONLY | getattr(os, \"O_BINARY\", 0))", C_PROBE),
    # An unmeasured capability defaults to AVAILABLE, so a POSIX-only fixture
    # runs on a platform that cannot stage it and fails for the wrong reason.
    _mut("B_unmeasured_capability_defaults_to_true", "B_PROBE", CAP,
         "REPLACE_OVER_OPEN_PATH = False\n"
         "UNLINK_OPEN_PATH = False",
         "REPLACE_OVER_OPEN_PATH = True\n"
         "UNLINK_OPEN_PATH = True", C_PROBE),
    # The probe reports a verdict it never obtained.
    _mut("B_probe_reports_without_measuring", "B_PROBE", CAP,
         "def _record(name: str, exc: \"BaseException | None\") -> bool:",
         "def _record(name: str, exc: \"BaseException | None\") -> bool:\n"
         "    PROBE_DIAGNOSTIC[name] = \"unknown\"\n"
         "    return True", C_PROBE),
    # The probe stops holding the descriptor production holds, so it measures
    # a question nobody asked.
    _mut("B_probe_does_not_hold_the_descriptor", "B_PROBE", CAP,
         "        fd = os.open(held, os.O_RDONLY | getattr(os, \"O_BINARY\", 0))\n"
         "        try:",
         "        fd = os.open(held, os.O_RDONLY | getattr(os, \"O_BINARY\", 0))\n"
         "        os.close(fd)\n"
         "        fd = os.open(os.devnull, os.O_RDONLY)\n"
         "        try:", C_PROBE),
    # The universal mechanism disappears, so every parametrised cross-platform
    # test collects an EMPTY set — which pytest reports as a silent skip.
    _mut("B_continuous_set_loses_the_universal_mechanism", "B_SKIPWASH", CAP,
         "    if m in (Mutation.ATOMIC_REPLACE, Mutation.IN_PLACE_REWRITE))",
         "    if m in (Mutation.ATOMIC_REPLACE,))", C_PROBE),
    # A skip reason stops citing its measurement: back to "Windows".
    _mut("B_skip_reason_loses_its_measurement", "B_PROBE", CAP,
         "WHY_NO_REPLACE = (\n"
         "    \"POSIX_CAPABILITY_TEST: this platform refuses os.replace() over a path \"",
         "WHY_NO_REPLACE = (\n"
         "    \"Windows\" if False else (\n"
         "    \"POSIX_CAPABILITY_TEST: this platform refuses os.replace() over a path \"",
         C_PROBE),
]

# ══ C · BYTE-EXPLICIT FIXTURES ══════════════════════════════════════════════
MUTATIONS += [
    # write_bytes -> write_text in the shared fixture writer: the exact
    # regression the Group B repair exists to prevent, in both suites.
    _mut("C_m68c_fixture_writer_translates_again", "C_BYTES", T_M68C,
         "    payload = text.encode(\"utf-8\")\n"
         "    target.write_bytes(payload)\n"
         "    return payload",
         "    payload = text.encode(\"utf-8\")\n"
         "    target.write_text(text)\n"
         "    return payload", C_BYTES),
    _mut("C_h02_fixture_writer_translates_again", "C_BYTES", T_H02,
         "    payload = text.encode(\"utf-8\")\n"
         "    target.write_bytes(payload)\n"
         "    return payload",
         "    payload = text.encode(\"utf-8\")\n"
         "    target.write_text(text)\n"
         "    return payload", C_BYTES),
    # The atomic-replacement helper loses its byte explicitness.
    _mut("C_atomic_replace_helper_translates", "C_BYTES", T_H02,
         "    with os.fdopen(fd, \"wb\") as handle:\n"
         "        handle.write(text.encode(\"utf-8\"))",
         "    with os.fdopen(fd, \"w\", encoding=\"utf-8\") as handle:\n"
         "        handle.write(text)", C_BYTES),
    # THE FORBIDDEN FIX (§9): normalise the bytes until the hashes match.
    _mut("C_digest_bytes_normalises_newlines", "C_BYTES", SI,
         "def digest_bytes(payload: bytes) -> str:\n",
         "def digest_bytes(payload: bytes) -> str:\n"
         "    payload = payload.replace(b\"\\r\\n\", b\"\\n\")\n", C_BYTES),
    _mut("C_the_snapshot_normalises_newlines", "C_BYTES", SI,
         "            payload = b\"\".join(chunks)\n",
         "            payload = b\"\".join(chunks).replace(b\"\\r\\n\", b\"\\n\")\n",
         C_BYTES),
    # The whole-file write stops being binary, so what lands depends on the host.
    _mut("C_whole_file_write_becomes_text_mode", "C_BYTES", SI,
         "        with os.fdopen(handle_fd, \"wb\") as handle:\n"
         "            handle.write(payload)",
         "        with os.fdopen(handle_fd, \"w\", encoding=encoding) as handle:\n"
         "            handle.write(content)", C_BYTES),
    # The receipt stops counting the bytes it wrote.
    _mut("C_receipt_ignores_the_byte_count", "C_BYTES", SI,
         "    return _receipt(WriteStatus.APPLIED, method=MutationMethod.ATOMIC_REPLACE,\n"
         "                    before=before, after=after, written=len(payload),",
         "    return _receipt(WriteStatus.APPLIED, method=MutationMethod.ATOMIC_REPLACE,\n"
         "                    before=before, after=after, written=0,", C_BYTES),
    # The non-vacuity harness is neutered, so the §10 proof certifies nothing.
    _mut("C_translation_harness_stops_translating", "C_BYTES", T_H05,
         "        with open(translated, \"w\", encoding=\"utf-8\", newline=\"\\r\\n\") as handle:",
         "        with open(translated, \"w\", encoding=\"utf-8\") as handle:", H05_CAP),
    # The byte-identity detector is pointed at nothing.
    _mut("C_byte_identity_detector_scans_nothing", "C_BYTES", T_CLOSE,
         "BYTE_IDENTITY_SUITES = (\n"
         "    \"tests/test_source_integrity_m68c.py\",",
         "BYTE_IDENTITY_SUITES = (\n", C_BYTES),
]

# ══ D · GIT MODE AND THE NEWLINE POLICY ═════════════════════════════════════
MUTATIONS += [
    # os.access comes back as the executable-mode authority.
    _mut("D_os_access_restored_as_the_authority", "D_GITMODE", CPV,
         "    modes = _git_index_modes(*mode_targets)\n"
         "    if not modes:",
         "    modes = {p: (\"100755\" if os.access(p, os.X_OK) else \"100644\")\n"
         "             for p in mode_targets}\n"
         "    if not modes:", C_GIT),
    # A failed ls-files becomes "not executable" instead of a failure.
    _mut("D_git_mode_failure_assumed_benign", "D_GITMODE", CPV,
         "    if not modes:\n"
         "        report.fail(\"PATH_INTEGRITY\",\n"
         "                    \"git ls-files --stage failed; the repository executable-mode \"\n"
         "                    \"invariant cannot be verified\")",
         "    if not modes:\n"
         "        pass", C_GIT),
    # The verifier bypasses the mode control on Windows.
    _mut("D_verifier_bypasses_itself_on_windows", "D_GITMODE", CPV,
         "GIT_MODE_EXECUTABLE = \"100755\"",
         "GIT_MODE_EXECUTABLE = \"100755\" if os.name == \"nt\" else \"100755\"",
         C_GIT),
    # The newline policy is gated on the platform, which is what it exists for.
    _mut("D_newline_policy_disabled_on_windows", "D_NEWLINE", CPV,
         "def check_newline_policy(cp: ControlPlane, report: Report) -> None:\n",
         "def check_newline_policy(cp: ControlPlane, report: Report) -> None:\n"
         "    if os.name == \"nt\":\n"
         "        return\n", C_GIT),
    # Source integrity is skipped on Windows.
    _mut("D_source_digest_skipped_on_windows", "D_NEWLINE", SI,
         "def digest_file(path: \"Path | str\") -> \"str | None\":\n",
         "def digest_file(path: \"Path | str\") -> \"str | None\":\n"
         "    if os.name == \"nt\":\n"
         "        return None\n", C_GIT),
    # The exec-bit witness is skipped for a platform NAME instead of a
    # measured capability.
    _mut("D_exec_bit_skipped_by_platform_name", "D_GITMODE", T_H05,
         "    @pytest.mark.skipif(not EXEC_BIT_OBSERVABLE, reason=WHY_NO_EXEC_BIT)",
         "    @pytest.mark.skipif(os.name == \"nt\", reason=\"Windows\")", C_SKIP),
]

# ══ E · THE WINDOWS CI JOB ══════════════════════════════════════════════════
#
# M68D.2 COMPATIBILITY REPAIR (M68D.2 §23). The M68D.2 suite was appended to
# the Windows job's pytest invocation, so the closure suite is no longer the
# LAST continued line and now carries a trailing backtick. The anchor below was
# RE-TARGETED to match, exactly as `E_h02_suite_removed_from_ci` already did.
# Same mutation, same mapped test, same intended detection property.
MUTATIONS += [
    # The closure suite is removed from the Windows runner.
    _mut("E_closure_suite_removed_from_ci", "E_CI", WORKFLOW,
         "            jarvis/tests/test_windows_portability_closure_m68d1.py `\n",
         "", C_CI),
    # The H02 suite is removed from the Windows runner.
    _mut("E_h02_suite_removed_from_ci", "E_CI", WORKFLOW,
         "            jarvis/tests/test_trust_boundary_m68d_h02_source_identity.py `\n",
         "", C_CI),
    # The job becomes advisory.
    _mut("E_windows_job_becomes_advisory", "E_CI", WORKFLOW,
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n",
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n"
         "    continue-on-error: true\n", C_CI),
    # A failing step is swallowed by the shell.
    _mut("E_windows_step_swallows_failure", "E_CI", WORKFLOW,
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run: python jarvis/scripts/verify_m62_control_plane.py\n",
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run: python jarvis/scripts/verify_m62_control_plane.py || true\n",
         C_CI),
    # The job makes itself pass by avoiding the condition it witnesses.
    _mut("E_windows_job_disables_autocrlf", "E_CI", WORKFLOW,
         "      - name: Report the checkout's newline configuration\n"
         "        run: |\n"
         "          git config --get core.autocrlf\n",
         "      - name: Report the checkout's newline configuration\n"
         "        run: |\n"
         "          git config --global core.autocrlf=false\n",
         C_CI),
    # A declared check goes back to a form that cannot run (exit 2 on its own
    # argument parsing) - the latent red M68D.1 found at step 8.
    _mut("E_manifest_check_invoked_without_dist", "E_CI", WORKFLOW,
         '          python scripts/check_package_manifest.py --dist "$env:RUNNER_TEMP/dist"',
         "          python scripts/check_package_manifest.py", C_CI),
    _mut("E_manifest_scanned_before_anything_is_built", "E_CI", WORKFLOW,
         '          python -m build --outdir "$env:RUNNER_TEMP/dist"\n'
         '          python scripts/check_package_manifest.py --dist "$env:RUNNER_TEMP/dist"',
         '          python scripts/check_package_manifest.py --dist "$env:RUNNER_TEMP/dist"\n'
         '          python -m build --outdir "$env:RUNNER_TEMP/dist"', C_CI),
    # The control plane verifier is dropped from the WINDOWS job. The `tests`
    # job runs the identical line, so the step NAME is what disambiguates.
    _mut("E_control_plane_verifier_removed_from_ci", "E_CI", WORKFLOW,
         "      - name: Control Plane verifier (must be PASS / 0 on Windows)\n"
         "        run: python jarvis/scripts/verify_m62_control_plane.py\n"
         "      - name: Source-integrity and portability suites\n",
         "      - name: Source-integrity and portability suites\n", C_CI),
]


def _pytest(target: str) -> "subprocess.CompletedProcess":
    return subprocess.run(  # nosec B603 - fixed argv, shell=False
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:randomly",
         "-p", "no:cacheprovider", target],
        cwd=_ROOT, capture_output=True, text=True, check=False)


def _preflight(targets: "list[str]") -> "list[str]":
    """Every mapped test must PASS, and collect at least one test, BEFORE any
    mutation. A red or skipped mapping makes every mutation look DETECTED."""
    bad: "list[str]" = []
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
    print(f"M68D.1 FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    ids = [m["id"] for m in MUTATIONS]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        print(f"DUPLICATE MUTATION IDS: {duplicate_ids}")
        print("M68D1_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68D1_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    survivors: "list[str]" = []
    anchor_errors: "list[str]" = []
    detected = 0
    print(f"\nMUTATING — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']:46s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:46s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                marker = ("EXPLAINED" if m["id"] in EXPLAINED_SURVIVORS
                          else "NO TEST FAILED")
                print(f"  [SURVIVOR] {m['id']:46s} ({m['cat']}) — {marker}")
        finally:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original)

    unexplained = [s for s in survivors if s not in EXPLAINED_SURVIVORS]
    explained = [s for s in survivors if s in EXPLAINED_SURVIVORS]
    print(f"\n{'=' * 72}")
    print(f"mutations:              {total}")
    print(f"detected:               {detected}")
    print(f"explained survivors:    {len(explained)}  {explained if explained else ''}")
    print(f"unexplained survivors:  {len(unexplained)}  "
          f"{unexplained if unexplained else ''}")
    print(f"anchor errors:          {len(anchor_errors)}  "
          f"{anchor_errors if anchor_errors else ''}")
    for sid in explained:
        print(f"  EXPLAINED {sid}: {EXPLAINED_SURVIVORS[sid]}")
    ok = not unexplained and not anchor_errors
    print(f"M68D1_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(unexplained)} unexplained survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
