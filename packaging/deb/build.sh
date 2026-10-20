#!/bin/sh
# Builds dist/iconfetch_<version>_all.deb for Debian, Ubuntu and their relatives.
# Needs dpkg-deb. Usage: sh packaging/deb/build.sh
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/iconfetch/__init__.py")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"
umask 022

sh "$root/install.sh" --prefix /usr --destdir "$stage" --no-deps >/dev/null
mkdir -p "$stage/usr/share/doc/iconfetch" "$stage/DEBIAN"
cp "$root/LICENSE" "$stage/usr/share/doc/iconfetch/copyright"
size=$(du -sk "$stage/usr" | cut -f1)

cat >"$stage/DEBIAN/control" <<EOF
Package: iconfetch
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-gi, gir1.2-gtk-4.0 (>= 4.6), gir1.2-adw-1 (>= 1.1), gir1.2-gdkpixbuf-2.0, librsvg2-common, shared-mime-info
Installed-Size: $size
Maintainer: Oluwasegun Babalola <133790822+Officialbabs@users.noreply.github.com>
Homepage: https://github.com/Officialbabs/iconfetch
Description: give installed apps their real icons
 IconFetch lists installed apps with the icon each one shows now, finds its
 real icon online (Flathub, the Snap Store, the app's website, brand icons
 or the Papirus theme) and applies it at once. Every change can be undone.
EOF

mkdir -p "$root/dist"
out="$root/dist/iconfetch_${version}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$out" >/dev/null
echo "$out"
