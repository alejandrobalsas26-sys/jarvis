#!/usr/bin/env python3
"""scripts/mutation_campaign_m66a1.py — V69 M66A.1 mutation campaign (§37/§45).

For each mutation: apply a single, load-bearing weakening to a source file IN
PLACE, run its targeted detector test(s), and require the detector to FAIL
(non-zero exit = the mutation was DETECTED). Then restore the file byte-for-byte.
A mutation the suite does NOT detect is a "load-bearing survivor" — the check it
weakened is not actually guarded, and the campaign fails.

Every file touched is snapshotted at start and restored at the end (and on any
error / SIGINT), so a clean tree is left behind regardless of outcome.

Run:  python scripts/mutation_campaign_m66a1.py
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

_JARVIS = Path(__file__).resolve().parent.parent
_T = "tests/test_four_layer_defense_v69_m66a1.py"

# ── mutation table: (id, relpath, old, new, [detector nodes]) ────────────────
# `old` MUST occur exactly once in the file. The detector must PASS on healthy
# code and FAIL once the mutation is applied.
M = [
    # ── L2 FILE ──────────────────────────────────────────────────────────────
    ("file_containment_always_allow", "tools/executor.py",
     "if p.is_relative_to(allowed.resolve()):", "if True or p.is_relative_to(allowed.resolve()):",
     [f"{_T}::TestF1FilePolicy", f"{_T}::TestCrossLayerProperties::test_A_l1_allow_but_l2_denies_out_of_scope_path"]),
    ("file_gate_none_ignored", "tools/executor.py",
     "    if resolved is None:\n        security_metrics.incr(\"file_policy_denials\")",
     "    if False:\n        security_metrics.incr(\"file_policy_denials\")",
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied"]),
    ("file_windows_flavour_removed", "tools/executor.py",
     "    if _is_foreign_flavour_path(path):", "    if False and _is_foreign_flavour_path(path):",
     [f"{_T}::TestF1FilePolicy::test_windows_flavour_path_denied_on_posix"]),
    ("file_hash_bypasses_gate", "tools/executor.py",
     "        p, denied = _gate_path(path, FileIntent.HASH)",
     "        p, denied = (Path(path).expanduser().resolve(), None)",
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied[hash_file-path]"]),
    ("file_leer_bypasses_gate", "tools/executor.py",
     "        p, denied = _gate_path(filepath, FileIntent.READ)",
     "        p, denied = (Path(filepath).expanduser().resolve(), None)",
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied[leer_archivo_universal-filepath]"]),
    ("file_list_bypasses_gate", "tools/executor.py",
     "        p, denied = _gate_path(path, FileIntent.LIST)",
     "        p, denied = (Path(path).expanduser(), None)",
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied[list_directory-path]"]),
    ("file_sast_bypasses_gate", "tools/executor.py",
     "        p, denied = _gate_path(filepath, FileIntent.ANALYZE)",
     "        p, denied = (Path(filepath).expanduser().resolve(), None)",
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied[analizar_codigo_sast-filepath]"]),
    ("file_ingest_skips_gate", "tools/executor.py",
     "        if folder_path:\n            _p, denied = _gate_path(folder_path, FileIntent.INGEST)",
     "        if False:\n            _p, denied = _gate_path(folder_path, FileIntent.INGEST)",
     [f"{_T}::TestF1FilePolicy::test_ingest_docs_out_of_sandbox_denied"]),
    ("file_write_bypasses_gate", "tools/executor.py",
     "        p = _resolve_within_allowed(path)\n        if p is None:\n            logger.warning(\"Intento de escritura bloqueado (fuera del sandbox).\")",
     "        p = Path(path).expanduser().resolve()\n        if False:\n            logger.warning(\"Intento de escritura bloqueado (fuera del sandbox).\")",
     [f"{_T}::TestMoreFileSurfaces::test_write_file_out_of_sandbox_denied"]),
    ("file_coverage_registry_emptied", "tools/executor.py",
     "FILE_CAPABLE_TOOLS: dict[str, tuple[str, FileIntent]] = {\n    \"read_file\": (\"path\", FileIntent.READ),",
     "FILE_CAPABLE_TOOLS: dict[str, tuple[str, FileIntent]] = {\n    \"XXread_file\": (\"path\", FileIntent.READ),",
     [f"{_T}::TestFileCapabilityCoverage"]),

    # ── L2 NETWORK ───────────────────────────────────────────────────────────
    # is_private (in Python's ipaddress) SUBSUMES loopback/link-local/unspecified/
    # reserved for those addresses, so removing is_private is the load-bearing IPv4
    # mutation (10.x is caught ONLY by is_private); the redundant flags are
    # defence-in-depth, not independently load-bearing — a truthful campaign does
    # not claim otherwise. The whole-guard removal below covers the class outright.
    ("net_private_allowed", "tools/executor.py",
     "ip.is_private or ip.is_loopback or ip.is_link_local",
     "False or ip.is_loopback or ip.is_link_local",
     [f"{_T}::TestF2SSRF::test_target_blocked_by_gate[http://10.0.0.1/x]"]),
    ("net_ssrf_guard_removed", "tools/executor.py",
     "        if (\n            ip.is_private or ip.is_loopback or ip.is_link_local\n            or ip.is_multicast or ip.is_reserved or ip.is_unspecified\n        ):",
     "        if (\n            False and (ip.is_private or ip.is_loopback or ip.is_link_local\n            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)\n        ):",
     [f"{_T}::TestF2SSRF::test_target_blocked_by_gate"]),
    ("net_scheme_gate_removed", "tools/executor.py",
     '    if parsed.scheme not in ("http", "https"):', '    if False:',
     [f"{_T}::TestF2SSRF::test_non_http_scheme_blocked"]),
    ("cred_cookie2_kept", "tools/executor.py",
     '    "authorization", "proxy-authorization", "cookie", "cookie2", "x-api-key",',
     '    "authorization", "proxy-authorization", "cookie", "x-api-key",',
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header[Cookie2]"]),
    ("net_dns_resolution_ignored", "tools/executor.py",
     "                candidates.add(sockaddr[0])", "                pass  # mut",
     [f"{_T}::TestMoreNetwork::test_hostname_resolving_to_private_blocked"]),
    ("net_redirect_hop_not_revalidated", "tools/executor.py",
     "        block = _http_target_blocked(current_url)", "        block = None",
     [f"{_T}::TestF2SSRF::test_arbitrary_http_surface_blocks_loopback",
      f"{_T}::TestF2SSRF::test_redirect_to_internal_blocked"]),
    ("net_redirect_hops_unbounded", "tools/executor.py",
     "    for _hop in range(max_redirects + 1):", "    for _hop in range(max_redirects + 40):",
     [f"{_T}::TestMoreNetwork::test_redirect_loop_bounded"]),
    ("net_fetch_webpage_surface", "tools/executor.py",
     "            resp, meta = _safe_http_fetch(\n                \"GET\", url, headers={\"User-Agent\": \"Mozilla/5.0\"}, timeout=10)",
     "            import requests as _rq\n            resp = _rq.get(url); meta={'error':None,'final_url':url,'sensitive_headers_stripped':0}",
     [f"{_T}::TestF2SSRF::test_arbitrary_http_surface_blocks_loopback[fetch_webpage]"]),

    # ── CREDENTIAL ───────────────────────────────────────────────────────────
    ("cred_authorization_kept", "tools/executor.py",
     "    \"authorization\", \"proxy-authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     "    \"proxy-authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header[Authorization]"]),
    ("cred_cookie_kept", "tools/executor.py",
     "    \"authorization\", \"proxy-authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     "    \"authorization\", \"proxy-authorization\", \"cookie2\", \"x-api-key\",",
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header[Cookie]"]),
    ("cred_apikey_kept", "tools/executor.py",
     "    \"authorization\", \"proxy-authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     "    \"authorization\", \"proxy-authorization\", \"cookie\", \"cookie2\",",
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header[X-API-Key]"]),
    ("cred_proxyauth_kept", "tools/executor.py",
     "    \"authorization\", \"proxy-authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     "    \"authorization\", \"cookie\", \"cookie2\", \"x-api-key\",",
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header[Proxy-Authorization]"]),
    ("cred_never_strips_crossorigin", "tools/executor.py",
     "    if _same_origin(from_url, to_url):\n        return dict(headers), 0",
     "    if True:\n        return dict(headers), 0",
     [f"{_T}::TestF3Credentials::test_cross_origin_strips_each_sensitive_header",
      f"{_T}::TestF3Credentials::test_end_to_end_redirect_strips_credentials"]),
    ("cred_origin_comparison_broken", "tools/executor.py",
     "    return _http_origin(a) == _http_origin(b)", "    return True",
     [f"{_T}::TestF3Credentials::test_cross_origin_by_scheme_and_port"]),

    # ── L1 AUTHORITY / APPROVAL ──────────────────────────────────────────────
    ("l1_hitl_disabled", "tools/executor.py",
     "        must_challenge = requires_hitl(risk_class)", "        must_challenge = False",
     [f"{_T}::TestExecutorGateIntegration::test_p7_hitl_denied_effect_boundary_never_reached"]),
    ("l1_binding_ignored", "tools/executor.py",
     "            if not tool_approval.bind_matches(", "            if False and not tool_approval.bind_matches(",
     [f"{_T}::TestExecutorGateIntegration::test_p8_approval_identity_mismatch_blocks"]),
    ("l1_bind_matches_always_true", "core/tool_approval.py",
     "    return descriptor.call_digest == call_digest(tool, effective_call)", "    return True",
     [f"{_T}::TestF5F19Approval::test_binding_rejects_mutation"]),
    ("l1_call_digest_constant", "core/tool_approval.py",
     "    material = _canonical_json({\"tool\": tool, \"args\": effective_call})",
     "    material = \"CONSTANT\"",
     [f"{_T}::TestF5F19Approval::test_200char_collision_distinguished",
      f"{_T}::TestF5F19Approval::test_binding_rejects_mutation"]),
    ("l1_redaction_disabled", "core/tool_approval.py",
     "        if _is_sensitive(k):\n            raw = v if isinstance(v, str) else _canonical_json(v)",
     "        if False:\n            raw = v if isinstance(v, str) else _canonical_json(v)",
     [f"{_T}::TestF5F19Approval::test_secret_redacted_in_descriptor"]),

    # ── L3 EXECUTION ─────────────────────────────────────────────────────────
    ("l3_env_inherited", "tools/executor.py",
     "        cwd=workdir, env=child_env, stdout=subprocess.PIPE",
     "        cwd=workdir, env=None, stdout=subprocess.PIPE",
     [f"{_T}::TestF6Execution::test_env_not_inherited"]),
    ("l3_cwd_not_isolated", "tools/executor.py",
     "        cwd=workdir, env=child_env, stdout=subprocess.PIPE",
     "        cwd=None, env=child_env, stdout=subprocess.PIPE",
     [f"{_T}::TestF6Execution::test_cwd_isolated"]),
    ("l3_memory_limit_removed", "tools/executor.py",
     "            resource.setrlimit(resource.RLIMIT_AS, (_CE_MEM_BYTES, _CE_MEM_BYTES))",
     "            resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))",
     [f"{_T}::TestF6Execution::test_memory_limit_enforced"]),
    ("l3_child_cleanup_removed", "tools/executor.py",
     "                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)",
     "                pass  # mut: no tree kill",
     [f"{_T}::TestF6Execution::test_child_process_tree_killed"]),
    ("l3_timeout_control_unreported", "tools/executor.py",
     "            returncode = proc.returncode\n            report.controls[\"wall_timeout\"] = ControlStatus.ENFORCED",
     "            returncode = proc.returncode\n            report.controls[\"wall_timeout\"] = ControlStatus.NOT_ENFORCED",
     [f"{_T}::TestExecutionProfileClassifier::test_timeout_control_reported_enforced"]),
    ("l3_profile_false_sandbox", "core/execution_profile.py",
     "        if baseline_all:\n            return ExecutionProfile.RESTRICTED_PROCESS",
     "        if baseline_all:\n            return ExecutionProfile.SANDBOXED",
     [f"{_T}::TestF6Execution::test_profile_is_restricted_not_direct"]),
    ("l3_classify_no_downgrade", "core/execution_profile.py",
     "            if self.network_isolation is ControlStatus.ENFORCED and baseline_all:\n                return ExecutionProfile.SANDBOXED\n            return (ExecutionProfile.RESTRICTED_PROCESS if baseline_all\n                    else ExecutionProfile.DIRECT_PROCESS)",
     "            return ExecutionProfile.SANDBOXED",
     [f"{_T}::TestExecutionProfileClassifier::test_false_sandbox_downgraded"]),

    # ── L4 TRUTH / STATUS ────────────────────────────────────────────────────
    ("l4_nostdout_to_active", "core/windows_hardener.py",
     "        return scs.unknown(\"defender\", _DEFENDER_SOURCE, \"no_stdout\")",
     "        return scs.active(\"defender\", _DEFENDER_SOURCE, \"no_stdout\")",
     [f"{_T}::TestF9StatusTruth::test_unobservable_is_unknown_never_active[-no_stdout]"]),
    ("l4_malformed_to_active", "core/windows_hardener.py",
     "        return scs.unknown(\"defender\", _DEFENDER_SOURCE, \"malformed_json\")",
     "        return scs.active(\"defender\", _DEFENDER_SOURCE, \"malformed_json\")",
     [f"{_T}::TestF9StatusTruth::test_unobservable_is_unknown_never_active[not json{{-malformed_json]"]),
    ("l4_field_absent_to_active", "core/windows_hardener.py",
     "    if enabled is True:", "    if enabled is not False:",
     [f"{_T}::TestF9StatusTruth::test_unobservable_is_unknown_never_active"]),
    ("l4_command_issued_is_success", "core/windows_hardener.py",
     "        requeried = _query_defender_realtime()\n        requeried.attempted_change = True",
     "        requeried = scs.active(\"defender\", _DEFENDER_SOURCE, \"assumed\")\n        requeried.attempted_change = True",
     [f"{_T}::TestDefenderRequery::test_inactive_then_enable_requeries"]),
    ("l4_is_active_always_true", "core/security_control_state.py",
     "        return self.state is SecurityControlState.ACTIVE", "        return True",
     [f"{_T}::TestF9StatusTruth::test_unobservable_is_unknown_never_active",
      f"{_T}::TestF9StatusTruth::test_inactive_is_inactive"]),

    # ── PACKAGING ────────────────────────────────────────────────────────────
    ("pkg_manifest_no_index", "scripts/check_package_manifest.py",
     'REQUIRED_FRAGMENTS = ("core/", "tools/", "main.py", "aura/index.html")',
     'REQUIRED_FRAGMENTS = ("core/", "tools/", "main.py")',
     [f"{_T}::TestF8PackagingDeclarations::test_manifest_checker_requires_index_html"]),
    ("pkg_pyproject_no_index", "pyproject.toml",
     'aura = ["index.html", "*.html", "templates/*.html", "static/*"]',
     'aura = ["templates/*.html", "static/*"]',
     [f"{_T}::TestF8PackagingDeclarations::test_pyproject_declares_index_html"]),
    ("pkg_manifest_no_aura_html", "MANIFEST.in",
     "recursive-include aura *.html", "recursive-include aura *.nonexistent",
     [f"{_T}::TestF8PackagingDeclarations::test_manifest_includes_aura_html"]),

    # ── DOCKER ───────────────────────────────────────────────────────────────
    ("docker_env_allowed", ".dockerignore", "\n.env\n", "\n#.env\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[.env]"]),
    ("docker_logs_allowed", ".dockerignore", "\nlogs\n", "\n#logs\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[logs]"]),
    ("docker_db_allowed", ".dockerignore", "\n*.db\n", "\n#*.db\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[*.db]"]),
    ("docker_git_allowed", ".dockerignore", "\n.git\n", "\n#.git\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[.git]"]),
    ("docker_pycache_allowed", ".dockerignore", "\n__pycache__\n", "\n#__pycache__\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[__pycache__]"]),
    ("docker_telemetry_allowed", ".dockerignore",
     "\ncore/telemetry_keys.json\n", "\n#core/telemetry_keys.json\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[core/telemetry_keys.json]"]),

    # ── EXTRA L2 NETWORK ─────────────────────────────────────────────────────
    # 224.0.0.1 is caught ONLY by is_multicast (is_private=False), so this flag IS
    # load-bearing — unlike is_reserved/is_unspecified which is_private subsumes.
    ("net_multicast_allowed", "tools/executor.py",
     "or ip.is_multicast or ip.is_reserved or ip.is_unspecified",
     "or False or ip.is_reserved or ip.is_unspecified",
     [f"{_T}::TestF2SSRF::test_target_blocked_by_gate[http://224.0.0.1/x]"]),
    ("net_estudiar_surface", "tools/executor.py",
     '            resp, meta = _safe_http_fetch(\n                "GET", url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)',
     '            import requests as _rq\n            resp = _rq.get(url); meta={"error":None,"final_url":url,"sensitive_headers_stripped":0}',
     [f"{_T}::TestF2SSRF::test_arbitrary_http_surface_blocks_loopback[estudiar_tema]"]),
    ("file_read_bypasses_gate", "tools/executor.py",
     '        p = _resolve_within_allowed(path)\n        if p is None:\n            logger.warning("Intento de lectura bloqueado (fuera del sandbox).")',
     '        p = Path(path).expanduser().resolve()\n        if False:\n            logger.warning("Intento de lectura bloqueado (fuera del sandbox).")',
     [f"{_T}::TestF1FilePolicy::test_out_of_sandbox_denied[read_file-path]"]),

    # ── EXTRA L3 ─────────────────────────────────────────────────────────────
    ("l3_cpu_limit_removed", "tools/executor.py",
     "            resource.setrlimit(resource.RLIMIT_CPU, (_CE_CPU_SECONDS, _CE_CPU_SECONDS + 1))",
     "            pass  # mut: no cpu limit",
     [f"{_T}::TestMoreExecutionLimits::test_cpu_limit_enforced"]),
    ("l3_fsize_limit_removed", "tools/executor.py",
     "            resource.setrlimit(resource.RLIMIT_FSIZE, (_CE_FSIZE_BYTES, _CE_FSIZE_BYTES))",
     "            resource.setrlimit(resource.RLIMIT_FSIZE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))",
     [f"{_T}::TestMoreExecutionLimits::test_fsize_limit_enforced"]),

    # ── EXTRA L4 STATUS ──────────────────────────────────────────────────────
    ("l4_access_denied_to_active", "core/windows_hardener.py",
     '            return scs.unknown("defender", _DEFENDER_SOURCE, "access_denied")',
     '            return scs.active("defender", _DEFENDER_SOURCE, "access_denied")',
     [f"{_T}::TestMoreStatusReasons::test_access_denied_is_unknown"]),
    ("l4_nonzero_to_active", "core/windows_hardener.py",
     '        return scs.unknown("defender", _DEFENDER_SOURCE, "nonzero_exit")',
     '        return scs.active("defender", _DEFENDER_SOURCE, "nonzero_exit")',
     [f"{_T}::TestMoreStatusReasons::test_nonzero_exit_is_unknown"]),
    ("l4_timeout_to_active", "core/windows_hardener.py",
     '        return scs.unknown("defender", _DEFENDER_SOURCE, "timeout")',
     '        return scs.active("defender", _DEFENDER_SOURCE, "timeout")',
     [f"{_T}::TestMoreStatusReasons::test_timeout_is_unknown"]),
    ("l4_command_unavailable_to_active", "core/windows_hardener.py",
     '        return scs.unknown("defender", _DEFENDER_SOURCE, "command_unavailable")',
     '        return scs.active("defender", _DEFENDER_SOURCE, "command_unavailable")',
     [f"{_T}::TestF9StatusTruth::test_command_unavailable_is_unknown"]),

    # ── EXTRA DOCKER CANARIES ────────────────────────────────────────────────
    ("docker_envglob_allowed", ".dockerignore", "\n.env.*\n", "\n#.env.*\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[.env.*]"]),
    ("docker_data_allowed", ".dockerignore", "\ndata\n", "\n#data\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[data]"]),
    ("docker_state_allowed", ".dockerignore", "\nstate\n", "\n#state\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[state]"]),
    ("docker_training_allowed", ".dockerignore", "\ntraining\n", "\n#training\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[training]"]),
    ("docker_evaluation_allowed", ".dockerignore", "\nevaluation\n", "\n#evaluation\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[evaluation]"]),
    ("docker_venv_allowed", ".dockerignore", "\n.venv\n", "\n#.venv\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[.venv]"]),
    ("docker_sqlite_allowed", ".dockerignore", "\n*.sqlite*\n", "\n#*.sqlite*\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[*.sqlite*]"]),
    ("docker_tests_allowed", ".dockerignore", "\ntests\n", "\n#tests\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[tests]"]),
    ("docker_logglob_allowed", ".dockerignore", "\n*.log\n", "\n#*.log\n",
     ["tests/test_docker_and_startup_v69_m66a1.py::TestDockerignoreDeclaration::test_excludes_canary[*.log]"]),

    # ── ROUND-1 REMEDIATION (load-bearing checks for the F1/F2/F7 fixes) ──────
    ("file_list_pattern_ungated", "tools/executor.py",
     "            if _resolve_within_allowed(str(item)) is None:\n                continue",
     "            if False:\n                continue",
     [f"{_T}::TestRound1Fixes::test_f1_list_directory_pattern_traversal_contained",
      f"{_T}::TestRound1Fixes::test_f1_absolute_pattern_target_contained"]),
    ("cred_embedded_secret_not_scrubbed", "core/tool_approval.py",
     "    return scrubbed", "    return value",
     [f"{_T}::TestRound1Fixes::test_f2_embedded_url_secret_redacted",
      f"{_T}::TestRound1Fixes::test_f2_command_embedded_secret_redacted"]),
    ("l4_nondict_json_to_active", "core/windows_hardener.py",
     "    if not isinstance(status, dict):", "    if False:",
     [f"{_T}::TestRound1Fixes::test_f7_defender_non_dict_json_is_unknown"]),

    # ── LEGACY PRESERVATION (M65D / M66A) ────────────────────────────────────
    ("legacy_m65d_identity_idempotency", "tools/executor.py",
     "    effective.pop(_IDEMPOTENCY_ARG, None)", "    pass  # mut",
     ["tests/test_effect_semantics_v69_m65d.py -x"]),
    ("legacy_m66a_strengthen_noop", "core/epistemic_deliberation.py",
     "        if not moved:\n            # Nothing rose.",
     "        if False:\n            # Nothing rose.",
     ["tests/test_epistemic_deliberation_v69_m66a.py -x"]),
]


def _snapshot(paths):
    return {p: (_JARVIS / p).read_bytes() for p in paths}


def _restore(snap):
    for p, b in snap.items():
        (_JARVIS / p).write_bytes(b)


def main() -> int:
    mutations = [m for m in M if m is not None and m[2] is not None]
    files = sorted({m[1] for m in mutations})
    snap = _snapshot(files)
    atexit.register(_restore, snap)

    detected, survivors, errors = [], [], []
    for mid, rel, old, new, detectors in mutations:
        path = _JARVIS / rel
        text = path.read_text()
        n = text.count(old)
        if n != 1:
            errors.append((mid, f"anchor occurs {n}x (need 1)"))
            continue
        try:
            path.write_text(text.replace(old, new))
            # Rapid same-second rewrites of one module make CPython's timestamp-based
            # .pyc invalidation serve a STALE compiled copy intermittently (a mutated
            # source with an unchanged-second mtime looked identical to the cached
            # pyc), which made a genuinely-detected mutation report as a flaky
            # survivor. Force a source recompile: bump mtime clear of the 1s window
            # and forbid the child from reading/writing bytecode.
            future = time.time() + 10
            os.utime(path, (future, future))
            child_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            args = []
            for d in detectors:
                args += d.split(" ")
            rc = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--no-header", *args],
                cwd=str(_JARVIS), capture_output=True, text=True, timeout=300,
                env=child_env).returncode
        finally:
            path.write_text(text)
        if rc != 0:
            detected.append(mid)
            print(f"  DETECTED   {mid}")
        else:
            survivors.append(mid)
            print(f"  SURVIVOR   {mid}   <-- LOAD-BEARING SURVIVOR")

    _restore(snap)
    print("\n" + "=" * 60)
    print(f"MUTATIONS RUN:        {len(mutations)}")
    print(f"DETECTED:             {len(detected)}")
    print(f"LOAD-BEARING SURVIVORS: {len(survivors)}  {survivors}")
    print(f"ANCHOR ERRORS:        {len(errors)}  {errors}")
    print("=" * 60)
    return 0 if (not survivors and not errors and len(mutations) >= 70) else 1


if __name__ == "__main__":
    sys.exit(main())
