import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reino import config
from reino.daemon import archipelago as A
from reino.daemon.__main__ import build_state
from reino.daemon.world import WATER, World

from test_remote import REPORT
from test_world import proc, snap


class ArchipelagoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name + "/run",
                                                "XDG_DATA_HOME": self.tmp.name + "/data"})
        self.env.start()
        self.key = str(Path(self.tmp.name) / "kk-test-key")
        Path(self.key).write_text("llave de prueba")
        self.cfg = config.load()
        self.cfg["world"]["seed"] = 11
        self.local = snap(browsers={"chrome": proc(3000, procs=30), "firefox": proc(2500, 20)})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_ssh(self, report: dict | None) -> str:
        path = Path(self.tmp.name) / "ssh"
        if report is None:
            body = "echo 'kk@secreto-lab: Permission denied (publickey).' >&2\nexit 255\n"
        else:
            body = "cat <<'EOF'\n" + json.dumps(report) + "\nEOF\n"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def arch_with_report(self, world) -> A.Archipelago:
        rep = copy.deepcopy(REPORT)
        rep["host"] = "secreto-lab"
        rep["groups"].append({"name": "ollama", "procs": 4, "rss_mb": 4900.0, "cpu_s": 900.0, "started": 1793300100})
        self.cfg["remote"] = {"hosts": ["kk@secreto-lab", "-oProxyCommand=sh"], "every": 5,
                              "ssh": self.fake_ssh(rep), "key": self.key}
        arch = A.Archipelago(self.cfg)
        self.addCleanup(arch.stop.set)
        isl = arch.fleet.islands["kk@secreto-lab"]
        for k in range(2):
            r = copy.deepcopy(rep)
            r["t"] += 20 * k
            r["cpu"]["ticks_total"] += 1000 * k
            isl.update(A.remote.validate(r))
        return arch

    def test_targets_that_look_like_ssh_options_are_rejected(self):
        for bad in ("-oProxyCommand=sh", "kk@-oProxyCommand=x", "kk@host;rm", "kk @host", "host", ""):
            self.assertFalse(A.valid_target(bad), bad)
        self.assertTrue(A.valid_target("kk@secreto-lab"))

    def test_growing_the_grid_keeps_every_coordinate(self):
        w = World(self.cfg)
        for _ in range(60):
            w.step(copy.deepcopy(self.local))
        before = {f["name"]: (list(f["capital"]), f["tiles"]) for f in w.factions.values()}
        land = [(i % w.n, i // w.n, t) for i, t in enumerate(w.terrain) if t != WATER]
        owners = sorted((i % w.n, i // w.n, o) for i, o in enumerate(w.owner) if o)
        w.expand(w.n + 48)
        self.assertEqual(land, [(i % w.n, i // w.n, t) for i, t in enumerate(w.terrain) if t != WATER])
        self.assertEqual(owners, sorted((i % w.n, i // w.n, o) for i, o in enumerate(w.owner) if o))
        for _ in range(5):
            w.step(copy.deepcopy(self.local))
        self.assertEqual({k: v[0] for k, v in before.items()},
                         {f["name"]: list(f["capital"]) for f in w.factions.values()})
        w2 = World(self.cfg, json.loads(json.dumps(w.to_dict())))     # se guarda y se carga grande
        self.assertEqual(w2.n, w.n)

    def test_a_machine_becomes_an_island_of_the_same_world(self):
        w = World(self.cfg)
        arch = self.arch_with_report(w)
        self.assertEqual(list(arch.fleet.islands), ["kk@secreto-lab"])   # la dirección mala ni entra
        for _ in range(80):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))
        iid = A.island_id("kk@secreto-lab")
        self.assertIn(iid, w.isles)
        remote = [f for f in w.factions.values() if f.get("isle") == iid]
        self.assertTrue({"ollama", "jellyfin"} <= {f["proc"] for f in remote}, [f["proc"] for f in remote])
        for f in remote:                                                  # sus reinos viven en su isla
            x, y = f["capital"]
            self.assertEqual(w._tile_isle[w.idx(x, y)], iid)
        home = [f for f in w.factions.values() if not f.get("isle")]
        self.assertTrue(home and all(w._tile_isle[w.idx(*f["capital"])] is None for f in home))
        state = build_state(w, self.local, [], 2.0)
        state["islands"] = arch.summary(w)
        text = json.dumps(state).lower()
        self.assertNotIn("secreto", text)                                 # ni host ni usuario
        self.assertNotIn("kk@", text)
        self.assertTrue(any(e.get("k") == "isle.rise" for e in w.chronicle.entries))

    def test_names_that_carry_the_hostname_are_cleaned_and_repaired(self):
        self.assertEqual(A.clean_name("secreto-lab-openwebui", {"secreto-lab", "kk"}), "openwebui")
        self.assertEqual(A.clean_name("kk_postgres", {"secreto-lab", "kk"}), "postgres")
        self.assertEqual(A.clean_name("secreto-lab", {"secreto-lab"}), "app")
        w = World(self.cfg)
        arch = self.arch_with_report(w)
        isl = arch.fleet.islands["kk@secreto-lab"]
        rep = copy.deepcopy(isl.prev)
        rep["containers"].append({"name": "secreto-lab-openwebui", "runtime": "docker", "state": "running",
                                  "started": 1793300000, "rss_mb": 900.0, "cpu_s": 5.0, "image": "open-webui"})
        isl.update(rep)
        iid = A.island_id("kk@secreto-lab")
        # un mundo viejo que ya había fundado el reino con el nombre sucio
        for _ in range(40):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))
        dirty = w._new_faction(f"{iid}/secreto-lab-openwebui", "browser", {"since": 1.0})
        w.chronicle.add(w.tick, "fundacion", f"Nace {dirty['realm']} en la isla.")
        arch._scrubbed = set()
        for _ in range(5):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))
        text = json.dumps(build_state(w, self.local, [], 2.0)).lower()
        self.assertNotIn("secreto", text)
        self.assertIn(f"{iid}/openwebui", w.factions)

    def test_island_size_follows_the_machine_and_shrinking_leaves_no_islets(self):
        self.assertEqual(World.isle_scale(512), 0.4)
        self.assertEqual(World.isle_scale(64 * 1024), 1.0)
        self.assertLess(World.isle_scale(2048), World.isle_scale(16 * 1024))
        w = World(self.cfg)
        arch = self.arch_with_report(w)
        for _ in range(60):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))
        iid = A.island_id("kk@secreto-lab")
        big = sum(1 for i, t in enumerate(w._tile_isle) if t == iid and w.terrain[i] != WATER)
        w.reshape_isle(iid, 0.4)
        land = {i for i, t in enumerate(w._tile_isle) if t == iid and w.terrain[i] != WATER}
        self.assertLess(len(land), big)
        for f in w.factions.values():                            # nada quedó en el agua
            if f.get("isle") == iid:
                self.assertIn(w.idx(*f["capital"]), land)
        for s in w.structures.values():
            if s["key"].startswith(iid):
                self.assertIn(w.idx(s["x"], s["y"]), land)
        for _ in range(5):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))

    def test_war_between_islands_sails_across_the_sea(self):
        w = World(self.cfg)
        arch = self.arch_with_report(w)
        for _ in range(80):
            w.step(arch.merge(copy.deepcopy(self.local), w, 2.0))
        iid = A.island_id("kk@secreto-lab")
        a = next(f for f in w.factions.values() if f.get("isle") is None and f["kind"] == "browser")
        b = next(f for f in w.factions.values() if f.get("isle") == iid and f.get("alive"))
        w._declare_war(a, b)
        army = next(e for e in w.expeditions if e["kind"] == "army")
        self.assertTrue(army.get("naval"))
        self.assertTrue(any(w.terrain[i] == WATER for i in army["path"]))
        self.assertTrue(any(e.get("k") == "exp.fleet.sail" for e in w.chronicle.entries))

    def test_offline_machine_shows_a_short_reason(self):
        w = World(self.cfg)
        self.cfg["remote"] = {"hosts": ["kk@secreto-lab"], "every": 5, "ssh": self.fake_ssh(None), "key": self.key}
        arch = A.Archipelago(self.cfg)
        self.addCleanup(arch.stop.set)
        arch.fleet.poll(force=True)
        s = arch.summary(w)[0]
        self.assertFalse(s["online"])
        self.assertEqual(s["reason"], "noaccess")
        self.assertNotIn("secreto", json.dumps(arch.summary(w)))
        merged = arch.merge(copy.deepcopy(self.local), w, 2.0)
        self.assertEqual(w.isles, {})                                     # sin respuesta no hay isla
        self.assertEqual(set(merged["browsers"]), {"chrome", "firefox"})


if __name__ == "__main__":
    unittest.main()
