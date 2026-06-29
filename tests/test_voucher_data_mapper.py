from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from app.voucher_data_mapper import (
    build_qr_code_image,
    build_voucher_pages,
    compute_op_calculated_fields,
    count_r1_rows,
    extract_r1_rows,
    first_r1_row_keys,
    format_date_yy_mm_dd,
    has_result_status_row,
    is_missing_voucher_no,
    parse_denpyo_numbers,
    r1_list_type_name,
    resolve_unit_and_amount_values,
    response_data_keys,
    response_top_keys,
)
import app.voucher_data_mapper as mapper


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VoucherDataMapperTest(unittest.TestCase):
    def test_parse_denpyo_numbers_trims_blanks_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_denpyo_numbers(" 1405113\n\n1405114, 1405113\r\n1405115 "),
            ["1405113", "1405114", "1405115"],
        )

    def test_is_missing_voucher_no(self) -> None:
        missing_values = [None, "", " ", 0, "0", "0000000"]
        for value in missing_values:
            with self.subTest(value=value):
                self.assertTrue(is_missing_voucher_no(value))
        for value in ("1234567", "Z739291"):
            with self.subTest(value=value):
                self.assertFalse(is_missing_voucher_no(value))

    def test_format_date_yy_mm_dd(self) -> None:
        self.assertEqual(format_date_yy_mm_dd("2026/06/19"), "26/06/19")
        self.assertEqual(format_date_yy_mm_dd("0000/00/00"), "")

    def test_extract_response_and_build_pages(self) -> None:
        response = _sample_response()
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))

        self.assertEqual(len(rows), 18)
        self.assertEqual(len(pages), 3)
        self.assertEqual(len(pages[0]["details"]), 7)
        self.assertEqual(pages[0]["issue_date"], "26/06/05")
        self.assertEqual(pages[0]["delivery_date"], "26/06/19")
        self.assertEqual(pages[0]["code_no"], "40630")
        self.assertEqual(pages[0]["order_no"], "1405113")
        self.assertEqual(pages[0]["voucher_no"], "Z737704")
        self.assertEqual(pages[1]["voucher_no"], "Z737705")
        self.assertEqual(pages[2]["voucher_no"], "Z737706")
        self.assertEqual(pages[0]["details"][0]["note_lines"], ["1,580 加", "7,594 倉庫ま"])
        self.assertEqual(pages[0]["total_note_upper"], "2,830")
        self.assertEqual(pages[0]["total_note_lower"], "14,111")

    def test_extract_uses_response_data_r1list_from_full_response(self) -> None:
        response = _sample_response()
        rows = extract_r1_rows(response)

        self.assertEqual(response_top_keys(response), ["PropertyStatuses", "ResultStatus", "ResponseData"])
        self.assertEqual(response_data_keys(response), ["R1List"])
        self.assertEqual(r1_list_type_name(response), "dict")
        self.assertEqual(count_r1_rows(response), 18)
        self.assertEqual(first_r1_row_keys(response)[:6], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(rows[0]["customer_name"], "株式会社たくみ硝子店")
        self.assertEqual(rows[0]["order_no"], "1405113")
        self.assertEqual(rows[0]["voucher_no"], "Z737704")

    def test_result_status_is_not_treated_as_detail_row(self) -> None:
        response = {
            "PropertyStatuses": [],
            "ResultStatus": _result_status_row(),
            "ResponseData": {},
        }

        self.assertEqual(count_r1_rows(response), 0)
        self.assertEqual(first_r1_row_keys(response), [])
        self.assertEqual(extract_r1_rows(response), [])

    def test_r1list_dict_values_are_detail_rows(self) -> None:
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {"5": "得意先A", "6": "1405113", "9": "Z001", "16": "商品A"},
                    "2": {"5": "得意先A", "6": "1405113", "9": "Z001", "16": "商品B"},
                }
            }
        }

        rows = extract_r1_rows(response)

        self.assertEqual(count_r1_rows(response), 2)
        self.assertEqual(rows[0]["product_name"], "商品A")
        self.assertEqual(rows[1]["product_name"], "商品B")

    def test_r1list_list_values_are_detail_rows(self) -> None:
        response = {
            "ResponseData": {
                "R1List": [
                    {"5": "得意先A", "6": "1405113", "9": "Z001", "16": "商品A"},
                    {"5": "得意先A", "6": "1405113", "9": "Z001", "16": "商品B"},
                ]
            }
        }

        rows = extract_r1_rows(response)

        self.assertEqual(count_r1_rows(response), 2)
        self.assertEqual(rows[0]["product_name"], "商品A")
        self.assertEqual(rows[1]["product_name"], "商品B")

    def test_output_log_and_rdata_rows_are_not_detail_rows(self) -> None:
        response = {
            "ResponseData": {
                "R1List": {
                    "1": _result_status_row(),
                    "2": {"5": "得意先A", "6": "1405113", "9": "Z001", "16": "商品A"},
                }
            }
        }

        rows = extract_r1_rows(response)

        self.assertTrue(has_result_status_row(response))
        self.assertEqual(count_r1_rows(response), 1)
        self.assertEqual(first_r1_row_keys(response), ["5", "6", "9", "16"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_name"], "商品A")

    def test_qr_code_image_is_created(self) -> None:
        buf = build_qr_code_image("1405113")
        self.assertGreater(len(buf.getvalue()), 0)

    # ── 品名列の表示はトリムしない（タスク3）──────────────────────────────────
    def test_product_name_keeps_fullwidth_space(self) -> None:
        """11. 商品名称『5ミリ　切断』の全角スペースが保持される。"""
        d = mapper._detail_row({"product_name": "5ミリ　切断"})
        self.assertEqual(d["name"], "5ミリ　切断")

    def test_product_name_keeps_consecutive_spaces(self) -> None:
        """12. 商品名称内の連続スペースが保持される。"""
        d = mapper._detail_row({"product_name": "5ミリ　小口加工　　磨き  ４方"})
        self.assertEqual(d["name"], "5ミリ　小口加工　　磨き  ４方")

    def test_product_name_keeps_leading_trailing_spaces(self) -> None:
        """13. 商品名称の先頭・末尾スペースを表示用データではトリムしない。"""
        d = mapper._detail_row({"product_name": "  前後空白  "})
        self.assertEqual(d["name"], "  前後空白  ")
        # 判定用の name_key は正規化（トリム）される。
        self.assertEqual(d["name_key"], "前後空白")

    def test_star_judgment_not_broken(self) -> None:
        """14. name == "*" などの既存判定は壊れない。"""
        from app.voucher_service import _is_star_row

        # 純粋な "*" は対象外行。
        d = mapper._detail_row({"product_name": "*"})
        self.assertTrue(_is_star_row(d))
        # 前後に空白がある "*" でも判定用キーで対象外行と判定できる。
        d2 = mapper._detail_row({"product_name": " * "})
        self.assertTrue(_is_star_row(d2))
        # 通常の品名は対象外行ではない。
        d3 = mapper._detail_row({"product_name": " 5ミリ　切断 "})
        self.assertFalse(_is_star_row(d3))

    def test_product_name_blanks_preserved_all_vouchers(self) -> None:
        """15. 01〜08すべての伝票で品名列のブランクが保持される（PDF描画値を確認）。"""
        from app import voucher_service
        from app.voucher_templates import VOUCHER_IDS

        raw_name = " 5ミリ　小口加工　磨き　４方 "
        # 全伝票の品名描画は row.get("name") を使う。トリムせず描画されることを確認する。
        captured: list[str] = []

        class _FakeCanvas:
            def setFont(self, *a):
                pass

            def stringWidth(self, *a, **k):
                return 0.0  # クリップしない

            def drawString(self, x, y, text):
                captured.append(text)

            def drawRightString(self, *a):
                pass

            def drawCentredString(self, *a):
                pass

        row = {"name": raw_name, "name_key": raw_name.strip()}
        for _vid in VOUCHER_IDS:
            captured.clear()
            voucher_service._str(_FakeCanvas(), row.get("name", ""), 0.0, 0.0, 8.0)
            self.assertIn(raw_name, captured)

    # ── OLAP取得経路（表示Noキー16→エイリアス）でのブランク保持（再修正）──────
    @staticmethod
    def _olap_row(product_name: str) -> dict[str, str]:
        """商品名称をキー16に持つ現行レイアウトのOLAP行を、エイリアス付与済みで返す。

        `_with_display_name_aliases` は product_name エイリアスに strip 済み値を入れる。
        この経路を通しても表示用 `name` が生値を保つことを確認するためのヘルパー。
        """
        # キー36の存在で現行レイアウト判定になる。
        raw = {"16": product_name, "36": ""}
        return mapper._with_display_name_aliases({k: str(v) for k, v in raw.items()})

    def test_olap_path_keeps_leading_fullwidth_space(self) -> None:
        """1. OLAP取得値の先頭に全角スペースがある場合、name に保持される。"""
        d = mapper._detail_row(self._olap_row("　5ミリ　切断"))
        self.assertEqual(d["name"], "　5ミリ　切断")

    def test_olap_path_keeps_leading_halfwidth_space(self) -> None:
        """2. OLAP取得値の先頭に半角スペースがある場合、name に保持される。"""
        d = mapper._detail_row(self._olap_row(" 5ミリ 切断"))
        self.assertEqual(d["name"], " 5ミリ 切断")

    def test_olap_path_keeps_consecutive_spaces(self) -> None:
        """3. OLAP取得値内の連続スペースが name に保持される。"""
        d = mapper._detail_row(self._olap_row("5ミリ　　小口加工  磨き"))
        self.assertEqual(d["name"], "5ミリ　　小口加工  磨き")

    def test_olap_path_keeps_trailing_space(self) -> None:
        """4. OLAP取得値の末尾スペースが表示用 name では保持される。"""
        d = mapper._detail_row(self._olap_row("5ミリ　切断　"))
        self.assertEqual(d["name"], "5ミリ　切断　")

    def test_olap_path_name_key_is_stripped(self) -> None:
        """5. name_key は従来どおり strip 済みで、* 行判定が壊れない。"""
        from app.voucher_service import _is_star_row

        d = mapper._detail_row(self._olap_row("　5ミリ　切断　"))
        self.assertEqual(d["name_key"], "5ミリ　切断")
        self.assertFalse(_is_star_row(d))
        # 前後空白付きの "*" でも対象外行と判定される。
        d_star = mapper._detail_row(self._olap_row("　*　"))
        self.assertEqual(d_star["name_key"], "*")
        self.assertTrue(_is_star_row(d_star))

    def test_build_pages_name_keeps_leading_space(self) -> None:
        """6. build_voucher_pages（name_lines相当の組み立て）後も先頭スペースが消えない。"""
        rows = extract_r1_rows({
            "ResponseData": {"R1List": [
                {"6": "X001", "9": "V001", "7": "1", "16": "　5ミリ　切断", "36": ""},
            ]}
        })
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        self.assertEqual(pages[0]["details"][0]["name"], "　5ミリ　切断")

    def test_pdf_draw_keeps_leading_space_all_vouchers(self) -> None:
        """7・8. 01〜08すべての伝票で、PDF描画直前の品名に先頭スペースが残る。"""
        import logging
        import os

        from app import voucher_service
        from app.voucher_templates import VOUCHER_IDS

        raw_name = "　5ミリ　小口加工　磨き　４方"
        rows = extract_r1_rows({
            "ResponseData": {"R1List": [
                {"6": "X001", "9": "V001", "7": "1", "16": raw_name,
                 "19": "1", "21": "枚", "40": "0", "36": ""},
            ]}
        })
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        print_data = {"pages": pages}

        captured: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.getMessage().startswith("product_name_display"):
                    captured.append(record.args[0])

        logger = logging.getLogger("tks_to_kintone_app")
        handler = _Handler()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        os.environ["VOUCHER_NAME_DEBUG"] = "1"
        try:
            for vid in VOUCHER_IDS:
                captured.clear()
                voucher_service._assemble_pdf_bytes([vid], print_data, None)
                self.assertTrue(
                    captured, f"伝票{vid}で品名描画ログが出ていない")
                self.assertIn(
                    raw_name, captured,
                    f"伝票{vid}でPDF描画直前の品名から先頭スペースが消えた: {captured!r}")
        finally:
            os.environ.pop("VOUCHER_NAME_DEBUG", None)
            logger.setLevel(old_level)
            logger.removeHandler(handler)

    def test_sales_rep_mapped_from_key33(self) -> None:
        """営業担当者名称(key33)が sales_rep にマッピングされること。"""
        response = _sample_response()
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        self.assertIn("sales_rep", pages[0])
        self.assertEqual(pages[0]["sales_rep"], "船橋")

    def test_construction_rep_mapped_from_key35(self) -> None:
        """工事担当者名称(key35)が construction_rep にマッピングされること。"""
        response = _sample_response()
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        self.assertIn("construction_rep", pages[0])

    def test_star_row_excluded_from_totals(self) -> None:
        """品名が*の行は合計計算に含まれないこと。"""
        from datetime import date as _date

        rows_all = [
            {"6": "X001", "9": "V001", "16": "品名A", "23": "1000", "24": "500", "33": "", "35": ""},
            {"6": "X001", "9": "V001", "16": "*",     "23": "9999", "24": "9999", "33": "", "35": ""},
        ]
        pages = build_voucher_pages(rows_all, today=_date(2026, 1, 1))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["total_note_upper"], "1,000")
        self.assertEqual(pages[0]["total_note_lower"], "500")

    def test_resolve_unit_and_amount_for_op00_case(self) -> None:
        row = _resolver_row(op_type="00", quantity_unit_name="ケース")
        self.assertEqual(resolve_unit_and_amount_values(row), ("9.999", "2.222"))

    def test_resolve_unit_and_amount_for_op00_lot(self) -> None:
        row = _resolver_row(op_type="00", quantity_unit_name="ロット")
        self.assertEqual(resolve_unit_and_amount_values(row), ("9.999", "2.222"))

    def test_resolve_unit_and_amount_for_op00_non_case_lot(self) -> None:
        row = _resolver_row(op_type="00", quantity_unit_name="枚")
        self.assertEqual(resolve_unit_and_amount_values(row), ("1.111", "2.222"))

    def test_resolve_unit_and_amount_for_op01(self) -> None:
        row = _resolver_row(op_type="01", quantity_unit_name="ケース")
        self.assertEqual(resolve_unit_and_amount_values(row), ("1.111", "2.222"))

    def test_resolve_op01_fallback_unit_when_stat_zero(self) -> None:
        row = _resolver_row(op_type="01", stat_quantity="0", ordered_stat_quantity="2.222")
        unit, amount = resolve_unit_and_amount_values(row)
        self.assertEqual(unit, "3.333")
        self.assertEqual(amount, "2.222")

    def test_resolve_op01_fallback_amount_when_ordered_stat_zero(self) -> None:
        row = _resolver_row(op_type="01", stat_quantity="1.111", ordered_stat_quantity="0")
        unit, amount = resolve_unit_and_amount_values(row)
        self.assertEqual(unit, "1.111")
        self.assertEqual(amount, "4.444")

    def test_resolve_op01_fallback_both_when_both_zero(self) -> None:
        row = _resolver_row(op_type="01", stat_quantity="0", ordered_stat_quantity="0.000")
        unit, amount = resolve_unit_and_amount_values(row)
        self.assertEqual(unit, "3.333")
        self.assertEqual(amount, "4.444")

    def test_resolve_op01_fallback_logs_info(self) -> None:
        import logging as _logging
        row = _resolver_row(op_type="01", stat_quantity="0", ordered_stat_quantity="0")
        logger_name = "test_op01_fallback_log"
        logger = _logging.getLogger(logger_name)
        with self.assertLogs(logger_name, level="INFO") as logs:
            resolve_unit_and_amount_values(row, logger=logger)
        messages = "\n".join(logs.output)
        self.assertIn("統計数量=0", messages)
        self.assertIn("受注統計数量=0", messages)

    def test_resolve_unit_and_amount_for_op02(self) -> None:
        row = _resolver_row(op_type="02", quantity_unit_name="枚")
        self.assertEqual(resolve_unit_and_amount_values(row), ("3.333", "4.444"))

    def test_star_row_blanks_unit_and_amount_regardless_of_op_type(self) -> None:
        rows = [
            {
                "6": "X001",
                "7": "1",
                "9": "V001",
                "16": "*",
                "19": "1",
                "20": "0",
                "21": "ケース",
                "22": "1.111",
                "23": "2.222",
                "24": "1000",
                "25": "500",
                "26": "2",
                "27": "倉庫ま",
                "34": "",
                "36": "",
                "40": "00",
                "case_lot_square": "9.999",
            }
        ]
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["qty"], "")
        self.assertEqual(detail["unit_price"], "")
        self.assertEqual(detail["amount"], "")
        self.assertEqual(detail["note_lines"], [])

    def test_current_olap_display_names_are_mapped_to_aliases(self) -> None:
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {
                        "1": "本社",
                        "4": "40630",
                        "5": "得意先",
                        "6": "1405113",
                        "7": "1",
                        "8": "2026/06/19",
                        "9": "Z001",
                        "16": "商品",
                        "19": "1.000",
                        "21": "枚",
                        "22": "1.234",
                        "23": "2.468",
                        "24": "1000",
                        "25": "500",
                        "26": "2",
                        "27": "倉庫ま",
                        "28": "2026/06/20",
                        "29": "住所",
                        "30": "摘要",
                        "31": "P001",
                        "32": "物件",
                        "34": "営業",
                        "36": "工事",
                        "37": "100",
                        "38": "200",
                        "40": "01",
                    }
                }
            }
        }
        rows = extract_r1_rows(response)
        self.assertEqual(rows[0]["quantity_unit_name"], "枚")
        self.assertEqual(rows[0]["stat_quantity"], "1.234")
        self.assertEqual(rows[0]["ordered_stat_quantity"], "2.468")
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertEqual(pages[0]["sales_rep"], "営業")
        self.assertEqual(pages[0]["construction_rep"], "工事")
        self.assertEqual(pages[0]["details"][0]["unit_price"], "1.234㎡")
        self.assertEqual(pages[0]["details"][0]["amount"], "2.468㎡")

    def test_display_name_mapping_survives_shifted_existing_keys(self) -> None:
        original = mapper._R1_DISPLAY_ALIAS_KEYS
        mapper._R1_DISPLAY_ALIAS_KEYS = {
            "customer_name": ("得意先名称", "50"),
            "order_no": ("受注No", "51"),
            "voucher_no": ("納品書No", "52"),
            "product_name": ("商品名称", "53"),
            "ordered_quantity": ("受注数量", "54"),
            "quantity_unit_name": ("数量単位名称", "55"),
            "stat_quantity": ("統計数量", "56"),
            "ordered_stat_quantity": ("受注統計数量", "57"),
            "sales_unit_price": ("売上単価", "58"),
            "purchase_unit_price": ("仕入単価", "59"),
        }
        try:
            rows = extract_r1_rows(
                {
                    "ResponseData": {
                        "R1List": {
                            "1": {
                                "40": "01",
                                "50": "得意先A",
                                "51": "1405331",
                                "52": "Z999999",
                                "53": "商品A",
                                "54": "2.000",
                                "55": "枚",
                                "56": "1.111",
                                "57": "2.222",
                                "58": "1000",
                                "59": "500",
                            }
                        }
                    }
                }
            )
            pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        finally:
            mapper._R1_DISPLAY_ALIAS_KEYS = original

        self.assertEqual(pages[0]["customer_name"], "得意先A")
        self.assertEqual(pages[0]["order_no"], "1405331")
        self.assertEqual(pages[0]["delivery_no"], "Z999999")
        self.assertEqual(pages[0]["details"][0]["item_name"], "商品A")
        self.assertEqual(pages[0]["details"][0]["quantity"], "2枚")

    def test_display_name_mapping_reads_added_op_fields(self) -> None:
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {
                        "5": "得意先",
                        "6": "1405331",
                        "9": "Z001",
                        "16": "商品",
                        "19": "1.000",
                        "21": "枚",
                        "22": "1.111",
                        "23": "2.222",
                        "24": "1000",
                        "25": "500",
                        "37": "100",
                        "38": "200",
                        "40": "02",
                        "42": "9.999",
                        "43": "3.333",
                        "44": "4.444",
                    }
                }
            }
        }
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertEqual(rows[0]["op_type"], "02")
        self.assertEqual(rows[0]["op02_square"], "3.333")
        self.assertEqual(rows[0]["op02_total_square"], "4.444")
        self.assertEqual(pages[0]["details"][0]["unit_price_display"], "3.333㎡")
        self.assertEqual(pages[0]["details"][0]["amount_display"], "4.444㎡")

    def test_fallback_key_logs_warning_when_display_name_missing(self) -> None:
        original = mapper._R1_DISPLAY_ALIAS_KEYS
        mapper._R1_DISPLAY_ALIAS_KEYS = {"op_type": ("OP区分", "40")}
        logger_name = "test_voucher_fallback"
        try:
            with self.assertLogs(logger_name, level="WARNING") as logs:
                extract_r1_rows(
                    {
                        "ResponseData": {
                            "R1List": {
                                "1": {
                                    "37": "100",
                                    "40": "02",
                                    "43": "3.333",
                                }
                            }
                        }
                    },
                    logger=__import__("logging").getLogger(logger_name),
                )
        finally:
            mapper._R1_DISPLAY_ALIAS_KEYS = original
        self.assertTrue(any("fallback key '43'" in message for message in logs.output))

    def test_build_pages_contains_required_print_data(self) -> None:
        rows = extract_r1_rows(_sample_response())
        pages = build_voucher_pages(rows, today=date(2026, 6, 5))
        self.assertTrue(pages[0]["order_no"])
        self.assertTrue(pages[0]["customer_name"])
        self.assertTrue(pages[0]["delivery_no"])
        self.assertGreater(len(pages[0]["details"]), 0)


class PropertyLinesTest(unittest.TestCase):
    """物件No欄の表示ルールのテスト。"""

    def _build(
        self,
        property_no: str,
        property_name: str,
        order_summary: str = "",
        delivery_address1: str = "",
        finish_date: str = "",
    ) -> list[dict]:
        rows = [{
            "6": "X001",
            "9": "V001",
            "16": "商品A",
            "property_no": property_no,
            "property_name": property_name,
            "order_summary": order_summary,
            "delivery_address1": delivery_address1,
            "finish_date": finish_date,
        }]
        from datetime import date as _date
        return build_voucher_pages(rows, today=_date(2026, 1, 1))

    def test_property_no_and_name_present(self) -> None:
        """物件No・物件名称1 が両方あれば "No 名称" を返す。"""
        pages = self._build("40630808", "シャングリラ京都")
        self.assertEqual(pages[0]["property_lines"], ["40630808 シャングリラ京都"])

    def test_property_no_only(self) -> None:
        """物件No のみの場合は No だけを返す。"""
        pages = self._build("40630808", "")
        self.assertEqual(pages[0]["property_lines"], ["40630808"])

    def test_property_name_only(self) -> None:
        """物件名称1 のみの場合は名称だけを返す。"""
        pages = self._build("", "シャングリラ京都")
        self.assertEqual(pages[0]["property_lines"], ["シャングリラ京都"])

    def test_both_empty_returns_empty_string(self) -> None:
        """物件No・物件名称1 が両方空なら空文字列を返す。"""
        pages = self._build("", "")
        self.assertEqual(pages[0]["property_lines"], [""])

    def test_order_summary_not_shown_in_property_lines_when_property_empty(self) -> None:
        """order_summary があっても property_no/name が空なら property_lines は空。"""
        pages = self._build("", "", order_summary="受注摘要テキスト")
        self.assertEqual(pages[0]["property_lines"], [""])

    def test_order_summary_not_shown_in_property_lines_when_property_no_empty(self) -> None:
        """property_no が空で order_summary があっても property_lines に混入しない。"""
        pages = self._build("", "物件名", order_summary="受注摘要テキスト")
        self.assertEqual(pages[0]["property_lines"], ["物件名"])

    def test_order_summary_appears_in_summary_lines(self) -> None:
        """order_summary は summary_lines にのみ表示される。"""
        pages = self._build("", "", order_summary="受注摘要テキスト")
        self.assertIn("受注摘要テキスト", pages[0]["summary_lines"])

    def test_summary_line1_is_delivery_address1(self) -> None:
        pages = self._build("", "", delivery_address1="京都市上京区")
        self.assertEqual(pages[0]["summary_line1"], "京都市上京区")
        self.assertEqual(pages[0]["summary_lines"][0], "京都市上京区")

    def test_summary_line2_is_order_summary(self) -> None:
        pages = self._build("", "", order_summary="受注見出摘要")
        self.assertEqual(pages[0]["summary_line2"], "受注見出摘要")
        self.assertEqual(pages[0]["summary_lines"][1], "受注見出摘要")

    def test_summary_lines_are_delivery_address_and_order_summary_only(self) -> None:
        pages = self._build(
            "",
            "",
            order_summary="受注見出摘要",
            delivery_address1="京都市上京区",
            finish_date="2026/06/19",
        )
        self.assertEqual(pages[0]["summary_lines"], ["京都市上京区", "受注見出摘要"])
        self.assertNotIn("2026/06/19", pages[0]["summary_lines"])

    def test_blank_summary_values_are_empty(self) -> None:
        pages = self._build("", "", order_summary="　 ", delivery_address1=None)  # type: ignore[arg-type]
        self.assertEqual(pages[0]["summary_line1"], "")
        self.assertEqual(pages[0]["summary_line2"], "")

    def test_summary_line1_dash_delivery_address_is_empty_and_finish_date_is_not_used(self) -> None:
        pages = self._build(
            "40630808",
            "物件名",
            order_summary="心斎橋パルコＣＯＮＺ",
            delivery_address1="-",
            finish_date="0000/00/00",
        )
        self.assertEqual(pages[0]["summary_line1"], "")
        self.assertEqual(pages[0]["summary_line2"], "心斎橋パルコＣＯＮＺ")
        self.assertEqual(pages[0]["summary_lines"], ["", "心斎橋パルコＣＯＮＺ"])
        self.assertNotIn("0000/00/00", pages[0]["summary_lines"])
        self.assertEqual(pages[0]["property_lines"], ["40630808 物件名"])

    def test_current_olap_dash_delivery_address_does_not_fallback_to_finish_date(self) -> None:
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {
                        "6": "X001",
                        "7": "1",
                        "9": "V001",
                        "16": "商品A",
                        "28": "0000/00/00",
                        "29": "-",
                        "30": "心斎橋パルコＣＯＮＺ",
                        "31": "40630808",
                        "32": "物件名",
                        "40": "",
                    }
                }
            }
        }
        rows = extract_r1_rows(response)
        pages = build_voucher_pages(rows, today=date(2026, 1, 1))
        self.assertEqual(pages[0]["summary_lines"], ["", "心斎橋パルコＣＯＮＺ"])
        self.assertNotIn("0000/00/00", pages[0]["summary_lines"])
        self.assertEqual(pages[0]["property_lines"], ["40630808 物件名"])

    def test_summary_line1_does_not_use_finish_date_for_all_voucher_types(self) -> None:
        for voucher_type in [f"{number:02d}" for number in range(1, 9)]:
            with self.subTest(voucher_type=voucher_type):
                rows = [{
                    "6": f"X{voucher_type}",
                    "9": f"V{voucher_type}",
                    "16": "商品A",
                    "voucher_type": voucher_type,
                    "delivery_address1": "-",
                    "finish_date": "0000/00/00",
                    "order_summary": "心斎橋パルコＣＯＮＺ",
                }]
                pages = build_voucher_pages(rows, today=date(2026, 1, 1))
                self.assertEqual(pages[0]["summary_line1"], "")
                self.assertEqual(pages[0]["summary_line2"], "心斎橋パルコＣＯＮＺ")
                self.assertNotIn("0000/00/00", pages[0]["summary_lines"])


class OpCalculatedFieldsTest(unittest.TestCase):
    def test_op02_square_computed_from_width_height(self) -> None:
        row: dict[str, str] = {"width": "1000", "height": "2000", "ordered_quantity": "1"}
        compute_op_calculated_fields(row)
        from decimal import Decimal
        self.assertAlmostEqual(float(row["op02_square"]), float(Decimal("1000") * Decimal("2000") / Decimal("1000000")))

    def test_op02_total_square_computed_from_width_height_qty(self) -> None:
        row: dict[str, str] = {"width": "1000", "height": "2000", "ordered_quantity": "3"}
        compute_op_calculated_fields(row)
        from decimal import Decimal
        expected = Decimal("1000") * Decimal("2000") / Decimal("1000000") * Decimal("3")
        self.assertAlmostEqual(float(row["op02_total_square"]), float(expected))

    def test_case_lot_square_computed_from_width_height(self) -> None:
        row: dict[str, str] = {"width": "1000", "height": "2000", "ordered_quantity": "1"}
        compute_op_calculated_fields(row)
        from decimal import Decimal
        expected = (Decimal("1000") * Decimal("25.4")) * (Decimal("2000") * Decimal("25.4")) / Decimal("1000000")
        self.assertAlmostEqual(float(row["case_lot_square"]), float(expected))

    def test_comma_dimension_is_parsed(self) -> None:
        row: dict[str, str] = {"width": "1,303", "height": "1,061", "ordered_quantity": "1.000"}
        compute_op_calculated_fields(row)
        from decimal import Decimal
        expected_sq = Decimal("1303") * Decimal("1061") / Decimal("1000000")
        self.assertAlmostEqual(float(row["op02_square"]), float(expected_sq))

    def test_null_width_results_in_empty_values(self) -> None:
        row: dict[str, str] = {"width": "", "height": "1000", "ordered_quantity": "1"}
        compute_op_calculated_fields(row)
        self.assertEqual(row.get("op02_square", ""), "")
        self.assertEqual(row.get("op02_total_square", ""), "")
        self.assertEqual(row.get("case_lot_square", ""), "")

    def test_null_height_results_in_empty_values(self) -> None:
        row: dict[str, str] = {"width": "1000", "height": "", "ordered_quantity": "1"}
        compute_op_calculated_fields(row)
        self.assertEqual(row.get("op02_square", ""), "")

    def test_existing_value_is_not_overwritten(self) -> None:
        row: dict[str, str] = {
            "width": "1000",
            "height": "2000",
            "ordered_quantity": "1",
            "op02_square": "already_set",
        }
        compute_op_calculated_fields(row)
        self.assertEqual(row["op02_square"], "already_set")

    def test_op02_square_used_as_unit_price_for_op02(self) -> None:
        row: dict[str, str] = {
            "width": "1000",
            "height": "2000",
            "ordered_quantity": "3",
            "op_type": "02",
            "quantity_unit_name": "枚",
            "stat_quantity": "0",
            "ordered_stat_quantity": "0",
        }
        compute_op_calculated_fields(row)
        unit_price, amount = resolve_unit_and_amount_values(row)
        self.assertEqual(unit_price, row["op02_square"])
        self.assertEqual(amount, row["op02_total_square"])

    def test_case_lot_square_used_as_unit_price_for_op00_case(self) -> None:
        row: dict[str, str] = {
            "width": "1000",
            "height": "2000",
            "ordered_quantity": "2",
            "op_type": "00",
            "quantity_unit_name": "ケース",
            "stat_quantity": "0",
            "ordered_stat_quantity": "99",
        }
        compute_op_calculated_fields(row)
        unit_price, amount = resolve_unit_and_amount_values(row)
        self.assertEqual(unit_price, row["case_lot_square"])
        self.assertEqual(amount, "99")

    def test_op_logic_works_without_olap_calc_columns_in_response(self) -> None:
        """calc列(42/43/44)をOLAPレスポンスに含めなくてもOP区分ロジックが動作する。"""
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {
                        "5": "得意先",
                        "6": "1405331",
                        "9": "Z001",
                        "16": "商品",
                        "19": "3.000",
                        "21": "枚",
                        "22": "1.111",
                        "23": "2.222",
                        "24": "1000",
                        "25": "500",
                        "37": "1000",
                        "38": "2000",
                        "40": "02",
                    }
                }
            }
        }
        rows = extract_r1_rows(response)
        self.assertEqual(rows[0]["op_type"], "02")
        self.assertTrue(rows[0]["op02_square"])
        self.assertTrue(rows[0]["op02_total_square"])
        unit_price, amount = resolve_unit_and_amount_values(rows[0])
        self.assertEqual(unit_price, rows[0]["op02_square"])
        self.assertEqual(amount, rows[0]["op02_total_square"])


class UnitDisplayFormattingTest(unittest.TestCase):
    """単価/金額列の小数第3位丸め・ゼロ空白テスト。"""

    def _make_op02_pages(self, width: str, height: str, qty: str) -> list[dict]:
        """extract_r1_rows を経由してOP02計算済み行からページを構築する。"""
        response = {
            "ResponseData": {
                "R1List": {
                    "1": {
                        "6": "X001",
                        "9": "V001",
                        "16": "商品A",
                        "19": qty,
                        "21": "枚",
                        "22": "0",
                        "23": "0",
                        "24": "0",
                        "25": "0",
                        "37": width,
                        "38": height,
                        "40": "02",
                    }
                }
            }
        }
        rows = extract_r1_rows(response)
        from datetime import date as _date
        return build_voucher_pages(rows, today=_date(2026, 1, 1))

    def test_unit_price_rounded_to_3dp(self) -> None:
        """1382mm x 1000mm / 1000000 = 1.382 → 1.382㎡"""
        pages = self._make_op02_pages("1382", "1000", "1")
        detail = pages[0]["details"][0]
        self.assertEqual(detail["unit_price"], "1.382㎡")
        self.assertEqual(detail["unit_price_display"], "1.382㎡")

    def test_unit_price_rounds_up_at_4th_decimal(self) -> None:
        """528mm x 591mm / 1000000 = 0.311928 → 0.312㎡"""
        pages = self._make_op02_pages("528", "591", "1")
        detail = pages[0]["details"][0]
        val = detail["unit_price"]
        self.assertTrue(val.endswith("㎡"))
        core = val[:-1]
        parts = core.split(".")
        self.assertLessEqual(len(parts[1]) if len(parts) > 1 else 0, 3)

    def test_unit_price_zero_is_blank(self) -> None:
        """単価が0の場合は空白。"""
        rows = [
            {
                "6": "X001",
                "9": "V001",
                "16": "商品A",
                "stat_quantity": "0",
                "ordered_stat_quantity": "0",
                "op_type": "01",
                "quantity_unit_name": "枚",
                "ordered_quantity": "1",
            }
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["unit_price"], "")
        self.assertEqual(detail["unit_price_display"], "")

    def test_amount_zero_is_blank(self) -> None:
        """金額が0の場合は空白。"""
        rows = [
            {
                "6": "X001",
                "9": "V001",
                "16": "商品A",
                "stat_quantity": "1.5",
                "ordered_stat_quantity": "0",
                "op_type": "01",
                "quantity_unit_name": "枚",
                "ordered_quantity": "1",
            }
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["amount"], "")
        self.assertEqual(detail["amount_display"], "")

    def test_star_row_unit_price_remains_blank(self) -> None:
        """*行の単価は引き続き空白のまま。"""
        rows = [
            {"6": "X001", "9": "V001", "16": "*", "stat_quantity": "9.999", "ordered_stat_quantity": "9.999"},
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["unit_price"], "")
        self.assertEqual(detail["amount"], "")

    def test_op01_fallback_unit_price_rounded_to_3dp(self) -> None:
        """OP01フォールバック値(02時平米)も小数第3位で丸められる。"""
        rows = [
            {
                "6": "X001",
                "9": "V001",
                "16": "商品A",
                "stat_quantity": "0",
                "ordered_stat_quantity": "0",
                "op_type": "01",
                "quantity_unit_name": "枚",
                "ordered_quantity": "1",
                "op02_square": "1.382483",
                "op02_total_square": "1.382483",
            }
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["unit_price"], "1.382㎡")
        self.assertEqual(detail["amount"], "1.382㎡")

    def test_op01_fallback_value_zero_is_blank(self) -> None:
        """OP01フォールバック先(02時平米)も0なら空白になる。"""
        rows = [
            {
                "6": "X001",
                "9": "V001",
                "16": "商品A",
                "stat_quantity": "0",
                "ordered_stat_quantity": "0",
                "op_type": "01",
                "quantity_unit_name": "枚",
                "ordered_quantity": "1",
                "op02_square": "0",
                "op02_total_square": "0",
            }
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertEqual(detail["unit_price"], "")
        self.assertEqual(detail["amount"], "")

    def test_note_lines_not_affected_by_zero_blank(self) -> None:
        """note_lines（摘要列）はゼロ空白化の対象外。"""
        rows = [
            {
                "6": "X001",
                "9": "V001",
                "16": "商品A",
                "stat_quantity": "1.0",
                "ordered_stat_quantity": "1.0",
                "op_type": "01",
                "quantity_unit_name": "枚",
                "ordered_quantity": "1",
                "sales_unit_price": "1000",
                "purchase_unit_price": "500",
                "delivery_short_name": "",
                "detail_instruction_type": "",
            }
        ]
        from datetime import date as _date
        pages = build_voucher_pages(rows, today=_date(2026, 1, 1))
        detail = pages[0]["details"][0]
        self.assertTrue(len(detail["note_lines"]) > 0)
        self.assertTrue(any("1,000" in line for line in detail["note_lines"]))

    def test_unit_display_max_3dp(self) -> None:
        """アプリ計算値が4桁以上でも表示は最大3桁。"""
        from app.voucher_data_mapper import _format_unit_display
        result = _format_unit_display("1.382483")
        self.assertEqual(result, "1.382㎡")

    def test_unit_display_rounding(self) -> None:
        """0.705312 → 0.705㎡, 0.31188 → 0.312㎡"""
        from app.voucher_data_mapper import _format_unit_display
        self.assertEqual(_format_unit_display("0.705312"), "0.705㎡")
        self.assertEqual(_format_unit_display("0.31188"), "0.312㎡")

    def test_unit_display_trailing_zero_stripped(self) -> None:
        """1.000 → 末尾ゼロなし → 1㎡"""
        from app.voucher_data_mapper import _format_unit_display
        self.assertEqual(_format_unit_display("1.000"), "1㎡")

    def test_unit_display_zero_returns_empty(self) -> None:
        """0 → 空文字"""
        from app.voucher_data_mapper import _format_unit_display
        self.assertEqual(_format_unit_display("0"), "")
        self.assertEqual(_format_unit_display("0.000"), "")
        self.assertEqual(_format_unit_display(""), "")


def _resolver_row(**overrides: str) -> dict[str, str]:
    row = {
        "op_type": "",
        "quantity_unit_name": "",
        "stat_quantity": "1.111",
        "ordered_stat_quantity": "2.222",
        "case_lot_square": "9.999",
        "op02_square": "3.333",
        "op02_total_square": "4.444",
    }
    row.update(overrides)
    return row


def _sample_response() -> object:
    text = (PROJECT_ROOT / "docs" / "olap2" / "04_データ取得_レスポンス.txt").read_text(encoding="utf-8")
    return json.loads(text.split("\n\n", 1)[1])


def _result_status_row() -> dict[str, object]:
    return {
        "DesignGroup": None,
        "Index": 0,
        "MessageName": 0,
        "MessageParams": None,
        "OutputLog": {},
        "PropertyName": "",
        "RData": None,
    }


if __name__ == "__main__":
    unittest.main()
