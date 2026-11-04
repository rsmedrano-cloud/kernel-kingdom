import copy
import json
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
        a, b = self.world.factions["chrome"]["realm"], self.world.factions["firefox"]["realm"]
        # la que declaró el test (puede haber estallado otra antes, por azar)
        e = next(e for e in self.world.chronicle.entries if e.get("k") == "war.declare"
                 and (e.get("p") or {}).get("a") == a and (e.get("p") or {}).get("b") == b)
        self.assertIn("war", e["p"])

    def test_chapter_has_a_title_sections_and_an_epilogue(self):
        entries = self.world.chronicle.entries
        ch = annals.chapter("2026-11-03", entries, self.world.to_dict(), n=3)
        wars = {e["p"]["war"] for e in entries if e.get("k") in ("war.declare", "war.start") and "p" in e}
        self.assertIn(ch["title"], {w[:1].upper() + w[1:] for w in wars})         # el día lo titula una guerra
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

    def test_days_slept_through_midnight_get_their_chapter(self):
        import datetime as dt
        entries = self.world.chronicle.entries
        half = len(entries) // 2
        for k, part in enumerate((entries[:half], entries[half:])):   # dos días con crónica, sin capítulo
            t = dt.datetime(2026, 11, 2 + k, 12).timestamp()
            story.archive([dict(e, t=t) for e in part])
        self.assertEqual(story.missing_days("2026-11-05"), ["2026-11-02", "2026-11-03"])
        self.assertEqual(story.catch_up(world=self.world.to_dict(), today="2026-11-05"), ["2026-11-02", "2026-11-03"])
        index = annals._index()
        self.assertEqual([index[d]["n"] for d in ("2026-11-02", "2026-11-03")], [1, 2])
        self.assertEqual(story.catch_up(today="2026-11-05"), [])                # ya están: no se reescriben

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


class BardTest(unittest.TestCase):
    def serve(self, reply: dict):
        import http.server
        import threading
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}", seen

    def chapter(self):
        i18n.set_lang("es")
        return {"world": "Kernelia", "title": "La Caída de Zenhar", "previously": [],
                "prose": ["Escuchad la crónica de Kernelia.", "Zenhar se disolvió."]}

    def test_ollama(self):
        url, seen = self.serve({"response": "Escuchad la crónica de Kernelia. " * 20})
        text = annals.bard(self.chapter(), {"bard": {"enabled": True, "url": url, "model": "m"}})
        self.assertTrue(text.startswith("Escuchad"))
        self.assertEqual(seen["path"], "/api/generate")
        self.assertIn("Zenhar se disolvió.", seen["body"]["prompt"])          # el borrador ordenado va en el pedido

    def test_openai_compatible_with_key_from_env(self):
        url, seen = self.serve({"choices": [{"message": {"content": "Escuchad la crónica. " * 20}}]})
        with mock.patch.dict(os.environ, {"KK_TEST_KEY": "secreto"}):
            text = annals.bard(self.chapter(), {"bard": {"enabled": True, "provider": "openai", "url": url + "/v1",
                                                         "model": "m", "api_key_env": "KK_TEST_KEY"}})
        self.assertTrue(text.startswith("Escuchad"))
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer secreto")

    def test_a_short_answer_is_not_used(self):
        url, _ = self.serve({"response": "No puedo."})
        self.assertIsNone(annals.bard(self.chapter(), {"bard": {"enabled": True, "url": url}}))


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
            missing = video.missing_downloads({})                          # Kokoro: entorno, modelo y voces
            self.assertEqual(len(missing), 3)
            self.assertIn("kokoro-v1.0.onnx", missing[1])
            piper = video.missing_downloads({"video": {"engine": "piper"}})
            self.assertEqual(len(piper), 2)
            self.assertIn("es_ES-davefx-medium", piper[1])


if __name__ == "__main__":
    unittest.main()
