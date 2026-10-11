"""scripts/mutation_campaign_m68d3.py — V69 M68D.3: the simulator's own falsification.

WHY A SECOND, NARROWER CAMPAIGN
===============================
M68D.2's campaign mutated PRODUCTION and the governance that surrounds it, and
it passed 36/37 on Linux. It could not have caught what broke: the defect was
in the TEST HARNESS, and it only existed on a host the campaign never ran on.

CI run **38007992655** on candidate ``a0c6f08`` is the evidence. 10/10 Linux
jobs SUCCESS; the real Windows runner returned 376 passed / 20 failed /
17 skipped, with all 20 failures inside ``TestBinaryByteIdentity`` and not one
of them a production defect. The simulator stripped ``O_BINARY`` and delegated
to the real ``os.open``, so on a host whose C runtime implements text mode it
opened an ACTUAL text-mode descriptor: CRLF collapsed, the read stopped at the
first Ctrl-Z, and a correct 10240-byte binary production read was observed as
**26 bytes**.

So this campaign mutates THE HARNESS: the capability probe, the branch
selection, the shim, the descriptor lifecycle, the restoration guarantees, the
non-vacuity assertions and the CI wiring that makes any of it run on Windows.
Every mutation is one way to put CI 38007992655 back, or to make the suite stop
noticing that it had.

ONLY TEST FILES AND THE WORKFLOW ARE MUTATED HERE
=================================================
``core/source_integrity.py`` is BYTE-IDENTICAL to generation 70 and M68D.3 is
scoped not to touch it (§5, §15). Production mutations stay in
``mutation_campaign_m68d2.py``, which still runs.

The bar is the repository's: unique exact anchors, mapped tests, effective
mutations, non-vacuous detection, 0 unexplained survivors, 0 anchor errors. A
mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR — a property
with no test behind it — and is never resolved by deleting the mutation.

Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
T_M68D2 = "tests/test_cross_platform_source_integrity_m68d2.py"
WORKFLOW = "../.github/workflows/ci.yml"

# ── mapped tests ─────────────────────────────────────────────────────────────
#: The §14 detector: the native branch, the defective shims, the probe.
DOUBLE = f"{T_M68D2}::TestTheRuntimeIsNotDoubleSimulated"
#: PROOF A / PROOF C — the binary positive controls.
POSITIVE = f"{T_M68D2}::TestBinaryByteIdentity"
#: PROOF B / PROOF D — the text-mode negative controls.
NEGATIVE = f"{T_M68D2}::TestTheTextRuntimeReallyTranslates"
#: §17 — the accounting fault the length conjunct exists to reject.
INJECT = f"{T_M68D2}::TestFailureInjection"
#: The inventory and the skip budget that make the Windows job run all of it.
SKIPGOV = f"{T_M68D2}::TestSkipGovernance"
#: M68D.3's absent-controls: the harness's OWN assertions, scanned structurally.
#: Eleven mutations in the first run of this campaign survived because the
#: assertions they deleted had nothing above them.
CONTROLS = f"{T_M68D2}::TestTheSimulatorControlsThemselvesAreIntact"
#: The blocking Windows job itself, asserted from the M68D.1 closure suite.
CLOSE_CI = ("tests/test_windows_portability_closure_m68d1.py"
            "::TestWindowsCiStillBlocks")

#: EMPTY, and it must stay that way. A survivor here is a harness property with
#: nothing behind it, which is precisely the shape of the M68D.2 defect.
EXPLAINED_SURVIVORS: "dict[str, str]" = {}


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: "list[dict]" = []

# ══ D · BRANCH SELECTION — the defect itself ════════════════════════════════
MUTATIONS += [
    # THE M68D.2 DEFECT, restored: the shim runs whatever the host does, so a
    # native text CRT is handed a text-mode descriptor for a binary read.
    _mut("D_always_enable_the_simulator", "D_BRANCH", T_M68D2,
         "    if NATIVE.translates:\n"
         "        mode = TextRuntime(native=True, synthetic=False,",
         "    if False:\n"
         "        mode = TextRuntime(native=True, synthetic=False,", DOUBLE),
    # The mirror image: the simulator never runs, so a host with no text mode
    # has no way to reach the Windows branch and every Group A test is vacuous.
    _mut("D_always_disable_the_simulator", "D_BRANCH", T_M68D2,
         "    if NATIVE.translates:\n"
         "        mode = TextRuntime(native=True, synthetic=False,",
         "    if True:\n"
         "        mode = TextRuntime(native=True, synthetic=False,", NEGATIVE),
    # The binary bit is stripped on the NATIVE path — the exact line of code
    # that produced the 20 failures, reintroduced in its new home.
    _mut("D_strip_the_binary_flag_on_a_native_host", "D_BRANCH", T_M68D2,
         "        try:\n"
         "            if not binary_available and had:",
         "        try:\n"
         "            os.open = lambda p, f, *a, **k: _REAL_OPEN(\n"
         "                p, f & ~WIN_O_BINARY, *a, **k)\n"
         "            if not binary_available and had:", DOUBLE),
    # The native branch gets a synthetic transform layered on top of the real
    # CRT: both translations active, which is double modelling.
    _mut("D_double_transform_the_native_read", "D_BRANCH", T_M68D2,
         "        had = hasattr(os, \"O_BINARY\")\n"
         "        previous = getattr(os, \"O_BINARY\", None)\n"
         "        try:\n"
         "            if not binary_available and had:",
         "        had = hasattr(os, \"O_BINARY\")\n"
         "        previous = getattr(os, \"O_BINARY\", None)\n"
         "        try:\n"
         "            os.read = lambda fd, n: _REAL_READ(fd, n).replace(\n"
         "                b\"\\r\\n\", b\"\\n\")\n"
         "            if not binary_available and had:", DOUBLE),
]

# ══ E · THE CAPABILITY PROBE ════════════════════════════════════════════════
MUTATIONS += [
    # The probe stops measuring and guesses from a platform name instead —
    # the M68D.1 §13 prohibition, and the thing that would have been "simpler".
    _mut("E_probe_guesses_from_a_platform_name", "E_PROBE", T_M68D2,
         "        unflagged = _read_whole(target, os.O_RDONLY)\n"
         "        if unflagged == NATIVE_PROBE_BYTES:",
         "        unflagged = _read_whole(target, os.O_RDONLY)\n"
         "        if not str(type(os.sep)) or os.sep == \"/\":\n"
         "            return NativeTextRuntime(BYTE_EXACT, unflagged=unflagged)\n"
         "        if unflagged == NATIVE_PROBE_BYTES:", DOUBLE),
    # A probe FAILURE becomes an optimistic capability answer (§8's forbidden
    # resolution) instead of a fail-closed verdict.
    _mut("E_probe_failure_becomes_byte_exact", "E_PROBE", T_M68D2,
         "    except OSError as exc:\n"
         "        # EXPLICIT (§8). Not a quiet `except Exception: pass` that would hand\n"
         "        # back an optimistic default.\n"
         "        return NativeTextRuntime(\n"
         "            f\"FAILED: {type(exc).__name__} errno={getattr(exc, 'errno', None)}\"\n"
         "            f\" winerror={getattr(exc, 'winerror', None)}\")",
         "    except OSError:\n"
         "        return NativeTextRuntime(BYTE_EXACT)", DOUBLE),
    # An unmeasured host is allowed to produce a runtime anyway.
    _mut("E_unmeasured_host_runs_anyway", "E_PROBE", T_M68D2,
         "    if not NATIVE.measured:",
         "    if False:", DOUBLE),
    # The probe stops checking that the EXPLICIT-BINARY descriptor is exact, so
    # a host where binary mode is also broken is reported as repairable.
    _mut("E_probe_skips_the_binary_descriptor_check", "E_PROBE", T_M68D2,
         "        if binary != NATIVE_PROBE_BYTES:",
         "        if False:", DOUBLE),
    # The probe's own fixture is no longer verified byte-exact on disk, so a
    # translating WRITE path would silently invalidate every measurement.
    _mut("E_probe_fixture_is_not_verified", "E_PROBE", T_M68D2,
         "        if target.read_bytes() != NATIVE_PROBE_BYTES:",
         "        if False:", DOUBLE),
]

# ══ F · THE SHIM AND ITS DESCRIPTOR LIFECYCLE ═══════════════════════════════
MUTATIONS += [
    # The Ctrl-Z half of the negative control disappears, so a truncating
    # runtime is no longer modelled at all.
    _mut("F_remove_the_ctrl_z_negative_control", "F_SHIM", T_M68D2,
         "        cut = data.find(b\"\\x1a\")\n"
         "        if cut != -1:\n"
         "            data = data[:cut]\n"
         "        data = data.replace(b\"\\r\\n\", b\"\\n\")",
         "        data = data.replace(b\"\\r\\n\", b\"\\n\")", NEGATIVE),
    # The descriptor lifecycle hole, restored: a closed text fd keeps its mark,
    # and the next open inherits the translation whatever flags it asked for.
    # MEASURED while building this repair — it reported the probe's own binary
    # descriptor as translating.
    _mut("F_leak_the_text_descriptor_marking", "F_SHIM", T_M68D2,
         "        text_fds.discard(fd)\n"
         "        return real_close(fd)",
         "        return real_close(fd)", DOUBLE),
    # `os.read` is left patched after the block exits.
    _mut("F_leak_the_patched_read", "F_SHIM", T_M68D2,
         "        os.open, os.read, os.close = real_open, real_read, real_close\n"
         "        if had:",
         "        os.open, os.close = real_open, real_close\n"
         "        if had:", NEGATIVE),
    # `os.open` is left patched after the block exits.
    _mut("F_leak_the_patched_open", "F_SHIM", T_M68D2,
         "        os.open, os.read, os.close = real_open, real_read, real_close\n"
         "        if had:",
         "        os.read, os.close = real_read, real_close\n"
         "        if had:", NEGATIVE),
    # `os.O_BINARY` is never restored, so every later test silently runs with
    # a binary mode the host does not have.
    _mut("F_never_restore_the_binary_attribute", "F_SHIM", T_M68D2,
         "        os.open, os.read, os.close = real_open, real_read, real_close\n"
         "        if had:\n"
         "            os.O_BINARY = previous\n"
         "        elif hasattr(os, \"O_BINARY\"):\n"
         "            del os.O_BINARY",
         "        os.open, os.read, os.close = real_open, real_read, real_close",
         NEGATIVE),
    # Restoration stops being guaranteed on an exceptional exit: the `finally`
    # becomes an `else`, so a failing test poisons everything after it.
    _mut("F_no_restoration_after_an_exception", "F_SHIM", T_M68D2,
         "        os.open, os.read, os.close = win_open, win_read, win_close\n"
         "        yield mode\n"
         "    finally:",
         "        os.open, os.read, os.close = win_open, win_read, win_close\n"
         "        yield mode\n"
         "    else:", DOUBLE),
    # The simulated binary bit is pinned to a value the host may already use,
    # so the shim can strip a flag the host acts on (§10).
    _mut("F_simulated_bit_may_alias_a_host_flag", "F_SHIM", T_M68D2,
         "SIMULATED_O_BINARY = _free_flag_bit()",
         "SIMULATED_O_BINARY = os.O_RDONLY | 1", DOUBLE),
]

# ══ G · NON-VACUITY AND THE ASSERTIONS THEMSELVES ═══════════════════════════
MUTATIONS += [
    # The byte equality assertion — the whole point of Group A — is dropped.
    _mut("G_remove_the_byte_equality_assertion", "G_VACUITY", T_M68D2,
         "                assert snap.payload() == body, \"the snapshot changed the bytes\"\n"
         "                assert snap.size_bytes == len(body), \"the byte count was shortened\"",
         "                assert snap.size_bytes >= 0", CONTROLS),
    # A 26-byte observation is accepted as a complete 10240-byte source: the
    # exact wrong answer both real runners produced.
    _mut("G_accept_twenty_six_as_a_complete_source", "G_VACUITY", T_M68D2,
         "                assert snap.size_bytes == 10240, \\\n"
         "                    f\"the read stopped at a Ctrl-Z: {snap.size_bytes} bytes\"",
         "                assert snap.size_bytes in (26, 10240)", CONTROLS),
    # The branch-liveness check becomes a no-op, so a test that silently took
    # the wrong branch — or no branch — still reports success.
    _mut("G_branch_liveness_check_is_blinded", "G_VACUITY", T_M68D2,
         "        assert self.native != self.synthetic, \\\n"
         "            \"the runtime is neither native nor synthetic, or claims to be both\"",
         "        return", CONTROLS),
    # The native branch is no longer required to leave `os` alone, which is
    # what makes "native" mean "the host did it".
    _mut("G_native_branch_may_install_a_shim", "G_VACUITY", T_M68D2,
         "            assert self.opens == 0 and self.translations == 0, \\\n"
         "                \"the native branch installed a shim, which is double simulation\"",
         "            pass", CONTROLS),
    # The negative control stops having to show that anything was corrupted.
    _mut("G_negative_control_need_not_translate", "G_VACUITY", T_M68D2,
         "            assert self.translations >= 1, \\\n"
         "                \"the shim never changed a byte, so it proves nothing\"",
         "            pass", CONTROLS),
    # The source-coherence regression — the digest must describe the disk —
    # is turned off in the negative control, so a no-op runtime passes it.
    _mut("G_unrepaired_digest_need_not_differ", "G_VACUITY", T_M68D2,
         "                assert snap.sha256 != hashlib.sha256(body).hexdigest(), \\\n"
         "                    \"the unrepaired stream digest still described the disk\"",
         "                pass", CONTROLS),
    # §17's accounting fault is no longer injected, so the length conjunct
    # goes back to being an unfalsifiable claim.
    _mut("G_accounting_fault_is_not_injected", "G_VACUITY", T_M68D2,
         "            answer = (digest, total + 1)",
         "            answer = (digest, total)", INJECT),
    # The injected digest is corrupted too, which would let the DIGEST
    # conjunct do the rejecting and make the length test prove nothing.
    # The injected DIGEST is corrupted as well, so the digest conjunct would
    # do the rejecting and the length conjunct would prove nothing. Survived
    # the first run of this campaign, which is why the test now pins what the
    # witness HANDED BACK rather than what it computed.
    _mut("G_accounting_fault_corrupts_the_digest_too", "G_VACUITY", T_M68D2,
         "            answer = (digest, total + 1)",
         "            answer = (hashlib.sha256(b\"unrelated\").hexdigest(),\n"
         "                      total + 1)", INJECT),
]

# ══ H · THE INVENTORY AND THE WINDOWS JOB ═══════════════════════════════════
MUTATIONS += [
    # The native positive proof leaves the mandatory inventory, so a future
    # edit could stop collecting it on the only platform that can run it.
    _mut("H_remove_the_native_positive_from_the_inventory", "H_CI", T_M68D2,
         '        "TestTheRuntimeIsNotDoubleSimulated::test_the_native_branch_keeps_the_production_read_byte_exact",\n',
         "", CONTROLS),
    # The defective-shim detector leaves the inventory.
    _mut("H_remove_the_defect_detector_from_the_inventory", "H_CI", T_M68D2,
         '        "TestTheRuntimeIsNotDoubleSimulated::test_a_shim_that_strips_the_binary_flag_is_caught",\n',
         "", CONTROLS),
    # The negative controls leave the inventory, which is how the positives
    # become vacuous without anything turning red.
    _mut("H_remove_the_negative_control_from_the_inventory", "H_CI", T_M68D2,
         '        "TestTheTextRuntimeReallyTranslates::test_the_ctrl_z_fixture_really_is_truncated_at_twenty_six",\n',
         "", CONTROLS),
    # The M68D.2 suite is removed from the Windows job — the only place any of
    # this is measured on a host with a real text mode.
    _mut("H_remove_m68d2_from_the_windows_job", "H_CI", WORKFLOW,
         "            jarvis/tests/test_cross_platform_source_integrity_m68d2.py",
         "            jarvis/tests/test_windows_portability_closure_m68d1.py", CLOSE_CI),
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
    print(f"M68D.3 SIMULATOR FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    ids = [m["id"] for m in MUTATIONS]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        print(f"DUPLICATE MUTATION IDS: {duplicate_ids}")
        print("M68D3_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    missing = [f"{m['id']}: {m['file']} does not exist" for m in MUTATIONS
               if not os.path.exists(os.path.join(_ROOT, m["file"]))]
    if missing:
        for line in missing:
            print(f"  {line}")
        print("M68D3_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68D3_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
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
            print(f"  [ANCHOR ERR] {m['id']:50s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(mutated)
            proc = _pytest(m["test"])
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:50s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                marker = ("EXPLAINED" if m["id"] in EXPLAINED_SURVIVORS
                          else "NO TEST FAILED")
                print(f"  [SURVIVOR] {m['id']:50s} ({m['cat']}) — {marker}")
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
    print(f"M68D3_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(unexplained)} unexplained survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
