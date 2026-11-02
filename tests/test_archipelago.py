import copy
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from reino import config
from reino.daemon import archipelago as A
from reino.daemon.__main__ import build_state, write_atomic
from reino.daemon.world import World

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

    def test_targets_that_look_like_ssh_options_are_rejected(self):
        for bad in ("-oProxyCommand=sh", "kk@-oProxyCommand=x", "kk@host;rm", "kk @host", "host", ""):
            self.assertFalse(A.valid_target(bad), bad)
        self.assertTrue(A.valid_target("kk@secreto-lab"))

    def test_island_world_from_reports_without_leaking_names(self):
        cfg = config.load()
        rep = copy.deepcopy(REPORT)
        rep["host"] = "secreto-lab"
        cfg["remote"] = {"hosts": ["kk@secreto-lab", "-oProxyCommand=sh"], "every": 5,
                         "ssh": self.fake_ssh(rep), "key": self.key}
        arch = A.Archipelago(cfg)
        try:
            self.assertEqual(list(arch.fleet.islands), ["kk@secreto-lab"])   # la dirección mala ni entra
            isl = arch.fleet.islands["kk@secreto-lab"]
            for k in range(2):
                r = copy.deepcopy(rep)
                r["t"] += 20 * k
                r["cpu"]["ticks_total"] += 1000 * k
                isl.update(A.remote.validate(r))
            for tick in range(2, 80, 2):
                arch.step(tick, 2.0, {}, build_state, write_atomic)
            iid = A.island_id("kk@secreto-lab")
            state_file = arch._state_dir() / f"{iid}.json"
            st = json.loads(state_file.read_text())
            self.assertEqual(st["isle"], iid)
            self.assertTrue(st["islands"][0]["online"])
            self.assertNotEqual(st["kingdom"], "")
            realms = {f["name"] for f in st["factions"]}
            self.assertTrue({"jellyfin", "postgres"} & realms, realms)       # lo pesado se vuelve reino
            text = state_file.read_text().lower()
            self.assertNotIn("secreto", text)                                 # ni host ni usuario
            self.assertNotIn("kk@", text)
            arch.save(write_atomic)
            self.assertTrue((arch._save_dir() / f"{iid}.json").exists())
        finally:
            arch.stop.set()

    def test_unreachable_island_shows_a_short_reason(self):
        cfg = config.load()
        cfg["remote"] = {"hosts": ["kk@secreto-lab"], "every": 5, "ssh": self.fake_ssh(None), "key": self.key}
        arch = A.Archipelago(cfg)
        try:
            arch.fleet.poll(force=True)
            s = arch.summary()[0]
            self.assertFalse(s["online"])
            self.assertEqual(s["reason"], "noaccess")
            self.assertNotIn("secreto", json.dumps(arch.summary()))
        finally:
            arch.stop.set()

    def test_main_world_name_is_not_overridden(self):
        self.assertIsNotNone(World(config.load()).kingdom)


if __name__ == "__main__":
    unittest.main()
