#!/bin/sh
# IconFetch installer: launcher + menu entry. `./install.sh --uninstall` removes them.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON="$DATA/icons/hicolor/scalable/apps/io.iconfetch.IconFetch.svg"
ENTRY="$DATA/applications/io.iconfetch.IconFetch.desktop"

if [ "$1" = "--uninstall" ]; then
    "$HERE/bin/iconfetch" revert || true
    rm -f "$HOME/.local/bin/iconfetch" "$ENTRY" "$ICON"
    rm -rf "$DATA/iconfetch"
    echo "IconFetch removed, and every icon it changed is back to the original."
    exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null || {
    echo "IconFetch needs PyGObject with GTK 4 and libadwaita:"
    echo "  sudo apt-get install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    exit 1
}

echo "==> Installing launcher"
mkdir -p "$HOME/.local/bin" "$DATA/applications" "$(dirname "$ICON")"
ln -sf "$HERE/bin/iconfetch" "$HOME/.local/bin/iconfetch"
cp "$HERE/data/iconfetch.svg" "$ICON"
sed -e "s|@EXEC@|$HOME/.local/bin/iconfetch|" -e "s|@ICON@|$ICON|" "$HERE/iconfetch.desktop.in" > "$ENTRY"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true

echo "Done. Open IconFetch from your apps, or run 'iconfetch list' to see what it finds."
