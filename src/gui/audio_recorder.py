"""
Voice Activity Detection and audio recording module for CoreControl.
Listens to microphone input, detects speech via energy-based VAD,
transcribes with faster-whisper, and routes text to the orchestrator.
"""
from __future__ import annotations

import json
import logging
import queue
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
VAD_THRESHOLD: float = _cfg.get("vad_threshold", 0.5)
MIN_SILENCE_MS: int = _cfg.get("min_silence_duration_ms", 1000)
WHISPER_MODEL: str = _cfg.get("transcription_model", "base.en")

# Block size in samples (~30ms chunks)
BLOCK_SIZE: int = int(SAMPLE_RATE * 0.03)
SILENCE_BLOCKS: int = max(1, int(MIN_SILENCE_MS / 30))


class AudioRecorder:
    """
    Background thread that captures microphone audio, performs energy-based VAD,
    and transcribes complete utterances using faster-whisper (fully offline).

    Usage:
        def handle(text: str): print(text)
        recorder = AudioRecorder(on_transcription=handle)
        recorder.start()
        ...
        recorder.stop()
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
        self._model = None  # loaded lazily
        self.is_recording: bool = False  # True while speech is accumulating

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start microphone capture and transcription processing threads."""
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
        """Stop both threads gracefully."""
        self._running.clear()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3.0)
        if self._process_thread and self._process_thread.is_alive():
            self._audio_queue.put(None)  # poison pill
            self._process_thread.join(timeout=5.0)
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
        """Transcribe a complete speech segment using faster-whisper (offline)."""
        try:
            if self._model is None:
                self._model = self._load_model()
            if self._model is None:
                return

            audio_float = audio.astype(np.float32)

            segments, info = self._model.transcribe(
                audio_float,
                beam_size=5,
                language="en",
                vad_filter=True,
            )
            text = " ".join(seg.text for seg in segments).strip()
            if text:
                logger.info("Transcribed: %r", text[:120])
                self._callback(text)
        except Exception as exc:
            logger.error("Transcription error: %s", exc)

    def _load_model(self):
        """Lazily load the faster-whisper model."""
        try:
            from faster_whisper import WhisperModel
            logger.info("Loading Whisper model: %s", self._model_name)
            model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
            logger.info("Whisper model loaded")
            return model
        except ImportError:
            logger.error("faster-whisper not installed. Run: pip install faster-whisper")
            return None
        except Exception as exc:
            logger.error("Failed to load Whisper model: %s", exc)
            return None
