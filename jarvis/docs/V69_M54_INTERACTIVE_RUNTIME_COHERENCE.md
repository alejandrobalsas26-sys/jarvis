# JARVIS V69 M54 — Interactive Runtime Coherence, Latency Control & Clean Lifecycle

M54 repairs the *integration* seams between boot, console I/O, background logging,
routing, verification, TTS, schedulers, signals and shutdown. The architecture from
V61–V69 M53 was sound; a real manual execution exposed that the pieces did not
coordinate during live human use. This milestone adds the missing coordination spine
and makes the runtime behave coherently, without adding specialist agents, a new
model router, or a console rewrite.

## Root causes from the live run

| Symptom | Root cause |
|---|---|
| Background logs merged into the input line; tool JSON read as input | Three unsynchronized TTY writers (loguru→stderr, `print(chunk)`, blocking `input()`) |
| Input accepted during boot | No readiness gate; the read loop was reachable while warmup ran |
| "POO" routed to `query_knowledge`, refused on empty vault | `query_knowledge` unconditionally offered + a prompt pushing RAG for any subject |
| Language drift ES→EN | No text-mode conversation-language state; prompt rule was soft |
| Verifier "20 s" but 12-minute turn | Only the verifier *call* was bounded; the whole turn was not |
| "Episodic online / All nominal" while REINDEX_REQUIRED | Semantic summary computed but never fed into the boot snapshot |
| "No tengo acceso a la hora real" | Time was model-optional; nothing grounded it |
| "TTS dropped 28 pending" | Unbounded FIFO; boot narrated faster than synthesis |
| Repeated "SIGINT received" | Handler logged/acted on every signal, no idempotency |
| "HUNT: running H04" after shutdown | No global STOPPING state consulted before new work |
| Storage flushed while jobs could still write | Callbacks (flush) ran before task cancellation |

## Why existing tests missed them

Every module had strong isolated unit tests, but there was **no live-console / boot /
shutdown integration harness**. The failures were all in the *coordination* between
components (writer serialization, readiness ordering, whole-turn budget, truth
enforcement on the user-facing path), which no single-module test exercised. M54.14
adds that harness.

## The coordination spine

- **`core/lifecycle.py` (M54.10)** — one authoritative finite state machine
  (`STARTING → TEXT_READY → CORE_READY → OPERATIONAL → STOPPING → STOPPED / FAILED`)
  plus a monotonic phase-timing ledger. `begin_stopping()` is the idempotent gate the
  signal handler and shutdown driver share; `can_start_task()` / `is_stopping()` are
  the guards every task-creation and scheduler-iteration seam consults. Modules import
  the *call-time* convenience functions (`is_stopping()`, `begin_stopping()`), never
  the object, so the state is observed consistently.

- **`core/console.py` (M54.1)** — the single owner of every interactive terminal
  write. Producers enqueue typed `ConsoleEvent`s onto a bounded queue; one renderer
  thread erases + redraws the active input line around each framed log, so background
  output can never merge into the prompt, and tool JSON is posted as a framed line
  rather than fed to the input parser. Bounded queue with coalescing (low-value
  repeats) + drop-oldest-droppable backpressure; WARNING/ERROR/HITL lanes are never
  dropped. Works with plain stdout (no Rich/prompt_toolkit), Windows ANSI erase with
  CR fallback, never blocks the loop, bounded shutdown flush, no orphan thread. A
  loguru sink routes log records through it.

## Boot phases (M54.2)

`main.py` advances the lifecycle: PROCESS_STARTED (console + config + security
controls) → TEXT_READY (console + FAST routing + executor/LLM/TTS; input enabled) →
CORE_READY (startup diagnostic complete) → OPERATIONAL (warmup registered). The read
loop is gated on `lifecycle.accepts_input()`. Phase timings
(`text_ready_ms`/`core_ready_ms`/`operational_ready_ms`) are exposed through runtime
health.

## Routing discipline (M54.3)

`core/turn_policy.py` classifies each turn once, deterministically, into a request
class with an inspectable **reason code**:

| Request class | Reason code | Vault? | Verify policy |
|---|---|---|---|
| ordinary_conversation | `DIRECT_FAST` | no | SKIP_LLM_VERIFIER |
| general_educational ("POO") | `DIRECT_FAST` | **no** | DETERMINISTIC_CHECKS_ONLY |
| coding_explanation | `DIRECT_FAST` | no | DETERMINISTIC_CHECKS_ONLY |
| private_document ("mi PDF") | `PRIVATE_RAG` | **yes** | GROUNDING_CHECK |
| memory_recall | `MEMORY_RECALL` | no | DETERMINISTIC_CHECKS_ONLY |
| operational_status | `OPERATIONAL_QUERY` | no | EVIDENCE_REFERENCE_CHECK |
| current_time | `DETERMINISTIC_TIME` | no | SKIP_LLM_VERIFIER |
| effectful_tool | `TOOL_REQUIRED` | no | FULL_VERIFICATION |
| cyber_sensitive | `AUTHORIZATION_REQUIRED`/`TOOL_REQUIRED` | no | BOUNDED_MODEL_VERIFIER |

The policy withholds the private-vault tool family (`query_knowledge`) from the
per-turn tool set unless the turn is a private-document query — so general knowledge
is answered directly and never held hostage to an empty vault. It composes the
existing `classify_domain` + `is_security_sensitive_turn` + operator authority; cyber
turns fail closed without an established scope.

## Language continuity (M54.4)

`core/language_context.py` gains a deterministic (no-LLM) text detector, a sticky
explicit override ("answer in English"), and a first-party system-prompt directive.
Ambiguous tokens ("POO") inherit the active language; tool failures / verifier
timeouts / model switches never reset it. The `LLM` holds one shared context fed by
each user turn.

## End-to-end turn budget (M54.5) & verification policy (M54.6)

`core/turn_budget.py` gives the whole turn a real, risk-sized deadline (25 s simple …
120 s effectful) with a phase-timing ledger. The verifier receives only the
**remaining** budget (never a fresh full timeout) and is skipped with a concise
human-review status once the budget is exhausted — no turn can block for minutes.
Policy-exempt tool-free turns (greetings, basic education, time) skip the LLM verifier
entirely, while any turn that actually ran a tool keeps the existing high-risk gate.

## Truthful readiness (M54.7)

`BootState` folds `semantic_boot_summary()` into `semantic_degraded` /
`episodic_reindex_required` / `knowledge_vault_active`. A required episodic reindex
degrades health and blocks `all_systems_nominal()`; the memory line no longer claims
"Episodic memory online", and the ready line uses the allowed wording ("JARVIS is
ready with degraded semantic memory. Knowledge Vault is active. Episodic memory
requires migration."). One snapshot; every consumer renders from it.

## Host-time grounding (M54.8)

`core/host_time.py` is the single deterministic clock source. An authoritative "HOST
CLOCK … never say you lack real-time access" fact is injected into the system prompt
every turn; the model may format it but never invents or refuses it.

## TTS governance (M54.9)

`core/tts_queue.py` (`TTSGovernor`) bounds the queue with four priorities
(CRITICAL/HIGH/NORMAL/LOW), duplicate suppression, coalescing by event key, TTL stale
expiration, LOW-dropped-first backpressure, and metrics. Boot narration is LOW
priority and cancellable the moment the operator interacts; shutdown keeps only
HIGH/CRITICAL and drains them briefly. Offline pyttsx3 and the bounded daemon-worker
shutdown are unchanged.

## Lifecycle, signals, schedulers, shutdown (M54.10–M54.12)

- One SIGINT starts exactly one shutdown; repeats are no-ops; a third insists →
  documented emergency `os._exit`.
- `TaskWatchdog` gains `request_stop()/stop()/status()`; it refuses new supervision
  and stops restarting once STOPPING.
- The hunt scheduler consults `is_stopping()` before each sweep and each hypothesis —
  no H04 after STOPPING.
- `run_graceful_shutdown` order: STOPPING → stop watchdog → **cancel tasks (bounded)**
  → **then** checkpoint/flush/close callbacks → audit → STOPPED. Storage closes only
  after writers are stopped; the semantic checkpoint stays durable.

## Live-interaction harness (M54.14)

`tests/test_live_interaction_harness_v69.py` replays the exact broken scenario with
fakes and simulated time (no live Ollama): input gated before TEXT_READY, logs don't
clobber the prompt, tool JSON framed not parsed, hola/POO answered directly with no
vault/verifier and in Spanish, private-PDF routed to RAG, deterministic host time,
REINDEX_REQUIRED blocking "All systems nominal", the turn budget bounding the
verifier, the TTS queue bounded/coalescing, one-SIGINT-one-shutdown, no hunt after
STOPPING, and semantic checkpoint before storage close.

## Regression status

Full repository suite: **1929 passed, 18 skipped, 0 failures** (baseline before M54:
1841 passed / 18 skipped). Ruff clean on all changed files; `compileall` clean.

## Remaining limitations

- The single-threaded boot still starts the read loop after warmup registration; text
  input is enabled at the lifecycle/gate level early, but a full concurrent-input
  restructure (typing *during* warmup) is deferred.
- Live-terminal prompt redraw restores the prompt marker but cannot repaint
  characters already echoed by the OS mid-line without a raw-mode reader
  (prompt_toolkit); this is strictly better than the previous total corruption and
  bounded by the "usable without prompt_toolkit" constraint.
