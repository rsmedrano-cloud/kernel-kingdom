import copy
import os
import tempfile
import unittest
from unittest import mock

from reino import config, timelapse
from reino.daemon.world import World

from test_world import proc, snap


class TimelapseTest(unittest.TestCase):
    def test_records_and_builds_a_gif_without_pillow(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            cfg = config.load()
            cfg["world"]["seed"] = 5
            w = World(cfg)
            s = snap(browsers={"chrome": proc(3000), "firefox": proc(2000)})
            for k in range(60):
                w.step(copy.deepcopy(s))
                if k % 20 == 0:
                    timelapse.record(w, force=True)
            day = sorted(p.name for p in timelapse.folder().iterdir())[-1][:-6]
            self.assertEqual(len(timelapse.load(day)), 3)
            with mock.patch.dict("sys.modules", {"PIL": None}):
                out = timelapse.export(day, out_dir=timelapse.folder())
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"GIF89a") and data.endswith(b";"))
            try:
                from PIL import Image
            except ImportError:
                return
            im = Image.open(out)
            self.assertEqual(im.n_frames, 3)
            im.seek(2)
            im.convert("RGB")                               # decodifica sin errores


if __name__ == "__main__":
    unittest.main()
