from __future__ import annotations

import unittest
from pathlib import Path

from app.version import VERSION_CODE, VERSION_NAME
from tks_to_kintone import __version__


ROOT = Path(__file__).resolve().parents[1]


class TestVersion1413(unittest.TestCase):
    def test_application_version(self) -> None:
        self.assertEqual(VERSION_NAME, "1.4.13")
        self.assertEqual(VERSION_CODE, 23)
        self.assertEqual(__version__, "1.4.13")

    def test_packaging_versions(self) -> None:
        installer = (ROOT / "installer" / "tks-to-kintone.iss").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('#define MyAppVersion "1.4.13"', installer)
        self.assertIn("VersionInfoVersion=1.4.13.23", installer)
        self.assertIn('version = "1.4.13"', pyproject)


if __name__ == "__main__":
    unittest.main()
