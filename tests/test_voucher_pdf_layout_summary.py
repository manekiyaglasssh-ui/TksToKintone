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
            "delivery_course_name": "大阪南コース",
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
            self.assertIn("大阪南コース", text, voucher_id)
            self.assertIn("工事担当者名", text, voucher_id)
            self.assertIn("入力者名", text, voucher_id)
            self.assertIn("まねきや硝子株式会社", text, voucher_id)

    def test_second_detail_delivery_course_reaches_pdf(self) -> None:
        from datetime import date
        from app.voucher_data_mapper import build_voucher_pages, extract_r1_rows
        from app.voucher_service import build_vouchers_pdf_bytes

        response = {"ResponseData": {"R1List": [
            {"6": "1405113", "7": "1", "9": "Z001", "16": "商品1", "48": "01", "49": ""},
            {"6": "1405113", "7": "2", "9": "Z001", "16": "商品2", "48": "01", "49": "大阪南コース"},
        ]}}
        pages = build_voucher_pages(
            extract_r1_rows(response), today=date(2026, 6, 5)
        )
        pdf = build_vouchers_pdf_bytes(["01"], {"pages": pages})
        text = "\n".join(
            page.extract_text() or ""
            for page in pypdf.PdfReader(io.BytesIO(pdf)).pages
        )
        self.assertEqual(pages[0]["delivery_course_name"], "大阪南コース")
        self.assertIn("大阪南コース", text)

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

        with patch.object(vs, "_str") as draw, \
                patch.object(vs, "draw_text_fit_width") as fit:
            vs._draw_summary_lines(MagicMock(), self._page(), vs.SUMMARY_VALUE_FONT_SIZE)

        calls = {call.args[1]: call for call in fit.call_args_list}
        self.assertEqual(calls["長い摘要" * 8].args[3], vs._summary_line_y(0))
        self.assertEqual(calls["摘要二段目"].args[3], vs._summary_line_y(1))
        draw.assert_not_called()

    def test_summary_and_staff_use_separate_areas(self) -> None:
        from app import voucher_service as vs

        note_x = vs.FORM_HDR_LEFT + vs.FORM_SUBROW_LBL_W + 18.0
        self.assertEqual(vs.SUMMARY_TEXT_RIGHT, vs.FORM_SUM_RIGHT)
        self.assertGreater(vs.SUM_STAFF_X, vs.SUMMARY_TEXT_RIGHT)
        self.assertLess(vs.STAFF_TEXT_RIGHT, vs.STAMP_X)
        self.assertGreater(vs.SUMMARY_TEXT_RIGHT - note_x, 0)

    def test_staff_values_align_with_summary_lower_and_property_rows(self) -> None:
        from app import voucher_service as vs

        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(vs, "_str") as draw, \
                patch.object(vs, "draw_text_fit_width_right") as fit:
            vs._draw_staff_values(canvas, self._page())

        calls = {call.args[1]: call for call in draw.call_args_list}
        construction = calls["工事担当者名"]
        combined = fit.call_args
        self.assertEqual(combined.args[1], "大阪南コース 営業担当者名")
        self.assertEqual(combined.args[3], vs._summary_line_y(1))
        self.assertAlmostEqual(construction.args[2], vs.SUM_STAFF_X + 28.35)
        self.assertEqual(construction.args[3], vs.FORM_BKNO_BOT + 3.0)
        self.assertAlmostEqual(vs.STAFF_TEXT_X - vs.STAFF_PREVIOUS_X, 28.35)
        self.assertGreater(vs.STAFF_TEXT_X, vs.SUMMARY_TEXT_RIGHT)
        self.assertGreater(vs.STAFF_TEXT_RIGHT - vs.STAFF_TEXT_X, 0)

    def test_delivery_course_and_sales_rep_are_one_right_aligned_string(self) -> None:
        from app import voucher_service as vs

        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(vs, "draw_text_fit_width_right", return_value=vs.DETAIL_DATA_FONT_SIZE) as fit, \
                patch.object(vs, "_str") as draw:
            vs._draw_staff_values(canvas, self._page())

        combined = fit.call_args
        self.assertEqual(combined.args[1], "大阪南コース 営業担当者名")
        self.assertEqual(combined.args[3], vs.SALES_REP_Y)
        self.assertFalse(any(call.args[1] == "営業担当者名" for call in draw.call_args_list))
        self.assertEqual(combined.args[6], vs.DETAIL_DATA_FONT_SIZE)

    def test_blank_delivery_course_is_not_drawn(self) -> None:
        from app import voucher_service as vs

        page = {**self._page(), "delivery_course_name": "　 "}
        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(vs, "draw_text_fit_width_right") as fit, \
                patch.object(vs, "_str"):
            vs._draw_staff_values(canvas, page)
        fit.assert_not_called()

    def test_delivery_course_draw_logs_actual_geometry_and_font_size(self) -> None:
        from app import voucher_service as vs

        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(
            vs, "draw_text_fit_width_right", return_value=7.25
        ), patch.object(vs, "_str"), \
                self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
            vs._draw_staff_values(canvas, self._page())

        text = "\n".join(logs.output)
        self.assertIn("voucher_delivery_course_draw_requested", text)
        self.assertIn("voucher_delivery_course_staff_combined_drawn", text)
        self.assertIn("order_no=", text)
        self.assertIn("voucher_no=", text)
        self.assertIn(f"x={vs.DELIVERY_COURSE_X}", text)
        self.assertIn(f"y={vs.SALES_REP_Y}", text)
        self.assertIn("combined_text='大阪南コース 営業担当者名'", text)
        self.assertIn("font_size=7.25", text)

    def test_blank_delivery_course_logs_skip_reason(self) -> None:
        from app import voucher_service as vs

        page = {**self._page(), "delivery_course_name": "－"}
        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(vs, "_str"), \
                self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
            vs._draw_staff_values(canvas, page)
        self.assertIn("voucher_delivery_course_draw_skipped", "\n".join(logs.output))
        self.assertIn("reason=blank", "\n".join(logs.output))

    def test_long_delivery_course_shrinks_combined_text(self) -> None:
        from app import voucher_service as vs

        page = {**self._page(), "delivery_course_name": "非常に長い配送コース名称" * 5}
        canvas = MagicMock()
        canvas.stringWidth.return_value = 40.0
        with patch.object(vs, "draw_text_fit_width_right", return_value=2.5) as fit, \
                patch.object(vs, "_str") as draw:
            vs._draw_staff_values(canvas, page)
        combined = fit.call_args
        self.assertTrue(combined.args[1].endswith(" 営業担当者名"))
        self.assertGreater(combined.args[4], 0)
        self.assertFalse(any(call.args[1] == "営業担当者名" for call in draw.call_args_list))


if __name__ == "__main__":
    unittest.main()
