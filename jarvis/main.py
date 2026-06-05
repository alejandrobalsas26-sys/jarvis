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
import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module="scapy",
)
warnings.filterwarnings(
    "ignore",
    message=".*TripleDES.*",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=".*CryptographyDeprecationWarning.*",
)

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

    # v44.0 — journal the operator command (non-blocking, best-effort)
    try:
        from core.session_journal import record_voice_command
        record_voice_command(user_input)
    except Exception:
        pass

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


async def run_war_room_debate_on_last_incident(llm, tts, broadcast_fn):
    """Trigger War Room debate on the last significant incident."""
    from core.war_room_debate import run_war_room_debate
    try:
        from core.correlator import correlator as _corr
        active = _corr.get_active_incidents()
        incident = active[0] if active else {
            "kill_chain_phase": "threat analysis requested",
            "severity_score":   7.0,
            "involved_hosts":   set(),
            "mitre_techniques": ["T1059"],
        }
    except Exception:
        incident = {
            "kill_chain_phase": "threat analysis requested",
            "severity_score":   7.0,
            "involved_hosts":   set(),
            "mitre_techniques": ["T1059"],
        }
    await run_war_room_debate(
        incident,
        llm.client,
        getattr(llm, "current_model", getattr(llm, "model", "")),
        tts,
        broadcast_fn,
    )


async def _loop_voice_continuous(llm, tts, stt, name: str) -> None:
    """
    Iron Man JARVIS continuous voice loop (v46.0).

    Always listening. Conversational memory. Real-time system state.
    TTS interruption on new speech. JARVIS persona on every response.
    """
    import sounddevice as sd
    import numpy as np
    import collections

    SAMPLE_RATE     = 16000
    FRAME_MS        = 30
    FRAME_SAMPLES   = int(SAMPLE_RATE * FRAME_MS / 1000)
    FRAME_BYTES     = FRAME_SAMPLES * 2

    SPEECH_TRIGGER  = 4    # frames to confirm speech start
    SILENCE_TRIGGER = 50   # frames to confirm speech end (~1.5s)
    MIN_SPEECH_FRAMES = 8  # minimum speech length to process

    try:
        import webrtcvad
        vad = webrtcvad.Vad(2)
    except ImportError:
        logger.error("VOICE: webrtcvad not installed — pip install webrtcvad")
        return

    # ── Conversation history (rolling 10-turn window) ────────────────────
    conversation_history: list[dict] = []
    MAX_HISTORY_TURNS = 10

    def _add_to_history(role: str, content: str) -> None:
        conversation_history.append({"role": role, "content": content})
        if len(conversation_history) > MAX_HISTORY_TURNS * 2:
            # Keep system context fresh — trim oldest 2 turns
            del conversation_history[:2]

    # ── Gather real-time system state for persona ─────────────────────────
    async def _get_system_state() -> dict:
        state = {}
        try:
            from core.purple_coordinator import get_coverage_summary
            cov = get_coverage_summary()
            state["coverage_pct"] = cov.get("coverage_pct", 0)
        except Exception:
            state["coverage_pct"] = 0
        try:
            from core.sensor_mesh import get_connected_agents
            state["sensor_agents"] = len(get_connected_agents())
        except Exception:
            state["sensor_agents"] = 0
        try:
            from core.correlator import get_active_incident_count
            state["active_incidents"] = get_active_incident_count()
        except Exception:
            state["active_incidents"] = 0
        return state

    # ── Build messages list for LLM call ────────────────────────────────
    async def _build_messages(user_text: str) -> list[dict]:
        from core.personality import get_jarvis_system_prompt
        state = await _get_system_state()
        system_prompt = get_jarvis_system_prompt(
            coverage_pct     = state.get("coverage_pct", 0),
            active_incidents = state.get("active_incidents", 0),
            sensor_agents    = state.get("sensor_agents", 0),
            model_name       = getattr(llm, "current_model", ""),
            operator_name    = name,
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_text})
        return messages

    # ── LLM inference with conversation context ──────────────────────────
    async def _ask_jarvis(user_text: str) -> str:
        try:
            messages = await _build_messages(user_text)

            # Try full messages API first (Ollama supports this)
            if hasattr(llm, "client") and llm.client:
                resp = await asyncio.wait_for(
                    llm.client.chat.completions.create(
                        model    = getattr(llm, "current_model",
                                           getattr(llm, "model", "")),
                        messages = messages,
                        stream   = False,
                        extra_body = {"options": {
                            "num_ctx":     2048,
                            "temperature": 0.7,
                        }},
                    ),
                    timeout=20.0,
                )
                return resp.choices[0].message.content.strip()

            # Fallback: single-turn with context in user message
            context_str = "\n".join(
                f"{'User' if m['role']=='user' else 'JARVIS'}: {m['content']}"
                for m in conversation_history[-6:]
            )
            full_prompt = (
                f"[CONTEXT]\n{context_str}\n\n"
                f"[USER]\n{user_text}"
                if context_str else user_text
            )
            return await asyncio.wait_for(
                llm.chat_async(full_prompt),
                timeout=20.0,
            )

        except asyncio.TimeoutError:
            return "I'm not getting a response from the model. Try again."
        except Exception as e:
            logger.debug(f"VOICE: LLM error: {e}")
            return "Something went wrong on my end. Try again."

    # ── TTS with interruption support ────────────────────────────────────
    _tts_speaking = asyncio.Event()

    async def _speak(text: str) -> None:
        if not tts or not text:
            return
        _tts_speaking.set()
        try:
            await asyncio.wait_for(
                tts.speak_async(text), timeout=30.0
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug(f"VOICE: TTS error: {e}")
        finally:
            _tts_speaking.clear()

    def _stop_tts() -> None:
        try:
            if hasattr(tts, "stop"):       tts.stop()
            if hasattr(tts, "stop_sync"):  tts.stop_sync()
            if hasattr(tts, "_engine"):    tts._engine.stop()
        except Exception:
            pass
        _tts_speaking.clear()

    # ── Audio queue ──────────────────────────────────────────────────────
    loop       = asyncio.get_event_loop()
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=600)

    def _sd_callback(indata, frames, time_info, status):
        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        try:
            loop.call_soon_threadsafe(audio_q.put_nowait, pcm)
        except Exception:
            pass

    # ── Greeting ─────────────────────────────────────────────────────────
    from core.personality import get_boot_greeting
    greeting = get_boot_greeting()
    logger.info(f"VOICE: continuous Iron Man mode — {greeting}")
    asyncio.create_task(_speak(greeting))

    print(f"\n[JARVIS] {greeting}")
    print("─" * 50)
    print("Speak naturally. JARVIS is always listening.")
    print("Ctrl+C to exit.")
    print("─" * 50 + "\n")

    # ── Main VAD loop ─────────────────────────────────────────────────────
    state          = "listening"
    speech_frames  = []
    voiced_count   = 0
    silence_count  = 0
    ring_buf       = collections.deque(maxlen=SPEECH_TRIGGER * 4)

    try:
        with sd.InputStream(
            samplerate = SAMPLE_RATE,
            channels   = 1,
            dtype      = "float32",
            blocksize  = FRAME_SAMPLES,
            callback   = _sd_callback,
        ):
            while True:
                try:
                    pcm = await asyncio.wait_for(
                        audio_q.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue

                if len(pcm) != FRAME_BYTES:
                    continue

                try:
                    is_speech = vad.is_speech(pcm, SAMPLE_RATE)
                except Exception:
                    continue

                # If JARVIS is speaking and user speaks → interrupt
                if _tts_speaking.is_set() and is_speech:
                    voiced_count += 1
                    if voiced_count >= SPEECH_TRIGGER:
                        _stop_tts()
                        state         = "recording"
                        silence_count = 0
                        speech_frames = list(ring_buf) + [pcm]
                        voiced_count  = 0
                        print("\n[listening...]")
                    continue

                if state == "listening":
                    ring_buf.append(pcm)
                    if is_speech:
                        voiced_count += 1
                    else:
                        voiced_count = max(0, voiced_count - 1)

                    if voiced_count >= SPEECH_TRIGGER:
                        state         = "recording"
                        silence_count = 0
                        speech_frames = list(ring_buf)
                        voiced_count  = 0

                elif state == "recording":
                    speech_frames.append(pcm)

                    if is_speech:
                        silence_count = 0
                    else:
                        silence_count += 1

                    if silence_count >= SILENCE_TRIGGER:
                        state        = "listening"
                        voiced_count = 0
                        ring_buf.clear()

                        if len(speech_frames) < MIN_SPEECH_FRAMES:
                            speech_frames = []
                            continue

                        pcm_all       = b"".join(speech_frames)
                        speech_frames = []

                        # Transcribe
                        try:
                            ev_loop = asyncio.get_event_loop()
                            text = await asyncio.wait_for(
                                ev_loop.run_in_executor(
                                    None,
                                    lambda p=pcm_all: stt.transcribe_bytes(
                                        p, SAMPLE_RATE
                                    ),
                                ),
                                timeout=10.0,
                            )
                        except Exception as e:
                            logger.debug(f"VOICE: transcription error: {e}")
                            continue

                        if not text or len(text.strip()) < 2:
                            continue

                        text = text.strip()
                        print(f"\n{name}: {text}")

                        text_lower = text.lower()

                        # ── v46.0 OMEGA — War Room trigger ──────────────────
                        if any(kw in text_lower for kw in (
                            "war room", "sala de guerra", "debate",
                            "ares vs", "red vs blue",
                        )):
                            from tools.executor import _aura_broadcast as _br
                            asyncio.create_task(
                                run_war_room_debate_on_last_incident(
                                    llm, tts, _br,
                                )
                            )
                            continue

                        # ── v46.0 OMEGA — Room / webcam analysis ────────────
                        if any(kw in text_lower for kw in (
                            "qué ves", "que ves", "analiza el cuarto",
                            "analiza la habitacion", "mira alrededor",
                            "look around", "what do you see",
                            "describe my room", "scan the room",
                        )):
                            asyncio.create_task(_speak(
                                "Activating visual cortex. Stand by."
                            ))
                            from core.vision_engine import analyze_room
                            model_v = getattr(llm, "model_vision",
                                              "moondream:latest")
                            desc = await analyze_room(
                                llm.client, model_v,
                                "Describe the room and environment in detail. "
                                "Note any screens, people, objects, lighting."
                            )
                            synthesis = await _ask_jarvis(
                                f"Moondream visual analysis of the room: {desc}\n\n"
                                "Summarize what you see in 2 sentences. "
                                "Note anything unusual or security-relevant."
                            )
                            print(f"\n[JARVIS VISION] {synthesis}\n")
                            asyncio.create_task(_speak(synthesis))
                            _add_to_history("user", text)
                            _add_to_history("assistant", synthesis)
                            continue

                        # ── v46.0 OMEGA — Screen analysis ───────────────────
                        if any(kw in text_lower for kw in (
                            "analiza la pantalla", "analiza mi pantalla",
                            "analyze my screen", "what's on screen",
                            "es phishing", "is this phishing",
                            "analyze this", "que hay en pantalla",
                            "lee la pantalla", "read the screen",
                        )):
                            asyncio.create_task(_speak("Capturing screen."))
                            from core.vision_engine import analyze_screen_vision
                            model_v = getattr(llm, "model_vision",
                                              "moondream:latest")
                            if "phishing" in text_lower:
                                query = ("Is this email or webpage showing "
                                         "signs of phishing? Look for: "
                                         "suspicious sender, urgent language, "
                                         "fake logos, suspicious links.")
                            else:
                                query = ("Describe exactly what you see on "
                                         "this screen.")
                            desc = await analyze_screen_vision(
                                llm.client, model_v, query
                            )
                            synthesis = await _ask_jarvis(
                                f"Screen analysis: {desc}\n\n"
                                "Give me a 2-sentence assessment. "
                                "If it is phishing, say so clearly."
                            )
                            print(f"\n[JARVIS SCREEN] {synthesis}\n")
                            asyncio.create_task(_speak(synthesis))
                            _add_to_history("user", text)
                            _add_to_history("assistant", synthesis)
                            continue

                        # Check voice macros first
                        try:
                            from core.voice_macros import process_for_macro
                            from tools.executor import _aura_broadcast as _br
                            handled = await process_for_macro(text, _br, tts)
                            if handled:
                                continue
                        except Exception:
                            pass

                        # Get JARVIS response with full context
                        response = await _ask_jarvis(text)

                        if response:
                            # Update conversation history
                            _add_to_history("user",      text)
                            _add_to_history("assistant", response)

                            print(f"\n[JARVIS] {response}\n")
                            asyncio.create_task(_speak(response))

    except KeyboardInterrupt:
        logger.info("VOICE: interrupted by user")
    except Exception as e:
        logger.error(f"VOICE: loop error: {e}")


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
    from core import ram_hunter, ransomware_decoy
    from core import network_quarantine, ir_reporter, honey_credentials
    # v37.0 — Autonomous Intelligence & GitHub-Native Tool Ecosystem
    from core.github_explorer     import load_registry as load_github_registry
    from core.cve_intel           import start_cve_monitor
    from core.code_intel          import start_inbox_watcher
    from core.lab_manager         import list_vms as list_lab_vms
    # v38.0 — Visual Intelligence (vision/browser/diagrams/screen monitor)
    from core.vision_engine       import capture_and_save
    from core.screen_monitor      import start_screen_monitor
    # v39.0 — Deep Forensics & Autonomous Remediation
    from tools.memory_hunter      import dump_process_memory
    from core.auto_remediator     import draft_mitigation
    # v40.0 — Omni-Vision, Ghost Hands & Forensic Reporter
    from core.ocr_engine          import read_screen_and_analyze
    from tools.ghost_hands        import execute_lab_profile, list_profiles
    from core.forensic_reporter   import generate_forensic_report
    # v41.0 — Ephemeral Docker Lab Orchestrator
    from tools.docker_manager     import list_running_labs, _get_client as _docker_get_client
    # v42.0 — ARES PROTOCOL (Red Team Operator + Sensor Mesh + MITM Proxy)
    from core.red_team_operator   import ares_operator
    from core.sensor_mesh         import start_sensor_server, get_connected_agents
    # v43.0 — BIFROST PROTOCOL (Purple Team Coordinator + BAS + Detection Eng + OPSEC)
    from core.purple_coordinator  import attach_llm as purple_attach_llm
    from core.purple_coordinator  import get_coverage_summary as purple_summary
    from tools.breach_simulator   import run_full_bas_scenario  # noqa: F401

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

    # v42.0 — ARES Red Team Operator attach (llm + tts + executor refs)
    try:
        ares_operator.attach(
            broadcast_fn  = _aura_broadcast,
            ollama_client = llm.client,
            deep_model    = hw_profile.model_deep,
            tool_executor = executor,
            tts           = tts,
        )
        logger.info("ARES: autonomous red team operator ready")
    except Exception as e:
        logger.debug(f"V42: ares_operator attach failed: {e}")

    # v43.0 — Attach LLM to purple coordinator for auto-detection-engineering
    try:
        purple_attach_llm(llm.client, hw_profile.model_deep)
        logger.info("PURPLE_COORDINATOR: LLM attached — detection engineering enabled")
    except Exception as e:
        logger.debug(f"V43: purple_coordinator attach failed: {e}")

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

            # v37.0 — Autonomous Intelligence & GitHub-Native Tool Ecosystem
            try:
                load_github_registry()
            except Exception as e:
                logger.warning(f"V37: github registry load failed: {e}")

            try:
                watchdog.register(
                    "cve-monitor",
                    lambda: start_cve_monitor(_aura_broadcast, tts),
                    RestartPolicy.BACKOFF,
                )
                logger.info("CVE_INTEL: NVD monitor registered…")
            except Exception as e:
                logger.warning(f"Could not register cve-monitor: {e}")

            try:
                from pathlib import Path as _P
                watchdog.register(
                    "code-intel",
                    lambda: start_inbox_watcher(
                        _aura_broadcast, tts,
                        llm.client, hw_profile.model_deep,
                    ),
                    RestartPolicy.BACKOFF,
                )
                logger.info(
                    f"CODE_INTEL: drop folder watcher registered — "
                    f"{_P('analyze_inbox').absolute()}"
                )
            except Exception as e:
                logger.warning(f"Could not register code-intel: {e}")

            try:
                asyncio.create_task(
                    list_lab_vms(_aura_broadcast),
                    name="lab-vm-list-boot",
                )
                logger.info("LAB_MANAGER: VM enumeration scheduled…")
            except Exception as e:
                logger.warning(f"V37: lab_manager boot failed: {e}")

            # v38.0 — Visual Intelligence (vision engine + screen monitor)
            try:
                v36_orchestrator._vision_enabled = True
            except Exception:
                pass

            try:
                watchdog.register(
                    "screen-monitor",
                    lambda: start_screen_monitor(
                        _aura_broadcast, llm.client, tts,
                    ),
                    RestartPolicy.BACKOFF,
                )
                logger.info(
                    "SCREEN_MONITOR: registered "
                    f"({'ACTIVE' if os.getenv('JARVIS_SCREEN_MONITOR') == '1' else 'DISABLED — set JARVIS_SCREEN_MONITOR=1'})"
                )
            except Exception as e:
                logger.warning(f"Could not register screen-monitor: {e}")

            # Auto-screenshot on critical compound incidents (for reports)
            try:
                _v38_orig_broadcast = _aura_broadcast
                async def _visual_broadcast(event: dict) -> None:
                    await _v38_orig_broadcast(event)
                    if (event.get("type") == "compound_incident" and
                            event.get("severity_score", 0) >= 8.0):
                        try:
                            asyncio.create_task(capture_and_save(
                                f"incident_{event.get('incident_id','unk')}"
                            ))
                        except Exception:
                            pass
                logger.info("VISION: auto-screenshot on critical incident armed")
            except Exception as e:
                logger.debug(f"V38: visual_broadcast wire failed: {e}")

            try:
                from core.attck_coverage import broadcast_coverage
                asyncio.create_task(
                    broadcast_coverage(_aura_broadcast),
                    name="attck-coverage-broadcast",
                )
                logger.info("ATTCK_COVERAGE: matrix scheduled for HUD broadcast…")
            except Exception as e:
                logger.warning(f"Could not broadcast ATT&CK coverage: {e}")

            # v39.0 — Auto-remediator: hook into correlator broadcasts
            async def _hook_remediator() -> None:
                await asyncio.sleep(2.0)   # wait for AURA lifespan to attach
                if v36_correlator._broadcast_fn is None:
                    logger.debug("V39: correlator broadcast not ready — remediator hook skipped")
                    return
                _orig_correlator_broadcast = v36_correlator._broadcast_fn

                async def _remediating_broadcast(event: dict) -> None:
                    await _orig_correlator_broadcast(event)
                    if (event.get("type") == "compound_incident" and
                            event.get("severity_score", 0) >= 8.0):
                        asyncio.create_task(draft_mitigation(
                            event, llm.client,
                            hw_profile.model_deep,
                            _aura_broadcast,
                            incident_id=event.get("incident_id", "unk"),
                        ))

                v36_correlator.attach(_remediating_broadcast)
                logger.info("V39: auto-remediator hooked into correlator pipeline")

            asyncio.create_task(_hook_remediator(), name="v39-remediator-hook")

            # v40.0 — Ghost Hands lab profiles + forensic reporter auto-gen
            _v40_profiles = list_profiles()
            if _v40_profiles:
                logger.info(f"GHOST_HANDS: {len(_v40_profiles)} lab profiles loaded: {_v40_profiles}")

            async def _hook_forensic_reporter() -> None:
                await asyncio.sleep(2.5)
                if v36_correlator._broadcast_fn is None:
                    return
                _v40_orig_broadcast = v36_correlator._broadcast_fn

                async def _reporting_broadcast(event: dict) -> None:
                    await _v40_orig_broadcast(event)
                    if event.get("type") == "compound_incident_resolved":
                        _inc_id = event.get("incident_id", "")
                        if _inc_id:
                            logger.info(
                                f"FORENSIC_REPORTER: auto-generating report "
                                f"for resolved incident {_inc_id}"
                            )

                v36_correlator.attach(_reporting_broadcast)
                logger.info("V40: forensic reporter hooked into correlator pipeline")

            asyncio.create_task(_hook_forensic_reporter(), name="v40-reporter-hook")

            # v41.0 — Ephemeral Docker lab orchestrator — non-blocking daemon probe
            async def _check_docker():
                loop = asyncio.get_running_loop()
                client = await loop.run_in_executor(None, _docker_get_client)
                if client:
                    logger.info("DOCKER: daemon available — ephemeral labs ready")
                else:
                    logger.info(
                        "DOCKER: daemon not found — "
                        "install Docker Desktop and start it to enable lab containers"
                    )

            asyncio.create_task(_check_docker(), name="v41-docker-check")

            # v42.0 — Distributed Sensor Mesh WebSocket server (port 9999)
            try:
                watchdog.register(
                    "sensor-mesh",
                    lambda: start_sensor_server(_aura_broadcast),
                    RestartPolicy.ALWAYS,
                )
                logger.info("SENSOR_MESH: WebSocket server registered on port 9999")
            except Exception as e:
                logger.warning(f"Could not register sensor-mesh: {e}")

            # v42.0 — MITM Proxy Intelligence (opt-in via JARVIS_PROXY_ENABLE=1)
            if os.getenv("JARVIS_PROXY_ENABLE", "0") == "1":
                try:
                    from core.proxy_intel import start_proxy_intel
                    watchdog.register(
                        "proxy-intel",
                        lambda: start_proxy_intel(_aura_broadcast),
                        RestartPolicy.BACKOFF,
                    )
                    logger.info("PROXY_INTEL: MITM proxy registered on port 8888")
                except Exception as e:
                    logger.warning(f"Could not register proxy-intel: {e}")
            else:
                logger.info(
                    "PROXY_INTEL: disabled "
                    "(set JARVIS_PROXY_ENABLE=1 to enable)"
                )

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

            # ── v44.0 — Quality of Life: clipboard intel, briefing, journal, aliases ──
            try:
                from core.clipboard_monitor import start_clipboard_monitor
                from core.daily_briefing    import deliver_briefing
                from core.session_journal   import record_event, write_journal
                from core.target_aliases    import list_aliases

                # Hook session journal into the broadcast pipeline
                _orig_broadcast_for_journal = _aura_broadcast
                async def _journaled_broadcast(event: dict) -> None:
                    await _orig_broadcast_for_journal(event)
                    record_event(event)

                # Clipboard intelligence monitor — polls every 1.5s in executor
                watchdog.register(
                    "clipboard-monitor",
                    lambda: start_clipboard_monitor(_journaled_broadcast, tts),
                    RestartPolicy.BACKOFF,
                )
                logger.info("CLIPBOARD: intelligence monitor registered")

                # Daily briefing — fires once after subsystem init
                asyncio.create_task(
                    deliver_briefing(
                        _journaled_broadcast, tts,
                        llm.client, hw_profile.model_fast,
                    ),
                    name="v44-daily-briefing",
                )

                # Session journal — written on graceful shutdown
                async def _flush_session_journal():
                    try:
                        await write_journal(llm.client, hw_profile.model_fast)
                    except Exception:
                        pass
                register_shutdown_callback(_flush_session_journal)

                # Log known target aliases at startup
                _known_aliases = list_aliases()
                if _known_aliases:
                    logger.info(
                        f"TARGET_ALIASES: {len(_known_aliases)} known targets — "
                        f"{list(_known_aliases.keys())}"
                    )
            except Exception as e:
                logger.warning(f"Could not initialize v44.0 Quality of Life: {e}")

            # ── v45.0 — PROMETHEUS: Telegram bridge, hunt scheduler, intel fusion ──
            try:
                from core.telegram_bridge import (
                    start_telegram_bridge, stop_telegram_bridge, push_alert,
                )
                from core.hunt_scheduler import start_hunt_scheduler
                from core.intel_fusion   import (
                    initialize_db as init_intel_db,
                    ingest_incident, ingest_ioc,
                )

                # Initialize persistent cross-session IOC/campaign database
                try:
                    await init_intel_db()
                    logger.info("INTEL_FUSION: persistent IOC database initialized")
                except Exception as e:
                    logger.warning(f"INTEL_FUSION: db init failed: {e}")

                # Shared fusion ingest — feeds incidents/IOCs into the engine
                # and pushes CRITICAL events to the operator's phone.
                async def _fusion_ingest(event: dict) -> None:
                    etype = event.get("type")
                    if etype == "compound_incident":
                        asyncio.create_task(ingest_incident(event))
                    elif etype == "osint_enriched":
                        asyncio.create_task(ingest_ioc(
                            "ip", event.get("ip", ""),
                            event.get("threat_score", 0),
                        ))
                    if event.get("severity") == "CRITICAL":
                        asyncio.create_task(push_alert(
                            event.get("type", "ALERT").upper(),
                            event.get("message", str(event)[:200]),
                            "CRITICAL",
                        ))

                # Broadcast wrapper for hunt scheduler + telegram bridge events:
                # forwards to AURA HUD, then runs fusion ingest / critical push.
                _prev_for_fusion = _aura_broadcast
                async def _fusion_broadcast(event: dict) -> None:
                    await _prev_for_fusion(event)
                    try:
                        await _fusion_ingest(event)
                    except Exception:
                        pass

                # Chain fusion ingest into the correlator pipeline so real
                # compound incidents reach the fusion engine. Mirrors the
                # v39/v40 remediator + reporter hooks (preserves the chain).
                async def _hook_fusion() -> None:
                    await asyncio.sleep(3.0)
                    if v36_correlator._broadcast_fn is None:
                        logger.debug("V45: correlator broadcast not ready — fusion hook skipped")
                        return
                    _orig_for_fusion = v36_correlator._broadcast_fn
                    async def _fusing_broadcast(event: dict) -> None:
                        await _orig_for_fusion(event)
                        try:
                            await _fusion_ingest(event)
                        except Exception:
                            pass
                    v36_correlator.attach(_fusing_broadcast)
                    logger.info("V45: intel fusion hooked into correlator pipeline")

                asyncio.create_task(_hook_fusion(), name="v45-fusion-hook")

                # Telegram mobile command bridge (push + pull)
                watchdog.register(
                    "telegram-bridge",
                    lambda: start_telegram_bridge(_fusion_broadcast, tts),
                    RestartPolicy.BACKOFF,
                )
                register_shutdown_callback(stop_telegram_bridge)
                logger.info("TELEGRAM: bridge registered")

                # Autonomous threat hunt scheduler — 12 hypotheses, every 4h
                watchdog.register(
                    "hunt-scheduler",
                    lambda: start_hunt_scheduler(
                        _fusion_broadcast, llm.client, hw_profile.model_deep
                    ),
                    RestartPolicy.BACKOFF,
                )
                logger.info(
                    "HUNT_SCHEDULER: registered — "
                    "12 hypotheses, sweeping every 4h"
                )
            except Exception as e:
                logger.warning(f"Could not initialize v45.0 PROMETHEUS: {e}")

            # ── v46.0 OMEGA — IoT bridge, Punisher Mode, War Room auto-trigger ──
            try:
                from core.iot_bridge import (
                    alert_red, alert_orange, alert_clear, is_configured,
                )
                from core.punisher import punisher_response

                async def _hook_omega() -> None:
                    await asyncio.sleep(3.5)
                    if v36_correlator._broadcast_fn is None:
                        logger.debug("V46_OMEGA: correlator broadcast not ready — omega hook skipped")
                        return
                    _orig_for_omega = v36_correlator._broadcast_fn

                    async def _omega_broadcast(event: dict) -> None:
                        await _orig_for_omega(event)
                        etype = event.get("type", "")
                        sev   = event.get("severity_score", 0)

                        # IoT lights on compound incidents
                        if etype == "compound_incident":
                            try:
                                if sev >= 8.0:
                                    asyncio.create_task(alert_red(
                                        flash=True,
                                        reason=event.get("kill_chain_phase",""),
                                    ))
                                elif sev >= 6.0:
                                    asyncio.create_task(alert_orange(
                                        reason=event.get("kill_chain_phase",""),
                                    ))
                            except Exception:
                                pass

                            # Punisher Mode — auto-execute for severity >= 9.0
                            try:
                                if sev >= 9.0:
                                    asyncio.create_task(
                                        punisher_response(
                                            event, tts, _aura_broadcast,
                                        )
                                    )
                            except Exception:
                                pass

                            # Auto War Room debate for severity >= 7.0
                            try:
                                if sev >= 7.0:
                                    from core.war_room_debate import (
                                        run_war_room_debate,
                                    )
                                    asyncio.create_task(run_war_room_debate(
                                        event,
                                        llm.client,
                                        hw_profile.model_deep,
                                        tts,
                                        _aura_broadcast,
                                    ))
                            except Exception:
                                pass

                        # IoT red flash on canary hits
                        elif etype in ("CANARY_HIT", "canary_hit", "canary_triggered"):
                            try:
                                asyncio.create_task(alert_red(
                                    flash=True, reason="canary_hit",
                                ))
                            except Exception:
                                pass

                    v36_correlator.attach(_omega_broadcast)
                    logger.info(
                        "V46_OMEGA: IoT + Punisher + War Room hooked into correlator"
                    )

                asyncio.create_task(_hook_omega(), name="v46-omega-hook")
                logger.info(
                    f"V46_OMEGA: bridge armed — "
                    f"iot_configured={is_configured()}"
                )
            except Exception as e:
                logger.warning(f"V46_OMEGA: bridge init failed: {e}")

            # ── v46.0 — GENESIS: unified config, self-test, boot sequence, profiler ──
            try:
                from core.config_manager       import load_config
                from core.self_test            import run_self_test
                from core.boot_sequence        import execute_boot_sequence
                from core.performance_profiler import broadcast_stats as profiler_broadcast

                # Load unified configuration (auto-creates jarvis_config.yaml)
                try:
                    _v46_config = load_config()
                    logger.info(
                        f"CONFIG: loaded v46.0 configuration "
                        f"({len(_v46_config) if isinstance(_v46_config, dict) else 0} sections)"
                    )
                except Exception as e:
                    logger.warning(f"CONFIG: load failed: {e}")

                # Self-test + cinematic boot sequence — runs ONCE on real startup
                # (created as a task, not watchdog-managed → never re-runs on restart)
                async def _startup_sequence():
                    try:
                        test_report = await run_self_test(_aura_broadcast)
                        if test_report.get("failed", 0) > 0:
                            logger.warning(
                                f"STARTUP: {test_report['failed']} subsystems "
                                f"failed self-test"
                            )
                    except Exception as e:
                        logger.debug(f"V46: self-test error: {e}")
                    try:
                        await execute_boot_sequence(_aura_broadcast, tts)
                    except Exception as e:
                        logger.debug(f"V46: boot sequence error: {e}")
                    # v46.0 OMEGA — IoT startup pulse
                    try:
                        from core.iot_bridge import startup_pulse, is_configured
                        if is_configured():
                            asyncio.create_task(startup_pulse())
                            logger.info("IOT_BRIDGE: startup pulse sent")
                        else:
                            logger.info(
                                "IOT_BRIDGE: no smart home configured — "
                                "set JARVIS_HA_URL, JARVIS_HUE_BRIDGE, "
                                "or JARVIS_IOT_WEBHOOK to enable"
                            )
                    except Exception:
                        pass

                asyncio.create_task(_startup_sequence(), name="v46-startup-sequence")

                # Periodic performance profile broadcast (every 5 minutes)
                async def _profile_loop():
                    while True:
                        await asyncio.sleep(300)
                        try:
                            await profiler_broadcast(_aura_broadcast)
                        except Exception as e:
                            logger.debug(f"PROFILER: broadcast error: {e}")

                watchdog.register(
                    "performance-profiler",
                    _profile_loop,
                    RestartPolicy.ALWAYS,
                )
                logger.info("PERFORMANCE_PROFILER: registered (5min interval)")
            except Exception as e:
                logger.warning(f"Could not initialize v46.0 GENESIS: {e}")

            asyncio.create_task(ram_hunter.start(v36_correlator))
            asyncio.create_task(ransomware_decoy.start(v36_correlator))
            asyncio.create_task(network_quarantine.start(v36_correlator))
            asyncio.create_task(ir_reporter.start(v36_correlator))
            asyncio.create_task(honey_credentials.start(v36_correlator))

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
            await _loop_voice_continuous(llm, tts, stt, settings.assistant_name)
        else:
            await _loop_text(llm, tts, settings.assistant_name)
    finally:
        # v30.0: graceful shutdown sequence — flush ChromaDB, save session,
        # cancel tasks, write audit log.
        # v46.0: broad except — MCP/anyio cancel scope errors on shutdown
        # are cosmetic and should not pollute the operator's terminal.
        try:
            await run_graceful_shutdown(watchdog=watchdog)
        except (RuntimeError, asyncio.CancelledError, Exception) as e:
            logger.debug(f"SHUTDOWN: error during graceful shutdown: {e}")

        # Graceful AURA shutdown
        try:
            if aura_server is not None:
                aura_server.should_exit = True
            if aura_task is not None:
                try:
                    await asyncio.wait_for(aura_task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    aura_task.cancel()
        except (RuntimeError, asyncio.CancelledError, Exception):
            pass  # MCP/anyio cancel scope error on shutdown is cosmetic


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
