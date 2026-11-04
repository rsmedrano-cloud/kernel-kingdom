import re
import unittest
from pathlib import Path

from reino import __version__

ROOT = Path(__file__).resolve().parent.parent


class PackagingTest(unittest.TestCase):
    def test_every_package_has_the_same_version(self):
        def find(path, pattern):
            return re.search(pattern, (ROOT / path).read_text(), re.M).group(1)
        self.assertEqual(find("pyproject.toml", r'^version = "(.+)"'), __version__)
        self.assertEqual(find("packaging/rpm/kernel-kingdom.spec", r"^Version:\s+(\S+)"), __version__)
        self.assertEqual(find("packaging/arch/PKGBUILD", r"^pkgver=(\S+)"), __version__)

    def test_launcher_and_icon_are_shipped(self):
        desktop = (ROOT / "packaging" / "dev.kernelkingdom.Window.desktop").read_text()
        self.assertIn("Exec=kernel-kingdom window", desktop)
        self.assertTrue((ROOT / "assets" / "kernel-kingdom.png").is_file())


if __name__ == "__main__":
    unittest.main()
