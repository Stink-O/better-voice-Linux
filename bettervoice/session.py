"""Saved sessions on disk: the transcript, its screenshots, and their lifetime.

Mirrors ``SessionStorage``/``SessionOutput`` from the macOS build, including the
seven-day / 500 MB retention policy and the clipboard-and-paste delivery rules
that make a quick note leave the clipboard untouched.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    SessionRetentionPolicy,
    StoredSession,
    is_bettervoice_session_name,
)
from .errors import SessionStorageFull
from .paths import ensure, sessions_dir

log = logging.getLogger(__name__)

MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_BYTES = 500 * 1_024 * 1_024

#: Headroom kept aside for the transcript before screenshots claim the budget.
TRANSCRIPT_RESERVE = 1_024 * 1_024


def root() -> Path:
    return sessions_dir()


def directory_size(folder: Path) -> int:
    total = 0
    if not folder.is_dir():
        return 0
    for path in folder.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def prune(reserving_bytes: int = 0) -> None:
    base = root()
    if not base.is_dir():
        return
    sessions = []
    for folder in base.iterdir():
        if not folder.is_dir() or not is_bettervoice_session_name(folder.name):
            continue
        try:
            modified = folder.stat().st_mtime
        except OSError:
            continue
        sessions.append(StoredSession(folder.name, modified, directory_size(folder)))

    policy = SessionRetentionPolicy(
        max_age=MAX_AGE_SECONDS, max_bytes=max(0, MAX_BYTES - reserving_bytes)
    )
    for name in policy.sessions_to_remove(sessions, time.time()):
        shutil.rmtree(base / name, ignore_errors=True)


def clear() -> None:
    base = root()
    if base.is_dir():
        shutil.rmtree(base)


@dataclass
class DeliveryResult:
    markdown: Path
    clipboard_copied: bool
    transcript_inserted: bool


class SessionOutput:
    """One recording's folder: screenshots plus the transcript that names them."""

    def __init__(self) -> None:
        prune(reserving_bytes=TRANSCRIPT_RESERVE)
        self._used_bytes = directory_size(root()) + TRANSCRIPT_RESERVE
        stamp = (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            .replace(":", "-")
        )
        self.folder = ensure(root() / f"{stamp}-{uuid.uuid4()}")
        self.images: list[Path] = []

    def reserve_image(self) -> Path:
        return self.folder / f"context-{len(self.images) + 1}.png"

    def accept_image(self, path: Path) -> None:
        """Record a freshly captured screenshot, or refuse it if the disk budget is spent."""

        try:
            size = path.stat().st_size
        except OSError as error:
            raise SessionStorageFull() from error
        policy = SessionRetentionPolicy(max_age=MAX_AGE_SECONDS, max_bytes=MAX_BYTES)
        if not policy.can_store(additional_bytes=size, used_bytes=self._used_bytes):
            path.unlink(missing_ok=True)
            raise SessionStorageFull()
        self._used_bytes += size
        self.images.append(path)

    def discard(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    def write_markdown(self, transcript: str) -> Path:
        trimmed = transcript.strip()
        lines = ["# BetterVoice session", ""]
        lines.append(trimmed if trimmed else "_No transcript captured._")
        lines.append("")
        if self.images:
            lines += ["## Screen context", ""]
            for index, image in enumerate(self.images, start=1):
                lines.append(f"![Context {index}]({image.name})")
                lines.append("")
        path = self.folder / "context.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
