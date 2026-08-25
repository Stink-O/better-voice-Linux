"""Putting the transcript back into whatever field the user was typing in, and
offering the whole session on the clipboard.

The macOS build used the Accessibility API to send ``Cmd+V`` to the app that had
focus when recording stopped. Linux equivalents, in order of preference:

``portal``
    ``org.freedesktop.portal.RemoteDesktop``. Wayland-native, asks the user once
    (the same shape as the macOS Accessibility prompt) and then injects
    ``Ctrl+V`` through the compositor. No extra packages.
``wtype`` / ``ydotool`` / ``xdotool``
    The usual command-line injectors, if one is installed.

When none is available the transcript still lands on the clipboard and in the
saved session -- exactly what the macOS build does when Accessibility is off.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Callable, ClassVar

from PyQt6 import QtCore, QtDBus

from ..config import config
from . import environment, portal

log = logging.getLogger(__name__)

REMOTE_DESKTOP_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD_INTERFACE = "org.freedesktop.portal.Clipboard"

#: evdev keycodes -- what the RemoteDesktop portal expects.
KEY_LEFTCTRL = 29
KEY_V = 47

#: The portal declares these options as unsigned 32-bit; PyQt would otherwise
#: marshal a plain Python int as signed and the call is rejected outright.
_UINT32 = QtCore.QMetaType.Type.UInt.value


def _uint32(value: int) -> QtDBus.QDBusArgument:
    return QtDBus.QDBusArgument(value, _UINT32)


#: ``SetSelection`` declares ``mime_types`` as ``as``. PyQt marshals a plain
#: Python list inside a variant map as ``av`` -- an array of variants -- and the
#: portal rejects the call outright, so the type has to be spelled out.
_STRING_LIST = QtCore.QMetaType.Type.QStringList.value


def _string_list(values: list[str]) -> QtDBus.QDBusArgument:
    return QtDBus.QDBusArgument(values, _STRING_LIST)

#: Key states, which the portal declares unsigned.
_PRESSED = 1
_RELEASED = 0


class TextInsertionBackend(QtCore.QObject):
    """Sends a paste keystroke to whatever currently has focus."""

    name = "none"

    def prepare(self) -> None:
        """Optional warm-up so the first paste is not delayed by a prompt."""

    @property
    def grant_remembered(self) -> bool:
        """Whether :meth:`prepare` can reconnect without asking the user again.

        Backends that need no permission at all answer False: there is nothing
        to restore, and they are ready without being prepared.
        """

        return False

    def paste(self, on_done: Callable[[bool], None]) -> None:  # pragma: no cover
        on_done(False)

    def set_selection(self, payloads: dict[str, bytes]) -> bool:
        """Offer several clipboard formats at once. False if unsupported here."""

        return False

    @property
    def serves_clipboard(self) -> bool:
        """Whether :meth:`set_selection` can actually claim the clipboard."""

        return False

    @property
    def ready(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str | None:
        return None


class NullTextInsertionBackend(TextInsertionBackend):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def paste(self, on_done):
        on_done(False)


class CommandTextInsertionBackend(TextInsertionBackend):
    TOOLS: ClassVar[dict[str, list[str]]] = {
        "wtype": ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
        "ydotool": ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
        "xdotool": ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
    }

    def __init__(self, tool: str) -> None:
        super().__init__()
        self.name = tool
        self._tool = tool
        self._last_failure: str | None = None

    @staticmethod
    def first_available() -> str | None:
        order = (
            ["wtype", "ydotool", "xdotool"]
            if environment.is_wayland()
            else ["xdotool", "ydotool", "wtype"]
        )
        for tool in order:
            if environment.has(tool):
                return tool
        return None

    @property
    def ready(self) -> bool:
        # The tool being installed is not proof the compositor accepts it --
        # wtype needs virtual-keyboard support, which not every one implements.
        # There is no way to test that without typing somewhere, so believe it
        # until it actually fails, then stop claiming.
        return self._last_failure is None

    @property
    def unavailable_reason(self) -> str | None:
        if self._last_failure is None:
            return None
        return (
            f"{self._tool} could not type into the focused window ({self._last_failure}). "
            "Your compositor may not accept synthetic input from it; the transcript "
            "is on the clipboard instead."
        )

    def paste(self, on_done):
        try:
            result = subprocess.run(
                self.TOOLS[self._tool], capture_output=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("%s could not run: %s", self._tool, error)
            self._last_failure = str(error)
            on_done(False)
            return
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip() or (
                f"exit status {result.returncode}"
            )
            log.warning("%s failed: %s", self._tool, detail)
            self._last_failure = detail
        else:
            self._last_failure = None
        on_done(result.returncode == 0)


class PortalTextInsertionBackend(TextInsertionBackend):
    """Injects Ctrl+V through ``org.freedesktop.portal.RemoteDesktop``."""

    name = "portal"

    RESTORE_TOKEN_KEY = "remoteDesktopRestoreToken"

    #: Persist the grant across restarts so the user is asked at most once.
    PERSIST_UNTIL_REVOKED = 2

    DEVICE_KEYBOARD = 1

    def __init__(self) -> None:
        super().__init__()
        self._bus = QtDBus.QDBusConnection.sessionBus()
        self._session_path: str | None = None
        self._starting = False
        self._pending: list[Callable[[bool], None]] = []
        self._payloads: dict[str, bytes] = {}
        self._clipboard_requested = False
        self._clipboard_ready = False
        self._listening = False

    @staticmethod
    def available() -> bool:
        return portal.available(REMOTE_DESKTOP_INTERFACE)

    @property
    def ready(self) -> bool:
        return self._session_path is not None

    @property
    def unavailable_reason(self) -> str | None:
        if self._session_path is not None:
            return None
        return "Grant BetterVoice remote control once so it can paste for you."

    @property
    def grant_remembered(self) -> bool:
        """True once the portal has handed us a token to restore the session.

        The grant is asked for with ``persist_mode`` "until revoked", so a token
        means the user has already said yes and the portal will hand the session
        straight back -- no second prompt.
        """

        return bool(config().get(self.RESTORE_TOKEN_KEY))

    def prepare(self) -> None:
        if self._session_path is not None or self._starting:
            return
        self._starting = True
        portal.call(
            REMOTE_DESKTOP_INTERFACE,
            "CreateSession",
            [],
            {"session_handle_token": portal.unique_token("rdsession")},
            self._on_session_created,
            self,
        )

    def _on_session_created(self, code: int, results: dict) -> None:
        session = results.get("session_handle")
        if code != portal.SUCCESS or not isinstance(session, str):
            log.info("RemoteDesktop session was not created (code %s)", code)
            self._fail()
            return
        self._session_path = session
        self._listen()
        options: dict = {
            "types": _uint32(self.DEVICE_KEYBOARD),
            "persist_mode": _uint32(self.PERSIST_UNTIL_REVOKED),
        }
        token = config().get(self.RESTORE_TOKEN_KEY)
        if isinstance(token, str) and token:
            options["restore_token"] = token
        portal.call(
            REMOTE_DESKTOP_INTERFACE,
            "SelectDevices",
            [QtDBus.QDBusObjectPath(session)],
            options,
            self._on_devices_selected,
            self,
        )

    def _on_devices_selected(self, code: int, results: dict) -> None:
        if code != portal.SUCCESS or self._session_path is None:
            log.info("RemoteDesktop device selection failed (code %s)", code)
            self._fail()
            return
        self._request_clipboard()
        portal.call(
            REMOTE_DESKTOP_INTERFACE,
            "Start",
            [QtDBus.QDBusObjectPath(self._session_path), ""],
            {},
            self._on_started,
            self,
        )

    def _on_started(self, code: int, results: dict) -> None:
        if code != portal.SUCCESS:
            log.info("RemoteDesktop was declined (code %s)", code)
            self._session_path = None
            # The saved token is what let this be attempted without asking. If
            # the answer was no, the grant is gone -- keeping the token would
            # mean retrying a refused prompt at every launch.
            config().set(self.RESTORE_TOKEN_KEY, "")
            self._fail()
            return
        token = results.get("restore_token")
        if isinstance(token, str) and token:
            config().set(self.RESTORE_TOKEN_KEY, token)
        self._clipboard_ready = self._clipboard_requested
        self._starting = False
        pending, self._pending = self._pending, []
        for callback in pending:
            self._send(callback)

    def _request_clipboard(self) -> None:
        """Ask for clipboard access before Start, which is the only time it is allowed."""

        if self._session_path is None:
            return
        interface = QtDBus.QDBusInterface(
            portal.SERVICE, portal.OBJECT_PATH, CLIPBOARD_INTERFACE, self._bus
        )
        if not interface.isValid():
            return
        reply = interface.call(
            "RequestClipboard", QtDBus.QDBusObjectPath(self._session_path), {}
        )
        if reply.type() == QtDBus.QDBusMessage.MessageType.ErrorMessage:
            log.info("Clipboard access was not granted: %s", reply.errorMessage())
            return
        self._clipboard_requested = True

    def _listen(self) -> None:
        if self._listening:
            return
        self._bus.connect(
            portal.SERVICE,
            portal.OBJECT_PATH,
            CLIPBOARD_INTERFACE,
            "SelectionTransfer",
            self._on_selection_transfer,
        )
        self._listening = True

    @property
    def serves_clipboard(self) -> bool:
        return self._clipboard_ready and self._session_path is not None

    def set_selection(self, payloads: dict[str, bytes]) -> bool:
        """Claim the clipboard, offering every format in ``payloads`` at once."""

        if not self.serves_clipboard or not payloads:
            return False
        self._payloads = dict(payloads)
        interface = QtDBus.QDBusInterface(
            portal.SERVICE, portal.OBJECT_PATH, CLIPBOARD_INTERFACE, self._bus
        )
        reply = interface.call(
            "SetSelection",
            QtDBus.QDBusObjectPath(self._session_path),
            {"mime_types": _string_list(list(payloads))},
        )
        if reply.type() == QtDBus.QDBusMessage.MessageType.ErrorMessage:
            log.warning("Could not claim the clipboard: %s", reply.errorMessage())
            self._payloads = {}
            return False
        return True

    @QtCore.pyqtSlot(QtDBus.QDBusMessage)
    def _on_selection_transfer(self, message: QtDBus.QDBusMessage) -> None:
        """Serve one paste request: the portal hands us a pipe to write into."""

        arguments = message.arguments()
        if len(arguments) < 3 or self._session_path is None:
            return
        session = arguments[0]
        session_path = (
            session.path() if isinstance(session, QtDBus.QDBusObjectPath) else str(session)
        )
        if session_path != self._session_path:
            return
        mime = str(arguments[1])
        serial = int(arguments[2])

        interface = QtDBus.QDBusInterface(
            portal.SERVICE, portal.OBJECT_PATH, CLIPBOARD_INTERFACE, self._bus
        )
        reply = interface.call(
            "SelectionWrite", QtDBus.QDBusObjectPath(self._session_path), _uint32(serial)
        )
        arguments = reply.arguments()
        written = False
        if arguments and isinstance(arguments[0], QtDBus.QDBusUnixFileDescriptor):
            descriptor = os.dup(arguments[0].fileDescriptor())
            payload = self._payloads.get(mime, b"")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                written = True
            except OSError as error:
                log.warning("Could not serve %s to the clipboard: %s", mime, error)
        else:
            log.warning("The clipboard portal did not hand back a pipe: %s", reply.errorMessage())

        interface.call(
            "SelectionWriteDone",
            QtDBus.QDBusObjectPath(self._session_path),
            _uint32(serial),
            written,
        )

    def _fail(self) -> None:
        self._starting = False
        pending, self._pending = self._pending, []
        for callback in pending:
            callback(False)

    def paste(self, on_done):
        if self._session_path is not None:
            self._send(on_done)
            return
        self._pending.append(on_done)
        if not self._starting:
            self.prepare()

    def _send(self, on_done: Callable[[bool], None]) -> None:
        if self._session_path is None:
            on_done(False)
            return
        interface = QtDBus.QDBusInterface(
            portal.SERVICE, portal.OBJECT_PATH, REMOTE_DESKTOP_INTERFACE, self._bus
        )
        session = QtDBus.QDBusObjectPath(self._session_path)
        sequence = (
            (KEY_LEFTCTRL, _PRESSED),
            (KEY_V, _PRESSED),
            (KEY_V, _RELEASED),
            (KEY_LEFTCTRL, _RELEASED),
        )
        for keycode, state in sequence:
            reply = interface.call(
                "NotifyKeyboardKeycode", session, {}, keycode, _uint32(state)
            )
            if reply.type() == QtDBus.QDBusMessage.MessageType.ErrorMessage:
                log.warning("Paste keystroke failed: %s", reply.errorMessage())
                self._session_path = None
                on_done(False)
                return
        on_done(True)

    def stop(self) -> None:
        if self._session_path is None:
            return
        self._bus.call(
            QtDBus.QDBusMessage.createMethodCall(
                portal.SERVICE, self._session_path, "org.freedesktop.portal.Session", "Close"
            ),
            QtDBus.QDBus.CallMode.NoBlock,
        )
        self._session_path = None


def create(preference: str = "auto") -> TextInsertionBackend:
    if preference == "none":
        return NullTextInsertionBackend("Transcript insertion is switched off in settings.")

    if preference not in {"auto", ""}:
        if preference == "portal" and PortalTextInsertionBackend.available():
            return PortalTextInsertionBackend()
        if preference in CommandTextInsertionBackend.TOOLS and environment.has(preference):
            return CommandTextInsertionBackend(preference)
        return NullTextInsertionBackend(
            f"The {preference} insertion backend is not usable in this session."
        )

    tool = CommandTextInsertionBackend.first_available()
    if tool is not None and environment.is_x11():
        return CommandTextInsertionBackend(tool)
    if PortalTextInsertionBackend.available():
        return PortalTextInsertionBackend()
    if tool is not None:
        return CommandTextInsertionBackend(tool)
    return NullTextInsertionBackend(
        "Install wtype (Wayland) or xdotool (X11) so BetterVoice can paste the "
        "transcript for you. Until then it is copied to the clipboard."
    )
