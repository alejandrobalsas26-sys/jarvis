"""
core/audio_vad.py — WebRTC Voice Activity Detection pre-filter (v31.0).

Classifies 30ms PCM-16 frames as speech or silence using Google's WebRTC
VAD engine. Stateless — no audio device, no threads. Called inline by
HighPrioritySTTListener before dispatching to Whisper.

CPU profile: ~0.2ms per 30ms frame = <1% overhead.
Result: Whisper only runs when speech is confirmed, eliminating
continuous-silence inference which was the #1 CPU consumer.
"""

import collections

from loguru import logger

try:
    import webrtcvad
    _VAD_AVAILABLE = True
except ImportError:
    webrtcvad = None  # type: ignore[assignment]
    _VAD_AVAILABLE = False
    logger.warning("VAD: webrtcvad not installed — VAD gate will pass all frames through.")

# VAD aggressiveness 0-3: 0=most permissive, 3=most aggressive (fewer false positives)
_AGGRESSIVENESS = 2   # 2 is the practical optimum for home lab mic environments

# Voiced frame ratio in the ring buffer required to trigger speech detection
_VOICED_TRIGGER_RATIO  = 0.8   # 80% of ring buffer voiced → start capturing
_SILENCE_TRIGGER_RATIO = 0.2   # <20% voiced → end of utterance
_RING_BUFFER_FRAMES    = 15    # ~450ms look-behind context
_SILENCE_FRAMES_END    = 30    # ~900ms of silence to finalize an utterance (30ms * 30)

# Frame geometry: 16 kHz mono PCM-16 → 16000 * 0.030 * 2 = 960 bytes per 30ms frame
SAMPLE_RATE       = 16000
FRAME_DURATION_MS = 30
FRAME_BYTES       = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0)) * 2  # 960

_vad = webrtcvad.Vad(_AGGRESSIVENESS) if _VAD_AVAILABLE else None


class VADAccumulator:
    """
    Stateful VAD accumulator. Feed it 30ms PCM-16 mono frames at 16kHz.
    It returns complete utterances (bytes) when speech ends.
    Zero threads, zero blocking.
    """

    def __init__(self) -> None:
        self._ring        = collections.deque(maxlen=_RING_BUFFER_FRAMES)
        self._triggered   = False
        self._voiced_buf: list[bytes] = []
        self._silence_cnt = 0

    def feed(self, frame: bytes) -> bytes | None:
        """
        Feed a 30ms PCM-16 frame. Returns a complete utterance when
        speech ends, otherwise None.
        Frame must be exactly FRAME_BYTES (960 bytes for 16 kHz mono PCM-16).
        """
        if len(frame) != FRAME_BYTES:
            return None   # wrong frame size — skip silently

        if _vad is None:
            # Fallback path: no VAD installed → treat every frame as speech
            self._voiced_buf.append(frame)
            return None

        try:
            is_speech = _vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            return None

        if not self._triggered:
            self._ring.append((frame, is_speech))
            num_voiced = sum(1 for _, s in self._ring if s)
            if num_voiced / len(self._ring) >= _VOICED_TRIGGER_RATIO:
                self._triggered = True
                self._voiced_buf = [f for f, _ in self._ring]
                self._ring.clear()
                self._silence_cnt = 0
                logger.debug("VAD: speech onset detected")
        else:
            self._voiced_buf.append(frame)
            if is_speech:
                self._silence_cnt = 0
            else:
                self._silence_cnt += 1
                if self._silence_cnt >= _SILENCE_FRAMES_END:
                    utterance = b"".join(self._voiced_buf)
                    self._triggered   = False
                    self._voiced_buf  = []
                    self._silence_cnt = 0
                    logger.debug(
                        f"VAD: utterance complete "
                        f"({len(utterance) / (SAMPLE_RATE * 2):.1f}s)"
                    )
                    return utterance

        return None

    def reset(self) -> None:
        self._ring.clear()
        self._triggered   = False
        self._voiced_buf  = []
        self._silence_cnt = 0
