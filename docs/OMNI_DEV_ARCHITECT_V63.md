# V63.0 OMNI_DEV_ARCHITECT — General-Purpose Agent Runtime

Successor to `OMNI_DEV_ARCHITECT_V62.md` (authoritative for completed V62 work
and its deferred items). V63 evolves JARVIS from a strong cyber-focused local
assistant into a general-purpose, multimodal, memory-aware, agentic runtime
**while preserving all existing capabilities and security controls**.

Target host: AMD Ryzen 5 7430U (15W TDP, CPU-bound), 64GB DDR4. All hot-path
additions are pure Python control flow (dict/set/regex), sub-millisecond next
to an Ollama round-trip.

## Guiding constraints (non-negotiable)

1. Security controls may not be weakened.
2. Model output never bypasses `ToolExecutor`; MCP stays behind the gateway;
   unknown tools fail-closed.
3. Consent enforced at real capture sites; trusted-lab operator-controlled.
4. No unbounded agent loops / concurrency; no full-memory dumps into prompts.
5. No big-bang storage migration; incremental, reversible only.
6. No decorative modules without production callers. Behavior over naming.
7. Tests must prove real call paths.

The V62 architect deliberately did **not** introduce `AgentRuntime`/`InputEvent`/
etc. as named classes, on the grounds that the *behavior* was already unified
through `chat_stream()` and wrapping it would be renaming working code. V63
honors that: new abstractions are introduced **only** where they remove real
duplication or add a real capability, and each is wired into a production call
path with tests, not left decorative.

---

## Target architecture (the spine being made real)

```
Operator / Event / Sensor
  -> Unified live turn (llm.chat_stream)
       -> Context assembly
       -> Intent + complexity + domain routing   (M2: TaskDomain, additive)
       -> Task graph planning (only when justified) (M3)
       -> Agent team selection                     (M4)
       -> Shared blackboard / evidence store        (M4)
       -> Specialist execution                      (M4)
       -> Critic / conflict resolution              (M4)
       -> Verification (existing verifier)          (kept)
       -> Result integration
       -> Response surface router (reason once, render per surface) (M6)
       -> Memory persistence (scoped fabric facade) (M5)
       -> Proactive follow-up policy (presence)     (M7)
```

---

## Milestone 0 — baseline & safety closure

### Baseline (branch point `jarvis-v63-general-agent-runtime` off master `4652a19`)

Measured before any V63 change:

| Gate | Result |
|---|---|
| `pytest` (jarvis/tests + ../tests) | **773 passed, 1 failed, 15 skipped** |
| `ruff check .` | clean |
| `python -m compileall core tools main.py` | clean |

The single failure is **pre-existing and unrelated**:
`tests/test_security.py::TestReadFile::test_relative_traversal_blocked` — a host
path quirk where `../../etc/passwd` resolves to a nonexistent-but-inside-Downloads
path and returns "archivo no encontrado" instead of the security message
(documented as V62 residual risk #5, traced to commit `3e48f85`, predates this
work). Preserved as known-pre-existing; **not** masked or "fixed" by weakening
the assertion.

### A. `take_screenshot` save_path sandboxing (closes V62 residual risk #8)

`tools/executor.py`:
- New module-level `_resolve_within_allowed(path) -> Path | None` and
  `_sandbox_allowed_dirs()` centralize the containment check that
  `_tool_read_file`/`_tool_write_file` performed inline (Downloads / Documents /
  project cwd). Fail-closed: resolves the path first, then rejects relative
  traversal, absolute escapes, drive-letter escapes, and symlinks whose target
  resolves outside an allowed root (`.resolve()` canonicalizes symlinks + `..`
  before the test). Malformed paths / OS errors → rejected.
- `_tool_take_screenshot` now validates `save_path` through that helper **before**
  importing/invoking `pyautogui`, so a rejected path never captures the screen.
  The consent gate still runs first (a denied path with no consent returns the
  consent error, not a path error). The empty-path default moved from the bare
  home dir (outside containment) to a timestamped PNG under Downloads.
- HITL classification unchanged: `take_screenshot` stays `HIGH_IMPACT`.

Tests: `tests/test_screenshot_sandbox.py` (valid Downloads path, default-lands-in-
Downloads, traversal/absolute/`~`-escape rejection with no capture, consent
precedes path check, helper unit tests incl. symlink escape). Updated
`tests/test_consent_gating.py::test_screenshot_allowed_with_screen_consent` to a
hermetic allowed path (it previously asserted a save to pytest `tmp_path`, which
is correctly outside the sandbox now).

### B. Version metadata alignment

`jarvis/pyproject.toml` `version` `61.0.0` -> `63.0.0`. The package reported V61
while V62 was merged and documented. Historical `V6x` references in comments and
docstrings are intentionally left as-is.

---

## Milestone 2 — general semantic domain routing (additive)

`core/task_domain.py`: `TaskDomain` (14 semantic domains) + pure deterministic
`classify_domain(prompt, tool_names) -> DomainSignal`. Bilingual EN/ES keyword
scoring, fixed tie-break order, tool-name hints. Domain is a dimension
independent of `ModelRole` (model choice), complexity, and risk — advisory only.
`model_router.route()` precedence and the `ModelRole` enum are untouched, so
every `test_model_router_roles.py` / `test_live_brain_v61.py` assertion holds.
Tests: `tests/test_task_domain.py`.

## Milestone 6 — response surface router (reason once, render per surface)

`core/response_surface.py`: `ResponseSurface` (VOICE/TEXT/HUD/TECHNICAL/REPORT/
NOTIFICATION) + pure `render(text, surface)`. Lossless surfaces (TEXT/TECHNICAL/
REPORT) are verbatim; VOICE strips Markdown while preserving prose words;
HUD/NOTIFICATION are bounded summaries. Invariant (presentation changes,
reasoning truth does not) is test-enforced. Wired into `main._run_turn`'s TTS
consumer so the spoken channel renders VOICE per sentence (no markdown read
aloud) while the console keeps TEXT — one reasoning result, rendered per surface,
never re-reasoned. Closes the "brief in voice" half of V62 residual risk #6.
Tests: `tests/test_response_surface.py`.

## Milestone 1 — unified live agent runtime (composed per-turn decision)

`core/agent_runtime.py` introduces `TaskDecision`, the composed per-turn decision
object the V63 spine calls for once per turn:

```
chat_stream (llm.py:1541)
  -> assemble_task_decision(user_message, force_deep, query_category, surface)
       route_turn()            -> ModelDecision   (authoritative role + verify)
       classify_domain()       -> TaskDomain       (M2, semantic)
       ResponseSurface         -> presentation     (M6)
       + requires_planning / prefers_agent_team / requires_tools advisories
  -> decision = td.model_decision   # existing reads unchanged (byte-identical)
  -> AURA model_decision event += td.telemetry()  # additive domain/surface
```

Single-source invariant: the FAST→DEEP `force_deep` escalation lives only in
`route_turn`; `LLM._route_turn` delegates to it (no drift, its tests unchanged).
`chat_stream` reads `td.model_decision` so model selection and verifier gating
are identical to V62; the composed object adds the semantic/planning/surface
dimensions for telemetry and future planner/agent-team routing (M3/M4). No new
runtime bypasses chat_stream; ToolExecutor / consent / verifier / memory /
cancellation invariants are all preserved. Tests: `tests/test_agent_runtime.py`.

## Milestone 5 — scoped memory fabric facade

`core/memory_fabric.py`: a policy layer unifying episodic / KnowledgeVault /
VectorMemory behind one facade — adapters wrap the stores unchanged (no
migration, the V62 "consolidate before adding" direction). Enforces secret
redaction on write, untrusted-source labeling (untrusted excluded from retrieval
by default = anti-injection), sensitivity + scope filters, bounded retrieval,
dedup, relevance+recency ranking, provenance on every record. `store()` is wired
into `LLM._maybe_persist_memory` (behavior-preserving); `retrieve()` is the read
API (also used by M8). Migrating the PageRank hot-retrieval path onto it is the
next gradual step. Tests: `tests/test_memory_fabric.py`.

## Milestone 8 — project & decision awareness

`core/project_context.py`: records/recalls project facts (goal / decision / task
/ blocked / question / artifact) via the M5 fabric at `scope="project"` with
provenance + timestamps — memory retrieval, not a static prompt. Wired as two
real tools: `project_note` (LOW_IMPACT) and `project_status` (READ_ONLY),
answering "what are we building / decided / blocked". Tests:
`tests/test_project_context.py`.

## Remaining milestones (M3, M4, M7) — deferred with seams identified

Built on the `TaskDecision` seam (M1) and the fabric (M5):

- **M3 task-graph planner** — a bounded DAG (reasoning/tool/agent/verification
  nodes, deps, retries, timeouts, cancellation, conservative concurrency),
  gated by `TaskDecision.requires_planning`. Not on the fast path.
- **M4 controlled multi-agent team** — generalize `AgentOrchestrator`
  (`SpecialistSpec` + a bounded `SharedBlackboard` with provenance + critic
  conflict detection), gated by `TaskDecision.prefers_agent_team`. Tool-capable
  specialists must delegate to the `ToolExecutor`-gated `CognitiveEngine.execute_step`
  (never a second raw LLM path) to preserve the no-bypass invariant; keep
  concurrency conservative (fast ≤2, deep ≤1) for the Ryzen 5 7430U.
- **M7 presence engine** — extend `AssistantState`/`ironman_mode` with the
  OBSERVE→UNDERSTAND→SUGGEST→ASK→ACT ladder; ACT must obey ToolExecutor / risk
  taxonomy / consent / HITL (never an autonomous bypass).

Recon call-graph maps for all three exist (this session's runtime recon); each is
a self-contained, testable, committable increment.

## Test / gate status (end of this session)

`pytest` (jarvis/tests + ../tests): **855 passed, 1 failed, 18 skipped**. The one
failure is the pre-existing, unrelated `test_relative_traversal_blocked` (V62
residual risk #5 — a host path quirk), preserved as known-pre-existing.
`ruff check .`: clean. `compileall core tools main.py`: clean.
