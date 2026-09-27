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
| `_tool_run_shell_command` | tools/executor.py | **NO** (closed §4) — was YES via `python f.py`/`-m`/`node`/`npm run`/`make` | YES (allowlist) | HIGH_IMPACT / HITL+NATO | **RESTRICTED_ONLY**, explicitly incapable |
| `RedTeamShellExecutor.execute_shell` | tools/executor.py | **NO** (closed §4) — routes through the same `_validate_command` | YES (allowlist + lab) | HIGH_IMPACT / FULL_NATO | **RESTRICTED_ONLY**, explicitly incapable |

`code_execute`'s L1/HITL path is unchanged: authority preflight → `classify_tool`
(HIGH_IMPACT) → `_ALWAYS_HITL_TOOLS` challenge → approval-identity binding → effect
journal. M66B inserts the ContainmentBroker between the identity binding and the
subprocess, and nowhere else.

### A.1 — The SECOND arbitrary-code door (recovery audit, §3/§4)

The recovery audit found that `run_shell_command` and
`RedTeamShellExecutor.execute_shell` — both RESTRICTED_ONLY — could execute
**caller-controlled arbitrary code** through allowlisted interpreters:
`python attacker.py`, `python -m evil`, `node a.js`, `npm run evil`, bare `make`
(runs the default Makefile target). HITL/NATO are AUTHORITY, not L3 containment,
so this was a way around the SANDBOX_REQUIRED broker.

**Closed executably (not by a label):** `_forbidden_interpreter_exec` in
`_validate_command` (the single validator both gateways use) now refuses any
interpreter/script-runner invocation carrying a script, a module or inline code;
only informational flags (`--version`, `--help`) pass. Fixed diagnostic commands
(nmap, git, ping, curl, ls, whois) are unaffected. Both surfaces are therefore
`arbitrary_code=False` in the registry. Arbitrary code has exactly one door:
`code_execute` → the SANDBOX_REQUIRED broker.

`ALTERNATE_ARBITRARY_EXECUTION_AUDIT`:
* `code_execute` — arbitrary YES, brokered YES (SANDBOX_REQUIRED).
* `run_shell_command` — arbitrary **NO** after §4 (was: `python file.py`,
  `python -m mod`, `node file.js`, `npm run x`, bare `make`); now blocked.
* `red_team_shell` — arbitrary **NO** after §4 (same forms, same validator);
  lab allowlist adds offensive binaries, never interpreters running caller code.

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

Hardened by the recovery audit (§4): the invariant is now stronger than "each
surface has *a* declaration" — **every arbitrary-code surface must be
BROKER_REQUIRED**. A RESTRICTED_ONLY surface is acceptable only when it is
`arbitrary_code=False` (explicitly incapable). So both sets equal exactly
`{code_execute}`. §B is the reviewed fixed-internal set. A newly added
arbitrary-execution path in a tool handler
without a registry declaration fails CI (the coverage test + the static bypass
detector, §27). No implementation precedes this document.

## F. Known limitation — the static bypass detector's file scope (Round-3)

A fresh, independent Round-2 review of the gen41 frozen candidate (Round-3
remediation) found that `scripts/check_execution_surfaces.py`'s static bypass
detector (§27) scans only `tools/executor.py`. §B's other ~26 files are
declared "reviewed" in `FIXED_INTERNAL_SURFACES`, but that label is **asserted
by this document and the registry, not machine-verified by CI** — a newly added
ungoverned `subprocess`/`os.system`/`shell=True` call in any file *other than*
`tools/executor.py` currently passes CI undetected.

Broadening the scanner's `_TOOL_FILES` to the full §B file set surfaces 22
additional execution primitives across 12 files (mostly Windows
hardening/forensics/RF tooling) that have never been individually reviewed
against this document's granularity (`file:function`, one entry per file today
where several of these files have five or six distinct call sites). None of
these are on the LLM-tool-calling surfaces M66B governs (`code_execute`,
`run_shell_command`, `red_team_shell`); auditing them file-by-file is a
separate, bounded piece of work this milestone does not close.

One item surfaced in passing and not exploited: `core/github_explorer.py`'s
`_clone_and_install` runs an unconfirmed `pip install` on any GitHub repo
matched by a search query, reached via a voice-macro path rather than an LLM
tool-calling surface — pre-existing, out of M66B's declared scope, flagged for
separate review rather than fixed here.

## G. Known limitation — a pre-existing, unrelated over-block in Layer 2 (Round-4)

A fresh Round-2 review of the gen42 frozen candidate found that
`tools/executor.py:_build_system_dirs` (§155) unconditionally includes
`Path("/").resolve()` in `_SYSTEM_DIRS`. Because root is an ancestor of every
non-root absolute path, `_validate_command`'s Layer-2 path check (§969) refuses
almost any argument that resolves to an absolute path containing at least one
`/` — confirmed live: `curl http://example.com/`, `curl -s
https://example.com/path` and `wget http://example.com/file.txt` are all
refused with "apunta a un directorio del sistema", while `curl example.com`
(no slash) is allowed. This pre-dates M66B and is orthogonal to the
command-SEMANTIC policy (§4); it fails CLOSED (over-restrictive, not a new
escape) rather than open, so it is not a new security hole and is not fixed
here. It is noted because it INCIDENTALLY masked test coverage for some of
this document's own findings — several Round-3/4 payload strings that happened
to contain an absolute path were blocked by this bug rather than by the
command-policy denier under test, which is why every regression test added in
Round-3/4 deliberately uses slash-free payload names. Whether `run_shell_command`
usage of curl/wget/find/git with real URLs or filesystem paths works at all
today is a question for the owning team; it is out of M66B's declared
containment/isolation scope to fix.

## H. Known limitation — `open_application` uncurated fallback (Round-11)

A fresh independent review of the gen49 frozen candidate noted (as an
observation, not scored BLOCKER/MAJOR) that `tools/executor.py:
_tool_open_application`, when the requested name is not in the curated
`APP_MAP`, falls back to launching any bare PATH executable matching
`^[a-zA-Z0-9._-]+$` with no arguments (`shell=False`). This surface is NOT one
of the two caller-argv gateways M66B governs (`run_shell_command`,
`RedTeamShellExecutor.execute_shell`), never consults `core.command_policy`,
and launching a program is the app-launcher's declared capability. It is
recorded here as follow-up engineering debt — the uncurated fallback is broader
than the curated map (e.g. a bare `make` would run a Makefile in the process
CWD) — rather than claimed closed or silently widened in scope. Narrowing the
fallback to the curated map is a separate, bounded change outside M66B.

## I. Known limitations carried forward from the Round-17 review

A fresh independent review of the gen55 frozen candidate raised two items it
explicitly declined to score, having been unable to reproduce either on this
host. Both are recorded here as follow-up engineering debt rather than claimed
closed, and neither is a containment claim M66B makes.

**I.1 — `_VCS_METADATA_DIRS` is a case-sensitive component match.**
`tools/executor.py: _resolve_within_allowed` refuses a path whose components
intersect `frozenset({".git", ".svn", ".hg"})`. The comparison is exact and
case-sensitive. On this Linux/ext4 host that is complete, because `.GIT` and
`.git` are distinct directories and only the latter is git's metadata directory.
On a case-insensitive filesystem (APFS by default, NTFS) a differently-cased
component would name the same directory while failing this match. The reviewer
could obtain no live evidence either way here, and M66B targets this host, so
this is NOT scored as a defect — but any port of this gate to such a filesystem
must fold case before comparing. The fix is one `.lower()`; it is deliberately
not applied blind, because a fix with no failing test to prove it is not
evidence.

**I.2 — `RedTeamShellExecutor._classify` parses in non-POSIX mode.**
`_classify` uses `shlex.split(command, posix=False)` while `_validate_command`
(the policy gate) and the execution path both re-derive their argv from a
POSIX-mode `shlex.split` of the same immutable string. A parsing-mode mismatch
therefore exists, but it reaches only the cosmetic trust/challenge-tier label —
never the argv that is policy-checked or executed. Its failure direction is
over-refusal and extra operator friction, not a bypass. Recorded so the mismatch
is on the record and does not later get "fixed" by making the POLICY gate use
the non-POSIX parse, which would be the dangerous direction.

**What §28 still excludes.** Neither item above, nor anything in §F/§G/§H,
constitutes a claim of repository-wide execution-surface closure. The detector's
file scope (§F), non-tool-calling installation/execution paths repository-wide,
and the Layer-2 absolute-path over-restriction (§G) remain open follow-up work.
