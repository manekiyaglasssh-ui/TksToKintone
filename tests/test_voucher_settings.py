"""伝票設定（印刷する伝票デフォルト・キャッシュ保存期間）の保存/読み込みテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

try:
    from PySide6.QtCore import QSettings

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


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

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_voucher_printer_settings_defaults(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(str(Path(temp_dir) / "settings.ini"), QSettings.Format.IniFormat)
            loaded = voucher_settings.load_voucher_printer_settings(settings)
        self.assertEqual(loaded.printer_name, "")
        self.assertEqual(loaded.paper_size, "B5")
        self.assertEqual(loaded.orientation, "landscape")
        self.assertEqual(loaded.color_mode, "grayscale")
        self.assertEqual(loaded.copies, 1)
        self.assertEqual(loaded.scale_mode, "actual_size")
        self.assertEqual(loaded.print_backend, "sumatra")
        self.assertEqual(loaded.acrobat_path, "")
        self.assertTrue(loaded.acrobat_hide_window)
        self.assertTrue(loaded.acrobat_close_after_print)
        self.assertEqual(loaded.acrobat_close_delay_seconds, 10)
        self.assertFalse(loaded.acrobat_allow_force_kill)
        self.assertTrue(loaded.acrobat_hide_watch_enabled)
        self.assertEqual(loaded.acrobat_hide_watch_seconds, 10)
        # 新規環境の既定は同梱 SumatraPDF。SumatraPDFパスはインストール先の固定既定。
        self.assertEqual(loaded.print_backend, "sumatra")
        self.assertEqual(loaded.sumatra_path, voucher_settings.DEFAULT_SUMATRA_PATH)
        self.assertEqual(
            loaded.sumatra_print_settings, "noscale,monochrome,paper=auto,bin=auto,center"
        )
        self.assertEqual(loaded.sumatra_paperkind, "")
        self.assertEqual(loaded.sumatra_wait_timeout_seconds, 15)
        self.assertFalse(loaded.sumatra_allow_force_kill)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_voucher_printer_settings_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.ini"
            custom_sumatra = Path(temp_dir) / "SumatraPDF.exe"
            custom_sumatra.write_bytes(b"exe")
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            voucher_settings.save_voucher_printer_settings(
                voucher_settings.VoucherPrinterSettings(
                    printer_name="Printer A",
                    paper_size="B5",
                    orientation="landscape",
                    color_mode="grayscale",
                    copies=2,
                    scale_mode="fit_to_page",
                    print_backend="qt",
                    acrobat_path=r"C:\Adobe\AcroRd32.exe",
                    acrobat_hide_window=False,
                    acrobat_close_after_print=False,
                    acrobat_close_delay_seconds=42,
                    acrobat_allow_force_kill=True,
                    acrobat_hide_watch_enabled=False,
                    acrobat_hide_watch_seconds=12,
                    sumatra_path=str(custom_sumatra),
                    sumatra_print_settings="noscale,monochrome,paperkind=13,center",
                    sumatra_paperkind="13",
                    sumatra_wait_timeout_seconds=42,
                    sumatra_allow_force_kill=True,
                ),
                settings,
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertEqual(loaded.printer_name, "Printer A")
        self.assertEqual(loaded.paper_size, "B5")
        self.assertEqual(loaded.orientation, "landscape")
        self.assertEqual(loaded.color_mode, "grayscale")
        self.assertEqual(loaded.copies, 2)
        self.assertEqual(loaded.scale_mode, "fit_to_page")
        self.assertEqual(loaded.print_backend, "qt")
        self.assertEqual(loaded.acrobat_path, r"C:\Adobe\AcroRd32.exe")
        self.assertFalse(loaded.acrobat_hide_window)
        self.assertFalse(loaded.acrobat_close_after_print)
        self.assertEqual(loaded.acrobat_close_delay_seconds, 42)
        self.assertTrue(loaded.acrobat_allow_force_kill)
        self.assertFalse(loaded.acrobat_hide_watch_enabled)
        self.assertEqual(loaded.acrobat_hide_watch_seconds, 12)
        self.assertEqual(loaded.sumatra_path, str(custom_sumatra))
        self.assertEqual(loaded.sumatra_print_settings, "noscale,monochrome,paperkind=13,center")
        self.assertEqual(loaded.sumatra_paperkind, "13")
        self.assertEqual(loaded.sumatra_wait_timeout_seconds, 42)
        self.assertTrue(loaded.sumatra_allow_force_kill)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_sumatra_path_empty_uses_auto_detection_and_explicit_is_preserved(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.ini"
            settings = QSettings(str(path), QSettings.Format.IniFormat)
            settings.setValue(voucher_settings.VOUCHER_PRINT_SUMATRA_PATH, "")
            loaded = voucher_settings.load_voucher_printer_settings(settings)
            self.assertEqual(loaded.sumatra_path, "")

            settings.setValue(
                voucher_settings.VOUCHER_PRINT_SUMATRA_PATH,
                str(Path(temp_dir) / "missing" / "SumatraPDF.exe"),
            )
            loaded = voucher_settings.load_voucher_printer_settings(settings)
            self.assertEqual(
                loaded.sumatra_path,
                str(Path(temp_dir) / "missing" / "SumatraPDF.exe"),
            )

    def test_sumatra_path_valid_custom_is_kept(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            custom_sumatra = Path(temp_dir) / "SumatraPDF.exe"
            custom_sumatra.write_bytes(b"exe")
            self.assertEqual(
                voucher_settings.normalize_sumatra_path(str(custom_sumatra)),
                str(custom_sumatra),
            )

    def test_dependency_check_does_not_restore_from_bundle(self) -> None:
        from unittest import mock

        from app import voucher_settings

        installed = r"C:\Program Files\SumatraPDF\SumatraPDF.exe"
        with mock.patch(
            "app.sumatra_detection.find_installed_sumatra_pdf_exe",
            return_value=(installed, "hklm64"),
        ):
            self.assertTrue(voucher_settings.ensure_default_sumatra_executable())
        self.assertEqual(voucher_settings._sumatra_bundle_restore_candidates(), [])

    def test_dependency_missing_logs_without_copy_attempt(self) -> None:
        from unittest import mock

        from app import voucher_settings

        with mock.patch(
            "app.sumatra_detection.find_installed_sumatra_pdf_exe",
            return_value=("", "not_found"),
        ), self.assertLogs("tks_to_kintone_app", level="WARNING") as cm:
            self.assertFalse(voucher_settings.ensure_default_sumatra_executable())
        self.assertTrue(
            any("voucher_print_sumatra_not_installed" in line for line in cm.output)
        )

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_show_pdf_created_dialog_default_is_on(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.ini"
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertTrue(loaded.show_pdf_created_dialog)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_show_pdf_created_dialog_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.ini"
            store = QSettings(str(path), QSettings.Format.IniFormat)
            voucher_settings.save_voucher_printer_settings(
                voucher_settings.VoucherPrinterSettings(show_pdf_created_dialog=False),
                store,
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertFalse(loaded.show_pdf_created_dialog)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_open_pdf_after_create_default_is_on_and_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.ini"
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
            self.assertTrue(loaded.open_pdf_after_create)

            store = QSettings(str(path), QSettings.Format.IniFormat)
            voucher_settings.save_voucher_printer_settings(
                voucher_settings.VoucherPrinterSettings(open_pdf_after_create=False),
                store,
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertFalse(loaded.open_pdf_after_create)

    def test_sumatra_wait_timeout_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_sumatra_wait_timeout_seconds("abc"), 15)
        self.assertEqual(voucher_settings.normalize_sumatra_wait_timeout_seconds(1), 5)
        self.assertEqual(voucher_settings.normalize_sumatra_wait_timeout_seconds(999), 120)
        self.assertEqual(voucher_settings.normalize_sumatra_wait_timeout_seconds(30), 30)

    def test_build_sumatra_print_settings_from_detail_options(self) -> None:
        from app import voucher_settings

        settings = voucher_settings.build_sumatra_print_settings(
            scaling_mode="fit",
            monochrome=True,
            paper_mode="paperkind",
            paperkind="182",
            center=True,
            auto_rotation=False,
            bin_value="auto",
            extra_options="duplexshort",
        )
        self.assertEqual(
            settings,
            "fit,monochrome,paperkind=182,bin=auto,center,disable-auto-rotation,duplexshort",
        )

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_sumatra_profiles_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "settings.ini"), QSettings.Format.IniFormat)
            profiles = [
                voucher_settings.SumatraPrintProfile(
                    profile_name="B5 test",
                    print_settings="noscale,monochrome,paperkind=182,bin=auto,center",
                    paperkind="182",
                    memo="memo",
                    updated_at="2026-07-03T12:00:00",
                )
            ]
            voucher_settings.save_sumatra_print_profiles(profiles, store)
            loaded = voucher_settings.load_sumatra_print_profiles(
                QSettings(str(Path(temp_dir) / "settings.ini"), QSettings.Format.IniFormat)
            )
        self.assertEqual(loaded[0].profile_name, "B5 test")
        self.assertEqual(loaded[0].paperkind, "182")
        self.assertEqual(loaded[0].memo, "memo")

    def test_acrobat_close_delay_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_acrobat_close_delay_seconds("abc"), 10)
        self.assertEqual(voucher_settings.normalize_acrobat_close_delay_seconds(1), 5)
        self.assertEqual(voucher_settings.normalize_acrobat_close_delay_seconds(99), 60)

    def test_acrobat_hide_watch_seconds_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_acrobat_hide_watch_seconds("abc"), 10)
        self.assertEqual(voucher_settings.normalize_acrobat_hide_watch_seconds(0), 1)
        self.assertEqual(voucher_settings.normalize_acrobat_hide_watch_seconds(99), 30)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_new_env_default_backend_is_sumatra_when_bundled_present(self) -> None:
        from unittest import mock

        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            with mock.patch(
                "app.voucher_settings.bundled_sumatra_available", return_value=True
            ):
                loaded = voucher_settings.load_voucher_printer_settings(store)
        self.assertEqual(loaded.print_backend, "sumatra")

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_new_env_default_backend_is_sumatra_without_bundled(self) -> None:
        from unittest import mock

        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            with mock.patch(
                "app.voucher_settings.bundled_sumatra_available", return_value=False
            ):
                loaded = voucher_settings.load_voucher_printer_settings(store)
        self.assertEqual(loaded.print_backend, "sumatra")

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_existing_saved_backend_is_respected_over_bundled_default(self) -> None:
        from unittest import mock

        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "s.ini"
            store = QSettings(str(path), QSettings.Format.IniFormat)
            store.setValue(voucher_settings.VOUCHER_PRINT_BACKEND, "acrobat")
            store.sync()
            # 同梱版があっても、保存済み backend（acrobat）を尊重する。
            with mock.patch(
                "app.voucher_settings.bundled_sumatra_available", return_value=True
            ):
                loaded = voucher_settings.load_voucher_printer_settings(
                    QSettings(str(path), QSettings.Format.IniFormat)
                )
        self.assertEqual(loaded.print_backend, "acrobat")

    def test_print_backend_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_print_backend("acrobat"), "acrobat")
        self.assertEqual(voucher_settings.normalize_print_backend("qt"), "qt")
        self.assertEqual(voucher_settings.normalize_print_backend("sumatra"), "sumatra")
        self.assertEqual(voucher_settings.normalize_print_backend("bad"), "acrobat")

    def test_sumatra_print_settings_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(
            voucher_settings.normalize_sumatra_print_settings(""),
            "noscale,monochrome,paper=auto,bin=auto,center",
        )
        self.assertEqual(
            voucher_settings.normalize_sumatra_print_settings("noscale,center"), "noscale,center"
        )

    def test_sumatra_print_settings_presets_include_fit_shrink_and_paperkind(self) -> None:
        from app import voucher_settings

        preset_values = [value for _label, value in voucher_settings.SUMATRA_PRINT_SETTINGS_PRESETS]
        self.assertIn("noscale,monochrome,paper=auto,bin=auto,center", preset_values)
        self.assertIn("fit,monochrome,paper=auto,bin=auto,center", preset_values)
        self.assertIn("shrink,monochrome,paper=auto,bin=auto,center", preset_values)
        self.assertIn("noscale,monochrome,paperkind=<B5の番号>,bin=auto,center", preset_values)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_print_backend_default_source_reports_saved_or_default(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "s.ini"
            store = QSettings(str(path), QSettings.Format.IniFormat)
            self.assertEqual(voucher_settings.print_backend_default_source(store), "default_sumatra")
            store.setValue(voucher_settings.VOUCHER_PRINT_BACKEND, "acrobat")
            self.assertEqual(voucher_settings.print_backend_default_source(store), "saved")

    def test_sumatra_paperkind_normalization(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.normalize_sumatra_paperkind("13"), "13")
        self.assertEqual(voucher_settings.normalize_sumatra_paperkind(""), "")
        self.assertEqual(voucher_settings.normalize_sumatra_paperkind("abc"), "")
        self.assertEqual(voucher_settings.normalize_sumatra_paperkind("0"), "")

    def test_parse_print_types_filters_unknown(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.parse_print_types("99,03,01"), ["01", "03"])

    # ── 印刷時にPDFも作成する（save_pdf_on_print）──────────────────────────
    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_save_pdf_on_print_default_is_off(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            loaded = voucher_settings.load_voucher_printer_settings(store)
        self.assertFalse(loaded.save_pdf_on_print)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_save_pdf_on_print_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "s.ini"
            voucher_settings.save_voucher_printer_settings(
                voucher_settings.VoucherPrinterSettings(save_pdf_on_print=True), QSettings(str(path), QSettings.Format.IniFormat)
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertTrue(loaded.save_pdf_on_print)

    # ── 新規環境の既定値（印刷方式・プリセット・印刷補正）─────────────────
    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_new_env_print_adjustment_defaults(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            store = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            loaded = voucher_settings.load_voucher_printer_settings(store)
        # 印刷方式=sumatra、印刷補正=ON、左右4mm/上3mm/下1.5mm。
        self.assertEqual(loaded.print_backend, "sumatra")
        self.assertTrue(loaded.print_adjustment_enabled)
        self.assertAlmostEqual(loaded.print_adjustment_margin_left_mm, 4.0)
        self.assertAlmostEqual(loaded.print_adjustment_margin_right_mm, 4.0)
        self.assertAlmostEqual(loaded.print_adjustment_margin_top_mm, 3.0)
        self.assertAlmostEqual(loaded.print_adjustment_margin_bottom_mm, 1.5)

    def test_default_sumatra_preset_is_default_label(self) -> None:
        from app import voucher_settings

        self.assertEqual(voucher_settings.DEFAULT_SUMATRA_PRESET, "既定")
        # 「既定」プリセットが SUMATRA_PRINT_SETTINGS_PRESETS の先頭に存在する。
        first_label = voucher_settings.SUMATRA_PRINT_SETTINGS_PRESETS[0][0]
        self.assertEqual(first_label, "既定")

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_existing_saved_adjustment_is_respected(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "s.ini"
            # 既存ユーザーが印刷補正OFF・余白0で保存済みなら、その値を尊重する。
            voucher_settings.save_voucher_printer_settings(
                voucher_settings.VoucherPrinterSettings(
                    print_adjustment_enabled=False,
                    print_adjustment_margin_left_mm=0.0,
                    print_adjustment_margin_right_mm=0.0,
                    print_adjustment_margin_top_mm=0.0,
                    print_adjustment_margin_bottom_mm=0.0,
                ),
                QSettings(str(path), QSettings.Format.IniFormat),
            )
            loaded = voucher_settings.load_voucher_printer_settings(
                QSettings(str(path), QSettings.Format.IniFormat)
            )
        self.assertFalse(loaded.print_adjustment_enabled)
        self.assertAlmostEqual(loaded.print_adjustment_margin_left_mm, 0.0)

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_visible_column_keys_default_is_none(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(str(Path(temp_dir) / "s.ini"), QSettings.Format.IniFormat)
            self.assertIsNone(voucher_settings.load_visible_column_keys(settings))

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_visible_column_keys_roundtrip(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "s.ini")
            voucher_settings.save_visible_column_keys(
                ["select", "order_no", "olap"],
                QSettings(path, QSettings.Format.IniFormat),
            )
            loaded = voucher_settings.load_visible_column_keys(
                QSettings(path, QSettings.Format.IniFormat)
            )
        self.assertEqual(loaded, ["select", "order_no", "olap"])

    @unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
    def test_visible_column_keys_ignores_broken_json(self) -> None:
        from app import voucher_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "s.ini")
            store = QSettings(path, QSettings.Format.IniFormat)
            store.setValue(voucher_settings.VOUCHER_VISIBLE_COLUMNS_KEY, "{not json")
            store.sync()
            self.assertIsNone(
                voucher_settings.load_visible_column_keys(
                    QSettings(path, QSettings.Format.IniFormat)
                )
            )


if __name__ == "__main__":
    unittest.main()
