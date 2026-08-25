"""What the user is told once a recording has been delivered.

Ported from the reporting branches of ``AppController.finishSession``. It is the
last thing that happens after every recording, so it lives here as plain logic
rather than tangled into the controller: given what actually reached the user,
decide whether that is a status line or something that needs attention.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How long a transient status line stays before reverting to "Ready".
STATUS_RESET_SECONDS = 4.0


@dataclass(frozen=True)
class DeliveryReport:
    """Either a passing status line or a failure worth surfacing."""

    status: str | None = None
    error: str | None = None
    reset_after: float | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


def delivery_report(
    *,
    transcription_error: str | None,
    copy_to_clipboard: bool,
    clipboard_copied: bool,
    inserted: bool,
    had_transcript: bool,
    has_context: bool,
    context_on_clipboard: bool = False,
) -> DeliveryReport:
    """Describe how a finished session ended up.

    ``copy_to_clipboard`` marks a long explanation, which deliberately leaves the
    session on the clipboard; a quick note does not. ``context_on_clipboard``
    says whether the screenshots could ride along, which on Wayland depends on
    which clipboard backend is in use.
    """

    if transcription_error is not None:
        if not copy_to_clipboard:
            delivery = "Session saved."
        elif clipboard_copied:
            delivery = "Screen context copied." if has_context else "Session saved."
        else:
            delivery = "Session saved; clipboard text fallback used."
        return DeliveryReport(
            error=f"Transcription failed: {transcription_error} {delivery}"
        )

    if inserted:
        return DeliveryReport(
            status=(
                "Inserted transcript • context copied"
                if copy_to_clipboard and context_on_clipboard
                else "Inserted transcript"
            ),
            reset_after=STATUS_RESET_SECONDS,
        )

    if clipboard_copied:
        if had_transcript and context_on_clipboard:
            status = "Copied transcript + context"
        elif had_transcript:
            status = "Copied transcript"
        elif has_context:
            status = "No speech detected • context copied"
        else:
            status = "No speech detected • session saved"
        return DeliveryReport(status=status, reset_after=STATUS_RESET_SECONDS)

    if not copy_to_clipboard and not had_transcript:
        return DeliveryReport(
            status="Screen context saved" if has_context else "Session saved",
            reset_after=STATUS_RESET_SECONDS,
        )

    return DeliveryReport(
        error=(
            "Saved session; transcript was not inserted."
            if not copy_to_clipboard
            else "Saved session; plain-text clipboard fallback used."
        )
    )
