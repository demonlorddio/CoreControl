"""Great Sage TTS — Edge-TTS + Voice-of-the-World DSP pipeline with Japanese support."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import subprocess
import threading
import tempfile
import wave
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Cache directory — parent of this file is src/tts/, so parent.parent.parent is project root
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / ".tts_cache"
_CACHE_DIR.mkdir(exist_ok=True)

# ── Sample rate & DSP constants ───────────────────────────────────────────────
SR = 24000  # edge-tts native sample rate


# ── Voice selection ──────────────────────────────────────────────────────────
# Preset voices matching the tensura-great-sage project
_DEFAULT_VOICE_EN = "en-US-AriaNeural"   # calm, clear, slightly cool female
_DEFAULT_VOICE_JA = "ja-JP-NanamiNeural" # natural Japanese female
_SPEECH_RATE = "-10%"                     # slower, portentous delivery
_PITCH_SHIFT = "-4Hz"                     # lower = more sage-like
_VOLUME = "+0%"

# Supported languages
SUPPORTED_LANGUAGES = {
    "English": "en",
    "Japanese": "ja",
}

# Common phrase translations (Great Sage persona)
TRANSALATIONS: dict[str, str] = {
    # Greetings
    "Notice, Master.": "マスター、ご通知いたします。",
    "Report.": "報告いたします。",
    "Analysis completed.": "分析を完了しました。",
    "All systems operational.": "全システム稼働中です。",
    # Responses
    "I have analyzed the current system state.": "現在のシステム状態を分析しました。",
    "All systems are operational.": "すべてのシステムが稼働しています。",
    "System status check complete.": "システム状態チェック完了。",
    "Processing your request, Master.": "リクエストを処理いたします、マスター。",
    "The analysis is complete.": "分析は完了しました。",
    "Visual analysis completed.": "視覚分析を完了しました、マスター。",
    # Status
    "Awaiting your command.": "ご命令をお待ちしております。",
    "Stand by, Master.": "スタンバイ、マスター。",
    "Connecting to the network.": "ネットワークに接続中。",
}


# ── DSP Pipeline (Voice of the World) ────────────────────────────────────────

def highpass(x: np.ndarray, hz: float = 120.0) -> np.ndarray:
    """Simple one-pole highpass (rumble removal)."""
    dt = 1.0 / SR
    rc = 1.0 / (2 * math.pi * hz)
    a = rc / (rc + dt)
    y = np.empty_like(x)
    prev_x = prev_y = 0.0
    for i in range(x.size):
        prev_y = a * (prev_y + x[i] - prev_x)
        prev_x = x[i]
        y[i] = prev_y
    return y


def saturate(x: np.ndarray, drive: float = 0.5) -> np.ndarray:
    """Soft tanh saturation: smooth, slightly compressed, 'processed' voice."""
    return np.tanh(x * (1.0 + drive * 4.0)) / math.tanh(1.0 + drive * 4.0)


def feedback_comb(x: np.ndarray, delay_s: float, gain: float) -> np.ndarray:
    """y[n] = x[n] + g*y[n-D] -> static metallic resonance."""
    d = max(1, int(round(delay_s * SR)))
    y = np.empty_like(x)
    memory = np.zeros(d, dtype=np.float64)
    idx = 0
    for i in range(x.size):
        acc = x[i] + gain * memory[idx]
        memory[idx] = acc
        y[i] = acc
        idx = (idx + 1) % d
    return y.astype(np.float32)


def vibrato(x: np.ndarray, rate_hz: float = 0.4, depth: float = 1.2) -> np.ndarray:
    """Slow pitch drift via modulated delay line (linear interpolation)."""
    n = np.arange(x.size)
    pos = n + depth * np.sin(2 * math.pi * rate_hz * n / SR)
    pos = np.clip(pos, 0, x.size - 1)
    i0 = np.floor(pos).astype(np.int64)
    i1 = np.minimum(i0 + 1, x.size - 1)
    frac = (pos - i0).astype(np.float32)
    return x[i0] * (1 - frac) + x[i1] * frac


def schroeder_reverb(x: np.ndarray, wet: float = 0.35) -> np.ndarray:
    """Sparse Schroeder reverberator: 4 combs -> 3 allpasses (bright, vast tail)."""
    if x.ndim == 2:  # process each stereo channel independently
        return np.stack([schroeder_reverb(x[:, c], wet) for c in range(x.shape[1])], axis=1)
    combs = [(0.0297, 0.78), (0.0371, 0.78), (0.0411, 0.78), (0.0437, 0.78)]
    sums = np.zeros_like(x, dtype=np.float64)
    for delay_s, g in combs:
        d = int(round(delay_s * SR))
        buf = np.zeros(d, dtype=np.float64)
        idx = 0
        out = np.empty(x.size, dtype=np.float64)
        for i in range(x.size):
            acc = x[i] + g * buf[idx]
            buf[idx] = acc
            out[i] = acc
            idx = (idx + 1) % d
        sums += out
    y = sums / len(combs)

    for delay_s, g in [(0.005, 0.7), (0.0017, 0.7), (0.0011, 0.7)]:
        d = int(round(delay_s * SR))
        buf = np.zeros(d, dtype=np.float64)
        idx = 0
        out = np.empty_like(y)
        for i in range(y.size):
            buf_idx_val = buf[idx]
            out[i] = -g * y[i] + buf_idx_val
            buf[idx] = y[i] + g * buf_idx_val
            idx = (idx + 1) % d
        y = out

    return (x * (1 - wet) + y * wet).astype(np.float32)


def make_stereo(x: np.ndarray) -> np.ndarray:
    """Left: dry + comb resonance. Right: 11 ms delayed + one soft echo repeat."""
    comb = feedback_comb(x, 0.0047, 0.35)
    left = 0.7 * x + 0.3 * comb
    delay = int(0.011 * SR)
    right = np.empty_like(x)
    right[:delay] = x[:delay]
    right[delay:] = x[:-delay]
    right = 0.8 * right
    echo = np.zeros_like(x)
    shift = int(0.19 * SR)
    if x.size > shift:
        echo[shift:] = right[:-shift] * 0.45
    right = right + echo
    st = np.empty((x.size, 2), dtype=np.float32)
    st[:, 0] = left
    st[:, 1] = right
    return st


def add_fingerprint(st: np.ndarray, level_db: float = -34.0) -> np.ndarray:
    """Great Sage's low machine hum (55 Hz) under the voice."""
    t = np.arange(st.shape[0]) / SR
    hum = np.sin(2 * math.pi * 55.0 * t) + 0.35 * np.sin(2 * math.pi * 110.0 * t)
    amp = 10 ** (level_db / 20) * 0.4
    st = st.copy()
    st[:, 0] += amp * hum
    st[:, 1] += amp * hum * 0.9
    return st


def normalize(st: np.ndarray, peak_db: float = -1.5) -> np.ndarray:
    """Peak normalize to just below 0 dB."""
    peak = np.max(np.abs(st)) or 1.0
    return st * (10 ** (peak_db / 20) / peak)


def write_wav(path: str, st: np.ndarray):
    """Write 16-bit stereo WAV at SR."""
    pcm = (np.clip(st, -1, 1) * 32767).astype("<i2")
    inter = pcm.reshape(-1).tolist()
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.array(inter, dtype="<i2").tobytes())


def decode_to_pcm(mp3_path: str) -> np.ndarray:
    """Decode MP3 to mono float32 at SR via ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error", "-i", mp3_path,
        "-f", "s16le", "-ac", "1", "-ar", str(SR), "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size == 0:
        raise ValueError("TTS produced no audio (empty input text?)")
    return x


def apply_dsp_chain(x: np.ndarray, reverb: float = 0.35, fingerprint: bool = True) -> np.ndarray:
    """Apply the full Voice-of-the-World DSP chain."""
    x = highpass(x)
    x = saturate(x)
    x = vibrato(x)
    st = make_stereo(x)
    st = schroeder_reverb(st, wet=reverb)
    if fingerprint:
        st = add_fingerprint(st)
    st = normalize(st)
    return st


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_japanese(text: str) -> bool:
    """Return True if text contains Japanese characters."""
    for ch in text:
        cp = ord(ch)
        if (0x3040 <= cp <= 0x30FF) or (0xFF00 <= cp <= 0xFF9F) or (0x4E00 <= cp <= 0x9FFF):
            return True
    return False


def _cache_key(text: str, lang: str, voice: str) -> str:
    """Short hex key for cache lookup."""
    h = hashlib.md5(f"{lang}_{voice}_{text}".encode()).hexdigest()[:12]
    return f"{h}.wav"


def _estimate_duration_ms(text: str) -> int:
    """Estimate audio duration from word count at ~135 wpm (slower due to DSP), min 3000 ms."""
    word_count = len(text.split())
    return max(int((word_count / 135.0) * 60.0 * 1000), 3000)


class LocalTTS:
    """
    Great Sage TTS engine.

    Pipeline:
      1. Synthesize speech with edge-tts (free neural voices)
      2. Decode to 24 kHz mono PCM via ffmpeg
      3. Apply Voice-of-the-World DSP chain
      4. Write 16-bit stereo WAV to cache
      5. Play via AudioPlayer

    Supports English and Japanese.
    """

    def __init__(self, language: str = "en", voice: Optional[str] = None,
                 reverb: float = 0.35, fingerprint: bool = True) -> None:
        self._language = language  # "en" or "ja"
        self._voice = voice or (_DEFAULT_VOICE_JA if language == "ja" else _DEFAULT_VOICE_EN)
        self._reverb = reverb
        self._fingerprint = fingerprint
        self._cache_dir = _CACHE_DIR
        self._lock = threading.Lock()
        self._current_task: Optional[threading.Thread] = None
        self._stop_requested = False

    @property
    def language(self) -> str:
        return self._language

    @language.setter
    def language(self, value: str) -> None:
        lang_map = {"en": "en", "ja": "ja", "English": "en", "Japanese": "ja"}
        self._language = lang_map.get(value, value)
        # Update voice when language changes
        if self._language == "ja":
            self._voice = _DEFAULT_VOICE_JA
        else:
            self._voice = _DEFAULT_VOICE_EN

    def translate_to_japanese(self, text: str) -> str:
        """Translate text to Japanese. Checks known phrases first, then Google Translate."""
        if text in TRANSALATIONS:
            return TRANSALATIONS[text]
        try:
            import translators
            translated = translators.translate_text(text, translator="google", to_language="ja")
            if translated and translated != text:
                logger.info("Translated: %s -> %s", text[:50], translated[:50])
                return translated
        except Exception as e:
            logger.debug("Translation failed: %s", e)
        return text

    def speak(self, text: str, callback: Optional[callable] = None) -> None:
        """
        Generate and play speech. Returns immediately; plays asynchronously.

        Args:
            text: The text to speak.
            callback: Called with (success: bool) when done.
        """
        if self._language == "ja":
            text_to_speak = self.translate_to_japanese(text)
        else:
            text_to_speak = text

        cache_file = self._cache_dir / _cache_key(text_to_speak, self._language, self._voice)

        def _play():
            try:
                audio_data = self._generate_or_get_audio(text_to_speak, cache_file)
                if audio_data is None:
                    logger.error("No audio generated for: %s", text[:40])
                    if callback:
                        callback(False)
                    return

                from .player import AudioPlayer
                player = AudioPlayer()
                success = player.play_audio(audio_data)
                if callback:
                    callback(success)
            except Exception as e:
                logger.error("Playback failed: %s", e)
                if callback:
                    callback(False)

        self._stop_requested = False
        self._current_task = threading.Thread(target=_play, daemon=True)
        self._current_task.start()

    def _generate_or_get_audio(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Return cached audio bytes or generate new audio via DSP pipeline."""
        if cache_file.exists():
            logger.debug("Using cached audio: %s", cache_file.name)
            return cache_file.read_bytes()

        # Try edge-tts + DSP pipeline
        data = self._generate_edge_tts_dsp(text, cache_file)
        if data:
            return data

        # Fall back to plain edge-tts (no DSP)
        logger.warning("DSP pipeline failed, trying plain edge-tts for: %s", text[:40])
        data = self._generate_edge_tts_plain(text, cache_file)
        if data:
            return data

        # Fall back to pyttsx3
        logger.warning("edge-tts failed, falling back to pyttsx3 for: %s", text[:40])
        data = self._generate_pyttsx3(text, cache_file)
        if data:
            return data

        logger.error("All TTS engines failed")
        return None

    def _generate_edge_tts_dsp(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Generate speech via edge-tts + Voice-of-the-World DSP chain."""
        try:
            import edge_tts

            with tempfile.TemporaryDirectory() as td:
                mp3_path = os.path.join(td, "tts.mp3")

                # Step 1: Synthesize with edge-tts
                async def _synth():
                    tts = edge_tts.Communicate(
                        text, self._voice,
                        rate=_SPEECH_RATE,
                        pitch=_PITCH_SHIFT,
                        volume=_VOLUME,
                    )
                    await tts.save(mp3_path)

                asyncio.run(_synth())

                if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 500:
                    logger.warning("edge-tts generated suspiciously small file")
                    return None

                # Step 2: Decode to 24 kHz mono PCM
                x = decode_to_pcm(mp3_path)

                # Step 3: Apply Voice-of-the-World DSP chain
                st = apply_dsp_chain(x, reverb=self._reverb, fingerprint=self._fingerprint)

                # Step 4: Write 16-bit stereo WAV to cache
                wav_path = str(cache_file)
                write_wav(wav_path, st)

                if cache_file.exists() and cache_file.stat().st_size >= 500:
                    logger.info("edge-tts+DSP generated %d bytes for: %s",
                                cache_file.stat().st_size, text[:30])
                    return cache_file.read_bytes()

                logger.warning("DSP output suspiciously small")
                cache_file.unlink(missing_ok=True)
                return None

        except ImportError:
            logger.warning("edge-tts not installed — cannot use primary engine")
            return None
        except Exception as e:
            logger.warning("edge-tts+DSP failed (%s), will try plain edge-tts", e)
            return None

    def _generate_edge_tts_plain(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Fallback: plain edge-tts without DSP (for compatibility)."""
        try:
            import edge_tts
            temp_path = str(cache_file)

            comm = edge_tts.Communicate(text, self._voice, rate=_SPEECH_RATE)
            asyncio.run(comm.save(temp_path))

            if cache_file.exists() and cache_file.stat().st_size >= 500:
                logger.info("edge-tts (plain) generated %d bytes for: %s",
                            cache_file.stat().st_size, text[:30])
                return cache_file.read_bytes()

            logger.warning("edge-tts plain generated suspiciously small file")
            cache_file.unlink(missing_ok=True)
            return None
        except Exception as e:
            logger.warning("edge-tts plain failed: %s", e)
            return None

    def _generate_pyttsx3(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Fallback TTS using local pyttsx3 (fully offline, lower quality)."""
        try:
            import pyttsx3
            temp_path = str(self._cache_dir / f"_pyttsx_{hashlib.md5(text.encode()).hexdigest()[:12]}.wav")

            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            if voices:
                for v in voices:
                    if "david" in v.name.lower() or "male" in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break

            engine.setProperty("rate", 145)
            engine.save_to_file(text, temp_path)
            engine.runAndWait()

            if os.path.exists(temp_path):
                data = Path(temp_path).read_bytes()
                if len(data) < 1000:
                    logger.warning("pyttsx3 generated suspiciously small file (%d bytes)", len(data))
                    Path(temp_path).unlink(missing_ok=True)
                    return None
                cache_file.write_bytes(data)
                logger.info("pyttsx3 generated %d bytes for: %s", len(data), text[:30])
                return data
            return None
        except Exception as e:
            logger.error("pyttsx3 failed: %s", e)
            return None

    def stop(self) -> None:
        """Stop current speech playback."""
        self._stop_requested = True
        from .player import AudioPlayer
        AudioPlayer.stop_all()
        if self._current_task:
            self._current_task.join(timeout=1.0)
            self._current_task = None

    def get_duration_estimate(self, text: str) -> int:
        """Estimate audio duration in ms."""
        return _estimate_duration_ms(text)


class TTSRequestThread:
    """Qt-compatible wrapper for async TTS requests."""

    def __init__(self, tts: LocalTTS, text: str, callback: Optional[callable] = None):
        self._tts = tts
        self._text = text
        self._callback = callback

    def run(self) -> tuple[bool, int]:
        """Run TTS and return (success, duration_ms)."""
        try:
            duration_ms = self._tts.get_duration_estimate(self._text)
            self._tts.speak(self._text)
            return (True, duration_ms)
        except Exception as e:
            logger.error("TTS request failed: %s", e)
            return (False, 0)
