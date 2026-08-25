#!/usr/bin/env bash
# Remove the BetterVoice launcher, desktop entry, icon and service.
# Pass --purge to also delete downloaded models, settings and saved sessions.
set -euo pipefail

APP_ID="io.github.taruntomar122.BetterVoice"
SERVICE="app-$APP_ID.service"
PREFIX="${PREFIX:-$HOME/.local}"
VENV="${BETTERVOICE_VENV:-$HOME/.local/share/BetterVoice/venv}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }

systemctl --user disable --now "$SERVICE" 2>/dev/null || true
pkill -f "python -m bettervoice" 2>/dev/null || true

info "Removing the launcher and desktop integration"
rm -f "$PREFIX/bin/bettervoice"
rm -f "$PREFIX/share/applications/$APP_ID.desktop"
rm -f "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
rm -f "$PREFIX/share/metainfo/$APP_ID.metainfo.xml"
rm -f "$UNIT_DIR/$SERVICE"
rm -f "$UNIT_DIR/$APP_ID.service"  # unit name used before 0.1.0
rm -rf "$VENV"
command -v update-desktop-database >/dev/null && update-desktop-database "$PREFIX/share/applications" || true
systemctl --user daemon-reload 2>/dev/null || true

if [ "$PURGE" -eq 1 ]; then
    info "Removing models, settings and saved sessions"
    rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/BetterVoice"
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/BetterVoice"
    rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/BetterVoice"
    SESSIONS="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/BetterVoice"
    [ -d "$SESSIONS" ] && rm -rf "$SESSIONS"
    info "Your desktop may still list BetterVoice under keyboard shortcuts; remove it there."
else
    info "Kept models, settings and saved sessions. Re-run with --purge to remove them."
fi

info "Done."
