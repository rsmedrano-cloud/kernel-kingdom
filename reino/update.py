"""Actualizaciones: ver si hay una versión nueva en GitHub e instalarla.

Instalado con install.sh (venv): pip baja el tarball de main. Desde un clon de
git: `git pull --ff-only`. Después se reinician los servicios para que el mundo
y el fondo corran el código nuevo (el mundo guardado se conserva).
"""
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__

REPO = "https://github.com/rsmedrano-cloud/kernel-kingdom"
TARBALL = f"{REPO}/archive/refs/heads/main.tar.gz"
PYPROJECT = "https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/pyproject.toml"
ROOT = Path(__file__).resolve().parent.parent


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest(timeout: float = 6.0) -> str | None:
    """Versión publicada en main (None si no hay red)."""
    try:
        with urllib.request.urlopen(PYPROJECT, timeout=timeout) as r:
            m = re.search(r'^version\s*=\s*"([^"]+)"', r.read().decode(), re.M)
        return m.group(1) if m else None
    except (OSError, ValueError):
        return None


def newer(timeout: float = 6.0) -> str | None:
    v = latest(timeout)
    return v if v and _parse(v) > _parse(__version__) else None


def _offline_build() -> bool:
    try:
        import psutil  # noqa: F401
        import importlib.util
        import setuptools
        v = int(setuptools.__version__.split(".")[0])
        return v >= 61 and (v >= 71 or importlib.util.find_spec("wheel") is not None)
    except (ImportError, ValueError):
        return False


def install() -> tuple[bool, str]:
    """Baja e instala la última versión. Devuelve (ok, detalle)."""
    if (ROOT / ".git").exists() and shutil.which("git"):
        cmds = [["git", "-C", str(ROOT), "pull", "--ff-only"]]
    else:
        pip = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        if _offline_build():                     # psutil y setuptools del sistema: nada de PyPI
            cmds = [pip + ["--upgrade", "--force-reinstall", "--no-deps", "--no-build-isolation", TARBALL]]
        else:
            cmds = [pip + ["--upgrade", "--force-reinstall", "--no-deps", TARBALL], pip + [TARBALL]]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            lines = (r.stderr or r.stdout or "error").strip().splitlines()
            return False, lines[-1] if lines else "error"
    return True, ""


def restart_services() -> list[str]:
    """Reinicia el mundo y los fondos que estén corriendo bajo systemd."""
    if not shutil.which("systemctl"):
        return []
    out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "--state=active",
                          "kernel-kingdom*"], capture_output=True, text=True).stdout
    units = [line.split()[0] for line in out.splitlines() if line.strip()]
    if units:
        subprocess.run(["systemctl", "--user", "restart", *units], check=False)
    return units
