"""When a session is kept, and how long saved sessions live on disk.

Port of ``SessionCompletionPolicy.swift`` and ``SessionRetentionPolicy.swift``.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

_SESSION_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-"
    r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$"
)


class SessionCompletionDisposition(enum.Enum):
    DISCARD_ACCIDENTAL = "discard_accidental"
    SAVE_EMPTY = "save_empty"
    DELIVER = "deliver"


def session_completion_disposition(
    *,
    has_transcript: bool,
    has_context: bool,
    duration: float,
    accidental_threshold: float = 2.5,
) -> SessionCompletionDisposition:
    if has_transcript or has_context:
        return SessionCompletionDisposition.DELIVER
    if duration < accidental_threshold:
        return SessionCompletionDisposition.DISCARD_ACCIDENTAL
    return SessionCompletionDisposition.SAVE_EMPTY


def is_bettervoice_session_name(name: str) -> bool:
    return _SESSION_NAME.match(name) is not None


@dataclass(frozen=True)
class StoredSession:
    name: str
    modified_at: float
    """Seconds since the epoch."""
    bytes: int


@dataclass(frozen=True)
class SessionRetentionPolicy:
    max_age: float
    max_bytes: int

    def can_store(self, *, additional_bytes: int, used_bytes: int) -> bool:
        return (
            additional_bytes >= 0
            and used_bytes >= 0
            and additional_bytes <= self.max_bytes - used_bytes
        )

    def sessions_to_remove(self, sessions: list[StoredSession], now: float) -> set[str]:
        removed = {
            session.name for session in sessions if now - session.modified_at > self.max_age
        }
        kept = [session for session in sessions if session.name not in removed]
        total = sum(session.bytes for session in kept)

        for session in sorted(kept, key=lambda item: item.modified_at):
            if total <= self.max_bytes:
                continue
            removed.add(session.name)
            total -= session.bytes
        return removed
