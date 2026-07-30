"""印刷設定画面（VoucherPrinterSettingsDialog）の非同期化・非ブロッキング化テスト。

印刷設定画面を開いた直後にアプリが応答なしになる不具合の再発防止。
初期表示で重い処理（プリンター一覧取得・自動検出・subprocess）を UI スレッドで
同期実行しないこと、プリンター一覧はバックグラウンドで取得することを検証する。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import (
        QApplication,
        QDialogButtonBox,
        QPlainTextEdit,
        QScrollArea,
    )

    PYSIDE_AVAILABLE = True
except Exception:  # pragma: no cover - PySide6 未導入環境
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherPrinterSettingsDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev_home
        self._home.cleanup()

    def _make_dialog(self, *, printer_name: str = "SavedPrinter", backend: str = "acrobat"):
        from app.voucher_settings import VoucherPrinterSettings
        from app.voucher_window import VoucherPrinterSettingsDialog

        settings = VoucherPrinterSettings(printer_name=printer_name, print_backend=backend)
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings", return_value=settings
        ):
            dialog = VoucherPrinterSettingsDialog()
        self.addCleanup(dialog.deleteLater)
        return dialog

    def _pump(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        QApplication.processEvents()
        return bool(predicate())

    # 1. 初期化で QPrinterInfo.availablePrinters を同期呼び出ししない
    def test_init_does_not_call_available_printers(self) -> None:
        with mock.patch("PySide6.QtPrintSupport.QPrinterInfo.availablePrinters") as available:
            self._make_dialog()
        available.assert_not_called()

    # 2. 初期化で SumatraPDF 自動検出を実行しない
    def test_init_does_not_auto_detect_sumatra(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "detect_sumatra_pdf_path") as detect:
            self._make_dialog()
        detect.assert_not_called()

    # 3. 初期化で Acrobat Reader 自動検出を実行しない
    def test_init_does_not_auto_detect_acrobat(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service, "detect_acrobat_reader_path") as detect:
            self._make_dialog()
        detect.assert_not_called()

    # 4. 初期化で subprocess を実行しない
    def test_init_does_not_run_subprocess(self) -> None:
        from app import voucher_print_service

        with mock.patch.object(voucher_print_service.subprocess, "Popen") as popen, \
                mock.patch.object(voucher_print_service.subprocess, "check_output") as check_output:
            self._make_dialog()
        popen.assert_not_called()
        check_output.assert_not_called()

    def test_backend_labels_mark_sumatra_standard_and_acrobat_non_standard(self) -> None:
        dialog = self._make_dialog()
        labels = [dialog._backend_combo.itemText(i) for i in range(dialog._backend_combo.count())]
        self.assertIn("SumatraPDF経由（標準・高速）", labels)
        self.assertIn("Acrobat Reader経由（サイズ確認用・画面表示あり）", labels)
        self.assertIn("Qt直接印刷（予備）", labels)

    def test_new_env_default_sumatra_preset_is_default_label(self) -> None:
        # 新規環境のSumatraPDFプリセット既定は「既定」。
        from app.voucher_settings import VoucherPrinterSettings
        from app.voucher_window import VoucherPrinterSettingsDialog

        settings = VoucherPrinterSettings()  # 新規環境の既定値
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings", return_value=settings
        ):
            dialog = VoucherPrinterSettingsDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog._sumatra_preset_combo.currentText(), "既定")

    def test_save_pdf_on_print_checkbox_defaults_off_and_reflected(self) -> None:
        dialog = self._make_dialog()
        # 新規環境の既定はOFF。
        self.assertFalse(dialog._save_pdf_on_print_check.isChecked())
        self.assertFalse(dialog.values().save_pdf_on_print)
        dialog._save_pdf_on_print_check.setChecked(True)
        self.assertTrue(dialog.values().save_pdf_on_print)

    def test_save_pdf_on_print_restored_off_on_restore_defaults(self) -> None:
        dialog = self._make_dialog()
        dialog._save_pdf_on_print_check.setChecked(True)
        dialog._restore_defaults()
        self.assertFalse(dialog._save_pdf_on_print_check.isChecked())

    def test_open_pdf_after_create_checkbox_defaults_on_and_reflected(self) -> None:
        dialog = self._make_dialog()
        self.assertTrue(dialog._open_pdf_after_create_check.isChecked())
        self.assertTrue(dialog.values().open_pdf_after_create)
        dialog._open_pdf_after_create_check.setChecked(False)
        self.assertFalse(dialog.values().open_pdf_after_create)

    def test_open_pdf_after_create_restored_on_on_restore_defaults(self) -> None:
        dialog = self._make_dialog()
        dialog._open_pdf_after_create_check.setChecked(False)
        dialog._restore_defaults()
        self.assertTrue(dialog._open_pdf_after_create_check.isChecked())

    def test_sumatra_preset_updates_print_settings_field(self) -> None:
        dialog = self._make_dialog()
        index = dialog._sumatra_preset_combo.findText("fit")
        self.assertGreaterEqual(index, 0)
        dialog._sumatra_preset_combo.setCurrentIndex(index)
        self.assertEqual(
            dialog._sumatra_settings_edit.text(),
            "fit,monochrome,paper=auto,bin=auto,center",
        )

    def test_custom_sumatra_print_settings_are_directly_editable(self) -> None:
        dialog = self._make_dialog()
        text = "noscale,monochrome,paperkind=182,bin=auto,center,disable-auto-rotation"
        dialog._sumatra_settings_edit.setText(text)
        dialog._on_sumatra_settings_edited(text)
        self.assertEqual(dialog.values().sumatra_print_settings, text)
        self.assertFalse(dialog.values().sumatra_auto_rotation)

    # 12. 保存済みプリンター名は一覧取得前でも表示される
    def test_saved_printer_shown_before_list_load(self) -> None:
        dialog = self._make_dialog(printer_name="SavedPrinter")
        names = [dialog._printer_combo.itemText(i) for i in range(dialog._printer_combo.count())]
        self.assertIn("SavedPrinter", names)
        # showEvent 前は一覧取得を開始しない。
        self.assertFalse(dialog._printer_load_started)

    # 13. 保存・キャンセルボタンは初期表示直後から押せる
    def test_save_and_cancel_enabled_immediately(self) -> None:
        dialog = self._make_dialog()
        box = dialog.findChild(QDialogButtonBox)
        self.assertIsNotNone(box)
        self.assertTrue(box.button(QDialogButtonBox.StandardButton.Save).isEnabled())
        self.assertTrue(box.button(QDialogButtonBox.StandardButton.Cancel).isEnabled())

    # 5 & 6. プリンター一覧はバックグラウンド worker で取得され、signal 経由で combo が更新される
    def test_printer_list_loaded_in_background_updates_combo(self) -> None:
        from app import voucher_print_service

        dialog = self._make_dialog(printer_name="SavedPrinter")
        with mock.patch.object(
            voucher_print_service,
            "list_available_printer_names",
            return_value=(["P1", "P2"], "P1"),
        ):
            dialog._start_printer_list_load()
            finished = self._pump(lambda: dialog._printer_load_finished, timeout=3.0)
        self.assertTrue(finished)
        names = [dialog._printer_combo.itemText(i) for i in range(dialog._printer_combo.count())]
        self.assertIn("P1", names)
        self.assertIn("P2", names)

    def test_on_printer_list_loaded_slot_updates_combo(self) -> None:
        dialog = self._make_dialog(printer_name="SavedPrinter")
        dialog._on_printer_list_loaded((["A", "B"], "A"))
        names = [dialog._printer_combo.itemText(i) for i in range(dialog._printer_combo.count())]
        self.assertIn("A", names)
        self.assertIn("B", names)

    # 7. タイムアウト時も画面が操作可能
    def test_timeout_keeps_dialog_operable(self) -> None:
        dialog = self._make_dialog()
        dialog._printer_load_finished = False
        dialog._printer_load_start_monotonic = time.monotonic()
        dialog._on_printer_list_timeout()
        self.assertTrue(dialog.isEnabled())
        box = dialog.findChild(QDialogButtonBox)
        self.assertTrue(box.button(QDialogButtonBox.StandardButton.Save).isEnabled())
        self.assertIn("時間がかかっています", dialog._status_label.text())

    # 8. 自動検出ボタン押下時だけ SumatraPDF 検出が走る
    def test_sumatra_detection_runs_only_on_button_click(self) -> None:
        from app import voucher_print_service

        dialog = self._make_dialog()
        calls: list[tuple] = []
        dialog._run_in_background = (
            lambda func, on_result, on_error, *, name: calls.append((func, name)) or (None, None)
        )
        dialog._sumatra_detect_button.click()
        self.assertTrue(any(name == "sumatra_detect" for _, name in calls))
        self.assertIs(calls[0][0], voucher_print_service.detect_sumatra_pdf_path)

    # 9. 自動検出ボタン押下時だけ Acrobat Reader 検出が走る
    def test_acrobat_detection_runs_only_on_button_click(self) -> None:
        from app import voucher_print_service

        dialog = self._make_dialog()
        calls: list[tuple] = []
        dialog._run_in_background = (
            lambda func, on_result, on_error, *, name: calls.append((func, name)) or (None, None)
        )
        dialog._acrobat_detect_button.click()
        self.assertTrue(any(name == "acrobat_detect" for _, name in calls))
        self.assertIs(calls[0][0], voucher_print_service.detect_acrobat_reader_path)

    # 10. ダイアログを閉じた後に worker が返っても UI 更新しない
    def test_worker_result_ignored_after_dialog_closed(self) -> None:
        dialog = self._make_dialog(printer_name="SavedPrinter")
        dialog._alive = False
        dialog._on_printer_list_loaded((["X", "Y"], "X"))
        names = [dialog._printer_combo.itemText(i) for i in range(dialog._printer_combo.count())]
        self.assertNotIn("X", names)
        self.assertNotIn("Y", names)

    def test_sumatra_detect_result_ignored_after_dialog_closed(self) -> None:
        from app import voucher_settings

        dialog = self._make_dialog()
        dialog._alive = False
        dialog._on_sumatra_detected("/tmp/SumatraPDF.exe")
        self.assertEqual(dialog._sumatra_path_edit.text(), voucher_settings.DEFAULT_SUMATRA_PATH)

    # 11. closeEvent で worker.wait() を同期実行しない
    def test_shutdown_does_not_wait_on_worker(self) -> None:
        dialog = self._make_dialog()
        fake_thread = mock.Mock()
        dialog._bg_threads.add(fake_thread)
        dialog._shutdown_background()
        fake_thread.quit.assert_called_once()
        fake_thread.wait.assert_not_called()
        self.assertFalse(dialog._alive)

    def test_close_event_sets_not_alive_without_wait(self) -> None:
        dialog = self._make_dialog()
        fake_thread = mock.Mock()
        dialog._bg_threads.add(fake_thread)
        dialog.close()
        fake_thread.wait.assert_not_called()
        self.assertFalse(dialog._alive)

    # ── 画面が収まらない問題の再発防止（スクロール化・サイズ制限）───────────

    # 1. 印刷設定ダイアログに QScrollArea が使われている
    def test_dialog_uses_scroll_area(self) -> None:
        dialog = self._make_dialog()
        self.assertIsNotNone(dialog.findChild(QScrollArea))

    # 2. ダイアログが resize 可能である（size grip 有効）
    def test_dialog_is_resizable(self) -> None:
        dialog = self._make_dialog()
        self.assertTrue(dialog.isSizeGripEnabled())
        # 最小サイズ以上に広げられること（固定サイズになっていない）。
        self.assertLessEqual(dialog.minimumWidth(), dialog.maximumWidth())
        self.assertLess(dialog.minimumHeight(), 16777215)

    # 3. ダイアログ初期高さが利用可能画面高さを超えない
    def test_dialog_initial_height_within_screen(self) -> None:
        dialog = self._make_dialog()
        screen = dialog.screen() or QApplication.primaryScreen()
        if screen is None:
            self.skipTest("スクリーン情報が取得できません")
        available = screen.availableGeometry()
        self.assertLessEqual(dialog.height(), available.height())
        self.assertLessEqual(dialog.width(), available.width())

    # 4. コマンド概要が固定高さのテキスト欄になっている
    def test_command_summary_is_fixed_height_text(self) -> None:
        dialog = self._make_dialog()
        summary = dialog._sumatra_command_summary_label
        self.assertIsInstance(summary, QPlainTextEdit)
        self.assertTrue(summary.isReadOnly())
        self.assertLessEqual(summary.maximumHeight(), 200)

    # 5. 保存/キャンセル等の主要ボタンがスクロール領域外にある
    def test_primary_buttons_outside_scroll_area(self) -> None:
        dialog = self._make_dialog()
        scroll = dialog.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        box = dialog.findChild(QDialogButtonBox)
        self.assertIsNotNone(box)
        save_button = box.button(QDialogButtonBox.StandardButton.Save)
        cancel_button = box.button(QDialogButtonBox.StandardButton.Cancel)
        for button in (save_button, cancel_button, dialog._sumatra_test_print_button):
            self.assertIsNotNone(button)
            # スクロール内容ウィジェットの子孫でないこと。
            self.assertFalse(scroll.widget().isAncestorOf(button))

    # 6. SumatraPDF詳細設定がスクロール内でも保存・復元できる
    def test_sumatra_detail_persisted_within_scroll(self) -> None:
        dialog = self._make_dialog()
        scroll = dialog.findChild(QScrollArea)
        # 詳細コントロールはスクロール内容の子孫であること。
        self.assertTrue(scroll.widget().isAncestorOf(dialog._sumatra_settings_edit))
        text = "fit,color,paperkind=200,bin=auto,disable-auto-rotation"
        dialog._sumatra_settings_edit.setText(text)
        dialog._on_sumatra_settings_edited(text)
        values = dialog.values()
        self.assertEqual(values.sumatra_print_settings, text)
        self.assertFalse(values.sumatra_auto_rotation)

    # 7. 長い SumatraPDF パスでも横幅が崩れない（tooltip で全文表示）
    def test_long_sumatra_path_does_not_break_width(self) -> None:
        dialog = self._make_dialog()
        long_path = "C:\\" + "\\".join(["very_long_directory_name"] * 20) + "\\SumatraPDF.exe"
        dialog._sumatra_path_edit.setText(long_path)
        QApplication.processEvents()
        # 横スクロールは出さない設定。
        from PySide6.QtCore import Qt

        self.assertEqual(
            dialog._settings_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        # 全文は tooltip で確認できる。
        self.assertEqual(dialog._sumatra_path_edit.toolTip(), long_path)
        self.assertEqual(dialog.values().sumatra_path, long_path)


    # ── 印刷位置・余白補正 ─────────────────────────────────────────────────
    def test_adjustment_group_exists_and_defaults_on(self) -> None:
        # 新規環境の既定は印刷補正 ON・左右4mm/上3mm/下1.5mm。倍率は据え置き100%。
        dialog = self._make_dialog()
        self.assertTrue(dialog._adjustment_enabled_check.isChecked())
        self.assertEqual(dialog._adjustment_scale_x_spin.value(), 100.0)
        self.assertEqual(dialog._adjustment_scale_y_spin.value(), 100.0)
        self.assertEqual(dialog._adjustment_margin_left_spin.value(), 4.0)
        self.assertEqual(dialog._adjustment_margin_right_spin.value(), 4.0)
        self.assertEqual(dialog._adjustment_margin_top_spin.value(), 3.0)
        self.assertEqual(dialog._adjustment_margin_bottom_spin.value(), 1.5)

    def test_adjustment_values_reflected_in_settings(self) -> None:
        dialog = self._make_dialog()
        dialog._adjustment_enabled_check.setChecked(True)
        dialog._adjustment_margin_left_spin.setValue(1.5)
        dialog._adjustment_scale_y_spin.setValue(99.5)
        dialog._adjustment_offset_x_spin.setValue(0.5)
        dialog._adjustment_save_pdf_check.setChecked(True)
        values = dialog.values()
        self.assertTrue(values.print_adjustment_enabled)
        self.assertAlmostEqual(values.print_adjustment_margin_left_mm, 1.5)
        self.assertAlmostEqual(values.print_adjustment_scale_y_percent, 99.5)
        self.assertAlmostEqual(values.print_adjustment_offset_x_mm, 0.5)
        self.assertTrue(values.print_adjustment_save_pdf)

    def test_adjustment_summary_shows_on_and_off(self) -> None:
        dialog = self._make_dialog()
        dialog._adjustment_enabled_check.setChecked(False)
        self.assertIn("OFF", dialog._adjustment_summary_label.text())
        dialog._adjustment_enabled_check.setChecked(True)
        dialog._adjustment_scale_y_spin.setValue(99.5)
        self.assertIn("ON", dialog._adjustment_summary_label.text())
        self.assertIn("99.5%", dialog._adjustment_summary_label.text())

    def test_adjustment_survives_restore_defaults(self) -> None:
        # 既定に戻すで印刷補正 ON・左右4mm/上3mm/下1.5mm・倍率100%へ戻る。
        dialog = self._make_dialog()
        dialog._adjustment_enabled_check.setChecked(False)
        dialog._adjustment_scale_x_spin.setValue(101.0)
        dialog._adjustment_margin_left_spin.setValue(0.0)
        dialog._restore_defaults()
        self.assertTrue(dialog._adjustment_enabled_check.isChecked())
        self.assertEqual(dialog._adjustment_scale_x_spin.value(), 100.0)
        self.assertEqual(dialog._adjustment_margin_left_spin.value(), 4.0)
        self.assertEqual(dialog._adjustment_margin_right_spin.value(), 4.0)
        self.assertEqual(dialog._adjustment_margin_top_spin.value(), 3.0)
        self.assertEqual(dialog._adjustment_margin_bottom_spin.value(), 1.5)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 が利用できません")
class TestVoucherPrinterSettingsTestPrint(unittest.TestCase):
    """統合設定タブ（embedded）でもテスト印刷ボタンが動作することの検証。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self._prev_home = os.environ.get("TKS_TO_KINTONE_HOME")
        os.environ["TKS_TO_KINTONE_HOME"] = self._home.name

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("TKS_TO_KINTONE_HOME", None)
        else:
            os.environ["TKS_TO_KINTONE_HOME"] = self._prev_home
        self._home.cleanup()

    def _make_dialog(self, *, embedded: bool, printer_name: str = "SavedPrinter"):
        from app.voucher_settings import VoucherPrinterSettings
        from app.voucher_window import VoucherPrinterSettingsDialog

        settings = VoucherPrinterSettings(printer_name=printer_name, print_backend="acrobat")
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings", return_value=settings
        ):
            dialog = VoucherPrinterSettingsDialog(embedded=embedded)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def _make_host_dialog(self, host):
        """テスト印刷ホスト（_enqueue_sumatra_test_print を持つ）を親に持つ埋め込みタブを作る。

        host → QScrollArea → 印刷設定タブ の親子関係を作り、親チェーン探索で host を
        見つけられるようにする（統合設定ダイアログの構造を模す）。
        """
        from PySide6.QtWidgets import QScrollArea, QVBoxLayout

        layout = QVBoxLayout(host)
        scroll = QScrollArea(host)
        layout.addWidget(scroll)
        dialog = self._make_dialog(embedded=True)
        scroll.setWidget(dialog)
        return dialog, host

    # 1. テスト印刷ボタンが存在する（単独）
    def test_button_exists_standalone(self) -> None:
        dialog = self._make_dialog(embedded=False)
        self.assertIsNotNone(dialog._sumatra_test_print_button)

    # 2. embedded=True でもテスト印刷ボタンが表示・有効
    def test_button_visible_and_enabled_when_embedded(self) -> None:
        dialog = self._make_dialog(embedded=True)
        dialog.show()
        QApplication.processEvents()
        self.assertTrue(dialog._sumatra_test_print_button.isVisibleTo(dialog))
        self.assertTrue(dialog._sumatra_test_print_button.isEnabled())

    # 3. ボタン押下でテスト印刷処理（ホストの _enqueue_sumatra_test_print）が呼ばれる
    def test_click_invokes_host_enqueue_when_embedded(self) -> None:
        from PySide6.QtWidgets import QWidget

        class FakeHost(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.called = 0
                self.override = "unset"

            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:  # noqa: N802
                self.called += 1
                self.override = settings_override
                return True

        host = FakeHost()
        self.addCleanup(host.deleteLater)
        dialog, _container = self._make_host_dialog(host)
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("RealPrinter", "RealPrinter")
        dialog._printer_combo.setCurrentIndex(0)

        dialog._sumatra_test_print_button.click()
        self.assertEqual(host.called, 1)

    # 4. テスト印刷は画面上の現在値を settings_override として渡す（保存済みは使わない）
    def test_uses_current_screen_values(self) -> None:
        dialog = self._make_dialog(embedded=True, printer_name="SavedPrinter")
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("EditedPrinter", "EditedPrinter")
        dialog._printer_combo.setCurrentIndex(0)

        captured: dict[str, object] = {}

        class FakeHost:
            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
                captured["override"] = settings_override
                return True

        with mock.patch.object(dialog, "_find_test_print_host", return_value=FakeHost()):
            dialog._on_test_print_clicked()

        self.assertIsNotNone(captured.get("override"))
        self.assertEqual(captured["override"].printer_name, "EditedPrinter")

    # 4b. テスト印刷だけでは save_voucher_printer_settings() を呼ばない（要件・永続保存しない）
    def test_does_not_persist_settings(self) -> None:
        dialog = self._make_dialog(embedded=True, printer_name="SavedPrinter")
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("EditedPrinter", "EditedPrinter")
        dialog._printer_combo.setCurrentIndex(0)

        class FakeHost:
            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
                return True

        with mock.patch(
            "app.voucher_window.save_voucher_printer_settings"
        ) as save, mock.patch.object(
            dialog, "_find_test_print_host", return_value=FakeHost()
        ):
            dialog._on_test_print_clicked()

        save.assert_not_called()

    # 4c. テスト印刷しても QSettings 上の保存値は変わらない（キャンセル相当の永続化なし）
    def test_saved_settings_unchanged_after_test_print(self) -> None:
        from app.voucher_settings import (
            load_voucher_printer_settings,
            save_voucher_printer_settings,
            VoucherPrinterSettings,
        )
        from app.voucher_window import VoucherPrinterSettingsDialog

        save_voucher_printer_settings(
            VoucherPrinterSettings(printer_name="SavedPrinter", print_backend="acrobat")
        )
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings",
            return_value=load_voucher_printer_settings(),
        ):
            dialog = VoucherPrinterSettingsDialog(embedded=True)
        self.addCleanup(dialog.deleteLater)
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("EditedPrinter", "EditedPrinter")
        dialog._printer_combo.setCurrentIndex(0)

        class FakeHost:
            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
                return True

        with mock.patch.object(dialog, "_find_test_print_host", return_value=FakeHost()):
            dialog._on_test_print_clicked()

        self.assertEqual(load_voucher_printer_settings().printer_name, "SavedPrinter")

    # 4d. テスト印刷が失敗しても保存済み設定は変わらない
    def test_saved_settings_unchanged_on_failure(self) -> None:
        from app.voucher_settings import (
            load_voucher_printer_settings,
            save_voucher_printer_settings,
            VoucherPrinterSettings,
        )
        from app.voucher_window import VoucherPrinterSettingsDialog

        save_voucher_printer_settings(
            VoucherPrinterSettings(printer_name="SavedPrinter", print_backend="acrobat")
        )
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings",
            return_value=load_voucher_printer_settings(),
        ):
            dialog = VoucherPrinterSettingsDialog(embedded=True)
        self.addCleanup(dialog.deleteLater)
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("EditedPrinter", "EditedPrinter")
        dialog._printer_combo.setCurrentIndex(0)

        class FakeHost:
            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
                raise RuntimeError("boom")

        with mock.patch.object(dialog, "_find_test_print_host", return_value=FakeHost()):
            dialog._on_test_print_clicked()

        self.assertEqual(load_voucher_printer_settings().printer_name, "SavedPrinter")

    # 5. 単独ダイアログでもテスト印刷が動く（同じく override を渡し、保存しない）
    def test_standalone_invokes_host(self) -> None:
        from PySide6.QtWidgets import QWidget

        class FakeHost(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.called = 0
                self.override = "unset"

            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:  # noqa: N802
                self.called += 1
                self.override = settings_override
                return True

        host = FakeHost()
        self.addCleanup(host.deleteLater)
        from app.voucher_settings import VoucherPrinterSettings
        from app.voucher_window import VoucherPrinterSettingsDialog

        settings = VoucherPrinterSettings(printer_name="P", print_backend="acrobat")
        with mock.patch(
            "app.voucher_window.load_voucher_printer_settings", return_value=settings
        ):
            dialog = VoucherPrinterSettingsDialog(parent=host)
        self.addCleanup(dialog.deleteLater)
        with mock.patch("app.voucher_window.save_voucher_printer_settings") as save:
            dialog._on_test_print_clicked()
        self.assertEqual(host.called, 1)
        self.assertIsNotNone(host.override)
        save.assert_not_called()

    # 6. プリンター未選択時はエラー表示され、印刷は実行されない
    def test_printer_empty_shows_error(self) -> None:
        dialog = self._make_dialog(embedded=True)
        dialog._printer_combo.clear()
        with mock.patch(
            "app.voucher_window.save_voucher_printer_settings"
        ) as save, mock.patch.object(dialog, "_find_test_print_host") as find:
            dialog._on_test_print_clicked()
        save.assert_not_called()
        find.assert_not_called()
        self.assertIn("プリンター", dialog._status_label.text())

    # 7. 印刷処理で例外が起きてもログを残し無反応にしない
    def test_enqueue_exception_logs_and_shows_status(self) -> None:
        from app import voucher_print_service

        dialog = self._make_dialog(embedded=True)
        dialog._printer_combo.clear()
        dialog._printer_combo.addItem("P", "P")
        dialog._printer_combo.setCurrentIndex(0)

        class FakeHost:
            def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
                raise RuntimeError("boom")

        with mock.patch.object(dialog, "_find_test_print_host", return_value=FakeHost()), \
            mock.patch.object(
                voucher_print_service, "log_print_settings_event"
            ) as log:
            dialog._on_test_print_clicked()

        events = [c.args[0] for c in log.call_args_list]
        self.assertIn("voucher_print_settings_test_print_failed", events)
        self.assertIn("失敗", dialog._status_label.text())


if __name__ == "__main__":
    unittest.main()
