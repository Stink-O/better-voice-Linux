"""The application controller.

A port of ``AppController`` from ``Sources/BetterVoice/main.swift``: the same
state machine (idle / recording / finishing), the same two recording modes, the
same delivery rules, wired to the Linux backends instead of AppKit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from . import APP_NAME, configure_application, session, workers
from .asr import GrammarCorrector, LocalTranscriber, ModelState
from .audio import AudioRecorder, MicrophoneManager
from .backends import (
    appearance,
    clipboard,
    environment,
    focus,
    global_shortcuts,
    hotkeys,
    pointer,
    screenshot,
    textinject,
)
from .backends.sounds import SoundPlayer
from .config import config
from .paths import cache_dir, ensure
from .core import (
    CircleGesture,
    delivery_report,
    CircleGestureDetector,
    RecordingSoundCue,
    SessionCompletionDisposition,
    session_completion_disposition,
)
from .errors import (
    BetterVoiceError,
    LocalModelUnavailable,
    MicrophoneUnavailable,
    ScreenPermissionRequired,
    SessionStorageFull,
    SessionUnavailable,
)
from .ui.recovery import RecoveryNotice
from .ui.overlay import RecordingOverlay
from .ui.setup_window import SetupModel, SetupRowState, SetupWindow
from .ui.tray import StatusIconState, Tray

log = logging.getLogger(__name__)

#: Safety net, matching the macOS build.
RECORDING_LIMIT_MS = 20 * 60 * 1_000

#: How long the clipboard is left holding text-only so the paste lands.
CLIPBOARD_SETTLE_MS = 300

#: How long to let a blanked overlay reach the screen before capturing. macOS
#: excluded its own windows from the capture outright; here the overlay has to
#: stop drawing and the compositor has to show that, which takes a frame or two.
CAPTURE_BLANK_MS = 48

#: Breathing room after bringing the target window back before typing into it.
FOCUS_SETTLE_MS = 120

#: Below this peak level a recording is treated as silence and never sent to the
#: model. Speech recognisers happily invent a word or two out of near-silence,
#: and an accidental shortcut press should not paste "Yeah." into your editor.
DEFAULT_SILENCE_THRESHOLD = 0.015

AUTOMATIC = "automatic"


class SessionState:
    IDLE = "idle"
    RECORDING = "recording"
    FINISHING = "finishing"


class RecordingMode:
    PUSH_TO_TALK = "push_to_talk"
    LONG_FORM = "long_form"


class AppController(QtCore.QObject):
    def __init__(self, application: QtWidgets.QApplication) -> None:
        super().__init__()
        self._application = application
        configure_application(application)
        self._settings = config()
        self._appearance = appearance.AppearanceWatcher(self._settings.get("reduceMotion"))
        self._appearance.reduce_motion_changed.connect(self._on_reduce_motion_changed)

        self.microphones = MicrophoneManager()
        self.recorder = AudioRecorder()
        self.transcriber = LocalTranscriber(
            model=self._settings.get("asrModel"),
            quantization=self._settings.get("asrQuantization"),
        )
        self.grammar = GrammarCorrector()
        self.sounds = SoundPlayer(enabled=self._settings.bool("soundCuesEnabled"))

        self.pointer = pointer.create(self._settings.get("pointerBackend"))
        self.hotkeys = hotkeys.create(self._settings.get("hotkeyBackend"))
        self.screenshots = screenshot.create(self._settings.get("screenshotBackend"))
        self.focus = focus.create(self._settings.get("focusBackend"))
        self.text_insertion = textinject.create(self._settings.get("textInsertionBackend"))

        self.overlay = RecordingOverlay(reduce_motion=self.reduce_motion)
        self.tray = Tray(reduce_motion=self.reduce_motion)
        self._appearance_applied = self.reduce_motion
        self.recovery = RecoveryNotice()

        self._state = SessionState.IDLE
        self._mode: str | None = None
        self._detector = CircleGestureDetector()
        self._output: session.SessionOutput | None = None
        self._transcript = ""
        self._started_at: float | None = None
        self._recorded_seconds = 0.0
        self._focus_target: focus.FocusTarget | None = None
        self._focus_capture_pending = False
        self._focus_waiters: list = []
        self._recheck_clipboard = False
        self._grammar_ready = False
        self._grammar_busy = False

        self._pending_gestures: list[CircleGesture] = []
        self._capturing = False
        self._awaiting_captures: list = []

        self._limit_timer = QtCore.QTimer(self)
        self._limit_timer.setSingleShot(True)
        self._limit_timer.setInterval(RECORDING_LIMIT_MS)
        self._limit_timer.timeout.connect(self._on_recording_limit)

        self._status_reset = QtCore.QTimer(self)
        self._status_reset.setSingleShot(True)
        self._status_reset.timeout.connect(self._reset_status)

        self._capture_message_timer = QtCore.QTimer(self)
        self._capture_message_timer.setSingleShot(True)
        self._capture_message_timer.timeout.connect(self._clear_capture_message)

        self._setup_model = self._build_setup_model()
        self._setup_window = SetupWindow(self._setup_model)

    @property
    def reduce_motion(self) -> bool:
        """Follows the desktop's animation preference unless overridden."""

        return self._appearance.reduce_motion

    def _on_reduce_motion_changed(self, reduce: bool) -> None:
        self.overlay.reduce_motion = reduce
        self.tray.reduce_motion = reduce
        self.tray.set_icon(
            StatusIconState.RECORDING
            if self._state == SessionState.RECORDING
            else StatusIconState.IDLE
        )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        AudioRecorder.remove_abandoned_recordings()
        self.microphones.refresh()

        self._wire_tray()
        self._update_menu_title(self._start_title(), True)
        self.tray.show()
        self._set_status(self._ready_message())

        self.transcriber.on_status = workers.on_main_thread(self._on_model_status)
        workers.run(self.transcriber.load_cached)
        workers.run(self.grammar.is_cached, self._on_grammar_cached)

        self.hotkeys.push_to_talk_started.connect(self._start_push_to_talk)
        self.hotkeys.push_to_talk_stopped.connect(self._stop_push_to_talk)
        self.hotkeys.long_form_toggled.connect(self.toggle_recording)
        self.hotkeys.promote_to_long_form.connect(self._promote_to_long_form)
        client = getattr(self.hotkeys, "_client", None)
        if client is not None:
            client.bindings_changed.connect(self._on_shortcut_bindings_changed)
            client.failed.connect(self._on_shortcut_failure)
        self.hotkeys.start()

        self.pointer.moved.connect(self._on_pointer_moved)

        self._restore_text_insertion()

        try:
            session.prune()
        except OSError as error:
            self._show_error("Saved-session cleanup failed", detail=str(error))

        if not self._settings.bool("completedOnboarding"):
            QtCore.QTimer.singleShot(0, self.show_setup)

    def _restore_text_insertion(self) -> None:
        """Reconnect a text-insertion grant the user has already given.

        macOS held its Accessibility permission from launch. The portal
        equivalent is a session that dies with the process, so without this the
        user has to open the setup window and press Set Up on every launch --
        with the grant already given and its token already saved. Only when it
        is remembered, or this would be a permission prompt at startup.
        """

        if self.text_insertion.grant_remembered:
            self.text_insertion.prepare()

    def shutdown(self) -> None:
        self.hotkeys.stop()
        self.pointer.stop()
        self.focus.stop()
        # Overlay windows outlive a recording on purpose; shutdown is the one
        # place that actually hands them back.
        self.overlay.close()
        self._limit_timer.stop()
        if self._state == SessionState.RECORDING and self._output is not None:
            self._output.discard()
            self._output = None
        # Always tear the audio stream down, recording or not. Leaving it to the
        # garbage collector lets PortAudio's callback thread run against objects
        # that have already been freed, which takes the process down rather than
        # the stream.
        self.recorder.close()
        stop = getattr(self.text_insertion, "stop", None)
        if callable(stop):
            stop()
        self.tray.hide()

    # -- tray ------------------------------------------------------------

    def _wire_tray(self) -> None:
        self.tray.recording_action.triggered.connect(self.toggle_recording)
        self.tray.model_action.triggered.connect(self.download_model)
        self.tray.setup_action.triggered.connect(self.show_setup)
        self.tray.open_sessions_action.triggered.connect(self.open_saved_sessions)
        self.tray.clear_sessions_action.triggered.connect(self.clear_saved_sessions)
        self.tray.quit_action.triggered.connect(self.quit)
        self.tray.on_menu_open = self._on_menu_open
        self._refresh_microphone_menu()
        self._refresh_model_menu()

    def _on_menu_open(self) -> None:
        self.microphones.refresh()
        self._refresh_microphone_menu()

    def _microphone_options(self) -> list[tuple[str, str]]:
        automatic = self.microphones.automatic_device
        label = (
            self.microphones.selected_label
            if self.microphones.selected_id is None
            else f"Automatic — {automatic.name if automatic else 'Unavailable'}"
        )
        return [(AUTOMATIC, label)] + [
            (device.id, device.name) for device in self.microphones.devices
        ]

    def _refresh_microphone_menu(self) -> None:
        self.tray.rebuild_microphone_menu(
            self._microphone_options(),
            self.microphones.selected_id or AUTOMATIC,
            self._state == SessionState.IDLE,
            self.select_microphone,
        )

    def select_microphone(self, identifier: str) -> None:
        if self._state != SessionState.IDLE:
            return
        self.microphones.select(None if identifier == AUTOMATIC else identifier)
        self.microphones.refresh()
        self._refresh_microphone_menu()
        self._refresh_setup_model()
        self._set_status(f"Microphone: {self.microphones.selected_label}", reset_after=3)

    def _refresh_model_menu(self) -> None:
        state = self.transcriber.status
        idle = self._state == SessionState.IDLE
        if state.state is ModelState.MISSING:
            self.tray.model_action.setText("Download Local Model (~665 MB)")
            self.tray.model_action.setEnabled(idle)
        elif state.state is ModelState.DOWNLOADING:
            self.tray.model_action.setText(f"Downloading Local Model… {state.percent}%")
            self.tray.model_action.setEnabled(False)
        elif state.state is ModelState.LOADING:
            self.tray.model_action.setText("Loading Local Model…")
            self.tray.model_action.setEnabled(False)
        elif state.state is ModelState.READY:
            self.tray.model_action.setText("Local Parakeet Model Ready")
            self.tray.model_action.setEnabled(False)
        else:
            self.tray.model_action.setText("Retry Local Model Download")
            self.tray.model_action.setEnabled(idle)

        if self._state == SessionState.IDLE:
            self.tray.recording_action.setEnabled(self.transcriber.is_ready)

    def _update_menu_title(self, title: str, enabled: bool) -> None:
        self.tray.recording_action.setText(title)
        self.tray.recording_action.setEnabled(
            enabled and (self._state != SessionState.IDLE or self.transcriber.is_ready)
        )

    def _ready_message(self) -> str:
        return f"Ready • {self.hotkeys.shortcut_hint}"

    @property
    def _long_form_keys(self) -> str:
        """The chord that starts a long explanation, for menu titles."""

        if self.hotkeys.name != "portal":
            return "Super + Alt"
        triggers = getattr(self.hotkeys, "_triggers", {})
        return global_shortcuts.friendly_trigger(
            triggers.get(global_shortcuts.LONG_FORM_ID)
            or global_shortcuts.DEFAULT_LONG_FORM_TRIGGER
        )

    @property
    def _quick_note_keys(self) -> str:
        if self.hotkeys.name != "portal":
            return "Alt"
        triggers = getattr(self.hotkeys, "_triggers", {})
        return global_shortcuts.friendly_trigger(
            triggers.get(global_shortcuts.PUSH_TO_TALK_ID)
            or global_shortcuts.DEFAULT_PUSH_TO_TALK_TRIGGER
        )

    def _start_title(self) -> str:
        return f"Start long recording ({self._long_form_keys})"

    def _set_status(self, message: str, reset_after: float | None = None) -> None:
        self._status_reset.stop()
        self.tray.set_status(message)
        if reset_after is not None:
            self._status_reset.start(int(reset_after * 1000))

    def _reset_status(self) -> None:
        if self._state == SessionState.IDLE:
            self._set_status(self._ready_message())

    # -- model -----------------------------------------------------------

    def _on_model_status(self, status) -> None:
        self._refresh_model_menu()
        self._refresh_setup_model()
        if status.state is ModelState.DOWNLOADING:
            self._set_status(f"Downloading local model… {status.percent}%")

    def download_model(self) -> None:
        if self._state != SessionState.IDLE:
            return
        self._set_status("Downloading local model…")

        def finished(_result) -> None:
            state = self.transcriber.status
            if state.state is ModelState.READY:
                self._set_status(f"Local model ready • {self.hotkeys.shortcut_hint}", 4)
            elif state.state is ModelState.FAILED:
                self._show_error(f"Model download failed: {state.message}")

        workers.run(self.transcriber.download, finished)

    def _on_grammar_cached(self, cached: bool) -> None:
        self._grammar_ready = bool(cached)
        self._refresh_setup_model()
        if not self._settings.bool("grammarCorrectionEnabled"):
            return
        if cached:
            # Load the ONNX sessions now so the first transcript is not delayed.
            workers.run(self.grammar.preload)
        else:
            self.download_grammar_model()

    def download_grammar_model(self) -> None:
        if self._state != SessionState.IDLE or self._grammar_busy:
            return
        self._grammar_busy = True
        self._set_status("Downloading grammar model…")
        self._refresh_setup_model()

        def finished(ok: bool) -> None:
            self._grammar_busy = False
            self._grammar_ready = bool(ok)
            self._refresh_setup_model()
            if ok:
                self._set_status("Grammar cleanup ready", 4)
            else:
                self._show_error(
                    "Grammar model download failed",
                    detail="Retry from Getting Started when you are back online.",
                )

        workers.run(self.grammar.preload, finished)

    def set_grammar_correction(self, enabled: bool) -> None:
        self._settings.set("grammarCorrectionEnabled", enabled)
        self._refresh_setup_model()
        if not enabled:
            return
        if self._grammar_ready:
            workers.run(self.grammar.preload)
        else:
            self.download_grammar_model()

    # -- recording -------------------------------------------------------

    def toggle_recording(self) -> None:
        if self._state == SessionState.IDLE:
            self.start_recording(RecordingMode.LONG_FORM)
        elif self._state == SessionState.RECORDING:
            self.stop_recording()

    def _start_push_to_talk(self) -> None:
        if self._state != SessionState.IDLE:
            return
        self.start_recording(RecordingMode.PUSH_TO_TALK)

    def _stop_push_to_talk(self) -> None:
        if self._state == SessionState.RECORDING and self._mode == RecordingMode.PUSH_TO_TALK:
            self.stop_recording()

    def _promote_to_long_form(self) -> None:
        if self._state == SessionState.IDLE:
            self.start_recording(RecordingMode.LONG_FORM)
        elif self._state == SessionState.RECORDING and self._mode == RecordingMode.PUSH_TO_TALK:
            self._mode = RecordingMode.LONG_FORM
            self._set_status("Recording • long-form mode")
            self._update_menu_title(
                f"Stop long recording ({self._long_form_keys})", True
            )

    def start_recording(self, mode: str) -> None:
        try:
            if not self.transcriber.is_ready:
                raise LocalModelUnavailable()
            self.microphones.refresh()
            device = self.microphones.recording_device
            if device is None:
                raise MicrophoneUnavailable()

            self._output = session.SessionOutput()
            self._focus_target = None
            self._focus_capture_pending = False
            self._focus_waiters.clear()
            self._pending_gestures.clear()
            self._awaiting_captures.clear()
            self._capturing = False
            self._detector.reset()
            self._transcript = ""
            self.sounds.play(RecordingSoundCue.STARTED)
            self.recorder.on_level = workers.on_main_thread(self._on_level)
            self.recorder.start(device)
        except (BetterVoiceError, OSError) as error:
            # Whatever failed, leave nothing behind: no half-made session folder
            # on the user's Desktop and no state that would confuse the next try.
            self._abandon_failed_start()
            if isinstance(error, BetterVoiceError):
                self._show_error(str(error))
            else:
                self._show_error("Could not start recording", detail=str(error))
            return

        self._state = SessionState.RECORDING
        self._mode = mode
        self._started_at = time.monotonic()
        self._limit_timer.start()

        self.pointer.start()
        self._refresh_microphone_menu()
        self._refresh_model_menu()

        self.overlay.hud.microphone = device.name
        self.overlay.hud.level = 0.0
        self.overlay.hud.context_count = 0
        self.overlay.hud.is_finishing = False
        self.overlay.hud.capture_message = None
        self.overlay.hud.visible = True
        self.overlay.start(hud_screen=self._pointer_screen())
        self.overlay.announce(f"BetterVoice listening on {device.name}")

        self.tray.set_icon(StatusIconState.RECORDING)
        self._set_status(f"Recording • Microphone: {device.name}")
        self._update_menu_title(
            f"Release {self._quick_note_keys} to stop"
            if mode == RecordingMode.PUSH_TO_TALK
            else f"Stop long recording ({self._long_form_keys})",
            True,
        )

    def _abandon_failed_start(self) -> None:
        if self._output is not None:
            self._output.discard()
        self._output = None
        self._started_at = None
        self._recorded_seconds = 0.0
        self._mode = None
        self._state = SessionState.IDLE

    def _pointer_screen(self) -> QtGui.QScreen | None:
        position = QtGui.QCursor.pos()
        for candidate in QtGui.QGuiApplication.screens():
            if candidate.geometry().contains(position):
                return candidate
        return QtGui.QGuiApplication.primaryScreen()

    def _on_recording_limit(self) -> None:
        if self._state != SessionState.RECORDING:
            return
        self.stop_recording()
        self._show_error(
            "20-minute recording limit reached",
            detail="The recording stopped safely and is being transcribed now.",
        )

    def _on_level(self, level: float) -> None:
        self.overlay.hud.level = max(level, self.overlay.hud.level * 0.78)

    def stop_recording(self) -> None:
        if self._state != SessionState.RECORDING:
            return
        self._limit_timer.stop()
        # Freeze the recording's own length here. Transcription happens after
        # this point and can take seconds, which would otherwise push an
        # accidental tap past the discard threshold.
        self._recorded_seconds = (
            time.monotonic() - self._started_at if self._started_at else 0.0
        )
        self._state = SessionState.FINISHING
        # Remember where the user was typing before the model takes its seconds,
        # so a transcript never lands in whatever they moved on to.
        self._focus_target = None
        self._focus_capture_pending = True
        self.focus.capture(self._on_focus_captured)
        self.pointer.stop()
        self._refresh_microphone_menu()
        self.overlay.stop_trail()

        self.overlay.hud.is_finishing = True
        self.overlay.hud.capture_message = None
        self.overlay.hud.finishing_message = "Transcribing…"
        self.overlay.hud.level = 0.2
        self.overlay.announce("BetterVoice transcribing")

        self.tray.set_icon(StatusIconState.FINISHING)
        self._set_status("Finishing…")
        self._update_menu_title(f"Finishing… ({self._long_form_keys})", False)

        try:
            audio = self.recorder.stop()
        except BetterVoiceError as error:
            failure = error
            self._when_captures_settle(lambda: self._finish_session(failure))
            return

        self.sounds.play(RecordingSoundCue.FINISHED)
        use_grammar = self._settings.bool("grammarCorrectionEnabled")

        threshold = self._settings.get("silenceThreshold", DEFAULT_SILENCE_THRESHOLD)
        peak = self.recorder.peak_level
        if isinstance(threshold, (int, float)) and threshold > 0 and peak < threshold:
            log.info(
                "Heard nothing above %.3f (peak %.3f) — %.1fs held, %.1fs of audio "
                "delivered from %s; skipping the model",
                threshold,
                peak,
                self._recorded_seconds,
                self.recorder.recorded_seconds,
                self.microphones.selected_label,
            )
            audio.unlink(missing_ok=True)
            self._transcript = ""
            self._when_captures_settle(lambda: self._finish_session(None))
            return

        status = workers.on_main_thread(self._set_finishing_status)

        def work() -> str:
            try:
                text = self.transcriber.transcribe(audio)
                if use_grammar and len(text.split()) > 1:
                    status("Polishing transcript locally…")
                    text = self.grammar.correct(text)
                    status("Finishing…")
                return text
            finally:
                audio.unlink(missing_ok=True)

        def finished(text: str) -> None:
            self._transcript = text
            self._when_captures_settle(lambda: self._finish_session(None))

        def failed(error: Exception) -> None:
            self._when_captures_settle(lambda: self._finish_session(error))

        workers.run(work, finished, failed)

    def _on_focus_captured(self, target) -> None:
        self._focus_target = target
        self._focus_capture_pending = False
        if target is not None:
            log.debug("Transcript will go back to %s", target.application or target.caption)
        waiting, self._focus_waiters = self._focus_waiters, []
        for callback in waiting:
            callback()

    def _paste_into_target(self, on_done) -> None:
        """Bring the remembered window forward, then paste into it.

        Asking the compositor which window was active is a round trip, and a
        short recording can reach delivery before the answer arrives -- so wait
        for it rather than pasting somewhere arbitrary.
        """

        if self._focus_capture_pending:
            self._focus_waiters.append(lambda: self._paste_into_target(on_done))
            return

        def paste(_restored: bool = False) -> None:
            window = focus.focused_own_window()
            if window is not None:
                # macOS refused to paste into its own window; so do we. The
                # transcript is already on the clipboard either way.
                log.info(
                    "BetterVoice's own %r window has focus; not pasting into itself",
                    window.title() or type(window).__name__,
                )
                on_done(False)
                return
            self.text_insertion.paste(on_done)

        def restored(ok: bool) -> None:
            if not ok:
                paste()
                return
            # Give the compositor a moment to finish the switch before typing.
            QtCore.QTimer.singleShot(FOCUS_SETTLE_MS, paste)

        self.focus.restore(self._focus_target, restored)

    def _set_finishing_status(self, message: str) -> None:
        """Keep the HUD and the tray in step while a session is wrapping up."""

        if self._state != SessionState.FINISHING:
            return
        self.overlay.hud.finishing_message = message
        self.overlay.announce(message)
        self._set_status(message)

    # -- screen context --------------------------------------------------

    def _on_pointer_moved(self, x: float, y: float) -> None:
        if self._state != SessionState.RECORDING:
            return
        now = time.monotonic()
        self.overlay.add((x, y), now)
        gesture = self._detector.add((x, y), at=now)
        if gesture is None or self._output is None:
            return
        self._pending_gestures.append(gesture)
        self._drain_captures()

    def _drain_captures(self) -> None:
        if self._capturing or not self._pending_gestures or self._output is None:
            return
        gesture = self._pending_gestures.pop(0)
        output = self._output
        destination = output.reserve_image()
        self._capturing = True

        def done(error: Exception | None) -> None:
            self._capturing = False
            if not self._pending_gestures:
                # Only once the queue is empty; blanking and restoring between
                # back-to-back captures would flicker for no benefit.
                self.overlay.show_after_capture()
            self._on_capture_done(gesture, destination, output, error)
            self._drain_captures()
            if not self._pending_gestures:
                self._captures_settled()

        def take() -> None:
            self.screenshots.capture(gesture, destination, workers.on_main_thread(done))

        if self.overlay.hide_from_capture():
            # The blanked frame has to reach the screen before the portal reads
            # it, and a repaint is not on screen until the compositor has
            # composited it.
            QtCore.QTimer.singleShot(CAPTURE_BLANK_MS, take)
        else:
            take()

    def _on_capture_done(
        self,
        gesture: CircleGesture,
        destination: Path,
        output: session.SessionOutput,
        error: Exception | None,
    ) -> None:
        if error is None:
            try:
                output.accept_image(destination)
            except BetterVoiceError as storage_error:
                error = storage_error
        if error is None:
            self.overlay.confirm(gesture.center, gesture.radius, time.monotonic())
            self.overlay.hud.context_count = len(output.images)
            self._show_capture_message("Screenshot captured", clear_after_ms=1_400)
            self.overlay.announce(f"Screenshot {len(output.images)} captured")
            self._set_status(f"Recording • {len(output.images)} screen context")
            return

        destination.unlink(missing_ok=True)
        if isinstance(error, ScreenPermissionRequired):
            self._show_capture_message("Screen permission required")
            self._show_error(
                "Screen capture was declined",
                detail=(
                    "Allow BetterVoice to take screenshots when your desktop asks, "
                    "or clear the decision in your desktop's application permissions."
                ),
            )
        elif isinstance(error, SessionStorageFull):
            self._show_capture_message("Saved-session limit reached")
            self._show_error(
                str(error),
                detail="Finish this recording, or clear older sessions from the tray menu.",
                action_title="Manage Sessions",
                action=self.open_saved_sessions,
            )
        else:
            self._show_capture_message("Screenshot failed")
            self._show_error("Screenshot capture failed", detail=str(error))

    def _show_capture_message(self, message: str, clear_after_ms: int | None = None) -> None:
        """Show a line on the HUD. Failures stay put until something replaces them."""

        self._capture_message_timer.stop()
        self.overlay.hud.capture_message = message
        if clear_after_ms is None:
            self.overlay.announce(message)
        if clear_after_ms is not None:
            self._capture_message_timer.start(clear_after_ms)

    def _clear_capture_message(self) -> None:
        self.overlay.hud.capture_message = None

    def _when_captures_settle(self, callback) -> None:
        if not self._capturing and not self._pending_gestures:
            callback()
            return
        self._awaiting_captures.append(callback)

    def _captures_settled(self) -> None:
        if self._capturing or self._pending_gestures:
            return
        waiting, self._awaiting_captures = self._awaiting_captures, []
        for callback in waiting:
            callback()

    # -- delivery --------------------------------------------------------

    def _finish_session(self, transcription_error: Exception | None) -> None:
        output = self._output
        copy_to_clipboard = self._mode == RecordingMode.LONG_FORM
        has_transcript = bool(self._transcript.strip())
        has_context = bool(output.images) if output is not None else False
        disposition = session_completion_disposition(
            has_transcript=has_transcript,
            has_context=has_context,
            duration=self._recorded_seconds,
        )

        if disposition is not SessionCompletionDisposition.DELIVER and output is not None:
            if disposition is SessionCompletionDisposition.DISCARD_ACCIDENTAL:
                output.discard()
            else:
                try:
                    output.write_markdown(self._transcript)
                except OSError:
                    output.discard()
            self._finish_recording_ui()
            self._set_status(
                "Recording discarded"
                if disposition is SessionCompletionDisposition.DISCARD_ACCIDENTAL
                else "No speech or screen context captured",
                reset_after=4,
            )
            self._prune_saved_sessions()
            return

        if output is None:
            self._finish_recording_ui()
            self._show_error(str(SessionUnavailable()))
            return

        try:
            output.write_markdown(self._transcript)
        except OSError as error:
            self._finish_recording_ui()
            self._show_error("Could not save the session", detail=str(error))
            return

        images = list(output.images)
        transcript = self._transcript.strip()
        self._finish_recording_ui()
        self._deliver(transcript, images, copy_to_clipboard, has_context, transcription_error)
        self._prune_saved_sessions()

    def _deliver(
        self,
        transcript: str,
        images: list[Path],
        copy_to_clipboard: bool,
        has_context: bool,
        transcription_error: Exception | None,
    ) -> None:
        """Insert the transcript where the user was typing, then settle the clipboard.

        A long explanation deliberately leaves the transcript and screenshots on
        the clipboard; a quick note puts back whatever was there before.
        """

        if not transcript:
            copied = copy_to_clipboard and clipboard.copy(transcript, images, self.text_insertion)
            self._report_delivery(
                transcription_error, copy_to_clipboard, copied, False, False, has_context
            )
            return

        previous = None if copy_to_clipboard else clipboard.snapshot()
        if not clipboard.copy_text_only(transcript):
            if previous is not None:
                clipboard.restore(previous)
            self._report_delivery(
                transcription_error, copy_to_clipboard, False, False, True, has_context
            )
            return

        def pasted(success: bool) -> None:
            if success and images:
                # The transcript has landed; send the screenshots after it.
                self._paste_images_after(transcript, images, copy_to_clipboard, previous)
                copied = copy_to_clipboard
            elif success and copy_to_clipboard:
                # Put the screenshots back alongside the transcript once the
                # paste has landed -- unless the user has copied something else
                # in the meantime, in which case their copy wins.
                QtCore.QTimer.singleShot(
                    CLIPBOARD_SETTLE_MS,
                    lambda: clipboard.still_ours(transcript)
                    and clipboard.copy(transcript, images, self.text_insertion),
                )
                copied = True
            elif success and previous is not None:
                QtCore.QTimer.singleShot(
                    CLIPBOARD_SETTLE_MS,
                    lambda: clipboard.restore(previous, only_if_holding=transcript),
                )
                copied = False
            elif not success:
                copied = copy_to_clipboard and clipboard.copy(
                    transcript, images, self.text_insertion
                )
                if not copy_to_clipboard and previous is not None:
                    clipboard.restore(previous, only_if_holding=transcript)
            else:
                copied = copy_to_clipboard
            self._report_delivery(
                transcription_error,
                copy_to_clipboard,
                copied,
                success,
                True,
                has_context,
            )

        self._paste_into_target(workers.on_main_thread(pasted))

    def _paste_images_after(
        self,
        transcript: str,
        images: list[Path],
        copy_to_clipboard: bool,
        previous,
    ) -> None:
        """Send the screenshots as a second paste, behind the transcript.

        macOS put images and text on the pasteboard as separate items, so one
        keystroke delivered both. A Wayland selection holds one item and the
        receiving application picks a single type from it -- offered text, it
        takes text every time. So the screenshots go in a selection of their
        own and get their own keystroke.

        Offering *only* the pictures is what makes the second keystroke safe: a
        plain text field finds nothing it accepts and does nothing, rather than
        pasting the transcript a second time.
        """

        def settle(callback) -> None:
            QtCore.QTimer.singleShot(CLIPBOARD_SETTLE_MS, callback)

        def finish() -> None:
            """Leave the clipboard the way the mode says it should end up."""

            if copy_to_clipboard:
                clipboard.copy(transcript, images, self.text_insertion)
            elif previous is not None:
                clipboard.restore(previous)

        def send() -> None:
            self.text_insertion.paste(workers.on_main_thread(delivered))

        def delivered(success: bool) -> None:
            if not success:
                log.info("The screenshots were not pasted; they stay on the clipboard")
            settle(finish)

        def swap_to_images() -> None:
            """Replace the transcript with the screenshots, once it has landed."""

            if not clipboard.copy_images_only(images, self.text_insertion):
                # Nothing can serve a picture on its own here, so there is no
                # second paste to send; settle the clipboard and stop.
                log.info("Screenshots cannot be pasted on this desktop; leaving them on disk")
                settle(finish)
                return
            settle(send)

        # A paste is reported done when the keystroke has been *sent*, not when
        # the other application has read the clipboard. Swapping it out at once
        # races that read, and the first paste delivers the screenshot instead
        # of the transcript -- so the picture arrives twice and the words never
        # do. Give the transcript the same moment to land that macOS gives it.
        settle(swap_to_images)

    def _report_delivery(
        self,
        transcription_error: Exception | None,
        copy_to_clipboard: bool,
        clipboard_copied: bool,
        inserted: bool,
        had_transcript: bool,
        has_context: bool,
    ) -> None:
        report = delivery_report(
            transcription_error=str(transcription_error) if transcription_error else None,
            copy_to_clipboard=copy_to_clipboard,
            clipboard_copied=clipboard_copied,
            inserted=inserted,
            had_transcript=had_transcript,
            has_context=has_context,
            context_on_clipboard=has_context
            and clipboard.carries_images(self.text_insertion),
        )
        if report.is_error:
            self._show_error(
                report.error,
                detail=self.text_insertion.unavailable_reason
                or "The transcript is on the clipboard — press Ctrl+V where you want it.",
            )
            return
        self._set_status(report.status, reset_after=report.reset_after)

    def _finish_recording_ui(self) -> None:
        self._output = None
        self._started_at = None
        self._recorded_seconds = 0.0
        self._state = SessionState.IDLE
        self._mode = None
        self.overlay.hud.visible = False
        self.overlay.stop()
        self._refresh_microphone_menu()
        self._refresh_model_menu()
        self.tray.set_icon(StatusIconState.IDLE)
        self._update_menu_title(self._start_title(), True)

    def _prune_saved_sessions(self) -> None:
        try:
            session.prune()
        except OSError as error:
            self._show_error("Saved-session cleanup failed", detail=str(error))

    # -- menu actions ----------------------------------------------------

    def open_saved_sessions(self) -> None:
        folder = session.root()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._show_error("Could not open saved sessions", detail=str(error))
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def clear_saved_sessions(self) -> None:
        folder = str(session.root()).replace(str(Path.home()), "~")
        alert = QtWidgets.QMessageBox()
        alert.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        alert.setWindowTitle(APP_NAME)
        alert.setText("Clear all saved BetterVoice sessions?")
        alert.setInformativeText(
            f"This permanently removes transcripts and screenshots from {folder}."
        )
        clear = alert.addButton("Clear Sessions", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        cancel = alert.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        alert.setDefaultButton(cancel)
        alert.exec()
        if alert.clickedButton() is not clear:
            return
        try:
            session.clear()
        except OSError as error:
            self._show_error("Could not clear saved sessions", detail=str(error))
            return
        self._set_status("Saved sessions cleared", reset_after=4)

    def quit(self) -> None:
        self.shutdown()
        self._application.quit()

    # -- setup window ----------------------------------------------------

    def _build_setup_model(self) -> SetupModel:
        model = SetupModel()
        model.choose_microphone = self.select_microphone
        model.set_grammar_correction = self.set_grammar_correction
        model.download_grammar_model = self.download_grammar_model
        model.refresh = self._refresh_setup_model
        model.complete = self._complete_setup
        return model

    def _complete_setup(self) -> None:
        self._settings.set("completedOnboarding", True)
        self._setup_window.hide()

    def show_setup(self) -> None:
        self._recheck_clipboard = True
        self._refresh_setup_model()
        self._setup_window.present()

    def _refresh_setup_model(self) -> None:
        model = self._setup_model
        self.microphones.refresh()

        model.shortcut_hint = self.hotkeys.shortcut_hint
        if self.hotkeys.name == "portal":
            triggers = getattr(self.hotkeys, "_triggers", {})
            model.quick_note_keys = global_shortcuts.friendly_trigger(
                triggers.get(global_shortcuts.PUSH_TO_TALK_ID)
                or hotkeys.DEFAULT_PUSH_TO_TALK_TRIGGER
            )
            model.long_form_keys = global_shortcuts.friendly_trigger(
                triggers.get(global_shortcuts.LONG_FORM_ID)
                or hotkeys.DEFAULT_LONG_FORM_TRIGGER
            )
        else:
            model.quick_note_keys = "Alt"
            model.long_form_keys = "Super + Alt"
        model.environment = (
            f"{environment.describe()} • shortcuts: {self.hotkeys.name} • "
            f"pointer: {self.pointer.name} • capture: {self.screenshots.name} • "
            f"insertion: {self.text_insertion.name}"
        )

        model.microphone_options = self._microphone_options()
        model.selected_microphone = self.microphones.selected_id or AUTOMATIC
        model.microphone_selection_enabled = self._state == SessionState.IDLE
        model.microphone_ready = self.microphones.recording_device is not None
        model.microphone_name = self.microphones.selected_label

        model.grammar_enabled = self._settings.bool("grammarCorrectionEnabled")
        model.grammar_selection_enabled = self._state == SessionState.IDLE
        model.grammar_ready = self._grammar_ready
        model.grammar_busy = self._grammar_busy
        if self._grammar_busy:
            model.grammar_status = "Downloading…"
        elif self._grammar_ready:
            model.grammar_status = "Ready • runs locally on this machine"
        else:
            model.grammar_status = "Download once (~36 MB) before recording"

        days = session.MAX_AGE_SECONDS // 86_400
        megabytes = session.MAX_BYTES // (1024 * 1024)
        folder = str(session.root()).replace(str(Path.home()), "~")
        model.storage_note = (
            f"Sessions stay in {folder} for up to {days} days, capped at {megabytes} MB."
        )

        model.rows = [
            self._shortcut_row(),
            self._screenshot_row(),
            self._insertion_row(),
            self._clipboard_row(),
            self._pointer_row(),
            self._model_row(),
        ]
        if self._setup_window is not None:
            self._setup_window.apply()

    def _on_shortcut_bindings_changed(self, _triggers: dict) -> None:
        self._refresh_setup_model()
        if self._state == SessionState.IDLE:
            self._set_status(self._ready_message())
            self._update_menu_title(self._start_title(), True)
        if self._setup_window.isVisible():
            # Onboarding is on screen and already says this, with a Set Up button.
            return
        if getattr(self.hotkeys, "needs_key_assignment", False) and not self._settings.bool(
            "shortcutAssignmentPrompted"
        ):
            self._settings.set("shortcutAssignmentPrompted", True)
            self._show_error(
                "BetterVoice has no keyboard shortcut yet",
                detail=(
                    "Your desktop registered BetterVoice but has not assigned keys to "
                    "its two shortcuts. Assign them once and hold-to-dictate works "
                    "everywhere. You can always start a recording from the tray menu."
                ),
                action_title="Assign keys",
                action=self._open_shortcut_settings,
            )

    def _on_shortcut_failure(self, message: str) -> None:
        self._refresh_setup_model()
        self._show_error("Global shortcuts are unavailable", detail=message)

    def _open_shortcut_settings(self) -> None:
        if not environment.open_shortcut_settings():
            self._show_error(
                "Could not open your shortcut settings",
                detail=(
                    "Look for BetterVoice under keyboard shortcuts in your desktop's "
                    "settings and assign its two entries."
                ),
            )

    def _shortcut_row(self) -> SetupRowState:
        reason = self.hotkeys.unavailable_reason
        needs_keys = getattr(self.hotkeys, "needs_key_assignment", False)
        return SetupRowState(
            title="Global shortcuts",
            detail=reason or f"Ready via {self.hotkeys.name} • {self.hotkeys.shortcut_hint}",
            ready=reason is None,
            action_title="Set Up" if needs_keys else None,
            action=self._open_shortcut_settings if needs_keys else None,
        )

    def _pointer_row(self) -> SetupRowState:
        reason = self.pointer.unavailable_reason
        if reason is not None:
            detail = reason
        elif self.pointer.accurate:
            detail = f"Ready via {self.pointer.name} • circle anything while recording"
        else:
            detail = (
                f"Ready via {self.pointer.name} • the pointer position is estimated, "
                "so the highlight may drift"
            )
        return SetupRowState(
            title="Circle to capture", detail=detail, ready=reason is None
        )

    def _clipboard_row(self) -> SetupRowState:
        reason = clipboard.unavailable_reason(recheck=self._recheck_clipboard)
        self._recheck_clipboard = False
        if reason is not None:
            detail = reason
        elif clipboard.carries_images(self.text_insertion):
            detail = "Ready • long explanations carry the transcript and its screenshots"
        else:
            detail = (
                "Ready • the transcript is copied. Enable transcript insertion below "
                "and the screenshots ride along too"
            )
        return SetupRowState(title="Clipboard", detail=detail, ready=reason is None)

    def _screenshot_row(self) -> SetupRowState:
        reason = self.screenshots.unavailable_reason
        return SetupRowState(
            title="Screen capture",
            detail=reason
            or f"Ready via {self.screenshots.name} • used only when you circle the screen",
            ready=reason is None,
            action_title="Set Up",
            action=self._test_capture,
        )

    def _insertion_row(self) -> SetupRowState:
        ready = self.text_insertion.ready
        reason = self.text_insertion.unavailable_reason
        return SetupRowState(
            title="Transcript insertion",
            detail=(
                f"Ready via {self.text_insertion.name} • "
                + (
                    "the transcript returns to the window you were typing in"
                    if self.focus.remembers_target
                    else "the transcript goes into whatever has focus"
                )
            )
            if ready
            else (reason or "Not available"),
            ready=ready,
            action_title=None if ready else "Set Up",
            action=None if ready else self.text_insertion.prepare,
        )

    def _model_row(self) -> SetupRowState:
        status = self.transcriber.status
        if status.state is ModelState.MISSING:
            detail = "Download once (~665 MB); transcription stays on this machine"
        elif status.state is ModelState.DOWNLOADING:
            detail = f"Downloading… {status.percent}%"
        elif status.state is ModelState.LOADING:
            detail = "Loading…"
        elif status.state is ModelState.READY:
            detail = "Parakeet model ready"
        else:
            detail = f"Download failed: {status.message}"
        busy = status.state in {ModelState.DOWNLOADING, ModelState.LOADING}
        ready = status.state is ModelState.READY
        return SetupRowState(
            title="Local transcription model",
            detail=detail,
            ready=ready,
            busy=busy,
            action_title=None if ready else "Set Up",
            action=None if ready else self.download_model,
        )

    def _test_capture(self) -> None:
        """Take one throwaway screenshot so the desktop asks now, not mid-recording."""

        destination = ensure(cache_dir()) / "permission-check.png"
        gesture = CircleGesture(center=self._centre_of_pointer_screen(), radius=64)

        def done(error: Exception | None) -> None:
            destination.unlink(missing_ok=True)
            if error is None:
                self._set_status("Screen capture is ready", reset_after=4)
            else:
                self._show_error("Screen capture is not available", detail=str(error))
            self._refresh_setup_model()

        self.screenshots.capture(gesture, destination, workers.on_main_thread(done))

    @staticmethod
    def _centre_of_pointer_screen() -> tuple[float, float]:
        position = QtGui.QCursor.pos()
        return (float(position.x()), float(position.y()))

    # -- errors ----------------------------------------------------------

    def _show_error(
        self,
        message: str,
        detail: str | None = None,
        action_title: str = "Open Setup",
        action=None,
    ) -> None:
        detail = detail or (
            "Open setup to check the microphone, screen capture and the local model."
        )
        self._set_status(f"Needs attention: {message}", reset_after=8)
        # Wayland will not let an unfocused app raise a window, so the recovery
        # notice can open behind whatever the user is doing. A desktop
        # notification is the native way to make sure they see it either way.
        self.tray.notify(message, detail, warning=True)
        self.recovery.show_notice(message, detail, action_title, action or self.show_setup)
