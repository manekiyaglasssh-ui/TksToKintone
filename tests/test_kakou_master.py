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

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False

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


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class KakouMasterDialogLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_initial_size_column_widths_and_maximize_button(self) -> None:
        from app.gui import KakouMasterDialog

        with tempfile.TemporaryDirectory() as tmp:
            master_path = Path(tmp) / "master.csv"
            backup_dir = Path(tmp) / "backup"
            _write_csv(master_path, [_SAMPLE_ROW], "utf-8-sig")
            dialog = KakouMasterDialog(master_path, backup_dir)
            self.addCleanup(dialog.deleteLater)

            screen = dialog.screen() or QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.assertLessEqual(dialog.width(), available.width())
                self.assertLessEqual(dialog.minimumWidth(), available.width())
            self.assertGreaterEqual(dialog.minimumWidth(), min(760, dialog.width()))
            self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
            idx = KAKOU_MASTER_HEADERS.index("得意先4")
            self.assertGreaterEqual(dialog.table.columnWidth(idx), 120)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class KakouMasterResetToDefaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_dialog_with_default(self, tmp: Path):
        import app.settings_service as settings_service
        from app.gui import KakouMasterDialog

        master_path = Path(tmp) / "master.csv"
        backup_dir = Path(tmp) / "backup"
        # 現在のマスタ（ユーザー編集済み相当）
        _write_csv(master_path, [dict(_SAMPLE_ROW, 加工名="ユーザー編集")], "utf-8-sig")
        # 初期CSV（2件）
        default_csv = Path(tmp) / "default.csv"
        default_rows = [
            dict(_SAMPLE_ROW, 掛率集計コード="0010", 加工名="初期A"),
            dict(_SAMPLE_ROW, 掛率集計コード="0020", 加工名="初期B"),
        ]
        _write_csv(default_csv, default_rows, "utf-8-sig")

        self._orig_default = settings_service.find_default_kakou_master_csv
        settings_service.find_default_kakou_master_csv = lambda: default_csv
        self.addCleanup(
            setattr, settings_service, "find_default_kakou_master_csv", self._orig_default
        )

        dialog = KakouMasterDialog(master_path, backup_dir)
        self.addCleanup(dialog.deleteLater)
        return dialog, master_path, backup_dir

    def test_reset_button_exists(self) -> None:
        from PySide6.QtWidgets import QPushButton

        with tempfile.TemporaryDirectory() as tmp:
            dialog, _master, _backup = self._make_dialog_with_default(Path(tmp))
            labels = [
                b.text() for b in dialog.findChildren(QPushButton)
            ]
            self.assertIn("初期値に戻す", labels)

    def test_reset_no_does_nothing(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        with tempfile.TemporaryDirectory() as tmp:
            dialog, master_path, _backup = self._make_dialog_with_default(Path(tmp))
            orig = QMessageBox.question
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
            self.addCleanup(setattr, QMessageBox, "question", orig)

            dialog._reset_to_default()
            from app.kakou_master import load_master

            rows = load_master(master_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["加工名"], "ユーザー編集")

    def test_reset_yes_overwrites_and_backs_up(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        with tempfile.TemporaryDirectory() as tmp:
            dialog, master_path, backup_dir = self._make_dialog_with_default(Path(tmp))
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
            QMessageBox.information = staticmethod(lambda *a, **k: None)
            QMessageBox.critical = staticmethod(lambda *a, **k: None)
            self.addCleanup(setattr, QMessageBox, "question", QMessageBox.question)

            dialog._reset_to_default()

            from app.kakou_master import load_master

            rows = load_master(master_path)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["加工名"] for r in rows}, {"初期A", "初期B"})
            # 一覧が再読み込みされている。
            self.assertEqual(dialog.table.rowCount(), 2)
            # 上書き前のバックアップが作成されている。
            backups = list(backup_dir.glob("kakou_master_*.csv.bak"))
            self.assertTrue(backups, "バックアップが作成されていない")


if __name__ == "__main__":
    unittest.main()
