"""
main.py — Orquestador asíncrono con pipeline Producer-Consumer de ultra-baja latencia.

Arquitectura de un turno:

    LLM.chat_stream()           asyncio.Queue           TTS.speak_async()
    [AsyncGenerator]  ──chunks──►  [sentence buffer]  ──sentences──►  [ThreadPool]
         │                              │                                   │
         │  genera tokens             split on                        audio suena
         │  mientras TTS             [.!?;:]                         mientras LLM
         │  habla                                                     sigue generando
         └──────────────────────── asyncio.gather ───────────────────────────────┘

Uso:
    python main.py           # modo texto
    python main.py --voice   # modo voz completo (STT + LLM + TTS)
    python main.py --no-greeting
"""

import re
import sys
import asyncio
import argparse
from dotenv import load_dotenv
from loguru import logger

# load_dotenv ANTES de cualquier import que use settings (pydantic-settings
# también lee el archivo, pero esto garantiza que os.environ esté poblado)
load_dotenv()

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
)
logger.add("jarvis.log", level="DEBUG", rotation="10 MB", retention="7 days")

# Regex para detectar final de oración — se usa en el sentence splitter
_SENTENCE_END_RE = re.compile(r'(?<=[.!?;:])\s+')


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """
    Divide el buffer en oraciones completas y el resto pendiente.

    Returns:
        (oraciones_completas, resto_sin_terminar)
    """
    parts = _SENTENCE_END_RE.split(buffer)
    if len(parts) <= 1:
        return [], buffer
    # Si el buffer termina con puntuación, todas las partes están completas
    if re.search(r'[.!?;:]\s*$', buffer):
        return [p for p in parts if p.strip()], ""
    return [p for p in parts[:-1] if p.strip()], parts[-1]


async def _run_turn(llm, tts, user_input: str, name: str) -> None:
    """
    Ejecuta un turno completo de conversación con el modelo Producer-Consumer.

    El producer consume el stream del LLM, acumula chunks en un buffer y
    encola oraciones completas. El consumer desencola y habla cada oración.
    Ambas corrutinas corren en paralelo via asyncio.gather.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer() -> None:
        buffer = ""
        async for chunk in llm.chat_stream(user_input):
            print(chunk, end="", flush=True)
            buffer += chunk
            sentences, buffer = _split_sentences(buffer)
            for sentence in sentences:
                await queue.put(sentence)
        # Volcar el remanente final (puede no tener puntuación al final)
        if buffer.strip():
            await queue.put(buffer.strip())
        await queue.put(None)  # sentinel: indica al consumer que terminó

    async def consumer() -> None:
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            await tts.speak_async(sentence)

    print(f"\n[{name}] ", end="", flush=True)
    await asyncio.gather(producer(), consumer())
    print()  # newline tras el output del streaming


async def _greeting(llm, tts, name: str, user: str) -> None:
    await _run_turn(
        llm,
        tts,
        f"Saluda a {user}. Dile la hora actual y pregúntale en qué lo puedes ayudar. "
        "Sé concisa y con tu personalidad habitual.",
        name,
    )


async def _loop_text(llm, tts, name: str) -> None:
    loop = asyncio.get_running_loop()
    print("Modo texto activo. Escribe tu mensaje o 'salir' para terminar.\n")

    while True:
        try:
            # run_in_executor para no bloquear el event loop con input()
            user_input = await loop.run_in_executor(None, input, "Tú: ")
            user_input = user_input.strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCerrando...")
            break

        if not user_input:
            continue
        if user_input.lower() in ("salir", "exit", "quit"):
            await tts.speak_async("Hasta luego.")
            break

        await _run_turn(llm, tts, user_input, name)


async def _loop_voice(llm, tts, stt, name: str) -> None:
    loop = asyncio.get_running_loop()
    print("Modo voz activo. Presiona Enter para hablar. Ctrl+C para salir.\n")

    while True:
        try:
            await loop.run_in_executor(None, input, "[Enter para hablar | Ctrl+C para salir]")
            user_input: str = await loop.run_in_executor(None, stt.listen)

            if not user_input:
                print("(sin voz detectada, intenta de nuevo)")
                continue

            print(f"Tú: {user_input}")

            if any(w in user_input.lower() for w in ("salir", "adiós", "hasta luego")):
                await tts.speak_async("Hasta luego.")
                break

            await _run_turn(llm, tts, user_input, name)

        except KeyboardInterrupt:
            print("\nCerrando...")
            break


async def _main_async() -> None:
    parser = argparse.ArgumentParser(description="JARVIS — Asistente de IA Personal")
    parser.add_argument("--voice", action="store_true", help="Activa modo voz (STT + TTS)")
    parser.add_argument("--no-greeting", action="store_true", help="Omite el saludo inicial")
    parser.add_argument("--no-aura", action="store_true", help="Disable AURA WebSocket server")
    args = parser.parse_args()

    from tools.executor import ToolExecutor, _aura_broadcast
    from core.llm import LLM
    from core.tts import TTS
    from core.config import settings
    from core.audio import HighPrioritySTTListener
    from core.events import make_event
    from core.healthcheck import run_startup_diagnostic
    from core.task_watchdog import TaskWatchdog, RestartPolicy
    from core.episodic_memory import store_episode  # noqa: F401 — ensures module is warm
    from core.trust_engine import load_profile      # noqa: F401 — ensures module is warm
    from tools.sysmon_bridge import start_sysmon_bridge

    # Async bridge queue: STT threads push (text, confidence) here via
    # loop.call_soon_threadsafe; the executor's _challenge() awaits from it.
    stt_queue: asyncio.Queue = asyncio.Queue()

    # Pre-load Whisper in a high-priority background thread.
    # The model is ready before the LLM starts, preventing CPU contention.
    audio_listener = HighPrioritySTTListener()

    executor = ToolExecutor(stt_queue=stt_queue, stt_listener=audio_listener)
    llm = LLM(tool_executor=executor)
    tts = TTS()

    # In voice mode, reuse the already-loaded HighPrioritySTTListener
    # to avoid loading Whisper a second time.
    stt = audio_listener if args.voice else None

    # ── Startup diagnostic ────────────────────────────────────────────────────
    diag = await run_startup_diagnostic()
    s    = diag["summary"]
    logger.info(f"STARTUP DIAGNOSTIC — OK:{s['ok']} MISSING:{s['missing']} BROKEN:{s['broken']}")
    for name, info in diag["subsystems"].items():
        if info["status"] != "OK":
            logger.warning(f"  [{info['status']}] {name}: {info['detail'][:100]}")

    # ── AURA WebSocket server ─────────────────────────────────────────────────
    # Runs as an asyncio task alongside the LLM/STT pipeline in the same event
    # loop.  uvicorn.Server.serve() is a native coroutine — no extra threads.
    # Signal handlers are disabled to avoid conflicts with the parent loop.
    watchdog = TaskWatchdog()

    aura_server = None
    aura_task: asyncio.Task | None = None
    if not args.no_aura:
        try:
            import uvicorn
            from aura.server import app as aura_app

            aura_cfg = uvicorn.Config(
                aura_app,
                host="127.0.0.1",
                port=8765,
                log_level="warning",
            )
            aura_server = uvicorn.Server(aura_cfg)
            # Prevent uvicorn from fighting over SIGTERM/SIGINT with our loop
            aura_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            aura_task = asyncio.create_task(aura_server.serve(), name="aura-server")
            logger.info("AURA: ws://127.0.0.1:8765/ws  |  open aura/index.html to monitor")

            # Zeek L7 DPI log streamer
            try:
                from tools.zeek_dpi import start_zeek_dpi
                watchdog.register("zeek-dpi", lambda: start_zeek_dpi(_aura_broadcast), RestartPolicy.BACKOFF)
                logger.info("ZEEK_DPI: L7 deep packet inspection streamer initializing…")
            except ImportError:
                logger.warning("ZEEK_DPI: tools.zeek_dpi unavailable — DPI monitoring disabled")

            # Honeypot matrix — passes executor/llm refs so canary can trigger agentic loop
            try:
                from core.canary import start_canaries
                watchdog.register(
                    "canary-matrix",
                    lambda: start_canaries(_aura_broadcast, executor, llm),
                    RestartPolicy.ALWAYS,
                )
                logger.info("CANARY: honeypot matrix initializing…")
            except ImportError:
                logger.warning("CANARY: core.canary unavailable — honeypot matrix disabled")

            # RF/RFID hardware abstraction layer
            try:
                from tools.rf_bridge import start_rf_bridge
                watchdog.register("rf-bridge", lambda: start_rf_bridge(_aura_broadcast), RestartPolicy.BACKOFF)
                logger.info("RF_BRIDGE: initializing hardware sources…")
            except ImportError:
                logger.warning("RF_BRIDGE: tools.rf_bridge unavailable — RF monitoring disabled")

            # ETW kernel telemetry monitor
            try:
                from tools.etw_monitor import start_etw_monitor
                watchdog.register("etw-monitor", lambda: start_etw_monitor(_aura_broadcast), RestartPolicy.BACKOFF)
                logger.info("ETW: kernel telemetry monitor task queued…")
            except ImportError:
                logger.warning("ETW: tools.etw_monitor unavailable — kernel telemetry disabled")

            # Environmental Intel — weather telemetry + NTP chrono-sync
            try:
                from tools.environmental_intel import start_environmental_polling
                watchdog.register(
                    "env-intel",
                    lambda: start_environmental_polling(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("ENV_INTEL: environmental polling + NTP chrono-sync initializing…")
            except ImportError:
                logger.warning("ENV_INTEL: tools.environmental_intel unavailable — env monitoring disabled")

            # Live OSINT threat feed aggregator — Abuse.ch + CISA
            try:
                from tools.threat_feed_sync import start_threat_feed_sync
                watchdog.register(
                    "threat-feed",
                    lambda: start_threat_feed_sync(_aura_broadcast),
                    RestartPolicy.BACKOFF,
                )
                logger.info("THREAT_FEED: live OSINT feed sync initializing…")
            except ImportError:
                logger.warning("THREAT_FEED: tools.threat_feed_sync unavailable — feed sync disabled")

            # Hardware resource watchdog sentinel — RAM/temp + VM auto-suspend
            try:
                from tools.resource_sentinel import start_resource_sentinel
                watchdog.register(
                    "resource-watchdog",
                    lambda: start_resource_sentinel(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("RESOURCE_SENTINEL: hardware watchdog initializing…")
            except ImportError:
                logger.warning("RESOURCE_SENTINEL: tools.resource_sentinel unavailable — watchdog disabled")

            # VM Sysmon telemetry bridge
            watchdog.register(
                "sysmon-bridge",
                lambda: start_sysmon_bridge(_aura_broadcast),
                RestartPolicy.BACKOFF,
            )
            logger.info("SYSMON_BRIDGE: VM telemetry bridge registered…")

            # Start the task watchdog monitor
            asyncio.create_task(watchdog.start(_aura_broadcast), name="task-watchdog")

            # Broadcast startup diagnostic so AURA HUD can show subsystem health
            asyncio.create_task(
                _aura_broadcast(make_event("startup_diagnostic", **diag)),
                name="startup-diag-broadcast",
            )
        except ImportError:
            logger.warning("AURA: fastapi/uvicorn not installed — UI disabled. pip install fastapi uvicorn[standard]")

    logger.info("JARVIS listo.")

    try:
        if not args.no_greeting:
            await _greeting(llm, tts, settings.assistant_name, settings.user_name)

        if args.voice and stt is not None:
            await _loop_voice(llm, tts, stt, settings.assistant_name)
        else:
            await _loop_text(llm, tts, settings.assistant_name)
    finally:
        # Graceful AURA shutdown
        if aura_server is not None:
            aura_server.should_exit = True
        if aura_task is not None:
            try:
                await asyncio.wait_for(aura_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                aura_task.cancel()


def main() -> None:
    try:
        from core.config import settings  # Validación temprana de .env
    except Exception as e:
        print(f"[ERROR] Configuración inválida: {e}", file=sys.stderr)
        print("Copia .env.example → .env y configura tu ANTHROPIC_API_KEY.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
