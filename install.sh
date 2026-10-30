#!/usr/bin/env bash
# kernel-kingdom installer.
#   curl -fsSL https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/install.sh | bash
# Installs into ~/.local/share/kernel-kingdom/venv (uses the system's GTK/PyGObject),
# links ~/.local/bin/kernel-kingdom and enables the systemd --user services.
# Options (env vars): KERNEL_KINGDOM_REPO=<github url>  KERNEL_KINGDOM_NO_SERVICE=1
#                     KERNEL_KINGDOM_YES=1 (install missing system packages without asking)
set -euo pipefail

REPO="${KERNEL_KINGDOM_REPO:-https://github.com/rsmedrano-cloud/kernel-kingdom}"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/kernel-kingdom"
BIN="$HOME/.local/bin"

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# -- system packages ----------------------------------------------------------
# GTK 4, gtk4-layer-shell and PyGObject come from the distro (they can't be pip-installed
# sanely). If something is missing we offer to install it; KERNEL_KINGDOM_YES=1 says yes.
check_deps() {
  command -v python3 >/dev/null || return 1
  python3 - 2>/dev/null <<'PY'
import sys
assert sys.version_info >= (3, 11)
import venv, ensurepip, gi, cairo
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
PY
}

ask() {   # ask "question" -> 0 if yes. Works under curl | bash (reads the terminal).
  [ -n "${KERNEL_KINGDOM_YES:-}" ] && return 0
  [ -r /dev/tty ] || return 1
  local reply
  printf '\033[1;33m==>\033[0m %s [Y/n] ' "$1" > /dev/tty
  read -r reply < /dev/tty || return 1
  case "$reply" in [nN]*) return 1;; *) return 0;; esac
}

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
if [ -e /run/ostree-booted ]; then
  PKG_CMD="rpm-ostree install --idempotent python3-gobject gtk4 gtk4-layer-shell"; SUDO=""; NEEDS_REBOOT=1
elif command -v dnf >/dev/null; then
  PKG_CMD="dnf install -y python3 python3-gobject gtk4 gtk4-layer-shell"
elif command -v apt-get >/dev/null; then
  APT=1
  PKG_CMD="apt-get install -y python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0"
elif command -v pacman >/dev/null; then
  PKG_CMD="pacman -S --needed --noconfirm python python-gobject python-cairo gtk4 gtk4-layer-shell"
else
  PKG_CMD=""
fi

if ! check_deps; then
  say "Kernel Kingdom needs Python 3.11+, GTK 4, gtk4-layer-shell and PyGObject from your distro."
  [ -n "$PKG_CMD" ] || die "Install them with your package manager, then run this installer again."
  echo "    ${SUDO:+$SUDO }$PKG_CMD"
  if ask "Install them now?"; then
    if [ -n "${APT:-}" ]; then $SUDO apt-get update -qq || true; fi
    $SUDO $PKG_CMD || die "The package install failed; fix it and run this installer again."
    if [ -n "${NEEDS_REBOOT:-}" ]; then
      say "Packages layered. Reboot, then run this installer again (Atomic desktops apply packages on boot)."
      exit 0
    fi
  else
    die "Run the command above, then this installer again."
  fi
  check_deps || die "Still missing a dependency after installing. Please open an issue saying which distro you use."
fi

say "Creating virtualenv in $PREFIX"
python3 -m venv --system-site-packages "$PREFIX/venv"
SRC="$REPO"
# a tarball, so git isn't required
if [ -f pyproject.toml ] && grep -q 'name = "kernel-kingdom"' pyproject.toml; then SRC="."; else SRC="$REPO/archive/refs/heads/main.tar.gz"; fi
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
