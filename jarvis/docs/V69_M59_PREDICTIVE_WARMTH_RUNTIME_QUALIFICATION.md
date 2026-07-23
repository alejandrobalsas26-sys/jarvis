# JARVIS V69 M59 — Predictive Warmth, Portable Interruption & Runtime Qualification

**Branch:** `jarvis-v69-m59-predictive-warmth-runtime-qualification`
**Base:** M58 `58b52dd` (proven ancestor of `master`)
**Status:** M59.1, M59.2, M59.3, M59.4, M59.6 COMPLETE. M59.5 (portable barge-in) DEFERRED — see *Remaining work*.

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

## Runtime health (content-free additions)
`runtime_health` prompt_cache subsystem gains sampling-parity
(`prewarm_runner_identity`, `live_runner_identity`, `runner_parity`), session-warmth
(`session_warmth_state`, `reuse_state`, `predictive_rewarm_attempts`, …) and the M59.4
governor/quality metrics — all fingerprints, counts, ms and enum states.

## Validation
- **Full suite (git-root scope, jarvis/tests + legacy ../tests):** 2753 passed,
  18 skipped, 1 failed.
- The **1 failure** is the pre-existing legacy sibling
  `../tests/test_security.py::TestReadFile::test_relative_traversal_blocked` (Windows
  "Archivo no encontrado" vs "permiso"/"seguridad"). Proven pre-existing: the security
  path (`tools/executor.py`, `tests/test_security.py`) is **byte-identical to master**;
  M59 touches no security/executor code (15 changed files, none security-related);
  `58b52dd` is an ancestor of HEAD.
- **79 new M59 deterministic tests** — all pass. Every touched M55–M58 regression suite
  — green.
- **ruff:** clean. **compileall** (core/tools/scripts/main.py): clean.

## Remaining work (M59.5 — Portable Active-Console Barge-In)
Deferred under the session budget-discipline stop rule. Scope: an optional
`prompt_toolkit` backend behind an `AUTO` selector
(`AUTO → PROMPT_TOOLKIT → WINDOWS_MSVCRT → COMMAND_ONLY`), console-local only (no global
hooks, no keylogging), coexisting with `ConsoleCoordinator` redraw, with terminal
restoration on error/shutdown and content-free barge-in health
(`selected_backend`, `portable_backend_available`, `fallback_reason`, …). Extend
`core/barge_in.py` and `core/console.py` seams only.

## Do NOT
Begin M60. Merge automatically. Download/replace models. Restart/reconfigure Ollama.
Modify persistent Windows settings. Create a global keyboard hook.
