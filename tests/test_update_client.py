from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.update_client import (
    UpdateInfo,
    _create_external_update_script,
    _download_payload_name,
    _looks_like_installer,
    _ps_single_quote,
    _record_to_update_info,
)


class UpdateClientTest(unittest.TestCase):
    def test_record_to_update_info_reads_distribution_record(self) -> None:
        info = _record_to_update_info(
            {
                "バージョン名": {"value": "1.2.0"},
                "バージョンコード": {"value": "12"},
                "リリースノート": {"value": "更新内容"},
                "APKファイル": {"value": [{"fileKey": "abc", "name": "TksToKintone.exe", "size": "12345"}]},
            }
        )

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version_name, "1.2.0")
        self.assertEqual(info.version_code, 12)
        self.assertEqual(info.file_key, "abc")
        self.assertEqual(info.file_size, 12345)

    def test_record_to_update_info_ignores_missing_file(self) -> None:
        info = _record_to_update_info(
            {
                "バージョン名": {"value": "1.2.0"},
                "バージョンコード": {"value": "12"},
                "APKファイル": {"value": []},
            }
        )

        self.assertIsNone(info)

    def test_looks_like_installer_requires_installer_name(self) -> None:
        self.assertTrue(_looks_like_installer(Path("tks-to-kintone-setup.exe")))
        self.assertTrue(_looks_like_installer(Path("TksToKintoneInstaller.exe")))
        self.assertTrue(_looks_like_installer(Path("tks-to-kintone-setup.installer")))
        self.assertFalse(_looks_like_installer(Path("TksToKintone.exe")))

    def test_ps_single_quote_escapes_path(self) -> None:
        self.assertEqual(_ps_single_quote(Path("C:/Temp/O'Reilly/setup.exe")), "C:/Temp/O''Reilly/setup.exe")

    def test_download_payload_name_does_not_use_exe_extension(self) -> None:
        self.assertEqual(_download_payload_name("tks-to-kintone-setup.exe"), "tks-to-kintone-setup.installer")

    def test_external_update_script_downloads_outside_app_process(self) -> None:
        info = UpdateInfo(
            version_name="1.2.0",
            version_code=12,
            file_key="abc'123",
            file_name="tks-to-kintone-setup.exe",
            file_size=12345,
        )
        with TemporaryDirectory() as temp_dir:
            script_path = _create_external_update_script(
                info,
                Path(temp_dir),
                info.file_name,
                Path("C:/Program Files/Manekiya/TksToKintone/TksToKintone.exe"),
            )

            script = script_path.read_text(encoding="utf-8")
            self.assertIn("System32\\curl.exe", script)
            self.assertIn("X-Cybozu-API-Token", script)
            self.assertIn("abc''123", script)
            self.assertIn("tks-to-kintone-setup.installer", script)
            self.assertIn("Move-Item -LiteralPath $payload -Destination $installer", script)


if __name__ == "__main__":
    unittest.main()
