"""伝票作成・印刷画面。

受注Noごとに1行で設定する一覧形式の画面。
1行 = 1つの受注No を扱い、行ごとに仕上日・AM/PM・加工名チェック・
印刷する伝票チェックを設定し、行単位で PDF作成・印刷を実行できる。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QDate, QSettings, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
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
from app.voucher_data_mapper import build_voucher_pages, display_mapping_summary
from app.voucher_olap_service import VoucherOlapService
from app.voucher_settings import (
    DEFAULT_CACHE_RETENTION_DAYS,
    load_cache_retention_days,
    load_default_print_types,
    save_cache_retention_days,
    save_default_print_types,
)
from app.voucher_templates import VOUCHER_TYPES
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.version import VERSION_NAME

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
]

# 一覧の列構成
COLUMN_LABELS: list[str] = [
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
COL_DELETE = 11

# 行ごとの削除ボタンの警告色スタイル（ライト/ダーク両モードで白文字が読める: 要件1-5・3）。
ROW_DELETE_BUTTON_STYLE = """
QPushButton {
    background-color: #c62828;
    color: white;
    border: 1px solid #8e0000;
    border-radius: 3px;
    font-weight: bold;
    padding: 2px 10px;
}
QPushButton:hover { background-color: #d32f2f; }
QPushButton:disabled { background-color: #9e9e9e; color: #eeeeee; border: 1px solid #757575; }
"""

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
        self.select_check: QCheckBox
        self.order_input: QLineEdit
        self.refetch_button: QPushButton
        self.date_edit: QDateEdit
        self.ampm_group: QButtonGroup
        self.ampm_am: QRadioButton
        self.ampm_pm: QRadioButton
        self.process_checks: dict[str, QCheckBox] = {}
        self.voucher_checks: dict[str, QCheckBox] = {}
        # 取り直しで再取得したOLAPデータの保持（再利用・更新確認用）。
        self.cached_olap: dict | None = None
        self.edit_button: QPushButton
        self.pdf_button: QPushButton
        self.preview_button: QPushButton
        self.print_button: QPushButton
        self.delete_button: QPushButton


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


class VoucherWindow(QMainWindow):
    """伝票作成・印刷画面（受注一覧形式）。"""

    back_requested = Signal()

    def __init__(self, olap_login_id: str = "", olap_password: str = "") -> None:
        super().__init__()
        self.olap_login_id = olap_login_id
        self.olap_password = olap_password

        self.setWindowTitle(f"伝票作成・印刷 — TKS OLAP to kintone {VERSION_NAME}")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        # 起動直後から全8列（受注No〜印刷）が横スクロールなしで見える初期幅。
        self.resize(1560, 760)
        self.setMinimumSize(1360, 680)

        self._rows: list[_RowWidgets] = []

        # 新規行の「印刷する伝票」初期チェック（設定から読み込み・以後の追加行に反映）
        self._default_print_types: set[str] = set(load_default_print_types())
        # 起動時に期限切れのOLAPキャッシュを削除する。
        self._cleanup_expired_cache()

        # 上部の行操作ボタン
        self._add_row_button = QPushButton("行追加")
        self._remove_row_button = QPushButton("選択削除")
        self._voucher_settings_button = QPushButton("印刷する伝票設定")
        self._select_pdf_button = QPushButton("選択PDF作成")
        self._select_preview_button = QPushButton("選択プレビュー")
        self._select_print_button = QPushButton("選択印刷")

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

        self._back_button = QPushButton("戻る")

        self._build_layout()

        self._add_row_button.clicked.connect(self._on_add_row)
        self._remove_row_button.clicked.connect(self._on_remove_selected)
        self._voucher_settings_button.clicked.connect(self._on_voucher_settings)
        self._select_pdf_button.clicked.connect(self._on_select_pdf)
        self._select_preview_button.clicked.connect(self._on_select_preview)
        self._select_print_button.clicked.connect(self._on_select_print)
        self._select_all_check.clicked.connect(self._on_select_all_clicked)
        self._browse_output_button.clicked.connect(self._browse_pdf_output_dir)
        self._back_button.clicked.connect(self._on_back)

        # 初期表示は空行を1行
        self._add_row()
        self._update_selection_state()

    # ── レイアウト ────────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("受注一覧"))
        top_row.addWidget(self._select_all_check)
        top_row.addStretch(1)
        top_row.addWidget(self._add_row_button)
        top_row.addWidget(self._remove_row_button)
        top_row.addWidget(self._voucher_settings_button)
        top_row.addWidget(self._select_pdf_button)
        top_row.addWidget(self._select_preview_button)
        top_row.addWidget(self._select_print_button)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("PDF出力先:"))
        output_row.addWidget(self._pdf_output_dir, 1)
        output_row.addWidget(self._browse_output_button)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._back_button)
        btn_row.addStretch(1)

        root = QVBoxLayout()
        root.addLayout(top_row)
        root.addWidget(self._table, 1)
        root.addLayout(output_row)
        root.addLayout(btn_row)

        widget = QWidget()
        widget.setLayout(root)
        self.setCentralWidget(widget)

    # ── 行の生成 ─────────────────────────────────────────────────────────────
    def _add_row(self) -> _RowWidgets:
        rw = _RowWidgets()
        row_index = self._table.rowCount()
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

        # 取り直し（受注NoでOLAPを再取得して行を更新。指図書編集内容は維持する）
        rw.refetch_button = QPushButton("取り直し")
        rw.refetch_button.setToolTip("受注Noで最新のOLAPデータを再取得します。指図書編集内容は保持されます。")
        rw.refetch_button.clicked.connect(lambda _=False, r=rw: self._on_refetch_row(r))
        self._table.setCellWidget(row_index, COL_REFETCH, self._wrap(rw.refetch_button))

        # 仕上日
        rw.date_edit = QDateEdit()
        rw.date_edit.setCalendarPopup(True)
        rw.date_edit.setDisplayFormat("yyyy/MM/dd")
        rw.date_edit.setDate(QDate.currentDate())
        self._table.setCellWidget(row_index, COL_FINISH_DATE, self._wrap(rw.date_edit))

        # AM・PM（縦2行のラジオボタン。行ごとに排他選択）
        ampm_widget = QWidget()
        ampm_layout = QVBoxLayout(ampm_widget)
        ampm_layout.setContentsMargins(4, 4, 4, 4)
        ampm_layout.setSpacing(2)
        rw.ampm_am = QRadioButton("AM")
        rw.ampm_pm = QRadioButton("PM")
        rw.ampm_group = QButtonGroup(ampm_widget)
        rw.ampm_group.setExclusive(True)
        rw.ampm_group.addButton(rw.ampm_am)
        rw.ampm_group.addButton(rw.ampm_pm)
        # 初期値は従来のコンボボックスと同じく AM。
        rw.ampm_am.setChecked(True)
        ampm_layout.addWidget(rw.ampm_am)
        ampm_layout.addWidget(rw.ampm_pm)
        self._table.setCellWidget(row_index, COL_AMPM, ampm_widget)

        # 加工名チェックボックス（3段折り返し）
        process_widget = QWidget()
        process_grid = QGridLayout(process_widget)
        process_grid.setContentsMargins(4, 4, 4, 4)
        process_grid.setHorizontalSpacing(8)
        process_grid.setVerticalSpacing(2)
        process_cols = (len(PROCESS_NAMES) + 2) // 3
        for i, name in enumerate(PROCESS_NAMES):
            cb = QCheckBox(name)
            # 加工名チェックは初期値すべてOFF（行追加時・初期空行とも）。
            cb.setChecked(False)
            rw.process_checks[name] = cb
            process_grid.addWidget(cb, i // process_cols, i % process_cols)
        self._table.setCellWidget(row_index, COL_PROCESS, process_widget)

        # 印刷する伝票チェックボックス（3段折り返し）
        voucher_widget = QWidget()
        voucher_grid = QGridLayout(voucher_widget)
        voucher_grid.setContentsMargins(4, 4, 4, 4)
        voucher_grid.setHorizontalSpacing(8)
        voucher_grid.setVerticalSpacing(2)
        v_cols = (len(VOUCHER_TYPES) + 2) // 3
        for i, (vid, vname) in enumerate(VOUCHER_TYPES):
            cb = QCheckBox(vname)
            # 印刷する伝票の初期チェックは「印刷する伝票設定」の保存値に従う。
            cb.setChecked(vid in self._default_print_types)
            rw.voucher_checks[vid] = cb
            voucher_grid.addWidget(cb, i // v_cols, i % v_cols)
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

        # 削除（行単位・赤い警告色）。一番右の列に配置する（要件1-4・1-5）。
        rw.delete_button = QPushButton("削除")
        rw.delete_button.setObjectName("rowDeleteButton")
        rw.delete_button.setProperty("danger", True)
        rw.delete_button.setStyleSheet(ROW_DELETE_BUTTON_STYLE)
        rw.delete_button.clicked.connect(lambda _=False, r=rw: self._on_delete_row(r))
        self._table.setCellWidget(row_index, COL_DELETE, self._wrap(rw.delete_button))

        self._rows.append(rw)
        self._table.resizeColumnsToContents()
        self._table.resizeRowsToContents()
        self._apply_table_column_widths()
        return rw

    # 「加工名」「印刷する伝票」のラベルが見切れないための最低列幅（要件1-1・5）。
    # 3段化により2段時より横幅を狭められる。
    PROCESS_MIN_WIDTH = 250
    VOUCHER_MIN_WIDTH = 270

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
            COL_DELETE: 100,
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

    @staticmethod
    def _wrap(inner: QWidget) -> QWidget:
        """セル内ウィジェットに余白を持たせるためのラッパ。"""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(inner)
        return holder

    def _on_add_row(self) -> None:
        self._add_row()
        self._update_selection_state()

    def _on_remove_selected(self) -> None:
        """チェックONの行だけ削除する。削除後に最低1行は残す。"""
        indices = self._selected_indices()
        if not indices:
            return
        for index in sorted(indices, reverse=True):
            self._table.removeRow(index)
            del self._rows[index]
        # すべて削除した場合は空行を1行追加する。
        if not self._rows:
            self._add_row()
        self._update_selection_state()

    def _on_delete_row(self, rw: _RowWidgets) -> None:
        """押下した行だけを削除する。最後の1行を消した場合は空行を残す（要件1-4）。"""
        try:
            index = self._rows.index(rw)
        except ValueError:
            return
        self._table.removeRow(index)
        del self._rows[index]
        # すべて削除した場合は空行を1行追加する。
        if not self._rows:
            self._add_row()
        self._update_selection_state()

    def _on_voucher_settings(self) -> None:
        """印刷する伝票設定ダイアログを開き、保存・既存行反映を行う。"""
        dialog = VoucherPrintSettingsDialog(
            selected_ids=set(self._default_print_types),
            retention_days=load_cache_retention_days(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_ids = dialog.selected_ids()
        try:
            save_default_print_types(sorted(new_ids))
            save_cache_retention_days(dialog.retention_days())
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

    def _on_refetch_row(self, rw: _RowWidgets) -> None:
        """対象行の受注NoでOLAPを再取得し、行のOLAPデータを更新する（要件2・6）。

        仕上日・AM/PM・加工名・印刷する伝票の現在設定はウィジェットを触らないため
        そのまま維持される。また指図書編集の編集オブジェクト
        （work/voucher_edit_objects 配下）は一切削除・クリアしない。再取得するのは
        OLAP由来の伝票データだけで、編集オブジェクトは再利用できるよう残す。
        """
        row = self._collect_row(rw)
        if not row.order_no:
            # 受注Noが空の場合は再取得しない。
            QMessageBox.warning(self, "入力エラー", "受注Noを入力してください。")
            return
        button = rw.refetch_button
        original_text = button.text()
        button.setEnabled(False)
        button.setText("取得中...")
        try:
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            # 再取得したOLAPデータを行に保持し、受注No単位のキャッシュを更新する。
            # 編集オブジェクトには触れないため指図書編集内容は維持される。
            rw.cached_olap = data
            self._cache_row_olap(row, data)
        except (FileNotFoundError, RuntimeError) as exc:
            # 取得失敗時は既存データ（設定・編集内容）を壊さずメッセージのみ表示する。
            QMessageBox.critical(self, "取り直しエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(
                self, "取り直しエラー", f"OLAP再取得中に予期しないエラーが発生しました:\n{exc}"
            )
        finally:
            button.setText(original_text)
            button.setEnabled(True)

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
        except Exception:
            logging.getLogger("tks_to_kintone_app").exception(
                "OLAPキャッシュ保存に失敗しました。受注No=%s", row.order_no
            )

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
            self._remove_row_button,
            self._select_pdf_button,
            self._select_preview_button,
            self._select_print_button,
        ):
            button.setEnabled(has_selection)

    # ── 行データ収集 ──────────────────────────────────────────────────────────
    def _collect_row(self, rw: _RowWidgets) -> VoucherOrderRow:
        qd = rw.date_edit.date()
        finish_date = date(qd.year(), qd.month(), qd.day()) if qd.isValid() else None
        return VoucherOrderRow(
            order_no=rw.order_input.text().strip(),
            finish_date=finish_date,
            am_pm="PM" if rw.ampm_pm.isChecked() else "AM",
            process_checks={name: cb.isChecked() for name, cb in rw.process_checks.items()},
            voucher_checks={vid: cb.isChecked() for vid, cb in rw.voucher_checks.items()},
        )

    # ── 戻る ─────────────────────────────────────────────────────────────────
    def _on_back(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:
        """ウィンドウが閉じられるときに back_requested を emit して起動元に通知する。"""
        self.back_requested.emit()
        super().closeEvent(event)

    def _set_processing(self, processing: bool) -> None:
        for widget in (
            self._add_row_button,
            self._select_all_check,
            self._back_button,
            self._pdf_output_dir,
            self._browse_output_button,
            self._table,
        ):
            widget.setEnabled(not processing)
        if processing:
            for button in (
                self._remove_row_button,
                self._select_pdf_button,
                self._select_preview_button,
                self._select_print_button,
            ):
                button.setEnabled(False)
        else:
            # 処理終了後は選択状態に応じてボタン有効/無効を戻す。
            self._update_selection_buttons()

    # ── PDF作成（行単位）─────────────────────────────────────────────────────
    def _on_pdf(self, rw: _RowWidgets) -> None:
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
        except RuntimeError as exc:
            QMessageBox.critical(self, "PDF作成エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "PDF作成エラー", f"PDF作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    # ── プレビュー（行単位）─────────────────────────────────────────────────
    def _on_preview(self, rw: _RowWidgets) -> None:
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
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "プレビューエラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "プレビューエラー", f"プレビュー作成中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

    # ── 印刷（行単位）────────────────────────────────────────────────────────
    def _on_print(self, rw: _RowWidgets) -> None:
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
        if row.finish_date is None:
            return "仕上日を設定してください。"
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
            if message:
                errors.append(f"{i + 1}行目：{message}")
        if errors:
            QMessageBox.warning(self, "入力エラー", "\n".join(errors))
            return None
        return collected

    def _build_selected_pdf_parts(
        self, collected: list[tuple[int, VoucherOrderRow]]
    ) -> list[bytes]:
        """選択行ごとにPDFバイト列を生成して返す（既存の行単位処理を再利用）。"""
        from app import voucher_service

        parts: list[bytes] = []
        for _, row in collected:
            ids = [vid for vid, on in row.voucher_checks.items() if on]
            data = self._build_print_data([row.order_no])
            self._attach_row_settings(data, row)
            self._cache_row_olap(row, data)
            parts.append(voucher_service.build_vouchers_pdf_bytes(ids, data))
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
            merged = voucher_service.merge_pdf_bytes(parts)
            pdf_path = voucher_service.save_pdf_bytes(merged, output_dir=output_dir, filename_token="multi")
            QMessageBox.information(self, "PDF作成完了", f"PDFを作成しました:\n{pdf_path}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pdf_path)))
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

            parts = self._build_selected_pdf_parts(collected)
            merged = voucher_service.merge_pdf_bytes(parts)
            self._open_preview_window(merged)
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

            parts = self._build_selected_pdf_parts(collected)
            merged = voucher_service.merge_pdf_bytes(parts)
            voucher_print_service.print_pdf_with_dialog(merged, self)
        except (FileNotFoundError, RuntimeError) as exc:
            QMessageBox.critical(self, "印刷エラー", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "印刷エラー", f"印刷中に予期しないエラーが発生しました:\n{exc}")
        finally:
            self._set_processing(False)

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
        preview.show()
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


def _has_minimum_detail_mapping(row: dict[str, str]) -> bool:
    checks = (
        ("order_no", "6"),
        ("customer_name", "5"),
        ("voucher_no", "9"),
        ("product_name", "16"),
    )
    return any(str(row.get(alias) or row.get(fallback) or "").strip() for alias, fallback in checks)


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
