"""End-to-end smoke test against the live desktop session.

Skipped by default because it needs a real seat: a compositor, PipeWire, and the
downloaded model. Run it with::

    BETTERVOICE_INTEGRATION=1 pytest tests/test_integration.py -v

It records real audio from a temporary null sink, draws a circle for the gesture
detector, captures a real screenshot through the portal, and checks that the
session folder ends up with a transcript and its screenshot.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BETTERVOICE_INTEGRATION") != "1",
    reason="set BETTERVOICE_INTEGRATION=1 to run against the live desktop session",
)

PHRASE = "the quick brown fox jumps over the lazy dog"
SINK_NAME = "bettervoice_test_sink"


def _screen_is_locked() -> bool:
    """Whether the session is locked, via whichever screensaver service answers."""

    for service, path in (
        ("org.kde.screensaver", "/ScreenSaver"),
        ("org.freedesktop.ScreenSaver", "/ScreenSaver"),
        ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver"),
    ):
        result = subprocess.run(
            [
                "busctl", "--user", "call", service, path,
                "org.freedesktop.ScreenSaver", "GetActive",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip().endswith("true")
    return False


#: Controllers built during a test, closed when it ends. A controller left to
#: the garbage collector keeps its PortAudio stream running -- which shows up as
#: a second recording competing for the microphone, and as a callback thread
#: running against objects that have already been freed.
_CREATED: list = []


@pytest.fixture(autouse=True)
def _close_controllers():
    yield
    while _CREATED:
        controller = _CREATED.pop()
        try:
            controller.shutdown()
        except Exception:
            pass


@pytest.fixture()
def unlocked_screen():
    """Skip tests that photograph the screen while the session is locked.

    A locked compositor has nothing to capture, so these fail in ways that look
    like transcription or storage bugs. Skipping says what is actually wrong.
    """

    if _screen_is_locked():
        pytest.skip("the session is locked; screen capture cannot work")


@pytest.fixture(scope="session")
def local_model():
    """One loaded model for the whole run.

    Each test used to build its own controller, and each controller loaded the
    ~630 MB ONNX model again in the same process. A dozen of those is enough
    memory pressure to make the native audio and ONNX libraries fall over -- the
    suite was intermittently dumping core. The real app only ever has one.
    """

    from bettervoice.asr import LocalTranscriber

    transcriber = LocalTranscriber()
    transcriber.load_cached()
    if not transcriber.is_ready:
        pytest.skip("the local model has not been downloaded")
    return transcriber


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6 import QtWidgets

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield application


@dataclass(frozen=True)
class NullSink:
    """A silent loopback so a test can 'speak' without using real speakers."""

    sink: str
    monitor: str


@pytest.fixture()
def null_sink():
    result = subprocess.run(
        [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={SINK_NAME}",
            f"sink_properties=device.description={SINK_NAME}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("could not create a null sink")
    module = result.stdout.strip()
    try:
        # PulseAudio silently suffixes a name that is already taken, so read
        # back what we actually got rather than assuming: targeting a stale
        # sink would record silence and fail the test for the wrong reason.
        listing = subprocess.run(
            ["pactl", "-f", "json", "list", "sinks"], capture_output=True, text=True, check=False
        )
        names = [
            entry["name"]
            for entry in json.loads(listing.stdout or "[]")
            if entry.get("owner_module") == int(module)
        ]
        if not names:
            pytest.skip("the null sink did not appear")
        yield NullSink(sink=names[0], monitor=f"{names[0]}.monitor")
    finally:
        subprocess.run(["pactl", "unload-module", module], capture_output=True, check=False)


def _speak(path: Path) -> None:
    subprocess.run(
        ["espeak-ng", "-s", "150", "-w", str(path), PHRASE], check=True, capture_output=True
    )


def _pump(application, seconds: float) -> None:
    from PyQt6 import QtCore

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    # processEvents() deliberately skips DeferredDelete, which a real event loop
    # does process; without this the tests would never see an object freed.
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_records_transcribes_and_captures(
    qt_app, null_sink, tmp_path, monkeypatch, local_model, unlocked_screen
):
    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))

    from bettervoice import session
    from bettervoice.app import AppController, RecordingMode, SessionState
    from bettervoice.audio.devices import MicrophoneDevice

    controller = AppController(qt_app)
    controller.transcriber = local_model

    # Record from the null sink's monitor instead of a real microphone.
    monitor = MicrophoneDevice(id=null_sink.monitor, name="Test monitor", is_external=False)
    monkeypatch.setattr(
        type(controller.microphones), "recording_device", property(lambda _self: monitor)
    )

    wav = tmp_path / "speech.wav"
    _speak(wav)

    controller.start_recording(RecordingMode.LONG_FORM)
    assert controller._state == SessionState.RECORDING

    _wait_for_capture_stream(qt_app, null_sink)
    player = subprocess.Popen(
        ["pw-play", f"--target={null_sink.sink}", str(wav)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Draw two circles in different places; the screenshots must come back in
    # the order they were referenced.
    start = time.monotonic()
    for centre in ((700.0, 500.0), (300.0, 250.0)):
        for index in range(60):
            angle = index / 59 * 2 * math.pi
            controller._on_pointer_moved(
                centre[0] + 120 * math.cos(angle), centre[1] + 100 * math.sin(angle)
            )
            qt_app.processEvents()
            time.sleep(0.012)
        _pump(qt_app, 1.2)  # let the capture finish before the next circle

    player.wait(timeout=30)
    _pump(qt_app, 1.0)
    assert time.monotonic() - start > 2.5, "recording must outlast the accidental-tap window"

    # The level meter is fed from the audio callback thread; if cross-thread
    # dispatch ever breaks again, the HUD would sit silently at zero.
    assert controller.overlay.hud.level > 0, "the HUD level meter never moved"

    controller.stop_recording()

    deadline = time.monotonic() + 90
    while controller._state != SessionState.IDLE and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)
    assert controller._state == SessionState.IDLE, "the session never finished"

    folders = [path for path in session.root().iterdir() if path.is_dir()]
    assert len(folders) == 1, f"expected one session folder, found {folders}"
    folder = folders[0]

    markdown = (folder / "context.md").read_text(encoding="utf-8")
    assert "quick brown fox" in markdown.lower(), markdown
    assert "lazy dog" in markdown.lower(), markdown

    screenshots = sorted(folder.glob("context-*.png"))
    assert len(screenshots) == 2, f"expected one screenshot per circle, got {screenshots}"
    assert [path.name for path in screenshots] == ["context-1.png", "context-2.png"]
    for index, path in enumerate(screenshots, start=1):
        assert path.stat().st_size > 10_000
        assert f"![Context {index}]({path.name})" in markdown
    assert markdown.index("context-1.png") < markdown.index("context-2.png"), (
        "screenshots must be listed in the order they were referenced"
    )

    controller.shutdown()


def test_grammar_cleanup_reports_its_progress(qt_app, null_sink, tmp_path, monkeypatch, local_model):
    """With grammar cleanup on, the HUD should narrate the extra step."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))

    from bettervoice import session
    from bettervoice.app import AppController, RecordingMode, SessionState
    from bettervoice.audio.devices import MicrophoneDevice

    controller = AppController(qt_app)
    _CREATED.append(controller)
    controller.transcriber = local_model
    if not controller.grammar.is_cached():
        pytest.skip("the grammar model has not been downloaded")

    monkeypatch.setattr(controller._settings, "bool", lambda key: key == "grammarCorrectionEnabled")
    monitor = MicrophoneDevice(id=null_sink.monitor, name="Test monitor", is_external=False)
    monkeypatch.setattr(
        type(controller.microphones), "recording_device", property(lambda _self: monitor)
    )

    seen: list[str] = []
    original = controller._set_finishing_status
    monkeypatch.setattr(
        controller, "_set_finishing_status", lambda message: (seen.append(message), original(message))
    )

    wav = tmp_path / "speech.wav"
    _speak(wav)

    controller.start_recording(RecordingMode.LONG_FORM)
    _wait_for_capture_stream(qt_app, null_sink)
    player = subprocess.Popen(
        ["pw-play", f"--target={null_sink.sink}", str(wav)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    player.wait(timeout=30)
    _pump(qt_app, 3.0)
    controller.stop_recording()

    deadline = time.monotonic() + 120
    while controller._state != SessionState.IDLE and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)
    assert controller._state == SessionState.IDLE

    # Check the audio actually arrived first: if it did not, the silence gate
    # skips the model entirely and the progress assertion below would fail for
    # a misleading reason.
    folder = next(path for path in session.root().iterdir() if path.is_dir())
    markdown = (folder / "context.md").read_text(encoding="utf-8")
    assert "quick brown fox" in markdown.lower(), (
        f"the null sink delivered no speech (peak {controller.recorder.peak_level:.4f})"
    )

    assert seen == ["Polishing transcript locally…", "Finishing…"], seen

    controller.shutdown()


def _controller_ready(qt_app, monkeypatch, null_sink, local_model):
    """A controller wired to the silent null sink instead of a real microphone."""

    from bettervoice.app import AppController
    from bettervoice.audio.devices import MicrophoneDevice

    controller = AppController(qt_app)
    controller.transcriber = local_model
    monitor = MicrophoneDevice(id=null_sink.monitor, name="Test monitor", is_external=False)
    monkeypatch.setattr(
        type(controller.microphones), "recording_device", property(lambda _self: monitor)
    )
    _CREATED.append(controller)
    return controller


def _sources() -> list[dict]:
    listing = subprocess.run(
        ["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, check=False
    )
    try:
        return json.loads(listing.stdout or "[]")
    except ValueError:
        return []


def _source_names() -> dict[int, str]:
    try:
        return {entry["index"]: entry["name"] for entry in _sources()}
    except KeyError:
        return {}


def _source_state(name: str) -> str | None:
    for entry in _sources():
        if entry.get("name") == name:
            return entry.get("state")
    return None


def _our_capture_sources() -> list[str]:
    """Every source a BetterVoice stream is connected to.

    More than one means a previous recorder was never closed and is still
    capturing -- which competes for the microphone and makes any assertion about
    "our" stream meaningless.
    """

    listing = subprocess.run(
        ["pactl", "-f", "json", "list", "source-outputs"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        outputs = json.loads(listing.stdout or "[]")
    except ValueError:
        return []
    sources = _source_names()
    return [
        sources.get(entry.get("source"))
        for entry in outputs
        if (entry.get("properties") or {}).get("application.name") == "BetterVoice"
    ]


def _our_capture_source() -> str | None:
    found = _our_capture_sources()
    return found[0] if found else None


def _wait_for_capture_stream(qt_app, null_sink, seconds: float = 12) -> None:
    """Block until the recorder is connected to the null sink's monitor.

    Two things go wrong without this. Starting playback before PipeWire has
    connected the stream records silence; and if the stream lands on the *default*
    source instead, the test records the room through a real microphone -- which
    then transcribes to something and fails an assertion far from the cause.

    The budget is deliberately longer than the recorder's own routing-correction
    window, so a failure here means the app never corrected it, not that the test
    ran out of patience first.
    """

    deadline = time.monotonic() + seconds
    connected: list[str] = []
    while time.monotonic() < deadline:
        connected = _our_capture_sources()
        if connected == [null_sink.monitor]:
            break
        qt_app.processEvents()
        time.sleep(0.05)
    else:
        if len(connected) > 1:
            pytest.fail(
                f"{len(connected)} BetterVoice streams are capturing at once ({connected}); "
                "a recorder from an earlier test was never closed"
            )
        pytest.fail(
            f"the capture stream never reached {null_sink.monitor} (it is on {connected!r}); "
            "the test would have recorded the room instead"
        )

    # PipeWire suspends idle nodes, and a just-resumed one drops the first
    # moments of audio. A null sink's monitor stays IDLE even while it is being
    # captured, so its state is not a usable readiness signal -- give the graph
    # a beat instead.
    _pump(qt_app, 0.6)  # clear the warm-up window and let the node resume


def _wait_for_idle(controller, qt_app, seconds: float = 90) -> None:
    from bettervoice.app import SessionState

    deadline = time.monotonic() + seconds
    while controller._state != SessionState.IDLE and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)
    assert controller._state == SessionState.IDLE, "the session never finished"


def test_an_accidental_tap_is_discarded_without_a_trace(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """Under 2.5 seconds with no speech and no circles: quietly dropped."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)

    controller.start_recording(RecordingMode.PUSH_TO_TALK)
    _wait_for_capture_stream(qt_app, null_sink)
    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    assert controller.recorder.peak_level < 0.015, (
        f"the recording was not silent (peak {controller.recorder.peak_level:.4f}): "
        "the stream was on a real microphone, not the null sink"
    )
    assert not list(session.root().glob("*/context.md")), "nothing should have been saved"
    controller.shutdown()


def test_a_longer_silent_recording_is_saved_without_an_error(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """Past the accidental-tap window, an empty session is kept, not an error."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    errors: list[str] = []
    monkeypatch.setattr(
        controller, "_show_error", lambda message, **kwargs: errors.append(message)
    )

    controller.start_recording(RecordingMode.LONG_FORM)
    _wait_for_capture_stream(qt_app, null_sink)
    _pump(qt_app, 3.2)
    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    assert controller.recorder.peak_level < 0.015, (
        f"the recording was not silent (peak {controller.recorder.peak_level:.4f}): "
        "the stream was on a real microphone, not the null sink"
    )
    saved = list(session.root().glob("*/context.md"))
    assert len(saved) == 1, f"the session should have been kept, found {saved}"
    assert "_No transcript captured._" in saved[0].read_text(encoding="utf-8")
    assert errors == [], f"an empty recording is not an error: {errors}"
    controller.shutdown()


def test_the_recording_limit_stops_the_session_on_its_own(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """The 20-minute safety net, wound down so the test can watch it fire."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice.app import RecordingMode, SessionState

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    errors: list[str] = []
    monkeypatch.setattr(
        controller, "_show_error", lambda message, **kwargs: errors.append(message)
    )

    controller._limit_timer.setInterval(700)
    controller.start_recording(RecordingMode.LONG_FORM)
    assert controller._state == SessionState.RECORDING

    _pump(qt_app, 1.5)
    assert controller._state != SessionState.RECORDING, "the limit should have stopped it"

    _wait_for_idle(controller, qt_app)
    assert any("limit reached" in message for message in errors), errors
    controller.shutdown()


def test_the_window_being_typed_in_is_remembered(qt_app, null_sink, tmp_path, monkeypatch, local_model):
    """The transcript must go back where the user was, not wherever focus drifts."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice.app import RecordingMode

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    if not controller.focus.remembers_target:
        pytest.skip("this desktop cannot report the active window")

    captured: list = []
    original = controller._on_focus_captured
    monkeypatch.setattr(
        controller,
        "_on_focus_captured",
        lambda target: (captured.append(target), original(target)),
    )

    controller.start_recording(RecordingMode.LONG_FORM)
    _pump(qt_app, 0.5)
    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    deadline = time.monotonic() + 10
    while not captured and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.02)
    assert captured, "the active window was never captured"
    assert controller._focus_target is not None or captured[0] is None, (
        "the captured target must survive until the transcript is delivered"
    )
    target = captured[0]
    if target is not None:
        assert target.window_id, target
        assert not target.application.lower().startswith("bettervoice"), (
            "BetterVoice must never choose itself as the paste target"
        )
    controller.shutdown()


def test_repeated_sessions_do_not_accumulate_anything(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """BetterVoice runs for days in a tray; sessions must leave nothing behind."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from PyQt6 import QtDBus, QtWidgets

    from bettervoice.app import RecordingMode

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    controller.start()
    _pump(qt_app, 0.5)

    def open_files() -> int:
        return len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))

    def settled_open_files(seconds: float = 4) -> int:
        """Descriptors once the count stops moving.

        Sessions leave subprocesses and worker threads finishing for a moment
        afterwards; sampling immediately measures that churn rather than a leak.
        """

        deadline = time.monotonic() + seconds
        previous, stable_since = open_files(), time.monotonic()
        while time.monotonic() < deadline:
            _pump(qt_app, 0.15)
            current = open_files()
            if current != previous:
                previous, stable_since = current, time.monotonic()
            elif time.monotonic() - stable_since > 0.6:
                return current
        return open_files()

    def kwin_scripts() -> list[str]:
        bus = QtDBus.QDBusConnection.sessionBus()
        scripting = QtDBus.QDBusInterface(
            "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus
        )
        if not scripting.isValid():
            return []
        loaded = []
        for name in (
            "bettervoice-pointer",
            "bettervoice-focus-capture",
            "bettervoice-focus-restore",
        ):
            reply = scripting.call("isScriptLoaded", name)
            if reply.arguments() and reply.arguments()[0]:
                loaded.append(name)
        return loaded

    def run_session() -> None:
        controller.start_recording(RecordingMode.LONG_FORM)
        _pump(qt_app, 0.35)
        controller.stop_recording()
        _wait_for_idle(controller, qt_app)
        _pump(qt_app, 0.4)

    run_session()  # first run warms caches; measure from a settled state
    baseline_widgets = len(QtWidgets.QApplication.topLevelWidgets())
    baseline_files = settled_open_files()

    for _ in range(3):
        run_session()
        assert controller.overlay._windows == [], "overlay windows outlived their session"
        assert kwin_scripts() == [], "a KWin script was left loaded"

    # Fewer is fine -- earlier tests' widgets get reaped as the loop runs. The
    # invariant is that sessions do not *add* any.
    assert len(QtWidgets.QApplication.topLevelWidgets()) <= baseline_widgets, (
        "windows are accumulating across sessions"
    )
    settled = settled_open_files()
    assert settled <= baseline_files + 4, (
        f"file descriptors grew from {baseline_files} to {settled} and stayed there"
    )

    controller.shutdown()
    _pump(qt_app, 0.3)
    assert kwin_scripts() == [], "shutdown left a KWin script behind"


def test_a_missing_model_refuses_to_record_and_says_why(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """The one thing that genuinely blocks recording should say so clearly."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode, SessionState
    from bettervoice.asr.transcriber import ModelState, ModelStatus

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    monkeypatch.setattr(controller.transcriber, "status", ModelStatus(ModelState.MISSING))
    monkeypatch.setattr(controller.transcriber, "_model", None)

    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", lambda message, **kw: errors.append(message))

    controller.start_recording(RecordingMode.LONG_FORM)
    _pump(qt_app, 0.3)

    assert controller._state == SessionState.IDLE, "a refused start must not leave a session open"
    assert errors and "model" in errors[0].lower(), errors
    assert not list(session.root().glob("*/")), "nothing should have been written to disk"
    controller.shutdown()


def test_no_microphone_is_reported_and_leaves_nothing_behind(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode, SessionState
    from bettervoice.audio.devices import MicrophoneManager

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    monkeypatch.setattr(MicrophoneManager, "recording_device", property(lambda _self: None))

    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", lambda message, **kw: errors.append(message))

    controller.start_recording(RecordingMode.PUSH_TO_TALK)
    _pump(qt_app, 0.3)

    assert controller._state == SessionState.IDLE
    assert errors and "microphone" in errors[0].lower(), errors
    assert not list(session.root().glob("*/"))
    controller.shutdown()


def test_a_failing_screenshot_does_not_break_the_recording(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """A capture that fails should be reported on the HUD, not abort the session."""

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode
    from bettervoice.errors import ScreenshotUnavailable

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)
    monkeypatch.setattr(
        controller.screenshots,
        "capture",
        lambda gesture, destination, on_done: on_done(ScreenshotUnavailable()),
    )
    monkeypatch.setattr(controller, "_show_error", lambda message, **kw: None)

    controller.start_recording(RecordingMode.LONG_FORM)
    centre = (700.0, 500.0)
    for index in range(60):
        angle = index / 59 * 2 * math.pi
        controller._on_pointer_moved(
            centre[0] + 120 * math.cos(angle), centre[1] + 100 * math.sin(angle)
        )
        qt_app.processEvents()
        time.sleep(0.012)
    _pump(qt_app, 2.5)

    assert controller.overlay.hud.capture_message == "Screenshot failed"

    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    # The recording still completes and is saved; only the picture is missing.
    saved = list(session.root().glob("*/context.md"))
    assert len(saved) == 1, saved
    assert not list(session.root().glob("*/context-*.png"))
    controller.shutdown()


def test_it_still_works_with_every_optional_backend_switched_off(
    qt_app, null_sink, tmp_path, monkeypatch, local_model
):
    """The shape of a desktop that is not KDE: no pointer, no focus tracking.

    Those backends are unavailable on plenty of Wayland compositors, so the
    degraded path has to keep recording, transcribing and saving.
    """

    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import AppController, RecordingMode
    from bettervoice.audio.devices import MicrophoneDevice
    from bettervoice.config import config

    settings = config()
    for key in ("pointerBackend", "focusBackend"):
        monkeypatch.setitem(settings._values, key, "none")

    controller = AppController(qt_app)
    _CREATED.append(controller)
    controller.transcriber = local_model

    assert controller.pointer.name == "none"
    assert controller.focus.remembers_target is False
    assert controller.pointer.unavailable_reason, "the setup window must explain why"

    monitor = MicrophoneDevice(id=null_sink.monitor, name="Test monitor", is_external=False)
    monkeypatch.setattr(
        type(controller.microphones), "recording_device", property(lambda _self: monitor)
    )
    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", lambda message, **kw: errors.append(message))

    wav = tmp_path / "speech.wav"
    _speak(wav)

    controller.start_recording(RecordingMode.LONG_FORM)
    _wait_for_capture_stream(qt_app, null_sink)
    player = subprocess.Popen(
        ["pw-play", f"--target={null_sink.sink}", str(wav)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    player.wait(timeout=30)
    _pump(qt_app, 1.0)
    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    saved = list(session.root().glob("*/context.md"))
    assert len(saved) == 1, saved
    assert "quick brown fox" in saved[0].read_text(encoding="utf-8").lower()
    assert errors == [], f"a desktop without KWin is not an error state: {errors}"
    controller.shutdown()


def test_a_full_session_folder_is_reported_without_losing_the_recording(
    qt_app, null_sink, tmp_path, monkeypatch, local_model, unlocked_screen
):
    monkeypatch.setenv("BETTERVOICE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from bettervoice import session
    from bettervoice.app import RecordingMode
    from bettervoice.errors import SessionStorageFull

    controller = _controller_ready(qt_app, monkeypatch, null_sink, local_model)

    def refuse(path):
        raise SessionStorageFull()

    reported: list[str] = []
    monkeypatch.setattr(controller, "_show_error", lambda message, **kw: reported.append(message))

    controller.start_recording(RecordingMode.LONG_FORM)
    monkeypatch.setattr(controller._output, "accept_image", refuse)

    centre = (700.0, 500.0)
    for index in range(60):
        angle = index / 59 * 2 * math.pi
        controller._on_pointer_moved(
            centre[0] + 120 * math.cos(angle), centre[1] + 100 * math.sin(angle)
        )
        qt_app.processEvents()
        time.sleep(0.012)
    _pump(qt_app, 3.0)

    assert controller.overlay.hud.capture_message == "Saved-session limit reached"
    assert any("500 MB" in message for message in reported), reported

    controller.stop_recording()
    _wait_for_idle(controller, qt_app)

    assert list(session.root().glob("*/context.md")), "the transcript must still be saved"
    controller.shutdown()
