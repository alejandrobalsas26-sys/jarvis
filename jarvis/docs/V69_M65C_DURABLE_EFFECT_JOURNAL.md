# V69 M65C — DURABLE EFFECT JOURNAL / CROSS-PROCESS RECOVERY

M65B proved exactly-once for two callers inside one living process. This
milestone addresses the boundary that a future in memory cannot cross: the
process dies.

It does **not** claim universal exactly-once, and the reason is stated before
anything else, because every design decision below follows from it.

---

## 0. THE CLAIM THIS MILESTONE REFUSES TO MAKE

A journal alone cannot guarantee exactly-once for an arbitrary external,
non-transactional effect. The reason is one window:

```
        external effect happened
                 |
                 X   PROCESS DIES
                 |
        durable commit not recorded
```

After a restart the journal contains `EXECUTING`. So does a process that died
*before* it ever invoked the tool. **Those two realities are locally
indistinguishable.** No amount of journalling separates them, because the
evidence that would separate them lives in the external system, not here.

Everything M65C builds is a way of either (a) making that window impossible to
reach in a way that matters, or (b) reporting it truthfully instead of guessing.

---

## 1. CRASH-WINDOW TABLE (P0–P5)

The load-bearing table. `EXECUTING` is committed durably *immediately before*
the handler is invoked, and `COMMITTED` *after* it returns, which is what pins
each row below.

| | journal knows | external world may contain | retry safe? | reconciliation? | exactly-once provable? |
|---|---|---|---|---|---|
| **P0** before reservation | nothing — no row exists | nothing. The reservation strictly precedes every gate and every handler call | **YES** | no | **YES**, trivially: nothing happened |
| **P1** after RESERVED, before effect | `RESERVED`, owner instance, lease | nothing. No external mutation occurs between the reservation commit and the `EXECUTING` commit | **YES**, once the owner is proven gone (lease expired + owner is not this instance) | no | **YES** |
| **P2** during the effect | `EXECUTING` | **unknown** — not started, partially applied, or fully applied | **NO** without a class protocol | yes, for `RECONCILABLE` | conditional on durability class |
| **P3** effect done, before COMMITTED | `EXECUTING` — *identical to P2* | the effect, once | **NO** for `NON_REPLAYABLE` | required for `RECONCILABLE` | `IDEMPOTENT`/`IDEMPOTENT_WITH_KEY`/reconciled `RECONCILABLE`: **YES**. `NON_REPLAYABLE`: **NO** → `INDETERMINATE` |
| **P4** COMMITTED, before caller sees it | `COMMITTED` + receipt digest + stored result | the effect, exactly once | must **not** retry; recovery returns the durable record | no | **YES** — the strongest case |
| **P5** caller received the result | `COMMITTED` | the effect, once | no retry occurs; a later identical identity dedupes durably | no | **YES** |

**The single most important row is that P2 and P3 are the same row from the
journal's point of view.** That is why "the EXECUTING owner's lease expired"
can never be read as "the effect did not happen", and why a lease expiry is
never, on its own, permission to re-run an effect.

---

## 2. STORAGE DECISION

### What already exists

| primitive | what it is | reusable as the effect journal? |
|---|---|---|
| `core/operational_store.py` | stdlib `sqlite3`, WAL, schema-versioned, forward migration, on the local NVMe at `jarvis/data/operational_state.db` | **no** — see below |
| `data/sessions/session_continuity.db` | session journal, same engine | no — different domain, same reasons |
| `core/managed_backup.py` | registry of eligible durable artifacts | **yes, extended** — the journal is registered there |
| `core/semantic_migration.py`, `core/persistence_hunter.py` | JSON state files | no — no atomicity, no uniqueness constraint |

### Why `OperationalStore` is not reused directly

Four disqualifying properties, each of which M65C specifically needs the
opposite of:

1. **It fails open.** If the DB cannot be opened it silently degrades to an
   in-memory database and continues, reporting `durable=False`. An effect
   journal that quietly becomes volatile lets a crash duplicate an effect while
   the system believes it is protected. M65C must fail *closed* (§25).
2. **Its write primitive is an upsert keyed by content hash.** That is
   check-then-act at a higher level; it cannot express "exactly one process wins
   this reservation".
3. **It persists full payload projections.** The journal must persist digests
   only (§52).
4. **No `busy_timeout` and no explicit transactions** (`isolation_level=None`,
   autocommit). It was built for one process. Cross-process contention would
   raise `database is locked` on the first collision.

So M65C reuses the **engine and the conventions** — stdlib `sqlite3`, local
`jarvis/data/`, WAL, a `meta` table carrying `schema_version`, forward-only
migration, registration for managed backup — in a **new module** with the
cross-process discipline this domain requires. No new dependency and no external
service (§64): a local-first JARVIS should not need a distributed database to
survive its own restart.

### Why SQLite is the right engine here

M65C needs exactly the four things SQLite provides and a JSON file does not:

* **atomic reservation** — `INSERT … ON CONFLICT DO NOTHING` against a primary
  key decides a race in one statement;
* **process-safe locking** — `BEGIN IMMEDIATE` serialises writers across
  processes, and `busy_timeout` bounds the wait;
* **durable commit** — an fsync'd WAL frame, so a committed row survives power
  loss;
* **transactions** — a state transition and its audit row land together or not
  at all.

### PRAGMAs, each with a reason

Nothing here is cargo-culted; every setting is present because a specific
failure mode requires it.

| pragma | value | why it exists |
|---|---|---|
| `journal_mode` | `WAL` | recovery inspection and the Runtime Doctor read the journal while an effect is reserving. Under the rollback journal a reader blocks the writer. Persistent property of the file, set once. |
| `busy_timeout` | `5000` ms | §24 requires bounded contention. Without it SQLite raises immediately on a collision; with an unbounded wait a stuck process hangs startup. Finite by construction. |
| `synchronous` | `FULL` | **the load-bearing one.** Under WAL the default `NORMAL` does *not* fsync on commit — it syncs at checkpoints. A power loss could then lose a `COMMITTED` row for an effect that really happened, manufacturing the exact P3 ambiguity this module exists to prevent. `FULL` fsyncs the WAL on commit. §63 warns against accidental per-effect fsync; this one is deliberate, understood, and paid only on effect-journal writes (single digits per turn), never on a hot path. |
| `foreign_keys` | `ON` | the transition-audit table references the effect row. An orphan transition is a corrupt audit trail. |

No DB transaction is ever held across a tool invocation (§9, §23). The
reservation transaction covers a handful of statements and nothing that awaits.

---

## 3. STATE MACHINE

```
                         UNSEEN
                            |
                            v
            +---------- RESERVED <--------+ reclaim (new owner, attempt+1)
            |               |             | only from RESERVED, lease expired
            v               v             |
  FAILED_BEFORE_EFFECT  EXECUTING --------+
                            |
              +-------------+-------------+
              |             |             |
              v             v             v
         COMMITTED   FAILED_OBSERVED  INDETERMINATE
                                          |
                              +-----------+-----------+
                              |                       |
                              v                       v
                   RECONCILED_COMMITTED   RECONCILED_NOT_EXECUTED
```

* `FAILED_BEFORE_EFFECT` — **proven** pre-effect: a preflight refusal, a
  guardrail block, an authority/scope refusal, a denied HITL challenge, or a
  cancellation observed before `EXECUTING` was committed. Retry is legitimate.
* `FAILED_OBSERVED` — the handler returned an error or raised, and **this
  process observed it**. Distinct from `INDETERMINATE`, which means the owner
  never came back at all. See the limitation in §12 below.
* `INDETERMINATE` — a row left in `EXECUTING` whose owner is gone. The P2/P3
  window. Never collapsed into "did not execute".

Transitions are validated against an explicit allowed-edge table and rejected
otherwise; a rejected transition raises rather than silently writing.

---

## 4. ATOMIC RESERVATION

The claim is a **single statement** inside `BEGIN IMMEDIATE`:

```sql
INSERT INTO effects (...) VALUES (...) ON CONFLICT(effect_id) DO NOTHING
```

`cur.rowcount == 1` means this process owns the effect. There is deliberately no
`SELECT → decide → INSERT` anywhere in `reserve()`: that shape is exactly the
M65B bug, and writing it against a database would only move the same race onto
disk where it is harder to see.

Reclaiming a stale owner is likewise **one conditional `UPDATE`**, guarded on the
state, the expiry *and* the `owner_attempt` this caller read — a compare-and-swap.
Two processes that both observe the same stale row still produce one winner,
because the loser's `UPDATE` matches zero rows after the winner bumps the attempt.

**No transaction spans a tool call.** The reservation transaction covers a
handful of statements and nothing that awaits; the journal is left in `EXECUTING`
with no lock held while the external call runs, which is precisely why a crash
there is recoverable. Proven by construction: four processes hold `RESERVED` rows
simultaneously while a fifth reads the journal.

### A defect this found

`PRAGMA journal_mode=WAL` was being issued **before** `busy_timeout` was set, and
SQLite does not reliably route a journal-mode change through the busy handler. Two
processes opening a brand-new journal at the same instant made one fail outright
with `database is locked`. Not a test artefact — two JARVIS processes starting
together would hit it. WAL is now established under a bounded retry that also
accepts another process having won the race.

---

## 5. OWNER, WAITER AND LOST OWNER

| situation | behaviour |
|---|---|
| same process, second caller | the M65B in-flight reservation parks it on the owner's future. It never reaches the journal. |
| another process, live owner | `OWNED_ELSEWHERE`. The caller polls within a bounded window (`DURABLE_WAIT_S`), then reports `BLOCKED_OWNED_ELSEWHERE`. It never executes. |
| another process, expired `RESERVED` owner | reclaimable by any class — see §6. |
| another process, expired `EXECUTING` owner | class-dependent — see §6. |
| **a lost owner waking up** | its write is a **silent no-op**. `_apply` checks `owner_instance_id` before validating the edge, so a process that lost its reservation inside the tool cannot commit over the new owner. It returns `False` rather than raising: a late-waking owner is a race, not a programming mistake, and raising would surface as the failure of a call whose effect already happened. |

---

## 6. STALE OWNERS, BY STATE AND CLASS

This table is the heart of §11/§39.

| stale row | `IDEMPOTENT` / `IDEMPOTENT_WITH_KEY` | `RECONCILABLE` | `NON_REPLAYABLE` |
|---|---|---|---|
| `RESERVED` (P1) | reclaim and run | reclaim and run | reclaim and run |
| `EXECUTING` (P2/P3) | reclaim and replay | → `INDETERMINATE`, reconcile | → `INDETERMINATE`, blocked |

A `RESERVED` row is safe for **every** class, and the reason is structural rather
than statistical: `EXECUTING` is committed durably *before* the handler is
invoked, so an owner still in `RESERVED` provably never called anything.

An `EXECUTING` row is safe for **no** class on its own. An expired lease means the
owner is gone; it says nothing whatever about whether the effect happened. A
`NON_REPLAYABLE` effect there is blocked, permanently, until a human resolves it.

`_take_over` carries a second guard against reclaiming an `EXECUTING` row for a
non-replayable class, so a future caller reaching that helper by a path which
skipped the class check still cannot re-run an ambiguous effect.

### Clock anomalies (§40)

Expiry requires `now > lease_expires_at + grace`. A backward jump makes `now`
smaller and can only ever say "not expired"; a modest forward jump is absorbed by
the grace. A malformed timestamp reads as **not expired**, because guessing wrong
in that direction costs a duplicate effect and guessing wrong in the other costs
a wait.

---

## 7. IDEMPOTENCY KEYS

`idempotency_key = SHA256("jarvis.m65c.idempotency.v1" ‖ effect_id)`

Derived from the canonical effect identity and nothing else, so it is identical
on every attempt and identical after a restart — which is the only reason the
external system's deduplication works at the moment it is needed. It contains no
timestamp and no random component; either would defeat it precisely when the
crash happens.

It is also **not user-selectable**. An argument literally named
`idempotency_key` feeds the identity like any other argument and can never *be*
the key, so model-authored text cannot steer two different actions onto one key
or one action onto two. A test asserts this with a forged argument.

---

## 8. RECONCILIATION

A `RECONCILABLE` tool registers a bounded probe answering one of three things:

* `CONFIRMED_COMMITTED` → the row becomes `RECONCILED_COMMITTED`; the effect is
  recovered, never replayed;
* `CONFIRMED_NOT_EXECUTED` → `RECONCILED_NOT_EXECUTED`; a fresh attempt is
  legitimate;
* `UNKNOWN` → **nothing is written**. The effect stays `INDETERMINATE`.

`UNKNOWN` is a first-class answer. A probe that raises, returns the wrong type,
or does not exist is read as `UNKNOWN` — never as either certainty. Turning
uncertainty into success is the single failure this module exists to prevent.

---

## 9. INDETERMINATE

`INDETERMINATE ≠ SAFE_TO_RETRY`. An automatic retry is permitted only where:

* the effect is proven never to have started (`FAILED_BEFORE_EFFECT`,
  `RECONCILED_NOT_EXECUTED`); or
* the class makes a repeat safe on its own terms (`IDEMPOTENT`,
  `IDEMPOTENT_WITH_KEY`).

`RECONCILABLE` is deliberately excluded from that list: its route out of
ambiguity is a reconciliation *answer*, not a retry.

At the caller boundary an `INDETERMINATE` identity returns a typed refusal
(`error_class: indeterminate_effect`) naming the effect id and the class. Asking
again does not wear it down — a test asks three times — because repetition is not
evidence.

---

## 10. MCP UNIFICATION

Before this milestone `aexecute_mcp` carried the race `aexecute` had lost in
M65B: read the ledger, await a HITL challenge and an RPC, write the ledger. Two
concurrent MCP callers both saw an empty ledger and both ran.

Both surfaces now call one `_execute_effect_protocol`. Identity, the in-flight
reservation and the durable journal live there once; each surface keeps only its
own gate. A test asserts that neither `aexecute` nor `aexecute_mcp` contains the
string `journal.reserve` or `mark_executing`, because two implementations of the
protocol would be two things to keep in step by hand.

The gates communicate through one hook, called at exactly one point —
immediately before the tool runs — which is what lets the protocol tell "refused
before the effect" from "the effect started" without duplicating either gate.
The hook sits **outside** each gate's `try`, because a journal that cannot record
an effect is not a tool fault and sanitising it into one would tell the operator
"the tool failed" for a call that never ran.

**MCP also gained a check it never had**: the native gate has enforced
`authorize_action` (operator authority / authorized scope) since V63; the MCP
gate did not. It does now, and a test covers it.

### The exact identity boundary

The durable identity includes `surface`, so a native `x` and an MCP `x` are
different effects. That is deliberate: a local handler and a remote server are
different code, and fusing their identities would claim an equivalence nothing
has established. In this build the question is moot — the two namespaces are
**disjoint** (`MCP_TOOL_ALLOWLIST` is `abrir_packet_tracer` and
`generar_laboratorio_red`; neither has a local `_tool_*` handler) — but the
boundary is stated rather than left implicit.

---

## 11. CANCELLATION

* **Before the tool ran** → `FAILED_BEFORE_EFFECT`. Cancelling proves nothing
  happened, and the identity is not poisoned.
* **After the tool ran** → `INDETERMINATE`. The handler is on a thread pool that
  keeps going, and a caller changing its mind is not evidence about the world.

A committed or indeterminate record is never erased to make a cancellation look
clean.

---

## 12. HITL AND SCOPE RECOVERY

**A durable reservation is not permission.** The journal binds an authority
digest, a scope digest and an approval digest to an effect identity so a later
attempt can be *compared* against them; there is no field an authority can be
read back out of, and a test asserts the rendered record contains no posture or
scope text.

On reclaim, those digests are **overwritten with the new attempt's, never
inherited**. A durable row cannot lend a later attempt an approval it did not
obtain.

Recovering an already-proven `COMMITTED` receipt is not a new effect and asks the
operator nothing. A genuinely new effect is challenged on its own — both halves
are tested on the live path.

---

## 13. PRIVACY

Every column is an id, an enum, a timestamp, a counter or a domain-separated
digest. Tests read the **database bytes** and assert a planted secret argument
and a planted secret result are absent, in-process and from a spawned worker.

The consequence, stated rather than worked around: **recovery returns an
envelope, not the original response body.**

```json
{"status": "recovered", "disposition": "RECOVERED_COMMITTED",
 "effect_id": "...", "tool": "...", "committed_at": "...",
 "receipt_digest": "...",
 "detail": "... The original result body is not retained by the journal."}
```

Retaining bodies would make this file a copy of every tool result the runtime has
ever produced. A test pins the envelope's exact field set so it cannot quietly
grow one.

Digests are domain-separated (`args`, `action`, `receipt`, `opaque`, `effect`,
`idempotency`), so a value cannot be replayed from one field into another.

### What the digests are NOT

SHA-256 over canonical JSON with **no key**. It detects accidental
inconsistency. It is **not** an authentication tag: anyone who can write the file
can also make it self-consistent. Nothing here is described as tamper-proof, and
§26 of the brief is honoured by saying so plainly rather than by adding a
ceremony that would not survive a local attacker.

---

## 14. CORRUPTION AND SCHEMA

**Fail closed, never repair destructively.**

| condition | behaviour |
|---|---|
| file cannot be opened | `JournalUnhealthy`. **No in-memory fallback** — that is `OperationalStore`'s behaviour and it is exactly what an effect journal must not do. |
| `PRAGMA integrity_check` fails | `JournalUnhealthy`. The file is **not modified** — a test asserts its bytes and size are unchanged after the refusal. |
| a table is missing | `JournalUnhealthy`, naming the table. |
| schema newer than this build | refused, unmodified. A newer layout may carry states this code cannot honour. |
| a migration step is missing | refused. There is deliberately no generic "recreate the table" path. |
| effectful call while unhealthy | refused with `error_class: journal_unhealthy`. |

The sanctioned recovery is restoring from a **managed backup**, so the journal is
registered in `core/managed_backup.py` — it can only be that recovery route if it
is actually backed up.

Committed identities are **retained**, always. Deleting dedupe history reopens
every replay this module closes, so there is no GC.

---

## 15. THE CHAOS TESTS

Workers are **subprocesses**, not `multiprocessing`. Measured: `spawn`
re-executes the parent's `__main__`, which under `python -m pytest` is pytest, so
every worker started a nested pytest session and the suite hung with no output at
all. A plain subprocess has no such coupling and gives what the tests need — a
fresh interpreter, and therefore a fresh `runtime_instance_id`, which is what a
restart really is.

Ordering is forced by an N-way **file barrier** and a **park/release**
rendezvous, never by sleeping. The one wall-clock wait in the suite is where a
real lease has to really expire. Every wait is bounded, so a broken
implementation fails instead of hanging. The parent terminates only processes it
created.

### The synthetic world

```
world/attempts.log      one line per INVOCATION
world/effects/<name>    one file per DISTINCT external effect
```

The split is what makes the claim matrix measurable. An idempotent-with-key
system deduplicates, so two attempts leave two lines and **one** effect file; a
non-replayable one leaves two lines and **two**. Counting invocations would prove
nothing about duplication.

### Coverage

`C0` before reserve · `C1` after reserve · `C2` immediately before the tool ·
`C3` effect applied then crash · `C4` after commit · `C5` after delivery. Plus a
parent-sent **SIGKILL** to its own child, because normal exception cleanup runs
the `finally` blocks a crash does not.

---

## 16. THE DURABILITY CLAIM MATRIX

Every cell is backed by a test, and the wording of §18 follows from this table.

| durability class | no duplicate after a **pre-effect** crash (P0/P1/C2) | no duplicate after a **P3** crash | automatic recovery | automatic retry | reconciliation needed |
|---|---|---|---|---|---|
| `READ_ONLY` | n/a — never journalled | n/a | n/a | n/a (repeating a read is not an effect) | no |
| `IDEMPOTENT` | **yes** | converges to one state; the call may repeat | yes | yes | no |
| `IDEMPOTENT_WITH_KEY` | **yes** | **yes — one external effect**, two invocations | yes | yes, under the same derived key | no |
| `RECONCILABLE` | **yes** | **yes when the probe answers**; `UNKNOWN` → blocked | yes, via reconciliation | **no** | yes |
| `NON_REPLAYABLE` | **yes** | **no** — `INDETERMINATE` | no | **no, denied** | manual |

### The reachable production surface, audited

| class | production tools |
|---|---|
| `READ_ONLY` | 24 — never journalled |
| `IDEMPOTENT` | **1** — `set_clipboard` |
| `IDEMPOTENT_WITH_KEY` | **0** |
| `RECONCILABLE` | **0** |
| `NON_REPLAYABLE` | **23** — everything else effectful, including all 15 `HIGH_IMPACT` |

The audit was deliberately pessimistic. `set_clipboard` is the only local tool
whose repeat provably converges — `pyperclip.copy(text)` with the same text
leaves the same clipboard, and the text is part of the identity. `write_file`
*looks* idempotent and is **not**: it accepts `mode="a"`, and an append repeated
is an append duplicated, so a per-tool table that must hold for every argument
the tool accepts classifies it `NON_REPLAYABLE`.

**No production tool declares `IDEMPOTENT_WITH_KEY` or `RECONCILABLE` today.**
Neither protocol has a real external system behind it in this build. Both are
implemented and proven against test-owned synthetic tools registered through
`register_durability` — the same entry point a real tool would use. Saying so is
the point: the protocols exist and work, and nothing in production uses them yet.

---

## 17. WHAT M65C DOES **NOT** CLAIM

> **Universal exactly-once across crashes is NOT claimed, and cannot be.**

Precisely:

```
DURABLE_DEDUPE                                 YES
CROSS_PROCESS_EXACTLY_ONCE_FOR_COMMITTED       YES
P3_IDEMPOTENT_WITH_KEY_EXACTLY_ONCE            YES
P3_RECONCILABLE_EXACTLY_ONCE                   YES, when the probe answers
P3_NON_REPLAYABLE                              INDETERMINATE / MANUAL RECONCILIATION
UNIVERSAL_EXACTLY_ONCE                         NO
```

23 of the 24 reachable effectful tools are `NON_REPLAYABLE`, so for the large
majority of what JARVIS can actually do, a P3 crash ends in a truthful "I cannot
tell" rather than in exactly-once. That is the honest state of the system, and
the whole design is arranged so the system says it rather than guesses.

---

## 18. LIMITATIONS

* **`FAILED_OBSERVED` inherits M64.1's retry policy unchanged, and that policy is
  an assumption.** When a handler returns an error while the process is alive,
  M64.1 treats the world as unchanged and permits a retry. For a network-mediated
  tool that is *not* provable — a timeout may well have applied the effect. M65C
  models the state truthfully (it is neither `FAILED_BEFORE_EFFECT` nor
  `INDETERMINATE`) but does **not** change the policy: doing so alters behaviour
  for every existing tool and belongs to a milestone authorised to make that
  change. This is the largest known gap.
* **Durable dedupe reaches exactly as far as the caller's identity scope.** The
  scope is the effect epoch, which is `turn:<task_id>` when the turn has a durable
  id and `turn:<id(task_decision)>` — a memory address — when it does not. A bare
  chat turn therefore does not dedupe across a restart. That is correct (two
  separate operator requests are two effects) but it means the P4 recovery
  guarantee applies to resumed *tasks*, not to retyped *questions*.
* **`IDEMPOTENT_WITH_KEY` and `RECONCILABLE` have no production tools.** §16.
* **Row digests are not authenticated.** §13.
* **The journal is local-first and single-file.** No multi-host coordination; two
  machines sharing an NFS journal is out of scope and untested.
* **`DURABLE_WAIT_S` is a fixed 30s poll** for a live owner in another process.
  Bounded and safe (it never executes on timeout) but not adaptive.
* **A recovered result is an envelope, not the original body.** §13. A caller that
  needs the original payload after a restart must re-derive it.
* **Pre-existing, unchanged:** production L2 NONE, production L3 NONE,
  `L2_POLICY_MAPPING_GAP` OPEN.
