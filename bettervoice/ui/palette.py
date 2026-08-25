"""The small colour vocabulary shared by the overlay, HUD and screenshots."""

from __future__ import annotations

from PyQt6 import QtGui

BLUE = QtGui.QColor(0, 122, 255)
CYAN = QtGui.QColor(50, 173, 230)
HUD_BACKGROUND = QtGui.QColor(18, 18, 18, 245)
HUD_TITLE = QtGui.QColor(255, 255, 255)
HUD_DETAIL = QtGui.QColor(184, 184, 184)


def alpha(color: QtGui.QColor, value: float) -> QtGui.QColor:
    """A copy of ``color`` at ``value`` opacity (0-1)."""

    faded = QtGui.QColor(color)
    faded.setAlpha(max(0, min(255, round(value * 255))))
    return faded
