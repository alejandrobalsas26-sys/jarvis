# JARVIS V69 M55 — Native Ollama Fast Path & Non-Reasoning Interactive Inference

**Status:** COMPLETE on branch `jarvis-v69-m55-native-fast-path` (NOT merged).
**Baseline:** master `f9d96e7` (V69 M54.1) — 2009 passed / 18 skipped / 0 failures.
**After M55:** 2068 passed / 18 skipped / 0 failures (+59 M55 tests). ruff + compileall clean.
**Host:** Windows 11, AMD Ryzen 5 7430U (6c/12t, 15 W), 64 GB DDR4, CPU-only Ollama 0.32.0.

## 1. Problem

The FAST role runs `qwen3:8b`, a *reasoning* model. Through the OpenAI-compatible
`/v1` shim there is no way to disable reasoning: the shim drops the `<think>` block
from the content but still pays the full hidden-reasoning latency. M54.1 measured
110–173 s to first content token on reasoning-heavy turns — no interactive budget
can answer a simple question in time. M54.1 fixed *reliability* (clean deadline +
cancellation); M55 fixes *responsiveness*.

## 2. Prior transport (before M55)

`core/llm.py::LLM.chat_stream` → `AsyncOpenAI(base_url=".../v1")` → SSE. One shared
client, per-request `with_options(timeout=…)`, bounded first-token/idle/total via
`_iter_stream_bounded`. `think` is not expressible; qwen3 reasons on every turn.

## 3. Native transport (M55.1)

New `core/ollama_native.py` — a transport-neutral async client for `POST /api/chat`
on the already-installed `httpx` (the `ollama` python package is **not** installed
and is **not** added; no second inference framework). It:

- serializes `think` explicitly — `None` omits the field, `True`/`False` set it;
- streams NDJSON and normalizes each line to a `ChatChunk` with **safe fields only**
  (`content`, `done`, `done_reason`, `model`, `created_at`, `*_count`, `*_duration`,
  and a boolean `thinking_present`); the reasoning **text is discarded at parse time**
  and never stored or surfaced;
- enforces connect/first-token/idle/total bounds via the existing `TurnBudget` /
  `StageTimeouts` contract, cancellable via a `CancellationToken` bridged from
  `cancel_bus`, and always closes the live HTTP response (no late chunk / orphan).

Public surface: `async def chat_stream(*, model, messages, think, max_tokens,
temperature, budget, timeouts, cancellation, ctx, keep_alive, client, base_url) ->
AsyncIterator[ChatChunk]`.

## 4. Capability probe (M55.2)

`probe_native()` runs a tiny (`num_predict=8`) native `think=false` stream and proves
it is **structurally valid with reasoning omitted** — never `NATIVE_READY` on HTTP 200
alone. States: `UNKNOWN / PROBING / NATIVE_READY / NATIVE_DEGRADED / OPENAI_FALLBACK /
UNAVAILABLE`. Bounded, cached (`get_native_capability`), refreshable. Run once at boot
in `main._fast_warmup` (doubles as the warmup — the probe loads qwen3:8b think=false).

**Live probe result:** `state=NATIVE_READY version=0.32.0 think_false_supported=True`.

## 5. think=false evidence (M55.4/M55.6)

Controlled bounded trials, `qwen3:8b`, target host:

| Path | think | first content token | thinking field | tok/s | note |
|---|---|---|---|---|---|
| `/v1` (current) | (n/a) | **29.3 s** warm | none surfaced, full latency | — | shim hides `<think>`, still slow |
| native `/api/chat` | `true` | **18.7 s** warm | **yes (339 chars)** | 5.3 | reasoning on |
| native `/api/chat` | `false` | **1.3 s** warm / **15.5 s** cold (load 12.9 s) | **none (0 chars)** | 5–6 | ✅ |
| native `/api/chat` | `false` + `num_predict` | first token immediate; total bounded | none | 5.2 | ✅ bound total |

`think=false` is honored at the wire (thinking field present only with `think=true`).
`/no_think` prompt phrasing and OpenAI `think:false` did **not** work (M54.1) — this does.

## 6. Transport policy (M55.3)

`core/fast_path.py::decide_fast_route` (pure). Native no-think serves ONLY
`reason_code=DIRECT_FAST` + FAST role + not security-sensitive + verify policy in
{`SKIP_LLM_VERIFIER`, `DETERMINISTIC_CHECKS_ONLY`}. Everything else stays on `/v1`:
DEEP/coder reasoning, cyber-sensitive, effectful, private-document (vault), verifier,
tool orchestration. Transparent reason codes: `NATIVE_FAST_NO_THINK`,
`OPENAI_TOOL_CHAT`, `DEEP_REASONING`, `NATIVE_UNAVAILABLE_FALLBACK`, `OPENAI_FORCED`.
`auto` uses native when the probe proved support (optimistic on UNKNOWN); a transport
error before first content falls back to `/v1` honestly.

## 7. Bounded generation (M55.5)

The native fast path sends `num_predict = fast_max_tokens` (default 256), a small
`num_ctx` (`fast_context`, default 2048, further shrunk by `_adaptive_ctx`), no tools,
no verifier, no RAG, and a **lean system prompt** (identity + host-clock + language
directive — not the 200-line tool manual), which prefills far faster on CPU and keeps
answers concise. At ~5 tok/s, 256 tokens completes inside the 60 s SKIP/DETERMINISTIC
budget.

## 8. Configuration seam (M55.7)

`core/config.py` (single source of truth), `JARVIS_FAST_*` env aliases, clamped:

| Setting | Env | Default | Bounds |
|---|---|---|---|
| `fast_transport` | `JARVIS_FAST_TRANSPORT` | `auto` | auto/native/openai |
| `fast_think` | `JARVIS_FAST_THINK` | `off` | off/on/omit → False/True/None |
| `fast_max_tokens` | `JARVIS_FAST_MAX_TOKENS` | `256` | 32–2048 |
| `fast_context` | `JARVIS_FAST_CONTEXT` | `2048` | 512–8192 |
| `fast_keep_alive` | `JARVIS_FAST_KEEP_ALIVE` | `10m` | — |
| `fast_model` | `JARVIS_FAST_MODEL` | `""` | optional distinct non-reasoning override (native path only) |

`fast_model` empty ⇒ resolved FAST-role model (`JARVIS_MODEL_FAST`). No fork, no
silent model change, no automatic `ollama pull`.

## 9. Live acceptance (M55.15)

Real `LLM.chat_stream`, target host, model warm after boot probe:

| Prompt | class → transport | first token | total | reasoning leak | language |
|---|---|---|---|---|---|
| `¿qué hora es?` | CURRENT_TIME → **deterministic bypass** | 0.015 s (no model) | 0.015 s | — | ES ✅ |
| `hola` | DIRECT_FAST → native | 1.3–23 s* | — | none | ES ✅ |
| `como saco la raiz cubica de algo` | DIRECT_FAST → native | **9.95 s** | **27.47 s** | none | ES ✅ |
| `explícame POO brevemente` | DIRECT_FAST → native | 15.0 s | 34.3 s | none | ES ✅ |

\* `hola` cold-reloaded because the embedding model (`nomic-embed-text`) was resident
and evicted `qwen3:8b` under the one-model server posture (see §11). Warm native
first-token is ~1.3 s (§5).

**The exact previously-failing turn** (`como saco la raiz cubica de algo`) went from
hitting the 60 s deadline (cancelled, no answer) to **first token 9.95 s, complete in
27.5 s, no reasoning, Spanish** — materially faster than the 110–173 s reasoning path.

**Cancellation + next-turn (M55.9/M55.15):** cancelling a fast turn mid-stream returns
control immediately (cooperative token check); the interrupted partial answer is paired
with the user turn, and the next `hola` correctly answers a greeting
(`POLLUTED_BY_PREVIOUS_QUESTION = False`). Client cancellation guarantees no late chunk,
prompt restored, resources released. Server-side continuation after client disconnect
is **suspected but not asserted** — it is recorded as a proxy (next-turn start), never
as proven fact.

## 10. Deterministic bypasses (M55.11)

`core/deterministic_bypass.py` answers time/date/lifecycle-state/active-FAST-model/
vault-empty from trusted runtime data in the active language — **no model call**
(`¿qué hora es?` → 0.015 s). Returns `None` (falls through) when a trusted source is
unavailable; never invents a value.

## 11. Actual Ollama environment truth (M55.8)

`core/ollama_env.py` refuses to conflate: `configured_by_jarvis` (advisory hardware
recommendation) / `process_environment` (this process's `OLLAMA_*`) / `server_observed`
(cached `/api/version` + `/api/ps`) / `unknown` (the server's real
`OLLAMA_NUM_PARALLEL`/`MAX_LOADED_MODELS` — the API does not expose them).

**Observed on this host:** only `OLLAMA_HOST=127.0.0.1` is set. JARVIS *logs*
`OLLAMA_MAX_LOADED_MODELS=1` (via `configure_ollama_for_hardware`) but **never sets it**,
and the Ollama server (PID 34068) is a separate process. Verdict:
`max_loaded_models = not-applied`, `settings_verified = false`. The live run confirmed
model eviction (nomic resident, evicting qwen3 between probe and first turn), which is
exactly the one-model behavior this surface warns about.

**Operator guidance (not executed):** to pin models in RAM, set
`OLLAMA_MAX_LOADED_MODELS=2` (so `nomic-embed-text` + `qwen3:8b` coexist and FAST is not
cold-reloaded after each embedding) and `OLLAMA_NUM_PARALLEL=1` in the **Ollama server's**
environment, then restart the Ollama service. JARVIS never restarts or kills the server.

## 12. FastReadiness & runtime health (M55.10/M55.13)

`FastReadiness` gained (additively) transport, `think_supported`, native state, and
bounded first-token/total/throughput moving stats + timeout/cancel/fallback counters
(`fast_inference_snapshot`). Runtime health gained two additive subsystems:
`fast_inference` (DORMANT until first turn; DEGRADED only on a sustained timeout rate)
and `ollama_env` (advisory OPTIONAL — never shifts overall health). No new module; no
raw prompts/generated text in metrics.

## 13. Model recommendation

**Keep `qwen3:8b` as FAST.** Native `/api/chat` + `think=false` + bounded `num_predict`
meets the acceptance bar (warm first token ~1.3 s; the cube-root turn completes in
~27 s). **No model download is required or was performed.**

Optional, operator-approved only (NOT executed):
- Prefer keeping models resident: `OLLAMA_MAX_LOADED_MODELS=2` on the server (removes the
  nomic↔qwen3 cold-swap that dominates the current cold first-token time).
- For even lower CPU cold-start, a smaller genuinely non-reasoning model could back the
  FAST role via `JARVIS_FAST_MODEL` — e.g. `ollama pull llama3.2:3b` or
  `ollama pull qwen2.5:3b-instruct`. Do **not** run without operator approval; qwen3:8b
  already satisfies M55.

## 14. Files

New: `core/ollama_native.py`, `core/fast_path.py`, `core/deterministic_bypass.py`,
`core/ollama_env.py`; tests `test_native_transport_v69_m55.py`,
`test_fast_route_v69_m55.py`, `test_deterministic_bypass_v69_m55.py`,
`test_fast_readiness_env_v69_m55.py`.
Extended: `core/config.py`, `core/llm.py`, `core/fast_readiness.py`,
`core/runtime_health.py`, `core/knowledge.py`, `main.py`; tests
`test_runtime_health_v67.py`, `test_text_ready_v691.py`.
