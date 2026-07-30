# JARVIS

A **local-first, operator-controlled AI workstation and authorized Purple
Team/SOC homelab platform**: a voice/text assistant, SOC/DFIR automation, and a
guarded tool executor — all running against a local
[Ollama](https://ollama.com) backend by default. Cloud is opt-in.

**Current release: V69 M61** — Production Stabilization, CI & Release
Engineering. The canonical version lives in
[`jarvis/core/version.py`](jarvis/core/version.py); package metadata,
diagnostics, release qualification and this README all derive from or are
verified against it (`core.release_check.audit()`, a mandatory CI gate).

Nothing JARVIS does is autonomous. Every high-risk OS/network action requires
human approval (HITL or NATO OTP challenge), the tool executor runs
`shell=False` against an allowlist, and the verifier pass is advisory — it flags
findings, it never acts on them.

> Authorized, defensive use only. JARVIS ships offensive-capable, **lab-only**
> modules (MITM proxy, C2 bridges, Metasploit RPC, RF tooling). They are not
> installed by the `base` profile and are gated at runtime behind human approval
> and the `JARVIS_TRUSTED_LAB` flag. See [SECURITY.md](SECURITY.md) and
> [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## What it is

- **Local-first brain** — Ollama on `localhost:11434`. No prompt data leaves the
  machine unless you explicitly enable the cloud backend.
- **Live role-based model routing** — every turn is classified into a cognitive
  role (FAST / CODER / DEEP / VISION / EMBEDDING / VERIFIER) and routed to the
  right local model in the streaming path, with hardware-tier-aware
  recommendations. High-risk turns get a post-stream **verifier** pass; tool
  output is **trust-labeled** (untrusted web/file/RAG/screen) and memory writes
  refuse secrets (V61).
- **Guarded tool executor** — allowlist, `shell=False`, shell-metacharacter
  blocking, path canonicalization, NATO vocal HITL approval, SSRF defense, audit
  logging, and PII detection.
- **SOC / DFIR suite** — YARA, packet inspection, forensic triage, detection
  engineering, RBAC, persistent state, and SIEM forwarding (TITAN).
- **Voice + text modes** — faster-whisper STT and pyttsx3 TTS (offline), or pure
  text mode for development.

- **Crash-safe session continuity (V69 M60)** — a versioned session/turn/run
  journal on the durable operational store. After an unclean exit the runtime
  reconciles interrupted turns; reconciliation has **no execution path**, so a
  recovered session never replays an effectful action.

## Repository layout

```
jarvis_v2/
├── .github/workflows/   # ci.yml — deterministic quality gates (V69 M61)
├── jarvis/              # application package (flat layout)
│   ├── core/            # config, model_router, llm, memory, verification, SOC modules
│   │   └── version.py   # THE canonical version — everything else derives from it
│   ├── tools/           # executor + tool handlers (lab-gated where offensive-capable)
│   ├── scripts/         # doctor.py, model_doctor.py, install.ps1/.sh, qualify_release_m61.py
│   ├── requirements/    # base, voice, docs, soc, lab, dev, all + constraints-ci
│   ├── tests/           # app test suite
│   ├── main.py          # async orchestrator  (python -m jarvis from this directory's parent)
│   └── pyproject.toml   # metadata (version is dynamic), ruff + pytest config
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

`requirements/<profile>.txt` is the **single dependency authority** (V69 M61).
`pyproject.toml` extras mirror it and are verified by
`core.dependency_authority.audit()` as a mandatory CI gate, so
`pip install jarvis[soc]` and `pip install -r requirements/soc.txt` cannot drift
apart. The legacy monolithic `jarvis/requirements.txt` is deprecated to a pointer
at `requirements/all.txt`. CI resolves against `requirements/constraints-ci.txt`,
which bounds only the test/lint toolchain — runtime packages needing
platform-specific resolution are deliberately left unpinned.

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
python -m pytest -q   # both suites: jarvis/tests + the repo-level tests/
```

`pytest` from `jarvis/` collects **both** trees (`testpaths = ["tests", "../tests"]`).
CI runs the same command; see [.github/workflows/ci.yml](.github/workflows/ci.yml).
It needs no Ollama, no models, no microphone, no Docker daemon, no Redis/Postgres
and no API keys — anything that does is skipped behind a marker.

Release qualification (read-only; mutates no git, host or Ollama state):

```bash
python jarvis/scripts/qualify_release_m61.py --quick
python jarvis/scripts/qualify_release_m61.py --full --output release.json
```

## Known limitations

- A truly minimal `base` install is tuned for text mode; some eagerly-imported
  subsystems may want the `soc`/`lab` profiles.
- Verification is **post-stream** and advisory (the draft streams first, then is
  audited; the verifier flags issues rather than rewriting the answer).
- Cloud escalation is supported by the router but **not streamed** from the local
  client; cloud remains opt-in and off by default.
- Iron Man Mode (V61) is a **consent-gated policy foundation** — always-on
  behavior is gated, with no silent screen/camera/clipboard capture.

See [CHANGELOG.md](CHANGELOG.md) for the full history and roadmap.
