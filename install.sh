#!/bin/sh
# IconFetch installer, for most Linux distributions.
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/iconfetch/main/install.sh | sh
#
# Installs what IconFetch needs (Python 3, PyGObject, GTK 4, libadwaita) with
# the system's package manager, asking first, then IconFetch for the current
# user: ~/.local/lib/iconfetch, ~/.local/bin/iconfetch and an app menu entry.
# It also keeps IconFetch's changes in step with app installs, updates and
# removals in the background (a systemd user service, or a sign-in entry).
# Run it again to update.
#
#   --uninstall     put back every icon IconFetch changed, then remove it
#   --yes           install system packages without asking
#   --no-deps       leave system packages alone
#   --ref REF       install this tag, branch or commit (default: the latest release)
#   --no-watch      no background refresh (IconFetch still catches up when opened)
#   --link          from a checkout: run the checkout itself, for development
#   --prefix DIR    install under DIR, e.g. /usr (packaging; names the icon by theme)
#   --destdir DIR   stage the files under DIR (packaging)
set -eu
umask 022  # files 644, folders 755, whoever runs it

REPO="Officialbabs/iconfetch"
APP_ID="io.iconfetch.IconFetch"

user=1 deps=1 yes=0 uninstall=0 link=0 watch=1 ref="" destdir="" prefix="${HOME:-}/.local"

say() { printf '%s\n' "$*"; }
die() { printf 'iconfetch: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) uninstall=1 ;;
        --yes | -y) yes=1 ;;
        --no-deps) deps=0 ;;
        --link) link=1 ;;
        --no-watch) watch=0 ;;
        --ref) [ $# -gt 1 ] || die "--ref needs a value"; ref="$2"; shift ;;
        --ref=*) ref="${1#*=}" ;;
        --prefix) [ $# -gt 1 ] || die "--prefix needs a value"; prefix="$2"; user=0; shift ;;
        --prefix=*) prefix="${1#*=}"; user=0 ;;
        --destdir) [ $# -gt 1 ] || die "--destdir needs a value"; destdir="$2"; shift ;;
        --destdir=*) destdir="${1#*=}" ;;
        -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" 2>/dev/null || say "See https://github.com/$REPO"; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ -n "$prefix" ] && [ "$prefix" != "/.local" ] || die "HOME isn't set; pass --prefix"
lib="$prefix/lib/iconfetch"
bin="$prefix/bin"
config="${XDG_CONFIG_HOME:-${HOME:-}/.config}"
if [ "$user" = 1 ]; then
    share="${XDG_DATA_HOME:-$prefix/share}"
else
    share="$prefix/share"
fi
D="$destdir"

# --- System packages ---------------------------------------------------------

# Python 3.9+ with PyGObject, GTK 4.6+, libadwaita 1.1+ and GdkPixbuf. Fails saying what's missing.
DEPS_CHECK='
import sys
if sys.version_info < (3, 9):
    sys.exit("Python %d.%d is older than 3.9" % sys.version_info[:2])
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, GdkPixbuf, Gtk
gtk = (Gtk.get_major_version(), Gtk.get_minor_version())
adw = (Adw.get_major_version(), Adw.get_minor_version())
if gtk < (4, 6):
    sys.exit("GTK %d.%d is older than 4.6" % gtk)
if adw < (1, 1):
    sys.exit("libadwaita %d.%d is older than 1.1" % adw)
'

deps_ok() {
    have python3 && python3 -c "$DEPS_CHECK" >/dev/null 2>&1
}

deps_problem() {
    if have python3; then
        python3 -c "$DEPS_CHECK" 2>&1 | tail -n 1
    else
        say "python3 isn't installed"
    fi
}

# The package manager, and what it calls Python 3, PyGObject, GTK 4, libadwaita,
# the SVG loader for GdkPixbuf and the MIME database.
package_manager() {
    for pm in apt-get dnf zypper pacman apk xbps-install emerge; do
        if have "$pm"; then
            say "$pm"
            return
        fi
    done
}

packages() {
    case "$1" in
        apt-get) say "python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0 librsvg2-common shared-mime-info" ;;
        dnf) say "python3 python3-gobject gobject-introspection gtk4 libadwaita gdk-pixbuf2 librsvg2 shared-mime-info" ;;
        zypper) say "python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 typelib-1_0-GdkPixbuf-2_0 gdk-pixbuf-loader-rsvg shared-mime-info" ;;
        pacman) say "python python-gobject gtk4 libadwaita gdk-pixbuf2 librsvg shared-mime-info" ;;
        apk) say "python3 py3-gobject3 gtk4.0 libadwaita gdk-pixbuf librsvg shared-mime-info" ;;
        xbps-install) say "python3 python3-gobject gtk4 libadwaita gdk-pixbuf librsvg shared-mime-info" ;;
        emerge) say "dev-python/pygobject gui-libs/gtk gui-libs/libadwaita x11-libs/gdk-pixbuf gnome-base/librsvg x11-misc/shared-mime-info" ;;
    esac
}

as_root() {
    # Package managers must not read the rest of a piped script as answers.
    if [ "$(id -u)" = 0 ]; then
        "$@" </dev/null
    elif have sudo; then
        sudo "$@" </dev/null
    elif have doas; then
        doas "$@" </dev/null
    else
        die "need root to install packages, and there's no sudo or doas. Run: $*"
    fi
}

confirm() {
    [ "$yes" = 1 ] && return 0
    if ! (: </dev/tty) 2>/dev/null; then
        die "$1 Re-run with --yes to allow it, or --no-deps to skip."
    fi
    printf '%s [Y/n] ' "$1" >/dev/tty
    read -r answer </dev/tty || answer=n
    case "$answer" in "" | [Yy]*) return 0 ;; *) return 1 ;; esac
}

install_deps() {
    if deps_ok; then
        return
    fi
    if [ -r /etc/os-release ] && grep -q '^ID=nixos' /etc/os-release; then
        die "on NixOS, run IconFetch in: nix-shell -p 'python3.withPackages (ps: [ ps.pygobject3 ])' gtk4 libadwaita librsvg gobject-introspection"
    fi
    pm=$(package_manager)
    [ -n "$pm" ] || die "$(deps_problem). No supported package manager found: install Python 3.9+, PyGObject, GTK 4.6+ and libadwaita 1.1+, then re-run with --no-deps."
    pkgs=$(packages "$pm")
    say "IconFetch needs: $pkgs"
    confirm "Install them with $pm?" || die "stopped. Install them yourself, then re-run with --no-deps."
    # $pkgs is a list of words on purpose.
    # shellcheck disable=SC2086
    case "$pm" in
        apt-get) as_root apt-get install -y $pkgs || { as_root apt-get update && as_root apt-get install -y $pkgs; } ;;
        dnf) as_root dnf install -y $pkgs ;;
        zypper) as_root zypper --non-interactive install $pkgs ;;
        pacman) as_root pacman -S --needed --noconfirm $pkgs ;;
        apk) as_root apk add $pkgs ;;
        xbps-install) as_root xbps-install -Sy $pkgs ;;
        emerge) as_root emerge --noreplace $pkgs ;;
    esac
    deps_ok || die "the packages are in, but IconFetch still can't run: $(deps_problem)"
}

# --- IconFetch's files -------------------------------------------------------

fetch() {
    if have curl; then
        curl -fsSL "$1"
    elif have wget; then
        wget -qO- "$1"
    else
        die "need curl or wget to download IconFetch"
    fi
}

# Where the source is: this checkout, or a download of the release (or --ref).
find_source() {
    case "$0" in
        */install.sh | install.sh)
            here=$(cd "$(dirname "$0")" && pwd)
            if [ -z "$ref" ] && [ -f "$here/iconfetch/__init__.py" ]; then
                src="$here"
                return
            fi
            ;;
    esac
    [ "$link" = 0 ] || die "--link needs a checkout: run ./install.sh from it"
    have tar || die "need tar to unpack IconFetch"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    if [ -n "$ref" ]; then
        url="https://github.com/$REPO/archive/$ref.tar.gz"
    else
        url="https://github.com/$REPO/releases/latest/download/iconfetch.tar.gz"
    fi
    say "Downloading IconFetch${ref:+ $ref}…"
    if ! fetch "$url" >"$tmp/src.tar.gz" 2>/dev/null; then
        [ -z "$ref" ] || die "couldn't download $url"
        fetch "https://github.com/$REPO/archive/refs/heads/main.tar.gz" >"$tmp/src.tar.gz" || die "couldn't download IconFetch"
    fi
    tar -xzf "$tmp/src.tar.gz" -C "$tmp"
    init=$(find "$tmp" -path '*/iconfetch/__init__.py' | head -n 1)
    [ -n "$init" ] || die "the download doesn't contain IconFetch"
    src=$(cd "$(dirname "$init")/.." && pwd)
}

refresh_caches() {
    [ -z "$D" ] || return 0
    if have update-desktop-database; then update-desktop-database -q "$share/applications" 2>/dev/null || true; fi
    if have gtk-update-icon-cache && [ -d "$share/icons/hicolor" ]; then
        gtk-update-icon-cache -q -t -f "$share/icons/hicolor" 2>/dev/null || true
    fi
}

install_files() {
    version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$src/iconfetch/__init__.py")
    mkdir -p "$D$bin" "$D$share/applications" "$D$share/icons/hicolor/scalable/apps" "$D$share/metainfo"
    # Never write through an older install's symlink into a checkout.
    rm -f "$D$bin/iconfetch"
    rm -rf "$D$lib"
    if [ "$link" = 1 ]; then
        ln -s "$src/bin/iconfetch" "$D$bin/iconfetch"
    else
        mkdir -p "$D$lib/iconfetch"
        cp "$src"/iconfetch/*.py "$D$lib/iconfetch/"
        [ "$user" = 0 ] || cp "$src/install.sh" "$D$lib/install.sh"
        cat >"$D$bin/iconfetch" <<EOF
#!/bin/sh
PYTHONPATH="$lib\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m iconfetch "\$@"
EOF
        chmod 755 "$D$bin/iconfetch"
    fi

    icon="$share/icons/hicolor/scalable/apps/$APP_ID.svg"
    cp "$src/data/$APP_ID.svg" "$D$icon"
    cp "$src/data/$APP_ID.metainfo.xml" "$D$share/metainfo/$APP_ID.metainfo.xml"
    if [ "$user" = 1 ]; then
        # Full paths: a session may not have ~/.local/bin on PATH, or look for icons in ~/.local.
        exec_line="$bin/iconfetch" icon_line="$icon"
    else
        exec_line="iconfetch" icon_line="$APP_ID"
    fi
    sed -e "s|@EXEC@|$exec_line|" -e "s|@ICON@|$icon_line|" "$src/data/$APP_ID.desktop.in" \
        >"$D$share/applications/$APP_ID.desktop"
    refresh_caches
}

user_systemd() {
    have systemctl && systemctl --user show-environment >/dev/null 2>&1
}

# Keeps IconFetch's copies in step with app installs, updates and removals
# without it being opened: a systemd user service run by changes to the app
# folders and at sign-in, or else a sign-in entry.
install_refresh() {
    service=$(sed "s|@EXEC@|$bin/iconfetch|" "$src/data/iconfetch-refresh.service.in")
    autostart=$(sed -e "s|@EXEC@|$bin/iconfetch|" -e "s|@ICON@|$APP_ID|" "$src/data/$APP_ID.Refresh.desktop.in")
    if [ "$user" = 0 ]; then
        # Packages ship both; a person turns on the instant one with systemctl --user enable --now.
        mkdir -p "$D$prefix/lib/systemd/user" "$D/etc/xdg/autostart"
        printf '%s\n' "$service" >"$D$prefix/lib/systemd/user/iconfetch-refresh.service"
        cp "$src/data/iconfetch-refresh.path" "$D$prefix/lib/systemd/user/"
        printf '%s\n' "$autostart" >"$D/etc/xdg/autostart/$APP_ID.Refresh.desktop"
        return
    fi
    [ "$watch" = 1 ] && [ -z "$D" ] || return 0
    if user_systemd; then
        mkdir -p "$config/systemd/user"
        printf '%s\n' "$service" >"$config/systemd/user/iconfetch-refresh.service"
        path_unit >"$config/systemd/user/iconfetch-refresh.path"
        rm -f "$config/autostart/$APP_ID.Refresh.desktop"
        systemctl --user daemon-reload
        if systemctl --user enable iconfetch-refresh.service >/dev/null 2>&1; then
            watching="each time you sign in"
        fi
        if systemctl --user enable --now iconfetch-refresh.path >/dev/null 2>&1; then
            watching="as soon as apps are installed, updated or removed"
        elif [ "$(systemctl --user show -p Result --value iconfetch-refresh.path 2>/dev/null)" = resources ]; then
            limit=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo "?")
            say "The instant refresh couldn't start: this session has used up its file watches"
            say "(fs.inotify.max_user_instances is $limit). To raise the limit, then start it:"
            say "  echo 'fs.inotify.max_user_instances = 1024' | sudo tee /etc/sysctl.d/60-inotify.conf"
            say "  sudo sysctl --system"
            say "  systemctl --user restart iconfetch-refresh.path"
        else
            say "The instant refresh couldn't start; see: systemctl --user status iconfetch-refresh.path"
        fi
    else
        mkdir -p "$config/autostart"
        printf '%s\n' "$autostart" >"$config/autostart/$APP_ID.Refresh.desktop"
        watching="each time you sign in"
    fi
}

# The watcher for this computer: only the app folders it has, since each one
# costs an inotify instance and sessions can run short of those. A folder that
# appears later is picked up the next time this runs (updates do).
path_unit() {
    while IFS= read -r line; do
        case "$line" in
            PathChanged=*)
                dir=$(printf '%s' "${line#PathChanged=}" | sed "s|%h|${HOME:-}|")
                [ -d "$dir" ] || [ "$dir" = /usr/share/applications ] || continue
                ;;
        esac
        printf '%s\n' "$line"
    done <"$src/data/iconfetch-refresh.path"
}

uninstall_refresh() {
    [ "$watch" = 1 ] || return 0
    if [ -f "$config/systemd/user/iconfetch-refresh.path" ] && user_systemd; then
        systemctl --user disable --now iconfetch-refresh.path iconfetch-refresh.service >/dev/null 2>&1 || true
    fi
    rm -f "$config/systemd/user/iconfetch-refresh.service" "$config/systemd/user/iconfetch-refresh.path" \
        "$config/autostart/$APP_ID.Refresh.desktop"
    if user_systemd; then systemctl --user daemon-reload || true; fi
}

uninstall_files() {
    # Stop the background refresh first, so it can't act while icons are being put back.
    uninstall_refresh
    if [ -x "$bin/iconfetch" ]; then
        if "$bin/iconfetch" revert; then
            # Only now: the icons it applied live here until they're reverted.
            rm -rf "${XDG_DATA_HOME:-${HOME:-}/.local/share}/iconfetch"
        else
            say "Couldn't put every icon back; keeping IconFetch's icons so those apps still show one."
        fi
    fi
    rm -f "$bin/iconfetch" "$share/applications/$APP_ID.desktop" \
        "$share/icons/hicolor/scalable/apps/$APP_ID.svg" "$share/metainfo/$APP_ID.metainfo.xml"
    rm -rf "$lib"
    refresh_caches
    say "IconFetch is removed."
}

# --- Run ---------------------------------------------------------------------

if [ "$uninstall" = 1 ]; then
    uninstall_files
    exit 0
fi

[ "$deps" = 0 ] || [ -n "$D" ] || install_deps
find_source
install_files
watching=""
install_refresh

if [ -n "$D" ]; then
    say "IconFetch $version staged in $D$prefix."
    exit 0
fi
say "IconFetch $version is installed. Open it from your apps, or run: iconfetch"
[ -z "$watching" ] || say "It keeps its icons in step with your apps $watching."
case ":${PATH:-}:" in
    *":$bin:"*) ;;
    *) say "($bin isn't on your PATH yet; sign out and back in, or run $bin/iconfetch)" ;;
esac
