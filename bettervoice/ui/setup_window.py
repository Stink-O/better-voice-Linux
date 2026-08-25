"""The "Getting Started with BetterVoice" window.

Laid out to match the macOS sheet in ``Sources/BetterVoice/SetupView.swift``:
the same heading and capture illustration, the same two key-cap shortcut guides,
the same checked setup rows with a "Set Up" button on whatever is not ready, the
same grammar row with its toggle, and the same footer.

The rows differ where Linux genuinely differs -- macOS permissions become portal
grants, and two rows exist that macOS had no need for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME
from .widgets import (
    CapturePreview,
    InfoIcon,
    KeyCap,
    Spinner,
    StatusIcon,
    secondary,
)

WINDOW_WIDTH = 720
PADDING = 28
SECTION_SPACING = 22


@dataclass
class SetupRowState:
    title: str
    detail: str = ""
    ready: bool = False
    busy: bool = False
    action_title: str | None = None
    action: Callable[[], None] | None = None


@dataclass
class SetupModel:
    """Everything the window shows, refreshed by the app controller."""

    quick_note_keys: str = "Alt"
    long_form_keys: str = "Super + Alt"
    shortcut_hint: str = ""
    environment: str = ""
    storage_note: str = ""
    rows: list[SetupRowState] = field(default_factory=list)

    microphone_ready: bool = False
    microphone_name: str = "Checking…"
    microphone_options: list[tuple[str, str]] = field(default_factory=list)
    selected_microphone: str = "automatic"
    microphone_selection_enabled: bool = True

    grammar_enabled: bool = False
    grammar_status: str = ""
    grammar_ready: bool = False
    grammar_busy: bool = False
    grammar_selection_enabled: bool = True

    choose_microphone: Callable[[str], None] = lambda _: None
    set_grammar_correction: Callable[[bool], None] = lambda _: None
    download_grammar_model: Callable[[], None] = lambda: None
    refresh: Callable[[], None] = lambda: None
    complete: Callable[[], None] = lambda: None


def _muted(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    palette = label.palette()
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, secondary(label))
    label.setPalette(palette)
    return label


def _medium(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    font = label.font()
    font.setWeight(QtGui.QFont.Weight.Medium)
    label.setFont(font)
    return label


def _callout(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    font = label.font()
    font.setPointSizeF(max(8.0, font.pointSizeF() - 0.5))
    label.setFont(font)
    return _muted(label)


def _wrapping(label: QtWidgets.QLabel) -> QtWidgets.QLabel:
    label.setWordWrap(True)
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    policy.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Minimum)
    label.setSizePolicy(policy)
    return label


class _ShortcutGuide(QtWidgets.QWidget):
    """A key cap beside its name and description."""

    def __init__(self, keys: str, title: str, detail: str) -> None:
        super().__init__()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self._cap = KeyCap(keys)
        layout.addWidget(self._cap, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_medium(QtWidgets.QLabel(title)))
        description = _wrapping(_muted(QtWidgets.QLabel(detail)))
        description.setMinimumWidth(0)
        text.addWidget(description)
        layout.addLayout(text, 1)

    def set_keys(self, keys: str) -> None:
        self._cap.setText(keys)


class _SetupRow(QtWidgets.QWidget):
    """Status icon, title and detail, with a Set Up button when it is not ready."""

    def __init__(self) -> None:
        super().__init__()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(12)

        self._icon = StatusIcon()
        layout.addWidget(self._icon, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        self._title = _medium(QtWidgets.QLabel())
        text.addWidget(self._title)
        self._detail = _wrapping(_callout(QtWidgets.QLabel()))
        text.addWidget(self._detail)
        layout.addLayout(text, 1)

        self._spinner = Spinner()
        self._spinner.setVisible(False)
        layout.addWidget(self._spinner, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        self._button = QtWidgets.QPushButton()
        layout.addWidget(self._button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        self._action: Callable[[], None] | None = None
        self._button.clicked.connect(self._run)

    def _run(self) -> None:
        if self._action is not None:
            self._action()

    def apply(self, state: SetupRowState) -> None:
        self._icon.set_ready(state.ready)
        self._title.setText(state.title)
        self._detail.setText(state.detail)
        self.setAccessibleName(f"{state.title}. {state.detail}")
        self._button.setAccessibleName(f"Set up {state.title}")
        self._spinner.setVisible(state.busy)
        self._action = state.action
        show_button = not state.ready and not state.busy and state.action is not None
        self._button.setVisible(show_button)
        if show_button:
            self._button.setText(state.action_title or "Set Up")


class _MicrophoneRow(QtWidgets.QWidget):
    """The microphone row, with the input picker on the right."""

    def __init__(self, model: SetupModel) -> None:
        super().__init__()
        self._model = model
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(12)

        self._icon = StatusIcon()
        layout.addWidget(self._icon, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_medium(QtWidgets.QLabel("Microphone")))
        self._detail = _callout(QtWidgets.QLabel())
        text.addWidget(self._detail)
        layout.addLayout(text, 1)

        self._picker = QtWidgets.QComboBox()
        self._picker.setAccessibleName("Microphone input")
        self._picker.setMaximumWidth(260)
        self._picker.setMinimumWidth(200)
        self._picker.activated.connect(self._on_chosen)
        layout.addWidget(self._picker, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._empty = _callout(QtWidgets.QLabel("No inputs found"))
        layout.addWidget(self._empty, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

    def _on_chosen(self, index: int) -> None:
        identifier = self._picker.itemData(index)
        if identifier:
            self._model.choose_microphone(identifier)

    def apply(self) -> None:
        model = self._model
        self._icon.set_ready(model.microphone_ready)
        detail = (
            model.microphone_name if model.microphone_ready else "Needed to record your voice"
        )
        self._detail.setText(detail)
        self.setAccessibleName(f"Microphone. {detail}")
        has_options = bool(model.microphone_options)
        self._picker.setVisible(has_options)
        self._empty.setVisible(not has_options)
        if not has_options:
            return
        blocked = self._picker.blockSignals(True)
        self._picker.clear()
        for identifier, name in model.microphone_options:
            self._picker.addItem(name, identifier)
        index = self._picker.findData(model.selected_microphone)
        self._picker.setCurrentIndex(max(0, index))
        self._picker.setEnabled(model.microphone_selection_enabled)
        self._picker.blockSignals(blocked)


class _GrammarRow(QtWidgets.QWidget):
    """The beta row: description, live status, a toggle and a download button."""

    DETAIL = (
        "t5-tiny-gec-hone runs locally after transcription to fix punctuation and "
        "sentence structure. It falls back to the raw transcript if unavailable."
    )

    def __init__(self, model: SetupModel) -> None:
        super().__init__()
        self._model = model
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(12)

        layout.addWidget(InfoIcon(), 0, QtCore.Qt.AlignmentFlag.AlignTop)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_medium(QtWidgets.QLabel("Grammar cleanup (Beta)")))
        text.addWidget(_wrapping(_callout(QtWidgets.QLabel(self.DETAIL))))
        self._status = _callout(QtWidgets.QLabel())
        text.addWidget(self._status)
        layout.addLayout(text, 1)

        side = QtWidgets.QVBoxLayout()
        side.setSpacing(7)
        side.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self._toggle = QtWidgets.QCheckBox()
        self._toggle.setAccessibleName("Enable grammar cleanup beta")
        self._toggle.toggled.connect(self._on_toggled)
        side.addWidget(self._toggle, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        self._spinner = Spinner()
        side.addWidget(self._spinner, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        self._download = QtWidgets.QPushButton("Download")
        self._download.setAccessibleName("Download the grammar model")
        self._download.clicked.connect(lambda: self._model.download_grammar_model())
        side.addWidget(self._download, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        layout.addLayout(side, 0)
        layout.setAlignment(side, QtCore.Qt.AlignmentFlag.AlignTop)

    def _on_toggled(self, enabled: bool) -> None:
        if not self._toggle.signalsBlocked():
            self._model.set_grammar_correction(enabled)

    def apply(self) -> None:
        model = self._model
        self._status.setText(model.grammar_status)
        blocked = self._toggle.blockSignals(True)
        self._toggle.setChecked(model.grammar_enabled)
        self._toggle.setEnabled(model.grammar_selection_enabled)
        self._toggle.blockSignals(blocked)
        self._spinner.setVisible(model.grammar_busy)
        self._download.setVisible(not model.grammar_ready and not model.grammar_busy)
        self._download.setEnabled(model.grammar_selection_enabled)


class SetupWindow(QtWidgets.QWidget):
    def __init__(self, model: SetupModel) -> None:
        super().__init__(None, QtCore.Qt.WindowType.Window)
        self._model = model
        self.setWindowTitle(f"Getting Started with {APP_NAME}")

        # Pin the content width. Every wrapping label then has one exact height,
        # so the window hugs its content instead of the layout handing spare
        # space to whichever section happens to be expandable.
        frame = QtWidgets.QVBoxLayout(self)
        frame.setContentsMargins(PADDING, PADDING, PADDING, PADDING)
        frame.setSpacing(0)
        content = QtWidgets.QWidget()
        content.setFixedWidth(WINDOW_WIDTH - PADDING * 2)
        frame.addWidget(content)
        frame.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)

        outer = QtWidgets.QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SECTION_SPACING)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_shortcuts())

        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        divider.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        outer.addWidget(divider)

        outer.addLayout(self._build_setup_section())
        outer.addLayout(self._build_footer())

    def _build_header(self) -> QtWidgets.QLayout:
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(20)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(9)
        left.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        title_row = QtWidgets.QHBoxLayout()
        title_row.setSpacing(9)
        mark = QtWidgets.QLabel()
        from . import icons

        # Follow the palette: the tray glyph is a mask tinted by the panel, but
        # in a window it has to pick its own colour or it disappears on a light
        # background.
        mark.setPixmap(
            icons.waveform_in_circle(
                28, color=self.palette().color(QtGui.QPalette.ColorRole.WindowText)
            ).pixmap(28, 28)
        )
        title_row.addWidget(mark, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        heading = QtWidgets.QLabel(APP_NAME)
        font = heading.font()
        font.setPointSizeF(font.pointSizeF() + 12)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        heading.setFont(font)
        title_row.addWidget(heading, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        left.addLayout(title_row)

        tagline = QtWidgets.QLabel("Talk. Point. Give your agent the whole thought.")
        tagline_font = tagline.font()
        tagline_font.setPointSizeF(tagline_font.pointSizeF() + 2)
        tagline.setFont(tagline_font)
        left.addWidget(tagline)

        left.addWidget(
            _wrapping(
                _muted(
                    QtWidgets.QLabel(
                        "Speak normally, circle anything important, and BetterVoice "
                        "keeps the words and full-screen visual context together."
                    )
                )
            )
        )
        header.addLayout(left, 1)
        header.addWidget(CapturePreview(), 0, QtCore.Qt.AlignmentFlag.AlignTop)
        return header

    def _build_shortcuts(self) -> QtWidgets.QLayout:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(28)
        self._quick_guide = _ShortcutGuide(
            self._model.quick_note_keys, "Quick note", "Hold to record. Release to finish."
        )
        self._long_guide = _ShortcutGuide(
            self._model.long_form_keys,
            "Long explanation",
            "Press once to start, again to finish.",
        )
        row.addWidget(self._quick_guide, 1)
        row.addWidget(self._long_guide, 1)
        return row

    def _build_setup_section(self) -> QtWidgets.QLayout:
        section = QtWidgets.QVBoxLayout()
        section.setSpacing(12)

        label = QtWidgets.QLabel("Setup")
        font = label.font()
        font.setWeight(QtGui.QFont.Weight.Bold)
        label.setFont(font)
        section.addWidget(label)

        self._microphone_row = _MicrophoneRow(self._model)
        section.addWidget(self._microphone_row)

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._rows_layout.setSpacing(8)
        section.addLayout(self._rows_layout)
        self._rows: list[_SetupRow] = []

        self._grammar_row = _GrammarRow(self._model)
        section.addWidget(self._grammar_row)
        return section

    def _build_footer(self) -> QtWidgets.QLayout:
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(10)
        self._storage = _callout(QtWidgets.QLabel())
        footer.addWidget(self._storage, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._model.refresh())
        footer.addWidget(refresh)
        done = QtWidgets.QPushButton("Done")
        done.setDefault(True)
        done.setAutoDefault(True)
        done.clicked.connect(lambda: self._model.complete())
        footer.addWidget(done)
        return footer

    def apply(self) -> None:
        model = self._model
        self._quick_guide.set_keys(model.quick_note_keys)
        self._long_guide.set_keys(model.long_form_keys)
        self._storage.setText(model.storage_note)
        self._storage.setToolTip(model.environment)
        self._microphone_row.apply()

        while len(self._rows) < len(model.rows):
            row = _SetupRow()
            self._rows.append(row)
            self._rows_layout.addWidget(row)
        for index, row in enumerate(self._rows):
            if index < len(model.rows):
                row.apply(model.rows[index])
                row.setVisible(True)
            else:
                row.setVisible(False)

        self._grammar_row.apply()
        self.adjustSize()

    def present(self) -> None:
        self.apply()
        self.show()
        self.raise_()
        self.activateWindow()
