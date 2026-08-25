"""Linux system integration.

Every macOS framework the original app leaned on has a different shape here, and
often several competing implementations. Each module in this package exposes one
small interface and picks the best available backend for the running session
(Wayland vs X11, KDE vs GNOME vs wlroots), degrading instead of failing.
"""
