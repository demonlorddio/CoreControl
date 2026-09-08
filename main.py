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

            overlay.set_state(NPCState.PROCESSING)
            overlay.speak("Analysis in progress…", duration_ms=0)

            coro = orch.process_prompt(text)
            asyncio.run_coroutine_threadsafe(coro, _main_loop)

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
            orch = orch_handle[0]
            if orch is None:
                logger.warning("Orchestrator not ready, dropping HITL request")
                return
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

        orch = Orchestrator()
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
