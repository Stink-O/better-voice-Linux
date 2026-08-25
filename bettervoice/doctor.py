"""``bettervoice --doctor``: which backend was chosen for each part of the system.

Linux desktops differ enough that "it does not work" is usually "this one piece
picked a different backend than you expected". This prints the whole picture in
one place, including what to install to improve any of it.
"""

from __future__ import annotations

from . import APP_NAME, __version__
from .asr import GrammarCorrector, LocalTranscriber
from .audio import MicrophoneManager
from .backends import (
    clipboard,
    environment,
    focus,
    hotkeys,
    pointer,
    screenshot,
    textinject,
)
from .backends.sounds import describe as describe_sounds
from .config import config
from .paths import sessions_dir


def _line(label: str, value: str, hint: str | None = None) -> str:
    text = f"  {label:<22} {value}"
    if hint:
        text += f"\n  {'':<22} → {hint}"
    return text


def report() -> str:
    settings = config()
    lines = [f"{APP_NAME} {__version__}", "", f"  {'session':<22} {environment.describe()}"]

    hotkey = hotkeys.create(settings.get("hotkeyBackend"))
    lines.append(_line("shortcuts", hotkey.name, hotkey.unavailable_reason))
    lines.append(_line("", hotkey.shortcut_hint))

    pointer_backend = pointer.create(settings.get("pointerBackend"))
    hint = pointer_backend.unavailable_reason
    if hint is None and not pointer_backend.accurate:
        hint = "position is estimated from raw device motion"
    lines.append(_line("pointer", pointer_backend.name, hint))

    capture = screenshot.create(settings.get("screenshotBackend"))
    lines.append(_line("screen capture", capture.name, capture.unavailable_reason))

    insertion = textinject.create(settings.get("textInsertionBackend"))
    lines.append(_line("transcript insertion", insertion.name, insertion.unavailable_reason))

    focus_backend = focus.create(settings.get("focusBackend"))
    lines.append(
        _line(
            "focus target",
            focus_backend.name,
            None
            if focus_backend.remembers_target
            else "the transcript goes wherever focus is when the model finishes",
        )
    )

    reason = clipboard.unavailable_reason(recheck=True)
    lines.append(
        _line(
            "clipboard",
            "wl-clipboard" if environment.is_wayland() else "Qt",
            reason
            or (
                None
                if clipboard.carries_images()
                else "transcript only, until transcript insertion is enabled"
            ),
        )
    )
    lines.append(_line("sound cues", describe_sounds()))

    microphones = MicrophoneManager()
    microphones.refresh()
    lines.append(
        _line(
            "microphone",
            microphones.selected_label,
            f"{len(microphones.devices)} input(s) via "
            f"{'PipeWire/PulseAudio' if microphones.uses_pipewire else 'PortAudio'}",
        )
    )

    transcriber = LocalTranscriber(
        model=settings.get("asrModel"), quantization=settings.get("asrQuantization")
    )
    lines.append(
        _line(
            "local model",
            transcriber.model_name,
            "downloaded" if transcriber.is_downloaded else f"not downloaded ({transcriber.directory})",
        )
    )
    lines.append(
        _line(
            "grammar model",
            "cached" if GrammarCorrector().is_cached() else "not downloaded",
        )
    )
    lines.append(_line("sessions", str(sessions_dir())))
    lines.append(_line("settings", str(settings.path)))

    lines += _suggestions()
    return "\n".join(lines)


def _suggestions() -> list[str]:
    """Only name packages that are genuinely absent and would actually help."""

    wanted: list[tuple[str, str]] = []
    if environment.is_wayland():
        if not environment.has("wl-copy"):
            wanted.append(
                ("wl-clipboard", "put the transcript on the clipboard without window focus")
            )
        if not (environment.has("wtype") or environment.has("ydotool")):
            wanted.append(("wtype", "paste the transcript into the focused field"))
    else:
        if not environment.has("xclip"):
            wanted.append(("xclip", "save and restore your clipboard around a quick note"))
        if not environment.has("xdotool"):
            wanted.append(("xdotool", "paste the transcript into the focused field"))
    if not environment.has("pactl"):
        wanted.append(("pipewire-utils", "list microphones by their friendly names"))

    if not wanted:
        return ["", "  Everything BetterVoice can use is installed."]

    width = max(len(name) for name, _ in wanted)
    return ["", "  Not installed, and would help:"] + [
        f"    {name:<{width}}  {why}" for name, why in wanted
    ]
