"""伝票PDF 合計欄（上下2段）の算出ロジックのテスト。

上段 = Σ(売上単価 × 受注数量)
下段 = Σ(仕入単価 × 受注数量)
金額列の合計ではなく、各明細行の元データから計算する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestCalculateUnitPriceTotals(unittest.TestCase):
    def test_sales_total_is_sum_of_sales_unit_price_times_qty(self) -> None:
        """1. 売上単価 × 受注数量 の合計が上段に表示される。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {"name": "A", "sales_unit_price": "430", "purchase_unit_price": "316", "ordered_quantity": "1"},
            {"name": "B", "sales_unit_price": "250", "purchase_unit_price": "224", "ordered_quantity": "1"},
            {"name": "C", "sales_unit_price": "40", "purchase_unit_price": "34", "ordered_quantity": "1"},
        ]
        sales_total, _ = calculate_unit_price_totals(rows)
        self.assertEqual(sales_total, 720.0)

    def test_purchase_total_is_sum_of_purchase_unit_price_times_qty(self) -> None:
        """2. 仕入単価 × 受注数量 の合計が下段に表示される。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {"name": "A", "sales_unit_price": "430", "purchase_unit_price": "316", "ordered_quantity": "1"},
            {"name": "B", "sales_unit_price": "250", "purchase_unit_price": "224", "ordered_quantity": "1"},
            {"name": "C", "sales_unit_price": "40", "purchase_unit_price": "34", "ordered_quantity": "1"},
        ]
        _, purchase_total = calculate_unit_price_totals(rows)
        self.assertEqual(purchase_total, 574.0)

    def test_does_not_use_amount_column(self) -> None:
        """3. 表の金額列(amount)の合計を使っていないこと。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {
                "name": "A",
                "sales_unit_price": "430",
                "purchase_unit_price": "316",
                "ordered_quantity": "1",
                # 金額列はわざと別の値にしておく。これが使われたら合計が変わる。
                "amount": "99999㎡",
            },
        ]
        sales_total, purchase_total = calculate_unit_price_totals(rows)
        self.assertEqual(sales_total, 430.0)
        self.assertEqual(purchase_total, 316.0)

    def test_uses_raw_ordered_quantity_even_when_qty_display_has_star(self) -> None:
        """4. 数量表示に「*」が付く行でも、元の受注数量で計算されること。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {
                "name": "A",
                "sales_unit_price": "100",
                "purchase_unit_price": "80",
                "ordered_quantity": "120",
                # 表示用の数量は「*」付き整形済みだが計算には使わない。
                "qty": "120 *",
            },
        ]
        sales_total, purchase_total = calculate_unit_price_totals(rows)
        self.assertEqual(sales_total, 100 * 120)
        self.assertEqual(purchase_total, 80 * 120)

    def test_blank_and_non_numeric_treated_as_zero(self) -> None:
        """5. 空欄・非数値は 0 扱いになること。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {"name": "A", "sales_unit_price": "", "purchase_unit_price": "abc", "ordered_quantity": "1"},
            {"name": "B", "sales_unit_price": "500", "purchase_unit_price": "300", "ordered_quantity": ""},
            {"name": "C", "sales_unit_price": "200", "purchase_unit_price": "150"},
        ]
        sales_total, purchase_total = calculate_unit_price_totals(rows)
        # A: 単価が空/非数値→0、B: 受注数量空→0、C: 受注数量キー無し→0
        self.assertEqual(sales_total, 0.0)
        self.assertEqual(purchase_total, 0.0)

    def test_multiple_rows_with_quantity_summed_correctly(self) -> None:
        """6. 複数行の合計が正しく計算されること（数量2以上を含む）。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {"name": "A", "sales_unit_price": "430", "purchase_unit_price": "316", "ordered_quantity": "2"},
            {"name": "B", "sales_unit_price": "1,250", "purchase_unit_price": "1,000", "ordered_quantity": "3"},
        ]
        sales_total, purchase_total = calculate_unit_price_totals(rows)
        self.assertEqual(sales_total, 430 * 2 + 1250 * 3)
        self.assertEqual(purchase_total, 316 * 2 + 1000 * 3)

    def test_star_rows_excluded(self) -> None:
        """対象外行（name == '*'）や空行は合計しないこと。"""
        from app.voucher_service import calculate_unit_price_totals

        rows = [
            {"name": "A", "sales_unit_price": "430", "purchase_unit_price": "316", "ordered_quantity": "1"},
            {"name": "*", "sales_unit_price": "999", "purchase_unit_price": "999", "ordered_quantity": "1"},
        ]
        sales_total, purchase_total = calculate_unit_price_totals(rows)
        self.assertEqual(sales_total, 430.0)
        self.assertEqual(purchase_total, 316.0)

    def test_format_total_rounds_to_current_spec(self) -> None:
        """小数が出る場合は現行仕様（3桁区切り・小数2桁）に整形されること。"""
        from app.voucher_service import _format_total

        self.assertEqual(_format_total(720.0), "720")
        self.assertEqual(_format_total(1234567.0), "1,234,567")
        self.assertEqual(_format_total(720.5), "720.50")


if __name__ == "__main__":
    unittest.main()
