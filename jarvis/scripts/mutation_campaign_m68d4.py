"""scripts/mutation_campaign_m68d4.py — V69 M68D.4: the HOST emulator's falsification.

WHY A THIRD, NARROWER CAMPAIGN
==============================
M68D.3's campaign mutated the simulator — ``windows_text_runtime`` — and passed
28/28. It could not have caught what broke, because the defect was one layer
ABOVE everything it mutated: in ``host_with_a_text_crt``, the emulator that
exists so the simulator's *native* branch can be exercised from a POSIX host.

CI run **38160730836** on candidate ``530d2ea`` is the evidence. 10/10 Linux
jobs SUCCESS; the real Windows runner returned 425 passed / 9 failed /
17 skipped, with all 9 failures inside ``TestTheRuntimeIsNotDoubleSimulated``
and not one of them a production defect. The emulator unconditionally installed
its own CRT, and its open did ``host_open(path, flags & ~bit, ...)``. On Linux
``bit`` is a flag this host spends on nothing, so that line is bookkeeping. On
Windows ``bit`` IS the real ``os.O_BINARY``, so the capability probe measured
its own explicitly-binary descriptor as translating:

    FAILED: the explicitly-binary descriptor is not byte-exact either
    (4 of 8 bytes)

Five of the nine died on the fail-closed refusal that verdict correctly
triggered. **The refusal was right; the emulator was wrong.**

THE PROPERTY UNDER TEST: THE NO-OP RULE IS RECURSIVE
====================================================
Every layer that imitates a platform capability must ask *does the host already
provide this?* and stand down if so — the simulator, the host emulator above
it, and the §10 inverse fixture beside it. Fixing one layer by measurement does
not protect the next. So this campaign mutates the HOST layer: the stand-down
branch, the measurement that selects it, the fail-closed refusal, the
restoration guarantees, the nested regression's own assertions, the §10
negative control, and the inventory and CI wiring that make any of it run on
Windows.

Each mutation is one way to put CI 38160730836 back, or to make the suite stop
noticing that it had.

ONLY TEST FILES AND THE WORKFLOW ARE MUTATED HERE
=================================================
``core/source_integrity.py`` is BYTE-IDENTICAL to generation 71, and so is all
of ``core/`` and ``tools/``. M68D.4 is scoped not to touch production (§6).
Production mutations stay in ``mutation_campaign_m68d2.py``; the simulator's
own stay in ``mutation_campaign_m68d3.py``. Both still run, and all 229
anchors of the five preserved campaigns still resolve uniquely against this
tree — no anchor was discarded to make this one fit.

WHERE THE §12 SEMANTICS LIVE
============================
Two of §12's twelve are already covered, with their original anchors, by
``mutation_campaign_m68d3.py``: ``D_strip_the_binary_flag_on_a_native_host``
and ``D_double_transform_the_native_read`` mutate the *simulator's* native
branch. The ones here mutate the *host emulator's* native branch, which is new
code in generation 72 and was unguarded before it.

The bar is the repository's: unique exact anchors, mapped tests, effective
mutations, non-vacuous detection, 0 unexplained survivors, 0 anchor errors. A
mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR — a property
with no test behind it — and is never resolved by deleting the mutation.

NEVER run this concurrently with an authoritative suite. The campaign rewrites
tracked files in place, and `census()`-style tests spawn a nested pytest that
reads them; M68D.3 measured one bogus failure that way and wasted a run on it.

Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── files under mutation ─────────────────────────────────────────────────────
T_M68D2 = "tests/test_cross_platform_source_integrity_m68d2.py"
T_H05 = "tests/test_trust_boundary_m68d_h05_portability.py"
WORKFLOW = "../.github/workflows/ci.yml"

# ── mapped tests ─────────────────────────────────────────────────────────────
#: The behavioural detector: the nested no-op proof, the fail-closed refusal,
#: the restoration controls, and the two-sided probe contrast.
DOUBLE = f"{T_M68D2}::TestTheRuntimeIsNotDoubleSimulated"
#: The structural absent-controls: the harness's OWN assertions, scanned.
CONTROLS = f"{T_M68D2}::TestTheSimulatorControlsThemselvesAreIntact"
#: The blocking Windows job — the only place the native branch is real.
CI_JOB = f"{T_H05}::TestWindowsCiJob"

#: EMPTY, and it must stay that way. A survivor here is a harness property with
#: nothing behind it, which is precisely the shape of the M68D.3 defect.
EXPLAINED_SURVIVORS: "dict[str, str]" = {}


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: "list[dict]" = []

# ══ A · THE STAND-DOWN — the defect itself ══════════════════════════════════
MUTATIONS += [
    # THE M68D.3 DEFECT, restored verbatim: the host emulator installs its CRT
    # whatever the host does, so on a translating host it strips the real
    # O_BINARY and the probe condemns its own binary descriptor.
    _mut("A_native_host_is_always_emulated", "A_STANDDOWN", T_M68D2,
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if before.translates:\n"
         "        # THE NO-OP BRANCH (M68D.4). The host IS the translating host this",
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if False:\n"
         "        # THE NO-OP BRANCH (M68D.4). The host IS the translating host this",
         DOUBLE),
    # The mirror image: the emulator ALWAYS stands down, so a byte-exact host
    # never gets the translating runtime the native proofs need and every one
    # of them becomes vacuous instead of red.
    _mut("A_the_emulator_always_stands_down", "A_STANDDOWN", T_M68D2,
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if before.translates:\n"
         "        # THE NO-OP BRANCH (M68D.4). The host IS the translating host this",
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if True:\n"
         "        # THE NO-OP BRANCH (M68D.4). The host IS the translating host this",
         DOUBLE),
    # The measurement runs and is then DISCARDED for a constant — the §8
    # prohibition, in the one place generation 71 had no guard at all.
    _mut("B_the_native_capability_is_ignored", "B_MEASURE", T_M68D2,
         "    # MEASURE FIRST (§5). Nothing is patched before the verdict is known, so\n"
         "    # the measurement can never be a measurement of this function's own shim.\n"
         "    before = measure_native_text_runtime()",
         "    # MEASURE FIRST (§5). Nothing is patched before the verdict is known, so\n"
         "    # the measurement can never be a measurement of this function's own shim.\n"
         "    before = measure_native_text_runtime()\n"
         "    before = NativeTextRuntime(BYTE_EXACT, unflagged=before.unflagged)",
         DOUBLE),
    # The second measurement — of the host the emulator actually PRESENTED —
    # is replaced by the answer it was hoping for.
    _mut("B_the_second_measurement_is_skipped", "B_MEASURE", T_M68D2,
         "        # MEASURED, not asserted: the emulated host is put through the same\n"
         "        # probe as a real one, and whatever it answers is what gets installed.\n"
         "        NATIVE = measure_native_text_runtime()",
         "        # MEASURED, not asserted: the emulated host is put through the same\n"
         "        # probe as a real one, and whatever it answers is what gets installed.\n"
         "        NATIVE = NativeTextRuntime(\n"
         "            NATIVE_TEXT, unflagged=b\"A\\nB\\n\",\n"
         "            binary=NATIVE_PROBE_BYTES)",
         CONTROLS),
    # A failed capability measurement is accepted as a valid one, so an
    # unmeasurable host produces a verdict and a pass instead of a refusal.
    _mut("H_a_failed_probe_is_accepted_as_valid", "H_FAILCLOSED", T_M68D2,
         "    CRT_EMULATOR.entries += 1\n"
         "    # MEASURE FIRST (§5). Nothing is patched before the verdict is known, so\n"
         "    # the measurement can never be a measurement of this function's own shim.\n"
         "    before = measure_native_text_runtime()\n"
         "    if not before.measured:",
         "    CRT_EMULATOR.entries += 1\n"
         "    # MEASURE FIRST (§5). Nothing is patched before the verdict is known, so\n"
         "    # the measurement can never be a measurement of this function's own shim.\n"
         "    before = measure_native_text_runtime()\n"
         "    if False:",
         DOUBLE),
]

# ══ C · THE NATIVE BRANCH TOUCHES THE RUNTIME IT PROMISED NOT TO ════════════
MUTATIONS += [
    # "Do not change the host's O_BINARY presence or value" (§5), broken: the
    # stand-down strips the real binary mode production depends on.
    _mut("C_the_binary_flag_is_unconditionally_stripped", "C_NATIVE_PATCHES",
         T_M68D2,
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            NATIVE = before",
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            if hasattr(os, \"O_BINARY\"):\n"
         "                del os.O_BINARY\n"
         "            NATIVE = before",
         DOUBLE),
    # "Do not alter os.open" (§5), broken with the exact line of CI
    # 38160730836, moved into its new home on the stand-down path.
    _mut("F_the_native_branch_patches_os_open", "C_NATIVE_PATCHES", T_M68D2,
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            NATIVE = before",
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            os.open = lambda p, f, *a, **k: _REAL_OPEN(\n"
         "                p, f & ~WIN_O_BINARY, *a, **k)\n"
         "            NATIVE = before",
         DOUBLE),
    # "Do not alter os.read" (§5), broken HARMLESSLY — a pass-through wrapper
    # that changes no byte. Only the IDENTITY assertion can see this one, so
    # it is the mutation that proves the identity check is load-bearing and
    # not decoration on top of the byte comparisons.
    _mut("G_the_native_branch_replaces_os_read", "C_NATIVE_PATCHES", T_M68D2,
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            NATIVE = before",
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            os.read = lambda fd, n: _REAL_READ(fd, n)\n"
         "            NATIVE = before",
         DOUBLE),
    # The nested layer TRANSFORMS on top of a host that already translates —
    # double modelling at the host layer, which the non-idempotent CRLF
    # witness is the only assertion able to detect.
    _mut("D_the_nested_emulator_transforms_as_well", "C_NATIVE_PATCHES",
         T_M68D2,
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            NATIVE = before\n"
         "            NATIVE_TEXT_TRANSLATION_OBSERVED = before.translates\n"
         "            yield before",
         "        CRT_EMULATOR.noops += 1\n"
         "        try:\n"
         "            NATIVE = before\n"
         "            NATIVE_TEXT_TRANSLATION_OBSERVED = before.translates\n"
         "            os.read = lambda fd, n: _REAL_READ(fd, n).replace(\n"
         "                b\"\\r\\n\", b\"\\n\")\n"
         "            yield before",
         DOUBLE),
]

# ══ I · RESTORATION AND CONTEXT ISOLATION ═══════════════════════════════════
MUTATIONS += [
    # State is not restored on the way out of the stand-down, so an exception
    # — or a clean exit — leaves the module's verdict pointing at the emulated
    # host and silently rewrites every test that follows.
    _mut("I_state_is_not_restored_after_an_exception", "I_RESTORE", T_M68D2,
         "            yield before\n"
         "        finally:\n"
         "            NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict\n"
         "        return\n"
         "\n"
         "    bit = getattr(os, \"O_BINARY\", SIMULATED_O_BINARY)",
         "            yield before\n"
         "        finally:\n"
         "            pass\n"
         "        return\n"
         "\n"
         "    bit = getattr(os, \"O_BINARY\", SIMULATED_O_BINARY)",
         DOUBLE),
    # The inner context corrupts the outer one: it restores a FAILED verdict
    # rather than the one it was handed, so the outer emulator is dead after
    # the inner block even though the outer block is still open.
    _mut("J_the_inner_context_corrupts_the_outer_one", "I_RESTORE", T_M68D2,
         "            yield before\n"
         "        finally:\n"
         "            NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED = saved_verdict\n"
         "        return\n"
         "\n"
         "    bit = getattr(os, \"O_BINARY\", SIMULATED_O_BINARY)",
         "            yield before\n"
         "        finally:\n"
         "            NATIVE = NativeTextRuntime(\"FAILED: clobbered\")\n"
         "            NATIVE_TEXT_TRANSLATION_OBSERVED = False\n"
         "        return\n"
         "\n"
         "    bit = getattr(os, \"O_BINARY\", SIMULATED_O_BINARY)",
         DOUBLE),
]

# ══ K · THE §10 NEGATIVE CONTROL ════════════════════════════════════════════
MUTATIONS += [
    # The inverse fixture always stands down, so on EVERY host both sides of
    # the probe contrast are no-ops: the test still runs, and proves nothing.
    # This is §10's hazard made concrete.
    #
    # MAPPED STRUCTURALLY, and this is the asymmetry §10 warns about rather
    # than a weak mapping. On WINDOWS the behavioural test catches this on its
    # own: the inverse fixture is the one that must install there, so standing
    # down makes `installed == 1` fail. On a POSIX host standing down is the
    # CORRECT branch, the behavioural test cannot tell the mutant from the
    # repair, and only the structural control can. The campaign runs on POSIX,
    # so the structural detector is the honest mapping here.
    _mut("K_the_probe_contrast_is_made_vacuous", "K_NEGATIVE", T_M68D2,
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if not before.translates:\n"
         "        # THE NO-OP BRANCH. This host's descriptors are already byte-exact.",
         "    saved_verdict = (NATIVE, NATIVE_TEXT_TRANSLATION_OBSERVED)\n"
         "    if True:\n"
         "        # THE NO-OP BRANCH. This host's descriptors are already byte-exact.",
         CONTROLS),
    # The inverse fixture presents a TRANSLATING descriptor instead of a
    # byte-exact one, so the two verdicts collapse into one.
    _mut("K_the_inverse_fixture_strips_instead_of_adding", "K_NEGATIVE",
         T_M68D2,
         "        return host_open(path, flags | bit, *args, **kwargs)",
         "        return host_open(path, flags & ~bit, *args, **kwargs)",
         DOUBLE),
    # The fixture stops verifying the host it just presented, so a fixture
    # that silently failed to change anything yields an unearned verdict.
    _mut("K_the_inverse_fixture_does_not_verify_itself", "K_NEGATIVE", T_M68D2,
         "        if NATIVE.outcome != BYTE_EXACT:\n"
         "            raise RuntimeError(",
         "        if False:\n"
         "            raise RuntimeError(",
         CONTROLS),
]

# ══ E · THE NESTED REGRESSION'S OWN ASSERTIONS ══════════════════════════════
MUTATIONS += [
    # The binary byte-identity assertion is deleted from the nested proof —
    # the single assertion that says the inner layer did not corrupt an
    # explicitly-binary read. This is the 4-of-8-bytes defect's only witness.
    _mut("E_the_nested_byte_identity_assertion_is_removed", "E_ASSERTIONS",
         T_M68D2,
         "                assert _read_whole(target, os.O_RDONLY | WIN_O_BINARY) \\\n"
         "                    == self.FIXTURE, (\n"
         "                    \"the nested emulator corrupted an explicitly-binary \"\n"
         "                    \"descriptor — the 4-of-8-bytes defect\")",
         "                assert True",
         CONTROLS),
    # The no-op is no longer required: the inner layer may install a second
    # CRT shim as long as nothing it does is observed.
    _mut("E_the_second_shim_is_no_longer_forbidden", "E_ASSERTIONS", T_M68D2,
         "                assert CRT_EMULATOR.installs == outer_installs, \\\n"
         "                    \"the inner emulator installed a second CRT shim\"",
         "                assert True",
         CONTROLS),
    # The negative control is made vacuous: the outer translating host need no
    # longer actually truncate, so the proof it is still live goes with it.
    _mut("M_the_nested_negative_control_is_made_vacuous", "E_ASSERTIONS",
         T_M68D2,
         "                    assert snap.size_bytes == 26, (\n"
         "                        \"the outer translating host no longer truncates: \"\n"
         "                        f\"{snap.size_bytes}\")",
         "                    assert snap.size_bytes >= 0",
         CONTROLS),
    # The single-translation witness is deleted, so a nested layer that
    # translates on top of the host becomes undetectable.
    _mut("E_the_single_translation_witness_is_removed", "E_ASSERTIONS",
         T_M68D2,
         "                assert _read_whole(witness, os.O_RDONLY) == b\"A\\r\\nB\", (\n"
         "                    \"the nested emulator translated on top of the host: \"\n"
         "                    f\"{_read_whole(witness, os.O_RDONLY)!r} (a second pass \"\n"
         "                    \"would give b'A\\\\nB')\")",
         "                assert True",
         CONTROLS),
]

# ══ L · THE INVENTORY AND THE CI WIRING ═════════════════════════════════════
MUTATIONS += [
    # The mandatory nested invariant leaves the inventory, so the Windows job
    # stops being required to collect the only proof of the recursive rule.
    _mut("L_the_nested_invariant_leaves_the_inventory", "L_GOVERNANCE",
         T_M68D2,
         "        \"TestTheRuntimeIsNotDoubleSimulated::test_a_nested_emulator_is_a_true_noop_on_a_translating_host\",\n"
         "        \"TestTheRuntimeIsNotDoubleSimulated::test_the_host_emulator_refuses_a_host_it_cannot_measure\",",
         "        \"TestTheRuntimeIsNotDoubleSimulated::test_the_host_emulator_refuses_a_host_it_cannot_measure\",",
         CONTROLS),
    # The source-integrity suite leaves the blocking Windows job entirely —
    # §11's "removes the source-integrity suite from CI". Nothing in the
    # repository caught this before M68D.4 added the requirement, even though
    # this suite is the one the last four real Windows runs were executing.
    _mut("L_the_source_integrity_suite_leaves_ci", "L_GOVERNANCE", WORKFLOW,
         "            jarvis/tests/test_cross_platform_source_integrity_m68d2.py\n",
         "",
         CI_JOB),
    # The blocking Windows job becomes advisory, which is how a red runner is
    # turned green without repairing anything.
    _mut("L_the_windows_job_becomes_advisory", "L_GOVERNANCE", WORKFLOW,
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n"
         "    runs-on: windows-latest\n",
         "  windows-portability:\n"
         "    name: Windows portability (3.11, blocking)\n"
         "    runs-on: windows-latest\n"
         "    continue-on-error: true\n",
         CI_JOB),
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
    print(f"M68D.4 HOST-EMULATOR FALSIFICATION CAMPAIGN — {total} mutations, "
          f"{len(targets)} mapped targets\n")

    ids = [m["id"] for m in MUTATIONS]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        print(f"DUPLICATE MUTATION IDS: {duplicate_ids}")
        print("M68D4_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    missing = [f"{m['id']}: {m['file']} does not exist" for m in MUTATIONS
               if not os.path.exists(os.path.join(_ROOT, m["file"]))]
    if missing:
        for line in missing:
            print(f"  {line}")
        print("M68D4_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
        return 2

    vacuous = _preflight(targets)
    if vacuous:
        print("\nPREFLIGHT FAILED — the campaign would be vacuous:")
        for line in vacuous:
            print(f"  {line}")
        print("M68D4_MUTATION_CAMPAIGN: COULD_NOT_EVALUATE")
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
    print(f"M68D4_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(unexplained)} unexplained survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
