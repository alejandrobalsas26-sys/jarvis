# Troubleshooting

Run `python scripts/doctor.py` first — it diagnoses most of the issues below and
prints a PASS/WARN/FAIL table. All commands run from the `jarvis/` directory.

## Install / environment

**`doctor.py` reports a FAIL on a base import**
Text mode can't run. Install the base profile into your venv:
`pip install -r requirements/base.txt` (or re-run `scripts/install.*`).

**"Python 3.11+ is required"**
The installer and CI target 3.11. Install a 3.11+ interpreter and recreate the
venv (`python3.11 -m venv .venv`). Note: f-string expressions with backslashes
are a 3.12+ feature — `ruff check .` (target py311) will catch those.

**Virtualenv WARN**
Not fatal — but activate the venv (`.\.venv\Scripts\Activate.ps1` /
`source .venv/bin/activate`) so you don't install into the system Python.

## Ollama / models

**`doctor.py` / `model_doctor.py`: "Ollama not reachable"**
Start the server: `ollama serve`. If it runs on another host/port, set
`OLLAMA_HOST=http://<host>:11434`.

**A role model is "not pulled"**
`model_doctor.py` prints the exact commands, e.g. `ollama pull qwen2.5-coder:7b`.
Pull at least the FAST and (if used) VERIFIER models.

**Model too slow / OOM on a laptop**
You're likely on the LOW tier. Override roles with smaller tags in `.env`
(`JARVIS_MODEL_DEEP=qwen2.5:7b-instruct`) and let the hardware profiler shrink the
context window. `python scripts/model_doctor.py` shows the recommended set.

**Docker container can't reach Ollama**
The base image defaults to `OLLAMA_HOST=http://host.docker.internal:11434`. On
Linux, run with `--add-host=host.docker.internal:host-gateway` or point
`OLLAMA_HOST` at the host IP.

## Tools / security gates

**A tool returns "GUARDRAIL: operación bloqueada"**
A destructive pattern (root delete / system-dir write) was detected. This is
intended. For an **isolated, authorized lab** only, set `JARVIS_TRUSTED_LAB=true`
in `.env`. Passing `FORCE_OVERRIDE` in a tool call does **not** work by design.

**`http_request` returns "Destino interno bloqueado (SSRF)"**
The URL resolves to a loopback/private/link-local/metadata address. Use a public
target, or enable `JARVIS_TRUSTED_LAB=true` for internal lab ranges.

**`run_shell_command` says "no está en la allowlist"**
Only allowlisted binaries run. Add the binary to `COMMAND_ALLOWLIST` in
`tools/executor.py` if it's genuinely needed (and safe), with a test.

**NATO challenge never completes / no microphone**
The challenge falls back to a keyboard `y/N` prompt when STT is unavailable. In
non-interactive contexts, use the read-only/exempt tools or run with a TTY.

## Voice mode

**No audio / `requires_audio`**
Install the voice profile: `pip install -r requirements/voice.txt`. On Linux you
may need PortAudio (`sudo apt-get install portaudio19-dev`) for `pyaudio`.

**OCR / `pytesseract` errors**
Install the `docs` profile and the Tesseract binary in the OS
(`tesseract-ocr`), or rely on the bundled `easyocr` fallback.

## Tests / CI

**`pytest` import errors for win32/scapy on Linux**
`tests/conftest.py` stubs Windows-only and raw-socket modules automatically.
Run from the `jarvis/` directory so the pythonpath is correct.

**Async tests**
The suite uses `asyncio.run()` inside sync tests (no `pytest-asyncio` config).
Follow that pattern for new async tests.

**`ruff check .` fails**
The gate is `E9` + full pyflakes (`F`). Fix the reported issue; only add a
per-file ignore for a *proven* false positive, with a comment explaining why
(see `pyproject.toml` for the two existing, documented cases).

Still stuck? See [THREAT_MODEL.md](THREAT_MODEL.md) for the security model and
[INSTALLATION.md](INSTALLATION.md) for the full setup matrix.
