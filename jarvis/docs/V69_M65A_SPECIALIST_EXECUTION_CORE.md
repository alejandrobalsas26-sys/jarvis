# V69 M65A — Specialist Execution Core

**Branch** `jarvis-v69-m65a-specialist-execution-core`
**Source** `882e7e08c8e6fb1eeee3b11fec286c017adca5ef` (V69 S4H)

M64 built the Cognitive Specialist Mesh: a registry of role contracts, typed
contracts to speak in, a deterministic router, a preflight gate and ARGUS. M64.1
wired the mesh's **judgement** onto the live operator turn — a route, a compiled
context, an evidence graph, a verifier verdict.

Neither gave a specialist a way to do **work**. `MeshTurn.support_results` was
declared in M64.1 and nothing ever appended to it. `preferred_model_roles` was
declared on all 14 records, copied into telemetry, and read by nothing that
chooses a backend; its own docstring said "advisory only".

M65A closes both gaps, and nothing else. It is a spine, not a swarm.

---

## 1. What became live

```
    USER
      |
    JARVIS                       one operator-facing assistant
      |
    ROUTER            mesh_router.route_task — pure, deterministic, no model
      |
    SPECIALIST        specialist_execution.SpecialistExecutor — NEW
      |
    CAPABILITY/SCOPE  SpecialistRecord + AuthorizedSecurityScope
      |
    AUTHORITY         AutonomyLevel + HitlApproval
      |
    TOOL EXECUTOR     tools.executor.ToolExecutor.aexecute — unchanged
      |
    EFFECT LEDGER     that executor's own ledger — unchanged
      |
    ARGUS             mesh_verifier.verify — unchanged
      |
    JARVIS
      |
    USER
```

Measured on a real turn through `LLM.chat_stream`:

> *"Please analyse this Sysmon alert and correlate it with our previous
> incidents and threat intel"* routes `team_verified` with **GUARDIAN** primary;
> **TRACE** executes as the supporting specialist on the **deep** role, backend
> **qwen3:14b**, and returns a typed result into `support_results`.

Two new modules, everything else reused:

| Module | Contents |
|---|---|
| `core/specialist_execution.py` | `SpecialistExecutionRequest` / `SpecialistExecutionResult`, `ExecutionStatus`, `ToolIntent`, `ToolReceipt`, `HitlApproval` + registry, `SpecialistExecutor`, `ExecutionCounters` |
| `core/model_role_router.py` | `ModelRoleRouter`, `RoleSelection`, `RoleAvailability`, `RoleDenial` |

Edited: `core/mesh_live.py` (support execution + telemetry), `core/llm.py` (the
call site), `core/runtime_doctor.py` (a check), `main.py` (boot wiring).

There is **no** second registry, broker, ledger, verifier or executive, and no
`subprocess`, `socket` or raw-handler path — asserted over the parsed AST, not
by grep, so the module's own prose does not fail the test that proves it.

---

## 2. The execution contract

`SpecialistExecutionRequest` carries `execution_id`, `plan_id`, `specialist_id`,
`objective`, `capability`, `model_role`, `autonomy_level`, `authorized_scope`,
`allowed_tools`, `activity`, `budget`, `deadline_s`, `evidence_requirements`,
`effect_epoch`, `task_class`.

`SpecialistExecutionResult` carries `status`, `summary`, `findings`,
`evidence_ids`, `proposals`, `tool_receipts`, `verification`, `warnings`,
`effective_autonomy`, `model_selection`, `attempts`, `duration_ms`,
`body_safe_trace`.

**The request is not the source of truth for authority.** `autonomy_level` and
`allowed_tools` say what the *caller* permits; the executor intersects them with
the registry and takes the **minimum**. A request can narrow an execution. It can
never widen one.

`executed_effects`, `deduplicated_effects`, `denied_tools`, `verified` and
`result_status` are **derived properties**, never stored — a specialist cannot
report zero denials by saying so.

---

## 3. The load-bearing asymmetry

A specialist supplies **intent**. Every question of **permission** is answered by
the executor reading the registry — never by reading the specialist's output.

Its text is parsed for exactly one thing: `TOOL_INTENT:` blocks. `ToolIntent` has
no field for autonomy, scope, capability, approval or verification, so a
specialist has **nowhere to put the claim**. `parse_tool_intents` drops unknown
keys and records what it dropped.

That is why "a specialist cannot grant itself authority" is structural rather
than policed: it is not that the claim is checked and rejected; it is that the
claim has no shape to arrive in.

Brace-balancing note: the intent scanner balances braces and is string-aware. A
non-greedy `\{.*?\}` regex closes on the **inner** brace of any intent carrying a
`tool_input` object — every real tool request parsed as malformed and was
silently dropped. Measured on the first end-to-end run, not theorised.

---

## 4. Authority: the engine vs. the shipped posture

**This distinction is the most important thing in this document.**

### 4.1 The shipped registry

All 14 records sit at **L0 ADVISE or L1 OBSERVE**. The only `scoped_autonomy`
that exists lifts SPECTER and VIOLET from L0 to L1.

| | ceiling | with scope | capabilities |
|---|---|---|---|
| JARVIS, ATLAS, CIRRUS, ARCHIVIST | L1 | — | read (+web) |
| FORGE, CIRCUIT | L1 | — | read, code |
| HELIOS, MESH | L1 | — | read, system |
| GUARDIAN, TRACE | L1 | — | read, forensic |
| ORACLE | L1 | — | read, web |
| SPECTER | **L0** | L1 | read, recon |
| VIOLET | **L0** | L1 | read, forensic |
| ARGUS | **L0** | — | read |

So on the specialist path **as it ships, L2 SAFE_EXECUTE and L3 HITL_EXECUTE are
unreachable** — not because the ladder is broken, but because it is deliberately
unclimbed. Compounding it, the tool categories a specialist can reach and the
`LOW_IMPACT` risk set are **disjoint**, so no L2-risk tool is reachable either.

Two regression guards pin this, so a future record that grants more has to say
so out loud:

* `test_no_registered_specialist_reaches_l2_or_above`
* `test_no_scope_lift_reaches_l2_or_above`

### 4.2 What M65A did NOT do

**It did not promote anyone.** Raising a production ceiling so an L2 test could
pass would be an authority-expansion milestone wearing an execution milestone's
name.

### 4.3 What the engine supports

The executor governs the full ladder. The L2/L3 tests elevate **one record for
the duration of one test** via the frozen record's own `dataclasses.replace`,
which is what "change this file and land a commit" would do. They prove the
engine governs those rungs. They do **not** claim any shipped specialist occupies
them.

| | engine | shipped specialists |
|---|---|---|
| L0 ADVISE — analysis only, every tool denied | enforced | ARGUS, SPECTER, VIOLET |
| L1 OBSERVE — read-only tools, writes denied | enforced | the other 11 |
| L2 SAFE_EXECUTE | **supported** | **none** |
| L3 HITL_EXECUTE — bound approval required | **supported** | **none** |
| L4 PROHIBITED — always denied, before inference | enforced | none |

`permits()` fails closed on **both** sides: a PROHIBITED requirement is refused,
and a PROHIBITED *ceiling* permits nothing rather than everything.

---

## 5. Model-role routing

Real, observable, and privilege-free. Every backend name still comes from
`model_router.resolve_role_model` — the one precedence ladder (env override →
central config → hardware hint → installed-compatible → central default).
Nothing is invented.

Resolved on this host: **coder** → `qwen2.5-coder:latest`, **deep** →
`qwen3:14b`, **fast** → `qwen3:8b`, **verifier** → `qwen3:8b`, **vision** →
`gemma3:4b`. Several roles sharing one backend is a supported host, not a
degraded one.

`RoleSelection` records requested role, selected role, backend, whether a
fallback was used and why, what was considered, and a bounded trace.

**Fail-closed (§16):** an unknown role is **refused, not coerced to FAST** —
coercion would hide both the bug and the injection. A non-reasoning role
(embedding, cloud) is never selected. No backend at all is a refusal; there is no
"run it on whatever is loaded" branch.

**A model role grants nothing.** `RoleSelection.grants_authority` is a property
returning `False` with no constructor argument behind it, and the dataclass has
no field named for autonomy, capability, scope, tools or approval. §15 is
therefore a property of the type, not a promise about it. A fallback can only
move *across* roles; since a role carries no privilege, no fallback can raise
one.

---

## 6. ToolExecutor, and exactly-once

`ToolExecutor.aexecute` remains the one effect path. The specialist reaches it
through `ToolBroker`, which enforces the capability allowlist and then delegates —
so authority, risk class, LAB_ONLY, the NATO challenge and the effect ledger all
re-apply. `ToolIntent.effect_identity` is byte-identical to
`ToolExecutor._effect_key`; a test asserts it, because an identity that did not
match the ledger's would let a duplicate through while looking careful.

**Two distinct mechanisms, reported separately:**

* **HITL replay protection** — a single-use `HitlApproval` is spent on first use.
* **Effect ledger dedupe** — the ledger suppresses an identical effect within an
  epoch and **returns the recorded result**.

They are tested separately on purpose. The duplicate-effect test originally
passed while proving nothing: the single-use approval refused the second intent
before the ledger was ever consulted. It now grants a *reusable* approval, which
removes that gate and leaves the ledger as the only thing standing between two
identical intents and two effects. Seven further tests call `aexecute` directly
with nothing in the way.

Proven: duplicate intent → one effect; retry → one effect; crash after commit but
before delivery → receipt recovered, no second effect; two different specialists
submitting the same intent → one effect; argument order cannot manufacture a
second identity; a new epoch is never blocked; read-only calls are never keyed; a
failed effect is never ledgered.

---

## 7. HITL

`HitlApproval` binds to `effect_identity` — epoch, tool and canonical arguments —
which is exactly what the ledger keys on. Change the target, the arguments or the
tool and the identity changes, so approval for A cannot approve a modified B.

It does **not** replace the executor's NATO challenge or
`ContainmentAuthorization`: it is the earlier, narrower question of whether an
intent may be handed to the executor at all. An approval here is a **necessary**
condition and never a sufficient one — a denied challenge still stops the effect.

No expiry means **expired**; an unparseable expiry means **expired**. Both are
the fail-closed reading `ContainmentAuthorization` adopted after the M64.1 CASE E
finding: an approval whose deadline cannot be read is not an approval. Single-use
consumption is recorded in the registry, not on the frozen approval, so an
approval object cannot un-consume itself.

Nothing in the specialist path calls `grant`. That absence is the control.

---

## 8. ARGUS

ARGUS **verifies**; it does not authorize, elevate autonomy, grant scope or
approve L3. It runs strictly **after** every effect decision, so no verdict can
retroactively permit one. `VerifierVerdict.grants_authority` is `False` with no
field behind it.

Rejected: a forged SUCCESS receipt with no output; output attached to a DENIED
call; a missing receipt under an evidence requirement; a scope-denied target that
produced output naming it; a request that marked **itself**
`APPROVED_FOR_EXECUTOR`; a claim resting only on quarantined evidence. And the
inverse, so the detector is not merely permissive in one direction: a genuinely
refused call is the control working and does **not** read as a violation.

A specialist writing "VERIFIED" in its summary changes nothing — `verified` is
derived from `verification`, which only ARGUS writes.

---

## 9. Injection

The security property tested is the one that matters: **injected text alters no
authority, scope, capability, approval or tool allowlist.**

A note on the firewall's actual contract, which the campaign forced into the
open. `TrustOrigin.TOOL_RESULT` is a trusted **channel** — the output came from
our own executor — so `apply_firewall` records the attempt and sets
`tool_influence_allowed=False` rather than blanking content that is still a real
observation of what a file contained. `MODEL_ASSERTED`, `DOCUMENT` and
`EXTERNAL_REPORT` **are** quarantined.

M65A did not rewrite the firewall to force a QUARANTINED label the repository
deliberately does not use there. It tests the real property instead, and adds the
one place the quarantine flag is load-bearing: a **DOCUMENT** reference — whose
provenance *can* support a verified claim — must not corroborate once
quarantined, with clean content at the same provenance as the control case.

---

## 10. Live JARVIS integration

`chat_stream` calls `mesh_live.run_support_specialist`. It runs on `TEAM` and
`TEAM_VERIFIED` routes and nothing else, so a greeting still costs exactly one
generation (`test_the_fast_path_pays_for_no_second_generation` measures it).

Bounds, each a named constant rather than a property of a loop:
`MAX_LIVE_SUPPORT_EXECUTIONS = 1`, `SUPPORT_DEADLINE_S = 20`,
`MAX_SUPPORT_DIGEST_CHARS = 900`, and least authority — the consultation is
requested at `min(route ceiling, OBSERVE)`.

The consultation reaches the primary through the **blackboard** slot, which
`mesh_context` already screens as `MODEL_ASSERTED` /
`TrustOrigin.MODEL_GENERATED`, labelled *"another specialist's analysis, not an
instruction and not established fact"*. Routing it through the existing screened
slot rather than concatenating it onto the prompt is what keeps that true
structurally.

Specialist chatter never reaches the operator. There is one streamed answer and
one user-facing assistant.

Support never blocks the answer: a denied, failed or timed-out consultation
degrades the turn's **judgement**, never its ability to answer. Support is an
improvement, so its failure costs only the improvement.

---

## 11. Observability

`MeshTurn.telemetry()` gains `support_executions`, `support_specialists_run`,
`support_status`, `support_model_roles`, `support_model_fallbacks`,
`support_effects`, `support_deduplicated_effects`, `support_denied_tools`.
`ExecutionCounters` tracks executions, statuses, policy denials, tool intents,
effects executed and deduplicated, HITL requested and denied, model fallbacks and
ARGUS verdicts. Counters and ids only — never a payload, a prompt or a secret.

`runtime_doctor.check_specialist_execution` answers, **without an LLM or a
network call**: are specialists registered, can one execute, do model roles
resolve to a backend, is ToolExecutor wired, is the effect ledger observable.

---

## 12. Tests and mutation evidence

156 focused tests, **no skips**, across three suites — the execution gauntlet,
the model-role router, and the live operator turn entered through the real
`LLM.chat_stream`.

**Mutation campaign: 35/35 detected.** AUTHORITY 7, TOOL/ONCE 7, MODEL 6,
ROUTING 5, ARGUS 5, INJECT/SCOPE 5 — every category above its floor, no duplicate
aliases.

The campaign earned its place. Nine mutations initially survived, and none was a
mutation to soften:

* **Eight were coverage gaps of the same shape** — with the records and routes
  the repository actually ships, the weakened branch and the correct branch
  return the *same* answer. A missing scope "collapsing the ceiling to ADVISE" is
  invisible through SPECTER, already at ADVISE. "Support only on a team route" is
  invisible through real fast-path routes, which name no support to recruit.
  Those tests now build the record or route directly.
* **One was dead code.** The `turn.is_fast` guard could not fail — FAST_PATH is
  not in `{TEAM, TEAM_VERIFIED}` either. It was deleted. A guard that cannot fail
  is not defence in depth, it is a second place to have to keep correct.

Two bugs in M65A's own code were found by writing the tests, not by review:

1. `_scope_lift` clamped the lifted ceiling by the **pre-lift** ceiling. For every
   specialist that declares a `scoped_autonomy`, the pre-lift ceiling *is*
   `default_autonomy`, so the min always returned it and a registered scope
   lifted nothing at all.
2. The `TOOL_INTENT` regex closed on the inner brace (see §3).

---

## 12b. Control plane, and one rescoped suite

Generation **29** (`0029-m65a-specialist-execution-core-branch.json`, milestone
label **S5C**) is **governance-only**. It exists for one mechanical reason:
`GIT_AUTHORITY` pins `project.branch`, so working on a new branch fails the
verifier until a generation declares it. M64 wrote gen 19 (S5A) and M64.1 wrote
gen 20 (S5B) for exactly the same reason; M65A takes the next label.

Nothing scientific moves. A diff of generations 28 and 29 shows only
`control_plane_note`, `generation_label`, `parent_snapshot_sha256`, `project`,
`state_generation`, `subject_state_commit` and `subject_state_milestone`. The
eight content-addressed record pointers are **byte-for-byte** those of generation
28, so candidates, datasets, receipts, policy identities, defects, limitations,
frozen invariants and the archive digest are the *same blocks*. Candidate 005
stays `EVALUATED_NOT_ELIGIBLE`, 004 keeps its HOLD, `eval-v7` stays
`USED_IMMUTABLE`, and both evaluation receipts hash to the values they had at the
source commit. Verifier: **PASS, PROBLEMS 0**.

The schema refused three drafts, each usefully: `subject_state_milestone` must
match `^S[0-9A-Z.]{1,10}$` (hence S5C, not "M65A"); `test_baseline` has a closed
key set, so a note about M65A's tests belongs here rather than smuggled in as an
extra field; and `HOLDOUT_FIREWALL` caps `control_plane_note` at 320 characters
precisely so a body cannot arrive in instalments — the first draft was 1,210.

### The rescoping, argued here because precedent requires it

Writing generation 29 broke four assertions in
`test_training_gym_m62_s4h_control_plane.py`. PROGRESS §10 already names the
shape: *an assertion comparing a sealed milestone's property against LIVE state
also asserts, silently, that no later generation exists.* The suite's `stored`
fixture read `current.json` and followed it, so four tests about **generation
28** were really being asked of whatever generation was newest.

They now read generation 28 **by path**. No assertion is weakened — each still
re-derives S4H's claims from the record store, the production modules and the
suite manifest, against the snapshot that actually made them. This is the
precedent S3N, S3S, S3X.1, S4D and S4F each set.

One test could not simply be repointed, because its subject *is* the live
pointer. `test_the_pointer_names_generation_28` is replaced by what S4H can
honestly assert about live state: generation 28 still exists where it was
written, the schema did not change under it, and the chain only moves forward
(`>= 28`). A successor generation is a normal event, not a finding.

That trade would have quietly lost something: the immutability of 28's bytes was
being protected by the digest check the old test performed *through* the pointer.
So it is restored from the other side —
`test_generation_28_hashes_to_what_its_successor_recorded` asserts that whatever
names itself generation 29 carries 28's real digest as its parent. The property
survives the rescoping rather than leaking out of it.

**PROGRESS.md** was recompacted, not grown: it sat at 40,903 of its 40,960-byte
cap. The budget was **not** raised. Savings came from §15's own rule — superseded
detail folds into the document that owns it — plus the file's existing `…`
abbreviation for pointer digests whose full values live in the records the
verifier re-hashes. The three frozen `*_policy_hash` identities in §6 were left in
full on purpose: §6 exists so a human can compare them by eye. A drift was fixed
in passing: the header table still declared generation **27** while §1 and §2
carried the gen-28 identities, and the verifier's four required needles were all
satisfied elsewhere in the file, so the stale header had gone unnoticed.

---

## 13. Limitations

* **No L2-risk tool is reachable through the capability map.** `ToolBroker`'s
  tool→category map and the `LOW_IMPACT` risk set are disjoint, so every
  categorised tool is READ_ONLY (L1) or HIGH_IMPACT (L3). The L2 rung is
  engine-supported and, today, unoccupiable in production.
* **L2/L3 evidence uses test-only elevated records.** The engine is proven; no
  shipped specialist has that authority.
* **`openai` and `pytest-asyncio` are declared dependencies absent from this
  host**, and PEP 668 refuses to install them. The M65A live suite works around
  both — a two-name `openai` shim installed only when genuinely absent, and
  synchronous tests over `asyncio.run` — so its load-bearing tests execute. The
  pre-existing M64.1 gauntlet (64 tests) and the other `pytest.mark.asyncio`
  suites cannot run here; they fail identically at the source commit.
* **One supporting specialist per turn.** By design.

---

## 14. Deferred to M65B

Arbitrary multi-specialist DAGs · broad parallel specialist teams · recursive
delegation · specialist-spawns-specialist · conflict/resource scheduling ·
advanced cancellation graphs · advanced backpressure · multi-specialist
consensus.

M65A deliberately implements none of them. Raising `MAX_LIVE_SUPPORT_EXECUTIONS`
would not produce a team; it would produce a scheduler this milestone does not
have.
