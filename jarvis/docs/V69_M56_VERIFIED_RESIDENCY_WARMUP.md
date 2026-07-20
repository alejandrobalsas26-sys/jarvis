# JARVIS V69 M56 — Verified Model Residency, Full-Path Warmup & Resource Governance

Branch: `jarvis-v69-m56-verified-residency-warmup`
Baseline: `95db4dc` (M55.1–M55.6), merged into master as `a24cd34`.

M55 made a DIRECT_FAST turn fast. M56 makes the things around it **measurable**:
which models are actually resident, what the server is actually configured with,
what a warmup actually warms, and who gets the CPU when two jobs want it.

---

## 1. The four M55 unknowns, and what M56 measured

| M55 said | M56 measured (this host, Ollama 0.32.1) |
|---|---|
| server `OLLAMA_NUM_PARALLEL` — UNKNOWN | **VERIFIED ABSENT** (server env block read from pid 31212) |
| server `OLLAMA_MAX_LOADED_MODELS` — UNKNOWN | **VERIFIED ABSENT** — the server runs on built-in defaults |
| "qwen3:8b was evicted after nomic-embed-text became resident" | **DISPROVEN**: `DUAL_RESIDENT_OBSERVED` — both stay loaded, neither evicts the other |
| launch mode unknown | **STARTUP_APP** — child of `ollama app.exe` (per-user logon item, not a service) |

The eviction that motivated `OLLAMA_MAX_LOADED_MODELS=2` does not occur here.
`recommend_posture(observed_dual_residency=True)` therefore **drops** that variable:
advising a change that measurement shows is unnecessary is the M55 mistake, and
pinning `2` could only cap a server whose default already allows more.

## 2. Truth categories (M56.1)

`RECOMMENDED` · `JARVIS_PROCESS` · `WINDOWS_USER_ENV` · `WINDOWS_MACHINE_ENV` ·
`SERVER_PROCESS_OBSERVED` · `SERVER_INHERITANCE_VERIFIED` · `SERVER_BEHAVIOR_OBSERVED` ·
`UNKNOWN`

Two rules are enforced in code and locked by tests:

* **A Windows environment value is never inheritance proof.** A running server read
  its environment at creation; a registry value written afterwards cannot reach it.
  With the server's own block unreadable, the answer stays `UNKNOWN` *even when the
  values match exactly*.
* **A loaded-model count is never slot-count proof.** A server with four free slots
  holding one model is indistinguishable, at the API, from a server pinned to one.
  Slot count is not observable; **eviction** is.

Reading the server's own block also verifies **absence** — strictly stronger than
"unknown", and how the table above was settled.

Discovery is one bounded, TTL-cached `psutil` pass. Command lines are **never**
captured (argument *count* only), so no secret can reach a log or the operator UI.

## 3. Operator-gated posture workflow (M56.2)

```
ollama-posture-status     read-only truth report
ollama-posture-plan       recommendation from measured hardware
ollama-posture-dry-run    exact diff, scope, restart requirement — no mutation
ollama-posture-apply      OPERATOR/HITL REQUIRED
ollama-posture-verify     re-read the SERVER PROCESS after an operator restart
ollama-posture-rollback   OPERATOR/HITL REQUIRED
```

* Allowlist of exactly three writable variables; integer bounds and an anchored
  duration grammar; shell metacharacters refused even though the write path is the
  **typed `winreg` API** — no shell, no `setx`, no PowerShell fragment is ever built.
* `apply`/`rollback` need an `OperatorAuthorization` covering the **exact target map**,
  so an approval cannot be replayed onto a different change.
* HKLM is never written. **Nothing is ever restarted.** An apply is inert until the
  operator restarts the server; `verify` may only claim success from the server
  process's own block.
* Durable bounded journal stores **previous values verbatim** → exact rollback. A
  corrupt journal cannot become a write primitive.
* The interactive surface never mints an authorization: typing `apply` reports what a
  real approval would require. No model output can reach an effectful path.

## 4. Residency verification (M56.3)

Bounded 8-step sequence over `/api/ps`: inspect → FAST → inspect → embedding →
inspect → FAST → inspect → compare. States: `UNKNOWN`, `SINGLE_SLOT_OBSERVED`,
`DUAL_RESIDENT_OBSERVED`, `FAST_EVICTED`, `EMBEDDING_EVICTED`, `RESIDENCY_UNSTABLE`,
`VERIFICATION_INCOMPLETE`. A failed step leaves the run **incomplete** rather than
yielding a confident verdict. The embedding vector is discarded — no semantic write.

Live result (2026-07-20): `DUAL_RESIDENT_OBSERVED`, qwen3:8b (5.41 GB) +
nomic-embed-text (376 MB), `size_vram=0` (pure CPU), run 7.8 s,
`reload_cost_ms=-157` (the post-embedding turn was *faster*, not slower).

## 5. Full-path prewarm (M56.4) — and the defect it exposed

One complete native `/api/chat` request over the same transport a real turn uses.
Forbidden by construction: no tools, no RAG, no verifier, no history mutation, no
TTS, no memory write, and **no pollution of the real FAST-turn latency window**.
Once per model activation; refused after `STOPPING`; cancellation-aware.

**Measured defect, then fixed.** The first implementation warmed at `num_ctx=512`
while real FAST turns use `fast_context=2048`. Ollama reloads the runner when
generation parameters change, so the operator's first real turn still paid a full
reload of an already-resident model:

| | first token | load_duration |
|---|---|---|
| prewarm ctx=512 → turn ctx=2048 | **10 485 ms** | **8 723 ms** |
| prewarm ctx=2048 → turn ctx=2048 | **1 235 ms** | **411 ms** |

Warming a configuration no real turn uses is not a warmup. The prewarm now resolves
`num_ctx` from the same setting the live turn uses, and a test locks it.

### Modes (M56.4.1)

`OFF` · `BACKGROUND` (default) · `BEFORE_TEXT_READY` (hard-capped outside the runner,
so even a runner that ignores its own timeout cannot hold the prompt closed).
An unrecognized mode falls back to `BACKGROUND` and never silently upgrades into the
boot-blocking mode.

`TEXT_READY` remains independent of model warmth. `WARMING` split into
`MODEL_LOADING` / `PREWARMING`, each with its own true operator hint; every warming
state still accepts input. `READY` is never claimed because a prewarm *started* —
only a real content token yields it, and a failed prewarm degrades to `WARMING`,
never `UNAVAILABLE`.

## 6. Residency governor (M56.5)

Adds no second model table — selection stays with `core.model_router`. It answers
who gets the CPU:

```
CRITICAL (HITL) > INTERACTIVE (live FAST) > VERIFICATION > SEMANTIC_QUERY
                > BACKGROUND (semantic batches) > PREWARM
```

Heavy inference is serialized (`max_concurrent=1`): a second concurrent decode on 6
cores at 15 W makes **both** turns slower. A live FAST turn preempts *queued*
background embedding work; background work is deferred, never dropped, and **ages
into priority** so it cannot be starved forever. Bounded queue, bounded waits (DEEP
waits inside the requesting turn's budget), cancellation releases the slot on every
exit path, duplicate concurrent cold loads suppressed, and shutdown **fails** waiters
rather than leaving them pending.

Slot borrowing: the governor records *why* the machine was borrowed, never unloads a
model itself, flags restoration only from an **observed** missing FAST, and schedules
one bounded restoration — refused while heavy work is active, refused after
`STOPPING`, never stacked, never claimed until a real token proves it.

## 7. Power profiles (M56.6)

`AC_PERFORMANCE` · `BALANCED` · `BATTERY_SAVER` · `UNKNOWN`. Battery disables the
automatic full prewarm and background DEEP, shrinks caps and shortens keep_alive. A
low charge forces `BATTERY_SAVER` even when plugged. An explicit operator override
always wins; the runtime never upgrades *itself* out of `BATTERY_SAVER`.

This **observes** power and never controls it — a test parses the module AST
(docstrings stripped) to prove no `powercfg` / `SetActiveScheme` / `subprocess` /
`ctypes` call exists.

Live at validation time: `source=BATTERY`, 17–23 %, `BATTERY_SAVER` → the automatic
background prewarm was correctly **skipped by policy**. The prewarm numbers above
come from an explicitly forced diagnostic instance, not from overriding that policy.

## 8. Benchmarks, health and UX (M56.7 / M56.8)

Six scenarios as data (`PROCESS_COLD`, `FAST_WARM`, `POST_EMBEDDING`, `POST_DEEP`,
`POST_CANCEL`, `PREWARM`), three fixed controlled prompts, never production content,
trials clamped to ≤5. A missing measurement returns `None`, never `0`.

Runtime health gains **one advisory subsystem** (`residency`) extending the single
existing surface — rank 0, so an evicted model or a battery-disabled prewarm never
degrades the overall verdict. Boot prints one line; the full panel is
operator-requested (`/residency`) and prints `settings_verified=false` unless the
server process's own environment was genuinely read.

## 9. Measured performance (live, observe-only)

| Measurement | Value |
|---|---|
| native probe (server 0.32.1, think:false supported) | 2 687 ms |
| prewarm, full path, ctx=2048 | first token 1 516–2 344 ms, total 1 985–2 828 ms |
| warm FAST first token | **1 203–1 235 ms** |
| warm FAST load_duration | 409–428 ms |
| throughput | 5.2–6.4 tok/s |
| residency sequence (FAST → embed → FAST) | 7.8 s, no eviction |
| post-cancellation next turn | succeeded, governor released the slot |
| environment mutated | **none** (before == after, both scopes) |
| server restarted | **no** (pid 31212 unchanged, same start time) |

## 10. Commands the operator MAY run later (not executed here)

Applying posture needs the **server's** environment plus a restart the operator
performs. On this host the server is a `STARTUP_APP` (child of `ollama app.exe`), so
a user-scope change is picked up only after the tray app is fully restarted.

```powershell
# Preview first — read-only, mutates nothing:
#   ollama-posture-dry-run

# Then, only if the operator decides to (JARVIS will not do this):
[Environment]::SetEnvironmentVariable('OLLAMA_NUM_PARALLEL','1','User')
[Environment]::SetEnvironmentVariable('OLLAMA_KEEP_ALIVE','30m','User')
# Restart the Ollama tray app manually, then:
#   ollama-posture-verify
```

`OLLAMA_MAX_LOADED_MODELS` is deliberately **not** in that list: dual residency was
observed on the defaults, so setting it would cap a server that already does the
right thing.

## 11. Remaining limitations

1. Dual residency is proven for **this** server version and model pair; a different
   model set (DEEP 14B + FAST 8B ≈ 14 GB) may still evict. The verifier is the way to
   re-check, not an assumption.
2. `SERVER_BEHAVIOR_OBSERVED` is not wired as an automatic posture inference — it is
   reported, never converted into a configuration claim.
3. A fully cold qwen3:8b activation still costs seconds; the prewarm moves that cost
   off the operator's first turn but cannot remove it.
4. Battery policy means an unplugged boot has a cold first turn by default. That is
   intentional and operator-overridable.
5. `STARTUP_APP` vs `MANUAL` cannot be distinguished for an `explorer.exe` parent; it
   reports `MANUAL` rather than guessing.
