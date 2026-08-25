"""Running blocking work off the GUI thread.

Model loading, downloads and transcription all block for seconds at a time. Each
runs on Qt's thread pool and reports back on the GUI thread, so the overlay keeps
animating and the tray menu stays responsive throughout.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PyQt6 import QtCore

log = logging.getLogger(__name__)


class _Signals(QtCore.QObject):
    finished = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)


class _Task(QtCore.QRunnable):
    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__()
        self.signals = _Signals()
        self._work = work

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            result = self._work()
        except Exception as error:
            log.debug("Background task failed:\n%s", traceback.format_exc())
            self.signals.failed.emit(error)
        else:
            self.signals.finished.emit(result)


def run(
    work: Callable[[], Any],
    on_finished: Callable[[Any], None] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
) -> None:
    """Run ``work`` on the thread pool; callbacks fire on the GUI thread."""

    task = _Task(work)
    if on_finished is not None:
        task.signals.finished.connect(on_finished)
    if on_failed is not None:
        task.signals.failed.connect(on_failed)
    QtCore.QThreadPool.globalInstance().start(task)


class _Dispatcher(QtCore.QObject):
    """Hops a call onto the GUI thread.

    ``QTimer.singleShot`` posts to the *calling* thread's event loop, and a
    thread-pool worker has none, so callbacks made from one would simply be
    dropped. A queued signal connection is the mechanism that actually crosses
    threads.
    """

    invoke = QtCore.pyqtSignal(object, tuple)

    def __init__(self) -> None:
        super().__init__()
        application = QtCore.QCoreApplication.instance()
        if application is not None:
            self.moveToThread(application.thread())
        self.invoke.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot(object, tuple)
    def _run(self, callback: Callable[..., None], arguments: tuple) -> None:
        try:
            callback(*arguments)
        except Exception:
            log.warning("A GUI-thread callback failed:\n%s", traceback.format_exc())


_dispatcher: _Dispatcher | None = None


def _dispatcher_instance() -> _Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
    return _dispatcher


def on_main_thread(callback: Callable[..., None]) -> Callable[..., None]:
    """Wrap a callback so it always runs on the GUI thread, from any thread."""

    dispatcher = _dispatcher_instance()

    def _dispatch(*arguments: Any) -> None:
        dispatcher.invoke.emit(callback, arguments)

    return _dispatch
