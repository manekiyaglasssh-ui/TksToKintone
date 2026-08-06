from __future__ import annotations

import unittest
from pathlib import Path

from app.version import VERSION_CODE, VERSION_NAME
from tks_to_kintone import __version__

ROOT = Path(__file__).resolve().parents[1]
STABLE_APP_ID = "AppId={{8C19583E-55BA-47BA-93AC-C9F2E1CF3A9F}"


class TestVersion161(unittest.TestCase):
    def test_application_version(self) -> None:
        self.assertEqual((VERSION_NAME, VERSION_CODE, __version__), ("1.6.2", 46, "1.6.2"))

    def test_packaging_version_and_stable_identity(self) -> None:
        installer = (ROOT / "installer" / "tks-to-kintone.iss").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('#define MyAppVersion "1.6.2"', installer)
        self.assertIn("VersionInfoVersion=1.6.2.46", installer)
        self.assertIn('version = "1.6.2"', pyproject)
        self.assertEqual(installer.count(STABLE_APP_ID), 1)
        self.assertNotIn("UpgradeCode", installer)
        self.assertIn(r"DefaultDirName={autopf}\Manekiya\TksToKintone", installer)
        self.assertIn(r'Name: "{commonappdata}\Manekiya\TksToKintone"', installer)
        self.assertIn("Flags: ignoreversion recursesubdirs createallsubdirs", installer)
        self.assertIn("Flags: ignoreversion onlyifdoesntexist", installer)

    def test_pyinstaller_version_info(self) -> None:
        version_info = (ROOT / "installer" / "version_info.txt").read_text(encoding="utf-8")
        spec = (ROOT / "TksToKintone.spec").read_text(encoding="utf-8")
        self.assertIn("filevers=(1, 6, 2, 46)", version_info)
        self.assertIn("prodvers=(1, 6, 2, 46)", version_info)
        self.assertIn("StringStruct('FileVersion', '1.6.2.46')", version_info)
        self.assertIn("StringStruct('ProductVersion', '1.6.2.46')", version_info)
        self.assertIn("version='installer/version_info.txt'", spec)

    def test_release_history_is_preserved(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## 1.5.13 (43)", changelog)
        self.assertIn("## 1.5.12 (42)", changelog)
        self.assertIn("## 1.5.11 (41)", changelog)


if __name__ == "__main__":
    unittest.main()
