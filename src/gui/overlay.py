"""
Great Sage NPC Overlay — CoreControl desktop companion.

A translucent, frameless, always-on-top widget that renders an animated
Great Sage avatar with a speech bubble and local HITL confirmation modals.

States:
    IDLE        — bouncing gently, random blink
    LISTENING   — pulsing blue core (mic hot)
    PROCESSING  — rapid core rotation / colour shift (LLM thinking)
    SPEAKING    — avatar animates, speech bubble visible

Interaction:
    Drag     — left-click and hold on the avatar to reposition
    Trigger  — Ctrl+Space (or click the avatar) starts voice capture
    Close    — right-click → quit
"""
from __future__ import annotations

import logging
import math
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Optional

from PyQt6.QtCore import (
    QPoint,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QMovie,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygon,
    QPixmap,
)
from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

logger = logging.getLogger(__name__)


# ── Enums & Data ──────────────────────────────────────────────────────────────

class NPCState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass
class HitlRequest:
    request_id: str
    tool_name: str
    tool_args: dict
    description: str
    screenshot_b64: Optional[str] = None


# ── Great Sage Avatar ─────────────────────────────────────────────────────────

class GreatSageAvatar(QWidget):
    """
    Animated Great Sage companion with GIF avatar and
    state-dependent glow effects layered on top.
    """

    _IMG_PATH = Path(__file__).resolve().parent.parent.parent / "great-sage-transparent-clean.gif"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Large avatar: 200×220 pixels
        self.setFixedSize(200, 220)

        self._state = NPCState.IDLE
        self._bounce_offset: float = 0.0
        self._is_blinking: bool = False
        self._core_hue: float = 200.0
        self._processing_spin: float = 0.0

        # Load animated GIF — scale to fit inside the 200×220 widget
        self._movie: Optional[QMovie] = None
        self._current_frame: QPixmap = QPixmap()
        gif_path = str(self._IMG_PATH)
        self._movie = QMovie(gif_path)
        if self._movie and self._movie.isValid():
            self._movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self._movie.frameChanged.connect(self._on_frame_changed)
            self._movie.start()
            first_frame = self._movie.currentPixmap()
            if not first_frame.isNull():
                self._current_frame = first_frame.scaled(
                    190, 210,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        else:
            logger.warning("Could not load GIF at %s — falling back to blank avatar", gif_path)

        # Animation timers
        self._bounce_timer = QTimer(self)
        self._bounce_timer.timeout.connect(self._tick_bounce)
        # Start disabled — enable only when needed
        # self._bounce_timer.start(50)

        self._processing_timer = QTimer(self)
        self._processing_timer.timeout.connect(self._tick_processing)
        self._processing_timer.start(30)

    @pyqtProperty(float)
    def bounce_offset(self) -> float:
        return self._bounce_offset

    @bounce_offset.setter  # type: ignore[no-redef]
    def bounce_offset(self, value: float) -> None:
        self._bounce_offset = value
        self.update()

    def _tick_bounce(self) -> None:
        self._bounce_offset = math.sin(self._bounce_offset + 0.08) * 5.0
        self.update()

    def _schedule_blink(self) -> None:
        if self._state == NPCState.PROCESSING:
            return
        self._is_blinking = True
        self.update()
        QTimer.singleShot(120, self._end_blink)

    def _end_blink(self) -> None:
        self._is_blinking = False
        self.update()

    def _tick_processing(self) -> None:
        if self._state == NPCState.PROCESSING:
            self._processing_spin = (self._processing_spin + 0.15) % (2 * math.pi)
            self._core_hue = 200.0 + math.sin(self._processing_spin) * 60.0
            self.update()
        elif self._state == NPCState.LISTENING:
            self._core_hue = 210.0 + math.sin(self._processing_spin) * 20.0
            self._processing_spin += 0.1
            self.update()
        else:
            # Idle: slow hue drift
            self._core_hue = 210.0 + math.sin(self._processing_spin * 0.3) * 15.0
            self._processing_spin += 0.05
            self.update()

    def set_state(self, state: NPCState) -> None:
        self._state = state
        self._processing_spin = 0.0
        if state in (NPCState.LISTENING, NPCState.PROCESSING):
            self._core_hue = 200.0
        self.update()

    def _on_frame_changed(self) -> None:
        """Update the current frame from the GIF animation."""
        pixmap = self._movie.currentPixmap() if self._movie else QPixmap()
        if not pixmap.isNull():
            self._current_frame = pixmap.scaled(
                190, 210,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.update()

    def set_bounce(self, enabled: bool) -> None:
        """Enable or disable the bounce animation."""
        if enabled:
            self._bounce_timer.start(50)
        else:
            self._bounce_timer.stop()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = 100, 110 + self._bounce_offset

        # ── Image only (no glow overlay to avoid GIF flickering) ─────────
        if not self._current_frame.isNull():
            img_rect = self._current_frame.rect()
            draw_x = int(cx - img_rect.width() / 2)
            draw_y = int(cy - img_rect.height() / 2)
            p.drawPixmap(draw_x, draw_y, self._current_frame)

        # ── State indicator dot ──────────────────────────────────────────
        body_bot = cy + 80
        indicator_y = body_bot + 8
        if self._state == NPCState.LISTENING:
            dot_color = QColor(80, 180, 255)
        elif self._state == NPCState.PROCESSING:
            dot_color = QColor(255, 200, 80)
        elif self._state == NPCState.SPEAKING:
            dot_color = QColor(100, 220, 180)
        else:
            dot_color = QColor(60, 120, 180)
        dot_color.setAlpha(180)
        p.setBrush(dot_color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(cx - 4), int(indicator_y), 8, 8)

        # ── Blink overlay (IDLE only) ────────────────────────────────────
        if self._state == NPCState.IDLE and self._is_blinking:
            eye_y = int(cy - 20)
            p.setPen(QPen(QColor(180, 210, 240, 200), 3, Qt.PenStyle.SolidLine))
            p.drawLine(int(cx - 24), eye_y, int(cx - 12), eye_y)
            p.drawLine(int(cx + 12), eye_y, int(cx + 24), eye_y)

        p.end()

    # ── Drag support ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            # Right-click triggers voice capture
            self.parent().on_avatar_clicked()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if hasattr(self, '_drag_start') and event.buttons() == Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_start
            self.parent().move(new_pos)  # type: ignore
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_start = None


# ── Speech Bubble ─────────────────────────────────────────────────────────────

class SpeechBubble(QWidget):
    """
    Floating monospace speech bubble that appears next to the avatar.
    Auto-resizes based on text length.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._visible = False
        self.setFixedSize(0, 0)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def speak(self, text: str) -> None:
        """Set and display speech text."""
        self._text = text
        self._visible = True
        self._update_size()
        self.show()
        self.update()

    def clear(self) -> None:
        self._text = ""
        self._visible = False
        self.hide()
        self.setFixedSize(0, 0)
        self.update()

    def _update_size(self) -> None:
        if not self._text:
            self.setFixedSize(0, 0)
            return
        fm = QFontMetrics(self.font() if self.font() else QFont("Consolas", 10))
        lines = self._text.split("\n")
        max_width = max(fm.horizontalAdvance(line) for line in lines)
        padding = 16
        line_height = fm.height() + 2
        bubble_h = len(lines) * line_height + padding * 2
        self.setFixedSize(int(max_width) + padding * 2, int(bubble_h))

    def set_font(self, font: QFont) -> None:
        self._font = font
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if not self._visible or not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        bg = QColor(15, 20, 40, 220)
        p.setBrush(bg)
        border = QColor(100, 150, 255, 180)
        p.setPen(QPen(border, 1.5))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 10, 10)

        # Text
        font = QFont("Consolas", 10)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        p.setFont(font)
        p.setPen(QColor(180, 220, 255))
        fm = QFontMetrics(font)
        lines = self._text.split("\n")
        padding = 16
        line_height = fm.height() + 2
        for i, line in enumerate(lines):
            y = padding + i * line_height + fm.ascent()
            p.drawText(padding, y, line)
        p.end()

    def sizeHint(self) -> QSize:  # type: ignore[name-defined]
        return self.size()


# ── HITL Confirmation Modal ──────────────────────────────────────────────────

class HITLModal(QDialog):
    """
    Local on-screen confirmation dialog for high-risk tool calls.
    Dark, semi-transparent overlay with Approve / Edit / Deny buttons.
    """

    def __init__(
        self,
        request: HitlRequest,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._request = request
        self._result: Optional[dict] = None
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setFixedSize(480, 320)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Dark overlay background
        overlay = QWidget(self)
        overlay.setStyleSheet(
            "background: rgba(0, 0, 0, 180);"
        )
        overlay.setFixedSize(480, 320)
        overlay.raise_()

        # Card
        card = QWidget(self)
        card.setFixedSize(440, 260)
        card.move(20, 30)
        card.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(30, 40, 60, 240), stop:1 rgba(15, 20, 35, 240));"
            "border: 2px solid rgba(255, 140, 50, 180);"
            "border-radius: 12px;"
        )
        card.raise_()

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # Title
        title = QLabel(f"⚠  Action Requires Approval")
        title.setStyleSheet(
            "color: rgba(255, 180, 100, 240); "
            "font-size: 14px; font-weight: bold; font-family: Consolas;"
        )
        layout.addWidget(title)

        # Tool info
        info = QLabel(
            f"<b>Tool:</b> <font color='#88ccff'>{self._request.tool_name}</font>\n"
            f"<b>Request ID:</b> <font color='#aaa'>{self._request.request_id}</font>"
        )
        info.setStyleSheet("color: rgba(200, 220, 240, 200); font-size: 11px; font-family: Consolas;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # Description
        desc = QLabel(self._request.description)
        desc.setStyleSheet("color: rgba(180, 200, 220, 200); font-size: 11px; font-family: Consolas;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Args preview
        args_preview = (
            f"<b>Arguments:</b>\n"
            f"<font face='Consolas' size='2'>{self._request.tool_args}</font>"
        )
        args_label = QLabel(args_preview)
        args_label.setStyleSheet(
            "background: rgba(0, 0, 0, 80); "
            "color: rgba(150, 200, 255, 220); "
            "font-size: 10px; font-family: Consolas; "
            "padding: 6px; border-radius: 4px;"
        )
        args_label.setWordWrap(True)
        layout.addWidget(args_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        approve_btn = QPushButton("✅ Approve")
        approve_btn.setStyleSheet(
            "background: rgba(40, 120, 80, 200); color: white; "
            "border: 1px solid rgba(80, 200, 140, 180); "
            "border-radius: 6px; padding: 8px 16px; "
            "font-family: Consolas; font-size: 12px;"
        )
        approve_btn.clicked.connect(self._on_approve)

        edit_btn = QPushButton("✏️ Edit")
        edit_btn.setStyleSheet(
            "background: rgba(60, 80, 140, 200); color: white; "
            "border: 1px solid rgba(100, 140, 200, 180); "
            "border-radius: 6px; padding: 8px 16px; "
            "font-family: Consolas; font-size: 12px;"
        )
        edit_btn.clicked.connect(self._on_edit)

        deny_btn = QPushButton("❌ Deny")
        deny_btn.setStyleSheet(
            "background: rgba(140, 40, 40, 200); color: white; "
            "border: 1px solid rgba(200, 80, 80, 180); "
            "border-radius: 6px; padding: 8px 16px; "
            "font-family: Consolas; font-size: 12px;"
        )
        deny_btn.clicked.connect(self._on_deny)

        btn_layout.addStretch()
        btn_layout.addWidget(approve_btn)
        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(deny_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _on_approve(self) -> None:
        self._result = {"decision": "approve", "args": self._request.tool_args}
        self.accept()

    def _on_edit(self) -> None:
        edit_dialog = EditArgsDialog(self._request.tool_args, self)
        if edit_dialog.exec() == QDialog.DialogCode.Accepted:
            self._result = {"decision": "edit", "args": edit_dialog.get_args()}
        else:
            self._result = {"decision": "deny", "args": None}
        self.accept()

    def _on_deny(self) -> None:
        self._result = {"decision": "deny", "args": None}
        self.accept()

    def get_result(self) -> Optional[dict]:
        return self._result


class EditArgsDialog(QDialog):
    """Inline editor for HITL argument modification."""

    def __init__(
        self,
        current_args: dict,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 280)
        self._current_args = current_args
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("✏️ Edit Command Arguments")
        title.setStyleSheet(
            "color: rgba(255, 200, 100, 240); "
            "font-size: 13px; font-weight: bold; font-family: Consolas;"
        )
        layout.addWidget(title)

        self._text_edit = QLineEdit()
        import json
        self._text_edit.setText(json.dumps(self._current_args, indent=2))
        self._text_edit.setStyleSheet(
            "background: rgba(0, 0, 0, 120); "
            "color: rgba(180, 220, 255, 240); "
            "font-family: Consolas; font-size: 11px; "
            "padding: 8px; border: 1px solid rgba(100, 150, 255, 150); "
            "border-radius: 4px;"
        )
        layout.addWidget(self._text_edit)

        hint = QLabel("Enter a valid JSON object, e.g. {\"key\": \"value\"}")
        hint.setStyleSheet("color: rgba(150, 170, 200, 160); font-size: 9px; font-family: Consolas;")
        layout.addWidget(hint)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        ok_btn = QPushButton("✓ Apply")
        ok_btn.setStyleSheet(
            "background: rgba(40, 120, 80, 200); color: white; "
            "border-radius: 4px; padding: 6px 14px; "
            "font-family: Consolas; font-size: 11px;"
        )
        ok_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            "background: rgba(80, 80, 80, 200); color: white; "
            "border-radius: 4px; padding: 6px 14px; "
            "font-family: Consolas; font-size: 11px;"
        )
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_args(self) -> dict:
        import json
        try:
            return json.loads(self._text_edit.text())
        except json.JSONDecodeError:
            return self._current_args


# ── Main Overlay Widget ───────────────────────────────────────────────────────

class OverlayWidget(QWidget):
    """
    Primary desktop companion widget. Hosts the Great Sage avatar,
    speech bubble, and HITL modal integration.
    """

    # Signals
    transcription_requested = pyqtSignal()  # Emitted when user clicks avatar
    prompt_submitted = pyqtSignal(str)  # Emitted when user submits text prompt
    hitl_requested = pyqtSignal(object)  # Emitted with HitlRequest
    hitl_response_received = pyqtSignal(str, object)  # (request_id, result_dict)
    response_received = pyqtSignal(str)  # Emitted when orchestrator returns a response
    app_closing = pyqtSignal()  # Emitted on close

    def __init__(
        self,
        initial_x: int = 100,
        initial_y: int = 100,
    ) -> None:
        super().__init__(None)

        # ── Window flags ────────────────────────────────────────────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._current_state = NPCState.IDLE
        self._speech_text = ""
        self._speech_timer: Optional[QTimer] = None
        self._drag_start: Optional[QPoint] = None
        self._hitl_modal: Optional[HITLModal] = None
        self._hitl_waiting: dict[str, tuple[HITLRequest, Callable]] = {}
        self._show_on_top = True

        # ── Avatar ──────────────────────────────────────────────────────
        self._avatar = GreatSageAvatar(self)
        self._avatar.setGeometry(10, 10, 200, 220)

        # ── Speech bubble ───────────────────────────────────────────────
        self._bubble = SpeechBubble(self)
        self._bubble.set_font(QFont("Consolas", 10))
        self._bubble.hide()

        # ── Text prompt input ──────────────────────────────────────────
        self._prompt_input = QLineEdit(self)
        self._prompt_input.setPlaceholderText("Type a prompt…")
        self._prompt_input.setFixedSize(190, 28)
        self._prompt_input.move(10, 232)
        self._prompt_input.returnPressed.connect(self._submit_prompt)
        self._prompt_input.setStyleSheet(
            "QLineEdit { "
            "    background: rgba(20, 30, 50, 200); "
            "    color: rgba(200, 230, 255, 255); "
            "    border: 1px solid rgba(100, 150, 255, 150); "
            "    border-radius: 4px; "
            "    padding: 2px 8px; "
            "    font-size: 12px; "
            "    font-family: Consolas; "
            "} "
            "QLineEdit:focus { "
            "    border-color: rgba(140, 190, 255, 255); "
            "    background: rgba(25, 40, 70, 220); "
            "}"
        )

        # ── Size ────────────────────────────────────────────────────────
        self._large_screen = False
        self._fish_tts = None
        self._tts_language = "en"  # "en" or "ja"
        # ── Audio ───────────────────────────────────────────────────────
        self._audio_player: Optional[QMediaPlayer] = None
        self._audio_output: Optional[QAudioOutput] = None
        self._use_audio_player = False  # True = QtMultimedia, False = pygame (QtMultimedia unavailable on this system)
        self._large_screen_w, self._large_screen_h = 700, 520
        self.setFixedSize(220, 280)
        self.move(initial_x, initial_y)

        # ── Auto-hide speech timer ──────────────────────────────────────
        self._speech_timer = QTimer(self)
        self._speech_timer.setSingleShot(True)
        self._speech_timer.timeout.connect(self._clear_speech)
        self._speech_timer.setInterval(8000)

        # ── System Tray ─────────────────────────────────────────────────
        self._tray_menu = QMenu(self)
        self._tray_menu.setStyleSheet(
            "QMenu { background: rgba(20, 30, 50, 230); color: rgba(200, 230, 255, 220); "
            "border: 1px solid rgba(100, 150, 255, 180); border-radius: 6px; padding: 4px; }"
            "QMenu::item { padding: 6px 20px; font-family: Consolas; font-size: 11px; }"
            "QMenu::item:selected { background: rgba(60, 100, 180, 160); border-radius: 4px; }"
        )
        act_show = self._tray_menu.addAction("Show")
        act_show.triggered.connect(self._do_show)
        act_hide = self._tray_menu.addAction("Hide")
        act_hide.triggered.connect(self._do_hide)
        act_top = self._tray_menu.addAction("Show on Top" if self._show_on_top else "Always on Top")
        act_top.triggered.connect(self._toggle_show_on_top)
        self._large_screen_act = self._tray_menu.addAction("Large Screen  (off)")
        self._large_screen_act.triggered.connect(self._toggle_large_screen)
        act_quit = self._tray_menu.addAction("Quit")
        act_quit.triggered.connect(self._do_quit)

        # ── Audio Language Menu ─────────────────────────────────────────
        self._lang_menu = QMenu("Audio Language", self._tray_menu)
        self._lang_menu.setStyleSheet(
            "QMenu { background: rgba(20, 30, 50, 230); color: rgba(200, 230, 255, 220); "
            "border: 1px solid rgba(100, 150, 255, 180); border-radius: 6px; padding: 4px; }"
            "QMenu::item { padding: 6px 20px; font-family: Consolas; font-size: 11px; }"
            "QMenu::item:selected { background: rgba(60, 100, 180, 160); border-radius: 4px; }"
        )
        self._lang_en = self._lang_menu.addAction("English")
        self._lang_en.triggered.connect(lambda: self._set_tts_language("en"))
        self._lang_ja = self._lang_menu.addAction("日本語")
        self._lang_ja.triggered.connect(lambda: self._set_tts_language("ja"))
        self._lang_menu.addSeparator()
        self._lang_status = self._lang_menu.addAction("Status: English")
        self._lang_status.setEnabled(False)
        self._tray_menu.addMenu(self._lang_menu)

        self._tray = QSystemTrayIcon(self)
        # Set a minimal icon so the tray shows something
        _icon_pix = QPixmap(16, 16)
        _icon_pix.fill(QColor(30, 60, 120))
        self._tray.setIcon(QIcon(_icon_pix))
        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_state(self, state: NPCState) -> None:
        self._current_state = state
        self._avatar.set_state(state)
        if state == NPCState.LISTENING:
            self._bubble.clear()
        elif state == NPCState.IDLE:
            self._bubble.clear()

    def _init_fish_tts(self) -> bool:
        """Initialize Fish Audio TTS engine with Great Sage voice model."""
        if self._fish_tts is not None:
            return True
        try:
            from ..tts.fish_audio import FishAudioTTS

            # Load settings
            import json
            from pathlib import Path
            settings_path = Path(__file__).resolve().parent.parent.parent / "config" / "settings.json"
            with open(settings_path) as f:
                settings = json.load(f)
            fish_config = settings.get("fish_audio", {})
            api_key = fish_config.get("api_key", "")

            if not api_key:
                logger.warning("Fish Audio API key not configured in settings.json")
                return False

            self._fish_tts = FishAudioTTS(api_key=api_key)
            model_id = fish_config.get("model_id", "")
            if model_id:
                self._fish_tts._voice_model = model_id
            lang = fish_config.get("language", "en")
            self._fish_tts.language = lang
            self._tts_language = lang
            # Pre-initialize session so it's ready for TTS calls
            if self._fish_tts._ensure_session():
                logger.info("Fish Audio TTS initialized with Great Sage voice (lang=%s)", lang)
                return True
            else:
                logger.error("Fish Audio session initialization failed")
                self._fish_tts = None
                return False
        except ImportError as e:
            logger.warning("Fish Audio SDK not installed — TTS disabled: %s", e)
            return False
        except Exception as exc:
            logger.warning("Fish Audio TTS initialization failed: %s", exc)
            return False

    def _speak_fish(self, text: str) -> int:
        """
        Speak text via Fish Audio TTS.
        Returns estimated display duration in ms (for bubble timing safety-net).
        The bubble is cleared when audio actually finishes, not on this estimate.
        """
        if self._fish_tts is None and not self._init_fish_tts():
            return self._estimated_duration_ms(text)

        if self._fish_tts is None:
            return self._estimated_duration_ms(text)

        # Estimate duration based on text length (used as safety-net timer)
        duration_ms = self._estimated_duration_ms(text)

        # Play audio asynchronously; clear bubble only after audio ends
        def _play_audio():
            try:
                import tempfile
                import os
                from PyQt6.QtCore import QUrl, QTimer

                # Translate if Japanese
                if self._tts_language == "ja":
                    text_to_speak = self._translate_to_japanese(text)
                else:
                    text_to_speak = text

                # Ensure session is initialized before TTS call
                if not self._fish_tts._ensure_session():
                    logger.error("Fish Audio session not available")
                    return

                # Generate audio (uses edge-tts for Japanese, pyttsx3 for English fallback)
                audio_data = self._fish_tts._generate_or_get_audio(
                    text_to_speak,
                    self._fish_tts._cache_dir / f"{hash(text_to_speak) & 0xFFFFFFFF:08x}.mp3"
                )

                if audio_data:
                    # Play audio with fallback from QtMultimedia to pygame
                    played = self._play_audio_with_fallback(audio_data)
                    if played:
                        # Schedule bubble clear AFTER audio actually finishes
                        QTimer.singleShot(duration_ms + 500, self._clear_speech)

            except Exception as e:
                logger.error("Fish Audio playback failed: %s", e)

        threading.Thread(target=_play_audio, daemon=True).start()
        return duration_ms

    def _play_audio_with_fallback(self, audio_data: bytes) -> bool:
        """Play audio with automatic fallback from QtMultimedia to pygame to system player."""
        import os
        import tempfile
        try:
            # Detect format from magic bytes so the temp file gets the right extension
            is_mp3 = (
                audio_data[:4] == b"ID3"
                or (len(audio_data) > 1 and audio_data[0] == 0xFF and (audio_data[1] & 0xE0) == 0xE0)
            )
            ext = ".mp3" if is_mp3 else ".wav"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(audio_data)
                temp_path = f.name

            def cleanup():
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

            def _system_play():
                """Fall back to OS default player for formats pygame can't handle."""
                import subprocess
                if os.name == "nt":
                    os.startfile(temp_path)
                else:
                    subprocess.run(["open", temp_path], check=False)
                duration_sec = max(len(audio_data) / 4096, 2.0)
                threading.Timer(duration_sec + 0.5, cleanup).start()

            # Iterative fallback: try Qt first if enabled, then pygame, then system player.
            # Max 3 attempts — prevents infinite recursion if all fail.
            attempts = (["qt", "pygame", "system"] if self._use_audio_player else ["pygame", "system"])
            for attempt in attempts:
                try:
                    if attempt == "qt":
                        # QtMultimedia path
                        if self._audio_player is None:
                            self._audio_player = QMediaPlayer()
                            self._audio_output = QAudioOutput()
                            self._audio_player.setAudioOutput(self._audio_output)
                        self._audio_player.setSource(QUrl.fromLocalFile(temp_path))
                        self._audio_output.setVolume(0.8)
                        self._audio_player.play()
                        self._audio_player.mediaStatusChanged.connect(
                            lambda status, c=cleanup: c()
                            if status == QMediaPlayer.MediaStatus.EndOfMedia
                            else None
                        )
                        return True
                    elif attempt == "pygame":
                        # pygame-ce fallback
                        import pygame
                        if not pygame.mixer.get_init():
                            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
                        pygame.mixer.music.load(temp_path)
                        pygame.mixer.music.set_volume(0.8)
                        pygame.mixer.music.play()
                        import threading
                        duration_sec = max(len(audio_data) / 44100 * 4, 2.0)
                        threading.Timer(duration_sec + 0.5, cleanup).start()
                        return True
                    else:
                        # System player — handles MP3 and any format the OS supports
                        _system_play()
                        return True
                except Exception as e:
                    logger.warning("Audio playback failed via %s (%s)", attempt, e)
                    if self._audio_player:
                        try:
                            self._audio_player.stop()
                            self._audio_player.deleteLater()
                        except Exception:
                            pass
                        self._audio_player = None
                        self._audio_output = None
            # All attempts failed
            cleanup()
            return False

        except Exception as e:
            logger.error("Failed to play audio: %s", e)
            return False

    def _translate_to_japanese(self, text: str) -> str:
        """Translate text to Japanese using free translation APIs."""
        # Check known translations first
        KNOWN_TRANSLATIONS = {
            "Notice, Master.": "マスター、ご通知いたします。",
            "Report.": "報告いたします。",
            "Analysis completed.": "分析を完了しました。",
            "All systems operational.": "全システム稼働中です。",
            "I have analyzed the current system state.": "現在のシステム状態を分析しました。",
            "All systems are operational.": "すべてのシステムが稼働しています。",
            "System status check complete.": "システム状態チェック完了。",
            "Processing your request, Master.": "リクエストを処理いたします、マスター。",
            "The analysis is complete.": "分析は完了しました。",
            "Visual analysis completed.": "視覚分析を完了しました、マスター。",
            "Awaiting your command.": "ご命令をお待ちしております。",
            "Stand by, Master.": "スタンバイ、マスター。",
            "Connecting to the network.": "ネットワークに接続中。",
        }

        if text in KNOWN_TRANSLATIONS:
            return KNOWN_TRANSLATIONS[text]

        # Try Google Translate via translators library
        try:
            import translators
            translated = translators.translate_text(text, translator="google", to_language="ja")
            if translated and translated != text:
                logger.info("Translated to Japanese: %s -> %s", text[:50], translated[:50])
                return translated
        except Exception as e:
            logger.debug("Translation failed (will use original): %s", e)

        logger.warning("Could not translate to Japanese, using original text")
        return text

    @staticmethod
    def _estimated_duration_ms(text: str) -> int:
        """Estimate TTS duration from word count at ~160 wpm (Fish Audio is faster), min 3000ms."""
        word_count = len(text.split())
        # Fish Audio speaks more naturally, ~160-180 wpm
        duration_ms = max(int((word_count / 160.0) * 60.0 * 1000), 3000)
        return duration_ms

    def speak(self, text: str, duration_ms: int = 0) -> None:
        """Display text in the speech bubble and speak via Fish Audio TTS."""
        import logging
        logger = logging.getLogger("corecontrol.overlay")
        logger.info("speak() called with: %r", text[:80])
        self._speech_text = text
        self._bubble.speak(text)
        self._reposition_bubble()
        self._speech_timer.stop()
        # Use Fish Audio TTS-driven duration
        tts_duration = self._speak_fish(text)
        if duration_ms > 0:
            # User specified explicit duration — use max of that and TTS estimate
            tts_duration = max(tts_duration, duration_ms)
        # Safety-net timer: clear bubble if TTS/pygame never fires its own
        # clear (e.g. if audio playback fails silently).
        self._speech_timer.setInterval(tts_duration + 1000)
        self._speech_timer.start()
        # Resize widget to accommodate bubble
        self._update_widget_size()

    def _set_tts_language(self, lang: str) -> None:
        """Set TTS language and update menu display."""
        self._tts_language = lang
        if self._fish_tts:
            self._fish_tts.language = lang
        # Update menu status
        lang_name = "English" if lang == "en" else "日本語"
        self._lang_status.setText(f"Status: {lang_name}")
        logger.info("TTS language set to: %s", lang_name)

    def on_avatar_clicked(self) -> None:
        """Triggered by right-click on avatar — starts voice capture."""
        self.transcription_requested.emit()

    def request_hitl(
        self,
        request: HitlRequest,
        on_result: Callable[[dict], None],
    ) -> None:
        """Show HITL modal and register callback for the result."""
        self._hitl_waiting[request.request_id] = (request, on_result)
        modal = HITLModal(request, self)
        self._hitl_modal = modal
        modal.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        modal.finished.connect(lambda code: self._on_hitl_finished(modal, request.request_id))
        modal.exec()

    def closeEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.app_closing.emit()
        event.ignore()  # We handle cleanup ourselves

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reposition_bubble(self) -> None:
        """Position speech bubble to the right of the avatar."""
        avatar_right = self._avatar.x() + self._avatar.width() + 8
        bubble_x = min(avatar_right, self.width() - self._bubble.width() - 4)
        if bubble_x < 0:
            bubble_x = self.width() - self._bubble.width() - 4
        self._bubble.move(max(0, bubble_x), 10)

    def _update_widget_size(self) -> None:
        """Expand widget to fit speech bubble if needed."""
        if self._large_screen:
            return  # Large screen mode handles its own sizing
        bubble_w = self._bubble.width()
        bubble_h = self._bubble.height()
        new_w = max(self.width(), 220 + bubble_w + 20)
        new_h = max(self.height(), 280 + bubble_h + 10)
        if new_w != self.width() or new_h != self.height():
            self.setFixedSize(new_w, new_h)

    def _toggle_large_screen(self) -> None:
        """Toggle between normal (220×280) and large screen (700×520) modes."""
        self._large_screen = not self._large_screen
        if self._large_screen:
            self.setFixedSize(self._large_screen_w, self._large_screen_h)
            self._avatar.setGeometry(10, 10, 200, 220)
            self._prompt_input.move(10, 232)
            self._large_screen_act.setText("Large Screen  (on)")
        else:
            self.setFixedSize(220, 280)
            self._avatar.setGeometry(10, 10, 200, 220)
            self._prompt_input.move(10, 232)
            self._large_screen_act.setText("Large Screen  (off)")
        self._reposition_bubble()
        self._update_widget_size()

    def _submit_prompt(self) -> None:
        """Submit text from the input field."""
        text = self._prompt_input.text().strip()
        if not text:
            return
        self._prompt_input.clear()
        self.prompt_submitted.emit(text)

    def _clear_speech(self) -> None:
        self._bubble.clear()
        self._update_widget_size()

    def _on_hitl_finished(self, modal: HITLModal, request_id: str) -> None:
        result = modal.get_result()
        if result is None:
            return
        decision = result.get("decision")
        args = result.get("args")
        if decision == "approve":
            self.hitl_response_received.emit(request_id, {"approved": True, "args": args})
        elif decision == "edit":
            self.hitl_response_received.emit(request_id, {"approved": True, "args": args})
        else:
            self.hitl_response_received.emit(request_id, {"approved": False, "args": args})
        self._hitl_modal = None
        self._hitl_waiting.pop(request_id, None)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon clicks."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._do_show()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            self._tray_menu.exec(QPoint())

    def _do_show(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _do_hide(self) -> None:
        self.hide()

    def _toggle_show_on_top(self) -> None:
        self._show_on_top = not self._show_on_top
        flags = self.windowFlags()
        if self._show_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _do_quit(self) -> None:
        # Stop any playing audio
        if self._use_audio_player and self._audio_player:
            self._audio_player.stop()
        elif not self._use_audio_player:
            try:
                import pygame
                pygame.mixer.music.stop()
            except Exception:
                pass
        if self._fish_tts:
            self._fish_tts.stop()
        self.app_closing.emit()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # Stop audio on close
        if self._use_audio_player and self._audio_player:
            self._audio_player.stop()
        elif not self._use_audio_player:
            try:
                import pygame
                pygame.mixer.music.stop()
            except Exception:
                pass
        if self._fish_tts:
            self._fish_tts.stop()
        super().closeEvent(event)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        # Transparent background — no box behind the avatar
        pass

    # ── Drag support ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_start is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_start = None


# ── App Factory ───────────────────────────────────────────────────────────────

def create_app_and_overlay(
    x: int = 100,
    y: int = 100,
) -> tuple[QApplication, OverlayWidget]:
    """
    Create (or reuse) the QApplication instance and return a ready-to-show overlay.
    Call overlay.show() after connecting signals.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    overlay = OverlayWidget(initial_x=x, initial_y=y)
    return app, overlay


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app, overlay = create_app_and_overlay(400, 300)
    overlay.show()

    # Simulate states for testing
    def _cycle_states() -> None:
        overlay.set_state(NPCState.LISTENING)
        QTimer.singleShot(2000, lambda: overlay.set_state(NPCState.PROCESSING))
        QTimer.singleShot(4000, lambda: overlay.speak(
            "Notice, Master. I have analyzed the current system state.\n"
            "All systems operational.", duration_ms=5000
        ))
        QTimer.singleShot(9000, lambda: overlay.set_state(NPCState.IDLE))

    QTimer.singleShot(1000, _cycle_states)
    sys.exit(app.exec())
