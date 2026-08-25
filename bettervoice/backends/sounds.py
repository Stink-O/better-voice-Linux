"""The two soft cues that bracket a recording.

macOS played the built-in "Purr" and "Pop" system sounds. The Linux equivalent
is the freedesktop sound theme, played through libcanberra or PipeWire. If the
theme is missing, BetterVoice synthesises the two cues itself so the audible
confirmation never silently disappears.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

from ..core import RecordingSoundCue
from . import environment

log = logging.getLogger(__name__)

_THEME_DIRECTORIES = (
    Path("/usr/share/sounds/freedesktop/stereo"),
    Path("/usr/local/share/sounds/freedesktop/stereo"),
)

#: Synthesised fallbacks: (frequencies, seconds, peak amplitude).
_SYNTH = {
    RecordingSoundCue.STARTED: ((587.33, 880.0), 0.11, 0.22),
    RecordingSoundCue.FINISHED: ((880.0, 587.33), 0.08, 0.22),
}

VOLUME = 0.35


def _theme_file(cue: RecordingSoundCue) -> Path | None:
    for directory in _THEME_DIRECTORIES:
        candidate = directory / f"{cue.freedesktop_sound_name}.oga"
        if candidate.is_file():
            return candidate
    return None


class SoundPlayer:
    """Fire-and-forget cue playback on a background thread."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._canberra = shutil.which("canberra-gtk-play")
        self._player = shutil.which("paplay") or shutil.which("pw-play")

    def play(self, cue: RecordingSoundCue) -> None:
        if not self.enabled:
            return
        threading.Thread(
            target=self._play, args=(cue,), name="bettervoice-sound", daemon=True
        ).start()

    def _play(self, cue: RecordingSoundCue) -> None:
        if self._canberra:
            result = self._run(
                [self._canberra, "--id", cue.freedesktop_sound_name, f"--volume={VOLUME:.2f}"]
            )
            if result:
                return
        theme = _theme_file(cue)
        if theme is not None and self._player:
            argv = [self._player, str(theme)]
            if self._player.endswith("paplay"):
                argv.insert(1, f"--volume={int(VOLUME * 65536)}")
            if self._run(argv):
                return
        self._synthesise(cue)

    @staticmethod
    def _run(argv: list[str]) -> bool:
        try:
            return subprocess.run(argv, capture_output=True, timeout=5, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError) as error:
            log.debug("%s failed: %s", argv[0], error)
            return False

    @staticmethod
    def _synthesise(cue: RecordingSoundCue) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as error:  # pragma: no cover - audio stack missing
            log.debug("Cannot synthesise the %s cue: %s", cue.value, error)
            return

        frequencies, duration, amplitude = _SYNTH[cue]
        rate = 44_100
        samples = np.zeros(0, dtype="float32")
        for frequency in frequencies:
            count = int(rate * duration / len(frequencies))
            t = np.arange(count) / rate
            tone = np.sin(2 * np.pi * frequency * t)
            envelope = np.minimum(1.0, np.minimum(t * 220, (count / rate - t) * 220))
            samples = np.concatenate([samples, (tone * envelope).astype("float32")])
        try:
            sd.play(samples * amplitude * VOLUME / 0.35, rate)
            sd.wait()
        except Exception as error:  # pragma: no cover
            log.debug("Cue playback failed: %s", error)


def describe() -> str:
    if shutil.which("canberra-gtk-play"):
        return "freedesktop sound theme (libcanberra)"
    if _theme_file(RecordingSoundCue.STARTED) is not None:
        return "freedesktop sound theme"
    return f"synthesised tones on {environment.session_type()}"
