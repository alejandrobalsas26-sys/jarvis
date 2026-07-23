# JARVIS V69 M57 — Adaptive Streaming Response & Conversation Efficiency

Branch: `jarvis-v69-m57-adaptive-streaming-response`
Base: `27dd783` (M56 merged into master)
Suite: **2579 passed / 18 skipped / 0 failed** (M57 base: 2327 / 18 / 0 → **+252 tests**)

---

## 1. The problem M55/M56 did not solve

M55 made the FIRST token fast (native `/api/chat`, `think:false`). M56 proved the
model stays resident and fixed the prewarm's context. Neither touched **sustained
generation**, which the host runs at ~5 tok/s:

| answer length | generation time at 5.2 tok/s |
|---|---|
| 50 tokens | ~10 s |
| 100 tokens | ~19 s |
| 200 tokens | ~38 s |

Every DIRECT_FAST turn asked for the same `num_predict=256` — the config default.
A greeting and a Kerberos deep-dive had identical budgets, so *answer length* had
become the dominant perceived latency. The fix is not another model: it is
answering a short question shortly.

---

## 2. Measured defect: M56's prewarm fix was defeated on the live path

M56.4 proved Ollama reloads the runner when generation parameters change
(prewarm `ctx=512` → turn `ctx=2048` cost **8 723 ms** of load on an
already-resident model; matched = **411 ms**) and pinned the PREWARM to
`settings.fast_context`.

It did not fix the **live turn**. `core.llm._adaptive_ctx` shrinks a short
conversation to `min(1024, base_ctx)`, so the operator's first real turn asked for
`num_ctx=1024` against a runner warmed at 2048 — and paid the reload anyway.

**Fix.** `core/generation_budget.resolve_live_fast_context()` is now the single
source both the prewarm and the live turn read. The `/v1` fallback pins the same
value whenever it is serving the warmed FAST model. `test_response_wiring_v69_m572`
locks the identity, and the live benchmark confirms `ctx=2048` on every contract.

---

## 3. Architecture

```
user intent
  → TurnPolicy (existing, unchanged)          request class / verify / RAG / risk
  → ResponseShape          M57.1              HOW to answer  (never WHO answers)
  → ContextComposer        M57.6              bounded layered prompt
  → GenerationBudget       M57.2              num_predict / ctx / sampling / bounds
  → native Ollama          M55 (unchanged)
  → StreamAssembler        M57.3              chunks → readable fragments
  → SpeechPlanner          M57.4              fragments → bounded utterances
  → QualityGovernor        M57.8              deterministic artefact checks
  → ContinuationState      M57.7              what can be resumed, from shown text
```

A contract describes **how the selected model should answer**. It never chooses the
model, transport or role, and `verify_policy` / tool eligibility / RAG eligibility /
risk class are **inherited verbatim** from `TurnPolicy` and carried only for
inspection.

### Contracts and reason codes

| contract | tokens (min/base/max) | example |
|---|---|---|
| INSTANT | 24 / 40 / 64 | "hola" |
| BRIEF | 64 / 96 / 128 | "como saco la raíz cuadrada" |
| STANDARD | 96 / 160 / 224 | "explícame herencia con un ejemplo" |
| TECHNICAL | 160 / 256 / 384 | "explica Kerberos con más detalle" |
| STRUCTURED | 128 / 224 / 352 | "cuáles son los tipos de datos" |
| CODE | 128 / 288 / 512 | "escríbeme una función" |
| DOCUMENT_GROUNDED | 96 / 176 / 288 | "según mi PDF…" |
| OPERATIONAL | 64 / 128 / 192 | "system status" |
| DEEP | 192 / 384 / 640 | router chose the DEEP role |
| ERROR_RECOVERY | 24 / 64 / 96 | resuming an incomplete answer |

19 reason codes make every selection inspectable (`GREETING_SMALLTALK`,
`SIMPLE_HOWTO`, `EXPLICIT_BRIEF_REQUEST`, `SECURITY_SENSITIVE_PROCEDURE`, …).
Precedence: recovery → router role → security/effectful → evidence-bound →
explicit turn instruction → session profile → request class.

**A brevity request never compresses a security-sensitive procedure below its
contract floor.**

---

## 4. Live benchmark (real host, real transport)

`python jarvis/scripts/bench_response_m57.py` — qwen3:8b, native, `think=false`,
server 0.32.1. No model pulled, no Ollama setting written, no server restart, no
semantic collection touched.

| prompt | contract | budget | ctx | 1st token | 1st fragment | total | tok/s | eval | truncated |
|---|---|---|---|---|---|---|---|---|---|
| hola | INSTANT | 40 | 2048 | 14 016 ms* | – | 16.0 s | 5.0 | 10 | no |
| raíz cuadrada | BRIEF | 96 | 2048 | 8 125 ms | 11 031 ms | 21.3 s | 5.4 | 71 | no |
| POO brevemente | BRIEF | 96 | 2048 | 2 406 ms | 7 812 ms | 13.9 s | 5.2 | 59 | no |
| herencia + ejemplo | STANDARD | 144 | 2048 | 7 828 ms | 10 328 ms | 35.0 s | 5.2 | 141 | no |
| Kerberos detalle | TECHNICAL | 255 | 2048 | 7 734 ms | 15 938 ms | 60.0 s | 4.9 | 255 | **yes** |
| English polymorphism | STANDARD | 144 | 2048 | 6 812 ms | 8 969 ms | 20.5 s | 5.5 | 75 | no |

\* first call after the probe — a cold activation.

- throughput 4.9 / 5.2 / 5.5 tok/s (min/median/max) — consistent with M56
- **fragments/turn = 4.0 against chunks/turn = 100.8** — a **25× reduction** in
  console writes; this is the character-flood fix, quantified
- max assembler buffer 398 chars (ceiling 400) — bounded as designed
- `num_ctx = 2048` on **every** contract — the prewarm-parity invariant holds live
- TECHNICAL hit its cap at exactly `eval_count = 255 = num_predict` and was
  reported truncated, not presented as complete

### First-token A/B (same warm model, back-to-back)

| system prompt | prompt-eval tokens | first token |
|---|---|---|
| none | 18 | 1 422 ms |
| lean only | 42 | 3 437 ms |
| lean + M57 style directive | 77 | **4 187 ms** |
| lean + style, **repeated** | 77 | **1 250 ms** |

**M56's warm first-token guarantee is preserved** (1 250 ms with the full M57
system prompt). A contract pays ~2.9 s of prefill the FIRST time its distinct
style tail is seen; an identical prefix is then nearly free.

This is the first **directly observed** evidence of server-side prompt-prefix
reuse in this project — 4 187 → 1 250 ms with identical `load_duration`. M57.6.2's
cache key (model, role, transport, ctx, system-prompt fingerprint, language,
contract, policy version) is therefore keyed on reality, not on an assumption.
The style directive is appended **last**, so the lean prefix stays shared across
contracts and only the tail re-evaluates.

### Interruption (live)

```
visible chars before interrupt = 174 (3 fragments)
cancel requested at 11 031 ms → stream closed at 11 031 ms
teardown latency = 0 ms
late chunks emitted = 0
next turn first token = 1 313 ms   (next_turn_ok = True)
```

---

## 5. What each milestone changed

**M57.1 / M57.2 — contracts and budgets.** `core/response_contract.py` +
`core/generation_budget.py`. Sampling reaches the wire through an **allowlist** on
`build_chat_request` (`top_p`, `repeat_penalty` only) — `num_ctx`, `tools` and
structural options can never be injected by a caller. Budgets adapt from measured
throughput: median of a bounded 20-sample ring, ≥3 samples required, movement
capped to [0.5×, 1.5×] of the contract base, and the remaining turn time can only
shrink the result. One abnormal run cannot distort policy (implausible samples are
*rejected*, not clamped). Battery reduces the ceiling **and** the base — capping
only the ceiling left an unadapted turn byte-identical on battery.

**M57.3 — sentence-aware rendering.** `core/stream_assembler.py` replaces
per-delta console posting and the `(?<=[.!?;:])\s+` splitter that broke `3.14`,
`Dr. House`, URL paths, `10:30` and every code line. Fragment kinds: SENTENCE,
PARAGRAPH, LIST_ITEM, CODE_LINE, CODE_BLOCK_BOUNDARY, TEXT, FINAL_STATUS.
**Conservation invariant:** concatenating emitted fragments reproduces the stream
exactly — a test asserts it, which is what makes "no duplicate text, exact
ordering" a property rather than a hope.

*Defect found in this milestone's own wiring:* an idle flush called after `push()`
can never fire, because the push just reset the idle clock. It is now a bounded
timer task, cancelled in the producer's `finally`.

**M57.3.1 — first-sentence optimization.** A bounded, purely stylistic directive:
open with the answer, no restated question, no long courtesy preamble. No
subject-specific content is hardcoded anywhere.

**M57.4 / M57.4.1 — progressive speech.** `core/speech_stream.py` is a pure
planner; the existing TTS queue remains the only speech worker. It fixes a real
defect: assistant sentences were enqueued at `TTSPriority.NORMAL` with no key —
exactly as droppable as boot narration — so `cancel_boot_narration()` silently
killed the operator's own answer. Answers are now HIGH, conclusions and status
CRITICAL, keys scoped per turn. Backlog is bounded: intermediate speech is dropped
while the conclusion and status survive, and **the text is never shortened to
accommodate speech**.

**M57.5 / M57.5.1 — barge-in.** The runtime gains a **turn identity**. Terminal
states: ACTIVE / COMPLETED / INTERRUPTED_BY_OPERATOR / REPLACED_BY_NEW_TURN /
TIMED_OUT / FAILED / CANCELLED_ON_SHUTDOWN. Shutdown cancellation and operator
interruption are deliberately distinct — conflating them would offer a
continuation for an answer killed by shutdown. `accepts(turn_id)` gates every
fragment before display, so a replaced turn's late output is refused and counted.

`core/response_commands.py` is an **exact-match allowlist over the whole line**:
`/stop the port scan` is not `/stop`, it is a user turn. No command takes an
argument, so no path, PID, scope or value can arrive from free text.

**M57.6 / M57.6.1 / M57.6.2 — bounded context.** Both transports previously sent
`[system] + entire history`, forever. `core/context_composer.py` assembles with an
explicit retention order — SYSTEM, PINNED preferences, CURRENT message and TOOL
evidence are protected; MEMORY, RECENT and DIGEST trim in that order, and inside
RECENT greetings and repeated status lines go before content. **A token budget may
not change what is true or what is allowed.**

`core/conversation_digest.py` compacts extractively and labels every item:
EXPLICIT (the user said it) / OBSERVED (measured from the transcript) / INFERRED
(model-assisted, never authoritative) / UNKNOWN. The labels travel into the prompt
with a header stating the contract. The merge validator forces model-assisted
items to INFERRED regardless of what the model claimed, accepts only
topic/decision/open-question kinds, and drops anything duplicating or
contradicting an EXPLICIT item.

*Metric-honesty fix:* the recent-message pool cap discarded turns **without
counting them**, so `trimmed_items` reported 0 while 68 turns had been dropped.

**M57.7 / M57.7.1 — continuation.** Deterministic intents, most-specific first
(`hazlo más corto` is SHORTEN, not MORE_DETAIL). It remembers only bounded,
already-VISIBLE facts: the last stable **displayed** boundary, structural
checkpoints at headings/list items/closed code blocks, and a topic fingerprint. A
refusal costs **zero generation**; a topic change clears the cursor.

**M57.8 / M57.8.1 — quality governor.** Deterministic checks over the output
artefact only. **Never calls a model.** The permission boundary is explicit:
suppression, blocking and bounded retry exist ONLY before any visible content.
Once the operator has seen text, the runtime is honest about it — a truthful
one-line status, an unclosed fence closed without touching a word — never a silent
rewrite.

**M57.9 — commands, health, soak.** Five read-only panels; one advisory
`response_pipeline` health subsystem (rank 0, 38 bounded content-free metrics)
extending the single existing surface; a 34-turn soak asserting no context growth,
no speech-queue growth, coherent history, and no orphan task.

---

## 6. Two production defects found by the full suite

**A lingering mute after `/stop`.** `TTS.interrupt()` sets the process-wide
`cancel_bus.tts_cancel`, and the speech worker only clears it *while handling an
utterance*. With an empty queue and nothing speaking, the flag survived and the
**first sentence of the next turn** was silently swallowed by the pre-speech
drain. `/stop`, or `/mute` then `/unmute`, cost the operator their next answer's
opening line for no visible reason. `cancel_answer_speech` now leaves the flag set
only when there is genuinely an utterance in flight to preempt.

**A latent cross-instance leak.** `core.cancel_bus` starts uninitialized, so
`TTS._teardown()`'s "set `tts_cancel` if not None" is a no-op for most of the
suite. The first test to initialize the bus silently changed that for every later
test. The M57 tests now borrow the bus through a context manager and restore it.

---

## 7. Operator surface

```
/brief /standard /detailed /auto      session verbosity
/mute /unmute                         speech
/stop                                 cancel active generation + speech
/continue                             conversational — reaches the model
/response-status /response-profile /latency /context-status /tts-status
```

Configuration (all clamped in `core/config.py`):
`response_contracts_enabled`, `response_profile`, `response_max_output_tokens`
(32–1024), `response_adaptive_budget`, `response_stream_flush_ms` (100–5000),
`response_max_buffer_chars` (80–4000), `response_progressive_tts`,
`response_tts_backlog` (1–12), `response_context_tokens` (256–8192),
`response_digest_max_chars` (120–4000).

---

## 8. Invariants held

No model downloaded. No Ollama setting written. No server restart. No persistent
host change. No semantic collection mutated. ToolExecutor, Authority Modes,
ScopePolicy, risk classification, HITL, NATO/OTP, audit, lifecycle, truthful
readiness, language continuity, deterministic bypasses, bounded queues and clean
shutdown are all untouched. Chain of thought, internal prompts, tool schemas,
secrets and tokens never appear in output, history, metrics or panels.

---

## 9. Limitations

1. **Text-mode barge-in is submission-based.** The reader is a blocking line read
   in an executor; no raw-keystroke backend exists, and none was invented.
2. **A contract's first use pays ~2.9 s of prefill** for its distinct style tail.
   Shortening the directives, or sharing more prefix, is an M58 opportunity.
3. **Cold first token is still 8–14 s** when the model is not resident. That is
   M56's territory (prewarm), not M57's.
4. **The `/v1` path caps `num_predict` only on tool-free legs** — a cap landing
   mid-tool-call would truncate the JSON and break the agentic loop.
5. **Model-assisted digest compaction is implemented but not wired to a
   scheduler**; the extractive digest is the only live path.
6. **`_adaptive_ctx` still governs DEEP/CODER turns.** Only the FAST model's
   context is pinned, because only it is prewarmed.

---

## 10. Recommendation for M58

The remaining latency is **prefill**, not generation. Measurement, not assumption,
should drive it:

1. Measure `prompt_eval_count` / `prompt_eval_duration` per contract on the live
   path and confirm the prefix-reuse boundary directly (M57 observed 4 187 →
   1 250 ms; M58 should characterise *when* reuse is lost).
2. Shorten and share the style directives so the cached prefix covers more of the
   system prompt.
3. Extend the prewarm to warm the *actual* live system prefix, not `"ok"` — M56
   matched `num_ctx`; the next parity gap is the prompt itself.
4. Only then consider a smaller FAST model, and only with a measured A/B.
