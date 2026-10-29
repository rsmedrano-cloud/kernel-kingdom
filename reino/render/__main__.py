"""Renderer del wallpaper.

    python -m reino.render                    # fondo de pantalla (layer-shell, capa BACKGROUND)
    python -m reino.render --windowed         # ventana normal, para desarrollar
    python -m reino.render --snapshot out.png # un frame a PNG, sin GTK
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .. import config, i18n, paths

LAYER_SHELL_LIB = "libgtk4-layer-shell.so"


def _ensure_layer_shell_preload():
    """gtk4-layer-shell tiene que cargarse antes que libwayland-client.
    Desde Python la única forma confiable es LD_PRELOAD, así que nos re-ejecutamos."""
    if LAYER_SHELL_LIB in os.environ.get("LD_PRELOAD", ""):
        return
    for d in ("/usr/lib64", "/usr/lib", "/usr/lib/x86_64-linux-gnu"):
        for lib in sorted(Path(d).glob(LAYER_SHELL_LIB + "*")):  # Fedora solo trae .so.0
            env = dict(os.environ, LD_PRELOAD=f"{lib} {os.environ.get('LD_PRELOAD', '')}".strip())
            os.execve(sys.executable, [sys.executable, "-m", "reino.render", *sys.argv[1:]], env)
    print("aviso: gtk4-layer-shell no encontrado, uso ventana normal", file=sys.stderr)


def snapshot(args, cfg):
    """Renderiza un frame a PNG tal como se vería en una pantalla de --size."""
    import cairo

    from .iso import Painter, detail_for, fit_scale, zoom_scales

    state = json.loads(Path(args.state).read_text())
    painter = Painter(hemisphere=cfg["render"]["hemisphere"])
    painter.hour_override = args.hour
    painter.season_override = args.season
    painter.window_mode = args.window
    sw, sh = (int(v) for v in args.size.split("x"))
    now = time.monotonic()
    painter.set_state(state, now)
    # forzar que los eventos del tick se vean en la foto
    painter.bolts = [dict(ev, start=now - 0.03, seed=k / 7) for k, ev in enumerate(state.get("events", []))]
    base = fit_scale(sw, sh, painter.content_size(), cfg["render"]["pixel_scale"])
    scales = zoom_scales(base)
    scale = scales[min(args.zoom, len(scales) - 1)]
    if args.focus:   # centrar la cámara en la capital de una facción
        f = next(f for f in state["factions"] if f["name"] == args.focus)
        x, y = f["capital"]
        wx, wy = painter.tile_xy(x, y)
        ww, wh = painter.world_size()
        painter.pan = [wx - ww / 2, wy - wh / 2]
    fw, fh = int(-(-sw // scale)), int(-(-sh // scale))
    painter.set_detail(detail_for(scale))
    t0 = time.perf_counter()
    painter.frame(fw, fh, now)                 # primer frame: incluye el base estático
    t1 = time.perf_counter()
    for k in range(30):
        painter.frame(fw, fh, now + 0.001 * k)   # frames animados
    t2 = time.perf_counter()
    if args.click:
        px, py = (float(v) for v in args.click.split(","))
        painter.inspect(px / scale, py / scale, now, ui_pos=(px / base, py / base))
        painter.hover = painter.pick(px / scale, py / scale)
    surf = painter.frame(fw, fh, now + 0.05)
    ui, _ = painter.ui(int(-(-sw // base)), int(-(-sh // base)), now + 0.05)
    big = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
    ctx = cairo.Context(big)
    for layer, k in ((surf, scale / painter.u), (None if args.no_ui else ui, base)):
        if layer is None:
            continue
        ctx.save()
        ctx.scale(k, k)
        ctx.set_source_surface(layer, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)
        ctx.paint()
        ctx.restore()
    big.write_to_png(args.snapshot)
    print(f"{args.snapshot}: escala {scale:g} ({fw}x{fh} lógicos), base {1000 * (t1 - t0):.1f} ms, "
          f"frame animado {1000 * (t2 - t1) / 30:.2f} ms")


def main():
    ap = argparse.ArgumentParser(prog="reino.render")
    ap.add_argument("--windowed", action="store_true", help="ventana normal en vez de wallpaper")
    ap.add_argument("--monitor", help="monitor del wallpaper: índice (0, 1...) o conector (DP-1)")
    ap.add_argument("--state", default=str(paths.state_file()))
    ap.add_argument("--config", type=Path)
    ap.add_argument("--fps", type=int, help="tope de FPS (máx. 30)")
    ap.add_argument("--no-occlusion", action="store_true", help="no pausar con ventanas maximizadas")
    ap.add_argument("--snapshot", metavar="PNG")
    ap.add_argument("--size", default="2560x1440", help="tamaño de pantalla para --snapshot")
    ap.add_argument("--click", metavar="X,Y", help="--snapshot: simular un clic (px de pantalla)")
    ap.add_argument("--season", choices=["invierno", "primavera", "verano", "otoño"])
    ap.add_argument("--zoom", type=int, default=0, help="--snapshot: niveles de zoom extra")
    ap.add_argument("--focus", help="--snapshot: centrar en la capital de esta facción (ej. chrome)")
    ap.add_argument("--no-ui", action="store_true", help="--snapshot: sin HUD ni crónica")
    ap.add_argument("--window", action="store_true", help="--snapshot: interfaz del modo ventana (listas, poderes)")
    ap.add_argument("--hour", type=float, help="forzar hora del día (0-24)")
    args = ap.parse_args()
    cfg = config.load(args.config)
    i18n.set_lang(cfg.get("language", "es"))

    if args.snapshot:
        return snapshot(args, cfg)
    if not args.windowed:
        _ensure_layer_shell_preload()
    from .app import run
    run(args, cfg)


if __name__ == "__main__":
    main()
