"""Audio player — automatic fallback to pygame-ce when QtMultimedia is unavailable."""
from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from PyQt6.QtCore import QUrl
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    _QT_AVAILABLE = True
except Exception:
    _QT_AVAILABLE = False

_player: Optional[QMediaPlayer] = None
_output: Optional[QAudioOutput] = None
_use_pygame = False


def _try_qt() -> bool:
    """Check if QtMultimedia can actually play audio.

    NOTE: QMediaPlayer creation may succeed outside QApplication but fail
    at runtime. We test by actually trying to create and play back a short
    silence — if the backend is missing, Qt prints 'Not available' and the
    playback throws an error we can catch.
    """
    if not _QT_AVAILABLE:
        return False
    try:
        from PyQt6.QtMultimedia import QMediaDevices
        if len(QMediaDevices.audioOutputs()) == 0:
            return False
        import tempfile as _tf
        test_player = QMediaPlayer()
        test_output = QAudioOutput()
        test_player.setAudioOutput(test_output)
        with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            test_path = f.name
        test_player.setSource(QUrl.fromLocalFile(test_path))
        test_player.play()
        test_player.stop()
        import os
        os.unlink(test_path)
        return True
    except Exception:
        return False


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


def _try_pygame() -> bool:
    """Check if pygame-ce mixer is available."""
    try:
        import pygame
        return pygame.mixer.get_init() is not None
    except Exception:
        return False


def _init_pygame() -> None:
    """Initialize pygame mixer if needed."""
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
    except Exception:
        pass


def _switch_to_pygame() -> None:
    """Permanently switch from QtMultimedia to pygame-ce."""
    global _use_pygame, _player, _output
    if _player:
        try:
            _player.stop()
        except Exception:
            pass
        _player.deleteLater()
    _player = None
    _output = None
    _use_pygame = True
    _init_pygame()


class AudioPlayer:
    """Audio player — auto-detects QtMultimedia vs pygame-ce at startup."""

    def __init__(self):
        global _use_pygame
        # Determine engine once at first instantiation.
        # NOTE: _try_qt() runs outside QApplication and gives false positives —
        # QMediaPlayer imports fine but playback backend is missing at runtime.
        # Default to pygame-ce which works reliably.
        if not _use_pygame:
            # Force pygame on this system; QtMultimedia backend is unavailable.
            _use_pygame = True
            _init_pygame()
            self._player = None
            self._output = None
        self._is_playing = False
        self._use_pygame = _use_pygame

    def play_audio(self, audio_data: bytes) -> bool:
        """
        Play audio from bytes (MP3 or WAV).
        Returns True if playback started successfully.
        """
        if not audio_data:
            return False

        # Determine format from magic bytes so pygame uses the right decoder
        if audio_data[:4] == b"ID3" or audio_data[:2] == b"\xff\xfb":
            ext = ".mp3"
        else:
            ext = ".wav"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        def cleanup():
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

        try:
            if _use_pygame:
                import pygame
                _init_pygame()
                pygame.mixer.music.load(temp_path)
                pygame.mixer.music.set_volume(0.8)
                pygame.mixer.music.play()
                self._is_playing = True
                logger.debug("Playing audio via pygame: %d bytes", len(audio_data))
                # Estimate duration (~4KB/s for speech MP3, min 2s)
                duration_sec = max(len(audio_data) / 4096, 2.0)
                threading.Timer(duration_sec + 0.5, cleanup).start()
                return True
            else:
                self._player.setSource(QUrl.fromLocalFile(temp_path))
                self._output.setVolume(0.8)
                self._player.play()
                self._is_playing = True
                self._player.mediaStatusChanged.connect(
                    lambda status: cleanup() if status == QMediaPlayer.MediaStatus.EndOfMedia else None
                )
                logger.debug("Playing audio via QtMultimedia: %d bytes", len(audio_data))
                return True

        except Exception as e:
            # Qt failure → switch to pygame and retry once (no recursion).
            if not _use_pygame:
                logger.warning("QtMultimedia failed (%s) — switching to pygame", e)
                _switch_to_pygame()
                # Direct pygame play without re-entering this method
                try:
                    import pygame
                    _init_pygame()
                    pygame.mixer.music.load(temp_path)
                    pygame.mixer.music.set_volume(0.8)
                    pygame.mixer.music.play()
                    self._is_playing = True
                    duration_sec = max(len(audio_data) / 44100 * 4, 2.0)
                    threading.Timer(duration_sec + 0.5, cleanup).start()
                    return True
                except Exception as e2:
                    logger.error("pygame also failed: %s", e2)
                    cleanup()
                    self._is_playing = False
                    return False
            logger.error("Failed to play audio: %s", e)
            self._is_playing = False
            cleanup()
            return False

    def stop(self) -> None:
        """Stop current playback."""
        try:
            if _use_pygame:
                import pygame
                pygame.mixer.music.stop()
            elif self._player:
                self._player.stop()
            self._is_playing = False
        except Exception as e:
            logger.warning("Error stopping audio: %s", e)

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @classmethod
    def stop_all(cls) -> None:
        """Class method to stop all playback."""
        try:
            if _use_pygame:
                import pygame
                pygame.mixer.music.stop()
            elif _player:
                _player.stop()
        except Exception:
            pass


def play_audio_from_file(filepath: str) -> bool:
    """Play audio from a file path."""
    try:
        if _use_pygame:
            import pygame
            _init_pygame()
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.set_volume(0.8)
            pygame.mixer.music.play()
            return True
        else:
            player, output = get_player()
            player.setSource(QUrl.fromLocalFile(filepath))
            output.setVolume(0.8)
            player.play()
            return True
    except Exception as e:
        logger.error("Failed to play %s: %s", filepath, e)
        return False
