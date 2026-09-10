"""
CoreControl — Great Sage Desktop Companion
Main entry point: starts the PyQt6 overlay, voice recorder, and orchestrator.

Usage:
    python main.py

Controls:
    Ctrl+Space  — activate voice capture (hold to record, release to transcribe)
    Right-click — activate voice capture (alternative)
    Drag        — reposition the companion
    Close       — click the X or press Escape to quit
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path
import webbrowser

# ── Unicode codec patch — Windows console can't print emoji with cp1252 ──────
try:
    codecs.register(lambda name: codecs.getencoder("utf-8") if name == "cp65001" else None)
except Exception:
    pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("corecontrol")

# ── Settings ──────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "config" / "settings.json"


def _load_settings() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("Cannot load settings.json: %s", exc)
        return {}


_settings = _load_settings()

# ── Message Logger ────────────────────────────────────────────────────────────
from src.gui.message_logger import MessageLogger, get_html_path

_msg_log = MessageLogger()
_msg_log.reset()  # clear on every bot start


def _deduplicate_text(text: str) -> str:
    """Strip consecutive duplicate phrases from LLM output.

    Handles punctuation and case differences, e.g.
    'Good job, good job.'              →  'Good job.'
    'I'm sorry, I'm sorry, I'm sorry.' →  'I'm sorry.'
    'The the quick brown fox'          →  'The quick brown fox'
    """
    import re
    if not text:
        return text

    def _norm(s: str) -> str:
        return re.sub(r"[^\w\s]", "", s).strip().lower()

    # Split into alternating text/delimiter chunks:
    #   'Good job, good job.' → ['Good job', ', ', 'good job', '.']
    chunks = re.findall(r"[^,.;!?]+|[,.;!?]+\s*", text)

    result_chunks: list[str] = []
    prev_norm = ""
    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            result_chunks.append(chunk)
            continue
        n = _norm(stripped)
        if not n:
            # Pure-punctuation chunk — append but don't touch prev_norm
            result_chunks.append(chunk)
            continue
        if n == prev_norm:
            # Duplicate clause — skip it AND its preceding delimiter
            if result_chunks and re.match(r"^[,.;!?]+\s*$", result_chunks[-1]):
                result_chunks.pop()
            continue
        result_chunks.append(chunk)
        prev_norm = n

    result = "".join(result_chunks).strip()

    # Fallback: if no punctuation was present at all, dedup consecutive words
    if not re.search(r"[,.;!?]", text):
        words = result.split()
        if len(words) > 1:
            w_result = [words[0]]
            w_prev = _norm(words[0])
            for w in words[1:]:
                wn = _norm(w)
                if wn == w_prev:
                    continue
                w_result.append(w)
                w_prev = wn
            result = " ".join(w_result)

    return result

# ── Hotkey listener (background thread) ──────────────────────────────────────

class HotkeyTrigger:
    """
    Listens for Ctrl+Space globally and emits a callback when triggered.
    Uses pynput for cross-platform hotkey detection.
    """

    def __init__(self, on_trigger: callable) -> None:
        self._on_trigger = on_trigger
        self._listener = None
        self._thread: threading.Thread | None = None
        self._keyboard = None

    def start(self) -> None:
        """Start the hotkey listener in a background thread."""
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed — hotkey trigger disabled. Run: pip install pynput")
            return

        self._keyboard = keyboard
        self._pressed = set()

        def _on_press(key):
            try:
                self._pressed.add(key)
            except TypeError:
                pass
            if self._is_triggered():
                self._on_trigger()

        def _on_release(key):
            try:
                self._pressed.discard(key)
            except TypeError:
                pass

        self._listener = keyboard.Listener(
            on_press=_on_press,
            on_release=_on_release,
        )
        self._thread = threading.Thread(target=self._listener.start, daemon=True, name="hotkey")
        self._thread.start()
        logger.info("Hotkey listener started (Ctrl+Space)")

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()

    def _is_triggered(self) -> bool:
        """Check if Ctrl+Space is currently held."""
        ctrl_down = any(
            isinstance(k, self._keyboard.KeyCode) and k.vk == 17 or
            isinstance(k, self._keyboard.Key) and k == self._keyboard.Key.ctrl_l
            for k in self._pressed
        )
        space_down = any(
            (isinstance(k, self._keyboard.KeyCode) and k.vk == 32) or
            (isinstance(k, self._keyboard.Key) and k == self._keyboard.Key.space)
            for k in self._pressed
        )
        return ctrl_down and space_down


# ── Main ──────────────────────────────────────────────────────────────────────

_main_loop: asyncio.AbstractEventLoop | None = None


def _run_overlay_thread(
    orch_handle: list,
    orch_ready: threading.Event,
    overlay_cfg: dict,
) -> None:
    """
    Run the PyQt6 overlay in its own thread.
    Manages the full application lifecycle.
    """
    try:
        from PyQt6.QtWidgets import QApplication
        from src.gui.overlay import OverlayWidget, NPCState, HitlRequest
        from src.gui.audio_recorder import AudioRecorder

        x = overlay_cfg.get("position", {}).get("x", 100)
        y = overlay_cfg.get("position", {}).get("y", 100)

        app, overlay = None, None

        def _startup() -> None:
            nonlocal app, overlay
            app = QApplication.instance() or QApplication(sys.argv)
            overlay = OverlayWidget(initial_x=x, initial_y=y)
            overlay.show()

        def _process_prompt(text: str) -> None:
            """Handle voice transcription result."""
            if not text or not text.strip():
                return
            orch = orch_handle[0]
            if orch is None:
                logger.warning("Orchestrator not ready yet, dropping transcription")
                return
            if not _main_loop or not _main_loop.is_running():
                logger.warning("Main event loop not running, dropping transcription")
                return

            _msg_log.log_user(text)

            overlay.set_state(NPCState.PROCESSING)
            overlay.speak("Analysis in progress…", duration_ms=0)

            coro = orch.process_prompt(text)
            future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
            def _on_result(_fut):
                try:
                    result = _fut.result()
                    clean = _deduplicate_text(result.text)
                    logger.info("Orchestrator result text (first 80 chars): %r", clean[:80])
                    _msg_log.log_assistant(clean)
                    overlay.response_received.emit(clean)
                except Exception as exc:
                    logger.error("Voice result callback error: %s", exc, exc_info=True)
            future.add_done_callback(_on_result)

        def _handle_hotkey() -> None:
            """Triggered when Ctrl+Space is pressed."""
            overlay.set_state(NPCState.LISTENING)

        # Start Qt app
        _startup()

        # Start audio recorder (Whisper runs in a subprocess to avoid CTranslate2 segfault)
        recorder = AudioRecorder(on_transcription=_process_prompt)
        recorder.start()

        # Connect overlay signals
        overlay.transcription_requested.connect(lambda: overlay.set_state(NPCState.LISTENING))
        def _on_prompt(text: str) -> None:
            if not text or not text.strip():
                return
            orch = orch_handle[0]
            if orch is None:
                logger.warning("Orchestrator not ready, dropping prompt")
                return
            _msg_log.log_user(text)

            overlay.set_state(NPCState.PROCESSING)
            overlay.speak("Analysis in progress…", duration_ms=0)

            coro = orch.process_prompt(text)
            future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
            def _on_result(_fut):
                try:
                    result = _fut.result()
                    clean = _deduplicate_text(result.text)
                    logger.info("Text prompt result (first 80 chars): %r", clean[:80])
                    _msg_log.log_assistant(clean)
                    overlay.response_received.emit(clean)
                except Exception as exc:
                    logger.error("Text prompt result callback error: %s", exc, exc_info=True)
            future.add_done_callback(_on_result)

        overlay.prompt_submitted.connect(_on_prompt)
        overlay.response_received.connect(overlay.speak)

        # Mute mic while bot is speaking/processing to prevent feedback loop
        def _on_state_changed(state: str) -> None:
            should_mute = state in ("processing", "speaking")
            recorder.set_mic_muted(should_mute)
        overlay.state_changed.connect(_on_state_changed)

        # Connect HITL signal
        def _on_hitl_request(request: HitlRequest) -> None:
            orch = orch_handle[0]
            if orch is None:
                logger.warning("Orchestrator not ready, dropping HITL request")
                return
            overlay.request_hitl(request, lambda result: orch.handle_hitl_response(
                request.request_id, result.get("approved", False), result.get("args")
            ))
        overlay.hitl_requested.connect(_on_hitl_request)

        # Offline mode toggle — passes through to orchestrator
        def _on_force_offline_toggled(enabled: bool) -> None:
            orch = orch_handle[0]
            if orch is not None:
                orch.set_force_offline(enabled)
        overlay.force_offline_toggled.connect(_on_force_offline_toggled)

        # Cutscene on skill execution
        from src.gui.cutscene import get_cutscene
        _cutscene_cfg = _settings.get("cutscene", {})
        _cutscene_sound = _cutscene_cfg.get("sound_path")
        def _on_cutscene() -> None:
            if _cutscene_cfg.get("enabled", True):
                get_cutscene().play(_cutscene_sound)
        overlay.cutscene_triggered.connect(_on_cutscene)

        # Hotkey trigger
        hotkey = HotkeyTrigger(on_trigger=_handle_hotkey)
        hotkey.start()

        # Graceful shutdown on close
        def _on_close() -> None:
            logger.info("Shutting down…")
            recorder.stop()
            hotkey.stop()
            if _main_loop and _main_loop.is_running():
                _main_loop.call_soon_threadsafe(_main_loop.stop)

        overlay.app_closing.connect(_on_close)

        # Wait until orchestrator is ready (or timeout)
        orch_ready.wait(timeout=10)

        # Run Qt event loop
        ret = app.exec()

        # Cleanup
        recorder.stop()
        hotkey.stop()
        logger.info("Overlay thread exited with code %d", ret)

    except ImportError as exc:
        logger.error("Qt import failed: %s", exc)
    except Exception as exc:
        logger.error("Overlay thread error: %s", exc, exc_info=True)


def main() -> None:
    global _main_loop

    overlay_cfg = _settings.get("overlay", {})

    # Shared orchestrator handle — set by async main, read by overlay thread.
    # Using a plain list avoids asyncio.Future() (needs a running loop).
    _orch_handle: list = [None]  # _orch_handle[0] = Orchestrator instance
    _orch_ready = threading.Event()

    overlay_thread = threading.Thread(
        target=_run_overlay_thread,
        args=(_orch_handle, _orch_ready, overlay_cfg),
        daemon=True,
        name="overlay",
    )
    overlay_thread.start()

    # Give Qt a moment to initialise
    time.sleep(0.5)

    # Run the async orchestrator in the main thread
    async def _async_main() -> None:
        global _main_loop
        _main_loop = asyncio.get_running_loop()

        from src.orchestrator import Orchestrator

        orch = Orchestrator(on_cutscene=lambda: overlay.cutscene_triggered.emit())
        _orch_handle[0] = orch
        _orch_ready.set()  # signal overlay thread that orchestrator is ready
        await orch.start()

        # Keep the event loop alive until the overlay thread signals shutdown
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Received shutdown signal")
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                _main_loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                signal.signal(sig, lambda s, f: _signal_handler())

        logger.info("CoreControl Great Sage ready. Press Ctrl+Space to activate voice.")
        logger.info("Close the overlay window to exit.")

        # Open message log in browser
        log_url = get_html_path().resolve().as_uri()
        webbrowser.open(log_url)

        try:
            await shutdown_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            logger.info("Shutting down orchestrator…")
            await orch.stop()
            logger.info("CoreControl stopped.")

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
