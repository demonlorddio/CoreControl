"""
Floating NPC overlay for CoreControl.
A translucent, always-on-top widget with animated NPC character.
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import (
    QPoint,
    QPropertyAnimation,
    QEasingCurve,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


class NPCCreature(QWidget):
    """
    A cute floating NPC character with animation.
    Bounces gently and blinks periodically.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(60, 70)
        self._bounce_offset: float = 0.0
        self._blink_state: float = 1.0  # 1.0 = open, 0.0 = closed
        self._is_blinking: bool = False
        self._processing: bool = False

        # Animation for bouncing
        self._bounce_anim = QPropertyAnimation(self, b"bounce_offset", self)
        self._bounce_anim.setDuration(1500)
        self._bounce_anim.setStartValue(0.0)
        self._bounce_anim.setEndValue(-8.0)
        self._bounce_anim.setEasingCurve(QEasingCurve.Type.Sinusoidal)
        self._bounce_anim.setLoopCount(-1)  # infinite

        # Blink timer
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._start_blink)
        self._blink_timer.start(3000)  # blink every 3 seconds

    @pyqtProperty(float)
    def bounce_offset(self) -> float:
        return self._bounce_offset

    @bounce_offset.setter  # type: ignore[no-redef]
    def bounce_offset(self, value: float) -> None:
        self._bounce_offset = value
        self.update()

    def set_processing(self, processing: bool) -> None:
        self._processing = processing
        self.update()

    def _start_blink(self) -> None:
        self._is_blinking = True
        self._blink_state = 1.0
        self.update()
        QTimer.singleShot(150, self._end_blink)

    def _end_blink(self) -> None:
        self._is_blinking = False
        self._blink_state = 1.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx, cy = 30, 35 + self._bounce_offset

        # Body glow
        glow_color = QColor(100, 200, 255) if self._processing else QColor(80, 180, 220)
        glow_color.setAlphaF(0.3)
        glow = QPainter(painter)
        glow.setRenderHint(QPainter.RenderHint.Antialiasing)
        glow.setBrush(glow_color)
        glow.setPen(Qt.PenStyle.NoPen)
        glow.drawEllipse(cx - 25, cy - 25, 50, 50)
        glow.end()

        # Body (round blob)
        body_color = QColor(60, 140, 180) if self._processing else QColor(50, 120, 160)
        painter.setBrush(body_color)
        painter.setPen(QPen(QColor(40, 100, 140), 2))
        painter.drawEllipse(cx - 20, cy - 18, 40, 36)

        # Eyes
        eye_y = cy - 5
        eye_spacing = 8

        if self._is_blinking:
            # Closed eyes (lines)
            painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.PenStyle.SolidLine))
            painter.drawLine(cx - eye_spacing - 3, eye_y, cx - eye_spacing + 3, eye_y)
            painter.drawLine(cx + eye_spacing - 3, eye_y, cx + eye_spacing + 3, eye_y)
        else:
            # Open eyes
            painter.setBrush(QColor(255, 255, 255))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(cx - eye_spacing - 3, eye_y - 3, 6, 6)
            painter.drawEllipse(cx + eye_spacing - 3, eye_y - 3, 6, 6)
            # Pupils
            painter.setBrush(QColor(30, 30, 50))
            painter.drawEllipse(cx - eye_spacing - 1, eye_y - 1, 3, 3)
            painter.drawEllipse(cx + eye_spacing - 1, eye_y - 1, 3, 3)

        # Mouth
        mouth_y = cy + 8
        if self._processing:
            # Surprised/open mouth when processing
            painter.setBrush(QColor(200, 100, 100))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(cx - 4, mouth_y, 8, 6)
        else:
            # Simple smile
            painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.PenStyle.SolidLine))
            painter.drawArc(cx - 6, mouth_y - 2, 12, 8, 16 * 180, 16 * 180)

        # Antenna/aura when processing
        if self._processing:
            antenna_color = QColor(255, 220, 100)
            antenna_color.setAlphaF(0.8 + 0.2 * self._bounce_offset / 8)
            painter.setBrush(antenna_color)
            painter.setPen(Qt.PenStyle.NoPen)
            # Small diamond on top
            points = [(cx, cy - 22), (cx + 4, cy - 18), (cx, cy - 14), (cx - 4, cy - 18)]
            painter.drawPolygon(points)


class OverlayWidget(QWidget):
    """
    Translucent, frameless, always-on-top overlay with NPC character.
    Shows status and animates when processing.
    Draggable via mouse events.
    """

    recording_state_changed = pyqtSignal(bool)

    def __init__(self, initial_x: int = 100, initial_y: int = 100) -> None:
        super().__init__(None)

        # ── Window flags ─────────────────────────────────────────────────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # ── Layout ───────────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # NPC character
        self._npc = NPCCreature(self)
        layout.addWidget(self._npc, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Status label below NPC
        self._status_label = QLabel("Idle")
        status_font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        self._status_label.setFont(status_font)
        self._status_label.setStyleSheet(
            "color: rgba(220, 240, 255, 200); background: transparent;"
        )
        layout.addWidget(self._status_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.setFixedSize(80, 110)
        self.move(initial_x, initial_y)

        # ── Drag state ───────────────────────────────────────────────────────
        self._drag_start: Optional[QPoint] = None

        # ── Blink timer (status messages) ─────────────────────────────────
        self._blink_timer = QTimer(self)
        self._blink_timer.setSingleShot(True)
        self._blink_timer.timeout.connect(self._clear_status)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_recording(self, active: bool) -> None:
        """Toggle the pulse animation and status text."""
        self._npc.set_processing(active)
        self.set_status("🎙 Listening…" if active else "Idle")
        self.recording_state_changed.emit(active)

    def set_status(self, text: str, duration_ms: int = 0) -> None:
        """Update the status label; optionally auto-clear after duration_ms."""
        self._status_label.setText(text)
        if duration_ms > 0:
            self._blink_timer.start(duration_ms)

    def set_processing(self, processing: bool) -> None:
        self._npc.set_processing(processing)
        self.set_status("⚙ Processing…" if processing else "Idle")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _clear_status(self) -> None:
        self._status_label.setText("Idle")

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Subtle gradient background
        from PyQt6.QtGui import QLinearGradient
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0, QColor(20, 30, 40, 180))
        grad.setColorAt(1, QColor(10, 20, 30, 120))
        painter.setBrush(grad)
        painter.setPen(QColor(60, 100, 140, 100))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 15, 15)

    # ── Drag events ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_start = None


def create_app_and_overlay(x: int = 100, y: int = 100) -> tuple[QApplication, OverlayWidget]:
    """
    Create (or reuse) the QApplication instance and return a ready-to-show overlay.
    Call overlay.show() after connecting signals.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    overlay = OverlayWidget(initial_x=x, initial_y=y)
    return app, overlay


if __name__ == "__main__":
    # Quick manual test
    app, overlay = create_app_and_overlay(400, 300)
    overlay.show()

    # Simulate processing after 1 second
    QTimer.singleShot(1000, lambda: overlay.set_processing(True))
    QTimer.singleShot(4000, lambda: overlay.set_processing(False))
    QTimer.singleShot(5000, lambda: overlay.set_status("✅ Done", 2000))

    sys.exit(app.exec())
