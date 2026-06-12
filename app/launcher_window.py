"""起動時の機能選択画面。

アプリ起動時に最初に表示される画面。
OLAPアカウントとkintoneアカウントを入力し、使用する機能を選択する。
子画面起動中はUI全体をロックし、子画面終了後にロック解除・前面表示する。
"""
from __future__ import annotations

import logging

from dotenv import dotenv_values

from PySide6.QtCore import QSettings, Qt
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
    resource_path,
    update_values_in_config,
    user_config_path,
)
from app.update_client import UpdateClient, default_update_dir, launch_external_update
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.version import VERSION_CODE, VERSION_NAME

_LOGGER = logging.getLogger("tks_to_kintone_app")

_SETTINGS_DEBUG_VISIBLE = "ui/debug_visible"
_NGS_DEBUG_VISIBLE_KEY = "NGS_DEBUG_VISIBLE"

_SETTINGS_ORG = "Manekiya"
_SETTINGS_APP = "TksToKintone"
_SETTINGS_LOGIN_ID = "olap/login_id"
_SETTINGS_PASSWORD = "olap/password"
_SETTINGS_KINTONE_LOGIN_ID = "kintone/login_id"
_SETTINGS_KINTONE_PASSWORD = "kintone/password"
_SETTINGS_THEME = "ui/theme"
_THEME_SYSTEM = "system"
_THEME_LABELS = {
    _THEME_SYSTEM: "システム",
    "light": "ライト",
    "dark": "ダーク",
}


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
        self.debug_visible.setChecked(_checked)
        _LOGGER.info("debug_visible loaded=%s", _checked)

        form = QFormLayout()
        form.addRow("テーマカラー", self.theme)
        form.addRow("デバッグ表示", self.debug_visible)

        version = QGroupBox("バージョン情報")
        version_form = QFormLayout()
        version_form.addRow("バージョンネーム", QLabel(VERSION_NAME))
        version_form.addRow("バージョンコード", QLabel(str(VERSION_CODE)))
        self.update_button = QPushButton("更新確認")
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

    def check_update(self) -> None:
        try:
            info = UpdateClient().check_for_update(VERSION_CODE)
        except Exception as exc:
            QMessageBox.warning(self, "更新確認失敗", str(exc))
            return
        if info is None:
            QMessageBox.information(self, "更新確認", "現在のバージョンは最新です。")
            return
        release_notes = f"\n\nリリースノート:\n{info.release_notes}" if info.release_notes else ""
        QMessageBox.information(
            self,
            "更新確認",
            "新しいバージョンが見つかりました。\n\n"
            f"現在: {VERSION_NAME} (コード {VERSION_CODE})\n"
            f"新しいバージョン: {info.version_name} (コード {info.version_code})\n"
            f"ファイル名: {info.file_name}"
            f"{release_notes}\n\n"
            "更新ファイルをダウンロードして適用します。",
        )
        try:
            import sys
            from pathlib import Path

            launch_external_update(info, default_update_dir(), Path(sys.executable).resolve())
        except Exception as exc:
            QMessageBox.warning(self, "更新失敗", str(exc))
            return
        QApplication.quit()

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
        self._settings_btn.setFixedSize(34, 34)
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
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        login_id = str(settings.value(_SETTINGS_LOGIN_ID, "") or "")
        password = str(settings.value(_SETTINGS_PASSWORD, "") or "")
        if login_id:
            self._olap_id.setText(login_id)
        if password:
            self._olap_password.setText(password)
        kintone_login_id = str(settings.value(_SETTINGS_KINTONE_LOGIN_ID, "") or "")
        kintone_password = str(settings.value(_SETTINGS_KINTONE_PASSWORD, "") or "")
        try:
            values = dotenv_values(user_config_path())
            kintone_login_id = str(values.get(KINTONE_LOGIN_ID_ENV_KEY) or kintone_login_id)
            kintone_password = str(values.get(KINTONE_PASSWORD_ENV_KEY) or kintone_password)
        except Exception:
            pass
        if kintone_login_id:
            self._kintone_id.setText(kintone_login_id)
        if kintone_password:
            self._kintone_password.setText(kintone_password)

    def _save_kintone_credentials(self) -> None:
        login_id = self._kintone_id.text().strip()
        password = self._kintone_password.text()
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(_SETTINGS_KINTONE_LOGIN_ID, login_id)
        settings.setValue(_SETTINGS_KINTONE_PASSWORD, password)
        settings.sync()
        update_values_in_config(
            user_config_path(),
            {
                KINTONE_LOGIN_ID_ENV_KEY: login_id,
                KINTONE_PASSWORD_ENV_KEY: password,
            },
        )

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
        from app.voucher_window import VoucherWindow

        win = VoucherWindow(
            olap_login_id=self._olap_id.text().strip(),
            olap_password=self._olap_password.text(),
        )
        win.back_requested.connect(self._on_voucher_closed)
        self._voucher_window = win
        self._update_credential_locks()
        self._update_buttons()
        win.show()

    def _open_kintone(self) -> None:
        if self._main_window is not None:
            self._main_window.show()
            self._main_window.raise_()
            return
        from app.gui import MainWindow, apply_theme, SETTINGS_THEME, THEME_SYSTEM
        from PySide6.QtCore import QSettings

        self._save_kintone_credentials()
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
        win.show()

    def _on_voucher_closed(self, *_args: object) -> None:
        """伝票画面が閉じられたときの後処理。"""
        self._voucher_window = None
        self._update_credential_locks()
        self._bring_launcher_front()

    def _on_kintone_closed(self, *_args: object) -> None:
        """Kintone登録処理画面が閉じられたときの後処理。"""
        self._main_window = None
        self._update_credential_locks()
        self._bring_launcher_front()

    def closeEvent(self, event) -> None:
        """入口画面を閉じたら子画面も閉じてアプリ全体を終了する。"""
        self._closing = True
        for win in (self._voucher_window, self._main_window):
            if win is not None and hasattr(win, "close"):
                win.close()
        QApplication.quit()
        super().closeEvent(event)
