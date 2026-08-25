"""Tray artwork.

Linux has no SF Symbols, so the three waveform states the macOS build used are
drawn here. They follow the freedesktop convention of a monochrome, symbolic
glyph that inherits the panel's colour, and are also written out as the app icon
for the desktop entry.
"""

from __future__ import annotations

from PyQt6 import QtCore, QtGui

#: Bar heights, as a fraction of the icon height. The middle is tallest, which
#: reads as "waveform" at 22px on a panel.
_BARS = (0.30, 0.58, 0.92, 0.66, 0.38)


def _draw_waveform(painter: QtGui.QPainter, rect: QtCore.QRectF, color: QtGui.QColor) -> None:
    count = len(_BARS)
    spacing = rect.width() / (count * 2 - 1)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for index, fraction in enumerate(_BARS):
        height = rect.height() * fraction
        bar = QtCore.QRectF(
            rect.left() + index * spacing * 2,
            rect.center().y() - height / 2,
            spacing,
            height,
        )
        painter.drawRoundedRect(bar, spacing / 2, spacing / 2)


def waveform(size: int = 22, color: QtGui.QColor | None = None) -> QtGui.QIcon:
    """The idle glyph: a bare waveform."""

    return _render(size, color, ring=None)


def waveform_in_circle(
    size: int = 22, color: QtGui.QColor | None = None, filled: bool = True
) -> QtGui.QIcon:
    """The recording glyph. Alternating filled/outline gives the pulse."""

    return _render(size, color, ring="filled" if filled else "outline")


def _render(size: int, color: QtGui.QColor | None, ring: str | None) -> QtGui.QIcon:
    color = color or QtGui.QColor(220, 220, 220)
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

    inset = size * 0.08
    bounds = QtCore.QRectF(inset, inset, size - inset * 2, size - inset * 2)

    if ring == "filled":
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(bounds)
        glyph = bounds.adjusted(
            bounds.width() * 0.26,
            bounds.height() * 0.30,
            -bounds.width() * 0.26,
            -bounds.height() * 0.30,
        )
        _draw_waveform(painter, glyph, QtGui.QColor(0, 0, 0, 0))
        painter.setCompositionMode(
            QtGui.QPainter.CompositionMode.CompositionMode_DestinationOut
        )
        _draw_waveform(painter, glyph, QtGui.QColor(255, 255, 255))
    else:
        if ring == "outline":
            pen = QtGui.QPen(color)
            pen.setWidthF(max(1.2, size * 0.07))
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(bounds.adjusted(pen.widthF() / 2, pen.widthF() / 2,
                                                -pen.widthF() / 2, -pen.widthF() / 2))
            glyph = bounds.adjusted(
                bounds.width() * 0.28,
                bounds.height() * 0.32,
                -bounds.width() * 0.28,
                -bounds.height() * 0.32,
            )
        else:
            glyph = bounds.adjusted(0, bounds.height() * 0.18, 0, -bounds.height() * 0.18)
        _draw_waveform(painter, glyph, color)

    painter.end()
    icon = QtGui.QIcon(pixmap)
    icon.setIsMask(True)
    return icon


def application_svg() -> str:
    """The desktop-entry icon, as SVG so it scales in every launcher."""

    bars = []
    for index, fraction in enumerate(_BARS):
        height = 34 * fraction
        bars.append(
            f'<rect x="{16 + index * 8}" y="{32 - height / 2:.1f}" '
            f'width="4" height="{height:.1f}" rx="2" fill="#ffffff"/>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#32ade6"/><stop offset="1" stop-color="#007aff"/>'
        "</linearGradient></defs>"
        '<rect width="64" height="64" rx="14" fill="url(#g)"/>'
        + "".join(bars)
        + "</svg>"
    )
