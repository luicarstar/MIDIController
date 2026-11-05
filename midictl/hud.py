"""Small click-through on-screen hint near the cursor."""
from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLabel, QWidget, QVBoxLayout


class Hud(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setStyleSheet(
            "QLabel { background: rgba(47, 58, 61, 215); color: #ffffff; border-radius: 8px;"
            " padding: 8px 14px; font-size: 14px; font-weight: 600; }")
        lay.addWidget(self.label)
        self._hide = QTimer(self, singleShot=True, timeout=self._fade)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(220)
        self._anim.finished.connect(lambda: self.hide() if self.windowOpacity() < 0.05 else None)

    def show_text(self, text: str, ms: int = 900) -> None:
        self._anim.stop()
        self.label.setText(text)
        self.adjustSize()
        pos = QCursor.pos()
        screen = self.screen().availableGeometry() if self.screen() else None
        x, y = pos.x() + 18, pos.y() + 22
        if screen is not None:
            x = min(x, screen.right() - self.width() - 4)
            y = min(y, screen.bottom() - self.height() - 4)
        self.move(x, y)
        self.setWindowOpacity(1.0)
        self.show()
        self._hide.start(ms)

    def _fade(self) -> None:
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()
