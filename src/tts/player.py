"""Audio player using QtMultimedia for MP3/WAV playback."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

logger = logging.getLogger(__name__)

# Singleton player instance (Qt requires one per thread)
_player: Optional[QMediaPlayer] = None
_output: Optional[QAudioOutput] = None


def get_player() -> tuple[QMediaPlayer, QAudioOutput]:
    """Get or create the singleton player instance."""
    global _player, _output
    if _player is None:
        _player = QMediaPlayer()
        _output = QAudioOutput()
        _player.setAudioOutput(_output)
        _player.playbackStateChanged.connect(_on_playback_state)
    return _player, _output


def _on_playback_state(state):
    """Log playback state changes."""
    from PyQt6.QtMultimedia import QMediaPlayer
    states = {
        QMediaPlayer.PlaybackState.StoppedState: "stopped",
        QMediaPlayer.PlaybackState.PlayingState: "playing",
        QMediaPlayer.PlaybackState.PausedState: "paused",
    }
    logger.debug("Audio playback: %s", states.get(state, str(state)))


class AudioPlayer:
    """Singleton audio player for MP3/WAV playback."""

    def __init__(self):
        self._player, self._output = get_player()
        self._is_playing = False

    def play_audio(self, audio_data: bytes) -> bool:
        """
        Play audio from bytes (MP3 or WAV).
        Tries QtMultimedia first, falls back to pygame if Qt backend is unavailable.
        Returns True if playback started successfully.
        """
        # Write to temp file (needed by both Qt and pygame)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        # Try QtMultimedia first
        try:
            self._player.setSource(QUrl.fromLocalFile(temp_path))
            self._output.setVolume(0.8)  # 80% volume
            self._player.play()
            self._is_playing = True

            # Clean up temp file after playback
            def cleanup():
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass

            self._player.mediaStatusChanged.connect(
                lambda status: cleanup() if status == QMediaPlayer.MediaStatus.EndOfMedia else None
            )

            logger.debug("Playing audio via QtMultimedia: %d bytes", len(audio_data))
            return True

        except Exception as qt_err:
            logger.warning("QtMultimedia failed (%s), falling back to pygame", qt_err)

        # Fallback: pygame (works without Qt multimedia backend)
        try:
            import pygame
            pygame.mixer.init()
            pygame.mixer.music.load(temp_path)
            pygame.mixer.music.set_volume(0.8)
            pygame.mixer.music.play()
            self._is_playing = True
            logger.debug("Playing audio via pygame: %d bytes", len(audio_data))

            # Clean up after playback
            def cleanup():
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass

            # Schedule cleanup ~2s after play starts (approx duration)
            import threading
            threading.Timer(2.0, cleanup).start()
            return True

        except Exception as e:
            logger.error("Failed to play audio: %s", e)
            self._is_playing = False
            # Cleanup temp file on failure
            Path(temp_path).unlink(missing_ok=True)
            return False

    def stop(self) -> None:
        """Stop current playback."""
        try:
            self._player.stop()
            self._is_playing = False
            logger.debug("Audio playback stopped")
        except Exception as e:
            logger.warning("Error stopping audio: %s", e)

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @classmethod
    def stop_all(cls) -> None:
        """Class method to stop all playback (singleton pattern)."""
        global _player
        if _player:
            _player.stop()


def play_audio_from_file(filepath: str) -> bool:
    """Play audio from a file path."""
    player, output = get_player()
    try:
        player.setSource(QUrl.fromLocalFile(filepath))
        output.setVolume(0.8)
        player.play()
        return True
    except Exception as e:
        logger.error("Failed to play %s: %s", filepath, e)
        return False
