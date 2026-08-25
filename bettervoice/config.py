"""Persistent preferences -- the Linux stand-in for macOS UserDefaults.

Stored as JSON under ``$XDG_CONFIG_HOME/BetterVoice/settings.json`` so the file
stays hand-editable, which is what a Linux user expects of an app's config.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from .paths import config_dir, ensure

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "selectedMicrophoneName": None,
    "grammarCorrectionEnabled": False,
    "completedOnboarding": False,
    "shortcutAssignmentPrompted": False,
    "pointerBackend": "auto",
    "focusBackend": "auto",
    "hotkeyBackend": "auto",
    "screenshotBackend": "auto",
    "textInsertionBackend": "auto",
    "silenceThreshold": 0.015,
    "soundCuesEnabled": True,
    "reduceMotion": "auto",
    "asrModel": "nemo-parakeet-tdt-0.6b-v2",
    "asrQuantization": "int8",
}


class Config:
    def __init__(self) -> None:
        self._path = config_dir() / "settings.json"
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            log.warning("Ignoring unreadable settings file %s: %s", self._path, error)
            return
        if isinstance(stored, dict):
            self._values.update(stored)

    def _save(self) -> None:
        ensure(self._path.parent)
        temporary = self._path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(self._values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(self._path)
        except OSError as error:
            log.warning("Could not save settings to %s: %s", self._path, error)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if self._values.get(key) == value:
                return
            self._values[key] = value
            self._save()

    def bool(self, key: str) -> bool:
        return bool(self.get(key))

    @property
    def path(self):
        return self._path


_config: Config | None = None


def config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
