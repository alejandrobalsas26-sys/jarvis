# Changelog

## V62.0 — Voice/text runtime unification, consent enforcement, MCP gateway hardening

Full details: `docs/OMNI_DEV_ARCHITECT_V62.md` (old-vs-new call graphs,
migration plan, residual risks, performance impact).

- **Security fix**: closed a live, unauthenticated arbitrary-file-write
  vulnerability — MCP tool calls bypassed `ToolExecutor`'s allowlist/HITL
  gate entirely (`tools.executor.aexecute_mcp`, `MCP_TOOL_ALLOWLIST`).
- **Voice mode was structurally broken** (STT never loaded on the
  continuous-voice path, model resolution always resolved to `""`,
  tool-calling unreachable) — now runs through the same `chat_stream()`
  pipeline as text, with the interrupt-command vocabulary it never had.
- Added a multilingual foundation: `core/language_context.py`
  (`LanguageContext`) and `core/tts.py`'s `TTSVoiceRouter`, fixing a latent
  bug where the old Spanish-voice heuristic matched every Windows SAPI
  voice and always picked the first one enumerated.
- AURA HUD now receives the assistant's actual response text
  (`AssistantResponseEvent`), not just routing/verifier/memory metadata.
- `core.ironman_mode.SessionConsent` (previously defined, tested, but never
  consulted anywhere) is now enforced at every real screen/camera/clipboard
  capture site — tool handlers, voice macros, voice keyword triggers, the
  screen monitor, Telegram `/hud`, and the incident auto-screenshot hook.
  Added `core/consent_commands.py`, an EN/ES grant/revoke command surface.
- Episodic memory now carries real provenance/scope metadata; closed
  secret-redaction gaps in session snapshots and saved notes; web content
  now passes a prompt-injection gate before vector-store ingestion.
- `cognitive_optimizer.classify_query()`'s `force_deep` signal (computed
  every turn, never used) now escalates FAST routing decisions to
  DEEP + verification, without touching `ModelRole`/`route()` precedence.
- **Unified Safe Action Model** (`core/risk_classes.py`): a five-tier HITL
  risk taxonomy (READ_ONLY/LOW_IMPACT/REVERSIBLE/HIGH_IMPACT/LAB_ONLY) now
  drives `ToolExecutor.aexecute()`/`aexecute_mcp()` for both local and MCP
  tools — one shared classification for the whole tool gateway, replacing
  the ad hoc `_HITL_EXEMPT_TOOLS`/`_ALWAYS_HITL_TOOLS` binary split as the
  live decision (the legacy sets stay in place and are verified consistent
  at import time, since 5 test files assert on them directly). Zero tools
  changed HITL behavior — every classification was chosen and tested to
  match the pre-retrofit gating exactly. `ToolAuthPendingEvent` (previously
  unemitted) now broadcasts on every challenge with the real risk class and
  a rollback hint for REVERSIBLE tools. Surfaced two pre-existing findings
  along the way (`open_application`/`open_software`'s arbitrary-executable
  fallback, `take_screenshot`'s unsandboxed `save_path`) — documented in
  the architecture doc, not silently fixed alongside the taxonomy change.
- **Behavior model** (`core/assistant_state.py`, `core/mode_commands.py`):
  `core.ironman_mode.AssistantMode` and its policy predicates
  (`allowed_proactive_actions`, `should_run_background_tasks`) had zero
  production callers — no live "current mode" existed anywhere. Now a
  session-scoped `AssistantState` (default `ACTIVE`) drives
  `telegram_bridge.push_alert` (real "notification suppression during FOCUS
  mode", the original spec's explicit test requirement) and
  `hunt_scheduler.start_hunt_scheduler` (skips the 4-hourly autonomous sweep
  under CPU/RAM/battery pressure or in a quiet mode), with an EN/ES
  mode-switch command surface from voice and text. `ModeEvent` (previously
  unemitted) now broadcasts on every mode change.

## V61.0 — Live AI brain + Iron Man Mode foundation

Wires the V60 brain modules (role router, verifier, memory discipline) into the
**live streaming response path**, and lays a consent-gated foundation for an
always-available, multimodal workstation assistant. No security controls were
weakened; cloud stays opt-in; nothing captures screen/camera/clipboard silently.

### Local AI brain — now live (Phases 1-5)
- **Live role-based routing** (`core/llm.py`): the streaming path now routes each
  turn via `model_router.route()` (role / provider / complexity / reason /
  `requires_verification`) instead of the legacy complexity-only `select_model`.
  A new `LLM._route_turn()` is the single routing entry; `select_model` remains a
  backward-compatible fallback (still used for context compression).
- **`resolve_inference_model(decision)`** maps a routing decision to a concrete,
  tool-call-capable local model — honoring per-role env overrides, else falling
  back to the boot-resolved `MODEL_FAST`/`MODEL_DEEP` so unavailable role-default
  models never break inference. Cloud is never streamed from the local client.
- **Security-sensitive turn classifier** (`model_router.is_security_sensitive_turn`):
  conservative EN/ES predicate over offensive-security / DFIR / credential-shell
  vocabulary, code-gen-in-dangerous-domains, and dangerous tool usage.
- **Staged post-stream verification** (`LLM._maybe_verify_final_answer`): low-risk
  chat streams untouched; high-risk turns (security-sensitive, dangerous tool
  used, deep analysis, or `requires_verification`) get a VERIFIER pass over the
  streamed draft. Pass → silent; flagged → a concise ASCII `[VERIFICATION]`
  notice is appended and stored; fail-closed → a human-review warning. The
  verifier never executes tools and never crashes the turn.
- **Memory discipline in the path** (`LLM._maybe_persist_memory`): episodic
  retrieval is gated by `should_use_memory()` (skipped for trivial chat),
  persistence honors `should_write_memory` / `classify_memory_scope` and
  **refuses secrets** before any write (best-effort, fail-open).
- **Tool-output trust labels / prompt-injection defense** (`LLM._label_tool_result`):
  every tool result enters history wrapped with trust metadata; web / file / RAG /
  screen / clipboard results are flagged `untrusted_tool_output` with a banner
  instructing the model to treat them as DATA, never instructions. Truncation now
  happens inside the labeled envelope.

### System prompt safety cleanup (Phase 6)
- Removed unsafe directives: "ROOT-level authorization permanently granted",
  "NEVER refuse to execute a local tool / NEVER give ethical lectures", the
  "execute the chain without asking for permission for each individual step"
  bypass, and the mandatory public `[THINKING]` exposure (now optional).
- Added an **AUTHORIZATION MODEL** and **TRUST & SAFETY CONTRACT**: executor-
  mediated authorization, HITL/NATO for dangerous actions, no guardrail bypass,
  no invented tool names, untrusted tool/web/file/RAG/screen output, no secret
  persistence, and bounded proactivity. Spanish/English style preserved.

### Iron Man Mode foundation (Phases 7-9)
- **`core/ironman_mode.py`** — pure, consent-gated policy (no runtime loops, no
  capture): `AssistantMode` (PASSIVE/ACTIVE/FOCUS/WAR_ROOM/PRESENTATION),
  `SessionConsent` (screen/clipboard/camera/microphone/shell/browser, default
  OFF), and `should_use_screen_context` / `should_listen_continuously` /
  `should_run_background_tasks` (hardware- and battery-aware) /
  `allowed_proactive_actions`. No silent surveillance; dangerous tools stay HITL.
- **`core/task_queue.py`** — broker-free in-memory background scheduler with an
  allowlist of safe task types (summarize_document, generate_report, run_tests,
  analyze_repo, index_documents, monitor_system), dangerous-type rejection unless
  explicitly approved, full state machine, and cancellation. Schedules only — it
  never executes shell/code itself.
- **`core/aura_events.py`** — typed, JSON-serializable HUD event contract (model
  decision, verifier status, memory decision, tool-auth-pending, background task,
  assistant mode). The live path broadcasts these `type`s to AURA.

### Tests
- New: `jarvis/tests/test_live_brain_v61.py` (routing, security classifier,
  verifier integration with fake clients, memory policy, trust labels / prompt
  injection, system-prompt safety) and `jarvis/tests/test_ironman_foundation_v61.py`
  (mode policy, task queue, AURA events). No Ollama/GPU/internet/mic required.

### Limitations (intentional, this PR)
- Verification is **post-stream** (the draft streams first, then is audited) and
  is **advisory** — the verifier flags issues, it does not rewrite the answer.
- Cloud escalation is supported by `route()` but **not streamed** from the local
  client; the live path passes `allow_cloud=False`. Cloud remains opt-in.
- Iron Man Mode is a **policy foundation**; always-on behavior stays consent-
  gated and there is **no silent screen/camera/clipboard capture**.

## V60.0 — Hardening, role routing, and installability

### Security (Phase 7)
- **Neutralized the `FORCE_OVERRIDE` guardrail bypass.** LLM-generated tool
  arguments can no longer disable destructive-pattern guardrails. The key is
  stripped and logged at both execution gates; the only legitimate override is
  operator-set trusted-lab mode (`JARVIS_TRUSTED_LAB`), read from `.env`/env.
- **SSRF defense for `http_request`.** Loopback, RFC1918 private, link-local
  (incl. `169.254.169.254` cloud metadata), multicast, and reserved targets are
  blocked — including hostnames that resolve to them — unless trusted-lab mode
  is enabled.
- Added `core/config.py: trusted_lab_mode` (env-only, hardened off by default).

### Architecture
- **Role-based model router** (`core/model_router.py`): `ModelRole`,
  `ModelDecision`, `route()` with bilingual (EN/ES) keyword scoring and
  env-overridable, hardware-friendly defaults. Legacy `select_model` /
  `calculate_complexity` remain backward compatible.
- **Planner/verifier** (`core/verification.py`): `should_verify` and a
  fail-closed `verify_answer` that audits drafts with a dedicated VERIFIER model.
- **Memory discipline** (`core/memory_router.py`): secret refusal, scope
  classification, and untrusted-source tagging.
- **Hardware-tier model profiles** (`core/hardware_model_profile.py`): LOW/MID/
  HIGH/EXTREME tiers → recommended models and `ollama pull` commands.

### Bug fixes
- Fixed a **Python 3.11 SyntaxError** in `core/session_journal.py` (backslash
  escape inside an f-string expression — a 3.12+ feature).
- Fixed a latent **`NameError`** in `main.py`'s voice path: `_process_voice_input`
  used `is_interrupt_command` / `handle_interrupt` / `process_for_macro` without
  importing them in scope.

### Lint cleanup (Phase 12 follow-through)
- Ruff gate expanded to **`E9` + full pyflakes (`F`)** and the tree made clean:
  81 unused imports / empty f-strings auto-fixed, 3 unused variables removed, one
  genuinely-unused name dropped from a multi-import.
- **Avoided two autofix regressions** (verified, not blindly applied):
  - `core/self_test.py` uses `try: import <dep>` as availability probes — the
    bound name is intentionally unused. Reverted the removals and added a
    documented per-file `F401` ignore (proven false positive).
  - `main.py` early `.env` validation (`from core.config import settings`) is a
    side-effect import; restored with a narrow commented `# noqa: F401`.
- `core/sensor_agent_template.py` keeps its documented `F821` per-file ignore
  (`__JARVIS_PORT__` is string-substituted at runtime).

### Documentation drift fixed
- Root `README.md` (new) and `jarvis/README.md` (rewritten): corrected the brain
  from "Claude Sonnet" to **Ollama (local default)**; `ANTHROPIC_API_KEY` is
  documented as optional/cloud-only. Removed the stale "migrate to Ollama"
  roadmap item (already done).
- Added `docs/TROUBLESHOOTING.md`.

### Installability & tooling (Phases 1, 2, 9, 10, 11)
- `requirements/` profiles: `base`, `voice`, `docs`, `soc`, `lab`, `dev`, `all`.
  Base is lean enough for text mode without audio/OCR/ML/lab deps.
- `scripts/install.ps1`, `scripts/install.sh`, `scripts/doctor.py`,
  `scripts/model_doctor.py`.
- `pyproject.toml`: metadata, `requires-python >= 3.11`, optional-dependency
  groups, ruff config, pytest config + markers, `jarvis` console script.
- `python -m jarvis` entrypoint (`python main.py` still works).
- CI split into `lint` (ruff gate, **no `|| true`**), `tests-base`, `security`
  (bandit + pip-audit), and `docker-build`.
- Dockerfile builds the lean base image (the old monolithic image pinned the
  Windows-only `torch-directml` wheel and could not build on Linux).
- Docs: `SECURITY.md`, `docs/THREAT_MODEL.md`, this changelog.

### Tests
- New: `tests/test_security_hardening.py` (FORCE_OVERRIDE + SSRF),
  `jarvis/tests/test_model_router_roles.py`,
  `jarvis/tests/test_memory_and_verification.py`,
  `jarvis/tests/test_hardware_model_profile.py`.

### Roadmap / follow-ups
- Ruff is gated on correctness rules (`E9, F63, F7, F82`); a repo-wide
  unused-import (`F401`) and style cleanup is the next lint PR.
- Verifier integration is wired as a standalone module; hooking it into the
  streaming response path (non-streaming/security-sensitive paths first) is the
  next orchestration PR.
