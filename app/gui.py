from __future__ import annotations

import csv
import importlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QSettings, QStandardPaths, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPalette, QTextCursor
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
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QRadioButton,
    QStyle,
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
from app.build_features import updates_enabled
from app.cleanup_service import cleanup_old_files
from app.csv_processor import (
    create_output_csv,
    export_registration_records_to_csv,
    read_output_rows,
    unique_timestamp_csv_path,
    write_failed_csv,
    write_input_history,
    write_output_csv,
)
from app.csv_column_settings import (
    CsvColumnSetting,
    default_csv_column_settings,
    enabled_csv_columns,
    load_csv_column_settings,
    save_csv_column_settings,
)
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
    load_default_master,
    load_master,
    load_master_cached,
    lookup,
    read_csv_with_auto_encoding,
    restore_master,
    save_master,
)
from app.kintone_client import KintoneClient
from app.kintone_existing import (
    merge_existing_kintone_records_into_preview_rows,
    summarize_existing_reflection,
)
from app.logger import setup_logger
from app.models import AppConfig, PendingRegistration, ProcessResult, RunInput, TksDebugResult
from app.teams_notifier import default_teams_webhook_url_prod, default_teams_webhook_url_test
from app.preview_state import (
    DEFAULT_CUSTOMER_KEY,
    DEFAULT_KAKOU_TYPE,
    KAKOU_TYPE_CODES,
    KAKOU_TYPE_NAMES,
    PreviewState,
    kakou_type_label,
)
from app.tks_client import create_tks_client
from app.version import VERSION_CODE, VERSION_NAME
from app.window_geometry import apply_app_dpi_policy, clamp_window_to_available_geometry


SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
SETTINGS_THEME = "ui/theme"
SETTINGS_DEBUG_VISIBLE = "ui/debug_visible"
SETTINGS_KINTONE_TARGET = "kintone/target"
SETTINGS_TEAMS_ENABLED = "teams/enabled"
SETTINGS_TEAMS_WEBHOOK_URL_TEST = "teams/webhook_url_test"
SETTINGS_TEAMS_WEBHOOK_URL_PROD = "teams/webhook_url_prod"
SETTINGS_R2_OVERRIDES_KAKOU = "olap/kakou_r2_overrides"
SETTINGS_R2_OVERRIDES_SOBA = "olap/soba_r2_overrides"
# 登録前確認画面「CSV作成」の出力先フォルダ（要件3）。
SETTINGS_CSV_OUTPUT_DIR = "registration_preview/csv_output_dir"
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

# 登録前確認は最初の10件で操作可能にし、以降も10件単位でGUIへ反映する。
INITIAL_INTERACTIVE_ROW_COUNT = 10
BACKGROUND_LOAD_BATCH_SIZE = 10
_REGISTRATION_LOADING_TOOLTIP = "全レコードの確認が完了するまで登録できません。"
# close時にGUIスレッドでwaitしない一方、実行中QThreadのPython wrapperを
# 通信完了前に破棄しないための退避領域。
_DETACHED_RUNNING_THREADS: set[QThread] = set()
_SINGLE_INSTANCE_SERVER: QLocalServer | None = None
UPDATE_SHUTDOWN_COMMITTED_PROPERTY = "update_shutdown_committed"


def _detach_running_thread(worker: QThread) -> None:
    _DETACHED_RUNNING_THREADS.add(worker)
    worker.finished.connect(
        lambda current=worker: _DETACHED_RUNNING_THREADS.discard(current)
    )

# Kintone登録処理画面の出荷区分（AM・PM）で「なし」を表す選択肢ラベル。
# 「なし」選択時は登録値・登録前確認とも空欄扱いにする（要件3）。
SHUKKA_NONE_LABEL = "なし"

# 登録前確認の仕上日「なし」（空欄）を表す番兵日付。
# QDateEdit の minimumDate に設定し setSpecialValueText で「なし」と表示する（要件3・5）。
_SHIAGE_NONE_DATE = QDate(1900, 1, 1)

# 受注No入力欄の区切り文字（改行・カンマ・全角カンマ・空白・全角空白）。
# \s は全角空白(U+3000)も含むが、明示しておく（要件4）。
_ORDER_NO_SEPARATORS = re.compile(r"[,，　\s]+")


def parse_order_numbers(text: str) -> list[str]:
    """受注No入力欄のテキストを各受注Noへ分解する（要件4）。

    改行・カンマ・全角カンマ・空白・全角空白で区切り、前後空白を除去する。
    受注Noは文字列として扱い、先頭ゼロは保持する（数値変換しない）。
    """
    if not text:
        return []
    return [token for token in _ORDER_NO_SEPARATORS.split(text) if token]


def normalize_order_no(value: object) -> str:
    """受注Noを重複比較用に正規化する（前後空白除去・全角→半角）。

    空欄は空文字を返す。全角数字が入っても半角と同一視できるよう NFKC で正規化する。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text)


def find_duplicate_order_numbers(order_numbers: list[str]) -> list[str]:
    """受注Noリストの中で2回以上現れる受注Noを出現順で返す（正規化後で判定）。"""
    counts: dict[str, int] = {}
    first_form: dict[str, str] = {}
    order: list[str] = []
    for value in order_numbers:
        key = normalize_order_no(value)
        if not key:
            continue
        if key not in counts:
            counts[key] = 0
            first_form[key] = str(value).strip()
            order.append(key)
        counts[key] += 1
    return [first_form[key] for key in order if counts[key] > 1]


def dedupe_order_numbers(order_numbers: list[str]) -> list[str]:
    """受注Noリストから重複を除去し、初出の順序・表記を保持して返す（正規化後で判定）。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in order_numbers:
        key = normalize_order_no(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(str(value).strip())
    return result


def _override_shiage_text(finish_date: object) -> str:
    """override の仕上日（date | None）を登録前確認用の文字列へ変換する。

    None（＝「なし」）は空欄にする。
    """
    if finish_date is None:
        return ""
    strftime = getattr(finish_date, "strftime", None)
    if callable(strftime):
        return strftime("%Y-%m-%d")
    return str(finish_date)


def _override_shukka_text(am_pm: object) -> str:
    """override の AM・PM を登録前確認の出荷区分用の文字列へ変換する。

    None や "none"（＝「なし」）は空欄にする。
    """
    if am_pm is None:
        return ""
    text = str(am_pm).strip()
    if not text or text == "none":
        return ""
    return text


def apply_order_overrides(
    rows: list[dict[str, str]],
    overrides: dict[str, dict[str, object]],
) -> list[dict[str, str]]:
    """受注Noごとの override（仕上日／AM・PM）を行データへ適用した新リストを返す（要件5）。

    伝票作成・印刷画面から渡された値を、同一受注Noの全行（非表示行を含む）へ反映する。
    override が無い受注Noは元の値（Kintone登録処理画面の既定値）を維持する。
    """
    result: list[dict[str, str]] = []
    for row in rows:
        new_row = dict(row)
        order_no = new_row.get("受注No", "")
        override = overrides.get(order_no)
        if override is not None:
            new_row["仕上日"] = _override_shiage_text(override.get("finish_date"))
            new_row["出荷区分"] = _override_shukka_text(override.get("am_pm"))
        result.append(new_row)
    return result

PREVIEW_ROW_HEADERS = (
    "No", "受注No", "商品名称", "掛率集計コード", "掛率集計名称", "硝/加工",
    "加工種類", "仕上日", "出荷区分", "得意先選択", "判定加工名", "未登録警告",
)
_COL_NO = 0
_COL_ORDER_NO = 1
_COL_PRODUCT = 2
_COL_KAKURITSU_CODE = 3
_COL_KAKURITSU_NAME = 4
_COL_TYPE = 5
_COL_KAKOU_TYPE = 6
_COL_SHIAGE = 7
_COL_SHUKKA = 8
_COL_CUSTOMER = 9
_COL_KAKOU = 10
_COL_WARNING = 11

# 加工種類番号の凡例文（要件2）。KAKOU_TYPE_NAMES から動的生成して同期を保つ。
KAKOU_TYPE_LEGEND_TEXT = "加工種類：" + "、".join(
    f"{code}={name}" for code, name in KAKOU_TYPE_NAMES.items()
)

CONDITION_HEADERS =("フィールド論理名", "OLAP値", "OLAP範囲Val_From", "OLAP範囲Val_To", "OLAP空白", "OLAP条件グループ")
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


class KakouTypeEdit(QLineEdit):
    """加工種類のテキスト入力欄（要件1〜4）。

    通常表示は「1：四方」形式。フォーカスが入ると数値部分（"1"）のみを表示して
    上書き入力しやすくし、フォーカスアウト時に 1〜11 を判定して正式名称へ変換する。
    範囲外・不正・空欄の入力は静かに元の値へ戻す（警告ダイアログなし）。
    内部的にはコード値（"1".."11"）のみを保持する。
    """

    committed = Signal(str)  # 確定したコード値（"1".."11"）
    move_to_next_requested = Signal()  # Tab/Enter 押下時に次セルへ移動を要求（要件1〜3）

    def __init__(self, code: str) -> None:
        super().__init__()
        self._code = str(code).strip() or DEFAULT_KAKOU_TYPE
        self._show_label()

    def code(self) -> str:
        """現在保持しているコード値（"1".."11"）を返す。"""
        return self._code

    def _show_label(self) -> None:
        self.setText(kakou_type_label(self._code))

    def focusInEvent(self, event: object) -> None:
        # 数値部分のみ表示し、そのまま上書きできるよう全選択する。
        super().focusInEvent(event)  # type: ignore[arg-type]
        self.setText(self._code)
        self.selectAll()
        # クリック位置により selectAll() が解除される場合があるため遅延でも全選択する。
        QTimer.singleShot(0, self.selectAll)

    def mousePressEvent(self, event: object) -> None:
        # 初回クリックでも全選択を維持する。
        super().mousePressEvent(event)  # type: ignore[arg-type]
        QTimer.singleShot(0, self.selectAll)

    def focusOutEvent(self, event: object) -> None:
        self._commit()
        super().focusOutEvent(event)  # type: ignore[arg-type]

    def keyPressEvent(self, event: object) -> None:
        # Tab / Enter は入力を確定したうえで「加工種類の次のセル」へ移動する（要件1〜3）。
        # 通常のテーブル移動に任せず、移動先を呼び出し側で固定する。
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            self._commit()
            self.move_to_next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def _commit(self) -> None:
        text = self.text().strip()
        if text in KAKOU_TYPE_CODES:
            self._code = text
        # 範囲外・不正・空欄は元の値を維持（静かに戻す）。
        self._show_label()
        self.committed.emit(self._code)


class WorkerThread(QThread):
    log_line = Signal(str)
    credentials_validated = Signal(str, str)
    succeeded = Signal(object)
    pending_registration = Signal(object)
    kintone_checks_ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, run_input: RunInput) -> None:
        super().__init__()
        self.config = config
        self.run_input = run_input

    def run(self) -> None:
        worker_started = time.perf_counter()
        logger, log_file = setup_logger(self.config.paths.log_dir, self.log_line.emit)
        def log_phase(phase: str, phase_started: float, **fields: object) -> None:
            extras = " ".join(f"{key}={value}" for key, value in fields.items())
            logger.info(
                "event=perf_kintone_registration phase=%s elapsed_ms=%d %s",
                phase,
                int((time.perf_counter() - phase_started) * 1000),
                extras,
            )
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            work_dir = self.config.paths.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)

            logger.info("起動日時: %s", datetime.now().isoformat(timespec="seconds"))
            logger.info("アプリバージョン: %s (コード %s)", VERSION_NAME, VERSION_CODE)
            logger.info("対象受注No: %s", ",".join(self.run_input.denpyo_numbers))
            logger.info("仕上日: %s", self.run_input.shiage_date)
            logger.info("出荷区分: %s", self.run_input.shukka_kbn)

            write_input_history(
                work_dir / f"input_{timestamp}.csv",
                self.run_input.denpyo_numbers,
                self.run_input.shiage_date,
                self.run_input.shukka_kbn,
            )

            phase_started = time.perf_counter()
            tks_client = create_tks_client(self.config, logger)
            log_phase("kintone_settings_loaded", phase_started)
            if self.isInterruptionRequested():
                return
            phase_started = time.perf_counter()
            tks_client.login(self.run_input)
            log_phase("authentication_complete", phase_started, kintone_request_count=0)
            self.credentials_validated.emit(self.run_input.olap_login_id, self.run_input.olap_password)
            if self.isInterruptionRequested():
                return
            phase_started = time.perf_counter()
            soba_csv, kakou_csv = tks_client.fetch_csvs(self.run_input, work_dir, self.config.csv_encoding)
            log_phase("reference_data_fetched", phase_started)
            if self.isInterruptionRequested():
                return

            output_csv = work_dir / "outputTksToKintone.csv"
            phase_started = time.perf_counter()
            rows = create_output_csv(
                soba_csv,
                kakou_csv,
                output_csv,
                self.run_input.shiage_date,
                self.run_input.shukka_kbn,
            )
            log_phase("record_data_mapping", phase_started, total_count=len(rows))
            logger.info("outputTksToKintone.csv 出力件数: %s", len(rows))

            phase_started = time.perf_counter()
            output_rows = read_output_rows(output_csv)
            log_phase("registration_data_deep_copy", phase_started, total_count=len(output_rows))
            if self.isInterruptionRequested():
                return
            phase_started = time.perf_counter()
            master, master_cache_hit = load_master_cached(self.config.paths.kakou_master_csv)
            apply_kakou_names_per_row(output_rows, master)
            log_phase(
                "processing_name_and_warning_generation",
                phase_started,
                total_count=len(output_rows),
                master_cache_hit=str(master_cache_hit).lower(),
            )
            logger.info("加工名マスタ適用件数: %s", len(master))

            # 表示用データができた時点で先にGUIへ渡す。Kintone既存確認はこの後も
            # 同じworkerで継続し、登録可否だけは完了まで確定しない。
            pending = PendingRegistration(
                output_csv=output_csv,
                rows=output_rows,
                output_count=len(rows),
                log_file=log_file,
                timestamp=timestamp,
                kakou_master=master,
            )
            self.pending_registration.emit(pending)

            # Kintoneに同一受注Noの既存レコードがあるか確認する（登録前確認画面に反映するため）。
            # 検索に失敗しても処理を止めず、画面を開く前に警告で続行/中止を選べるようにする（要件11）。
            existing_records: list[dict[str, str]] = []
            existing_fetch_error: str | None = None
            kintone_client = KintoneClient(self.config, logger)
            phase_started = time.perf_counter()
            try:
                existing_records = kintone_client.fetch_existing_records_by_order_numbers(
                    self.run_input.denpyo_numbers
                )
                logger.info("Kintone既存レコード取得件数: %s", len(existing_records))
            except Exception as fetch_exc:  # noqa: BLE001 - 通信失敗でも落とさず警告に回す
                existing_fetch_error = str(fetch_exc) or fetch_exc.__class__.__name__
                logger.warning("Kintone既存データの検索に失敗しました: %s", existing_fetch_error)
            log_phase(
                "duplicate_and_registered_check",
                phase_started,
                duplicate_check_request_count=kintone_client.duplicate_check_request_count,
                kintone_request_count=kintone_client.request_count,
            )
            if self.isInterruptionRequested():
                return
            logger.info(
                "event=perf_kintone_registration phase=kintone_app_and_field_info "
                "elapsed_ms=0 field_schema_cache_hit=%s note=field_mapping_cache",
                str(kintone_client.field_mapping_cache_hit).lower(),
            )
            self.kintone_checks_ready.emit(existing_records, existing_fetch_error)
            logger.info(
                "event=perf_kintone_registration phase=kintone_checks_complete "
                "elapsed_ms=%d total_count=%d kintone_request_count=%d "
                "duplicate_check_request_count=%d field_schema_cache_hit=%s master_cache_hit=%s",
                int((time.perf_counter() - worker_started) * 1000),
                len(output_rows),
                kintone_client.request_count,
                kintone_client.duplicate_check_request_count,
                str(kintone_client.field_mapping_cache_hit).lower(),
                str(master_cache_hit).lower(),
            )
        except Exception as exc:
            logger.exception("処理中にエラーが発生しました")
            debug_file = _latest_debug_file(self.config.paths.work_dir)
            if debug_file is not None:
                logger.error("最新debugファイル: %s", debug_file)
            self.failed.emit(_format_error_message(exc, log_file, debug_file))


class RegistrationPrepareWorkerThread(QThread):
    """登録前確認用のplain dataを1つのworkerで10件ずつ準備する。

    QWidgetには一切触れず、override・既存Kintone値の突合とdeep copyだけを行い、
    GUIへは list[dict] のバッチを送る。
    """

    batch_ready = Signal(int, object, object)
    succeeded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        generation: int,
        rows: list[dict[str, str]],
        existing_records: list[dict[str, str]],
        overrides: dict[str, dict[str, object]],
    ) -> None:
        super().__init__()
        self.generation = generation
        self.rows = rows
        self.existing_records = existing_records
        self.overrides = overrides

    def run(self) -> None:
        started = time.perf_counter()
        try:
            copy_started = time.perf_counter()
            overridden = apply_order_overrides(self.rows, self.overrides)
            copy_ms = int((time.perf_counter() - copy_started) * 1000)

            existing_started = time.perf_counter()
            prepared_rows, existing_by_row = merge_existing_kintone_records_into_preview_rows(
                overridden, self.existing_records
            )
            existing_ms = int((time.perf_counter() - existing_started) * 1000)
            validation_started = time.perf_counter()
            validation_errors = [
                f"{index + 1}行目: 検索キーが空です"
                for index, row in enumerate(prepared_rows)
                if not str(row.get("検索キー", "")).strip()
            ]
            validation_ms = int((time.perf_counter() - validation_started) * 1000)
            logging.getLogger("tks_to_kintone_app").info(
                "event=perf_kintone_registration phase=target_rows_and_existing_merge "
                "elapsed_ms=%d deep_copy_ms=%d total_count=%d",
                existing_ms,
                copy_ms,
                len(prepared_rows),
            )

            for start in range(0, len(prepared_rows), BACKGROUND_LOAD_BATCH_SIZE):
                if self.isInterruptionRequested():
                    return
                end = start + BACKGROUND_LOAD_BATCH_SIZE
                # signal境界を越えるデータはworker所有のmutable objectと共有しない。
                row_batch = [dict(row) for row in prepared_rows[start:end]]
                existing_batch = [dict(row) for row in existing_by_row[start:end]]
                self.batch_ready.emit(self.generation, row_batch, existing_batch)

            self.succeeded.emit(
                self.generation,
                {
                    "total_count": len(prepared_rows),
                    "deep_copy_ms": copy_ms,
                    "existing_check_ms": existing_ms,
                    "validation_ms": validation_ms,
                    "validation_errors": validation_errors,
                    "worker_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        except Exception as exc:  # noqa: BLE001 - GUI側へ安全に通知する
            logging.getLogger("tks_to_kintone_app").exception(
                "登録前確認データの準備に失敗しました"
            )
            self.failed.emit(self.generation, str(exc) or exc.__class__.__name__)


class RegistrationExistingMergeWorkerThread(QThread):
    """遅れて完了したKintone既存確認をplain dataへ反映するworker。"""

    succeeded = Signal(int, object, object, object)
    failed = Signal(int, str)

    def __init__(
        self,
        generation: int,
        rows: list[dict[str, str]],
        existing_records: list[dict[str, str]],
    ) -> None:
        super().__init__()
        self.generation = generation
        self.rows = rows
        self.existing_records = existing_records

    def run(self) -> None:
        started = time.perf_counter()
        try:
            merged_rows, existing_by_row = merge_existing_kintone_records_into_preview_rows(
                self.rows, self.existing_records
            )
            if self.isInterruptionRequested():
                return
            self.succeeded.emit(
                self.generation,
                merged_rows,
                existing_by_row,
                {"existing_merge_worker_ms": int((time.perf_counter() - started) * 1000)},
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("tks_to_kintone_app").exception(
                "Kintone既存データの反映に失敗しました"
            )
            self.failed.emit(self.generation, str(exc) or exc.__class__.__name__)


class KintoneRegisterWorkerThread(QThread):
    log_line = Signal(str)
    succeeded = Signal(object)
    registration_completed = Signal(list)
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
            successful_order_numbers = _unique_order_numbers(kintone_result.successful_records)
            if successful_order_numbers:
                logger.info("kintone登録成功受注No: %s", "、".join(successful_order_numbers))
                self.registration_completed.emit(successful_order_numbers)
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
                logger.info("対象受注No件数: %s", len(self.run_input.denpyo_numbers))
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
            update_client = _update_client_module()
            self.succeeded.emit(update_client.UpdateClient().check_for_update(VERSION_CODE))
        except Exception as exc:
            self.failed.emit(str(exc))


def _update_client_module():
    return importlib.import_module("app." + "update_client")


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

    def __init__(
        self,
        customer_labels: dict[str, str],
        customer_match_patterns: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("得意先ヘッダー設定")
        self.resize(520, 340)

        self._edits: dict[str, QLineEdit] = {}
        self._match_edits: dict[str, QLineEdit] = {}
        form = QFormLayout()
        for key in ("得意先1", "得意先2", "得意先3", "得意先4"):
            edit = QLineEdit(customer_labels.get(key, CUSTOMER_LABEL_DEFAULTS[key]))
            edit.setMaxLength(CUSTOMER_LABEL_MAX_LEN)
            self._edits[key] = edit
            form.addRow(f"{key} 表示名:", edit)
            match_edit = QLineEdit((customer_match_patterns or {}).get(key, ""))
            self._match_edits[key] = match_edit
            form.addRow(f"{key} 判定文字列:", match_edit)

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
            self._match_edits[key].setText("")

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

    def result_match_patterns(self) -> dict[str, str]:
        """保存後の得意先名称判定文字列を返す。"""
        return {key: edit.text().strip() for key, edit in self._match_edits.items()}


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
        clamp_window_to_available_geometry(
            self,
            desired_width=1520,
            desired_height=720,
            min_width=1100,
            min_height=560,
        )
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.master_path = master_path
        self.backup_dir = backup_dir
        self._dirty = False

        _cl = customer_labels or {}
        display_headers = [_cl.get(h, h) for h in KAKOU_MASTER_HEADERS]

        self.table = QTableWidget()
        self.table.setColumnCount(len(KAKOU_MASTER_HEADERS))
        self.table.setHorizontalHeaderLabels(display_headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.itemChanged.connect(self._on_item_changed)

        self._load_from_file()
        self._apply_initial_column_widths()

        add_btn = QPushButton("行追加")
        del_btn = QPushButton("行削除")
        import_btn = QPushButton("CSVインポート")
        export_btn = QPushButton("CSVエクスポート")
        backup_btn = QPushButton("バックアップ作成")
        restore_btn = QPushButton("バックアップから復元")
        reset_default_btn = QPushButton("初期値に戻す")

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(import_btn)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(backup_btn)
        btn_row.addWidget(restore_btn)
        btn_row.addWidget(reset_default_btn)

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
        reset_default_btn.clicked.connect(self._reset_to_default)
        save_btn.clicked.connect(self._save_and_close)
        close_btn.clicked.connect(self._close_with_confirm)

    def _apply_initial_column_widths(self) -> None:
        widths = {
            "メーカー識別掛率集計コード": 190,
            "メーカー識別コード": 130,
            "掛率集計コード": 120,
            "掛率集計名称": 210,
            "掛率集計略称": 160,
            "加工名": 170,
            "得意先1": 130,
            "得意先2": 130,
            "得意先3": 130,
            "得意先4": 130,
        }
        for col, header in enumerate(KAKOU_MASTER_HEADERS):
            self.table.setColumnWidth(col, widths.get(header, 120))

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

    def _reset_to_default(self) -> None:
        from app.settings_service import find_default_kakou_master_csv

        reply = QMessageBox.question(
            self,
            "初期値に戻す",
            "加工名マスタを初期値に戻します。\n"
            "現在の内容は初期CSVの内容で上書きされます。\n"
            "よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 起動時自動投入と同じ共通探索関数を使う（候補が無ければログに候補パスを出す）。
        default_csv = find_default_kakou_master_csv()
        default_rows = load_default_master(default_csv) if default_csv else []
        if not default_rows:
            QMessageBox.critical(
                self, "初期値に戻す", "初期CSVが見つからない、または空のため初期化できません。"
            )
            return

        # 上書き前に現在の内容を必ずバックアップする。
        if self._dirty:
            save_master(self.master_path, self._table_to_rows())
            self._dirty = False
        backup = backup_master(self.master_path, self.backup_dir)

        save_master(self.master_path, default_rows)
        self._load_from_file()

        message = f"初期CSVの内容（{len(default_rows)}件）に戻しました。"
        if backup:
            message += f"\n現在の内容は以下にバックアップしました:\n{backup}"
        QMessageBox.information(self, "初期値に戻す", message)

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


_CONFIRM_LOGGER = logging.getLogger("tks_to_kintone_app")


class CsvColumnSettingsDialog(QDialog):
    """登録前確認CSVの列順と出力対象を編集するダイアログ。"""

    def __init__(self, columns: list[CsvColumnSetting], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CSV列設定")
        self.resize(520, 560)
        self._saved_columns: list[CsvColumnSetting] | None = None

        self.column_list = QListWidget()
        self.column_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.column_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.column_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.column_list.setToolTip("ドラッグ＆ドロップ、または「上へ」「下へ」で列順を変更できます。")
        self._populate(columns)

        self.up_button = QPushButton("上へ")
        self.down_button = QPushButton("下へ")
        self.reset_button = QPushButton("初期順序に戻す")
        self.save_button = QPushButton("保存して閉じる")
        self.cancel_button = QPushButton("キャンセル")
        self.up_button.clicked.connect(lambda: self._move_current(-1))
        self.down_button.clicked.connect(lambda: self._move_current(1))
        self.reset_button.clicked.connect(self._reset_to_default)
        self.save_button.clicked.connect(self._save_and_close)
        self.cancel_button.clicked.connect(self.reject)

        order_buttons = QVBoxLayout()
        order_buttons.addWidget(self.up_button)
        order_buttons.addWidget(self.down_button)
        order_buttons.addStretch(1)

        list_row = QHBoxLayout()
        list_row.addWidget(self.column_list, 1)
        list_row.addLayout(order_buttons)

        bottom = QHBoxLayout()
        bottom.addWidget(self.reset_button)
        bottom.addStretch(1)
        bottom.addWidget(self.save_button)
        bottom.addWidget(self.cancel_button)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("CSVへ出力する列を選び、出力順を設定してください。"))
        root.addLayout(list_row, 1)
        root.addLayout(bottom)

    def _populate(self, columns: list[CsvColumnSetting]) -> None:
        from app.csv_column_settings import STANDARD_CSV_COLUMNS

        labels = {column.key: column.header for column in STANDARD_CSV_COLUMNS}
        self.column_list.clear()
        for column in columns:
            if column.key not in labels:
                continue
            item = QListWidgetItem(labels[column.key])
            item.setData(Qt.ItemDataRole.UserRole, column.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if column.enabled else Qt.CheckState.Unchecked)
            self.column_list.addItem(item)
        if self.column_list.count():
            self.column_list.setCurrentRow(0)

    def column_settings(self) -> list[CsvColumnSetting]:
        result: list[CsvColumnSetting] = []
        for index in range(self.column_list.count()):
            item = self.column_list.item(index)
            result.append(
                CsvColumnSetting(
                    str(item.data(Qt.ItemDataRole.UserRole)),
                    item.checkState() == Qt.CheckState.Checked,
                )
            )
        return result

    def saved_column_settings(self) -> list[CsvColumnSetting] | None:
        return self._saved_columns

    def _move_current(self, offset: int) -> None:
        current = self.column_list.currentRow()
        target = current + offset
        if current < 0 or target < 0 or target >= self.column_list.count():
            return
        item = self.column_list.takeItem(current)
        self.column_list.insertItem(target, item)
        self.column_list.setCurrentRow(target)

    def _reset_to_default(self) -> None:
        self._populate(default_csv_column_settings())

    def _save_and_close(self) -> None:
        columns = self.column_settings()
        if not any(column.enabled for column in columns):
            QMessageBox.warning(self, "CSV列設定", "出力する列を1つ以上選択してください。")
            return
        self._saved_columns = columns
        self.accept()


class RegistrationPreviewDialog(QDialog):
    """登録前確認ダイアログ。

    内部状態は PreviewState で管理する。Qt ウィジェットは表示用であり、
    登録データは必ず PreviewState.build_registration_rows() から生成する。
    絞り込みフィルタは表示制御のみで、登録対象は常に全CSVレコードである。
    """

    loading_cancel_requested = Signal()
    retry_requested = Signal()

    def __init__(
        self,
        rows: list[dict[str, str]],
        shukka_options: list[str],
        master: list[dict[str, str]],
        customer_labels: dict[str, str],
        customer_match_patterns: dict[str, str] | None = None,
        parent: QWidget | None = None,
        preview_color_theme: str = "light",
        debug_visible: bool = False,
        kintone_existing_by_row: list[dict[str, str]] | None = None,
        progressive_loading: bool = False,
        request_started: float | None = None,
        generation: int = 0,
    ) -> None:
        super().__init__(parent)
        _confirm_init_start = time.perf_counter()
        self._request_started = request_started or _confirm_init_start
        self._progressive_loading = progressive_loading
        self._generation = generation
        # 受注Noごとに、得意先選択を誰が決めたかを保持する。非同期結果の反映時に
        # 自動設定・ユーザー設定を暗黙に上書きしないための所有権情報。
        self._customer_selection_source_by_order: dict[str, str] = {}
        self._all_rows_ready = not progressive_loading
        self._load_failed = False
        self._first_batch_logged = False
        self._gui_row_generation_ms = 0
        self._total_count = len(rows)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_init_started %s", {"row_count": len(rows)}
        )
        # 起動時に大量セルウィジェットを一括生成する際は再描画を抑制する（要件3）。
        self._cell_widget_count = 0
        self.setWindowTitle("登録前確認")
        # 縦幅を約1.5cm狭め、横幅は「表の実際の列幅合計」に合わせて狭める（要件1）。
        # 旧実装は固定幅1520pxで、列幅を狭めても未登録警告列の右に大きな余白が残っていた。
        # 初期幅はレイアウト確定後に _apply_content_based_geometry() で列幅合計から算出する。
        self._CONFIRM_DESIRED_HEIGHT = 645  # 従来700から55px（約1.5cm）縮小
        self._CONFIRM_MIN_WIDTH = 820  # 下部ボタン・CSV出力先欄・参照ボタンが見切れない最小幅
        self._CONFIRM_MIN_HEIGHT = 505  # 従来560から縮小
        # 参考: 旧固定初期幅（この値より狭くなることを検証する）。
        self._CONFIRM_PREVIOUS_FIXED_WIDTH = 1520
        _CONFIRM_LOGGER.info(
            "kintone_confirm_window_height_reduced %s",
            {"old_height": 700, "new_height": self._CONFIRM_DESIRED_HEIGHT, "reduced_px": 700 - self._CONFIRM_DESIRED_HEIGHT},
        )
        self._master = master
        self._shukka_options = shukka_options
        self._customer_match_patterns = customer_match_patterns or {}
        self._preview_color_theme = preview_color_theme
        # PreviewState が唯一の内部データモデル
        # Kintone既存データの行単位反映値を渡し、加工名・加工mm・㎡ などをKintone値で優先表示する。
        existing_by_row = [dict(item) for item in (kintone_existing_by_row or [])]

        def _build_state() -> PreviewState:
            return PreviewState(
                rows=[dict(row) for row in rows],
                kintone_existing_by_row=existing_by_row,
            )

        # Kintone既存データの行単位反映（既存確認）の所要時間を計測する（要件3）。
        self._state = self._confirm_timed("kintone_confirm_existing_check", _build_state)

        self._kakou_options: list[tuple[str, str]] = [("selected", "選択なし")]
        for key in CUSTOMER_KEYS:
            self._kakou_options.append((key, customer_labels.get(key, key)))

        # ウィジェットリスト（行インデックスと1対1対応）
        # 受注No先頭行のみウィジェットを持ち、2行目以降は None
        self._shiage_widgets: list[PopupDateEdit | None] = []
        self._shukka_widgets: list[QComboBox | None] = []
        self._customer_widgets: list[QComboBox | None] = []
        self._kakou_type_widgets: list[KakouTypeEdit | None] = []
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
        # 判定加工名（旧Stretch）を Interactive の固定幅にして狭める。Stretch のままだと
        # 横幅縮小時に潰れ、逆に画面を広げると過度に伸びるため、明示幅で制御する。
        # 列幅は表の実幅（＝ウィンドウ幅）を決める主因。右側余白を消して画面全体を
        # 狭めるため、内容が短い/tooltipで補える列をさらに詰める（要件1）。
        self.table.setColumnWidth(_COL_NO, 34)
        self.table.setColumnWidth(_COL_ORDER_NO, 110)
        self.table.setColumnWidth(_COL_PRODUCT, 270)
        self.table.setColumnWidth(_COL_KAKURITSU_CODE, 84)
        self.table.setColumnWidth(_COL_KAKURITSU_NAME, 145)
        self.table.setColumnWidth(_COL_TYPE, 46)
        self.table.setColumnWidth(_COL_KAKOU_TYPE, 84)
        self.table.setColumnWidth(_COL_SHIAGE, 130)
        self.table.setColumnWidth(_COL_SHUKKA, 72)
        self.table.setColumnWidth(_COL_CUSTOMER, 155)
        # 判定加工名: 旧Stretch（約280px）→168px。長い加工名はtooltipで確認可能。
        _DETECTED_PROCESS_NAME_WIDTH = 168
        # 未登録警告: 185px→96px。全文はセルtooltipで確認可能。
        _UNREGISTERED_WARNING_WIDTH = 96
        self.table.setColumnWidth(_COL_KAKOU, _DETECTED_PROCESS_NAME_WIDTH)
        self.table.setColumnWidth(_COL_WARNING, _UNREGISTERED_WARNING_WIDTH)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_column_width_adjusted %s",
            {
                "detected_process_name": _DETECTED_PROCESS_NAME_WIDTH,
                "unregistered_warning": _UNREGISTERED_WARNING_WIDTH,
            },
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_detected_process_name_width %s",
            {"old": 190, "new": _DETECTED_PROCESS_NAME_WIDTH},
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_unregistered_warning_width %s",
            {"old": 110, "new": _UNREGISTERED_WARNING_WIDTH},
        )
        # 大量行のセルウィジェット生成中はテーブルの再描画・シグナルを止めて軽くする（要件3）。
        self._cell_widgets_started = time.perf_counter()
        _CONFIRM_LOGGER.info("kintone_confirm_cell_widgets_started")
        self.table.setUpdatesEnabled(False)
        _prev_block = self.table.blockSignals(True)
        try:
            self._confirm_timed("kintone_confirm_table_populate", self._populate_table)
        finally:
            self.table.blockSignals(_prev_block)
            self.table.setUpdatesEnabled(True)
        # 生成したセルウィジェット数（判定加工名・未登録警告は全行、他は受注No先頭行のみ）。
        # 全行全セルに QWidget を敷かず、編集が必要な列だけに限定していることを可視化する。
        _combobox_count = (
            sum(1 for w in self._shukka_widgets if w is not None)
            + sum(1 for w in self._customer_widgets if w is not None)
        )
        _dateedit_count = sum(1 for w in self._shiage_widgets if w is not None)
        _lineedit_count = sum(1 for w in self._kakou_type_widgets if w is not None)
        self._cell_widget_count = (
            len(self._kakou_labels)
            + len(self._state.rows)
            + _combobox_count
            + _dateedit_count
            + _lineedit_count
        )
        _CONFIRM_LOGGER.info("kintone_confirm_rows_count %s", {"rows_count": len(self._state.rows)})
        _CONFIRM_LOGGER.info(
            "kintone_confirm_cell_widget_count %s", {"cell_widget_count": self._cell_widget_count}
        )
        _CONFIRM_LOGGER.info("kintone_confirm_combobox_count %s", {"combobox_count": _combobox_count})
        _CONFIRM_LOGGER.info("kintone_confirm_dateedit_count %s", {"dateedit_count": _dateedit_count})
        _CONFIRM_LOGGER.info("kintone_confirm_lineedit_count %s", {"lineedit_count": _lineedit_count})
        _cell_widgets_ms = int((time.perf_counter() - self._cell_widgets_started) * 1000)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_cell_widgets_finished %s",
            {"elapsed_ms": _cell_widgets_ms, "cell_widget_count": self._cell_widget_count},
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_cell_widgets_elapsed_ms %s",
            {"elapsed_ms": _cell_widgets_ms, "cell_widget_count": self._cell_widget_count},
        )
        if _cell_widgets_ms >= 300:
            _CONFIRM_LOGGER.warning(
                "kintone_confirm_slow_step_detected %s",
                {"step": "kintone_confirm_cell_widgets", "elapsed_ms": _cell_widgets_ms},
            )
        self._apply_initial_customer_matches()

        # 下部レイアウト・ボタン・警告バナーの構築時間を計測する（要件3）。
        _build_ui_start = time.perf_counter()
        _CONFIRM_LOGGER.info("kintone_confirm_build_ui_started")
        # 未登録警告バナー
        all_warnings: list[str] = []
        for row in self._state.rows:
            w = _row_unregistered_warning(row, master)
            if w:
                all_warnings.append(w)

        buttons = QDialogButtonBox()
        self.register_button = buttons.addButton("登録", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = buttons.addButton("登録キャンセル", QDialogButtonBox.ButtonRole.RejectRole)

        # CSV出力先の入力欄・参照ボタン・CSV作成ボタン（要件2・3・4）。
        # CSV作成は kintone へ送信せず、登録ボタン押下時と同じ登録用データを確認用に書き出す。
        self.csv_output_dir_edit = QLineEdit()
        self.csv_output_dir_edit.setPlaceholderText("CSV出力先フォルダ")
        self.csv_output_dir_edit.setToolTip(
            "CSV作成ボタンで保存するフォルダを指定してください。kintoneへは送信しません。"
        )
        self.csv_browse_button = QPushButton("参照")
        self.csv_browse_button.clicked.connect(self._browse_csv_output_dir)
        self.csv_create_button = QPushButton("CSV作成")
        self.csv_create_button.setToolTip("登録ボタン押下時と同じ登録用データをCSVへ出力します（kintone登録はしません）。")
        self.csv_create_button.clicked.connect(self._on_create_csv)
        self.csv_column_settings_button = QPushButton("CSV列設定")
        self.csv_column_settings_button.setToolTip("CSVファイルに出力する列と並び順を設定します。")
        self.csv_column_settings_button.clicked.connect(self._open_csv_column_settings)
        self._load_csv_output_dir()

        csv_row = QHBoxLayout()
        csv_row.addWidget(QLabel("CSV出力先:"))
        csv_row.addWidget(self.csv_output_dir_edit, 1)
        csv_row.addWidget(self.csv_browse_button)

        bottom = QHBoxLayout()
        self.retry_button = QPushButton("再試行")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self.retry_requested)
        bottom.addWidget(self.retry_button)
        self.print_button: QPushButton | None = None
        if debug_visible:
            self.print_button = QPushButton("印刷")
            self.print_button.clicked.connect(self._print_slips)
            bottom.addWidget(self.print_button)
        bottom.addStretch(1)
        bottom.addWidget(self.csv_column_settings_button)
        bottom.addWidget(self.csv_create_button)
        bottom.addWidget(buttons)

        # 登録対象データが無い場合は CSV作成ボタンを無効化する（要件12）。
        self.csv_create_button.setEnabled(bool(self._state.rows))

        root = QVBoxLayout()
        root.addLayout(filter_row)
        root.addWidget(QLabel(
            "仕上日・出荷区分・得意先選択は受注No先頭行にのみ表示されます。"
            "変更は同じ受注Noの全行（非表示行を含む）に反映されます。"
            "加工名は行ごとに判定されます。"
        ))
        # 加工種類番号の凡例（要件2・4）。上部の他の説明文と同じ QLabel スタイル
        # （専用の小さいフォント指定を外し、既定サイズ・既定色に合わせる）。
        kakou_type_legend = QLabel(KAKOU_TYPE_LEGEND_TEXT)
        root.addWidget(kakou_type_legend)
        self.loading_status_label = QLabel()
        self.loading_status_label.setObjectName("registrationLoadingStatus")
        if progressive_loading:
            self.loading_status_label.setText("Kintone登録準備中\n0件を表示しました\nデータを確認しています…")
        else:
            self.loading_status_label.setText(f"{len(self._state.rows)}件を表示しました")
        root.addWidget(self.loading_status_label)
        self.warning_label = QLabel()
        self.warning_label.setStyleSheet("color: #cc7700;")
        self.warning_label.setVisible(bool(all_warnings))
        if all_warnings:
            unique_warnings = list(dict.fromkeys(all_warnings))
            self.warning_label.setText(
                "未登録の掛率集計コードがあります:\n" + "\n".join(unique_warnings)
            )
        root.addWidget(self.warning_label)
        root.addWidget(self.table, 1)
        root.addLayout(csv_row)
        root.addLayout(bottom)
        self.setLayout(root)

        self.register_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        if progressive_loading:
            self.register_button.setEnabled(False)
            self.register_button.setToolTip(_REGISTRATION_LOADING_TOOLTIP)
            self.csv_create_button.setEnabled(False)
            self.csv_create_button.setToolTip(_REGISTRATION_LOADING_TOOLTIP)
            self.csv_column_settings_button.setEnabled(False)
            if self.print_button is not None:
                self.print_button.setEnabled(False)
                self.print_button.setToolTip(_REGISTRATION_LOADING_TOOLTIP)

        _build_ui_ms = int((time.perf_counter() - _build_ui_start) * 1000)
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=ui_components_created elapsed_ms=%d "
            "total_count=%d",
            _build_ui_ms,
            len(self._state.rows),
        )
        _CONFIRM_LOGGER.info("kintone_confirm_build_ui_finished %s", {"elapsed_ms": _build_ui_ms})
        _CONFIRM_LOGGER.info("kintone_confirm_build_ui_elapsed_ms %s", {"elapsed_ms": _build_ui_ms})
        if _build_ui_ms >= 300:
            _CONFIRM_LOGGER.warning(
                "kintone_confirm_slow_step_detected %s",
                {"step": "kintone_confirm_build_ui", "elapsed_ms": _build_ui_ms},
            )

        # レイアウト確定後に、表の実際の列幅合計に合わせて初期ウィンドウ幅を算出する（要件4・5）。
        self._confirm_timed("kintone_confirm_geometry", self._apply_content_based_geometry)

        _confirm_init_ms = int((time.perf_counter() - _confirm_init_start) * 1000)
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=registration_window_created "
            "elapsed_ms=%d total_count=%d",
            _confirm_init_ms,
            len(self._state.rows),
        )
        _CONFIRM_LOGGER.info("kintone_confirm_init_finished %s", {"elapsed_ms": _confirm_init_ms})
        _CONFIRM_LOGGER.info("kintone_confirm_init_elapsed_ms %s", {"elapsed_ms": _confirm_init_ms})
        if _confirm_init_ms >= 1000:
            _CONFIRM_LOGGER.warning(
                "kintone_confirm_slow_step_detected %s",
                {"step": "kintone_confirm_init", "elapsed_ms": _confirm_init_ms},
            )

    @staticmethod
    def _confirm_timed(event: str, func):
        """登録前確認画面の主要ステップ所要時間(ms)を計測しログへ出す（要件3）。

        `{event}_started` / `{event}_finished` / `{event}_elapsed_ms` を出力し、
        300ms以上は kintone_confirm_slow_step_detected を出す。例外は伝播する。
        """
        start = time.perf_counter()
        _CONFIRM_LOGGER.info("%s_started", event)
        try:
            return func()
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            _CONFIRM_LOGGER.info("%s_finished %s", event, {"elapsed_ms": elapsed_ms})
            _CONFIRM_LOGGER.info("%s_elapsed_ms %s", event, {"elapsed_ms": elapsed_ms})
            if elapsed_ms >= 300:
                _CONFIRM_LOGGER.warning(
                    "kintone_confirm_slow_step_detected %s",
                    {"step": event, "elapsed_ms": elapsed_ms},
                )

    def _apply_content_based_geometry(self) -> None:
        """設定した列幅の合計から初期ウィンドウ幅を明示的に決めて狭める（要件4・5）。

        前回修正では clamp 経由の resize だけで幅を決めていたが、実機で幅が縮まらなかった。
        原因は (1) `horizontalHeader().length()` ではなく実設定列幅で算出すべきだったこと、
        (2) CSV出力先欄(QLineEdit, stretch=1)がウィンドウ幅を引っ張っていたこと、
        (3) resize 後に layout/exec で幅が広がる環境があったこと。
        本メソッドでは実列幅合計から target を計算し、`resize` に加えて初回表示中だけ
        `setMaximumWidth(target)` を掛けて幅を強制し、右側余白をなくす。maximumWidth は
        showEvent 後に解除してユーザーが手動で広げられるようにする。
        """
        initial_width = self.width()
        _CONFIRM_LOGGER.info(
            "kintone_confirm_initial_width_before_resize %s", {"width": initial_width}
        )

        header = self.table.horizontalHeader()
        header_length = int(header.length())
        # header.length() ではなく、実際に setColumnWidth した列幅の合計を基準にする（要件4）。
        column_width_sum = sum(
            self.table.columnWidth(c) for c in range(self.table.columnCount())
        )
        viewport_width = int(self.table.viewport().width())
        vheader_width = self.table.verticalHeader().width() if self.table.verticalHeader().isVisible() else 0

        frame_width = self.table.frameWidth() * 2
        # 縦スクロールバー1本分は表の実幅に含める（多数行でも最終列が横スクロールなしで見える）。
        scrollbar_width = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)

        _CONFIRM_LOGGER.info(
            "kintone_confirm_table_column_width_sum %s",
            {"column_width_sum": column_width_sum},
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_table_header_length %s",
            {"header_length": header_length, "vheader_width": vheader_width},
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_table_viewport_width %s",
            {"viewport_width": viewport_width},
        )

        # 表の実幅（縦ヘッダ + 列幅合計 + 枠 + 縦スクロールバー分）。
        table_width = vheader_width + column_width_sum + frame_width + scrollbar_width
        # テーブルが横に伸びて右側に余白を作らないよう、実幅で上限を固定する（右側余白の主因対策）。
        self.table.setMaximumWidth(table_width)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_table_width_target %s",
            {"table_width": table_width, "scrollbar_width": scrollbar_width, "frame_width": frame_width},
        )

        # CSV出力先欄（stretch=1 の QLineEdit）がウィンドウ幅を引っ張らないよう、
        # 最大幅を表幅以下に抑える（要件4・5）。下部ボタン行も同様に表幅内へ収める。
        self.csv_output_dir_edit.setMaximumWidth(table_width)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_csv_row_width_policy %s",
            {
                "csv_edit_max_width": table_width,
                "note": "expanding_but_capped_to_table_width",
            },
        )

        margins = self.layout().contentsMargins()
        layout_margins = margins.left() + margins.right()
        safety_margin = 4
        dialog_target_width = table_width + layout_margins + safety_margin
        _CONFIRM_LOGGER.info(
            "kintone_confirm_dialog_width_target %s",
            {
                "dialog_target_width": dialog_target_width,
                "layout_margins": layout_margins,
                "safety_margin": safety_margin,
            },
        )

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            max_allowed_w = int(avail.width() * 0.95)
            max_allowed_h = int(avail.height() * 0.95)
        else:
            max_allowed_w = dialog_target_width
            max_allowed_h = self._CONFIRM_DESIRED_HEIGHT
        # 画面外に出ないよう上限だけ掛ける（広げる方向には使わない）。
        final_target_width = min(dialog_target_width, max_allowed_w)
        min_width = min(self._CONFIRM_MIN_WIDTH, max_allowed_w)
        final_target_width = max(final_target_width, min_width)
        final_height = min(self._CONFIRM_DESIRED_HEIGHT, max_allowed_h)
        min_height = min(self._CONFIRM_MIN_HEIGHT, max_allowed_h)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_target_dialog_width %s",
            {"target_width": final_target_width, "target_height": final_height},
        )
        # clamp が幅を広げていないことを検証ログに残す（要件4）。
        _CONFIRM_LOGGER.info(
            "kintone_confirm_clamp_did_not_expand_width %s",
            {
                "dialog_target_width": dialog_target_width,
                "final_target_width": final_target_width,
                "expanded": final_target_width > dialog_target_width,
            },
        )

        # 最小サイズ→resize→初回表示中のみ maximumWidth 強制、の順で確実に幅を適用する。
        self.setMinimumSize(min_width, min_height)
        self.resize(final_target_width, final_height)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_resize_applied %s",
            {"width": final_target_width, "height": final_height},
        )
        # 初回表示で layout/exec が幅を広げるのを防ぐため maximumWidth を掛ける。
        # showEvent 後に解除してユーザーが手動で広げられるようにする（要件4）。
        self._initial_target_width = final_target_width
        self._initial_max_width_locked = True
        self.setMaximumWidth(final_target_width)

        _CONFIRM_LOGGER.info(
            "kintone_confirm_width_after_resize %s",
            {"width": self.width(), "height": self.height()},
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_minimum_width %s", {"minimum_width": self.minimumWidth()}
        )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_maximum_width %s", {"maximum_width": self.maximumWidth()}
        )

        right_blank = max(0, final_target_width - table_width)
        _CONFIRM_LOGGER.info(
            "kintone_confirm_right_blank_width %s",
            {"right_blank_width": right_blank, "window_width": final_target_width, "table_width": table_width},
        )
        if right_blank >= 50:
            _CONFIRM_LOGGER.warning(
                "kintone_confirm_right_blank_warning %s",
                {"right_blank_width": right_blank, "window_width": final_target_width, "table_width": table_width},
            )
        if final_target_width < self._CONFIRM_PREVIOUS_FIXED_WIDTH:
            _CONFIRM_LOGGER.info(
                "kintone_confirm_window_width_reduced_to_table %s",
                {
                    "previous_fixed_width": self._CONFIRM_PREVIOUS_FIXED_WIDTH,
                    "new_width": final_target_width,
                    "reduced_px": self._CONFIRM_PREVIOUS_FIXED_WIDTH - final_target_width,
                    "table_width": table_width,
                },
            )
        _CONFIRM_LOGGER.info(
            "kintone_confirm_window_final_width %s",
            {"width": final_target_width, "height": final_height},
        )
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=column_width_calculated "
            "elapsed_ms=0 column_count=%d column_width_sum=%d",
            self.table.columnCount(),
            column_width_sum,
        )

    def showEvent(self, event) -> None:  # noqa: N802
        """初回表示時に幅ロックを解除し、最大化表示を検出・記録する（要件4）。"""
        super().showEvent(event)
        if not getattr(self, "_window_shown_logged", False):
            self._window_shown_logged = True
            elapsed_ms = int((time.perf_counter() - self._request_started) * 1000)
            _CONFIRM_LOGGER.info(
                "event=perf_kintone_registration phase=window_shown elapsed_ms=%d "
                "total_count=%d first_batch_count=%d batch_size=%d",
                elapsed_ms,
                self._total_count,
                min(self._total_count, INITIAL_INTERACTIVE_ROW_COUNT),
                BACKGROUND_LOAD_BATCH_SIZE,
            )
        if getattr(self, "_initial_max_width_locked", False):
            self._initial_max_width_locked = False
            # 幅の強制は初回表示のみ。以降はユーザーが手動で広げられるよう解除する。
            # QWIDGETSIZE_MAX(=16777215) を最大幅に戻す（PySide6では定数未エクスポート）。
            self.setMaximumWidth(16777215)
            _CONFIRM_LOGGER.info(
                "kintone_confirm_initial_width_lock_released %s",
                {"width": self.width(), "restored_max_width": self.maximumWidth()},
            )
        if self.isMaximized():
            _CONFIRM_LOGGER.warning(
                "kintone_confirm_show_maximized_detected %s", {"width": self.width()}
            )

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
            sel_bg = "#2F5F8F"
            # 編集ウィジェット（QLineEdit=KakouTypeEdit / QComboBox / QDateEdit）の配色。
            # ダークでも入力文字が背景と同化しないよう明示する（要件2・3）。
            widget_ss = (
                "QLineEdit, QComboBox, QDateEdit {"
                " background-color: #2F343A;"
                " color: #F0F0F0;"
                " border: 1px solid #5A6470;"
                " border-radius: 3px;"
                " padding: 2px 4px;"
                f" selection-background-color: {sel_bg};"
                " selection-color: #FFFFFF;"
                "}"
                "QComboBox QAbstractItemView {"
                " background-color: #2F343A;"
                " color: #F0F0F0;"
                f" selection-background-color: {sel_bg};"
                " selection-color: #FFFFFF;"
                "}"
            )
            return {
                "group_bg_hex": ["#2B3036", "#252A30"],
                "fg_hex": "#F0F0F0",
                "warning_color": "#FFB000",
                "sel_bg": sel_bg,
                "widget_ss": widget_ss,
                # 表全体の配色（要件3・ダーク）。背景は濃いグレー、ヘッダーはさらに濃く、
                # グリッド線は暗めの境界線にする。
                "table_bg": "#1F2328",
                "header_bg": "#343A40",
                "header_fg": "#F0F0F0",
                "gridline": "#555555",
            }
        else:
            sel_bg = "#2D78B8"
            widget_ss = (
                "QLineEdit, QComboBox, QDateEdit {"
                " background-color: #FFFFFF;"
                " color: #000000;"
                " border: 1px solid #AAAAAA;"
                " border-radius: 3px;"
                " padding: 2px 4px;"
                f" selection-background-color: {sel_bg};"
                " selection-color: #FFFFFF;"
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
                # 表全体の配色（要件3・ライト）。背景は白、ヘッダーは薄いグレー。
                "table_bg": "#FFFFFF",
                "header_bg": "#E8E8E8",
                "header_fg": "#1A1A1A",
                "gridline": "#C8C8C8",
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
        table_bg: str = pal["table_bg"]                # type: ignore[assignment]
        header_bg: str = pal["header_bg"]              # type: ignore[assignment]
        header_fg: str = pal["header_fg"]              # type: ignore[assignment]
        gridline: str = pal["gridline"]                # type: ignore[assignment]

        # 表全体（背景・グリッド線・ヘッダー）をテーマに合わせて配色する（要件9）。
        # 選択セルの色は item セルのみ有効（widget セルは widget が全面を覆う）。
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {table_bg}; gridline-color: {gridline}; color: {pal['fg_hex']}; }}"
            f"QTableWidget::item:selected {{ background-color: {sel_bg}; color: #FFFFFF; }}"
            f"QHeaderView::section {{ background-color: {header_bg}; color: {header_fg};"
            f" border: 0px; border-right: 1px solid {gridline}; border-bottom: 1px solid {gridline}; padding: 3px; }}"
            f"QTableCornerButton::section {{ background-color: {header_bg}; }}"
        )

        fg_brush = QBrush(QColor(fg_hex))

        population_start = getattr(self, "_population_start", 0)
        for row_idx in range(population_start, len(self._state.rows)):
            row = self._state.rows[row_idx]
            is_first = row_idx in first_indices
            group_idx = group_indices[row_idx]
            # 同一受注No内は先頭行・後続行とも同じ背景色（グループ単位の交互色のみ）
            bg_hex = group_bg_hex[group_idx % 2]
            bg_brush = QBrush(QColor(bg_hex))
            label_ss = f"color: {fg_hex}; background-color: {bg_hex};"

            # Item セル（文字色・背景色を明示）
            self._set_ro(row_idx, _COL_NO, str(row_idx + 1), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_ORDER_NO, row.get("受注No", ""), bold=is_first, bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_PRODUCT, row.get("商品名称", ""), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_KAKURITSU_CODE, row.get("掛率集計コード", ""), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_KAKURITSU_NAME, row.get("掛率集計名称", ""), bg=bg_brush, fg=fg_brush)
            self._set_ro(row_idx, _COL_TYPE, row.get("硝/加工", ""), bg=bg_brush, fg=fg_brush)

            # 加工種類セル（要件1〜4）。硝/加工 = '2' の行だけテキスト入力可能。
            if row.get("硝/加工", "") == _PROCESSING_TYPE:
                current_code = self._state.kakou_type_by_row[row_idx] or DEFAULT_KAKOU_TYPE
                kakou_type = KakouTypeEdit(current_code)
                # 内部状態を初期値に揃える（既定 1：四方）。
                self._state.set_kakou_type(row_idx, kakou_type.code())
                kakou_type.setStyleSheet(widget_ss)
                self._kakou_type_widgets.append(kakou_type)
                self.table.setCellWidget(row_idx, _COL_KAKOU_TYPE, kakou_type)
                kakou_type.committed.connect(
                    lambda _code, ri=row_idx: self._on_kakou_type_changed(ri)
                )
                kakou_type.move_to_next_requested.connect(
                    lambda ri=row_idx: self._move_from_kakou_type_to_next_cell(ri)
                )
            else:
                # 硝/加工 ≠ '2' は編集不可・空欄表示（加工mm計算対象外。要件4）。
                self._kakou_type_widgets.append(None)
                self._set_ro(row_idx, _COL_KAKOU_TYPE, "", bg=bg_brush, fg=fg_brush)

            if is_first:
                # 仕上日ウィジェット（受注No先頭行のみ）。
                # 「なし」（空欄）は最小日付＋特別表示文字で表現する（要件3・5）。
                date_edit = PopupDateEdit()
                date_edit.setCalendarPopup(False)
                date_edit.setDisplayFormat("yyyy-MM-dd")
                date_edit.setSpecialValueText(SHUKKA_NONE_LABEL)
                date_edit.setMinimumDate(_SHIAGE_NONE_DATE)
                shiage_text = self._state.shiage_by_row[row_idx]
                if shiage_text:
                    date_edit.setDate(_date_from_text(shiage_text))
                else:
                    date_edit.setDate(_SHIAGE_NONE_DATE)
                date_edit.setStyleSheet(widget_ss)
                self._shiage_widgets.append(date_edit)
                self.table.setCellWidget(row_idx, _COL_SHIAGE, date_edit)
                date_edit.dateChanged.connect(
                    lambda _d, ri=row_idx: self._on_shiage_changed(ri)
                )

                # 出荷区分ウィジェット（受注No先頭行のみ）。先頭に「なし」（空欄）を追加。
                shukka = QComboBox()
                shukka.addItem(SHUKKA_NONE_LABEL)
                shukka.addItems(self._shukka_options)
                current = self._state.shukka_by_row[row_idx]
                if current and current not in self._shukka_options:
                    shukka.addItem(current)
                if current:
                    shukka.setCurrentText(current)
                else:
                    shukka.setCurrentText(SHUKKA_NONE_LABEL)
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
                current_key = self._state.customer_key_by_row[row_idx]
                idx = customer.findData(current_key)
                if idx >= 0:
                    customer.setCurrentIndex(idx)
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
            # 列幅を狭めたため、全文はtooltipで確認できるようにする。
            if warning_text:
                warning_label.setToolTip(warning_text)
            self.table.setCellWidget(row_idx, _COL_WARNING, warning_label)

            self._refresh_kakou_label(row_idx)
            if self._progressive_loading:
                for column in (_COL_KAKOU_TYPE, _COL_SHIAGE, _COL_SHUKKA, _COL_CUSTOMER):
                    editor = self.table.cellWidget(row_idx, column)
                    if editor is not None:
                        editor.setEnabled(False)

    def append_prepared_batch(
        self,
        rows: list[dict[str, str]],
        existing_by_row: list[dict[str, str]],
        *,
        total_count: int,
    ) -> None:
        """workerが準備したplain dataをGUIスレッドで1バッチだけ反映する。"""
        if not self._progressive_loading or self._all_rows_ready or self._load_failed:
            return
        if not rows:
            return
        batch_started = time.perf_counter()
        start = len(self._state.rows)
        batch_state = PreviewState(
            rows=[dict(row) for row in rows],
            kintone_existing_by_row=[dict(row) for row in existing_by_row],
        )
        self._state.rows.extend(batch_state.rows)
        self._state.kintone_existing_by_row.extend(batch_state.kintone_existing_by_row)
        self._state.shiage_by_row.extend(batch_state.shiage_by_row)
        self._state.shukka_by_row.extend(batch_state.shukka_by_row)
        self._state.customer_key_by_row.extend(batch_state.customer_key_by_row)
        self._state.kakou_type_by_row.extend(batch_state.kakou_type_by_row)
        self._total_count = total_count

        # 受注Noがバッチ境界をまたぐ場合も先頭行の編集状態を引き継ぐ。
        first_by_order: dict[str, int] = {}
        for index, row in enumerate(self._state.rows):
            order_no = row.get("受注No", "")
            if order_no not in first_by_order:
                first_by_order[order_no] = index
            elif index >= start:
                first = first_by_order[order_no]
                self._state.shiage_by_row[index] = self._state.shiage_by_row[first]
                self._state.shukka_by_row[index] = self._state.shukka_by_row[first]
                self._state.customer_key_by_row[index] = self._state.customer_key_by_row[first]

        self.table.setUpdatesEnabled(False)
        previous_block = self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._state.rows))
            self._population_start = start
            self._populate_table()
            self._apply_initial_customer_matches(start)
            self._apply_filter(self._filter_edit.text())
        finally:
            self._population_start = 0
            self.table.blockSignals(previous_block)
            self.table.setUpdatesEnabled(True)

        batch_ms = int((time.perf_counter() - batch_started) * 1000)
        self._gui_row_generation_ms += batch_ms
        self._cell_widget_count = (
            len(self._kakou_labels)
            + len(self._state.rows)
            + sum(1 for widget in self._shiage_widgets if widget is not None)
            + sum(1 for widget in self._shukka_widgets if widget is not None)
            + sum(1 for widget in self._customer_widgets if widget is not None)
            + sum(1 for widget in self._kakou_type_widgets if widget is not None)
        )
        displayed = len(self._state.rows)
        self.loading_status_label.setText(
            f"Kintone登録準備中\n{displayed} / {total_count}件を表示しました\n"
            "残りのデータを確認しています…"
        )
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=table_rows_generated elapsed_ms=%d "
            "batch_count=%d displayed_count=%d total_count=%d",
            batch_ms,
            len(rows),
            displayed,
            total_count,
        )
        interactive_threshold = min(total_count, INITIAL_INTERACTIVE_ROW_COUNT)
        if not self._first_batch_logged and displayed >= interactive_threshold:
            self._first_batch_logged = True
            elapsed_ms = int((time.perf_counter() - self._request_started) * 1000)
            _CONFIRM_LOGGER.info(
                "event=perf_kintone_registration phase=first_10_rows_ready elapsed_ms=%d "
                "total_count=%d first_batch_count=%d batch_size=%d gui_row_generation_ms=%d",
                elapsed_ms,
                total_count,
                interactive_threshold,
                BACKGROUND_LOAD_BATCH_SIZE,
                self._gui_row_generation_ms,
            )
            _CONFIRM_LOGGER.info(
                "event=perf_kintone_registration phase=interactive elapsed_ms=%d "
                "displayed_count=%d total_count=%d loading=true",
                elapsed_ms,
                displayed,
                total_count,
            )

    def finish_progressive_loading(self, metrics: dict[str, object] | None = None) -> None:
        if not self._progressive_loading or self._load_failed:
            return
        self._all_rows_ready = True
        displayed = len(self._state.rows)
        self.loading_status_label.setText(f"{displayed} / {self._total_count}件の確認が完了しました")
        warnings = [
            warning
            for row in self._state.rows
            if (warning := _row_unregistered_warning(row, self._master))
        ]
        if warnings:
            self.warning_label.setText(
                "未登録の掛率集計コードがあります:\n"
                + "\n".join(dict.fromkeys(warnings))
            )
            self.warning_label.setVisible(True)
        self.register_button.setEnabled(bool(self._state.rows))
        self.register_button.setToolTip("")
        self.csv_create_button.setEnabled(bool(self._state.rows))
        self.csv_create_button.setToolTip(
            "登録ボタン押下時と同じ登録用データをCSVへ出力します（kintone登録はしません）。"
        )
        self.csv_column_settings_button.setEnabled(True)
        for widgets in (
            self._kakou_type_widgets,
            self._shiage_widgets,
            self._shukka_widgets,
            self._customer_widgets,
        ):
            for widget in widgets:
                if widget is not None:
                    widget.setEnabled(True)
        if self.print_button is not None:
            self.print_button.setEnabled(True)
            self.print_button.setToolTip("")
        elapsed_ms = int((time.perf_counter() - self._request_started) * 1000)
        worker_ms = int((metrics or {}).get("worker_ms", 0))
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=all_rows_ready elapsed_ms=%d "
            "total_count=%d first_batch_count=%d batch_size=%d "
            "gui_row_generation_ms=%d worker_processing_ms=%d",
            elapsed_ms,
            self._total_count,
            min(self._total_count, INITIAL_INTERACTIVE_ROW_COUNT),
            BACKGROUND_LOAD_BATCH_SIZE,
            self._gui_row_generation_ms,
            worker_ms,
        )

    def apply_existing_check_result(
        self,
        merged_rows: list[dict[str, str]],
        existing_by_row: list[dict[str, str]],
    ) -> None:
        """Kintone確認完了後の値を、ユーザー編集開始前に表示済み行へ反映する。"""
        if len(merged_rows) != len(self._state.rows):
            raise ValueError("Kintone既存確認結果の行数が一致しません。")
        # 後着のKintone確認結果は行データを更新するが、既に画面で確定した得意先
        # 選択の所有権は奪わない。旧実装はここで PreviewState を丸ごと交換し、
        # setCurrentIndex の currentIndexChanged まで発火させていた。
        preserved_customer_by_order: dict[str, str] = {}
        for row_idx in self._state.first_indices_by_order():
            order_no = str(self._state.rows[row_idx].get("受注No", "") or "")
            preserved_customer_by_order[order_no] = self._state.customer_key_by_row[row_idx]

        refreshed = PreviewState(
            rows=[dict(row) for row in merged_rows],
            kintone_existing_by_row=[dict(row) for row in existing_by_row],
        )
        self._state = refreshed
        for row_idx in self._state.first_indices_by_order():
            order_no = str(self._state.rows[row_idx].get("受注No", "") or "")
            if order_no in preserved_customer_by_order:
                self._state.set_customer_key_for_order(
                    row_idx, preserved_customer_by_order[order_no]
                )
        self.table.setUpdatesEnabled(False)
        try:
            for row_idx in range(len(self._state.rows)):
                date_widget = self._shiage_widgets[row_idx]
                if date_widget is not None:
                    value = self._state.shiage_by_row[row_idx]
                    date_widget.setDate(_date_from_text(value) if value else _SHIAGE_NONE_DATE)
                shukka_widget = self._shukka_widgets[row_idx]
                if shukka_widget is not None:
                    value = self._state.shukka_by_row[row_idx] or SHUKKA_NONE_LABEL
                    if value not in (SHUKKA_NONE_LABEL, *self._shukka_options):
                        shukka_widget.addItem(value)
                    shukka_widget.setCurrentText(value)
                customer_widget = self._customer_widgets[row_idx]
                if customer_widget is not None:
                    index = customer_widget.findData(self._state.customer_key_by_row[row_idx])
                    if index >= 0:
                        previous = customer_widget.blockSignals(True)
                        try:
                            customer_widget.setCurrentIndex(index)
                        finally:
                            customer_widget.blockSignals(previous)
                kakou_widget = self._kakou_type_widgets[row_idx]
                if kakou_widget is not None:
                    kakou_widget._code = self._state.kakou_type_by_row[row_idx]
                    kakou_widget._show_label()
                self._refresh_kakou_label(row_idx)
        finally:
            self.table.setUpdatesEnabled(True)

    def fail_progressive_loading(self, message: str) -> None:
        self._load_failed = True
        displayed = len(self._state.rows)
        if displayed:
            text = f"{displayed}件を表示しましたが、残りの確認に失敗しました。\n{message}"
        else:
            text = f"データの確認に失敗しました。\n{message}"
        self.loading_status_label.setText(text)
        self.register_button.setEnabled(False)
        self.register_button.setToolTip(_REGISTRATION_LOADING_TOOLTIP)
        self.csv_create_button.setEnabled(False)
        self.csv_column_settings_button.setEnabled(False)
        if self.print_button is not None:
            self.print_button.setEnabled(False)
        self.retry_button.setVisible(True)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._progressive_loading and not self._all_rows_ready:
            self.loading_cancel_requested.emit()
        super().closeEvent(event)

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

    # ── 仕上日 / 出荷区分 の「なし」（空欄）正規化 ──────────────

    @staticmethod
    def _shiage_text_of(widget: "PopupDateEdit | None") -> str | None:
        """仕上日ウィジェットの値を文字列で返す。「なし」（最小日付）は空欄。"""
        if widget is None:
            return None
        if widget.date() == widget.minimumDate():
            return ""
        return widget.date().toString("yyyy-MM-dd")

    @staticmethod
    def _shukka_text_of(widget: "QComboBox | None") -> str | None:
        """出荷区分ウィジェットの値を文字列で返す。「なし」は空欄。"""
        if widget is None:
            return None
        text = widget.currentText()
        return "" if text == SHUKKA_NONE_LABEL else text

    # ── 仕上日 変更ハンドラ ───────────────────────────────

    def _on_shiage_changed(self, row_idx: int) -> None:
        # PreviewState を更新（同一受注No 全行に反映）
        # 先頭行のみウィジェットを持つため他ウィジェットへの同期は不要
        text = self._shiage_text_of(self._shiage_widgets[row_idx]) or ""
        self._state.set_shiage(row_idx, text)

    # ── 出荷区分 変更ハンドラ ─────────────────────────────

    def _on_shukka_changed(self, row_idx: int, text: str) -> None:
        # PreviewState を更新（同一受注No 全行に反映）。「なし」は空欄扱い。
        self._state.set_shukka(row_idx, "" if text == SHUKKA_NONE_LABEL else text)

    # ── 得意先選択 変更ハンドラ ───────────────────────────

    def _on_customer_changed(self, row_idx: int) -> None:
        widget = self._customer_widgets[row_idx]
        new_key = (widget.currentData() if widget is not None else None) or DEFAULT_CUSTOMER_KEY
        order_no = str(self._state.rows[row_idx].get("受注No", "") or "")
        self._customer_selection_source_by_order[order_no] = "user_selected"
        # 同一受注No の全行（非表示行含む）に反映
        self._state.set_customer_key_for_order(row_idx, new_key)
        self._log_customer_auto_select(
            phase="selection_preserved",
            row_idx=row_idx,
            target_customer_key=new_key,
            current_customer_key=new_key,
            selection_source="user_selected",
            reason="user_selected",
        )
        # 同一受注No の全行の判定加工名を更新
        for i in self._state.indices_for_order(row_idx):
            self._refresh_kakou_label(i)

    def _apply_initial_customer_matches(self, start_row: int = 0) -> None:
        first_indices = self._state.first_indices_by_order()
        for row_idx in sorted(first_indices):
            if row_idx < start_row:
                continue
            row = self._state.rows[row_idx]
            order_no = str(row.get("受注No", "") or "")
            selection_source = self._customer_selection_source_by_order.get(order_no, "")
            if selection_source == "user_selected":
                self._log_customer_auto_select(
                    phase="selection_preserved",
                    row_idx=row_idx,
                    target_customer_key=self._state.customer_key_by_row[row_idx],
                    current_customer_key=self._state.customer_key_by_row[row_idx],
                    selection_source=selection_source,
                    reason="user_selected",
                )
                continue
            raw_name = str(row.get("得意先名称", "") or "")
            normalized_name = normalize_customer_name(raw_name)
            key = customer_key_from_name(normalized_name, self._customer_match_patterns)
            current_key = self._state.customer_key_by_row[row_idx]
            self._log_customer_auto_select(
                phase="source_resolved",
                row_idx=row_idx,
                target_customer_key=key,
                current_customer_key=current_key,
                selection_source=selection_source or "unresolved",
                customer_name_raw=raw_name,
                customer_name_normalized=normalized_name,
            )
            self._state.set_customer_key_for_order(row_idx, key)
            self._customer_selection_source_by_order[order_no] = (
                "auto_match" if key != DEFAULT_CUSTOMER_KEY else "auto_cleared"
            )
            widget = self._customer_widgets[row_idx]
            if widget is not None:
                idx = widget.findData(key)
                if idx >= 0:
                    previous = widget.blockSignals(True)
                    try:
                        widget.setCurrentIndex(idx)
                    finally:
                        widget.blockSignals(previous)
            self._log_customer_auto_select(
                phase="selection_applied" if key != DEFAULT_CUSTOMER_KEY else "selection_cleared",
                row_idx=row_idx,
                target_customer_key=key,
                current_customer_key=key,
                selection_source=self._customer_selection_source_by_order[order_no],
                reason="elevator_match" if key != DEFAULT_CUSTOMER_KEY else "no_elevator",
                customer_name_raw=raw_name,
                customer_name_normalized=normalized_name,
            )
            for i in self._state.indices_for_order(row_idx):
                self._refresh_kakou_label(i)

    def _log_customer_auto_select(
        self,
        *,
        phase: str,
        row_idx: int,
        target_customer_key: str,
        current_customer_key: str,
        selection_source: str,
        reason: str = "",
        customer_name_raw: str | None = None,
        customer_name_normalized: str | None = None,
    ) -> None:
        row = self._state.rows[row_idx]
        raw = (
            str(row.get("得意先名称", "") or "")
            if customer_name_raw is None
            else customer_name_raw
        )
        normalized = normalize_customer_name(raw) if customer_name_normalized is None else customer_name_normalized
        _CONFIRM_LOGGER.info(
            "event=kintone_customer_auto_select phase=%s order_no=%s "
            "customer_name_raw=%r customer_name_normalized=%r contains_elevator=%s "
            "target_customer_key=%s current_customer_key=%s selection_source=%s "
            "generation=%d reason=%s",
            phase,
            str(row.get("受注No", "") or ""),
            raw,
            normalized,
            str("エレベータ" in normalized).lower(),
            target_customer_key,
            current_customer_key,
            selection_source,
            self._generation,
            reason,
        )

    # ── 加工種類 変更ハンドラ ─────────────────────────────

    def _on_kakou_type_changed(self, row_idx: int) -> None:
        """加工種類入力の確定値を PreviewState に反映する（行ごと独立。要件4・9）。"""
        widget = self._kakou_type_widgets[row_idx]
        if widget is None:
            return
        self._state.set_kakou_type(row_idx, widget.code() or DEFAULT_KAKOU_TYPE)

    def _move_from_kakou_type_to_next_cell(self, row: int) -> None:
        """加工種類セルから同じ行の次セル（仕上日列）へ移動する（要件1〜3）。"""
        self.table.setCurrentCell(row, _COL_SHIAGE)
        widget = self.table.cellWidget(row, _COL_SHIAGE)
        if widget is not None:
            widget.setFocus()

    def _refresh_kakou_label(self, row_idx: int) -> None:
        name = self._state.compute_kakou_name(row_idx, self._master)
        label = self._kakou_labels[row_idx]
        label.setText(name)
        # 列幅を狭めたため、長い判定加工名はtooltipで全文を確認できるようにする。
        label.setToolTip(name)

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
            shiage_text = self._shiage_text_of(self._shiage_widgets[i])
            if shiage_text is not None:
                self._state.set_shiage(i, shiage_text)
            shukka_text = self._shukka_text_of(self._shukka_widgets[i])
            if shukka_text is not None:
                self._state.set_shukka(i, shukka_text)
            if self._customer_widgets[i] is not None:
                self._state.set_customer_key_for_order(
                    i, self._customer_widgets[i].currentData() or DEFAULT_CUSTOMER_KEY
                )
            if self._kakou_type_widgets[i] is not None:
                self._state.set_kakou_type(
                    i, self._kakou_type_widgets[i].code() or DEFAULT_KAKOU_TYPE
                )
        return self._state.build_registration_rows(self._master)

    def build_registration_records_from_preview(self) -> list[dict[str, str]]:
        """登録ボタン押下時にkintoneへ送信する登録用データを生成して返す（要件9）。

        登録処理（registration_rows）とCSV出力で生成処理が分岐しないよう共通化した入口。
        """
        return self.registration_rows()

    # ── CSV出力先 ───────────────────────────────

    def _settings(self) -> QSettings:
        return QSettings(SETTINGS_ORG, SETTINGS_APP)

    def _default_csv_output_dir(self) -> str:
        """CSV出力先の初期値。Documents を基本とし、取得できなければ空文字。"""
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return docs or ""

    def _load_csv_output_dir(self) -> None:
        """保存済みのCSV出力先を復元する。未保存なら Documents を初期表示する（要件3）。"""
        saved = str(self._settings().value(SETTINGS_CSV_OUTPUT_DIR, "") or "").strip()
        self.csv_output_dir_edit.setText(saved or self._default_csv_output_dir())

    def _save_csv_output_dir(self, output_dir: str) -> None:
        settings = self._settings()
        settings.setValue(SETTINGS_CSV_OUTPUT_DIR, output_dir)
        settings.sync()

    def _browse_csv_output_dir(self) -> None:
        current = self.csv_output_dir_edit.text().strip() or self._default_csv_output_dir()
        selected = QFileDialog.getExistingDirectory(self, "CSV出力先を選択", current)
        if not selected:
            return
        self.csv_output_dir_edit.setText(selected)
        self._save_csv_output_dir(selected)

    def _open_csv_column_settings(self) -> None:
        dialog = CsvColumnSettingsDialog(load_csv_column_settings(self._settings()), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        columns = dialog.saved_column_settings()
        if columns is not None:
            save_csv_column_settings(self._settings(), columns)

    def _on_create_csv(self) -> None:
        """登録ボタン押下時と同じ登録用データを確認用CSVへ出力する（要件4）。

        kintone API送信・登録状態更新・Teams通知・updated_at更新などは一切行わない。
        """
        # 登録対象データの有無を確認（要件12）。
        records = self.build_registration_records_from_preview()
        if not records:
            QMessageBox.warning(self, "CSV作成", "登録対象データがありません。")
            return

        # 出力先の検証（要件11）。
        raw = self.csv_output_dir_edit.text().strip()
        if not raw:
            QMessageBox.warning(self, "CSV作成", "CSV出力先を指定してください。")
            return
        output_dir = Path(raw).expanduser()
        if not output_dir.is_dir():
            QMessageBox.warning(self, "CSV作成", f"CSV出力先が存在しません。\n{output_dir}")
            return
        if not os.access(output_dir, os.W_OK):
            QMessageBox.warning(self, "CSV作成", f"CSV出力先に書き込みできません。\n{output_dir}")
            return

        # 出力先を保存し、次回も同じパスを表示する（要件3）。
        self._save_csv_output_dir(str(output_dir))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = unique_timestamp_csv_path(output_dir, timestamp)
        try:
            column_settings = load_csv_column_settings(self._settings())
            export_registration_records_to_csv(
                records,
                output_path,
                columns=enabled_csv_columns(column_settings),
            )
        except OSError as exc:
            QMessageBox.critical(self, "CSV作成", f"CSVの作成に失敗しました。\n{exc}")
            return

        result = QMessageBox.question(
            self,
            "CSV作成",
            f"CSVを作成しました。\n{output_path}\n\n保存先フォルダを開きますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir)))

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
                shiage_text = self._shiage_text_of(self._shiage_widgets[i])
                shukka_text = self._shukka_text_of(self._shukka_widgets[i])
                order_values[order_no] = {
                    "仕上日": (shiage_text if shiage_text is not None
                               else self._state.shiage_by_row[i]),
                    "出荷区分": (shukka_text if shukka_text is not None
                                else self._state.shukka_by_row[i]),
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
        clamp_window_to_available_geometry(
            self,
            desired_width=980,
            desired_height=620,
            min_width=720,
            min_height=480,
        )
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
        clamp_window_to_available_geometry(
            self,
            desired_width=520,
            desired_height=360,
            min_width=420,
            min_height=320,
        )
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

        self.teams_enabled = QCheckBox("Teams通知を有効にする")
        self.teams_enabled.setChecked(_settings_bool(settings, SETTINGS_TEAMS_ENABLED, True))
        test_default = default_teams_webhook_url_test()
        prod_default = default_teams_webhook_url_prod()
        self.teams_webhook_url_test = QLineEdit()
        self.teams_webhook_url_test.setEchoMode(QLineEdit.EchoMode.Password)
        self.teams_webhook_url_test.setText(str(settings.value(SETTINGS_TEAMS_WEBHOOK_URL_TEST, test_default) or ""))
        self.teams_webhook_url_prod = QLineEdit()
        self.teams_webhook_url_prod.setEchoMode(QLineEdit.EchoMode.Password)
        self.teams_webhook_url_prod.setText(str(settings.value(SETTINGS_TEAMS_WEBHOOK_URL_PROD, prod_default) or ""))
        self.teams_webhook_url_test_label = QLabel("テスト用Webhook URL")
        self.teams_webhook_url_prod_label = QLabel("本番用Webhook URL")
        debug_visible = _settings_bool(settings, SETTINGS_DEBUG_VISIBLE, False)
        self._debug_visible = debug_visible
        self.kintone_target.setEnabled(debug_visible)
        self.teams_enabled.setEnabled(debug_visible)
        for widget in (
            self.teams_webhook_url_test_label,
            self.teams_webhook_url_test,
            self.teams_webhook_url_prod_label,
            self.teams_webhook_url_prod,
        ):
            widget.setVisible(debug_visible)

        form = QFormLayout()
        form.addRow("Kintone接続先", self.kintone_target)
        form.addRow("", self.teams_enabled)
        form.addRow(self.teams_webhook_url_test_label, self.teams_webhook_url_test)
        form.addRow(self.teams_webhook_url_prod_label, self.teams_webhook_url_prod)

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
        # 制限モード中は接続先・Teams関連設定を表示専用とし、既存値を保持する。
        if self._debug_visible:
            self.settings.setValue(SETTINGS_KINTONE_TARGET, self.kintone_target.currentData())
            self.settings.setValue(SETTINGS_TEAMS_ENABLED, self.teams_enabled.isChecked())
            self.settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_TEST, self.teams_webhook_url_test.text().strip())
            self.settings.setValue(SETTINGS_TEAMS_WEBHOOK_URL_PROD, self.teams_webhook_url_prod.text().strip())
        self.settings.sync()
        super().accept()


class MainWindow(QMainWindow):
    # 受注No入力欄が変化したことを外部（伝票作成・印刷画面）へ通知する（要件3・5）。
    order_numbers_changed = Signal()
    kintone_registration_completed = Signal(list)

    def __init__(
        self,
        initial_olap_id: str | None = None,
        initial_olap_password: str | None = None,
    ) -> None:
        super().__init__()
        self._initial_olap_id = initial_olap_id
        self._initial_olap_password = initial_olap_password
        self._olap_login_id = (initial_olap_id or "").strip()
        self._olap_password = initial_olap_password or ""
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.setWindowTitle(f"TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        self.resize(900, 720)
        self.config: AppConfig | None = None
        self.fallback_base_dir = default_base_dir()
        self.worker: WorkerThread | None = None
        self.prepare_worker: RegistrationPrepareWorkerThread | None = None
        self.existing_merge_worker: RegistrationExistingMergeWorkerThread | None = None
        self.debug_worker: TksDebugWorkerThread | None = None
        self.register_worker: KintoneRegisterWorkerThread | None = None
        self.update_check_worker: UpdateCheckWorkerThread | None = None
        self.update_check_manual = False
        self._closing = False
        self._registration_generation = 0
        self._preview_dialog: RegistrationPreviewDialog | None = None
        self._pending_registration: PendingRegistration | None = None
        self._preview_prepare_metrics: dict[str, object] | None = None
        self._kintone_checks_received = False
        # 起動直後の自動更新確認は廃止。更新確認は機能選択画面（LauncherWindow）表示時に行う。
        # ここでは設定画面からの手動更新確認のみを残す。

        self.tks_client_mode_label = QLabel("TKS_CLIENT_MODE")
        self.tks_client_mode = QLineEdit()
        self.tks_client_mode.setReadOnly(True)
        self.kintone_target_label = QLabel("Kintone接続先")
        self.kintone_target_display = QLineEdit()
        self.kintone_target_display.setReadOnly(True)
        self.programdata_path_label = QLabel("ProgramDataフォルダ")
        self.programdata_path = QLineEdit()
        self.programdata_path.setReadOnly(True)
        self.denpyo_numbers = QPlainTextEdit()
        self.denpyo_numbers.setPlaceholderText("1386680\n1386681")
        self.shiage_date = PopupDateEdit()
        self.shiage_date.setDisplayFormat("yyyy-MM-dd")
        self.shiage_date.setDate(QDate.currentDate())
        self.shiage_date.setMinimumHeight(36)
        self.shiage_date.setStyleSheet("QDateEdit { padding: 4px 8px; }")
        # 仕上日「なし」チェック。ONで仕上日を空欄扱いにする（要件3）。
        self.shiage_none = QCheckBox("なし")
        self.shiage_none.setToolTip("チェックすると仕上日を「なし」（空欄）にします。")
        self.shiage_none.toggled.connect(self.shiage_date.setDisabled)
        self.shukka_kbn = QComboBox()
        # 受注Noごとの override（伝票作成・印刷画面から渡された仕上日／AM・PM）。
        # 画面連携中のみ保持し、永続化はしない（要件4）。
        self._order_overrides: dict[str, dict[str, object]] = {}
        self.run_button = QPushButton("実行")
        self.settings_button = QPushButton("⚙")
        self.settings_button.setToolTip("設定")
        self.settings_button.setAccessibleName("設定")
        self.settings_button.setMinimumSize(40, 40)
        self.settings_button.setStyleSheet("QPushButton { font-size: 20px; padding: 4px; }")
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
        # 処理中表示用の不定進捗バー（OLAP取得・CSV出力・Kintone登録）。
        # UI全体は無効化せず、処理中であることだけを示す。
        self._busy_counter = 0
        self._busy_progress = QProgressBar()
        self._busy_progress.setObjectName("busyProgress")
        self._busy_progress.setTextVisible(False)
        self._busy_progress.setFixedWidth(160)
        self._busy_progress.setRange(0, 0)
        self._busy_progress.setVisible(False)
        self._busy_progress.setToolTip("処理中です。")

        self._build_layout()
        apply_theme(str(self.settings.value(SETTINGS_THEME, THEME_SYSTEM) or THEME_SYSTEM))
        self.run_button.clicked.connect(self.start_run)
        # 受注No欄の変更を外部へ通知する（伝票画面のボタン状態同期: 要件3）。
        self.denpyo_numbers.textChanged.connect(self.order_numbers_changed)
        # 受注Noが削除されたら対応する override も破棄する（要件4）。
        self.denpyo_numbers.textChanged.connect(self._prune_order_overrides)
        self.settings_button.clicked.connect(self.open_settings)
        self.tks_login_test_button.clicked.connect(self.start_tks_login_test)
        self.olap_test_button.clicked.connect(self.start_olap_test)
        self.cleanup_button.clicked.connect(self.run_manual_cleanup)
        self.open_config_button.clicked.connect(lambda: self.open_folder("config"))
        self.open_log_button.clicked.connect(lambda: self.open_folder("log"))
        self.open_work_button.clicked.connect(lambda: self.open_folder("work"))
        self.kakou_master_button.clicked.connect(self.open_kakou_master)
        self.customer_labels_button.clicked.connect(self.open_customer_label_settings)
        self._load_config()
        self._apply_debug_visibility()

    def _build_layout(self) -> None:
        form = QFormLayout()
        form.addRow(self.tks_client_mode_label, self.tks_client_mode)
        form.addRow(self.kintone_target_label, self.kintone_target_display)
        form.addRow(self.programdata_path_label, self.programdata_path)
        form.addRow("受注No", self.denpyo_numbers)
        shiage_row = QHBoxLayout()
        shiage_row.addWidget(self.shiage_date, 1)
        shiage_row.addWidget(self.shiage_none)
        shiage_holder = QWidget()
        shiage_holder.setLayout(shiage_row)
        form.addRow("仕上日", shiage_holder)
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
        result_row = QHBoxLayout()
        result_row.addWidget(self.result_label, 1)
        result_row.addWidget(self._busy_progress)
        root.addLayout(result_row)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    def closeEvent(self, event) -> None:
        """初期化途中でも安全に閉じられるように、遅延処理とワーカーを停止する。"""
        self._closing = True
        for worker in (
            self.worker,
            self.prepare_worker,
            self.existing_merge_worker,
            self.debug_worker,
            self.register_worker,
            self.update_check_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                _detach_running_thread(worker)
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

        self.tks_client_mode.setText(self.config.tks_client_mode)
        self._update_kintone_target_display()
        self.programdata_path.setText(str(self.config.paths.base_dir))
        # 「なし」（空欄扱い）を先頭に追加し、AM・PM等の設定値を続ける（要件3）。
        self.shukka_kbn.clear()
        self.shukka_kbn.addItem(SHUKKA_NONE_LABEL)
        self.shukka_kbn.addItems(self.config.shukka_kbn_options)
        # 既定選択は従来どおり設定値の先頭（AM等）にし、挙動を変えない。
        if self.config.shukka_kbn_options:
            self.shukka_kbn.setCurrentText(self.config.shukka_kbn_options[0])
        self.append_log(f"設定ファイル: {self.config.paths.config_env}")
        op_fields = ",".join(self.config.tks_voucher_olap_enabled_op_fields)
        self.append_log(f"TKS_VOUCHER_OLAP_ENABLED_OP_FIELDS={op_fields}")
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
        dialog = CustomerLabelDialog(self.config.customer_labels, self.config.customer_match_patterns, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_labels = dialog.result_labels()
        new_patterns = dialog.result_match_patterns()
        try:
            update_customer_labels_in_config(self.config.paths.config_env, new_labels, new_patterns)
            self.config = load_app_config()
            QMessageBox.information(self, "保存完了", "得意先ヘッダー設定を保存しました。")
        except Exception as exc:
            QMessageBox.critical(self, "保存エラー", f"設定の保存に失敗しました:\n{exc}")

    def start_run(self) -> None:
        button_started = time.perf_counter()
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=button_clicked elapsed_ms=0 "
            "total_count=0 first_batch_count=0 batch_size=%d",
            BACKGROUND_LOAD_BATCH_SIZE,
        )
        if self.config is None:
            return
        # 同じ受注Noが複数入力されている場合は実行前に警告して中止する（要件5）。
        duplicates = find_duplicate_order_numbers(
            parse_order_numbers(self.denpyo_numbers.toPlainText())
        )
        if duplicates:
            QMessageBox.warning(
                self,
                "受注Noの重複",
                "以下の受注Noはすでに一覧に存在します。\n" + "\n".join(duplicates),
            )
            return
        try:
            run_input = self._collect_input(require_denpyo=True)
        except ValueError as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        _CONFIRM_LOGGER.info(
            "event=perf_kintone_registration phase=target_rows_collected elapsed_ms=%d "
            "target_order_count=%d",
            int((time.perf_counter() - button_started) * 1000),
            len(run_input.denpyo_numbers),
        )

        self.result_label.setText("")
        self.log_view.clear()
        self._set_buttons_enabled(False)
        self._registration_generation += 1
        generation = self._registration_generation
        self._kintone_register_perf_start = button_started

        # 画面枠は通信・全件マッピング・セル生成より先に表示する。
        dialog = RegistrationPreviewDialog(
            [],
            self.config.shukka_kbn_options,
            [],
            self.config.customer_labels,
            self.config.customer_match_patterns,
            self,
            preview_color_theme=self.config.preview_color_theme,
            debug_visible=_settings_bool(self.settings, SETTINGS_DEBUG_VISIBLE, False),
            progressive_loading=True,
            request_started=self._kintone_register_perf_start,
            generation=generation,
        )
        dialog.loading_status_label.setText(
            "Kintone登録準備中\n"
            f"対象受注No: {len(run_input.denpyo_numbers)}件\n"
            "データを確認しています…"
        )
        self._preview_dialog = dialog
        self._pending_registration = None
        self._preview_prepare_metrics = None
        self._kintone_checks_received = False
        dialog.loading_cancel_requested.connect(self._cancel_registration_loading)
        dialog.retry_requested.connect(self._retry_registration_loading)
        dialog.rejected.connect(self._on_preview_rejected)
        dialog.accepted.connect(self._on_preview_accepted)
        dialog.open()

        self._set_busy("OLAP取得中...", context="run_olap")
        # event loopへ戻して画面をpaintした後にworkerを起動する。
        QTimer.singleShot(0, lambda: self._start_registration_source_worker(generation, run_input))

    def _start_registration_source_worker(self, generation: int, run_input: RunInput) -> None:
        if generation != self._registration_generation or self._preview_dialog is None:
            return
        self.worker = WorkerThread(self._effective_config(), run_input)
        worker = self.worker
        worker.log_line.connect(self.append_log)
        worker.pending_registration.connect(
            lambda pending, g=generation: self.on_pending_registration(pending, g)
        )
        worker.kintone_checks_ready.connect(
            lambda records, error, g=generation: self._on_kintone_checks_ready(
                g, records, error
            )
        )
        worker.failed.connect(
            lambda message, g=generation: self._on_registration_load_failed(g, message)
        )
        worker.finished.connect(lambda: self._clear_busy(context="run_olap"))
        worker.start()

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
        self._set_busy("OLAP取得中..." if mode == "olap" else "TKS接続確認中...", context="debug_worker")
        self.debug_worker = TksDebugWorkerThread(self._effective_config(), run_input, mode)
        self.debug_worker.log_line.connect(self.append_log)
        self.debug_worker.succeeded.connect(self.on_debug_succeeded)
        self.debug_worker.failed.connect(self.on_failed)
        self.debug_worker.finished.connect(lambda: self._set_buttons_enabled(True))
        self.debug_worker.finished.connect(lambda: self._clear_busy(context="debug_worker"))
        self.debug_worker.start()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self.settings, self, self.start_manual_update_check, self._cleanup_old_files)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._update_kintone_target_display()
            self._apply_debug_visibility()
            self._cleanup_old_files()

    def start_manual_update_check(self) -> None:
        if not updates_enabled():
            QMessageBox.information(self._message_parent(), "更新確認", "このビルドでは更新機能は無効です。")
            return
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        if not updates_enabled():
            return
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

    def on_update_check_succeeded(self, info: object | None) -> None:
        if info is None:
            if self.update_check_manual:
                self.append_log("更新確認結果: 最新です。")
                QMessageBox.information(self._message_parent(), "更新確認", "現在のバージョンは最新です。")
            return

        release_notes = f"\n\nリリースノート:\n{info.release_notes}" if info.release_notes else ""
        self.append_log(f"新しいバージョンを検出: {info.version_name} (コード {info.version_code})")
        answer = QMessageBox.question(
            self._message_parent(),
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
        self.start_update_download(info)

    def on_update_check_failed(self, message: str) -> None:
        if self.update_check_manual:
            self.append_log(f"更新確認失敗: {message}")
            QMessageBox.warning(self._message_parent(), "更新確認失敗", message)

    def start_update_download(self, info: object) -> None:
        from app.update_progress import start_update

        start_update(
            self._message_parent(),
            info,
            Path(sys.executable).resolve(),
        )

    def _message_parent(self) -> QWidget:
        active_window = QApplication.activeWindow()
        return active_window if isinstance(active_window, QWidget) else self

    def on_pending_registration(
        self, pending: PendingRegistration, generation: int | None = None
    ) -> None:
        generation = self._registration_generation if generation is None else generation
        dialog = self._preview_dialog
        if (
            self.config is None
            or dialog is None
            or generation != self._registration_generation
        ):
            return
        self._pending_registration = pending
        dialog._master = [dict(row) for row in pending.kakou_master]
        dialog._total_count = len(pending.rows)
        self.prepare_worker = RegistrationPrepareWorkerThread(
            generation,
            pending.rows,
            pending.existing_kintone_records,
            self.get_order_overrides(),
        )
        prepare_worker = self.prepare_worker
        prepare_worker.batch_ready.connect(self._on_preview_batch_ready)
        prepare_worker.succeeded.connect(self._on_preview_preparation_complete)
        prepare_worker.failed.connect(self._on_registration_load_failed)
        prepare_worker.start()

    def _on_preview_batch_ready(
        self,
        generation: int,
        rows: list[dict[str, str]],
        existing_by_row: list[dict[str, str]],
    ) -> None:
        dialog = self._preview_dialog
        pending = self._pending_registration
        if (
            generation != self._registration_generation
            or dialog is None
            or pending is None
        ):
            return
        dialog.append_prepared_batch(
            rows, existing_by_row, total_count=len(pending.rows)
        )

    def _on_preview_preparation_complete(
        self, generation: int, metrics: dict[str, object]
    ) -> None:
        dialog = self._preview_dialog
        pending = self._pending_registration
        if (
            generation != self._registration_generation
            or dialog is None
            or pending is None
        ):
            return
        validation_errors = metrics.get("validation_errors", [])
        if validation_errors:
            first_errors = "\n".join(str(item) for item in list(validation_errors)[:5])
            dialog.fail_progressive_loading(
                "必須項目の確認でエラーが見つかりました。\n" + first_errors
            )
            return
        self._preview_prepare_metrics = metrics
        if self._kintone_checks_received:
            self._start_existing_result_merge(generation)

    def _on_kintone_checks_ready(
        self,
        generation: int,
        existing_records: list[dict[str, str]],
        existing_fetch_error: str | None,
    ) -> None:
        pending = self._pending_registration
        dialog = self._preview_dialog
        if (
            generation != self._registration_generation
            or pending is None
            or dialog is None
        ):
            return
        pending.existing_kintone_records = [dict(row) for row in existing_records]
        pending.existing_fetch_error = existing_fetch_error
        reflection_message = summarize_existing_reflection(existing_records)
        if reflection_message:
            self.append_log(reflection_message)
        self._kintone_checks_received = True
        if existing_fetch_error:
            dialog.fail_progressive_loading(
                "Kintone既存データの確認に失敗したため登録できません。\n"
                + existing_fetch_error
            )
            return
        if self._preview_prepare_metrics is not None:
            self._start_existing_result_merge(generation)

    def _start_existing_result_merge(self, generation: int) -> None:
        dialog = self._preview_dialog
        pending = self._pending_registration
        if dialog is None or pending is None or dialog._load_failed:
            return
        if self.existing_merge_worker is not None and self.existing_merge_worker.isRunning():
            return
        self.existing_merge_worker = RegistrationExistingMergeWorkerThread(
            generation,
            [dict(row) for row in dialog._state.rows],
            pending.existing_kintone_records,
        )
        worker = self.existing_merge_worker
        worker.succeeded.connect(self._on_existing_result_merged)
        worker.failed.connect(self._on_registration_load_failed)
        worker.start()

    def _on_existing_result_merged(
        self,
        generation: int,
        merged_rows: list[dict[str, str]],
        existing_by_row: list[dict[str, str]],
        merge_metrics: dict[str, object],
    ) -> None:
        dialog = self._preview_dialog
        if generation != self._registration_generation or dialog is None:
            return
        dialog.apply_existing_check_result(merged_rows, existing_by_row)
        metrics = dict(self._preview_prepare_metrics or {})
        metrics.update(merge_metrics)
        dialog.finish_progressive_loading(metrics)

    def _on_registration_load_failed(self, generation: int, message: str) -> None:
        if generation != self._registration_generation:
            return
        dialog = self._preview_dialog
        if dialog is not None:
            dialog.fail_progressive_loading(message)
        self.result_label.setText(f"エラー有無: あり\n{message}")
        self._set_buttons_enabled(True)

    def _cancel_registration_loading(self) -> None:
        self._registration_generation += 1
        for worker in (self.worker, self.prepare_worker, self.existing_merge_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
        self._preview_dialog = None
        self._pending_registration = None
        self._set_buttons_enabled(True)
        self._clear_busy(context="run_olap")

    def _on_preview_rejected(self) -> None:
        pending = self._pending_registration
        if self._preview_dialog is not None:
            self._cancel_registration_loading()
        if pending is not None:
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

    def _retry_registration_loading(self) -> None:
        dialog = self._preview_dialog
        self._cancel_registration_loading()
        if dialog is not None:
            dialog.reject()
        QTimer.singleShot(0, self.start_run)

    def _on_preview_accepted(self) -> None:
        dialog = self._preview_dialog
        pending = self._pending_registration
        if (
            dialog is None
            or pending is None
            or not dialog._all_rows_ready
            or dialog._load_failed
        ):
            return
        self._preview_dialog = None
        self.log_view.clear()
        self._set_buttons_enabled(False)
        self._set_busy("Kintone登録中...", context="kintone_register")
        self.register_worker = KintoneRegisterWorkerThread(
            self._effective_config(),
            dialog.registration_rows(),
            pending.output_csv,
            pending.timestamp,
        )
        self.register_worker.log_line.connect(self.append_log)
        self.register_worker.registration_completed.connect(self._on_kintone_registration_completed)
        self.register_worker.succeeded.connect(self.on_succeeded)
        self.register_worker.failed.connect(self.on_failed)
        self.register_worker.finished.connect(lambda: self._set_buttons_enabled(True))
        self.register_worker.finished.connect(
            lambda: self._clear_busy(context="kintone_register")
        )
        self.register_worker.start()

    def _collect_input(self, require_denpyo: bool) -> RunInput:
        # 同じ受注Noで二重にOLAP取得・Kintone既存検索・CSV作成・登録が走らないよう
        # 登録前確認画面へ渡す前に重複を排除する（要件6）。
        denpyo_numbers = dedupe_order_numbers(
            parse_order_numbers(self.denpyo_numbers.toPlainText())
        )
        if require_denpyo and not denpyo_numbers:
            raise ValueError("受注Noを1件以上入力してください。")
        # 仕上日「なし」・出荷区分「なし」は空欄（未設定）として扱う（要件3）。
        shiage_date = "" if self.shiage_none.isChecked() else self.shiage_date.date().toString("yyyy-MM-dd")
        shukka_kbn = self.shukka_kbn.currentText()
        if shukka_kbn == SHUKKA_NONE_LABEL:
            shukka_kbn = ""
        return RunInput(
            company_code=self.config.company_code if self.config is not None else "",
            olap_login_id=self._olap_login_id,
            olap_password=self._olap_password,
            denpyo_numbers=denpyo_numbers,
            shiage_date=shiage_date,
            shukka_kbn=shukka_kbn,
        )

    def get_order_numbers(self) -> set[str]:
        """受注No入力欄に現在入力されている受注Noの集合を返す（要件4・5）。

        改行・カンマ・全角カンマ・空白・全角空白で区切り、前後空白を除去する。
        受注Noは文字列扱いで先頭ゼロを保持する。
        """
        return set(parse_order_numbers(self.denpyo_numbers.toPlainText()))

    def add_order_no(
        self,
        order_no: str,
        finish_date: date | None = None,
        am_pm: str | None = None,
    ) -> None:
        """外部（伝票作成・印刷画面）から受注Noを入力欄へ追記する。

        既存入力は消さず、改行区切りで末尾へ追加する。空の場合は何もしない。
        finish_date / am_pm が渡された場合は受注Noごとの override として保持し、
        登録前確認の仕上日／出荷区分の初期値に反映する（要件1・4）。
        追記後はこの画面を前面に出して分かりやすくする。
        """
        order_no = (order_no or "").strip()
        if not order_no:
            return
        # 行設定が渡された場合は override を保持（再追加でも最新値で更新する）。
        if finish_date is not None or am_pm is not None:
            self.set_order_overrides(order_no, finish_date, am_pm)
        existing = self.denpyo_numbers.toPlainText()
        if order_no in set(parse_order_numbers(existing)):
            # 既に入力済みの受注Noは重複追加しない（既存の重複防止仕様を維持: 要件4）。
            # 前面化だけ行い、override は上で更新済み。
            self.show()
            self.raise_()
            self.activateWindow()
            return
        if existing.strip():
            # 既存入力の末尾に改行区切りで追加する（既存仕様の改行区切りに合わせる）。
            new_text = existing.rstrip("\n\r") + "\n" + order_no
        else:
            new_text = order_no
        self.denpyo_numbers.setPlainText(new_text)
        # カーソルを末尾へ移動して追記内容が見えるようにする。
        cursor = self.denpyo_numbers.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.denpyo_numbers.setTextCursor(cursor)
        # 画面を前面化する。
        self.show()
        self.raise_()
        self.activateWindow()

    def set_order_overrides(
        self,
        order_no: str,
        finish_date: date | None,
        am_pm: str | None,
    ) -> None:
        """受注Noごとの仕上日／AM・PM override を保持する（要件4）。

        伝票作成・印刷画面で「なし」を指定した場合は finish_date=None / am_pm="none"
        が渡され、登録前確認で空欄として扱われる。
        """
        order_no = (order_no or "").strip()
        if not order_no:
            return
        self._order_overrides[order_no] = {
            "finish_date": finish_date,
            "am_pm": am_pm,
        }

    def get_order_overrides(self) -> dict[str, dict[str, object]]:
        """現在入力欄に存在する受注Noの override だけを返す（要件4・5）。

        受注No欄から削除されたNoの override は登録前確認へ誤適用しない。
        """
        active = self.get_order_numbers()
        return {
            order_no: dict(override)
            for order_no, override in self._order_overrides.items()
            if order_no in active
        }

    def _prune_order_overrides(self) -> None:
        """受注No欄から消えた受注Noの override を破棄する（要件4）。"""
        active = self.get_order_numbers()
        for order_no in list(self._order_overrides):
            if order_no not in active:
                del self._order_overrides[order_no]

    def remove_order_numbers(self, order_numbers: list[str] | set[str]) -> None:
        """登録成功した受注Noだけを入力欄から削除する。"""
        remove_set = {str(value).strip() for value in order_numbers if str(value).strip()}
        if not remove_set:
            return
        remaining = [order_no for order_no in parse_order_numbers(self.denpyo_numbers.toPlainText()) if order_no not in remove_set]
        self.denpyo_numbers.setPlainText("\n".join(remaining))

    def _on_kintone_registration_completed(self, order_numbers: list[str]) -> None:
        self.remove_order_numbers(order_numbers)
        self.kintone_registration_completed.emit(order_numbers)

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

    # ── 処理中表示（busy/進捗）────────────────────────────────────────────────
    def _set_busy(self, message: str = "", *, context: str = "") -> None:
        """時間がかかる処理の開始時に不定進捗バーを表示する（UI全体は無効化しない）。"""
        self._busy_counter = getattr(self, "_busy_counter", 0) + 1
        _log_busy_event(
            "busy_started", busy_message=message, busy_context=context,
            busy_counter=self._busy_counter,
        )
        bar = getattr(self, "_busy_progress", None)
        if bar is not None:
            try:
                bar.setVisible(True)
            except RuntimeError:
                pass
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _clear_busy(self, *, context: str = "") -> None:
        """処理完了/失敗時に進捗バーを非表示へ戻す（カウンタが0になったら消す）。"""
        self._busy_counter = max(0, getattr(self, "_busy_counter", 0) - 1)
        _log_busy_event(
            "busy_finished", busy_context=context, busy_counter=self._busy_counter
        )
        if self._busy_counter == 0:
            bar = getattr(self, "_busy_progress", None)
            if bar is not None:
                try:
                    bar.setVisible(False)
                except RuntimeError:
                    pass

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


def prepare_app_for_update() -> bool:
    """Run existing unsaved-edit confirmation before installer launch becomes irreversible."""
    app = QApplication.instance()
    if app is None:
        return True
    running_threads: list[QThread] = []
    seen_threads: set[int] = set()
    for widget in list(app.topLevelWidgets()):
        for thread in widget.findChildren(QThread):
            if id(thread) in seen_threads:
                continue
            seen_threads.add(id(thread))
            if thread.isRunning():
                running_threads.append(thread)
    if running_threads:
        logging.getLogger("tks_to_kintone_app").info(
            "event=update_cancelled reason=background_worker_running count=%s",
            len(running_threads),
        )
        parent = QApplication.activeWindow()
        QMessageBox.information(
            parent if isinstance(parent, QWidget) else None,
            "アップデート",
            "実行中の処理があるため更新を開始できません。\n"
            "処理の完了後にもう一度更新を実行してください。",
        )
        return False
    for widget in list(app.topLevelWidgets()):
        is_dirty = getattr(widget, "is_dirty", None)
        if not callable(is_dirty):
            continue
        try:
            if not is_dirty():
                continue
            closed = widget.close()
            if not closed:
                logging.getLogger("tks_to_kintone_app").info(
                    "event=update_cancelled reason=unsaved_edit_confirmation"
                )
                return False
        except Exception:  # pragma: no cover - an unknown editor must block update safely.
            logging.getLogger("tks_to_kintone_app").exception(
                "event=update_preflight_failed"
            )
            return False
    return True


def update_shutdown_is_committed() -> bool:
    app = QApplication.instance()
    return bool(app and app.property(UPDATE_SHUTDOWN_COMMITTED_PROPERTY))


def release_single_instance_lock() -> None:
    """Release the local-server lock before the installer needs the app to be gone."""
    global _SINGLE_INSTANCE_SERVER
    server = _SINGLE_INSTANCE_SERVER
    _SINGLE_INSTANCE_SERVER = None
    if server is not None:
        try:
            server.close()
        except Exception:  # pragma: no cover - shutdown remains best-effort.
            logging.getLogger("tks_to_kintone_app").exception(
                "event=update_single_instance_lock_release_failed"
            )
    QLocalServer.removeServer(f"{SETTINGS_ORG}.{SETTINGS_APP}.single-instance")


def commit_update_shutdown() -> None:
    """Make shutdown irreversible only after Setup produced this attempt's log."""
    app = QApplication.instance()
    if app is not None:
        app.setProperty(UPDATE_SHUTDOWN_COMMITTED_PROPERTY, True)
    release_single_instance_lock()
    logging.getLogger("tks_to_kintone_app").info(
        "event=update_shutdown_committed single_instance_lock_released=true"
    )


def quit_app_for_update() -> None:
    """更新インストーラ起動後に本体アプリを確実に終了する。

    インストーラが本体ファイルを上書きできるよう、``quit()`` だけでは終了しない
    ケースに備えて全トップレベルウィンドウを閉じてから ``quit()`` する。
    """
    app = QApplication.instance()
    if app is None:
        return
    commit_update_shutdown()
    QSettings().sync()
    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
        except Exception:  # pragma: no cover - 終了処理は best-effort
            pass
    logging.getLogger("tks_to_kintone_app").info(
        "event=update_application_quit_requested method=QApplication.quit"
    )
    for handler in logging.getLogger().handlers + logging.getLogger(
        "tks_to_kintone_app"
    ).handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - best-effort during shutdown
            pass
    app.quit()


def run_gui(*, post_update: bool = False) -> int:
    global _SINGLE_INSTANCE_SERVER
    apply_app_dpi_policy()
    app = QApplication([])
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationName(SETTINGS_APP)
    app.setApplicationVersion(VERSION_NAME)
    app.setProperty("post_update", post_update)
    app.setProperty(UPDATE_SHUTDOWN_COMMITTED_PROPERTY, False)
    app.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
    # The launcher performs its update check before any feature worker starts.
    # Initialize the normal application log now so update diagnostics are persisted.
    try:
        startup_config = load_app_config()
        setup_logger(startup_config.paths.log_dir)
    except Exception as exc:  # noqa: BLE001 - logging must not prevent startup.
        logging.getLogger("tks_to_kintone_app").warning(
            "event=application_log_startup_failed error_type=%s", type(exc).__name__
        )
    from app.context_menu import install_japanese_context_menus
    install_japanese_context_menus(app)

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
    _SINGLE_INSTANCE_SERVER = server

    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    # QSettings 側の既定値を補完する。config.env / 加工名マスタは load_app_config() で補完する。
    from app.settings_service import ensure_default_webhook_urls

    ensure_default_webhook_urls(settings)
    apply_theme(str(settings.value(SETTINGS_THEME, THEME_SYSTEM) or THEME_SYSTEM))
    from app.launcher_window import LauncherWindow
    window = LauncherWindow()

    def activate_existing_window() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.close()
        window.activate_existing_instance()

    server.newConnection.connect(activate_existing_window)
    app.aboutToQuit.connect(release_single_instance_lock)
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


def _log_busy_event(event_type: str, **fields: object) -> None:
    """処理中表示のログを残す（失敗しても本処理は止めない）。"""
    try:
        from app import voucher_print_service

        voucher_print_service.log_voucher_print_event(event_type, **fields)
    except Exception:  # noqa: BLE001 - ログ失敗で本処理を止めない
        pass


def _format_error_message(exc: Exception, log_file: Path, debug_file: Path | None) -> str:
    lines = [str(exc), "", f"ログファイル: {log_file}"]
    if debug_file is not None:
        lines.append(f"最新debugファイル: {debug_file}")
    return "\n".join(lines)


def _unique_order_numbers(rows: list[dict[str, str]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        order_no = str(row.get("受注No", "") or "").strip()
        if order_no and order_no not in seen:
            seen.add(order_no)
            result.append(order_no)
    return result


def split_customer_match_keywords(value: str) -> list[str]:
    """得意先自動判定文字列をキーワードに分割する。"""
    return [part for part in re.split(r"[,、\s　]+", str(value or "").strip()) if part]


def normalize_customer_name(customer_name: str) -> str:
    """得意先名称を自動判定用に正規化する（全半角差・前後空白を吸収）。"""
    return unicodedata.normalize("NFKC", str(customer_name or "")).strip()


def customer_key_from_name(customer_name: str, match_patterns: dict[str, str]) -> str:
    """得意先名称に合う得意先キーを、得意先1〜4の順で返す。"""
    name = normalize_customer_name(customer_name)
    if not name:
        return DEFAULT_CUSTOMER_KEY
    for key in CUSTOMER_KEYS:
        for keyword in split_customer_match_keywords(match_patterns.get(key, "")):
            keyword = normalize_customer_name(keyword)
            if keyword and keyword in name:
                return key
    return DEFAULT_CUSTOMER_KEY


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
    from app.theme_utils import (
        SEMANTIC_BUTTON_STYLESHEET,
        apply_app_font_size,
        apply_title_bar_theme_to_top_level_widgets,
        current_app_is_dark,
    )

    apply_app_font_size()
    if theme == THEME_DARK:
        app.setStyleSheet(_with_checkmark_assets(DARK_STYLESHEET + SEMANTIC_BUTTON_STYLESHEET))
        apply_title_bar_theme_to_top_level_widgets(True)
    elif theme == THEME_LIGHT:
        app.setStyleSheet(_with_checkmark_assets(LIGHT_STYLESHEET + SEMANTIC_BUTTON_STYLESHEET))
        apply_title_bar_theme_to_top_level_widgets(False)
    else:
        app.setStyleSheet(SEMANTIC_BUTTON_STYLESHEET)
        apply_title_bar_theme_to_top_level_widgets(current_app_is_dark())
    # 指図書編集画面の反映先ボタンはグローバルQSSより強い直接スタイルを使う。
    # テーマ変更時は、既に開いている画面の通常色も現在テーマへ更新する。
    for widget in app.topLevelWidgets():
        refresh = getattr(widget, "_refresh_reflect_target_button_styles", None)
        if callable(refresh):
            refresh()


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
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateEdit, QTableWidget, QTableView {
  background: #ffffff;
  color: #1f2933;
  border: 1px solid #c7d0d9;
  border-radius: 4px;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QComboBox:disabled, QDateEdit:disabled {
  background: #e5e9ed;
  color: #4b5563;
  border-color: #a8b2bc;
}
QAbstractItemView {
  background: #ffffff;
  color: #1f2933;
  selection-background-color: #1565c0;
  selection-color: #ffffff;
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
QCheckBox:disabled, QRadioButton:disabled {
  color: #66717c;
}
QCheckBox::indicator:disabled {
  border-color: #9aa3ac;
  background: #e1e5e9;
}
QRadioButton {
  color: #1f2933;
}
QRadioButton::indicator {
  width: 14px;
  height: 14px;
  border-radius: 7px;
  border: 2px solid #5f6b76;
  background: #ffffff;
}
QRadioButton::indicator:checked {
  border: 2px solid #0d6efd;
  background: #0d6efd;
}
QRadioButton::indicator:disabled {
  border-color: #9aa3ac;
  background: #dfe4e8;
}
QRadioButton::indicator:checked:disabled {
  border-color: #7f8b96;
  background: #7f8b96;
}
QPushButton, QToolButton {
  background: #1f7a8c;
  color: #ffffff;
  border: 0;
  border-radius: 6px;
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
/* 押下アニメーション: 押した瞬間だけ暗く沈める（背景色のみ変更しレイアウトはズラさない）。 */
QPushButton:pressed, QToolButton:pressed {
  background: #17606e;
}
QPushButton:checked:pressed, QToolButton:checked:pressed {
  background: #004c5c;
}
QPushButton:disabled, QToolButton:disabled {
  background: #9aa7b2;
  color: #f4f6f8;
  border-radius: 6px;
}
QPushButton[reflectTargetButton="true"]:checked,
QPushButton#reflectTargetButton[reflectTargetSelected="true"],
QPushButton[reflectTargetSelected="true"] {
  background-color: #0d6efd;
  color: #ffffff;
  border: 2px solid #66b2ff;
  font-weight: bold;
}
QPushButton[reflectTargetSelected="true"]:disabled {
  background-color: #9aa7b2;
  color: #f4f6f8;
  border: 1px solid #7f8b96;
}
QTabWidget::pane {
  border: 1px solid #c7d0d9;
  background: #ffffff;
}
QTabBar::tab {
  color: #222222;
  background: #e9ecef;
  border: 1px solid #c7d0d9;
  padding: 7px 14px;
}
QTabBar::tab:selected {
  color: #ffffff;
  background: #1565c0;
}
QTabBar::tab:disabled {
  color: #66717c;
  background: #d9dee3;
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
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateEdit, QTableWidget, QTableView {
  background: #2b323a;
  color: #eef2f6;
  border: 1px solid #52606d;
  border-radius: 4px;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QComboBox:disabled, QDateEdit:disabled {
  background: #343c45;
  color: #b9c2cb;
  border-color: #65717d;
}
QAbstractItemView {
  background: #2b323a;
  color: #eef2f6;
  selection-background-color: #1976d2;
  selection-color: #ffffff;
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
QCheckBox:disabled, QRadioButton:disabled {
  color: #aeb8c2;
}
QCheckBox::indicator:disabled {
  border-color: #697580;
  background: #343c45;
}
QRadioButton {
  color: #eef2f6;
}
QRadioButton::indicator {
  width: 14px;
  height: 14px;
  border-radius: 7px;
  border: 2px solid #aab4be;
  background: #20252b;
}
QRadioButton::indicator:checked {
  border: 2px solid #42a5f5;
  background: #42a5f5;
}
QRadioButton::indicator:disabled {
  border-color: #697580;
  background: #343c45;
}
QRadioButton::indicator:checked:disabled {
  border-color: #7b8792;
  background: #7b8792;
}
QPushButton, QToolButton {
  background: #2f9bb3;
  color: #ffffff;
  border: 0;
  border-radius: 6px;
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
/* 押下アニメーション: 押した瞬間だけ暗く沈める（背景色のみ変更しレイアウトはズラさない）。 */
QPushButton:pressed, QToolButton:pressed {
  background: #1f7d92;
}
QPushButton:checked:pressed, QToolButton:checked:pressed {
  background: #d18f00;
}
QPushButton:disabled, QToolButton:disabled {
  background: #52606d;
  color: #e1e6eb;
  border-radius: 6px;
}
QPushButton[reflectTargetButton="true"]:checked,
QPushButton#reflectTargetButton[reflectTargetSelected="true"],
QPushButton[reflectTargetSelected="true"] {
  background-color: #0d6efd;
  color: #ffffff;
  border: 2px solid #66b2ff;
  font-weight: bold;
}
QPushButton[reflectTargetSelected="true"]:disabled {
  background-color: #52606d;
  color: #e1e6eb;
  border: 1px solid #697580;
}
QTabWidget::pane {
  border: 1px solid #52606d;
  background: #2b323a;
}
QTabBar::tab {
  color: #f0f0f0;
  background: #3a424b;
  border: 1px solid #52606d;
  padding: 7px 14px;
}
QTabBar::tab:selected {
  color: #ffffff;
  background: #1976d2;
}
QTabBar::tab:disabled {
  color: #aeb8c2;
  background: #303840;
}
QGroupBox {
  border: 1px solid #52606d;
  border-radius: 4px;
  margin-top: 8px;
  padding-top: 12px;
}
"""
