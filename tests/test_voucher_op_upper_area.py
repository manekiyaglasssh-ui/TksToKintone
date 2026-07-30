from __future__ import annotations

import unittest

from app.voucher_data_mapper import (
    _detail_row,
    normalize_op_category,
    resolve_unit_and_amount_values,
    should_draw_upper_area_by_op_category,
)
from app.voucher_service import upper_area_text_for_row
from app.voucher_settings import PRICE_DISPLAY_ALWAYS_SHOW
from tests.test_voucher_invoice_glass import _draw_form, _glass_page


class TestVoucherOpUpperArea(unittest.TestCase):
    def test_normalization_and_allowed_codes(self) -> None:
        for value in ("00", "01", "02", " ０１ "):
            with self.subTest(value=value):
                self.assertTrue(should_draw_upper_area_by_op_category(
                    {"op_category": value}))
        for value in ("03", "10", "", None, 0, 1, 2):
            with self.subTest(value=value):
                self.assertFalse(should_draw_upper_area_by_op_category(
                    {"op_category": value}))
        self.assertEqual(normalize_op_category("０１"), "01")
        self.assertEqual(normalize_op_category("00"), "00")
        self.assertEqual(normalize_op_category(1), "1")

    def test_raw_alias_is_supported_without_numeric_conversion(self) -> None:
        self.assertTrue(should_draw_upper_area_by_op_category(
            {"op_category_raw": "02"}))
        self.assertFalse(should_draw_upper_area_by_op_category(
            {"op_category_raw": 2}))

    def test_mapper_hides_only_upper_area_for_disallowed_op(self) -> None:
        source = {
            "product_name": "品A", "op_type": "03",
            "stat_quantity": "1.234", "ordered_stat_quantity": "2.468",
            "sales_unit_price": "430", "purchase_unit_price": "300",
            "ordered_quantity": "2", "quantity_unit_name": "枚",
        }
        detail = _detail_row(source)
        self.assertEqual(detail["unit_price"], "")
        self.assertEqual(detail["amount"], "")
        self.assertEqual(detail["sales_unit_price"], "430")
        self.assertEqual(detail["ordered_quantity"], "2")
        self.assertEqual(detail["op_category"], "03")
        # 内部の面積解決値は表示制御で変更しない。
        self.assertEqual(resolve_unit_and_amount_values(source), ("1.234", "2.468"))

    def test_common_draw_guard(self) -> None:
        row = {"unit_price": "1.2㎡", "amount": "2.4㎡", "op_category": "03"}
        self.assertEqual(upper_area_text_for_row(row, "unit_price"), "")
        row["op_category"] = "０２"
        self.assertEqual(upper_area_text_for_row(row, "unit_price"), "1.2㎡")

    def test_01_02_07_apply_op_guard_without_hiding_lower_prices(self) -> None:
        for voucher in ("01", "02", "07"):
            for code, expected in (("00", True), ("01", True), ("02", True),
                                   ("03", False), ("10", False), ("", False),
                                   (None, False), ("０１", True)):
                with self.subTest(voucher=voucher, code=code):
                    page = _glass_page(False)
                    page["price_display_mode"] = PRICE_DISPLAY_ALWAYS_SHOW
                    for detail in page["details"]:
                        detail["op_category"] = code
                    texts = _draw_form(page, voucher)
                    self.assertEqual("UA" in texts, expected)
                    self.assertEqual("AA" in texts, expected)
                    self.assertIn("430", texts)
                    self.assertIn("51,600", texts)


if __name__ == "__main__":
    unittest.main()
