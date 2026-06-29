"""中央摘要・物件Noエリアのレイアウト回帰テスト。"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pypdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestVoucherSummaryLayout(unittest.TestCase):
    def _page(self) -> dict:
        from app.voucher_templates import DUMMY_DATA

        return {
            **DUMMY_DATA,
            "summary_line1": "長い摘要" * 8,
            "summary_line2": "摘要二段目",
            "property_lines": ["PROPERTY-1234567890"],
            "sales_rep": "営業担当者名",
            "construction_rep": "工事担当者名",
        }

    def test_staff_labels_are_absent_from_all_voucher_pdfs(self) -> None:
        from app.voucher_service import build_vouchers_pdf_bytes

        for voucher_id in ("01", "02", "03", "04", "05", "06", "07", "08"):
            pdf = build_vouchers_pdf_bytes([voucher_id], self._page())
            text = "\n".join(
                page.extract_text() or ""
                for page in pypdf.PdfReader(io.BytesIO(pdf)).pages
            )
            self.assertNotIn("営業担当：", text, voucher_id)
            self.assertNotIn("工事担当：", text, voucher_id)
            self.assertIn("営業担当者名", text, voucher_id)
            self.assertIn("工事担当者名", text, voucher_id)
            self.assertIn("入力者名", text, voucher_id)
            self.assertIn("まねきや硝子株式会社", text, voucher_id)

    def test_summary_and_property_fonts_are_current_size_times_1_1(self) -> None:
        from app import voucher_service as vs

        self.assertAlmostEqual(
            vs.SUMMARY_TEXT_SCALE,
            vs.SUMMARY_PREVIOUS_TEXT_SCALE * 1.1,
        )
        self.assertAlmostEqual(
            vs.PROPERTY_TEXT_SCALE,
            vs.PROPERTY_PREVIOUS_TEXT_SCALE * 1.1,
        )
        self.assertAlmostEqual(
            vs.SUMMARY_VALUE_FONT_SIZE,
            vs.SUMMARY_VALUE_BASE_FONT_SIZE * vs.SUMMARY_TEXT_SCALE,
        )
        self.assertAlmostEqual(
            vs.PROPERTY_VALUE_FONT_SIZE,
            vs.PROPERTY_VALUE_BASE_FONT_SIZE * vs.PROPERTY_TEXT_SCALE,
        )

    def test_summary_keeps_two_fixed_rows(self) -> None:
        from app import voucher_service as vs

        with patch.object(vs, "_str") as draw:
            vs._draw_summary_lines(MagicMock(), self._page(), vs.SUMMARY_VALUE_FONT_SIZE)

        summary_calls = [
            call for call in draw.call_args_list
            if call.args[1] in {"長い摘要" * 8, "摘要二段目"}
        ]
        self.assertEqual(len(summary_calls), 2)
        self.assertEqual(summary_calls[0].args[3], vs._summary_line_y(0))
        self.assertEqual(summary_calls[1].args[3], vs._summary_line_y(1))

    def test_summary_and_staff_use_separate_areas(self) -> None:
        from app import voucher_service as vs

        note_x = vs.FORM_HDR_LEFT + vs.FORM_SUBROW_LBL_W + 18.0
        self.assertEqual(vs.SUMMARY_TEXT_RIGHT, vs.FORM_SUM_RIGHT)
        self.assertGreater(vs.SUM_STAFF_X, vs.SUMMARY_TEXT_RIGHT)
        self.assertLess(vs.STAFF_TEXT_RIGHT, vs.STAMP_X)
        self.assertGreater(vs.SUMMARY_TEXT_RIGHT - note_x, 0)

    def test_staff_values_align_with_summary_lower_and_property_rows(self) -> None:
        from app import voucher_service as vs

        with patch.object(vs, "_str") as draw:
            vs._draw_staff_values(MagicMock(), self._page())

        calls = {call.args[1]: call for call in draw.call_args_list}
        sales = calls["営業担当者名"]
        construction = calls["工事担当者名"]
        self.assertAlmostEqual(sales.args[2], vs.SUM_STAFF_X + 28.35)
        self.assertAlmostEqual(construction.args[2], vs.SUM_STAFF_X + 28.35)
        self.assertEqual(sales.args[3], vs._summary_line_y(1))
        self.assertEqual(construction.args[3], vs.FORM_BKNO_BOT + 3.0)
        self.assertAlmostEqual(vs.STAFF_TEXT_X - vs.STAFF_PREVIOUS_X, 28.35)
        self.assertGreater(vs.STAFF_TEXT_X, vs.SUMMARY_TEXT_RIGHT)
        self.assertGreater(vs.STAFF_TEXT_RIGHT - vs.STAFF_TEXT_X, 0)


if __name__ == "__main__":
    unittest.main()
