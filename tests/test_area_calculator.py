"""登録用 ㎡ / 総㎡ 算出（app.area_calculator）のテスト。

OP区分による条件分岐が伝票作成処理（resolve_unit_and_amount_values）と一致すること、
OP区分が無いときに固定値 1 を入れないこと、CSV出力と kintone登録で同じ値になることを確認する。
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path

from app.area_calculator import apply_area_values, calculate_area_values, format_area_3
from app.preview_state import PreviewState
from app.voucher_data_mapper import (
    compute_op_calculated_fields,
    resolve_unit_and_amount_values,
)
from tks_to_kintone.transform import OUTPUT_HEADERS, SOURCE_HEADERS, transform_rows

ROOT = Path(__file__).resolve().parents[1]


def _reg_row(**overrides: str) -> dict[str, str]:
    """登録行（日本語表示名キー）のひな形。"""
    row = {
        "商品名称": "品",
        "OP区分": "",
        "数量単位名称": "",
        "㎡": "1.111",        # 統計数量
        "総㎡": "2.222",       # 受注統計数量
        "W寸法": "1000",
        "H寸法": "2000",
        "受注数量": "3",
    }
    row.update(overrides)
    return row


class CalculateAreaValuesTest(unittest.TestCase):
    def test_op02_uses_width_height_area(self) -> None:
        """OP区分02は W×H から ㎡ / 総㎡ を算出する（統計数量1固定を使わない）。"""
        row = _reg_row(OP区分="02", W寸法="1000", H寸法="2000", 受注数量="3")
        m2, total = calculate_area_values(row)
        self.assertEqual(m2, "2")          # 1000*2000/1000000
        self.assertEqual(total, "6")       # 2 * 3
        self.assertNotEqual(m2, "1")

    def test_op00_non_case_uses_stat_quantity(self) -> None:
        """OP区分00（枚）は統計数量 / 受注統計数量をそのまま使う。"""
        row = _reg_row(OP区分="00", 数量単位名称="枚", **{"㎡": "0.5", "総㎡": "1.5"})
        self.assertEqual(calculate_area_values(row), ("0.5", "1.5"))

    def test_op01_zero_stat_falls_back_to_op02(self) -> None:
        """OP区分01で統計数量が0なら W×H 由来の面積へフォールバックする。"""
        row = _reg_row(OP区分="01", W寸法="1000", H寸法="2000", 受注数量="3", **{"㎡": "0", "総㎡": "0"})
        m2, total = calculate_area_values(row)
        self.assertEqual(m2, "2")
        self.assertEqual(total, "6")

    def test_missing_op_returns_blank_not_one(self) -> None:
        """OP区分が取得できない場合は固定値1ではなく空欄を返す（要件7）。"""
        row = _reg_row(OP区分="")
        self.assertEqual(calculate_area_values(row), ("", ""))

    def test_star_row_returns_blank(self) -> None:
        """商品名称が '*' の明細行は ㎡ / 総㎡ を空欄にする。"""
        row = _reg_row(OP区分="02", 商品名称="*")
        self.assertEqual(calculate_area_values(row), ("", ""))

    def test_matches_voucher_resolver_for_each_op(self) -> None:
        """各OP区分で伝票作成処理の resolve_unit_and_amount_values と同じ値になる。"""
        cases = [
            {"OP区分": "00", "数量単位名称": "ケース"},
            {"OP区分": "00", "数量単位名称": "枚"},
            {"OP区分": "01", "数量単位名称": "枚"},
            {"OP区分": "02", "数量単位名称": "枚"},
        ]
        for override in cases:
            with self.subTest(**override):
                row = _reg_row(**override)
                m2, total = calculate_area_values(row)

                alias = {
                    "op_type": row["OP区分"],
                    "quantity_unit_name": row["数量単位名称"],
                    "stat_quantity": row["㎡"],
                    "ordered_stat_quantity": row["総㎡"],
                    "width": row["W寸法"],
                    "height": row["H寸法"],
                    "ordered_quantity": row["受注数量"],
                }
                compute_op_calculated_fields(alias)
                expected_m2, expected_total = resolve_unit_and_amount_values(alias)
                self.assertEqual(Decimal(m2), Decimal(expected_m2))
                self.assertEqual(Decimal(total), Decimal(expected_total))

    def test_apply_area_values_mutates_row(self) -> None:
        row = _reg_row(OP区分="02", W寸法="1000", H寸法="2000", 受注数量="3")
        returned = apply_area_values(row)
        self.assertIs(returned, row)
        self.assertEqual(row["㎡"], "2.000")
        self.assertEqual(row["総㎡"], "6.000")


class FormatArea3Test(unittest.TestCase):
    def test_rounds_to_three_decimals(self) -> None:
        self.assertEqual(format_area_3("1.382483"), "1.382")
        self.assertEqual(format_area_3("0.705312"), "0.705")
        self.assertEqual(format_area_3("0.31188"), "0.312")

    def test_integer_gets_three_decimals(self) -> None:
        self.assertEqual(format_area_3("2"), "2.000")

    def test_half_up_rounding(self) -> None:
        self.assertEqual(format_area_3("0.0005"), "0.001")

    def test_blank_and_none_stay_blank(self) -> None:
        self.assertEqual(format_area_3(""), "")
        self.assertEqual(format_area_3("   "), "")
        self.assertEqual(format_area_3(None), "")

    def test_non_numeric_returns_blank(self) -> None:
        self.assertEqual(format_area_3("abc"), "")

    def test_op02_area_rounded_to_three_decimals(self) -> None:
        """OP区分02のW×H面積も小数第3位までで丸められる。"""
        # 1303 * 1061 / 1000000 = 1.382483 -> 1.382
        row = _reg_row(OP区分="02", W寸法="1303", H寸法="1061", 受注数量="1")
        apply_area_values(row)
        self.assertEqual(row["㎡"], "1.382")
        self.assertEqual(row["総㎡"], "1.382")

    def test_op00_area_rounded_to_three_decimals(self) -> None:
        """OP区分00系も小数第3位までで丸められる。"""
        row = _reg_row(OP区分="00", 数量単位名称="枚", **{"㎡": "0.31188", "総㎡": "0.705312"})
        apply_area_values(row)
        self.assertEqual(row["㎡"], "0.312")
        self.assertEqual(row["総㎡"], "0.705")

    def test_star_row_stays_blank_after_rounding(self) -> None:
        row = _reg_row(OP区分="02", 商品名称="*")
        apply_area_values(row)
        self.assertEqual(row["㎡"], "")
        self.assertEqual(row["総㎡"], "")

    def test_missing_op_stays_blank_not_one(self) -> None:
        row = _reg_row(OP区分="")
        apply_area_values(row)
        self.assertEqual(row["㎡"], "")
        self.assertEqual(row["総㎡"], "")


class TransformOpKubunTest(unittest.TestCase):
    def test_headers_include_op_kubun(self) -> None:
        self.assertIn("OP区分", SOURCE_HEADERS)
        self.assertIn("OP区分", OUTPUT_HEADERS)

    def test_op_kubun_carried_through_transform(self) -> None:
        """OP区分が抽出行から登録用出力行まで保持される。"""
        glass = {header: "" for header in SOURCE_HEADERS}
        glass.update({"受注No": "1000", "受注行No": "1", "硝/加工": "1", "OP区分": "02", "発注先コード": "11111"})
        rows = transform_rows([glass], [])
        self.assertEqual(rows[0]["OP区分"], "02")

    def test_missing_op_kubun_does_not_break_transform(self) -> None:
        """OP区分列が無い旧抽出CSVでも検証エラーにならない（要件11）。"""
        headers_without_op = [h for h in SOURCE_HEADERS if h != "OP区分"]
        glass = {header: "" for header in headers_without_op}
        glass.update({"受注No": "1000", "受注行No": "1", "硝/加工": "1", "発注先コード": "11111"})
        rows = transform_rows([glass], [])
        self.assertEqual(rows[0]["OP区分"], "")


class BuildRegistrationRowsAreaTest(unittest.TestCase):
    def _state(self, **row_overrides: str) -> PreviewState:
        row = {
            "受注No": "1000",
            "硝/加工": "1",
            "商品名称": "品",
            "OP区分": "02",
            "W寸法": "1000",
            "H寸法": "2000",
            "受注数量": "3",
            "㎡": "1",
            "総㎡": "1",
        }
        row.update(row_overrides)
        return PreviewState(rows=[row])

    def test_build_registration_rows_sets_area_from_op(self) -> None:
        """登録用データ生成時点で ㎡ / 総㎡ が OP区分に応じて確定する（1固定にならない）。

        小数第3位までで四捨五入された値（2 -> 2.000）になる。
        """
        rows = self._state().build_registration_rows([])
        self.assertEqual(rows[0]["㎡"], "2.000")
        self.assertEqual(rows[0]["総㎡"], "6.000")

    def test_build_registration_rows_blank_when_no_op(self) -> None:
        rows = self._state(**{"OP区分": ""}).build_registration_rows([])
        self.assertEqual(rows[0]["㎡"], "")
        self.assertEqual(rows[0]["総㎡"], "")

    def test_csv_and_kintone_use_same_area(self) -> None:
        """build_registration_rows を共通入口とするため CSV用と kintone用で ㎡ / 総㎡ が一致する。"""
        state = self._state()
        csv_rows = state.build_registration_rows([])
        kintone_rows = state.build_registration_rows([])
        self.assertEqual(csv_rows[0]["㎡"], kintone_rows[0]["㎡"])
        self.assertEqual(csv_rows[0]["総㎡"], kintone_rows[0]["総㎡"])


class OlapTemplateOpKubunTest(unittest.TestCase):
    def test_templates_request_op_kubun(self) -> None:
        for name in ("kakou_request_template.json", "soba_request_template.json"):
            path = ROOT / "docs" / "olap" / name
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            names = [c.get("OLAP表示名") for c in payload.get("R1List", [])]
            self.assertIn("OP区分", names, f"{name} に OP区分 が含まれていません")


if __name__ == "__main__":
    unittest.main()
