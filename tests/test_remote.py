import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reino import remote

REPORT = json.loads((Path(__file__).resolve().parent.parent / "docs" / "remote-protocol.md").read_text()
                    .split("## Ejemplo")[1].split("```json")[1].split("```")[0])


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        self.env.start()
        self.key = str(Path(self.tmp.name) / "kk-test-key")
        Path(self.key).write_text("llave de prueba")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_ssh(self, body: str) -> str:
        path = Path(self.tmp.name) / "ssh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_example_in_the_protocol_doc_is_valid(self):
        r = remote.validate(REPORT)
        self.assertEqual(r["host"], "servidor1")
        self.assertEqual([g["name"] for g in r["groups"]], ["postgres", "smbd"])
        self.assertEqual(r["containers"][1]["state"], "exited")

    def test_command_uses_system_ssh_with_multiplexing_and_no_remote_command(self):
        cmd = remote.ssh_command("kk@servidor1", "~/.ssh/kernel-kingdom")
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-1], "kk@servidor1")                   # nada después del destino
        joined = " ".join(cmd)
        for opt in ("BatchMode=yes", "ControlMaster=auto", "ControlPersist=10m", "IdentitiesOnly=yes"):
            self.assertIn(opt, joined)
        self.assertIn(os.path.expanduser("~/.ssh/kernel-kingdom"), cmd)

    def test_bad_reports_are_rejected_or_cleaned(self):
        with self.assertRaises(remote.RemoteError):
            remote.validate({"v": 2})
        with self.assertRaises(remote.RemoteError):
            remote.validate({"v": 1, "error": "no hay /proc"})
        r = remote.validate({"v": 1, "cpu": {"cores": "ocho"}, "groups": [{"name": "x", "rss_mb": -5}, {"rss_mb": 1}],
                             "cmdline": "secreto"})
        self.assertEqual(r["cores"], 1)
        self.assertEqual(r["groups"], [{"name": "x", "procs": 1, "rss_mb": 0.0, "cpu_s": 0.0, "started": None}])
        self.assertNotIn("cmdline", r)

    def test_missing_key_never_runs_ssh(self):
        """Sin la llave propia, ssh caería en ~/.ssh/id_* (acceso completo): no se llama."""
        marker = Path(self.tmp.name) / "ssh-was-called"
        fake = self.fake_ssh(f"touch {marker}\necho '{{}}'\n")
        with self.assertRaises(remote.RemoteError) as err:
            remote.fetch("kk@servidor1", str(Path(self.tmp.name) / "no-existe"), ssh=fake)
        self.assertFalse(marker.exists())
        self.assertIn("identity", str(err.exception))

    def test_non_report_reply_closes_the_master_connection(self):
        log = Path(self.tmp.name) / "calls"
        fake = self.fake_ssh(f'echo "$@" >> {log}\necho "bienvenido a bash"\n')
        with self.assertRaises(remote.RemoteError):
            remote.fetch("kk@servidor1", self.key, ssh=fake)
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("-O exit", calls[1])                            # se cerró la conexión maestra
        for opt in ("IdentityAgent=none", "PasswordAuthentication=no", "PreferredAuthentications=publickey"):
            self.assertIn(opt, calls[0])

    def test_rates_come_from_two_reports(self):
        isl = remote.Island("kk@servidor1")
        a = remote.validate(REPORT)
        b = json.loads(json.dumps(REPORT))
        b["t"] += 20
        b["cpu"]["ticks_total"] += 2000
        b["cpu"]["ticks_idle"] += 1500                                  # 25% ocupado
        b["net"]["rx_bytes"] += 20 * 1024 * 1024                        # 1 MB/s
        b["groups"][0]["cpu_s"] += 10                                   # 50% de un núcleo
        isl.update(a)
        self.assertEqual(isl.snap["cpu"], 0.0)                          # con un solo reporte no hay tasas
        isl.update(remote.validate(b))
        self.assertAlmostEqual(isl.snap["cpu"], 25.0)
        self.assertAlmostEqual(isl.snap["net_rx"], 1.0)
        self.assertAlmostEqual(isl.snap["groups"][0]["cpu"], 50.0)
        self.assertTrue(isl.online)

    def test_fleet_fetches_with_ssh_and_marks_offline(self):
        good = self.fake_ssh("cat <<'EOF'\n" + json.dumps(REPORT) + "\nEOF\n")
        fleet = remote.Fleet({"remote": {"hosts": ["kk@servidor1"], "every": 20, "key": self.key}}, ssh=good)
        self.assertTrue(fleet.poll(now=0))
        self.assertFalse(fleet.poll(now=5))                             # todavía no toca
        isl = fleet.islands["kk@servidor1"]
        self.assertTrue(isl.online, isl.error)
        fleet.ssh = self.fake_ssh("echo 'kk@servidor1: Permission denied (publickey).' >&2\nexit 255\n")
        for k in range(remote.OFFLINE_AFTER):
            fleet.poll(now=100 + 30 * k)
        self.assertFalse(isl.online)
        self.assertIn("Permission denied", isl.error)


if __name__ == "__main__":
    unittest.main()
