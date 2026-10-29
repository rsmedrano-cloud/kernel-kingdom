"""python -m unittest discover -s tests -v"""
import copy
import json
import unittest

from reino import buildings as B
from reino import config, i18n
from reino.daemon.probes import SystemProbe
from reino.daemon.world import FOREST, GRASS, LAND, WATER, World
from reino.render.occlusion import _coverage


def snap(**over) -> dict:
    s = {
        "t": 1_000_000.0, "dt": 2.0, "cpu": 5.0, "ram": 30.0, "ram_used_mb": 8000, "ram_total_mb": 27000,
        "swap": 0.0, "load1": 1.0, "cores": 16, "procs": 400, "boot": 1000,
        "browsers": {}, "fortresses": {}, "editors": {}, "compilers": {}, "apps": {}, "top": [],
        "net_rx": 0.0, "net_tx": 0.0, "disk_r": 0.0, "disk_w": 0.0, "births": 0, "deaths": 0,
        "cpu_temp": 50.0,
        "input": {"source": "idle", "keys": 0, "kpm": 0, "active": 1.0, "idle_for": 0},
    }
    s.update(over)
    return s


def proc(rss_mb: float, procs: int = 20, since: float = 500.0) -> dict:
    return {"procs": procs, "rss_mb": rss_mb, "cpu": 1.0, "uptime": 100.0, "since": since}


def fortress(kind: str, name: str, uptime: float) -> dict:
    return {"procs": 1, "rss_mb": 50.0, "cpu": 0.0, "uptime": uptime, "kind": kind, "name": name, "since": 0}


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.cfg["world"]["seed"] = 1337      # isla fija para que los tests sean reproducibles
        self.world = World(self.cfg)
        self.world.rng.seed(0)                # y azar fijo (exploradores, rayos...)

    def run_ticks(self, n: int, s: dict) -> list[dict]:
        events = []
        for _ in range(n):
            events += self.world.step(copy.deepcopy(s))
        return events

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.world.chronicle.entries]

    def texts(self) -> str:
        return "\n".join(e["text"] for e in self.world.chronicle.entries)

    def test_terrain_is_deterministic_wide_island(self):
        other = World(self.cfg)
        self.assertEqual(self.world.terrain, other.terrain)
        n = self.world.n
        border = [self.world.terrain[i] for i in range(n)] + [self.world.terrain[(n - 1) * n + i] for i in range(n)]
        self.assertGreater(border.count(WATER) / len(border), 0.9)
        # la isla es más ancha que alta en pantalla (x - y = horizontal, x + y = vertical)
        land = [divmod(i, n) for i, t in enumerate(self.world.terrain) if t != WATER]
        width = max(x - y for y, x in land) - min(x - y for y, x in land)
        height = max(x + y for y, x in land) - min(x + y for y, x in land)
        self.assertGreater(width * 16 / (height * 8), 1.4)

    def test_browser_grows_then_decays_with_lore(self):
        self.run_ticks(30, snap(browsers={"chrome": proc(2000)}))
        f = self.world.factions["chrome"]
        self.assertEqual(f["tiles"], 50)
        self.assertTrue(all(self.world.terrain[i] in LAND for i, o in enumerate(self.world.owner) if o))
        self.assertIn("fundacion", self.kinds())
        self.assertIn("Cromaria", f["display"])
        self.run_ticks(5, snap())  # se cerró el navegador
        self.assertLess(self.world.factions["chrome"]["tiles"], 50)
        self.assertGreater(sum(1 for r in self.world.ruin if r), 0)
        self.assertIn("caida", self.kinds())

    def test_reopened_app_is_reborn_elsewhere(self):
        s = snap(browsers={"chrome": proc(2000), "zen": proc(1500)})
        self.run_ticks(30, s)
        f = self.world.factions["chrome"]
        old_cap = list(f["capital"])
        self.run_ticks(3, snap(browsers={"zen": proc(1500)}))       # se cerró chrome: el reino cae
        self.assertFalse(f["alive"])
        self.run_ticks(1, s)                                          # y volvió
        self.assertTrue(f["alive"])
        self.assertGreaterEqual(self.world._dist(self.world.idx(*f["capital"]), old_cap), 14)
        mine = [i for i, o in enumerate(self.world.owner) if o == f["id"]]
        self.assertTrue(all(self.world._dist(i, old_cap) >= 10 for i in mine))   # nada junto a las ruinas
        self.assertGreater(self.world.ruin[self.world.idx(*old_cap)], 0)
        self.assertIn("dyn.reborn", [e.get("k") for e in self.world.chronicle.entries])
        self.run_ticks(30, s)
        self.assertGreater(f["tiles"], 0)                             # la ciudad nueva crece allá

    def test_restart_is_a_new_dynasty(self):
        self.run_ticks(3, snap(browsers={"zen": proc(1000, since=100)}))
        self.run_ticks(1, snap(browsers={"zen": proc(1000, since=900)}))   # el proceso raíz cambió
        self.assertEqual(self.world.factions["zen"]["dynasty"], 2)
        self.assertEqual(sum(e["kind"] == "dinastia" for e in self.world.chronicle.entries), 1)

    def test_heavy_app_founds_hamlet_with_landmark(self):
        self.run_ticks(5, snap(apps={"claude": proc(400, procs=1)}))
        f = self.world.factions["claude"]
        self.assertEqual(f["kind"], "app")
        self.assertEqual(f["landmark"], "oraculo")
        self.assertLessEqual(f["target"], self.cfg["apps"]["max_tiles"])
        self.assertIn("Claudel", self.texts())

    def test_small_app_does_not_found(self):
        self.run_ticks(3, snap(apps={"tiny": proc(100, procs=1)}))
        self.assertNotIn("tiny", self.world.factions)

    def test_density_follows_process_count(self):
        self.run_ticks(30, snap(browsers={"chrome": proc(2000, procs=4)}))
        low = max(self.world.level)
        self.run_ticks(2, snap(browsers={"chrome": proc(2000, procs=60)}))
        self.assertGreater(max(self.world.level), low)

    def test_friction_bigger_faction_steals_tiles(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["browsers_rules"].update(max_land_fraction=1.0, expand_per_tick=80)
        self.world = World(cfg)
        self.run_ticks(20, snap(browsers={"b": proc(8000)}))
        b_full = self.world.factions["b"]["tiles"]
        # "a" quiere toda la isla; crece por su perímetro y al quedarse sin tierra libre invade a "b"
        events = self.run_ticks(150, snap(browsers={"a": proc(10 ** 6), "b": proc(8000)}))
        self.assertTrue(any(e["type"] == "conquest" for e in events))
        self.assertLess(self.world.factions["b"]["tiles"], b_full)

    def test_fortress_is_stable_builds_walls_and_roads(self):
        s = snap(browsers={"chrome": proc(1000)}, fortresses={"container:db": fortress("container", "db", 30)})
        self.run_ticks(1, s)
        self.assertNotIn("container:db", self.world.structures)  # aún no cumple min_uptime
        s["fortresses"]["container:db"]["uptime"] = 4000
        self.run_ticks(1, s)
        st = self.world.structures["container:db"]
        pos = (st["x"], st["y"])
        self.assertEqual(st["walls"], 2)
        self.assertGreater(sum(self.world.wall), 0)
        self.assertGreater(sum(self.world.road), 0)                # camino capital <-> castillo
        self.assertFalse(any(w and r for w, r in zip(self.world.wall, self.world.road)))  # puertas
        self.run_ticks(3, snap())  # el contenedor se detuvo -> ruina, mismo lugar
        self.assertFalse(st["alive"])
        self.run_ticks(1, s)
        self.assertEqual((st["x"], st["y"]), pos)

    def test_cpu_spike_storm_and_eruption_are_narrated(self):
        events = self.run_ticks(20, snap(cpu=99.0, browsers={"chrome": proc(2000)}))
        self.assertGreater(self.world.weather["storm"], 0.5)
        self.assertTrue(self.world.weather["eruption"])
        self.assertTrue(any(e["type"] == "lightning" for e in events))
        self.assertGreater(sum(1 for v in self.world.lava if v), 0)
        self.run_ticks(20, snap(cpu=3.0))
        self.assertEqual(self.world.weather["storm"], 0.0)
        self.assertFalse(self.world.weather["eruption"])
        self.assertEqual(self.kinds().count("storm"), 2)

    def test_fortress_shield_blocks_lightning(self):
        s = snap(cpu=99.0, fortresses={"daemon:sshd": fortress("daemon", "sshd", 5000)})
        self.world.resources["defense"] = 1000
        self.world.cfg["fortresses"]["shield_radius"] = 99  # escudo cubre todo el mapa
        events = self.run_ticks(15, s)
        kinds = {e["type"] for e in events}
        self.assertIn("shield", kinds)
        self.assertNotIn("lightning", kinds)

    def test_fog_freezes_expansion(self):
        self.run_ticks(5, snap(ram=95.0, browsers={"chrome": proc(2000)}))
        self.assertTrue(self.world.weather["fog"])
        self.assertEqual(self.world.factions["chrome"]["tiles"], 0)

    def test_download_brings_a_fleet(self):
        self.run_ticks(4, snap(net_rx=20.0))
        self.assertIn("fleet", self.kinds())
        self.run_ticks(3, snap(net_rx=0.0))
        self.assertEqual(self.kinds().count("fleet"), 2)

    def test_afk_and_return(self):
        self.run_ticks(1, snap(input={"source": "idle", "active": 0.0, "idle_for": 900}))
        self.assertIn("soberano", self.kinds())
        self.run_ticks(1, snap(t=1_001_000.0, input={"source": "idle", "active": 1.0, "idle_for": 0}))
        self.assertEqual(self.kinds().count("soberano"), 2)

    def test_reboot_starts_new_era(self):
        self.run_ticks(1, snap(boot=1000))
        self.run_ticks(1, snap(boot=2000))
        self.assertEqual(self.world.era["n"], 2)
        self.assertEqual(self.kinds().count("era"), 2)

    def test_city_has_urban_structure(self):
        self.run_ticks(60, snap(browsers={"chrome": proc(6000, procs=40)}))
        f = self.world.factions["chrome"]
        mine = [self.world.bld[i] for i, o in enumerate(self.world.owner) if o == f["id"]]
        for b in (B.PALACE, B.PLAZA, B.STREET, B.HOUSE, B.MARKET, B.TEMPLE, B.BARRACKS):
            self.assertIn(b, mine, B.NAMES[b])
        self.assertTrue(set(mine) & {B.FARM, B.PASTURE, B.BARN, B.DOCK, B.LUMBER})   # afueras
        # no todo son torres: la mayoría de las casas son bajas
        houses = [self.world.level[i] for i, o in enumerate(self.world.owner)
                  if o == f["id"] and self.world.bld[i] == B.HOUSE]
        self.assertGreater(sum(1 for lv in houses if lv <= 2), len(houses) / 2)
        self.assertIn("cult", f)                     # templo -> religión
        self.assertIn("obra", self.kinds())

    def test_war_gets_a_name_and_ends_in_peace(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["browsers_rules"].update(max_land_fraction=1.0, expand_per_tick=80)
        self.world = World(cfg)
        self.run_ticks(20, snap(browsers={"b": proc(8000)}))
        self.run_ticks(150, snap(browsers={"a": proc(10 ** 6), "b": proc(8000)}))
        self.assertIn("guerra", self.kinds())
        self.run_ticks(160, snap(browsers={"a": proc(1000), "b": proc(1000)}))   # se calman
        self.assertIn("paz", self.kinds())
        self.assertEqual(self.world.wars, {})

    def test_ssh_session_brings_an_embassy(self):
        session = {"user": "ana", "tty": "pts/1", "host": "100.1.2.3", "start": "x"}
        self.run_ticks(2, snap(sessions=[session], visits={"100.1.2.3": 7}))
        st = self.world.structures["embassy:100.1.2.3"]
        self.assertTrue(st["alive"])
        self.assertIn("visita", self.kinds())
        self.run_ticks(2, snap(sessions=[]))
        self.assertFalse(st["alive"])
        self.assertEqual(self.kinds().count("visita"), 2)

    def test_heroes_are_born_and_die(self):
        self.run_ticks(40, snap(browsers={"chrome": proc(3000)}))
        rnd = self.world.rng.random
        self.world.rng.random = lambda: 0.0            # forzar un nacimiento (en la vida real, cada tanto)
        self.run_ticks(1, snap(browsers={"chrome": proc(3000)}))
        self.world.rng.random = rnd
        self.assertTrue(self.world.notables)
        h = self.world.notables[0]
        h["life"] = 0
        self.run_ticks(1, snap(browsers={"chrome": proc(3000)}))
        self.assertFalse(h["alive"])

    def test_v2_save_is_migrated(self):
        self.run_ticks(5, snap(browsers={"chrome": proc(2000)}))
        data = json.loads(json.dumps(self.world.to_dict()))
        data["v"] = 2
        for k in ("bld", "born", "notables", "wars", "visitors"):
            data.pop(k)
        again = World(self.cfg, data)
        self.assertEqual(again.owner, self.world.owner)
        again.step(snap(browsers={"chrome": proc(2000)}))

    def test_each_install_gets_its_own_island(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["world"]["seed"] = 0
        a, b = World(cfg), World(cfg)
        self.assertNotEqual(a.seed, b.seed)
        self.assertNotEqual(a.terrain, b.terrain)
        again = World(cfg, json.loads(json.dumps(a.to_dict())))   # la semilla se guarda
        self.assertEqual(again.seed, a.seed)

    def test_forests_grow_and_lumber_camps_cut(self):
        before = self.world.terrain.count(FOREST)
        self.run_ticks(150, snap())
        self.assertGreater(self.world.terrain.count(FOREST), before)
        # un incendio se lleva el bosque: queda pradera
        i = self.world.terrain.index(FOREST)
        self.world.burn[i] = 1
        self.run_ticks(1, snap())
        self.assertEqual(self.world.terrain[i], GRASS)

    def test_powers_cost_resources_and_need_targets(self):
        self.run_ticks(30, snap(browsers={"chrome": proc(2000)}))
        f = self.world.factions["chrome"]
        x, y = f["capital"]
        self.world.resources.update(mana=0)
        self.world.step(snap(browsers={"chrome": proc(2000)}), [{"power": "rayo", "x": x, "y": y}])
        self.assertFalse(self.world.power_log[-1]["ok"])               # no alcanza el maná
        self.world.resources.update(mana=100, gold=200, stone=50)
        ev = self.world.step(snap(browsers={"chrome": proc(2000)}), [{"power": "rayo", "x": x, "y": y}])
        self.assertTrue(self.world.power_log[-1]["ok"])
        self.assertTrue(any(e["type"] == "lightning" for e in ev))
        self.assertAlmostEqual(self.world.resources["mana"], 95, delta=0.5)   # +un poco por actividad
        self.world.step(snap(browsers={"chrome": proc(2000)}), [{"power": "fiesta", "x": 0, "y": 0}])
        self.assertFalse(self.world.power_log[-1]["ok"])               # el mar no es un reino
        self.world.step(snap(browsers={"chrome": proc(2000)}), [{"power": "fiesta", "x": x, "y": y}])
        self.assertGreater(f["festival"], self.world.tick)
        coast = next(i for i, t in enumerate(self.world.terrain) if t in LAND and self.world._wild(i)
                     and any(self.world.terrain[j] == WATER for j in self.world.neighbors(i)))
        self.world.step(snap(browsers={"chrome": proc(2000)}),
                        [{"power": "faro", "x": coast % self.world.n, "y": coast // self.world.n}])
        self.assertTrue(any(s["kind"] == "lighthouse" for s in self.world.structures.values()))
        self.assertIn("poder", self.kinds())

    def test_lighthouse_is_built_by_workers_over_time(self):
        s = snap(browsers={"chrome": proc(2000)})
        self.run_ticks(30, s)
        self.world.resources.update(gold=200, stone=50)
        coast = next(i for i, t in enumerate(self.world.terrain) if t in LAND and self.world._wild(i)
                     and any(self.world.terrain[j] == WATER for j in self.world.neighbors(i)))
        x, y = coast % self.world.n, coast // self.world.n
        self.world.step(copy.deepcopy(s), [{"power": "faro", "x": x, "y": y}])
        lh = self.world.structures[f"lighthouse:{x}:{y}"]
        exp = self.world.expeditions[-1]
        self.assertTrue(lh["building"])
        self.assertEqual(exp["kind"], "build")
        self.assertEqual(exp["path"][-1], coast)               # caminan desde la capital hasta la costa
        self.run_ticks(exp["eta"] + exp["work"] + 1, s)
        self.assertFalse(lh["building"])
        self.assertFalse([e for e in self.world.expeditions if e["kind"] == "build"])

    def test_forest_is_planted_tile_by_tile(self):
        s = snap(browsers={"chrome": proc(2000)})
        self.run_ticks(30, s)
        self.world.resources.update(gold=100)
        cap = self.world.idx(*self.world.factions["chrome"]["capital"])
        target = next(i for i, t in enumerate(self.world.terrain) if t == GRASS and self.world._wild(i)
                      and self.world._dist(i, self.world.xy(cap)) > 8)
        self.world.step(copy.deepcopy(s), [{"power": "bosque", "x": target % self.world.n, "y": target // self.world.n}])
        exp = self.world.expeditions[-1]
        self.assertEqual(self.world.terrain[target], GRASS)    # todavía no: los druidas están en camino
        self.run_ticks(exp["eta"] + exp["work"] + 1, s)
        self.assertEqual(self.world.terrain[target], FOREST)

    def test_explorers_and_sieges(self):
        s = snap(browsers={"chrome": proc(4000, procs=30)},
                 fortresses={"daemon:sshd": fortress("daemon", "sshd", 5000)})
        self.run_ticks(40, s)
        self.world.rng.random = lambda: 0.0          # forzar que salgan ya (en la vida real, cada tanto)
        self.world._adventures()
        kinds = {e["kind"] for e in self.world.expeditions}
        self.assertIn("explore", kinds)
        self.assertIn("siege", kinds)
        self.assertIn("expedicion", self.kinds())

    def test_ambition_war_reshapes_the_map(self):
        s = snap(browsers={"chrome": proc(4000, procs=30), "zen": proc(3500, procs=20)})
        self.run_ticks(60, s)
        a, b = self.world.factions["chrome"], self.world.factions["zen"]
        self.world._declare_war(a, b)
        self.assertIn("war.declare", [e.get("k") for e in self.world.chronicle.entries])
        caps = (list(a["capital"]), list(b["capital"]))
        self.run_ticks(600, s)
        keys = [e.get("k") for e in self.world.chronicle.entries]
        # o chocaron las fronteras (batallas) o marcharon sobre la capital
        self.assertTrue({"battle", "exp.siegecity.start"} & set(keys))

    def test_story_of_the_day(self):
        import tempfile, os
        from reino import story
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_DATA_HOME"] = tmp
            try:
                self.run_ticks(40, snap(browsers={"chrome": proc(2000)}, boot=1))
                story.archive(self.world.chronicle.take_fresh())
                md, txt = story.export(None, self.world.kingdom, "Era del Alba")
                text = md.read_text()
                self.assertIn(f"# Crónica de {self.world.kingdom}", text)
                self.assertIn("Cromaria", text)
                self.assertNotIn("#", txt.read_text())    # el .txt es texto plano, para leer en voz alta
            finally:
                del os.environ["XDG_DATA_HOME"]

    def test_english_chronicle(self):
        i18n.set_lang("en")
        try:
            self.world = World(self.cfg)
            self.run_ticks(30, snap(browsers={"chrome": proc(2000)}, boot=1))
            text = self.texts()
            self.assertIn("Cromaria", text)
            self.assertNotIn(" de ", text)
            self.assertIn("of Cromaria", self.world.factions["chrome"]["display"])
        finally:
            i18n.set_lang("es")

    def test_save_roundtrip(self):
        self.run_ticks(10, snap(browsers={"chrome": proc(2000)}))
        data = json.loads(json.dumps(self.world.to_dict()))
        again = World(self.cfg, data)
        self.assertEqual(again.owner, self.world.owner)
        self.assertEqual(again.tick, self.world.tick)
        self.assertEqual(again.chronicle.entries, self.world.chronicle.entries)


class ProbeTest(unittest.TestCase):
    def test_children_inherit_browser_faction(self):
        probe = SystemProbe(config.load())
        procs = {
            1: {"pid": 1, "ppid": 0, "name": "systemd", "create_time": 0},
            10: {"pid": 10, "ppid": 1, "name": "bwrap", "create_time": 0},
            11: {"pid": 11, "ppid": 10, "name": "chrome", "create_time": 0},
            12: {"pid": 12, "ppid": 11, "name": "chrome_crashpad", "create_time": 0},
            13: {"pid": 13, "ppid": 11, "name": "Isolated Web Co", "create_time": 0},
            20: {"pid": 20, "ppid": 1, "name": "bash", "create_time": 0},
        }
        owners = probe._classify(procs)
        self.assertEqual(owners[13], ("browser", "chrome"))
        self.assertEqual(owners[12], ("browser", "chrome"))
        self.assertIsNone(owners[20])
        self.assertIsNone(owners[10])

    def test_system_processes_are_not_apps(self):
        probe = SystemProbe(config.load())
        self.assertFalse(probe._is_app("cosmic-applet-time"))
        self.assertFalse(probe._is_app("python3"))
        self.assertTrue(probe._is_app("claude"))


class CoverageTest(unittest.TestCase):
    def test_tiled_windows_with_gaps_count_as_covered(self):
        # Dos ventanas en mosaico en 2560x1440 con panel arriba y gaps de 8 px (caso real)
        rects = [(8, 46, 1268, 1386), (1284, 46, 1268, 1386)]
        self.assertGreater(_coverage(rects, 2560, 1440), 0.9)
        self.assertLess(_coverage(rects[:1], 2560, 1440), 0.6)


if __name__ == "__main__":
    unittest.main()
