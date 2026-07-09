# JARVIS V67 — Phase 0 Architecture Map (reconnaissance deliverable)

> Built by tracing the **live runtime** (not milestone names/comments). Proves what
> exists so V67 **extends** the spine and never forks a parallel system.
> Branch: `jarvis-v67-field-intelligence` off master @ V66.1.

## 0. The operational spine (canonical path — do not duplicate)

```
SOURCE producer (start_X(broadcast_fn))            # ~30 bridges/sensors/monitors
  → core.events.make_event(type, **fields)         # the ONE envelope
  → [external] telemetry_auth.make_signed_broadcaster(fn, source)  # HMAC per source
  → tools.executor._aura_broadcast(event)          # the ONE fire-and-forget facade
  → aura.server.broadcast(event)                   # THE ingestion boundary
        ├─ verify_and_unwrap(__src)  (drop tampered/unsigned external)
        ├─ correlator.ingest(event)        (legacy TemporalCorrelator)
        ├─ correlation_v2.feed(event)      (V66 canonical; operational types only)
        └─ manager.broadcast(event)        (WebSocket fan-out to AURA clients)
  correlation_v2.feed → ingest → ops_events.normalize_event(dict) → OperationalEvent
        → windowed rule engine → CorrelationFinding(s) → _emit():
              ├─ _recent ring buffer
              ├─ _link_assets → asset_graph.graph.add_observation/add_relationship
              ├─ broadcast_fn(finding.to_aura_event())
              └─ sinks: incident_finding_sink → incident_workspace.workspace.ingest_finding → IncidentCase
```

Singletons (import, never re-construct): `ops_events.registry`, `asset_graph.graph`,
`correlation_v2.correlator_v2`, `incident_workspace.workspace`, `digital_twin.twin`,
`runbook_engine.engine`, `situation_engine.engine`, `aura.server.manager`,
`tools.executor` (the ONE `ToolExecutor`), `core.presence.presence`,
`core.task_watchdog` instance (main-owned), `performance_profiler._latencies`.

## 1. Canonical event model — `core/ops_events.py`
- `OperationalEvent` (frozen): provenance, source, category, severity, entities
  (`EntityReference`), evidence (`EvidenceReference`), `untrusted_text` (firewall-screened),
  `content_hash` (SHA-256 over identity fields, excludes ingest time → dedup).
- `normalize_event(payload, *, now_iso=None, signed=False) -> EventIngestResult(ok, event, duplicate, adapter, error)`.
- `EventAdapterRegistry` singleton `registry`; `register(adapter, first=False)`; adapters:
  Sysmon/ZeekConn/ZeekDns/ZeekHttp/NetworkBaseline/SensorMesh/Correlator/Internal(catch-all,last).
- **Extension point:** new producers register an `EventAdapter`, never a new event shape.

## 2. Control plane (never bypass) — `tools/executor.py` + `core/*`
- `await ToolExecutor.aexecute(tool, args, reasoning)` = THE guarded dispatch:
  strip-override → resolve `_tool_<name>` → preflight → guardrails → `authorize_action`
  (scope, fail-closed) → `classify_tool`→RiskClass → `requires_trusted_lab` → HITL `_challenge`
  (NATO OTP) → run in executor thread → PII check + `TacticAuditLogger.log_action` + AURA.
- `AuthorityMode`/`ScopePolicy` (server-side only, never reads tool_input);
  `risk_classes.classify_tool` (unknown→HIGH_IMPACT); `injection_firewall.apply_firewall`
  (untrusted origins structurally cannot authorize tools); `source_trust`; `cancel_bus`.
- Runbooks: `runbook_engine.engine.dry_run/execute(name, params, approval_fn, cancel)` compile
  to `TaskGraph` → `aexecute`. `situation_engine` *recommends* runbooks (advisory), never executes.
- Incident world-effect: ONLY `IncidentWorkspace.execute_proposal(...)` → `aexecute`.

## 3. Model runtime — `core/model_router.py`, `core/llm.py`
- ROLE SELECTION: `route(prompt) -> ModelDecision(role, model, complexity, requires_verification)`;
  `resolve_role_model(role, installed=, hw_recommendation=)` 5-level precedence (env→central→hw→installed→fallback).
- INFERENCE SURFACE: `resolve_inference_model(decision)` maps role→concrete streaming model.
- Live turn: `llm.chat_stream` → `assemble_task_decision` → `resolve_inference_model` →
  single `AsyncOpenAI` Ollama client `client.chat.completions.create(model, tools=TOOLS, stream=True)`.
- Vision: `vision_engine.analyze_image/analyze_room/analyze_screen_vision` (VISION model, separate path);
  result fed back as a text `chat_stream` turn for synthesis.
- Embeddings: **sentence-transformers all-MiniLM-L6-v2** (CPU) in `core/memory.py`,`core/knowledge.py`.
  No Ollama embeddings call exists; `nomic-embed-text` is configured but not used for vectors.

### M27 defect (fixed in V67)
`resolve_inference_model` returned the role model verbatim when the role's env var was set,
so `JARVIS_MODEL_EMBEDDING=nomic-embed-text` / `JARVIS_MODEL_VISION=gemma3` could reach the
tool-use chat stream. Also `main.py` vision handlers used `getattr(llm,'model_vision','moondream:latest')`
but `LLM.__init__` never set `model_vision`, so the operator's `gemma3:4b` was ignored (moondream leaked).

## 4. Collectors / lifecycle (M28 seam) — already-present frameworks to EXTEND
- `TaskWatchdog.register(name, coro_factory, RestartPolicy.{ALWAYS,BACKOFF,NEVER})` (primary supervisor, 30s monitor).
- `health_watchdog.track(name, factory)` / `mark_present(name, status_fn)` (secondary + passive markers + audit).
- Producers are bare `async def start_X(broadcast_fn)` — **no common base-class, no health()/heartbeat/checkpoint/metrics, no unified registry.** Dormant = `await asyncio.Event().wait()`.
- **Gap → M28:** thin collector adapter/registry carrying identity/capabilities/health/heartbeat/
  checkpoint/metrics over the existing producers + TaskWatchdog + `_aura_broadcast`, feeding
  `correlation_v2.feed`. Reconcile the two supervisors; do NOT add a third.

## 5. Asset discovery (M29 seam)
- `asset_graph.graph` has exactly ONE production writer (`correlation_v2._link_assets`). `add_observation`,
  `observe_service`, `add_relationship` (all provenance/conflict-aware) are exercised only by tests.
- **Gap → M29:** operator-controlled `EnvironmentRegistry` + discovery (Docker/VMware/local/remote)
  writing observations with proper `ObservationSource`, preserving V66 conflict/provenance. No raw creds in events.

## 6. AURA + voice (M31–M33 seam)
- ONE broadcast facade (`_aura_broadcast`), ONE `BroadcastManager`, ONE `/ws`. Client = `aura/index.html`
  `ws.onmessage` linear `if(msg.type===...)` (~150 handlers) + timeline.
- `core.ops_views` builds 7 bounded/redacted panels + `build_live_system_status` — **coded but UNWIRED**
  (only tests import it); index.html has no handlers for the M20–M26 typed events.
- Voice: `_process_voice_input` = interrupt → macros → mode → LLM → `response_surface.render(VOICE)` → `tts.speak_async`.
- **Gap → M31:** periodic broadcaster loop calling `build_live_system_status` → `_aura_broadcast` + index.html panel/handlers.
- **Gap → M32:** no NL ops-query in `_HUD_ALLOWED_COMMANDS`/`_dispatch_hud_command`.
- **Gap → M33:** voice macros are static YAML; no typed ops-query voice intents.

## 7. Health / observability / tests (M34–M36 seam)
- `self_test.classify_result` → {OK,DORMANT,OPTIONAL,DEGRADED,FAILED}; `healthcheck` → {OK,MISSING_DEP,BROKEN};
  `situation_engine.SituationSeverity` → {calm..critical}; `doctor` → {PASS,WARN,FAIL}. **Four divergent vocabularies.**
- `performance_profiler` (rolling p50/p95/p99 singleton); `health_watchdog` (30s audit).
- **Gap → M34:** no shared status enum, no readiness summary object. Reconcile onto one canonical status + a
  `FieldReadiness` snapshot fed by real checks (collector fabric, model resolver, ops singletons).
- Test conventions: module-level dict/dataclass factories, fixed ISO `now_iso=`, `asyncio.run(...)`,
  assert on typed `.status/.severity/.to_dict()`, fail-closed on empty input.

## 8. Security invariants (must survive V67)
No fact without provenance · unknown stays unknown · external text is untrusted data (firewall before LLM/memory) ·
a finding is a signal not truth (only `execute_proposal`/runbook `execute` effect the world, via `aexecute`) ·
`shell=False` argv-only · authority server-side only · HITL fail-closed · bounded (Rule of Silicon) ·
HUD redaction at the boundary · HMAC-signed external telemetry · single shared Ollama client · one `ToolExecutor`.

## 9. V67 milestone → seam summary
| M | Deliverable | Extends (no fork) |
|---|---|---|
| M27 | Role-safe inference surfaces | `model_router.resolve_inference_model` + new `model_capabilities` |
| M28 | Unified collector fabric | `TaskWatchdog` + `_aura_broadcast` + `correlation_v2.feed` |
| M29 | Environment enrollment + discovery | `asset_graph.graph` (provenance) |
| M30 | E2E scenario harness | real spine (normalize→…→runbook dry_run) |
| M31 | AURA live command center | `ops_views` + `aura_events` + `_aura_broadcast` |
| M32 | NL operational query | structured singletons → bounded fact bundle → LLM synth |
| M33 | Voice operational control | `voice_macros`/`voice_interrupt` → typed intents |
| M34 | Runtime observability + readiness | `self_test`/`health_watchdog`/`performance_profiler` |
| M35 | Chaos/failure validation | deterministic injection over the spine |
| M36 | Field readiness doctor + docs | `doctor`/`model_doctor`/`self_test` |
