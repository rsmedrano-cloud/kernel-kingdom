#!/bin/sh
# Builds dist/kernel-kingdom_<version>_all.deb from this checkout.
# Pure Python, a single package: no debhelper needed, just dpkg-deb.
set -eu
cd "$(dirname "$0")/../.."
V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
PKG=$(mktemp -d)/kernel-kingdom
SITE=$PKG/usr/lib/python3/dist-packages

install -d "$PKG/DEBIAN" "$SITE" "$PKG/usr/bin" "$PKG/usr/share/applications" \
           "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/doc/kernel-kingdom"
cp -r reino "$SITE/"
find "$SITE" -name __pycache__ -prune -exec rm -rf {} +
echo deb > "$SITE/reino/PACKAGED"                 # the in-app updater leaves it to apt
printf '#!/usr/bin/python3\nimport sys\nfrom reino.cli import main\nsys.exit(main())\n' > "$PKG/usr/bin/kernel-kingdom"
chmod 755 "$PKG/usr/bin/kernel-kingdom"
install -m644 packaging/dev.kernelkingdom.Window.desktop "$PKG/usr/share/applications/"
install -m644 assets/kernel-kingdom.png "$PKG/usr/share/icons/hicolor/256x256/apps/"
install -m644 LICENSE "$PKG/usr/share/doc/kernel-kingdom/copyright"
install -m644 README.md "$PKG/usr/share/doc/kernel-kingdom/"

cat > "$PKG/DEBIAN/control" <<CONTROL
Package: kernel-kingdom
Version: $V
Architecture: all
Section: games
Priority: optional
Maintainer: rsmedrano-cloud <143425037+rsmedrano-cloud@users.noreply.github.com>
Depends: python3 (>= 3.11), python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0, python3-psutil
Recommends: gir1.2-gtk4layershell-1.0, python3-pil
Suggests: ffmpeg
Homepage: https://github.com/rsmedrano-cloud/kernel-kingdom
Description: your computer as a pixel-art kingdom (live wallpaper / idle game)
 A pixel-art isometric world that grows from your system: processes become
 realms, CPU brings storms, RAM is land, containers are fortresses. Runs as a
 Wayland live wallpaper (gtk4-layer-shell) or as a window, keeps a daily
 chronicle, and can show the machines of your homelab as islands.
CONTROL
SIZE=$(du -sk "$PKG/usr" | cut -f1)
echo "Installed-Size: $SIZE" >> "$PKG/DEBIAN/control"

mkdir -p dist
dpkg-deb --root-owner-group -Zxz --build "$PKG" "dist/kernel-kingdom_${V}_all.deb"
rm -rf "$(dirname "$PKG")"
echo "dist/kernel-kingdom_${V}_all.deb"
