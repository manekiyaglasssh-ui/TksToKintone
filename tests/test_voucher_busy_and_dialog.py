"""処理中表示（busy/進捗）とPDF作成完了ダイアログ表示設定のテスト。

- OLAP取得/PDF作成の開始で進捗バーが表示され、完了・エラーで消えることを確認する。
- UI全体を setEnabled(False) しないことを確認する。
- PDF作成完了ダイアログを ON/OFF で切り替えられ、OFF でもステータス表示は出ることを確認する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@contextmanager
def _temp_home():
    previous = os.environ.get("TKS_TO_KINTONE_HOME")
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
        try:
            yield Path(temp_dir)
        finally:
            if previous is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = previous


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class BusyAndDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._home = _temp_home()
        self._home.__enter__()
        from app.voucher_window import VoucherWindow

        self.win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(self.win.deleteLater)

    def tearDown(self) -> None:
        self._home.__exit__(None, None, None)

    # ── 処理中表示（busy）────────────────────────────────────────────────
    def test_busy_shows_and_hides(self) -> None:
        self.assertFalse(self.win._busy_progress.isVisible() if self.win.isVisible() else self.win._busy_progress.isVisibleTo(self.win))
        self.win._set_busy("OLAP取得中...", context="test")
        # busy中はカウンタが増え、進捗バーは表示対象になる（不定進捗 range=0,0）。
        self.assertEqual(self.win._busy_counter, 1)
        self.assertEqual(self.win._busy_progress.minimum(), 0)
        self.assertEqual(self.win._busy_progress.maximum(), 0)
        self.win._clear_busy(context="test")
        self.assertEqual(self.win._busy_counter, 0)

    def test_busy_counter_survives_nested(self) -> None:
        self.win._set_busy("PDF作成中...", context="a")
        self.win._set_busy("印刷ジョブを追加中...", context="b")
        self.assertEqual(self.win._busy_counter, 2)
        self.win._clear_busy(context="b")
        # まだ処理が残っているので0にはならない。
        self.assertEqual(self.win._busy_counter, 1)
        self.win._clear_busy(context="a")
        self.assertEqual(self.win._busy_counter, 0)

    def test_olap_fetch_sets_and_clears_busy(self) -> None:
        rw = self.win._new_input_row
        rw.order_input.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(self.win, "_build_print_data", return_value=data), \
                mock.patch.object(self.win, "_cache_row_olap"):
            rw.refetch_button.click()
        # 完了後は必ずカウンタ0（進捗バー非表示）。
        self.assertEqual(self.win._busy_counter, 0)

    def test_busy_cleared_on_error(self) -> None:
        rw = self.win._new_input_row
        rw.order_input.setText("9999999")
        with mock.patch.object(self.win, "_build_print_data", side_effect=RuntimeError("boom")), \
                mock.patch("app.voucher_window.QMessageBox.critical"):
            rw.refetch_button.click()
        self.assertEqual(self.win._busy_counter, 0)

    def test_does_not_disable_whole_ui(self) -> None:
        self.win._set_busy("処理中...", context="test")
        # UI全体（centralWidget/テーブル/ウィンドウ）は無効化しない。
        self.assertTrue(self.win.centralWidget().isEnabled())
        self.assertTrue(self.win._table.isEnabled())
        self.win._clear_busy(context="test")

    # ── PDF作成完了ダイアログ ────────────────────────────────────────────
    def _set_dialog_pref(self, enabled: bool) -> None:
        from app import voucher_settings

        settings = voucher_settings.load_voucher_printer_settings()
        voucher_settings.save_voucher_printer_settings(
            voucher_settings.VoucherPrinterSettings(
                **{**settings.__dict__, "show_pdf_created_dialog": enabled}
            )
        )

    def test_pdf_created_dialog_shown_when_on(self) -> None:
        self._set_dialog_pref(True)
        self.assertTrue(self.win._pdf_created_dialog_enabled())
        with mock.patch("app.voucher_window.QMessageBox.information") as info, \
                mock.patch.object(self.win, "_set_print_status") as status:
            self.win._notify_pdf_created("PDFを作成しました:\nout.pdf", status="OK")
        info.assert_called_once()
        self.assertTrue(status.called)

    def test_pdf_created_dialog_suppressed_when_off(self) -> None:
        self._set_dialog_pref(False)
        self.assertFalse(self.win._pdf_created_dialog_enabled())
        # OFFでもステータス表示は更新するがダイアログは出ない。
        with mock.patch("app.voucher_window.QMessageBox.information") as info, \
                mock.patch.object(self.win, "_set_print_status") as status:
            self.win._notify_pdf_created("PDFを作成しました:\nout.pdf", status="OK")
        info.assert_not_called()
        self.assertTrue(status.called)

    def test_select_pdf_result_respects_dialog_off(self) -> None:
        """選択PDF作成でもOFF設定でダイアログを抑止する（成功のみの場合）。"""
        self._set_dialog_pref(False)
        with mock.patch("app.voucher_window.QMessageBox.information") as info, \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(self.win, "_set_print_status"):
            self.win._show_select_pdf_result([Path("a.pdf")], [])
        info.assert_not_called()
        warn.assert_not_called()

    def test_select_pdf_result_error_dialog_always_shown(self) -> None:
        """失敗があればOFF設定でも警告ダイアログは必ず表示する。"""
        self._set_dialog_pref(False)
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(self.win, "_set_print_status"):
            self.win._show_select_pdf_result([Path("a.pdf")], ["9999999"])
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
