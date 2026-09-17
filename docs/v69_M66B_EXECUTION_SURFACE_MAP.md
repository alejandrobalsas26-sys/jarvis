# V69 M66B — Execution Surface Map (Phase 0)

Source master: `3bd46fbdb06744de5e07485db62050e3e9eedf6b`.
Every execution-capable surface in the repository, discovered by structural
search for `subprocess.run/Popen/call`, `os.system`, `os.exec*`, `os.popen`,
`exec`, `eval`, `multiprocessing`, `shell=True`, `ProcessPoolExecutor`,
`asyncio.create_subprocess_*`, interpreter/shell/PowerShell/native/OCI launches
and worker processes across `core/`, `tools/`, `aura/`, `plugins/`, `brain/`,
`main.py`, `training_gym/`, `mcp_servers/`. `code_execute` is NOT assumed to be
the only relevant surface (§4).

The machine-readable version — the source of the §26 coverage gate — is
`jarvis/data/execution_surface_registry.json`. This document is its rationale.

## Legend — M66B disposition

* **BROKER_REQUIRED** — arbitrary caller-controlled code; MUST route through the
  ContainmentBroker with a declared ContainmentRequirement.
* **RESTRICTED_ONLY** — caller-influenced command, but constrained (allowlist +
  HITL + `shell=False`); arbitrary-code-capable only via allowlisted interpreters.
  Declared with its containment posture; full sandboxing is out of scope because
  the tool's legitimate purpose needs host network/filesystem.
* **FIXED_INTERNAL_EXEMPT** — fixed literal argv (a named binary), not
  caller-controlled arbitrary code; reviewed infrastructure launch.
* **DOCUMENT_ONLY** — belongs to another subsystem with its own containment
  (the science/training plane, which §3 forbids M66B to touch), or is a UI launch.

## A. Tool-handler execution surfaces (the ones the broker/registry govern)

| symbol | file:line | arbitrary code | caller-controlled cmd | RiskClass / HITL | disposition |
|---|---|---|---|---|---|
| `_tool_code_execute` | tools/executor.py:4234 | **YES** (Python snippet) | YES | HIGH_IMPACT / HITL | **BROKER_REQUIRED** — default `SANDBOX_REQUIRED` |
| `_run_contained_python` | tools/executor.py:859 | YES (impl of above) | via code_execute | — | folds into RestrictedProcessBackend |
| `_tool_run_shell_command` | tools/executor.py:3393 | YES via `python f.py`/`node f.js` | YES (allowlist) | HIGH_IMPACT / HITL+NATO | **RESTRICTED_ONLY** (allowlist, `shell=False`, `python -c` blocked) |
| `RedTeamShellExecutor.execute` | tools/executor.py:4569 | YES via interpreters | YES (allowlist + lab) | HIGH_IMPACT / FULL_NATO | **RESTRICTED_ONLY** (trusted-lab gated) |

`code_execute`'s L1/HITL path is unchanged: authority preflight → `classify_tool`
(HIGH_IMPACT) → `_ALWAYS_HITL_TOOLS` challenge → approval-identity binding → effect
journal. M66B inserts the ContainmentBroker between the identity binding and the
subprocess, and nowhere else.

## B. Fixed internal process launches (reviewed registry, FIXED_INTERNAL_EXEMPT)

Literal-argv infrastructure/diagnostics. None run caller-supplied code; where a
value is interpolated (`ip`, `port`, repo URL, path) it is validated and passed as
a `list[str]` element, never a command string. `shell=True` appears NOWHERE.

| symbol | file:line | fixed command |
|---|---|---|
| `AssetDiscovery._run` | core/asset_discovery.py:361,386 | `docker ps`, `vmrun list` |
| `execute_mitigation` | core/auto_remediator.py:186 | `powershell -NoProfile …` |
| `BinaryCapability.version` | core/capabilities.py:135,406 | `<binary> --version` (allowlisted binary) |
| `_check_and_start` | core/dependency_guardian.py:195,274,316 | `ollama serve`, `ollama …` |
| `_clone_and_install` | core/github_explorer.py:211,225,232 | `git clone --depth=1`, `pip install` |
| `hardware_profile` / `hardware_model_profile` | core/hardware_profile.py:99,198; hardware_model_profile.py:187 | `powershell Get-Physical…`, `lscpu`/`sysctl` |
| `_vmrun` | core/lab_manager.py:39 | `vmrun …` |
| `isolate_ip` | core/mitigation.py:138; core/punisher.py:90,121 | `powershell New-NetFirewallRule`, `netsh`/`iptables` |
| `network_quarantine._run` | core/network_quarantine.py:123 | `netsh`/`iptables` (validated IP) |
| `pcap_capture._run_capture` | core/pcap_capture.py:234 | `tshark`/`tcpdump` (validated argv) |
| `persistence_hunter` | core/persistence_hunter.py:78,216,245 | `powershell`, `schtasks /query` |
| `security_auditor` | core/security_auditor.py:276 | `powershell New-NetFirewallRule` |
| `vss_vaccine` | core/vss_vaccine.py:62,117 | `powershell`, `vssadmin list` |
| `windows_hardener` | core/windows_hardener.py:99,143,153,192,219,266 | `powershell`, `netsh` |
| `decoy_filesystem` | core/decoy_filesystem.py:79,90 | `auditpol`, `powershell` |
| `docker_manager._compose_up` | tools/docker_manager.py:230,283 | `docker compose up/down` |
| `forensic_volatility` | tools/forensic_volatility.py:55,105,127 | `vmrun`, `vol` |
| `ghost_hands._execute_step` | tools/ghost_hands.py:128 | launch validated app path |
| `rf_bridge` / `rf_oob` | tools/rf_bridge.py:46,67,96; rf_oob.py:200 | `tshark`, RF tooling |
| `resource_sentinel` | tools/resource_sentinel.py:104 | `vmrun` |
| `vmware_triage_runner` | tools/vmware_triage_runner.py:55 | `vmrun.exe snapshot` |
| `binary_inverter` | tools/binary_inverter.py:22 | `ProcessPoolExecutor` (in-process pool, no exec) |
| `_loop_text` self-test | main.py:881 | `pytest` |
| `packet_tracer_bridge` | mcp_servers/packet_tracer_bridge.py:44 | launch Packet Tracer |
| `_tool_open_application` / `_tool_open_software` / `_tool_packet_tracer_open` | tools/executor.py:3562,3600,3882 | launch a resolved app (UI), `shell=False` |
| `_tool_check_connectivity` / `_tool_whois_lookup` / `_tool_git_query` / `_tool_network_scan` | tools/executor.py:3948,4048,4488,3455 | `ping`/`whois`/`git`/`nmap` (validated argv) |

## C. Science / training plane (DOCUMENT_ONLY — §3 forbids modification)

The training gym has its OWN execution containment for grading model output. It
is the science plane; M66B must not modify it.

| symbol | file:line | note |
|---|---|---|
| grader subprocess + `_kill_tree` | training_gym/graders/base.py:175,192,252 | gym's own timeout+kill for graded code (`nosec B603`) |
| docker backend | training_gym/sandbox/docker_backend.py:72 | gym's own OCI grading backend |
| `model.eval()` | training_gym/evaluation/backends/transformers_peft.py:345 | PyTorch eval mode — NOT `builtins.eval` |

## D. `exec`/`eval` occurrences (all benign, confirmed)

* `tools/executor.py:350-352` — regex PATTERNS that *detect* `eval(`/`exec(`/
  `os.system(` in scanned text (SAST), not calls.
* `core/security_analyzer.py:340-364` — the AST analyzer that *reports* these
  primitives; `eval("2+2")` at :364 is a comment describing a literal it ignores.
* `model.eval()` — PyTorch, §C.

No `builtins.eval`/`builtins.exec` on caller data exists in a tool handler.

## E. Coverage invariant (§26)

```
ARBITRARY_CODE_EXECUTION_SURFACES  ==  CONTAINMENT_POLICY_COVERED_SURFACES
```

The registry enumerates §A as arbitrary-execution surfaces, each with a
containment declaration (BROKER_REQUIRED or RESTRICTED_ONLY). §B is the reviewed
fixed-internal set. A newly added arbitrary-execution path in a tool handler
without a registry declaration fails CI (the coverage test + the static bypass
detector, §27). No implementation precedes this document.
