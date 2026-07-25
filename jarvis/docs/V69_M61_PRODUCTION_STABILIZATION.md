# JARVIS V69 M61 — Production Stabilization, CI & Release Engineering

**Branch:** `jarvis-v69-m61-production-stabilization`
**Base:** `df35289d` (V69 M60, merged to `master`) — verified ancestor
**Status:** complete, pushed, awaiting review. Not merged automatically.
**Canonical version:** `69.61.0` (`core/version.py`)

---

## 1. Why M61 adds no feature subsystem

M52–M60 built capability at a rate the repository's *engineering surface* did not
keep up with. The result was a codebase whose runtime was well-tested and whose
packaging, versioning, dependency story and documentation were not tested at all.
Concretely, at `df35289d`:

- package metadata declared version `63.0.0`, six milestones behind;
- `JARVIS.md` described "a complete autonomous Purple Team security platform" that
  "attacks autonomously", contradicting the HITL/NATO gating the code enforces;
- three dependency lists were maintained independently, and had drifted;
- the project had **never been built** — `python -m build` would have failed;
- the DEBUG log was written to a CWD-relative path, producing one 2.7 MB log per
  directory the app had been launched from;
- the boot sequence ran `pip install --break-system-packages` on the operator's host;
- the CI workflow's audit gate ended in `|| true`.

None of that is fixed by adding a subsystem. M61 adds **no new agent, model, security
subsystem or autonomous capability**. Every change either establishes a source of
truth, verifies an existing claim, or removes an unrequested side effect.

---

## 2. Canonical version source (M61.1)

`core/version.py` is the single authority. It is pure stdlib with **no imports from
the rest of `core`**, so a build backend can read it before dependencies exist.

```
GENERATION = 69   MILESTONE = 61   PATCH = 0   ->   VERSION = "69.61.0"
```

`MAJOR.MINOR.PATCH` = generation.milestone.patch — the scheme the repository already
used implicitly (V63 → `63.0.0`), now explicit and checkable.

Everything derives:

| Consumer | How |
|---|---|
| `pyproject.toml` | `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "core.version.VERSION"}` |
| wheel / sdist | build output is `jarvis-69.61.0.*` — asserted by the manifest scan |
| runtime & diagnostics | `release_identity()` — all primitives, safe to embed in a redacted bundle |
| release qualification | `MILESTONE_TAG`, `VERSION` |
| documentation | `core.release_check.audit()` verifies the docs against the constants |

`tests/test_version_v69_m611.py` fails the build if `pyproject.toml` ever hard-codes a
literal again, or if any script re-states a rival current version.

---

## 3. Documentation corrections (M61.1 / M61.7)

`core/release_check.py` is a deterministic scanner over the repository's own files —
it reads, never writes, and never executes what it reads. Five check families:

| Family | What it refuses |
|---|---|
| `version` | `pyproject.toml` not deriving from `core/version.py` |
| `models` | a current-facing doc naming the superseded `qwen2.5` family (`qwen2.5-coder` is still the live CODER role and is correctly **not** matched) |
| `claims` | "complete autonomous", "fully autonomous", "attacks autonomously" |
| `status` | a release doc saying "NOT merged" for a milestone at or below the canonical one |
| `install` | a README install command pointing at a file that does not exist |

**Scope discipline:** only current-facing documents are scanned — the root README,
`jarvis/README.md` and the *header* of `JARVIS.md`. The per-version sections of
`JARVIS.md` and the historical release documents are out of scope by design.
Rewriting history to look consistent is the opposite of truth.

What was corrected:

- `JARVIS.md` header: now "Local-first, operator-controlled AI workstation and
  authorized Purple Team/SOC homelab platform", the configured role table
  (FAST `qwen3:8b`, CODER `qwen2.5-coder:latest`, DEEP `qwen3:14b`,
  VISION `gemma3:4b`, EMBEDDING `nomic-embed-text:latest`, VERIFIER `qwen3:8b`),
  and an explicit **Authority model** paragraph. ARES is described as running only
  when the operator authorizes each campaign. The authorized-lab warning is kept and
  strengthened.
- "Built across 46 versions" → the canonical release.
- Merge status corrected in four documents, each naming its merge commit:
  M55 `d101a83`, M58 `8c0a390`, M59 `384c147`, M60 `df35289`.
- Both READMEs state the current release, the dependency authority, the CI
  invocation and the qualification command.

A note on the corrections themselves: the first attempt quoted the stale phrase
("previously said *NOT merged*") and the scanner correctly flagged its own fix. The
notes were reworded rather than adding an escape hatch to the checker.

---

## 4. CI architecture (M61.2)

`.github/workflows/ci.yml` replaces `main.yml`, which was unfit as a release gate:
`pip-audit ... || true` discarded the audit's exit code **and** its output, and a
`docker-build` job required a Docker daemon.

Triggers: push to `master`, PR targeting `master`, and `workflow_dispatch` (the old
workflow had no manual dispatch).

| Job | Python | Gate |
|---|---|---|
| `consistency` | 3.11 | version / documentation / dependency truth. **No dependency install** — the checker is pure stdlib, so this gate cannot be broken by a resolution failure elsewhere |
| `lint` | 3.11 | ruff (E9 + full pyflakes) + `compileall` |
| `tests` | 3.11 | **the authoritative job** — the complete deterministic suite (both trees), the M61 soak, `doctor.py` |
| `compat` | 3.12 | consistency, grammar, the M61 suites |
| `base-install` | 3.11 | installs **only** `requirements/base.txt`, imports the declared text-mode surface, and asserts the optional packages are genuinely **absent** |
| `packaging` | 3.11 | builds wheel + sdist, then scans the artifacts |
| `dependency-audit` | 3.11 | bandit (blocking) + pip-audit (**advisory**) |

The `base-install` job's second step is the one that makes it worth having: without
`--assert-optional-absent`, a green run in a fat environment proves nothing.

**Why exactly one step is advisory.** `pip-audit` resolves against live advisory
feeds, so a new CVE published against a transitive dependency turns an unchanged,
correct commit red with no code change available to fix it. That is a maintenance
signal, not a correctness gate. It uses `continue-on-error`, which **preserves the
real exit status and the full output** and writes the outcome to the run summary —
unlike the `|| true` it replaces, which discarded the result entirely. Bandit stays
blocking. `tests/test_ci_workflow_v69_m612.py` asserts that no *mandatory* job
contains `|| true` or `continue-on-error`, that exactly one advisory step exists and
that it is reported.

CI requires no Ollama, model, microphone, TTS hardware, Docker daemon, Redis,
PostgreSQL, Zeek, Sysmon, Sliver, Metasploit, API key, trusted-lab mode or elevation.
No job declares a service container, references a secret, or uploads an artifact —
a build artifact from this repository could carry runtime state, so the manifest scan
runs **in-job**, where its failure is a gate rather than a download.

**The suite runs from the repository root with both trees named explicitly**
(`pytest -q jarvis/tests tests`), not from `jarvis/`. See §10.

---

## 5. Dependency authority (M61.3)

`requirements/<profile>.txt` is the **declared authority**; `pyproject.toml` mirrors
it and is verified. `core/dependency_authority.py` is the verifier — pure stdlib
parsing, no network, no pip invocation, no environment mutation. Names are normalized
per PEP 503, so `python_whois`/`python-whois` and `PyYAML`/`pyyaml` cannot hide a
duplicate.

**Drift it found (real, not hypothetical):**

| Extra | Packages the profile installed and the extra did not |
|---|---|
| `soc` | `stix2`, `mmh3`, `ntplib`, `piexif`, `ijson`, `igraph`, `pyserial`, `pyserial-asyncio` |
| `lab` | `pymetasploit3`, `grpcio`, `grpcio-tools`, `sliver-py`, `qrcode`, `pygetwindow`, `python-telegram-bot` |

`pip install jarvis[soc]` and `pip install -r requirements/soc.txt` produced different
environments. They no longer can.

**Legacy monolith.** `jarvis/requirements.txt` (170 lines, mixing text-mode, OCR, RAG
and GUI-automation packages) is deprecated to a pointer at `requirements/all.txt`.
Verified lossless before replacing it: 90 packages declared, **0 legacy-only**, and
the single specifier difference was `all` being *stricter* (`pywintrace` carries a
Windows platform marker).

**Base purity.** `keyboard`, `pyautogui`, `pygetwindow`, `mss`, `mitmproxy`,
`pymetasploit3`, `sliver-py`, `shodan`, `docker`, `playwright`, `opencv-python`,
`python-telegram-bot`, `grpcio` are asserted absent from anything reachable by a base
install, and asserted still present in `lab` — absent from base is only correct if it
is still installable where intended.

**CI reproducibility.** `requirements/constraints-ci.txt` bounds **only** the
test/lint toolchain (pytest, pytest-asyncio, ruff, bandit, pip-audit), upper-exclusive
at the next major. Runtime packages needing platform-specific resolution —
`pyaudio`, `yara-python`, `capstone`, `opencv-python`, `torch-directml`, `pywintrace` —
are deliberately **not** pinned; hard-pinning them across an untested matrix would
trade a reproducibility problem for an installability problem. A test asserts they
stay unpinned.

**Host mutation is now opt-in.** `core/dependency_guardian.py` ran
`pip install --break-system-packages` for eight packages and `winget install
jqlang.jq` on every boot, unasked. Both are report-only by default and require
`JARVIS_AUTO_INSTALL_DEPS=true`. `--break-system-packages` is not passed at all any
more — it exists to override the guard protecting an externally-managed interpreter,
and if that guard fires, refusing is the correct outcome. Failures are reported
content-free (package name + exit code; pip's stderr can echo a private path).

---

## 6. Packaging (M61.5)

Nothing had ever built this project, so it was undiscovered that it could not: the
flat layout (top-level `core/`, `tools/`, `aura/`, `main.py`) is exactly the shape
setuptools auto-discovery refuses — it sees several candidate top-level packages and
errors rather than guessing.

`pyproject.toml` now declares `packages`, `py-modules`, an explicit `package-data`
allowlist and `exclude-package-data` for the four gitignored signing/security files.
`__main__.py` is deliberately **not** packaged: installing a top-level `__main__` into
site-packages would put it on every other program's path. `python -m jarvis` stays a
source-tree launcher; the installed launch path is the `jarvis` console command.

`MANIFEST.in` prunes what a developer working tree actually carries: `.env`, the DEBUG
log, SQLite stores holding conversation state, diagnostics bundles, backup archives,
the HMAC signing keys, `tests/`, `conftest.py`, caches.

`scripts/check_package_manifest.py` does not trust those rules — a manifest rule that
is quietly wrong looks exactly like one that works. It opens the built wheel and sdist
and inspects the **real member list** (from the archive index; nothing is extracted).

**Verified locally — all four launch paths, none previously tested:**

| Check | Result |
|---|---|
| `python -m build` | `jarvis-69.61.0.tar.gz` + `jarvis-69.61.0-py3-none-any.whl` |
| manifest + secret scan | PASS — wheel 295 members, sdist 318 members |
| independent re-check | only match for the secret patterns is `core/integrity_baseline.py`, the **source module**; the `.json` baseline, `.env`, logs, `data/`, `tests/` and `conftest.py` are all absent, while `core/*.yaml`, playbooks and sigma rules are present |
| `python main.py --help` | exit 0 |
| `python -m jarvis --help` | exit 0 |
| wheel in a throwaway venv | version `69.61.0`, `console_scripts: jarvis = main:main`, `jarvis.exe` created |
| editable install | version `69.61.0`, resolves to the source tree |

Nothing was published. The operator's environment was not modified — a temporary venv
under the scratchpad was used, and the wheel was installed `--no-deps`.

---

## 7. Silent-failure and logging corrections (M61.4)

### Logging

The file sink was `logger.add("jarvis.log", …)` — a CWD-relative string. The evidence
was in the working tree: a 2.7 MB `jarvis.log` at the repository root **and** a second
one inside `jarvis/`, because the app had been launched from both. Under the M60.5
startup/scheduler deployment targets the CWD is whatever the service manager picked.

`core/managed_logging.py` routes it through `core.managed_paths` (absolute,
application-owned), declares rotation (`10 MB`) and retention (`7 days`) **once**
instead of duplicating the literals at two `logger.add` call sites, and **redacts on
the way to disk** by reusing the M60 deterministic pipeline — an OTP, a
credential-shaped token or the operator's home path cannot reach the log file even if
a call site formats one into a message. The filter never drops a record: the message
is rewritten, not the decision to log it. Prompts, responses and tool arguments are
not logged by policy; this is the backstop for accidental violation, not a licence.

Two continuity modules were also creating a directory **at import time** — a side
effect that fired inside test collection and packaging tools:

- `core/session_journal.py` (v44 Markdown report) → managed `logs/journals`
- `core/session_manager.py` (crash-resume snapshot, holds **redacted conversation
  turns**) → managed `logs/sessions`

Proven: a subprocess launched with a temp directory as its CWD, importing
`session_journal`, `session_manager`, `managed_logging`, `managed_paths` **and
`main`**, leaves that directory empty.

### Broad exception suppression

`main.py` has 69 broad catches, 50 pass-only. Most are legitimate optional
degradation and were left alone — optional features must not crash text mode.
Corrected only where a silent failure hides something that must never be invisible:

| Site | Class | Why it could not stay silent |
|---|---|---|
| console coordinator install | `HEALTH_DEGRADE` | was a bare `pass`; losing it means background logs smear the input line — the exact M54.1 fault the coordinator exists to prevent |
| `CONSOLE_READY` stamp | `LOG_AND_CONTINUE` | boot phase latencies silently incomplete |
| shutdown audit trail write | `LOG_AND_CONTINUE` | an unwritable **audit record** read as a written one |
| shutdown event `set()` | `LOG_AND_CONTINUE` | waiters never wake; the stall was unexplainable |
| SIGINT handler install | `LOG_AND_CONTINUE` | the **only** Ctrl+C path on Windows; without it Ctrl+C hard-kills mid-write |
| watchdog stop | `LOG_AND_CONTINUE` | a running watchdog restarts tasks while they are being cancelled |
| crash-resume snapshot write | debug → warning | silent loss is discovered only after the crash it was meant to survive |
| session-journal LLM summary | `EXPECTED_OPTIONAL_DEGRADATION` | genuinely optional; now at debug instead of vanishing |

Every new message is **content-free**: the exception *type* only, never its message,
which can quote a path or a redacted turn. Asserted by a test.

### The residue, measured rather than assumed away

~24 feature modules still resolve `Path("logs/…")` against the CWD and 12 still
`mkdir` at import time. Migrating them is the broad rewrite M61 refuses. They are
inventoried by `managed_logging.cwd_relative_log_modules()` and
`import_time_mkdir_modules()` — both **AST-based**, because several modules now carry
prose explaining the old pattern and a description of a defect must not be counted as
the defect. Tests pin both as ceilings with the critical modules excluded, so the
exposure cannot grow silently and a later milestone can drive it to zero with evidence.

---

## 8. Release qualification (M61.6) and soak (M61.6.1)

`python jarvis/scripts/qualify_release_m61.py [--quick|--full] [--live]` coordinates
13 gates and emits a content-free JSON artifact (counts, enum states, milliseconds,
gate names — no prompt, response, tool argument, secret or absolute private path).

It mutates nothing: git is read-only, no pip install, no Ollama setting or model
download, no service or startup entry, no semantic collection, and the build directory
is a temporary one that is always removed.
`tests/test_release_qualification_v69_m616.py` asserts that **over the AST** rather
than trusting the docstring: no mutating git subcommand may appear in any argv the
harness builds, and neither script may reach `os.system`, `eval`, `exec` or
`shell=True`.

### Deterministic soak

`scripts/soak_stabilization_m61.py` drives the existing M60 spine in-process with no
Ollama, no network and no model: run/session journal writes, clean turns, an
**interrupted** turn, an unfinished **effectful** tool op, crash reconciliation,
supervisor failure/restart, diagnostics preview, backup integrity, clean shutdown.

**40 cycles in 7.8 s:**

| Measure | Before → After |
|---|---|
| threads | 1 → 1 |
| asyncio tasks | 1 → 1 |
| journal writes | 0 → 157 (**0** write failures, **0** read failures) |
| temp files | 0 → 0 |
| RSS | 33.2 → 34.1 MB (+0.9) |
| `actions_replayed` | **0 in every cycle** |

15/15 checks PASS. Two of them were passing **vacuously** on the first run: an
`ImportError` in the diagnostics path and a false backup result made "diagnostics are
content-free" and "backup verified" report success without either having executed.
Both now require the subsystem to have actually run, and an EMPTY backup reports
*unproven* rather than pass. That is the same silent-pass shape M61 removed from CI.

### Bounded live smoke (M61.6.2)

Safe checks only: Ollama reachability, one deterministic time bypass that makes **no
model call**, and runtime-log placement. It never downloads a model, restarts or
configures the server, runs an offensive tool, scans a network or touches Windows
settings. A missing Ollama yields `INSUFFICIENT_EVIDENCE` → `PASS_WITH_WARNINGS`,
never a false PASS and never a false FAIL (a service-free host is supported).

**Observed on this host:** `ollama_reachable=True`, live smoke `PASS`.

---

## 9. Validation results

| Gate | Result |
|---|---|
| Full suite, `python -m pytest -q` from the git root | **3481 passed, 18 skipped, 0 failed** (baseline `df35289d`: 3286/18/0 → **+195 tests**) |
| Ruff (`core tools scripts aura main.py tests`) | clean |
| `compileall` (`core tools scripts aura main.py __main__.py`) | clean |
| Wheel + sdist | built, version `69.61.0` |
| Package manifest + secret scan | PASS |
| Deterministic soak, 40 cycles | PASS (15/15) |
| Bounded live smoke | PASS (Ollama reachable) |
| M61 test suites | 195 tests across 6 files |

New test files: `test_version_v69_m611.py` (22), `test_ci_workflow_v69_m612.py` (30),
`test_dependency_authority_v69_m613.py` (54), `test_managed_logging_v69_m614.py` (26),
`test_packaging_v69_m615.py` (30), `test_release_qualification_v69_m616.py` (32).

### A defect this milestone found in its own new test

The first full run reported 14 failures in
`test_session_journal_v69_m601.py::TestManagedPaths`, all passing in isolation. Cause:
the M61.4 CWD-scatter test used `importlib.reload` on `core.managed_paths`, which
rebinds the module's classes — so the M60 suite's
`pytest.raises(managed_paths.UnsafeLeafName)` was comparing against a *different class
object*. The reload was replaced by a subprocess with a temp CWD: no shared-session
mutation, and stronger evidence, since it is the real deployment scenario.

---

## 10. Known limitations

1. **`pytest` from `jarvis/` is not equivalent to `pytest` from the repository root.**
   `tests/test_security.py::TestReadFile::test_relative_traversal_blocked` resolves its
   `../../etc/passwd` fixture relative to the CWD, so the two invocations exercise
   different paths and disagree: from the git root it passes (3481/0), from `jarvis/`
   it fails on the Spanish message (`Archivo no encontrado` instead of
   `permiso`/`seguridad`). Both `tests/test_security.py` and `jarvis/tools/executor.py`
   are **byte-identical to `df35289d`**, so M61 did not change the test or the code
   under test. An attempt to reproduce the `jarvis/`-CWD run at `df35289d` in a
   temporary worktree produced **three different** failures (home-path redaction, caused
   by the worktree's temp path), so that comparison could not establish a clean
   pre-existing proof and **is not claimed as one**. CI and the release qualification
   both use the repository-root invocation with both trees named explicitly. The
   underlying CWD sensitivity of that legacy test is left for a later milestone; fixing
   it means touching a security path, which a stabilization release should not do
   opportunistically.
2. **~24 modules still resolve `logs/…` against the CWD**, 12 still `mkdir` at import.
   Bounded, inventoried and pinned by tests; not migrated (§7).
3. **Remote CI execution is not independently observed** — see §11.
4. **The advisory `pip-audit` job's findings are not triaged here.** M61 makes the
   result visible; acting on it is separate work.
5. **`main.py` is still 3,261 lines.** M61 deliberately performed no extraction: the
   only changes are the logging sink and the two exception classifications, both
   locally reviewable. A structural split needs its own milestone with its own tests.
6. **The base-install purity claim is proven in CI, not locally.** The local
   environment has every profile installed, so `check_base_import.py` passing here is
   necessary but not sufficient — `--assert-optional-absent` is what makes the CI job
   meaningful, and it can only pass in a genuinely base environment.

---

## 11. Remote CI observation

The workflow is committed and pushed on
`jarvis-v69-m61-production-stabilization`. **Remote execution was not independently
observed**: `ci.yml` triggers on push to `master` and on pull requests targeting
`master`, and this branch has neither been merged nor opened as a PR, so no run has
been produced for it. Manual `workflow_dispatch` is available.

No claim is made that CI passed remotely. Every gate in the workflow was executed
locally with the results in §9.

---

## 12. Security invariants — unchanged

M61 weakened nothing. `ToolExecutor`, `Authority`, `ScopePolicy`, risk classification,
HITL/NATO approval, audit logging, the prompt-injection firewall, content trust, SSRF
protection, path restrictions, crash reconciliation, secret redaction and the
no-replay-of-effectful-actions guarantee are untouched — and the M52–M60 regression
suites covering them are green.

Nothing was introduced from the forbidden set: no `shell=True`, no `os.system`, no
`eval`/`exec`, no arbitrary command templates, no model-supplied file paths, no global
keyboard hooks, no Windows service, scheduled task or startup entry.

Two things moved in the *safer* direction:

- automatic dependency installation on the operator's host is now **opt-in and off by
  default**, and `--break-system-packages` is gone;
- the runtime DEBUG log is redacted before it reaches disk, which it previously was not.
