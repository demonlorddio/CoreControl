"""
Telegram bot gateway for CoreControl.
Handles remote command reception and Human-In-The-Loop (HITL) confirmation middleware.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Logging: stderr only ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Settings ─────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "settings.json"


def _load_settings() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("Cannot load settings.json: %s", exc)
        return {}


_settings = _load_settings()
_tg_cfg = _settings.get("telegram", {})
BOT_TOKEN: str = _tg_cfg.get("bot_token", "")
WHITELISTED_IDS: set[int] = set(int(x) for x in _tg_cfg.get("whitelisted_user_ids", []))
HITL_TIMEOUT: int = _tg_cfg.get("hitl_timeout_seconds", 300)


# ── HITL data structures ──────────────────────────────────────────────────────

class HITLDecision(Enum):
    APPROVE = "approve"
    EDIT = "edit"
    DENY = "deny"


@dataclass
class HITLRequest:
    """Encapsulates a pending high-risk action awaiting user decision."""
    request_id: str
    tool_name: str
    tool_args: dict
    description: str
    screenshot_b64: Optional[str] = None
    future: asyncio.Future = field(default_factory=asyncio.Future)


@dataclass
class HITLResult:
    decision: HITLDecision
    modified_args: Optional[dict] = None  # Set when decision == EDIT
    reason: Optional[str] = None


# ── Bot singleton ─────────────────────────────────────────────────────────────

class TelegramGateway:
    """
    Multi-modal Telegram gateway with HITL middleware.

    Usage:
        gateway = TelegramGateway(on_prompt=my_handler)
        await gateway.start()
        ...
        await gateway.stop()
    """

    def __init__(
        self,
        on_prompt: Callable[[str, int], Coroutine[Any, Any, str]],
        bot_token: str = BOT_TOKEN,
        whitelisted_ids: set[int] = WHITELISTED_IDS,
    ) -> None:
        if not bot_token:
            raise ValueError(
                "Telegram bot token is not configured. "
                "Set telegram.bot_token in config/settings.json."
            )

        self._on_prompt = on_prompt
        self._bot_token = bot_token
        self._whitelist = whitelisted_ids
        self._app: Optional[Application] = None
        self._hitl_pending: dict[str, HITLRequest] = {}
        self._edit_awaiting: dict[int, str] = {}  # chat_id → request_id

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _is_authorised(self, user_id: int) -> bool:
        if not self._whitelist:
            logger.warning("Whitelist is empty — all users allowed (dev mode)")
            return True
        return user_id in self._whitelist

    async def _send_reply(self, chat_id: int, text: str) -> None:
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.error("Failed to send message to %d: %s", chat_id, exc)

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _send_text_with_attachments(self, chat_id: int, text: str,
                                          screenshots: list[bytes] = None,
                                          web_links: list[str] = None) -> None:
        """Send text response along with any screenshots or links."""
        if screenshots is None:
            screenshots = []
        if web_links is None:
            web_links = []

        # Send screenshots first
        for i, img_bytes in enumerate(screenshots):
            try:
                caption = text if i == 0 and text else None
                await self._app.bot.send_photo(
                    chat_id=chat_id,
                    photo=io.BytesIO(img_bytes),
                    caption=caption,
                )
            except Exception as exc:
                logger.error("Failed to send screenshot: %s", exc)

        # Send text if not already sent with screenshots
        if not screenshots and text:
            await self._send_reply(chat_id, text)
        elif screenshots and text and not any(screenshots):
            # Text already sent with first screenshot
            pass

        # Send web links separately
        if web_links:
            link_text = "🔗 Links found:\n" + "\n".join(f"<a href='{l}'>{l}</a>" for l in web_links)
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=link_text,
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.error("Failed to send links: %s", exc)

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self._is_authorised(user.id):
            logger.warning("Blocked unauthorised user %d (%s)", user.id, user.username)
            return

        # If the user is in the "edit" flow, treat the text as modified args
        if user.id in self._edit_awaiting:
            await self._handle_edit_response(update, context)
            return

        prompt = update.message.text.strip()
        logger.info("Text prompt from user %d: %r", user.id, prompt[:120])

        await self._send_reply(chat_id, "⏳ Processing your request…")
        try:
            from ..orchestrator import ProcessResult
            result = await self._on_prompt(prompt, user.id)

            if isinstance(result, ProcessResult):
                await self._send_text_with_attachments(
                    chat_id, result.text, result.screenshots, result.web_links
                )
            else:
                # Backward compatibility for old-style string returns
                await self._send_reply(chat_id, result or "✅ Done.")
        except Exception as exc:
            logger.exception("Error processing prompt")
            await self._send_reply(chat_id, f"❌ Error: {exc}")

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorised(user.id):
            return

        photo = update.message.photo[-1]  # highest resolution
        file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        import base64
        b64 = base64.b64encode(buf.read()).decode("ascii")

        caption = update.message.caption or "Analyse this screenshot and describe what you see."
        prompt = f"{caption}\n[IMAGE_BASE64:{b64[:50]}…]"

        logger.info("Photo from user %d (caption: %r)", user.id, caption[:80])
        await self._send_reply(update.effective_chat.id, "⏳ Analysing image…")
        try:
            from ..orchestrator import ProcessResult
            result = await self._on_prompt(prompt, user.id)

            if isinstance(result, ProcessResult):
                await self._send_text_with_attachments(
                    update.effective_chat.id, result.text,
                    result.screenshots, result.web_links
                )
            else:
                await self._send_reply(update.effective_chat.id, result or "✅ Done.")
        except Exception as exc:
            logger.exception("Error processing image")
            await self._send_reply(update.effective_chat.id, f"❌ Error: {exc}")

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorised(user.id):
            return

        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)

        logger.info("Voice message from user %d (%ds)", user.id, voice.duration)
        await self._send_reply(update.effective_chat.id, "🎙️ Transcribing voice message…")

        try:
            transcript = await self._transcribe_ogg(buf.read())
            await self._send_reply(
                update.effective_chat.id,
                f"📝 Transcribed: _{transcript}_",
            )
            from ..orchestrator import ProcessResult
            result = await self._on_prompt(transcript, user.id)

            if isinstance(result, ProcessResult):
                await self._send_text_with_attachments(
                    update.effective_chat.id, result.text,
                    result.screenshots, result.web_links
                )
            else:
                await self._send_reply(update.effective_chat.id, result or "✅ Done.")
        except Exception as exc:
            logger.exception("Error processing voice")
            await self._send_reply(update.effective_chat.id, f"❌ Transcription error: {exc}")

    async def _transcribe_ogg(self, ogg_bytes: bytes) -> str:
        """Transcribe a Telegram OGG voice message using faster-whisper."""
        import tempfile
        from faster_whisper import WhisperModel

        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(ogg_bytes)
            tmp_path = tmp.name

        try:
            segments, _info = model.transcribe(tmp_path, beam_size=5)
            return " ".join(seg.text for seg in segments).strip()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _handle_command_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorised(user.id):
            return
        await self._send_reply(
            update.effective_chat.id,
            "👋 CoreControl online. Send me a command, image, or voice message.",
        )

    async def _handle_command_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not self._is_authorised(user.id):
            return
        pending = len(self._hitl_pending)
        await self._send_reply(
            update.effective_chat.id,
            f"✅ CoreControl is running.\n🔒 HITL queue: {pending} pending.",
        )

    # ── HITL machinery ────────────────────────────────────────────────────────

    async def request_hitl_approval(
        self,
        request_id: str,
        tool_name: str,
        tool_args: dict,
        description: str,
        screenshot_b64: Optional[str] = None,
    ) -> HITLResult:
        """
        Send a confirmation card to all whitelisted users and wait for a decision.
        Returns an HITLResult with the user's choice.
        Raises asyncio.TimeoutError if no decision is made within HITL_TIMEOUT seconds.
        """
        req = HITLRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description,
            screenshot_b64=screenshot_b64,
        )
        self._hitl_pending[request_id] = req

        args_preview = json.dumps(tool_args, indent=2)[:800]
        card_text = (
            f"🔐 *Action Requires Approval*\n\n"
            f"*Tool:* `{tool_name}`\n"
            f"*Description:* {description}\n\n"
            f"*Arguments:*\n```json\n{args_preview}\n```"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"hitl:approve:{request_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"hitl:edit:{request_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"hitl:deny:{request_id}"),
            ]
        ])

        for uid in self._whitelist:
            try:
                if screenshot_b64:
                    import base64
                    img_bytes = base64.b64decode(screenshot_b64)
                    await self._app.bot.send_photo(
                        chat_id=uid,
                        photo=io.BytesIO(img_bytes),
                        caption=card_text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                else:
                    await self._app.bot.send_message(
                        chat_id=uid,
                        text=card_text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.error("Could not send HITL card to user %d: %s", uid, exc)

        try:
            result: HITLResult = await asyncio.wait_for(req.future, timeout=HITL_TIMEOUT)
        except asyncio.TimeoutError:
            self._hitl_pending.pop(request_id, None)
            raise asyncio.TimeoutError(
                f"HITL approval for '{tool_name}' timed out after {HITL_TIMEOUT}s"
            )

        self._hitl_pending.pop(request_id, None)
        return result

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = query.from_user

        if not self._is_authorised(user.id):
            await query.answer("Not authorised.")
            return

        data: str = query.data
        if not data.startswith("hitl:"):
            return

        parts = data.split(":", 2)
        if len(parts) != 3:
            return

        _, action, request_id = parts
        req = self._hitl_pending.get(request_id)
        if req is None:
            await query.answer("Request already resolved or expired.")
            return

        if action == "approve":
            await query.answer("✅ Approved")
            await query.edit_message_reply_markup(reply_markup=None)
            if not req.future.done():
                req.future.set_result(HITLResult(decision=HITLDecision.APPROVE))

        elif action == "deny":
            await query.answer("❌ Denied")
            await query.edit_message_reply_markup(reply_markup=None)
            if not req.future.done():
                req.future.set_result(HITLResult(
                    decision=HITLDecision.DENY,
                    reason="User denied the action via Telegram.",
                ))

        elif action == "edit":
            await query.answer("✏️ Send the modified JSON args as a message.")
            self._edit_awaiting[user.id] = request_id
            await self._app.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "Send the modified arguments as a JSON object.\n\n"
                    f"Current args:\n```json\n{json.dumps(req.tool_args, indent=2)}\n```"
                ),
                parse_mode="Markdown",
            )

    async def _handle_edit_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        request_id = self._edit_awaiting.pop(user.id, None)
        if not request_id:
            return

        req = self._hitl_pending.get(request_id)
        if req is None:
            await self._send_reply(update.effective_chat.id, "⚠️ Request expired.")
            return

        try:
            modified_args = json.loads(update.message.text)
        except json.JSONDecodeError as exc:
            await self._send_reply(
                update.effective_chat.id,
                f"❌ Invalid JSON: {exc}\nPlease send valid JSON or /cancel.",
            )
            self._edit_awaiting[user.id] = request_id  # Re-enter edit mode
            return

        if not req.future.done():
            req.future.set_result(HITLResult(
                decision=HITLDecision.EDIT,
                modified_args=modified_args,
            ))
        await self._send_reply(update.effective_chat.id, "✅ Modified args accepted. Executing…")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )

        self._app.add_handler(CommandHandler("start", self._handle_command_start))
        self._app.add_handler(CommandHandler("status", self._handle_command_status))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram gateway polling started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram gateway stopped")
