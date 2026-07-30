"""納入先住所1・2のPDF表示用連結テスト。"""
from __future__ import annotations

import io
import unittest
from datetime import date

import pypdf

from app.voucher_data_mapper import build_voucher_pages, combine_delivery_address


class DeliveryAddressCombineTest(unittest.TestCase):
    def test_combine_rules(self) -> None:
        cases = [
            ((" 京都市上京区 ", " ○○ビル3F　"), "京都市上京区○○ビル3F"),
            (("京都市上京区", ""), "京都市上京区"),
            (("", "○○ビル3F"), "○○ビル3F"),
            ((None, None), ""),
            (("-", "－"), ""),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(combine_delivery_address(*args), expected)

    def test_page_keeps_original_addresses_and_combined_display_value(self) -> None:
        rows = [{
            "6": "X001", "9": "V001", "16": "商品A",
            "delivery_address1": "京都市上京区",
            "delivery_address2": "○○ビル3F",
        }]
        page = build_voucher_pages(rows, today=date(2026, 7, 15))[0]
        self.assertEqual(page["delivery_address1"], "京都市上京区")
        self.assertEqual(page["delivery_address2"], "○○ビル3F")
        self.assertEqual(page["delivery_address_combined"], "京都市上京区○○ビル3F")
        self.assertEqual(page["summary_line1"], "京都市上京区○○ビル3F")

    def test_address2_only_moves_to_address1_display_position(self) -> None:
        rows = [{"6": "X001", "9": "V001", "16": "商品A", "delivery_address2": "○○ビル3F"}]
        page = build_voucher_pages(rows, today=date(2026, 7, 15))[0]
        self.assertEqual(page["summary_line1"], "○○ビル3F")


class DeliveryAddressPdfTest(unittest.TestCase):
    def test_combined_address_is_drawn_once_on_all_vouchers(self) -> None:
        from app.voucher_service import build_vouchers_pdf_bytes
        from app.voucher_templates import DUMMY_DATA

        page = {
            **DUMMY_DATA,
            "summary_line1": "京都市上京区○○ビル3F",
            "delivery_address1": "京都市上京区",
            "delivery_address2": "○○ビル3F",
            "delivery_address_combined": "京都市上京区○○ビル3F",
        }
        for voucher_id in ("01", "02", "03", "04", "05", "06", "07", "08"):
            with self.subTest(voucher_id=voucher_id):
                pdf = build_vouchers_pdf_bytes([voucher_id], page)
                text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
                self.assertEqual(text.count("京都市上京区○○ビル3F"), 2)  # 擬似太字の2回描画
                self.assertNotIn("○○ビル3F\n○○ビル3F", text)

    def test_separate_address2_draw_function_is_removed(self) -> None:
        from app import voucher_service
        self.assertFalse(hasattr(voucher_service, "_draw_delivery_address2"))


if __name__ == "__main__":
    unittest.main()
