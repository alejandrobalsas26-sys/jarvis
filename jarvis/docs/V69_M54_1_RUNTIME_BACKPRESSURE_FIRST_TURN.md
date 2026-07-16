# V69 M54.1 — Runtime Backpressure, First-Turn Reliability & True Text Readiness

A reliability patch over M54. It repairs four failures a real manual execution on
`a3f737a` exposed, which the 1929-test suite did not catch. It adds no new agents,
no new router, no new UI, and does not begin M55.

## The four live failures

| # | Symptom | Proven producer | Root cause |
|---|---------|-----------------|------------|
| 1 | Dozens of `ERROR:asyncio: … Queue.put_nowait(WindowsPath(…)) QueueFull` during boot | `tools/yara_file_monitor.py:176,186`; `tools/memory_hunter.py:297` | `except QueueFull` around `call_soon_threadsafe` is dead code; the watcher watched the repo through `~/Downloads` |
| 2 | "como saco la raiz cubica de algo" never returned; operator hit Ctrl+C | `core/llm.py:926`, `:1885`; `main.py:189` | `TurnBudget` bounded only the verifier; `AsyncOpenAI` inherited the SDK's `read=600` |
| 3 | Greeting said "Pues ahora mismo son **[hora actual]**" | `main.py:200` | The prompt ordered the model to state the time and never supplied it |
| 4 | `TEXT_READY` logged while no prompt existed | `main.py:962` vs `:2188` | The state was a claim about intent; nothing bound it to a reader |

## 1. The QueueFull storm

### Why the existing `try/except` could never work

```python
try:
    loop.call_soon_threadsafe(scan_queue.put_nowait, Path(event.src_path))
except asyncio.QueueFull:      # dead code
    pass
```

`call_soon_threadsafe` only *schedules* a `Handle` and returns on the watchdog
observer thread. It raises `RuntimeError` when the loop is closed — never
`QueueFull`. The `put_nowait` runs **later, on the loop thread**, by which time this
`except` frame has already returned. The exception therefore escaped into the event
loop's default exception handler, which printed one full traceback per dropped
event. The guard created a false sense of safety.

`tests/test_safe_enqueue_v691.py::test_unsafe_shape_leaks_queuefull_to_loop_handler`
characterizes this deterministically: it installs a loop exception handler and fires
from a **real thread** (an inline call would let the dead `except` catch spuriously
and pass against a broken implementation).

### Why it overflowed at all

`_MONITOR_PATHS` was hardcoded to `[<repo>/analyze_inbox, Path.home()/"Downloads"]`,
scheduled `recursive=True`. On the target host the repo **is**
`C:\Users\aleja\Downloads\jarvis_v2\jarvis` — strictly inside the watch root. The
module docstring three lines above swore:

> NEVER watch the repo root: it contains logs/, and JARVIS log writes would trigger
> scans → which write logs → infinite YARA loop / QueueFull.

Watching `~/Downloads` defeated that reasoning transitively. **JARVIS watched
itself.** Meanwhile `_WATCHED_EXTENSIONS`, the intended executable allowlist, was
dead code (one repo-wide hit: its own definition) — `_should_scan` only excluded, so
`main.py`, `tests/*.py` and `vector_store/*.bin` were enqueued **and really
scanned**. Exclusions were substrings over the whole path (`"log" in str(p)` also
matches `catalog`; `tmp` was never checked), while components `core`/`tools`/`aura`
skipped any such folder anywhere under Downloads — a genuine detection blind spot.

### The fix

`core/safe_enqueue.py` fixes the **shape, not the size**:

```python
def _put_in_loop(self, item, priority):     # runs ON the loop
    try:
        self.queue.put_nowait(item)
    except asyncio.QueueFull:
        self._handle_overflow(item, priority)   # counted, never raised
```

`SafeEnqueue.offer()` is callable from any thread, never blocks, never raises. It
carries producer-side debounce/coalescing (a bounded LRU, so memory stays bounded
under a storm of unique paths), priority (`HIGH` evicts an older `LOW` rather than
being dropped), lifecycle awareness (`STOPPING` refuses new low-priority work), and
**one aggregated warning per cooldown** instead of one per path:

```
FILE_WATCHER: backpressure active — 645 events coalesced, 27 dropped
```

`core/watch_policy.py` replaces the ad-hoc filter with one explicit, path-aware
policy: component-wise matching on normalized paths (`C:\foo-bar` is not inside
`C:\foo`; case never matters on Windows), the repo tree excluded except
`analyze_inbox`, and **SECURITY_SCAN** (executables in the security root) split from
**CODE_ANALYSIS** (source in the inbox) onto separate bounded queues so one burst
cannot starve the other.

Verified on the real host — before vs after:

| Path | Before | After |
|------|--------|-------|
| `<repo>/main.py` | SCAN | skip |
| `<repo>/tests/test_console_v69.py` | SCAN | skip |
| `<repo>/vector_store/index.bin` | SCAN | skip |
| `~/Downloads/setup.exe` | SCAN | **SCAN** (detection intact) |
| `~/Downloads/readme.txt` | SCAN | skip |

### Overflow recovery

`core/watch_reconcile.py` makes dropped events honest. Overflow marks the root
`STALE` — we never claim nothing changed — and schedules **exactly one** bounded,
lifecycle-aware, paged rescan per episode (never one per dropped event). It refuses
to start after `STOPPING`, stops mid-flight on shutdown, and reports
`CURRENT / RECONCILING / STALE / DEGRADED` truthfully (a truncated scan is
`DEGRADED`, not `CURRENT`).

Operator config (clamped, never raised): `WATCH_INCLUDE`, `WATCH_EXCLUDE`,
`WATCH_QUEUE_SIZE`, `WATCH_DEBOUNCE_MS`, `WATCH_SECURITY_ROOT`.

## 2. The first turn that never returned

Two independent defects compounded.

**The budget was a passive stopwatch.** `core/turn_budget.py` imported no `asyncio`
at all — pure arithmetic over an injected clock that must be *voluntarily polled*.
The generation path never polled it: `_budget` reached exactly **one** call site
repo-wide, `budget=_budget` into the verifier. M54 deadlined the one stage that was
already bounded (a 20 s `wait_for`) and left the stage that hung untouched. `main.py`
never imported `turn_budget` at all. Five of six declared `_PHASES` were never
stamped, so health reported `generation_ms=0.0` forever.

**The only real bound was an accident.** `core/llm.py:926` built
`AsyncOpenAI(base_url=…, api_key="ollama")` with no `timeout=`. Verified against the
installed **openai 2.36.0**:

```
DEFAULT_TIMEOUT = Timeout(connect=5.0, read=600, write=600, pool=600)
```

The 5 s connect always succeeded instantly (Ollama's listener is up); the wait
happened **after** connect while Ollama synchronously swapped models under
`OLLAMA_MAX_LOADED_MODELS=1`. A **600-second read timeout nobody chose** was the
turn's only ceiling. And the operator-interrupt check lived *inside*
`async for chunk in stream`, so it only ran when a chunk **arrived** — during a cold
load zero chunks arrive, making the cancel path structurally incapable of breaking a
pre-first-token stall.

### The design

The budget now starts the instant the message enters the turn — policy
classification included — and the boundary is real:

* `bounded_stream()` / `_iter_stream_bounded()` await `__anext__` inside
  `wait_for`, so a stall **inside** the generator (HTTP connect, model swap, silent
  socket) is *interrupted*, not merely measured;
* `wait_for` cancels **and awaits** the inner task before raising, so the generator
  is never left mid-flight (no "async generator is already running");
* `aclose()` always runs in a `finally` — on success, timeout **and** cancellation —
  so the inference lock releases, the live SSE response is torn down (a leaked pool
  slot would make the *next* turn hang on pool acquisition), and no late chunk or
  orphan inference survives;
* every step waits `min(stage bound, remaining total)`, so queue/lock wait and
  first-token wait count against the same total **by construction**;
* `main._run_turn` races the turn as a cancellable task and always restores the
  prompt with one concise message in the active language.

Separate observable bounds: `queue_wait`, `connect`, `first_token`, `idle`, `total`.

### Honest timing

Ollama cannot report model-load separately from prefill, so we record the
**observable** quantity (time to first token, which *contains* queue wait + model
swap + connect) and leave `model_load_ms` / `connect_ms` as `null`. Reporting `0.0`
would read as "instant" when the truth is "unknown".

### M54.1.7 — live calibration on the target host

Measured on the real host (AMD Ryzen 5 7430U, 15 W, CPU-only Ollama, `qwen3:8b`,
`num_ctx=2048`), not guessed:

| Measurement | Value |
|---|---|
| embedding (`nomic-embed-text`) load | 0.61 s |
| **cold first token** | **110.2 s** |
| cold total (2364-char answer) | 290.1 s |
| **warm first token** ("hola") | **10.3 s** |
| warm total ("hola", 32 chars) | 12.3 s |
| warm educational first token | 172.8 s |
| warm educational total (2380 chars) | 378.9 s |
| derived generation rate | **~13 chars/s** |

Two findings changed the design:

1. **The wait is not model loading — it is reasoning.** `qwen3:8b` is a reasoning
   model: it emits no *content* tokens while thinking, so "first token" for an
   educational question is 110–173 s even with the weights already resident. This
   fully explains the original symptom ("no visible answer for several minutes"):
   thinking silence plus a 600 s ceiling.
2. **A 2.4k-char answer costs ~3 minutes of generation** at ~13 chars/s, regardless
   of thinking. Neither `think: false` via the `/v1` shim nor the `/no_think` soft
   switch brought the educational turn under 150 s on this host.

So `first_token` is the **anti-silence** bound and `idle` is the **anti-stall**
bound; the total stays at the risk-sized interactive ceiling. Once tokens are
streaming the operator is reading, not waiting — silence was the pathology.

Budgets were raised from M54's 25/35 s (calibrated on a warm model, unreachable
during a cold swap — an unmeetable deadline is not safety) to 60/60/75/75/90/120 s,
and `first_token` to 90 s, clamped down to each policy's total. Overrides are capped
(`total ≤ 300 s`, scale ∈ [0.25, 3.0]) so an operator typo cannot recreate an
effectively unlimited wait.

**Honest consequence.** On this host, with `qwen3:8b` as FAST, the cube-root
question **bounds out cleanly** rather than answering: it exceeds a 60 s interactive
ceiling by physics. That satisfies the requirement the patch is about — control
returns, the prompt comes back, one concise Spanish message is shown, no orphan
inference is left holding the lock — but it is not a satisfying answer, and no
deadline can make a 3-minute reasoning chain interactive. That is a **model
configuration** problem, not a runtime-deadline problem. See "Remaining limitations".

## 3. Deterministic greeting

`main.py:200` told the model *"Dile la hora actual"* and never gave it one, so the
model emitted a placeholder for a value only the host knows. It also made the first
cold Ollama load happen **before the prompt existed**.

`core/greeting.py` renders it deterministically from `HostTime` with **no LLM call**,
reuses the one truthful M54 readiness claim (via `boot_state.readiness_sentence()`,
so the greeting can never contradict the boot snapshot), and validates the final
string for unresolved placeholders before emit — with a fallback that is itself
unconditionally clean (an early version leaked `{user_name}` through the fallback; a
test caught it).

## 4. TEXT_READY as a real guarantee

`mark_text_ready()` fired at `main.py:962`; the reader started at `main.py:2188` —
~1200 lines later, behind optional subsystem registration, self-test, boot
narration, briefing, MCP attachment, integrity regeneration and Whisper warmup, plus
a blocking LLM greeting.

The transition is now **gated on a bound reader**: `lifecycle.bind_input_reader()`
supplies the availability probe, `input_available()` is the capability question, and
`mark_text_ready()` refuses without it (`force_text_ready()` is the explicit
headless/voice escape hatch). The interactive loops flip the flag at the moment they
are about to read. Optional warmup continues behind the prompt.

`core/fast_readiness.py` answers whether FAST can actually serve a turn
(`CONFIGURED → REACHABLE → WARMING → READY`, or `DEGRADED`/`UNAVAILABLE`) — a
configured model *name* is not readiness. The probe is bounded metadata (no
inference); the prewarm asks for **one** token and is idempotent. `WARMING` and
`DEGRADED` still accept input: a bounded failure beats refusing to listen.

## Observability

`filesystem_watch` joins the **one** existing runtime-health surface (the YARA
monitor was TaskWatchdog-registered only, so its drops were invisible while the
console flooded). The turn rollup gained `timeout_stage`, `cancellations`,
`successful_turns`, first-token latency, and `input_available`. All bounded; no
prompt content, secrets or model payloads.

## Remaining limitations (honest)

1. **FAST is a reasoning model on a 15 W CPU.** Measured: 110–173 s to first
   content token and ~13 chars/s for an educational answer. No interactive budget
   can accommodate that. The runtime now fails *cleanly and fast* instead of
   hanging, but the operator still does not get an answer to a simple maths
   question. Fixing that means changing the model posture, not the deadline —
   the single highest-value next step (see below).
2. **Server-side generation can outlive client cancellation.** `aclose()` tears
   down the HTTP response, releases the lock, and guarantees no late chunk reaches
   the operator — verified. But Ollama may keep generating server-side for a while
   after the client disconnects; observed during calibration as CPU still busy
   after a killed request. We bound the *client*; we cannot force the server to
   abandon a running generation through the OpenAI-compatible endpoint.
3. **`OLLAMA_MAX_LOADED_MODELS` is not actually set in this environment.** Both
   `nomic-embed-text` and `qwen3:8b` were resident simultaneously during
   calibration, so the assumed embed→FAST eviction did not occur on this run. The
   code does not depend on the assumption (it simply does not pin both).
4. **`think: false` is not honored through Ollama's `/v1` OpenAI-compatible shim**,
   and the `/no_think` soft switch did not bring the turn under 150 s here.
5. The console coordinator still cannot perfectly restore typed characters without
   a raw-mode backend; unchanged from M54 and deliberately out of scope.

## Recommendation for M55

**Make the interactive FAST role non-reasoning.** Either point FAST at a
non-reasoning model, or drive `qwen3` through Ollama's native `/api/chat` with
`think: false` (the `/v1` shim ignores it) for `DIRECT_FAST` turns, and cap
`max_tokens` for `SKIP_LLM_VERIFIER` / `DETERMINISTIC_CHECKS_ONLY`. The measurements
above are the acceptance bar: a warm simple turn should reach first token in ~10 s
and finish well inside 60 s. Everything needed to verify it already exists —
`first_token_ms`, `timeout_stage` and `successful_turns` are in runtime health.

## Preserved

M52 (unified embeddings), M53 (semantic continuity) and M54 (lifecycle, console
coordinator, truthful readiness, language continuity, TTS governance, idempotent
signals, ordered shutdown) are extended, never replaced. The truthful boot snapshot
still reports `SEMANTIC MEMORY [DEGRADED]` / `jarvis_episodic: REINDEX_REQUIRED` /
`Knowledge Vault: ACTIVE` and still refuses to say "All systems nominal".
