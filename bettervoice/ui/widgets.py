"""Small drawn widgets that mirror the macOS setup sheet.

SF Symbols and SwiftUI's material fills have no Linux equivalent, so the status
icons, the key caps and the capture illustration are drawn here with the same
geometry and weights the original used. Everything takes its colours from the Qt
palette so the window still looks native in a light or dark desktop theme.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from .palette import BLUE

READY_GREEN = QtGui.QColor(48, 209, 88)

#: Matches the macOS sheet: 250 x 150 illustration beside the heading.
PREVIEW_SIZE = QtCore.QSize(262, 162)

#: The original's `.shadow(color: .black.opacity(0.12), radius: 12, y: 5)`.
SHADOW_RADIUS = 6
SHADOW_ALPHA = 34
SHADOW_OFFSET_Y = 2


def secondary(widget: QtWidgets.QWidget) -> QtGui.QColor:
    """The palette's muted text colour, as SwiftUI's `.secondary` would be."""

    colour = QtGui.QColor(widget.palette().color(QtGui.QPalette.ColorRole.WindowText))
    colour.setAlphaF(0.62)
    return colour


def quaternary(widget: QtWidgets.QWidget) -> QtGui.QColor:
    """The faint fill behind a key cap."""

    colour = QtGui.QColor(widget.palette().color(QtGui.QPalette.ColorRole.WindowText))
    colour.setAlphaF(0.11)
    return colour


class StatusIcon(QtWidgets.QWidget):
    """A filled green check when ready, a hollow circle when not."""

    SIZE = 19

    def __init__(self, ready: bool = False) -> None:
        super().__init__()
        self._ready = ready
        self.setFixedSize(self.SIZE, self.SIZE)
        self._announce()

    def set_ready(self, ready: bool) -> None:
        if ready != self._ready:
            self._ready = ready
            self._announce()
            self.update()

    def _announce(self) -> None:
        self.setAccessibleName("Ready" if self._ready else "Needs setup")

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        bounds = QtCore.QRectF(1, 1, self.SIZE - 2, self.SIZE - 2)

        if not self._ready:
            pen = QtGui.QPen(secondary(self))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(bounds)
            painter.end()
            return

        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(READY_GREEN)
        painter.drawEllipse(bounds)

        tick = QtGui.QPainterPath()
        tick.moveTo(bounds.left() + bounds.width() * 0.26, bounds.top() + bounds.height() * 0.52)
        tick.lineTo(bounds.left() + bounds.width() * 0.44, bounds.top() + bounds.height() * 0.70)
        tick.lineTo(bounds.left() + bounds.width() * 0.75, bounds.top() + bounds.height() * 0.32)
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255))
        pen.setWidthF(2.1)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(tick)
        painter.end()


class InfoIcon(QtWidgets.QWidget):
    """The blue badge the grammar row uses instead of a readiness check."""

    SIZE = 19

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAccessibleName("Information")

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(BLUE)
        pen.setWidthF(1.7)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for index in range(3):
            y = 4.5 + index * 4.2
            width = self.SIZE - 7 if index < 2 else self.SIZE - 12
            painter.drawLine(QtCore.QPointF(3, y), QtCore.QPointF(3 + width - 4, y))
        tick = QtGui.QPainterPath()
        tick.moveTo(self.SIZE - 8.5, self.SIZE - 6.5)
        tick.lineTo(self.SIZE - 6.0, self.SIZE - 4.0)
        tick.lineTo(self.SIZE - 1.5, self.SIZE - 9.5)
        painter.drawPath(tick)
        painter.end()


class KeyCap(QtWidgets.QLabel):
    """The rounded key cap that carries a shortcut, as on the macOS sheet."""

    def __init__(self, keys: str) -> None:
        super().__init__(keys)
        self.setAccessibleName(f"Shortcut {keys}")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(52)
        font = self.font()
        font.setPointSizeF(font.pointSizeF() + 4)
        font.setWeight(QtGui.QFont.Weight.Medium)
        self.setFont(font)
        self.setContentsMargins(12, 8, 12, 8)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )

    def sizeHint(self) -> QtCore.QSize:
        metrics = QtGui.QFontMetrics(self.font())
        margins = self.contentsMargins()
        return QtCore.QSize(
            max(self.minimumWidth(), metrics.horizontalAdvance(self.text()) + margins.left() + margins.right()),
            metrics.height() + margins.top() + margins.bottom(),
        )

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setAccessibleName(f"Shortcut {text}")
        # A key cap must never be squeezed by its neighbour: a clipped shortcut
        # is worse than a narrower description beside it.
        self.setFixedWidth(self.sizeHint().width())
        self.updateGeometry()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(quaternary(self))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()), 9, 9)
        painter.end()
        super().paintEvent(event)


class CapturePreview(QtWidgets.QWidget):
    """The little illustration of a circled button, drawn as SwiftUI drew it."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(PREVIEW_SIZE)
        self.setAccessibleName(
            "A blue mouse trail circles a button and captures the screen"
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        bounds = QtCore.QRectF(self.rect())

        card = self.palette().color(QtGui.QPalette.ColorRole.Window)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)

        # The original card carries a soft drop shadow, which is what separates
        # it from the sheet in a light theme where the two are nearly the same
        # colour. Approximated with a few expanding passes.
        body = bounds.adjusted(SHADOW_RADIUS, SHADOW_RADIUS, -SHADOW_RADIUS, -SHADOW_RADIUS)
        for spread in range(int(SHADOW_RADIUS), 0, -1):
            alpha = int(SHADOW_ALPHA * (1 - spread / SHADOW_RADIUS) ** 2)
            if alpha <= 0:
                continue
            painter.setBrush(QtGui.QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                body.adjusted(-spread, -spread + SHADOW_OFFSET_Y, spread, spread + SHADOW_OFFSET_Y),
                13 + spread,
                13 + spread,
            )

        painter.setBrush(card.lighter(118) if card.lightness() < 128 else card.lighter(112))
        painter.drawRoundedRect(body, 13, 13)

        # Title-bar dots.
        dots = (
            QtGui.QColor(255, 95, 87, 179),
            QtGui.QColor(254, 188, 46, 179),
            QtGui.QColor(40, 200, 64, 179),
        )
        origin = body.topLeft()
        for index, colour in enumerate(dots):
            painter.setBrush(colour)
            painter.drawEllipse(
                QtCore.QRectF(origin.x() + 15 + index * 12, origin.y() + 15, 7, 7)
            )

        # Two placeholder lines and a button.
        muted = secondary(self)
        for width, height, top, alpha in ((110, 8, 33, 0.22), (86, 8, 49, 0.15)):
            fill = QtGui.QColor(muted)
            fill.setAlphaF(alpha)
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QtCore.QRectF(origin.x() + 15, origin.y() + top, width, height),
                height / 2,
                height / 2,
            )
        button = QtGui.QColor(BLUE)
        button.setAlphaF(0.16)
        painter.setBrush(button)
        painter.drawRoundedRect(
            QtCore.QRectF(origin.x() + 15, origin.y() + 65, 72, 28), 6, 6
        )

        # The circled area, offset to the right exactly as the original.
        centre = QtCore.QPointF(body.center().x() + 39, body.center().y() + 25)
        fill = QtGui.QColor(BLUE)
        fill.setAlphaF(0.14)
        painter.setBrush(fill)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, 39, 30)

        painter.save()
        painter.translate(centre)
        painter.rotate(-16)
        pen = QtGui.QPen(BLUE)
        pen.setWidthF(4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setDashPattern([11, 1.75])
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QtCore.QPointF(0, 0), 46, 36)
        painter.restore()
        painter.end()


class Spinner(QtWidgets.QProgressBar):
    """A small indeterminate progress control, matching SwiftUI's ProgressView."""

    def __init__(self) -> None:
        super().__init__()
        self.setRange(0, 0)
        self.setTextVisible(False)
        self.setFixedSize(76, 6)
