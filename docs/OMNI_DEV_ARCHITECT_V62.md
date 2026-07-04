# V62.0 OMNI_DEV_ARCHITECT — Architecture, Migration, and Risk Report

Scope: milestones 0-8 of the OMNI_DEV_ARCHITECT directive, implemented on
branch `omni-dev-architect-phase1` (`a789a73`..present, updated as of the
Phase 8 behavior-model milestone). This document is the Phase 9 deliverable:
old-path-vs-new-path call graphs, a migration plan for what's intentionally
deferred, and residual risk / performance impact on the target host (AMD
Ryzen 5 7430U, 15W TDP, 64GB RAM).

No existing capability was removed. No security control was weakened. Every
change below is backed by tests (see "Test evidence").

---

## 1. Call graphs — old path vs new path

### 1.1 Voice ↔ text unification (Phase 2)

**Before:**

```
Text:  main._loop_text
         -> main._run_turn(llm, tts, user_input, name)
         -> llm.chat_stream(user_input)          [full agentic loop: routing,
                                                    tool-calling, HITL/NATO,
                                                    verification, memory]

Voice: main._loop_voice_continuous
         -> main._ask_jarvis(user_text)           [closure, NOT _run_turn]
         -> llm.client.chat.completions.create(   [raw AsyncOpenAI call]
                model=getattr(llm, "current_model", getattr(llm, "model", "")),
                messages=[...],                    # no tools= kwarg at all
            )
```

Three confirmed dead ends made voice non-functional, not just "reduced":
- `stt.transcribe_bytes(pcm, sample_rate)` was called but did not exist on
  `HighPrioritySTTListener` — every utterance raised `AttributeError`,
  silently swallowed by a broad `except`.
- The Whisper model loader thread was started only as a side effect deep
  inside `listen_vad()` (misplaced code inside `_vad_broadcast`). The
  continuous-voice loop never calls `listen_vad()`, so the model was **never
  loaded** on that path — `self._model` stayed `None` forever.
- `model=getattr(llm, "current_model", getattr(llm, "model", ""))` always
  evaluated to `""` (neither attribute is ever set anywhere in the
  codebase), and the fallback branch called `llm.chat_async(...)`, a method
  that does not exist in the codebase at all.

**After:**

```
Text:  main._loop_text
         -> main._run_turn(llm, tts, user_input, name, lang=None)
         -> llm.chat_stream(user_input)

Voice: main._loop_voice_continuous
         -> core.audio.HighPrioritySTTListener.transcribe_bytes(pcm, rate)  [NEW, real]
         -> main._handle_turn(text, lang_hint)     [background task, preserves
                                                      VAD barge-in behavior]
         -> main._process_voice_input(text, llm, tts, name, lang, consent)
              1. core.voice_interrupt.is_interrupt_command()   [abort/status/reset]
              2. core.consent_commands.parse_consent_command() [NEW — grant/revoke]
              3. core.voice_macros.process_for_macro()
              4. main._run_turn(llm, tts, text, name, lang)
         -> llm.chat_stream(user_input)             [SAME entry point as text]
```

Voice now gets, for free, everything text already had: tool-calling,
HITL/NATO gates, V61 model routing, post-stream verification, secret-safe
memory writes, and session persistence. It also gained the interrupt-command
vocabulary (abort/status/reset), which the old continuous-voice loop never
checked for transcribed text (only raw acoustic barge-in via VAD).

**Test evidence:** `tests/test_voice_parity.py` (characterization test
asserting `_ask_jarvis`/`chat_async` are gone and `_process_voice_input` is
present; end-to-end fake-LLM tests proving text and voice reach the *same*
`chat_stream()` call with the same argument), `tests/test_stt_transcribe_bytes.py`.

### 1.2 Multilingual core (Phase 1, minimal step)

**Before:** `core/tts.py`'s only "language routing" was a one-time voice
pick at construction: `"es" in voice.id.lower() or "spanish" in voice.name.lower()`.
This is a **real, confirmed bug**, not just a missing feature: every Windows
SAPI5 voice id contains the registry path segment `...Speech\Voices\Tokens\...`,
and `"Voices"` itself contains the substring `"es"` — so the check matched
*every* voice and always silently picked the first one enumerated, regardless
of actual language.

**After:**
```
core/language_context.py:  LanguageContext (detected_lang, confidence, updated_at)
                              .update(candidate_lang, candidate_confidence)
                              .voice_hint() -> graceful fallback for unknown langs

core/audio.py:              HighPrioritySTTListener.transcribe_with_confidence()
                              now captures faster-whisper's info.language /
                              info.language_probability into
                              last_detected_language / last_language_confidence

core/tts.py:                 TTSVoiceRouter(voices) — engine-independent, built
                              once from installed voices, matched via
                              voice.languages metadata when available, else
                              locale-code hints (en-us/es-es/...) that do NOT
                              collide with the "Voices" substring bug above.
                              TTS.speak_async(text, lang=...) applies the
                              per-turn voice switch inside the worker thread.
```

Default behavior (fixed `whisper_language='es'`) is unchanged in substance —
`LanguageContext` starts at the configured language and `update()` is a
no-op unless STT actually runs in `auto` mode, so this is purely additive.
What *did* change even in fixed mode: the TTS voice picked for the
configured language is now actually correct, not coincidentally correct.

**Test evidence:** `tests/test_language_context.py` (13 tests, including a
regression test locking in the substring-collision fix).

### 1.3 MCP tool gateway (Phase 7, security-critical slice)

**Before:**
```
core/llm.py tool-dispatch loop:
  if tool_name in self._mcp_tool_names and self._mcp_session:
      mcp_result = await self._mcp_session.call_tool(tool_name, tool_input)  # DIRECT
  else:
      result = await self.tool_executor.aexecute(tool_name, tool_input, thinking)
```
The MCP branch bypassed `ToolExecutor` completely: no allowlist, no
path-traversal check, no HITL/NATO challenge, no audit log. Combined with the
external `packet_tracer_bridge.py`'s `generar_laboratorio_red(nombre_archivo)`
doing an unsanitized `os.path.join(Downloads, nombre_archivo)` +
`open(path, "w")`, this was a **live, unauthenticated arbitrary-file-write
primitive** reachable purely by the model choosing to call the tool — zero
test coverage anywhere in the repo.

**After:**
```
core/llm.py tool-dispatch loop:
  if tool_name in self._mcp_tool_names and self._mcp_session:
      result = await self.tool_executor.aexecute_mcp(tool_name, tool_input, _call_mcp, thinking)
          -> MCP_TOOL_ALLOWLIST membership check (allowlist-not-denylist)
          -> _validate_mcp_filename() on filename-shaped args (traversal/absolute/drive-letter guard)
          -> self._challenge() — unconditional HITL/NATO, no exempt tier for MCP
          -> call_fn(tool_name, tool_input)   [the actual mcp_session.call_tool()]
  else:
      result = await self.tool_executor.aexecute(tool_name, tool_input, thinking)  # unchanged
```
`packet_tracer_bridge.py` itself also hardened (defense in depth, since it
lives outside a single gateway's reach if ever invoked another way): same
basename + resolved-path-containment check before writing.

**Test evidence:** `tests/test_mcp_gateway.py` (16 tests: allowlist
rejection, traversal rejection *before* any call, HITL denial/grant, error
handling, FORCE_OVERRIDE stripping).

### 1.4 Consent enforcement (Phase 6)

**Before:** `core/ironman_mode.py`'s `SessionConsent` (all fields default
`False`) and its predicates were fully implemented and unit-tested in
isolation, but had **zero production callers**. Every real capture path
executed unconditionally: `tools/executor.py`'s screenshot/OCR/clipboard
tools, `core/voice_macros.py`'s 5 vision/screen macro actions, `main.py`'s
webcam/screen voice-keyword triggers, the `screen_monitor.py` background
poller (env-var gated, but env config ≠ operator consent), Telegram's
`/hud` command (chat-ID whitelist = authentication, not consent), and the
auto-screenshot-on-critical-incident hook.

**After:** one `SessionConsent` instance, constructed once in
`main._main_async` (`session_consent = default_consent()`, all `False`),
threaded into every one of those call sites:

```
main._main_async
  session_consent = default_consent()
    -> ToolExecutor(consent=session_consent)
         -> _tool_take_screenshot / _tool_escanear_pantalla /
            _tool_get_clipboard / _tool_set_clipboard   [gate at top of handler]
    -> start_screen_monitor(..., consent=session_consent)
         -> gate re-checked every poll (not just once)
    -> start_telegram_bridge(..., consent=session_consent)
         -> module-level _consent, checked in _cmd_hud
    -> _visual_broadcast() [incident hook] checks session_consent.screen
    -> _loop_voice_continuous(..., consent=session_consent)
         -> webcam/screen keyword branches check consent.camera / .screen
         -> _process_voice_input(..., consent=session_consent)
              -> core.consent_commands.parse_consent_command()  [grant/revoke]
              -> core.voice_macros.process_for_macro(..., consent=session_consent)
    -> _loop_text(..., consent=session_consent)
         -> same consent-command parsing, text surface
```

Since strict enforcement means these tools now refuse until granted,
`core/consent_commands.py` provides the only way to turn a surface on: EN/ES
phrases ("enable screen access" / "activa la pantalla" / "disable camera
access" / ...) recognized identically from voice and text input. The parser
requires word-boundary matches on **both** a grant/revoke verb and a surface
name — a naive substring check misreads `"desactiva"`/`"deshabilita"` as
containing their own grant-side counterparts (`"activa"`/`"habilita"`) and
drops the command as ambiguous. Caught by test, fixed before landing.

**Test evidence:** `tests/test_consent_commands.py` (23),
`tests/test_consent_gating.py` (9), `tests/test_voice_macro_consent.py` (9),
`tests/test_screen_monitor_consent.py` (3),
`tests/test_telegram_hud_consent.py` (4), `tests/test_consent_wiring.py` (1
source-level characterization test locking in that every known site is
wired).

### 1.5 Response surfaces (Phase 5, minimal step)

**Before:** `aura/server.py`'s `BroadcastManager` was pure telemetry pub/sub
— it never carried the assistant's actual answer text, only meta-events
(`model_decision`/`verifier_status`/`memory_decision`). The HUD had no
"conversational content" leg at all.

**After:**
```
core/llm.py chat_stream() tail (after verification + memory persistence):
  await self._maybe_broadcast_response(final_answer, full_text, decision)
    -> core.aura_events.AssistantResponseEvent(text, verified, model_role)
    -> secret-redacted via self._context_mgr.redact_secrets() first
    -> tools.executor._aura_broadcast(event.to_dict())
```
Best-effort, fail-open — a HUD/AURA outage never affects the conversation.

**Test evidence:** `tests/test_ironman_foundation_v61.py::TestAuraEvents`
(dataclass shape), `tests/test_live_brain_v61.py::TestResponseSurfaceBroadcast`
(5 tests: emits correctly, `verified` reflects the actual verifier outcome,
secrets are redacted, broadcast failure never raises, missing decision
defaults role to `"fast"`).

### 1.6 Memory fabric (Phase 3, low-risk slice)

**Before:** `episodic_memory.store_episode()` accepted `source` only to
decide whether to sanitize, then discarded it — stored episodes carried no
provenance metadata at all, and `scope` was never real/filterable metadata
(only ever baked into document *text* by the caller in `core/llm.py`).
`session_manager.save_session()` wrote the raw conversation to disk
**unconditionally every turn**, with no secret redaction — unlike the
episodic-memory write path. `tools/executor.py`'s `_tool_save_note` wrote
raw text to `brain/notes.md`. `_tool_estudiar_tema` indexed arbitrary
scraped web content into a vector store with zero sanitization, despite
`memory_router.is_untrusted_source` explicitly modeling web/url content as
untrusted.

**After:**
```
episodic_memory.store_episode(content, event_type, severity, mitre_tags,
                               source="internal", scope="none", sensitivity="normal")
  -> _write_episode(data)  # source/scope/sensitivity now real Chroma metadata,
                            # additive alongside any existing text prefix

session_manager.save_session(history)
  -> [core.memory_router.redact_secrets(turn["content"]) for turn in history]
     before writing to disk

tools/executor._tool_save_note(title, content, tags)
  -> redact_secrets(title), redact_secrets(content)   before appending to notes.md

tools/executor._tool_estudiar_tema(url)
  -> feed_sanitizer.check_prompt_injection(text, source=url)   [hard reject]
     before chunking/embedding into VectorMemory
```

Full 3-store (episodic / KnowledgeVault / VectorMemory) consolidation is
**intentionally deferred** — see §2.

**Test evidence:** `tests/test_episodic_memory_metadata.py` (7),
`tests/test_session_manager_redaction.py` (4),
`tests/test_notes_and_web_ingest_hardening.py` (4).

### 1.7 Intelligence routing (Phase 4, minimal step)

**Before:** `core/cognitive_optimizer.classify_query()`'s `force_deep`
output was computed and logged every turn (`core/llm.py chat_stream`) but
never fed into `_route_turn()`/`route()` — dead for routing purposes
despite the name.

**After:**
```
LLM._route_turn(user_message, tool_names, force_deep=False)
  decision = route(user_message, security_sensitive=sec, allow_cloud=False)
  if force_deep and decision.role == ModelRole.FAST:
      decision = dataclasses.replace(decision, role=ModelRole.DEEP,
                                      requires_verification=True, reason=...)
  return decision
```
Escalation-only (never de-escalates) and only overrides a `FAST` decision —
a `CODER`/`DEEP`/`VISION`/`VERIFIER`/`CLOUD` decision the router made for a
specific reason is never touched. `model_router.py`'s `ModelRole` enum and
`route()` precedence order are completely unchanged.

**Test evidence:** `tests/test_live_brain_v61.py::TestForceDeepEscalation`
(5 tests), `tests/test_model_router_roles.py` (18, unchanged, still green).

### 1.8 Unified Safe Action Model — HITL risk taxonomy (Phase 7)

**Before:** tool risk was an ad hoc binary split in `tools/executor.py`:
`_HITL_EXEMPT_TOOLS` (26 tools, no challenge) and `_ALWAYS_HITL_TOOLS` (3
tools, always challenged), with every other tool implicitly challenged via
`tool_name not in _HITL_EXEMPT_TOOLS`. 13 of the 42 local tools sat in that
implicit "everything else" bucket with no name, no documented rationale, and
no way to distinguish "launches a known app" from "kills every process
matching a substring" — both were just "not exempt." MCP tools (`aexecute_mcp`,
Phase 7's earlier MCP-gateway fix) used a *separate* unconditional-HITL rule
with no shared classification with local tools at all. `core.aura_events
.ToolAuthPendingEvent` existed, fully specified, with zero production
emitters.

**After:**
```
core/risk_classes.py:
  RiskClass = READ_ONLY | LOW_IMPACT | REVERSIBLE | HIGH_IMPACT | LAB_ONLY
  TOOL_RISK_CLASS: dict[str, RiskClass]   # all 42 local + 2 MCP tools, explicit
  classify_tool(name) -> RiskClass        # unknown tool -> HIGH_IMPACT (fail-closed)
  requires_hitl(risk_class) -> bool
  requires_trusted_lab(risk_class) -> bool
  rollback_hint(risk_class, name) -> str | None
  binary_risk_class(binary) -> RiskClass  # informational, shell sub-binaries only
  verify_consistent_with_legacy_sets(exempt, always_hitl)  # import-time guard

tools/executor.py:
  ToolExecutor.aexecute(tool_name, ...)
    risk_class = classify_tool(tool_name)
    if requires_trusted_lab(risk_class) and not _trusted_lab_enabled():
        return {"error": "... LAB_ONLY ..."}                    # refused outright
    if requires_hitl(risk_class):
        broadcast ToolAuthPendingEvent(tool, risk_class.value, preview, rollback_hint)
        granted, auth_audit = await self._challenge(tool_name, preview)
        ...
  ToolExecutor.aexecute_mcp(tool_name, ...)
    # SAME classify_tool()/requires_hitl()/requires_trusted_lab() calls —
    # one shared risk-classification function now gates both local and MCP
    # dispatch, realizing "all actions pass through one gateway" concretely.
```

`_HITL_EXEMPT_TOOLS`/`_ALWAYS_HITL_TOOLS` in `tools/executor.py` were **not
removed** — 5 other test files assert on them directly by name
(`test_security.py`, `test_redteam_allowlist.py`, `test_code_execute_gate.py`,
`test_trust_floor.py`, `test_punisher_hitl.py`). Instead,
`verify_consistent_with_legacy_sets()` runs at `tools/executor.py` import
time and raises immediately if `risk_classes.py`'s classification would ever
produce a different HITL outcome than those two sets for any tool they
name — a drift here is now a loud import-time failure, not a silent security
regression.

Every one of the 13 previously-"implicit" tools was individually reviewed by
reading its actual implementation (not guessed from its name) before being
classified — this surfaced two real findings along the way:
- `open_application`/`open_software` both have a fallback path that launches
  *any* bare-alphanumeric executable name found on `PATH`, not just their
  pre-approved app map — classified `HIGH_IMPACT`, not the `REVERSIBLE`
  their names suggest.
- `take_screenshot`'s `save_path` parameter is **not** sandboxed the way
  `read_file`/`write_file` are (no `allowed_dirs` check) — a real,
  pre-existing arbitrary-file-write surface. Deliberately kept `HIGH_IMPACT`
  (unchanged from today's behavior) rather than being reclassified down to
  `LOW_IMPACT` alongside this retrofit — fixing that path-sandboxing bug is
  a distinct, separate change and shouldn't be bundled with a permission
  change in the same commit. **Flagged as a residual risk below.**

Net effect on existing gating: **zero tools changed HITL behavior.** Of the
13 previously-implicit tools, 10 landed in `HIGH_IMPACT` and 3 in
`REVERSIBLE` — both require HITL, identical to their pre-retrofit "implicitly
challenged" status. The 26 already-exempt tools split across `READ_ONLY` (23)
and `LOW_IMPACT` (3, matching tools that write only to JARVIS's own local
data: `save_note`/`estudiar_tema`/`ingest_docs`) — both non-HITL, identical
to their pre-retrofit exempt status. This equivalence is asserted by test,
not just claimed (see `test_every_classified_tool_matches_legacy_gating_exactly`).

`ToolAuthPendingEvent` (previously zero production emitters) is now broadcast
on every HITL challenge with the real risk class and, for `REVERSIBLE` tools,
a rollback hint — closing another gap the original recon flagged.

**Test evidence:** `tests/test_risk_classes.py` (95 tests — including the
equivalence proof against every legacy-classified tool, and a completeness
check that every `_tool_*` handler has an explicit classification),
`tests/test_risk_taxonomy_gating.py` (10 tests proving live `aexecute()`/
`aexecute_mcp()` wiring: READ_ONLY/LOW_IMPACT skip the challenge,
REVERSIBLE/HIGH_IMPACT trigger it with the correct broadcast shape, LAB_ONLY
refuses outright without `JARVIS_TRUSTED_LAB` and still requires HITL when
enabled). The 5 previously-flagged dependent files (81 tests) re-run
unchanged and green.

### 1.9 Behavior model — live AssistantMode (Phase 8)

**Before:** `core/ironman_mode.py`'s `AssistantMode` enum and its policy
predicates (`allowed_proactive_actions`, `should_run_background_tasks`,
`should_listen_continuously`) were fully implemented and unit-tested — but
had **zero production callers** anywhere in the codebase. There was no live
"current mode" object for those predicates to be evaluated against, only the
enum and policy tables sitting inert. Proactive Telegram alerts
(`core.telegram_bridge.push_alert`) fired unconditionally for every severity;
the autonomous 4-hourly threat-hunt sweep (`core.hunt_scheduler
.start_hunt_scheduler`) ran unconditionally except for a concurrency guard
against an active LLM/agentic operation — no notion of "quiet mode" or
hardware pressure gated either one, despite `should_run_background_tasks`
existing specifically for that purpose.

**After:**
```
core/assistant_state.py:
  AssistantState(mode: AssistantMode = ACTIVE)   # mutable, session-scoped
  .set_mode(mode) -> bool                        # returns whether it changed

core/mode_commands.py:
  parse_mode_command(text) -> AssistantMode | None   # EN/ES phrases
  describe_mode(mode) -> str                          # operator confirmation

main._main_async:
  assistant_state = default_state()   # ACTIVE by default
    -> start_telegram_bridge(..., state=assistant_state)
         -> push_alert() gated by allowed_proactive_actions(mode, consent)
    -> start_hunt_scheduler(..., state=assistant_state)
         -> each sweep gated by should_run_background_tasks(mode, battery, cpu%, ram%)
    -> _loop_voice_continuous(..., state=assistant_state)
    -> _loop_text(..., state=assistant_state)
         -> both: parse_mode_command() -> state.set_mode() -> ModeEvent broadcast
```

`push_alert`'s gating directly implements the original spec's explicit test
requirement — "proactive notification suppression during FOCUS mode" — using
`allowed_proactive_actions`'s existing, already-tested policy table, not a
new one invented for this. That table models `"notify"` (general capability,
granted in ACTIVE/WAR_ROOM) as a superset of `"notify_urgent"` (the narrower
capability FOCUS/PRESENTATION grant instead) — a real design detail this
milestone's own tests caught: naively checking for `"notify_urgent"` alone
on `CRITICAL` alerts would have silently suppressed critical alerts in
`ACTIVE` mode, since that mode's action list contains `"notify"` but not the
literal string `"notify_urgent"`. Fixed in `_may_notify()` before landing.

`should_run_background_tasks` reads real CPU/RAM/battery state via
`psutil` (fails open — 0%/plugged-in — if `psutil` is unavailable, never
blocking the hunt on a monitoring failure) and skips that interval's sweep
(rescheduled for the next one, not cancelled outright) under pressure or in
a quiet mode.

`ModeEvent` (previously zero production emitters, like `ToolAuthPendingEvent`
before §1.8) now broadcasts on every actual mode change.

Response-surface brevity (voice short, text/technical detailed — the other
half of the original Phase 8 spec) is **not** part of this milestone: no
mode-driven length/tone adaptation was added to `_run_turn`'s streaming
output. This remains prompt-driven exactly as before; see the migration
plan below.

**Test evidence:** `tests/test_assistant_state.py` (4), `tests/test_mode_commands.py`
(19), `tests/test_push_alert_mode_gating.py` (6 — including the `"notify"`
superset regression), `tests/test_hunt_scheduler_mode_gating.py` (5),
`tests/test_voice_parity.py` (4 new mode-command tests), `tests/test_mode_wiring.py`
(1 source-level characterization test).

---

## 2. Migration plan — what's deferred and why

| Item | Status | Why deferred | Risk if done carelessly |
|---|---|---|---|
| Full `ModelRole`/`route()` taxonomy expansion (GENERAL/RESEARCH/ARCHITECT/MATHEMATICS/LANGUAGE/CYBER_BLUE/CYBER_PURPLE/DFIR/GRC/PLANNER) | Not started | `ModelRole` and `route()`'s precedence order are directly asserted by `tests/test_model_router_roles.py` and `tests/test_live_brain_v61.py` — a careless enum change breaks both | MEDIUM-HIGH |
| `CognitiveEngine` / `AgentOrchestrator` merged into live per-turn routing (currently two fully parallel dispatch paths — canary/honeypot flow and AURA's `multi_agent_analyze` command respectively) | Not started | Neither has ever run inside the verification/memory-write timing assumptions around `is_plain_assistant` in `chat_stream` | HIGH |
| ~~Phase 7 full risk taxonomy~~ | **Done — §1.8** | `core/risk_classes.py`, wired into `aexecute()`/`aexecute_mcp()`, verified against the 5 dependent test files with zero behavior change | — |
| `take_screenshot`'s unsandboxed `save_path` | Not started (flagged, not fixed) | Found during the Phase 7 tool review; deliberately not bundled with the taxonomy retrofit to avoid mixing a permission decision with a bug fix in one commit | Real, small, well-scoped — add an `allowed_dirs` check matching `read_file`/`write_file`'s existing pattern |
| 3-store memory consolidation (episodic / KnowledgeVault / VectorMemory → one) | Not started | No existing test imports `core/memory.py` or `core/episodic_memory.py`'s storage layer directly — a regression here is invisible to CI until this gap is closed first | MEDIUM |
| ~~Phase 8 behavior model — proactive-action/background-task gating~~ | **Done — §1.9** | `core/assistant_state.py` + `core/mode_commands.py`, wired into `push_alert`/`start_hunt_scheduler`/both loops | — |
| Phase 8 remainder — response-surface brevity/tone by mode (voice short, text/technical detailed, "aware of active projects/previous decisions" beyond memory retrieval) | Not started | No mode-driven adaptation exists in `_run_turn`'s streaming output; still 100% prompt-driven | LOW — purely additive, no invariant to break |
| `AgentRuntime`/`InputEvent`/`ContextAssembler`/`TaskPlanner`/`CapabilityPolicy`/`ResultIntegrator`/`ResponseSurfaceRouter` as named, unified abstractions | Not started as named classes | The *behavior* these were meant to unify (voice=text pipeline, MCP=local tool gateway sharing one risk taxonomy, HUD gets conversational content) is now real (§1.1, §1.3, §1.5, §1.8) without introducing new abstraction layers on top of a codebase that already had `chat_stream()` as a working single entry point. Introducing formal wrapper classes now would be renaming working code, not fixing a gap | LOW, but real refactor cost |

**Recommended next sequence** (updated — Phase 7 is done): the approach used
for Phase 7 — add the new classification as a parallel, verified-consistent
layer rather than replacing the legacy sets outright — is the template for
the still-deferred items above. Attempting a single big-bang replacement
(e.g. deleting `_HITL_EXEMPT_TOOLS`/`_ALWAYS_HITL_TOOLS` now that
`risk_classes.py` exists, or expanding `ModelRole` in place) remains the
highest-risk path available in this codebase.

---

## 3. Residual risks

1. **Whisper model now actually loads in continuous-voice mode.** Before
   this branch, the loader thread never started on that path (a bug — see
   §1.1), so voice mode was silently "free" in RAM/CPU terms because it was
   silently broken. Now that voice actually works, running `--voice` loads
   the configured Whisper model (`small` by default) into RAM at boot and
   runs it in a dedicated high-priority thread, as the module's own
   docstring always promised. This is correct behavior, not a regression,
   but it is a genuine new resource cost on the 15W-TDP target host that
   didn't materialize before because the feature didn't work.
2. **Consent now blocks previously-unconditional tools until granted.**
   Screenshot/OCR/clipboard/webcam tools and voice macros now refuse by
   default every session. First-time users (including the operator, on the
   very next `--voice` run) will hit a refusal message rather than the tool
   silently working — this is the deliberate, explicit trade-off confirmed
   during this work, not a bug, but it is a real behavior change worth
   flagging to anyone who relied on the old unconditional behavior in
   scripts or muscle memory.
3. **MCP allowlist is allowlist-not-denylist by design.** Adding a new MCP
   tool to `packet_tracer_bridge.py` (or any future MCP server) requires
   also adding it to `tools/executor.MCP_TOOL_ALLOWLIST`, or it will be
   refused outright. This is intentional (fail-closed) but is an operational
   step future contributors need to remember.
4. **Full 3-store memory fragmentation is unresolved.** `episodic_memory.py`,
   `knowledge.py` (KnowledgeVault), and `memory.py` (VectorMemory) remain
   three independent ChromaDB instances with separate embedding caches,
   contradicting the project's own "consolidate before adding" direction.
   Retrieval also remains monolithic (always queries `jarvis_episodic`
   regardless of task category) — `cognitive_optimizer.classify_query()`'s
   5-category output is still not wired to memory-store selection.
5. **The pre-existing flaky test** (`test_security.py::TestReadFile::
   test_relative_traversal_blocked`) predates this branch (confirmed via
   `git blame` to commit `3e48f85`) and is unrelated to any change here — a
   platform quirk where `../../etc/passwd` resolves to a nonexistent-but-
   still-inside-Downloads path on this host, not a security bypass.
6. **Response-surface tone/brevity is still 100% persona-prompt-driven.**
   Phase 8's proactive-action/background-work gating is done (§1.9), but
   the "brief in voice, detailed in technical surfaces" half of the original
   spec has no live implementation — `_run_turn`'s streaming output isn't
   adapted by mode or surface today. See the migration plan (§2).
7. **The default `AssistantMode` is `ACTIVE`, not gated by any onboarding
   step.** A fresh session starts with `push_alert`/`start_hunt_scheduler`
   fully active (matching pre-Phase-8 unconditional behavior) — this was a
   deliberate default to avoid silently going quieter than before for
   existing users, not an oversight. Switching to `FOCUS`/`PASSIVE` is
   entirely manual (voice/text command) until Phase 9 adds any
   automatic mode inference.
8. **`take_screenshot`'s `save_path` is not sandboxed.** Found during the
   Phase 7 tool-by-tool review (§1.8): unlike `read_file`/`write_file`, it
   never checks the resolved path against an `allowed_dirs` list, so a
   caller-supplied `save_path` could write a PNG anywhere the OS user has
   write access to. Deliberately left as HIGH_IMPACT (unchanged HITL
   behavior) rather than fixed in the same commit as the risk-taxonomy
   retrofit, to avoid bundling a bug fix with a classification change. See
   the migration plan (§2) — small, well-scoped, not yet done.
9. **`open_application`/`open_software`'s arbitrary-executable fallback.**
   Also found during the Phase 7 review: both tools fall back to launching
   *any* bare-alphanumeric executable name found on `PATH` when the name
   isn't in their pre-approved app map. Already classified `HIGH_IMPACT`
   (always HITL-gated) — not a new gap, just newly documented. No behavior
   change needed; flagged here so the fallback's existence is explicit
   rather than discovered again later.

## 4. Performance impact (AMD Ryzen 5 7430U, 15W TDP, 64GB DDR4)

All new code on the hot per-turn path is pure Python control flow — dict/set
membership checks, regex matches, one `dataclasses.replace()` call — with no
measurable CPU cost (sub-millisecond, well under noise floor next to a
network round-trip to Ollama). No new heavy imports were added anywhere in
the boot sequence; `feed_sanitizer`, `memory_router`, and `ironman_mode` were
already imported elsewhere in the codebase and are lazy/pure modules. The
one real cost is item 1 above (Whisper model load) — a fix to a bug, not new
scope, and gated behind `--voice` exactly as before.

## 5. Test evidence summary

| Milestone | Commit | New tests |
|---|---|---|
| M0 — MCP gateway | `a789a73` | 16 |
| M1 — Voice unification + multilingual core | `0c67159` | 23 |
| M2 — HUD response broadcast | `f0f13ce` | 6 |
| M3 — Consent enforcement (core sites) | `98b5762` | 44 |
| M4 — Consent enforcement (remaining sites) | `5511e9b` | 8 |
| M5 — Memory fabric low-risk slice | `671f34d` | 15 |
| M6 — force_deep routing escalation | `55de9b5` | 5 |
| M7 — Phase 7 HITL risk taxonomy retrofit | `d00e98b` | 105 (95 + 10) |
| M8 — Phase 8 behavior model | (pending commit) | 39 |

Full suite: 773/774 passing, 15 skipped (1 pre-existing, unrelated failure —
see residual risk 5; skips are the equivalence test's intentional no-op for
the 2 MCP-only tool names that have no legacy-set membership to compare
against). `ruff check .`: clean. `py_compile` across all 225 `.py` files in
the repo: clean.
