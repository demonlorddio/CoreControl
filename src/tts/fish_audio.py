"""Fish Audio TTS engine with Japanese translation fallback."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fish_audio_sdk import Session, TTSRequest
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# Great Sage voice model (from fish.audio)
GREAT_SAGE_MODEL_ID = "2c8b612163a24831b0db6675ff4ebb9b"

# Supported languages
SUPPORTED_LANGUAGES = {
    "English": "en",
    "Japanese": "ja",
}

# Translations for common Great Sage phrases
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
    # Status updates
    "Awaiting your command.": "ご命令をお待ちしております。",
    "Stand by, Master.": " standby、マスター。",
    "Connecting to the network.": "ネットワークに接続中。",
}


class FishAudioTTS:
    """
    Fish Audio TTS with automatic Japanese translation.

    When language is 'Japanese':
    1. The text is checked against TRANSALATIONS for known phrases
    2. If no match, the text is translated using free translation APIs
    3. Fish Audio generates speech from the translated text
    4. The MP3 is cached for reuse

    When language is 'English':
    - Fish Audio generates speech from the original text
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get("FISH_AUDIO_API_KEY", "")
        self._session: Optional[Session] = None
        self._language = "en"  # default English
        self._cache_dir = Path(__file__).resolve().parent.parent.parent / ".tts_cache"
        self._cache_dir.mkdir(exist_ok=True)
        self._voice_model = GREAT_SAGE_MODEL_ID

        # Thread-safe lock for API calls
        self._lock = threading.Lock()
        self._current_task: Optional[threading.Thread] = None
        self._stop_requested = False

    @property
    def language(self) -> str:
        return self._language

    @language.setter
    def language(self, value: str) -> None:
        """Set language. Accepts 'en' or 'ja'."""
        lang_map = {"en": "English", "ja": "Japanese", "English": "English", "Japanese": "Japanese"}
        self._language = lang_map.get(value, value)

    def _ensure_session(self) -> bool:
        """Initialize the Fish Audio session if needed."""
        if not self._api_key:
            logger.warning("Fish Audio API key not configured. Set FISH_AUDIO_API_KEY env var or add to settings.")
            return False
        if self._session is None:
            try:
                self._session = Session(self._api_key)
                logger.info("Fish Audio session initialized with Great Sage voice model")
            except Exception as e:
                logger.error("Failed to initialize Fish Audio: %s", e)
                return False
        return True

    @classmethod
    def from_settings(cls, settings_path: str = "config/settings.json") -> "FishAudioTTS":
        """Create FishAudioTTS instance from settings.json."""
        import json
        from pathlib import Path
        path = Path(settings_path)
        if not path.exists():
            raise FileNotFoundError(f"Settings not found: {path}")
        with open(path) as f:
            settings = json.load(f)
        fish_config = settings.get("fish_audio", {})
        api_key = fish_config.get("api_key", "")
        if not api_key:
            raise ValueError("Fish Audio API key not configured in settings.json")
        instance = cls(api_key=api_key)
        model_id = fish_config.get("model_id", "")
        if model_id:
            instance._voice_model = model_id
        lang = fish_config.get("language", "en")
        instance.language = lang
        return instance

    def translate_to_japanese(self, text: str) -> str:
        """Translate text to Japanese using free translation APIs."""
        # Check known translations first
        if text in TRANSALATIONS:
            return TRANSALATIONS[text]

        # Try multiple translation sources
        try:
            import translators
            translated = translators.translate_text(text, translator="google", to_language="ja")
            if translated and translated != text:
                logger.info("Translated to Japanese: %s -> %s", text[:50], translated[:50])
                return translated
        except Exception as e:
            logger.debug("Translation failed (will use original): %s", e)

        # Fallback: return original text
        logger.warning("Could not translate to Japanese, using original text")
        return text

    def speak(self, text: str, callback: Optional[callable] = None) -> None:
        """
        Generate and play speech from text.

        For Japanese mode: text is translated to Japanese first.
        For English mode: text is used as-is.

        The method returns immediately; speech plays asynchronously.
        """
        if not self._ensure_session():
            logger.error("Cannot speak: Fish Audio not configured")
            if callback:
                callback(None)
            return

        # Translate if Japanese
        if self._language == "ja":
            text_to_speak = self.translate_to_japanese(text)
        else:
            text_to_speak = text

        # Generate cache key
        cache_key = f"{self._language}_{self._voice_model}_{hash(text_to_speak) & 0xFFFFFFFF:08x}"
        cache_file = self._cache_dir / f"{cache_key}.mp3"

        def _play():
            try:
                # Generate or retrieve cached audio
                audio_data = self._generate_or_get_audio(text_to_speak, cache_file)

                if audio_data is None:
                    logger.error("Failed to generate audio")
                    if callback:
                        callback(None)
                    return

                # Play audio
                from .player import AudioPlayer
                player = AudioPlayer()
                success = player.play_audio(audio_data)

                if callback:
                    callback(success)

            except Exception as e:
                logger.error("Audio playback failed: %s", e)
                if callback:
                    callback(False)

        # Run in thread to not block UI
        self._stop_requested = False
        self._current_task = threading.Thread(target=_play, daemon=True)
        self._current_task.start()

    def _generate_or_get_audio(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Generate TTS audio or return cached version. Falls back to pyttsx3 on API failure."""
        # Check cache
        if cache_file.exists():
            logger.debug("Using cached audio: %s", cache_file.name)
            return cache_file.read_bytes()

        try:
            with self._lock:
                request = TTSRequest(
                    text=text,
                    model_id=self._voice_model,
                    format="mp3",
                    mp3_bitrate=128,
                    latency="balanced",
                )

                # Generate audio
                audio_generator = self._session.tts(request)
                audio_data = b"".join(audio_generator)

            if not audio_data:
                logger.error("Empty audio generated")
                return None

            # Cache the result
            cache_file.write_bytes(audio_data)
            logger.info("Generated and cached %d bytes for: %s", len(audio_data), text[:30])
            return audio_data

        except Exception as e:
            logger.warning("Fish Audio API failed (%s), falling back to pyttsx3", e)
            return self._fallback_generate_audio(text, cache_file)

    def _fallback_generate_audio(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Fallback TTS using edge-tts (Japanese) or pyttsx3 (English).

        edge-tts provides high-quality neural Japanese voices (free, no API key).
        pyttsx3 handles English as a local fallback.
        """
        import asyncio

        def _is_japanese(text: str) -> bool:
            """Check if text contains Japanese characters (hiragana, katakana, kanji, half-width)."""
            for ch in text:
                cp = ord(ch)
                if (0x3040 <= cp <= 0x30FF) or (0xFF00 <= cp <= 0xFF9F) or (0x4E00 <= cp <= 0x9FFF):
                    return True
            return False

        if _is_japanese(text):
            return self._fallback_generate_edge_tts(text, cache_file)
        else:
            return self._fallback_generate_pyttsx3(text, cache_file)

    def _fallback_generate_pyttsx3(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Fallback TTS for English using local pyttsx3 engine."""
        try:
            import pyttsx3
            temp_path = str(self._cache_dir / f"_pyttsx_{hash(text) & 0xFFFFFFFF:08x}.wav")

            engine = pyttsx3.init()
            # Try to select a good voice
            voices = engine.getProperty("voices")
            if voices:
                # Prefer male voice for Great Sage persona
                for voice in voices:
                    if "male" in voice.name.lower() or "david" in voice.name.lower():
                        engine.setProperty("voice", voice.id)
                        break

            engine.setProperty("rate", 150)
            engine.save_to_file(text, temp_path)
            engine.runAndWait()

            if os.path.exists(temp_path):
                audio_data = Path(temp_path).read_bytes()
                if len(audio_data) < 1000:
                    logger.warning("pyttsx3 generated suspiciously small file (%d bytes)", len(audio_data))
                    os.unlink(temp_path)
                    return None
                cache_file.write_bytes(audio_data)
                logger.info("Fallback pyttsx3 generated %d bytes for: %s", len(audio_data), text[:30])
                return audio_data
            return None

        except Exception as e:
            logger.error("pyttsx3 fallback failed: %s", e)
            return None

    def _fallback_generate_edge_tts(self, text: str, cache_file: Path) -> Optional[bytes]:
        """Fallback TTS for Japanese using edge-tts (free, neural voices)."""
        try:
            import edge_tts

            temp_path = str(self._cache_dir / f"_edge_{hash(text) & 0xFFFFFFFF:08x}.mp3")

            # Use KeitaNeural (male) for Great Sage persona
            voice = "ja-JP-KeitaNeural"

            comm = edge_tts.Communicate(text, voice)
            asyncio.run(comm.save(temp_path))

            if os.path.exists(temp_path):
                audio_data = Path(temp_path).read_bytes()
                if len(audio_data) < 500:
                    logger.warning("edge-tts generated suspiciously small file (%d bytes)", len(audio_data))
                    os.unlink(temp_path)
                    return None
                cache_file.write_bytes(audio_data)
                logger.info("Fallback edge-tts generated %d bytes for: %s", len(audio_data), text[:30])
                return audio_data
            return None

        except ImportError:
            logger.error("edge-tts not installed — cannot generate Japanese TTS")
            return None
        except Exception as e:
            logger.error("edge-tts fallback failed: %s", e)
            return None

    def stop(self) -> None:
        """Stop current playback."""
        self._stop_requested = True
        from .player import AudioPlayer
        AudioPlayer.stop()
        if self._current_task:
            self._current_task.join(timeout=1.0)
            self._current_task = None

    def get_duration_estimate(self, text: str) -> int:
        """
        Estimate audio duration in ms based on text length.
        Fish Audio speaks at roughly 150-180 WPM.
        """
        word_count = len(text.split())
        # Faster than TTS because Fish Audio is more natural
        duration_ms = max(int((word_count / 160.0) * 60.0 * 1000), 3000)
        return duration_ms


class TTSRequestThread(QThread):
    """Thread for async TTS requests (Qt-compatible wrapper)."""

    finished = pyqtSignal(object)  # (success: bool, duration_ms: int)
    error = pyqtSignal(str)

    def __init__(self, tts: FishAudioTTS, text: str, callback: Optional[callable] = None):
        super().__init__()
        self._tts = tts
        self._text = text
        self._callback = callback

    def run(self):
        try:
            duration_ms = self._tts.get_duration_estimate(self._text)
            self._tts.speak(self._text)
            self.finished.emit((True, duration_ms))
            if self._callback:
                self._callback(True, duration_ms)
        except Exception as e:
            self.error.emit(str(e))
            if self._callback:
                self._callback(False, 0)
