"""Reading the kernel's own input-device table.

``evdev.list_devices()`` reports only the devices the current user can already
open, so it cannot answer the question that actually matters: *is there a device
I am missing?* Watching some keyboards but not others looks like it works right
up until you type on the wrong one, so the honest source is
``/proc/bus/input/devices``, which is world-readable and lists everything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEVICE_TABLE = "/proc/bus/input/devices"


@dataclass(frozen=True)
class InputDevice:
    name: str
    handlers: frozenset[str]
    node: str

    @property
    def is_keyboard(self) -> bool:
        """A keyboard you type on.

        Power buttons and a mouse's consumer-control endpoint also claim ``kbd``;
        only a real keyboard has LEDs to drive.
        """

        return "kbd" in self.handlers and "leds" in self.handlers

    @property
    def is_pointer(self) -> bool:
        return any(handler.startswith("mouse") for handler in self.handlers)

    @property
    def is_readable(self) -> bool:
        return os.access(self.node, os.R_OK)


def _table_path() -> Path:
    return Path(os.environ.get("BETTERVOICE_INPUT_DEVICE_TABLE", DEVICE_TABLE))


def all_devices() -> list[InputDevice]:
    try:
        table = _table_path().read_text(encoding="utf-8")
    except OSError:
        return []

    devices = []
    for block in table.split("\n\n"):
        name = ""
        handlers: set[str] = set()
        for line in block.splitlines():
            if line.startswith('N: Name="'):
                name = line.partition("=")[2].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = set(line.partition("=")[2].split())
        node = next((token for token in handlers if token.startswith("event")), "")
        if node:
            devices.append(
                InputDevice(name=name, handlers=frozenset(handlers), node=f"/dev/input/{node}")
            )
    return devices


def keyboards() -> list[InputDevice]:
    return [device for device in all_devices() if device.is_keyboard]


def pointers() -> list[InputDevice]:
    return [device for device in all_devices() if device.is_pointer]


def readable(devices: list[InputDevice]) -> list[str]:
    return [device.node for device in devices if device.is_readable]


def coverage(devices: list[InputDevice]) -> tuple[list[str], int]:
    """The nodes we can open, and how many exist in total."""

    return readable(devices), len(devices)


def partial_access_warning(kind: str, devices: list[InputDevice]) -> str | None:
    """Explain when only some devices of a kind can be read."""

    nodes, total = coverage(devices)
    if not nodes:
        return (
            f"No {kind} is readable. Add your user to the 'input' group, then "
            "log out and back in."
        )
    if total > len(nodes):
        return (
            f"Only {len(nodes)} of {total} {kind}s can be read, so BetterVoice "
            f"will miss the others. Add your user to the 'input' group."
        )
    return None
