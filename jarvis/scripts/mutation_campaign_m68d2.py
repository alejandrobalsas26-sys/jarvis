"""scripts/mutation_campaign_m68d2.py — V69 M68D.2: the falsification campaign.

For each mutation:
  0. (once, up front) run every MAPPED test UNMUTATED and require it to PASS,
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

Step 0 is not ceremony. A mapped test that is RED — or merely SKIPPED — before
any mutation reports DETECTED for every mutation and turns the whole campaign
into a green rubber stamp. Exit 2 means "could not be evaluated".

WHAT M68D.2 DEPENDS ON, AND SO WHAT IS MUTATED
==============================================
Three controls, and the mutations are grouped to match:

GROUP A — the source descriptor is opened BINARY, and the digest is over the
bytes that came back. Mutated in production: the flag derivation, the call site
that uses it, and every forbidden "fix" that would make the hashes agree by
changing the bytes instead of reading them properly.

GROUP B — stability is witnessed at the BYTE level, and metadata is kept as the
complementary layer that catches ABA. Mutated in production: each witness, each
conjunct, the recheck, and the identity gate that must refuse an unstable
observation. Mutated in test code too, because a race harness that cannot show
its adversary ran is as decorative as a deleted production check.

GROUP C — Windows test governance. A skip budget bounded by the COLLECTION, an
explicit mandatory-invariant inventory, and a blocking CI job that runs all of
it. These live in test code and in the workflow, which is exactly where the
M68D.1 failure lived: an assertion no result could satisfy blocked a real
runner for a reason unrelated to the property it defended.

ONE MUTATION HERE IS DELIBERATELY PLATFORM-BLIND
================================================
`C_restore_the_impossible_ratio` reintroduces ``passed >= 8 * skipped``. On a
host with few capability skips that assertion PASSES, so no behavioural test
can catch it — which is why M68D.2 added a STRUCTURAL detector for the shape,
and why this mutation is mapped to it. A control that only fails on one
platform is a control that one platform does not have.

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
CAP = "tests/_test_support/platform_capabilities.py"
CENSUS = "tests/_test_support/suite_census.py"
T_H02 = "tests/test_trust_boundary_m68d_h02_source_identity.py"
T_CLOSE = "tests/test_windows_portability_closure_m68d1.py"
T_M68D2 = "tests/test_cross_platform_source_integrity_m68d2.py"
WORKFLOW = "../.github/workflows/ci.yml"

# ── mapped tests ─────────────────────────────────────────────────────────────
A_BYTES = f"{T_M68D2}::TestBinaryByteIdentity"
A_SIM = f"{T_M68D2}::TestTheSimulatorIsFaithful"
B_STABLE = f"{T_M68D2}::TestByteLevelStability"
B_CONTROLS = f"{T_M68D2}::TestPositiveControls"
B_THREAT = f"{T_M68D2}::TestTheThreatModelIsStated"
B_INJECT = f"{T_M68D2}::TestFailureInjection"
B_ABSENT = f"{T_M68D2}::TestAbsentControls"
C_SKIPGOV = f"{T_M68D2}::TestSkipGovernance"
C_APPEND = f"{T_M68D2}::TestTheAppendDefectIsStillOpen"
#: The absent-controls for the governance that lives in TEST code. Added after
#: the first campaign run: seven mutations of those controls survived, because
#: nothing was watching the watchers.
C_CONTROLS = f"{T_M68D2}::TestTheGovernanceControlsThemselvesAreIntact"

H02_RACE = f"{T_H02}::TestSnapshotRaces"
H02_SNAP = f"{T_H02}::TestSnapshotCoherence"
CLOSE_SKIP = f"{T_CLOSE}::TestNoSkipWashing"
CLOSE_CI = f"{T_CLOSE}::TestWindowsCiStillBlocks"

#: Survivors with a MEASURED explanation. Empty is the preferred state.
EXPLAINED_SURVIVORS: "dict[str, str]" = {
    # MEASURED, and the explanation is arithmetic rather than opinion:
    # `verified_sha == digest.hexdigest()` compares sha256 over the SAME byte
    # stream, and two byte strings of different LENGTH cannot share a sha256
    # digest short of a collision. So `verified_total == total` is strictly
    # IMPLIED by the conjunct above it, and no behaviour exists that the
    # length check alone can reject. It is kept as cheap defence in depth
    # against a future bug in `_descriptor_content`'s own accounting — and the
    # implication is itself pinned, by
    # `TestFailureInjection::test_a_short_read_is_not_a_whole_observation`,
    # which rejects a truncated read through the digest comparison.
    "B_drop_the_verified_length":
        "the digest comparison strictly implies the length comparison, so no "
        "observable behaviour depends on the length conjunct alone; kept as "
        "defence in depth and documented in the milestone's §3",
}


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: "list[dict]" = []

# ══ A · BINARY IDENTITY ═════════════════════════════════════════════════════
MUTATIONS += [
    # THE Group A regression, restored exactly: a text-mode source descriptor.
    _mut("A_source_open_drops_the_derivation", "A_BINARY", SI,
         "        fd = os.open(str(resolved), _source_open_flags())",
         "        fd = os.open(str(resolved), os.O_RDONLY)", A_BYTES),
    # The derivation stops consulting the platform, so it is always POSIX.
    _mut("A_remove_the_binary_flag", "A_BINARY", SI,
         "    return os.O_RDONLY | getattr(os, _O_BINARY_FLAG, 0)",
         "    return os.O_RDONLY", A_BYTES),
    # The flag is asked for under a name no platform publishes: silently 0.
    _mut("A_binary_flag_looked_up_under_a_dead_name", "A_BINARY", SI,
         '_O_BINARY_FLAG = "O_BINARY"',
         '_O_BINARY_FLAG = "O_BINARY_NOT_REALLY"', A_BYTES),
    # The forbidden fix: normalise until the hashes agree. Every byte seal in
    # the repository becomes decorative.
    _mut("A_normalise_crlf_inside_the_source_digest", "A_BINARY", SI,
         "            digest.update(payload)",
         '            digest.update(payload.replace(b"\\r\\n", b"\\n"))', A_BYTES),
    # The digest is taken over DECODED text rather than the source bytes.
    _mut("A_hash_decoded_text_instead_of_source_bytes", "A_BINARY", SI,
         "            payload = b\"\".join(chunks)\n"
         "            digest.update(payload)",
         "            payload = b\"\".join(chunks)\n"
         "            digest.update(payload.decode(\"utf-8\", \"ignore\")\n"
         "                          .encode(\"utf-8\"))", A_BYTES),
    # The captured size stops describing the capture.
    _mut("A_wrong_byte_size", "A_BINARY", SI,
         "            total = len(payload)",
         "            total = max(len(payload) - 1, 0)", A_BYTES),
    # The chunked (stream-mode) digest normalises instead.
    _mut("A_stream_mode_digest_normalises", "A_BINARY", SI,
         "                digest.update(chunk)\n"
         "                total += len(chunk)\n"
         "        # BYTE-LEVEL stability witness",
         "                digest.update(chunk.replace(b\"\\r\\n\", b\"\\n\"))\n"
         "                total += len(chunk)\n"
         "        # BYTE-LEVEL stability witness", A_BYTES),
    # `digest_file` — the path-based helper — normalises.
    _mut("A_digest_file_normalises", "A_BINARY", SI,
         "                digest.update(chunk)\n"
         "        return digest.hexdigest()",
         "                digest.update(chunk.replace(b\"\\r\\n\", b\"\\n\"))\n"
         "        return digest.hexdigest()", A_BYTES),
    # The non-vacuity harness is neutered: the simulator always behaves as if
    # binary mode were available, so GROUP A would pass with the repair gone.
    _mut("A_bypass_the_binary_mode_test", "A_BINARY", T_M68D2,
         "    if binary_available:\n        os.O_BINARY = WIN_O_BINARY",
         "    if True:\n        os.O_BINARY = WIN_O_BINARY", A_SIM),
    # The simulator stops translating at all, which would make every Group A
    # assertion vacuously true.
    _mut("A_simulator_stops_translating", "A_BINARY", T_M68D2,
         '        return data.replace(b"\\r\\n", b"\\n")',
         "        return data", A_SIM),
]

# ══ B · STABILITY ═══════════════════════════════════════════════════════════
MUTATIONS += [
    # THE Group B regression: metadata-only stability.
    _mut("B_metadata_only_stability_restored", "B_STABILITY", SI,
         "            and before.st_size == after.st_size == total\n"
         "            and verified_sha == digest.hexdigest()\n"
         "            and verified_total == total\n"
         "        )",
         "            and before.st_size == after.st_size == total\n"
         "        )", B_STABLE),
    # The byte witness is replaced by a self-certifying tautology.
    _mut("B_byte_witness_self_certifies", "B_STABILITY", SI,
         "        verified_sha, verified_total = _descriptor_content(fd)",
         "        verified_sha, verified_total = digest.hexdigest(), total",
         B_STABLE),
    # The witness digest conjunct alone is dropped.
    _mut("B_accept_same_size_same_mtime_rewrite", "B_STABILITY", SI,
         "            and verified_sha == digest.hexdigest()",
         "            and True", B_STABLE),
    # The witness LENGTH conjunct alone is dropped.
    _mut("B_drop_the_verified_length", "B_STABILITY", SI,
         "            and verified_total == total",
         "            and True", B_INJECT),
    # `recheck()` goes back to metadata-only, so the derived path's two passes
    # over one descriptor can disagree unnoticed.
    _mut("B_recheck_skips_the_byte_witness", "B_STABILITY", SI,
         "                or verified_sha != self.sha256\n"
         "                or verified_total != self.size_bytes",
         "                or False", B_STABLE),
    # The verification pass reopens the MUTABLE PATH instead of reading the
    # held descriptor, so identity comes from a different observation again.
    _mut("B_reopen_the_path_for_final_identity", "B_STABILITY", SI,
         "        verified_sha, verified_total = _descriptor_content(fd)\n"
         "        after = os.fstat(fd)",
         "        with open(str(resolved), \"rb\") as _re:\n"
         "            _rb = _re.read()\n"
         "        verified_sha, verified_total = (\n"
         "            hashlib.sha256(_rb).hexdigest(), len(_rb))\n"
         "        after = os.fstat(fd)", B_STABLE),
    # The metadata layer is dropped instead, which loses ABA detection.
    _mut("B_drop_the_metadata_layer", "B_STABILITY", SI,
         "            and before.st_mtime_ns == after.st_mtime_ns",
         "            and True", H02_RACE),
    # One fstat cannot bracket an observation.
    _mut("B_single_fstat", "B_STABILITY", SI,
         "        after = os.fstat(fd)\n"
         "        stable = (",
         "        after = before\n"
         "        stable = (", H02_RACE),
    # A detected instability is FORGOTTEN by the recheck — "hide it behind a
    # retry" in its simplest form.
    _mut("B_recheck_forgets_a_detected_mutation", "B_STABILITY", SI,
         "                or verified_total != self.size_bytes):\n"
         "            self.stable = False",
         "                or verified_total != self.size_bytes):\n"
         "            self.stable = self.stable", B_STABLE),
    # An unstable observation hands out a precondition again.
    _mut("B_unstable_still_states_a_precondition", "B_STABILITY", SI,
         "    sha = snapshot.sha256 if snapshot.stable else None",
         "    sha = snapshot.sha256", B_STABLE),
    # The descriptor is released before the snapshot is published, so the
    # identity describes a path rather than an observation.
    _mut("B_close_the_descriptor_before_publishing", "B_STABILITY", SI,
         "        snapshot = SourceSnapshot(",
         "        os.close(fd)\n"
         "        snapshot = SourceSnapshot(", B_STABLE),
    # The race harness fires its adversary where it cannot overlap the read.
    _mut("B_race_harness_never_overlaps", "B_STABILITY", T_M68D2,
         "        if not fired:\n"
         "            fired.append(1)\n"
         "            witness.writer_attempted = True",
         "        if False:\n"
         "            fired.append(1)\n"
         "            witness.writer_attempted = True", B_STABLE),
    # A refused adversary is counted as successful coverage.
    _mut("B_witness_swallows_the_writer_exception", "B_STABILITY", T_M68D2,
         "        assert not self.writer_errors, (",
         "        assert True or self.writer_errors, (", B_ABSENT),
    # The witness stops checking that the bytes on disk actually changed.
    _mut("B_witness_drops_the_mutation_check", "B_STABILITY", T_M68D2,
         "        assert self.mutation_observed, \\\n"
         "            \"NON-VACUITY: the bytes on disk never changed\"",
         "        assert True, \\\n"
         "            \"NON-VACUITY: the bytes on disk never changed\"",
         B_ABSENT),
]

# ══ C · WINDOWS TEST GOVERNANCE ═════════════════════════════════════════════
MUTATIONS += [
    # THE Group C regression: the unsatisfiable ratio, restored. Caught only
    # by the structural detector — see the module docstring.
    _mut("C_restore_the_impossible_ratio", "C_SKIPWASH", T_CLOSE,
         "        budget = skip_budget(result.collected)\n"
         "        assert result.skipped <= budget, (",
         "        assert result.passed >= 8 * result.skipped\n"
         "        budget = skip_budget(result.collected)\n"
         "        assert result.skipped <= budget, (", C_SKIPGOV),
    # The budget stops being bounded by the collection and can exceed it.
    _mut("C_budget_can_exceed_the_collection", "C_SKIPWASH", CENSUS,
         "    return max(1, min(collected, math.floor(collected * fraction)))",
         "    return collected * 8", C_SKIPGOV),
    # The budget becomes so generous that a hollowed-out suite passes.
    _mut("C_budget_permits_hollowing_the_suite", "C_SKIPWASH", CENSUS,
         "DEFAULT_SKIP_FRACTION = 0.25",
         "DEFAULT_SKIP_FRACTION = 0.99", C_SKIPGOV),
    # A failure is allowed to be read as a skip again.
    _mut("C_failures_may_become_skips", "C_SKIPWASH", T_CLOSE,
         "        assert result.failed == 0 and result.errors == 0, (",
         "        assert True or result.failed == 0, (", C_CONTROLS),
    # The outcomes stop having to account for every collected case.
    _mut("C_collection_need_not_be_accounted_for", "C_SKIPWASH", CENSUS,
         "        return (self.passed + self.failed + self.skipped + self.errors\n"
         "                + self.xfailed + self.xpassed)",
         "        return self.passed + self.skipped", C_CONTROLS),
    # A mandatory cross-platform invariant leaves the inventory.
    _mut("C_remove_a_mandatory_invariant", "C_SKIPWASH", T_M68D2,
         '        "TestByteLevelStability::test_that_race_cannot_authorise_a_write",\n',
         "", C_SKIPGOV),
    # The whole M68D.2 inventory entry disappears.
    _mut("C_remove_the_ctrl_z_invariant", "C_SKIPWASH", T_M68D2,
         '        "TestBinaryByteIdentity::test_the_ctrl_z_truncation_is_closed",\n',
         "", C_SKIPGOV),
    # H02 is skipped wholesale on the platform that found the defects.
    _mut("C_broadly_skip_h02", "C_SKIPWASH", T_H02,
         "class TestSnapshotCoherence:",
         "pytestmark = pytest.mark.skipif(True, reason=\"portability\")\n\n\n"
         "class TestSnapshotCoherence:", CLOSE_SKIP),
    # A skip reason stops citing the measurement that produced it.
    _mut("C_drop_skip_reason_evidence", "C_SKIPWASH", CAP,
         "    \"an unobservable source is asserted everywhere by the open()-failure \"\n"
         "    f\"injection instead. Measured: {_D('mode_bits_remove_read')}\")",
         "    \"an unobservable source is asserted everywhere.\")", C_CONTROLS),
    # The M68D.2 suite is removed from the Windows job.
    _mut("C_remove_m68d2_from_the_windows_job", "C_SKIPWASH", WORKFLOW,
         "            jarvis/tests/test_cross_platform_source_integrity_m68d2.py",
         "            jarvis/tests/test_windows_portability_closure_m68d1.py",
         CLOSE_CI),
    # The Windows job becomes advisory.
    _mut("C_windows_job_becomes_advisory", "C_SKIPWASH", WORKFLOW,
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)",
         "  windows-portability:\n"
         "    continue-on-error: true\n"
         "    name: Windows portability (3.11, blocking)", CLOSE_CI),
    # The structural ratio detector is blinded. RE-TARGETED after the first
    # campaign run: the detector was factored into ONE shared helper precisely
    # because two copies let this mutation blind the scan while leaving its
    # non-vacuity proof green.
    _mut("C_blind_the_ratio_detector", "C_SKIPWASH", T_M68D2,
         "            if any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult)\n"
         "                   for sub in ast.walk(node)):",
         "            if False:", C_SKIPGOV),
    # The deferred append defect is quietly declared closed.
    _mut("C_append_defect_pin_is_blinded", "C_SKIPWASH", T_M68D2,
         "        assert appends, (",
         "        assert True or appends, (", C_CONTROLS),
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
    print(f"M68D.2 FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    ids = [m["id"] for m in MUTATIONS]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        print(f"DUPLICATE MUTATION IDS: {duplicate_ids}")
        print("M68D2_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    missing = [f"{m['id']}: {m['file']} does not exist" for m in MUTATIONS
               if not os.path.exists(os.path.join(_ROOT, m["file"]))]
    if missing:
        for line in missing:
            print(f"  {line}")
        print("M68D2_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68D2_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
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
    print(f"M68D2_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(unexplained)} unexplained survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
