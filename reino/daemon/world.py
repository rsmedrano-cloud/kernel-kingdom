"""Simulación del reino: traduce métricas del sistema en mecánicas de juego.

El mundo vive en el daemon (no en el renderer) para que siga evolucionando
aunque el fondo esté tapado o el renderer apagado: es un idle game.

Grilla N*N en listas planas (índice = y * N + x):
  terrain  tipo de terreno (constantes abajo). También cambia: los bosques
           crecen y se talan, los incendios dejan pradera, la lava deja colinas
  owner    id de facción dueña de la casilla (0 = libre)
  level    altura del edificio 0..4
  burn     ticks restantes de casilla quemada (rayo, incendio)
  lava     ticks restantes de lava (erupción)
  ruin     ticks restantes de ruinas (barrio abandonado)
  wall     1 si hay muralla de una fortaleza
  road     1 si hay camino
  bld      tipo de edificio (reino/buildings.py): plaza, calle, mercado, granja...
  born     tick en que la casilla fue ocupada (obras en construcción)

La isla no es un rombo: se genera con forma de elipse *en pantalla*
(proporción `aspect`), así llena un monitor 16:9 en vez de quedar un rombo
chico en el medio. Cada instalación tiene su propia isla (semilla al azar).
"""
import heapq
import json
import math
import random
import re
import socket
import time
from pathlib import Path

from .. import buildings as B
from ..i18n import tr, words
from .lore import Chronicle, Lore, cap, fmt_mb, landmark_name, rng_for, the

WATER, SAND, GRASS, FOREST, HILL, MOUNTAIN = range(6)
LAND = {SAND, GRASS, FOREST, HILL}
GRIDS = ("terrain", "owner", "level", "burn", "lava", "ruin", "wall", "road", "bld", "born")
SAVE_VERSION = 4
MILESTONES = (25, 50, 100, 200, 400, 800)
KEY_MILESTONES = (1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
LANDMARKS = ("oraculo", "arena", "teatro", "taberna", "atalaya", "molino", "granero", "mercado", "herreria")
POWERS = ("rayo", "lluvia", "bendicion", "heroe", "bosque", "faro", "fiesta")
NO_SHIELD = {"tower", "mine", "lighthouse", "embassy"}


def _value_noise(n: int, rng: random.Random, cell: float) -> list[float]:
    g = int(n / cell) + 2
    grid = [[rng.random() for _ in range(g)] for _ in range(g)]
    out = []
    for y in range(n):
        fy = y / cell
        iy, ty = int(fy), fy - int(fy)
        ty = ty * ty * (3 - 2 * ty)
        for x in range(n):
            fx = x / cell
            ix, tx = int(fx), fx - int(fx)
            tx = tx * tx * (3 - 2 * tx)
            top = grid[iy][ix] * (1 - tx) + grid[iy][ix + 1] * tx
            bot = grid[iy + 1][ix] * (1 - tx) + grid[iy + 1][ix + 1] * tx
            out.append(top * (1 - ty) + bot * ty)
    return out


def island_distance(n: int, aspect: float):
    """d(x, y): 0 en el centro, 1 en el borde de una elipse con la proporción
    de la pantalla (en coordenadas isométricas), que entra justa en el rombo."""
    q = 0.94 / math.sqrt(1 + (aspect / 2) ** 2)
    p = aspect / 2 * q

    def d(x: int, y: int) -> float:
        u = (x - y) / n
        v = (x + y - (n - 1)) / n
        return math.hypot(u / p, v / q)
    return d


def generate_terrain(n: int, aspect: float, seed: int) -> list[int]:
    rng = random.Random(seed)
    n1, n2, n3 = (_value_noise(n, rng, c) for c in (14, 7, 3.5))
    moist = _value_noise(n, rng, 8)
    dist = island_distance(n, aspect)
    elev, inside = [], 0
    for i in range(n * n):
        d = dist(i % n, i // n)
        inside += d < 0.97
        noise = 0.55 * n1[i] + 0.30 * n2[i] + 0.15 * n3[i]
        elev.append(0.65 * noise + 0.85 * (1 - d * d))
    land = inside / len(elev)
    ranked = sorted(elev)
    water_p = 1 - land
    cut = [ranked[min(len(ranked) - 1, int(len(ranked) * p))]
           for p in (water_p, water_p + 0.05 * land, water_p + 0.80 * land, water_p + 0.93 * land)]
    terrain = []
    for i, e in enumerate(elev):
        if e < cut[0]:
            t = WATER
        elif e < cut[1]:
            t = SAND
        elif e < cut[2]:
            t = FOREST if moist[i] + 0.3 * n3[i] > 0.80 else GRASS
        elif e < cut[3]:
            t = HILL
        else:
            t = MOUNTAIN
        terrain.append(t)
    return terrain


class World:
    def __init__(self, cfg: dict, data: dict | None = None):
        self.cfg = cfg
        wc = cfg["world"]
        self.n = self.w = self.h = wc["size"]
        self.rng = random.Random()
        ok = bool(data) and data.get("v") in (2, 3, SAVE_VERSION) and data["w"] == self.n \
            and data.get("aspect") == wc["aspect"]
        # semilla: la guardada; si no, la de la config; 0 = una al azar (cada instalación, su isla)
        self.seed = data.get("seed", 1337) if ok else (wc.get("seed") or random.randrange(1, 2 ** 31))
        self.lore = Lore(cfg, self.seed)
        if ok:
            for g in GRIDS:
                setattr(self, g, data.get(g) or [0] * (self.n * self.n))
            self.tick = data["tick"]
            self.factions = {f["name"]: f for f in data["factions"]}
            self.structures = {s["key"]: s for s in data["structures"]}
            self.resources = data["resources"]
            self.volcano = data["volcano"]
            self.kingdom = data["kingdom"]
            self.era = data["era"]
            self.counters = data["counters"]
            self.chronicle = Chronicle(data["chronicle"], self.seed)
            self.notables = data.get("notables", [])
            self.wars = data.get("wars", {})
            self.visitors = data.get("visitors", {})
            self.power_log = data.get("power_log", [])
            self.expeditions = data.get("expeditions", [])
            self.war_cooldown = data.get("war_cooldown", {})
            for f in self.factions.values():          # mundos v3: edificios únicos guardados por nombre
                f["specials"] = {_migrate_special(k): v for k, v in f.get("specials", {}).items()}
        else:
            cells = self.n * self.n
            self.terrain = generate_terrain(self.n, wc["aspect"], self.seed)
            for g in GRIDS[1:]:
                setattr(self, g, [0] * cells)
            self.tick = 0
            self.factions: dict[str, dict] = {}
            self.structures: dict[str, dict] = {}
            # un tesoro inicial chico para poder probar los poderes desde el primer minuto
            self.resources = {"gold": 60.0, "stone": 10.0, "defense": 0.0, "mana": 25.0}
            peaks = [i for i, t in enumerate(self.terrain) if t == MOUNTAIN] or [cells // 2]
            self.volcano = list(self.xy(max(peaks, key=self._peakness)))
            self.kingdom = self.lore.kingdom(socket.gethostname())
            self.era = {"boot": None, "n": 0}
            self.counters = {"keys": 0, "net_rx": 0.0, "births": 0, "lightning": 0, "conquests": 0}
            self.chronicle = Chronicle([], self.seed)
            self.notables: list[dict] = []
            self.wars: dict[str, dict] = {}
            self.visitors: dict[str, dict] = {}
            self.power_log: list[dict] = []
            self.expeditions: list[dict] = []
            self.war_cooldown: dict[str, int] = {}
            self._say("mundo", "world.born", kingdom=self.kingdom)
        self.era.setdefault("idx", self.lore.era_index(self.era.get("boot") or 0))
        self.land_tiles = sum(1 for t in self.terrain if t in LAND)
        self.routes: list[list[int]] = []
        self._anchor_sig = None
        self._struct_tiles: dict[int, dict] = {}
        self.cpu_ema = 0.0
        self.hot_time = 0.0
        self.eruption_ticks = 0
        self.rain_until = 0
        self.episodes: dict[str, dict] = {}
        self.weather = {"storm": 0.0, "eruption": False, "fog": False, "heat": False,
                        "plague": False, "rain": False, "culprit": None}
        self.activity = {}
        rng = rng_for(self.seed, "tiles")
        self._tile_rand = [rng.random() for _ in range(self.n * self.n)]   # azar fijo por casilla
        self._ensure_mine()

    # -- utilidades ----------------------------------------------------------

    def xy(self, i: int) -> tuple[int, int]:
        return i % self.n, i // self.n

    def idx(self, x: int, y: int) -> int:
        return y * self.n + x

    def neighbors(self, i: int):
        x, y = self.xy(i)
        if x > 0: yield i - 1
        if x < self.n - 1: yield i + 1
        if y > 0: yield i - self.n
        if y < self.n - 1: yield i + self.n

    def _dist(self, i: int, xy) -> float:
        x, y = self.xy(i)
        return math.hypot(x - xy[0], y - xy[1])

    def _peakness(self, i: int) -> int:
        x, y = self.xy(i)
        return sum(1 for dy in range(-2, 3) for dx in range(-2, 3)
                   if 0 <= x + dx < self.n and 0 <= y + dy < self.n
                   and self.terrain[self.idx(x + dx, y + dy)] == MOUNTAIN)

    def _wild(self, i: int) -> bool:
        """Casilla sin dueño ni obras: la naturaleza puede cambiarla."""
        return not (self.owner[i] or self.road[i] or self.wall[i] or self.burn[i] or self.lava[i]
                    or self.ruin[i] or i in self._struct_tiles)

    def _place(self, key: str, prefer_hill: bool, coastal: bool = False) -> list[int]:
        """Ubicación determinista (misma clave -> mismo lugar) lejos de los demás."""
        rng = rng_for(self.seed, key)
        taken = [f["capital"] for f in self.factions.values()]
        taken += [[s["x"], s["y"]] for s in self.structures.values()]
        land = [i for i, t in enumerate(self.terrain)
                if t in LAND and not self.wall[i] and i not in self._struct_tiles and not self.owner[i]
                and (not coastal or any(self.terrain[j] == WATER for j in self.neighbors(i)))]
        if not land:
            land = [i for i, t in enumerate(self.terrain) if t in LAND]
        best, best_score = land[0], -1.0
        for i in rng.sample(land, min(80, len(land))):
            spread = min((self._dist(i, p) for p in taken), default=14.0)
            score = min(spread, 14.0) + (3 if prefer_hill and self.terrain[i] == HILL else 0) + rng.random()
            if score > best_score:
                best, best_score = i, score
        return list(self.xy(best))

    def _ensure_mine(self):
        if "mine:main" in self.structures:
            return
        cand = [i for i, t in enumerate(self.terrain)
                if t == HILL and any(self.terrain[j] == MOUNTAIN for j in self.neighbors(i))]
        cand = cand or [i for i, t in enumerate(self.terrain) if t == HILL] or [self.n * self.n // 2]
        i = rng_for(self.seed, "mine").choice(cand)
        x, y = self.xy(i)
        self.structures["mine:main"] = {"key": "mine:main", "kind": "mine", "name": "mina", "x": x, "y": y,
                                        "alive": True, "walls": 0, "activity": 0.0}

    def _say(self, kind: str, key: str, where=None, ckey: str | None = None, cooldown: int = 0, **kw):
        """Agrega a la crónica la plantilla `key` (con una variante al azar) en el idioma activo."""
        self.chronicle.add(self.tick, kind, self.chronicle.tr(key, **kw), where, ckey, cooldown, tkey=key)

    def _realm_kw(self, f: dict) -> dict:
        ruler = self.lore.ruler_str(f["ruler"])
        full = self.lore.the_full(f)
        return {"realm": f["realm"], "the_full": full, "The_full": cap(full), "ruler": ruler, "Ruler": cap(ruler),
                "name": f["name"]}

    @staticmethod
    def _fort_kw(title: str) -> dict:
        return {"the_fort": the(title), "The_fort": cap(the(title))}

    def _volcano(self) -> str:
        return tr("volcano", place=self.lore.place("volcan"))

    # -- persistencia --------------------------------------------------------

    @classmethod
    def load_or_new(cls, cfg: dict, path: Path) -> "World":
        try:
            return cls(cfg, json.loads(path.read_text()))
        except (OSError, ValueError, KeyError):
            return cls(cfg)

    def to_dict(self, full: bool = True) -> dict:
        d = {
            "v": SAVE_VERSION, "w": self.n, "h": self.n, "aspect": self.cfg["world"]["aspect"],
            "seed": self.seed, "tick": self.tick,
            "factions": list(self.factions.values()),
            "structures": list(self.structures.values()),
            "resources": {k: round(v, 2) for k, v in self.resources.items()},
            "volcano": self.volcano, "kingdom": self.kingdom, "era": self.era,
            "counters": self.counters, "weather": self.weather,
            "chronicle": self.chronicle.entries if full else self.chronicle.recent(40),
            "notables": self.notables if full else [h for h in self.notables if h["alive"]],
            "wars": self.wars, "visitors": self.visitors, "power_log": self.power_log[-5:],
            "expeditions": self.expeditions, "war_cooldown": self.war_cooldown,
        }
        for g in GRIDS:
            d[g] = getattr(self, g)
        return d

    def to_state(self) -> dict:
        """Lo que necesita el renderer (crónica recortada + datos efímeros)."""
        d = self.to_dict(full=False)
        d["chronicle"] = self.chronicle.recent(Chronicle.MAX)   # la ventana deja recorrer todo el historial
        d["routes"] = self.routes
        d["activity"] = self.activity
        d["era_name"] = self.lore.era_name(self.era)
        d["powers"] = self.cfg.get("powers", {})
        return d

    # -- simulación ----------------------------------------------------------

    def step(self, snap: dict, commands: list[dict] | None = None) -> list[dict]:
        """Avanza un tick. `commands` = poderes pedidos desde el wallpaper.
        Devuelve los eventos visuales del tick (rayos, conquistas...)."""
        self.tick += 1
        dt = snap.get("dt") or self.cfg["daemon"]["interval"]
        events: list[dict] = []
        self._check_era(snap)
        self._decay_timers()
        self._update_weather(snap, dt)
        self._update_structures(snap)
        self._update_factions(snap, events)
        self._update_roads()
        self._rebuild_walls()
        self._powers(commands or [], events)
        self._expeditions(events)
        self._weather_effects(snap, events)
        self._nature()
        self._economy_events(snap, dt)
        self._society(snap)
        self._ambition(events)
        self._drift()
        self._adventures()
        self._flavor(snap)
        self._update_resources(snap, dt)
        self._refresh_names()
        return events

    def _refresh_names(self):
        """Nombres visibles en el idioma activo (HUD y fichas del renderer)."""
        for f in self.factions.values():
            f["display"] = self.lore.full_realm(f)
            f["ruler_display"] = cap(self.lore.ruler_str(f["ruler"]))
            if f.get("cult"):
                f["cult_display"] = self.lore.cult_name(f["cult"])
        for h in self.notables:
            h["display"] = self.lore.hero_str(h)
            h["trait"] = self.lore.hero_trait(h)
        mine = self.structures["mine:main"]
        mine["title"] = self.lore.structure_name("mine", "profundas")
        acad = self.structures.get("tower:academy")
        if acad:
            acad["title"] = self.lore.structure_name("tower", "academia")
        for s in self.structures.values():
            if s["kind"] == "embassy":
                s["title"] = tr("embassy", land=self.lore.foreign_land(s["name"]))
            elif s["kind"] == "lighthouse":
                s["title"] = tr("lighthouse", place=self.lore.place(s["key"]))

    def _check_era(self, snap: dict):
        boot = snap.get("boot")
        if not boot or self.era["boot"] == boot:
            return
        first = self.era["boot"] is None
        old_kernel = self.era.get("kernel")
        kernel = snap.get("kernel")
        self.era = {"boot": boot, "idx": self.lore.era_index(boot), "n": self.era["n"] + 1, "kernel": kernel}
        era = self.lore.era_name(self.era)
        if first:
            boots = snap.get("boots") or []
            if len(boots) > 1:
                self._say("era", "era.annals", n=len(boots) - 1, oldest=boots[-1]["start"][:10],
                          gods=len({b["kernel"] for b in boots}))
            self._say("era", "era.start", era=era, god=tr("era.god", kernel=kernel) if kernel else "")
        else:
            self._say("era", "era.new", era=era)
            if kernel and old_kernel and kernel != old_kernel:
                self._say("era", "era.kernel", kernel=kernel, old=old_kernel)

    def _decay_timers(self):
        for i, v in enumerate(self.burn):
            if v:
                self.burn[i] = v - 1
                if v == 1 and self.terrain[i] == FOREST:
                    self.terrain[i] = GRASS             # el fuego se llevó el bosque
        for i, v in enumerate(self.ruin):
            if v:
                self.ruin[i] = v - 1
        for i, v in enumerate(self.lava):
            if v:
                self.lava[i] = v - 1
                if v == 1:                              # la lava se enfría: tierra quemada y roca
                    self.burn[i] = self.cfg["weather"]["burn_ticks"]
                    if self.terrain[i] in (GRASS, FOREST, SAND):
                        self.terrain[i] = HILL
                        self.counters["volcanic"] = self.counters.get("volcanic", 0) + 1

    # Facciones: navegadores (imperios) y apps (aldeas) ------------------------

    def _present_factions(self, snap: dict) -> dict[str, tuple[str, dict]]:
        present = {name: ("browser", g) for name, g in snap["browsers"].items()}
        ac = self.cfg["apps"]
        alive_apps = [n for n, f in self.factions.items() if f.get("kind") == "app" and f["alive"]]
        slots = ac["max"] - sum(1 for n in alive_apps if n in snap["apps"])
        for name, g in snap["apps"].items():
            f = self.factions.get(name)
            if f and f["alive"]:
                present[name] = ("app", g)        # ya fundada: se queda mientras esté en la lista
            elif g["rss_mb"] >= ac["min_rss_mb"] and slots > 0:
                present[name] = ("app", g)
                slots -= 1
        return present

    def _new_faction(self, name: str, kind: str, g: dict) -> dict:
        rng = rng_for(self.seed, name)
        colors = self.cfg.get("colors", {})
        landmarks = self.cfg["apps"].get("landmarks", {})
        f = {
            "id": max((f["id"] for f in self.factions.values()), default=0) + 1,
            "name": name, "kind": kind,
            "color": colors.get(name) or [rng.randrange(70, 230) for _ in range(3)],
            "capital": self._place(f"{kind}:{name}", prefer_hill=False),
            "style": 4 if kind == "app" else rng.randrange(4),
            "material": rng.randrange(5),
            "landmark": (landmarks.get(name) or rng.choice(LANDMARKS)) if kind == "app" else None,
            "realm": self.lore.realm(name),
            "since": g["since"], "founded": self.tick, "peak": 0, "dynasty": 0, "tiles": 0,
        }
        f["ruler"] = self.lore.new_ruler(f, g.get("since") or 0.0)
        self.factions[name] = f
        return f

    def _update_factions(self, snap: dict, events: list[dict]):
        rules = self.cfg["browsers_rules"]
        apps = self.cfg["apps"]
        present = self._present_factions(snap)
        now = snap["t"]

        for name, (kind, g) in present.items():
            f = self.factions.get(name)
            if f is None:
                f = self._new_faction(name, kind, g)
                f["alive"] = True
                if kind == "app":
                    self._say("fundacion", "found.app", f["capital"], name=name, realm=f["realm"],
                              landmark=landmark_name(f["landmark"]))
                else:
                    self._say("fundacion", "found.realm", f["capital"], **self._realm_kw(f))
            elif not f.get("alive"):
                f["ruler"] = self.lore.new_ruler(f, now)
                self._say("dinastia", "dyn.reborn", f["capital"], **self._realm_kw(f))
            elif abs(g["since"] - f.get("since", g["since"])) > 5:   # reinició el proceso raíz
                old = self.lore.ruler_str(f["ruler"])
                f["ruler"] = self.lore.new_ruler(f, now)
                self._say("dinastia", "dyn.succession", f["capital"], old=old, Old=cap(old), **self._realm_kw(f))
            f["since"] = g["since"]

        land_cap = int(self.land_tiles * rules["max_land_fraction"])
        for name, f in self.factions.items():
            item = present.get(name)
            g = item[1] if item else None
            if f.get("alive") and not g:
                self._say("caida", "fall", f["capital"], tiles=f.get("tiles", 0), **self._realm_kw(f))
            f["alive"] = g is not None
            f["procs"] = g["procs"] if g else 0
            f["rss_mb"] = g["rss_mb"] if g else 0.0
            f["cpu"] = g["cpu"] if g else 0.0
            if f["kind"] == "app":
                f["target"] = min(apps["max_tiles"], int(f["rss_mb"] / apps["mb_per_tile"]))
            else:
                f["target"] = min(land_cap, int(f["rss_mb"] / rules["mb_per_tile"]))
            at_war = any(f["id"] in (w["a"], w["b"]) for w in self.wars.values())
            if f.get("bonus") and not at_war and self.tick % 10 == 0:
                f["bonus"] -= 1                       # en paz, lo conquistado se va abandonando
            if f["alive"] and f["target"]:
                f["target"] += f.get("bonus", 0)
            f["heading"] = self._heading(f, now)
            for w in self.wars.values():                 # en guerra, el atacante avanza hacia el enemigo
                if w.get("until") and w["a"] == f["id"]:
                    foe = next((o for o in self.factions.values() if o["id"] == w["b"]), None)
                    if foe:
                        (ax, ay), (bx, by) = f["capital"], foe["capital"]
                        f["heading"] = math.atan2(by - ay, bx - ax)
            r = f["ruler"]
            if f["alive"] and not r.get("ep") and not r.get("epithet") and now - r["since"] > 600:
                r["ep"] = self.lore.epithet_key(f, r, now)

        owned: dict[int, list[int]] = {}
        for i, o in enumerate(self.owner):
            if o:
                owned.setdefault(o, []).append(i)
        by_id = {f["id"]: f for f in self.factions.values()}
        conquests: dict[tuple[int, int], list[int]] = {}
        plague_victim = max(self.factions.values(), key=lambda f: f.get("tiles", 0), default=None) \
            if self.weather["plague"] else None

        # Las facciones más grandes juegan primero (y ganan los empates)
        for f in sorted(self.factions.values(), key=lambda f: -f["rss_mb"]):
            tiles = owned.get(f["id"], [])
            diff = f["target"] - len(tiles)
            if diff > 0 and not self.weather["fog"]:
                gained = self._grow(f, tiles, min(diff, rules["expand_per_tick"]), by_id, events)
                for i, loser in gained:
                    if loser:
                        owned[loser].remove(i)
                        conquests.setdefault((f["id"], loser), []).append(i)
                    self.born[i] = self.tick
                tiles += [i for i, _ in gained]
            elif diff < 0:
                n = min(-diff, rules["shrink_per_tick"])
                tiles.sort(key=lambda i: self._dist(i, f["capital"]))
                for i in tiles[-n:]:
                    self.owner[i], self.level[i], self.bld[i] = 0, 0, B.NONE
                    self.ruin[i] = rules["ruin_ticks"]
                del tiles[-n:]
            owned[f["id"]] = tiles
            f["tiles"] = len(tiles)

            self._zone(f, tiles, 1 if f is plague_victim else (2 if f["kind"] == "app" else 3))

            for m in MILESTONES:
                if f["tiles"] >= m > f.get("peak", 0):
                    f["peak"] = m
                    self._say("hito", "milestone", f["capital"], m=m, **self._realm_kw(f))

        for (att, dfn), lost in conquests.items():
            self._battle(by_id[att], by_id[dfn], lost)

    def _claimable(self, i: int, f: dict, by_id: dict) -> bool:
        if self.terrain[i] not in LAND or self.wall[i] or self.burn[i] or self.lava[i]:
            return False
        if i in self._struct_tiles:
            return False
        o = self.owner[i]
        if not o:
            return True
        # Fricción: le robo casillas a una facción con bastante menos RAM
        enemy = by_id.get(o)
        return bool(enemy) and f["rss_mb"] > enemy["rss_mb"] * self.cfg["browsers_rules"]["friction_ratio"]

    def _grow(self, f: dict, tiles: list[int], n: int, by_id: dict, events: list[dict]):
        fid = f["id"]
        if tiles:
            frontier = {j for i in tiles for j in self.neighbors(i) if self.owner[j] != fid}
        else:
            frontier = {self.idx(*f["capital"])}
            frontier |= set(self.neighbors(next(iter(frontier))))
        frontier = [j for j in frontier if self._claimable(j, f, by_id)]
        frontier.sort(key=lambda j: self._grow_score(j, f) + self.rng.random() * 1.5 + (6 if self.owner[j] else 0))
        gained = []
        for j in frontier[:n]:
            loser = self.owner[j]
            if loser:
                events.append({"type": "conquest", "x": j % self.n, "y": j // self.n, "faction": fid})
            self.owner[j], self.ruin[j] = fid, 0
            gained.append((j, loser))
        return gained

    # Fortalezas: servicios estables -----------------------------------------

    def _update_structures(self, snap: dict):
        fort = self.cfg["fortresses"]
        seen = {"mine:main"} | {k for k in self.structures if k.startswith("lighthouse:")}
        for key, g in snap["fortresses"].items():
            if g["uptime"] < fort["min_uptime"]:
                continue
            seen.add(key)
            s = self.structures.get(key)
            walls = sum(g["uptime"] >= t for t in fort["walls_at"])
            title = self.lore.structure_name(g["kind"], g["name"])
            if s is None:
                x, y = self._place(key, prefer_hill=True)
                s = self.structures[key] = {"key": key, "kind": g["kind"], "name": g["name"], "x": x, "y": y,
                                            "title": title, "walls": walls}
                self._say("fundacion", "fort.found", (x, y), name=g["name"], **self._fort_kw(title))
            elif not s.get("alive"):
                self._say("fortaleza", "fort.relit", (s["x"], s["y"]), **self._fort_kw(title))
            if walls > s.get("walls", 0):
                self._say("fortaleza", "fort.walls1" if walls == 1 else "fort.walls2", (s["x"], s["y"]),
                          **self._fort_kw(title))
            s.update(alive=True, dead_ticks=0, uptime=g["uptime"], rss_mb=g["rss_mb"], walls=walls, title=title)

        seen |= self._embassies(snap)

        # Torre de la Academia: entornos de desarrollo
        editors, compilers = snap["editors"], snap["compilers"]
        if editors or compilers:
            seen.add("tower:academy")
            s = self.structures.get("tower:academy")
            title = self.lore.structure_name("tower", "academia")
            if s is None:
                x, y = self._place("tower:academy", prefer_hill=False)
                s = self.structures["tower:academy"] = {"key": "tower:academy", "kind": "tower",
                                                        "name": "academia", "x": x, "y": y, "title": title}
                self._say("fundacion", "academy.found", (x, y), **self._fort_kw(title))
            floors = min(6, 1 + sum(editors.values()))
            if floors > s.get("floors", 0) and s.get("floors"):
                self._say("academia", "academy.floor", (s["x"], s["y"]), "academy-floor", 30,
                          floors=floors, **self._fort_kw(title))
            s.update(alive=True, dead_ticks=0, walls=0, floors=floors, editors=sorted(editors),
                     forging=sorted(compilers, key=lambda c: -compilers[c]["cpu"])[:2])

        for key, s in self.structures.items():
            if key not in seen:
                if s.get("alive"):
                    self._say("fortaleza", "fort.silent", (s["x"], s["y"]), name=s["name"],
                              **self._fort_kw(s.get("title", key)))
                s["alive"] = False
                s["dead_ticks"] = s.get("dead_ticks", 0) + 1

        # La casilla del castillo es suya: si había un barrio, lo desaloja
        self._struct_tiles = {}
        for s in self.structures.values():
            i = self.idx(s["x"], s["y"])
            self._struct_tiles[i] = s
            if s["alive"]:
                self.owner[i] = self.level[i] = 0

    def _rebuild_walls(self):
        self.wall = [0] * (self.n * self.n)
        for s in self.structures.values():
            if not s["alive"] or not s.get("walls") or s.get("breached_until", 0) > self.tick:
                continue
            r = s["walls"] + 1
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    x, y = s["x"] + dx, s["y"] + dy
                    if not (0 <= x < self.n and 0 <= y < self.n):
                        continue
                    i = self.idx(x, y)
                    if (self.terrain[i] in LAND and not self.owner[i] and not self.road[i]
                            and i not in self._struct_tiles):
                        self.wall[i] = 1      # donde pasa un camino queda una puerta

    # Caminos entre capitales y fortalezas -----------------------------------

    def _update_roads(self):
        anchors = sorted({tuple(f["capital"]) for f in self.factions.values() if f.get("alive")}
                         | {(s["x"], s["y"]) for s in self.structures.values()
                            if s.get("alive") and s["kind"] != "lighthouse"})
        if anchors == self._anchor_sig:
            return
        self._anchor_sig = anchors
        self.road = [0] * (self.n * self.n)
        self.routes = []
        if len(anchors) < 2:
            return
        # Árbol de expansión mínima (Prim) sobre distancia Manhattan, luego Dijkstra por tierra
        connected, rest = [anchors[0]], set(anchors[1:])
        while rest:
            a, b = min(((a, b) for a in connected for b in rest),
                       key=lambda ab: abs(ab[0][0] - ab[1][0]) + abs(ab[0][1] - ab[1][1]))
            connected.append(b)
            rest.discard(b)
            path = self._path(self.idx(*a), self.idx(*b))
            if path:
                self.routes.append(path)
                for i in path[1:-1]:
                    self.road[i] = 1

    def _path(self, start: int, goal: int) -> list[int] | None:
        """Dijkstra con costo por terreno + ruido fijo: caminos que serpentean
        y que prefieren reusar tramos ya construidos (se forman troncales)."""
        if not hasattr(self, "_road_noise"):
            rng = rng_for(self.seed, "roads")
            self._road_noise = [rng.random() for _ in range(self.n * self.n)]
        extra = {HILL: 2.5, FOREST: 0.8, SAND: 0.5}
        dist = {start: 0.0}
        prev = {start: None}
        heap = [(0.0, start)]
        while heap:
            d, i = heapq.heappop(heap)
            if i == goal:
                path = []
                while i is not None:
                    path.append(i)
                    i = prev[i]
                return path[::-1]
            if d > dist[i]:
                continue
            for j in self.neighbors(i):
                if j != goal and (self.terrain[j] not in LAND or j in self._struct_tiles):
                    continue
                step = 0.35 if self.road[j] else 1 + 1.6 * self._road_noise[j] + extra.get(self.terrain[j], 0)
                nd = d + step
                if nd < dist.get(j, 1e18):
                    dist[j], prev[j] = nd, i
                    heapq.heappush(heap, (nd, j))
        return None

    # Clima: CPU / RAM / swap / temperatura ------------------------------------

    def _update_weather(self, snap: dict, dt: float):
        wc = self.cfg["weather"]
        self.cpu_ema = 0.6 * self.cpu_ema + 0.4 * snap["cpu"]
        ema = self.cpu_ema

        storm = self.weather["storm"]
        threshold = wc["storm_cpu"] - (10 if storm else 0)  # histéresis
        storm = max(0.2, min(1.0, (ema - wc["storm_cpu"] + 10) / (110 - wc["storm_cpu"]))) if ema >= threshold else 0.0

        if ema >= wc["eruption_cpu"]:
            self.hot_time += dt
        else:
            self.hot_time = max(0.0, self.hot_time - 2 * dt)
        eruption = self.hot_time >= wc["eruption_hold"] or (self.weather["eruption"] and self.hot_time > 0)
        self.eruption_ticks = self.eruption_ticks + 1 if eruption else 0

        culprit = None
        if snap["compilers"]:
            culprit = max(snap["compilers"], key=lambda c: snap["compilers"][c]["cpu"])
        elif snap["top"]:
            culprit = snap["top"][0]["name"]
        temp = snap.get("cpu_temp")
        self.weather = {
            "storm": round(storm, 2),
            "eruption": eruption,
            "fog": snap["ram"] >= wc["fog_ram"],
            "heat": bool(temp and temp >= wc["heat_temp"]),
            "plague": snap.get("swap", 0) >= wc["plague_swap"],
            "rain": self.tick < self.rain_until,
            "culprit": culprit if (storm or eruption) else None,
            "cpu_ema": round(ema, 1),
        }
        w = self.weather
        if not culprit:
            mage = tr("mage.dark")
        else:
            mage = tr("mage.smiths" if snap["compilers"] else "mage.wizard", culprit=culprit)
        say = self.chronicle.tr
        self._episode("storm", bool(storm), 1, dt,
                      lambda e: say("storm.start", mage=mage, kingdom=self.kingdom, cpu=f"{ema:.0f}"),
                      lambda e: say("storm.end", mins=f"{e['dur'] / 60:.0f}", burned=e.get("burned", 0),
                                    blocked=e.get("blocked", 0)))
        self._episode("eruption", eruption, 1, dt,
                      lambda e: say("eruption.start", volcano=self._volcano()),
                      lambda e: say("eruption.end", volcano=self._volcano(), mins=f"{e['dur'] / 60:.0f}"))
        self._episode("fog", w["fog"], 1, dt, lambda e: say("fog.start", ram=f"{snap['ram']:.0f}"),
                      lambda e: say("fog.end"))
        self._episode("heat", w["heat"], 1, dt, lambda e: say("heat.start", temp=f"{temp:.0f}"),
                      lambda e: say("heat.end"))
        self._episode("plague", w["plague"], 1, dt, lambda e: say("plague.start", swap=f"{snap.get('swap', 0):.0f}"),
                      lambda e: say("plague.end"))

    def _episode(self, key: str, on: bool, value: float, dt: float, start, end, hold: int = 1, where=None):
        """Episodios con principio y fin (tormenta, flota, minas...): narra ambos extremos.
        `hold` = ticks seguidos que tiene que cumplirse (o dejar de cumplirse) la condición."""
        e = self.episodes.get(key)
        if e is None:
            if not on:
                return
            e = self.episodes[key] = {"on": 0, "off": 0, "active": False, "total": 0.0, "dur": 0.0, "peak": 0.0}
        e["on"], e["off"] = (e["on"] + 1, 0) if on else (0, e["off"] + 1)
        if e["active"]:
            if on:
                e["total"] += value * dt
                e["dur"] += dt
                e["peak"] = max(e["peak"], value)
            elif e["off"] >= hold:
                self.chronicle.add(self.tick, key, end(e), where, tkey=f"{key}.end")
                del self.episodes[key]
        elif on and e["on"] >= hold:
            e.update(active=True, total=value * dt, dur=dt, peak=value)
            self.chronicle.add(self.tick, key, start(e), where, tkey=f"{key}.start")
        elif not on:
            del self.episodes[key]

    def _strike(self, i: int, events: list[dict]):
        """Un rayo cae sobre la casilla i: quema lo que haya y algo de alrededor."""
        x, y = self.xy(i)
        burn = self.cfg["weather"]["burn_ticks"]
        events.append({"type": "lightning", "x": x, "y": y})
        self.counters["lightning"] += 1
        if self.terrain[i] != WATER:
            self.burn[i] = burn
        for j in self.neighbors(i):
            if (self.owner[j] or self.terrain[j] == FOREST) and self.rng.random() < 0.3:
                self.burn[j] = burn // 2

    def _weather_effects(self, snap: dict, events: list[dict]):
        wc = self.cfg["weather"]
        fort = self.cfg["fortresses"]
        storm = self.weather["storm"]
        ep = self.episodes.get("storm")
        if storm:
            strikes = sum(self.rng.random() < storm * wc["lightning_rate"] / 2 for _ in range(2))
            owned = [i for i, o in enumerate(self.owner) if o]
            land = owned or [i for i, t in enumerate(self.terrain) if t in LAND]
            for _ in range(strikes):
                i = self.rng.choice(land)
                x, y = self.xy(i)
                shield = self._shield_for(x, y, fort)
                if shield:
                    self.resources["defense"] -= fort["shield_cost"]
                    events.append({"type": "shield", "x": x, "y": y, "sx": shield["x"], "sy": shield["y"]})
                    if ep:
                        ep["blocked"] = ep.get("blocked", 0) + 1
                    continue
                if ep and self.owner[i] and self.level[i]:
                    ep["burned"] = ep.get("burned", 0) + 1
                self._strike(i, events)

        if self.weather["heat"] and not self.weather["rain"] and self.rng.random() < 0.15:   # incendio forestal
            forests = [i for i, t in enumerate(self.terrain) if t == FOREST and not self.burn[i]]
            if forests:
                i = self.rng.choice(forests)
                self.burn[i] = wc["burn_ticks"]
                for j in self.neighbors(i):
                    if self.terrain[j] == FOREST and self.rng.random() < 0.5:
                        self.burn[j] = wc["burn_ticks"]
                self._say("calor", "wildfire", self.xy(i), "wildfire", 60)

        if self.weather["eruption"]:
            vx, vy = self.volcano
            r = min(7, 1 + self.eruption_ticks // 3)
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    x, y = vx + dx, vy + dy
                    if 0 <= x < self.n and 0 <= y < self.n and math.hypot(dx, dy) <= r:
                        i = self.idx(x, y)
                        if self.terrain[i] != WATER and self.rng.random() < 0.35:
                            self.lava[i] = max(self.lava[i], 6)
            if self.eruption_ticks == 1:
                events.append({"type": "eruption", "x": vx, "y": vy})

    def _shield_for(self, x: int, y: int, fort: dict) -> dict | None:
        if self.resources["defense"] < fort["shield_cost"]:
            return None
        for s in self.structures.values():
            if s["alive"] and s["kind"] not in NO_SHIELD:
                if max(abs(s["x"] - x), abs(s["y"] - y)) <= fort["shield_radius"] + s.get("walls", 0):
                    return s
        return None

    # Naturaleza: el terreno también cambia ---------------------------------

    def _nature(self):
        """Cada ~30 s: los bosques se expanden sobre la pradera libre (hasta un
        tope), los aserraderos talan el bosque vecino, y la crónica nota cuando
        el paisaje cambió mucho."""
        if self.tick % 15:
            return
        rng = self.rng
        forests = [i for i, t in enumerate(self.terrain) if t == FOREST]
        if len(forests) < 0.3 * self.land_tiles:
            for i in rng.sample(forests, min(10, len(forests))):
                for j in self.neighbors(i):
                    if self.terrain[j] == GRASS and self._wild(j) and rng.random() < 0.15:
                        self.terrain[j] = FOREST
        felled_by: dict[int, int] = {}
        for i, b in enumerate(self.bld):
            if b == B.LUMBER and self.owner[i] and rng.random() < 0.25:
                for j in self.neighbors(i):
                    if self.terrain[j] == FOREST and self._wild(j):
                        self.terrain[j] = GRASS
                        felled_by[self.owner[i]] = felled_by.get(self.owner[i], 0) + 1
                        break
        for fid, n in felled_by.items():
            total = self.counters.get("felled", 0) + n
            if total // 25 > self.counters.get("felled", 0) // 25:
                f = next(f for f in self.factions.values() if f["id"] == fid)
                self._say("naturaleza", "nature.felled", f["capital"], realm=f["realm"], n=total)
            self.counters["felled"] = total
        count = sum(1 for t in self.terrain if t == FOREST)
        last = self.counters.get("forest_last") or count
        if count > last * 1.08:
            self._say("naturaleza", "nature.grows")
            self.counters["forest_last"] = count
        elif count < last * 0.92:
            self._say("naturaleza", "nature.shrinks", kingdom=self.kingdom)
            self.counters["forest_last"] = count
        else:
            self.counters.setdefault("forest_last", count)
        if self.counters.get("volcanic", 0) >= 5:
            self._say("naturaleza", "nature.volcanic", self.volcano)
            self.counters["volcanic"] = 0

    # Poderes del Soberano (el usuario gasta recursos) -------------------------

    def _powers(self, commands: list[dict], events: list[dict]):
        costs = self.cfg.get("powers", {})
        for cmd in commands:
            power = cmd.get("power")
            cost = costs.get(power)
            if power not in POWERS or cost is None:
                continue
            name = words("powers")[power][0]
            if any(self.resources.get(k, 0) < v for k, v in cost.items()):
                res = words("resources")
                self._feedback(power, False, tr("power.fail", power=name,
                                                cost=", ".join(f"{v} {res[k]}" for k, v in cost.items())))
                continue
            x, y = cmd.get("x", -1), cmd.get("y", -1)
            i = self.idx(x, y) if 0 <= x < self.n and 0 <= y < self.n else None
            if not getattr(self, "_power_" + power)(i, events):
                self._feedback(power, False, tr("power.notarget", power=name))
                continue
            for k, v in cost.items():
                self.resources[k] -= v
            self._feedback(power, True, name)

    def _feedback(self, power: str, ok: bool, msg: str):
        self.power_log = (self.power_log + [{"t": round(time.time()), "power": power, "ok": ok, "msg": msg}])[-5:]

    def _target_name(self, i: int) -> str:
        s = self._struct_tiles.get(i)
        if s:
            return the(s.get("title", s["name"]))
        f = next((f for f in self.factions.values() if f["id"] == self.owner[i]), None)
        if f:
            return f["realm"]
        return words("terrain")[self.terrain[i]].lower()

    def _realm_at(self, i: int | None) -> dict | None:
        if i is None:
            return None
        return next((f for f in self.factions.values() if f["id"] == self.owner[i] and f.get("alive")), None)

    def _power_rayo(self, i, events) -> bool:
        if i is None:
            return False
        self._strike(i, events)
        self._say("poder", "power.rayo", self.xy(i), target=self._target_name(i))
        return True

    def _power_lluvia(self, i, events) -> bool:
        self.burn = [0] * len(self.burn)
        self.lava = [0] * len(self.lava)
        self.rain_until = self.tick + 45
        self.resources["gold"] += 20
        self._say("poder", "power.lluvia", kingdom=self.kingdom)
        return True

    def _power_bendicion(self, i, events) -> bool:
        f = self._realm_at(i)
        if not f:
            return False
        self.resources["gold"] += 30 + f["tiles"] * 0.5
        f["blessed"] = self.tick + 150
        self._say("poder", "power.bendicion", f["capital"], realm=f["realm"])
        return True

    def _power_heroe(self, i, events) -> bool:
        f = self._realm_at(i)
        if not f:
            return False
        h = self._spawn_hero(f)
        self._say("heroe", "power.heroe", f["capital"], hero=self.lore.hero_str(h), realm=f["realm"])
        return True

    def _power_bosque(self, i, events) -> bool:
        if i is None or self.terrain[i] not in (GRASS, SAND, HILL) or not self._wild(i):
            return False
        x0, y0 = self.xy(i)
        tiles = [j for j in [i] + [self.idx(x0 + dx, y0 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                                   if (dx or dy) and 0 <= x0 + dx < self.n and 0 <= y0 + dy < self.n]
                 if self.terrain[j] in (GRASS, HILL, SAND) and self._wild(j)]
        place = self.lore.place(f"bosque:{i}")
        f = self._nearest_realm(i)
        self._launch("plant", f, i, work=2 * len(tiles), n=3, tiles=tiles, place=place)
        self._say("poder", "power.bosque", self.xy(i), place=place)
        if f:
            self._say("expedicion", "exp.plant.start", self.xy(i), realm=f["realm"], place=place)
        return True

    def _power_faro(self, i, events) -> bool:
        if i is None or self.terrain[i] not in LAND or not self._wild(i) \
                or not any(self.terrain[j] == WATER for j in self.neighbors(i)):
            return False
        x, y = self.xy(i)
        key = f"lighthouse:{x}:{y}"
        title = tr("lighthouse", place=self.lore.place(key))
        self.structures[key] = {"key": key, "kind": "lighthouse", "name": "faro", "x": x, "y": y,
                                "alive": True, "walls": 0, "title": title, "building": True}
        self._struct_tiles[i] = self.structures[key]
        self._say("obra", "power.faro", (x, y), title=the(title))
        f = self._nearest_realm(i)
        self._launch("build", f, i, work=25, n=4, key=key)
        if f:
            self._say("expedicion", "exp.build.start", (x, y), realm=f["realm"], title=the(title))
        else:
            self._say("expedicion", "exp.build.alone", (x, y), title=the(title))
        return True

    def _power_fiesta(self, i, events) -> bool:
        f = self._realm_at(i)
        if not f:
            return False
        f["festival"] = self.tick + 90
        self._say("poder", "power.fiesta", f["capital"], realm=f["realm"])
        return True

    # Expediciones: grupos que caminan por el mapa y hacen algo al llegar -----

    def _nearest_realm(self, i: int) -> dict | None:
        alive = [f for f in self.factions.values() if f.get("alive")]
        return min(alive, key=lambda f: self._dist(i, f["capital"]), default=None)

    def _launch(self, kind: str, f: dict | None, target: int, work: int, n: int, **data) -> dict:
        """Manda `n` personas de la capital de `f` a la casilla `target` (por tierra).
        Tardan ~1 tick cada 2 casillas en llegar y `work` ticks trabajando allá."""
        src = self.idx(*f["capital"]) if f else target
        path = (self._path(src, target) or [src, target]) if src != target else [target]
        self.counters["expeditions"] = self.counters.get("expeditions", 0) + 1
        e = {"id": self.counters["expeditions"], "kind": kind, "fid": f["id"] if f else 0, "path": path,
             "start": self.tick, "eta": max(2, len(path) // 2), "work": work, "n": n,
             "target": list(self.xy(target)), "data": data}
        self.expeditions.append(e)
        return e

    def _expeditions(self, events: list[dict]):
        keep = []
        for e in self.expeditions:
            t = self.tick - e["start"] - e["eta"]
            if t < 0:
                keep.append(e)                                  # todavía caminando
            elif t < e["work"]:
                self._exp_work(e, t)
                keep.append(e)
            else:
                self._exp_finish(e, events)
        self.expeditions = keep

    def _exp_work(self, e: dict, t: int):
        if e["kind"] == "plant" and t % 2 == 0:                 # plantan de a una casilla
            tiles = e["data"]["tiles"]
            if t // 2 < len(tiles):
                j = tiles[t // 2]
                if self.terrain[j] in (GRASS, HILL, SAND) and self._wild(j):
                    self.terrain[j] = FOREST

    def _exp_finish(self, e: dict, events: list[dict]):
        f = next((f for f in self.factions.values() if f["id"] == e["fid"]), None)
        x, y = e["target"]
        i = self.idx(x, y)
        d = e["data"]
        if e["kind"] == "build":
            s = self.structures.get(d["key"])
            if s:
                s["building"] = False
                self._say("obra", "exp.build.done", (x, y), title=cap(the(s["title"])))
        elif e["kind"] == "plant":
            if self.terrain[i] in (GRASS, HILL, SAND) and self._wild(i):
                self.terrain[i] = FOREST
            self._say("naturaleza", "exp.plant.done", (x, y), place=d["place"])
        elif e["kind"] == "explore" and f:
            kind, amount = self.rng.choice((("gold", 40), ("gold", 25), ("mana", 20), ("stone", 25), ("mana", 35)))
            self.resources[kind] += amount
            self._say("expedicion", "exp.explore.find", (x, y), realm=f["realm"], place=d["place"],
                      reward=f"{amount} {words('resources')[kind]}")
        elif e["kind"] == "siegecity" and f:
            d_f = next((o for o in self.factions.values() if o["id"] == d["defender"]), None)
            if not d_f or not d_f.get("alive") or list(d_f["capital"]) != [x, y]:
                return
            if self.rng.random() < 0.6:
                self._sack_capital(f, d_f, events)
            else:
                self._say("guerra", "exp.siegecity.held", (x, y), a=f["realm"], b=d_f["realm"])
        elif e["kind"] == "siege" and f:
            s = self.structures.get(d["key"])
            if not s or not s.get("alive"):
                return
            cost = self.cfg["fortresses"]["shield_cost"] * 3
            if self.resources["defense"] >= cost:
                self.resources["defense"] -= cost
                self._say("guerra", "exp.siege.held", (x, y), realm=f["realm"], cost=f"{cost:.0f}",
                          **self._fort_kw(s["title"]))
            else:
                s["breached_until"] = self.tick + 300
                self.resources["gold"] += 40
                self._say("guerra", "exp.siege.breach", (x, y), realm=f["realm"], gold=40,
                          **self._fort_kw(s["title"]))

    # Evolución del mapa: rumbos, migraciones, guerras de ambición, capitales ----

    def _heading(self, f: dict, now: float) -> float:
        """Rumbo de expansión del reino (radianes). Gira lento durante el día, así
        la forma del territorio cambia aunque el uso de RAM no cambie."""
        return f["id"] * 2.39 + (now / 3600) * 0.26

    def _grow_score(self, j: int, f: dict) -> float:
        """Menor = mejor casilla para crecer: cerca de la capital y hacia el rumbo."""
        x, y = self.xy(j)
        cx, cy = f["capital"]
        h = f.get("heading", 0.0)
        ahead = (x - cx) * math.cos(h) + (y - cy) * math.sin(h)
        return math.hypot(x - cx, y - cy) - 0.6 * ahead

    def _direction(self, h: float) -> tuple[str, str]:
        """Nombre del rumbo como se ve en pantalla (arriba = norte) y su opuesto."""
        dx, dy = math.cos(h), math.sin(h)
        sx, sy = dx - dy, dx + dy                           # proyección isométrica
        k = round((math.atan2(-sy, sx) % (2 * math.pi)) / (math.pi / 4)) % 8
        dirs = words("directions")                           # norte, noreste, este...
        order = [2, 1, 0, 7, 6, 5, 4, 3]                      # ángulo 0 = este, sentido antihorario
        return dirs[order[k]], dirs[order[(k + 4) % 8]]

    def _drift(self):
        """En paz y con el territorio completo, cada reino abandona un poco de su
        retaguardia; el crecimiento normal la recupera hacia adelante: migra."""
        if self.tick % 45 or self.weather["fog"]:
            return
        for f in self.factions.values():
            if not f.get("alive") or f["tiles"] < 12 or f["tiles"] < f["target"]:
                continue
            fid = f["id"]
            cap = self.idx(*f["capital"])
            tiles = [i for i, o in enumerate(self.owner) if o == fid and i != cap
                     and self.bld[i] not in (B.PLAZA, B.PALACE, B.MONUMENT)]
            tiles.sort(key=lambda i: self._grow_score(i, f), reverse=True)   # lo más lejano y de atrás
            for i in tiles[:max(1, f["tiles"] // 40)]:
                self.owner[i], self.level[i], self.bld[i] = 0, 0, B.NONE
                self.ruin[i] = 15
            ahead, back = self._direction(f.get("heading", 0.0))
            self._say("migracion", "drift", f["capital"], f"drift:{fid}", 2700, realm=f["realm"], dir=ahead, back=back,
                      Ruler=cap_str(self.lore, f))

    def _ambition(self, events: list[dict]):
        """Guerras que no dependen de la RAM: vecinos que se pelean cada tanto.
        Mientras dura, el atacante gana tierras de frontera (arrasadas) y, si llega,
        asedia la capital enemiga."""
        rng = self.rng
        realms = [f for f in self.factions.values() if f.get("alive") and f["kind"] == "browser" and f["tiles"] >= 15]
        busy = {w["a"] for w in self.wars.values()} | {w["b"] for w in self.wars.values()}
        if len(realms) >= 2 and rng.random() < 1 / 4000:          # ~10 guerras por día
            pairs = [(a, b) for a in realms for b in realms if a["id"] < b["id"]
                     and a["id"] not in busy and b["id"] not in busy
                     and self.war_cooldown.get(f"{a['id']}:{b['id']}", 0) <= self.tick]
            if pairs:
                a, b = min(pairs, key=lambda ab: self._dist(self.idx(*ab[0]["capital"]), ab[1]["capital"])
                           + rng.random() * 8)
                if rng.random() < 0.5:
                    a, b = b, a
                self._declare_war(a, b)
        by_id = {f["id"]: f for f in self.factions.values()}
        for key, war in self.wars.items():
            if not war.get("until") or self.tick >= war["until"] or self.tick % 6:
                continue
            a, b = by_id.get(war["a"]), by_id.get(war["b"])
            if not (a and b and a.get("alive") and b.get("alive")):
                continue
            # pelea el más fuerte casi siempre, a veces contraataca el otro
            att, dfn = (a, b) if rng.random() < (0.7 if a["rss_mb"] >= b["rss_mb"] else 0.4) else (b, a)
            border = [i for i, o in enumerate(self.owner) if o == dfn["id"]
                      and any(self.owner[j] == att["id"] for j in self.neighbors(i))
                      and (list(self.xy(i)) != list(dfn["capital"]) or dfn["tiles"] < 6)]
            # lo conquistable en una guerra tiene tope: mueve fronteras, no borra reinos
            room = max(10, int(0.4 * dfn.get("target", 0))) - war["score"].get(str(att["id"]), 0)
            if border and room <= 0:
                pass                                          # ya ganó lo que podía ganar en esta guerra
            elif border:
                lost = rng.sample(border, min(len(border), rng.randint(1, 2), room))
                for i in lost:
                    self.owner[i] = att["id"]
                    self.born[i] = self.tick
                    self.burn[i] = 8                          # arrasada: quemada un rato
                    events.append({"type": "conquest", "x": i % self.n, "y": i // self.n, "faction": att["id"]})
                att["bonus"] = att.get("bonus", 0) + len(lost)
                dfn["bonus"] = max(0, dfn.get("bonus", 0) - len(lost))
                self._battle(att, dfn, lost)
            elif att["id"] == war["a"]:
                # sin frontera común: el atacante avanza hacia el enemigo (colonos armados)
                fa_tiles = [i for i, o in enumerate(self.owner) if o == att["id"]]
                gained = self._grow(att, fa_tiles, 2, by_id, events)
                for i, _ in gained:
                    self.born[i] = self.tick
                att["bonus"] = att.get("bonus", 0) + len(gained)
            # un asedio a la capital por guerra, si a mitad de la guerra no hubo contacto
            half = war["start"] + (war["until"] - war["start"]) // 2
            if not war.get("sieged") and att["id"] == war["a"] and (not border or self.tick >= half) \
                    and self.tick >= half:
                war["sieged"] = True
                self._launch("siegecity", att, self.idx(*dfn["capital"]), work=15, n=12,
                             war=key, defender=dfn["id"])
                self._say("guerra", "exp.siegecity.start", dfn["capital"], a=att["realm"], b=dfn["realm"])

    def _declare_war(self, a: dict, b: dict):
        key = ":".join(map(str, sorted((a["id"], b["id"]))))
        reason = self.rng.choice(words("war_reasons")).format(place=self.lore.place(f"motivo:{self.tick}"))
        war = self.wars[key] = {"name": self.lore.war_name(a, b, f"{key}:{self.tick}"), "a": a["id"], "b": b["id"],
                                "start": self.tick, "last": self.tick, "score": {}, "front": [],
                                "until": self.tick + self.rng.randint(300, 900)}   # 10 a 30 minutos
        self._say("guerra", "war.declare", b["capital"], a=a["realm"], b=b["realm"], reason=reason, war=war["name"],
                  War=cap(war["name"]), Ruler=cap_str(self.lore, a))
        target = min((i for i, o in enumerate(self.owner) if o == b["id"]),
                     key=lambda i: self._dist(i, a["capital"]), default=None)
        if target is not None:
            self._launch("army", a, target, work=40, n=10)

    def _sack_capital(self, att: dict, dfn: dict, events: list[dict]):
        """Saqueo: arde el centro, el tesoro del atacante crece y la corte huye
        a la tierra propia más lejana del enemigo: la ciudad se rearma allá."""
        cap = self.idx(*dfn["capital"])
        for j in [cap, *self.neighbors(cap)]:
            self.burn[j] = 20
        self.resources["gold"] += 60
        events.append({"type": "lightning", "x": cap % self.n, "y": cap // self.n})
        self._say("guerra", "capital.sacked", dfn["capital"], a=att["realm"], b=dfn["realm"], gold=60)
        own = [i for i, o in enumerate(self.owner) if o == dfn["id"] and not self.burn[i]]
        if own:
            new = max(own, key=lambda i: self._dist(i, att["capital"]) - 0.3 * self._dist(i, dfn["capital"]))
            dfn["capital"] = list(self.xy(new))
            dfn["specials"] = {}
            dfn["moved"] = dfn.get("moved", 0) + 1
            self._say("dinastia", "capital.moved", dfn["capital"], realm=dfn["realm"], Ruler=cap_str(self.lore, dfn))

    def _adventures(self):
        """Exploradores y asedios: más vida en el mapa aunque el sistema esté tranquilo."""
        rng = self.rng
        alive = [f for f in self.factions.values() if f.get("alive") and f.get("tiles", 0) >= 10]
        if not alive:
            return
        kinds = [e["kind"] for e in self.expeditions]
        if kinds.count("explore") < 2 and rng.random() < 1 / 900:        # unas 50 expediciones por día
            f = rng.choice(alive)
            wild = [i for i in rng.sample(range(len(self.terrain)), 400)
                    if self.terrain[i] in LAND and self._wild(i) and self._dist(i, f["capital"]) > 10]
            if wild:
                i = wild[0]
                place = self.lore.place(f"lugar:{i}")
                self._launch("explore", f, i, work=8, n=3, place=place)
                self._say("expedicion", "exp.explore.start", self.xy(i), realm=f["realm"], place=place)
        forts = [s for s in self.structures.values() if s.get("alive") and s["kind"] not in NO_SHIELD
                 and s.get("breached_until", 0) <= self.tick]
        big = [f for f in alive if f["kind"] == "browser" and f["tiles"] >= 40]
        if forts and big and "siege" not in kinds and rng.random() < 1 / 6000:   # unos 7 asedios por día
            f = rng.choice(big)
            s = min(forts, key=lambda s: self._dist(self.idx(*f["capital"]), (s["x"], s["y"])))
            self._launch("siege", f, self.idx(s["x"], s["y"]), work=20, n=12, key=s["key"])
            self._say("guerra", "exp.siege.start", (s["x"], s["y"]), realm=f["realm"],
                      Ruler=cap(self.lore.ruler_str(f["ruler"])), **self._fort_kw(s["title"]))

    # Economía y actividad: red, disco, tipeo, presencia, procesos -----------

    def _economy_events(self, snap: dict, dt: float):
        ev = self.cfg["events"]
        inp = snap.get("input") or {}
        rx, tx = snap.get("net_rx", 0.0), snap.get("net_tx", 0.0)
        disk = snap.get("disk_r", 0.0) + snap.get("disk_w", 0.0)
        mine = self.structures["mine:main"]
        mine["activity"] = round(min(1.0, disk / ev["mine_mbps"]), 2)
        self.counters["net_rx"] += rx * dt
        self.counters["births"] += snap.get("births", 0)
        say = self.chronicle.tr
        port = tr("port", place=self.lore.place("puerto"))
        the_mine = cap(the(self.lore.structure_name("mine", "profundas")))

        self._episode("fleet", rx >= ev["fleet_mbps"], rx, dt,
                      lambda e: say("fleet.start", port=port, rate=f"{rx:.0f}"),
                      lambda e: say("fleet.end", port=port, total=fmt_mb(e["total"])), hold=2)
        self._episode("caravan", tx >= ev["caravan_mbps"], tx, dt, lambda e: say("caravan.start"),
                      lambda e: say("caravan.end", total=fmt_mb(e["total"])), hold=2)
        self._episode("mine", disk >= ev["mine_mbps"], disk, dt,
                      lambda e: say("mine.start", The_mine=the_mine, rate=f"{disk:.0f}"),
                      lambda e: say("mine.end", The_mine=the_mine, total=fmt_mb(e["total"])),
                      hold=2, where=(mine["x"], mine["y"]))

        if snap.get("births", 0) >= ev["births_boom"]:
            self._say("pueblo", "boom", None, "boom", 60, n=snap["births"])

        if inp.get("source") == "evdev":
            before = self.counters["keys"]
            self.counters["keys"] += inp.get("keys", 0)
            for m in KEY_MILESTONES:
                if before < m <= self.counters["keys"]:
                    self._say("escribas", "keys.milestone", n=f"{m:,}")
            self._episode("typing", inp.get("kpm", 0) >= ev["typing_burst_kpm"], inp.get("kpm", 0), dt,
                          lambda e: say("typing.start", kpm=inp.get("kpm", 0)),
                          lambda e: say("typing.end", mins=f"{e['dur'] / 60:.0f}"), hold=2)

        if inp.get("source") in ("idle", "evdev"):
            afk = inp.get("idle_for", 0) >= ev["afk_minutes"] * 60
            was = self.activity.get("afk_since")
            if afk and not was:
                self.activity["afk_since"] = snap["t"] - inp["idle_for"]
                self._say("soberano", "afk.leave")
            elif not afk and was and inp.get("idle_for", 0) == 0:
                self._say("soberano", "afk.back", mins=f"{(snap['t'] - was) / 60:.0f}")
                self.activity.pop("afk_since", None)

        self.activity.update(
            active=inp.get("active", 1.0), kpm=inp.get("kpm", 0), source=inp.get("source", "none"),
            kbd_denied=inp.get("kbd_denied", False), net_rx=rx, net_tx=tx, disk=round(disk, 1),
            births=snap.get("births", 0), deaths=snap.get("deaths", 0), procs=snap["procs"],
            cpu_temp=snap.get("cpu_temp"))

    def _flavor(self, snap: dict):
        """Sucesos de color, raros, armados con lo que hay en el mundo."""
        if self.rng.random() > 1 / self.cfg["events"]["flavor_every"]:
            return
        alive = [f for f in self.factions.values() if f.get("alive") and f.get("tiles")]
        forts = [s for s in self.structures.values() if s.get("alive") and s["kind"] not in NO_SHIELD]
        options: list[tuple[str, dict]] = []
        if alive:
            big = max(alive, key=lambda f: f["tiles"])
            options += [
                ("flavor.feast", {"realm": big["realm"], "ruler": self.lore.ruler_str(big["ruler"]),
                                  "hours": f"{(snap['t'] - big['ruler']['since']) / 3600:.0f}"}),
                ("flavor.harvest", {"realm": big["realm"], "gold": f"{self.resources['gold']:.0f}"}),
                ("flavor.bard", {"realm": big["realm"], "procs": big["procs"]}),
            ]
            if big.get("cult"):
                options += [("flavor.pilgrims", {"cult": self.lore.cult_name(big["cult"]), "realm": big["realm"]}),
                            ("flavor.heresy", {"cult": self.lore.cult_name(big["cult"]), "deity": big["cult"]["name"]})]
        if len(alive) >= 2:
            a, b = self.rng.sample(alive, 2)
            options += [("flavor.treaty", {"a": a["realm"], "b": b["realm"]}),
                        ("flavor.rivals", {"a": a["realm"], "b": b["realm"]})]
        if forts:
            old = max(forts, key=lambda s: s.get("uptime", 0))
            options.append(("flavor.ballad", {"the_fort": the(old["title"]), "hours": f"{old.get('uptime', 0) / 3600:.0f}"}))
        options += [("flavor.dragon", {"volcano": self._volcano()}),
                    ("flavor.census", {"n": snap["procs"], "kingdom": self.kingdom}),
                    ("flavor.comet", {}),
                    ("flavor.forest", {"place": self.lore.place(f"bosque:{self.rng.randrange(20)}")})]
        if snap["cpu"] < 10:
            options.append(("flavor.calm", {"cpu": f"{snap['cpu']:.0f}"}))
        key, kw = self.rng.choice(options)
        self._say("cronica", key, **kw)

    def _update_resources(self, snap: dict, dt: float):
        minutes = dt / 60
        r = self.resources
        inp = snap.get("input") or {}
        r["gold"] += minutes * 0.05 * sum(1 for o in self.owner if o) + snap.get("net_rx", 0) * dt * 0.002
        r["stone"] = r.get("stone", 0.0) + minutes * 0.2 + (snap.get("disk_r", 0) + snap.get("disk_w", 0)) * dt * 0.001
        r["mana"] += inp.get("keys", 0) * 0.01 + minutes * inp.get("active", 0) * 0.5
        if self.weather["plague"]:
            r["gold"] = max(0.0, r["gold"] - minutes * 2)
        for s in self.structures.values():
            if not s["alive"]:
                continue
            if s["kind"] == "tower":
                r["mana"] += minutes * s.get("floors", 1)
            elif s["kind"] not in NO_SHIELD:
                r["defense"] += minutes * (1 + s.get("walls", 0))

    # Urbanismo ---------------------------------------------------------------

    def _zone(self, f: dict, tiles: list[int], cap_level: int):
        """Plan urbano de una facción. Centro: palacio, plaza y calles en grilla
        con pozos en los cruces; edificios públicos según el tamaño; casas;
        afueras: granjas, pasturas, graneros, muelles en la costa, aserraderos
        junto al bosque; atalayas en la frontera con otros reinos."""
        if not tiles:
            return
        cx, cy = f["capital"]
        cap_i = self.idx(cx, cy)
        n_t = len(tiles)
        radius = max(1.5, math.sqrt(n_t / math.pi))
        core = max(1.6, radius * 0.6)
        town = f["kind"] == "browser"
        procs = f.get("procs", 0)
        k = self.cfg["browsers_rules"]["density_k"]
        houses = []
        for i in tiles:
            x, y = self.xy(i)
            d = math.hypot(x - cx, y - cy)
            h = self._tile_rand[i]
            nb = list(self.neighbors(i))
            if i == cap_i:
                b = B.PALACE
            elif town and d <= 1.01 and n_t >= 12:
                b = B.PLAZA
            elif town and d <= core and ((x - cx) % 3 == 0 or (y - cy) % 3 == 0):
                b = B.WELL if (x - cx) % 6 == 0 and (y - cy) % 6 == 0 else B.STREET
            elif d <= core or (not town and d <= radius * 0.75):
                b = B.HOUSE
            elif d <= radius * 0.9:
                b = B.GARDEN if h < 0.12 else B.HOUSE
            elif any(self.terrain[j] == WATER for j in nb) and h < 0.4:
                b = B.DOCK
            elif any(self.terrain[j] == FOREST and not self.owner[j] for j in nb) and h < 0.4:
                b = B.LUMBER
            else:
                b = B.BARN if h < 0.08 else B.PASTURE if h < 0.3 else B.FARM
            if b not in (B.PALACE, B.PLAZA) and h > 0.8 and \
                    any(self.owner[j] and self.owner[j] != f["id"] for j in nb):
                b = B.WATCHTOWER
            self.bld[i] = b
            if b == B.HOUSE:
                houses.append((d, h, i))
            # altura: casas bajas en general, torres solo en el corazón de ciudades muy pobladas
            lv = 0
            if b == B.HOUSE:
                lv = min(cap_level, 1 + int(k * procs / (4 + 2 * d)))
                if town and d <= core * 0.5 and h < 0.35 and procs >= 20 and cap_level >= 3:
                    lv = 4
            self.level[i] = 0 if self.burn[i] or self.lava[i] else lv

        # Edificios únicos: se quedan donde se construyeron mientras la casilla siga siendo del reino
        specials = f.setdefault("specials", {})
        ever = f.setdefault("ever", [])
        owned = set(tiles)
        for name, i in list(specials.items()):
            if i not in owned or self.bld[i] in (B.PLAZA, B.STREET, B.PALACE, B.WELL) and name != "monument":
                del specials[name]
        free = sorted(houses)                                   # de la plaza hacia afuera
        used = set(specials.values())
        wanted = [(str(b), b, need) for b, need in B.SPECIALS] if town else [("well", B.WELL, 6)]
        if town and n_t >= 25:
            wanted.append(("graveyard", B.GRAVEYARD, 25))
        wanted += [(f"workshop{j}", B.WORKSHOP, 15 + 30 * j) for j in range(n_t // 30)]
        for name, b, need in wanted:
            if n_t < need or name in specials:
                continue
            pool = [c for c in free if c[2] not in used]
            if b in (B.BARRACKS, B.GRAVEYARD) or name.startswith("workshop"):
                pool = [c for c in pool if c[0] > core * 0.7] or pool   # en el anillo exterior
            if b == B.GRAVEYARD:
                pool = sorted(pool, reverse=True)
            if not pool:
                continue
            i = pool[0][2]
            specials[name] = i
            used.add(i)
            if b in dict(B.SPECIALS) and b not in ever:
                ever.append(b)
                self._say("obra", "build.first", self.xy(i), bname=words("buildings")[b],
                          a_bname=words("buildings_a").get(b, words("buildings")[b]),
                          first_bname=words("buildings_first").get(b, words("buildings")[b]), **self._realm_kw(f))
                if b == B.TEMPLE and not f.get("cult"):
                    f["cult"] = c = self.lore.deity(f["name"])
                    self._say("fe", "faith.found", self.xy(i), realm=f["realm"], deity=c["name"],
                              epithet=self.lore.deity_epithet(c), cult=self.lore.cult_name(c))
        names = {str(b): b for b, _ in B.SPECIALS}
        for name, i in specials.items():
            self.bld[i] = (B.MONUMENT if name == "monument" else names.get(name) or
                           {"well": B.WELL, "graveyard": B.GRAVEYARD}.get(name, B.WORKSHOP))
            self.level[i] = 1

    # Guerras -----------------------------------------------------------------

    def _battle(self, a: dict, d: dict, lost: list[int]):
        key = ":".join(map(str, sorted((a["id"], d["id"]))))
        self.counters["conquests"] += len(lost)
        war = self.wars.get(key)
        if war is None:
            war = self.wars[key] = {"name": self.lore.war_name(a, d, f"{key}:{self.tick}"),
                                    "a": a["id"], "b": d["id"], "start": self.tick, "score": {}, "front": []}
            ruler = self.lore.ruler_str(a["ruler"])
            self._say("guerra", "war.start", self.xy(lost[0]), war=war["name"], War=cap(war["name"]),
                      a=a["realm"], b=d["realm"], Ruler=cap(ruler))
            self._launch("army", a, lost[0], work=40, n=10)
            self._say("guerra", "exp.army.march", self.xy(lost[0]), realm=a["realm"],
                      place=self.lore.place(f"batalla:{lost[0]}"))
        war["last"] = self.tick
        war["score"][str(a["id"])] = war["score"].get(str(a["id"]), 0) + len(lost)
        war["front"] = (war["front"] + [list(self.xy(i)) for i in lost])[-12:]
        self._say("guerra", "battle", self.xy(lost[0]), f"battle:{key}", 150,
                  place=self.lore.place(f"batalla:{lost[0]}"), war=war["name"], a=a["realm"], b=d["realm"],
                  n=len(lost))

    # Sociedad: héroes, paz, fe, monumentos, días ------------------------------

    def _spawn_hero(self, f: dict) -> dict:
        self.counters["heroes"] = self.counters.get("heroes", 0) + 1
        h = self.lore.hero(f"{f['name']}:{self.counters['heroes']}")
        h.update(faction=f["name"], born=self.tick, alive=True, deeds=0, life=self.rng.randint(7200, 21600))
        self.notables.append(h)
        return h

    def _society(self, snap: dict):
        rng = self.rng
        by_id = {f["id"]: f for f in self.factions.values()}
        alive = [f for f in self.factions.values() if f.get("alive") and f.get("tiles", 0) >= 15]
        living = [h for h in self.notables if h["alive"]]

        # Paz: una guerra sin batallas por ~5 min termina en tratado
        for key, war in list(self.wars.items()):
            a, b = by_id.get(war["a"]), by_id.get(war["b"])
            ongoing = self.tick < war["until"] if war.get("until") else \
                self.tick - war.get("last", war["start"]) < 150
            if ongoing and a and b and a.get("alive") and b.get("alive"):
                continue
            self.war_cooldown[key] = self.tick + 3600
            del self.wars[key]
            if not (a and b):
                continue
            winner = max((a, b), key=lambda f: war["score"].get(str(f["id"]), 0))
            self._say("paz", "peace", place=self.lore.place("paz:" + key), war=war["name"],
                      mins=f"{(self.tick - war['start']) * self.cfg['daemon']['interval'] / 60:.0f}",
                      winner=winner["realm"], n=war["score"].get(str(winner["id"]), 0), a=a["realm"], b=b["realm"])

        # Héroes: nacen (más en reinos de fiesta), hacen hazañas y mueren
        festive = [f for f in alive if f.get("festival", 0) > self.tick]
        if alive and len(living) < 2 + len(alive) and rng.random() < (1 / 60 if festive else 1 / 600):
            f = rng.choice(festive) if festive else rng.choices(alive, weights=[f["tiles"] for f in alive])[0]
            h = self._spawn_hero(f)
            self._say("heroe", "hero.born", f["capital"], realm=f["realm"], hero=self.lore.hero_str(h),
                      job=self.lore.hero_job(h))
        if living and rng.random() < 1 / 900:
            h = rng.choice(living)
            f = self.factions.get(h["faction"])
            if f:
                others = [o for o in self.factions.values() if o.get("alive") and o is not f] or [f]
                text = rng.choice(words("deeds")[self.lore.hero_key(h)]).format(
                    h=self.lore.hero_str(h), r=f["realm"], e=rng.choice(others)["realm"],
                    p=self.lore.place(f"lugar:{rng.randrange(40)}"))
                h["deeds"] += 1
                self.chronicle.add(self.tick, "heroe", _gender(text, h["fem"]), f["capital"], tkey="hero.deed")
        at_war = {w["a"] for w in self.wars.values()} | {w["b"] for w in self.wars.values()}
        for h in living:
            f = self.factions.get(h["faction"])
            hero = self.lore.hero_str(h)
            if not f or not f.get("alive"):
                h["alive"] = False
                self._say("heroe", "hero.exile", hero=hero)
            elif f["id"] in at_war and self.lore.hero_key(h) == "brave" and rng.random() < 1 / 150:
                h["alive"] = False
                war = next(w for w in self.wars.values() if f["id"] in (w["a"], w["b"]))
                self._say("heroe", "hero.fell", hero=hero, war=war["name"])
            elif self.tick - h["born"] > h["life"]:
                h["alive"] = False
                self._say("heroe", "hero.died", hero=hero, job=self.lore.hero_job(h), realm=f["realm"],
                          deeds=f"{h['deeds']} {words('deed_words')[0 if h['deeds'] == 1 else 1]}")
        self.notables = [h for h in self.notables if h["alive"]] + \
            [h for h in self.notables if not h["alive"]][-30:]

        # Monumentos: cuando el tesoro cruza ciertos umbrales, el reino más grande levanta uno
        thresholds = (150, 600, 2500, 10000, 40000)
        done = self.counters.get("monuments", 0)
        if done < len(thresholds) and self.resources["gold"] >= thresholds[done] and alive:
            f = max(alive, key=lambda f: f["tiles"])
            plaza = [i for i in range(len(self.bld)) if self.bld[i] == B.PLAZA and self.owner[i] == f["id"]]
            if plaza:
                f.setdefault("specials", {})["monument"] = plaza[0]
                self.bld[plaza[0]] = B.MONUMENT
                self.counters["monuments"] = done + 1
                self._say("obra", "monument", self.xy(plaza[0]), realm=f["realm"],
                          name=self.lore.monument(self.lore.ruler_str(f["ruler"]), rng),
                          gold=f"{self.resources['gold']:.0f}")

        # Resumen del día (al cambiar la fecha local)
        today = time.strftime("%Y-%m-%d")
        prev = self.counters.get("day")
        if prev != today:
            if prev:
                base = self.counters.get("day_base", {})
                diff = {k: self.counters.get(k, 0) - base.get(k, 0) for k in ("lightning", "conquests", "births")}
                self._say("cronica", "day.summary", day=prev, **diff)
            self.counters["day"] = today
            self.counters["day_base"] = {k: self.counters.get(k, 0) for k in ("lightning", "conquests", "births")}

    # Visitantes: sesiones de login (ssh, tty) -> emisarios y embajadas -------

    def _embassies(self, snap: dict) -> set[str]:
        seen = set()
        now = snap["t"]
        current = {}
        for s in snap.get("sessions") or []:
            current.setdefault(s["host"] or f"local:{s['tty']}", s)
        for host, s in current.items():
            v = self.visitors.get(host)
            if v is None:
                remote = not host.startswith("local:")
                land = self.lore.foreign_land(host) if remote else None
                v = self.visitors[host] = {"land": land, "user": s["user"], "since": now, "remote": remote}
                count = (snap.get("visits") or {}).get(host if remote else "local", 1)
                if remote:
                    self._say("visita", "visit.arrive", land=land, host=host, count=count, user=s["user"])
                else:
                    self._say("visita", "visit.local", user=s["user"], tty=s["tty"])
            if v["remote"]:
                key = f"embassy:{host}"
                seen.add(key)
                st = self.structures.get(key)
                if st is None:
                    x, y = self._place(key, prefer_hill=False, coastal=True)
                    st = self.structures[key] = {"key": key, "kind": "embassy", "name": host, "x": x, "y": y,
                                                 "title": tr("embassy", land=v["land"]), "walls": 0,
                                                 "color": [rng_for(self.seed, host).randrange(60, 230) for _ in range(3)]}
                st.update(alive=True, dead_ticks=0, uptime=now - v["since"], user=v["user"])
        for host in [h for h in self.visitors if h not in current]:
            v = self.visitors.pop(host)
            mins = f"{(now - v['since']) / 60:.0f}"
            if v["remote"]:
                self._say("visita", "visit.leave", land=v["land"], user=v["user"], mins=mins)
            else:
                self._say("visita", "visit.local_leave", user=v["user"], mins=mins)
        return seen


_OLD_SPECIALS = {"mercado": str(B.MARKET), "taberna": str(B.TAVERN), "templo": str(B.TEMPLE),
                 "cuartel": str(B.BARRACKS), "biblioteca": str(B.LIBRARY), "pozo": "well", "cementerio": "graveyard"}


def _migrate_special(key: str) -> str:
    if key.startswith("taller"):
        return "workshop" + key[len("taller"):]
    return _OLD_SPECIALS.get(key, key)


def cap_str(lore: Lore, f: dict) -> str:
    return cap(lore.ruler_str(f["ruler"]))


def _gender(text: str, fem: bool) -> str:
    """'sola/o' -> 'sola' o 'solo' según el héroe (solo aparece en castellano)."""
    return re.sub(r"(\w*)a/o\b", lambda m: m.group(1) + ("a" if fem else "o"), text)
