"""User-facing failures. Messages mirror the macOS build, retargeted to Linux."""

from __future__ import annotations


class BetterVoiceError(Exception):
    """Base class for every failure BetterVoice reports to the user."""


class MicrophoneUnavailable(BetterVoiceError):
    def __str__(self) -> str:
        return "No microphone input is available."


class MicrophoneRoutingFailed(BetterVoiceError):
    def __init__(self, name: str, detail: str) -> None:
        super().__init__(name, detail)
        self.name = name
        self.detail = detail

    def __str__(self) -> str:
        return f"Could not route audio from {self.name} ({self.detail})."


class LocalModelUnavailable(BetterVoiceError):
    def __str__(self) -> str:
        return "Download the local Parakeet model from the BetterVoice menu first."


class SessionUnavailable(BetterVoiceError):
    def __str__(self) -> str:
        return "The recording session is no longer available."


class SessionStorageFull(BetterVoiceError):
    def __str__(self) -> str:
        return "The 500 MB saved-session limit has been reached."


class ScreenPermissionRequired(BetterVoiceError):
    def __str__(self) -> str:
        return "Screen capture was declined for BetterVoice."


class ScreenshotUnavailable(BetterVoiceError):
    def __str__(self) -> str:
        return "The screen could not be captured."
