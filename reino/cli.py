"""`kernel-kingdom`: un solo comando para todo.

    kernel-kingdom                 # = run: daemon + wallpaper en todos los monitores
    kernel-kingdom window          # en una ventana sin bordes (con zoom)
    kernel-kingdom install         # arranque automático con la sesión (systemd --user) y lo inicia
    kernel-kingdom uninstall
    kernel-kingdom doctor          # revisa dependencias y soporte del compositor
    kernel-kingdom config          # crea ~/.config/kernel-kingdom/config.toml para editar
    kernel-kingdom chronicle       # imprime la crónica
    kernel-kingdom story           # la historia del día como relato (.md y .txt)
    kernel-kingdom snapshot out.png
"""
import argparse
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import paths

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
DAEMON_UNIT = """[Unit]
Description=kernel-kingdom: system monitor and kingdom simulation

[Service]
ExecStart={python} -m reino.daemon
Restart=on-failure
RestartSec=5
Nice=10
CPUQuota=10%
MemoryMax=250M

[Install]
WantedBy=default.target
"""
RENDER_UNIT = """[Unit]
Description=kernel-kingdom: wallpaper on monitor %i
PartOf=graphical-session.target
After=graphical-session.target kernel-kingdom-daemon.service
Wants=kernel-kingdom-daemon.service

[Service]
ExecStart={python} -m reino.render --monitor %i
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
"""


def monitors() -> list[str]:
    """Conectores de los monitores (DP-1, HDMI-A-1...) preguntándole al compositor."""
    from .wl import Connection
    names: dict[int, str] = {}
    conn = None

    def on_global(name, iface, ver):
        if iface == "wl_output" and ver >= 4:
            oid = conn.bind(name, iface, 4)
            names[oid] = ""
            conn.adopt(oid, lambda op, r, oid=oid: names.__setitem__(oid, r.s()) if op == 4 else None)
    try:
        conn = Connection(on_global)
        conn.roundtrip()
        conn.roundtrip()
    except (OSError, KeyError, ConnectionError, TimeoutError):
        return []
    return [n for n in names.values() if n]


def _spawn(module: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", module, *args])


def cmd_run(args):
    targets = args.monitors.split(",") if args.monitors and args.monitors != "all" else (monitors() or [None])
    procs = [_spawn("reino.daemon")]
    time.sleep(1.5)                       # que exista el primer estado
    for m in targets:
        procs.append(_spawn("reino.render", *(["--monitor", m] if m else [])))
    _wait(procs)


def cmd_window(args):
    procs = [] if _daemon_running() else [_spawn("reino.daemon")]
    procs.append(_spawn("reino.render", "--windowed"))
    _wait(procs)


def _daemon_running() -> bool:
    st = paths.state_file()
    return st.exists() and time.time() - st.stat().st_mtime < 10


def _wait(procs):
    def stop(*_):
        for p in procs:
            p.terminate()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while all(p.poll() is None for p in procs):
            select.select([], [], [], 1)
    finally:
        stop()
        for p in procs:
            p.wait()


def _systemctl(*a, check=True):
    return subprocess.run(["systemctl", "--user", *a], check=check)


def cmd_install(args):
    if not shutil.which("systemctl"):
        sys.exit("systemctl no está disponible: usá `kernel-kingdom run` desde el autostart de tu escritorio.")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / "kernel-kingdom-daemon.service").write_text(DAEMON_UNIT.format(python=sys.executable))
    (UNIT_DIR / "kernel-kingdom-render@.service").write_text(RENDER_UNIT.format(python=sys.executable))
    targets = args.monitors.split(",") if args.monitors and args.monitors != "all" else monitors()
    if not targets:
        sys.exit("No encontré monitores Wayland. ¿Estás dentro de la sesión gráfica?")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "kernel-kingdom-daemon.service")
    for m in targets:
        _systemctl("enable", "--now", f"kernel-kingdom-render@{m}.service")
    print(f"kernel-kingdom instalado: daemon + wallpaper en {', '.join(targets)}.")
    print("Ver logs:  journalctl --user -u 'kernel-kingdom*' -f")


def cmd_uninstall(args):
    units = [p.name for p in UNIT_DIR.glob("kernel-kingdom*")]
    for name in units:
        if name.endswith("@.service"):
            continue
        _systemctl("disable", "--now", name, check=False)
    out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "kernel-kingdom-render@*"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        _systemctl("disable", "--now", line.split()[0], check=False)
    for name in units:
        (UNIT_DIR / name).unlink(missing_ok=True)
    _systemctl("daemon-reload", check=False)
    print(f"Listo. Tu mundo sigue guardado en {paths.save_file()} (borralo si querés empezar de cero).")


def cmd_doctor(args):
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'✔' if cond else '✘'} {label}" + ("" if cond or not hint else f"\n      → {hint}"))

    print("kernel-kingdom doctor")
    check(f"Python {sys.version.split()[0]} (>= 3.11)", sys.version_info >= (3, 11))
    try:
        import psutil  # noqa: F401
        check("psutil", True)
    except ImportError:
        check("psutil", False, "pip install psutil")
    try:
        import cairo  # noqa: F401
        import gi
        gi.require_version("Gtk", "4.0")
        check("PyGObject + GTK 4 + pycairo", True)
    except (ImportError, ValueError):
        check("PyGObject + GTK 4 + pycairo", False, _deps_hint())
    try:
        import gi
        gi.require_version("Gtk4LayerShell", "1.0")
        check("gtk4-layer-shell", True)
    except (ImportError, ValueError):
        check("gtk4-layer-shell", False, _deps_hint())
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    check("sesión Wayland", wayland, "el modo fondo de pantalla necesita Wayland; en X11 usá `kernel-kingdom window`")
    if wayland:
        from .wl import Connection
        found = set()
        try:
            c = Connection(lambda n, i, v: found.add(i))
            c.roundtrip()
        except (OSError, ConnectionError, TimeoutError):
            pass
        check("wlr-layer-shell (fondo de pantalla)", "zwlr_layer_shell_v1" in found,
              "tu compositor no lo soporta (GNOME): usá `kernel-kingdom window`")
        check("detección de ventanas que tapan el fondo (pausa)", "zcosmic_toplevel_info_v1" in found,
              "por ahora solo en COSMIC; en otros compositores el fondo no se pausa solo")
        check("presencia del usuario (ext_idle_notifier_v1)", "ext_idle_notifier_v1" in found)
        print(f"    monitores: {', '.join(monitors()) or '?'}")
    in_input = os.access("/dev/input/event0", os.R_OK)
    print(f"  {'✔' if in_input else '·'} contar teclas (opcional, grupo `input`): {'sí' if in_input else 'no'}")
    print("Todo listo." if ok else "Falta algo (ver arriba).")
    return 0 if ok else 1


def _deps_hint() -> str:
    osr = Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else ""
    if "ostree" in osr or Path("/run/ostree-booted").exists():
        return "rpm-ostree install python3-gobject gtk4 gtk4-layer-shell  (y reiniciá)"
    if "fedora" in osr.lower():
        return "sudo dnf install python3-gobject gtk4 gtk4-layer-shell"
    if "arch" in osr.lower():
        return "sudo pacman -S python-gobject gtk4 gtk4-layer-shell"
    if "debian" in osr.lower() or "ubuntu" in osr.lower():
        return "sudo apt install python3-gi gir1.2-gtk-4.0 libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0"
    return "instalá PyGObject, GTK 4 y gtk4-layer-shell con el gestor de paquetes de tu distro"


def cmd_story(args):
    from . import config, i18n, story
    i18n.set_lang(config.load().get("language", "es"))
    out = story.export(args.date)
    if not out:
        print(i18n.tr("story.empty", date=args.date or time.strftime("%Y-%m-%d")))
        return 1
    md, txt = out
    if args.print:
        print(md.read_text(encoding="utf-8"))
    print(f"{md}\n{txt}")
    return 0


def cmd_config(args):
    dest = paths.user_config()
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(paths.DEFAULT_CONFIG, dest)
    print(dest)


def main():
    ap = argparse.ArgumentParser(prog="kernel-kingdom", description="Live wallpaper / idle game fed by your system.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="daemon + wallpaper (default)")
    run.add_argument("--monitors", default="all", help="all, o DP-1,HDMI-A-1")
    sub.add_parser("window", help="ventana sin bordes, con zoom")
    inst = sub.add_parser("install", help="arranque automático (systemd --user)")
    inst.add_argument("--monitors", default="all")
    sub.add_parser("uninstall")
    sub.add_parser("doctor")
    sub.add_parser("config", help="crea/muestra tu config de usuario")
    sub.add_parser("chronicle", help="imprime la crónica")
    st = sub.add_parser("story", help="la historia del día como relato (.md y .txt para leer o escuchar)")
    st.add_argument("--date", help="AAAA-MM-DD (por defecto, hoy)")
    st.add_argument("--print", action="store_true", help="mostrarla en vez de solo guardarla")
    snap = sub.add_parser("snapshot", help="un frame a PNG")
    snap.add_argument("png")
    snap.add_argument("extra", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    if args.cmd in (None, "run"):
        if args.cmd is None:
            args.monitors = "all"
        return cmd_run(args)
    if args.cmd == "chronicle":
        os.execv(sys.executable, [sys.executable, "-m", "reino.daemon", "--chronicle"])
    if args.cmd == "snapshot":
        os.execv(sys.executable, [sys.executable, "-m", "reino.render", "--snapshot", args.png, *args.extra])
    if args.cmd == "story":
        return cmd_story(args)
    return {"window": cmd_window, "install": cmd_install, "uninstall": cmd_uninstall,
            "doctor": cmd_doctor, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
