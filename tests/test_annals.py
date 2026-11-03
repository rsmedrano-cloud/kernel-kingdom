import copy
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from reino import annals, config, i18n, story, video
from reino.daemon.world import World

from test_world import proc, snap


class AnnalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name, "XDG_STATE_HOME": self.tmp.name,
                                                "XDG_RUNTIME_DIR": self.tmp.name})
        self.env.start()
        i18n.set_lang("es")
        cfg = config.load()
        cfg["world"]["seed"] = 21
        self.world = World(cfg)
        s = snap(browsers={"chrome": proc(3000, procs=30), "firefox": proc(2500, 20)})
        for _ in range(80):
            self.world.step(copy.deepcopy(s))
        a, b = self.world.factions["chrome"], self.world.factions["firefox"]
        self.world._declare_war(a, b)
        for _ in range(60):
            self.world.step(copy.deepcopy(s))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_events_keep_their_proper_names(self):
        e = next(e for e in self.world.chronicle.entries if e.get("k") == "war.declare")
        self.assertEqual(e["p"]["a"], self.world.factions["chrome"]["realm"])
        self.assertIn("war", e["p"])

    def test_chapter_has_a_title_sections_and_an_epilogue(self):
        entries = self.world.chronicle.entries
        ch = annals.chapter("2026-11-03", entries, self.world.to_dict(), n=3)
        war = next(e for e in entries if e.get("k") == "war.declare")["p"]["war"]
        self.assertEqual(ch["title"], war[:1].upper() + war[1:])                 # el día lo titula la guerra
        self.assertIn("war", [s["theme"] for s in ch["sections"]])
        self.assertTrue(any("capítulo 3" in t for t in ch["epilogue"]))
        self.assertLessEqual(len(ch["narration"]), 12)
        text = annals.render(ch, markdown=False)
        self.assertTrue(text.startswith("CAPÍTULO 3:"))
        self.assertNotIn("#", text)

    def test_the_book_grows_one_chapter_per_day(self):
        entries = self.world.chronicle.entries
        half = len(entries) // 2
        annals.write("2026-11-02", entries[:half], {}, self.world.to_dict())
        annals.write("2026-11-03", entries[half:], {}, self.world.to_dict())
        index = annals._index()
        self.assertEqual([index[d]["n"] for d in sorted(index)], [1, 2])
        ch = annals.chapter("2026-11-03", entries[half:], self.world.to_dict())
        self.assertIn(index["2026-11-02"]["title"], " ".join(ch["previously"]))   # "en el capítulo anterior"
        book = annals.book("Prueba").read_text()
        self.assertIn("Capítulo 1:", book)
        self.assertIn("Capítulo 2:", book)

    def test_repetitive_events_are_summed_up(self):
        entries = [{"t": k, "tick": k, "kind": "guerra", "k": "battle", "text": f"Batalla {k}."} for k in range(30)]
        ch = annals.chapter("2026-11-03", entries, {})
        war = next(s for s in ch["sections"] if s["theme"] == "war")
        self.assertLessEqual(len(war["texts"]), 6)
        self.assertIn("batallas", war["texts"][-1])

    def test_bard_is_off_unless_configured(self):
        ch = annals.chapter("2026-11-03", self.world.chronicle.entries, self.world.to_dict())
        self.assertIsNone(annals.bard(ch, {}))
        self.assertIsNone(annals.bard(ch, {"bard": {"enabled": True, "url": "http://127.0.0.1:9", "timeout": 1}}))


class VideoPartsTest(unittest.TestCase):
    def test_narration_parts_are_joined_with_pauses(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for k, secs in enumerate((1.0, 0.5)):
                p = Path(tmp) / f"{k}.wav"
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(44100)
                    w.writeframes(b"\0\0" * int(44100 * secs))
                parts.append(p)
            out = Path(tmp) / "all.wav"
            video._join(parts, 0.5, out)
            with wave.open(str(out)) as w:
                self.assertAlmostEqual(w.getnframes() / w.getframerate(), 1.0 + 0.5 + 3 * 0.5, places=2)

    def test_voice_downloads_are_announced_before(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            i18n.set_lang("es")
            missing = video.missing_downloads({})
            self.assertEqual(len(missing), 2)
            self.assertIn("es_ES-davefx-medium", missing[1])


if __name__ == "__main__":
    unittest.main()
