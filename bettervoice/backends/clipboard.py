"""Clipboard handling, including the save/restore dance around a quick note.

Wayland only lets an application claim the clipboard when it holds a recent input
serial, and BetterVoice never has one -- it is a tray app driven by global
shortcuts. The ``wlr-data-control`` protocol exists for exactly this case, and
``wl-copy``/``wl-paste`` speak it, so on Wayland they do the reading and writing
and Qt is only the fallback.

That protocol serves one MIME type per selection, so by itself Wayland gets the
transcript (or the screenshot URIs when there is no transcript). When the user
has granted the remote-desktop session that BetterVoice also uses for pasting,
``org.freedesktop.portal.Clipboard`` can offer every format at once instead, and
that path is preferred. On X11 Qt can claim the selection unprompted and always
offers the full set: plain text, rich text with the screenshots inline, the file
URIs, and the first image.
"""

from __future__ import annotations

import html
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from . import environment

log = logging.getLogger(__name__)

_TEXT_TARGETS = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING")
_SKIPPED_TARGETS = ("TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS")

#: wl-copy forks and serves the selection until it is replaced; give it a moment
#: to take ownership before anything reads the clipboard back.
_SETTLE_SECONDS = 5


@dataclass(frozen=True)
class ClipboardSnapshot:
    """What the clipboard held before BetterVoice touched it."""

    contents: dict[str, bytes] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.contents

    def text(self) -> str:
        for target in _TEXT_TARGETS:
            if target in self.contents:
                return self.contents[target].decode("utf-8", errors="replace")
        return ""


def _clipboard() -> QtGui.QClipboard:
    return QtWidgets.QApplication.clipboard()


def _run(argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            argv, input=stdin, capture_output=True, timeout=_SETTLE_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.debug("%s failed: %s", argv[0], error)
        return None


#: Phrases wl-clipboard uses when the compositor has no data-control protocol.
#: Anything else it complains about (an empty clipboard, say) is not our problem.
_MISSING_PROTOCOL = ("data-control", "data_control", "not support", "protocol")

_probe_result: bool | None = None


def data_control_available(recheck: bool = False) -> bool:
    """Whether wl-clipboard can actually claim the selection here.

    The binary existing is not the question: ``wl-copy`` needs the compositor to
    implement ``wlr-data-control`` (or ``ext-data-control``), and some do not.
    ``wl-paste --list-types`` exercises the same protocol without touching the
    clipboard, so it is a safe thing to ask.
    """

    global _probe_result
    if _probe_result is not None and not recheck:
        return _probe_result

    if not environment.has("wl-paste"):
        _probe_result = False
        return _probe_result

    result = _run(["wl-paste", "--list-types"])
    if result is None:
        _probe_result = False
        return _probe_result
    if result.returncode == 0:
        _probe_result = True
        return _probe_result

    # A non-zero exit is usually just an empty clipboard; only a complaint about
    # the protocol itself means we cannot use this route.
    message = result.stderr.decode(errors="replace").lower()
    _probe_result = not any(phrase in message for phrase in _MISSING_PROTOCOL)
    return _probe_result


def _uses_wl_clipboard() -> bool:
    """Whether the wl-clipboard route is both present and actually usable."""

    return (
        environment.is_wayland()
        and environment.has("wl-copy")
        and data_control_available()
    )


def _uses_xclip() -> bool:
    return environment.is_x11() and environment.has("xclip")


# -- reading -------------------------------------------------------------


def _list_targets() -> list[str]:
    if _uses_wl_clipboard() and environment.has("wl-paste"):
        result = _run(["wl-paste", "--list-types"])
    elif _uses_xclip():
        result = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    else:
        return []
    if result is None or result.returncode != 0:
        return []
    return [
        target
        for target in result.stdout.decode(errors="replace").split()
        if not target.startswith(_SKIPPED_TARGETS)
    ]


def _read_target(mime: str) -> bytes | None:
    if _uses_wl_clipboard():
        result = _run(["wl-paste", "--no-newline", "--type", mime])
    elif _uses_xclip():
        result = _run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])
    else:
        return None
    if result is None or result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def snapshot() -> ClipboardSnapshot:
    """Best-effort capture of the current clipboard so it can be put back."""

    contents: dict[str, bytes] = {}
    for mime in _list_targets():
        payload = _read_target(mime)
        if payload is not None:
            contents[mime] = payload
    if contents:
        return ClipboardSnapshot(contents)

    text = _clipboard().text()
    return ClipboardSnapshot({"text/plain": text.encode("utf-8")} if text else {})


# -- writing -------------------------------------------------------------


def _wl_copy(payload: bytes | None, mime: str | None = None) -> bool:
    """Hand the selection to wl-copy, which forks and serves it until replaced.

    Its pipes must not be captured: the forked server inherits them and keeps
    them open for as long as it owns the clipboard, so anything waiting on them
    would wait for the next copy.
    """

    argv = ["wl-copy"]
    if mime is None:
        argv.append("--clear")
    else:
        argv += ["--type", mime]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        log.warning("wl-copy could not run: %s", error)
        return False
    try:
        if payload is not None and process.stdin is not None:
            process.stdin.write(payload)
            process.stdin.close()
    except OSError as error:
        log.warning("wl-copy would not take the selection: %s", error)
        return False
    # The parent forks immediately once it owns the selection; a non-zero exit
    # within that window is a real failure, and still running means success.
    try:
        return process.wait(timeout=1) == 0
    except subprocess.TimeoutExpired:
        return True


def _clear() -> bool:
    if _uses_wl_clipboard():
        return _wl_copy(None, None)
    _clipboard().clear()
    return True


def current_text() -> str:
    """What the clipboard holds right now, as far as we can tell."""

    payload = _read_target("text/plain")
    if payload is not None:
        return payload.decode("utf-8", errors="replace")
    return _clipboard().text()


def still_ours(expected: str) -> bool:
    """Whether the clipboard still holds what BetterVoice last put there.

    macOS compared the pasteboard's change count; there is no such counter here,
    so the check is on the content itself. Either way the point is the same:
    never put an old clipboard back over something the user copied in between.
    """

    return current_text().strip() == expected.strip()


def restore(previous: ClipboardSnapshot, only_if_holding: str | None = None) -> bool:
    """Put a snapshot back after a quick note has been pasted.

    ``only_if_holding`` is the text BetterVoice expects to still be there; if
    anything else has claimed the clipboard, the snapshot is dropped rather than
    overwriting the user's own copy.
    """

    if only_if_holding is not None and not still_ours(only_if_holding):
        log.debug("Something else claimed the clipboard; leaving it alone")
        return False

    if previous.is_empty:
        return _clear()

    if _uses_wl_clipboard():
        # One type per selection: prefer text, else whatever was richest.
        for mime in _TEXT_TARGETS:
            if mime in previous.contents:
                return _wl_copy(previous.contents[mime], "text/plain")
        mime, payload = next(iter(previous.contents.items()))
        return _wl_copy(payload, mime)

    data = QtCore.QMimeData()
    for mime, payload in previous.contents.items():
        if mime in _TEXT_TARGETS:
            data.setText(payload.decode("utf-8", errors="replace"))
        else:
            data.setData(mime, QtCore.QByteArray(payload))
    _clipboard().setMimeData(data)
    return True


def copy_text_only(transcript: str) -> bool:
    if not transcript:
        return False
    if _uses_wl_clipboard():
        return _wl_copy(transcript.encode("utf-8"), "text/plain")
    _clipboard().setText(transcript)
    return True


def _rich_payloads(transcript: str, images: list[Path]) -> dict[str, bytes]:
    """Every representation of a finished session, keyed by MIME type."""

    payloads: dict[str, bytes] = {}
    if transcript:
        payloads["text/plain;charset=utf-8"] = transcript.encode("utf-8")
        payloads["text/plain"] = transcript.encode("utf-8")
    if not images:
        return payloads

    payloads["text/uri-list"] = "".join(
        f"{QtCore.QUrl.fromLocalFile(str(path)).toString()}\r\n" for path in images
    ).encode("utf-8")
    payloads["text/html"] = _rich_html(transcript, images).encode("utf-8")
    try:
        payloads["image/png"] = images[0].read_bytes()
    except OSError as error:
        log.debug("Could not read %s for the clipboard: %s", images[0], error)
    return payloads


def _rich_html(transcript: str, images: list[Path]) -> str:
    parts = [f"<p>{html.escape(transcript).replace(chr(10), '<br>')}</p>"] if transcript else []
    for index, path in enumerate(images, start=1):
        url = html.escape(QtCore.QUrl.fromLocalFile(str(path)).toString())
        parts.append(f'<p><img src="{url}" alt="Context {index}"><br>Context {index}</p>')
    return "".join(parts)


def _image_payloads(images: list[Path]) -> dict[str, bytes]:
    """Just the screenshots, with no text alongside them.

    A selection carries one item and the receiving application chooses which of
    the offered types to take -- and given the choice, nearly everything takes
    the text. Offering only the pictures is what lets a paste be a picture.
    """

    payloads: dict[str, bytes] = {}
    if not images:
        return payloads
    payloads["text/uri-list"] = "".join(
        f"{QtCore.QUrl.fromLocalFile(str(path)).toString()}\r\n" for path in images
    ).encode("utf-8")
    try:
        payloads["image/png"] = images[0].read_bytes()
    except OSError as error:
        log.debug("Could not read %s for the clipboard: %s", images[0], error)
    return payloads


def copy_images_only(images: list[Path], provider: object | None = None) -> bool:
    """Put the screenshots on the clipboard with the transcript left off.

    Only the portal path can do this: `wl-copy` claims one format at a time, and
    a `QMimeData` without text still ends up offering text to some readers.
    False means the caller should not expect a paste to produce a picture.
    """

    payloads = _image_payloads(images)
    if not payloads or not _provider_serves_clipboard(provider):
        return False
    return provider.set_selection(payloads)


def copy(transcript: str, images: list[Path], provider: object | None = None) -> bool:
    """Offer the transcript and, where the platform allows, the screenshots too.

    ``provider`` is an optional granted remote-desktop session that can serve
    several MIME types at once; it is used in preference to the single-format
    ``wl-copy`` path.
    """

    if not transcript and not images:
        return False

    if images and _provider_serves_clipboard(provider):
        payloads = _rich_payloads(transcript, images)
        if payloads and provider.set_selection(payloads):
            return True

    if _uses_wl_clipboard():
        if transcript:
            return _wl_copy(transcript.encode("utf-8"), "text/plain")
        uris = "".join(f"{QtCore.QUrl.fromLocalFile(str(path)).toString()}\r\n" for path in images)
        return _wl_copy(uris.encode("utf-8"), "text/uri-list")

    data = QtCore.QMimeData()
    if transcript:
        data.setText(transcript)

    if images:
        data.setHtml(_rich_html(transcript, images))
        data.setUrls([QtCore.QUrl.fromLocalFile(str(image)) for image in images])

        first = QtGui.QImage(str(images[0]))
        if not first.isNull():
            data.setImageData(first)

    _clipboard().setMimeData(data)
    return True


def _provider_serves_clipboard(provider: object | None) -> bool:
    return bool(provider is not None and getattr(provider, "serves_clipboard", False))


def carries_images(provider: object | None = None) -> bool:
    """Whether a copy can include the screenshots as well as the transcript."""

    return _provider_serves_clipboard(provider) or not _uses_wl_clipboard()


def unavailable_reason(recheck: bool = False) -> str | None:
    if not environment.is_wayland():
        return None
    if not environment.has("wl-copy"):
        return (
            "Install wl-clipboard so BetterVoice can put the transcript on the "
            "clipboard; Wayland does not let a tray app claim it directly."
        )
    if not data_control_available(recheck):
        return (
            "Your compositor does not offer the data-control protocol that lets a "
            "background app set the clipboard. The transcript is still saved to "
            "the session folder, and transcript insertion can paste it directly."
        )
    return None
