"""Microphone enumeration -- the PipeWire/PulseAudio answer to CoreAudio.

``pactl`` is the source of truth when it is present: it gives friendly
descriptions, tells monitors apart from real inputs, and reports the bus a
device sits on, which is how "prefer a connected external microphone" is decided.
PortAudio's own device list is the fallback for a bare ALSA box.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

from ..config import config

log = logging.getLogger(__name__)

_EXTERNAL_BUSES = {"usb", "bluetooth", "firewire", "thunderbolt", "pci"}
_SELECTED_KEY = "selectedMicrophoneName"


@dataclass(frozen=True)
class MicrophoneDevice:
    """One recordable input.

    ``id`` is the PipeWire/PulseAudio source name, which is stable across
    reboots -- the equivalent of the CoreAudio device UID.
    """

    id: str
    name: str
    is_external: bool
    portaudio_index: int | None = None


def _pactl(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["pactl", *arguments], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.debug("pactl %s failed: %s", " ".join(arguments), error)
        return None
    return result.stdout if result.returncode == 0 else None


def _pactl_sources() -> list[MicrophoneDevice]:
    raw = _pactl("-f", "json", "list", "sources")
    if raw is None:
        return []
    try:
        sources = json.loads(raw)
    except ValueError:
        return []

    devices = []
    for source in sources:
        properties = source.get("properties", {}) or {}
        if properties.get("media.class") == "Audio/Sink":
            continue
        name = source.get("name", "")
        if not name or name.endswith(".monitor"):
            continue
        bus = (properties.get("device.bus") or "").lower()
        devices.append(
            MicrophoneDevice(
                id=name,
                name=source.get("description") or name,
                is_external=bus in _EXTERNAL_BUSES,
            )
        )
    return sorted(devices, key=lambda device: device.name.casefold())


def _portaudio_sources() -> list[MicrophoneDevice]:
    try:
        import sounddevice as sd
    except Exception as error:  # pragma: no cover - audio stack missing
        log.warning("PortAudio is unavailable: %s", error)
        return []

    devices = []
    try:
        listed = sd.query_devices()
    except Exception as error:  # pragma: no cover
        log.warning("Could not list audio devices: %s", error)
        return []
    for index, device in enumerate(listed):
        if device.get("max_input_channels", 0) <= 0:
            continue
        name = device.get("name", f"Input {index}")
        if name in {"default", "sysdefault", "pipewire", "pulse"}:
            continue
        devices.append(
            MicrophoneDevice(
                id=f"portaudio:{index}",
                name=name,
                is_external="usb" in name.lower() or "bluetooth" in name.lower(),
                portaudio_index=index,
            )
        )
    return devices


def default_source_name() -> str | None:
    raw = _pactl("get-default-source")
    if raw is None:
        return None
    name = raw.strip()
    return name or None


class MicrophoneManager:
    """Mirrors the macOS ``MicrophoneManager``: automatic pick plus an override."""

    def __init__(self) -> None:
        self.devices: list[MicrophoneDevice] = []
        self._uses_pactl = True

    def refresh(self) -> None:
        devices = _pactl_sources()
        self._uses_pactl = bool(devices)
        if not devices:
            devices = _portaudio_sources()
        self.devices = devices
        selected = self.selected_id
        if selected is not None and not any(device.id == selected for device in devices):
            config().set(_SELECTED_KEY, None)

    @property
    def uses_pipewire(self) -> bool:
        return self._uses_pactl

    @property
    def selected_id(self) -> str | None:
        value = config().get(_SELECTED_KEY)
        return value if isinstance(value, str) and value else None

    def select(self, device_id: str | None) -> None:
        config().set(_SELECTED_KEY, device_id)

    @property
    def selected_device(self) -> MicrophoneDevice | None:
        selected = self.selected_id
        if selected is None:
            return None
        return next((device for device in self.devices if device.id == selected), None)

    @property
    def automatic_device(self) -> MicrophoneDevice | None:
        """Prefer a connected external input, then the system default."""

        default_name = default_source_name()
        candidates = [
            lambda device: device.id == default_name and device.is_external,
            lambda device: device.is_external,
            lambda device: device.id == default_name,
        ]
        for predicate in candidates:
            match = next((device for device in self.devices if predicate(device)), None)
            if match is not None:
                return match
        return self.devices[0] if self.devices else None

    @property
    def recording_device(self) -> MicrophoneDevice | None:
        return self.selected_device or self.automatic_device

    @property
    def selected_label(self) -> str:
        device = self.selected_device
        if device is not None:
            return device.name
        automatic = self.automatic_device
        return f"Automatic — {automatic.name if automatic else 'Unavailable'}"
