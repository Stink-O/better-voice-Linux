"""XDG locations. Replaces macOS Application Support / Desktop lookups."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .errors import SessionUnavailable

APP_DIRNAME = "BetterVoice"


def _xdg(variable: str, default: Path) -> Path:
    raw = os.environ.get(variable)
    return Path(raw).expanduser() if raw else default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_DIRNAME


def data_dir() -> Path:
    """Where downloaded models live (macOS: ~/Library/Application Support)."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_DIRNAME


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / APP_DIRNAME


def runtime_dir() -> Path:
    """Where in-progress recordings live.

    ``XDG_RUNTIME_DIR`` is the right home for them: it is per-user and 0700. It
    is absent often enough to need an answer -- distributions without systemd or
    elogind, ``su``, minimal containers, plain SSH -- and that answer must not be
    a directory shared with every other account on the machine. Raw microphone
    audio in ``/tmp`` is readable by anyone, and a symlink planted there ahead of
    time would redirect every recording. The private cache directory is worth
    more here than the tmpfs.
    """

    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw:
        return Path(raw) / APP_DIRNAME
    return cache_dir() / "recordings"


def desktop_dir() -> Path:
    """The user's Desktop, honouring localised names via xdg-user-dir."""
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        candidate = Path(result.stdout.strip())
        if result.returncode == 0 and candidate.is_dir():
            return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else Path.home()


def sessions_dir() -> Path:
    """Saved sessions, matching the macOS build's Desktop/BetterVoice folder."""
    override = os.environ.get("BETTERVOICE_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    return desktop_dir() / APP_DIRNAME


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_private(path: Path) -> Path:
    """Create a directory only this user can enter, and refuse an impostor.

    ``mkdir(exist_ok=True)`` is happy to hand back a symlink someone else
    planted, because it falls through to ``is_dir()``, which follows links. For
    a directory that is about to hold recordings, that is the whole attack.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SessionUnavailable()
    if info.st_uid != os.getuid():
        raise SessionUnavailable()
    if info.st_mode & 0o077:
        path.chmod(0o700)
    return path
