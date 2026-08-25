"""The on-screen recording surface: pointer trail, capture pulses and the HUD.

macOS used one borderless ``NSPanel`` per screen at screen-saver level, plus a
second panel for the HUD placed at an exact position. Wayland does not let an
application place a window, so both jobs are done by one fullscreen,
input-transparent window per screen -- the HUD is simply painted inside it at the
bottom of whichever screen the pointer started on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PyQt6 import QtCore, QtGui, QtWidgets

from ..core import trail_segments
from .palette import BLUE, CYAN, HUD_BACKGROUND, HUD_DETAIL, HUD_TITLE, alpha

TRAIL_LIFETIME = 0.9
CONFIRMATION_LIFETIME = 1.1
FRAME_INTERVAL_MS = 16

HUD_WIDTH = 290
HUD_HEIGHT = 56
HUD_RADIUS = 18
HUD_BOTTOM_MARGIN = 24

#: The macOS HUD used 13pt semibold over 11pt regular in a fixed-size panel.
HUD_TITLE_PIXELS = 13
HUD_DETAIL_PIXELS = 11


@dataclass
class _TrailPoint:
    point: tuple[float, float]
    time: float


@dataclass
class _Confirmation:
    center: tuple[float, float]
    radius: float
    started_at: float


@dataclass
class HUDModel:
    """Everything the HUD shows. Shared by every screen's overlay."""

    microphone: str = ""
    level: float = 0.0
    context_count: int = 0
    is_finishing: bool = False
    finishing_message: str = "Transcribing…"
    capture_message: str | None = None
    visible: bool = False
    description: str = ""

    @property
    def title(self) -> str:
        """The headline: a capture note wins, then progress, then "Listening"."""

        if self.capture_message is not None:
            return self.capture_message
        return self.finishing_message if self.is_finishing else "Listening"

    @property
    def detail(self) -> str:
        """The microphone, with a running count once anything has been captured."""

        if self.context_count > 0:
            return f"{self.microphone}  •  {self.context_count} captured"
        return self.microphone


class _OverlayWindow(QtWidgets.QWidget):
    """One fullscreen, click-through canvas on one screen."""

    def __init__(self, screen: QtGui.QScreen, hud: HUDModel, reduce_motion: bool) -> None:
        super().__init__(
            None,
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowTransparentForInput
            | QtCore.Qt.WindowType.BypassWindowManagerHint,
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("BetterVoice overlay")

        self._screen = screen
        self._hud = hud
        self.reduce_motion = reduce_motion
        self.shows_hud = False
        #: True while a screenshot is being taken, so our own drawing stays out
        #: of it. macOS excluded the whole application from the capture; a
        #: portal screenshot takes the screen as it is, so the only way to keep
        #: the trail and HUD out of the picture is not to be drawing them.
        self.suppressed = False
        self._trail: list[_TrailPoint] = []
        self._confirmations: list[_Confirmation] = []
        self.setScreen(screen)
        self.setGeometry(screen.geometry())

    @property
    def origin(self) -> QtCore.QPoint:
        return self._screen.geometry().topLeft()

    @property
    def _hud_baseline(self) -> float:
        """Bottom edge of the usable area, so the HUD clears panels and docks."""

        available = self._screen.availableGeometry()
        return available.bottom() - self._screen.geometry().top()

    def add(self, point: tuple[float, float], at: float) -> None:
        self._trail.append(_TrailPoint(point, at))
        self._prune(at)

    def confirm(self, center: tuple[float, float], radius: float, at: float) -> None:
        self._confirmations.append(_Confirmation(center, radius, at))

    def adopt(self, screen: QtGui.QScreen) -> bool:
        """Re-point a reused window at a screen, or refuse if it is unusable.

        A monitor that comes back gets a fresh ``QScreen``, so the cached
        window has to be told about the new one before it is shown again.
        """

        try:
            self.setScreen(screen)
        except RuntimeError:  # the C++ side was deleted under us
            return False
        self._screen = screen
        return True

    def reset(self) -> None:
        self._trail.clear()
        self._confirmations.clear()

    def tick(self, now: float) -> None:
        self._prune(now)
        self.update()

    def _prune(self, now: float) -> None:
        self._trail = [item for item in self._trail if now - item.time <= TRAIL_LIFETIME]
        self._confirmations = [
            item for item in self._confirmations if now - item.started_at <= CONFIRMATION_LIFETIME
        ]

    def _local(self, point: tuple[float, float]) -> QtCore.QPointF:
        origin = self.origin
        return QtCore.QPointF(point[0] - origin.x(), point[1] - origin.y())

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.GlobalColor.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)

        if self.suppressed:
            painter.end()
            return

        now = time.monotonic()
        self._paint_trail(painter, now)
        self._paint_confirmations(painter, now)
        if self.shows_hud and self._hud.visible:
            self._paint_hud(painter)
        painter.end()

    def _paint_trail(self, painter: QtGui.QPainter, now: float) -> None:
        points = [item.point for item in self._trail]
        times = [item.time for item in self._trail]
        for segment in trail_segments(points, times):
            current = self._trail[segment.end]
            fade = max(0.0, 1 - (now - current.time) / TRAIL_LIFETIME)
            if fade <= 0:
                continue
            start = self._local(self._trail[segment.start].point)
            end = self._local(current.point)

            glow = QtGui.QPen(alpha(CYAN, 0.28 * fade))
            glow.setWidthF(12)
            glow.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(glow)
            painter.drawLine(start, end)

            core = QtGui.QPen(alpha(BLUE, 0.92 * fade))
            core.setWidthF(4.5)
            core.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(core)
            painter.drawLine(start, end)

        if not self._trail:
            return
        head = self._trail[-1]
        fade = max(0.0, 1 - (now - head.time) / TRAIL_LIFETIME)
        if fade <= 0:
            return
        center = self._local(head.point)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(alpha(BLUE, 0.24 * fade))
        painter.drawEllipse(center, 9, 9)
        painter.setBrush(alpha(CYAN, 0.98 * fade))
        painter.drawEllipse(center, 4, 4)

    def _paint_confirmations(self, painter: QtGui.QPainter, now: float) -> None:
        for confirmation in self._confirmations:
            progress = min(1.0, max(0.0, now - confirmation.started_at) / CONFIRMATION_LIFETIME)
            opacity = max(0.0, 0.82 * (1 - progress))
            if opacity <= 0:
                continue
            scale = 1.0 if self.reduce_motion else 0.82 + 0.18 * progress
            radius = confirmation.radius * scale
            center = self._local(confirmation.center)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(alpha(BLUE, opacity * 0.2))
            painter.drawEllipse(center, radius, radius)
            pen = QtGui.QPen(alpha(BLUE, opacity))
            pen.setWidthF(6)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)

    def _paint_hud(self, painter: QtGui.QPainter) -> None:
        self._paint_hud_at(
            painter,
            QtCore.QRectF(
                (self.width() - HUD_WIDTH) / 2,
                self._hud_baseline - HUD_HEIGHT - HUD_BOTTOM_MARGIN,
                HUD_WIDTH,
                HUD_HEIGHT,
            ),
        )

    def _paint_hud_at(self, painter: QtGui.QPainter, area: QtCore.QRectF) -> None:
        """Draw the panel, matching the macOS HUD point for point."""

        hud = self._hud
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        # The macOS panel had a real window shadow; approximate it so the HUD
        # still separates from a busy desktop behind it.
        for spread, shadow in ((7.0, 18), (4.0, 26), (1.5, 34)):
            painter.setBrush(QtGui.QColor(0, 0, 0, shadow))
            painter.drawRoundedRect(
                area.adjusted(-spread, -spread + 2, spread, spread + 2),
                HUD_RADIUS + spread,
                HUD_RADIUS + spread,
            )
        painter.setBrush(HUD_BACKGROUND)
        painter.drawRoundedRect(area, HUD_RADIUS, HUD_RADIUS)

        phase = 0 if self.reduce_motion else int(time.monotonic() * 10)
        shape = (0.45, 0.75, 1.0, 0.7, 0.4)
        bar_colour = BLUE if hud.capture_message is None else CYAN
        painter.setBrush(bar_colour)
        for index, fraction in enumerate(shape):
            pulse = ((phase + index) % 5) * 0.35
            height = 7 + fraction * hud.level * 20 + pulse
            bar = QtCore.QRectF(
                area.left() + 18 + index * 7,
                area.top() + 28 - height / 2,
                3.5,
                height,
            )
            painter.drawRoundedRect(bar, 2, 2)

        # Pixel sizes, not point sizes: the panel is a fixed 290x56 like the
        # macOS one, so the type has to be the same size regardless of the
        # desktop's font settings.
        font = painter.font()
        font.setPixelSize(HUD_TITLE_PIXELS)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(HUD_TITLE)
        painter.drawText(
            QtCore.QRectF(area.left() + 62, area.top() + 8, area.width() - 74, 18),
            int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter),
            hud.title,
        )

        font.setPixelSize(HUD_DETAIL_PIXELS)
        font.setWeight(QtGui.QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(HUD_DETAIL)
        painter.drawText(
            QtCore.QRectF(area.left() + 62, area.top() + 28, area.width() - 74, 18),
            int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter),
            painter.fontMetrics().elidedText(
                hud.detail, QtCore.Qt.TextElideMode.ElideRight, int(area.width() - 74)
            ),
        )


class RecordingOverlay(QtCore.QObject):
    """Owns one overlay window per screen and the shared HUD state."""

    def __init__(self, reduce_motion: bool = False) -> None:
        super().__init__()
        self.reduce_motion = reduce_motion
        self.hud = HUDModel()
        self._windows: list[_OverlayWindow] = []
        # Windows are expensive on Wayland: each one is a fullscreen surface
        # with its own GPU swapchain. Keep them between recordings, keyed by
        # screen name, and only build a new one when a monitor appears.
        self._cache: dict[str, _OverlayWindow] = {}
        self._hud_screen: QtGui.QScreen | None = None
        self._screens_connected = False
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def is_running(self) -> bool:
        return bool(self._windows)

    def start(self, hud_screen: QtGui.QScreen | None = None) -> None:
        self.stop()
        self._hud_screen = hud_screen or QtGui.QGuiApplication.primaryScreen()
        self._build_windows()
        self._connect_screen_changes()
        self._timer.start()

    def _build_windows(self) -> None:
        screens = QtGui.QGuiApplication.screens()
        hud_screen = self._hud_screen
        if hud_screen not in screens:
            hud_screen = QtGui.QGuiApplication.primaryScreen()
        self._evict_stale(screens)
        for screen in screens:
            window = self._window_for(screen)
            window.reduce_motion = self.reduce_motion
            window.shows_hud = screen is hud_screen
            if self.hud.description:
                window.setAccessibleName(self.hud.description)
            window.setGeometry(screen.geometry())
            # Wayland ignores a window's requested position, so the only way to
            # pin an overlay to one output is to ask for fullscreen on it.
            # Without this, a second monitor's overlay lands on the wrong screen.
            window.showFullScreen()
            window.raise_()
            self._windows.append(window)

    def _window_for(self, screen: QtGui.QScreen) -> _OverlayWindow:
        """Reuse this screen's window, or build one the first time we see it."""

        window = self._cache.get(screen.name())
        if window is not None and window.adopt(screen):
            return window
        if window is not None:
            window.deleteLater()
        window = _OverlayWindow(screen, self.hud, self.reduce_motion)
        self._cache[screen.name()] = window
        return window

    def _evict_stale(self, screens: list[QtGui.QScreen]) -> None:
        """Destroy windows for monitors that are no longer attached."""

        live = {screen.name() for screen in screens}
        for name in [name for name in self._cache if name not in live]:
            self._cache.pop(name).deleteLater()

    def _connect_screen_changes(self) -> None:
        if self._screens_connected:
            return
        application = QtGui.QGuiApplication.instance()
        if application is None:
            return
        application.screenAdded.connect(self._on_screens_changed)
        application.screenRemoved.connect(self._on_screens_changed)
        self._screens_connected = True

    def _on_screens_changed(self, _screen: QtGui.QScreen) -> None:
        """Rebuild the overlay when a monitor appears or disappears mid-recording."""

        if not self._windows:
            return
        trail = list(self._windows[0]._trail)
        confirmations = list(self._windows[0]._confirmations)
        self._hide_windows()
        self._build_windows()
        for window in self._windows:
            window._trail = list(trail)
            window._confirmations = list(confirmations)

    def stop(self) -> None:
        self._timer.stop()
        self._hide_windows()

    def _hide_windows(self) -> None:
        """End the recording's display without giving up the windows.

        Destroying and rebuilding a fullscreen surface per screen per recording
        churns compositor buffers hard enough to matter; hiding costs nothing
        and the next recording starts without waiting on a new swapchain.
        """

        for window in self._windows:
            window.reset()
            window.hide()
        self._windows.clear()

    def close(self) -> None:
        """Give up every cached window. For shutdown, not between recordings."""

        self.stop()
        for window in self._cache.values():
            window.hide()
            window.deleteLater()
        self._cache.clear()

    def hide_from_capture(self) -> bool:
        """Blank our own drawing so a screenshot does not contain it.

        Returns whether anything was actually showing. ``repaint()`` rather than
        ``update()``: the cleared frame has to be painted before the capture is
        asked for, not queued behind it.
        """

        if not self._windows:
            return False
        for window in self._windows:
            window.suppressed = True
            window.repaint()
        return True

    def show_after_capture(self) -> None:
        """Put our drawing back once the screenshot has been taken."""

        for window in self._cache.values():
            window.suppressed = False
        for window in self._windows:
            window.update()

    def stop_trail(self) -> None:
        """Clear the drawing but keep the HUD on screen while finishing."""

        for window in self._windows:
            window.reset()

    def announce(self, message: str) -> None:
        """Describe the HUD's state for assistive technology.

        The HUD is painted rather than built from widgets, so there is nothing
        for a screen reader to walk; naming the window is the closest Linux gets
        to the accessibility label the macOS HUD carried.
        """

        self.hud.description = message
        for window in self._windows:
            window.setAccessibleName(message)

    def add(self, point: tuple[float, float], at: float) -> None:
        for window in self._windows:
            window.add(point, at)

    def confirm(self, center: tuple[float, float], radius: float, at: float) -> None:
        for window in self._windows:
            window.confirm(center, radius, at)

    def _tick(self) -> None:
        now = time.monotonic()
        for window in self._windows:
            window.tick(now)
