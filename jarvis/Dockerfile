# JARVIS — Multi-stage Docker build (BASE / text-mode image).
# Builder: compiles the base text-mode dependencies only (no audio/OCR/ML/lab).
# Runtime: minimal non-root image. For the full workstation, build with the
# `all` profile locally — see docs/INSTALLATION.md.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev libxml2-dev libxslt1-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Base profile only — keeps the image small and buildable on Linux (the full
# requirements.txt pins torch-directml, which is a Windows-only wheel).
COPY requirements/base.txt requirements/base.txt
RUN pip install --upgrade pip && pip install --prefix=/install -r requirements/base.txt


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JARVIS_ENV=production

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 libssl3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash jarvis
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=jarvis:jarvis . .

RUN mkdir -p logs && chown jarvis:jarvis logs

USER jarvis

# Ollama runs on the host/another container; reach it via this default.
ENV OLLAMA_HOST=http://host.docker.internal:11434

# Healthcheck: confirm the base Python environment imports cleanly. (The base
# image is text-mode; the AURA telemetry HUD requires the `all` profile.)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import core.config" || exit 1

# Entrypoint: main.py orchestrates all subsystems
# --no-greeting: skip TTS greeting in container
CMD ["python", "main.py", "--no-greeting"]
