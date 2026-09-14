# V69 M66A — EPISTEMIC DELIBERATION & ANSWER ORCHESTRATION

Runtime engineering only. No training, no candidate006, no eval-v8, no promotion,
no production L2/L3, no scope grant, no tag/release/deploy. candidate004 stays
HOLD, candidate005 stays HOLD_FOR_RESEARCH, eval-v7 USED_IMMUTABLE.

Branch `jarvis-v69-m66a-epistemic-deliberation`, from master
`cfa23bb548b98204e88b03ccb2a82c4eee4bf761` (M65D integrated, Control Plane V4
generation 36).

## 0. THE ONE SENTENCE

JARVIS already computed one per-turn decision; M66A adds the dimensions it was
missing — intent, ambiguity, freshness, premise state, evidence obligation,
verification obligation, deliberation depth, tool need, delivery timing and
success-claim permission — onto that same decision, and makes one rule true of
all of them: **within a turn, policy may only strengthen, and substantive output
that requires a fail-closed verdict is never shown as fact before the verdict is
known.**

## 1. WHAT WAS THERE BEFORE (see `V69_M66A_EXISTING_DECISION_MAP.md`)

The live path (`LLM.chat_stream`) computed, per turn: `classify_query` (category
+ force_deep), `classify_request` (TurnPolicy: request class, M54.6 verify
matrix, vault gate), `assemble_task_decision` (TaskDecision: domain, role,
complexity, security, planning/tools/team advisories), then `mesh_live.plan_turn`
(MeshRoute: owner, autonomy ceiling, effectful/offensive/target, required
evidence), then delivery: **every substantive token streamed before any
verifier ran**, and verification "status" was inferred as
`final_answer == draft_answer`. The decision map catalogues all 34 concerns, the
five overlapping deliberation signals, and the classifier drift M66A closed.

## 2. ONE CANONICAL DECISION PLANE (§4)

`TaskDecision` remains the one per-turn object. M66A adds a single `epistemic`
component (`EpistemicDecision`) and carries the `turn_policy` it composed with.
`CANONICAL_TURN_DECISION_COUNT = 1`. No `TaskDecision2`, no second router,
classifier, evidence store, verifier or renderer. Legacy classifier APIs remain
for compatibility and none overrides the canonical field
(`test_the_task_decision_carries_exactly_one_epistemic_component`, and the
ownership table in the decision map).

## 3. THE NEW TYPED AXES (`core/epistemic_deliberation.py`, pure & deterministic)

  * **IntentFrame** (§9) — goal / deliverable / effect_requested / target /
    constraints / confidence (LOW/MEDIUM/HIGH, never a fake probability).
    Deterministic; a greeting adds **zero** model calls (§10, §23).
  * **AmbiguityAssessment** (§11) — PROCEED / PROCEED_WITH_ASSUMPTION /
    CLARIFICATION_REQUIRED + the smallest blocking question. Clarification is
    reserved for security-scope / target / destructive / irreversible ambiguity.
  * **FreshnessRequirement / FreshnessStatus** (§14) — STABLE /
    FRESHNESS_PREFERRED / FRESHNESS_REQUIRED; freshness is not web-only (a
    repository/host/API observation satisfies it). If REQUIRED and unsatisfied,
    the success-claim gate blocks any current-state claim.
  * **PremiseState** (§12) — a MAPPING over `mesh_contracts.ClaimStatus` +
    provenance, not a second evidence store.
  * **EvidenceQuality** (§17) — a 0–5 factual-reliability rank over `Provenance`,
    a **different axis** from `TrustOrigin` (control influence). Evidence beats an
    unsupported model prior (§18).
  * **DeliberationMode** (§20) — DIRECT / SINGLE_ANALYSIS / PLAN_SINGLE / TEAM.
    **Planning is not team** (§21): code-mod, architecture and roadmaps are
    PLAN_SINGLE; TEAM is reserved for genuinely cross-domain work.
  * **ToolNeed** (§19) — NONE / OPTIONAL / REQUIRED, derived from intent /
    freshness / domain, **never** from a tool merely being registered;
    `ToolExecutionState` is the separate execution axis.
  * **VerificationStatus** (§26) — NOT_REQUIRED / VERIFIED /
    VERIFIED_WITH_LIMITATIONS / NOT_VERIFIED / HUMAN_REVIEW_REQUIRED /
    FALLBACK_AUDITED / BLOCKED, derived from the ARGUS verdict or the model
    verifier's `VerificationResult` — **never** from string equality.
  * **DeliveryPolicy** (§27/§28) — STREAM_DIRECT / STAGED_VERIFY /
    BUFFER_UNTIL_VERIFIED, from the composed verification policy.
  * **EffectTruth** (§30) — a mirror of `effect_journal.ExternalOutcome`;
    UNKNOWN → INDETERMINATE, never rounded to a certainty; INDETERMINATE
    dominates aggregation.
  * **SuccessClaimPermission** (§29) — ALLOWED / QUALIFIED / BLOCKED, from
    verification status + effect truth + freshness + tool denial.
  * **AnswerContract** (§31) and **PhaseMachine** (§6, forward-only, bounded).

## 4. THE POLICY RATCHET (§7, §35, §37)

`PolicyRatchet` exposes exactly one mutator, `consider`, which RAISES a level and
records the transition; there is **no** lowering method. A proposal at/below the
current level is a no-op; a proposal from any origin other than
OPERATOR_INPUT / TRUSTED_SYSTEM is ignored entirely (untrusted content can
neither strengthen nor weaken). `EpistemicDecision.strengthened` folds the mesh's
facts in after routing — the ratchet **on the live turn**: decision v1 (pre-mesh)
→ mesh discovers effectful/offensive → v2, observably, never weaker. Monotonicity
is a property of the type, mirroring `RoleSelection.grants_authority`.

## 5. FAIL-CLOSED DELIVERY (§27), THE PRIMARY FEATURE

`chat_stream` reads `decision.delivery_policy`:

  * STREAM_DIRECT / STAGED_VERIFY — unchanged from M64.1 (stream, then a verdict
    suffix). The fast path is untouched; a buffered turn never takes the native
    transport.
  * BUFFER_UNTIL_VERIFIED — the substantive draft is accumulated, **not shown**.
    After ARGUS / the model verifier runs, `_m66a_finalize` decides: on a passing
    verdict the verified answer is delivered (with any limitation caveat); on a
    failing verdict the draft is **withheld as an established result** and shown
    only under an explicit UNVERIFIED banner. A verifier timeout fails closed
    (HUMAN_REVIEW_REQUIRED → withheld); cancellation stops cleanly; no unbounded
    buffering (one turn's generation, bounded by the existing turn budget).

`UNVERIFIED_REQUIRED_FAIL_CLOSED_DRAFT_VISIBLE = NO`.

## 6. SUCCESS-CLAIM GATE (§29) AND M65D TRUTH (§30)

The live tool loop now passes `effect_note` to `ToolExecutor.aexecute` /
`aexecute_mcp` and aggregates the `external_outcome` values — so an INDETERMINATE
effect reaches delivery as M65D wrote it. The gate blocks a success claim when
verification failed, the effect outcome is unknown, a required freshness is
unmet, or a relied-on tool was denied. On a streamed turn a blocked but asserted
success gets a correction suffix; M66A never rewrites INDETERMINATE to FAILED,
NOT_EXECUTED or SAFE_TO_RETRY.

## 7. RESPONSE-SURFACE TRUTH (§32)

`response_surface.render` gained an `epistemic_marker` argument. The marker is
placed FIRST and its length is RESERVED from every bounded surface's budget, so
HUD / NOTIFICATION / VOICE can shorten prose but can never truncate the warning
away. "It succeeded" can never survive a lossy surface without its warning.

## 8. WHAT M66A DID NOT DO

No live model-assisted intent extraction; no live re-author correction pass (the
bound `MAX_CORRECTION_PASSES = 1` is typed and tested, but the CPU host does not
spend a second generation); no persisted chain-of-thought (only body-safe enum
metadata); no change to the authority chain, ToolBroker, ToolExecutor, effect
journal, scope, HITL, or ARGUS's own logic; `AgentTeamSelector.should_form_team`
dropped the `requires_planning` term but is otherwise intact.

## 9. VERIFICATION

Focused suites: `test_epistemic_deliberation_v69_m66a.py` (golden matrix,
metamorphic monotonicity, counterfactual, adversarial isolation, phase machine,
surface preservation), `test_epistemic_live_turn_v69_m66a.py` (buffering,
fail-closed delivery, success gate, effect truth on the REAL `chat_stream`), and
`test_epistemic_mutation_campaign_v69_m66a.py` (51 load-bearing mutations, 0
survivors, plus the ≥50 count assertion and the graceful-degradation resilience
test). Regression firewall: the M64.1 / M65A / M65B live gauntlets, the M64 mesh
gauntlet, M65D effect semantics, verification, injection firewall, skill
profiles, task domain and agent runtime suites all stay green. Full authoritative
and scientific counts and the CI evidence are recorded in PROGRESS.md §10 and the
control-plane snapshot at close.
