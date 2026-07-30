"""受注No範囲取得の検証、逐次worker、進捗・結果UI。"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterator

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

MAX_ORDER_RANGE_COUNT = 500
LARGE_RANGE_CONFIRM_COUNT = 101
_RANGE_THREADS: set[QThread] = set()
_RANGE_ORDER_NO_PATTERN = re.compile(r"^([A-Za-z]*)([0-9]+)$")


@dataclass(frozen=True)
class ParsedOrderNo:
    """範囲生成に使う、受注Noの固定プレフィックスと末尾連番。"""

    prefix: str
    number: int
    number_text: str
    width: int


@dataclass(frozen=True)
class OrderRangeValidation:
    start: str = ""
    end: str = ""
    count: int = 0
    valid: bool = False
    error: str = ""
    limit_exceeded: bool = False


def _normalize_order_no(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "").strip())


def parse_range_order_no(value: object) -> ParsedOrderNo:
    """NFKC正規化後の「英字* + 数字+」を解析する。"""
    normalized = _normalize_order_no(value)
    match = _RANGE_ORDER_NO_PATTERN.fullmatch(normalized)
    if match is None:
        if not re.search(r"[0-9]+$", normalized):
            raise ValueError("受注Noの末尾には連番となる数字が必要です。")
        raise ValueError("受注Noは、英字の後に数字が続く形式で入力してください。")
    prefix, number_text = match.groups()
    return ParsedOrderNo(prefix, int(number_text), number_text, len(number_text))


def validate_order_range(
    start_value: object,
    end_value: object,
    *,
    normalizer: Callable[[object], str] = _normalize_order_no,
) -> OrderRangeValidation:
    """リスト/iteratorを作らず、両端と件数だけを検証する。"""
    # 既存の単一取得用normalizerを通した後も、範囲の共通仕様として
    # NFKCを必ず適用する。
    start = _normalize_order_no(normalizer(start_value))
    end = _normalize_order_no(normalizer(end_value))
    if not start or not end:
        return OrderRangeValidation(start, end, error="開始受注Noと終了受注Noを入力してください。")
    try:
        parsed_start = parse_range_order_no(start)
        parsed_end = parse_range_order_no(end)
    except ValueError as exc:
        return OrderRangeValidation(start, end, error=str(exc))
    if parsed_start.prefix.casefold() != parsed_end.prefix.casefold():
        return OrderRangeValidation(
            start, end, error="開始受注Noと終了受注Noの英字部分が一致していません。"
        )
    if parsed_start.number > parsed_end.number:
        return OrderRangeValidation(
            start, end, error="開始受注Noは終了受注No以下で入力してください。"
        )
    count = parsed_end.number - parsed_start.number + 1
    exceeded = count > MAX_ORDER_RANGE_COUNT
    error = ""
    if exceeded:
        error = (
            f"一度に取得できる受注Noは{MAX_ORDER_RANGE_COUNT}件までです。\n"
            "開始受注Noと終了受注Noの範囲を狭めてください。\n"
            f"現在の対象件数: {count:,}件"
        )
    return OrderRangeValidation(start, end, count, not exceeded, error, exceeded)


def validate_order_no_range(
    start_value: object,
    end_value: object,
    *,
    normalizer: Callable[[object], str] = _normalize_order_no,
) -> OrderRangeValidation:
    """意味が明確な公開名。旧名 ``validate_order_range`` も互換維持する。"""
    return validate_order_range(start_value, end_value, normalizer=normalizer)


def iter_order_no_range(start: str, end: str) -> Iterator[str]:
    """開始側の英字表記と数値幅を保ち、末尾数値だけを逐次増加する。"""
    validation = validate_order_range(start, end)
    if not validation.valid:
        raise ValueError(validation.error)
    parsed_start = parse_range_order_no(validation.start)
    parsed_end = parse_range_order_no(validation.end)
    for number in range(parsed_start.number, parsed_end.number + 1):
        yield f"{parsed_start.prefix}{number:0{parsed_start.width}d}"


def iter_order_range(start: str, end: str) -> Iterator[str]:
    """既存呼び出し向け互換名。"""
    yield from iter_order_no_range(start, end)


def failure_classification(exc: BaseException) -> tuple[str, bool]:
    text = f"{type(exc).__name__}: {exc}"
    lower = text.lower()
    fatal = any(word in lower for word in ("認証", "authentication", "unauthorized", "forbidden", "401", "403"))
    if fatal:
        return "authentication", True
    if "timeout" in lower or "タイムアウト" in lower:
        return "timeout", False
    if "見つかりません" in text or "0件" in text:
        return "not_found", False
    if "変換" in text or "mapping" in lower or "解析" in text:
        return "parse", False
    return "fetch", False


class VoucherRangeWorker(QObject):
    item_started = Signal(str)
    item_fetched = Signal(str, object)
    item_failed = Signal(str, str, str)
    finished = Signal(bool, int)

    def __init__(self, start: str, end: str, fetch_one: Callable[[str], dict]) -> None:
        super().__init__()
        self.start_order_no = start
        self.end_order_no = end
        self._fetch_one = fetch_one
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        validation = validate_order_range(self.start_order_no, self.end_order_no)
        if not validation.valid:
            self.finished.emit(False, validation.count)
            return
        processed = 0
        cancelled = False
        for order_no in iter_order_no_range(validation.start, validation.end):
            if self._cancel.is_set():
                cancelled = True
                break
            self.item_started.emit(order_no)
            try:
                data = self._fetch_one(order_no)
            except Exception as exc:  # noqa: BLE001 - 1件失敗で範囲全体を止めない
                classification, fatal = failure_classification(exc)
                self.item_failed.emit(order_no, str(exc), classification)
                processed += 1
                if fatal:
                    break
            else:
                self.item_fetched.emit(order_no, data)
                processed += 1
        self.finished.emit(cancelled, processed)


class VoucherRangeResultDialog(QDialog):
    def __init__(self, successes: list[tuple[str, str]], failures: list[tuple[str, str]], parent=None,
                 *, unprocessed_count: int = 0, cancelled: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("範囲取得 登録結果")
        self.resize(820, 520)
        self._successes = successes
        self._failures = failures
        panes = QHBoxLayout()
        success_group, self.success_table, self.success_copy = self._pane(
            f"登録成功（{len(successes)}件）", ["受注No", "状態"], "成功した受注Noをコピー"
        )
        for order_no, status in successes:
            self._append_row(self.success_table, [order_no, status])
        failure_group, self.failure_table, self.failure_copy = self._pane(
            f"登録失敗（{len(failures)}件）", ["受注No", "理由"], "失敗した受注Noをコピー"
        )
        for order_no, reason in failures:
            self._append_row(self.failure_table, [order_no, reason])
        panes.addWidget(success_group, 1)
        panes.addWidget(failure_group, 1)
        self.success_copy.setEnabled(bool(successes))
        self.failure_copy.setEnabled(bool(failures))
        self.success_copy.clicked.connect(lambda: self._copy([x[0] for x in successes], "voucher_range_success_copied"))
        self.failure_copy.clicked.connect(lambda: self._copy([x[0] for x in failures], "voucher_range_failure_copied"))
        self.status_label = QLabel("")
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)
        bottom.addWidget(close_button)
        root = QVBoxLayout(self)
        if cancelled or unprocessed_count:
            reason = "中止しました。" if cancelled else "致命的エラーのため中断しました。"
            root.addWidget(QLabel(f"{reason} 未処理: {unprocessed_count}件"))
        root.addLayout(panes, 1)
        root.addLayout(bottom)
        logging.getLogger("tks_to_kintone_app").info("voucher_range_result_dialog_opened success=%s failure=%s", len(successes), len(failures))

    @staticmethod
    def _pane(title: str, headers: list[str], copy_text: str):
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        button = QPushButton(copy_text)
        layout.addWidget(table, 1)
        layout.addWidget(button)
        return group, table, button

    @staticmethod
    def _append_row(table: QTableWidget, values: list[str]) -> None:
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(value))

    def _copy(self, numbers: list[str], event: str) -> None:
        if not numbers:
            return
        QApplication.clipboard().setText("\n".join(numbers))
        self.status_label.setText(f"{len(numbers)}件をコピーしました。")
        logging.getLogger("tks_to_kintone_app").info("%s count=%s", event, len(numbers))


class VoucherRangeDialog(QDialog):
    """非モーダルで進捗を表示し、OLAP取得だけをQThreadで行う画面。"""
    def __init__(self, owner, fetch_one: Callable[[str], dict], normalizer: Callable[[object], str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("受注No 範囲指定")
        self.setModal(False)
        self._owner = owner
        self._fetch_one = fetch_one
        self._normalizer = normalizer
        self._thread: QThread | None = None
        self._worker: VoucherRangeWorker | None = None
        self._successes: list[tuple[str, str]] = []
        self._failures: list[tuple[str, str]] = []
        self._started_at = 0.0
        self._total = 0
        self._completed = 0
        self._cancelled = False

        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        input_examples = (
            "受注Noは数字、または英字＋数字で入力してください。\n"
            "入力例: 405113 ～ 405120 / C405113 ～ C405120"
        )
        self.start_edit.setToolTip(input_examples)
        self.end_edit.setToolTip(input_examples)
        self.example_label = QLabel(input_examples)
        self.count_label = QLabel("0件（上限500件）")
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #c62828;")
        self.current_label = QLabel("処理中: -")
        self.progress_label = QLabel("0 / 0件 完了")
        self.result_count_label = QLabel("成功: 0件　失敗: 0件")
        self.progress_bar = QProgressBar()
        self.fetch_button = QPushButton("取得")
        self.cancel_button = QPushButton("キャンセル")
        form = QGridLayout()
        form.addWidget(QLabel("開始受注No:"), 0, 0)
        form.addWidget(self.start_edit, 0, 1)
        form.addWidget(QLabel("終了受注No:"), 1, 0)
        form.addWidget(self.end_edit, 1, 1)
        form.addWidget(QLabel("件数:"), 2, 0)
        form.addWidget(self.count_label, 2, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.fetch_button)
        buttons.addWidget(self.cancel_button)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(self.example_label)
        root.addWidget(self.error_label)
        root.addWidget(self.current_label)
        root.addWidget(self.progress_label)
        root.addWidget(self.result_count_label)
        root.addWidget(self.progress_bar)
        root.addLayout(buttons)
        self.start_edit.textChanged.connect(self._revalidate)
        self.end_edit.textChanged.connect(self._revalidate)
        self.fetch_button.clicked.connect(self.start_fetch)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        self._revalidate()

    def validation(self) -> OrderRangeValidation:
        return validate_order_range(self.start_edit.text(), self.end_edit.text(), normalizer=self._normalizer)

    @Slot()
    def _revalidate(self) -> None:
        result = self.validation()
        self.count_label.setText(f"{result.count:,}件（上限{MAX_ORDER_RANGE_COUNT}件）")
        self.error_label.setText(result.error)
        running = self._thread is not None
        self.fetch_button.setEnabled(result.valid and not running)
        self.fetch_button.setToolTip(result.error if not result.valid else "")

    @Slot()
    def start_fetch(self) -> bool:
        # ボタンの有効状態に依存せず、押下時にも必ず再検証する。
        result = self.validation()
        logger = logging.getLogger("tks_to_kintone_app")
        if result.limit_exceeded:
            logger.warning(
                "voucher_range_limit_exceeded start_order_no=%s end_order_no=%s requested_count=%s max_count=%s",
                result.start, result.end, result.count, MAX_ORDER_RANGE_COUNT,
            )
            self.error_label.setText(result.error)
            return False
        if not result.valid:
            logger.warning("voucher_range_validation_failed start_order_no=%s end_order_no=%s reason=%s", result.start, result.end, result.error)
            self.error_label.setText(result.error)
            return False
        if result.count >= LARGE_RANGE_CONFIRM_COUNT:
            answer = QMessageBox.question(
                self,
                "多数の受注Noを取得",
                f"{result.count:,}件の受注Noを取得します。\n処理に時間がかかる可能性があります。続行しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                logger.info("voucher_range_large_cancelled total_count=%s", result.count)
                return False
            logger.info("voucher_range_large_confirmed total_count=%s", result.count)
        self._start_worker(result)
        return True

    def _start_worker(self, result: OrderRangeValidation) -> None:
        if self._thread is not None:
            return
        self._successes.clear()
        self._failures.clear()
        self._total = result.count
        self._completed = 0
        self._cancelled = False
        self._started_at = time.perf_counter()
        self.progress_bar.setRange(0, self._total)
        self.progress_bar.setValue(0)
        self.start_edit.setEnabled(False)
        self.end_edit.setEnabled(False)
        self.fetch_button.setEnabled(False)
        self.cancel_button.setText("処理を中止")
        self._thread = QThread()
        _RANGE_THREADS.add(self._thread)
        self._worker = VoucherRangeWorker(result.start, result.end, self._fetch_one)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.item_started.connect(self._item_started)
        self._worker.item_fetched.connect(self._item_fetched)
        self._worker.item_failed.connect(self._item_failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread_finished)
        self._thread.finished.connect(lambda t=self._thread: _RANGE_THREADS.discard(t))
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_range_fetch_started start_order_no=%s end_order_no=%s total_count=%s",
            result.start, result.end, result.count,
        )

    @Slot(str)
    def _item_started(self, order_no: str) -> None:
        self.current_label.setText(f"処理中: {order_no}")
        logging.getLogger("tks_to_kintone_app").info("voucher_range_item_started order_no=%s", order_no)

    @Slot(str, object)
    def _item_fetched(self, order_no: str, data: object) -> None:
        try:
            status = self._owner.reflect_range_fetch_result(order_no, data)
        except Exception as exc:  # GUI一覧反映失敗は成功扱いにしない
            self._failures.append((order_no, f"一覧追加失敗: {exc}"))
            classification = "list_add"
            logging.getLogger("tks_to_kintone_app").warning("voucher_range_item_failed order_no=%s classification=%s reason=%s", order_no, classification, exc)
        else:
            self._successes.append((order_no, status))
            logging.getLogger("tks_to_kintone_app").info("voucher_range_item_succeeded order_no=%s status=%s", order_no, status)
        self._advance()

    @Slot(str, str, str)
    def _item_failed(self, order_no: str, reason: str, classification: str) -> None:
        self._failures.append((order_no, reason))
        logging.getLogger("tks_to_kintone_app").warning("voucher_range_item_failed order_no=%s classification=%s reason=%s", order_no, classification, reason)
        self._advance()

    def _advance(self) -> None:
        self._completed += 1
        self.progress_bar.setValue(self._completed)
        self.progress_label.setText(f"{self._completed} / {self._total}件 完了")
        self.result_count_label.setText(f"成功: {len(self._successes)}件　失敗: {len(self._failures)}件")
        logging.getLogger("tks_to_kintone_app").info("voucher_range_progress completed_count=%s total_count=%s success_count=%s failure_count=%s", self._completed, self._total, len(self._successes), len(self._failures))

    @Slot(bool, int)
    def _worker_finished(self, cancelled: bool, _processed: int) -> None:
        self._cancelled = cancelled
        if not cancelled and self._completed == self._total:
            self.progress_bar.setValue(self._total)
        self.current_label.setText("処理中: -")

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        self.start_edit.setEnabled(True)
        self.end_edit.setEnabled(True)
        self.cancel_button.setText("閉じる")
        self.cancel_button.setEnabled(True)
        self._revalidate()
        elapsed = time.perf_counter() - self._started_at
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_range_fetch_completed total_count=%s completed_count=%s success_count=%s failure_count=%s elapsed_seconds=%.3f cancelled=%s",
            self._total, self._completed, len(self._successes), len(self._failures), elapsed, self._cancelled,
        )
        result = VoucherRangeResultDialog(
            self._successes,
            self._failures,
            self,
            unprocessed_count=max(0, self._total - self._completed),
            cancelled=self._cancelled,
        )
        result.exec()

    def _cancel_or_close(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()
            self.cancel_button.setEnabled(False)
            logging.getLogger("tks_to_kintone_app").info("voucher_range_cancel_requested completed_count=%s total_count=%s", self._completed, self._total)
            return
        self.close()

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None:
            self.request_cancel()
            from app.gui import update_shutdown_is_committed

            if update_shutdown_is_committed():
                super().closeEvent(event)
                return
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)
