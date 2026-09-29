# JARVIS V69 — M68A · RUNTIME INTEGRITY HARDENING

**External audit revalidation.** Four priority findings, independently reproduced
against current `master`, repaired, and pinned by focused and absent-control tests.

| | |
|---|---|
| Branch | `jarvis-v69-m68a-runtime-integrity-hardening` |
| Base | `2c08607abc7429592de06570c4a2939c0eccb2af` (M67A, generation 62) |
| Audit base | `7125557f74cbf1756b1dc0072a1f3ca37c236cfa` (M66B, generation 58) |
| Scope | RUNTIME INTEGRITY ONLY. No science, no training, no evaluation, no corpus |
| Spends | **0** — `eval-v7` stays `USED_IMMUTABLE`; candidate006 and eval-v8 absent |

---

## 0 — Why the audit was revalidated rather than patched

The audit was static and was run against `7125557`, which was already two
integrated milestones behind (`3bd46fb` → `7125557` → `2c08607`). A report written
against an older tree is a hypothesis about the current one, so every finding was
reproduced against `2c08607` **before a line changed**. All four reproduced, two of
them as demonstrated fail-opens rather than as code smells.

Reproduction is recorded per finding below. Nothing here rests on "the code looks
wrong": each finding has either a dynamic reproducer that executed, or an AST
property over the shipped source.

---

## A — EFFECT JOURNAL INITIALISATION · CONFIRMED

### The defect

`ToolExecutor._effect_journal` published its readiness flag **before** doing the
work that readiness asserts:

```python
if self._journal_ready:          # (1) every later call short-circuits here
    return self._journal
self._journal_ready = True       # (2) READY published BEFORE construction
if not journal_enabled():
    self._journal = None
    return None
journal = DurableEffectJournal() # (3) may raise
journal.assert_healthy()         # (4) may raise
self._journal = journal          # (5) only now is the slot filled
```

If (3) or (4) raised, the executor was left holding `(_journal_ready=True,
_journal=None)` — which is **byte-for-byte the state that means "the operator
switched durability off"**. `_durable_effect` reads `journal is None` as exactly
that, and its disabled branch *executes the tool*.

So the first effectful call of the process refused correctly, and the second one
ran. An initialisation error became an operator decision by the act of asking twice.

### Reproduction (dynamic, on unmodified `2c08607`)

Two effectful calls through the real `_durable_effect`, with a journal whose
constructor raises `JournalUnhealthy`:

```
[pre-call#1] _journal_ready=False _journal=None
--- call #1 ---  error_class: journal_unhealthy  disposition: BLOCKED_INDETERMINATE
                 side effects so far: []
[pre-call#2] _journal_ready=True  _journal=None
--- call #2 ---  error_class: None               disposition: EXECUTED_NOW
                 side effects so far: ['EXECUTED']
```

Call #2 reports `EXECUTED_NOW` with no `journal_unhealthy` marker. The effect ran
because journal construction had failed.

### Root cause

Three distinguishable states — *not yet attempted*, *deliberately off*, *broken* —
were carried in one boolean plus a nullable slot. The collapse was not lossless.

### Fix

`JournalLifecycle`, a six-state machine in `tools/executor.py`, and an accessor
whose only route to `READY` runs after construction *and* verification return:

| State | Meaning | Effect path |
|---|---|---|
| `UNINITIALIZED` | never asked | attempts construction |
| `UNVERIFIED` | injected via the constructor, not yet checked | verifies on first use |
| `INITIALIZING` | construction/verification running now | **refuses** (re-entrant) |
| `READY` | constructed **and** verified | proceeds |
| `DISABLED` | operator set `JARVIS_EFFECT_JOURNAL=0` | in-process guarantees, disposition says so |
| `FAILED` | construction or verification raised | **refuses, permanently** |

`INITIALIZING` is published before the work and `READY` only after it. `FAILED`
re-raises for the life of the process — re-raising is what keeps it distinct from
`DISABLED`. The handler catches `BaseException`: a `CancelledError` part-way through
construction proves exactly as little as a sqlite error does.

An injected journal is `UNVERIFIED`, never `READY`. A constructor argument proves
somebody built it, which is not the claim that it is healthy — so there is no door
into `READY` that skips verification.

### Post-fix

```
[pre-call#1] _journal_state=UNINITIALIZED  -> refused, side effects []
[pre-call#2] _journal_state=FAILED         -> refused, side effects []
```

`DurableEffectJournal` is constructed **once**; the failure is sticky.

### Residual limitation

A journal that is healthy at first use and becomes corrupt later is still caught
per-operation by `assert_healthy`/`reserve` inside the journal, not by this state
machine — `READY` is not re-verified per call. That is unchanged from M65D and is
deliberate: re-probing the database on every effect would trade a real latency cost
for a window the journal's own fail-closed `reserve` already covers.

---

## B — TOOL BUDGET / EMPTY ELIGIBILITY · CONFIRMED

### The defect

`core/llm.py` ended the eligible-name set with `or None`:

```python
_eligible_names = {
    t.get("function", {}).get("name")
    for t in _turn_tools if isinstance(t, dict)
} or None
```

and `core/tool_loop.py` read `None` as *unrestricted*:

```python
if eligible_names is not None and nm not in set(eligible_names):
    return False, {}, "tool_not_eligible"
```

`_turn_tools` is set to `[]` at the top of the loop the moment
`_tool_budget.force_final()` is true — the round budget being spent, i.e. the moment
the loop has decided that **nothing more may run**. An empty set is falsy, so it
became `None`, so the eligibility check was skipped entirely.

**Exhaustion was normalised into omnipotence.**

### Reproduction (on unmodified `2c08607`)

```
B.3 _turn_tools=[] (round budget spent) -> _eligible_names=None
B.4 composed: an EXHAUSTED turn validates run_shell_command -> ok=True reason=''
```

### Root cause

A falsy-collapsing `or None` at the producer, and a sentinel meaning *no constraint*
at the consumer. Either alone is survivable; together they invert the control.

### Fix — both layers, so neither masks a regression in the other

1. `eligible_tool_names(turn_tools) -> frozenset[str]` is **total** and never
   returns `None`. An empty input yields an empty frozenset, which is a real answer.
2. `validate_tool_call` refuses when the eligible set is empty *or* absent, with a
   distinct reason `no_eligible_tools`. There is no longer any value of that
   parameter that grants a wildcard.
3. `core/llm.py` counts `no_eligible_tools` as a **denial**, not a malformed call,
   so it cannot consume the repair budget.

### Also verified, as the audit asked

| Control | Status |
|---|---|
| Round budget termination | **Present.** `force_final()` at `rounds >= max_rounds`, and it stays spent |
| Malformed-response accounting | **Present and bounded.** `max_repairs=2`, then `note_malformed()` returns False |
| Tool calls **per response** | **WAS ABSENT — added.** See below |
| Byte ceilings | **Present.** `_TOOL_RESULT_MAX_CHARS` truncates the tool-result envelope and marks `truncated` |

**The per-response gap.** The round budget bounded how many times the model is
*asked*, and nothing bounded how much it could ask for each time:
`for tc in tool_calls_list:` iterated whatever arrived. One response carrying fifty
tool calls executed fifty tools inside a single "round", so the documented bound of
four rounds described a quantity that was not what it sounded like.
`ToolLoopBudget.admit_response_calls` now caps it at `_MAX_CALLS_PER_RESPONSE = 8`,
applied **before** the assistant turn reaches history so the `tool_call`/`tool`
pairing stays coherent — a call the model is never shown cannot be left without a
result.

### Residual limitation

The ceiling of 8 is a policy number, not a derived one. It is generous enough for
genuine parallel tool use and finite, which is the property that was missing; it is
not claimed to be optimal.

---

## C — INVALID DECISION != RESOLVED · CONFIRMED

### The defect

`JarvisLLM.decide_next_action` ended with:

```python
return {"tool": "RESOLVED",
        "input": {},
        "reasoning": f"LLM response not parseable: {raw[:100]}"}
```

`"RESOLVED"` is the sentinel `run_agentic_incident` reads to broadcast
`agentic_resolved` and stop. So a truncated stream, an Ollama error page rendered as
prose, or a bare apology from the model **closed a live security incident**, and the
event log recorded it as *resolved* rather than as a reasoning failure.

There was also no shape validation at all on the success path: a parsed list, or a
dict with no `tool` key, was returned from a function annotated `-> dict` and
dispatched.

### Reproduction (on unmodified `2c08607`)

```
C.2 parse failure maps to RESOLVED: True
C.3 any schema/contract validation of a PARSED decision: False
```

### Fix

A new module, `core/agentic_decision.py`, carrying the closed status set the audit
required plus the dispatch case:

```
ACT != RESOLVED != INVALID_DECISION != MODEL_ERROR != UNKNOWN
```

* `RESOLVED` is a **claim about the world** and is reachable only from a well-formed
  decision naming the `RESOLVED` sentinel.
* `INVALID_DECISION` — a reply arrived and is not a decision.
* `MODEL_ERROR` — no usable reply arrived; says nothing about the incident.
* `UNKNOWN` — nothing was produced and we will not guess which.

`validate_decision()` is total and fail-closed over arbitrary input, and runs
**before dispatch**. Dispatch is gated on `Decision.may_dispatch`, which is
positively `status is ACT and bool(tool)` — deliberately *not* `status != RESOLVED`,
because that formulation is what let a failure state reach a tool call.

`honour_status=False` for anything a model produced: the envelope status is ours. A
model that could set it could assert `RESOLVED` without the sentinel, which is the
same defect wearing a different field name.

The SOC tool list is defined once, as `SOC_ADVERTISED_TOOLS`. The system prompt is
rendered from it and the validator checks against it, so a tool cannot be offered to
the model without being dispatchable, or validated without being offered.

In the loop, an unusable decision is **not terminal**: it is fed back once as an
observation, under a budget of `_MAX_UNUSABLE_DECISIONS = 3`, after which the run
ends as `REASONING_UNUSABLE`. A malformed reply must not end an incident either way.

### Residual limitation

Validation is structural. A **well-formed** decision that is strategically wrong —
`RESOLVED` on an incident that is not contained — is indistinguishable from a
correct one at this layer, and always will be: the model's judgement is not
checkable by a schema. What M68A guarantees is that the *machinery* can no longer
manufacture that claim on the model's behalf.

---

## D — PER-RUN CANCELLATION / DEADLINE · CONFIRMED (every sub-claim)

### The defects

`core/cancel_bus.py` held one module-global `asyncio.Event` per *kind*, and
`run_agentic_incident` registered under the literal string `"agentic_loop"`.

| Required property | Status on `2c08607` |
|---|---|
| cancellation identity is per execution | **Violated.** One event, one registry key, shared by every run |
| one run cannot clear another's state | **Violated twice.** See below |
| absolute deadline propagates through the run | **Violated.** Compared only between cycles |
| terminal state recorded exactly once | **Violated.** Zero on cancellation, two on timeout |
| uncertainty preserved after an effect boundary | **Violated.** Cancelled effects logged as `{"error": ...}` |
| approval cannot cause an unbounded headless hang | **Violated.** `input()` with no timeout and no tty check |

**The revocation.** Lines 61–63 cleared the shared event at start-up:

```python
register_operation("agentic_loop")
if _cancel_bus.agentic_loop_cancel is not None:
    _cancel_bus.agentic_loop_cancel.clear()
```

An operator cancels incident A; incident B starts and clears the flag; A's next
cycle check reads `is_set() == False` and **carries on invoking
`run_shell_command`**. Reproduced:

```
D.2a operator cancelled run A -> is_set()=True
D.2b run B started            -> run A cancellation is_set()=False
D.3  after run B's finally, active ops = {}   <-- A is gone while still running
```

### Fix

`ExecutionHandle` — frozen, one per execution, with its own token, its own event and
an **absolute** deadline. A frozen handle matters: a reassignable token is a shared
identity with extra steps.

Cross-run revocation is removed rather than guarded. A monotonic
`_cancel_generation` counter is bumped by `cancel_all()`; a handle records the value
it saw at registration and is cancelled by any *later* abort. Consequences:

* no run clears anything, so no run can revoke another's cancellation;
* an abort fired **before** a run existed does not reach it, so a fresh execution
  starts clean **without needing a clear** — which is what made the old clear
  necessary, and therefore dangerous;
* `reset_all()` deliberately does **not** rewind the counter. A reset is an operator
  saying "ready for new work", not "the abort I just fired never happened".

`unregister_execution` pops by **token**, so whichever incident finishes first no
longer deregisters the ones still running. `get_active_operations()` aggregates
executions **by kind**, reporting the longest-running: callers ask by kind
(`"agentic_loop" not in ops` in `hunt_scheduler`), and answering per token would
have silently told the hunt scheduler that no incident was running.
`get_active_executions()` is the per-token view; they are different questions.

**The deadline now propagates.** Every long await in the run is wrapped in
`_bounded(...)` against `handle.remaining()`: the reasoning call (also capped at
30 s, tighter wins), the NATO approval, and the tool execution. A non-positive
budget refuses **without starting the call** — starting it is what the deadline
exists to prevent.

**Terminal state, exactly once.** One `agentic_terminal` event on every exit path,
including `asyncio.CancelledError`, which previously recorded nothing at all. The
latch is set **before** the broadcast, so a broadcast that raises cannot permit a
second record. Outcomes: `RESOLVED`, `CANCELLED`, `DEADLINE_EXCEEDED`,
`CYCLE_BUDGET_EXHAUSTED`, `REASONING_UNUSABLE`, `APPROVAL_UNAVAILABLE`, `FAILED`.
`cycles_run` now counts cycles that actually ran — a run aborted before doing
anything used to report one completed cycle. The pre-existing HUD event types
(`agentic_resolved`, `agentic_loop_timeout`, `agentic_aborted`, `agentic_summary`,
`agentic_cycle`, `agentic_loop_start`) are all preserved.

**Uncertainty survives the boundary.** A tool cut off by the deadline or cancelled
in flight is recorded with `external_outcome: UNKNOWN`,
`disposition: BLOCKED_INDETERMINATE` and `retry_authority: BLOCKED_INDETERMINATE` —
never as `{"error": ...}`, which reads as *it did not happen*, a claim about the
external world that nothing observed. This matches what the durable journal records
independently in `_settle_cancelled` (§41).

**The headless hang.** The NATO keyboard fallback was
`await loop.run_in_executor(None, lambda: input(...))` — no timeout. On a
non-interactive stdin that never closes (a systemd unit, a container holding stdin
open, a CI runner) `input()` blocks forever, and the loop consulted its deadline only
between cycles.

The fix **refuses** rather than walking away. `_approval_input_is_interactive()` is
checked before the thread is ever started, and denies fail-closed when nobody could
answer. Wrapping the call in `asyncio.wait_for` was rejected deliberately, and the
audit brief names why: it would return control while the executor thread stayed
blocked inside `input()`, holding the default executor and still owning stdin, so the
**next** approval prompt would consume the keystroke this one was waiting for. An
abandoned thread that owns stdin is a worse failure than the hang it replaces.

### Residual limitations

1. **An operator sitting at a real TTY who walks away still blocks that approval
   indefinitely.** `_challenge`'s own `input()` has no timeout; what bounds it is the
   run's deadline in `run_agentic_incident`, which now terminates the run as
   `APPROVAL_UNAVAILABLE`. The abandoned-thread problem above is the reason the
   inner call is not independently bounded, and the thread does remain blocked until
   a keystroke arrives. This is a deliberate trade, not an oversight.
2. **`_cancel_generation` is process-global**, so a global abort cancels every live
   execution of every kind. That is the intended operator semantic
   ("abort everything"), but it means there is still no way to abort exactly one
   incident by name from the voice/HUD path — `cancel_execution(token)` exists and is
   tested, and no caller is wired to it yet.
3. Cancelling `aexecute` mid-flight is now reachable from the deadline where it
   previously was not. The journal settles it truthfully as indeterminate, so the
   uncertainty is recorded in two independent places; it does **not** make the
   external effect un-happen.

---

## Scope discipline — what M68A deliberately did NOT touch

Confirmed present in the audit and **deferred**, none of them required by the four
blockers:

| Deferred finding | Why it is not here |
|---|---|
| AURA backpressure | Transport concern, no runtime-integrity blocker |
| `code_intel` retention / worker limits | Resource policy, not a fail-open |
| Output streaming / buffer bounding | Overlaps the M58.7 generation budget; needs its own measurement |
| Ollama endpoint configuration | Deployment posture |
| Docker persistence | Infrastructure |
| `write_file` atomic patch semantics | SWE patch engine — explicitly out of scope |
| `git diff` truncation | SWE patch engine |
| Diff applicability validation | SWE patch engine |
| Dependency locking | Release/supply-chain track |
| Benchmark integrations | Science track; no spend is authorised here |

Runtime integrity and the SWE patch engine are not mixed in one milestone.

---

## Verification

Every gate below was run from the repository root on CPython 3.11.16 in the
CI-parity environment, which is the invocation `ci.yml`'s `tests` job uses.

| Gate | Result |
|---|---|
| Reproducers on unmodified `2c08607` | 4/4 findings reproduced |
| Focused regression tests | `test_runtime_integrity_v69_m68a.py` |
| Absent-control tests | `test_runtime_integrity_absent_controls_m68a.py` |
| Falsification campaign | each control reverted individually; see below |
| Authoritative suite | `python -m pytest -q --tb=short jarvis/tests tests` |
| Scientific suite | unchanged |
| `ruff check .` | clean |
| `bandit` (medium/high) | clean |
| `git diff --check` | clean |
| Control Plane V4 | PASS / PROBLEMS: 0 |

### The absent-control tests are the point

M66B ran 131 mutations with 0 survivors; M66A.1 ran 75 with 0. **All four of these
defects survived every one of them**, because a mutation campaign can only break a
line that exists. It cannot find the state that was never representable, the
eligibility check that was skipped rather than failed, or the terminal event a
cancelled run never emitted.

So the second suite asks *"what required control could be completely missing?"* and
checks properties over the AST of the shipped source, generalised past the four
specific findings:

* **no module in `core/` may end a comprehension with `or None`** — the general form
  of §B, and it applies to modules written after M68A;
* **no `except` handler anywhere in `core/` may manufacture the `RESOLVED`
  sentinel** — the general form of §C;
* **every `JournalLifecycle` state, every `DecisionStatus` and every
  `TerminalOutcome` must have a producer** — the M67A lesson, where a reason code
  sat in a closed enum with nothing able to emit it;
* **`READY` must be assigned only after verification**, checked over statement order;
* **every long await in the run must be deadline-bounded**, checked by walking
  `ast.Await` nodes.

Three of these probes were written against raw text first and **passed on this
module's own prose describing the bug**. They are AST-based now, and that failure is
recorded here because it is the same class of mistake as the defects themselves:
a check that cannot fail is a control that is not there.

### Two defects M68A found in its own work

Recorded because both are the same class as the four findings, and because the
second one was found by the gate rather than by reading.

**1. Three absent-control probes passed on this document's prose.** They searched
raw text for a name that must be absent from the *code* — and the paragraphs
explaining the bug contain that name. Rewritten over the AST. A probe that
matches its own explanation cannot fail.

**2. The new `bus` fixture leaked an initialised cancel bus.** It called
`cancel_bus.initialize()` and restored `_cancel_generation`, `_executions` and
`_active_operations` — but not the four `asyncio.Event` globals or `_loop`. The
bus normally starts *uninitialised*, and `TTS._teardown()` sets `tts_cancel` only
when it is not `None`; so leaving the bus initialised made that teardown live for
every later test, and the next TTS worker saw a flag a previous instance had set
and **drained its first utterance instead of speaking it**.

It surfaced in `test_tts_shutdown.py`, as
`test_stop_leaves_no_nondaemon_worker_even_when_utterance_wedged` failing with
"worker never started speaking" — in the full suite only, some 700 tests later,
and never in isolation. The temptation was to
call it load flakiness on a 15 W CPU-bound host, and the assertion it trips is
indeed a 2-second scheduling window. It was not flakiness: removing only the
restore reproduces it in five seconds, and putting it back fixes it.

`test_barge_in_v69_m575._isolated_cancel_bus` had already documented this exact
hazard, in a docstring that describes the failure mode sentence for sentence. The
lesson is not "restore your globals" — it is that the repository had written the
warning down and a new fixture did not read it. `_BUS_GLOBALS` is now shared
vocabulary between the two modules.
