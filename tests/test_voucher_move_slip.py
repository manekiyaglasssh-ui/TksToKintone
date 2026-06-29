"""取引区分8（移動伝票）対応のテスト。

得意先コードに紐づく取引区分のOLAP取得と、取引区分8の場合の
売上伝票(01)・工場控(02)・納品書(07)へのPDF表示変更を検証する。
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.models import AppConfig, AppPaths
from app.voucher_data_mapper import build_voucher_pages
from app.voucher_olap_service import (
    VoucherOlapService,
    build_transaction_type_payload,
    parse_transaction_type,
)
from app import voucher_service as vs


# ── フェイクキャンバス（描画テキスト記録）──────────────────────────────────────
class _NoOpPath:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _RecordingCanvas:
    """drawString/drawRightString/drawCentredString のテキストを記録する。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def setFont(self, *a):
        pass

    def stringWidth(self, text, font, size):
        return 0.0

    def beginPath(self):
        return _NoOpPath()

    def drawString(self, x, y, text):
        self.texts.append(str(text))

    def drawRightString(self, x, y, text):
        self.texts.append(str(text))

    def drawCentredString(self, cx, y, text):
        self.texts.append(str(text))

    def __getattr__(self, name):
        return lambda *a, **k: None


# ── フェイクOLAPセッション ────────────────────────────────────────────────────
_TRANSACTION_SAMPLE = {
    "ResponseData": {"R1List": {"1": {"1": "9991173", "2": "8"}}}
}


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200
        self.text = str(data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


class _CountingSession:
    def __init__(self, response_data: dict) -> None:
        self.response_data = response_data
        self.put_calls = 0
        self.values: list[str] = []

    def put(self, *args, **kwargs) -> _FakeResponse:
        self.put_calls += 1
        return _FakeResponse(self.response_data)


class _RaisingSession:
    def put(self, *args, **kwargs):
        raise RuntimeError("OLAP通信失敗（テスト）")


def _config() -> AppConfig:
    base = Path(tempfile.gettempdir()) / "tks_to_kintone_test_move"
    paths = AppPaths(
        base_dir=base,
        config_env=base / "config.env",
        field_mapping_json=base / "field_mapping.json",
        work_dir=base / "work",
        log_dir=base / "logs",
        error_dir=base / "error",
    )
    return AppConfig(
        paths=paths,
        company_code="999",
        kintone_domain="example.cybozu.com",
        kintone_app_id="1",
        kintone_api_token="token",
        csv_encoding="utf-8",
        shukka_kbn_options=[],
        cleanup_retention_days=7,
        tks_client_mode="http",
        tks_base_url="https://example.test",
    )


def _service(session) -> VoucherOlapService:
    service = VoucherOlapService(_config(), logging.getLogger("test_move_slip"))
    service._session = session
    return service


def _move_page(transaction_type="8"):
    """移動伝票表示テスト用ページ。上段表示は下段と衝突しない固定文字列にする。"""
    return {
        "code_no": "9991173",
        "customer_name": "テスト得意先",
        "order_no": "",  # QR を発火させない
        "construction_rep": "工事担当太郎",
        "transaction_type": transaction_type,
        "details": [
            {"name": "品A", "unit_price": "UA", "amount": "AA",
             "sales_unit_price": "430", "ordered_quantity": "120"},
            {"name": "品B", "unit_price": "UB", "amount": "AB",
             "sales_unit_price": "250", "ordered_quantity": "120"},
            {"name": "品C", "unit_price": "UC", "amount": "AC",
             "sales_unit_price": "40", "ordered_quantity": "480"},
            # 対象外行（合計・下段表示に含めない）
            {"name": "*", "unit_price": "", "amount": "",
             "sales_unit_price": "999", "ordered_quantity": "999"},
        ],
    }


class TransactionTypeFetchTest(unittest.TestCase):
    def test_request_payload_built_from_customer_code(self) -> None:
        """テスト1: 得意先コードから取引区分取得リクエストが作成される。"""
        payload = build_transaction_type_payload("9991173")
        self.assertEqual(payload["OLAP対象データ"], "OLAP_M05-01 得意先マスタ")
        r1_names = [c["OLAP表示名"] for c in payload["R1List"]]
        self.assertEqual(r1_names, ["得意先コード", "取引区分"])
        condition = payload["R2List"][0]
        self.assertEqual(condition["フィールド論理名"], "得意先コード")
        self.assertEqual(condition["OLAP値"], "9991173")

    def test_parse_transaction_type_from_response(self) -> None:
        """テスト2: 取引区分レスポンスから取引区分を正しく取得できる。"""
        self.assertEqual(parse_transaction_type(_TRANSACTION_SAMPLE), "8")
        self.assertEqual(parse_transaction_type({"ResponseData": {"R1List": {}}}), "")
        self.assertEqual(parse_transaction_type({}), "")

    def test_page_retains_transaction_type_8(self) -> None:
        """テスト3: 取引区分8がPDF用ページデータに transaction_type=="8" として保持される。"""
        session = _CountingSession(_TRANSACTION_SAMPLE)
        service = _service(session)
        rows = [{"customer_code": "9991173", "9": "1", "6": "100", "7": "1"}]
        service._enrich_transaction_types(rows)
        self.assertEqual(rows[0]["transaction_type"], "8")
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertEqual(pages[0]["transaction_type"], "8")

    def test_empty_customer_code_is_blank(self) -> None:
        """テスト: 得意先コードが空なら取引区分は空扱い（問い合わせもしない）。"""
        session = _CountingSession(_TRANSACTION_SAMPLE)
        service = _service(session)
        self.assertEqual(service.fetch_transaction_type_by_customer_code(""), "")
        self.assertEqual(session.put_calls, 0)

    def test_fetch_failure_does_not_stop(self) -> None:
        """テスト12: 取引区分取得に失敗しても既存の伝票作成は止まらない。"""
        service = _service(_RaisingSession())
        rows = [{"customer_code": "9991173", "9": "1", "6": "100", "7": "1"}]
        # 例外を送出しないこと
        service._enrich_transaction_types(rows)
        self.assertEqual(rows[0]["transaction_type"], "")
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertEqual(pages[0]["transaction_type"], "")

    def test_same_customer_code_not_queried_twice(self) -> None:
        """テスト13: 同じ得意先コードへの問い合わせは重複しない（キャッシュ）。"""
        session = _CountingSession(_TRANSACTION_SAMPLE)
        service = _service(session)
        rows = [
            {"customer_code": "9991173"},
            {"customer_code": "9991173"},
            {"customer_code": "9991173"},
            {"customer_code": "8880000"},
        ]
        service._enrich_transaction_types(rows)
        # ユニークな得意先コードは2件 → put は2回のみ
        self.assertEqual(session.put_calls, 2)


class MoveSlipDisplayTest(unittest.TestCase):
    _SHIZU_TITLES = {
        "03": "指　図　書　(1)",
        "04": "指　図　書　(2)",
        "05": "梱　包　明　細　書",
        "06": "配　送　指　示　書",
    }

    def _draw_form(self, page, voucher) -> list[str]:
        c = _RecordingCanvas()
        if voucher == "01":
            vs._draw_form_01(c, page, "売　上　伝　票")
        elif voucher == "02":
            vs._draw_form_01(c, page, "工　場　控")
        elif voucher == "07":
            vs._draw_form_07(c, page)
        elif voucher == "08":
            vs._draw_form_08(c, page)
        elif voucher in self._SHIZU_TITLES:
            vs._draw_form_shizu(c, page, self._SHIZU_TITLES[voucher], "工場印")
        return c.texts

    _ALL_VOUCHERS = ("01", "02", "03", "04", "05", "06", "07", "08")

    def test_label_on_all_vouchers_when_8(self) -> None:
        """テスト1〜8: 取引区分8で全伝票(01〜08)に「移動伝票」が表示される。"""
        for voucher in self._ALL_VOUCHERS:
            with self.subTest(voucher=voucher):
                self.assertIn("移動伝票", self._draw_form(_move_page("8"), voucher))

    def test_label_absent_on_all_vouchers_when_not_8(self) -> None:
        """テスト9: 取引区分8以外では全伝票で「移動伝票」が表示されない。"""
        for voucher in self._ALL_VOUCHERS:
            with self.subTest(voucher=voucher):
                self.assertNotIn("移動伝票", self._draw_form(_move_page("1"), voucher))

    def test_label_absent_when_blank_or_failed(self) -> None:
        """取引区分が空欄・取得失敗（空文字）の場合は表示されない。"""
        for voucher in self._ALL_VOUCHERS:
            with self.subTest(voucher=voucher):
                self.assertNotIn("移動伝票", self._draw_form(_move_page(""), voucher))

    def test_numeric_transaction_type_8(self) -> None:
        """取引区分が数値 8 でも文字列 "8" でも判定できる。"""
        self.assertIn("移動伝票", self._draw_form(_move_page(8), "01"))
        self.assertTrue(vs.is_move_slip_transaction_type(8))
        self.assertTrue(vs.is_move_slip_transaction_type("8"))
        self.assertFalse(vs.is_move_slip_transaction_type("18"))
        self.assertFalse(vs.is_move_slip_transaction_type(""))

    def test_unit_price_lower_displayed(self) -> None:
        """テスト8: 取引区分8で単価列下段に売上単価が表示される。"""
        texts = self._draw_form(_move_page("8"), "01")
        self.assertIn("430", texts)
        self.assertIn("250", texts)
        self.assertIn("40", texts)

    def test_amount_lower_displayed(self) -> None:
        """テスト9: 取引区分8で金額列下段に 売上単価×受注数量 が表示される。"""
        texts = self._draw_form(_move_page("8"), "01")
        self.assertIn("51,600", texts)   # 430 * 120
        self.assertIn("30,000", texts)   # 250 * 120
        self.assertIn("19,200", texts)   # 40 * 480

    def test_amount_total_lower_displayed(self) -> None:
        """テスト10: 取引区分8で金額列合計行下段に Σ(売上単価×受注数量) が表示される。"""
        texts = self._draw_form(_move_page("8"), "01")
        # 100,800 = 51,600 + 30,000 + 19,200。既存の右下合計欄(摘要列)にも同値が出るため、
        # 金額列合計行下段の追加分を含め2回出現することで移動伝票分の描画を確認する。
        self.assertGreaterEqual(texts.count("100,800"), 2)

    def test_columns_only_on_sales_factory_delivery(self) -> None:
        """テスト10: 単価列・金額列の下段表示は売上伝票・工場控・納品書のみ。

        51,600 は移動伝票の金額列下段（行単位）でのみ描画されるため、
        指図書系(03〜06)・受領書(08)には現れないことを確認する。
        """
        for voucher in ("03", "04", "05", "06", "08"):
            with self.subTest(voucher=voucher):
                texts = self._draw_form(_move_page("8"), voucher)
                # ラベルは表示される（全伝票対象）が、列下段は表示されない。
                self.assertIn("移動伝票", texts)
                self.assertNotIn("51,600", texts)
        # 対象3伝票では金額列下段が表示される。
        for voucher in ("01", "02", "07"):
            with self.subTest(voucher=voucher):
                self.assertIn("51,600", self._draw_form(_move_page("8"), voucher))


class MoveSlipTotalCalcTest(unittest.TestCase):
    def test_star_row_excluded(self) -> None:
        """テスト14: name == "*" の対象外行は合計対象外。"""
        rows = [
            {"name": "品A", "sales_unit_price": "430", "ordered_quantity": "120"},
            {"name": "*", "sales_unit_price": "999", "ordered_quantity": "999"},
        ]
        self.assertEqual(vs.calculate_sales_amount_total_for_move_slip(rows), 51600.0)

    def test_blank_and_non_numeric_as_zero(self) -> None:
        """テスト15: 空欄・非数値は0扱い。"""
        rows = [
            {"name": "品A", "sales_unit_price": "430", "ordered_quantity": "120"},
            {"name": "品B", "sales_unit_price": "", "ordered_quantity": "10"},
            {"name": "品C", "sales_unit_price": "abc", "ordered_quantity": "10"},
            {"name": "品D", "sales_unit_price": "100", "ordered_quantity": ""},
        ]
        self.assertEqual(vs.calculate_sales_amount_total_for_move_slip(rows), 51600.0)
        self.assertIsNone(vs._parse_number_or_none(""))
        self.assertIsNone(vs._parse_number_or_none("abc"))
        self.assertEqual(vs._parse_number_or_none("1,200"), 1200.0)

    def test_ordered_quantity_with_asterisk_uses_numeric(self) -> None:
        """数量表示に '*' が付いても数値部分の受注数量で計算する。"""
        rows = [{"name": "品A", "sales_unit_price": "430", "ordered_quantity": "120*"}]
        self.assertEqual(vs.calculate_sales_amount_total_for_move_slip(rows), 51600.0)


if __name__ == "__main__":
    unittest.main()
