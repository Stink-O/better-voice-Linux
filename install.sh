#!/usr/bin/env bash
# Install BetterVoice for Linux into the current user's home.
#
# Creates a virtual environment, installs the app and its dependencies, and
# registers the desktop entry, icon and launcher so the desktop treats
# BetterVoice as a normal application -- which also lets the XDG portals
# identify it when it asks for global shortcuts and screen capture.
set -euo pipefail

APP_ID="io.github.taruntomar122.BetterVoice"
# systemd units for applications are named app-<AppID>, which is how the XDG
# portals map a running process back to its desktop entry.
SERVICE="app-$APP_ID.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
VENV="${BETTERVOICE_VENV:-$HOME/.local/share/BetterVoice/venv}"
BIN_DIR="$PREFIX/bin"
DESKTOP_DIR="$PREFIX/share/applications"
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
METAINFO_DIR="$PREFIX/share/metainfo"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$1" >&2; }

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "BetterVoice needs Python 3.11 or newer; found $("$PYTHON" --version)" >&2
    exit 1
fi

info "Creating the virtual environment in $VENV"
mkdir -p "$(dirname "$VENV")"
"$PYTHON" -m venv --upgrade-deps "$VENV" >/dev/null

info "Installing BetterVoice and its dependencies"
EXTRAS=""
case "${XDG_SESSION_TYPE:-}" in
    x11) EXTRAS="[x11]" ;;
esac
"$VENV/bin/pip" install --quiet --upgrade "$SOURCE_DIR$EXTRAS"

info "Installing the launcher, desktop entry and icon"
mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
cat > "$BIN_DIR/bettervoice" <<LAUNCHER
#!/usr/bin/env bash
# Launch BetterVoice inside its own systemd scope when possible, so the XDG
# portals can identify the application by its desktop entry.
exec_app() { exec "$VENV/bin/python" -m bettervoice "\$@"; }
if [ -n "\${BETTERVOICE_NO_SCOPE:-}" ] || ! command -v systemd-run >/dev/null 2>&1; then
    exec_app "\$@"
fi
if systemctl --user show-environment >/dev/null 2>&1; then
    exec systemd-run --user --scope --quiet --collect \\
        --unit="app-$APP_ID-\$\$" \\
        "$VENV/bin/python" -m bettervoice "\$@"
fi
exec_app "\$@"
LAUNCHER
chmod +x "$BIN_DIR/bettervoice"

install -m 644 "$SOURCE_DIR/bettervoice/resources/$APP_ID.desktop" "$DESKTOP_DIR/$APP_ID.desktop"
install -m 644 "$SOURCE_DIR/bettervoice/resources/$APP_ID.svg" "$ICON_DIR/$APP_ID.svg"
mkdir -p "$METAINFO_DIR"
install -m 644 "$SOURCE_DIR/bettervoice/resources/$APP_ID.metainfo.xml" "$METAINFO_DIR/$APP_ID.metainfo.xml"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$PREFIX/share/icons/hicolor" 2>/dev/null || true

info "Installing the optional autostart service"
mkdir -p "$UNIT_DIR"
install -m 644 "$SOURCE_DIR/bettervoice/resources/$SERVICE" "$UNIT_DIR/$SERVICE"
systemctl --user daemon-reload 2>/dev/null || true

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH; add it to run 'bettervoice' from a shell." ;;
esac

for tool in wtype wl-copy pactl; do
    command -v "$tool" >/dev/null || warn "'$tool' is not installed; run 'bettervoice --doctor' to see what it affects."
done

info "Done. Start it with:  bettervoice"
info "Check the setup with: bettervoice --doctor"
info "Start it at login:    systemctl --user enable --now $SERVICE"
