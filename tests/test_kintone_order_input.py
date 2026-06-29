"""Kintone登録処理画面（MainWindow）の受注No入力欄に関するテスト。

- 外部から受注Noを追記する add_order_no の挙動
- ユーザー表示が「伝票番号」ではなく「受注No」になっていること
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtCore import QEvent

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False

GUI_SOURCE = Path("app/gui.py").read_text(encoding="utf-8")


class KintoneLabelStaticTest(unittest.TestCase):
    """ソース上のユーザー表示文言が「受注No」に統一されていること。"""

    def test_input_label_is_order_no(self) -> None:
        self.assertIn('form.addRow("受注No", self.denpyo_numbers)', GUI_SOURCE)
        self.assertNotIn('form.addRow("伝票番号"', GUI_SOURCE)

    def test_validation_message_uses_order_no(self) -> None:
        self.assertIn("受注Noを1件以上入力してください。", GUI_SOURCE)
        self.assertNotIn("伝票番号を1件以上入力してください。", GUI_SOURCE)
        self.assertNotIn("OLAPログインIDを入力してください。", GUI_SOURCE)
        self.assertNotIn("OLAPパスワードを入力してください。", GUI_SOURCE)

    def test_olap_account_labels_are_not_on_kintone_screen(self) -> None:
        layout_source = GUI_SOURCE[
            GUI_SOURCE.index("def _build_layout") : GUI_SOURCE.index("def closeEvent", GUI_SOURCE.index("def _build_layout"))
        ]
        self.assertNotIn("契約会社コード", layout_source)
        self.assertNotIn("OLAPログインID", layout_source)
        self.assertNotIn("OLAPパスワード", layout_source)

    def test_no_user_facing_denpyo_number_label_remains(self) -> None:
        # ログ・ラベル等のユーザー表示文言から「伝票番号」が消えていること。
        self.assertNotIn("伝票番号", GUI_SOURCE)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class AddOrderNoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from app.gui import MainWindow

        # _load_config は設定ファイル/ネットワークに依存し、未整備環境では
        # モーダルダイアログを出しうるため、テストではスキップする。
        with mock.patch.object(MainWindow, "_load_config", lambda self: None):
            win = MainWindow()
        self.addCleanup(win.deleteLater)
        return win

    def test_add_order_no_to_empty_input(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("")
        win.add_order_no("1405113")
        self.assertEqual(win.denpyo_numbers.toPlainText(), "1405113")

    def test_add_order_no_appends_without_clearing(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("14050001")
        win.add_order_no("1405113")
        self.assertEqual(win.denpyo_numbers.toPlainText(), "14050001\n1405113")

    def test_add_order_no_strips_whitespace(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("14050001\n")
        win.add_order_no("  1405113  ")
        self.assertEqual(win.denpyo_numbers.toPlainText(), "14050001\n1405113")

    def test_add_empty_order_no_is_noop(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("14050001")
        win.add_order_no("")
        win.add_order_no("   ")
        self.assertEqual(win.denpyo_numbers.toPlainText(), "14050001")

    def test_input_label_text_is_order_no(self) -> None:
        from PySide6.QtWidgets import QLabel

        win = self._make_window()
        labels = [w.text() for w in win.findChildren(QLabel)]
        self.assertIn("受注No", labels)
        self.assertNotIn("伝票番号", labels)

    # ── 受注No一覧の取得・区切り解析（要件4・5）─────────────────────────────────
    def test_get_order_numbers_splits_various_separators(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405001\n1405113, 1405222\n１４０５３３３　1405444")
        self.assertEqual(
            win.get_order_numbers(),
            {"1405001", "1405113", "1405222", "１４０５３３３", "1405444"},
        )

    def test_get_order_numbers_preserves_leading_zero(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("0014050\n005")
        self.assertEqual(win.get_order_numbers(), {"0014050", "005"})

    def test_get_order_numbers_empty(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("   \n  ")
        self.assertEqual(win.get_order_numbers(), set())

    def test_remove_successful_order_numbers_keeps_failed_only(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1402088\n1405113\n1409999")
        win.remove_order_numbers(["1402088", "1405113"])
        self.assertEqual(win.denpyo_numbers.toPlainText(), "1409999")

    def test_remove_successful_order_numbers_all_success_clears_input(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("0014050, 0014051")
        win.remove_order_numbers(["0014050", "0014051"])
        self.assertEqual(win.denpyo_numbers.toPlainText(), "")

    def test_registration_completed_signal_removes_input_and_reemits(self) -> None:
        win = self._make_window()
        fired = []
        win.denpyo_numbers.setPlainText("1402088\n1409999")
        win.kintone_registration_completed.connect(lambda values: fired.append(values))
        win._on_kintone_registration_completed(["1402088"])
        self.assertEqual(win.denpyo_numbers.toPlainText(), "1409999")
        self.assertEqual(fired, [["1402088"]])

    def test_order_numbers_changed_signal_emitted_on_text_change(self) -> None:
        win = self._make_window()
        fired = []
        win.order_numbers_changed.connect(lambda: fired.append(True))
        win.denpyo_numbers.setPlainText("1405113")
        self.assertTrue(fired)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class OrderOverridesTest(unittest.TestCase):
    """受注Noごとの override 保持・取得・破棄（要件1・4・5）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, olap_id: str = "id", olap_password: str = "pw"):
        from app.gui import MainWindow

        with mock.patch.object(MainWindow, "_load_config", lambda self: None):
            win = MainWindow(initial_olap_id=olap_id, initial_olap_password=olap_password)
        self.addCleanup(win.deleteLater)
        return win

    def test_add_order_no_stores_override(self) -> None:
        from datetime import date

        win = self._make_window()
        win.add_order_no("1405113", finish_date=date(2026, 6, 26), am_pm="PM")
        overrides = win.get_order_overrides()
        self.assertEqual(overrides["1405113"]["finish_date"], date(2026, 6, 26))
        self.assertEqual(overrides["1405113"]["am_pm"], "PM")

    def test_override_stores_none_values(self) -> None:
        win = self._make_window()
        win.add_order_no("1405113", finish_date=None, am_pm="none")
        overrides = win.get_order_overrides()
        self.assertIsNone(overrides["1405113"]["finish_date"])
        self.assertEqual(overrides["1405113"]["am_pm"], "none")

    def test_duplicate_add_does_not_duplicate_text_but_updates_override(self) -> None:
        from datetime import date

        win = self._make_window()
        win.add_order_no("1405113", finish_date=date(2026, 6, 26), am_pm="AM")
        win.add_order_no("1405113", finish_date=date(2026, 7, 1), am_pm="PM")
        # 受注No欄は重複しない。
        self.assertEqual(win.denpyo_numbers.toPlainText(), "1405113")
        # override は最新値に更新される。
        self.assertEqual(win.get_order_overrides()["1405113"]["am_pm"], "PM")

    def test_override_pruned_when_order_removed_from_input(self) -> None:
        from datetime import date

        win = self._make_window()
        win.add_order_no("1405113", finish_date=date(2026, 6, 26), am_pm="PM")
        win.add_order_no("1405999", finish_date=date(2026, 6, 27), am_pm="AM")
        # 1405113 を入力欄から削除する。
        win.denpyo_numbers.setPlainText("1405999")
        overrides = win.get_order_overrides()
        self.assertNotIn("1405113", overrides)
        self.assertIn("1405999", overrides)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class ApplyOrderOverridesTest(unittest.TestCase):
    """apply_order_overrides が登録前確認用データへ override を適用する（要件5）。"""

    def _rows(self):
        return [
            {"受注No": "1405113", "仕上日": "2026-01-01", "出荷区分": "AM"},
            {"受注No": "1405113", "仕上日": "2026-01-01", "出荷区分": "AM"},  # 同一受注No
            {"受注No": "1405999", "仕上日": "2026-01-01", "出荷区分": "AM"},  # 別受注No
        ]

    def test_applies_finish_date_and_am_pm(self) -> None:
        from datetime import date

        from app.gui import apply_order_overrides

        overrides = {"1405113": {"finish_date": date(2026, 6, 26), "am_pm": "PM"}}
        result = apply_order_overrides(self._rows(), overrides)
        self.assertEqual(result[0]["仕上日"], "2026-06-26")
        self.assertEqual(result[0]["出荷区分"], "PM")

    def test_applies_to_all_rows_of_same_order(self) -> None:
        from datetime import date

        from app.gui import apply_order_overrides

        overrides = {"1405113": {"finish_date": date(2026, 6, 26), "am_pm": "PM"}}
        result = apply_order_overrides(self._rows(), overrides)
        # 同一受注Noの2行とも反映される。
        self.assertEqual(result[1]["仕上日"], "2026-06-26")
        self.assertEqual(result[1]["出荷区分"], "PM")

    def test_does_not_touch_other_orders(self) -> None:
        from datetime import date

        from app.gui import apply_order_overrides

        overrides = {"1405113": {"finish_date": date(2026, 6, 26), "am_pm": "PM"}}
        result = apply_order_overrides(self._rows(), overrides)
        # 別受注Noは元の値（Kintone登録処理画面の既定値）を維持。
        self.assertEqual(result[2]["仕上日"], "2026-01-01")
        self.assertEqual(result[2]["出荷区分"], "AM")

    def test_none_override_results_in_blank(self) -> None:
        from app.gui import apply_order_overrides

        overrides = {"1405113": {"finish_date": None, "am_pm": "none"}}
        result = apply_order_overrides(self._rows(), overrides)
        self.assertEqual(result[0]["仕上日"], "")
        self.assertEqual(result[0]["出荷区分"], "")

    def test_no_override_keeps_original(self) -> None:
        from app.gui import apply_order_overrides

        result = apply_order_overrides(self._rows(), {})
        self.assertEqual(result[0]["仕上日"], "2026-01-01")
        self.assertEqual(result[0]["出荷区分"], "AM")


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class MainWindowNoneInputTest(unittest.TestCase):
    """Kintone登録処理画面の仕上日／出荷区分「なし」選択（要件3）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, olap_id: str = "id", olap_password: str = "pw"):
        from app.gui import MainWindow

        with mock.patch.object(MainWindow, "_load_config", lambda self: None):
            win = MainWindow(initial_olap_id=olap_id, initial_olap_password=olap_password)
        self.addCleanup(win.deleteLater)
        return win

    def test_shiage_none_checkbox_exists_and_disables_date(self) -> None:
        win = self._make_window()
        self.assertFalse(win.shiage_date.isEnabled() is False)  # 既定は有効
        win.shiage_none.setChecked(True)
        self.assertFalse(win.shiage_date.isEnabled())

    def test_collect_input_shiage_none_is_blank(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405113")
        win.shiage_none.setChecked(True)
        run_input = win._collect_input(require_denpyo=True)
        self.assertEqual(run_input.shiage_date, "")

    def test_shukka_has_none_option(self) -> None:
        from app.gui import SHUKKA_NONE_LABEL

        win = self._make_window()
        # _load_config をスキップしているため手動で選択肢を構成する。
        win.shukka_kbn.clear()
        win.shukka_kbn.addItem(SHUKKA_NONE_LABEL)
        win.shukka_kbn.addItems(["AM", "PM"])
        items = [win.shukka_kbn.itemText(i) for i in range(win.shukka_kbn.count())]
        self.assertIn(SHUKKA_NONE_LABEL, items)

    def test_collect_input_shukka_none_is_blank(self) -> None:
        from app.gui import SHUKKA_NONE_LABEL

        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405113")
        win.shukka_kbn.clear()
        win.shukka_kbn.addItem(SHUKKA_NONE_LABEL)
        win.shukka_kbn.addItems(["AM", "PM"])
        win.shukka_kbn.setCurrentText(SHUKKA_NONE_LABEL)
        run_input = win._collect_input(require_denpyo=True)
        self.assertEqual(run_input.shukka_kbn, "")

    def test_collect_input_uses_launcher_olap_credentials(self) -> None:
        win = self._make_window(olap_id="launcher-id", olap_password="launcher-pw")
        win.denpyo_numbers.setPlainText("1405113")
        run_input = win._collect_input(require_denpyo=True)
        self.assertEqual(run_input.olap_login_id, "launcher-id")
        self.assertEqual(run_input.olap_password, "launcher-pw")

    def test_collect_input_does_not_require_olap_credentials(self) -> None:
        win = self._make_window(olap_id="", olap_password="")
        win.denpyo_numbers.setPlainText("1405113")
        run_input = win._collect_input(require_denpyo=True)
        self.assertEqual(run_input.denpyo_numbers, ["1405113"])


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class RegistrationPreviewReflectTest(unittest.TestCase):
    """登録前確認に override 適用後の仕上日／出荷区分が反映される（要件1・3・5）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_dialog(self, rows, debug_visible: bool = False):
        from app.gui import RegistrationPreviewDialog

        dialog = RegistrationPreviewDialog(
            rows,
            ["AM", "PM"],
            master=[],
            customer_labels={},
            preview_color_theme="light",
            debug_visible=debug_visible,
        )
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_preview_reflects_finish_date_and_am_pm(self) -> None:
        rows = [{"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"}]
        dialog = self._make_dialog(rows)
        self.assertEqual(dialog._shiage_widgets[0].date().toString("yyyy-MM-dd"), "2026-06-26")
        self.assertEqual(dialog._shukka_widgets[0].currentText(), "PM")

    def test_preview_same_order_multiple_rows(self) -> None:
        rows = [
            {"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"},
            {"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"},
        ]
        dialog = self._make_dialog(rows)
        # 先頭行ウィジェットに反映され、登録データは全行へ伝播する。
        result = dialog.registration_rows()
        self.assertEqual(result[0]["仕上日"], "2026-06-26")
        self.assertEqual(result[1]["仕上日"], "2026-06-26")
        self.assertEqual(result[0]["出荷区分"], "PM")
        self.assertEqual(result[1]["出荷区分"], "PM")

    def test_preview_blank_when_none(self) -> None:
        rows = [{"受注No": "1405113", "仕上日": "", "出荷区分": "", "硝/加工": "1"}]
        dialog = self._make_dialog(rows)
        from app.gui import SHUKKA_NONE_LABEL

        # 仕上日は最小日付（特別表示文字＝「なし」）になり、登録値は空欄。
        self.assertEqual(dialog._shiage_widgets[0].date(),
                         dialog._shiage_widgets[0].minimumDate())
        self.assertEqual(dialog._shukka_widgets[0].currentText(), SHUKKA_NONE_LABEL)
        result = dialog.registration_rows()
        self.assertEqual(result[0]["仕上日"], "")
        self.assertEqual(result[0]["出荷区分"], "")

    def test_kintone_window_default_used_when_no_override(self) -> None:
        """override が無い場合は元の（Kintone登録処理画面の既定）値が使われる（要件2）。"""
        rows = [{"受注No": "1405113", "仕上日": "2026-02-02", "出荷区分": "AM", "硝/加工": "1"}]
        dialog = self._make_dialog(rows)
        self.assertEqual(dialog._shiage_widgets[0].date().toString("yyyy-MM-dd"), "2026-02-02")
        self.assertEqual(dialog._shukka_widgets[0].currentText(), "AM")

    def test_print_button_hidden_when_debug_visible_is_off(self) -> None:
        dialog = self._make_dialog(
            [{"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"}],
            debug_visible=False,
        )
        self.assertIsNone(dialog.print_button)

    def test_print_button_visible_when_debug_visible_is_on(self) -> None:
        dialog = self._make_dialog(
            [{"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"}],
            debug_visible=True,
        )
        self.assertIsNotNone(dialog.print_button)
        self.assertEqual(dialog.print_button.text(), "印刷")

    def test_debug_print_button_keeps_existing_print_handler(self) -> None:
        dialog = self._make_dialog(
            [{"受注No": "1405113", "仕上日": "2026-06-26", "出荷区分": "PM", "硝/加工": "1"}],
            debug_visible=True,
        )
        with mock.patch.object(dialog, "_print_slips") as handler:
            dialog.print_button.clicked.disconnect()
            dialog.print_button.clicked.connect(handler)
            dialog.print_button.click()
        handler.assert_called_once()


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class RegistrationPreviewKakouTypeTest(unittest.TestCase):
    """登録前確認の「加工種類」列を検証する（要件3・4・5・6・9）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_dialog(self, rows):
        from app.gui import RegistrationPreviewDialog

        dialog = RegistrationPreviewDialog(
            rows, ["AM", "PM"], master=[], customer_labels={},
            preview_color_theme="light",
        )
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_column_between_type_and_shiage(self) -> None:
        from app.gui import PREVIEW_ROW_HEADERS, _COL_TYPE, _COL_KAKOU_TYPE, _COL_SHIAGE

        self.assertEqual(PREVIEW_ROW_HEADERS[_COL_KAKOU_TYPE], "加工種類")
        self.assertEqual(_COL_KAKOU_TYPE, _COL_TYPE + 1)
        self.assertEqual(_COL_SHIAGE, _COL_KAKOU_TYPE + 1)

    def test_widget_is_line_edit_not_combobox(self) -> None:
        """加工種類セルが QComboBox ではなくテキスト入力（QLineEdit）であること（要件1・8）。"""
        from PySide6.QtWidgets import QComboBox, QLineEdit
        from app.gui import KakouTypeEdit

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        self.assertIsInstance(widget, KakouTypeEdit)
        self.assertIsInstance(widget, QLineEdit)
        self.assertNotIsInstance(widget, QComboBox)

    def test_editable_only_for_processing_row(self) -> None:
        rows = [
            {"受注No": "1000", "硝/加工": "1", "W寸法": "1303", "H寸法": "1061"},
            {"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"},
        ]
        dialog = self._make_dialog(rows)
        # 硝/加工=1 はウィジェットなし（編集不可・空欄）
        self.assertIsNone(dialog._kakou_type_widgets[0])
        # 硝/加工=2 はテキスト入力あり（編集可能）
        self.assertIsNotNone(dialog._kakou_type_widgets[1])

    def test_default_is_shiho(self) -> None:
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        self.assertEqual(widget.text(), "1：四方")
        self.assertEqual(widget.code(), "1")

    def test_focus_in_shows_number_only(self) -> None:
        """フォーカスイン時に「1：四方」が数値「1」になり編集しやすい（要件4・8）。"""
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        self.assertEqual(widget.text(), "1")

    def test_focus_in_selects_all(self) -> None:
        """フォーカスイン直後に数値が全選択され、そのまま上書きできる（要件1）。"""
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        self.app.processEvents()  # QTimer.singleShot(0, selectAll) を実行させる
        self.assertEqual(widget.selectedText(), "1")

    def test_click_keeps_all_selected(self) -> None:
        """クリック直後でも全選択が維持される（要件1）。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QMouseEvent

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress, QPoint(2, 2),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        widget.mousePressEvent(event)
        self.app.processEvents()
        self.assertEqual(widget.selectedText(), "1")

    def test_valid_input_converts_to_label_on_focus_out(self) -> None:
        """1〜8 入力後フォーカスアウトで正式名称表示になる（要件2・8）。"""
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        for code, label in (("1", "1：四方"), ("8", "8：短1")):
            widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
            widget.setText(code)
            widget.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
            self.assertEqual(widget.text(), label)
            self.assertEqual(widget.code(), code)

    def test_invalid_input_reverts_to_previous(self) -> None:
        """12 / abc / 空欄 は元の値に戻す（要件3・8）。"""
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        for bad in ("12", "a", ""):
            widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
            widget.setText(bad)
            widget.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
            self.assertEqual(widget.text(), "1：四方")
            self.assertEqual(widget.code(), "1")

    def test_change_reflected_in_kakou_mm(self) -> None:
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("2")  # 長2
        widget.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        result = dialog.registration_rows()
        self.assertEqual(result[0]["加工mm"], "2606")

    def test_non_processing_row_no_kakou_mm(self) -> None:
        rows = [{"受注No": "1000", "硝/加工": "1", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        result = dialog.registration_rows()
        self.assertEqual(result[0].get("加工mm", ""), "")

    def test_legend_text_contains_all_codes(self) -> None:
        """加工種類凡例文に 1=四方〜11=3方 が含まれる（要件2）。"""
        from app.gui import KAKOU_TYPE_LEGEND_TEXT

        self.assertIn("加工種類", KAKOU_TYPE_LEGEND_TEXT)
        for fragment in ("1=四方", "2=長2", "3=短2", "4=長2短1",
                         "5=長1短2", "6=長1短1", "7=長1", "8=短1",
                         "9=1方", "10=2方", "11=3方"):
            self.assertIn(fragment, KAKOU_TYPE_LEGEND_TEXT)
        for old_fragment in ("9=１方", "10=２方", "11=３方"):
            self.assertNotIn(old_fragment, KAKOU_TYPE_LEGEND_TEXT)

    def test_legend_label_shown_in_dialog(self) -> None:
        """登録前確認画面の上部に加工種類説明文が表示される（要件2）。"""
        from PySide6.QtWidgets import QLabel
        from app.gui import KAKOU_TYPE_LEGEND_TEXT

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        labels = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertIn(KAKOU_TYPE_LEGEND_TEXT, labels)

    def test_default_kakou_mm_is_shiho(self) -> None:
        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        result = dialog.registration_rows()
        self.assertEqual(result[0]["加工mm"], "4728")  # 1：四方

    # ── Tab / Enter での確定＋次セル移動（要件1〜3・5）────────────────────

    @staticmethod
    def _key_event(key):
        from PySide6.QtCore import QEvent as _QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        return QKeyEvent(_QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)

    def test_has_move_to_next_signal(self) -> None:
        """KakouTypeEdit に move_to_next_requested シグナルがある（要件3）。"""
        from app.gui import KakouTypeEdit

        self.assertTrue(hasattr(KakouTypeEdit, "move_to_next_requested"))

    def test_enter_commits_input(self) -> None:
        """加工種類セルで Enter を押すと入力が確定される（要件2・5）。"""
        from PySide6.QtCore import Qt

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("2")
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Return))
        self.assertEqual(widget.code(), "2")
        self.assertEqual(widget.text(), "2：長2")

    def test_tab_commits_input(self) -> None:
        """加工種類セルで Tab を押すと入力が確定される（要件2・5）。"""
        from PySide6.QtCore import Qt

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("3")
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Tab))
        self.assertEqual(widget.code(), "3")
        self.assertEqual(widget.text(), "3：短2")

    def test_enter_moves_to_finish_date_cell(self) -> None:
        """Enter 後に同じ行の加工種類の次セル（仕上日列）へ移動する（要件1・5）。"""
        from PySide6.QtCore import Qt
        from app.gui import _COL_SHIAGE

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("2")
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Enter))
        self.assertEqual(dialog.table.currentColumn(), _COL_SHIAGE)
        self.assertEqual(dialog.table.currentRow(), 0)

    def test_tab_moves_to_finish_date_cell(self) -> None:
        """Tab 後に同じ行の加工種類の次セル（仕上日列）へ移動する（要件1・5）。"""
        from PySide6.QtCore import Qt
        from app.gui import _COL_SHIAGE

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("4")
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Tab))
        self.assertEqual(dialog.table.currentColumn(), _COL_SHIAGE)
        self.assertEqual(dialog.table.currentRow(), 0)

    def test_invalid_input_reverts_then_moves(self) -> None:
        """不正入力でも元の値へ戻したうえで次セルへ移動する（要件2・5）。"""
        from PySide6.QtCore import Qt
        from app.gui import _COL_SHIAGE

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("12")  # 範囲外
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Enter))
        # 元の値（1：四方）へ戻る
        self.assertEqual(widget.code(), "1")
        self.assertEqual(widget.text(), "1：四方")
        # それでも次セルへ移動する
        self.assertEqual(dialog.table.currentColumn(), _COL_SHIAGE)

    def test_move_targets_correct_row(self) -> None:
        """複数行で、押下した行の仕上日セルへ移動する（要件1）。"""
        from PySide6.QtCore import Qt
        from app.gui import _COL_SHIAGE

        rows = [
            {"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"},
            {"受注No": "2000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"},
        ]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[1]
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.setText("2")
        widget.keyPressEvent(self._key_event(Qt.Key.Key_Tab))
        self.assertEqual(dialog.table.currentRow(), 1)
        self.assertEqual(dialog.table.currentColumn(), _COL_SHIAGE)

    def test_other_keys_do_not_emit_move(self) -> None:
        """数字など他キーでは移動シグナルを出さず通常入力のまま（既存挙動を壊さない）。"""
        from PySide6.QtCore import Qt

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        widget = dialog._kakou_type_widgets[0]
        emitted = []
        widget.move_to_next_requested.connect(lambda: emitted.append(True))
        widget.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        widget.keyPressEvent(self._key_event(Qt.Key.Key_5))
        self.assertEqual(emitted, [])

    def test_legend_font_matches_sibling_explanation(self) -> None:
        """加工種類説明文のフォントサイズが既存説明文と一致する（要件4）。"""
        from PySide6.QtWidgets import QLabel
        from app.gui import KAKOU_TYPE_LEGEND_TEXT

        rows = [{"受注No": "1000", "硝/加工": "2", "W寸法": "1303", "H寸法": "1061"}]
        dialog = self._make_dialog(rows)
        legend = None
        sibling = None
        for w in dialog.findChildren(QLabel):
            if w.text() == KAKOU_TYPE_LEGEND_TEXT:
                legend = w
            elif "受注No先頭行にのみ表示" in w.text():
                sibling = w
        self.assertIsNotNone(legend)
        self.assertIsNotNone(sibling)
        # 専用の小さいフォント指定（font-size）が外れている
        self.assertNotIn("font-size", legend.styleSheet())
        # 実フォントサイズが既存説明文と一致する
        self.assertEqual(legend.font().pointSizeF(), sibling.font().pointSizeF())
        self.assertEqual(legend.font().pixelSize(), sibling.font().pixelSize())


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class ParseOrderNumbersTest(unittest.TestCase):
    """parse_order_numbers の区切り解析（要件4）。"""

    def test_parse_full_width_comma_and_space(self) -> None:
        from app.gui import parse_order_numbers

        self.assertEqual(
            parse_order_numbers("1405001，1405113　1405222 1405333\n1405444,1405555"),
            ["1405001", "1405113", "1405222", "1405333", "1405444", "1405555"],
        )

    def test_parse_strips_and_skips_blanks(self) -> None:
        from app.gui import parse_order_numbers

        self.assertEqual(parse_order_numbers("  1405001 ,, \n 1405113  "), ["1405001", "1405113"])

    def test_parse_empty(self) -> None:
        from app.gui import parse_order_numbers

        self.assertEqual(parse_order_numbers(""), [])
        self.assertEqual(parse_order_numbers("   "), [])


class DuplicateOrderNumberHelpersTest(unittest.TestCase):
    """受注No重複検出・除去ヘルパー（要件5・6）。"""

    def test_find_duplicates_returns_repeated_only(self) -> None:
        from app.gui import find_duplicate_order_numbers

        self.assertEqual(
            find_duplicate_order_numbers(["1405113", "1405114", "1405113"]),
            ["1405113"],
        )

    def test_find_duplicates_none_when_unique(self) -> None:
        from app.gui import find_duplicate_order_numbers

        self.assertEqual(find_duplicate_order_numbers(["1405113", "1405114"]), [])

    def test_find_duplicates_full_width_matches_half_width(self) -> None:
        from app.gui import find_duplicate_order_numbers

        # 全角数字と半角数字は同一受注No扱い（要件2の正規化方針）。
        self.assertEqual(
            find_duplicate_order_numbers(["1405113", "１４０５１１３"]),
            ["1405113"],
        )

    def test_dedupe_keeps_first_occurrence_order(self) -> None:
        from app.gui import dedupe_order_numbers

        self.assertEqual(
            dedupe_order_numbers(["1405113", "1405114", "1405113", "1405115"]),
            ["1405113", "1405114", "1405115"],
        )


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class StartRunDuplicateTest(unittest.TestCase):
    """Kintone登録処理画面で同じ受注Noが複数あると実行できない（要件5・6）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from app.gui import MainWindow

        with mock.patch.object(MainWindow, "_load_config", lambda self: None):
            win = MainWindow()
        win.config = mock.Mock()  # config 未設定だと start_run が即 return するため。
        self.addCleanup(win.deleteLater)
        return win

    def test_start_run_aborts_on_duplicate_order_no(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405113\n1405113")
        with mock.patch("app.gui.QMessageBox.warning") as warn, \
                mock.patch("app.gui.WorkerThread") as worker_cls:
            win.start_run()
        warn.assert_called_once()
        message = warn.call_args.args[2]
        self.assertIn("以下の受注Noはすでに一覧に存在します。", message)
        self.assertIn("1405113", message)
        self.assertNotIn("重複を削除", message)
        worker_cls.assert_not_called()

    def test_start_run_proceeds_when_unique(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405113\n1405114")
        with mock.patch("app.gui.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_collect_input", side_effect=ValueError("stop")):
            win.start_run()
        # 重複警告は出ず、_collect_input まで到達する（ValueError で停止）。
        # 重複メッセージではなく入力エラー扱いになることを確認。
        self.assertEqual(warn.call_args.args[1], "入力エラー")

    def test_collect_input_dedupes_order_numbers(self) -> None:
        win = self._make_window()
        win.denpyo_numbers.setPlainText("1405113\n1405113\n1405114")
        run_input = win._collect_input(require_denpyo=True)
        self.assertEqual(run_input.denpyo_numbers, ["1405113", "1405114"])


if __name__ == "__main__":
    unittest.main()
