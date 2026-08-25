"""``org.freedesktop.portal.GlobalShortcuts`` over a dedicated D-Bus connection.

Qt's D-Bus binding cannot marshal the ``a(sa{sv})`` argument this portal takes,
and the whole flow -- session, binding, and the ``Activated``/``Deactivated``
signals -- has to happen on one connection, so it all lives here on jeepney and
is handed to the GUI thread through Qt signals.

Compositors report a key press and its release separately, which is what makes
hold-to-talk possible without reading raw input devices.
"""

from __future__ import annotations

import logging
import threading

from PyQt6 import QtCore

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

PUSH_TO_TALK_ID = "push-to-talk"
LONG_FORM_ID = "long-form"

#: XDG shortcut syntax: LOGO is the Super/Meta key.
DEFAULT_PUSH_TO_TALK_TRIGGER = "ALT+v"
DEFAULT_LONG_FORM_TRIGGER = "LOGO+ALT+v"

_RESPONSE_TIMEOUT = 60.0

#: XDG shortcut syntax spelled the way a keyboard is labelled.
_KEY_NAMES = {
    "LOGO": "Super",
    "SUPER": "Super",
    "META": "Super",
    "ALT": "Alt",
    "CTRL": "Ctrl",
    "CONTROL": "Ctrl",
    "SHIFT": "Shift",
    "SPACE": "Space",
}


def friendly_trigger(trigger: str) -> str:
    """``LOGO+ALT+v`` -> ``Super + Alt + V``."""

    if not trigger:
        return ""
    parts = []
    for raw in trigger.split("+"):
        key = raw.strip()
        if not key:
            continue
        parts.append(_KEY_NAMES.get(key.upper(), key.upper() if len(key) == 1 else key))
    return " + ".join(parts)


def available() -> bool:
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False
    try:
        connection = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        address = DBusAddress(
            PORTAL_PATH, bus_name=PORTAL_BUS, interface="org.freedesktop.DBus.Properties"
        )
        reply = connection.send_and_get_reply(
            new_method_call(address, "Get", "ss", (INTERFACE, "version")),
            timeout=5,
        )
        return reply.header.message_type.name == "method_return"
    except Exception:
        return False
    finally:
        connection.close()


class GlobalShortcutsClient(QtCore.QObject):
    """Owns the portal session and republishes its signals on the GUI thread."""

    activated = QtCore.pyqtSignal(str)
    deactivated = QtCore.pyqtSignal(str)
    bindings_changed = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(
        self,
        push_to_talk_trigger: str = DEFAULT_PUSH_TO_TALK_TRIGGER,
        long_form_trigger: str = DEFAULT_LONG_FORM_TRIGGER,
    ) -> None:
        super().__init__()
        self._push_trigger = push_to_talk_trigger
        self._long_trigger = long_form_trigger
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection = None
        self.triggers: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bettervoice-shortcuts", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover - already closed
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    # -- worker thread ---------------------------------------------------

    def _run(self) -> None:  # pragma: no cover - needs a live portal
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import Proxy, open_dbus_connection

        try:
            connection = open_dbus_connection(bus="SESSION")
        except Exception as error:
            self.failed.emit(f"Could not reach the session bus: {error}")
            return
        self._connection = connection
        portal = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=INTERFACE)

        try:
            bus = Proxy(message_bus, connection)
            bus.AddMatch(
                MatchRule(type="signal", interface=REQUEST_INTERFACE, member="Response")
            )
            bus.AddMatch(MatchRule(type="signal", interface=INTERFACE))

            session = self._create_session(connection, portal, new_method_call)
            if session is None:
                return
            self._bind(connection, portal, session, new_method_call)
            self._listen(connection, session)
        except Exception as error:
            if not self._stop.is_set():
                log.warning("Global shortcuts stopped: %s", error)
                self.failed.emit(str(error))
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _create_session(self, connection, portal, new_method_call):
        reply = connection.send_and_get_reply(
            new_method_call(
                portal,
                "CreateSession",
                "a{sv}",
                (
                    {
                        "handle_token": ("s", "bettervoice_shortcuts"),
                        "session_handle_token": ("s", "bettervoice_session"),
                    },
                ),
            ),
            timeout=15,
        )
        if reply.header.message_type.name != "method_return":
            message = reply.body[0] if reply.body else "unknown error"
            self.failed.emit(self._explain(str(message)))
            return None

        response = self._await_response(connection)
        if response is None:
            return None
        code, results = response
        if code != 0:
            self.failed.emit("The desktop declined the global shortcuts session.")
            return None
        handle = results.get("session_handle", (None, None))[1]
        return handle

    def _bind(self, connection, portal, session, new_method_call) -> None:
        shortcuts = [
            (
                PUSH_TO_TALK_ID,
                {
                    "description": ("s", "BetterVoice: hold for a quick note"),
                    "preferred_trigger": ("s", self._push_trigger),
                },
            ),
            (
                LONG_FORM_ID,
                {
                    "description": ("s", "BetterVoice: toggle a long explanation"),
                    "preferred_trigger": ("s", self._long_trigger),
                },
            ),
        ]
        reply = connection.send_and_get_reply(
            new_method_call(
                portal,
                "BindShortcuts",
                "oa(sa{sv})sa{sv}",
                (session, shortcuts, "", {"handle_token": ("s", "bettervoice_bind")}),
            ),
            timeout=15,
        )
        if reply.header.message_type.name != "method_return":
            message = reply.body[0] if reply.body else "unknown error"
            self.failed.emit(self._explain(str(message)))
            return
        response = self._await_response(connection)
        if response is None:
            return
        code, results = response
        if code != 0:
            self.failed.emit("The desktop declined the shortcut registration.")
            return
        self._publish_triggers(results.get("shortcuts", (None, []))[1] or [])

    def _publish_triggers(self, shortcuts) -> None:
        triggers = {}
        for identifier, properties in shortcuts:
            description = properties.get("trigger_description", ("s", ""))[1]
            triggers[identifier] = description
        self.triggers = triggers
        self.bindings_changed.emit(dict(triggers))

    def _await_response(self, connection):
        import time

        deadline = time.monotonic() + _RESPONSE_TIMEOUT
        while not self._stop.is_set() and time.monotonic() < deadline:
            message = self._receive(connection)
            if message is None:
                continue
            if message.header.fields.get(3) != "Response":
                self._handle_signal(message)
                continue
            return message.body
        return None

    def _listen(self, connection, session) -> None:
        while not self._stop.is_set():
            message = self._receive(connection)
            if message is not None:
                self._handle_signal(message, session)

    @staticmethod
    def _receive(connection):
        try:
            return connection.receive(timeout=1)
        except TimeoutError:
            return None
        except Exception:
            raise

    def _handle_signal(self, message, session: str | None = None) -> None:
        member = message.header.fields.get(3)
        if member not in {"Activated", "Deactivated", "ShortcutsChanged"}:
            return
        body = message.body
        if not body:
            return
        if session is not None and str(body[0]) != session:
            return
        if member == "ShortcutsChanged":
            self._publish_triggers(body[1] if len(body) > 1 else [])
            return
        if len(body) < 2:
            return
        identifier = str(body[1])
        if member == "Activated":
            self.activated.emit(identifier)
        else:
            self.deactivated.emit(identifier)

    @staticmethod
    def _explain(message: str) -> str:
        if "app id" in message.lower():
            return (
                "The desktop could not identify BetterVoice, so it will not grant "
                "global shortcuts. Install it with ./install.sh and launch it from "
                "your application menu."
            )
        return message
