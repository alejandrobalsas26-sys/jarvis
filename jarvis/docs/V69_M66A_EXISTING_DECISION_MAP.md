# V69 M66A — EXISTING DECISION MAP

Produced BEFORE any M66A implementation, from repository reality at master
`cfa23bb548b98204e88b03ccb2a82c4eee4bf761` (M65D integrated, Control Plane V4
generation 36, PROBLEMS 0). Every row names the code that owns a per-turn concern
today, what it produces, who reads it, whether it is authoritative, and what M66A
does with it.

Dispositions: **REUSE** (consumed unchanged) · **EXTEND** (new fields/parameters,
existing behaviour byte-identical for existing callers) · **CONSOLIDATE** (two
signals become one canonical owner) · **DEPRECATE_COMPATIBLY** (API stays,
authority moves) · **LEAVE_UNCHANGED**.

## 0. The live decision flow before M66A

`LLM.chat_stream` (core/llm.py) computes, per operator turn, in this order:

```
maybe_bypass()                     deterministic answers (time/date/lifecycle)  -> return
classify_query()                   (query_category, force_deep)      cognitive_optimizer
classify_request()                 TurnPolicy (request_class, verify_policy,
                                   knowledge_vault_allowed, security)  turn_policy  [M54.3/.6]
TurnBudget(budget_for(policy))     one turn deadline                  turn_budget
assemble_task_decision()           TaskDecision  <- route_turn() (ModelDecision)
                                                 <- classify_domain() (DomainSignal)
                                                 <- is_security_sensitive_turn()
mesh_live.plan_turn()              MeshRoute (primary, supporting, verifier_required,
                                   autonomy ceiling, CLARIFY question)  mesh_router
mesh_live.run_specialists()        DIRECT / ONE_SPECIALIST / TEAM       [M65A/M65B]
decide_fast_route()                native no-think transport or /v1     fast_path
  (mesh overrides a native route the mesh says needs evidence)
select_contract()/budget_for_shape response shape + token budget       [M57]
[ generation streams token-by-token to the operator ]                  yield delta.content
tool loop: validate_tool_call -> ToolExecutor.aexecute -> record_tool_outcome (evidence)
finish_turn()                      ARGUS when route.verifier_required; MeshAnswer
verdict_suffix()                   APPENDED to the already-streamed text
_maybe_verify_final_answer()       model verifier (staged, POST-stream), APPENDS notice
_maybe_broadcast_response()        verified = (final_answer == draft_answer)
```

Three facts about this flow are load-bearing for M66A:

1. **Every substantive token is streamed before any verifier runs.** Both ARGUS
   (`finish_turn`) and the model verifier (`_maybe_verify_final_answer`) operate on
   `full_text` after the stream closed; their output is a *suffix*. A
   REQUIRED_FAIL_CLOSED policy therefore cannot be honoured today — the draft is
   already visible.
2. **Verification status is inferred from string equality.**
   `AssistantResponseEvent.verified = (final_answer == draft_answer)`; a verifier
   that never ran and a verifier that passed are indistinguishable.
3. **The live tool loop calls `aexecute(tool, args, thinking)` without
   `effect_note`.** M65D's `external_outcome` / `reconciliation_required` are
   produced by the executor and consumed by the M65A specialist path, but the
   primary live turn never reads them, so an INDETERMINATE effect reaches the
   operator only as a `FAILURE` ToolCallStatus in the evidence graph.

## 1. The map

| # | Concern | Canonical current owner | Current type / signal | Producer | Consumer | Authoritative? | Behaviour-affecting? | Duplication discovered | M66A disposition |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Per-turn decision object | `core.agent_runtime.TaskDecision` | frozen dataclass: model_decision, domain, complexity, security_sensitive, query_category, force_deep, requires_verification, requires_planning, requires_tools, prefers_agent_team, preferred_model_role, response_surface, reason | `assemble_task_decision()` once per turn | `chat_stream`, `mesh_router.route_task`, `AgentTeamSelector`, telemetry | authoritative for model role/verify (via ModelDecision); the planning/tools/team booleans are **advisory** | yes | `MeshRoute` re-derives effectful/risk/verifier_required/urgency from the message; `TurnPolicy` re-classifies request class + verify policy from the message | **EXTEND** — add the M66A typed components as fields (intent, ambiguity, freshness, evidence_policy, verification_policy, deliberation_mode, tool_need, delivery_policy, turn_policy, trace). Stays the ONE canonical object. |
| 2 | Model role selection | `core.model_router.route()` via `agent_runtime.route_turn()` | `ModelDecision(role, provider, model, complexity, reason, requires_verification)` | `route_turn` | `chat_stream` (model), `decide_fast_route`, verifier gating | **authoritative** for role | yes | `TaskDomain.preferred_role` and `SkillProfile.preferred_model_role` are advisory hints only; `ModelRoleRouter` selects for *specialists* only | **REUSE** — role stays pure model selection; no new role. |
| 3 | Complexity | `core.model_router.calculate_complexity()` | float 0..1 in `ModelDecision.complexity` | `route()` | `TaskDecision.complexity`, mesh thresholds, `AgentTeamSelector` | authoritative | yes | none (single function) | **REUSE** |
| 4 | Security sensitivity | `core.model_router.is_security_sensitive_turn()` | bool | called in `route_turn`, `assemble_task_decision` (twice), `classify_request`, `_maybe_verify_final_answer` | routing, verify gating, turn policy | authoritative | yes | computed 4× per turn from the same inputs (pure, identical) | **REUSE** — M66A reads `TaskDecision.security_sensitive`; the repeated calls are harmless and left. |
| 5 | Semantic domain | `core.task_domain.classify_domain()` | `DomainSignal(domain, confidence, preferred_role, requires_planning, prefers_agent_team, reason, matched)` | `assemble_task_decision`, `classify_request` (second call) | TaskDecision, TurnPolicy, mesh primary selection, SkillProfile lookup | authoritative for domain | yes | `_PLANNING_DOMAINS` / `_AGENT_TEAM_DOMAINS` overlap with `mesh_router` support logic and `AgentTeamSelector` | **REUSE** for domain; the two boolean advisories are **DEPRECATE_COMPATIBLY** — kept on DomainSignal/TaskDecision, no longer the authority for team formation (see #16). |
| 6 | Query category / force_deep | `core.cognitive_optimizer.classify_query()` | `(category:str, force_deep:bool)` | `chat_stream` | `route_turn` escalation FAST→DEEP; `TaskDecision.query_category` | escalation-only authority | yes (FAST→DEEP) | overlaps `TurnPolicy.request_class` and M66A intent | **LEAVE_UNCHANGED** — remains the escalation input; M66A intent does not replace it. |
| 7 | Request class + vault gate + verify matrix | `core.turn_policy.classify_request()` | `TurnPolicy(request_class, reason_code, verify_policy(M54.6), security_sensitive, knowledge_vault_allowed)` | `chat_stream` before `assemble_task_decision` | fast route, budget, `filter_tools`, verifier skip, response contract | authoritative for vault access and verifier skip | yes | **drift**: its `verify_policy` (6 values) and `SkillProfile.verification_policy` (4 values) and `ModelDecision.requires_verification` and `MeshRoute.verifier_required` are four verification signals | **CONSOLIDATE** — `TurnPolicy` is carried INSIDE `TaskDecision` (`turn_policy` field) and its verify matrix is mapped onto `VerificationPolicy` as the floor of the M66A composition. `classify_request` keeps its API. |
| 8 | Fast transport selection | `core.fast_path.decide_fast_route()` | `FastRouteDecision(use_native, reason, ...)` | `chat_stream` | native vs /v1 transport | authoritative for transport only; mesh may narrow | yes | `mesh.is_fast` is a second fast-path definition; M64.1 already made the mesh win | **REUSE** — M66A adds one more narrowing only: BUFFER_UNTIL_VERIFIED or FRESHNESS_REQUIRED or CLARIFICATION_REQUIRED never takes the native transport. Never widens. |
| 9 | Mesh route / specialist ownership / autonomy ceiling | `core.mesh_router.route_task()` via `mesh_orchestrator.plan` / `mesh_live.plan_turn` | `MeshRoute` (mode, primary, supporting, verifier_required, autonomy_ceiling, effectful, risk, security_intent, offensive_intent, target_scope, required_evidence, clarifying_question) | `plan_turn` | `run_specialists`, directive compilation, `finish_turn`, effect epoch | authoritative for specialist ownership and ceiling (ceiling = min of everything) | yes | re-derives `effectful` (own `_EFFECT_SIGNALS`), `risk`, `urgency`, `verifier_required` from the message; `CLARIFY` produces a `clarifying_question` that never reaches the operator | **REUSE** — consumes TaskDecision (already). M66A reads `route.effectful`/`offensive_intent`/`target_scope` as turn facts to strengthen policy (ratchet), and `mesh_live.team_route` honours the canonical deliberation ceiling (#16). |
| 10 | Team route (DIRECT / ONE_SPECIALIST / TEAM) | `core.mesh_live.team_route()` + `run_specialists()` | `TeamRoute` | live turn | specialist execution fabric (M65A/M65B) | authoritative for what RUNS | yes | `AgentTeamSelector.should_form_team` (V63 M4) is a second team-formation authority, unused by the live turn but tested | **EXTEND** — `team_route` caps at the TaskDecision `deliberation_mode` ceiling; TEAM only when the canonical decision is TEAM. |
| 11 | V63 team selector | `core.specialist_runtime.AgentTeamSelector.should_form_team()` | bool = prefers_agent_team OR **requires_planning** OR complexity ≥ 0.75 | callers of `run_team_for_decision` (main.py attach only) | V63 runtime | advisory | not on the live turn | **planning ⇒ team coupling** (§21) | **CONSOLIDATE** — reads `TaskDecision.deliberation_mode`; `requires_planning` alone never forms a team. Legacy objects without the field fall back to `prefers_agent_team` only. |
| 12 | Evidence policy (role contract) | `core.skill_profiles.EvidencePolicy` (NONE/RECOMMENDED/REQUIRED/REQUIRED_AUTHORITATIVE) on `SkillProfile` | enum | `SkillProfileRegistry.for_domain()` | `AgentTeamSelector` (verifier add), evals | contract for a role's outputs | indirectly | `core.cognitive_mesh.EvidencePolicy` (NONE_REQUIRED/CITE_ON_CLAIM/EVIDENCE_REQUIRED) on `SpecialistRecord` is a second enum for the same idea, read by ARGUS `_needs_evidence` and `mesh_router._required_evidence` | **REUSE** `skill_profiles.EvidencePolicy` as the turn-level lattice; the mesh enum is mapped INTO it (NONE_REQUIRED→NONE, CITE_ON_CLAIM→RECOMMENDED, EVIDENCE_REQUIRED→REQUIRED) and left unchanged for its own consumers. |
| 13 | Verification policy (role contract) | `core.skill_profiles.VerificationPolicy` (NONE/OPTIONAL/REQUIRED/REQUIRED_FAIL_CLOSED) | enum | `SkillProfile` | `AgentTeamSelector` (adds VERIFIER); `fail_closed_verification` read by nothing on the live turn | contract | **not live-enforced** | see #7 | **REUSE** as the turn-level lattice; composed monotonically with turn facts; **enforced on the live turn** by the delivery policy. |
| 14 | Model verifier | `core.verification.verify_answer()` + `deterministic_precheck()` + `should_verify()` | `VerificationResult(verified, confidence, issues, reasoning, needs_human_review)` | `LLM._maybe_verify_final_answer` | suffix on streamed text; HUD event | authoritative verdict on prose (fail-closed on error/timeout/cancel) | yes | ARGUS is a second verifier; M64.1 already enforces ONE per turn (`should_run_llm_verifier`) | **REUSE** — unchanged; M66A maps its result onto an explicit `VerificationStatus` and decides *when* it may be shown. |
| 15 | Deterministic mesh verifier | `core.mesh_verifier.verify()` (ARGUS) | `VerifierVerdict(verdict, reasons, limitations, ...)`, `grants_authority=False` | `orchestrator.finish` when `route.verifier_required` | `verdict_suffix`, `MeshAnswer.verifier_status` | authoritative structural verdict; cannot grant | yes | none | **REUSE** — `finish`/`verify_task` gain an `indeterminate_effects` parameter so the LIVE turn hands ARGUS the M65D truth the M65A path already hands it. |
| 16 | Deliberation depth | none — split across `TaskDecision.requires_planning`, `DomainSignal.prefers_agent_team`, `MeshRoute.mode`, `TeamRoute`, `AgentTeamSelector` | booleans + two enums | various | various | none is canonical | yes | **five overlapping signals** | **NEW component on TaskDecision**: `DeliberationMode` (DIRECT / SINGLE_ANALYSIS / PLAN_SINGLE / TEAM). Planning and team are separate values. |
| 17 | Intent (what outcome the operator wants) | none — nearest are `TurnPolicy.request_class` (interaction class), `mesh_router.classify_security_intent` (security speech act), `is_explanatory` | — | — | — | — | — | request class ≠ intent | **NEW component on TaskDecision**: `IntentFrame` (goal, deliverable, constraints, effect_requested, target, assumptions, confidence LOW/MEDIUM/HIGH). Deterministic; no model call. |
| 18 | Ambiguity | `mesh_router` `ambiguous` (routing-confidence based) → `RouteMode.CLARIFY` + `clarifying_question` (not surfaced) | route mode | `route_task` | `MeshAnswer.clarifying_question` only | advisory in practice | no (question never delivered) | — | **NEW component on TaskDecision**: `AmbiguityDisposition` (PROCEED / PROCEED_WITH_ASSUMPTION / CLARIFICATION_REQUIRED) + smallest blocking question; the live turn DELIVERS it for effect-critical ambiguity. Mesh CLARIFY left unchanged. |
| 19 | Freshness | none (`fetch_webpage`/`web_search` availability only) | — | — | — | — | — | — | **NEW component on TaskDecision**: `FreshnessRequirement` (STABLE / FRESHNESS_PREFERRED / FRESHNESS_REQUIRED) + per-turn `FreshnessStatus` (source, satisfied). |
| 20 | Premise epistemics | `core.mesh_contracts.ClaimStatus` (UNVERIFIED/OBSERVED/INFERRED/DISPUTED/VERIFIED/REJECTED) on `Claim`, derived from bound `EvidenceRef` provenance | enum | `EvidenceGraph.add_claim` | ARGUS | authoritative for claims | yes | — | **REUSE** — M66A `PremiseState` (OBSERVED/SUPPORTED/INFERRED/ASSUMED/UNKNOWN/CONTRADICTED) is a MAPPING of ClaimStatus + provenance, not a second store; premise records are bounded and never persisted. |
| 21 | Evidence provenance / quality | `core.mesh_contracts.Provenance`, `EvidenceRef.corroborating`, `CORROBORATING_PROVENANCE` | enum + property | `record_tool_outcome`, `record_operator_request`, specialists | ARGUS, adjudicate | authoritative | yes | — | **REUSE** — M66A adds an `EvidenceQuality` ranking over Provenance for fact resolution (observation > primary source > operator > secondary > model prior). No EvidenceStore2. |
| 22 | Trust origin (control influence) | `core.injection_firewall.TrustOrigin` (+ `may_influence_tools`, `trusted`) | enum | `_label_tool_result`, mesh_context | firewall, tool loop | authoritative | yes | — | **REUSE** — control-plane inputs to the M66A ratchet are typed by `TrustOrigin`; only `TRUSTED_SYSTEM`/`OPERATOR_INPUT` may strengthen from content, nothing may weaken. Trust ≠ evidence quality kept as two enums. |
| 23 | Tool availability vs need vs execution | `assemble_task_decision`: `requires_tools = bool(tool_names) or domain in _TOOL_HEAVY_DOMAINS` (availability ⇒ need); `TurnPolicy.filter_tools` (availability); `ToolCallStatus` (execution) | bool / list / enum | — | telemetry, mesh fast eligibility (`not tool_names`) | advisory | mesh: yes | **availability conflated with need** (§19) | **CONSOLIDATE** — `ToolNeed` (NONE/OPTIONAL/REQUIRED) derived from intent/freshness/domain, never from `tool_names`; `requires_tools` kept as a compatibility mirror of `ToolNeed is REQUIRED`; execution stays `ToolCallStatus`. |
| 24 | Effect truth | `core.effect_journal.ExternalOutcome` (PROVEN_COMMITTED / PROVEN_NOT_EXECUTED / UNKNOWN) via `aexecute(effect_note=)` | dict keys `external_outcome`, `reconciliation_required`, `disposition` | ToolExecutor | M65A `ToolReceipt.recovery_required` → ARGUS `indeterminate_effects` | **authoritative** | yes (specialist path); **not read on the live turn** | — | **REUSE** — the live tool loop passes `effect_note`, aggregates an `EffectTruth`, and hands ARGUS + the AnswerContract the same fact. M66A never rewrites UNKNOWN. |
| 25 | Verification status (what actually happened) | none — inferred as `final_answer == draft_answer` | bool | `_maybe_broadcast_response` | HUD | — | yes (HUD) | — | **NEW typed `VerificationStatus`** (NOT_REQUIRED / VERIFIED / VERIFIED_WITH_LIMITATIONS / NOT_VERIFIED / HUMAN_REVIEW_REQUIRED / FALLBACK_AUDITED / BLOCKED) derived from ARGUS verdict or `VerificationResult`, never from string equality. |
| 26 | Delivery timing | implicit: always stream; verifier appends | — | `chat_stream` | operator | — | yes | — | **NEW `DeliveryPolicy`** (STREAM_DIRECT / STAGED_VERIFY / BUFFER_UNTIL_VERIFIED) derived from `VerificationPolicy`; `chat_stream` buffers `delta.content` under BUFFER_UNTIL_VERIFIED and releases only a verified answer or a bounded failure status. |
| 27 | Success-claim permission | none (verifier prose) | — | — | — | — | — | — | **NEW `SuccessClaimPermission`** derived from verification status + effect truth + tool execution + freshness; bounded lexical detection of success vocabulary in the draft; blocked claims are corrected with an explicit status line. |
| 28 | Answer contract | none (`MeshAnswer` is the mesh's synthesis record; `AssistantResponseEvent` is the HUD event) | — | — | — | — | — | — | **NEW `AnswerContract`** (body-safe): surface, freshness status, evidence policy + satisfied, verification policy + status, effect truth, uncertainty_required, success permission, citation requirement, detail. Built once before rendering. |
| 29 | Presentation surface | `core.response_surface.ResponseSurface` + `render()` | enum + pure renderer | `chat_stream` (TEXT), `main._run_turn` (VOICE), HUD | TTS, HUD, notifications | authoritative for presentation | yes | — | **EXTEND** — `render()` gains `epistemic_marker=`; lossy surfaces (HUD / NOTIFICATION / VOICE) reserve budget for the marker so a critical warning survives truncation and first-sentence extraction. No second renderer. |
| 30 | Correction loop | none on the live turn (verifier appends; no re-author); `MeshBudget.max_verifier_retries` for the mesh | — | — | — | — | — | — | **NEW bounded constant** `MAX_CORRECTION_PASSES = 1` in the answer contract; M66A does not add a live re-author pass (that would be a second generation on a CPU host); the bound is typed and tested so a future pass cannot loop. |
| 31 | Resource pressure | `TeamExecutionPolicy.under_pressure` (concurrency only); `TurnBudget.can_afford_verifier` (skips the verifier with a human-review notice); `resource_aware_timeout` | — | — | — | — | yes | budget exhaustion currently returns the DRAFT + notice | **REUSE + tighten** — pressure may reduce concurrency/timeouts; under BUFFER_UNTIL_VERIFIED an unaffordable verifier yields HUMAN_REVIEW_REQUIRED and the draft is withheld, never silently delivered. |
| 32 | Injection firewall on tool/web/file/OCR/MCP content | `core.injection_firewall.apply_firewall` in `_label_tool_result` | `FirewallResult` | tool loop | history (data envelope) | authoritative | yes | — | **REUSE** — M66A's policy inputs are never parsed from tool content; a `TrustOrigin` other than OPERATOR/SYSTEM cannot produce a ratchet transition. |
| 33 | Security execution chain | `mesh_router` security intent → `AuthorizedSecurityScope` → capability registry → `ToolBroker` → risk/HITL → `ToolExecutor` → effect journal | as documented in M64/M65 | — | — | authoritative | yes | — | **LEAVE_UNCHANGED** — M66A has no scope-grant function; deliberation never raises the ceiling. |
| 34 | Observability | `MeshTurn.telemetry`, `RoleSelection.counters`, `ExecutionCounters`, `verifier_latency_stats` | dicts | — | HUD / doctor | — | no | — | **EXTEND** — bounded M66A counters (`deliberation_counters()`), body-free. |

## 2. Classifier drift, and the canonical ownership M66A establishes

| Dimension | Canonical owner after M66A | Legacy signal kept for compatibility |
|---|---|---|
| semantic domain | `TaskDomain` (`classify_domain`) | — |
| model role | `ModelDecision.role` (`route()`) | `DomainSignal.preferred_role`, `SkillProfile.preferred_model_role` (hints) |
| complexity | `ModelDecision.complexity` | — |
| security sensitivity | `TaskDecision.security_sensitive` (`is_security_sensitive_turn`) | `TurnPolicy.security_sensitive` (same function) |
| intent | `TaskDecision.intent: IntentFrame` (new) | `TurnPolicy.request_class`, `classify_query` category |
| ambiguity | `TaskDecision.ambiguity` (new) | `MeshRoute.mode is CLARIFY` |
| freshness | `TaskDecision.freshness` (new) | — |
| evidence obligation | `TaskDecision.evidence_policy` (composed) | `SpecialistRecord.evidence_policy`, `MeshRoute.required_evidence` |
| verification obligation | `TaskDecision.verification_policy` (composed) | `TurnPolicy.verify_policy`, `ModelDecision.requires_verification`, `MeshRoute.verifier_required`, `TaskDecision.requires_verification` |
| deliberation depth | `TaskDecision.deliberation_mode` (new) | `requires_planning`, `prefers_agent_team`, `AgentTeamSelector.should_form_team` |
| tool need | `TaskDecision.tool_need` (new) | `requires_tools` (mirror) |
| delivery | `TaskDecision.delivery_policy` → `AnswerContract` (new) | — |

The legacy signals remain readable; none may override the canonical field, and the
tests in `tests/test_epistemic_deliberation_v69_m66a.py` pin that.

## 3. What M66A will NOT build

`TaskDecision2`, `DecisionEnvelope2`, `CognitiveRouter2`, `ModelRouter2`,
`DomainClassifier2`, `SecurityRouter2`, `EvidenceGraph2`, `EvidenceStore2`,
`VerificationEngine2`, `AgentRuntime2`, a second renderer, a new `ModelRole`, a
scope-grant function, a model-assisted intent call on the live path, a live
re-author pass, or any persisted reasoning text.
