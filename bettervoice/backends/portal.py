"""Thin helper for the XDG desktop portal request/response dance.

Portal calls return a ``Request`` object path and deliver the real answer later
on that object's ``Response`` signal, so every call needs a listener wired up
before it is made. This module hides that.
"""

from __future__ import annotations

import itertools
import logging
import os
from typing import Callable

from PyQt6 import QtCore, QtDBus

log = logging.getLogger(__name__)

SERVICE = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

#: Portal response codes.
SUCCESS = 0
CANCELLED = 1
FAILED = 2

_counter = itertools.count(1)


def available(interface: str) -> bool:
    bus = QtDBus.QDBusConnection.sessionBus()
    return QtDBus.QDBusInterface(SERVICE, OBJECT_PATH, interface, bus).isValid()


def unique_token(prefix: str = "bettervoice") -> str:
    return f"{prefix}_{os.getpid()}_{next(_counter)}"


class PortalRequest(QtCore.QObject):
    """One in-flight portal request.

    Keep a reference alive until ``finished`` fires; the object disconnects
    itself once the portal answers.
    """

    finished = QtCore.pyqtSignal(int, dict)

    def __init__(
        self,
        interface: str,
        method: str,
        arguments: list,
        options: dict,
        parent: QtCore.QObject | None = None,
        timeout_ms: int = 120_000,
    ) -> None:
        super().__init__(parent)
        self._bus = QtDBus.QDBusConnection.sessionBus()
        self._token = unique_token()
        self._interface = interface
        self._method = method
        self._arguments = arguments
        self._options = dict(options)
        self._options["handle_token"] = self._token
        self._done = False

        unique_name = self._bus.baseService().lstrip(":").replace(".", "_")
        self._request_path = f"{OBJECT_PATH}/request/{unique_name}/{self._token}"
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self._on_timeout)

    def call(self) -> None:
        connected = self._bus.connect(
            SERVICE,
            self._request_path,
            REQUEST_INTERFACE,
            "Response",
            self._on_response,
        )
        if not connected:
            log.warning("Could not listen for the portal response to %s", self._method)
            self._emit(FAILED, {})
            return

        interface = QtDBus.QDBusInterface(SERVICE, OBJECT_PATH, self._interface, self._bus)
        reply = interface.call(self._method, *self._arguments, self._options)
        if reply.type() == QtDBus.QDBusMessage.MessageType.ErrorMessage:
            log.warning("Portal call %s failed: %s", self._method, reply.errorMessage())
            self._emit(FAILED, {})
            return
        self._timeout.start()

    @QtCore.pyqtSlot(QtDBus.QDBusMessage)
    def _on_response(self, message: QtDBus.QDBusMessage) -> None:
        arguments = message.arguments()
        code = int(arguments[0]) if arguments else FAILED
        results = arguments[1] if len(arguments) > 1 and isinstance(arguments[1], dict) else {}
        self._emit(code, results)

    def _on_timeout(self) -> None:
        log.warning("Portal call %s timed out", self._method)
        self._emit(FAILED, {})

    def _emit(self, code: int, results: dict) -> None:
        if self._done:
            return
        self._done = True
        self._timeout.stop()
        self._bus.disconnect(
            SERVICE,
            self._request_path,
            REQUEST_INTERFACE,
            "Response",
            self._on_response,
        )
        self.finished.emit(code, results)


def call(
    interface: str,
    method: str,
    arguments: list,
    options: dict,
    on_finished: Callable[[int, dict], None],
    parent: QtCore.QObject | None = None,
    timeout_ms: int = 120_000,
) -> PortalRequest:
    request = PortalRequest(interface, method, arguments, options, parent, timeout_ms)
    holder: list[PortalRequest] = [request]

    def _forward(code: int, results: dict) -> None:
        holder.clear()
        on_finished(code, results)

    request.finished.connect(_forward)
    request.call()
    return request
