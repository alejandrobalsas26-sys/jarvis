# V69 M66A.1 — Security Surface Map (Phase 0, pre-implementation)

Source master: `98f18a889b721a6069ef2d76a2aff2a503b9f70d`
Produced from repository reality at branch creation; NO production code was
edited before this document existed (§8).

The map classifies every relevant surface into the four-layer model:

* **L1 Decision & Authority** — SHOULD this be attempted? (`authorize_action`,
  `classify_tool`/`RiskClass`, `_ALWAYS_HITL_TOOLS`, HITL `_challenge`.)
* **L2 Resource Boundaries** — WHAT may it touch? (`_resolve_within_allowed`
  for files; `_http_target_blocked` / `core/url_policy.py` for network.)
* **L3 Execution Containment** — how much power does execution REALLY have?
* **L4 Truth / Audit / Recovery** — what actually happened? (effect journal,
  status enums, packaging/startup truth.)

Handler dispatch: `ToolExecutor` resolves `getattr(self, f"_tool_{name}")`.
Gating: `aexecute()` → authority preflight → `classify_tool` → HITL → effect
protocol → `_aexecute_gated` → handler.

---

## A. FILESYSTEM SURFACES

| symbol | file | capability | effectful | L1 (risk) | L2 today | bypass? | disposition |
|---|---|---|---|---|---|---|---|
| `_tool_read_file` | tools/executor.py:2612 | READ path (multi-format+OCR) | read | READ_ONLY (no HITL) | `_resolve_within_allowed` | none | REUSE |
| `_tool_write_file` | tools/executor.py:3762 | WRITE path | write | HIGH_IMPACT (HITL) | `_resolve_within_allowed` | none | REUSE |
| `_tool_take_screenshot` | tools/executor.py:3294 | WRITE save_path | write | HIGH_IMPACT (HITL) | `_resolve_within_allowed` (3312) | none | REUSE |
| `_tool_list_directory` | tools/executor.py:2735 | LIST dir | read | READ_ONLY (no HITL) | **direct `Path(path).expanduser()`** | **YES** | HARDEN |
| `_tool_leer_archivo_universal` | tools/executor.py:2752 | READ path (multi-format) | read | READ_ONLY (no HITL) | **direct `Path().expanduser().resolve()`** | **YES** | HARDEN |
| `_tool_analizar_codigo_sast` | tools/executor.py:2798 | READ+ANALYZE path | read | READ_ONLY (no HITL) | **direct `Path().expanduser().resolve()`** | **YES** | HARDEN |
| `_tool_hash_file` | tools/executor.py:3965 | READ (hash) path | read | READ_ONLY (no HITL) | **direct `Path().expanduser().resolve()`** | **YES** | HARDEN |
| `_tool_ingest_docs` | tools/executor.py:3630 | INGEST folder → vault | low_impact | LOW_IMPACT (no HITL) | delegates to `KnowledgeVault.ingest_docs` (unverified) | **INVESTIGATE** | HARDEN/DOCUMENT |
| `_tool_desplegar_webapp` | tools/executor.py:3257 | WRITE to tempdir only | write | HIGH_IMPACT (HITL) | fixed `tempfile.mkdtemp` (no user path) | n/a | DOCUMENT_ONLY |

**Key insight:** the four bypassing readers are all **READ_ONLY**, i.e. L1
grants them NO HITL by design. For them L2 is the *only* control between a
model/user-supplied path and the file. A single L2 gap there is a complete
read-primitive escape with no second layer — exactly the class M66A.1 targets.

Canonical gate: `_resolve_within_allowed(path)` (tools/executor.py:455) with
`_sandbox_allowed_dirs() = (~/Downloads, ~/Documents, Path.cwd())` and
`_is_foreign_flavour_path` for Windows-shaped paths on POSIX (§F1, §12).

---

## B. NETWORK SURFACES (arbitrary destination vs fixed service)

| symbol | file | destination | L2 today | bypass? | disposition |
|---|---|---|---|---|---|
| `_tool_http_request` | tools/executor.py:3818 | ARBITRARY (model URL) | `_http_target_blocked` per hop | none for target; **credentials forwarded cross-origin on redirect** | REUSE + HARDEN (F3) |
| `_tool_fetch_webpage` | tools/executor.py:2849 | ARBITRARY (model URL) | **direct `requests.get(url)`** | **YES (SSRF)** | HARDEN |
| `_tool_estudiar_tema` | tools/executor.py:3531 | ARBITRARY (model URL) | **direct `requests.get(url)`** | **YES (SSRF)** | HARDEN |
| `_tool_get_weather` | tools/executor.py:2590 | FIXED host `wttr.in` (city in path, quoted) | host is a literal | low | DOCUMENT_ONLY |
| `_tool_web_search` | tools/executor.py:2834 | duckduckgo library | library-managed | low | DOCUMENT_ONLY |
| `_tool_check_connectivity` | tools/executor.py:3504 | socket to host:port | `_validate_network_target` | n/a (not HTTP) | DOCUMENT_ONLY |
| ai_reverser / health_watchdog / network_quarantine | core/*.py | FIXED service | `core/url_policy.py` (`require_local`) | none | REUSE |

Canonical gate: `_http_target_blocked(url)` (tools/executor.py:596) blocks
loopback/private/link-local/multicast/reserved/unspecified across **every**
resolved address, plus a trusted-lab exception (`_trusted_lab_enabled`).

**Redirect credential handling:** `_tool_http_request` re-validates each hop's
target but forwards the caller's `headers` dict UNCHANGED across origins. There
is no sensitive-header stripping. Destination-safety and credential-safety are
one control today; F3 requires they be separated.

**DNS consistency (F17):** `_http_target_blocked` resolves + checks all A/AAAA
records, then `requests` re-resolves independently — a rebinding TOCTOU window
exists. No address pinning. To be recorded as a measured limitation, not
overclaimed.

---

## C. EXECUTION SURFACES

| symbol | file | creates | L1 | L3 profile today | disposition |
|---|---|---|---|---|---|
| `_tool_code_execute` | tools/executor.py:3786 | `subprocess.run([sys.executable, tmp])` | HIGH_IMPACT (HITL) | **DIRECT_PROCESS**: full inherited env, inherited cwd, no rlimits, network open, no process-tree kill; doc says "isolated subprocess" | HARDEN → RESTRICTED_PROCESS |
| `_tool_run_shell_command` | tools/executor.py:2963 | allowlisted argv (`shell=False`) | HIGH_IMPACT (HITL) | DIRECT_PROCESS, allowlist-constrained | DOCUMENT + profile |
| `_tool_network_scan` | tools/executor.py:3455 | nmap subprocess | HIGH_IMPACT (HITL) | DIRECT_PROCESS | DOCUMENT_ONLY |
| `_tool_open_application`/`open_software` | 3095/3144 | launch app | HIGH_IMPACT (HITL) | DIRECT_PROCESS | DOCUMENT_ONLY |

**F6:** `code_execute`'s docstring calls the subprocess "isolated"; it inherits
the parent environment and cwd, has no CPU/memory/fd/proc-count limits, can open
arbitrary sockets, spawn children that outlive a timeout, and write anywhere the
user can. This is `subprocess != sandbox` in the prompt's exact terms.

---

## D. PACKAGING / DISTRIBUTION SURFACES

| item | file | state | disposition |
|---|---|---|---|
| runtime asset `aura/index.html` | aura/server.py:30 `_INDEX_PATH`; served at `/`,`/ui` FileResponse | **NOT in `[tool.setuptools.package-data].aura` (`templates/*.html`,`static/*`) nor MANIFEST.in** | HARDEN (F8) |
| package-data globs | pyproject.toml:93 `aura=["templates/*.html","static/*"]` | **no `aura/templates/` or `aura/static/` dir exists → glob matches nothing (vacuous)** | HARDEN |
| Dockerfile build context | jarvis/Dockerfile | `COPY . .`, **no `.dockerignore` anywhere** | HARDEN (F4) |
| MANIFEST.in | jarvis/MANIFEST.in | exclude-first; prunes tests/logs/data/state-ish; `check_package_manifest.py` re-verifies | REUSE + extend |
| entry point | pyproject.toml:74 `jarvis = "main:main"` | present | REUSE |

---

## E. STATUS-REPORTING SURFACES

| symbol | file | claim | state | disposition |
|---|---|---|---|---|
| `_harden_defender` | core/windows_hardener.py:204 | Defender real-time protection | returns `None`; `try/except: pass`; missing key defaults to "enabled"; enable not re-queried | HARDEN (F9) |
| `apply_host_hardening` report | core/windows_hardener.py:315 | `report["defender_active"]=True` | **set UNCONDITIONALLY after the check** — the `command(); status=True` anti-pattern | HARDEN |
| adjacent `try/except: pass; state=True` | repo-wide | various | to be audited (§30), classified IN_SCOPE_CRITICAL / FOLLOWUP | AUDIT |

---

## Root-cause dispositions (feeds §10)

* F1 → DUPLICATE_SECURITY_IMPLEMENTATION + POLICY_BYPASS (RESOURCE_BOUNDARY_GAP)
* F2 → POLICY_BYPASS (RESOURCE_BOUNDARY_GAP)
* F3 → RESOURCE_BOUNDARY_GAP (credential dimension of L2)
* F4 → DISTRIBUTION_TRUTH_GAP
* F5 → AUTHORIZATION_REVIEW_GAP
* F6 → EXECUTION_CONTAINMENT_GAP
* F7 → STARTUP_TRUTH_GAP
* F8 → DISTRIBUTION_TRUTH_GAP
* F9 → OBSERVATION_TRUTH_GAP / FALSE_SUCCESS_CLAIM

No implementation precedes this document.
