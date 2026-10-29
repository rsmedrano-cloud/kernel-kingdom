#!/usr/bin/env bash
# kernel-kingdom installer.
#   curl -fsSL https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/install.sh | bash
# Installs into ~/.local/share/kernel-kingdom/venv (uses the system's GTK/PyGObject),
# links ~/.local/bin/kernel-kingdom and enables the systemd --user services.
# Options (env vars): KERNEL_KINGDOM_REPO=<git url>  KERNEL_KINGDOM_NO_SERVICE=1
set -euo pipefail

REPO="${KERNEL_KINGDOM_REPO:-https://github.com/rsmedrano-cloud/kernel-kingdom}"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/kernel-kingdom"
BIN="$HOME/.local/bin"

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"
python3 - <<'PY' || {
import sys
assert sys.version_info >= (3, 11), "Python 3.11+ required"
import gi, cairo
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
PY
  say "Missing system packages (GTK 4, gtk4-layer-shell, PyGObject). Install them with:"
  if [ -e /run/ostree-booted ]; then echo "  rpm-ostree install python3-gobject gtk4 gtk4-layer-shell   # then reboot"
  elif command -v dnf >/dev/null; then echo "  sudo dnf install python3-gobject gtk4 gtk4-layer-shell"
  elif command -v apt >/dev/null; then echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0"
  elif command -v pacman >/dev/null; then echo "  sudo pacman -S python-gobject python-cairo gtk4 gtk4-layer-shell"
  else echo "  PyGObject, pycairo, GTK 4 and gtk4-layer-shell from your distro"; fi
  die "then run this installer again."
}

say "Creating virtualenv in $PREFIX"
python3 -m venv --system-site-packages "$PREFIX/venv"
SRC="$REPO"
if [ -f pyproject.toml ] && grep -q 'name = "kernel-kingdom"' pyproject.toml; then SRC="."; else SRC="git+$REPO"; fi
say "Installing kernel-kingdom from $SRC"
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"

mkdir -p "$BIN"
ln -sf "$PREFIX/venv/bin/kernel-kingdom" "$BIN/kernel-kingdom"
case ":$PATH:" in *":$BIN:"*) ;; *) say "Add $BIN to your PATH";; esac

"$PREFIX/venv/bin/kernel-kingdom" doctor || true
if [ -z "${KERNEL_KINGDOM_NO_SERVICE:-}" ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then
  say "Enabling autostart (systemd --user)"
  "$PREFIX/venv/bin/kernel-kingdom" install || say "Could not enable the services; run 'kernel-kingdom' manually."
fi
say "Done. Commands: kernel-kingdom | kernel-kingdom window | kernel-kingdom chronicle | kernel-kingdom uninstall"
