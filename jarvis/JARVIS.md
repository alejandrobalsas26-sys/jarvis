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
        │
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

---

*GENESIS — v46.0. The collection of subsystems became one thing: JARVIS.*

*V63 — the one thing became a general agent: bounded, gated, resource-aware,
operator-controlled.*

*V64 — the general agent learned what to trust: evidence-grounded,
injection-resistant, and measurable.*
