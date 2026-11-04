"""Actualizaciones: ver si hay una versión nueva en GitHub e instalarla.

Solo cuentan los *releases* publicados (v0.4.4, v0.5.0…), no lo último de main:
así lo que está a medio hacer nunca le llega a nadie. Instalado con install.sh
(venv): pip baja el tarball de ese release. Desde un clon de git (desarrollo):
`git pull --ff-only`. Después se reinician los servicios para que el mundo y el
fondo corran el código nuevo (el mundo guardado se conserva).
"""
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__

REPO = "https://github.com/rsmedrano-cloud/kernel-kingdom"
RELEASES = "https://api.github.com/repos/rsmedrano-cloud/kernel-kingdom/releases/latest"
ROOT = Path(__file__).resolve().parent.parent


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_release(timeout: float = 6.0) -> tuple[str, str] | None:
    """(versión, tag) del último release publicado; None si no hay red o releases."""
    req = urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": f"kernel-kingdom/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tag = json.loads(r.read().decode()).get("tag_name") or ""
    except (OSError, ValueError):
        return None
    return (tag.lstrip("v"), tag) if re.match(r"^v?\d+\.\d+", tag) else None


def latest(timeout: float = 6.0) -> str | None:
    rel = latest_release(timeout)
    return rel[0] if rel else None


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


RELEASE_PAGE = f"{REPO}/releases/latest"


def packaged() -> str | None:
    """Cómo se instaló, si vino en un paquete: "deb", "rpm" o "arch" (los del release).
    Lo deja el empaquetado en reino/PACKAGED. None: install.sh, pip o un clon de git."""
    try:
        kind = (Path(__file__).resolve().parent / "PACKAGED").read_text().strip()
    except OSError:
        return None
    return kind if kind in ("arch", "deb", "rpm") else None


def packaged_hint(kind: str) -> str:
    """Qué hacer para actualizar un paquete (lo maneja la distro, no la app)."""
    from .i18n import ui
    return ui(f"update.packaged.{kind}", url=RELEASE_PAGE)


def install() -> tuple[bool, str]:
    """Baja e instala la última versión. Devuelve (ok, detalle)."""
    kind = packaged()
    if kind:                                     # los archivos son del gestor de paquetes: no se tocan
        return False, packaged_hint(kind)
    if (ROOT / ".git").exists() and shutil.which("git"):
        cmds = [["git", "-C", str(ROOT), "pull", "--ff-only"]]
    else:
        rel = latest_release()
        if not rel:
            return False, "no pude consultar los releases de GitHub"
        TARBALL = f"{REPO}/archive/refs/tags/{rel[1]}.tar.gz"
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
