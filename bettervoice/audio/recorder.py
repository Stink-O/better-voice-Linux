"""Recording to a 16 kHz mono WAV, routed to the chosen microphone.

PortAudio reaches PipeWire through its ALSA plugin, which resamples to whatever
rate is asked for. Choosing a specific microphone without disturbing the user's
system-wide default is done with ``PIPEWIRE_NODE``: the plugin reads it when the
stream is created and connects straight to that source. ``pactl
move-source-output`` is the fallback for a PulseAudio server that does not honour
it, and it is what the desktop's own volume panel does anyway.

The stream also announces itself as "BetterVoice", so it appears under its own
name in the system volume panel rather than as an anonymous ALSA client.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from contextlib import contextmanager

from ..errors import MicrophoneRoutingFailed, MicrophoneUnavailable, SessionUnavailable
from ..paths import ensure_private, runtime_dir
from .devices import MicrophoneDevice

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
BLOCK_SIZE = 1_024

#: Ignore the first moments so the stream's start-up noise never reaches disk.
WARMUP_SECONDS = 0.2

#: PortAudio devices that route through PipeWire/PulseAudio rather than raw ALSA.
_SERVER_DEVICES = ("pipewire", "pulse", "default")

#: Property carrying a token unique to one recording. Two recordings a moment
#: apart both answer to "BetterVoice", so the name alone cannot tell the routing
#: check which stream is the one it just opened -- and correcting the wrong one
#: leaves the recording on the wrong microphone for its whole length.
STREAM_TOKEN_PROPERTY = "bettervoice.stream.id"


def _stream_properties(token: str) -> str:
    """Announce the stream under the app's own name in the volume panel."""

    return (
        "{ application.name = BetterVoice "
        "application.icon_name = audio-input-microphone "
        f"{STREAM_TOKEN_PROPERTY} = {token} }}"
    )

LevelCallback = Callable[[float], None]


def _pactl_json(*arguments: str):
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


@contextmanager
def _stream_routed_to(device: MicrophoneDevice, token: str):
    """Ask the PipeWire ALSA plugin to open on one specific source."""

    previous = {key: os.environ.get(key) for key in ("PIPEWIRE_NODE", "PIPEWIRE_PROPS")}
    if device.portaudio_index is None:
        os.environ["PIPEWIRE_NODE"] = device.id
    os.environ["PIPEWIRE_PROPS"] = _stream_properties(token)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AudioRecorder:
    """One recording at a time, mirroring the macOS ``AudioRecorder``."""

    def __init__(self) -> None:
        self._stream = None
        self._file = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        self._accepting = False
        # PortAudio calls this from its own thread. If the closure were only
        # reachable from the C stream, a garbage collection could free it while
        # that thread is inside it -- which crashes the process, not the stream.
        self._callback = None
        self._routing_done = threading.Event()
        self._routing_thread: threading.Thread | None = None
        # Identifies the stream this recording opened, so a routing check can
        # tell it apart from one a previous recording has not finished closing.
        self._stream_token: str | None = None
        self._moved_at = 0.0
        self._move_failure_logged = False
        self._frames_seen = 0
        self._peak_level = 0.0
        self.on_level: LevelCallback | None = None

    @staticmethod
    def remove_abandoned_recordings() -> None:
        """Delete WAVs left behind by a BetterVoice process that is gone."""

        directory = runtime_dir()
        if not directory.is_dir():
            return
        for path in directory.glob("BetterVoice-*.wav"):
            parts = path.stem.split("-", 2)
            if len(parts) == 3 and parts[1].isdigit():
                try:
                    os.kill(int(parts[1]), 0)
                    continue  # still running
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
            path.unlink(missing_ok=True)

    @staticmethod
    def _server_device_name() -> str | None:
        try:
            import sounddevice as sd

            names = {str(device["name"]) for device in sd.query_devices()}
        except Exception:  # pragma: no cover - audio stack missing
            return None
        for candidate in _SERVER_DEVICES:
            if candidate in names:
                return candidate
        return None

    def start(self, device: MicrophoneDevice) -> None:
        try:
            import sounddevice as sd
            import soundfile as sf
        except Exception as error:  # pragma: no cover
            raise MicrophoneUnavailable() from error

        with self._lock:
            if self._stream is not None:
                raise SessionUnavailable()

        if device.portaudio_index is not None:
            target = device.portaudio_index
        else:
            target = self._server_device_name()
            if target is None:
                raise MicrophoneUnavailable()

        path = ensure_private(runtime_dir()) / f"BetterVoice-{os.getpid()}-{uuid.uuid4()}.wav"
        # Claim the file at 0600 before any audio reaches it. Left to the
        # library it is created with the umask's blessing, which on most
        # systems means every account on the machine can read the recording
        # while it is still being written.
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600))
        handle = sf.SoundFile(
            str(path), mode="w", samplerate=SAMPLE_RATE, channels=CHANNELS, subtype="PCM_16"
        )
        self._frames_seen = 0
        self._peak_level = 0.0
        warmup_frames = int(SAMPLE_RATE * WARMUP_SECONDS)
        level_callback = self.on_level

        def callback(indata, frames, time_info, status) -> None:
            if status:
                log.debug("Audio stream status: %s", status)
            self._frames_seen += frames
            if self._frames_seen < warmup_frames:
                return
            # PortAudio can still be inside a callback while stop() runs, so the
            # handle must not be closed out from under this write. The lock is
            # held only around the write, which is already doing I/O anyway.
            with self._lock:
                if not self._accepting:
                    return
                try:
                    handle.write(indata.copy())
                except Exception as error:  # pragma: no cover
                    log.warning("Could not write audio: %s", error)
                    return
            if frames == 0:
                return
            if level_callback is None:
                total = float((indata[:, 0].astype("float64") ** 2).sum())
                self._peak_level = max(
                    self._peak_level, min(1.0, math.sqrt(total / frames) * 12)
                )
                return
            total = float((indata[:, 0].astype("float64") ** 2).sum())
            level = min(1.0, math.sqrt(total / frames) * 12)
            self._peak_level = max(self._peak_level, level)
            level_callback(level)

        token = uuid.uuid4().hex
        self._moved_at = 0.0
        self._move_failure_logged = False
        with self._lock:
            self._accepting = True
        try:
            with _stream_routed_to(device, token):
                stream = sd.InputStream(
                    device=target,
                    channels=CHANNELS,
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SIZE,
                    dtype="float32",
                    callback=callback,
                )
                stream.start()
        except Exception as error:
            with self._lock:
                self._accepting = False
            handle.close()
            path.unlink(missing_ok=True)
            raise MicrophoneRoutingFailed(device.name, str(error).strip()) from error

        with self._lock:
            self._stream = stream
            self._file = handle
            self._path = path
            self._callback = callback
            self._stream_token = token

        if device.portaudio_index is None:
            self._route_to(device, token)

    #: How long to keep looking for our own stream before giving up on checking
    #: which source it landed on. Generous, because the check costs nothing once
    #: the stream is found and giving up early leaves the user recording from
    #: the wrong microphone for the whole session. The loop exits as soon as the
    #: recording stops regardless, so this can never outlive a session.
    ROUTING_TIMEOUT_SECONDS = 10.0

    #: Gap between checks. Each one runs two `pactl` queries, so this is a
    #: trade between correcting quickly and churning subprocesses.
    ROUTING_POLL_SECONDS = 0.1

    #: How long to let a move take effect before asking for it again. PipeWire
    #: accepts `move-source-output` for a stream it is still setting up and then
    #: quietly drops it, so a move that is never confirmed has to be repeated --
    #: but repeating it every poll would spawn a `pactl` per 100ms.
    MOVE_RETRY_SECONDS = 1.0

    def _route_to(self, device: MicrophoneDevice, token: str) -> None:
        """Verify which source we actually landed on, and move if it is wrong.

        Run on a worker thread: the stream takes a moment to register with
        PipeWire, and checking once immediately would usually find nothing and
        silently leave the user recording from the wrong microphone.
        """

        self._routing_done.clear()
        self._routing_thread = threading.Thread(
            target=self._verify_routing,
            args=(device, token),
            name="bettervoice-routing",
            daemon=True,
        )
        self._routing_thread.start()

    def _verify_routing(self, device: MicrophoneDevice, token: str) -> None:
        deadline = time.monotonic() + self.ROUTING_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # A thread that outlived its own recording -- stop() only joins it
            # briefly -- must not go on to correct the next recording's stream
            # towards the microphone this one was asked for.
            if self._routing_done.is_set() or self._stream_token != token:
                return
            if not self.is_recording:
                return  # the recording ended before we could check
            if self._check_routing(device, token):
                return
            # Wait on the event rather than sleeping, so stopping the recording
            # ends this thread at once instead of after another poll.
            if self._routing_done.wait(self.ROUTING_POLL_SECONDS):
                return
        log.warning(
            "Could not confirm that audio is coming from %s within %.0fs",
            device.name,
            self.ROUTING_TIMEOUT_SECONDS,
        )

    def _check_routing(self, device: MicrophoneDevice, token: str) -> bool:
        """True once our stream has been *seen* on the right source.

        False means the answer is still unknown -- the stream has not appeared
        yet, or a move has been asked for and has not taken effect -- so the
        caller polls again. Treating the move itself as success is what left
        recordings on the default microphone: PipeWire returns 0 for a stream it
        is still setting up and then drops the request on the floor.
        """

        sources = {
            source.get("index"): source.get("name")
            for source in (_pactl_json("list", "sources") or [])
        }
        outputs = _pactl_json("list", "source-outputs")
        if not outputs:
            return False
        for output in outputs:
            properties = output.get("properties", {}) or {}
            if properties.get(STREAM_TOKEN_PROPERTY) != token:
                continue
            landed_on = sources.get(output.get("source"))
            if landed_on == device.id:
                return True
            index = output.get("index")
            if index is None:
                return False
            now = time.monotonic()
            if now - self._moved_at < self.MOVE_RETRY_SECONDS:
                return False  # a move is already in flight; give it a moment
            self._moved_at = now
            log.info("Audio landed on %s; moving it to %s", landed_on, device.name)
            try:
                result = subprocess.run(
                    ["pactl", "move-source-output", str(index), device.id],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                self._log_move_failure(device, error)
                return False
            if result.returncode != 0:
                self._log_move_failure(device, result.stderr.decode(errors="replace").strip())
            return False  # confirm it on the next poll rather than assuming
        return False

    def _log_move_failure(self, device: MicrophoneDevice, reason: object) -> None:
        """Report a failed move once per recording, then stop repeating it."""

        if self._move_failure_logged:
            log.debug("Still could not route audio to %s: %s", device.name, reason)
            return
        self._move_failure_logged = True
        log.warning("Could not route audio to %s: %s", device.name, reason)

    def stop(self) -> Path:
        # Release the routing check first: it holds pipes to `pactl` while it
        # runs, and a stopped recorder should own no threads.
        self._routing_done.set()
        thread, self._routing_thread = self._routing_thread, None
        if thread is not None:
            thread.join(timeout=1)
        with self._lock:
            # Refuse further writes before anything is torn down; a callback
            # already running will finish, and the next one will bail out.
            self._accepting = False
            stream, self._stream = self._stream, None
            handle, self._file = self._file, None
            path, self._path = self._path, None
            # Retires the token, so a routing thread that outlived the join
            # above sees its recording is over and stops.
            self._stream_token = None
        if stream is None or handle is None or path is None:
            raise SessionUnavailable()
        try:
            stream.stop()
            stream.close()
        except Exception as error:  # pragma: no cover
            log.warning("Could not stop the audio stream cleanly: %s", error)
        with self._lock:
            handle.close()
            self._callback = None
        self._routing_done = threading.Event()
        self._routing_thread: threading.Thread | None = None
        return path

    def close(self) -> None:
        """Tear the stream down deterministically.

        Letting the garbage collector do it is how the callback thread ends up
        running against freed objects.
        """

        try:
            self.stop().unlink(missing_ok=True)
        except (SessionUnavailable, OSError):
            pass

    def __del__(self) -> None:  # pragma: no cover - a safety net, not a path
        try:
            self.close()
        except Exception:
            pass

    @property
    def peak_level(self) -> float:
        """The loudest moment of the last recording, on the same 0-1 scale as the HUD."""

        return self._peak_level

    @property
    def recorded_seconds(self) -> float:
        """Audio the stream actually delivered, which is not the wall clock.

        A session can run for seconds while PortAudio hands over almost nothing;
        telling the two apart is the difference between "you tapped too fast"
        and "the microphone is not feeding us".
        """

        return self._frames_seen / SAMPLE_RATE

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None
