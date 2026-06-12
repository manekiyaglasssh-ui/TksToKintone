"""VoucherWindow（受注一覧形式）の動的テスト。

QApplication を offscreen で起動し、実ウィジェットの構成と行ごとの
PDF作成・印刷処理が呼ばれることを検証する。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QDateEdit,
        QLineEdit,
        QPushButton,
        QRadioButton,
    )

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherWindowRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        return win

    def _find(self, rw, widget_type):
        return widget_type

    def test_initial_single_row(self) -> None:
        win = self._make_window()
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(len(win._rows), 1)

    def test_add_row_increases_count(self) -> None:
        win = self._make_window()
        win._on_add_row()
        self.assertEqual(win._table.rowCount(), 2)
        self.assertEqual(len(win._rows), 2)

    def test_remove_selected_deletes_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        self.assertEqual(win._table.rowCount(), 3)
        win._rows[1].select_check.setChecked(True)
        win._on_remove_selected()
        self.assertEqual(win._table.rowCount(), 2)
        self.assertEqual(len(win._rows), 2)

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
        rw.order_input.setText("5218869")
        self.assertEqual(win._collect_row(rw).am_pm, "AM")
        rw.ampm_pm.setChecked(True)
        self.assertEqual(win._collect_row(rw).am_pm, "PM")
        # 排他選択：PM選択でAMは外れる。
        self.assertFalse(rw.ampm_am.isChecked())

    def test_ampm_radio_layout_is_two_rows(self) -> None:
        """AM/PMラジオが縦2行に配置されること（要件3）。"""
        from PySide6.QtWidgets import QVBoxLayout

        from app.voucher_window import COL_AMPM

        win = self._make_window()
        cell = win._table.cellWidget(0, COL_AMPM)
        self.assertIsNotNone(cell)
        radios = cell.findChildren(QRadioButton)
        self.assertEqual(len(radios), 2)
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
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch.object(win, "_create_pdf") as create:
            rw.pdf_button.click()
        build.assert_called_once_with(["5218869"])
        create.assert_called_once()

    def test_print_button_calls_print_per_row(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_print_service.print_pdf_with_dialog") as pr:
            rw.print_button.click()
        build.assert_called_once_with(["5218869"])
        gen.assert_called_once()
        pr.assert_called_once()

    def test_preview_button_opens_inapp_window_without_formal_save(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
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
        preview.assert_called_once_with(b"%PDF")

    def test_row_preview_opens_preview_window(self) -> None:
        """行別プレビューで VoucherPrintPreviewWindow が開かれること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_preview_window.VoucherPrintPreviewWindow") as preview_cls:
            rw.preview_button.click()
        preview_cls.assert_called_once()
        self.assertEqual(preview_cls.call_args.args[0], b"%PDF")

    def test_empty_order_no_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            rw.pdf_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_no_voucher_selected_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        for cb in rw.voucher_checks.values():
            cb.setChecked(False)
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            rw.pdf_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_edit_order_sheet_empty_order_no_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        with mock.patch("app.voucher_window.QMessageBox.warning") as warn, \
                mock.patch.object(win, "_build_print_data") as build:
            rw.edit_button.click()
        warn.assert_called_once()
        build.assert_not_called()

    def test_edit_order_sheet_opens_editor(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch.object(win, "_cache_row_olap"), \
                mock.patch("app.voucher_edit_window.VoucherEditWindow") as editor_cls:
            rw.edit_button.click()
        build.assert_called_once_with(["5218869"])
        # 指図書(1) のみでプレビューPDFを生成する
        self.assertEqual(gen.call_args.args[0], ["03"])
        editor_cls.assert_called_once()

    def test_initial_size_shows_all_columns(self) -> None:
        """起動直後の初期幅・最小幅が全列を表示できるサイズであること。"""
        from app.voucher_window import COLUMN_LABELS

        win = self._make_window()
        self.assertEqual(len(COLUMN_LABELS), 12)
        self.assertEqual(win._table.columnCount(), 12)
        self.assertGreaterEqual(win.width(), 1560)
        self.assertGreaterEqual(win.minimumWidth(), 1360)
        # 横スクロールは必要時のみ（ポリシーは AsNeeded）
        from PySide6.QtCore import Qt

        self.assertEqual(
            win._table.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )

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
        from datetime import date

        from app.voucher_window import VoucherOrderRow

        win = self._make_window()
        row = VoucherOrderRow(
            order_no="5218869",
            finish_date=date(2026, 6, 10),
            am_pm="PM",
            process_checks={"広幅": True, "BOB": False},
            voucher_checks={"01": True},
        )
        data = {"pages": [{}, {}]}
        win._attach_row_settings(data, row)

        self.assertEqual(data["finish_date"], date(2026, 6, 10))
        self.assertEqual(data["am_pm"], "PM")
        for page in data["pages"]:
            self.assertEqual(page["row_finish_date"], date(2026, 6, 10))
            self.assertEqual(page["row_am_pm"], "PM")
            self.assertTrue(page["row_process_checks"]["広幅"])
            self.assertFalse(page["row_process_checks"]["BOB"])


    # ── 選択列・選択系ボタン ─────────────────────────────────────────────────
    def test_select_column_present(self) -> None:
        """初期表示で選択列（一番左）が存在し、行に選択チェックボックスがあること。"""
        from app.voucher_window import COLUMN_LABELS, COL_SELECT

        win = self._make_window()
        self.assertEqual(COL_SELECT, 0)
        self.assertEqual(COLUMN_LABELS[0], "選択")
        self.assertIsInstance(win._rows[0].select_check, QCheckBox)

    def test_selection_buttons_disabled_without_selection(self) -> None:
        """選択行がない場合、選択削除/選択PDF作成/選択印刷が無効であること。"""
        win = self._make_window()
        self.assertFalse(win._remove_row_button.isEnabled())
        self.assertFalse(win._select_pdf_button.isEnabled())
        self.assertFalse(win._select_print_button.isEnabled())

    def test_row_check_enables_selection_buttons(self) -> None:
        """行チェックONで選択系ボタンが有効、OFFで無効に戻ること。"""
        win = self._make_window()
        win._rows[0].select_check.setChecked(True)
        self.assertTrue(win._remove_row_button.isEnabled())
        self.assertTrue(win._select_pdf_button.isEnabled())
        self.assertTrue(win._select_print_button.isEnabled())
        win._rows[0].select_check.setChecked(False)
        self.assertFalse(win._remove_row_button.isEnabled())
        self.assertFalse(win._select_pdf_button.isEnabled())
        self.assertFalse(win._select_print_button.isEnabled())

    def test_header_select_all_checks_all_rows(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        win._select_all_check.setChecked(True)
        win._on_select_all_clicked()
        for rw in win._rows:
            self.assertTrue(rw.select_check.isChecked())
        self.assertEqual(win._select_all_check.checkState(), Qt.CheckState.Checked)

    def test_header_select_all_clears_all_rows(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._set_all_rows_checked(True)
        win._select_all_check.setChecked(False)
        win._on_select_all_clicked()
        for rw in win._rows:
            self.assertFalse(rw.select_check.isChecked())
        self.assertEqual(win._select_all_check.checkState(), Qt.CheckState.Unchecked)

    def test_partial_selection_sets_tristate(self) -> None:
        from PySide6.QtCore import Qt

        win = self._make_window()
        win._on_add_row()
        win._rows[0].select_check.setChecked(True)
        self.assertEqual(
            win._select_all_check.checkState(), Qt.CheckState.PartiallyChecked
        )

    def test_remove_selected_keeps_at_least_one_row(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._set_all_rows_checked(True)
        win._on_remove_selected()
        # すべて削除しても空行が1行残る
        self.assertEqual(win._table.rowCount(), 1)
        self.assertEqual(len(win._rows), 1)
        self.assertFalse(win._rows[0].select_check.isChecked())

    def test_select_pdf_targets_only_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("1111111")
        win._rows[1].order_input.setText("2222222")
        win._rows[0].select_check.setChecked(True)  # 1行目だけ選択
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch.object(win, "_resolve_pdf_output_dir", return_value="/tmp"), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF") as merge, \
                mock.patch("app.voucher_service.save_pdf_bytes", return_value="/tmp/out.pdf"), \
                mock.patch("app.voucher_window.QDesktopServices.openUrl"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_pdf()
        # チェックON行（1111111）だけが処理対象
        build.assert_called_once_with(["1111111"])
        gen.assert_called_once()
        merge.assert_called_once()

    def test_select_print_targets_only_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("1111111")
        win._rows[1].order_input.setText("2222222")
        win._rows[1].select_check.setChecked(True)  # 2行目だけ選択
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}) as build, \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF") as gen, \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%PDF") as merge, \
                mock.patch("app.voucher_print_service.print_pdf_with_dialog") as pr:
            win._on_select_print()
        build.assert_called_once_with(["2222222"])
        gen.assert_called_once()
        merge.assert_called_once()
        pr.assert_called_once()

    def test_select_preview_does_not_formally_save_pdf(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("1111111")
        win._rows[1].order_input.setText("2222222")
        win._rows[0].select_check.setChecked(True)
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
        win._rows[0].order_input.setText("1111111")
        win._rows[1].order_input.setText("2222222")
        win._rows[0].select_check.setChecked(True)
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch("app.voucher_service.merge_pdf_bytes", return_value=b"%MERGED"), \
                mock.patch("app.voucher_preview_window.VoucherPrintPreviewWindow") as preview_cls:
            win._on_select_preview()
        preview_cls.assert_called_once()
        self.assertEqual(preview_cls.call_args.args[0], b"%MERGED")

    def test_select_pdf_validates_each_row_with_number(self) -> None:
        """選択行に不正があれば、行番号付きで中断すること。"""
        win = self._make_window()
        win._on_add_row()
        win._rows[0].order_input.setText("1111111")
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


    # ── 切断仕上日カラム・印刷する伝票設定 ───────────────────────────────────
    def test_no_cut_finish_date_column(self) -> None:
        """画面に「切断仕上日」カラムが追加されていないこと。"""
        from app.voucher_window import COLUMN_LABELS

        self.assertNotIn("切断仕上日", COLUMN_LABELS)
        win = self._make_window()
        headers = [win._table.horizontalHeaderItem(i).text()
                   for i in range(win._table.columnCount())]
        self.assertNotIn("切断仕上日", headers)

    def test_voucher_settings_button_present(self) -> None:
        win = self._make_window()
        self.assertEqual(win._voucher_settings_button.text(), "印刷する伝票設定")

    def test_default_print_types_reflected_in_new_row(self) -> None:
        """_default_print_types が新規追加行の印刷する伝票チェックへ反映されること。"""
        win = self._make_window()
        win._default_print_types = {"01", "03"}
        win._on_add_row()
        rw = win._rows[-1]
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

    # ── 行ごとの削除ボタン・最大化・列区切り（要件1-1〜1-5）─────────────────────
    def test_row_has_delete_button_in_last_column(self) -> None:
        from app.voucher_window import COL_DELETE, COLUMN_LABELS

        win = self._make_window()
        # 削除列が一番右端であること。
        self.assertEqual(COL_DELETE, len(COLUMN_LABELS) - 1)
        rw = win._rows[0]
        self.assertEqual(rw.delete_button.text(), "削除")
        widget = win._table.cellWidget(0, COL_DELETE)
        self.assertIsNotNone(widget)
        # 削除ボタンがその列のセル内にある。
        self.assertIn(rw.delete_button, widget.findChildren(type(rw.delete_button)))

    def test_row_delete_button_is_danger_colored(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self.assertTrue(rw.delete_button.property("danger"))
        style = rw.delete_button.styleSheet()
        self.assertIn("#c62828", style)
        self.assertIn("white", style)

    def test_row_delete_removes_only_that_row(self) -> None:
        win = self._make_window()
        win._on_add_row()
        win._on_add_row()
        win._rows[1].order_input.setText("ROW-B")
        target = win._rows[1]
        win._on_delete_row(target)
        self.assertEqual(len(win._rows), 2)
        self.assertNotIn(target, win._rows)
        # 残った行に ROW-B が無い（対象行だけ消えた）。
        self.assertNotIn("ROW-B", [rw.order_input.text() for rw in win._rows])

    def test_row_delete_keeps_at_least_one_row(self) -> None:
        win = self._make_window()
        self.assertEqual(len(win._rows), 1)
        win._on_delete_row(win._rows[0])
        # 最後の1行を消しても空行が1行残る。
        self.assertEqual(len(win._rows), 1)

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

    # ── 加工名・印刷する伝票の3段配置（要件1）─────────────────────────────────
    def test_process_checkboxes_three_rows(self) -> None:
        """加工名チェックボックスが3段（3行）に配置されること（要件1）。"""
        from PySide6.QtWidgets import QGridLayout

        from app.voucher_window import COL_PROCESS

        win = self._make_window()
        cell = win._table.cellWidget(0, COL_PROCESS)
        grid = cell.layout()
        self.assertIsInstance(grid, QGridLayout)
        self.assertEqual(grid.rowCount(), 3)

    def test_voucher_checkboxes_three_rows(self) -> None:
        """印刷する伝票チェックボックスが3段（3行）に配置されること（要件1）。"""
        from PySide6.QtWidgets import QGridLayout

        from app.voucher_window import COL_VOUCHER

        win = self._make_window()
        cell = win._table.cellWidget(0, COL_VOUCHER)
        grid = cell.layout()
        self.assertIsInstance(grid, QGridLayout)
        self.assertEqual(grid.rowCount(), 3)

    # ── 取り直し列・ボタン（要件2・6）─────────────────────────────────────────
    def test_refetch_column_between_order_no_and_finish_date(self) -> None:
        from app.voucher_window import (
            COLUMN_LABELS,
            COL_FINISH_DATE,
            COL_ORDER_NO,
            COL_REFETCH,
        )

        self.assertEqual(COL_REFETCH, COL_ORDER_NO + 1)
        self.assertEqual(COL_FINISH_DATE, COL_REFETCH + 1)
        self.assertEqual(COLUMN_LABELS[COL_REFETCH], "取り直し")

    def test_row_has_refetch_button(self) -> None:
        from app.voucher_window import COL_REFETCH

        win = self._make_window()
        rw = win._rows[0]
        self.assertIsInstance(rw.refetch_button, QPushButton)
        self.assertEqual(rw.refetch_button.text(), "取り直し")
        widget = win._table.cellWidget(0, COL_REFETCH)
        self.assertIsNotNone(widget)
        self.assertIn(rw.refetch_button, widget.findChildren(QPushButton))

    def test_refetch_calls_olap_for_order_no(self) -> None:
        """取り直しボタン押下で対象受注NoのOLAP再取得処理が呼ばれること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}], "raw_rows": []}) as build, \
                mock.patch.object(win, "_cache_row_olap") as cache:
            rw.refetch_button.click()
        build.assert_called_once_with(["5218869"])
        cache.assert_called_once()

    def test_refetch_success_updates_row_data(self) -> None:
        """取り直し成功時に対象行のOLAPデータ（cached_olap）が更新されること。"""
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("5218869")
        self.assertIsNone(rw.cached_olap)
        data = {"pages": [{}], "raw_rows": []}
        with mock.patch.object(win, "_build_print_data", return_value=data), \
                mock.patch.object(win, "_cache_row_olap"):
            rw.refetch_button.click()
        self.assertIs(rw.cached_olap, data)
        # ボタン文言・有効状態が元に戻る。
        self.assertEqual(rw.refetch_button.text(), "取り直し")
        self.assertTrue(rw.refetch_button.isEnabled())

    def test_refetch_failure_keeps_existing_data(self) -> None:
        """取り直し失敗時に既存データ（設定・OLAPデータ）が維持されること。"""
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
        self.assertEqual(rw.refetch_button.text(), "取り直し")
        self.assertTrue(rw.refetch_button.isEnabled())

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
        """取り直し時に voucher_edit_objects 配下の編集JSONが削除されないこと。"""
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

    def test_column_order_matches_recommendation(self) -> None:
        """推奨列順どおりであること（要件5）。"""
        from app.voucher_window import COLUMN_LABELS

        self.assertEqual(
            COLUMN_LABELS,
            [
                "選択",
                "受注No",
                "取り直し",
                "仕上日",
                "AM・PM",
                "加工名",
                "印刷する伝票",
                "指図書編集",
                "PDF作成",
                "プレビュー",
                "印刷",
                "削除",
            ],
        )


if __name__ == "__main__":
    unittest.main()
