from __future__ import annotations

import unittest
from pathlib import Path


GUI_SOURCE = Path("app/gui.py").read_text(encoding="utf-8")
LAUNCHER_SOURCE = Path("app/launcher_window.py").read_text(encoding="utf-8")


class GuiFlowStaticTest(unittest.TestCase):
    def test_dry_run_ui_and_branch_are_removed(self) -> None:
        self.assertNotIn("DRY_RUN", GUI_SOURCE)
        self.assertNotIn("dry_run", GUI_SOURCE)

    def test_worker_always_emits_pending_registration_before_kintone_register(self) -> None:
        worker_source = _slice("class WorkerThread", "class KintoneRegisterWorkerThread")
        self.assertIn("self.pending_registration.emit", worker_source)
        self.assertNotIn("register_rows(", worker_source)

    def test_kintone_register_only_runs_in_registration_worker(self) -> None:
        register_source = _slice("class KintoneRegisterWorkerThread", "class TksDebugWorkerThread")
        self.assertIn("KintoneClient(self.config, logger).register_rows(self.rows)", register_source)

    def test_preview_print_button_is_guarded_by_debug_visible(self) -> None:
        preview_source = _slice("class RegistrationPreviewDialog", "class AdvancedSettingsDialog")
        self.assertIn('QPushButton("印刷")', preview_source)
        self.assertIn("if debug_visible", preview_source)

    def test_preview_receives_debug_visible_setting_from_main_window(self) -> None:
        start_source = _slice("def start_run", "def start_tks_login_test")
        self.assertIn("debug_visible=_settings_bool(self.settings, SETTINGS_DEBUG_VISIBLE, False)", start_source)

    def test_theme_and_version_settings_live_on_launcher(self) -> None:
        launcher_settings = _slice_source(
            LAUNCHER_SOURCE,
            "class LauncherSettingsDialog",
            "class LauncherWindow",
        )
        self.assertIn("テーマカラー", launcher_settings)
        self.assertIn("バージョン情報", launcher_settings)
        self.assertIn("VERSION_NAME", launcher_settings)
        self.assertIn("VERSION_CODE", launcher_settings)

    def test_kintone_settings_dialog_excludes_theme_and_version(self) -> None:
        kintone_settings = _slice("class SettingsDialog", "class MainWindow")
        self.assertNotIn("テーマカラー", kintone_settings)
        self.assertNotIn("バージョン情報", kintone_settings)
        self.assertIn("Kintone接続先", kintone_settings)

    def test_settings_buttons_use_gear_icon(self) -> None:
        self.assertIn('self._settings_btn = QPushButton("⚙")', LAUNCHER_SOURCE)
        self.assertIn('self.settings_button = QPushButton("⚙")', GUI_SOURCE)
        self.assertNotIn('QPushButton("設定")', LAUNCHER_SOURCE)
        self.assertNotIn('QPushButton("設定")', GUI_SOURCE)

    def test_kintone_settings_gear_is_top_right(self) -> None:
        layout_source = _slice("def _build_layout", "def closeEvent")
        self.assertIn("settings_top_row = QHBoxLayout()", layout_source)
        self.assertIn("settings_top_row.addStretch(1)", layout_source)
        self.assertIn("settings_top_row.addWidget(self.settings_button)", layout_source)
        self.assertIn("root.addLayout(settings_top_row)", layout_source)
        self.assertLess(
            layout_source.index("root.addLayout(settings_top_row)"),
            layout_source.index("root.addWidget(input_group)"),
        )

    def test_kintone_screen_excludes_olap_account_inputs(self) -> None:
        layout_source = _slice("def _build_layout", "def closeEvent")
        for label in ("契約会社コード", "OLAPログインID", "OLAPパスワード"):
            self.assertNotIn(label, layout_source)
        for attr in ("self.company_code", "self.login_id", "self.password"):
            self.assertNotIn(attr, layout_source)

    def test_kintone_input_validation_does_not_require_olap_fields(self) -> None:
        collect_source = _slice("def _collect_input", "def get_order_numbers")
        self.assertNotIn("OLAPログインIDを入力してください。", collect_source)
        self.assertNotIn("OLAPパスワードを入力してください。", collect_source)
        self.assertIn("受注Noを1件以上入力してください。", collect_source)
        self.assertIn("olap_login_id=self._olap_login_id", collect_source)
        self.assertIn("olap_password=self._olap_password", collect_source)

    def test_kintone_screen_does_not_save_olap_credentials(self) -> None:
        main_source = _slice("class MainWindow", "def quit_app_for_update")
        self.assertNotIn("def _save_credentials", main_source)
        self.assertNotIn("SETTINGS_LOGIN_ID", main_source)
        self.assertNotIn("SETTINGS_PASSWORD", main_source)

    def test_folder_buttons_exist_in_launcher(self) -> None:
        self.assertIn('QPushButton("設定フォルダを開く")', LAUNCHER_SOURCE)
        self.assertIn('QPushButton("ログフォルダを開く")', LAUNCHER_SOURCE)
        self.assertIn('QPushButton("workフォルダを開く")', LAUNCHER_SOURCE)

    def test_folder_buttons_not_visible_in_kintone_screen(self) -> None:
        """フォルダボタンはKintone登録処理画面で常時非表示。"""
        debug_vis = _slice("def _apply_debug_visibility", "def run_gui")
        self.assertNotIn("open_config_button", debug_vis.split("widget.setVisible(visible)")[0])
        self.assertIn("open_config_button", debug_vis)
        self.assertIn("setVisible(False)", debug_vis)

    def test_debug_visible_in_launcher_settings(self) -> None:
        launcher_settings = _slice_source(
            LAUNCHER_SOURCE,
            "class LauncherSettingsDialog",
            "class LauncherWindow",
        )
        self.assertIn("デバッグ表示", launcher_settings)
        self.assertIn("debug_visible", launcher_settings)

    def test_debug_visible_removed_from_kintone_settings(self) -> None:
        kintone_settings = _slice("class SettingsDialog", "class MainWindow")
        self.assertNotIn("デバッグ表示", kintone_settings)
        self.assertNotIn("self.debug_visible", kintone_settings)

    def test_debug_visible_setting_written_by_launcher(self) -> None:
        launcher_settings = _slice_source(
            LAUNCHER_SOURCE,
            "class LauncherSettingsDialog",
            "class LauncherWindow",
        )
        self.assertIn("SETTINGS_DEBUG_VISIBLE", launcher_settings)
        self.assertIn("debug_visible.isChecked()", launcher_settings)

    def test_debug_visible_saved_as_zero_and_one(self) -> None:
        """OFFは "0"、ONは "1" として保存する。"""
        accept_source = _slice_source(
            LAUNCHER_SOURCE,
            "class LauncherSettingsDialog",
            "class LauncherWindow",
        )
        self.assertIn('"0"', accept_source)
        self.assertIn('"1"', accept_source)

    def test_launcher_has_apply_debug_visibility(self) -> None:
        self.assertIn("def _apply_debug_visibility(self)", LAUNCHER_SOURCE)

    def test_launcher_open_settings_calls_apply_debug_visibility(self) -> None:
        open_settings_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _open_settings",
            "def _apply_debug_visibility",
        )
        self.assertIn("_apply_debug_visibility()", open_settings_source)

    def test_launcher_folder_buttons_controlled_by_debug_visibility(self) -> None:
        apply_vis_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _apply_debug_visibility",
            "def open_folder",
        )
        self.assertIn("_open_config_btn", apply_vis_source)
        self.assertIn("_open_log_btn", apply_vis_source)
        self.assertIn("_open_work_btn", apply_vis_source)
        self.assertIn("setVisible(visible)", apply_vis_source)

    def test_launcher_apply_debug_visibility_called_on_init(self) -> None:
        init_source = _slice_source(
            LAUNCHER_SOURCE,
            "def __init__(self) -> None:",
            "def _build_layout",
        )
        self.assertIn("_apply_debug_visibility()", init_source)

    def test_light_theme_checkbox_indicator_has_border(self) -> None:
        light_source = _slice("LIGHT_STYLESHEET", "DARK_STYLESHEET")
        self.assertIn("QCheckBox::indicator", light_source)
        self.assertIn("border: 1px solid", light_source)
        self.assertIn("QCheckBox::indicator:checked", light_source)

    def test_dark_theme_checkbox_indicator_has_border(self) -> None:
        dark_source = GUI_SOURCE[GUI_SOURCE.index("DARK_STYLESHEET"):]
        self.assertIn("QCheckBox::indicator", dark_source)
        self.assertIn("border: 1px solid", dark_source)
        self.assertIn("QCheckBox::indicator:checked", dark_source)

    def test_light_theme_checked_checkbox_shows_checkmark_image(self) -> None:
        light_source = _slice("LIGHT_STYLESHEET", "DARK_STYLESHEET")
        checked = _slice_source(light_source, "QCheckBox::indicator:checked", "QCheckBox::indicator:unchecked")
        self.assertIn("image: url(", checked)

    def test_dark_theme_checked_checkbox_shows_checkmark_image(self) -> None:
        dark_source = GUI_SOURCE[GUI_SOURCE.index("DARK_STYLESHEET"):]
        checked = _slice_source(dark_source, "QCheckBox::indicator:checked", "QCheckBox::indicator:unchecked")
        self.assertIn("image: url(", checked)

    def test_checkmark_assets_exist(self) -> None:
        self.assertTrue(Path("assets/check_white.svg").exists())
        self.assertTrue(Path("assets/check_dark.svg").exists())

    def test_light_theme_has_checked_button_highlight(self) -> None:
        light_source = _slice("LIGHT_STYLESHEET", "DARK_STYLESHEET")
        self.assertIn("QPushButton:checked, QToolButton:checked", light_source)
        self.assertIn("QPushButton:hover, QToolButton:hover", light_source)

    def test_dark_theme_has_checked_button_highlight(self) -> None:
        dark_source = GUI_SOURCE[GUI_SOURCE.index("DARK_STYLESHEET"):]
        self.assertIn("QPushButton:checked, QToolButton:checked", dark_source)
        self.assertIn("QPushButton:hover, QToolButton:hover", dark_source)

    def test_apply_theme_injects_checkmark_asset_path(self) -> None:
        apply_theme_source = _slice("def apply_theme", "LIGHT_STYLESHEET")
        self.assertIn("_with_checkmark_assets", apply_theme_source)

    def test_apply_theme_updates_title_bar_theme(self) -> None:
        apply_theme_source = _slice("def apply_theme", "LIGHT_STYLESHEET")
        self.assertIn("apply_title_bar_theme_to_top_level_widgets", apply_theme_source)
        self.assertIn("apply_app_font_size()", apply_theme_source)

    def test_ui_font_size_is_larger_than_default(self) -> None:
        # 年配の方でも見やすいよう標準フォントを12ptへ拡大（要件4）。
        self.assertIn("font-size: 12pt", GUI_SOURCE)


    def test_update_check_runs_after_launcher_shown_once(self) -> None:
        show_source = _slice_source(LAUNCHER_SOURCE, "def showEvent", "def _check_update_once_after_show")
        self.assertIn("_check_update_once_after_show()", show_source)
        once_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _check_update_once_after_show",
            "def check_update_on_launcher_start",
        )
        # 一度だけ実行するためのガードと、画面表示後に開始する QTimer.singleShot。
        self.assertIn("self._update_checked", once_source)
        self.assertIn("QTimer.singleShot(0, self.check_update_on_launcher_start)", once_source)

    def test_launcher_update_check_runs_in_background_thread(self) -> None:
        self.assertIn("class _UpdateCheckThread(QThread)", LAUNCHER_SOURCE)
        start_source = _slice_source(
            LAUNCHER_SOURCE,
            "def check_update_on_launcher_start",
            "def _on_launch_update_check_succeeded",
        )
        self.assertIn("_UpdateCheckThread(self)", start_source)
        self.assertIn(".start()", start_source)

    def test_launcher_no_update_when_latest(self) -> None:
        succeeded_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _on_launch_update_check_succeeded",
            "def _on_launch_update_check_failed",
        )
        # info is None の場合は通知せずログのみ。
        self.assertIn("if info is None", succeeded_source)
        self.assertNotIn("QMessageBox", succeeded_source)

    def test_launcher_update_check_failure_is_not_fatal(self) -> None:
        failed_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _on_launch_update_check_failed",
            "def _prompt_and_start_update",
        )
        # 失敗時はログのみで、機能選択画面を閉じない（quit/close しない）。
        self.assertIn("_LOGGER.warning", failed_source)
        self.assertNotIn("QApplication.quit", failed_source)
        self.assertNotIn("self.close", failed_source)

    def test_launcher_starts_update_only_when_user_accepts(self) -> None:
        prompt_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _prompt_and_start_update",
            "def _apply_saved_theme",
        )
        self.assertIn("QMessageBox.question", prompt_source)
        self.assertIn("if answer != QMessageBox.StandardButton.Yes", prompt_source)
        # 更新を承諾したときだけ共通の進捗ダイアログを開始する。
        self.assertIn("_run_update_and_quit", prompt_source)
        accept_index = prompt_source.index("if answer != QMessageBox.StandardButton.Yes")
        launch_index = prompt_source.index("_run_update_and_quit")
        self.assertLess(accept_index, launch_index)

    def test_mainwindow_does_not_auto_check_update_on_launch(self) -> None:
        # 起動直後の自動更新確認タイマーは廃止済み。
        self.assertNotIn("_auto_update_timer", GUI_SOURCE)
        self.assertNotIn("start_auto_update_check", GUI_SOURCE)

    def test_no_powershell_in_update_related_sources(self) -> None:
        for forbidden in ("powershell", "ExecutionPolicy", "run_update.ps1", ".ps1"):
            self.assertNotIn(forbidden, GUI_SOURCE)
            self.assertNotIn(forbidden, LAUNCHER_SOURCE)

    def test_gui_has_quit_app_for_update_installer(self) -> None:
        quit_source = _slice("def quit_app_for_update", "def run_gui")
        # quit() だけでなく全ウィンドウを閉じてから終了する。
        self.assertIn("topLevelWidgets()", quit_source)
        self.assertIn(".close()", quit_source)
        self.assertIn("app.quit()", quit_source)

    def test_launcher_delegates_shutdown_exclusively_to_controller(self) -> None:
        update_source = _slice_source(
            LAUNCHER_SOURCE,
            "def _run_update_and_quit",
            "class LauncherSettingsDialog",
        )
        self.assertIn("start_update", update_source)
        self.assertNotIn("quit_app_for_update", update_source)
        self.assertNotIn("run_update_dialog", update_source)

    def test_launcher_settings_dialog_quits_only_after_installer_success(self) -> None:
        dialog_source = _slice_source(LAUNCHER_SOURCE, "def check_update", "def showEvent")
        self.assertIn("_run_update_and_quit", dialog_source)

    def test_worker_emits_display_rows_before_existing_kintone_check_finishes(self) -> None:
        worker_source = _slice("class WorkerThread", "class KintoneRegisterWorkerThread")
        self.assertIn("fetch_existing_records_by_order_numbers", worker_source)
        self.assertIn("self.kintone_checks_ready.emit", worker_source)
        # 通信失敗でも落とさず警告に回す（例外を握ってerrorに格納）。
        self.assertIn("existing_fetch_error", worker_source)
        fetch_index = worker_source.index("fetch_existing_records_by_order_numbers")
        emit_index = worker_source.index("self.pending_registration.emit")
        self.assertLess(emit_index, fetch_index)

    def test_pending_registration_warns_on_fetch_error_and_merges(self) -> None:
        pending_source = _slice("def on_pending_registration", "def _collect_input")
        # 検索失敗でも表示済み行は残し、登録だけを無効にする。
        self.assertIn("existing_fetch_error", pending_source)
        self.assertIn("dialog.fail_progressive_loading", pending_source)
        self.assertIn("RegistrationPrepareWorkerThread", pending_source)
        self.assertNotIn("QMessageBox.question", pending_source)

    def test_gui_start_update_download_delegates_shutdown_to_controller(self) -> None:
        download_source = _slice("def start_update_download", "def _message_parent")
        self.assertIn("start_update", download_source)
        self.assertNotIn("quit_app_for_update", download_source)
        self.assertNotIn("run_update_dialog", download_source)


def _slice(start: str, end: str) -> str:
    return _slice_source(GUI_SOURCE, start, end)


def _slice_source(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


if __name__ == "__main__":
    unittest.main()
