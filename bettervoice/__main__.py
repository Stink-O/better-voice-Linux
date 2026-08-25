"""Entry point: ``python -m bettervoice`` or the ``bettervoice`` command."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

from . import APP_NAME, __version__, configure_application
from .config import config
from .paths import cache_dir, config_dir, data_dir, ensure, sessions_dir

log = logging.getLogger(__name__)


def _lock_path():
    return ensure(cache_dir()) / "bettervoice.lock"


def _single_instance_lock():
    """Refuse to start twice; two instances would fight over the shortcuts."""

    import fcntl

    # Deliberately not a context manager and not Path.open(): the handle has to
    # outlive this function, because closing it releases the flock and would let
    # a second instance start.
    handle = open(  # noqa: SIM115, PTH123
        _lock_path(), "r+" if _lock_path().exists() else "w"
    )
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _running_instance() -> int | None:
    """The PID recorded in the lock file, if that process is still alive."""

    try:
        pid = int(_lock_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def _show_running_instance() -> bool:
    """Ask the instance that already holds the lock to come forward.

    Launching an app that is already running should surface it, the way
    activating a macOS app does -- not fail silently from a desktop icon.
    """

    pid = _running_instance()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


def _install_signal_handlers(application: QtWidgets.QApplication, controller) -> None:
    def _quit(*_args) -> None:
        controller.shutdown()
        application.quit()

    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, _quit)

    # A second launch signals us instead of starting a rival instance.
    signal.signal(
        signal.SIGUSR1,
        lambda *_args: QtCore.QTimer.singleShot(0, controller.show_setup),
    )
    # Let the Python interpreter run often enough to notice the signal.
    timer = QtCore.QTimer(application)
    timer.setInterval(400)
    timer.timeout.connect(lambda: None)
    timer.start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bettervoice",
        description="Voice dictation with the screen context you point at.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="log what each backend is doing"
    )
    parser.add_argument(
        "--paths", action="store_true", help="print where settings, models and sessions live"
    )
    parser.add_argument(
        "--install-desktop-entry",
        action="store_true",
        help=(
            "write the desktop entry, icon and metainfo into ~/.local/share so the "
            "XDG portals can identify BetterVoice (the install script does this for you)"
        ),
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="report which backend was picked for each part of the system",
    )
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if arguments.install_desktop_entry:
        from .resources import install

        prefix = Path(os.environ.get("PREFIX") or Path.home() / ".local")
        for path in install(prefix):
            print(f"wrote {path}")
        print(
            "\nLog out and back in, or run 'update-desktop-database "
            f"{prefix / 'share' / 'applications'}', for your desktop to notice."
        )
        return 0

    if arguments.paths:
        print(f"settings  {config().path}")
        print(f"config    {config_dir()}")
        print(f"models    {data_dir()}")
        print(f"cache     {cache_dir()}")
        print(f"sessions  {sessions_dir()}")
        return 0

    application = QtWidgets.QApplication(sys.argv[:1])
    configure_application(application)

    if arguments.doctor:
        from .doctor import report

        print(report())
        return 0

    lock = _single_instance_lock()
    if lock is None:
        if _show_running_instance():
            print(f"{APP_NAME} is already running; bringing it forward.")
            return 0
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 1

    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        log.warning(
            "No system tray is available; BetterVoice will run without a tray icon."
        )

    from .app import AppController

    controller = AppController(application)
    controller.start()
    _install_signal_handlers(application, controller)

    application.aboutToQuit.connect(controller.shutdown)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
