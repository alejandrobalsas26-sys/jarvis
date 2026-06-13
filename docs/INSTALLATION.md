# Installing JARVIS

JARVIS is **local-first**: the default LLM backend is [Ollama](https://ollama.com)
on `localhost:11434`. Cloud is opt-in. Python **3.11+** is required. Windows is
the primary target; Linux/macOS are supported.

All commands run from the `jarvis/` directory.

## Dependency profiles

| Profile | Install | Adds |
|---|---|---|
| `base` | text mode core | LLM client, web/system tools, config |
| `voice` | STT + TTS + VAD | `sounddevice`, `faster-whisper`, `pyttsx3` |
| `docs` | file reading / OCR / office | `pdfplumber`, `python-docx`, OCR |
| `soc` | defensive SOC / DFIR | YARA, scapy, pefile, capstone, TITAN |
| `lab` | **lab-only**, offensive-capable | docker, mitmproxy, OSINT, GUI automation |
| `dev` | testing / linting / audit | pytest, ruff, bandit, pip-audit |
| `all` | everything (incl. RAG/torch) | the full workstation |

Install a profile together with `base`, e.g.:

```bash
pip install -r requirements/base.txt -r requirements/voice.txt
```

## Windows (PowerShell)

```powershell
cd jarvis
./scripts/install.ps1                 # base profile
# ./scripts/install.ps1 -Profile all  # everything
.\.venv\Scripts\Activate.ps1
python scripts/doctor.py
```

## Linux / macOS

```bash
cd jarvis
./scripts/install.sh                  # base profile
# ./scripts/install.sh all            # everything
source .venv/bin/activate
python scripts/doctor.py
```

## Ollama setup

```bash
ollama serve
python scripts/model_doctor.py        # shows tier + missing `ollama pull` commands
```

`model_doctor.py` prints your hardware tier (LOW/MID/HIGH/EXTREME), the configured
role models, and the exact `ollama pull` commands for anything missing. It exits
0 even if Ollama is down (pass `--require-ollama` to make that a hard failure).

## Run

```bash
python main.py        # or: python -m jarvis
```

### What happens each turn (V61 live brain)

No new env vars are required. Each turn is routed by role (`model_router.route()`)
to a local model; high-risk turns (security-sensitive, tool-using, deep) get a
post-stream **verifier** pass using the `VERIFIER` role model (override with
`JARVIS_MODEL_VERIFIER`). Tool output is trust-labeled (untrusted for
web/file/RAG/screen) and memory writes refuse secrets. The verifier model is the
same small local model by default — pull it like any other (see `model_doctor.py`).
Iron Man Mode (`core/ironman_mode.py`) is a **consent-gated** foundation: screen,
camera, clipboard, and microphone stay OFF until you explicitly enable them for a
session — there is no silent capture.

## Docker (base text mode)

```bash
cd jarvis
docker build -t jarvis:base .
docker run --rm -it jarvis:base
```

The image is non-root, installs the `base` profile only, and reaches Ollama via
`host.docker.internal:11434` by default. For the full workstation, install the
`all` profile locally rather than in the container.

## Configuration

Copy `.env.example` → `.env` (the installer does this for you) and adjust as
needed. Everything has a safe default; you can run text mode with an empty
`.env`. See `docs/THREAT_MODEL.md` before enabling `JARVIS_TRUSTED_LAB`.
