#!/bin/sh
# Builds dist/iconfetch-<version>-1.noarch.rpm from dist/iconfetch-<version>.tar.gz.
# Runs on Fedora (CI: docker run -v "$PWD:/src" -w /src fedora sh packaging/rpm/build.sh).
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/iconfetch/__init__.py")
tarball="$root/dist/iconfetch-$version.tar.gz"
[ -f "$tarball" ] || { echo "missing $tarball (make it with git archive first)" >&2; exit 1; }

command -v rpmbuild >/dev/null || dnf install -y -q rpm-build >/dev/null
top=$(mktemp -d)
trap 'rm -rf "$top"' EXIT
mkdir -p "$top/SOURCES" "$top/SPECS"
cp "$tarball" "$top/SOURCES/"
sed "s/^Version: .*/Version:        $version/" "$root/packaging/rpm/iconfetch.spec" >"$top/SPECS/iconfetch.spec"
rpmbuild --quiet --define "_topdir $top" -bb "$top/SPECS/iconfetch.spec"
cp "$top"/RPMS/noarch/*.rpm "$root/dist/"
ls "$root"/dist/*.rpm
