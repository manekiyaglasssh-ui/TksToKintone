from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from app.voucher_service import (
    _draw_move_slip_columns,
    build_vouchers_pdf_bytes,
    calculate_sales_amount_total_for_move_slip,
    line_amount_text_for_mode,
    resolve_price_amount_visibility,
    unit_price_text_for_mode,
)
from app.voucher_templates import DUMMY_DATA
from tests.test_voucher_invoice_glass import _draw_form, _glass_page
from app.voucher_settings import (
    PRICE_DISPLAY_ALWAYS_HIDE,
    PRICE_DISPLAY_ALWAYS_SHOW,
    PRICE_DISPLAY_CONDITIONAL,
    VOUCHER_PRICE_DISPLAY_MODE_KEY,
    load_price_display_mode,
    save_price_display_mode,
)


class TestVoucherPriceDisplaySettings(unittest.TestCase):
    def settings(self, path: Path):
        return QSettings(str(path), QSettings.Format.IniFormat)

    def test_save_restore_and_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / "settings.ini")
            self.assertEqual(load_price_display_mode(settings), PRICE_DISPLAY_CONDITIONAL)
            save_price_display_mode(PRICE_DISPLAY_ALWAYS_SHOW, settings)
            self.assertEqual(load_price_display_mode(settings), PRICE_DISPLAY_ALWAYS_SHOW)
            settings.setValue(VOUCHER_PRICE_DISPLAY_MODE_KEY, "broken")
            self.assertEqual(load_price_display_mode(settings), PRICE_DISPLAY_CONDITIONAL)

    def test_common_visibility_resolver(self):
        self.assertFalse(resolve_price_amount_visibility(False, PRICE_DISPLAY_CONDITIONAL))
        self.assertTrue(resolve_price_amount_visibility(True, PRICE_DISPLAY_CONDITIONAL))
        self.assertTrue(resolve_price_amount_visibility(False, PRICE_DISPLAY_ALWAYS_SHOW))
        self.assertFalse(resolve_price_amount_visibility(True, PRICE_DISPLAY_ALWAYS_HIDE))

    def test_upper_unit_and_amount_share_mode_and_empty_is_not_fabricated(self):
        self.assertEqual(unit_price_text_for_mode({"price_display_mode": PRICE_DISPLAY_CONDITIONAL}, "123"), "123")
        self.assertEqual(line_amount_text_for_mode({"price_display_mode": PRICE_DISPLAY_CONDITIONAL}, "246"), "246")
        self.assertEqual(unit_price_text_for_mode({"price_display_mode": PRICE_DISPLAY_ALWAYS_SHOW}, ""), "")
        self.assertEqual(line_amount_text_for_mode({"price_display_mode": PRICE_DISPLAY_ALWAYS_SHOW}, ""), "")
        self.assertEqual(unit_price_text_for_mode({"price_display_mode": PRICE_DISPLAY_ALWAYS_HIDE}, "123"), "")
        self.assertEqual(line_amount_text_for_mode({"price_display_mode": PRICE_DISPLAY_ALWAYS_HIDE}, "246"), "")

    def test_all_voucher_01_to_08_generate_with_price_mode(self):
        data = dict(DUMMY_DATA)
        data["price_display_mode"] = PRICE_DISPLAY_ALWAYS_HIDE
        for number in range(1, 9):
            pdf = build_vouchers_pdf_bytes([f"{number:02d}"], data)
            self.assertTrue(pdf.startswith(b"%PDF"), f"voucher {number:02d}")

    def test_01_02_07_always_show_draws_all_three_price_amount_parts(self):
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                page = _glass_page(False)
                page["price_display_mode"] = PRICE_DISPLAY_ALWAYS_SHOW
                texts = _draw_form(page, voucher)
                self.assertIn("UA", texts)       # 上段単価（既存マッピング値）
                self.assertIn("AA", texts)       # 上段明細金額（既存マッピング値）
                self.assertIn("430", texts)      # 条件外でも下段単価
                self.assertIn("51,600", texts)   # 条件外でも下段明細金額
                self.assertGreaterEqual(texts.count("100,800"), 2)  # 金額列下部合計を含む

    def test_01_02_07_always_hide_hides_unit_line_amount_and_amount_column_total(self):
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                page = _glass_page(True)
                page["price_display_mode"] = PRICE_DISPLAY_ALWAYS_HIDE
                texts = _draw_form(page, voucher)
                self.assertNotIn("UA", texts)
                self.assertNotIn("AA", texts)
                self.assertNotIn("430", texts)
                self.assertNotIn("51,600", texts)

    def test_01_02_07_conditional_keeps_existing_condition(self):
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                hidden = _glass_page(False)
                hidden["price_display_mode"] = PRICE_DISPLAY_CONDITIONAL
                hidden_texts = _draw_form(hidden, voucher)
                self.assertIn("UA", hidden_texts)
                self.assertIn("AA", hidden_texts)
                self.assertNotIn("430", hidden_texts)
                self.assertNotIn("51,600", hidden_texts)
                shown = _glass_page(True)
                shown["price_display_mode"] = PRICE_DISPLAY_CONDITIONAL
                shown_texts = _draw_form(shown, voucher)
                self.assertIn("430", shown_texts)
                self.assertIn("51,600", shown_texts)

    @mock.patch("app.voucher_service._rstr")
    def test_forced_show_adds_lower_unit_line_amount_and_total(self, draw):
        data = {
            "price_display_mode": PRICE_DISPLAY_ALWAYS_SHOW,
            "invoice_price_amount_lower_glass_enabled": False,
            "details": [{"name": "A", "sales_unit_price": "123", "ordered_quantity": "2"}],
        }
        _draw_move_slip_columns(mock.Mock(), data, 100, 200)
        self.assertEqual([call.args[1] for call in draw.call_args_list], ["123", "246", "246"])

    @mock.patch("app.voucher_service._rstr")
    def test_forced_hide_hides_lower_unit_line_amount_and_total(self, draw):
        data = {
            "price_display_mode": PRICE_DISPLAY_ALWAYS_HIDE,
            "invoice_price_amount_lower_glass_enabled": True,
            "details": [{"name": "A", "sales_unit_price": "123", "ordered_quantity": "2"}],
        }
        _draw_move_slip_columns(mock.Mock(), data, 100, 200)
        draw.assert_not_called()
        self.assertEqual(calculate_sales_amount_total_for_move_slip(data["details"]), 246.0)

    @mock.patch("app.voucher_service._rstr")
    def test_conditional_preserves_existing_lower_condition(self, draw):
        hidden = {
            "price_display_mode": PRICE_DISPLAY_CONDITIONAL,
            "invoice_price_amount_lower_glass_enabled": False,
            "details": [{"name": "A", "sales_unit_price": "123", "ordered_quantity": "2"}],
        }
        _draw_move_slip_columns(mock.Mock(), hidden, 100, 200)
        draw.assert_not_called()
        shown = dict(hidden)
        shown.pop("_resolved_price_display_mode", None)
        shown["invoice_price_amount_lower_glass_enabled"] = True
        _draw_move_slip_columns(mock.Mock(), shown, 100, 200)
        self.assertEqual([call.args[1] for call in draw.call_args_list], ["123", "246", "246"])

    @mock.patch("app.voucher_service._rstr")
    def test_multiple_lines_total_and_unit_code_19_use_internal_quantity(self, draw):
        data = {
            "price_display_mode": PRICE_DISPLAY_ALWAYS_SHOW,
            "invoice_price_amount_lower_glass_enabled": False,
            "details": [
                {"name": "A", "sales_unit_price": "9,660", "ordered_quantity": "1", "quantity_unit_code": "19", "qty": ""},
                {"name": "B", "sales_unit_price": "100.25", "ordered_quantity": "2"},
            ],
        }
        self.assertEqual(calculate_sales_amount_total_for_move_slip(data["details"]), 9860.5)
        _draw_move_slip_columns(mock.Mock(), data, 100, 200)
        values = [call.args[1] for call in draw.call_args_list]
        self.assertEqual(values, ["9,660", "9,660", "100.25", "200.50", "9,860.50"])

    @mock.patch("app.voucher_service._rstr")
    def test_empty_unit_or_quantity_does_not_create_zero(self, draw):
        data = {
            "price_display_mode": PRICE_DISPLAY_ALWAYS_SHOW,
            "details": [
                {"name": "A", "sales_unit_price": "", "ordered_quantity": "2"},
                {"name": "B", "sales_unit_price": "100", "ordered_quantity": ""},
            ],
        }
        _draw_move_slip_columns(mock.Mock(), data, 100, 200)
        self.assertEqual([call.args[1] for call in draw.call_args_list], ["100"])


if __name__ == "__main__":
    unittest.main()
