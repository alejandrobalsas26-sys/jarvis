# V69 M65B — Team Execution Fabric

**Branch** `jarvis-v69-m65b-team-execution-fabric`
**Source** `bde332d6ea0d27876c3e8d9a64675b00f7e32e57` (V69 M65A)

M65A made one specialist real: a typed request, authority recomputed from the
registry, a chosen model role, tool intents through `ToolBroker` →
`ToolExecutor.aexecute`, a typed result. It stopped there deliberately and said
so — *"no concept of a second specialist, no queue, no DAG and no delegation"* —
and listed the rest as M65B's.

M65B builds that, and nothing else. It is a controlled execution fabric, not an
agent swarm.

---

## 1. What became live

```
    OPERATOR
      |
    JARVIS                 one operator-facing assistant
      |
    ROUTER                 mesh_router.route_task — pure, deterministic
      |
    ROUTE CHOICE           mesh_live.team_route → DIRECT / ONE_SPECIALIST / TEAM
      |
    TEAM PLANNER           mesh_live.build_team_plan
      |
    PLAN VALIDATOR         specialist_team.validate_plan — before ANY task
      |
    DAG SCHEDULER          TeamOrchestrator — bounded parallelism, conflicts
      |
    SPECIALIST             specialist_execution.SpecialistExecutor — unchanged
      |
    CAPABILITY/SCOPE       SpecialistRecord + AuthorizedSecurityScope — unchanged
      |
    AUTHORITY              AutonomyLevel + HitlApproval — unchanged
      |
    TOOL EXECUTOR          tools.executor.ToolExecutor.aexecute — one fix, §6
      |
    EFFECT LEDGER          that executor's own ledger — one fix, §6
      |
    TEAM ARGUS             specialist_team.verify_team — NEW, verifies the PLAN
      |
    JARVIS
      |
    OPERATOR
```

Measured on a real turn through `LLM.chat_stream`:

> *"Please analyse this Sysmon alert and correlate it with our previous
> incidents and threat intel"* routes `team_verified` with **GUARDIAN** primary.
> **TRACE** and **ORACLE** execute **concurrently**; **ARGUS** joins after both
> and reasons over their results. Three specialists, one turn, one answer,
> **0 skips**.

One new module. Everything that decides anything is reused:

| Module | Contents |
|---|---|
| `core/specialist_team.py` | `SpecialistTeamPlan` / `Task` / `Result`, `validate_plan`, `TaskState`, `TeamStatus`, `ResourceClaim`, `ResourceArbiter`, `CancellationToken`, `TeamAdmissionController`, `BackendLimiter`, `DelegationProposal` + guard, `TeamOrchestrator`, `verify_team`, `TeamCounters` |

Edited: `core/mesh_live.py` (the TEAM route), `core/llm.py` (the call site),
`core/runtime_doctor.py` (a check), `tools/executor.py` (§6).

There is **no** second registry, broker, ledger, verifier, executor or planner,
and no `subprocess`, `socket` or raw-handler path — asserted over the parsed AST,
so this module's own prose cannot satisfy the test that proves it.

### A component this is NOT built on

`specialist_runtime.SpecialistTeamRuntime` already exists: a legacy team
executive with semaphore-bounded fan-out, a shared blackboard and a critic. It
predates the mesh. It has no DAG, no authority recomputation, no effect identity
and no conflict control, so it is not M65B's ancestor and is not touched. M65B
builds on `SpecialistExecutor`. No `V2` of anything was created.

---

## 2. The team contract

`SpecialistTeamPlan` carries `plan_id`, `turn_id`, `objective`, `tasks`,
`scope`, `authority_ceiling`, `execution_budget`, `timeout_budget_s`,
`retry_budget`, `max_parallelism`, `delegation_depth`, `completion_policy`,
`verification_policy`, `effect_epoch`, and derives `dependency_graph`.

`SpecialistTeamTask` carries `task_id`, `specialist_id`, `objective`,
`capability`, `dependencies`, `dependency_policy`, `autonomy`, `scope`,
`model_role`, `allowed_tools`, `resource_claims`, `effect_class`, `timeout_s`,
`retry_limit`, `evidence_requirements`, `activity`, `depth`, `parent_task_id`.

`SpecialistTeamResult` carries `status`, `task_results`, `validation`,
`verification`, `delegations`, `budget_usage`, `body_safe_trace`,
`cancelled_reason`.

**The plan is not the source of truth for authority.** A task's `autonomy` and
`allowed_tools` say what the *plan* permits. `TeamOrchestrator` takes
`min(task, plan)`; `SpecialistExecutor` then takes the minimum of *that* and the
registry. Three narrowings on the path and no widening anywhere on it.

`completed`, `failed`, `denied`, `skipped`, `cancelled`, `specialists_executed`,
`evidence`, `receipts`, `executed_effects`, `deduplicated_effects` and
`parallel_overlaps` are **derived properties, never stored** — a team cannot
report zero failures by saying so, and `specialists_executed` counts only nodes
that actually produced an execution, so a SKIPPED task cannot pad the roster.

**Why a task carries its own `scope`.** A delegated child is bounded by its
*parent*, and a parent is frequently narrower than the team. Without a per-task
scope, "child scope ⊆ parent scope" could only be checked against the team
ceiling — which is not the property §20 asks for.

### Plan identity (§48)

`canonical_identity()` hashes what the plan **executes**: objective digest,
scope, ceiling, policies, parallelism, delegation depth, and each task's
executable fields. It excludes `created_at`, `plan_id`, `turn_id` and every prose
field, so replanning the same work yields the same identity and a change to what
actually runs yields a different one. Useful for binding an approval, replay
protection, audit and recovery.

---

## 3. Validation happens before execution, and collects everything

`validate_plan` returns a `PlanValidation` whose `valid` is **derived from
`defects`** — a validator cannot report a clean plan by setting a flag.

Rejected: empty plan · too many tasks · duplicate task id · missing dependency ·
self-dependency · **cycle** · unknown specialist · unknown model role · illegal
(L4) autonomy · autonomy above the team ceiling · capability mismatch · scope
expansion · unregistered tool · budget overflow · delegation depth overflow ·
unknown claim kind · effect-class mismatch.

It collects **every** defect rather than the first: a planner told about one
problem at a time fixes them one at a time, and since no partially valid DAG may
begin execution there is no benefit in stopping early.

Cycle detection is an **iterative** DFS with an explicit stack. Recursive would
be shorter and would also mean a malformed plan could exhaust the interpreter's
stack before the validator got to reject it.

A rejected plan returns `TeamStatus.INVALID` with **no task results**, and a test
asserts the scripted model was never consulted at all.

---

## 4. DAG semantics, and the state vocabulary

Nine states: `PENDING`, `READY`, `RUNNING`, `SUCCESS`, `FAILED`, `DENIED`,
`TIMED_OUT`, `CANCELLED`, `SKIPPED`. No member means "probably fine". Every
terminal state names a different thing a human would do about it: FAILED is a bug
or a bad backend, DENIED is policy working, TIMED_OUT is a budget, CANCELLED is
the operator, SKIPPED is a dependency that did not hold.

A node becomes runnable only when its dependencies satisfy its declared policy,
the team is not cancelled, budget remains, and its resource claims can be
reserved. `_dependencies_satisfied` returns **three** answers — yes / never / not
yet — because a two-valued version cannot tell a dependency *wait* from a *skip*.

`ALL_SUCCESS` is the default and the minimum. `ALL_TERMINAL` exists for the
genuinely different case: a summariser or an independent verifier whose job is to
report what happened, **including that something failed**. It is used by the live
team's ARGUS join, and requiring every dependency to SUCCEED there would silence
the verifier exactly when it matters.

**Failure propagation.** `A → B`, `C` independent. A fails, B requires
ALL_SUCCESS, so B is SKIPPED and ran no specialist at all; C still runs and
succeeds; the team is `PARTIAL_SUCCESS`. A single branch failing does not destroy
an independent useful result.

**SKIPPED beats CANCELLED when both apply**, and the ordering in the scheduler
loop is deliberate rather than accidental. A task whose dependency failed was
already unrunnable before any stop arrived, so reporting it as CANCELLED would
imply the cancellation is what prevented it. Skip propagation therefore runs
*before* cancellation, and CANCELLED is reserved for work the stop actually
prevented.

Four team outcomes: `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`, `CANCELLED` (plus
`INVALID` for a plan that never started). `derive_team_status` is a **free
function**, so team ARGUS recomputes it independently and compares rather than
reading the status the orchestrator claimed.

---

## 5. Parallelism is proven, not claimed

Two tasks appearing in one `asyncio.gather` proves nothing. Every parallelism
test uses a **deadlock fixture**: task A's model call blocks on an
`asyncio.Event` that only task B's model call can set. If the scheduler runs them
one after the other, A waits to its deadline and the test **fails**. The
assertion is therefore that the team succeeded at all — which is only possible if
both specialists were genuinely in flight at the same time.

The same fixture inverted proves the bounds: four tasks against a ceiling of
three, asserting the observed peak concurrency; and two WRITE claims on one
resource recording entry and exit, asserting the observed peak was one.

`parallel_overlaps` is computed from recorded start/finish sequence numbers, so
it is a **measurement of what the scheduler did**, not a claim about what it was
asked to do.

### Bounds (§12)

| | |
|---|---|
| `MAX_PLAN_TASKS` | 8 |
| `MAX_PARALLEL_SPECIALISTS` | 3 |
| `MAX_PARALLEL_EFFECTFUL` | 1 |
| `MAX_DELEGATION_DEPTH` | 1 |
| `MAX_DELEGATION_PROPOSALS` | 2 |
| `MAX_ACTIVE_TEAMS` / `MAX_QUEUED_TEAMS` | 2 / 2 |
| `MAX_BACKEND_CONCURRENCY` | 2 |
| `MAX_TEAM_RETRIES` / `MAX_TASK_RETRIES` | 3 / 1 |
| `MAX_TEAM_TIMEOUT_S` / `MAX_TASK_TIMEOUT_S` | 180 / 60 |
| live: `MAX_LIVE_TEAM_TASKS` / `MAX_LIVE_TEAM_SUPPORT` | 3 / 2 |
| live: `LIVE_TEAM_DEADLINE_S` / per task | 30 / 20 |

Effectful concurrency is strictly tighter than read-only: concurrency on
observation costs latency, concurrency on effects costs the world.

A plan cannot buy itself more than the module allows — `__post_init__` clamps
`max_parallelism`, `delegation_depth`, `retry_budget` and `timeout_budget_s`, and
a task's `timeout_s` and `retry_limit`, exactly as `SpecialistExecutionRequest`
clamps its deadline.

**Per-backend concurrency**, not per-role. Several roles legitimately resolve to
one backend on this host, so bounding roles would not bound the machine. The
orchestrator resolves the backend through the **same** `ModelRoleRouter` the
executor will use — it is pure and deterministic, so asking it twice cannot
produce two answers. Queueing for a backend changes *when* a specialist reasons
and nothing else: authority is recomputed inside the executor, after the limiter
has already let go.

---

## 6. Exactly-once, and the defect M65B had to fix first

`ToolExecutor.aexecute` remains the one effect path, and its ledger remains the
one ledger. But M65A's exactly-once proofs were all **sequential**, and that is
all M65A could reach — it had one specialist.

The ledger was a **check-then-act**: read near the top of the gate, written only
after the handler returns, with the HITL challenge, an AURA broadcast and
`run_in_executor` awaiting in between. Two specialists submitting the same effect
identity at the same time therefore both saw an empty ledger and both ran. A team
makes that reachable.

M65B closes it **in the ledger, not in the scheduler**. A scheduler-level lock
would protect only effects the scheduler routed — which is the same reason the
ledger lives in `ToolExecutor` rather than in a caller.

The mechanism is a reservation. The first caller for an effect identity publishes
a future *before it awaits anything*; later callers await that future instead of
the handler. What they receive is read back from **the ledger**, so a refused,
failed or cancelled effect publishes `None` and a waiter legitimately runs — a
failed call left the world unchanged, and refusing to repeat it would be the
ledger inventing a policy it does not have. Read-only calls are never reserved,
for the same reason they are never keyed.

The duplicate ledger *read* inside the gate went with it. `aexecute` performs the
identical check with no `await` in between, so by the time the inner one ran the
answer was already known — and a guard that cannot fail is not defence in depth,
it is a second place to have to keep correct. **One reader, one writer, one
identity.**

Proven, all measured from the executor's own ledger: two concurrent identical
submissions → one effect, and the suppressed caller receives the *recorded
result* rather than an error · four concurrent → one effect · two different
specialists in one team proposing one effect → one effect, one deduplication ·
a refused gate releases its reservation so the real call still runs · a failed
effect is never ledgered · a new epoch is never blocked · argument order cannot
manufacture a second identity.

**Retries are two-layered and both are bounded**: `SpecialistExecutor` retries
the *inference* up to `MAX_ATTEMPTS`, and the team retries the *task* up to
`retry_limit` within `retry_budget`. A retry re-enters the same objective and
therefore the same effect identity, so the ledger — not either counter — is what
holds the effect count at one. That separation is deliberate: a retry policy that
also had to be the deduplication policy would eventually get one of the two
wrong.

**Crash recovery.** The §18 shape is tested directly: the effect commits, the
worker dies before delivering its result, the orchestrator retries, and the
recovered receipt is marked `deduplicated` with the effect count still at one.
**Limitation, stated precisely:** this is *in-process* recovery. The effect
ledger is an in-memory dict on the live `ToolExecutor` with a 180-second TTL and
a per-turn epoch. It survives a worker failure and a task retry inside one
process; it does not survive a process restart, because the repository has no
durable effect journal and M65B did not invent one.

---

## 7. Resource claims and the conflict scheduler

A claim is `kind:identity` plus a mode. Kinds: `file`, `service`, `process`,
`container`, `host`, `network-target`, `world-state`, `memory`. An unknown kind
is a *validation error*, not an unscheduled free-for-all.

**Claims support scheduling. Claims grant nothing.** A claim is read by the
arbiter to decide ordering; every gate downstream is unaware it exists.

### The policy, stated exactly

| | |
|---|---|
| READ + READ on one resource | **PARALLEL** |
| READ + WRITE on one resource | **SERIALIZE** |
| WRITE + WRITE on one resource | **SERIALIZE** |
| any pair on different resources | PARALLEL |
| the same canonical effect identity | `ToolExecutor` exactly-once governs |

**SERIALIZE, not DENY.** A write/write pair is a scheduling problem, not a policy
violation: both tasks were separately authorised, and refusing the second would
make a legal plan fail for a reason its planner cannot see. Denial stays where
denial belongs — capability, scope and autonomy.

### Canonical identity (§16)

`./foo`, `foo` and `/base/foo` must be **one** write lock, or two tasks mutating
one file would be scheduled in parallel because they spelled it differently.
Normalisation is therefore lexical and total: separator collapse, `.`/`..`
resolution, and a caller-supplied base for relative paths.

It is also **lexical only**. No `realpath`, no `stat`, no symlink resolution.
Normalising a name must not touch the filesystem — both because the scheduler
runs before any authority check, and because resolving a path is exactly the
operation an attacker would want a pre-authorisation component to perform on
their behalf. A test creates a real symlink and asserts the link and its target
canonicalise **differently**.

### No lock, no effect (§15)

The reservation is taken **before** the execution starts and released only when
the task is terminal, so there is no window in which a policy check has passed,
the world has moved underneath it and the effect then runs. Reservation is
**all-or-nothing**: a partial reservation is a deadlock generator, so a blocked
task takes nothing at all and a test asserts the arbiter still holds exactly one
task's claims after a refusal.

---

## 8. Delegation

A specialist may emit a `DelegationProposal`. It may **not** instantiate another
specialist — there is no `spawn` and no `delegate` method on any specialist, and
a test asserts that over the parsed AST. The only place a task is constructed
from a proposal is the orchestrator's guard.

The grammar has **no key for autonomy, scope, capability, approval, depth or
verification** — exactly as `ToolIntent` has no field for them. A specialist has
nowhere to put the claim, which is what makes "delegation cannot escalate"
structural rather than policed. Unknown keys are dropped and reported.

The scanner is **brace-balanced and string-aware**, for the measured M65A reason:
a non-greedy `\{.*?\}` closes on the inner brace of any object carrying a nested
value, so every real proposal would parse as malformed and be silently dropped.

### The guard (§20)

The child's authority is **computed**, never taken from the proposal:

* `autonomy = min(parent's effective ceiling, the plan's ceiling)`
* `scope = the parent's scope`, checked against the **parent** and the plan
* `tools ⊆ the parent's`, and naming anything outside is a **refusal**

That last one is deliberate. A silent intersection would be equally safe and
completely untestable: a mutation removing the check would produce identical
behaviour, so the control would look present while proving nothing.

Also refused, each with its own code so "denied" is an operable answer: depth
above the ceiling, unknown specialist, a handoff the registry forbids, exhausted
budget, a full plan, and a child that would make the plan fail re-validation. The
successor plan is re-validated **in full** — two independent checks, and the
cheaper one is not allowed to be the only one.

**Depth.** 0 is a task JARVIS planned; 1 is one orchestrator-approved delegation;
there is no 2. A test drives a live run in which a delegated child itself
proposes a delegation, and asserts the observed depths are exactly `{0, 1}`.

---

## 9. Cancellation

`CancellationToken` is **one-way**: a token that could be un-cancelled would let a
race resurrect a team the operator stopped.

Once cancelled, no new task starts; PENDING and READY become CANCELLED; running
tasks receive the token, which `SpecialistExecutor` already checks between its
steps and before each tool intent. Already-committed effects **remain**, and
nothing claims a rollback that did not happen.

* **Cancel before the effect** → effect count 0, measured from the ledger.
* **Cancel after commit** → effect count 1, the receipt preserved and marked
  executed, the dependent task CANCELLED, the team `PARTIAL_SUCCESS`.

**Effectful cancellation paths (§27).** M65B introduces none and reaches none.
Its cancellation is `asyncio.Task.cancel` plus a cooperative token — process
state inside this interpreter. It performs no `os.kill`, no process or service
termination, no container stop and no filesystem mutation, and a test asserts the
module imports none of `subprocess`, `socket`, `os`, `shutil`, `requests` or
`httpx`. **Internal asyncio cancellation is not an external effect**, and the
distinction matters: an external effect would have to go through
`ToolExecutor.aexecute` like every other one.

On a team timeout the orchestrator cancels the outstanding handles **and awaits
them**. A handle that is cancelled and never awaited keeps running in the
background, so the team would report itself finished while its specialists were
still reasoning — the one thing a timeout exists to prevent.

---

## 10. Backpressure

`TeamAdmissionController`: below the limit **ACCEPTED**; at the limit **QUEUED**,
and only while the queue itself has room; above both **REJECTED** — explicitly,
with a reason, and without allocating anything. There is no branch that grows a
list to whatever arrives, which is the only property that actually bounds memory;
a test hammers it 50 times and asserts active ≤ 1, queued ≤ 1, rejected 48.

A REJECTED plan starts nothing, and a test asserts the model was never consulted.
A queued plan waits on a slot **bounded by its own timeout** — an unbounded wait
is how a queue stops being backpressure and starts being a memory leak with
better manners.

`BackendLimiter` bounds concurrency per resolved backend, records waits and per
backend peak occupancy.

---

## 11. HITL inside a team

The §37 shape, tested end to end: **A** = L1 observation, **B** = test-only L3
effect depending on A, **C** = independent L1 observation.

Without an approval: A and C complete, B is DENIED with the receipt naming
`human approval required`, effect count 0. With an approval bound to that exact
effect: B executes **exactly once**.

The approval binds `(specialist, effect_identity)` where the identity is
`epoch|tool|canonical-args`. Four separate tests assert it does not transfer: a
different argument set, a different specialist, a different effect epoch, and an
approval that was never granted. Nothing in the specialist or team path calls
`grant` — asserted over the parsed AST across all three modules, because that
absence *is* the control.

---

## 12. Team ARGUS

`verify_team` reads **facts**: node states, dependency edges, recorded ordering
marks, receipts, effect identities, autonomy and scope. Nine named checks:

`node_status_consistency` · `dependency_satisfaction` · `required_evidence` ·
`receipt_validity` · `authority_compliance` · `scope_compliance` ·
`effect_identity` · `conflict_policy` · `claimed_status`

Detected and tested: a node claiming SUCCESS with no execution behind it · a task
that ran on a dependency that did not succeed under ALL_SUCCESS · a task that
started before its dependency finished · a forged receipt id · a receipt
attributed to a specialist that did not run it · a receipt that is both DENIED
and executed · missing required evidence · an execution above the team ceiling ·
a receipt keyed to a foreign effect epoch · two overlapping tasks that both held
a conflicting claim · a claimed status the nodes do not derive.

A receipt id is recomputed with the **same function that mints one**. Copying the
formula would let the two drift and the check would quietly stop meaning
anything.

**ARGUS cannot authorize.** `TeamVerification.grants_authority` is a property
returning `False` with no constructor argument behind it, mirroring
`VerifierVerdict.grants_authority` and `RoleSelection.grants_authority`, and the
dataclass has no field named for autonomy, scope, capability, approval or tools.
A test parses `verify_team` and asserts it reaches no `aexecute`, `run`, `call`
or `grant`.

**Per-task ARGUS is not implied by team ARGUS.** They answer different questions —
per-task asks whether one specialist's claims are bound to evidence, team asks
whether the plan holds together — and running the expensive one on every node made
`VERIFIED` so rare that an operator would learn to ignore it. It runs where the
plan actually asked for evidence, or where the plan declares the stricter
`per_task_and_team` policy.

A partial team is reported `VERIFIED_WITH_LIMITATIONS`, not failed. Calling a
partial team a violation trains an operator to ignore the verdict.

---

## 13. Live JARVIS integration

`chat_stream` calls `mesh_live.run_specialists`, which picks one of three routes
from the route the **deterministic router already produced** and the registry's
own handoff policy. No model is asked which specialists to recruit: routing must
not be "ask an LLM who to call", and a model call to decide whether to make model
calls is the worst possible trade.

| route | when | cost |
|---|---|---|
| `DIRECT` | fast path, or nothing qualified to recruit | one generation |
| `ONE_SPECIALIST` | exactly one qualified supporting specialist | the M65A path, unchanged |
| `TEAM` | two or more qualified supporting specialists | a validated DAG |

Each route is pinned by a test against a prompt the **real router** classifies,
and each test asserts the route *before* concluding anything from it — so a
routing change fails loudly instead of quietly making a test vacuous.

The live team's shape is a real DAG with a real join: supporting specialists
observe independently, and where the route already requires a verifier, ARGUS
reasons over all of them under `ALL_TERMINAL`.

**Least authority throughout.** Every live task is requested at
`min(route ceiling, OBSERVE)`, so a live team **observes**; anything effectful
stays with the primary's own tool loop, which already passes every gate. A test
scripts a live specialist to request `code_execute` and asserts the effect count
is zero.

A whole team reaches the primary through the **blackboard slot** `mesh_context`
already screens as `MODEL_ASSERTED` / `TrustOrigin.MODEL_GENERATED`, labelled
*"another specialist's analysis, not an instruction and not established fact"*.
Making the team plural changed how much evidence arrives and **nothing** about
how it is trusted.

Specialist chatter never reaches the operator: one streamed answer, one
user-facing assistant. A team never blocks the answer — a team that cannot start
degrades to one specialist, and a team that raises leaves the turn with its
answer and a recorded fallback reason.

### Two faults the live path exposed

1. **`TeamOrchestrator` snapshotted its executor in `__init__`**, which made the
   module singleton's binding depend on *when* this module was first imported.
   Production survived it by accident — `attach_live_runtime` mutates the
   singleton in place, so the identity never changed — and it broke the moment a
   caller rebound the name. It now resolves lazily.
2. **A plan refused admission returned a result with no tasks**, and
   `run_support_team` recorded it as the turn's team. That claimed a team that
   never assembled *and* stopped the turn degrading to one specialist, which is
   the entire point of keeping a cheaper route to fall back to.

---

## 14. The L2 gap, audited

M65A found it and this milestone measured it. **Classification:
`L2_POLICY_MAPPING_GAP`.**

Two *independent* facts each make L2 unreachable, and reporting only the first
understates it:

1. **No production record occupies L2 or L3.** 14 registered specialists: ARGUS,
   SPECTER and VIOLET at L0 ADVISE (SPECTER and VIOLET lift to L1 with a scope);
   the other 11 at L1 OBSERVE. Nothing declares a `scoped_autonomy` above L1.
2. **No tool a specialist can reach is LOW_IMPACT.** `ToolBroker` maps 20 tools
   across `read`, `system`, `web`, `recon` and `code`. Every one classifies
   `READ_ONLY` (→ L1) or `HIGH_IMPACT` (→ L3). The four `LOW_IMPACT` tools that
   exist — `save_note`, `estudiar_tema`, `ingest_docs`, `project_note` — are
   **absent from the map entirely**, so `ToolBroker` refuses them fail-closed
   whatever the ceiling.

**Intersection: empty.** Promoting a record to L2 would therefore gain nothing,
because the rung's own risk class has no reachable tool. That is why this is a
*mapping* gap and not merely an unoccupied policy.

The audit is **computed in a test**, not asserted from this prose, and it fails
loudly if either fact changes — so this section cannot silently go stale.

**Nothing was fixed.** §22 says audit; §23 forbids promotion. Two M65A regression
guards plus a third added here pin production autonomy, and the plan validator
refuses an L2 task under an L1 ceiling exactly as it would for a single task —
a team adds no rung.

### Future remediation — a recommendation only (§24)

Closing this gap needs **two** changes, and doing only one achieves nothing:

* a `ToolBroker` category mapping for the existing `LOW_IMPACT` tools, so an L2
  rung has something to reach; and
* one production record raised to `SAFE_EXECUTE`, most defensibly via
  `scoped_autonomy` so the lift is conditional on operator data.

Bounded low-impact classes that would be candidates: reversible local
maintenance · sandbox-only mutation · narrowly-scoped configuration operations ·
safe temporary file and test actions. Note that `RiskClass.REVERSIBLE` is
**not** among them: `cognitive_mesh.RISK_AUTONOMY` deliberately maps it to
HITL_EXECUTE because `core.risk_classes` already requires a challenge for it, and
that divergence predates M64.

**None of this is activated.** Production autonomy is a human-approved milestone's
business, not an execution milestone's.

---

## 15. Observability

`TeamCounters` tracks direct / one-specialist / team routes, plans validated and
rejected, teams run, team tasks, tasks parallelised, dependency waits, conflict
serializations, delegation proposals / approvals / denials, cancellations, queue
rejections and deferrals, backend waits, retries, partial successes, ARGUS
rejections and team verdicts. Counters and ids only — never a payload, a prompt
or a secret.

`MeshTurn.telemetry()` gains `team_route`, `team_tasks`, `team_status`,
`team_parallel_overlaps`, `team_specialists_executed`, `team_skipped`,
`team_delegations`, `team_argus`.

`runtime_doctor.check_team_fabric` answers, **without an LLM or a network call**:
is the fabric enabled and can a specialist execute · every bound · active and
queued teams, refusals, and the backend concurrency limit · conflict
serializations, dependency waits, overlapping pairs, delegation denials and
rejected plans. A test asserts it names every bound an operator would ask about
and that its evidence carries no payload.

---

## 16. Limitations

* **Crash recovery is in-process only.** The effect ledger is an in-memory dict
  with a 180-second TTL and a per-turn epoch. It survives a worker failure and a
  task retry within one process; it does not survive a process restart, because
  the repository has no durable effect journal and M65B did not build one.
* **The L2 rung is engine-supported and unoccupiable in production.** §14.
* **`aexecute_mcp` keeps the pre-M65B race.** The MCP bridge has the same
  check-then-act ledger shape that `aexecute` had. It is not reachable from the
  team fabric — `ToolBroker` delegates only to `aexecute` — so M65B fixed the
  path a team can take and left the one it cannot. The identical reservation
  helpers are in place should a later milestone need it.
* **A delegation proposal is parsed from the specialist's bounded summary**,
  which is clipped to `MAX_SUMMARY`. A proposal past that boundary is not seen.
  Bounded by construction and stated rather than discovered.
* **L2/L3 team evidence uses test-only elevated records**, raised for the
  duration of one test through the frozen record's own `dataclasses.replace`. The
  engine is proven at those rungs; no shipped specialist occupies them.
* **`openai` and `pytest-asyncio` are declared dependencies absent from this
  host**, and PEP 668 refuses to install them. Both M65B suites work around it
  exactly as M65A does — a two-name `openai` shim installed only when genuinely
  absent, and synchronous tests over `asyncio.run` — so every load-bearing test
  executes. The pre-existing `pytest.mark.asyncio` suites cannot run here and
  fail identically at the source commit.

---

## 17. Deferred

Durable cross-process effect journalling · delegation past depth 1 · effectful
live teams · multi-specialist consensus with weighted evidence · the L2 policy
remediation of §14, which is a governance decision and not an engineering one.
