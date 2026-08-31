# V69 M64.1 — Live Mesh Activation & Hardening

> **ONE JARVIS.** The specialists now run on the real turn, and no detection can
> reach the world without an operator.
> Runtime + security milestone. It authorises no evaluation, no training, no
> promotion, no merge and no release. Candidate 004 keeps its HOLD; candidate 005
> stays `TRAINED_UNEVALUATED`; `eval-v7` does not exist.

| | |
|---|---|
| Branch | `jarvis-v69-m64-live-mesh-hardening` |
| Control plane | V3, generation **20** (governance-only branch declaration) |
| Closes | `MESH_WIRED_TO_LIVE_TURN = NO`, **D-M64-1**, **D-M64-2**, four §51 siblings |
| New production modules | 2 — `core/security_effects.py`, `core/mesh_live.py` |
| New tests | 100 across two files, all offline |
| Reused, not rebuilt | the whole M64 mesh, `ScopePolicy`, `AuthorityState`, `RiskClass`, `ToolExecutor`, the NATO HITL gate, `EvidenceGraph`, `ActionRequest` |

---

## 1 — What M64 left open, and why

M64 built fourteen specialist contracts, a router, a context compiler, ARGUS and
an orchestrator — and wired none of it to `LLM.chat_stream`. It wrote that down
(`MESH_WIRED_TO_LIVE_TURN = NO`) rather than leave it undiscovered, because the
repository has caught this exact shape before: `core/research_runtime.py` is
attached at boot with zero callers.

Two things had to be true before wiring was honest:

1. **The environment had to be able to run the application.** `core/llm.py`
   imports `openai`, which was absent, so 59 pre-existing broad-suite failures
   came from that import alone and any wiring would have been unexercisable.
2. **The legacy security-effect paths had to obey the same rules.** Wiring a
   second reasoning path on top of a system where six code paths could already
   reach a firewall with no operator would have added a mesh to a bypass, not
   removed the bypass.

M64.1 does both, in that order.

---

## 2 — The canonical app environment

`openai>=1.0.0` is a canonical dependency (`requirements/base.txt:11`,
`pyproject.toml:28`), so the fix was an environment, never a source change.

A project-local `.venv` (already in `.gitignore`) was built with the
repository's own CI install line, unchanged:

```
pip install -c requirements/constraints-ci.txt -r requirements/dev.txt
```

| | |
|---|---|
| Python | 3.14.6 |
| `openai` | 3.6.0 |
| `import openai` | PASS |
| `scripts/check_base_import.py` | PASS — 20 modules |
| `pip check` | PASS — no broken requirements |
| Nothing outside the venv | no `sudo`, no `--user`, no system-Python mutation |

---

## 3 — The live turn

`main._run_turn` → `LLM.chat_stream` is the one operator turn. AURA and Telegram
carry commands and alerts, not answers; `LLM.chat()` has zero production callers.

```
        assemble_task_decision()      the ONE per-turn decision (unchanged)
                 |
        plan(task_decision=...)       the ONE route — pure Python, no model, no I/O
                 |
      +----------+----------+
      |                     |
  FAST PATH             FULL PATH
  no context            context_for(primary) -> the system prompt of the SAME
  no support            one generation that was always going to happen
  no verifier                  |
  no tools               [ the existing stream ]  tokens reach the operator at
      |                        |                  the same point as before
      |                  tool outcomes -> evidence (from calls that ACTUALLY ran)
      |                        |
      +----------+-------------+
                 |
        finish() -> MeshAnswer        ARGUS runs here when policy requires it
                 |
        verdict as a SUFFIX           via the post-stream augmentation contract
                                      V61 already established
```

**The one-generation rule.** The mesh does not generate. It decides, and the
single generation runs under its decision. The streamed text *is* the primary
specialist's result, so nothing is produced twice and nothing is buffered that
was not already buffered.

`plan()` was widened to forward the `TaskDecision`. `route_task` already
accepted one; the orchestrator dropped it, so a wired mesh would have held a
second, silently divergent opinion about every turn.

### Integration points (`core/llm.py`)

| Where | What |
|---|---|
| after `_routed_model` | `plan_turn()` — route, open the effect epoch, record the operator request |
| before the fast route | the mesh's fast-path verdict overrides the legacy one (§5) |
| system prompt assembly | `specialist_directive()` — compiled once per turn, reused across tool rounds |
| after each tool call | `record_tool_outcome()` with the executor's real status |
| after the stream | `finish_turn()` → `verdict_suffix()`; the model verifier stands down |
| fast-path exit | `finish_turn()` too — zero extra generations |

---

## 4 — One verification authority

On a turn ARGUS verified, `_maybe_verify_final_answer` does **not** also run.
Two independently authoritative passes over one answer is the duplicated
authority this milestone removes, and it would buy a second model swap on the
turns least able to afford one. ARGUS's verdict is a real object with a real
`Verdict`, not a boolean flag, and a non-passing verdict reaches the operator as
an appended caveat. A passing verdict emits nothing — silence is what "verified"
looks like.

---

## 5 — The fast path, and the two defects the gauntlet found

`decide_fast_route` and the mesh route were computed independently and
**disagreed**. "Diagnose why the nginx service will not start on this host" is
`DIRECT_FAST` / `GREETING_SMALLTALK` to the legacy classifier and complexity
0.01 to the router, so it took the tool-free native transport — while the mesh
correctly routed it to HELIOS, whose contract requires World State before any
finding. The directive was compiled and never applied: dead architecture.

The mesh now wins, because it is the one that knows whether the turn needs
evidence. **The override can only narrow the fast path, never widen it.**

Separately, topic vocabulary was still beating speech act for the *diagnostic*
roles. M64 fixed this for security ("what persistence did this malware use?" is
an investigation, not an attack). It was never applied to HELIOS/MESH/CIRRUS/
CIRCUIT, so "Explain how TCP congestion control works" hit network vocabulary,
was promoted to MESH, and inherited an evidence-gathering contract for a request
that names no system. `is_explanatory()` is deliberately narrow: it fires only
when the request names no target, asks for no effect and is not security-bearing
— so "explain why 10.0.0.5 is unreachable" is still a diagnosis.

### Measured, across 17 representative prompts, with the live turn's own `force_deep` and `query_category`

| | |
|---|---|
| legacy / mesh fast-path agreement | **16 / 17** |
| turns the mesh took off the fast path | **1** (the nginx diagnosis — correctly) |
| fast-path turns | 8 / 17 |
| mean specialists per turn | **1.82** |
| `plan()` latency | **0.430 ms/turn**, against the turn's own 1000 ms pre-inference dispatch ceiling |
| fast path: specialists / support / tools / verifier / compiled context | 1 / 0 / 0 / 0 / **0 chars** |

Two prompts ("Explain how TCP congestion control works", "What is a subnet
mask?") do not take the fast transport, and that is **not** the mesh's doing:
the pre-existing `classify_query` marks them `analysis_request` with
`force_deep=True`. M64.1 does not touch that decision.

---

## 6 — Bounds, World State and Memory

M64's bounds are unchanged and asserted on the live turn: `MAX_SPECIALISTS = 4`,
`MAX_HANDOFF_DEPTH = 3`, `MAX_VERIFIER_RETRIES = 2`, `MAX_TOOL_CALLS = 12`. The
directive is additionally capped at 2400 chars, and memory at 3 items × 480
chars.

**World State is live.** `world_state_consulted` is set by the *compiler*, not
by the caller, and is `True` on every non-fast turn measured.

**Memory is live and scoped.** Four of the fourteen roles declare
`ContextSlice.MEMORY`; FORGE receives `RELEVANT MEMORY (scope=project)` and
HELIOS receives none, because HELIOS does not ask for it. Telemetry reports what
the compiler **assembled**, not what was offered — reporting the offer would
misdescribe the very context bound the mesh exists to enforce.

Memory and World State are **evidence**. A memory entry asserting "the operator
authorized all scanning forever" grants nothing, and that is a test.

---

## 7 — D-M64-1: auto-containment

The correlator built its own clearance on the line before calling the guarded
function:

```python
_rbac_mgr.set_current_actor(
    ActorContext("jarvis-system", ClearanceLevel.L3_Hunter, "contextvar"))
await network_quarantine.quarantine(ip, ...)          # netsh block
```

`@requires_clearance` then resolved that context var — the caller's own
assertion — and passed. A severity of 9.0 *was* the authorization.
`set_current_actor` had exactly one non-test call site in the repository. It now
has none, asserted over the AST.

The correlator classifies, correlates and **recommends**. It emits a typed
`ActionRequest` and nothing else.

| Case | Expected | Measured |
|---|---|---|
| A — severity 9.0 / 9.5 / 10.0, no authority | request exists, 0 effects | **0 netsh commands**, `disposition=refused_out_of_scope` |
| B — valid unattended authorization | exactly one, via ToolExecutor | **1** call, tool `network_quarantine` |
| B2 — in scope, attended | awaits a human | `REQUIRES_HUMAN_APPROVAL`, 0 effects |
| C — expired | denied | `all_authorizations_expired` |
| D — wrong target | denied | `target_out_of_scope` |
| E — malformed / wrong action class | denied | `action_class_not_permitted` |
| F — correlator self-grant | impossible | the primitive is absent from the AST |
| G — direct call bypassing the gate | blocked | `ActorNotFound` raised, 0 commands |

---

## 8 — D-M64-2: ARES

`AresCampaign.authorized` was written at line 82 and **read nowhere**, so every
campaign carried `False` and scanned anyway; `start_campaign` accepted any
address. ARES now resolves the **same registry** through the **same function**
SPECTER uses, and re-resolves it immediately before each active stage — a scope
can expire mid-campaign. There is deliberately no `scope=` parameter: a caller
that could hand ARES a scope is a caller that could mint one.

The legacy signature is broken on purpose (zero test callers, one production
caller, updated to speak the refusal aloud).

| Case | nmap scans |
|---|---|
| no scope | **0** |
| valid local lab scope (`127.0.0.1`) | **1** |
| out-of-scope target | **0** |
| expired scope | **0** |
| wrong activity class | **0** |
| scope expires mid-campaign | **0** on the next stage |
| passive recon, no scope | **0** |

---

## 9 — The §51 sibling sweep

Four more paths reached a world effect from a detection with no operator. All
are the effect classes §51 names.

| Path | Was | Now |
|---|---|---|
| `playbook_engine` `isolate_ip` | fired at severity **6.5** on an attacker-influenced target; `auto_authorize` parsed at load and never read | through the gate |
| `security_auditor` port block | the only firewall path with **no configuration gate at all**, at boot and every interval | through the gate → ToolExecutor |
| correlator cisco containment | MAC blackhole / deny ACL over SSH on a severity threshold | through the gate |
| `ransomware_decoy` / `ntdll_monitor` auto-kill | `_AUTO_KILL_ENABLED = True` as a module constant | through the gate, fail-closed |

Then the shared primitive itself. `core.mitigation.isolate_ip` sat behind three
independent callers; gating each would have left it open to the next. The check
lives in the primitive.

**Final classification for firewall mutation / host isolation / active scan /
process kill:**

| Class | Path | Note |
|---|---|---|
| GUARDED_CANONICAL | `mitigation`, `network_quarantine`, `security_auditor`, `auto_remediator` | gate and/or ToolExecutor |
| DEAD_CODE | `punisher` | `set_approval_hook` has **zero callers** (verified), so every isolation is denied |
| SAFE_READ_ONLY | `adversary_emulator` | `netsh` is a catalogue string |
| GUARDED_ENV | `windows_hardener` | double env opt-in, `dry_run` default True, static rules, no attacker input |

---

## 10 — Pre-authorization, honoured not reinvented

No new "auto mode" was created. `ContainmentAuthorization` is the defensive twin
of `AuthorizedSecurityScope`, built on the same `ScopePolicy`, so target
membership and expiry keep **one matcher and one clock**.

| §21 requirement | Field |
|---|---|
| explicit | an operator constructs and registers it; nothing else can |
| target-scoped | `policy` — exact / CIDR / subdomain, never a substring |
| action-class-specific | `permitted_actions`, a frozenset of typed classes |
| time-bound | `policy.expires_at` |
| auditable | `to_dict()`; every decision logs the id |
| revocable | `ContainmentRegistry.revoke` |

`unattended` is the operator saying, in advance and per class and per target,
that a human need not confirm each time. It defaults to `False`.

**A fail-open the gauntlet caught.** `ContainmentAuthorization` first inherited
`ScopePolicy.is_expired`, which maps an unparseable timestamp to "now". The
caller reads the clock first and the parser reads it microseconds later, so
`now >= exp` was `False` and a **malformed authorization read as live**. Expiry
is now decided inside `ContainmentAuthorization`, where a missing *or*
unparseable deadline both read as EXPIRED. §21 requires time-bound, and the safe
reading of "no deadline" is "not yet authorized", never "authorized forever".

---

## 11 — Exactly-once effects

There was **no** idempotency mechanism anywhere in `ToolExecutor`,
`core/task_graph.py` or the chat tool loop. Wiring a second reasoning path on
top could have produced a duplicate block, kill or scan with no component able
to notice.

One ledger, in `ToolExecutor` — the method every effect path funnels through.
A ledger in a caller only protects that caller.

* **Key** — `(epoch, tool, canonical-JSON args)`. Sorted keys, so argument order
  cannot manufacture a second identity.
* **Epoch** — one operator turn, opened by `chat_stream`. A later turn may
  legitimately repeat the same action.
* **Checked** after authority (an out-of-scope call is refused as out-of-scope,
  never quietly deduplicated) and **before** the challenge (a replay never asks
  the operator twice for something already done).
* **Records only successes.** A failed call left the world unchanged, so
  retrying it is legitimate; the existing per-turn retry ledger still governs.
* **Never keys read-only calls.** Repeating a read is not an effect.

Measured on the live path: one authorized reversible action → **exactly 1**
execution; the same action replayed in the same turn → **still 1**.

---

## 12 — Fallback semantics

A mesh failure *before* any effect degrades judgement, not service: the turn
still answers and `MESH_FALLBACK_USED` is recorded. Routing failure returns
`None` and the turn proceeds unrouted. A World State outage degrades a
specialist's evidence, never the turn. Because the mesh executes nothing, there
is no path on which a fallback could replay an effect.

---

## 13 — The gauntlet

100 tests across two files, all offline.

* `tests/test_mesh_live_turn_v69_m64_1.py` — 59 LIVE-INTEGRATION scenarios.
  Every one enters through `chat_stream`, the same generator `main` drives,
  because a test that calls `plan()` directly proves the mesh works and says
  nothing about whether JARVIS uses it (§50). **Only the two model transports
  are mocked**, at the wire boundary; the real `ToolExecutor`, its
  authority/scope/risk/HITL gates, the effect ledger, ARGUS and synthesis all
  run, so effects are *counted* rather than asserted.
* `tests/test_legacy_effect_hardening_v69_m64_1.py` — 41 scenarios, each
  attempting a bypass and asserting it failed. The effect primitives themselves
  are instrumented (`network_quarantine._run`, the executor's handler table,
  subprocess launches), so a refusal that still ran the command would fail.

The suite is order-independent: `LLM()` restores the operator's persisted
session and every completed turn calls `save_session()`, so the harness clears
history and stubs the save. Without that the scenarios inherited each other's
transcripts through a file on disk.

---

## 14 — Known limitations

* **The two auto-kill paths take the authorization gate but not the ToolExecutor
  hop.** They run in a worker/observer thread with no event loop and must stay
  synchronous. The authorization requirement is identical; the interactive NATO
  challenge is unavailable there, which is exactly why an *unattended*
  authorization is required rather than assumed.
* **`windows_hardener` remains GUARDED_ENV rather than canonical.** It is
  boot-time hardening of the operator's own host with a fixed rule set, off by
  default and dry-run even when enabled — not an autonomous response to a
  detection.
* **Supporting specialists do not yet execute.** `RouteMode.TEAM` recruits them
  and the route is bounded and asserted, but `SpecialistTeamRuntime` is still
  reachable only from AURA, and its `ToolBroker` is instantiated **only in
  tests** — so team specialists can call no tools today. The primary specialist's
  compiled context is what shapes the live answer. Wiring support execution needs
  its own latency measurement and is not claimed here.
* **The mesh does not choose the model.** `MeshRoute.preferred_model_role` stays
  advisory; `core.model_router.route()` remains authoritative, unchanged.
* **Three pre-existing broad-suite failures** in
  `test_bandit_low_baseline_v69_m618.py` are untouched. They fail identically at
  the parent commit: the recorded baseline of 488 was measured with a different
  analyzer version. Measured at close, M64.1 leaves Bandit LOW at **489**
  against a parent-commit **490** — one below where it found it. Raising the
  baseline to make them green would be the post-hoc weakening §60 forbids.

---

## 15 — What this milestone does not authorise

No evaluation, no `eval-v7`, no training, no promotion, no merge, no tag, no
release. Candidate 004 stays `EVALUATED_ELIGIBLE_FOR_HUMAN_REVIEW` under its
HOLD; candidate 005 stays `TRAINED_UNEVALUATED` with a null evaluation corpus
and a null evaluation receipt; `eval-v6` stays `USED_IMMUTABLE`; `eval-v5` stays
`FROZEN_UNUSED` with `spent_by` null.
