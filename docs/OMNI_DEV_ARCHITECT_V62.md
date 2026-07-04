# V62.0 OMNI_DEV_ARCHITECT — Architecture, Migration, and Risk Report

Scope: milestones 0-6 of the OMNI_DEV_ARCHITECT directive, implemented on
branch `omni-dev-architect-phase1` (7 commits, `a789a73`..`55de9b5`). This
document is the Phase 9 deliverable: old-path-vs-new-path call graphs, a
migration plan for what's intentionally deferred, and residual risk /
performance impact on the target host (AMD Ryzen 5 7430U, 15W TDP, 64GB RAM).

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

---

## 2. Migration plan — what's deferred and why

| Item | Status | Why deferred | Risk if done carelessly |
|---|---|---|---|
| Full `ModelRole`/`route()` taxonomy expansion (GENERAL/RESEARCH/ARCHITECT/MATHEMATICS/LANGUAGE/CYBER_BLUE/CYBER_PURPLE/DFIR/GRC/PLANNER) | Not started | `ModelRole` and `route()`'s precedence order are directly asserted by `tests/test_model_router_roles.py` and `tests/test_live_brain_v61.py` — a careless enum change breaks both | MEDIUM-HIGH |
| `CognitiveEngine` / `AgentOrchestrator` merged into live per-turn routing (currently two fully parallel dispatch paths — canary/honeypot flow and AURA's `multi_agent_analyze` command respectively) | Not started | Neither has ever run inside the verification/memory-write timing assumptions around `is_plain_assistant` in `chat_stream` | HIGH |
| Phase 7 full risk taxonomy (`READ_ONLY`/`LOW_IMPACT`/`REVERSIBLE`/`HIGH_IMPACT`/`LAB_ONLY` replacing the binary `_HITL_EXEMPT_TOOLS`/`_ALWAYS_HITL_TOOLS` split) | Not started | Those two frozensets and their disjointness invariant are directly exercised by 5 test files (`test_security.py`, `test_redteam_allowlist.py`, `test_code_execute_gate.py`, `test_trust_floor.py`, `test_punisher_hitl.py`) | **HIGH** |
| 3-store memory consolidation (episodic / KnowledgeVault / VectorMemory → one) | Not started | No existing test imports `core/memory.py` or `core/episodic_memory.py`'s storage layer directly — a regression here is invisible to CI until this gap is closed first | MEDIUM |
| Phase 8 behavior model (state-driven, not persona-prompt-driven) | Not started | Structurally depends on Phase 6 (done — consent/mode state now exists) but there's still no `AssistantMode`-reading proactive-action dispatcher to gate; the natural first step is wiring `ironman_mode.allowed_proactive_actions(mode)` into the one still-unconditional proactive hook design allows for (none currently exist post-M4 — all known capture hooks are now consent-gated) | LOW (nothing left to gate yet) |
| `AgentRuntime`/`InputEvent`/`ContextAssembler`/`TaskPlanner`/`CapabilityPolicy`/`ResultIntegrator`/`ResponseSurfaceRouter` as named, unified abstractions | Not started as named classes | The *behavior* these were meant to unify (voice=text pipeline, MCP=local tool gateway, HUD gets conversational content) is now real (§1.1, §1.3, §1.5) without introducing new abstraction layers on top of a codebase that already had `chat_stream()` as a working single entry point. Introducing formal wrapper classes now would be renaming working code, not fixing a gap | LOW, but real refactor cost |

**Recommended next sequence** (unchanged from the original recon, still
accurate): Phase 7's MCP-style allowlist thinking could extend to a formal
risk-class enum *without* touching the existing frozensets — add the enum as
a parallel classification, migrate call sites gradually, then retire the
binary split once every tool has a class assigned. Attempting a single
big-bang replacement is the highest-risk path available in this codebase.

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
6. **Behavior is still 100% persona-prompt-driven** (Phase 8 not started).
   `ironman_mode.allowed_proactive_actions()` is correct and tested but has
   no live proactive-action dispatcher consuming it yet — there's currently
   nothing state-driven left un-gated (Phase 6 closed every known capture
   hook), but there's also no *emergent* behavior yet, just a policy table
   waiting for a consumer.

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

Full suite: 644/645 passing (1 pre-existing, unrelated failure — see
residual risk 5). `ruff check .`: clean. `py_compile` across all 215 `.py`
files in the repo: clean.
