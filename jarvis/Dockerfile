# JARVIS V55.0 TITAN — Multi-stage Docker build
# Builder: compiles all Python dependencies
# Runtime: minimal non-root production image

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --prefix=/install -r requirements.txt


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JARVIS_ENV=production

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libssl3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash jarvis
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=jarvis:jarvis . .

RUN mkdir -p logs && chown jarvis:jarvis logs

USER jarvis

# AURA WebSocket telemetry server
EXPOSE 8765

# Healthcheck via AURA WebSocket port
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('localhost',8765),timeout=5); s.close()" \
    || exit 1

# Entrypoint: main.py orchestrates all subsystems
# --no-greeting: skip TTS greeting in container
CMD ["python", "main.py", "--no-greeting"]
