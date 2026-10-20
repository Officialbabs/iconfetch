#!/bin/sh
# Builds dist/iconfetch-<version>-1-any.pkg.tar.zst and dist/PKGBUILD (with the source's checksum).
# Runs on Arch as root (CI: docker run -v "$PWD:/src" -w /src archlinux sh packaging/arch/build.sh).
# SOURCE_URL: download the source from there (a release's GitHub archive);
# without it, dist/iconfetch-<version>.tar.gz is used.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/iconfetch/__init__.py")

pacman -Syu --noconfirm --needed base-devel >/dev/null
id builder >/dev/null 2>&1 || useradd -m builder
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

source="$work/iconfetch-$version.tar.gz"
if [ -n "${SOURCE_URL:-}" ]; then
    curl -fsSL "$SOURCE_URL" -o "$source"
else
    cp "$root/dist/iconfetch-$version.tar.gz" "$source"
fi
sum=$(sha256sum "$source" | cut -d' ' -f1)
sed -e "s/^pkgver=.*/pkgver=$version/" \
    -e "s/^sha256sums=.*/sha256sums=('$sum')/" \
    -e '/^# Each release.s PKGBUILD/d' \
    "$root/packaging/arch/PKGBUILD" >"$work/PKGBUILD"
chown -R builder "$work"
su builder -c "cd '$work' && makepkg --nodeps --noconfirm" >/dev/null

mkdir -p "$root/dist"
cp "$work/PKGBUILD" "$root/dist/PKGBUILD"
cp "$work"/iconfetch-*-any.pkg.tar.zst "$root/dist/"
ls "$root"/dist/*.pkg.tar.zst
