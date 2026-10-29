"""`kernel-kingdom`: un solo comando para todo.

    kernel-kingdom                 # daemon + wallpaper en todos los monitores, en segundo plano
    kernel-kingdom window          # abre la ventana (zoom, poderes) y devuelve la terminal
    kernel-kingdom stop            # detiene todo
    kernel-kingdom install         # lanzador en el menú + arranque automático con la sesión
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

from . import config, i18n, paths
from .i18n import ui

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


APP_ID = "dev.kernelkingdom.Window"
DEV_ROOT = Path(__file__).resolve().parent.parent
IS_CHECKOUT = (DEV_ROOT / "pyproject.toml").exists() and (DEV_ROOT / "reino").is_dir()
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _logs() -> Path:
    d = paths.save_file().parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pidfile(name: str) -> Path:
    d = paths.state_file().parent / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _detach(name: str, module: str, *args: str) -> int:
    """Lanza un proceso separado de la terminal (sin salida en pantalla: va a un log)."""
    log = open(_logs() / f"{name}.log", "a")
    proc = subprocess.Popen([sys.executable, "-m", module, *args], stdin=subprocess.DEVNULL, stdout=log,
                            stderr=log, start_new_session=True, cwd=DEV_ROOT if IS_CHECKOUT else None)
    _pidfile(name).write_text(str(proc.pid))
    return proc.pid


def _running(name: str) -> bool:
    f = _pidfile(name)
    return f.exists() and f.read_text().strip().isdigit() and _alive(int(f.read_text()))


def _unit_active(unit: str) -> bool:
    return shutil.which("systemctl") is not None and subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def _unit_installed(unit: str) -> bool:
    return (UNIT_DIR / unit).exists()


def ensure_daemon():
    """El mundo corre aparte de cualquier ventana o terminal: servicio de systemd si
    está instalado; si no, un proceso suelto en segundo plano."""
    st = paths.state_file()
    if st.exists() and time.time() - st.stat().st_mtime < 10:
        return
    if _unit_installed("kernel-kingdom-daemon.service"):
        _systemctl("start", "kernel-kingdom-daemon.service", check=False)
    elif not _running("daemon"):
        _detach("daemon", "reino.daemon")
        time.sleep(1.5)                       # que exista el primer estado


def cmd_run(args):
    """Fondo de pantalla en todos los monitores, en segundo plano. La terminal queda libre."""
    if args.foreground:
        return _foreground(args)
    ensure_daemon()
    targets = args.monitors.split(",") if args.monitors and args.monitors != "all" else (monitors() or [None])
    for m in targets:
        unit = f"kernel-kingdom-render@{m}.service"
        if m and _unit_installed("kernel-kingdom-render@.service"):
            _systemctl("start", unit, check=False)
        elif not _running(f"render-{m}"):
            _detach(f"render-{m}", "reino.render", *(["--monitor", m] if m else []))
    print(ui("cli.started", monitors=", ".join(t or "?" for t in targets)))


def cmd_window(args):
    """Abre la ventana y devuelve la terminal. Cerrar la ventana no detiene el mundo."""
    ensure_daemon()
    if args.foreground:
        os.execv(sys.executable, [sys.executable, "-m", "reino.render", "--windowed"])
    _detach("window", "reino.render", "--windowed")
    print(ui("cli.window"))


def cmd_stop(args):
    for name in ("window", *(p.stem for p in (paths.state_file().parent / "pids").glob("render-*.pid")), "daemon"):
        f = _pidfile(name)
        if f.exists() and f.read_text().strip().isdigit() and _alive(int(f.read_text())):
            os.kill(int(f.read_text()), signal.SIGTERM)
        f.unlink(missing_ok=True)
    if shutil.which("systemctl"):
        out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "kernel-kingdom*"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            _systemctl("stop", line.split()[0], check=False)
    print(ui("cli.stopped"))


def _foreground(args):
    """Modo desarrollo: todo en esta terminal, con logs; Ctrl+C corta."""
    targets = args.monitors.split(",") if args.monitors and args.monitors != "all" else (monitors() or [None])
    procs = [subprocess.Popen([sys.executable, "-m", "reino.daemon"])]
    time.sleep(1.5)
    for m in targets:
        procs.append(subprocess.Popen([sys.executable, "-m", "reino.render", *(["--monitor", m] if m else [])]))

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


def install_launcher():
    """Lanzador 'Kernel Kingdom' en el menú de aplicaciones (abre la ventana)."""
    from . import icon
    icon.make(DATA / "icons" / "hicolor" / "256x256" / "apps" / "kernel-kingdom.png")
    apps = DATA / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    path_line = f"Path={DEV_ROOT}\n" if IS_CHECKOUT else ""
    (apps / f"{APP_ID}.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Kernel Kingdom\n"
        "Comment=Your computer as a pixel-art kingdom / Tu computadora como un reino\n"
        f"Exec={sys.executable} -m reino window\n{path_line}Icon=kernel-kingdom\nTerminal=false\n"
        f"Categories=Game;Simulation;\nStartupWMClass={APP_ID}\nKeywords=wallpaper;kingdom;reino;\n")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], check=False, capture_output=True)


def cmd_install(args):
    install_launcher()
    if not shutil.which("systemctl"):
        print(ui("cli.no_systemctl"))
        return
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    workdir = f"WorkingDirectory={DEV_ROOT}\n" if IS_CHECKOUT else ""
    (UNIT_DIR / "kernel-kingdom-daemon.service").write_text(
        DAEMON_UNIT.format(python=sys.executable).replace("[Service]\n", "[Service]\n" + workdir))
    (UNIT_DIR / "kernel-kingdom-render@.service").write_text(
        RENDER_UNIT.format(python=sys.executable).replace("[Service]\n", "[Service]\n" + workdir))
    cmd_stop(args)                                        # lo que hubiera suelto, pasa a manos de systemd
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "kernel-kingdom-daemon.service")
    targets = [] if args.no_wallpaper else (args.monitors.split(",") if args.monitors != "all" else monitors())
    for m in targets:
        _systemctl("enable", "--now", f"kernel-kingdom-render@{m}.service")
    print(ui("cli.installed", monitors=", ".join(targets)) if targets else ui("cli.installed_world"))
    print(ui("cli.launcher"))
    print(ui("cli.logs"))


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
    (DATA / "applications" / f"{APP_ID}.desktop").unlink(missing_ok=True)
    (DATA / "icons" / "hicolor" / "256x256" / "apps" / "kernel-kingdom.png").unlink(missing_ok=True)
    print(ui("cli.uninstalled", path=paths.save_file()))


def cmd_doctor(args):
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'✔' if cond else '✘'} {label}" + ("" if cond or not hint else f"\n      → {hint}"))

    print(ui("cli.doctor"))
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
    check(ui("cli.wayland"), wayland, ui("cli.wayland_hint"))
    if wayland:
        from .wl import Connection
        found = set()
        try:
            c = Connection(lambda n, i, v: found.add(i))
            c.roundtrip()
        except (OSError, ConnectionError, TimeoutError):
            pass
        check(ui("cli.layer"), "zwlr_layer_shell_v1" in found, ui("cli.layer_hint"))
        check(ui("cli.pause"), "zcosmic_toplevel_info_v1" in found, ui("cli.pause_hint"))
        check(ui("cli.idle"), "ext_idle_notifier_v1" in found)
        print(f"    {ui('cli.monitors')}: {', '.join(monitors()) or '?'}")
    in_input = os.access("/dev/input/event0", os.R_OK)
    print(f"  {'✔' if in_input else '·'} {ui('cli.keys')}: {ui('cli.yes') if in_input else ui('cli.no')}")
    print(ui("cli.ok") if ok else ui("cli.missing"))
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
    from . import story
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
    run = sub.add_parser("run", help="daemon + wallpaper en segundo plano (default)")
    run.add_argument("--monitors", default="all", help="all, o DP-1,HDMI-A-1")
    run.add_argument("-f", "--foreground", action="store_true", help="en esta terminal, con logs (desarrollo)")
    win = sub.add_parser("window", help="abre la ventana (con zoom y poderes) y devuelve la terminal")
    win.add_argument("-f", "--foreground", action="store_true")
    sub.add_parser("stop", help="detiene todo (ventana, wallpaper y daemon)")
    inst = sub.add_parser("install", help="lanzador en el menú + arranque automático (systemd --user)")
    inst.add_argument("--monitors", default="all")
    inst.add_argument("--no-wallpaper", action="store_true", help="solo el mundo y el lanzador, sin fondo de pantalla")
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
    try:
        i18n.set_lang(config.load().get("language", "auto"))
    except (OSError, ValueError):
        i18n.set_lang("auto")

    if args.cmd in (None, "run"):
        if args.cmd is None:
            args.monitors, args.foreground = "all", False
        return cmd_run(args)
    if args.cmd == "chronicle":
        os.execv(sys.executable, [sys.executable, "-m", "reino.daemon", "--chronicle"])
    if args.cmd == "snapshot":
        os.execv(sys.executable, [sys.executable, "-m", "reino.render", "--snapshot", args.png, *args.extra])
    if args.cmd == "story":
        return cmd_story(args)
    return {"window": cmd_window, "stop": cmd_stop, "install": cmd_install, "uninstall": cmd_uninstall,
            "doctor": cmd_doctor, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
