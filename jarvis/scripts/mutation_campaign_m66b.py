"""scripts/mutation_campaign_m66b.py — V69 M66B (§37): the mutation campaign.

For each mutation:
  1. assert the anchor occurs EXACTLY ONCE in its file (a non-unique anchor is a
     campaign error, never a silent success),
  2. apply the mutation (find → replace) on disk,
  3. run the mapped focused test node in a FRESH subprocess (no stale bytecode),
  4. require the test to FAIL — the mutation must be DETECTED,
  5. restore the file (always, in a finally).

A mutation that leaves its mapped test GREEN is a LOAD-BEARING SURVIVOR: a
security property with no test behind it. The milestone requires 0 survivors.

Prints to stdout (no artefact), mirroring the M66A.1 campaign. Run from `jarvis/`:
    python scripts/mutation_campaign_m66b.py
Exit 0 iff every mutation was detected and every anchor was unique.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Detector test nodes (relative to jarvis/).
G = "tests/test_golden_matrix_m66b.py"
E = "tests/test_escape_matrix_m66b.py"
C = "tests/test_containment_m66b.py"
F = "tests/test_four_layer_defense_v69_m66a1.py"
N = "tests/test_four_layer_nonvacuity_m66b.py"
S = "tests/test_execution_surfaces_m66b.py"

SBX = f"{G}::test_golden_sandbox_scenarios"
NET = f"{E}::TestNetworkEscapes::test_net01_host_ipv4_loopback_unreachable"
FS = f"{E}::TestFilesystemEscapes::test_fs03_host_home_enumeration"
REPO = f"{E}::TestFilesystemEscapes::test_fs04_repository_enumeration"
ENV = f"{E}::TestEnvironmentEscapes::test_env01_synthetic_secret_env_not_visible"
PRIV1 = f"{E}::TestPrivilegeEscapes::test_priv01_effective_identity_non_root"
PRIV2 = f"{E}::TestPrivilegeEscapes::test_priv02_no_new_privs_and_no_caps"
PROC3 = f"{E}::TestProcessEscapes::test_proc03_setsid_descendant_contained"
CPU = f"{E}::TestResourceEscapes::test_res01b_cpu_limit_kills_before_wall_timeout"
MEM = f"{G}::test_golden_sandbox_scenarios[A25_memory]"
STOR = f"{G}::test_golden_sandbox_scenarios[A26_storage]"
OUT = f"{G}::test_golden_sandbox_scenarios[A27_output_flood]"
FSIZE = f"{G}::test_golden_sandbox_scenarios[A29_fsize]"
PID = f"{G}::test_golden_sandbox_scenarios[A32_pidcount]"
TIMEOUT = f"{G}::test_golden_sandbox_scenarios[A23_timeout_sleep]"
A01 = f"{G}::test_golden_sandbox_scenarios[A01_arith]"

CONT = "core/containment.py"
EXEC = "tools/executor.py"
REG = "core/execution_surface_registry.py"


def _mut(mid, category, file, find, replace, test):
    return {"id": mid, "cat": category, "file": file, "find": find,
            "replace": replace, "test": test}


def _bwrap_label(control, test=A01):
    """A bubblewrap control-label flip: `c["X"] = ENFORCED` → NOT_ENFORCED.
    Detected because the derivation drops SANDBOXED when any mandatory label lies."""
    return _mut(f"BWLABEL_{control}", "TRUTH", CONT,
                f'c["{control}"] = ControlStatus.ENFORCED',
                f'c["{control}"] = ControlStatus.NOT_ENFORCED', test)


def _restricted_label(control, test=f"{G}::test_golden_B08_restricted_backend_runs_everywhere"):
    return _mut(f"RSLABEL_{control}", "TRUTH", CONT,
                f'receipt.controls["{control}"] = ControlStatus.ENFORCED',
                f'receipt.controls["{control}"] = ControlStatus.NOT_ENFORCED', test)


MUTATIONS: list[dict] = []

# ── Group 1: bubblewrap control-label flips (13) — the SANDBOXED contract ─────
for _c in ("filesystem_isolation", "network_isolation", "host_loopback_isolation",
           "descendant_containment", "pid_limit", "cpu_limit", "memory_limit",
           "storage_limit", "environment_isolation", "privilege_restriction",
           "workspace_ephemeral"):
    MUTATIONS.append(_bwrap_label(_c))
MUTATIONS.append(_mut("BWLABEL_wall_timeout", "TRUTH", CONT,
                      'c["wall_timeout"] = ControlStatus.ENFORCED',
                      'c["wall_timeout"] = ControlStatus.NOT_ENFORCED', A01))
MUTATIONS.append(_mut("BWLABEL_output_limit", "TRUTH", CONT,
                      'c["output_limit"] = ControlStatus.ENFORCED',
                      'c["output_limit"] = ControlStatus.NOT_ENFORCED', A01))

# ── Group 2: bubblewrap REAL behaviour mutations (flag removals) ──────────────
MUTATIONS += [
    _mut("BW_net_shared", "NETWORK", CONT,
         '"--unshare-all",              # user+mount+pid+net+ipc+uts+cgroup ns',
         '"--unshare-user", "--unshare-mount", "--unshare-pid", "--unshare-ipc",',
         NET),
    _mut("BW_pid_shared", "PROCESS", CONT,
         '"--unshare-all",              # user+mount+pid+net+ipc+uts+cgroup ns\n            "--die-with-parent",          # broker death tears the jail down',
         '"--unshare-user", "--unshare-mount", "--unshare-net", "--unshare-ipc", "--unshare-uts",\n            "--die-with-parent",          # broker death tears the jail down',
         PROC3),
    _mut("BW_no_clearenv", "ENV", CONT,
         '"--clearenv",                 # withhold every host env var/secret',
         '"--setenv", "PYTHONDONTWRITEBYTECODE", "1",',
         ENV),
    _mut("BW_uid_root", "PRIVILEGE", CONT,
         '"--uid", _SANDBOX_UID,', '"--uid", "0",', PRIV1),
    _mut("BW_home_mount", "FILESYSTEM", CONT,
         '"--ro-bind", scriptdir, "/jarvis_exec",',
         '"--ro-bind", scriptdir, "/jarvis_exec", "--ro-bind", "/home", "/home",',
         FS),
    _mut("BW_workspace_huge", "RESOURCE", CONT,
         'CE_WORKSPACE_BYTES = 64 * 1024 * 1024',
         'CE_WORKSPACE_BYTES = 64 * 1024 * 1024 * 1024', STOR),
    _mut("BW_no_proc_isolation", "FILESYSTEM", CONT,
         '"--symlink", "usr/bin", "/bin",',
         '"--symlink", "usr/bin", "/bin", "--ro-bind", "/home/kali", "/mnt_home",',
         # this makes the repo/home reachable at /mnt_home; escape REPO won't see
         # it (different path), so target a home-enumeration variant via FS
         FS),
]
# BW_no_proc_isolation actually does not weaken /home enumeration; replace with a
# real host-home bind that FS detects.
MUTATIONS[-1] = _mut("BW_extra_home_bind", "FILESYSTEM", CONT,
                     '"--symlink", "usr/sbin", "/sbin",',
                     '"--symlink", "usr/sbin", "/sbin", "--ro-bind", "/home", "/home",',
                     FS)

# ── Group 3: bootstrap rlimit mutations (4) ───────────────────────────────────
MUTATIONS += [
    _mut("BOOT_cpu_off", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_CPU, ({CE_CPU_SECONDS}, {CE_CPU_SECONDS} + 1))',
         'pass  # RLIMIT_CPU removed', CPU),
    _mut("BOOT_mem_off", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_AS, ({CE_MEM_BYTES}, {CE_MEM_BYTES}))',
         'pass  # RLIMIT_AS removed', MEM),
    _mut("BOOT_nproc_off", "PROCESS", CONT,
         'resource.setrlimit(resource.RLIMIT_NPROC, ({CE_NPROC}, {CE_NPROC}))',
         'pass  # RLIMIT_NPROC removed', PID),
    _mut("BOOT_fsize_off", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_FSIZE, ({CE_FSIZE_BYTES}, {CE_FSIZE_BYTES}))',
         'pass  # RLIMIT_FSIZE removed', FSIZE),
]

# ── Group 4: derive_profile mutations (5) ─────────────────────────────────────
MUTATIONS += [
    _mut("DERIVE_any_not_all", "TRUTH", CONT,
         'sandbox_all = all(enforced(c) for c in MANDATORY_SANDBOX_CONTROLS)',
         'sandbox_all = any(enforced(c) for c in MANDATORY_SANDBOX_CONTROLS)',
         f"{C}::TestProfileDerivation::test_missing_any_mandatory_control_is_not_sandboxed"),
    _mut("DERIVE_ignore_cleanup", "TRUTH", CONT,
         'cleanup_ok = cleanup_status is not ControlStatus.NOT_ENFORCED',
         'cleanup_ok = True',
         f"{C}::TestProfileDerivation::test_cleanup_failure_blocks_sandboxed"),
    _mut("DERIVE_force_sandboxed", "TRUTH", CONT,
         'if sandbox_all and cleanup_ok:\n        return ExecutionProfile.SANDBOXED',
         'if True:\n        return ExecutionProfile.SANDBOXED',
         f"{C}::TestProfileDerivation::test_missing_any_mandatory_control_is_not_sandboxed"),
    _mut("DERIVE_baseline_any", "TRUTH", CONT,
         'if all(enforced(c) for c in BASELINE_RESTRICTED_CONTROLS):',
         'if any(enforced(c) for c in BASELINE_RESTRICTED_CONTROLS):',
         f"{C}::TestDerivationExtra::test_partial_baseline_is_direct"),
    _mut("DERIVE_direct_to_sandboxed", "TRUTH", CONT,
         'return ExecutionProfile.DIRECT_PROCESS',
         'return ExecutionProfile.SANDBOXED',
         f"{C}::TestProfileDerivation::test_empty_controls_is_direct"),
]

# ── Group 5: receipt mutations (3) ────────────────────────────────────────────
MUTATIONS += [
    _mut("RCPT_net_hardcoded", "NETWORK", CONT,
         '"network_isolation": self.network_isolation.value,',
         '"network_isolation": "enforced",',
         f"{C}::TestRestrictedBackendProperties::test_restricted_network_is_not_enforced"),
    _mut("RCPT_hide_downgrade", "TRUTH", CONT,
         '"downgraded": self.downgraded,',
         '"downgraded": False,',
         f"{G}::test_golden_B05_compat_downgrade_never_sandboxed"),
    _mut("RCPT_profile_from_label", "TRUTH", CONT,
         'prof = self.derived_profile()',
         'prof = self.requirement and ExecutionProfile.SANDBOXED',
         f"{C}::TestProfileDerivation::test_receipt_derives_profile_from_control"
         f"s_not_label"),
]

# ── Group 6: broker mutations (8) ─────────────────────────────────────────────
MUTATIONS += [
    _mut("BRK_req_downgrade", "BROKER", CONT,
         'if tool_name == "code_execute":\n            return ContainmentRequirement.SANDBOX_REQUIRED',
         'if tool_name == "code_execute":\n            return ContainmentRequirement.RESTRICTED_OK',
         f"{N}::TestPolicyImmovable::test_requirement_unchanged_by_content"),
    _mut("BRK_req_reads_input", "BROKER", CONT,
         'def evaluate_requirement(self, tool_name: str, tool_input: dict\n'
         '                             ) -> ContainmentRequirement:',
         'def evaluate_requirement(self, tool_name: str, tool_input: dict\n'
         '                             ) -> ContainmentRequirement:\n'
         '        if tool_input.get("sandbox") is False:\n'
         '            return ContainmentRequirement.RESTRICTED_OK',
         f"{C}::TestNoUntrustedDowngrade::test_tool_input_cannot_change_requirement"),
    _mut("BRK_select_first", "BROKER", CONT,
         'for backend in self._backends:\n            if backend.can_satisfy(requirement):\n                return backend\n        return None',
         'return self._backends[0]',
         f"{C}::TestBrokerSelection::test_weaker_backend_never_selected_for_sandbox"),
    _mut("BRK_cansatisfy_true", "BROKER", CONT,
         'return prof_rank >= _REQUIREMENT_RANK[requirement]',
         'return True',
         f"{C}::TestBackendContract::test_restricted_cannot_satisfy_sandbox_required"),
    _mut("BRK_silent_fallback", "BROKER", CONT,
         'receipt = ContainmentReceipt(\n            requirement=request.requirement, backend="none",',
         'return RestrictedProcessBackend().execute(request)\n        receipt = ContainmentReceipt(\n            requirement=request.requirement, backend="none",',
         f"{C}::TestBrokerSelection::test_fail_closed_when_no_backend_and_strict"),
    _mut("BRK_compat_default", "BROKER", CONT,
         'return OperatorPolicy.COMPAT if raw == "compat" else OperatorPolicy.STRICT',
         'return OperatorPolicy.COMPAT',
         f"{C}::TestNoUntrustedDowngrade::test_operator_policy_reads_only_env"),
    _mut("BRK_rank_swapped", "BROKER", CONT,
         'ContainmentRequirement.SANDBOX_REQUIRED: 3,',
         'ContainmentRequirement.SANDBOX_REQUIRED: 1,',
         f"{C}::TestBackendContract::test_restricted_cannot_satisfy_sandbox_required"),
    _mut("BRK_profrank_sandbox", "BROKER", CONT,
         'ExecutionProfile.SANDBOXED: 3,\n        }[prof]',
         'ExecutionProfile.SANDBOXED: 0,\n        }[prof]',
         f"{C}::TestBackendContract::test_bubblewrap_satisfies_sandbox_iff_available"),
]

# ── Group 7: RestrictedProcessBackend mutations (4) ───────────────────────────
MUTATIONS += [
    _mut("RS_net_enforced", "NETWORK", CONT,
         'receipt.network_isolation = ControlStatus.NOT_ENFORCED\n        receipt.measured_limitations.append(\n            "network is NOT isolated: a snippet can still open outbound sockets "',
         'receipt.network_isolation = ControlStatus.ENFORCED\n        receipt.measured_limitations.append(\n            "network is NOT isolated: a snippet can still open outbound sockets "',
         f"{C}::TestRestrictedBackendProperties::test_restricted_network_is_not_enforced"),
    _mut("RS_maxprofile_sandbox", "BROKER", CONT,
         'def max_profile(self) -> ExecutionProfile:\n        return ExecutionProfile.RESTRICTED_PROCESS',
         'def max_profile(self) -> ExecutionProfile:\n        return ExecutionProfile.SANDBOXED',
         f"{G}::test_golden_B04_L3_denial_weaker_backend"),
    _mut("RS_cpu_off", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_CPU,\n                                   (CE_CPU_SECONDS, CE_CPU_SECONDS + 1))',
         'pass',
         f"{C}::TestRestrictedBackendProperties::test_restricted_cpu_limit_kills_busy_loop"),
    _mut("RS_windows_sandbox", "TRUTH", CONT,
         'return (ExecutionProfile.RESTRICTED_PROCESS if os.name == "nt"\n                else ExecutionProfile.DIRECT_PROCESS)',
         'return ExecutionProfile.SANDBOXED',
         f"{G}::test_golden_B07_windows_backend_truthful"),
]

# ── Group 8: executor routing mutations (4) ───────────────────────────────────
MUTATIONS += [
    _mut("EXE_not_failclosed", "BROKER", EXEC,
         'if not outcome.executed:\n            return {"error": outcome.error or "containment unavailable",',
         'if False:\n            return {"error": outcome.error or "containment unavailable",',
         f"{C}::TestExecutorRouting::test_code_execute_fails_closed_when_broker_cannot"),
    _mut("EXE_hardcode_restricted", "BROKER", EXEC,
         'requirement = broker.evaluate_requirement("code_execute", {"code": code})',
         'requirement = ContainmentRequirement.RESTRICTED_OK',
         f"{C}::TestExecutorRouting::test_code_execute_is_sandboxed_when_available"),
    _mut("EXE_drop_receipt", "TRUTH", EXEC,
         'receipt = outcome.receipt.to_dict() if outcome.receipt is not None else {}',
         'receipt = {}',
         f"{F}::TestF6Execution::test_network_isolation_reported_truthfully"),
    _mut("EXE_bypass_broker", "BROKER", EXEC,
         'outcome = broker.execute(ExecutionRequest(code, timeout, requirement))',
         'from core.containment import RestrictedProcessBackend as _RPB\n        outcome = _RPB().execute(ExecutionRequest(code, timeout, requirement))',
         f"{C}::TestExecutorRouting::test_code_execute_is_sandboxed_when_available"),
]

# ── Group 9: LEGACY invariants (6) ────────────────────────────────────────────
MUTATIONS += [
    _mut("LEG_m65d_effect_key", "LEGACY", EXEC,
         'return f"{epoch}|{tool_name}|{args}"',
         'return f"{epoch}|{tool_name}"',
         "tests/test_effect_semantics_v69_m65d.py::test_a_normaliser_never_merges_genuinely_different_calls"),
    _mut("LEG_windows_backend_status", "LEGACY", CONT,
         '"status": ("WINDOWS_RESTRICTED_PROCESS_ONLY" if os.name == "nt"\n                       else "NOT_ON_THIS_HOST"),',
         '"status": "WINDOWS_SANDBOXED",',
         f"{G}::test_golden_B07_windows_backend_truthful"),
    _mut("LEG_coverage_equal", "LEGACY", REG,
         'if d["disposition"] in (BROKER_REQUIRED, RESTRICTED_ONLY)',
         'if d["disposition"] in (BROKER_REQUIRED,)',
         f"{S}::test_coverage_invariant_holds"),
    _mut("LEG_registry_drop_surface", "LEGACY", REG,
         '"disposition": RESTRICTED_ONLY,\n        "controls": "allowlist + shell=False + `python -c` blocked + NATO HITL",',
         '"disposition": DOCUMENT_ONLY,\n        "controls": "allowlist + shell=False + `python -c` blocked + NATO HITL",',
         f"{S}::test_coverage_invariant_holds"),
    _mut("LEG_mandatory_shrink", "LEGACY", CONT,
         '    "network_isolation",\n    "host_loopback_isolation",',
         '    "host_loopback_isolation",',
         f"{C}::TestDerivationExtra::test_network_isolation_is_mandatory"),
    _mut("LEG_env_allowlist_widen", "LEGACY", CONT,
         'CE_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")',
         'CE_ENV_ALLOWLIST: tuple[str, ...] = tuple(__import__("os").environ)',
         f"{C}::TestRestrictedBackendProperties::test_restricted_env_allowlist_withholds_secret"),
]

# ── Group 10: static detector / surface registry non-vacuity (3) ──────────────
MUTATIONS += [
    _mut("DET_miss_subprocess", "LEGACY", "scripts/check_execution_surfaces.py",
         '("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),',
         '("subprocess", "call"),',
         f"{S}::test_detector_flags_direct_subprocess"),
    _mut("DET_allow_everything", "LEGACY", "scripts/check_execution_surfaces.py",
         'def _reviewed(relpath: str, func_dotted: str, funcs: list[str]) -> bool:',
         'def _reviewed(relpath: str, func_dotted: str, funcs: list[str]) -> bool:\n    return True',
         f"{S}::test_detector_flags_direct_subprocess"),
    _mut("DET_coverage_blind", "LEGACY", "scripts/check_execution_surfaces.py",
         'if arbitrary != covered:',
         'if False:',
         f"{S}::test_coverage_check_flags_a_mismatch"),
]


# ── Group 11: restricted-backend baseline label flips (6) ─────────────────────
for _c in ("dedicated_cwd", "minimal_env", "wall_timeout", "stdout_cap",
           "stderr_cap"):
    MUTATIONS.append(_restricted_label(_c))

# ── Group 12: additional real-behaviour and logic mutations (20) ──────────────
MUTATIONS += [
    _mut("BW_stdout_cap_huge", "RESOURCE", CONT,
         'CE_STDOUT_CAP = 3000', 'CE_STDOUT_CAP = 3000 * 100000', OUT),
    _mut("BW_cpu_value_huge", "RESOURCE", CONT,
         'CE_CPU_SECONDS = 5                      # RLIMIT_CPU soft (SIGXCPU)',
         'CE_CPU_SECONDS = 100000                 # RLIMIT_CPU soft (SIGXCPU)', CPU),
    _mut("BW_mem_value_huge", "RESOURCE", CONT,
         'CE_MEM_BYTES = 512 * 1024 * 1024        # RLIMIT_AS address-space cap',
         'CE_MEM_BYTES = 512 * 1024 * 1024 * 1024  # RLIMIT_AS address-space cap', MEM),
    _mut("BW_fsize_value_huge", "RESOURCE", CONT,
         'CE_FSIZE_BYTES = 16 * 1024 * 1024       # RLIMIT_FSIZE max single-file write',
         'CE_FSIZE_BYTES = 512 * 1024 * 1024      # RLIMIT_FSIZE max single-file write',
         FSIZE),
    _mut("BW_nproc_value_huge", "PROCESS", CONT,
         'CE_NPROC = 64                           # RLIMIT_NPROC inside the remapped UID',
         'CE_NPROC = 100000                       # RLIMIT_NPROC inside the remapped UID',
         PID),
    _mut("BW_no_proc_mount", "PRIVILEGE", CONT,
         '"--proc", "/proc",            # fresh proc for the PID ns',
         '"--tmpfs", "/proc",           # fresh proc for the PID ns', PRIV2),
    _mut("BOOT_no_exec_snippet", "BROKER", CONT,
         'os.execv(sys.executable, [sys.executable, "-I", sys.argv[1]])',
         'pass  # never exec the snippet', A01),
    _mut("BW_snippet_path_wrong", "BROKER", CONT,
         '"/jarvis_exec/_bootstrap.py", "/jarvis_exec/snippet.py"]',
         '"/jarvis_exec/_bootstrap.py", "/jarvis_exec/_bootstrap.py"]', A01),
    _mut("RCPT_derived_profile_liar", "TRUTH", CONT,
         'def derived_profile(self) -> ExecutionProfile:\n        return derive_profile(self.controls, cleanup_status=self.cleanup_status)',
         'def derived_profile(self) -> ExecutionProfile:\n        return ExecutionProfile.SANDBOXED',
         f"{G}::test_golden_B03_cleanup_failure_truthful"),
    _mut("BRK_execute_select_first", "BROKER", CONT,
         'backend = self.select_backend(request.requirement)\n        if backend is not None:',
         'backend = self._backends[0]\n        if backend is not None:',
         f"{C}::TestBrokerSelection::test_fail_closed_when_no_backend_and_strict"),
    _mut("BRK_noncode_sandbox", "BROKER", CONT,
         'return ContainmentRequirement.RESTRICTED_OK\n\n    def select_backend',
         'return ContainmentRequirement.SANDBOX_REQUIRED\n\n    def select_backend',
         f"{G}::test_golden_B10_production_L3_negative_guard"),
    _mut("BRK_compat_downgraded_false", "BROKER", CONT,
         'outcome.receipt.downgraded = True',
         'outcome.receipt.downgraded = False',
         f"{G}::test_golden_B05_compat_downgrade_never_sandboxed"),
    _mut("BRK_policy_check_inverted", "BROKER", CONT,
         'return OperatorPolicy.COMPAT if raw == "compat" else OperatorPolicy.STRICT',
         'return OperatorPolicy.COMPAT if raw != "compat" else OperatorPolicy.STRICT',
         f"{C}::TestNoUntrustedDowngrade::test_operator_policy_reads_only_env"),
    _mut("LEG_always_hitl_code", "LEGACY", EXEC,
         '_ALWAYS_HITL_TOOLS: frozenset[str] = frozenset({',
         '_ALWAYS_HITL_TOOLS: frozenset[str] = frozenset({\n    "___removed_placeholder___",',
         "tests/test_code_execute_gate.py::test_code_execute_in_always_hitl"),
    _mut("DERIVE_cleanup_or", "TRUTH", CONT,
         'if sandbox_all and cleanup_ok:',
         'if sandbox_all or cleanup_ok:',
         f"{C}::TestProfileDerivation::test_empty_controls_is_direct"),
    _mut("RCPT_mandatory_promote_absent", "TRUTH", CONT,
         'out[name] = self.controls.get(name, ControlStatus.NOT_ENFORCED).value',
         'out[name] = ControlStatus.ENFORCED.value',
         f"{C}::TestDerivationExtra::test_promoted_named_field_reflects_reality"),
    _mut("CANSAT_ge_to_le", "BROKER", CONT,
         'return prof_rank >= _REQUIREMENT_RANK[requirement]',
         'return prof_rank <= _REQUIREMENT_RANK[requirement]',
         f"{C}::TestBackendContract::test_restricted_cannot_satisfy_sandbox_required"),
    _mut("EVAL_req_default_sandbox", "BROKER", CONT,
         '        if tool_name == "code_execute":\n            return ContainmentRequirement.SANDBOX_REQUIRED\n        return ContainmentRequirement.RESTRICTED_OK',
         '        return ContainmentRequirement.SANDBOX_REQUIRED',
         f"{G}::test_golden_B10_production_L3_negative_guard"),
]


def _run() -> int:
    py = sys.executable
    total = len(MUTATIONS)
    survivors: list[str] = []
    anchor_errors: list[str] = []
    detected = 0
    print(f"M66B MUTATION CAMPAIGN — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        original = open(path, encoding="utf-8").read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']}: {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            open(path, "w", encoding="utf-8").write(mutated)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(  # nosec B603 - fixed argv test runner
                [py, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider",
                 m["test"]],
                cwd=_ROOT, capture_output=True, text=True, env=env, timeout=180)
            failed = proc.returncode != 0
            if failed:
                detected += 1
                print(f"  [DETECTED] {m['id']:32s} ({m['cat']}) via {m['test'].split('::')[-1]}")
            else:
                survivors.append(m["id"])
                print(f"  [SURVIVOR] {m['id']:32s} ({m['cat']}) — NO TEST FAILED")
        finally:
            open(path, "w", encoding="utf-8").write(original)

    print(f"\n{'='*70}")
    print(f"mutations:        {total}")
    print(f"detected:         {detected}")
    print(f"survivors:        {len(survivors)}  {survivors if survivors else ''}")
    print(f"anchor errors:    {len(anchor_errors)}  {anchor_errors if anchor_errors else ''}")
    ok = not survivors and not anchor_errors and total >= 80
    print(f"M66B_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(survivors)} survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
