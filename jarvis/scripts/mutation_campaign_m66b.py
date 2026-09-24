"""scripts/mutation_campaign_m66b.py — V69 M66B (§37 + Round-1 §19): the campaign.

Rebuilt after Round-1 remediation (the receipt now DERIVES controls from a READY
handshake's observed evidence, and the host gateway is governed by a
command-SEMANTIC policy). For each mutation:
  1. assert the anchor occurs EXACTLY ONCE in its file,
  2. apply it, run the mapped focused test in a FRESH subprocess,
  3. require the test to FAIL (the mutation must be DETECTED),
  4. restore the file (always).

A mutation whose mapped test stays GREEN is a LOAD-BEARING SURVIVOR: a security
property with no test behind it. Requirement: >= 85 meaningful mutations, valid
(a security mutation must leave the program runnable — a weakened boundary, not a
setup crash), 0 load-bearing survivors. Run from `jarvis/`.
"""
from __future__ import annotations

import os
import subprocess  # nosec B404 - this IS a test runner; every argv is a fixed list
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

G = "tests/test_golden_matrix_m66b.py"
C = "tests/test_containment_m66b.py"
F = "tests/test_four_layer_defense_v69_m66a1.py"
N = "tests/test_four_layer_nonvacuity_m66b.py"
S = "tests/test_execution_surfaces_m66b.py"
OBS = "tests/test_observed_containment_m66b.py"
CP = "tests/test_command_policy_m66b.py"

A01 = f"{G}::test_golden_sandbox_scenarios[A01_arith]"
B04 = f"{G}::test_golden_B04_L3_denial_weaker_backend"
B05 = f"{G}::test_golden_B05_compat_downgrade_never_sandboxed"
B07 = f"{G}::test_golden_B07_windows_backend_truthful"
B08 = f"{G}::test_golden_B08_restricted_backend_runs_everywhere"
B10 = f"{G}::test_golden_B10_production_L3_negative_guard"

MAJOR_A = f"{OBS}::TestLiveObservation::test_major_a_net_share_not_sandboxed_and_zero_effect"
MAJOR_B = f"{OBS}::TestLiveObservation::test_major_b_bad_bind_fails_closed_structural"
BOOTEXIT = f"{OBS}::TestLiveObservation::test_bootstrap_exits_before_ready_is_zero_effect"
READY0 = f"{OBS}::TestLiveObservation::test_ready_zero_flag_does_not_execute"
FORGE2 = f"{OBS}::TestLiveObservation::test_forged_wrong_nonce_ready_is_rejected"
DERIV = f"{OBS}::TestEvidenceDerivation::test_bad_evidence_drops_the_control"
UNKNOWN_T = f"{OBS}::TestEvidenceDerivation::test_unknown_control_never_yields_sandboxed"

CP_GIT = f"{CP}::TestGit::test_git_execution_vectors_blocked"
CP_NMAP = f"{CP}::TestNmap::test_nmap_nse_blocked"
CP_WGET = f"{CP}::TestWget::test_wget_helpers_blocked"
CP_FIND = f"{CP}::TestFind::test_find_exec_blocked"
CP_REMOVED = f"{CP}::TestRemoved::test_removed_binary_exec_vectors_blocked"
CP_INTERP = f"{CP}::TestInterpreters::test_interpreter_code_blocked"
CP_INJECT = f"{CP}::TestGlobalInjection::test_injection_fragment_refused"
CP_UNKNOWN = f"{CP}::TestPolicyCoverage::test_unknown_binary_defaults_to_deny"
CP_FIND_UNIT = f"{CP}::TestPolicyCoverage::test_find_rule_is_load_bearing"
CP_REMOVED_UNIT = f"{CP}::TestPolicyCoverage::test_removed_binary_rule_is_load_bearing"
CP_GIT_GLOBAL = f"{CP}::TestPolicyCoverage::test_git_global_option_rules_are_load_bearing"
CP_CURL = f"{CP}::TestPolicyCoverage::test_curl_config_rule_is_load_bearing"

CONT = "core/containment.py"
CMD = "core/command_policy.py"
EXEC = "tools/executor.py"
REG = "core/execution_surface_registry.py"
DET = "scripts/check_execution_surfaces.py"
EJ = "core/effect_journal.py"


def _mut(mid, cat, file, find, replace, test):
    return {"id": mid, "cat": cat, "file": file, "find": find,
            "replace": replace, "test": test}


MUTATIONS: list[dict] = []

# ── Group 1: evidence-derivation lies (a control derived True regardless) ─────
_DERIV = [
    ("network_isolation", 'c["network_isolation"] = unk(\n            (ev.get("ifaces") == ["lo"] and netns != "" and netns != host_netns),\n            verifiable=bool(host_netns))',
     'c["network_isolation"] = unk(True, verifiable=True)'),
    ("host_loopback", 'c["host_loopback_isolation"] = unk(\n            not ev.get("loopback_connect", True), verifiable=probe_live)',
     'c["host_loopback_isolation"] = unk(True, verifiable=True)'),
    ("descendant", 'c["descendant_containment"] = unk(\n            (0 < ev.get("proc_count", -1) <= 15 and pidns != "" and pidns != host_pidns),\n            verifiable=bool(host_pidns))',
     'c["descendant_containment"] = unk(True, verifiable=True)'),
    ("pid_limit", 'c["pid_limit"] = st(ev.get("nproc") == CE_NPROC)',
     'c["pid_limit"] = st(True)'),
    ("cpu_limit", 'c["cpu_limit"] = st(ev.get("cpu") == CE_CPU_SECONDS)',
     'c["cpu_limit"] = st(True)'),
    ("memory_limit", 'c["memory_limit"] = st(ev.get("as_") == CE_MEM_BYTES)',
     'c["memory_limit"] = st(True)'),
    ("storage_limit", 'c["storage_limit"] = st(0 < ev.get("tmpfs_bytes", -1) <= CE_WORKSPACE_BYTES)',
     'c["storage_limit"] = st(True)'),
    ("environment", 'c["environment_isolation"] = st(not ev.get("env_extra", ["x"]))',
     'c["environment_isolation"] = st(True)'),
    ("filesystem", 'c["filesystem_isolation"] = unk(\n            (not ev.get("home", True) and not ev.get("shadow", True)\n             and not ev.get("repo", True) and not ev.get("canary_read", True)),\n            verifiable=canary_established)',
     'c["filesystem_isolation"] = unk(True, verifiable=True)'),
    ("privilege", 'c["privilege_restriction"] = st(ev.get("uid") not in (0, None)',
     'c["privilege_restriction"] = st(True or ev.get("uid") not in (0, None)'),
    ("workspace", 'c["workspace_ephemeral"] = st(ev.get("cwd") == "/work"',
     'c["workspace_ephemeral"] = st(True or ev.get("cwd") == "/work"'),
]
for _name, _find, _repl in _DERIV:
    MUTATIONS.append(_mut(f"DERIV_{_name}", "RECEIPT", CONT, _find, _repl, DERIV))

# ── Group 2: bootstrap self-check + evidence gathering (must fail closed) ─────
MUTATIONS += [
    _mut("BOOT_selfcheck_true", "FAILCLOSED", CONT,
         '_ok = (_ev["uid"] != 0 and _ev["euid"] != 0 and _ev["gid"] != 0',
         '_ok = (True or _ev["uid"] != 0 and _ev["euid"] != 0 and _ev["gid"] != 0',
         MAJOR_A),
]

# ── Group 3: broker handshake parsing (fail-closed authority) ─────────────────
MUTATIONS += [
    _mut("BRK_ready_flag_ignored", "FAILCLOSED", CONT,
         'if ready_flag != "1":',
         'if ready_flag == "\\x00never":',
         READY0),
    _mut("BRK_nonce_check_bypassed", "FAILCLOSED", CONT,
         'if len(parts) == 4 and parts[1] == nonce:',
         'if len(parts) == 4 and len(parts[1]) >= 0:',
         FORGE2),
    _mut("BRK_noready_controls_enforced", "FAILCLOSED", CONT,
         'for name in MANDATORY_SANDBOX_CONTROLS:\n                c[name] = ControlStatus.UNKNOWN\n            receipt.failure_reason = (FailureReason.NAMESPACE_SETUP_FAILED.value',
         'for name in MANDATORY_SANDBOX_CONTROLS:\n                c[name] = ControlStatus.ENFORCED\n            receipt.failure_reason = (FailureReason.NAMESPACE_SETUP_FAILED.value',
         BOOTEXIT),
]

# ── Group 4: bwrap argv flags (removal weakens a boundary; self-check catches) ─
MUTATIONS += [
    _mut("BW_net_shared", "NETWORK", CONT,
         '"--unshare-all",              # user+mount+pid+net+ipc+uts+cgroup ns',
         '"--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",',
         A01),
    _mut("BW_pid_shared", "PROCESS", CONT,
         '"--unshare-all",              # user+mount+pid+net+ipc+uts+cgroup ns\n            "--die-with-parent",          # broker death tears the jail down',
         '"--unshare-user", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",\n            "--die-with-parent",          # broker death tears the jail down',
         A01),
    _mut("BW_no_clearenv", "ENV", CONT,
         '"--clearenv",                 # withhold every host env var/secret',
         '"--setenv", "PYTHONDONTWRITEBYTECODE", "1",',
         A01),
    _mut("BW_uid_root", "PRIVILEGE", CONT,
         '"--uid", _SANDBOX_UID,', '"--uid", "0",', A01),
    _mut("BW_no_proc", "PRIVILEGE", CONT,
         '"--proc", "/proc",            # fresh proc for the PID ns',
         '"--tmpfs", "/proc",           # fresh proc for the PID ns',
         A01),
    _mut("BW_workspace_unbounded", "RESOURCE", CONT,
         '"--size", str(CE_WORKSPACE_BYTES), "--tmpfs", "/work",  # bounded workspace',
         '"--tmpfs", "/work",  # bounded workspace',
         A01),
]

# ── Group 5: bootstrap rlimit values (self-check observes the mismatch) ────────
MUTATIONS += [
    _mut("BOOT_cpu_value", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_CPU, ({CE_CPU_SECONDS}, {CE_CPU_SECONDS} + 1))',
         'resource.setrlimit(resource.RLIMIT_CPU, (999999, 999999))', A01),
    _mut("BOOT_mem_value", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_AS, ({CE_MEM_BYTES}, {CE_MEM_BYTES}))',
         'pass  # RLIMIT_AS removed', A01),
    _mut("BOOT_nproc_value", "PROCESS", CONT,
         'resource.setrlimit(resource.RLIMIT_NPROC, ({CE_NPROC}, {CE_NPROC}))',
         'pass  # RLIMIT_NPROC removed', A01),
    _mut("BOOT_fsize_value", "RESOURCE", CONT,
         'resource.setrlimit(resource.RLIMIT_FSIZE, ({CE_FSIZE_BYTES}, {CE_FSIZE_BYTES}))',
         'pass  # RLIMIT_FSIZE removed', A01),
]

# ── Group 6: command-SEMANTIC policy (LOLBins) ────────────────────────────────
MUTATIONS += [
    _mut("CMD_git_c_allowed", "LOLBIN", CMD,
         '    "-c", "-C", "--exec-path", "--config-env", "--namespace", "--work-tree",',
         '    "--exec-path", "--config-env", "--namespace", "--work-tree",',
         CP_GIT),
    _mut("CMD_nmap_script_allowed", "LOLBIN", CMD,
         'if low.startswith("--script") or low == "--datadir" or low.startswith("--datadir="):',
         'if low == "\\x00never":',
         CP_NMAP),
    _mut("CMD_find_exec_allowed", "LOLBIN", CMD,
         'banned = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprintf",',
         'banned = {"\\x00never", "-execdir", "-ok", "-okdir", "-delete", "-fprintf",',
         CP_FIND_UNIT),
    _mut("CMD_removed_binary_reintroduced", "LOLBIN", CMD,
         '"ssh": CommandCapability.REMOVED,',
         '"ssh": CommandCapability.SAFE_FIXED_DIAGNOSTIC,',
         CP_REMOVED_UNIT),
    _mut("CMD_interpreter_allowed", "LOLBIN", CMD,
         'if args and all(a.lower() in _INFO_FLAGS for a in args):',
         'if True or (args and all(a.lower() in _INFO_FLAGS for a in args)):',
         CP_INTERP),
    _mut("CMD_injection_fragment_dropped", "LOLBIN", CMD,
         '    "proxycommand",            # ssh/scp -oProxyCommand=',
         '    "__nomatch__",             # ssh/scp -oProxyCommand=',
         CP_INJECT),
    _mut("CMD_unknown_binary_allowed", "LOLBIN", CMD,
         'cap = HOST_COMMAND_POLICY.get(binary, CommandCapability.REMOVED)',
         'cap = HOST_COMMAND_POLICY.get(binary, CommandCapability.SAFE_FIXED_DIAGNOSTIC)',
         CP_UNKNOWN),
]

# ── Group 7: broker selection / requirement / fail-closed ─────────────────────
MUTATIONS += [
    _mut("BRK_req_downgrade", "BROKER", CONT,
         'if tool_name == "code_execute":\n            return ContainmentRequirement.SANDBOX_REQUIRED',
         'if tool_name == "code_execute":\n            return ContainmentRequirement.RESTRICTED_OK',
         f"{N}::TestPolicyImmovable::test_requirement_unchanged_by_content"),
    _mut("BRK_noncode_sandbox", "BROKER", CONT,
         '        return ContainmentRequirement.RESTRICTED_OK\n\n    def select_backend',
         '        return ContainmentRequirement.SANDBOX_REQUIRED\n\n    def select_backend',
         B10),
    _mut("BRK_cansatisfy_true", "BROKER", CONT,
         'return prof_rank >= _REQUIREMENT_RANK[requirement]',
         'return True',
         f"{C}::TestBackendContract::test_restricted_cannot_satisfy_sandbox_required"),
    _mut("BRK_rank_swapped", "BROKER", CONT,
         'ContainmentRequirement.SANDBOX_REQUIRED: 3,',
         'ContainmentRequirement.SANDBOX_REQUIRED: 1,',
         f"{C}::TestBackendContract::test_restricted_cannot_satisfy_sandbox_required"),
    _mut("BRK_profrank_sandbox", "BROKER", CONT,
         'ExecutionProfile.SANDBOXED: 3,\n        }[prof]',
         'ExecutionProfile.SANDBOXED: 0,\n        }[prof]',
         f"{C}::TestBackendContract::test_bubblewrap_satisfies_sandbox_iff_available"),
    _mut("BRK_silent_fallback", "BROKER", CONT,
         'receipt = ContainmentReceipt(\n            requirement=request.requirement, backend="none",',
         'return RestrictedProcessBackend().execute(request)\n        receipt = ContainmentReceipt(\n            requirement=request.requirement, backend="none",',
         f"{C}::TestBrokerSelection::test_fail_closed_when_no_backend_and_strict"),
    _mut("BRK_compat_default", "BROKER", CONT,
         'return OperatorPolicy.COMPAT if raw == "compat" else OperatorPolicy.STRICT',
         'return OperatorPolicy.COMPAT',
         f"{C}::TestNoUntrustedDowngrade::test_operator_policy_reads_only_env"),
    _mut("BRK_compat_downgraded_false", "BROKER", CONT,
         'outcome.receipt.downgraded = True',
         'outcome.receipt.downgraded = False',
         B05),
    _mut("BRK_select_first", "BROKER", CONT,
         'for backend in self._backends:\n            if backend.can_satisfy(requirement):\n                return backend\n        return None',
         'return self._backends[0]',
         f"{C}::TestBrokerSelection::test_weaker_backend_never_selected_for_sandbox"),
]

# ── Group 8: derive_profile + receipt + restricted backend ────────────────────
MUTATIONS += [
    _mut("DERIVE_any_not_all", "TRUTH", CONT,
         'sandbox_all = all(enforced(c) for c in MANDATORY_SANDBOX_CONTROLS)',
         'sandbox_all = any(enforced(c) for c in MANDATORY_SANDBOX_CONTROLS)',
         UNKNOWN_T),
    _mut("DERIVE_ignore_cleanup", "TRUTH", CONT,
         'cleanup_ok = cleanup_status is ControlStatus.ENFORCED',
         'cleanup_ok = True',
         f"{C}::TestProfileDerivation::test_cleanup_none_default_is_not_sandboxed"),
    _mut("DERIVE_force_sandboxed", "TRUTH", CONT,
         'if sandbox_all and cleanup_ok:\n        return ExecutionProfile.SANDBOXED',
         'if True:\n        return ExecutionProfile.SANDBOXED',
         UNKNOWN_T),
    _mut("DERIVE_baseline_any", "TRUTH", CONT,
         'if all(enforced(c) for c in BASELINE_RESTRICTED_CONTROLS):',
         'if any(enforced(c) for c in BASELINE_RESTRICTED_CONTROLS):',
         f"{C}::TestDerivationExtra::test_partial_baseline_is_direct"),
    _mut("DERIVE_direct_to_sandboxed", "TRUTH", CONT,
         '    return ExecutionProfile.DIRECT_PROCESS',
         '    return ExecutionProfile.SANDBOXED',
         f"{C}::TestProfileDerivation::test_empty_controls_is_direct"),
    _mut("RCPT_net_hardcoded", "TRUTH", CONT,
         '"network_isolation": self.controls.get(\n                "network_isolation", self.network_isolation).value,',
         '"network_isolation": "enforced",',
         f"{C}::TestRestrictedBackendProperties::test_restricted_network_is_not_enforced"),
    _mut("RCPT_hide_downgrade", "TRUTH", CONT,
         '"downgraded": self.downgraded,',
         '"downgraded": False,',
         B05),
    _mut("RCPT_promote_absent_enforced", "TRUTH", CONT,
         'out[name] = self.controls.get(name, ControlStatus.NOT_ENFORCED).value',
         'out[name] = ControlStatus.ENFORCED.value',
         f"{C}::TestDerivationExtra::test_promoted_named_field_reflects_reality"),
    _mut("RS_net_enforced", "TRUTH", CONT,
         'receipt.controls["network_isolation"] = ControlStatus.NOT_ENFORCED',
         'receipt.controls["network_isolation"] = ControlStatus.ENFORCED',
         f"{C}::TestRestrictedBackendProperties::test_restricted_network_is_not_enforced"),
    _mut("RS_maxprofile_sandbox", "BROKER", CONT,
         'def max_profile(self) -> ExecutionProfile:\n        return ExecutionProfile.RESTRICTED_PROCESS',
         'def max_profile(self) -> ExecutionProfile:\n        return ExecutionProfile.SANDBOXED',
         B04),
    _mut("RS_windows_sandbox", "TRUTH", CONT,
         'return (ExecutionProfile.RESTRICTED_PROCESS if os.name == "nt"\n                else ExecutionProfile.DIRECT_PROCESS)',
         'return ExecutionProfile.SANDBOXED',
         B07),
]

# ── Group 9: restricted-backend baseline labels + env allowlist ───────────────
for _c in ("dedicated_cwd", "minimal_env", "wall_timeout", "stdout_cap", "stderr_cap"):
    MUTATIONS.append(_mut(f"RSLABEL_{_c}", "TRUTH", CONT,
                          f'receipt.controls["{_c}"] = ControlStatus.ENFORCED',
                          f'receipt.controls["{_c}"] = ControlStatus.NOT_ENFORCED',
                          B08))
MUTATIONS.append(_mut("RS_env_allowlist_widen", "LEGACY", CONT,
    'child_env = {k: os.environ[k] for k in CE_ENV_ALLOWLIST if k in os.environ}',
    'child_env = dict(os.environ)',
    f"{C}::TestRestrictedBackendProperties::test_restricted_env_allowlist_withholds_secret"))

# ── Group 10: executor routing ────────────────────────────────────────────────
MUTATIONS += [
    _mut("EXE_not_failclosed", "BROKER", EXEC,
         'if not outcome.executed:\n            return {"error": outcome.error or "containment unavailable",',
         'if False:\n            return {"error": outcome.error or "containment unavailable",',
         f"{C}::TestExecutorRouting::test_code_execute_fails_closed_when_broker_cannot"),
    _mut("EXE_hardcode_restricted", "BROKER", EXEC,
         'requirement = broker.evaluate_requirement("code_execute", {"code": code})',
         'requirement = ContainmentRequirement.RESTRICTED_OK',
         f"{C}::TestExecutorRouting::test_code_execute_is_sandboxed_when_available"),
    _mut("EXE_bypass_broker", "BROKER", EXEC,
         'outcome = broker.execute(ExecutionRequest(code, timeout, requirement))',
         'from core.containment import RestrictedProcessBackend as _RPB\n        outcome = _RPB().execute(ExecutionRequest(code, timeout, requirement))',
         f"{C}::TestExecutorRouting::test_code_execute_is_sandboxed_when_available"),
    _mut("EXE_drop_receipt", "TRUTH", EXEC,
         'receipt = outcome.receipt.to_dict() if outcome.receipt is not None else {}',
         'receipt = {}',
         f"{F}::TestF6Execution::test_network_isolation_reported_truthfully"),
    _mut("EXE_always_hitl_code", "LEGACY", EXEC,
         '_ALWAYS_HITL_TOOLS: frozenset[str] = frozenset({\n    "code_execute",\n    "run_shell_command",',
         '_ALWAYS_HITL_TOOLS: frozenset[str] = frozenset({\n    "run_shell_command",',
         "tests/test_code_execute_gate.py::test_code_execute_in_always_hitl"),
]

# ── Group 11: legacy invariants + static detector + registry ──────────────────
MUTATIONS += [
    _mut("LEG_m65d_effect_id", "LEGACY", EJ,
         'return _digest(_D_EFFECT, surface, tool_id, identity_scope,\n                   canonical_json(tool_input))',
         'return _digest(_D_EFFECT, surface, tool_id, identity_scope,\n                   "")',
         "tests/test_effect_semantics_v69_m65d.py"),
    _mut("DET_miss_subprocess", "LEGACY", DET,
         '("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),',
         '("subprocess", "call"),',
         f"{S}::test_detector_flags_direct_subprocess"),
    _mut("DET_allow_everything", "LEGACY", DET,
         'def _reviewed(relpath: str, func_dotted: str, funcs: list[str]) -> bool:',
         'def _reviewed(relpath: str, func_dotted: str, funcs: list[str]) -> bool:\n    return True',
         f"{S}::test_detector_flags_direct_subprocess"),
    _mut("DET_coverage_blind", "LEGACY", DET,
         'if arbitrary != covered:',
         'if False:',
         f"{S}::test_coverage_check_flags_a_mismatch"),
    _mut("REG_coverage_counts_all", "LEGACY", REG,
         'if d["arbitrary_code"])',
         'if d["arbitrary_code"] or True)',
         f"{S}::test_coverage_invariant_holds"),
    _mut("REG_code_execute_downgraded", "LEGACY", REG,
         '"disposition": BROKER_REQUIRED,\n        "default_requirement": "SANDBOX_REQUIRED",',
         '"disposition": RESTRICTED_ONLY,\n        "default_requirement": "SANDBOX_REQUIRED",',
         f"{S}::test_coverage_invariant_holds"),
    _mut("LEG_mandatory_shrink", "LEGACY", CONT,
         '    "network_isolation",\n    "host_loopback_isolation",',
         '    "host_loopback_isolation",',
         UNKNOWN_T),
]


# ── Group 12: additional command-policy sub-vectors + fail-closed returns ─────
MUTATIONS += [
    _mut("CMD_git_subcommand_check_off", "LOLBIN", CMD,
         'if subcommand not in _GIT_ALLOWED_SUBCOMMANDS:',
         'if subcommand in ("\\x00never",):',
         CP_GIT),
    _mut("CMD_git_config_write_allowed", "LOLBIN", CMD,
         'if not any(r in ("--get", "--list", "-l", "--get-all", "--get-regexp")',
         'if not any(r in ("--get", "--list", "-l", "--get-all", "--get-regexp", "core.pager")',
         CP_GIT),
    _mut("CMD_wget_execute_allowed", "LOLBIN", CMD,
         'if low in ("-e", "--execute") or low.startswith("--execute="):',
         'if low in ("\\x00never",):',
         CP_WGET),
    _mut("CMD_wget_config_allowed", "LOLBIN", CMD,
         'if low.startswith("--config"):',
         'if low.startswith("\\x00never"):',
         CP_WGET),
    _mut("CMD_injection_loop_disabled", "LOLBIN", CMD,
         'for frag in _GLOBAL_INJECTION_FRAGMENTS:\n            if frag in low:',
         'for frag in ():\n            if frag in low:',
         CP_INJECT),
    _mut("BRK_req_reads_input", "BROKER", CONT,
         'if tool_name == "code_execute":\n            return ContainmentRequirement.SANDBOX_REQUIRED',
         'if tool_input.get("sandbox") is False:\n            return ContainmentRequirement.RESTRICTED_OK\n        if tool_name == "code_execute":\n            return ContainmentRequirement.SANDBOX_REQUIRED',
         f"{C}::TestNoUntrustedDowngrade::test_tool_input_cannot_change_requirement"),
    _mut("BRK_noready_executed_true", "FAILCLOSED", CONT,
         'executed=False,\n                                    error=err or "containment not established",',
         'executed=True,\n                                    error=err or "containment not established",',
         BOOTEXIT),
    _mut("BRK_notverified_executed_true", "FAILCLOSED", CONT,
         'executed=False,\n                                    error="containment not verified; snippet not run",',
         'executed=True,\n                                    error="containment not verified; snippet not run",',
         READY0),
    _mut("LEG_windows_status_sandboxed", "LEGACY", CONT,
         '"status": ("WINDOWS_RESTRICTED_PROCESS_ONLY" if os.name == "nt"\n                       else "NOT_ON_THIS_HOST"),',
         '"status": "WINDOWS_SANDBOXED",',
         B07),
    _mut("CMD_git_execpath_allowed", "LOLBIN", CMD,
         '_GIT_DENIED_GLOBAL_PREFIXES: tuple[str, ...] = (\n    "-c", "-C", "--exec-path",',
         '_GIT_DENIED_GLOBAL_PREFIXES: tuple[str, ...] = (\n    "-c", "-C",',
         CP_GIT_GLOBAL),
    _mut("BW_stdout_cap_huge", "RESOURCE", CONT,
         'CE_STDOUT_CAP = 3000',
         'CE_STDOUT_CAP = 3000 * 100000',
         f"{G}::test_golden_sandbox_scenarios[A27_output_flood]"),
    _mut("CMD_curl_config_allowed", "LOLBIN", CMD,
         'if a.startswith("-K") or low == "--config" or low.startswith("--config="):',
         'if a == "\\x00never":',
         CP_CURL),
    _mut("CMD_execution_capable_bare_allowed", "LOLBIN", CMD,
         'if args and all(a.lower() in _INFO_FLAGS for a in args):\n            return None',
         'if (not args) or all(a.lower() in _INFO_FLAGS for a in args):\n            return None',
         CP_INTERP),
]


# ── Group 13: Round-2 remediation (lab policy + probe/canary/ns non-vacuity) ──
CP_LAB = f"{CP}::TestTrustedLab::test_lab_generic_host_exec_is_blocked"
DEAD_PROBE = f"{OBS}::TestLiveObservation::test_dead_loopback_probe_fails_closed"
PROBE_UNK = f"{OBS}::TestEvidenceDerivation::test_dead_loopback_probe_yields_unknown"
CANARY_UNK = f"{OBS}::TestEvidenceDerivation::test_unestablished_canary_yields_unknown_filesystem"
NETNS_T = f"{OBS}::TestEvidenceDerivation::test_shared_netns_inode_drops_network"
PIDNS_T = f"{OBS}::TestEvidenceDerivation::test_shared_pidns_inode_drops_descendant"

MUTATIONS += [
    # MAJOR 1: the lab exemption / lab arg policy
    _mut("R2_lab_exemption_reinstated", "LOLBIN", EXEC,
         'policy_reason = _command_policy.command_refusal(argv, lab=is_lab_only)',
         'policy_reason = None if is_lab_only else _command_policy.command_refusal(argv, lab=is_lab_only)',
         CP_LAB),
    _mut("R2_tcpdump_z_cluster_allowed", "LOLBIN", CMD,
         'if low.startswith("-") and not low.startswith("--") and "z" in low[1:]:',
         'if False and low.startswith("-") and not low.startswith("--") and "z" in low[1:]:',
         CP_LAB),
    _mut("R3_tcpdump_postrotate_long_allowed", "LOLBIN", CMD,
         'if low.startswith("--postrotate-command"):',
         'if low.startswith("\\x00never"):',
         CP_LAB),
    _mut("R2_tshark_lua_allowed", "LOLBIN", CMD,
         'if low == "-x" or low.startswith("-x") or "lua_script" in low:',
         'if low == "\\x00never":',
         CP_LAB),
    _mut("R2_sqlmap_eval_allowed", "LOLBIN", CMD,
         'if (low in ("--eval", "--alert") or low.startswith("--eval=")',
         'if (low in ("\\x00never",) or low.startswith("\\x00none=")',
         CP_LAB),
    _mut("R4_sqlmap_configfile_allowed", "LOLBIN", CMD,
         'or low.startswith("--alert=") or a.startswith("-c")\n                or low.startswith("--configfile")):',
         'or low.startswith("--alert=") or a.startswith("\\x00n1")\n                or low.startswith("\\x00n2")):',
         CP_LAB),
    _mut("R4_sqlmap_c_uppercase_not_confused", "LOLBIN", CMD,
         'if (low in ("--eval", "--alert") or low.startswith("--eval=")\n                or low.startswith("--alert=") or a.startswith("-c")',
         'if (low in ("--eval", "--alert") or low.startswith("--eval=")\n                or low.startswith("--alert=") or low.startswith("-c")',
         f"{CP}::TestTrustedLab::test_lab_purpose_built_still_works"),
    _mut("R9_sqlmap_tamper_allowed", "LOLBIN", CMD,
         'if len(head) >= 4 and any(name.startswith(head) for name in _EXEC_SCRIPT_OPTS):',
         'if len(head) >= 4 and any(name.startswith("\\x00n") for name in _EXEC_SCRIPT_OPTS):',
         CP_LAB),
    _mut("R2_msf_exec_allowed", "LOLBIN", CMD,
         'if low.startswith("-x") or low.startswith("-r"):',
         'if low.startswith("\\x00n1") or low.startswith("\\x00n2"):',
         CP_LAB),
    _mut("R3_msfconsole_plugin_allowed", "LOLBIN", CMD,
         'if low.startswith("-p"):',
         'if low.startswith("\\x00never"):',
         CP_LAB),
    _mut("R5_msfconsole_module_path_allowed", "LOLBIN", CMD,
         'if a.startswith("-m"):',
         'if a.startswith("\\x00never"):',
         CP_LAB),
    _mut("R8_msfconsole_migration_path_allowed", "LOLBIN", CMD,
         'if a.startswith("-M"):',
         'if a.startswith("\\x00never"):',
         CP_LAB),
    _mut("R10_msfconsole_longopt_abbrev_allowed", "LOLBIN", CMD,
         'if head and any(name.startswith(head) for name in _EXEC_LONG):',
         'if head and any(name.startswith("\\x00n") for name in _EXEC_LONG):',
         CP_LAB),
    _mut("R3_ffuf_input_cmd_allowed", "LOLBIN", CMD,
         'if low == name or low.startswith(name + "="):',
         'if low == name and False:',
         CP_LAB),
    _mut("R11_ffuf_config_allowed", "LOLBIN", CMD,
         '_EXEC = ("-input-cmd", "--input-cmd", "-input-shell", "--input-shell",\n             "-config", "--config")',
         '_EXEC = ("-input-cmd", "--input-cmd", "-input-shell", "--input-shell")',
         CP_LAB),
    _mut("R3_nikto_config_allowed", "LOLBIN", CMD,
         'if len(head) >= 2 and "config".startswith(head):',
         'if False and len(head) >= 2 and "config".startswith(head):',
         CP_LAB),
    _mut("R5_john_config_external_allowed", "LOLBIN", CMD,
         'if len(head) >= 2 and ("config".startswith(head)\n                                or "external".startswith(head)):',
         'if False and len(head) >= 2 and ("config".startswith(head)\n                                or "external".startswith(head)):',
         CP_LAB),
    _mut("R6_msfconsole_config_allowed", "LOLBIN", CMD,
         'if a.startswith("-c"):',
         'if a.startswith("\\x00never"):',
         CP_LAB),
    _mut("R7_hashcat_bridge_parameter_allowed", "LOLBIN", CMD,
         'if low in ("--bridge-parameter1", "--bridge-parameter2",\n                    "--bridge-parameter3", "--bridge-parameter4") or any(',
         'if low in ("\\x00never",) or any(',
         CP_LAB),
    _mut("R2_lab_purpose_built_ungoverned", "LOLBIN", CMD,
         'if cap is CommandCapability.PURPOSE_BUILT_AUTHORIZED:\n            return None\n        policy = _LAB_ARG_POLICIES.get(binary)',
         'if cap is CommandCapability.PURPOSE_BUILT_AUTHORIZED:\n            return None\n        policy = None and _LAB_ARG_POLICIES.get(binary)',
         CP_LAB),
    # MAJOR 2 / F2: host-loopback probe non-vacuity
    _mut("R2_failclosed_guard_disabled", "FAILCLOSED", CONT,
         'if not (probe_live and canary_established and host_netns and host_pidns):',
         'if not (True or probe_live and canary_established and host_netns and host_pidns):',
         DEAD_PROBE),
    _mut("R2_probe_live_ignored", "FAILCLOSED", CONT,
         'not ev.get("loopback_connect", True), verifiable=probe_live)',
         'not ev.get("loopback_connect", True), verifiable=True)',
         PROBE_UNK),
    _mut("R2_probe_verify_live_always_true", "FAILCLOSED", CONT,
         'if self.port == 0 or self._srv is None:\n            self.live = False\n            return False',
         'if False:\n            self.live = False\n            return False\n        self.live = True; return True',
         f"{OBS}::TestEvidenceDerivation::test_verify_live_is_false_for_unbound_probe"),
    # F3: filesystem host-canary non-vacuity
    _mut("R2_canary_verifiable_ignored", "RECEIPT", CONT,
         'verifiable=canary_established)',
         'verifiable=True)',
         CANARY_UNK),
    _mut("R2_canary_read_dropped", "RECEIPT", CONT,
         'and not ev.get("repo", True) and not ev.get("canary_read", True)),',
         'and not ev.get("repo", True) and (True or ev.get("canary_read", True))),',
         DERIV),
    # F4: structural namespace identity
    _mut("R2_netns_identity_dropped", "NETWORK", CONT,
         'ev.get("ifaces") == ["lo"] and netns != "" and netns != host_netns',
         'ev.get("ifaces") == ["lo"] and (netns == netns or netns != host_netns)',
         NETNS_T),
    _mut("R2_pidns_identity_dropped", "PROCESS", CONT,
         '0 < ev.get("proc_count", -1) <= 15 and pidns != "" and pidns != host_pidns',
         '0 < ev.get("proc_count", -1) <= 15 and (pidns == pidns or pidns != host_pidns)',
         PIDNS_T),
    # F5: cleanup default footgun
    _mut("R2_cleanup_default_lax", "TRUTH", CONT,
         'cleanup_ok = cleanup_status is ControlStatus.ENFORCED',
         'cleanup_ok = cleanup_status is not ControlStatus.NOT_ENFORCED',
         f"{C}::TestProfileDerivation::test_cleanup_none_default_is_not_sandboxed"),
]

# ── Group 14: Round-6 remediation (write_file .git/.svn/.hg metadata door) ────
AED = "tests/test_alternate_execution_doors_m66b.py"
MUTATIONS += [
    _mut("R6_vcs_metadata_write_allowed", "PATH", EXEC,
         'if _VCS_METADATA_DIRS & set(p.parts):\n        return None',
         'if False and _VCS_METADATA_DIRS & set(p.parts):\n        return None',
         f"{AED}::test_resolve_within_allowed_refuses_git_config_relative"),
]


def _run() -> int:
    py = sys.executable
    total = len(MUTATIONS)
    survivors: list[str] = []
    anchor_errors: list[str] = []
    detected = 0
    print(f"M66B MUTATION CAMPAIGN (Round-1 rebuild) — {total} mutations\n")
    for m in MUTATIONS:
        path = os.path.join(_ROOT, m["file"])
        original = open(path, encoding="utf-8").read()
        count = original.count(m["find"])
        if count != 1:
            anchor_errors.append(f"{m['id']}: anchor occurs {count}x in {m['file']}")
            print(f"  [ANCHOR ERR] {m['id']:34s} {count}x")
            continue
        mutated = original.replace(m["find"], m["replace"], 1)
        try:
            open(path, "w", encoding="utf-8").write(mutated)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(  # nosec B603 - fixed argv test runner
                [py, "-m", "pytest", "-x", "-q", "--no-header",
                 "-p", "no:cacheprovider", m["test"]],
                cwd=_ROOT, capture_output=True, text=True, env=env, timeout=240)
            if proc.returncode != 0:
                detected += 1
                print(f"  [DETECTED] {m['id']:34s} ({m['cat']})")
            else:
                survivors.append(m["id"])
                print(f"  [SURVIVOR] {m['id']:34s} ({m['cat']}) — NO TEST FAILED")
        finally:
            open(path, "w", encoding="utf-8").write(original)

    print(f"\n{'='*70}")
    print(f"mutations:      {total}")
    print(f"detected:       {detected}")
    print(f"survivors:      {len(survivors)}  {survivors if survivors else ''}")
    print(f"anchor errors:  {len(anchor_errors)}  {anchor_errors if anchor_errors else ''}")
    ok = not survivors and not anchor_errors and total >= 85
    print(f"M66B_MUTATION_CAMPAIGN: {'PASS' if ok else 'FAIL'} "
          f"({detected}/{total} detected, {len(survivors)} survivors)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_run())
