from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import voucher_print_service


class _Settings:
    printer_name = "Printer"
    sumatra_path = ""


class TestVoucherPrintSumatraResolution(unittest.TestCase):
    def test_installed_path_is_used_and_old_bundle_is_not_considered(self) -> None:
        expected = r"C:\Program Files\SumatraPDF\SumatraPDF.exe"
        with mock.patch(
            "app.sumatra_detection.find_installed_sumatra_pdf_exe",
            return_value=(expected, "hklm64"),
        ):
            self.assertEqual(
                voucher_print_service.resolve_sumatra_executable(_Settings()),
                (expected, "hklm64"),
            )
        self.assertEqual(voucher_print_service._bundled_sumatra_paths(), [])

    def test_explicit_path_is_passed_to_installed_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "SumatraPDF.exe"
            exe.write_bytes(b"MZ")
            settings = _Settings()
            settings.sumatra_path = str(exe)
            self.assertEqual(
                voucher_print_service.resolve_sumatra_executable(settings),
                (str(exe), "saved"),
            )

    def test_missing_message_instructs_setup_rerun(self) -> None:
        self.assertIn("セットアップを再実行", voucher_print_service.SUMATRA_NOT_FOUND_MESSAGE)
        self.assertIn("SumatraPDFが見つかりません", voucher_print_service.SUMATRA_NOT_FOUND_MESSAGE)


if __name__ == "__main__":
    unittest.main()
