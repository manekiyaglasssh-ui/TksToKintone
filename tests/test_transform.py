from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.preview_state import PreviewState
from tks_to_kintone.csv_io import read_csv_dicts, write_quoted_csv
from tks_to_kintone.transform import (
    SOURCE_HEADERS,
    calculate_total_weight,
    transform_files,
    transform_rows,
)


ROOT = Path(__file__).resolve().parents[1]


def _source_row(**overrides: str) -> dict[str, str]:
    row = {header: "" for header in SOURCE_HEADERS}
    row.update(
        {
            "受注No": "1000",
            "受注行No": "1",
            "硝/加工": "1",
            "追加区分": "0",
            "納品書行No": "1",
            "商品コード": "CFL8",
            "商品名称": "FL8 四方 磨き",
            "㎡": "0.106",
            "発注先コード": "11111",
        }
    )
    row.update(overrides)
    return row


class TransformSampleTest(unittest.TestCase):
    def test_transform_matches_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.csv"

            transform_files(
                ROOT / "docs/samples/素板抽出ロジックCSVサンプル.csv",
                ROOT / "docs/samples/加工抽出ロジックCSVサンプル.csv",
                output,
            )

            expected = (ROOT / "docs/samples/outputTksToKintone_sample.csv").read_bytes()
            self.assertEqual(output.read_bytes(), expected)


class TotalWeightTest(unittest.TestCase):
    def test_calculates_total_weight_from_area_and_thickness(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.106", "硝子厚み": "8"})), "2.12")
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.135", "硝子厚み": "8"})), "2.70")
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.15", "硝子厚み": "6"})), "2.25")

    def test_total_weight_rounds_half_up_to_two_decimals(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.107", "硝子厚み": "8"})), "2.14")

    def test_total_weight_is_blank_when_area_blank(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "", "硝子厚み": "8"})), "")

    def test_total_weight_is_blank_when_thickness_blank(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.106", "硝子厚み": ""})), "")

    def test_total_weight_is_blank_when_area_is_not_numeric(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "abc", "硝子厚み": "8"})), "")

    def test_total_weight_is_blank_when_thickness_is_not_numeric(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.106", "硝子厚み": "abc"})), "")

    def test_total_weight_zero_area_outputs_zero_fixed_decimals(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0", "硝子厚み": "8"})), "0.00")

    def test_total_weight_zero_thickness_outputs_zero_fixed_decimals(self) -> None:
        self.assertEqual(calculate_total_weight(_source_row(**{"㎡": "0.106", "硝子厚み": "0"})), "0.00")

    def test_transform_rows_outputs_total_weight_column(self) -> None:
        rows = transform_rows([_source_row(**{"㎡": "0.106", "商品コード": "CFL8", "硝子厚み": "8"})], [])
        self.assertEqual(rows[0]["総重量"], "2.12")

    def test_registration_csv_outputs_total_weight_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            glass_csv = base / "glass.csv"
            processing_csv = base / "processing.csv"
            output_csv = base / "output.csv"

            write_quoted_csv(
                glass_csv,
                SOURCE_HEADERS,
                [_source_row(**{"㎡": "0.135", "商品コード": "CFL8", "硝子厚み": "8"})],
            )
            write_quoted_csv(processing_csv, SOURCE_HEADERS, [])

            transform_files(glass_csv, processing_csv, output_csv)

            rows = read_csv_dicts(output_csv)
            self.assertIn("総重量", rows[0])
            self.assertEqual(rows[0]["総重量"], "2.70")

    def test_preview_rows_keep_total_weight_for_printing(self) -> None:
        state = PreviewState(rows=[_source_row(**{"総重量": "2.12"})])
        rows = state.build_registration_rows([])
        self.assertEqual(rows[0]["総重量"], "2.12")


if __name__ == "__main__":
    unittest.main()
