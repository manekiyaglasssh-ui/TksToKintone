from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QSettings, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPalette
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidgetAction,
    QWidget,
)

from app.config import (
    CUSTOMER_LABEL_DEFAULTS,
    CUSTOMER_LABEL_MAX_LEN,
    ConfigError,
    default_base_dir,
    load_app_config,
    resource_path,
    update_customer_labels_in_config,
    validate_customer_label,
)
from app.cleanup_service import cleanup_old_files
from app.csv_processor import create_output_csv, read_output_rows, write_failed_csv, write_input_history, write_output_csv
from app.kakou_master import (
    KAKOU_MASTER_HEADERS,
    CUSTOMER_KEYS,
    CsvEncodingError,
    apply_kakou_names_per_row,
    apply_kakou_names_to_rows,
    backup_master,
    compute_kakou_name_for_order,
    find_unregistered,
    get_kakou_name,
    load_master,
    lookup,
    read_csv_with_auto_encoding,
    restore_master,
    save_master,
)
from app.kintone_client import KintoneClient
from app.logger import setup_logger
from app.models import AppConfig, PendingRegistration, ProcessResult, RunInput, TksDebugResult
from app.preview_state import DEFAULT_CUSTOMER_KEY, PreviewState
from app.tks_client import create_tks_client
from app.update_client import UpdateClient, UpdateInfo, default_update_dir, launch_external_update
from app.version import VERSION_CODE, VERSION_NAME


SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
SETTINGS_LOGIN_ID = "olap/login_id"
SETTINGS_PASSWORD = "olap/password"
SETTINGS_THEME = "ui/theme"
SETTINGS_DEBUG_VISIBLE = "ui/debug_visible"
SETTINGS_KINTONE_TARGET = "kintone/target"
SETTINGS_R2_OVERRIDES_KAKOU = "olap/kakou_r2_overrides"
SETTINGS_R2_OVERRIDES_SOBA = "olap/soba_r2_overrides"
THEME_SYSTEM = "system"
THEME_LIGHT = "light"
THEME_DARK = "dark"
KINTONE_TARGET_PROD = "production"
KINTONE_TARGET_TEST = "test"
KINTONE_TARGET_LABELS = {
    KINTONE_TARGET_PROD: "本番",
    KINTONE_TARGET_TEST: "テスト",
}
KINTONE_TARGET_DISPLAY_LABELS = {
    KINTONE_TARGET_PROD: "東大阪工場生産進捗",
    KINTONE_TARGET_TEST: "【テスト】東大阪工場生産進捗",
}
KINTONE_PROD_DOMAIN = "manekiya.cybozu.com"
KINTONE_PROD_APP_ID = "211"
KINTONE_PROD_API_TOKEN = "eyfsPQPTTZ7EYYXLBRaRlK9QPCRWioQP3h4rBpPz"
THEME_LABELS = {
    THEME_SYSTEM: "システム",
    THEME_LIGHT: "ライト",
    THEME_DARK: "ダーク",
}
KAKOU_SETTING_DEFAULT = "selected"
_PROCESSING_TYPE = "2"

PREVIEW_ROW_HEADERS = (
    "No", "受注No", "掛率集計コード", "掛率集計名称", "硝/加工",
    "仕上日", "出荷区分", "得意先選択", "判定加工名", "未登録警告",
)
_COL_NO = 0
_COL_ORDER_NO = 1
_COL_KAKURITSU_CODE = 2
_COL_KAKURITSU_NAME = 3
_COL_TYPE = 4
_COL_SHIAGE = 5
_COL_SHUKKA = 6
_COL_CUSTOMER = 7
_COL_KAKOU = 8
_COL_WARNING = 9

CONDITION_HEADERS = ("フィールド論理名", "OLAP値", "OLAP範囲Val_From", "OLAP範囲Val_To", "OLAP空白", "OLAP条件グループ")
CONDITION_EDITABLE_HEADERS = set(CONDITION_HEADERS[1:])


class PopupDateEdit(QDateEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setCalendarPopup(False)
        self.setReadOnly(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setCursor(Qt.CursorShape.ArrowCursor)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._calendar_action = self.lineEdit().addAction(
            QIcon(str(resource_path("assets/calendar.svg"))),
            QLineEdit.ActionPosition.TrailingPosition,
        )
        self._calendar_action.triggered.connect(self._show_calendar)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, watched: object, event: object) -> bool:
        if watched is self.lineEdit() and getattr(event, "type", lambda: None)() == QEvent.Type.MouseButtonPress:
            self._show_calendar()
            return True
        return super().eventFilter(watched, event)  # type: ignore[arg-type]

    def mousePressEvent(self, event: object) -> None:
        self._show_calendar()
        if hasattr(event, "accept"):
            event.accept()

    def _show_calendar(self) -> None:
        menu = QMenu(self)
        calendar = QCalendarWidget(menu)
        calendar.setSelectedDate(self.date())
        action = QWidgetAction(menu)
        action.setDefaultWidget(calendar)
        menu.addAction(action)
        calendar.clicked.connect(lambda selected_date: self._select_popup_date(menu, selected_date))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _select_popup_date(self, menu: QMenu, selected_date: QDate) -> None:
        self.setDate(selected_date)
        menu.close()


class WorkerThread(QThread):
    log_line = Signal(str)
    credentials_validated = Signal(str, str)
    succeeded = Signal(object)
    pending_registration = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, run_input: RunInput) -> None:
        super().__init__()
        self.config = config
        self.run_input = run_input

    def run(self) -> None:
        logger, log_file = setup_logger(self.config.paths.log_dir, self.log_line.emit)
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            work_dir = self.config.paths.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)

            logger.info("起動日時: %s", datetime.now().isoformat(timespec="seconds"))
            logger.info("対象伝票番号: %s", ",".join(self.run_input.denpyo_numbers))
            logger.info("仕上日: %s", self.run_input.shiage_date)
            logger.info("出荷区分: %s", self.run_input.shukka_kbn)

            write_input_history(
                work_dir / f"input_{timestamp}.csv",
                self.run_input.denpyo_numbers,
                self.run_input.shiage_date,
                self.run_input.shukka_kbn,
            )

            tks_client = create_tks_client(self.config, logger)
            tks_client.login(self.run_input)
            self.credentials_validated.emit(self.run_input.olap_login_id, self.run_input.olap_password)
            soba_csv, kakou_csv = tks_client.fetch_csvs(self.run_input, work_dir, self.config.csv_encoding)

            output_csv = work_dir / "outputTksToKintone.csv"
            rows = create_output_csv(
                soba_csv,
                kakou_csv,
                output_csv,
                self.run_input.shiage_date,
                self.run_input.shukka_kbn,
            )
            logger.info("outputTksToKintone.csv 出力件数: %s", len(rows))

            output_rows = read_output_rows(output_csv)
            master = load_master(self.config.paths.kakou_master_csv)
            apply_kakou_names_per_row(output_rows, master)
            logger.info("加工名マスタ適用件数: %s", len(master))
            logger.info("登録前確認画面を表示します。登録ボタン押下までkintoneへ送信しません。")
            self.pending_registration.emit(
                PendingRegistration(
                    output_csv=output_csv,
                    rows=output_rows,
                    output_count=len(rows),
                    log_file=log_file,
                    timestamp=timestamp,
                )
            )
        except Exception as exc:
            logger.exception("処理中にエラーが発生しました")
            debug_file = _latest_debug_file(self.config.paths.work_dir)
            if debug_file is not None:
                logger.error("最新debugファイル: %s", debug_file)
            self.failed.emit(_format_error_message(exc, log_file, debug_file))


class KintoneRegisterWorkerThread(QThread):
    log_line = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, rows: list[dict[str, str]], output_csv: Path, timestamp: str) -> None:
        super().__init__()
        self.config = config
        self.rows = rows
        self.output_csv = output_csv
        self.timestamp = timestamp

    def run(self) -> None:
        logger, log_file = setup_logger(self.config.paths.log_dir, self.log_line.emit)
        try:
            write_output_csv(self.output_csv, self.rows)
            logger.info("登録確認後のCSVを保存しました: %s", self.output_csv)
            kintone_result = KintoneClient(self.config, logger).register_rows(self.rows)

            error_csv = None
            if kintone_result.failed_records:
                error_csv = self.config.paths.error_dir / f"failed_{self.timestamp}.csv"
                write_failed_csv(error_csv, kintone_result.failed_records)
                logger.info("失敗レコードCSV: %s", error_csv)

            logger.info("kintone登録成功件数: %s", kintone_result.success_count)
            logger.info("kintone登録失敗件数: %s", kintone_result.failure_count)
            self.succeeded.emit(
                ProcessResult(
                    output_csv=self.output_csv,
                    output_count=len(self.rows),
                    kintone_success_count=kintone_result.success_count,
                    kintone_failure_count=kintone_result.failure_count,
                    has_error=kintone_result.failure_count > 0,
                    log_file=log_file,
                    error_csv=error_csv,
                )
            )
        except Exception as exc:
            logger.exception("kintone登録中にエラーが発生しました")
            self.failed.emit(_format_error_message(exc, log_file, _latest_debug_file(self.config.paths.work_dir)))


class TksDebugWorkerThread(QThread):
    log_line = Signal(str)
    credentials_validated = Signal(str, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, run_input: RunInput, mode: str) -> None:
        super().__init__()
        self.config = config
        self.run_input = run_input
        self.mode = mode

    def run(self) -> None:
        logger, log_file = setup_logger(self.config.paths.log_dir, self.log_line.emit)
        try:
            client = create_tks_client(self.config, logger)
            if self.mode == "login":
                logger.info("TKS接続テスト開始")
                client.login(self.run_input)
                self.credentials_validated.emit(self.run_input.olap_login_id, self.run_input.olap_password)
                logger.info(".ASPXAUTH Cookie取得有無: %s", "あり" if client.has_auth_cookie() else "なし")
                message = f"TKS接続テスト成功\n.ASPXAUTH Cookie取得有無: {'あり' if client.has_auth_cookie() else 'なし'}\nログファイル: {log_file}"
                self.succeeded.emit(TksDebugResult(message=message, log_file=log_file))
                return

            if self.mode == "olap":
                logger.info("OLAP取得テスト開始")
                logger.info("対象伝票番号件数: %s", len(self.run_input.denpyo_numbers))
                client.login(self.run_input)
                self.credentials_validated.emit(self.run_input.olap_login_id, self.run_input.olap_password)
                soba_csv, kakou_csv = client.fetch_csvs(self.run_input, self.config.paths.work_dir, self.config.csv_encoding)
                message = "\n".join(
                    [
                        "OLAP取得テスト成功",
                        f"加工CSV: {kakou_csv} ({_count_csv_records(kakou_csv, self.config.csv_encoding)}件)",
                        f"素板CSV: {soba_csv} ({_count_csv_records(soba_csv, self.config.csv_encoding)}件)",
                        f"debug保存先: {self.config.paths.work_dir / 'debug'}",
                        f"ログファイル: {log_file}",
                    ]
                )
                self.succeeded.emit(TksDebugResult(message=message, log_file=log_file))
                return

            raise ValueError(f"未対応のテスト種別です: {self.mode}")
        except Exception as exc:
            logger.exception("TKSデバッグ処理でエラーが発生しました")
            debug_file = _latest_debug_file(self.config.paths.work_dir)
            if debug_file is not None:
                logger.error("最新debugファイル: %s", debug_file)
            self.failed.emit(_format_error_message(exc, log_file, debug_file))


class UpdateCheckWorkerThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.succeeded.emit(UpdateClient().check_for_update(VERSION_CODE))
        except Exception as exc:
            self.failed.emit(str(exc))


class AddRowDialog(QDialog):
    """加工名マスタへの行追加ダイアログ。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("行追加")

        self.maker_code = QLineEdit("MK")
        self.kakuritsu_code = QLineEdit()
        self.kakuritsu_name = QLineEdit()
        self.kakuritsu_ryaku = QLineEdit()
        self.kakou_name_edit = QLineEdit()
        self.maker_id_code = QLineEdit()
        self.maker_id_code.setReadOnly(True)

        self.maker_code.textChanged.connect(self._update_maker_id_code)
        self.kakuritsu_code.textChanged.connect(self._update_maker_id_code)
        self._update_maker_id_code()

        form = QFormLayout()
        form.addRow("メーカー識別コード", self.maker_code)
        form.addRow("掛率集計コード", self.kakuritsu_code)
        form.addRow("メーカー識別掛率集計コード (自動)", self.maker_id_code)
        form.addRow("掛率集計名称", self.kakuritsu_name)
        form.addRow("掛率集計略称", self.kakuritsu_ryaku)
        form.addRow("加工名", self.kakou_name_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(buttons)
        self.setLayout(root)

    def _update_maker_id_code(self) -> None:
        self.maker_id_code.setText(self.maker_code.text() + self.kakuritsu_code.text())

    def row_data(self) -> dict[str, str]:
        code = self.kakuritsu_code.text().strip()
        maker = self.maker_code.text().strip()
        return {
            "メーカー識別掛率集計コード": maker + code,
            "メーカー識別コード": maker,
            "掛率集計コード": code,
            "掛率集計名称": self.kakuritsu_name.text().strip(),
            "掛率集計略称": self.kakuritsu_ryaku.text().strip(),
            "加工名": self.kakou_name_edit.text().strip(),
            "得意先1": "",
            "得意先2": "",
            "得意先3": "",
            "得意先4": "",
        }


class CustomerLabelDialog(QDialog):
    """得意先ヘッダー表示名設定ダイアログ。"""

    def __init__(self, customer_labels: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("得意先ヘッダー設定")
        self.resize(380, 220)

        self._edits: dict[str, QLineEdit] = {}
        form = QFormLayout()
        for key in ("得意先1", "得意先2", "得意先3", "得意先4"):
            edit = QLineEdit(customer_labels.get(key, CUSTOMER_LABEL_DEFAULTS[key]))
            edit.setMaxLength(CUSTOMER_LABEL_MAX_LEN)
            self._edits[key] = edit
            form.addRow(f"{key} 表示名:", edit)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = buttons.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        reset_btn = buttons.addButton("初期値に戻す", QDialogButtonBox.ButtonRole.ResetRole)

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(buttons)
        self.setLayout(root)

        save_btn.clicked.connect(self._on_save)
        cancel_btn.clicked.connect(self.reject)
        reset_btn.clicked.connect(self._reset)

    def _reset(self) -> None:
        for key, default in CUSTOMER_LABEL_DEFAULTS.items():
            self._edits[key].setText(default)

    def _on_save(self) -> None:
        for key, edit in self._edits.items():
            error = validate_customer_label(edit.text().strip())
            if error:
                QMessageBox.warning(self, "入力エラー", f"{key} の表示名: {error}")
                return
        self.accept()

    def result_labels(self) -> dict[str, str]:
        """保存後の表示名辞書を返す。空欄は初期値に戻す。"""
        return {
            key: (edit.text().strip() or CUSTOMER_LABEL_DEFAULTS[key])
            for key, edit in self._edits.items()
        }


class KakouMasterDialog(QDialog):
    """加工名マスタの管理ダイアログ。"""

    def __init__(
        self,
        master_path: Path,
        backup_dir: Path,
        customer_labels: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("加工名マスタ管理")
        self.resize(1200, 600)
        self.master_path = master_path
        self.backup_dir = backup_dir
        self._dirty = False

        _cl = customer_labels or {}
        display_headers = [_cl.get(h, h) for h in KAKOU_MASTER_HEADERS]

        self.table = QTableWidget()
        self.table.setColumnCount(len(KAKOU_MASTER_HEADERS))
        self.table.setHorizontalHeaderLabels(display_headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)

        self._load_from_file()

        add_btn = QPushButton("行追加")
        del_btn = QPushButton("行削除")
        import_btn = QPushButton("CSVインポート")
        export_btn = QPushButton("CSVエクスポート")
        backup_btn = QPushButton("バックアップ作成")
        restore_btn = QPushButton("バックアップから復元")

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(backup_btn)
        btn_row.addWidget(restore_btn)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("保存して閉じる", QDialogButtonBox.ButtonRole.AcceptRole)
        close_btn = buttons.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)

        root = QVBoxLayout()
        root.addLayout(btn_row)
        root.addWidget(self.table, 1)
        root.addWidget(buttons)
        self.setLayout(root)

        add_btn.clicked.connect(self._add_row)
        del_btn.clicked.connect(self._delete_row)
        import_btn.clicked.connect(self._import_csv)
        export_btn.clicked.connect(self._export_csv)
        backup_btn.clicked.connect(self._create_backup)
        restore_btn.clicked.connect(self._restore_backup)
        save_btn.clicked.connect(self._save_and_close)
        close_btn.clicked.connect(self._close_with_confirm)

    def _load_from_file(self) -> None:
        rows = load_master(self.master_path)
        self._populate_table(rows)
        self._dirty = False

    def _populate_table(self, rows: list[dict[str, str]]) -> None:
        self.table.itemChanged.disconnect(self._on_item_changed)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col_index, header in enumerate(KAKOU_MASTER_HEADERS):
                self.table.setItem(row_index, col_index, QTableWidgetItem(row.get(header, "")))
        self.table.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        self._dirty = True
        row = item.row()
        col = item.column()
        maker_col = KAKOU_MASTER_HEADERS.index("メーカー識別コード")
        code_col = KAKOU_MASTER_HEADERS.index("掛率集計コード")
        id_col = KAKOU_MASTER_HEADERS.index("メーカー識別掛率集計コード")
        if col in (maker_col, code_col):
            maker_item = self.table.item(row, maker_col)
            code_item = self.table.item(row, code_col)
            maker_val = maker_item.text() if maker_item else ""
            code_val = code_item.text() if code_item else ""
            self.table.itemChanged.disconnect(self._on_item_changed)
            id_item = self.table.item(row, id_col)
            if id_item:
                id_item.setText(maker_val + code_val)
            else:
                self.table.setItem(row, id_col, QTableWidgetItem(maker_val + code_val))
            self.table.itemChanged.connect(self._on_item_changed)

    def _table_to_rows(self) -> list[dict[str, str]]:
        rows = []
        for row_index in range(self.table.rowCount()):
            row: dict[str, str] = {}
            for col_index, header in enumerate(KAKOU_MASTER_HEADERS):
                item = self.table.item(row_index, col_index)
                row[header] = item.text() if item else ""
            rows.append(row)
        return rows

    def _add_row(self) -> None:
        dialog = AddRowDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.row_data()
        row_index = self.table.rowCount()
        self.table.itemChanged.disconnect(self._on_item_changed)
        self.table.insertRow(row_index)
        for col_index, header in enumerate(KAKOU_MASTER_HEADERS):
            self.table.setItem(row_index, col_index, QTableWidgetItem(data.get(header, "")))
        self.table.itemChanged.connect(self._on_item_changed)
        self._dirty = True

    def _delete_row(self) -> None:
        selected_rows = sorted({item.row() for item in self.table.selectedItems()})
        if not selected_rows:
            QMessageBox.warning(self, "削除", "削除する行を選択してください。")
            return

        reply = QMessageBox.question(
            self,
            "削除確認",
            f"{len(selected_rows)}行を削除します。削除前に自動バックアップを作成します。\nよろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        rows = self._table_to_rows()
        save_master(self.master_path, rows)
        backup = backup_master(self.master_path, self.backup_dir)
        if backup:
            QMessageBox.information(self, "バックアップ", f"バックアップを作成しました:\n{backup}")

        for row in reversed(selected_rows):
            self.table.removeRow(row)
        self._dirty = True

    def _create_backup(self) -> None:
        if self._dirty:
            save_master(self.master_path, self._table_to_rows())
            self._dirty = False
        backup = backup_master(self.master_path, self.backup_dir)
        if backup:
            QMessageBox.information(self, "バックアップ", f"バックアップを作成しました:\n{backup}")
        else:
            QMessageBox.warning(self, "バックアップ", "マスタファイルが存在しないためバックアップできません。")

    def _restore_backup(self) -> None:
        if not self.backup_dir.exists():
            QMessageBox.warning(self, "復元", "バックアップフォルダが見つかりません。")
            return
        backup_files = sorted(self.backup_dir.glob("kakou_master_*.csv.bak"), reverse=True)
        if not backup_files:
            QMessageBox.warning(self, "復元", "バックアップファイルが見つかりません。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("バックアップから復元")
        dialog.resize(600, 300)
        list_table = QTableWidget(len(backup_files), 2)
        list_table.setHorizontalHeaderLabels(["ファイル名", "更新日時"])
        list_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        list_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        list_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        list_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, f in enumerate(backup_files):
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            list_table.setItem(i, 0, QTableWidgetItem(f.name))
            list_table.setItem(i, 1, QTableWidgetItem(mtime))

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        root = QVBoxLayout()
        root.addWidget(QLabel("復元するバックアップを選択してください:"))
        root.addWidget(list_table, 1)
        root.addWidget(btns)
        dialog.setLayout(root)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected = list_table.selectedItems()
        if not selected:
            return
        backup_file = backup_files[selected[0].row()]

        reply = QMessageBox.question(
            self,
            "復元確認",
            f"バックアップから復元します:\n{backup_file.name}\n\nよろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        restore_master(backup_file, self.master_path)
        self._load_from_file()
        QMessageBox.information(self, "復元", "復元が完了しました。")

    def _import_csv(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, "CSVインポート", "", "CSVファイル (*.csv)")
        if not path_str:
            return
        import_path = Path(path_str)

        mode_dialog = QDialog(self)
        mode_dialog.setWindowTitle("インポート方式選択")
        rb_merge = QRadioButton("追加・更新のみ（CSVにない既存行は残す）")
        rb_replace = QRadioButton("完全置換（CSVの内容でマスタを置き換える）")
        rb_merge.setChecked(True)
        mode_group = QGroupBox("インポート方式")
        mode_layout = QVBoxLayout()
        mode_layout.addWidget(rb_merge)
        mode_layout.addWidget(rb_replace)
        mode_group.setLayout(mode_layout)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        root = QVBoxLayout()
        root.addWidget(mode_group)
        root.addWidget(btns)
        mode_dialog.setLayout(root)
        btns.accepted.connect(mode_dialog.accept)
        btns.rejected.connect(mode_dialog.reject)
        if mode_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        full_replace = rb_replace.isChecked()

        rows_now = self._table_to_rows()
        save_master(self.master_path, rows_now)
        backup = backup_master(self.master_path, self.backup_dir)
        if backup:
            QMessageBox.information(self, "バックアップ", f"インポート前にバックアップを作成しました:\n{backup}")

        try:
            raw_rows, detected_encoding = read_csv_with_auto_encoding(import_path)
            import_rows = [{h: str(row.get(h, "") or "") for h in KAKOU_MASTER_HEADERS} for row in raw_rows]
        except CsvEncodingError as exc:
            QMessageBox.critical(self, "インポートエラー", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "インポートエラー", f"CSVの読み込みに失敗しました:\n{exc}")
            return

        if full_replace:
            merged = import_rows
        else:
            existing = list(rows_now)
            code_to_index: dict[str, int] = {
                r.get("掛率集計コード", "").strip(): i
                for i, r in enumerate(existing)
                if r.get("掛率集計コード", "").strip()
            }
            for imp_row in import_rows:
                code = imp_row.get("掛率集計コード", "").strip()
                if code and code in code_to_index:
                    existing[code_to_index[code]] = imp_row
                else:
                    existing.append(imp_row)
                    if code:
                        code_to_index[code] = len(existing) - 1
            merged = existing

        self._populate_table(merged)
        self._dirty = True
        QMessageBox.information(
            self, "インポート完了",
            f"{len(import_rows)}行をインポートしました。\n（文字コード: {detected_encoding}）",
        )

    def _export_csv(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"kakou_master_{timestamp}.csv"
        path_str, _ = QFileDialog.getSaveFileName(self, "CSVエクスポート", default_name, "CSVファイル (*.csv)")
        if not path_str:
            return
        try:
            save_master(Path(path_str), self._table_to_rows())
            QMessageBox.information(self, "エクスポート完了", f"エクスポートしました:\n{path_str}")
        except Exception as exc:
            QMessageBox.critical(self, "エクスポートエラー", f"エクスポートに失敗しました:\n{exc}")

    def _save_and_close(self) -> None:
        backup_master(self.master_path, self.backup_dir)
        save_master(self.master_path, self._table_to_rows())
        self._dirty = False
        self.accept()

    def _close_with_confirm(self) -> None:
        if self._dirty:
            reply = QMessageBox.question(
                self,
                "保存確認",
                "変更が保存されていません。保存しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            if reply == QMessageBox.StandardButton.Yes:
                save_master(self.master_path, self._table_to_rows())
        self.reject()


class RegistrationPreviewDialog(QDialog):
    """登録前確認ダイアログ。

    内部状態は PreviewState で管理する。Qt ウィジェットは表示用であり、
    登録データは必ず PreviewState.build_registration_rows() から生成する。
    絞り込みフィルタは表示制御のみで、登録対象は常に全CSVレコードである。
    """

    def __init__(
        self,
        rows: list[dict[str, str]],
        shukka_options: list[str],
        master: list[dict[str, str]],
        customer_labels: dict[str, str],
        parent: QWidget | None = None,
        preview_color_theme: str = "light",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("登録前確認")
        self.resize(1300, 700)
        self._master = master
        self._shukka_options = shukka_options
        self._preview_color_theme = preview_color_theme
        # PreviewState が唯一の内部データモデル
        self._state = PreviewState(rows=[dict(row) for row in rows])

        self._kakou_options: list[tuple[str, str]] = [("selected", "選択なし")]
        for key in CUSTOMER_KEYS:
            self._kakou_options.append((key, customer_labels.get(key, key)))

        # ウィジェットリスト（行インデックスと1対1対応）
        # 受注No先頭行のみウィジェットを持ち、2行目以降は None
        self._shiage_widgets: list[PopupDateEdit | None] = []
        self._shukka_widgets: list[QComboBox | None] = []
        self._customer_widgets: list[QComboBox | None] = []
        self._kakou_labels: list[QLabel] = []

        # フィルタバー
        filter_label = QLabel("受注No絞り込み:")
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("受注No を入力（表示のみ、登録対象は全件）")
        self._filter_edit.setMaximumWidth(260)
        filter_clear = QPushButton("クリア")
        filter_clear.setMaximumWidth(60)
        filter_clear.clicked.connect(lambda: self._filter_edit.clear())
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row = QHBoxLayout()
        filter_row.addWidget(filter_label)
        filter_row.addWidget(self._filter_edit)
        filter_row.addWidget(filter_clear)
        filter_row.addStretch(1)

        # テーブル
        self.table = QTableWidget(len(self._state.rows), len(PREVIEW_ROW_HEADERS))
        self.table.setHorizontalHeaderLabels(PREVIEW_ROW_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(False)
        # ソート無効: 有効にすると画面 row_idx と PreviewState 内部 row_idx がズレる
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(_COL_KAKOU, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(_COL_NO, 40)
        self.table.setColumnWidth(_COL_ORDER_NO, 110)
        self.table.setColumnWidth(_COL_KAKURITSU_CODE, 100)
        self.table.setColumnWidth(_COL_KAKURITSU_NAME, 145)
        self.table.setColumnWidth(_COL_TYPE, 55)
        self.table.setColumnWidth(_COL_SHIAGE, 130)
        self.table.setColumnWidth(_COL_SHUKKA, 85)
        self.table.setColumnWidth(_COL_CUSTOMER, 155)
        self.table.setColumnWidth(_COL_WARNING, 185)
        self._populate_table()

        # 未登録警告バナー
        all_warnings: list[str] = []
        for row in self._state.rows:
            w = _row_unregistered_warning(row, master)
            if w:
                all_warnings.append(w)

        buttons = QDialogButtonBox()
        self.register_button = buttons.addButton("登録", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("登録キャンセル", QDialogButtonBox.ButtonRole.RejectRole)

        bottom = QHBoxLayout()
        self.print_button = QPushButton("印刷")
        self.print_button.clicked.connect(self._print_slips)
        bottom.addWidget(self.print_button)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        root = QVBoxLayout()
        root.addLayout(filter_row)
        root.addWidget(QLabel(
            "仕上日・出荷区分・得意先選択は受注No先頭行にのみ表示されます。"
            "変更は同じ受注Noの全行（非表示行を含む）に反映されます。"
            "加工名は行ごとに判定されます。"
        ))
        if all_warnings:
            unique_warnings = list(dict.fromkeys(all_warnings))
            warn_label = QLabel("未登録の掛率集計コードがあります:\n" + "\n".join(unique_warnings))
            warn_label.setStyleSheet("color: #cc7700;")
            root.addWidget(warn_label)
        root.addWidget(self.table, 1)
        root.addLayout(bottom)
        self.setLayout(root)

        self.register_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    # ── テーブル構築 ──────────────────────────────────────

    def _is_dark_theme(self) -> bool:
        """配色テーマを判定する。

        self._preview_color_theme が "light"/"dark" なら即座に返す。
        "auto" の場合は QPalette.Base の輝度で自動判定する。
        不正値は "light" 扱い（False を返す）。
        """
        theme = self._preview_color_theme
        if theme == "dark":
            print("登録前確認テーマ判定: theme=dark (固定)")
            return True
        if theme == "light":
            print("登録前確認テーマ判定: theme=light (固定)")
            return False
        # auto
        app = QApplication.instance()
        if app is None:
            print("登録前確認テーマ判定: theme=auto, QApplication なし → light")
            return False
        base_color = app.palette().color(QPalette.ColorRole.Base)
        lightness = base_color.lightness()
        is_dark = lightness < 128
        print(f"登録前確認テーマ判定: theme=auto, base_lightness={lightness}, is_dark={is_dark}")
        return is_dark

    @staticmethod
    def _preview_colors(is_dark: bool) -> dict[str, object]:
        """テーブル描画に使う配色辞書を返す。

        キー一覧:
          group_bg_hex  : list[str]  受注Noグループ交互背景色 [A, B]（先頭行・後続行で共通）
          fg_hex        : str        通常文字色
          warning_color : str        未登録警告文字色
          sel_bg        : str        選択セル背景色
          widget_ss     : str        DateEdit / ComboBox 用スタイルシート
        """
        if is_dark:
            sel_bg = "#3A5A78"
            widget_ss = (
                "QComboBox, QDateEdit {"
                " background-color: #2D3540;"
                " color: #F2F2F2;"
                " border: 1px solid #555555;"
                " border-radius: 3px;"
                " padding: 2px 4px;"
                "}"
                "QComboBox QAbstractItemView {"
                " background-color: #2D3540;"
                " color: #F2F2F2;"
                f" selection-background-color: {sel_bg};"
                " selection-color: #FFFFFF;"
                "}"
            )
            return {
                "group_bg_hex": ["#263544", "#243A2A"],
                "fg_hex": "#F2F2F2",
                "warning_color": "#FFB000",
                "sel_bg": sel_bg,
                "widget_ss": widget_ss,
            }
        else:
            sel_bg = "#2D78B8"
            widget_ss = (
                "QComboBox, QDateEdit {"
                " background-color: #FFFFFF;"
                " color: #1A1A1A;"
                " border: 1px solid #AAAAAA;"
                " border-radius: 3px;"
                " padding: 2px 4px;"
                "}"
                "QComboBox QAbstractItemView {"
                " background-color: #FFFFFF;"
                " color: #1A1A1A;"
                f" selection-background-color: {sel_bg};"
                " selection-color: #FFFFFF;"
                "}"
            )
            return {
                "group_bg_hex": ["#E8F0F8", "#E8F5EC"],
                "fg_hex": "#1A1A1A",
                "warning_color": "#B35C00",
                "sel_bg": sel_bg,
                "widget_ss": widget_ss,
            }

    def _populate_table(self) -> None:
        first_indices = self._state.first_indices_by_order()
        group_indices = self._state.order_group_index()

        # テーマ判定と配色取得
        pal = self._preview_colors(self._is_dark_theme())
        group_bg_hex: list[str] = pal["group_bg_hex"]  # type: ignore[assignment]
        fg_hex: str = pal["fg_hex"]                    # type: ignore[assignment]
        warning_color: str = pal["warning_color"]      # type: ignore[assignment]
        widget_ss: str = pal["widget_ss"]              # type: ignore[assignment]
        sel_bg: str = pal["sel_bg"]                    # type: ignore[assignment]

        # 選択セルの色（item セルのみ有効; widget セルは widget が全面を覆う）
        self.table.setStyleSheet(
            f"QTableWidget::item:selected {{ background-color: {sel_bg}; color: #FFFFFF; }}"
        )

        fg_brush = QBrush(QColor(fg_hex))

        for row_idx, row in enumerate(self._state.rows):
            is_first = row_idx in first_indices
            group_idx = group_indices[row_idx]
            # 同一受注No内は先頭行・後続行とも同じ背景色（グループ単位の交互色のみ）
            bg_hex = group_bg_hex[group_idx % 2]
            bg_brush = QBrush(QColor(bg_hex))
            label_ss = f"color: {fg_hex}; background-color: {bg_hex};"

            # Item セル（文字色・背景色を明示）
            self._set_ro(row_idx, _COL_NO, str(row_idx + 1), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_ORDER_NO, row.get("受注No", ""), bold=is_first, bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_KAKURITSU_CODE, row.get("掛率集計コード", ""), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_KAKURITSU_NAME, row.get("掛率集計名称", ""), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_TYPE, row.get("硝/加工", ""), bg=bg_brush, fg=fg_brush)

            if is_first:
                # 仕上日ウィジェット（受注No先頭行のみ）
                date_edit = PopupDateEdit()
                date_edit.setCalendarPopup(False)
                date_edit.setDisplayFormat("yyyy-MM-dd")
                date_edit.setDate(_date_from_text(self._state.shiage_by_row[row_idx]))
                date_edit.setStyleSheet(widget_ss)
                self._shiage_widgets.append(date_edit)
                self.table.setCellWidget(row_idx, _COL_SHIAGE, date_edit)
                date_edit.dateChanged.connect(
                    lambda d, ri=row_idx: self._on_shiage_changed(ri, d)
                )

                # 出荷区分ウィジェット（受注No先頭行のみ）
                shukka = QComboBox()
                shukka.addItems(self._shukka_options)
                current = self._state.shukka_by_row[row_idx]
                if current and current not in self._shukka_options:
                    shukka.addItem(current)
                if current:
                    shukka.setCurrentText(current)
                shukka.setStyleSheet(widget_ss)
                self._shukka_widgets.append(shukka)
                self.table.setCellWidget(row_idx, _COL_SHUKKA, shukka)
                shukka.currentTextChanged.connect(
                    lambda text, ri=row_idx: self._on_shukka_changed(ri, text)
                )

                # 得意先選択ウィジェット（受注No先頭行のみ）
                customer = QComboBox()
                for key, label in self._kakou_options:
                    customer.addItem(label, key)
                customer.setStyleSheet(widget_ss)
                self._customer_widgets.append(customer)
                self.table.setCellWidget(row_idx, _COL_CUSTOMER, customer)
                customer.currentIndexChanged.connect(
                    lambda _i, ri=row_idx: self._on_customer_changed(ri)
                )
            else:
                # 2行目以降: ウィジェットなし（内部データは先頭行と同値を保持）
                self._shiage_widgets.append(None)
                self._shukka_widgets.append(None)
                self._customer_widgets.append(None)
                self._set_ro(row_idx, _COL_SHIAGE, "", bg=bg_brush, fg=fg_brush)
                self._set_ro(row_idx, _COL_SHUKKA, "", bg=bg_brush, fg=fg_brush)
                self._set_ro(row_idx, _COL_CUSTOMER, "", bg=bg_brush, fg=fg_brush)

            # 判定加工名ラベル（全行）背景を行色に合わせ文字色を明示
            kakou_label = QLabel()
            kakou_label.setStyleSheet(label_ss)
            self._kakou_labels.append(kakou_label)
            self.table.setCellWidget(row_idx, _COL_KAKOU, kakou_label)

            # 未登録警告ラベル（全行）警告色を明示し背景も行色に合わせる
            warning_text = _row_unregistered_warning(row, self._master)
            warning_label = QLabel(warning_text)
            warn_color = warning_color if warning_text else fg_hex
            warning_label.setStyleSheet(f"color: {warn_color}; background-color: {bg_hex};")
            self.table.setCellWidget(row_idx, _COL_WARNING, warning_label)

            self._refresh_kakou_label(row_idx)

    def _set_ro(self, row: int, col: int, text: str, bold: bool = False, bg: QBrush | None = None, fg: QBrush | None = None) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        if bg is not None:
            item.setBackground(bg)
        if fg is not None:
            item.setForeground(fg)
        self.table.setItem(row, col, item)

    # ── 仕上日 変更ハンドラ ───────────────────────────────

    def _on_shiage_changed(self, row_idx: int, date: QDate) -> None:
        # PreviewState を更新（同一受注No 全行に反映）
        # 先頭行のみウィジェットを持つため他ウィジェットへの同期は不要
        self._state.set_shiage(row_idx, date.toString("yyyy-MM-dd"))

    # ── 出荷区分 変更ハンドラ ─────────────────────────────

    def _on_shukka_changed(self, row_idx: int, text: str) -> None:
        # PreviewState を更新（同一受注No 全行に反映）
        self._state.set_shukka(row_idx, text)

    # ── 得意先選択 変更ハンドラ ───────────────────────────

    def _on_customer_changed(self, row_idx: int) -> None:
        widget = self._customer_widgets[row_idx]
        new_key = (widget.currentData() if widget is not None else None) or DEFAULT_CUSTOMER_KEY
        # 同一受注No の全行（非表示行含む）に反映
        self._state.set_customer_key_for_order(row_idx, new_key)
        # 同一受注No の全行の判定加工名を更新
        for i in self._state.indices_for_order(row_idx):
            self._refresh_kakou_label(i)

    def _refresh_kakou_label(self, row_idx: int) -> None:
        name = self._state.compute_kakou_name(row_idx, self._master)
        self._kakou_labels[row_idx].setText(name)

    # ── 受注No 絞り込み（表示のみ、登録対象は変わらない）────

    def _apply_filter(self, text: str) -> None:
        text = text.strip()
        for row_idx, row in enumerate(self._state.rows):
            hidden = bool(text) and text not in row.get("受注No", "")
            self.table.setRowHidden(row_idx, hidden)

    # ── 登録用データ生成（絞り込みに無関係な全件返却）────────

    def registration_rows(self) -> list[dict[str, str]]:
        """PreviewState の全行から登録データを生成して返す。

        絞り込みフィルタで非表示になっている行も必ず含む。
        各行に 仕上日・出荷区分・加工名 が設定される。
        先頭行のウィジェット値を PreviewState に反映後、build_registration_rows を呼ぶ。
        非先頭行は先頭行の値が set_shiage/set_shukka/set_customer_key_for_order で既に反映済み。
        """
        for i in range(len(self._state.rows)):
            if self._shiage_widgets[i] is not None:
                self._state.set_shiage(i, self._shiage_widgets[i].date().toString("yyyy-MM-dd"))
            if self._shukka_widgets[i] is not None:
                self._state.set_shukka(i, self._shukka_widgets[i].currentText())
            if self._customer_widgets[i] is not None:
                self._state.set_customer_key_for_order(
                    i, self._customer_widgets[i].currentData() or DEFAULT_CUSTOMER_KEY
                )
        return self._state.build_registration_rows(self._master)

    # ── 印刷 ─────────────────────────────────

    def _print_slips(self) -> None:
        # 先頭行ウィジェットの現在値を _state に反映
        for i in range(len(self._state.rows)):
            if self._customer_widgets[i] is not None:
                self._state.set_customer_key_for_order(
                    i, self._customer_widgets[i].currentData() or DEFAULT_CUSTOMER_KEY
                )
        print_rows = self._state.build_registration_rows(self._master)

        # 受注No 単位の基本値を収集（先頭行のウィジェットまたは _state から取得）
        order_values: dict[str, dict[str, str]] = {}
        for i, row in enumerate(self._state.rows):
            order_no = row.get("受注No", "")
            if order_no not in order_values:
                shiage_w = self._shiage_widgets[i]
                shukka_w = self._shukka_widgets[i]
                order_values[order_no] = {
                    "仕上日": (shiage_w.date().toString("yyyy-MM-dd")
                               if shiage_w is not None else self._state.shiage_by_row[i]),
                    "出荷区分": (shukka_w.currentText()
                                if shukka_w is not None else self._state.shukka_by_row[i]),
                }
        try:
            from app import print_service
            print_service.print_order_slips(self, print_rows, order_values)
        except Exception as exc:
            QMessageBox.critical(self, "印刷エラー", f"印刷中にエラーが発生しました:\n{exc}")


class AdvancedSettingsDialog(QDialog):
    def __init__(self, settings: QSettings, config: AppConfig | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("高度な設定")
        self.resize(980, 620)
        self.settings = settings
        self.tables: dict[str, QTableWidget] = {}
        kakou_template = (
            config.tks_kakou_request_template
            if config is not None and config.tks_kakou_request_template is not None
            else resource_path("docs/olap/kakou_request_template.json")
        )
        soba_template = (
            config.tks_soba_request_template
            if config is not None and config.tks_soba_request_template is not None
            else resource_path("docs/olap/soba_request_template.json")
        )
        self.defaults = {
            "kakou": _load_r2_conditions(kakou_template),
            "soba": _load_r2_conditions(soba_template),
        }

        tabs = QTabWidget()
        tabs.addTab(self._condition_tab("kakou", "加工抽出ロジック"), "加工抽出ロジック")
        tabs.addTab(self._condition_tab("soba", "素板抽出ロジック"), "素板抽出ロジック")

        self.reset_button = QPushButton("初期値に戻す")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.reset_button)
        bottom.addStretch(1)
        bottom.addWidget(buttons)

        log_group = QGroupBox("ログ管理")
        log_form = QFormLayout()
        retention_days = config.cleanup_retention_days if config is not None else 7
        log_form.addRow("古いファイル保存日数", QLabel(f"{retention_days} 日"))
        log_group.setLayout(log_form)

        root = QVBoxLayout()
        root.addWidget(log_group)
        root.addWidget(QLabel("OLAPリクエストテンプレートのR2List抽出条件を変更できます。受注Noは実行時に画面入力値へ差し替えます。"))
        root.addWidget(tabs, 1)
        root.addLayout(bottom)
        self.setLayout(root)
        self.reset_button.clicked.connect(self.reset_defaults)

    def _condition_tab(self, kind: str, title: str) -> QWidget:
        table = QTableWidget()
        table.setColumnCount(len(CONDITION_HEADERS))
        table.setHorizontalHeaderLabels(CONDITION_HEADERS)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        self.tables[kind] = table
        self._populate_condition_table(kind)

        root = QVBoxLayout()
        root.addWidget(QLabel(title))
        root.addWidget(table, 1)
        widget = QWidget()
        widget.setLayout(root)
        return widget

    def _populate_condition_table(self, kind: str) -> None:
        table = self.tables[kind]
        defaults = self.defaults[kind]
        overrides = _load_r2_overrides(self.settings, kind)
        table.setRowCount(len(defaults))
        for row_index, condition in enumerate(defaults):
            effective = dict(condition)
            effective.update(overrides.get(str(row_index), {}))
            for column_index, header in enumerate(CONDITION_HEADERS):
                item = QTableWidgetItem(str(effective.get(header, "")))
                if header not in CONDITION_EDITABLE_HEADERS or condition.get("フィールド論理名") == "受注No":
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row_index, column_index, item)

    def reset_defaults(self) -> None:
        self.settings.remove(SETTINGS_R2_OVERRIDES_KAKOU)
        self.settings.remove(SETTINGS_R2_OVERRIDES_SOBA)
        self.settings.sync()
        self._populate_condition_table("kakou")
        self._populate_condition_table("soba")

    def accept(self) -> None:
        _save_r2_overrides(self.settings, "kakou", self.tables["kakou"], self.defaults["kakou"])
        _save_r2_overrides(self.settings, "soba", self.tables["soba"], self.defaults["soba"])
        self.settings.sync()
        super().accept()


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: AppConfig | None,
        settings: QSettings,
        parent: QWidget | None = None,
        update_callback: object | None = None,
        cleanup_callback: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(520, 360)
        self.config = config
        self.settings = settings
        self.update_callback = update_callback
        self.cleanup_callback = cleanup_callback

        self.kintone_target = QComboBox()
        for value, label in KINTONE_TARGET_LABELS.items():
            self.kintone_target.addItem(label, value)
        current_target = str(settings.value(SETTINGS_KINTONE_TARGET, KINTONE_TARGET_PROD) or KINTONE_TARGET_PROD)
        target_index = self.kintone_target.findData(current_target)
        self.kintone_target.setCurrentIndex(target_index if target_index >= 0 else 0)

        form = QFormLayout()
        form.addRow("Kintone接続先", self.kintone_target)

        self.advanced_button = QPushButton("高度な設定を開く")

        advanced = QGroupBox("高度な設定")
        advanced_form = QFormLayout()
        advanced_form.addRow("", self.advanced_button)
        if config is None:
            advanced_form.addRow("ProgramData", QLabel(str(default_base_dir())))
        else:
            advanced_form.addRow("ProgramData", QLabel(str(config.paths.base_dir)))
            advanced_form.addRow("設定ファイル", QLabel(str(config.paths.config_env)))
            advanced_form.addRow("ログフォルダ", QLabel(str(config.paths.log_dir)))
            advanced_form.addRow("workフォルダ", QLabel(str(config.paths.work_dir)))
        advanced.setLayout(advanced_form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(advanced)
        root.addStretch(1)
        root.addWidget(buttons)
        self.setLayout(root)
        self.advanced_button.clicked.connect(self.open_advanced_settings)

    def open_advanced_settings(self) -> None:
        dialog = AdvancedSettingsDialog(self.settings, self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and callable(self.cleanup_callback):
            self.cleanup_callback()

    def accept(self) -> None:
        self.settings.setValue(SETTINGS_KINTONE_TARGET, self.kintone_target.currentData())
        self.settings.sync()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(
        self,
        initial_olap_id: str | None = None,
        initial_olap_password: str | None = None,
    ) -> None:
        super().__init__()
        self._initial_olap_id = initial_olap_id
        self._initial_olap_password = initial_olap_password
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.setWindowTitle(f"TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        self.resize(900, 720)
        self.config: AppConfig | None = None
        self.fallback_base_dir = default_base_dir()
        self.worker: WorkerThread | None = None
        self.debug_worker: TksDebugWorkerThread | None = None
        self.register_worker: KintoneRegisterWorkerThread | None = None
        self.update_check_worker: UpdateCheckWorkerThread | None = None
        self.update_check_manual = False
        self._closing = False
        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.setSingleShot(True)
        self._auto_update_timer.timeout.connect(self.start_auto_update_check)

        self.company_code = QLineEdit()
        self.company_code.setReadOnly(True)
        self.tks_client_mode_label = QLabel("TKS_CLIENT_MODE")
        self.tks_client_mode = QLineEdit()
        self.tks_client_mode.setReadOnly(True)
        self.kintone_target_label = QLabel("Kintone接続先")
        self.kintone_target_display = QLineEdit()
        self.kintone_target_display.setReadOnly(True)
        self.programdata_path_label = QLabel("ProgramDataフォルダ")
        self.programdata_path = QLineEdit()
        self.programdata_path.setReadOnly(True)
        self.login_id = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password_visible = False
        self.password_visibility_action = self.password.addAction(
            QIcon(str(resource_path("assets/eye.svg"))),
            QLineEdit.ActionPosition.TrailingPosition,
        )
        self.password_visibility_action.setToolTip("パスワードを表示")
        self.denpyo_numbers = QPlainTextEdit()
        self.denpyo_numbers.setPlaceholderText("1386680\n1386681")
        self.shiage_date = PopupDateEdit()
        self.shiage_date.setDisplayFormat("yyyy-MM-dd")
        self.shiage_date.setDate(QDate.currentDate())
        self.shiage_date.setMinimumHeight(36)
        self.shiage_date.setStyleSheet("QDateEdit { padding: 4px 8px; }")
        self.shukka_kbn = QComboBox()
        self.run_button = QPushButton("実行")
        self.settings_button = QPushButton("⚙")
        self.settings_button.setToolTip("設定")
        self.settings_button.setAccessibleName("設定")
        self.settings_button.setFixedSize(34, 34)
        self.tks_login_test_button = QPushButton("TKS接続テスト")
        self.olap_test_button = QPushButton("OLAP取得テスト")
        self.cleanup_button = QPushButton("古いファイル削除")
        self.open_config_button = QPushButton("設定フォルダを開く")
        self.open_log_button = QPushButton("ログフォルダを開く")
        self.open_work_button = QPushButton("workフォルダを開く")
        self.kakou_master_button = QPushButton("加工名マスタ")
        self.customer_labels_button = QPushButton("得意先ヘッダー設定")
        self.log_title = QLabel("ログ")
        self.result_title = QLabel("結果")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.result_label = QLabel("")

        self._build_layout()
        apply_theme(str(self.settings.value(SETTINGS_THEME, THEME_SYSTEM) or THEME_SYSTEM))
        self.run_button.clicked.connect(self.start_run)
        self.settings_button.clicked.connect(self.open_settings)
        self.tks_login_test_button.clicked.connect(self.start_tks_login_test)
        self.olap_test_button.clicked.connect(self.start_olap_test)
        self.cleanup_button.clicked.connect(self.run_manual_cleanup)
        self.password_visibility_action.triggered.connect(self.toggle_password_visibility)
        self.open_config_button.clicked.connect(lambda: self.open_folder("config"))
        self.open_log_button.clicked.connect(lambda: self.open_folder("log"))
        self.open_work_button.clicked.connect(lambda: self.open_folder("work"))
        self.kakou_master_button.clicked.connect(self.open_kakou_master)
        self.customer_labels_button.clicked.connect(self.open_customer_label_settings)
        self._load_config()
        self._apply_debug_visibility()
        self._auto_update_timer.start(1200)

    def _build_layout(self) -> None:
        form = QFormLayout()
        form.addRow("契約会社コード", self.company_code)
        form.addRow(self.tks_client_mode_label, self.tks_client_mode)
        form.addRow(self.kintone_target_label, self.kintone_target_display)
        form.addRow(self.programdata_path_label, self.programdata_path)
        form.addRow("OLAPログインID", self.login_id)
        form.addRow("OLAPパスワード", self.password)
        form.addRow("伝票番号", self.denpyo_numbers)
        form.addRow("仕上日", self.shiage_date)
        form.addRow("出荷区分", self.shukka_kbn)

        input_group = QGroupBox("入力")
        input_group.setLayout(form)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.tks_login_test_button)
        button_row.addWidget(self.olap_test_button)
        button_row.addWidget(self.run_button)

        folder_button_row = QHBoxLayout()
        folder_button_row.addWidget(self.kakou_master_button)
        folder_button_row.addWidget(self.customer_labels_button)
        folder_button_row.addWidget(self.cleanup_button)
        folder_button_row.addStretch(1)
        folder_button_row.addWidget(self.open_config_button)
        folder_button_row.addWidget(self.open_log_button)
        folder_button_row.addWidget(self.open_work_button)

        settings_top_row = QHBoxLayout()
        settings_top_row.addStretch(1)
        settings_top_row.addWidget(self.settings_button)

        root = QVBoxLayout()
        root.addLayout(settings_top_row)
        root.addWidget(input_group)
        root.addLayout(folder_button_row)
        root.addLayout(button_row)
        root.addWidget(self.log_title)
        root.addWidget(self.log_view, 1)
        root.addWidget(self.result_title)
        root.addWidget(self.result_label)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    def closeEvent(self, event) -> None:
        """初期化途中でも安全に閉じられるように、遅延処理とワーカーを停止する。"""
        self._closing = True
        if hasattr(self, "_auto_update_timer"):
            self._auto_update_timer.stop()
        for worker in (
            self.worker,
            self.debug_worker,
            self.register_worker,
            self.update_check_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.wait(1000)
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark

        apply_windows_title_bar_theme(self, current_title_bar_is_dark())

    def _load_config(self) -> None:
        try:
            self.config = load_app_config()
        except ConfigError as exc:
            self.run_button.setEnabled(False)
            self.tks_login_test_button.setEnabled(False)
            self.olap_test_button.setEnabled(False)
            self.programdata_path.setText(str(self.fallback_base_dir))
            self.append_log(str(exc))
            QMessageBox.critical(self, "設定不足", str(exc))
            return

        self.company_code.setText(self.config.company_code)
        self.tks_client_mode.setText(self.config.tks_client_mode)
        self._update_kintone_target_display()
        self.programdata_path.setText(str(self.config.paths.base_dir))
        self.shukka_kbn.addItems(self.config.shukka_kbn_options)
        self.append_log(f"設定ファイル: {self.config.paths.config_env}")
        op_fields = ",".join(self.config.tks_voucher_olap_enabled_op_fields)
        self.append_log(f"TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS={op_fields}")
        self._load_saved_credentials()
        self._cleanup_old_files()

    def open_folder(self, target: str) -> None:
        if self.config is None:
            paths = {
                "config": self.fallback_base_dir,
                "log": self.fallback_base_dir / "logs",
                "work": self.fallback_base_dir / "work",
            }
        else:
            paths = {
                "config": self.config.paths.base_dir,
                "log": self.config.paths.log_dir,
                "work": self.config.paths.work_dir,
            }
        path = paths[target]
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "フォルダを開けません", str(path))

    def open_kakou_master(self) -> None:
        if self.config is None:
            QMessageBox.warning(self, "設定不足", "設定が読み込まれていません。")
            return
        dialog = KakouMasterDialog(
            self.config.paths.kakou_master_csv,
            self.config.paths.kakou_master_backup_dir,
            customer_labels=self.config.customer_labels,
            parent=self,
        )
        dialog.exec()

    def open_customer_label_settings(self) -> None:
        if self.config is None:
            QMessageBox.warning(self, "設定不足", "設定が読み込まれていません。")
            return
        dialog = CustomerLabelDialog(self.config.customer_labels, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_labels = dialog.result_labels()
        try:
            update_customer_labels_in_config(self.config.paths.config_env, new_labels)
            self.config = load_app_config()
            QMessageBox.information(self, "保存完了", "得意先ヘッダー設定を保存しました。")
        except Exception as exc:
            QMessageBox.critical(self, "保存エラー", f"設定の保存に失敗しました:\n{exc}")

    def start_run(self) -> None:
        if self.config is None:
            return
        try:
            run_input = self._collect_input(require_denpyo=True)
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return

        self.result_label.setText("")
        self.log_view.clear()
        self._cleanup_old_files()
        self._set_buttons_enabled(False)
        self.worker = WorkerThread(self._effective_config(), run_input)
        self.worker.log_line.connect(self.append_log)
        self.worker.credentials_validated.connect(self._save_credentials)
        self.worker.succeeded.connect(self.on_succeeded)
        self.worker.pending_registration.connect(self.on_pending_registration)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(lambda: self._set_buttons_enabled(True))
        self.worker.start()

    def start_tks_login_test(self) -> None:
        if self.config is None:
            return
        try:
            run_input = self._collect_input(require_denpyo=False)
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._start_debug_worker(run_input, "login")

    def start_olap_test(self) -> None:
        if self.config is None:
            return
        try:
            run_input = self._collect_input(require_denpyo=True)
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._start_debug_worker(run_input, "olap")

    def _start_debug_worker(self, run_input: RunInput, mode: str) -> None:
        self.result_label.setText("")
        self.log_view.clear()
        self._set_buttons_enabled(False)
        self.debug_worker = TksDebugWorkerThread(self._effective_config(), run_input, mode)
        self.debug_worker.log_line.connect(self.append_log)
        self.debug_worker.credentials_validated.connect(self._save_credentials)
        self.debug_worker.succeeded.connect(self.on_debug_succeeded)
        self.debug_worker.failed.connect(self.on_failed)
        self.debug_worker.finished.connect(lambda: self._set_buttons_enabled(True))
        self.debug_worker.start()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self.settings, self, self.start_manual_update_check, self._cleanup_old_files)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_kintone_target_display()
            self._apply_debug_visibility()
            self._cleanup_old_files()

    def start_auto_update_check(self) -> None:
        if self._closing:
            return
        self._start_update_check(manual=False)

    def start_manual_update_check(self) -> None:
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            if manual:
                QMessageBox.information(self._message_parent(), "更新確認", "更新確認を実行中です。")
            return
        self.update_check_manual = manual
        if manual:
            self.append_log("更新確認開始")
        self.update_check_worker = UpdateCheckWorkerThread()
        self.update_check_worker.succeeded.connect(self.on_update_check_succeeded)
        self.update_check_worker.failed.connect(self.on_update_check_failed)
        self.update_check_worker.start()

    def on_update_check_succeeded(self, info: UpdateInfo | None) -> None:
        if info is None:
            if self.update_check_manual:
                self.append_log("更新確認結果: 最新です。")
                QMessageBox.information(self._message_parent(), "更新確認", "現在のバージョンは最新です。")
            return

        release_notes = f"\n\nリリースノート:\n{info.release_notes}" if info.release_notes else ""
        self.append_log(f"新しいバージョンを検出: {info.version_name} (コード {info.version_code})")
        QMessageBox.information(
            self._message_parent(),
            "更新確認",
            "新しいバージョンが見つかりました。\n\n"
            f"現在: {VERSION_NAME} (コード {VERSION_CODE})\n"
            f"新しいバージョン: {info.version_name} (コード {info.version_code})\n"
            f"ファイル名: {info.file_name}"
            f"{release_notes}\n\n"
            "更新ファイルをダウンロードして適用します。",
        )
        self.start_update_download(info)

    def on_update_check_failed(self, message: str) -> None:
        if self.update_check_manual:
            self.append_log(f"更新確認失敗: {message}")
            QMessageBox.warning(self._message_parent(), "更新確認失敗", message)

    def start_update_download(self, info: UpdateInfo) -> None:
        try:
            launch_external_update(info, default_update_dir(), Path(sys.executable).resolve())
        except Exception as exc:
            QMessageBox.warning(self._message_parent(), "更新失敗", str(exc))
            return
        QMessageBox.information(
            self._message_parent(),
            "更新開始",
            "更新を開始します。\nアプリを終了し、自動でダウンロードとインストールを行います。",
        )
        QApplication.quit()

    def _message_parent(self) -> QWidget:
        active_window = QApplication.activeWindow()
        return active_window if isinstance(active_window, QWidget) else self

    def on_pending_registration(self, pending: PendingRegistration) -> None:
        if self.config is None:
            return
        master = load_master(self.config.paths.kakou_master_csv)
        dialog = RegistrationPreviewDialog(
            pending.rows,
            self.config.shukka_kbn_options,
            master,
            self.config.customer_labels,
            self,
            preview_color_theme=self.config.preview_color_theme,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.result_label.setText(
                "\n".join(
                    [
                        "登録キャンセル",
                        f"outputTksToKintone.csv 出力件数: {pending.output_count}",
                        f"ログファイルパス: {pending.log_file}",
                        f"出力CSV: {pending.output_csv}",
                    ]
                )
            )
            return
        self.log_view.clear()
        self._set_buttons_enabled(False)
        self.register_worker = KintoneRegisterWorkerThread(
            self._effective_config(),
            dialog.registration_rows(),
            pending.output_csv,
            pending.timestamp,
        )
        self.register_worker.log_line.connect(self.append_log)
        self.register_worker.succeeded.connect(self.on_succeeded)
        self.register_worker.failed.connect(self.on_failed)
        self.register_worker.finished.connect(lambda: self._set_buttons_enabled(True))
        self.register_worker.start()

    def _collect_input(self, require_denpyo: bool) -> RunInput:
        login_id = self.login_id.text().strip()
        password = self.password.text()
        denpyo_numbers = [value.strip() for value in re.split(r"[,\n\r]+", self.denpyo_numbers.toPlainText()) if value.strip()]
        if not login_id:
            raise ValueError("OLAPログインIDを入力してください。")
        if not password:
            raise ValueError("OLAPパスワードを入力してください。")
        if require_denpyo and not denpyo_numbers:
            raise ValueError("伝票番号を1件以上入力してください。")
        return RunInput(
            company_code=self.company_code.text(),
            olap_login_id=login_id,
            olap_password=password,
            denpyo_numbers=denpyo_numbers,
            shiage_date=self.shiage_date.date().toString("yyyy-MM-dd"),
            shukka_kbn=self.shukka_kbn.currentText(),
        )

    def toggle_password_visibility(self) -> None:
        self.password_visible = not self.password_visible
        if self.password_visible:
            self.password.setEchoMode(QLineEdit.EchoMode.Normal)
            self.password_visibility_action.setIcon(QIcon(str(resource_path("assets/eye-off.svg"))))
            self.password_visibility_action.setToolTip("パスワードを非表示")
        else:
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_visibility_action.setIcon(QIcon(str(resource_path("assets/eye.svg"))))
            self.password_visibility_action.setToolTip("パスワードを表示")

    def _load_saved_credentials(self) -> None:
        if self._initial_olap_id is not None:
            self.login_id.setText(self._initial_olap_id)
            if self._initial_olap_password is not None:
                self.password.setText(self._initial_olap_password)
            return
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        login_id = str(settings.value(SETTINGS_LOGIN_ID, "") or "")
        password = str(settings.value(SETTINGS_PASSWORD, "") or "")
        if login_id:
            self.login_id.setText(login_id)
        if password:
            self.password.setText(password)

    def _save_credentials(self, login_id: str, password: str) -> None:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.setValue(SETTINGS_LOGIN_ID, login_id)
        settings.setValue(SETTINGS_PASSWORD, password)
        settings.sync()

    def _effective_config(self) -> AppConfig:
        if self.config is None:
            raise RuntimeError("設定が読み込まれていません。")
        config = replace(
            self.config,
            tks_kakou_r2_overrides=_load_r2_overrides(self.settings, "kakou"),
            tks_soba_r2_overrides=_load_r2_overrides(self.settings, "soba"),
        )
        if self._kintone_target() != KINTONE_TARGET_PROD:
            return config
        return replace(
            config,
            kintone_domain=KINTONE_PROD_DOMAIN,
            kintone_app_id=KINTONE_PROD_APP_ID,
            kintone_api_token=KINTONE_PROD_API_TOKEN,
        )

    def _kintone_target(self) -> str:
        value = str(self.settings.value(SETTINGS_KINTONE_TARGET, KINTONE_TARGET_PROD) or KINTONE_TARGET_PROD)
        return value if value in KINTONE_TARGET_LABELS else KINTONE_TARGET_PROD

    def _update_kintone_target_display(self) -> None:
        self.kintone_target_display.setText(KINTONE_TARGET_DISPLAY_LABELS.get(self._kintone_target(), "【テスト】東大阪工場生産進捗"))

    def run_manual_cleanup(self) -> None:
        result = self._cleanup_old_files()
        if result is None:
            return
        QMessageBox.information(
            self,
            "古いファイル削除",
            f"対象={result.target_count}, 削除={result.deleted_count}, 失敗={result.failed_count}",
        )

    def _cleanup_old_files(self):
        if self.config is None:
            return None
        logger, _log_file = setup_logger(self.config.paths.log_dir)
        result = cleanup_old_files(self.config.paths, self.config.cleanup_retention_days, logger)
        self.append_log(
            f"古いファイル削除: 対象={result.target_count}, 削除={result.deleted_count}, 失敗={result.failed_count}"
        )
        return result

    def append_log(self, text: str) -> None:
        self.log_view.append(text)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.run_button.setEnabled(enabled)
        self.tks_login_test_button.setEnabled(enabled)
        self.olap_test_button.setEnabled(enabled)
        self.settings_button.setEnabled(enabled)
        self.cleanup_button.setEnabled(enabled)

    def on_succeeded(self, result: ProcessResult) -> None:
        self.result_label.setText(
            "\n".join(
                [
                    f"outputTksToKintone.csv 出力件数: {result.output_count}",
                    f"kintone登録成功件数: {result.kintone_success_count}",
                    f"kintone登録失敗件数: {result.kintone_failure_count}",
                    f"エラー有無: {'あり' if result.has_error else 'なし'}",
                    f"ログファイルパス: {result.log_file}",
                    f"出力CSV: {result.output_csv}",
                ]
            )
        )

    def on_failed(self, message: str) -> None:
        self.result_label.setText(f"エラー有無: あり\n{message}")
        QMessageBox.critical(self, "実行エラー", message)

    def on_debug_succeeded(self, result: TksDebugResult) -> None:
        self.result_label.setText(result.message)

    def _apply_debug_visibility(self) -> None:
        visible = _settings_bool(self.settings, SETTINGS_DEBUG_VISIBLE, False)
        for widget in (
            self.tks_client_mode,
            self.tks_client_mode_label,
            self.kintone_target_display,
            self.kintone_target_label,
            self.programdata_path,
            self.programdata_path_label,
            self.tks_login_test_button,
            self.olap_test_button,
            self.log_title,
            self.log_view,
        ):
            widget.setVisible(visible)
        for widget in (
            self.open_config_button,
            self.open_log_button,
            self.open_work_button,
        ):
            widget.setVisible(False)


def run_gui() -> int:
    app = QApplication([])
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationName(SETTINGS_APP)
    app.setApplicationVersion(VERSION_NAME)
    app.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))

    instance_key = f"{SETTINGS_ORG}.{SETTINGS_APP}.single-instance"
    socket = QLocalSocket()
    socket.connectToServer(instance_key)
    if socket.waitForConnected(250):
        socket.write(b"activate")
        socket.flush()
        socket.waitForBytesWritten(250)
        socket.disconnectFromServer()
        return 0

    server = QLocalServer()
    if not server.listen(instance_key):
        QLocalServer.removeServer(instance_key)
        if not server.listen(instance_key):
            return 1

    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    apply_theme(str(settings.value(SETTINGS_THEME, THEME_SYSTEM) or THEME_SYSTEM))
    from app.launcher_window import LauncherWindow
    window = LauncherWindow()

    def activate_existing_window() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.close()
        window.activate_existing_instance()

    server.newConnection.connect(activate_existing_window)
    app.aboutToQuit.connect(server.close)
    window.show()
    return app.exec()


def _count_csv_records(path: object, encoding: str) -> int:
    with open(path, "r", encoding=encoding, newline="") as fp:
        return max(sum(1 for _ in csv.reader(fp)) - 1, 0)


def _latest_debug_file(work_dir: Path) -> Path | None:
    debug_dir = work_dir / "debug"
    if not debug_dir.exists():
        return None
    files = [path for path in debug_dir.iterdir() if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def _format_error_message(exc: Exception, log_file: Path, debug_file: Path | None) -> str:
    lines = [str(exc), "", f"ログファイル: {log_file}"]
    if debug_file is not None:
        lines.append(f"最新debugファイル: {debug_file}")
    return "\n".join(lines)


def _date_from_text(value: str) -> QDate:
    date = QDate.fromString(value, "yyyy-MM-dd")
    return date if date.isValid() else QDate.currentDate()


def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _compute_row_kakou(row: dict[str, str], master: list[dict[str, str]], column_key: str) -> str:
    """1行の加工名を計算する。硝/加工 ≠ '2' の行は空文字を返す。"""
    if row.get("硝/加工") != _PROCESSING_TYPE:
        return ""
    code = row.get("掛率集計コード", "").strip()
    name = row.get("掛率集計名称", "").strip()
    master_row = lookup(master, code, name)
    return get_kakou_name(master_row, column_key)


def _row_unregistered_warning(row: dict[str, str], master: list[dict[str, str]]) -> str:
    """1行の未登録警告テキストを返す。硝/加工 ≠ '2' または登録済みの場合は空文字。"""
    if row.get("硝/加工") != _PROCESSING_TYPE:
        return ""
    code = row.get("掛率集計コード", "").strip()
    name = row.get("掛率集計名称", "").strip()
    if not code and not name:
        return ""
    master_row = lookup(master, code, name)
    if master_row is not None:
        return ""
    key = f"MK{code}" if code else name
    return f"{key} / {name}" if code and name else key


def _load_r2_conditions(template_path: Path) -> list[dict[str, str]]:
    data = json.loads(template_path.read_text(encoding="utf-8-sig"))
    r2_list = data.get("R2List") if isinstance(data, dict) else None
    if not isinstance(r2_list, list):
        return []
    rows: list[dict[str, str]] = []
    for condition in r2_list:
        if not isinstance(condition, dict):
            continue
        rows.append({header: str(condition.get(header, "") or "") for header in CONDITION_HEADERS})
    return rows


def _load_r2_overrides(settings: QSettings, kind: str) -> dict[str, dict[str, str]]:
    key = SETTINGS_R2_OVERRIDES_KAKOU if kind == "kakou" else SETTINGS_R2_OVERRIDES_SOBA
    raw = str(settings.value(key, "") or "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for index, values in data.items():
        if isinstance(values, dict):
            overrides[str(index)] = {str(k): str(v) for k, v in values.items() if str(k) in CONDITION_EDITABLE_HEADERS}
    return overrides


def _save_r2_overrides(settings: QSettings, kind: str, table: QTableWidget, defaults: list[dict[str, str]]) -> None:
    overrides: dict[str, dict[str, str]] = {}
    for row_index, default in enumerate(defaults):
        if default.get("フィールド論理名") == "受注No":
            continue
        row_override: dict[str, str] = {}
        for column_index, header in enumerate(CONDITION_HEADERS):
            if header not in CONDITION_EDITABLE_HEADERS:
                continue
            item = table.item(row_index, column_index)
            value = item.text() if item is not None else ""
            if value != default.get(header, ""):
                row_override[header] = value
        if row_override:
            overrides[str(row_index)] = row_override
    key = SETTINGS_R2_OVERRIDES_KAKOU if kind == "kakou" else SETTINGS_R2_OVERRIDES_SOBA
    if overrides:
        settings.setValue(key, json.dumps(overrides, ensure_ascii=False, separators=(",", ":")))
    else:
        settings.remove(key)


def _settings_bool(settings: QSettings, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _settings_int(settings: QSettings, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def apply_theme(theme: str) -> None:
    app = QApplication.instance()
    if app is None:
        return
    from app.theme_utils import apply_app_font_size, apply_title_bar_theme_to_top_level_widgets, current_app_is_dark

    apply_app_font_size()
    if theme == THEME_DARK:
        app.setStyleSheet(_with_checkmark_assets(DARK_STYLESHEET))
        apply_title_bar_theme_to_top_level_widgets(True)
    elif theme == THEME_LIGHT:
        app.setStyleSheet(_with_checkmark_assets(LIGHT_STYLESHEET))
        apply_title_bar_theme_to_top_level_widgets(False)
    else:
        app.setStyleSheet("")
        apply_title_bar_theme_to_top_level_widgets(current_app_is_dark())


def _with_checkmark_assets(stylesheet: str) -> str:
    """スタイルシート中のチェックマーク画像プレースホルダを実ファイルパスへ置換する。

    Qt の stylesheet `url()` はスラッシュ区切り（POSIX形式）を要求するため、
    Windows でもバックスラッシュにならないよう as_posix() を使う。
    """
    check_white = resource_path("assets/check_white.svg").as_posix()
    check_dark = resource_path("assets/check_dark.svg").as_posix()
    return stylesheet.replace("__CHECK_WHITE__", check_white).replace("__CHECK_DARK__", check_dark)


LIGHT_STYLESHEET = """
QWidget {
  background: #f7f9fb;
  color: #1f2933;
  font-size: 12pt;
}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateEdit, QTableWidget {
  background: #ffffff;
  color: #1f2933;
  border: 1px solid #c7d0d9;
  border-radius: 4px;
}
QHeaderView::section {
  background: #e9eef3;
  color: #1f2933;
  border: 1px solid #c7d0d9;
  font-size: 12pt;
  font-weight: bold;
  padding: 4px;
}
QCheckBox::indicator {
  width: 15px;
  height: 15px;
  border: 1px solid #777777;
  background: #ffffff;
}
QCheckBox::indicator:checked {
  border: 1px solid #0078d4;
  background: #0078d4;
  image: url(__CHECK_WHITE__);
}
QCheckBox::indicator:unchecked {
  border: 1px solid #777777;
  background: #ffffff;
}
QPushButton, QToolButton {
  background: #1f7a8c;
  color: #ffffff;
  border: 0;
  border-radius: 4px;
  padding: 6px 12px;
  font-size: 12pt;
}
QPushButton:hover, QToolButton:hover {
  background: #2a96ac;
}
QPushButton:checked, QToolButton:checked {
  background: #005f73;
  color: #ffffff;
  border: 2px solid #003f4f;
  font-weight: bold;
}
QPushButton:checked:hover, QToolButton:checked:hover {
  background: #00788f;
}
QPushButton:disabled, QToolButton:disabled {
  background: #9aa7b2;
}
QGroupBox {
  border: 1px solid #c7d0d9;
  border-radius: 4px;
  margin-top: 8px;
  padding-top: 12px;
}
"""

DARK_STYLESHEET = """
QWidget {
  background: #20252b;
  color: #eef2f6;
  font-size: 12pt;
}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateEdit, QTableWidget {
  background: #2b323a;
  color: #eef2f6;
  border: 1px solid #52606d;
  border-radius: 4px;
}
QHeaderView::section {
  background: #333c46;
  color: #eef2f6;
  border: 1px solid #52606d;
  font-size: 12pt;
  font-weight: bold;
  padding: 4px;
}
QCheckBox::indicator {
  width: 15px;
  height: 15px;
  border: 1px solid #9aa7b2;
  background: #20252b;
}
QCheckBox::indicator:checked {
  border: 1px solid #2f9bb3;
  background: #2f9bb3;
  image: url(__CHECK_WHITE__);
}
QCheckBox::indicator:unchecked {
  border: 1px solid #9aa7b2;
  background: #20252b;
}
QPushButton, QToolButton {
  background: #2f9bb3;
  color: #ffffff;
  border: 0;
  border-radius: 4px;
  padding: 6px 12px;
  font-size: 12pt;
}
QPushButton:hover, QToolButton:hover {
  background: #46b4cc;
}
QPushButton:checked, QToolButton:checked {
  background: #f2a900;
  color: #1f2933;
  border: 2px solid #ffd166;
  font-weight: bold;
}
QPushButton:checked:hover, QToolButton:checked:hover {
  background: #ffbf2e;
}
QPushButton:disabled, QToolButton:disabled {
  background: #52606d;
}
QGroupBox {
  border: 1px solid #52606d;
  border-radius: 4px;
  margin-top: 8px;
  padding-top: 12px;
}
"""
