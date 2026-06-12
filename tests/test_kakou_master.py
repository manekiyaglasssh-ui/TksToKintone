from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from app.kakou_master import (
    IMPORT_ENCODINGS,
    KAKOU_MASTER_HEADERS,
    CsvEncodingError,
    read_csv_with_auto_encoding,
)

_SAMPLE_ROW: dict[str, str] = {
    "メーカー識別掛率集計コード": "MK0300",
    "メーカー識別コード": "MK",
    "掛率集計コード": "0300",
    "掛率集計名称": "エッチング",
    "掛率集計略称": "エッチ",
    "加工名": "エッチング加工",
    "得意先1": "A社",
    "得意先2": "",
    "得意先3": "",
    "得意先4": "",
}


def _write_csv(path: Path, rows: list[dict[str, str]], encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=KAKOU_MASTER_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in KAKOU_MASTER_HEADERS})


class ReadCsvAutoEncodingTest(unittest.TestCase):
    def test_read_csv_utf8_sig(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            _write_csv(path, [_SAMPLE_ROW], "utf-8-sig")

            rows, encoding = read_csv_with_auto_encoding(path)

            self.assertEqual(encoding, "utf-8-sig")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["掛率集計コード"], "0300")
            self.assertEqual(rows[0]["掛率集計名称"], "エッチング")

    def test_read_csv_utf8_no_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            _write_csv(path, [_SAMPLE_ROW], "utf-8")

            rows, encoding = read_csv_with_auto_encoding(path)

            # utf-8-sig is tried first and succeeds (reads utf-8 without BOM correctly)
            self.assertIn(encoding, ("utf-8-sig", "utf-8"))
            self.assertEqual(rows[0]["掛率集計コード"], "0300")

    def test_read_csv_cp932(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            _write_csv(path, [_SAMPLE_ROW], "cp932")

            rows, encoding = read_csv_with_auto_encoding(path)

            self.assertEqual(encoding, "cp932")
            self.assertEqual(rows[0]["掛率集計コード"], "0300")

    def test_read_csv_shift_jis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            _write_csv(path, [_SAMPLE_ROW], "shift_jis")

            rows, encoding = read_csv_with_auto_encoding(path)

            self.assertIn(encoding, ("cp932", "shift_jis"))
            self.assertEqual(rows[0]["掛率集計コード"], "0300")

    def test_read_csv_japanese_content_preserved_cp932(self) -> None:
        """CP932で保存された日本語が正しく読み込まれることを確認。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            row = dict(_SAMPLE_ROW)
            row["掛率集計名称"] = "エッチング（加工）"
            row["加工名"] = "エッチング加工品"
            row["得意先1"] = "まねきや硝子"
            _write_csv(path, [row], "cp932")

            rows, encoding = read_csv_with_auto_encoding(path)

            self.assertEqual(encoding, "cp932")
            self.assertEqual(rows[0]["掛率集計名称"], "エッチング（加工）")
            self.assertEqual(rows[0]["加工名"], "エッチング加工品")
            self.assertEqual(rows[0]["得意先1"], "まねきや硝子")

    def test_read_csv_multiple_rows(self) -> None:
        """複数行のCSVが正しく読み込まれることを確認。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.csv"
            rows = [
                {**_SAMPLE_ROW, "掛率集計コード": "0300", "掛率集計名称": "エッチング"},
                {**_SAMPLE_ROW, "掛率集計コード": "0400", "掛率集計名称": "DM-10"},
                {**_SAMPLE_ROW, "掛率集計コード": "0500", "掛率集計名称": "広幅"},
            ]
            _write_csv(path, rows, "cp932")

            result, encoding = read_csv_with_auto_encoding(path)

            self.assertEqual(encoding, "cp932")
            self.assertEqual(len(result), 3)
            self.assertEqual(result[1]["掛率集計コード"], "0400")
            self.assertEqual(result[2]["掛率集計名称"], "広幅")

    def test_all_encodings_fail_raises_csv_encoding_error(self) -> None:
        """どの文字コードでも読めないバイト列は CsvEncodingError になる。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.bin"
            # 0x81 0x3F: CP932/Shift-JISでは先頭バイト(0x81)の後に
            # 不正な2バイト目(0x3F < 0x40)が続くシーケンス。
            # UTF-8/UTF-8-sig でも無効なため、すべてのエンコーディングで失敗する。
            path.write_bytes(b"\x81\x3f" * 4)

            with self.assertRaises(CsvEncodingError) as ctx:
                read_csv_with_auto_encoding(path)

            error = ctx.exception
            self.assertIsInstance(error.tried_encodings, list)
            self.assertEqual(error.tried_encodings, IMPORT_ENCODINGS)

    def test_csv_encoding_error_message_contains_all_encodings(self) -> None:
        """エラーメッセージに試した文字コードがすべて含まれる。"""
        tried = ["utf-8-sig", "utf-8", "cp932", "shift_jis"]
        error = CsvEncodingError(tried)
        msg = str(error)

        for enc in tried:
            self.assertIn(enc, msg)
        self.assertEqual(error.tried_encodings, tried)

    def test_csv_encoding_error_is_value_error(self) -> None:
        """CsvEncodingError は ValueError のサブクラス。"""
        error = CsvEncodingError(["utf-8-sig"])
        self.assertIsInstance(error, ValueError)

    def test_import_encodings_constant_order(self) -> None:
        """IMPORT_ENCODINGS は utf-8-sig 優先の順で定義されている。"""
        self.assertEqual(IMPORT_ENCODINGS[0], "utf-8-sig")
        self.assertIn("cp932", IMPORT_ENCODINGS)
        self.assertIn("shift_jis", IMPORT_ENCODINGS)


if __name__ == "__main__":
    unittest.main()
