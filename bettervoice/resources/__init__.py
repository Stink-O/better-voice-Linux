"""Desktop-integration files shipped inside the package.

These live here rather than beside the source tree so that a plain
``pip install`` carries them: the XDG portals identify BetterVoice by its desktop
entry, and without one it cannot be granted global shortcuts or screen capture.
``bettervoice --install-desktop-entry`` writes them out.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from .. import APP_ID

DESKTOP_ENTRY = f"{APP_ID}.desktop"
ICON = f"{APP_ID}.svg"
METAINFO = f"{APP_ID}.metainfo.xml"
SERVICE = f"app-{APP_ID}.service"


def read(name: str) -> bytes:
    return (resources.files(__package__) / name).read_bytes()


def install(prefix: Path, venv_python: str | None = None) -> list[Path]:
    """Write the desktop entry, icon, metainfo and user service under ``prefix``."""

    targets = {
        DESKTOP_ENTRY: prefix / "share" / "applications",
        ICON: prefix / "share" / "icons" / "hicolor" / "scalable" / "apps",
        METAINFO: prefix / "share" / "metainfo",
    }
    written = []
    for name, directory in targets.items():
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        destination.write_bytes(read(name))
        written.append(destination)
    return written
