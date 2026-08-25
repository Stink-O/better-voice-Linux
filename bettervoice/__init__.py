"""BetterVoice for Linux.

A Linux port of the BetterVoice macOS menu-bar dictation app: hold a modifier to
dictate, circle something on screen to attach it as visual context.
"""

__version__ = "0.1.0"
APP_NAME = "BetterVoice"
APP_ID = "io.github.taruntomar122.BetterVoice"


def configure_application(application) -> None:
    """Stamp the app's identity onto Qt.

    The desktop file name is how the XDG portals and the notification service
    recognise BetterVoice, and the application name is what the user sees on a
    notification -- so this has to happen for every entry point, not just the
    command line one.
    """

    from PyQt6 import QtWidgets

    QtWidgets.QApplication.setDesktopFileName(APP_ID)
    QtWidgets.QApplication.setApplicationName(APP_NAME)
    QtWidgets.QApplication.setApplicationDisplayName(APP_NAME)
    QtWidgets.QApplication.setApplicationVersion(__version__)
    QtWidgets.QApplication.setOrganizationName(APP_NAME)
    application.setQuitOnLastWindowClosed(False)
