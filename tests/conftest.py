"""Shared fixtures.

Qt needs one QApplication per process; tests that paint or ask about screens
share it. The offscreen platform keeps the suite headless-friendly.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6 import QtWidgets

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield application
