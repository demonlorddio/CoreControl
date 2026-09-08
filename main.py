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
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

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

    def start(self) -> None:
        """Start the hotkey listener in a background thread."""
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed — hotkey trigger disabled. Run: pip install pynput")
            return

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
            isinstance(k, keyboard.KeyCode) and k.vk == 17 or
            isinstance(k, keyboard.Key) and k == keyboard.Key.ctrl_l
            for k in self._pressed
        )
        space_down = any(
            (isinstance(k, keyboard.KeyCode) and k.vk == 32) or
            (isinstance(k, keyboard.Key) and k == keyboard.Key.space)
            for k in self._pressed
        )
        return ctrl_down and space_down


# ── Qt Overlay Thread ─────────────────────────────────────────────────────────

def _run_overlay_thread(
    orchestrator_future: asyncio.Future,
    overlay_cfg: dict,
) -> None:
    """
    Run the PyQt6 overlay in its own thread.
    Manages the full application lifecycle.
    """
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTimer
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
            overlay.set_state(NPCState.PROCESSING)
            overlay.speak("Analysis in progress…", duration_ms=0)

            async def _run():
                orch = orchestrator_future.result()
                result = await orch.process_prompt(text)
                logger.info("Great Sage response: %s", result.text[:150])
                overlay.set_state(NPCState.SPEAKING)
                overlay.speak(result.text)
                overlay.set_state(NPCState.IDLE)

            asyncio.run_coroutine_threadsafe(_run(), _main_loop)

        def _handle_hotkey() -> None:
            """Triggered when Ctrl+Space is pressed."""
            overlay.set_state(NPCState.LISTENING)

        # Start Qt app
        _startup()

        # Start audio recorder
        recorder = AudioRecorder(on_transcription=_process_prompt)
        recorder.start()

        # Connect overlay signals
        overlay.transcription_requested.connect(lambda: overlay.set_state(NPCState.LISTENING))

        # Connect HITL signal
        def _on_hitl_request(request: HitlRequest) -> None:
            orch = orchestrator_future.result()
            overlay.request_hitl(request, lambda result: orch.handle_hitl_response(
                request.request_id, result.get("approved", False), result.get("args")
            ))
        overlay.hitl_requested.connect(_on_hitl_request)

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


# ── Main ──────────────────────────────────────────────────────────────────────

_main_loop: asyncio.AbstractEventLoop | None = None


def main() -> None:
    global _main_loop

    overlay_cfg = _settings.get("overlay", {})

    # Start Qt overlay thread FIRST (Qt must be in the main thread)
    orchestrator_future: asyncio.Future = asyncio.get_event_loop().create_future()

    overlay_thread = threading.Thread(
        target=_run_overlay_thread,
        args=(orchestrator_future, overlay_cfg),
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

        orch = Orchestrator()
        orchestrator_future.set_result(orch)
        await orch.start()

        # Keep the event loop alive until the overlay thread signals shutdown
        shutdown_event = asyncio.Event()

        def _check_shutdown() -> None:
            if not overlay_thread.is_alive():
                shutdown_event.set()

        # Handle signals for clean shutdown
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
