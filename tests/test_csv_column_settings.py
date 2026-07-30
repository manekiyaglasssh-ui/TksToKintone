from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.csv_column_settings import (
    SETTINGS_CSV_COLUMNS,
    STANDARD_CSV_COLUMNS,
    CsvColumnSetting,
    default_csv_column_settings,
    enabled_csv_columns,
    load_csv_column_settings,
    reconcile_csv_column_settings,
    save_csv_column_settings,
)
from app.csv_processor import REGISTRATION_EXPORT_HEADERS, export_registration_records_to_csv

try:
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover
    _QT_AVAILABLE = False


class _MemorySettings:
    def __init__(self, value: object = "") -> None:
        self.value_to_return = value
        self.saved: dict[str, object] = {}

    def value(self, key: str, default: object = "") -> object:
        return self.saved.get(key, self.value_to_return if self.value_to_return != "" else default)

    def setValue(self, key: str, value: object) -> None:
        self.saved[key] = value

    def sync(self) -> None:
        pass


class CsvColumnSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_standard_headers_match_existing_csv_order(self) -> None:
        self.assertEqual(
            [column.header for column in STANDARD_CSV_COLUMNS],
            REGISTRATION_EXPORT_HEADERS,
        )

    def test_changed_order_applies_to_header_and_data(self) -> None:
        settings = [CsvColumnSetting("product_name"), CsvColumnSetting("order_no")]
        path = self.dir / "ordered.csv"
        export_registration_records_to_csv(
            [{"受注No": "1000", "商品名称": "強化ガラス"}],
            path,
            enabled_csv_columns(settings),
        )
        with path.open(encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp))
        self.assertEqual(rows[0], ["商品名称", "受注No"])
        self.assertEqual(rows[1], ["強化ガラス", "1000"])

    def test_disabled_column_is_not_exported(self) -> None:
        settings = [
            CsvColumnSetting("order_no", True),
            CsvColumnSetting("customer_name", False),
            CsvColumnSetting("product_name", True),
        ]
        path = self.dir / "hidden.csv"
        export_registration_records_to_csv(
            [{"受注No": "1000", "得意先名称": "A社", "商品名称": "品"}],
            path,
            enabled_csv_columns(settings),
        )
        with path.open(encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp))
        self.assertEqual(rows[0], ["受注No", "商品名称"])
        self.assertEqual(rows[1], ["1000", "品"])
        self.assertNotIn("得意先名称", rows[0])

    def test_missing_new_columns_are_appended_in_standard_order(self) -> None:
        restored = reconcile_csv_column_settings(
            [CsvColumnSetting("product_name", False), CsvColumnSetting("order_no", True)]
        )
        self.assertEqual([item.key for item in restored[:2]], ["product_name", "order_no"])
        self.assertFalse(restored[0].enabled)
        self.assertEqual(restored[-1].key, STANDARD_CSV_COLUMNS[-1].key)
        self.assertEqual(len(restored), len(STANDARD_CSV_COLUMNS))
        appended = restored[2:]
        self.assertTrue(all(item.enabled for item in appended))

    def test_unknown_column_key_is_ignored(self) -> None:
        raw = json.dumps([
            {"key": "unknown_removed_column", "enabled": True},
            {"key": "product_name", "enabled": False},
            {"key": "order_no", "enabled": True},
        ])
        restored = load_csv_column_settings(_MemorySettings(raw))
        keys = [item.key for item in restored]
        self.assertNotIn("unknown_removed_column", keys)
        self.assertEqual(keys[:2], ["product_name", "order_no"])

    def test_corrupt_empty_and_missing_settings_fall_back_to_standard(self) -> None:
        expected = default_csv_column_settings()
        for raw in ("", "not-json", "[]", "{}", '[{"key":"order_no"}]'):
            with self.subTest(raw=raw):
                self.assertEqual(load_csv_column_settings(_MemorySettings(raw)), expected)

    def test_saved_payload_uses_fixed_keys_not_japanese_labels(self) -> None:
        settings = _MemorySettings()
        save_csv_column_settings(settings, [CsvColumnSetting("order_no", True)])
        payload = str(settings.saved[SETTINGS_CSV_COLUMNS])
        self.assertIn('"order_no"', payload)
        self.assertNotIn("受注No", payload)


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class CsvColumnSettingsQtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def test_qsettings_survives_equivalent_restart_reload(self) -> None:
        first = QSettings("Manekiya", "TksToKintone")
        selected = [
            CsvColumnSetting("product_name", True),
            CsvColumnSetting("order_no", True),
            CsvColumnSetting("customer_name", False),
        ]
        save_csv_column_settings(first, selected)

        second = QSettings("Manekiya", "TksToKintone")
        restored = load_csv_column_settings(second)
        self.assertEqual([item.key for item in restored[:3]], ["product_name", "order_no", "customer_name"])
        self.assertFalse(restored[2].enabled)

    def test_reset_restores_standard_order_and_enables_all(self) -> None:
        from app.gui import CsvColumnSettingsDialog

        dialog = CsvColumnSettingsDialog([
            CsvColumnSetting("product_name", False),
            CsvColumnSetting("order_no", True),
        ])
        self.addCleanup(dialog.deleteLater)
        dialog._reset_to_default()
        restored = dialog.column_settings()
        self.assertEqual(restored, default_csv_column_settings())

    def test_up_and_down_buttons_change_order(self) -> None:
        from app.gui import CsvColumnSettingsDialog

        dialog = CsvColumnSettingsDialog(default_csv_column_settings())
        self.addCleanup(dialog.deleteLater)
        dialog.column_list.setCurrentRow(1)
        dialog.up_button.click()
        self.assertEqual(dialog.column_settings()[0].key, STANDARD_CSV_COLUMNS[1].key)
        dialog.down_button.click()
        self.assertEqual(dialog.column_settings()[0].key, STANDARD_CSV_COLUMNS[0].key)

    def test_preview_has_csv_column_settings_button(self) -> None:
        from app.gui import RegistrationPreviewDialog

        dialog = RegistrationPreviewDialog(
            rows=[{"受注No": "1000", "硝/加工": "2", "商品名称": "品"}],
            shukka_options=[],
            master=[],
            customer_labels={},
        )
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.csv_column_settings_button.text(), "CSV列設定")


if __name__ == "__main__":
    unittest.main()
