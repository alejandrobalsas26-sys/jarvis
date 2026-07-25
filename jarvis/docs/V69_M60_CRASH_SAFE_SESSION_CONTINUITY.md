# JARVIS V69 M60 — Crash-Safe Session Continuity & Windows Runtime Recovery

**Branch:** `jarvis-v69-m60-crash-safe-session-continuity`
**Base:** `384c147` (V69 M59 merged into `master`)
**Status:** complete and **merged to `master`** as `df35289` (merge status corrected in V69
M61.1 — this line was stale)

---

## 1. What was actually broken

M59 left the runtime fast, warm and measurable. It left it **not durable**.

`core/session_manager.py` was the only conversation persistence, and it had three
faults that a power cut turns into data loss or — worse — into a lie:

| # | Fault | Consequence |
|---|---|---|
| 1 | `path.write_text(...)` truncates the file in place | a crash between truncate and flush leaves a zero-length or half-written JSON; the whole session is gone |
| 2 | history written unconditionally after each completed turn | a turn interrupted mid-stream was absent or indistinguishable from a completed one; on resume a partial answer would be presented as fact |
| 3 | no notion of a RUN | nothing could tell a clean exit from a crash |

Two further gaps:

* `core/task_watchdog.py` restarts with `min(30·2ⁿ, 300)` backoff and a
  monotonic `restart_count` that never decays — no restart **window**, no
  circuit breaker, no criticality, no notion of an operation that must never
  be restarted;
* `core/optional_service.py` has the opposite gap: a truthful lifecycle and a
  bounded, cancellation-safe `stop()` — but no restart at all.

---

## 2. Evidence audit (focused; pre-coding)

| Component | Durable before | Volatile before | Crash risk | Seam used |
|---|---|---|---|---|
| `operational_store.py` | SQLite WAL, versioned, idempotent `put`, hash dedup, retention, corrupt-row isolation | in-memory fallback | low | **reused as the journal engine** |
| `session_manager.py` | whole-history JSON + `redact_secrets` | no turn state, no run marker | non-atomic write; partial answer saved as complete | superseded, API kept |
| `session_journal.py` (v44) | markdown on clean shutdown only | RAM event list | lost on crash | untouched (different concern) |
| `response_runtime.py` | none | `TurnHandle` / 7 `TurnState`s | turn state lost | `begin_turn` / `end_turn` vocabulary |
| `lifecycle.py` + `shutdown_manager.py` | one `system_shutdown` audit line, never read at boot | full FSM | no clean/unclean distinction across processes | `register_shutdown_callback` |
| `task_watchdog.py` | none | backoff only | restart storm | `RestartPolicy` |
| `optional_service.py` | none | truthful FSM | no restart | `ServiceState` |
| `runtime_health.py` | none | ONE collector | — | one new subsystem |
| `runtime_commands.py` | none | exact-match allowlist | — | pattern copied |
| `tools/executor.py` | `TacticAuditLogger` jsonl | in-flight tool state in RAM | effectful interrupt → unknown outcome | audit reference |
| paths | `_JARVIS_DIR/data` | `logs/` CWD-relative | breaks off-CWD launch | `core/managed_paths.py` |

---

## 3. M59 → M60

M59 answered *"is it fast, and can I prove it?"*
M60 answers *"if the power goes out mid-answer, what does it say when it comes back?"*

M52–M59 are preserved unchanged. M60 adds **no** new agent, model router, memory
architecture or supervisor framework, and changes **no** Windows startup entry,
service, scheduled task, registry key, environment variable or Ollama setting.

---

## 4. Session-journal architecture (M60.1)

`core/session_continuity.py` instantiates the proven
`core.operational_store.OperationalStore` against its **own** managed file
(`data/sessions/session_continuity.db`) — same engine, separate lifecycle, so session
state can be backed up, exported and pruned independently of the operational domains.

Four entities, all schema-versioned:

* **SESSION** — id, timestamps, language, response profile, authority / scope /
  security-policy / prompt / tool-schema fingerprints, state, persistence mode;
* **TURN** — sequence, timestamps, role, terminal state, response contract,
  continuation state, tool references, `content_chars`, content hash, and —
  subject to the mode — the visible content after redaction;
* **RUN** — process start, runtime version, Git commit, **clean-shutdown marker**,
  lifecycle terminal state, last checkpoint, recovery result, PID + boot token;
* **TOOL_OP** — tool name, effectful flag, timestamps, outcome, audit reference,
  redacted summary, and an **argument digest** — a hash, never the arguments.

Never persisted: readiness, model warmth, loaded-model claims, live connections,
active locks, active tasks, queue ownership, pending TTS, raw model streams, hidden
reasoning, OTPs, credentials, raw tool arguments. All of those are **re-measured**.

---

## 5. Persistence modes (M60.1.1)

| Mode | Stores |
|---|---|
| `OFF` | nothing but the crash marker |
| `METADATA_ONLY` | ids, timestamps, terminal states, fingerprints, char **counts** |
| `LOCAL_REDACTED` *(default)* | bounded visible content after deterministic redaction |
| `LOCAL_FULL_EXPLICIT` | local visible content; still excludes secrets, OTPs, hidden reasoning, raw tool payloads |

An **unknown** configured value falls back to the default — never to the most
permissive mode. The active mode is reported truthfully in health and in
`/session-status`. `LOCAL_FULL_EXPLICIT` widens what *conversation* is kept, never
what *secrets* are kept.

---

## 6. Redaction policy (`core/redaction_policy.py`)

One deterministic scanner, applied before every M60 disk write:

* **hidden reasoning** — `<think>`, `<thinking>`, `<reasoning>`, `<scratchpad>`,
  `<analysis>`; closed blocks *and* the **unterminated** opener a crash leaves
  behind, cut to end-of-string (the safe direction);
* **NATO OTP** — only inside an authorization context, so "hotel" in ordinary
  Spanish is not destroyed;
* **numeric OTP** — 4–8 digits in an OTP/2FA/code context;
* **credentials** — reuses `core.memory_router`'s proven patterns, so there is one
  credential vocabulary in the codebase, not two;
* **home paths** → `<HOME>`;
* **length bound**, with the truncation reported.

Order matters: reasoning is removed **first**, so a secret quoted only inside a
`<think>` block disappears with the block rather than surviving as a marker.

`scan_for_leaks` / `scan_structure` are the finalize gate. A hit **refuses** the
artifact instead of patching it — a quietly repaired file would hide the bug.

---

## 7. Transaction and checkpoint behavior (M60.1.2)

* SQLite autocommit → **each statement is its own transaction**; a crash mid-write
  leaves the *previous* row intact, never a half-written one;
* idempotent + content-hash deduplicated (re-putting an unchanged record is a no-op);
* `finalize_turn_async` runs in a bounded `to_thread` with an explicit deadline — a
  write that misses it is a reported **failure**, not an indefinite await;
* every write returns a `WriteResult`; a failure increments a counter, degrades
  `journal_state`, and **never** reports success;
* file artifacts (exports, bundles, backups) use temp + atomic rename;
* a read that **failed** is counted separately from a read that found nothing.

**Observed on the target host** (300 writes, 400-char turns, `perf_counter`):

```
journal write   median 1.21 ms   p95 2.21 ms   p99 2.92 ms   max 3.36 ms
session restore 5.0 ms (12 turns)      reconcile 5.2 ms      checkpoint 1.37 ms
```

Preferred bound is 10 ms, warning threshold 25 ms — both met.
The journal's clock is `time.perf_counter`, **not** `time.monotonic`: on Windows
monotonic ticks at ~15.6 ms, so a sub-millisecond write measured as either 0.0 or
16.0 and both thresholds would have been meaningless noise.

---

## 8. Clean / unclean detection (M60.2)

A run is **clean** only when its RUN record carries an explicit `clean_shutdown`
marker written by the shutdown driver **and** left no unfinished work.

* explicit non-clean end → `UNCLEAN_CRASH`
* no end marker, but a checkpoint landed → `UNCLEAN_CRASH`
* no end marker, no checkpoint → `UNCLEAN_POWER_LOSS`
* clean marker **and** unfinished work → `UNKNOWN` (the two facts disagree;
  recovery reports the disagreement instead of picking the convenient one)

**PID existence is never proof.** A Windows PID is reused within minutes. The PID is
recorded and reported as advisory context only; a stale run marker whose PID is
genuinely alive is still reconciled as unclean (asserted in tests).

---

## 9. Interrupted-turn reconciliation

`FAILED_BEFORE_CONTENT`, `PARTIAL_VISIBLE_RESPONSE`, `INTERRUPTED_BY_CRASH`,
`INTERRUPTED_BY_POWER_LOSS`, `UNKNOWN_TERMINATION`, `REVIEW_REQUIRED`,
`UNRESOLVED_USER_MESSAGE`.

* only the characters the operator **saw** are restorable — persisted text is trimmed
  to `content_chars`, so a stream that persisted more than it rendered can never
  surface the extra text later;
* a user message with no finalized assistant response stays **unresolved**; recovery
  never invents a response;
* the reconciled state is **written back**, so the truth survives a second crash;
* the finalize stamp is derived from the record's own `started_at`, not from `now` —
  stamping recovery time would claim the turn ran until this boot;
* reconciliation is idempotent: a second pass reconciles nothing.

`reconcile()` has **no execution path**. `actions_replayed` is structurally 0 and
asserted, and the module is checked for the absence of `chat_stream`, `ollama`,
`AsyncOpenAI`, `httpx`, `generate(`, `embed(`.

---

## 10. Tool-outcome reconciliation (M60.2.1)

`READ_ONLY_COMPLETED`, `READ_ONLY_INTERRUPTED`, `EFFECTFUL_COMPLETED`,
**`EFFECTFUL_UNKNOWN_OUTCOME`**, `DENIED`, `FAILED`, `REVIEW_REQUIRED`.

An interrupted **effectful** operation may have completed, partially completed or
never started. It is never rerun, never described as "probably fine". The operator
gets the intended action, the audit reference, the uncertainty and
*"verify the target state manually before repeating this action"*.

No HITL grant, OTP or approval field is stored, so none can be carried forward.
Retention refuses to delete an unresolved effectful op even when it is old.

---

## 11. Conversation restoration (M60.3)

Restored: last stable session metadata, recent **finalized** turns, digest, operator
preferences, unresolved-question markers, active language.

Recomputed: prompt manifest, tool-schema fingerprint, authority/scope,
security-policy version, language directive, model readiness, runtime health.

* newest-first selection under a turn count **and** a character budget;
* an `ACTIVE`/never-finalized turn is **never** restored — its state is unknown;
* a partial answer is included but **labelled** as interrupted;
* `FORBIDDEN_RESTORE_KEYS` names what may never come back (authority, scope,
  HITL/OTP, warmth, readiness, locks, queues, pending TTS) so the guarantee is
  testable rather than aspirational.

**Continuation invalidation:** a changed authority / scope / security-policy /
prompt / tool-schema fingerprint sets `continuation_valid = False`, names the reason,
and **keeps** the human-readable history — it is still a true record of what was
said; only the machine state that depended on the old fingerprint is dropped.

---

## 12. Session commands (M60.3.1)

`/session-status` `/sessions` `/session-new` `/session-resume-last`
`/session-checkpoint` `/session-forget-current` `/session-forget-confirm`
`/session-export-redacted` `/recovery-status` (+ exact Spanish aliases).

Exact-match allowlist on the proven `runtime_commands` pattern. **No argument
parsing anywhere**, so no path, session id or destination can arrive from free text:
`/session-export-redacted ../../etc/passwd` is an ordinary conversational turn.

* export writes to a **managed** directory under an application-generated name
  (derived from the session-id hash) via atomic rename, after a secret scan that
  refuses on a hit;
* `/session-forget-current` is a deterministic two-step confirmation whose token is
  minted locally, is **single-use**, **session-scoped**, and **expires** on an
  intervening turn. Model output can never reach the destructive branch;
* `/session-resume-last` restores conversation text only — the restore path has no
  execution seam;
* sessions are listed by **hashed** id.

Deployment: `/deployment-status`, `/deployment-plan terminal|startup|scheduler|service`.

---

## 13. Retention (M60.3.2)

Bounded: sessions (20), turns/session (200), visible chars/turn (4000), journal rows
(5000), tool ops (500), export size (512 KB), bundle size (256 KB), backup (64 MB),
digest (1200), recovery warnings (20).

* the **active** session is excluded from pruning by identity — a long uptime can
  never silently delete the conversation in progress;
* an unresolved effectful tool op survives retention;
* `/session-forget-current` removes turns and leaves an audit **tombstone**;
* pruning reports **counts**, never deleted content;
* proven: repeated crash cycles do not grow the journal.

---

## 14. Recovery supervisor (M60.4)

A **policy layer** between `task_watchdog` and `optional_service` — not a third
supervisor. It owns no event loop and creates no task; every decision is a pure
function of recorded counters.

Classes: `CRITICAL` / `OPTIONAL` / `BACKGROUND` / `ONE_SHOT`, each with its own
`max_restarts`, window, backoff, cooldown, startup/shutdown timeouts and
notification threshold.

**Six refusals**, evaluated *before* any counter so a policy bug cannot turn one into
a permission:

1. `REFUSED_STOPPING` — never after STOPPING;
2. `REFUSED_DUPLICATE` — never a second instance;
3. `REFUSED_BACKOFF` — never inside the exponential backoff;
4. `REFUSED_CIRCUIT_OPEN` — the breaker opens after *N* restarts in the window and
   allows exactly **one** probe restart after the cooldown (`HALF_OPEN`);
5. `REFUSED_EFFECTFUL` — active turn, tool execution, HITL prompt, effectful runbook
   step, semantic migration, backup restore, deployment apply;
6. no service is `READY` without a **passing health probe** — a restart revokes
   readiness until it is re-proven.

A restart bumps `generation`: the contract that the service receives a **fresh**
task/queue context rather than one from a closed event loop (the M54.1 QueueFull
storm). Restart history, notifications and a global restart budget are all bounded.

---

## 15. Windows deployment planner (M60.5)

Read-only. It **cannot** mutate the host: it imports no mutation API, defines no
apply path, and every plan reports `dry_run=yes`, `apply_supported=no`,
`host_changes=0` (asserted structurally and behaviourally).

Targets: `INTERACTIVE_TERMINAL`, `STARTUP_APPLICATION`, `TASK_SCHEDULER_PLAN`,
`WINDOWS_SERVICE_PLAN`. Each states the exact executable, arguments, working
directory, data/log paths, required privileges, risks and rollback — including that a
Windows service loses the console, HITL, barge-in and audio to session 0 isolation,
and that LocalSystem would run JARVIS with far more privilege than it needs.

Command text comes from **fixed templates** with inspected-path substitutions only.
Nothing is model-generated and nothing is executed. The Ollama check is a bounded TCP
connect that sends no bytes and reads no configuration.

Panels keep real paths in the operator's own console — a manual step they cannot copy
is useless — and `snapshot(redact_home=True)` / `render_plan(redact_home=True)` are
used for anything that leaves the host.

**No service, scheduled task or startup entry is installed. M60 has no apply path.**

---

## 16. Diagnostics bundle (M60.6)

Modes: `PREVIEW`, `REDACTED`, `REDACTED_WITH_SESSION_METADATA`. **There is no `FULL`
mode** — it does not exist in the enum, so it cannot be requested by accident.

Includes: Git commit, schema versions, sanitized config, model-role names, Ollama
reachability, runtime health, lifecycle/service states, bounded error **classes**,
persistence health, recovery result, qualification verdict, file manifest with hashes.

* configuration is **allowlisted**, not denylisted — a future `*_api_key` setting
  cannot leak by being forgotten, because it was never eligible;
* error reporting keeps type names and counts, never messages;
* the file manifest carries names, sizes and hashes, never contents;
* the home-path sweep runs over the **whole serialized bundle**, since a path can
  arrive through any nested section;
* `PREVIEW` and `REDACTED` share the identical payload — preview is not a reduced
  sample, so what the operator approves is exactly what would ship;
* every collector is independently guarded: one broken subsystem must not stop the
  bundle that would explain why it is broken.

---

## 17. Backup and restore (M60.6.1)

Eligible: session journal, operational store, alias registry. **Semantic collections
are excluded by an explicit marker list** — a Chroma directory copied while the
database is live is not a backup, it is a plausible-looking corrupt file.

Backup: temp + atomic rename, schema manifest with per-member SHA-256 and the source
Git commit, verified by **re-reading the written archive** (verifying the buffer just
written would prove nothing).

Restore is **dry-run first**:

* `plan_restore` inspects and reports; a corrupt archive or a schema mismatch is
  `INCOMPATIBLE` and is never merged silently;
* `apply_restore` requires an explicit approval token **and** a compatible plan;
* it takes a **rollback backup of current state first**, and **refuses outright if
  that backup fails** — replacing state you cannot restore would be irreversible;
* a failure part-way cleans every temp file and reports `ROLLED_BACK` with the
  rollback archive named;
* `jobs_launched` is structurally 0. Nothing restored is ever launched, and no HITL
  token or authorization is restorable because none is stored.

---

## 18. Runtime-health additions (M60.9)

**ONE** new subsystem on the **existing** collector — no second registry (asserted).
Unlike the M57/M58 advisory entries, `session_continuity` is **not** purely advisory:
a journal that cannot write is a real durability fault and may degrade the overall
verdict, because the operator's next crash would otherwise lose the conversation
silently. An intentionally `OFF` journal is `DORMANT` (rank 0) — an explicit choice
is not a fault. An unresolved effectful outcome is `WARMING`: an operator obligation,
not a runtime failure.

Metrics (all content-free): `persistence_mode`, `active_session_id_hash`,
`sessions_retained`, `turns_retained`, `journal_state`, `last_checkpoint_ms`,
`checkpoint_failures`, `journal_write_failures`, `journal_slow_writes`,
`redactions_applied`, `corrupt_records_quarantined`, `recovery_required`,
`recovery_state`, `previous_run`, `interrupted_turns`, `unresolved_tool_outcomes`,
`actions_replayed`, `services_registered`, `services_ready`, `services_degraded`,
`restart_attempts`, `circuits_open`, `last_restart_reason`, `last_backup_at`,
`last_backup_state`, `last_backup_size`, `integrity_verified`, `restore_plan_state`,
`rollback_available`, `last_bundle_state`, `files_included`, `bundle_size`,
`bundle_redactions`, `secret_scan_state`.

Producers **publish** their snapshots through `core/recovery_state.py` (the same
pattern `tool_loop.publish_tool_metrics` already uses), so a health read never runs a
backup or builds a bundle — that would turn a read-only view into an expensive side
effect on a 15 W CPU.

---

## 19. Live-runtime wiring

* **boot** — open the RUN record, reconcile the previous process's wreckage,
  re-measure every fingerprint, open this session. The recovery panel prints only
  when there is something to say, so startup stays concise;
* **shutdown callback** — writes the clean-shutdown marker; its **absence** is the
  crash signature;
* **`core/llm.py`** — journals the user turn and opens the assistant turn `ACTIVE`
  *before* generation (the crash anchor), and closes it inside the **existing single
  idempotent finalizer**, so every terminal state — including the
  `GeneratorExit`/`CANCELLED` path — is recorded exactly once with the text the
  operator actually saw;
* a turn found still `ACTIVE` at the next turn or at shutdown is closed truthfully
  (`REPLACED_BY_NEW_TURN` / `CANCELLED_ON_SHUTDOWN`), so an in-process cancellation
  is never misread as a crash at the next boot.

Every hook fails silently by design: a journal problem costs continuity, never an
answer the runtime could otherwise give.

---

## 20. Validation

| Gate | Result |
|---|---|
| Full suite (from Git root) | **3286 passed, 18 skipped, 0 failed** (pre-M60 baseline 2900/18/0) |
| New M60 tests | **386** |
| Focused regression (touched seams) | 223 passed |
| Ruff `jarvis/` | clean |
| `compileall core tools scripts main.py` | clean |
| Deterministic crash soak | 37 passed (child-process harness + storage faults) |
| Temporary-child live validation | **30 / 30 checks passed** |

The one full-suite failure encountered was `test_runtime_health_v67`'s exhaustive
subsystem-set assertion, which every milestone that adds a subsystem updates; it was
updated with the rationale in place, exactly as M55–M58 did.

### Crash-soak coverage

clean shutdown · termination before turn content · termination after partial visible
content · termination between journal transaction steps · effectful tool in flight ·
stale run marker with a live PID · corrupt record · corrupt SQLite file · database
locked · disk write failure / disk full · unreadable journal · backup interruption ·
restore interruption · failed rollback backup · restart storm · circuit breaker ·
recovery without persistence · shutdown during recovery · incompatible schema ·
changed security-policy fingerprint · changed scope fingerprint · continuation
invalidation · metadata-only mode.

### Live validation (bounded, temporary child, no Ollama call)

Checkpoint → complete turn → partial turn → abrupt termination → restart → recovery
status → resume → truthful interrupted turn → zero replay → clean next turn →
diagnostics preview → redacted bundle → redacted export → managed backup → integrity
verify → dry-run restore (+ refusal without approval) → deployment plans (nothing
applied) → clean shutdown → no child process, lock or temp file remains.

---

## 21. Before / after

| Situation | Before M60 | After M60 |
|---|---|---|
| Power loss mid-answer | session file possibly truncated to zero bytes | previous rows intact; partial turn marked `INTERRUPTED_BY_POWER_LOSS` |
| Interrupted answer on resume | replayed as if complete | labelled interrupted; only seen characters restored |
| Text generated but never rendered | could resurface as history | trimmed to `content_chars`; provably absent |
| Effectful tool killed mid-flight | no record at all | `EFFECTFUL_UNKNOWN_OUTCOME` + audit ref + verification recommendation; never rerun |
| Unanswered question after a crash | silently lost | `UNRESOLVED_USER_MESSAGE` marker; no answer invented |
| Clean vs crashed exit | indistinguishable | explicit marker; absence is the crash signature |
| Hidden reasoning / OTP on disk | `redact_secrets` only (no reasoning, no OTP rules) | removed by a deterministic scanner, closed **and** unterminated blocks |
| Flapping service | restarted forever | bounded window + circuit breaker + cooldown probe |
| Restarted service | could reuse a closed-loop queue | fresh `generation`; READY only after a passing probe |
| "Is my journal healthy?" | unanswerable | one health subsystem, content-free, able to degrade the verdict |
| Unreadable journal | would look like "nothing to recover" | `DEGRADED` + `UNKNOWN`, never "clean" |

---

## 22. Remaining limitations

1. **Restore does not hot-swap a live journal.** `apply_restore` replaces files on
   disk; a process holding the old handle keeps it until restart. The plan states
   what it would replace; it does not orchestrate a restart (deliberately — that
   would be an effectful action taken automatically).
2. **Semantic collections are out of scope.** Their backup goes through the existing
   M53 checkpoint/export seam. M60 refuses rather than producing a copy that only
   looks valid.
3. **`UNCLEAN_CRASH` vs `UNCLEAN_POWER_LOSS` is heuristic** — it distinguishes them
   by whether a checkpoint landed. Both are honestly UNCLEAN; the sub-classification
   is advisory and is reported as such.
4. **No deployment apply path.** By design for M60.
5. **The `/v1` tool-calling path journals on the success branch and via the
   finalizer**; a failure outside both is caught by the stale-turn closure at the
   next turn or at shutdown rather than at the moment of failure. The recorded state
   is still truthful, just coarser (`REPLACED_BY_NEW_TURN`).
6. **`content_chars` is only as accurate as the last `record_visible_progress`
   checkpoint** — bounded by design (one write per sentence flush, not per token), so
   a crash can under-report what was seen. Under-reporting is the safe direction.

---

## 23. Is M60 safe to merge?

**Yes**, with the standard caveat that it is a *durability* release: it should be
merged and then exercised across several real sessions before its retention defaults
are tuned.

* 3286 passed / 18 skipped / **0 failed**; no new failure introduced;
* every host-mutation prohibition is enforced structurally (no mutation API imported,
  no apply path defined) and asserted behaviourally;
* no model was downloaded, replaced or reconfigured; Ollama was not restarted; no
  service, startup entry, scheduled task, registry key or environment variable was
  touched;
* the default persistence mode is the conservative one, and every failure mode
  degrades continuity rather than the runtime.

---

## 24. Recommendation for M61

M60 makes the runtime *survive*. The next unanswered question is **evidence quality
over time**: the journal now holds a bounded, redacted, truthful record of many
sessions, and nothing yet reads it back for anything but resumption.

**M61 — Longitudinal Session Intelligence & Continuity Verification** should:

1. verify continuity claims against the journal (does a resumed session actually
   improve the next turn, or does it just cost context?) using the M59 qualification
   harness;
2. measure retention pressure with real usage — the current bounds are principled
   guesses, not measurements;
3. close limitation #1 with an operator-driven restore-and-restart flow (still
   explicit, still never automatic);
4. extend the compaction digest to draw on **finalized journalled turns** rather than
   in-memory history only, so a digest survives a crash the way turns now do.

It should **not** add another store, another supervisor or another command surface.
