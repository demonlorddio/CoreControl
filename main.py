"""
CoreControl — AI Desktop Assistant
Main entry point: starts the Telegram gateway, audio recorder,
PyQt6 overlay, and orchestrator together.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
from pathlib import Path

# ── Logging (stderr only) ─────────────────────────────────────────────────────
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


# ── Qt overlay (optional — skipped if PyQt6 unavailable) ─────────────────────

def _start_overlay(orchestrator) -> None:
    """Run the PyQt6 overlay in its own thread."""
    try:
        from PyQt6.QtWidgets import QApplication
        from src.gui.overlay import create_app_and_overlay
        from src.gui.audio_recorder import AudioRecorder

        overlay_cfg = _settings.get("overlay", {})
        x = overlay_cfg.get("position", {}).get("x", 100)
        y = overlay_cfg.get("position", {}).get("y", 100)

        qt_app, overlay = create_app_and_overlay(x=x, y=y)
        overlay.show()

        def on_transcription(text: str) -> None:
            overlay.set_processing(True)
            overlay.set_status("⚙ Processing…")

            async def _dispatch():
                response = await orchestrator.process_prompt(text)
                logger.info("Voice response: %r", response[:120])
                overlay.set_processing(False)
                overlay.set_status("✅ Done", duration_ms=2000)

            asyncio.run_coroutine_threadsafe(_dispatch(), _main_loop)

        recorder = AudioRecorder(on_transcription=on_transcription)

        def on_recording_state(active: bool) -> None:
            recorder.is_recording = active

        overlay.recording_state_changed.connect(on_recording_state)
        recorder.start()

        qt_app.exec()
        recorder.stop()

    except ImportError as exc:
        logger.warning("PyQt6 overlay skipped: %s", exc)
    except Exception as exc:
        logger.error("Overlay error: %s", exc)


# ── Telegram gateway (optional) ───────────────────────────────────────────────

async def _start_telegram(orchestrator) -> None:
    tg_cfg = _settings.get("telegram", {})
    token = tg_cfg.get("bot_token", "")

    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.warning(
            "Telegram bot token not configured — gateway disabled. "
            "Set telegram.bot_token in config/settings.json."
        )
        return

    try:
        from src.bridge.telegram_bot import TelegramGateway

        async def handle_prompt(prompt: str, user_id: int) -> str:
            return await orchestrator.process_prompt(prompt, user_id=user_id)

        gateway = TelegramGateway(on_prompt=handle_prompt)
        # Attach gateway to orchestrator's HITL filter
        orchestrator._hitl._gateway = gateway
        await gateway.start()
        logger.info("Telegram gateway active")
        return gateway
    except Exception as exc:
        logger.error("Telegram gateway failed to start: %s", exc)
        return None


# ── Main async loop ───────────────────────────────────────────────────────────

_main_loop: asyncio.AbstractEventLoop | None = None


async def _async_main() -> None:
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    from src.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    await orchestrator.start()

    gateway = await _start_telegram(orchestrator)

    # Handle SIGINT / SIGTERM for clean shutdown
    stop_event = asyncio.Event()

    def _signal_handler(*_) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            _main_loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows does not support add_signal_handler for all signals
            signal.signal(sig, _signal_handler)

    logger.info("CoreControl ready. Press Ctrl+C to stop.")

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down…")
        if gateway:
            try:
                await gateway.stop()
            except Exception as exc:
                logger.warning("Gateway shutdown error: %s", exc)
        await orchestrator.stop()
        logger.info("CoreControl stopped.")


def main() -> None:
    # Start PyQt6 overlay in a background thread (Qt must own its thread)
    # We create the orchestrator inside the async loop, but we need the
    # overlay thread to reference it — use a threading.Event as a handshake.
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # Also spin up the overlay if Qt is available — do it in a daemon thread
    # so the process exits when the async loop finishes.
    from src.orchestrator import Orchestrator

    _orch_ref: list[Orchestrator] = []

    original_async_main = _async_main

    async def _patched_async_main() -> None:
        from src.orchestrator import Orchestrator as _Orch
        orch = _Orch()
        _orch_ref.append(orch)
        # Give overlay thread a moment to start
        await asyncio.sleep(0.2)
        await original_async_main()

    overlay_thread = threading.Thread(
        target=lambda: _start_overlay(_orch_ref[0]) if _orch_ref else None,
        daemon=True,
        name="overlay",
    )
    # Start overlay thread after a short delay so the orchestrator is ready
    threading.Timer(1.0, lambda: overlay_thread.start()).start()

    main()
