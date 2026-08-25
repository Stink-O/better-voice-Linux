"""Talking to KWin through its scripting interface.

KWin scripts can call out over D-Bus but cannot be called into, so the pattern
throughout is: generate a small script, load it, let it push what it knows back
to an object we exported, and unload it. Two things BetterVoice needs are only
reachable this way on Wayland -- the pointer position and the active window.
"""

from __future__ import annotations

import itertools
import logging
import os
import re

from PyQt6 import QtCore, QtDBus

from ..paths import cache_dir, ensure

log = logging.getLogger(__name__)

_paths = itertools.count(1)

SCRIPTING_SERVICE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
SCRIPT_INTERFACE = "org.kde.kwin.Script"


def available() -> bool:
    bus = QtDBus.QDBusConnection.sessionBus()
    return QtDBus.QDBusInterface(
        SCRIPTING_SERVICE, SCRIPTING_PATH, SCRIPTING_INTERFACE, bus
    ).isValid()


def exported_interface_name(bus: QtDBus.QDBusConnection, service: str, path: str) -> str | None:
    """Read back the D-Bus interface PyQt actually gave one of our objects.

    PyQt derives the name from the Python module and class, so asking the bus is
    more honest than guessing and hard-coding it into the generated script.
    """

    reply = bus.call(
        QtDBus.QDBusMessage.createMethodCall(
            service, path, "org.freedesktop.DBus.Introspectable", "Introspect"
        )
    )
    arguments = reply.arguments()
    if not arguments or not isinstance(arguments[0], str):
        return None
    for name in re.findall(r'<interface name="([^"]+)"', arguments[0]):
        if not name.startswith("org.freedesktop.DBus"):
            return name
    return None


def write_script(name: str, source: str) -> str:
    """Write a generated script where only this user can read it.

    The scripts carry the nonce that tells a callback apart from a forged one,
    so the file must not be readable by anyone else on the machine.
    """

    directory = ensure(cache_dir() / "kwin")
    directory.chmod(0o700)
    path = directory / f"{name}.js"
    path.write_text(source, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def load_and_run(name: str, source: str) -> bool:
    """Install a generated script and run it. False if KWin refused."""

    bus = QtDBus.QDBusConnection.sessionBus()
    scripting = QtDBus.QDBusInterface(
        SCRIPTING_SERVICE, SCRIPTING_PATH, SCRIPTING_INTERFACE, bus
    )
    reply = scripting.call("loadScript", write_script(name, source), name)
    arguments = reply.arguments()
    if not arguments or not isinstance(arguments[0], int) or arguments[0] < 0:
        log.warning("KWin refused the %s script: %s", name, reply.errorMessage())
        return False
    runner = QtDBus.QDBusInterface(
        SCRIPTING_SERVICE, f"{SCRIPTING_PATH}/Script{arguments[0]}", SCRIPT_INTERFACE, bus
    )
    runner.call("run")
    return True


def unload(name: str) -> None:
    bus = QtDBus.QDBusConnection.sessionBus()
    QtDBus.QDBusInterface(
        SCRIPTING_SERVICE, SCRIPTING_PATH, SCRIPTING_INTERFACE, bus
    ).call("unloadScript", name)


def unique_service(suffix: str) -> str:
    return f"io.github.bettervoice.{suffix}{os.getpid()}"


def unique_path(prefix: str) -> str:
    """A D-Bus object path no other instance in this process will claim.

    The service name alone is not enough: two objects in the same process share
    a connection, so a fixed path means the second one silently fails to export
    and its backend quietly does nothing.
    """

    return f"/{prefix}{next(_paths)}"


class ExportedObject(QtCore.QObject):
    """Base for the little objects KWin scripts call back into."""

    def __init__(self, service: str, path: str) -> None:
        super().__init__()
        self._bus = QtDBus.QDBusConnection.sessionBus()
        self._service = service
        self._path = path
        self._registered = False
        #: Why registration failed, for the backend to report rather than hide.
        self.failure: str | None = None

    @property
    def service(self) -> str:
        return self._service

    @property
    def path(self) -> str:
        return self._path

    def register(self) -> str | None:
        """Claim the bus name and export ourselves; returns the interface name."""

        if not self._registered:
            if not self._bus.registerService(self._service):
                self.failure = (
                    f"Another process already owns {self._service} on D-Bus. "
                    "Is a second copy of BetterVoice running?"
                )
                log.warning("Could not take the D-Bus name %s", self._service)
                return None
            if not self._bus.registerObject(
                self._path, self, QtDBus.QDBusConnection.RegisterOption.ExportAllSlots
            ):
                self.failure = f"Could not publish {self._path} on D-Bus."
                log.warning("Could not export %s on D-Bus", self._path)
                return None
            self._registered = True
        self.failure = None
        return exported_interface_name(self._bus, self._service, self._path)
