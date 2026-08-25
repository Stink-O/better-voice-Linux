"""Platform-independent behaviour ported 1:1 from Sources/BetterVoiceCore."""

from .circle_gesture import CircleGesture, CircleGestureDetector
from .delivery_report import DeliveryReport, delivery_report
from .session_policy import (
    SessionCompletionDisposition,
    SessionRetentionPolicy,
    StoredSession,
    is_bettervoice_session_name,
    session_completion_disposition,
)
from .shortcut_state import RecordingShortcutAction, RecordingShortcutState
from .sound_cue import RecordingSoundCue
from .trail import TrailSegment, trail_segments

__all__ = [
    "CircleGesture",
    "CircleGestureDetector",
    "DeliveryReport",
    "RecordingShortcutAction",
    "RecordingShortcutState",
    "RecordingSoundCue",
    "SessionCompletionDisposition",
    "SessionRetentionPolicy",
    "StoredSession",
    "TrailSegment",
    "delivery_report",
    "is_bettervoice_session_name",
    "session_completion_disposition",
    "trail_segments",
]
