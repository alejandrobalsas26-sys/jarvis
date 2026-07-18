# JARVIS V69 M55.1–M55.6 — Dispatch, Stream, Readiness & Service Lifecycle

Reliability pack repairing the remaining live-integration failures exposed after
**V69 M55 (native Ollama fast path)** was merged and manually executed on master
`d101a83`. It extends existing seams only — the native transport, model router, turn
policy, turn budget, lifecycle FSM, ConsoleCoordinator, FastReadiness, Knowledge
Vault, semantic continuity, ToolExecutor, verification policy and TTS governor are all
preserved.

Baseline before: **2068 passed / 18 skipped / 0 failed**.
After: **2111 passed / 18 skipped / 0 failed** (+43 regression tests, 0 regressions).
Live soak against real Ollama 0.32.1: **14/14 checks pass**.

---

## The live evidence (master d101a83)

A real manual run produced, in order:

| time | observation |
|---|---|
| 16:52:15 | `LIFECYCLE: OPERATIONAL — text_ready=Nonems core_ready=12907ms operational=13469ms`, then the prompt appeared |
| during prompt | `Tú: ERROR:jarvis.db_manager:PostgreSQL unavailable — No module named 'asyncpg'` (background log on the input line) |
| 16:52:20 | `FAST_READINESS: UNAVAILABLE model=qwen3:8b` |
| 16:52:39 | `NATIVE_PROBE: state=NATIVE_READY think_false_supported=True version=0.32.0` |
| ~16:53:15 | operator submits "explicame como sacar la raiz al cuadrado" |
| 16:53:58 | `MCP: Conectado al bridge.` immediately followed by `FAST_ROUTE: native no-think` — **~43s dispatch delay** |
| — | answer streamed partially ("… Por ejemplo,") then stopped; no completion marker, no restored prompt |
| 17:01:45 | operator closes JARVIS; Uvicorn prints `asyncio.exceptions.CancelledError` inside the Starlette lifespan |
| boot | `OLLAMA CONFIG: OLLAMA_NUM_PARALLEL=1 … MAX_LOADED_MODELS=1` (reads like verified server config) |

Every root cause below was proven at the code, not guessed.

---

## M55.1 — Immediate Turn Dispatch Isolation

**Root cause of the 43s.** `LLM.chat_stream` unconditionally `await self._init_mcp()`
at its top (`core/llm.py`), *before* routing and the tool-free native FAST branch. On
the first turn that lazily cold-spawned the MCP stdio bridge subprocess
(`packet_tracer_bridge.py` via `sys.executable`) + handshake — ~43s on the 15 W CPU —
and it was even billed against the turn budget. `MCP: Conectado al bridge` (logged at
the *end* of `_init_mcp`) landing immediately before `FAST_ROUTE` was the fingerprint.
A second, independent cost was hiding behind it: `refresh_threat_enrichment()` ran
before the fast-route decision though the native path uses the lean prompt.

**Fix.**
- MCP connects in its **own supervised background task** (`LLM.start_mcp_background`,
  kicked off at boot off the critical path). DIRECT_FAST never awaits it.
- The tool-chat path awaits it via `LLM._ensure_mcp(timeout=remaining_budget)` —
  shielded, so a turn timeout cancels the *wait*, not the shared connection; a still-
  cold bridge proceeds with local tools rather than stalling. `aclose` cancels a
  still-warming MCP task before tearing down its exit stack.
- Threat enrichment is deferred to the non-fast path.
- A truthful `pre_inference dispatch_ms` (message-in → transport selected) is logged on
  the `FAST_ROUTE` line with a 1 s-ceiling warning, and the classification + task-
  decision path is warmed at boot so the operator's first turn is warm.

**Measured.** Warm pre-inference dispatch **~0 ms** (live soak); cold first turn ~1.0–
1.3 s (one-time lazy import/compile, warmed at boot in production). Was ~43 s.

### M55.1.2 — input-line safety
The ConsoleCoordinator only intercepted **loguru**. `db_manager` and ~30 core modules
use stdlib `logging`, which had no handler and hit logging's *lastResort* stderr path —
exactly the `Tú: ERROR:jarvis.db_manager:…` corruption. A root logging bridge
(`core.console.install_stdlib_logging_bridge`) now routes those records through the
coordinator (erase/redraw around the prompt) and disables lastResort. WARNING/ERROR
stay fully visible on their own line.

---

## M55.2 — Native Stream Completion & Prompt Recovery

A native turn now resolves to **exactly one** terminal state — `COMPLETED`,
`TIMED_OUT`, `CANCELLED`, `FAILED`, `DISCONNECTED` — through a single idempotent guard
(`LLM._finalize_native_turn`, replacing `_finalize_fast_turn`). It finalizes history
once (full text on success; partial + a short localized *"answer left incomplete"*
status on `TIMED_OUT`/`DISCONNECTED`/`FAILED`; drop-dangling on a content-free cancel),
records readiness/health once, and never depends on TTS/MCP/console.

`_native_fast_stream` tracks `chunks_received / content_chars / done_received /
stream_closed / done_reason / final_state`, distinguishes a clean end-of-stream from a
mid-stream disconnect after partial content, and **captures the partial into
`result["text"]` before each yield** — because an `async for` does not close the inner
generator before `GeneratorExit` reaches the caller's handler. The FAST branch wraps
the serve in one `try/finally` that also catches `GeneratorExit` (the turn-level
`bounded_stream` deadline aclose()ing mid-stream) — the exact case that previously left
a partial answer dangling with no prompt and let the **next** turn answer the
**previous** question.

`main._run_turn`'s TTS consumer swallows a `speak_async` fault (never
`CancelledError`), so a wedged pyttsx3/COM voice can no longer block prompt restoration.

---

## M55.3 — Truthful Readiness & Boot-Phase Synchronization

**FAST readiness** (`core/fast_readiness.py`): new `PROBING` state; `probe()`
transitions `CONFIGURED/UNKNOWN → PROBING` while in flight and resolves a *single*
failure to `WARMING`, never a premature `UNAVAILABLE` — the boot-time nomic embedding
load makes Ollama momentarily unresponsive under `OLLAMA_MAX_LOADED_MODELS=1`, which
produced the 16:52:20 verdict 19 s before `NATIVE_READY`. `reconcile(cap)` is the
truthful verdict from **both** probes; `UNAVAILABLE` requires native **and** fallback
proven unavailable (and the server never reached), else `DEGRADED`. `PROBING`/`WARMING`
keep accepting input.

**TEXT_READY timing** (`core/lifecycle.py`): `note_reader_ready()` stamps the real
reader-live moment and **backfills** the `TEXT_READY` phase time when boot advanced past
it before the reader existed (CORE_READY first) — so `text_ready_ms` is never `None`
once the prompt accepts input. `mark_text_ready()` keeps its monotonic semantics; the
FSM is never moved backward. `snapshot()` now exposes `console_ready_ms` /
`reader_ready_ms` alongside text/core/operational.

---

## M55.4 — Optional Service Supervision & Clean Shutdown

`core/optional_service.py` — one small contract (not a new framework; does not replace
TaskWatchdog): `OptionalService` with a truthful state machine
(`REGISTERED/STARTING/READY/DORMANT/DEGRADED/STOPPING/STOPPED/FAILED`), `ready_event`,
`criticality`, and a bounded cancellation-safe `stop()`. `start()` refuses to run once
the runtime is `STOPPING`; a failed start is `FAILED`+`last_error`, never a crash.

**M55.4.1 clean Uvicorn shutdown.** The live `CancelledError` traceback came from
`run_graceful_shutdown`'s blanket task-cancel hitting `serve()` **mid-lifespan**.
`stop_uvicorn_gracefully()` stops AURA through its supported API (`should_exit`) and
awaits `serve()` bounded **before** the blanket cancel; expected `CancelledError` at the
ownership boundary is suppressed (logs `AURA stopped normally`), a real `serve()` error
is still reported, force-cancel is a last resort. No uvicorn monkey-patching.

**M55.4.2 ordered stop.** AURA/network stops before storage is checkpointed/closed, so
no service writes to a closed DB and the aura-server task is never blanket-cancelled
mid-lifespan.

---

## M55.5 — Ollama Server Posture Truth & Residency Guidance

`configure_ollama_for_hardware` no longer prints `OLLAMA CONFIG:` (which read like
verified config). It logs a clearly-labelled **RECOMMENDED (advisory)** line plus THIS
process's env with an explicit *"NOT the server's / not API-verifiable"* caveat.

`core/ollama_env.py` `posture_report()` surfaces the **five honest categories**, never
conflated: recommended / jarvis-process-env / server-observed / server-settings-verified
(always `False`) / unknown (the server's real parallel + max-loaded, which the API does
not expose). `residency_guidance()` is deterministic, recommends
`OLLAMA_MAX_LOADED_MODELS=2` (room for FAST + nomic so an embedding call does not evict
FAST), notes a restart is required, and makes **no** memory/latency claim without a
measurement. Logged at boot after the native probe (`OLLAMA POSTURE` / `OLLAMA
RESIDENCY`). No server restart/kill, no persistent env mutation.

---

## M55.6 — Live Interaction Soak

A bounded soak (`scratchpad/live_soak_m556.py`) against the real Ollama 0.32.1, event
loop, native transport, probe, console bridge and shutdown validated end-to-end:
native probe → `NATIVE_READY` (`think_false_supported=True`); readiness reconciled to
`READY` (no premature `UNAVAILABLE`); DIRECT_FAST answered with a 120 s-stalling MCP;
warm dispatch ~0 ms; native no-think, Spanish, no `<think>`; coherent history (turn 2 did
not answer turn 1); time bypass 0 ms; posture never verified; Uvicorn stopped normally
with no traceback; `aclose` left no orphan MCP task. **14/14.**

---

## Remaining limitations

- The **very first** DIRECT_FAST turn in a cold process still pays a one-time lazy
  import/compile cost (~1.0–1.3 s dispatch). Production warms the classification +
  task-decision path in `_fast_warmup`'s background task at boot, so the operator's
  first real turn is warm (~0 ms in the soak). Warm dispatch is always ≪ 1 s.
- Ollama server `OLLAMA_NUM_PARALLEL` / `OLLAMA_MAX_LOADED_MODELS` remain **UNKNOWN** —
  the API does not expose them and JARVIS never claims otherwise. Applying the residency
  recommendation (`MAX_LOADED_MODELS=2`) requires editing the server environment and
  restarting the Ollama service; this is operator guidance only.
