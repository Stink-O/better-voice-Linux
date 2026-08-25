"""What kind of Linux desktop are we on?"""

from __future__ import annotations

import functools
import os
import shutil


def session_type() -> str:
    """``wayland``, ``x11`` or ``unknown``."""
    value = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if value in {"wayland", "x11"}:
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def is_wayland() -> bool:
    return session_type() == "wayland"


def is_x11() -> bool:
    return session_type() == "x11"


def desktops() -> list[str]:
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    return [part.strip().lower() for part in raw.split(":") if part.strip()]


def is_kde() -> bool:
    return any(desktop in {"kde", "plasma", "plasmawayland"} for desktop in desktops())


def is_gnome() -> bool:
    return any("gnome" in desktop for desktop in desktops())


@functools.cache
def has(tool: str) -> bool:
    return shutil.which(tool) is not None


def describe() -> str:
    return f"{'/'.join(desktops()) or 'unknown desktop'} on {session_type()}"


def open_shortcut_settings() -> bool:
    """Open the desktop's keyboard-shortcut editor, if we know where it is."""

    import subprocess

    candidates: list[list[str]] = []
    if is_kde():
        candidates.append(["systemsettings", "kcm_keys"])
        candidates.append(["kcmshell6", "kcm_keys"])
        candidates.append(["kcmshell5", "kcm_keys"])
    if is_gnome():
        candidates.append(["gnome-control-center", "keyboard"])
    candidates.append(["xdg-open", "settings://keyboard"])

    for argv in candidates:
        if not has(argv[0]):
            continue
        try:
            subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
            return True
        except OSError:
            continue
    return False
