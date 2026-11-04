"""Rutas XDG compartidas por daemon y renderer."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(__file__).resolve().parent / "default_config.toml"


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def user_config() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "kernel-kingdom" / "config.toml"


def _runtime() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/kernel-kingdom-{os.getuid()}")


def state_file() -> Path:
    """Estado vivo: va a XDG_RUNTIME_DIR (tmpfs), así no gastamos disco cada 2 s."""
    return _runtime() / "kernel-kingdom" / "state.json"


def commands_dir() -> Path:
    """Pedidos de poderes del wallpaper al daemon: un archivo JSON por pedido."""
    return state_file().parent / "commands"


def save_file() -> Path:
    """Mundo persistente entre reinicios."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "kernel-kingdom" / "world.json"


def migrate_legacy():
    """El proyecto se llamaba lw-reino: si hay un mundo o una config con el nombre
    viejo y todavía no con el nuevo, se copian (no se pierde el reino)."""
    import shutil
    for new, folder, base in ((save_file(), "XDG_STATE_HOME", ".local/state"),
                              (user_config(), "XDG_CONFIG_HOME", ".config")):
        old = _xdg(folder, base) / "lw-reino" / new.name
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)


def data_dir() -> Path:
    """Archivo de crónicas por día e historias exportadas."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "kernel-kingdom"


def make_private():
    """La crónica, el mundo, la lista de máquinas y el estado vivo dicen qué corre en la
    PC: solo los lee su dueño (otros usuarios del mismo equipo, no). Crea las carpetas
    con permisos 700 y corrige las que ya existían con otros."""
    dirs = [data_dir(), save_file().parent, user_config().parent, state_file().parent]
    if not os.environ.get("XDG_RUNTIME_DIR"):
        dirs.insert(0, _runtime())                          # /tmp es de todos: la carpeta propia, cerrada
    for d in dirs:
        try:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            if d.stat().st_uid == os.getuid() and d.stat().st_mode & 0o077:
                d.chmod(0o700)
        except OSError:
            pass
