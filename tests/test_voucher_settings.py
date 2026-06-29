"""伝票設定（印刷する伝票デフォルト・キャッシュ保存期間）の保存/読み込みテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager


@contextmanager
def _temp_home():
    previous = os.environ.get("TKS_TO_KINTONE_HOME")
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
        try:
            yield temp_dir
        finally:
            if previous is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = previous


class TestVoucherSettings(unittest.TestCase):
    def test_default_print_types_default_is_all(self) -> None:
        from app import voucher_settings
        from app.voucher_templates import VOUCHER_IDS

        with _temp_home():
            self.assertEqual(voucher_settings.load_default_print_types(), list(VOUCHER_IDS))

    def test_save_and_load_print_types_roundtrip(self) -> None:
        from app import voucher_settings

        with _temp_home():
            voucher_settings.save_default_print_types(["03", "01", "05"])
            # VOUCHER_IDS の並び順に正規化される
            self.assertEqual(voucher_settings.load_default_print_types(), ["01", "03", "05"])

    def test_save_empty_print_types(self) -> None:
        from app import voucher_settings

        with _temp_home():
            voucher_settings.save_default_print_types([])
            self.assertEqual(voucher_settings.load_default_print_types(), [])

    def test_cache_retention_default_is_60(self) -> None:
        from app import voucher_settings

        with _temp_home():
            self.assertEqual(voucher_settings.load_cache_retention_days(), 60)

    def test_save_and_load_cache_retention(self) -> None:
        from app import voucher_settings

        with _temp_home():
            voucher_settings.save_cache_retention_days(14)
            self.assertEqual(voucher_settings.load_cache_retention_days(), 14)

    def test_invalid_retention_falls_back_to_default(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_cache_retention_days("abc"), 60)
        self.assertEqual(voucher_settings.normalize_cache_retention_days(0), 60)
        self.assertEqual(voucher_settings.normalize_cache_retention_days(-3), 60)

    def test_record_retention_default_is_1095(self) -> None:
        from app import voucher_settings

        with _temp_home():
            self.assertEqual(voucher_settings.load_record_retention_days(), 1095)

    def test_save_and_load_record_retention(self) -> None:
        from app import voucher_settings

        with _temp_home():
            voucher_settings.save_record_retention_days(365)
            self.assertEqual(voucher_settings.load_record_retention_days(), 365)

    def test_invalid_record_retention_falls_back_to_default(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_record_retention_days("abc"), 1095)
        self.assertEqual(voucher_settings.normalize_record_retention_days(0), 1095)

    def test_parse_print_types_filters_unknown(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.parse_print_types("99,03,01"), ["01", "03"])


if __name__ == "__main__":
    unittest.main()
