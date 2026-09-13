# V69 M65D — TRUTHFUL UNCERTAIN-EFFECT SEMANTICS

M65C proved the durable effect journal is a correct **store**, and named its own
largest gap in §18: `FAILED_OBSERVED` kept M64.1's retry policy unchanged, and
that policy assumed a handler which returned an error had left the world
unchanged.

This milestone is about that assumption. It was not a rough edge. It was a
statement about the external world inferred from a fact about local software,
and it produced a duplicated irreversible effect on the first fixture built to
look for one.

---

## 0. THE TWO SENTENCES

```
        an observed error   is a fact about the software
        an external effect  is a fact about the world
```

They are not the same variable, and after the effect boundary nothing local can
derive the second from the first:

```
        JARVIS sends the request
                 |
        the remote system applies the effect
                 |
        the response is lost / the connection resets / the call times out
                 |
        the handler raises
                 |
        JARVIS observes an error
```

External reality is now **EFFECT HAPPENED**. Local reality is **ERROR
OBSERVED**. One dropped TCP segment separates them.

---

## 1. THE HISTORICAL DEFECT, REPRODUCED BEFORE ANYTHING WAS CHANGED

Two lines on sealed master `72e2948`.

**`core/effect_journal.py`, `_resolve_existing`:**

```python
if state in (EffectState.FAILED_BEFORE_EFFECT,
             EffectState.FAILED_OBSERVED,
             EffectState.RECONCILED_NOT_EXECUTED):
    return self._take_over(..., ReservationOutcome.OWNED,
                           "retry after a non-effect outcome")
```

`FAILED_OBSERVED` sat between two states that really *are* proven pre-effect,
under a note calling all three "a non-effect outcome". No durability class was
consulted on that branch. `_take_over`'s own guard read
`if frm is EffectState.EXECUTING and cls not in _REPLAYABLE_AFTER_AMBIGUITY` —
it did not fire, because `frm` was `FAILED_OBSERVED`. A `NON_REPLAYABLE` effect
was handed back to the next caller as `OWNED`.

**`tools/executor.py`, `_durable_effect`:**

```python
journal.fail_observed(effect_id, "tool_returned_error")
note.update({"disposition": ExecutionDisposition.FAILED_BEFORE_EFFECT.value,
```

The disposition published to the caller, after the boundary had been crossed,
was the literal string `FAILED_BEFORE_EFFECT`.

The module already contained the correct rule: `may_auto_retry()` answered
`False` for `FAILED_OBSERVED`. **Two policies existed and the live one was the
unsound one** — which is why the fix is one decision function rather than one
more conditional.

### Measured, against unmodified master

A localhost-only fixture that applies a durable effect and then closes the
connection before answering:

```
first_attempt_handler_result   sanitized internal_error (RemoteDisconnected)
first_attempt_external_effect  1
first_attempt_journal_state    FAILED_OBSERVED
first_attempt_disposition      FAILED_BEFORE_EFFECT      <- untrue
second_attempt_authorized      YES  (ReservationOutcome.OWNED)
second_attempt_handler_called  YES
final_external_effect_count    2
unsafe_duplicate_reproduced    YES
```

Kept executable as
`test_uncertain_effect_live_v69_m65d.py::test_the_historical_semantics_would_have_duplicated_it`,
which re-applies the historical branch to a live row and asserts that the
fixture really can be made to duplicate — so the flagship test above it cannot
become vacuous without that one going red.

---

## 2. FOUR THINGS, NOT TWO

M65C spelled four concepts with two. M65D separates them.

| axis | question | representation |
|---|---|---|
| execution phase | has the boundary been crossed? | `EffectState` (`RESERVED` vs `EXECUTING`) |
| local observation | what did this process see? | returned / raised / timed out / cancelled |
| **external knowledge** | **what is true of the world?** | **`ExternalOutcome`** |
| **retry authority** | **what may happen next?** | **`RetryAuthority`** |

Only the first is a state machine. The third is a separate axis stored beside
the state. The fourth is a decision, and it lives in exactly one function.

### `ExternalOutcome`

`PROVEN_COMMITTED` · `PROVEN_NOT_EXECUTED` · `UNKNOWN`

Three values, not two. `UNKNOWN` is a first-class answer and is never rounded.

### Where it comes from

`_STATE_OUTCOME` maps every state to what it implies about the world.
**Exactly one entry is `None`** — `FAILED_OBSERVED` — meaning "the state does
not settle this; read the stored column". Everywhere else the phase and the
knowledge coincide, and only after the boundary, where a local error and a lost
response are indistinguishable, do they come apart.

That asymmetry is enforced by a test, not by a comment:
`test_exactly_one_state_leaves_the_external_outcome_open`.

A stored value is consulted **only** where the state leaves the question open,
so writing `PROVEN_NOT_EXECUTED` into a `COMMITTED` row's column changes
nothing. An unparseable or absent value reads as `UNKNOWN` — fail-closed, and
the direction that costs a reconciliation rather than a duplicate.

---

## 3. ONE DECISION, ONE TABLE

```python
retry_authority(*, state, durability_class, recorded_outcome=None, verdict=None)
        -> RetryAuthority
```

Pure, total, offline. No clock, no database, no tool call — so the whole truth
table is exercised directly rather than inferred from end-to-end behaviour.

It answers a question that has **already been narrowed**: *given that no live
owner holds this identity, what may happen next?* Liveness is a separate, prior
question — a leased row is `OWNED_ELSEWHERE` and never reaches here — and
conflating the two would let an unexpired lease read as permission.

```
knowledge is PROVEN_COMMITTED    -> ALREADY_COMMITTED       (outranks every class)
knowledge is PROVEN_NOT_EXECUTED -> SAFE_TO_RETRY
knowledge is UNKNOWN:
    READ_ONLY                    -> SAFE_TO_RETRY           (no effect by contract)
    IDEMPOTENT / _WITH_KEY       -> REPLAY_SAFE_BY_CONTRACT
    RECONCILABLE, not yet asked  -> REQUIRES_RECONCILIATION
    RECONCILABLE, asked: UNKNOWN -> BLOCKED_INDETERMINATE
    NON_REPLAYABLE               -> BLOCKED_INDETERMINATE
```

Three consequences worth stating:

* **`PROVEN_COMMITTED` outranks the durability class.** An `IDEMPOTENT` tool
  proven to have landed gains nothing from a replay, and a second external call
  made for no reason is still a second external call.
* **Asked-and-unanswered is not unasked.** A reconciler that said `UNKNOWN` —
  or that raised, which `reconcile()` reports as `UNKNOWN` — has been consulted
  and produced nothing. Asking again in a loop is not a route out.
* **`READ_ONLY` is kept separate from the replay classes.** It is not "a replay
  we decided was safe"; it is "there was never an effect".

Every caller consults it: `_resolve_existing`'s observed-failure branch, its
stale-`EXECUTING` branch, `_take_over`'s guard, and `may_auto_retry`, which is
now a thin reading of it rather than a second copy.

### The guard, widened

```python
# M65C
if frm is EffectState.EXECUTING and cls not in _REPLAYABLE_AFTER_AMBIGUITY:
    raise InvalidTransition(...)

# M65D
authority = retry_authority(state=frm, durability_class=..., recorded_outcome=...)
if authority not in _MAY_EXECUTE_AGAIN:
    raise InvalidTransition(...)
```

The old shape is exactly why the duplicate got through: the caller arrived with
`frm=FAILED_OBSERVED`, the comparison did not fire.

---

## 4. NO EXCEPTION-TYPE MAGIC

Nothing in the decision path reads an exception type, an HTTP status, an error
message or a lease expiry. There is nowhere for them to enter: `retry_authority`
takes four keyword arguments and none of them is a failure, which is asserted
structurally by `test_a_lease_expiry_is_not_an_input_to_the_decision`.

Behaviourally, every one of these produces `UNKNOWN`:

```
TimeoutError            ConnectionResetError      ConnectionRefusedError
OSError                 RuntimeError("...rejected the request")
ValueError("nothing was changed")
```

`retryable` in the sanitised failure envelope was the one remaining place an
exception type reached a retry hint (`retryable=(error_class == "timeout")`).
It is advice to the caller rather than authority — the journal blocks either
way — but advice may not be false, so it is now `False` once the boundary has
been crossed.

---

## 5. TYPED EVIDENCE — THE ONLY WAY UNCERTAINTY BECOMES CERTAINTY

```python
@dataclass(frozen=True)
class EffectOutcomeEvidence:
    outcome: ExternalOutcome        # never UNKNOWN
    reason_code: str                # <= 64 chars of [a-z0-9_.:-]

class DeclaredEffectOutcome(Exception):
    ...
```

An adapter that knows something raises `DeclaredEffectOutcome`. Both gates
recognise it, hand the evidence to the protocol out of band, and return an
ordinary sanitised failure envelope to the caller.

**The trust boundary is the whole point.** A field in a returned payload would
be writable by a remote MCP server, echoable out of a model's tool argument,
and forgeable by a compromised handler — and "the effect did not happen" is
precisely the claim an untrusted party must not be able to make about JARVIS's
safety decision. A Python exception raised by in-process JARVIS code is none of
those things.

Proven by `test_a_response_body_cannot_manufacture_evidence` and its MCP twin:
a payload carrying `external_outcome: PROVEN_NOT_EXECUTED`, or a forged
`effect_uncertainty` block, is inert, and the second call is still blocked.

`reason_code` is validated, not trusted. A bounded lowercase token cannot become
a channel for a body to reach the journal in instalments (§28).

`UNKNOWN` cannot be asserted: evidence of uncertainty is either a no-op or a way
to overwrite a certainty with one.

---

## 6. WHAT `FAILED_OBSERVED` MEANS NOW

Not renamed. Renaming would have moved every historical row for an aesthetic
gain, and the name was never the problem — the policy attached to it was.

> **`FAILED_OBSERVED`**: this process observed a failed local invocation *after*
> the effect boundary was durably entered.

That is all it means. What the world contains is the separate axis, defaulting
to `UNKNOWN`.

Two edges changed:

* `FAILED_OBSERVED -> RESERVED` **survives but is guarded**. The edge existing is
  not permission to traverse it.
* `FAILED_OBSERVED -> INDETERMINATE` is **new**. A post-boundary failure nobody
  owns, whose outcome is unknown and whose class cannot repeat, is classified so
  the reconciliation machinery and the operator surfaces can see it.
  Uncertainty gets one home; a second would be a second thing to keep correct.

`EXECUTING -> FAILED_BEFORE_EFFECT` still does not exist, and now cannot exist:
`test_every_post_boundary_state_leaves_only_through_evidence_or_contract` reads
the edge set structurally and requires every edge out of an `UNKNOWN` state into
an executable one to be one the guard covers.

---

## 7. THE FAILURE-WINDOW MATRIX

| window | produced by | external truth | what JARVIS knows | handler runs again |
|---|---|---|---|---|
| F0 | failure before reservation | none | n/a | reservation never made |
| F1 | gate refuses pre-boundary | 0 | `PROVEN_NOT_EXECUTED` | **YES** |
| F2 | `EXECUTING` durable, handler raises before the call | 0 | `UNKNOWN` | no |
| F3 | connection refused | 0 | `UNKNOWN` | no |
| F4 | **effect applied, response lost** | **1** | `UNKNOWN` | no |
| F5 | effect applied, reply received, postprocessing raises | 1 | `UNKNOWN` | no |
| F6 | client timeout over a hung server | 1 | `UNKNOWN` | no |
| F7 | SIGKILL after the boundary | 1 | `UNKNOWN` | no |
| F8 | observed-failure journal write fails | 1 | `UNKNOWN` (row stays `EXECUTING`) | no |
| F9 | reconciler raises / times out | 1 | `UNKNOWN` | no |
| F10 | reconciler answers `UNKNOWN` | 1 | `UNKNOWN` | no |
| F11 | no-effect evidence obtained, process dies first | 0 | `UNKNOWN` | no |
| F12 | committed evidence obtained, process dies first | 1 | `UNKNOWN` | no |

**F1 is the only row where the handler may run again, and it is the only row
whose external truth is PROVEN.** That correspondence is the milestone.

F2 and F3 really did leave the world untouched, and JARVIS still declines to say
so. That is not timidity — the only thing separating them from F4 is knowledge
it does not have, and the inference that would let it through ("connection
refused means nothing happened") is wrong the moment the reset arrives *after*
the send.

F11 and F12 are the losing-evidence cases, and both degrade toward `UNKNOWN`:
what survives a crash is `EXECUTING`. Losing evidence costs a reconciliation,
never a duplicate.

---

## 8. SCHEMA v1 -> v2, AND WHAT IT DOES TO M65C JOURNALS

`SCHEMA_VERSION` goes to **2**. This is a change of persisted meaning, not
ceremony: without somewhere on disk to say that a post-boundary failure was
proven harmless, every such row would have to read as the same thing, and one of
the two readings would be a guess about the external world.

```sql
ALTER TABLE effects ADD COLUMN external_outcome TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE effects ADD COLUMN outcome_evidence TEXT NOT NULL DEFAULT '';
```

Forward-only, transactional, in place. No row rewritten, no row deleted, the
`transitions` audit untouched.

**The constant default is the entire safety argument.** An M65C journal contains
`FAILED_OBSERVED` rows written under semantics that treated them as proven
harmless. After the step they read as "post-boundary failure, external outcome
unknown" — which for a `NON_REPLAYABLE` tool is a block. The migration makes
historical rows **more conservative, never less**, and does it without needing
to know anything about them, which is what makes it safe against a journal this
code has never seen.

Proven against a **real v1 file** built with the M65C DDL written out verbatim
in the test (importing the current DDL would make the test agree with whatever
the schema happens to be):

* the row survives, with its `failure_class`, `owner_attempt` and transition history;
* it reads `UNKNOWN`;
* re-reserving returns `INDETERMINATE`, not `OWNED`;
* a historical `COMMITTED` row still deduplicates;
* a newer-than-known schema is still refused;
* a missing migration step refuses rather than guessing;
* a **failed** migration leaves the file at v1 with its rows intact.

A new attempt resets `external_outcome` and `outcome_evidence` alongside the
approval, authority and scope digests M65C already reset — a durable row must
not lend a later attempt a fact it did not establish.

---

## 9. STRUCTURED FAILURE, AND WHAT THE OPERATOR READS

`effect_note` gains three keys, always present so reading them is never a guess
about which branch ran:

```
external_outcome          UNKNOWN | PROVEN_NOT_EXECUTED | PROVEN_COMMITTED
retry_authority           the RetryAuthority value
reconciliation_required   bool
```

Three new dispositions replace the single false `FAILED_BEFORE_EFFECT`:
`FAILED_OBSERVED_UNKNOWN`, `FAILED_OBSERVED_NOT_EXECUTED`,
`FAILED_OBSERVED_COMMITTED`.

The failing call's own result gains one bounded, body-free `effect_uncertainty`
block. The blocked call gets a message that names the identity, the uncertainty,
the durability class and whether anything other than a human can settle it:

> "Efecto bloqueado: la llamada falló localmente, pero no se puede probar si el
> efecto externo llegó a ocurrir. No se reintenta automáticamente para no
> duplicarlo. Clase de durabilidad: NON_REPLAYABLE. No existe reconciliación
> automática para esta herramienta; requiere reconciliación manual."

Not "Tool failed. Retrying." — that sentence is what produced the duplicate.

**Switching the journal off does not make a failure look harmless.** M65C
reported `FAILED_BEFORE_EFFECT` on the journal-disabled branch too: the same
false statement, in the one place where nothing is left to catch it. That branch
now reports the truth as well.

---

## 10. OBSERVABILITY

Bounded counters, no high-cardinality or secret-bearing labels:

```
failed_observed_unknown          failed_observed_proven_not_executed
failed_observed_proven_committed failed_observed_blocked
failed_observed_replayed_by_contract
failed_observed_retry_proven_safe
replay_permitted_by_contract
```

`status()` gains `failed_observed` and `uncertain_observed` (post-boundary
failures with an unknown outcome whose class cannot repeat), and
`recovery_required` now counts them — otherwise the doctor would report "nothing
to do" about the state this milestone exists to make visible.

The Runtime Doctor gains `effects.uncertain`, DEGRADED/HIGH when non-zero.
`startup_recovery()` reports them and **transitions nothing**: classification
happens when a caller actually asks again, so a boot cannot change the meaning
of a row nobody is asking about.

---

## 11. NATIVE / MCP / TEAM PARITY

M65C's unification is preserved and is a precondition for M65D's claim:
`test_there_is_still_exactly_one_effect_protocol` asserts two call sites of
`_execute_effect_protocol`, one `_durable_effect` and one
`_settle_observed_failure`.

Parity is asserted as a **comparison** rather than as two separate assertions:
`test_both_surfaces_report_the_same_semantics_for_the_same_window` runs the
identical window through both surfaces and compares the notes key by key.

Team execution reaches a tool only through `ToolBroker -> ToolExecutor.aexecute`,
so it inherits M65D rather than reimplementing it — proven end to end, including
four concurrent specialists after one ambiguous failure, all blocked, one
external effect.

---

## 12. PRODUCTION DURABILITY INVENTORY

Derived from `_TOOL_DURABILITY` and `durability_class()` on this branch, not
inherited from documentation.

```
reachable tool names  48   46 native handlers + 2 MCP allowlist entries
READ_ONLY             24   never journalled
IDEMPOTENT             1   set_clipboard
IDEMPOTENT_WITH_KEY    0
RECONCILABLE           0
NON_REPLAYABLE        23   every other reachable effectful tool, incl. all 15 HIGH_IMPACT
```

24 effectful tools, 23 of them `NON_REPLAYABLE` — unchanged from M65C, and
re-derived here rather than copied: `test_the_production_durability_inventory`
recomputes all five counts from `dir(ToolExecutor)` and `MCP_TOOL_ALLOWLIST`, so
a tool added without a classification moves the numbers and fails.

**Reclassifications: 0. Unproven reclassifications: 0.**

M65D is allowed to leave every production tool `NON_REPLAYABLE`, and does. A
milestone about truthful uncertainty must not pay for its own tests by declaring
a real tool replayable, so the audited table is pinned by
`test_no_production_tool_was_reclassified_by_this_milestone`, and
`test_no_production_tool_declares_a_reconciler` pins the empty reconciler
registry. Both protocols are proven against test-owned synthetic tools
registered through `register_durability`, which is the same entry point a real
tool would use.

---

## 12b. D57 — A DEFECT THIS MILESTONE'S OWN CLOSE EXPOSED

Not an effect-semantics defect. Worth recording because of how it was found.

S5G.1's F1 removed three tests that pinned
`observe_integration_state(...)[0] == "TARGET_AT_AUTHORIZED_BASE"` as a permanent
invariant, because an authorised fast-forward made all three fail and the only
cure was the exact-base rollback this programme forbids. Its replacement test
carried this line:

```python
assert authority["integration_base"] == HISTORICAL_MASTER
```

That is the same mistake one layer down. F1 unpinned the OBSERVATION and left a
pin on the generation-specific VALUE the observation is taken against — in a
test whose own docstring says it "says nothing at all about whether integration
has happened yet".

**Measured, not reasoned about.** Writing generation 36 with
`integration_base = 72e2948` — the master generation 35 was integrated onto,
which is the only honest base for a successor — turned it red:

```
E  AssertionError: assert '72e2948bbc4f...' == '3705114228ed...'
1 failed, 220 passed
```

**And then the scientific suite found four more.** The same pin, in four
modules that assert it against the LIVE snapshot:

```
FAILED test_training_gym_m62_s3n1_control_plane.py::test_the_snapshot_names_the_subject_commit_and_master
FAILED test_training_gym_m62_s4h_s4f_result_invariance.py::test_master_is_unchanged_and_nothing_was_merged_tagged_or_released
FAILED test_training_gym_m63_s4b_control_plane.py::test_the_project_block_did_not_move_master_or_merge
FAILED test_training_gym_m63_s4c_trained_state.py::test_the_project_block_did_not_move_master
4 failed, 3175 passed, 2 skipped
```

Five sites in total, each carrying a comment arguing that the integration base
is "a fact about the past that cannot go stale". It is not a fact about the
past. It is the master a generation authorises a fast-forward **from**, so it
advances every time a generation is integrated — and pinning it means the suite
goes red precisely when integration succeeds.

Declaring the stale base instead would have verified, and would have made the
observation read `INTEGRATED_FAST_FORWARD` — i.e. it would have claimed M65D was
already merged. The truthful declaration is the one the test refused.

All five now assert the property that is not generation-specific: **the
authorisation may advance and must never rewind.** A base behind an
already-integrated master would authorise a fast-forward onto a rollback.
`test_the_advance_only_rule_actually_bites` keeps that guard falsifiable, because
the assertion it replaced could only ever have failed by a successor doing the
right thing.

### A boundary this milestone measured and did NOT widen

Both closure checks diff `subject..HEAD`. An **uncommitted** change to an
authority-critical path is therefore invisible, and the verifier reads `PASS`
with modified test files sitting in the working tree — observed directly while
the five D57 sites were being repaired.

This is not widened here. It is controlled where it already was: `git status` is
step one of PROGRESS §0, and CI verifies a clean checkout, where the same change
is a commit and the closure does see it. Widening the checker to inspect the
working tree is a control-plane design decision, and making it inside an
effect-semantics milestone would be exactly the unscoped expansion the S5G.1
trust boundary exists to prevent. It is recorded in the `limitations` record
instead.

---

## 12c. RED TEAM ROUND 1 — ELEVEN FINDINGS, ONE OF THEM A BLOCKER

An independent read-only reviewer was told to distrust this document and these
tests, and to make one external effect happen twice. It did.

Every finding below was **reproduced by the main agent before any repair**, and
every repair carries a named regression test.

### BLOCKER — D1: the decision was right and was being asked about the wrong thing

`retry_authority` survived every direct attack. It was never consulted about the
action; it was consulted about an **identity hashed from the caller's dict as
given**, and the dict that reaches the handler is a different object —
`_strip_override` removes a model-supplied flag afterwards, and Python fills
defaults at call time.

So two spellings of one action were two identities:

```
{"ip": "10.0.0.1"}                           -> FAILED_OBSERVED_UNKNOWN, 1 effect
{"ip": "10.0.0.1"}                           -> BLOCKED_INDETERMINATE,   1 effect
{"ip": "10.0.0.1", "reason": "containment"}  -> FAILED_OBSERVED_UNKNOWN, 2 effects
                                                 (the DEFAULT, spelled out)

effective calls the handler ran: [('10.0.0.1','containment'), ('10.0.0.1','containment')]
```

Same epoch, same process, same turn, no operator, byte-identical effective
calls. `FORCE_OVERRIDE` — the flag JARVIS deletes *as a probable injection
attempt* — worked identically, surviving exactly long enough to be hashed.
Ten effectful production tools have defaulted parameters, `network_quarantine`
and `host_firewall_rule` among them.

**This is not the declared identity-scope limitation.** That one is about
epochs. This was inside one.

**Fix:** one canonical effective call, computed once by the surface —
`_strip_override` first, then the handler's own defaults bound — and used for
the ledger key, the effect id and the reservation. The gates no longer derive
their own; the key is carried on `_EffectHooks`. Normalisation is deliberately
conservative: anything that cannot be bound exactly is left untouched, because
a wrong merge would suppress a real effect, which is the worse direction.

### MAJOR

| | finding | fix |
|---|---|---|
| D2 | The ledger READ key (unstripped) and WRITE key (stripped) were computed independently, so a call that **succeeded** was journalled `FAILED_OBSERVED` / `UNKNOWN` with no receipt. Native and MCP also stripped at different points. | One carried key; both surfaces strip before the identity. |
| D3 | `committed` was read back from the in-process ledger, which only holds a `dict` — an ordinary MCP content list made a committed effect read `UNKNOWN` forever. | The gate **states** success on the hooks; the ledger's shape is a dedup detail, not evidence about the world. |
| D4 | No production caller ever passed `verdict`. A probe that had answered `UNKNOWN` five times still published `REQUIRES_RECONCILIATION` — "asked and unanswered is not unasked" was in the table and never reached the runtime. | The blocked branch calls `retry_authority` with the verdict instead of computing the field by hand. |
| D5 | `reconcile()`'s docstring said "bounded"; the call had no deadline. A probe that never returned hung the effect call forever and consumed an executor thread. | `RECONCILE_TIMEOUT_S`; a probe that has not answered has not answered. |
| D6 | The idempotency key was derived, stored, and handed to nothing — so an authorised `IDEMPOTENT_WITH_KEY` replay was an *undeduplicated* second effect. The class's whole safety argument is that the far side dedupes on it. | Opt-in delivery by signature: a handler declaring `idempotency_key` is given it, and the reserved argument is excluded from the identity it is derived from. |
| D7 | Only the *blocked* branch set `recovery_required`, so the specialist receipt and ARGUS never saw the single-attempt case — the common one. | The call that creates the uncertainty says so. |
| D8 | `_resolve_existing`'s `INDETERMINATE` branch read the durability class by hand and blocked every class — a **second retry policy**, contradicting the table, which leaves the one production `IDEMPOTENT` tool stuck forever after a cancellation. | That branch asks `retry_authority` too. |

### MINOR

* **D9** — the reader mapped an unparseable `external_outcome` to `UNKNOWN` and
  blocked; the *counter* compared the literal `'UNKNOWN'` and saw nothing, so
  the doctor reported "nothing to do". The reader failed closed and the counter
  failed open. Both now ask for "not one of the two certainties".
* **D10** — two branches left `external_outcome`/`retry_authority` `None`,
  contradicting "always present, so reading them is never a guess".
* **D11** — `assert_healthy()` existed and was never called on the effect path,
  so a corrupt-but-openable journal raised a raw `sqlite3.DatabaseError` past
  §25's refusal envelope.

### What survived the attack

Nothing in the decision path reads an exception type, a status code, a message
or a clock — nine hostile exceptions, a forged `effect_uncertainty` block on
both surfaces, a real v1 journal with hand-set columns, six racing subprocesses,
a real `SIGKILL` after the boundary, and direct hostile calls to `_take_over`
and `fail_observed` were all repelled. The reviewer's own mutation battery
against a copy of the tree found one surviving mutant — the journal-disabled
branch publishing `SAFE_TO_RETRY` unconditionally — which now has a test.

**The shape of this round is worth keeping.** The rule was sound and the layer
underneath it was not. A milestone that verifies its decision function and not
the identity it decides about has proved something true and irrelevant.

---

## 12d. THE MUTATION CAMPAIGN — 43/43, AND THE TWO THAT NEARLY GOT AWAY

**43 targeted mutations, 43 detected, 0 survivors.** Categories: effect identity
5 · retry authority 4 · state transitions 3 · outcome knowledge 4 · evidence 3 ·
idempotency 3 · old journal 3 · reconciliation 4 · executor settlement 3 ·
observability 2 · integration rule 2 · durability class 2 · cancellation 1 ·
native/MCP parity 1 · body-safety 1 · fail-closed 1 · failed-observed 2.

The first pass found **two survivors**, and both were worth the run.

### M39 — dead redundant code

The D57 live assertion and its own "does this guard bite?" companion asserted
the SAME property two ways, so deleting either was invisible. That is the M65C
`DEAD_REDUNDANT_CODE` shape exactly: a guard that cannot fail is not defence in
depth, it is a second place to keep correct.

Closed with **one** validator, `assert_advance_only`, exercised positively
against the live snapshot and **negatively** against a rewound base and a
fictional commit — both of which it must refuse.

### M37 — a safety regression the durable journal was hiding

Reverting a gate to derive its own ledger key instead of carrying the
protocol's. The effect count stayed at 1 and nothing went red, because the
durable journal caught the repeat that layer 1 had missed. It reads like a
layering nit.

It is not. With `JARVIS_EFFECT_JOURNAL=0` — an operator-supported configuration,
documented in §9 of this milestone as *removing the protection, not the
physics* — there is no second layer:

```
JARVIS_EFFECT_JOURNAL=0
  call 1: EXECUTED_NOW   runs=1
  call 2: EXECUTED_NOW   runs=2
  EXTERNAL EFFECTS: 2   (correct answer is 1)
```

Two tests pin it now: one requiring `DEDUPLICATED_IN_PROCESS` (layer 1 must be
the layer that answers a repeat), and one that runs with the journal off and
requires a single execution.

**The lesson is about masking, not about the mutation.** A defence-in-depth
stack makes a broken lower layer look healthy, and a campaign that only counts
external effects in the default configuration will report green. The question
that found it was "which layer answered?", not "how many effects happened?".

---

## 12e. RED TEAM ROUND 2 — THE REPAIRED TREE, BROKEN AGAIN

A second independent read-only reviewer was pointed at the frozen tree with one
instruction: falsify the repairs. It found one BLOCKER, seven MAJOR and two
MINOR. Every one was **reproduced by the main agent from the reviewer's own
scripts before repair**, and every repair carries a named regression test.

### BLOCKER — B1: D58's shape, with the free variable moved

D58 closed the *structural* identity split (strip, then bind defaults). B1 is
the *value* split: the real `_tool_http_request` handler upper-cases `method`
and treats `headers=None` as `{}`, so five spellings of one POST were five
identities, five POSTs on the wire, each individually "blocked".

The executor cannot know that a handler upper-cases a method. Only the handler
can. So a tool may now **declare its own equivalence** — `_TOOL_IDENTITY_NORMALISER`
— under exactly the rule the durability table already lives by: only with proof
from the handler's own code, because a WRONG normaliser suppresses a real second
effect, which is worse than the duplicate it prevents.

Three contracts are declared, each citing the line that proves it:
`http_request` (`method.upper()`, `headers or {}`), `project_note`
(`kind.strip().lower()`), `save_note` (`tags` `None` ≡ `[]`). `timeout` is
**not** normalised — a 1 s and a 30 s request are different requests from
JARVIS's side — and numeric spelling (`10.0` vs `10`) is not the executor's to
judge; M65C declined that on purpose.

**What "automatic" means here.** Twenty of the twenty-four effectful tools
require a human challenge per call, so a re-spelled request there is a second
request a human approved. The four HITL-exempt tools are the genuinely
automatic surface; two of them normalise a value, both are now declared, and
`test_every_hitl_exempt_normalisation_is_declared` pins that the set is closed.

### MAJOR

| | finding | fix |
|---|---|---|
| M1 | Only the chat turn opens an epoch. Containment, runbooks, the task graph, incidents and playbooks reach the protocol with scope `""` — **durable and unbounded** — so a legitimate quarantine of the same address on a later boot was answered "recovered" and never applied. A real containment, suppressed. | An undeclared epoch is scoped to the process: same-process repeats still dedupe, a restart is a new decision. |
| M2 | A slow reconciliation probe truthfully read "not executed" about attempt 1, returned inside the timeout, and was written against attempt 2 — whose effect had landed. One RECONCILABLE effect, two external effects. The timeout bounds *how long*, not *which attempt*. | `apply_reconciliation(expect_attempt=…)`; a row whose attempt has moved rejects the verdict as stale. |
| M3 | `host_firewall_rule` returned `{"blocked": False}` with no `error` key when nothing was applied; `_call_mcp` dropped `isError`. Both read as SUCCESS, were durably `PROVEN_COMMITTED`, and deduplicated every later attempt against an effect that never happened. | The handler says "not applied" with an error key; the MCP adapter is a module-level `mcp_result_envelope` that propagates `isError`, and is tested. |
| M4 | Vacuity: `effect_hooks.succeeded = True` in the MCP gate survived 917 tests. The only MCP-failure assertion was `mcp_count() == 1`, which a wrong COMMIT satisfies as well as the right UNKNOWN. | A test pins the state, not the count. |
| M5 | The idempotency-key injection *yielded* to a caller-supplied key — a model-authored argument choosing what the far side deduplicates on. | The protocol's key overwrites; a caller-supplied one is discarded. |
| M6 | A call that cannot bind (`{"cod": …}`) crossed EXECUTING, raised inside the handler, and was journalled as an UNKNOWN post-boundary failure needing a human — about a call that provably never ran. The identity helper knew; "preflight will refuse it" was untrue. | Bind-check before the boundary; refused as `invalid_arguments`, pre-effect. |
| M7 | `journal.commit()` was unguarded. A locked journal turned the owner's KNOWN success into a raw exception and a `disposition` of `None`; a concurrent classifier moving the row to INDETERMINATE made the late owner's commit an `InvalidTransition`. | Commit is guarded and the result returned with `journal_write_failed`; the `INDETERMINATE → COMMITTED` edge exists for an owner holding a receipt, CAS-protected against a takeover. |

### MINOR

* **m1** — one unparseable enum in one row raised past `reserve` with
  `BEGIN IMMEDIATE` open, wedging the process and locking the file for every
  other. Now `JournalUnhealthy`, transaction rolled back.
* **m2** — the blocked-branch message reported the *current* durability class
  after a runtime re-registration, not the row's — the one the journal decided
  on.

### What held

Six subprocesses on one journal in seven interleavings, SIGKILL after the
boundary under default and zero leases, registration flips between calls, the
merge direction of `_effective_call` (no two genuinely different calls
collapsed), every v1-journal row, and the verifier against a rewound base, an
edited sealed snapshot, a forged generation 37, and a governed-subject slide.

---

## 13. WHAT M65D DOES **NOT** CLAIM

```
POST_BOUNDARY_FAILURE_IS_TRUTHFUL              YES
NON_REPLAYABLE_AMBIGUOUS_RETRY                 BLOCKED
OLD_M65C_ROWS_SAFE_AFTER_UPGRADE               YES (more conservative)
NATIVE_MCP_TEAM_PARITY                         YES
UNIVERSAL_EXACTLY_ONCE                         NO   (unchanged from M65C)
```

* **It does not make an uncertain effect certain.** For 23 of 24 reachable
  effectful tools an ambiguous post-boundary failure ends in a truthful "I
  cannot tell" and an operator task. That is the correct result, not a gap.
* **It does not narrow the identity scope.** Durable dedupe still reaches
  exactly as far as the effect epoch (M65C §18.2), unchanged.
* **It does not authenticate the journal.** The digests are not keyed; a local
  writer who can edit the file can also make it self-consistent (M65C §13).
* **A tool declaring `IDEMPOTENT` is trusted on that declaration.** The class is
  a contract JARVIS cannot verify, which is why the default is
  `NON_REPLAYABLE` and why the audited table is pinned by a test.
