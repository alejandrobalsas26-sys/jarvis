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

## Milestones 1-9 — status

Appended as each lands (see CHANGELOG for the running summary). Each milestone is
committed and pushed at a green boundary.
