"""Modifier state machine for the two recording gestures.

Port of ``Sources/BetterVoiceCore/RecordingShortcutState.swift`` with the macOS
modifiers remapped to their Linux equivalents:

===================  ==================  ====================================
macOS                Linux               Meaning
===================  ==================  ====================================
Option (``alt``)     Alt                 hold for a quick note
Command + Option     Super + Alt         press to toggle a long explanation
Shift / Control      Shift / Control     "other modifier" -- suppresses both
===================  ==================  ====================================
"""

from __future__ import annotations

import enum


class RecordingShortcutAction(enum.Enum):
    SCHEDULE_PUSH_TO_TALK = "schedule_push_to_talk"
    CANCEL_PENDING_PUSH_TO_TALK = "cancel_pending_push_to_talk"
    START_PUSH_TO_TALK = "start_push_to_talk"
    STOP_PUSH_TO_TALK = "stop_push_to_talk"
    TOGGLE_LONG_FORM = "toggle_long_form"
    PROMOTE_TO_LONG_FORM = "promote_to_long_form"


class _Mode(enum.Enum):
    IDLE = "idle"
    PENDING_PUSH_TO_TALK = "pending_push_to_talk"
    PUSH_TO_TALK = "push_to_talk"
    SUPPRESS_UNTIL_ALT_RELEASE = "suppress_until_alt_release"


class RecordingShortcutState:
    """Turns raw modifier transitions into recording actions."""

    def __init__(self) -> None:
        self._mode = _Mode.IDLE

    def flags_changed(
        self,
        *,
        super_: bool,
        alt: bool,
        other_modifier: bool = False,
    ) -> list[RecordingShortcutAction]:
        if self._mode is _Mode.PUSH_TO_TALK and other_modifier:
            if alt:
                return []
            self._mode = _Mode.IDLE
            return [RecordingShortcutAction.STOP_PUSH_TO_TALK]

        if self._mode is _Mode.PENDING_PUSH_TO_TALK and other_modifier:
            self._mode = _Mode.IDLE
            return [RecordingShortcutAction.CANCEL_PENDING_PUSH_TO_TALK]

        if other_modifier:
            return []

        if super_ and alt:
            if self._mode is _Mode.IDLE:
                self._mode = _Mode.SUPPRESS_UNTIL_ALT_RELEASE
                return [RecordingShortcutAction.TOGGLE_LONG_FORM]
            if self._mode is _Mode.PENDING_PUSH_TO_TALK:
                self._mode = _Mode.SUPPRESS_UNTIL_ALT_RELEASE
                return [
                    RecordingShortcutAction.CANCEL_PENDING_PUSH_TO_TALK,
                    RecordingShortcutAction.TOGGLE_LONG_FORM,
                ]
            if self._mode is _Mode.PUSH_TO_TALK:
                self._mode = _Mode.SUPPRESS_UNTIL_ALT_RELEASE
                return [RecordingShortcutAction.PROMOTE_TO_LONG_FORM]
            return []

        if not alt and self._mode is _Mode.SUPPRESS_UNTIL_ALT_RELEASE:
            self._mode = _Mode.IDLE
            return []

        if super_:
            return []

        if alt and self._mode is _Mode.IDLE:
            self._mode = _Mode.PENDING_PUSH_TO_TALK
            return [RecordingShortcutAction.SCHEDULE_PUSH_TO_TALK]

        if not alt and self._mode is _Mode.PUSH_TO_TALK:
            self._mode = _Mode.IDLE
            return [RecordingShortcutAction.STOP_PUSH_TO_TALK]

        if not alt and self._mode is _Mode.PENDING_PUSH_TO_TALK:
            self._mode = _Mode.IDLE
            return [RecordingShortcutAction.CANCEL_PENDING_PUSH_TO_TALK]

        return []

    def push_to_talk_delay_elapsed(self) -> list[RecordingShortcutAction]:
        if self._mode is not _Mode.PENDING_PUSH_TO_TALK:
            return []
        self._mode = _Mode.PUSH_TO_TALK
        return [RecordingShortcutAction.START_PUSH_TO_TALK]
