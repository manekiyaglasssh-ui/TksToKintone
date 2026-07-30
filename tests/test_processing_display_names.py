from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from app.processing_display_names import (
    DEFAULT_PROCESSING_DISPLAY_NAMES,
    PROCESSING_DEFINITIONS,
    SETTINGS_KEY,
    load_processing_display_names,
    processing_name_display_width,
    resolve_processing_display_name,
    save_processing_display_names,
    validate_processing_display_name,
)


class TestProcessingDisplayNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self._tmp.name) / "settings.ini"), QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _defaults(self) -> dict[str, str]:
        return dict(PROCESSING_DEFINITIONS)

    def test_display_width_rules(self) -> None:
        self.assertEqual(processing_name_display_width("あいうえおか"), 12)
        self.assertEqual(processing_name_display_width("ABCDEF123456"), 12)
        self.assertEqual(processing_name_display_width("ＡＢＣ１２３"), 12)
        self.assertEqual(processing_name_display_width("AあB"), 4)
        self.assertEqual(processing_name_display_width("e\u0301"), 2)  # NFC: é はA幅

    def test_length_validation(self) -> None:
        for allowed in ("あいうえおか", "ABCDEF123456", "ＡＢＣ１２３", "DM-10"):
            self.assertEqual(validate_processing_display_name(allowed), allowed)
        for rejected in ("あいうえおかき", "ABCDEFGHIJKLM"):
            with self.assertRaises(ValueError):
                validate_processing_display_name(rejected)

    def test_empty_is_allowed_and_control_characters_are_rejected(self) -> None:
        self.assertEqual(validate_processing_display_name(""), "")
        self.assertEqual(validate_processing_display_name("  "), "")
        for rejected in ("a\nb", "a\tb", "a\u0001b"):
            with self.assertRaises(ValueError):
                validate_processing_display_name(rejected)

    def test_save_reload_and_resolve_keep_stable_key(self) -> None:
        values = self._defaults()
        values["edging"] = "端加工"
        save_processing_display_names(values, self.settings)
        loaded = load_processing_display_names(self.settings)
        self.assertEqual(loaded["edging"], "端加工")
        self.assertEqual(resolve_processing_display_name("エッジング", loaded), "端加工")
        self.assertIn("edging", json.loads(self.settings.value(SETTINGS_KEY)))

    def test_twelve_lines_keep_existing_key_order(self) -> None:
        self.assertEqual(
            tuple(key for key, _default in PROCESSING_DEFINITIONS),
            (
                "edging", "wide", "factory_cut", "hand_processing", "dm_10",
                "pull_handle", "multi", "cleaning", "bob", "printing",
                "film_lamination", "rounding",
            ),
        )

    def test_blank_names_save_reload_and_resolve_as_blank(self) -> None:
        values = self._defaults()
        values["edging"] = ""
        save_processing_display_names(values, self.settings)
        loaded = load_processing_display_names(self.settings)
        self.assertEqual(loaded["edging"], "")
        self.assertEqual(resolve_processing_display_name("エッジング", loaded), "")

    def test_saved_line_13_and_unknown_keys_are_ignored(self) -> None:
        legacy = self._defaults()
        legacy.update({"wide": "幅広", "line_13": "旧予備", "unknown": "不明"})
        self.settings.setValue(
            SETTINGS_KEY, json.dumps(legacy, ensure_ascii=False))
        loaded = load_processing_display_names(self.settings)
        self.assertEqual(loaded["wide"], "幅広")
        self.assertEqual(set(loaded), set(DEFAULT_PROCESSING_DISPLAY_NAMES))
        self.assertNotIn("line_13", loaded)
        self.assertNotIn("unknown", loaded)

    def test_save_after_legacy_setting_writes_only_twelve_official_keys(self) -> None:
        legacy = {**self._defaults(), "line_13": "旧予備", "unknown": "不明"}
        self.settings.setValue(
            SETTINGS_KEY, json.dumps(legacy, ensure_ascii=False))
        save_processing_display_names(
            load_processing_display_names(self.settings), self.settings)
        saved = json.loads(self.settings.value(SETTINGS_KEY))
        self.assertEqual(set(saved), set(DEFAULT_PROCESSING_DISPLAY_NAMES))
        self.assertEqual(len(saved), 12)

    def test_corrupt_item_falls_back_individually(self) -> None:
        self.settings.setValue(
            SETTINGS_KEY,
            json.dumps({"edging": "1234567890123", "wide": "幅広"},
                       ensure_ascii=False))
        loaded = load_processing_display_names(self.settings)
        self.assertEqual(loaded["edging"], DEFAULT_PROCESSING_DISPLAY_NAMES["edging"])
        self.assertEqual(loaded["wide"], "幅広")

    def test_settings_widget_uses_fixed_line_labels_and_twelve_inputs(self) -> None:
        from unittest import mock

        from app.voucher_window import ProcessingDisplayNamesWidget

        with mock.patch(
            "app.voucher_window.load_processing_display_names",
            return_value=self._defaults(),
        ):
            widget = ProcessingDisplayNamesWidget()
        self.addCleanup(widget.deleteLater)
        labels = [label.text() for label in widget.findChildren(QLabel)]
        self.assertIn("左の行番号は固定です。右の伝票表示名だけ変更できます。", labels)
        self.assertIn("行", labels)
        self.assertIn("伝票表示名", labels)
        for row in range(1, 13):
            self.assertIn(f"{row}行目", labels)
        self.assertNotIn("13行目", labels)
        for _key, processing_name in PROCESSING_DEFINITIONS[:12]:
            self.assertNotIn(processing_name, labels)
        edits = widget.findChildren(QLineEdit)
        self.assertEqual(len(edits), 12)
        self.assertEqual(
            [widget._edits[key].text() for key, _default in PROCESSING_DEFINITIONS],
            [default for _key, default in PROCESSING_DEFINITIONS],
        )

    def test_widget_reset_restores_twelve_default_names(self) -> None:
        from unittest import mock

        from app.voucher_window import ProcessingDisplayNamesWidget

        current = self._defaults()
        current["edging"] = "端加工"
        with mock.patch(
            "app.voucher_window.load_processing_display_names",
            return_value=current,
        ):
            widget = ProcessingDisplayNamesWidget()
        self.addCleanup(widget.deleteLater)
        widget.reset_defaults()
        self.assertEqual(widget.values(), self._defaults())

    def test_only_twelve_configured_names_are_drawn_on_processing_forms(self) -> None:
        import pypdf
        from unittest import mock

        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA

        values = self._defaults()
        values["edging"] = "端加工"
        # 保存済み旧設定に残っていても13枠目へは描画しない。
        values["line_13"] = "旧予備"
        with mock.patch(
            "app.voucher_service.load_processing_display_names",
            return_value=values,
        ):
            for voucher_id in ("01", "02", "03", "04", "05", "06"):
                pdf = build_vouchers_pdf_bytes([voucher_id], DUMMY_DATA)
                text = "".join(
                    page.extract_text() or ""
                    for page in pypdf.PdfReader(io.BytesIO(pdf)).pages
                )
                self.assertIn("端加工", text, voucher_id)
                self.assertNotIn("旧予備", text, voucher_id)
