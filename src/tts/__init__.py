"""Great Sage TTS — Local TTS (Edge-TTS + pyttsx3 fallback) with Japanese support."""
from .local_tts import LocalTTS, SUPPORTED_LANGUAGES, _estimate_duration_ms
from .player import AudioPlayer

__all__ = ["LocalTTS", "AudioPlayer", "SUPPORTED_LANGUAGES", "_estimate_duration_ms"]
