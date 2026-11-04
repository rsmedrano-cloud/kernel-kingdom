#!/bin/sh
# Arch: builds the package from this checkout as user $1 (makepkg refuses root)
# and writes dist/PKGBUILD + dist/.SRCINFO for the AUR, pointing at the release
# tarball of tag $2 (if given) with its real checksum.
set -eu
cd "$(dirname "$0")/../.."
U=${1:?usage: build.sh <user> [tag]}
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
W=$(mktemp -d)
tar --exclude=./.git --exclude=./dist --transform "s,^\.,kernel-kingdom-$V," -czf "$W/kernel-kingdom-$V.tar.gz" .
sed -e "s/^pkgver=.*/pkgver=$V/" packaging/arch/PKGBUILD > "$W/PKGBUILD.aur"
sed -e "s|::\$url/archive/refs/tags/v\$pkgver.tar.gz|::file://$W/kernel-kingdom-$V.tar.gz|" "$W/PKGBUILD.aur" > "$W/PKGBUILD"
chown -R "$U" "$W"
su "$U" -c "cd $W && makepkg -f --noconfirm"
mkdir -p dist
cp "$W"/kernel-kingdom-*.pkg.tar.zst dist/
if [ -n "${2:-}" ]; then
  SUM=$(curl -fsSL "https://github.com/rsmedrano-cloud/kernel-kingdom/archive/refs/tags/$2.tar.gz" | sha256sum | cut -d' ' -f1)
  sed -i "s/^sha256sums=.*/sha256sums=('$SUM')/" "$W/PKGBUILD.aur"
  cp "$W/PKGBUILD.aur" dist/PKGBUILD
  (cd "$W" && cp PKGBUILD.aur PKGBUILD && su "$U" -c "cd $W && makepkg --printsrcinfo" > "$OLDPWD/dist/.SRCINFO")
fi
rm -rf "$W"
ls -A dist
