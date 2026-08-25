"""Following the desktop's own accessibility preferences.

The macOS build asked ``NSWorkspace`` whether the user had asked for reduced
motion, and turned off the tray pulse and the capture animation when they had.
The Linux equivalent is the ``org.freedesktop.portal.Settings`` interface, which
both GNOME and KDE answer -- KDE maps its own animation-speed slider onto the
same key -- with KDE's config file as a fallback when the portal is absent.
"""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path

from PyQt6 import QtCore, QtDBus

log = logging.getLogger(__name__)

SETTINGS_INTERFACE = "org.freedesktop.portal.Settings"
ANIMATIONS_NAMESPACE = "org.gnome.desktop.interface"
ANIMATIONS_KEY = "enable-animations"


def _portal_animations_enabled() -> bool | None:
    bus = QtDBus.QDBusConnection.sessionBus()
    interface = QtDBus.QDBusInterface(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        SETTINGS_INTERFACE,
        bus,
    )
    if not interface.isValid():
        return None
    reply = interface.call("Read", ANIMATIONS_NAMESPACE, ANIMATIONS_KEY)
    if reply.type() == QtDBus.QDBusMessage.MessageType.ErrorMessage:
        return None
    arguments = reply.arguments()
    if not arguments:
        return None
    value = arguments[0]
    # The reply is a variant wrapping a variant wrapping the boolean.
    for _ in range(3):
        if isinstance(value, bool):
            return value
        if isinstance(value, QtDBus.QDBusVariant):
            value = value.variant()
        else:
            break
    return bool(value) if isinstance(value, (bool, int)) else None


def _kde_animations_enabled() -> bool | None:
    """KDE writes an animation-speed multiplier; zero means no animation."""

    base = os.environ.get("XDG_CONFIG_HOME")
    candidates = [
        (Path(base) if base else Path.home() / ".config") / "kdeglobals",
        Path("/etc/xdg/kdeglobals"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        raw = parser.get("KDE", "AnimationDurationFactor", fallback=None)
        if raw is None:
            continue
        try:
            return float(raw) > 0
        except ValueError:
            continue
    return None


def reduce_motion(override: object = "auto") -> bool:
    """Whether to hold still. ``override`` may be ``True``, ``False`` or ``"auto"``."""

    if isinstance(override, bool):
        return override
    if isinstance(override, str) and override.lower() in {"true", "yes", "on"}:
        return True
    if isinstance(override, str) and override.lower() in {"false", "no", "off"}:
        return False

    for probe in (_portal_animations_enabled, _kde_animations_enabled):
        enabled = probe()
        if enabled is not None:
            return not enabled
    return False


class AppearanceWatcher(QtCore.QObject):
    """Emits when the desktop's reduced-motion preference changes."""

    reduce_motion_changed = QtCore.pyqtSignal(bool)

    def __init__(self, override: object = "auto") -> None:
        super().__init__()
        self._override = override
        self._current = reduce_motion(override)
        QtDBus.QDBusConnection.sessionBus().connect(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            SETTINGS_INTERFACE,
            "SettingChanged",
            self._on_setting_changed,
        )

    @property
    def reduce_motion(self) -> bool:
        return self._current

    def set_override(self, override: object) -> None:
        self._override = override
        self._recheck()

    @QtCore.pyqtSlot(QtDBus.QDBusMessage)
    def _on_setting_changed(self, message: QtDBus.QDBusMessage) -> None:
        arguments = message.arguments()
        if len(arguments) < 2:
            return
        if str(arguments[0]) != ANIMATIONS_NAMESPACE or str(arguments[1]) != ANIMATIONS_KEY:
            return
        self._recheck()

    def _recheck(self) -> None:
        value = reduce_motion(self._override)
        if value == self._current:
            return
        self._current = value
        self.reduce_motion_changed.emit(value)
