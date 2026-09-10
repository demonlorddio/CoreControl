"""
Voice Activity Detection and audio recording module for CoreControl.
Listens to microphone input, detects speech via energy-based VAD,
transcribes complete utterances using a standalone Whisper subprocess
(to avoid CTranslate2 segfaulting with PyQt6), and routes text to
the orchestrator.
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Settings ──────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "settings.json"


def _load_audio_settings() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("audio", {})
    except Exception:
        return {}


_cfg = _load_audio_settings()
SAMPLE_RATE: int = _cfg.get("sample_rate", 16000)
CHANNELS: int = 1
_RAW_VAD_THRESHOLD: float = _cfg.get("vad_threshold", None)  # None = auto-calibrate
MIN_SILENCE_MS: int = _cfg.get("min_silence_duration_ms", 1000)
WHISPER_MODEL: str = _cfg.get("transcription_model", "base.en")

# Block size in samples (~30ms chunks)
BLOCK_SIZE: int = int(SAMPLE_RATE * 0.03)
SILENCE_BLOCKS: int = max(1, int(MIN_SILENCE_MS / 30))

# ── Whisper subprocess (module-level singleton) ───────────────────────────────
# CTranslate2 segfaults inside the same process as PyQt6, so we run Whisper
# in a separate subprocess that talks to us via JSON over stdin/stdout.
_whisper_proc: Optional[subprocess.Popen] = None
_whisper_lock = threading.Lock()


def _start_whisper_worker() -> None:
    """Launch the standalone Whisper transcription worker subprocess."""
    global _whisper_proc
    try:
        script = Path(__file__).parent / "whisper_worker.py"
        _whisper_proc = subprocess.Popen(
            [sys.executable, str(script), WHISPER_MODEL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        logger.info("Whisper subprocess started (pid=%d)", _whisper_proc.pid)
    except Exception as exc:
        logger.error("Failed to start Whisper subprocess: %s", exc)
        _whisper_proc = None


def _stop_whisper_worker() -> None:
    """Gracefully shut down the Whisper subprocess."""
    global _whisper_proc
    if _whisper_proc is None:
        return
    try:
        _whisper_proc.stdin.write(json.dumps({"stop": True}) + "\n")
        _whisper_proc.stdin.flush()
        _whisper_proc.wait(timeout=3)
    except Exception as exc:
        logger.debug("Whisper subprocess cleanup: %s", exc)
    finally:
        _whisper_proc = None


def _whisper_transcribe(audio: np.ndarray) -> str:
    """
    Send audio to the Whisper subprocess and return the transcribed text.
    Audio is serialized as base64-encoded float32 bytes.
    """
    global _whisper_proc
    if _whisper_proc is None:
        return ""
    try:
        audio_b64 = base64.b64encode(audio.tobytes()).decode("ascii")
        req = json.dumps({"audio": audio_b64, "len": len(audio)})
        with _whisper_lock:
            _whisper_proc.stdin.write(req + "\n")
            _whisper_proc.stdin.flush()
            line = _whisper_proc.stdout.readline()
        resp = json.loads(line)
        return resp.get("text", "") or resp.get("error", "")
    except Exception as exc:
        logger.error("Whisper subprocess error: %s", exc)
        return ""


# ── VAD calibration ───────────────────────────────────────────────────────────

def _calibrate_vad_threshold() -> float:
    """Measure ambient noise for 1.5 s and return a threshold 3× the RMS peak."""
    try:
        import sounddevice as sd
    except ImportError:
        return 0.03  # fallback
    frames: list[np.ndarray] = []

    def _cb(_indata, _frames, _time_info, _status):  # noqa: D401
        if not _status:
            frames.append(_indata[:, 0].copy())

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            blocksize=BLOCK_SIZE, dtype="float32", callback=_cb):
            time.sleep(1.5)
    except Exception as exc:
        logger.warning("VAD calibration failed (%s), using default threshold", exc)
        return 0.03
    if not frames:
        return 0.03
    all_data = np.concatenate(frames)
    rms = float(np.sqrt(np.mean(all_data ** 2)))
    threshold = max(rms * 3.0, 0.005)  # at least 0.005 to catch quiet speech
    logger.info("VAD calibrated: ambient RMS=%.4f, threshold=%.4f", rms, threshold)
    return float(threshold)


# Resolve VAD threshold: explicit config > auto-calibrated > hardcoded default
VAD_THRESHOLD: float = (
    _RAW_VAD_THRESHOLD
    if _RAW_VAD_THRESHOLD is not None
    else _calibrate_vad_threshold()
)


class AudioRecorder:
    """
    Background thread that captures microphone audio, performs energy-based VAD,
    and transcribes complete utterances using a standalone Whisper subprocess
    (to avoid CTranslate2 segfaulting when used alongside PyQt6).
    """

    def __init__(
        self,
        on_transcription: Callable[[str], None],
        sample_rate: int = SAMPLE_RATE,
        vad_threshold: float = VAD_THRESHOLD,
        model_name: str = WHISPER_MODEL,
    ) -> None:
        self._callback = on_transcription
        self._sample_rate = sample_rate
        self._vad_threshold = vad_threshold
        self._model_name = model_name

        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._running = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self.is_recording: bool = False  # True while speech is accumulating

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start microphone capture and transcription processing threads."""
        # Ensure the Whisper subprocess is running
        with _whisper_lock:
            if _whisper_proc is None:
                _start_whisper_worker()
        self._running.set()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="audio-capture"
        )
        self._process_thread = threading.Thread(
            target=self._processing_loop, daemon=True, name="audio-process"
        )
        self._capture_thread.start()
        self._process_thread.start()
        logger.info("Audio recorder started (model=%s, sr=%d)", self._model_name, self._sample_rate)

    def stop(self) -> None:
        """Stop both threads and the Whisper subprocess."""
        self._running.clear()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3.0)
        if self._process_thread and self._process_thread.is_alive():
            self._audio_queue.put(None)  # poison pill
            self._process_thread.join(timeout=5.0)
        _stop_whisper_worker()
        logger.info("Audio recorder stopped")

    # ── Internal loops ────────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """Capture audio blocks from the microphone and push them to the queue."""
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not installed. Run: pip install sounddevice")
            return

        def _sd_callback(
            indata: np.ndarray,
            frames: int,
            time_info,
            status,
        ) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            self._audio_queue.put(indata[:, 0].copy())

        try:
            with sd.InputStream(
                samplerate=self._sample_rate,
                channels=CHANNELS,
                blocksize=BLOCK_SIZE,
                dtype="float32",
                callback=_sd_callback,
            ):
                logger.debug("Microphone stream open")
                while self._running.is_set():
                    time.sleep(0.1)
        except Exception as exc:
            logger.error("Microphone capture error: %s", exc)

    def _processing_loop(self) -> None:
        """
        Consume audio blocks, apply VAD, accumulate speech frames,
        and dispatch completed utterances for transcription.
        """
        speech_frames: list[np.ndarray] = []
        silence_count: int = 0
        in_speech: bool = False

        while self._running.is_set():
            try:
                block = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if block is None:  # poison pill
                break

            is_speech = self._vad_detect(block)

            if is_speech:
                if not in_speech:
                    logger.debug("Speech started")
                    in_speech = True
                    self.is_recording = True
                silence_count = 0
                speech_frames.append(block)
            else:
                if in_speech:
                    silence_count += 1
                    speech_frames.append(block)
                    if silence_count >= SILENCE_BLOCKS:
                        logger.debug("Speech ended (%d blocks)", len(speech_frames))
                        audio = np.concatenate(speech_frames)
                        self._transcribe(audio)
                        speech_frames = []
                        silence_count = 0
                        in_speech = False
                        self.is_recording = False

    def _vad_detect(self, block: np.ndarray) -> bool:
        """
        Simple energy-based Voice Activity Detection.
        Returns True if the RMS energy exceeds the configured threshold.
        """
        rms = float(np.sqrt(np.mean(block ** 2)))
        return rms > self._vad_threshold

    def _transcribe(self, audio: np.ndarray) -> None:
        """Transcribe a complete speech segment via the Whisper subprocess."""
        try:
            text = _whisper_transcribe(audio)
            if text:
                logger.info("Transcribed: %r", text[:120])
                self._callback(text)
        except Exception as exc:
            logger.error("Transcription error: %s", exc)
