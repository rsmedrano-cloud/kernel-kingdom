"""Pintor isométrico pixel art. Cairo puro, sin GTK: se puede usar headless.

Capas:
  * mar   : patrón repetido (infinito), se mueve con la cámara.
  * mundo : la isla con todos sus edificios, en su propia superficie. Se
            repinta solo cuando cambia algo visible (~30 ms cada tanto).
  * vida  : aldeanos, barcos, ovejas, humo, rayos... se dibujan cada frame
            sobre el mundo (<1 ms).
  * UI    : HUD, crónica y fichas, en coordenadas de pantalla.
Como el mundo no depende de la cámara, el zoom y el paneo son gratis.
Todo es baja resolución; el escalado lo hace la GPU con NEAREST.
"""
import math
import os
from collections import deque
import random
import time
import zlib

import cairo

from .. import buildings as B
from ..i18n import ui, words

TW, TH = 16, 8                       # rombo de casilla 2:1
WATER, SAND, GRASS, FOREST, HILL, MOUNTAIN = range(6)
ELEV = {WATER: -2, SAND: 0, GRASS: 0, FOREST: 0, HILL: 3, MOUNTAIN: 7}
BASE = 3                             # grosor de la losa bajo cada casilla
MARGIN = 24                          # aire alrededor de la isla en la superficie del mundo
POWER_KEYS = ("rayo", "lluvia", "bendicion", "heroe", "bosque", "faro", "fiesta")


def rgb(hexstr: str) -> tuple[float, float, float]:
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def shade(c, f: float):
    return tuple(min(1.0, max(0.0, v * f)) for v in c)


def mix(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def h32(*parts) -> int:
    return zlib.crc32(":".join(map(str, parts)).encode())


# -- paleta por estación -----------------------------------------------------

SEASONS = ("invierno", "primavera", "verano", "otoño")
_GROUND = {
    "primavera": {SAND: "#e2cc8b", GRASS: "#74b850", FOREST: "#58963f", HILL: "#93a35e", MOUNTAIN: "#8c8784", WATER: "#3f74b8"},
    "verano":    {SAND: "#e6cf8a", GRASS: "#6aaa4b", FOREST: "#4e8c3c", HILL: "#8f9c5c", MOUNTAIN: "#8c8784", WATER: "#3f78be"},
    "otoño":     {SAND: "#dcc584", GRASS: "#8ea44c", FOREST: "#7a8a3e", HILL: "#9a9358", MOUNTAIN: "#8a8380", WATER: "#3b6eb0"},
    "invierno":  {SAND: "#cfc9b6", GRASS: "#dfe7eb", FOREST: "#d3dde2", HILL: "#c9d2d6", MOUNTAIN: "#9a9898", WATER: "#36639f"},
}
_LEAVES = {
    "primavera": ["#4f9a3e", "#5aa543"], "verano": ["#2f6b34", "#3a7a3a"],
    "otoño": ["#c8642b", "#d99a2b", "#a8402a", "#b7782c"], "invierno": [],
}
DEEP_WATER = rgb("#2f5d99")
SNOW = rgb("#eef0f2")
TRUNK, PINE = rgb("#6b4a2b"), rgb("#2c5a3a")
STONE, WALL_STONE, TOWER_STONE = rgb("#b9b2a5"), rgb("#a59e92"), rgb("#7f72a6")
MARBLE, WOOD, DARK_WOOD = rgb("#ece6da"), rgb("#8a6a4a"), rgb("#5a3d22")
WINDOW, WINDOW_LIT = rgb("#3a3a4a"), rgb("#ffd36b")
SCORCHED, EMBER, LAVA = rgb("#3b2d27"), rgb("#ff8a2a"), rgb("#ff6b1a")
RUBBLE, DIRT, COBBLE = rgb("#8a8580"), rgb("#a07e52"), rgb("#a8a094")
SKIN, GOLD = rgb("#f1c9a5"), rgb("#f5d76e")
FLAG = {"container": rgb("#2bb3d9"), "db": rgb("#a35bd6"), "daemon": rgb("#f0c419"), "mine": rgb("#c0c0c0")}
MATERIALS = [  # (pared izq, pared der)
    (rgb("#eadfc8"), rgb("#cbbd9e")),   # revoque
    (rgb("#c8c4bc"), rgb("#a8a49c")),   # piedra
    (rgb("#d9c39a"), rgb("#b8a27a")),   # madera
    (rgb("#d8b48a"), rgb("#b8946a")),   # adobe
    (rgb("#f2f0ea"), rgb("#d2d0ca")),   # cal
]


def season_for(month: int, hemisphere: str) -> str:
    s = SEASONS[(month % 12) // 3]          # dic-feb invierno, mar-may primavera...
    if hemisphere == "sur":
        s = SEASONS[(SEASONS.index(s) + 2) % 4]
    return s


def hemisphere_auto() -> str:
    tz = os.path.realpath("/etc/localtime")
    south = ("Argentina", "Montevideo", "Santiago", "Asuncion", "Sao_Paulo", "Australia", "Auckland",
             "Johannesburg", "Lima", "La_Paz", "Punta_Arenas")
    return "sur" if any(s in tz for s in south) else "norte"


def night_factor(hour: float) -> float:
    if 7 <= hour <= 18:
        return 0.0
    if 18 < hour < 21:
        return (hour - 18) / 3
    if 5 < hour < 7:
        return (7 - hour) / 2
    return 1.0


def fit_scale(w: int, h: int, content: tuple[int, int], mode) -> float:
    """Escala pantalla/pixel lógico. "auto": la isla entra entera; entera si no
    se pierde más de 25% de tamaño (pixels parejos), si no fraccionaria."""
    if mode != "auto":
        return max(1, int(mode))
    s = min(w / content[0], h / content[1])
    si = math.floor(s * 1.06)
    return float(si) if si >= 1 and si / s >= 0.75 else max(0.5, s)


def zoom_scales(base: float) -> list[float]:
    """Escalas de zoom: la base y después enteros pares (con detalle ×2, cada pixel
    nativo ocupa un número entero de pixels de pantalla: nada de pixels desparejos)."""
    return [base] + [float(s) for s in (4, 6, 8, 10, 12, 16) if s > base + 0.5]


def detail_for(scale: float) -> int:
    """Con 4+ pixels de pantalla por unidad vale la pena dibujar los sprites al doble."""
    return 2 if scale >= 4 and float(scale).is_integer() and int(scale) % 2 == 0 else 1


def map_size(n: int = 72) -> tuple[int, int]:
    """Tamaño aproximado de la isla antes de recibir estado."""
    return int(n * TW * 0.62), int(n * TH * 0.75)


# -- primitivas pixel-perfect ------------------------------------------------

# Los sprites se describen en "unidades" (1 unidad = 1 pixel del arte base).
# Con _U = 2 se generan al doble de resolución para el zoom: los rombos tienen
# bordes más finos, las cajas llevan contorno de 1 px y las texturas son más
# finas. El mismo código de sprite sirve para las dos resoluciones.
_U = 1


def _surface(w: float, h: float, u: int | None = None):
    """Superficie de w x h unidades; el contexto ya viene escalado a unidades."""
    u = _U if u is None else u
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w * u)), max(1, int(h * u)))
    ctx = cairo.Context(s)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    if u != 1:
        ctx.scale(u, u)
    return s, ctx


def _unit(ctx) -> int:
    return max(1, round(ctx.get_matrix().xx))


def _diamond_rows(w: int):
    h = w // 2
    for r in range(h):
        yield r, (2 * (r + 1) if r < h // 2 else 2 * (h - r))


def diamond(ctx, ox: float, oy: float, w: int, left, right=None):
    """Rombo de ancho w unidades (múltiplo de 4) con su esquina superior-izq en
    (ox, oy). Se rasteriza en pixels nativos: a más resolución, bordes más finos.
    Con `right` pinta la mitad derecha de otro color (caras laterales)."""
    u = _unit(ctx)
    X, Y = ctx.user_to_device(ox, oy)
    X, Y, W = round(X), round(Y), int(w * u)
    ctx.save()
    ctx.identity_matrix()
    cx = X + W // 2
    for color, part in ((left, "l"), (right, "r")) if right else ((left, "a"),):
        for r, half in _diamond_rows(W):
            if part == "a":
                ctx.rectangle(cx - half, Y + r, 2 * half, 1)
            elif part == "l":
                ctx.rectangle(cx - half, Y + r, half, 1)
            else:
                ctx.rectangle(cx, Y + r, half, 1)
        ctx.set_source_rgb(*color)
        ctx.fill()
    ctx.restore()


def prism(ctx, ox: float, oy: float, w: int, height: float, top, left=None, right=None, outline: bool = True):
    """Caja isométrica: caras laterales apiladas + tapa (+ contorno en alta resolución)."""
    left = left or shade(top, 0.8)
    right = right or shade(top, 0.64)
    u = _unit(ctx)
    for i in range(int(height * u), 0, -1):
        diamond(ctx, ox, oy + i / u, w, left, right)
    diamond(ctx, ox, oy, w, top)
    if u >= 2 and height > 0 and outline:
        _outline(ctx, ox, oy, w, height, top, left, right)


def _outline(ctx, ox, oy, w, height, top, left, right):
    """Aristas de 1 px nativo: luz en el borde superior, sombra en la arista
    frontal y en la base. Es lo que hace legible un edificio con zoom."""
    u = _unit(ctx)
    X, Y = ctx.user_to_device(ox, oy)
    X, Y, W, HH = round(X), round(Y), int(w * u), int(height * u)
    H = W // 2
    cx = X + W // 2
    ctx.save()
    ctx.identity_matrix()
    rows = list(_diamond_rows(W))
    hi = [(cx - half, Y + r) for r, half in rows[:H // 2]] + [(cx + half - 1, Y + r) for r, half in rows[:H // 2]]
    for x, y in hi:
        ctx.rectangle(x, y, 1, 1)
    ctx.set_source_rgb(*shade(top, 1.18))
    ctx.fill()
    ctx.rectangle(cx, Y + H, 1, HH)                                  # arista frontal
    ctx.set_source_rgb(*shade(right, 0.72))
    ctx.fill()
    lo = [(cx - half, Y + HH + r) for r, half in rows[H // 2:]] + \
         [(cx + half - 1, Y + HH + r) for r, half in rows[H // 2:]]
    for x, y in lo:
        ctx.rectangle(x, y, 1, 1)
    ctx.rectangle(X, Y + H // 2, 1, HH)                              # aristas laterales
    ctx.rectangle(X + W - 1, Y + H // 2, 1, HH)
    ctx.set_source_rgb(*shade(left, 0.6))
    ctx.fill()
    ctx.restore()


def pixels(ctx, pts, color, alpha: float = 1.0, size=(1, 1)):
    for x, y in pts:
        ctx.rectangle(int(x), int(y), *size)
    ctx.set_source_rgba(*color, alpha)
    ctx.fill()


def _diamond_pts(w: int, oy: int = 0):
    """Todos los pixels de un rombo (para texturas)."""
    return [(w // 2 - half + c, oy + r) for r, half in _diamond_rows(w) for c in range(2 * half)]


def _speckle(ctx, w: int, oy: float, color, n: int, seed: int):
    """Pixels sueltos sobre la tapa de un rombo (textura). En alta resolución
    son más y más chicos: la textura se ve más fina, no más gruesa."""
    u = _unit(ctx)
    rng = random.Random(seed)
    W = w * u
    rows = list(_diamond_rows(W))
    X, Y = ctx.user_to_device(0, oy)
    ctx.save()
    ctx.identity_matrix()
    lo = min(u, max(0, len(rows) // 2 - 1))       # no pegarse al borde (si el rombo da)
    for _ in range(n * u):
        r, half = rows[rng.randrange(lo, max(lo + 1, len(rows) - lo))]
        ctx.rectangle(round(X) + W // 2 - half + u + rng.randrange(max(1, 2 * half - 2 * u)), round(Y) + r, 1, 1)
    ctx.set_source_rgb(*color)
    ctx.fill()
    ctx.restore()


ROOF_RISE = {0: 2, 1: 1, 2: 1, 3: 3, 4: 2}


def _roof(ctx, ox: int, oy: int, bw: int, style: int, color):
    """Techo sobre una caja de ancho bw cuya tapa está en (ox, oy)."""
    if style == 1:                                   # terraza con pretil
        prism(ctx, ox, oy - 1, bw, 1, color)
        diamond(ctx, ox + 2, oy, bw - 4, shade(color, 0.85))
        return
    steps = bw // 4
    rise = ROOF_RISE.get(style, 2)
    for k in range(steps):
        hh = 2 if style == 2 and k == 0 else rise
        prism(ctx, ox + 2 * k, oy - rise * k - hh + 1, bw - 4 * k, hh, shade(color, 1 + 0.06 * k))
    top = oy - rise * steps
    if style == 2:
        pixels(ctx, [(ox + bw // 2 - 1, top)], GOLD, size=(2, 2))               # remate dorado
    if style == 3:
        pixels(ctx, [(ox + bw // 2, top - 2)], shade(color, 0.6), size=(1, 3))  # aguja


def house_geometry(level: int, style: int) -> tuple[int, int, int]:
    """(ancho, alto del cuerpo, aire arriba para el techo) de una casa."""
    bw = 8 if level <= 2 or style == 4 else 12
    bh = (2 + 2 * level) if style == 4 else 3 + 3 * level
    return bw, bh, 4 + ROOF_RISE[style] * (bw // 4) + 3


# -- sprites -----------------------------------------------------------------
# Cada sprite es (superficie, dx, dy): se pega en (ancla_x + dx, ancla_y + dy).
# Terreno y pavimento: ancla = vértice superior del rombo.
# Objetos: ancla = centro de la tapa de la casilla (ya elevada).
# Los edificios se dibujan sobre una "base" en y = 36 del sprite (= centro de casilla).

OBJ_H = 40          # alto estándar de los sprites de edificio
OBJ_BASE = 36       # y del centro de la casilla dentro del sprite


def _obj(w: int = 18):
    s, ctx = _surface(w, OBJ_H)
    return s, ctx, (w - 16) // 2     # ox: desplazamiento para centrar un rombo de 16


class Sprites:
    def __init__(self, u: int = 1):
        self.u = u
        self._cache: dict[tuple, tuple] = {}

    def get(self, key: tuple):
        """(superficie nativa, dx, dy en unidades)."""
        global _U
        sp = self._cache.get(key)
        if sp is None:
            prev, _U = _U, self.u
            try:
                sp = self._cache[key] = getattr(self, "_make_" + key[0])(*key[1:])
            finally:
                _U = prev
        return sp

    @staticmethod
    def _anchor(s, w: int = 18):
        return s, -w // 2, -OBJ_BASE

    # terreno y naturaleza ---------------------------------------------------

    def _make_terrain(self, t: int, e: int, variant: int, special: str, season: str):
        top = shade(rgb(_GROUND[season][t]), (0.97, 1.0, 1.03)[variant % 3])
        top = {"snow": SNOW, "scorched": SCORCHED, "lava": LAVA}.get(special, top)
        s, ctx = _surface(TW, TH + BASE + e)
        prism(ctx, 0, 0, TW, BASE + e, top, outline=False)   # sin contorno: si no, se ve la grilla
        if special == "lava":
            _speckle(ctx, TW, 0, rgb("#ffd25a"), 5, variant)
            _speckle(ctx, TW, 0, rgb("#c2410c"), 6, variant + 9)
        elif special == "scorched":
            _speckle(ctx, TW, 0, rgb("#1f1814"), 6, variant)
        elif t != WATER:
            _speckle(ctx, TW, 0, shade(top, 1.1), 5, variant * 31 + t)
            _speckle(ctx, TW, 0, shade(top, 0.88), 4, variant * 17 + t)
        return s, -TW // 2, -e

    def _make_tree(self, kind: str, variant: int, season: str):
        s, ctx = _surface(8, 15)
        rng = random.Random(variant)
        leaves = _LEAVES[season]
        if kind == "birch":                                    # abedul: tronco blanco, copa liviana
            pixels(ctx, [(4, 6)], rgb("#ece8dc"), size=(1, 9))
            pixels(ctx, [(4, 8), (4, 11)], rgb("#3a3a3a"))
            if season == "invierno":
                pixels(ctx, [(3, 5), (5, 4), (2, 3), (6, 6)], rgb("#ece8dc"))
                return s, -4, -14
            col = {"otoño": rgb("#e8c040"), "primavera": rgb("#a6d06a")}.get(season, rgb("#7cb04e"))
            prism(ctx, 2, 1, 4, 5, col)
            _speckle(ctx, 4, 1, shade(col, 1.15), 2, variant)
            return s, -4, -14
        if kind == "cypress":                                  # ciprés: alto, angosto y oscuro
            pixels(ctx, [(3, 12)], TRUNK, size=(2, 3))
            prism(ctx, 2, 3, 4, 8, rgb("#2b4f36"))
            prism(ctx, 2, 0, 4, 2, rgb("#335c40"))
            if season == "invierno":
                pixels(ctx, [(3, 0), (4, 1), (3, 4)], SNOW)
            return s, -4, -14
        if kind == "palm":                                     # palmera: tronco curvo y hojas en abanico
            trunk = [(3, 14), (3, 13), (3, 12), (4, 11), (4, 10), (4, 9), (5, 8), (5, 7), (5, 6)]
            pixels(ctx, trunk, rgb("#9a7a4a"))
            fronds = [(2, 5), (3, 4), (4, 4), (6, 4), (7, 5), (1, 6), (8, 6), (5, 3), (5, 5), (4, 5), (6, 5)]
            pixels(ctx, fronds, rgb("#3f8f3a"))
            pixels(ctx, [(5, 6), (6, 6)], rgb("#7a4a2a"))      # cocos
            return s, -4, -14
        if kind == "dead":                                     # árbol seco
            pixels(ctx, [(4, 6)], rgb("#6a5a4a"), size=(1, 9))
            pixels(ctx, [(3, 5), (2, 4), (5, 7), (6, 6), (6, 5), (3, 8), (5, 3)], rgb("#6a5a4a"))
            return s, -4, -14
        if kind in ("oak", "fruit") and leaves:                # roble frondoso / frutal
            pixels(ctx, [(3, 10)], TRUNK, size=(2, 5))
            green = rgb(leaves[rng.randrange(len(leaves))]) if kind == "oak" or season == "otoño" else rgb("#4f8f3e")
            prism(ctx, 0, 3, 8, 5, green)
            prism(ctx, 2, 1, 4, 2, shade(green, 1.12))
            if kind == "fruit" and season in ("verano", "otoño"):
                pixels(ctx, [(2, 5), (5, 6), (6, 4), (3, 8)], rgb("#e74c3c" if season == "verano" else "#f39c12"))
            elif kind == "fruit" and season == "primavera":
                _speckle(ctx, 8, 3, rgb("#fbe3ee"), 5, variant)
            return s, -4, -14
        if kind == "pine":
            pixels(ctx, [(3, 11)], TRUNK, size=(2, 4))
            for k, (w, y) in enumerate(((8, 7), (8, 4), (4, 2))):
                prism(ctx, (8 - w) // 2, y, w, 2, shade(PINE, 1 + 0.08 * k))
            if season == "invierno":
                pixels(ctx, [(3, 2), (4, 2), (2, 5), (5, 5), (1, 8), (6, 8)], SNOW)
            return s, -4, -14
        pixels(ctx, [(3, 9)], TRUNK, size=(2, 6))
        if not leaves:                                   # invierno: ramas peladas con nieve
            pixels(ctx, [(2, 6), (5, 5), (1, 4), (6, 7), (3, 3), (4, 4)], TRUNK)
            pixels(ctx, [(1, 3), (6, 4), (3, 2)], SNOW)
            return s, -4, -14
        green = rgb(leaves[rng.randrange(len(leaves))])
        prism(ctx, 0, 4, 8, 4, green)
        prism(ctx, 2, 2, 4, 1, shade(green, 1.15))
        if season == "primavera":
            _speckle(ctx, 8, 4, rgb("#f2a6c8"), 4, variant)
        elif season == "otoño":
            _speckle(ctx, 8, 4, rgb("#e8b84a"), 3, variant)
        return s, -4, -14

    def _make_decor(self, kind: str, variant: int, season: str):
        s, ctx = _surface(8, 6)
        rng = random.Random(variant)
        if kind == "rock":
            prism(ctx, 2, 2, 4, 1, rgb("#9a958e"))
        elif kind == "bush":
            leaves = _LEAVES[season] or ["#7a6a4a"]
            prism(ctx, 1, 2, 4, 1, rgb(leaves[0]))
            prism(ctx, 3, 3, 4, 1, shade(rgb(leaves[-1]), 0.9))
        else:  # flores
            cols = ["#f4d03f", "#e74c3c", "#f5f5f5", "#af7ac5"] if season != "invierno" else ["#f5f5f5"]
            pixels(ctx, [(rng.randrange(1, 7), rng.randrange(2, 5)) for _ in range(5)], rgb(rng.choice(cols)))
            pixels(ctx, [(rng.randrange(1, 7), rng.randrange(2, 5)) for _ in range(3)], rgb(rng.choice(cols)))
        return s, -4, -3

    # pavimentos (sobre la tapa de la casilla) -------------------------------

    def _make_paving(self, kind: str, season: str, variant: int):
        s, ctx = _surface(TW, TH)
        base = {"street": COBBLE, "plaza": rgb("#cbbfa6"), "yard": DIRT}[kind]
        if season == "invierno":
            base = mix(base, SNOW, 0.5)
        diamond(ctx, 0, 0, TW, base)
        rng = random.Random(variant)
        joints = [p for p in _diamond_pts(TW) if (p[0] + 2 * p[1]) % 4 == 0 and rng.random() < 0.7]
        pixels(ctx, joints, shade(base, 0.85))
        if kind == "plaza":
            pixels(ctx, [p for p in _diamond_pts(TW) if (p[0] // 2 + p[1]) % 2 == 0][::3], shade(base, 1.07))
        return s, -TW // 2, 0

    def _make_road(self, mask: int, season: str):
        """Camino de tierra. mask: bits 1=x+1, 2=x-1, 4=y+1, 8=y-1."""
        s, ctx = _surface(TW, TH)
        dirt = DIRT if season != "invierno" else rgb("#c8bfae")
        ends = {1: (12, 6), 2: (4, 2), 4: (4, 6), 8: (12, 2)}
        pts = [(7, 3), (8, 4), (7, 4), (8, 3)]
        for bit, (ex, ey) in ends.items():
            if mask & bit:
                for k in range(13):
                    t = k / 12
                    pts.append((round(8 + (ex - 8) * t) - (1 if ex < 8 else 0), round(4 + (ey - 4) * t)))
        pixels(ctx, pts, dirt, size=(2, 1))
        pixels(ctx, pts[::5], shade(dirt, 0.8))
        return s, -TW // 2, 0

    def _make_pasture(self, season: str):
        s, ctx = _surface(TW, TH)
        edge = [(x, y) for x, y in _diamond_pts(TW) if (x + y) % 2 == 0 and
                (y in (0, TH - 1) or abs(x - TW // 2) >= TW // 2 - 2 - min(y, TH - 1 - y) * 2)]
        pixels(ctx, edge, WOOD)
        return s, -TW // 2, 0

    # campo y casas ----------------------------------------------------------

    def _make_field(self, crop: str, color: tuple, season: str):
        s, ctx = _surface(12, 6)
        base, row = {
            "wheat": (rgb("#d8c063"), rgb("#b89f45")),
            "vine": (rgb("#8a7a4a"), rgb("#4f8a3a")),
            "pumpkin": (rgb("#7a8a3e"), rgb("#5f6e2e")),
            "fallow": (rgb("#9a7a52"), rgb("#7c5f3e")),
        }[crop]
        if season == "invierno":
            base, row = rgb("#d8dde0"), rgb("#9a8a6e")
        diamond(ctx, 0, 0, 12, base)
        for r, half in _diamond_rows(12):            # surcos
            if r % 2:
                ctx.rectangle(6 - half, r, 2 * half, 1)
        ctx.set_source_rgb(*row)
        ctx.fill()
        if crop == "pumpkin" and season != "invierno":
            pixels(ctx, [(3, 2), (7, 3), (5, 4), (8, 1)], rgb("#f39c12"))
        pixels(ctx, [(0, 1)], color, size=(1, 2))   # mojón con el color de la facción
        return s, -6, -3

    def _make_house(self, level: int, style: int, color: tuple, material: int, variant: int):
        bw, bh, rh = house_geometry(level, style)
        wl, wr = MATERIALS[material]
        if style == 4:
            color = mix(rgb("#c9a24a"), color, 0.35)    # paja teñida
        s, ctx = _surface(bw + 4, rh + bw // 2 + bh)
        ox = 2
        if variant == 5 and level <= 2:                  # anexo
            prism(ctx, ox + bw - 6, rh + bh - 1, 4, 2, color, wl, wr)
        prism(ctx, ox, rh, bw, bh, color, wl, wr)
        if material == 2:                                # vigas de madera
            pixels(ctx, [(ox + 1, rh + bw // 4 + 2), (ox + bw - 2, rh + bw // 4 + 2)], rgb("#6b4a2b"),
                   size=(1, max(1, bh - 2)))
        _roof(ctx, ox, rh, bw, style, color)
        pixels(ctx, self.house_windows(level, style), WINDOW)
        pixels(ctx, [(ox + bw // 2 - 2, rh + bw // 2 + bh - 3)], DARK_WOOD, size=(1, 2))  # puerta
        if variant in (0, 4):                            # chimenea
            pixels(ctx, [(ox + bw - 3, rh - 3)], rgb("#7a5a4a"), size=(2, 4))
        elif variant == 1:                               # estandarte
            pixels(ctx, [(ox + 1, rh - 4)], shade(color, 0.7), size=(1, 5))
            pixels(ctx, [(ox + 2, rh - 4)], color, size=(2, 2))
        elif variant == 2:                               # huerta
            pixels(ctx, [(0, rh + bw // 2 + bh - 2), (1, rh + bw // 2 + bh - 1)], rgb("#4f9a3e"), size=(2, 1))
        return s, -bw // 2 - 2, -(rh + bh + bw // 4)

    @staticmethod
    def house_windows(level: int, style: int):
        bw, bh, rh = house_geometry(level, style)
        floors = max(1, level if style != 4 else 1)
        pts = []
        for f in range(floors):
            for c in ((2,) if bw == 8 else (2, 4)):
                y = rh + bw // 4 + c // 2 + 3 + 3 * f
                if y < rh + bw // 2 + bh - 2:
                    pts += [(2 + c, y), (2 + bw - 1 - c, y)]
        return pts

    @staticmethod
    def chimney_top(level: int, style: int) -> tuple[int, int]:
        bw, bh, rh = house_geometry(level, style)
        return bw // 2 - 1, -(rh + bh + bw // 4) + rh - 4

    def _make_lights(self, level: int, style: int):
        """Solo las ventanas encendidas, mismo encuadre que la casa."""
        surf, dx, dy = self.get(("house", level, style, STONE, 0, 3))
        s, ctx = _surface(surf.get_width() / self.u, surf.get_height() / self.u)
        pixels(ctx, self.house_windows(level, style), WINDOW_LIT)
        return s, dx, dy

    def _make_scaffold(self):
        """Obra en construcción: paredes a medio levantar y andamios."""
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 4, b - 6, 8, 2, rgb("#d8cfc0"), rgb("#d8cfc0"), rgb("#b8af9f"))
        posts = [(ox + 4, b - 12), (ox + 11, b - 12), (ox + 7, b - 14), (ox + 8, b - 9)]
        pixels(ctx, posts, WOOD, size=(1, 9))
        pixels(ctx, [(ox + 4, b - 12), (ox + 8, b - 14)], WOOD, size=(5, 1))
        pixels(ctx, [(ox + 2, b - 1), (ox + 3, b - 1)], rgb("#c0573a"), size=(2, 1))   # pila de ladrillos
        return self._anchor(s)

    # edificios públicos -------------------------------------------------------

    def _make_palace(self, style: int, color: tuple, material: int):
        wl, wr = MATERIALS[material]
        s, ctx = _surface(16, 46)
        prism(ctx, 0, 30, 16, 8, color, wl, wr)          # cuerpo
        _roof(ctx, 0, 30, 16, 1, color)
        prism(ctx, 4, 18, 8, 12, color, wl, wr)          # torre central
        _roof(ctx, 4, 18, 8, style if style != 4 else 0, color)
        for x in (1, 14):                                # estandartes
            pixels(ctx, [(x, 20)], rgb("#4a3b2a"), size=(1, 10))
            pixels(ctx, [(x + 1 if x == 1 else x - 2, 20)], color, size=(2, 3))
        pixels(ctx, [(6, 26), (9, 26)], WINDOW_LIT, size=(1, 2))
        pixels(ctx, [(7, 40)], DARK_WOOD, size=(2, 3))
        return s, -8, -42

    def _make_temple(self, style: int, color: tuple):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox, b - 4, 16, 1, shade(MARBLE, 0.9))                      # escalinata
        prism(ctx, ox + 2, b - 14, 12, 9, MARBLE)                             # nave
        _roof(ctx, ox + 2, b - 14, 12, 2 if style in (0, 2) else 0, color)
        prism(ctx, ox + 10, b - 24, 4, 12, MARBLE)                            # campanario
        _roof(ctx, ox + 10, b - 24, 4, 3, color)
        pixels(ctx, [(ox + 11, b - 20)], GOLD, size=(2, 2))                   # campana
        pixels(ctx, [(ox + 6, b - 5)], DARK_WOOD, size=(2, 4))                # portón
        pixels(ctx, [(ox + 4, b - 9), (ox + 13, b - 8)], rgb("#9fc5e8"), size=(1, 2))   # vitrales
        return self._anchor(s)

    def _make_barracks(self, color: tuple):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        diamond(ctx, ox, b - 4, 16, DIRT)                                     # patio de armas
        prism(ctx, ox, b - 11, 12, 5, rgb("#9a948a"))
        _roof(ctx, ox, b - 11, 12, 1, shade(color, 0.75))
        pixels(ctx, [(ox + 13, b - 6), (ox + 14, b - 5), (ox + 15, b - 4)], rgb("#cfcfcf"), size=(1, 3))  # lanzas
        pixels(ctx, [(ox + 2, b - 18)], DARK_WOOD, size=(1, 8))
        pixels(ctx, [(ox + 3, b - 18)], color, size=(3, 3))
        pixels(ctx, [(ox + 11, b - 2)], rgb("#c49a6c"), size=(2, 2))          # muñeco de práctica
        return self._anchor(s)

    def _make_library(self, color: tuple, material: int):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        wl, wr = MATERIALS[material]
        prism(ctx, ox + 2, b - 18, 12, 12, wl, wl, wr)
        _roof(ctx, ox + 2, b - 18, 12, 1, color)
        prism(ctx, ox + 5, b - 22, 4, 2, rgb("#7fa3b8"))                     # cúpula del observatorio
        for x in (ox + 3, ox + 5, ox + 7):
            pixels(ctx, [(x, b - 13 + (x - ox) // 2)], MARBLE, size=(1, 7))   # columnas
        pixels(ctx, [(ox + 10, b - 12), (ox + 12, b - 13)], WINDOW_LIT, size=(1, 2))
        return self._anchor(s)

    def _make_watchtower(self, color: tuple):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        pixels(ctx, [(ox + 6, b - 16), (ox + 9, b - 16)], WOOD, size=(1, 16))
        pixels(ctx, [(ox + 7, b - 10)], WOOD, size=(2, 1))
        prism(ctx, ox + 4, b - 19, 8, 1, shade(WOOD, 1.1))
        _roof(ctx, ox + 4, b - 20, 8, 0, rgb("#6b4a2b"))
        pixels(ctx, [(ox + 8, b - 30)], DARK_WOOD, size=(1, 6))
        pixels(ctx, [(ox + 9, b - 30)], color, size=(3, 2))
        return self._anchor(s)

    def _make_lighthouse(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 4, b - 4, 8, 2, STONE)
        for k in range(4):                                     # torre a franjas rojas y blancas
            prism(ctx, ox + 6, b - 8 - 5 * k, 4, 5, rgb("#c0392b") if k % 2 else MARBLE)
        prism(ctx, ox + 5, b - 28, 6, 2, rgb("#f5d76e"))         # linterna
        _roof(ctx, ox + 5, b - 28, 4, 0, rgb("#5a4a4a"))
        return self._anchor(s)

    def _make_monument(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 4, b - 4, 8, 2, STONE)
        prism(ctx, ox + 6, b - 24, 4, 20, shade(MARBLE, 0.95))
        pixels(ctx, [(ox + 7, b - 26)], GOLD, size=(2, 2))
        return self._anchor(s)

    def _make_well(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 6, b - 3, 4, 2, STONE)
        pixels(ctx, [(ox + 7, b - 2)], rgb("#3f74b8"), size=(2, 1))
        pixels(ctx, [(ox + 6, b - 8), (ox + 9, b - 8)], WOOD, size=(1, 6))
        prism(ctx, ox + 4, b - 10, 8, 1, rgb("#7a4a2a"))
        return self._anchor(s)

    def _make_fountain(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 4, b - 4, 8, 2, STONE)
        diamond(ctx, ox + 6, b - 3, 4, rgb("#5a9ad6"))
        pixels(ctx, [(ox + 7, b - 8)], STONE, size=(2, 5))
        return self._anchor(s)

    def _make_garden(self, season: str):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        leaves = _LEAVES[season] or ["#8a9a8a"]
        hedge = [(x + ox, y + b - 4) for x, y in _diamond_pts(16) if (x + y) % 3 == 0]
        pixels(ctx, hedge[::2], rgb(leaves[0]))
        pixels(ctx, [(ox + 5, b - 2), (ox + 10, b - 1), (ox + 8, b + 1)], rgb("#e74c3c"))
        pixels(ctx, [(ox + 6, b), (ox + 11, b - 3)], rgb("#f4d03f"))
        pixels(ctx, [(ox + 7, b - 2)], rgb("#e8e4da"), size=(2, 1))         # banco
        return self._anchor(s)

    def _make_graveyard(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        diamond(ctx, ox + 2, b - 3, 12, rgb("#5f7a4f"))
        stones = [(ox + 5, b - 3), (ox + 8, b - 2), (ox + 11, b - 2), (ox + 6, b), (ox + 9, b + 1)]
        pixels(ctx, stones, rgb("#a8a8a8"), size=(1, 2))
        pixels(ctx, [(ox + 8, b - 7), (ox + 7, b - 6)], rgb("#a8a8a8"), size=(1, 1))
        pixels(ctx, [(ox + 8, b - 6)], rgb("#a8a8a8"), size=(1, 4))          # cruz de la entrada
        return self._anchor(s)

    def _make_lumber(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        prism(ctx, ox + 2, b - 9, 8, 4, rgb("#9a7a52"))
        _roof(ctx, ox + 2, b - 9, 8, 0, rgb("#6b4a2b"))
        for x, y in ((ox + 10, b - 2), (ox + 10, b - 4), (ox + 11, b - 1)):
            pixels(ctx, [(x, y)], rgb("#8a5a2a"), size=(5, 1))                  # troncos
            pixels(ctx, [(x + 4, y)], rgb("#d9b98a"))
        pixels(ctx, [(ox + 4, b), (ox + 7, b + 1)], rgb("#7a5a3a"), size=(2, 1))  # tocones
        return self._anchor(s)

    def _make_dock(self):
        s, ctx, ox = _obj()
        b = OBJ_BASE
        diamond(ctx, ox, b - 4, 16, WOOD)
        planks = [(x + ox, y + b - 4) for x, y in _diamond_pts(16) if (x - 2 * y) % 4 == 0]
        pixels(ctx, planks, shade(WOOD, 0.75))
        pixels(ctx, [(ox + 1, b - 1), (ox + 14, b - 1), (ox + 7, b + 3)], DARK_WOOD, size=(1, 3))
        pixels(ctx, [(ox + 9, b - 3)], rgb("#c49a6c"), size=(2, 2))          # barril
        return self._anchor(s)

    def _make_tent(self, color: tuple):
        """Tienda de una embajada extranjera."""
        s, ctx, ox = _obj()
        b = OBJ_BASE
        for k in range(4):
            prism(ctx, ox + 2 + 2 * k, b - 6 - 2 * k, 12 - 4 * k if 12 - 4 * k >= 4 else 4, 2,
                  color if k % 2 == 0 else rgb("#f0e6d2"))
        pixels(ctx, [(ox + 7, b - 3)], rgb("#3a2a2a"), size=(2, 3))
        pixels(ctx, [(ox + 8, b - 20)], DARK_WOOD, size=(1, 8))
        pixels(ctx, [(ox + 9, b - 20)], color, size=(3, 2))
        return self._anchor(s)

    def _make_windmill(self, color: tuple):
        s, ctx = _surface(8, 18)
        prism(ctx, 0, 6, 8, 8, rgb("#e8e0d0"), rgb("#e8e0d0"), rgb("#c8c0b0"))
        prism(ctx, 2, 3, 4, 3, color)
        return s, -4, -14

    # emblemas de las aldeas (apps) y edificios con forma de emblema -------------

    def _make_landmark(self, kind: str, color: tuple):
        s, ctx = _surface(18, 36)
        ox, base = 1, 32                                 # base = y del centro de la casilla
        if kind == "oraculo":
            prism(ctx, ox, base - 8, 16, 2, rgb("#e8e4da"))
            for x in (ox + 3, ox + 6, ox + 9, ox + 12):
                pixels(ctx, [(x, base - 14)], rgb("#f4f1ea"), size=(1, 7))
            prism(ctx, ox + 2, base - 16, 12, 1, rgb("#f4f1ea"))
            _roof(ctx, ox + 2, base - 16, 12, 0, rgb("#efe9dc"))
            pixels(ctx, [(ox + 7, base - 11)], rgb("#b46cff"), size=(2, 2))
        elif kind == "arena":
            prism(ctx, ox, base - 10, 16, 6, rgb("#c9a77c"))
            diamond(ctx, ox + 4, base - 8, 8, rgb("#e6cf8a"))
            for x in (ox + 1, ox + 15):
                pixels(ctx, [(x, base - 17)], rgb("#4a3b2a"), size=(1, 7))
                pixels(ctx, [(x + 1 if x == ox + 1 else x - 2, base - 17)], color, size=(2, 2))
        elif kind == "atalaya":
            pixels(ctx, [(ox + 6, base - 20), (ox + 9, base - 20)], rgb("#7a5a3a"), size=(1, 18))
            prism(ctx, ox + 4, base - 24, 8, 2, WOOD)
            pixels(ctx, [(ox + 7, base - 28)], rgb("#ffb03a"), size=(2, 3))
        elif kind == "molino":
            prism(ctx, ox + 4, base - 14, 8, 10, rgb("#e8e0d0"))
            _roof(ctx, ox + 4, base - 14, 8, 0, color)
        elif kind == "granero":
            prism(ctx, ox + 2, base - 11, 12, 6, rgb("#b0412e"), rgb("#b0412e"), rgb("#8e3324"))
            _roof(ctx, ox + 2, base - 11, 12, 0, rgb("#5a4a3a"))
            pixels(ctx, [(ox + 5, base - 2)], rgb("#f0e6d2"), size=(2, 3))
        elif kind == "mercado":
            for k, (x, y) in enumerate(((ox, base - 8), (ox + 8, base - 8), (ox + 4, base - 5))):
                awn = (color, rgb("#f0e6d2"), rgb("#e0b030"))[k]
                pixels(ctx, [(x + 1, y + 2), (x + 6, y + 2)], rgb("#6b4a2b"), size=(1, 4))
                prism(ctx, x, y, 8, 1, awn)
                pixels(ctx, [(x + 3, y + 4)], rgb("#e07b24"), size=(2, 1))
        elif kind == "herreria":
            prism(ctx, ox + 2, base - 10, 12, 5, rgb("#8a8580"))
            _roof(ctx, ox + 2, base - 10, 12, 1, rgb("#5a5550"))
            pixels(ctx, [(ox + 11, base - 16)], rgb("#5a4a4a"), size=(2, 6))
            pixels(ctx, [(ox + 4, base - 2)], rgb("#ff8a2a"), size=(2, 2))
        elif kind == "teatro":
            prism(ctx, ox + 2, base - 12, 12, 7, rgb("#d8c8a8"))
            _roof(ctx, ox + 2, base - 12, 12, 2, rgb("#a8323a"))
            pixels(ctx, [(ox + 6, base - 4)], rgb("#3a1a1a"), size=(4, 3))
        else:  # taberna
            prism(ctx, ox + 2, base - 10, 12, 6, rgb("#d9c39a"), rgb("#d9c39a"), rgb("#b8a27a"))
            _roof(ctx, ox + 2, base - 10, 12, 0, rgb("#7a4a2a"))
            pixels(ctx, [(ox, base - 7)], rgb("#e0b030"), size=(2, 2))
            pixels(ctx, [(ox + 14, base - 1), (ox + 15, base)], rgb("#6b4a2b"), size=(2, 2))
        return s, -9, -base

    # fortalezas -------------------------------------------------------------

    def _make_castle(self, kind: str, alive: bool):
        s, ctx = _surface(16, 40)
        base = 36                                        # y del centro de la casilla
        if not alive:
            prism(ctx, 2, base - 6, 12, 4, shade(RUBBLE, 0.9))
            pixels(ctx, [(4, base - 4), (9, base - 5), (11, base - 1)], shade(RUBBLE, 0.6))
            return s, -8, -base
        if kind == "db":                                 # Bóveda: maciza con cúpula
            prism(ctx, 0, base - 10, 16, 7, STONE)
            _roof(ctx, 0, base - 10, 16, 2, rgb("#6d7d96"))
        elif kind == "daemon":                           # Bastión: torre alta
            prism(ctx, 4, base - 22, 8, 18, STONE)
            for cx, cy in ((6, base - 24), (4, base - 23), (8, base - 23), (6, base - 22)):
                prism(ctx, cx, cy, 4, 1, shade(STONE, 1.08))
        else:                                            # Ciudadela: torreón con almenas
            prism(ctx, 2, base - 14, 12, 12, STONE)
            for cx, cy in ((6, base - 16), (2, base - 14), (10, base - 14), (6, base - 12)):
                prism(ctx, cx, cy, 4, 2, shade(STONE, 1.08))
        fx, fy = self.castle_flag(kind)
        pixels(ctx, [(8 + fx - 3, base + fy)], rgb("#4a3b2a"), size=(1, 9))
        pixels(ctx, [(8 + fx - 2, base + fy)], FLAG.get(kind, STONE), size=(3, 2))
        return s, -8, -base

    @staticmethod
    def castle_flag(kind: str) -> tuple[int, int]:
        """Punta de la bandera relativa al centro de la casilla (para animarla)."""
        return 3, {"db": -26, "daemon": -33}.get(kind, -27)

    def _make_wall(self):
        s, ctx = _surface(12, 6 + 5)
        prism(ctx, 0, 0, 12, 5, WALL_STONE)
        pixels(ctx, [(2, 3), (5, 4), (9, 3), (6, 1)], shade(WALL_STONE, 0.7))
        return s, -6, -(5 + 3)

    def _make_tower(self, floors: int, glow: bool):
        bh = 6 * floors + 2
        s, ctx = _surface(8, 14 + bh)
        prism(ctx, 0, 10, 8, bh, TOWER_STONE)
        _roof(ctx, 0, 10, 8, 3, rgb("#4b3a86"))
        win = WINDOW_LIT if glow else WINDOW
        pixels(ctx, [(2, 10 + 2 + 3 + 6 * f) for f in range(floors)], win, size=(1, 2))
        pixels(ctx, [(5, 10 + 2 + 3 + 6 * f) for f in range(floors)], win, size=(1, 2))
        return s, -4, -(10 + bh + 2)

    def _make_mine(self, active: bool):
        s, ctx = _surface(14, 12)
        prism(ctx, 1, 2, 12, 3, rgb("#7a5a3a"))
        pixels(ctx, [(5, 3)], rgb("#120d0a"), size=(4, 4))
        pixels(ctx, [(4, 2), (9, 2)], DARK_WOOD, size=(1, 5))
        pixels(ctx, [(5, 9), (7, 10), (9, 11)], rgb("#555555"), size=(2, 1))   # rieles
        if active:
            pixels(ctx, [(6, 4)], rgb("#ffb03a"))
        return s, -7, -9

    def _make_rubble(self, variant: int):
        s, ctx = _surface(12, 6)
        rng = random.Random(variant)
        pixels(ctx, [(rng.randrange(2, 10), rng.randrange(1, 5)) for _ in range(7)], RUBBLE)
        pixels(ctx, [(rng.randrange(2, 10), rng.randrange(1, 5)) for _ in range(3)], shade(RUBBLE, 0.7))
        return s, -6, -3

    def _make_crater(self, active: bool):
        s, ctx = _surface(8, 4)
        diamond(ctx, 0, 0, 8, rgb("#3b2b2b"))
        diamond(ctx, 2, 1, 4, LAVA if active else rgb("#5a4040"))
        return s, -4, -2

    def _make_boat(self, flip: bool, color: tuple):
        s, ctx = _surface(9, 9)
        pixels(ctx, [(1, 6), (2, 7), (3, 7), (4, 7), (5, 7), (6, 7), (7, 6)], rgb("#6b4a2b"))
        pixels(ctx, [(2, 6)], WOOD, size=(5, 1))
        pixels(ctx, [(4, 0)], rgb("#4a3b2a"), size=(1, 6))
        sail = [(5, 1), (5, 2), (6, 2), (5, 3), (6, 3), (7, 3), (5, 4), (6, 4)] if not flip else \
               [(3, 1), (3, 2), (2, 2), (3, 3), (2, 3), (1, 3), (3, 4), (2, 4)]
        pixels(ctx, sail, color)
        return s, -4, -7

    def _make_cloud(self, variant: int):
        rng = random.Random(variant)
        w = 28 + rng.randrange(20)
        s, ctx = _surface(w, 12)
        for _ in range(6):
            bw = rng.randrange(10, 18)
            x, y = rng.randrange(0, w - bw), rng.randrange(2, 7)
            ctx.rectangle(x, y, bw, 4)
            ctx.rectangle(x + 2, y - 2, bw - 4, 8)
        ctx.set_source_rgba(1, 1, 1, 0.55)
        ctx.fill()
        return s, 0, 0


# Qué sprite usa cada tipo de edificio (además de la casa)
_LANDMARK_BLD = {B.MARKET: "mercado", B.BARN: "granero", B.WORKSHOP: "herreria", B.TAVERN: "taberna"}
# Por dónde caminan los aldeanos
_WALKABLE = {B.STREET, B.PLAZA, B.WELL, B.MONUMENT, B.FARM, B.PASTURE, B.GARDEN, B.DOCK}
# Sprites planos: no tapan a nadie (no van al buffer de profundidad)
_FLAT_SPRITES = {"paving", "road", "field", "pasture", "dock", "garden", "graveyard", "rubble",
                 "crater", "decor", "tree"}   # los árboles no tapan a la gente: el movimiento se tiene que ver


# -- texto -------------------------------------------------------------------

FONT = "Liberation Mono"   # a 9 px sin antialias es la más legible; si no está, cae a monospace


def _font(ctx, size: int = 9):
    fo = cairo.FontOptions()
    fo.set_antialias(cairo.ANTIALIAS_NONE)
    fo.set_hint_style(cairo.HINT_STYLE_FULL)
    ctx.set_font_options(fo)
    ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)


def _wrap(ctx, text: str, width: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and ctx.text_extents(trial).x_advance > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


_MEASURE = _surface(1, 1, u=1)[1]
_font(_MEASURE)
TEXT = (0.93, 0.93, 0.9)
DIM = (0.62, 0.64, 0.68)


def _panel(lines: list[tuple[str, tuple, tuple | None]], width: int | None = None, title: str | None = None):
    """Panel semitransparente. lines = [(texto, color, cuadradito|None)]."""
    lh = 11
    w = width or int(max([_MEASURE.text_extents(t).x_advance + (8 if sq else 0) for t, _, sq in lines]
                         + [_MEASURE.text_extents(title).x_advance if title else 0]) + 12)
    h = len(lines) * lh + 6 + (lh + 2 if title else 0)
    s, ctx = _surface(w, h, u=1)
    ctx.rectangle(0, 0, w, h)
    ctx.set_source_rgba(0.05, 0.06, 0.1, 0.58)
    ctx.fill()
    _font(ctx)
    y = 3
    if title:
        ctx.move_to(5, y + lh - 2)
        ctx.set_source_rgb(*GOLD)
        ctx.show_text(title)
        y += lh + 2
    for text, col, sq in lines:
        x = 5
        if sq:
            pixels(ctx, [(x, y + 3)], sq, size=(5, 5))
            x += 8
        ctx.move_to(x, y + lh - 2)
        ctx.set_source_rgb(*col)
        ctx.show_text(text)
        y += lh
    return s


def _ocean_tile(color) -> cairo.ImageSurface:
    """Patrón de mar 64x64 que se repite (y se mueve con la cámara)."""
    s, ctx = _surface(64, 64, u=1)
    ctx.set_source_rgb(*color)
    ctx.paint()
    rng = random.Random(7)
    pixels(ctx, [(rng.randrange(62), rng.randrange(64)) for _ in range(5)], shade(color, 1.18), size=(3, 1))
    return s


# -- pintor ------------------------------------------------------------------

class Painter:
    def __init__(self, interval: float = 2.0, hemisphere: str = "auto"):
        self.u = 1                       # resolución de detalle (2 = sprites finos para zoom)
        self._sprites = {1: Sprites(1)}
        self.sprites = self._sprites[1]
        self.state: dict | None = None
        self.interval = interval
        self.hemisphere = hemisphere_auto() if hemisphere == "auto" else hemisphere
        self.world = None            # superficie de la isla (independiente de la cámara)
        self.frame_surf = None
        self.size = (0, 0)
        self.bolts: list[dict] = []
        self.hour_override: float | None = None
        self.season_override: str | None = None
        self.show_hud = True
        self.show_chronicle = True
        self.hover: tuple[int, int] | None = None
        self.card: dict | None = None
        self.pan = [0.0, 0.0]        # desplazamiento de cámara (px lógicos)
        self._sig = None
        self._state_sig = None
        self._ui_dirty = True
        self._bbox = (0, 0) + map_size()
        self._bbox_terrain = None
        self._elev: list[int] = []
        self._depth: list[int] = []
        self._coast: list[bool] = []
        self._ocean = (None, None)
        self.wox = self.woy = 0       # origen de la grilla dentro de la superficie del mundo
        self.cam = (0, 0)             # dónde cae la superficie del mundo en el frame
        self.hud = self.chron = None
        self.ui_surf = None
        self._ui_size = (0, 0)
        self._ui_drawn = None
        self.show_powers = True
        self.window_mode = False          # en la ventana se actúa; el fondo de pantalla es solo para mirar
        self.hud_scroll = 0
        self.chron_scroll = 0
        self.chron_mode = 1               # 0 oculta, 1 normal, 2 expandida
        self._hud_lines: dict[int, int] = {}
        self._rects: dict[str, tuple] = {}
        self.powers_panel = None
        self.power_rects: list[str] = []
        self._powers_pos = (0, 0)
        self.selected_power: str | None = None
        self._toast = None
        self._power_seen = 0
        self._reset_anim()

    def _reset_anim(self):
        self.burn_px: list[tuple[int, int]] = []
        self.lava_px: list[tuple[int, int]] = []
        self.flags: list[tuple[int, int, tuple]] = []
        self.mills: list[tuple[int, int]] = []
        self.smoke: list[tuple[int, int, float]] = []
        self.fountains: list[tuple[int, int]] = []
        self.sheep: list[tuple[int, int]] = []
        self.fishers: list[tuple[int, int]] = []
        self.envoys: list[tuple[int, int, tuple]] = []
        self.routes_px: list[list[tuple[int, int]]] = []
        self.volcano_px = None
        self.academy_px = None
        self.mine_px = None
        self.walk_routes: dict[int, list[dict]] = {}
        self.beacons: list[tuple[int, int]] = []
        self._zdata = None

    def set_detail(self, u: int):
        """1 = pixel art base; 2 = sprites al doble de resolución (para el zoom)."""
        if u != self.u:
            self.u = u
            self.sprites = self._sprites.setdefault(u, Sprites(u))
            self._sig = None
            self.frame_surf = None

    def _put(self, ctx, key, x: float, y: float, alpha: float = 1.0):
        """Pega un sprite (generado a resolución nativa) en coordenadas de unidades."""
        surf, dx, dy = self.sprites.get(key)
        U = self.u
        x, y = int(x) + dx, int(y) + dy
        pat = cairo.SurfacePattern(surf)
        pat.set_filter(cairo.FILTER_NEAREST)
        pat.set_matrix(cairo.Matrix(xx=U, yy=U, x0=-x * U, y0=-y * U))
        ctx.save()
        ctx.rectangle(x, y, surf.get_width() / U, surf.get_height() / U)
        ctx.clip()
        ctx.set_source(pat)
        ctx.paint_with_alpha(alpha)
        ctx.restore()
        return pat, (x, y, surf.get_width() / U, surf.get_height() / U)

    def _visible(self, x: float, y: float, z: int) -> bool:
        """¿Un punto a profundidad z queda tapado por algo dibujado más adelante?"""
        if self._zdata is None:
            return True
        U = self.u
        px, py = int(x * U), int(y * U)
        if not (0 <= px < self._zw and 0 <= py < self._zh):
            return True
        return self._zdata[py * self._zstride + px * 4 + 2] <= z

    # -- entrada de estado ---------------------------------------------------

    def set_state(self, state: dict, now: float):
        prev_tick = self.state["tick"] if self.state else None
        self.state = state
        self._state_t = now
        self.interval = state.get("interval", self.interval)
        self._ui_dirty = True
        if self._bbox_terrain != state["terrain"]:
            self._terrain_changed()
        tick = state["tick"]
        born = state.get("born") or []
        building = tuple(i for i, b in enumerate(born) if b and tick - b < 4 and state["owner"][i])
        structs = [(s["key"], s.get("alive"), s.get("walls"), s.get("floors"), bool(s.get("forging")),
                    s.get("dead_ticks", 0) < 600, s.get("activity", 0) > 0.05) for s in state["structures"]]
        facs = [(f["id"], tuple(f["color"]), f.get("alive"), tuple(f["capital"])) for f in state["factions"]]
        w = state["weather"]
        sig = (state["terrain"], state["owner"], state["level"], state["burn"], state["lava"], state["ruin"],
               state["wall"], state.get("road"), state.get("bld"), building, structs, facs,
               bool(w["storm"]), w["eruption"], w["fog"], w.get("heat"), w.get("plague"))
        if sig != self._state_sig:
            self._state_sig = sig
            self._sig = None
        if prev_tick is not None and tick != prev_tick:
            rng = random.Random(tick)
            for ev in state.get("events", []):
                self.bolts.append(dict(ev, start=now + rng.uniform(0, self.interval * 0.8), seed=rng.random()))

    def _terrain_changed(self):
        st = self.state
        n = st["w"]
        terrain = st["terrain"]
        # relieve: cuanto más adentro del macizo, más alto (BFS desde el borde)
        depth = [0 if t == MOUNTAIN else -1 for t in terrain]
        frontier = [i for i, t in enumerate(terrain) if t == MOUNTAIN
                    and any(terrain[j] != MOUNTAIN for j in _neigh(i, n, n))]
        for i in frontier:
            depth[i] = 1
        while frontier:
            nxt = []
            for i in frontier:
                for j in _neigh(i, n, n):
                    if depth[j] == 0:
                        depth[j] = depth[i] + 1
                        nxt.append(j)
            frontier = nxt
        self._depth = depth
        self._elev = [ELEV[t] + (2 * min(depth[i], 5) - 2 if t == MOUNTAIN else 0)
                      for i, t in enumerate(terrain)]
        xs, ys = [], []
        for i, t in enumerate(terrain):
            if t != WATER:
                x, y = i % n, i // n
                xs.append((x - y) * TW // 2)
                ys.append((x + y) * TH // 2)
        if not xs:
            xs, ys = [0], [0]
        self._bbox = (min(xs) - TW, min(ys) - 44, max(xs) + TW, max(ys) + TH + BASE + 6)
        self._bbox_terrain = terrain
        self._coast = self._coast_mask(terrain, n)
        self.wox = MARGIN - self._bbox[0]
        self.woy = MARGIN - self._bbox[1]
        self._sig = None

    def hour(self) -> float:
        if self.hour_override is not None:
            return self.hour_override
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60

    def season(self) -> str:
        return self.season_override or season_for(time.localtime().tm_mon, self.hemisphere)

    # -- coordenadas ---------------------------------------------------------
    # "mundo" = pixels de la superficie de la isla; "frame" = pixels lógicos de pantalla.

    def tile_xy(self, x: int, y: int, e: int = 0) -> tuple[int, int]:
        """Vértice superior de la casilla en coordenadas de mundo."""
        return self.wox + (x - y) * TW // 2, self.woy + (x + y) * TH // 2 - e

    def tile_at(self, wx: float, wy: float, e: int = 0) -> tuple[int, int]:
        mx, my = (wx - self.wox) / (TW / 2), (wy + e - self.woy) / (TH / 2)
        return math.floor((mx + my) / 2), math.floor((my - mx) / 2)

    def to_world(self, fx: float, fy: float) -> tuple[float, float]:
        return fx - self.cam[0], fy - self.cam[1]

    def elev_at(self, x: int, y: int) -> int:
        return self._elev[y * self.state["w"] + x]

    def world_size(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self._bbox
        return x1 - x0 + 2 * MARGIN, y1 - y0 + 2 * MARGIN

    def content_size(self) -> tuple[int, int]:
        """Tamaño (px lógicos) que tiene que entrar en pantalla: la isla + margen."""
        x0, y0, x1, y1 = self._bbox
        return x1 - x0 + 24, y1 - y0 + 24

    def _camera(self, fw: int, fh: int):
        ww, wh = self.world_size()
        self.pan[0] = max(-ww / 2, min(ww / 2, self.pan[0]))
        self.pan[1] = max(-wh / 2, min(wh / 2, self.pan[1]))
        self.cam = (int(fw / 2 - ww / 2 - self.pan[0]), int(fh / 2 - wh / 2 - self.pan[1]))

    def keep_point(self, world_pt, fx: float, fy: float, fw: int, fh: int):
        """Ajusta el paneo para que el punto de mundo quede bajo (fx, fy): zoom hacia el cursor."""
        ww, wh = self.world_size()
        self.pan[0] = fw / 2 - ww / 2 - (fx - world_pt[0])
        self.pan[1] = fh / 2 - wh / 2 - (fy - world_pt[1])

    def wants_fast(self, now: float) -> bool:
        if not self.state:
            return False
        w = self.state["weather"]
        return bool(w["storm"] or w["eruption"] or self.lava_px or self.bolts)

    # -- mundo (estático) ----------------------------------------------------

    def _rebuild_world(self):
        st = self.state
        U = self.u
        ww, wh = self.world_size()
        self.world, ctx = _surface(ww, wh, u=U)
        zsurf, zctx = _surface(ww, wh, u=U)      # buffer de profundidad: canal R = x+y+1 de la casilla
        night = night_factor(self.hour())
        season = self.season()
        self._reset_anim()
        if not st:
            return
        n = st["w"]
        tick = st["tick"]
        terrain, owner, level = st["terrain"], st["owner"], st["level"]
        burn, lava, ruin, wall = st["burn"], st["lava"], st["ruin"], st["wall"]
        road = st.get("road") or [0] * len(terrain)
        bld = st.get("bld") or [0] * len(terrain)
        born = st.get("born") or [0] * len(terrain)
        facs = {f["id"]: f for f in st["factions"]}
        colors = {fid: tuple(c / 255 for c in f["color"]) for fid, f in facs.items()}
        capitals = {tuple(f["capital"]): f for f in st["factions"] if f.get("alive")}
        structs = {(s["x"], s["y"]): s for s in st["structures"]
                   if s.get("alive") or s.get("dead_ticks", 0) < 600}
        vx, vy = st["volcano"]
        erupting = st["weather"]["eruption"]
        lit = night > 0.4
        lights: list[tuple[tuple, int, int]] = []
        cur_z = [0]
        walk: dict[int, set] = {}
        dests: dict[int, dict[str, list]] = {}

        def blit(key, ax, ay):
            pat, rect = self._put(ctx, key, ax, ay)
            solid = key[0] not in _FLAT_SPRITES and not (key[0] == "terrain" and key[2] <= 0)
            if solid:
                zctx.save()
                zctx.rectangle(*rect)
                zctx.clip()
                zctx.set_source_rgb(cur_z[0] / 255, 0, 0)
                zctx.mask(pat)
                zctx.restore()

        def place(fid, kind, i):
            dests.setdefault(fid, {}).setdefault(kind, []).append(i)

        for d in range(2 * n - 1):
            cur_z[0] = d + 1
            for x in range(max(0, d - n + 1), min(n, d + 1)):
                y = d - x
                i = y * n + x
                t = terrain[i]
                if t == WATER and not self._coast[i]:
                    continue                          # mar abierto = patrón de fondo
                sx, sy = self.tile_xy(x, y)
                e = self._elev[i]
                hsh = h32(x, y)
                var = hsh % 3
                special = ""
                if lava[i]:
                    special = "lava"
                    self.lava_px.append((sx, sy - e))
                elif burn[i]:
                    special = "scorched"
                    self.burn_px.append((sx, sy - e + 4))
                elif t == MOUNTAIN and self._depth[i] >= 4 and season != "verano":
                    special = "snow"
                blit(("terrain", t, e, var, special, season), sx, sy)
                if t == WATER:
                    continue
                cx, cy = sx, sy - e + TH // 2   # centro de la tapa
                o = owner[i]
                b = bld[i]
                s = structs.get((x, y))
                cap = capitals.get((x, y))
                if o and not lava[i] and not burn[i] and (b in _WALKABLE or road[i]) and not s:
                    walk.setdefault(o, set()).add(i)

                if (x, y) == (vx, vy):
                    blit(("crater", erupting), cx, cy)
                    self.volcano_px = (cx, cy)
                    continue
                if s:
                    self._draw_structure(blit, s, cx, cy, lit, i)
                    continue
                if road[i] and not lava[i] and not (o and b in (B.STREET, B.PLAZA)):
                    mask = 0
                    for bit, (ddx, ddy) in ((1, (1, 0)), (2, (-1, 0)), (4, (0, 1)), (8, (0, -1))):
                        xx, yy = x + ddx, y + ddy
                        if 0 <= xx < n and 0 <= yy < n and (road[yy * n + xx] or (xx, yy) in structs
                                                            or (xx, yy) in capitals
                                                            or bld[yy * n + xx] in (B.STREET, B.PLAZA)):
                            mask |= bit
                    blit(("road", mask, season), sx, sy - e)
                    continue
                if wall[i]:
                    blit(("wall",), cx, cy)
                    continue
                if not o or lava[i]:
                    self._draw_nature(blit, t, i, burn[i], ruin[i], var, hsh, cx, cy, season)
                    continue

                f = facs.get(o, {})
                col = colors.get(o, STONE)
                style, material = f.get("style", 0), f.get("material", 0)
                if cap and cap["id"] == o:
                    if cap.get("kind") == "app":
                        mark = cap.get("landmark") or "molino"
                        blit(("landmark", mark, col), cx, cy)
                        if mark == "molino":
                            self.mills.append((cx, cy - 18))
                        elif mark == "herreria":
                            self.smoke.append((cx + 3, cy - 18, 1.0))
                    else:
                        blit(("palace", style, col, material), cx, cy)
                        self.flags.append((cx - 4, cy - 22, col))
                    place(o, "palace", i)
                    continue
                if burn[i]:
                    continue
                if tick - born[i] < 4 and b not in (B.STREET, B.PLAZA, B.FARM, B.PASTURE, B.NONE):
                    blit(("scaffold",), cx, cy)
                    place(o, "site", i)
                    continue
                self._draw_building(blit, place, lights, i, o, f, b, level[i], col, style, material,
                                    hsh, sx, sy, e, cx, cy, season, lit, night, var)

        for route in st.get("routes", []):        # rutas de caravanas
            pts = [(self.tile_xy(i % n, i // n, self._elev[i])[0],
                    self.tile_xy(i % n, i // n, self._elev[i])[1] + TH // 2) for i in route]
            if len(pts) > 1:
                self.routes_px.append(pts)
        self._plan_walks(n, walk, dests)

        # Tintes (ATOP: solo sobre la isla; el mar se tiñe aparte)
        ctx.set_operator(cairo.OPERATOR_ATOP)
        for rgba in self._tints(night):
            ctx.set_source_rgba(*rgba)
            ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        for key, ax, ay in lights:
            self._put(ctx, key, ax, ay)
        if lit:
            torches = []
            for s in st["structures"]:
                if s.get("alive") and s["kind"] in ("container", "daemon", "db", "embassy"):
                    cx, cy = self.tile_xy(s["x"], s["y"], self.elev_at(s["x"], s["y"]))
                    torches += [(cx - 6, cy), (cx + 5, cy)]
            pixels(ctx, torches, rgb("#ffb03a"), size=(1, 2))
        zsurf.flush()
        self._zsurf = zsurf
        self._zdata = zsurf.get_data()
        self._zw, self._zh, self._zstride = zsurf.get_width(), zsurf.get_height(), zsurf.get_stride()

    def _center(self, i: int) -> tuple[int, int]:
        n = self.state["w"]
        x, y = self.tile_xy(i % n, i // n, self._elev[i])
        return x, y + TH // 2

    def _water_next_to(self, i: int):
        """Centro de una casilla de agua vecina (para amarrar botes), o None."""
        st = self.state
        n = st["w"]
        for j in _neigh(i, n, n):
            if st["terrain"][j] == WATER:
                return self._center(j)
        return None

    def _plan_walks(self, n: int, walk: dict[int, set], dests: dict[int, dict[str, list]]):
        """Rutas de los aldeanos por calles, plazas, caminos y campos. Se arma un
        árbol BFS desde la puerta del palacio; el camino entre dos puntos sube
        por el árbol hasta el ancestro común. Las puertas son la casilla
        caminable vecina de cada edificio (preferentemente la de adelante)."""
        st = self.state
        wars = list((st.get("wars") or {}).values())
        for fid, wset in walk.items():
            d = dests.get(fid, {})

            def door(i):
                x, y = i % n, i // n
                for j, ok in ((i + n, y < n - 1), (i + 1, x < n - 1), (i - 1, x > 0), (i - n, y > 0)):
                    if ok and j in wset:
                        return j
                return None

            palace_doors = [door(i) for i in d.get("palace", []) if door(i) is not None]
            root = palace_doors[0] if palace_doors else min(wset)
            parent = {root: None}
            queue = deque([root])
            while queue:
                i = queue.popleft()
                for j in _neigh(i, n, n):
                    if j in wset and j not in parent:
                        parent[j] = i
                        queue.append(j)

            def chain(i):
                out = []
                while i is not None:
                    out.append(i)
                    i = parent[i]
                return out

            def path(a, b):
                ca, cb = chain(a), chain(b)
                pos = {v: k for k, v in enumerate(cb)}
                for k, v in enumerate(ca):
                    if v in pos:
                        return ca[:k + 1] + cb[:pos[v]][::-1]
                return [a]

            def doors(kind):
                return [j for j in (door(i) for i in d.get(kind, [])) if j in parent]

            homes = doors("homes") or [root]
            targets = {
                "fields": [i for i in d.get("fields", []) if i in parent],
                "work": doors("work"), "market": doors("market"), "temple": doors("temple"),
                "plaza": [i for i in parent if st["bld"][i] in (B.PLAZA, B.STREET, B.WELL, B.MONUMENT)] or [root],
            }
            plan = ["fields", "work", "plaza", "fields", "market", "work", "temple", "fields", "plaza", "work"]
            routes = []
            for k in range(28):
                kind = plan[k % len(plan)]
                if not targets.get(kind):
                    kind = "plaza"
                a = homes[h32(fid, "h", k) % len(homes)]
                b = targets[kind][h32(fid, kind, k) % len(targets[kind])]
                routes.append({"kind": kind, "hide": kind in ("work", "market", "temple"),
                               "pts": self._route_pts(path(a, b), k)})
            plaza = targets["plaza"]
            for k in range(4):                                            # guardias
                a, b = plaza[h32(fid, "g", k) % len(plaza)], plaza[h32(fid, "g2", k) % len(plaza)]
                routes.append({"kind": "guard", "hide": False, "pts": self._route_pts(path(a, b), k)})
            if palace_doors:                                              # rey o reina
                a = palace_doors[0]
                b = plaza[h32(fid, "rey") % len(plaza)]
                routes.append({"kind": "king", "hide": False, "pts": self._route_pts(path(a, b), 0)})
            barracks = doors("barracks") or homes
            for war in wars:                                              # soldados al frente
                if fid not in (war["a"], war["b"]) or not war.get("front"):
                    continue
                for k, (fx, fy) in enumerate(war["front"][-6:]):
                    goal = min(parent, key=lambda j: abs(j % n - fx) + abs(j // n - fy))
                    routes.append({"kind": "soldier", "hide": False,
                                   "pts": self._route_pts(path(barracks[k % len(barracks)], goal), k)})
            for k, i in enumerate(d.get("site", [])[:8]):                 # obreros en las obras
                cx, cy = self._center(i)
                routes.append({"kind": "builder", "hide": False,
                               "pts": [(cx + (3 if k % 2 else -3), cy, (i % n) + (i // n) + 1)]})
            self.walk_routes[fid] = routes

    def _route_pts(self, tiles: list[int], k: int) -> list[tuple[float, float, int]]:
        n = self.state["w"]
        lateral = (k % 3 - 1) * 2
        return [(cx + lateral, cy, (i % n) + (i // n) + 1) for i in tiles for cx, cy in (self._center(i),)]

    def _tints(self, night: float) -> list[tuple]:
        w = self.state["weather"] if self.state else {}
        out = []
        if night:
            out.append((*rgb("#141a40"), 0.5 * night))
        for on, rgba in ((w.get("storm"), (0.05, 0.07, 0.15, 0.15 + 0.2 * (w.get("storm") or 0))),
                         (w.get("eruption"), (0.6, 0.1, 0.0, 0.12)), (w.get("fog"), (0.8, 0.8, 0.85, 0.15)),
                         (w.get("heat"), (1.0, 0.75, 0.3, 0.08)), (w.get("plague"), (0.4, 0.6, 0.2, 0.07))):
            if on:
                out.append(rgba)
        return out

    def _ocean_pattern(self, night: float):
        tints = tuple(self._tints(night))
        if self._ocean[0] != tints:
            color = DEEP_WATER
            for r, g, b, a in tints:
                color = mix(color, (r, g, b), a)
            pat = cairo.SurfacePattern(_ocean_tile(color))
            pat.set_extend(cairo.EXTEND_REPEAT)
            pat.set_filter(cairo.FILTER_NEAREST)
            self._ocean = (tints, pat)
        return self._ocean[1]

    def _draw_nature(self, blit, t, i, burned, ruined, var, hsh, cx, cy, season):
        if ruined:
            blit(("rubble", var), cx, cy)
        elif burned:
            return
        elif t == FOREST:
            species = self._species(i, hsh)
            spots = ((-3, -1), (3, 1), (0, 3))[:1 + (hsh >> 5) % 3] if (hsh >> 5) % 3 else ((0, 0),)
            for k, (dx, dy) in enumerate(spots):
                kind = species[(hsh >> (9 + 2 * k)) % len(species)]
                if (hsh >> 20) % 40 == 0:
                    kind = "dead"
                blit(("tree", kind, var + k, season), cx + dx, cy + dy)
        elif t == SAND and (hsh >> 12) % 100 < 9 and self._near(i, (WATER,)):
            blit(("tree", "palm", var, season), cx, cy)
        elif t in (GRASS, HILL):
            r = (hsh >> 12) % 100
            if r < 4:
                blit(("decor", "flowers", var, season), cx, cy)
            elif r < 7:
                blit(("decor", "rock", var, season), cx, cy)
            elif r < 11:
                blit(("decor", "bush", var, season), cx, cy)
            elif t == HILL and r < 16:
                blit(("tree", "pine", var, season), cx, cy)

    def _near(self, i: int, kinds) -> bool:
        n = self.state["w"]
        terrain = self.state["terrain"]
        return any(terrain[j] in kinds for j in _neigh(i, n, n))

    def _species(self, i: int, hsh: int) -> tuple[str, ...]:
        """Especies según el bioma: palmeras en la costa, coníferas al pie de la
        montaña, y bosque mixto (roble, abedul, frutales, ciprés) en el llano."""
        if self._near(i, (WATER, SAND)):
            return ("palm", "round", "palm")
        if self._near(i, (HILL, MOUNTAIN)):
            return ("pine", "pine", "cypress")
        mixes = (("oak", "round", "birch"), ("birch", "birch", "pine"), ("oak", "fruit", "round"),
                 ("round", "cypress", "oak"), ("pine", "oak", "birch"))
        return mixes[(i // 7 + hsh % 3) % len(mixes)]

    def _draw_building(self, blit, place, lights, i, o, f, b, lv, col, style, material,
                       hsh, sx, sy, e, cx, cy, season, lit, night, var):
        if b == B.STREET or b == B.WELL:
            blit(("paving", "street", season, var), sx, sy - e)
            if b == B.WELL:
                blit(("well",), cx, cy)
        elif b in (B.PLAZA, B.MONUMENT):
            blit(("paving", "plaza", season, var), sx, sy - e)
            if b == B.MONUMENT:
                blit(("monument",), cx, cy)
            elif hsh % 2 == 0:
                blit(("fountain",), cx, cy)
                self.fountains.append((cx, cy - 8))
        elif b == B.HOUSE:
            variant = (hsh >> 4) % 6
            blit(("house", max(1, lv), style, col, material, variant), cx, cy)
            place(o, "homes", i)
            if lit and hsh % 4:
                lights.append((("lights", max(1, lv), style), cx, cy))
            if variant in (0, 4) and (night > 0.3 or season == "invierno") and hsh % 3 == 0:
                dx, dy = self.sprites.chimney_top(max(1, lv), style)
                self.smoke.append((cx + dx, cy + dy, 0.4))
        elif b == B.FARM:
            if hsh % 13 == 0 and season != "invierno":
                blit(("windmill", col), cx, cy)
                self.mills.append((cx, cy - 10))
            else:
                blit(("field", ("wheat", "wheat", "vine", "pumpkin", "fallow")[hsh % 5], col, season), cx, cy)
            place(o, "fields", i)
        elif b == B.PASTURE:
            blit(("pasture", season), sx, sy - e)
            self.sheep.append((cx, cy, i))
            place(o, "fields", i)
        elif b == B.TEMPLE:
            blit(("temple", style, col), cx, cy)
            place(o, "temple", i)
        elif b == B.BARRACKS:
            blit(("barracks", col), cx, cy)
            place(o, "barracks", i)
        elif b == B.LIBRARY:
            blit(("library", col, material), cx, cy)
            place(o, "work", i)
        elif b == B.WATCHTOWER:
            blit(("watchtower", col), cx, cy)
            self.flags.append((cx + 3, cy - 30, col))
        elif b == B.GARDEN:
            blit(("garden", season), cx, cy)
        elif b == B.GRAVEYARD:
            blit(("graveyard",), cx, cy)
        elif b == B.LUMBER:
            blit(("lumber",), cx, cy)
            place(o, "work", i)
        elif b == B.DOCK:
            blit(("dock",), cx, cy)
            place(o, "fields", i)
            water = self._water_next_to(i)
            if water:
                self.fishers.append(water)
        elif b in _LANDMARK_BLD:
            blit(("landmark", _LANDMARK_BLD[b], col), cx, cy)
            place(o, "market" if b == B.MARKET else "work", i)
            if b == B.WORKSHOP:
                self.smoke.append((cx + 3, cy - 18, 1.0))
        else:                                   # sin plan (mundos viejos): casa
            blit(("house", max(1, lv), style, col, material, 3), cx, cy)
            place(o, "homes", i)

    def _draw_structure(self, blit, s: dict, cx: int, cy: int, lit: bool, i: int):
        kind = s["kind"]
        if kind == "tower":
            floors = s.get("floors", 1) if s.get("alive") else 1
            blit(("tower", floors, bool(s.get("forging")) or lit), cx, cy)
            self.academy_px = (cx, cy - 6 * floors - 14)
            if s.get("forging") and s.get("alive"):
                self.smoke.append((cx, cy - 6 * floors - 16, 1.0))
        elif kind == "mine":
            blit(("mine", s.get("activity", 0) > 0.05), cx, cy)
            self.mine_px = (cx, cy - 4, s.get("activity", 0))
        elif kind == "lighthouse":
            if s.get("building"):
                blit(("scaffold",), cx, cy)                    # los obreros todavía lo están levantando
            else:
                blit(("lighthouse",), cx, cy)
                self.beacons.append((cx, cy - 27))
        elif kind == "embassy":
            if s.get("alive"):
                col = tuple(c / 255 for c in s.get("color", (200, 200, 200)))
                blit(("tent", col), cx, cy)
                self.envoys.append((cx, cy, col, self._water_next_to(i)))
                self.flags.append((cx + 3, cy - 20, col))
        else:
            blit(("castle", kind, bool(s.get("alive"))), cx, cy)
            if s.get("alive"):
                fx, fy = self.sprites.castle_flag(kind)
                self.flags.append((cx + fx, cy + fy, FLAG.get(kind, STONE)))

    @staticmethod
    def _coast_mask(terrain: list[int], n: int) -> list[bool]:
        """Agua a <= 2 casillas de tierra: se dibuja; el resto es mar de fondo."""
        near = [False] * len(terrain)
        for i, t in enumerate(terrain):
            if t != WATER:
                x, y = i % n, i // n
                for yy in range(max(0, y - 2), min(n, y + 3)):
                    for xx in range(max(0, x - 2), min(n, x + 3)):
                        near[yy * n + xx] = True
        return near

    # -- UI: HUD, crónica, ficha -------------------------------------------------

    def _rebuild_ui(self, fw: int, fh: int):
        st = self.state
        self.hud = self.chron = self.powers_panel = None
        self.power_rects = []
        if not st:
            return
        m, r, w = st["metrics"], st["resources"], st["weather"]
        act = st.get("activity", {})
        temp = f"  {m['cpu_temp']:.0f}°C" if m.get("cpu_temp") else ""
        if act.get("source") == "evdev":
            presence = ui("hud.typing", kpm=act.get("kpm", 0))
        else:
            presence = ui("hud.present") if act.get("active", 1) > 0.2 else ui("hud.away")
        lines = [
            (ui("hud.cpu", cpu=m["cpu"], ram=m["ram"], temp=temp), TEXT, None),
            (ui("hud.net", rx=m.get("net_rx") or 0, tx=m.get("net_tx") or 0, disk=act.get("disk", 0)), DIM, None),
            (ui("hud.res", gold=r["gold"], stone=r.get("stone", 0), defense=r["defense"], mana=r["mana"]), TEXT, None),
            (ui("hud.souls", procs=m["procs"], births=m.get("births") or 0, deaths=m.get("deaths") or 0,
                presence=presence), DIM, None),
        ]
        # Reinos: en pie primero (por tamaño), después los caídos. En la ventana se recorren con la rueda.
        realms = sorted(st["factions"], key=lambda f: (not f.get("alive"), -f.get("tiles", 0), f["id"]))
        if not self.window_mode:
            realms = [f for f in realms if f.get("tiles")]
        shown = 6
        self.hud_scroll = max(0, min(self.hud_scroll, len(realms) - shown))
        self._hud_lines = {}
        if self.window_mode:
            fallen = sum(1 for f in realms if not f.get("alive"))
            lines.append((ui("hud.realms", alive=len(realms) - fallen, fallen=fallen), GOLD, None))
            if self.hud_scroll:
                lines.append((f"▲ {ui('hud.more', n=self.hud_scroll)}", DIM, None))
        for f in realms[self.hud_scroll:self.hud_scroll + shown]:
            col = tuple(c / 255 for c in f["color"])
            if f.get("alive"):
                text = f"{f.get('realm', f['name'])[:12]:<12}{f.get('tiles', 0):>4}  {f['name'][:12]}"
                lines.append((text, TEXT, col))
            else:
                lines.append((f"{f.get('realm', f['name'])[:12]:<12}  {ui('hud.fallen')}  {f['name'][:10]}", DIM,
                              shade(col, 0.5)))
            self._hud_lines[len(lines) - 1] = f["id"]
        below = len(realms) - self.hud_scroll - shown
        if self.window_mode and below > 0:
            lines.append((f"▼ {ui('hud.more', n=below)}", DIM, None))
        alerts = []
        for war in (st.get("wars") or {}).values():
            alerts.append((war["name"][:40], rgb("#ff9a8a")))
        visitors = [v for v in (st.get("visitors") or {}).values() if v.get("remote")]
        if visitors:
            alerts.append((ui("hud.envoys", lands=", ".join(v["land"] for v in visitors[:2])), rgb("#9fe0c0")))
        if w["eruption"]:
            alerts.append((ui("hud.eruption", culprit=w["culprit"]), LAVA))
        elif w["storm"]:
            alerts.append((ui("hud.storm", pct=f"{w['storm']:.0%}", culprit=w["culprit"]), rgb("#9fd0ff")))
        for flag, key, col in (("fog", "hud.fog", "#c8c8c8"), ("heat", "hud.heat", "#ffb03a"),
                               ("plague", "hud.plague", "#9fd06a")):
            if w.get(flag):
                alerts.append((ui(key), rgb(col)))
        lines += [(f"!! {t}", c, None) for t, c in alerts]
        kingdom = st.get("kingdom", "")
        if self.show_hud:
            self.hud = _panel(lines, title=ui("hud.title", kingdom=kingdom, era=st.get("era_name") or ""))

        if self.show_chronicle and self.chron_mode and st.get("chronicle"):
            entries = st["chronicle"]
            expanded = self.window_mode and self.chron_mode == 2
            width = min(560, int(fw * 0.6)) if expanded else min(340, fw // 2 - 12)
            free = fh - 12 - (self.hud.get_height() + 8 if self.hud else 0)   # sin tapar el HUD
            max_rows = max(9, free // 11 - 2) if expanded else 9
            self.chron_scroll = max(0, min(self.chron_scroll, len(entries) - 1))
            rows: list[tuple[str, tuple, None]] = []
            for k, e in enumerate(entries[::-1][self.chron_scroll:]):
                stamp = time.strftime("%d/%m %H:%M" if expanded else "%H:%M", time.localtime(e["t"]))
                col = TEXT if k == 0 and not self.chron_scroll else DIM
                wrapped = _wrap(_MEASURE, e["text"], width - 12 - _MEASURE.text_extents(stamp + "  ").x_advance)
                if len(rows) + len(wrapped) > max_rows:
                    break
                for j, line in enumerate(wrapped):
                    rows.append(((stamp if j == 0 else " " * len(stamp)) + "  " + line, col, None))
            title = ui("chron.title", kingdom=kingdom)
            if self.chron_scroll:
                title += ui("chron.back", n=self.chron_scroll)
            self.chron = _panel(rows, width=width, title=title)

        if self.show_powers and self.window_mode and st.get("powers"):
            names, res = words("powers"), words("resources")
            rows = []
            for k, key in enumerate(POWER_KEYS):
                cost = st["powers"].get(key)
                if not cost:
                    continue
                afford = all(r.get(c, 0) >= v for c, v in cost.items())
                price = ", ".join(f"{v} {res[c]}" for c, v in cost.items())
                col = GOLD if key == self.selected_power else (TEXT if afford else (0.42, 0.43, 0.46))
                rows.append((f"{k + 1} {names[key][0]:<10} {price}", col, None))
                self.power_rects.append(key)
            self.powers_panel = _panel(rows, title=ui("powers.title"))

    def ui_region(self, ux: float, uy: float) -> str | None:
        """¿Qué panel hay bajo el punto (coordenadas de UI)? hud, chron, powers o None."""
        for name, (x, y, w, h) in self._rects.items():
            if x <= ux < x + w and y <= uy < y + h:
                return name
        return None

    def hud_realm_at(self, ux: float, uy: float) -> int | None:
        """Id del reino de la fila del HUD bajo el punto."""
        if self.ui_region(ux, uy) != "hud":
            return None
        return self._hud_lines.get(int((uy - 6 - 16) // 11))

    def scroll_panel(self, name: str, delta: int):
        if name == "hud":
            self.hud_scroll = max(0, self.hud_scroll + delta)
        elif name == "chron":
            self.chron_scroll = max(0, self.chron_scroll + delta)
        self._ui_dirty = True

    def cycle_chronicle(self):
        """Normal -> expandida -> oculta -> normal."""
        self.chron_mode = {1: 2, 2: 0, 0: 1}[self.chron_mode]
        self.chron_scroll = 0
        self._ui_dirty = True

    def focus_tile(self, x: int, y: int):
        """Centra la cámara en una casilla."""
        wx, wy = self.tile_xy(x, y, self.elev_at(x, y))
        ww, wh = self.world_size()
        self.pan = [wx - ww / 2, wy - wh / 2]

    def power_at(self, ux: float, uy: float) -> str | None:
        """¿Qué poder hay bajo el punto (coordenadas de la capa de UI)?"""
        if not self.powers_panel:
            return None
        px, py = self._powers_pos
        row = int((uy - py - 3 - 13) // 11)          # 13 = alto del título del panel
        if px <= ux <= px + self.powers_panel.get_width() and 0 <= row < len(self.power_rects):
            return self.power_rects[row]
        return None

    def select_power(self, key: str | None):
        self.selected_power = key
        self._ui_dirty = True

    def toast(self, text: str, now: float, ok: bool = True, secs: float = 4.0):
        self._toast = (_panel([(text, TEXT if ok else rgb("#ff9a8a"), None)]), now + secs)
        self._ui_drawn = None

    def _height_px(self, i: int) -> int:
        """Alto aproximado (px) de lo que está dibujado sobre la casilla."""
        st = self.state
        n = st["w"]
        if not st["owner"][i]:
            return 0
        if any(tuple(f["capital"]) == (i % n, i // n) for f in st["factions"]):
            return 40
        b = (st.get("bld") or [0] * len(st["owner"]))[i]
        if b == B.HOUSE:
            lv = max(1, st["level"][i])
            return 10 + 4 * lv
        return {B.TEMPLE: 30, B.LIBRARY: 24, B.WATCHTOWER: 28, B.MONUMENT: 26, B.BARRACKS: 16,
                B.MARKET: 12, B.WORKSHOP: 16, B.BARN: 16, B.TAVERN: 16}.get(b, 0)

    def pick(self, fx: float, fy: float) -> tuple[int, int] | None:
        """Casilla bajo el punto de frame: primero edificios altos que estén delante
        (los de más abajo en pantalla tapan a los de atrás), después el terreno."""
        if not self.state:
            return None
        n = self.state["w"]
        wx, wy = self.to_world(fx, fy)
        x0, y0 = self.tile_at(wx, wy)
        for k in range(5, 0, -1):                       # casillas delante, en la misma columna
            x, y = x0 + k, y0 + k
            if 0 <= x < n and 0 <= y < n:
                i = y * n + x
                cx, cy = self.tile_xy(x, y, self._elev[i])
                if abs(wx - cx) <= 7 and cy + TH // 2 - self._height_px(i) <= wy <= cy + TH:
                    return x, y
        for e in sorted(set(self._elev), reverse=True):
            x, y = self.tile_at(wx, wy, e)
            if 0 <= x < n and 0 <= y < n and self._elev[y * n + x] == e:
                return x, y
        return (x0, y0) if 0 <= x0 < n and 0 <= y0 < n else None

    def inspect(self, fx: float, fy: float, now: float, ui_pos=None) -> dict | None:
        """Arma la ficha de lo que hay bajo (fx, fy) en coordenadas de frame.
        ui_pos: dónde mostrarla en la capa de UI (por defecto, el mismo punto)."""
        st = self.state
        if not st:
            return None
        n = st["w"]
        wx, wy = self.to_world(fx, fy)
        hit = None
        for s in st["structures"]:        # estructuras altas: vale clickear el dibujo, no solo la base
            if not (s.get("alive") or s.get("dead_ticks", 0) < 600):
                continue
            cx, cy = self.tile_xy(s["x"], s["y"], self.elev_at(s["x"], s["y"]))
            if abs(wx - cx) <= 8 and cy - 34 <= wy <= cy + 8:
                hit = s
        lines: list[tuple[str, tuple, tuple | None]] = []
        if hit:
            s = hit
            title = s.get("title", s["name"])
            lines.append((f"{words('kinds').get(s['kind'], s['kind'])}: {s['name']}", TEXT, None))
            if s["kind"] == "tower":
                lines.append((ui("card.floors", floors=s.get("floors", 1), editors=", ".join(s.get("editors", [])) or "-"), DIM, None))
                if s.get("forging"):
                    lines.append((ui("card.forging", what=", ".join(s["forging"])), rgb("#ffb03a"), None))
            elif s["kind"] == "mine":
                lines.append((ui("card.mine", pct=f"{s.get('activity', 0):.0%}", disk=st.get("activity", {}).get("disk", 0)), DIM, None))
            elif s["kind"] == "embassy":
                lines.append((ui("card.embassy", user=s.get("user", "?"), mins=s.get("uptime", 0) / 60), DIM, None))
            elif s["kind"] != "lighthouse":
                lines.append((ui("card.uptime", state=ui("card.alive") if s.get("alive") else ui("card.ruins"),
                                 hours=s.get("uptime", 0) / 3600, rss=s.get("rss_mb", 0)), DIM, None))
                lines.append((ui("card.walls", walls=s.get("walls", 0)), DIM, None))
            where = (s["x"], s["y"])
        else:
            where = self.pick(fx, fy)
            if where is None:
                self.card = None
                return None
            x, y = where
            i = y * n + x
            f = next((f for f in st["factions"] if f["id"] == st["owner"][i]), None)
            b = (st.get("bld") or [0] * len(st["owner"]))[i]
            if f:
                bname = ui("card.capital") if tuple(f["capital"]) == (x, y) else words("buildings").get(b, "")
                title = f.get("display") or f.get("realm", f["name"])
                lines.append((f.get("ruler_display", ""), TEXT, tuple(c / 255 for c in f["color"])))
                if bname:
                    lines.append((ui("card.of", building=bname, realm=f.get("realm", f["name"])), GOLD, None))
                lines.append((ui("card.proc", name=f["name"], procs=f.get("procs", 0)), DIM, None))
                lines.append((ui("card.ram", rss=f.get("rss_mb", 0), cpu=f.get("cpu", 0)), DIM, None))
                lines.append((ui("card.lands", tiles=f.get("tiles", 0), target=f.get("target", 0),
                                 style=words("styles")[f.get("style", 0)]), DIM, None))
                if f.get("cult"):
                    lines.append((ui("card.faith", cult=f.get("cult_display") or f["cult"].get("cult", "")), DIM, None))
                heroes = [h for h in st.get("notables", []) if h.get("alive") and h.get("faction") == f["name"]]
                for h in heroes[:2]:
                    lines.append((ui("card.hero", name=h["name"], trait=h.get("trait", "")), DIM, None))
            else:
                title = words("terrain")[st["terrain"][i]]
            flags = ui("card.flags")
            extra = [flags[g] for g in ("burn", "lava", "ruin", "wall", "road") if st.get(g) and st[g][i]]
            lines.append((ui("card.tile", x=x, y=y) + (f" · {', '.join(extra)}" if extra else ""), DIM, None))
        self.card = {"surf": _panel(lines, title=title), "pos": ui_pos or (fx, fy), "until": now + 8, "where": where}
        return self.card

    # -- frame ---------------------------------------------------------------

    def frame(self, fw: int, fh: int, now: float) -> cairo.ImageSurface:
        """Frame de fw x fh unidades. La superficie mide fw*u x fh*u pixels."""
        if (fw, fh) != self.size:
            self.size = (fw, fh)
            self.frame_surf = None
        night = night_factor(self.hour())
        sig = (round(night * 8), self.season())
        if sig != self._sig or self.world is None:
            self._rebuild_world()
            self._sig = sig
        U = self.u
        if self.frame_surf is None:
            self.frame_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, fw * U, fh * U)
        self._camera(fw, fh)
        ctx = cairo.Context(self.frame_surf)
        ctx.set_antialias(cairo.ANTIALIAS_NONE)
        ctx.scale(U, U)
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        pat = self._ocean_pattern(night)
        pat.set_matrix(cairo.Matrix(x0=-self.cam[0], y0=-self.cam[1]))
        ctx.set_source(pat)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        ctx.save()
        ctx.identity_matrix()
        ctx.set_source_surface(self.world, self.cam[0] * U, self.cam[1] * U)
        ctx.paint()
        ctx.restore()
        if self.state:
            ctx.save()
            ctx.translate(*self.cam)
            self._animate_world(ctx, now, night)
            ctx.restore()
            self._animate_screen(ctx, fw, fh, now)
            self._draw_hover(ctx)
        self.frame_surf.flush()
        return self.frame_surf

    def ui(self, uw: int, uh: int, now: float) -> tuple[cairo.ImageSurface, bool]:
        """Capa de interfaz (HUD, crónica, ficha) a escala base: no crece con el zoom.
        Devuelve (superficie, cambió) para no re-subir la textura si no hace falta."""
        if (uw, uh) != self._ui_size:
            self._ui_size = (uw, uh)
            self.ui_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, uw, uh)
            self._ui_dirty = True
        if self.card and now > self.card["until"]:
            self.card = None
            self._ui_drawn = None
        if self._toast and now > self._toast[1]:
            self._toast = None
            self._ui_drawn = None
        self._check_power_log(now)
        key = self._ui_key(now)
        if self._ui_dirty:
            self._rebuild_ui(uw, uh)
            self._ui_dirty = False
            key = None
        if key is not None and key == self._ui_drawn:
            return self.ui_surf, False
        ctx = cairo.Context(self.ui_surf)
        ctx.set_operator(cairo.OPERATOR_CLEAR)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        if self.state:
            self._draw_ui(ctx, uw, uh, now)
        self.ui_surf.flush()
        self._ui_drawn = self._ui_key(now)
        return self.ui_surf, True

    def _ui_key(self, now: float):
        return (id(self.hud), id(self.chron), id(self.powers_panel), id(self._toast),
                id(self.card["surf"]) if self.card else None,
                round(min(1.0, self.card["until"] - now), 1) if self.card else None)

    def _check_power_log(self, now: float):
        """Avisa el resultado de los poderes que procesó el daemon."""
        log = (self.state or {}).get("power_log") or []
        if log and log[-1]["t"] > self._power_seen:
            if self._power_seen:
                self.toast(log[-1]["msg"], now, log[-1]["ok"])
            self._power_seen = log[-1]["t"]

    def _draw_hover(self, ctx):
        if self.hover:
            x, y = self.hover
            sx, sy = self.tile_xy(x, y, self.elev_at(x, y))
            sx += self.cam[0]
            sy += self.cam[1]
            ctx.move_to(sx, sy)
            ctx.line_to(sx + 8, sy + 4)
            ctx.line_to(sx, sy + 8)
            ctx.line_to(sx - 8, sy + 4)
            ctx.close_path()
            ctx.set_line_width(1)
            ctx.set_source_rgba(1, 0.95, 0.5, 0.9)
            ctx.stroke()

    def _draw_ui(self, ctx, fw, fh, now):
        self._rects = {}
        if self.hud:
            ctx.set_source_surface(self.hud, 6, 6)
            ctx.paint()
            self._rects["hud"] = (6, 6, self.hud.get_width(), self.hud.get_height())
        if self.chron:
            y = fh - self.chron.get_height() - 6
            ctx.set_source_surface(self.chron, 6, y)
            ctx.paint()
            self._rects["chron"] = (6, y, self.chron.get_width(), self.chron.get_height())
        if self.powers_panel:
            self._powers_pos = (fw - self.powers_panel.get_width() - 6, fh - self.powers_panel.get_height() - 6)
            ctx.set_source_surface(self.powers_panel, *self._powers_pos)
            ctx.paint()
            self._rects["powers"] = (*self._powers_pos, self.powers_panel.get_width(), self.powers_panel.get_height())
        hint = self._toast[0] if self._toast else (
            _panel([(ui("powers.pick", power=words("powers")[self.selected_power][0]), GOLD, None)])
            if self.selected_power else None)
        if hint:
            ctx.set_source_surface(hint, (fw - hint.get_width()) // 2, 8)
            ctx.paint()
        if self.card:
            s = self.card["surf"]
            x, y = self.card["pos"]
            x = min(max(4, x + 10), fw - s.get_width() - 4)
            y = min(max(4, y - s.get_height() - 6), fh - s.get_height() - 4)
            ctx.set_source_surface(s, int(x), int(y))
            ctx.paint_with_alpha(min(1.0, self.card["until"] - now))

    # -- vida (coordenadas de mundo) -------------------------------------------

    def _animate_world(self, ctx, now: float, night: float):
        st = self.state
        w = st["weather"]
        act = st.get("activity", {})
        n = st["w"]

        # Barcos: tráfico de descarga (red entrante) rodeando la isla
        ww, wh = self.world_size()
        cx, cy = ww / 2, wh / 2 + 10
        rx, ry = ww / 2 - MARGIN + 6, wh / 2 - MARGIN - 4
        ships = 1 + min(7, int(math.log2(1 + (act.get("net_rx") or 0) * 4) * 1.5))
        for k in range(ships):
            sign = 1 if k % 2 else -1
            ang = now * 0.015 * (1 + k % 3 * 0.3) * sign + k * 2.4
            px, py = cx + rx * math.cos(ang), cy + ry * math.sin(ang)
            tx, ty = self.tile_at(px, py)
            if 0 <= tx < n and 0 <= ty < n and st["terrain"][ty * n + tx] != WATER:
                continue
            self._put(ctx, ("boat", math.sin(ang) * sign > 0, (1, 1, 1) if k else GOLD),
                      px, py + int(now * 2 + k) % 2)
        # Pescadores amarrados al agua de sus muelles y barcos de las embajadas
        boats = self.fishers[:10] + [e[3] for e in self.envoys if e[3]]
        for k, (x, y) in enumerate(boats):
            self._put(ctx, ("boat", k % 2 == 0, rgb("#e8e0c8")), x, y + 3 + int(now * 1.5 + k) % 2)

        # Banderas flameando, aspas de molino, fuentes
        flap = int(now * 3) % 2
        for x, y, col in self.flags:
            pixels(ctx, [(x + flap, y + 1)], col)
        spin = int(now * (6 if w["storm"] else 2)) % 2
        arms = []
        for x, y in self.mills:
            arms += ([(x - 4 + k, y) for k in range(9)] + [(x, y - 4 + k) for k in range(9)]) if spin else \
                    ([(x - 3 + k, y - 3 + k) for k in range(7)] + [(x - 3 + k, y + 3 - k) for k in range(7)])
        pixels(ctx, arms, rgb("#6b4a2b"))
        spray = int(now * 4) % 3
        pixels(ctx, [(x + 1, y - spray) for x, y in self.fountains] +
               [(x + (k % 3) - 1, y + 2) for k, (x, y) in enumerate(self.fountains * 2)], rgb("#9fd0ff"))

        # Humo: chimeneas, talleres, academia forjando
        puffs = []
        for k, (x, y, power) in enumerate(self.smoke[:50]):
            for j in range(3 if power > 0.5 else 1):
                life = (now * 0.4 + j / 3 + k * 0.13) % 1
                puffs.append((x + math.sin(now + k + j) * 3 * life, y - life * 14))
        pixels(ctx, puffs, (0.75, 0.75, 0.78), 0.45, size=(2, 2))

        # Ovejas pastando
        sheep = []
        n = st["w"]
        for k, (x, y, i) in enumerate(self.sheep[:60]):
            for j in range(2):
                ph = now * 0.15 + k * 1.7 + j * 2.1
                sx, sy = x + math.sin(ph) * 4 - 1 + j * 3, y - 1 + math.cos(ph * 0.7) * 1.5
                if self._visible(sx, sy, i % n + i // n + 1):
                    sheep.append((sx, sy))
        pixels(ctx, sheep, (0.96, 0.96, 0.94), size=(2, 1))
        pixels(ctx, [(x + 2, y) for x, y in sheep], rgb("#3a3a3a"))

        self._villagers(ctx, now, night, act)
        self._expedition_units(ctx, now)

        # Fiesta (confeti sobre la capital) y bendición (chispas doradas) por poderes del Soberano
        tick = st["tick"]
        confetti, glitter = [], []
        for f in st["factions"]:
            if not f.get("alive"):
                continue
            fx, fy = f["capital"]
            cx, cy = self.tile_xy(fx, fy, self.elev_at(fx, fy))
            if f.get("festival", 0) > tick:
                for k in range(24):
                    life = (now * 0.5 + k / 24) % 1
                    confetti.append((cx + math.sin(k * 2.7) * 20 * life, cy - 30 + life * 34, k % 4))
            if f.get("blessed", 0) > tick:
                for k in range(14):
                    ph = now * 1.3 + k
                    glitter.append((cx + math.cos(ph) * (6 + k), cy - 10 - (now * 8 + k * 3) % 30))
        for j, colr in enumerate(("#e74c3c", "#f4d03f", "#3498db", "#2ecc71")):
            pixels(ctx, [(x, y) for x, y, c in confetti if c == j], rgb(colr))
        pixels(ctx, glitter, GOLD)
        if night > 0.3:                                      # haz giratorio de los faros
            for bx, by in self.beacons:
                ang = now * 1.2
                for k in range(3, 26, 2):
                    pixels(ctx, [(bx + math.cos(ang) * k, by + math.sin(ang) * k * 0.5)], rgb("#fff2a8"), 0.7 - k / 40)

        # Mina: chispas según la actividad del disco
        if self.mine_px and self.mine_px[2] > 0.05:
            mx, my, a = self.mine_px
            pixels(ctx, [(mx + math.sin(now * 5 + k) * 5, my - ((now * 1.5 + k / 6) % 1) * 10)
                         for k in range(int(3 + 8 * a))], rgb("#ffcf6a"))

        # Pájaros mensajeros: tipeo (o presencia) -> vuelan desde la Academia
        active = act.get("active", 1.0)
        birds = min(10, act.get("kpm", 0) // 25) if act.get("source") == "evdev" else (2 if active > 0.3 else 0)
        if birds and night < 0.6:
            ax, ay = self.academy_px or (ww // 2, wh // 3)
            pts = []
            for k in range(birds):
                life = (now * 0.06 + k / birds) % 1
                ang = k * 2.39
                x = ax + math.cos(ang) * life * ww * 0.6
                y = ay - life * 40 + math.sin(ang) * life * wh * 0.3
                wing = int(now * 6 + k) % 2
                pts += [(x - 2, y - wing), (x - 1, y), (x, y + 1 - wing), (x + 1, y), (x + 2, y - wing)]
            pixels(ctx, pts, rgb("#2a2a33"))

        # Fuego en casillas quemadas
        f8 = int(now * 8)
        fire = [(x + (k * 5) % 7 - 3, y - 2 - (f8 + k) % 3) for k, (x, y) in enumerate(self.burn_px) if (f8 + k) % 3]
        pixels(ctx, fire, EMBER)
        pixels(ctx, [(x, y - 1) for x, y in fire[::2]], rgb("#ffe08a"))

        # Lava: pulso de brillo
        if self.lava_px:
            for x, y in self.lava_px:
                ctx.rectangle(x - 4, y + 2, 8, 4)
            ctx.set_source_rgba(1, 0.8, 0.3, 0.25 + 0.2 * math.sin(now * 4))
            ctx.fill()

        # Erupción: humo y brasas desde el cráter
        if w["eruption"] and self.volcano_px:
            vx, vy = self.volcano_px
            smoke, embers = [], []
            for j in range(28):
                life = (now * 0.35 + j / 28) % 1
                smoke.append((vx + math.sin(j * 1.7 + now) * 10 * life, vy - life * 90))
                el = (now * 0.9 + j / 28) % 1
                embers.append((vx + math.sin(j * 2.3) * 22 * el, vy - el * 40 + el * el * 50))
            pixels(ctx, smoke, (0.35, 0.33, 0.33), 0.55, size=(3, 3))
            pixels(ctx, embers, EMBER)

        self._draw_events(ctx, now)

    def _villagers(self, ctx, now: float, night: float, act: dict):
        """Aldeanos con oficio, al estilo WorldBox: salen por la puerta de su casa,
        caminan por calles y caminos hasta el trabajo (y entran), los granjeros
        trabajan el campo, los mercaderes van al mercado, los soldados marchan al
        frente, el rey pasea por la plaza. De noche quedan guardias con antorchas.
        Quien pasa detrás de un edificio o un árbol queda tapado."""
        st = self.state
        active = act.get("active", 1.0)
        day = 1 - night
        heads, crowns, torches, sparks, spears = [], [], [], [], []
        bodies: dict[tuple, list] = {}

        def put(x, y, z, col):
            if not self._visible(x, y - 2, z):
                return False
            heads.append((x, y - 2))
            bodies.setdefault(col, []).append((x, y - 1))
            return True

        for f in st["factions"]:
            routes = self.walk_routes.get(f["id"])
            if not routes or not f.get("alive"):
                continue
            col = tuple(c / 255 for c in f["color"])
            busy = (0.3 + 0.7 * active) * (1 + min(2.0, f.get("cpu", 0) / 30))
            workers = int(min(28, 2 + f.get("procs", 0) / 2) * busy)
            n_work = 0
            for k, r in enumerate(routes):
                kind, pts = r["kind"], r["pts"]
                if kind in ("fields", "work", "market", "temple", "plaza"):
                    n_work += 1
                    if n_work > workers or (kind != "plaza" and day < 0.35) or (kind == "plaza" and day < 0.15):
                        continue
                elif kind == "guard" and night < 0.4:
                    continue
                elif kind == "king" and day < 0.3:
                    continue
                if kind == "builder":
                    x, y, z = pts[0]
                    if put(x, y, z, col) and int(now * 4 + k) % 2:
                        sparks.append((x + (1 if k % 2 else -1), y - 3))
                    continue
                L = len(pts) - 1
                period = 6 + 1.6 * L
                t = (now / period + k * 0.37) % 1
                if 0.4 <= t < 0.6:                       # en destino
                    if r["hide"]:
                        continue                         # entró al edificio
                    u = 1.0
                else:
                    u = min(1.0, t / 0.4) if t < 0.4 else max(0.0, 1 - (t - 0.6) / 0.4)
                pos = u * L
                j = min(int(pos), max(0, L - 1))
                frac = pos - j if L else 0
                (ax, ay, az) = pts[j]
                (bx, by, bz) = pts[min(j + 1, L)]
                x, y = ax + (bx - ax) * frac, ay + (by - ay) * frac
                z = az if frac < 0.5 else bz
                if kind == "fields" and 0.4 <= t < 0.6 and int(now * 2 + k) % 2:
                    y -= 1                               # agachado trabajando
                if not put(x, y, z, col):
                    continue
                if kind == "king":
                    crowns.append((x, y - 3))
                elif kind == "guard":
                    torches.append((x + 1, y - 3))
                elif kind == "soldier":
                    spears.append((x + 1, y - 4))
                    if u >= 1.0 and int(now * 6 + k) % 3 == 0:
                        sparks.append((x + 1, y - 4))
        for j, (x, y, col, _) in enumerate(self.envoys):     # forasteros en las embajadas
            ex = x + math.sin(now * 0.5 + j) * 5
            heads.append((ex, y + 1))
            bodies.setdefault(col, []).append((ex, y + 2))

        pixels(ctx, heads, SKIN)
        for col, pts in bodies.items():
            pixels(ctx, pts, col, size=(1, 2))
        pixels(ctx, crowns, GOLD)
        pixels(ctx, spears, rgb("#cfcfcf"), size=(1, 3))
        pixels(ctx, torches, rgb("#ffcf6a"))
        pixels(ctx, sparks, (1, 1, 1))

    def _expedition_units(self, ctx, now: float):
        """Grupos en marcha: obreros, druidas, exploradores, columnas y asedios.
        Caminan en fila por el camino que calculó el daemon y trabajan al llegar."""
        st = self.state
        n = st["w"]
        tick_f = st["tick"] + (now - getattr(self, "_state_t", now)) / max(0.5, self.interval)
        colors = {f["id"]: tuple(c / 255 for c in f["color"]) for f in st["factions"]}
        heads, extras = [], {}
        bodies: dict[tuple, list] = {}

        def mark(kind, pt):
            extras.setdefault(kind, []).append(pt)

        for e in st.get("expeditions", []):
            path = e["path"]
            pts = [self._center(i) for i in path]
            zs = [(i % n) + (i // n) + 1 for i in path]
            L = len(pts) - 1
            t = tick_f - e["start"]
            walk = min(1.0, max(0.0, t / max(1, e["eta"])))
            working = t >= e["eta"]
            col = colors.get(e["fid"], (0.92, 0.92, 0.85))
            if e["kind"] == "plant":
                col = rgb("#3f8f3a")
            tx, ty = pts[-1]
            for k in range(e["n"]):
                if working:                                   # rodean el destino
                    ang = k * 2 * math.pi / e["n"] + (now * 0.2 if e["kind"] in ("army", "siege") else 0)
                    rad = 7 if e["kind"] in ("army", "siege") else 4
                    x, y, z = tx + math.cos(ang) * rad, ty + math.sin(ang) * rad * 0.5 + 1, zs[-1] + 1
                else:
                    pos = walk * L - k * 0.45                 # en fila
                    if pos < 0:
                        continue                              # todavía sale de la capital
                    j = min(int(pos), max(0, L - 1))
                    fr = pos - j if L else 0
                    (ax, ay), (bx, by) = pts[j], pts[min(j + 1, L)]
                    x, y = ax + (bx - ax) * fr, ay + (by - ay) * fr
                    z = zs[min(j + 1, L)] if fr >= 0.5 else zs[j]
                    y += int(now * 4 + k) % 2 * 0.5           # paso
                if not self._visible(x, y - 2, z):
                    continue
                heads.append((x, y - 2))
                bodies.setdefault(col, []).append((x, y - 1))
                if e["kind"] in ("army", "siege"):
                    mark("spear", (x + 1, y - 4))
                    if k == 0:
                        mark("banner", (x, y - 7))
                elif e["kind"] == "explore":
                    mark("hat", (x - 1, y - 3))
                elif e["kind"] == "build" and working and int(now * 5 + k) % 2:
                    mark("spark", (x + 1, y - 3))
                elif e["kind"] == "plant" and working:
                    mark("leaf", (x + 1, y))
            if working and e["kind"] == "siege":            # flechas y piedras contra el fuerte
                for k in range(6):
                    life = (now * 1.5 + k / 6) % 1
                    ang = k * 1.1
                    sx, sy = tx + math.cos(ang) * 8, ty + math.sin(ang) * 4
                    mark("arrow", (sx + (tx - sx) * life, sy - 12 * math.sin(life * math.pi) + (ty - 10 - sy) * life))
        pixels(ctx, heads, SKIN)
        for col, pts in bodies.items():
            pixels(ctx, pts, col, size=(1, 2))
        pixels(ctx, extras.get("spear", []), rgb("#cfcfcf"), size=(1, 3))
        pixels(ctx, extras.get("banner", []), GOLD, size=(2, 2))
        pixels(ctx, extras.get("hat", []), rgb("#6b4a2b"), size=(3, 1))
        pixels(ctx, extras.get("spark", []) + extras.get("arrow", []), (1, 0.95, 0.7))
        pixels(ctx, extras.get("leaf", []), rgb("#7fd06a"))

    # -- clima (coordenadas de pantalla) -------------------------------------------

    def _animate_screen(self, ctx, fw, fh, now):
        w = self.state["weather"]
        for k in range(3 + (4 if w["storm"] else 0) + (3 if w["fog"] else 0)):
            x = (k * 211 + now * (3 + k % 3)) % (fw + 80) - 60
            self._put(ctx, ("cloud", k), x, (k * 97) % max(1, fh - 40), 0.8 if w["storm"] else 0.5)
        if w["storm"] or w.get("rain"):
            drops = int(60 + 260 * (w["storm"] or 0.5))
            pts = [((j * 97.31 - now * 50) % fw, (j * 53.17 + now * 230) % fh) for j in range(drops)]
            pixels(ctx, pts, (0.7, 0.8, 1.0), 0.5, size=(1, 3))
        if w["fog"]:
            for k in range(4):
                ctx.rectangle(0, int((k * fh / 4 + now * 4) % fh), fw, 14)
            ctx.set_source_rgba(0.85, 0.85, 0.9, 0.12)
            ctx.fill()
        # destellos sobre el mar (en pantalla, pero anclados al mundo para que no "naden")
        step = int(now * 2)
        pts = []
        for j in range(fw * fh // 6000):
            hx = h32(j, step)
            px, py = hx % fw, (hx >> 12) % fh
            wx, wy = self.to_world(px, py)
            tx, ty = self.tile_at(wx, wy)
            n = self.state["w"]
            if not (0 <= tx < n and 0 <= ty < n) or self.state["terrain"][ty * n + tx] == WATER:
                pts.append((px, py))
        pixels(ctx, pts, (1, 1, 1), 0.5, size=(2, 1))

    def _draw_events(self, ctx, now):
        alive = []
        for ev in self.bolts:
            age = now - ev["start"]
            if age < 0:
                alive.append(ev)
                continue
            if age > 0.7:
                continue
            alive.append(ev)
            if ev["type"] in ("lightning", "shield"):
                tx, ty = self.tile_xy(ev["x"], ev["y"], self.elev_at(ev["x"], ev["y"]))
                ty += TH // 2
                if ev["type"] == "shield":   # el rayo pega en la cúpula del castillo
                    cx, cy = self.tile_xy(ev["sx"], ev["sy"], self.elev_at(ev["sx"], ev["sy"]))
                    tx, ty = cx, cy - 20
                    ctx.arc(cx, cy - 4, 16 + age * 10, math.pi, 2 * math.pi)
                    ctx.set_source_rgba(0.5, 0.95, 1.0, max(0, 0.9 - age))
                    ctx.set_line_width(1)
                    ctx.stroke()
                if age < 0.25:
                    rng = random.Random(ev["seed"])
                    x, y = tx + rng.randint(-30, 30), ty - 220
                    pts = [(x, y)]
                    while y < ty - 6:
                        y += rng.randint(5, 10)
                        x += rng.randint(-5, 5) + (tx - x) * 0.15
                        pts.append((x, min(y, ty)))
                    pts.append((tx, ty))
                    for width, col in ((3, (0.6, 0.75, 1.0, 0.5)), (1, (1, 1, 1, 1))):
                        ctx.move_to(*pts[0])
                        for p in pts[1:]:
                            ctx.line_to(*p)
                        ctx.set_line_width(width)
                        ctx.set_source_rgba(*col)
                        ctx.stroke()
                    if age < 0.07:
                        ctx.save()
                        ctx.identity_matrix()
                        ctx.set_source_rgba(1, 1, 1, 0.3)
                        ctx.paint()
                        ctx.restore()
            elif ev["type"] == "conquest":
                tx, ty = self.tile_xy(ev["x"], ev["y"])
                col = next((tuple(c / 255 for c in f["color"]) for f in self.state["factions"]
                            if f["id"] == ev["faction"]), (1, 1, 1))
                r = int(age * 8)
                pixels(ctx, [(tx - r, ty), (tx + r, ty), (tx, ty - r), (tx, ty + r)], col, 1 - age)
            elif ev["type"] == "eruption" and age < 0.15:
                ctx.save()
                ctx.identity_matrix()
                ctx.set_source_rgba(1, 0.4, 0.1, 0.35)
                ctx.paint()
                ctx.restore()
        self.bolts = alive


def _neigh(i: int, w: int, h: int):
    x, y = i % w, i // w
    if x > 0: yield i - 1
    if x < w - 1: yield i + 1
    if y > 0: yield i - w
    if y < h - 1: yield i + w
