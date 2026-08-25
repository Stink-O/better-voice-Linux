"""The two audible cues that bracket a recording."""

from __future__ import annotations

import enum


class RecordingSoundCue(enum.Enum):
    STARTED = "started"
    FINISHED = "finished"

    @property
    def freedesktop_sound_name(self) -> str:
        """Nearest sound in the freedesktop sound-naming spec.

        macOS used "Purr" to open and "Pop" to close; these are the closest
        equivalents that ship with every standard Linux sound theme.
        """
        return "message" if self is RecordingSoundCue.STARTED else "message-new-instant"
