# JARVIS V69 M59 — Predictive Warmth, Portable Interruption & Runtime Qualification

**Branch:** `jarvis-v69-m59-predictive-warmth-runtime-qualification`
**Base:** M58 `58b52dd` (proven ancestor of `master`)
**Status:** **M59 COMPLETE** — M59.1 · M59.2 · M59.3 · M59.4 · M59.5 · M59.6, deterministic
and bounded-live validated, and **merged to `master`** as `384c147` (merge status corrected in
V69 M61.1 — this line was stale).

M59 consolidates M58 and eliminates its measurable limitations: prewarm/live sampling
divergence, no session warmth baseline, manual (non-repeatable) prefix benchmarks, and
an idle compaction proposer that ran outside the resource governor and without
deterministic quality gates. Every module is deterministic, bounded, content-free and
extends the existing M52–M58 seams — nothing is rewritten or replaced.

## M59.1 — Prewarm / Live Sampling Parity
`core/inference_profile.py` (new). Separates three identities that M58 conflated:

- **RUNNER_IDENTITY** — `model, transport, think, num_ctx, grammar` + runner-affecting
  options (`num_batch`, `num_gpu`, …). A difference here CAN force an Ollama runner
  reload.
- **PREFIX_IDENTITY** — the existing `PromptManifest.compatibility_identity()` (stable
  prefix; excludes the contract delta and every turn-dynamic field).
- **GENERATION_ONLY** — `num_predict, temperature, top_p, top_k, repeat_penalty, seed,
  stop, …`. Applied to an already-built context; **never** reloads the runner.

A prewarm profile is **derived** from the live `GenerationBudget`
(`derive_prewarm_profile` / `profiles_for_shape`): it copies the runner + prefix
identity verbatim and keeps the live sampling posture, changing only the output cap.
`profile_compatibility()` decides field-by-field; an unrecognised option is UNKNOWN and
forces a conservative incompatibility. `classify_residual_load()` reads a post-prewarm
`load_duration` honestly: `NO_RELOAD` / `RELOAD_RUNNER_MISMATCH` /
`RELOAD_DESPITE_COMPATIBLE` (a compatible profile that still reloaded → eviction, never
a sampling mismatch that does not exist).

`contract_family` now derives its prewarm sampling from the live budget (no
hand-maintained `temperature=0.0` set) and records runner-parity provenance.

## M59.2 — Session Warmth Baseline & Predictive Rewarm
`core/session_warmth.py` (new).

- **`SessionWarmthBaseline`** — a bounded, content-free, process-scoped memory of the
  warmed runner+prefix identity. States: `UNINITIALIZED · MODEL_COLD ·
  MODEL_RESIDENT_PREFIX_UNKNOWN · PREWARMED · REUSE_LIKELY · REUSE_OBSERVED · STALE ·
  INVALIDATED · DEGRADED`. A prewarm is only ever `PREWARMED`; `REUSE_OBSERVED` requires
  **two** compatible live reuse observations. An identity change marks it STALE and
  re-baselines.
- **`PredictiveRewarmPolicy`** — decisions from **deterministic** workload/cache
  triggers only (never an LLM topic guess): STOPPING skips, battery skips, active FAST
  and requested embedding defer, a per-family attempt cap plus bounded exponential
  cooldown make looping impossible, and a measured success resets the family.

## M59.3 — Automated Prefix Qualification Matrix
`core/qualification.py` + `scripts/qualify_runtime_m59.py` (new).

- A curated **9-case deterministic matrix** (server-free) asserts the M59.1 invariants:
  same family shares a prefix; a compact-delta change stays compatible;
  language/num_ctx/authority/scope/tool-schema changes each invalidate. No Cartesian
  product.
- Optional **bounded live matrix** (documented generation caps: quick ≤ 4, full ≤ 8)
  judged against **separate** threshold profiles: `WARM_AC / COLD_AC / WARM_BATTERY /
  UNKNOWN` — a cold run is never judged by a warm bound.
- **Content-safe JSON artifact**: fixture IDs only (`GREETING_ES`, `BRIEF_MATH_ES`,
  `STANDARD_PYTHON_ES`, `GREETING_EN`), fingerprints, counts, ms. No prompts, bodies,
  secrets or private paths. Read-only git metadata. A missing server →
  `INSUFFICIENT_EVIDENCE`, never a false PASS.

## M59.4 — Governor-Integrated Compaction & Quality Gates
`core/compaction_quality.py` (new) + extensions to `compaction_scheduler`,
`residency_governor`.

- New `Priority.BACKGROUND_COMPACTION` inserted (additive) between `BACKGROUND`
  (embedding) and `PREWARM`. Final order: `CRITICAL > INTERACTIVE > VERIFICATION >
  SEMANTIC_QUERY > BACKGROUND > BACKGROUND_COMPACTION > PREWARM`.
- The idle model-assisted proposer now holds a governor slot for its whole duration, so
  it can never decode concurrently with the operator's live FAST turn and releases the
  slot on preemption.
- **`CompactionQualityGate`** is the deterministic authority over proposed digest items:
  rejects invented entities (source linkage), secrets, raw code, excessive quotation,
  bad kinds, over-long items and duplicates; a model item claiming EXPLICIT is refused
  outright. The extractive digest is always the authoritative fallback and **no
  semantic-memory write ever occurs.**

## M59.6 — Reproducible Release Qualification
`scripts/qualify_release_m59.py` + `release_verdict()` in `core/qualification.py`.

One bounded command coordinating: read-only git-state verification, focused M59
deterministic tests, M55–M58 regression, ruff, compileall, a deterministic soak, and an
optional bounded live qualification → one machine-readable JSON verdict.
`release_verdict()` aggregates honestly: any mandatory red or a live FAIL → `FAIL`; a
missing/insufficient live server → `PASS_WITH_WARNINGS`, never a silent PASS. No
git/host/Ollama/semantic/env mutation.

## M59.5 — Portable Active-Console Barge-In
`core/barge_in.py` + `core/console.py` extended (no second console subsystem).

### Backend selection
M58 hard-coded the msvcrt reader because `prompt_toolkit` is not installed on this host.
Selection is now a deterministic, fully injectable decision:

| Requested | Chain |
|---|---|
| `AUTO` (default) | prompt_toolkit → Windows msvcrt → COMMAND_ONLY |
| `PROMPT_TOOLKIT` | prompt_toolkit → *(same chain)*, `fallback_reason` names the cause |
| `WINDOWS_MSVCRT` | msvcrt → COMMAND_ONLY (never probes prompt_toolkit) |
| `COMMAND_ONLY` | terminal — forces the `/stop` line-mode fallback |

`prompt_toolkit` qualifies only when **installed** (detected with
`importlib.util.find_spec`, so an absent package costs nothing and a present one is not
imported until used), **enabled**, and the terminal is compatible (a real tty). It is
**optional and never installed automatically**; the msvcrt and COMMAND_ONLY paths remain
fully functional without it.

An explicitly requested backend that cannot run here **degrades honestly**: it falls
through the same chain while `fallback_reason` reports the true cause. Both
`selected_backend` and `fallback_reason` are exposed, so a degraded host is never
reported as a working one. `FallbackReason` values: `NONE ·
OPERATOR_FORCED_COMMAND_ONLY · PROMPT_TOOLKIT_NOT_INSTALLED ·
PROMPT_TOOLKIT_TERMINAL_INCOMPATIBLE · PROMPT_TOOLKIT_BACKEND_ERROR ·
NOT_WINDOWS_CONSOLE · NO_INTERACTIVE_CONSOLE · BACKEND_START_FAILED ·
CONSOLE_INPUT_BUSY`. Configured via `barge_in_mode` / `JARVIS_BARGE_IN_MODE`
(`ACTIVE_CONSOLE_KEY` is kept as the M58 alias for `AUTO`).

### Privacy guarantees
- **Console-local only.** No global keyboard hook, no OS-wide hotkey, no keylogging, no
  capture outside this process's own console, and nothing is armed after `STOPPING`.
- **Source-level allowlist.** Each backend maps a keystroke against the two-entry
  allowlist (`Escape`, `Ctrl+G`) **inside its own reader thread**. A non-interrupt key —
  including an accented Spanish character — never leaves the backend as a value; only a
  count (`ignored_keys`) survives. Windows function/arrow keys arrive as a `NUL`/`0xE0`
  prefix plus a scan code; both parts are consumed and dropped.
- **Ctrl+C is deliberately not claimed** — it remains the console's own SIGINT, so an
  interrupt can never be confused with shutdown.
- No raw key value appears in any log line, metric or health block. A dedicated
  AST-level test proves `core/barge_in.py` imports no global-input library
  (`keyboard`, `pynput`, `ctypes`, `win32api`, …) and calls no hook/hotkey/key-state API
  (`SetWindowsHookEx`, `RegisterHotKey`, `add_hotkey`, `GetAsyncKeyState`, …).

### Console-input ownership
`ConsoleCoordinator` already guaranteed a single owner for every console **write**.
`ConsoleInputOwnership` (new, in `core/console.py`) adds the mirror guarantee for
**reads**: the interactive line reader (`line_reader`) and the key backend
(`barge_in`) can no longer consume the same bytes. It is deliberately **not** a lock a
caller may block on — a backend that cannot take ownership **degrades** (records
`CONSOLE_INPUT_BUSY`, leaves `/stop` working) rather than waiting. Ownership is
re-entrant for the same owner, a foreign release is counted and ignored, and
`reset_console()` drops it.

### Terminal restoration
The msvcrt reader changes **no** terminal mode — that is precisely why it is the safe
Windows default. `PromptToolkitKeyReader` is the only backend that enters raw mode, so
it carries the guarantee: raw mode is exited on `stop()`, on shutdown, and on **any**
exception raised during `start()` or inside the reader loop. A failed start restores the
terminal, releases ownership and drops the session to `COMMAND_ONLY`. A restore that
itself fails is **counted** (`terminal_restore_failures`), never raised.
`orphan_reader_count` = readers started − readers stopped: a nonzero value is a leaked
input reader, which makes "no orphan reader" a checkable fact rather than a hope.

### Interruption semantics
During an active generated/spoken response, an allowlisted key →
cancel the native stream (`cancel_llm_only`) → cancel stale answer TTS
(`cancel_answer_speech`) → `end_turn(INTERRUPTED_BY_OPERATOR)`. Because
`ResponseRuntime.end_turn` closes a turn **exactly once**, the turn loop's
`finally: end_turn(COMPLETED)` cannot rewrite history — the partial turn stays truthfully
`INTERRUPTED_BY_OPERATOR`. `accepts(turn_id)` then refuses and counts every late chunk,
already-displayed partial text is retained, and the prompt is restored exactly once by
the existing loop. When **no** response is active the key is ignored and counted
(`ignored_no_active_turn`) — it never terminates JARVIS, and normal input keeps working.
`/stop` remains the always-available fallback and is recorded distinctly
(`command_interruptions`).

## M59.5 — Live warmth wiring
`core/warmth_runtime.py` (new) is the bridge M59.2 lacked: the baseline and policy
existed but nothing called them from a real prewarm, turn, eviction or shutdown.

- **Prewarm → `PREWARMED`, never reuse.** Every warm path (boot, background, predictive
  rewarm) publishes through **one** hook in `contract_family.warm_family`, so the session
  record is folded exactly once and can never disagree with what actually ran. A boot
  prewarm is deliberately *not* a policy attempt — otherwise a successful warm would
  silently reset a family the policy had correctly backed off.
- **Only measured live evidence promotes warmth.** `llm._observe_prefix` folds each
  turn's prefix-cache classification via `observe_turn()`; `PREWARMED → REUSE_LIKELY →
  REUSE_OBSERVED` (the last still requiring two compatible observations, M59.2).
- **Invalidation runs BEFORE evidence is folded**, so an incompatible measurement can
  never promote warmth. Reasons reuse the existing M58 field precedence
  (`prefix_cache.diff_invalidation`) — one vocabulary, not two: `MODEL_CHANGED ·
  NUM_CTX_CHANGED · THINK_CHANGED · LANGUAGE_CHANGED · AUTHORITY_CHANGED · SCOPE_CHANGED ·
  SECURITY_POLICY_CHANGED · TOOL_SCHEMA_CHANGED · POWER_PROFILE_CHANGED`, plus the two
  runtime events no manifest diff can express: `MODEL_EVICTED` (observed) and
  `PREWARM_CANCELLED`. An invalidation clears the baseline, the prefix observer's
  per-identity baselines **and** the family prewarm's once-per-identity guard together —
  leaving any one of them holding the old identity is exactly how a stale metric becomes
  a false readiness claim.
- **Predictive rewarm** is deterministic (no LLM topic prediction — an AST test asserts
  the module cannot reach a chat/completion surface), runs through the existing family
  prewarm at `Priority.PREWARM`, and is refused in this order: STOPPING → battery →
  active FAST turn → requested embedding → attempt cap → cooldown. A trigger raised
  *during* a turn is queued and consumed only *after* it (`consume_pending`), so a rewarm
  never delays TEXT_READY or prompt restoration; the operator's next turn calls
  `preempt()`; shutdown `await`s `cancel()` before the governor closes, so no rewarm task
  survives. Any **unreadable** live signal defaults to the refusing answer.

## M59.5 — Operator commands
`core/runtime_commands.py` (new), same exact-match allowlist discipline as
`core/response_commands.py`.

| Command | Kind |
|---|---|
| `/warmth-status` `/prewarm-status` `/compaction-status` `/barge-status` `/runtime-qualification` | read-only |
| `/rewarm concise` `/compact-now` `/qualify quick` | bounded action |

Spanish aliases exist for each. `"/rewarm concise"` and `"/qualify quick"` are **literal
two-word aliases, not a verb plus an argument** — there is no argument parsing anywhere
in the module, so no value, path, model, scope or host setting can arrive from free text
(`/qualify quick --live` is not a command, it is an ordinary turn). No shell, no
subprocess, no filesystem write, no environment/Ollama reconfiguration, no Git operation,
no semantic-collection write — all proven by AST-level import/call tests over both
`runtime_commands` and `compaction_scheduler`.

- `/compact-now` waives only the idle **timing** gates (minimum turns, context pressure,
  cooldown) and honours every measured **safety** gate (active turn, HITL, effectful
  tool, active answer speech, high-priority embedding, lifecycle, power) — reporting the
  blocking reason verbatim. It runs the M59.4 quality gate and writes only the in-memory
  conversation digest; **it never writes semantic memory.**
- `/qualify quick` runs the deterministic matrix in-process: no server, no file, no Git,
  no host mutation.
- Panels are **ASCII-framed** (`TITLE` + `  key=value`), matching `core/response_status`,
  because the live JARVIS console is cp1252 — a test encodes every panel in both
  languages to `cp1252` to prove it.

## Runtime health (content-free additions)
`runtime_health` prompt_cache subsystem gains sampling-parity
(`prewarm_runner_identity`, `live_runner_identity`, `runner_parity`), session-warmth
(`session_warmth_state`, `reuse_state`, `predictive_rewarm_attempts`, …) and the M59.4
governor/quality metrics — all fingerprints, counts, ms and enum states.

M59.5 extends **the same single registry** (no new health surface — asserted by test):

- **Barge-in:** `selected_backend`, `portable_backend_available`, `fallback_reason`,
  `active_interruptions`, `command_interruptions`, `cancellation_latency_ms`,
  `terminal_restore_failures`, `orphan_reader_count`, `console_busy_denials`,
  `barge_in_arm_failures`, `non_interrupt_keys_dropped`.
- **Session warmth:** `session_state`, `active_family`, `observation_count`,
  `reuse_state`, `invalidation_count`, `last_invalidation_reason`,
  `predictive_rewarm_attempts`, `predictive_rewarm_successes`, `rewarm_preemptions`,
  `cooldown_remaining`, `rewarm_pending`, `rewarm_deferred`, `rewarm_skipped`.

The subsystem stays **advisory** (`OPTIONAL`): a cold prefix or a `COMMAND_ONLY` backend
is a performance fact, never a runtime fault, so it must not degrade the overall verdict.
Fixed while adding these: the M58 cache reason is now
`prefix_last_invalidation_reason`, because two dict entries sharing
`last_invalidation_reason` meant the second **silently discarded** the first.

## Validation
### Deterministic
- **Full suite (git-root scope, `jarvis/tests` + legacy `../tests`): 2900 passed,
  18 skipped, 0 failed** (256s).
- **225 new M59 deterministic tests** (79 from M59.1–M59.4/.6 + 146 added by M59.5:
  45 portable barge-in, 39 warmth wiring + health contract, 62 operator commands).
- **554 passed** in the focused M55–M59 regression sweep (28 suites: barge-in, console,
  prefix cache, prompt manifest, contract family, compaction, tool loop, lifecycle,
  shutdown, TTS queue/shutdown, progressive TTS, turn pipeline, governor, runtime
  profile, fast prewarm, runtime health, text-ready, readiness truth).
- **ruff:** `ruff check jarvis/` clean. Repo-wide `ruff check .` reports 4 errors, all in
  the legacy sibling `tests/test_agentic_wiring.py` and `tests/test_cognitive_engine.py`
  — **proven pre-existing**: both blobs are byte-identical to `master`
  (`645bdcb0…`, `8b8a60e7…`). Unrelated to M59.
- **compileall** (`jarvis/core`, `jarvis/tools`, `jarvis/scripts`, `jarvis/main.py`):
  clean.
- `qualify_runtime_m59.py --no-live` → **PASS**, 9/9 deterministic cases.
- `qualify_release_m59.py --quick` → **PASS_WITH_WARNINGS** (only `live_not_requested`).

### Known legacy Windows failure
`../tests/test_security.py::TestReadFile::test_relative_traversal_blocked` failed in the
M59.4-era run and **passes in this run**, both inside the full suite and in isolation. It
is environment/ordering dependent (the assertion turns on whether the traversal target
exists on disk, yielding "Archivo no encontrado" instead of the security message), not a
code-state fact. M59 touches no security or executor code, so this milestone neither
caused nor fixed it — it is recorded here so a future red run is not misread as an M59
regression.

### Bounded live validation (real local Ollama 0.32.3, `qwen3:8b`)
**32/32 checks passed.** No model download, no server restart, no configuration change,
no semantic-collection write. Every generation capped (12–400 tokens) and deadline-bound.

| Step | Measurement |
|---|---|
| Selected backend | `WINDOWS_MSVCRT`, `portable_backend_available=False`, `fallback_reason=PROMPT_TOOLKIT_NOT_INSTALLED` |
| Real family prewarm (cold; `qwen3:8b` was not resident) | `load=15390.0ms`, `prompt_eval=17633.6ms`, `first_token=34438.0ms`, `runner_parity=True` |
| Warmth after prewarm | `PREWARMED` — **never** `REUSE_OBSERVED` |
| Live turn 1 | `load=671.8ms`, `prompt_eval=1906.5ms`, `first_content=3641.0ms` → `MODEL_WARM_PREFIX_UNKNOWN` |
| Live turn 2 | `load=573.6ms`, `prompt_eval=1320.7ms`, `first_content=2984.0ms` → `MODEL_WARM_PREFIX_UNKNOWN` |
| Warmth promotion | **refused** — ratio 1320.7/1906.5 = 0.69 is above the 0.6 reuse threshold, so no reuse was measured and none was claimed |
| Interruption of a real stream | stream cancelled, 1 chunk rendered, **1 late chunk refused**, stale TTS cancelled once, `INTERRUPTED_BY_OPERATOR`, latency ≈ 0.0ms |
| History truthfulness | a subsequent `end_turn(COMPLETED)` could **not** rewrite the interrupted state |
| Next turn | completed normally (`"Listo."`) |
| `/stop` fallback | recorded as `command_interruptions=1`, distinct from `active_interruptions=1` |
| Invalidation | `TOOL_SCHEMA_CHANGED` → baseline `INVALIDATED`, family guard cleared, matching trigger raised |
| Predictive rewarm | scheduled, then **preempted** by simulated operator work; `rewarm_pending=False` |
| Operator commands | all five panels rendered and cp1252-encodable; `/qualify quick` → `verdict=PASS` |
| Shutdown | `orphan_reader_count=0`, console ownership released, **0 orphan asyncio tasks**, **0 orphan reader threads** |

The single non-automatable step is a human finger on the key. The keystroke was injected
at the exact seam a real backend uses — the reader thread's `on_key` callback, from a
real background thread, against a **real streaming Ollama response**. Everything
downstream of that seam is the genuine production path.

## Remaining limitations
- **`prompt_toolkit` is not installed on this host**, so the portable backend is exercised
  only through injected fakes (raw-mode enter/exit, key mapping, read faults, restore
  failures, end-to-end through the controller). Its real-terminal behaviour is untested
  here by construction — the package is deliberately not installed automatically.
- **Prefix reuse remains unproven on this host.** Two compatible live turns dropped
  prompt_eval 1906.5 → 1320.7ms (ratio 0.69), which is suggestive but above the 0.6
  threshold, so M59 correctly reports `MODEL_RESIDENT_PREFIX_UNKNOWN` rather than reuse.
  `REUSE_OBSERVED` still requires two measurements below the strong-reuse ratio.
- The barge-in **key interrupt was validated at the callback seam**, not through a
  physical console keypress; the msvcrt polling loop itself is covered only by
  construction and unit-level tests.
- **Voice input is not wired** to the M59 command surface: the eight commands are
  TEXT-ONLY, following the M56 residency-command precedent, so a misheard phrase can
  never trigger a bounded action.
- `MODEL_CHANGED` / `NUM_CTX_CHANGED` / `THINK_CHANGED` invalidate but deliberately
  raise **no** speculative rewarm trigger: those rebuild the runner outright, and the
  family is merely re-armed for the next legitimate idle window.

## Do NOT
Begin M60. Merge automatically. Download/replace models. Restart/reconfigure Ollama.
Modify persistent Windows settings. Create a global keyboard hook.
