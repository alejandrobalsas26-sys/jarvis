# JARVIS

A **local-first, secure-by-default AI workstation** for an authorized homelab:
a voice/text assistant, SOC/DFIR automation, and a guarded tool executor — all
running against a local [Ollama](https://ollama.com) backend by default. Cloud
is opt-in.

> Authorized, defensive use only. JARVIS ships offensive-capable, **lab-only**
> modules (MITM proxy, C2 bridges, Metasploit RPC, RF tooling). They are not
> installed by the `base` profile and are gated at runtime behind human approval
> and the `JARVIS_TRUSTED_LAB` flag. See [SECURITY.md](SECURITY.md) and
> [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## What it is

- **Local-first brain** — Ollama on `localhost:11434`. No prompt data leaves the
  machine unless you explicitly enable the cloud backend.
- **Role-based model routing** — prompts are classified into cognitive roles
  (FAST / CODER / DEEP / VISION / EMBEDDING / VERIFIER) and routed to the right
  local model, with hardware-tier-aware recommendations.
- **Guarded tool executor** — allowlist, `shell=False`, shell-metacharacter
  blocking, path canonicalization, NATO vocal HITL approval, SSRF defense, audit
  logging, and PII detection.
- **SOC / DFIR suite** — YARA, packet inspection, forensic triage, detection
  engineering, RBAC, persistent state, and SIEM forwarding (TITAN).
- **Voice + text modes** — faster-whisper STT and pyttsx3 TTS (offline), or pure
  text mode for development.

## Repository layout

```
jarvis_v2/
├── jarvis/              # application package (flat layout)
│   ├── core/            # config, model_router, llm, memory, verification, SOC modules
│   ├── tools/           # executor + tool handlers (lab-gated where offensive-capable)
│   ├── scripts/         # doctor.py, model_doctor.py, install.ps1/.sh
│   ├── requirements/    # base, voice, docs, soc, lab, dev, all
│   ├── tests/           # app test suite
│   ├── main.py          # async orchestrator  (python -m jarvis)
│   └── pyproject.toml   # metadata, ruff + pytest config
├── tests/               # repo-level security/integration tests
├── docs/                # INSTALLATION, TROUBLESHOOTING, THREAT_MODEL
├── SECURITY.md  CHANGELOG.md
```

## Quick start

```bash
cd jarvis

# Windows
./scripts/install.ps1                 # base text mode; -Profile all for everything

# Linux/macOS
./scripts/install.sh                  # base; ./scripts/install.sh all for everything

python scripts/doctor.py              # PASS/WARN/FAIL environment check
ollama serve && python scripts/model_doctor.py   # tier + `ollama pull` guidance
python main.py                        # or: python -m jarvis
```

Full matrix and per-OS steps: [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Dependency profiles

`base` (text mode) · `voice` · `docs` (file/OCR) · `soc` (DFIR) · `lab`
(**offensive-capable, isolated lab only**) · `dev` · `all`. Base is intentionally
lean so text mode runs without audio/OCR/ML/lab dependencies.

## Recommended hardware tiers

| Tier | VRAM | Example role models |
|---|---|---|
| LOW | CPU / <12 GB | `qwen2.5-coder:7b`, `moondream` |
| MID | 12–16 GB | `qwen2.5-coder:14b`, `deepseek-r1:14b` |
| HIGH | 24–32 GB | `qwen2.5-coder:32b`, `deepseek-r1:32b` |
| EXTREME | 48 GB+ | `deepseek-r1:70b` |

`python scripts/model_doctor.py` detects your tier and prints the exact pulls.

## Security model (summary)

Every model-invoked tool passes the executor's layered checks; the LLM **cannot**
disable guardrails via tool arguments, and outbound HTTP blocks internal/metadata
targets. The only override is operator-set trusted-lab mode (env-only). Full
detail in [SECURITY.md](SECURITY.md) and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Development

```bash
cd jarvis
pip install -r requirements/dev.txt
ruff check .          # E9 + full pyflakes gate (CI fails on violations)
python -m pytest -q   # app suite;  (cd .. && python -m pytest tests/) for repo suite
```

## Known limitations

- A truly minimal `base` install is tuned for text mode; some eagerly-imported
  subsystems may want the `soc`/`lab` profiles.
- The verifier and memory-router modules are implemented and tested but not yet
  wired into the live streaming response path (next PR).

See [CHANGELOG.md](CHANGELOG.md) for the full history and roadmap.
