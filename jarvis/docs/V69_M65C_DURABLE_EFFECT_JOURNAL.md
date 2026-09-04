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

## 4. WHAT IS NOT DECIDED YET

Sections 5 onward (atomic reservation semantics, owner/waiter behaviour, stale
owners, idempotency classes, reconciliation, MCP unification, cancellation,
HITL/scope recovery, privacy, corruption, chaos tests, the mutation campaign,
the durability claim matrix and the limitations) are written as they are built
and measured, not predicted here.
