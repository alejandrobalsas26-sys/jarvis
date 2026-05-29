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
