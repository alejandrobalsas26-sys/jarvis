# JARVIS

A complete autonomous Purple Team security platform.

Built across 46 versions on a Ryzen 5 7430U + 64GB DDR4 dual-channel.
Local LLM inference via Ollama. No external AI APIs.

---

## What JARVIS Is

JARVIS is your Purple Team analyst. It:

- Listens to your voice via Whisper
- Reasons via local Ollama (qwen2.5 7B/14B)
- Speaks via TTS
- Monitors your lab via ETW, Sysmon, Zeek, canaries, sensors
- Detects threats via correlation, YARA, Sigma, network baselining
- Hunts proactively using 12 ATT&CK hypotheses every 4 hours
- Attacks autonomously via ARES Red Team Operator
- Defends adaptively via auto-generated Sigma rules
- Remembers everything via ChromaDB + SQLite intelligence fusion
- Talks to you anywhere via Telegram bridge

---

## Architecture Overview

```
                  ┌──────────────┐
                  │   OPERATOR   │
                  └──────┬───────┘
              voice │    │  text/cli
                    │    │
        ┌───────────▼────▼───────────┐
        │      JARVIS CORE LOOP      │
        │  STT → LLM → ACT → TTS     │
        └───┬──────────────────────┬─┘
            │                      │
   ┌────────▼────────┐    ┌────────▼────────┐
   │  RED SUBSYSTEMS │    │ BLUE SUBSYSTEMS │
   │                 │    │                 │
   │  ARES Operator  │◄───┤   Correlator    │
   │  BAS Simulator  │    │   ETW Monitor   │
   │  mitmproxy      │    │   Sysmon Bridge │
   │  Adv Emulator   │    │   Zeek DPI      │
   │  Metasploit RPC │    │   YARA + Sigma  │
   │  Sliver C2      │    │   Canaries      │
   └────────┬────────┘    └────────┬────────┘
            │                      │
            └──────────┬───────────┘
                       │
        ┌──────────────▼──────────────┐
        │     PURPLE COORDINATOR      │
        │  Measures detection latency │
        │  Identifies coverage gaps   │
        │  Auto-improves Sigma rules  │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │   INTELLIGENCE FUSION DB    │
        │  Cross-session correlation  │
        │   Campaign tracking         │
        │   Diamond Model analysis    │
        └──────────────┬──────────────┘
                       │
        ┌──────────────▼──────────────┐
        │       OUTPUT CHANNELS       │
        │   AURA 3D HUD (Three.js)    │
        │   Telegram (Mobile)         │
        │   .docx Forensic Reports    │
        │   Markdown Journals         │
        └─────────────────────────────┘
```

---

## V63 — Unified General Agent Runtime

V63 turns JARVIS from a Purple-Team platform into a **general-purpose, local,
operator-controlled agent runtime** without losing any security capability. One
per-turn decision object drives everything, and every autonomous component is
bounded, gated, and resource-aware.

```
 Operator / Voice / Text / Vision / Event
                 │
                 ▼
        Unified Agent Runtime            core/agent_runtime.py
                 │
                 ▼
           TaskDecision                  domain · complexity · risk ·
      (composed once per turn)           planning · agents · tools ·
                 │                        verification · surface
                 ▼
          Context Assembly               memory fabric (M5) · project (M8)
                 │
                 ▼
        Bounded Task Graph Planner       core/task_graph.py  (M3)
     REASON·TOOL·AGENT·VERIFY·           cycle-safe · capped · timed ·
     SYNTHESIZE·WAIT·HUMAN_APPROVAL      cancellable · partial-failure
                 │
                 ▼
        Controlled Specialist Team       core/specialist_runtime.py (M4)
       ┌─────────┼─────────┐             ≤2 FAST · ≤1 DEEP · resource back-off
   Specialist Specialist Specialist      14 capability roles, one shared model
       └─────────┼─────────┘
                 ▼
          Shared Blackboard              bounded · deduped · provenance ·
                 │                        structured conflict detection
                 ▼
          Critic → Conflict → Verifier   evidence-driven fan-in
                 ▼
          Result Integrator (Synthesis)
                 │
                 ▼
       Response Surface Router (M6)       voice · text · hud · report …
                 │
                 ▼
       Memory Persistence (M5)           scoped · redacted · provenance
                 │
                 ▼
       Presence Engine (M7)              OBSERVE→UNDERSTAND→SUGGEST→ASK→ACT
```

### Cross-cutting control planes

- **Operator Authority + Scope** (`core/authority.py`) — *Reasoning Freedom ≠
  Execution Authority.* JARVIS reasons freely about exploits, malware, and
  offensive technique; *acting on a target* is gated by an operator-selected
  authority mode (STANDARD / ADMIN_LOCAL / RESEARCH / CTF / TRUSTED_LAB /
  PURPLE_TEAM / INCIDENT_RESPONSE) and a fail-closed `ScopePolicy`. Out-of-scope
  or expired-scope target actions are refused before any challenge. Authority is
  server-side only — untrusted content can never widen it.

- **Typed Security Capability Registry** (`core/capabilities.py`) — an honest,
  gated inventory of external tooling. Availability/version probes report what is
  actually installed; shipped adapters (dns_lookup via nslookup, cert_inspect via
  openssl) build validated `shell=False` argv vectors, parse to structured
  results, capture hashed evidence artifacts, and route through
  `ToolExecutor.run_capability` (authority + risk/HITL + audit). Tools that are
  not present are inventory-only descriptors — never fake wrappers.

### Non-negotiable invariants

- **No bypass.** Every world-effect — specialist tool call, task-graph TOOL node,
  capability adapter — delegates to `ToolExecutor.aexecute` (risk class · HITL ·
  authority scope · audit). There is no `shell=True`, `os.system`, direct-MCP, or
  raw-handler path anywhere in the new runtime.
- **Bounded.** Agent teams, task graphs, blackboards, retries, and background
  work all have hard caps and timeouts; nothing fans out or loops unbounded.
- **Resource-aware.** Concurrency and background work back off under CPU/RAM
  pressure and on battery — the Rule of Silicon holds on the 15W host.
- **Fast path preserved.** Simple chat still routes `TaskDecision → direct
  inference`; teams/graphs/presence only engage when genuinely warranted.

---

## Voice Commands

JARVIS responds to spoken macros (defined in `core/macros.yaml`). A selection:

| Trigger | Action |
|---------|--------|
| "jarvis status report" | ATT&CK coverage status |
| "jarvis run hunt" | Threat hunt sweep (12 hypotheses) |
| "jarvis weekly digest" | Cross-session intelligence digest |
| "jarvis war room" | Toggle War Room HUD mode |
| "jarvis telegram test" | Push test message to phone |
| "jarvis self test" | Validate all subsystems |
| "jarvis performance" | Per-subsystem latency report |
| "jarvis reload config" | Hot-reload `jarvis_config.yaml` |
| "jarvis start campaign" | ARES autonomous red-team campaign |
| "jarvis coverage gaps" | Detection coverage gap analysis |

---

## Keyboard Shortcuts (AURA HUD)

| Key | Action |
|-----|--------|
| `?` | Toggle help |
| `Ctrl+A` | ATT&CK coverage matrix |
| `Ctrl+S` | Security status panel |
| `Ctrl+T` | Tactical timeline |
| `Ctrl+M` | Metrics sidebar |
| `Space` | Pause/resume event log |
| `F` | Cycle event filter |
| `O` | OCR analyze screen |
| `X` | ARES campaign panel |
| `P` | BIFROST coverage heatmap |
| `W` | War Room mode |
| `Esc` | ABORT all / close overlays |

---

## Configuration

All runtime behavior is driven by `jarvis_config.yaml` (auto-generated on
first launch). Priority order: **YAML > environment variables > defaults**.
Edits hot-reload on save — no restart required.

Telegram mobile bridge requires:

```
JARVIS_TELEGRAM_TOKEN   = <bot token from @BotFather>
JARVIS_TELEGRAM_CHAT_ID = <your Telegram user ID from @userinfobot>
```

---

## Hardware Target

- CPU: AMD Ryzen 5 7430U (15W TDP, CPU-bound)
- RAM: 64GB DDR4 dual-channel
- All I/O, subprocesses, and inference are asynchronous to protect the
  main event loop. Heavy modules (Whisper, TTS, Torch) are lazy-loaded.

---

## V64 — Intelligence, Trust & Learning Fabric

V64 makes JARVIS *evidence-grounded, injection-resistant, and measurable*. It
adds a content-trust axis, a defense-in-depth injection firewall, evidence-first
research, and (later milestones) evaluation + curated failure datasets — the
scaffolding that must exist **before** any fine-tuning.

```
 User / Event / Agent Task → TaskDecision → Context Assembly
        │
        ▼
   Trusted Research Runtime (M11)         core/research_runtime.py
        │  query decomposition → search plan → source discovery
        ▼
   Source Trust Policy (M10)              core/source_trust.py
   PRIMARY · TRUSTED_SECONDARY ·          domain rules + structural signals,
   COMMUNITY · UNTRUSTED · BLOCKED        operator allow/blocklist, reputation
        │
        ▼
   Fetch / Retrieve → Prompt Injection Firewall (M12)   core/injection_firewall.py
        │  6 layers: source-trust · lexical · semantic (de-obfuscation) ·
        │  context-role · tool-isolation · memory-write
        ▼
   Claim Extraction → Cross-Source Correlation → Evidence Blackboard
        │
        ▼
   Specialist Agents → Critic → Verifier → Cited Synthesis
        │  (code artifacts) → Security Analyzer (M13)  core/security_analyzer.py
        │                     AST taint pass → SQLi/RCE/SSRF/… findings
        ▼
   Eval Logger (M14) → Failure Repository (M16) → Curated Dataset
        → LoRA/SFT (M17) → Offline Evals → Promotion Gate → Model Registry
```

### Cross-cutting: three orthogonal trust axes

JARVIS now distinguishes **three** independent trust questions, each with its own
module — none is a substitute for another:

| Axis | Question | Module |
|------|----------|--------|
| Execution trust | May this *command* run without a challenge? | `core/trust_engine.py` |
| Authority/scope | May we *act on this target*? | `core/authority.py` |
| **Content trust** | How much should this *source/data* be believed & obeyed? | `core/source_trust.py` + `core/injection_firewall.py` |

### M10 — Trusted Source Registry (`core/source_trust.py`)

Deterministic, pure classification of a fetched URL into a `SourceTrustTier`.
Operator blocklist is absolute; allowlist overrides; **unknown ⇒ UNTRUSTED**
(never COMMUNITY). Structural caps (IP host, non-HTTPS, firewall injection flag)
only ever *lower* a tier — reputation is a soft ranking prior and never promotes.
`CitationRecord` is valid only if the source was *actually fetched* (no invented
citations); critical claims require an authoritative source **and** ≥2 distinct
corroborations. Operator-only config knobs (`source_trust_allowlist/blocklist`,
`source_require_https`), env/.env only.

### M12 — Prompt Injection Firewall (`core/injection_firewall.py`)

**Origin-aware** defense-in-depth. The same text is a benign question from
`OPERATOR_INPUT` but an attack from `WEB_UNTRUSTED`; the firewall combines
attack-typed lexical + semantic detection (NFKC + zero-width strip + base64/hex
de-obfuscation) with a `TrustOrigin`, so benign mentions are not quarantined
while real injections are. **Enforcement is structural, not detection-dependent:**

- Untrusted/ingested content can **never** authorize a tool call
  (`tool_influence_allowed` is False for every non-operator/non-system origin).
- Firewall-flagged untrusted content is **never persisted** to memory
  (`memory_fabric.store` refuses it — stored/second-order injection defense).
- The firewall **cannot** mutate authority or scope — it imports nothing from
  `core.authority` and never calls `set_mode`/`add_scope` (test-asserted).

Wired at the single tool-loop choke point (`llm.py::_label_tool_result`, covering
both local **and** MCP results — MCP is now correctly labeled untrusted) and at
the memory-write path. High-severity untrusted content is quarantined
(replaced with a neutral, observable stub) even at moderate confidence.

### M11 — Trusted Research Runtime (`core/research_runtime.py`)

Evidence-grounded research that **drives** existing pieces rather than adding
parallel infra: query decomposition → source discovery → **M10** trust
classification → fetch (guarded `ToolExecutor.aexecute`, never raw `requests`) →
**M12** injection scan → claim extraction → `SharedBlackboard` evidence →
cross-source correlation → conflict detection → optional verifier → cited
synthesis. Emits a structured `ResearchResult` (claims, evidence, sources,
conflicts, confidence, unresolved_questions, citations).

Guarantees: **no invented citations** (a `CitationRecord` exists only for an
actually-fetched source), bounded queries/sources/content, BLOCKED sources are
never fetched, and injected pages are **quarantined and excluded from evidence**
(never become a claim). The claim/correlate/conflict/synthesis stages are pure,
so a research run is reproducible offline (no live Ollama/network) — search/fetch
are injectable; production attaches them to the guarded executor at boot
(`attach_research_runtime` in `main.py`) with the fail-closed verifier as the
verify hook.

### M14 — Evaluation Harness (`core/eval_harness.py`)

The measurement layer that MUST exist **before** any fine-tuning. Runs versioned
`EvalCase` JSONL datasets against any *target* (a turn, a research run, the
firewall, the analyzer) that conforms to a small output contract, scores each
case **deterministically wherever possible** (model-graded only when a `rubric`
demands it), and emits reproducible JSON/JSONL results with baseline comparison
and regression detection. Only the dimensions a case *specifies* are scored, so a
case with no ground truth simply skips correctness (no false failures).

Reuses `CriticEngine` + `VerificationResult` (no second scorer) — and adds the
`CriticEngine` regression-floor tests it never had. `compare_runs` reports
per-metric **and** pass-rate deltas, so nothing is ever "looks better": a change
is only promotable when it does not regress. Seeded set:
`evals/prompt_injection/injection_resistance.jsonl` — the M12 firewall scores a
measured **100%** resistance on the adversarial set (attacks quarantined, benign
controls not false-positived). Timeouts and target exceptions fail closed to a
failed case; one bad case never aborts the suite.

### M13 — Code & Query Security Analyzer (`core/security_analyzer.py`)

A deterministic **AST** analyzer that answers "is this generated/reviewed code
insecure?" without executing it — so JARVIS can catch its own (and others')
dangerous code before it ships. A two-pass design first approximates taint
(`request.*`, `input()`, `os.environ`, and any name flowing from an external
source or dynamic-string build), then visits call/assignment sites to classify
findings across 11 `VulnCategory` families: `SQL_INJECTION`,
`COMMAND_INJECTION`, `INSECURE_SUBPROCESS`, `PATH_TRAVERSAL`, `SSRF`,
`UNSAFE_DESERIALIZATION`, `TEMPLATE_INJECTION`, `PROMPT_INJECTION_SINK`,
`DYNAMIC_CODE_EXECUTION`, `CREDENTIAL_LEAKAGE`, `WEAK_CRYPTO`.

**False-positive discipline is the point.** SQLi flags *dynamic-string
construction reaching a query sink* — concatenation, f-strings, `.format()`,
`%`, and `sqlalchemy.text()` misuse — while parameterized queries (`?`/`%s`
with a params tuple), ORM filters, stored-procedure calls, and fully-static
constant queries are left clean. Tainted input escalates severity to
`CRITICAL`; a merely dynamic (but not externally-tainted) query is `HIGH`.
Every `SecurityFinding` carries `confidence`, `evidence`, `data_flow`,
`remediation`, a suggested `regression_test`, and a CWE id. A `SyntaxError`
yields **no** findings (never a crash). Exposed as a pure `analyze_code()`,
scored through the M14 harness via `security_analyzer_eval_target()`, and
regression-locked by `evals/sql_injection/sqli.jsonl` (vulnerable + safe
controls, **100%** correct classification).

### M16 — Failure Dataset Pipeline (`core/dataset_pipeline.py`)

The *data* half of "eval-infra-before-training": it turns **M14 evaluation
failures** into training-candidate examples and forces each through a
fail-closed gauntlet before it can ever reach fine-tuning (M17):

```
eval failure → candidate → dedup → PII/secret scan → injection scan →
source-trust check → quality filter → verifier review → HUMAN-APPROVAL → versioned JSONL
```

Each gate **reuses** an existing trust primitive rather than duplicating it —
secret/PII from `memory_router` + `dlp_sensor`, content-trust of supporting
refs from **M10** `source_trust`, injection screening from **M12**
`injection_firewall`, the failing run from **M14** `eval_harness`, and an
injectable verifier (production wires the VERIFIER-role `verify_answer`). The
non-negotiables are structural, not advisory:

- **Nothing auto-approves.** `evaluate()`/`curate()` can at best mark a candidate
  `PENDING_REVIEW`; only an explicit human `approve(example, approver)` yields
  `APPROVED`, and `write_dataset()` writes **only** `APPROVED` rows.
- **Model text is never ground truth.** A `MODEL_GENERATED` target cannot pass
  without *both* a verifier verdict (≥ confidence floor) *and* a trusted
  corroborating source — no verifier ⇒ fail-closed reject.
- **No raw-internet training.** A `BLOCKED` source is fatal; a model target with
  only untrusted support is rejected. Targets are never fabricated — a failure
  with no trustworthy ideal is left for human authoring, logged, not invented.
- **No secrets in datasets.** Any secret/PII or injection match **quarantines**
  the candidate out of the trainable pool.
- **Reproducible + versioned + honest.** IDs are content hashes, timestamps are
  injected (`now_ts`), datasets are written to **immutable** `<version>/` dirs
  with a content-hash `manifest.json`, and every gate verdict (including
  rejections and skipped-unapproved counts) is recorded — regressions are never
  hidden. Tests: `tests/test_dataset_pipeline.py` (35).

---

## V65 — Adaptive Learning Runtime

V65 closes the adaptive-intelligence lifecycle: JARVIS learns *what specialist
skills are expected*, *measures* them, and — only on measured evidence — decides
whether a failure needs better retrieval, tools, routing, planning, prompts,
training, or a stronger model. The lifecycle is **measurable, reproducible, and
reversible** end-to-end:

```
real interaction → evaluation → failure classification → M16 curated candidate →
trust/secret/injection/quality gates → human approval → versioned dataset →
training experiment → candidate artifact → offline eval → baseline comparison →
promotion gate ─┬─ promote → route model → observe → detect regression → rollback
                └─ reject → archive
```

### M15 — Agent Skill Profiles (`core/skill_profiles.py`)

A **SkillProfile is an evaluation + operating contract** for a specialist role —
not another prompt directory and not another agent runtime. It sits on top of the
existing `SpecialistSpec` (the single source of truth for a role's model tier,
tool categories, context budget, and memory scope) and adds the missing
measurable-quality layer: owned `TaskDomain`s, preferred `ModelRole` (advisory —
`route()` stays authoritative), **evidence** and **verification** policies, the
eval datasets that benchmark the role, per-role quality metrics with minimum
**promotion thresholds**, and latency/resource budgets. One profile per role for
all **15** specialist roles.

**A profile can only ever *narrow* a spec.** `validate_against_spec` rejects any
profile that grants a tool category the spec lacks, raises the context budget
above the spec, or changes the tier — and `_build_default_registry` runs that
validation **fail-closed at construction**, so a capability-widening profile can
never ship. A profile therefore *cannot* weaken ToolExecutor, authority, or
scope: it has no channel to.

The registry is a **real production caller**, not documentation — it is wired
into `AgentTeamSelector`: a high-risk domain's profile (RESEARCH, DFIR,
CYBER_PURPLE, GRC, CYBER_BLUE) **forces the VERIFIER into the team**, additively
(it can add verification, never remove a role or grant a capability), respecting
the global agent cap. `SkillEvaluationSummary.from_eval_run` scores a role
against a real M14 `EvalRun`, so promotability is *measured* against the profile's
thresholds (an absent gating metric fails closed), never asserted. Tests:
`tests/test_skill_profiles.py` (25).

### M17 — Reproducible Training Pipeline (`core/training_pipeline.py`)

A *practical* training-experiment system for the local host (Ryzen 5 7430U,
64 GB, no GPU). It does **not** pretrain from scratch and it **never fakes a
training run**: with no available backend, an experiment plans and validates but
reports honestly that it did not execute. Backends are pluggable
(`TrainingBackend` protocol) so Transformers/PEFT/TRL/Unsloth/Axolotl can each be
an adapter — but only adapters whose dependencies are actually installed report
`available=True`. On this host torch + transformers are present while
`peft`/`trl`/`bitsandbytes` are absent, so SFT/LoRA/QLoRA/DPO are *planned but not
executable*, and the pipeline records an honest `FAILED` run rather than a
fabricated success.

The **safety contract** is enforced before any run: `verify_dataset` accepts
**only** an M16 dataset (or an equivalently manifested import) and re-checks
existence, manifest, pinned version, **content-hash match** (via the shared
`dataset_content_hash` — one source of truth with M16), all-`APPROVED` status, no
quarantined/rejected/secret-bearing records (re-scanned with `memory_router` +
`dlp_sensor`), schema, and a minimum sample count. A **dry run** reports estimated
examples/tokens, sequence length, a deterministic memory-pressure estimate,
backend availability, and the expected artifact path — without executing.
Backends emit **argv lists** (`shell=False`), never shell strings; execution is
explicit (`execute(config, confirm=run_id)`), never automatic, never on the chat
loop; and a `run_id` can never silently overwrite another. Metadata lives under a
versioned `training/` tree. Tests: `tests/test_training_pipeline.py` (23).

### Model Registry + Promotion/Rollback (`core/model_registry.py`)

The system of record for model artifacts and the **only** path by which a model
becomes ACTIVE for a role. A model is never promoted because "it feels smarter":
promotion is an evidence-based comparison of a candidate's
`ModelEvaluationSnapshot` (built from a real M14 `EvalRun`) against the current
baseline for that role, governed by a fail-closed `PromotionPolicy`.

- **No promotion without evaluation** — a candidate with no snapshot is rejected.
- **Critical regressions always block** — a drop on any safety dimension
  (injection resistance, tool safety, forbidden-output, verification, citation
  validity) blocks promotion regardless of gains elsewhere; the overall pass-rate
  may not fall past the regression budget.
- **Role-specific promotion** — assignments are per `ModelRole`; a coder candidate
  can win CODER (measured on its target domains) without displacing FAST/general.
  Tradeoffs are allowed within budget — one model need not win every domain.
- **Reversible** — every activation records a `RollbackPointer` to the model it
  replaced; `rollback(role)` restores it, deprecating the regressor. Promotion
  history is append-only and never rewritten — regressions are never hidden.
- Model versions are immutable identities (duplicate `model_id` refused); adapter
  artifacts are hash-verified on registration; the registry round-trips to JSON.
  Tests: `tests/test_model_registry.py` (17).

### M18 — Continuous Improvement Loop (`core/improvement_loop.py`)

M18 is a **coordinator, not a new engine** — it sequences the systems already
built and duplicates none of them (no second evaluator, curator, trainer, or
router). It enforces the central discipline: **fine-tuning is not the answer to
every failure.** A deterministic classifier maps each M14 failure to the cheapest
adequate remedy:

| Failure | Category | Remedy (not training) |
|---------|----------|-----------------------|
| injection not resisted | `PROMPT_INJECTION_FAILURE` | firewall / adversarial eval |
| wrong/ missing tool | `TOOL_SELECTION_ERROR` | tool schema / examples |
| forbidden tool used | `SCOPE_POLICY_ERROR` | human scope-policy review |
| bad routing | `ROUTING_ERROR` | routing policy / eval data |
| invalid citation | `CITATION_ERROR` | trusted RAG |
| evidence-domain miss | `KNOWLEDGE_GAP` | trusted RAG |
| timeout / OOM | `TIMEOUT` / `RESOURCE_PRESSURE` | scheduling policy |
| genuine reasoning/hallucination gap | `REASONING_ERROR` / `HALLUCINATION` | **training candidate** |

Only genuine reasoning/hallucination failures **with a trustworthy target**
become training candidates — and even then they flow through **M16 curation**,
stopping at `PENDING_REVIEW`; the coordinator **never approves, trains, or
promotes** (all remain explicit and human-gated). A training-warranted failure
with no trustworthy target routes to human authoring rather than fabricating a
target. Cycles are **bounded** (excess events deferred and *counted*, never
silently dropped). Tests: `tests/test_improvement_loop.py` (16).

### Model Tournament (`core/model_tournament.py`)

Evaluates registered models on the **same** eval basis and produces *empirical,
reviewable* routing recommendations — it never re-routes production by itself
(promotion stays governed by the registry's evaluation gate). Bounded to
`max_participants` (never an unbounded sweep); the tournament scores injected
snapshots (produced one-model-at-a-time upstream, so N models are never resident
at once). Ranking is **deterministic** (score desc, then `model_id` asc), and the
composite score is **domain-aware** with a latency/resource penalty and a safety
weighting — so a fast-but-injection-weak model cannot top a leaderboard on speed
alone, and a coder model can win CODER without winning every domain. Output is a
per-domain leaderboard plus a `RoutingRecommendation` (`by_domain` / `by_role`,
mapped through the M15 skill profiles) that is explicitly **advisory**
(`auto_applied: false`). Tests: `tests/test_model_tournament.py` (12).

---

*GENESIS — v46.0. The collection of subsystems became one thing: JARVIS.*

*V63 — the one thing became a general agent: bounded, gated, resource-aware,
operator-controlled.*

*V64 — the general agent learned what to trust: evidence-grounded,
injection-resistant, measurable, and able to curate its own failures into
vetted, human-approved training data.*

*V65 — the agent learned what skills it owes, how to measure them, and how to
improve on evidence: reproducible, promotable, reversible.*
