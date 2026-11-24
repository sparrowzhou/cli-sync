#!/usr/bin/env bash
# Add a Cli Sync launcher to Linux desktop and application menus.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
[ "$(uname)" = "Linux" ] || {
  printf '%s\n' "This script installs a Linux desktop launcher. On macOS, open sync-menu.command." >&2
  exit 1
}
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
F="$APPS/sftp-sync.desktop"
mkdir -p "$APPS"
cat > "$F" <<DESK
[Desktop Entry]
Type=Application
Name=Cli Sync
Comment=Sync local folders to a server
Exec=bash "$DIR/menu.sh"
Path=$DIR
Terminal=true
Icon=folder-remote
Categories=Utility;
DESK
chmod +x "$F"
# Remove launchers created by older versions.
rm -f "$APPS"/sftp-sync-[1-4].desktop "$DESKTOP"/sftp-sync-[1-4].desktop
if [ -d "$DESKTOP" ]; then
  cp "$F" "$DESKTOP/"
  chmod +x "$DESKTOP/sftp-sync.desktop"
  # Allow GNOME to launch the desktop entry.
  command -v gio >/dev/null && gio set "$DESKTOP/sftp-sync.desktop" metadata::trusted true 2>/dev/null || true
fi
printf '%s\n' "Added the Cli Sync launcher to your application menu and desktop, when available."
printf '%s\n' "If the desktop launcher does not open, right-click it and choose Allow Launching."
