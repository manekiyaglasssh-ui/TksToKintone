"""VoucherWindow（受注一覧形式）の動的テスト。

QApplication を offscreen で起動し、実ウィジェットの構成と行ごとの
PDF作成・印刷処理が呼ばれることを検証する。
"""
from __future__ import annotations

import os
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
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
        QComboBox,
        QRadioButton,
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


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherWindowRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        self._test_home = tempfile.TemporaryDirectory()
        os.environ["TKS_TO_KINTONE_HOME"] = self._test_home.name

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._previous_home
        self._test_home.cleanup()

    def _make_window(self):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw")
        self.addCleanup(win.deleteLater)
        return win

    def _make_window_with_kintone(self, kintone_window):
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(
            olap_login_id="id",
            olap_password="pw",
            kintone_window_provider=lambda: kintone_window,
        )
        self.addCleanup(win.deleteLater)
        return win

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

    def test_print_button_calls_print_per_row(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
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
        preview.assert_called_once_with(b"%PDF")

    def test_row_preview_opens_preview_window(self) -> None:
        """行別プレビューで VoucherPrintPreviewWindow が開かれること。"""
        win = self._make_window()
        rw = win._rows[0]
        self._mark_fetched(win, rw, "5218869")
        with mock.patch.object(win, "_build_print_data", return_value={"pages": [{}]}), \
                mock.patch("app.voucher_service.build_vouchers_pdf_bytes", return_value=b"%PDF"), \
                mock.patch.object(win, "_open_preview_window") as preview:
            rw.preview_button.click()
        preview.assert_called_once_with(b"%PDF")

    def test_empty_order_no_is_error(self) -> None:
        win = self._make_window()
        rw = win._rows[0]
        rw.order_input.setText("")
        self.assertFalse(rw.pdf_button.isEnabled())

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
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(olap_login_id="id", olap_password="pw",
                            kintone_window_provider=lambda: fake_kintone)
        self.addCleanup(win.deleteLater)
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
        self.assertEqual(win._table.rowCount(), 2)
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
        # 入力用の空行が最低1行残る。
        self.assertGreaterEqual(len(win._rows), 1)
        self.assertEqual(win._table.rowCount(), len(win._rows))

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
            "app.voucher_print_service.print_pdf_with_dialog"
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
                mock.patch("app.voucher_service.save_pdf_bytes", return_value="/tmp/out.pdf") as save, \
                mock.patch("app.voucher_window.QDesktopServices.openUrl"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_pdf()
        # チェックON行（1111111）だけが処理対象
        build.assert_called_once_with(["1111111"])
        gen.assert_called_once()
        merge.assert_not_called()
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["filename_token"], "1111111")

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
                mock.patch("app.voucher_service.save_pdf_bytes", side_effect=["/tmp/1111111.pdf", "/tmp/2222222.pdf"]) as save, \
                mock.patch("app.voucher_window.QDesktopServices.openUrl"), \
                mock.patch("app.voucher_window.QMessageBox.information"):
            win._on_select_pdf()
        self.assertEqual(build.call_count, 2)
        self.assertEqual(
            sorted(call.kwargs["filename_token"] for call in save.call_args_list),
            ["1111111", "2222222"],
        )

    def test_select_print_targets_only_checked_rows(self) -> None:
        win = self._make_window()
        win._on_add_row()
        other = win._rows[0]
        target = win._rows[1]
        self._mark_fetched(win, other, "1111111")
        self._mark_fetched(win, target, "2222222")
        target.select_check.setChecked(True)  # 受注No=2222222 だけ選択
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

    def test_voucher_settings_button_present(self) -> None:
        win = self._make_window()
        self.assertEqual(win._voucher_settings_button.text(), "⚙")
        self.assertEqual(win._voucher_settings_button.toolTip(), "印刷する伝票設定")
        self.assertGreaterEqual(win._voucher_settings_button.minimumWidth(), 40)
        self.assertGreaterEqual(win._voucher_settings_button.minimumHeight(), 40)

    def test_voucher_settings_button_is_top_right(self) -> None:
        source = Path("app/voucher_window.py").read_text(encoding="utf-8")
        layout_source = source[source.index("def _build_layout") : source.index("def _wrap")]
        self.assertLess(
            layout_source.index("top_row.addWidget(self._select_print_button)"),
            layout_source.index("top_row.addWidget(self._voucher_settings_button)"),
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
            layout_source.index("top_row.addWidget(self._voucher_settings_button)"),
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
        self.assertEqual(len(win._rows), 2)
        self.assertIs(win._rows[0], next(row for row in win._rows if row is not rw))
        self.assertEqual(win._rows[0].order_input.text(), "")
        self.assertEqual(win._rows[0].refetch_button.text(), "取得")
        self.assertFalse(win._rows[0].date_edit.isEnabled())

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
                mock.patch("app.voucher_print_service.print_pdf_with_dialog"):
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

    def test_add_row_button_disabled_when_any_order_no_empty(self) -> None:
        win = self._make_window()
        self.assertFalse(win._add_row_button.isEnabled())

    def test_add_row_button_reenabled_after_order_no_input(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("1405113")
        self.assertTrue(win._add_row_button.isEnabled())

    def test_whitespace_only_order_no_keeps_add_row_button_disabled(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText(" 　")
        self.assertFalse(win._add_row_button.isEnabled())
        win._rows[0].order_input.setText("1405113")
        self.assertTrue(win._add_row_button.isEnabled())

    def test_hidden_empty_row_still_disables_add_row_button(self) -> None:
        win = self._make_window()
        empty = win._rows[0]
        win._on_add_row()
        filled = win._rows[0]
        filled.order_input.setText("ABC100")
        empty.order_input.setText("")

        win._order_search_edit.setText("ABC")

        self.assertFalse(win._table.isRowHidden(self._rows_index(win, empty)))
        self.assertFalse(win._add_row_button.isEnabled())

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
            self.assertFalse(win._add_row_button.isEnabled())

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

            self.assertEqual(self._visible_order_numbers(win), ["", "DONE", "TODO"])
            self.assertFalse(win._add_row_button.isEnabled())

    def test_empty_added_row_is_visible_and_does_not_hide_other_records(self) -> None:
        win = self._make_window()
        win._rows[0].order_input.setText("ABC100")

        win._on_add_row()

        self.assertEqual(self._visible_order_numbers(win), ["", "ABC100"])
        self.assertFalse(win._add_row_button.isEnabled())
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
                "選択",
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
        rw = win._rows[0]
        rw.order_input.setText("")
        self.assertFalse(rw.kintone_button.isEnabled())
        fake_window.add_order_no.assert_not_called()

    def test_kintone_register_noop_when_window_closed(self) -> None:
        """画面が閉じている場合は add_order_no を呼ばず防御的に警告する。"""
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(kintone_window_provider=lambda: None)
        self.addCleanup(win.deleteLater)
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


if __name__ == "__main__":
    unittest.main()
