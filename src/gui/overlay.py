"""
Translucent floating overlay widget for CoreControl.
Built with PyQt6 — always-on-top, frameless, draggable.
Shows a visual recording indicator that pulses when audio is active.
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import (
    QPoint,
    QPropertyAnimation,
    QRect,
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
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


class PulseIndicator(QWidget):
    """
    A circular widget that animates opacity when recording is active.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._opacity: float = 1.0
        self._active: bool = False

        self._anim = QPropertyAnimation(self, b"pulse_opacity", self)
        self._anim.setDuration(800)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.2)
        self._anim.setLoopCount(-1)  # infinite

    @pyqtProperty(float)
    def pulse_opacity(self) -> float:
        return self._opacity

    @pulse_opacity.setter  # type: ignore[no-redef]
    def pulse_opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._anim.start()
        else:
            self._anim.stop()
            self._opacity = 0.3
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = QColor(220, 50, 50) if self._active else QColor(100, 100, 100)
        color.setAlphaF(self._opacity)

        grad = QRadialGradient(10, 10, 10)
        grad.setColorAt(0, color)
        center_color = QColor(color)
        center_color.setAlphaF(0.0)
        grad.setColorAt(1, center_color)

        painter.setBrush(grad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 20, 20)


class OverlayWidget(QWidget):
    """
    Translucent, frameless, always-on-top overlay window.
    Displays CoreControl status and the recording pulse indicator.
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
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        self._title_label = QLabel("CoreControl")
        title_font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet("color: rgba(240,240,240,220);")
        layout.addWidget(self._title_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._status_label = QLabel("Idle")
        status_font = QFont("Segoe UI", 8)
        self._status_label.setFont(status_font)
        self._status_label.setStyleSheet("color: rgba(200,200,200,180);")
        layout.addWidget(self._status_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._pulse = PulseIndicator(self)
        layout.addWidget(self._pulse, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.setFixedSize(160, 90)
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
        self._pulse.set_active(active)
        self.set_status("🎙 Listening…" if active else "Idle")
        self.recording_state_changed.emit(active)

    def set_status(self, text: str, duration_ms: int = 0) -> None:
        """Update the status label; optionally auto-clear after duration_ms."""
        self._status_label.setText(text)
        if duration_ms > 0:
            self._blink_timer.start(duration_ms)

    def set_processing(self, processing: bool) -> None:
        if processing:
            self.set_status("⚙ Processing…")
        else:
            self.set_status("Idle")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _clear_status(self) -> None:
        self._status_label.setText("Idle")

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QColor(30, 30, 30, 200)
        painter.setBrush(bg)
        painter.setPen(QColor(80, 80, 80, 160))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

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
    app, overlay = create_app_and_overlay(200, 200)
    overlay.show()

    # Simulate recording toggle after 1 second
    QTimer.singleShot(1000, lambda: overlay.set_recording(True))
    QTimer.singleShot(4000, lambda: overlay.set_recording(False))
    QTimer.singleShot(4500, lambda: overlay.set_status("✅ Done", 2000))

    sys.exit(app.exec())
