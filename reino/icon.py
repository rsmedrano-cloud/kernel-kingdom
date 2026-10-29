"""Ícono de la aplicación: un islote con un palacio, dibujado con los mismos sprites del juego."""
from pathlib import Path

import cairo

from . import buildings as B  # noqa: F401  (los sprites importan el catálogo)
from .render.iso import DEEP_WATER, GRASS, SAND, Sprites, rgb


def make(path: Path, size: int = 256):
    sprites = Sprites(1)
    base = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
    ctx = cairo.Context(base)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    ctx.arc(32, 36, 30, 0, 6.283)                      # fondo: mar redondo
    ctx.set_source_rgb(*DEEP_WATER)
    ctx.fill()

    def put(key, x, y):
        surf, dx, dy = sprites.get(key)
        ctx.set_source_surface(surf, x + dx, y + dy)
        ctx.paint()

    for gx, gy in ((0, 0), (1, 0), (0, 1), (1, 1), (-1, 0), (0, -1), (2, 1), (1, 2)):
        t = GRASS if (gx, gy) in ((0, 0), (1, 0), (0, 1), (1, 1)) else SAND
        put(("terrain", t, 0, 1, "", "verano"), 32 + (gx - gy) * 8, 26 + (gx + gy) * 4)
    col = rgb("#d94a3d")
    put(("house", 2, 0, col, 0, 1), 22, 38)
    put(("house", 1, 0, col, 0, 0), 42, 38)
    put(("palace", 2, col, 0), 32, 36)
    put(("tree", "pine", 0, "verano"), 18, 32)
    put(("tree", "round", 1, "verano"), 46, 31)

    big = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    bctx = cairo.Context(big)
    bctx.scale(size / 64, size / 64)
    bctx.set_source_surface(base, 0, 0)
    bctx.get_source().set_filter(cairo.FILTER_NEAREST)
    bctx.paint()
    path.parent.mkdir(parents=True, exist_ok=True)
    big.write_to_png(str(path))
