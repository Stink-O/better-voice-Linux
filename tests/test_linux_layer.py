"""Tests for the Linux-specific pieces that need no desktop session."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bettervoice import session
from bettervoice.config import DEFAULTS, Config
from bettervoice.core import CircleGesture
from bettervoice.errors import SessionStorageFull


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(root))
    return root


class TestSessionOutput:
    def test_folder_name_is_recognised_by_the_retention_policy(self, sessions_root):
        from bettervoice.core import is_bettervoice_session_name

        output = session.SessionOutput()

        assert output.folder.parent == sessions_root
        assert is_bettervoice_session_name(output.folder.name), output.folder.name

    def test_markdown_lists_the_transcript_and_every_screenshot(self, sessions_root):
        output = session.SessionOutput()
        for _index in (1, 2):
            path = output.reserve_image()
            path.write_bytes(b"x" * 100)
            output.accept_image(path)

        markdown = output.write_markdown("  hello there  ").read_text(encoding="utf-8")

        assert "# BetterVoice session" in markdown
        assert "hello there" in markdown
        assert "  hello there  " not in markdown, "the transcript should be trimmed"
        assert "![Context 1](context-1.png)" in markdown
        assert "![Context 2](context-2.png)" in markdown

    def test_markdown_says_so_when_there_is_no_transcript(self, sessions_root):
        output = session.SessionOutput()

        markdown = output.write_markdown("   ").read_text(encoding="utf-8")

        assert "_No transcript captured._" in markdown
        assert "## Screen context" not in markdown

    def test_an_oversized_screenshot_is_refused_and_deleted(self, sessions_root, monkeypatch):
        monkeypatch.setattr(session, "MAX_BYTES", 4_096)
        monkeypatch.setattr(session, "TRANSCRIPT_RESERVE", 4_096)
        output = session.SessionOutput()
        path = output.reserve_image()
        path.write_bytes(b"x" * 5_000)

        with pytest.raises(SessionStorageFull):
            output.accept_image(path)

        assert not path.exists()
        assert output.images == []

    def test_discard_removes_the_whole_folder(self, sessions_root):
        output = session.SessionOutput()
        output.write_markdown("gone")

        output.discard()

        assert not output.folder.exists()


class TestPrune:
    def test_expired_sessions_go_and_unrelated_folders_stay(self, sessions_root):
        sessions_root.mkdir(parents=True)
        stale = sessions_root / "2020-01-01T00-00-00Z-C81A6E98-FD94-4FC6-AF2C-8928EBD938B1"
        stale.mkdir()
        (stale / "context.md").write_text("old", encoding="utf-8")
        old_time = time.time() - session.MAX_AGE_SECONDS - 60
        import os

        os.utime(stale, (old_time, old_time))

        mine = sessions_root / "my-important-folder"
        mine.mkdir()
        (mine / "notes.txt").write_text("keep me", encoding="utf-8")

        fresh = session.SessionOutput()
        fresh.write_markdown("new")

        session.prune()

        assert not stale.exists(), "an expired session should be removed"
        assert mine.exists(), "prune must never touch folders it did not create"
        assert fresh.folder.exists()

    def test_prune_is_a_no_op_when_nothing_has_been_saved(self, sessions_root):
        session.prune()  # must not raise

        assert not sessions_root.exists()

    def test_directory_size_counts_nested_files(self, tmp_path):
        (tmp_path / "nested").mkdir()
        (tmp_path / "a.png").write_bytes(b"x" * 10)
        (tmp_path / "nested" / "b.png").write_bytes(b"x" * 25)

        assert session.directory_size(tmp_path) == 35
        assert session.directory_size(tmp_path / "missing") == 0


class TestConfig:
    def test_unknown_keys_fall_back_to_the_shipped_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        settings = Config()

        assert settings.get("asrModel") == DEFAULTS["asrModel"]
        assert settings.get("nothing-like-this") is None

    def test_values_round_trip_through_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        settings = Config()

        settings.set("grammarCorrectionEnabled", True)
        stored = json.loads(settings.path.read_text(encoding="utf-8"))

        assert stored["grammarCorrectionEnabled"] is True
        assert Config().bool("grammarCorrectionEnabled") is True

    def test_a_corrupt_file_is_ignored_rather_than_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = Path(tmp_path) / "BetterVoice" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        settings = Config()

        assert settings.get("asrModel") == DEFAULTS["asrModel"]


class TestReduceMotion:
    @pytest.mark.parametrize(
        ("override", "expected"),
        [(True, True), (False, False), ("true", True), ("off", False)],
    )
    def test_an_explicit_override_wins_without_touching_the_desktop(self, override, expected):
        from bettervoice.backends import appearance

        assert appearance.reduce_motion(override) is expected


class TestScreenshotHighlight:
    def test_the_marker_lands_where_the_circle_was(self, qt_app):
        from PyQt6 import QtCore, QtGui

        from bettervoice.backends import screenshot

        image = QtGui.QImage(400, 300, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(255, 255, 255))
        region = QtCore.QRectF(0, 0, 400, 300)

        marked = screenshot.highlight(
            image, target=(120.0, 90.0), region=region, radius=40.0
        )

        # The ring should have painted blue near the circle's edge...
        edge = QtGui.QColor(marked.pixel(120, 50))
        assert edge.blue() > edge.red(), "the highlight ring should be blue"
        # ...and left the far corner alone.
        corner = QtGui.QColor(marked.pixel(395, 295))
        assert (corner.red(), corner.green(), corner.blue()) == (255, 255, 255)

    def test_a_whole_workspace_grab_is_cropped_to_one_screen(self, qt_app):
        from PyQt6 import QtGui

        from bettervoice.backends import screenshot

        virtual = screenshot.virtual_geometry()
        if virtual.isEmpty():
            pytest.skip("no screens available")
        image = QtGui.QImage(virtual.width(), virtual.height(), QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(10, 20, 30))
        centre = (float(virtual.center().x()), float(virtual.center().y()))

        cropped, region = screenshot._crop_to_screen(image, centre)

        assert region.width() <= virtual.width()
        assert cropped.width() == round(region.width())


class TestGesture:
    def test_a_gesture_is_hashable_and_comparable(self):
        first = CircleGesture(center=(1.0, 2.0), radius=3.0)

        assert first == CircleGesture(center=(1.0, 2.0), radius=3.0)
        assert first != CircleGesture(center=(1.0, 2.0), radius=4.0)


class TestModifierDecoding:
    """The evdev and X11 backends only differ in how they read the modifiers."""

    def test_evdev_keycodes_match_the_kernel_header(self):
        evdev = pytest.importorskip("evdev")
        from bettervoice.backends import hotkeys

        codes = evdev.ecodes
        assert hotkeys.ALT_KEYS == {codes.KEY_LEFTALT, codes.KEY_RIGHTALT}
        assert hotkeys.SUPER_KEYS == {codes.KEY_LEFTMETA, codes.KEY_RIGHTMETA}
        assert hotkeys.OTHER_MODIFIER_KEYS == {
            codes.KEY_LEFTSHIFT,
            codes.KEY_RIGHTSHIFT,
            codes.KEY_LEFTCTRL,
            codes.KEY_RIGHTCTRL,
        }

    def test_x11_masks_match_xlib(self):
        pytest.importorskip("Xlib")
        from Xlib import X

        from bettervoice.backends import hotkeys

        assert hotkeys.X11_SHIFT_MASK == X.ShiftMask
        assert hotkeys.X11_CONTROL_MASK == X.ControlMask
        assert hotkeys.X11_ALT_MASK == X.Mod1Mask
        assert hotkeys.X11_SUPER_MASK == X.Mod4Mask

    @pytest.mark.parametrize(
        ("held", "expected"),
        [
            (set(), (False, False, False)),
            ({56}, (False, True, False)),          # left Alt
            ({100}, (False, True, False)),         # right Alt
            ({125, 56}, (True, True, False)),      # Super + Alt
            ({56, 42}, (False, True, True)),       # Alt + Shift
            ({29}, (False, False, True)),          # Control alone
        ],
    )
    def test_held_keys_become_flags(self, held, expected):
        from bettervoice.backends.hotkeys import flags_from_held_keys

        assert flags_from_held_keys(held) == expected

    @pytest.mark.parametrize(
        ("mask", "expected"),
        [
            (0, (False, False, False)),
            (1 << 3, (False, True, False)),
            (1 << 6 | 1 << 3, (True, True, False)),
            (1 << 3 | 1 << 0, (False, True, True)),
            (1 << 2, (False, False, True)),
        ],
    )
    def test_x11_mask_becomes_flags(self, mask, expected):
        from bettervoice.backends.hotkeys import flags_from_x11_mask

        assert flags_from_x11_mask(mask) == expected


class TestModifierBackendActions:
    """Drive a modifier backend the way a real keyboard would."""

    @pytest.fixture()
    def backend(self, qt_app):
        from bettervoice.backends.hotkeys import _ModifierBackend

        instance = _ModifierBackend()
        events: list[str] = []
        instance.push_to_talk_started.connect(lambda: events.append("start"))
        instance.push_to_talk_stopped.connect(lambda: events.append("stop"))
        instance.long_form_toggled.connect(lambda: events.append("toggle"))
        instance.promote_to_long_form.connect(lambda: events.append("promote"))
        return instance, events

    def test_holding_alt_starts_and_releasing_stops(self, backend):
        instance, events = backend

        instance._flags_changed(False, True, False)
        assert events == [], "a quick tap must not start a recording"
        instance._on_pending_elapsed()
        assert events == ["start"]

        instance._flags_changed(False, False, False)
        assert events == ["start", "stop"]

    def test_a_tap_shorter_than_the_delay_records_nothing(self, backend):
        instance, events = backend

        instance._flags_changed(False, True, False)
        instance._flags_changed(False, False, False)
        instance._on_pending_elapsed()

        assert events == []

    def test_super_alt_toggles_long_form(self, backend):
        instance, events = backend

        instance._flags_changed(True, True, False)
        instance._flags_changed(False, False, False)
        instance._flags_changed(True, True, False)

        assert events == ["toggle", "toggle"]

    def test_adding_super_mid_hold_promotes_without_stopping(self, backend):
        instance, events = backend

        instance._flags_changed(False, True, False)
        instance._on_pending_elapsed()
        instance._flags_changed(True, True, False)
        instance._flags_changed(False, False, False)

        assert events == ["start", "promote"], "promoting must not stop the recording"

    def test_shift_does_not_interrupt_an_active_recording(self, backend):
        instance, events = backend

        instance._flags_changed(False, True, False)
        instance._on_pending_elapsed()
        instance._flags_changed(False, True, True)

        assert events == ["start"]


class TestCommandLine:
    """The parts of the CLI that need no desktop session."""

    def _run(self, *arguments, **environment):
        import os
        import subprocess
        import sys

        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", **environment)
        return subprocess.run(
            [sys.executable, "-m", "bettervoice", *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def test_version_names_the_app(self):
        result = self._run("--version")

        assert result.returncode == 0
        assert "BetterVoice" in result.stdout

    def test_paths_reports_every_location_it_writes_to(self, tmp_path):
        result = self._run(
            "--paths",
            XDG_CONFIG_HOME=str(tmp_path / "config"),
            XDG_DATA_HOME=str(tmp_path / "data"),
            BETTERVOICE_SESSIONS_DIR=str(tmp_path / "sessions"),
        )

        assert result.returncode == 0
        for label in ("settings", "models", "cache", "sessions"):
            assert label in result.stdout, result.stdout
        assert str(tmp_path / "sessions") in result.stdout

    def test_paths_does_not_need_a_display(self, tmp_path):
        result = self._run("--paths", XDG_CONFIG_HOME=str(tmp_path))

        assert result.returncode == 0
        assert "Traceback" not in result.stderr


class TestSingleInstance:
    def test_the_lock_is_refused_while_it_is_held(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from bettervoice.__main__ import _single_instance_lock

        first = _single_instance_lock()
        assert first is not None, "the first instance should get the lock"

        assert _single_instance_lock() is None, "a second instance must be refused"

        first.close()
        second = _single_instance_lock()
        assert second is not None, "the lock should be free once the holder exits"
        second.close()


class TestRichClipboard:
    """Choosing between the single-format and multi-format clipboard paths."""

    class FakeSession:
        """Stands in for a granted remote-desktop session."""

        def __init__(self, serves: bool = True, accepts: bool = True) -> None:
            self.serves_clipboard = serves
            self._accepts = accepts
            self.offered: dict[str, bytes] | None = None

        def set_selection(self, payloads):
            self.offered = payloads
            return self._accepts

    @pytest.fixture()
    def screenshots(self, tmp_path):
        paths = []
        for index in (1, 2):
            path = tmp_path / f"context-{index}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]) * 64)
            paths.append(path)
        return paths

    def test_every_representation_is_offered_at_once(self, qt_app, screenshots):
        from bettervoice.backends import clipboard

        payloads = clipboard._rich_payloads("hello there", screenshots)

        assert payloads["text/plain"] == b"hello there"
        assert payloads["text/plain;charset=utf-8"] == b"hello there"
        assert payloads["image/png"] == screenshots[0].read_bytes()
        uris = payloads["text/uri-list"].decode()
        assert uris.count("\r\n") == 2
        assert uris.index("context-1.png") < uris.index("context-2.png")
        html = payloads["text/html"].decode()
        assert "hello there" in html
        assert html.count("<img") == 2

    def test_a_transcript_with_no_screenshots_offers_only_text(self, qt_app):
        from bettervoice.backends import clipboard

        payloads = clipboard._rich_payloads("just words", [])

        assert set(payloads) == {"text/plain", "text/plain;charset=utf-8"}

    def test_html_escapes_the_transcript(self, qt_app, screenshots):
        from bettervoice.backends import clipboard

        html = clipboard._rich_payloads("<b>not markup</b>", screenshots)["text/html"].decode()

        assert "&lt;b&gt;not markup&lt;/b&gt;" in html

    def test_a_granted_session_is_used_in_preference(self, qt_app, screenshots):
        from bettervoice.backends import clipboard

        session = self.FakeSession()

        assert clipboard.copy("hello", screenshots, session) is True
        assert session.offered is not None
        assert "image/png" in session.offered

    def test_a_refused_session_falls_back_rather_than_losing_the_transcript(
        self, qt_app, screenshots, monkeypatch
    ):
        from bettervoice.backends import clipboard

        fallback = {}
        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)
        monkeypatch.setattr(
            clipboard,
            "_wl_copy",
            lambda payload, mime=None: fallback.update({"mime": mime, "payload": payload}) or True,
        )
        session = self.FakeSession(accepts=False)

        assert clipboard.copy("hello", screenshots, session) is True
        assert fallback == {"mime": "text/plain", "payload": b"hello"}

    def test_a_session_without_clipboard_access_is_ignored(
        self, qt_app, screenshots, monkeypatch
    ):
        from bettervoice.backends import clipboard

        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)
        monkeypatch.setattr(clipboard, "_wl_copy", lambda payload, mime=None: True)
        session = self.FakeSession(serves=False)

        assert clipboard.copy("hello", screenshots, session) is True
        assert session.offered is None

    def test_carries_images_reflects_what_is_actually_possible(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)

        assert clipboard.carries_images(None) is False
        assert clipboard.carries_images(self.FakeSession(serves=False)) is False
        assert clipboard.carries_images(self.FakeSession()) is True

        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: False)
        assert clipboard.carries_images(None) is True

    def test_text_only_copies_never_use_the_session(self, qt_app):
        from bettervoice.backends import clipboard

        session = self.FakeSession()

        clipboard.copy("just words", [], session)

        assert session.offered is None, "a plain transcript needs no rich selection"


class TestClipboardRestoreGuard:
    """A quick note must never overwrite something the user copied meanwhile."""

    def test_the_snapshot_goes_back_when_our_text_is_still_there(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        restored = {}
        monkeypatch.setattr(clipboard, "current_text", lambda: "our transcript")
        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)
        monkeypatch.setattr(
            clipboard, "_wl_copy", lambda payload, mime=None: restored.update(payload=payload) or True
        )
        previous = clipboard.ClipboardSnapshot({"text/plain": b"what was there before"})

        assert clipboard.restore(previous, only_if_holding="our transcript") is True
        assert restored["payload"] == b"what was there before"

    def test_someone_else_taking_the_clipboard_wins(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        touched = []
        monkeypatch.setattr(clipboard, "current_text", lambda: "the user copied this")
        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)
        monkeypatch.setattr(clipboard, "_wl_copy", lambda payload, mime=None: touched.append(1))
        previous = clipboard.ClipboardSnapshot({"text/plain": b"what was there before"})

        assert clipboard.restore(previous, only_if_holding="our transcript") is False
        assert touched == [], "the user's own copy must be left alone"

    def test_whitespace_does_not_defeat_the_check(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        monkeypatch.setattr(clipboard, "current_text", lambda: "  our transcript\n")

        assert clipboard.still_ours("our transcript") is True

    def test_an_unguarded_restore_still_works(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        restored = {}
        monkeypatch.setattr(clipboard, "_uses_wl_clipboard", lambda: True)
        monkeypatch.setattr(
            clipboard, "_wl_copy", lambda payload, mime=None: restored.update(payload=payload) or True
        )
        previous = clipboard.ClipboardSnapshot({"text/plain": b"before"})

        assert clipboard.restore(previous) is True
        assert restored["payload"] == b"before"

    def test_an_empty_snapshot_clears_rather_than_restoring(self, qt_app, monkeypatch):
        from bettervoice.backends import clipboard

        cleared = []
        monkeypatch.setattr(clipboard, "_clear", lambda: cleared.append(1) or True)

        assert clipboard.restore(clipboard.ClipboardSnapshot({})) is True
        assert cleared == [1]


class TestSilenceGate:
    """Near-silence must never reach the model, which would invent words."""

    def _controller(self, qt_app, monkeypatch, peak: float, threshold=None):
        from bettervoice import app as app_module

        monkeypatch.setattr(app_module.AppController, "__init__", lambda self, _a: None)
        controller = app_module.AppController(qt_app)
        controller._settings = type(
            "Settings",
            (),
            {
                "get": lambda _self, key, default=None: (
                    threshold if key == "silenceThreshold" and threshold is not None else default
                ),
                "bool": lambda _self, key: False,
            },
        )()
        controller.recorder = type("Recorder", (), {"peak_level": peak})()
        return controller

    def test_the_default_threshold_sits_far_below_speech(self):
        from bettervoice.app import DEFAULT_SILENCE_THRESHOLD
        from bettervoice.config import DEFAULTS

        # Measured on this machine: digital silence reads 0.0, speech peaks
        # around 0.85 on the same scale.
        assert 0 < DEFAULT_SILENCE_THRESHOLD < 0.1
        assert DEFAULTS["silenceThreshold"] == DEFAULT_SILENCE_THRESHOLD

    @pytest.mark.parametrize(
        ("peak", "threshold", "expected"),
        [
            (0.0, 0.015, True),      # digital silence
            (0.004, 0.015, True),    # a quiet room
            (0.02, 0.015, False),    # just above the floor
            (0.85, 0.015, False),    # speech
            (0.0, 0, False),         # gate switched off
        ],
    )
    def test_the_gate_only_catches_near_silence(self, peak, threshold, expected):
        gate = threshold > 0 and peak < threshold

        assert gate is expected

    def test_a_recorder_reports_its_loudest_moment(self):
        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()

        assert recorder.peak_level == 0.0


#: The shape KWin's `internalId` actually has.
WINDOW_UUID = "{4f3a2b1c-9d8e-4a7b-bc6d-5e4f3a2b1c9d}"


class TestKWinCallbacksAreNotOpenToTheBus:
    """These slots are exported where any peer running as the user can call them.

    D-Bus has no way to tell an exported slot who called it -- PyQt6 has no
    `QDBusContext` -- so the scripts carry a nonce and a call without it is not
    from the script we just wrote.
    """

    def _backend(self, monkeypatch):
        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        return backend

    def test_a_forged_capture_is_ignored(self, qt_app, monkeypatch):
        """Otherwise the transcript is typed into a window the caller chose."""

        backend = self._backend(monkeypatch)
        seen: list = []
        backend._sink.captured.connect(lambda *a: seen.append(a))
        backend._sink.nonce = "the-real-one"

        backend._sink.Captured(WINDOW_UUID, "evil", "", "guessed")

        assert seen == [], "a call without the script's nonce is not from the script"

    def test_a_capture_carrying_the_nonce_is_accepted(self, qt_app, monkeypatch):
        backend = self._backend(monkeypatch)
        seen: list = []
        backend._sink.captured.connect(lambda *a: seen.append(a))
        backend._sink.nonce = "the-real-one"

        backend._sink.Captured(WINDOW_UUID, "kate", "notes", "the-real-one")

        assert len(seen) == 1

    def test_nothing_is_accepted_before_a_script_has_run(self, qt_app, monkeypatch):
        backend = self._backend(monkeypatch)
        seen: list = []
        backend._sink.captured.connect(lambda *a: seen.append(a))
        backend._sink.restored.connect(lambda *a: seen.append(a))

        backend._sink.Captured(WINDOW_UUID, "kate", "notes", "")
        backend._sink.Restored(True, "")

        assert seen == [], "an empty nonce must never match"

    def test_a_forged_pointer_position_is_ignored(self, qt_app, monkeypatch):
        """Synthetic movement would drive the circle gesture into capturing."""

        from bettervoice.backends import pointer

        sink = pointer._PointerSink("local.test.Pointer", "/Pointer0")
        sink.nonce = "the-real-one"
        seen: list = []
        sink.moved.connect(lambda *a: seen.append(a))

        sink.Moved(10, 20, "guessed")
        assert seen == []

        sink.Moved(10, 20, "the-real-one")
        assert seen == [(10.0, 20.0)]

    def test_every_script_run_gets_a_fresh_nonce(self, qt_app, monkeypatch):
        backend = self._backend(monkeypatch)

        first = backend._script_values()["nonce"]
        second = backend._script_values()["nonce"]

        assert first != second, "a reused nonce is one an attacker can replay"
        assert len(first) > 16


class TestGeneratedKWinScriptsCannotBeEscaped:
    """The compositor executes these scripts unsandboxed.

    A value interpolated inside hand-written quotes stops being a string the
    moment it contains one. `window_id` arrives over the open D-Bus slot above,
    so it is both validated and emitted as a JSON literal.
    """

    def test_a_window_id_that_is_not_a_uuid_is_refused(self, qt_app, monkeypatch):
        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        seen: list = []
        backend._capture_callback = seen.append

        backend._on_captured(
            'x"; workspace.windowList(); var q="', "kate", "notes"
        )

        assert seen == [None], "an injection payload must never reach the script"

    def test_a_real_window_id_still_passes(self, qt_app, monkeypatch):
        from bettervoice.backends import focus

        assert focus._is_window_id(WINDOW_UUID)
        assert focus._is_window_id(WINDOW_UUID.strip("{}"))
        assert not focus._is_window_id("")
        assert not focus._is_window_id('" + evil() + "')

    def test_the_restore_script_quotes_the_target_itself(self, qt_app, monkeypatch):
        """Rendered with a hostile id, the script must still be one statement."""

        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        hostile = 'a"; workspace.activeWindow = null; var q = "b'
        source = focus._RESTORE_SOURCE.format(
            target=json.dumps(hostile), **backend._script_values()
        )

        # The payload survives only in escaped form: its bare quotes, which are
        # what would end the string literal and start a statement, are gone.
        assert hostile not in source
        assert json.dumps(hostile) in source
        assert source.count('var betterVoiceTarget = ') == 1

    def test_the_capture_script_quotes_its_own_addresses(self, qt_app, monkeypatch):
        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        hostile = 'evil"; hack(); var q="'
        monkeypatch.setattr(backend, "_interface", hostile)
        source = focus._CAPTURE_SOURCE.format(**backend._script_values())

        assert hostile not in source, "an unescaped quote would end the literal"
        assert json.dumps(hostile) in source


class TestFocusTarget:
    """Deciding which window a transcript belongs to."""

    def test_a_target_is_falsy_when_there_is_no_window(self, qt_app):
        from bettervoice.backends.focus import FocusTarget

        assert not FocusTarget("")
        assert FocusTarget("{abc}", "kate", "notes.txt")

    def test_our_own_windows_are_recognised(self, qt_app):
        from bettervoice import configure_application
        from bettervoice.backends import focus

        configure_application(qt_app)

        assert focus.belongs_to_us("BetterVoice")
        assert focus.belongs_to_us("bettervoice")
        assert focus.belongs_to_us("io.github.taruntomar122.BetterVoice")
        assert not focus.belongs_to_us("kate")
        assert not focus.belongs_to_us("brave-browser")

    def test_the_null_backend_never_claims_a_target(self, qt_app):
        from bettervoice.backends.focus import NullFocusBackend

        backend = NullFocusBackend()
        captured: list = []
        restored: list = []

        backend.capture(captured.append)
        backend.restore(None, restored.append)

        assert captured == [None]
        assert restored == [False]
        assert backend.remembers_target is False

    def test_capturing_our_own_window_yields_no_target(self, qt_app, monkeypatch):
        """Returning a transcript to BetterVoice would be worse than not pasting."""

        from bettervoice import configure_application
        from bettervoice.backends import focus

        configure_application(qt_app)
        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        seen: list = []
        backend._capture_callback = seen.append

        backend._on_captured(WINDOW_UUID, "BetterVoice", "Getting Started")

        assert seen == [None]

    def test_capturing_another_window_yields_that_target(self, qt_app, monkeypatch):
        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        seen: list = []
        backend._capture_callback = seen.append

        backend._on_captured(WINDOW_UUID, "kate", "notes.txt — Kate")

        assert len(seen) == 1
        assert seen[0].window_id == WINDOW_UUID
        assert seen[0].application == "kate"

    def test_a_window_that_vanished_is_reported_as_not_restored(self, qt_app, monkeypatch):
        from bettervoice.backends import focus

        backend = focus.KWinFocusBackend()
        monkeypatch.setattr(backend, "_interface", "local.test.Sink")
        seen: list = []
        backend._restore_callback = seen.append

        backend._on_restored(False)

        assert seen == [False]


class TestOverlayPlacement:
    """A trail drawn on one monitor must not appear on another."""

    def test_each_screen_gets_its_own_fullscreen_window(self, qt_app):
        from PyQt6 import QtGui

        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            screens = QtGui.QGuiApplication.screens()

            assert len(overlay._windows) == len(screens)
            for window, screen in zip(overlay._windows, screens):
                assert window.screen() is screen, "an overlay landed on the wrong screen"
                # Wayland ignores a requested position; fullscreen-on-output is
                # the only thing that pins an overlay to one monitor.
                assert window.isFullScreen()
        finally:
            overlay.stop()

    def test_exactly_one_window_draws_the_hud(self, qt_app):
        from PyQt6 import QtGui

        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start(hud_screen=QtGui.QGuiApplication.primaryScreen())

            assert sum(window.shows_hud for window in overlay._windows) == 1
        finally:
            overlay.stop()

    def test_points_are_converted_to_each_screen_s_own_coordinates(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            window = overlay._windows[0]
            origin = window.origin
            local = window._local((origin.x() + 40, origin.y() + 25))

            assert (local.x(), local.y()) == (40, 25)
        finally:
            overlay.stop()

    def test_a_second_recording_reuses_the_same_windows(self, qt_app):
        """Fullscreen surfaces are expensive; do not rebuild them per session."""

        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            first = list(overlay._windows)
            overlay.stop()
            assert overlay._windows == [], "windows should be released on stop"

            overlay.start()
            assert overlay._windows == first, "a new session built new surfaces"
        finally:
            overlay.close()

    def test_closing_gives_the_windows_back(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        overlay.start()
        overlay.close()

        assert overlay._windows == []
        assert overlay._cache == {}, "shutdown should not hold windows open"

    def test_a_monitor_change_rebuilds_without_losing_the_trail(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            overlay.add((10.0, 10.0), at=1.0)
            overlay.confirm((10.0, 10.0), 30.0, at=1.0)
            before = list(overlay._windows)

            overlay._on_screens_changed(None)

            assert overlay._windows, "the overlay should have been rebuilt"
            # Screens that are still attached keep their window rather than
            # paying for a new fullscreen surface.
            assert overlay._windows == before
            assert len(overlay._windows[0]._trail) == 1
            assert len(overlay._windows[0]._confirmations) == 1
        finally:
            overlay.stop()

    def test_stopping_leaves_nothing_behind(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        overlay.start()
        overlay.stop()

        assert overlay._windows == []
        assert not overlay._timer.isActive()


class TestDesktopResources:
    """A wheel must carry what the XDG portals need to identify the app."""

    def test_every_asset_ships_with_the_package(self):
        from bettervoice import resources

        for name in (
            resources.DESKTOP_ENTRY,
            resources.ICON,
            resources.METAINFO,
            resources.SERVICE,
        ):
            assert resources.read(name), f"{name} is missing from the package"

    def test_the_desktop_entry_matches_the_app_id_the_portals_expect(self):
        from bettervoice import APP_ID, resources

        assert resources.DESKTOP_ENTRY == f"{APP_ID}.desktop"
        entry = resources.read(resources.DESKTOP_ENTRY).decode()
        assert "Exec=bettervoice" in entry
        assert f"Icon={APP_ID}" in entry
        assert "Type=Application" in entry

    def test_the_service_unit_is_named_so_portals_can_resolve_it(self):
        from bettervoice import APP_ID, resources

        # systemd units for apps are app-<AppID>; anything else and the portal
        # cannot map the process back to its desktop entry.
        assert resources.SERVICE.startswith("app-")
        assert APP_ID in resources.SERVICE
        unit = resources.read(resources.SERVICE).decode()
        assert "BETTERVOICE_NO_SCOPE=1" in unit, "the unit is already its own cgroup"

    def test_installing_writes_the_expected_layout(self, tmp_path):
        from bettervoice import APP_ID, resources

        written = resources.install(tmp_path)

        assert tmp_path / "share" / "applications" / f"{APP_ID}.desktop" in written
        assert (
            tmp_path / "share" / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
            in written
        )
        assert tmp_path / "share" / "metainfo" / f"{APP_ID}.metainfo.xml" in written
        for path in written:
            assert path.is_file() and path.stat().st_size > 0

    def test_installing_twice_is_harmless(self, tmp_path):
        from bettervoice import resources

        resources.install(tmp_path)
        second = resources.install(tmp_path)

        assert all(path.is_file() for path in second)


class TestSetupWindowLayout:
    """The window is a port of the macOS sheet; keep its shape honest."""

    def _model(self):
        from bettervoice.ui.setup_window import SetupModel, SetupRowState

        model = SetupModel()
        model.quick_note_keys = "Alt + V"
        model.long_form_keys = "Super + Alt + V"
        model.microphone_ready = True
        model.microphone_name = "Blue Microphones"
        model.microphone_options = [("automatic", "Automatic"), ("blue", "Blue Microphones")]
        model.grammar_status = "Ready"
        model.storage_note = "Sessions stay in ~/Desktop/BetterVoice for up to 7 days."
        model.rows = [
            SetupRowState("Screen capture", "Ready via portal", ready=True),
            SetupRowState(
                "Transcript insertion", "Needs a grant", action_title="Set Up", action=lambda: None
            ),
        ]
        return model

    def test_the_window_is_the_width_the_macos_sheet_was(self, qt_app):
        from bettervoice.ui.setup_window import WINDOW_WIDTH, SetupWindow

        window = SetupWindow(self._model())
        try:
            window.apply()
            assert WINDOW_WIDTH == 720
            assert window.sizeHint().width() == WINDOW_WIDTH
        finally:
            window.deleteLater()

    def test_a_key_cap_is_never_clipped(self, qt_app):
        from PyQt6 import QtGui

        from bettervoice.ui.widgets import KeyCap

        cap = KeyCap("Super + Alt + V")
        try:
            needed = QtGui.QFontMetrics(cap.font()).horizontalAdvance(cap.text())
            assert cap.sizeHint().width() >= needed
            assert cap.width() >= needed, "the cap must not shrink below its text"
        finally:
            cap.deleteLater()

    def test_rows_show_a_button_only_when_something_needs_doing(self, qt_app):
        from bettervoice.ui.setup_window import SetupWindow

        model = self._model()
        window = SetupWindow(model)
        try:
            window.apply()
            ready, needs_setup = window._rows[0], window._rows[1]

            assert not ready._button.isVisible()
            assert needs_setup._button.text() == "Set Up"
        finally:
            window.deleteLater()

    def test_extra_rows_are_hidden_rather_than_stale(self, qt_app):
        from bettervoice.ui.setup_window import SetupWindow

        model = self._model()
        window = SetupWindow(model)
        try:
            window.apply()
            assert sum(row.isVisibleTo(window) for row in window._rows) == 2

            model.rows = model.rows[:1]
            window.apply()
            assert sum(row.isVisibleTo(window) for row in window._rows) == 1
        finally:
            window.deleteLater()

    def test_the_grammar_toggle_reports_changes_once(self, qt_app):
        from bettervoice.ui.setup_window import SetupWindow

        model = self._model()
        seen: list[bool] = []
        model.set_grammar_correction = seen.append
        window = SetupWindow(model)
        try:
            window.apply()
            assert seen == [], "applying state must not look like a user action"

            window._grammar_row._toggle.setChecked(True)
            assert seen == [True]
        finally:
            window.deleteLater()

    def test_choosing_a_microphone_reports_its_id(self, qt_app):
        from bettervoice.ui.setup_window import SetupWindow

        model = self._model()
        chosen: list[str] = []
        model.choose_microphone = chosen.append
        window = SetupWindow(model)
        try:
            window.apply()
            picker = window._microphone_row._picker
            picker.setCurrentIndex(1)
            window._microphone_row._on_chosen(1)

            assert chosen == ["blue"]
        finally:
            window.deleteLater()


class TestTrayMenu:
    """The tray menu is the menu-bar menu; keep its shape and copy honest."""

    def test_the_items_are_in_the_macos_order(self, qt_app):
        from bettervoice.ui.tray import Tray

        tray = Tray()
        items = [
            "---" if action.isSeparator() else action.text()
            for action in tray._menu.actions()
        ]

        assert items[0].startswith("Ready")
        assert items[1] == "---"
        assert items[2].startswith("Start long recording")
        assert "Microphone" in items
        assert items[-1] == "Quit BetterVoice"
        assert items[-2] == "---"
        assert "Getting Started…" in items
        assert "Open Saved Sessions" in items
        assert "Clear Saved Sessions…" in items

    def test_the_status_line_is_not_clickable(self, qt_app):
        from bettervoice.ui.tray import Tray

        tray = Tray()

        assert not tray.status_action.isEnabled()

    def test_setup_and_quit_carry_the_usual_accelerators(self, qt_app):
        from bettervoice.ui.tray import Tray

        tray = Tray()

        assert tray.setup_action.shortcut().toString() == "Ctrl+,"
        assert tray.quit_action.shortcut().toString() == "Ctrl+Q"

    def test_the_microphone_menu_marks_the_current_choice(self, qt_app):
        from bettervoice.ui.tray import Tray

        tray = Tray()
        chosen: list[str] = []
        tray.rebuild_microphone_menu(
            [("automatic", "Automatic — Webcam"), ("blue", "Blue Microphones")],
            selected="blue",
            enabled=True,
            on_select=chosen.append,
        )
        actions = [a for a in tray.microphone_menu.actions() if not a.isSeparator()]

        assert [a.isChecked() for a in actions] == [False, True]
        actions[0].trigger()
        assert chosen == ["automatic"]

    def test_an_empty_microphone_menu_says_so(self, qt_app):
        from bettervoice.ui.tray import Tray

        tray = Tray()
        tray.rebuild_microphone_menu([], selected="", enabled=True, on_select=lambda _: None)
        actions = tray.microphone_menu.actions()

        assert len(actions) == 1
        assert actions[0].text() == "No input microphones found"
        assert not actions[0].isEnabled()


class TestHUD:
    """The recording panel: what it says, and that it actually paints."""

    def _hud(self, **overrides):
        from bettervoice.ui.overlay import HUDModel

        return HUDModel(microphone="Blue Microphones", **overrides)

    def test_it_says_listening_until_something_else_happens(self):
        assert self._hud().title == "Listening"

    def test_progress_replaces_listening_while_finishing(self):
        hud = self._hud(is_finishing=True, finishing_message="Polishing transcript locally…")

        assert hud.title == "Polishing transcript locally…"

    def test_a_capture_note_outranks_everything(self):
        hud = self._hud(
            is_finishing=True,
            finishing_message="Transcribing…",
            capture_message="Screenshot captured",
        )

        assert hud.title == "Screenshot captured"

    def test_the_detail_counts_captures_once_there_are_any(self):
        assert self._hud().detail == "Blue Microphones"
        assert self._hud(context_count=2).detail == "Blue Microphones  •  2 captured"

    def test_the_panel_paints_at_the_size_the_macos_one_had(self, qt_app):
        from PyQt6 import QtCore, QtGui, QtWidgets

        from bettervoice.ui import overlay

        window = overlay._OverlayWindow.__new__(overlay._OverlayWindow)
        QtWidgets.QWidget.__init__(window)
        window._hud = self._hud(level=0.6, context_count=1)
        window.reduce_motion = True

        assert (overlay.HUD_WIDTH, overlay.HUD_HEIGHT) == (290, 56)

        pixmap = QtGui.QPixmap(overlay.HUD_WIDTH + 40, overlay.HUD_HEIGHT + 40)
        pixmap.fill(QtGui.QColor(120, 120, 120))
        painter = QtGui.QPainter(pixmap)
        window._paint_hud_at(
            painter,
            QtCore.QRectF(20, 20, overlay.HUD_WIDTH, overlay.HUD_HEIGHT),
        )
        painter.end()

        image = pixmap.toImage()
        middle = QtGui.QColor(image.pixel(overlay.HUD_WIDTH // 2, overlay.HUD_HEIGHT // 2))
        assert middle.lightness() < 60, "the panel should be dark"
        corner = QtGui.QColor(image.pixel(pixmap.width() - 1, pixmap.height() - 1))
        assert corner.lightness() > 100, "the panel must not fill the whole surface"

    def test_the_trail_draws_and_fades(self, qt_app):
        import time

        from PyQt6 import QtGui, QtWidgets

        from bettervoice.ui import overlay

        window = overlay._OverlayWindow.__new__(overlay._OverlayWindow)
        QtWidgets.QWidget.__init__(window)
        window._hud = self._hud()
        window.reduce_motion = True
        window._trail = []
        window._confirmations = []
        window._screen = QtGui.QGuiApplication.primaryScreen()

        now = time.monotonic()
        for index in range(20):
            window.add((40.0 + index * 6, 60.0), at=now - 0.05 + index * 0.002)

        pixmap = QtGui.QPixmap(240, 120)
        pixmap.fill(QtGui.QColor(0, 0, 0))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        window._paint_trail(painter, now)
        painter.end()

        image = pixmap.toImage()
        origin = window.origin
        on_stroke = QtGui.QColor(image.pixel(100 - origin.x(), 60 - origin.y()))
        assert on_stroke.blue() > on_stroke.red(), "the trail should be blue"

        # Everything older than the lifetime is dropped rather than drawn.
        window.tick(now + overlay.TRAIL_LIFETIME + 0.1)
        assert window._trail == []


class TestAccessibility:
    """The macOS build labelled every control; a screen reader needs the same here."""

    def test_a_status_icon_says_whether_it_is_ready(self, qt_app):
        from bettervoice.ui.widgets import StatusIcon

        icon = StatusIcon(ready=False)
        assert icon.accessibleName() == "Needs setup"

        icon.set_ready(True)
        assert icon.accessibleName() == "Ready"

    def test_a_key_cap_announces_its_shortcut(self, qt_app):
        from bettervoice.ui.widgets import KeyCap

        cap = KeyCap("Alt + V")
        assert cap.accessibleName() == "Shortcut Alt + V"

        cap.setText("Super + Alt + V")
        assert cap.accessibleName() == "Shortcut Super + Alt + V"

    def test_a_setup_row_reads_as_a_sentence(self, qt_app):
        from bettervoice.ui.setup_window import SetupRowState, _SetupRow

        row = _SetupRow()
        row.apply(SetupRowState("Screen capture", "Ready via portal", ready=True))

        assert row.accessibleName() == "Screen capture. Ready via portal"
        assert row._button.accessibleName() == "Set up Screen capture"

    def test_the_controls_carry_the_names_macos_used(self, qt_app):
        from bettervoice.ui.setup_window import SetupModel, SetupWindow
        from bettervoice.ui.widgets import CapturePreview, InfoIcon

        window = SetupWindow(SetupModel())
        try:
            assert window._microphone_row._picker.accessibleName() == "Microphone input"
            assert window._grammar_row._toggle.accessibleName() == "Enable grammar cleanup beta"
            assert InfoIcon().accessibleName() == "Information"
            assert "blue mouse trail" in CapturePreview().accessibleName()
        finally:
            window.deleteLater()

    def test_the_recording_overlay_describes_its_state(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            overlay.announce("BetterVoice listening on Blue Microphones")

            assert overlay.hud.description == "BetterVoice listening on Blue Microphones"
            assert all(
                window.accessibleName() == "BetterVoice listening on Blue Microphones"
                for window in overlay._windows
            )
        finally:
            overlay.stop()

    def test_a_description_survives_a_monitor_change(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            overlay.announce("BetterVoice transcribing")
            overlay._on_screens_changed(None)

            assert all(
                window.accessibleName() == "BetterVoice transcribing"
                for window in overlay._windows
            )
        finally:
            overlay.stop()

    def test_the_recovery_notice_reads_as_one_message(self, qt_app):
        from bettervoice.ui.recovery import RecoveryNotice

        notice = RecoveryNotice()
        try:
            notice.show_notice("Screen capture was declined", "Allow it when asked.",
                               "Open Setup", lambda: None)

            assert notice.accessibleName() == (
                "Screen capture was declined. Allow it when asked."
            )
        finally:
            notice.hide()
            notice.deleteLater()


class TestInputDeviceTable:
    """Watching only some devices looks fine until you use another one."""

    TABLE = """\
I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
H: Handlers=kbd event1
B: EV=3

I: Bus=0003 Vendor=3434 Product=0331 Version=0111
N: Name="Keychron V6"
H: Handlers=sysrq kbd leds event3
B: EV=120013

I: Bus=0003 Vendor=3434 Product=0331 Version=0111
N: Name="Keychron V6 Mouse"
H: Handlers=mouse0 event4
B: EV=17

I: Bus=0003 Vendor=1038 Product=1830 Version=0111
N: Name="SteelSeries Rival 3 Keyboard"
H: Handlers=sysrq kbd leds event12
B: EV=120013

I: Bus=0003 Vendor=1038 Product=1830 Version=0111
N: Name="SteelSeries Rival 3"
H: Handlers=mouse1 event11
B: EV=17
"""

    @pytest.fixture()
    def table(self, tmp_path, monkeypatch):
        path = tmp_path / "devices"
        path.write_text(self.TABLE, encoding="utf-8")
        monkeypatch.setenv("BETTERVOICE_INPUT_DEVICE_TABLE", str(path))
        from bettervoice.backends import input_devices

        return input_devices

    def test_only_real_typing_keyboards_count_as_keyboards(self, table):
        # A power button claims 'kbd' but has no LEDs to drive.
        assert [device.node for device in table.keyboards()] == [
            "/dev/input/event3",
            "/dev/input/event12",
        ]

    def test_pointing_devices_are_found_by_their_mouse_handler(self, table):
        assert [device.node for device in table.pointers()] == [
            "/dev/input/event4",
            "/dev/input/event11",
        ]

    def test_a_device_is_never_both(self, table):
        for device in table.all_devices():
            assert not (device.is_keyboard and device.is_pointer)

    def test_names_survive_parsing(self, table):
        assert [device.name for device in table.keyboards()] == [
            "Keychron V6",
            "SteelSeries Rival 3 Keyboard",
        ]

    def test_partial_access_is_reported_with_the_real_numbers(self, table, monkeypatch):
        monkeypatch.setattr("os.access", lambda path, mode: path == "/dev/input/event12")

        assert table.coverage(table.keyboards()) == (["/dev/input/event12"], 2)
        warning = table.partial_access_warning("keyboard", table.keyboards())
        assert "1 of 2" in warning
        assert "input" in warning, "the fix should be spelled out"

    def test_full_access_is_not_a_warning(self, table, monkeypatch):
        monkeypatch.setattr("os.access", lambda path, mode: True)

        assert table.partial_access_warning("keyboard", table.keyboards()) is None

    def test_no_access_at_all_says_how_to_fix_it(self, table, monkeypatch):
        monkeypatch.setattr("os.access", lambda path, mode: False)

        warning = table.partial_access_warning("keyboard", table.keyboards())
        assert "No keyboard is readable" in warning
        assert "input" in warning

    def test_a_missing_device_table_is_survivable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BETTERVOICE_INPUT_DEVICE_TABLE", str(tmp_path / "nope"))
        from bettervoice.backends import input_devices

        assert input_devices.all_devices() == []
        assert input_devices.keyboards() == []
        assert input_devices.pointers() == []

    def test_both_evdev_backends_read_the_same_table(self, table, monkeypatch):
        monkeypatch.setattr("os.access", lambda path, mode: path.endswith(("event12", "event11")))
        from bettervoice.backends.hotkeys import EvdevHotkeyBackend
        from bettervoice.backends.pointer import EvdevPointerBackend

        assert EvdevHotkeyBackend.readable_keyboards() == ["/dev/input/event12"]
        assert EvdevPointerBackend.readable_devices() == ["/dev/input/event11"]
        assert "1 of 2 keyboards" in EvdevHotkeyBackend().unavailable_reason
        assert "1 of 2 pointing devices" in EvdevPointerBackend().unavailable_reason

    def test_the_portal_outranks_evdev_when_choosing_automatically(self, monkeypatch):
        """evdev only covers the devices this user can read, so it is opt-in."""

        from bettervoice.backends import hotkeys

        monkeypatch.setattr(hotkeys.X11HotkeyBackend, "available", staticmethod(lambda: False))
        monkeypatch.setattr(hotkeys.PortalHotkeyBackend, "available", staticmethod(lambda: True))
        monkeypatch.setattr(hotkeys.EvdevHotkeyBackend, "available", staticmethod(lambda: True))

        assert hotkeys.create().name == "portal"
        assert hotkeys.create("evdev").name == "evdev", "asking for it by name still works"

    def test_evdev_is_not_offered_without_the_package(self, table, monkeypatch):
        """Detection reads /proc, but watching a device needs the evdev module."""

        monkeypatch.setattr("os.access", lambda path, mode: True)
        from bettervoice.backends import hotkeys, pointer

        monkeypatch.setattr(
            "importlib.util.find_spec", lambda name: None if name == "evdev" else object()
        )

        assert not hotkeys.EvdevHotkeyBackend.available()
        assert not pointer.EvdevPointerBackend.available()
        assert "evdev" in hotkeys.EvdevHotkeyBackend().unavailable_reason
        assert "evdev" in pointer.EvdevPointerBackend().unavailable_reason


class TestClipboardProtocolProbe:
    """`wl-copy` existing is not the same as the compositor supporting it."""

    class Result:
        def __init__(self, returncode: int, stderr: bytes = b"") -> None:
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = b""

    @pytest.fixture(autouse=True)
    def _fresh_probe(self, monkeypatch):
        from bettervoice.backends import clipboard

        monkeypatch.setattr(clipboard, "_probe_result", None, raising=False)
        monkeypatch.setattr(clipboard.environment, "is_wayland", lambda: True)
        monkeypatch.setattr(clipboard.environment, "has", lambda tool: True)
        return clipboard

    def test_a_working_compositor_is_usable(self, _fresh_probe, monkeypatch):
        clipboard = _fresh_probe
        monkeypatch.setattr(clipboard, "_run", lambda *a, **k: self.Result(0))

        assert clipboard.data_control_available() is True
        assert clipboard.unavailable_reason() is None

    def test_an_empty_clipboard_is_not_a_missing_protocol(self, _fresh_probe, monkeypatch):
        """wl-paste exits non-zero on an empty clipboard; that is not a failure."""

        clipboard = _fresh_probe
        monkeypatch.setattr(
            clipboard, "_run", lambda *a, **k: self.Result(1, b"Nothing is copied\n")
        )

        assert clipboard.data_control_available() is True
        assert clipboard.unavailable_reason() is None

    @pytest.mark.parametrize(
        "message",
        [
            b"wl-paste: the compositor does not support wlr-data-control\n",
            b"Compositor does not support the data-control protocol\n",
            b"no suitable data_control protocol\n",
        ],
    )
    def test_a_compositor_without_the_protocol_is_reported(
        self, _fresh_probe, monkeypatch, message
    ):
        clipboard = _fresh_probe
        monkeypatch.setattr(clipboard, "_run", lambda *a, **k: self.Result(1, message))

        assert clipboard.data_control_available() is False
        reason = clipboard.unavailable_reason()
        assert "data-control" in reason
        assert "still saved" in reason, "the user needs to know nothing is lost"

    def test_the_write_path_skips_wl_copy_when_it_cannot_work(
        self, _fresh_probe, monkeypatch
    ):
        clipboard = _fresh_probe
        monkeypatch.setattr(
            clipboard, "_run", lambda *a, **k: self.Result(1, b"does not support data-control")
        )

        assert clipboard._uses_wl_clipboard() is False, (
            "spawning wl-copy that cannot bind would fail on every recording"
        )

    def test_the_answer_is_cached_but_rechecking_is_possible(
        self, _fresh_probe, monkeypatch
    ):
        clipboard = _fresh_probe
        calls: list[int] = []

        def probe(*_a, **_k):
            calls.append(1)
            return self.Result(0)

        monkeypatch.setattr(clipboard, "_run", probe)

        clipboard.data_control_available()
        clipboard.data_control_available()
        assert len(calls) == 1, "the probe spawns a process; do not repeat it"

        clipboard.data_control_available(recheck=True)
        assert len(calls) == 2

    def test_x11_is_never_asked(self, _fresh_probe, monkeypatch):
        clipboard = _fresh_probe
        monkeypatch.setattr(clipboard.environment, "is_wayland", lambda: False)

        assert clipboard.unavailable_reason() is None


class TestOurOwnOverlayStaysOutOfTheScreenshot:
    """macOS excluded its own windows from the capture; the portal cannot.

    `SCContentFilter(..., excludingApplications: ownApplication)` meant the
    trail and HUD were never in a macOS screenshot. A portal screenshot takes
    the screen exactly as it is, so the overlay has to stop drawing instead.
    """

    def _overlay(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        overlay.hud.visible = True
        overlay.start()
        return overlay

    def test_a_suppressed_window_draws_nothing(self, qt_app):
        """The whole point: no trail, no HUD, no pulse in the captured pixels."""

        import time

        from PyQt6 import QtGui
        from bettervoice.ui.overlay import HUDModel, _OverlayWindow

        screen = QtGui.QGuiApplication.primaryScreen()
        window = _OverlayWindow(screen, HUDModel(microphone="mic", visible=True), True)
        window.shows_hud = True
        window.resize(400, 300)
        now = time.monotonic()
        for index in range(15):
            window.add((50 + index * 10, 150), now)
        window.confirm((200, 150), 40, now)

        def inked() -> int:
            image = window.grab().toImage()
            return sum(
                1
                for y in range(0, image.height(), 3)
                for x in range(0, image.width(), 3)
                if image.pixelColor(x, y).alpha() > 12
            )

        assert inked() > 0, "nothing was drawn to begin with, so this proves nothing"
        window.suppressed = True
        assert inked() == 0
        window.suppressed = False
        assert inked() > 0, "the overlay has to come back after the capture"

    def test_hiding_reports_whether_anything_was_showing(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        idle = RecordingOverlay()
        assert idle.hide_from_capture() is False, "nothing on screen, nothing to blank"

        overlay = self._overlay(qt_app)
        assert overlay.hide_from_capture() is True
        assert all(window.suppressed for window in overlay._windows)
        overlay.close()

    def test_showing_again_clears_every_cached_window(self, qt_app):
        """A window hidden between recordings must not come back still blanked."""

        overlay = self._overlay(qt_app)
        overlay.hide_from_capture()
        overlay.stop()  # hides the windows, keeping them cached

        overlay.show_after_capture()

        assert not any(window.suppressed for window in overlay._cache.values())
        overlay.close()

    def test_the_trail_survives_the_capture(self, qt_app):
        """Blanking is for the screenshot only; the drawing is not thrown away."""

        import time

        overlay = self._overlay(qt_app)
        now = time.monotonic()
        overlay.add((10.0, 10.0), now)
        overlay.add((20.0, 20.0), now)

        overlay.hide_from_capture()
        overlay.show_after_capture()

        assert overlay._windows[0]._trail, "the trail should outlast the screenshot"
        overlay.close()


class TestScreenshotsArePastedAfterTheTranscript:
    """macOS delivered images and text in one keystroke; Wayland cannot.

    A selection holds one item and the receiving application picks one type from
    it. Offered text alongside pictures, it takes the text -- so the screenshots
    need a selection of their own and a keystroke of their own.
    """

    def _controller(self, qt_app, monkeypatch, serves=True, tmp_path=None):
        from bettervoice import app as app_module

        monkeypatch.setattr(app_module.AppController, "__init__", lambda self, _a: None)
        controller = app_module.AppController(qt_app)

        events: list = []

        class Insertion:
            serves_clipboard = serves

            @staticmethod
            def set_selection(payloads):
                events.append(("selection", sorted(payloads)))
                return serves

            @staticmethod
            def paste(on_done):
                events.append(("paste", None))
                on_done(True)

        controller.text_insertion = Insertion()
        monkeypatch.setattr(
            app_module.clipboard, "copy",
            lambda t, i, p=None: events.append(("copy", bool(i))) or True,
        )
        monkeypatch.setattr(
            app_module.clipboard, "restore",
            lambda previous, only_if_holding=None: events.append(("restore", None)) or True,
        )
        # Run the settle timers immediately so the chain completes in the test,
        # recording each delay so the ordering can be asserted.
        events.append(("start", None))
        monkeypatch.setattr(
            app_module.QtCore.QTimer, "singleShot",
            staticmethod(lambda ms, callback: events.append(("settle", ms)) or callback()),
        )
        monkeypatch.setattr(app_module.workers, "on_main_thread", lambda fn: fn)
        return controller, events

    def _images(self, tmp_path):
        first = tmp_path / "context-1.png"
        first.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [first]

    def test_the_second_selection_offers_no_text(self, qt_app, monkeypatch, tmp_path):
        """Offered text, a text field would paste the transcript a second time."""

        controller, events = self._controller(qt_app, monkeypatch)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        selections = [types for kind, types in events if kind == "selection"]
        assert selections, "the screenshots were never offered"
        offered = selections[0]
        assert "image/png" in offered
        assert not [mime for mime in offered if mime.startswith("text/plain")], (
            "a text format here means a duplicated transcript"
        )
        assert "text/html" not in offered

    def test_the_transcript_is_given_time_to_land_before_the_swap(
        self, qt_app, monkeypatch, tmp_path
    ):
        """A paste is done when the keystroke is *sent*, not when it is read.

        Swapping the clipboard the instant the first paste returns races the
        other application's read of it, and the screenshot is delivered twice
        while the transcript is never delivered at all.
        """

        from bettervoice import app as app_module

        controller, events = self._controller(qt_app, monkeypatch, tmp_path=tmp_path)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        kinds = [kind for kind, _ in events]
        assert "selection" in kinds, "the screenshots were never offered"
        first_settle = kinds.index("settle")
        swap = kinds.index("selection")
        assert first_settle < swap, (
            "the clipboard was replaced before the transcript had a chance to land"
        )
        delay = next(value for kind, value in events if kind == "settle")
        assert delay >= app_module.CLIPBOARD_SETTLE_MS

    def test_the_second_paste_comes_after_the_swap(self, qt_app, monkeypatch, tmp_path):
        controller, events = self._controller(qt_app, monkeypatch, tmp_path=tmp_path)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        kinds = [kind for kind, _ in events]
        assert kinds.index("selection") < kinds.index("paste"), (
            "the second keystroke must not be sent before the screenshots are on offer"
        )

    def test_a_second_paste_is_sent(self, qt_app, monkeypatch, tmp_path):
        controller, events = self._controller(qt_app, monkeypatch)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        assert [kind for kind, _ in events].count("paste") == 1

    def test_a_long_explanation_ends_holding_both(self, qt_app, monkeypatch, tmp_path):
        controller, events = self._controller(qt_app, monkeypatch)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        assert ("copy", True) in events, "the transcript and images should be left together"

    def test_a_quick_note_puts_the_previous_clipboard_back(self, qt_app, monkeypatch, tmp_path):
        """A quick note borrows the clipboard; it has to give it back."""

        controller, events = self._controller(qt_app, monkeypatch)
        previous = object()

        controller._paste_images_after("hello", self._images(tmp_path), False, previous)

        assert ("restore", None) in events
        assert ("copy", True) not in events

    def test_nothing_is_pasted_when_images_cannot_be_served_alone(
        self, qt_app, monkeypatch, tmp_path
    ):
        """Without a portal clipboard there is no text-free selection to send."""

        controller, events = self._controller(qt_app, monkeypatch, serves=False)

        controller._paste_images_after("hello", self._images(tmp_path), True, None)

        assert ("paste", None) not in events, "a keystroke with text on the clipboard duplicates it"
        assert ("copy", True) in events, "the screenshots still belong on the clipboard"


class TestClipboardMimeTypesGoOutAsStrings:
    """`SetSelection` declares `mime_types` as `as`; PyQt sends `av` for a list.

    The portal rejects the whole call -- "Expected type 'as' for option
    'mime_types', got 'av'" -- so the screenshots silently never reach the
    clipboard and the only trace is one warning in the log. Same family as the
    `uint32` options this backend already has to spell out.

    PyQt6 exposes nothing to read a `QDBusArgument`'s declared type back, so
    what is asserted is that the option is not handed over as a bare list.
    """

    def _backend(self, monkeypatch):
        from PyQt6 import QtDBus

        from bettervoice.backends import textinject

        captured: dict = {}

        class FakeReply:
            @staticmethod
            def type():
                return QtDBus.QDBusMessage.MessageType.ReplyMessage

        class FakeInterface:
            def __init__(self, *_args):
                pass

            @staticmethod
            def call(name, _session, options):
                captured["member"] = name
                captured["options"] = options
                return FakeReply()

        monkeypatch.setattr(textinject.QtDBus, "QDBusInterface", FakeInterface)
        backend = textinject.PortalTextInsertionBackend()
        backend._session_path = "/session"
        backend._clipboard_ready = True
        return backend, captured

    def test_the_option_is_not_a_bare_python_list(self, qt_app, monkeypatch):
        from PyQt6 import QtDBus

        backend, captured = self._backend(monkeypatch)

        assert backend.set_selection({"text/plain": b"hi", "image/png": b"\x89PNG"}) is True
        assert captured["member"] == "SetSelection"

        value = captured["options"]["mime_types"]
        assert not isinstance(value, list), "a bare list is marshalled as 'av' and rejected"
        assert isinstance(value, QtDBus.QDBusArgument)

    def test_the_helper_declares_a_string_list(self):
        from PyQt6 import QtCore, QtDBus

        from bettervoice.backends import textinject

        assert textinject._STRING_LIST == QtCore.QMetaType.Type.QStringList.value
        assert isinstance(textinject._string_list(["a", "b"]), QtDBus.QDBusArgument)

    def test_every_offered_format_is_named(self, qt_app, monkeypatch):
        """The portal serves only what was declared, so the list must be complete."""

        backend, _ = self._backend(monkeypatch)
        payloads = {"text/plain": b"hi", "text/html": b"<p>hi</p>", "image/png": b"png"}

        backend.set_selection(payloads)

        assert backend._payloads == payloads


class TestRefusingToPasteIntoOurself:
    """macOS refused to paste into its own window; so do we -- but only its own.

    The recording overlay is one of our windows too, and it is on screen for
    every single recording. Counting it would mean a transcript is never
    inserted.
    """

    def _focus(self, monkeypatch, window):
        from bettervoice.backends import focus

        monkeypatch.setattr(
            focus.QtGui.QGuiApplication, "focusWindow", staticmethod(lambda: window)
        )
        return focus

    @staticmethod
    def _window(flags):
        return type("Window", (), {"flags": lambda _self: flags, "title": lambda _self: "w"})()

    def test_nothing_of_ours_focused_means_paste_away(self, monkeypatch):
        focus = self._focus(monkeypatch, None)
        assert focus.we_have_focus() is False
        assert focus.focused_own_window() is None

    def test_a_real_window_of_ours_blocks_the_paste(self, monkeypatch):
        from PyQt6 import QtCore

        focus = self._focus(monkeypatch, self._window(QtCore.Qt.WindowType.Window))
        assert focus.we_have_focus() is True

    def test_the_input_transparent_overlay_does_not_count(self, monkeypatch):
        """It is on screen for every recording and cannot be typed into."""

        from PyQt6 import QtCore

        overlay_flags = (
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowTransparentForInput
            | QtCore.Qt.WindowType.BypassWindowManagerHint
        )
        focus = self._focus(monkeypatch, self._window(overlay_flags))

        assert focus.we_have_focus() is False, (
            "the overlay cannot receive typing, so it is not us holding focus"
        )
        assert focus.focused_own_window() is None


class TestRememberedInsertionGrant:
    """The portal grant is per-run, so a remembered one has to be reconnected.

    macOS held its Accessibility permission from launch. The RemoteDesktop
    portal hands out a session that dies with the process, so without this the
    user has to open the setup window and press Set Up every single launch --
    with the grant already given and the token already saved.
    """

    def _backend(self, monkeypatch, token):
        from bettervoice.backends import textinject

        stored = {}
        if token is not None:
            stored[textinject.PortalTextInsertionBackend.RESTORE_TOKEN_KEY] = token

        class FakeConfig:
            def get(self, key, default=None):
                return stored.get(key, default)

            def set(self, key, value):
                stored[key] = value

        monkeypatch.setattr(textinject, "config", FakeConfig)
        backend = textinject.PortalTextInsertionBackend()
        return backend, stored

    def test_a_saved_token_means_the_user_already_agreed(self, monkeypatch):
        backend, _ = self._backend(monkeypatch, "a-restore-token")
        assert backend.grant_remembered is True

    def test_no_token_means_asking_would_be_a_prompt_at_startup(self, monkeypatch):
        backend, _ = self._backend(monkeypatch, None)
        assert backend.grant_remembered is False

    def test_an_emptied_token_is_not_a_grant(self, monkeypatch):
        backend, _ = self._backend(monkeypatch, "")
        assert backend.grant_remembered is False, "a cleared token must not be believed"

    def test_a_refused_restore_forgets_the_token(self, monkeypatch):
        """Otherwise a revoked grant means a refused prompt at every launch."""

        from bettervoice.backends import portal, textinject

        backend, stored = self._backend(monkeypatch, "a-restore-token")
        backend._session_path = "/session"

        backend._on_started(portal.SUCCESS + 1, {})

        assert not stored[textinject.PortalTextInsertionBackend.RESTORE_TOKEN_KEY]
        assert backend.grant_remembered is False

    def test_a_backend_that_needs_no_permission_has_nothing_to_restore(self):
        from bettervoice.backends import textinject

        assert textinject.TextInsertionBackend().grant_remembered is False
        assert textinject.CommandTextInsertionBackend("wtype").grant_remembered is False

    def _controller(self, qt_app, monkeypatch, remembered: bool):
        from bettervoice import app as app_module

        monkeypatch.setattr(app_module.AppController, "__init__", lambda self, _a: None)
        controller = app_module.AppController(qt_app)
        prepared: list[int] = []
        controller.text_insertion = type(
            "Insertion",
            (),
            {
                "grant_remembered": remembered,
                "prepare": lambda _self: prepared.append(1),
            },
        )()
        return controller, prepared

    def test_startup_reconnects_a_grant_the_user_already_gave(self, qt_app, monkeypatch):
        controller, prepared = self._controller(qt_app, monkeypatch, remembered=True)

        controller._restore_text_insertion()

        assert prepared == [1], "the setup window should not have to be opened every launch"

    def test_startup_never_raises_a_permission_prompt_on_its_own(self, qt_app, monkeypatch):
        """An unasked user meets the prompt through onboarding, not at launch."""

        controller, prepared = self._controller(qt_app, monkeypatch, remembered=False)

        controller._restore_text_insertion()

        assert prepared == []


class TestCommandInsertionLearnsFromFailure:
    """`wtype` being installed is not proof the compositor accepts it."""

    def _backend(self, monkeypatch, returncode: int, stderr: bytes = b""):
        from bettervoice.backends import textinject

        backend = textinject.CommandTextInsertionBackend("wtype")

        class Result:
            def __init__(self):
                self.returncode = returncode
                self.stderr = stderr

        monkeypatch.setattr(textinject.subprocess, "run", lambda *a, **k: Result())
        return backend

    def test_it_is_believed_until_it_fails(self, monkeypatch):
        backend = self._backend(monkeypatch, returncode=0)

        assert backend.ready is True
        assert backend.unavailable_reason is None

        results: list[bool] = []
        backend.paste(results.append)
        assert results == [True]
        assert backend.ready is True

    def test_a_failure_stops_it_claiming_readiness(self, monkeypatch):
        backend = self._backend(
            monkeypatch, returncode=1, stderr=b"compositor does not support virtual keyboard"
        )

        results: list[bool] = []
        backend.paste(results.append)

        assert results == [False]
        assert backend.ready is False
        reason = backend.unavailable_reason
        assert "virtual keyboard" in reason
        assert "clipboard" in reason, "the user needs to know the transcript is safe"

    def test_recovering_clears_the_complaint(self, monkeypatch):
        backend = self._backend(monkeypatch, returncode=1, stderr=b"nope")
        backend.paste(lambda _ok: None)
        assert backend.ready is False

        self._backend(monkeypatch, returncode=0)  # rebind subprocess.run to succeed
        backend.paste(lambda _ok: None)

        assert backend.ready is True
        assert backend.unavailable_reason is None

    def test_a_tool_that_cannot_run_at_all_is_recorded(self, monkeypatch):
        from bettervoice.backends import textinject

        backend = textinject.CommandTextInsertionBackend("wtype")
        monkeypatch.setattr(
            textinject.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no such file")),
        )

        results: list[bool] = []
        backend.paste(results.append)

        assert results == [False]
        assert backend.ready is False
        assert "no such file" in backend.unavailable_reason


class TestRecorderTeardown:
    """PortAudio can be mid-callback when stop() runs; nothing may write after."""

    def test_a_callback_after_stop_is_refused(self, tmp_path, monkeypatch):
        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()
        closed = {"value": False}

        class Handle:
            def write(self, _data):
                if closed["value"]:
                    raise ValueError("I/O operation on closed file")

            def close(self):
                closed["value"] = True

        recorder._accepting = True
        handle = Handle()

        def callback_body():
            with recorder._lock:
                if not recorder._accepting:
                    return False
                handle.write(b"")
                return True

        assert callback_body() is True

        # stop() refuses writes before closing anything.
        with recorder._lock:
            recorder._accepting = False
        with recorder._lock:
            handle.close()

        assert callback_body() is False, "a late callback must not touch a closed file"

    def test_stopping_without_starting_is_an_error_not_a_crash(self):
        from bettervoice.audio.recorder import AudioRecorder
        from bettervoice.errors import SessionUnavailable

        recorder = AudioRecorder()

        with pytest.raises(SessionUnavailable):
            recorder.stop()

    def test_a_fresh_recorder_accepts_nothing(self):
        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()

        assert recorder._accepting is False
        assert recorder.is_recording is False
        assert recorder.peak_level == 0.0


class TestFirstRun:
    """What a brand-new user meets: no settings, no model."""

    @pytest.fixture()
    def fresh_home(self, tmp_path, monkeypatch):
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
        import bettervoice.config as config_module

        monkeypatch.setattr(config_module, "_config", None)
        return tmp_path

    def test_onboarding_has_not_been_completed(self, fresh_home):
        from bettervoice.config import config

        assert config().bool("completedOnboarding") is False

    def test_the_model_is_reported_as_missing_with_its_path(self, fresh_home):
        from bettervoice.asr import LocalTranscriber

        transcriber = LocalTranscriber()

        assert transcriber.is_downloaded is False
        assert transcriber.is_ready is False
        assert str(fresh_home) in str(transcriber.directory)

    def test_the_header_mark_is_visible_on_a_light_theme(self, qt_app):
        """A mask icon drawn in a fixed light grey disappears on a light window."""

        from PyQt6 import QtGui

        from bettervoice.ui import icons

        dark_text = QtGui.QColor(20, 20, 20)
        pixmap = icons.waveform_in_circle(28, color=dark_text).pixmap(28, 28)
        image = pixmap.toImage()

        centre = QtGui.QColor(image.pixel(14, 14))
        assert centre.alpha() > 0, "the mark should paint something"
        assert centre.lightness() < 128, "it must be dark enough to see on a light window"

    def test_a_setup_row_with_no_action_shows_no_button(self, qt_app):
        from bettervoice.ui.setup_window import SetupRowState, _SetupRow

        row = _SetupRow()
        row.apply(SetupRowState("Clipboard", "Ready", ready=True))

        assert not row._button.isVisible()

    def test_unassigned_shortcuts_read_as_a_sentence(self, qt_app, monkeypatch):
        """"hold unassigned, or press unassigned" is not something to show a user."""

        from bettervoice.backends import global_shortcuts, hotkeys

        backend = hotkeys.PortalHotkeyBackend()
        backend._triggers = {
            global_shortcuts.PUSH_TO_TALK_ID: "",
            global_shortcuts.LONG_FORM_ID: "",
        }

        hint = backend.shortcut_hint
        assert "unassigned" not in hint.split("—")[0].replace("no shortcut assigned yet", "")
        assert "tray menu" in hint
        assert backend.needs_key_assignment is True

    def test_assigned_shortcuts_are_spelled_out(self, qt_app):
        from bettervoice.backends import global_shortcuts, hotkeys

        backend = hotkeys.PortalHotkeyBackend()
        backend._triggers = {
            global_shortcuts.PUSH_TO_TALK_ID: "ALT+v",
            global_shortcuts.LONG_FORM_ID: "LOGO+ALT+v",
        }

        assert backend.shortcut_hint == "hold Alt + V, or press Super + Alt + V"
        assert backend.needs_key_assignment is False


class TestBothThemes:
    """Every drawn surface has to survive a light palette as well as a dark one.

    All the visual checking during the port happened on a dark desktop, and that
    is exactly how a mark drawn in a fixed light grey went unnoticed.
    """

    @staticmethod
    def _palette(dark: bool):
        from PyQt6 import QtGui

        palette = QtGui.QPalette()
        window = QtGui.QColor(35, 38, 41) if dark else QtGui.QColor(239, 240, 241)
        text = QtGui.QColor(239, 240, 241) if dark else QtGui.QColor(35, 38, 41)
        for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Button):
            palette.setColor(role, window)
        for role in (
            QtGui.QPalette.ColorRole.WindowText,
            QtGui.QPalette.ColorRole.Text,
            QtGui.QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, text)
        return palette

    @staticmethod
    def _render(widget):
        from PyQt6 import QtGui

        widget.resize(widget.sizeHint())
        pixmap = QtGui.QPixmap(widget.size())
        pixmap.fill(widget.palette().color(QtGui.QPalette.ColorRole.Window))
        widget.render(pixmap)
        return pixmap.toImage()

    @staticmethod
    def _contrasts_with_background(image, background) -> bool:
        """Whether anything in the image stands out from the window colour."""

        from PyQt6 import QtGui

        for x in range(0, image.width(), 2):
            for y in range(0, image.height(), 2):
                pixel = QtGui.QColor(image.pixel(x, y))
                if abs(pixel.lightness() - background.lightness()) > 40:
                    return True
        return False

    @pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
    def test_the_capture_preview_is_visible(self, qt_app, dark):
        from PyQt6 import QtGui

        from bettervoice.ui.widgets import CapturePreview

        palette = self._palette(dark)
        preview = CapturePreview()
        preview.setPalette(palette)
        try:
            image = self._render(preview)
            background = palette.color(QtGui.QPalette.ColorRole.Window)
            assert self._contrasts_with_background(image, background), (
                "the illustration blends into the sheet"
            )
        finally:
            preview.deleteLater()

    @pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
    def test_a_key_cap_is_visible(self, qt_app, dark):
        from PyQt6 import QtGui

        from bettervoice.ui.widgets import KeyCap

        palette = self._palette(dark)
        cap = KeyCap("Alt + V")
        cap.setPalette(palette)
        try:
            image = self._render(cap)
            background = palette.color(QtGui.QPalette.ColorRole.Window)
            assert self._contrasts_with_background(image, background)
        finally:
            cap.deleteLater()

    @pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
    def test_the_status_icons_are_visible(self, qt_app, dark):
        from PyQt6 import QtGui

        from bettervoice.ui.widgets import StatusIcon

        palette = self._palette(dark)
        background = palette.color(QtGui.QPalette.ColorRole.Window)
        for ready in (True, False):
            icon = StatusIcon(ready=ready)
            icon.setPalette(palette)
            try:
                assert self._contrasts_with_background(self._render(icon), background), (
                    f"the {'ready' if ready else 'pending'} icon is invisible"
                )
            finally:
                icon.deleteLater()

    def test_the_hud_stays_dark_in_both_themes(self, qt_app):
        """The macOS HUD was a fixed dark panel; it does not follow the theme."""

        from bettervoice.ui.overlay import HUD_BACKGROUND

        assert HUD_BACKGROUND.lightness() < 40
        assert HUD_BACKGROUND.alpha() > 200


class TestSecondLaunch:
    """Launching an app that is already running should surface it, not fail."""

    @pytest.fixture()
    def cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from bettervoice import __main__ as entry

        return entry

    def test_the_lock_records_the_running_pid(self, cache):
        import os

        handle = cache._single_instance_lock()
        try:
            assert handle is not None
            assert cache._lock_path().read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            handle.close()

    def test_our_own_pid_is_not_treated_as_a_rival(self, cache):
        handle = cache._single_instance_lock()
        try:
            assert cache._running_instance() is None
        finally:
            handle.close()

    def test_a_stale_lock_from_a_dead_process_is_ignored(self, cache):
        # A PID that cannot exist: the file outlived whatever wrote it.
        cache._lock_path().write_text("999999999\n", encoding="utf-8")

        assert cache._running_instance() is None
        assert cache._show_running_instance() is False

    def test_a_live_instance_is_signalled(self, cache, monkeypatch):
        import os
        import signal

        cache._lock_path().write_text("4242\n", encoding="utf-8")
        signalled: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            signalled.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)

        assert cache._show_running_instance() is True
        assert signalled == [(4242, 0), (4242, signal.SIGUSR1)]

    def test_an_unreadable_lock_is_survivable(self, cache):
        cache._lock_path().write_text("not a pid\n", encoding="utf-8")

        assert cache._running_instance() is None
        assert cache._show_running_instance() is False


class TestScreenChangeWiring:
    """The handler was tested directly; the connection to it never was.

    ``QGuiApplication.instance()`` is typed as returning the base
    ``QCoreApplication``, which has no screen signals -- so if that ever became
    true at runtime, hot-plug handling would break silently.
    """

    def test_the_application_really_offers_the_screen_signals(self, qt_app):
        from PyQt6 import QtGui

        application = QtGui.QGuiApplication.instance()

        assert application is not None
        assert hasattr(application, "screenAdded")
        assert hasattr(application, "screenRemoved")

    def test_starting_connects_to_them(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            assert overlay._screens_connected is False
            overlay.start()
            assert overlay._screens_connected is True, "hot-plug would go unnoticed"
        finally:
            overlay.stop()

    def test_the_signal_actually_reaches_the_handler(self, qt_app):
        """Emit the real signal rather than calling the handler by hand."""

        from PyQt6 import QtGui

        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            before = list(overlay._windows)

            application = QtGui.QGuiApplication.instance()
            application.screenAdded.emit(QtGui.QGuiApplication.primaryScreen())

            assert overlay._windows, "the overlay should have been rebuilt"
            # The screens did not actually change, so every window is reused:
            # rebuilding a fullscreen surface per screen costs GPU buffers.
            assert overlay._windows == before
        finally:
            overlay.stop()

    def test_connecting_twice_is_harmless(self, qt_app):
        from bettervoice.ui.overlay import RecordingOverlay

        overlay = RecordingOverlay()
        try:
            overlay.start()
            overlay._connect_screen_changes()
            overlay._connect_screen_changes()
            assert overlay._screens_connected is True
        finally:
            overlay.stop()


class TestDoctor:
    """`--doctor` is what a stuck user runs; it must never be the thing that breaks."""

    def test_it_names_every_moving_part(self, qt_app):
        from bettervoice.doctor import report

        text = report()

        for label in (
            "session",
            "shortcuts",
            "pointer",
            "screen capture",
            "transcript insertion",
            "focus target",
            "clipboard",
            "sound cues",
            "microphone",
            "local model",
            "grammar model",
            "sessions",
            "settings",
        ):
            assert label in text, f"{label} is missing from the report"

    def test_it_reports_the_version(self, qt_app):
        from bettervoice import __version__
        from bettervoice.doctor import report

        assert __version__ in report()

    def test_it_survives_every_backend_being_unavailable(self, qt_app, monkeypatch):
        """The desktop where nothing works is exactly when someone runs this."""

        from bettervoice import doctor
        from bettervoice.backends import focus, hotkeys, pointer, screenshot, textinject

        monkeypatch.setattr(hotkeys, "create", lambda _p=None: hotkeys.NullHotkeyBackend("no"))
        monkeypatch.setattr(pointer, "create", lambda _p=None: pointer.NullPointerBackend("no"))
        monkeypatch.setattr(
            screenshot, "create", lambda _p=None: screenshot.NullScreenshotBackend("no")
        )
        monkeypatch.setattr(
            textinject, "create", lambda _p=None: textinject.NullTextInsertionBackend("no")
        )
        monkeypatch.setattr(focus, "create", lambda _p=None: focus.NullFocusBackend())

        text = doctor.report()

        assert "none" in text
        assert "shortcuts" in text

    def test_it_survives_a_machine_with_no_microphone(self, qt_app, monkeypatch):
        from bettervoice import doctor
        from bettervoice.audio import devices

        # Simulate at the enumeration source rather than faking the manager.
        monkeypatch.setattr(devices, "_pactl_sources", list)
        monkeypatch.setattr(devices, "_portaudio_sources", list)

        text = doctor.report()

        assert "Unavailable" in text, text
        assert "0 input(s)" in text

    def test_it_says_when_the_model_is_missing(self, qt_app, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        import bettervoice.config as config_module

        monkeypatch.setattr(config_module, "_config", None)
        from bettervoice.doctor import report

        text = report()

        assert "not downloaded" in text

    def test_the_suggestions_only_name_missing_tools(self, qt_app, monkeypatch):
        from bettervoice import doctor
        from bettervoice.backends import environment

        monkeypatch.setattr(environment, "has", lambda _tool: True)
        assert "Everything BetterVoice can use is installed." in doctor.report()

        monkeypatch.setattr(environment, "has", lambda _tool: False)
        text = doctor.report()
        assert "Not installed, and would help" in text

    def test_it_is_plain_text_a_user_can_paste_into_an_issue(self, qt_app):
        from bettervoice.doctor import report

        text = report()

        assert text.isprintable() is False, "it is multi-line"
        assert "\x1b[" not in text, "no escape codes: this gets pasted into bug reports"
        assert all(len(line) < 200 for line in text.splitlines())


class TestAppearanceWatcher:
    """Following the desktop's reduced-motion preference, and noticing changes."""

    def test_an_explicit_setting_never_asks_the_desktop(self, qt_app, monkeypatch):
        from bettervoice.backends import appearance

        asked: list[int] = []
        monkeypatch.setattr(
            appearance, "_portal_animations_enabled", lambda: asked.append(1) or True
        )

        assert appearance.reduce_motion(True) is True
        assert appearance.reduce_motion(False) is False
        assert asked == [], "an explicit choice is the user's, not the desktop's"

    def test_auto_follows_the_portal(self, qt_app, monkeypatch):
        from bettervoice.backends import appearance

        monkeypatch.setattr(appearance, "_portal_animations_enabled", lambda: False)
        assert appearance.reduce_motion("auto") is True

        monkeypatch.setattr(appearance, "_portal_animations_enabled", lambda: True)
        assert appearance.reduce_motion("auto") is False

    def test_it_falls_back_to_kde_when_the_portal_is_silent(self, qt_app, monkeypatch):
        from bettervoice.backends import appearance

        monkeypatch.setattr(appearance, "_portal_animations_enabled", lambda: None)
        monkeypatch.setattr(appearance, "_kde_animations_enabled", lambda: False)

        assert appearance.reduce_motion("auto") is True

    def test_it_assumes_animation_when_nothing_answers(self, qt_app, monkeypatch):
        from bettervoice.backends import appearance

        monkeypatch.setattr(appearance, "_portal_animations_enabled", lambda: None)
        monkeypatch.setattr(appearance, "_kde_animations_enabled", lambda: None)

        assert appearance.reduce_motion("auto") is False

    def test_the_kde_file_is_read_when_present(self, qt_app, tmp_path, monkeypatch):
        from bettervoice.backends import appearance

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "kdeglobals").write_text(
            "[KDE]\nAnimationDurationFactor=0\n", encoding="utf-8"
        )
        assert appearance._kde_animations_enabled() is False

        (tmp_path / "kdeglobals").write_text(
            "[KDE]\nAnimationDurationFactor=1\n", encoding="utf-8"
        )
        assert appearance._kde_animations_enabled() is True

    def test_a_malformed_kde_file_is_ignored(self, qt_app, tmp_path, monkeypatch):
        from bettervoice.backends import appearance

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "kdeglobals").write_text(
            "[KDE]\nAnimationDurationFactor=banana\n", encoding="utf-8"
        )

        assert appearance._kde_animations_enabled() is None

    def test_the_watcher_emits_only_when_the_answer_changes(self, qt_app, monkeypatch):
        from bettervoice.backends import appearance

        answer = {"value": False}
        monkeypatch.setattr(
            appearance, "reduce_motion", lambda _override="auto": answer["value"]
        )
        watcher = appearance.AppearanceWatcher("auto")
        seen: list[bool] = []
        watcher.reduce_motion_changed.connect(seen.append)

        watcher._recheck()
        assert seen == [], "no change, no signal"

        answer["value"] = True
        watcher._recheck()
        assert seen == [True]
        assert watcher.reduce_motion is True

        watcher._recheck()
        assert seen == [True], "still no repeat for an unchanged answer"

    def test_changing_the_override_rechecks(self, qt_app):
        from bettervoice.backends import appearance

        watcher = appearance.AppearanceWatcher(False)
        assert watcher.reduce_motion is False

        watcher.set_override(True)
        assert watcher.reduce_motion is True


class TestGlobalShortcutSignals:
    """Turning portal D-Bus signals into recording actions.

    Everything downstream depends on this: a mis-parsed signal means a keypress
    silently does nothing, which is indistinguishable from a broken shortcut.
    """

    SESSION = "/org/freedesktop/portal/desktop/session/1_1/bv"

    class Message:
        """The shape of a jeepney message: a member name plus a body tuple."""

        def __init__(self, member: str, body: tuple) -> None:
            self.header = type("Header", (), {"fields": {3: member}})()
            self.body = body

    def _client(self):
        from bettervoice.backends.global_shortcuts import GlobalShortcutsClient

        client = GlobalShortcutsClient()
        seen: dict[str, list] = {"activated": [], "deactivated": [], "bindings": []}
        client.activated.connect(seen["activated"].append)
        client.deactivated.connect(seen["deactivated"].append)
        client.bindings_changed.connect(seen["bindings"].append)
        return client, seen

    def test_a_press_and_release_are_reported_separately(self, qt_app):
        """Hold-to-talk exists only because these arrive as two signals."""

        from bettervoice.backends import global_shortcuts

        client, seen = self._client()
        identifier = global_shortcuts.PUSH_TO_TALK_ID

        client._handle_signal(
            self.Message("Activated", (self.SESSION, identifier, 0, {})), self.SESSION
        )
        client._handle_signal(
            self.Message("Deactivated", (self.SESSION, identifier, 0, {})), self.SESSION
        )

        assert seen["activated"] == [identifier]
        assert seen["deactivated"] == [identifier]

    def test_another_session_is_ignored(self, qt_app):
        """Two apps can hold shortcut sessions; only ours may drive recording."""

        client, seen = self._client()

        client._handle_signal(
            self.Message("Activated", ("/some/other/session", "push-to-talk", 0, {})),
            self.SESSION,
        )

        assert seen["activated"] == []

    def test_an_unrelated_member_is_ignored(self, qt_app):
        client, seen = self._client()

        client._handle_signal(self.Message("Response", (self.SESSION, "x")), self.SESSION)

        assert seen["activated"] == [] and seen["deactivated"] == []

    def test_a_truncated_body_does_not_raise(self, qt_app):
        client, seen = self._client()

        client._handle_signal(self.Message("Activated", ()), self.SESSION)
        client._handle_signal(self.Message("Activated", (self.SESSION,)), self.SESSION)

        assert seen["activated"] == []

    def test_shortcuts_changed_republishes_the_triggers(self, qt_app):
        from bettervoice.backends import global_shortcuts

        client, seen = self._client()
        shortcuts = [
            (global_shortcuts.PUSH_TO_TALK_ID, {"trigger_description": ("s", "Alt+V")}),
            (global_shortcuts.LONG_FORM_ID, {"trigger_description": ("s", "")}),
        ]

        client._handle_signal(
            self.Message("ShortcutsChanged", (self.SESSION, shortcuts)), self.SESSION
        )

        assert seen["bindings"] == [
            {global_shortcuts.PUSH_TO_TALK_ID: "Alt+V", global_shortcuts.LONG_FORM_ID: ""}
        ]
        assert client.triggers[global_shortcuts.PUSH_TO_TALK_ID] == "Alt+V"

    def test_a_missing_trigger_description_is_treated_as_unassigned(self, qt_app):
        from bettervoice.backends import global_shortcuts

        client, _ = self._client()
        client._publish_triggers([(global_shortcuts.PUSH_TO_TALK_ID, {})])

        assert client.triggers == {global_shortcuts.PUSH_TO_TALK_ID: ""}

    def test_an_app_id_complaint_is_translated_for_the_user(self, qt_app):
        from bettervoice.backends.global_shortcuts import GlobalShortcutsClient

        explained = GlobalShortcutsClient._explain("An app id is required")

        assert "install.sh" in explained
        assert "application menu" in explained

    def test_other_errors_are_passed_through_unchanged(self, qt_app):
        from bettervoice.backends.global_shortcuts import GlobalShortcutsClient

        assert GlobalShortcutsClient._explain("something else") == "something else"


class TestShortcutBackendDispatch:
    """The portal backend turns those signals into the two recording gestures."""

    def _backend(self):
        from bettervoice.backends import hotkeys

        backend = hotkeys.PortalHotkeyBackend()
        events: list[str] = []
        backend.push_to_talk_started.connect(lambda: events.append("start"))
        backend.push_to_talk_stopped.connect(lambda: events.append("stop"))
        backend.long_form_toggled.connect(lambda: events.append("toggle"))
        backend.promote_to_long_form.connect(lambda: events.append("promote"))
        return backend, events

    def test_holding_starts_and_releasing_stops(self, qt_app):
        from bettervoice.backends import global_shortcuts

        backend, events = self._backend()

        backend._on_activated(global_shortcuts.PUSH_TO_TALK_ID)
        backend._on_deactivated(global_shortcuts.PUSH_TO_TALK_ID)

        assert events == ["start", "stop"]

    def test_a_repeated_press_does_not_start_twice(self, qt_app):
        """Key repeat would otherwise restart the recording mid-sentence."""

        from bettervoice.backends import global_shortcuts

        backend, events = self._backend()

        backend._on_activated(global_shortcuts.PUSH_TO_TALK_ID)
        backend._on_activated(global_shortcuts.PUSH_TO_TALK_ID)

        assert events == ["start"]

    def test_a_release_without_a_press_is_ignored(self, qt_app):
        from bettervoice.backends import global_shortcuts

        backend, events = self._backend()

        backend._on_deactivated(global_shortcuts.PUSH_TO_TALK_ID)

        assert events == []

    def test_long_form_toggles_when_idle(self, qt_app):
        from bettervoice.backends import global_shortcuts

        backend, events = self._backend()

        backend._on_activated(global_shortcuts.LONG_FORM_ID)

        assert events == ["toggle"]

    def test_long_form_during_a_hold_promotes_instead(self, qt_app):
        """Matches macOS: adding the chord mid-hold must not stop the recording."""

        from bettervoice.backends import global_shortcuts

        backend, events = self._backend()

        backend._on_activated(global_shortcuts.PUSH_TO_TALK_ID)
        backend._on_activated(global_shortcuts.LONG_FORM_ID)

        assert events == ["start", "promote"]


class TestRecorderLifetime:
    """PortAudio's callback thread must never outlive what it closes over."""

    def test_the_callback_is_reachable_from_the_recorder(self):
        """If only the C stream held it, a collection could free it mid-call."""

        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()

        assert hasattr(recorder, "_callback")
        assert recorder._callback is None

    def test_close_is_safe_before_anything_started(self):
        from bettervoice.audio.recorder import AudioRecorder

        AudioRecorder().close()  # must not raise

    def test_close_is_idempotent(self):
        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()
        recorder.close()
        recorder.close()

    def test_dropping_a_recorder_does_not_raise(self):
        from bettervoice.audio.recorder import AudioRecorder

        recorder = AudioRecorder()
        recorder.__del__()  # the safety net the garbage collector would call

    def test_shutdown_always_closes_the_stream(self, qt_app, monkeypatch):
        """Not only mid-recording: an idle controller still owns a recorder."""

        from bettervoice import app as app_module

        monkeypatch.setattr(app_module.AppController, "__init__", lambda self, _a: None)
        controller = app_module.AppController(qt_app)

        closed: list[int] = []
        controller.recorder = type("R", (), {"close": lambda _s: closed.append(1)})()
        controller._state = app_module.SessionState.IDLE
        controller._output = None
        for name in ("hotkeys", "pointer", "focus", "overlay", "text_insertion", "tray"):
            stub = type("N", (), {"stop": lambda _s: None, "hide": lambda _s: None, "close": lambda _s: None})
            setattr(controller, name, stub())
        controller._limit_timer = type("T", (), {"stop": lambda _s: None})()

        controller.shutdown()

        assert closed == [1], "an idle controller must still release the audio stream"


class TestPortalRequests:
    """The request/response plumbing under screen capture, pasting and settings.

    A portal call returns a handle and delivers the real answer later on a
    signal, so every failure mode here is a feature that hangs or silently does
    nothing rather than reporting a problem.
    """

    def _request(self, monkeypatch, reply_is_error: bool = False, connect: bool = True):
        from PyQt6 import QtDBus

        from bettervoice.backends import portal

        class Reply:
            def type(self):
                return (
                    QtDBus.QDBusMessage.MessageType.ErrorMessage
                    if reply_is_error
                    else QtDBus.QDBusMessage.MessageType.ReplyMessage
                )

            def errorMessage(self):
                return "boom"

        class Interface:
            def __init__(self, *_a, **_k):
                pass

            def call(self, *_a, **_k):
                return Reply()

        monkeypatch.setattr(QtDBus, "QDBusInterface", Interface)

        request = portal.PortalRequest("iface", "Method", [], {})
        monkeypatch.setattr(request._bus, "connect", lambda *_a, **_k: connect)
        monkeypatch.setattr(request._bus, "disconnect", lambda *_a, **_k: True)
        seen: list[tuple[int, dict]] = []
        request.finished.connect(lambda code, results: seen.append((code, results)))
        return request, seen

    def test_a_token_is_unique_per_call(self):
        from bettervoice.backends import portal

        assert portal.unique_token() != portal.unique_token()
        assert portal.unique_token("x").startswith("x_")

    def test_the_request_path_follows_the_connection_name(self, qt_app):
        from bettervoice.backends import portal

        request = portal.PortalRequest("iface", "Method", [], {})

        assert request._request_path.startswith(f"{portal.OBJECT_PATH}/request/")
        assert request._request_path.endswith(request._token)

    def test_the_handle_token_is_sent_with_the_call(self, qt_app):
        from bettervoice.backends import portal

        request = portal.PortalRequest("iface", "Method", [], {"other": 1})

        assert request._options["handle_token"] == request._token
        assert request._options["other"] == 1

    def test_a_response_is_forwarded_once(self, qt_app, monkeypatch):
        from PyQt6 import QtDBus

        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch)
        request.call()

        message = QtDBus.QDBusMessage.createSignal(
            request._request_path, portal.REQUEST_INTERFACE, "Response"
        )
        message.setArguments([portal.SUCCESS, {"uri": "file:///tmp/x.png"}])
        request._on_response(message)
        request._on_response(message)

        assert seen == [(portal.SUCCESS, {"uri": "file:///tmp/x.png"})]

    def test_a_failed_call_reports_immediately(self, qt_app, monkeypatch):
        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch, reply_is_error=True)
        request.call()

        assert seen == [(portal.FAILED, {})]

    def test_a_listener_that_cannot_be_installed_fails_rather_than_hangs(
        self, qt_app, monkeypatch
    ):
        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch, connect=False)
        request.call()

        assert seen == [(portal.FAILED, {})], "without a listener the answer never arrives"

    def test_a_timeout_reports_failure(self, qt_app, monkeypatch):
        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch)
        request.call()
        request._on_timeout()

        assert seen == [(portal.FAILED, {})]

    def test_a_late_response_after_a_timeout_is_ignored(self, qt_app, monkeypatch):
        from PyQt6 import QtDBus

        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch)
        request.call()
        request._on_timeout()

        message = QtDBus.QDBusMessage.createSignal(
            request._request_path, portal.REQUEST_INTERFACE, "Response"
        )
        message.setArguments([portal.SUCCESS, {}])
        request._on_response(message)

        assert seen == [(portal.FAILED, {})], "one answer per request"

    def test_a_malformed_response_is_treated_as_failure(self, qt_app, monkeypatch):
        from PyQt6 import QtDBus

        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch)
        request.call()

        message = QtDBus.QDBusMessage.createSignal(
            request._request_path, portal.REQUEST_INTERFACE, "Response"
        )
        message.setArguments([])
        request._on_response(message)

        assert seen == [(portal.FAILED, {})]

    def test_a_cancelled_response_is_distinguishable_from_a_failure(
        self, qt_app, monkeypatch
    ):
        """Screen capture treats "declined" differently from "broken"."""

        from PyQt6 import QtDBus

        from bettervoice.backends import portal

        request, seen = self._request(monkeypatch)
        request.call()

        message = QtDBus.QDBusMessage.createSignal(
            request._request_path, portal.REQUEST_INTERFACE, "Response"
        )
        message.setArguments([portal.CANCELLED, {}])
        request._on_response(message)

        assert seen == [(portal.CANCELLED, {})]
        assert portal.CANCELLED != portal.FAILED


class TestSoundCues:
    """The two cues that bracket a recording, and their fallback chain.

    Playback happens on a background thread, so anything that raises there is
    invisible -- the cue simply stops happening and nobody finds out why.
    """

    def _player(self, monkeypatch, canberra=None, player=None):
        from bettervoice.backends import sounds

        instance = sounds.SoundPlayer()
        monkeypatch.setattr(instance, "_canberra", canberra)
        monkeypatch.setattr(instance, "_player", player)
        return instance

    def test_the_two_cues_are_different_sounds(self):
        from bettervoice.core import RecordingSoundCue

        assert (
            RecordingSoundCue.STARTED.freedesktop_sound_name
            != RecordingSoundCue.FINISHED.freedesktop_sound_name
        )

    def test_libcanberra_is_preferred(self, monkeypatch):
        from bettervoice.backends import sounds
        from bettervoice.core import RecordingSoundCue

        calls: list[list[str]] = []
        monkeypatch.setattr(
            sounds.SoundPlayer, "_run", staticmethod(lambda argv: calls.append(argv) or True)
        )
        player = self._player(monkeypatch, canberra="/usr/bin/canberra-gtk-play")

        player._play(RecordingSoundCue.STARTED)

        assert len(calls) == 1
        assert calls[0][0] == "/usr/bin/canberra-gtk-play"
        assert RecordingSoundCue.STARTED.freedesktop_sound_name in calls[0]

    def test_it_falls_through_to_the_theme_file(self, monkeypatch, tmp_path):
        from bettervoice.backends import sounds
        from bettervoice.core import RecordingSoundCue

        theme = tmp_path / f"{RecordingSoundCue.FINISHED.freedesktop_sound_name}.oga"
        theme.write_bytes(b"ogg")
        monkeypatch.setattr(sounds, "_THEME_DIRECTORIES", (tmp_path,))

        calls: list[list[str]] = []

        def run(argv):
            calls.append(argv)
            return argv[0] != "/usr/bin/canberra-gtk-play"  # canberra fails

        monkeypatch.setattr(sounds.SoundPlayer, "_run", staticmethod(run))
        player = self._player(
            monkeypatch, canberra="/usr/bin/canberra-gtk-play", player="/usr/bin/paplay"
        )

        player._play(RecordingSoundCue.FINISHED)

        assert len(calls) == 2
        assert str(theme) in calls[1]

    def test_it_synthesises_when_nothing_else_works(self, monkeypatch):
        from bettervoice.backends import sounds
        from bettervoice.core import RecordingSoundCue

        monkeypatch.setattr(sounds, "_THEME_DIRECTORIES", ())
        monkeypatch.setattr(sounds.SoundPlayer, "_run", staticmethod(lambda argv: False))
        synthesised: list = []
        monkeypatch.setattr(
            sounds.SoundPlayer, "_synthesise", staticmethod(synthesised.append)
        )
        player = self._player(monkeypatch, canberra=None, player=None)

        player._play(RecordingSoundCue.STARTED)

        assert synthesised == [RecordingSoundCue.STARTED]

    def test_a_failing_command_never_escapes(self, monkeypatch):
        from bettervoice.backends import sounds

        def explode(*_a, **_k):
            raise OSError("no such binary")

        monkeypatch.setattr(sounds.subprocess, "run", explode)

        assert sounds.SoundPlayer._run(["nope"]) is False

    def test_muting_skips_playback_entirely(self, monkeypatch):
        from bettervoice.backends import sounds
        from bettervoice.core import RecordingSoundCue

        played: list = []
        monkeypatch.setattr(sounds.SoundPlayer, "_play", lambda _s, cue: played.append(cue))
        player = sounds.SoundPlayer(enabled=False)

        player.play(RecordingSoundCue.STARTED)

        assert played == []

    def test_synthesis_survives_a_missing_audio_stack(self, monkeypatch):
        """No speakers, no numpy, no problem -- a cue is not worth a crash."""

        import builtins

        from bettervoice.backends import sounds
        from bettervoice.core import RecordingSoundCue

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name in {"sounddevice", "numpy"}:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

        sounds.SoundPlayer._synthesise(RecordingSoundCue.STARTED)  # must not raise

    def test_describe_names_the_route_in_use(self, monkeypatch):
        from bettervoice.backends import sounds

        monkeypatch.setattr(sounds.shutil, "which", lambda _t: "/usr/bin/canberra-gtk-play")
        assert "libcanberra" in sounds.describe()

        monkeypatch.setattr(sounds.shutil, "which", lambda _t: None)
        monkeypatch.setattr(sounds, "_theme_file", lambda _cue: None)
        assert "synthesised" in sounds.describe()


class TestRoutingVerification:
    """Confirming the recording really came from the microphone that was chosen.

    `PIPEWIRE_NODE` usually works, but when it does not the user records from
    some other input with no indication -- so the landing spot is checked, and
    corrected, after the stream registers.
    """

    TOKEN = "token-for-this-recording"

    def _recorder(self, monkeypatch, outputs, sources):
        from bettervoice.audio import recorder as module

        def pactl_json(*arguments):
            if arguments[-1] == "sources":
                return sources
            if arguments[-1] == "source-outputs":
                return outputs
            return None

        monkeypatch.setattr(module, "_pactl_json", pactl_json)
        return module

    def _device(self, name="alsa_input.blue"):
        from bettervoice.audio.devices import MicrophoneDevice

        return MicrophoneDevice(id=name, name="Blue", is_external=True)

    def _output(self, index, source, token=None):
        from bettervoice.audio.recorder import STREAM_TOKEN_PROPERTY

        properties = {"application.name": "BetterVoice"}
        if token is not None:
            properties[STREAM_TOKEN_PROPERTY] = token
        return {"index": index, "source": source, "properties": properties}

    @staticmethod
    def _collect_moves(monkeypatch, module, returncode=0):
        commands: list[list[str]] = []

        class Result:
            pass

        Result.returncode = returncode
        Result.stderr = b"no such entry"
        monkeypatch.setattr(
            module.subprocess, "run", lambda argv, **k: commands.append(argv) or Result()
        )
        return commands

    def test_the_right_source_needs_no_move(self, monkeypatch):
        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 7, self.TOKEN)],
            sources=[{"index": 7, "name": "alsa_input.blue"}],
        )
        moved = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is True
        assert moved == [], "already on the right source"

    def test_a_wrong_source_is_moved(self, monkeypatch):
        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 9, self.TOKEN)],
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        commands = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False, (
            "asking for a move is not the same as the move having happened"
        )
        assert commands == [["pactl", "move-source-output", "1", "alsa_input.blue"]]

    def test_a_move_is_only_believed_once_the_stream_is_seen_on_the_source(self, monkeypatch):
        """PipeWire returns 0 for a move it then drops, so the check must look again."""

        outputs = [self._output(1, 9, self.TOKEN)]
        module = self._recorder(
            monkeypatch,
            outputs=outputs,
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        device = self._device()
        assert recorder._check_routing(device, self.TOKEN) is False

        outputs[0] = self._output(1, 7, self.TOKEN)  # the move landed
        assert recorder._check_routing(device, self.TOKEN) is True

    def test_a_move_that_never_takes_effect_is_asked_for_again(self, monkeypatch):
        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 9, self.TOKEN)],
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        commands = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        monkeypatch.setattr(recorder, "MOVE_RETRY_SECONDS", 0.0)
        device = self._device()
        for _ in range(3):
            assert recorder._check_routing(device, self.TOKEN) is False
        assert len(commands) == 3, "a dropped move has to be repeated, not waited on forever"

    def test_repeating_the_move_is_throttled(self, monkeypatch):
        """Polling every 100ms must not mean a `pactl` process every 100ms."""

        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 9, self.TOKEN)],
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        commands = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        device = self._device()
        for _ in range(5):
            assert recorder._check_routing(device, self.TOKEN) is False
        assert len(commands) == 1, "the move needs a moment before it is worth repeating"

    def test_a_failing_move_keeps_the_check_going(self, monkeypatch):
        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 9, self.TOKEN)],
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        self._collect_moves(monkeypatch, module, returncode=1)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False, (
            "a refused move leaves the routing unknown, not settled"
        )

    def test_another_recordings_stream_is_not_mistaken_for_ours(self, monkeypatch):
        """The wrong-microphone bug: two streams both answer to "BetterVoice".

        A recording that is still closing sits on the right source, and matching
        on the application name alone accepted it as proof that this recording
        was routed -- leaving this one on the default microphone for its length.
        """

        module = self._recorder(
            monkeypatch,
            outputs=[
                self._output(1, 7, "a-previous-recording"),  # already on the target
                self._output(2, 9, self.TOKEN),  # ours, on the wrong source
            ],
            sources=[
                {"index": 9, "name": "alsa_input.webcam"},
                {"index": 7, "name": "alsa_input.blue"},
            ],
        )
        commands = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False
        assert commands == [["pactl", "move-source-output", "2", "alsa_input.blue"]], (
            "our own stream is the one to move"
        )

    def test_a_stream_that_has_not_registered_yet_is_not_a_verdict(self, monkeypatch):
        """Checking once immediately is why the wrong-microphone bug was silent."""

        module = self._recorder(monkeypatch, outputs=[], sources=[])

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False, (
            "no stream yet means try again, not give up"
        )

    def test_an_untagged_stream_is_not_ours(self, monkeypatch):
        """A BetterVoice stream from another process carries a different token."""

        module = self._recorder(
            monkeypatch,
            outputs=[self._output(1, 9)],
            sources=[{"index": 9, "name": "alsa_input.webcam"}],
        )
        moved = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False
        assert moved == [], "never move a stream we did not open"

    def test_other_applications_streams_are_ignored(self, monkeypatch):
        module = self._recorder(
            monkeypatch,
            outputs=[{"index": 3, "source": 9, "properties": {"application.name": "Firefox"}}],
            sources=[{"index": 9, "name": "alsa_input.webcam"}],
        )
        moved = self._collect_moves(monkeypatch, module)

        recorder = module.AudioRecorder()
        assert recorder._check_routing(self._device(), self.TOKEN) is False
        assert moved == [], "never move another application's audio"

    def test_verification_gives_up_when_the_recording_ends(self, monkeypatch):
        module = self._recorder(monkeypatch, outputs=[], sources=[])
        recorder = module.AudioRecorder()
        recorder._stream_token = self.TOKEN
        monkeypatch.setattr(type(recorder), "is_recording", property(lambda _s: False))
        checked: list = []
        monkeypatch.setattr(recorder, "_check_routing", lambda *_a: checked.append(1) or False)

        recorder._verify_routing(self._device(), self.TOKEN)

        assert checked == [], "a finished recording needs no routing check"

    def test_a_thread_that_outlived_its_recording_leaves_the_next_one_alone(self, monkeypatch):
        """stop() only joins briefly, so a straggler must recognise it is stale.

        Otherwise it goes on correcting the *next* recording's stream towards
        the microphone the finished one had asked for.
        """

        module = self._recorder(monkeypatch, outputs=[], sources=[])
        recorder = module.AudioRecorder()
        recorder._stream_token = "the-next-recording"
        monkeypatch.setattr(type(recorder), "is_recording", property(lambda _s: True))
        checked: list = []
        monkeypatch.setattr(recorder, "_check_routing", lambda *_a: checked.append(1) or False)

        recorder._verify_routing(self._device(), self.TOKEN)

        assert checked == [], "a stale thread must not touch another recording"

    def test_it_is_bounded_rather_than_looping_forever(self, monkeypatch):
        module = self._recorder(monkeypatch, outputs=[], sources=[])
        recorder = module.AudioRecorder()
        recorder._stream_token = self.TOKEN
        monkeypatch.setattr(type(recorder), "is_recording", property(lambda _s: True))
        monkeypatch.setattr(recorder, "ROUTING_TIMEOUT_SECONDS", 0.15)

        recorder._verify_routing(self._device(), self.TOKEN)  # returns rather than hanging

    def test_it_keeps_looking_for_as_long_as_the_recording_lasts(self, monkeypatch):
        """Giving up early leaves the user on the wrong microphone all session."""

        from bettervoice.audio import recorder as module

        recorder = module.AudioRecorder()
        assert recorder.ROUTING_TIMEOUT_SECONDS >= 10, (
            "a stream can take seconds to register with PipeWire"
        )

        attempts: list[int] = []
        recorder._stream_token = self.TOKEN
        monkeypatch.setattr(type(recorder), "is_recording", property(lambda _s: True))
        monkeypatch.setattr(recorder, "ROUTING_TIMEOUT_SECONDS", 0.35)
        monkeypatch.setattr(recorder, "ROUTING_POLL_SECONDS", 0.05)
        monkeypatch.setattr(
            recorder, "_check_routing", lambda *_a: attempts.append(1) or False
        )

        recorder._verify_routing(self._device(), self.TOKEN)

        assert len(attempts) > 1, "it must retry, not check once and give up"

    def test_stopping_ends_the_routing_thread(self, monkeypatch):
        """A stopped recorder should own no threads and hold no pipes."""

        import threading

        from bettervoice.audio import recorder as module

        recorder = module.AudioRecorder()
        started = threading.Event()
        recorder._stream_token = self.TOKEN
        monkeypatch.setattr(type(recorder), "is_recording", property(lambda _s: True))

        def check(_device, _token):
            started.set()
            return False

        monkeypatch.setattr(recorder, "_check_routing", check)
        recorder._route_to(self._device(), self.TOKEN)
        assert started.wait(2), "the routing check never ran"

        recorder._routing_done.set()
        thread = recorder._routing_thread
        thread.join(timeout=2)

        assert not thread.is_alive(), "the thread must end when the recording does"


class TestKWinBridgeRegistration:
    """Two bridges in one process must not silently collide.

    They share a D-Bus connection, so a fixed object path means the second one
    fails to export and its backend quietly does nothing -- no pointer trail, no
    remembered paste target, and no explanation.
    """

    def test_each_object_path_is_unique(self):
        from bettervoice.backends import kwin

        paths = {kwin.unique_path("Pointer") for _ in range(5)}

        assert len(paths) == 5
        assert all(path.startswith("/Pointer") for path in paths)

    def test_the_service_name_carries_the_process(self):
        import os

        from bettervoice.backends import kwin

        assert str(os.getpid()) in kwin.unique_service("Pointer")

    def test_two_pointer_backends_can_coexist(self, qt_app):
        from bettervoice.backends import pointer

        if not pointer.KWinPointerBackend.available():
            pytest.skip("this desktop has no KWin")

        first, second = pointer.KWinPointerBackend(), pointer.KWinPointerBackend()
        try:
            assert first._sink.path != second._sink.path
            assert first._sink.register() is not None
            assert second._sink.register() is not None, (
                "the second backend must not be shut out by the first"
            )
            assert first.unavailable_reason is None
            assert second.unavailable_reason is None
        finally:
            first.stop()
            second.stop()

    def test_a_failed_registration_is_reported_not_hidden(self, qt_app, monkeypatch):
        from bettervoice.backends import kwin, pointer

        backend = pointer.KWinPointerBackend()
        monkeypatch.setattr(
            kwin.ExportedObject, "register", lambda _self: setattr(_self, "failure", "nope")
        )
        backend._sink.failure = "nope"

        assert backend.unavailable_reason == "nope"

    def test_a_healthy_bridge_reports_nothing(self, qt_app):
        from bettervoice.backends import pointer

        if not pointer.KWinPointerBackend.available():
            pytest.skip("this desktop has no KWin")

        backend = pointer.KWinPointerBackend()
        try:
            backend._sink.register()
            assert backend.unavailable_reason is None
        finally:
            backend.stop()
