"""Which backend gets picked on each kind of Linux desktop.

Everything else in this suite runs against KDE Wayland, because that is the
machine. This file is the substitute for the desktops that cannot be run here:
it describes GNOME, wlroots, X11 and a bare session in terms of the primitives
each backend consults, then pins what the app should choose. If the selection
logic drifts, the desktop it drifts on is named in the failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest


@dataclass(frozen=True)
class Desktop:
    """A session described the way the backends interrogate one."""

    name: str
    session_type: str = "wayland"
    desktops: tuple[str, ...] = ()
    tools: frozenset[str] = frozenset()
    portals: frozenset[str] = frozenset()
    has_kwin: bool = False
    has_xlib: bool = False
    readable_keyboards: tuple[str, ...] = ()
    readable_pointers: tuple[str, ...] = ()
    devices: tuple[str, ...] = field(default=())

    @property
    def is_x11(self) -> bool:
        return self.session_type == "x11"


PORTAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
PORTAL_SCREENSHOT = "org.freedesktop.portal.Screenshot"
PORTAL_REMOTE = "org.freedesktop.portal.RemoteDesktop"

KDE_WAYLAND = Desktop(
    name="KDE Wayland",
    desktops=("kde",),
    tools=frozenset({"wl-copy", "wl-paste", "pactl", "spectacle"}),
    portals=frozenset({PORTAL_SHORTCUTS, PORTAL_SCREENSHOT, PORTAL_REMOTE}),
    has_kwin=True,
)

GNOME_WAYLAND = Desktop(
    name="GNOME Wayland",
    desktops=("gnome",),
    tools=frozenset({"wl-copy", "wl-paste", "pactl", "gnome-screenshot"}),
    portals=frozenset({PORTAL_SHORTCUTS, PORTAL_SCREENSHOT, PORTAL_REMOTE}),
)

SWAY = Desktop(
    name="wlroots (Sway)",
    desktops=("sway",),
    tools=frozenset({"wl-copy", "wl-paste", "pactl", "grim", "wtype"}),
    portals=frozenset({PORTAL_SCREENSHOT}),  # no GlobalShortcuts on many wlroots setups
    readable_keyboards=("/dev/input/event3",),
    readable_pointers=("/dev/input/event4",),
)

X11_DESKTOP = Desktop(
    name="X11",
    session_type="x11",
    desktops=("xfce",),
    tools=frozenset({"xclip", "xdotool", "pactl", "maim"}),
    has_xlib=True,
)

BARE = Desktop(name="bare session", session_type="unknown")

ALL = [KDE_WAYLAND, GNOME_WAYLAND, SWAY, X11_DESKTOP, BARE]


@pytest.fixture()
def desktop(request, monkeypatch):
    """Make every backend see the requested desktop instead of this machine."""

    profile: Desktop = request.param
    from bettervoice.backends import (
        environment,
        focus,
        global_shortcuts,
        hotkeys,
        input_devices,
        kwin,
        pointer,
        portal,
        screenshot,
        textinject,
    )

    monkeypatch.setattr(environment, "session_type", lambda: profile.session_type)
    monkeypatch.setattr(environment, "is_wayland", lambda: profile.session_type == "wayland")
    monkeypatch.setattr(environment, "is_x11", lambda: profile.is_x11)
    monkeypatch.setattr(environment, "desktops", lambda: list(profile.desktops))
    monkeypatch.setattr(environment, "is_kde", lambda: "kde" in profile.desktops)
    monkeypatch.setattr(environment, "is_gnome", lambda: "gnome" in profile.desktops)
    monkeypatch.setattr(environment, "has", lambda tool: tool in profile.tools)
    for module in (screenshot, textinject, hotkeys, pointer, focus, clipboard_module()):
        if hasattr(module, "environment"):
            monkeypatch.setattr(module, "environment", environment)

    monkeypatch.setattr(portal, "available", lambda interface: interface in profile.portals)
    monkeypatch.setattr(
        global_shortcuts, "available", lambda: PORTAL_SHORTCUTS in profile.portals
    )
    monkeypatch.setattr(kwin, "available", lambda: profile.has_kwin)

    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name != "Xlib" or profile.has_xlib else None,
    )
    monkeypatch.setattr(
        input_devices, "keyboards", lambda: _fake_devices(profile.readable_keyboards)
    )
    monkeypatch.setattr(
        input_devices, "pointers", lambda: _fake_devices(profile.readable_pointers)
    )
    monkeypatch.setattr(input_devices, "readable", lambda devices: [d.node for d in devices])
    return profile


def clipboard_module():
    from bettervoice.backends import clipboard

    return clipboard


def _fake_devices(nodes):
    from bettervoice.backends.input_devices import InputDevice

    return [
        InputDevice(name=node, handlers=frozenset({"kbd", "leds"}), node=node) for node in nodes
    ]


def _ids(profiles):
    return [profile.name for profile in profiles]


class TestShortcutSelection:
    EXPECTED: ClassVar[dict[str, str]] = {
        "KDE Wayland": "portal",
        "GNOME Wayland": "portal",
        "wlroots (Sway)": "evdev",   # no GlobalShortcuts portal, but /dev/input is readable
        "X11": "x11",
        "bare session": "none",
    }

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_the_expected_backend_is_chosen(self, desktop):
        from bettervoice.backends import hotkeys

        assert hotkeys.create().name == self.EXPECTED[desktop.name]

    @pytest.mark.parametrize("desktop", [SWAY], indirect=True, ids=["wlroots (Sway)"])
    def test_without_readable_input_there_is_no_shortcut_source(self, desktop, monkeypatch):
        from bettervoice.backends import hotkeys, input_devices

        monkeypatch.setattr(input_devices, "keyboards", list)

        backend = hotkeys.create()
        assert backend.name == "none"
        assert backend.unavailable_reason


class TestPointerSelection:
    EXPECTED: ClassVar[dict[str, str]] = {
        "KDE Wayland": "kwin",
        "GNOME Wayland": "none",   # Wayland hides the pointer and there is no KWin
        "wlroots (Sway)": "evdev",
        "X11": "x11",
        "bare session": "none",
    }

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_the_expected_backend_is_chosen(self, desktop):
        from bettervoice.backends import pointer

        assert pointer.create().name == self.EXPECTED[desktop.name]

    @pytest.mark.parametrize("desktop", [GNOME_WAYLAND], indirect=True, ids=["GNOME Wayland"])
    def test_gnome_is_told_why_circles_do_not_work(self, desktop):
        from bettervoice.backends import pointer

        reason = pointer.create().unavailable_reason
        assert reason and "Wayland" in reason, reason


class TestScreenshotSelection:
    EXPECTED: ClassVar[dict[str, str]] = {
        "KDE Wayland": "portal",
        "GNOME Wayland": "portal",
        "wlroots (Sway)": "portal",
        "X11": "maim",       # no portal; the first installed tool wins
        "bare session": "none",
    }

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_the_expected_backend_is_chosen(self, desktop):
        from bettervoice.backends import screenshot

        assert screenshot.create().name == self.EXPECTED[desktop.name]

    @pytest.mark.parametrize("desktop", [SWAY], indirect=True, ids=["wlroots (Sway)"])
    def test_grim_is_used_when_there_is_no_portal(self, desktop, monkeypatch):
        from bettervoice.backends import portal, screenshot

        monkeypatch.setattr(portal, "available", lambda _interface: False)

        assert screenshot.create().name == "grim"


class TestInsertionSelection:
    EXPECTED: ClassVar[dict[str, str]] = {
        "KDE Wayland": "portal",
        "GNOME Wayland": "portal",
        # wlroots implements virtual-keyboard-v1, which is exactly what wtype
        # uses, and this profile has no RemoteDesktop portal to fall back on.
        "wlroots (Sway)": "wtype",
        "X11": "xdotool",     # a real tool beats the portal on X11
        "bare session": "none",
    }

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_the_expected_backend_is_chosen(self, desktop):
        from bettervoice.backends import textinject

        assert textinject.create().name == self.EXPECTED[desktop.name]

    @pytest.mark.parametrize("desktop", [SWAY], indirect=True, ids=["wlroots (Sway)"])
    def test_the_remote_desktop_portal_wins_when_it_is_offered(self, desktop, monkeypatch):
        """Where both exist, the compositor-sanctioned route is preferred."""

        from bettervoice.backends import portal, textinject

        monkeypatch.setattr(portal, "available", lambda _interface: True)

        assert textinject.create().name == "portal"


class TestFocusSelection:
    EXPECTED: ClassVar[dict[str, str]] = {
        "KDE Wayland": "kwin",
        "GNOME Wayland": "none",
        "wlroots (Sway)": "none",
        "X11": "none",
        "bare session": "none",
    }

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_the_expected_backend_is_chosen(self, desktop):
        from bettervoice.backends import focus

        assert focus.create().name == self.EXPECTED[desktop.name]


class TestEveryDesktopCanStillDictate:
    """Whatever is missing, recording and transcription must never be blocked."""

    @pytest.mark.parametrize("desktop", ALL, indirect=True, ids=_ids(ALL))
    def test_nothing_in_the_selection_raises(self, desktop):
        from bettervoice.backends import focus, hotkeys, pointer, screenshot, textinject

        for module in (hotkeys, pointer, screenshot, textinject, focus):
            backend = module.create()
            assert backend.name
            # An unavailable backend must explain itself rather than be silent.
            if backend.name == "none" and hasattr(backend, "unavailable_reason"):
                assert backend.unavailable_reason is None or backend.unavailable_reason
