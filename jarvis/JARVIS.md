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

*GENESIS — v46.0. The collection of subsystems became one thing: JARVIS.*

*V63 — the one thing became a general agent: bounded, gated, resource-aware,
operator-controlled.*
