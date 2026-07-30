"""伝票作成・印刷画面の起動を軽くするための遅延化を検証する（要件2・3・4）。

- 画面起動時に統合設定/印刷設定/伝票設定ダイアログを生成しない。
- 画面起動時にプリンタ一覧取得を呼ばない。
- 画面起動時に指図書編集画面を import しない。
- 「設定」ボタン押下時には統合設定ダイアログが開く。
- 起動時の主要ステップの所要時間ログ（perf_counter）が出力される。

Qt ウィジェットを使うため offscreen プラットフォームで実行する。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 が無い環境
    _QT_AVAILABLE = False

if _QT_AVAILABLE:
    import app.voucher_window as vw
    from app.voucher_window import VoucherWindow


def _make_window() -> "VoucherWindow":
    return VoucherWindow(olap_login_id="id", olap_password="pw")


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 が利用できない環境")
class VoucherWindowStartupLazyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_dialogs_not_created_on_startup(self) -> None:
        """起動時に統合設定/印刷設定/伝票設定ダイアログを生成しない（要件3）。"""
        with mock.patch.object(vw, "CombinedVoucherSettingsDialog") as combined, \
                mock.patch.object(vw, "VoucherPrinterSettingsDialog") as printer, \
                mock.patch.object(vw, "VoucherPrintSettingsDialog") as print_dlg:
            win = _make_window()
            try:
                combined.assert_not_called()
                printer.assert_not_called()
                print_dlg.assert_not_called()
            finally:
                win.deleteLater()

    def test_printer_enumeration_not_called_on_startup(self) -> None:
        """起動時にプリンタ一覧取得（list_available_printer_names）を呼ばない（要件3）。"""
        with mock.patch.object(
            vw.voucher_print_service, "list_available_printer_names"
        ) as list_printers:
            win = _make_window()
            try:
                list_printers.assert_not_called()
            finally:
                win.deleteLater()

    def test_edit_window_not_imported_on_startup(self) -> None:
        """起動時に指図書編集画面（voucher_edit_window）を import しない（要件3）。

        既存プロセスでは事前 import 済みのことがあるため、独立プロセスで検証する。
        """
        code = (
            "import os; os.environ['QT_QPA_PLATFORM']='offscreen';\n"
            "import sys\n"
            "from PySide6.QtWidgets import QApplication\n"
            "app=QApplication.instance() or QApplication([])\n"
            "from app.voucher_window import VoucherWindow\n"
            "w=VoucherWindow(olap_login_id='id', olap_password='pw')\n"
            "assert 'app.voucher_edit_window' not in sys.modules, '起動時にvoucher_edit_windowがimportされた'\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)

    def test_settings_button_opens_combined_dialog(self) -> None:
        """「設定」ボタン押下時には統合設定ダイアログが開く（要件2/5）。"""
        win = _make_window()
        try:
            class _FakeDialog:
                def __init__(self, *args, **kwargs):
                    _FakeDialog.created += 1

                def select_tab(self, *_a, **_k):
                    pass

                def exec(self):
                    return QDialog.DialogCode.Rejected

            _FakeDialog.created = 0
            with mock.patch.object(vw, "CombinedVoucherSettingsDialog", _FakeDialog):
                win._on_display_settings()
            self.assertEqual(_FakeDialog.created, 1)
        finally:
            win.deleteLater()

    def test_startup_emits_timing_logs(self) -> None:
        """起動時に主要ステップの所要時間ログが出力される（要件2）。"""
        with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
            win = _make_window()
            win.deleteLater()
        text = "\n".join(logs.output)
        for event in (
            "voucher_window_init_started",
            "voucher_window_setup_ui_started",
            "voucher_window_setup_ui_finished",
            "voucher_window_load_settings_finished",
            "voucher_window_init_elapsed_ms",
            "voucher_window_combined_settings_lazy_skipped_on_startup",
            "voucher_window_printer_settings_lazy_skipped_on_startup",
            # OLAP認証・Kintone接続確認・プリンタ列挙は起動時に実行しない（要件2）。
            "voucher_window_olap_auth_deferred",
            "voucher_window_kintone_check_deferred",
            "voucher_window_printer_enum_deferred",
            # 期限切れOLAPキャッシュ掃除は表示後へ遅延する（要件2）。
            "voucher_window_cache_cleanup_deferred",
            # 保存済み一覧の復元は show 後へ遅延する（要件1）。
            "voucher_window_saved_rows_restore_deferred_to_after_show",
            "voucher_window_ready_before_saved_rows_restore",
            # 遅延復元中の進捗表示ウィジェットは起動時に生成される（要件1）。
            "voucher_window_saved_rows_progress_created",
            # 各ステップの elapsed_ms も記録される（要件2）。
            "voucher_window_load_settings_elapsed_ms",
            "voucher_window_setup_ui_elapsed_ms",
            "voucher_window_apply_column_visibility_elapsed_ms",
        ):
            self.assertIn(event, text)

    def test_saved_rows_restore_not_run_during_init(self) -> None:
        """保存済み一覧の復元は __init__ 中には実行されない（要件1）。"""
        with mock.patch.object(vw.VoucherWindow, "_restore_saved_records") as restore:
            win = _make_window()
            try:
                # __init__ 完了時点では復元は呼ばれていない（show後の singleShot 待ち）。
                restore.assert_not_called()
            finally:
                win.deleteLater()

    def _make_window_with_records(self, count: int):
        """保存済みレコードを count 件用意した状態で VoucherWindow を作る（復元は未実行）。"""
        import json
        import tempfile

        prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = tmp.name
        work = os.path.join(tmp.name, "work")
        os.makedirs(work, exist_ok=True)
        records = [
            {"updated_at": "2026-06-0%dT09:00:00" % ((i % 9) + 1),
             "order_no": str(1000 + i), "kintone_status": "未登録"}
            for i in range(count)
        ]
        with open(os.path.join(work, "voucher_records.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "saved_at": "2026-06-03T12:00:00", "records": records}, fh)

        def _restore_home() -> None:
            if prev_home is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = prev_home
            tmp.cleanup()

        self.addCleanup(_restore_home)
        win = _make_window()
        self.addCleanup(win.deleteLater)
        return win

    def test_saved_rows_restore_runs_after_show_chunked(self) -> None:
        """保存済み一覧の復元は show 後にチャンクで実行され、完了後に全件反映される（要件1）。"""
        win = self._make_window_with_records(20)
        # __init__ 直後は未復元。
        self.assertFalse(win._saved_rows_restored)
        self.assertEqual(len(win._rows), 0)
        with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
            # イベントループを回すとチャンク復元が進行し完了する。
            for _ in range(60):
                self.app.processEvents()
                if win._saved_rows_restored:
                    break
        self.assertTrue(win._saved_rows_restored)
        self.assertEqual(len(win._rows), 20)
        text = "\n".join(logs.output)
        self.assertIn("voucher_window_saved_rows_restore_progress_started", text)
        self.assertIn("voucher_window_saved_rows_restore_chunk_started", text)
        self.assertIn("voucher_window_saved_rows_restore_progress_finished", text)
        self.assertIn("voucher_window_saved_rows_restore_completed_after_show", text)

    def _pump_until(self, win, predicate, *, limit: int = 400) -> None:
        """ワーカースレッドの結果反映を待ちながらイベントループを回す。"""
        import time as _time

        for _ in range(limit):
            self.app.processEvents()
            if predicate():
                return
            _time.sleep(0.005)
        self.app.processEvents()

    def test_saved_rows_restore_progress_shown_then_hidden(self) -> None:
        """復元開始で進捗表示され、総件数がmaximumになり、完了で非表示になる（要件1・2）。"""
        win = self._make_window_with_records(20)
        # 開始前は進捗非表示。
        self.assertTrue(win._saved_rows_progress.isHidden())
        win._restore_saved_records_after_show()  # begin（ワーカー起動＋busy表示）
        self.assertFalse(win._saved_rows_progress.isHidden())
        # ワーカーが読み込み終わると総件数が maximum に反映される。
        self._pump_until(win, lambda: win._saved_rows_progress.maximum() == 20)
        self.assertEqual(win._saved_rows_progress.maximum(), 20)
        # 残りを流し込むと完了し、進捗は非表示に戻る。
        self._pump_until(win, lambda: win._saved_rows_restored)
        self.assertTrue(win._saved_rows_restored)
        self.assertTrue(win._saved_rows_progress.isHidden())

    def test_saved_rows_restore_zero_records_no_progress(self) -> None:
        """0件のときは進捗表示を出さず即完了する（要件1）。"""
        win = _make_window()
        try:
            win._restore_saved_records_after_show()
            self._pump_until(win, lambda: win._saved_rows_restored)
            self.assertTrue(win._saved_rows_restored)
            self.assertTrue(win._saved_rows_progress.isHidden())
        finally:
            win.deleteLater()

    def test_saved_rows_restore_disables_then_enables_controls(self) -> None:
        """復元中は主要操作を無効化し、完了後に再有効化する（要件2）。"""
        win = self._make_window_with_records(20)
        win._restore_saved_records_after_show()  # begin → 操作無効化
        self.assertFalse(win._display_settings_button.isEnabled())
        self.assertFalse(win._select_print_button.isEnabled())
        for _ in range(60):
            self.app.processEvents()
            if win._saved_rows_restored:
                break
        # 完了後は操作が再有効化される。
        self.assertTrue(win._display_settings_button.isEnabled())

    def test_ensure_saved_rows_restored_completes_chunked_restore(self) -> None:
        """バッチ復元の途中に外部操作ガードが呼ばれると、残りを同期完了させる（要件2）。"""
        win = self._make_window_with_records(20)
        win._restore_saved_records_after_show()  # begin（ワーカー起動）
        # 最初の10件バッチが表示されるまで待つ（全件完了前）。
        self._pump_until(
            win, lambda: 0 < len(win._rows) < 20 and win._deferred_restore_active
        )
        self.assertGreater(len(win._rows), 0)
        # QtバックエンドによってはprocessEvents() 1回で0msタイマーが連続消化される。
        # 途中なら競合ガードが残りを同期完了し、完了済みなら冪等に維持する。
        win._ensure_saved_rows_restored()
        self.assertTrue(win._saved_rows_restored)
        self.assertEqual(len(win._rows), 20)
        self.assertTrue(win._saved_rows_progress.isHidden())
        self.assertTrue(win._display_settings_button.isEnabled())

    def test_saved_rows_controls_enabled_after_first_batch(self) -> None:
        """最初の10件表示後、全件完了前に主要操作が有効化される（要件3）。"""
        win = self._make_window_with_records(25)
        win._restore_saved_records_after_show()  # begin → 操作無効化
        self.assertFalse(win._display_settings_button.isEnabled())
        # 最初のバッチが表示された時点（全件未完了）で操作が有効化される。
        self._pump_until(win, lambda: win._deferred_restore_first_batch_done)
        self.assertTrue(win._deferred_restore_first_batch_done)
        self.assertFalse(win._saved_rows_restored)  # まだ全件完了していない
        self.assertTrue(win._display_settings_button.isEnabled())
        self.assertGreaterEqual(len(win._rows), 10)
        self.assertLess(len(win._rows), 25)
        # 残りはバックグラウンドで継続し、最終的に全件復元される。
        self._pump_until(win, lambda: win._saved_rows_restored)
        self.assertEqual(len(win._rows), 25)

    def test_saved_rows_worker_result_ignored_after_close(self) -> None:
        """close中にワーカー結果が来ても無視される（要件4）。"""
        win = self._make_window_with_records(20)
        win._restore_saved_records_after_show()  # begin（ワーカー起動）
        # close 相当: 生存フラグを落として結果破棄を指示する。
        win._alive = False
        win._saved_rows_worker_cancelled = True
        win._on_saved_records_loaded([{"order_no": "9999"}])
        self.assertEqual(len(win._rows), 0)
        self.assertFalse(win._deferred_restore_active)

    def test_saved_rows_batches_of_ten(self) -> None:
        """25件は10/10/5のバッチで進捗が 10→20→25 と更新される（要件2）。"""
        win = self._make_window_with_records(25)
        with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
            win._restore_saved_records_after_show()
            self._pump_until(win, lambda: win._saved_rows_restored)
        self.assertTrue(win._saved_rows_restored)
        self.assertEqual(len(win._rows), 25)
        text = "\n".join(logs.output)
        self.assertIn("voucher_window_saved_rows_worker_started", text)
        self.assertIn("voucher_window_saved_rows_worker_batch_loaded", text)
        self.assertIn("voucher_window_saved_rows_first_batch_displayed", text)
        self.assertIn("voucher_window_saved_rows_controls_enabled_after_first_batch", text)
        self.assertIn("voucher_window_saved_rows_worker_finished", text)
        # 進捗更新が 10 / 25, 20 / 25, 25 / 25 と出る。
        self.assertIn("'done': 10, 'total': 25", text)
        self.assertIn("'done': 20, 'total': 25", text)
        self.assertIn("'done': 25, 'total': 25", text)

    def test_restore_saved_rows_suppresses_table_updates(self) -> None:
        """保存済み一覧の一括追加中はテーブル再描画を抑制する（要件2）。"""
        import json
        import tempfile

        prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        tmp = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = tmp.name
        work = os.path.join(tmp.name, "work")
        os.makedirs(work, exist_ok=True)
        records = [
            {"updated_at": "2026-06-0%dT09:00:00" % ((i % 9) + 1),
             "order_no": str(1000 + i), "kintone_status": "未登録"}
            for i in range(5)
        ]
        with open(os.path.join(work, "voucher_records.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "saved_at": "2026-06-03T12:00:00", "records": records}, fh)
        try:
            win = _make_window()
            try:
                calls: list[bool] = []
                original = win._table.setUpdatesEnabled

                def _spy(flag: bool) -> None:
                    calls.append(bool(flag))
                    original(flag)

                win._table.setUpdatesEnabled = _spy  # type: ignore[method-assign]
                with self.assertLogs("tks_to_kintone_app", level="INFO") as logs:
                    win._restore_saved_records()
                # 復元処理は setUpdatesEnabled(False)→(True) で再描画を一括化している。
                self.assertIn(False, calls)
                self.assertIn(True, calls)
                text = "\n".join(logs.output)
                self.assertIn("voucher_window_restore_rows_started", text)
                self.assertIn("voucher_window_restore_rows_finished", text)
                # bulk中は列表示反映が抑制され、復元後に1回だけ反映される（要件2）。
                self.assertIn("voucher_window_column_visibility_applied_once_after_bulk", text)
            finally:
                win.deleteLater()
        finally:
            if prev_home is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = prev_home
            tmp.cleanup()

    def test_olap_auth_not_called_on_startup(self) -> None:
        """起動時にOLAP認証（login_if_needed）が呼ばれない（要件2）。"""
        with mock.patch.object(
            vw.VoucherOlapService, "login_if_needed"
        ) as login_if_needed:
            win = _make_window()
            try:
                login_if_needed.assert_not_called()
            finally:
                win.deleteLater()


if __name__ == "__main__":
    unittest.main()
