"""Screen capture with the referenced area highlighted.

Replaces ScreenCaptureKit. Backends, in order of preference:

``portal``
    ``org.freedesktop.portal.Screenshot``. The Wayland-native route: the
    compositor asks the user once, then remembers -- the same shape as the macOS
    Screen Recording permission.
``grim``
    wlroots compositors (Sway, Hyprland) where the portal is absent.
``spectacle`` / ``gnome-screenshot`` / ``maim`` / ``import``
    Whatever the desktop already ships.
``x11``
    Qt's own ``grabWindow`` on the root window.

Every backend returns the whole display beneath the pointer, matching the macOS
behaviour, and the highlight is painted on afterwards.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, ClassVar
from urllib.parse import unquote, urlparse

from PyQt6 import QtCore, QtGui

from ..core import CircleGesture
from ..errors import ScreenPermissionRequired, ScreenshotUnavailable
from . import environment, portal

log = logging.getLogger(__name__)

SCREENSHOT_INTERFACE = "org.freedesktop.portal.Screenshot"

#: How long to wait for one capture before giving up on it.
CAPTURE_TIMEOUT_MS = 20_000

#: Matches the macOS highlight: a soft cyan-to-blue glow with a firm blue ring.
_GLOW_INNER = QtGui.QColor(50, 199, 222, int(0.18 * 255))
_GLOW_MIDDLE = QtGui.QColor(0, 122, 255, int(0.12 * 255))
_GLOW_OUTER = QtGui.QColor(0, 122, 255, 0)
_RING = QtGui.QColor(0, 122, 255, int(0.9 * 255))


def virtual_geometry() -> QtCore.QRect:
    geometry = QtCore.QRect()
    for screen in QtGui.QGuiApplication.screens():
        geometry = geometry.united(screen.geometry())
    return geometry


def screen_at(point: tuple[float, float]) -> QtGui.QScreen | None:
    qpoint = QtCore.QPoint(int(point[0]), int(point[1]))
    for screen in QtGui.QGuiApplication.screens():
        if screen.geometry().contains(qpoint):
            return screen
    return QtGui.QGuiApplication.primaryScreen()


def highlight(
    image: QtGui.QImage,
    *,
    target: tuple[float, float],
    region: QtCore.QRectF,
    radius: float,
) -> QtGui.QImage:
    """Draw the blue reference marker, mirroring ``ScreenshotCapture.highlight``."""

    marked = image.convertToFormat(QtGui.QImage.Format.Format_ARGB32)
    if region.width() <= 0 or region.height() <= 0:
        return marked

    scale_x = marked.width() / region.width()
    scale_y = marked.height() / region.height()
    x = (target[0] - region.left()) * scale_x
    y = (target[1] - region.top()) * scale_y
    marked_radius = max(24.0, radius * min(scale_x, scale_y))

    painter = QtGui.QPainter(marked)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

    gradient = QtGui.QRadialGradient(QtCore.QPointF(x, y), marked_radius * 1.35)
    gradient.setColorAt(0.0, _GLOW_INNER)
    gradient.setColorAt(0.68, _GLOW_MIDDLE)
    gradient.setColorAt(1.0, _GLOW_OUTER)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QBrush(gradient))
    painter.drawEllipse(
        QtCore.QPointF(x, y), marked_radius * 1.35, marked_radius * 1.35
    )

    pen = QtGui.QPen(_RING)
    pen.setWidthF(max(4.0, marked_radius * 0.055))
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QtCore.QPointF(x, y), marked_radius, marked_radius)
    painter.end()
    return marked


def _crop_to_screen(
    image: QtGui.QImage, gesture_center: tuple[float, float]
) -> tuple[QtGui.QImage, QtCore.QRectF]:
    """Reduce a whole-workspace grab to the display beneath the pointer."""

    virtual = virtual_geometry()
    screen = screen_at(gesture_center)
    if screen is None or virtual.isEmpty():
        return image, QtCore.QRectF(virtual)

    region = QtCore.QRectF(screen.geometry())
    if image.width() == region.width() and image.height() == region.height():
        return image, region

    scale_x = image.width() / virtual.width()
    scale_y = image.height() / virtual.height()
    crop = QtCore.QRect(
        round((region.left() - virtual.left()) * scale_x),
        round((region.top() - virtual.top()) * scale_y),
        round(region.width() * scale_x),
        round(region.height() * scale_y),
    ).intersected(image.rect())
    if crop.isEmpty():
        return image, QtCore.QRectF(virtual)
    return image.copy(crop), region


class ScreenshotBackend(QtCore.QObject):
    name = "none"

    def capture(
        self,
        gesture: CircleGesture,
        destination: Path,
        on_done: Callable[[Exception | None], None],
    ) -> None:  # pragma: no cover - overridden
        on_done(ScreenshotUnavailable())

    @property
    def unavailable_reason(self) -> str | None:
        return None


class NullScreenshotBackend(ScreenshotBackend):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def capture(self, gesture, destination, on_done):
        on_done(ScreenshotUnavailable())


def _finish(
    image: QtGui.QImage, gesture: CircleGesture, destination: Path
) -> Exception | None:
    if image.isNull():
        return ScreenshotUnavailable()
    cropped, region = _crop_to_screen(image, gesture.center)
    marked = highlight(cropped, target=gesture.center, region=region, radius=gesture.radius)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not marked.save(str(destination), "PNG"):
        return ScreenshotUnavailable()
    return None


class PortalScreenshotBackend(ScreenshotBackend):
    name = "portal"

    @staticmethod
    def available() -> bool:
        return portal.available(SCREENSHOT_INTERFACE)

    def capture(self, gesture, destination, on_done):
        def handle(code: int, results: dict) -> None:
            if code == portal.CANCELLED:
                on_done(ScreenPermissionRequired())
                return
            if code != portal.SUCCESS:
                on_done(ScreenshotUnavailable())
                return
            uri = results.get("uri")
            if not isinstance(uri, str):
                on_done(ScreenshotUnavailable())
                return
            source = Path(unquote(urlparse(uri).path))
            image = QtGui.QImage(str(source))
            error = _finish(image, gesture, destination)
            # The portal writes its own copy into the pictures folder; the
            # session folder is the only place BetterVoice should leave files.
            try:
                source.unlink()
            except OSError:
                pass
            on_done(error)

        portal.call(
            SCREENSHOT_INTERFACE,
            "Screenshot",
            [""],
            {"interactive": False},
            handle,
            self,
            # A capture happens mid-recording; the session must never sit
            # waiting on the portal's default two-minute window.
            timeout_ms=CAPTURE_TIMEOUT_MS,
        )


class CommandScreenshotBackend(ScreenshotBackend):
    """Shells out to whichever capture tool the desktop already provides."""

    #: name -> argv builder taking the output path.
    TOOLS: ClassVar[dict[str, Callable[[str], list[str]]]] = {
        "grim": lambda path: ["grim", path],
        "spectacle": lambda path: ["spectacle", "-b", "-n", "-f", "-o", path],
        "gnome-screenshot": lambda path: ["gnome-screenshot", "-f", path],
        "maim": lambda path: ["maim", path],
        "import": lambda path: ["import", "-window", "root", path],
    }

    def __init__(self, tool: str) -> None:
        super().__init__()
        self.name = tool
        self._tool = tool

    @staticmethod
    def first_available() -> str | None:
        for tool in CommandScreenshotBackend.TOOLS:
            if tool in {"maim", "import"} and not environment.is_x11():
                continue
            if environment.has(tool):
                return tool
        return None

    def capture(self, gesture, destination, on_done):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            result = subprocess.run(
                self.TOOLS[self._tool](str(temporary)),
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                log.warning(
                    "%s failed: %s", self._tool, result.stderr.decode(errors="replace").strip()
                )
                on_done(ScreenshotUnavailable())
                return
            on_done(_finish(QtGui.QImage(str(temporary)), gesture, destination))
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("%s could not run: %s", self._tool, error)
            on_done(ScreenshotUnavailable())
        finally:
            temporary.unlink(missing_ok=True)


class X11ScreenshotBackend(ScreenshotBackend):
    name = "x11"

    @staticmethod
    def available() -> bool:
        return environment.is_x11()

    def capture(self, gesture, destination, on_done):
        screen = screen_at(gesture.center)
        if screen is None:
            on_done(ScreenshotUnavailable())
            return
        region = screen.geometry()
        pixmap = screen.grabWindow(0, region.x(), region.y(), region.width(), region.height())
        on_done(_finish(pixmap.toImage(), gesture, destination))


def create(preference: str = "auto") -> ScreenshotBackend:
    if preference == "none":
        return NullScreenshotBackend("Screen capture is switched off in settings.")

    if preference not in {"auto", ""}:
        if preference == "portal" and PortalScreenshotBackend.available():
            return PortalScreenshotBackend()
        if preference == "x11" and X11ScreenshotBackend.available():
            return X11ScreenshotBackend()
        if preference in CommandScreenshotBackend.TOOLS and environment.has(preference):
            return CommandScreenshotBackend(preference)
        return NullScreenshotBackend(
            f"The {preference} screenshot backend is not usable in this session."
        )

    if PortalScreenshotBackend.available():
        return PortalScreenshotBackend()
    tool = CommandScreenshotBackend.first_available()
    if tool is not None:
        return CommandScreenshotBackend(tool)
    if X11ScreenshotBackend.available():
        return X11ScreenshotBackend()
    return NullScreenshotBackend(
        "No screen capture backend is available. Install xdg-desktop-portal for your "
        "desktop, or grim on wlroots compositors."
    )
