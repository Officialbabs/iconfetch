#!/bin/sh
# Runs inside a fresh distribution container, as root, with the checkout at /src (read-only).
# Installs IconFetch the way a person would (the one-line installer, for commit $REF),
# then checks it against this distribution's Python, GTK and libadwaita.
# UI=1 also opens the window under Xvfb.
set -eu
export DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
step() { printf '\n=== %s\n' "$*"; }

# Minimal images lack what any desktop has: curl, certificates, find.
step "Base tools"
if command -v apt-get >/dev/null; then
    apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
elif command -v dnf >/dev/null; then
    dnf install -y -q curl tar gzip findutils >/dev/null
elif command -v zypper >/dev/null; then
    zypper -n -q install curl tar gzip findutils >/dev/null
elif command -v pacman >/dev/null; then
    pacman -Syu --noconfirm --needed curl tar findutils >/dev/null
elif command -v apk >/dev/null; then
    apk add -q curl tar findutils
elif command -v xbps-install >/dev/null; then
    xbps-install -Syu xbps >/dev/null && xbps-install -Syu curl tar findutils >/dev/null
fi
grep '^PRETTY_NAME=' /etc/os-release

step "One-line installer (commit $REF)"
sh -s -- --yes --ref "$REF" </src/install.sh
export PATH="$HOME/.local/bin:$PATH"
iconfetch --version

step "Unit tests on this distribution's libraries"
cd /src
python3 -m unittest discover -s tests

step "Window module loads"
PYTHONPATH="$HOME/.local/lib/iconfetch" python3 -c '
from iconfetch import ui
gtk, adw = ui.versions()
print("GTK %d.%d, libadwaita %d.%d" % (*gtk, *adw))
assert gtk >= ui.MIN_GTK and adw >= ui.MIN_ADW'

if [ "${UI:-0}" = 1 ]; then
    step "Window, under Xvfb"
    if command -v apt-get >/dev/null; then
        apt-get install -y -qq xvfb xauth dbus >/dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon which >/dev/null
    elif command -v pacman >/dev/null; then
        pacman -S --noconfirm --needed xorg-server-xvfb xorg-xauth dbus which >/dev/null
    fi
    export GSK_RENDERER=cairo NO_AT_BRIDGE=1
    dbus-run-session -- xvfb-run -a python3 tests/smoke_ui.py
    dbus-run-session -- xvfb-run -a python3 tests/smoke_ui.py --old-adw
fi

step "A real scan"
iconfetch list

step "Uninstall leaves nothing behind"
sh "$HOME/.local/lib/iconfetch/install.sh" --uninstall
for path in "$HOME/.local/bin/iconfetch" "$HOME/.local/lib/iconfetch" \
    "$HOME/.local/share/applications/io.iconfetch.IconFetch.desktop"; do
    if [ -e "$path" ]; then
        echo "still there: $path"
        exit 1
    fi
done
echo "passed"
