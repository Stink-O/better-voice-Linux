"""The small recovery window.

Matches the macOS panel: a warning triangle, the failure and what to do about
it, and one action button on the right -- a single 540-point-wide row rather than
a stacked dialog. The macOS build deliberately kept errors on screen with a way
forward instead of letting them vanish as a system beep.
"""

from __future__ import annotations

from typing import Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME
from .widgets import secondary

WIDTH = 540
PADDING = 18

WARNING_ORANGE = QtGui.QColor(255, 159, 10)


class _WarningIcon(QtWidgets.QWidget):
    SIZE = 24

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        triangle = QtGui.QPainterPath()
        triangle.moveTo(self.SIZE / 2, 2.0)
        triangle.lineTo(self.SIZE - 1.5, self.SIZE - 3.0)
        triangle.lineTo(1.5, self.SIZE - 3.0)
        triangle.closeSubpath()
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(WARNING_ORANGE)
        painter.drawPath(triangle)

        pen = QtGui.QPen(QtGui.QColor(255, 255, 255))
        pen.setWidthF(2.0)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(
            QtCore.QPointF(self.SIZE / 2, self.SIZE * 0.36),
            QtCore.QPointF(self.SIZE / 2, self.SIZE * 0.63),
        )
        painter.drawPoint(QtCore.QPointF(self.SIZE / 2, self.SIZE * 0.77))
        painter.end()


class RecoveryNotice(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent, QtCore.Qt.WindowType.Tool)
        self.setWindowTitle(APP_NAME)
        self.setFixedWidth(WIDTH)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(PADDING, PADDING, PADDING, PADDING)
        layout.setSpacing(14)

        layout.addWidget(_WarningIcon(), 0, QtCore.Qt.AlignmentFlag.AlignTop)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(3)
        self._title = QtWidgets.QLabel()
        title_font = self._title.font()
        title_font.setWeight(QtGui.QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        self._title.setWordWrap(True)
        text.addWidget(self._title)

        self._detail = QtWidgets.QLabel()
        self._detail.setWordWrap(True)
        detail_font = self._detail.font()
        detail_font.setPointSizeF(max(8.0, detail_font.pointSizeF() - 0.5))
        self._detail.setFont(detail_font)
        palette = self._detail.palette()
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, secondary(self._detail))
        self._detail.setPalette(palette)
        text.addWidget(self._detail)
        layout.addLayout(text, 1)

        self._action = QtWidgets.QPushButton()
        self._action.setDefault(True)
        layout.addWidget(self._action, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        self._callback: Callable[[], None] | None = None
        self._action.clicked.connect(self._run)

    def _run(self) -> None:
        callback, self._callback = self._callback, None
        self.hide()
        if callback is not None:
            callback()

    def show_notice(
        self,
        title: str,
        detail: str,
        action_title: str,
        action: Callable[[], None],
    ) -> None:
        self._title.setText(title)
        self._detail.setText(detail)
        self._action.setText(action_title)
        self.setAccessibleName(f"{title}. {detail}")
        self._callback = action
        self.adjustSize()
        self.show()
        self.raise_()
        self.activateWindow()
