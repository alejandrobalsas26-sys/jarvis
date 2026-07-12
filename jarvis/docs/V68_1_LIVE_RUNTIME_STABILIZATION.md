# JARVIS V68.1 — Live Runtime Stabilization, Truthful Readiness & Tool Recovery

V68.1 is **not** a new architecture. It fixes integration failures that a real interactive
run exposed but the existing test suite did not catch. Every fix **extends** the V66/V67/V68
spine and preserves the existing guarded control plane
(ToolExecutor + Authority + Scope + Risk + HITL/NATO-OTP + Audit + verification + telemetry
provenance). Nothing here weakens a security control.

Run everything from the `jarvis/` directory.

---

## Why the live run failed where tests passed

The suite validated each subsystem in isolation with mocks. The live failures were **seams**
between subsystems on the real host (Windows 11, Ryzen 5 7430U, CPU Ollama,
`OLLAMA_MAX_LOADED_MODELS=1`, often on battery):

- A dependency **import-time** fault (`torch`/`transformers`) that unit tests never triggered
  because they mock or skip the vector backend.
- Two subsystems (Guardian, self-test) probing the same service with **different timeouts**.
- Boot narration reading from **hardcoded strings** instead of measured state.
- A verifier deadline that assumed a **warm** model on a host that cold-swaps models.

---

## M45 — Knowledge tool reliability

**Root cause.** `torch 2.4.1+cpu` + `transformers 5.8.1` are incompatible: at
`transformers/integrations/moe.py`, `torch.library.custom_op` calls `infer_schema` on a
grouped-GEMM fallback whose stringized `'torch.Tensor'` annotations torch 2.4.1 cannot
resolve — raising `ValueError: infer_schema(func): Parameter input has unsupported type
torch.Tensor` at `import sentence_transformers`, i.e. inside `KnowledgeVault.__init__`. The
raw text leaked out of `query_knowledge` into the conversation.

**Fix (`core/knowledge.py`).** Construction no longer imports heavy deps; backend init is
deferred to a guarded `_ensure_backend()` that classifies faults
(`dependency_incompatibility` / `dependency_missing` / `backend_init_failed`) behind a short,
internal-free message. A new structured `query()` returns `ok` / `empty` / `unavailable` /
`error`; `query_knowledge()` is a thin string wrapper. The public tool schema stays plain
JSON (`query: string`, optional `n_results`). The Knowledge Vault now **degrades honestly**:

| Condition | Result |
|---|---|
| empty vault | useful "vault empty" message (not an error) |
| dependency unavailable | structured `unavailable` (no stack trace) |
| embedding error | structured `error` (no torch internals) |
| success | bounded fragments + source references |

We did **not** blindly change dependency versions — the incompatibility is reproduced and the
package boundary proven, but a stable adapter is preferred so the tool degrades honestly
regardless of the backend's state.

---

## M46 — Tool failure recovery & context isolation

After `query_knowledge` failed, the model switched tool families and invented an unrelated
Packet Tracer / XML task — context contamination.

**Fix (`core/tool_result.py`, `tools/executor.py`, `core/llm.py`).** A typed `ToolFailure`
envelope (`status`, `tool`, `error_class`, `safe_message`, `retryable`, `retry_after`,
`fallback_allowed`, `evidence_refs`). The executor maps any tool exception through
`classify_exception()` to a sanitized envelope (never `str(e)`), and `query_knowledge` emits a
typed failure. In the LLM loop:

- A per-turn failure ledger bounds retries — **0** for structural/config/schema errors, **1**
  only for explicitly retryable (transient) ones.
- `recovery_guidance()` is appended **outside** the untrusted-data envelope (it is first-party
  policy) and pins the model to the failed tool, forbids switching tool families, and states
  the only valid next moves (retry once if transient, else answer without the tool or say it
  is unavailable).

---

## M47 — Cyber intent, authorization & scope gate

The ambiguous "hack a vending machine over Wi-Fi/Bluetooth/SDR" request (no lab, owned device,
CTF, or scope) should have been gated before any knowledge search or operational planning.

**Fix (`core/cyber_intent.py`, `core/llm.py`).** A deterministic classifier composes the
offensive-operational *shape* of the request, the operator-controlled `AuthorityState`, and
in-prompt lab/CTF framing into a per-turn decision — not a keyword block:

- **Ambiguous real-world target, no authorization** → hard-block **all** tool execution
  (typed `authorization_required` failure) and inject a directive requiring: state
  authorization is missing, no exploit steps, offer safe alternatives (threat model, defensive
  checklist, legal isolated-lab design, firmware/radio inventory, detection strategy).
- **Authorized lab / CTF** (named scope or scoped authority) → request a **defensive
  assessment plan**; every effectful action still passes the existing authority/scope/HITL
  gate. In-prompt framing never widens execution authority.
- An operational imperative ("how to hack the X") stays offensive; a conceptual noun ("what is
  a replay attack") does not. "For education" never establishes authorization.

---

## M48 — Truthful startup, self-test & boot narration

Guardian found both Ollama models while self-test reported "Ollama LLM Server FAILED"; boot
narration claimed Moondream loaded, ETW/Sysmon armed, Telegram established, and "all systems
nominal" — none true.

**Fix.**
- `core/boot_state.py`: one read-only truthful snapshot from the self-test report + explicit
  runtime flags (vision model, ETW, Sysmon, Telegram, PostgreSQL). Consumed by logs, spoken
  narration, AURA, self-test summary, and field readiness. "All systems nominal" is emitted
  **only** when nothing failed or degraded.
- `core/self_test.py`: Guardian (5 s httpx) and self-test (was 3 s aiohttp) now share **one
  tolerant probe** (8 s + 1 retry, same normalized host, cached per run), so they cannot
  disagree under CPU model-load latency.
- `core/boot_sequence.py`: narrates from the snapshot; the hardcoded Moondream/ETW/Sysmon/
  Telegram/"all nominal" script is gone.
- `core/personality.py`: wake greetings no longer assert an unverified status.
- `core/grc_auditor.py`: fixed Loguru `%s`/`%d` placeholders that printed literally.
- `main.py`: removed the duplicate "agent_planner attached" log; builds/logs the snapshot.
- `tools/executor.py`: `get_datetime` reads the host clock (`astimezone()`) and returns
  tz/offset/ISO/`source=host_system_clock`.

---

## M49 — CPU-aware verification latency

The verifier waited minutes then failed closed. A single 25 s deadline was both too long to
abort a cold model swap promptly and too short to succeed on one (verifying a `qwen3:14b`
draft with the `qwen3:8b` verifier forces an unload/reload under
`OLLAMA_MAX_LOADED_MODELS=1`).

**Fix (`core/verification.py`, `core/llm.py`, `core/runtime_health.py`).**
- `deterministic_precheck()` runs model-free first: a failed-tool/unauthorized fallback is
  audited without a verifier pass (never rubber-stamped); a security-sensitive turn with a
  failed tool is flagged promptly instead of blocking on a cold swap.
- `resource_aware_timeout()` bounds the wait by warm/cold and halves it on battery, under a
  hard ceiling — so a turn can never block for minutes. Fail-closed posture is preserved.
- `verify_answer()` is cancellable via the operator interrupt event, reuses the `qwen3:8b`
  VERIFIER role (never loads `qwen3:14b` for a lightweight check), and records latency.
- A `verifier` subsystem in runtime-health surfaces avg/max/last latency and timeout count.

---

## M50 — Network surface & log hygiene

The canary honeypot matrix bound `0.0.0.0` (exposing decoys on any network); the security
auditor re-warned the identical "UDP/41641 owned by tailscaled.exe" every scan cycle.

**Fix.**
- `core/canary.py`: deception services default to `127.0.0.1`. External exposure requires an
  explicit opt-in (`JARVIS_CANARY_EXPOSE`, optional `JARVIS_CANARY_BIND`) and is logged. The
  real bind address is read from the socket (never assumed from `:port`), the availability
  probe uses the same host, and collisions are logged.
- `core/security_auditor.py`: identical `(proto, port, process)` findings update a
  count/last_seen and re-surface at most hourly. Operator classification
  (`EXPECTED` / `KNOWN_SERVICE` / `INVESTIGATE` / `SUPPRESS_UNTIL` / `BLOCKED`) is explicit and
  never inferred from a process name; `EXPECTED`/`KNOWN_SERVICE` also suppress the auto-block,
  `BLOCKED` still blocks.

---

## M51 — Live end-to-end validation

`tests/test_live_runtime_v681.py` walks the exact failure chain deterministically with a
synthetic isolated-lab identity (never a real target): role-model/probe agreement, truthful
boot narration, host-grounded time, `query_knowledge` through the executor (injected and real
backend), no Packet-Tracer contamination, the ambiguous-cyber block, authorized-lab scope
enforcement, bounded verifier latency, finding dedup, localhost canary default, and an intact
graceful-shutdown registry.

---

## Operator flags introduced

| Env var | Default | Effect |
|---|---|---|
| `JARVIS_CANARY_EXPOSE` | off | allow canaries to bind off-localhost (authorized lab only) |
| `JARVIS_CANARY_BIND` | `0.0.0.0` when exposed | explicit canary bind address |

Existing flags unchanged: `JARVIS_ETW_ENABLE`, `SYSMON_LOG_PATH`, `JARVIS_TELEGRAM_TOKEN`,
`JARVIS_TELEGRAM_CHAT_ID`, `JARVIS_TRUSTED_LAB`, `DATABASE_URL`, `JARVIS_GRC_ENABLED`.

---

## Known limitation

The Loguru printf-placeholder anti-pattern (`logger.info("… %s", x)`, which prints the literal
`%s` and drops the arg) was fixed in `core/grc_auditor.py` but exists in ~230 other call sites
across the repo. A repo-wide sweep is a candidate for V69 (mechanical, but outside the
focused-audit scope of this release).
