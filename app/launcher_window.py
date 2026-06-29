"""起動時の機能選択画面。

アプリ起動時に最初に表示される画面。
OLAPアカウントとkintoneアカウントを入力し、使用する機能を選択する。
子画面起動中はUI全体をロックし、子画面終了後にロック解除・前面表示する。
"""
from __future__ import annotations

import logging
import importlib
import sys
from pathlib import Path

from dotenv import dotenv_values

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from app.config import (
    KINTONE_LOGIN_ID_ENV_KEY,
    KINTONE_PASSWORD_ENV_KEY,
    default_base_dir,
    load_app_config,
    resource_path,
    update_values_in_config,
    user_config_path,
)
from app.credential_store import (
    load_saved_credentials,
    save_kintone_credentials,
    save_olap_credentials,
)
from app.build_features import updates_enabled
from app.kintone_client import KintoneClient
from app.models import RunInput
from app.tks_client import create_tks_client
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.version import VERSION_CODE, VERSION_NAME

_LOGGER = logging.getLogger("tks_to_kintone_app")

_SETTINGS_DEBUG_VISIBLE = "ui/debug_visible"
_NGS_DEBUG_VISIBLE_KEY = "NGS_DEBUG_VISIBLE"
_DEBUG_VISIBLE_PASSWORD = "admin"

_SETTINGS_ORG = "Manekiya"
_SETTINGS_APP = "TksToKintone"
_SETTINGS_THEME = "ui/theme"
_THEME_SYSTEM = "system"
_THEME_LABELS = {
    _THEME_SYSTEM: "システム",
    "light": "ライト",
    "dark": "ダーク",
}


class _UpdateCheckThread(QThread):
    """更新確認をバックグラウンドで実行し、画面を固まらせないためのワーカー。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:  # noqa: D401 - QThread エントリポイント
        try:
            update_client = _update_client_module()
            self.succeeded.emit(update_client.UpdateClient().check_for_update(VERSION_CODE))
        except Exception as exc:  # noqa: BLE001 - 失敗しても致命エラーにしない
            self.failed.emit(str(exc))


def _update_client_module():
    return importlib.import_module("app." + "update_client")


class LauncherSettingsDialog(QDialog):
    """機能選択画面から開くアプリ共通設定。"""

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(420, 260)
        self.settings = settings

        self.theme = QComboBox()
        for value, label in _THEME_LABELS.items():
            self.theme.addItem(label, value)
        current_theme = str(settings.value(_SETTINGS_THEME, _THEME_SYSTEM) or _THEME_SYSTEM)
        index = self.theme.findData(current_theme)
        self.theme.setCurrentIndex(index if index >= 0 else 0)

        self.debug_visible = QCheckBox("デバッグ項目を表示")
        _raw = settings.value(_SETTINGS_DEBUG_VISIBLE, "0")
        _checked = _raw if isinstance(_raw, bool) else str(_raw).strip().lower() in {"1", "true", "yes", "on"}
        self._initial_debug_visible = bool(_checked)
        self._prompting_debug_password = False
        self.debug_visible.setChecked(_checked)
        self.debug_visible.toggled.connect(self._on_debug_visible_toggled)
        _LOGGER.info("debug_visible loaded=%s", _checked)

        form = QFormLayout()
        form.addRow("テーマカラー", self.theme)
        form.addRow("デバッグ表示", self.debug_visible)

        version = QGroupBox("バージョン情報")
        version_form = QFormLayout()
        version_form.addRow("バージョンネーム", QLabel(VERSION_NAME))
        version_form.addRow("バージョンコード", QLabel(str(VERSION_CODE)))
        self.update_button = QPushButton("更新確認")
        self.update_button.setEnabled(updates_enabled())
        if not updates_enabled():
            self.update_button.setToolTip("このビルドでは更新機能は無効です。")
        version_form.addRow("", self.update_button)
        version.setLayout(version_form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(version)
        root.addStretch(1)
        root.addWidget(buttons)
        self.setLayout(root)
        self.update_button.clicked.connect(self.check_update)

    def _on_debug_visible_toggled(self, checked: bool) -> None:
        if self._prompting_debug_password:
            return
        if not checked:
            return
        if self._initial_debug_visible:
            return
        password, ok = QInputDialog.getText(
            self,
            "デバッグ表示",
            "デバッグ表示を有効にするにはパスワードを入力してください。",
            QLineEdit.EchoMode.Password,
        )
        if ok and password == _DEBUG_VISIBLE_PASSWORD:
            return
        self._prompting_debug_password = True
        try:
            self.debug_visible.setChecked(False)
        finally:
            self._prompting_debug_password = False

    def check_update(self) -> None:
        if not updates_enabled():
            QMessageBox.information(self, "更新確認", "このビルドでは更新機能は無効です。")
            return
        try:
            update_client = _update_client_module()
            info = update_client.UpdateClient().check_for_update(VERSION_CODE)
        except Exception as exc:
            QMessageBox.warning(self, "更新確認失敗", str(exc))
            return
        if info is None:
            QMessageBox.information(self, "更新確認", "現在のバージョンは最新です。")
            return
        release_notes = f"\n\nリリースノート:\n{info.release_notes}" if info.release_notes else ""
        answer = QMessageBox.question(
            self,
            "更新確認",
            "新しいバージョンが見つかりました。\n\n"
            f"現在: {VERSION_NAME} (コード {VERSION_CODE})\n"
            f"新しいバージョン: {info.version_name} (コード {info.version_code})\n"
            f"ファイル名: {info.file_name}"
            f"{release_notes}\n\n"
            "更新ファイルをダウンロードして適用しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            update_client = _update_client_module()
            launch_external_update = update_client.launch_external_update
            default_update_dir = update_client.default_update_dir
            started = launch_external_update(
                info,
                default_update_dir(),
                Path(sys.executable).resolve(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "更新失敗", str(exc))
            return
        if not started:
            QMessageBox.warning(
                self,
                "更新失敗",
                "更新インストーラを起動できませんでした。\n"
                "ログフォルダの update_installer.log を確認してください。",
            )
            return
        from app.gui import quit_app_for_update

        quit_app_for_update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())

    def accept(self) -> None:
        self.settings.setValue(_SETTINGS_THEME, self.theme.currentData())
        checked = self.debug_visible.isChecked()
        self.settings.setValue(_SETTINGS_DEBUG_VISIBLE, "1" if checked else "0")
        self.settings.sync()
        try:
            config_path = default_base_dir() / "config.env"
            update_values_in_config(config_path, {_NGS_DEBUG_VISIBLE_KEY: "1" if checked else "0"})
            _LOGGER.info("debug_visible saved=%s, config.env=%s", checked, config_path)
        except Exception:
            _LOGGER.warning("config.env への NGS_DEBUG_VISIBLE 保存に失敗しました", exc_info=True)
        super().accept()


class LauncherWindow(QMainWindow):
    """起動時の機能選択画面。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        self.resize(420, 380)

        self._main_window: object | None = None
        self._voucher_window: object | None = None
        self._closing = False
        self._update_checked = False
        self._update_check_thread: _UpdateCheckThread | None = None
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self._base_dir = default_base_dir()

        # OLAPアカウント
        self._olap_id = QLineEdit()
        self._olap_password = QLineEdit()
        self._olap_password.setEchoMode(QLineEdit.EchoMode.Password)

        # kintoneアカウント
        self._kintone_id = QLineEdit()
        self._kintone_password = QLineEdit()
        self._kintone_password.setEchoMode(QLineEdit.EchoMode.Password)

        for edit in (self._olap_id, self._olap_password, self._kintone_id, self._kintone_password):
            edit.setStyleSheet("font-size: 12px;")

        # ボタン
        self._voucher_btn = QPushButton("伝票作成・印刷")
        self._kintone_btn = QPushButton("Kintone登録処理")
        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("設定")
        self._settings_btn.setAccessibleName("設定")
        self._settings_btn.setMinimumSize(40, 40)
        self._settings_btn.setStyleSheet("QPushButton { font-size: 20px; padding: 4px; }")
        self._voucher_btn.setEnabled(False)
        self._kintone_btn.setEnabled(False)

        self._open_config_btn = QPushButton("設定フォルダを開く")
        self._open_log_btn = QPushButton("ログフォルダを開く")
        self._open_work_btn = QPushButton("workフォルダを開く")

        self._apply_saved_theme()
        self._build_layout()
        self._load_saved_credentials()
        self._update_buttons()

        self._olap_id.textChanged.connect(self._update_buttons)
        self._olap_password.textChanged.connect(self._update_buttons)
        self._kintone_id.textChanged.connect(self._update_buttons)
        self._kintone_password.textChanged.connect(self._update_buttons)
        self._voucher_btn.clicked.connect(self._open_voucher)
        self._kintone_btn.clicked.connect(self._open_kintone)
        self._settings_btn.clicked.connect(self._open_settings)
        self._open_config_btn.clicked.connect(lambda: self.open_folder("config"))
        self._open_log_btn.clicked.connect(lambda: self.open_folder("log"))
        self._open_work_btn.clicked.connect(lambda: self.open_folder("work"))
        self._apply_debug_visibility()

    def _build_layout(self) -> None:
        olap_group = QGroupBox("OLAPアカウント")
        olap_form = QFormLayout()
        olap_form.addRow("OLAPログインID:", self._olap_id)
        olap_form.addRow("OLAPパスワード:", self._olap_password)
        olap_group.setLayout(olap_form)

        kintone_group = QGroupBox("kintoneアカウント（kintone登録処理で使用）")
        kintone_form = QFormLayout()
        kintone_form.addRow("kintoneログインID:", self._kintone_id)
        kintone_form.addRow("kintoneパスワード:", self._kintone_password)
        kintone_group.setLayout(kintone_form)

        btn_layout = QVBoxLayout()
        btn_layout.addWidget(self._voucher_btn)
        btn_layout.addWidget(self._kintone_btn)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self._open_config_btn)
        folder_row.addWidget(self._open_log_btn)
        folder_row.addWidget(self._open_work_btn)

        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self._settings_btn)

        root = QVBoxLayout()
        root.addLayout(top_row)
        root.addWidget(olap_group)
        root.addWidget(kintone_group)
        root.addStretch(1)
        root.addLayout(folder_row)
        root.addLayout(btn_layout)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())
        # 機能選択画面が表示されたタイミングで一度だけ更新確認する。
        self._check_update_once_after_show()

    def _check_update_once_after_show(self) -> None:
        if not updates_enabled():
            self._update_checked = True
            return
        if self._update_checked:
            return
        self._update_checked = True
        # 画面表示処理が完了してから確認を開始する。
        QTimer.singleShot(0, self.check_update_on_launcher_start)

    def check_update_on_launcher_start(self) -> None:
        """機能選択画面表示後にバックグラウンドで更新確認を開始する。"""
        if not updates_enabled():
            return
        if self._closing:
            return
        if self._update_check_thread is not None and self._update_check_thread.isRunning():
            return
        thread = _UpdateCheckThread(self)
        thread.succeeded.connect(self._on_launch_update_check_succeeded)
        thread.failed.connect(self._on_launch_update_check_failed)
        self._update_check_thread = thread
        thread.start()

    def _on_launch_update_check_succeeded(self, info: object) -> None:
        # 更新なしの場合は通知しない（ログのみ）。
        if info is None:
            _LOGGER.info("更新確認結果: 最新です。")
            return
        self._prompt_and_start_update(info)

    def _on_launch_update_check_failed(self, message: str) -> None:
        # 更新確認に失敗しても致命エラーにせず、機能選択画面はそのまま使用可能にする。
        _LOGGER.warning("更新確認に失敗しました: %s", message)

    def _prompt_and_start_update(self, info: object) -> None:
        if self._closing:
            return
        release_notes = f"\n\nリリースノート:\n{info.release_notes}" if info.release_notes else ""
        answer = QMessageBox.question(
            self,
            "更新の確認",
            "新しいバージョンが見つかりました。\n\n"
            f"現在: {VERSION_NAME} (コード {VERSION_CODE})\n"
            f"新しいバージョン: {info.version_name} (コード {info.version_code})\n"
            f"ファイル名: {info.file_name}"
            f"{release_notes}\n\n"
            "更新ファイルをダウンロードして適用しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # ユーザーが「はい（更新する）」を選んだ場合だけ更新インストーラを開始する。
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            update_client = _update_client_module()
            launch_external_update = update_client.launch_external_update
            default_update_dir = update_client.default_update_dir
            started = launch_external_update(
                info,
                default_update_dir(),
                Path(sys.executable).resolve(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "更新失敗", str(exc))
            return
        if not started:
            # インストーラ起動失敗を握りつぶさない。本体は終了せず、原因を案内する。
            QMessageBox.warning(
                self,
                "更新失敗",
                "更新インストーラを起動できませんでした。\n"
                "ログフォルダの update_installer.log を確認してください。",
            )
            return
        # インストーラ起動が成功したので本体を速やかに終了する。
        from app.gui import quit_app_for_update

        quit_app_for_update()

    def _apply_saved_theme(self) -> None:
        from app.gui import apply_theme

        apply_theme(str(self._settings.value(_SETTINGS_THEME, _THEME_SYSTEM) or _THEME_SYSTEM))

    def _open_settings(self) -> None:
        dialog = LauncherSettingsDialog(self._settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_saved_theme()
            self._apply_debug_visibility()
            if self._main_window is not None and hasattr(self._main_window, "_apply_debug_visibility"):
                self._main_window._apply_debug_visibility()

    def _apply_debug_visibility(self) -> None:
        raw = self._settings.value(_SETTINGS_DEBUG_VISIBLE, "0")
        visible = raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}
        self._open_config_btn.setVisible(visible)
        self._open_log_btn.setVisible(visible)
        self._open_work_btn.setVisible(visible)
        _LOGGER.info("launcher folder buttons visible=%s", visible)

    def open_folder(self, target: str) -> None:
        paths = {
            "config": self._base_dir,
            "log": self._base_dir / "logs",
            "work": self._base_dir / "work",
        }
        path = paths[target]
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "フォルダを開けません", str(path))

    def _load_saved_credentials(self) -> None:
        """保存済みログイン情報を読み込み各入力欄へ反映する。失敗しても空欄で続行する。"""
        saved = load_saved_credentials()
        olap_login_id = saved.olap_login_id
        olap_password = saved.olap_password
        kintone_login_id = saved.kintone_login_id
        kintone_password = saved.kintone_password
        # 旧バージョンが config.env に平文保存した kintone 情報からの移行読み込み。
        if not kintone_login_id or not kintone_password:
            try:
                values = dotenv_values(user_config_path())
                kintone_login_id = kintone_login_id or str(values.get(KINTONE_LOGIN_ID_ENV_KEY) or "")
                kintone_password = kintone_password or str(values.get(KINTONE_PASSWORD_ENV_KEY) or "")
            except Exception:
                pass
        if olap_login_id:
            self._olap_id.setText(olap_login_id)
        if olap_password:
            self._olap_password.setText(olap_password)
        if kintone_login_id:
            self._kintone_id.setText(kintone_login_id)
        if kintone_password:
            self._kintone_password.setText(kintone_password)

    def _save_kintone_credentials(self) -> None:
        """kintoneログイン情報を保存する（平文 config.env ではなく資格情報ストアへ）。"""
        save_kintone_credentials(self._kintone_id.text().strip(), self._kintone_password.text())

    def _update_credential_locks(self) -> None:
        """子画面の起動状態に応じてログイン情報欄をロック/解除する。"""
        olap_enabled = self._voucher_window is None and self._main_window is None
        kintone_enabled = self._main_window is None
        self._olap_id.setEnabled(olap_enabled)
        self._olap_password.setEnabled(olap_enabled)
        self._kintone_id.setEnabled(kintone_enabled)
        self._kintone_password.setEnabled(kintone_enabled)

    def _bring_launcher_front(self) -> None:
        """子画面終了後に機能選択画面を前面表示する。"""
        if self._closing:
            return
        self._update_buttons()
        self.show()
        self.raise_()
        self.activateWindow()

    def activate_existing_instance(self) -> None:
        """二重起動要求を受けたとき、既存の入口画面または子画面を前面に出す。"""
        target = self._main_window or self._voucher_window or self
        if hasattr(target, "showNormal"):
            target.showNormal()
        if hasattr(target, "show"):
            target.show()
        if hasattr(target, "raise_"):
            target.raise_()
        if hasattr(target, "activateWindow"):
            target.activateWindow()

    def _update_buttons(self) -> None:
        olap_ok = bool(self._olap_id.text().strip() and self._olap_password.text())
        kintone_ok = olap_ok and bool(
            self._kintone_id.text().strip() and self._kintone_password.text()
        )
        self._voucher_btn.setEnabled(olap_ok and self._voucher_window is None)
        self._kintone_btn.setEnabled(kintone_ok and self._main_window is None)

    def _open_voucher(self) -> None:
        if self._voucher_window is not None:
            self._voucher_window.show()
            self._voucher_window.raise_()
            return
        if not self._authorize_olap():
            return
        self._launch_voucher_window()

    def _open_kintone(self) -> None:
        if self._main_window is not None:
            self._main_window.show()
            self._main_window.raise_()
            return
        if not self._authorize_kintone():
            return
        self._launch_kintone_window()

    def _authorize_olap(self) -> bool:
        """OLAPログインチェックを行い、成功したらOLAP情報を保存する。"""
        olap_id = self._olap_id.text().strip()
        olap_password = self._olap_password.text()
        config = self._load_config_or_warn()
        if config is None:
            return False
        if not self._verify_olap_login(config, olap_id, olap_password):
            return False
        save_olap_credentials(olap_id, olap_password)
        return True

    def _authorize_kintone(self) -> bool:
        """OLAPログインチェックとkintone接続チェックの両方が成功したら両方を保存する。"""
        olap_id = self._olap_id.text().strip()
        olap_password = self._olap_password.text()
        kintone_id = self._kintone_id.text().strip()
        kintone_password = self._kintone_password.text()
        config = self._load_config_or_warn()
        if config is None:
            return False
        if not self._verify_olap_login(config, olap_id, olap_password):
            return False
        if not self._verify_kintone_connection(config):
            return False
        save_olap_credentials(olap_id, olap_password)
        save_kintone_credentials(kintone_id, kintone_password)
        return True

    def _load_config_or_warn(self) -> object | None:
        try:
            return load_app_config()
        except Exception as exc:
            QMessageBox.critical(self, "設定エラー", str(exc))
            return None

    def _verify_olap_login(self, config: object, login_id: str, password: str) -> bool:
        run_input = RunInput(
            company_code=config.company_code,
            olap_login_id=login_id,
            olap_password=password,
            denpyo_numbers=[],
            shiage_date="",
            shukka_kbn="",
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            create_tks_client(config, _LOGGER).login(run_input)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "OLAPログイン失敗", str(exc))
            return False
        QApplication.restoreOverrideCursor()
        return True

    def _verify_kintone_connection(self, config: object) -> bool:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            KintoneClient(config, _LOGGER).check_connection()
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "kintone接続失敗", str(exc))
            return False
        QApplication.restoreOverrideCursor()
        return True

    def _launch_voucher_window(self) -> None:
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(
            olap_login_id=self._olap_id.text().strip(),
            olap_password=self._olap_password.text(),
            # 伝票画面から都度ランチャー保持のKintone登録処理画面を参照できるようにする。
            kintone_window_provider=lambda: self._main_window,
        )
        win.back_requested.connect(self._on_voucher_closed)
        self._voucher_window = win
        self._update_credential_locks()
        self._update_buttons()
        # Kintone登録処理画面が既に開いている場合は受注No変更シグナルを接続し、
        # 現在の起動状態を反映する（要件3・6）。
        self._connect_kintone_voucher_sync()
        self._notify_voucher_kintone_state()
        win.show()

    def _launch_kintone_window(self) -> None:
        from app.gui import MainWindow, apply_theme, SETTINGS_THEME, THEME_SYSTEM
        from PySide6.QtCore import QSettings

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        apply_theme(str(settings.value(SETTINGS_THEME, THEME_SYSTEM) or THEME_SYSTEM))

        win = MainWindow(
            initial_olap_id=self._olap_id.text().strip(),
            initial_olap_password=self._olap_password.text(),
        )
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        win.destroyed.connect(self._on_kintone_closed)
        self._main_window = win
        self._update_credential_locks()
        self._update_buttons()
        # 伝票画面が既に開いている場合は受注No変更シグナルを接続し、
        # 「Kintone登録」ボタン状態を同期させる（要件3・6）。
        self._connect_kintone_voucher_sync()
        self._notify_voucher_kintone_state()
        win.show()

    def _notify_voucher_kintone_state(self) -> None:
        """伝票画面へKintone登録処理画面の起動状態を通知しボタンを同期させる。"""
        win = self._voucher_window
        if win is not None and hasattr(win, "refresh_kintone_buttons"):
            win.refresh_kintone_buttons()

    def _connect_kintone_voucher_sync(self) -> None:
        """Kintone登録処理画面の受注No変更を伝票画面のボタン更新へ接続する（要件6）。

        両画面が揃っているときのみ接続する。二重接続・参照切れは無視する。
        """
        win_k = self._main_window
        win_v = self._voucher_window
        if win_k is None or win_v is None:
            return
        if not hasattr(win_k, "order_numbers_changed") or not hasattr(win_v, "refresh_kintone_buttons"):
            return
        try:
            win_k.order_numbers_changed.connect(
                win_v.refresh_kintone_buttons, Qt.ConnectionType.UniqueConnection
            )
        except (TypeError, RuntimeError):
            # 既に接続済み（UniqueConnection）等は無視する。
            pass
        if hasattr(win_k, "kintone_registration_completed") and hasattr(
            win_v, "notify_kintone_registration_completed"
        ):
            try:
                win_k.kintone_registration_completed.connect(
                    win_v.notify_kintone_registration_completed, Qt.ConnectionType.UniqueConnection
                )
            except (TypeError, RuntimeError):
                pass

    def _disconnect_kintone_voucher_sync(self) -> None:
        """受注No変更シグナルの接続を解除する。参照切れ・未接続は安全に無視する（要件6）。"""
        win_k = self._main_window
        win_v = self._voucher_window
        if win_k is None or win_v is None:
            return
        try:
            # 破棄済みQObjectへのアクセスは RuntimeError になるため広めに保護する。
            win_k.order_numbers_changed.disconnect(win_v.refresh_kintone_buttons)
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            win_k.kintone_registration_completed.disconnect(win_v.notify_kintone_registration_completed)
        except (TypeError, RuntimeError, AttributeError):
            pass

    def _on_voucher_closed(self, *_args: object) -> None:
        """伝票画面が閉じられたときの後処理。"""
        # 伝票画面消滅前にシグナル接続を解除する（参照切れ防止: 要件6）。
        self._disconnect_kintone_voucher_sync()
        self._voucher_window = None
        self._update_credential_locks()
        self._bring_launcher_front()

    def _on_kintone_closed(self, *_args: object) -> None:
        """Kintone登録処理画面が閉じられたときの後処理。"""
        # Kintone画面はWA_DeleteOnCloseで破棄され接続も自動解除されるが、念のため外す。
        self._disconnect_kintone_voucher_sync()
        self._main_window = None
        self._update_credential_locks()
        # 伝票画面が開いている場合は全行を「Kintone登録」表示・無効へ戻す（要件3ケース3）。
        self._notify_voucher_kintone_state()
        self._bring_launcher_front()

    def closeEvent(self, event) -> None:
        """入口画面を閉じたら子画面も閉じてアプリ全体を終了する。"""
        self._closing = True
        if self._update_check_thread is not None and self._update_check_thread.isRunning():
            self._update_check_thread.quit()
            self._update_check_thread.wait(1000)
        for win in (self._voucher_window, self._main_window):
            if win is not None and hasattr(win, "close"):
                win.close()
        QApplication.quit()
        super().closeEvent(event)
