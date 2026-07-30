"""TKS受注No取込機能のテスト。

- 機能選択画面のボタン表示制御（デバッグ表示ON/OFF・位置）
- TksOrderCaptureWindow の起動・単一インスタンス・常に手前
- 受注Noの保存（空欄不可・重複不可・破損ファイル耐性）
- 伝票一覧への追加（伝票画面の有無で有効/無効、OLAP取得呼び出し）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt, QSettings
    from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover
    PYSIDE_AVAILABLE = False


class FakeHelperProcess:
    """subprocess.Popen の代役。stdout に payload を返す短命helperを模す。"""

    payload = b"{}"
    returncode_value = 0

    def __init__(self, argv, stdout=None, stderr=None, creationflags=0):
        self.argv = argv
        self.returncode = None
        self.killed = False

    def communicate(self, timeout=None):
        self.returncode = type(self).returncode_value
        return (type(self).payload, b"")

    def kill(self):
        self.killed = True


class TimeoutHelperProcess:
    """communicate() で最初に TimeoutExpired を投げ、kill 後は空で返す代役。"""

    killed = False

    def __init__(self, argv, stdout=None, stderr=None, creationflags=0):
        type(self).killed = False
        self.returncode = None

    def communicate(self, timeout=None):
        import subprocess

        if not type(self).killed:
            raise subprocess.TimeoutExpired(cmd="dummy", timeout=timeout)
        return (b"", b"")

    def kill(self):
        type(self).killed = True


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CapturedOrdersStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def test_save_order_no(self) -> None:
        from app import captured_orders

        saved, reason = captured_orders.add_captured_order("1405773")
        self.assertTrue(saved)
        self.assertEqual(reason, "saved")
        orders = captured_orders.load_captured_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_no"], "1405773")
        self.assertFalse(orders[0]["added_to_voucher"])

    def test_empty_order_no_not_saved(self) -> None:
        from app import captured_orders

        for value in ("", "   ", "\t"):
            saved, reason = captured_orders.add_captured_order(value)
            self.assertFalse(saved)
            self.assertEqual(reason, "empty")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_duplicate_order_no_not_saved(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        saved, reason = captured_orders.add_captured_order("1405773")
        self.assertFalse(saved)
        self.assertEqual(reason, "duplicate")
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)

    def test_remove_captured_orders_by_order_no(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1111111")
        captured_orders.add_captured_order("2222222")
        captured_orders.add_captured_order("3333333")
        removed = captured_orders.remove_captured_orders_by_order_no({"２２２２２２２", "3333333"})
        self.assertEqual(removed, 2)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1111111"],
        )

    def test_corrupted_file_does_not_crash(self) -> None:
        from app import captured_orders

        path = captured_orders.get_captured_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not valid json ]", encoding="utf-8")
        # 破損していても例外を投げず空リスト扱い。
        self.assertEqual(captured_orders.load_captured_orders(), [])
        # 続けて保存できる（破損ファイルを上書き）。
        saved, _ = captured_orders.add_captured_order("1409999")
        self.assertTrue(saved)
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class LauncherCaptureButtonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def _make_launcher(self):
        from app.launcher_window import LauncherWindow

        win = LauncherWindow()
        self.addCleanup(win.deleteLater)
        return win

    def _set_debug(self, win, visible: bool) -> None:
        win._settings.setValue("ui/debug_visible", "1" if visible else "0")
        win._settings.sync()
        win._apply_debug_visibility()

    def test_button_visible_when_debug_on(self) -> None:
        win = self._make_launcher()
        self._set_debug(win, True)
        self.assertTrue(win._tks_capture_btn.isVisible() or not win.isVisible())
        # isVisible はウィンドウ非表示時 False になるため、visibility プロパティで確認する。
        self.assertFalse(win._tks_capture_btn.isHidden())

    def test_button_visible_when_debug_off(self) -> None:
        # 受注No取込ボタンはデバッグ表示OFFでも常に表示する（要件5）。
        win = self._make_launcher()
        self._set_debug(win, False)
        self.assertFalse(win._tks_capture_btn.isHidden())
        # 他のデバッグ専用フォルダボタンは従来どおり非表示のまま（既存制御が壊れていない）。
        self.assertTrue(win._open_work_btn.isHidden())

    def test_button_left_of_settings_button(self) -> None:
        # TKS取込ボタンは設定（歯車）ボタンと同じ行にあり、その左側に配置される（要件2）。
        win = self._make_launcher()
        capture_idx = self._index_in_layout(win, win._tks_capture_btn)
        settings_idx = self._index_in_layout(win, win._settings_btn)
        self.assertIsNotNone(capture_idx)
        self.assertIsNotNone(settings_idx)
        self.assertLess(capture_idx, settings_idx)

    def test_settings_button_action_unchanged(self) -> None:
        # 設定ボタンの動作（設定画面を開く）は変わらない（要件2）。
        win = self._make_launcher()
        with mock.patch.object(win, "_open_settings") as open_settings:
            win._settings_btn.click()
        open_settings.assert_called_once()

    @staticmethod
    def _index_in_layout(win, target):
        # 中央ウィジェット配下の全レイアウトを再帰探索し、target を持つレイアウト内の
        # インデックスを返す（TKS取込と設定ボタンが同一レイアウトにある前提）。
        from PySide6.QtWidgets import QLayout

        def walk(layout):
            found = {}
            for i in range(layout.count()):
                item = layout.itemAt(i)
                w = item.widget()
                if w is not None:
                    found[w] = i
                child = item.layout()
                if isinstance(child, QLayout):
                    sub = walk(child)
                    if win._tks_capture_btn in sub and win._settings_btn in sub:
                        return sub
            return found

        result = walk(win.centralWidget().layout())
        return result.get(target)

    def test_open_capture_window(self) -> None:
        win = self._make_launcher()
        self.assertIsNone(win._capture_window)
        win._open_tks_capture()
        self.addCleanup(lambda: win._capture_window and win._capture_window.close())
        self.assertIsNotNone(win._capture_window)

    def test_open_capture_window_single_instance(self) -> None:
        win = self._make_launcher()
        win._open_tks_capture()
        first = win._capture_window
        self.addCleanup(lambda: first and first.close())
        win._open_tks_capture()
        self.assertIs(win._capture_window, first)

    def test_capture_window_is_top_level(self) -> None:
        # 取込画面は parent=None のトップレベルウィンドウとして生成される（要件1）。
        win = self._make_launcher()
        win._open_tks_capture()
        capture = win._capture_window
        self.addCleanup(lambda: capture and capture.close())
        self.assertIsNone(capture.parent())
        self.assertTrue(capture.isWindow())

    def test_open_capture_does_not_activate_launcher(self) -> None:
        # 取込画面を開いても機能選択画面（親）は前面化しない（要件2）。
        win = self._make_launcher()
        with mock.patch.object(win, "activateWindow") as launcher_activate, \
            mock.patch.object(win, "raise_") as launcher_raise:
            win._open_tks_capture()
        capture = win._capture_window
        self.addCleanup(lambda: capture and capture.close())
        launcher_activate.assert_not_called()
        launcher_raise.assert_not_called()

    def test_reopen_activates_only_capture_window(self) -> None:
        # 既存の取込画面を再表示すると、取込画面だけが前面化される（要件2・3）。
        win = self._make_launcher()
        win._open_tks_capture()
        capture = win._capture_window
        self.addCleanup(lambda: capture and capture.close())
        with mock.patch.object(win, "activateWindow") as launcher_activate, \
            mock.patch.object(win, "raise_") as launcher_raise, \
            mock.patch.object(capture, "raise_") as cap_raise, \
            mock.patch.object(capture, "activateWindow") as cap_activate:
            win._open_tks_capture()
        launcher_activate.assert_not_called()
        launcher_raise.assert_not_called()
        cap_raise.assert_called_once()
        cap_activate.assert_called_once()

    def test_closing_capture_does_not_close_launcher(self) -> None:
        # 取込画面を閉じても機能選択画面は閉じない（要件4）。
        win = self._make_launcher()
        win._open_tks_capture()
        capture = win._capture_window
        capture.close()
        self.assertIsNone(win._capture_window)
        self.assertFalse(win._closing)

    def test_capture_window_stays_on_top(self) -> None:
        win = self._make_launcher()
        win._open_tks_capture()
        capture = win._capture_window
        self.addCleanup(lambda: capture and capture.close())
        self.assertTrue(bool(capture.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_capture_button_does_not_break_voucher_kintone_enable(self) -> None:
        # 既存の伝票・Kintoneボタンの有効/無効制御が壊れていないこと。
        win = self._make_launcher()
        self.assertFalse(win._voucher_btn.isEnabled())
        self.assertFalse(win._kintone_btn.isEnabled())
        win._olap_id.setText("id")
        win._olap_password.setText("pw")
        self.assertTrue(win._voucher_btn.isEnabled())


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CaptureWindowBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        # ウィンドウ内の QSettings(org, app) はネイティブ形式（実ユーザ設定）を読むため、
        # 自動保存などの設定が前のテスト/実アプリから漏れないよう明示的にクリアする。
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self, voucher_window=None):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: voucher_window)
        self.addCleanup(win.deleteLater)
        return win

    def test_save_via_manual_input(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1405773")
        win._on_save()
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)
        self.assertEqual(win._count_label.text(), "1 件")

    def test_empty_not_saved_via_window(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("   ")
        win._on_save()
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_manual_input_save_when_auto_capture_and_save_off(self) -> None:
        # 自動取得OFF/自動保存OFFでも入力欄テキストを手動保存できる（要件1）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(False)
        win._order_input.setText("1405773")
        win._save_manual_input_order_no()
        saved = captured_orders.load_captured_orders()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["order_no"], "1405773")
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_manual_input_save_uses_input_text_not_internal_values(self) -> None:
        # 保存対象は入力欄テキスト。内部候補値が別にあっても入力欄を保存する（要件1/2）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._last_valid_order_no = "9999999"
        win._latest_detected_order_no = "8888888"
        win._last_header_order_no = "7777777"
        win._set_latest_order_no("6666666")
        win._order_input.setText("1405773")
        win._save_manual_input_order_no()
        saved = [e["order_no"] for e in captured_orders.load_captured_orders()]
        self.assertEqual(saved, ["1405773"])

    def test_manual_input_save_normalizes_fullwidth_digits(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("１４０５７７３")
        win._save_manual_input_order_no()
        saved = [e["order_no"] for e in captured_orders.load_captured_orders()]
        self.assertEqual(saved, ["1405773"])

    def test_manual_input_save_rejects_empty(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("")
        self.assertEqual(win._save_manual_input_order_no(), "invalid")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_manual_input_save_rejects_short(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("12345")
        self.assertEqual(win._save_manual_input_order_no(), "invalid")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_manual_input_save_rejects_alpha(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("14057A73")
        self.assertEqual(win._save_manual_input_order_no(), "invalid")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_manual_input_save_rejects_duplicate(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("1405773")
        win._save_manual_input_order_no()
        result = win._save_manual_input_order_no()
        self.assertEqual(result, "duplicate")
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)

    def test_manual_input_save_does_not_start_helper_or_process(self) -> None:
        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(False)
        win._order_input.setText("1405773")
        with mock.patch.object(win, "_start_capture_worker_once") as starter, \
                mock.patch(
                    "app.tks_order_capture_window.run_capture_via_helper"
                ) as helper:
            win._save_manual_input_order_no()
        starter.assert_not_called()
        helper.assert_not_called()

    def test_enter_key_saves_manual_input(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._order_input.setText("1405773")
        win._order_input.returnPressed.emit()
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)

    def test_add_to_voucher_disabled_without_voucher_window(self) -> None:
        win = self._make_capture(voucher_window=None)
        win._order_input.setText("1405773")
        win._refresh_add_to_voucher_enabled()
        self.assertFalse(win._add_to_voucher_button.isEnabled())

    def test_add_to_voucher_warns_without_voucher_window(self) -> None:
        win = self._make_capture(voucher_window=None)
        win._set_latest_order_no("1405773")
        with mock.patch(
            "app.tks_order_capture_window.QMessageBox.information"
        ) as info:
            win._on_add_to_voucher()
        info.assert_called_once()

    def test_add_to_voucher_calls_window_method(self) -> None:
        fake = mock.Mock()
        fake.add_order_no_and_fetch.return_value = {"status": "added", "order_no": "1405773"}
        win = self._make_capture(voucher_window=fake)
        win._set_latest_order_no("1405773")
        win._refresh_add_to_voucher_enabled()
        self.assertTrue(win._add_to_voucher_button.isEnabled())
        win._on_add_to_voucher()
        fake.add_order_no_and_fetch.assert_called_once_with("1405773")

    def test_add_to_voucher_processes_all_saved_orders_and_removes_successes(self) -> None:
        from app import captured_orders

        for order_no in ("1111111", "2222222", "3333333"):
            captured_orders.add_captured_order(order_no)
        fake = mock.Mock()
        fake.add_order_no_and_fetch.side_effect = [
            {"status": "added", "order_no": "1111111"},
            {"status": "duplicate", "order_no": "2222222"},
            {"status": "error", "order_no": "3333333"},
        ]
        win = self._make_capture(voucher_window=fake)
        win._set_latest_order_no("9999999")
        win._on_add_to_voucher()
        self.assertEqual(
            [call.args[0] for call in fake.add_order_no_and_fetch.call_args_list],
            ["1111111", "2222222", "3333333"],
        )
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["3333333"],
        )
        self.assertIn("追加完了: 1件", win._status_label.toolTip())
        self.assertIn("重複: 1件", win._status_label.toolTip())
        self.assertIn("削除: 2件", win._status_label.toolTip())

    def test_add_to_voucher_refreshes_open_saved_list_after_removal(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1111111")
        fake = mock.Mock()
        fake.add_order_no_and_fetch.return_value = {"status": "added"}
        win = self._make_capture(voucher_window=fake)
        win._on_open_list()
        self.assertEqual(win._list_window._table.rowCount(), 1)
        win._on_add_to_voucher()
        self.assertEqual(win._list_window._table.rowCount(), 0)
        self.assertEqual(win._count_label.text(), "0 件")

    def test_add_to_voucher_does_not_remove_order_saved_during_add(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1111111")
        captured_orders.add_captured_order("2222222")

        def add_and_save_new(order_no):
            if order_no == "1111111":
                captured_orders.add_captured_order("3333333")
            return {"status": "added"}

        fake = mock.Mock()
        fake.add_order_no_and_fetch.side_effect = add_and_save_new
        win = self._make_capture(voucher_window=fake)
        win._on_add_to_voucher()
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["3333333"],
        )

    def test_window_has_no_maximize_button_flag(self) -> None:
        win = self._make_capture()
        flags = win.windowFlags()
        self.assertTrue(flags & Qt.WindowType.Window)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.WindowCloseButtonHint)
        # 最小化・最大化は無効化する（要件3/4）。
        self.assertFalse(bool(flags & Qt.WindowType.WindowMinimizeButtonHint))
        self.assertFalse(bool(flags & Qt.WindowType.WindowMaximizeButtonHint))

    def test_window_is_fixed_size(self) -> None:
        # サイズ変更を無効化（minimumSize == maximumSize・要件4）。
        win = self._make_capture()
        self.assertEqual(win.minimumSize(), win.maximumSize())
        self.assertGreater(win.minimumSize().width(), 0)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class VoucherAddOrderNoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_window(self):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        # 保存済み一覧の遅延（チャンク）復元を同期的に確定させてから返す（要件1）。
        win._ensure_saved_rows_restored()
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def _olap_data(order_no: str) -> dict:
        return {
            "order_no": order_no,
            "pages": [{"order_no": order_no}],
            "raw_rows": [{"order_no": order_no}],
        }

    def test_add_order_no_and_fetch_adds_row_and_calls_olap(self) -> None:
        win = self._make_window()
        with mock.patch.object(
            win, "_build_print_data", return_value=self._olap_data("1405773")
        ) as build, mock.patch.object(win, "_cache_row_olap"), mock.patch.object(
            win, "_attach_row_settings"
        ):
            result = win.add_order_no_and_fetch("1405773")
        self.assertEqual(result["status"], "added")
        build.assert_called_once()
        self.assertIn("1405773", [rw.order_input.text().strip() for rw in win._rows])

    def test_add_order_no_and_fetch_invalid(self) -> None:
        win = self._make_window()
        result = win.add_order_no_and_fetch("   ")
        self.assertEqual(result["status"], "invalid")

    def test_add_order_no_and_fetch_duplicate(self) -> None:
        win = self._make_window()
        with mock.patch.object(
            win, "_build_print_data", return_value=self._olap_data("1405773")
        ), mock.patch.object(win, "_cache_row_olap"), mock.patch.object(
            win, "_attach_row_settings"
        ):
            win.add_order_no_and_fetch("1405773")
            result = win.add_order_no_and_fetch("1405773")
        self.assertEqual(result["status"], "duplicate")
        count = sum(1 for rw in win._rows if rw.order_input.text().strip() == "1405773")
        self.assertEqual(count, 1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class NormalizeOrderNoTest(unittest.TestCase):
    def test_full_width_digits_converted(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertEqual(normalize_captured_order_no("１４０９９９９"), "1409999")

    def test_strips_surrounding_whitespace(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertEqual(normalize_captured_order_no("  1409999 \n"), "1409999")

    def test_empty_returns_none(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertIsNone(normalize_captured_order_no(""))
        self.assertIsNone(normalize_captured_order_no("   "))
        self.assertIsNone(normalize_captured_order_no(None))

    def test_non_digit_returns_none(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertIsNone(normalize_captured_order_no("ABC123"))
        self.assertIsNone(normalize_captured_order_no("1409-999"))

    def test_six_digits_or_less_returns_none(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertIsNone(normalize_captured_order_no("010"))
        self.assertIsNone(normalize_captured_order_no("123456"))

    def test_seven_and_eight_digits_are_valid(self) -> None:
        from app.captured_orders import normalize_captured_order_no

        self.assertEqual(normalize_captured_order_no("1234567"), "1234567")
        self.assertEqual(normalize_captured_order_no("12345678"), "12345678")


class CaptureFunctionTest(unittest.TestCase):
    def test_returns_none_when_window_not_found(self) -> None:
        # Windows 以外、または対象ウィンドウ未検出でも None で落ちない。
        from app.tks_cloud_capture import read_order_no_from_tkscloud8

        self.assertIsNone(read_order_no_from_tkscloud8())

    def test_capture_function_normalizes(self) -> None:
        from app import tks_order_capture_window as mod

        with mock.patch(
            "app.tks_cloud_capture.read_order_no_from_tkscloud8", return_value="１４０９９９９"
        ):
            self.assertEqual(mod.capture_order_no_from_tkscloud8(), "1409999")

    def test_capture_function_handles_exception(self) -> None:
        from app import tks_order_capture_window as mod

        with mock.patch(
            "app.tks_cloud_capture.read_order_no_from_tkscloud8",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(mod.capture_order_no_from_tkscloud8())

    def test_read_order_no_uses_fast_path_before_full_scan(self) -> None:
        from app import tks_cloud_capture as mod

        with mock.patch.object(mod, "_read_cached_order_no", return_value="1409999"), \
            mock.patch.object(mod, "capture_order_no") as full_scan:
            self.assertEqual(mod.read_order_no_from_tkscloud8(), "1409999")
        full_scan.assert_not_called()

    def test_read_order_no_falls_back_to_full_scan_when_fast_path_fails(self) -> None:
        from app import tks_cloud_capture as mod

        result = mod.CaptureResult("1400000", mod.REASON_OK, window_found=True)
        with mock.patch.object(mod, "_read_cached_order_no", return_value=None), \
            mock.patch.object(mod, "capture_order_no", return_value=result) as full_scan:
            self.assertEqual(mod.read_order_no_from_tkscloud8(), "1400000")
        full_scan.assert_called_once()

    def test_read_order_no_attempt_fast_path_success_skips_full_scan(self) -> None:
        from app import tks_cloud_capture as mod

        with mock.patch.object(
            mod,
            "_read_cached_order_no_attempt",
            return_value=mod.OrderReadAttempt("1409999", used_fast_path=True, reason="ok"),
        ), mock.patch.object(mod, "capture_order_no") as full_scan:
            attempt = mod.read_order_no_attempt_from_tkscloud8()
        self.assertEqual(attempt.value, "1409999")
        self.assertTrue(attempt.used_fast_path)
        full_scan.assert_not_called()

    def test_read_order_no_attempt_fast_path_failure_uses_full_scan(self) -> None:
        from app import tks_cloud_capture as mod

        result = mod.CaptureResult("1400000", mod.REASON_OK, window_found=True)
        with mock.patch.object(
            mod,
            "_read_cached_order_no_attempt",
            return_value=mod.OrderReadAttempt(
                None, fast_path_failed=True, cache_cleared=True, reason="window_changed"
            ),
        ), mock.patch.object(mod, "capture_order_no", return_value=result) as full_scan:
            attempt = mod.read_order_no_attempt_from_tkscloud8()
        self.assertEqual(attempt.value, "1400000")
        self.assertTrue(attempt.full_scan_used)
        self.assertTrue(attempt.fast_path_failed)
        self.assertTrue(attempt.cache_cleared)
        full_scan.assert_called_once()

    def test_pick_order_no_prefers_label_neighbour(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "Static", "text": "得意先", "rect": (10, 10, 80, 30)},
            {"class": "Edit", "text": "001", "rect": (90, 10, 200, 30)},
            {"class": "Static", "text": "受注No", "rect": (10, 50, 80, 70)},
            {"class": "Edit", "text": "1409999", "rect": (90, 50, 200, 70)},
        ]
        self.assertEqual(_pick_order_no_value(controls), "1409999")

    def test_pick_order_no_without_label_is_not_selected(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "Edit", "text": "1409999", "rect": (90, 50, 200, 70)},
        ]
        self.assertIsNone(_pick_order_no_value(controls))

    def test_pick_order_no_none_when_no_edit(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [{"class": "Static", "text": "受注No", "rect": (10, 50, 80, 70)}]
        self.assertIsNone(_pick_order_no_value(controls))


class OrderNoSelectionTest(unittest.TestCase):
    """受注No欄の特定ロジック（プラットフォーム非依存）の追加テスト。"""

    def test_selects_value_right_of_order_no_label(self) -> None:
        # 子ウィンドウ一覧から、受注Noラベル右側の値を選べる（クラス名非依存）。
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "営業所", "rect": (10, 10, 70, 30)},
            {"class": "TcxTextEdit", "text": "010", "rect": (90, 10, 160, 30)},
            {"class": "TLabel", "text": "受注No.", "rect": (10, 50, 70, 70)},
            {"class": "TcxTextEdit", "text": "1234567", "rect": (90, 50, 200, 70)},
        ]
        self.assertEqual(_pick_order_no_value(controls), "1234567")

    def test_order_no_label_variants(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        for label in ("受注No", "受注No.", "受注№", "受注№.", "受注Ｎｏ", "受注Ｎｏ.", "受注番号"):
            controls = [
                {"class": "TLabel", "text": label, "rect": (10, 50, 70, 70)},
                {"class": "TcxTextEdit", "text": "1394143", "rect": (90, 50, 200, 70)},
                {"class": "TLabel", "text": "物件", "rect": (10, 180, 70, 200)},
                {"class": "TcxTextEdit", "text": "24170845", "rect": (90, 180, 220, 200)},
            ]
            self.assertEqual(_pick_order_no_value(controls), "1394143")

    def test_short_office_code_not_selected(self) -> None:
        # 「010」など短い営業所コードは受注Noとして採用しない。
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "営業所", "rect": (10, 10, 70, 30)},
            {"class": "TcxTextEdit", "text": "010", "rect": (90, 10, 160, 30)},
        ]
        self.assertIsNone(_pick_order_no_value(controls))

    def test_seven_digit_without_order_no_label_not_selected(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TcxTextEdit", "text": "010", "rect": (90, 10, 160, 30)},
            {"class": "TcxTextEdit", "text": "1234567", "rect": (90, 80, 200, 100)},
        ]
        self.assertIsNone(_pick_order_no_value(controls))

    def test_non_edit_class_with_wm_gettext_is_candidate(self) -> None:
        # Edit以外のクラスでも WM_GETTEXT で値が取れれば候補にできる。
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "受注No.", "rect": (10, 50, 70, 70)},
            {"class": "TppCustomControl", "text": "1234567", "rect": (90, 50, 200, 70)},
        ]
        self.assertEqual(_pick_order_no_value(controls), "1234567")

    def test_full_width_digits_candidate(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "受注No.", "rect": (10, 50, 70, 70)},
            {"class": "TcxTextEdit", "text": "１２３４５６７", "rect": (90, 50, 200, 70)},
        ]
        self.assertEqual(_pick_order_no_value(controls), "1234567")

    def test_property_number_not_selected_over_order_no(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "受注No.", "rect": (10, 50, 70, 70)},
            {"class": "TcxTextEdit", "text": "1394141", "rect": (90, 50, 200, 70)},
            {"class": "TLabel", "text": "物件", "rect": (10, 90, 70, 110)},
            {"class": "TcxTextEdit", "text": "24170845", "rect": (90, 90, 220, 110)},
        ]
        self.assertEqual(_pick_order_no_value(controls), "1394141")

    def test_non_order_no_seven_digit_is_not_selected(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TLabel", "text": "物件", "rect": (10, 90, 70, 110)},
            {"class": "TcxTextEdit", "text": "24170845", "rect": (90, 90, 220, 110)},
        ]
        self.assertIsNone(_pick_order_no_value(controls))

    def test_expected_region_fallback_when_label_missing(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TcxTextEdit", "text": "1394143", "rect": (90, 80, 200, 100)},
            {"class": "TcxTextEdit", "text": "24170845", "rect": (90, 260, 220, 280)},
        ]
        self.assertEqual(_pick_order_no_value(controls, (0, 0, 800, 600)), "1394143")

    def test_expected_region_does_not_pick_outside_value(self) -> None:
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "TcxTextEdit", "text": "24170845", "rect": (90, 260, 220, 280)},
        ]
        self.assertIsNone(_pick_order_no_value(controls, (0, 0, 800, 600)))


class CaptureReasonTest(unittest.TestCase):
    """失敗理由・診断・フォールバックのテスト。"""

    def test_window_not_found_message(self) -> None:
        from app.tks_cloud_capture import (
            REASON_WINDOW_NOT_FOUND,
            capture_failure_message,
        )

        msg = capture_failure_message(REASON_WINDOW_NOT_FOUND)
        self.assertIn("見つかりません", msg)

    def test_classify_failure_reasons(self) -> None:
        from app.tks_cloud_capture import (
            REASON_FIELD_NOT_FOUND,
            REASON_NO_CANDIDATE,
            REASON_PRIVILEGE,
            REASON_WINDOW_NOT_FOUND,
            _classify_result,
        )

        self.assertEqual(
            _classify_result(
                value_found=False, window_found=False, access_denied=False, candidate_count=0
            ),
            REASON_WINDOW_NOT_FOUND,
        )
        self.assertEqual(
            _classify_result(
                value_found=False, window_found=True, access_denied=True, candidate_count=0
            ),
            REASON_PRIVILEGE,
        )
        self.assertEqual(
            _classify_result(
                value_found=False, window_found=True, access_denied=False, candidate_count=2
            ),
            REASON_FIELD_NOT_FOUND,
        )
        self.assertEqual(
            _classify_result(
                value_found=False, window_found=True, access_denied=False, candidate_count=0
            ),
            REASON_NO_CANDIDATE,
        )

    def test_uia_skipped_without_comtypes(self) -> None:
        # comtypes 未導入時は落ちずにスキップ理由を返す（Win32 へフォールバック可能）。
        import importlib

        from app.tks_cloud_capture import REASON_UIA_SKIPPED, _uia_capture

        if importlib.util.find_spec("comtypes") is not None:
            self.skipTest("comtypes が導入済みのため UIA スキップを検証できません")
        result = _uia_capture(debug=False)
        self.assertEqual(result.reason, REASON_UIA_SKIPPED)
        self.assertFalse(result.window_found)

    def test_diagnostics_written_only_in_debug(self) -> None:
        import os
        import tempfile

        from app.tks_cloud_capture import write_capture_diagnostics

        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as home:
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                self.assertIsNone(write_capture_diagnostics({"a": 1}, debug=False))
                path = write_capture_diagnostics({"a": 1}, debug=True)
                self.assertIsNotNone(path)
                self.assertTrue(path.exists())
                self.assertIn("order_capture_controls_", path.name)
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_capture_order_no_not_windows(self) -> None:
        # Windows 以外では window_not_found ではなく not_windows を返し、落ちない。
        import sys

        from app.tks_cloud_capture import REASON_NOT_WINDOWS, capture_order_no

        if sys.platform == "win32":
            self.skipTest("Windows 環境のため非Windows分岐を検証できません")
        result = capture_order_no(debug=False)
        self.assertEqual(result.reason, REASON_NOT_WINDOWS)
        self.assertIsNone(result.value)


class ForegroundAndExecuteButtonTest(unittest.TestCase):
    """前面判定（プロセス名込み）と「F12 実行」ボタン矩形取得のテスト。"""

    def test_process_name_matches_kensho(self) -> None:
        # TKSCloud8_KENSHO.exe も対象プロセスとして判定される（要件7）。
        from app.tks_cloud_capture import _process_name_matches_target

        self.assertTrue(_process_name_matches_target("TKSCloud8.exe"))
        self.assertTrue(_process_name_matches_target("TKSCloud8_KENSHO.exe"))
        self.assertTrue(_process_name_matches_target("tkscloud8_kensho.exe"))
        self.assertFalse(_process_name_matches_target("explorer.exe"))
        self.assertFalse(_process_name_matches_target(None))

    def test_foreground_true_when_kensho_process(self) -> None:
        # タイトルが一致しなくても、前面プロセスが検証環境なら対象とみなす（要件7）。
        from app import tks_cloud_capture as mod

        if sys.platform != "win32":
            # 非Windowsでは早期Falseのため、判定関数を直接検証する。
            info = {"title": "別画面", "pid": 1, "process_name": "TKSCloud8_KENSHO.exe"}
            self.assertTrue(
                mod.WINDOW_TITLE_KEYWORD in (info["title"] or "")
                or mod._process_name_matches_target(info["process_name"])
            )
            return
        with mock.patch.object(
            mod,
            "get_foreground_window_info",
            return_value={"title": "別画面", "pid": 1, "process_name": "TKSCloud8_KENSHO.exe"},
        ):
            self.assertTrue(mod.is_target_window_foreground())

    def test_foreground_false_for_other_process(self) -> None:
        from app import tks_cloud_capture as mod

        if sys.platform != "win32":
            info = {"title": "別画面", "pid": 1, "process_name": "explorer.exe"}
            self.assertFalse(
                mod.WINDOW_TITLE_KEYWORD in (info["title"] or "")
                or mod._process_name_matches_target(info["process_name"])
            )
            return
        with mock.patch.object(
            mod,
            "get_foreground_window_info",
            return_value={"title": "別画面", "pid": 1, "process_name": "explorer.exe"},
        ):
            self.assertFalse(mod.is_target_window_foreground())

    def test_pick_execute_button_rect(self) -> None:
        # UIA要素から「F12\n実行」ボタンの矩形を取得できる（要件8）。
        from app.tks_cloud_capture import _pick_execute_button_rect

        elements = [
            {"name": "受注No.", "control_type": 50020, "rect": [10, 50, 70, 70]},
            {"name": "F12\n実行", "control_type": 50000, "rect": [300, 500, 380, 540]},
        ]
        self.assertEqual(_pick_execute_button_rect(elements), (300, 500, 380, 540))

    def test_pick_execute_button_rect_none_when_absent(self) -> None:
        from app.tks_cloud_capture import _pick_execute_button_rect

        elements = [
            {"name": "受注No.", "control_type": 50020, "rect": [10, 50, 70, 70]},
        ]
        self.assertIsNone(_pick_execute_button_rect(elements))

    def test_negative_execute_rect_inside_negative_window_is_valid(self) -> None:
        from app.tks_order_capture_window import _valid_execute_button_rect

        valid, rect, reason = _valid_execute_button_rect(
            (-446, 891, -358, 939),
            tkscloud_window_rect=(-1576, 213, -296, 947),
            screen_rect=(-1920, 0, 1920, 1080),
        )
        self.assertTrue(valid)
        self.assertEqual(rect, (-446, 891, -358, 939))
        self.assertEqual(reason, "")

    def test_is_execute_button_name_ignores_whitespace_and_width(self) -> None:
        from app.tks_cloud_capture import _is_execute_button_name

        self.assertTrue(_is_execute_button_name("F12\n実行"))
        self.assertTrue(_is_execute_button_name("F12 実行"))
        self.assertTrue(_is_execute_button_name("Ｆ12　実行"))
        self.assertFalse(_is_execute_button_name("F11 実行"))
        self.assertFalse(_is_execute_button_name("実行"))


class UiaPathTest(unittest.TestCase):
    """UI Automation 経路（comtypes 利用）のテスト（COM をモックして検証）。"""

    @staticmethod
    def _elements() -> list[dict]:
        # TKSCloud8（WPF）想定: Win32 children が空でも UIA では値が見える。
        return [
            {
                "name": "営業所",
                "control_type": 50020,
                "is_edit": False,
                "has_value_pattern": False,
                "value": "",
                "rect": [10, 10, 70, 30],
            },
            {
                "name": "",
                "control_type": 50004,
                "is_edit": True,
                "has_value_pattern": True,
                "value": "010",
                "rect": [90, 10, 160, 30],
            },
            {
                "name": "受注No.",
                "control_type": 50020,
                "is_edit": False,
                "has_value_pattern": False,
                "value": "",
                "rect": [10, 50, 70, 70],
            },
            {
                "name": "",
                "control_type": 50004,
                "is_edit": True,
                "has_value_pattern": True,
                "value": "12345678",
                "rect": [90, 50, 200, 70],
            },
        ]

    def test_build_uia_result_picks_order_no(self) -> None:
        # UIA要素から、受注Noラベル右側の8桁数字を取得し、010は除外する（要件4・5）。
        from app.tks_cloud_capture import REASON_OK, _build_uia_result

        result = _build_uia_result(self._elements(), debug=True)
        self.assertEqual(result.value, "12345678")
        self.assertEqual(result.reason, REASON_OK)
        self.assertTrue(result.window_found)

    def test_build_uia_result_emits_elements_only_in_debug(self) -> None:
        # 診断JSONには UIA skipped ではなく要素一覧が出る（要件7）。
        from app.tks_cloud_capture import _build_uia_result

        with_debug = _build_uia_result(self._elements(), debug=True)
        self.assertIn("elements", with_debug.diagnostics)
        self.assertNotIn("skipped", with_debug.diagnostics)
        # 取得値が要素の value に存在することを確認できる。
        values = [e["value"] for e in with_debug.diagnostics["elements"]]
        self.assertIn("12345678", values)

        without_debug = _build_uia_result(self._elements(), debug=False)
        self.assertNotIn("elements", without_debug.diagnostics)

    def test_uia_capture_attempted_when_comtypes_present(self) -> None:
        # comtypes がある場合、UIA経路を試行し、要素から値を取得できる（要件1・3）。
        from app import tks_cloud_capture as mod

        sentinel = object()
        with mock.patch.object(mod, "_import_comtypes", return_value=sentinel), \
            mock.patch.object(mod, "_create_uia", return_value=(object(), object())), \
            mock.patch.object(mod, "_uia_find_window", return_value=object()), \
            mock.patch.object(
                mod, "_uia_extract_elements", return_value=(self._elements(), 0)
            ):
            result = mod._uia_capture(debug=True)
        self.assertEqual(result.value, "12345678")
        self.assertIn("elements", result.diagnostics)

    def test_uia_full_scan_success_updates_cache(self) -> None:
        from app import tks_cloud_capture as mod

        previous = mod._ORDER_FIELD_CACHE
        try:
            element = object()
            elements = self._elements()
            elements[3]["_element"] = element
            result = mod._build_uia_result(
                elements, debug=True, window_name="受注入力（見出）", window_rect=(0, 0, 800, 600)
            )
            self.assertEqual(result.value, "12345678")
            self.assertIsNotNone(mod._ORDER_FIELD_CACHE)
            self.assertEqual(mod._ORDER_FIELD_CACHE.kind, "uia")
            self.assertIs(mod._ORDER_FIELD_CACHE.uia_element, element)
        finally:
            mod._ORDER_FIELD_CACHE = previous

    def test_uia_capture_skips_when_comtypes_missing(self) -> None:
        # comtypes が無い場合は従来どおりスキップしてWin32へフォールバックできる（要件2・6）。
        from app import tks_cloud_capture as mod

        with mock.patch.object(mod, "_import_comtypes", return_value=None):
            result = mod._uia_capture(debug=False)
        self.assertEqual(result.reason, mod.REASON_UIA_SKIPPED)
        self.assertEqual(result.diagnostics.get("skipped"), "comtypes not installed")


class UiaDiagnosticsTest(unittest.TestCase):
    """UIA例外診断・COM初期化・gen_dir・要素耐性・候補選定の追加テスト。"""

    def test_exception_diagnostics_emitted_in_debug(self) -> None:
        # 要件1: UIA例外時に type/message/stage/traceback が診断JSONへ出る。
        from app import tks_cloud_capture as mod

        def boom(_c):
            raise RuntimeError("uia boom")

        with mock.patch.object(mod, "_import_comtypes", return_value=object()), \
            mock.patch.object(mod, "_configure_comtypes_gen_dir", return_value="X"), \
            mock.patch.object(mod, "_uia_co_initialize", return_value=False), \
            mock.patch.object(mod, "_create_uia", side_effect=boom):
            result = mod._uia_capture(debug=True)
        self.assertEqual(result.reason, mod.REASON_ERROR)
        diag = result.diagnostics
        self.assertEqual(diag.get("error"), "exception")
        self.assertEqual(diag.get("uia_stage"), "create_uia")
        self.assertEqual(diag.get("exception_type"), "RuntimeError")
        self.assertIn("uia boom", diag.get("exception_message", ""))
        self.assertIn("Traceback", diag.get("traceback", ""))

    def test_exception_details_hidden_without_debug(self) -> None:
        # デバッグOFF時は例外詳細（type/message/traceback）を出さない。
        from app import tks_cloud_capture as mod

        with mock.patch.object(mod, "_import_comtypes", return_value=object()), \
            mock.patch.object(mod, "_configure_comtypes_gen_dir", return_value=None), \
            mock.patch.object(mod, "_uia_co_initialize", return_value=False), \
            mock.patch.object(mod, "_create_uia", side_effect=RuntimeError("x")):
            result = mod._uia_capture(debug=False)
        self.assertEqual(result.diagnostics.get("uia_stage"), "create_uia")
        self.assertNotIn("exception_type", result.diagnostics)
        self.assertNotIn("traceback", result.diagnostics)

    def test_co_initialize_called_and_uninitialized(self) -> None:
        # 要件2: COM初期化処理が呼ばれ、初期化した場合は finally で解放される。
        from app import tks_cloud_capture as mod

        fake_comtypes = mock.Mock()
        with mock.patch.object(mod, "_import_comtypes", return_value=fake_comtypes), \
            mock.patch.object(mod, "_configure_comtypes_gen_dir", return_value="X"), \
            mock.patch.object(mod, "_create_uia", return_value=(object(), object())), \
            mock.patch.object(mod, "_uia_find_window", return_value=object()), \
            mock.patch.object(mod, "_uia_extract_elements", return_value=([], 0)):
            mod._uia_capture(debug=False)
        fake_comtypes.CoInitialize.assert_called_once()
        fake_comtypes.CoUninitialize.assert_called_once()

    def test_co_initialize_survives_already_initialized(self) -> None:
        # 要件3: COM初期化済み（例外）でも落ちず、解放を要求しない。
        from app import tks_cloud_capture as mod

        comtypes = mock.Mock()
        comtypes.CoInitialize.side_effect = OSError("already initialized")
        self.assertFalse(mod._uia_co_initialize(comtypes))

    def test_gen_dir_set_to_writable_path(self) -> None:
        # 要件4: comtypes.gen_dir が書き込み可能パスへ設定される。
        import tempfile

        from app import tks_cloud_capture as mod

        comtypes = mock.Mock()
        previous = os.environ.get("TKS_TO_KINTONE_HOME")
        with tempfile.TemporaryDirectory() as home:
            os.environ["TKS_TO_KINTONE_HOME"] = home
            try:
                gen_dir = mod._configure_comtypes_gen_dir(comtypes)
                self.assertIsNotNone(gen_dir)
                self.assertTrue(os.path.isdir(gen_dir))
                self.assertEqual(comtypes.client.gen_dir, gen_dir)
            finally:
                if previous is None:
                    os.environ.pop("TKS_TO_KINTONE_HOME", None)
                else:
                    os.environ["TKS_TO_KINTONE_HOME"] = previous

    def test_extract_elements_one_failure_does_not_abort(self) -> None:
        # 要件5: UIA要素1件の取得失敗で全体が落ちず、残りを取得できる。
        from app import tks_cloud_capture as mod

        good = mock.Mock()
        good.CurrentName = "受注No."
        good.CurrentControlType = 50004
        rect = mock.Mock(left=90, top=50, right=200, bottom=70)
        good.CurrentBoundingRectangle = rect
        good.GetCurrentPattern.side_effect = Exception("no pattern")

        collection = mock.Mock()
        collection.Length = 2

        def get_element(i):
            if i == 0:
                raise RuntimeError("element 0 broken")
            return good

        collection.GetElement.side_effect = get_element

        window = mock.Mock()
        window.FindAll.return_value = collection
        iuia = mock.Mock()
        uia_module = mock.Mock()
        uia_module.UIA_EditControlTypeId = 50004

        elements, errors = mod._uia_extract_elements(iuia, uia_module, window)
        self.assertEqual(errors, 1)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["control_type"], 50004)

    def test_seven_and_eight_digit_selected_as_order_no(self) -> None:
        # 要件6: 7桁/8桁の数字があれば受注Noとして採用する。
        from app.tks_cloud_capture import _pick_order_no_value

        for order_no in ("1234567", "12345678"):
            controls = [
                {"class": "text", "text": "受注No.", "rect": (10, 50, 70, 70)},
                {"class": "edit", "text": order_no, "rect": (90, 50, 200, 70)},
            ]
            self.assertEqual(_pick_order_no_value(controls), order_no)

    def test_three_digit_not_selected(self) -> None:
        # 要件7: 010 など短い数字は受注Noとして採用しない。
        from app.tks_cloud_capture import _pick_order_no_value

        controls = [
            {"class": "text", "text": "受注No.", "rect": (10, 50, 70, 70)},
            {"class": "edit", "text": "010", "rect": (90, 50, 200, 70)},
        ]
        self.assertIsNone(_pick_order_no_value(controls))

    def test_selection_reason_recorded(self) -> None:
        # 候補選定理由が診断へ出る。
        from app.tks_cloud_capture import _build_uia_result

        elements = [
            {
                "name": "受注No.",
                "control_type": 50020,
                "is_edit": False,
                "has_value_pattern": False,
                "value": "",
                "rect": [10, 50, 70, 70],
            },
            {
                "name": "",
                "control_type": 50004,
                "is_edit": True,
                "has_value_pattern": True,
                "value": "12345678",
                "rect": [90, 50, 200, 70],
            },
        ]
        result = _build_uia_result(elements, debug=True)
        self.assertIn("label_neighbour", result.diagnostics.get("select_reason", ""))
        self.assertEqual(result.diagnostics.get("selection_reason"), "order_label_same_row_right")
        self.assertEqual(result.diagnostics["selected_candidate"]["value"], "12345678")
        self.assertIn("detected_candidates", result.diagnostics)

    def test_reject_reason_recorded_for_outside_candidate(self) -> None:
        from app.tks_cloud_capture import _build_uia_result

        elements = [
            {
                "name": "",
                "control_type": 50004,
                "is_edit": True,
                "has_value_pattern": True,
                "value": "24170845",
                "rect": [90, 260, 220, 280],
            },
        ]
        result = _build_uia_result(elements, debug=True, window_rect=(0, 0, 800, 600))
        self.assertIsNone(result.value)
        self.assertEqual(
            result.diagnostics["rejected_candidates"][0]["reject_reason"],
            "outside_expected_order_no_region",
        )


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CaptureButtonReflectsValueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_capture_success_reflected_in_ui(self) -> None:
        # 手動取得も自動取得と同じ helper wrapper 経由（要件6）。
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.run_capture_via_helper",
            return_value={
                "order_no": "1409999",
                "screen_type": "header",
                "error": "",
                "reason": "ok",
                "elapsed_ms": 10.0,
            },
        ):
            win._on_capture()
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")
        self.assertEqual(win._status_label.text(), "取得OK")
        self.assertEqual(win._status_label.toolTip(), "受注Noを取得できました")

    def test_manual_capture_button_uses_common_worker_starter_when_visible(self) -> None:
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True), \
            mock.patch.object(win, "_start_capture_worker_once") as starter:
            win._on_capture()
        starter.assert_called_once_with(source="manual")

    def test_manual_capture_while_helper_running_queues_one_manual_rerun(self) -> None:
        win = self._make_capture()
        win._capture_worker_running = True
        with mock.patch.object(win, "isVisible", return_value=True):
            win._start_capture_worker_once(source="manual")
        self.assertTrue(win._manual_capture_rerun_requested)

    def test_auto_capture_worker_wrapper_uses_common_finished_path(self) -> None:
        win = self._make_capture()
        with mock.patch.object(win, "_on_capture_worker_finished") as finished:
            win._on_auto_capture_worker_finished("1409999", "", 10.0, "header", 2)
        finished.assert_called_once_with(
            "1409999", "", 10.0, "header", 2, source="auto"
        )

    def test_capture_failure_does_not_crash(self) -> None:
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.run_capture_via_helper",
            return_value={
                "order_no": "",
                "screen_type": "none",
                "error": "",
                "reason": "window_not_found",
                "elapsed_ms": 10.0,
            },
        ):
            win._on_capture()
        self.assertEqual(win._status_label.text(), "取得不可")
        self.assertEqual(win._status_label.toolTip(), "受注Noを取得できませんでした")
        self.assertEqual(win._latest_order_no, "")

    def test_order_input_placeholder_empty(self) -> None:
        win = self._make_capture()
        self.assertEqual(win._order_input.placeholderText(), "")

    def test_capture_window_buttons_have_colored_styles(self) -> None:
        win = self._make_capture()
        style = win.styleSheet()
        self.assertIn("QPushButton#captureButton", style)
        self.assertIn("QPushButton#saveButton", style)
        self.assertIn("QPushButton#addButton", style)
        self.assertIn("QPushButton#listButton", style)
        self.assertIn("QPushButton#closeButton", style)
        self.assertIn("QPushButton:disabled", style)
        self.assertIn("QPushButton#saveButton:disabled", style)
        self.assertIn("QPushButton#addButton:disabled", style)
        self.assertIn("QPushButton:disabled:hover", style)
        self.assertIn("QPushButton:disabled:pressed", style)
        self.assertIn("#D1D5DB", style)

    def test_capture_window_disabled_buttons_are_grayed_by_style(self) -> None:
        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._refresh_add_to_voucher_enabled()
        self.assertFalse(win._save_button.isEnabled())
        self.assertFalse(win._add_to_voucher_button.isEnabled())
        self.assertIn("QPushButton:disabled", win.styleSheet())
        self.assertIn("QPushButton:disabled:hover", win.styleSheet())
        self.assertIn("QPushButton#addButton:disabled", win.styleSheet())

    def test_auto_capture_updates_latest_and_input(self) -> None:
        # 自動取得ワーカーの結果を反映し、最新取得受注Noと手入力欄が更新される（要件5・6）。
        win = self._make_capture()
        win._on_worker_captured("1409999")
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")

    def test_auto_capture_updates_input_even_when_same_latest_or_readonly(self) -> None:
        win = self._make_capture()
        win._auto_captured_value = "1409999"
        win._latest_order_no = "1409999"
        win._order_input.setText("")
        win._auto_save_check.setChecked(True)
        self.assertTrue(win._order_input.isReadOnly())
        win._on_worker_captured("1409999")
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")

    def test_worker_signal_ignored_after_close_started(self) -> None:
        win = self._make_capture()
        win._closing = True
        win._on_worker_captured("1409999")
        win._on_worker_capture_failed()
        self.assertEqual(win._latest_order_no, "")
        self.assertEqual(win._latest_label.text(), "-")
        self.assertEqual(win._status_label.text(), "未保存")

    def test_auto_capture_does_not_save(self) -> None:
        # 自動取得だけでは captured_order_numbers.json に保存されない（要件7・8）。
        from app import captured_orders

        win = self._make_capture()
        win._on_worker_captured("1409999")
        # 受注Noが変化してもう一度反映しても保存しない。
        win._auto_captured_value = ""
        win._on_worker_captured("1409999")
        self.assertEqual(captured_orders.load_captured_orders(), [])
        self.assertFalse(captured_orders.is_dirty())
        self.assertFalse(win._flush_timer_scheduled)
        self.assertEqual(win._status_label.text(), "取得OK")

    def test_auto_capture_failure_keeps_latest(self) -> None:
        # 取得失敗しても最新取得受注Noをすぐには消さない。
        win = self._make_capture()
        win._on_worker_captured("1409999")
        win._on_worker_capture_failed()
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertEqual(win._latest_label.text(), "1409999")

    def test_auto_capture_failure_does_not_set_unavailable_when_latest_valid(self) -> None:
        from app.tks_order_capture_window import _AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD

        win = self._make_capture()
        win._on_worker_captured("1394149")
        for _ in range(_AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD + 1):
            win._on_worker_capture_failed()
        self.assertEqual(win._latest_order_no, "1394149")
        self.assertNotEqual(win._status_label.text(), "取得不可")

    def test_auto_capture_failure_does_not_set_unavailable_when_input_valid(self) -> None:
        from app.tks_order_capture_window import _AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD

        win = self._make_capture()
        win._order_input.setText("1394149")
        for _ in range(_AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD + 1):
            win._on_worker_capture_failed()
        self.assertNotEqual(win._status_label.text(), "取得不可")

    def test_auto_capture_failure_sets_unavailable_only_without_valid_order(self) -> None:
        from app.tks_order_capture_window import _AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD

        win = self._make_capture()
        for _ in range(_AUTO_CAPTURE_FAILURE_STATUS_THRESHOLD):
            win._on_worker_capture_failed()
        self.assertEqual(win._status_label.text(), "取得不可")

    def test_capture_worker_survives_exception(self) -> None:
        # 自動取得ワーカーで例外が出てもスレッドが落ちず、失敗として通知する。
        from app.tks_order_capture_window import _CaptureWorker

        worker = _CaptureWorker()
        failed = []
        worker.capture_failed.connect(lambda: failed.append(True))
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("boom"),
        ):
            worker.capture_once()  # 例外を投げない
        self.assertEqual(failed, [True])

    def test_capture_worker_enabled_captures(self) -> None:
        # 自動取得ONなら取得が実行される（要件4）。
        from app.tks_order_capture_window import _CaptureWorker
        from app.tks_cloud_capture import OrderReadAttempt

        worker = _CaptureWorker()
        worker.set_enabled(True)
        captured = []
        worker.captured.connect(lambda v: captured.append(v))
        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            return_value=OrderReadAttempt("1409999", used_fast_path=True, reason="ok"),
        ):
            worker.capture_once()
        self.assertEqual(captured, ["1409999"])

    def test_capture_worker_six_digits_is_failure_and_continues(self) -> None:
        from app.tks_order_capture_window import _CaptureWorker
        from app.tks_cloud_capture import OrderReadAttempt

        worker = _CaptureWorker()
        worker.set_enabled(True)
        captured = []
        failed = []
        worker.captured.connect(lambda v: captured.append(v))
        worker.capture_failed.connect(lambda: failed.append(True))
        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            return_value=OrderReadAttempt(None, full_scan_used=True, reason="field_not_found"),
        ):
            worker.capture_once()
            worker.capture_once()
        self.assertEqual(captured, [])
        self.assertEqual(failed, [True, True])

    def test_capture_worker_fast_interval_when_entry_window_running(self) -> None:
        from app.tks_order_capture_window import (
            _AUTO_CAPTURE_ACTIVE_INTERVAL_MS,
            _AUTO_CAPTURE_INTERVAL_MS,
            _AUTO_CAPTURE_MIN_INTERVAL_MS,
            _CaptureWorker,
        )

        worker = _CaptureWorker()
        timer = mock.Mock()
        timer.interval.return_value = _AUTO_CAPTURE_INTERVAL_MS
        worker._timer = timer
        self.assertLessEqual(_AUTO_CAPTURE_ACTIVE_INTERVAL_MS, _AUTO_CAPTURE_MIN_INTERVAL_MS)
        with mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            return_value=True,
        ):
            worker._refresh_interval()
        timer.setInterval.assert_called_once_with(_AUTO_CAPTURE_MIN_INTERVAL_MS)

    def test_capture_worker_normal_interval_without_entry_window(self) -> None:
        from app.tks_order_capture_window import _AUTO_CAPTURE_INTERVAL_MS, _CaptureWorker

        worker = _CaptureWorker()
        timer = mock.Mock()
        timer.interval.return_value = 123
        worker._timer = timer
        with mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            return_value=False,
        ):
            worker._refresh_interval()
        timer.setInterval.assert_called_once_with(_AUTO_CAPTURE_INTERVAL_MS)

    def test_capture_worker_disabled_does_not_capture(self) -> None:
        # 自動取得OFFなら取得しない（要件5）。
        from app.tks_order_capture_window import _CaptureWorker

        worker = _CaptureWorker()
        worker.set_enabled(False)
        called = []
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=lambda: called.append(1) or "1409999",
        ):
            worker.capture_once()
        self.assertEqual(called, [])

    def test_capture_worker_no_reentrancy(self) -> None:
        # 取得処理が重なっても多重実行しない（要件17）。
        from app.tks_order_capture_window import _CaptureWorker
        from app.tks_cloud_capture import OrderReadAttempt

        worker = _CaptureWorker()
        worker.set_enabled(True)
        calls = []

        def slow(**_kwargs):
            calls.append(1)
            worker.capture_once()  # 実行中の再入は _busy で無視される
            return OrderReadAttempt("1409999", used_fast_path=True, reason="ok")

        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            side_effect=slow,
        ):
            worker.capture_once()
        self.assertEqual(len(calls), 1)

    def test_capture_worker_pending_is_consumed_once(self) -> None:
        from app.tks_order_capture_window import _CaptureWorker
        from app.tks_cloud_capture import OrderReadAttempt

        worker = _CaptureWorker()
        worker.set_enabled(True)
        calls = []
        scheduled = []

        def slow(**_kwargs):
            calls.append(1)
            worker.capture_once()
            worker.capture_once()
            return OrderReadAttempt("1409999", used_fast_path=True, reason="ok")

        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            side_effect=slow,
        ), mock.patch("app.tks_order_capture_window.QTimer.singleShot") as single_shot:
            single_shot.side_effect = lambda _ms, callback: scheduled.append(callback)
            worker.capture_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(scheduled), 1)
        self.assertFalse(worker._pending)

    def test_capture_worker_same_order_no_emit_is_not_suppressed(self) -> None:
        from app.tks_order_capture_window import _CaptureWorker
        from app.tks_cloud_capture import OrderReadAttempt

        worker = _CaptureWorker()
        worker.set_enabled(True)
        captured = []
        worker.captured.connect(lambda v: captured.append(v))
        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            return_value=OrderReadAttempt("1409999", used_fast_path=True, reason="ok"),
        ):
            worker.capture_once()
            worker.capture_once()
        self.assertEqual(captured, ["1409999", "1409999"])

    def test_capture_worker_slow_tick_backs_off_interval(self) -> None:
        from app.tks_order_capture_window import (
            _AUTO_CAPTURE_BACKOFF_STEP_MS,
            _AUTO_CAPTURE_MIN_INTERVAL_MS,
            _CaptureWorker,
        )

        worker = _CaptureWorker()
        timer = mock.Mock()
        timer.interval.return_value = _AUTO_CAPTURE_MIN_INTERVAL_MS
        worker._timer = timer
        worker._apply_slow_tick_backoff(250)
        self.assertEqual(worker._backoff_ms, _AUTO_CAPTURE_BACKOFF_STEP_MS)
        timer.setInterval.assert_called_once()

    def test_workers_start_on_show_and_stop_on_close(self) -> None:
        # 自動取得は画面所有timerで起動する。execute monitor は自動保存ONでも起動しない
        # （安定性のため常駐QThreadを廃止・要件2）。
        win = self._make_capture()
        self.assertIsNone(win._auto_capture_timer)
        self.assertIsNone(win._execute_thread)
        win.show()
        self.assertIsNotNone(win._auto_capture_timer)
        self.assertTrue(win._auto_capture_timer.isActive())
        # 自動保存OFF（既定）では実行検知ワーカーは起動しない。
        self.assertIsNone(win._execute_thread)
        # 自動保存ONにしても execute monitor は起動しない（要件2）。
        win._auto_save_check.setChecked(True)
        self.assertIsNone(win._execute_thread)
        self.assertIsNone(win._execute_worker)
        win.close()
        self.assertFalse(win._auto_capture_timer.isActive())
        self.assertIsNone(win._execute_thread)

    def test_auto_capture_uses_single_ui_timer(self) -> None:
        win = self._make_capture()
        win._start_auto_capture_timer()
        first = win._auto_capture_timer
        win._start_auto_capture_timer()
        self.assertIs(win._auto_capture_timer, first)
        self.assertFalse(hasattr(win, "_f12_monitor_timer"))

    def test_auto_capture_restarts_inactive_timer_when_workers_already_started(self) -> None:
        win = self._make_capture()
        win.show()
        self.assertTrue(win._auto_capture_timer.isActive())
        win._auto_capture_timer.stop()
        self.assertFalse(win._auto_capture_timer.isActive())
        win._start_workers()
        self.assertTrue(win._auto_capture_timer.isActive())

    def test_auto_row_uses_capture_and_save_checkboxes(self) -> None:
        # 「自動： □取得 □保存」形式になっている（要件1）。
        win = self._make_capture()
        self.assertEqual(win._auto_capture_check.text(), "取得")
        self.assertEqual(win._auto_save_check.text(), "保存")

    def test_auto_capture_default_on(self) -> None:
        # 自動取得の初期値はON（要件・自動設定）。
        win = self._make_capture()
        self.assertTrue(win._auto_capture_check.isChecked())

    def test_auto_capture_start_runs_tick_and_reflects_first_order(self) -> None:
        win = self._make_capture()
        self.assertFalse(win._capture_worker_running)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1409999", "", 12.0)
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")
        self.assertEqual(win._status_label.text(), "取得OK")

    def test_auto_capture_tick_starts_worker(self) -> None:
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True), \
            mock.patch.object(win, "_start_auto_capture_worker_once") as start_worker:
            win._on_auto_capture_tick()
        start_worker.assert_called_once()

    def test_auto_capture_tick_while_worker_running_requests_one_rerun(self) -> None:
        win = self._make_capture()
        win._capture_worker_running = True
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_tick()
            win._on_auto_capture_tick()
        self.assertTrue(win._capture_rerun_requested)
        self.assertEqual(win._capture_rerun_count, 2)

    def test_auto_capture_worker_finished_consumes_rerun_once(self) -> None:
        win = self._make_capture()
        win._capture_worker_running = True
        win._capture_rerun_requested = True
        with mock.patch.object(win, "isVisible", return_value=True), \
            mock.patch("app.tks_order_capture_window.QTimer.singleShot") as single_shot:
            win._on_auto_capture_worker_finished("1409999", "", 10.0)
        self.assertFalse(win._capture_rerun_requested)
        single_shot.assert_called_once()

    def test_auto_capture_worker_result_ignored_after_close(self) -> None:
        win = self._make_capture()
        win._closing = True
        win._capture_worker_running = True
        win._on_auto_capture_worker_finished("1409999", "", 10.0)
        self.assertEqual(win._latest_label.text(), "-")

    def test_auto_capture_sync_test_helper_reflects_first_order(self) -> None:
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            return_value="1409999",
        ) as capture:
            win._run_auto_capture_sync_for_test()
        capture.assert_called()
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")
        self.assertEqual(win._status_label.text(), "取得OK")

    def test_worker_result_displays_only_even_when_auto_save_on(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1409999", "", 10.0)
        self.assertEqual(captured_orders.load_captured_orders(), [])
        self.assertFalse(captured_orders.is_dirty())
        self.assertFalse(win._flush_timer_scheduled)
        self.assertEqual(win._latest_label.text(), "1409999")
        self.assertEqual(win._order_input.text(), "1409999")
        self.assertEqual(win._status_label.text(), "取得OK")

    def test_auto_capture_tick_updates_when_order_changes(self) -> None:
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=["1409999", "1410000"],
        ):
            win._run_auto_capture_sync_for_test()
            win._run_auto_capture_sync_for_test()
        self.assertEqual(win._latest_label.text(), "1410000")
        self.assertEqual(win._order_input.text(), "1410000")

    def test_auto_capture_tick_running_is_cleared_after_exception(self) -> None:
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("boom"),
        ):
            win._run_auto_capture_sync_for_test()
        self.assertFalse(win._capture_tick_running)

    def test_auto_capture_interval_is_bounded(self) -> None:
        from app.tks_order_capture_window import _AUTO_CAPTURE_MIN_INTERVAL_MS

        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            return_value=True,
        ):
            self.assertGreaterEqual(win._current_auto_capture_interval(), _AUTO_CAPTURE_MIN_INTERVAL_MS)
            self.assertLessEqual(win._current_auto_capture_interval(), 2000)

    def test_auto_capture_setting_persisted(self) -> None:
        # 自動取得ON/OFFがQSettingsに保存される（要件2）。
        from app.tks_order_capture_window import _SETTINGS_AUTO_CAPTURE

        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.sync()
        raw = str(settings.value(_SETTINGS_AUTO_CAPTURE)).strip().lower()
        self.assertIn(raw, {"0", "false", "no", "off"})
        win2 = self._make_capture()
        self.assertFalse(win2._auto_capture_check.isChecked())

    def test_auto_save_on_locks_input_and_shows_badge(self) -> None:
        # 自動保存ON時、手入力欄が編集不可＋🔒バッヂ表示＋保存ボタン無効（要件7・8）。
        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        self.assertTrue(win._order_input.isReadOnly())
        self.assertFalse(win._lock_badge.isHidden())
        self.assertFalse(win._save_button.isEnabled())

    def test_auto_save_off_input_editable(self) -> None:
        # 自動保存OFF時、手入力欄が編集可能・バッヂ非表示（要件9）。
        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._auto_save_check.setChecked(False)
        self.assertFalse(win._order_input.isReadOnly())
        self.assertTrue(win._lock_badge.isHidden())
        self.assertTrue(win._save_button.isEnabled())

    def test_auto_save_on_preserves_existing_input(self) -> None:
        # 自動保存ONにしても既に入力されている手入力欄の値は消さない。
        win = self._make_capture()
        win._order_input.setText("1234567")
        win._auto_save_check.setChecked(True)
        self.assertEqual(win._order_input.text(), "1234567")

    def test_manual_save_blocked_when_auto_save_on(self) -> None:
        # 自動保存ON時は手動「保存」で保存しない（案内のみ）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        with mock.patch(
            "app.tks_order_capture_window.QMessageBox.information"
        ) as info:
            win._on_save()
        info.assert_called_once()
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_app_f12_shortcut_removed(self) -> None:
        # アプリ側F12ショートカットは廃止済み。画面に QShortcut を登録しない（要件1/9）。
        from PySide6.QtGui import QShortcut

        win = self._make_capture()
        self.assertEqual(win.findChildren(QShortcut), [])
        self.assertFalse(hasattr(win, "_f12_shortcut"))
        self.assertFalse(hasattr(win, "_on_f12_pressed"))
        self.assertFalse(hasattr(win, "_on_execute_button_clicked"))

    def test_execute_button_saves_last_valid_order_without_recapture(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._last_valid_order_no = "1409999"
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not recapture"),
        ):
            win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1409999"],
        )

    def test_f12_saves_last_valid_order_without_recapture(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(False)
        win._last_valid_order_no = "1409999"
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not recapture"),
        ):
            win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1409999"],
        )

    def test_same_latest_order_no_does_not_repeat_heavy_ui_refresh(self) -> None:
        win = self._make_capture()
        with mock.patch.object(win, "_refresh_add_to_voucher_enabled") as refresh, \
            mock.patch.object(win, "_sync_execute_context") as sync:
            win._set_latest_order_no("1409999")
            win._set_latest_order_no("1409999")
        refresh.assert_called_once()
        sync.assert_called_once()

    def test_buttons_in_single_horizontal_row(self) -> None:
        # 取得・保存・追加・閉じるボタンが横一列（同一QHBoxLayout）に配置される（要件10）。
        from PySide6.QtWidgets import QHBoxLayout, QLayout

        win = self._make_capture()
        targets = {
            win._capture_button,
            win._save_button,
            win._add_to_voucher_button,
            win._close_button,
        }

        def find_row(layout):
            widgets = set()
            for i in range(layout.count()):
                item = layout.itemAt(i)
                w = item.widget()
                if w is not None:
                    widgets.add(w)
                child = item.layout()
                if isinstance(child, QLayout):
                    hit = find_row(child)
                    if hit is not None:
                        return hit
            if isinstance(layout, QHBoxLayout) and targets.issubset(widgets):
                return layout
            return None

        row = find_row(win.layout())
        self.assertIsNotNone(row)

    def test_execute_debug_jsonl_written_with_save_result(self) -> None:
        # 実行検知から保存結果までの診断JSONLが work/debug に出力される。
        from app.path_utils import get_order_capture_debug_dir

        win = self._make_capture()
        win._settings.setValue("ui/debug_visible", "1")
        win._settings.sync()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        win._on_execute_detected("f12_key", diag={})
        files = list(
            get_order_capture_debug_dir().glob("order_capture_execute_*.jsonl")
        )
        self.assertTrue(files)
        rows = []
        for path in files:
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        self.assertIn("saved", {row.get("save_result") for row in rows})
        self.assertIn("save_success", {row.get("event") for row in rows})

    def test_capture_worker_debug_jsonl_records_fast_path_and_elapsed(self) -> None:
        from app.path_utils import get_order_capture_debug_dir
        from app.tks_cloud_capture import OrderReadAttempt
        from app.tks_order_capture_window import _CaptureWorker

        settings = QSettings("Manekiya", "TksToKintone")
        settings.setValue("ui/debug_visible", "1")
        settings.sync()
        worker = _CaptureWorker()
        worker.set_enabled(True)
        worker.set_debug(True)
        with mock.patch(
            "app.tks_cloud_capture.read_order_no_attempt_from_tkscloud8",
            return_value=OrderReadAttempt("1409999", used_fast_path=True, reason="ok"),
        ):
            worker.capture_once()
        rows = []
        for path in get_order_capture_debug_dir().glob("order_capture_worker_*.jsonl"):
            rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        events = {row.get("event") for row in rows}
        self.assertIn("capture_fast_path_success", events)
        self.assertTrue(any("elapsed_ms" in row for row in rows))

    def test_auto_save_default_off(self) -> None:
        # 初期値はOFF（要件2・設定保存）。
        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())

    def test_auto_save_setting_persisted_to_qsettings(self) -> None:
        # 設定ON/OFFがQSettingsに保存される（要件3）。
        from app.tks_order_capture_window import _SETTINGS_AUTO_SAVE

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.sync()
        raw = str(settings.value(_SETTINGS_AUTO_SAVE)).strip().lower()
        self.assertIn(raw, {"1", "true", "yes", "on"})
        # 再生成したウィンドウが保存済みのONを読み戻す。
        win2 = self._make_capture()
        self.assertTrue(win2._auto_save_check.isChecked())

    def test_capture_does_not_save_even_with_auto_on(self) -> None:
        # 受注Noが取得（変化）しただけでは保存されない（要件4）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch(
            "app.tks_order_capture_window.run_capture_via_helper",
            return_value={
                "order_no": "1409999",
                "screen_type": "header",
                "error": "",
                "reason": "ok",
                "elapsed_ms": 10.0,
            },
        ):
            win._on_capture()
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_f12_detection_saves_displayed_order_without_read(self) -> None:
        # F12実行検知は重い再取得を待たず、表示中の受注Noを保存する。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1409999")
        with mock.patch(
            "app.tks_cloud_capture.read_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not read"),
        ) as read:
            win._on_execute_detected("f12_key")
        read.assert_not_called()
        orders = captured_orders.load_captured_orders()
        self.assertEqual([o["order_no"] for o in orders], ["1409999"])
        self.assertEqual(orders[0]["method"], "f12")
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertEqual(win._status_label.toolTip(), "F12実行時に保存しました")

    def test_f12_detection_uses_latest_when_refetch_fails(self) -> None:
        # 実行の瞬間に再取得が失敗しても、直前に自動取得できた受注Noで保存する（要件4）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")  # 自動取得済みの受注No
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("refetch boom"),
        ) as refetch:
            win._on_execute_detected("f12_key")
        # 最新取得済みを優先するため再取得は呼ばれない。
        refetch.assert_not_called()
        orders = captured_orders.load_captured_orders()
        self.assertEqual([o["order_no"] for o in orders], ["1409999"])
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_execute_uses_input_when_latest_is_empty(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1394147")
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("refetch boom"),
        ) as refetch:
            win._on_execute_detected("execute_button_click")
        refetch.assert_not_called()
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1394147"],
        )

    def test_execute_click_saves_with_hidden_order_entry_from_input(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1394148")
        with mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            return_value=False,
        ), mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not refetch"),
        ) as refetch:
            win._on_execute_detected("execute_button_click", diag={"target_order_entry_window_exists": False})
        refetch.assert_not_called()
        self.assertEqual([o["order_no"] for o in captured_orders.load_captured_orders()], ["1394148"])

    def test_execute_click_saves_with_hidden_order_entry_from_latest(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1394149")
        with mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            return_value=False,
        ), mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not refetch"),
        ) as refetch:
            win._on_execute_detected("execute_button_click", diag={"target_order_entry_window_exists": False})
        refetch.assert_not_called()
        self.assertEqual([o["order_no"] for o in captured_orders.load_captured_orders()], ["1394149"])

    def test_execute_no_save_when_auto_save_off(self) -> None:
        # 自動保存OFF時は実行検知しても保存しない（要件5・10）。
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        win._set_latest_order_no("1409999")
        win._on_execute_detected("f12_key")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_execute_button_click_no_save_when_auto_save_off(self) -> None:
        # 自動保存OFF時は実行ボタンクリック検知でも保存しない。
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        win._set_latest_order_no("1409999")
        win._on_execute_detected("execute_button_click")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_execute_detection_no_duplicate(self) -> None:
        # 重複受注Noは二重保存されない（要件12）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1409999")
        win._on_execute_detected("f12_key")
        win._on_execute_detected("f12_key")
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)

    def test_execute_detection_no_save_when_empty(self) -> None:
        # 受注Noが空欄なら保存しない（要件8・13）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            return_value=None,
        ):
            win._on_execute_detected("f12_key")
        self.assertEqual(captured_orders.load_captured_orders(), [])
        self.assertEqual(
            win._status_label.text(),
            "取得不可",
        )

    def test_execute_detection_survives_exception(self) -> None:
        # 実行検知処理で例外が出てもアプリが落ちない。
        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("boom"),
        ):
            win._on_execute_detected("f12_key")  # 例外を投げない
        self.assertEqual(
            win._status_label.text(),
            "取得不可",
        )

    def test_auto_save_releases_guard_on_success(self) -> None:
        # 自動保存成功後は _saving ガードが必ず解除される。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        win._on_execute_detected("f12_key")
        self.assertFalse(win._saving)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1409999"]
        )

    def test_auto_save_releases_guard_and_retries_after_failure(self) -> None:
        # 保存失敗後もガードが解除され、同じ受注Noで再試行できる（要件）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        with mock.patch.object(
            captured_orders, "stage_order", side_effect=OSError("disk boom")
        ):
            win._on_execute_detected("f12_key")
        # 失敗後: ガード解除・状態は保存失敗・保存済みにはなっていない。
        self.assertFalse(win._saving)
        self.assertEqual(win._status_label.text(), "保存失敗")
        self.assertEqual(captured_orders.load_captured_orders(), [])
        # 同じ受注Noで再試行すると今度は保存できる。
        win._on_execute_detected("f12_key")
        self.assertFalse(win._saving)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1409999"]
        )

    def test_auto_save_skipped_when_reentrant(self) -> None:
        # _saving=True でもメモリstageへ即時に進む。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        win._saving = True
        win._on_execute_detected("f12_key")
        self.assertFalse(win._saving)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1409999"],
        )

    def test_new_order_not_treated_as_saved_after_previous(self) -> None:
        # 前の受注No保存後に別の新規受注Noを検知したら、保存済み扱いにせず保存する。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        win._on_execute_detected("f12_key")
        # 新しい受注Noへ切り替え（前回状態を持ち越さない）。
        win._reflect_detected_order_no("1408888")
        self.assertEqual(win._status_label.text(), "取得OK")
        win._set_latest_order_no("1408888")
        win._on_execute_detected("f12_key")
        self.assertEqual(
            {o["order_no"] for o in captured_orders.load_captured_orders()},
            {"1409999", "1408888"},
        )

    @staticmethod
    def _make_execute_worker():
        from app.tks_order_capture_window import _ExecuteWorker

        worker = _ExecuteWorker()
        worker.set_enabled(True)
        # F12キー／実行ボタンのクリック座標監視は既定OFF。この一連のテストは
        # そのクリック/キー検知経路を検証するため、明示的にONにする（要件2/4）。
        worker.set_f12_monitor(True)
        detected = []
        worker.execute_detected.connect(lambda s, d: detected.append((s, d)))
        return worker, detected

    def test_execute_worker_fires_on_f12_edge_when_foreground(self) -> None:
        # 前面がTKSCloud8でF12押下エッジを検知したら実行検知を通知する（要件3・11）。
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=True
        ):
            worker.poll_once()  # 押下エッジ→発火
            worker.poll_once()  # 押しっぱなしでは再発火しない
        self.assertEqual([s for s, _ in detected], ["f12_key"])

    def test_execute_worker_ignored_when_not_foreground_without_target_or_order(self) -> None:
        # 前面がTKSCloud8でなく、対象画面・保存候補もなければF12押下で通知しない。
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=False
        ):
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_execute_worker_fires_on_f12_when_not_foreground_but_target_and_latest_exist(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.set_order_context("1409999", "")
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=True
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["f12_key"])
        self.assertTrue(detected[0][1]["target_window_exists"])
        self.assertEqual(detected[0][1]["latest_order_no"], "1409999")

    def test_execute_worker_disabled_ignores_edges(self) -> None:
        # 実行検知ワーカーが無効（自動保存OFF相当）ならエッジを無視する（要件14）。
        worker, detected = self._make_execute_worker()
        worker.set_enabled(False)
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=True
        ):
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_execute_worker_button_click_inside_rect(self) -> None:
        # 「F12 実行」ボタン矩形内の左クリックエッジで実行検知を通知する（要件9・12）。
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
            return_value=(100, 200, 180, 230),
        ), mock.patch(
            "app.tks_order_capture_window._get_cursor_pos", return_value=(140, 215)
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])
        self.assertEqual(detected[0][1]["mouse_pos"], [140, 215])

    def test_execute_worker_click_uses_physical_cursor_when_available(self) -> None:
        worker, detected = self._make_execute_worker()
        worker._cached_execute_button_rect = (-446, 891, -358, 939)
        worker._tkscloud_window_rect = (-1576, 213, -296, 947)
        worker._rect_cache_updated_at = 1_000_000.0
        with mock.patch("time.monotonic", return_value=1_000_001.0), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=True), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch("app.tks_order_capture_window._get_cursor_pos", return_value=(-330, 259)), \
            mock.patch("app.tks_order_capture_window._get_physical_cursor_pos", return_value=(-400, 910)), \
            mock.patch("app.tks_order_capture_window._monitor_info_for_point", return_value={
                "monitor_handle": 1,
                "monitor_rect": (-1920, 0, 0, 1080),
                "dpi_scale_x": 1.5,
                "dpi_scale_y": 1.5,
            }), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])
        self.assertEqual(detected[0][1]["raw_mouse_pos"], [-330, 259])
        self.assertEqual(detected[0][1]["normalized_mouse_pos"], [-400, 910])

    def test_execute_worker_click_scales_mouse_when_dpi_differs(self) -> None:
        worker, detected = self._make_execute_worker()
        worker._cached_execute_button_rect = (95, 95, 160, 140)
        worker._tkscloud_window_rect = (0, 0, 240, 180)
        worker._rect_cache_updated_at = 1_000_000.0
        with mock.patch("time.monotonic", return_value=1_000_001.0), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=True), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch("app.tks_order_capture_window._get_cursor_pos", return_value=(50, 50)), \
            mock.patch("app.tks_order_capture_window._get_physical_cursor_pos", return_value=None), \
            mock.patch("app.tks_order_capture_window._monitor_info_for_point", return_value={
                "monitor_handle": 1,
                "monitor_rect": (0, 0, 1920, 1080),
                "dpi_scale_x": 2.0,
                "dpi_scale_y": 2.0,
            }), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])
        self.assertEqual(detected[0][1]["normalized_mouse_pos"], [100, 100])

    def test_execute_worker_button_click_inside_rect_even_when_not_foreground(self) -> None:
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
            return_value=(100, 200, 180, 230),
        ), mock.patch(
            "app.tks_order_capture_window._get_cursor_pos", return_value=(140, 215)
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])
        self.assertFalse(detected[0][1]["foreground_is_tkscloud8"])

    def test_execute_worker_button_click_inside_rect_without_order_entry_window(self) -> None:
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
            return_value=(100, 200, 180, 230),
        ), mock.patch(
            "app.tks_order_capture_window._get_cursor_pos", return_value=(140, 215)
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])
        self.assertFalse(detected[0][1]["target_order_entry_window_exists"])
        self.assertTrue(detected[0][1]["click_inside_execute_button"])

    def test_execute_worker_rejects_invalid_cached_rect(self) -> None:
        worker, detected = self._make_execute_worker()
        worker._cached_execute_button_rect = (-446, 891, -358, 939)
        worker._tkscloud_window_rect = (0, 0, 1200, 900)
        worker._rect_cache_updated_at = 1_000_000.0
        with mock.patch("time.monotonic", return_value=1_000_001.0), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=True), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch("app.tks_order_capture_window._get_cursor_pos", return_value=(140, 215)), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_execute_click_rejected_has_reason(self) -> None:
        worker, detected = self._make_execute_worker()
        rejected = []
        worker.set_debug(True)
        worker.edge_diagnostics.connect(lambda d: rejected.append(d))
        worker._cached_execute_button_rect = (100, 200, 180, 240)
        worker._tkscloud_window_rect = (0, 0, 300, 300)
        worker._rect_cache_updated_at = 1_000_000.0
        with mock.patch("time.monotonic", return_value=1_000_001.0), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=True), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch("app.tks_order_capture_window._get_cursor_pos", return_value=(10, 10)), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual(detected, [])
        self.assertEqual(rejected[-1]["reject_reason"], "click_outside_rect")

    def test_execute_worker_uses_inferred_rect_when_uia_rect_invalid(self) -> None:
        worker, detected = self._make_execute_worker()
        with mock.patch("app.tks_order_capture_window._tkscloud_window_rect", return_value=(0, 0, 1200, 900)), \
            mock.patch(
                "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
                return_value=(-446, 891, -358, 939),
            ):
            worker._refresh_execute_rect_cache_if_needed()
        self.assertIsNotNone(worker._cached_execute_button_rect)
        self.assertEqual(worker._cached_execute_button_rect, worker._inferred_execute_button_rect)
        x = (worker._cached_execute_button_rect[0] + worker._cached_execute_button_rect[2]) // 2
        y = (worker._cached_execute_button_rect[1] + worker._cached_execute_button_rect[3]) // 2
        with mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=True), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch("app.tks_order_capture_window._get_cursor_pos", return_value=(x, y)), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["execute_button_click"])

    def test_execute_worker_does_not_start_rect_lookup_after_stop_requested(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.request_stop()
        with mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8"
        ) as rect_lookup:
            worker.poll_once()
        rect_lookup.assert_not_called()
        self.assertEqual(detected, [])

    def test_execute_worker_f12_with_latest_does_not_require_order_entry_window(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.set_order_context("1409999", "")
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=False
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["f12_key"])

    def test_execute_worker_transition_header_to_detail_emits(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.set_order_context("1394149", "")
        worker._current_tks_title = "受注入力（見出）"
        with mock.patch("time.monotonic", return_value=10.0), \
            mock.patch("app.tks_order_capture_window._tkscloud_window_title", return_value="受注入力（明細）"), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["detail_screen_detected"])
        self.assertTrue(detected[0][1]["transition_detected"])

    def test_execute_worker_transition_other_to_header_does_not_emit(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.set_order_context("", "1394149")
        worker._current_tks_title = "売上入力"
        with mock.patch("time.monotonic", return_value=10.0), \
            mock.patch("app.tks_order_capture_window._tkscloud_window_title", return_value="受注入力（見出）"), \
            mock.patch("app.tks_order_capture_window._f12_key_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._left_mouse_is_down", return_value=False), \
            mock.patch("app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False), \
            mock.patch("app.tks_order_capture_window._tks_order_entry_window_running", return_value=False), \
            mock.patch.object(worker, "_refresh_execute_rect_cache_if_needed"):
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_execute_worker_transition_debounced(self) -> None:
        worker, detected = self._make_execute_worker()
        worker.set_order_context("1394149", "")
        worker._current_tks_title = "受注入力（見出）"
        with mock.patch("app.tks_order_capture_window._tkscloud_window_title", return_value="受注入力（明細）"):
            self.assertTrue(worker._check_screen_transition({}, 10.0))
        worker._current_tks_title = "受注入力（見出）"
        with mock.patch("app.tks_order_capture_window._tkscloud_window_title", return_value="受注入力（明細）"):
            self.assertFalse(worker._check_screen_transition({}, 11.0))
        self.assertEqual([s for s, _ in detected], ["detail_screen_detected"])

    def test_execute_worker_button_click_outside_rect(self) -> None:
        # クリック位置がボタン矩形外なら通知しない（要件10）。
        worker, detected = self._make_execute_worker()
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=False
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
            return_value=(100, 200, 180, 230),
        ), mock.patch(
            "app.tks_order_capture_window._get_cursor_pos", return_value=(10, 10)
        ):
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_worker_execute_detected_saves_end_to_end(self) -> None:
        # ワーカーからの実行検知通知でUI側が保存する（要件9・11）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1409999")
        win._on_worker_execute_detected("execute_button_click", {})
        orders = captured_orders.load_captured_orders()
        self.assertEqual([o["order_no"] for o in orders], ["1409999"])
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertEqual(win._status_label.toolTip(), "実行ボタン押下時に保存しました")

    def test_worker_transition_detected_saves_end_to_end(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1394149")
        win._on_worker_execute_detected(
            "detail_screen_detected",
            {
                "source": "detail_screen_detected",
                "previous_tks_title": "受注入力（見出）",
                "current_tks_title": "受注入力（明細）",
                "transition_detected": True,
            },
        )
        orders = captured_orders.load_captured_orders()
        self.assertEqual([o["order_no"] for o in orders], ["1394149"])
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertEqual(win._status_label.toolTip(), "保存しました")

    def test_worker_header_detected_does_not_save(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("1394149")
        win._on_worker_execute_detected(
            "detail_screen_detected",
            {
                "source": "detail_screen_detected",
                "previous_tks_title": "メニュー",
                "current_tks_title": "受注入力（見出）",
                "transition_detected": True,
            },
        )
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_worker_detail_detected_does_not_save_same_order_repeatedly(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._reflect_detected_order_no("1394149")
        diag = {
            "source": "detail_screen_detected",
            "previous_tks_title": "受注入力（見出）",
            "current_tks_title": "受注入力（明細）",
            "transition_detected": True,
        }
        win._on_worker_execute_detected("detail_screen_detected", diag)
        win._on_worker_execute_detected("detail_screen_detected", diag)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1394149"],
        )

    def test_worker_detail_detected_saves_new_order_after_order_changes(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        diag = {
            "source": "detail_screen_detected",
            "previous_tks_title": "受注入力（見出）",
            "current_tks_title": "受注入力（明細）",
            "transition_detected": True,
        }
        win._reflect_detected_order_no("1394149")
        win._on_worker_execute_detected("detail_screen_detected", diag)
        win._reflect_detected_order_no("1394150")
        win._on_worker_execute_detected("detail_screen_detected", diag)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1394149", "1394150"],
        )

    def test_worker_detail_detected_no_order_does_not_crash(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        result = win._save_current_order_no_from_detail_detected(
            diag={"current_tks_title": "受注入力（明細）"}
        )
        self.assertEqual(result, "empty")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_worker_transition_detected_no_save_when_auto_save_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._set_latest_order_no("1394149")
        win._on_worker_execute_detected(
            "detail_screen_detected",
            {"source": "detail_screen_detected", "transition_detected": True},
        )
        self.assertEqual(captured_orders.load_captured_orders(), [])


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class ExecuteAndF12SaveTest(unittest.TestCase):
    """実行ボタン/F12での確実な保存（要件1）と状態表示の正確化（要件4）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_execute_button_saves_new_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1409999"]
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_f12_saves_new_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1409999"]
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_f12_monitor_event_saves_via_single_entry(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1392343")
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392343"],
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_f12_monitor_does_not_save_when_auto_save_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1392343")
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_execute_button_does_not_capture_or_flush_synchronously(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1409999")
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not capture"),
        ) as capture, mock.patch.object(captured_orders, "flush") as flush:
            win._execute_and_save_current_order_no("f12")
        capture.assert_not_called()
        flush.assert_not_called()
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertEqual(win._count_label.text(), "1 件")

    def test_f12_does_not_capture_or_flush_synchronously(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1409999")
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("should not capture"),
        ) as capture, mock.patch.object(captured_orders, "flush") as flush:
            win._execute_and_save_current_order_no("f12")
        capture.assert_not_called()
        flush.assert_not_called()
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_f12_save_uses_main_thread(self) -> None:
        from PySide6.QtCore import QThread

        win = self._make_capture()
        win._order_input.setText("1392343")

        def assert_main_thread(*args, **kwargs):
            self.assertIs(QThread.currentThread(), win.thread())
            return original(*args, **kwargs)

        original = win._save_order_no
        with mock.patch.object(win, "_save_order_no", side_effect=assert_main_thread):
            win._execute_and_save_current_order_no("f12")

    def test_execute_button_saves_while_worker_running(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._capture_worker_running = True
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1409999"],
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_f12_saves_with_saved_list_open(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._on_open_list()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1392343")
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392343"],
        )
        self.assertEqual(win._list_window._table.rowCount(), 1)

    def test_f12_saves_after_saved_list_closed(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._on_open_list()
        list_window = win._list_window
        list_window.close()
        win._on_list_closed()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1392343")
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392343"],
        )

    def test_f12_monitor_ignored_after_close_started(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._order_input.setText("1392343")
        win._closing = True
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_f12_save_exception_recovers_for_next_save(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1392343")
        with mock.patch.object(captured_orders, "stage_order", side_effect=RuntimeError("boom")):
            win._execute_and_save_current_order_no("f12")
        self.assertFalse(win._saving)
        self.assertFalse(win._manual_save_in_progress)
        self.assertEqual(captured_orders.load_captured_orders(), [])
        win._last_f12_save_requested_at = 0.0
        win._order_input.setText("1392344")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392344"],
        )

    def test_f12_selects_1392343_from_last_valid_and_latest_detected(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._last_valid_order_no = "1392343"
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392343"],
        )

        win2 = self._make_capture()
        win2._latest_detected_order_no = "1392343"
        win2._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1392343"],
        )

    def test_f12_saves_while_worker_running(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._capture_worker_running = True
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1409999"],
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_execute_saves_even_when_auto_save_off(self) -> None:
        # 自動保存OFF（＝自動保存が未実行/失敗相当）でも実行ボタンで保存する。
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        win._set_latest_order_no("1394147")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394147"]
        )

    def test_f12_saves_even_when_auto_save_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        win._set_latest_order_no("1394148")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394148"]
        )

    def test_execute_does_not_duplicate_saved_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)
        self.assertEqual(win._status_label.text(), "重複")

    def test_guard_released_after_exception_allows_next_save(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        # 最新も手入力も空で、再取得が例外 → 取得不可（保存しない）。
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=RuntimeError("boom"),
        ):
            win._execute_and_save_current_order_no("f12")
        self.assertFalse(win._saving)
        self.assertEqual(win._status_label.text(), "取得不可")
        # ガードが解除されているので、次回は保存できる。
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1409999"]
        )

    def test_reentrant_save_is_skipped(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._order_input.setText("2222222")
        win._saving = True
        result = win._execute_and_save_current_order_no("f12")
        self.assertEqual(result, "saved")
        self.assertFalse(win._saving)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["2222222"],
        )

    def test_auto_save_during_guard_is_pending_and_saved_after_release(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("2222222")
        win._saving = True
        win._on_execute_detected("f12_key")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["2222222"],
        )
        self.assertEqual(win._pending_auto_save, {})
        self.assertFalse(win._saving)

    def test_auto_save_failure_can_retry_same_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no("2222222")
        with mock.patch(
            "app.captured_orders.stage_order",
            side_effect=[RuntimeError("boom"), (True, "saved")],
        ) as add:
            win._on_execute_detected("f12_key")
            self.assertEqual(win._status_label.text(), "保存失敗")
            win._on_execute_detected("f12_key")
        self.assertEqual(add.call_count, 2)

    def test_auto_save_continuous_new_orders_are_all_saved(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        for order_no in ("1111111", "2222222", "３３３３３３３"):
            win._set_latest_order_no(order_no)
            win._on_execute_detected("f12_key")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1111111", "2222222", "3333333"],
        )

    def test_execute_no_save_when_no_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(captured_orders.load_captured_orders(), [])
        self.assertEqual(win._status_label.text(), "取得不可")

    # ── 要件4: ログ・状態表示の正確化 ────────────────────────────────────────
    def test_new_order_not_shown_as_saved(self) -> None:
        win = self._make_capture()
        win._reflect_detected_order_no("1409999")
        self.assertNotEqual(win._status_label.text(), "保存済み")
        self.assertEqual(win._status_label.text(), "取得OK")

    def test_saved_success_shows_hozon_ok(self) -> None:
        win = self._make_capture()
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_existing_saved_order_shows_saved(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1409999")
        win = self._make_capture()
        win._reflect_detected_order_no("1409999")
        self.assertEqual(win._status_label.text(), "取得OK")
        win._execute_and_save_current_order_no("f12")
        self.assertIn(win._status_label.text(), {"重複", "保存済み"})

    def test_status_reset_when_order_changes(self) -> None:
        win = self._make_capture()
        win._order_input.setText("1409999")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(win._status_label.text(), "保存OK")
        # 別の新規受注Noを取得したら前回の「保存OK」は残らない。
        win._reflect_detected_order_no("1400000")
        self.assertEqual(win._status_label.text(), "取得OK")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class LightweightSaveTest(unittest.TestCase):
    """自動保存の軽量化（即時メモリ反映とディスクflushの分離）のテスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()
        from app import captured_orders

        captured_orders.reset_cache()

    def tearDown(self) -> None:
        from app import captured_orders

        captured_orders.reset_cache()
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self, voucher_window=None):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: voucher_window)
        self.addCleanup(win.deleteLater)
        return win

    def _auto_save(self, win, order_no: str) -> None:
        win._auto_save_check.setChecked(True)
        win._set_latest_order_no(order_no)
        win._on_execute_detected("f12_key")

    def _disk_orders(self):
        from app import captured_orders

        path = captured_orders.get_captured_path()
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # 1. 保存要求後すぐにメモリ上リストへ追加される
    def test_auto_save_reflects_in_memory_immediately(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self._auto_save(win, "2222222")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["2222222"]
        )
        self.assertTrue(captured_orders.is_dirty())

    # 2. 保存要求後すぐにUI状態が保存OKになる
    def test_auto_save_updates_ui_status_immediately(self) -> None:
        win = self._make_capture()
        self._auto_save(win, "2222222")
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertEqual(win._count_label.text(), "1 件")

    # 3. 自動保存1件ごとに同期のディスク書き込み（atomic save）が直接呼ばれない
    def test_auto_save_does_not_write_disk_synchronously(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        with mock.patch.object(captured_orders, "_write_to_disk") as writer:
            self._auto_save(win, "2222222")
        writer.assert_not_called()
        # ただしメモリには反映済みで、dirty（要flush）である。
        self.assertTrue(captured_orders.is_dirty())

    def test_flush_delay_is_1000ms_or_less(self) -> None:
        from app.tks_order_capture_window import _SAVE_FLUSH_DEBOUNCE_MS

        self.assertLessEqual(_SAVE_FLUSH_DEBOUNCE_MS, 1000)

    def test_save_ok_does_not_wait_for_flush_completion(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        with mock.patch.object(captured_orders, "flush", side_effect=RuntimeError("should not flush")):
            self._auto_save(win, "2222222")
        self.assertEqual(win._status_label.text(), "保存OK")
        self.assertTrue(captured_orders.is_dirty())

    # 4/5. 保存中フラグが残っていてもpending待ちせず全件即時保存される
    def test_pending_queue_saves_all_orders_detected_during_save(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._saving = True
        for order_no in ("1111111", "2222222", "3333333"):
            win._set_latest_order_no(order_no)
            win._on_execute_detected("f12_key")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1111111", "2222222", "3333333"],
        )
        self.assertEqual(win._pending_auto_save, {})
        self.assertFalse(win._saving)

    # 6. dirty状態でcloseEventするとflushされる
    def test_close_flushes_dirty_orders_to_disk(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self._auto_save(win, "2222222")
        self.assertEqual(self._disk_orders(), [])  # まだ書かれていない
        win.close()
        self.assertFalse(captured_orders.is_dirty())
        self.assertEqual([o["order_no"] for o in self._disk_orders()], ["2222222"])

    # 7. closeEventでpending自動保存も処理される
    def test_close_processes_pending_auto_save(self) -> None:
        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        win._set_pending_auto_save("2222222", "f12_key", {})
        self.assertIn("2222222", win._pending_auto_save)
        win.close()
        self.assertEqual([o["order_no"] for o in self._disk_orders()], ["2222222"])

    # 8. flush失敗時にdirtyが残り、再試行できる
    def test_flush_failure_keeps_dirty_and_retries(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self._auto_save(win, "2222222")
        with mock.patch.object(
            captured_orders, "_write_to_disk", side_effect=OSError("disk boom")
        ):
            ok = win._flush_saved_orders_now(reason="debounce")
        self.assertFalse(ok)
        self.assertTrue(captured_orders.is_dirty())
        # 再試行すると成功し、dirty が解消する。
        ok2 = win._flush_saved_orders_now(reason="debounce")
        self.assertTrue(ok2)
        self.assertFalse(captured_orders.is_dirty())
        self.assertEqual([o["order_no"] for o in self._disk_orders()], ["2222222"])

    # 9. 保存済みsetにより重複判定され、重複保存されない
    def test_duplicate_detected_via_cache_set(self) -> None:
        from app import captured_orders

        saved1, reason1 = captured_orders.stage_order("1409999")
        saved2, reason2 = captured_orders.stage_order("1409999")
        self.assertTrue(saved1)
        self.assertEqual(reason1, "saved")
        self.assertFalse(saved2)
        self.assertEqual(reason2, "duplicate")
        self.assertEqual(len(captured_orders.load_captured_orders()), 1)

    # 10. 保存済み一覧画面が開いている場合、UIが更新される
    def test_open_list_updates_on_auto_save(self) -> None:
        win = self._make_capture()
        win._on_open_list()
        self.assertEqual(win._list_window._table.rowCount(), 0)
        self._auto_save(win, "2222222")
        self.assertEqual(win._list_window._table.rowCount(), 1)

    # 11. 一覧が開いていても、1件保存では全再構築(_reload)せず増分追加する
    def test_open_list_uses_incremental_update_not_full_reload(self) -> None:
        win = self._make_capture()
        win._on_open_list()
        lw = win._list_window
        with mock.patch.object(lw, "_reload") as reload_mock, mock.patch.object(
            lw, "note_saved_order", wraps=lw.note_saved_order
        ) as note_mock:
            self._auto_save(win, "2222222")
        reload_mock.assert_not_called()
        note_mock.assert_called_once()

    # 11b. 一覧が閉じている場合、UI更新処理を呼ばない
    def test_closed_list_skips_ui_update(self) -> None:
        win = self._make_capture()
        self.assertIsNone(win._list_window)
        # 例外なくスキップされる（一覧が無ければ何もしない）。
        self._auto_save(win, "2222222")

    # 12. 追加処理後の削除・保存リスト更新と競合しない
    def test_delete_after_auto_save_stays_consistent(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self._auto_save(win, "2222222")
        win._flush_saved_orders_now(reason="debounce")
        win._on_open_list()
        lw = win._list_window
        lw._delete_rows([0])
        self.assertEqual(captured_orders.load_captured_orders(), [])
        win._refresh_count()
        self.assertEqual(win._count_label.text(), "0 件")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class AlwaysOnTopCheckboxTest(unittest.TestCase):
    """受注No取込画面の「常に手前に表示」チェック（要件3）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_default_checked_and_flag_on(self) -> None:
        win = self._make_capture()
        self.assertTrue(win._always_on_top_check.isChecked())
        self.assertTrue(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_uncheck_removes_flag(self) -> None:
        win = self._make_capture()
        win._always_on_top_check.setChecked(False)
        self.assertFalse(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_recheck_adds_flag(self) -> None:
        win = self._make_capture()
        win._always_on_top_check.setChecked(False)
        win._always_on_top_check.setChecked(True)
        self.assertTrue(bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_setting_persisted_and_restored(self) -> None:
        from app.tks_order_capture_window import _SETTINGS_ALWAYS_ON_TOP

        win = self._make_capture()
        win._always_on_top_check.setChecked(False)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.sync()
        raw = str(settings.value(_SETTINGS_ALWAYS_ON_TOP)).strip().lower()
        self.assertIn(raw, {"0", "false", "no", "off"})
        win2 = self._make_capture()
        self.assertFalse(win2._always_on_top_check.isChecked())
        self.assertFalse(bool(win2.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_checkbox_in_auto_row(self) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLayout

        win = self._make_capture()

        def find_row(layout):
            widgets = set()
            for i in range(layout.count()):
                item = layout.itemAt(i)
                w = item.widget()
                if w is not None:
                    widgets.add(w)
                child = item.layout()
                if isinstance(child, QLayout):
                    hit = find_row(child)
                    if hit is not None:
                        return hit
            if isinstance(layout, QHBoxLayout) and {
                win._auto_capture_check,
                win._always_on_top_check,
            }.issubset(widgets):
                return layout
            return None

        self.assertIsNotNone(find_row(win.layout()))


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CapturedOrdersListWindowTest(unittest.TestCase):
    """保存済み受注No一覧画面のテスト。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_list(self, voucher_window=None):
        from app.captured_orders_window import CapturedOrdersWindow

        win = CapturedOrdersWindow(voucher_window_provider=lambda: voucher_window)
        self.addCleanup(win.deleteLater)
        return win

    @staticmethod
    def _check(win, row: int, checked: bool = True) -> None:
        box = win._table.cellWidget(row, 0).findChild(QCheckBox)
        box.setChecked(checked)

    def test_list_window_opens(self) -> None:
        win = self._make_list()
        self.assertTrue(win.isWindow())

    def test_removed_buttons_and_columns(self) -> None:
        win = self._make_list()
        self.assertFalse(hasattr(win, "_add_row_button"))
        self.assertFalse(hasattr(win, "_save_button"))
        headers = [
            win._table.horizontalHeaderItem(i).text()
            for i in range(win._table.columnCount())
        ]
        self.assertEqual(headers, ["□", "受注No", "保存日時", "保存方法", "操作"])
        self.assertNotIn("伝票追加済み", headers)
        self.assertNotIn("OLAP取得済み", headers)
        self.assertEqual(win._delete_row_button.text(), "選択削除")
        self.assertTrue(win._add_to_voucher_button.styleSheet() or win.styleSheet())
        self.assertTrue(win._delete_row_button.styleSheet() or win.styleSheet())
        self.assertTrue(win._close_button.styleSheet() or win.styleSheet())
        self.assertIn("QPushButton:disabled", win.styleSheet())
        self.assertIn("QPushButton#addToVoucherButton:disabled", win.styleSheet())
        self.assertIn("QPushButton#deleteButton:disabled", win.styleSheet())
        self.assertIn("QPushButton:disabled:hover", win.styleSheet())
        self.assertIn("QPushButton:disabled:pressed", win.styleSheet())
        self.assertIn("#D1D5DB", win.styleSheet())

    def test_empty_list_action_buttons_are_disabled_gray(self) -> None:
        win = self._make_list()
        self.assertEqual(win._table.rowCount(), 0)
        self.assertFalse(win._add_to_voucher_button.isEnabled())
        self.assertFalse(win._delete_row_button.isEnabled())
        self.assertTrue(win._close_button.isEnabled())
        style = win.styleSheet()
        self.assertIn("QPushButton#addToVoucherButton:disabled", style)
        self.assertIn("QPushButton#deleteButton:disabled", style)
        self.assertIn("#D1D5DB", style)

    def test_list_shows_saved_orders_and_checkboxes(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(win._table.item(0, 1).text(), "1405773")
        self.assertIsNotNone(win._table.cellWidget(0, 0).findChild(QCheckBox))

    def test_list_edit_auto_saves_json(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        got = []
        win.saved.connect(lambda: got.append(True))
        win._table.item(0, 1).setText("1400000")
        orders = captured_orders.load_captured_orders()
        self.assertEqual([o["order_no"] for o in orders], ["1400000"])
        self.assertEqual(got, [True])

    def test_full_width_edit_normalized(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        win._table.item(0, 1).setText("１２３４５６７")
        self.assertEqual(captured_orders.load_captured_orders()[0]["order_no"], "1234567")

    def test_invalid_edit_is_not_saved(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        win._table.item(0, 1).setText("ABC")
        self.assertEqual([o["order_no"] for o in captured_orders.load_captured_orders()], ["1405773"])
        win._table.item(0, 1).setText("")
        self.assertEqual([o["order_no"] for o in captured_orders.load_captured_orders()], ["1405773"])
        win._table.item(0, 1).setText("123456")
        self.assertEqual([o["order_no"] for o in captured_orders.load_captured_orders()], ["1405773"])

    def test_duplicate_edit_is_not_saved(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        win = self._make_list()
        win._table.item(1, 1).setText("1405773")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1405773", "1405774"],
        )

    def test_row_delete_button_always_enabled(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        button = win._table.cellWidget(0, 4)
        self.assertTrue(button.isEnabled())
        self._check(win, 0, True)
        self.assertTrue(button.isEnabled())
        self.assertIn('rowDeleteButton="true"', win.styleSheet())

    def test_add_button_requires_checked_row_and_voucher_window(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        self.assertFalse(win._add_to_voucher_button.isEnabled())
        self._check(win, 0, True)
        self.assertFalse(win._add_to_voucher_button.isEnabled())

        fake = mock.Mock()
        win = self._make_list(voucher_window=fake)
        self.assertFalse(win._add_to_voucher_button.isEnabled())
        self._check(win, 0, True)
        self.assertTrue(win._add_to_voucher_button.isEnabled())
        self.assertIn("QPushButton#addToVoucherButton", win.styleSheet())
        self.assertIn("QPushButton:disabled", win.styleSheet())

    def test_selected_delete_enabled_only_when_checked(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        win = self._make_list()
        self.assertFalse(win._delete_row_button.isEnabled())
        self._check(win, 0, True)
        self.assertTrue(win._delete_row_button.isEnabled())
        self.assertIn("QPushButton#deleteButton", win.styleSheet())
        self.assertIn("QPushButton:disabled", win.styleSheet())

    def test_header_checkbox_selects_and_clears_all(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        fake = mock.Mock()
        win = self._make_list(voucher_window=fake)
        header = win._table.horizontalHeader()

        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "□")
        header.sectionClicked.emit(0)
        self.assertEqual(win._checked_rows(), [0, 1])
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "☑")
        self.assertTrue(win._add_to_voucher_button.isEnabled())
        self.assertTrue(win._delete_row_button.isEnabled())

        header.sectionClicked.emit(0)
        self.assertEqual(win._checked_rows(), [])
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "□")
        self.assertFalse(win._add_to_voucher_button.isEnabled())
        self.assertFalse(win._delete_row_button.isEnabled())

    def test_header_checkbox_shows_partial_state(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        win = self._make_list(voucher_window=mock.Mock())
        self._check(win, 0, True)
        self.assertEqual(win._table.horizontalHeaderItem(0).text(), "◩")

    def test_checked_rows_only_added_to_voucher(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        fake = mock.Mock()
        fake.add_order_no_and_fetch.return_value = {"status": "added"}
        win = self._make_list(voucher_window=fake)
        self._check(win, 1, True)
        win._on_add_to_voucher()
        fake.add_order_no_and_fetch.assert_called_once_with("1405774")

    def test_multiple_checked_rows_added_to_voucher(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        fake = mock.Mock()
        fake.add_order_no_and_fetch.return_value = {"status": "added"}
        win = self._make_list(voucher_window=fake)
        self._check(win, 0, True)
        self._check(win, 1, True)
        win._on_add_to_voucher()
        self.assertEqual(
            [call.args[0] for call in fake.add_order_no_and_fetch.call_args_list],
            ["1405773", "1405774"],
        )

    def test_list_add_removes_added_and_duplicate_checked_rows(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        captured_orders.add_captured_order("1405775")
        fake = mock.Mock()
        fake.add_order_no_and_fetch.side_effect = [
            {"status": "added"},
            {"status": "duplicate"},
        ]
        win = self._make_list(voucher_window=fake)
        self._check(win, 0, True)
        self._check(win, 1, True)
        win._on_add_to_voucher()
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1405775"],
        )
        self.assertEqual(win._table.rowCount(), 1)
        self.assertIn("削除: 2件", win._status_label.text())

    def test_row_delete_button_deletes_only_that_row_and_auto_saves(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        win = self._make_list()
        got = []
        win.saved.connect(lambda: got.append(True))
        self._check(win, 0, True)
        win._table.cellWidget(0, 4).click()
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1405774"],
        )
        self.assertEqual(got, [True])

    def test_bottom_delete_deletes_checked_rows_and_auto_saves(self) -> None:
        from app import captured_orders

        captured_orders.add_captured_order("1405773")
        captured_orders.add_captured_order("1405774")
        captured_orders.add_captured_order("1405775")
        win = self._make_list()
        self._check(win, 0, True)
        self._check(win, 2, True)
        win._delete_row_button.click()
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()],
            ["1405774"],
        )

    def test_list_corrupt_file_does_not_crash(self) -> None:
        from app import captured_orders

        path = captured_orders.get_captured_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ broken ]", encoding="utf-8")
        win = self._make_list()
        self.assertEqual(win._table.rowCount(), 0)

    def test_capture_window_count_updates_after_list_auto_save(self) -> None:
        from app import captured_orders
        from app.tks_order_capture_window import TksOrderCaptureWindow

        captured_orders.add_captured_order("1405773")
        cap = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(cap.deleteLater)
        cap._on_open_list()
        lst = cap._list_window
        self.assertIsNotNone(lst)
        self._check(lst, 0, True)
        lst._delete_row_button.click()
        self.assertEqual(cap._count_label.text(), "0 件")

    def test_capture_window_refresh_voucher_state_updates_open_list(self) -> None:
        from app import captured_orders
        from app.tks_order_capture_window import TksOrderCaptureWindow

        captured_orders.add_captured_order("1405773")
        voucher = {"window": None}
        cap = TksOrderCaptureWindow(voucher_window_provider=lambda: voucher["window"])
        self.addCleanup(cap.deleteLater)
        cap._on_open_list()
        lst = cap._list_window
        self.assertIsNotNone(lst)
        self._check(lst, 0, True)
        self.assertFalse(lst._add_to_voucher_button.isEnabled())
        voucher["window"] = mock.Mock()
        cap.refresh_voucher_state()
        self.assertTrue(lst._add_to_voucher_button.isEnabled())

    def test_list_close_does_not_close_capture(self) -> None:
        from app.tks_order_capture_window import TksOrderCaptureWindow

        cap = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(cap.deleteLater)
        cap._on_open_list()
        lst = cap._list_window
        self.assertIsNotNone(lst)
        lst.close()
        self.assertIsNone(cap._list_window)
        self.assertTrue(cap.isWindow())


class ExecuteMonitorSafetyTest(unittest.TestCase):
    """50msグローバル監視の廃止・実行検知ワーカーの安全化（要件2/4/7）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name
        )
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_execute_monitor_interval_is_at_least_500ms(self) -> None:
        # 50ms の高頻度ポーリングは廃止し、最短500ms以上とする（要件2）。
        from app import tks_order_capture_window as mod

        self.assertGreaterEqual(mod._EXECUTE_POLL_INTERVAL_MS, 500)
        self.assertGreaterEqual(mod._EXECUTE_MONITOR_MIN_INTERVAL_MS, 500)

    def test_execute_monitor_not_started_by_default_on_show(self) -> None:
        # 既定（自動保存OFF・F12 monitor設定OFF）では実行検知ワーカーを起動しない（要件2/7）。
        win = self._make_capture()
        win.show()
        self.assertIsNone(win._execute_thread)
        self.assertIsNone(win._execute_worker)
        win.close()

    def test_auto_save_does_not_start_execute_monitor(self) -> None:
        # 自動保存ONでも execute monitor（常駐QThread）は起動しない（要件2/8）。
        win = self._make_capture()
        win.show()
        self.assertIsNone(win._execute_thread)
        win._auto_save_check.setChecked(True)
        self.assertIsNone(win._execute_thread)
        self.assertIsNone(win._execute_worker)
        win.close()

    def test_execute_monitor_starts_only_when_f12_monitor_explicit(self) -> None:
        # tks_capture/f12_monitor_enabled=True の明示設定時だけ execute monitor を起動する。
        settings = QSettings("Manekiya", "TksToKintone")
        settings.setValue("tks_capture/f12_monitor_enabled", "1")
        settings.sync()
        win = self._make_capture()
        win.show()
        self.assertIsNotNone(win._execute_thread)
        self.assertTrue(win._execute_thread.isRunning())
        win.close()
        self.assertIsNone(win._execute_thread)

    def test_execute_worker_refs_none_by_default(self) -> None:
        # 既定設定では _execute_thread / _execute_worker が None のまま（要件2）。
        win = self._make_capture()
        win.show()
        win._auto_save_check.setChecked(True)
        win._maybe_start_execute_monitor()
        self.assertIsNone(win._execute_thread)
        self.assertIsNone(win._execute_worker)
        win.close()

    def test_execute_worker_started_timer_interval_at_least_500(self) -> None:
        # ワーカーが実際に生成する QTimer の間隔が最短500ms以上であること（要件2）。
        from app.tks_order_capture_window import _ExecuteWorker

        worker = _ExecuteWorker()
        try:
            worker.start()
            self.assertIsNotNone(worker._timer)
            self.assertGreaterEqual(worker._timer.interval(), 500)
        finally:
            worker.stop()

    def test_execute_worker_f12_monitor_default_off_ignores_edges(self) -> None:
        # F12 monitor 設定が既定OFFのとき、F12/マウスのエッジでは発火しない（要件2/4/9）。
        from app.tks_order_capture_window import _ExecuteWorker

        worker = _ExecuteWorker()
        worker.set_enabled(True)  # 自動保存ON相当でも、F12 monitorがOFFなら座標検知しない
        detected = []
        worker.execute_detected.connect(lambda s, d: detected.append((s, d)))
        with mock.patch(
            "app.tks_order_capture_window._f12_key_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._left_mouse_is_down", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window.execute_button_rect_from_tkscloud8",
            side_effect=AssertionError("重いWin32矩形取得はF12 monitor OFFでは呼ばない"),
        ):
            worker.poll_once()
            worker.poll_once()
        self.assertEqual(detected, [])

    def test_execute_worker_transition_saves_without_f12_monitor(self) -> None:
        # F12 monitor がOFFでも、見出→明細の画面遷移では発火する（要件4）。
        from app.tks_order_capture_window import _ExecuteWorker

        worker = _ExecuteWorker()
        worker.set_enabled(True)
        worker.set_order_context("1392348", "")
        worker._current_tks_title = "受注入力（見出）"
        detected = []
        worker.execute_detected.connect(lambda s, d: detected.append((s, d)))
        with mock.patch(
            "app.tks_order_capture_window._tkscloud_window_title",
            return_value="受注入力（明細）",
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud8_is_foreground", return_value=True
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running", return_value=True
        ):
            worker.poll_once()
        self.assertEqual([s for s, _ in detected], ["detail_screen_detected"])

    def test_tks_f12_monitor_save_independent_of_execute_monitor_thread(self) -> None:
        # TKS側F12検知保存（opt-in monitor経由）は execute monitor thread の有無に依存しない。
        from app import captured_orders

        win = self._make_capture()
        self.assertIsNone(win._execute_thread)  # 監視threadは無くてよい
        win._order_input.setText("1392348")
        with mock.patch.object(
            win, "_execute_and_save_current_order_no", wraps=win._execute_and_save_current_order_no
        ) as spy:
            win._execute_and_save_current_order_no("f12")
        spy.assert_called_once_with("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1392348"]
        )
        self.assertEqual(win._status_label.text(), "保存OK")

    def test_tks_f12_monitor_save_when_auto_save_off(self) -> None:
        # 自動保存OFFでも、TKS側F12検知保存（monitor経由）は保存する（source=f12は常に保存）。
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        win._order_input.setText("1392347")
        win._execute_and_save_current_order_no("f12")
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1392347"]
        )

    def test_uia_value_accepted_even_when_win32_window_missing(self) -> None:
        # UIAで7桁以上のASCII数字が取れていれば、Win32側 window_found=false でも有効扱い（要件5）。
        win = self._make_capture()
        # 検出反映（Win32列挙が失敗していてもUIA値がそのまま渡ってくる想定）。
        win._reflect_detected_order_no("1392348")
        self.assertEqual(win._last_valid_order_no, "1392348")
        self.assertEqual(win._latest_detected_order_no, "1392348")
        # この状態でアプリ側実行ボタンは last_valid/latest_detected から保存できる。
        from app import captured_orders

        win._order_input.clear()
        win._execute_and_save_current_order_no("f12")
        self.assertIn(
            "1392348", [o["order_no"] for o in captured_orders.load_captured_orders()]
        )

    def test_long_run_ticks_do_not_grow_threads(self) -> None:
        # 長時間相当に監視起動要求を繰り返しても execute monitor は増殖しない（既定は起動しない）。
        settings = QSettings("Manekiya", "TksToKintone")
        settings.setValue("tks_capture/f12_monitor_enabled", "1")
        settings.sync()
        win = self._make_capture()
        win.show()
        thread = win._execute_thread
        self.assertIsNotNone(thread)
        for _ in range(50):
            # 監視の起動要求を何度呼んでも二重起動しない。
            win._maybe_start_execute_monitor()
        self.assertIs(win._execute_thread, thread)
        win.close()
        self.assertIsNone(win._execute_thread)

    def test_worker_result_after_close_does_not_crash(self) -> None:
        # close後にワーカー結果が戻っても落ちない（要件7/10）。
        win = self._make_capture()
        win._closing = True
        # 例外を出さずに無視されること。
        win._on_worker_execute_detected("f12_key", {"f12_edge_detected": True})
        win._on_worker_captured("1392348")

    # ── 画面遷移検出を capture worker へ統合（要件3） ──────────────────────────
    def test_capture_header_remembers_order(self) -> None:
        # header 画面 + 有効受注No → last_header_order_no に保持する。
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
        self.assertEqual(win._last_header_order_no, "1394149")

    def test_capture_detail_saves_when_auto_save_on(self) -> None:
        # 次の capture 結果 detail → 自動保存ONなら保存する（execute monitorなし）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394149"]
        )

    def test_capture_detail_does_not_save_when_auto_save_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        self.assertFalse(win._auto_save_check.isChecked())
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_capture_detail_does_not_double_save_same_order(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394149"]
        )

    # ── Section 8: 擬似的な長時間稼働の安定性テスト ────────────────────────────
    def test_capture_ticks_do_not_spawn_unbounded_workers(self) -> None:
        # capture worker 実行中に tick しても worker は増えない（要件4）。
        win = self._make_capture()
        win._capture_worker_running = True
        with mock.patch.object(win, "isVisible", return_value=True), \
            mock.patch.object(win, "_start_auto_capture_worker_once") as start_worker:
            for _ in range(1000):
                win._on_auto_capture_tick()
        start_worker.assert_not_called()
        self.assertTrue(win._capture_rerun_requested)

    def test_heartbeat_does_not_probe_uia(self) -> None:
        # heartbeat は内部状態だけを出し、UIA/Win32探索をしない（要件6）。
        win = self._make_capture()
        with mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=AssertionError("heartbeatでUIA探索はしない"),
        ), mock.patch(
            "app.tks_order_capture_window._tkscloud_window_title",
            side_effect=AssertionError("heartbeatでWin32探索はしない"),
        ), mock.patch(
            "app.tks_order_capture_window._tks_order_entry_window_running",
            side_effect=AssertionError("heartbeatでWin32探索はしない"),
        ):
            for _ in range(5):
                win._emit_heartbeat()

    def test_generation_mismatch_result_is_ignored(self) -> None:
        # 世代不一致の worker 結果は破棄する（要件7）。
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1409999", "", 10.0, "header", win._generation + 5)
        self.assertEqual(win._latest_label.text(), "-")
        self.assertEqual(win._last_header_order_no, "")

    def test_close_stops_all_timers_and_ignores_callbacks(self) -> None:
        # close後は timer/worker が止まり、戻ってきた結果も無視される（要件4/7）。
        win = self._make_capture()
        win.show()
        gen_before = win._generation
        win.close()
        self.assertGreater(win._generation, gen_before)
        self.assertIsNone(win._execute_thread)
        if win._auto_capture_timer is not None:
            self.assertFalse(win._auto_capture_timer.isActive())
        if win._heartbeat_timer is not None:
            self.assertFalse(win._heartbeat_timer.isActive())
        # close後に古い世代の結果が戻ってもUI更新しない。
        win._on_auto_capture_worker_finished("1409999", "", 10.0, "header", gen_before)
        self.assertEqual(win._latest_label.text(), "-")

    def test_capture_worker_result_contains_only_primitives(self) -> None:
        # worker結果にCOM/UIAオブジェクトを含めず、安全なプリミティブだけ返す（要件5）。
        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(3, command=["dummy"])
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        with mock.patch.object(FakeHelperProcess, "payload", payload), mock.patch(
            "subprocess.Popen", FakeHelperProcess
        ):
            worker.run()
        self.assertEqual(len(results), 1)
        order_no, error, elapsed_ms, screen_type, generation = results[0]
        self.assertIsInstance(order_no, str)
        self.assertIsInstance(error, str)
        self.assertIsInstance(elapsed_ms, float)
        self.assertIsInstance(screen_type, str)
        self.assertIsInstance(generation, int)
        self.assertEqual(order_no, "1409999")
        self.assertEqual(screen_type, "header")
        self.assertEqual(generation, 3)

    def test_capture_worker_does_not_call_uia_in_process(self) -> None:
        # 本体worker内でUIA/COM/Win32を直接呼ばず、helperをsubprocessで起動するだけ（要件2/4）。
        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(0, command=["dummy"])
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        payload = json.dumps(
            {"ok": True, "screen_type": "detail", "order_no": "", "reason": "detail_detected"}
        ).encode("utf-8")
        with mock.patch.object(FakeHelperProcess, "payload", payload), mock.patch(
            "subprocess.Popen", FakeHelperProcess
        ), mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=AssertionError("本体プロセスでUIA探索してはならない"),
        ), mock.patch(
            "app.tks_order_capture_window._com_initialize",
            side_effect=AssertionError("本体プロセスでCOM初期化してはならない"),
        ):
            worker.run()
        self.assertEqual(results[0][3], "detail")

    def test_capture_worker_timeout_does_not_crash(self) -> None:
        # helper timeout でも本体は落ちず、error=timeout・取得不可扱いになる（要件4/7）。
        import subprocess

        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(0, command=["dummy"], timeout_ms=200)
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        with mock.patch("subprocess.Popen", TimeoutHelperProcess):
            worker.run()
        self.assertEqual(len(results), 1)
        order_no, error, _elapsed, screen_type, _gen = results[0]
        self.assertEqual(order_no, "")
        self.assertEqual(error, "timeout")
        self.assertEqual(screen_type, "unknown")
        self.assertTrue(TimeoutHelperProcess.killed)

    def test_capture_worker_crash_does_not_crash_app(self) -> None:
        # helperが異常終了（非0 returncode）しても本体は落ちず取得不可扱い（要件4）。
        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(0, command=["dummy"])
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        with mock.patch.object(FakeHelperProcess, "payload", b""), mock.patch.object(
            FakeHelperProcess, "returncode_value", 3
        ), mock.patch("subprocess.Popen", FakeHelperProcess):
            worker.run()
        self.assertEqual(results[0][0], "")
        self.assertTrue(results[0][1])  # error が入っている

    def test_capture_worker_invalid_json_does_not_crash(self) -> None:
        # helperが不正JSONを返しても本体は落ちない（要件4/9）。
        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(0, command=["dummy"])
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        with mock.patch.object(FakeHelperProcess, "payload", b"not json at all"), mock.patch(
            "subprocess.Popen", FakeHelperProcess
        ):
            worker.run()
        self.assertEqual(results[0][0], "")
        self.assertIn("invalid_json", results[0][1])

    def test_capture_worker_missing_helper_does_not_crash(self) -> None:
        # helperが解決できない（command=None）でも本体は落ちず取得不可扱い（要件5）。
        from app.tks_order_capture_window import _CaptureOnceWorker

        worker = _CaptureOnceWorker(0, command=None)
        results = []
        worker.finished.connect(lambda *a: results.append(a))
        worker.run()
        self.assertEqual(results[0][0], "")
        self.assertEqual(results[0][1], "helper_unavailable")

    def test_long_running_capture_poll_stability(self) -> None:
        # 自動保存ONで1000tick相当を回しても execute monitor は起動せず thread は増えない。
        win = self._make_capture()
        win.show()
        win._auto_save_check.setChecked(True)
        self.assertIsNone(win._execute_thread)
        with mock.patch.object(win, "isVisible", return_value=True), \
            mock.patch.object(win, "_start_auto_capture_worker_once"):
            for _ in range(1000):
                win._on_auto_capture_tick()
                win._emit_heartbeat()
        self.assertIsNone(win._execute_thread)
        win.close()


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class ExecuteButtonRemovalTest(unittest.TestCase):
    """アプリ側「実行」ボタン削除の検証（要件1/9）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_execute_button_not_in_ui(self) -> None:
        # 「実行」ボタンが画面上に存在しない（生成もされない）。
        win = self._make_capture()
        self.assertFalse(hasattr(win, "_execute_button"))
        labels = {b.text() for b in win.findChildren(QPushButton)}
        self.assertNotIn("実行", labels)
        object_names = {b.objectName() for b in win.findChildren(QPushButton)}
        self.assertNotIn("executeButton", object_names)

    def test_execute_button_handler_removed(self) -> None:
        # 実行ボタンの clicked 系ハンドラが存在しない。
        win = self._make_capture()
        self.assertFalse(hasattr(win, "_on_execute_button_clicked"))

    def test_app_f12_shortcut_does_not_save(self) -> None:
        # アプリ側F12キー押下（QKeyEvent）では保存処理が起動しない（要件1/9）。
        from app import captured_orders
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        win = self._make_capture()
        win._order_input.setText("1409999")
        with mock.patch.object(win, "_save_order_no", side_effect=AssertionError("app F12で保存してはならない")):
            event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F12, Qt.KeyboardModifier.NoModifier)
            win.keyPressEvent(event)
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_auto_save_hint_visible(self) -> None:
        # 自動保存ONの意味が分かる案内文が表示される。
        win = self._make_capture()
        self.assertTrue(hasattr(win, "_auto_save_hint"))
        self.assertIn("TKS", win._auto_save_hint.text())


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CaptureHelperTest(unittest.TestCase):
    """受注No取得helper（別プロセス化）の検証（要件2〜7）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        from app import tks_order_capture_window as mod

        mod.reset_capture_helper_command_cache()
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_helper_module_returns_json(self) -> None:
        # helper が JSON 1件を返す（非Windowsでは window_not_found）。
        from app import tks_order_capture_helper as helper

        result = helper.run_capture()
        self.assertIn("ok", result)
        self.assertIn("screen_type", result)
        self.assertIn("order_no", result)
        self.assertIn("reason", result)
        self.assertIn("elapsed_ms", result)

    def test_helper_dev_command_resolved(self) -> None:
        # 開発環境では python -m app.tks_order_capture_helper のコマンドが解決される（要件5）。
        from app import tks_order_capture_window as mod

        mod.reset_capture_helper_command_cache()
        with mock.patch.object(sys, "frozen", False, create=True):
            command = mod._resolve_capture_helper_command()
        self.assertIsNotNone(command)
        self.assertEqual(command[:2], [sys.executable, "-m"])
        self.assertEqual(command[-1], "app.tks_order_capture_helper")

    def test_helper_frozen_command_resolved(self) -> None:
        # frozen環境では <exe> --tks-order-capture-helper が解決される（要件5）。
        from app import tks_order_capture_window as mod

        mod.reset_capture_helper_command_cache()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", "C:/app/TksToKintone.exe"
        ):
            command = mod._resolve_capture_helper_command()
        self.assertEqual(command, ["C:/app/TksToKintone.exe", "--tks-order-capture-helper"])

    def test_helper_header_result_reflected(self) -> None:
        # helperがheader/order_noを返したらUIへ反映する（要件6・テスト5）。
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
        self.assertEqual(win._latest_label.text(), "1394149")
        self.assertEqual(win._last_header_order_no, "1394149")

    def test_helper_detail_saves_last_header(self) -> None:
        # helperがdetailを返したら自動保存ON時に直前headerの受注Noを保存する（テスト6）。
        from app import captured_orders

        win = self._make_capture()
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394149"]
        )

    def test_backoff_applied_after_consecutive_failures_and_reset(self) -> None:
        # 連続失敗でバックオフ適用、成功でリセットする（要件7・テスト12）。
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            for _ in range(mod._CAPTURE_BACKOFF_FAILURE_THRESHOLD):
                win._on_auto_capture_worker_finished("", "timeout", 10.0, "unknown", win._generation)
            self.assertTrue(win._capture_backoff_active)
            self.assertEqual(
                win._current_auto_capture_interval(), mod._CAPTURE_BACKOFF_INTERVAL_MS
            )
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            self.assertFalse(win._capture_backoff_active)
            self.assertEqual(win._capture_consecutive_failures, 0)

    def test_tick_skipped_while_helper_running(self) -> None:
        # helper実行中tickでは多重起動しない（要件7・テスト10）。
        win = self._make_capture()
        win._capture_worker_running = True
        with mock.patch.object(win, "isVisible", return_value=True), mock.patch.object(
            win, "_start_auto_capture_worker_once"
        ) as start_worker:
            win._on_auto_capture_tick()
        start_worker.assert_not_called()
        self.assertTrue(win._capture_rerun_requested)

    def test_close_after_helper_result_discards(self) -> None:
        # close/hide後にhelper結果が戻っても破棄する（要件7・テスト11）。
        win = self._make_capture()
        gen_before = win._generation
        win._closing = True
        win._generation += 1
        win._on_auto_capture_worker_finished("1409999", "", 10.0, "header", gen_before)
        self.assertEqual(win._latest_label.text(), "-")

    def test_heartbeat_does_not_spawn_subprocess(self) -> None:
        # heartbeat はUIA/Win32/helper(subprocess)を呼ばない（要件6/8・テスト14）。
        win = self._make_capture()
        with mock.patch(
            "subprocess.Popen", side_effect=AssertionError("heartbeatでsubprocessを起動してはならない")
        ), mock.patch(
            "app.tks_order_capture_window.capture_order_no_from_tkscloud8",
            side_effect=AssertionError("heartbeatでUIA探索はしない"),
        ):
            for _ in range(5):
                win._emit_heartbeat()

    def test_qt_message_handler_suppresses_warnings_in_normal_run(self) -> None:
        # 通常運用（debug OFF）では QtWarningMsg を運用ログへ大量に出さない（要件8・テスト15）。
        import logging as _logging

        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        from app import tks_order_capture_window as mod

        settings = QSettings("Manekiya", "TksToKintone")
        settings.setValue("ui/debug_visible", "0")
        settings.sync()
        mod.reset_debug_cache()
        # クラッシュ追跡は一度しかインストールされないため、状態を戻して再インストールする。
        mod._CRASH_TRACKING_INSTALLED = False
        handler_holder = {}

        def _capture_handler(fn):
            handler_holder["fn"] = fn

        with mock.patch("PySide6.QtCore.qInstallMessageHandler", side_effect=_capture_handler):
            mod._install_crash_tracking()
        handler = handler_holder.get("fn")
        self.assertIsNotNone(handler)
        with self.assertLogs("tks_to_kintone_app", level="WARNING") as cm:
            _logging.getLogger("tks_to_kintone_app").warning("keepalive")
            for _ in range(100):
                handler(QtMsgType.QtWarningMsg, None, "spammy qt warning")
        warnings = [m for m in cm.output if "spammy qt warning" in m]
        self.assertEqual(warnings, [])


class _FakeSignal:
    """QProcess シグナルの代役。connect した callback を emit で呼ぶ。"""

    def __init__(self) -> None:
        self._cb = None

    def connect(self, cb) -> None:
        self._cb = cb

    def disconnect(self, *args) -> None:
        self._cb = None

    def emit(self, *args) -> None:
        if self._cb is not None:
            self._cb(*args)


class FakeQProcess:
    """QProcess の代役。start を記録し、stdout/finished をテストから駆動できる。"""

    instances: list = []

    def __init__(self, parent=None) -> None:
        self._stdout = b""
        self._stderr = b""
        self._state = 2  # QProcess.Running 相当
        self.started_args = None
        self.killed = False
        self.readyReadStandardOutput = _FakeSignal()
        self.readyReadStandardError = _FakeSignal()
        self.finished = _FakeSignal()
        self.errorOccurred = _FakeSignal()
        FakeQProcess.instances.append(self)

    def start(self, program, args) -> None:
        self.started_args = (program, list(args))

    def readAllStandardOutput(self):
        data, self._stdout = self._stdout, b""
        return data

    def readAllStandardError(self):
        data, self._stderr = self._stderr, b""
        return data

    def state(self):
        return self._state

    def kill(self) -> None:
        self.killed = True
        self._state = 0

    def waitForFinished(self, ms=0):
        self._state = 0
        return True

    def deleteLater(self) -> None:
        pass

    # ── テスト駆動ヘルパ ─────────────────────────────────────────────
    def feed_stdout(self, payload: bytes) -> None:
        self._stdout = payload
        self.readyReadStandardOutput.emit()

    def emit_finished(self, exit_code: int = 0) -> None:
        self._state = 0
        self.finished.emit(exit_code, 0)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class CaptureProcessLifecycleTest(unittest.TestCase):
    """受注No取得を QProcess（QThread廃止）で回すことの検証（要件2/3）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()
        FakeQProcess.instances = []

    def tearDown(self) -> None:
        from app import tks_order_capture_window as mod

        mod.reset_capture_helper_command_cache()
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def _start(self, win, *, source="auto"):
        """QProcess を FakeQProcess に差し替えて helper 起動経路を回す。"""
        from app import tks_order_capture_window as mod

        with mock.patch.object(win, "isVisible", return_value=True), mock.patch.object(
            mod, "QProcess", FakeQProcess
        ), mock.patch.object(
            mod, "_resolve_capture_helper_command", return_value=["dummy", "helper"]
        ):
            win._start_capture_process_once(source=source)
        return FakeQProcess.instances[-1] if FakeQProcess.instances else None

    def test_helper_launched_via_qprocess_not_qthread(self) -> None:
        # helper 起動は QProcess で行い、QThread は生成しない（要件2）。
        win = self._make_capture()
        proc = self._start(win)
        self.assertIsNotNone(proc)
        self.assertEqual(proc.started_args[0], "dummy")
        self.assertIn("helper", proc.started_args[1])
        self.assertIsNone(win._capture_thread)
        self.assertTrue(win._capture_process_running)

    def test_valid_json_reflects_order_no(self) -> None:
        # 正常JSONで受注Noが反映され、フラグが解除される（要件2）。
        win = self._make_capture()
        proc = self._start(win)
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.feed_stdout(payload)
            proc.emit_finished(0)
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertFalse(win._capture_process_running)
        self.assertIsNone(win._capture_process)

    def test_returncode_nonzero_releases_flag(self) -> None:
        # 非0 returncode でも落ちず flag 解除される（要件2/4）。
        win = self._make_capture()
        proc = self._start(win)
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.emit_finished(3)
        self.assertFalse(win._capture_process_running)

    def test_invalid_json_releases_flag(self) -> None:
        # 不正JSONでも落ちず flag 解除される（要件4）。
        win = self._make_capture()
        proc = self._start(win)
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.feed_stdout(b"not json at all")
            proc.emit_finished(0)
        self.assertFalse(win._capture_process_running)

    def test_timeout_kills_and_cleans_up(self) -> None:
        # timeout で kill/cleanup/flag解除される（要件4/7）。
        win = self._make_capture()
        proc = self._start(win)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_capture_process_timeout()
        self.assertTrue(proc.killed)
        self.assertFalse(win._capture_process_running)
        self.assertIsNone(win._capture_process)

    def test_window_not_found_keeps_timer_and_retries(self) -> None:
        # window_not_found を10回連続させてもアプリ状態は継続する（要件3）。
        win = self._make_capture()
        win.show()
        payload = json.dumps(
            {"ok": False, "screen_type": "none", "order_no": "", "reason": "window_not_found"}
        ).encode("utf-8")
        for _ in range(10):
            proc = self._start(win)
            with mock.patch.object(win, "isVisible", return_value=True):
                proc.feed_stdout(payload)
                proc.emit_finished(0)
        self.assertFalse(win._capture_process_running)
        # timer は生きている（close していない）。
        self.assertIsNotNone(win._auto_capture_timer)
        win.close()

    def test_five_failures_sets_unavailable_and_continues(self) -> None:
        # 5回目の失敗で取得不可表示、6回目以降も落ちず再試行できる（要件3）。
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            for _ in range(4):
                win._on_worker_capture_failed()
            self.assertNotEqual(win._status_label.text(), "取得不可")
            win._on_worker_capture_failed()
            self.assertEqual(win._status_label.text(), "取得不可")
            # 6回目以降も例外なく継続。
            for _ in range(5):
                win._on_worker_capture_failed()
            self.assertEqual(win._status_label.text(), "取得不可")

    def test_capture_success_resets_failure_count(self) -> None:
        # 取得成功したら failure count が reset される（要件3）。
        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True):
            for _ in range(5):
                win._on_worker_capture_failed()
            self.assertEqual(win._auto_capture_failures, 5)
            win._on_worker_captured("1409999")
        self.assertEqual(win._auto_capture_failures, 0)

    def test_finished_after_close_is_discarded(self) -> None:
        # close/hide中に process finished が来ても結果を破棄し落ちない（要件2）。
        win = self._make_capture()
        proc = self._start(win)
        # close 相当: worker を停止（結果破棄フラグを立てる）。
        win._closing = True
        win._stop_auto_capture_worker()
        self.assertFalse(win._capture_process_running)
        # 破棄後に finished が来ても latest は更新されない。
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        proc.feed_stdout(payload)
        proc.emit_finished(0)
        self.assertEqual(win._latest_label.text(), "-")

    def test_helper_unavailable_does_not_start_process(self) -> None:
        # helper 未解決なら QProcess を起動せず取得不可扱いにする（要件4/5）。
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        with mock.patch.object(win, "isVisible", return_value=True), mock.patch.object(
            mod, "QProcess", FakeQProcess
        ), mock.patch.object(mod, "_resolve_capture_helper_command", return_value=None):
            win._start_capture_process_once(source="auto")
        self.assertEqual(FakeQProcess.instances, [])
        self.assertFalse(win._capture_process_running)

    def test_many_ticks_do_not_create_qthreads(self) -> None:
        # 1000tick相当を回しても QThread は生成されない（要件2）。
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        win.show()
        payload = json.dumps(
            {"ok": False, "screen_type": "none", "order_no": "", "reason": "window_not_found"}
        ).encode("utf-8")
        with mock.patch.object(mod.QThread, "start", side_effect=AssertionError("QThreadを使ってはならない")):
            for _ in range(1000):
                proc = self._start(win)
                with mock.patch.object(win, "isVisible", return_value=True):
                    proc.feed_stdout(payload)
                    proc.emit_finished(0)
        self.assertIsNone(win._capture_thread)
        win.close()

    # ── 要件1: source ごとの timeout ────────────────────────────────────────
    def test_auto_timeout_is_around_5000ms(self) -> None:
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        self._start(win, source="auto")
        self.assertEqual(win._capture_timeout_timer.interval(), mod._CAPTURE_HELPER_AUTO_TIMEOUT_MS)
        self.assertGreaterEqual(win._capture_timeout_timer.interval(), 5000)
        # 2秒ではkillされない設定であること。
        self.assertGreater(win._capture_timeout_timer.interval(), 2000)

    def test_manual_timeout_is_longer_than_auto(self) -> None:
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        self._start(win, source="manual")
        self.assertEqual(win._capture_timeout_timer.interval(), mod._CAPTURE_HELPER_MANUAL_TIMEOUT_MS)
        self.assertGreater(
            mod._CAPTURE_HELPER_MANUAL_TIMEOUT_MS, mod._CAPTURE_HELPER_AUTO_TIMEOUT_MS
        )

    def test_helper_returning_in_3s_is_success_not_timeout(self) -> None:
        # helperが3秒相当で返れば timeout せず成功扱い（timeout>3s設定の検証）。
        from app import tks_order_capture_window as mod

        for source in ("auto", "manual"):
            win = self._make_capture()
            proc = self._start(win, source=source)
            timeout = win._capture_timeout_timer.interval()
            self.assertGreater(timeout, 3000, f"{source} timeout must exceed 3s")
            payload = json.dumps(
                {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
            ).encode("utf-8")
            with mock.patch.object(win, "isVisible", return_value=True):
                proc.feed_stdout(payload)
                proc.emit_finished(0)
            self.assertEqual(win._latest_order_no, "1409999")
            self.assertFalse(win._capture_process_running)

    # ── 要件2: stdout/stderr 回収ログ ───────────────────────────────────────
    def test_finished_collects_stdout_even_without_readyread(self) -> None:
        # readyRead が発火しなくても finished時に stdout を回収して parse する。
        win = self._make_capture()
        proc = self._start(win)
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        # feed_stdout を呼ばず、finished時に proc に stdout を仕込む。
        proc._stdout = payload
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.emit_finished(0)
        self.assertEqual(win._latest_order_no, "1409999")

    def test_empty_stdout_logs_helper_empty_stdout(self) -> None:
        win = self._make_capture()
        proc = self._start(win)
        with mock.patch.object(win, "_log_order_event") as log, mock.patch.object(
            win, "isVisible", return_value=True
        ):
            proc.emit_finished(0)  # stdout 空
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("order_import_capture_helper_empty_stdout", events)

    def test_valid_json_logs_result_summary(self) -> None:
        win = self._make_capture()
        proc = self._start(win)
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        with mock.patch.object(win, "_log_order_event") as log, mock.patch.object(
            win, "isVisible", return_value=True
        ):
            proc.feed_stdout(payload)
            proc.emit_finished(0)
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("order_import_capture_helper_result_summary", events)
        self.assertIn("order_import_capture_helper_json_parsed", events)

    # ── 要件3: 自動取得timerを止めない/復旧 ─────────────────────────────────
    def test_timer_stays_active_after_timeout(self) -> None:
        win = self._make_capture()
        win.show()
        proc = self._start(win, source="auto")
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_capture_process_timeout()
        self.assertTrue(win._auto_capture_timer.isActive())
        win.close()

    def test_timer_stays_active_after_window_not_found_burst(self) -> None:
        win = self._make_capture()
        win.show()
        payload = json.dumps(
            {"ok": False, "screen_type": "none", "order_no": "", "reason": "window_not_found"}
        ).encode("utf-8")
        for _ in range(10):
            proc = self._start(win, source="auto")
            with mock.patch.object(win, "isVisible", return_value=True):
                proc.feed_stdout(payload)
                proc.emit_finished(0)
            self.assertTrue(win._auto_capture_timer.isActive())
        win.close()

    def test_heartbeat_recovers_inactive_timer_while_visible(self) -> None:
        win = self._make_capture()
        win.show()
        # 何らかの原因で timer が止まった状況を再現する。
        win._auto_capture_timer.stop()
        self.assertFalse(win._auto_capture_timer.isActive())
        with mock.patch.object(win, "isVisible", return_value=True):
            win._emit_heartbeat()
        self.assertTrue(win._auto_capture_timer.isActive())
        win.close()

    def test_timer_only_stops_on_hide_or_close(self) -> None:
        win = self._make_capture()
        win.show()
        self.assertTrue(win._auto_capture_timer.isActive())
        win.hide()
        self.assertFalse(win._auto_capture_timer.isActive())

    # ── 要件4: 手動取得 ─────────────────────────────────────────────────────
    def test_manual_timeout_shows_failure_reason(self) -> None:
        win = self._make_capture()
        win.show()
        proc = self._start(win, source="manual")
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_capture_process_timeout()
        self.assertIn("timeout", win._status_label.toolTip())
        self.assertFalse(win._capture_process_running)
        win.close()

    def test_manual_success_resets_failure_count(self) -> None:
        win = self._make_capture()
        win.show()
        win._auto_capture_failures = 4
        proc = self._start(win, source="manual")
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.feed_stdout(payload)
            proc.emit_finished(0)
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertEqual(win._auto_capture_failures, 0)
        win.close()

    def test_manual_completion_keeps_auto_timer_active(self) -> None:
        win = self._make_capture()
        win.show()
        proc = self._start(win, source="manual")
        payload = json.dumps(
            {"ok": True, "screen_type": "header", "order_no": "1409999", "reason": "ok"}
        ).encode("utf-8")
        with mock.patch.object(win, "isVisible", return_value=True):
            proc.feed_stdout(payload)
            proc.emit_finished(0)
        self.assertTrue(win._auto_capture_timer.isActive())
        win.close()

    # ── 要件5: 間隔 ─────────────────────────────────────────────────────────
    def test_success_interval_returns_to_normal(self) -> None:
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        win._last_screen_type = "none"
        win._capture_backoff_active = False
        self.assertEqual(win._current_auto_capture_interval(), mod._AUTO_CAPTURE_NORMAL_INTERVAL_MS)
        self.assertLessEqual(mod._AUTO_CAPTURE_NORMAL_INTERVAL_MS, 1000)

    def test_backoff_interval_capped_and_not_permanent(self) -> None:
        from app import tks_order_capture_window as mod

        win = self._make_capture()
        win._capture_backoff_active = True
        interval = win._current_auto_capture_interval()
        self.assertGreaterEqual(interval, 2000)
        self.assertLessEqual(interval, 3000)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class AutoCaptureAutoSaveDecoupleTest(unittest.TestCase):
    """自動取得と自動保存の分離（要件2）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self._tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        settings = QSettings("Manekiya", "TksToKintone")
        settings.clear()
        settings.sync()

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home

    def _make_capture(self):
        from app.tks_order_capture_window import TksOrderCaptureWindow

        win = TksOrderCaptureWindow(voucher_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
        return win

    def test_scheduler_runs_when_auto_capture_off_auto_save_on(self) -> None:
        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(True)
        win.show()
        self.assertTrue(win._auto_capture_scheduler_needed())
        self.assertIsNotNone(win._auto_capture_timer)
        self.assertTrue(win._auto_capture_timer.isActive())
        win.close()

    def test_scheduler_stops_when_both_off(self) -> None:
        win = self._make_capture()
        win.show()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(False)
        self.assertFalse(win._auto_capture_scheduler_needed())
        if win._auto_capture_timer is not None:
            self.assertFalse(win._auto_capture_timer.isActive())
        win.close()

    def test_header_to_detail_saves_with_auto_capture_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(
            [o["order_no"] for o in captured_orders.load_captured_orders()], ["1394149"]
        )

    def test_ui_not_updated_but_internal_held_when_auto_capture_off(self) -> None:
        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        win._auto_save_check.setChecked(True)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
        # UI表示（最新ラベル）は更新しない。
        self.assertEqual(win._latest_label.text(), "-")
        # 内部の見出受注Noは保持する。
        self.assertEqual(win._last_header_order_no, "1394149")

    def test_display_updates_but_no_save_when_auto_save_off(self) -> None:
        from app import captured_orders

        win = self._make_capture()
        win._auto_capture_check.setChecked(True)
        win._auto_save_check.setChecked(False)
        with mock.patch.object(win, "isVisible", return_value=True):
            win._on_auto_capture_worker_finished("1394149", "", 10.0, "header", win._generation)
            win._on_auto_capture_worker_finished("", "", 10.0, "detail", win._generation)
        self.assertEqual(win._latest_label.text(), "1394149")
        self.assertEqual(captured_orders.load_captured_orders(), [])

    def test_manual_capture_works_when_auto_capture_off(self) -> None:
        win = self._make_capture()
        win._auto_capture_check.setChecked(False)
        with mock.patch(
            "app.tks_order_capture_window.run_capture_via_helper",
            return_value={
                "order_no": "1409999",
                "screen_type": "header",
                "error": "",
                "reason": "ok",
                "elapsed_ms": 10.0,
            },
        ):
            win._on_capture()
        self.assertEqual(win._latest_order_no, "1409999")
        self.assertEqual(win._status_label.text(), "取得OK")


if __name__ == "__main__":
    unittest.main()
