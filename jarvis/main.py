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

import os
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


async def _process_voice_input(
    user_input: str, llm, tts, name: str
) -> bool:
    """
    v35.0 — pre-process STT output. Returns True if handled (skip LLM).
    Order: interrupt commands → voice macros → LLM.
    """
    from tools.executor import _aura_broadcast

    # 1. Interrupt commands always win — fire in <200ms
    interrupt_type = is_interrupt_command(user_input)
    if interrupt_type:
        await handle_interrupt(interrupt_type, _aura_broadcast)
        if interrupt_type == "abort":
            # Silence current TTS immediately before speaking confirmation
            try: tts.interrupt()
            except Exception: pass
            await tts.speak_async("Understood. Stopping.")
        elif interrupt_type == "status":
            from core.cancel_bus import get_active_operations
            ops = get_active_operations()
            msg = (f"Active: {', '.join(ops.keys())}"
                   if ops else "Nothing active.")
            await tts.speak_async(msg)
        elif interrupt_type == "reset":
            await tts.speak_async("Reset complete.")
        return True

    # 2. Voice macros — YAML-defined shortcuts
    try:
        is_macro = await process_for_macro(user_input, _aura_broadcast, tts)
        if is_macro:
            return True
    except Exception as e:
        logger.debug(f"MACRO: process error: {e}")

    # 3. Normal LLM routing
    await _run_turn(llm, tts, user_input, name)
    return False


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

            # v35.0 — interrupt + macro routing before LLM
            await _process_voice_input(user_input, llm, tts, name)

        except KeyboardInterrupt:
            print("\nCerrando...")
            break


async def _main_async() -> None:
    parser = argparse.ArgumentParser(description="JARVIS — Asistente de IA Personal")
    parser.add_argument("--voice", action="store_true", help="Activa modo voz (STT + TTS)")
    parser.add_argument("--no-greeting", action="store_true", help="Omite el saludo inicial")
    parser.add_argument("--no-aura", action="store_true", help="Disable AURA WebSocket server")
    args = parser.parse_args()

    from core.hardware_profile import detect_hardware, set_cached_profile
    from core.dependency_guardian import ensure_all, resolve_models
    from core.shutdown_manager import (
        install_signal_handlers, get_shutdown_event,
        run_graceful_shutdown, register_shutdown_callback,
    )
    from core.relevance_graph import start_pruning_loop
    from core.power_monitor import start_power_monitor
    from core.process_governor import enforce_cpu_priorities
    from core.model_router import (
        check_model_availability, configure_ollama_for_hardware,
        MODEL_FAST, MODEL_DEEP,
    )
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
    from tools.ebpf_bridge import start_ebpf_bridge
    from tools.sliver_bridge import start_sliver_monitor
    from tools.active_tarpit import start_tarpit
    from tools.yara_file_monitor import start_yara_file_monitor
    from core.severity_calibrator import start_calibration_loop
    # v34.0 — Paranoid Fortress & Cognitive Self-Optimization
    from core.security_auditor    import start_security_auditor
    from core.windows_hardener    import apply_host_hardening
    from core.integrity_baseline  import run_integrity_check
    from core.cognitive_optimizer import start_cognitive_monitor
    # v35.0 — Real-Time Interrupt Architecture
    from core.cancel_bus     import initialize as init_cancel_bus
    from core.voice_interrupt import is_interrupt_command, handle_interrupt
    from core.voice_macros   import process_for_macro
    # v36.0 — Predictive Cognition & Autonomous Intelligence
    from core.agent_orchestrator  import orchestrator as v36_orchestrator
    from core.model_swapper       import attach as attach_model_swapper
    from core.memory_consolidator import start_consolidation_scheduler
    from core.correlator          import correlator as v36_correlator

    # FIRST: detect hardware before any model loading or task registration
    hw_profile = detect_hardware()
    set_cached_profile(hw_profile)

    # v30.0: dependency guardian — start Ollama, install missing packages,
    # check disk space, install jq. Concurrent — won't block boot.
    dep_results = await ensure_all(hw_profile)
    logger.debug(f"GUARDIAN: results → {dep_results}")

    # v30.0: resolve best available models from fallback chain
    resolved_fast, resolved_deep = await resolve_models(hw_profile)
    hw_profile.model_fast = resolved_fast
    hw_profile.model_deep = resolved_deep
    try:
        import core.model_router as _mr
        _mr.MODEL_FAST = resolved_fast
        _mr.MODEL_DEEP = resolved_deep
    except Exception:
        pass

    await configure_ollama_for_hardware(hw_profile)

    # v29.0: elevate ollama.exe to HIGH_PRIORITY_CLASS so LLM inference
    # wins CPU time on the 15W U-series TDP budget. Best-effort; never blocks.
    enforce_cpu_priorities()

    # v30.0: install signal handlers for graceful shutdown
    install_signal_handlers(asyncio.get_event_loop())

    # v35.0: initialize the global cancellation bus on this event loop
    init_cancel_bus(asyncio.get_event_loop())

    # v30.0: register ChromaDB flush callback for graceful shutdown
    async def _flush_chroma():
        try:
            from core.knowledge import get_vault
            vault = get_vault()
            chroma = getattr(vault, "_chroma", None)
            if chroma is not None and hasattr(chroma, "persist"):
                chroma.persist()
            logger.info("SHUTDOWN: ChromaDB flushed")
        except Exception:
            pass
    register_shutdown_callback(_flush_chroma)

    # Check model availability and warn if not pulled
    model_avail = await check_model_availability()
    for model, avail in model_avail.items():
        if not avail:
            logger.warning(f"MODEL: {model} not pulled — run: ollama pull {model}")

    # Async bridge queue: STT threads push (text, confidence) here via
    # loop.call_soon_threadsafe; the executor's _challenge() awaits from it.
    stt_queue: asyncio.Queue = asyncio.Queue()

    # Pre-load Whisper in a high-priority background thread.
    # The model is ready before the LLM starts, preventing CPU contention.
    audio_listener = HighPrioritySTTListener()
    # v32.0: wire VAD events into AURA HUD broadcast
    audio_listener._loop_ref      = asyncio.get_event_loop()
    audio_listener._broadcast_ref = _aura_broadcast

    executor = ToolExecutor(stt_queue=stt_queue, stt_listener=audio_listener)
    llm = LLM(tool_executor=executor)
    tts = TTS()

    # v35.0: wire tts reference into STT listener for fast interrupt path
    try:
        audio_listener._tts_ref = tts
    except Exception:
        pass

    # v36.0 — Predictive Cognition wiring (model hot-swap + orchestrator + narrator)
    try:
        attach_model_swapper(llm)
    except Exception as e:
        logger.debug(f"V36: model_swapper attach failed: {e}")
    try:
        v36_orchestrator.attach(
            broadcast_fn  = _aura_broadcast,
            ollama_client = llm.client,
            fast_model    = hw_profile.model_fast,
            deep_model    = hw_profile.model_deep,
        )
        logger.info("V36: agent_orchestrator attached")
    except Exception as e:
        logger.debug(f"V36: orchestrator attach failed: {e}")
    try:
        v36_correlator.attach_llm(
            tts           = tts,
            ollama_client = llm.client,
            fast_model    = hw_profile.model_fast,
            deep_model    = hw_profile.model_deep,
        )
        logger.info("V36: correlator LLM/TTS refs attached for autonomous narration")
    except Exception as e:
        logger.debug(f"V36: correlator.attach_llm failed: {e}")

    # v30.0: register session-save callback (closure now has llm reference)
    async def _flush_session():
        try:
            from core.session_manager import save_session
            save_session(llm.history)
        except Exception:
            pass
    register_shutdown_callback(_flush_session)

    # v27.0: store executor reference for HUD bidirectional command dispatch
    try:
        from aura.server import attach_executor
        attach_executor(executor)
    except ImportError:
        pass

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
                try:
                    watchdog.register("zeek-dpi", lambda: start_zeek_dpi(_aura_broadcast), RestartPolicy.BACKOFF)
                    logger.info("ZEEK_DPI: L7 deep packet inspection streamer initializing…")
                except Exception as e:
                    logger.warning(f"Could not register zeek-dpi: {e}")
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
            try:
                watchdog.register(
                    "sysmon-bridge",
                    lambda: start_sysmon_bridge(_aura_broadcast),
                    RestartPolicy.BACKOFF,
                )
                logger.info("SYSMON_BRIDGE: VM telemetry bridge registered…")
            except Exception as e:
                logger.warning(f"Could not register sysmon-bridge: {e}")

            # eBPF kernel telemetry from Kali VM via Falco
            try:
                watchdog.register(
                    "ebpf-bridge",
                    lambda: start_ebpf_bridge(_aura_broadcast),
                    RestartPolicy.BACKOFF,
                )
                logger.info("EBPF_BRIDGE: eBPF/Falco bridge registered…")
            except Exception as e:
                logger.warning(f"Could not register ebpf-bridge: {e}")

            # Sliver C2 session monitor
            try:
                watchdog.register(
                    "sliver-monitor",
                    lambda: start_sliver_monitor(_aura_broadcast),
                    RestartPolicy.BACKOFF,
                )
                logger.info("SLIVER_MONITOR: Sliver C2 session monitor registered…")
            except Exception as e:
                logger.warning(f"Could not register sliver-monitor: {e}")

            # v27.0 severity calibration background loop
            watchdog.register(
                "severity-calibrator",
                lambda: start_calibration_loop(_aura_broadcast),
                RestartPolicy.BACKOFF,
            )
            logger.info("SEVERITY_CALIBRATOR: adaptive severity scoring registered…")

            # v30.0 episodic memory pruning loop (PageRank-based relevance)
            try:
                watchdog.register(
                    "pruning-loop",
                    lambda: start_pruning_loop(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("RELEVANCE_GRAPH: episodic memory pruning loop registered…")
            except Exception as e:
                logger.warning(f"Could not register pruning-loop: {e}")

            # v30.0 battery-aware dynamic reconfiguration
            try:
                watchdog.register(
                    "power-monitor",
                    lambda: start_power_monitor(_aura_broadcast, hw_profile),
                    RestartPolicy.ALWAYS,
                )
                logger.info("POWER_MONITOR: battery-aware reconfiguration registered…")
            except Exception as e:
                logger.warning(f"Could not register power-monitor: {e}")

            # v31.0 active deception TCP tarpit (4444/5900/8080/9200/27017)
            try:
                watchdog.register(
                    "active-tarpit",
                    lambda: start_tarpit(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("TARPIT: cognitive deception matrix registered…")
            except Exception as e:
                logger.warning(f"Could not register active-tarpit: {e}")

            # v31.0 event-driven YARA file integrity monitor
            try:
                watchdog.register(
                    "yara-file-monitor",
                    lambda: start_yara_file_monitor(_aura_broadcast),
                    RestartPolicy.BACKOFF,
                )
                logger.info("YARA_MONITOR: event-driven file integrity monitor registered…")
            except Exception as e:
                logger.warning(f"Could not register yara-file-monitor: {e}")

            # v32.0 — IP geolocation service for AURA globe markers
            try:
                from tools.geo_resolver import watch_and_resolve
                watchdog.register(
                    "geo-resolver",
                    lambda: watch_and_resolve(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("GEO_RESOLVER: IP geolocation service registered…")
            except Exception as e:
                logger.warning(f"Could not register geo-resolver: {e}")

            # v33.0 — Adversarial Intelligence subsystems
            try:
                from core.adversary_emulator import adversary_emulator
                adversary_emulator.attach(_aura_broadcast)
                logger.info(
                    f"ADVERSARY_EMULATOR: chains={adversary_emulator.get_available_chains()} "
                    f"techniques={len(adversary_emulator.get_available_techniques())}"
                )
            except Exception as e:
                logger.warning(f"Could not attach adversary-emulator: {e}")

            try:
                from core.network_baseline import start_network_baseline
                watchdog.register(
                    "network-baseline",
                    lambda: start_network_baseline(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("NETWORK_BASELINE: statistical anomaly engine registered…")
            except Exception as e:
                logger.warning(f"Could not register network-baseline: {e}")

            # v34.0 — Paranoid Fortress
            # Run integrity check FIRST (before any code changes), then hardening
            asyncio.create_task(
                run_integrity_check(_aura_broadcast),
                name="integrity-check-boot",
            )
            asyncio.create_task(
                apply_host_hardening(_aura_broadcast),
                name="host-hardening-boot",
            )
            try:
                watchdog.register(
                    "security-auditor",
                    lambda: start_security_auditor(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("SECURITY_AUDITOR: host port auditor registered…")
            except Exception as e:
                logger.warning(f"Could not register security-auditor: {e}")

            try:
                watchdog.register(
                    "cognitive-monitor",
                    lambda: start_cognitive_monitor(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("COGNITIVE: self-optimization monitor registered…")
            except Exception as e:
                logger.warning(f"Could not register cognitive-monitor: {e}")

            # v36.0 — Memory consolidation scheduler (24h cycle, idle-gated)
            try:
                watchdog.register(
                    "memory-consolidator",
                    lambda: start_consolidation_scheduler(
                        _aura_broadcast, llm.client, hw_profile.model_deep
                    ),
                    RestartPolicy.ALWAYS,
                )
                logger.info("MEMORY_CONSOLIDATOR: 24h consolidation cycle registered…")
            except Exception as e:
                logger.warning(f"Could not register memory-consolidator: {e}")

            try:
                from core.attck_coverage import broadcast_coverage
                asyncio.create_task(
                    broadcast_coverage(_aura_broadcast),
                    name="attck-coverage-broadcast",
                )
                logger.info("ATTCK_COVERAGE: matrix scheduled for HUD broadcast…")
            except Exception as e:
                logger.warning(f"Could not broadcast ATT&CK coverage: {e}")

            # v28.0 SOAR playbook engine — deterministic incident response
            try:
                from core.playbook_engine import playbook_engine
                playbook_engine.load_playbooks()
                playbook_engine.start_hot_reload()
                playbook_engine.attach(
                    broadcast_fn  = _aura_broadcast,
                    tool_executor = executor,
                    config        = {
                        "SECONDARY_VMS":         [v for v in os.getenv("SECONDARY_VMS", "").split(",") if v],
                        "VOLATILITY_TARGET_VMX": os.getenv("VOLATILITY_TARGET_VMX", ""),
                    }
                )
                logger.info(
                    f"PLAYBOOK: engine ready — {len(playbook_engine._playbooks)} playbooks loaded"
                )
            except ImportError as e:
                logger.warning(f"PLAYBOOK: engine unavailable — {e}")

            # v28.0 RF out-of-band command channel
            try:
                from tools.rf_oob import start_rf_oob
                try:
                    watchdog.register(
                        "rf-oob",
                        lambda: start_rf_oob(_aura_broadcast),
                        RestartPolicy.BACKOFF,
                    )
                    logger.info("RF_OOB: out-of-band command channel registered")
                except Exception as e:
                    logger.warning(f"Could not register rf-oob: {e}")
            except ImportError:
                logger.warning("RF_OOB: tools.rf_oob unavailable — OOB channel disabled")

            # Start the task watchdog monitor
            asyncio.create_task(watchdog.start(_aura_broadcast), name="task-watchdog")

            # Broadcast hardware profile and startup diagnostic to AURA HUD
            asyncio.create_task(
                _aura_broadcast({
                    "type":         "hardware_profile",
                    "ram_gb":       hw_profile.total_ram_gb,
                    "dual_channel": hw_profile.is_dual_channel,
                    "pools":        hw_profile.recommended_pools,
                    "ctx":          hw_profile.recommended_ctx,
                }),
                name="hardware-profile-broadcast",
            )
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
        # v30.0: graceful shutdown sequence — flush ChromaDB, save session,
        # cancel tasks, write audit log.
        try:
            await run_graceful_shutdown(watchdog=watchdog)
        except Exception as e:
            logger.debug(f"SHUTDOWN: error during graceful shutdown: {e}")

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
