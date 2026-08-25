"""The tray icon and its menu -- the Linux stand-in for the macOS menu bar item.

On KDE and GNOME this becomes a StatusNotifierItem, so the menu is a real native
menu rather than a drawn popup, and the icon follows the panel's colour scheme.
"""

from __future__ import annotations

import enum
from typing import Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME
from . import icons


class StatusIconState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINISHING = "finishing"


PULSE_INTERVAL_MS = 450


class Tray(QtCore.QObject):
    """Owns the tray icon, its menu, and the status line at the top of it."""

    def __init__(self, reduce_motion: bool = False) -> None:
        super().__init__()
        self.reduce_motion = reduce_motion
        self._icon = QtWidgets.QSystemTrayIcon()
        self._icon.setToolTip(APP_NAME)
        self._menu = QtWidgets.QMenu()

        self.status_action = self._menu.addAction("Ready")
        self.status_action.setEnabled(False)
        self._menu.addSeparator()

        self.recording_action = self._menu.addAction("Start long recording")
        self.model_action = self._menu.addAction("Download Local Model")

        self.microphone_menu = QtWidgets.QMenu("Microphone")
        self._menu.addMenu(self.microphone_menu)

        self._menu.addSeparator()
        self.setup_action = self._menu.addAction("Getting Started…")
        self.open_sessions_action = self._menu.addAction("Open Saved Sessions")
        self.clear_sessions_action = self._menu.addAction("Clear Saved Sessions…")
        self._menu.addSeparator()
        self.quit_action = self._menu.addAction("Quit BetterVoice")

        # The macOS menu showed ⌘, and ⌘Q beside these; Ctrl is the Linux
        # equivalent, and the accelerators work while the menu is open.
        self.setup_action.setShortcut(QtGui.QKeySequence("Ctrl+,"))
        # Spelled out rather than StandardKey.Quit, which resolves through the
        # platform theme and comes back empty on some Qt platforms.
        self.quit_action.setShortcut(QtGui.QKeySequence("Ctrl+Q"))
        for action in (self.setup_action, self.quit_action):
            action.setShortcutVisibleInContextMenu(True)

        self._icon.setContextMenu(self._menu)
        self._menu.aboutToShow.connect(self._on_about_to_show)
        self.on_menu_open: Callable[[], None] | None = None

        self._pulse = QtCore.QTimer(self)
        self._pulse.setInterval(PULSE_INTERVAL_MS)
        self._pulse.timeout.connect(self._on_pulse)
        self._pulse_on = False
        self._state = StatusIconState.IDLE
        self.set_icon(StatusIconState.IDLE)

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._pulse.stop()
        self._icon.hide()

    @staticmethod
    def is_available() -> bool:
        return QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()

    def _on_about_to_show(self) -> None:
        if self.on_menu_open is not None:
            self.on_menu_open()

    def set_status(self, message: str) -> None:
        self.status_action.setText(message)
        self._icon.setToolTip(f"{APP_NAME} — {message}")

    def set_icon(self, state: StatusIconState) -> None:
        self._state = state
        self._pulse.stop()
        if state is StatusIconState.RECORDING:
            self._pulse_on = False
            self._icon.setIcon(icons.waveform_in_circle(filled=True))
            if not self.reduce_motion:
                self._pulse.start()
        else:
            self._icon.setIcon(icons.waveform())

    def _on_pulse(self) -> None:
        if self._state is not StatusIconState.RECORDING:
            self._pulse.stop()
            return
        self._pulse_on = not self._pulse_on
        self._icon.setIcon(icons.waveform_in_circle(filled=not self._pulse_on))

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        self._icon.showMessage(
            title,
            message,
            QtWidgets.QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QtWidgets.QSystemTrayIcon.MessageIcon.Information,
            5_000,
        )

    def rebuild_microphone_menu(
        self,
        options: list[tuple[str, str]],
        selected: str,
        enabled: bool,
        on_select: Callable[[str], None],
    ) -> None:
        self.microphone_menu.clear()
        if not options:
            action = self.microphone_menu.addAction("No input microphones found")
            action.setEnabled(False)
            return
        group = QtGui.QActionGroup(self.microphone_menu)
        group.setExclusive(True)
        for index, (identifier, name) in enumerate(options):
            action = self.microphone_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(identifier == selected)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked, key=identifier: on_select(key))
            group.addAction(action)
            if index == 0 and len(options) > 1:
                self.microphone_menu.addSeparator()
