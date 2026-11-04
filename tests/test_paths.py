import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reino import paths, video


class PrivacyTest(unittest.TestCase):
    def test_folders_are_only_for_their_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: f"{tmp}/{k}" for k in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR")}
            with mock.patch.dict(os.environ, env):
                old = paths.data_dir()
                old.mkdir(parents=True)
                old.chmod(0o755)                                   # como quedaba antes de 0.6.1
                paths.make_private()
                for d in (paths.data_dir(), paths.save_file().parent, paths.user_config().parent,
                          paths.state_file().parent):
                    self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)

    def test_studio_downloads_can_be_removed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            self.assertEqual(video.downloads_mb(), 0)
            f = video.voice_dir() / "modelo.onnx"
            f.write_bytes(b"\0" * 2_000_000)
            self.assertEqual(video.downloads_mb(), 2)
            video.remove_downloads()
            self.assertFalse((Path(tmp) / "kernel-kingdom" / "voice").exists())
            self.assertEqual(video.downloads_mb(), 0)


if __name__ == "__main__":
    unittest.main()
