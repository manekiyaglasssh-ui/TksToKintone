"""登録前確認画面の「CSV作成」機能のテスト。

- csv_processor の純粋関数（Qt不要）
- 登録前確認ダイアログのUI・出力先設定・CSV作成挙動（Qt使用、offscreen）

CSV作成は kintone へ送信せず、登録ボタン押下時と同じ登録用データを確認用に書き出す。
"""
from __future__ import annotations

import csv
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.csv_processor import (
    REGISTRATION_EXPORT_HEADERS,
    export_registration_records_to_csv,
    unique_timestamp_csv_path,
)

try:
    from PySide6.QtCore import QDate, QSettings
    from PySide6.QtWidgets import QApplication, QPushButton

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6が無い環境
    _QT_AVAILABLE = False


# ── テスト用マスタ・行 ────────────────────────────────────────

_MASTER = [
    {
        "掛率集計コード": "0300", "掛率集計名称": "エッチング", "加工名": "エッチング",
        "得意先1": "A社向け", "得意先2": "", "得意先3": "", "得意先4": "",
        "メーカー識別掛率集計コード": "MK0300", "メーカー識別コード": "MK", "掛率集計略称": "",
    },
    {
        "掛率集計コード": "0400", "掛率集計名称": "広幅", "加工名": "広幅",
        "得意先1": "", "得意先2": "B社向け", "得意先3": "", "得意先4": "",
        "メーカー識別掛率集計コード": "MK0400", "メーカー識別コード": "MK", "掛率集計略称": "",
    },
]


def _row(order_no: str, row_type: str = "2", code: str = "0300", name: str = "エッチング") -> dict[str, str]:
    return {
        "受注No": order_no,
        "硝/加工": row_type,
        "商品名称": "品",
        "掛率集計コード": code,
        "掛率集計名称": name,
        "W寸法": "1303",
        "H寸法": "1061",
        "仕上日": "2026-06-01",
        "出荷区分": "AM",
    }


# ── csv_processor 純粋関数テスト ─────────────────────────────


class RegistrationExportFunctionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_headers_include_kakou_fields(self) -> None:
        """出力列に登録用フィールド（加工名・加工mm・加工種類・得意先選択）が含まれる。"""
        self.assertIn("受注No", REGISTRATION_EXPORT_HEADERS)
        self.assertIn("加工名", REGISTRATION_EXPORT_HEADERS)
        self.assertIn("加工mm", REGISTRATION_EXPORT_HEADERS)
        self.assertIn("加工種類", REGISTRATION_EXPORT_HEADERS)
        self.assertIn("得意先選択", REGISTRATION_EXPORT_HEADERS)

    def test_export_writes_utf8_bom(self) -> None:
        """UTF-8 BOM付き（utf-8-sig）で出力される。"""
        path = self.dir / "out.csv"
        export_registration_records_to_csv([_row("1000")], path)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))

    def test_export_writes_all_rows(self) -> None:
        """渡した登録データ全件が出力される。"""
        path = self.dir / "out.csv"
        rows = [_row("1000"), _row("1000"), _row("1001")]
        export_registration_records_to_csv(rows, path)
        text = path.read_text(encoding="utf-8-sig")
        data = list(csv.DictReader(text.splitlines()))
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["受注No"], "1000")
        self.assertEqual(data[2]["受注No"], "1001")

    def test_export_keeps_empty_cells(self) -> None:
        """空欄もそのまま出力する。"""
        path = self.dir / "out.csv"
        export_registration_records_to_csv([{"受注No": "1000"}], path)
        text = path.read_text(encoding="utf-8-sig")
        data = list(csv.DictReader(text.splitlines()))
        self.assertEqual(data[0]["仕上日"], "")

    def test_unique_timestamp_path_no_collision(self) -> None:
        path = unique_timestamp_csv_path(self.dir, "20260618_134522")
        self.assertEqual(path.name, "20260618_134522.csv")

    def test_unique_timestamp_path_appends_index(self) -> None:
        (self.dir / "20260618_134522.csv").write_text("x")
        path = unique_timestamp_csv_path(self.dir, "20260618_134522")
        self.assertEqual(path.name, "20260618_134522_2.csv")


# ── ダイアログUIテスト ───────────────────────────────────────


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class PreviewCsvExportDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # QSettings を一時ディレクトリに隔離し、ユーザー設定を汚さない。
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def _dialog(self, rows):
        from app.gui import RegistrationPreviewDialog

        dlg = RegistrationPreviewDialog(
            rows=rows,
            shukka_options=["AM", "PM"],
            master=_MASTER,
            customer_labels={"得意先1": "得意先1", "得意先2": "得意先2", "得意先3": "得意先3", "得意先4": "得意先4"},
            preview_color_theme="light",
        )
        self.addCleanup(dlg.deleteLater)
        return dlg

    def _read_csv(self, path: Path):
        text = path.read_text(encoding="utf-8-sig")
        return list(csv.DictReader(text.splitlines()))

    def _create_csv(self, dlg) -> Path:
        dlg.csv_output_dir_edit.setText(str(self.dir))
        with mock.patch("app.gui.QMessageBox.question", return_value=None), \
             mock.patch("app.gui.QMessageBox.warning") as warn, \
             mock.patch("app.gui.QMessageBox.critical") as crit:
            dlg._on_create_csv()
            self.assertFalse(warn.called, "予期しない警告が出ました")
            self.assertFalse(crit.called, "予期しないエラーが出ました")
        files = list(self.dir.glob("*.csv"))
        self.assertEqual(len(files), 1)
        return files[0]

    # ── UI ───────────────────────────────────────────────

    def test_has_csv_output_widgets(self) -> None:
        """CSV出力先欄・参照ボタン・CSV作成ボタンがある。"""
        dlg = self._dialog([_row("1000")])
        self.assertTrue(hasattr(dlg, "csv_output_dir_edit"))
        self.assertEqual(dlg.csv_browse_button.text(), "参照")
        self.assertEqual(dlg.csv_create_button.text(), "CSV作成")

    def test_register_and_cancel_buttons_remain(self) -> None:
        """登録 / 登録キャンセル の既存ボタンが残っている。"""
        dlg = self._dialog([_row("1000")])
        self.assertEqual(dlg.register_button.text(), "登録")
        self.assertEqual(dlg.cancel_button.text(), "登録キャンセル")

    # ── 出力先設定 ───────────────────────────────────────

    def test_browse_sets_and_saves_dir(self) -> None:
        """参照で選んだフォルダが表示され、設定に保存される。"""
        dlg = self._dialog([_row("1000")])
        with mock.patch("app.gui.QFileDialog.getExistingDirectory", return_value=str(self.dir)):
            dlg._browse_csv_output_dir()
        self.assertEqual(dlg.csv_output_dir_edit.text(), str(self.dir))
        saved = QSettings("Manekiya", "TksToKintone").value("registration_preview/csv_output_dir")
        self.assertEqual(str(saved), str(self.dir))

    def test_restore_saved_dir(self) -> None:
        """再表示時に保存済みフォルダが復元される。"""
        settings = QSettings("Manekiya", "TksToKintone")
        settings.setValue("registration_preview/csv_output_dir", str(self.dir))
        settings.sync()
        dlg = self._dialog([_row("1000")])
        self.assertEqual(dlg.csv_output_dir_edit.text(), str(self.dir))

    # ── CSV作成 ──────────────────────────────────────────

    def test_create_csv_makes_file(self) -> None:
        dlg = self._dialog([_row("1000")])
        path = self._create_csv(dlg)
        self.assertTrue(path.exists())

    def test_filename_timestamp_format(self) -> None:
        dlg = self._dialog([_row("1000")])
        path = self._create_csv(dlg)
        self.assertRegex(path.name, r"^\d{8}_\d{6}(_\d+)?\.csv$")

    def test_output_is_utf8_bom(self) -> None:
        dlg = self._dialog([_row("1000")])
        path = self._create_csv(dlg)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_all_rows_output_even_when_filtered(self) -> None:
        """絞り込み中でも非表示行を含めて全件出力される。"""
        rows = [_row("1000"), _row("1000"), _row("1001")]
        dlg = self._dialog(rows)
        dlg._filter_edit.setText("1001")  # 表示は1001のみ
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(len(data), 3)

    def test_shiage_change_reflected_to_all_same_order_rows(self) -> None:
        """仕上日変更が同一受注No全行へ反映された状態で出力される。"""
        rows = [_row("1000"), _row("1000"), _row("1001")]
        dlg = self._dialog(rows)
        dlg._shiage_widgets[0].setDate(QDate(2026, 12, 31))
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(data[0]["仕上日"], "2026-12-31")
        self.assertEqual(data[1]["仕上日"], "2026-12-31")
        self.assertNotEqual(data[2]["仕上日"], "2026-12-31")

    def test_shukka_change_reflected_to_all_same_order_rows(self) -> None:
        """出荷区分変更が同一受注No全行へ反映された状態で出力される。"""
        rows = [_row("1000"), _row("1000"), _row("1001")]
        dlg = self._dialog(rows)
        dlg._shukka_widgets[0].setCurrentText("PM")
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(data[0]["出荷区分"], "PM")
        self.assertEqual(data[1]["出荷区分"], "PM")

    def test_customer_change_reflected_to_all_same_order_rows(self) -> None:
        """得意先選択変更が同一受注No全行へ反映された状態で出力される（加工名で確認）。"""
        rows = [_row("1000"), _row("1000")]
        dlg = self._dialog(rows)
        widget = dlg._customer_widgets[0]
        widget.setCurrentIndex(widget.findData("得意先1"))
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(data[0]["加工名"], "A社向け")
        self.assertEqual(data[1]["加工名"], "A社向け")

    def test_kakou_name_and_type_match_registration(self) -> None:
        """加工名・加工種類が登録時（registration_rows）と同じ値で出力される。"""
        rows = [_row("1000")]
        dlg = self._dialog(rows)
        expected = dlg.build_registration_records_from_preview()[0]
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(data[0]["加工名"], expected["加工名"])
        self.assertEqual(data[0]["加工種類"], expected["加工種類"])
        self.assertEqual(data[0]["加工mm"], expected["加工mm"])

    def test_kakou_type_and_customer_match_kintone_records(self) -> None:
        """CSVの 加工種類 / 得意先選択 がKintone送信用records（build_registration_rows）と一致する。"""
        rows = [_row("1000")]
        dlg = self._dialog(rows)
        widget = dlg._customer_widgets[0]
        widget.setCurrentIndex(widget.findData("得意先1"))
        expected = dlg.build_registration_records_from_preview()[0]
        path = self._create_csv(dlg)
        data = self._read_csv(path)
        self.assertEqual(data[0]["加工種類"], expected["加工種類"])
        self.assertEqual(data[0]["得意先選択"], expected["得意先選択"])
        self.assertEqual(data[0]["得意先選択"], "得意先1")

    # ── 登録しないこと ───────────────────────────────────

    def test_create_csv_does_not_accept_dialog(self) -> None:
        """CSV作成では登録（accept）されず、登録状態が変更されない。"""
        dlg = self._dialog([_row("1000")])
        with mock.patch.object(dlg, "accept") as accept:
            self._create_csv(dlg)
            self.assertFalse(accept.called)
        self.assertNotEqual(dlg.result(), 1)  # Accepted=1

    def test_create_csv_does_not_mutate_rows(self) -> None:
        """CSV作成では入力行（登録対象データ）が変更されない。"""
        rows = [_row("1000")]
        before = dict(rows[0])
        dlg = self._dialog(rows)
        self._create_csv(dlg)
        self.assertEqual(rows[0], before)

    # ── エラー処理 ───────────────────────────────────────

    def test_warns_when_output_dir_empty(self) -> None:
        """出力先未設定時に警告される。"""
        dlg = self._dialog([_row("1000")])
        dlg.csv_output_dir_edit.setText("")
        with mock.patch("app.gui.QMessageBox.warning") as warn:
            dlg._on_create_csv()
            self.assertTrue(warn.called)
        self.assertEqual(list(self.dir.glob("*.csv")), [])

    def test_warns_when_output_dir_missing(self) -> None:
        """存在しない出力先はエラー表示される。"""
        dlg = self._dialog([_row("1000")])
        dlg.csv_output_dir_edit.setText(str(self.dir / "no_such_dir"))
        with mock.patch("app.gui.QMessageBox.warning") as warn:
            dlg._on_create_csv()
            self.assertTrue(warn.called)

    def test_warns_when_dir_not_writable(self) -> None:
        """書き込み不可フォルダ時にエラー表示される。"""
        dlg = self._dialog([_row("1000")])
        dlg.csv_output_dir_edit.setText(str(self.dir))
        with mock.patch("app.gui.os.access", return_value=False), \
             mock.patch("app.gui.QMessageBox.warning") as warn:
            dlg._on_create_csv()
            self.assertTrue(warn.called)

    def test_warns_when_no_records(self) -> None:
        """登録対象0件時に警告される。"""
        dlg = self._dialog([])
        dlg.csv_output_dir_edit.setText(str(self.dir))
        with mock.patch("app.gui.QMessageBox.warning") as warn:
            dlg._on_create_csv()
            self.assertTrue(warn.called)

    def test_create_button_disabled_when_no_records(self) -> None:
        """登録対象データが0件のときCSV作成ボタンが無効。"""
        dlg = self._dialog([])
        self.assertFalse(dlg.csv_create_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
