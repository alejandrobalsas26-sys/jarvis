# JARVIS — Asistente de IA Personal

Sistema modular de asistente de voz con LLM, inspirado en J.A.R.V.I.S.

## Arquitectura

```
[Tú hablas]
     ↓
[STT — faster-whisper]    ← Transcripción local, sin enviar audio a la nube
     ↓
[LLM — Claude Sonnet]     ← Razonamiento + decisión de herramientas
     ↓
[Tool Executor]           ← Clima, shell, GUI, WhatsApp...
     ↓
[TTS — ElevenLabs/pyttsx3] ← Respuesta en voz
```

## Setup rápido

```bash
# Linux/macOS
chmod +x setup.sh && ./setup.sh

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edita .env con tu API key
```

## Configuración (.env)

| Variable | Descripción | Ejemplo |
|---|---|---|
| `ANTHROPIC_API_KEY` | Tu API key de Anthropic | `sk-ant-...` |
| `ASSISTANT_NAME` | Nombre del asistente | `Alicia` |
| `USER_NAME` | Tu nombre | `Alejandro` |
| `CITY` | Tu ciudad (para clima) | `Panama` |
| `WHISPER_MODEL` | Tamaño del modelo STT | `small` |
| `ELEVENLABS_API_KEY` | Opcional — voz de alta calidad | *(vacío = offline)* |

## Uso

```bash
# Modo texto (sin micrófono — para desarrollo)
python main.py

# Modo voz completo
python main.py --voice

# Sin saludo inicial
python main.py --no-greeting
```

## Añadir nuevas tools

1. Agrega la definición en `core/llm.py` → lista `TOOLS`
2. Implementa el handler en `tools/executor.py` → método `_tool_<nombre>`

El LLM automáticamente aprenderá cuándo invocar la tool nueva.

## Roadmap

- [ ] Wake word con Porcupine (siempre escuchando)
- [ ] Módulo de seguridad WhatsApp
- [ ] Control de GUI con pyautogui
- [ ] Rutinas programadas (APScheduler)
- [ ] Migración a LLM local (Ollama + Llama)
