# JARVIS — application package

Modular local-first AI assistant: voice agent, SOC/DFIR automation, and a
guarded tool executor. This directory is the app root (flat layout — top-level
`core/`, `tools/`, `aura/`). For the project overview and install matrix see the
[root README](../README.md) and [docs/INSTALLATION.md](../docs/INSTALLATION.md).

## Architecture (real runtime)

```
[You speak / type]
       ↓
[STT — faster-whisper]      local transcription (voice mode; text mode skips this)
       ↓
[Role router → Ollama]      route() picks role/model per turn (FAST/CODER/DEEP/…)
       ↓                    on localhost:11434  (cloud is opt-in, off by default)
[Tool Executor]             allowlist + shell=False + NATO HITL + SSRF/guardrails
       ↓                    tool output enters history TRUST-LABELED (untrusted
       ↓                    for web/file/RAG/screen — data, never instructions)
[Verifier (high-risk only)] post-stream audit; flags issues, fail-closed
       ↓
[TTS — pyttsx3 / ElevenLabs]  pyttsx3 offline by default
```

The default LLM backend is **Ollama (local)**. `ANTHROPIC_API_KEY` / OpenRouter
are **optional** and only used when the cloud backend is explicitly enabled.

### V61 live brain (`core/`)

`llm.py` routes each turn through `model_router.route()`, runs a post-stream
`verification.verify_answer()` on high-risk turns, and applies
`memory_router` policy (secret-safe, scope-classified) before persisting.
`ironman_mode.py` / `task_queue.py` / `aura_events.py` provide the consent-gated
multimodal + background-task foundation. See [../CHANGELOG.md](../CHANGELOG.md).

## Quick start

```bash
# Windows
./scripts/install.ps1                 # base (text mode); -Profile all for everything
.\.venv\Scripts\Activate.ps1

# Linux/macOS
./scripts/install.sh                  # base; ./scripts/install.sh all for everything
source .venv/bin/activate

python scripts/doctor.py              # environment health
ollama serve && python scripts/model_doctor.py
python main.py                        # or: python -m jarvis
```

## Configuration (`.env`)

Copy `.env.example` → `.env` (the installer does this). Everything has a safe
default — text mode runs with an empty `.env`.

| Variable | Description | Default |
|---|---|---|
| `OLLAMA_HOST` | Ollama base URL (bare host auto-normalized) | `http://127.0.0.1:11434` |
| `LLM_MODEL` | Local Ollama model tag | `qwen3:8b` |
| `JARVIS_MODEL_*` | Per-role model overrides (FAST/CODER/DEEP/VISION/EMBEDDING/VERIFIER) | see below |
| `JARVIS_CLOUD_ENABLED` | Allow cloud escalation | `false` |
| `ANTHROPIC_API_KEY` | Optional — only for the cloud backend | *(empty = local-only)* |
| `JARVIS_TRUSTED_LAB` | Relax guardrails for an **isolated** lab | `false` |
| `ASSISTANT_NAME` / `USER_NAME` / `CITY` | Persona | `Alicia` / `Alejandro` / `Panama` |
| `WHISPER_MODEL` | STT model size (voice mode) | `small` |

### Model roles (V66.1 — one source of truth)

Every subsystem resolves models through one precedence ladder: **explicit
`JARVIS_MODEL_*` override → central role config → hardware recommendation
(advisory only) → installed-compatible fallback → safe fallback**. What you set
in `.env` is what actually runs — hardware profiling advises but never overrides.

| Role | Env var | Default |
|---|---|---|
| FAST (chat) | `JARVIS_MODEL_FAST` | `qwen3:8b` |
| CODER | `JARVIS_MODEL_CODER` | `qwen2.5-coder:latest` |
| DEEP (architecture/DFIR) | `JARVIS_MODEL_DEEP` | `qwen3:14b` |
| VISION | `JARVIS_MODEL_VISION` | `gemma3:4b` |
| EMBEDDING | `JARVIS_MODEL_EMBEDDING` | `nomic-embed-text:latest` |
| VERIFIER | `JARVIS_MODEL_VERIFIER` | `qwen3:8b` |

`python scripts/model_doctor.py` reads this same `.env` config, reports the
hardware tier's advisory recommendation, checks each role model is pulled, and
fires a bounded thinking-model-aware smoke test at FAST/VERIFIER.

**CPU-only inference (Ryzen 5 7430U / 64 GB):** system RAM — not the integrated
GPU's VRAM — is the model-capacity ceiling. Keep Ollama conservative:
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`.

## Usage

```bash
python main.py                 # text mode (no mic — dev default)
python main.py --voice         # full voice mode
python main.py --no-greeting   # skip TTS greeting
```

## Adding a tool (security-gated)

1. Implement the handler in `tools/executor.py` → `_tool_<name>` (return a `dict`,
   `{"error": ...}` on failure).
2. Declare the JSON schema in `core/llm.py` → `TOOLS`.
3. Add tests in `tests/` (security-relevant tools → assert the guardrails).

Risky tools stay gated by the NATO/HITL challenge; never weaken the executor
controls to make a tool easier to call. See [../SECURITY.md](../SECURITY.md).

## Tests / lint

```bash
python -m pytest -q          # app suite
ruff check .                 # E9 + full pyflakes gate (must pass)
```
