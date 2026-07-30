"""得意先マスタ「納品書単価・金額下段（硝子）」対応のテスト。

得意先マスタからのOLAP取得、伝票データへの正規化（bool化）、および下段値が 1 の
ときに通常伝票（売上伝票01・工場控02・納品書07）で単価・金額列の下段と
合計行下段が表示されることを検証する。移動伝票ラベル条件とは独立していることも確認する。
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.models import AppConfig, AppPaths
from app.voucher_data_mapper import (
    build_voucher_pages,
    normalize_invoice_price_amount_lower_glass,
    normalize_invoice_price_amount_upper_glass,
)
from app.voucher_olap_service import (
    VoucherOlapService,
    build_transaction_type_payload,
    parse_invoice_price_amount_lower_glass,
    parse_invoice_price_amount_upper_glass,
)
from app import voucher_service as vs

from tests.test_voucher_move_slip import (
    _CountingSession,
    _RecordingCanvas,
    _service,
)


_GLASS_DETAILS = [
    {"name": "品A", "unit_price": "UA", "amount": "AA", "op_category": "00",
     "sales_unit_price": "430", "ordered_quantity": "120"},
    {"name": "品B", "unit_price": "UB", "amount": "AB", "op_category": "01",
     "sales_unit_price": "250", "ordered_quantity": "120"},
    {"name": "品C", "unit_price": "UC", "amount": "AC", "op_category": "02",
     "sales_unit_price": "40", "ordered_quantity": "480"},
    {"name": "*", "unit_price": "", "amount": "",
     "sales_unit_price": "999", "ordered_quantity": "999"},
]


def _glass_page(enabled: bool, transaction_type: str = "1", upper_enabled: bool = False):
    """硝子（納品書単価・金額下段）表示テスト用ページ（正規化済みbool版）。"""
    return {
        "code_no": "9991173",
        "customer_name": "テスト得意先",
        "order_no": "",  # QR を発火させない
        "construction_rep": "工事担当太郎",
        "transaction_type": transaction_type,
        "invoice_price_amount_upper_glass_enabled": upper_enabled,
        "invoice_price_amount_lower_glass_enabled": enabled,
        "details": [dict(row) for row in _GLASS_DETAILS],
    }


def _glass_page_raw(lower_raw, transaction_type: str = "1", upper_raw="0"):
    """正規化済みbool を持たず、生値のみのページ。

    下段表示判定は invoice_price_amount_lower_glass のみを見る。
    """
    return {
        "code_no": "9991173",
        "customer_name": "テスト得意先",
        "order_no": "",
        "construction_rep": "工事担当太郎",
        "transaction_type": transaction_type,
        "invoice_price_amount_upper_glass": upper_raw,
        "invoice_price_amount_lower_glass": lower_raw,
        "details": [dict(row) for row in _GLASS_DETAILS],
    }


_GLASS_SAMPLE = {
    "ResponseData": {
        "R1List": [
            {"1": "9991173", "2": "1", "3": "9", "4": "1"},
        ]
    }
}


def _draw_form(page, voucher) -> list[str]:
    c = _RecordingCanvas()
    if voucher == "01":
        vs._draw_form_01(c, page, "売　上　伝　票")
    elif voucher == "02":
        vs._draw_form_01(c, page, "工　場　控")
    elif voucher == "07":
        vs._draw_form_07(c, page)
    elif voucher == "08":
        vs._draw_form_08(c, page)
    return c.texts


class NormalizeGlassTest(unittest.TestCase):
    def test_upper_true_only_for_one(self) -> None:
        # 「1」だけ True（str "1" / int 1）。
        self.assertTrue(normalize_invoice_price_amount_upper_glass("1"))
        self.assertTrue(normalize_invoice_price_amount_upper_glass(1))

    def test_upper_false_for_empty_zero_and_other(self) -> None:
        # "0" / 0 / 空 / None / 未取得 / "false" / "2" / その他はすべて False。
        for value in ("", "0", "2", None, "abc", 0, "false", "False", "１"):
            with self.subTest(value=value):
                self.assertFalse(normalize_invoice_price_amount_upper_glass(value))

    def test_lower_true_only_for_one(self) -> None:
        self.assertTrue(normalize_invoice_price_amount_lower_glass("1"))
        self.assertTrue(normalize_invoice_price_amount_lower_glass(1))

    def test_lower_false_for_empty_zero_nine_and_other(self) -> None:
        for value in ("", "0", "2", "9", None, "abc", 0, 9, "false", "False", "１"):
            with self.subTest(value=value):
                self.assertFalse(normalize_invoice_price_amount_lower_glass(value))


class GlassOlapFetchTest(unittest.TestCase):
    def test_payload_includes_upper_and_lower_glass_columns(self) -> None:
        payload = build_transaction_type_payload("9991173")
        names = [c["OLAP表示名"] for c in payload["R1List"]]
        self.assertIn("納品書単価・金額上段（硝子）", names)
        self.assertIn("納品書単価・金額下段（硝子）", names)

    def test_parse_glass_values(self) -> None:
        self.assertEqual(parse_invoice_price_amount_upper_glass(_GLASS_SAMPLE), "9")
        self.assertEqual(parse_invoice_price_amount_lower_glass(_GLASS_SAMPLE), "1")
        self.assertEqual(parse_invoice_price_amount_upper_glass({}), "")
        self.assertEqual(parse_invoice_price_amount_lower_glass({}), "")
        self.assertEqual(
            parse_invoice_price_amount_upper_glass({"ResponseData": {"R1List": {}}}), ""
        )
        self.assertEqual(
            parse_invoice_price_amount_lower_glass({"ResponseData": {"R1List": {}}}), ""
        )

    def test_enrich_sets_glass_and_page_enabled(self) -> None:
        service = _service(_CountingSession(_GLASS_SAMPLE))
        rows = [{"customer_code": "9991173"}]
        service._enrich_transaction_types(rows)
        self.assertEqual(rows[0]["invoice_price_amount_upper_glass"], "9")
        self.assertEqual(rows[0]["invoice_price_amount_lower_glass"], "1")
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertFalse(pages[0]["invoice_price_amount_upper_glass_enabled"])
        self.assertTrue(pages[0]["invoice_price_amount_lower_glass_enabled"])

    def test_enrich_blank_when_missing(self) -> None:
        service = _service(_CountingSession({"ResponseData": {"R1List": []}}))
        rows = [{"customer_code": "9991173"}]
        service._enrich_transaction_types(rows)
        self.assertEqual(rows[0]["invoice_price_amount_upper_glass"], "")
        self.assertEqual(rows[0]["invoice_price_amount_lower_glass"], "")
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertFalse(pages[0]["invoice_price_amount_upper_glass_enabled"])
        self.assertFalse(pages[0]["invoice_price_amount_lower_glass_enabled"])


class GlassDisplayTest(unittest.TestCase):
    def _capture_lower_decision_log(self, page) -> str:
        with self.assertLogs("tks_to_kintone_app", level=logging.INFO) as captured:
            _draw_form(page, "01")
        return "\n".join(captured.output)

    def test_lower_columns_displayed_when_enabled(self) -> None:
        """有効(True)のとき単価下段・金額下段・合計行下段が表示される。"""
        texts = _draw_form(_glass_page(True), "01")
        self.assertIn("430", texts)          # 単価下段
        self.assertIn("51,600", texts)       # 金額下段 430*120
        self.assertGreaterEqual(texts.count("100,800"), 2)  # 合計行下段

    def test_lower_columns_absent_when_disabled(self) -> None:
        """無効(False)のとき下段は表示されない（移動伝票でもない）。"""
        texts = _draw_form(_glass_page(False), "01")
        self.assertNotIn("430", texts)
        self.assertNotIn("51,600", texts)

    def test_target_vouchers_only(self) -> None:
        """下段表示は売上伝票・工場控・納品書のみ（受領書08には出ない）。"""
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                self.assertIn("51,600", _draw_form(_glass_page(True), voucher))
        self.assertNotIn("51,600", _draw_form(_glass_page(True), "08"))

    def test_move_slip_label_does_not_enable_lower_columns(self) -> None:
        """下段硝子フラグ無効なら移動伝票(取引区分8)でも下段は表示されない。"""
        page = _glass_page(False, transaction_type="8")
        texts = _draw_form(page, "01")
        self.assertNotIn("430", texts)
        self.assertNotIn("51,600", texts)
        self.assertIn("移動伝票", texts)
        self.assertNotIn("移動伝票", _draw_form(_glass_page(True, transaction_type="1"), "01"))

    # ── 生値（正規化前）経路: 下段フィールドだけで表示判定する ──
    def test_raw_one_shows_lower_on_normal_voucher(self) -> None:
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                texts = _draw_form(_glass_page_raw("1"), voucher)
                self.assertIn("430", texts)          # 単価下段
                self.assertIn("51,600", texts)       # 金額下段
                self.assertGreaterEqual(texts.count("100,800"), 2)  # 合計行下段

    def test_raw_non_one_hides_lower_on_normal_voucher(self) -> None:
        # 単価下段(430)・金額下段(51,600)は下段専用の値で、通常の摘要合計欄には出ない。
        for raw in ("0", "", "2", None, "false"):
            for voucher in ("01", "02", "07"):
                with self.subTest(raw=raw, voucher=voucher):
                    texts = _draw_form(_glass_page_raw(raw), voucher)
                    self.assertNotIn("430", texts)       # 単価下段なし
                    self.assertNotIn("51,600", texts)    # 金額下段なし

    def test_total_row_lower_only_when_enabled(self) -> None:
        """合計行下段(Σ=100,800)は下段硝子=1 のときだけ増える（摘要合計欄と区別）。

        100,800 は摘要合計欄にも出るため、有効時に出現回数が1つ増えることで
        金額列「合計」行下段の描画有無を判定する。
        """
        shown = _draw_form(_glass_page_raw("1"), "01").count("100,800")
        hidden = _draw_form(_glass_page_raw("0"), "01").count("100,800")
        self.assertGreater(shown, hidden)

    def test_raw_zero_move_slip_hides_lower(self) -> None:
        """下段硝子=0（生値）なら移動伝票(取引区分8)でも下段は表示されない。"""
        texts = _draw_form(_glass_page_raw("0", transaction_type="8"), "01")
        self.assertNotIn("430", texts)
        self.assertNotIn("51,600", texts)
        self.assertIn("移動伝票", texts)

    def test_lower_decision_log_identifies_invoice_glass_reason(self) -> None:
        page = _glass_page_raw("1", transaction_type="1")
        page.update({"order_no": "5218869", "customer_code": "9991173"})
        logs = self._capture_lower_decision_log(page)
        self.assertIn("order_no='5218869'", logs)
        self.assertIn("customer_code='9991173'", logs)
        self.assertIn("customer_name='テスト得意先'", logs)
        self.assertIn("transaction_type='1'", logs)
        self.assertIn("is_move_slip=False", logs)
        self.assertIn("move_slip_reason=not_transaction_type_8", logs)
        self.assertIn("invoice_price_amount_upper_glass_raw='0'", logs)
        self.assertIn("invoice_price_amount_upper_glass_enabled=False", logs)
        self.assertIn("invoice_price_amount_lower_glass_raw='1'", logs)
        self.assertIn("invoice_price_amount_lower_glass_enabled=True", logs)
        self.assertIn("show_price_amount_lower=True", logs)
        self.assertIn("show_price_amount_lower_reason=invoice_lower_glass_1", logs)

    def test_lower_decision_log_identifies_hidden_reason(self) -> None:
        page = _glass_page_raw("0", transaction_type="1")
        logs = self._capture_lower_decision_log(page)
        self.assertIn("is_move_slip=False", logs)
        self.assertIn("move_slip_reason=not_transaction_type_8", logs)
        self.assertIn("invoice_price_amount_upper_glass_raw='0'", logs)
        self.assertIn("invoice_price_amount_upper_glass_enabled=False", logs)
        self.assertIn("invoice_price_amount_lower_glass_raw='0'", logs)
        self.assertIn("invoice_price_amount_lower_glass_enabled=False", logs)
        self.assertIn("show_price_amount_lower=False", logs)
        self.assertIn("show_price_amount_lower_reason=hidden_invoice_lower_glass_not_1", logs)

    def test_lower_decision_log_never_uses_move_slip_legacy_reason(self) -> None:
        page = _glass_page_raw("0", transaction_type="8")
        logs = self._capture_lower_decision_log(page)
        self.assertIn("transaction_type='8'", logs)
        self.assertIn("is_move_slip=True", logs)
        self.assertIn("move_slip_reason=transaction_type_8", logs)
        self.assertIn("invoice_price_amount_upper_glass_raw='0'", logs)
        self.assertIn("invoice_price_amount_upper_glass_enabled=False", logs)
        self.assertIn("invoice_price_amount_lower_glass_raw='0'", logs)
        self.assertIn("invoice_price_amount_lower_glass_enabled=False", logs)
        self.assertIn("show_price_amount_lower=False", logs)
        self.assertIn("show_price_amount_lower_reason=hidden_invoice_lower_glass_not_1", logs)
        self.assertNotIn("move_slip_legacy", logs)

    def test_move_slip_and_glass_decisions_are_independent(self) -> None:
        cases = [
            ("8", "0", True, False),
            ("8", "1", True, True),
            ("1", "1", False, True),
            ("1", "0", False, False),
        ]
        for transaction_type, glass_raw, expect_label, expect_lower in cases:
            with self.subTest(transaction_type=transaction_type, glass_raw=glass_raw):
                texts = _draw_form(_glass_page_raw(glass_raw, transaction_type=transaction_type), "01")
                hidden_total_count = _draw_form(
                    _glass_page_raw("0", transaction_type=transaction_type), "01"
                ).count("100,800")
                self.assertEqual("移動伝票" in texts, expect_label)
                self.assertEqual("430" in texts, expect_lower)
                self.assertEqual("51,600" in texts, expect_lower)
                if expect_lower:
                    self.assertGreater(texts.count("100,800"), hidden_total_count)
                else:
                    self.assertEqual(texts.count("100,800"), hidden_total_count)

    def test_upper_glass_does_not_control_lower_display(self) -> None:
        cases = [
            ("8", "9", "1", True),
            ("8", "1", "0", False),
        ]
        for transaction_type, upper_raw, lower_raw, expect_lower in cases:
            with self.subTest(upper_raw=upper_raw, lower_raw=lower_raw):
                texts = _draw_form(
                    _glass_page_raw(
                        lower_raw, transaction_type=transaction_type, upper_raw=upper_raw
                    ),
                    "01",
                )
                self.assertIn("移動伝票", texts)
                self.assertEqual("430" in texts, expect_lower)
                self.assertEqual("51,600" in texts, expect_lower)

    def test_all_vouchers_render_with_glass(self) -> None:
        """01〜08すべてのPDF帳票が硝子フラグ有無でエラーなく生成できる。"""
        titles = {
            "03": "指　図　書　(1)", "04": "指　図　書　(2)",
            "05": "梱　包　明　細　書", "06": "配　送　指　示　書",
        }
        for enabled in (True, False):
            for voucher in ("01", "02", "03", "04", "05", "06", "07", "08"):
                with self.subTest(enabled=enabled, voucher=voucher):
                    page = _glass_page(enabled)
                    c = _RecordingCanvas()
                    if voucher == "01":
                        vs._draw_form_01(c, page, "売　上　伝　票")
                    elif voucher == "02":
                        vs._draw_form_01(c, page, "工　場　控")
                    elif voucher == "07":
                        vs._draw_form_07(c, page)
                    elif voucher == "08":
                        vs._draw_form_08(c, page)
                    else:
                        vs._draw_form_shizu(c, page, titles[voucher], "工場印")
                    # 指図書系(03〜06)・受領書(08)には単価金額下段は出ない。
                    if voucher not in ("01", "02", "07"):
                        self.assertNotIn("51,600", c.texts)


if __name__ == "__main__":
    unittest.main()
