"""Global recording shortcuts -- the Linux answer to ``NSEvent`` flag monitoring.

Two shapes of backend, picked by what the session allows:

*Modifier backends* (``x11``, ``evdev``) watch the raw modifier keys and drive
:class:`~bettervoice.core.RecordingShortcutState` directly, so they reproduce the
macOS feel exactly -- hold **Alt** for a quick note, press **Super+Alt** for a
long explanation, and add Super mid-hold to promote a quick note.

*The portal backend* (``portal``) asks the compositor for two named shortcuts
through ``org.freedesktop.portal.GlobalShortcuts``. Wayland compositors will not
report a bare modifier, so these are real chords (**Alt+V** held, **Super+Alt+V**
to toggle) that the user can rebind in their desktop's shortcut editor.
"""

from __future__ import annotations

import importlib.util
import logging
import threading

from PyQt6 import QtCore

from ..core import RecordingShortcutAction as Action
from ..core import RecordingShortcutState
from . import environment, global_shortcuts, input_devices

log = logging.getLogger(__name__)

DEFAULT_PUSH_TO_TALK_TRIGGER = global_shortcuts.DEFAULT_PUSH_TO_TALK_TRIGGER
DEFAULT_LONG_FORM_TRIGGER = global_shortcuts.DEFAULT_LONG_FORM_TRIGGER

#: How long Alt must be held before a quick note starts, matching macOS.
PUSH_TO_TALK_DELAY_MS = 140

def _has_evdev() -> bool:
    return importlib.util.find_spec("evdev") is not None


#: evdev keycodes for the modifiers the shortcut state machine cares about.
#: Hard-coded so the mapping can be tested without readable input devices;
#: they are ABI-stable in ``linux/input-event-codes.h``.
ALT_KEYS = frozenset({56, 100})  # KEY_LEFTALT, KEY_RIGHTALT
SUPER_KEYS = frozenset({125, 126})  # KEY_LEFTMETA, KEY_RIGHTMETA
OTHER_MODIFIER_KEYS = frozenset({42, 54, 29, 97})  # shift and control, both sides

#: X11 modifier mask bits: Shift, Control, Mod1 (Alt), Mod4 (Super).
X11_SHIFT_MASK = 1 << 0
X11_CONTROL_MASK = 1 << 2
X11_ALT_MASK = 1 << 3
X11_SUPER_MASK = 1 << 6


def flags_from_held_keys(held: set[int] | frozenset[int]) -> tuple[bool, bool, bool]:
    """Turn a set of held evdev keycodes into ``(super, alt, other_modifier)``."""

    return (
        bool(held & SUPER_KEYS),
        bool(held & ALT_KEYS),
        bool(held & OTHER_MODIFIER_KEYS),
    )


def flags_from_x11_mask(mask: int) -> tuple[bool, bool, bool]:
    """Turn an X11 modifier mask into ``(super, alt, other_modifier)``."""

    return (
        bool(mask & X11_SUPER_MASK),
        bool(mask & X11_ALT_MASK),
        bool(mask & (X11_SHIFT_MASK | X11_CONTROL_MASK)),
    )


class HotkeyBackend(QtCore.QObject):
    push_to_talk_started = QtCore.pyqtSignal()
    push_to_talk_stopped = QtCore.pyqtSignal()
    long_form_toggled = QtCore.pyqtSignal()
    promote_to_long_form = QtCore.pyqtSignal()

    name = "none"

    def start(self) -> None:  # pragma: no cover - overridden
        pass

    def stop(self) -> None:  # pragma: no cover - overridden
        pass

    @property
    def unavailable_reason(self) -> str | None:
        return None

    @property
    def shortcut_hint(self) -> str:
        return "hold Alt, or press Super + Alt"


class NullHotkeyBackend(HotkeyBackend):
    name = "none"

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    @property
    def shortcut_hint(self) -> str:
        return "use the tray menu"


class _ModifierBackend(HotkeyBackend):
    """Shared plumbing for backends that see raw modifier keys."""

    def __init__(self) -> None:
        super().__init__()
        self._shortcut = RecordingShortcutState()
        self._pending = QtCore.QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(PUSH_TO_TALK_DELAY_MS)
        self._pending.timeout.connect(self._on_pending_elapsed)

    def _reset(self) -> None:
        self._shortcut = RecordingShortcutState()
        self._pending.stop()

    @QtCore.pyqtSlot(bool, bool, bool)
    def _flags_changed(self, super_: bool, alt: bool, other: bool) -> None:
        self._apply(
            self._shortcut.flags_changed(super_=super_, alt=alt, other_modifier=other)
        )

    def _on_pending_elapsed(self) -> None:
        self._apply(self._shortcut.push_to_talk_delay_elapsed())

    def _apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action is Action.SCHEDULE_PUSH_TO_TALK:
                self._pending.start()
            elif action is Action.CANCEL_PENDING_PUSH_TO_TALK:
                self._pending.stop()
            elif action is Action.START_PUSH_TO_TALK:
                self.push_to_talk_started.emit()
            elif action is Action.STOP_PUSH_TO_TALK:
                self.push_to_talk_stopped.emit()
            elif action is Action.TOGGLE_LONG_FORM:
                self.long_form_toggled.emit()
            elif action is Action.PROMOTE_TO_LONG_FORM:
                self.promote_to_long_form.emit()


class X11HotkeyBackend(_ModifierBackend):
    """Polls the X server's modifier mask; no grab, so nothing is swallowed."""

    name = "x11"

    def __init__(self, interval_ms: int = 16) -> None:
        super().__init__()
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._sample)
        self._display = None
        self._root = None
        self._last: tuple[bool, bool, bool] | None = None

    @staticmethod
    def available() -> bool:
        if not environment.is_x11():
            return False
        return importlib.util.find_spec("Xlib") is not None

    def start(self) -> None:
        from Xlib import display as xdisplay  # imported here so X11 stays optional

        self._reset()
        self._last = None
        self._display = xdisplay.Display()
        self._root = self._display.screen().root
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._display is not None:
            try:
                self._display.close()
            except Exception:  # pragma: no cover
                pass
        self._display = None
        self._root = None

    def _sample(self) -> None:
        if self._root is None:
            return
        try:
            mask = self._root.query_pointer().mask
        except Exception as error:  # pragma: no cover
            log.warning("Modifier poll failed: %s", error)
            self.stop()
            return
        flags = flags_from_x11_mask(mask)
        if flags == self._last:
            return
        self._last = flags
        self._flags_changed(*flags)


class EvdevHotkeyBackend(_ModifierBackend):
    """Reads modifier keys straight from ``/dev/input``; needs the input group."""

    name = "evdev"

    flags = QtCore.pyqtSignal(bool, bool, bool)

    def __init__(self) -> None:
        super().__init__()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.flags.connect(self._flags_changed)

    @staticmethod
    def keyboards() -> tuple[list[str], int]:
        """Readable keyboards, and how many typing keyboards exist in total."""

        return input_devices.coverage(input_devices.keyboards())

    @staticmethod
    def readable_keyboards() -> list[str]:
        return EvdevHotkeyBackend.keyboards()[0]

    @staticmethod
    def available() -> bool:
        # Detection reads the kernel's table, but actually watching a device
        # needs the evdev package; without it start() would fail silently.
        return bool(EvdevHotkeyBackend.readable_keyboards()) and _has_evdev()

    @property
    def unavailable_reason(self) -> str | None:
        if not _has_evdev():
            return "The 'evdev' package is not installed; run: pip install evdev"
        return input_devices.partial_access_warning(
            "keyboard", input_devices.keyboards()
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bettervoice-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1)

    def _run(self) -> None:
        import selectors

        import evdev
        from evdev import ecodes

        watched = ALT_KEYS | SUPER_KEYS | OTHER_MODIFIER_KEYS
        held: set[int] = set()

        devices = []
        for path in self.readable_keyboards():
            try:
                devices.append(evdev.InputDevice(path))
            except OSError:
                continue
        if not devices:
            return
        selector = selectors.DefaultSelector()
        for device in devices:
            selector.register(device, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=0.2):
                    try:
                        events = list(key.fileobj.read())
                    except OSError:
                        continue
                    changed = False
                    for event in events:
                        if event.type != ecodes.EV_KEY or event.code not in watched:
                            continue
                        if event.value == 1:
                            held.add(event.code)
                            changed = True
                        elif event.value == 0:
                            held.discard(event.code)
                            changed = True
                    if changed:
                        self.flags.emit(*flags_from_held_keys(held))
        finally:
            selector.close()
            for device in devices:
                device.close()


class PortalHotkeyBackend(HotkeyBackend):
    """``org.freedesktop.portal.GlobalShortcuts`` -- the Wayland-native route.

    The compositor owns the key bindings, so the user can rebind them in their
    desktop's own shortcut editor. Some desktops (KDE among them) register the
    shortcuts but leave the keys unassigned until the user picks them, so this
    backend reports which triggers are actually live.
    """

    name = "portal"

    def __init__(self) -> None:
        super().__init__()
        self._client = global_shortcuts.GlobalShortcutsClient()
        self._client.activated.connect(self._on_activated)
        self._client.deactivated.connect(self._on_deactivated)
        self._client.bindings_changed.connect(self._on_bindings_changed)
        self._client.failed.connect(self._on_failed)
        self._push_active = False
        self._triggers: dict[str, str] = {}
        self._error: str | None = None

    @staticmethod
    def available() -> bool:
        return global_shortcuts.available()

    @property
    def unavailable_reason(self) -> str | None:
        if self._error is not None:
            return self._error
        if self._triggers and not any(self._triggers.values()):
            return "Registered with your desktop, but no keys are assigned yet."
        return None

    @property
    def shortcut_hint(self) -> str:
        friendly = global_shortcuts.friendly_trigger
        push = friendly(self._triggers.get(global_shortcuts.PUSH_TO_TALK_ID, ""))
        long_form = friendly(self._triggers.get(global_shortcuts.LONG_FORM_ID, ""))
        if self._triggers and not (push or long_form):
            return "no shortcut assigned yet — use the tray menu"
        if self._triggers:
            return f"hold {push or 'unassigned'}, or press {long_form or 'unassigned'}"
        return (
            f"hold {friendly(global_shortcuts.DEFAULT_PUSH_TO_TALK_TRIGGER)}, "
            f"or press {friendly(global_shortcuts.DEFAULT_LONG_FORM_TRIGGER)}"
        )

    @property
    def needs_key_assignment(self) -> bool:
        return bool(self._triggers) and not any(self._triggers.values())

    def start(self) -> None:
        self._client.start()

    def stop(self) -> None:
        self._client.stop()

    def _on_bindings_changed(self, triggers: dict) -> None:
        self._triggers = dict(triggers)

    def _on_failed(self, message: str) -> None:
        self._error = message
        log.warning("Global shortcuts unavailable: %s", message)

    def _on_activated(self, identifier: str) -> None:
        if identifier == global_shortcuts.PUSH_TO_TALK_ID:
            if self._push_active:
                return
            self._push_active = True
            self.push_to_talk_started.emit()
        elif identifier == global_shortcuts.LONG_FORM_ID:
            if self._push_active:
                self.promote_to_long_form.emit()
            else:
                self.long_form_toggled.emit()

    def _on_deactivated(self, identifier: str) -> None:
        if identifier == global_shortcuts.PUSH_TO_TALK_ID and self._push_active:
            self._push_active = False
            self.push_to_talk_stopped.emit()


def create(preference: str = "auto") -> HotkeyBackend:
    # Order matters. X11 sees every key with no privileges. The portal is the
    # compositor-sanctioned route on Wayland and always sees the whole keyboard.
    # evdev is last: it gives the closest feel to macOS but only covers the
    # devices this user happens to be able to read, so it is opt-in by name.
    candidates = [
        ("x11", X11HotkeyBackend.available, X11HotkeyBackend),
        ("portal", PortalHotkeyBackend.available, PortalHotkeyBackend),
        ("evdev", EvdevHotkeyBackend.available, EvdevHotkeyBackend),
    ]

    if preference not in {"auto", "none"}:
        for name, available, factory in candidates:
            if name != preference:
                continue
            if available():
                return factory()
            return NullHotkeyBackend(f"The {name} shortcut backend is not usable here.")
        return NullHotkeyBackend(f"Unknown shortcut backend {preference!r}.")

    if preference == "none":
        return NullHotkeyBackend("Global shortcuts are switched off in settings.")

    for _, available, factory in candidates:
        if available():
            return factory()
    return NullHotkeyBackend(
        "No global shortcut source is available. Start recordings from the tray menu, "
        "or add your user to the 'input' group so BetterVoice can watch the modifier keys."
    )
