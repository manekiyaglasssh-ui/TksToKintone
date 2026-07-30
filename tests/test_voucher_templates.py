from __future__ import annotations

import io
import unittest

import pypdf

from app import voucher_service as vs
from app.voucher_templates import (
    DATA_X_PAD,
    FORM_HDR_RIGHT,
    HDR_AMPM_X,
    HDR_CUSTOMER_VOUCHER_WIDEN,
    HDR_DELIVERY_RIGHT,
    HDR_OPERATOR_X,
    HDR_ORDER_NO_X,
    HDR_ROW1_DIVS,
    HDR_ROW2_DIVS,
    HDR_TRADE_RIGHT,
    HDR_VOUCHER_RIGHT,
    VOUCHER_IDS,
)


class TestVoucherHeaderCellExpansion(unittest.TestCase):
    def test_customer_and_voucher_cells_are_widened_by_10pt(self) -> None:
        self.assertEqual(HDR_CUSTOMER_VOUCHER_WIDEN, 10.0)
        self.assertAlmostEqual(HDR_ORDER_NO_X, 315.0 + 10.0)
        self.assertAlmostEqual(
            vs._customer_max_w(),
            (315.0 - 20.0 - (108.0 + DATA_X_PAD)) + 10.0,
        )
        self.assertAlmostEqual(HDR_VOUCHER_RIGHT, 231.0 + 10.0)
        self.assertAlmostEqual(
            HDR_VOUCHER_RIGHT - HDR_DELIVERY_RIGHT - DATA_X_PAD,
            (231.0 - 171.0 - DATA_X_PAD) + 10.0,
        )

    def test_related_borders_stay_aligned_and_neighbor_widths_are_preserved(self) -> None:
        self.assertEqual(HDR_ROW1_DIVS[1], HDR_ORDER_NO_X)
        self.assertEqual(HDR_ORDER_NO_X, HDR_OPERATOR_X)
        self.assertEqual(HDR_ROW2_DIVS[4], HDR_OPERATOR_X)
        self.assertAlmostEqual(HDR_TRADE_RIGHT - HDR_VOUCHER_RIGHT, 36.0)
        self.assertAlmostEqual(HDR_OPERATOR_X - HDR_TRADE_RIGHT, 48.0)
        self.assertAlmostEqual(HDR_AMPM_X - HDR_OPERATOR_X, 68.0)
        self.assertAlmostEqual(FORM_HDR_RIGHT - HDR_AMPM_X, 59.0)

    def test_long_customer_and_neighbor_fields_render_in_all_vouchers(self) -> None:
        page = {
            "code_no": "I1106",
            "customer_name": "横浜　硝子→東大阪営業部　硝",
            "order_no": "I40186",
            "issue_date": "26/07/09",
            "delivery_date": "26/07/10",
            "voucher_no": "I638018",
            "trade_type": "売上",
            "ship_type": "販PM",
            "operator": "入力者名",
            "details": [{"name": "品名", "qty": "1", "quantity_unit_code": "19"}],
        }
        for voucher_id in VOUCHER_IDS:
            pdf = vs.build_vouchers_pdf_bytes([voucher_id], {"pages": [page]})
            self.assertTrue(pdf.startswith(b"%PDF"), voucher_id)
            text = pypdf.PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
            for expected in (
                page["customer_name"],
                page["trade_type"],
                page["ship_type"],
                page["operator"],
            ):
                self.assertIn(expected, text, f"{voucher_id}: {expected}")
            # 先頭I補正は I と残りを分割描画するため、PDF抽出でも別行になる。
            self.assertIn(page["order_no"][1:], text, voucher_id)
            self.assertIn(page["voucher_no"][1:], text, voucher_id)


if __name__ == "__main__":
    unittest.main()
