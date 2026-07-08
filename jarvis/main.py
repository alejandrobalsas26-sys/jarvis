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

_V4X_TASKS = []   # strong refs; asyncio only holds weak refs to tasks


def _is_windows_admin() -> bool:
    """True only when running elevated on Windows. Non-Windows returns False so
    elevation-gated subsystems (ETW) stay dormant unless explicitly forced."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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


async def _run_turn(llm, tts, user_input: str, name: str, lang: str | None = None) -> None:
    """
    Ejecuta un turno completo de conversación con el modelo Producer-Consumer.

    El producer consume el stream del LLM, acumula chunks en un buffer y
    encola oraciones completas. El consumer desencola y habla cada oración.
    Ambas corrutinas corren en paralelo via asyncio.gather.

    ``lang`` (V62.0 Phase 1) es un hint opcional de idioma detectado — se
    reenvía a tts.speak_async() para el voice-routing (TTSVoiceRouter). None
    preserva el comportamiento previo (sin cambio de voz); usado por el modo
    texto, que no tiene una fuente de detección de idioma.
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
        # V63 M6 — the spoken channel is the VOICE surface: strip Markdown so TTS
        # reads naturally instead of vocalizing `backticks`/**asterisks**/tables.
        # The console (producer's print above) keeps the full TEXT surface — one
        # reasoning result, rendered per surface, never re-reasoned.
        from core.response_surface import ResponseSurface, render
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            spoken = render(sentence, ResponseSurface.VOICE)
            if spoken:
                await tts.speak_async(spoken, lang=lang)

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


async def _loop_text(llm, tts, name: str, consent=None, state=None) -> None:
    from core.consent_commands import parse_consent_command, apply_consent_command
    from core.mode_commands import parse_mode_command, describe_mode

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

        if user_input.lower() == "/loop":
            import subprocess
            from functools import partial
            print("Ejecutando tests...")
            func = partial(subprocess.run, ["pytest"], capture_output=True, text=True)
            result = await loop.run_in_executor(None, func)
            print(result.stdout)
            if result.stderr:
                print(result.stderr)
            continue

        # V62.0 Phase 6 — explicit consent grant/revoke, same command surface
        # voice uses (core.consent_commands). Only way any sensitive tool
        # surface (screenshot/OCR/clipboard/camera) turns on for the session.
        if consent is not None:
            consent_cmd = parse_consent_command(user_input)
            if consent_cmd:
                surface, grant = consent_cmd
                print(apply_consent_command(consent, surface, grant))
                continue

        # V62.0 Phase 8 — explicit mode-switch commands, same command surface
        # voice uses (core.mode_commands). Only way the live AssistantMode changes.
        if state is not None:
            requested_mode = parse_mode_command(user_input)
            if requested_mode:
                changed = state.set_mode(requested_mode)
                if changed:
                    try:
                        from core.aura_events import ModeEvent
                        from tools.executor import _aura_broadcast
                        await _aura_broadcast(ModeEvent(mode=requested_mode.value).to_dict())
                    except Exception:
                        pass
                print(describe_mode(requested_mode))
                continue

        await _run_turn(llm, tts, user_input, name)


async def _process_voice_input(
    user_input: str, llm, tts, name: str, lang: str | None = None, consent=None,
    state=None,
) -> bool:
    """
    v35.0 — pre-process STT output. Returns True if handled (skip LLM).
    Order: interrupt commands → consent commands → mode commands → voice
    macros → LLM.

    ``lang`` (V62.0 Phase 1): detected-language hint forwarded to _run_turn's
    TTS voice routing for the LLM-routed branch. The canned interrupt replies
    below are fixed English text, so they don't take a lang hint.

    ``consent`` (V62.0 Phase 6, core.ironman_mode.SessionConsent): the shared,
    session-scoped consent object. Forwarded to voice macros so screen/camera
    macros stay gated; also the target of explicit grant/revoke commands
    (core.consent_commands) — the only way any surface turns on.

    ``state`` (V62.0 Phase 8, core.assistant_state.AssistantState): the
    shared, session-scoped operating posture. Target of explicit mode-switch
    commands (core.mode_commands) — the only way the live mode changes.
    """
    from tools.executor import _aura_broadcast
    from core.voice_interrupt import is_interrupt_command, handle_interrupt
    from core.voice_macros import process_for_macro
    from core.consent_commands import parse_consent_command, apply_consent_command
    from core.mode_commands import parse_mode_command, describe_mode

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

    # 2. Explicit consent grant/revoke commands (V62.0 Phase 6)
    if consent is not None:
        consent_cmd = parse_consent_command(user_input)
        if consent_cmd:
            surface, grant = consent_cmd
            confirmation = apply_consent_command(consent, surface, grant)
            await tts.speak_async(confirmation)
            return True

    # 3. Explicit mode-switch commands (V62.0 Phase 8)
    if state is not None:
        requested_mode = parse_mode_command(user_input)
        if requested_mode:
            changed = state.set_mode(requested_mode)
            if changed:
                try:
                    from core.aura_events import ModeEvent
                    await _aura_broadcast(ModeEvent(mode=requested_mode.value).to_dict())
                except Exception:
                    pass
            await tts.speak_async(describe_mode(requested_mode))
            return True

    # 4. Voice macros — YAML-defined shortcuts
    try:
        is_macro = await process_for_macro(user_input, _aura_broadcast, tts, consent=consent)
        if is_macro:
            return True
    except Exception as e:
        logger.debug(f"MACRO: process error: {e}")

    # 5. Normal LLM routing
    await _run_turn(llm, tts, user_input, name, lang=lang)
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


async def _loop_voice_continuous(llm, tts, stt, name: str, consent=None, state=None) -> None:
    """
    Iron Man JARVIS continuous voice loop (v46.0).

    Always listening. Conversational memory. Real-time system state.
    TTS interruption on new speech. JARVIS persona on every response.

    ``consent`` (V62.0 Phase 6, core.ironman_mode.SessionConsent): shared
    session consent gating screen/camera capture — see core.consent_commands
    for the grant/revoke command surface.

    ``state`` (V62.0 Phase 8, core.assistant_state.AssistantState): shared
    session operating posture — see core.mode_commands for the mode-switch
    command surface.
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

    # Turns go through _process_voice_input() → _run_turn() → llm.chat_stream()
    # — the same pipeline _loop_text uses, giving voice tool-calling, HITL/NATO,
    # model routing, verification, and memory. LLM.history is the conversation
    # store; there is no separate local history here.
    from core.language_context import LanguageContext
    language_context = LanguageContext()

    # ── TTS with interruption support ────────────────────────────────────
    _tts_speaking = asyncio.Event()

    async def _handle_turn(user_text: str, lang: str | None) -> None:
        """Run one conversational turn through the real agentic pipeline as a
        background task (so the VAD loop keeps reading audio_q for barge-in
        detection while JARVIS thinks/speaks — mirrors the old fire-and-forget
        asyncio.create_task(_speak(...)) pattern)."""
        _tts_speaking.set()
        try:
            await _process_voice_input(
                user_text, llm, tts, name, lang=lang, consent=consent, state=state,
            )
        except Exception as e:
            logger.debug(f"VOICE: turn error: {e}")
        finally:
            _tts_speaking.clear()

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
        # Barge-in: silence the CURRENT utterance when the user starts speaking,
        # but keep the TTS engine alive for the reply. interrupt() (not stop()/
        # stop_sync(), which are for permanent shutdown) is the right primitive —
        # the previous calls here were no-ops (async stop() left unawaited; no
        # _engine attribute exists), so barge-in silence never actually fired.
        try:
            tts.interrupt()
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

                        # V62.0 Phase 1 — refresh language context from this
                        # utterance's faster-whisper language-ID. No-op unless
                        # whisper_language='auto' (fixed mode always reports
                        # the same forced language back).
                        language_context.update(
                            getattr(stt, "last_detected_language", None),
                            getattr(stt, "last_language_confidence", 0.0),
                        )
                        lang_hint = language_context.voice_hint()

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
                            if consent is None or not consent.camera:
                                asyncio.create_task(_speak(
                                    "Camera access isn't enabled for this session. "
                                    "Say 'enable camera access' to allow it."
                                ))
                                continue
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
                            asyncio.create_task(_handle_turn(
                                f"[Vision context — visual-model analysis of the room]: {desc}\n\n"
                                "Summarize what you see in 2 sentences for the operator. "
                                "Note anything unusual or security-relevant.",
                                lang_hint,
                            ))
                            continue

                        # ── v46.0 OMEGA — Screen analysis ───────────────────
                        if any(kw in text_lower for kw in (
                            "analiza la pantalla", "analiza mi pantalla",
                            "analyze my screen", "what's on screen",
                            "es phishing", "is this phishing",
                            "analyze this", "que hay en pantalla",
                            "lee la pantalla", "read the screen",
                        )):
                            if consent is None or not consent.screen:
                                asyncio.create_task(_speak(
                                    "Screen access isn't enabled for this session. "
                                    "Say 'enable screen access' to allow it."
                                ))
                                continue
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
                            asyncio.create_task(_handle_turn(
                                f"[Vision context — screen analysis]: {desc}\n\n"
                                "Give me a 2-sentence assessment for the operator. "
                                "If it is phishing, say so clearly.",
                                lang_hint,
                            ))
                            continue

                        # Interrupt commands, voice macros, and normal LLM
                        # routing (tool-calling, HITL/NATO, model routing,
                        # verification, memory) all happen inside
                        # _process_voice_input()/_run_turn().
                        asyncio.create_task(_handle_turn(text, lang_hint))

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
        install_signal_handlers, run_graceful_shutdown, register_shutdown_callback,
    )
    from core.relevance_graph import start_pruning_loop
    from core.power_monitor import start_power_monitor
    from core.process_governor import enforce_cpu_priorities
    from core.model_router import (
        check_model_availability, configure_ollama_for_hardware,
        list_pulled_models,
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
    # v36.0 — Predictive Cognition & Autonomous Intelligence
    from core.agent_orchestrator  import orchestrator as v36_orchestrator
    from core.model_swapper       import attach as attach_model_swapper
    from core.memory_consolidator import start_consolidation_scheduler
    from core.correlator          import correlator as v36_correlator
    from core import ram_hunter, ransomware_decoy
    from core import network_quarantine, ir_reporter, honey_credentials
    from core import ai_reverser, ntdll_monitor, amsi_bridge
    from core import soar_enrichment, persistence_hunter, tarpit_deception
    from core import (dlp_sensor, exfil_detector, decoy_filesystem,
                      decoy_service, detection_harness, coverage_reporter)
    from core import c2_dashboard, health_watchdog, itdr_sentinel
    from core import dns_sinkhole, arp_deception, cmd_analyser
    from core import mobile_c2, vss_vaccine, industrial_asset_guard
    from core import kernel_telemetry, self_integrity, plugin_loader
    # v37.0 — Autonomous Intelligence & GitHub-Native Tool Ecosystem
    from core.github_explorer     import load_registry as load_github_registry
    from core.cve_intel           import start_cve_monitor
    from core.code_intel          import start_inbox_watcher
    from core.lab_manager         import list_vms as list_lab_vms
    # v38.0 — Visual Intelligence (vision/browser/diagrams/screen monitor)
    from core.vision_engine       import capture_and_save
    from core.screen_monitor      import start_screen_monitor
    # v39.0 — Deep Forensics & Autonomous Remediation
    from core.auto_remediator     import draft_mitigation
    # v40.0 — Omni-Vision, Ghost Hands & Forensic Reporter
    from tools.ghost_hands        import list_profiles
    # v41.0 — Ephemeral Docker Lab Orchestrator
    from tools.docker_manager     import _get_client as _docker_get_client
    # v42.0 — ARES PROTOCOL (Red Team Operator + Sensor Mesh + MITM Proxy)
    from core.red_team_operator   import ares_operator
    from core.sensor_mesh         import start_sensor_server
    # v43.0 — BIFROST PROTOCOL (Purple Team Coordinator + BAS + Detection Eng + OPSEC)
    from core.purple_coordinator  import attach_llm as purple_attach_llm
    from tools.breach_simulator   import run_full_bas_scenario  # noqa: F401

    # FIRST: detect hardware before any model loading or task registration
    hw_profile = detect_hardware()
    set_cached_profile(hw_profile)

    # v61.1: surface the GPU/VRAM-tier model recommendation (LOW/MID/HIGH/
    # EXTREME) alongside the TDP-tier profile above. Advisory only — it does
    # NOT change routing, it just tells the operator what this host could run
    # if they opted into bigger role models via env override. Previously this
    # only existed in scripts/model_doctor.py; surfacing it at boot means a
    # beefier desktop is told it's being undersold by the default 7B/14B
    # models, same as the TDP profile already tells a weak laptop to step down.
    try:
        from core.hardware_model_profile import detect_model_profile
        model_profile = detect_model_profile()
        logger.info(
            f"HARDWARE: GPU tier → {model_profile.tier.value} "
            f"(GPU {model_profile.gpu_vendor} {model_profile.gpu_vram_gb}GB VRAM) — "
            f"run scripts/model_doctor.py for the full per-role recommendation"
        )
    except Exception as e:
        logger.debug(f"HARDWARE: GPU-tier probe skipped: {e}")

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

    # Check model availability — clear, non-catastrophic guidance if missing.
    model_avail = await check_model_availability()
    missing = [m for m, ok in model_avail.items() if not ok]
    if missing:
        pulled = await list_pulled_models()
        if not pulled:
            logger.warning(
                "MODEL: Ollama unreachable or no models pulled — "
                f"start Ollama and run: ollama pull {missing[0]}"
            )
        else:
            for model in missing:
                logger.warning(f"MODEL: '{model}' not pulled — run: ollama pull {model}")
            logger.info(
                f"MODEL: {len(pulled)} local model(s) available as fallback: "
                f"{', '.join(pulled[:6])}"
            )
        if os.environ.get("JARVIS_OLLAMA_AUTO_PULL", "").strip().lower() not in ("1", "true", "yes", "on"):
            logger.debug("MODEL: auto-pull disabled (set JARVIS_OLLAMA_AUTO_PULL=1 to enable)")

    # Async bridge queue: STT threads push (text, confidence) here via
    # loop.call_soon_threadsafe; the executor's _challenge() awaits from it.
    stt_queue: asyncio.Queue = asyncio.Queue()

    # Pre-load Whisper in a high-priority background thread.
    # The model is ready before the LLM starts, preventing CPU contention.
    audio_listener = HighPrioritySTTListener()
    # v32.0: wire VAD events into AURA HUD broadcast
    audio_listener._loop_ref      = asyncio.get_event_loop()
    audio_listener._broadcast_ref = _aura_broadcast

    # V62.0 Phase 6 — session-scoped consent, shared by ToolExecutor (gates
    # screenshot/OCR/clipboard tools) and the voice/text loops (gates the
    # webcam/screen keyword triggers and voice macros, and is the mutation
    # target of explicit grant/revoke commands). Defaults fully OFF.
    from core.ironman_mode import default_consent
    session_consent = default_consent()

    # V62.0 Phase 8 — session-scoped operating posture, shared by the voice/
    # text loops (mutation target of explicit mode-switch commands) and any
    # proactive/background subsystem that wants to respect it (Telegram
    # push_alert, the hunt scheduler). Defaults to ACTIVE.
    from core.assistant_state import default_state
    assistant_state = default_state()

    # V63 — operator authority + authorized-scope posture. Defaults to STANDARD
    # with no scopes (scope enforcement inactive → existing risk/HITL gate
    # governs unchanged). Mutated only by explicit operator commands; a scoped
    # mode (CTF / TRUSTED_LAB / PURPLE_TEAM / INCIDENT_RESPONSE) makes target
    # actions fail-closed outside the registered scope.
    from core.authority import default_authority
    authority_state = default_authority()

    # V63 M7 — Presence Engine: the state-driven OBSERVE→UNDERSTAND→SUGGEST→ASK→
    # ACT ladder over the live mode / consent / authority / resource state. Shares
    # the same session state so its decisions track the operator's real posture.
    from core.presence import presence as presence_engine

    executor = ToolExecutor(
        stt_queue=stt_queue, stt_listener=audio_listener, consent=session_consent,
        authority=authority_state,
    )
    llm = LLM(tool_executor=executor)
    tts = TTS()

    # v58.2: tear down the MCP stdio session during graceful shutdown, before the
    # blanket task-cancellation step. Closing in-task avoids the anyio
    # "exit cancel scope in a different task" error on Ctrl+C.
    register_shutdown_callback(llm.aclose)
    # v61.1: TTS.stop() was never wired into shutdown — its worker lived in a
    # dedicated ThreadPoolExecutor that the blanket task-cancellation step
    # (run_graceful_shutdown step 4) cannot interrupt mid-utterance, so the
    # process hung 80s+ waiting on a non-daemon 'tts-worker' thread that
    # nobody had asked to stop. stop() sends the queue sentinel and shuts
    # down the executor cleanly instead of abandoning it to cancellation.
    register_shutdown_callback(tts.stop)

    # v35.0: wire tts reference into STT listener for fast interrupt path
    try:
        audio_listener._tts_ref = tts
    except Exception:
        pass

    # v58.0 COGNITIVE CORE — planner/critic/context/memory (fail-open; disabled
    # silently if modules are unavailable, never blocks startup).
    cognitive_engine = None
    try:
        from core.context_manager import ContextManager
        from core.critic import CriticEngine
        from core.task_memory import TaskMemory
        from core.cognitive_engine import CognitiveEngine

        cognitive_engine = CognitiveEngine(
            tool_executor=executor,
            llm_client=llm,
            critic=CriticEngine(),
            context_manager=ContextManager(),
            memory=TaskMemory(),
            max_steps=settings.agentic_max_cycles,
            max_wall_seconds=settings.agentic_loop_timeout,
        )
        try:
            # Passive presence marker — the cognitive engine is driven inline by
            # the incident/agentic flow, not a long-running coroutine. Supervising
            # a one-shot sleep(0) made the watchdog log it "down" every cycle.
            health_watchdog.mark_present(
                "cognitive_core",
                status_fn=lambda eng=cognitive_engine: eng is not None,
            )
        except Exception:
            pass
        logger.info("V58.0 COGNITIVE CORE: engine online")
    except Exception as e:
        logger.warning(f"V58.0 COGNITIVE CORE disabled: {e}")

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

    # V63 M4 — controlled specialist team runtime. Shares ONE inference client
    # and the SAME protected ToolExecutor as the live turn (no tool bypass), so
    # specialists reason on the shared models and any world-effect still passes
    # the risk-class / HITL / audit gate. Conservative, resource-aware defaults.
    try:
        from core.specialist_runtime import team_runtime as v63_team_runtime
        v63_team_runtime.attach(
            ollama_client = llm.client,
            fast_model    = hw_profile.model_fast,
            deep_model    = hw_profile.model_deep,
            tool_executor = executor,
            broadcast_fn  = _aura_broadcast,
        )
        logger.info("V63 M4: specialist_team_runtime attached")

        # V63 M3 — bounded task-graph planner over the same shared client, the
        # controlled team runtime (AGENT nodes), the protected executor (TOOL
        # nodes) and the fail-closed verifier (VERIFY nodes). Only planning-worthy
        # turns are ever routed here; the fast path never plans.
        from core.agent_planner import agent_planner as v63_planner
        v63_planner.attach(
            ollama_client = llm.client,
            fast_model    = hw_profile.model_fast,
            deep_model    = hw_profile.model_deep,
            llm_client    = llm,
            tool_executor = executor,
            team_runtime  = v63_team_runtime,
            broadcast_fn  = _aura_broadcast,
        )
        logger.info("V63 M3: agent_planner attached")
    except Exception as e:
        logger.debug(f"V63 M4/M3: team_runtime/planner attach failed: {e}")

    # V66 M21 — evidence-linked correlation layer. Fed the live event stream by
    # aura.server.broadcast (operational telemetry only; no legacy double-ingest),
    # it emits explainable CorrelationFinding signals to the HUD and links involved
    # entities into the M20 asset graph. main wires the broadcast surface + the
    # M22 incident-case sink so a finding can open/append an incident case.
    try:
        from core.correlation_v2 import correlator_v2 as v66_correlator_v2
        from core.correlator import correlator as _legacy_corr
        v66_correlator_v2.attach(legacy=_legacy_corr, broadcast_fn=_aura_broadcast)
        try:
            from core.incident_workspace import incident_finding_sink
            v66_correlator_v2.add_sink(incident_finding_sink)
            logger.info("V66 M22: incident-case sink attached to correlator_v2")
        except Exception as e:
            logger.debug(f"V66 M22: incident sink attach deferred: {e}")
        logger.info("V66 M21: correlator_v2 attached (evidence-linked findings)")
    except Exception as e:
        logger.debug(f"V66 M21: correlator_v2 attach failed: {e}")

    # V66 M24 — guarded runbook engine. Every runbook world-effect compiles to a
    # TaskGraph node that routes through the SAME protected executor (authority /
    # scope / risk / HITL / audit) — no second executor, no bypass. Wiring the live
    # ToolExecutor here is what lets a runbook actually run; until wired it fails
    # closed.
    try:
        from core.runbook_engine import engine as v66_runbook_engine
        v66_runbook_engine.attach(tool_executor=executor, broadcast_fn=_aura_broadcast)
        logger.info(f"V66 M24: runbook_engine attached "
                    f"({len(v66_runbook_engine.registry.names())} runbooks)")
    except Exception as e:
        logger.debug(f"V66 M24: runbook_engine attach failed: {e}")

    # V64 M11 — trusted research runtime over the SAME guarded ToolExecutor
    # (web_search/fetch_webpage route through risk-class/HITL/SSRF/audit; every
    # fetched page is trust-classified (M10) and injection-scanned (M12), and no
    # citation is ever emitted for a source that was not actually fetched).
    try:
        from core.research_runtime import attach_research_runtime
        from core.verification import verify_answer as _verify_answer

        async def _research_verify(question: str, synthesis: str):
            vr = await _verify_answer(llm.client, question, synthesis)
            return vr.verified

        attach_research_runtime(executor, verify_fn=_research_verify)
        logger.info("V64 M11: trusted_research_runtime attached")
    except Exception as e:
        logger.debug(f"V64 M11: research_runtime attach failed: {e}")
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

    # V63 M7: wire the Presence Engine + live AssistantState for presence_status.
    try:
        from aura.server import attach_presence
        attach_presence(presence_engine, assistant_state)
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
                    lambda: start_canaries(_aura_broadcast, executor, llm,
                                           cognitive_engine),
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

            # ETW kernel telemetry monitor — requires Administrator. Dormant by
            # default off-elevation so we don't spin a restart loop spamming
            # "[WinError 5] Access is denied" every backoff cycle.
            _etw_force = os.environ.get("JARVIS_ETW_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")
            if not _is_windows_admin() and not _etw_force:
                logger.info("ETW: disabled — requires Administrator "
                            "(set JARVIS_ETW_ENABLE=1 to force an attempt)")
            else:
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
                        _aura_broadcast, llm.client, tts, consent=session_consent,
                    ),
                    RestartPolicy.BACKOFF,
                )
                logger.info(
                    "SCREEN_MONITOR: registered "
                    f"({'ACTIVE' if os.getenv('JARVIS_SCREEN_MONITOR') == '1' else 'DISABLED — set JARVIS_SCREEN_MONITOR=1'})"
                )
            except Exception as e:
                logger.warning(f"Could not register screen-monitor: {e}")

            # Auto-screenshot on critical compound incidents (for reports).
            # Still consent-gated (V62.0 Phase 6) — incident severity is not
            # operator consent; grant screen access to enable evidence capture.
            try:
                _v38_orig_broadcast = _aura_broadcast
                async def _visual_broadcast(event: dict) -> None:
                    await _v38_orig_broadcast(event)
                    if (event.get("type") == "compound_incident" and
                            event.get("severity_score", 0) >= 8.0 and
                            session_consent.screen):
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
                    lambda: start_telegram_bridge(
                        _fusion_broadcast, tts, consent=session_consent, state=assistant_state,
                    ),
                    RestartPolicy.BACKOFF,
                )
                register_shutdown_callback(stop_telegram_bridge)
                logger.info("TELEGRAM: bridge registered")

                # Autonomous threat hunt scheduler — 12 hypotheses, every 4h
                watchdog.register(
                    "hunt-scheduler",
                    lambda: start_hunt_scheduler(
                        _fusion_broadcast, llm.client, hw_profile.model_deep,
                        state=assistant_state,
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
                    alert_red, alert_orange, is_configured,
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

            _V4X_TASKS.append(asyncio.create_task(ram_hunter.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(ransomware_decoy.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(network_quarantine.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(ir_reporter.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(honey_credentials.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(ai_reverser.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(ntdll_monitor.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(amsi_bridge.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(soar_enrichment.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(persistence_hunter.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(tarpit_deception.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(dlp_sensor.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(exfil_detector.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(decoy_filesystem.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(decoy_service.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(detection_harness.start(v36_correlator)))
            _V4X_TASKS.append(asyncio.create_task(coverage_reporter.start(v36_correlator)))

            _V4X_TASKS.append(asyncio.create_task(health_watchdog.start(v36_correlator, _V4X_TASKS)))
            health_watchdog.track("c2_dashboard", lambda: c2_dashboard.start(v36_correlator))
            health_watchdog.track("itdr_sentinel", lambda: itdr_sentinel.start(v36_correlator))
            health_watchdog.track("dns_sinkhole", lambda: dns_sinkhole.start(v36_correlator))
            health_watchdog.track("arp_deception", lambda: arp_deception.start(v36_correlator))
            health_watchdog.track("cmd_analyser", lambda: cmd_analyser.start(v36_correlator))
            health_watchdog.track("mobile_c2", lambda: mobile_c2.start(v36_correlator))
            health_watchdog.track("vss_vaccine", lambda: vss_vaccine.start(v36_correlator))
            health_watchdog.track("industrial_asset_guard", lambda: industrial_asset_guard.start(v36_correlator))
            health_watchdog.track("kernel_telemetry", lambda: kernel_telemetry.start(v36_correlator))
            health_watchdog.track("self_integrity",   lambda: self_integrity.start(v36_correlator))
            health_watchdog.track("plugin_loader",    lambda: plugin_loader.start(v36_correlator))

            # ── v57.0 NEXUS — Cisco Bare-Metal, GRC Auditor, PCAP Forensics ──────
            try:
                from core.cisco_controller import cisco_controller as _cisco_ctrl
                from core.grc_auditor      import grc_auditor      as _grc_aud
                from core.pcap_capture     import pcap_orchestrator as _pcap_orc

                # Wire hardware controllers to correlator (inject optional deps)
                if hasattr(v36_correlator, "attach_cisco_controller"):
                    v36_correlator.attach_cisco_controller(_cisco_ctrl)
                if hasattr(v36_correlator, "attach_pcap_orchestrator"):
                    v36_correlator.attach_pcap_orchestrator(_pcap_orc)

                # Cisco hardware watchdog (dormant when env unconfigured)
                health_watchdog.track("cisco_controller", lambda: _cisco_ctrl.start())
                _cisco_state = "ENABLED" if _cisco_ctrl.is_enabled() \
                    else "DORMANT — set JARVIS_HW_SSH_URL/USERNAME/PASSWORD"
                logger.info(f"CISCO_CTRL: bare-metal containment registered ({_cisco_state})")

                # GRC auditor periodic reports
                health_watchdog.track("grc_auditor", lambda: _grc_aud.start())
                _grc_state = "ENABLED" if _grc_aud.is_enabled() \
                    else "DISABLED — set JARVIS_GRC_ENABLED=1"
                logger.info(f"GRC_AUDITOR: compliance reporting registered ({_grc_state})")

                # PCAP orchestrator — passive (fires per-alert via correlator)
                _pcap_state = "ENABLED" if _pcap_orc.is_enabled() \
                    else "DISABLED — set JARVIS_PCAP_ENABLED=1"
                logger.info(f"PCAP_ORCHESTRATOR: forensic capture registered ({_pcap_state})")
            except Exception as _v57_err:
                logger.warning(f"V57_NEXUS: initialization failed: {_v57_err}")

            # ── v55.0 TITAN — persistent alert state + SIEM forwarding ───────────
            # Background attach: never blocks boot. Each component degrades to
            # no-op on its own (no PostgreSQL → volatile state, no SIEM_ENDPOINT
            # → events dropped locally), so JARVIS runs identically without them.
            try:
                from core.db_manager     import get_db_manager
                from core.siem_forwarder import SIEMForwarder

                async def _titan_persistence_attach() -> None:
                    try:
                        dbm  = await get_db_manager()
                        siem = SIEMForwarder()
                        await siem.start()
                        v36_correlator.attach_persistence(
                            dbm if dbm.is_connected else None,
                            siem if siem.is_enabled else None,
                        )
                        register_shutdown_callback(siem.stop)
                        register_shutdown_callback(dbm.close)
                        _pg_state = "ENABLED" if dbm.is_connected \
                            else "DEGRADED — PostgreSQL unreachable, alert state volatile"
                        _siem_state = "ENABLED" if siem.is_enabled \
                            else "NO-OP — set SIEM_ENDPOINT to enable"
                        logger.info(
                            f"TITAN_PERSISTENCE: alerts={_pg_state} | siem={_siem_state}"
                        )
                    except Exception as e:
                        logger.warning(f"TITAN_PERSISTENCE: attach failed: {e}")

                asyncio.create_task(
                    _titan_persistence_attach(), name="titan-persistence"
                )
            except Exception as _v55_err:
                logger.warning(f"V55_TITAN: initialization failed: {_v55_err}")

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
            await _loop_voice_continuous(
                llm, tts, stt, settings.assistant_name,
                consent=session_consent, state=assistant_state,
            )
        else:
            await _loop_text(
                llm, tts, settings.assistant_name,
                consent=session_consent, state=assistant_state,
            )
    finally:
        # v61.1: absolute hard-kill watchdog, independent of the event loop.
        # asyncio.wait_for()'s timeout is NOT a true ceiling on a coroutine
        # blocked awaiting a run_in_executor() future once the underlying
        # thread has started (e.g. pyttsx3.runAndWait() wedged on a COM/audio
        # issue) — cancellation is requested but wait_for itself blocks
        # re-awaiting that same never-completing future, so the internal
        # 5s/10s bounds in run_graceful_shutdown() can be silently defeated.
        # This threading.Timer runs on its own OS thread and calls os._exit()
        # directly — it owes nothing to asyncio and cannot be wedged by it.
        import threading as _threading
        _hard_kill_timer = _threading.Timer(30.0, lambda: os._exit(1))
        _hard_kill_timer.daemon = True
        _hard_kill_timer.start()

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

        _hard_kill_timer.cancel()


def main() -> None:
    try:
        # Imported for its side effect: loading core.config runs Settings() and
        # validates .env early, surfacing a clear error before the event loop.
        from core.config import settings  # noqa: F401  (early .env validation)
    except Exception as e:
        print(f"[ERROR] Configuración inválida: {e}", file=sys.stderr)
        print(
            "Copia .env.example -> .env. JARVIS usa Ollama local por defecto "
            "(ANTHROPIC_API_KEY es opcional, solo para el backend cloud).",
            file=sys.stderr,
        )
        sys.exit(1)
    asyncio.run(_main_async())

    # v61.1: force-exit after graceful async shutdown. The many v3x-v6x
    # sensor/monitor subsystems spawn raw non-daemon threading.Thread
    # workers (ETW, sensor mesh, kernel telemetry, ...) that live outside
    # asyncio.all_tasks() and don't respond to cancellation — CPython's
    # interpreter-exit sequence blocks joining every non-daemon thread, which
    # measured 80-170s in practice even though run_graceful_shutdown() above
    # (DB flush, audit log, bounded task cancellation) had already finished
    # in ~10-30s. All meaningful cleanup is done by this point, so force-exit
    # rather than hang the terminal on threads with nothing left to do.
    import threading
    lingering = [t.name for t in threading.enumerate()
                if t is not threading.main_thread() and not t.daemon]
    if lingering:
        logger.warning(
            f"SHUTDOWN: {len(lingering)} non-daemon thread(s) still alive "
            f"after graceful shutdown — forcing exit: {lingering}"
        )
    os._exit(0)


if __name__ == "__main__":
    main()
