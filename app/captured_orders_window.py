"""保存済み受注No一覧画面。"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import captured_orders
from app.config import resource_path
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark

_LOGGER = logging.getLogger("tks_to_kintone_app")

_COL_CHECK = 0
_COL_ORDER_NO = 1
_COL_CAPTURED_AT = 2
_COL_METHOD = 3
_COL_ACTION = 4
_HEADERS = ["□", "受注No", "保存日時", "保存方法", "操作"]

_BUTTON_STYLE = """
QPushButton#addToVoucherButton {
    background-color: #1F7A4D;
    color: white;
    border: 1px solid #17613D;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#deleteButton, QPushButton[rowDeleteButton="true"] {
    background-color: #B42318;
    color: white;
    border: 1px solid #8F1D14;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton#closeButton {
    background-color: #5F6673;
    color: white;
    border: 1px solid #4B5563;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton:disabled,
QPushButton#addToVoucherButton:disabled,
QPushButton#deleteButton:disabled,
QPushButton#closeButton:disabled,
QPushButton[rowDeleteButton="true"]:disabled {
    background-color: #D1D5DB;
    color: #6B7280;
    border: 1px solid #C4C9D1;
}
QPushButton:disabled:hover,
QPushButton:disabled:pressed,
QPushButton#addToVoucherButton:disabled:hover,
QPushButton#addToVoucherButton:disabled:pressed,
QPushButton#deleteButton:disabled:hover,
QPushButton#deleteButton:disabled:pressed,
QPushButton#closeButton:disabled:hover,
QPushButton#closeButton:disabled:pressed,
QPushButton[rowDeleteButton="true"]:disabled:hover,
QPushButton[rowDeleteButton="true"]:disabled:pressed {
    background-color: #D1D5DB;
    color: #6B7280;
    border: 1px solid #C4C9D1;
}
"""


class CapturedOrdersWindow(QWidget):
    """保存済み受注Noの確認・編集画面（独立ウィンドウ）。"""

    saved = Signal()
    closed = Signal()

    def __init__(self, voucher_window_provider=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._voucher_window_provider = voucher_window_provider
        self._reloading = False
        self._updating_checks = False

        self.setWindowTitle("保存済み受注No一覧")
        self.setWindowIcon(QIcon(str(resource_path("assets/app_icon.ico"))))
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(640, 400)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnWidth(_COL_CHECK, 56)
        self._table.setColumnWidth(_COL_ACTION, 80)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_COL_ORDER_NO, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_CAPTURED_AT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_METHOD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeMode.Fixed)

        self._status_label = QLabel("")
        self._add_to_voucher_button = QPushButton("伝票一覧に追加")
        self._add_to_voucher_button.setObjectName("addToVoucherButton")
        self._delete_row_button = QPushButton("選択削除")
        self._delete_row_button.setObjectName("deleteButton")
        self._close_button = QPushButton("閉じる")
        self._close_button.setObjectName("closeButton")
        self.setStyleSheet(_BUTTON_STYLE)

        self._build_layout()

        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self._add_to_voucher_button.clicked.connect(self._on_add_to_voucher)
        self._delete_row_button.clicked.connect(self._on_delete_checked_rows)
        self._close_button.clicked.connect(self.close)

        self._reload()

    def _build_layout(self) -> None:
        buttons = QHBoxLayout()
        buttons.addWidget(self._status_label, 1)
        buttons.addWidget(self._add_to_voucher_button)
        buttons.addWidget(self._delete_row_button)
        buttons.addWidget(self._close_button)

        root = QVBoxLayout()
        root.addWidget(self._table)
        root.addLayout(buttons)
        self.setLayout(root)

    def _reload(self) -> None:
        try:
            orders = captured_orders.load_captured_orders()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("保存済み受注Noの読み込みに失敗しました。", exc_info=True)
            orders = []
        self._reloading = True
        try:
            self._table.setRowCount(0)
            for order in orders:
                self._append_row(order)
        finally:
            self._reloading = False
        self._refresh_buttons()
        self._refresh_header_check_state()
        self._refresh_button_styles()

    def note_saved_order(self, order_no: str) -> None:
        """自動保存された1件を一覧へ増分反映する（全再構築を避ける・要件8）。

        既に表示済みなら何もしない。表示されていなければキャッシュから該当行を探して
        1行だけ追加する。
        """
        normalized = captured_orders.normalize_captured_order_no(order_no)
        if not normalized:
            return
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_ORDER_NO)
            if item is not None and captured_orders.normalize_captured_order_no(item.text()) == normalized:
                return
        order: dict = {"order_no": normalized}
        try:
            for entry in captured_orders.load_captured_orders():
                if captured_orders.normalize_captured_order_no(entry.get("order_no")) == normalized:
                    order = entry
                    break
        except Exception:  # noqa: BLE001 - 一覧更新失敗でUIを落とさない
            _LOGGER.debug("増分反映用の受注No情報取得に失敗しました。", exc_info=True)
        self._reloading = True
        try:
            self._append_row(order)
        finally:
            self._reloading = False
        self._refresh_buttons()
        self._refresh_header_check_state()

    def _append_row(self, order: dict) -> int:
        row = self._table.rowCount()
        self._table.insertRow(row)

        check = QCheckBox()
        check.stateChanged.connect(self._refresh_buttons)
        check_box = QWidget()
        check_layout = QHBoxLayout(check_box)
        check_layout.setContentsMargins(0, 0, 0, 0)
        check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        check_layout.addWidget(check)
        self._table.setCellWidget(row, _COL_CHECK, check_box)

        order_item = QTableWidgetItem(str(order.get("order_no", "")))
        order_item.setFlags(order_item.flags() | Qt.ItemFlag.ItemIsEditable)
        order_item.setData(Qt.ItemDataRole.UserRole, dict(order))
        self._table.setItem(row, _COL_ORDER_NO, order_item)

        self._set_readonly_cell(row, _COL_CAPTURED_AT, str(order.get("captured_at", "")))
        self._set_readonly_cell(row, _COL_METHOD, str(order.get("method", "")))

        delete_button = QPushButton("削除")
        delete_button.setProperty("rowDeleteButton", True)
        delete_button.setEnabled(True)
        delete_button.clicked.connect(lambda _checked=False, b=delete_button: self._delete_button_row(b))
        self._table.setCellWidget(row, _COL_ACTION, delete_button)
        self._refresh_button_styles()
        return row

    def _set_readonly_cell(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, col, item)

    def _checkbox_at(self, row: int) -> QCheckBox | None:
        container = self._table.cellWidget(row, _COL_CHECK)
        if container is None:
            return None
        return container.findChild(QCheckBox)

    def _delete_button_at(self, row: int) -> QPushButton | None:
        widget = self._table.cellWidget(row, _COL_ACTION)
        return widget if isinstance(widget, QPushButton) else None

    def _checked_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self._table.rowCount()):
            checkbox = self._checkbox_at(row)
            if checkbox is not None and checkbox.isChecked():
                rows.append(row)
        return rows

    def _refresh_buttons(self, *_args: object) -> None:
        if self._updating_checks:
            return
        checked = set(self._checked_rows())
        has_checked = bool(checked)
        self._delete_row_button.setEnabled(has_checked)
        self._add_to_voucher_button.setEnabled(has_checked and self._current_voucher_window() is not None)
        self._refresh_header_check_state()
        self._refresh_button_styles()

    def _refresh_button_styles(self) -> None:
        """有効/無効変更後も stylesheet の disabled 表示を確実に再評価する。"""
        buttons = [
            self._add_to_voucher_button,
            self._delete_row_button,
            self._close_button,
        ]
        for row in range(self._table.rowCount()):
            button = self._delete_button_at(row)
            if button is not None:
                buttons.append(button)
        for button in buttons:
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def refresh_voucher_state(self) -> None:
        """伝票一覧画面の起動状態に合わせて追加ボタンを更新する。"""
        self._refresh_buttons()

    def _on_header_clicked(self, section: int) -> None:
        if section != _COL_CHECK:
            return
        row_count = self._table.rowCount()
        if row_count <= 0:
            return
        checked_count = len(self._checked_rows())
        target_checked = checked_count != row_count
        self._updating_checks = True
        try:
            for row in range(row_count):
                checkbox = self._checkbox_at(row)
                if checkbox is not None:
                    checkbox.setChecked(target_checked)
        finally:
            self._updating_checks = False
        self._refresh_buttons()

    def _refresh_header_check_state(self) -> None:
        item = self._table.horizontalHeaderItem(_COL_CHECK)
        if item is None:
            return
        row_count = self._table.rowCount()
        checked_count = len(self._checked_rows())
        if row_count == 0 or checked_count == 0:
            item.setText("□")
        elif checked_count == row_count:
            item.setText("☑")
        else:
            item.setText("◩")

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._reloading or col != _COL_ORDER_NO:
            return
        item = self._table.item(row, col)
        normalized = captured_orders.normalize_captured_order_no(item.text() if item else "")
        if normalized and item is not None and item.text() != normalized:
            self._reloading = True
            try:
                item.setText(normalized)
            finally:
                self._reloading = False
        self._auto_save_changes()

    def _collect_rows(self) -> tuple[list[dict], list[str]]:
        result: list[dict] = []
        errors: list[str] = []
        seen: set[str] = set()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_ORDER_NO)
            raw = item.text() if item is not None else ""
            normalized = captured_orders.normalize_captured_order_no(raw)
            if normalized is None:
                if not str(raw).strip():
                    errors.append(f"{row + 1}行目: 受注Noが空です")
                else:
                    errors.append(f"{row + 1}行目: 受注Noは7桁以上の半角数字で入力してください")
                continue
            if normalized in seen:
                errors.append(f"{row + 1}行目: 受注No {normalized} が重複しています")
                continue
            seen.add(normalized)
            base = {}
            if item is not None:
                stored = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(stored, dict):
                    base = dict(stored)
            base["order_no"] = normalized
            base.setdefault("captured_at", datetime.now().isoformat(timespec="seconds"))
            base.setdefault("source", captured_orders.SOURCE_TKSCLOUD8)
            base.setdefault("method", "manual")
            base.setdefault("added_to_voucher", False)
            base.setdefault("olap_fetched", False)
            result.append(base)
        return result, errors

    def _auto_save_changes(self) -> bool:
        rows, errors = self._collect_rows()
        if errors:
            self._status_label.setText("保存不可: " + " / ".join(errors))
            self._reload()
            return False
        try:
            captured_orders.save_captured_orders(rows)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("保存済み受注Noの書き込みに失敗しました。", exc_info=True)
            self._status_label.setText(f"保存失敗: {exc}")
            self._reload()
            return False
        self._status_label.setText(f"保存OK: {len(rows)}件")
        self._reload()
        self.saved.emit()
        return True

    def _on_save(self) -> bool:
        return self._auto_save_changes()

    def _delete_button_row(self, button: QPushButton) -> None:
        for row in range(self._table.rowCount()):
            if self._delete_button_at(row) is button:
                self._delete_rows([row])
                return

    def _on_delete_checked_rows(self) -> None:
        self._delete_rows(self._checked_rows())

    def _on_delete_row(self) -> None:
        self._on_delete_checked_rows()

    def _delete_rows(self, rows: list[int]) -> None:
        if not rows:
            return
        self._reloading = True
        try:
            for row in sorted(set(rows), reverse=True):
                self._table.removeRow(row)
        finally:
            self._reloading = False
        self._auto_save_changes()

    def _current_voucher_window(self):
        provider = self._voucher_window_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001
            return None

    def _on_add_to_voucher(self) -> None:
        window = self._current_voucher_window()
        if window is None:
            QMessageBox.information(
                self, "保存済み受注No一覧", "伝票作成・印刷画面を開いてください。"
            )
            return
        adder = getattr(window, "add_order_no_and_fetch", None)
        if not callable(adder):
            QMessageBox.warning(self, "保存済み受注No一覧", "伝票一覧に追加できません。")
            return

        added = 0
        duplicates = 0
        failed = 0
        remove_targets: set[str] = set()
        for row in self._checked_rows():
            item = self._table.item(row, _COL_ORDER_NO)
            order_no = captured_orders.normalize_captured_order_no(item.text() if item else "")
            if not order_no:
                continue
            try:
                result = adder(order_no)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("伝票一覧への追加に失敗しました: %s", order_no, exc_info=True)
                QMessageBox.critical(
                    self, "保存済み受注No一覧", f"伝票一覧に追加できませんでした:\n{exc}"
                )
                return
            status = (result or {}).get("status") if isinstance(result, dict) else None
            if status == "added":
                added += 1
                remove_targets.add(order_no)
            elif status == "duplicate":
                duplicates += 1
                remove_targets.add(order_no)
            else:
                failed += 1
        removed = 0
        if remove_targets:
            try:
                removed = captured_orders.remove_captured_orders_by_order_no(remove_targets)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("追加済み受注Noの保存リスト削除に失敗しました。", exc_info=True)
                self._status_label.setText("削除失敗")
                return
        if added or duplicates or failed:
            self._reload()
            self.saved.emit()
            self._status_label.setText(
                f"追加完了: {added}件 / 重複: {duplicates}件 / 削除: {removed}件"
                + (f" / 失敗: {failed}件" if failed else "")
            )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)
