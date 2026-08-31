# V69 M64 — Cognitive Specialist Mesh

> **ONE JARVIS. Many disciplined expert perspectives.**
> Architecture milestone. It authorises no evaluation, no training, no promotion,
> no merge and no release. Candidate 004 keeps its HOLD; candidate 005 stays
> `TRAINED_UNEVALUATED`; `eval-v7` does not exist.

| | |
|---|---|
| Branch | `jarvis-v69-m64-cognitive-mesh` |
| Control plane | V3, generation **19** (governance-only branch declaration) |
| New production modules | 8 under `jarvis/core/` |
| New tests | 111 across two files, all offline |
| Reused, not rebuilt | team executive, blackboard, tool broker, executor, authority, scope matcher, risk taxonomy, model router, injection firewall, world state, memory fabric |

---

## 1 — What this milestone actually adds

JARVIS already had a controlled multi-agent runtime (V63 M4): 15 capability
roles, a bounded `SharedBlackboard`, a `ToolBroker` that delegates to the one
`ToolExecutor`, resource-aware concurrency, and a critic/verifier fan-in. M64
does **not** replace any of it.

What was missing was not an executive. It was **judgement about who owns a
problem, how far they may go, and what their conclusion rests on.** M64 adds
exactly that, in eight modules:

| Module | Answers |
|---|---|
| `core/cognitive_mesh.py` | What is a specialist *allowed to be*? |
| `core/security_scope.py` | Which security activity is authorised, where, how far, until when? |
| `core/mesh_contracts.py` | What shapes do specialists speak in? |
| `core/mesh_router.py` | Who owns this request, and how far may they act? |
| `core/mesh_context.py` | What is this specialist shown — and screened from? |
| `core/mesh_workflows.py` | In what ORDER does each discipline work? |
| `core/mesh_verifier.py` | Does the conclusion survive ARGUS? |
| `core/mesh_orchestrator.py` | Choose, bound, bind evidence, verify, synthesise. |

**Nothing here executes.** There is no `subprocess`, `os`, `socket`, `eval` or
`exec` anywhere in the eight modules, and none of them imports `tools.*` or
`mcp_servers.*` — asserted over the AST, not over text
(`test_no_mesh_module_imports_an_execution_primitive`).

---

## 2 — The roster

Fourteen role contracts. A specialist is a **role**, never a resident model:
`preferred_model_roles` is advisory and `core.model_router.route()` stays
authoritative.

| Codename | Role | Autonomy | Max risk | Runtime role (reused) |
|---|---|---|---|---|
| JARVIS | Orchestrator | L1 | read_only | `PLANNER` |
| ATLAS | Generalist / chief of staff | L1 | read_only | `GENERAL` |
| FORGE | Software engineering | L1 | low_impact | `CODE` |
| HELIOS | Systems / DevOps / SRE | L1 | read_only | `OPERATIONAL` |
| MESH | Network engineering | L1 | read_only | `OPERATIONAL` |
| GUARDIAN | Blue team / SOC | **L1** | read_only | `CYBER_BLUE` |
| TRACE | DFIR | L1 | read_only | `DFIR` |
| ORACLE | Threat intel / OSINT | L1 | read_only | `RESEARCH` |
| SPECTER | Authorized red team | **L0** (L1 with scope) | lab_only | `CYBER_PURPLE` |
| VIOLET | Purple team | **L0** (L1 with scope) | lab_only | `CYBER_PURPLE` |
| CIRRUS | Cloud | L1 | read_only | `ARCHITECT` |
| CIRCUIT | Embedded / IoT | L1 | read_only | `CODE` |
| ARCHIVIST | Research | L1 | read_only | `RESEARCH` |
| ARGUS | Verifier | **L0** | read_only | `VERIFIER` |

The `runtime_role` column is the load-bearing one: every record names the
**existing** `SpecialistRole` that executes it, which is why the mesh needs no
second executive, no second blackboard and no second tool path.

---

## 3 — Autonomy

```
L0 ADVISE        reason only; no tool execution
L1 OBSERVE       read-only tools, World State, logs, files, status
L2 SAFE_EXECUTE  reversible low-risk actions the capability policy permits
L3 HITL_EXECUTE  effectful / security-impacting; a human confirms
L4 PROHIBITED    never executable by the agent runtime, at any ceiling
```

`permits(ceiling, required)` is **fail-closed on both sides**: a `PROHIBITED`
requirement is refused, and a `PROHIBITED` *ceiling* permits nothing rather than
everything. There is no `raise_autonomy()` anywhere in the codebase; a specialist
cannot lift its own ceiling and neither can a caller.

### Deliberate divergence from the directive

The directive describes L2 as covering "reversible low-risk actions".
`core/risk_classes.py` already requires a HITL challenge for
`RiskClass.REVERSIBLE`, and says so explicitly: *"HITL is never removed where it
already applied."* M64 is an architecture milestone and does not get to relax a
control that predates it.

> **`RiskClass.REVERSIBLE` maps to `L3 HITL_EXECUTE`, not `L2`.**
> `L2 SAFE_EXECUTE` covers `LOW_IMPACT` alone — actions that mutate JARVIS's own
> notes and vector store and nothing else.

### The one lift that exists

`SpecialistRecord.ceiling_with_scope(scope_valid)` raises SPECTER and VIOLET from
`L0 ADVISE` to `L1 OBSERVE` — **and no further**. This is not self-granting: the
lift is conditional on an operator-registered `AuthorizedSecurityScope` that no
specialist can write, and `L1` is read-only enumeration. An active validation or
an exploit proof remains an `ActionRequest` and a human decision.

---

## 4 — Routing

Deterministic. No model call, so routing cannot be talked into a different
answer, and the gauntlet asserts on it exactly.

`route_task()` consumes the existing `classify_domain()` and
`assemble_task_decision()` rather than re-classifying, and adds only what neither
models: which expert owns the problem, and how far it may go.

### Security intent — the semantic layer

The single most important correction in this milestone. The gauntlet found:

> *"A suspicious process is beaconing from an infected endpoint"* routed to the
> **red team**.

Not because the offensive gate was weak — because the classification underneath
it was wrong. `core.task_domain`'s `CYBER_PURPLE` vocabulary is
`{beacon, persistence, lateral movement, c2, exploit, payload, shellcode,
privilege escalation}` — the daily vocabulary of a SOC analyst describing what an
intruder *did*. Those words are **subject matter**. They say what the
conversation is about; they say nothing about who owns it.

What separates red from blue is the **speech act**. `classify_security_intent()`
models four frames over *verbs*, not topics:

```
PURPLE       "did our detection catch ..."   -> VIOLET
OPERATIONAL  "pentest X", "get a shell"      -> SPECTER
ANALYTIC     "investigate", "triage", "detect" -> GUARDIAN / TRACE / ORACLE
NONE         not a security request at all
```

Ordered, and conservative by construction:

1. A purple **frame** wins outright — measuring a detection is its own act.
2. The request must be security-bearing at all. *"Test my code"* is not a
   security request and is never read as one.
3. An **analytic verb governs the sentence** when present. *"Investigate this
   exploit attempt"* is an investigation; the noun does not make it an attack.
   A misread request therefore lands on the **defensive** specialist, where the
   worst case is a wasted consultation rather than an unwanted operation.
4. An **operational verb** with no analytic verb governing it is a request to
   act — and only then does SPECTER own the request.
5. A **directive verb** (`test`, `check`, `verify`) plus adversary subject matter
   and no analytic verb is also operational: settling *"test my vulnerable lab"*
   requires touching the target.

**This fixed what a request MEANS. The offensive-scope gate is untouched** —
SPECTER still requires a real `AuthorizedSecurityScope` for anything active.

### Reconciling §12 and §42.13

They read as contradictory until intent is separated from ambiguity:

* An **explicit** unscoped attack request is *not ambiguous*. SPECTER owns it, at
  `L0 ADVISE`, with every active step denied for want of a scope (§42.13).
* An **ambiguous** request never reaches SPECTER at all. A red/purple primary
  arrived at without an operational or purple speech act falls back to GUARDIAN
  (§12).

### Keyword matching is word-boundary

`core.task_domain`'s substring convention does not survive a vocabulary this
size: CIRCUIT's `"ble"` matched *vulnera**ble*** and routed an authorized web-lab
request to the embedded specialist. Boundaries are checked rather than the short
tokens deleted, because `ble`/`rce`/`xss`/`apt`/`vm` are exactly what a
length rule would remove.

### Fast path

The fast path is **ATLAS's alone**. Every other specialist's completion contract
begins with an observation, so "answer directly with no tools" is not a shape
they have — a diagnostic question that merely looks cheap is still a diagnosis.

Symmetrically, low routing confidence asks a clarifying question **only** when
complexity or stakes make a wrong guess expensive. Asking on *"what is 2+2?"*
would be theatre.

---

## 5 — Context compilation

A specialist receives the slices its record declares and nothing else. Omissions
are **recorded**, not silent.

Two controls are structural:

* **World State before observation.** `redundant_observation()` refuses a probe of
  an entity the world model saw seconds ago, returning what is already known. The
  specialist is not asked politely to check first.
* **Evidence is data, never instruction.** Every external string passes through
  the existing `injection_firewall` with the right `TrustOrigin`.

> The M64 audit found the live tool path firewalls only an **allowlist of twelve
> tool names**, leaving scan banners, malware strings and packet payloads
> unscreened. **The mesh does not inherit that gap.** Screening happens at
> ingestion (`add_screened`) *and* again at render, and a quarantined
> `EvidenceRef` is not `corroborating` — so injected text cannot support a claim
> even if some other caller renders it differently, or not at all.

`memory_scope` is passed **explicitly**. The audit found that field declared on
every role and never consulted, and `MemoryFabric.retrieve()` applies *no*
scoping when a caller omits it — omitting it here would be the leak, not a missed
optimisation.

---

## 6 — Handoffs

```
task_id · from · to · objective · depth · world_state_refs · evidence_refs
known_facts · hypotheses · assumptions · uncertainty · scope
prohibited_actions · requested_output · budget
```

`HandoffScope.narrow()` intersects targets and activities and takes the **minimum**
ceiling. There is no `widen()`, no `add_target()` and no `raise_ceiling()`, so
scope cannot creep along a delegation chain. `_min_budget()` is field-wise
minimum for the same reason: a caller passing a *larger* budget gets the current
one back.

---

## 7 — Evidence and claims

```
Claim ──┬──> EvidenceRef (provenance, source, tool_outcome)
        ├──> EvidenceRef
        └──> inference_rule
```

`Provenance` says **how the evidence came to exist**, and `MODEL_ASSERTED` is
deliberately absent from `CORROBORATING_PROVENANCE`: *a model asserting its own
conclusion is not evidence for it.*

> This closes a real gap. `AgentReport.evidence` is parsed out of a specialist's
> own bullet points (`_parse_report`) and is today indistinguishable from a
> genuine tool result.

`ToolCallStatus` has **no default member** and `ToolOutcome` requires one, so a
command that never ran cannot be described as `SUCCESS` — there is nothing to
construct the outcome from. A `DENIED`, `UNAVAILABLE`, `TIMEOUT` or `FAILURE`
outcome is never citable.

`EvidenceGraph.add_claim()` **re-derives** a claim's status from the evidence
actually bound to it. A caller cannot assert `VERIFIED` into existence. Only
ARGUS can promote, and only over corroborating evidence.

Contradiction detection is **structural** — the same normalised statement, one
negated — not the substring verdict scan in `_parse_report`, which a specialist
merely quoting the word "malicious" can flip.

---

## 8 — Actions

**Reasoning is cheap. World effects are expensive.**

```
evidence -> capability -> scope -> authority -> risk -> HITL -> ToolExecutor -> audit
```

`ActionRequest.executed` is a property that **always returns `False`**.
Constructing one performs nothing. `dispose_action()`'s best available outcome is
`APPROVED_FOR_EXECUTOR` — a hand-off to `authorize_action` → `classify_tool` →
the NATO HITL challenge in `ToolExecutor.aexecute`, the gate that always existed.

---

## 9 — Workflows

**GUARDIAN** — `triage → verify → assets → evidence → correlate → timeline →
ATT&CK → hypotheses → verify → containment recommendation → HITL → recovery`.

`Severity` and `Confidence` are **separate ladders**, and containment needs both.
`CRITICAL` severity at `UNCONFIRMED` confidence is exactly the false positive that
takes a production host offline. There is no `contain()`, `isolate()` or
`block()` on the class; its containment surface is exactly two methods and both
are questions.

**TRACE** — `preserve → hash → acquire → timeline → correlate → analyze → report`.
`preservation_gate()` returns `EVIDENCE_PRESERVATION_REQUIRED` as a **refusal**,
not a warning, for a destructive step before acquisition is complete.

**VIOLET** — `technique → hypothesis → authorized emulation → expected telemetry →
observed telemetry → detection result → gap → remediation → retest`.
`PurpleCycle.measure()` derives status from telemetry actually recorded, and
`gap_closed()` will not report a gap closed without a retest that **observed the
rule fire**.

**HELIOS / MESH** — ordered ladders. `diagnostic_gate()` makes "no blind restart"
and "no ping spam" *behaviour* rather than advice: the remediation rung is simply
unreachable while an earlier rung is unexamined.

---

## 10 — ARGUS

Deterministic. Checks run in **severity order**, so the operator is told the most
serious thing that is wrong:

```
scope violation → invented authority → fabricated tool results →
unresolved disagreement → insufficient evidence → completion → budgets
```

An empty task is `FAILED`, never `VERIFIED`.

`adjudicate()` ranks disagreeing claims by evidence quality and **deletes
neither**. When both rest on equal support it says `UNRESOLVED`, which is the
honest answer and the one an operator can act on.

`VerifierVerdict.grants_authority` is a property returning `False`. ARGUS
reports; there is no field on the type that could express a grant.

---

## 11 — Budgets

```
max_specialists      4      (== specialist_runtime._MAX_TOTAL_AGENTS; the
                             repository's own stricter convention wins)
max_handoff_depth    3
max_handoffs         6
max_verifier_retries 2
max_tool_calls       12
max_runtime_s        180
max_context_chars    6000
max_evidence_items   60
max_claims           40
```

Fast path: 1 specialist, 0 tool calls, 0 verifier retries, 30 s, 1500 chars.

An exhausted budget yields a `PARTIAL` result naming what remains — never an
extra attempt.

---

## 12 — Recorded pre-existing risks (NOT introduced by M64, NOT fixed by M64)

Two findings from the M64 read-only audit sit **outside this milestone's change
scope**. They are recorded here rather than quietly fixed, because changing live
defensive and offensive subsystems is a separate, explicitly-authorised decision.

### D-M64-1 — `correlator.py` auto-contains with no human confirmation

`core/correlator.py::_maybe_quarantine` (≈ line 397) self-grants
`ActorContext("jarvis-system", ClearanceLevel.L3_Hunter)` and calls
`network_quarantine.quarantine()`, which is gated only by
`@requires_clearance(L3_Hunter)` — a pure clearance-level comparison in
`RBACManager.check()`, **not** a human confirmation. On a Windows host with admin
rights and `_QUARANTINE_ENABLED`, a `severity >= 9.0` event whose type or ATT&CK
technique looks like lateral movement or scanning runs a real
`netsh advfirewall firewall add rule ... action=block`.

Interlocks that DO apply: `_is_protected()` (self/local/gateway/loopback),
`_valid_ip()`, `_MAX_ACTIVE = 16`, full audit, and a reversible `release()`.
Interlock that does NOT apply: **any human decision.**

*Verified independently for this document by reading
`core/correlator.py`, `core/network_quarantine.py` and `core/rbac_manager.py`.*

> **This is why the acceptance line reads `BLUE_AUTO_CONTAINMENT: NO (mesh)`.**
> Within the specialist mesh GUARDIAN provably cannot contain — asserted by four
> tests. The correlator is a different, older path and M64 did not change it.

**Recommendation:** route `_maybe_quarantine` through an `ActionRequest` and the
same HITL gate, or make the self-granted `ActorContext` require a live approval
hook. Note `core/punisher.py` already implements the correct fail-closed shape
(`_request_approval`) but its `set_approval_hook()` is never called anywhere, so
its destructive path is currently inert — wiring an *auto-approving* hook there
would silently convert a fail-closed control into a rubber stamp.

### D-M64-2 — `red_team_operator.py` runs unscoped active scans

`core/red_team_operator.py::AresOperator` accepts an arbitrary `target_ip` with
no IP validation and no scope check; its `AresCampaign.authorized: bool = False`
field is **never read**. Its SCAN stage issues `network_scan` (nmap `-sV -O`)
against whatever address was passed. The only gate is the generic `network_scan`
HIGH_IMPACT HITL challenge; `core.authority.authorize_action` is never consulted
because ARES never touches `AuthorityState`. EXPLOIT/POST stages are currently
inert stubs.

> SPECTER inside the mesh is unaffected: it goes through
> `authorize_security_activity` and is denied without a covering scope. ARES is a
> separate, older entry point.

**Recommendation:** have `start_campaign()` require an `AuthorizedSecurityScope`
and read its own `authorized` flag before any stage runs.

---

## 12b — Wiring status, stated plainly

**The mesh is complete, tested, and NOT yet on the live chat turn.** No module
outside `core/mesh_*.py`, `core/cognitive_mesh.py` and `core/security_scope.py`
imports any of them — verified by grep across `core/`, `tools/`, `aura/` and
`main.py`. In the vocabulary of this repository's own audits, M64 is
`EXISTS_NOT_WIRED`.

That is a deliberate stopping point, not an oversight:

* Wiring the orchestrator into `LLM.chat_stream` changes the behaviour of
  **every turn**, including the fast path. That is a materially larger and
  riskier change than an architecture milestone authorises, and it belongs to a
  milestone that can measure the latency and quality impact on a live turn.
* It is also not honestly testable in this environment: `core/llm.py` imports
  `openai`, which is not installed here, so 59 pre-existing broad-suite failures
  already come from that import. Wiring against a module that cannot be imported
  would produce a change nobody could exercise.
* The structural consequence is worth stating because it is exactly the defect
  this repository has caught before — `core/research_runtime.py` is attached at
  boot and has zero callers. M64 is in the same state **by choice and with it
  written down**, rather than by accident and undiscovered.

The intended wiring, for the milestone that does it: `assemble_task_decision()`
already runs on every turn, so `route_task()` takes that `TaskDecision` and adds
the specialist dimension; `CognitiveOrchestrator.plan()` is the entry point, and
`finish()` returns the `MeshAnswer` whose `answer` field is what the operator
reads. Everything below it — team execution, blackboard, tool broker, executor —
is already the machinery `chat_stream` uses today.


## 13 — The gauntlet

111 offline tests across two files. Not a candidate evaluation — it measures the
**architecture**. No model is loaded, no socket is opened, no holdout is touched.

Every active-security scenario uses loopback, RFC-1918 or documentation ranges
and synthetic fixtures only. Nothing names a public target, performs discovery,
or reaches a third-party system.

* `tests/test_mesh_gauntlet_v69_m64.py` — the 25 routing/behaviour scenarios.
* `tests/test_mesh_negative_security_v69_m64.py` — what the mesh must **refuse**.
  Every test attempts a bypass and asserts it failed.

---

## 14 — Performance

The fast path costs one deterministic `route_task()` call — pure Python, no model
and no I/O. Ordinary chat forms **no team**: measured across eight ordinary
prompts the mean specialist count is ≤ 1.5, and the three simplest take the fast
path with `max_tool_calls == 0`.
