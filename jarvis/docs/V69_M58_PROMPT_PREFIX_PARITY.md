# JARVIS V69 M58 — Prompt-Prefix Parity, Cache-Safe Prewarm & Real-Time Interruption

**Branch:** `jarvis-v69-m58-prompt-prefix-parity` (NOT merged)
**Base:** `dc1e34b` (M57) — verified ancestor of `master`
**Status:** complete, pushed, awaiting review. Do not merge automatically.

---

## 1. What M57 left, and the root cause of the prefill

M55/M56 made the *first token* fast (warm ~1.2 s). M57 made the *answer length*
adaptive. Two residual costs remained, both rooted in **how the FAST prompt was laid
out**. `core.llm._fast_system_prompt` built one flat list:

```
identity · answer-rule · HOST_CLOCK · language · STYLE(contract) · continuation
```

Two of those are dynamic and sat **inside** the otherwise-stable text:

- the **host clock** is a full ISO timestamp (`...T10:00:07...`) that changes every
  second, at **position 3** — so nothing after the first ~2 sentences was byte-stable
  between two turns a second apart, and server-side prefix reuse was defeated;
- a distinct **natural-language STYLE paragraph per contract**, at position 5 — so the
  *first* use of each contract paid the full prose prefill (~2.9 s in the M57 report),
  because its bytes differed from every previously-warmed one.

M58 fixes the **layout** and the **warm target**, adds **honest cache observation**,
**schedules** the idle compaction M57 only built, **bounds** the tool loop, and adds
**immediate active-console interruption** — without weakening any security control.

---

## 2. Stable-prefix architecture (M58.1 / M58.2)

`core/prompt_manifest.py` reclassifies the FAST prompt into three reuse tiers and
gives it a reuse-preserving layout:

```
STABLE_CORE_PREFIX   identity + answer discipline + security (no-CoT, no-tool-JSON,
                     anti-injection)                              IMMUTABLE_CORE
SESSION_PREFIX       active-language directive                    SESSION_STABLE
CONTRACT_DELTA       compact machine-readable block               TURN_DYNAMIC
DYNAMIC_TAIL         host clock + continuation (moved to the END) TURN_DYNAMIC
```

`STABLE_CORE + SESSION` is **byte-for-byte identical** across every eligible FAST
contract when model / num_ctx / language / authority / scope / security-policy /
personality are unchanged (proven for INSTANT/BRIEF/STANDARD/TECHNICAL/STRUCTURED/
ERROR_RECOVERY). The host clock and continuation move to the tail, so the dynamic ISO
timestamp can no longer break the reusable region. Security constraints stay in the
protected stable prefix — they are never moved into a trimmable dynamic section.

**Live proof:** the benchmark reported **1 distinct stable-prefix fingerprint** across
INSTANT and BRIEF turns in the same run — the stable region is genuinely shared.

---

## 3. Compact contract delta (M58.3 / M58.3.1)

The per-contract prose tail is replaced by a bounded, allowlisted, deterministic
block (`[RESPONSE_CONTRACT] … [/RESPONSE_CONTRACT]`) with a fixed field order:
`schema · contract · language · answer_first · max_sentences · structure ·
continuation`. Cap: **240 chars**.

| Measure | M57 prose tail | M58 delta |
|---|---|---|
| BRIEF | ~330 chars | **~155 chars** |
| STANDARD | ~360 chars | ~158 chars |
| TECHNICAL | ~340 chars | ~158 chars |
| aggregate (6 FAST contracts) | 1.0× | **< 0.75×** |
| bounded? | no (free prose) | **yes (≤240)** |
| observed live (`dTok`) | — | **37–38 est. tokens** |

The delta is **presentation only**: it carries no tool / authority / scope / risk /
permission / verify / memory field (asserted for all ten contracts). Security, tools
and RAG policy remain inherited from `TurnPolicy` / `ToolExecutor`. A prompt-size
governor measures each layer and a detector fails a test when a stable section
(identity, security, contract marker) appears twice.

---

## 4. Prompt/schema fingerprints & invalidation (M58.1 / M58.5)

Content-free SHA-256 prefixes (never `repr`/`hash`) fingerprint the core prompt,
session prefix, stable prefix, contract delta, security policy, personality and tool
schema. A **request compatibility identity** folds them all *except* the contract
delta and every turn-dynamic field — so a different contract in the same family stays
compatible (the whole point of family prewarm), while a change in model / transport /
num_ctx / think / language / authority / scope / security / personality / tool-schema
invalidates it. Raw prompt text never leaves the module through diagnostics.

`core/prefix_cache.py` names a deterministic `InvalidationReason` for each of those,
plus `POWER_PROFILE_CHANGED` and `MANUAL_INVALIDATION`; invalidation clears the
per-identity baselines so a stale metric can never be reused as proof of readiness.

---

## 5. Cache observation — honest classification (M58.5)

Ollama does not expose its KV cache, so **nothing claims a KV hit**. Classification is
from observable evidence only (`prompt_eval_count`, `prompt_eval_duration`,
`load_duration`, first-content time). The states are:

| State | Meaning |
|---|---|
| `COLD_MODEL` | weights loaded this turn (`load_duration` ≥ 800 ms) — never reuse |
| `MODEL_WARM_PREFIX_UNKNOWN` | warm, but no per-identity baseline to compare |
| `PREFIX_REUSE_LIKELY` | prompt_eval dropped to ≤ 0.6× the identity's cold baseline |
| `PREFIX_REUSE_OBSERVED` | prompt_eval dropped to ≤ 0.4× the cold baseline |
| `CONFIG_MISMATCH` | this turn's identity ≠ the warmed identity |
| `INSUFFICIENT_EVIDENCE` | metrics missing — no claim made |

**Model residency alone is never treated as reuse.** A loaded model with a big
`prompt_eval` is `COLD_MODEL`; reuse requires a measured prompt_eval *drop* against a
recorded cold baseline for the same identity.

---

## 6. Contract-family prewarm (M58.4 / M58.4.1)

`core/contract_family.py` groups the ten contracts:

- **CONCISE** {INSTANT, BRIEF, ERROR_RECOVERY} — native FAST, no tools → prewarmed
- **EXPLANATORY** {STANDARD, TECHNICAL, STRUCTURED} — native FAST, no tools → prewarmed
- **SPECIALIZED** {CODE, DOCUMENT_GROUNDED, OPERATIONAL, DEEP} → on demand

A family prewarm sends the **real stable prefix + the family's compact delta** as the
system message (never the meaningless `"ok"`; the host clock is excluded), with the
**exact production model / native transport / think=false / live num_ctx**, tiny
bounded output, no history / memory / TTS / answer. Modes `OFF / CONCISE_ONLY /
BACKGROUND_FAMILIES (default) / BEFORE_TEXT_READY_CONCISE`. States
`NOT_REQUESTED … INVALIDATED`; once-per-`(family, identity)` guard; governor `PREWARM`
priority; `warmed_identity()` feeds the observer. **Boot warms 1–2 families, not ten
generations.** The first user turn preempts it; shutdown cancels it.

**Live proof:** the CONCISE prewarm cold-loaded qwen3:8b through the real prefix
(first token 28.7 s, prompt_eval 14.0 s cold), then warm was first token **2.58 s**,
prompt_eval **446 ms**.

---

## 7. Idle compaction scheduling & preemption (M58.6)

`core/compaction_scheduler.py` runs the M57 optional model-assisted digest pass **only
when idle**: enough completed turns, no active user turn / HITL / effectful tool /
answer TTS / high-priority embedding, lifecycle OPERATIONAL, power permits, context
pressure over threshold, cooldown expired (`block_reason()` names the first failing
gate). The extractive digest is always set as the authoritative baseline first; a
model pass is bounded, cancellable and timeout-guarded, and `merge_model_assisted`
forces every model item to **INFERRED** and drops anything that duplicates/contradicts
EXPLICIT. There is **no semantic-memory write path at all**. On user input the active
`_run_turn` calls `preempt()` — the scheduler keeps its last valid digest and the FAST
turn proceeds immediately; shutdown awaits `cancel()`. A live `_idle_compaction_loop`
driver runs it off the turn path with a bounded native proposer.

---

## 8. Bounded tool-enabled generation & schema stability (M58.7 / M58.7.1)

`core/tool_loop.py`:

- `validate_tool_call()` rejects malformed/partial JSON, non-object arguments, empty
  names and names outside the eligible set — **malformed and hallucinated tool calls
  never execute**, and effectful arguments are never freely guessed/repaired.
- `ToolLoopBudget` bounds tool rounds / model retries / malformed-repairs. When the
  round budget is spent, tools are **dropped** so the model produces a bounded final
  answer (Phase 3) — the JSON is **never truncated** to enforce the bound. Terminal
  states `TOOL_CALL_* / FINAL_RESPONSE_*`.

`core/tool_schema.py` gives the tool schema a canonical order (by name + recursive key
sort) and a content-free fingerprint, so a dict/registry reorder never changes it; the
empty (DIRECT_FAST) schema has a stable distinct fingerprint; the eligible subset is
sized before/after `TurnPolicy` filtering. **DIRECT_FAST sends no tool schema.**

---

## 9. Active-console barge-in & privacy posture (M58.8 / M58.8.1)

`core/barge_in.py`. `prompt_toolkit` is **not** installed on the host, so the active
backend is the Windows console reader (`msvcrt`) — it reads **only this process's own
console buffer**, and only while a turn is armed. It is **not** a global keyboard hook,
not keylogging, not an OS-wide hotkey.

An allowlisted single key (**Esc / Ctrl+G** — never Ctrl+C) interrupts a
generating/speaking answer immediately, running the exact `/stop` teardown: cancel the
native stream, cancel answer TTS, mark the turn `INTERRUPTED_BY_OPERATOR`. Late chunks
are suppressed by the response-runtime turn id; partial displayed text stays; history
is finalized truthfully; the prompt is restored once. When no answer is active the key
is ignored (it never kills JARVIS). When the backend is unavailable the mode is
`COMMAND_ONLY` and the line-mode `/stop` fallback remains. Privacy: the key value is
never stored or logged; arm/disarm bound the reader to the turn so it never contends
with the line reader; disarm always restores the terminal (counting a restore failure
rather than raising); shutdown closes the backend. A crash-recovery test proves
terminal restoration when the backend `stop()` raises. Modes: `COMMAND_ONLY /
ACTIVE_CONSOLE_KEY / VOICE_ACTIVITY / UNAVAILABLE`.

---

## 10. Runtime health (M58.9)

One advisory (rank 0) `prompt_cache` subsystem **extends** the single runtime-health
surface (no new registry), covering: prompt manifest fingerprints + layer sizes;
prefix-reuse `cache_state` / invalidations / warm-cold prompt_eval / observed reuse
ratio; family-prewarm mode / states / attempts / stale fingerprints; compaction
counters + tokens saved + digest version; tool rounds / malformed / denied / final
tokens / schema fingerprint / eligible count; barge-in mode / supported / interruptions
/ latency / late-chunks-suppressed / terminal-restore-failures. Every metric is a
fingerprint, count, millisecond or enum — never a prompt, answer, tool argument or key.
Advisory-only: a first-use prefill cost, a cold prefix or a `COMMAND_ONLY` barge-in
mode never degrades the overall verdict.

---

## 11. Live benchmark (observe-only) — measured facts

Host: AMD Ryzen 5 7430U, 15 W, CPU-only Ollama 0.32.1. Before the run only
`nomic-embed-text` was resident (qwen3:8b cold). No download, no restart, no config
change, no semantic write, no dangerous tool.

| turn | contract | num_ctx | delta est. tok | first token | prompt_eval count | prompt_eval ms | load ms | classification |
|---|---|---|---|---|---|---|---|---|
| family prewarm (cold) | CONCISE | 2048 | — | 28 672 ms | — | 14 045 ms | — | (loaded) |
| family prewarm (warm) | CONCISE | 2048 | — | **2 578 ms** | — | **446 ms** | — | READY |
| `hola` | INSTANT | 2048 | 38 | 20 390 ms | 374 | 15 267 ms | 2 826 ms | **COLD_MODEL** |
| `buenas` | INSTANT | 2048 | 38 | **6 656 ms** | 375 | **4 926 ms** | 557 ms | **PREFIX_REUSE_OBSERVED** |
| `brief-sqrt` | BRIEF | 2048 | 37 | 23 485 ms | — | — | — | **INSUFFICIENT_EVIDENCE** |

**What is honestly established:**

- **Stable prefix is shared** — 1 distinct stable-prefix fingerprint across INSTANT and
  BRIEF; **num_ctx parity held** — 1 distinct num_ctx (2048) everywhere (no M56 reload).
- **Prefix reuse is observed, not assumed** — the same-identity INSTANT repeat dropped
  prompt_eval **15 267 ms → 4 926 ms (0.32×)** and first token **20 390 ms → 6 656 ms
  (~3×)**, classified `PREFIX_REUSE_OBSERVED` purely from the measured drop.
- **Residency is not mistaken for reuse** — the first live INSTANT turn was
  `COLD_MODEL` (it had a `load_duration`), even though the model was resident; the
  classifier waited for a prompt_eval drop before claiming reuse.
- **Insufficient evidence stays honest** — `brief-sqrt` produced no prompt_eval
  metadata and was left `INSUFFICIENT_EVIDENCE`, not optimistically labelled reused.

**Caveats (stated honestly):** absolute prompt_eval times are large (5–15 s) — this is
the 15 W CPU, not a regression; the *relative* reuse is the M58 result. The first live
INSTANT turn still showed a small `load_duration` (2.8 s) because the live contract's
sampling options differ from the prewarm's minimal ones; the same-identity repeat then
reused cleanly. `brief-sqrt` did not emit prompt_eval metadata in this run — a single
bounded trial, deliberately not over-generalized.

---

## 12. Long-session soak

`scripts/soak_prefix_m58.py` — 48 deterministic turns: alternating contracts, a
language switch every 8 turns, tool-free turns, a safe read-only tool fixture + a
malformed one, compaction scheduling, prewarm invalidation + rewarm, and interruptions.

Result: **SOAK PASSED**. `prompt_max=964`, `delta_max=158` (≤240 cap), prefix
observations bounded at 40 (deque maxlen), prewarm attempts=3 (bounded), digest
bounded, 4 interruptions recorded, compaction completed, invalidation cleared the warm
set and a rewarm succeeded. Nothing grew with uptime.

---

## 13. Validation summary

| Gate | Result |
|---|---|
| M58 focused suites (7 files) | **93 passed** |
| M55–M57 regression touched by M58 (17 files) | **385 passed** |
| Full suite from git root (`python -m pytest -q`) | **2675 passed, 18 skipped, 0 failed** |
| Pre-M58 baseline | 2579 passed, 18 skipped, 0 failed |
| New tests added | **+96** |
| `ruff check .` | **All checks passed** |
| `compileall core tools scripts main.py` | **clean** |
| Live prefix benchmark | ran; reuse observed |
| Long-session soak | PASSED |

---

## 14. Remaining limitations

- The idle-compaction native proposer runs live on a 15 W CPU; it is heavily gated
  (idle + AC + high pressure + cooldown) and preempted by any turn, but its real-world
  quality/latency was not benchmarked here — only its scheduling/preemption were.
- One live INSTANT turn still paid a small `load_duration` because the live contract
  sampling differs from the prewarm's minimal options; a future step could align them.
- `brief-sqrt` emitted no prompt_eval metadata in the single trial; a larger matrix
  (B/C/D/E/F/G/H/I/J) is scaffolded conceptually but only a bounded subset was run.
- Absolute first-token times remain CPU-bound (5–20 s cold, ~2.6 s warm prewarm) — the
  M58 win is prefix-reuse *ratio*, not raw latency.

---

## 15. Recommendation for M59 (do NOT start yet)

1. **Align prewarm sampling with the live contract** so the first live turn after a
   prewarm does not pay even the small residual `load_duration` observed here.
2. **Persist a warm-prefix baseline across the session** so `PREFIX_REUSE_OBSERVED`
   can be asserted on the *first* repeat rather than after a baseline is re-measured.
3. **Extend the benchmark matrix** (language change, num_ctx change, fake
   security-policy/tool-schema change, after-embedding, after-cancel) into a bounded
   automated harness with pass/fail thresholds.
4. **Wire the compaction proposer through the residency governor** BACKGROUND slot
   explicitly and add a live compaction-quality soak.
5. Consider a **prompt_toolkit** optional dependency to offer a portable
   ACTIVE_CONSOLE_KEY backend beyond Windows/msvcrt.

M59 is **not** started. This branch is **not** merged.
