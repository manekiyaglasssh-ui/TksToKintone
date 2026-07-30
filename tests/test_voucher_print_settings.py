from __future__ import annotations

import unittest

from app.voucher_settings import DEFAULT_SUMATRA_PATH, normalize_sumatra_path


class TestVoucherPrintSettings1513(unittest.TestCase):
    def test_empty_path_means_auto_detection(self) -> None:
        self.assertEqual(DEFAULT_SUMATRA_PATH, "")
        self.assertEqual(normalize_sumatra_path(""), "")

    def test_explicit_path_is_preserved_for_first_priority(self) -> None:
        path = r"D:\PDF tools\SumatraPDF.exe"
        self.assertEqual(normalize_sumatra_path(path), path)


if __name__ == "__main__":
    unittest.main()
