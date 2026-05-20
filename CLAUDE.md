# JARVIS v2 — Guía de Arquitectura y Estilo

## Arquitectura General

```
jarvis_v2/
├── jarvis/
│   ├── main.py              # Orquestador asíncrono (entry point)
│   ├── core/
│   │   ├── config.py        # Settings Pydantic — ÚNICA fuente de verdad para .env
│   │   ├── llm.py           # Cliente Claude (AsyncAnthropic + streaming)
│   │   ├── tts.py           # Text-to-Speech (ElevenLabs / pyttsx3)
│   │   └── stt.py           # Speech-to-Text (faster-whisper)
│   ├── tools/
│   │   └── executor.py      # Handlers de tools con hardening de seguridad
│   └── requirements.txt
├── tests/
│   └── test_security.py     # Tests de inyección de comandos
└── CLAUDE.md
```

## Pipeline de Ultra-Baja Latencia

El audio empieza a sonar mientras el LLM sigue generando texto:

```
LLM.chat_stream()              asyncio.Queue              TTS.speak_async()
[AsyncGenerator]  ──chunks──►  [sentence splitter]  ──►   [ThreadPool]
      │                              │                           │
      │  genera tokens             split on                audio suena
      │  mientras TTS             [.!?;:]                 mientras LLM
      │  habla                                             sigue generando
      └──────────────── asyncio.gather(producer, consumer) ──────────────┘
```

Implementado en `main.py:_run_turn()`.

### Por qué funciona

- `LLM.chat_stream()` es un `AsyncGenerator[str, None]` — no bloquea el event loop.
- `TTS.speak_async()` usa `run_in_executor()` — el audio corre en un thread pool.
- `asyncio.Queue` desacopla producción y consumo: el buffer absorbe la diferencia de velocidad.
- `_split_sentences()` acumula chunks hasta detectar puntuación final antes de enviar a TTS.

## Modelo de Seguridad (Purple Team)

### executor.py — Capas de defensa

| Capa | Mecanismo | Bloquea |
|------|-----------|---------|
| 1 | `_FORBIDDEN_CHARS_RE` | Metacaracteres de shell: `; & \| \` $ < > ( ) { } ! \\ \n` |
| 2 | `shlex.split()` | Parseo seguro, sin interpolación de strings |
| 3 | `COMMAND_ALLOWLIST` | Solo ejecutables explícitamente permitidos |
| 4 | HITL | Aprobación humana antes de ejecutar cualquier shell command |
| 5 | `shell=False` | **Nunca** se pasa el comando a un intérprete de shell |
| 6 | Regex en inputs de red | Dominios/IPs validados antes de pasarlos a nmap/whois/ping |

### Reglas de oro

1. **`shell=False` siempre.** No existe ningún caso en este proyecto que justifique `shell=True`.
2. **Nunca interpolar user input en strings de comando.** Construir vectores `list[str]` con valores fijos.
3. **Allowlist, no denylist.** Es más seguro definir lo que está permitido que intentar bloquear lo malo.
4. **Variables de entorno solo via `settings`.** Nunca `os.getenv()` directo en módulos de negocio.

## Configuración — core/config.py

`settings` es un singleton de Pydantic `BaseSettings`. Lee de `.env` y valida tipos en el arranque.

```python
from core.config import settings

# Bien:
api_key = settings.anthropic_api_key.get_secret_value()
model   = settings.llm_model

# Mal — no usar directamente:
api_key = os.getenv("ANTHROPIC_API_KEY")
```

### Variables de entorno

| Variable | Tipo | Default | Descripción |
|----------|------|---------|-------------|
| `ANTHROPIC_API_KEY` | `SecretStr` | — | **Requerida** |
| `ELEVENLABS_API_KEY` | `SecretStr` | `""` | Opcional — activa ElevenLabs |
| `ELEVENLABS_VOICE_ID` | `str` | `""` | Requerida si hay API key de ElevenLabs |
| `ASSISTANT_NAME` | `str` | `Alicia` | Nombre del asistente |
| `USER_NAME` | `str` | `Alejandro` | Nombre del usuario |
| `CITY` | `str` | `Panama` | Ciudad para contexto y clima |
| `LLM_MODEL` | `str` | `claude-sonnet-4-6` | Modelo de Claude |
| `LLM_MAX_TOKENS` | `int` | `2048` | Tokens máximos de respuesta |
| `WHISPER_MODEL` | `str` | `small` | tiny/base/small/medium/large |
| `WHISPER_LANGUAGE` | `str` | `es` | ISO 639-1 o `auto` |
| `RECORD_SECONDS` | `int` | `5` | Duración de grabación de voz |
| `SAMPLE_RATE` | `int` | `16000` | Hz: 8000/16000/22050/44100/48000 |

## Cómo agregar una nueva Tool

1. **Registrar el handler** en `tools/executor.py`:
   ```python
   def _tool_mi_nueva_tool(self, param1: str, param2: int = 0) -> dict:
       # Validar inputs con regex si van a subprocess
       # Retornar siempre un dict con "error" o datos
       ...
   ```

2. **Declarar el schema** en `core/llm.py` dentro de `TOOLS`:
   ```python
   {
       "name": "mi_nueva_tool",
       "description": "Descripción clara para que Claude sepa cuándo usarla.",
       "input_schema": {
           "type": "object",
           "properties": {
               "param1": {"type": "string"},
               "param2": {"type": "integer"},
           },
           "required": ["param1"],
       },
   }
   ```

3. **Escribir tests** en `tests/test_security.py` si la tool acepta inputs que podrían ser usados en subprocess.

## Guía de Estilo

- **Sin comentarios innecesarios.** Solo documenta el WHY cuando no sea obvio.
- **Async-first.** Toda función que toca I/O o llama a una API debe ser `async def`.
- **`shell=False` siempre.** Sin excepciones.
- **Imports diferidos** para módulos pesados (pyttsx3, whisper, pyautogui) — se cargan solo cuando se necesitan.
- **Pydantic para validación.** No escribas lógica de validación de strings a mano si Pydantic puede hacerlo.
- **Retorno de tools:** siempre `dict`. En error, incluir la clave `"error"`.
- **Logging:** `logger.info()` para acciones, `logger.warning()` para bloqueos de seguridad, `logger.debug()` para datos verbosos.

## Tests

```bash
cd jarvis_v2
python -m pytest tests/test_security.py -v
```

Los tests no requieren `.env` ni conexión a internet — solo importan `tools/executor.py`.

## Inicio rápido

```bash
cd jarvis_v2/jarvis
cp .env.example .env
# Editar .env con tu ANTHROPIC_API_KEY

pip install -r requirements.txt

python main.py            # modo texto
python main.py --voice    # modo voz
python main.py --no-greeting --voice
```
