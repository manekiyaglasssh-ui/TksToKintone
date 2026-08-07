"""伝票作成・印刷画面。

受注Noごとに1行で設定する一覧形式の画面。
1行 = 1つの受注No を扱い、行ごとに仕上日・AM/PM・加工名チェック・
印刷する伝票チェックを設定し、行単位で PDF作成・印刷を実行できる。
"""
from __future__ import annotations

import atexit
import copy
import hashlib
import logging
import json
import os
import tempfile
import threading
import time
import traceback as traceback_module
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QDate, QObject, QSettings, QThread, QTimer, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import load_app_config, resource_path, update_values_in_config
from app.logger import setup_logger
from app.path_utils import (
    VOUCHER_OUTPUT_DIR_ENV_KEY,
    cleanup_old_test_print_pdfs,
    ensure_voucher_output_dir,
    get_order_capture_debug_dir,
    get_test_print_dir,
    get_voucher_output_dir,
)
from app.voucher_data_mapper import (
    build_voucher_pages,
    display_mapping_summary,
    is_missing_voucher_no,
)
from app.voucher_edit_objects import edit_objects_sha256, voucher_key_for
from app.processing_display_names import (
    PROCESSING_DEFINITIONS,
    load_processing_display_names,
    resolve_processing_display_name,
    save_processing_display_names,
    validate_processing_display_name,
)
from app import voucher_print_service
from app.voucher_olap_service import VoucherOlapService
from app.voucher_settings import (
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_RECORD_RETENTION_DAYS,
    PRINT_BACKEND_ACROBAT,
    PRINT_BACKEND_QT,
    PRINT_BACKEND_SUMATRA,
    DEFAULT_OPEN_PDF_AFTER_CREATE,
    load_cache_retention_days,
    load_record_retention_days,
    load_visible_column_keys,
    save_visible_column_keys,
    load_default_ampm,
    load_default_finish_date_none,
    load_default_print_types,
    load_voucher_printer_settings,
    load_sumatra_print_profiles,
    normalize_finish_date_none,
    parse_sumatra_print_settings,
    print_adjustment_summary_text,
    build_sumatra_print_settings,
    ADJUSTMENT_MARGIN_MIN_MM,
    ADJUSTMENT_MARGIN_MAX_MM,
    ADJUSTMENT_OFFSET_MIN_MM,
    ADJUSTMENT_OFFSET_MAX_MM,
    ADJUSTMENT_SCALE_MIN_PERCENT,
    ADJUSTMENT_SCALE_MAX_PERCENT,
    save_default_ampm,
    save_default_finish_date_none,
    save_cache_retention_days,
    save_record_retention_days,
    save_default_print_types,
    save_voucher_printer_settings,
    save_sumatra_print_profiles,
    load_price_display_mode,
    save_price_display_mode,
    PRICE_DISPLAY_CONDITIONAL,
    PRICE_DISPLAY_ALWAYS_SHOW,
    PRICE_DISPLAY_ALWAYS_HIDE,
    SumatraPrintProfile,
    VoucherPrinterSettings,
)
from app.voucher_templates import VOUCHER_TYPES
from app.theme_utils import UI_FONT_POINT_SIZE, apply_windows_title_bar_theme, current_title_bar_is_dark
from app.teams_notifier import (
    KINTONE_TARGET_PROD,
    KINTONE_TARGET_TEST,
    TeamsNotifyError,
    build_teams_order_links_payload,
    default_teams_webhook_url_prod,
    default_teams_webhook_url_test,
    post_teams_webhook,
)
from app.version import VERSION_NAME
from app.window_geometry import clamp_window_to_available_geometry


def _perf_voucher_list(phase: str, started: float, **fields: object) -> None:
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    logging.getLogger("tks_to_kintone_app").info(
        "event=perf_voucher_list phase=%s elapsed_ms=%s%s",
        phase, elapsed_ms, f" {suffix}" if suffix else "",
    )

def normalize_order_no(value: object) -> str:
    """受注Noを重複比較用に正規化する（前後空白除去・全角→半角）。

    空欄は空文字を返す。全角数字が入っても半角と同一視できるよう NFKC で正規化する。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text)


def fetch_voucher_print_data(
    numbers: list[str], olap_login_id: str, olap_password: str
) -> dict:
    """Qtウィジェットへ触れず、OLAP取得からページ変換までを行う純データ経路。"""
    config = load_app_config()
    logger = logging.getLogger("tks_to_kintone_app")
    if not logger.handlers:
        logger, _ = setup_logger(config.paths.log_dir)
    service = VoucherOlapService(config, logger)
    try:
        rows = service.fetch_vouchers(numbers, olap_login_id, olap_password)
    except Exception:
        logger.exception("売上伝票用OLAPデータ取得に失敗しました。受注No=%s", ",".join(numbers))
        raise
    if not rows:
        if service.last_response_r1_count > 0:
            raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
        raise RuntimeError("対象データが見つかりません。\n受注Noを確認してください。")
    missing_order_nos = _missing_voucher_no_order_numbers(rows, numbers)
    if missing_order_nos:
        raise MissingVoucherNoError(sorted(missing_order_nos))
    if not any(_has_minimum_detail_mapping(row) for row in rows):
        raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
    pages = build_voucher_pages(rows)
    if not pages:
        raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
    missing = _missing_required_voucher_fields(pages)
    if missing:
        raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
    logger.info("売上伝票PDFデータ作成: 受注No=%s rows=%s pages=%s", ",".join(numbers), len(rows), len(pages))
    return {"pages": pages, "raw_rows": rows}


# 加工名（行ごとのチェックボックス）
PROCESS_NAMES: list[str] = [
    "エッジング",
    "広幅",
    "工場切",
    "手加工",
    "DM-10",
    "引手",
    "マルチ",
    "洗浄",
    "BOB",
    "印刷",
    "フィルム貼",
    "Rとり",
]

# 加工名は3列、印刷する伝票は2列で表示し、ラベル見切れを防ぐ（要件1）。
PROCESS_COLUMNS = 3
VOUCHER_COLUMNS = 2
VOUCHER_ROW_FONT_SIZE = UI_FONT_POINT_SIZE

# 一覧の列構成
COLUMN_LABELS: list[str] = [
    "□",
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
]

# 列インデックス（COLUMN_LABELS の並びと一致させること）
COL_SELECT = 0
COL_ORDER_NO = 1
COL_REFETCH = 2
COL_FINISH_DATE = 3
COL_AMPM = 4
COL_PROCESS = 5
COL_VOUCHER = 6
COL_EDIT = 7
COL_PDF = 8
COL_PREVIEW = 9
COL_PRINT = 10
COL_KINTONE = 11

# 一番上の「OLAP取得用の新規入力行」専用の固定行高（px）。通常の保存済みレコード行
# （加工名3段チェック・AM/PM2段ラジオ）に比べて中身が受注No入力欄＋取得ボタンだけの
# ため細い。resizeRowsToContents や setDefaultSectionSize(108) の影響で太くなったり細く
# なったりするので、専用の固定値へ常に戻して不揃いを防ぐ（要件1）。
VOUCHER_NEW_INPUT_ROW_HEIGHT = 40
# 別名（OLAP取得用入力行の高さ。上と同一値）。
VOUCHER_OLAP_INPUT_ROW_HEIGHT = VOUCHER_NEW_INPUT_ROW_HEIGHT
# 新規入力行内の受注No入力欄・取得ボタンの最大高さ。行高を押し広げないよう抑える。
VOUCHER_NEW_INPUT_ROW_WIDGET_HEIGHT = VOUCHER_NEW_INPUT_ROW_HEIGHT - 10

# 保存済みレコードの復元は10件単位のバッチで行う（要件2）。最初の10件を表示した時点で
# 画面操作を可能にし、残りはバックグラウンド（イベントループ）で継続する。
INITIAL_INTERACTIVE_ROW_COUNT = 10
BACKGROUND_LOAD_BATCH_SIZE = 10
SAVED_ROWS_RESTORE_BATCH_SIZE = BACKGROUND_LOAD_BATCH_SIZE


@dataclass(frozen=True)
class VoucherColumnSpec:
    """一覧テーブルの列定義（表示設定の列表示/非表示に使う・要件3）。

    - index: COLUMN_LABELS 上の列インデックス
    - key: 設定保存用の安定した識別子（COLUMN_LABELS の並びが変わっても不変）
    - label: 表示設定に出すチェックボックス名
    - hideable: False の列は非表示不可（必須列・右端の操作ボタン列など）
    - default_visible: 初期表示状態
    """

    index: int
    key: str
    label: str
    hideable: bool
    default_visible: bool = True


# 列定義の一元管理（COLUMN_LABELS と並び・インデックスを一致させること）。
# 固定列（非表示不可）は「□」「受注No」「OLAP」の3つだけ（hideable=False）。
# それ以外は右端の操作ボタン列（指図書編集/PDF作成/プレビュー/印刷/Kintone登録）を含め、
# すべて表示/非表示を選択できる（hideable=True）。固定3列が常に表示されるため、
# すべての列が非表示になることはない（要件2）。
VOUCHER_COLUMN_SPECS: list[VoucherColumnSpec] = [
    VoucherColumnSpec(COL_SELECT, "select", "□", False),
    VoucherColumnSpec(COL_ORDER_NO, "order_no", "受注No", False),
    VoucherColumnSpec(COL_REFETCH, "olap", "OLAP", False),
    VoucherColumnSpec(COL_FINISH_DATE, "finish_date", "仕上日", True),
    VoucherColumnSpec(COL_AMPM, "ampm", "AM・PM", True),
    VoucherColumnSpec(COL_PROCESS, "process", "加工名", True),
    VoucherColumnSpec(COL_VOUCHER, "voucher", "印刷する伝票", True),
    VoucherColumnSpec(COL_EDIT, "edit", "指図書編集", True),
    VoucherColumnSpec(COL_PDF, "pdf", "PDF作成", True),
    VoucherColumnSpec(COL_PREVIEW, "preview", "プレビュー", True),
    VoucherColumnSpec(COL_PRINT, "print", "印刷", True),
    VoucherColumnSpec(COL_KINTONE, "kintone", "Kintone登録", True),
]


def default_visible_columns() -> dict[str, bool]:
    """列 key → 既定表示状態の辞書を返す。"""
    return {spec.key: spec.default_visible for spec in VOUCHER_COLUMN_SPECS}


def resolve_visible_columns(stored_keys: list[str] | None) -> dict[str, bool]:
    """保存済みの表示列 key リストから、列 key → 表示可否の辞書を作る（要件3）。

    - 非表示不可(hideable=False)列は常に表示。
    - 保存値に無い hideable 列は非表示。
    - stored_keys が None（未設定）なら既定表示状態を返す。
    - 存在しない古い key は無視する（辞書化の過程で自然に落ちる）。
    """
    if stored_keys is None:
        return default_visible_columns()
    stored = set(stored_keys)
    result: dict[str, bool] = {}
    for spec in VOUCHER_COLUMN_SPECS:
        if not spec.hideable:
            result[spec.key] = True
        else:
            result[spec.key] = spec.key in stored
    return result

# 新規入力行「取得」処理のバージョン識別子。実機で起動中のEXEに今回の修正が
# 反映されているかを、デバッグ表示ON時に画面上で確認できるようにするための目印。
NEW_ROW_FETCH_VERSION = "v4"
NEW_ROW_FETCH_HANDLER = "data-only-fetch"

# 「追加済」状態のKintone登録ボタンの緑色スタイル（要件6）。
# 無効化されても緑色で「追加済」と分かり、未起動の通常無効ボタン（灰色）と区別できる。
# ライト/ダーク両モードで白系文字が読める配色にする。
KINTONE_ADDED_BUTTON_STYLE = """
QPushButton {
    background-color: #2e7d32;
    color: white;
    border: 1px solid #1b5e20;
    border-radius: 3px;
    font-weight: bold;
    padding: 2px 10px;
}
QPushButton:disabled {
    background-color: #2e7d32;
    color: #e8f5e9;
    border: 1px solid #1b5e20;
}
"""

KINTONE_STATUS_UNREGISTERED = "未登録"
KINTONE_STATUS_COMPLETED = "登録完了"

SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
SETTINGS_KINTONE_TARGET = "kintone/target"
SETTINGS_KINTONE_TARGET_PROD = KINTONE_TARGET_PROD
SETTINGS_KINTONE_TARGET_TEST = KINTONE_TARGET_TEST
SETTINGS_TEAMS_ENABLED = "teams/enabled"
SETTINGS_TEAMS_WEBHOOK_URL_TEST = "teams/webhook_url_test"
SETTINGS_TEAMS_WEBHOOK_URL_PROD = "teams/webhook_url_prod"

KINTONE_STATUS_STYLES = {
    KINTONE_STATUS_UNREGISTERED: """
QPushButton {
    background-color: #c62828;
    color: white;
    border: 1px solid #8e0000;
    border-radius: 3px;
    font-weight: bold;
    padding: 2px 10px;
}
QPushButton:disabled { background-color: #c62828; color: #ffffff; border: 1px solid #8e0000; }
""",
    KINTONE_STATUS_COMPLETED: """
QPushButton {
    background-color: #2e7d32;
    color: white;
    border: 1px solid #1b5e20;
    border-radius: 3px;
    font-weight: bold;
    padding: 2px 10px;
}
QPushButton:disabled { background-color: #2e7d32; color: #ffffff; border: 1px solid #1b5e20; }
""",
}

MISSING_VOUCHER_NO_BASE_MESSAGE = "伝票Noがありません。\nTKSで先に処理してください。"
MISSING_VOUCHER_NO_MESSAGE = MISSING_VOUCHER_NO_BASE_MESSAGE


class MissingVoucherNoError(RuntimeError):
    """OLAP取得結果に未発行の伝票Noが含まれるため後続処理を止める例外。"""

    def __init__(self, order_numbers: list[str] | set[str]) -> None:
        self.order_numbers = {str(value).strip() for value in order_numbers if str(value).strip()}
        super().__init__(format_missing_voucher_no_message(self.order_numbers))

# テーブルの列区切り線スタイル（ライト/ダーク両モードで境界が見える: 要件1-2）。
VOUCHER_TABLE_STYLE = """
QTableWidget {
    gridline-color: #9aa0a6;
}
QHeaderView::section {
    border-right: 1px solid #9aa0a6;
    border-bottom: 1px solid #9aa0a6;
    padding: 4px;
}
QTableWidget::item {
    border-right: 1px solid #c4c8cc;
}
"""


@dataclass
class VoucherOrderRow:
    """1受注No行の設定値。"""

    order_no: str
    finish_date: date | None
    am_pm: str
    process_checks: dict[str, bool] = field(default_factory=dict)
    voucher_checks: dict[str, bool] = field(default_factory=dict)
    finish_date_none: bool | None = None

    def __post_init__(self) -> None:
        if self.finish_date_none is None:
            self.finish_date_none = self.finish_date is None


class _RowWidgets:
    """テーブル1行分のウィジェット参照を束ねる。"""

    def __init__(self) -> None:
        self.updated_at: datetime = datetime.now()
        self.is_new_input_row: bool = False
        self.table_row_index: int = -1
        self.select_check: QCheckBox
        self.order_input: QLineEdit
        self.refetch_button: QPushButton
        self.date_edit: QDateEdit
        # 仕上日「なし」チェック。ONで finish_date=None（伝票仕上日欄を空白にする）。
        self.finish_none_check: QCheckBox
        self.ampm_group: QButtonGroup
        self.ampm_none: QRadioButton
        self.ampm_am: QRadioButton
        self.ampm_pm: QRadioButton
        self.process_checks: dict[str, QCheckBox] = {}
        self.voucher_checks: dict[str, QCheckBox] = {}
        # OLAPで取得した伝票データの保持（取得済み判定・再利用用）。
        self.cached_olap: dict | None = None
        self.edit_button: QPushButton
        self.pdf_button: QPushButton
        self.preview_button: QPushButton
        self.print_button: QPushButton
        self.kintone_button: QPushButton
        self.kintone_status_button: QPushButton


class VoucherPrintSettingsDialog(QDialog):
    """印刷する伝票の初期チェック状態とOLAPキャッシュ保存期間を設定するダイアログ。"""

    def __init__(self, selected_ids: set[str], retention_days: int,
                 finish_date_none: bool = False, ampm_default: str = "am",
                 price_display_mode: str = PRICE_DISPLAY_CONDITIONAL,
                 parent: QWidget | None = None, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("印刷する伝票設定")
        self._embedded = bool(embedded)
        self._checks: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)

        group = QGroupBox("新規行の印刷する伝票（初期チェック）")
        group_layout = QVBoxLayout(group)
        for vid, vname in VOUCHER_TYPES:
            cb = QCheckBox(vname)
            cb.setChecked(vid in selected_ids)
            self._checks[vid] = cb
            group_layout.addWidget(cb)
        layout.addWidget(group)

        row_defaults_group = QGroupBox("新規行の仕上日・AM/PM（初期値）")
        row_defaults_layout = QVBoxLayout(row_defaults_group)

        finish_row = QHBoxLayout()
        finish_row.addWidget(QLabel("仕上日:"))
        self._finish_date_none_check = QCheckBox("なし")
        self._finish_date_none_check.setChecked(normalize_finish_date_none(finish_date_none))
        finish_row.addWidget(self._finish_date_none_check)
        finish_row.addStretch(1)
        row_defaults_layout.addLayout(finish_row)

        ampm_row = QHBoxLayout()
        ampm_row.addWidget(QLabel("AM/PM:"))
        self._ampm_group = QButtonGroup(self)
        self._ampm_none = QRadioButton("なし")
        self._ampm_am = QRadioButton("AM")
        self._ampm_pm = QRadioButton("PM")
        self._ampm_group.addButton(self._ampm_none)
        self._ampm_group.addButton(self._ampm_am)
        self._ampm_group.addButton(self._ampm_pm)
        normalized_ampm = str(ampm_default or "am").lower()
        self._ampm_none.setChecked(normalized_ampm == "none")
        self._ampm_pm.setChecked(normalized_ampm == "pm")
        self._ampm_am.setChecked(normalized_ampm not in {"none", "pm"})
        ampm_row.addWidget(self._ampm_none)
        ampm_row.addWidget(self._ampm_am)
        ampm_row.addWidget(self._ampm_pm)
        ampm_row.addStretch(1)
        row_defaults_layout.addLayout(ampm_row)
        layout.addWidget(row_defaults_group)

        price_group = QGroupBox("帳票の単価表示")
        price_layout = QFormLayout(price_group)
        self._price_display_combo = QComboBox()
        self._price_display_combo.addItem("従来条件に従う", PRICE_DISPLAY_CONDITIONAL)
        self._price_display_combo.addItem("常に表示する", PRICE_DISPLAY_ALWAYS_SHOW)
        self._price_display_combo.addItem("常に表示しない", PRICE_DISPLAY_ALWAYS_HIDE)
        index = self._price_display_combo.findData(price_display_mode)
        self._price_display_combo.setCurrentIndex(max(0, index))
        price_layout.addRow("単価表示:", self._price_display_combo)
        layout.addWidget(price_group)

        retention_row = QHBoxLayout()
        retention_row.addWidget(QLabel("OLAPキャッシュ保存期間（日）:"))
        self._retention_spin = QSpinBox()
        self._retention_spin.setRange(1, 365)
        self._retention_spin.setValue(retention_days or DEFAULT_CACHE_RETENTION_DAYS)
        retention_row.addWidget(self._retention_spin)
        retention_row.addStretch(1)
        layout.addLayout(retention_row)

        record_retention_row = QHBoxLayout()
        record_retention_row.addWidget(QLabel("レコード情報保持期間（日）:"))
        self._record_retention_spin = QSpinBox()
        self._record_retention_spin.setRange(1, 3650)
        self._record_retention_spin.setValue(DEFAULT_RECORD_RETENTION_DAYS)
        record_retention_row.addWidget(self._record_retention_spin)
        record_retention_row.addStretch(1)
        layout.addLayout(record_retention_row)

        # 伝票設定タブ下部の「現在の一覧に反映する」チェックボックス（要件4）。
        # これは一時的な操作指定で保存しない（開くたびにOFF）。ON+OK時のみ、
        # 現在表示中の一覧へ伝票設定（印刷する伝票・仕上日/AMPM初期値）を反映する。
        layout.addStretch(1)
        self._apply_to_current_check = QCheckBox("現在の一覧に反映する")
        self._apply_to_current_check.setChecked(False)
        self._apply_to_current_check.setToolTip(
            "チェックすると、OK押下時にこの伝票設定を現在表示中の一覧の全行へ反映します。"
        )
        layout.addWidget(self._apply_to_current_check)

        # 統合設定ダイアログのタブに埋め込むとき（embedded）は、自前のOK/Cancelを出さず
        # 親ダイアログのボタンで一括保存する（要件4）。
        if not self._embedded:
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

    def selected_ids(self) -> set[str]:
        return {vid for vid, cb in self._checks.items() if cb.isChecked()}

    def retention_days(self) -> int:
        return int(self._retention_spin.value())

    def apply_to_current_list_requested(self) -> bool:
        """「現在の一覧に反映する」がONか（要件4）。既定OFF・保存しない一時指定。"""
        return self._apply_to_current_check.isChecked()

    def finish_date_none(self) -> bool:
        return self._finish_date_none_check.isChecked()

    def ampm_default(self) -> str:
        if self._ampm_none.isChecked():
            return "none"
        if self._ampm_pm.isChecked():
            return "pm"
        return "am"

    def price_display_mode(self) -> str:
        return str(self._price_display_combo.currentData() or PRICE_DISPLAY_CONDITIONAL)

    def set_record_retention_days(self, days: int) -> None:
        self._record_retention_spin.setValue(days or DEFAULT_RECORD_RETENTION_DAYS)

    def record_retention_days(self) -> int:
        return int(self._record_retention_spin.value())


# 印刷設定画面のプリンター一覧取得タイムアウト（ミリ秒）。
PRINTER_LIST_LOAD_TIMEOUT_MS = 4000

# バックグラウンドスレッドがダイアログ破棄後も安全に完走できるよう、
# 完了まで参照を保持するモジュールレベルのレジストリ。
_BACKGROUND_SETTINGS_THREADS: "set[QThread]" = set()


def _register_background_settings_thread(thread: "QThread") -> None:
    _BACKGROUND_SETTINGS_THREADS.add(thread)
    thread.finished.connect(lambda: _BACKGROUND_SETTINGS_THREADS.discard(thread))


def _stop_background_settings_threads_atexit() -> None:
    """インタプリタ終了時に取り残したバックグラウンドスレッドを quit + wait で停止する。

    ウィンドウが close されず deleteLater だけで破棄されたケースでも、稼働中の
    QThread が破棄されて SIGABRT になるのを防ぐ（要件4）。
    """
    for thread in list(_BACKGROUND_SETTINGS_THREADS):
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
        except Exception:
            pass


atexit.register(_stop_background_settings_threads_atexit)


class _CallableWorker(QObject):
    """UI スレッドを止めずに任意の関数をバックグラウンド実行するワーカー。"""

    finished = Signal(object)
    failed = Signal(str, object)

    def __init__(self, func) -> None:
        super().__init__()
        self._func = func

    @Slot()
    def run(self) -> None:
        try:
            result = self._func()
        except Exception as exc:
            self.failed.emit(
                str(exc),
                {
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback_module.format_exc(),
                },
            )
            return
        self.finished.emit(result)


def _read_and_normalize_saved_records(path: "Path", retention_days: int) -> list[dict]:
    """保存済み一覧ファイルを読み込み、保持期間フィルタ・正規化・重複排除して返す。

    UIウィジェットには一切触れないため、ワーカースレッドから安全に呼べる（要件4）。
    受注No空・期限切れ・重複（更新日時が新しい1件のみ採用）を除いた最終復元対象を返す。
    読み込み失敗や0件のときは空リストを返す。
    """
    logger = logging.getLogger("tks_to_kintone_app")
    perf_started = time.perf_counter()
    if not path.is_file():
        _perf_voucher_list("saved_vouchers_loaded", perf_started, count=0)
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    payload_saved_at = str(payload.get("saved_at") or "") if isinstance(payload, dict) else ""

    # 保持期間フィルタ（UIスレッドの _filter_records_by_retention と同一ロジック）。
    cutoff = datetime.now() - timedelta(days=retention_days)
    fallback_dt = datetime.fromtimestamp(path.stat().st_mtime)
    kept: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        dt = _record_datetime(record) or fallback_dt
        if dt >= cutoff:
            kept.append(record)
    logger.info("voucher records loaded count: %s", len(kept))
    if not kept:
        return []

    fallback_updated_at = _datetime_from_iso(payload_saved_at) or datetime.now()
    normalized_records: list[dict] = []
    for record in kept:
        if not isinstance(record, dict):
            continue
        normalized = dict(record)
        if _datetime_from_iso(str(normalized.get("updated_at") or "")) is None:
            normalized["updated_at"] = (
                _record_datetime(normalized) or fallback_updated_at
            ).isoformat(timespec="seconds")
        normalized_records.append(normalized)
    normalized_records.sort(
        key=lambda record: _datetime_from_iso(str(record.get("updated_at") or "")) or datetime.min,
        reverse=True,
    )
    # 同じ受注Noが複数保存されている場合は更新日時が新しい1件だけ残す。
    deduped_records: list[dict] = []
    seen_order_nos: set[str] = set()
    for record in normalized_records:
        key = normalize_order_no(record.get("order_no"))
        if not key:
            logger.info("復元時に受注Noが空の保存行を除外しました。")
            continue
        if key in seen_order_nos:
            logger.info(
                "復元時に重複受注Noを除外しました（最新1件のみ採用）。受注No=%s",
                str(record.get("order_no") or "").strip(),
            )
            continue
        seen_order_nos.add(key)
        deduped_records.append(record)
    _perf_voucher_list("saved_vouchers_loaded", perf_started,
                       count=len(deduped_records))
    return deduped_records


class _SavedRecordsLoadWorker(QObject):
    """保存済み一覧のファイル読み込み・正規化をUIスレッド外で行うワーカー（要件2・4）。

    Qtウィジェットには一切触れず、正規化済みレコードの list だけを loaded signal で返す。
    UIへの反映（QTableWidget への行追加）は必ずメインスレッド側の slot で行う。
    """

    loaded = Signal(object)  # list[dict]
    failed = Signal(str)

    def __init__(self, path: "Path", retention_days: int) -> None:
        super().__init__()
        self._path = path
        self._retention_days = retention_days

    @Slot()
    def run(self) -> None:
        try:
            records = _read_and_normalize_saved_records(self._path, self._retention_days)
        except Exception as exc:  # noqa: BLE001 - 失敗はUIスレッドへ通知する
            logging.getLogger("tks_to_kintone_app").exception(
                "保存済み一覧のワーカー読み込みに失敗しました。"
            )
            self.failed.emit(str(exc))
            return
        self.loaded.emit(records)


class _VoucherEditorDataWorker(QObject):
    """編集画面用データ/PDFを生成する純データworker。Qt GUIには触れない。"""

    current_ready = Signal(int, object, str, bytes)  # generation, voucher_nos, voucher_no, pdf
    all_ready = Signal(int, object)  # generation, dict[voucher_no, pdf]
    failed = Signal(int, str)
    completed = Signal()

    def __init__(self, generation: int, row: VoucherOrderRow,
                 cached_data: dict | None, login_id: str, password: str) -> None:
        super().__init__()
        self._generation = generation
        self._row = row
        self._cached_data = copy.deepcopy(cached_data) if isinstance(cached_data, dict) else None
        self._login_id = login_id
        self._password = password
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        started = time.perf_counter()
        try:
            data = self._cached_data
            pages = data.get("pages") if isinstance(data, dict) else None
            # 保存レコードのplaceholderページではPDFを作れないためOLAPから再取得する。
            if not isinstance(pages, list) or not pages or not any(
                isinstance(page, dict) and str(page.get("customer_name") or "").strip()
                for page in pages
            ):
                data = fetch_voucher_print_data(
                    [self._row.order_no], self._login_id, self._password)
            if self._cancelled.is_set():
                return
            data = copy.deepcopy(data)
            VoucherWindow._attach_row_settings(data, self._row)
            unique_pages: list[tuple[str, dict]] = []
            seen: set[str] = set()
            for page in data.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                page["edit_objects"] = []
                voucher_no = str(page.get("voucher_no") or "").strip()
                key = voucher_no or "__empty__"
                if key not in seen:
                    seen.add(key)
                    unique_pages.append((voucher_no, page))
            voucher_nos = [number for number, _page in unique_pages] or [""]
            from app import voucher_service

            generated: dict[str, bytes] = {}
            first_no, first_page = unique_pages[0] if unique_pages else ("", None)
            first_data = dict(data)
            if first_page is not None:
                first_data["pages"] = [first_page]
            phase = time.perf_counter()
            first_pdf = voucher_service.build_vouchers_pdf_bytes(["03"], first_data)
            logging.getLogger("tks_to_kintone_app").info(
                "event=perf_voucher_editor phase=background_pdf_generate elapsed_ms=%.3f voucher_no=%s",
                (time.perf_counter() - phase) * 1000.0, first_no,
            )
            if self._cancelled.is_set():
                return
            generated[first_no] = first_pdf
            self.current_ready.emit(self._generation, voucher_nos, first_no, first_pdf)
            for voucher_no, page in unique_pages[1:]:
                if self._cancelled.is_set():
                    return
                single = dict(data)
                single["pages"] = [page]
                generated[voucher_no] = voucher_service.build_vouchers_pdf_bytes(["03"], single)
            if self._cancelled.is_set():
                return
            self.all_ready.emit(self._generation, generated)
            logging.getLogger("tks_to_kintone_app").info(
                "event=perf_voucher_editor phase=all_preload_complete elapsed_ms=%.3f count=%s",
                (time.perf_counter() - started) * 1000.0, len(generated),
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("tks_to_kintone_app").exception(
                "指図書編集の背景生成workerに失敗しました。")
            self.failed.emit(self._generation, str(exc))
        finally:
            self.completed.emit()


class VoucherPrinterSettingsDialog(QDialog):
    """保存済みプリンターで即時印刷するための設定ダイアログ。

    重い処理（プリンター一覧取得・自動検出）は UI スレッドで同期実行せず、
    初期表示では保存済み設定のみを表示する。プリンター一覧は表示後に
    バックグラウンドで取得し、signal 経由で反映する。
    """

    def __init__(self, parent: QWidget | None = None, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("伝票印刷設定")
        self._embedded = bool(embedded)
        self._settings = load_voucher_printer_settings()
        self._sumatra_profiles = load_sumatra_print_profiles()
        self._syncing_sumatra_controls = False

        # バックグラウンドワーカー管理用の状態。
        self._alive = True
        self._bg_threads: "set[QThread]" = set()
        self._printer_load_started = False
        self._printer_load_finished = False
        self._printer_load_start_monotonic = 0.0

        # ── ダイアログ全体レイアウト（スクロール領域＋下部固定ボタン）─────────
        # 内容が縦に長いため QScrollArea に入れ、画面が小さくても下部の主要
        # ボタンが見切れないよう保存/キャンセル等はスクロール外に固定する。
        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._settings_scroll = scroll
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        # ── グループ1: 基本設定 ────────────────────────────────────────────
        basic_group = QGroupBox("基本設定")
        basic_form = QFormLayout(basic_group)
        basic_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._printer_combo = QComboBox()
        self._init_printer_combo_from_saved()
        basic_form.addRow("プリンター:", self._printer_combo)

        self._backend_combo = QComboBox()
        self._backend_combo.addItem("SumatraPDF経由（標準・高速）", PRINT_BACKEND_SUMATRA)
        self._backend_combo.addItem("Acrobat Reader経由（サイズ確認用・画面表示あり）", PRINT_BACKEND_ACROBAT)
        self._backend_combo.addItem("Qt直接印刷（予備）", PRINT_BACKEND_QT)
        backend_index = self._backend_combo.findData(self._settings.print_backend)
        self._backend_combo.setCurrentIndex(backend_index if backend_index >= 0 else 0)
        basic_form.addRow("印刷方式:", self._backend_combo)

        backend_note = QLabel(
            "SumatraPDF経由：標準方式です。起動が軽く高速です。\n"
            "Acrobat Reader経由：PDF手動印刷に近い結果になる場合がありますが、環境によってAcrobat画面が表示されることがあります。\n"
            "Qt直接印刷：Acrobat Reader不要ですが、印刷サイズが異なる場合があります。"
        )
        backend_note.setWordWrap(True)
        basic_form.addRow("", backend_note)

        # 初期化中はUIスレッドでレジストリ探索を行わない。空欄は印刷時の自動検出を表し、
        # 利用者が即時確認したい場合は「自動検出」ボタンを使う。
        sumatra_path_display = self._settings.sumatra_path
        self._sumatra_path_edit = QLineEdit(sumatra_path_display)
        # 長いパスでも横幅が広がりすぎないよう最小幅を抑え、全文は tooltip で見せる。
        self._sumatra_path_edit.setMinimumWidth(0)
        self._sumatra_path_edit.setToolTip(sumatra_path_display)
        self._sumatra_path_edit.textChanged.connect(self._sumatra_path_edit.setToolTip)
        sumatra_path_row = QHBoxLayout()
        sumatra_path_row.addWidget(self._sumatra_path_edit, 1)
        self._sumatra_detect_button = QPushButton("自動検出")
        self._sumatra_detect_button.clicked.connect(self._detect_sumatra_path)
        sumatra_path_row.addWidget(self._sumatra_detect_button)
        sumatra_browse_button = QPushButton("参照")
        sumatra_browse_button.clicked.connect(self._browse_sumatra_path)
        sumatra_path_row.addWidget(sumatra_browse_button)
        sumatra_path_widget = QWidget()
        sumatra_path_widget.setLayout(sumatra_path_row)
        basic_form.addRow("SumatraPDFパス:", sumatra_path_widget)

        scroll_layout.addWidget(basic_group)

        # ── グループ2: 設定プロファイル ────────────────────────────────────
        profile_group = QGroupBox("設定プロファイル")
        profile_form = QFormLayout(profile_group)
        profile_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._sumatra_profile_combo = QComboBox()
        self._refresh_sumatra_profile_combo(self._settings.sumatra_profile_name)
        profile_form.addRow("設定読込:", self._sumatra_profile_combo)

        self._sumatra_profile_name_edit = QLineEdit(self._settings.sumatra_profile_name)
        profile_form.addRow("設定名:", self._sumatra_profile_name_edit)

        self._sumatra_profile_memo_edit = QLineEdit("")
        self._sumatra_profile_memo_edit.setPlaceholderText("任意メモ")
        profile_form.addRow("メモ:", self._sumatra_profile_memo_edit)

        profile_buttons = QHBoxLayout()
        self._sumatra_profile_load_button = QPushButton("設定読込")
        self._sumatra_profile_load_button.clicked.connect(self._load_sumatra_profile)
        self._sumatra_profile_save_button = QPushButton("設定保存")
        self._sumatra_profile_save_button.clicked.connect(self._save_sumatra_profile)
        profile_buttons.addWidget(self._sumatra_profile_load_button)
        profile_buttons.addWidget(self._sumatra_profile_save_button)
        profile_buttons_widget = QWidget()
        profile_buttons_widget.setLayout(profile_buttons)
        profile_form.addRow("", profile_buttons_widget)

        scroll_layout.addWidget(profile_group)

        # ── グループ3: SumatraPDF詳細設定 ──────────────────────────────────
        sumatra_group = QGroupBox("SumatraPDF詳細設定")
        sumatra_form = QFormLayout(sumatra_group)
        sumatra_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._sumatra_preset_combo = QComboBox()
        from app.voucher_settings import SUMATRA_PRINT_SETTINGS_PRESETS

        for label, value in SUMATRA_PRINT_SETTINGS_PRESETS:
            self._sumatra_preset_combo.addItem(label, value)
        self._sumatra_preset_combo.currentIndexChanged.connect(self._apply_sumatra_preset)
        sumatra_form.addRow("SumatraPDFプリセット:", self._sumatra_preset_combo)

        self._sumatra_settings_edit = QLineEdit(self._settings.sumatra_print_settings)
        self._sumatra_settings_edit.textEdited.connect(self._on_sumatra_settings_edited)
        sumatra_form.addRow("SumatraPDF印刷設定:", self._sumatra_settings_edit)

        self._sumatra_scaling_combo = QComboBox()
        self._sumatra_scaling_combo.addItem("noscale", "noscale")
        self._sumatra_scaling_combo.addItem("fit", "fit")
        self._sumatra_scaling_combo.addItem("shrink", "shrink")
        sumatra_form.addRow("拡大縮小:", self._sumatra_scaling_combo)

        self._sumatra_paper_mode_combo = QComboBox()
        self._sumatra_paper_mode_combo.addItem("paper=auto", "auto")
        self._sumatra_paper_mode_combo.addItem("paperkind指定", "paperkind")
        self._sumatra_paper_mode_combo.addItem("paper=<任意文字列>", "paper")
        sumatra_form.addRow("用紙指定:", self._sumatra_paper_mode_combo)

        self._sumatra_paper_value_edit = QLineEdit(self._settings.sumatra_paper_value)
        self._sumatra_paper_value_edit.setPlaceholderText("paper=<任意文字列> の値")
        sumatra_form.addRow("用紙:", self._sumatra_paper_value_edit)

        self._sumatra_paperkind_edit = QLineEdit(self._settings.sumatra_paperkind)
        self._sumatra_paperkind_edit.setPlaceholderText("空=paper=auto。B5が選ばれない場合のみ番号を指定")
        sumatra_form.addRow("SumatraPDF paperkind:", self._sumatra_paperkind_edit)

        self._sumatra_monochrome_check = QCheckBox("白黒印刷")
        self._sumatra_monochrome_check.setChecked(bool(self._settings.sumatra_monochrome))
        sumatra_form.addRow("", self._sumatra_monochrome_check)

        self._sumatra_center_check = QCheckBox("中央配置")
        self._sumatra_center_check.setChecked(bool(self._settings.sumatra_center))
        sumatra_form.addRow("", self._sumatra_center_check)

        self._sumatra_auto_rotation_check = QCheckBox("自動回転")
        self._sumatra_auto_rotation_check.setChecked(bool(self._settings.sumatra_auto_rotation))
        sumatra_form.addRow("", self._sumatra_auto_rotation_check)

        self._sumatra_bin_edit = QLineEdit(self._settings.sumatra_bin)
        self._sumatra_bin_edit.setPlaceholderText("auto または任意文字列")
        sumatra_form.addRow("給紙トレイ:", self._sumatra_bin_edit)

        self._sumatra_extra_options_edit = QLineEdit(self._settings.sumatra_extra_options)
        self._sumatra_extra_options_edit.setPlaceholderText("追加トークンをカンマ区切りで指定")
        sumatra_form.addRow("追加オプション:", self._sumatra_extra_options_edit)

        self._sumatra_generated_label = QLabel("")
        self._sumatra_generated_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._sumatra_generated_label.setWordWrap(True)
        sumatra_form.addRow("生成 print-settings:", self._sumatra_generated_label)

        self._sumatra_wait_timeout_spin = QSpinBox()
        self._sumatra_wait_timeout_spin.setRange(5, 120)
        self._sumatra_wait_timeout_spin.setValue(
            int(self._settings.sumatra_wait_timeout_seconds or 15)
        )
        self._sumatra_wait_timeout_spin.setSuffix(" 秒")
        sumatra_form.addRow("SumatraPDF終了待ち秒数:", self._sumatra_wait_timeout_spin)

        sumatra_wait_note = QLabel(
            "印刷要求送信後、SumatraPDFの終了コード確認を待つ秒数です。"
            "印刷要求送信後は画面操作に戻ります。"
        )
        sumatra_wait_note.setWordWrap(True)
        sumatra_form.addRow("", sumatra_wait_note)

        scroll_layout.addWidget(sumatra_group)

        self._connect_sumatra_detail_controls()
        self._set_sumatra_detail_controls_from_settings(self._settings.sumatra_print_settings)

        # ── グループ: 印刷位置・余白補正（SumatraPDF印刷時のみ適用）──────────
        adjustment_group = QGroupBox("印刷位置・余白補正")
        adjustment_form = QFormLayout(adjustment_group)
        adjustment_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._adjustment_enabled_check = QCheckBox("印刷補正を有効にする")
        self._adjustment_enabled_check.setChecked(bool(self._settings.print_adjustment_enabled))
        adjustment_form.addRow("", self._adjustment_enabled_check)

        def _make_margin_spin(value: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(ADJUSTMENT_MARGIN_MIN_MM, ADJUSTMENT_MARGIN_MAX_MM)
            spin.setSingleStep(0.5)
            spin.setDecimals(2)
            spin.setSuffix(" mm")
            spin.setValue(float(value))
            return spin

        def _make_offset_spin(value: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(ADJUSTMENT_OFFSET_MIN_MM, ADJUSTMENT_OFFSET_MAX_MM)
            spin.setSingleStep(0.5)
            spin.setDecimals(2)
            spin.setSuffix(" mm")
            spin.setValue(float(value))
            return spin

        def _make_scale_spin(value: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(ADJUSTMENT_SCALE_MIN_PERCENT, ADJUSTMENT_SCALE_MAX_PERCENT)
            spin.setSingleStep(0.5)
            spin.setDecimals(2)
            spin.setSuffix(" %")
            spin.setValue(float(value))
            return spin

        self._adjustment_margin_left_spin = _make_margin_spin(self._settings.print_adjustment_margin_left_mm)
        adjustment_form.addRow("左余白補正:", self._adjustment_margin_left_spin)
        self._adjustment_margin_right_spin = _make_margin_spin(self._settings.print_adjustment_margin_right_mm)
        adjustment_form.addRow("右余白補正:", self._adjustment_margin_right_spin)
        self._adjustment_margin_top_spin = _make_margin_spin(self._settings.print_adjustment_margin_top_mm)
        adjustment_form.addRow("上余白補正:", self._adjustment_margin_top_spin)
        self._adjustment_margin_bottom_spin = _make_margin_spin(self._settings.print_adjustment_margin_bottom_mm)
        adjustment_form.addRow("下余白補正:", self._adjustment_margin_bottom_spin)

        self._adjustment_scale_x_spin = _make_scale_spin(self._settings.print_adjustment_scale_x_percent)
        adjustment_form.addRow("横倍率:", self._adjustment_scale_x_spin)
        self._adjustment_scale_y_spin = _make_scale_spin(self._settings.print_adjustment_scale_y_percent)
        adjustment_form.addRow("縦倍率:", self._adjustment_scale_y_spin)

        self._adjustment_offset_x_spin = _make_offset_spin(self._settings.print_adjustment_offset_x_mm)
        adjustment_form.addRow("横位置補正:", self._adjustment_offset_x_spin)
        self._adjustment_offset_y_spin = _make_offset_spin(self._settings.print_adjustment_offset_y_mm)
        adjustment_form.addRow("縦位置補正:", self._adjustment_offset_y_spin)

        self._adjustment_save_pdf_check = QCheckBox("補正済みPDFを保存する")
        self._adjustment_save_pdf_check.setChecked(bool(self._settings.print_adjustment_save_pdf))
        adjustment_form.addRow("", self._adjustment_save_pdf_check)

        adjustment_note = QLabel(
            "印刷補正はSumatraPDF経由印刷とテスト印刷にのみ適用されます。\n"
            "Acrobat Reader経由・Qt直接印刷・PDF保存・プレビューには適用しません。\n"
            "補正設定は「設定プロファイル」の保存/読込にも含まれます。"
        )
        adjustment_note.setWordWrap(True)
        adjustment_form.addRow("", adjustment_note)

        self._adjustment_summary_label = QLabel("")
        self._adjustment_summary_label.setWordWrap(True)
        self._adjustment_summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        adjustment_form.addRow("補正内容:", self._adjustment_summary_label)

        scroll_layout.addWidget(adjustment_group)
        self._connect_adjustment_controls()
        self._update_adjustment_summary()

        # ── グループ: Acrobat Reader設定 ───────────────────────────────────
        acrobat_group = QGroupBox("Acrobat Reader設定")
        acrobat_form = QFormLayout(acrobat_group)
        acrobat_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._acrobat_path_edit = QLineEdit(self._settings.acrobat_path)
        self._acrobat_path_edit.setToolTip(self._settings.acrobat_path)
        self._acrobat_path_edit.textChanged.connect(self._acrobat_path_edit.setToolTip)
        acrobat_path_row = QHBoxLayout()
        acrobat_path_row.addWidget(self._acrobat_path_edit, 1)
        self._acrobat_detect_button = QPushButton("自動検出")
        self._acrobat_detect_button.clicked.connect(self._detect_acrobat_path)
        acrobat_path_row.addWidget(self._acrobat_detect_button)
        browse_button = QPushButton("参照")
        browse_button.clicked.connect(self._browse_acrobat_path)
        acrobat_path_row.addWidget(browse_button)
        acrobat_path_widget = QWidget()
        acrobat_path_widget.setLayout(acrobat_path_row)
        acrobat_form.addRow("Acrobat Readerパス:", acrobat_path_widget)

        self._acrobat_hide_window_check = QCheckBox("Acrobat Readerを最小化/非表示で起動")
        self._acrobat_hide_window_check.setChecked(bool(self._settings.acrobat_hide_window))
        acrobat_form.addRow("", self._acrobat_hide_window_check)

        self._acrobat_close_after_print_check = QCheckBox("印刷後にAcrobat Readerを閉じる")
        self._acrobat_close_after_print_check.setChecked(bool(self._settings.acrobat_close_after_print))
        acrobat_form.addRow("", self._acrobat_close_after_print_check)

        self._acrobat_close_delay_spin = QSpinBox()
        self._acrobat_close_delay_spin.setRange(5, 60)
        self._acrobat_close_delay_spin.setValue(int(self._settings.acrobat_close_delay_seconds or 10))
        acrobat_form.addRow("閉じるまでの待機秒数:", self._acrobat_close_delay_spin)

        self._acrobat_allow_force_kill_check = QCheckBox("強制終了を許可")
        self._acrobat_allow_force_kill_check.setChecked(bool(self._settings.acrobat_allow_force_kill))
        acrobat_form.addRow("", self._acrobat_allow_force_kill_check)

        self._acrobat_hide_watch_check = QCheckBox("Acrobat画面を監視して非表示にする")
        self._acrobat_hide_watch_check.setChecked(bool(self._settings.acrobat_hide_watch_enabled))
        acrobat_form.addRow("", self._acrobat_hide_watch_check)

        self._acrobat_hide_watch_seconds_spin = QSpinBox()
        self._acrobat_hide_watch_seconds_spin.setRange(1, 30)
        self._acrobat_hide_watch_seconds_spin.setValue(int(self._settings.acrobat_hide_watch_seconds or 10))
        acrobat_form.addRow("非表示監視秒数:", self._acrobat_hide_watch_seconds_spin)

        driver_note = QLabel(
            "Acrobat Reader経由印刷はサイズ確認用・予備です。用紙サイズ・白黒設定はプリンタードライバー側の設定が優先される場合があります。"
            "PDF手動印刷に近い結果になる場合がありますが、完全サイレントは保証しません。\n"
            "既に開いているAcrobat Readerは安全のため閉じません。強制終了は通常使わないでください。"
        )
        driver_note.setWordWrap(True)
        acrobat_form.addRow("", driver_note)

        scroll_layout.addWidget(acrobat_group)

        # ── グループ: 共通印刷設定 ─────────────────────────────────────────
        common_group = QGroupBox("共通印刷設定")
        common_form = QFormLayout(common_group)
        common_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._paper_combo = QComboBox()
        self._paper_combo.addItem("B5", "B5")
        self._paper_combo.setCurrentIndex(0)
        common_form.addRow("用紙サイズ:", self._paper_combo)

        self._orientation_combo = QComboBox()
        self._orientation_combo.addItem("横", "landscape")
        self._orientation_combo.addItem("縦", "portrait")
        orientation_index = self._orientation_combo.findData(self._settings.orientation)
        self._orientation_combo.setCurrentIndex(orientation_index if orientation_index >= 0 else 0)
        common_form.addRow("印刷方向:", self._orientation_combo)

        self._color_combo = QComboBox()
        self._color_combo.addItem("白黒", "grayscale")
        self._color_combo.addItem("カラー", "color")
        color_index = self._color_combo.findData(self._settings.color_mode)
        self._color_combo.setCurrentIndex(color_index if color_index >= 0 else 0)
        common_form.addRow("色:", self._color_combo)

        self._copies_spin = QSpinBox()
        self._copies_spin.setRange(1, 99)
        self._copies_spin.setValue(max(1, int(self._settings.copies or 1)))
        common_form.addRow("部数:", self._copies_spin)

        self._scale_mode_combo = QComboBox()
        self._scale_mode_combo.addItem("実際のサイズ（100%）", "actual_size")
        self._scale_mode_combo.addItem("用紙に合わせる", "fit_to_page")
        scale_index = self._scale_mode_combo.findData(self._settings.scale_mode)
        self._scale_mode_combo.setCurrentIndex(scale_index if scale_index >= 0 else 0)
        common_form.addRow("印刷倍率:", self._scale_mode_combo)

        # 行の「印刷」ボタン押下時に、印刷用PDFに加えてPDF出力先へ通常PDFも保存する。
        self._save_pdf_on_print_check = QCheckBox("印刷時にPDFも作成する")
        self._save_pdf_on_print_check.setChecked(bool(self._settings.save_pdf_on_print))
        self._save_pdf_on_print_check.setToolTip(
            "ONにすると、行の印刷ボタン押下時にPDF出力先へも補正前の通常PDFを保存します。\n"
            "PDF保存に失敗した場合は印刷を中止します。"
        )
        common_form.addRow("", self._save_pdf_on_print_check)

        # 「PDF作成」ボタン押下後の「作成しました」ダイアログ表示可否（共通印刷設定）。
        self._show_pdf_created_dialog_check = QCheckBox("PDF作成完了ダイアログを表示する")
        self._show_pdf_created_dialog_check.setChecked(
            bool(getattr(self._settings, "show_pdf_created_dialog", True))
        )
        self._show_pdf_created_dialog_check.setToolTip(
            "ONにすると、PDF作成ボタン押下後に「作成しました」ダイアログを表示します。\n"
            "OFFにすると、ダイアログを表示せず画面下部のステータス表示のみ更新します。\n"
            "エラー時の通知はどちらの設定でも表示します。"
        )
        common_form.addRow("", self._show_pdf_created_dialog_check)

        self._open_pdf_after_create_check = QCheckBox("PDF作成後にPDFを自動で開く")
        self._open_pdf_after_create_check.setChecked(
            bool(getattr(self._settings, "open_pdf_after_create", True))
        )
        self._open_pdf_after_create_check.setToolTip(
            "ONにすると、PDF作成ボタンで保存したPDFを作成後に開きます。\n"
            "複数PDFを作成した場合はPDFを連続で開かず、出力フォルダを1回だけ開きます。\n"
            "印刷時に保存されるPDFには適用しません。"
        )
        common_form.addRow("", self._open_pdf_after_create_check)

        scroll_layout.addWidget(common_group)

        # ── グループ4: コマンド概要（固定高さ・読み取り専用）───────────────
        command_group = QGroupBox("コマンド概要")
        command_layout = QVBoxLayout(command_group)
        self._sumatra_command_summary_label = QPlainTextEdit("")
        self._sumatra_command_summary_label.setReadOnly(True)
        # 縦に伸びすぎないよう固定高さにし、長い内容は内部スクロールで表示する。
        self._sumatra_command_summary_label.setMaximumHeight(160)
        self._sumatra_command_summary_label.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        command_layout.addWidget(self._sumatra_command_summary_label)
        scroll_layout.addWidget(command_group)
        self._update_sumatra_command_summary()

        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        # ── 下部固定エリア: ステータス＋主要ボタン（スクロール外）──────────
        self._status_label = QLabel("設定を読み込みました")
        self._status_label.setWordWrap(True)

        self._sumatra_test_print_button = QPushButton("テスト印刷")
        self._sumatra_test_print_button.clicked.connect(self._request_test_print)

        # 統合設定ダイアログのタブに埋め込むとき（embedded）は、保存/キャンセルは親の
        # ボタンで一括処理するため出さない。テスト印刷・既定に戻すはタブ内でも使える（要件4）。
        if self._embedded:
            standard_buttons = QDialogButtonBox.StandardButton.RestoreDefaults
        else:
            standard_buttons = (
                QDialogButtonBox.StandardButton.Save
                | QDialogButtonBox.StandardButton.Cancel
                | QDialogButtonBox.StandardButton.RestoreDefaults
            )
        buttons = QDialogButtonBox(standard_buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        reset_button = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        if reset_button is not None:
            reset_button.setText("既定に戻す")
            reset_button.clicked.connect(self._restore_defaults)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("保存")
        # テスト印刷は保存/キャンセルと同じ下部固定行に置く。
        buttons.addButton(self._sumatra_test_print_button, QDialogButtonBox.ButtonRole.ActionRole)

        bottom_bar = QHBoxLayout()
        bottom_bar.addWidget(self._status_label, 1)
        bottom_bar.addWidget(buttons)
        layout.addLayout(bottom_bar)

        # ── ダイアログサイズを画面サイズに合わせる（固定値だけにしない）────
        self.setSizeGripEnabled(True)
        available = None
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
        if available is not None:
            width = min(820, int(available.width() * 0.9))
            height = min(900, int(available.height() * 0.9))
            self.setMinimumSize(min(720, width), min(500, height))
            self.resize(max(min(720, available.width()), width), max(min(500, available.height()), height))
        else:
            self.setMinimumSize(720, 500)
            self.resize(780, 760)

        voucher_print_service.log_print_settings_event(
            "print_settings_dialog_opened",
            saved_printer_name=self._settings.printer_name,
            print_backend=self._settings.print_backend,
        )

    # ── ステータス表示 ──────────────────────────────────────────────────────
    def _set_status(self, text: str) -> None:
        """モーダルを出さずにステータスラベルへ表示する。破棄済みなら無視する。"""
        if not self._alive:
            return
        try:
            self._status_label.setText(text)
        except RuntimeError:
            # 既に C++ オブジェクトが破棄済みの場合は無視する。
            self._alive = False

    # ── プリンター一覧（非同期取得）─────────────────────────────────────────
    def _init_printer_combo_from_saved(self) -> None:
        """初期表示では保存済みプリンター名だけを表示する（重い列挙はしない）。"""
        saved = (self._settings.printer_name or "").strip()
        if saved:
            self._printer_combo.addItem(saved, saved)
            self._printer_combo.setCurrentIndex(0)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().showEvent(event)
        # 画面を即座に表示した後にプリンター一覧の取得を開始する。
        self._start_printer_list_load()

    def _start_printer_list_load(self) -> None:
        if self._printer_load_started or not self._alive:
            return
        self._printer_load_started = True
        self._printer_load_finished = False
        self._printer_load_start_monotonic = time.monotonic()
        self._set_status("プリンター一覧を読み込み中...")
        voucher_print_service.log_print_settings_event(
            "printer_list_load_started", saved_printer_name=self._settings.printer_name
        )
        self._run_in_background(
            voucher_print_service.list_available_printer_names,
            self._on_printer_list_loaded,
            self._on_printer_list_failed,
            name="printer_list",
        )
        QTimer.singleShot(PRINTER_LIST_LOAD_TIMEOUT_MS, self._on_printer_list_timeout)

    def _printer_elapsed_ms(self) -> int:
        return int((time.monotonic() - self._printer_load_start_monotonic) * 1000)

    def _on_printer_list_loaded(self, result: object) -> None:
        self._printer_load_finished = True
        if not self._alive:
            voucher_print_service.log_print_settings_event(
                "worker_result_ignored_dialog_closed", worker="printer_list"
            )
            return
        try:
            names, default_name = result  # type: ignore[misc]
            names = list(names)
            default_name = str(default_name or "")
        except Exception:
            names, default_name = [], ""
        self._apply_printer_names(names, default_name)
        voucher_print_service.log_print_settings_event(
            "printer_list_load_finished",
            printer_list_count=len(names),
            elapsed_ms=self._printer_elapsed_ms(),
        )
        self._set_status("プリンター一覧を更新しました")

    def _on_printer_list_failed(self, message: str, info: object) -> None:
        self._printer_load_finished = True
        if not self._alive:
            voucher_print_service.log_print_settings_event(
                "worker_result_ignored_dialog_closed", worker="printer_list"
            )
            return
        details = info if isinstance(info, dict) else {}
        voucher_print_service.log_print_settings_event(
            "printer_list_load_error",
            elapsed_ms=self._printer_elapsed_ms(),
            exception_type=details.get("exception_type", ""),
            exception_message=details.get("exception_message", message),
            traceback=details.get("traceback", ""),
        )
        self._set_status("プリンター一覧の取得に失敗しました。保存済み設定はそのまま使用できます。")

    def _on_printer_list_timeout(self) -> None:
        if self._printer_load_finished or not self._alive:
            return
        voucher_print_service.log_print_settings_event(
            "printer_list_load_timeout", elapsed_ms=self._printer_elapsed_ms()
        )
        self._set_status(
            "プリンター一覧の取得に時間がかかっています。保存済み設定はそのまま使用できます。"
        )

    def _apply_printer_names(self, names: "list[str]", default_name: str) -> None:
        current = (self._settings.printer_name or "").strip() or default_name
        selected = str(
            self._printer_combo.currentData() or self._printer_combo.currentText() or ""
        ).strip()
        if selected:
            # 一覧到着前にユーザーが選択・入力していれば尊重する。
            current = selected
        self._printer_combo.blockSignals(True)
        try:
            self._printer_combo.clear()
            for name in names:
                self._printer_combo.addItem(name, name)
            if current and current not in names:
                self._printer_combo.addItem(current, current)
            index = self._printer_combo.findData(current)
            if index >= 0:
                self._printer_combo.setCurrentIndex(index)
        finally:
            self._printer_combo.blockSignals(False)

    # ── バックグラウンド実行の共通処理 ─────────────────────────────────────
    def _run_in_background(self, func, on_result, on_error, *, name: str) -> "tuple[QThread, _CallableWorker]":
        thread = QThread()
        worker = _CallableWorker(func)
        # worker が GC されると thread.started→run が発火しないため、
        # thread へ参照を持たせて thread の寿命に紐付ける。
        thread._worker = worker  # type: ignore[attr-defined]
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_result)
        worker.failed.connect(on_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        _register_background_settings_thread(thread)
        self._bg_threads.add(thread)
        thread.finished.connect(lambda t=thread: self._bg_threads.discard(t))
        thread.start()
        return thread, worker

    def _shutdown_background(self) -> None:
        """ダイアログ破棄時：以後の UI 更新を止める。wait() はしない。"""
        self._alive = False
        for thread in list(self._bg_threads):
            try:
                # quit() のみ。wait() すると UI を固めるため呼ばない。
                thread.quit()
            except Exception:
                pass

    def done(self, result: int) -> None:  # noqa: N802 (Qt命名)
        self._shutdown_background()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        self._shutdown_background()
        super().closeEvent(event)

    def _refresh_sumatra_profile_combo(self, current_name: str = "") -> None:
        if not hasattr(self, "_sumatra_profile_combo"):
            return
        self._sumatra_profile_combo.blockSignals(True)
        try:
            self._sumatra_profile_combo.clear()
            for profile in self._sumatra_profiles:
                self._sumatra_profile_combo.addItem(profile.profile_name, profile.profile_name)
            index = self._sumatra_profile_combo.findData(current_name or self._settings.sumatra_profile_name)
            if index >= 0:
                self._sumatra_profile_combo.setCurrentIndex(index)
        finally:
            self._sumatra_profile_combo.blockSignals(False)

    def _connect_sumatra_detail_controls(self) -> None:
        for widget in (
            self._sumatra_scaling_combo,
            self._sumatra_paper_mode_combo,
            self._sumatra_paper_value_edit,
            self._sumatra_paperkind_edit,
            self._sumatra_bin_edit,
            self._sumatra_extra_options_edit,
        ):
            signal = getattr(widget, "currentIndexChanged", None) or getattr(widget, "textEdited")
            signal.connect(self._update_sumatra_settings_from_controls)
        for widget in (
            self._sumatra_monochrome_check,
            self._sumatra_center_check,
            self._sumatra_auto_rotation_check,
        ):
            widget.toggled.connect(self._update_sumatra_settings_from_controls)
        self._printer_combo.currentIndexChanged.connect(self._update_sumatra_command_summary)
        self._backend_combo.currentIndexChanged.connect(self._update_sumatra_command_summary)
        self._sumatra_path_edit.textChanged.connect(self._update_sumatra_command_summary)

    def _select_combo_data(self, combo: QComboBox, value: object) -> None:
        index = combo.findData(str(value or ""))
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _set_sumatra_detail_controls_from_settings(self, settings_text: str) -> None:
        parsed = parse_sumatra_print_settings(settings_text)
        self._syncing_sumatra_controls = True
        try:
            self._select_combo_data(self._sumatra_scaling_combo, parsed.get("scaling_mode", "noscale"))
            self._select_combo_data(self._sumatra_paper_mode_combo, parsed.get("paper_mode", "auto"))
            self._sumatra_paper_value_edit.setText(str(parsed.get("paper_value", "") or ""))
            paperkind = str(parsed.get("paperkind", "") or self._settings.sumatra_paperkind or "")
            self._sumatra_paperkind_edit.setText(paperkind)
            self._sumatra_monochrome_check.setChecked(bool(parsed.get("monochrome", True)))
            self._sumatra_center_check.setChecked(bool(parsed.get("center", True)))
            self._sumatra_auto_rotation_check.setChecked(bool(parsed.get("auto_rotation", True)))
            self._sumatra_bin_edit.setText(str(parsed.get("bin", "auto") or "auto"))
            self._sumatra_extra_options_edit.setText(str(parsed.get("extra_options", "") or ""))
        finally:
            self._syncing_sumatra_controls = False
        self._update_sumatra_generated_label()
        self._update_sumatra_command_summary()

    def _sumatra_settings_from_controls(self) -> str:
        return build_sumatra_print_settings(
            scaling_mode=self._sumatra_scaling_combo.currentData(),
            monochrome=self._sumatra_monochrome_check.isChecked(),
            paper_mode=self._sumatra_paper_mode_combo.currentData(),
            paperkind=self._sumatra_paperkind_edit.text(),
            paper_value=self._sumatra_paper_value_edit.text(),
            center=self._sumatra_center_check.isChecked(),
            auto_rotation=self._sumatra_auto_rotation_check.isChecked(),
            bin_value=self._sumatra_bin_edit.text(),
            extra_options=self._sumatra_extra_options_edit.text(),
        )

    def _update_sumatra_settings_from_controls(self, *_args) -> None:
        if self._syncing_sumatra_controls:
            return
        settings_text = self._sumatra_settings_from_controls()
        self._sumatra_settings_edit.setText(settings_text)
        custom_index = self._sumatra_preset_combo.findText("カスタム")
        if custom_index >= 0 and self._sumatra_preset_combo.currentData() != settings_text:
            self._sumatra_preset_combo.blockSignals(True)
            self._sumatra_preset_combo.setCurrentIndex(custom_index)
            self._sumatra_preset_combo.blockSignals(False)
        self._update_sumatra_generated_label()
        self._update_sumatra_command_summary()

    def _update_sumatra_generated_label(self) -> None:
        if hasattr(self, "_sumatra_generated_label"):
            self._sumatra_generated_label.setText(self._sumatra_settings_from_controls())

    def _on_sumatra_settings_edited(self, text: str) -> None:
        self._set_sumatra_detail_controls_from_settings(text)
        custom_index = self._sumatra_preset_combo.findText("カスタム")
        if custom_index >= 0:
            self._sumatra_preset_combo.blockSignals(True)
            self._sumatra_preset_combo.setCurrentIndex(custom_index)
            self._sumatra_preset_combo.blockSignals(False)

    def _update_sumatra_command_summary(self, *_args) -> None:
        if not hasattr(self, "_sumatra_command_summary_label"):
            return
        printer = str(self._printer_combo.currentData() or self._printer_combo.currentText() or "").strip()
        path = self._sumatra_path_edit.text().strip() or "SumatraPDF.exe"
        exe_name = Path(path).name or "SumatraPDF.exe"
        settings_text = self._sumatra_settings_edit.text().strip()
        summary = (
            "Backend:\nSumatraPDF\n\n"
            f"Executable:\n{path}\n\n"
            f"Printer:\n{printer}\n\n"
            f"Print settings:\n{settings_text}\n\n"
            "Command preview:\n"
            f'{exe_name} -silent -print-to "<printer>" -print-settings "<settings>" "<pdf>"'
        )
        if hasattr(self, "_adjustment_enabled_check"):
            summary += "\n\n" + print_adjustment_summary_text(self._adjustment_namespace())
        self._sumatra_command_summary_label.setPlainText(summary)

    def _connect_adjustment_controls(self) -> None:
        for spin in (
            self._adjustment_margin_left_spin,
            self._adjustment_margin_right_spin,
            self._adjustment_margin_top_spin,
            self._adjustment_margin_bottom_spin,
            self._adjustment_scale_x_spin,
            self._adjustment_scale_y_spin,
            self._adjustment_offset_x_spin,
            self._adjustment_offset_y_spin,
        ):
            spin.valueChanged.connect(self._update_adjustment_summary)
            spin.valueChanged.connect(self._update_sumatra_command_summary)
        self._adjustment_enabled_check.toggled.connect(self._update_adjustment_summary)
        self._adjustment_enabled_check.toggled.connect(self._update_sumatra_command_summary)

    def _adjustment_namespace(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            print_adjustment_enabled=self._adjustment_enabled_check.isChecked(),
            print_adjustment_margin_left_mm=float(self._adjustment_margin_left_spin.value()),
            print_adjustment_margin_right_mm=float(self._adjustment_margin_right_spin.value()),
            print_adjustment_margin_top_mm=float(self._adjustment_margin_top_spin.value()),
            print_adjustment_margin_bottom_mm=float(self._adjustment_margin_bottom_spin.value()),
            print_adjustment_scale_x_percent=float(self._adjustment_scale_x_spin.value()),
            print_adjustment_scale_y_percent=float(self._adjustment_scale_y_spin.value()),
            print_adjustment_offset_x_mm=float(self._adjustment_offset_x_spin.value()),
            print_adjustment_offset_y_mm=float(self._adjustment_offset_y_spin.value()),
        )

    def _update_adjustment_summary(self, *_args) -> None:
        if not hasattr(self, "_adjustment_summary_label"):
            return
        self._adjustment_summary_label.setText(
            print_adjustment_summary_text(self._adjustment_namespace())
        )

    def _apply_sumatra_preset(self, *_args) -> None:
        value = str(self._sumatra_preset_combo.currentData() or "")
        if self._sumatra_preset_combo.currentText() == "カスタム":
            self._sumatra_settings_edit.setFocus()
            return
        if not value:
            return
        self._sumatra_settings_edit.setText(value)
        self._set_sumatra_detail_controls_from_settings(value)

    def _selected_sumatra_profile(self) -> SumatraPrintProfile | None:
        name = str(self._sumatra_profile_combo.currentData() or self._sumatra_profile_combo.currentText() or "").strip()
        for profile in self._sumatra_profiles:
            if profile.profile_name == name:
                return profile
        return None

    def _load_sumatra_profile(self) -> None:
        profile = self._selected_sumatra_profile()
        if profile is None:
            self._set_status("読み込む設定プロファイルがありません")
            return
        self._sumatra_profile_name_edit.setText(profile.profile_name)
        self._sumatra_profile_memo_edit.setText(profile.memo)
        self._sumatra_settings_edit.setText(profile.print_settings)
        self._set_sumatra_detail_controls_from_settings(profile.print_settings)
        if profile.paperkind:
            self._sumatra_paperkind_edit.setText(profile.paperkind)
        # 印刷補正もプロファイルから復元する。
        self._adjustment_enabled_check.setChecked(bool(profile.adjustment_enabled))
        self._adjustment_margin_left_spin.setValue(float(profile.margin_left_mm))
        self._adjustment_margin_right_spin.setValue(float(profile.margin_right_mm))
        self._adjustment_margin_top_spin.setValue(float(profile.margin_top_mm))
        self._adjustment_margin_bottom_spin.setValue(float(profile.margin_bottom_mm))
        self._adjustment_scale_x_spin.setValue(float(profile.scale_x_percent))
        self._adjustment_scale_y_spin.setValue(float(profile.scale_y_percent))
        self._adjustment_offset_x_spin.setValue(float(profile.offset_x_mm))
        self._adjustment_offset_y_spin.setValue(float(profile.offset_y_mm))
        self._update_adjustment_summary()
        self._set_status(f"SumatraPDF設定を読み込みました: {profile.profile_name}")

    def _save_sumatra_profile(self) -> None:
        name = self._sumatra_profile_name_edit.text().strip() or "ユーザー定義1"
        settings_text = self._sumatra_settings_edit.text().strip()
        now = datetime.now().isoformat(timespec="seconds")
        profile = SumatraPrintProfile(
            profile_name=name,
            print_settings=settings_text,
            paperkind=self._sumatra_paperkind_edit.text().strip(),
            memo=self._sumatra_profile_memo_edit.text().strip(),
            updated_at=now,
            adjustment_enabled=self._adjustment_enabled_check.isChecked(),
            margin_left_mm=float(self._adjustment_margin_left_spin.value()),
            margin_right_mm=float(self._adjustment_margin_right_spin.value()),
            margin_top_mm=float(self._adjustment_margin_top_spin.value()),
            margin_bottom_mm=float(self._adjustment_margin_bottom_spin.value()),
            scale_x_percent=float(self._adjustment_scale_x_spin.value()),
            scale_y_percent=float(self._adjustment_scale_y_spin.value()),
            offset_x_mm=float(self._adjustment_offset_x_spin.value()),
            offset_y_mm=float(self._adjustment_offset_y_spin.value()),
        )
        replaced = False
        profiles: list[SumatraPrintProfile] = []
        for existing in self._sumatra_profiles:
            if existing.profile_name == name:
                profiles.append(profile)
                replaced = True
            else:
                profiles.append(existing)
        if not replaced:
            profiles.append(profile)
        self._sumatra_profiles = profiles
        save_sumatra_print_profiles(self._sumatra_profiles)
        self._refresh_sumatra_profile_combo(name)
        self._set_status(f"SumatraPDF設定を保存しました: {name}")

    def _request_test_print(self) -> None:
        """「テスト印刷」ボタンの入口。単独ダイアログ／embeddedタブどちらからでも動く。"""
        self._on_test_print_clicked()

    def _on_test_print_clicked(self) -> None:
        """現在タブ上の入力値でテスト印刷を実行する（保存前の値を使う・要件2）。

        単独ダイアログでも統合設定ダイアログの印刷設定タブ（embedded）でも同じ経路を通る。
        失敗時は無反応にせず、必ずステータス表示とログを残す（要件3）。
        """
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_clicked",
            embedded=bool(self._embedded),
        )
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_embedded_mode",
            embedded=bool(self._embedded),
        )
        settings = self._collect_current_print_settings()
        self._run_test_print(settings)

    def _collect_current_print_settings(self) -> "VoucherPrinterSettings":
        """画面上の現在値を収集する（保存済み設定ではなく入力中の値・要件2）。"""
        values = self.values()
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_current_values_collected",
            printer_name=values.printer_name,
            print_backend=values.print_backend,
            copies=values.copies,
        )
        return values

    def _find_test_print_host(self):
        """`_enqueue_sumatra_test_print` を持つ VoucherWindow を親チェーンから探す。

        embedded=True のときは QScrollArea/QTabWidget/統合設定ダイアログを経由するため、
        self.parent() だけでは見つからない。親を辿って伝票作成・印刷画面まで探索する。
        """
        node = self.parent()
        seen = 0
        while node is not None and seen < 50:
            if hasattr(node, "_enqueue_sumatra_test_print"):
                return node
            node = node.parent() if hasattr(node, "parent") else None
            seen += 1
        return None

    def _run_test_print(self, settings: "VoucherPrinterSettings") -> None:
        """収集済みの現在値でテスト印刷を実行する（共通処理）。"""
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_started",
            embedded=bool(self._embedded),
        )

        # プリンター未選択は明確にエラー表示（要件3）。
        if not (settings.printer_name or "").strip():
            self._set_status("プリンターを選択してください")
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_failed",
                reason="printer_name_empty",
            )
            return
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_printer_name",
            printer_name=settings.printer_name,
        )

        # SumatraPDF経由なのに実行ファイルが見つからない場合は明確なメッセージ（要件3）。
        if settings.print_backend == PRINT_BACKEND_SUMATRA:
            sumatra_path, _sumatra_source = voucher_print_service.resolve_sumatra_executable(settings)
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_sumatra_path",
                sumatra_path=sumatra_path,
            )
            if not sumatra_path or not Path(sumatra_path).is_file():
                self._set_status(
                    "SumatraPDFが見つかりません。TksToKintoneのセットアップを再実行してください。"
                )
                voucher_print_service.log_print_settings_event(
                    "voucher_print_settings_test_print_failed",
                    reason="sumatra_not_found",
                    sumatra_path=sumatra_path,
                )
                return

        # テスト印刷では QSettings へ保存しない。画面上の現在値（settings）を印刷経路へ
        # 直接渡し、OK/キャンセルの保存判断は統合設定ダイアログ側に委ねる（要件2）。
        host = self._find_test_print_host()
        if host is None:
            self._set_status("テスト印刷は伝票作成・印刷画面から開いた場合に使用できます")
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_failed",
                reason="host_not_found",
            )
            return

        try:
            ok = host._enqueue_sumatra_test_print(settings_override=settings)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            message = f"テスト印刷の追加に失敗しました: {exc}"
            self._set_status(message)
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_exception",
                error=str(exc),
            )
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_failed",
                reason="enqueue_exception",
                error=str(exc),
            )
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_status_message",
                message=message,
            )
            return

        if ok:
            message = "テスト印刷ジョブを追加しました"
            self._set_status(message)
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_status_message",
                message=message,
            )
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_finished",
                embedded=bool(self._embedded),
            )
        else:
            # PDF自動生成失敗など。ホスト側で詳細ログ・ステータスは出しているため、
            # ダイアログ側はユーザーに原因が伝わる文言を表示する（要件3）。
            message = "テスト印刷に失敗しました。ログを確認してください"
            self._set_status(message)
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_failed",
                reason="enqueue_returned_false",
            )
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_status_message",
                message=message,
            )

    def _restore_defaults(self) -> None:
        backend_index = self._backend_combo.findData(PRINT_BACKEND_SUMATRA)
        self._backend_combo.setCurrentIndex(backend_index if backend_index >= 0 else 0)
        self._paper_combo.setCurrentIndex(0)
        orientation_index = self._orientation_combo.findData("landscape")
        self._orientation_combo.setCurrentIndex(orientation_index if orientation_index >= 0 else 0)
        color_index = self._color_combo.findData("grayscale")
        self._color_combo.setCurrentIndex(color_index if color_index >= 0 else 0)
        self._copies_spin.setValue(1)
        self._acrobat_hide_window_check.setChecked(True)
        self._acrobat_close_after_print_check.setChecked(True)
        self._acrobat_close_delay_spin.setValue(10)
        self._acrobat_allow_force_kill_check.setChecked(False)
        self._acrobat_hide_watch_check.setChecked(True)
        self._acrobat_hide_watch_seconds_spin.setValue(10)
        scale_index = self._scale_mode_combo.findData("actual_size")
        self._scale_mode_combo.setCurrentIndex(scale_index if scale_index >= 0 else 0)
        from app.voucher_settings import (
            DEFAULT_SUMATRA_PRINT_SETTINGS,
            DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS,
        )

        self._sumatra_settings_edit.setText(DEFAULT_SUMATRA_PRINT_SETTINGS)
        preset_index = self._sumatra_preset_combo.findData(DEFAULT_SUMATRA_PRINT_SETTINGS)
        self._sumatra_preset_combo.setCurrentIndex(preset_index if preset_index >= 0 else 0)
        self._sumatra_paperkind_edit.setText("")
        self._set_sumatra_detail_controls_from_settings(DEFAULT_SUMATRA_PRINT_SETTINGS)
        self._sumatra_wait_timeout_spin.setValue(DEFAULT_SUMATRA_WAIT_TIMEOUT_SECONDS)
        # 印刷補正は既定で ON・左右4mm/上3mm/下1.5mmに戻す（新規環境の既定値と一致）。
        from app.voucher_settings import (
            DEFAULT_ADJUSTMENT_ENABLED,
            DEFAULT_ADJUSTMENT_MARGIN_LEFT_MM,
            DEFAULT_ADJUSTMENT_MARGIN_RIGHT_MM,
            DEFAULT_ADJUSTMENT_MARGIN_TOP_MM,
            DEFAULT_ADJUSTMENT_MARGIN_BOTTOM_MM,
            DEFAULT_SAVE_PDF_ON_PRINT,
            DEFAULT_SHOW_PDF_CREATED_DIALOG,
            DEFAULT_OPEN_PDF_AFTER_CREATE,
        )

        self._adjustment_enabled_check.setChecked(DEFAULT_ADJUSTMENT_ENABLED)
        self._adjustment_margin_left_spin.setValue(DEFAULT_ADJUSTMENT_MARGIN_LEFT_MM)
        self._adjustment_margin_right_spin.setValue(DEFAULT_ADJUSTMENT_MARGIN_RIGHT_MM)
        self._adjustment_margin_top_spin.setValue(DEFAULT_ADJUSTMENT_MARGIN_TOP_MM)
        self._adjustment_margin_bottom_spin.setValue(DEFAULT_ADJUSTMENT_MARGIN_BOTTOM_MM)
        for spin in (
            self._adjustment_offset_x_spin,
            self._adjustment_offset_y_spin,
        ):
            spin.setValue(0.0)
        self._adjustment_scale_x_spin.setValue(100.0)
        self._adjustment_scale_y_spin.setValue(100.0)
        self._adjustment_save_pdf_check.setChecked(False)
        self._save_pdf_on_print_check.setChecked(DEFAULT_SAVE_PDF_ON_PRINT)
        self._show_pdf_created_dialog_check.setChecked(DEFAULT_SHOW_PDF_CREATED_DIALOG)
        self._open_pdf_after_create_check.setChecked(DEFAULT_OPEN_PDF_AFTER_CREATE)
        self._update_adjustment_summary()

    def _detect_sumatra_path(self) -> None:
        if not self._alive:
            return
        self._sumatra_detect_button.setEnabled(False)
        self._set_status("SumatraPDFを検出中...")
        voucher_print_service.log_print_settings_event("sumatra_auto_detect_started")
        self._run_in_background(
            voucher_print_service.detect_sumatra_pdf_path,
            self._on_sumatra_detected,
            self._on_sumatra_detect_failed,
            name="sumatra_detect",
        )

    def _on_sumatra_detected(self, result: object) -> None:
        if not self._alive:
            voucher_print_service.log_print_settings_event(
                "worker_result_ignored_dialog_closed", worker="sumatra_detect"
            )
            return
        self._sumatra_detect_button.setEnabled(True)
        path = str(result or "")
        voucher_print_service.log_print_settings_event(
            "sumatra_auto_detect_finished", found=bool(path)
        )
        if path:
            self._sumatra_path_edit.setText(path)
            self._set_status("SumatraPDFが見つかりました")
        else:
            self._set_status("SumatraPDFが見つかりませんでした。パスを指定してください。")

    def _on_sumatra_detect_failed(self, message: str, info: object) -> None:
        if not self._alive:
            return
        self._sumatra_detect_button.setEnabled(True)
        details = info if isinstance(info, dict) else {}
        voucher_print_service.log_print_settings_event(
            "sumatra_auto_detect_finished",
            found=False,
            exception_type=details.get("exception_type", ""),
            exception_message=details.get("exception_message", message),
            traceback=details.get("traceback", ""),
        )
        self._set_status("SumatraPDFの検出に失敗しました。パスを指定してください。")

    def _browse_sumatra_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "SumatraPDFを選択",
            self._sumatra_path_edit.text().strip() or "",
            "実行ファイル (*.exe);;すべてのファイル (*)",
        )
        if path:
            self._sumatra_path_edit.setText(path)

    def _detect_acrobat_path(self) -> None:
        if not self._alive:
            return
        self._acrobat_detect_button.setEnabled(False)
        self._set_status("Acrobat Readerを検出中...")
        voucher_print_service.log_print_settings_event("acrobat_auto_detect_started")
        self._run_in_background(
            voucher_print_service.detect_acrobat_reader_path,
            self._on_acrobat_detected,
            self._on_acrobat_detect_failed,
            name="acrobat_detect",
        )

    def _on_acrobat_detected(self, result: object) -> None:
        if not self._alive:
            voucher_print_service.log_print_settings_event(
                "worker_result_ignored_dialog_closed", worker="acrobat_detect"
            )
            return
        self._acrobat_detect_button.setEnabled(True)
        path = str(result or "")
        voucher_print_service.log_print_settings_event(
            "acrobat_auto_detect_finished", found=bool(path)
        )
        if path:
            self._acrobat_path_edit.setText(path)
            self._set_status("Acrobat Readerが見つかりました")
        else:
            self._set_status("Acrobat Readerが見つかりませんでした。パスを指定してください。")

    def _on_acrobat_detect_failed(self, message: str, info: object) -> None:
        if not self._alive:
            return
        self._acrobat_detect_button.setEnabled(True)
        details = info if isinstance(info, dict) else {}
        voucher_print_service.log_print_settings_event(
            "acrobat_auto_detect_finished",
            found=False,
            exception_type=details.get("exception_type", ""),
            exception_message=details.get("exception_message", message),
            traceback=details.get("traceback", ""),
        )
        self._set_status("Acrobat Readerの検出に失敗しました。パスを指定してください。")

    def _browse_acrobat_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Acrobat Readerを選択",
            self._acrobat_path_edit.text().strip() or "",
            "実行ファイル (*.exe);;すべてのファイル (*)",
        )
        if path:
            self._acrobat_path_edit.setText(path)

    def values(self) -> VoucherPrinterSettings:
        return VoucherPrinterSettings(
            printer_name=str(self._printer_combo.currentData() or self._printer_combo.currentText() or "").strip(),
            paper_size=str(self._paper_combo.currentData() or "B5"),
            orientation=str(self._orientation_combo.currentData() or "landscape"),
            color_mode=str(self._color_combo.currentData() or "grayscale"),
            copies=int(self._copies_spin.value()),
            scale_mode=str(self._scale_mode_combo.currentData() or "actual_size"),
            print_backend=str(self._backend_combo.currentData() or PRINT_BACKEND_SUMATRA),
            acrobat_path=self._acrobat_path_edit.text().strip(),
            acrobat_hide_window=self._acrobat_hide_window_check.isChecked(),
            acrobat_close_after_print=self._acrobat_close_after_print_check.isChecked(),
            acrobat_close_delay_seconds=int(self._acrobat_close_delay_spin.value()),
            acrobat_allow_force_kill=self._acrobat_allow_force_kill_check.isChecked(),
            acrobat_hide_watch_enabled=self._acrobat_hide_watch_check.isChecked(),
            acrobat_hide_watch_seconds=int(self._acrobat_hide_watch_seconds_spin.value()),
            sumatra_path=self._sumatra_path_edit.text().strip(),
            sumatra_print_settings=self._sumatra_settings_edit.text().strip(),
            sumatra_paperkind=self._sumatra_paperkind_edit.text().strip(),
            sumatra_profile_name=self._sumatra_profile_name_edit.text().strip(),
            sumatra_scaling_mode=str(self._sumatra_scaling_combo.currentData() or "noscale"),
            sumatra_paper_mode=str(self._sumatra_paper_mode_combo.currentData() or "auto"),
            sumatra_paper_value=self._sumatra_paper_value_edit.text().strip(),
            sumatra_monochrome=self._sumatra_monochrome_check.isChecked(),
            sumatra_center=self._sumatra_center_check.isChecked(),
            sumatra_auto_rotation=self._sumatra_auto_rotation_check.isChecked(),
            sumatra_bin=self._sumatra_bin_edit.text().strip(),
            sumatra_extra_options=self._sumatra_extra_options_edit.text().strip(),
            sumatra_wait_timeout_seconds=int(self._sumatra_wait_timeout_spin.value()),
            sumatra_allow_force_kill=self._settings.sumatra_allow_force_kill,
            print_adjustment_enabled=self._adjustment_enabled_check.isChecked(),
            print_adjustment_margin_left_mm=float(self._adjustment_margin_left_spin.value()),
            print_adjustment_margin_right_mm=float(self._adjustment_margin_right_spin.value()),
            print_adjustment_margin_top_mm=float(self._adjustment_margin_top_spin.value()),
            print_adjustment_margin_bottom_mm=float(self._adjustment_margin_bottom_spin.value()),
            print_adjustment_scale_x_percent=float(self._adjustment_scale_x_spin.value()),
            print_adjustment_scale_y_percent=float(self._adjustment_scale_y_spin.value()),
            print_adjustment_offset_x_mm=float(self._adjustment_offset_x_spin.value()),
            print_adjustment_offset_y_mm=float(self._adjustment_offset_y_spin.value()),
            print_adjustment_save_pdf=self._adjustment_save_pdf_check.isChecked(),
            save_pdf_on_print=self._save_pdf_on_print_check.isChecked(),
            show_pdf_created_dialog=self._show_pdf_created_dialog_check.isChecked(),
            open_pdf_after_create=self._open_pdf_after_create_check.isChecked(),
        )


class _ColumnVisibilityWidget(QWidget):
    """表示設定タブ: 一覧テーブルの列表示/非表示を選ぶチェックボックス群（要件3）。

    非表示不可(hideable=False)列は disabled checked にして常時表示を明示する。
    「すべて表示」「初期値に戻す」で hideable 列を一括操作できる。
    """

    def __init__(self, visible_columns: dict[str, bool], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checks: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)

        group = QGroupBox("列表示")
        group_layout = QVBoxLayout(group)
        for spec in VOUCHER_COLUMN_SPECS:
            cb = QCheckBox(spec.label)
            if not spec.hideable:
                # 必須列・右端の操作ボタン列はOFFにできない（要件3）。
                cb.setChecked(True)
                cb.setEnabled(False)
                cb.setToolTip("この列は常に表示します（非表示にできません）。")
            else:
                cb.setChecked(bool(visible_columns.get(spec.key, spec.default_visible)))
            self._checks[spec.key] = cb
            group_layout.addWidget(cb)
        layout.addWidget(group)

        button_row = QHBoxLayout()
        self._show_all_button = QPushButton("すべて表示")
        self._reset_button = QPushButton("初期値に戻す")
        self._show_all_button.clicked.connect(self._on_show_all)
        self._reset_button.clicked.connect(self._on_reset_default)
        button_row.addWidget(self._show_all_button)
        button_row.addWidget(self._reset_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        layout.addStretch(1)

    def _on_show_all(self) -> None:
        for spec in VOUCHER_COLUMN_SPECS:
            cb = self._checks.get(spec.key)
            if cb is not None and spec.hideable:
                cb.setChecked(True)

    def _on_reset_default(self) -> None:
        for spec in VOUCHER_COLUMN_SPECS:
            cb = self._checks.get(spec.key)
            if cb is not None and spec.hideable:
                cb.setChecked(spec.default_visible)

    def visible_columns(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for spec in VOUCHER_COLUMN_SPECS:
            cb = self._checks.get(spec.key)
            if not spec.hideable:
                result[spec.key] = True
            else:
                result[spec.key] = bool(cb.isChecked()) if cb is not None else spec.default_visible
        return result


class CombinedVoucherSettingsDialog(QDialog):
    """表示設定・印刷設定・伝票設定を1つにまとめた統合設定ダイアログ（要件4）。

    3タブ（表示設定/印刷設定/伝票設定）を QTabWidget に並べ、各タブは QScrollArea に
    入れて表示倍率125%以上でも見切れないようにする。既存の印刷設定/伝票設定の入力UIは
    embedded=True の既存ダイアログをそのまま埋め込んで再利用し、保存値・機能を維持する。
    保存は親（VoucherWindow）が各アクセサから値を取り出して既存の保存関数へ渡す。
    """

    # 初期表示タブ識別子 → タブインデックス。
    TAB_INDEX_BY_NAME = {"display": 0, "printer": 1, "voucher": 2, "processing": 3}

    def __init__(
        self,
        *,
        visible_columns: dict[str, bool],
        selected_ids: set[str],
        retention_days: int,
        record_retention_days: int,
        finish_date_none: bool,
        ampm_default: str,
        price_display_mode: str = PRICE_DISPLAY_CONDITIONAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("伝票設定")

        self.display_tab = _ColumnVisibilityWidget(visible_columns)
        self.printer_tab = VoucherPrinterSettingsDialog(embedded=True)
        self.voucher_tab = VoucherPrintSettingsDialog(
            selected_ids=set(selected_ids),
            retention_days=retention_days,
            finish_date_none=finish_date_none,
            ampm_default=ampm_default,
            price_display_mode=price_display_mode,
            embedded=True,
        )
        self.voucher_tab.set_record_retention_days(record_retention_days)
        self.processing_tab = ProcessingDisplayNamesWidget()

        self._tabs = QTabWidget()
        self._tabs.addTab(self._scrollable(self.display_tab), "表示設定")
        self._tabs.addTab(self._scrollable(self.printer_tab), "印刷設定")
        self._tabs.addTab(self._scrollable(self.voucher_tab), "伝票設定")
        self._tabs.addTab(self._scrollable(self.processing_tab), "伝票加工名設定")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addWidget(buttons)

        # 125%以上でも見切れないよう、現在画面に収まる初期サイズへ丸める（要件2/4）。
        self.setSizeGripEnabled(True)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(900, int(available.width() * 0.9))
            height = min(840, int(available.height() * 0.9))
            self.setMinimumSize(min(600, width), min(400, height))
            self.resize(width, height)

    def select_tab(self, name: str) -> None:
        """初期表示タブを選ぶ（display/printer/voucher）。不明値は表示設定タブ。"""
        index = self.TAB_INDEX_BY_NAME.get(str(name), 0)
        self._tabs.setCurrentIndex(index)

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def done(self, result: int) -> None:  # noqa: N802 (Qt命名)
        # 埋め込んだ印刷設定タブのバックグラウンドスレッド（プリンター一覧取得）を確実に止める。
        try:
            self.printer_tab._shutdown_background()
        except Exception:  # noqa: BLE001
            pass
        super().done(result)

    def accept(self) -> None:
        for row, (key, _default) in enumerate(PROCESSING_DEFINITIONS, 1):
            try:
                validate_processing_display_name(
                    self.processing_tab.values().get(key, ""))
            except ValueError as exc:
                QMessageBox.warning(
                    self, "伝票加工名設定", f"{row}行目: {exc}")
                return
        super().accept()

    # ── アクセサ（VoucherWindow が保存に使う）─────────────────────────────────
    def visible_columns(self) -> dict[str, bool]:
        return self.display_tab.visible_columns()

    def printer_values(self) -> VoucherPrinterSettings:
        return self.printer_tab.values()

    def selected_ids(self) -> set[str]:
        return self.voucher_tab.selected_ids()

    def retention_days(self) -> int:
        return self.voucher_tab.retention_days()

    def record_retention_days(self) -> int:
        return self.voucher_tab.record_retention_days()

    def finish_date_none(self) -> bool:
        return self.voucher_tab.finish_date_none()

    def ampm_default(self) -> str:
        return self.voucher_tab.ampm_default()

    def price_display_mode(self) -> str:
        return self.voucher_tab.price_display_mode()

    def apply_to_current_list_requested(self) -> bool:
        """伝票設定を現在の一覧へ反映するか（伝票設定タブのチェック状態・要件4）。"""
        return self.voucher_tab.apply_to_current_list_requested()

    def processing_display_names(self) -> dict[str, str]:
        return self.processing_tab.values()


class ProcessingDisplayNamesWidget(QWidget):
    """内部キーを変えず、伝票へ描画する名称だけを編集する設定欄。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edits: dict[str, QLineEdit] = {}
        current = load_processing_display_names()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("左の行番号は固定です。右の伝票表示名だけ変更できます。"))
        grid = QGridLayout()
        grid.addWidget(QLabel("行"), 0, 0)
        grid.addWidget(QLabel("伝票表示名"), 0, 1)
        for row, (key, default) in enumerate(PROCESSING_DEFINITIONS, 1):
            grid.addWidget(QLabel(f"{row}行目"), row, 0)
            edit = QLineEdit(current[key])
            edit.setMaxLength(12)
            self._edits[key] = edit
            grid.addWidget(edit, row, 1)
        layout.addLayout(grid)
        reset = QPushButton("既定値に戻す")
        reset.clicked.connect(self.reset_defaults)
        layout.addWidget(reset)
        layout.addStretch(1)

    def reset_defaults(self) -> None:
        for key, default in PROCESSING_DEFINITIONS:
            self._edits[key].setText(default)

    def values(self) -> dict[str, str]:
        return {key: edit.text() for key, edit in self._edits.items()}


class VoucherWindow(QMainWindow):
    """伝票作成・印刷画面（受注一覧形式）。"""

    back_requested = Signal()

    def _log_voucher_event(self, event: str, **payload: object) -> None:
        """伝票作成・印刷画面のイベントを標準ロガーへ記録する（失敗してもUIを落とさない）。"""
        try:
            logging.getLogger("tks_to_kintone_app").info("%s %s", event, payload)
        except Exception:  # noqa: BLE001
            pass

    def _timed_step(self, event: str, func):
        """主要ステップの所要時間(ms)を計測しログへ出す。

        `{event}_started` / `{event}_finished` / `{event}_elapsed_ms` を出力し、
        300ms以上は voucher_window_slow_step_detected、1000ms以上は warning を出す。
        Windows実機ログで最も遅いステップを特定できる粒度にする（要件2）。
        例外は握りつぶさず呼び出し元へ伝播する。
        """
        start = time.perf_counter()
        self._log_voucher_event(f"{event}_started")
        try:
            return func()
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self._log_voucher_event(f"{event}_finished", elapsed_ms=elapsed_ms)
            self._log_voucher_event(f"{event}_elapsed_ms", elapsed_ms=elapsed_ms)
            if elapsed_ms >= 300:
                self._log_voucher_event(
                    "voucher_window_slow_step_detected", step=event, elapsed_ms=elapsed_ms
                )
            if elapsed_ms >= 1000:
                try:
                    logging.getLogger("tks_to_kintone_app").warning(
                        "voucher_window_slow_step_detected %s",
                        {"step": event, "elapsed_ms": elapsed_ms},
                    )
                except Exception:  # noqa: BLE001
                    pass

    def __init__(
        self,
        olap_login_id: str = "",
        olap_password: str = "",
        kintone_window_provider=None,
    ) -> None:
        _main_window_started = time.perf_counter()
        super().__init__()
        self._perf_list_started = _main_window_started
        _perf_voucher_list("main_window_generation", _main_window_started)
        self._log_voucher_event("voucher_window_init_started")
        _init_start = time.perf_counter()
        # 起動時には設定ダイアログ・プリンタ一覧・指図書編集画面・画像処理系の重い処理を
        # 一切行わない（要件3）。これらは各ボタン押下時に遅延生成/遅延importする。
        self._log_voucher_event("voucher_window_combined_settings_lazy_skipped_on_startup")
        self._log_voucher_event("voucher_window_printer_settings_lazy_skipped_on_startup")
        self._log_voucher_event("voucher_window_heavy_import_deferred")
        # VoucherWindow.__init__ 内ではネットワーク処理・プリンタ列挙・重いimportを一切
        # 行わない（要件2）。OLAP認証はランチャーが画面を開く前に実施済みで、
        # 実データ取得時の再認証は VoucherOlapService.login_if_needed が担う。
        self._log_voucher_event("voucher_window_olap_auth_deferred")
        self._log_voucher_event("voucher_window_kintone_check_deferred")
        self._log_voucher_event("voucher_window_printer_enum_deferred")
        self._log_voucher_event("voucher_window_pdf_import_deferred")
        self._log_voucher_event("voucher_window_edit_window_import_deferred")
        self.olap_login_id = olap_login_id
        self.olap_password = olap_password
        # Kintone登録処理画面（MainWindow）の現在インスタンスを返すコールバック。
        # ランチャーが保持する _main_window を都度参照するため、開閉のたびに
        # 最新の状態（None=未起動）を取得できる。未指定なら常に未起動扱い。
        self._kintone_window_provider = kintone_window_provider

        self.setWindowTitle(f"伝票作成・印刷 — TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        clamp_window_to_available_geometry(
            self,
            desired_width=1680,
            desired_height=760,
            min_width=1100,
            min_height=620,
        )

        self._rows: list[_RowWidgets] = []
        self._new_input_row: _RowWidgets | None = None
        self._last_pdf_bytes: bytes = b""
        self._last_pdf_path: str = ""
        self._last_pdf_job_name: str = ""
        # 新規入力行の受注No欄・取得ボタンは、再描画で参照がズレないよう明示保持する。
        # 常にこの参照から現在表示中の値を読む（実機の再描画対策）。
        self._new_order_no_edit: QLineEdit | None = None
        self._new_fetch_button: QPushButton | None = None
        # 新規入力行取得の診断ログ（デバッグ表示ON時のみ生成）。
        self._new_row_fetch_log_path: Path | None = None
        self._voucher_no_blocked_order_nos: set[str] = set()
        self._registration_status_by_order: dict[str, str] = {}
        self._restoring_records = False
        # bulk復元中は _add_row の行単位の重い後処理（列幅再計算・列表示反映・
        # フィルタ・スタイル再適用）を抑制し、復元完了後に1回だけ実行する（要件1・2）。
        self._bulk_restoring_saved_rows = False
        # 列表示反映の呼び出し回数を計測し、多発（実機で144回）を検出できるようにする。
        self._column_visibility_apply_count = 0
        self._last_applied_visible_columns: dict[str, bool] | None = None
        self._print_in_progress = False
        # 受注No入力欄 Enter からのOLAP取得の二重起動抑制フラグ（要件3）。
        self._fetch_enter_in_progress = False
        self._print_workers: set[object] = set()
        self._editor_load_generation = 0
        self._editor_workers: dict[int, tuple[QThread, _VoucherEditorDataWorker]] = {}
        self._edit_render_context_by_order: dict[str, dict[str, object]] = {}
        self._range_dialog = None
        self._print_disabled_widget_states: dict[QWidget, bool] = {}

        # 新規行の「印刷する伝票」初期チェック・列表示など、画面表示に必要な軽い設定のみ
        # 起動時に読み込む（要件3）。期限切れキャッシュ削除など重い処理は表示後に遅延実行する。
        def _load_settings() -> None:
            self._default_print_types = set(load_default_print_types())
            self._default_finish_date_none = load_default_finish_date_none()
            self._default_ampm = load_default_ampm()
            # 一覧テーブルの列表示/非表示（表示設定・要件3）。設定から復元する。
            self._visible_columns = resolve_visible_columns(load_visible_column_keys())

        self._timed_step("voucher_window_load_settings", _load_settings)
        _perf_voucher_list("settings_loaded", _main_window_started)
        self._log_voucher_event(
            "voucher_window_column_visibility_settings_loaded",
            visible_columns=self._visible_columns,
        )

        # 上部の行操作ボタン。設定入口はヘッダー上「表示設定」1つに集約する（要件4）。
        # 旧「印刷設定」「伝票設定」ボタンはヘッダーから撤去した（関数は互換維持で残置し、
        # 呼ばれた場合は統合設定ダイアログの該当タブを開く）。
        # 表示設定・印刷設定・伝票設定を1つにまとめた統合設定を開くボタン（要件2/4）。
        self._display_settings_button = QPushButton("設定")
        self._display_settings_button.setToolTip("表示設定・印刷設定・伝票設定")
        self._display_settings_button.setAccessibleName("設定")
        self._log_voucher_event("voucher_window_display_settings_button_created")
        self._select_pdf_button = QPushButton("選択PDF作成")
        self._select_preview_button = QPushButton("選択プレビュー")
        self._select_print_button = QPushButton("選択印刷")
        self._remove_row_button = QPushButton("選択削除")
        self._remove_row_button.setToolTip(
            "選択した行を一覧から削除します。Kintoneや出力済みPDFには影響しません。"
        )
        self._select_order_no_button = QPushButton("選択受注No追加")
        self._order_search_edit = QLineEdit()
        self._order_search_edit.setPlaceholderText("受注No")
        self._status_filter = QComboBox()
        self._status_filter.addItems([KINTONE_STATUS_UNREGISTERED, KINTONE_STATUS_COMPLETED, "すべて"])
        self._status_filter.setCurrentText("すべて")

        # 受注一覧テーブル
        self._table = QTableWidget(0, len(COLUMN_LABELS))
        self._table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._table.setVerticalScrollBarPolicy(
            self._table.verticalScrollBarPolicy().ScrollBarAsNeeded
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_section_clicked)
        # 列区切り線をライト/ダーク両モードで見えるようにする（要件1-2）。
        self._table.setShowGrid(True)
        self._table.setStyleSheet(VOUCHER_TABLE_STYLE)

        # PDF出力先（共通設定）
        self._pdf_output_dir = QLineEdit()
        self._pdf_output_dir.setPlaceholderText("PDF出力先フォルダ")
        self._pdf_output_dir.setToolTip("PDF作成ボタンで保存するフォルダを指定してください。")
        self._browse_output_button = QPushButton("参照")
        self._load_pdf_output_dir()

        self._timed_step("voucher_window_setup_ui", self._build_layout)

        self._display_settings_button.clicked.connect(self._on_display_settings)
        self._select_pdf_button.clicked.connect(self._on_select_pdf)
        self._select_preview_button.clicked.connect(self._on_select_preview)
        self._select_print_button.clicked.connect(self._on_select_print)
        self._remove_row_button.clicked.connect(self._on_remove_selected)
        self._select_order_no_button.clicked.connect(self._on_select_order_no_add)
        self._browse_output_button.clicked.connect(self._browse_pdf_output_dir)
        self._order_search_edit.textChanged.connect(self._apply_filters)
        self._status_filter.currentTextChanged.connect(self._apply_filters)

        # 一覧の先頭には常に入力専用の新規行を表示する。保存済みデータは通常行として後続に復元する。
        self._ensure_new_input_row()
        # 期限切れ判定は保存レコードworkerの正規化中に行う。起動前のファイル走査はしない。
        self._log_voucher_event("voucher_window_saved_record_prune_deferred_to_worker")
        self._update_selection_state()
        # 列表示/非表示は初期化時に1回だけ反映する（この時点は新規入力行のみ）。以降は
        # 設定変更時・bulk復元完了時のみ再反映する（要件2）。所要時間を計測する。
        self._timed_step("voucher_window_apply_column_visibility", self._apply_column_visibility)

        # 保存済み一覧の復元は重い（実機で45件約5.6秒）。画面を先に表示するため、
        # __init__ では同期実行せず、show 後に singleShot(0) でチャンク復元する（要件1）。
        self._saved_rows_restored = False
        self._deferred_restore_active = False
        self._deferred_restore_finished = False
        self._deferred_restore_records: list[dict] = []
        self._deferred_restore_total = 0
        self._deferred_restore_done = 0
        # 保存済み一覧のファイル読み込み・正規化を行うワーカースレッドの管理状態（要件2・4）。
        # ウィンドウclose時に結果を破棄し、参照を安全に解放するために保持する。
        self._alive = True
        self._saved_rows_thread: "QThread | None" = None
        self._saved_rows_worker: "_SavedRecordsLoadWorker | None" = None
        self._deferred_restore_worker_active = False
        self._deferred_restore_first_batch_done = False
        self._saved_rows_worker_cancelled = False
        self._startup_perf = time.perf_counter()
        self._log_voucher_event("voucher_window_ready_before_saved_rows_restore")
        self._log_voucher_event("voucher_window_saved_rows_restore_deferred_to_after_show")
        QTimer.singleShot(0, self._restore_saved_records_after_show)

        # 期限切れOLAPキャッシュ（ディスク走査）の削除は起動を軽くするため、画面表示後へ
        # QTimer.singleShot(0) で遅延する（要件2）。表示行には影響しない処理のため安全に遅延できる。
        self._log_voucher_event("voucher_window_cache_cleanup_deferred")
        QTimer.singleShot(0, self._deferred_cleanup_expired_olap_cache)

        elapsed_ms = int((time.perf_counter() - _init_start) * 1000)
        # 遅延復元までの経過（空画面が表示されていた時間）計測用の基準時刻。
        self._init_finished_perf = time.perf_counter()
        self._log_voucher_event("voucher_window_init_finished")
        self._log_voucher_event("voucher_window_init_elapsed_ms", elapsed_ms=elapsed_ms)
        _perf_voucher_list("window_frame_ready", _main_window_started)
        if elapsed_ms >= 300:
            self._log_voucher_event(
                "voucher_window_slow_step_detected", step="voucher_window_init", elapsed_ms=elapsed_ms
            )

    # ── レイアウト ────────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("受注一覧"))
        top_row.addSpacing(12)
        top_row.addWidget(QLabel("受注No検索:"))
        top_row.addWidget(self._order_search_edit)
        top_row.addWidget(QLabel("登録状態:"))
        top_row.addWidget(self._status_filter)
        top_row.addStretch(1)
        top_row.addWidget(self._select_pdf_button)
        top_row.addWidget(self._select_preview_button)
        top_row.addWidget(self._select_print_button)
        top_row.addWidget(self._remove_row_button)
        top_row.addWidget(self._select_order_no_button)
        top_row.addWidget(self._display_settings_button)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("PDF出力先:"))
        output_row.addWidget(self._pdf_output_dir, 1)
        output_row.addWidget(self._browse_output_button)

        # 画面下部のステータス行。新規入力行「取得」押下時の処理状態を必ずここへ表示する。
        # 取得ボタンを押してもここが変わらなければ clicked が発火していないと判断できる。
        status_row = QHBoxLayout()
        self._new_row_status_label = QLabel("")
        self._new_row_status_label.setObjectName("newRowStatusLabel")
        self._new_row_status_label.setToolTip("新規入力行「取得」の処理状態を表示します。")
        status_row.addWidget(self._new_row_status_label, 1)

        # 保存済み一覧の遅延復元中に表示する進捗ラベル＋プログレスバー（要件1・2）。
        # 初期は非表示。復元開始で表示し、件数（done / total）を更新、完了で非表示に戻す。
        self._saved_rows_progress_label = QLabel("")
        self._saved_rows_progress_label.setObjectName("savedRowsProgressLabel")
        self._saved_rows_progress_label.setToolTip("保存済み一覧を読み込み中です。")
        self._saved_rows_progress_label.setVisible(False)
        status_row.addWidget(self._saved_rows_progress_label)
        self._saved_rows_progress = QProgressBar()
        self._saved_rows_progress.setObjectName("savedRowsProgress")
        self._saved_rows_progress.setFixedWidth(160)
        self._saved_rows_progress.setRange(0, 0)
        self._saved_rows_progress.setVisible(False)
        self._saved_rows_progress.setToolTip("保存済み一覧を読み込み中です。")
        status_row.addWidget(self._saved_rows_progress)
        self._log_voucher_event("voucher_window_saved_rows_progress_created")
        # 処理中表示用の不定進捗バー（OLAP取得・PDF作成・印刷・登録など）。
        # UI全体を無効化せず、処理中であることだけを小さく示す。
        self._busy_progress = QProgressBar()
        self._busy_progress.setObjectName("busyProgress")
        self._busy_progress.setTextVisible(False)
        self._busy_progress.setFixedWidth(140)
        self._busy_progress.setRange(0, 0)  # 不定進捗（busy表示）
        self._busy_progress.setVisible(False)
        self._busy_progress.setToolTip("処理中です。")
        status_row.addWidget(self._busy_progress)
        # 実機で起動中のEXEに今回の修正が入っているかを確認するためのバージョン表示。
        # デバッグ表示ON時のみ表示する。
        self._new_row_version_label = QLabel(
            f"新規行処理: {NEW_ROW_FETCH_VERSION}  /  NewRowFetch: {NEW_ROW_FETCH_HANDLER}"
        )
        self._new_row_version_label.setObjectName("newRowVersionLabel")
        self._new_row_version_label.setStyleSheet("color: #6b7280;")
        self._new_row_version_label.setVisible(self._new_row_fetch_debug_enabled())
        status_row.addWidget(self._new_row_version_label)

        top_widget = QWidget()
        top_widget.setLayout(top_row)
        top_widget.setMinimumWidth(top_widget.sizeHint().width())
        top_scroll = QScrollArea()
        top_scroll.setObjectName("voucherTopControlsScrollArea")
        top_scroll.setWidgetResizable(False)
        top_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        top_scroll.setWidget(top_widget)
        top_scroll.setMinimumHeight(top_widget.sizeHint().height() + 18)
        logging.getLogger("tks_to_kintone_app").info(
            "app_window_scroll_area_enabled %s",
            {"class": type(self).__name__, "area": "top_controls"},
        )

        root = QVBoxLayout()
        root.addWidget(top_scroll)
        root.addWidget(self._table, 1)
        root.addLayout(output_row)
        root.addLayout(status_row)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    # ── 行の生成 ─────────────────────────────────────────────────────────────
    def _add_row(self, *, new_input_row: bool = False) -> _RowWidgets:
        rw = _RowWidgets()
        rw.is_new_input_row = new_input_row
        row_index = self._table.rowCount()
        rw.table_row_index = row_index
        self._table.insertRow(row_index)

        # 選択チェックボックス（中央寄せ）
        rw.select_check = QCheckBox()
        rw.select_check.stateChanged.connect(self._on_row_selection_changed)
        select_holder = QWidget()
        select_layout = QHBoxLayout(select_holder)
        select_layout.setContentsMargins(4, 4, 4, 4)
        select_layout.addStretch(1)
        select_layout.addWidget(rw.select_check)
        select_layout.addStretch(1)
        self._table.setCellWidget(row_index, COL_SELECT, select_holder)

        # 受注No
        rw.order_input = QLineEdit()
        rw.order_input.setPlaceholderText("例: 5218869")
        if new_input_row:
            rw.order_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            rw.order_input.customContextMenuRequested.connect(
                lambda pos, edit=rw.order_input: self._show_order_range_context_menu(edit, pos)
            )
        self._table.setCellWidget(row_index, COL_ORDER_NO, self._wrap(rw.order_input))

        # OLAP（未取得行は取得、取得済み行は同じ受注Noで更新）
        rw.refetch_button = QPushButton("取得")
        rw.refetch_button.setProperty("buttonRole", "olapFetch")
        rw.refetch_button.setToolTip("受注NoでOLAPデータを取得します。取得済み行は同じ受注Noで更新します。")
        if new_input_row:
            # 新規入力行の取得ボタンは lambda で rw を束ねず、専用スロットへ直接接続する。
            # 処理側は常に self._new_input_row / self._new_order_no_edit を読むため、
            # 実機の再描画で古い row widget や QLineEdit を参照する事故が起きない。
            self._log_new_row_event(
                "new_fetch_button_created",
                button_object_id=self._obj_id(rw.refetch_button),
                edit_object_id=self._obj_id(rw.order_input),
            )
            rw.refetch_button.clicked.connect(self._on_fetch_new_input_row)
            self._log_new_row_event(
                "new_fetch_button_connected",
                button_object_id=self._obj_id(rw.refetch_button),
                edit_object_id=self._obj_id(rw.order_input),
            )
        else:
            rw.refetch_button.clicked.connect(lambda _=False, r=rw: self._on_refetch_row(r))
        self._table.setCellWidget(row_index, COL_REFETCH, self._wrap(rw.refetch_button))

        # 受注No入力欄で Enter を押したら「取得」ボタンと同じOLAP取得を実行する（要件3）。
        # 取得ボタン押下と完全に同じ処理へ入り、多重取得・取得中の二重起動は抑制する。
        rw.order_input.returnPressed.connect(
            lambda r=rw: self._on_order_input_return_pressed(r)
        )

        # 仕上日（日付選択 ＋「なし」チェック）。なしの場合は伝票に印字しない（要件1）。
        finish_widget = QWidget()
        finish_layout = QVBoxLayout(finish_widget)
        finish_layout.setContentsMargins(4, 4, 4, 4)
        finish_layout.setSpacing(2)
        rw.date_edit = QDateEdit()
        rw.date_edit.setCalendarPopup(True)
        rw.date_edit.setDisplayFormat("yyyy/MM/dd")
        rw.date_edit.setDate(QDate.currentDate())
        rw.finish_none_check = QCheckBox("なし")
        rw.finish_none_check.setToolTip("チェックすると仕上日を「なし」にし、伝票の仕上日欄を空白にします。")
        rw.finish_none_check.toggled.connect(
            lambda checked, r=rw: r.date_edit.setDisabled(checked)
        )
        rw.finish_none_check.setChecked(normalize_finish_date_none(self._default_finish_date_none))
        finish_layout.addWidget(rw.date_edit)
        finish_layout.addWidget(rw.finish_none_check)
        self._table.setCellWidget(row_index, COL_FINISH_DATE, finish_widget)

        # AM・PM（縦3行のラジオボタン: なし/AM/PM。行ごとに排他選択）
        ampm_widget = QWidget()
        ampm_layout = QVBoxLayout(ampm_widget)
        ampm_layout.setContentsMargins(4, 4, 4, 4)
        ampm_layout.setSpacing(2)
        rw.ampm_none = QRadioButton("なし")
        rw.ampm_am = QRadioButton("AM")
        rw.ampm_pm = QRadioButton("PM")
        rw.ampm_group = QButtonGroup(ampm_widget)
        rw.ampm_group.setExclusive(True)
        rw.ampm_group.addButton(rw.ampm_none)
        rw.ampm_group.addButton(rw.ampm_am)
        rw.ampm_group.addButton(rw.ampm_pm)
        if self._default_ampm == "none":
            rw.ampm_none.setChecked(True)
        elif self._default_ampm == "pm":
            rw.ampm_pm.setChecked(True)
        else:
            rw.ampm_am.setChecked(True)
        ampm_layout.addWidget(rw.ampm_none)
        ampm_layout.addWidget(rw.ampm_am)
        ampm_layout.addWidget(rw.ampm_pm)
        self._table.setCellWidget(row_index, COL_AMPM, ampm_widget)

        # 加工名チェックボックス（3列表示・ラベル見切れ防止: 要件1）。
        process_widget = QWidget()
        process_grid = QGridLayout(process_widget)
        process_grid.setContentsMargins(4, 4, 4, 4)
        process_grid.setHorizontalSpacing(8)
        process_grid.setVerticalSpacing(2)
        processing_display_names = load_processing_display_names()
        for i, name in enumerate(PROCESS_NAMES):
            cb = QCheckBox(resolve_processing_display_name(name, processing_display_names))
            # 加工名チェックは初期値すべてOFF（行追加時・初期空行とも）。
            cb.setChecked(False)
            rw.process_checks[name] = cb
            process_grid.addWidget(cb, i // PROCESS_COLUMNS, i % PROCESS_COLUMNS)
        self._table.setCellWidget(row_index, COL_PROCESS, process_widget)

        # 印刷する伝票チェックボックス（2列表示・ラベル見切れ防止: 要件1）。
        voucher_widget = QWidget()
        voucher_grid = QGridLayout(voucher_widget)
        voucher_grid.setContentsMargins(4, 4, 4, 4)
        voucher_grid.setHorizontalSpacing(8)
        voucher_grid.setVerticalSpacing(2)
        for i, (vid, vname) in enumerate(VOUCHER_TYPES):
            cb = QCheckBox(vname)
            # 印刷する伝票の初期チェックは「印刷する伝票設定」の保存値に従う。
            cb.setChecked(vid in self._default_print_types)
            rw.voucher_checks[vid] = cb
            voucher_grid.addWidget(cb, i // VOUCHER_COLUMNS, i % VOUCHER_COLUMNS)
        self._table.setCellWidget(row_index, COL_VOUCHER, voucher_widget)

        # 指図書編集（指図書(1)プレビューを全画面で編集）
        rw.edit_button = QPushButton("指図書編集")
        rw.edit_button.clicked.connect(lambda _=False, r=rw: self._on_edit_order_sheet(r))
        self._table.setCellWidget(row_index, COL_EDIT, self._wrap(rw.edit_button))

        # PDF作成（行単位）
        rw.pdf_button = QPushButton("PDF作成")
        rw.pdf_button.clicked.connect(lambda _=False, r=rw: self._on_pdf(r))
        self._table.setCellWidget(row_index, COL_PDF, self._wrap(rw.pdf_button))

        # プレビュー（行単位）
        rw.preview_button = QPushButton("プレビュー")
        rw.preview_button.clicked.connect(lambda _=False, r=rw: self._on_preview(r))
        self._table.setCellWidget(row_index, COL_PREVIEW, self._wrap(rw.preview_button))

        # 印刷（行単位）
        rw.print_button = QPushButton("印刷")
        rw.print_button.clicked.connect(lambda _=False, r=rw: self._on_print(r))
        self._table.setCellWidget(row_index, COL_PRINT, self._wrap(rw.print_button))

        # Kintone登録（行単位）。対象行の受注NoをKintone登録処理画面の入力欄へ追加する。
        # Kintone登録処理画面が起動していないときは無効。
        kintone_widget = QWidget()
        kintone_layout = QVBoxLayout(kintone_widget)
        kintone_layout.setContentsMargins(4, 4, 4, 4)
        kintone_layout.setSpacing(4)
        rw.kintone_button = QPushButton("受注No追加")
        rw.kintone_button.setToolTip(
            "この行の受注NoをKintone登録処理画面の入力欄へ追加します。"
            "（Kintone登録処理画面の起動が必要です）"
        )
        rw.kintone_button.clicked.connect(lambda _=False, r=rw: self._on_kintone_register(r))
        rw.kintone_status_button = QPushButton(KINTONE_STATUS_UNREGISTERED)
        rw.kintone_status_button.setEnabled(False)
        rw.kintone_status_button.setToolTip("Kintone登録状態を表示します。")
        kintone_layout.addWidget(rw.kintone_button)
        kintone_layout.addWidget(rw.kintone_status_button)
        self._table.setCellWidget(row_index, COL_KINTONE, kintone_widget)
        # 受注Noが変わったらボタン状態（追加済/登録可）を再判定する（要件3）。
        rw.order_input.textChanged.connect(lambda _text, r=rw: self._on_order_text_changed(r))
        self._connect_row_data_change_signals(rw)

        if new_input_row:
            self._new_input_row = rw
            # 再描画で参照がズレないよう、現在の新規入力行の受注No欄・取得ボタンを保持する。
            self._new_order_no_edit = rw.order_input
            self._new_fetch_button = rw.refetch_button
        else:
            self._rows.append(rw)
        self._apply_voucher_table_row_font(row_index)
        # bulk復元中は行単位の重い後処理（O(n²)になる resize/列幅/列表示/フィルタ/
        # スタイル再適用）を行わず、復元完了後に1回だけまとめて実行する（要件1・2）。
        bulk = getattr(self, "_bulk_restoring_saved_rows", False)
        if not bulk:
            self._table.resizeColumnsToContents()
            self._table.resizeRowsToContents()
            self._apply_table_column_widths()
            # 追加行のKintone登録ボタン状態を初期化（起動状態・追加済判定: 要件2・3）。
            self.refresh_kintone_buttons()
            self._refresh_registration_status_buttons()
        self._refresh_row_olap_state(rw)
        if new_input_row:
            self._apply_new_input_row_state(rw)
            # 入力行作成直後に細い固定行高を適用する（要件1）。
            self._apply_new_input_row_height()
        if not bulk:
            # 画面表示後に追加された行にも共通の用途別ボタン色を適用する。
            from app.theme_utils import apply_semantic_button_styles

            apply_semantic_button_styles(self)
            self._apply_filters()
            if not new_input_row:
                self._save_records_if_ready()
        return rw

    def _ensure_new_input_row(self) -> _RowWidgets:
        if self._new_input_row is not None:
            return self._new_input_row
        return self._add_row(new_input_row=True)

    def _apply_new_input_row_height(self) -> None:
        """一番上のOLAP取得用新規入力行だけを細い固定行高へ揃える（要件1）。

        通常の保存済みレコード行は setDefaultSectionSize(108) の高い行高のまま維持し、
        新規入力行のみ VOUCHER_NEW_INPUT_ROW_HEIGHT へ固定する。resizeRowsToContents や
        setDefaultSectionSize の後でも太く／細く不揃いにならないよう、入力行の作成後・
        フィルタ後・列表示反映後・チャンク復元後に本メソッドを呼んで常に細い方へ戻す。
        """
        rw = getattr(self, "_new_input_row", None)
        table = getattr(self, "_table", None)
        if rw is None or table is None:
            return
        # 入力欄・取得ボタン・セル内ラッパの上下余白を詰め、行高を押し広げないようにする。
        for column in (COL_SELECT, COL_ORDER_NO, COL_REFETCH):
            holder = table.cellWidget(rw.table_row_index, column)
            if holder is None:
                continue
            layout = holder.layout()
            if layout is not None:
                layout.setContentsMargins(4, 1, 4, 1)
                layout.setSpacing(0)
        self._log_voucher_event("voucher_window_new_input_row_layout_margins_adjusted")
        # 受注No入力欄・取得ボタンの最大高さを抑え、細い行高に収める。
        for widget in (
            getattr(rw, "order_input", None),
            getattr(rw, "refetch_button", None),
        ):
            if widget is None:
                continue
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(VOUCHER_NEW_INPUT_ROW_WIDGET_HEIGHT)
        self._log_voucher_event(
            "voucher_window_new_input_row_widget_height_applied",
            height=VOUCHER_NEW_INPUT_ROW_WIDGET_HEIGHT,
        )
        # 入力行だけを固定の細い行高へ戻す（通常行の行高は変更しない）。
        try:
            table.setRowHeight(rw.table_row_index, VOUCHER_NEW_INPUT_ROW_HEIGHT)
        except Exception:  # noqa: BLE001 - 行高設定失敗でUIを落とさない
            pass
        self._log_voucher_event(
            "voucher_window_olap_input_row_height_applied", height=VOUCHER_OLAP_INPUT_ROW_HEIGHT
        )
        self._log_voucher_event(
            "voucher_window_new_input_row_height_fixed", height=VOUCHER_NEW_INPUT_ROW_HEIGHT
        )

    def _is_new_input_row(self, rw: _RowWidgets) -> bool:
        return bool(getattr(rw, "is_new_input_row", False))

    def _all_table_rows(self) -> list[_RowWidgets]:
        rows: list[_RowWidgets] = []
        if self._new_input_row is not None:
            rows.append(self._new_input_row)
        rows.extend(self._rows)
        return rows

    def _connect_row_data_change_signals(self, rw: _RowWidgets) -> None:
        rw.date_edit.dateChanged.connect(lambda _date, r=rw: self._on_row_data_changed(r))
        rw.finish_none_check.toggled.connect(lambda _checked, r=rw: self._on_row_data_changed(r))
        for radio in (rw.ampm_none, rw.ampm_am, rw.ampm_pm):
            radio.toggled.connect(lambda _checked, r=rw: self._on_row_data_changed(r))
        for cb in rw.process_checks.values():
            cb.stateChanged.connect(lambda _state, r=rw: self._on_row_data_changed(r))
        for cb in rw.voucher_checks.values():
            cb.stateChanged.connect(lambda _state, r=rw: self._on_row_data_changed(r))

    # 「加工名」「印刷する伝票」のラベルが見切れないための最低列幅（要件1）。
    # 加工名は3列・印刷する伝票は2列の各ラベル（DM-10／配送指示書 等）が
    # 折り返さず収まる幅を確保する。
    PROCESS_MIN_WIDTH = 300
    VOUCHER_MIN_WIDTH = 280

    def _apply_table_column_widths(self) -> None:
        """固定列を設定し、余剰幅を「加工名」「印刷する伝票」へ配分する（要件1-1・1-3・5）。"""
        fixed = {
            COL_SELECT: 60,
            COL_ORDER_NO: 130,
            COL_REFETCH: 110,
            COL_FINISH_DATE: 165,
            COL_AMPM: 100,
            COL_EDIT: 125,
            COL_PDF: 105,
            COL_PREVIEW: 115,
            COL_PRINT: 90,
            COL_KINTONE: 135,
        }
        for column, width in fixed.items():
            self._table.setColumnWidth(column, width)

        # 表示幅の余剰を加工名・印刷する伝票へ配分し、チェックボックスの
        # ラベル（DM-10／売上伝票／配送指示書 等）が見切れないようにする。
        viewport_w = self._table.viewport().width()
        remaining = viewport_w - sum(fixed.values())
        if remaining >= self.PROCESS_MIN_WIDTH + self.VOUCHER_MIN_WIDTH:
            process_w = int(remaining * 0.48)
            voucher_w = remaining - process_w
        else:
            process_w = self.PROCESS_MIN_WIDTH
            voucher_w = self.VOUCHER_MIN_WIDTH
        self._table.setColumnWidth(COL_PROCESS, process_w)
        self._table.setColumnWidth(COL_VOUCHER, voucher_w)
        # フォント拡大・AM/PM2段ラジオ・3段チェックに合わせて行高を広げる（要件4・5）。
        self._table.verticalHeader().setDefaultSectionSize(108)
        # 列幅再計算後も列表示/非表示を維持する（要件3）。
        self._apply_column_visibility(source="apply_table_column_widths")

    def _apply_column_visibility(self, *, source: str = "unspecified", force: bool = False) -> None:
        """現在の _visible_columns を一覧テーブルへ反映する（setColumnHidden・要件2・3）。

        非表示不可(hideable=False)列は常に表示する。横スクロール設定は変更しない。
        行追加・フィルタ・選択変更などから多発（実機で144回）していたため、
        bulk復元中はskip、同一状態ならskipして再適用を抑制する。
        """
        visible = getattr(self, "_visible_columns", None)
        table = getattr(self, "_table", None)
        self._log_voucher_event(
            "voucher_window_column_visibility_apply_requested", source=source
        )
        if visible is None or table is None:
            return
        # bulk復元中は行ごとの再反映を抑制し、復元完了後に1回だけ反映する（要件2）。
        if getattr(self, "_bulk_restoring_saved_rows", False) and not force:
            self._log_voucher_event(
                "voucher_window_column_visibility_apply_skipped_bulk_restore", source=source
            )
            return
        # 同一 visible_columns 状態なら再適用しない（無駄な setColumnHidden 連発を防ぐ）。
        if not force and self._last_applied_visible_columns == visible:
            self._log_voucher_event(
                "voucher_window_column_visibility_apply_skipped_same_state", source=source
            )
            return
        for spec in VOUCHER_COLUMN_SPECS:
            if not spec.hideable:
                should_hide = False
            else:
                should_hide = not bool(visible.get(spec.key, spec.default_visible))
            try:
                table.setColumnHidden(spec.index, should_hide)
            except Exception:  # noqa: BLE001 - 列反映失敗でUIを落とさない
                pass
        self._last_applied_visible_columns = dict(visible)
        self._column_visibility_apply_count += 1
        self._log_voucher_event(
            "voucher_window_column_visibility_applied",
            visible_columns=dict(visible),
            source=source,
        )
        self._log_voucher_event(
            "voucher_window_column_visibility_apply_call_count",
            count=self._column_visibility_apply_count,
            source=source,
        )
        # 列表示反映後も新規入力行だけは細い固定行高を保つ（要件1）。
        self._apply_new_input_row_height()

    def _save_column_visibility(self, visible: dict[str, bool]) -> None:
        """列表示/非表示の設定を保存し、テーブルへ即時反映する（要件3）。"""
        # 表示不可列は常時表示のため、保存には hideable 列のうち表示ONのものだけを含める。
        visible_keys = [
            spec.key
            for spec in VOUCHER_COLUMN_SPECS
            if (not spec.hideable) or bool(visible.get(spec.key, spec.default_visible))
        ]
        self._visible_columns = resolve_visible_columns(visible_keys)
        try:
            save_visible_column_keys(visible_keys)
            self._log_voucher_event(
                "voucher_window_column_visibility_settings_saved",
                visible_columns=self._visible_columns,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_voucher_event(
                "voucher_window_column_visibility_settings_save_failed",
                error=str(exc),
            )
        # 設定変更時は状態が変わったので強制反映する（要件2）。
        self._apply_column_visibility(source="settings_change", force=True)

    def _apply_voucher_row_font(self, widget: QWidget) -> None:
        font = widget.font()
        font.setPointSize(VOUCHER_ROW_FONT_SIZE)
        widget.setFont(font)

        for child in widget.findChildren(QWidget):
            child_font = child.font()
            child_font.setPointSize(VOUCHER_ROW_FONT_SIZE)
            child.setFont(child_font)

    def _apply_voucher_table_row_font(self, row_index: int) -> None:
        for column in range(self._table.columnCount()):
            widget = self._table.cellWidget(row_index, column)
            if widget is not None:
                self._apply_voucher_row_font(widget)

    def _apply_voucher_table_fonts(self) -> None:
        for row_index in range(self._table.rowCount()):
            self._apply_voucher_table_row_font(row_index)

    def _row_updated_at_for_sort(self, rw: _RowWidgets) -> datetime:
        updated_at = getattr(rw, "updated_at", None)
        if isinstance(updated_at, datetime):
            return updated_at
        if isinstance(updated_at, str):
            return _datetime_from_iso(updated_at) or datetime.min
        return datetime.min

    def _row_has_olap_data(self, rw: _RowWidgets) -> bool:
        data = getattr(rw, "cached_olap", None)
        if not isinstance(data, dict):
            return False
        return bool(data.get("pages") or data.get("raw_rows"))

    def _sort_rows_by_updated_at(self) -> None:
        sorted_rows = sorted(
            self._all_table_rows(),
            key=lambda rw: (
                2 if self._is_new_input_row(rw) else 1 if not self._row_has_olap_data(rw) else 0,
                self._row_updated_at_for_sort(rw),
            ),
            reverse=True,
        )
        self._rows = [rw for rw in sorted_rows if not self._is_new_input_row(rw)]
        header = self._table.verticalHeader()
        for visual_index, rw in enumerate(sorted_rows):
            logical_index = getattr(rw, "table_row_index", -1)
            if logical_index < 0:
                continue
            current_visual = header.visualIndex(logical_index)
            if current_visual != visual_index:
                header.moveSection(current_visual, visual_index)
        self._apply_table_column_widths()

    def _is_empty_order_no(self, value: object) -> bool:
        return not str(value or "").strip()

    def _duplicate_order_no_row(
        self, order_no: object, exclude: _RowWidgets | None = None
    ) -> _RowWidgets | None:
        """同じ受注No（正規化後）を持つ他行を返す。なければ None。

        空欄は対象外。exclude（自分自身の行）は重複判定から除外する。
        取得済み・未取得・登録状態に関係なく一覧内にあれば重複扱いする（要件2）。
        """
        target = normalize_order_no(order_no)
        if not target:
            return None
        for rw in self._rows:
            if rw is exclude:
                continue
            if self._is_new_input_row(rw):
                continue
            if normalize_order_no(rw.order_input.text()) == target:
                return rw
        return None

    def _has_empty_order_no_row(self) -> bool:
        return any(
            not self._is_new_input_row(rw) and self._is_empty_order_no(rw.order_input.text())
            for rw in self._rows
        )

    def _update_add_row_button_enabled(self) -> None:
        return None

    def _mark_row_updated(self, rw: _RowWidgets) -> None:
        if getattr(self, "_restoring_records", False):
            return
        self._apply_filters()
        self._save_records_if_ready()

    def _on_row_data_changed(self, rw: _RowWidgets) -> None:
        # bulk復元中は行データ変更の後処理を抑制する（復元後に1回だけ整合する・要件1・2）。
        if getattr(self, "_bulk_restoring_saved_rows", False):
            return
        if self._is_new_input_row(rw):
            return
        self._mark_row_updated(rw)

    @staticmethod
    def _wrap(inner: QWidget) -> QWidget:
        """セル内ウィジェットに余白を持たせるためのラッパ。"""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(inner)
        return holder

    def _on_add_row(self) -> None:
        rw = self._add_row()
        rw.order_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._update_selection_state()

    def _greyed_cell(self) -> str:
        if current_title_bar_is_dark():
            return """
QWidget {
    background-color: #171b20;
}
QLabel {
    color: #a8b0ba;
}
"""
        return """
QWidget {
    background-color: #e5e7eb;
}
QLabel {
    color: #6b7280;
}
"""

    def _replace_cell_with_disabled_blank(self, rw: _RowWidgets, column: int) -> None:
        holder = QWidget()
        holder.setEnabled(False)
        holder.setStyleSheet(self._greyed_cell())
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(4, 4, 4, 4)
        self._table.setCellWidget(rw.table_row_index, column, holder)

    def _apply_new_input_row_state(self, rw: _RowWidgets) -> None:
        rw.select_check.blockSignals(True)
        rw.select_check.setChecked(False)
        rw.select_check.setEnabled(False)
        rw.select_check.blockSignals(False)
        rw.order_input.setReadOnly(False)
        rw.order_input.setToolTip("")
        rw.refetch_button.setText("取得")
        rw.refetch_button.setEnabled(True)
        rw.refetch_button.setProperty("buttonRole", "olapFetch")

        for widget in (
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
        ):
            self._set_widget_tree_enabled(widget, False)
        for cb in rw.process_checks.values():
            cb.setChecked(False)
            self._set_widget_tree_enabled(cb, False)
        for cb in rw.voucher_checks.values():
            cb.setChecked(False)
            self._set_widget_tree_enabled(cb, False)

        for column in range(COL_FINISH_DATE, COL_KINTONE + 1):
            self._replace_cell_with_disabled_blank(rw, column)

    def _set_widget_tree_enabled(self, widget: QWidget, enabled: bool) -> None:
        widget.setEnabled(enabled)

    def _refresh_row_olap_state(self, rw: _RowWidgets) -> None:
        if self._is_new_input_row(rw):
            self._apply_new_input_row_state(rw)
            return
        has_data = self._row_has_olap_data(rw)
        rw.refetch_button.setText("更新" if has_data else "取得")
        rw.refetch_button.setProperty("buttonRole", "olapUpdate" if has_data else "olapFetch")
        style = rw.refetch_button.style()
        style.unpolish(rw.refetch_button)
        style.polish(rw.refetch_button)
        rw.order_input.setReadOnly(has_data)
        rw.order_input.setToolTip("OLAP取得済みの受注Noは変更できません。" if has_data else "")
        rw.date_edit.setEnabled(has_data and not rw.finish_none_check.isChecked())
        for widget in (
            rw.finish_none_check,
            rw.ampm_none,
            rw.ampm_am,
            rw.ampm_pm,
            rw.edit_button,
            rw.pdf_button,
            rw.preview_button,
            rw.print_button,
        ):
            self._set_widget_tree_enabled(widget, has_data)
        for cb in rw.process_checks.values():
            self._set_widget_tree_enabled(cb, has_data)
        for cb in rw.voucher_checks.values():
            self._set_widget_tree_enabled(cb, has_data)
        self.refresh_kintone_buttons()

    # ── Kintone登録処理画面との連携 ─────────────────────────────────────────────
    def _current_kintone_window(self):
        """Kintone登録処理画面（MainWindow）の現在インスタンスを返す。未起動なら None。

        画面が閉じられた後の参照で例外にならないよう、プロバイダ呼び出しは保護する。
        """
        provider = self._kintone_window_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001 - 参照取得失敗時は未起動扱い
            return None

    def _is_kintone_window_open(self) -> bool:
        return self._current_kintone_window() is not None

    def _kintone_order_numbers(self) -> set[str] | None:
        """Kintone登録処理画面に現在入力されている受注No一覧を返す。

        画面が未起動なら None を返す（追加済判定とは区別する）。受注No取得で
        例外が出ても落とさず空集合扱いにする（要件6）。
        """
        window = self._current_kintone_window()
        if window is None:
            return None
        getter = getattr(window, "get_order_numbers", None)
        if getter is None:
            return set()
        try:
            result = getter()
        except Exception:  # noqa: BLE001 - 取得失敗時は空集合扱い
            return set()
        try:
            return {str(value) for value in result}
        except TypeError:
            return set()

    def refresh_kintone_buttons(self) -> None:
        """各行の「Kintone登録」ボタンの状態をKintone画面の状態に同期する（要件2・3）。

        - 画面が未起動：文言「Kintone登録」・無効。
        - 画面が起動中で受注Noが既に入力済み：文言「追加済」・無効。
        - 画面が起動中で未追加：文言「Kintone登録」・有効。

        ランチャーからの開閉通知・受注No欄変更シグナル・行の受注No変更で呼ばれる。
        """
        order_numbers = self._kintone_order_numbers()
        window_open = order_numbers is not None
        for rw in self._rows:
            button = getattr(rw, "kintone_button", None)
            if button is None:
                continue
            if self._is_new_input_row(rw):
                button.setText("受注No追加")
                button.setEnabled(False)
                button.setStyleSheet("")
                continue
            if not window_open:
                button.setText("受注No追加")
                button.setEnabled(False)
                button.setStyleSheet("")
                continue
            row_order_no = rw.order_input.text().strip()
            if not self._row_has_olap_data(rw):
                button.setText("受注No追加")
                button.setEnabled(False)
                button.setStyleSheet("")
                continue
            if row_order_no and row_order_no in self._voucher_no_blocked_order_nos:
                button.setText("受注No追加")
                button.setEnabled(False)
                button.setStyleSheet("")
                continue
            if row_order_no and row_order_no in order_numbers:
                button.setText("追加済")
                button.setEnabled(False)
                # 追加済は緑色で表示（未起動の灰色無効ボタンと区別: 要件6）。
                button.setStyleSheet(KINTONE_ADDED_BUTTON_STYLE)
            else:
                button.setText("受注No追加")
                button.setEnabled(True)
                button.setStyleSheet("")
        self._apply_voucher_table_fonts()

    def _on_kintone_register(self, rw: _RowWidgets) -> None:
        """対象行の受注NoをKintone登録処理画面の入力欄へ追加する。"""
        window = self._current_kintone_window()
        if window is None:
            # ボタンは無効化されているため通常到達しないが、念のため防御する。
            QMessageBox.warning(
                self,
                "Kintone登録処理画面が未起動",
                "Kintone登録処理画面が起動していません。\n"
                "先にKintone登録処理画面を起動してください。",
            )
            self.refresh_kintone_buttons()
            return
        row = self._collect_row(rw)
        order_no = row.order_no
        if not order_no:
            QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
            return
        try:
            # 受注Noに加えて、同じ行の仕上日／AM・PMも渡して登録前確認へ反映する（要件1・4）。
            window.add_order_no(order_no, finish_date=row.finish_date, am_pm=row.am_pm)
        except Exception as exc:  # noqa: BLE001 - 追加失敗を握りつぶさない
            QMessageBox.warning(self, "Kintone登録エラー", f"受注Noの追加に失敗しました:\n{exc}")
            return
        self.refresh_kintone_buttons()

    def _on_order_text_changed(self, rw: _RowWidgets) -> None:
        # bulk復元中は行ごとの重い再描画（ボタン再判定・フィルタ・保存）を抑制する。
        # QLineEdit.textChanged は table.blockSignals では止まらないため、ここで明示的に
        # 早期returnして O(n²) 化を防ぐ（復元完了後に1回だけ反映する・要件1・2）。
        if getattr(self, "_bulk_restoring_saved_rows", False):
            return
        self.refresh_kintone_buttons()
        self._refresh_registration_status_buttons()
        self._apply_filters()
        if self._is_new_input_row(rw):
            return
        self._save_records_if_ready()

    def notify_kintone_registration_completed(self, order_numbers: list[str] | set[str]) -> None:
        """Kintone登録成功通知を受け、該当受注Noの全行を登録完了にする。"""
        notification_items = self._build_teams_notification_items(order_numbers)
        now = datetime.now()
        for order_no in order_numbers:
            value = str(order_no or "").strip()
            if value:
                self._registration_status_by_order[value] = KINTONE_STATUS_COMPLETED
                for rw in self._rows:
                    if rw.order_input.text().strip() == value:
                        rw.updated_at = now
        self._refresh_registration_status_buttons()
        self._apply_filters()
        self._save_records()
        if notification_items:
            self._notify_teams_registration_completed(notification_items)

    def _registration_status_for_order(self, order_no: str) -> str:
        order_no = str(order_no or "").strip()
        return self._registration_status_by_order.get(order_no, KINTONE_STATUS_UNREGISTERED)

    def _build_teams_notification_items(self, order_numbers: list[str] | set[str]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for order_no in order_numbers:
            value = str(order_no or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            label = "更新" if self._registration_status_for_order(value) == KINTONE_STATUS_COMPLETED else "新規"
            items.append({"order_no": value, "label": label})
        return items

    def _notify_teams_registration_completed(self, items: list[dict[str, str]]) -> None:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        if not _settings_bool(settings, SETTINGS_TEAMS_ENABLED, True):
            return
        webhook_url = self._teams_webhook_url_for_current_kintone_target(settings)
        if not webhook_url:
            logging.getLogger("tks_to_kintone_app").warning("Teams Webhook URLが未設定です。")
            return
        payload = build_teams_order_links_payload(items, target=self._current_kintone_target(settings))
        try:
            post_teams_webhook(webhook_url, payload)
        except TeamsNotifyError:
            logging.getLogger("tks_to_kintone_app").warning("Teams通知に失敗しました。")

    def _current_kintone_target(self, settings: QSettings | None = None) -> str:
        store = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
        target = str(store.value(SETTINGS_KINTONE_TARGET, SETTINGS_KINTONE_TARGET_PROD) or SETTINGS_KINTONE_TARGET_PROD)
        if target in {SETTINGS_KINTONE_TARGET_TEST, SETTINGS_KINTONE_TARGET_PROD}:
            return target
        return SETTINGS_KINTONE_TARGET_PROD

    def _teams_webhook_url_for_current_kintone_target(self, settings: QSettings | None = None) -> str:
        store = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
        target = self._current_kintone_target(store)
        if target == SETTINGS_KINTONE_TARGET_TEST:
            return str(store.value(SETTINGS_TEAMS_WEBHOOK_URL_TEST, default_teams_webhook_url_test()) or "").strip()
        if target == SETTINGS_KINTONE_TARGET_PROD:
            return str(store.value(SETTINGS_TEAMS_WEBHOOK_URL_PROD, default_teams_webhook_url_prod()) or "").strip()
        return ""

    def _refresh_registration_status_buttons(self) -> None:
        for rw in self._rows:
            status = self._registration_status_for_order(rw.order_input.text())
            button = getattr(rw, "kintone_status_button", None)
            if button is None:
                continue
            if self._is_new_input_row(rw):
                button.setText("")
                button.setEnabled(False)
                button.setStyleSheet("")
                continue
            button.setText(status)
            button.setEnabled(False)
            button.setStyleSheet(KINTONE_STATUS_STYLES.get(status, ""))
        self._apply_voucher_table_fonts()

    def _apply_filters(self, *_args, sort_rows: bool = True) -> None:
        logger = logging.getLogger("tks_to_kintone_app")
        if sort_rows:
            try:
                self._sort_rows_by_updated_at()
            except Exception:
                logger.warning("伝票一覧の更新日時ソートに失敗しました。", exc_info=True)
        search = self._order_search_edit.text().strip() if hasattr(self, "_order_search_edit") else ""
        status_filter = self._status_filter.currentText() if hasattr(self, "_status_filter") else "すべて"
        visible_count = 0
        filtered_count = 0
        for rw in self._rows:
            order_no = rw.order_input.text().strip()
            status = self._registration_status_for_order(order_no)
            is_new_unfetched = self._is_new_input_row(rw) or not self._row_has_olap_data(rw)
            hidden = False
            if not is_new_unfetched:
                hidden = bool(search) and search not in order_no
                if status_filter != "すべて" and status != status_filter:
                    hidden = True
            row_index = getattr(rw, "table_row_index", -1)
            if row_index >= 0:
                self._table.setRowHidden(row_index, hidden)
            if hidden:
                filtered_count += 1
            else:
                visible_count += 1
        logger.info("voucher records visible count: %s", visible_count)
        logger.info("voucher records filtered count: %s", filtered_count)
        self._update_selection_state()
        self._apply_voucher_table_fonts()
        # フィルタ適用（ソート・行高再計算を伴う）後も新規入力行の高さを細い方へ戻す（要件1）。
        self._apply_new_input_row_height()

    # 統合設定ダイアログの初期表示タブ識別子。
    COMBINED_SETTINGS_TAB_DISPLAY = "display"
    COMBINED_SETTINGS_TAB_PRINTER = "printer"
    COMBINED_SETTINGS_TAB_VOUCHER = "voucher"

    def _on_display_settings(self) -> None:
        """表示設定・印刷設定・伝票設定を1つにまとめた統合設定を開く（要件2/3/4）。"""
        self._open_combined_settings(self.COMBINED_SETTINGS_TAB_DISPLAY)

    def _refresh_processing_display_names(self) -> None:
        """チェック辞書の従来キーを維持したままラベルだけ更新する。"""
        names = load_processing_display_names()
        for row in self._rows:
            for default_name, checkbox in row.process_checks.items():
                checkbox.setText(resolve_processing_display_name(default_name, names))

    def _open_combined_settings(self, initial_tab: str) -> None:
        """統合設定ダイアログを指定タブで開き、OK時に3カテゴリを保存・反映する（要件4）。"""
        dialog = CombinedVoucherSettingsDialog(
            visible_columns=dict(self._visible_columns),
            selected_ids=set(self._default_print_types),
            retention_days=load_cache_retention_days(),
            record_retention_days=load_record_retention_days(),
            finish_date_none=self._default_finish_date_none,
            ampm_default=self._default_ampm,
            price_display_mode=load_price_display_mode(),
            parent=self,
        )
        try:
            dialog.select_tab(initial_tab)
        except Exception:  # noqa: BLE001 - タブ選択失敗でも設定は開く
            pass
        self._log_voucher_event(
            "voucher_window_combined_settings_opened", initial_tab=initial_tab
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # (1) 表示設定（列表示/非表示）を保存・反映する。
        self._save_column_visibility(dialog.visible_columns())

        # (2) 印刷設定を保存する（従来の保存経路と同じ関数を使う）。
        printer_values = dialog.printer_values()
        try:
            save_voucher_printer_settings(printer_values)
        except Exception as exc:
            QMessageBox.warning(
                self, "印刷設定保存エラー", f"印刷設定の保存に失敗しました:\n{exc}"
            )
        else:
            self._set_print_status(
                "印刷設定を保存しました。SumatraPDFパスが空欄の場合は自動検出します。"
            )

        # (3) 伝票設定（印刷する伝票初期値・保存期間・仕上日/AMPM初期値）を保存・反映する。
        # 現在の一覧への反映は、伝票設定タブ下部のチェックボックスの状態で決める（要件3/4）。
        self._persist_voucher_print_settings(
            new_ids=dialog.selected_ids(),
            retention_days=dialog.retention_days(),
            record_retention_days=dialog.record_retention_days(),
            finish_date_none=dialog.finish_date_none(),
            ampm_default=dialog.ampm_default(),
            price_display_mode=(
                dialog.price_display_mode()
                if hasattr(dialog, "price_display_mode")
                else load_price_display_mode()
            ),
            apply_to_current_list=dialog.apply_to_current_list_requested(),
        )
        if hasattr(dialog, "processing_display_names"):
            try:
                save_processing_display_names(dialog.processing_display_names())
            except ValueError as exc:
                QMessageBox.warning(self, "伝票加工名設定", str(exc))
                return
            self._refresh_processing_display_names()

    def _persist_voucher_print_settings(
        self,
        *,
        new_ids: set[str],
        retention_days: int,
        record_retention_days: int,
        finish_date_none: bool,
        ampm_default: str,
        price_display_mode: str = PRICE_DISPLAY_CONDITIONAL,
        apply_to_current_list: bool = False,
    ) -> None:
        """伝票設定（印刷する伝票の初期値など）を保存し、必要なら既存行へ反映する。

        設定値の保存は常に行う。現在の一覧への反映は apply_to_current_list が True の
        ときだけ実行する（要件3/4・OK後の確認ダイアログは廃止）。
        """
        try:
            save_default_print_types(sorted(new_ids))
            save_cache_retention_days(retention_days)
            save_record_retention_days(record_retention_days)
            save_default_finish_date_none(finish_date_none)
            save_default_ampm(ampm_default)
            save_price_display_mode(price_display_mode)
        except Exception as exc:
            QMessageBox.warning(self, "設定保存エラー", f"設定の保存に失敗しました:\n{exc}")
            return
        self._default_print_types = set(new_ids)
        self._default_finish_date_none = load_default_finish_date_none()
        self._default_ampm = load_default_ampm()
        if apply_to_current_list and self._rows:
            self._apply_print_settings_to_rows(
                new_ids,
                finish_date_none=self._default_finish_date_none,
                ampm_default=self._default_ampm,
            )

    def _on_voucher_settings(self) -> None:
        """旧「伝票設定」入口。統合設定ダイアログの伝票設定タブを初期表示で開く（要件4）。"""
        self._log_voucher_event("voucher_window_legacy_voucher_settings_redirected")
        self._open_combined_settings(self.COMBINED_SETTINGS_TAB_VOUCHER)

    def _on_printer_settings(self) -> None:
        """旧「印刷設定」入口。統合設定ダイアログの印刷設定タブを初期表示で開く（要件4）。"""
        self._log_voucher_event("voucher_window_legacy_print_settings_redirected")
        self._open_combined_settings(self.COMBINED_SETTINGS_TAB_PRINTER)

    def _apply_print_types_to_rows(self, ids: set[str]) -> None:
        """現在表示中の全行の印刷する伝票チェックを設定値で上書きする。"""
        id_set = set(ids)
        for rw in self._rows:
            if self._is_new_input_row(rw):
                continue
            for vid, cb in rw.voucher_checks.items():
                cb.setChecked(vid in id_set)
        if not getattr(self, "_restoring_records", False):
            self._apply_filters()
            self._save_records_if_ready()

    def _apply_print_settings_to_rows(
        self,
        ids: set[str],
        *,
        finish_date_none: bool,
        ampm_default: str,
    ) -> None:
        """印刷設定の初期値を現在の一覧行にも反映する。

        「仕上日なし」は ON/OFF とも既存行へ明示的に上書きする。
        OFF時も日付欄の日付は保持し、なし扱いだけを解除する。
        AM/PM も設定値を明示的な既定として全行に上書きする。
        """
        id_set = set(ids)
        for rw in self._rows:
            if self._is_new_input_row(rw):
                continue
            for vid, cb in rw.voucher_checks.items():
                cb.setChecked(vid in id_set)
            self._apply_row_finish_ampm_defaults(
                rw,
                finish_date_none=finish_date_none,
                ampm_default=ampm_default,
            )
        if not getattr(self, "_restoring_records", False):
            self._apply_filters()
            self._save_records_if_ready()

    def _apply_row_finish_ampm_defaults(
        self,
        rw: _RowWidgets,
        *,
        finish_date_none: bool,
        ampm_default: str,
        apply_finish_off: bool = True,
    ) -> None:
        """行ウィジェットへ仕上日なし・AM/PM既定値を反映する。"""
        normalized_finish_none = normalize_finish_date_none(finish_date_none)
        if normalized_finish_none or apply_finish_off:
            rw.finish_none_check.setChecked(normalized_finish_none)
        rw.date_edit.setEnabled(
            self._row_has_olap_data(rw) and not rw.finish_none_check.isChecked()
        )

        normalized_ampm = str(ampm_default or "am").strip().lower()
        if normalized_ampm == "none":
            rw.ampm_none.setChecked(True)
        elif normalized_ampm == "pm":
            rw.ampm_pm.setChecked(True)
        else:
            rw.ampm_am.setChecked(True)

    # ── 新規入力行取得の診断ログ（デバッグ表示ON時のみ）─────────────────────────
    def _new_row_fetch_debug_enabled(self) -> bool:
        """新規入力行取得の診断ログを出力するか（環境変数 or デバッグ表示設定）。"""
        if os.environ.get("TKS_VOUCHER_DEBUG", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return True
        try:
            settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
            raw = settings.value("ui/debug_visible", "0")
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:  # noqa: BLE001 - 設定取得失敗時は無効扱い
            return False

    def _log_new_row_event(self, event: str, **extra: object) -> None:
        """新規入力行取得の1イベントを work/debug のJSONLへ追記する（デバッグ時のみ）。

        取得ボタン押下から通常行追加・再描画・フィルター非表示までを追跡できるよう、
        現在のUI状態（受注No・検証・重複・件数・フィルター）を各行に含める。
        ログ出力自体は本処理を絶対に妨げないよう、例外は握りつぶす。
        """
        if not self._new_row_fetch_debug_enabled():
            return
        try:
            rw = self._new_input_row
            edit = self._new_order_no_edit
            cell_edit = self._new_row_cell_line_edit()
            raw_order_no = edit.text() if edit is not None else ""
            raw_from_cell = cell_edit.text() if cell_edit is not None else ""
            stripped = raw_order_no.strip()
            payload: dict[str, object] = {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event": event,
                "table_row": getattr(rw, "table_row_index", -1),
                "is_new_input_row": self._is_new_input_row(rw) if rw is not None else None,
                "self_new_fetch_button_object_id": self._obj_id(self._new_fetch_button),
                "self_new_order_edit_object_id": self._obj_id(edit),
                "raw_order_no": raw_order_no,
                "raw_order_no_from_self_edit": raw_order_no,
                "raw_order_no_from_table_cell": raw_from_cell,
                "stripped_order_no": stripped,
                "normalized_order_no": normalize_order_no(raw_order_no),
                "rows_count": len(self._rows),
                "visible_row_count": self._visible_row_count(),
                "search_filter_text": (
                    self._order_search_edit.text()
                    if hasattr(self, "_order_search_edit")
                    else ""
                ),
                "registration_filter": (
                    self._status_filter.currentText()
                    if hasattr(self, "_status_filter")
                    else ""
                ),
                "existing_order_numbers": [
                    r.order_input.text().strip()
                    for r in self._rows
                    if not self._is_new_input_row(r)
                ],
            }
            payload.update(extra)
            debug_dir = get_order_capture_debug_dir()
            debug_dir.mkdir(parents=True, exist_ok=True)
            if self._new_row_fetch_log_path is None:
                self._new_row_fetch_log_path = (
                    debug_dir
                    / f"voucher_new_row_fetch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
                )
            with open(self._new_row_fetch_log_path, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 診断ログ失敗は本処理に影響させない
            logging.getLogger("tks_to_kintone_app").debug(
                "新規入力行取得の診断ログ出力に失敗しました。", exc_info=True
            )

    def _visible_row_count(self) -> int:
        """フィルターで非表示にされていない通常行の件数を返す。"""
        count = 0
        for rw in self._rows:
            if self._is_new_input_row(rw):
                continue
            idx = getattr(rw, "table_row_index", -1)
            if idx >= 0 and not self._table.isRowHidden(idx):
                count += 1
        return count

    def _set_new_row_status(self, text: str, *, error: bool = False) -> None:
        """新規入力行「取得」の処理状態を画面下部へ必ず表示する。

        取得ボタン押下から通常行追加まで、どこまで処理が到達したかを実機の画面上でも
        確認できるようにする。エラー時は赤字にして黙らないようにする。
        """
        label = getattr(self, "_new_row_status_label", None)
        if label is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        label.setText(f"[{stamp}] {text}")
        if current_title_bar_is_dark():
            color = "#fca5a5" if error else "#e5e7eb"
        else:
            color = "#b91c1c" if error else "#111827"
        weight = "font-weight: bold;" if error else ""
        label.setStyleSheet(f"color: {color}; {weight}")

    def _set_print_status(self, text: str, *, error: bool = False) -> None:
        self._set_new_row_status(text, error=error)

    # ── 処理中表示（busy/進捗）────────────────────────────────────────────────
    def _set_busy(self, message: str, *, context: str = "") -> None:
        """時間がかかる処理の開始時に不定進捗バーとステータスを表示する。

        UI全体を無効化せず、処理中であることだけを示す。複数処理が重なっても
        カウンタで管理し、最後の処理が終わるまでバーを消さない（早く消えすぎ防止）。
        """
        counter = getattr(self, "_busy_counter", 0) + 1
        self._busy_counter = counter
        self._log_busy_event(
            "busy_started",
            busy_message=message,
            busy_context=context,
            busy_counter=counter,
        )
        bar = getattr(self, "_busy_progress", None)
        if bar is not None:
            try:
                bar.setVisible(True)
            except RuntimeError:
                pass
        if message:
            self._set_print_status(message)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _clear_busy(self, message: str | None = None, *, context: str = "") -> None:
        """処理完了/失敗時に進捗バーを非表示へ戻す。

        カウンタが0になったときだけバーを消す。エラー時も必ず呼び出して
        処理中表示が残らないようにする。message を渡すとステータスも更新する。
        """
        counter = max(0, getattr(self, "_busy_counter", 0) - 1)
        self._busy_counter = counter
        self._log_busy_event(
            "busy_finished",
            busy_message=message or "",
            busy_context=context,
            busy_counter=counter,
        )
        if counter == 0:
            bar = getattr(self, "_busy_progress", None)
            if bar is not None:
                try:
                    bar.setVisible(False)
                except RuntimeError:
                    pass
        if message:
            self._set_print_status(message)

    @staticmethod
    def _log_busy_event(event_type: str, **fields: object) -> None:
        """処理中表示のログを残す（画面が無くてもログは出す）。"""
        try:
            from app import voucher_print_service

            voucher_print_service.log_voucher_print_event(event_type, **fields)
        except Exception:  # noqa: BLE001 - ログ失敗で本処理を止めない
            pass

    def _new_row_cell_line_edit(self) -> QLineEdit | None:
        """テーブル上に実際に表示されている新規入力行の受注No QLineEdit を取得する。

        self._new_order_no_edit と、テーブルセル内の実 QLineEdit が万一ズレていても、
        画面に見えている値を確実に読めるよう、セルウィジェットから直接探す。
        """
        rw = self._new_input_row
        if rw is None:
            return None
        row_index = getattr(rw, "table_row_index", -1)
        if row_index is None or row_index < 0:
            return None
        holder = self._table.cellWidget(row_index, COL_ORDER_NO)
        if holder is None:
            return None
        if isinstance(holder, QLineEdit):
            return holder
        return holder.findChild(QLineEdit)

    def _build_order_input_context_menu(self, edit: QLineEdit) -> QMenu:
        """受注No入力欄だけに、標準編集動作＋範囲指定を構成する。"""
        from app.context_menu import create_japanese_standard_context_menu

        menu = create_japanese_standard_context_menu(edit)
        menu.addSeparator()
        action = menu.addAction("範囲指定")
        action.triggered.connect(self._open_order_range_dialog)
        return menu

    def _show_order_range_context_menu(self, edit: QLineEdit, pos) -> None:
        menu = self._build_order_input_context_menu(edit)
        menu.exec(edit.mapToGlobal(pos))
        menu.deleteLater()

    @Slot()
    def _open_order_range_dialog(self) -> None:
        existing = self._range_dialog
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        from app.voucher_range import VoucherRangeDialog

        login_id = str(self.olap_login_id)
        password = str(self.olap_password)
        fetch_one = lambda number: fetch_voucher_print_data([number], login_id, password)
        dialog = VoucherRangeDialog(self, fetch_one, normalize_order_no, parent=self)
        dialog.finished.connect(lambda _=0, d=dialog: self._clear_range_dialog(d))
        dialog.destroyed.connect(lambda _=None, d=dialog: self._clear_range_dialog(d))
        self._range_dialog = dialog
        self._log_voucher_event("voucher_range_dialog_opened")
        dialog.show()

    def _clear_range_dialog(self, dialog) -> None:
        if self._range_dialog is dialog:
            self._range_dialog = None

    def reflect_range_fetch_result(self, order_no: str, data: object) -> str:
        """worker取得データをGUI一覧へ反映し、反映完了後の最終状態を返す。"""
        if not isinstance(data, dict):
            raise TypeError("OLAP取得データの形式が正しくありません。")
        existing = self._duplicate_order_no_row(order_no)
        if existing is not None:
            self._select_row_widget(existing)
            return "登録済み"
        settings = self._create_default_row_settings_for_new_fetch(order_no)
        created = self._add_row()
        try:
            created.order_input.setText(order_no)
            self._attach_row_settings(data, settings)
            created.cached_olap = data
            self._cache_row_olap(settings, data)
            created.updated_at = datetime.now()
            self._refresh_row_olap_state(created)
            self._apply_filters()
            self._save_records_if_ready()
        except Exception:
            # 部分追加した行を成功一覧へ残さない。
            try:
                index = created.table_row_index
                self._rows = [row for row in self._rows if row is not created]
                if index >= 0:
                    self._table.removeRow(index)
                    for row in self._rows:
                        if row.table_row_index > index:
                            row.table_row_index -= 1
            except Exception:
                pass
            raise
        return "新規登録"

    @staticmethod
    def _obj_id(widget: object) -> int | None:
        return id(widget) if widget is not None else None

    def _read_new_row_order_no(self) -> tuple[str, dict[str, object]]:
        """新規入力行の受注Noを、保持参照とテーブルセル両方から読んで突き合わせる。

        画面に値が見えているのに処理側が空欄になる「参照ズレ」を検出できるよう、
        両者の値と一致可否を返す。表示中のセル値を優先して採用する。
        """
        self_edit = self._new_order_no_edit
        cell_edit = self._new_row_cell_line_edit()
        raw_from_self = self_edit.text() if self_edit is not None else ""
        raw_from_cell = cell_edit.text() if cell_edit is not None else ""
        # 画面に見えている値（セル側）を優先。空ならば保持参照側を使う。
        chosen_raw = raw_from_cell if raw_from_cell.strip() else raw_from_self
        info: dict[str, object] = {
            "self_new_order_edit_is_none": self_edit is None,
            "self_new_fetch_button_object_id": self._obj_id(self._new_fetch_button),
            "self_new_order_edit_object_id": self._obj_id(self_edit),
            "cell_line_edit_object_id": self._obj_id(cell_edit),
            "raw_order_no_from_self_edit": raw_from_self,
            "raw_order_no_from_table_cell": raw_from_cell,
            "self_and_cell_edit_match": (self_edit is cell_edit),
            "self_and_cell_value_match": (raw_from_self == raw_from_cell),
        }
        return chosen_raw.strip(), info

    def _on_order_input_return_pressed(self, rw: _RowWidgets) -> None:
        """受注No入力欄で Enter を押したときのOLAP取得（取得ボタンと同一処理・要件3）。

        - 「取得」ボタン押下と完全に同じ `_on_refetch_row` を呼ぶ（新規行/通常行を自動分岐）。
        - 取得中（busy）や Enter 連打では二重取得しない。
        - 受注Noが空でも既存のバリデーション（空欄チェック）へ委譲する。
        """
        logger = logging.getLogger("tks_to_kintone_app")
        order_no = ""
        try:
            order_no = (rw.order_input.text() or "").strip()
        except Exception:  # noqa: BLE001 - 参照失効でも落とさない
            order_no = ""
        logger.info("voucher_fetch_enter_pressed order_no=%s", order_no)
        # 取得中／Enter連打では多重取得しない（要件3）。
        if getattr(self, "_fetch_enter_in_progress", False) or getattr(self, "_busy_counter", 0) > 0:
            logger.info("voucher_fetch_enter_ignored_busy order_no=%s", order_no)
            return
        if not order_no:
            # 空欄でも取得ボタンと同じ既存バリデーションへ進める（要件3）。
            logger.info("voucher_fetch_enter_no_order_no")
        logger.info("voucher_fetch_enter_started order_no=%s", order_no)
        self._fetch_enter_in_progress = True
        try:
            self._on_refetch_row(rw)
        finally:
            self._fetch_enter_in_progress = False

    def _on_refetch_row(self, rw: _RowWidgets) -> None:
        """OLAPボタン押下時のディスパッチャ。

        新規入力行（table row 0・_rows 非所属）と通常行（table row >= 1・
        _rows 所属）で取得処理を明確に分岐する。ボタンは行ウィジェット（rw）へ
        直接束ねているため、テーブル行インデックスと _rows インデックスの変換
        （table row - 1）に依存せず、常に対象行を正しく特定できる。

        なお新規入力行の取得ボタンは _on_fetch_new_input_row へ直接接続しており
        （_add_row 参照）、通常はこのディスパッチャを経由しない。
        """
        if self._is_new_input_row(rw):
            self._on_fetch_new_input_row()
        else:
            self._on_refetch_existing_row(rw)

    def _on_fetch_new_input_row(self, _checked: bool = False) -> None:
        """新規入力行（table row 0）専用のOLAP取得処理（要件・新仕様）。

        取得ボタンから lambda を介さず直接呼ばれる。対象行は常に
        self._new_input_row（現在表示中の新規入力行）で、受注Noは
        self._new_order_no_edit.text() から読むため、再描画で参照がズレない。

        1. 新規入力行の受注No入力欄から実入力値を strip して読む。
        2. 空欄なら取得しない。
        3. 既存の通常行に同じ受注Noがあれば重複として扱い取得しない。
        4. OLAP取得を実行し、成功したら通常行を1件追加（_rows へ追加）する。
        5. 新規入力行の受注No欄はクリアし、行は1行だけ維持する。

        失敗時は通常行を追加せず、新規入力行の受注No入力値も残す。
        """
        # 押下されたボタン（signalの送信元）と、現在保持中のボタンが一致するか記録する。
        clicked_button = self.sender()
        self._set_new_row_status("取得開始")
        self._log_new_row_event(
            "new_row_fetch_button_clicked",
            button_object_id=self._obj_id(clicked_button),
            current_new_fetch_button_object_id=self._obj_id(self._new_fetch_button),
            clicked_is_current_button=(clicked_button is self._new_fetch_button),
            edit_object_id=self._obj_id(self._new_order_no_edit),
            current_new_order_edit_object_id=self._obj_id(self._new_order_no_edit),
        )
        rw = self._new_input_row
        if rw is None:
            # 通常は起動時に必ず生成されるが、万一失われていても落とさず再生成する。
            rw = self._ensure_new_input_row()
        # 保存済み一覧の復元が未完のまま取得すると、重複判定が未読込分を見落として
        # 重複行が発生し得る。全件必要な操作なので入口で残りを安全に同期完了させる（要件3）。
        self._log_new_row_event("new_row_fetch_handler_entered")

        # 「受注No読取OK」の直後にOLAP取得開始前で沈黙して止まる不具合を防ぐため、
        # 読取以降の検証〜OLAP取得呼び出しまでを必ず try/except で囲む。ここで
        # 例外を握りつぶすと、実機では画面ステータスが「受注No読取OK」のまま止まり、
        # 通常行も追加されない（＝報告された現象）。どの段階で止まったかを画面・ログの
        # 両方で確定できるよう、途中returnには必ず return_reason を残す。
        order_no = ""
        return_reason = "return_unknown"
        try:
            # 必ず「現在表示中」の受注No欄から読む（実機の再描画対策）。
            # 保持参照とテーブルセル両方から読み、画面に見えている値を優先採用する。
            order_no, read_info = self._read_new_row_order_no()
            self._log_new_row_event(
                "new_row_order_no_read", stripped_order_no=order_no, **read_info
            )
            self._log_new_row_event(
                "new_row_after_order_read", order_no=order_no
            )
            if not read_info.get("self_and_cell_value_match", True):
                # 参照ズレを検出（画面には値があるのに保持参照が空、等）。画面へ明示する。
                self._set_new_row_status(
                    f"参照ズレ検出: セル='{read_info.get('raw_order_no_from_table_cell')}' "
                    f"保持='{read_info.get('raw_order_no_from_self_edit')}' → セル値を使用"
                )

            # ── 4. 空欄チェック ────────────────────────────────────────────
            self._log_new_row_event(
                "new_row_before_validation", order_no=order_no
            )
            if not order_no:
                return_reason = "return_empty_order_no"
                self._log_new_row_event(
                    "new_row_order_no_invalid",
                    order_no=order_no,
                    validation_result="empty",
                    return_reason=return_reason,
                )
                self._set_new_row_status(
                    f"受注No未入力 ({return_reason})", error=True
                )
                QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
                return
            self._set_new_row_status(f"受注No読取OK: {order_no}")

            # ── 5. 受注No形式チェック ──────────────────────────────────────
            if not self._is_valid_new_row_order_no(order_no):
                return_reason = "return_invalid_order_no"
                self._log_new_row_event(
                    "new_row_order_no_invalid",
                    order_no=order_no,
                    validation_result="invalid_format",
                    return_reason=return_reason,
                )
                self._set_new_row_status(
                    f"受注No形式エラー: {order_no} ({return_reason})", error=True
                )
                QMessageBox.warning(
                    self,
                    "入力エラー",
                    f"受注No「{order_no}」の形式が正しくありません。",
                )
                return
            self._log_new_row_event(
                "new_row_after_validation",
                order_no=order_no,
                validation_result="ok",
            )

            # ── 6. 重複チェック ────────────────────────────────────────────
            self._log_new_row_event(
                "new_row_before_duplicate_check", order_no=order_no
            )
            duplicate_row = self._duplicate_order_no_row(order_no, exclude=rw)
            self._log_new_row_event(
                "new_row_after_duplicate_check",
                order_no=order_no,
                duplicate=(duplicate_row is not None),
            )
            if duplicate_row is not None:
                return_reason = "return_duplicate_order_no"
                self._log_new_row_event(
                    "new_row_duplicate_detected",
                    order_no=order_no,
                    duplicate=True,
                    validation_result="duplicate",
                    return_reason=return_reason,
                )
                self._set_new_row_status(
                    f"重複: {order_no} は既に一覧にあります ({return_reason})",
                    error=True,
                )
                QMessageBox.warning(
                    self,
                    "伝票作成・印刷",
                    f"受注No「{order_no}」はすでに一覧に存在します。",
                )
                return

            # ── 7. OLAP取得（正常系は必ずここへ到達する）─────────────────
            # v4: 新規入力行の取得は「データだけ」で行う（data-only-fetch）。
            # 新規入力行は通常行ではなく、仕上日(QDateEdit)/AM・PM(QRadioButton)/
            # 加工名・印刷する伝票(QCheckBox) のセルは無効・空欄で、再描画時に
            # setCellWidget で差し替えられ削除され得る。したがってここでは Qt
            # ウィジェットを一切読まず、既定値から純データの row settings を作る。
            # OLAP取得が成功して初めて通常行ウィジェットを生成する（下記メソッド内）。
            # ここから先で例外が出ると「受注No読取OK」で止まって見えるため、
            # 例外時は必ず except 側で理由を画面・ログに残す（return_before_olap）。
            return_reason = "return_before_olap"
            (self._new_order_no_edit or rw.order_input).setText(order_no)
            self._log_new_row_event(
                "new_row_before_olap_status", order_no=order_no
            )
            settings = self._create_default_row_settings_for_new_fetch(order_no)
            self._log_new_row_event(
                "new_row_before_olap_fetch_call", order_no=order_no
            )
            self._perform_olap_fetch_for_new_order(order_no, settings)
            self._log_new_row_event(
                "new_row_after_olap_fetch_call", order_no=order_no
            )
            return_reason = "return_delegated_to_perform"
        except Exception as exc:  # noqa: BLE001 - 沈黙で止めず必ず画面へ出す
            # OLAP取得呼び出し自体は _perform_olap_fetch 内で失敗を処理するため、
            # ここへ来るのは主に取得開始前の準備段階（行データ収集など）での例外。
            if return_reason not in ("return_before_olap",):
                return_reason = "return_exception"
            self._set_new_row_status(
                f"例外発生: {type(exc).__name__}: {exc} ({return_reason})",
                error=True,
            )
            self._log_new_row_event(
                "new_row_exception",
                order_no=order_no,
                validation_result="exception",
                duplicate=None,
                olap_result_success=False,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                error_message=str(exc),
                traceback=traceback_module.format_exc(),
                return_reason=return_reason,
            )
            QMessageBox.critical(
                self,
                "OLAP取得エラー",
                f"OLAP取得を開始できませんでした:\n{type(exc).__name__}: {exc}",
            )
        finally:
            self._log_new_row_event(
                "new_row_fetch_return_outer", return_reason=return_reason
            )

    def _is_valid_new_row_order_no(self, order_no: str) -> bool:
        """新規入力行の受注Noが取得可能な形式か軽く検証する。

        受注Noは半角/全角数字を主とする英数字（区切りのハイフンを許容）。
        正規化後に英数字・ハイフン以外を含む場合のみ形式エラーとする。実運用の
        受注No（例: 1394160）は通過させ、明らかな誤入力だけを弾く。
        """
        normalized = normalize_order_no(order_no)
        if not normalized:
            return False
        return all(ch.isalnum() or ch == "-" for ch in normalized)

    def _create_default_row_settings_for_new_fetch(
        self, order_no: str
    ) -> VoucherOrderRow:
        """新規入力行の取得成功で追加する通常行の初期設定を既定値から作る。

        新規入力行の仕上日/AM・PM/加工名/印刷する伝票のセルは無効化・空欄であり、
        再描画でこれらのチェックボックス等が削除される可能性がある。そのため取得処理
        では新規入力行のUIウィジェットを一切読まず（`_collect_row(rw)` を呼ばない）、
        保存済み設定・既定値から VoucherOrderRow を組み立てる。これにより削除済み
        QCheckBox 参照（RuntimeError: C++ object already deleted）を構造的に避ける。

        既定値は `_add_row` が新規通常行へ適用する初期状態と一致させる：
        仕上日なし/仕上日=本日、AM/PM初期値、加工名すべてOFF、印刷する伝票は
        「印刷する伝票設定」の保存値。
        """
        finish_none = normalize_finish_date_none(self._default_finish_date_none)
        finish_date = None if finish_none else date.today()
        if self._default_ampm == "none":
            am_pm = "none"
        elif self._default_ampm == "pm":
            am_pm = "PM"
        else:
            am_pm = "AM"
        return VoucherOrderRow(
            order_no=order_no,
            finish_date=finish_date,
            am_pm=am_pm,
            # 加工名チェックは初期値すべてOFF（_add_row と一致）。
            process_checks={name: False for name in PROCESS_NAMES},
            # 印刷する伝票の初期チェックは「印刷する伝票設定」の保存値に従う。
            voucher_checks={
                vid: (vid in self._default_print_types) for vid, _ in VOUCHER_TYPES
            },
            finish_date_none=finish_none,
        )

    def _perform_olap_fetch_for_new_order(
        self, order_no: str, settings: VoucherOrderRow
    ) -> None:
        """新規入力行の受注No取得を「データだけ」で行う（v4: data-only-fetch）。

        Qtウィジェットを一切読まない。OLAP取得（純データ）が成功して初めて
        `_add_row` で通常行ウィジェットを生成し、その行の値はUIから読み取らず
        `settings`（既定値から作成済み）とOLAPデータから流し込む。これにより
        再描画で削除された QDateEdit/QCheckBox/QRadioButton を参照して
        RuntimeError（Internal C++ object already deleted）になる事故を構造的に防ぐ。

        失敗時は通常行を追加せず、新規入力行の受注No入力値も残す。エラー表示は
        Qt参照エラーではなくOLAP取得そのものの理由（受注No不明・接続失敗等）を出す。
        """
        # 取得ボタンは安定保持している参照を使う（rw属性は読まない）。
        button = self._new_fetch_button
        if button is not None:
            button.setEnabled(False)
            button.setText("取得中...")
        success = False
        created_row: _RowWidgets | None = None
        return_reason = "return_unknown"
        rows_count_before = len(self._rows)
        self._set_busy(f"OLAP取得中: {order_no}", context="new_row_olap")
        try:
            self._set_new_row_status(f"OLAP取得中: {order_no}")
            self._log_new_row_event(
                "new_row_olap_fetch_start",
                order_no=order_no,
                olap_fetch_started=True,
                rows_count_before=rows_count_before,
            )
            # ── OLAP取得のみ（純データ・ウィジェット非依存）──
            data = self._build_print_data([order_no])
            self._set_new_row_status("OLAP取得成功")
            self._log_new_row_event(
                "new_row_olap_fetch_success", olap_result_success=True
            )
            # ── OLAP成功後に初めて通常行ウィジェットを生成する ──
            self._log_new_row_event(
                "new_row_append_start", rows_count_before=rows_count_before
            )
            created_row = self._add_row()
            # 受注Noは QLineEdit への「書き込み」のみ（既存値の読み取りはしない）。
            created_row.order_input.setText(order_no)
            # UIを読まず、既定値から作った settings と OLAPデータを合成して保持する。
            # （_add_row が既に settings と同じ既定値を created_row の各ウィジェットへ
            #   適用済みのため、表示上も仕上日/AM・PM/印刷する伝票が既定値で反映される）
            self._attach_row_settings(data, settings)
            created_row.cached_olap = data
            self._cache_row_olap(settings, data)
            # 新規入力行の受注No欄をクリア（QLineEdit への書き込みのみ）。
            if self._new_order_no_edit is not None:
                self._new_order_no_edit.clear()
            success = True
            return_reason = "return_success"
            self._set_new_row_status(f"行追加OK: {order_no}")
            self._log_new_row_event(
                "new_row_append_success",
                appended_order_no=order_no,
                append_row_called=True,
                rows_count_before=rows_count_before,
                rows_count_after=len(self._rows),
            )
        except MissingVoucherNoError as exc:
            return_reason = "return_olap_failed"
            self._set_new_row_status(f"OLAP取得失敗: {exc}", error=True)
            self._log_new_row_event(
                "new_row_olap_fetch_failed",
                olap_result_success=False,
                olap_error_message=str(exc),
                error_message=str(exc),
                exception_type=type(exc).__name__,
                return_reason=return_reason,
            )
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            return_reason = "return_olap_failed"
            self._set_new_row_status(f"OLAP取得失敗: {exc}", error=True)
            self._log_new_row_event(
                "new_row_olap_fetch_failed",
                olap_result_success=False,
                olap_error_message=str(exc),
                error_message=str(exc),
                exception_type=type(exc).__name__,
                return_reason=return_reason,
            )
            QMessageBox.critical(
                self,
                "OLAP取得エラー",
                f"OLAP取得に失敗しました:\n{exc}\n\n"
                "ログイン情報や受注Noを確認してください。",
            )
        except Exception as exc:
            return_reason = "return_exception"
            self._set_new_row_status(
                f"例外発生: {type(exc).__name__}: {exc}", error=True
            )
            self._log_new_row_event(
                "new_row_exception",
                order_no=order_no,
                olap_result_success=False,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                error_message=str(exc),
                traceback=traceback_module.format_exc(),
                return_reason=return_reason,
            )
            QMessageBox.critical(
                self,
                "OLAP取得エラー",
                f"OLAP取得中に予期しないエラーが発生しました:\n{exc}",
            )
        finally:
            self._clear_busy(context="new_row_olap")
            if button is not None:
                button.setEnabled(True)
                button.setText("取得")
            if created_row is not None:
                self._refresh_row_olap_state(created_row)
            if not getattr(self, "_restoring_records", False):
                if success and created_row is not None:
                    created_row.updated_at = datetime.now()
                    self._log_new_row_event("new_row_redraw_start")
                self._apply_filters()
                if success and created_row is not None:
                    # 追加行が現在のフィルターで隠れないよう必ず可視化し、
                    # 該当行までスクロールして選択する（実機で「追加されない」対策）。
                    self._force_show_added_row(created_row, order_no)
                    self._log_new_row_event(
                        "new_row_redraw_done",
                        redraw_called=True,
                        rows_count_after=len(self._rows),
                        visible_row_count=self._visible_row_count(),
                        visible_order_numbers_after_redraw=self._visible_order_numbers(),
                    )
                self._save_records_if_ready()
            self._log_new_row_event(
                "new_row_fetch_return", return_reason=return_reason
            )

    def _on_refetch_existing_row(self, rw: _RowWidgets) -> None:
        """通常行（table row >= 1 / _rows[table row - 1]）のOLAP取得・更新処理（要件2・6）。

        仕上日・AM/PM・加工名・印刷する伝票の現在設定はウィジェットを触らないため
        そのまま維持される。また指図書編集の編集オブジェクト
        （work/voucher_edit_objects 配下）は一切削除・クリアしない。再取得するのは
        OLAP由来の伝票データだけで、編集オブジェクトは再利用できるよう残す。
        """
        was_new_unfetched = not self._row_has_olap_data(rw)
        row = self._collect_row(rw)
        if not row.order_no:
            # 受注Noが空の場合は再取得しない。
            QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
            return
        # 他行に同じ受注Noがあれば取得しない（受注Noは一覧内で一意: 要件2・3）。
        # 自分自身の行は除外する。受注Noは編集可能のまま・仕上日以右は無効のまま。
        if self._duplicate_order_no_row(row.order_no, exclude=rw) is not None:
            QMessageBox.warning(
                self,
                "伝票作成・印刷",
                f"受注No「{row.order_no}」はすでに一覧に存在します。",
            )
            return
        self._perform_olap_fetch(
            rw, row, was_new_unfetched=was_new_unfetched
        )

    def _perform_olap_fetch(
        self,
        rw: _RowWidgets,
        row: VoucherOrderRow,
        *,
        was_new_unfetched: bool,
    ) -> None:
        """通常行（既存行）の受注NoでOLAP取得・更新を行う。

        対象行 `rw` は通常行で、`row` は `_collect_row(rw)` 済みの設定値。取得した
        OLAPデータを対象行へ反映するのみで、行の追加・削除はしない。取得失敗時は
        既存の設定・編集内容を壊さずメッセージのみ表示する。

        なお新規入力行の取得はこの共通処理を使わず、UIを一切読まない
        `_perform_olap_fetch_for_new_order` を使う（削除済みウィジェット参照回避）。
        """
        button = rw.refetch_button
        button.setEnabled(False)
        button.setText("取得中...")
        success = False
        self._set_busy(f"OLAP取得中: {row.order_no}", context="row_olap")
        try:
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            # 再取得したOLAPデータを行に保持し、受注No単位のキャッシュを更新する。
            # 編集オブジェクトには触れないため指図書編集内容は維持される。
            rw.cached_olap = data
            self._cache_row_olap(row, data)
            success = True
            if not was_new_unfetched:
                QMessageBox.information(
                    self,
                    "OLAP更新完了",
                    f"OLAPデータを更新しました。\n受注No：{row.order_no}",
                )
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            # 取得失敗時は既存データ（設定・編集内容）を壊さずメッセージのみ表示する。
            QMessageBox.critical(
                self,
                "OLAP取得エラー",
                f"OLAP取得に失敗しました:\n{exc}\n\n"
                "ログイン情報や受注Noを確認してください。",
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "OLAP取得エラー", f"OLAP取得中に予期しないエラーが発生しました:\n{exc}"
            )
        finally:
            self._clear_busy(context="row_olap")
            button.setEnabled(True)
            self._refresh_row_olap_state(rw)
            if row.order_no and not getattr(self, "_restoring_records", False):
                if success and was_new_unfetched:
                    rw.updated_at = datetime.now()
                self._apply_filters()
                self._save_records_if_ready()

    def _visible_order_numbers(self) -> list[str]:
        """フィルターで非表示にされていない通常行の受注No一覧を返す。"""
        result: list[str] = []
        for rw in self._rows:
            if self._is_new_input_row(rw):
                continue
            idx = getattr(rw, "table_row_index", -1)
            if idx >= 0 and not self._table.isRowHidden(idx):
                result.append(rw.order_input.text().strip())
        return result

    def _force_show_added_row(self, created_row: _RowWidgets, order_no: str) -> None:
        """新規取得で追加した通常行を必ず画面に表示し、スクロール・選択する（要件7）。

        実機では検索欄や登録状態フィルターの条件が残っていると、取得直後の行が
        setRowHidden で隠れ「追加されない」ように見える。ここでは条件の有無に
        かかわらずフィルターを初期化して再描画し、追加行を探してスクロール・選択する。
        """
        # 検索欄と登録状態フィルターをシグナル抑止で初期化し、一度だけ再適用する。
        search_before = (
            self._order_search_edit.text() if hasattr(self, "_order_search_edit") else ""
        )
        status_before = (
            self._status_filter.currentText() if hasattr(self, "_status_filter") else ""
        )
        if hasattr(self, "_order_search_edit"):
            self._order_search_edit.blockSignals(True)
            self._order_search_edit.clear()
            self._order_search_edit.blockSignals(False)
        if hasattr(self, "_status_filter"):
            self._status_filter.blockSignals(True)
            self._status_filter.setCurrentText("すべて")
            self._status_filter.blockSignals(False)
        self._apply_filters()
        self._log_new_row_event(
            "filter_reset_done",
            search_filter_text_before=search_before,
            search_filter_text_after=(
                self._order_search_edit.text() if hasattr(self, "_order_search_edit") else ""
            ),
            registration_filter_before=status_before,
            registration_filter_after=(
                self._status_filter.currentText() if hasattr(self, "_status_filter") else ""
            ),
        )

        # 追加行を探す。まずオブジェクト一致、無ければ受注No一致で再取得する。
        target = created_row if created_row in self._rows else self._find_row_widget_by_order(order_no)
        idx = getattr(target, "table_row_index", -1) if target is not None else -1
        found = target is not None and idx >= 0
        visible = found and not self._table.isRowHidden(idx)
        self._log_new_row_event(
            "added_row_found_after_redraw",
            added_row_found=found,
            added_row_table_index=idx,
            added_row_visible=visible,
        )
        if not found:
            self._set_new_row_status(
                f"追加行が見つかりません（要調査）: {order_no}", error=True
            )
            return
        # 念のため対象行の非表示を明示解除してからスクロール・選択する。
        if self._table.isRowHidden(idx):
            self._table.setRowHidden(idx, False)
        self._scroll_to_table_row(idx)
        self._table.selectRow(idx)
        self._log_new_row_event(
            "scroll_to_added_row_done",
            added_row_table_index=idx,
            added_row_visible=not self._table.isRowHidden(idx),
        )

    def _scroll_to_table_row(self, row_index: int) -> None:
        """セルウィジェットを使うテーブルで指定行を確実に可視位置へスクロールする。

        本テーブルは QTableWidgetItem ではなく setCellWidget を使うため、item は
        None になり scrollToItem が効かない。モデルインデックス経由でスクロールする。
        """
        if row_index is None or row_index < 0:
            return
        try:
            index = self._table.model().index(row_index, 0)
            self._table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        except Exception:  # noqa: BLE001 - スクロール失敗は致命的でない
            logging.getLogger("tks_to_kintone_app").debug(
                "追加行へのスクロールに失敗しました。", exc_info=True
            )

    def _on_edit_order_sheet(self, rw: _RowWidgets) -> None:
        """編集画面を先に表示し、現在伝票PDFはworkerで最優先生成する。"""
        request_started = time.perf_counter()
        logger = logging.getLogger("tks_to_kintone_app")
        logger.info("event=perf_voucher_editor phase=button_pressed elapsed_ms=0")
        row = self._collect_row(rw)
        if not row.order_no:
            QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
            return
        from app.voucher_edit_window import VoucherEditWindow
        cached = copy.deepcopy(rw.cached_olap) if isinstance(rw.cached_olap, dict) else None
        voucher_nos = []
        for page in (cached or {}).get("pages") or []:
            if isinstance(page, dict):
                number = str(page.get("voucher_no") or "").strip()
                if number not in voucher_nos:
                    voucher_nos.append(number)
        if not voucher_nos:
            voucher_nos = [""]
        editor = VoucherEditWindow(
            order_no=row.order_no, background_pdf_bytes=b"", parent=self,
            voucher_nos=voucher_nos,
            background_pdf_by_voucher={}, defer_background=True,
            request_started=request_started)
        editor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        editor.voucherEditSaved.connect(self._on_voucher_edit_saved)
        # 参照を保持してGCを防ぐ。
        self._editor_window = editor
        # タイトルバー付きの最大化表示を標準にする（全画面はボタンで切替: 要件2-1・2-2）。
        editor.showMaximized()
        logger.info(
            "event=perf_voucher_editor phase=show_called elapsed_ms=%.3f",
            (time.perf_counter() - request_started) * 1000.0,
        )
        editor.statusBar().showMessage("背景を読み込んでいます…")

        self._editor_load_generation += 1
        generation = self._editor_load_generation
        thread = QThread()
        worker = _VoucherEditorDataWorker(
            generation, row, cached, self.olap_login_id, self.olap_password)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.current_ready.connect(self._on_editor_current_background_ready)
        worker.all_ready.connect(self._on_editor_all_backgrounds_ready)
        worker.failed.connect(self._on_editor_background_failed)
        worker.completed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda g=generation: self._editor_workers.pop(g, None))
        _register_background_settings_thread(thread)
        self._editor_workers[generation] = (thread, worker)
        editor.destroyed.connect(lambda _obj=None, g=generation: self._cancel_editor_worker(g))
        thread.start()

    @Slot(str, str, str, str, int)
    def _on_voucher_edit_saved(
        self, order_no: str, voucher_no: str, edit_objects_hash: str,
        trace_id: str, edit_revision: int,
    ) -> None:
        """保存通知で古いsnapshot/背景世代を失効し、次のPDF要求へtraceを渡す。"""
        order_key = str(order_no or "").strip()
        self._edit_render_context_by_order[order_key] = {
            "trace_id": str(trace_id),
            "edit_objects_sha256": str(edit_objects_hash),
            "edit_revision": int(edit_revision),
            "voucher_no": str(voucher_no or ""),
        }
        # 編集画面起動時の背景先読みworkerは保存後のPDF要求へ流用させない。
        self._editor_load_generation += 1
        for generation, (_thread, worker) in list(self._editor_workers.items()):
            worker.cancel()
            logging.getLogger("tks_to_kintone_app").info(
                "event=voucher_edit_background_generation_invalidated "
                "trace_id=%s generation=%s order_no=%s",
                trace_id, generation, order_key,
            )
        # 行キャッシュ内に古い事前解決edit_objectsがあれば必ず除去する。
        for rw in self._rows:
            if rw.order_input.text().strip() != order_key:
                continue
            cached = rw.cached_olap
            if isinstance(cached, dict):
                cached.pop("edit_objects", None)
                for page in cached.get("pages") or []:
                    if isinstance(page, dict):
                        page.pop("edit_objects", None)
                        page.pop("_edit_objects_sha256", None)
                        page.pop("_edit_data_revision", None)
        editor = getattr(self, "_editor_window", None)
        if editor is not None:
            editor._background_load_generation += 1
            editor.invalidate_preview_cache(voucher_no)
        logging.getLogger("tks_to_kintone_app").info(
            "event=voucher_edit_saved_notified trace_id=%s order_no=%s "
            "voucher_no=%s edit_data_revision=%s edit_objects_sha256=%s "
            "cached_snapshot_invalidated=true pixmap_cache_invalidated=true",
            trace_id, order_key, voucher_no, edit_revision, edit_objects_hash,
        )

    def _edit_render_context(self, order_no: str) -> dict[str, object]:
        context = dict(self._edit_render_context_by_order.get(
            str(order_no or "").strip(), {}))
        context.setdefault("trace_id", str(uuid.uuid4()))
        return context

    def _cancel_editor_worker(self, generation: int) -> None:
        pair = self._editor_workers.get(generation)
        if pair is not None:
            pair[1].cancel()

    @Slot(int, object, str, bytes)
    def _on_editor_current_background_ready(
        self, generation: int, voucher_nos: object, voucher_no: str, pdf_bytes: bytes
    ) -> None:
        editor = getattr(self, "_editor_window", None)
        if generation != self._editor_load_generation or editor is None:
            return
        try:
            editor.set_voucher_numbers(list(voucher_nos) if isinstance(voucher_nos, list) else [voucher_no])
            editor.set_background_pdf_async(voucher_no, pdf_bytes)
            editor.statusBar().showMessage("現在の伝票背景を読み込んでいます…")
        except RuntimeError:
            self._cancel_editor_worker(generation)

    @Slot(int, object)
    def _on_editor_all_backgrounds_ready(self, generation: int, backgrounds: object) -> None:
        editor = getattr(self, "_editor_window", None)
        if generation != self._editor_load_generation or editor is None or not isinstance(backgrounds, dict):
            return
        try:
            for number, pdf_bytes in backgrounds.items():
                editor._background_pdf_by_voucher[voucher_key_for(number)] = bytes(pdf_bytes)
            editor.statusBar().showMessage("全背景の準備が完了しました", 3000)
        except RuntimeError:
            pass

    @Slot(int, str)
    def _on_editor_background_failed(self, generation: int, message: str) -> None:
        editor = getattr(self, "_editor_window", None)
        if generation != self._editor_load_generation or editor is None:
            return
        try:
            editor.statusBar().showMessage(f"背景の読み込みに失敗しました: {message}")
        except RuntimeError:
            pass

    # ── OLAPキャッシュ ────────────────────────────────────────────────────────
    def _cleanup_expired_cache(self) -> None:
        try:
            from app import voucher_cache

            voucher_cache.cleanup_expired_cache(load_cache_retention_days())
            self._cleanup_expired_saved_records()
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("期限切れOLAPキャッシュ削除に失敗しました。")

    def _deferred_cleanup_expired_olap_cache(self) -> None:
        """期限切れOLAPキャッシュ（ディスク走査）を削除する。起動後に遅延実行する（要件2）。

        表示行に影響しないディスク走査のみを扱う。保存レコードの整理は起動時に
        既に実行済みのため、ここでは重い OLAP キャッシュ掃除だけを行う。
        """
        def _run() -> None:
            try:
                from app import voucher_cache

                voucher_cache.cleanup_expired_cache(load_cache_retention_days())
            except Exception:
                logging.getLogger("tks_to_kintone_app").exception(
                    "期限切れOLAPキャッシュ削除に失敗しました。"
                )

        self._timed_step("voucher_window_cleanup_expired_cache", _run)

    # 1チャンク（バッチ）あたりの復元行数。10件ごとに追加し、各バッチ後にUIイベントを
    # 処理して進捗を反映する。最初のバッチ表示で操作可能にする（要件2・3）。
    _SAVED_ROWS_RESTORE_CHUNK_SIZE = SAVED_ROWS_RESTORE_BATCH_SIZE

    def _ensure_saved_rows_restored(self) -> None:
        """テスト/旧内部API用の明示的同期復元。

        実際のGUI操作経路からは呼ばない。通常動作は段階読込と後着重複排除を使う。
        """
        if getattr(self, "_saved_rows_restored", False):
            return
        self._log_voucher_event("voucher_window_saved_rows_operation_requested_while_loading")
        if getattr(self, "_deferred_restore_active", False):
            while getattr(self, "_deferred_restore_active", False):
                if not self._process_saved_rows_chunk():
                    self._finish_deferred_saved_rows_restore()
                    break
            return
        # worker結果との競合をgeneration/cancelで破棄し、互換API要求分を同期復元する。
        if getattr(self, "_deferred_restore_worker_active", False):
            self._saved_rows_worker_cancelled = True
            self._deferred_restore_worker_active = False
            thread = getattr(self, "_saved_rows_thread", None)
            if thread is not None:
                thread.quit()
        self._saved_rows_restored = True
        self._restore_saved_records()
        self._hide_saved_rows_progress()
        self._set_saved_rows_restore_controls_enabled(True)

    def _restore_saved_records_after_show(self) -> None:
        """画面表示後に保存済み一覧をバッチ復元する（進捗表示付き・要件1・2）。

        __init__ では実行せず、show 後の singleShot(0) から1回だけ呼ぶ。
        既に復元済み／復元中なら何もしない。
        """
        if getattr(self, "_saved_rows_restored", False):
            return
        if getattr(self, "_deferred_restore_active", False):
            return
        if getattr(self, "_deferred_restore_worker_active", False):
            return
        _first_show_ms = int(
            (time.perf_counter() - getattr(self, "_init_finished_perf", time.perf_counter())) * 1000
        )
        self._log_voucher_event("voucher_window_first_show_elapsed_ms", elapsed_ms=_first_show_ms)
        _until_first_paint_ms = int(
            (time.perf_counter() - getattr(self, "_startup_perf", time.perf_counter())) * 1000
        )
        self._log_voucher_event(
            "voucher_window_startup_until_first_paint_elapsed_ms", elapsed_ms=_until_first_paint_ms
        )
        self._begin_deferred_saved_rows_restore()

    def _begin_deferred_saved_rows_restore(self) -> None:
        """ファイル読み込みをワーカースレッドで開始する（要件2・4）。

        ワーカーは正規化済みレコードの list だけを返す。UI（テーブル行追加）は
        _on_saved_records_loaded 以降のメインスレッド側で10件バッチで行う。
        """
        if getattr(self, "_deferred_restore_worker_active", False):
            return
        if getattr(self, "_deferred_restore_active", False):
            return
        # 復元開始時点では主要操作を一時無効化し、最初のバッチ表示後に再有効化する（要件2・3）。
        self._set_saved_rows_restore_controls_enabled(False)
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_controls_disabled_until_first_batch"
        )
        # 総件数が判明するまで busy 表示（0/0）にしておく。
        self._show_saved_rows_progress_indeterminate()
        self._saved_rows_worker_cancelled = False
        self._deferred_restore_worker_active = True
        self._deferred_restore_first_batch_done = False
        self._saved_rows_first_batch_perf = time.perf_counter()
        self._saved_rows_all_batches_perf = self._saved_rows_first_batch_perf
        self._log_voucher_event("voucher_window_saved_rows_worker_started")
        try:
            path = self._records_path()
            retention_days = load_record_retention_days()
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception(
                "保存済み一覧の読み込み準備に失敗しました。"
            )
            self._deferred_restore_worker_active = False
            self._hide_saved_rows_progress()
            self._set_saved_rows_restore_controls_enabled(True)
            self._saved_rows_restored = True
            return
        thread = QThread()
        worker = _SavedRecordsLoadWorker(path, retention_days)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.loaded.connect(self._on_saved_records_loaded)
        worker.failed.connect(self._on_saved_records_load_failed)
        # ワーカー完了でスレッドを終了し、参照を安全に解放する（要件4）。
        worker.loaded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_saved_rows_worker_thread_finished)
        self._saved_rows_thread = thread
        self._saved_rows_worker = worker
        # ウィンドウが先に破棄されてもスレッドが安全に完走できるよう参照を保持する（要件4）。
        _register_background_settings_thread(thread)
        self._log_voucher_event("voucher_window_saved_rows_worker_thread_started")
        thread.start()

    @Slot(object)
    def _on_saved_records_loaded(self, records: object) -> None:
        """ワーカーが読み込んだレコードをメインスレッドで受け取り、バッチ追加を始める（要件2・4）。"""
        if not getattr(self, "_alive", True) or getattr(self, "_saved_rows_worker_cancelled", False):
            # ウィンドウclose中／競合ガードで同期完了済み: 結果を破棄する（要件4）。
            self._log_voucher_event("voucher_window_saved_rows_worker_result_ignored_closed")
            return
        self._deferred_restore_worker_active = False
        records = list(records) if isinstance(records, list) else []
        total = len(records)
        self._log_voucher_event(
            "voucher_window_saved_rows_worker_batch_loaded", total=total
        )
        _perf_voucher_list(
            "first_10_records_fetched", self._perf_list_started,
            count=min(INITIAL_INTERACTIVE_ROW_COUNT, total), total=total,
        )
        if total == 0:
            # 0件は進捗表示を出さず即完了（表示が残らない・要件1）。
            self._hide_saved_rows_progress()
            self._set_saved_rows_restore_controls_enabled(True)
            self._saved_rows_restored = True
            self._update_selection_state()
            self._join_saved_rows_thread()
            self._log_voucher_event("voucher_window_saved_rows_worker_finished")
            self._log_voucher_event("voucher_window_saved_rows_restore_completed_after_show")
            return
        self._deferred_restore_records = records
        self._deferred_restore_total = total
        self._deferred_restore_done = 0
        self._deferred_restore_active = True
        self._deferred_restore_finished = False
        self._deferred_restore_first_batch_done = False
        self._saved_rows_first_batch_perf = time.perf_counter()
        self._saved_rows_all_batches_perf = self._saved_rows_first_batch_perf
        self._show_saved_rows_progress(total)
        # バッチ追加中は行単位の重処理を抑制する。ただしテーブル更新は各バッチごとに
        # 有効化して最初の10件を可視化する（hold_updates=False・要件2/3）。
        self._enter_bulk_restore(total, hold_updates=False)
        self._log_voucher_event("voucher_window_saved_rows_restore_progress_started", total=total)
        QTimer.singleShot(0, self._restore_saved_rows_chunk)

    @Slot(str)
    def _on_saved_records_load_failed(self, message: str) -> None:
        """ワーカー読み込み失敗時: ログとステータスを出し、操作を必ず再有効化する（要件2・4）。"""
        self._deferred_restore_worker_active = False
        self._log_voucher_event("voucher_window_saved_rows_worker_failed", error=str(message))
        logging.getLogger("tks_to_kintone_app").error(
            "保存済み一覧の読み込みに失敗しました: %s", message
        )
        self._hide_saved_rows_progress()
        self._set_saved_rows_restore_controls_enabled(True)
        self._saved_rows_restored = True
        self._update_selection_state()
        self._join_saved_rows_thread()
        label = getattr(self, "_new_row_status_label", None)
        if label is not None:
            label.setText("保存済み一覧の読み込みに失敗しました。再起動して再試行してください。")

    def _on_saved_rows_worker_thread_finished(self) -> None:
        """ワーカースレッド終了時に参照を解放する（要件4）。"""
        self._saved_rows_thread = None
        self._saved_rows_worker = None
        self._log_voucher_event("voucher_window_saved_rows_worker_thread_finished")
        self._log_voucher_event("voucher_window_saved_rows_worker_cleanup_finished")

    def _join_saved_rows_thread(self, *, wait_ms: int = 0) -> None:
        """終了要求だけを送り、GUIスレッドではwaitしない。"""
        thread = getattr(self, "_saved_rows_thread", None)
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
        except Exception:  # noqa: BLE001 - 停止失敗でUIを落とさない
            pass

    def _restore_saved_rows_chunk(self) -> None:
        """次のバッチを処理し、残りがあれば継続、なければ完了する（singleShotコールバック）。"""
        if not getattr(self, "_deferred_restore_active", False):
            return
        self._log_voucher_event("voucher_window_saved_rows_batch_apply_started")
        try:
            more = self._process_saved_rows_chunk()
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception(
                "保存済み一覧のバッチ復元に失敗しました。"
            )
            label = getattr(self, "_new_row_status_label", None)
            if label is not None:
                label.setText(
                    f"{self._deferred_restore_done}件を表示しました。残りの読み込みに失敗しました。"
                )
            self._finish_deferred_saved_rows_restore()
            return
        self._log_voucher_event("voucher_window_saved_rows_batch_apply_finished")
        if not getattr(self, "_deferred_restore_first_batch_done", False):
            # 最初の10件を表示できた時点で主要操作を有効化する（要件3）。
            self._deferred_restore_first_batch_done = True
            self._enable_controls_after_first_batch()
        if more:
            # 入力・スクロール・表示済み行操作へイベントループを返してから次へ進む。
            QTimer.singleShot(0, self._restore_saved_rows_chunk)
        else:
            self._finish_deferred_saved_rows_restore()

    def _enable_controls_after_first_batch(self) -> None:
        """最初のバッチ表示後に主要操作を有効化する（残りはバックグラウンド継続・要件3）。"""
        self._set_saved_rows_restore_controls_enabled(True)
        # 先頭に表示した行の列表示・新規入力行の高さを整える（残りは最終反映で再度整合）。
        self._apply_column_visibility(source="after_first_batch", force=True)
        self._apply_new_input_row_height()
        _first_batch_ms = int(
            (time.perf_counter() - getattr(self, "_saved_rows_first_batch_perf", time.perf_counter()))
            * 1000
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_first_batch_displayed", done=self._deferred_restore_done
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_first_batch_elapsed_ms", elapsed_ms=_first_batch_ms
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_controls_enabled_after_first_batch"
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_controls_enabled_after_first_batch"
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_loading_continues_in_background",
            remaining=len(self._deferred_restore_records),
        )
        _perf_voucher_list(
            "first_10_rows_ready", self._perf_list_started,
            count=self._deferred_restore_done,
        )
        _perf_voucher_list(
            "first_10_rows_created", self._perf_list_started,
            count=self._deferred_restore_done,
        )
        _perf_voucher_list("pdf_existence_check_deferred", self._perf_list_started)
        _perf_voucher_list("kintone_status_check_deferred", self._perf_list_started)
        _perf_voucher_list("interactive", self._perf_list_started,
                           count=self._deferred_restore_done)

    def _process_saved_rows_chunk(self) -> bool:
        """次のバッチ（最大 _SAVED_ROWS_RESTORE_CHUNK_SIZE=10 行）を処理する。残りがあれば True。

        テーブル更新は各バッチ内で setUpdatesEnabled(False)→追加→(True) と切り替え、
        追加した10件をその都度可視化する（要件2/3）。ワーカースレッドは呼ばれない。
        """
        chunk_start = time.perf_counter()
        size = self._SAVED_ROWS_RESTORE_CHUNK_SIZE
        chunk = self._deferred_restore_records[:size]
        del self._deferred_restore_records[:size]
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_chunk_started", size=len(chunk)
        )
        table = getattr(self, "_table", None)
        if table is not None:
            table.setUpdatesEnabled(False)
        try:
            self._add_saved_records_batch(chunk)
            # 読込途中に変更された検索・登録状態条件を後続行にも即時適用する。
            self._apply_filters(sort_rows=False)
        finally:
            if table is not None:
                table.setUpdatesEnabled(True)
        self._deferred_restore_done += len(chunk)
        self._update_saved_rows_progress(self._deferred_restore_done, self._deferred_restore_total)
        chunk_ms = int((time.perf_counter() - chunk_start) * 1000)
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_chunk_finished", done=self._deferred_restore_done
        )
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_chunk_elapsed_ms", elapsed_ms=chunk_ms
        )
        if self._deferred_restore_done > INITIAL_INTERACTIVE_ROW_COUNT:
            _perf_voucher_list(
                "other_rows_initialized", self._perf_list_started,
                count=self._deferred_restore_done,
            )
        return bool(self._deferred_restore_records)

    def _finish_deferred_saved_rows_restore(self) -> None:
        """バッチ復元の完了処理。列表示/フィルタ等を1回だけ反映し、進捗を消す（要件1・2）。"""
        if getattr(self, "_deferred_restore_finished", False):
            return
        self._deferred_restore_finished = True
        self._deferred_restore_active = False
        try:
            self._exit_bulk_restore()
            self._apply_after_bulk_restore()
        finally:
            # 例外が起きても進捗を消し、操作を必ず再有効化する（要件2）。
            self._hide_saved_rows_progress()
            self._set_saved_rows_restore_controls_enabled(True)
            self._apply_new_input_row_height()
            self._saved_rows_restored = True
            self._update_selection_state()
        _all_ms = int(
            (time.perf_counter() - getattr(self, "_saved_rows_all_batches_perf", time.perf_counter()))
            * 1000
        )
        self._join_saved_rows_thread()
        self._log_voucher_event(
            "voucher_window_saved_rows_all_batches_elapsed_ms", elapsed_ms=_all_ms
        )
        self._log_voucher_event("voucher_window_saved_rows_worker_finished")
        self._log_voucher_event("voucher_window_saved_rows_restore_progress_finished")
        self._log_voucher_event("voucher_window_saved_rows_restore_completed_after_show")
        _perf_voucher_list("all_rows_loaded", self._perf_list_started,
                           count=self._deferred_restore_done)
        _perf_voucher_list("all_background_work_complete", self._perf_list_started)
        status = getattr(self, "_new_row_status_label", None)
        if status is not None and "失敗" not in status.text():
            status.setText(f"{self._deferred_restore_done}件の読み込みが完了しました")

    # ── 保存済み一覧復元の進捗表示・操作抑制 ──────────────────────────────────
    def _show_saved_rows_progress(self, total: int) -> None:
        bar = getattr(self, "_saved_rows_progress", None)
        label = getattr(self, "_saved_rows_progress_label", None)
        if bar is not None:
            bar.setRange(0, total)
            bar.setValue(0)
            bar.setVisible(True)
        if label is not None:
            label.setText(f"保存済み一覧を読み込み中... 0 / {total}")
            label.setVisible(True)

    def _show_saved_rows_progress_indeterminate(self) -> None:
        """総件数が判明する前（ワーカー読み込み中）の busy 進捗表示（要件2）。"""
        bar = getattr(self, "_saved_rows_progress", None)
        label = getattr(self, "_saved_rows_progress_label", None)
        if bar is not None:
            bar.setRange(0, 0)  # busy インジケータ
            bar.setVisible(True)
        if label is not None:
            label.setText("保存済み一覧を読み込み中...")
            label.setVisible(True)

    def _update_saved_rows_progress(self, done: int, total: int) -> None:
        value = min(done, total)
        bar = getattr(self, "_saved_rows_progress", None)
        label = getattr(self, "_saved_rows_progress_label", None)
        if bar is not None:
            bar.setValue(value)
        if label is not None:
            label.setText(f"保存済み一覧を読み込み中... {value} / {total}")
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_progress_updated", done=value, total=total
        )

    def _hide_saved_rows_progress(self) -> None:
        bar = getattr(self, "_saved_rows_progress", None)
        label = getattr(self, "_saved_rows_progress_label", None)
        if bar is not None:
            bar.setVisible(False)
        if label is not None:
            label.setVisible(False)
            label.setText("")

    def _set_saved_rows_restore_controls_enabled(self, enabled: bool) -> None:
        """保存済み一覧復元中に主要操作ボタン等を一時的に無効化/再有効化する（要件2・3）。

        一覧全体を対象にする選択系・削除・設定ボタンだけを無効化する。一番上の
        新規入力行（受注No入力・OLAP取得）は復元中も操作可能にする。全件前提の追加処理は
        入口で _ensure_saved_rows_restored() を呼び、残りを安全に同期完了させる（要件3）。
        """
        for name in (
            "_display_settings_button",
            "_select_pdf_button",
            "_select_preview_button",
            "_select_print_button",
            "_remove_row_button",
            "_select_order_no_button",
        ):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.setEnabled(enabled)
        self._log_voucher_event(
            "voucher_window_saved_rows_restore_controls_enabled"
            if enabled
            else "voucher_window_saved_rows_restore_controls_disabled"
        )

    def _cache_row_olap(self, row: VoucherOrderRow, data: dict) -> None:
        """取得済みOLAPデータを受注Noごとにアプリ内へ保存する。"""
        try:
            from app import voucher_cache

            voucher_cache.save_olap_cache(
                row.order_no,
                raw_rows=data.get("raw_rows") or [],
                pages=data.get("pages") or [],
                request_conditions={"order_no": row.order_no},
                row_settings={
                    "finish_date": row.finish_date,
                    "finish_date_none": bool(row.finish_date_none),
                    "am_pm": row.am_pm,
                    "process_checks": dict(row.process_checks),
                    "voucher_checks": dict(row.voucher_checks),
                },
            )
            rw = self._find_row_widget_by_order(row.order_no)
            if rw is not None:
                rw.cached_olap = data
                self._refresh_row_olap_state(rw)
                self._apply_filters()
            self._save_records()
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception(
                "OLAPキャッシュ保存に失敗しました。受注No=%s", row.order_no
            )

    def _find_row_widget_by_order(self, order_no: str) -> _RowWidgets | None:
        order_no = str(order_no or "").strip()
        for rw in self._rows:
            if rw.order_input.text().strip() == order_no:
                return rw
        return None

    def _select_row_widget(self, rw: _RowWidgets) -> None:
        """指定行をテーブル上で選択し可視位置までスクロールする。"""
        row_index = getattr(rw, "table_row_index", -1)
        if row_index is None or row_index < 0:
            return
        self._table.selectRow(row_index)
        # 本テーブルは setCellWidget を使うため item は None。モデル経由でスクロールする。
        self._scroll_to_table_row(row_index)

    def add_order_no_and_fetch(self, order_no: str) -> dict:
        """外部（TKS受注No取込画面）から受注Noを一覧に追加し、OLAP取得まで行う公開メソッド。

        既存の行追加・OLAP取得処理（_on_refetch_row）を再利用する。
        戻り値 dict["status"]: "added" / "duplicate" / "invalid"。
        既に同じ受注Noが一覧にある場合は重複追加せず、その行を選択する（要件）。
        """
        normalized = str(order_no or "").strip()
        if not normalized:
            return {"status": "invalid", "order_no": ""}

        # 保存済み一覧の遅延復元が未完なら先に確定させる。復元がこの追加処理中に
        # 割り込むと、保存直後のレコードを再取り込みして重複行になるため（要件1）。
        existing = self._duplicate_order_no_row(normalized)
        if existing is not None:
            self._select_row_widget(existing)
            self.show()
            self.raise_()
            return {"status": "duplicate", "order_no": normalized}

        # 外部追加は先頭の入力専用行を使わず、通常行として追加する。
        target = next(
            (
                rw
                for rw in self._rows
                if not self._is_new_input_row(rw)
                and not self._row_has_olap_data(rw)
                and self._is_empty_order_no(rw.order_input.text())
            ),
            None,
        )
        if target is None:
            target = self._add_row()
        target.order_input.setText(normalized)
        # 既存のOLAP取得処理を再利用する（行追加・キャッシュ・保存まで共通化）。
        self._on_refetch_row(target)
        self._select_row_widget(target)
        self.show()
        self.raise_()
        return {"status": "added", "order_no": normalized}

    # ── 選択状態の管理 ────────────────────────────────────────────────────────
    def _selected_indices(self) -> list[int]:
        return [
            i
            for i, rw in enumerate(self._rows)
            if not self._is_new_input_row(rw) and rw.select_check.isChecked()
        ]

    def _on_row_selection_changed(self, _state: int = 0) -> None:
        # bulk復元中は選択状態の再計算を抑制する（復元後に1回だけ反映する・要件1・2）。
        if getattr(self, "_bulk_restoring_saved_rows", False):
            return
        self._update_selection_state()

    def _on_select_all_clicked(self, _checked: bool = False) -> None:
        # tristate チェックの状態遷移に依存せず、現在の選択状況から全選択/全解除を決める。
        # 全行が選択済みなら全解除、それ以外（一部・未選択）なら全選択。
        selectable_count = len([rw for rw in self._rows if not self._is_new_input_row(rw)])
        all_selected = selectable_count > 0 and len(self._selected_indices()) == selectable_count
        self._set_all_rows_checked(not all_selected)

    def _on_header_section_clicked(self, section: int) -> None:
        if section == COL_SELECT:
            self._on_select_all_clicked()

    def _set_all_rows_checked(self, checked: bool) -> None:
        if self._new_input_row is not None:
            self._new_input_row.select_check.blockSignals(True)
            self._new_input_row.select_check.setChecked(False)
            self._new_input_row.select_check.blockSignals(False)
        for rw in self._rows:
            if self._is_new_input_row(rw):
                rw.select_check.blockSignals(True)
                rw.select_check.setChecked(False)
                rw.select_check.blockSignals(False)
                continue
            rw.select_check.blockSignals(True)
            rw.select_check.setChecked(checked)
            rw.select_check.blockSignals(False)
        self._update_selection_state()

    def _update_selection_state(self) -> None:
        """行のチェック状態に合わせて全選択チェックとボタン有効/無効を更新する。"""
        self._update_select_all_check()
        self._update_selection_buttons()

    def _update_select_all_check(self) -> None:
        total = len([rw for rw in self._rows if not self._is_new_input_row(rw)])
        selected = len(self._selected_indices())
        item = self._table.horizontalHeaderItem(COL_SELECT)
        if item is None:
            return
        if total == 0 or selected == 0:
            label = "□"
        elif selected == total:
            label = "☑"
        else:
            label = "◩"
        item.setText(label)

    def _update_selection_buttons(self) -> None:
        has_selection = bool(self._selected_indices())
        for button in (
            self._select_pdf_button,
            self._select_preview_button,
            self._select_print_button,
            self._remove_row_button,
            self._select_order_no_button,
        ):
            button.setEnabled(has_selection)

    # ── 選択削除 ──────────────────────────────────────────────────────────────
    def _on_remove_selected(self) -> None:
        """選択中の行だけを一覧から削除する。

        Kintone登録状態・OLAP元データ・出力済みPDF・ログには一切手を付けず、
        画面上の行と、その行に紐づく取得済みデータ（cached_olap）および
        ローカル保存の一覧データのみを削除する（要件「削除対象/重要」）。
        フィルター・並び替え中でも、選択された実レコード（_RowWidgets）を
        直接対象にするため、表示順や絞り込みに依存せず正しく削除できる。
        """
        rows_to_delete = [
            rw for rw in self._rows if not self._is_new_input_row(rw) and rw.select_check.isChecked()
        ]
        if not rows_to_delete:
            QMessageBox.information(
                self, "選択削除", "削除するレコードを選択してください。"
            )
            return
        count = len(rows_to_delete)
        reply = QMessageBox.question(
            self,
            "選択削除",
            f"選択した {count} 件のレコードを削除します。\nよろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            # 「いいえ」またはキャンセル時は何も変更しない（要件5）。
            return
        self._delete_rows(rows_to_delete)

    def _delete_rows(self, rows_to_delete: list[_RowWidgets]) -> None:
        """指定した行ウィジェットを一覧から取り除き、保存と表示を更新する。"""
        delete_set = {id(rw) for rw in rows_to_delete}
        # テーブルからは論理行（table_row_index）を大きい順に削除し、
        # 削除による index ずれが未処理行に影響しないようにする。
        deleted_logical = sorted(
            (rw.table_row_index for rw in rows_to_delete if rw.table_row_index >= 0)
        )
        for logical_index in sorted(deleted_logical, reverse=True):
            if 0 <= logical_index < self._table.rowCount():
                self._table.removeRow(logical_index)
        # 残す行を _rows から再構築（新規未取得行も rw を破棄するだけで安全）。
        self._rows = [rw for rw in self._rows if id(rw) not in delete_set]
        # 残った行の論理 index を、自分より前に消えた行数だけ詰める。
        for rw in self._rows:
            shift = sum(1 for d in deleted_logical if d < rw.table_row_index)
            rw.table_row_index -= shift
        # 入力用の空行を最低1行残す（要件「削除後…空行が最低1行残る」）。
        self._ensure_new_input_row()
        # 表示・選択・全選択・件数（フィルタログ）・保存を更新する。
        self._apply_filters()
        self._update_selection_state()
        self._save_records()

    # ── 行データ収集 ──────────────────────────────────────────────────────────
    def _collect_row(self, rw: _RowWidgets) -> VoucherOrderRow:
        # 仕上日「なし」選択時は finish_date=None（伝票仕上日欄を空白にする: 要件1）。
        if rw.finish_none_check.isChecked():
            finish_date = None
        else:
            qd = rw.date_edit.date()
            finish_date = date(qd.year(), qd.month(), qd.day()) if qd.isValid() else None
        # AM/PM は なし/AM/PM の3択（要件1）。なしは "none" として後段で丸を描かない。
        if rw.ampm_none.isChecked():
            am_pm = "none"
        elif rw.ampm_pm.isChecked():
            am_pm = "PM"
        else:
            am_pm = "AM"
        return VoucherOrderRow(
            order_no=rw.order_input.text().strip(),
            finish_date=finish_date,
            am_pm=am_pm,
            process_checks={name: cb.isChecked() for name, cb in rw.process_checks.items()},
            voucher_checks={vid: cb.isChecked() for vid, cb in rw.voucher_checks.items()},
            finish_date_none=rw.finish_none_check.isChecked(),
        )

    def closeEvent(self, event) -> None:
        """ウィンドウが閉じられるときに back_requested を emit して起動元に通知する。"""
        range_dialog = getattr(self, "_range_dialog", None)
        if range_dialog is not None:
            try:
                range_dialog.request_cancel()
                range_dialog.hide()
            except Exception:
                pass
        # 保存済み一覧ワーカーが残っていても、結果を破棄しスレッドを安全に停止する（要件4）。
        self._alive = False
        self._saved_rows_worker_cancelled = True
        thread = getattr(self, "_saved_rows_thread", None)
        if thread is not None:
            try:
                thread.quit()
            except Exception:
                pass
            self._log_voucher_event("voucher_window_saved_rows_worker_result_ignored_closed")
        for _generation, (_thread, worker) in list(self._editor_workers.items()):
            worker.cancel()
        for worker in list(getattr(self, "_print_workers", set())):
            try:
                worker.cancel()
            except Exception:
                pass
        self._save_records()
        self.back_requested.emit()
        super().closeEvent(event)

    def _set_print_controls_enabled(self, enabled: bool) -> None:
        self._select_print_button.setEnabled(enabled and bool(self._selected_indices()))
        for rw in self._rows:
            button = getattr(rw, "print_button", None)
            if button is not None:
                button.setEnabled(enabled)

    def _snapshot_print_ui_state(self) -> None:
        widgets: list[QWidget] = [
            self,
            self.centralWidget(),
            self._table,
            self._select_print_button,
            self._select_preview_button,
        ]
        widgets.extend(getattr(rw, "print_button", None) for rw in self._rows)
        widgets.extend(getattr(rw, "preview_button", None) for rw in self._rows)
        self._print_disabled_widget_states = {
            widget: widget.isEnabled() for widget in widgets if widget is not None
        }
        voucher_print_service.log_print_recovery_event(
            "ui_disabled_widgets_before",
            ui_disabled_widgets_before=True,
            disabled_widgets=[
                type(widget).__name__
                for widget, was_enabled in self._print_disabled_widget_states.items()
                if not was_enabled
            ],
            ui_thread_id=threading.get_ident(),
        )

    def _restore_print_ui_state(self, reason: str, status: str = "") -> None:
        self._print_in_progress = False
        for widget, was_enabled in list(getattr(self, "_print_disabled_widget_states", {}).items()):
            try:
                widget.setEnabled(was_enabled)
            except RuntimeError:
                pass
        if not self.isEnabled():
            self.setEnabled(True)
        central = self.centralWidget()
        if central is not None and not central.isEnabled():
            central.setEnabled(True)
        if not self._table.isEnabled():
            self._table.setEnabled(True)
        self._update_selection_buttons()
        if status:
            self._set_print_status(status, error=status.startswith("印刷失敗") or status.startswith("印刷エラー"))
        row_buttons_enabled = all(
            getattr(rw, "print_button", None) is None or rw.print_button.isEnabled()
            for rw in self._rows
        )
        voucher_print_service.log_print_recovery_event(
            "ui_restore_print_state",
            ui_restore_print_state=True,
            ui_restore_reason=reason,
            window_enabled_after_restore=self.isEnabled(),
            table_enabled_after_restore=self._table.isEnabled(),
            row_buttons_enabled_after_restore=row_buttons_enabled,
            select_print_button_enabled_after_restore=self._select_print_button.isEnabled(),
            ui_print_guard_released=True,
            ui_button_enabled=row_buttons_enabled,
        )
        self._print_disabled_widget_states = {}

    def _start_background_print(
        self,
        pdf_bytes: bytes,
        *,
        job_name: str,
        source_type: str | None = None,
        selected_count: int = 1,
        generated_pdf_count: int = 1,
        merged_pdf_created: bool = False,
    ) -> bool:
        from app import voucher_print_service

        self._snapshot_print_ui_state()
        self._print_in_progress = True
        backend = load_voucher_printer_settings().print_backend
        if backend == PRINT_BACKEND_SUMATRA:
            self._set_print_status("SumatraPDFへ印刷要求を送信中...")
        elif backend == PRINT_BACKEND_ACROBAT:
            self._set_print_status("Acrobat Readerへ印刷要求を送信中...")
        else:
            self._set_print_status("印刷要求を送信中...")
        worker = voucher_print_service.start_print_pdf_background(
            pdf_bytes,
            self,
            job_name=job_name,
            source_type=source_type or ("selected" if selected_count > 1 else "row"),
            selected_count=selected_count,
            generated_pdf_count=generated_pdf_count,
            merged_pdf_created=merged_pdf_created,
        )
        if worker is None:
            self._restore_print_ui_state("worker_none", "印刷要求を送信しました")
            return True

        def _status_for(event: str, message: str = "") -> str:
            if backend == PRINT_BACKEND_ACROBAT:
                return {
                    "enqueued": "Acrobat Reader印刷ジョブを登録しました",
                    "request_sent": "Acrobat Readerへ印刷要求を送信しました",
                    "finished": "Acrobat Reader印刷処理完了",
                    "error": f"Acrobat Reader印刷でエラーが発生しました: {message}",
                }[event]
            if backend == PRINT_BACKEND_SUMATRA:
                return {
                    "enqueued": "SumatraPDF印刷ジョブを登録しました",
                    "request_sent": "SumatraPDFへ印刷要求を送信しました",
                    "finished": "SumatraPDF印刷処理完了",
                    "error": f"SumatraPDF印刷でエラーが発生しました: {message}",
                }[event]
            return {
                "enqueued": "印刷ジョブを追加しました",
                "request_sent": "印刷要求を送信しました",
                "finished": "印刷処理完了",
                "error": f"印刷失敗: {message}",
            }[event]

        self._print_workers.add(worker)

        def _on_request_sent(_payload: dict) -> None:
            # Popen成功＝印刷要求送信済み。終了確認は裏で継続するが、
            # ここで印刷中ガードを解除し次の印刷を永久に止めない（連打対策は各ジョブ作成中のみ）。
            self._restore_print_ui_state("request_sent", _status_for("request_sent"))
            voucher_print_service.log_print_recovery_event(
                "ui_request_sent_received",
                trigger="request_sent",
                print_backend=backend,
                ui_request_sent_received=True,
                ui_print_guard_released=True,
                ui_button_enabled=True,
            )

        def _on_finished(_payload: dict) -> None:
            self._print_workers.discard(worker)
            self._restore_print_ui_state("finished", _status_for("finished"))
            voucher_print_service.log_print_recovery_event(
                "ui_print_guard_released",
                trigger="finished",
                print_backend=backend,
                ui_print_guard_released=True,
                ui_button_enabled=True,
            )

        def _on_error(message: str, _payload: dict) -> None:
            # request_sent が来ない Popen前エラーでも、error signal で必ずUIを復帰させる。
            # モーダルは出さず、ステータス表示のみで固まらせない。
            self._print_workers.discard(worker)
            self._restore_print_ui_state("error", _status_for("error", str(message)))
            voucher_print_service.log_print_recovery_event(
                "ui_error_received",
                trigger="error",
                print_backend=backend,
                ui_error_received=True,
                ui_print_guard_released=True,
                ui_button_enabled=True,
                error_message=str(message),
            )

        worker.status_changed.connect(self._set_print_status)
        worker.request_sent.connect(_on_request_sent)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        self._restore_print_ui_state("enqueued", _status_for("enqueued"))
        QTimer.singleShot(30000, lambda: self._restore_print_ui_state("watchdog") if self._print_in_progress else None)
        return True

    def _remember_last_pdf(self, pdf_bytes: bytes = b"", *, pdf_path: str = "", job_name: str = "") -> None:
        if pdf_bytes:
            self._last_pdf_bytes = bytes(pdf_bytes)
        if pdf_path:
            self._last_pdf_path = str(pdf_path)
        if job_name:
            self._last_pdf_job_name = str(job_name)

    def _ensure_test_print_pdf(self, settings) -> tuple[bytes, str, str] | None:
        """テスト印刷用PDFを用意する（既存PDFに依存しない・要件1）。

        テスト印刷は印刷設定の確認用なので、伝票PDFの有無に関わらず必ず設定確認用の
        簡易PDFをその場で生成する。用紙サイズ・印刷方向は画面上の現在値（settings）に
        従う。生成に失敗した場合のみ None を返す（呼び出し側でエラー表示する）。
        戻り値は (pdf_bytes, pdf_path(一時ファイル), job_name)。
        """
        from app import voucher_print_service, voucher_service

        # 既存のテスト印刷用PDFは無い前提で扱う（設定確認が目的のため常に自動生成する）。
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_pdf_missing",
        )
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_pdf_auto_create_started",
            paper_size=getattr(settings, "paper_size", ""),
            orientation=getattr(settings, "orientation", ""),
        )
        try:
            pdf_bytes = voucher_service.build_test_print_pdf_bytes(settings)
        except Exception as exc:  # noqa: BLE001
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_pdf_auto_create_failed",
                error=str(exc),
            )
            return None
        if not pdf_bytes:
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_pdf_auto_create_failed",
                error="empty_pdf_bytes",
            )
            return None

        # 一時ファイルに保存する（印刷が終わるまで残す）。次回起動時などに掃除する。
        pdf_path = ""
        try:
            directory = get_test_print_dir()
            directory.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix="test_print_", suffix=".pdf", dir=str(directory)
            )
            with os.fdopen(fd, "wb") as fp:
                fp.write(pdf_bytes)
            pdf_path = temp_name
            cleanup_old_test_print_pdfs()
        except OSError as exc:
            # 一時ファイル書き出しに失敗してもバイト列があれば印刷は可能。パスは空にする。
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_temp_pdf_write_failed",
                error=str(exc),
            )
            pdf_path = ""

        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_pdf_auto_created",
            pdf_bytes=len(pdf_bytes),
            pdf_path=pdf_path,
        )
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_temp_pdf_path",
            pdf_path=pdf_path,
        )
        return pdf_bytes, pdf_path, "test_print"

    def _enqueue_sumatra_test_print(self, *, settings_override=None) -> bool:
        from app import voucher_print_service

        # テスト印刷は画面上の現在値（settings_override）で印刷する。渡されなければ
        # 保存済み設定を使う（通常経路と同じ挙動）。QSettings への保存はここでは行わない。
        settings = settings_override
        if settings is None:
            settings = load_voucher_printer_settings()

        built = self._ensure_test_print_pdf(settings)
        if built is None:
            self._set_print_status("テスト印刷用PDFを作成できませんでした", error=True)
            voucher_print_service.log_print_settings_event(
                "voucher_print_settings_test_print_failed", reason="build_pdf_failed"
            )
            return False
        pdf_bytes, pdf_path, job_name = built
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_using_generated_pdf",
            job_name=job_name,
            pdf_bytes=len(pdf_bytes or b""),
            pdf_path=pdf_path,
        )
        voucher_print_service.log_print_settings_event(
            "voucher_print_settings_test_print_pdf_created",
            job_name=job_name,
            pdf_bytes=len(pdf_bytes or b""),
            pdf_path=pdf_path,
        )
        # テスト印刷の一時PDFは通常の「最後に作成したPDF」を上書きしない（伝票PDFと区別）。
        self._set_print_status("テスト印刷ジョブを追加しました")
        worker = voucher_print_service.start_print_pdf_background(
            pdf_bytes,
            self,
            job_name=job_name,
            source_type="test",
            selected_count=1,
            generated_pdf_count=1,
            merged_pdf_created=False,
            merged_pdf_path=pdf_path,
            test_print_requested=True,
            test_print_pdf_path=pdf_path,
            settings_override=settings_override,
        )
        if worker is not None:
            self._print_workers.add(worker)
            worker.finished.connect(lambda _payload: self._print_workers.discard(worker))
            worker.error.connect(lambda _message, _payload: self._print_workers.discard(worker))
        return True

    def _set_processing(self, processing: bool) -> None:
        for widget in (
            self._order_search_edit,
            self._status_filter,
            self._pdf_output_dir,
            self._browse_output_button,
            self._table,
        ):
            widget.setEnabled(not processing)
        if processing:
            for button in (
                self._select_pdf_button,
                self._select_preview_button,
                self._select_print_button,
                self._remove_row_button,
                self._select_order_no_button,
            ):
                button.setEnabled(False)
        else:
            # 処理終了後は選択状態に応じてボタン有効/無効を戻す。
            self._update_selection_buttons()
        if getattr(self, "_print_in_progress", False):
            self._set_print_controls_enabled(False)

    # ── PDF作成（行単位）─────────────────────────────────────────────────────
    def _on_pdf(self, rw: _RowWidgets) -> None:
        if not self._row_has_olap_data(rw):
            QMessageBox.warning(self, "入力エラー", "先にOLAPデータを取得してください。")
            return
        row = self._collect_row(rw)
        if not self._validate_row(row):
            return
        ids = [vid for vid, on in row.voucher_checks.items() if on]
        self._set_processing(True)
        self._set_busy("PDF作成中...", context="row_pdf")
        try:
            output_dir = self._resolve_pdf_output_dir()
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            context = self._edit_render_context(row.order_no)
            pdf_path = self._create_pdf(
                ids, data, output_dir=output_dir, open_after=True,
                edit_render_trace_id=str(context["trace_id"]))
            if pdf_path is not None:
                self._remember_last_pdf(pdf_path=str(pdf_path), job_name=row.order_no)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except RuntimeError as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="row_pdf")
            self._set_processing(False)

    # ── プレビュー（行単位）─────────────────────────────────────────────────
    def _on_preview(self, rw: _RowWidgets) -> None:
        if not self._row_has_olap_data(rw):
            QMessageBox.warning(self, "入力エラー", "先にOLAPデータを取得してください。")
            return
        row = self._collect_row(rw)
        if not self._validate_row(row):
            return
        from app.voucher_preview_controller import (
            build_voucher_preview_pdf,
            resolve_preview_voucher_ids,
        )

        ids = resolve_preview_voucher_ids(row.voucher_checks)
        self._set_processing(True)
        self._set_busy("プレビュー生成中...", context="row_preview")
        try:
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            context = self._edit_render_context(row.order_no)
            trace_id = str(context["trace_id"])
            pdf_bytes = build_voucher_preview_pdf(
                ids,
                data,
                edit_render_trace_id=trace_id,
                reload_edit_objects=True,
            )
            self._remember_last_pdf(pdf_bytes, job_name=row.order_no)
            self._open_preview_window(
                pdf_bytes, edit_render_trace_id=trace_id,
                edit_objects_sha=str(context.get("edit_objects_sha256") or ""),
                preview_cache_hit=False)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "プレビューエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "プレビューエラー", f"プレビュー作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="row_preview")
            self._set_processing(False)

    def build_editor_preview_request(
        self, order_no: str, voucher_no: str,
        edit_objects_snapshot: dict[str, object],
        preview_voucher_id: str = "",
    ) -> tuple[list[str], dict]:
        """編集画面用の未保存snapshotを、一覧と同じPDF入力形式へ変換する。"""
        from app.voucher_preview_controller import (
            apply_editor_preview_snapshot,
            resolve_preview_voucher_ids,
        )

        rw = self._find_row_widget_by_order(order_no)
        if rw is None or not isinstance(rw.cached_olap, dict):
            raise RuntimeError("プレビュー用の伝票データが見つかりません。")
        row = self._collect_row(rw)
        data = copy.deepcopy(rw.cached_olap)
        self._attach_row_settings(data, row)
        wanted = voucher_key_for(voucher_no)
        if not any(
            isinstance(page, dict)
            and voucher_key_for(page.get("voucher_no")) == wanted
            for page in data.get("pages") or []
        ):
            raise RuntimeError("現在の伝票Noに対応するプレビューデータが見つかりません。")
        ids = resolve_preview_voucher_ids(row.voucher_checks)
        if not ids:
            raise RuntimeError("プレビュー対象の伝票が選択されていません。")
        # preview_voucher_id は旧呼出し互換のため受け取るが、一覧と同じ対象を使うため
        # 意図的に採用しない。
        return ids, apply_editor_preview_snapshot(data, edit_objects_snapshot)

    # ── 印刷（行単位）────────────────────────────────────────────────────────
    def _on_print(self, rw: _RowWidgets) -> None:
        if not self._row_has_olap_data(rw):
            QMessageBox.warning(self, "入力エラー", "先にOLAPデータを取得してください。")
            return
        row = self._collect_row(rw)
        if not self._validate_row(row):
            return
        ids = [vid for vid, on in row.voucher_checks.items() if on]
        self._set_busy("印刷ジョブを追加中...", context="row_print")
        try:
            from app import voucher_service, voucher_print_service
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            context = self._edit_render_context(row.order_no)
            pdf_bytes = voucher_service.build_vouchers_pdf_bytes(
                ids, data, edit_render_trace_id=str(context["trace_id"]),
                reload_edit_objects=True)
            self._remember_last_pdf(pdf_bytes, job_name=row.order_no)
            settings = load_voucher_printer_settings()
            backend = settings.print_backend
            # 印刷時PDF同時作成: ONなら補正前の通常PDFをPDF出力先へ保存してから印刷する。
            # 保存に失敗した場合は「PDFも作成する」明示があるため安全優先で印刷を中止する。
            if not self._save_pdf_on_print_if_enabled(settings, pdf_bytes, row.order_no):
                return
            if backend == PRINT_BACKEND_SUMATRA:
                status_prefix = "SumatraPDF経由で印刷中"
            elif backend == PRINT_BACKEND_ACROBAT:
                status_prefix = "Acrobat Reader経由で印刷中"
            else:
                status_prefix = "印刷中"
            self._set_print_status(f"{status_prefix}: 受注No {row.order_no}")
            self._log_print_job_context(settings, order_no=row.order_no)
            if backend in (PRINT_BACKEND_ACROBAT, PRINT_BACKEND_SUMATRA):
                # 印刷に渡すのは補正前PDF。SumatraPDF印刷補正は印刷サービス側で内部適用する。
                self._start_background_print(
                    pdf_bytes,
                    job_name=row.order_no,
                    source_type="row",
                    selected_count=1,
                    generated_pdf_count=1,
                    merged_pdf_created=False,
                )
            else:
                voucher_print_service.print_pdf_direct(pdf_bytes, self, job_name=row.order_no)
                self._set_print_status("印刷要求を送信しました")
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            self._set_print_status(f"印刷失敗: {exc}", error=True)
            QMessageBox.critical(self, "印刷エラー", str(exc))
        except Exception as exc:
            self._set_print_status(f"印刷失敗: {exc}", error=True)
            QMessageBox.critical(self, "印刷エラー", f"印刷中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="row_print")

    def _save_pdf_on_print_if_enabled(
        self, settings, pdf_bytes: bytes, order_no: str
    ) -> bool:
        """印刷時PDF同時作成がONなら補正前PDFをPDF出力先へ保存する。

        戻り値は「印刷を続行してよいか」。OFFなら常に True。
        ONで保存に失敗した場合はステータス/ログ/ダイアログで通知し False を返して
        印刷を中止する（PDF作成を明示しているため安全優先）。
        """
        from app import voucher_service, voucher_print_service

        enabled = bool(getattr(settings, "save_pdf_on_print", False))
        voucher_print_service.log_voucher_print_event(
            "save_pdf_on_print_enabled", enabled=enabled, order_no=order_no
        )
        if not enabled:
            return True
        voucher_print_service.log_voucher_print_event(
            "save_pdf_on_print_started", order_no=order_no
        )
        try:
            output_dir = self._resolve_pdf_output_dir()
            # PDF出力先に保存するのは補正前の通常PDF（印刷補正PDFと混同しない）。
            saved_path = voucher_service.save_pdf_bytes(
                pdf_bytes, output_dir=output_dir, filename_token=order_no
            )
        except Exception as exc:  # noqa: BLE001 - 保存失敗は印刷中止として通知する
            voucher_print_service.log_voucher_print_event(
                "save_pdf_on_print_failed", order_no=order_no
            )
            voucher_print_service.log_voucher_print_event(
                "save_pdf_on_print_error_message", order_no=order_no, error_message=str(exc)
            )
            self._set_print_status(
                f"PDF保存に失敗したため印刷を中止しました: {exc}", error=True
            )
            QMessageBox.critical(
                self,
                "PDF保存エラー",
                "印刷時のPDF保存に失敗したため、印刷を中止しました。\n"
                "PDF出力先に書き込みできるか確認してください。\n\n"
                f"詳細:\n{exc}",
            )
            return False
        voucher_print_service.log_voucher_print_event(
            "save_pdf_on_print_output_path", order_no=order_no, output_path=str(saved_path)
        )
        voucher_print_service.log_voucher_print_event(
            "save_pdf_on_print_finished", order_no=order_no
        )
        return True

    def _log_print_job_context(self, settings, *, order_no: str = "") -> None:
        """印刷ジョブ投入時の付随情報（SumatraPDF実パス・印刷補正）をログへ残す。"""
        from app import voucher_print_service

        sumatra_path_actual = ""
        try:
            sumatra_path_actual, _source = voucher_print_service.resolve_sumatra_executable(settings)
        except Exception:  # noqa: BLE001 - ログ用途のため失敗しても無視
            sumatra_path_actual = ""
        voucher_print_service.log_voucher_print_event(
            "print_job_enqueued",
            order_no=order_no,
            print_backend=str(getattr(settings, "print_backend", "")),
            sumatra_pdf_path_actual=sumatra_path_actual,
            print_adjustment_enabled=bool(getattr(settings, "print_adjustment_enabled", False)),
        )

    @staticmethod
    def _row_error_message(row: VoucherOrderRow) -> str | None:
        """行のバリデーションエラーメッセージを返す。問題なければ None。"""
        if not row.order_no:
            return "受注Noを入力してください。"
        if not any(row.voucher_checks.values()):
            return "印刷する伝票を1つ以上選択してください。"
        # 仕上日は「なし」を選べるため、未設定（None）でもエラーにしない（要件1）。
        return None

    def _validate_row(self, row: VoucherOrderRow) -> bool:
        message = self._row_error_message(row)
        if message:
            QMessageBox.warning(self, "入力エラー", message)
            return False
        return True

    # ── 選択行の一括処理（PDF作成・印刷）─────────────────────────────────────
    def _collect_selected_rows(self) -> list[tuple[int, VoucherOrderRow]] | None:
        """チェックON行を (行番号index, 設定値) で集める。バリデーション失敗時は None。

        1行でも不正があれば、どの行が不正か分かるメッセージを表示して中断する。
        """
        indices = self._selected_indices()
        if not indices:
            return None
        collected = [(i, self._collect_row(self._rows[i])) for i in indices]
        errors: list[str] = []
        for i, row in collected:
            message = self._row_error_message(row)
            if message is None and not self._row_has_olap_data(self._rows[i]):
                message = "先にOLAPデータを取得してください。"
            if message:
                errors.append(f"{i + 1}行目：{message}")
        if errors:
            QMessageBox.warning(self, "入力エラー", "\n".join(errors))
            return None
        return collected

    def _build_selected_pdf_parts(
        self, collected: list[tuple[int, VoucherOrderRow]]
    ) -> list[tuple[VoucherOrderRow, bytes]]:
        """選択行ごとにPDFバイト列を生成して返す（既存の行単位処理を再利用）。"""
        from app import voucher_service

        parts: list[tuple[VoucherOrderRow, bytes]] = []
        prepared: list[tuple[list[str], dict, VoucherOrderRow]] = []
        for _, row in collected:
            ids = [vid for vid, on in row.voucher_checks.items() if on]
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            prepared.append((ids, data, row))
        for ids, data, row in prepared:
            self._cache_row_olap(row, data)
            context = self._edit_render_context(row.order_no)
            parts.append((row, voucher_service.build_vouchers_pdf_bytes(
                ids, data, edit_render_trace_id=str(context["trace_id"]),
                reload_edit_objects=True)))
        return parts

    def _on_select_pdf(self) -> None:
        """選択PDF作成: 選択された受注NoごとにPDFファイルを分けて作成する。

        1受注No = 1PDFファイル（同一受注Noが複数行あれば結合）。ファイル名は
        「<受注No>_伝票.pdf」で、同名があれば連番（_2, _3...）で回避する。
        """
        collected = self._collect_selected_rows()
        if not collected:
            return
        from app import voucher_service, voucher_print_service

        voucher_print_service.log_voucher_print_event(
            "selected_pdf_create_started", selected_row_count=len(collected)
        )
        self._set_processing(True)
        self._set_busy("選択PDF作成中...", context="select_pdf")
        try:
            output_dir = self._resolve_pdf_output_dir()
            parts = self._build_selected_pdf_parts(collected)
            # 受注No単位でまとめる（選択順を保つ）。同一受注Noの伝票種類は結合する。
            grouped: dict[str, list[bytes]] = {}
            order_sequence: list[str] = []
            for row, pdf_bytes in parts:
                if row.order_no not in grouped:
                    grouped[row.order_no] = []
                    order_sequence.append(row.order_no)
                grouped[row.order_no].append(pdf_bytes)
            voucher_print_service.log_voucher_print_event(
                "selected_pdf_create_order_count", order_count=len(order_sequence)
            )
            created_paths: list[Path] = []
            failed_orders: list[str] = []
            for order_no in order_sequence:
                voucher_print_service.log_voucher_print_event(
                    "selected_pdf_create_order_no", order_no=order_no
                )
                bytes_list = grouped[order_no]
                try:
                    merged = (
                        voucher_service.merge_pdf_bytes(bytes_list)
                        if len(bytes_list) > 1
                        else bytes_list[0]
                    )
                    pdf_path = voucher_service.save_named_pdf_bytes(
                        merged, output_dir=output_dir, filename_stem=f"{order_no}_伝票"
                    )
                    created_paths.append(Path(str(pdf_path)))
                    voucher_print_service.log_voucher_print_event(
                        "selected_pdf_create_output_path",
                        order_no=order_no,
                        output_path=str(pdf_path),
                    )
                except Exception as exc:  # noqa: BLE001 - 個別受注Noの失敗は継続する
                    failed_orders.append(order_no)
                    voucher_print_service.log_voucher_print_event(
                        "selected_pdf_create_failed", order_no=order_no
                    )
                    voucher_print_service.log_voucher_print_event(
                        "selected_pdf_create_error_message",
                        order_no=order_no,
                        error_message=str(exc),
                    )
            voucher_print_service.log_voucher_print_event(
                "selected_pdf_create_finished",
                created_count=len(created_paths),
                failed_count=len(failed_orders),
            )
            if created_paths:
                last_path = created_paths[-1]
                self._remember_last_pdf(pdf_path=str(last_path), job_name=last_path.stem)
            self._show_select_pdf_result(created_paths, failed_orders)
            self._auto_open_created_pdfs(created_paths)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="select_pdf")
            self._set_processing(False)

    def _show_select_pdf_result(
        self, created_paths: list[Path], failed_orders: list[str]
    ) -> None:
        """選択PDF作成の結果をステータス表示・ダイアログで通知する。"""
        status = f"PDFを {len(created_paths)} 件作成しました"
        if failed_orders:
            status += "\nPDF作成に失敗した受注Noがあります: " + "、".join(failed_orders)
        self._set_print_status(status, error=bool(failed_orders))
        if created_paths:
            body = f"PDFを {len(created_paths)} 件作成しました:\n" + "\n".join(
                str(path) for path in created_paths
            )
        else:
            body = "作成できたPDFはありません。"
        if failed_orders:
            body += "\n\nPDF作成に失敗した受注No: " + "、".join(failed_orders)
        # 失敗（エラー）通知は設定に関わらず必ず表示する。成功のみのダイアログは
        # 「PDF作成完了ダイアログを表示する」設定に従う（OFF時はステータスのみ）。
        if failed_orders:
            QMessageBox.warning(self, "PDF作成完了", body)
            return
        enabled = self._pdf_created_dialog_enabled()
        self._log_busy_event("pdf_created_dialog_enabled", pdf_created_dialog_enabled=enabled)
        if created_paths and enabled:
            self._log_busy_event("pdf_created_dialog_shown")
            QMessageBox.information(self, "PDF作成完了", body)
        elif created_paths:
            self._log_busy_event("pdf_created_dialog_suppressed")

    def _on_select_preview(self) -> None:
        collected = self._collect_selected_rows()
        if not collected:
            return
        self._set_processing(True)
        self._set_busy("プレビュー生成中...", context="select_preview")
        try:
            from app import voucher_service

            parts = [pdf_bytes for _row, pdf_bytes in self._build_selected_pdf_parts(collected)]
            merged = voucher_service.merge_pdf_bytes(parts)
            self._remember_last_pdf(merged, job_name="selected_preview")
            self._open_preview_window(merged)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "プレビューエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "プレビューエラー", f"プレビュー作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="select_preview")
            self._set_processing(False)

    def _on_select_print(self) -> None:
        collected = self._collect_selected_rows()
        if not collected:
            return
        self._set_busy("印刷ジョブを追加中...", context="select_print")
        try:
            from app import voucher_service, voucher_print_service

            parts = [pdf_bytes for _row, pdf_bytes in self._build_selected_pdf_parts(collected)]
            merged = voucher_service.merge_pdf_bytes(parts)
            order_token = "_".join(row.order_no for _index, row in collected[:3])
            if len(collected) > 3:
                order_token += f"_{len(collected)}件"
            batch_job_name = f"batch_{order_token}"
            self._remember_last_pdf(merged, job_name=batch_job_name)
            backend = load_voucher_printer_settings().print_backend
            if backend == PRINT_BACKEND_SUMATRA:
                self._set_print_status(f"SumatraPDF経由で印刷中: {len(collected)}件")
            elif backend == PRINT_BACKEND_ACROBAT:
                self._set_print_status(f"Acrobat Reader経由で印刷中: {len(collected)}件")
            else:
                self._set_print_status(f"印刷中: {len(collected)}件")
            if backend in (PRINT_BACKEND_ACROBAT, PRINT_BACKEND_SUMATRA):
                self._start_background_print(
                    merged,
                    job_name=batch_job_name,
                    source_type="selected",
                    selected_count=len(collected),
                    generated_pdf_count=len(parts),
                    merged_pdf_created=True,
                )
            else:
                voucher_print_service.print_pdf_direct(merged, self, job_name=batch_job_name)
                self._set_print_status("印刷要求を送信しました")
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            self._set_print_status(f"印刷失敗: {exc}", error=True)
            QMessageBox.critical(self, "印刷エラー", str(exc))
        except Exception as exc:
            self._set_print_status(f"印刷失敗: {exc}", error=True)
            QMessageBox.critical(self, "印刷エラー", f"印刷中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._clear_busy(context="select_print")

    def _selected_order_rows(self) -> list[VoucherOrderRow]:
        rows: list[VoucherOrderRow] = []
        seen: set[str] = set()
        for index in self._selected_indices():
            if not self._row_has_olap_data(self._rows[index]):
                continue
            row = self._collect_row(self._rows[index])
            if not row.order_no or row.order_no in seen:
                continue
            seen.add(row.order_no)
            rows.append(row)
        return rows

    def _on_select_order_no_add(self) -> None:
        """選択行の受注NoだけをKintone登録処理画面へ追加する。"""
        if not self._selected_indices():
            QMessageBox.warning(self, "伝票作成・印刷", "受注Noが選択されていません。")
            return
        rows = self._selected_order_rows()
        if not rows:
            QMessageBox.warning(self, "伝票作成・印刷", "追加できる受注Noがありません。")
            return
        window = self._current_kintone_window()
        if window is None:
            QMessageBox.warning(
                self,
                "Kintone登録処理画面が未起動",
                "Kintone登録処理画面が起動していません。\n"
                "先にKintone登録処理画面を起動してください。",
            )
            self.refresh_kintone_buttons()
            return
        try:
            for row in rows:
                window.add_order_no(row.order_no, finish_date=row.finish_date, am_pm=row.am_pm)
        except Exception as exc:  # noqa: BLE001 - 追加失敗をユーザーへ伝える
            QMessageBox.warning(self, "Kintone登録エラー", f"受注Noの追加に失敗しました:\n{exc}")
            return
        self.refresh_kintone_buttons()
        self._save_records_if_ready()

    @staticmethod
    def _attach_row_settings(data: dict, row: VoucherOrderRow) -> None:
        """行の設定値を伝票データへ付加する（既存PDF生成処理へ渡せるようにする）。

        PDF描画は pages 単位（_normalize_pages_data）で行われるため、トップレベル
        だけでなく各ページ辞書へも仕上日・AM/PM・加工名チェックを反映する。
        画面で設定した値を OLAP取得データより優先させるための専用キーを使う。
        """
        data["order_no"] = row.order_no
        data["finish_date"] = row.finish_date
        data["finish_date_none"] = bool(row.finish_date_none)
        data["am_pm"] = row.am_pm
        data["process_checks"] = dict(row.process_checks)
        data["voucher_checks"] = dict(row.voucher_checks)

        pages = data.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if isinstance(page, dict):
                    page["row_finish_date"] = row.finish_date
                    page["row_finish_date_none"] = bool(row.finish_date_none)
                    page["row_am_pm"] = row.am_pm
                    page["row_process_checks"] = dict(row.process_checks)

    def _show_missing_voucher_no_warning(self, exc: MissingVoucherNoError | None = None) -> None:
        message = MISSING_VOUCHER_NO_MESSAGE
        if exc is not None:
            for order_no in getattr(exc, "order_numbers", set()):
                if order_no:
                    self._voucher_no_blocked_order_nos.add(str(order_no))
            self.refresh_kintone_buttons()
            message = format_missing_voucher_no_message(getattr(exc, "order_numbers", set()))
        QMessageBox.warning(self, "伝票作成・印刷", message)

    # ── OLAP取得・ページ構築（既存処理を再利用）───────────────────────────────
    def _build_print_data(self, numbers: list[str]) -> dict:
        try:
            data = fetch_voucher_print_data(
                numbers, self.olap_login_id, self.olap_password
            )
        except MissingVoucherNoError as exc:
            self._voucher_no_blocked_order_nos.update(exc.order_numbers)
            self.refresh_kintone_buttons()
            raise
        self._voucher_no_blocked_order_nos.difference_update(
            str(n).strip() for n in numbers if str(n).strip()
        )
        self.refresh_kintone_buttons()
        return data

    # ── PDF出力先 ────────────────────────────────────────────────────────────
    def _load_pdf_output_dir(self) -> None:
        try:
            config = load_app_config()
            output_dir = get_voucher_output_dir(config)
        except Exception:
            output_dir = get_voucher_output_dir(None)
        self._pdf_output_dir.setText(str(output_dir))

    def _browse_pdf_output_dir(self) -> None:
        current = self._pdf_output_dir.text().strip()
        selected = QFileDialog.getExistingDirectory(self, "PDF出力先を選択", current)
        if not selected:
            return
        try:
            output_dir = ensure_voucher_output_dir(selected)
            self._save_pdf_output_dir(output_dir)
        except RuntimeError as exc:
            QMessageBox.warning(self, "PDF出力先エラー", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "設定保存エラー", f"PDF出力先の保存に失敗しました:\n{exc}")
            return
        self._pdf_output_dir.setText(str(output_dir))

    def _resolve_pdf_output_dir(self) -> Path:
        raw = self._pdf_output_dir.text().strip()
        if not raw:
            raise RuntimeError("PDF出力先が空です。出力先を指定してください。")
        output_dir = ensure_voucher_output_dir(raw)
        self._save_pdf_output_dir(output_dir)
        return output_dir

    def _save_pdf_output_dir(self, output_dir: Path) -> None:
        config = load_app_config()
        update_values_in_config(
            config.paths.config_env,
            {VOUCHER_OUTPUT_DIR_ENV_KEY: str(output_dir)},
        )

    def _open_preview_window(
        self, pdf_bytes: bytes, *, edit_render_trace_id: str = "",
        edit_objects_sha: str = "", preview_cache_hit: bool = False,
    ) -> "VoucherPrintPreviewWindow":
        """PDFバイト列をアプリ内プレビュー画面で表示する。

        一時PDFファイルも正式PDFも保存せず、メモリ上のバイト列をそのまま渡す。
        """
        from app.voucher_preview_controller import open_voucher_preview

        preview = open_voucher_preview(
            self, pdf_bytes,
            edit_render_trace_id=edit_render_trace_id,
            edit_objects_sha256=edit_objects_sha,
            preview_cache_hit=preview_cache_hit,
        )
        # 参照を保持してGCを防ぐ。
        self._preview_window = preview
        return preview

    def _create_pdf(
        self, ids: list[str], data: dict, *, output_dir: Path,
        open_after: bool, edit_render_trace_id: str = "",
    ) -> "Path | None":
        try:
            from app import voucher_service
            pdf_path = voucher_service.create_vouchers_pdf(
                ids, data, output_dir=output_dir,
                edit_render_trace_id=edit_render_trace_id,
                reload_edit_objects=True)
        except FileNotFoundError as exc:
            QMessageBox.critical(self, "テンプレートエラー", str(exc))
            return None
        except RuntimeError as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
            return None
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
            return None

        self._notify_pdf_created(f"PDFを作成しました:\n{pdf_path}", status=f"PDFを作成しました: {pdf_path}")
        if open_after:
            self._auto_open_created_pdfs([Path(str(pdf_path))])
        return pdf_path

    def _pdf_auto_open_enabled(self) -> bool:
        try:
            enabled = bool(load_voucher_printer_settings().open_pdf_after_create)
        except Exception:  # noqa: BLE001 - 設定読込失敗時は従来どおり開く
            enabled = True
        try:
            voucher_print_service.log_voucher_print_event(
                "pdf_auto_open_enabled", enabled=enabled
            )
        except Exception:
            pass
        return enabled

    def _open_local_path(self, path: Path) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))

    def _auto_open_created_pdfs(self, paths: list[Path]) -> None:
        created = [Path(str(path)) for path in paths if path]
        if not created:
            return
        if not self._pdf_auto_open_enabled():
            try:
                voucher_print_service.log_voucher_print_event(
                    "pdf_auto_open_suppressed", created_count=len(created)
                )
            except Exception:
                pass
            return
        target = created[0] if len(created) == 1 else created[0].parent
        target_type = "pdf" if len(created) == 1 else "folder"
        try:
            voucher_print_service.log_voucher_print_event(
                "pdf_auto_open_target",
                target=str(target),
                target_type=target_type,
                created_count=len(created),
            )
            voucher_print_service.log_voucher_print_event(
                "pdf_auto_open_started", target=str(target), target_type=target_type
            )
            ok = self._open_local_path(target)
            if not ok:
                raise RuntimeError("QDesktopServices.openUrl returned False")
        except Exception as exc:  # noqa: BLE001 - 自動オープン失敗はPDF作成成功を壊さない
            try:
                voucher_print_service.log_voucher_print_event(
                    "pdf_auto_open_failed",
                    target=str(target),
                    target_type=target_type,
                    error_message=str(exc),
                )
            except Exception:
                pass
            logging.getLogger("tks_to_kintone_app").warning(
                "PDF自動オープンに失敗しました: %s (%s)", target, exc
            )
            self._set_print_status(f"PDFを作成しました（自動オープンに失敗しました）: {target}", error=True)

    def _pdf_created_dialog_enabled(self) -> bool:
        """PDF作成完了ダイアログを表示する設定か（既定ON）。"""
        try:
            return bool(load_voucher_printer_settings().show_pdf_created_dialog)
        except Exception:  # noqa: BLE001 - 設定読込失敗時は従来どおり表示する
            return True

    def _notify_pdf_created(self, dialog_body: str, *, status: str) -> None:
        """PDF作成成功を通知する。設定ONならダイアログ、OFFならステータスのみ。

        OFFでもステータス表示とログには必ずPDF作成結果を残す（要件1）。
        """
        enabled = self._pdf_created_dialog_enabled()
        self._log_busy_event("pdf_created_dialog_enabled", pdf_created_dialog_enabled=enabled)
        self._set_print_status(status)
        if enabled:
            self._log_busy_event("pdf_created_dialog_shown")
            QMessageBox.information(self, "PDF作成完了", dialog_body)
        else:
            self._log_busy_event("pdf_created_dialog_suppressed")

    # ── 一覧レコード保存・復元 ───────────────────────────────────────────────
    def _records_path(self) -> Path:
        config = load_app_config()
        return config.paths.work_dir / "voucher_records.json"

    def _save_records_if_ready(self) -> None:
        if not getattr(self, "_restoring_records", False):
            self._save_records()

    def _save_records(self) -> None:
        if getattr(self, "_restoring_records", False):
            return
        try:
            path = self._records_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now().isoformat(timespec="seconds")
            records = []
            for rw in self._rows:
                if self._is_new_input_row(rw):
                    continue
                # 受注Noが空の通常行は保存しない（旧仕様の空行を新規行もどきとして
                # 復元させないため）。新規入力は _new_input_row 専用で扱う。
                if self._is_empty_order_no(rw.order_input.text()):
                    continue
                row = self._collect_row(rw)
                status = self._registration_status_for_order(row.order_no)
                updated_at = getattr(rw, "updated_at", None)
                if not isinstance(updated_at, datetime):
                    updated_at = _datetime_from_iso(str(updated_at or "")) or datetime.min
                    rw.updated_at = updated_at
                records.append(
                    {
                        "saved_at": now,
                        "updated_at": updated_at.isoformat(timespec="seconds"),
                        "order_no": row.order_no,
                        "finish_date": row.finish_date.isoformat() if row.finish_date else "",
                        "finish_date_none": bool(row.finish_date_none),
                        "am_pm": row.am_pm,
                        "process_checks": dict(row.process_checks),
                        "voucher_checks": dict(row.voucher_checks),
                        "kintone_status": status,
                        "has_olap_data": self._row_has_olap_data(rw),
                        "cached_olap": _jsonable_record_value(rw.cached_olap or {}),
                        "voucher_no": _voucher_no_from_cached_olap(rw.cached_olap),
                    }
                )
            payload = {"version": 1, "saved_at": now, "records": records}
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("伝票一覧レコード保存に失敗しました。")

    def _load_normalized_saved_records(self) -> list[dict]:
        """保存済み一覧を読み込み、保持期間フィルタ・正規化・重複排除して返す。

        受注No空・期限切れ・重複（更新日時が新しい1件のみ採用）を除いた最終復元対象。
        読み込み失敗や0件のときは空リストを返す（例外はログ化して握る）。

        ワーカースレッドと同じ純粋関数 _read_and_normalize_saved_records を用いる（要件2・4）。
        """
        try:
            return _read_and_normalize_saved_records(
                self._records_path(), load_record_retention_days()
            )
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("伝票一覧レコード読み込みに失敗しました。")
            return []

    def _enter_bulk_restore(self, total: int, *, hold_updates: bool = True) -> None:
        """bulk復元の開始: フラグ・テーブル状態を設定して行単位の重処理を抑制する（要件1・2）。

        hold_updates=True（同期・即時パス）は復元中ずっとテーブル再描画を止めて一括化する。
        hold_updates=False（バッチ復元）は各バッチ内でのみ再描画を止め、10件ごとに可視化する
        ため、ここでは setUpdatesEnabled(False) を保持しない（要件2/3）。
        """
        logger = logging.getLogger("tks_to_kintone_app")
        self._bulk_restore_start_perf = time.perf_counter()
        self._restoring_records = True
        self._bulk_restoring_saved_rows = True
        logger.info("voucher_window_restore_rows_started %s", {"count": total})
        logger.info("voucher_window_restore_rows_bulk_started %s", {"count": total})
        logger.info("voucher_window_restore_rows_bulk_count %s", {"count": total})
        logger.info("voucher_window_restore_rows_column_visibility_suppressed %s", {"suppressed": True})
        logger.info("voucher_window_restore_rows_filter_suppressed %s", {"suppressed": True})
        if hold_updates:
            self._table.setUpdatesEnabled(False)
        self._bulk_prev_block = self._table.blockSignals(True)
        self._bulk_prev_sort = self._table.isSortingEnabled()
        self._table.setSortingEnabled(False)

    def _add_saved_records_batch(self, records: list[dict]) -> None:
        """指定レコード群を行として追加する（bulk中・行単位の重処理は抑制済み）。"""
        for record in records:
            try:
                order_no = normalize_order_no(record.get("order_no"))
                if order_no and self._duplicate_order_no_row(order_no) is not None:
                    self._log_voucher_event(
                        "voucher_window_saved_row_duplicate_skipped", order_no=order_no)
                    continue
                rw = self._add_row()
                self._apply_saved_record_to_row(rw, record)
            except Exception:
                logging.getLogger("tks_to_kintone_app").warning(
                    "伝票一覧レコードの一部復元をスキップしました。受注No=%s",
                    str(record.get("order_no") or "").strip(),
                    exc_info=True,
                )

    def _exit_bulk_restore(self) -> None:
        """bulk復元の終了: フラグ・テーブル状態を元へ戻す。"""
        self._bulk_restoring_saved_rows = False
        try:
            self._table.setSortingEnabled(getattr(self, "_bulk_prev_sort", False))
            self._table.blockSignals(getattr(self, "_bulk_prev_block", False))
            self._table.setUpdatesEnabled(True)
        finally:
            self._restoring_records = False

    def _apply_after_bulk_restore(self) -> None:
        """bulk復元後に、行単位で抑制した重い処理を1回だけまとめて反映する（要件1・2）。"""
        logger = logging.getLogger("tks_to_kintone_app")
        self._apply_table_column_widths()  # 列幅再配分 → 列表示反映（1回）
        self._apply_column_visibility(source="after_bulk_restore", force=True)
        self._log_voucher_event("voucher_window_column_visibility_applied_once_after_bulk")
        from app.theme_utils import apply_semantic_button_styles

        apply_semantic_button_styles(self)
        self.refresh_kintone_buttons()
        self._refresh_registration_status_buttons()
        self._apply_filters()
        self._log_voucher_event("voucher_window_filters_applied_once_after_bulk")
        _bulk_ms = int(
            (time.perf_counter() - getattr(self, "_bulk_restore_start_perf", time.perf_counter())) * 1000
        )
        logger.info("voucher_window_restore_rows_finished %s", {"rows": len(self._rows)})
        logger.info("voucher_window_restore_rows_bulk_finished %s", {"rows": len(self._rows)})
        logger.info("voucher_window_restore_rows_bulk_elapsed_ms %s", {"elapsed_ms": _bulk_ms})
        if _bulk_ms >= 1000:
            logger.warning(
                "voucher_window_slow_step_detected %s",
                {"step": "voucher_window_restore_rows_bulk", "elapsed_ms": _bulk_ms},
            )
        self._save_records()

    def _restore_saved_records(self) -> bool:
        """保存済み一覧を同期的に全件復元する（競合ガード・テスト用の即時パス）。

        画面表示後の通常経路はチャンク復元（_restore_saved_records_after_show）だが、
        本メソッドは一括で復元し、bulk抑制と復元後の1回反映は共通ヘルパで行う。
        """
        records = self._load_normalized_saved_records()
        if not records:
            return False
        self._enter_bulk_restore(len(records))
        try:
            self._add_saved_records_batch(records)
        finally:
            self._exit_bulk_restore()
        self._apply_after_bulk_restore()
        return bool(self._rows)

    def _filter_records_by_retention(self, records: list[object], path: Path) -> list[dict]:
        days = load_record_retention_days()
        cutoff = datetime.now() - timedelta(days=days)
        kept: list[dict] = []
        fallback_dt = datetime.fromtimestamp(path.stat().st_mtime)
        for record in records:
            if not isinstance(record, dict):
                continue
            dt = _record_datetime(record) or fallback_dt
            if dt >= cutoff:
                kept.append(record)
        return kept

    def _cleanup_expired_saved_records(self) -> None:
        try:
            path = self._records_path()
            if not path.is_file():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records") if isinstance(payload, dict) else []
            kept = self._filter_records_by_retention(records, path) if isinstance(records, list) else []
            if len(kept) == len(records):
                return
            payload["records"] = kept
            payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("期限切れ伝票一覧レコード削除に失敗しました。")

    def _apply_saved_record_to_row(self, rw: _RowWidgets, record: dict) -> None:
        order_no = str(record.get("order_no") or "").strip()
        rw.order_input.setText(order_no)
        finish_date = _date_from_iso(str(record.get("finish_date") or ""))
        if "finish_date_none" in record:
            finish_date_none = normalize_finish_date_none(record.get("finish_date_none"))
        else:
            finish_date_none = finish_date is None
        if finish_date_none:
            rw.finish_none_check.setChecked(True)
        else:
            rw.finish_none_check.setChecked(False)
            if finish_date is not None:
                rw.date_edit.setDate(QDate(finish_date.year, finish_date.month, finish_date.day))
        am_pm = str(record.get("am_pm") or "AM")
        rw.ampm_none.setChecked(am_pm == "none")
        rw.ampm_pm.setChecked(am_pm == "PM")
        rw.ampm_am.setChecked(am_pm not in {"none", "PM"})
        for name, checked in (record.get("process_checks") or {}).items():
            if name in rw.process_checks:
                rw.process_checks[name].setChecked(bool(checked))
        for vid, checked in (record.get("voucher_checks") or {}).items():
            if vid in rw.voucher_checks:
                rw.voucher_checks[vid].setChecked(bool(checked))
        cached = record.get("cached_olap")
        has_saved_olap_flag = "has_olap_data" in record
        if isinstance(cached, dict) and bool(cached.get("pages") or cached.get("raw_rows")):
            rw.cached_olap = cached
        elif bool(record.get("has_olap_data")):
            rw.cached_olap = {"pages": [{"order_no": order_no}], "raw_rows": []}
        elif not has_saved_olap_flag and order_no:
            rw.cached_olap = {"pages": [{"order_no": order_no}], "raw_rows": []}
        else:
            rw.cached_olap = None
        status = str(record.get("kintone_status") or KINTONE_STATUS_UNREGISTERED)
        if order_no and status == KINTONE_STATUS_COMPLETED:
            self._registration_status_by_order[order_no] = KINTONE_STATUS_COMPLETED
        rw.updated_at = _datetime_from_iso(str(record.get("updated_at") or "")) or datetime.now()
        self._refresh_row_olap_state(rw)

    def show(self) -> None:  # noqa: N802
        """伝票作成・印刷画面は標準で最大化表示する（要件1-3）。"""
        self.showMaximized()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        _perf_voucher_list("show", self._perf_list_started)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())
        # 最大化後の表示幅に合わせて列幅を再調整する（要件1-3）。
        self._apply_table_column_widths()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """リサイズ時に列幅を再調整し、加工名・印刷する伝票列を広く保つ（要件1-3）。"""
        super().resizeEvent(event)
        # __init__ 内の resize() で _table 生成前に呼ばれても安全にする。
        if getattr(self, "_table", None) is not None:
            self._apply_table_column_widths()


def _missing_required_voucher_fields(pages: list[dict]) -> list[str]:
    missing: list[str] = []
    if not pages:
        return ["pages"]
    for index, page in enumerate(pages, start=1):
        prefix = f"page{index}"
        if not str(page.get("order_no") or "").strip():
            missing.append(f"{prefix}.order_no")
        if not str(page.get("customer_name") or "").strip():
            missing.append(f"{prefix}.customer_name")
        if not str(page.get("delivery_no") or page.get("voucher_no") or "").strip():
            missing.append(f"{prefix}.delivery_no")
        if not page.get("details"):
            missing.append(f"{prefix}.detail_rows")
    return missing


def format_missing_voucher_no_message(order_numbers: list[str] | set[str] | tuple[str, ...]) -> str:
    ordered = sorted({str(value).strip() for value in order_numbers if str(value).strip()})
    if not ordered:
        return MISSING_VOUCHER_NO_BASE_MESSAGE
    return MISSING_VOUCHER_NO_BASE_MESSAGE + "\n受注No：" + "、".join(ordered)


def _date_from_iso(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _datetime_from_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _record_datetime(record: dict) -> datetime | None:
    for key in ("updated_at", "saved_at", "fetched_at"):
        dt = _datetime_from_iso(str(record.get(key) or ""))
        if dt is not None:
            return dt
    cached = record.get("cached_olap")
    if isinstance(cached, dict):
        dt = _datetime_from_iso(str(cached.get("fetched_at") or ""))
        if dt is not None:
            return dt
    finish = _date_from_iso(str(record.get("finish_date") or ""))
    if finish is not None:
        return datetime.combine(finish, datetime.min.time())
    return None


def _voucher_no_from_cached_olap(cached_olap: dict | None) -> str:
    if not isinstance(cached_olap, dict):
        return ""
    pages = cached_olap.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict):
                value = page.get("delivery_no") or page.get("voucher_no")
                if str(value or "").strip():
                    return str(value).strip()
    rows = cached_olap.get("raw_rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                value = _voucher_no_from_row(row)
                if str(value or "").strip():
                    return str(value).strip()
    return ""


def _jsonable_record_value(value):
    if isinstance(value, dict):
        return {str(k): _jsonable_record_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_record_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _voucher_no_from_row(row: dict) -> object:
    for key in ("voucher_no", "納品書No", "伝票No", "伝票番号", "9"):
        if key in row:
            return row.get(key)
    return None


def _order_no_from_row(row: dict, fallback: str = "") -> str:
    for key in ("order_no", "受注No", "6"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return str(fallback or "").strip()


def _missing_voucher_no_order_numbers(rows: list[dict], requested_numbers: list[str]) -> set[str]:
    missing: set[str] = set()
    fallback = ",".join(str(value).strip() for value in requested_numbers if str(value).strip())
    for row in rows:
        if not isinstance(row, dict):
            continue
        if is_missing_voucher_no(_voucher_no_from_row(row)):
            missing.add(_order_no_from_row(row, fallback) or fallback)
    return {value for value in missing if value}


def _has_minimum_detail_mapping(row: dict[str, str]) -> bool:
    checks = (
        ("order_no", "6"),
        ("customer_name", "5"),
        ("voucher_no", "9"),
        ("product_name", "16"),
    )
    return any(str(row.get(alias) or row.get(fallback) or "").strip() for alias, fallback in checks)


def _settings_bool(settings: QSettings, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _log_voucher_page_diagnostics(logger: logging.Logger, page: dict) -> None:
    details = page.get("details") or []
    logger.info(
        "売上伝票1ページ目主要データ: code_no=%s customer_name=%s order_no=%s delivery_no=%s "
        "slip_type_name=%s shipping_type_name=%s operator_name=%s detail_rows=%s",
        page.get("code_no", ""),
        page.get("customer_name", ""),
        page.get("order_no", ""),
        page.get("delivery_no") or page.get("voucher_no", ""),
        page.get("slip_type_name") or page.get("trade_type", ""),
        page.get("shipping_type_name") or page.get("ship_type", ""),
        page.get("operator_name") or page.get("operator", ""),
        len(details),
    )
    logger.info(
        "voucher_delivery_course_page_aggregated "
        "order_no=%s voucher_no=%s response_key=%s display_no=%s "
        "value=%r raw_value=%r stage=_build_print_data",
        page.get("order_no", ""),
        page.get("voucher_no") or page.get("delivery_no", ""),
        page.get("delivery_course_response_key") or "(not_available)",
        page.get("delivery_course_display_no", ""),
        page.get("delivery_course_name", ""),
        page.get("delivery_course_name_raw", ""),
    )
    if not details:
        return
    first = details[0]
    logger.info(
        "売上伝票明細1行目主要データ: item_name=%s item_note=%s quantity=%s unit_price_display=%s "
        "amount_display=%s note_fields=%s",
        first.get("item_name") or first.get("name", ""),
        first.get("item_note") or first.get("dims", ""),
        first.get("quantity") or first.get("qty", ""),
        first.get("unit_price_display") or first.get("unit_price", ""),
        first.get("amount_display") or first.get("amount", ""),
        first.get("note_lines", []),
    )
