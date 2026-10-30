"""Timelapse del día: cómo se movieron las fronteras.

El daemon guarda cada 10 minutos una foto liviana del mapa (dueño, terreno e
incendios de cada casilla, comprimidos: ~2 KB) en data_dir()/timelapse/AAAA-MM-DD.jsonl.
`export()` dibuja cada foto como un mapa político isométrico y arma un GIF
(con Pillow si está; si no, con un codificador GIF propio, más lento y más chico).
"""
import base64
import datetime as dt
import json
import time
import zlib
from pathlib import Path

from . import paths

EVERY = 600            # segundos entre fotos
_TERRAIN = {0: (0.18, 0.33, 0.55), 1: (0.86, 0.79, 0.55), 2: (0.42, 0.63, 0.32), 3: (0.27, 0.5, 0.25),
            4: (0.55, 0.58, 0.38), 5: (0.55, 0.53, 0.52)}
_last_record = 0.0


def folder() -> Path:
    return paths.data_dir() / "timelapse"


def _pack(values) -> str:
    return base64.b64encode(zlib.compress(bytes(min(255, max(0, v)) for v in values), 9)).decode()


def _unpack(text: str) -> bytes:
    return zlib.decompress(base64.b64decode(text))


def record(world, force: bool = False):
    """Una foto cada EVERY segundos (la llama el daemon en cada tick)."""
    global _last_record
    now = time.time()
    if not force and now - _last_record < EVERY:
        return
    _last_record = now
    snap = {"t": round(now), "tick": world.tick, "n": world.n, "kingdom": world.kingdom,
            "owner": _pack(world.owner), "terrain": _pack(world.terrain), "burn": _pack(1 if b else 0 for b in world.burn),
            "factions": [[f["id"], f["color"], f["capital"], bool(f.get("alive")), f["realm"]]
                         for f in world.factions.values() if f.get("alive") or f.get("tiles")]}
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{time.strftime('%Y-%m-%d', time.localtime(now))}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(snap) + "\n")


def load(day: str) -> list[dict]:
    f = folder() / f"{day}.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# -- dibujo ---------------------------------------------------------------------

def render(snap: dict, title: str, scale: int = 10, antialias: bool = True):
    """Mapa político isométrico de una foto. Devuelve una superficie cairo RGB24."""
    import cairo
    n = snap["n"]
    owner, terrain, burn = _unpack(snap["owner"]), _unpack(snap["terrain"]), _unpack(snap["burn"])
    tw, th = scale, scale // 2
    head = 3 * scale + 6
    W, H = n * tw + 2 * tw, n * th + 2 * th + head
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
    ctx = cairo.Context(surf)
    ctx.set_antialias(cairo.ANTIALIAS_NONE)
    ctx.set_source_rgb(0.08, 0.09, 0.12)
    ctx.paint()
    colors = {fid: tuple(c / 255 for c in col) for fid, col, *_ in snap["factions"]}
    ox, oy = W / 2, head + th

    def diamond(x, y):
        cx, cy = ox + (x - y) * tw / 2, oy + (x + y) * th / 2
        ctx.move_to(cx, cy - th / 2)
        ctx.line_to(cx + tw / 2, cy)
        ctx.line_to(cx, cy + th / 2)
        ctx.line_to(cx - tw / 2, cy)
        ctx.close_path()

    groups: dict[tuple, list[tuple[int, int]]] = {}
    for i in range(n * n):
        t, o = terrain[i], owner[i]
        col = _TERRAIN.get(t, _TERRAIN[2])
        if o and o in colors and t != 0:
            fc = colors[o]
            col = tuple(round(0.35 * a + 0.65 * b, 2) for a, b in zip(col, fc))
        if burn[i] and t != 0:
            col = (0.95, 0.45, 0.15)
        groups.setdefault(col, []).append((i % n, i // n))
    for col, tiles in groups.items():
        for x, y in tiles:
            diamond(x, y)
        ctx.set_source_rgb(*col)
        ctx.fill()
    for fid, col, cap, alive, realm in snap["factions"]:     # capitales
        if not alive:
            continue
        x, y = cap
        cx, cy = ox + (x - y) * tw / 2, oy + (x + y) * th / 2
        ctx.rectangle(cx - scale * 0.3, cy - scale * 0.7, scale * 0.6, scale * 0.6)
        ctx.set_source_rgb(1, 0.93, 0.6)
        ctx.fill_preserve()
        ctx.set_source_rgb(0.15, 0.12, 0.1)
        ctx.set_line_width(1)
        ctx.stroke()
    fo = cairo.FontOptions()
    fo.set_antialias(cairo.ANTIALIAS_DEFAULT if antialias else cairo.ANTIALIAS_NONE)
    ctx.set_font_options(fo)
    ctx.select_font_face("Liberation Mono", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(max(10, scale * 1.3))
    ctx.set_source_rgb(0.96, 0.84, 0.43)
    ctx.move_to(scale, scale * 1.6)
    ctx.show_text(title)
    stamp = time.strftime("%H:%M", time.localtime(snap["t"]))
    realms = sum(1 for f in snap["factions"] if f[3])
    ctx.set_font_size(max(9, scale * 1.1))
    ctx.set_source_rgb(0.85, 0.86, 0.88)
    from .i18n import ui
    text = f"{stamp}  ·  {ui('timelapse.realms', n=realms)}"
    ctx.move_to(W - ctx.text_extents(text).x_advance - scale, scale * 1.6)
    ctx.show_text(text)
    surf.flush()
    return surf


# -- GIF --------------------------------------------------------------------------

def _rgb_bytes(surf) -> bytes:
    """RGB24 de cairo (BGRx little-endian) -> RGB."""
    w, h, stride = surf.get_width(), surf.get_height(), surf.get_stride()
    data = bytes(surf.get_data())
    out = bytearray(w * h * 3)
    for y in range(h):
        row = data[y * stride:y * stride + w * 4]
        out[y * w * 3:(y + 1) * w * 3:3] = row[2::4]
        out[y * w * 3 + 1:(y + 1) * w * 3:3] = row[1::4]
        out[y * w * 3 + 2:(y + 1) * w * 3:3] = row[0::4]
    return bytes(out)


def _lzw(indices: bytes, min_size: int = 8) -> bytes:
    """Compresión LZW de GIF (códigos de tamaño variable, sin 'early change')."""
    clear, eoi = 1 << min_size, (1 << min_size) + 1
    out = bytearray()
    acc = nbits = 0

    def emit(code, size):
        nonlocal acc, nbits
        acc |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(acc & 255)
            acc >>= 8
            nbits -= 8

    def fresh():
        return {bytes((i,)): i for i in range(clear)}

    table, nxt, size = fresh(), eoi + 1, min_size + 1
    emit(clear, size)
    cur = b""
    for b in indices:
        seq = cur + bytes((b,))
        if seq in table:
            cur = seq
            continue
        emit(table[cur], size)
        if nxt < 4096:
            table[seq] = nxt
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
        else:
            emit(clear, size)
            table, nxt, size = fresh(), eoi + 1, min_size + 1
        cur = bytes((b,))
    if cur:
        emit(table[cur], size)
    emit(eoi, size)
    if nbits:
        out.append(acc & 255)
    return bytes(out)


def _write_gif(path: Path, frames: list[bytes], w: int, h: int, delays: list[int]):
    """GIF animado con paleta global (hasta 256 colores; si hay más, se redondean)."""
    shift = 0
    while True:
        mask = (0xFF >> shift) << shift
        palette: dict[bytes, int] = {}
        for rgb in frames:
            for k in range(0, len(rgb), 3):
                c = bytes((rgb[k] & mask, rgb[k + 1] & mask, rgb[k + 2] & mask))
                if c not in palette:
                    palette[c] = len(palette)
                    if len(palette) > 256:
                        break
            if len(palette) > 256:
                break
        if len(palette) <= 256:
            break
        shift += 1
    pal = b"".join(sorted(palette, key=palette.get)).ljust(256 * 3, b"\0")
    with open(path, "wb") as f:
        f.write(b"GIF89a" + w.to_bytes(2, "little") + h.to_bytes(2, "little") + bytes((0xF7, 0, 0)) + pal)
        f.write(b"!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")                     # repetir para siempre
        for rgb, delay in zip(frames, delays):
            idx = bytes(palette[bytes((rgb[k] & mask, rgb[k + 1] & mask, rgb[k + 2] & mask))]
                        for k in range(0, len(rgb), 3))
            f.write(b"!\xf9\x04\x04" + (delay // 10).to_bytes(2, "little") + b"\x00\x00")
            f.write(b"," + bytes(4) + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00")
            data = _lzw(idx)
            f.write(b"\x08")
            for k in range(0, len(data), 255):
                chunk = data[k:k + 255]
                f.write(bytes((len(chunk),)) + chunk)
            f.write(b"\x00")
        f.write(b";")


def export(day: str | None = None, out_dir: Path | None = None, kingdom: str | None = None) -> Path | None:
    """Arma el GIF del día. Devuelve la ruta (None si todavía no hay fotos)."""
    day = day or time.strftime("%Y-%m-%d")
    snaps = load(day)
    if not snaps:
        return None
    date = dt.date.fromisoformat(day).strftime("%d/%m/%Y")
    title = f"{kingdom or snaps[-1].get('kingdom', '')} — {date}"
    out_dir = out_dir or folder()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kernel-kingdom-timelapse-{day}.gif"
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image:
        imgs = []
        for s in snaps:
            surf = render(s, title, scale=10)
            imgs.append(Image.frombytes("RGB", (surf.get_width(), surf.get_height()), _rgb_bytes(surf)))
        pal = [im.quantize(colors=200, method=Image.Quantize.MEDIANCUT) for im in imgs]
        durations = [180] * (len(pal) - 1) + [2000]
        pal[0].save(path, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
        return path
    step = max(1, len(snaps) // 72)                   # sin Pillow: más chico y menos cuadros
    picked = snaps[::step]
    frames, w, h = [], 0, 0
    for s in picked:
        surf = render(s, title, scale=6, antialias=False)
        w, h = surf.get_width(), surf.get_height()
        frames.append(_rgb_bytes(surf))
    _write_gif(path, frames, w, h, [180 * step] * (len(frames) - 1) + [2000])
    return path
