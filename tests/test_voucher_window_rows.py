"""VoucherWindow（受注一覧形式）の動的テスト。

QApplication を offscreen で起動し、実ウィジェットの構成と行ごとの
PDF作成・印刷処理が呼ばれることを検証する。
"""
from __future__ import annotations

import os
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QDateEdit,
        QLineEdit,
        QPushButton,
        QComboBox,
        QRadioButton,
        QScrollArea,
    )

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
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


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class _FakePrintWorker:
    def __init__(self) -> None:
        self.status_changed = _FakeSignal()
        self.request_sent = _FakeSignal()
        self.finished = _FakeSignal()
        self.error = _FakeSignal()
        self.cancel_called = False

    def cancel(self) -> None:
        self.cancel_called = True


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherWindowRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._test_home = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._test_home.name
        # notify_kintone_registration_completed() は Teams へ実HTTP送信する。
        # このクラスには同メソッドを直接呼ぶテストが多数あり、モックしないと
        # 既定の本番Webhook（東大阪）へ実通知が飛んでしまう。クラス全体で送信を無効化する。
        _teams_patch = mock.patch("app.voucher_window.post_teams_webhook")
        self._teams_post_mock = _teams_patch.start()
        self.addCleanup(_teams_patch.stop)

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home
        self._test_home.cleanup()

    def _make_window(self):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        # 保存済み一覧の復元は show 後のチャンク復元へ遅延したため、テストでは
        # 同期的に復元を確定させる（要件1）。
        win._ensure_saved_rows_restored()
        if not win._rows:
            win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        self.addCleanup(win.deleteLater)
        return win

    def _make_window_with_kintone(self, kintone_window):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(
            olap_login_id="id",
            olap_password="pw",
            kintone_window_provider=lambda: kintone_window,
        )
        # 遅延復元を同期的に確定させる（要件1）。
        win._ensure_saved_rows_restored()
        if not win._rows:
            win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def _remove_input_row_for_legacy_tests(win) -> None:
        rw = getattr(win, "_new_input_row", None)
        if rw is None:
            return
        logical_index = getattr(rw, "table_row_index", -1)
        if logical_index >= 0:
            win._table.removeRow(logical_index)
        win._new_input_row = None
        for row in win._rows:
            if row.table_row_index > logical_index:
                row.table_row_index -= 1

    def _find(self, rw, widget_type):
        return widget_type

    @staticmethod
    def _row_index_for_order(win, order_no: str) -> int:
        for rw in win._rows:
            if rw.order_input.text().strip() == order_no:
                return rw.table_row_index
        raise AssertionError(f"order row not found: {order_no}")

    @staticmethod
    def _row_for_order(win, order_no: str):
        for rw in win._rows:
            if rw.order_input.text().strip() == order_no:
                return rw
        raise AssertionError(f"order row not found: {order_no}")

    @staticmethod
    def _visible_order_numbers(win) -> list[str]:
        return [
            rw.order_input.text().strip()
            for rw in win._rows
            if not win._table.isRowHidden(rw.table_row_index)
        ]

    @staticmethod
    def _olap_row(order_no: str = "5218869", voucher_no: object = "Z737704") -> dict:
        return {
            "order_no": order_no,
            "customer_name": "得意先",
            "customer_code": "001",
            "voucher_no": voucher_no,
            "product_name": "商品",
            "ordered_quantity": "1",
            "quantity_unit_name": "枚",
            "details": [{"name": "商品"}],
        }

    @staticmethod
    def _mark_fetched(win, rw, order_no: str = "5218869") -> None:
        rw.order_input.setText(order_no)
        rw.cached_olap = {
            "pages": [{"order_no": order_no, "voucher_no": "Z1", "customer_name": "得意先", "details": [{"name": "商品"}]}],
            "raw_rows": [],
        }
        win._refresh_row_olap_state(rw)

    def test_initial_single_row(self) -> None:
        win = self._make_window()
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(len(win._rows), 1)

    def test_add_row_increases_count(self) -> None:
        win = self._make_window()
        win._on_add_row()
        self.assertEqual(win._table.rowCount(), 2)
        self.assertEqual(len(win._rows), 2)

    def test_startup_shows_only_new_input_row_without_data(self) -> None:
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(len(win._rows), 0)
        self.assertIsNotNone(win._new_input_row)
        self.assertEqual(win._new_input_row.order_input.text(), "")

    def test_new_input_row_is_first_and_only_order_olap_are_enabled(self) -> None:
        from app.voucher_window import COL_AMPM, COL_FINISH_DATE, COL_KINTONE, COL_PROCESS, COL_VOUCHER, VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        self.assertIsNotNone(rw)
        self.assertEqual(win._table.verticalHeader().visualIndex(rw.table_row_index), 0)
        self.assertFalse(rw.select_check.isEnabled())
        self.assertTrue(rw.order_input.isEnabled())
        self.assertFalse(rw.order_input.isReadOnly())
        self.assertEqual(rw.refetch_button.text(), "取得")
        self.assertTrue(rw.refetch_button.isEnabled())
        self.assertFalse(rw.date_edit.isEnabled())
        self.assertFalse(rw.finish_none_check.isEnabled())
        self.assertFalse(rw.ampm_none.isEnabled())
        for cb in list(rw.process_checks.values()) + list(rw.voucher_checks.values()):
            self.assertFalse(cb.isEnabled())
            self.assertFalse(cb.isChecked())
        self.assertFalse(rw.kintone_button.isEnabled())
        for column in (COL_FINISH_DATE, COL_AMPM, COL_PROCESS, COL_VOUCHER, COL_KINTONE):
            widget = win._table.cellWidget(rw.table_row_index, column)
            self.assertIsNotNone(widget)
            self.assertFalse(widget.isEnabled())
            self.assertEqual(widget.findChildren(QCheckBox), [])
            self.assertEqual(widget.findChildren(QPushButton), [])

    def test_new_input_row_is_excluded_from_selection_and_save(self) -> None:
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.select_check.setChecked(True)
        self.assertEqual(win._selected_indices(), [])
        win._set_all_rows_checked(True)
        self.assertFalse(rw.select_check.isChecked())
        win._save_records()
        saved = json.loads(win._records_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["records"], [])

    def test_new_input_row_fetch_success_adds_normal_row_and_clears_input(self) -> None:
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("1111111")
        data = {"pages": [{"order_no": "1111111", "voucher_no": "Z111"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertEqual(len(win._rows), 1)
        self.assertEqual(win._rows[0].order_input.text(), "1111111")
        self.assertIs(win._rows[0].cached_olap, data)
        self.assertEqual(rw.order_input.text(), "")
        self.assertIsNone(rw.cached_olap)
        self.assertEqual(win._table.verticalHeader().visualIndex(rw.table_row_index), 0)

    def test_new_input_row_button_dispatches_to_dedicated_handler(self) -> None:
        """新規入力行の「取得」ボタンは専用メソッドへ直接接続され、rwを渡さない（要件1・2）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("1394160")
        # ボタンは _on_fetch_new_input_row へ直結。lambda で rw を束ねないため、
        # 呼び出し引数は QPushButton.clicked の checked(bool) のみで rw は渡らない。
        with mock.patch.object(win, "_on_fetch_new_input_row") as new_handler, \
                mock.patch.object(win, "_on_refetch_existing_row") as existing_handler:
            rw.refetch_button.click()
        new_handler.assert_called_once()
        (args, _kwargs) = new_handler.call_args
        self.assertNotIn(rw, args)
        existing_handler.assert_not_called()

    def test_new_input_row_button_is_stable_reference(self) -> None:
        """新規入力行の受注No欄・取得ボタンは self に安定保持される（要件・実機対策）。"""
        from app.voucher_window import COL_REFETCH, COL_ORDER_NO, VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        self.assertIs(win._new_order_no_edit, rw.order_input)
        self.assertIs(win._new_fetch_button, rw.refetch_button)
        # テーブルセル内に表示されている実ウィジェットと同一であること。
        cell_btn = win._table.cellWidget(rw.table_row_index, COL_REFETCH)
        self.assertIn(win._new_fetch_button, cell_btn.findChildren(QPushButton))
        cell_edit = win._table.cellWidget(rw.table_row_index, COL_ORDER_NO)
        self.assertIn(win._new_order_no_edit, cell_edit.findChildren(QLineEdit))

    def test_new_input_row_reads_current_edit_after_redraw(self) -> None:
        """再描画（_apply_filters）後も、取得は現在表示中の受注No欄の値を読む（要件3・4・5）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        # 再描画を複数回起こしても新規入力行の参照は維持される。
        for _ in range(3):
            win._apply_filters()
        edit = win._new_order_no_edit
        self.assertIs(edit, win._new_input_row.order_input)
        edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data) as build, \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        build.assert_called_once_with(["1394160"])
        self.assertEqual(len(win._rows), 1)

    def test_new_input_row_hidden_by_filter_is_made_visible(self) -> None:
        """登録状態フィルターで隠れる場合でも、取得した通常行を可視化する（実機バグ修正）。"""
        from app.voucher_window import KINTONE_STATUS_COMPLETED, VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        # 取得直後の行は未登録なので、フィルターが「登録完了」だと隠れてしまう。
        win._status_filter.setCurrentText(KINTONE_STATUS_COMPLETED)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertEqual(len(win._rows), 1)
        created = win._rows[0]
        self.assertFalse(win._table.isRowHidden(created.table_row_index))
        self.assertEqual(win._status_filter.currentText(), "すべて")
        self.assertEqual(win._visible_row_count(), 1)

    def test_new_input_row_search_filter_reset_when_added_row_hidden(self) -> None:
        """検索欄の残存テキストで追加行が隠れる場合、検索欄を解除して可視化する。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._order_search_edit.setText("0000000")  # 追加受注Noに一致しない検索条件
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        created = win._rows[0]
        self.assertFalse(win._table.isRowHidden(created.table_row_index))
        self.assertEqual(win._order_search_edit.text(), "")

    def test_new_input_row_debug_log_records_events(self) -> None:
        """デバッグ表示ON時、新規入力行取得の診断ログが work/debug に出力される（要件9）。"""
        from app.voucher_window import KINTONE_STATUS_COMPLETED, VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            win._status_filter.setCurrentText(KINTONE_STATUS_COMPLETED)
            win._new_order_no_edit.setText("1394160")
            data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
            with mock.patch.object(win, "_build_print_data", return_value=data), \
                    mock.patch.object(win, "_cache_row_olap"):
                win._new_fetch_button.click()
            logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
            self.assertTrue(logs)
            events = [
                json.loads(line)["event"]
                for line in logs[0].read_text(encoding="utf-8").splitlines()
            ]
        self.assertIn("new_row_fetch_button_clicked", events)
        self.assertIn("new_row_append_success", events)
        self.assertIn("filter_reset_done", events)
        self.assertIn("added_row_found_after_redraw", events)

    def test_new_input_row_no_debug_log_when_disabled(self) -> None:
        """デバッグ表示OFF時は診断ログを出力しない。"""
        from app.voucher_window import VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "0"}), \
                mock.patch.object(
                    __import__("app.voucher_window", fromlist=["VoucherWindow"]).QSettings,
                    "value",
                    return_value="0",
                ):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            win._new_order_no_edit.setText("1394160")
            data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
            with mock.patch.object(win, "_build_print_data", return_value=data), \
                    mock.patch.object(win, "_cache_row_olap"):
                win._new_fetch_button.click()
        logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
        self.assertEqual(logs, [])

    def test_new_input_row_handler_reads_stripped_order_no(self) -> None:
        """新規入力行専用処理は入力欄の実値を strip して取得に使う（要件2）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("  1394160  ")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data) as build, \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        build.assert_called_once_with(["1394160"])
        self.assertEqual(win._rows[0].order_input.text(), "1394160")

    def test_new_input_row_stays_out_of_rows_after_fetch(self) -> None:
        """取得成功後も新規入力行は _rows に含まれず1行だけ維持される（要件6・7）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertIs(win._new_input_row, rw)
        self.assertNotIn(rw, win._rows)
        self.assertEqual(
            len([r for r in win._all_table_rows() if win._is_new_input_row(r)]), 1
        )

    def test_new_input_row_fetch_failure_keeps_input_and_adds_no_row(self) -> None:
        """OLAP取得失敗時は通常行を追加せず、受注No入力値も残す（要件10・11）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("9999999")
        with mock.patch.object(win, "_build_print_data", side_effect=RuntimeError("boom")), \
                mock.patch("app.voucher_window.QMessageBox.critical"):
            rw.refetch_button.click()
        self.assertEqual(len(win._rows), 0)
        self.assertEqual(rw.order_input.text(), "9999999")
        self.assertIsNone(rw.cached_olap)

    def test_new_input_row_duplicate_order_no_adds_no_row(self) -> None:
        """既存の通常行と重複する受注Noは取得せず通常行を追加しない（要件12）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        rw.order_input.setText("1234567")
        data = {"pages": [{"order_no": "1234567"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertEqual(len(win._rows), 1)
        # 同じ受注Noを再入力して取得しても通常行は増えず、OLAP取得も走らない。
        rw.order_input.setText("1234567")
        with mock.patch.object(win, "_build_print_data", return_value=data) as build, \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            rw.refetch_button.click()
        build.assert_not_called()
        warn.assert_called_once()
        self.assertEqual(len(win._rows), 1)

    # ── 実機診断強化（今回の改善: 直接接続・状態表示・ログ・強制表示）─────────────
    def test_new_input_row_button_directly_connected_to_handler(self) -> None:
        """新規入力行の取得ボタンは self._on_fetch_new_input_row へ直接接続される（要件1）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        with mock.patch.object(win, "_on_fetch_new_input_row") as handler, \
                mock.patch.object(win, "_on_refetch_row") as dispatcher:
            win._new_fetch_button.click()
        handler.assert_called_once()
        # ディスパッチャ(_on_refetch_row)は経由しない直接接続であること。
        dispatcher.assert_not_called()

    def test_new_input_row_button_still_direct_after_redraw(self) -> None:
        """再描画後も現在の取得ボタンは専用ハンドラへ直接接続されている（要件2）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        for _ in range(3):
            win._apply_filters()
        with mock.patch.object(win, "_on_fetch_new_input_row") as handler, \
                mock.patch.object(win, "_on_refetch_row") as dispatcher:
            win._new_fetch_button.click()
        handler.assert_called_once()
        dispatcher.assert_not_called()

    def test_new_input_row_reads_table_cell_value(self) -> None:
        """取得時に現在表示中のテーブルセル内 QLineEdit の値を読める（要件3・4）。"""
        from app.voucher_window import COL_ORDER_NO, VoucherWindow
        from PySide6.QtWidgets import QLineEdit

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        rw = win._new_input_row
        cell = win._table.cellWidget(rw.table_row_index, COL_ORDER_NO)
        cell_edit = cell.findChild(QLineEdit)
        # 保持参照とセル内 QLineEdit は一致すること（要件4）。
        self.assertIs(cell_edit, win._new_order_no_edit)
        cell_edit.setText("1394160")
        order_no, info = win._read_new_row_order_no()
        self.assertEqual(order_no, "1394160")
        self.assertEqual(info["raw_order_no_from_table_cell"], "1394160")
        self.assertTrue(info["self_and_cell_edit_match"])

    def test_new_input_row_reads_cell_value_even_if_reference_diverges(self) -> None:
        """保持参照とセル内 QLineEdit が不一致でも、セル側の値を読める（要件5）。"""
        from app.voucher_window import VoucherWindow
        from PySide6.QtWidgets import QLineEdit

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        # 保持参照だけ別の空 QLineEdit に差し替え、セル側とズレさせる。
        stray = QLineEdit()
        self.addCleanup(stray.deleteLater)
        win._new_order_no_edit = stray
        cell_edit = win._new_row_cell_line_edit()
        cell_edit.setText("1394160")
        order_no, info = win._read_new_row_order_no()
        self.assertEqual(order_no, "1394160")  # 画面に見えているセル値を採用
        self.assertFalse(info["self_and_cell_edit_match"])
        self.assertFalse(info["self_and_cell_value_match"])

    def test_new_input_row_status_label_shows_progress(self) -> None:
        """取得成功時、画面下部のステータスに行追加OKが表示される（要件2・6）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertIn("行追加OK", win._new_row_status_label.text())
        self.assertIn("1394160", win._new_row_status_label.text())

    def test_new_input_row_status_shows_empty_order_no(self) -> None:
        """受注No未入力時、ステータスに未入力が表示される（要件2・8）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("   ")
        with mock.patch("app.voucher_window.QMessageBox.warning"):
            win._new_fetch_button.click()
        self.assertIn("未入力", win._new_row_status_label.text())

    def test_new_input_row_status_shows_olap_failure(self) -> None:
        """OLAP取得失敗時、ステータスとメッセージにエラーが出る（要件5・12）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("9999999")
        with mock.patch.object(win, "_build_print_data", side_effect=RuntimeError("boom")), \
                mock.patch("app.voucher_window.QMessageBox.critical") as crit:
            win._new_fetch_button.click()
        self.assertIn("OLAP取得失敗", win._new_row_status_label.text())
        crit.assert_called_once()
        self.assertEqual(len(win._rows), 0)

    def test_new_input_row_status_shows_duplicate(self) -> None:
        """重複時、ステータスとメッセージに重複が出る（要件13）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1234567")
        data = {"pages": [{"order_no": "1234567"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        win._new_order_no_edit.setText("1234567")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            win._new_fetch_button.click()
        warn.assert_called_once()
        self.assertIn("重複", win._new_row_status_label.text())

    def test_new_input_row_version_label_visible_when_debug(self) -> None:
        """デバッグ表示ON時、新規行処理バージョン表示が見える（要件1）。"""
        from app.voucher_window import (
            NEW_ROW_FETCH_HANDLER,
            NEW_ROW_FETCH_VERSION,
            VoucherWindow,
        )

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
        self.assertTrue(win._new_row_version_label.isVisibleTo(win))
        self.assertIn(NEW_ROW_FETCH_VERSION, win._new_row_version_label.text())
        self.assertIn(NEW_ROW_FETCH_HANDLER, win._new_row_version_label.text())
        # 実機で修正反映を確認できるよう v4 / data-only-fetch であること（要件14）。
        self.assertEqual(NEW_ROW_FETCH_VERSION, "v4")
        self.assertEqual(NEW_ROW_FETCH_HANDLER, "data-only-fetch")
        self.assertIn("v4", win._new_row_version_label.text())
        self.assertIn("data-only-fetch", win._new_row_version_label.text())

    def test_new_input_row_log_records_click_and_return_reasons(self) -> None:
        """クリック・受注No読取・OLAP開始・return理由がログに残る（要件6・7・8・14）。"""
        from app.voucher_window import VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            win._new_order_no_edit.setText("1394160")
            data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
            with mock.patch.object(win, "_build_print_data", return_value=data), \
                    mock.patch.object(win, "_cache_row_olap"):
                win._new_fetch_button.click()
            logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
            self.assertTrue(logs)
            records = [
                json.loads(line)
                for line in logs[0].read_text(encoding="utf-8").splitlines()
            ]
        events = [r["event"] for r in records]
        self.assertIn("new_row_fetch_button_clicked", events)
        self.assertIn("new_row_order_no_read", events)
        self.assertIn("new_row_olap_fetch_start", events)
        self.assertIn("scroll_to_added_row_done", events)
        reasons = [r.get("return_reason") for r in records if r["event"] == "new_row_fetch_return"]
        self.assertIn("return_success", reasons)

    def test_new_input_row_log_return_reason_empty_and_duplicate(self) -> None:
        """未入力・重複でも return 理由がログに残る（要件7・14）。"""
        from app.voucher_window import VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            # 未入力
            win._new_order_no_edit.setText("")
            with mock.patch("app.voucher_window.QMessageBox.warning"):
                win._new_fetch_button.click()
            logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
            records = [
                json.loads(line)
                for line in logs[0].read_text(encoding="utf-8").splitlines()
            ]
        reasons = [r.get("return_reason") for r in records]
        self.assertIn("return_empty_order_no", reasons)

    def test_new_input_row_read_ok_always_proceeds_to_olap(self) -> None:
        """受注No読取OK後、正常系では必ずデータのみOLAP取得へ進む（要件11-1・11-2）。

        「受注No読取OK」の直後に途中returnして止まる不具合の回帰防止。新規入力行は
        通常行更新用の _perform_olap_fetch ではなく、UIを読まない
        _perform_olap_fetch_for_new_order へ進む。
        """
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        with mock.patch.object(win, "_perform_olap_fetch_for_new_order") as perf:
            win._new_fetch_button.click()
        perf.assert_called_once()
        args, _kwargs = perf.call_args
        self.assertEqual(args[0], "1394160")

    def test_new_input_row_does_not_use_existing_row_fetch(self) -> None:
        """新規入力行の取得は通常行更新用 _perform_olap_fetch を呼ばない（データのみ経路）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_perform_olap_fetch") as existing_fetch, \
                mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        existing_fetch.assert_not_called()
        self.assertEqual(len(win._rows), 1)

    def test_new_input_row_shows_olap_fetching_status(self) -> None:
        """OLAP取得直前に「OLAP取得中」がステータスに表示される（要件6・11-5）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        statuses: list[str] = []
        orig = win._set_new_row_status

        def _record(text, **kwargs):
            statuses.append(text)
            return orig(text, **kwargs)

        with mock.patch.object(win, "_set_new_row_status", side_effect=_record), \
                mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertTrue(any("受注No読取OK" in s for s in statuses))
        self.assertTrue(any("OLAP取得中" in s for s in statuses))
        self.assertTrue(any("行追加OK" in s for s in statuses))

    def test_new_input_row_invalid_format_shows_status(self) -> None:
        """受注No形式エラー時、ステータスに理由が出て通常行は増えない（要件11-3）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("###")
        with mock.patch.object(win, "_build_print_data") as build, \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            win._new_fetch_button.click()
        self.assertIn("形式エラー", win._new_row_status_label.text())
        self.assertIn("return_invalid_order_no", win._new_row_status_label.text())
        build.assert_not_called()
        warn.assert_called_once()
        self.assertEqual(len(win._rows), 0)
        # 形式エラー時は入力を残す。
        self.assertEqual(win._new_order_no_edit.text(), "###")

    def test_new_input_row_exception_before_olap_shows_status(self) -> None:
        """取得開始前の例外はステータスに「例外発生」を表示し行を追加しない（要件11-10・11-11）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        with mock.patch.object(
            win,
            "_create_default_row_settings_for_new_fetch",
            side_effect=RuntimeError("settings boom"),
        ), mock.patch("app.voucher_window.QMessageBox.critical") as crit:
            win._new_fetch_button.click()
        self.assertIn("例外発生", win._new_row_status_label.text())
        crit.assert_called_once()
        self.assertEqual(len(win._rows), 0)
        # 例外時は入力を残す。
        self.assertEqual(win._new_order_no_edit.text(), "1394160")

    def test_new_input_row_logs_read_to_fetch_call_events(self) -> None:
        """読取OK後からOLAP取得呼び出しまでの追跡ログが揃う（要件11-6）。"""
        from app.voucher_window import VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            win._new_order_no_edit.setText("1394160")
            data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
            with mock.patch.object(win, "_build_print_data", return_value=data), \
                    mock.patch.object(win, "_cache_row_olap"):
                win._new_fetch_button.click()
            logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
            self.assertTrue(logs)
            events = [
                json.loads(line)["event"]
                for line in logs[0].read_text(encoding="utf-8").splitlines()
            ]
        for expected in (
            "new_row_after_order_read",
            "new_row_before_validation",
            "new_row_after_validation",
            "new_row_before_duplicate_check",
            "new_row_after_duplicate_check",
            "new_row_before_olap_status",
            "new_row_before_olap_fetch_call",
            "new_row_after_olap_fetch_call",
        ):
            self.assertIn(expected, events)

    def test_new_input_row_exception_logs_return_before_olap(self) -> None:
        """取得開始前の例外は new_row_exception ログと return_before_olap を残す（要件11-10）。"""
        from app.voucher_window import VoucherWindow
        from app.path_utils import get_order_capture_debug_dir

        with mock.patch.dict(os.environ, {"TKS_VOUCHER_DEBUG": "1"}):
            win = VoucherWindow(olap_login_id="id", olap_password="pw")
            self.addCleanup(win.deleteLater)
            win._new_order_no_edit.setText("1394160")
            with mock.patch.object(
                win,
                "_create_default_row_settings_for_new_fetch",
                side_effect=RuntimeError("boom"),
            ), mock.patch("app.voucher_window.QMessageBox.critical"):
                win._new_fetch_button.click()
            logs = list(get_order_capture_debug_dir().glob("voucher_new_row_fetch_*.jsonl"))
            records = [
                json.loads(line)
                for line in logs[0].read_text(encoding="utf-8").splitlines()
            ]
        events = [r["event"] for r in records]
        self.assertIn("new_row_exception", events)
        reasons = [r.get("return_reason") for r in records]
        self.assertIn("return_before_olap", reasons)
        exc_rec = next(r for r in records if r["event"] == "new_row_exception")
        self.assertEqual(exc_rec.get("exception_type"), "RuntimeError")
        self.assertIn("traceback", exc_rec)

    def test_new_input_row_success_increases_rows_and_clears_input(self) -> None:
        """OLAP取得成功で _rows が1件増え、入力欄がクリアされる（要件11-7・11-8・11-12）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertEqual(len(win._rows), 1)
        self.assertIn("行追加OK", win._new_row_status_label.text())
        self.assertEqual(win._new_order_no_edit.text(), "")

    def test_new_input_row_fetch_never_calls_collect_row(self) -> None:
        """新規入力行の取得では _collect_row を一切呼ばない（要件1・強いテスト条件）。

        新規入力行だけでなく、一時作成した通常行(created_row)に対しても呼ばない。
        _collect_row は QDateEdit/QCheckBox/QRadioButton を読むため、再描画で削除
        され得るウィジェットを参照して RuntimeError になる。取得成功後の通常行の
        設定はUIからではなく既定値のrow settingsから流し込むため不要。
        """
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}

        def _fail(_rw):
            raise AssertionError("_collect_row must not be called for new input fetch")

        with mock.patch.object(win, "_collect_row", side_effect=_fail), \
                mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        # _collect_row が一度も呼ばれずに通常行が追加されていること。
        self.assertEqual(len(win._rows), 1)
        self.assertIn("行追加OK", win._new_row_status_label.text())

    def test_new_input_row_fetch_does_not_read_input_row_qt_widgets(self) -> None:
        """新規入力行の取得では新規入力行のQt値ウィジェットを読まない（要件2・3・4）。

        QDateEdit(date)/QCheckBox(isChecked)/QRadioButton(isChecked) を読み取ろうと
        すると即失敗するよう差し替えても、取得は成功する。
        """
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        input_row = win._new_input_row

        def _boom(*_a, **_k):
            raise AssertionError("new input row Qt widget must not be read")

        # 値読み取りメソッドを差し替え（呼ばれたら失敗）。
        input_row.date_edit.date = _boom
        input_row.finish_none_check.isChecked = _boom
        input_row.ampm_none.isChecked = _boom
        input_row.ampm_am.isChecked = _boom
        input_row.ampm_pm.isChecked = _boom
        for cb in list(input_row.process_checks.values()):
            cb.isChecked = _boom
        for cb in list(input_row.voucher_checks.values()):
            cb.isChecked = _boom
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertEqual(len(win._rows), 1)

    def test_new_input_row_no_normal_row_widget_before_olap_success(self) -> None:
        """OLAP取得成功前は _add_row で通常行ウィジェットを作らない（要件5・6・10）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        add_calls: list[object] = []
        real_add_row = win._add_row

        def _spy_add_row(*args, **kwargs):
            # OLAP取得が失敗する経路では _add_row が呼ばれてはいけない。
            add_calls.append((args, kwargs))
            return real_add_row(*args, **kwargs)

        with mock.patch.object(win, "_add_row", side_effect=_spy_add_row), \
                mock.patch.object(
                    win, "_build_print_data", side_effect=RuntimeError("boom")
                ), \
                mock.patch("app.voucher_window.QMessageBox.critical"):
            win._new_fetch_button.click()
        # OLAP失敗時は通常行ウィジェットを作らず、_rows も増えない。
        self.assertEqual(add_calls, [])
        self.assertEqual(len(win._rows), 0)
        self.assertIn("OLAP取得失敗", win._new_row_status_label.text())
        # 失敗時は新規入力行の受注Noを残す。
        self.assertEqual(win._new_order_no_edit.text(), "1394160")

    def test_new_input_row_fetch_survives_deleted_input_checkboxes(self) -> None:
        """新規入力行のチェックボックスが削除済みでも取得が成功する（要件3・削除C++参照回避）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        input_row = win._new_input_row
        # 実機の再描画で新規入力行のチェックボックス類が削除された状態を模す。
        # （_collect_row を呼ぶ実装だと、この時点で RuntimeError になる）
        for attr in ("finish_none_check", "ampm_none", "ampm_am", "ampm_pm"):
            getattr(input_row, attr).deleteLater()
        for cb in list(input_row.process_checks.values()):
            cb.deleteLater()
        for cb in list(input_row.voucher_checks.values()):
            cb.deleteLater()
        self.app.processEvents()
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        self.assertEqual(len(win._rows), 1)
        self.assertIn("行追加OK", win._new_row_status_label.text())
        self.assertEqual(win._new_order_no_edit.text(), "")

    def test_default_row_settings_for_new_fetch_from_defaults(self) -> None:
        """新規取得用の初期row settingsが既定値から作られUIを読まない（要件4・6）。"""
        from app.voucher_window import PROCESS_NAMES, VoucherWindow
        from app.voucher_templates import VOUCHER_TYPES

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._default_finish_date_none = True
        win._default_ampm = "pm"
        win._default_print_types = {vid for vid, _ in VOUCHER_TYPES[:1]}
        settings = win._create_default_row_settings_for_new_fetch("1394160")
        self.assertEqual(settings.order_no, "1394160")
        self.assertTrue(settings.finish_date_none)
        self.assertIsNone(settings.finish_date)
        self.assertEqual(settings.am_pm, "PM")
        self.assertEqual(
            set(settings.process_checks.keys()), set(PROCESS_NAMES)
        )
        self.assertFalse(any(settings.process_checks.values()))
        first_vid = VOUCHER_TYPES[0][0]
        self.assertTrue(settings.voucher_checks[first_vid])

    def test_new_input_row_added_row_reflects_default_settings(self) -> None:
        """取得成功で追加された通常行に既定の仕上日/AMPM/印刷伝票が反映される（要件6）。"""
        from app.voucher_window import VoucherWindow
        from app.voucher_templates import VOUCHER_TYPES

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._default_ampm = "pm"
        win._default_print_types = {VOUCHER_TYPES[0][0]}
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        created = win._rows[0]
        self.assertTrue(created.ampm_pm.isChecked())
        self.assertTrue(created.voucher_checks[VOUCHER_TYPES[0][0]].isChecked())

    def test_existing_row_update_still_works(self) -> None:
        """通常行の更新（再取得）処理は壊れない（要件11-13・デグレ防止）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        # まず通常行を1件追加する。
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        rw = win._rows[0]
        new_data = {"pages": [{"order_no": "1394160", "voucher_no": "Z9"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=new_data) as build, \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_refetch_existing_row(rw)
        build.assert_called_once_with(["1394160"])
        self.assertIs(rw.cached_olap, new_data)
        self.assertEqual(len(win._rows), 1)

    def test_new_input_row_added_row_is_scrolled_and_selected(self) -> None:
        """取得成功後、追加行が可視・選択され受注No一覧に含まれる（要件7・10・11）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        win._new_order_no_edit.setText("1394160")
        data = {"pages": [{"order_no": "1394160"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            win._new_fetch_button.click()
        created = win._rows[0]
        self.assertFalse(win._table.isRowHidden(created.table_row_index))
        self.assertIn("1394160", win._visible_order_numbers())
        # 追加行が選択されていること。
        self.assertIn(created.table_row_index, {i.row() for i in win._table.selectionModel().selectedRows()})

    def test_existing_row_button_updates_rows_index_not_table_row(self) -> None:
        """通常行の「更新」ボタンは table row - 1 の _rows 行を更新する（要件8・9）。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        # 通常行を2件作る（新規入力行は table row 0 のまま維持される）。
        for order_no in ("1111111", "2222222"):
            win._new_input_row.order_input.setText(order_no)
            data = {"pages": [{"order_no": order_no}], "raw_rows": []}
            with mock.patch.object(win, "_build_print_data", return_value=data), \
                    mock.patch.object(win, "_cache_row_olap"):
                win._new_input_row.refetch_button.click()
        self.assertEqual(len(win._rows), 2)
        # 新規入力行が table row 0、通常行が table row 1 以降であること。
        self.assertEqual(
            win._table.verticalHeader().visualIndex(win._new_input_row.table_row_index), 0
        )
        for rw in win._rows:
            self.assertGreaterEqual(
                win._table.verticalHeader().visualIndex(rw.table_row_index), 1
            )
        # 通常行の「更新」ボタンは、その行（rw）自身のみを更新し、_rows[0] を巻き込まない。
        target = win._rows[1]
        other = win._rows[0]
        target.order_input.setReadOnly(False)
        target.order_input.setText("3333333")
        new_data = {"pages": [{"order_no": "3333333"}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=new_data), \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            target.refetch_button.click()
        self.assertIs(target.cached_olap, new_data)
        self.assertIsNot(other.cached_olap, new_data)

    def _write_records(self, home, records: list[dict]) -> None:
        work = home / "work"
        work.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "saved_at": "2026-06-03T12:00:00", "records": records}
        (work / "voucher_records.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _new_window():
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        # 保存済み一覧の遅延（チャンク）復元を同期的に確定させる（要件1）。
        win._ensure_saved_rows_restored()
        return win

    def test_new_input_row_single_with_multiple_saved_rows(self) -> None:
        with _temp_home() as home:
            self._write_records(
                home,
                [
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "1111111",
                     "has_olap_data": True, "cached_olap": {"pages": [{"order_no": "1111111"}], "raw_rows": []}},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "2222222",
                     "has_olap_data": True, "cached_olap": {"pages": [{"order_no": "2222222"}], "raw_rows": []}},
                ],
            )
            win = self._new_window()
            self.addCleanup(win.deleteLater)
            self.assertEqual(len(win._rows), 2)
            self.assertIsNotNone(win._new_input_row)
            self.assertNotIn(win._new_input_row, win._rows)
            self.assertEqual(win._table.rowCount(), 3)
            self.assertEqual(
                win._table.verticalHeader().visualIndex(win._new_input_row.table_row_index), 0
            )

    def test_empty_order_no_saved_record_excluded_on_restore(self) -> None:
        with _temp_home() as home:
            self._write_records(
                home,
                [
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "", "has_olap_data": False,
                     "cached_olap": {}},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "5218869",
                     "has_olap_data": True, "cached_olap": {"pages": [{"order_no": "5218869"}], "raw_rows": []}},
                ],
            )
            win = self._new_window()
            self.addCleanup(win.deleteLater)
            # 空受注No行は復元されず、通常行は取得済み1件のみ。新規行は1行のまま。
            self.assertEqual(len(win._rows), 1)
            self.assertEqual(win._rows[0].order_input.text(), "5218869")
            self.assertEqual(win._table.rowCount(), 2)

    def test_empty_order_no_row_is_not_saved(self) -> None:
        with _temp_home() as home:
            win = self._make_window()
            # 旧仕様のように空の通常行を1つ足しても保存対象にはならない。
            win._on_add_row()
            win._rows[0].order_input.setText("1405113")
            win._save_records()
            saved = json.loads((home / "work" / "voucher_records.json").read_text(encoding="utf-8"))
            order_nos = [r["order_no"] for r in saved["records"]]
            self.assertEqual(order_nos, ["1405113"])

    def test_new_input_row_not_duplicated_after_apply_filters(self) -> None:
        win = self._new_window()
        self.addCleanup(win.deleteLater)
        for _ in range(3):
            win._apply_filters()
        self.assertIsNotNone(win._new_input_row)
        self.assertNotIn(win._new_input_row, win._rows)
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(len(win._rows), 0)

    def test_selected_delete_button_exists(self) -> None:
        win = self._make_window()
        self.assertIsInstance(win._remove_row_button, QPushButton)
        self.assertEqual(win._remove_row_button.text(), "選択削除")
        self.assertTrue(hasattr(win, "_on_remove_selected"))

    def test_row_has_order_input(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self.assertIsInstance(rw.order_input, QLineEdit)

    def test_row_has_date_edit_with_calendar_popup(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self.assertIsInstance(rw.date_edit, QDateEdit)
        self.assertTrue(rw.date_edit.calendarPopup())

    def test_row_has_ampm_radio_buttons(self) -> None:
        """AM/PM はコンボボックスではなく縦2行のラジオボタンであること（要件3）。"""
        win = self._make_window()
        rw = win._rows[0]
        # コンボボックスは廃止されている。
        self.assertFalse(hasattr(rw, "ampm_combo"))
        self.assertIsInstance(rw.ampm_am, QRadioButton)
        self.assertIsInstance(rw.ampm_pm, QRadioButton)
        self.assertEqual(rw.ampm_am.text(), "AM")
        self.assertEqual(rw.ampm_pm.text(), "PM")
        self.assertIsInstance(rw.ampm_group, QButtonGroup)
        self.assertTrue(rw.ampm_group.exclusive())
        # 初期値は AM が選択。
        self.assertTrue(rw.ampm_am.isChecked())
        self.assertFalse(rw.ampm_pm.isChecked())

    def test_ampm_radio_reflected_in_collect_row(self) -> None:
        """AM/PMラジオの選択が行データ（PDF作成データ）へ反映されること（要件3）。"""
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        rw.cached_olap = {
            "pages": [{"order_no": "5218869", "customer_name": "顧客", "voucher_no": "001"}],
            "raw_rows": [],
        }
        self.assertEqual(win._collect_row(rw).am_pm, "AM")
        rw.ampm_pm.setChecked(True)
        self.assertEqual(win._collect_row(rw).am_pm, "PM")
        # 排他選択：PM選択でAMは外れる。
        self.assertFalse(rw.ampm_am.isChecked())

    def test_ampm_radio_layout_is_three_rows(self) -> None:
        """AM/PMラジオが縦3行（なし/AM/PM）に配置されること（要件1）。"""
        from PySide6.QtWidgets import QVBoxLayout

        from app.voucher_window import COL_AMPM

        win = self._make_window()
        cell = win._table.cellWidget(0, COL_AMPM)
        self.assertIsNotNone(cell)
        radios = cell.findChildren(QRadioButton)
        self.assertEqual(len(radios), 3)
        self.assertEqual({r.text() for r in radios}, {"なし", "AM", "PM"})
        # 縦並び（QVBoxLayout）であること。
        self.assertIsInstance(cell.layout(), QVBoxLayout)

    def test_row_has_process_checkboxes_all_off(self) -> None:
        from app.voucher_window import PROCESS_NAMES

        win = self._make_window()
        rw = win._rows[0]
        self.assertEqual(set(rw.process_checks), set(PROCESS_NAMES))
        for cb in rw.process_checks.values():
            self.assertIsInstance(cb, QCheckBox)
            self.assertFalse(cb.isChecked())

    def test_added_row_process_checkboxes_all_off(self) -> None:
        win = self._make_window()
        win._on_add_row()
        rw = win._rows[-1]
        for cb in rw.process_checks.values():
            self.assertFalse(cb.isChecked())

    def test_row_has_film_and_rtori_checkboxes(self) -> None:
        """加工名チェックに フィルム貼・Rとり が表示され、ラベルが一致すること。"""
        win = self._make_window()
        rw = win._rows[0]
        self.assertIn("フィルム貼", rw.process_checks)
        self.assertIn("Rとり", rw.process_checks)
        labels = {cb.text() for cb in rw.process_checks.values()}
        self.assertIn("フィルム貼", labels)
        self.assertIn("Rとり", labels)
        # 初期値はOFF。
        self.assertFalse(rw.process_checks["フィルム貼"].isChecked())
        self.assertFalse(rw.process_checks["Rとり"].isChecked())

    def test_film_and_rtori_can_toggle_on_off(self) -> None:
        """フィルム貼・Rとり をON/OFFできること。"""
        win = self._make_window()
        rw = win._rows[0]
        for name in ("フィルム貼", "Rとり"):
            rw.process_checks[name].setChecked(True)
            self.assertTrue(rw.process_checks[name].isChecked())
            rw.process_checks[name].setChecked(False)
            self.assertFalse(rw.process_checks[name].isChecked())

    def test_film_and_rtori_save_and_restore(self) -> None:
        """フィルム貼・Rとり のチェック状態が保存レコードから復元されること。"""
        win = self._make_window()
        rw = win._rows[0]
        win._apply_saved_record_to_row(
            rw, {"process_checks": {"フィルム貼": True, "Rとり": False}}
        )
        self.assertTrue(rw.process_checks["フィルム貼"].isChecked())
        self.assertFalse(rw.process_checks["Rとり"].isChecked())

    def test_legacy_record_without_film_rtori_defaults_off(self) -> None:
        """旧保存データ（フィルム貼・Rとり 項目なし）でもエラーにならず未チェック扱い。"""
        win = self._make_window()
        rw = win._rows[0]
        # 旧データには フィルム貼・Rとり キーが存在しない。
        win._apply_saved_record_to_row(
            rw, {"process_checks": {"エッジング": True, "広幅": True}}
        )
        self.assertTrue(rw.process_checks["エッジング"].isChecked())
        self.assertFalse(rw.process_checks["フィルム貼"].isChecked())
        self.assertFalse(rw.process_checks["Rとり"].isChecked())

    def test_row_has_voucher_checkboxes_all_on(self) -> None:
        from app.voucher_templates import VOUCHER_IDS

        win = self._make_window()
        rw = win._rows[0]
        self.assertEqual(set(rw.voucher_checks), set(VOUCHER_IDS))
        for cb in rw.voucher_checks.values():
            self.assertTrue(cb.isChecked())

    def test_collect_row_builds_dataclass(self) -> None:
        from app.voucher_window import VoucherOrderRow

        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText(" 5218869 ")
        row = win._collect_row(rw)
        self.assertIsInstance(row, VoucherOrderRow)
        self.assertEqual(row.order_no, "5218869")
        self.assertEqual(row.am_pm, "AM")
        self.assertIsNotNone(row.finish_date)

    def test_pdf_button_calls_create_per_row(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch.object(win, "_create_pdf") as create:
            rw.pdf_button.click()
        build.assert_called_once_with(["5218869"])
        create.assert_called_once()

    def test_create_pdf_auto_open_on_calls_open(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        settings = VoucherPrinterSettings(open_pdf_after_create=True)
        path = Path("/tmp/5218869.pdf")
        with mock.patch("app.voucher_service.create_vouchers_pdf", return_value=path), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(win, "_notify_pdf_created"), \
                mock.patch.object(win, "_open_local_path", return_value=True) as opener:
            result = win._create_pdf(["01"], {"pages": [{}]}, output_dir=Path("/tmp"), open_after=True)
        self.assertEqual(result, path)
        opener.assert_called_once_with(path)

    def test_create_pdf_auto_open_off_does_not_call_open(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        settings = VoucherPrinterSettings(open_pdf_after_create=False)
        path = Path("/tmp/5218869.pdf")
        with mock.patch("app.voucher_service.create_vouchers_pdf", return_value=path), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(win, "_notify_pdf_created"), \
                mock.patch.object(win, "_open_local_path", return_value=True) as opener:
            result = win._create_pdf(["01"], {"pages": [{}]}, output_dir=Path("/tmp"), open_after=True)
        self.assertEqual(result, path)
        opener.assert_not_called()

    def test_multiple_pdf_auto_open_opens_folder_once(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        settings = VoucherPrinterSettings(open_pdf_after_create=True)
        paths = [Path("/tmp/out/1.pdf"), Path("/tmp/out/2.pdf")]
        with mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(win, "_open_local_path", return_value=True) as opener:
            win._auto_open_created_pdfs(paths)
        opener.assert_called_once_with(Path("/tmp/out"))

    def test_pdf_created_dialog_setting_does_not_suppress_auto_open(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        settings = VoucherPrinterSettings(
            show_pdf_created_dialog=False,
            open_pdf_after_create=True,
        )
        path = Path("/tmp/5218869.pdf")
        with mock.patch("app.voucher_service.create_vouchers_pdf", return_value=path), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(win, "_notify_pdf_created") as notify, \
                mock.patch.object(win, "_open_local_path", return_value=True) as opener:
            win._create_pdf(["01"], {"pages": [{}]}, output_dir=Path("/tmp"), open_after=True)
        notify.assert_called_once()
        opener.assert_called_once_with(path)

    def test_print_button_calls_print_per_row(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr, \
                mock.patch("app.voucher_window.QMessageBox.information"):
            rw.print_button.click()
        build.assert_called_once_with(["5218869"])
        gen.assert_called_once()
        pr.assert_called_once()
        self.assertEqual(pr.call_args.args[0], b"%PDF")
        self.assertEqual(pr.call_args.kwargs["selected_count"], 1)

    def test_print_error_signal_recovers_ui_without_modal(self) -> None:
        """Popen前エラー（request_sentなし）でも error signal でUIが復帰すること。"""
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        worker = _FakePrintWorker()
        sumatra_settings = VoucherPrinterSettings(
            printer_name="Printer A", print_backend="sumatra", sumatra_path=""
        )
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=sumatra_settings), \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker), \
                mock.patch("app.voucher_window.QMessageBox.critical") as critical:
            rw.print_button.click()
            self.assertFalse(win._print_in_progress)
            # request_sent は来ず、error signal のみで復帰する。
            worker.error.emit(
                "SumatraPDFが見つかりません。印刷設定でパスを指定してください。",
                {"worker_error": True, "worker_finished": True},
            )
        self.assertFalse(win._print_in_progress)
        self.assertTrue(rw.print_button.isEnabled())
        critical.assert_not_called()

    def test_preview_button_opens_inapp_window_without_formal_save(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.create_vouchers_pdf") as create, \
                mock.patch("app.voucher_service.save_pdf_bytes") as save, \
                mock.patch.object(win, "_open_preview_window") as preview:
            rw.preview_button.click()
        build.assert_called_once_with(["5218869"])
        gen.assert_called_once()
        create.assert_not_called()
        save.assert_not_called()
        preview.assert_called_once()
        self.assertEqual(preview.call_args.args, (b"%PDF",))
        self.assertFalse(preview.call_args.kwargs["preview_cache_hit"])
        self.assertTrue(preview.call_args.kwargs["edit_render_trace_id"])

    def test_row_preview_opens_preview_window(self) -> None:
        """行別プレビューで VoucherPrintPreviewWindow が開かれること。"""
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch.object(win, "_open_preview_window") as preview:
            rw.preview_button.click()
        preview.assert_called_once()
        self.assertEqual(preview.call_args.args, (b"%PDF",))
        self.assertFalse(preview.call_args.kwargs["preview_cache_hit"])
        self.assertTrue(preview.call_args.kwargs["edit_render_trace_id"])

    def test_empty_order_no_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        self.assertFalse(rw.pdf_button.isEnabled())

    def test_new_order_row_greyed_cells_are_darker_in_dark_theme(self) -> None:
        win = self._make_window()
        with mock.patch("app.voucher_window.current_title_bar_is_dark", return_value=True):
            dark_style = win._greyed_cell()
        with mock.patch("app.voucher_window.current_title_bar_is_dark", return_value=False):
            light_style = win._greyed_cell()
        self.assertIn("#171b20", dark_style)
        self.assertIn("#e5e7eb", light_style)
        self.assertNotIn("#171b20", light_style)

    def test_new_order_row_order_input_remains_editable(self) -> None:
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        new_row = win._new_input_row
        self.assertIsNotNone(new_row)
        self.assertFalse(new_row.order_input.isReadOnly())
        self.assertTrue(new_row.refetch_button.isEnabled())

    def test_no_voucher_selected_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        for cb in rw.voucher_checks.values():
            cb.setChecked(False)
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            rw.pdf_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_missing_voucher_no_shows_order_no_and_stops_pdf(self) -> None:
        from app.voucher_window import format_missing_voucher_no_message

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        for cb in rw.voucher_checks.values():
            cb.setChecked(False)
        next(iter(rw.voucher_checks.values())).setChecked(True)
        fake_service = mock.Mock()
        fake_service.fetch_vouchers.return_value = [self._olap_row(voucher_no="0000000")]
        fake_service.last_response_r1_count = 1
        with mock.patch("app.voucher_window.VoucherOlapService", return_value=fake_service), \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch.object(win, "_create_pdf") as create, \
                mock.patch.object(win, "_cache_row_olap") as cache, \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            rw.pdf_button.click()
        warn.assert_called_once()
        self.assertEqual(warn.call_args.args[1], "伝票作成・印刷")
        self.assertEqual(warn.call_args.args[2], format_missing_voucher_no_message({"5218869"}))
        create.assert_not_called()
        cache.assert_not_called()

    def test_missing_voucher_no_disables_kintone_button_after_fetch_error(self) -> None:
        fake_kintone = mock.Mock()
        fake_kintone.get_order_numbers.return_value = set()
        win = self._make_window_with_kintone(fake_kintone)
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        self.assertFalse(rw.kintone_button.isEnabled())
        for cb in rw.voucher_checks.values():
            cb.setChecked(False)
        next(iter(rw.voucher_checks.values())).setChecked(True)
        fake_service = mock.Mock()
        fake_service.fetch_vouchers.return_value = [self._olap_row(voucher_no=0)]
        fake_service.last_response_r1_count = 1
        with mock.patch("app.voucher_window.VoucherOlapService", return_value=fake_service), \
                mock.patch("app.voucher_window.QMessageBox.warning"):
            rw.refetch_button.click()
        self.assertFalse(rw.kintone_button.isEnabled())

    def test_normal_voucher_no_builds_print_data(self) -> None:
        win = self._make_window()
        fake_service = mock.Mock()
        fake_service.fetch_vouchers.return_value = [self._olap_row(voucher_no="Z739291")]
        fake_service.last_response_r1_count = 1
        with mock.patch("app.voucher_window.VoucherOlapService", return_value=fake_service):
            data = win._build_print_data(["5218869"])
        self.assertEqual(len(data["pages"]), 1)
        self.assertEqual(data["pages"][0]["voucher_no"], "Z739291")

    def test_build_print_data_preserves_aggregated_delivery_course(self) -> None:
        win = self._make_window()
        first = self._olap_row(voucher_no="Z739291")
        first.update({"order_line_no": "1", "delivery_course_name": ""})
        second = self._olap_row(voucher_no="Z739291")
        second.update({
            "order_line_no": "2",
            "product_name": "商品2",
            "delivery_course_name": "大阪南コース",
            "delivery_course_name_raw": "大阪南コース",
        })
        fake_service = mock.Mock()
        fake_service.fetch_vouchers.return_value = [first, second]
        fake_service.last_response_r1_count = 2
        with mock.patch("app.voucher_window.VoucherOlapService", return_value=fake_service):
            data = win._build_print_data(["5218869"])
        self.assertEqual(data["pages"][0]["delivery_course_name"], "大阪南コース")
        self.assertEqual(data["pages"][0]["delivery_course_name_raw"], "大阪南コース")

    def test_missing_voucher_no_in_multiple_orders_stops_all_selected_pdf(self) -> None:
        from app.voucher_window import format_missing_voucher_no_message

        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("1111111")
        win._rows[1].order_input.setText("2222222")
        for rw in win._rows:
            self._mark_fetched(win, rw, rw.order_input.text())
        win._set_all_rows_checked(True)
        for rw in win._rows:
            for cb in rw.voucher_checks.values():
                cb.setChecked(False)
            next(iter(rw.voucher_checks.values())).setChecked(True)
        calls = {
            "1111111": [self._olap_row("1111111", "Z111111")],
            "2222222": [self._olap_row("2222222", "0")],
        }
        fake_service = mock.Mock()
        fake_service.fetch_vouchers.side_effect = lambda nums, *_args: calls[nums[0]]
        fake_service.last_response_r1_count = 1
        with mock.patch("app.voucher_window.VoucherOlapService", return_value=fake_service), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes") as merge, \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            win._on_select_pdf()
        warn.assert_called_once()
        self.assertEqual(warn.call_args.args[2], format_missing_voucher_no_message({"2222222"}))
        gen.assert_not_called()
        merge.assert_not_called()

    def test_edit_order_sheet_empty_order_no_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        self.assertFalse(rw.edit_button.isEnabled())

    def test_edit_order_sheet_opens_editor(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        worker_thread_ids = []
        def _generate(*_args, **_kwargs):
            import threading
            worker_thread_ids.append(threading.get_ident())
            return b"%PDF"
        with mock.patch.object(win, "_build_print_data") as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", side_effect=_generate) as gen, \
                mock.patch("app.voucher_edit_window.VoucherEditWindow") as editor_cls:
            rw.edit_button.click()
            import time as _time
            for _ in range(100):
                self.app.processEvents()
                if not win._editor_workers:
                    break
                _time.sleep(0.002)
        # クリック処理はplaceholder付き画面を先に生成し、PDF生成はworkerで行う。
        build.assert_not_called()
        editor_cls.assert_called_once()
        self.assertTrue(editor_cls.call_args.kwargs["defer_background"])
        self.assertTrue(gen.called)
        import threading
        self.assertTrue(all(value != threading.get_ident() for value in worker_thread_ids))

    def test_initial_size_shows_all_columns(self) -> None:
        """起動直後の初期幅・最小幅が全列を表示できるサイズであること。"""
        from app.voucher_window import COLUMN_LABELS

        win = self._make_window()
        self.assertEqual(len(COLUMN_LABELS), 12)
        self.assertEqual(win._table.columnCount(), 12)
        screen = win.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.assertLessEqual(win.width(), available.width())
            self.assertLessEqual(win.minimumWidth(), available.width())
        self.assertGreaterEqual(win.width(), min(760, win._table.sizeHint().width()))
        # 右端列に必ず到達できるよう、水平スクロールバーは常時利用可能。
        from PySide6.QtCore import Qt

        self.assertEqual(
            win._table.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn,
        )
        self.assertIsNotNone(win.findChild(QScrollArea, "voucherTopControlsScrollArea"))

    def test_new_row_status_label_is_readable_in_dark_theme(self) -> None:
        from app.voucher_window import VoucherWindow

        win = self._make_window()
        with mock.patch("app.voucher_window.current_title_bar_is_dark", return_value=True):
            win._set_new_row_status("処理中")
        style = win._new_row_status_label.styleSheet().lower()
        self.assertIn("#e5e7eb", style)
        self.assertNotIn("#111827", style)

    def test_finish_date_and_ampm_columns_are_wider(self) -> None:
        from app.voucher_window import COL_AMPM, COL_FINISH_DATE

        win = self._make_window()
        self.assertGreaterEqual(win._table.columnWidth(COL_FINISH_DATE), 150)
        self.assertGreaterEqual(win._table.columnWidth(COL_AMPM), 100)

    def test_row_preview_button_exists(self) -> None:
        win = self._make_window()
        self.assertEqual(win._rows[0].preview_button.text(), "プレビュー")

    def test_select_preview_button_exists(self) -> None:
        win = self._make_window()
        self.assertEqual(win._select_preview_button.text(), "選択プレビュー")

    def test_attach_row_settings_propagates_to_pages(self) -> None:
        """行設定（仕上日・AM/PM・加工名チェック）が各ページ辞書へ付加されること。"""
        from app.voucher_window import VoucherOrderRow

        win = self._make_window()
        row = VoucherOrderRow(
            order_no="5218869",
            finish_date=date(2026, 6, 10),
            am_pm="PM",
            process_checks={"広幅": True, "BOB": False},
            voucher_checks={"01": True},
            finish_date_none=False,
        )
        data = {"pages": [{}, {}]}
        win._attach_row_settings(data, row)

        self.assertEqual(data["finish_date"], date(2026, 6, 10))
        self.assertFalse(data["finish_date_none"])
        self.assertEqual(data["am_pm"], "PM")
        for page in data["pages"]:
            self.assertEqual(page["row_finish_date"], date(2026, 6, 10))
            self.assertFalse(page["row_finish_date_none"])
            self.assertEqual(page["row_am_pm"], "PM")
            self.assertTrue(page["row_process_checks"]["広幅"])
            self.assertFalse(page["row_process_checks"]["BOB"])


    # ── 選択列・選択系ボタン ─────────────────────────────────────────────────
    def test_select_column_present(self) -> None:
        """初期表示で選択列（一番左）が存在し、行に選択チェックボックスがあること。"""
        from app.voucher_window import COLUMN_LABELS, COL_SELECT

        win = self._make_window()
        self.assertEqual(COL_SELECT, 0)
        self.assertEqual(COLUMN_LABELS[0], "□")
        self.assertIsInstance(win._rows[0].select_check, QCheckBox)

    def test_selection_buttons_disabled_without_selection(self) -> None:
        """選択行がない場合、選択PDF作成/選択印刷が無効であること。"""
        win = self._make_window()
        self.assertFalse(win._select_pdf_button.isEnabled())
        self.assertFalse(win._select_print_button.isEnabled())
        self.assertFalse(win._select_order_no_button.isEnabled())

    def test_row_check_enables_selection_buttons(self) -> None:
        """行チェックONで選択系ボタンが有効、OFFで無効に戻ること。"""
        win = self._make_window()
        win._rows[0].select_check.setChecked(True)
        self.assertTrue(win._select_pdf_button.isEnabled())
        self.assertTrue(win._select_print_button.isEnabled())
        self.assertTrue(win._select_order_no_button.isEnabled())
        win._rows[0].select_check.setChecked(False)
        self.assertFalse(win._select_pdf_button.isEnabled())
        self.assertFalse(win._select_print_button.isEnabled())
        self.assertFalse(win._select_order_no_button.isEnabled())

    def test_header_select_all_checks_all_rows(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        win._on_header_section_clicked(0)
        for rw in win._rows:
            self.assertTrue(rw.select_check.isChecked())
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "☑")

    def test_header_select_all_clears_all_rows(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._set_all_rows_checked(True)
        win._on_header_section_clicked(0)
        for rw in win._rows:
            self.assertFalse(rw.select_check.isChecked())
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "□")

    def test_partial_selection_sets_tristate(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._rows[0].select_check.setChecked(True)
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "◩")

    def test_selected_delete_handler_exists(self) -> None:
        win = self._make_window()
        self.assertTrue(hasattr(win, "_on_remove_selected"))
        self.assertTrue(callable(win._on_remove_selected))

    def test_remove_button_enabled_only_with_selection(self) -> None:
        """「選択削除」は他の選択ボタンと同様、選択時のみ有効になる。"""
        win = self._make_window()
        # 未選択では無効。
        self.assertFalse(win._remove_row_button.isEnabled())
        win._rows[0].select_check.setChecked(True)
        self.assertTrue(win._remove_row_button.isEnabled())
        win._rows[0].select_check.setChecked(False)
        self.assertFalse(win._remove_row_button.isEnabled())

    # ── 選択削除 ──────────────────────────────────────────────────────────────
    def test_remove_selected_deletes_only_checked_rows(self) -> None:
        """選択したレコードだけ削除され、未選択は残る（要件1・2）。"""
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        self._mark_fetched(win, rows[2], "3333333")
        rows[1].select_check.setChecked(True)  # 2222222 だけ選択
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        remaining = sorted(rw.order_input.text().strip() for rw in win._rows)
        self.assertEqual(remaining, ["1111111", "3333333"])
        self.assertEqual(win._table.rowCount(), 3)
        self.assertEqual(len(win._rows), 2)

    def test_remove_selected_with_no_selection_shows_message(self) -> None:
        """選択0件なら削除せず案内メッセージを出す（要件6）。"""
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1111111")
        with mock.patch(
            "app.voucher_window.QMessageBox.information"
        ) as info, mock.patch(
            "app.voucher_window.QMessageBox.question"
        ) as question:
            win._on_remove_selected()
        info.assert_called_once()
        self.assertIn("削除するレコードを選択してください", info.call_args.args[2])
        question.assert_not_called()
        self.assertEqual(len(win._rows), 1)

    def test_remove_selected_cancel_keeps_rows(self) -> None:
        """確認でキャンセル（No）した場合は削除しない（要件4・5）。"""
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        rows[0].select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._no(),
        ):
            win._on_remove_selected()
        self.assertEqual(len(win._rows), 2)
        self.assertTrue(rows[0].select_check.isChecked())

    def test_remove_selected_persists_to_local_save(self) -> None:
        """削除後はローカル保存へ反映される（要件「削除後は保存」）。"""
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        rows[0].select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        saved = json.loads(win._records_path().read_text(encoding="utf-8"))
        order_nos = [r.get("order_no") for r in saved["records"]]
        self.assertNotIn("1111111", order_nos)
        self.assertIn("2222222", order_nos)

    def test_remove_selected_during_filter_targets_real_record(self) -> None:
        """フィルター中でも選択した実レコードだけ削除する（要件6）。"""
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        rows[1].select_check.setChecked(True)
        # 受注No検索で 1111111 のみ表示にしても、選択した 2222222 を削除できる。
        win._order_search_edit.setText("1111111")
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        remaining = [rw.order_input.text().strip() for rw in win._rows]
        self.assertEqual(remaining, ["1111111"])

    def test_remove_selected_during_sort_targets_real_record(self) -> None:
        """並び替え中でも選択した実レコードだけ削除する（要件7）。"""
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        self._mark_fetched(win, rows[2], "3333333")
        # 更新日時を変えて並び替えを発生させる。
        rows[0].updated_at = datetime(2020, 1, 1)
        rows[1].updated_at = datetime(2024, 1, 1)
        rows[2].updated_at = datetime(2022, 1, 1)
        win._apply_filters()  # _sort_rows_by_updated_at で並び替え
        target = self._row_for_order(win, "3333333")
        target.select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        remaining = sorted(rw.order_input.text().strip() for rw in win._rows)
        self.assertEqual(remaining, ["1111111", "2222222"])

    def test_remove_new_unfetched_row_does_not_error(self) -> None:
        """新規未取得行を削除してもエラーにならず、空行が1行残る（要件8）。"""
        win = self._make_window()
        # 初期空行（未取得）だけを選択して削除する。
        win._rows[0].select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        # 通常行はなくなり、入力専用の新規行だけが残る。
        self.assertEqual(len(win._rows), 0)
        self.assertIsNotNone(win._new_input_row)
        self.assertEqual(win._table.rowCount(), 1)

    def test_remove_selected_does_not_trigger_kintone_teams_pdf(self) -> None:
        """削除でKintone登録・Teams通知・PDF作成が呼ばれない（要件9）。"""
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        rows[0].select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ), mock.patch(
            "app.voucher_service.create_vouchers_pdf"
        ) as create_pdf, mock.patch(
            "app.voucher_service.build_vouchers_pdf_bytes"
        ) as build_pdf, mock.patch(
            "app.voucher_print_service.print_pdf_direct"
        ) as print_pdf, mock.patch.object(
            win, "_build_print_data"
        ) as build_data:
            win._on_remove_selected()
        create_pdf.assert_not_called()
        build_pdf.assert_not_called()
        print_pdf.assert_not_called()
        build_data.assert_not_called()

    def test_remove_selected_does_not_change_updated_at(self) -> None:
        """削除で残った行の更新日時（登録完了日時）を変更しない（要件10）。"""
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        keep = rows[1]
        fixed = datetime(2023, 5, 6, 7, 8, 9)
        keep.updated_at = fixed
        rows[0].select_check.setChecked(True)
        with mock.patch(
            "app.voucher_window.QMessageBox.question",
            return_value=self._yes(),
        ):
            win._on_remove_selected()
        survivor = self._row_for_order(win, "2222222")
        self.assertEqual(survivor.updated_at, fixed)

    @staticmethod
    def _yes():
        from PySide6.QtWidgets import QMessageBox

        return QMessageBox.StandardButton.Yes

    @staticmethod
    def _no():
        from PySide6.QtWidgets import QMessageBox

        return QMessageBox.StandardButton.No

    def test_select_pdf_targets_only_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        target = win._rows[0]
        other = win._rows[1]
        self._mark_fetched(win, target, "1111111")
        self._mark_fetched(win, other, "2222222")
        target.select_check.setChecked(True)  # 受注No=1111111 だけ選択
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF") as merge, \
                mock.patch("app.voucher_service.save_named_pdf_bytes", return_value="/tmp/out.pdf") as save, \
                mock.patch("app.voucher_window.QDesktopServices.openUrl"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_pdf()
        # チェックON行（1111111）だけが処理対象
        build.assert_called_once_with(["1111111"])
        gen.assert_called_once()
        # 1受注No・単一伝票なら結合不要。ファイル名は「<受注No>_伝票」。
        merge.assert_not_called()
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["filename_stem"], "1111111_伝票")

    def test_select_pdf_saves_each_selected_order_with_order_no_token(self) -> None:
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        for rw in rows:
            rw.select_check.setChecked(True)
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_service.save_named_pdf_bytes", side_effect=["/tmp/1111111_伝票.pdf", "/tmp/2222222_伝票.pdf"]) as save, \
                mock.patch("app.voucher_window.QDesktopServices.openUrl"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_pdf()
        self.assertEqual(build.call_count, 2)
        # 受注NoごとにPDFファイルを分けて作成する（1受注No=1ファイル）。
        self.assertEqual(save.call_count, 2)
        self.assertEqual(
            sorted(call.kwargs["filename_stem"] for call in save.call_args_list),
            ["1111111_伝票", "2222222_伝票"],
        )

    def test_print_does_not_save_pdf_when_save_pdf_on_print_off(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        settings = VoucherPrinterSettings(print_backend="sumatra", save_pdf_on_print=False)
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_service.save_pdf_bytes") as save, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr:
            rw.print_button.click()
        # OFFなのでPDF出力先へは保存しない。印刷ジョブは投入される。
        save.assert_not_called()
        pr.assert_called_once()

    def test_print_saves_pdf_when_save_pdf_on_print_on_and_still_enqueues(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        settings = VoucherPrinterSettings(print_backend="sumatra", save_pdf_on_print=True)
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_service.save_pdf_bytes", return_value="/tmp/5218869.pdf") as save, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr:
            rw.print_button.click()
        # PDF出力先へ保存し、かつPrintQueueManagerへ投入する（両方行う）。
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["filename_token"], "5218869")
        pr.assert_called_once()

    def test_print_save_pdf_uses_uncorrected_bytes_even_with_adjustment_on(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        # 印刷補正ON。PDF出力先へ保存されるのは補正前の通常PDFであること。
        settings = VoucherPrinterSettings(
            print_backend="sumatra",
            save_pdf_on_print=True,
            print_adjustment_enabled=True,
            print_adjustment_margin_left_mm=4.0,
        )
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%RAWPDF"), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_service.save_pdf_bytes", return_value="/tmp/5218869.pdf") as save, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr:
            rw.print_button.click()
        # 保存に渡すのも印刷に渡すのも補正前バイト列（補正は印刷サービス側で内部適用）。
        self.assertEqual(save.call_args.args[0], b"%RAWPDF")
        self.assertEqual(pr.call_args.args[0], b"%RAWPDF")

    def test_print_aborts_when_save_pdf_fails(self) -> None:
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        settings = VoucherPrinterSettings(print_backend="sumatra", save_pdf_on_print=True)
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_window.load_voucher_printer_settings", return_value=settings), \
                mock.patch("app.voucher_service.save_pdf_bytes", side_effect=RuntimeError("書き込み不可")), \
                mock.patch("app.voucher_window.QMessageBox.critical") as critical, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr:
            rw.print_button.click()
        # 保存失敗時は安全優先で印刷を中止する（ジョブ投入しない）。
        pr.assert_not_called()
        critical.assert_called_once()

    def test_select_print_keeps_single_merged_job(self) -> None:
        win = self._make_window()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1111111")
        self._mark_fetched(win, rows[1], "2222222")
        for rw in rows:
            rw.select_check.setChecked(True)
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%MERGED") as merge, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr:
            win._on_select_print()
        # 選択印刷は従来どおり結合PDF 1ジョブ（受注No別に分割しない）。
        merge.assert_called_once()
        pr.assert_called_once()
        self.assertEqual(pr.call_args.args[0], b"%MERGED")
        self.assertEqual(pr.call_args.kwargs["selected_count"], 2)

    def test_select_print_targets_only_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        other = win._rows[0]
        target = win._rows[1]
        self._mark_fetched(win, other, "1111111")
        self._mark_fetched(win, target, "2222222")
        target.select_check.setChecked(True)  # 受注No=2222222 だけ選択
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF") as merge, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as pr, \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_print()
        build.assert_called_once_with(["2222222"])
        gen.assert_called_once()
        merge.assert_called_once()
        pr.assert_called_once()
        self.assertEqual(pr.call_args.args[0], b"%PDF")
        self.assertEqual(pr.call_args.kwargs["selected_count"], 1)

    def test_select_preview_does_not_formally_save_pdf(self) -> None:
        win = self._make_window()
        win._on_add_row()
        target = win._rows[0]
        other = win._rows[1]
        self._mark_fetched(win, target, "1111111")
        self._mark_fetched(win, other, "2222222")
        target.select_check.setChecked(True)
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF") as merge, \
                mock.patch("app.voucher_service.create_vouchers_pdf") as create, \
                mock.patch("app.voucher_service.save_pdf_bytes") as save, \
                mock.patch.object(win, "_open_preview_window") as preview:
            win._on_select_preview()
        build.assert_called_once_with(["1111111"])
        gen.assert_called_once()
        merge.assert_called_once()
        create.assert_not_called()
        save.assert_not_called()
        preview.assert_called_once_with(b"%PDF")

    def test_select_preview_opens_preview_window(self) -> None:
        """選択プレビューで VoucherPrintPreviewWindow が開かれること。"""
        win = self._make_window()
        win._on_add_row()
        self._mark_fetched(win, win._rows[0], "1111111")
        self._mark_fetched(win, win._rows[1], "2222222")
        win._rows[0].select_check.setChecked(True)
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%MERGED"), \
                mock.patch("app.voucher_preview_window.VoucherPrintPreviewWindow") as preview_cls:
            win._on_select_preview()
        preview_cls.assert_called_once()
        self.assertEqual(preview_cls.call_args.args[0], b"%MERGED")
        preview_cls.return_value.showMaximized.assert_called_once_with()

    def test_select_pdf_validates_each_row_with_number(self) -> None:
        """選択行に不正があれば、行番号付きで中断すること。"""
        win = self._make_window()
        win._on_add_row()
        self._mark_fetched(win, win._rows[0], "1111111")
        win._rows[1].order_input.setText("")  # 2行目は受注No未入力
        win._set_all_rows_checked(True)
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            win._on_select_pdf()
        warn.assert_called_once()
        self.assertIn("2行目", warn.call_args.args[2])
        build.assert_not_called()

    def test_select_pdf_noop_without_selection(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("1111111")
        with mock.patch.object(win, "_build_print_data") as build:
            win._on_select_pdf()
        build.assert_not_called()

    def test_select_order_no_add_targets_selected_non_empty_unique_orders(self) -> None:
        class FakeKintoneWindow:
            def __init__(self) -> None:
                self.values = ["0001234"]
                self.calls = []

            def get_order_numbers(self):
                return set(self.values)

            def add_order_no(self, order_no, finish_date=None, am_pm=None):
                self.calls.append((order_no, finish_date, am_pm))
                if order_no not in self.values:
                    self.values.append(order_no)

        fake = FakeKintoneWindow()
        win = self._make_window_with_kintone(fake)
        win._on_add_row()
        win._on_add_row()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "0001234")
        rows[1].order_input.setText("")
        self._mark_fetched(win, rows[2], "0005678")
        self._mark_fetched(win, rows[3], "0005678")
        for rw in win._rows:
            rw.select_check.setChecked(True)

        win._on_select_order_no_add()

        self.assertEqual(sorted(call[0] for call in fake.calls), ["0001234", "0005678"])
        self.assertEqual(fake.values, ["0001234", "0005678"])

    def test_select_order_no_add_warns_when_selected_orders_are_empty(self) -> None:
        win = self._make_window_with_kintone(mock.Mock())
        win._rows[0].order_input.setText("")
        win._rows[0].select_check.setChecked(True)

        with mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            win._on_select_order_no_add()

        warn.assert_called_once()
        self.assertEqual(warn.call_args.args[2], "追加できる受注Noがありません。")


    # ── 切断仕上日カラム・印刷する伝票設定 ───────────────────────────────────
    def test_no_cut_finish_date_column(self) -> None:
        """画面に「切断仕上日」カラムが追加されていないこと。"""
        from app.voucher_window import COLUMN_LABELS

        self.assertNotIn("切断仕上日", COLUMN_LABELS)
        win = self._make_window()
        headers = [win._table.horizontalHeaderItem(i).text()
                   for i in range(win._table.columnCount())]
        self.assertNotIn("切断仕上日", headers)

    def test_legacy_settings_buttons_removed_from_header(self) -> None:
        # ヘッダー上の設定入口は「設定」1つに集約し、旧ボタンは撤去する（要件1）。
        win = self._make_window()
        self.assertFalse(hasattr(win, "_voucher_settings_button"))
        self.assertFalse(hasattr(win, "_printer_settings_button"))
        self.assertEqual(win._display_settings_button.text(), "設定")

    def test_settings_button_label_is_settings_not_display_settings(self) -> None:
        # ヘッダーのボタンは「設定」であり、「表示設定」ではない（要件1）。
        win = self._make_window()
        texts = [
            b.text() for b in win.findChildren(type(win._display_settings_button))
        ]
        self.assertIn("設定", texts)
        self.assertNotIn("表示設定", texts)
        self.assertNotIn("印刷設定", texts)
        self.assertNotIn("伝票設定", texts)

    def test_legacy_settings_buttons_not_in_layout_source(self) -> None:
        source = Path("app/voucher_window.py").read_text(encoding="utf-8")
        layout_source = source[source.index("def _build_layout") : source.index("def _wrap")]
        self.assertNotIn("top_row.addWidget(self._voucher_settings_button)", layout_source)
        self.assertNotIn("top_row.addWidget(self._printer_settings_button)", layout_source)
        self.assertIn("top_row.addWidget(self._display_settings_button)", layout_source)

    def test_legacy_voucher_settings_opens_combined_voucher_tab(self) -> None:
        # _on_voucher_settings は統合設定ダイアログの伝票設定タブを初期表示で開く（要件4）。
        from PySide6.QtWidgets import QDialog
        from app.voucher_window import CombinedVoucherSettingsDialog

        win = self._make_window()
        captured = {}

        def fake_exec(dialog):
            captured["tab"] = dialog._tabs.currentIndex()
            return QDialog.DialogCode.Rejected

        with mock.patch.object(CombinedVoucherSettingsDialog, "exec", new=fake_exec):
            win._on_voucher_settings()
        self.assertEqual(captured["tab"], CombinedVoucherSettingsDialog.TAB_INDEX_BY_NAME["voucher"])

    def test_legacy_printer_settings_opens_combined_printer_tab(self) -> None:
        from PySide6.QtWidgets import QDialog
        from app.voucher_window import CombinedVoucherSettingsDialog

        win = self._make_window()
        captured = {}

        def fake_exec(dialog):
            captured["tab"] = dialog._tabs.currentIndex()
            return QDialog.DialogCode.Rejected

        with mock.patch.object(CombinedVoucherSettingsDialog, "exec", new=fake_exec):
            win._on_printer_settings()
        self.assertEqual(captured["tab"], CombinedVoucherSettingsDialog.TAB_INDEX_BY_NAME["printer"])

    def _clear_visible_columns_setting(self) -> None:
        from PySide6.QtCore import QSettings
        from app.voucher_settings import VOUCHER_VISIBLE_COLUMNS_KEY

        store = QSettings("Manekiya", "TksToKintone")
        store.remove(VOUCHER_VISIBLE_COLUMNS_KEY)
        store.sync()

    def test_display_settings_button_present(self) -> None:
        # ヘッダーに「設定」ボタンがある（要件1）。
        win = self._make_window()
        self.assertEqual(win._display_settings_button.text(), "設定")

    def test_combined_settings_dialog_has_three_categories(self) -> None:
        from PySide6.QtWidgets import QScrollArea
        from app.voucher_window import (
            CombinedVoucherSettingsDialog,
            default_visible_columns,
        )

        dialog = CombinedVoucherSettingsDialog(
            visible_columns=default_visible_columns(),
            selected_ids={"01"},
            retention_days=60,
            record_retention_days=1095,
            finish_date_none=False,
            ampm_default="am",
        )
        self.addCleanup(dialog.deleteLater)
        titles = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]
        self.assertEqual(
            titles, ["表示設定", "印刷設定", "伝票設定", "伝票加工名設定"])
        # 125%以上でも見切れないよう各タブは QScrollArea に入っている（要件2/4）。
        for i in range(dialog._tabs.count()):
            self.assertIsInstance(dialog._tabs.widget(i), QScrollArea)

    def test_apply_to_current_list_checkbox_default_off(self) -> None:
        # 伝票設定タブに「現在の一覧に反映する」があり、初期状態はOFF（要件4）。
        from app.voucher_window import (
            CombinedVoucherSettingsDialog,
            default_visible_columns,
        )

        dialog = CombinedVoucherSettingsDialog(
            visible_columns=default_visible_columns(),
            selected_ids={"01"},
            retention_days=60,
            record_retention_days=1095,
            finish_date_none=False,
            ampm_default="am",
        )
        self.addCleanup(dialog.deleteLater)
        cb = dialog.voucher_tab._apply_to_current_check
        self.assertEqual(cb.text(), "現在の一覧に反映する")
        self.assertFalse(cb.isChecked())
        self.assertFalse(dialog.apply_to_current_list_requested())

    def test_display_settings_button_opens_combined_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog

        win = self._make_window()
        with mock.patch(
            "app.voucher_window.CombinedVoucherSettingsDialog"
        ) as fake_dialog:
            fake_dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            win._on_display_settings()
        fake_dialog.assert_called_once()

    def test_column_visibility_hides_and_shows(self) -> None:
        from app.voucher_window import COL_EDIT

        self.addCleanup(self._clear_visible_columns_setting)
        win = self._make_window()
        # 「□」「受注No」「OLAP」以外（ここでは操作列の指図書編集）は非表示にできる（要件2）。
        vis = dict(win._visible_columns)
        vis["edit"] = False
        win._save_column_visibility(vis)
        self.assertTrue(win._table.isColumnHidden(COL_EDIT))
        vis["edit"] = True
        win._save_column_visibility(vis)
        self.assertFalse(win._table.isColumnHidden(COL_EDIT))

    def test_operation_column_can_be_hidden(self) -> None:
        # 右端の操作ボタン列（印刷・Kintone登録など）も非表示にできる（要件2）。
        from app.voucher_window import COL_KINTONE, COL_PRINT

        self.addCleanup(self._clear_visible_columns_setting)
        win = self._make_window()
        vis = dict(win._visible_columns)
        vis["print"] = False
        vis["kintone"] = False
        win._save_column_visibility(vis)
        self.assertTrue(win._table.isColumnHidden(COL_PRINT))
        self.assertTrue(win._table.isColumnHidden(COL_KINTONE))

    def test_required_columns_cannot_be_hidden(self) -> None:
        from app.voucher_window import (
            COL_ORDER_NO,
            COL_REFETCH,
            COL_SELECT,
            VOUCHER_COLUMN_SPECS,
        )

        self.addCleanup(self._clear_visible_columns_setting)
        win = self._make_window()
        # 全列OFFを試みても、固定列「□」「受注No」「OLAP」は常に表示される（要件2）。
        vis = {spec.key: False for spec in VOUCHER_COLUMN_SPECS}
        win._save_column_visibility(vis)
        self.assertFalse(win._table.isColumnHidden(COL_SELECT))
        self.assertFalse(win._table.isColumnHidden(COL_ORDER_NO))
        self.assertFalse(win._table.isColumnHidden(COL_REFETCH))
        # 固定3列が常に残るため、すべての列が非表示になることはない（要件2）。
        visible_count = sum(
            0 if win._table.isColumnHidden(spec.index) else 1
            for spec in VOUCHER_COLUMN_SPECS
        )
        self.assertGreaterEqual(visible_count, 3)

    def test_column_visibility_persists_and_restores(self) -> None:
        from app.voucher_window import COL_EDIT

        self.addCleanup(self._clear_visible_columns_setting)
        win = self._make_window()
        vis = dict(win._visible_columns)
        vis["edit"] = False
        win._save_column_visibility(vis)
        # 再起動相当: 別インスタンスで設定が復元される。
        win2 = self._make_window()
        self.assertFalse(win2._visible_columns["edit"])
        self.assertTrue(win2._table.isColumnHidden(COL_EDIT))

    def test_resolve_visible_columns_ignores_unknown_old_keys(self) -> None:
        from app.voucher_window import resolve_visible_columns

        result = resolve_visible_columns(["order_no", "olap", "removed_old_column"])
        self.assertNotIn("removed_old_column", result)
        self.assertTrue(result["olap"])
        # 保存値に無い hideable 列は非表示になる。
        self.assertFalse(result["finish_date"])
        # 必須列は保存値に無くても常に表示。
        self.assertTrue(result["select"])

    def test_column_visibility_widget_show_all_and_reset(self) -> None:
        from app.voucher_window import _ColumnVisibilityWidget, default_visible_columns

        vis = default_visible_columns()
        vis["olap"] = False
        vis["finish_date"] = False
        widget = _ColumnVisibilityWidget(vis)
        self.addCleanup(widget.deleteLater)
        widget._on_show_all()
        self.assertTrue(all(widget.visible_columns().values()))
        widget._on_reset_default()
        restored = widget.visible_columns()
        self.assertTrue(restored["olap"])
        self.assertTrue(restored["finish_date"])

    def test_voucher_settings_dialog_has_finish_none_and_ampm_defaults(self) -> None:
        from app.voucher_window import VoucherPrintSettingsDialog

        dialog = VoucherPrintSettingsDialog(
            selected_ids={"01"}, retention_days=60,
            finish_date_none=True, ampm_default="pm",
        )
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog._finish_date_none_check.text(), "なし")
        self.assertTrue(dialog.finish_date_none())
        self.assertEqual(dialog._ampm_none.text(), "なし")
        self.assertEqual(dialog._ampm_am.text(), "AM")
        self.assertEqual(dialog._ampm_pm.text(), "PM")
        self.assertEqual(dialog.ampm_default(), "pm")

    def test_voucher_finish_and_ampm_default_settings_are_saved(self) -> None:
        from app.voucher_settings import (
            load_default_ampm,
            load_default_finish_date_none,
            normalize_finish_date_none,
            save_default_ampm,
            save_default_finish_date_none,
        )

        save_default_finish_date_none(True)
        save_default_ampm("none")
        self.assertTrue(load_default_finish_date_none())
        self.assertEqual(load_default_ampm(), "none")
        self.assertFalse(normalize_finish_date_none("false"))
        self.assertFalse(normalize_finish_date_none("0"))
        self.assertFalse(normalize_finish_date_none("no"))
        self.assertTrue(normalize_finish_date_none("true"))

    def test_voucher_finish_date_none_off_save_overrides_previous_on(self) -> None:
        from app.voucher_settings import (
            load_default_finish_date_none,
            save_default_finish_date_none,
        )

        save_default_finish_date_none(True)
        self.assertTrue(load_default_finish_date_none())
        save_default_finish_date_none(False)
        self.assertFalse(load_default_finish_date_none())

    def test_voucher_finish_date_none_reads_false_string_as_off(self) -> None:
        from app.config import update_values_in_config, user_config_path
        from app.voucher_settings import (
            VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY,
            load_default_finish_date_none,
        )

        update_values_in_config(
            user_config_path(),
            {VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY: "false"},
        )
        self.assertFalse(load_default_finish_date_none())

    def test_voucher_finish_date_none_reads_true_string_as_on(self) -> None:
        from app.config import update_values_in_config, user_config_path
        from app.voucher_settings import (
            VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY,
            load_default_finish_date_none,
        )

        update_values_in_config(
            user_config_path(),
            {VOUCHER_DEFAULT_FINISH_DATE_NONE_KEY: "true"},
        )
        self.assertTrue(load_default_finish_date_none())

    def test_voucher_settings_dialog_treats_false_string_as_off(self) -> None:
        from app.voucher_window import VoucherPrintSettingsDialog

        dialog = VoucherPrintSettingsDialog(
            selected_ids={"01"}, retention_days=60,
            finish_date_none="false", ampm_default="am",
        )
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.finish_date_none())

    def test_voucher_default_finish_none_and_ampm_apply_to_new_rows(self) -> None:
        from app.voucher_settings import save_default_ampm, save_default_finish_date_none

        save_default_finish_date_none(True)
        save_default_ampm("pm")
        win = self._make_window()
        rw = win._rows[0]
        self.assertTrue(rw.finish_none_check.isChecked())
        self.assertFalse(rw.date_edit.isEnabled())
        self.assertTrue(rw.ampm_pm.isChecked())

    def test_voucher_default_finish_none_off_applies_to_new_rows_after_on(self) -> None:
        from app.voucher_settings import save_default_finish_date_none

        save_default_finish_date_none(True)
        save_default_finish_date_none(False)
        win = self._make_window()
        rw = win._rows[0]
        self.assertFalse(rw.finish_none_check.isChecked())

    def test_voucher_default_ampm_none_applies_to_new_rows(self) -> None:
        from app.voucher_settings import save_default_ampm

        save_default_ampm("none")
        win = self._make_window()
        self.assertTrue(win._rows[0].ampm_none.isChecked())

    def test_voucher_default_ampm_am_applies_to_new_rows(self) -> None:
        from app.voucher_settings import save_default_ampm

        save_default_ampm("am")
        win = self._make_window()
        self.assertTrue(win._rows[0].ampm_am.isChecked())

    def test_voucher_default_ampm_pm_applies_to_new_rows(self) -> None:
        from app.voucher_settings import save_default_ampm

        save_default_ampm("pm")
        win = self._make_window()
        self.assertTrue(win._rows[0].ampm_pm.isChecked())

    def test_voucher_settings_yes_applies_finish_none_and_ampm_to_existing_rows(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.finish_none_check.setChecked(False)
        rw.ampm_am.setChecked(True)

        win._apply_print_settings_to_rows(
            {"01", "03"},
            finish_date_none=True,
            ampm_default="pm",
        )

        self.assertTrue(rw.finish_none_check.isChecked())
        self.assertTrue(rw.ampm_pm.isChecked())
        row = win._collect_row(rw)
        self.assertIsNone(row.finish_date)
        self.assertEqual(row.am_pm, "PM")
        self.assertTrue(row.voucher_checks["01"])
        self.assertFalse(row.voucher_checks["02"])
        self.assertTrue(row.voucher_checks["03"])

    def test_voucher_settings_yes_applies_ampm_none_to_existing_rows(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.ampm_pm.setChecked(True)

        win._apply_print_settings_to_rows(
            {"01"},
            finish_date_none=False,
            ampm_default="none",
        )

        self.assertTrue(rw.ampm_none.isChecked())
        self.assertEqual(win._collect_row(rw).am_pm, "none")

    def test_voucher_settings_finish_none_off_does_not_clear_existing_date(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.date_edit.setDate(QDate(2026, 6, 20))
        rw.finish_none_check.setChecked(True)

        win._apply_print_settings_to_rows(
            {"01"},
            finish_date_none=False,
            ampm_default="am",
        )

        self.assertFalse(rw.finish_none_check.isChecked())
        self.assertEqual(win._collect_row(rw).finish_date, date(2026, 6, 20))

    def test_voucher_settings_yes_finish_none_off_updates_internal_and_pdf_data(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.date_edit.setDate(QDate(2026, 7, 2))
        rw.finish_none_check.setChecked(True)

        win._apply_print_settings_to_rows(
            {"01"},
            finish_date_none=False,
            ampm_default="am",
        )
        row = win._collect_row(rw)
        data = {"pages": [{}, {}]}
        win._attach_row_settings(data, row)

        self.assertFalse(rw.finish_none_check.isChecked())
        self.assertFalse(row.finish_date_none)
        self.assertEqual(row.finish_date, date(2026, 7, 2))
        self.assertFalse(data["finish_date_none"])
        self.assertEqual(data["finish_date"], date(2026, 7, 2))
        for page in data["pages"]:
            self.assertFalse(page["row_finish_date_none"])
            self.assertEqual(page["row_finish_date"], date(2026, 7, 2))

    def test_voucher_settings_no_keeps_existing_rows_but_updates_new_rows(self) -> None:
        from PySide6.QtWidgets import QDialog

        class FakeDialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def set_record_retention_days(self, days: int) -> None:
                self.record_retention = days

            def select_tab(self, name: str) -> None:
                self.selected_tab = name

            def visible_columns(self):
                from app.voucher_window import default_visible_columns

                return default_visible_columns()

            def printer_values(self):
                from app.voucher_settings import load_voucher_printer_settings

                return load_voucher_printer_settings()

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_ids(self) -> set[str]:
                return {"01"}

            def retention_days(self) -> int:
                return 60

            def record_retention_days(self) -> int:
                return 1095

            def finish_date_none(self) -> bool:
                return True

            def ampm_default(self) -> str:
                return "pm"

        # 「現在の一覧に反映する」OFF（要件4）→ 既存行は変わらない。
        FakeDialog.apply_to_current_list_requested = lambda self: False
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.finish_none_check.setChecked(False)
        rw.ampm_am.setChecked(True)

        with mock.patch("app.voucher_window.CombinedVoucherSettingsDialog", FakeDialog), \
                mock.patch("app.voucher_window.QMessageBox.question") as question:
            win._on_voucher_settings()

        # OK後の確認ダイアログは出さない（要件3）。
        question.assert_not_called()
        self.assertFalse(rw.finish_none_check.isChecked())
        self.assertTrue(rw.ampm_am.isChecked())
        win._on_add_row()
        new_row = next(r for r in win._rows if not r.order_input.text().strip())
        self.assertTrue(new_row.finish_none_check.isChecked())
        self.assertTrue(new_row.ampm_pm.isChecked())

    def test_voucher_settings_no_finish_none_off_keeps_existing_rows_but_updates_new_rows(self) -> None:
        from PySide6.QtWidgets import QDialog

        class FakeDialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def set_record_retention_days(self, days: int) -> None:
                self.record_retention = days

            def select_tab(self, name: str) -> None:
                self.selected_tab = name

            def visible_columns(self):
                from app.voucher_window import default_visible_columns

                return default_visible_columns()

            def printer_values(self):
                from app.voucher_settings import load_voucher_printer_settings

                return load_voucher_printer_settings()

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_ids(self) -> set[str]:
                return {"01"}

            def retention_days(self) -> int:
                return 60

            def record_retention_days(self) -> int:
                return 1095

            def finish_date_none(self) -> bool:
                return False

            def ampm_default(self) -> str:
                return "pm"

        from app.voucher_settings import save_default_finish_date_none

        FakeDialog.apply_to_current_list_requested = lambda self: False
        save_default_finish_date_none(True)
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.finish_none_check.setChecked(True)
        rw.ampm_am.setChecked(True)

        with mock.patch("app.voucher_window.CombinedVoucherSettingsDialog", FakeDialog), \
                mock.patch("app.voucher_window.QMessageBox.question") as question:
            win._on_voucher_settings()

        question.assert_not_called()
        self.assertTrue(rw.finish_none_check.isChecked())
        self.assertIsNone(win._collect_row(rw).finish_date)
        win._on_add_row()
        new_row = next(r for r in win._rows if not r.order_input.text().strip())
        self.assertFalse(new_row.finish_none_check.isChecked())
        self.assertFalse(win._collect_row(new_row).finish_date_none)

    def test_voucher_settings_yes_dialog_updates_existing_rows_and_saved_values(self) -> None:
        from PySide6.QtWidgets import QDialog
        from app.voucher_settings import load_default_ampm, load_default_finish_date_none

        class FakeDialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def set_record_retention_days(self, days: int) -> None:
                self.record_retention = days

            def select_tab(self, name: str) -> None:
                self.selected_tab = name

            def visible_columns(self):
                from app.voucher_window import default_visible_columns

                return default_visible_columns()

            def printer_values(self):
                from app.voucher_settings import load_voucher_printer_settings

                return load_voucher_printer_settings()

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_ids(self) -> set[str]:
                return {"02"}

            def retention_days(self) -> int:
                return 60

            def record_retention_days(self) -> int:
                return 1095

            def finish_date_none(self) -> bool:
                return True

            def ampm_default(self) -> str:
                return "none"

        # 「現在の一覧に反映する」ON（要件4）→ 既存行へ反映される。
        FakeDialog.apply_to_current_list_requested = lambda self: True
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.finish_none_check.setChecked(False)
        rw.ampm_pm.setChecked(True)

        with mock.patch("app.voucher_window.CombinedVoucherSettingsDialog", FakeDialog), \
                mock.patch("app.voucher_window.QMessageBox.question") as question:
            win._on_voucher_settings()

        question.assert_not_called()
        self.assertTrue(load_default_finish_date_none())
        self.assertEqual(load_default_ampm(), "none")
        self.assertTrue(rw.finish_none_check.isChecked())
        self.assertTrue(rw.ampm_none.isChecked())
        row = win._collect_row(rw)
        self.assertIsNone(row.finish_date)
        self.assertEqual(row.am_pm, "none")
        self.assertFalse(row.voucher_checks["01"])
        self.assertTrue(row.voucher_checks["02"])

    def test_voucher_settings_yes_dialog_finish_none_off_updates_existing_rows_and_saved_values(self) -> None:
        from PySide6.QtWidgets import QDialog
        from app.voucher_settings import load_default_ampm, load_default_finish_date_none, save_default_finish_date_none

        class FakeDialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def set_record_retention_days(self, days: int) -> None:
                self.record_retention = days

            def select_tab(self, name: str) -> None:
                self.selected_tab = name

            def visible_columns(self):
                from app.voucher_window import default_visible_columns

                return default_visible_columns()

            def printer_values(self):
                from app.voucher_settings import load_voucher_printer_settings

                return load_voucher_printer_settings()

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_ids(self) -> set[str]:
                return {"02"}

            def retention_days(self) -> int:
                return 60

            def record_retention_days(self) -> int:
                return 1095

            def finish_date_none(self) -> bool:
                return False

            def ampm_default(self) -> str:
                return "pm"

        FakeDialog.apply_to_current_list_requested = lambda self: True
        save_default_finish_date_none(True)
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.date_edit.setDate(QDate(2026, 6, 20))
        rw.finish_none_check.setChecked(True)
        rw.ampm_am.setChecked(True)

        with mock.patch("app.voucher_window.CombinedVoucherSettingsDialog", FakeDialog), \
                mock.patch("app.voucher_window.QMessageBox.question") as question:
            win._on_voucher_settings()

        question.assert_not_called()
        self.assertFalse(load_default_finish_date_none())
        self.assertEqual(load_default_ampm(), "pm")
        self.assertFalse(rw.finish_none_check.isChecked())
        self.assertTrue(rw.ampm_pm.isChecked())
        row = win._collect_row(rw)
        self.assertFalse(row.finish_date_none)
        self.assertEqual(row.finish_date, date(2026, 6, 20))
        self.assertFalse(row.voucher_checks["01"])
        self.assertTrue(row.voucher_checks["02"])

    def test_finish_none_and_ampm_defaults_reach_pdf_data(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        win._apply_print_settings_to_rows(
            {"01"},
            finish_date_none=True,
            ampm_default="pm",
        )
        row = win._collect_row(rw)
        data = {"pages": [{}, {}]}
        win._attach_row_settings(data, row)

        self.assertIsNone(data["finish_date"])
        self.assertTrue(data["finish_date_none"])
        self.assertEqual(data["am_pm"], "PM")
        for page in data["pages"]:
            self.assertIsNone(page["row_finish_date"])
            self.assertTrue(page["row_finish_date_none"])
            self.assertEqual(page["row_am_pm"], "PM")

    def test_select_header_is_checkbox_and_select_all_button_removed(self) -> None:
        win = self._make_window()
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "□")
        self.assertFalse(hasattr(win, "_select_all_check"))
        self.assertFalse(any(cb.text() == "全選択" for cb in win.findChildren(QCheckBox)))

    def test_display_settings_button_is_top_right(self) -> None:
        source = Path("app/voucher_window.py").read_text(encoding="utf-8")
        layout_source = source[source.index("def _build_layout") : source.index("def _wrap")]
        self.assertLess(
            layout_source.index("top_row.addWidget(self._select_print_button)"),
            layout_source.index("top_row.addWidget(self._display_settings_button)"),
        )

    def test_select_order_no_button_is_between_select_print_and_settings(self) -> None:
        source = Path("app/voucher_window.py").read_text(encoding="utf-8")
        layout_source = source[source.index("def _build_layout") : source.index("def _wrap")]
        win = self._make_window()
        self.assertEqual(win._select_order_no_button.text(), "選択受注No追加")
        self.assertLess(
            layout_source.index("top_row.addWidget(self._select_print_button)"),
            layout_source.index("top_row.addWidget(self._select_order_no_button)"),
        )
        self.assertLess(
            layout_source.index("top_row.addWidget(self._select_order_no_button)"),
            layout_source.index("top_row.addWidget(self._display_settings_button)"),
        )

    def test_default_print_types_reflected_in_new_row(self) -> None:
        """_default_print_types が新規追加行の印刷する伝票チェックへ反映されること。"""
        win = self._make_window()
        win._default_print_types = {"01", "03"}
        win._on_add_row()
        rw = win._rows[0]
        self.assertTrue(rw.voucher_checks["01"].isChecked())
        self.assertTrue(rw.voucher_checks["03"].isChecked())
        self.assertFalse(rw.voucher_checks["02"].isChecked())
        self.assertFalse(rw.voucher_checks["07"].isChecked())

    def test_apply_print_types_to_rows_overwrites(self) -> None:
        """既存行反映で全行の印刷する伝票チェックが設定値で上書きされること。"""
        win = self._make_window()
        win._on_add_row()
        win._apply_print_types_to_rows({"05"})
        for rw in win._rows:
            self.assertTrue(rw.voucher_checks["05"].isChecked())
            self.assertFalse(rw.voucher_checks["01"].isChecked())

    # ── 削除廃止・最大化・列区切り ───────────────────────────────────────────
    def test_delete_column_and_row_buttons_do_not_exist(self) -> None:
        win = self._make_window()
        from app.voucher_window import COLUMN_LABELS

        self.assertNotIn("削除", COLUMN_LABELS)
        self.assertFalse(hasattr(win._rows[0], "delete_button"))
        self.assertFalse(hasattr(win, "_on_delete_row"))

    def test_show_maximizes_window(self) -> None:
        win = self._make_window()
        with mock.patch.object(win, "showMaximized") as maximized:
            win.show()
        maximized.assert_called_once()

    def test_show_event_adjusts_column_widths(self) -> None:
        win = self._make_window()
        with mock.patch.object(win, "_apply_table_column_widths") as adjust:
            from PySide6.QtGui import QShowEvent

            win.showEvent(QShowEvent())
        adjust.assert_called()

    def test_process_and_voucher_columns_are_wide(self) -> None:
        from app.voucher_window import COL_PROCESS, COL_VOUCHER

        win = self._make_window()
        # 加工名・印刷する伝票はラベルが見切れない最低幅を確保する。
        self.assertGreaterEqual(win._table.columnWidth(COL_PROCESS),
                                win.PROCESS_MIN_WIDTH)
        self.assertGreaterEqual(win._table.columnWidth(COL_VOUCHER),
                                win.VOUCHER_MIN_WIDTH)

    def test_table_has_grid_divider_style(self) -> None:
        win = self._make_window()
        self.assertTrue(win._table.showGrid())
        style = win._table.styleSheet()
        self.assertIn("gridline-color", style)
        self.assertIn("QHeaderView::section", style)

    # ── 加工名（3列）・印刷する伝票（2列）の配置（要件1）─────────────────────────
    def test_process_checkboxes_three_columns(self) -> None:
        """加工名チェックボックスが3列に配置されること（要件1）。"""
        from PySide6.QtWidgets import QGridLayout

        from app.voucher_window import COL_PROCESS, PROCESS_COLUMNS

        self.assertEqual(PROCESS_COLUMNS, 3)
        win = self._make_window()
        cell = win._table.cellWidget(0, COL_PROCESS)
        grid = cell.layout()
        self.assertIsInstance(grid, QGridLayout)
        self.assertEqual(grid.columnCount(), 3)

    def test_voucher_checkboxes_two_columns(self) -> None:
        """印刷する伝票チェックボックスが2列に配置されること（要件1）。"""
        from PySide6.QtWidgets import QGridLayout

        from app.voucher_window import COL_VOUCHER, VOUCHER_COLUMNS

        self.assertEqual(VOUCHER_COLUMNS, 2)
        win = self._make_window()
        cell = win._table.cellWidget(0, COL_VOUCHER)
        grid = cell.layout()
        self.assertIsInstance(grid, QGridLayout)
        self.assertEqual(grid.columnCount(), 2)

    # ── OLAP列・ボタン（要件2・6）─────────────────────────────────────────
    def test_refetch_column_between_order_no_and_finish_date(self) -> None:
        from app.voucher_window import (
            COLUMN_LABELS,
            COL_FINISH_DATE,
            COL_ORDER_NO,
            COL_REFETCH,
        )

        self.assertEqual(COL_REFETCH, COL_ORDER_NO + 1)
        self.assertEqual(COL_FINISH_DATE, COL_REFETCH + 1)
        self.assertEqual(COLUMN_LABELS[COL_REFETCH], "OLAP")

    def test_row_has_refetch_button(self) -> None:
        from app.voucher_window import COL_REFETCH

        win = self._make_window()
        rw = win._rows[0]
        self.assertIsInstance(rw.refetch_button, QPushButton)
        self.assertEqual(rw.refetch_button.text(), "取得")
        self.assertEqual(rw.refetch_button.property("buttonRole"), "olapFetch")
        widget = win._table.cellWidget(0, COL_REFETCH)
        self.assertIsNotNone(widget)
        self.assertIn(rw.refetch_button, widget.findChildren(QPushButton))

    def test_fetched_row_refetch_button_is_update(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw)
        self.assertEqual(rw.refetch_button.text(), "更新")
        self.assertEqual(rw.refetch_button.property("buttonRole"), "olapUpdate")

    def test_refetch_calls_olap_for_order_no(self) -> None:
        """OLAPボタン押下で対象受注NoのOLAP取得処理が呼ばれること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}) as build, \
                mock.patch.object(win, "_cache_row_olap") as cache:
            rw.refetch_button.click()
        build.assert_called_once_with(["5218869"])
        cache.assert_called_once()

    def test_refetch_success_updates_row_data(self) -> None:
        """OLAP取得成功時に対象行のOLAPデータ（cached_olap）が更新されること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        self.assertIsNone(rw.cached_olap)
        data = {"pages": [{}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertIs(rw.cached_olap, data)
        # ボタン文言・有効状態が取得済みに更新される。
        self.assertEqual(rw.refetch_button.text(), "更新")
        self.assertTrue(rw.refetch_button.isEnabled())
        self.assertTrue(rw.order_input.isReadOnly())
        self.assertEqual(len(win._rows), 1)

    def test_refetch_failure_keeps_existing_data(self) -> None:
        """OLAP取得失敗時に既存データ（設定・OLAPデータ）が維持されること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        rw.ampm_pm.setChecked(True)
        with mock.patch.object(win, "_build_print_data", side_effect=RuntimeError("失敗")), \
                mock.patch.object(win, "_cache_row_olap") as cache, \
                mock.patch("app.voucher_window.QMessageBox.critical") as crit:
            rw.refetch_button.click()
        crit.assert_called_once()
        cache.assert_not_called()
        # 既存データ（cached_olap・AM/PM設定）が維持される。
        self.assertIsNone(rw.cached_olap)
        self.assertEqual(win._collect_row(rw).am_pm, "PM")
        self.assertEqual(rw.refetch_button.text(), "取得")
        self.assertTrue(rw.refetch_button.isEnabled())
        self.assertFalse(rw.order_input.isReadOnly())
        self.assertEqual(len(win._rows), 1)

    def test_refetch_update_success_does_not_add_row(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        original_updated_at = rw.updated_at
        with mock.patch.object(
            win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}
        ), mock.patch.object(win, "_cache_row_olap"), mock.patch(
            "app.voucher_window.QMessageBox.information"
        ) as info:
            rw.refetch_button.click()
        self.assertEqual(len(win._rows), 1)
        self.assertEqual(rw.refetch_button.text(), "更新")
        self.assertEqual(rw.updated_at, original_updated_at)
        info.assert_called_once()
        self.assertIn("OLAPデータを更新しました。", info.call_args.args[2])
        self.assertIn("5218869", info.call_args.args[2])

    def test_refetch_success_does_not_show_update_completion(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(
            win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}
        ), mock.patch.object(win, "_cache_row_olap"), mock.patch(
            "app.voucher_window.QMessageBox.information"
        ) as info:
            rw.refetch_button.click()
        info.assert_not_called()

    def test_refetch_update_failure_does_not_show_completion(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        with mock.patch.object(
            win, "_build_print_data", side_effect=RuntimeError("失敗")
        ), mock.patch("app.voucher_window.QMessageBox.critical"), mock.patch(
            "app.voucher_window.QMessageBox.information"
        ) as info:
            rw.refetch_button.click()
        info.assert_not_called()

    def test_refetch_success_reuses_existing_empty_unfetched_row(self) -> None:
        win = self._make_window()
        first_empty = win._rows[0]
        win._add_row()
        target = next(row for row in win._rows if row is not first_empty)
        target.order_input.setText("5218869")
        with mock.patch.object(
            win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}
        ), mock.patch.object(win, "_cache_row_olap"):
            target.refetch_button.click()
        self.assertEqual(len(win._rows), 2)
        self.assertIn(first_empty, win._rows)
        self.assertEqual(sum(not row.order_input.text().strip() for row in win._rows), 1)

    # ── 受注No重複防止（要件2・3・7）─────────────────────────────────────────
    def test_refetch_blocks_duplicate_order_no_of_existing_row(self) -> None:
        """既存行と同じ受注Noを新規行で取得しようとすると警告し取得しないこと。"""
        win = self._make_window()
        self._mark_fetched(win, win._rows[0], "1405113")
        win._on_add_row()
        new_row = win._rows[0]
        new_row.order_input.setText("1405113")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            new_row.refetch_button.click()
        warn.assert_called_once()
        self.assertEqual(
            warn.call_args.args[2], "受注No「1405113」はすでに一覧に存在します。"
        )
        build.assert_not_called()
        # 受注Noは編集可能のまま（取得済みにならない）。
        self.assertFalse(new_row.order_input.isReadOnly())
        self.assertEqual(new_row.refetch_button.text(), "取得")

    def test_refetch_self_order_no_is_not_duplicate(self) -> None:
        """自分自身の受注Noは重複扱いせず取得できること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("1405113")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}) as build, \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            rw.refetch_button.click()
        warn.assert_not_called()
        build.assert_called_once_with(["1405113"])

    def test_refetch_duplicate_detects_full_width_digits(self) -> None:
        """全角数字でも既存の半角受注Noと同一視して取得をブロックすること。"""
        win = self._make_window()
        self._mark_fetched(win, win._rows[0], "1405113")
        win._on_add_row()
        new_row = win._rows[0]
        new_row.order_input.setText("１４０５１１３")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            new_row.refetch_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_refetch_duplicate_blocks_regardless_of_registration_status(self) -> None:
        """登録完了済みの行と同じ受注Noでも未取得行は取得できないこと。"""
        win = self._make_window()
        self._mark_fetched(win, win._rows[0], "1405113")
        win.notify_kintone_registration_completed(["1405113"])
        win._on_add_row()
        new_row = win._rows[0]
        new_row.order_input.setText("1405113")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            new_row.refetch_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_restore_dedupes_same_order_no_keeping_newest(self) -> None:
        """保存済みデータ復元時、同じ受注Noは更新日時が新しい1件だけ残ること。"""
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-01T09:00:00", "order_no": "1405113", "am_pm": "AM", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "1405113", "am_pm": "PM", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "1405999", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            order_nos = [rw.order_input.text() for rw in win._rows]
            self.assertEqual(sorted(order_nos), ["1405113", "1405999"])
            # 1405113 は最新（PM）の1件のみ。
            kept = self._row_for_order(win, "1405113")
            self.assertEqual(win._collect_row(kept).am_pm, "PM")

    def test_refetch_empty_order_no_does_not_fetch(self) -> None:
        """受注Noが空の場合は再取得しないこと。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            rw.refetch_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_refetch_does_not_delete_edit_objects(self) -> None:
        """OLAP更新時に voucher_edit_objects 配下の編集JSONが削除されないこと。"""
        from app.path_utils import get_voucher_edit_objects_dir

        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")

        edit_dir = get_voucher_edit_objects_dir()
        edit_dir.mkdir(parents=True, exist_ok=True)
        marker = edit_dir / "test_refetch_marker_5218869.json"
        marker.write_text('{"objects": []}', encoding="utf-8")
        self.addCleanup(lambda: marker.exists() and marker.unlink())

        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        # 編集オブジェクトのファイルは残っている。
        self.assertTrue(marker.exists())

    def test_unfetched_row_disables_columns_right_of_finish_date(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self.assertFalse(rw.date_edit.isEnabled())
        self.assertFalse(rw.finish_none_check.isEnabled())
        self.assertFalse(rw.ampm_am.isEnabled())
        self.assertFalse(next(iter(rw.process_checks.values())).isEnabled())
        self.assertFalse(next(iter(rw.voucher_checks.values())).isEnabled())
        self.assertFalse(rw.edit_button.isEnabled())
        self.assertFalse(rw.pdf_button.isEnabled())
        self.assertFalse(rw.preview_button.isEnabled())
        self.assertFalse(rw.print_button.isEnabled())
        self.assertFalse(rw.kintone_button.isEnabled())
        self.assertTrue(rw.order_input.isEnabled())
        self.assertFalse(rw.order_input.isReadOnly())
        self.assertTrue(rw.refetch_button.isEnabled())
        self.assertEqual(rw.refetch_button.text(), "取得")

    def test_refetch_success_enables_right_side_and_locks_order_no(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{"order_no": "5218869"}], "raw_rows": []}), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertTrue(rw.date_edit.isEnabled())
        self.assertTrue(rw.ampm_am.isEnabled())
        self.assertTrue(next(iter(rw.process_checks.values())).isEnabled())
        self.assertTrue(next(iter(rw.voucher_checks.values())).isEnabled())
        self.assertTrue(rw.pdf_button.isEnabled())
        self.assertTrue(rw.preview_button.isEnabled())
        self.assertTrue(rw.print_button.isEnabled())
        self.assertTrue(rw.order_input.isReadOnly())
        self.assertEqual(rw.refetch_button.text(), "更新")

    def test_refetch_failure_keeps_unfetched_row_editable_and_disabled(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", side_effect=RuntimeError("失敗")), \
                mock.patch("app.voucher_window.QMessageBox.critical"):
            rw.refetch_button.click()
        self.assertFalse(rw.date_edit.isEnabled())
        self.assertFalse(rw.pdf_button.isEnabled())
        self.assertFalse(rw.order_input.isReadOnly())
        self.assertEqual(rw.refetch_button.text(), "取得")

    def test_unfetched_added_row_stays_visible_with_completed_filter(self) -> None:
        win = self._make_window()
        self._mark_fetched(win, win._rows[0], "DONE")
        win.notify_kintone_registration_completed(["DONE"])
        win._on_add_row()
        win._status_filter.setCurrentText("登録完了")
        self.assertEqual(self._visible_order_numbers(win)[0], "")

    def test_unfetched_saved_record_restores_as_get_disabled(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {
                        "updated_at": "2026-06-03T09:00:00",
                        "order_no": "NEW",
                        "has_olap_data": False,
                        "kintone_status": "未登録",
                    },
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            win = self._make_window()
            rw = win._rows[0]
            self.assertEqual(rw.order_input.text(), "NEW")
            self.assertEqual(rw.refetch_button.text(), "取得")
            self.assertFalse(rw.pdf_button.isEnabled())
            self.assertFalse(rw.order_input.isReadOnly())

    # ── フォント拡大・行高（要件4）─────────────────────────────────────────────
    def test_ui_font_point_size_enlarged(self) -> None:
        from app.theme_utils import UI_FONT_POINT_SIZE

        self.assertGreaterEqual(UI_FONT_POINT_SIZE, 12)

    def test_row_height_enlarged(self) -> None:
        """行高がAM/PM2段ラジオ・3段チェックに合わせて拡大されていること。"""
        win = self._make_window()
        self.assertGreaterEqual(
            win._table.verticalHeader().defaultSectionSize(), 100
        )

    def _row_font_widgets(self, win, row_index: int):
        from PySide6.QtWidgets import QWidget

        widgets: list[QWidget] = []
        rw = win._rows[row_index]
        widgets.extend([
            rw.select_check,
            rw.order_input,
            rw.refetch_button,
            rw.date_edit,
            rw.finish_none_check,
            rw.ampm_none,
            rw.ampm_am,
            rw.ampm_pm,
            rw.edit_button,
            rw.pdf_button,
            rw.preview_button,
            rw.print_button,
            rw.kintone_button,
            rw.kintone_status_button,
        ])
        widgets.extend(rw.process_checks.values())
        widgets.extend(rw.voucher_checks.values())
        for column in range(win._table.columnCount()):
            cell = win._table.cellWidget(row_index, column)
            if cell is not None:
                widgets.append(cell)
                widgets.extend(cell.findChildren(QWidget))
        return widgets

    def _assert_row_fonts_are_uniform(self, win) -> None:
        from app.voucher_window import VOUCHER_ROW_FONT_SIZE

        first_size = win._rows[0].order_input.font().pointSize()
        self.assertEqual(first_size, VOUCHER_ROW_FONT_SIZE)
        for row_index in range(len(win._rows)):
            for widget in self._row_font_widgets(win, row_index):
                with self.subTest(row=row_index, widget=type(widget).__name__):
                    self.assertEqual(widget.font().pointSize(), first_size)

    def test_added_row_widgets_use_same_font_size_as_first_row(self) -> None:
        win = self._make_window()
        win._on_add_row()
        self._assert_row_fonts_are_uniform(win)
        self.assertEqual(
            win._rows[0].order_input.font().pointSize(),
            win._rows[1].order_input.font().pointSize(),
        )
        self.assertEqual(
            win._rows[0].date_edit.font().pointSize(),
            win._rows[1].date_edit.font().pointSize(),
        )
        self.assertEqual(
            win._rows[0].pdf_button.font().pointSize(),
            win._rows[1].pdf_button.font().pointSize(),
        )

    def test_restored_rows_use_same_font_size(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            now = datetime.now().isoformat(timespec="seconds")
            payload = {
                "version": 1,
                "saved_at": now,
                "records": [
                    {"saved_at": now, "order_no": "1405113", "kintone_status": "未登録"},
                    {"saved_at": now, "order_no": "1405999", "kintone_status": "登録完了"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            self.assertEqual(len(win._rows), 2)
            self._assert_row_fonts_are_uniform(win)

    def test_filter_changes_keep_row_font_size(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("ABC100")
        win._rows[1].order_input.setText("XYZ200")

        win._order_search_edit.setText("ABC")
        win._status_filter.setCurrentText("未登録")
        win._order_search_edit.clear()

        self._assert_row_fonts_are_uniform(win)

    def test_rows_are_sorted_by_updated_at_desc(self) -> None:
        from datetime import datetime

        win = self._make_window()
        old = win._rows[0]
        old.order_input.setText("OLD")
        win._on_add_row()
        middle = win._rows[0]
        middle.order_input.setText("MIDDLE")
        win._on_add_row()
        newest = win._rows[0]
        newest.order_input.setText("NEW")

        old.updated_at = datetime(2026, 6, 1, 9, 0, 0)
        middle.updated_at = datetime(2026, 6, 2, 9, 0, 0)
        newest.updated_at = datetime(2026, 6, 3, 9, 0, 0)
        win._apply_filters()

        self.assertEqual([rw.order_input.text() for rw in win._rows], ["NEW", "MIDDLE", "OLD"])

    def test_restored_records_are_sorted_by_updated_at_desc(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-01T09:00:00", "order_no": "OLD", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "NEW", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "MID", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            self.assertEqual([rw.order_input.text() for rw in win._rows], ["NEW", "MID", "OLD"])

    def test_search_filter_keeps_updated_at_order(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-01T09:00:00", "order_no": "ABC-OLD", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "ABC-NEW", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "XYZ", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            win._order_search_edit.setText("ABC")
            self.assertEqual(self._visible_order_numbers(win), ["ABC-NEW", "ABC-OLD"])

    def test_registration_status_filter_keeps_updated_at_order(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-01T09:00:00", "order_no": "DONE-OLD", "kintone_status": "登録完了"},
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "DONE-NEW", "kintone_status": "登録完了"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "TODO", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            win._status_filter.setCurrentText("登録完了")
            self.assertEqual(self._visible_order_numbers(win), ["DONE-NEW", "DONE-OLD"])

    def test_add_row_goes_to_top_sets_updated_at_and_focus(self) -> None:
        from datetime import datetime

        win = self._make_window()
        win.show()
        self.app.processEvents()
        existing = win._rows[0]
        existing.order_input.setText("OLD")
        existing.updated_at = datetime(2026, 6, 1, 9, 0, 0)

        before_add = datetime.now()
        win._on_add_row()
        self.app.processEvents()

        added = win._rows[0]
        self.assertEqual(added.order_input.text(), "")
        self.assertGreaterEqual(added.updated_at, before_add)
        self.assertEqual(existing.updated_at, datetime(2026, 6, 1, 9, 0, 0))
        self.assertIs(self.app.focusWidget(), added.order_input)

    def test_input_and_check_changes_do_not_update_updated_at(self) -> None:
        from PySide6.QtCore import QDate

        win = self._make_window()
        rw = win._rows[0]
        fixed = datetime(2026, 6, 1, 9, 0, 0)
        rw.updated_at = fixed

        rw.order_input.setText("0012345")
        rw.date_edit.setDate(QDate(2026, 6, 18))
        rw.finish_none_check.setChecked(True)
        rw.ampm_pm.setChecked(True)
        next(iter(rw.process_checks.values())).setChecked(True)
        next(iter(rw.voucher_checks.values())).setChecked(True)

        self.assertEqual(rw.updated_at, fixed)

    def test_row_pdf_preview_print_do_not_update_updated_at(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "0012345")
        rw.voucher_checks["01"].setChecked(True)
        fixed = datetime(2026, 6, 1, 9, 0, 0)
        rw.updated_at = fixed

        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value=Path("/tmp")), \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch.object(win, "_create_pdf"):
            win._on_pdf(rw)
        self.assertEqual(rw.updated_at, fixed)

        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch.object(win, "_open_preview_window"):
            win._on_preview(rw)
        self.assertEqual(rw.updated_at, fixed)

        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_print_service.print_pdf_direct"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_print(rw)
        self.assertEqual(rw.updated_at, fixed)

    def test_select_order_no_add_does_not_update_updated_at(self) -> None:
        class FakeKintoneWindow:
            def get_order_numbers(self):
                return set()

            def add_order_no(self, order_no, finish_date=None, am_pm=None):
                return None

        win = self._make_window_with_kintone(FakeKintoneWindow())
        rw = win._rows[0]
        self._mark_fetched(win, rw, "0012345")
        rw.select_check.setChecked(True)
        fixed = datetime(2026, 6, 1, 9, 0, 0)
        rw.updated_at = fixed

        win._on_select_order_no_add()

        self.assertEqual(rw.updated_at, fixed)

    def test_registration_completion_updates_only_matching_updated_at(self) -> None:
        win = self._make_window()
        win._on_add_row()
        target = win._rows[0]
        other = win._rows[1]
        target.order_input.setText("0012345")
        other.order_input.setText("0099999")
        target.updated_at = datetime(2026, 6, 1, 9, 0, 0)
        other.updated_at = datetime(2026, 6, 2, 9, 0, 0)

        win.notify_kintone_registration_completed(["0012345"])

        self.assertGreater(target.updated_at, datetime(2026, 6, 2, 9, 0, 0))
        self.assertEqual(other.updated_at, datetime(2026, 6, 2, 9, 0, 0))
        self.assertEqual(win._rows[0].order_input.text(), "0012345")

    def test_non_registration_operations_do_not_change_updated_at_order(self) -> None:
        win = self._make_window()
        win._on_add_row()
        first = win._rows[0]
        second = win._rows[1]
        first.order_input.setText("FIRST")
        second.order_input.setText("SECOND")
        first.updated_at = datetime(2026, 6, 2, 9, 0, 0)
        second.updated_at = datetime(2026, 6, 1, 9, 0, 0)
        win._apply_filters()

        second.voucher_checks["01"].setChecked(True)
        second.ampm_pm.setChecked(True)
        win._apply_filters()

        self.assertEqual([rw.order_input.text() for rw in win._rows], ["FIRST", "SECOND"])

    def test_add_row_button_is_removed(self) -> None:
        win = self._make_window()
        self.assertFalse(hasattr(win, "_add_row_button"))

    def test_add_row_button_stays_removed_after_order_no_input(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("1405113")
        self.assertFalse(hasattr(win, "_add_row_button"))

    def test_whitespace_only_order_no_does_not_restore_add_row_button(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText(" 　")
        win._rows[0].order_input.setText("1405113")
        self.assertFalse(hasattr(win, "_add_row_button"))

    def test_hidden_empty_row_still_disables_add_row_button(self) -> None:
        win = self._make_window()
        empty = win._rows[0]
        win._on_add_row()
        filled = win._rows[0]
        filled.order_input.setText("ABC100")
        empty.order_input.setText("")

        win._order_search_edit.setText("ABC")

        self.assertFalse(win._table.isRowHidden(self._rows_index(win, empty)))
        self.assertFalse(hasattr(win, "_add_row_button"))

    @staticmethod
    def _rows_index(win, target) -> int:
        for rw in win._rows:
            if rw is target:
                return rw.table_row_index
        raise AssertionError("row widget not found")

    def test_restored_empty_order_no_disables_add_row_button(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "ABC100", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            self.assertFalse(hasattr(win, "_add_row_button"))

    def test_records_are_saved_with_updated_at(self) -> None:
        with _temp_home() as home:
            win = self._make_window()
            win._rows[0].order_input.setText("1405113")

            payload = json.loads((home / "work" / "voucher_records.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["records"][0]["order_no"], "1405113")
            self.assertTrue(payload["records"][0]["updated_at"])

    def test_legacy_records_without_updated_at_are_restored_and_backfilled(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"saved_at": "2026-06-01T09:00:00", "order_no": "LEGACY", "kintone_status": "未登録"},
                ],
            }
            path = work / "voucher_records.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            self.assertEqual(win._rows[0].order_input.text(), "LEGACY")
            self.assertEqual(win._rows[0].updated_at.isoformat(timespec="seconds"), "2026-06-01T09:00:00")

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["records"][0]["updated_at"], "2026-06-01T09:00:00")

    def test_restored_records_with_blank_or_invalid_updated_at_are_displayed(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "HAS-DATE", "kintone_status": "未登録"},
                    {"order_no": "MISSING-DATE", "kintone_status": "未登録"},
                    {"updated_at": "", "order_no": "BLANK-DATE", "kintone_status": "未登録"},
                    {"updated_at": "not-a-date", "order_no": "BAD-DATE", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            self.assertEqual(
                set(self._visible_order_numbers(win)),
                {"HAS-DATE", "MISSING-DATE", "BLANK-DATE", "BAD-DATE"},
            )

    def test_sort_uses_safe_updated_at_values(self) -> None:
        win = self._make_window()
        first = win._rows[0]
        first.order_input.setText("VALID")
        win._on_add_row()
        invalid = win._rows[0]
        invalid.order_input.setText("INVALID")
        win._on_add_row()
        missing = win._rows[0]
        missing.order_input.setText("MISSING")

        first.updated_at = datetime(2026, 6, 3, 9, 0, 0)
        invalid.updated_at = "not-a-date"
        missing.updated_at = None
        win._apply_filters()

        visible = self._visible_order_numbers(win)
        self.assertEqual(visible[0], "VALID")
        self.assertEqual(set(visible[1:]), {"INVALID", "MISSING"})

    def test_sort_failure_does_not_empty_the_list(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("ABC100")
        win._on_add_row()
        win._rows[0].order_input.setText("ABC200")

        with mock.patch.object(win, "_sort_rows_by_updated_at", side_effect=RuntimeError("boom")):
            win._apply_filters()

        self.assertEqual(set(self._visible_order_numbers(win)), {"ABC100", "ABC200"})
        self.assertEqual(len(win._rows), 2)

    def test_all_status_filter_and_empty_search_show_all_records(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            payload = {
                "version": 1,
                "saved_at": "2026-06-03T12:00:00",
                "records": [
                    {"updated_at": "2026-06-03T09:00:00", "order_no": "DONE", "kintone_status": "登録完了"},
                    {"updated_at": "2026-06-02T09:00:00", "order_no": "TODO", "kintone_status": "未登録"},
                    {"updated_at": "2026-06-01T09:00:00", "order_no": "", "kintone_status": "未登録"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            win = self._make_window()
            win._status_filter.setCurrentText("すべて")
            win._order_search_edit.clear()

            # 受注Noが空の保存行は復元されない（新規行もどきを表示しない）。
            self.assertEqual(self._visible_order_numbers(win), ["DONE", "TODO"])
            self.assertFalse(hasattr(win, "_add_row_button"))

    def test_empty_added_row_is_visible_and_does_not_hide_other_records(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("ABC100")

        win._on_add_row()

        self.assertEqual(self._visible_order_numbers(win), ["", "ABC100"])
        self.assertFalse(hasattr(win, "_add_row_button"))
        self.assertFalse(win._table.isRowHidden(self._row_index_for_order(win, "ABC100")))

    def test_apply_filters_does_not_clear_internal_rows(self) -> None:
        win = self._make_window()
        self._mark_fetched(win, win._rows[0], "ABC100")
        win._on_add_row()
        self._mark_fetched(win, win._rows[0], "XYZ200")
        before_ids = {id(rw) for rw in win._rows}

        win._order_search_edit.setText("ABC")
        win._status_filter.setCurrentText("すべて")
        win._apply_filters()

        self.assertEqual({id(rw) for rw in win._rows}, before_ids)
        self.assertEqual(len(win._rows), 2)
        self.assertEqual(self._visible_order_numbers(win), ["ABC100"])

    def test_column_order_matches_recommendation(self) -> None:
        """推奨列順どおりであること（要件5）。"""
        from app.voucher_window import COLUMN_LABELS

        self.assertEqual(
            COLUMN_LABELS,
            [
                "□",
                "受注No",
                "OLAP",
                "仕上日",
                "AM・PM",
                "加工名",
                "印刷する伝票",
                "指図書編集",
                "PDF作成",
                "プレビュー",
                "印刷",
                "Kintone登録",
            ],
        )


    # ── Kintone登録ボタン（要件1〜3・5）──────────────────────────────────────────
    def test_kintone_column_after_print(self) -> None:
        from app.voucher_window import (
            COLUMN_LABELS,
            COL_KINTONE,
            COL_PRINT,
        )

        self.assertEqual(COL_KINTONE, COL_PRINT + 1)
        self.assertEqual(COLUMN_LABELS[COL_KINTONE], "Kintone登録")

    def test_row_has_kintone_button(self) -> None:
        from app.voucher_window import COL_KINTONE

        win = self._make_window()
        rw = win._rows[0]
        self.assertIsInstance(rw.kintone_button, QPushButton)
        self.assertEqual(rw.kintone_button.text(), "受注No追加")
        widget = win._table.cellWidget(0, COL_KINTONE)
        self.assertIsNotNone(widget)
        self.assertIn(rw.kintone_button, widget.findChildren(QPushButton))
        self.assertEqual(rw.kintone_status_button.text(), "未登録")
        self.assertFalse(rw.kintone_status_button.isEnabled())

    def test_kintone_button_disabled_when_window_not_open(self) -> None:
        """Kintone登録処理画面が未起動なら、全行のKintone登録ボタンは無効。"""
        win = self._make_window()  # provider 未指定＝未起動扱い
        win._on_add_row()
        for rw in win._rows:
            self.assertFalse(rw.kintone_button.isEnabled())

    def test_kintone_button_enabled_when_window_open(self) -> None:
        """Kintone登録処理画面が起動中なら、全行のKintone登録ボタンは有効。"""
        from app.voucher_window import VoucherWindow

        fake_window = mock.Mock()
        win = VoucherWindow(kintone_window_provider=lambda: fake_window)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        for index, rw in enumerate(win._rows):
            self._mark_fetched(win, rw, f"140511{index}")
        # 取得済みの初期行・追加行とも有効。
        for rw in win._rows:
            self.assertTrue(rw.kintone_button.isEnabled())

    def test_refresh_kintone_buttons_syncs_all_rows(self) -> None:
        """画面開閉を模した状態変化が全行のボタンへ同期されること。"""
        from app.voucher_window import VoucherWindow

        state = {"win": None}
        win = VoucherWindow(kintone_window_provider=lambda: state["win"])
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        for index, rw in enumerate(win._rows):
            self._mark_fetched(win, rw, f"140511{index}")
        # 未起動：無効。
        win.refresh_kintone_buttons()
        for rw in win._rows:
            self.assertFalse(rw.kintone_button.isEnabled())
        # 起動：有効。
        state["win"] = mock.Mock()
        win.refresh_kintone_buttons()
        for rw in win._rows:
            self.assertTrue(rw.kintone_button.isEnabled())
        # 再び閉じる：無効へ戻る。
        state["win"] = None
        win.refresh_kintone_buttons()
        for rw in win._rows:
            self.assertFalse(rw.kintone_button.isEnabled())

    def test_kintone_button_adds_order_no_to_window(self) -> None:
        """ボタン押下で対象行の受注Noが add_order_no へ渡されること。"""
        from app.voucher_window import VoucherWindow

        fake_window = mock.Mock()
        win = VoucherWindow(kintone_window_provider=lambda: fake_window)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.kintone_button.click()
        # 受注Noに加えて仕上日／AM・PMも渡されること（要件1・4）。
        fake_window.add_order_no.assert_called_once()
        args, kwargs = fake_window.add_order_no.call_args
        self.assertEqual(args[0], "1405113")
        self.assertIn("finish_date", kwargs)
        self.assertIn("am_pm", kwargs)
        self.assertEqual(kwargs["am_pm"], "AM")

    def test_kintone_button_empty_order_no_warns(self) -> None:
        """空の受注Noでは追加せず警告すること。"""
        from app.voucher_window import VoucherWindow

        fake_window = mock.Mock()
        win = VoucherWindow(kintone_window_provider=lambda: fake_window)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        rw.order_input.setText("")
        self.assertFalse(rw.kintone_button.isEnabled())
        fake_window.add_order_no.assert_not_called()

    def test_kintone_register_noop_when_window_closed(self) -> None:
        """画面が閉じている場合は add_order_no を呼ばず防御的に警告する。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(kintone_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn:
            win._on_kintone_register(rw)
        warn.assert_called_once()

    def test_kintone_provider_exception_is_treated_as_closed(self) -> None:
        """プロバイダ参照で例外が出ても未起動扱いとなり例外を伝播しない。"""
        from app.voucher_window import VoucherWindow

        def boom():
            raise RuntimeError("deleted")

        win = VoucherWindow(kintone_window_provider=boom)
        self.addCleanup(win.deleteLater)
        self.assertFalse(win._is_kintone_window_open())
        for rw in win._rows:
            self.assertFalse(rw.kintone_button.isEnabled())

    # ── 追加済バッヂ・リアルタイム同期（要件2・3・5）─────────────────────────────
    def _make_window_with_orders(self, orders):
        """get_order_numbers を持つ擬似Kintone画面を提供する伝票画面を作る。"""
        from app.voucher_window import VoucherWindow

        state = {"orders": set(orders), "open": True}

        class FakeKintone:
            def get_order_numbers(self_inner):
                return set(state["orders"])

        fake = FakeKintone()

        def provider():
            return fake if state["open"] else None

        win = VoucherWindow(kintone_window_provider=provider)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        return win, state

    def test_button_shows_added_badge_when_order_exists(self) -> None:
        """Kintone画面に同じ受注Noがある場合は「追加済」表示で無効になる。"""
        win, _ = self._make_window_with_orders({"1405113"})
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        self.assertEqual(rw.kintone_button.text(), "追加済")
        self.assertFalse(rw.kintone_button.isEnabled())

    def test_button_enabled_when_order_not_added(self) -> None:
        """起動中かつ未追加なら「受注No追加」表示で有効になる。"""
        win, _ = self._make_window_with_orders({"9999999"})
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        self.assertEqual(rw.kintone_button.text(), "受注No追加")
        self.assertTrue(rw.kintone_button.isEnabled())

    def test_button_realtime_update_on_add_and_remove(self) -> None:
        """Kintone画面の受注No増減で伝票画面のボタン状態がリアルタイム更新される。"""
        win, state = self._make_window_with_orders(set())
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        # 未追加：有効。
        self.assertTrue(rw.kintone_button.isEnabled())
        # 追加された：追加済・無効。
        state["orders"] = {"1405113"}
        win.refresh_kintone_buttons()
        self.assertEqual(rw.kintone_button.text(), "追加済")
        self.assertFalse(rw.kintone_button.isEnabled())
        # 削除された：再び登録可能。
        state["orders"] = set()
        win.refresh_kintone_buttons()
        self.assertEqual(rw.kintone_button.text(), "受注No追加")
        self.assertTrue(rw.kintone_button.isEnabled())

    def test_button_disabled_when_window_closed_even_if_order_known(self) -> None:
        """画面を閉じたら（ケース3）受注Noが一致していても無効・「受注No追加」表示。"""
        win, state = self._make_window_with_orders({"1405113"})
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        self.assertEqual(rw.kintone_button.text(), "追加済")
        state["open"] = False
        win.refresh_kintone_buttons()
        self.assertEqual(rw.kintone_button.text(), "受注No追加")
        self.assertFalse(rw.kintone_button.isEnabled())

    def test_register_marks_row_added_immediately(self) -> None:
        """ボタン押下で追加した後、その行が即「追加済」になる（要件5）。"""
        from app.voucher_window import VoucherWindow

        orders: set[str] = set()

        class FakeKintone:
            def get_order_numbers(self_inner):
                return set(orders)

            def add_order_no(self_inner, order_no, finish_date=None, am_pm=None):
                orders.add(order_no.strip())

        fake = FakeKintone()
        win = VoucherWindow(kintone_window_provider=lambda: fake)
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        self.assertEqual(rw.kintone_button.text(), "受注No追加")
        rw.kintone_button.click()
        self.assertEqual(rw.kintone_button.text(), "追加済")
        self.assertFalse(rw.kintone_button.isEnabled())
        # 受注Noと仕上日／AM・PMが渡されること（要件1・4）。
        self.assertIn("1405113", orders)

    def test_status_filter_default_is_all(self) -> None:
        win = self._make_window()
        self.assertEqual(win._status_filter.currentText(), "すべて")

    def test_registration_completed_updates_same_order_rows_and_default_filter_keeps_them_visible(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "1405113")
        self._mark_fetched(win, rows[1], "1405113")
        self._mark_fetched(win, rows[2], "9999999")

        win.notify_kintone_registration_completed(["1405113"])

        self.assertEqual(
            [rw.kintone_status_button.text() for rw in win._rows if rw.order_input.text().strip() == "1405113"],
            ["登録完了", "登録完了"],
        )
        self.assertEqual(self._row_for_order(win, "9999999").kintone_status_button.text(), "未登録")
        for rw in win._rows:
            self.assertFalse(win._table.isRowHidden(rw.table_row_index))
        win._status_filter.setCurrentText("未登録")
        for rw in win._rows:
            if rw.order_input.text().strip() == "1405113":
                self.assertTrue(win._table.isRowHidden(rw.table_row_index))
            else:
                self.assertFalse(win._table.isRowHidden(rw.table_row_index))

    def test_registration_status_filter_and_order_search_apply_together(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        rows = list(win._rows)
        self._mark_fetched(win, rows[0], "ABC100")
        self._mark_fetched(win, rows[1], "ABC200")
        self._mark_fetched(win, rows[2], "XYZ100")
        win.notify_kintone_registration_completed(["ABC200"])

        win._status_filter.setCurrentText("すべて")
        win._order_search_edit.setText("ABC")
        self.assertFalse(win._table.isRowHidden(self._row_index_for_order(win, "ABC100")))
        self.assertFalse(win._table.isRowHidden(self._row_index_for_order(win, "ABC200")))
        self.assertTrue(win._table.isRowHidden(self._row_index_for_order(win, "XYZ100")))

        win._status_filter.setCurrentText("登録完了")
        self.assertTrue(win._table.isRowHidden(self._row_index_for_order(win, "ABC100")))
        self.assertFalse(win._table.isRowHidden(self._row_index_for_order(win, "ABC200")))
        self.assertTrue(win._table.isRowHidden(self._row_index_for_order(win, "XYZ100")))

        win._order_search_edit.clear()
        self.assertTrue(win._table.isRowHidden(self._row_index_for_order(win, "ABC100")))
        self.assertFalse(win._table.isRowHidden(self._row_index_for_order(win, "ABC200")))
        self.assertTrue(win._table.isRowHidden(self._row_index_for_order(win, "XYZ100")))

    def test_registration_status_button_is_disabled_and_colored(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self.assertEqual(rw.kintone_status_button.text(), "未登録")
        self.assertFalse(rw.kintone_status_button.isEnabled())
        self.assertIn("#c62828", rw.kintone_status_button.styleSheet())
        self._mark_fetched(win, rw, "1405113")
        win.notify_kintone_registration_completed(["1405113"])
        self.assertEqual(rw.kintone_status_button.text(), "登録完了")
        self.assertFalse(rw.kintone_status_button.isEnabled())
        self.assertIn("#2e7d32", rw.kintone_status_button.styleSheet())

    def test_saved_records_are_restored_with_registration_status(self) -> None:
        with _temp_home() as home:
            win = self._make_window()
            rw = win._rows[0]
            rw.order_input.setText("1405113")
            rw.cached_olap = {"pages": [{"order_no": "1405113", "voucher_no": "Z1"}], "raw_rows": []}
            win.notify_kintone_registration_completed(["1405113"])
            path = home / "work" / "voucher_records.json"
            self.assertTrue(path.exists())
            win.deleteLater()

            restored = self._make_window()
            self.assertEqual(restored._rows[0].order_input.text(), "1405113")
            self.assertEqual(restored._rows[0].kintone_status_button.text(), "登録完了")
            self.assertEqual(restored._status_filter.currentText(), "すべて")
            self.assertFalse(restored._table.isRowHidden(0))

    def test_corrupt_saved_records_json_does_not_crash(self) -> None:
        with _temp_home() as home:
            work = home / "work"
            work.mkdir(parents=True)
            (work / "voucher_records.json").write_text("{broken", encoding="utf-8")
            win = self._make_window()
            self.assertEqual(len(win._rows), 1)
            self.assertEqual(win._rows[0].order_input.text(), "")

    def test_expired_saved_records_are_excluded(self) -> None:
        with _temp_home() as home:
            from app.voucher_settings import save_record_retention_days

            save_record_retention_days(10)
            work = home / "work"
            work.mkdir(parents=True)
            old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
            fresh = datetime.now().isoformat(timespec="seconds")
            payload = {
                "version": 1,
                "saved_at": fresh,
                "records": [
                    {"saved_at": old, "order_no": "OLD", "kintone_status": "未登録"},
                    {"saved_at": fresh, "order_no": "FRESH", "kintone_status": "登録完了"},
                ],
            }
            (work / "voucher_records.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            win = self._make_window()
            self.assertEqual([rw.order_input.text() for rw in win._rows], ["FRESH"])
            self.assertEqual(win._rows[0].kintone_status_button.text(), "登録完了")

    def test_register_passes_finish_date_and_am_pm(self) -> None:
        """ボタン押下で仕上日／AM・PMが add_order_no へ渡されること（要件1・4）。"""
        from datetime import date

        from app.voucher_window import VoucherWindow

        captured = {}

        class FakeKintone:
            def get_order_numbers(self_inner):
                return set()

            def add_order_no(self_inner, order_no, finish_date=None, am_pm=None):
                captured["order_no"] = order_no
                captured["finish_date"] = finish_date
                captured["am_pm"] = am_pm

        from PySide6.QtCore import QDate

        win = VoucherWindow(kintone_window_provider=lambda: FakeKintone())
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.date_edit.setDate(QDate(2026, 6, 26))
        rw.ampm_pm.setChecked(True)
        rw.kintone_button.click()
        self.assertEqual(captured["order_no"], "1405113")
        self.assertEqual(captured["finish_date"], date(2026, 6, 26))
        self.assertEqual(captured["am_pm"], "PM")

    def test_register_passes_none_for_finish_and_ampm(self) -> None:
        """仕上日「なし」／AM・PM「なし」も override として渡されること（要件4）。"""
        from app.voucher_window import VoucherWindow

        captured = {}

        class FakeKintone:
            def get_order_numbers(self_inner):
                return set()

            def add_order_no(self_inner, order_no, finish_date=None, am_pm=None):
                captured["finish_date"] = finish_date
                captured["am_pm"] = am_pm

        win = VoucherWindow(kintone_window_provider=lambda: FakeKintone())
        self.addCleanup(win.deleteLater)
        win._on_add_row()
        self._remove_input_row_for_legacy_tests(win)
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        rw.finish_none_check.setChecked(True)
        rw.ampm_none.setChecked(True)
        rw.kintone_button.click()
        self.assertIsNone(captured["finish_date"])
        self.assertEqual(captured["am_pm"], "none")

    # ── 追加済ボタンの緑色スタイル（要件6）─────────────────────────────────────
    def test_added_button_has_green_style(self) -> None:
        """「追加済」ボタンが緑色スタイルになること（要件6）。"""
        from app.voucher_window import KINTONE_ADDED_BUTTON_STYLE

        win, _ = self._make_window_with_orders({"1405113"})
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        self.assertEqual(rw.kintone_button.text(), "追加済")
        style = rw.kintone_button.styleSheet()
        self.assertIn("#2e7d32", style)
        self.assertEqual(style, KINTONE_ADDED_BUTTON_STYLE)

    def test_added_button_style_differs_from_closed_disabled(self) -> None:
        """未起動の無効ボタンと追加済ボタンの見た目が区別できること（要件6）。"""
        win, state = self._make_window_with_orders({"1405113"})
        rw = win._rows[0]
        self._mark_fetched(win, rw, "1405113")
        added_style = rw.kintone_button.styleSheet()
        # 画面を閉じる→通常の無効ボタン（緑スタイルなし）。
        state["open"] = False
        win.refresh_kintone_buttons()
        closed_style = rw.kintone_button.styleSheet()
        self.assertNotEqual(added_style, closed_style)
        self.assertNotIn("#2e7d32", closed_style)

    def test_print_queue_runs_jobs_sequentially(self) -> None:
        from app import voucher_print_service

        manager = voucher_print_service._PRINT_QUEUE_MANAGER
        manager.running_job = None
        manager.running_proxy = None
        manager.current_worker = None
        manager.current_thread = None
        manager.queued_jobs.clear()
        self.addCleanup(manager.queued_jobs.clear)
        self.addCleanup(setattr, manager, "running_job", None)
        self.addCleanup(setattr, manager, "running_proxy", None)
        self.addCleanup(setattr, manager, "current_worker", None)
        self.addCleanup(setattr, manager, "current_thread", None)

        workers = [_FakePrintWorker(), _FakePrintWorker()]
        with mock.patch.object(
            voucher_print_service, "_start_print_pdf_worker", side_effect=workers
        ) as start_worker:
            first = voucher_print_service.start_print_pdf_background(
                b"%PDF-1", job_name="1111111", source_type="row"
            )
            second = voucher_print_service.start_print_pdf_background(
                b"%PDF-2", job_name="2222222", source_type="row"
            )
            self.app.processEvents()
            self.app.processEvents()

            self.assertEqual(start_worker.call_count, 1)
            self.assertEqual(manager.running_job.order_no, "1111111")
            self.assertEqual(len(manager.queued_jobs), 1)

            finished: list[str] = []
            first.finished.connect(lambda payload: finished.append(str(payload["print_job_id"])))
            second.finished.connect(lambda payload: finished.append(str(payload["print_job_id"])))
            workers[0].finished.emit({"print_job_id": first.job.job_id})
            self.app.processEvents()

            self.assertEqual(start_worker.call_count, 2)
            self.assertEqual(manager.running_job.order_no, "2222222")
            workers[1].finished.emit({"print_job_id": second.job.job_id})
            self.app.processEvents()

        self.assertIsNone(manager.running_job)
        self.assertEqual(manager.queued_jobs, [])
        self.assertEqual(finished, [first.job.job_id, second.job.job_id])

    def test_print_worker_dispatches_sumatra_off_ui_thread(self) -> None:
        from app import voucher_print_service
        from app.voucher_settings import VoucherPrinterSettings

        settings = VoucherPrinterSettings(printer_name="Printer A", print_backend="sumatra")
        events: list[dict[str, object]] = []
        finished: list[dict[str, object]] = []

        def fake_sumatra(_pdf_bytes, _settings, *, print_metadata, request_sent_callback, **_kwargs):
            request_sent_callback({"popen_pid": 1234})

        with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                mock.patch.object(voucher_print_service, "log_print_recovery_event", side_effect=lambda event_type, **fields: events.append({"event_type": event_type, **fields})), \
                mock.patch.object(voucher_print_service, "_print_pdf_with_sumatra", side_effect=fake_sumatra) as sumatra:
            worker = voucher_print_service._start_print_pdf_worker(
                b"%PDF",
                job_name="1111111",
                queued_job_id="job-1",
                queued_source_type="row",
                ui_thread_id=threading.get_ident(),
                queued_print_backend="sumatra",
                backend_default_source="default_sumatra",
            )
            worker.finished.connect(lambda payload: finished.append(payload))
            thread = worker.thread
            deadline = time.monotonic() + 2
            while not finished and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            deadline = time.monotonic() + 2
            while thread is not None and thread.isRunning() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertTrue(finished)
        event_types = [event["event_type"] for event in events]
        self.assertIn("print_queue_worker_thread_started", event_types)
        self.assertIn("print_job_backend_dispatch_started", event_types)
        self.assertIn("sumatra_backend_selected", event_types)
        dispatch = next(event for event in events if event["event_type"] == "print_job_backend_dispatch_started")
        self.assertNotEqual(dispatch["worker_thread_id"], dispatch["ui_thread_id"])
        self.assertFalse(dispatch["current_thread_is_ui_thread"])
        sumatra.assert_called_once()

    def test_print_queue_unknown_backend_fails_and_unblocks_queue(self) -> None:
        from app import voucher_print_service

        manager = voucher_print_service._PRINT_QUEUE_MANAGER
        manager.running_job = None
        manager.running_proxy = None
        manager.current_worker = None
        manager.current_thread = None
        manager.queued_jobs.clear()
        self.addCleanup(manager.queued_jobs.clear)
        self.addCleanup(setattr, manager, "running_job", None)
        self.addCleanup(setattr, manager, "running_proxy", None)
        self.addCleanup(setattr, manager, "current_worker", None)
        self.addCleanup(setattr, manager, "current_thread", None)
        job = voucher_print_service.PrintJob(
            job_id="job-unknown",
            source_type="row",
            order_no="1111111",
            pdf_bytes=b"%PDF",
            print_backend="unknown",
        )
        proxy = voucher_print_service.PrintJobProxy(job)
        errors: list[str] = []
        proxy.error.connect(lambda message, _payload: errors.append(message))
        events: list[str] = []
        with mock.patch.object(voucher_print_service, "log_print_recovery_event", side_effect=lambda event_type, **_fields: events.append(event_type)):
            manager.enqueue(job, proxy)
            self.app.processEvents()

        self.assertTrue(errors)
        self.assertIsNone(manager.running_job)
        self.assertEqual(manager.queued_jobs, [])
        self.assertIn("print_backend_unknown", events)
        self.assertIn("print_job_failed", events)
        self.assertIn("print_queue_worker_error", events)
        self.assertIn("print_queue_empty", events)

    def test_print_queue_invalid_pdf_or_printer_fails_and_unblocks_queue(self) -> None:
        from app import voucher_print_service
        from app.voucher_settings import VoucherPrinterSettings

        manager = voucher_print_service._PRINT_QUEUE_MANAGER
        self.addCleanup(manager.queued_jobs.clear)
        self.addCleanup(setattr, manager, "running_job", None)
        self.addCleanup(setattr, manager, "running_proxy", None)
        self.addCleanup(setattr, manager, "current_worker", None)
        self.addCleanup(setattr, manager, "current_thread", None)

        cases = [
            (
                voucher_print_service.PrintJob(
                    job_id="job-empty-pdf",
                    source_type="row",
                    order_no="1111111",
                    pdf_bytes=b"",
                    print_backend="sumatra",
                ),
                VoucherPrinterSettings(printer_name="Printer A", print_backend="sumatra"),
            ),
            (
                voucher_print_service.PrintJob(
                    job_id="job-missing-pdf",
                    source_type="selected",
                    order_no="",
                    pdf_bytes=b"%PDF",
                    print_backend="sumatra",
                    merged_pdf_path="/no/such/job.pdf",
                ),
                VoucherPrinterSettings(printer_name="Printer A", print_backend="sumatra"),
            ),
            (
                voucher_print_service.PrintJob(
                    job_id="job-empty-printer",
                    source_type="row",
                    order_no="1111111",
                    pdf_bytes=b"%PDF",
                    print_backend="sumatra",
                ),
                VoucherPrinterSettings(printer_name="", print_backend="sumatra"),
            ),
        ]
        for job, settings in cases:
            with self.subTest(job_id=job.job_id):
                manager.running_job = None
                manager.running_proxy = None
                manager.current_worker = None
                manager.current_thread = None
                manager.queued_jobs.clear()
                proxy = voucher_print_service.PrintJobProxy(job)
                errors: list[str] = []
                proxy.error.connect(lambda message, _payload: errors.append(message))
                events: list[str] = []
                with mock.patch("app.voucher_settings.load_voucher_printer_settings", return_value=settings), \
                        mock.patch.object(voucher_print_service, "log_print_recovery_event", side_effect=lambda event_type, **_fields: events.append(event_type)):
                    manager.enqueue(job, proxy)
                    deadline = time.monotonic() + 2
                    while not errors and manager.running_job is not None and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(0.01)
                    self.app.processEvents()
                self.assertTrue(errors)
                self.assertIsNone(manager.running_job)
                self.assertFalse(manager.queued_jobs)
                self.assertIn("print_job_failed", events)
                self.assertIn("print_queue_empty", events)

    def test_selected_print_enqueues_single_merged_pdf_job(self) -> None:
        win = self._make_window()
        win._on_add_row()
        for order_no, rw in zip(("1111111", "2222222"), win._rows):
            self._mark_fetched(win, rw, order_no)
            rw.select_check.setChecked(True)
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", side_effect=[b"%PDF-1", b"%PDF-2"]), \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF-MERGED") as merge, \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as enqueue:
            win._on_select_print()
        self.assertEqual(build.call_count, 2)
        merge.assert_called_once_with([b"%PDF-1", b"%PDF-2"])
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[0], b"%PDF-MERGED")
        self.assertEqual(enqueue.call_args.kwargs["selected_count"], 2)
        self.assertEqual(enqueue.call_args.kwargs["source_type"], "selected")

    def test_sumatra_test_print_generates_pdf_when_none_exists(self) -> None:
        """テスト印刷用PDFが無くても設定確認用PDFを自動生成して印刷キューへ入れる。"""
        win = self._make_window()
        # 既存PDF（伝票PDF）が無い状態を明示する。
        win._last_pdf_bytes = b""
        win._last_pdf_path = ""
        worker = _FakePrintWorker()
        with mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as enqueue:
            ok = win._enqueue_sumatra_test_print()
        self.assertTrue(ok)
        enqueue.assert_called_once()
        # 「テスト印刷用PDFがありません」で終了せず、生成されたPDFが投入される。
        self.assertTrue(enqueue.call_args.args[0].startswith(b"%PDF"))
        self.assertEqual(enqueue.call_args.kwargs["source_type"], "test")
        self.assertTrue(enqueue.call_args.kwargs["test_print_requested"])

    def test_sumatra_test_print_honors_override_paper_size(self) -> None:
        """settings_override の用紙サイズ/向きが生成PDFに反映される。"""
        import io as _io
        import pypdf
        from app.voucher_settings import VoucherPrinterSettings

        win = self._make_window()
        win._last_pdf_bytes = b""
        win._last_pdf_path = ""
        worker = _FakePrintWorker()
        override = VoucherPrinterSettings(
            printer_name="P", paper_size="A4", orientation="portrait"
        )
        with mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker) as enqueue:
            ok = win._enqueue_sumatra_test_print(settings_override=override)
        self.assertTrue(ok)
        # settings_override が印刷サービスまで渡る。
        self.assertIs(enqueue.call_args.kwargs["settings_override"], override)
        pdf_bytes = enqueue.call_args.args[0]
        reader = pypdf.PdfReader(_io.BytesIO(pdf_bytes))
        box = reader.pages[0].mediabox
        width_mm = round(float(box.width) / 72 * 25.4)
        height_mm = round(float(box.height) / 72 * 25.4)
        # A4縦 = 210 x 297mm。
        self.assertEqual((width_mm, height_mm), (210, 297))

    def test_sumatra_test_print_reports_failure_when_pdf_generation_fails(self) -> None:
        """PDF生成失敗時はエラー表示とログを出し、印刷キューへ入れない。"""
        win = self._make_window()
        events: list[str] = []
        with mock.patch(
            "app.voucher_service.build_test_print_pdf_bytes",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "app.voucher_print_service.start_print_pdf_background"
        ) as enqueue, mock.patch(
            "app.voucher_print_service.log_print_settings_event",
            side_effect=lambda name, **kw: events.append(name),
        ):
            ok = win._enqueue_sumatra_test_print()
        self.assertFalse(ok)
        enqueue.assert_not_called()
        self.assertIn("voucher_print_settings_test_print_pdf_auto_create_failed", events)
        self.assertIn("voucher_print_settings_test_print_failed", events)

    def test_print_does_not_disable_whole_table(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        worker = _FakePrintWorker()
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_print_service.start_print_pdf_background", return_value=worker):
            rw.print_button.click()
        self.assertTrue(win.isEnabled())
        self.assertTrue(win._table.isEnabled())
        self.assertTrue(rw.print_button.isEnabled())

    def test_restore_print_ui_state_recovers_window_table_and_buttons(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        win._snapshot_print_ui_state()
        win._print_in_progress = True
        win._table.setEnabled(False)
        rw.print_button.setEnabled(False)
        win._restore_print_ui_state("unit", "印刷処理完了")
        self.assertFalse(win._print_in_progress)
        self.assertTrue(win.isEnabled())
        self.assertTrue(win._table.isEnabled())
        self.assertTrue(rw.print_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
