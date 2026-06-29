"""伝票作成・印刷画面。

受注Noごとに1行で設定する一覧形式の画面。
1行 = 1つの受注No を扱い、行ごとに仕上日・AM/PM・加工名チェック・
印刷する伝票チェックを設定し、行単位で PDF作成・印刷を実行できる。
"""
from __future__ import annotations

import logging
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QDate, QSettings, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import load_app_config, resource_path, update_values_in_config
from app.logger import setup_logger
from app.path_utils import (
    VOUCHER_OUTPUT_DIR_ENV_KEY,
    ensure_voucher_output_dir,
    get_voucher_output_dir,
)
from app.voucher_data_mapper import (
    build_voucher_pages,
    display_mapping_summary,
    is_missing_voucher_no,
)
from app.voucher_olap_service import VoucherOlapService
from app.voucher_settings import (
    DEFAULT_CACHE_RETENTION_DAYS,
    DEFAULT_RECORD_RETENTION_DAYS,
    load_cache_retention_days,
    load_record_retention_days,
    load_default_print_types,
    save_cache_retention_days,
    save_record_retention_days,
    save_default_print_types,
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

def normalize_order_no(value: object) -> str:
    """受注Noを重複比較用に正規化する（前後空白除去・全角→半角）。

    空欄は空文字を返す。全角数字が入っても半角と同一視できるよう NFKC で正規化する。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text)


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

MISSING_VOUCHER_NO_BASE_MESSAGE = "伝票Noがありません。\nTSKで先に処理してください。"
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


class _RowWidgets:
    """テーブル1行分のウィジェット参照を束ねる。"""

    def __init__(self) -> None:
        self.updated_at: datetime = datetime.now()
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
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("印刷する伝票設定")
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

    def set_record_retention_days(self, days: int) -> None:
        self._record_retention_spin.setValue(days or DEFAULT_RECORD_RETENTION_DAYS)

    def record_retention_days(self) -> int:
        return int(self._record_retention_spin.value())


class VoucherWindow(QMainWindow):
    """伝票作成・印刷画面（受注一覧形式）。"""

    back_requested = Signal()

    def __init__(
        self,
        olap_login_id: str = "",
        olap_password: str = "",
        kintone_window_provider=None,
    ) -> None:
        super().__init__()
        self.olap_login_id = olap_login_id
        self.olap_password = olap_password
        # Kintone登録処理画面（MainWindow）の現在インスタンスを返すコールバック。
        # ランチャーが保持する _main_window を都度参照するため、開閉のたびに
        # 最新の状態（None=未起動）を取得できる。未指定なら常に未起動扱い。
        self._kintone_window_provider = kintone_window_provider

        self.setWindowTitle(f"伝票作成・印刷 — TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        self.resize(1680, 760)
        self.setMinimumSize(1420, 680)

        self._rows: list[_RowWidgets] = []
        self._voucher_no_blocked_order_nos: set[str] = set()
        self._registration_status_by_order: dict[str, str] = {}
        self._restoring_records = False

        # 新規行の「印刷する伝票」初期チェック（設定から読み込み・以後の追加行に反映）
        self._default_print_types: set[str] = set(load_default_print_types())
        # 起動時に期限切れのOLAPキャッシュを削除する。
        self._cleanup_expired_cache()

        # 上部の行操作ボタン
        self._add_row_button = QPushButton("行追加")
        self._voucher_settings_button = QPushButton("⚙")
        self._voucher_settings_button.setToolTip("印刷する伝票設定")
        self._voucher_settings_button.setAccessibleName("印刷する伝票設定")
        self._voucher_settings_button.setMinimumSize(40, 40)
        self._voucher_settings_button.setStyleSheet("QPushButton { font-size: 20px; padding: 4px; }")
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

        # 全選択チェックボックス（選択列ヘッダー相当）。中間状態に対応。
        self._select_all_check = QCheckBox("全選択")
        self._select_all_check.setTristate(True)
        self._select_all_check.setToolTip("チェックで全行を選択、解除で全行の選択を外します。")

        # 受注一覧テーブル
        self._table = QTableWidget(0, len(COLUMN_LABELS))
        self._table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setHorizontalScrollBarPolicy(
            self._table.horizontalScrollBarPolicy().ScrollBarAsNeeded
        )
        self._table.setVerticalScrollBarPolicy(
            self._table.verticalScrollBarPolicy().ScrollBarAsNeeded
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 列区切り線をライト/ダーク両モードで見えるようにする（要件1-2）。
        self._table.setShowGrid(True)
        self._table.setStyleSheet(VOUCHER_TABLE_STYLE)

        # PDF出力先（共通設定）
        self._pdf_output_dir = QLineEdit()
        self._pdf_output_dir.setPlaceholderText("PDF出力先フォルダ")
        self._pdf_output_dir.setToolTip("PDF作成ボタンで保存するフォルダを指定してください。")
        self._browse_output_button = QPushButton("参照")
        self._load_pdf_output_dir()

        self._build_layout()

        self._add_row_button.clicked.connect(self._on_add_row)
        self._voucher_settings_button.clicked.connect(self._on_voucher_settings)
        self._select_pdf_button.clicked.connect(self._on_select_pdf)
        self._select_preview_button.clicked.connect(self._on_select_preview)
        self._select_print_button.clicked.connect(self._on_select_print)
        self._remove_row_button.clicked.connect(self._on_remove_selected)
        self._select_order_no_button.clicked.connect(self._on_select_order_no_add)
        self._select_all_check.clicked.connect(self._on_select_all_clicked)
        self._browse_output_button.clicked.connect(self._browse_pdf_output_dir)
        self._order_search_edit.textChanged.connect(self._apply_filters)
        self._status_filter.currentTextChanged.connect(self._apply_filters)

        # 保存済みレコードがなければ空行を1行
        if not self._restore_saved_records():
            self._add_row()
        self._update_selection_state()
        self._update_add_row_button_enabled()

    # ── レイアウト ────────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("受注一覧"))
        top_row.addWidget(self._select_all_check)
        top_row.addSpacing(12)
        top_row.addWidget(QLabel("受注No検索:"))
        top_row.addWidget(self._order_search_edit)
        top_row.addWidget(QLabel("登録状態:"))
        top_row.addWidget(self._status_filter)
        top_row.addStretch(1)
        top_row.addWidget(self._add_row_button)
        top_row.addWidget(self._select_pdf_button)
        top_row.addWidget(self._select_preview_button)
        top_row.addWidget(self._select_print_button)
        top_row.addWidget(self._remove_row_button)
        top_row.addWidget(self._select_order_no_button)
        top_row.addWidget(self._voucher_settings_button)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("PDF出力先:"))
        output_row.addWidget(self._pdf_output_dir, 1)
        output_row.addWidget(self._browse_output_button)

        root = QVBoxLayout()
        root.addLayout(top_row)
        root.addWidget(self._table, 1)
        root.addLayout(output_row)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    # ── 行の生成 ─────────────────────────────────────────────────────────────
    def _add_row(self) -> _RowWidgets:
        rw = _RowWidgets()
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
        self._table.setCellWidget(row_index, COL_ORDER_NO, self._wrap(rw.order_input))

        # OLAP（未取得行は取得、取得済み行は同じ受注Noで更新）
        rw.refetch_button = QPushButton("取得")
        rw.refetch_button.setProperty("buttonRole", "olapFetch")
        rw.refetch_button.setToolTip("受注NoでOLAPデータを取得します。取得済み行は同じ受注Noで更新します。")
        rw.refetch_button.clicked.connect(lambda _=False, r=rw: self._on_refetch_row(r))
        self._table.setCellWidget(row_index, COL_REFETCH, self._wrap(rw.refetch_button))

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
        # 初期値は従来のコンボボックスと同じく AM。
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
        for i, name in enumerate(PROCESS_NAMES):
            cb = QCheckBox(name)
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

        self._rows.append(rw)
        self._apply_voucher_table_row_font(row_index)
        self._table.resizeColumnsToContents()
        self._table.resizeRowsToContents()
        self._apply_table_column_widths()
        # 追加行のKintone登録ボタン状態を初期化（起動状態・追加済判定: 要件2・3）。
        self.refresh_kintone_buttons()
        self._refresh_registration_status_buttons()
        self._refresh_row_olap_state(rw)
        # 画面表示後に追加された行にも共通の用途別ボタン色を適用する。
        from app.theme_utils import apply_semantic_button_styles

        apply_semantic_button_styles(self)
        self._apply_filters()
        self._save_records_if_ready()
        return rw

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
            self._rows,
            key=lambda rw: (1 if not self._row_has_olap_data(rw) else 0, self._row_updated_at_for_sort(rw)),
            reverse=True,
        )
        self._rows = sorted_rows
        header = self._table.verticalHeader()
        for visual_index, rw in enumerate(self._rows):
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
            if normalize_order_no(rw.order_input.text()) == target:
                return rw
        return None

    def _has_empty_order_no_row(self) -> bool:
        return any(self._is_empty_order_no(rw.order_input.text()) for rw in self._rows)

    def _update_add_row_button_enabled(self) -> None:
        self._add_row_button.setEnabled(not self._has_empty_order_no_row())

    def _mark_row_updated(self, rw: _RowWidgets) -> None:
        if getattr(self, "_restoring_records", False):
            return
        self._apply_filters()
        self._save_records_if_ready()

    def _on_row_data_changed(self, rw: _RowWidgets) -> None:
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

    def _set_widget_tree_enabled(self, widget: QWidget, enabled: bool) -> None:
        widget.setEnabled(enabled)

    def _refresh_row_olap_state(self, rw: _RowWidgets) -> None:
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
        self.refresh_kintone_buttons()
        self._refresh_registration_status_buttons()
        self._apply_filters()
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
            button.setText(status)
            button.setEnabled(False)
            button.setStyleSheet(KINTONE_STATUS_STYLES.get(status, ""))
        self._apply_voucher_table_fonts()

    def _apply_filters(self, *_args) -> None:
        logger = logging.getLogger("tks_to_kintone_app")
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
            is_new_unfetched = not self._row_has_olap_data(rw)
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
        self._update_add_row_button_enabled()
        self._update_selection_state()
        self._apply_voucher_table_fonts()

    def _on_voucher_settings(self) -> None:
        """印刷する伝票設定ダイアログを開き、保存・既存行反映を行う。"""
        dialog = VoucherPrintSettingsDialog(
            selected_ids=set(self._default_print_types),
            retention_days=load_cache_retention_days(),
            parent=self,
        )
        dialog.set_record_retention_days(load_record_retention_days())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_ids = dialog.selected_ids()
        try:
            save_default_print_types(sorted(new_ids))
            save_cache_retention_days(dialog.retention_days())
            save_record_retention_days(dialog.record_retention_days())
        except Exception as exc:
            QMessageBox.warning(self, "設定保存エラー", f"設定の保存に失敗しました:\n{exc}")
            return
        # 以後追加する行・初期空行に反映する初期チェックを更新。
        self._default_print_types = set(new_ids)
        if self._rows:
            reply = QMessageBox.question(
                self,
                "印刷する伝票設定",
                "現在の一覧にもこの設定を反映しますか？",
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._apply_print_types_to_rows(new_ids)

    def _apply_print_types_to_rows(self, ids: set[str]) -> None:
        """現在表示中の全行の印刷する伝票チェックを設定値で上書きする。"""
        id_set = set(ids)
        for rw in self._rows:
            for vid, cb in rw.voucher_checks.items():
                cb.setChecked(vid in id_set)
        if not getattr(self, "_restoring_records", False):
            self._apply_filters()
            self._save_records_if_ready()

    def _on_refetch_row(self, rw: _RowWidgets) -> None:
        """対象行の受注NoでOLAPを再取得し、行のOLAPデータを更新する（要件2・6）。

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
        button = rw.refetch_button
        button.setEnabled(False)
        button.setText("取得中...")
        success = False
        try:
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            # 再取得したOLAPデータを行に保持し、受注No単位のキャッシュを更新する。
            # 編集オブジェクトには触れないため指図書編集内容は維持される。
            rw.cached_olap = data
            self._cache_row_olap(row, data)
            success = True
            # 新規行の初回取得成功時だけ、次の入力用空行を先頭へ追加する。
            # 既に空の未取得行がある場合は重複追加しない。
            if was_new_unfetched and not any(
                other is not rw
                and not self._row_has_olap_data(other)
                and self._is_empty_order_no(other.order_input.text())
                for other in self._rows
            ):
                self._add_row()
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
            QMessageBox.critical(self, "OLAP取得エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(
                self, "OLAP取得エラー", f"OLAP取得中に予期しないエラーが発生しました:\n{exc}"
            )
        finally:
            button.setEnabled(True)
            self._refresh_row_olap_state(rw)
            if row.order_no and not getattr(self, "_restoring_records", False):
                if success and was_new_unfetched:
                    rw.updated_at = datetime.now()
                self._apply_filters()
                self._save_records_if_ready()

    def _on_edit_order_sheet(self, rw: _RowWidgets) -> None:
        """対象行の受注Noで指図書(1)プレビューを生成し、全画面の編集画面を開く。"""
        row = self._collect_row(rw)
        if not row.order_no:
            QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
            return
        self._set_processing(True)
        try:
            from app import voucher_service

            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            # 背景は「編集オブジェクトなし」の指図書(1)から生成する（要件1・11・13）。
            # ここで編集オブジェクトを反映すると保存済みテキストが背景へ焼き込まれ、
            # さらに編集レイヤーにも復元されて二重表示・背景化が起きるため明示的に空にする。
            for page in data.get("pages") or []:
                if isinstance(page, dict):
                    page["edit_objects"] = []
            pdf_bytes = voucher_service.build_vouchers_pdf_bytes(["03"], data)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
            return
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "指図書編集エラー", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "指図書編集エラー", f"指図書プレビュー生成中に予期しないエラーが発生しました:\n{exc}")
            return
        finally:
            self._set_processing(False)

        from app.voucher_edit_window import VoucherEditWindow

        editor = VoucherEditWindow(order_no=row.order_no, background_pdf_bytes=pdf_bytes, parent=self)
        editor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # 参照を保持してGCを防ぐ。
        self._editor_window = editor
        # タイトルバー付きの最大化表示を標準にする（全画面はボタンで切替: 要件2-1・2-2）。
        editor.showMaximized()

    # ── OLAPキャッシュ ────────────────────────────────────────────────────────
    def _cleanup_expired_cache(self) -> None:
        try:
            from app import voucher_cache

            voucher_cache.cleanup_expired_cache(load_cache_retention_days())
            self._cleanup_expired_saved_records()
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("期限切れOLAPキャッシュ削除に失敗しました。")

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

    # ── 選択状態の管理 ────────────────────────────────────────────────────────
    def _selected_indices(self) -> list[int]:
        return [i for i, rw in enumerate(self._rows) if rw.select_check.isChecked()]

    def _on_row_selection_changed(self, _state: int = 0) -> None:
        self._update_selection_state()

    def _on_select_all_clicked(self, _checked: bool = False) -> None:
        # tristate チェックの状態遷移に依存せず、現在の選択状況から全選択/全解除を決める。
        # 全行が選択済みなら全解除、それ以外（一部・未選択）なら全選択。
        all_selected = bool(self._rows) and len(self._selected_indices()) == len(self._rows)
        self._set_all_rows_checked(not all_selected)

    def _set_all_rows_checked(self, checked: bool) -> None:
        for rw in self._rows:
            rw.select_check.blockSignals(True)
            rw.select_check.setChecked(checked)
            rw.select_check.blockSignals(False)
        self._update_selection_state()

    def _update_selection_state(self) -> None:
        """行のチェック状態に合わせて全選択チェックとボタン有効/無効を更新する。"""
        self._update_select_all_check()
        self._update_selection_buttons()

    def _update_select_all_check(self) -> None:
        total = len(self._rows)
        selected = len(self._selected_indices())
        self._select_all_check.blockSignals(True)
        if total == 0 or selected == 0:
            self._select_all_check.setCheckState(Qt.CheckState.Unchecked)
        elif selected == total:
            self._select_all_check.setCheckState(Qt.CheckState.Checked)
        else:
            self._select_all_check.setCheckState(Qt.CheckState.PartiallyChecked)
        self._select_all_check.blockSignals(False)

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
        rows_to_delete = [rw for rw in self._rows if rw.select_check.isChecked()]
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
        if not self._rows:
            self._add_row()
        # 表示・選択・全選択・件数（フィルタログ）・保存を更新する。
        self._apply_filters()
        self._update_selection_state()
        self._update_add_row_button_enabled()
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
        )

    def closeEvent(self, event) -> None:
        """ウィンドウが閉じられるときに back_requested を emit して起動元に通知する。"""
        self._save_records()
        self.back_requested.emit()
        super().closeEvent(event)

    def _set_processing(self, processing: bool) -> None:
        for widget in (
            self._add_row_button,
            self._order_search_edit,
            self._status_filter,
            self._select_all_check,
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
            self._update_add_row_button_enabled()
            self._update_selection_buttons()

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
        try:
            output_dir = self._resolve_pdf_output_dir()
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            self._create_pdf(ids, data, output_dir=output_dir, open_after=True)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except RuntimeError as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    # ── プレビュー（行単位）─────────────────────────────────────────────────
    def _on_preview(self, rw: _RowWidgets) -> None:
        if not self._row_has_olap_data(rw):
            QMessageBox.warning(self, "入力エラー", "先にOLAPデータを取得してください。")
            return
        row = self._collect_row(rw)
        if not self._validate_row(row):
            return
        ids = [vid for vid, on in row.voucher_checks.items() if on]
        self._set_processing(True)
        try:
            from app import voucher_service

            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            pdf_bytes = voucher_service.build_vouchers_pdf_bytes(ids, data)
            self._open_preview_window(pdf_bytes)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "プレビューエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "プレビューエラー", f"プレビュー作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    # ── 印刷（行単位）────────────────────────────────────────────────────────
    def _on_print(self, rw: _RowWidgets) -> None:
        if not self._row_has_olap_data(rw):
            QMessageBox.warning(self, "入力エラー", "先にOLAPデータを取得してください。")
            return
        row = self._collect_row(rw)
        if not self._validate_row(row):
            return
        ids = [vid for vid, on in row.voucher_checks.items() if on]
        self._set_processing(True)
        try:
            from app import voucher_service, voucher_print_service
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            pdf_bytes = voucher_service.build_vouchers_pdf_bytes(ids, data)
            voucher_print_service.print_pdf_with_dialog(pdf_bytes, self)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "印刷エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "印刷エラー", f"印刷中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

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
            parts.append((row, voucher_service.build_vouchers_pdf_bytes(ids, data)))
        return parts

    def _on_select_pdf(self) -> None:
        collected = self._collect_selected_rows()
        if not collected:
            return
        self._set_processing(True)
        try:
            from app import voucher_service

            output_dir = self._resolve_pdf_output_dir()
            parts = self._build_selected_pdf_parts(collected)
            pdf_paths = [
                voucher_service.save_pdf_bytes(pdf_bytes, output_dir=output_dir, filename_token=row.order_no)
                for row, pdf_bytes in parts
            ]
            QMessageBox.information(self, "PDF作成完了", "PDFを作成しました:\n" + "\n".join(str(path) for path in pdf_paths))
            for pdf_path in pdf_paths:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    def _on_select_preview(self) -> None:
        collected = self._collect_selected_rows()
        if not collected:
            return
        self._set_processing(True)
        try:
            from app import voucher_service

            parts = [pdf_bytes for _row, pdf_bytes in self._build_selected_pdf_parts(collected)]
            merged = voucher_service.merge_pdf_bytes(parts)
            self._open_preview_window(merged)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "プレビューエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "プレビューエラー", f"プレビュー作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    def _on_select_print(self) -> None:
        collected = self._collect_selected_rows()
        if not collected:
            return
        self._set_processing(True)
        try:
            from app import voucher_service, voucher_print_service

            parts = [pdf_bytes for _row, pdf_bytes in self._build_selected_pdf_parts(collected)]
            merged = voucher_service.merge_pdf_bytes(parts)
            voucher_print_service.print_pdf_with_dialog(merged, self)
        except MissingVoucherNoError as exc:
            self._show_missing_voucher_no_warning(exc)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "印刷エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "印刷エラー", f"印刷中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

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
        data["am_pm"] = row.am_pm
        data["process_checks"] = dict(row.process_checks)
        data["voucher_checks"] = dict(row.voucher_checks)

        pages = data.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if isinstance(page, dict):
                    page["row_finish_date"] = row.finish_date
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
        config = load_app_config()
        logger = logging.getLogger("tks_to_kintone_app")
        if not logger.handlers:
            logger, _ = setup_logger(config.paths.log_dir)
        service = VoucherOlapService(config, logger)
        try:
            rows = service.fetch_vouchers(numbers, self.olap_login_id, self.olap_password)
        except Exception:
            logger.exception("売上伝票用OLAPデータ取得に失敗しました。受注No=%s", ",".join(numbers))
            raise
        if not rows:
            if service.last_response_r1_count > 0:
                logger.error(
                    "売上伝票用OLAPデータの行抽出結果が0件でした。受注No=%s R1List件数=%s display_mapping=%s",
                    ",".join(numbers),
                    service.last_response_r1_count,
                    display_mapping_summary(),
                )
                raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
            logger.info("売上伝票用OLAPデータが0件でした。受注No=%s", ",".join(numbers))
            raise RuntimeError("対象データが見つかりません。\n受注Noを確認してください。")
        missing_order_nos = _missing_voucher_no_order_numbers(rows, numbers)
        if missing_order_nos:
            for order_no in missing_order_nos:
                logger.warning("伝票Noなしのため伝票作成を中止: order_no=%s", order_no)
            logger.warning("伝票Noなし対象受注No: %s", "、".join(sorted(missing_order_nos)))
            self._voucher_no_blocked_order_nos.update(missing_order_nos)
            self.refresh_kintone_buttons()
            raise MissingVoucherNoError(sorted(missing_order_nos))
        self._voucher_no_blocked_order_nos.difference_update(str(n).strip() for n in numbers if str(n).strip())
        self.refresh_kintone_buttons()
        if not any(_has_minimum_detail_mapping(row) for row in rows):
            logger.error(
                "売上伝票用OLAPデータの明細判定に失敗しました。受注No=%s first_row_keys=%s display_mapping=%s",
                ",".join(numbers),
                sorted(rows[0].keys()),
                display_mapping_summary(),
            )
            raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
        logger.info("売上伝票OLAP先頭行キー一覧: %s", sorted(rows[0].keys()))
        pages = build_voucher_pages(rows)
        if not pages:
            logger.error(
                "売上伝票用OLAPデータのページ変換結果が0件でした。受注No=%s response_keys=%s display_mapping=%s",
                ",".join(numbers),
                sorted(rows[0].keys()),
                display_mapping_summary(),
            )
            raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
        logger.info("売上伝票マッピング後voucher page件数: %s", len(pages))
        _log_voucher_page_diagnostics(logger, pages[0])
        missing = _missing_required_voucher_fields(pages)
        if missing:
            logger.error(
                "売上伝票PDF生成前バリデーション失敗: missing=%s response_keys=%s display_mapping=%s",
                missing,
                sorted(rows[0].keys()),
                display_mapping_summary(),
            )
            raise RuntimeError("OLAPデータの変換に失敗しました。\n項目マッピングを確認してください。")
        logger.info("売上伝票PDFデータ作成: 受注No=%s rows=%s pages=%s", ",".join(numbers), len(rows), len(pages))
        # raw_rows は OLAPキャッシュ保存用（正規化前データ）。PDF描画では pages のみ使用する。
        return {"pages": pages, "raw_rows": rows}

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

    def _open_preview_window(self, pdf_bytes: bytes) -> "VoucherPrintPreviewWindow":
        """PDFバイト列をアプリ内プレビュー画面で表示する。

        一時PDFファイルも正式PDFも保存せず、メモリ上のバイト列をそのまま渡す。
        """
        from app.voucher_preview_window import VoucherPrintPreviewWindow

        preview = VoucherPrintPreviewWindow(pdf_bytes, parent=self)
        preview.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # 参照を保持してGCを防ぐ。
        self._preview_window = preview
        preview.showMaximized()
        return preview

    def _create_pdf(self, ids: list[str], data: dict, *, output_dir: Path, open_after: bool) -> "Path | None":
        try:
            from app import voucher_service
            pdf_path = voucher_service.create_vouchers_pdf(ids, data, output_dir=output_dir)
        except FileNotFoundError as exc:
            QMessageBox.critical(self, "テンプレートエラー", str(exc))
            return None
        except RuntimeError as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
            return None
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
            return None

        QMessageBox.information(
            self, "PDF作成完了",
            f"PDFを作成しました:\n{pdf_path}",
        )
        if open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
        return pdf_path

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

    def _restore_saved_records(self) -> bool:
        try:
            path = self._records_path()
            if not path.is_file():
                return False
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                return False
            payload_saved_at = str(payload.get("saved_at") or "") if isinstance(payload, dict) else ""
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception("伝票一覧レコード読み込みに失敗しました。")
            return False

        kept = self._filter_records_by_retention(records, path)
        logging.getLogger("tks_to_kintone_app").info("voucher records loaded count: %s", len(kept))
        if not kept:
            return False
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
        # 同じ受注Noが複数保存されている場合は更新日時が新しい1件だけ残す（要件7）。
        # 上で更新日時の降順に並べ替え済みのため、先頭（最新）から見て初出のみ採用する。
        deduped_records: list[dict] = []
        seen_order_nos: set[str] = set()
        logger = logging.getLogger("tks_to_kintone_app")
        for record in normalized_records:
            key = normalize_order_no(record.get("order_no"))
            if key and key in seen_order_nos:
                logger.info(
                    "復元時に重複受注Noを除外しました（最新1件のみ採用）。受注No=%s",
                    str(record.get("order_no") or "").strip(),
                )
                continue
            if key:
                seen_order_nos.add(key)
            deduped_records.append(record)
        normalized_records = deduped_records
        self._restoring_records = True
        try:
            for record in normalized_records:
                try:
                    rw = self._add_row()
                    self._apply_saved_record_to_row(rw, record)
                except Exception:
                    logging.getLogger("tks_to_kintone_app").warning(
                        "伝票一覧レコードの一部復元をスキップしました。受注No=%s",
                        str(record.get("order_no") or "").strip(),
                        exc_info=True,
                    )
            self._refresh_registration_status_buttons()
            self._apply_filters()
        finally:
            self._restoring_records = False
        self._save_records()
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
        if finish_date is None:
            rw.finish_none_check.setChecked(True)
        else:
            rw.finish_none_check.setChecked(False)
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
