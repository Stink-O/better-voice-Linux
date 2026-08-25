"""Ported from Tests/BetterVoiceCoreTests -- same cases, same expectations."""

from __future__ import annotations

import math

import pytest

from bettervoice.core import (
    CircleGestureDetector,
    RecordingShortcutAction as Action,
    RecordingShortcutState,
    RecordingSoundCue,
    SessionCompletionDisposition as Disposition,
    SessionRetentionPolicy,
    StoredSession,
    TrailSegment,
    is_bettervoice_session_name,
    session_completion_disposition,
    trail_segments,
)


class TestRecordingShortcutState:
    def test_alt_hold_starts_after_delay_and_stops_on_release(self):
        shortcut = RecordingShortcutState()

        assert shortcut.flags_changed(super_=False, alt=True) == [Action.SCHEDULE_PUSH_TO_TALK]
        assert shortcut.push_to_talk_delay_elapsed() == [Action.START_PUSH_TO_TALK]
        assert shortcut.flags_changed(super_=False, alt=False) == [Action.STOP_PUSH_TO_TALK]

    def test_other_modifier_does_not_stop_active_push_to_talk(self):
        shortcut = RecordingShortcutState()

        shortcut.flags_changed(super_=False, alt=True)
        shortcut.push_to_talk_delay_elapsed()
        assert shortcut.flags_changed(super_=False, alt=True, other_modifier=True) == []
        assert shortcut.flags_changed(super_=False, alt=False) == [Action.STOP_PUSH_TO_TALK]

    def test_alt_tap_cancels_before_recording_starts(self):
        shortcut = RecordingShortcutState()

        assert shortcut.flags_changed(super_=False, alt=True) == [Action.SCHEDULE_PUSH_TO_TALK]
        assert shortcut.flags_changed(super_=False, alt=False) == [
            Action.CANCEL_PENDING_PUSH_TO_TALK
        ]
        assert shortcut.push_to_talk_delay_elapsed() == []

    def test_super_alt_before_delay_starts_long_form_only(self):
        shortcut = RecordingShortcutState()

        assert shortcut.flags_changed(super_=False, alt=True) == [Action.SCHEDULE_PUSH_TO_TALK]
        assert shortcut.flags_changed(super_=True, alt=True) == [
            Action.CANCEL_PENDING_PUSH_TO_TALK,
            Action.TOGGLE_LONG_FORM,
        ]
        assert shortcut.push_to_talk_delay_elapsed() == []
        assert shortcut.flags_changed(super_=False, alt=False) == []

    def test_adding_super_promotes_push_to_talk_without_stopping(self):
        shortcut = RecordingShortcutState()

        shortcut.flags_changed(super_=False, alt=True)
        shortcut.push_to_talk_delay_elapsed()
        assert shortcut.flags_changed(super_=True, alt=True) == [Action.PROMOTE_TO_LONG_FORM]
        assert shortcut.flags_changed(super_=False, alt=False) == []

    def test_super_alt_toggles_once_per_chord(self):
        shortcut = RecordingShortcutState()

        assert shortcut.flags_changed(super_=True, alt=True) == [Action.TOGGLE_LONG_FORM]
        assert shortcut.flags_changed(super_=True, alt=True) == []
        assert shortcut.flags_changed(super_=False, alt=False) == []
        assert shortcut.flags_changed(super_=True, alt=True) == [Action.TOGGLE_LONG_FORM]


class TestTrailSegments:
    def test_skips_pauses_and_pointer_jumps(self):
        assert trail_segments([], []) == []
        assert trail_segments([(0, 0)], [0]) == []
        assert trail_segments([(0, 0), (30, 0)], [0, 0.15]) == [TrailSegment(0, 1)]
        assert trail_segments([(0, 0), (4, 3)], [0, 0.25]) == []
        assert trail_segments([(0, 0), (240, 0)], [0, 0.016]) == []


class TestCircleGestureDetector:
    def test_recognizes_closed_circle(self):
        detector = CircleGestureDetector()
        result = None
        center = (300.0, 200.0)

        for index in range(48):
            angle = index / 47 * 2 * math.pi
            found = detector.add(
                (center[0] + 52 * math.cos(angle), center[1] + 52 * math.sin(angle)),
                at=index / 60,
            )
            result = found or result

        assert result is not None
        assert result.center[0] == pytest.approx(center[0], abs=3)
        assert result.center[1] == pytest.approx(center[1], abs=3)
        assert result.radius == pytest.approx(52, abs=3)

    def test_recognizes_slow_loose_loop(self):
        detector = CircleGestureDetector()
        result = None
        center = (900.0, 500.0)

        for index in range(150):
            angle = index / 149 * 2 * math.pi
            wobble = 1 + 0.1 * math.sin(angle * 3)
            found = detector.add(
                (
                    center[0] + 110 * wobble * math.cos(angle),
                    center[1] + 82 * wobble * math.sin(angle),
                ),
                at=index * 0.02,
            )
            result = found or result

        assert result is not None

    def test_recognizes_slow_loose_loop_after_pointer_movement(self):
        detector = CircleGestureDetector()
        result = None

        for index in range(60):
            detector.add((300 + index * 5, 240 + index % 7), at=index * 0.02)

        center = (900.0, 500.0)
        for index in range(150):
            angle = index / 149 * 2 * math.pi
            wobble = 1 + 0.1 * math.sin(angle * 3)
            found = detector.add(
                (
                    center[0] + 110 * wobble * math.cos(angle),
                    center[1] + 82 * wobble * math.sin(angle),
                ),
                at=1.2 + index * 0.02,
            )
            result = found or result

        assert result is not None

    def test_rejects_partial_arc(self):
        """Reaching across the screen sweeps an arc; that is not a circle.

        The detector once accepted 4.5 radians -- about 258 degrees -- so an
        unclosed sweep captured the screen mid-sentence. It now wants very
        nearly a full turn.
        """

        detector = CircleGestureDetector()
        center = (400.0, 300.0)
        result = None

        # 335 degrees at this radius closes to within 43px, well inside the
        # closure allowance -- so this is rejected on the angle travelled and
        # nothing else, which is the threshold being pinned here.
        for index in range(48):
            angle = index / 47 * math.radians(335)
            found = detector.add(
                (center[0] + 100 * math.cos(angle), center[1] + 100 * math.sin(angle)),
                at=index / 60,
            )
            result = found or result

        assert result is None

    def test_rejects_an_irregular_closed_loop(self):
        """Closing back on yourself is not enough; the shape has to be round."""

        detector = CircleGestureDetector()
        center = (400.0, 300.0)
        result = None

        for index in range(60):
            angle = index / 59 * 2 * math.pi
            # A radius that swings far in and out: closed, but nothing like round.
            wobble = 1 + 0.55 * math.sin(angle * 2)
            found = detector.add(
                (
                    center[0] + 70 * wobble * math.cos(angle),
                    center[1] + 70 * wobble * math.sin(angle),
                ),
                at=index / 60,
            )
            result = found or result

        assert result is None

    def test_rejects_a_hook(self):
        """A flick out and back curves, returns near its start, and is not a circle."""

        detector = CircleGestureDetector()
        result = None

        for index in range(40):
            progress = index / 39
            angle = progress * math.pi  # half a turn out...
            x = 400 + 60 * math.cos(angle)
            y = 300 + 60 * math.sin(angle) * (1 - progress * 0.6)
            found = detector.add((x, y), at=index / 60)
            result = found or result

        assert result is None

    def test_rejects_a_zigzag(self):
        detector = CircleGestureDetector()
        result = None

        for index in range(40):
            x = 300.0 + index * 12
            y = 400.0 + (40 if index % 2 else -40)
            found = detector.add((x, y), at=index / 60)
            result = found or result

        assert result is None

    def test_still_recognizes_a_hand_drawn_circle(self):
        """The tightening must not cost an ordinary, slightly wobbly circle."""

        detector = CircleGestureDetector()
        center = (500.0, 400.0)
        result = None

        for index in range(56):
            angle = index / 55 * 2 * math.pi
            wobble = 1 + 0.08 * math.sin(angle * 3)
            found = detector.add(
                (
                    center[0] + 90 * wobble * math.cos(angle),
                    center[1] + 76 * wobble * math.sin(angle),
                ),
                at=index / 60,
            )
            result = found or result

        assert result is not None

    def test_rejects_straight_line(self):
        detector = CircleGestureDetector()
        result = None

        for index in range(48):
            result = detector.add((index * 4, 200), at=index / 60) or result

        assert result is None

    def test_long_continuous_loop_captures_once_until_pointer_leaves(self):
        detector = CircleGestureDetector()
        center = (400.0, 300.0)
        captures = 0

        for index in range(240):
            angle = index / 47 * 2 * math.pi
            if detector.add(
                (center[0] + 70 * math.cos(angle), center[1] + 70 * math.sin(angle)),
                at=index / 60,
            ):
                captures += 1

        assert captures == 1


class TestRecordingSoundCue:
    def test_cues_are_distinct(self):
        assert (
            RecordingSoundCue.STARTED.freedesktop_sound_name
            != RecordingSoundCue.FINISHED.freedesktop_sound_name
        )


class TestSessionCompletionPolicy:
    def test_short_empty_session_is_discarded_as_an_accidental_shortcut(self):
        assert (
            session_completion_disposition(
                has_transcript=False, has_context=False, duration=1.2
            )
            is Disposition.DISCARD_ACCIDENTAL
        )

    def test_long_empty_session_is_kept_without_being_an_error(self):
        assert (
            session_completion_disposition(has_transcript=False, has_context=False, duration=4)
            is Disposition.SAVE_EMPTY
        )

    def test_transcript_or_context_is_delivered(self):
        assert (
            session_completion_disposition(
                has_transcript=True, has_context=False, duration=0.2
            )
            is Disposition.DELIVER
        )
        assert (
            session_completion_disposition(
                has_transcript=False, has_context=True, duration=0.2
            )
            is Disposition.DELIVER
        )


class TestSessionRetentionPolicy:
    def test_removes_expired_then_oldest_sessions_until_under_size_limit(self):
        now = 1_000_000.0
        day = 86_400.0
        policy = SessionRetentionPolicy(max_age=7 * day, max_bytes=500)
        sessions = [
            StoredSession("expired", now - 8 * day, 100),
            StoredSession("oldest", now - 3 * day, 300),
            StoredSession("newest", now - day, 300),
        ]

        assert policy.sessions_to_remove(sessions, now) == {"expired", "oldest"}

    def test_rejects_a_file_that_would_exceed_the_storage_limit(self):
        policy = SessionRetentionPolicy(max_age=1, max_bytes=500)

        assert policy.can_store(additional_bytes=100, used_bytes=400)
        assert not policy.can_store(additional_bytes=101, used_bytes=400)

    def test_only_recognizes_generated_session_folder_names(self):
        assert is_bettervoice_session_name(
            "2026-08-23T15-16-45Z-C81A6E98-FD94-4FC6-AF2C-8928EBD938B1"
        )
        assert not is_bettervoice_session_name("my-important-folder")
        assert not is_bettervoice_session_name(
            "backup-C81A6E98-FD94-4FC6-AF2C-8928EBD938B1"
        )


class TestDeliveryReport:
    """What the user is told after a recording, for every way one can end."""

    def _report(self, **overrides):
        from bettervoice.core import delivery_report

        arguments = {
            "transcription_error": None,
            "copy_to_clipboard": True,
            "clipboard_copied": False,
            "inserted": False,
            "had_transcript": True,
            "has_context": False,
            "context_on_clipboard": False,
        }
        arguments.update(overrides)
        return delivery_report(**arguments)

    def test_a_transcript_that_reached_the_field_is_just_a_status(self):
        report = self._report(inserted=True)

        assert report.status == "Inserted transcript"
        assert not report.is_error
        assert report.reset_after == 4.0

    def test_a_long_explanation_mentions_the_context_only_when_it_travelled(self):
        rode_along = self._report(inserted=True, has_context=True, context_on_clipboard=True)
        stayed_behind = self._report(inserted=True, has_context=True, context_on_clipboard=False)

        assert rode_along.status == "Inserted transcript • context copied"
        assert stayed_behind.status == "Inserted transcript", (
            "claiming the context was copied when it was not would be a lie"
        )

    def test_a_quick_note_never_claims_the_context_was_copied(self):
        report = self._report(
            inserted=True, copy_to_clipboard=False, has_context=True, context_on_clipboard=True
        )

        assert report.status == "Inserted transcript"

    @pytest.mark.parametrize(
        ("had_transcript", "has_context", "context_on_clipboard", "expected"),
        [
            (True, True, True, "Copied transcript + context"),
            (True, True, False, "Copied transcript"),
            (True, False, False, "Copied transcript"),
            (False, True, False, "No speech detected • context copied"),
            (False, False, False, "No speech detected • session saved"),
        ],
    )
    def test_a_clipboard_delivery_says_what_it_carried(
        self, had_transcript, has_context, context_on_clipboard, expected
    ):
        report = self._report(
            clipboard_copied=True,
            had_transcript=had_transcript,
            has_context=has_context,
            context_on_clipboard=context_on_clipboard,
        )

        assert report.status == expected
        assert not report.is_error

    @pytest.mark.parametrize(
        ("has_context", "expected"),
        [(True, "Screen context saved"), (False, "Session saved")],
    )
    def test_a_silent_quick_note_is_saved_without_complaint(self, has_context, expected):
        report = self._report(
            copy_to_clipboard=False, had_transcript=False, has_context=has_context
        )

        assert report.status == expected
        assert not report.is_error, "an empty quick note is not a failure"

    def test_a_failed_insertion_is_reported_differently_per_mode(self):
        quick_note = self._report(copy_to_clipboard=False)
        long_form = self._report(copy_to_clipboard=True)

        assert quick_note.error == "Saved session; transcript was not inserted."
        assert long_form.error == "Saved session; plain-text clipboard fallback used."

    @pytest.mark.parametrize(
        ("copy_to_clipboard", "clipboard_copied", "has_context", "tail"),
        [
            (False, False, False, "Session saved."),
            (True, True, True, "Screen context copied."),
            (True, True, False, "Session saved."),
            (True, False, False, "Session saved; clipboard text fallback used."),
        ],
    )
    def test_a_transcription_failure_still_says_where_the_session_went(
        self, copy_to_clipboard, clipboard_copied, has_context, tail
    ):
        report = self._report(
            transcription_error="the model exploded",
            copy_to_clipboard=copy_to_clipboard,
            clipboard_copied=clipboard_copied,
            has_context=has_context,
        )

        assert report.is_error
        assert report.error.startswith("Transcription failed: the model exploded")
        assert report.error.endswith(tail)
        assert report.status is None
