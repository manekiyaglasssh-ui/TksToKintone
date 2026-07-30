"""伝票印刷サービス。

既定では保存済み設定に従い Acrobat Reader 経由でPDFを即時印刷する。
設定で Qt直接印刷を選んだ場合のみ、従来どおり QPrinter でPDFを描画する。
"""
from __future__ import annotations

import json
import os
import logging
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from datetime import datetime, timedelta

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

from app.path_utils import get_app_data_dir, get_order_capture_debug_dir

_LOGGER = logging.getLogger(__name__)
PRINT_JOB_RETENTION_DAYS = 7
_ACTIVE_PRINT_THREADS: set[object] = set()
ACROBAT_NOT_FOUND_MESSAGE = "Acrobat Readerが見つかりません。印刷設定でAcrobat Readerのパスを指定してください。"
SUMATRA_NOT_FOUND_MESSAGE = (
    "SumatraPDFが見つかりません。\nTksToKintoneのセットアップを再実行してください。"
)
SUMATRA_PATH_MISSING_MESSAGE = SUMATRA_NOT_FOUND_MESSAGE
# SumatraPDF の終了コード（公式仕様）に対応するメッセージ。
SUMATRA_EXIT_CODE_MESSAGES = {
    0: "成功",
    2: "PDFを開けませんでした",
    3: "印刷が許可されていないPDFです",
    4: "プリンターが見つかりません",
    5: "プリンタードライバーまたはデバイスで失敗しました",
    6: "制限ポリシーにより印刷できません",
}


def _sumatra_exit_code_message(exit_code: int | None) -> str:
    if exit_code is None:
        return "終了コード不明"
    return SUMATRA_EXIT_CODE_MESSAGES.get(int(exit_code), "不明なエラー")
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
SW_HIDE = 0
SW_MINIMIZE = 6
WM_CLOSE = 0x0010
HWND_BOTTOM = 1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
ACROBAT_PROCESS_NAMES = {"acrord32.exe", "acrobat.exe"}


@dataclass
class PrintJob:
    job_id: str
    source_type: str
    order_no: str
    pdf_bytes: bytes
    print_backend: str = ""
    backend_default_source: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"
    job_name: str = ""
    selected_count: int = 1
    generated_pdf_count: int = 1
    merged_pdf_created: bool = False
    merged_pdf_path: str = ""
    ui_thread_id: int = 0
    test_print_requested: bool = False
    test_print_pdf_path: str = ""
    # テスト印刷など、QSettings へ保存せず画面上の一時設定で印刷する場合に使う。
    # None のときは従来どおり load_voucher_printer_settings() を使う。
    settings_override: object | None = None


class _FallbackSignal:
    def __init__(self) -> None:
        self._callbacks: list[Callable] = []

    def connect(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self._callbacks):
            try:
                callback(*args)
            except RuntimeError as exc:
                if "already deleted" not in str(exc):
                    raise


class PrintJobProxy:
    """キュー投入直後にUIへ返す、既存worker互換のsignalホルダー。"""

    def __init__(self, job: PrintJob) -> None:
        self.job = job
        self.status_changed = _FallbackSignal()
        self.request_sent = _FallbackSignal()
        self.finished = _FallbackSignal()
        self.error = _FallbackSignal()
        self._actual_worker = None
        self.cancel_requested = False

    def cancel(self) -> None:
        self.cancel_requested = True
        worker = self._actual_worker
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass


class PrintQueueManager:
    """外部PDF印刷を1件ずつ直列実行する軽量キュー。"""

    def __init__(self) -> None:
        self.running_job: PrintJob | None = None
        self.running_proxy: PrintJobProxy | None = None
        self.current_worker = None
        self.current_thread = None
        self.queued_jobs: list[tuple[PrintJob, PrintJobProxy]] = []

    def enqueue(self, job: PrintJob, proxy: PrintJobProxy) -> None:
        self.queued_jobs.append((job, proxy))
        log_print_recovery_event(
            "print_job_enqueued",
            print_job_enqueued=True,
            print_job_id=job.job_id,
            print_backend=job.print_backend,
            print_backend_default_source=job.backend_default_source,
            source_type=job.source_type,
            order_no=job.order_no,
            print_queue_size=len(self.queued_jobs),
            running_job_id=self.running_job.job_id if self.running_job else "",
            ui_thread_id=job.ui_thread_id,
            test_print_requested=job.test_print_requested,
            test_print_pdf_path=job.test_print_pdf_path,
        )
        proxy.status_changed.emit(
            f"印刷待機中 {len(self.queued_jobs)}件" if self.running_job else "印刷ジョブを追加しました"
        )
        self.start_next_if_idle()

    def start_next_if_idle(self) -> None:
        if self.running_job is not None:
            return
        if not self.queued_jobs:
            log_print_recovery_event("print_queue_empty", print_queue_empty=True)
            return
        job, proxy = self.queued_jobs.pop(0)
        self.running_job = job
        self.running_proxy = proxy
        job.status = "running"
        job_fields = _print_job_log_fields(job, queue_size=len(self.queued_jobs))
        log_print_recovery_event(
            "print_job_started",
            print_job_started=True,
            print_queue_next_started=True,
            **job_fields,
        )
        try:
            _validate_queued_print_job(job)
            worker = _start_print_pdf_worker(
                job.pdf_bytes,
                None,
                job_name=job.job_name,
                selected_count=job.selected_count,
                generated_pdf_count=job.generated_pdf_count,
                merged_pdf_created=job.merged_pdf_created,
                merged_pdf_path=job.merged_pdf_path,
                queued_job_id=job.job_id,
                queued_source_type=job.source_type,
                ui_thread_id=job.ui_thread_id,
                queued_print_backend=job.print_backend,
                backend_default_source=job.backend_default_source,
                settings_override=job.settings_override,
            )
        except Exception as exc:
            self.job_error(
                job.job_id,
                str(exc),
                {
                    "print_job_id": job.job_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            return
        if worker is None:
            self.job_finished(job.job_id, {"print_job_id": job.job_id})
            return
        proxy._actual_worker = worker
        self.current_worker = worker
        self.current_thread = getattr(worker, "thread", None)
        log_print_recovery_event(
            "print_queue_worker_created",
            print_queue_worker_created=True,
            print_job_id=job.job_id,
            print_backend=job.print_backend,
            source_type=job.source_type,
            worker_object_id=id(worker),
            worker_thread_object_id=id(self.current_thread) if self.current_thread is not None else "",
            ui_thread_id=job.ui_thread_id,
            current_thread_id=threading.get_ident(),
            current_thread_is_ui_thread=threading.get_ident() == job.ui_thread_id,
        )

        def _request_sent(payload: dict[str, object]) -> None:
            job.status = "request_sent"
            payload = dict(payload)
            payload.setdefault("print_job_id", job.job_id)
            payload["ui_request_sent_received"] = True
            proxy.request_sent.emit(payload)

        def _finished(payload: dict[str, object]) -> None:
            self.job_finished(job.job_id, payload)

        def _error(message: str, payload: dict[str, object]) -> None:
            self.job_error(job.job_id, message, payload)

        worker.status_changed.connect(proxy.status_changed.emit)
        worker.request_sent.connect(_request_sent)
        worker.finished.connect(_finished)
        worker.error.connect(_error)

    def job_finished(self, job_id: str, payload: dict[str, object]) -> None:
        job = self.running_job
        proxy = self.running_proxy
        if job is None or proxy is None:
            return
        job.status = "finished"
        payload = dict(payload)
        payload.setdefault("print_job_id", job_id)
        log_print_recovery_event(
            "print_job_finished",
            print_job_finished=True,
            print_job_id=job_id,
            print_backend=job.print_backend,
            print_backend_default_source=job.backend_default_source,
            print_queue_size=len(self.queued_jobs),
        )
        proxy.finished.emit(payload)
        self.running_job = None
        self.running_proxy = None
        self.current_worker = None
        self.current_thread = None
        self.start_next_if_idle()

    def job_error(self, job_id: str, message: str, payload: dict[str, object]) -> None:
        job = self.running_job
        proxy = self.running_proxy
        if job is not None:
            job.status = "error"
        log_print_recovery_event(
            "print_job_failed",
            print_job_failed=True,
            print_job_id=job_id,
            print_backend=job.print_backend if job is not None else "",
            print_backend_default_source=job.backend_default_source if job is not None else "",
            print_queue_size=len(self.queued_jobs),
            error_message=str(message),
            **{
                k: v
                for k, v in dict(payload).items()
                if k
                not in {
                    "print_job_id",
                    "print_backend",
                    "print_backend_default_source",
                    "print_queue_size",
                    "error_message",
                }
            },
        )
        log_print_recovery_event(
            "print_queue_worker_error",
            print_queue_worker_error=True,
            print_job_id=job_id,
            print_backend=job.print_backend if job is not None else "",
            print_backend_default_source=job.backend_default_source if job is not None else "",
            print_queue_size=len(self.queued_jobs),
            error_message=str(message),
        )
        if proxy is not None:
            proxy.error.emit(message, dict(payload))
        self.running_job = None
        self.running_proxy = None
        self.current_worker = None
        self.current_thread = None
        self.start_next_if_idle()


_PRINT_QUEUE_MANAGER = PrintQueueManager()


def _print_job_log_fields(job: PrintJob, *, queue_size: int) -> dict[str, object]:
    from app.voucher_settings import load_voucher_printer_settings

    try:
        settings = load_voucher_printer_settings()
    except Exception:
        settings = None
    pdf_path = str(job.merged_pdf_path or "")
    pdf_path_exists = False
    if pdf_path:
        try:
            pdf_path_exists = Path(pdf_path).is_file()
        except OSError:
            pdf_path_exists = False
    printer_name = str(getattr(settings, "printer_name", "") or "").strip() if settings is not None else ""
    print_settings = str(getattr(settings, "sumatra_print_settings", "") or "") if settings is not None else ""
    return {
        "print_job_id": job.job_id,
        "print_backend": job.print_backend,
        "print_backend_default_source": job.backend_default_source,
        "source_type": job.source_type,
        "order_no": job.order_no,
        "pdf_path": pdf_path,
        "pdf_path_exists": pdf_path_exists,
        "pdf_bytes_size": len(job.pdf_bytes or b""),
        "printer_name": printer_name,
        "printer_name_empty": not bool(printer_name),
        "print_settings": print_settings,
        "paperkind": str(getattr(settings, "sumatra_paperkind", "") or "") if settings is not None else "",
        "created_at": job.created_at.isoformat(timespec="seconds"),
        "queue_size": queue_size,
        "print_queue_size": queue_size,
        "ui_thread_id": job.ui_thread_id,
        "current_thread_id": threading.get_ident(),
        "current_thread_is_ui_thread": threading.get_ident() == job.ui_thread_id,
        "test_print_requested": job.test_print_requested,
        "test_print_pdf_path": job.test_print_pdf_path,
    }


def _validate_queued_print_job(job: PrintJob) -> None:
    from app.voucher_settings import (
        PRINT_BACKEND_ACROBAT,
        PRINT_BACKEND_QT,
        PRINT_BACKEND_SUMATRA,
        load_voucher_printer_settings,
    )

    backend = str(job.print_backend or "").strip()
    if backend not in (PRINT_BACKEND_SUMATRA, PRINT_BACKEND_ACROBAT, PRINT_BACKEND_QT):
        log_print_recovery_event(
            "print_backend_unknown",
            print_backend_unknown=True,
            print_job_id=job.job_id,
            print_backend=backend,
            source_type=job.source_type,
        )
        raise RuntimeError(f"Unknown print backend: {backend}")
    if not bytes(job.pdf_bytes or b""):
        raise RuntimeError("印刷用PDFデータが空です。")
    merged_pdf_path = str(job.merged_pdf_path or "").strip()
    if merged_pdf_path and not Path(merged_pdf_path).is_file():
        raise RuntimeError(f"印刷用PDFが見つかりません: {merged_pdf_path}")


def print_pdf_with_dialog(pdf_bytes: bytes, parent: "QWidget | None" = None) -> bool:
    """互換用の入口。現在は印刷ダイアログを表示せず保存済み設定で印刷する。"""
    return print_pdf_direct(pdf_bytes, parent)


def print_pdf_direct(
    pdf_bytes: bytes,
    parent: "QWidget | None" = None,
    *,
    job_name: str = "",
) -> bool:
    """保存済みの伝票印刷設定でPDFを即時印刷する。

    Args:
        pdf_bytes: 印刷するPDFのバイト列。
        parent: 親ウィジェット。

    Returns:
        True: 印刷を実行した。

    Raises:
        RuntimeError: 印刷処理に失敗した場合。
    """
    from app.voucher_settings import (
        PRINT_BACKEND_ACROBAT,
        PRINT_BACKEND_SUMATRA,
        load_voucher_printer_settings,
        print_backend_default_source,
    )

    settings = load_voucher_printer_settings()
    backend_default_source = print_backend_default_source()
    if settings.print_backend in (PRINT_BACKEND_ACROBAT, PRINT_BACKEND_SUMATRA):
        start_print_pdf_background(
            pdf_bytes,
            parent,
            job_name=job_name,
            source_type="direct",
            selected_count=1,
            generated_pdf_count=1,
            merged_pdf_created=False,
        )
    else:
        print_pdf_qt_direct(pdf_bytes, parent)
    return True


def start_print_pdf_background(
    pdf_bytes: bytes,
    parent: "QWidget | None" = None,
    *,
    job_name: str = "",
    source_type: str = "",
    selected_count: int = 1,
    generated_pdf_count: int = 1,
    merged_pdf_created: bool = False,
    merged_pdf_path: str = "",
    test_print_requested: bool = False,
    test_print_pdf_path: str = "",
    settings_override: "VoucherPrinterSettings | None" = None,
):
    """印刷ジョブをキューへ投入し、外部PDFツール起動を直列化する。

    settings_override を渡すと、QSettings へ保存せず、その設定オブジェクトで印刷する
    （テスト印刷が「OK前の画面上の現在値」で印刷しつつ設定を永続化しないために使う）。
    """
    job_id = f"voucher-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    source = source_type or ("selected" if int(selected_count or 1) > 1 else "row")
    job = PrintJob(
        job_id=job_id,
        source_type=source,
        order_no=str(job_name or ""),
        pdf_bytes=bytes(pdf_bytes or b""),
        job_name=job_name,
        selected_count=max(1, int(selected_count or 1)),
        generated_pdf_count=max(1, int(generated_pdf_count or 1)),
        merged_pdf_created=bool(merged_pdf_created),
        merged_pdf_path=merged_pdf_path,
        ui_thread_id=threading.get_ident(),
        test_print_requested=bool(test_print_requested),
        test_print_pdf_path=str(test_print_pdf_path or ""),
        settings_override=settings_override,
    )
    proxy = PrintJobProxy(job)

    from PySide6.QtCore import QTimer
    from app.voucher_settings import load_voucher_printer_settings, print_backend_default_source

    # 一時設定があればそのバックエンドを使う（保存済みは読まない）。
    if settings_override is not None:
        print_backend = str(getattr(settings_override, "print_backend", "") or "")
    else:
        print_backend = load_voucher_printer_settings().print_backend
    backend_default_source = print_backend_default_source()
    # 呼び出し側が signal connect してから queued/running signal が届くよう、次イベントで投入する。
    job.print_backend = print_backend
    job.backend_default_source = backend_default_source
    QTimer.singleShot(0, lambda: _PRINT_QUEUE_MANAGER.enqueue(job, proxy))
    return proxy


def _start_print_pdf_worker(
    pdf_bytes: bytes,
    parent: "QWidget | None" = None,
    *,
    job_name: str = "",
    selected_count: int = 1,
    generated_pdf_count: int = 1,
    merged_pdf_created: bool = False,
    merged_pdf_path: str = "",
    queued_job_id: str = "",
    queued_source_type: str = "",
    ui_thread_id: int = 0,
    queued_print_backend: str = "",
    backend_default_source: str = "",
    settings_override: "VoucherPrinterSettings | None" = None,
):
    """Acrobat Reader / SumatraPDF 経由印刷を UI スレッド外で実行する。

    Qt直接印刷は従来の同期経路を使うため、この関数は外部PDFツール backend 用。
    戻り値は worker。呼び出し側は signal を接続し、参照を保持する。
    """
    from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

    from app.voucher_settings import (
        PRINT_BACKEND_ACROBAT,
        PRINT_BACKEND_QT,
        PRINT_BACKEND_SUMATRA,
        load_voucher_printer_settings,
    )

    # 一時設定（テスト印刷など）があれば保存済みを読まずにそれを使う。
    settings = settings_override if settings_override is not None else load_voucher_printer_settings()
    backend = str(queued_print_backend or settings.print_backend or "").strip()
    if backend == PRINT_BACKEND_SUMATRA:
        status_message = "SumatraPDFへ印刷要求を送信中..."
    elif backend == PRINT_BACKEND_ACROBAT:
        status_message = "Acrobat Readerへ印刷要求を送信中..."
    elif backend == PRINT_BACKEND_QT:
        status_message = "Qt直接印刷を開始しています..."
    else:
        log_print_recovery_event(
            "print_backend_unknown",
            print_backend_unknown=True,
            print_job_id=queued_job_id,
            print_backend=backend,
            source_type=queued_source_type,
        )
        raise RuntimeError(f"Unknown print backend: {backend}")

    class VoucherPrintWorker(QObject):
        status_changed = Signal(str)
        request_sent = Signal(dict)
        finished = Signal(dict)
        error = Signal(str, dict)

        def __init__(self) -> None:
            super().__init__()
            self.cancel_requested = False
            self.thread: QThread | None = None

        @Slot()
        def run(self) -> None:
            metadata: dict[str, object] = {}
            try:
                worker_thread_id = threading.get_ident()
                metadata = {
                    "print_job_id": queued_job_id
                    or f"voucher-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
                    "source_type": queued_source_type,
                    "print_backend": backend,
                    "print_backend_default_source": backend_default_source,
                    "acrobat_selected_as_non_standard_backend": backend == PRINT_BACKEND_ACROBAT,
                    "worker_started": True,
                    "worker_finished": False,
                    "worker_error": False,
                    "worker_thread_id": worker_thread_id,
                    "print_worker_thread_id": worker_thread_id,
                    "ui_thread_id": ui_thread_id,
                    "current_thread_is_ui_thread": worker_thread_id == ui_thread_id,
                    "selected_count": max(1, int(selected_count or 1)),
                    "generated_pdf_count": max(1, int(generated_pdf_count or 1)),
                    "merged_pdf_created": bool(merged_pdf_created),
                    "merged_pdf_path": merged_pdf_path,
                    "test_print_requested": queued_source_type == "test",
                    "test_print_pdf_path": merged_pdf_path if queued_source_type == "test" else "",
                    "print_job_enqueued": True,
                    "ui_thread_blocked": False,
                    "acrobat_launch_count": 0,
                    "acrobat_command_count": 0,
                    "sumatra_launch_count": 0,
                    "sumatra_command_count": 0,
                }
                self.status_changed.emit(status_message)
                log_print_recovery_event(
                    "print_queue_worker_thread_started",
                    print_queue_worker_thread_started=True,
                    print_job_id=metadata["print_job_id"],
                    print_backend=backend,
                    source_type=queued_source_type,
                    worker_thread_id=worker_thread_id,
                    ui_thread_id=ui_thread_id,
                    current_thread_is_ui_thread=worker_thread_id == ui_thread_id,
                )
                log_print_recovery_event(
                    "print_job_backend_dispatch_started",
                    print_job_backend_dispatch_started=True,
                    print_job_id=metadata["print_job_id"],
                    print_backend=backend,
                    source_type=queued_source_type,
                    pdf_path=merged_pdf_path,
                    pdf_path_exists=Path(merged_pdf_path).is_file() if merged_pdf_path else False,
                    printer_name=str(getattr(settings, "printer_name", "") or ""),
                    worker_thread_id=worker_thread_id,
                    ui_thread_id=ui_thread_id,
                    current_thread_is_ui_thread=worker_thread_id == ui_thread_id,
                )
                if not str(getattr(settings, "printer_name", "") or "").strip():
                    raise RuntimeError("プリンターが設定されていません。印刷設定でプリンターを選択してください。")

                def _on_request_sent(payload: dict[str, object]) -> None:
                    metadata.update(payload)
                    self.request_sent.emit(dict(metadata))

                if backend == PRINT_BACKEND_SUMATRA:
                    log_print_recovery_event(
                        "sumatra_backend_selected",
                        sumatra_backend_selected=True,
                        print_job_id=metadata["print_job_id"],
                        print_backend=backend,
                        source_type=queued_source_type,
                    )
                    _print_pdf_with_sumatra(
                        pdf_bytes,
                        settings,
                        job_name=job_name,
                        print_metadata=metadata,
                        request_sent_callback=_on_request_sent,
                    )
                elif backend == PRINT_BACKEND_ACROBAT:
                    log_print_recovery_event(
                        "acrobat_backend_selected",
                        acrobat_backend_selected=True,
                        print_job_id=metadata["print_job_id"],
                        print_backend=backend,
                        source_type=queued_source_type,
                    )
                    _print_pdf_with_acrobat(
                        pdf_bytes,
                        settings,
                        job_name=job_name,
                        print_metadata=metadata,
                        request_sent_callback=_on_request_sent,
                    )
                elif backend == PRINT_BACKEND_QT:
                    log_print_recovery_event(
                        "qt_backend_selected",
                        qt_backend_selected=True,
                        print_job_id=metadata["print_job_id"],
                        print_backend=backend,
                        source_type=queued_source_type,
                    )
                    log_print_recovery_event(
                        "qt_print_started",
                        qt_print_started=True,
                        print_job_id=metadata["print_job_id"],
                        print_backend=backend,
                        source_type=queued_source_type,
                    )
                    print_pdf_qt_direct(pdf_bytes, parent, settings_override=settings)
                else:
                    log_print_recovery_event(
                        "print_backend_unknown",
                        print_backend_unknown=True,
                        print_job_id=metadata["print_job_id"],
                        print_backend=backend,
                        source_type=queued_source_type,
                    )
                    raise RuntimeError(f"Unknown print backend: {backend}")
                metadata["worker_finished"] = True
                self.finished.emit(dict(metadata))
            except Exception as exc:
                if not metadata:
                    metadata = {
                        "print_job_id": queued_job_id
                        or f"voucher-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
                        "source_type": queued_source_type,
                        "print_backend": backend,
                        "print_backend_default_source": backend_default_source,
                        "worker_thread_id": threading.get_ident(),
                        "print_worker_thread_id": threading.get_ident(),
                        "ui_thread_id": ui_thread_id,
                        "current_thread_is_ui_thread": threading.get_ident() == ui_thread_id,
                    }
                metadata["worker_error"] = True
                metadata["worker_finished"] = True
                metadata["exception_type"] = type(exc).__name__
                metadata["exception_message"] = str(exc)
                metadata["traceback"] = traceback.format_exc()
                _LOGGER.warning("バックグラウンド印刷で例外が発生しました。", exc_info=True)
                self.error.emit(str(exc), dict(metadata))

        def cancel(self) -> None:
            self.cancel_requested = True

    thread = QThread()
    worker = VoucherPrintWorker()
    worker.thread = thread
    worker.moveToThread(thread)
    _ACTIVE_PRINT_THREADS.add(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    worker.error.connect(lambda _message, _payload: thread.quit())
    worker.error.connect(lambda _message, _payload: worker.deleteLater())
    thread.finished.connect(lambda t=thread: _ACTIVE_PRINT_THREADS.discard(t))
    thread.finished.connect(thread.deleteLater)
    # 呼び出し側が worker.request_sent などへ connect する前に run() が走ると、
    # Popen 直後に emit される request_sent がロストし「送信中...」のまま固まる。
    # 現在のスタックが解けて呼び出し側の connect が済んだ後に開始する。
    QTimer.singleShot(0, thread.start)
    return worker


def print_pdf_qt_direct(
    pdf_bytes: bytes,
    parent: "QWidget | None" = None,
    *,
    settings_override: "VoucherPrinterSettings | None" = None,
) -> bool:
    """QtのQPrinterでPDFを即時印刷する。印刷方式=qt の互換経路。"""
    printer = create_printer_from_saved_settings(settings_override=settings_override)
    _print_pdf_bytes(pdf_bytes, printer)
    return True


def create_printer_from_saved_settings(
    *, settings_override: "VoucherPrinterSettings | None" = None
):
    """QSettings の保存済み設定（または一時設定）から QPrinter を作成する。"""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

    from app.voucher_settings import (
        DEFAULT_PRINT_COLOR_MODE,
        DEFAULT_PRINT_ORIENTATION,
        DEFAULT_PRINT_PAPER_SIZE,
        PRINT_SCALE_MODE_ACTUAL_SIZE,
        VoucherPrinterSettings,
        load_voucher_printer_settings,
    )

    settings: VoucherPrinterSettings = (
        settings_override if settings_override is not None else load_voucher_printer_settings()
    )
    if not settings.printer_name:
        raise RuntimeError("印刷設定が未設定です。印刷設定画面でプリンターを選択してください。")

    available_names = {info.printerName() for info in QPrinterInfo.availablePrinters()}
    if settings.printer_name not in available_names:
        raise RuntimeError("設定済みプリンターが見つかりません。印刷設定を確認してください。")

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer._voucher_print_settings = settings
    printer.setPrinterName(settings.printer_name)
    paper_size = (settings.paper_size or DEFAULT_PRINT_PAPER_SIZE).upper()
    if paper_size != "B5":
        raise RuntimeError(f"未対応の用紙サイズです: {settings.paper_size}")
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.B5))

    orientation = (settings.orientation or DEFAULT_PRINT_ORIENTATION).lower()
    page_orientation = (
        QPageLayout.Orientation.Portrait
        if orientation == "portrait"
        else QPageLayout.Orientation.Landscape
    )
    printer.setPageOrientation(page_orientation)

    color_mode = (settings.color_mode or DEFAULT_PRINT_COLOR_MODE).lower()
    printer.setColorMode(
        QPrinter.ColorMode.Color if color_mode == "color" else QPrinter.ColorMode.GrayScale
    )
    printer.setCopyCount(max(1, int(settings.copies or 1)))
    printer.setFullPage(True)
    _set_zero_page_margins(printer, QMarginsF, QPageLayout)
    printer._voucher_print_scale_mode = settings.scale_mode or PRINT_SCALE_MODE_ACTUAL_SIZE
    return printer


def list_available_printer_names() -> tuple[list[str], str]:
    """利用可能なプリンター名一覧と既定プリンター名を返す。

    QPrinterInfo.availablePrinters() は Windows でドライバー問い合わせに時間が
    かかることがあるため、必ずバックグラウンドスレッドから呼び出すこと。
    """
    from PySide6.QtPrintSupport import QPrinterInfo

    printers = list(QPrinterInfo.availablePrinters())
    names = [info.printerName() for info in printers if info.printerName()]
    try:
        default_name = QPrinterInfo.defaultPrinter().printerName()
    except Exception:
        default_name = ""
    return names, default_name


def log_print_settings_event(event_type: str, **fields: object) -> None:
    """印刷設定画面の状態遷移を専用 jsonl へ記録する（画面固まり調査用）。"""
    event: dict[str, object] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
    }
    event.update(fields)
    _LOGGER.info("印刷設定画面イベント: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_settings_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("印刷設定画面JSONLログの書き込みに失敗しました。", exc_info=True)


def detect_acrobat_reader_path() -> str:
    """Acrobat Reader/Acrobat の実行ファイルパスを自動検出する。"""
    for path in _acrobat_candidate_paths():
        if path.is_file():
            return str(path)
    registry_path = _detect_acrobat_from_registry()
    if registry_path:
        return registry_path
    return ""


def _acrobat_candidate_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe"),
        Path(r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe"),
        Path(r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe"),
    ]
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_key)
        if not base:
            continue
        candidates.extend(
            [
                Path(base) / "Adobe" / "Acrobat DC" / "Acrobat" / "Acrobat.exe",
                Path(base) / "Adobe" / "Acrobat Reader DC" / "Reader" / "AcroRd32.exe",
            ]
        )
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _detect_acrobat_from_registry() -> str:
    if platform.system().lower() != "windows":
        return ""
    try:
        import winreg
    except Exception:
        return ""
    command_keys = [
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document.DC\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"AcroExch.Document\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r".pdf\OpenWithProgids"),
    ]
    for root, key_name in command_keys:
        try:
            with winreg.OpenKey(root, key_name) as key:
                if key_name.endswith("OpenWithProgids"):
                    continue
                raw = str(winreg.QueryValueEx(key, "")[0])
        except Exception:
            continue
        path = _extract_exe_from_command(raw)
        if path and Path(path).is_file():
            return path
    return ""


def _extract_exe_from_command(command: str) -> str:
    match = re.search(r'"([^"]+\.(?:exe|EXE))"', command or "")
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", command or "", re.IGNORECASE)
    return match.group(1) if match else ""


def detect_sumatra_pdf_path() -> str:
    """Windowsへ独立インストールされたSumatraPDFを自動検出する。"""
    from app.sumatra_detection import find_installed_sumatra_pdf_exe

    installed = find_installed_sumatra_pdf_exe()[0]
    if installed:
        return installed
    # Keep candidate probing as a test seam; production candidates contain only
    # Windows standard install paths and never TksToKintone portable locations.
    for candidate in _sumatra_candidate_paths():
        resolved = _path_if_file(candidate)
        if resolved:
            return resolved
    return ""


def bundled_sumatra_path() -> str:
    """旧API互換。ポータブル版は1.5.13から同梱しない。"""
    return ""


def _saved_path_is_file(path: str) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except OSError:
        return False


def _detect_program_files_sumatra_path() -> str:
    """旧API互換。標準パスを含むインストール済み探索を行う。"""
    from app.sumatra_detection import find_installed_sumatra_pdf_exe

    return find_installed_sumatra_pdf_exe()[0]


def resolve_sumatra_executable(settings) -> tuple[str, str]:
    """SumatraPDF実行ファイルを優先順位で解決し、(path, source) を返す。

    優先順位:
      1. ユーザー設定済みパス
      2. HKCUのInstallLocation / DisplayIcon（64/32bit view）
      3. HKLMのInstallLocation / DisplayIcon（64/32bit view）
      4. LOCALAPPDATA / Program Files標準パス
      5. 見つからない

    TksToKintone配下の旧ポータブル版は探索しない。
    """
    from app.sumatra_detection import find_installed_sumatra_pdf_exe

    saved = str(getattr(settings, "sumatra_path", "") or "").strip()
    installed = find_installed_sumatra_pdf_exe(saved)
    if installed[0]:
        return installed
    detected = detect_sumatra_pdf_path()
    if detected:
        return detected, "installed_detected"
    program = _detect_program_files_sumatra_path()
    if program:
        return program, "program_files"
    return "", "not_found"


def _path_if_file(path: Path) -> str:
    try:
        return str(path) if path.is_file() else ""
    except OSError:
        return ""


def _sumatra_license_notice_present(exe_path: str) -> bool:
    """同梱exeの隣に LICENSE-SumatraPDF.txt があるかを返す（配布ライセンス表記の確認）。"""
    if not exe_path:
        return False
    try:
        return (Path(exe_path).parent / "LICENSE-SumatraPDF.txt").is_file()
    except OSError:
        return False


def _sumatra_version_hint(exe_path: str) -> str:
    """セットアップへ固定同梱する依存バージョン（既存版は上書きしない）。"""
    return "3.6.1" if exe_path else ""


def _sumatra_candidate_paths() -> list[Path]:
    from app.sumatra_detection import standard_sumatra_paths

    return [Path(path) for path in standard_sumatra_paths()]


def _program_files_sumatra_paths() -> list[Path]:
    paths: list[Path] = [
        Path(r"C:\Program Files\SumatraPDF\SumatraPDF.exe"),
        Path(r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe"),
    ]
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_key)
        if base:
            paths.append(Path(base) / "SumatraPDF" / "SumatraPDF.exe")
    return paths


def _bundled_sumatra_paths() -> list[Path]:
    """旧API互換。旧ポータブル版を探索対象へ戻さない。"""
    return []


def _installed_bundled_sumatra_path() -> Path:
    """旧API互換。廃止済みの同梱先は返さない。"""
    return Path()


def _pyinstaller_bundled_sumatra_path() -> Path:
    return Path()


def _dev_bundled_sumatra_path() -> Path:
    return Path()


def _legacy_bundled_sumatra_paths() -> list[Path]:
    """旧API互換。旧ポータブル版は探索対象にしない。"""
    return []


def _resolve_sumatra_print_settings(settings) -> str:
    """設定から -print-settings 文字列を組み立てる。paperkind 指定時は paper=auto を置換する。"""
    from app.voucher_settings import DEFAULT_SUMATRA_PRINT_SETTINGS

    base = str(getattr(settings, "sumatra_print_settings", "") or "").strip() or DEFAULT_SUMATRA_PRINT_SETTINGS
    paperkind = str(getattr(settings, "sumatra_paperkind", "") or "").strip()
    if not paperkind:
        return base
    tokens = [token.strip() for token in base.split(",") if token.strip()]
    filtered = [
        token
        for token in tokens
        if not token.lower().startswith("paper=") and not token.lower().startswith("paperkind=")
    ]
    filtered.append(f"paperkind={paperkind}")
    return ",".join(filtered)


def _build_sumatra_print_command(
    sumatra_path: str, pdf_path: Path, printer_name: str, print_settings: str
) -> list[str]:
    command = [sumatra_path, "-silent", "-print-to", printer_name]
    if print_settings:
        command.extend(["-print-settings", print_settings])
    command.append(str(pdf_path))
    return command


def _build_sumatra_popen_kwargs() -> dict[str, object]:
    """SumatraPDF を余計なウィンドウなしで起動するための Popen 引数。"""
    kwargs: dict[str, object] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False}
    if _is_windows():
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    return kwargs


def _popen_sumatra(command: list[str]):
    return subprocess.Popen(command, close_fds=True, **_build_sumatra_popen_kwargs())


def _create_no_window_used(popen_kwargs: dict[str, object]) -> bool:
    return bool(int(popen_kwargs.get("creationflags", 0) or 0) & CREATE_NO_WINDOW)


def _decode_sumatra_stream(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, (bytes, bytearray)):
        try:
            return bytes(data).decode("utf-8", errors="replace").strip()
        except Exception:
            return str(data)
    return str(data)


def _sumatra_pdf_page_size(pdf_path: Path) -> dict[str, float]:
    """印刷サイズ検証用に PDF 先頭ページのサイズ（pt/mm）を取得する。"""
    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            if doc.page_count > 0:
                rect = doc[0].rect
                return {
                    "width_pt": round(float(rect.width), 3),
                    "height_pt": round(float(rect.height), 3),
                    "width_mm": round(float(rect.width) * 25.4 / 72.0, 3),
                    "height_mm": round(float(rect.height) * 25.4 / 72.0, 3),
                }
    except Exception:
        return {}
    return {}


def _sumatra_paper_setting(print_settings: str) -> str:
    """-print-settings から paper=/paperkind= トークンを抜き出す（ログ用）。"""
    for token in str(print_settings or "").split(","):
        token = token.strip()
        if token.lower().startswith("paper=") or token.lower().startswith("paperkind="):
            return token
    return ""


def _print_pdf_with_sumatra(
    pdf_bytes: bytes,
    settings,
    *,
    job_name: str = "",
    print_metadata: dict[str, object] | None = None,
    request_sent_callback: Callable[[dict[str, object]], None] | None = None,
) -> None:
    started_monotonic = time.monotonic()
    metadata = dict(print_metadata or {})
    metadata.setdefault(
        "print_job_id", f"sumatra-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    metadata.setdefault("worker_started", False)
    metadata.setdefault("worker_finished", False)
    metadata.setdefault("worker_error", False)
    metadata.setdefault("selected_count", 1)
    metadata.setdefault("generated_pdf_count", 1)
    metadata.setdefault("merged_pdf_created", False)
    metadata.setdefault("merged_pdf_path", "")
    metadata.setdefault("ui_thread_blocked", not bool(metadata.get("worker_started")))
    metadata.setdefault("sumatra_launch_count", 0)
    metadata.setdefault("sumatra_command_count", 0)
    metadata.setdefault("popen_started", False)
    metadata.setdefault("request_sent_signal_emitted", False)
    metadata.setdefault("ui_released_after_popen", False)
    metadata.setdefault("communicate_started", False)
    metadata.setdefault("communicate_finished", False)
    metadata.setdefault("sumatra_wait_timeout", False)
    metadata.setdefault("validation_started", False)
    metadata.setdefault("validation_failed", False)
    metadata.setdefault("return_reason", "")
    from app.voucher_settings import normalize_sumatra_wait_timeout_seconds

    wait_timeout_seconds = normalize_sumatra_wait_timeout_seconds(
        getattr(settings, "sumatra_wait_timeout_seconds", None)
    )
    metadata["wait_timeout_seconds"] = wait_timeout_seconds
    cleanup_old_print_jobs()

    # ── Popen前バリデーション ──────────────────────────────────────────────
    # 送信中表示のまま固まらないよう、Popen前エラーはここで即 RuntimeError にし、
    # worker_error / worker_finished を true にしてログする。
    # 重要: 重い QPrinterInfo 列挙（_validate_saved_printer）より前に、
    # SumatraPDFパス等の軽い検証を先に行う。
    metadata["validation_started"] = True
    printer_name_raw = str(getattr(settings, "printer_name", "") or "").strip()
    saved_sumatra_path = str(getattr(settings, "sumatra_path", "") or "").strip()
    # 優先順位: 明示設定 → HKCU → HKLM → LOCALAPPDATA → Program Files。
    log_print_recovery_event(
        "sumatra_resolve_started",
        sumatra_resolve_started=True,
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="sumatra",
        source_type=metadata.get("source_type", ""),
        printer_name=printer_name_raw,
    )
    sumatra_path, sumatra_path_source = resolve_sumatra_executable(settings)
    # Legacy metadata keys remain for log-schema compatibility, but portable
    # executables are no longer resolved or reported as present.
    installed_bundled_path = ""
    installed_bundled_exists = False
    bundled_path = ""
    metadata["sumatra_path_source"] = sumatra_path_source
    metadata["resolved_sumatra_path"] = sumatra_path
    metadata["installed_bundled_sumatra_path"] = installed_bundled_path
    metadata["installed_bundled_sumatra_exists"] = installed_bundled_exists
    metadata["bundled_sumatra_path"] = bundled_path
    metadata["bundled_sumatra_exists"] = bool(bundled_path)
    metadata["sumatra_license_notice_included"] = _sumatra_license_notice_present(
        sumatra_path or bundled_path
    )
    metadata["sumatra_version"] = _sumatra_version_hint(sumatra_path or bundled_path)
    print_settings = _resolve_sumatra_print_settings(settings)
    log_print_recovery_event(
        "sumatra_resolve_finished",
        sumatra_resolve_finished=True,
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="sumatra",
        source_type=metadata.get("source_type", ""),
        sumatra_path=sumatra_path,
        sumatra_path_source=sumatra_path_source,
        sumatra_path_found=bool(sumatra_path),
        sumatra_print_settings=print_settings,
    )

    def _fail_before_popen(
        reason: str,
        message: str,
        *,
        sumatra_path_value: str = "",
        printer_name_value: str = "",
    ) -> None:
        metadata["worker_error"] = True
        metadata["worker_finished"] = True
        metadata["validation_failed"] = True
        metadata["validation_error_message"] = message
        metadata["return_reason"] = reason
        metadata[reason] = True
        metadata["worker_error_signal_emitted"] = True
        metadata["elapsed_worker_total_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        _log_sumatra_print_event(
            settings,
            sumatra_path_value,
            "",
            printer_name_value,
            print_settings=print_settings,
            error_message=message,
            print_metadata=metadata,
        )
        raise RuntimeError(message)

    if not printer_name_raw:
        _fail_before_popen(
            "printer_name_missing",
            "プリンターが設定されていません。印刷設定でプリンターを選択してください。",
        )
    if not sumatra_path:
        # 明示設定もWindowsのインストール先も無い場合は即失敗としてキューを確定する。
        if saved_sumatra_path and not _saved_path_is_file(saved_sumatra_path):
            _fail_before_popen(
                "sumatra_path_not_found",
                SUMATRA_PATH_MISSING_MESSAGE,
                sumatra_path_value=saved_sumatra_path,
                printer_name_value=printer_name_raw,
            )
        _fail_before_popen(
            "sumatra_path_missing", SUMATRA_NOT_FOUND_MESSAGE, printer_name_value=printer_name_raw
        )
    if not print_settings.strip():
        _fail_before_popen(
            "print_settings_missing",
            "SumatraPDFの印刷設定が空です。印刷設定を確認してください。",
            sumatra_path_value=sumatra_path,
            printer_name_value=printer_name_raw,
        )

    # プリンター存在確認（QPrinterInfo）は軽い検証を通過してから実施する。
    printer_name = _validate_saved_printer(printer_name_raw)

    pdf_path = _write_print_job_pdf(pdf_bytes, job_name=job_name)
    if not pdf_path.exists():
        _fail_before_popen(
            "pdf_path_missing",
            "印刷用PDFの作成に失敗しました。",
            sumatra_path_value=sumatra_path,
            printer_name_value=printer_name,
        )
    if not metadata.get("merged_pdf_path"):
        metadata["merged_pdf_path"] = str(pdf_path)
    metadata.setdefault("merged_pdf_created", bool(int(metadata.get("selected_count", 1) or 1) > 1))
    metadata["elapsed_create_pdf_ms"] = int((time.monotonic() - started_monotonic) * 1000)

    # ── 印刷補正PDF（SumatraPDF印刷時のみ）─────────────────────────────────
    # 補正ONなら補正済み一時PDFを作成し、そのパスを SumatraPDF へ渡す。
    # 補正の作成に失敗したら元PDFを印刷せず、Popen前エラーとして扱う。
    log_print_recovery_event(
        "print_adjustment_evaluated",
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="sumatra",
        source_type=metadata.get("source_type", ""),
        **_print_adjustment_metadata_fields(settings),
    )
    try:
        actual_pdf_path = _apply_print_adjustment_for_sumatra(settings, pdf_path, metadata)
    except Exception as adjust_exc:
        _fail_before_popen(
            "print_adjustment_failed",
            "印刷補正PDFの作成に失敗しました。印刷補正設定を確認してください。",
            sumatra_path_value=sumatra_path,
            printer_name_value=printer_name,
        )
        raise adjust_exc  # 到達しない（_fail_before_popen が送出）。

    command = _build_sumatra_print_command(sumatra_path, actual_pdf_path, printer_name, print_settings)
    if metadata.get("source_type") == "test":
        log_print_settings_event(
            "voucher_print_settings_test_print_command_built",
            command_args=command,
            printer_name=printer_name,
        )
    popen_kwargs = _build_sumatra_popen_kwargs()
    metadata["powershell_usage_detected"] = False
    metadata["subprocess_shell_true_detected"] = bool(popen_kwargs.get("shell") is True)
    metadata["create_no_window_used"] = _create_no_window_used(popen_kwargs)
    metadata["console_window_suppressed"] = bool(metadata["create_no_window_used"]) or not _is_windows()
    pdf_page_size = _sumatra_pdf_page_size(actual_pdf_path)

    process_id = None
    process_started = False
    exit_code: int | None = None
    stdout_text = ""
    stderr_text = ""
    error_message = ""
    sumatra_wait_timeout = False
    wait_start_monotonic = started_monotonic

    # ── Popen（失敗時のみ即エラー。ここまでで request_sent は出していない）──
    try:
        log_print_recovery_event(
            "sumatra_popen_started",
            sumatra_popen_started=True,
            print_job_id=metadata.get("print_job_id", ""),
            print_backend="sumatra",
            source_type=metadata.get("source_type", ""),
            sumatra_path=sumatra_path,
            pdf_path=str(actual_pdf_path),
            sumatra_pdf_path_actual=str(actual_pdf_path),
            printer_name=printer_name,
            command_args=command,
            create_no_window_used=bool(metadata.get("create_no_window_used", False)),
            subprocess_shell_true_detected=bool(metadata.get("subprocess_shell_true_detected", False)),
        )
        process = _popen_sumatra(command)
    except Exception as exc:
        metadata["worker_error"] = True
        metadata["elapsed_worker_total_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        _log_sumatra_print_event(
            settings,
            sumatra_path,
            str(pdf_path),
            printer_name,
            command=command,
            print_settings=print_settings,
            process_id=None,
            process_started=False,
            exit_code=None,
            error_message=str(exc),
            print_metadata=metadata,
            pdf_page_size=pdf_page_size,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            traceback_text=traceback.format_exc(),
        )
        raise RuntimeError("SumatraPDF経由印刷に失敗しました。印刷設定を確認してください。") from exc

    # ── Popen 成功：ただちに request_sent を出して UI を復帰させる ──
    process_id = process.pid
    process_started = True
    metadata["popen_started"] = True
    request_sent_time = datetime.now().isoformat(timespec="seconds")
    metadata["request_sent_time"] = request_sent_time
    metadata["elapsed_popen_ms"] = int((time.monotonic() - started_monotonic) * 1000)
    metadata["elapsed_send_request_ms"] = metadata["elapsed_popen_ms"]
    metadata["elapsed_request_sent_ms"] = metadata["elapsed_popen_ms"]
    metadata["sumatra_launch_count"] = 1
    metadata["sumatra_command_count"] = 1
    metadata["request_sent_signal_emitted"] = True
    metadata["ui_released_after_popen"] = True
    metadata["process_id"] = process_id
    if request_sent_callback is not None:
        request_sent_callback(
            {
                "request_sent_time": request_sent_time,
                "elapsed_send_request_ms": metadata["elapsed_send_request_ms"],
                "elapsed_popen_ms": metadata["elapsed_popen_ms"],
                "sumatra_launch_count": 1,
                "sumatra_command_count": 1,
                "popen_pid": process_id,
                "request_sent_signal_emitted": True,
                "ui_released_after_popen": True,
            }
        )
    log_print_recovery_event(
        "sumatra_request_sent_signal_emitted",
        sumatra_request_sent_signal_emitted=True,
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="sumatra",
        source_type=metadata.get("source_type", ""),
        process_id=process_id,
        request_sent_signal_emitted=True,
    )

    # ── 終了コード確認は必ず timeout 付きで（UIは既に復帰済み）──
    metadata["communicate_started"] = True
    wait_start_monotonic = time.monotonic()
    try:
        stdout_data, stderr_data = process.communicate(timeout=wait_timeout_seconds)
        exit_code = process.returncode
        stdout_text = _decode_sumatra_stream(stdout_data)
        stderr_text = _decode_sumatra_stream(stderr_data)
        metadata["communicate_finished"] = True
    except subprocess.TimeoutExpired:
        sumatra_wait_timeout = True
        metadata["sumatra_wait_timeout"] = True
        # 既定では強制終了しない。ONの場合のみ、この Popen pid だけ terminate する。
        # プロセス名指定での全プロセス一括終了は決して行わない。
        if bool(getattr(settings, "sumatra_allow_force_kill", False)):
            try:
                process.terminate()
                metadata["force_kill_sent"] = True
            except Exception:
                _LOGGER.warning("SumatraPDFプロセスのterminateに失敗しました: pid=%s", process_id, exc_info=True)
        error_message = "印刷要求は送信済みですが、SumatraPDFの終了確認がタイムアウトしました。"

    metadata["elapsed_wait_ms"] = int((time.monotonic() - wait_start_monotonic) * 1000)
    metadata["elapsed_worker_total_ms"] = int((time.monotonic() - started_monotonic) * 1000)

    if not sumatra_wait_timeout and exit_code != 0:
        detail = _sumatra_exit_code_message(exit_code)
        error_message = f"SumatraPDFの印刷に失敗しました（終了コード {exit_code}: {detail}）。"
        metadata["worker_error"] = True

    if metadata.get("worker_started"):
        # request_sent 済み・タイムアウトも UI を固めないため finished 扱いにする。
        metadata["worker_finished"] = True

    _log_sumatra_print_event(
        settings,
        sumatra_path,
        str(pdf_path),
        printer_name,
        command=command,
        print_settings=print_settings,
        process_id=process_id,
        process_started=process_started,
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        error_message=error_message,
        print_metadata=metadata,
        pdf_page_size=pdf_page_size,
    )
    # タイムアウトは UI を復帰済みのため例外にしない（送信済み扱い）。
    # 終了コード異常のみ例外化してステータス/通知に反映する。
    if not sumatra_wait_timeout and exit_code != 0:
        raise RuntimeError(error_message)


def log_voucher_print_event(event_type: str, **fields: object) -> None:
    """伝票のPDF作成・印刷まわりの付随イベントを voucher_print jsonl へ記録する。

    印刷時PDF同時作成・選択PDF作成（受注No別）の進捗/結果を残すための汎用ログ。
    書き込み失敗は握りつぶし、印刷やPDF作成の本処理を止めない。
    """
    event: dict[str, object] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
    }
    event.update(fields)
    _LOGGER.info("伝票印刷イベント: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("伝票印刷JSONLログの書き込みに失敗しました。", exc_info=True)


def log_print_recovery_event(event_type: str, **fields: object) -> None:
    """印刷UIの復帰（ガード解除・ボタン再有効化・エラー受信）を jsonl へ記録する。"""
    event: dict[str, object] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
    }
    event.update(fields)
    _LOGGER.info("印刷UI復帰イベント: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("印刷UI復帰JSONLログの書き込みに失敗しました。", exc_info=True)


def _log_sumatra_print_event(
    settings,
    sumatra_path: str,
    pdf_path: str,
    printer_name: str,
    *,
    command: list[str] | None = None,
    print_settings: str = "",
    process_id: int | None = None,
    process_started: bool = False,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    error_message: str = "",
    print_metadata: dict[str, object] | None = None,
    pdf_page_size: dict[str, float] | None = None,
    exception_type: str = "",
    exception_message: str = "",
    traceback_text: str = "",
) -> None:
    meta = print_metadata or {}
    page = pdf_page_size or {}
    try:
        from app.voucher_settings import parse_sumatra_print_settings

        parsed_settings = parse_sumatra_print_settings(print_settings)
    except Exception:
        parsed_settings = {}
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "print_job_id": meta.get("print_job_id", ""),
        "print_backend": "sumatra",
        "print_backend_default_source": meta.get("print_backend_default_source", ""),
        "acrobat_selected_as_non_standard_backend": bool(
            meta.get("acrobat_selected_as_non_standard_backend", False)
        ),
        "powershell_usage_detected": bool(meta.get("powershell_usage_detected", False)),
        "subprocess_shell_true_detected": bool(meta.get("subprocess_shell_true_detected", False)),
        "console_window_suppressed": bool(meta.get("console_window_suppressed", False)),
        "create_no_window_used": bool(meta.get("create_no_window_used", False)),
        "worker_started": bool(meta.get("worker_started", False)),
        "worker_finished": bool(meta.get("worker_finished", False)),
        "worker_error": bool(meta.get("worker_error", False)),
        "print_worker_thread_id": meta.get("print_worker_thread_id", ""),
        "ui_thread_id": meta.get("ui_thread_id", ""),
        "worker_error_signal_emitted": bool(meta.get("worker_error_signal_emitted", False)),
        "worker_finished_signal_emitted": bool(meta.get("worker_finished_signal_emitted", False)),
        "validation_started": bool(meta.get("validation_started", False)),
        "validation_failed": bool(meta.get("validation_failed", False)),
        "validation_error_message": meta.get("validation_error_message", ""),
        "return_reason": meta.get("return_reason", ""),
        "printer_name_missing": bool(meta.get("printer_name_missing", False)),
        "sumatra_path_missing": bool(meta.get("sumatra_path_missing", False)),
        "sumatra_path_not_found": bool(meta.get("sumatra_path_not_found", False)),
        "print_settings_missing": bool(meta.get("print_settings_missing", False)),
        "pdf_path_missing": bool(meta.get("pdf_path_missing", False)),
        "selected_count": int(meta.get("selected_count", 1) or 1),
        "generated_pdf_count": int(meta.get("generated_pdf_count", 1) or 1),
        "merged_pdf_created": bool(meta.get("merged_pdf_created", False)),
        "merged_pdf_path": meta.get("merged_pdf_path", ""),
        "sumatra_launch_count": int(meta.get("sumatra_launch_count", 1 if process_started else 0) or 0),
        "sumatra_command_count": int(meta.get("sumatra_command_count", 1 if command else 0) or 0),
        "ui_thread_blocked": bool(meta.get("ui_thread_blocked", False)),
        "popen_started": bool(meta.get("popen_started", process_started)),
        "request_sent_signal_emitted": bool(meta.get("request_sent_signal_emitted", False)),
        "ui_released_after_popen": bool(meta.get("ui_released_after_popen", False)),
        "wait_timeout_seconds": meta.get("wait_timeout_seconds"),
        "communicate_started": bool(meta.get("communicate_started", False)),
        "communicate_finished": bool(meta.get("communicate_finished", False)),
        "sumatra_wait_timeout": bool(meta.get("sumatra_wait_timeout", False)),
        "force_kill_sent": bool(meta.get("force_kill_sent", False)),
        "request_sent_time": meta.get("request_sent_time", ""),
        "elapsed_create_pdf_ms": meta.get("elapsed_create_pdf_ms"),
        "elapsed_popen_ms": meta.get("elapsed_popen_ms"),
        "elapsed_send_request_ms": meta.get("elapsed_send_request_ms"),
        "elapsed_request_sent_ms": meta.get("elapsed_request_sent_ms"),
        "elapsed_wait_ms": meta.get("elapsed_wait_ms"),
        "elapsed_worker_total_ms": meta.get("elapsed_worker_total_ms"),
        "elapsed_ms": meta.get("elapsed_worker_total_ms"),
        "sumatra_path": sumatra_path,
        "sumatra_path_source": meta.get("sumatra_path_source", ""),
        "resolved_sumatra_path": meta.get("resolved_sumatra_path", sumatra_path),
        "installed_bundled_sumatra_path": meta.get("installed_bundled_sumatra_path", ""),
        "installed_bundled_sumatra_exists": bool(meta.get("installed_bundled_sumatra_exists", False)),
        "bundled_sumatra_path": meta.get("bundled_sumatra_path", ""),
        "bundled_sumatra_exists": bool(meta.get("bundled_sumatra_exists", False)),
        "sumatra_version": meta.get("sumatra_version", ""),
        "sumatra_license_notice_included": bool(meta.get("sumatra_license_notice_included", False)),
        "pdf_path": pdf_path,
        "printer_name": printer_name,
        "print_settings": print_settings,
        "sumatra_profile_name": str(getattr(settings, "sumatra_profile_name", "") or ""),
        "sumatra_print_settings": print_settings,
        "sumatra_scaling_mode": parsed_settings.get("scaling_mode", ""),
        "sumatra_paper_setting": _sumatra_paper_setting(print_settings),
        "sumatra_paperkind": str(
            parsed_settings.get("paperkind", "") or getattr(settings, "sumatra_paperkind", "") or ""
        ),
        "sumatra_monochrome": bool(parsed_settings.get("monochrome", False)),
        "sumatra_center": bool(parsed_settings.get("center", False)),
        "sumatra_auto_rotation": bool(parsed_settings.get("auto_rotation", True)),
        "sumatra_bin": str(parsed_settings.get("bin", "") or ""),
        "sumatra_extra_options": str(parsed_settings.get("extra_options", "") or ""),
        "sumatra_command_args": command or [],
        "test_print_requested": bool(meta.get("test_print_requested", False)),
        "test_print_pdf_path": meta.get("test_print_pdf_path", ""),
        "print_adjustment_enabled": bool(meta.get("print_adjustment_enabled", False)),
        "print_adjustment_profile_name": meta.get("print_adjustment_profile_name", ""),
        "print_adjustment_source_pdf": meta.get("print_adjustment_source_pdf", ""),
        "print_adjustment_output_pdf": meta.get("print_adjustment_output_pdf", ""),
        "print_adjustment_output_pdf_exists": bool(meta.get("print_adjustment_output_pdf_exists", False)),
        "print_adjustment_margin_left_mm": meta.get("print_adjustment_margin_left_mm", 0.0),
        "print_adjustment_margin_right_mm": meta.get("print_adjustment_margin_right_mm", 0.0),
        "print_adjustment_margin_top_mm": meta.get("print_adjustment_margin_top_mm", 0.0),
        "print_adjustment_margin_bottom_mm": meta.get("print_adjustment_margin_bottom_mm", 0.0),
        "print_adjustment_scale_x_percent": meta.get("print_adjustment_scale_x_percent", 100.0),
        "print_adjustment_scale_y_percent": meta.get("print_adjustment_scale_y_percent", 100.0),
        "print_adjustment_offset_x_mm": meta.get("print_adjustment_offset_x_mm", 0.0),
        "print_adjustment_offset_y_mm": meta.get("print_adjustment_offset_y_mm", 0.0),
        "print_adjustment_pdf_created": bool(meta.get("print_adjustment_pdf_created", False)),
        "sumatra_pdf_path_actual": meta.get("sumatra_pdf_path_actual", pdf_path),
        "print_job_enqueued": bool(meta.get("print_job_enqueued", False)),
        "print_job_started": bool(meta.get("worker_started", False)),
        "print_job_finished": bool(meta.get("worker_finished", False)),
        "paper_setting": _sumatra_paper_setting(print_settings),
        "paperkind": str(getattr(settings, "sumatra_paperkind", "") or ""),
        "pdf_page_size": page,
        "pdf_page_width_pt": page.get("width_pt"),
        "pdf_page_height_pt": page.get("height_pt"),
        "pdf_page_width_mm": page.get("width_mm"),
        "pdf_page_height_mm": page.get("height_mm"),
        "paper_size_setting": getattr(settings, "paper_size", "B5"),
        "orientation_setting": getattr(settings, "orientation", "landscape"),
        "color_mode_setting": getattr(settings, "color_mode", "grayscale"),
        "copies": getattr(settings, "copies", 1),
        "command": command or [],
        "command_args": command or [],
        "sumatra_args_include_silent": "-silent" in (command or []),
        "sumatra_args_include_print_to": "-print-to" in (command or []),
        "sumatra_args_include_print_settings": "-print-settings" in (command or []),
        "process_id": process_id,
        "process_started": process_started,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "error_message": error_message,
        "exception_type": exception_type,
        "exception_message": exception_message,
        "traceback": traceback_text,
    }
    _LOGGER.info("SumatraPDF経由印刷: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("SumatraPDF印刷JSONLログの書き込みに失敗しました。", exc_info=True)


def _print_pdf_with_acrobat(
    pdf_bytes: bytes,
    settings,
    *,
    job_name: str = "",
    print_metadata: dict[str, object] | None = None,
    request_sent_callback: Callable[[dict[str, object]], None] | None = None,
) -> None:
    started_monotonic = time.monotonic()
    metadata = dict(print_metadata or {})
    metadata.setdefault("print_job_id", f"sync-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}")
    metadata.setdefault("worker_started", False)
    metadata.setdefault("worker_finished", False)
    metadata.setdefault("worker_error", False)
    metadata.setdefault("selected_count", 1)
    metadata.setdefault("generated_pdf_count", 1)
    metadata.setdefault("merged_pdf_created", False)
    metadata.setdefault("merged_pdf_path", "")
    metadata.setdefault("ui_thread_blocked", not bool(metadata.get("worker_started")))
    metadata.setdefault("acrobat_launch_count", 0)
    metadata.setdefault("acrobat_command_count", 0)
    metadata.setdefault("acrobat_popen_started", False)
    metadata.setdefault("acrobat_request_sent_signal_emitted", False)
    metadata.setdefault("request_sent_signal_emitted", False)
    metadata.setdefault("ui_released_after_popen", False)
    cleanup_old_print_jobs()
    printer_name = _validate_saved_printer(settings.printer_name)
    log_print_recovery_event(
        "acrobat_resolve_started",
        acrobat_resolve_started=True,
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="acrobat",
        source_type=metadata.get("source_type", ""),
        printer_name=printer_name,
    )
    existing_acrobat_pids = _list_acrobat_process_ids()
    initial_hide_watch_info = {
        "existing_acrobat_pids_before": sorted(existing_acrobat_pids),
        "hide_watch_existing_pids": sorted(existing_acrobat_pids),
    }
    acrobat_path = (settings.acrobat_path or "").strip() or detect_acrobat_reader_path()
    log_print_recovery_event(
        "acrobat_resolve_finished",
        acrobat_resolve_finished=True,
        print_job_id=metadata.get("print_job_id", ""),
        print_backend="acrobat",
        source_type=metadata.get("source_type", ""),
        acrobat_path=acrobat_path,
        acrobat_path_found=bool(acrobat_path and Path(acrobat_path).is_file()),
        acrobat_existing_pids=sorted(existing_acrobat_pids),
    )
    if not acrobat_path:
        _log_acrobat_print_event(
            settings,
            "",
            "",
            printer_name,
            error_message=ACROBAT_NOT_FOUND_MESSAGE,
            hide_watch_info=initial_hide_watch_info,
            print_metadata=metadata,
        )
        raise RuntimeError(ACROBAT_NOT_FOUND_MESSAGE)
    if not Path(acrobat_path).is_file():
        message = "Acrobat Readerのパスが存在しません。印刷設定を確認してください。"
        _log_acrobat_print_event(
            settings,
            acrobat_path,
            "",
            printer_name,
            error_message=message,
            hide_watch_info=initial_hide_watch_info,
            print_metadata=metadata,
        )
        raise RuntimeError(message)

    pdf_path = _write_print_job_pdf(pdf_bytes, job_name=job_name)
    if not metadata.get("merged_pdf_path"):
        metadata["merged_pdf_path"] = str(pdf_path)
    metadata.setdefault("merged_pdf_created", bool(int(metadata.get("selected_count", 1) or 1) > 1))
    metadata["elapsed_create_pdf_ms"] = int((time.monotonic() - started_monotonic) * 1000)
    _log_driver_setting_notice(settings, printer_name)
    command = _build_acrobat_print_command(acrobat_path, pdf_path, printer_name, settings)
    acrobat_popen_kwargs = _build_acrobat_popen_kwargs(settings)
    metadata["powershell_usage_detected"] = False
    metadata["subprocess_shell_true_detected"] = bool(acrobat_popen_kwargs.get("shell") is True)
    metadata["create_no_window_used"] = _create_no_window_used(acrobat_popen_kwargs)
    metadata["console_window_suppressed"] = bool(metadata["create_no_window_used"]) or not _is_windows()
    metadata["acrobat_selected_as_non_standard_backend"] = True
    process_id = None
    process_started = False
    process_exited_quickly = False
    process_exit_code = None
    error_message = ""
    fallback_used = False
    exception_type = ""
    exception_message = ""
    traceback_text = ""
    close_info: dict[str, object] = {}
    hide_watch_info: dict[str, object] = dict(initial_hide_watch_info)
    hide_enabled = bool(getattr(settings, "acrobat_hide_watch_enabled", True)) and bool(
        getattr(settings, "acrobat_hide_window", True)
    )
    # Acrobatのホーム画面が一瞬でも前面に出るのを防ぐため、Popen前に監視を開始する。
    watch_handle = _start_hide_watch_before_popen(
        existing_acrobat_pids,
        duration_seconds=getattr(settings, "acrobat_hide_watch_seconds", 5),
        enabled=hide_enabled,
    )
    metadata["acrobat_hide_watch_started_before_popen"] = True
    try:
        log_print_recovery_event(
            "acrobat_popen_started",
            acrobat_popen_started=True,
            print_job_id=metadata.get("print_job_id", ""),
            print_backend="acrobat",
            source_type=metadata.get("source_type", ""),
            acrobat_path=acrobat_path,
            pdf_path=str(pdf_path),
            printer_name=printer_name,
            command_args=command,
            create_no_window_used=bool(metadata.get("create_no_window_used", False)),
            subprocess_shell_true_detected=bool(metadata.get("subprocess_shell_true_detected", False)),
        )
        process = _popen_acrobat(command, settings)
        process_id = process.pid
        process_started = True
        metadata["acrobat_popen_started"] = True
        metadata["popen_started"] = True
        metadata["process_id"] = process_id
        metadata["elapsed_popen_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        watch_handle.add_target_pid(process_id)
        request_sent_time = datetime.now().isoformat(timespec="seconds")
        metadata["request_sent_time"] = request_sent_time
        metadata["elapsed_send_request_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        metadata["elapsed_request_sent_ms"] = metadata["elapsed_send_request_ms"]
        metadata["acrobat_launch_count"] = 1
        metadata["acrobat_command_count"] = 1
        metadata["acrobat_request_sent_signal_emitted"] = True
        metadata["request_sent_signal_emitted"] = True
        metadata["ui_released_after_popen"] = True
        if request_sent_callback is not None:
            request_sent_callback(
                {
                    "request_sent_time": request_sent_time,
                    "elapsed_send_request_ms": metadata["elapsed_send_request_ms"],
                    "elapsed_popen_ms": metadata["elapsed_popen_ms"],
                    "elapsed_request_sent_ms": metadata["elapsed_request_sent_ms"],
                    "acrobat_launch_count": 1,
                    "acrobat_command_count": 1,
                    "popen_pid": process_id,
                    "acrobat_process_id": process_id,
                    "acrobat_request_sent_signal_emitted": True,
                    "request_sent_signal_emitted": True,
                    "ui_released_after_popen": True,
                }
            )
        process_exit_code = process.poll()
        process_exited_quickly = process_exit_code is not None
        hide_watch_info = watch_handle.join()
        close_info = _close_print_acrobat_processes(
            process,
            settings,
            existing_acrobat_pids,
            set(hide_watch_info.get("target_acrobat_pids", [])),
        )
    except Exception as exc:
        try:
            hide_watch_info = watch_handle.join()
        except Exception:
            _LOGGER.warning("Acrobat Reader非表示監視スレッドの終了待ちに失敗しました。", exc_info=True)
        exception_type = type(exc).__name__
        exception_message = str(exc)
        traceback_text = traceback.format_exc()
        metadata["worker_error"] = True
        metadata["elapsed_worker_total_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        if platform.system().lower() == "windows":
            try:
                process_id = _shell_execute_printto(pdf_path, printer_name)
                process_started = True
                fallback_used = True
            except Exception as shell_exc:
                error_message = f"{exc}; ShellExecute printto failed: {shell_exc}"
                exception_type = type(shell_exc).__name__
                exception_message = str(shell_exc)
                traceback_text = traceback.format_exc()
                _log_acrobat_print_event(
                    settings,
                    acrobat_path,
                    str(pdf_path),
                    printer_name,
                    command=command,
                    process_id=process_id,
                    process_started=process_started,
                    process_exited_quickly=process_exited_quickly,
                    process_exit_code=process_exit_code,
                    error_message=error_message,
                    fallback_used=fallback_used,
                    close_info=close_info,
                    hide_watch_info=hide_watch_info,
                    print_metadata=metadata,
                    exception_type=exception_type,
                    exception_message=exception_message,
                    traceback_text=traceback_text,
                )
                raise RuntimeError("Acrobat Reader経由印刷に失敗しました。印刷設定を確認してください。") from shell_exc
        else:
            error_message = str(exc)
            _log_acrobat_print_event(
                settings,
                acrobat_path,
                str(pdf_path),
                printer_name,
                command=command,
                process_id=process_id,
                process_started=process_started,
                process_exited_quickly=process_exited_quickly,
                process_exit_code=process_exit_code,
                error_message=error_message,
                fallback_used=fallback_used,
                close_info=close_info,
                hide_watch_info=hide_watch_info,
                print_metadata=metadata,
                exception_type=exception_type,
                exception_message=exception_message,
                traceback_text=traceback_text,
            )
            raise RuntimeError("Acrobat Reader経由印刷に失敗しました。印刷設定を確認してください。") from exc

    metadata["elapsed_worker_total_ms"] = int((time.monotonic() - started_monotonic) * 1000)
    if metadata.get("worker_started"):
        metadata["worker_finished"] = True
    _log_acrobat_print_event(
        settings,
        acrobat_path,
        str(pdf_path),
        printer_name,
        command=command,
        process_id=process_id,
        process_started=process_started,
        process_exited_quickly=process_exited_quickly,
        process_exit_code=process_exit_code,
        error_message=error_message,
        fallback_used=fallback_used,
        close_info=close_info,
        hide_watch_info=hide_watch_info,
        print_metadata=metadata,
        exception_type=exception_type,
        exception_message=exception_message,
        traceback_text=traceback_text,
    )


def _build_acrobat_print_command(acrobat_path: str, pdf_path: Path, printer_name: str, settings) -> list[str]:
    command = [acrobat_path]
    # /n 新規インスタンス, /s スプラッシュ抑制, /o 起動時ダイアログ抑制。
    command.extend(["/n", "/s", "/o"])
    if getattr(settings, "acrobat_hide_window", True):
        # /h は最小化/非表示での起動要求。
        command.append("/h")
    command.extend(["/t", str(pdf_path), printer_name])
    return command


def _popen_acrobat(command: list[str], settings):
    kwargs = _build_acrobat_popen_kwargs(settings)
    return subprocess.Popen(command, close_fds=True, **kwargs)


def _build_acrobat_popen_kwargs(settings) -> dict[str, object]:
    kwargs: dict[str, object] = {"shell": False}
    hide_window = bool(getattr(settings, "acrobat_hide_window", True))
    creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    if not hide_window:
        if _is_windows():
            kwargs["creationflags"] = creationflags
        return kwargs
    startupinfo = None
    startup_show_window = SW_HIDE
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startup_show_window = getattr(subprocess, "SW_HIDE", SW_HIDE)
        startupinfo.wShowWindow = startup_show_window
    kwargs["startupinfo"] = startupinfo
    kwargs["creationflags"] = creationflags
    return kwargs


def _startup_show_window_setting(settings) -> int | None:
    return SW_HIDE if bool(getattr(settings, "acrobat_hide_window", True)) else None


def _list_acrobat_process_ids() -> set[int]:
    if not _is_windows():
        return set()
    return {
        int(proc["pid"])
        for proc in _snapshot_processes()
        if str(proc.get("name", "")).lower() in ACROBAT_PROCESS_NAMES
    }


def _child_process_ids(parent_pid: int | None) -> set[int]:
    if not parent_pid or not _is_windows():
        return set()
    parent = int(parent_pid)
    return {
        int(proc["pid"])
        for proc in _snapshot_processes()
        if int(proc.get("parent_pid", 0) or 0) == parent
    }


def _snapshot_processes() -> list[dict[str, object]]:
    if not _is_windows():
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    processes: list[dict[str, object]] = []
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            processes.append(
                {
                    "pid": int(entry.th32ProcessID),
                    "parent_pid": int(entry.th32ParentProcessID),
                    "name": str(entry.szExeFile or ""),
                }
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def _close_print_acrobat_process(process, settings, existing_acrobat_pids: set[int]) -> dict[str, object]:
    return _close_print_acrobat_processes(
        process,
        settings,
        existing_acrobat_pids,
        {int(getattr(process, "pid", 0) or 0)},
    )


def _close_print_acrobat_processes(
    process,
    settings,
    existing_acrobat_pids: set[int],
    target_acrobat_pids: set[int],
) -> dict[str, object]:
    close_target_pids = {int(pid) for pid in target_acrobat_pids if int(pid or 0)}
    popen_pid = int(getattr(process, "pid", 0) or 0)
    popen_exited = _process_exited(process) if popen_pid else True
    if popen_pid and not popen_exited:
        close_target_pids.add(popen_pid)
    elif popen_pid and popen_exited:
        close_target_pids.discard(popen_pid)
    close_skipped_existing_pids = sorted(close_target_pids & set(existing_acrobat_pids))
    close_target_pids -= set(existing_acrobat_pids)
    info: dict[str, object] = {
        "close_after_print": bool(getattr(settings, "acrobat_close_after_print", True)),
        "close_delay_seconds": int(getattr(settings, "acrobat_close_delay_seconds", 10) or 10),
        "close_target_process_id": popen_pid or None,
        "close_target_pids": sorted(close_target_pids),
        "close_skipped_existing_pids": close_skipped_existing_pids,
        "close_sent_pids": [],
        "close_result_by_pid": {},
        "close_window_handles": [],
        "close_started": False,
        "close_delay_started": False,
        "close_attempted": False,
        "close_finished": False,
        "close_sent": False,
        "close_result": "not_requested",
        "close_skipped_reason": "",
    }
    if not info["close_after_print"]:
        info["close_result"] = "skipped"
        info["close_skipped_reason"] = "close_after_print_disabled"
        return info

    if not close_target_pids:
        info["close_result"] = "skipped"
        if close_skipped_existing_pids:
            info["close_skipped_reason"] = "existing_acrobat_process"
        elif popen_pid and popen_exited:
            info["close_skipped_reason"] = "process_already_exited"
        else:
            info["close_skipped_reason"] = "process_not_found"
        return info
    if close_target_pids == {popen_pid} and popen_exited:
        info["close_result"] = "skipped"
        info["close_skipped_reason"] = "process_already_exited"
        return info
    if not _is_windows():
        info["close_result"] = "skipped"
        info["close_skipped_reason"] = "windows_api_unavailable"
        return info

    info["close_started"] = True
    delay = min(60, max(5, int(info["close_delay_seconds"])))
    try:
        info["close_delay_started"] = True
        _wait_before_acrobat_close(delay)
        if close_target_pids == {popen_pid} and _process_exited(process):
            info["close_result"] = "skipped"
            info["close_skipped_reason"] = "process_already_exited"
            return info

        handles_by_pid: dict[int, list[int]] = {
            pid: _window_handles_for_process(pid) for pid in sorted(close_target_pids)
        }
        handles = [hwnd for pid_handles in handles_by_pid.values() for hwnd in pid_handles]
        info["close_window_handles"] = handles
        info["close_window_handles_by_pid"] = handles_by_pid
        info["close_attempted"] = True
        if not handles:
            info["close_result"] = "skipped"
            info["close_skipped_reason"] = "no_window_found"
            return info

        sent_by_pid: dict[int, bool] = {}
        for pid, pid_handles in handles_by_pid.items():
            sent_by_pid[pid] = _send_wm_close_to_windows(pid_handles) if pid_handles else False
        sent = any(sent_by_pid.values())
        info["close_result_by_pid"] = sent_by_pid
        info["close_sent_pids"] = sorted(pid for pid, pid_sent in sent_by_pid.items() if pid_sent)
        info["close_sent"] = sent
        if sent:
            info["close_result"] = "wm_close_sent"
        else:
            info["close_result"] = "skipped"
            info["close_skipped_reason"] = "wm_close_failed"
            return info

        time.sleep(2)
        still_running_popen = popen_pid in close_target_pids and not _process_exited(process)
        if still_running_popen or any(pid != popen_pid for pid in close_target_pids):
            if bool(getattr(settings, "acrobat_allow_force_kill", False)):
                terminated: list[int] = []
                terminate_errors: dict[int, str] = {}
                for pid in sorted(close_target_pids):
                    try:
                        if pid == popen_pid:
                            process.terminate()
                        else:
                            _terminate_process_by_pid(pid)
                        terminated.append(pid)
                    except Exception as exc:
                        terminate_errors[pid] = str(exc)
                info["force_kill_sent_pids"] = terminated
                info["force_kill_errors"] = terminate_errors
                info["close_result"] = "terminate_sent" if terminated else "warning"
                if terminate_errors:
                    info["close_skipped_reason"] = f"terminate_failed: {terminate_errors}"
            else:
                info["close_result"] = "warning"
                info["close_skipped_reason"] = "force_kill_disabled"
                _LOGGER.warning("印刷用Acrobat Readerを閉じきれませんでした: pids=%s", sorted(close_target_pids))
    finally:
        info["close_finished"] = True
    return info


def _wait_before_acrobat_close(seconds: int) -> None:
    time.sleep(seconds)


def _process_exited(process) -> bool:
    try:
        return process.poll() is not None
    except Exception:
        return True


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


HIDE_WATCH_FAST_INTERVAL_SECONDS = 0.03
HIDE_WATCH_SLOW_INTERVAL_SECONDS = 0.15
HIDE_WATCH_FAST_PHASE_SECONDS = 3.0


def _hide_watch_interval_seconds(elapsed_seconds: float) -> float:
    """起動直後は高頻度、その後は通常頻度でウィンドウを監視する。"""
    if float(elapsed_seconds) < HIDE_WATCH_FAST_PHASE_SECONDS:
        return HIDE_WATCH_FAST_INTERVAL_SECONDS
    return HIDE_WATCH_SLOW_INTERVAL_SECONDS


class _HideWatchHandle:
    """Popen前に開始した非表示監視スレッドを制御するハンドル。"""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.result: dict[str, object] = {}
        self._extra_pids: set[int] = set()
        self._lock = threading.Lock()

    def add_target_pid(self, pid: int | None) -> None:
        value = int(pid or 0)
        if not value:
            return
        with self._lock:
            self._extra_pids.add(value)

    def extra_pids(self) -> set[int]:
        with self._lock:
            return set(self._extra_pids)

    def join(self, timeout: float | None = None) -> dict[str, object]:
        if self.thread is not None:
            self.thread.join(timeout)
        return self.result


def _start_hide_watch_before_popen(
    existing_acrobat_pids: set[int],
    *,
    duration_seconds: int = 5,
    enabled: bool = True,
) -> _HideWatchHandle:
    """subprocess.Popen より前に非表示監視スレッドを起動する。"""
    handle = _HideWatchHandle()
    existing = set(existing_acrobat_pids)

    def _run() -> None:
        handle.result = _hide_acrobat_windows_for_pids(
            set(),
            existing,
            duration_seconds=duration_seconds,
            enabled=enabled,
            extra_target_pids_provider=handle.extra_pids,
            started_before_popen=True,
        )

    thread = threading.Thread(target=_run, name="acrobat-hide-watch", daemon=True)
    handle.thread = thread
    thread.start()
    return handle


def _hide_acrobat_windows_for_pid(
    pid: int | None,
    existing_acrobat_pids: set[int],
    *,
    duration_seconds: int = 5,
    enabled: bool = True,
    interval_seconds: float | None = None,
) -> dict[str, object]:
    return _hide_acrobat_windows_for_pids(
        {int(pid or 0)} if pid else set(),
        existing_acrobat_pids,
        duration_seconds=duration_seconds,
        enabled=enabled,
        interval_seconds=interval_seconds,
    )


def _hide_acrobat_windows_for_pids(
    initial_target_pids: set[int],
    existing_acrobat_pids: set[int],
    *,
    duration_seconds: int = 10,
    enabled: bool = True,
    interval_seconds: float | None = None,
    extra_target_pids_provider: "Callable[[], set[int]] | None" = None,
    started_before_popen: bool = False,
) -> dict[str, object]:
    seconds = min(30, max(1, int(duration_seconds or 10)))
    initial_targets = {int(pid) for pid in initial_target_pids if int(pid or 0)}
    target_pids = set(initial_targets) - set(existing_acrobat_pids)
    ignored_existing = sorted(initial_targets & set(existing_acrobat_pids))
    info: dict[str, object] = {
        "hide_watch_enabled": bool(enabled),
        "hide_watch_seconds": seconds,
        "hide_watch_started_before_popen": bool(started_before_popen),
        "hide_watch_poll_interval_ms": int(HIDE_WATCH_FAST_INTERVAL_SECONDS * 1000)
        if interval_seconds is None
        else int(float(interval_seconds) * 1000),
        "hide_watch_fast_poll_interval_ms": int(HIDE_WATCH_FAST_INTERVAL_SECONDS * 1000),
        "hide_watch_slow_poll_interval_ms": int(HIDE_WATCH_SLOW_INTERVAL_SECONDS * 1000),
        "hide_watch_first_window_detect_elapsed_ms": None,
        "hide_watch_first_hide_elapsed_ms": None,
        "window_foreground_detected": False,
        "window_sent_to_bottom": False,
        "window_hidden_count": 0,
        "window_minimized_count": 0,
        "hide_watch_started": False,
        "hide_watch_target_pid": next(iter(sorted(target_pids)), None),
        "hide_watch_target_pids": sorted(target_pids),
        "hide_watch_existing_pids": sorted(existing_acrobat_pids),
        "existing_acrobat_pids_before": sorted(existing_acrobat_pids),
        "current_acrobat_pids": [],
        "new_acrobat_pids_detected": [],
        "target_acrobat_pids": sorted(target_pids),
        "ignored_existing_acrobat_pids": ignored_existing,
        "hide_watch_loop_count": 0,
        "windows_seen": [],
        "acrobat_window_found": False,
        "acrobat_window_hwnd": None,
        "acrobat_window_title": "",
        "acrobat_window_pid": None,
        "window_pid": None,
        "window_hwnd": None,
        "window_title": "",
        "window_visible": None,
        "window_is_target": False,
        "hide_window_called": False,
        "hide_attempted": False,
        "hide_async_result": None,
        "minimize_window_called": False,
        "set_bottom_called": False,
        "hide_result": None,
        "minimize_async_result": None,
        "minimize_result": None,
        "set_bottom_result": None,
        "ignored_existing_acrobat_window": bool(ignored_existing),
        "no_new_acrobat_pid_found": False,
        "hide_skipped_reason": "",
        "hide_watch_finished": False,
        "hide_watch_exception": "",
        "exception_type": "",
        "exception_message": "",
        "traceback": "",
    }
    if not enabled:
        info["hide_skipped_reason"] = "hide_watch_disabled"
        info["hide_watch_finished"] = True
        return info
    if not _is_windows():
        info["hide_skipped_reason"] = "windows_api_unavailable"
        info["hide_watch_finished"] = True
        return info

    info["hide_watch_started"] = True
    watch_start = time.monotonic()
    deadline = watch_start + seconds
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            elapsed = now - watch_start
            info["hide_watch_loop_count"] = int(info["hide_watch_loop_count"]) + 1
            if extra_target_pids_provider is not None:
                try:
                    provided = {int(p) for p in extra_target_pids_provider() if int(p or 0)}
                except Exception:
                    provided = set()
                if provided:
                    initial_targets |= provided
                    target_pids |= provided - set(existing_acrobat_pids)
            current_pids = _list_acrobat_process_ids()
            child_pids = set()
            for pid in initial_targets:
                child_pids.update(_child_process_ids(pid))
            new_pids = (current_pids | child_pids) - set(existing_acrobat_pids)
            target_pids.update(new_pids)
            target_pids -= set(existing_acrobat_pids)
            info["current_acrobat_pids"] = sorted(current_pids)
            info["new_acrobat_pids_detected"] = sorted(
                set(info["new_acrobat_pids_detected"]) | new_pids
            )
            info["target_acrobat_pids"] = sorted(target_pids)
            info["hide_watch_target_pids"] = sorted(target_pids)
            info["hide_watch_target_pid"] = next(iter(sorted(target_pids)), None)

            windows = _enum_top_level_windows_for_pids(target_pids | set(existing_acrobat_pids))
            for window in windows:
                hwnd = int(window.get("hwnd") or 0)
                if not hwnd:
                    continue
                window_pid = int(window.get("pid") or 0)
                is_existing = window_pid in existing_acrobat_pids
                is_target = window_pid in target_pids and not is_existing
                seen = {
                    "window_hwnd": hwnd,
                    "window_pid": window_pid,
                    "window_title": str(window.get("title") or ""),
                    "window_visible": bool(window.get("visible", True)),
                    "window_is_target": bool(is_target),
                    "window_is_existing": bool(is_existing),
                }
                info["windows_seen"].append(seen)
                info["window_hwnd"] = hwnd
                info["window_pid"] = window_pid
                info["window_title"] = seen["window_title"]
                info["window_visible"] = seen["window_visible"]
                info["window_is_target"] = bool(is_target)
                if is_existing:
                    info["ignored_existing_acrobat_window"] = True
                    continue
                if not is_target:
                    continue
                info["acrobat_window_found"] = True
                info["acrobat_window_hwnd"] = hwnd
                info["acrobat_window_title"] = str(window.get("title") or "")
                info["acrobat_window_pid"] = window_pid
                if info["hide_watch_first_window_detect_elapsed_ms"] is None:
                    info["hide_watch_first_window_detect_elapsed_ms"] = int(elapsed * 1000)
                if _is_foreground_window(hwnd):
                    info["window_foreground_detected"] = True
                result = _hide_or_minimize_window(hwnd)
                info.update(result)
                if result.get("hide_window_called"):
                    info["window_hidden_count"] = int(info["window_hidden_count"]) + 1
                if result.get("minimize_window_called"):
                    info["window_minimized_count"] = int(info["window_minimized_count"]) + 1
                if result.get("set_bottom_result"):
                    info["window_sent_to_bottom"] = True
                if info["hide_watch_first_hide_elapsed_ms"] is None:
                    info["hide_watch_first_hide_elapsed_ms"] = int(elapsed * 1000)
            if interval_seconds is None:
                sleep_for = _hide_watch_interval_seconds(elapsed)
            else:
                sleep_for = max(0.02, float(interval_seconds))
            time.sleep(sleep_for)
    except Exception as exc:
        info["hide_watch_exception"] = f"{type(exc).__name__}: {exc}"
        info["exception_type"] = type(exc).__name__
        info["exception_message"] = str(exc)
        info["traceback"] = traceback.format_exc()
        _LOGGER.warning("Acrobat Readerウィンドウ監視中に例外が発生しました。", exc_info=True)
    finally:
        info["no_new_acrobat_pid_found"] = not bool(set(info["new_acrobat_pids_detected"]))
        if not target_pids and not info["hide_skipped_reason"]:
            info["hide_skipped_reason"] = "no_new_acrobat_pid_found"
        info["hide_watch_finished"] = True
    return info


def _enum_top_level_windows_for_pid(pid: int) -> list[dict[str, object]]:
    return _enum_top_level_windows_for_pids({int(pid)})


def _enum_top_level_windows_for_pids(pids: set[int]) -> list[dict[str, object]]:
    if not _is_windows():
        return []
    target_pids = {int(pid) for pid in pids if int(pid or 0)}
    if not target_pids:
        return []
    import ctypes
    from ctypes import wintypes

    windows: list[dict[str, object]] = []
    user32 = ctypes.windll.user32
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        target_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        visible = bool(user32.IsWindowVisible(hwnd))
        if int(target_pid.value) in target_pids and visible:
            title = _window_title(hwnd)
            windows.append(
                {"hwnd": int(hwnd), "pid": int(target_pid.value), "title": title, "visible": visible}
            )
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return windows


def _window_title(hwnd: int) -> str:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        length = int(user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def _hide_or_minimize_window(hwnd: int) -> dict[str, object]:
    info: dict[str, object] = {
        "hide_window_called": False,
        "hide_attempted": False,
        "hide_async_result": None,
        "minimize_window_called": False,
        "set_bottom_called": False,
        "hide_result": None,
        "minimize_async_result": None,
        "minimize_result": None,
        "set_bottom_result": None,
    }
    hide_async_result = _show_window_async(hwnd, SW_HIDE)
    hide_result = _show_window(hwnd, SW_HIDE)
    minimize_async_result = _show_window_async(hwnd, SW_MINIMIZE)
    minimize_result = _show_window(hwnd, SW_MINIMIZE)
    info["hide_attempted"] = True
    info["hide_window_called"] = True
    info["hide_async_result"] = hide_async_result
    info["hide_result"] = hide_result
    info["minimize_window_called"] = True
    info["minimize_async_result"] = minimize_async_result
    info["minimize_result"] = minimize_result
    bottom_result = _set_window_bottom_no_activate(hwnd)
    info["set_bottom_called"] = bottom_result
    info["set_bottom_result"] = bottom_result
    return info


def _show_window_async(hwnd: int, command: int) -> bool:
    import ctypes

    try:
        return bool(ctypes.windll.user32.ShowWindowAsync(int(hwnd), int(command)))
    except Exception:
        return False


def _show_window(hwnd: int, command: int) -> bool:
    import ctypes

    try:
        return bool(ctypes.windll.user32.ShowWindow(int(hwnd), int(command)))
    except Exception:
        return False


def _is_foreground_window(hwnd: int) -> bool:
    import ctypes

    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def _set_window_bottom_no_activate(hwnd: int) -> bool:
    import ctypes

    try:
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        return bool(ctypes.windll.user32.SetWindowPos(int(hwnd), HWND_BOTTOM, 0, 0, 0, 0, flags))
    except Exception:
        return False


def _window_handles_for_process(pid: int) -> list[int]:
    return [int(window["hwnd"]) for window in _enum_top_level_windows_for_pid(pid)]


def _send_wm_close_to_windows(handles: list[int]) -> bool:
    import ctypes

    user32 = ctypes.windll.user32
    sent = False
    for hwnd in handles:
        try:
            user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0)
            sent = True
        except Exception:
            _LOGGER.warning("Acrobat ReaderウィンドウへのWM_CLOSE送信に失敗しました: hwnd=%s", hwnd, exc_info=True)
    return sent


def _terminate_process_by_pid(pid: int) -> bool:
    if not _is_windows():
        return False
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _minimize_windows_for_process(pid: int) -> None:
    if not _is_windows():
        return
    try:
        for hwnd in _window_handles_for_process(pid):
            _show_window(int(hwnd), SW_MINIMIZE)
    except Exception:
        _LOGGER.warning("Acrobat Readerウィンドウの最小化に失敗しました: pid=%s", pid, exc_info=True)


def _validate_saved_printer(printer_name: str) -> str:
    name = str(printer_name or "").strip()
    if not name:
        raise RuntimeError("印刷設定が未設定です。印刷設定画面でプリンターを選択してください。")
    from PySide6.QtPrintSupport import QPrinterInfo

    available_names = {info.printerName() for info in QPrinterInfo.availablePrinters()}
    if name not in available_names:
        raise RuntimeError("指定プリンターが見つかりません。印刷設定を確認してください。")
    return name


def _write_print_job_pdf(pdf_bytes: bytes, *, job_name: str = "") -> Path:
    if not pdf_bytes:
        raise RuntimeError("印刷用PDFデータが空です。")
    directory = get_print_jobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    token = _sanitize_filename_token(job_name) or "job"
    filename = f"voucher_print_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{token}.pdf"
    path = directory / filename
    path.write_bytes(pdf_bytes)
    if path.stat().st_size <= 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"印刷用PDFが空です: {path}")
    return path


def get_print_jobs_dir() -> Path:
    return get_app_data_dir() / "work" / "print_jobs"


# mm → PDFポイント（1pt = 1/72inch）。
_MM_TO_PT = 72.0 / 25.4


def get_print_adjusted_dir() -> Path:
    """印刷補正済み一時PDFの保存先（元PDFとは別ディレクトリ）。"""
    return get_app_data_dir() / "work" / "print_adjusted"


def create_adjusted_print_pdf(
    source_pdf_path,
    output_pdf_path,
    margin_left_mm: float,
    margin_right_mm: float,
    margin_top_mm: float,
    margin_bottom_mm: float,
    scale_x_percent: float,
    scale_y_percent: float,
    offset_x_mm: float,
    offset_y_mm: float,
) -> Path:
    """印刷補正（余白・倍率・位置）を適用した一時PDFを作成する。

    元PDFは変更せず、各ページのページサイズ（MediaBox）を維持したまま、
    内容だけを拡大縮小・移動する。ラスタライズはせずベクター情報を保つ。
    複数ページ・回転ページにも対応する（回転は /Rotate を保持し、内容変換は
    未回転座標で行う）。失敗時は例外を送出し、呼び出し側で元PDFを印刷しない。

    補正の意味:
      - 左/上余白補正(正): 内容を右/下へ寄せる（内容領域を縮める）
      - 右/下余白補正(正): 右/下に余白を足すため内容を縮める
      - 横/縦倍率: 100%基準で内容を拡大縮小
      - 横位置補正(正): 右へ移動、縦位置補正(正): 下へ移動
    """
    from pypdf import PdfWriter, Transformation

    source = Path(source_pdf_path)
    output = Path(output_pdf_path)
    # ページを writer へ複製してから変換を適用する（reader ページ直変換は非推奨）。
    writer = PdfWriter(clone_from=str(source))

    ml = float(margin_left_mm) * _MM_TO_PT
    mr = float(margin_right_mm) * _MM_TO_PT
    mt = float(margin_top_mm) * _MM_TO_PT
    mb = float(margin_bottom_mm) * _MM_TO_PT
    ox = float(offset_x_mm) * _MM_TO_PT
    oy = float(offset_y_mm) * _MM_TO_PT
    scale_x = float(scale_x_percent) / 100.0
    scale_y = float(scale_y_percent) / 100.0

    for page in writer.pages:
        box = page.mediabox
        bx = float(box.left)
        by = float(box.bottom)
        width = float(box.width)
        height = float(box.height)
        # 余白補正による内容領域の縮小率（負の余白なら拡大方向）。
        sx_margin = (width - ml - mr) / width if width > 0 else 1.0
        sy_margin = (height - mt - mb) / height if height > 0 else 1.0
        sx = sx_margin * scale_x
        sy = sy_margin * scale_y
        # 補正後内容の左下を (左余白, 下余白) に合わせ、位置補正を加える。
        # PDF座標は下が原点・上が正のため、縦位置補正は正で下へ = y を減らす。
        e = bx * (1.0 - sx) + ml + ox
        f = by * (1.0 - sy) + mb - oy
        page.add_transformation(Transformation(ctm=(sx, 0.0, 0.0, sy, e, f)))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as fp:
        writer.write(fp)
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError(f"印刷補正PDFの作成に失敗しました: {output}")
    return output


def _print_adjustment_metadata_fields(settings) -> dict[str, object]:
    """印刷補正のログ用フィールド（有効/無効に関わらず常に出す）。"""
    return {
        "print_adjustment_enabled": bool(getattr(settings, "print_adjustment_enabled", False)),
        "print_adjustment_margin_left_mm": float(getattr(settings, "print_adjustment_margin_left_mm", 0.0)),
        "print_adjustment_margin_right_mm": float(getattr(settings, "print_adjustment_margin_right_mm", 0.0)),
        "print_adjustment_margin_top_mm": float(getattr(settings, "print_adjustment_margin_top_mm", 0.0)),
        "print_adjustment_margin_bottom_mm": float(getattr(settings, "print_adjustment_margin_bottom_mm", 0.0)),
        "print_adjustment_scale_x_percent": float(getattr(settings, "print_adjustment_scale_x_percent", 100.0)),
        "print_adjustment_scale_y_percent": float(getattr(settings, "print_adjustment_scale_y_percent", 100.0)),
        "print_adjustment_offset_x_mm": float(getattr(settings, "print_adjustment_offset_x_mm", 0.0)),
        "print_adjustment_offset_y_mm": float(getattr(settings, "print_adjustment_offset_y_mm", 0.0)),
        "print_adjustment_profile_name": str(getattr(settings, "sumatra_profile_name", "") or ""),
    }


def _apply_print_adjustment_for_sumatra(settings, source_pdf_path: Path, metadata: dict) -> Path:
    """印刷補正が有効なら補正PDFを作成し、その実パスを返す。無効なら元PDF。

    失敗時は RuntimeError を送出する（呼び出し側で元PDFを印刷しない）。
    """
    fields = _print_adjustment_metadata_fields(settings)
    metadata.update(fields)
    metadata["print_adjustment_source_pdf"] = str(source_pdf_path)
    metadata.setdefault("print_adjustment_output_pdf", "")
    metadata.setdefault("print_adjustment_output_pdf_exists", False)
    metadata.setdefault("print_adjustment_pdf_created", False)
    metadata["sumatra_pdf_path_actual"] = str(source_pdf_path)
    if not fields["print_adjustment_enabled"]:
        return source_pdf_path

    directory = get_print_adjusted_dir()
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{source_pdf_path.stem}_adjusted.pdf"
    adjusted = create_adjusted_print_pdf(
        source_pdf_path,
        output,
        margin_left_mm=fields["print_adjustment_margin_left_mm"],
        margin_right_mm=fields["print_adjustment_margin_right_mm"],
        margin_top_mm=fields["print_adjustment_margin_top_mm"],
        margin_bottom_mm=fields["print_adjustment_margin_bottom_mm"],
        scale_x_percent=fields["print_adjustment_scale_x_percent"],
        scale_y_percent=fields["print_adjustment_scale_y_percent"],
        offset_x_mm=fields["print_adjustment_offset_x_mm"],
        offset_y_mm=fields["print_adjustment_offset_y_mm"],
    )
    metadata["print_adjustment_output_pdf"] = str(adjusted)
    metadata["print_adjustment_output_pdf_exists"] = adjusted.is_file()
    metadata["print_adjustment_pdf_created"] = True
    metadata["sumatra_pdf_path_actual"] = str(adjusted)
    _LOGGER.info("印刷補正PDFを作成しました: %s", adjusted)
    return adjusted


def cleanup_old_print_jobs(retention_days: int = PRINT_JOB_RETENTION_DAYS) -> int:
    cutoff = datetime.now() - timedelta(days=max(1, int(retention_days or PRINT_JOB_RETENTION_DAYS)))
    removed = 0
    for directory, pattern in (
        (get_print_jobs_dir(), "voucher_print_*.pdf"),
        (get_print_adjusted_dir(), "*_adjusted.pdf"),
    ):
        if not directory.exists():
            continue
        for path in directory.glob(pattern):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                _LOGGER.warning("古い印刷ジョブPDFの削除に失敗しました: %s", path, exc_info=True)
    return removed


def _sanitize_filename_token(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", text)
    return text[:80].strip("._-")


def _shell_execute_printto(pdf_path: Path, printer_name: str) -> int | None:
    import ctypes

    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "printto",
        str(pdf_path),
        f'"{printer_name}"',
        None,
        0,
    )
    if result <= 32:
        raise RuntimeError(f"ShellExecute printto failed: {result}")
    return None


def _log_driver_setting_notice(settings, printer_name: str) -> None:
    _LOGGER.info(
        "Acrobat Reader経由印刷では用紙サイズ・白黒設定はプリンタードライバー側設定が優先される場合があります: "
        "printer=%s paper=%s orientation=%s color=%s copies=%s",
        printer_name,
        getattr(settings, "paper_size", "B5"),
        getattr(settings, "orientation", "landscape"),
        getattr(settings, "color_mode", "grayscale"),
        getattr(settings, "copies", 1),
    )


def log_preview_print_event(event_type: str, **fields: object) -> None:
    """プレビュー画面内印刷の状態遷移を jsonl へ記録する。

    プレビュー印刷固まり調査用のイベント（preview_print_clicked など）を残す。
    モーダルダイアログは表示しないため modal_messagebox_shown は常に False。
    """
    event: dict[str, object] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "print_backend": "acrobat",
        "event_type": event_type,
        "modal_messagebox_shown": False,
        "modal_messagebox_suppressed": True,
    }
    event.update(fields)
    _LOGGER.info("プレビュー印刷イベント: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("プレビュー印刷JSONLログの書き込みに失敗しました。", exc_info=True)


def _log_acrobat_print_event(
    settings,
    acrobat_path: str,
    pdf_path: str,
    printer_name: str,
    *,
    command: list[str] | None = None,
    process_id: int | None = None,
    process_started: bool = False,
    process_exited_quickly: bool = False,
    process_exit_code: int | None = None,
    error_message: str = "",
    fallback_used: bool = False,
    close_info: dict[str, object] | None = None,
    hide_watch_info: dict[str, object] | None = None,
    print_metadata: dict[str, object] | None = None,
    exception_type: str = "",
    exception_message: str = "",
    traceback_text: str = "",
) -> None:
    close = close_info or {}
    hide_watch = hide_watch_info or {}
    meta = print_metadata or {}
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "print_job_id": meta.get("print_job_id", ""),
        "print_backend": "acrobat",
        "print_backend_default_source": meta.get("print_backend_default_source", ""),
        "acrobat_selected_as_non_standard_backend": bool(
            meta.get("acrobat_selected_as_non_standard_backend", True)
        ),
        "powershell_usage_detected": bool(meta.get("powershell_usage_detected", False)),
        "subprocess_shell_true_detected": bool(meta.get("subprocess_shell_true_detected", False)),
        "console_window_suppressed": bool(meta.get("console_window_suppressed", False)),
        "create_no_window_used": bool(meta.get("create_no_window_used", False)),
        "worker_started": bool(meta.get("worker_started", False)),
        "worker_finished": bool(meta.get("worker_finished", False)),
        "worker_error": bool(meta.get("worker_error", False)),
        "print_worker_thread_id": meta.get("print_worker_thread_id", ""),
        "ui_thread_id": meta.get("ui_thread_id", ""),
        "selected_count": int(meta.get("selected_count", 1) or 1),
        "generated_pdf_count": int(meta.get("generated_pdf_count", 1) or 1),
        "merged_pdf_created": bool(meta.get("merged_pdf_created", False)),
        "merged_pdf_path": meta.get("merged_pdf_path", ""),
        "acrobat_launch_count": int(meta.get("acrobat_launch_count", 1 if process_started else 0) or 0),
        "acrobat_command_count": int(meta.get("acrobat_command_count", 1 if command else 0) or 0),
        "ui_thread_blocked": bool(meta.get("ui_thread_blocked", False)),
        "request_sent_time": meta.get("request_sent_time", ""),
        "elapsed_create_pdf_ms": meta.get("elapsed_create_pdf_ms"),
        "elapsed_popen_ms": meta.get("elapsed_popen_ms"),
        "elapsed_send_request_ms": meta.get("elapsed_send_request_ms"),
        "elapsed_request_sent_ms": meta.get("elapsed_request_sent_ms"),
        "elapsed_worker_total_ms": meta.get("elapsed_worker_total_ms"),
        "acrobat_path": acrobat_path,
        "pdf_path": pdf_path,
        "printer_name": printer_name,
        "paper_size_setting": getattr(settings, "paper_size", "B5"),
        "orientation_setting": getattr(settings, "orientation", "landscape"),
        "color_mode_setting": getattr(settings, "color_mode", "grayscale"),
        "copies": getattr(settings, "copies", 1),
        "command": command or [],
        "acrobat_command_args": command or [],
        "acrobat_popen_started": bool(meta.get("acrobat_popen_started", process_started)),
        "popen_started": bool(meta.get("popen_started", process_started)),
        "acrobat_process_id": process_id,
        "acrobat_request_sent_signal_emitted": bool(
            meta.get("acrobat_request_sent_signal_emitted", False)
        ),
        "request_sent_signal_emitted": bool(meta.get("request_sent_signal_emitted", False)),
        "ui_released_after_popen": bool(meta.get("ui_released_after_popen", False)),
        "acrobat_args_include_n": "/n" in (command or []),
        "acrobat_args_include_s": "/s" in (command or []),
        "acrobat_args_include_o": "/o" in (command or []),
        "acrobat_args_include_h": "/h" in (command or []),
        "acrobat_args_include_t": "/t" in (command or []),
        "hide_window_requested": bool(getattr(settings, "acrobat_hide_window", True)),
        "startup_show_window": _startup_show_window_setting(settings),
        "hide_watch_enabled": hide_watch.get(
            "hide_watch_enabled", bool(getattr(settings, "acrobat_hide_watch_enabled", True))
        ),
        "hide_watch_seconds": hide_watch.get(
            "hide_watch_seconds", getattr(settings, "acrobat_hide_watch_seconds", 5)
        ),
        "existing_acrobat_pids_before": hide_watch.get("existing_acrobat_pids_before", []),
        "acrobat_existing_pids": hide_watch.get("existing_acrobat_pids_before", []),
        "popen_pid": process_id,
        "popen_pid_exited_quickly": process_exited_quickly,
        "current_acrobat_pids": hide_watch.get("current_acrobat_pids", []),
        "new_acrobat_pids_detected": hide_watch.get("new_acrobat_pids_detected", []),
        "target_acrobat_pids": hide_watch.get("target_acrobat_pids", []),
        "acrobat_target_pids": hide_watch.get("target_acrobat_pids", []),
        "ignored_existing_acrobat_pids": hide_watch.get("ignored_existing_acrobat_pids", []),
        "hide_watch_loop_count": hide_watch.get("hide_watch_loop_count", 0),
        "windows_seen": hide_watch.get("windows_seen", []),
        "window_pid": hide_watch.get("window_pid"),
        "window_hwnd": hide_watch.get("window_hwnd"),
        "window_title": hide_watch.get("window_title", ""),
        "window_visible": hide_watch.get("window_visible"),
        "window_is_target": hide_watch.get("window_is_target", False),
        "hide_attempted": hide_watch.get("hide_attempted", False),
        "hide_async_result": hide_watch.get("hide_async_result"),
        "minimize_async_result": hide_watch.get("minimize_async_result"),
        "set_bottom_result": hide_watch.get("set_bottom_result"),
        "no_new_acrobat_pid_found": hide_watch.get("no_new_acrobat_pid_found", False),
        "hide_skipped_reason": hide_watch.get("hide_skipped_reason", ""),
        "hide_watch_started": hide_watch.get("hide_watch_started", False),
        "hide_watch_started_before_popen": hide_watch.get("hide_watch_started_before_popen", False),
        "hide_watch_poll_interval_ms": hide_watch.get("hide_watch_poll_interval_ms"),
        "hide_watch_fast_poll_interval_ms": hide_watch.get("hide_watch_fast_poll_interval_ms"),
        "hide_watch_slow_poll_interval_ms": hide_watch.get("hide_watch_slow_poll_interval_ms"),
        "hide_watch_first_window_detect_elapsed_ms": hide_watch.get(
            "hide_watch_first_window_detect_elapsed_ms"
        ),
        "hide_watch_first_hide_elapsed_ms": hide_watch.get("hide_watch_first_hide_elapsed_ms"),
        "window_foreground_detected": hide_watch.get("window_foreground_detected", False),
        "window_sent_to_bottom": hide_watch.get("window_sent_to_bottom", False),
        "window_hidden_count": hide_watch.get("window_hidden_count", 0),
        "acrobat_window_hidden_count": hide_watch.get("window_hidden_count", 0),
        "window_minimized_count": hide_watch.get("window_minimized_count", 0),
        "acrobat_window_minimized_count": hide_watch.get("window_minimized_count", 0),
        "hide_watch_target_pid": hide_watch.get("hide_watch_target_pid"),
        "hide_watch_target_pids": hide_watch.get("hide_watch_target_pids", []),
        "hide_watch_existing_pids": hide_watch.get("hide_watch_existing_pids", []),
        "acrobat_window_found": hide_watch.get("acrobat_window_found", False),
        "acrobat_window_hwnd": hide_watch.get("acrobat_window_hwnd"),
        "acrobat_window_title": hide_watch.get("acrobat_window_title", ""),
        "acrobat_window_pid": hide_watch.get("acrobat_window_pid"),
        "hide_window_called": hide_watch.get("hide_window_called", False),
        "minimize_window_called": hide_watch.get("minimize_window_called", False),
        "set_bottom_called": hide_watch.get("set_bottom_called", False),
        "hide_result": hide_watch.get("hide_result"),
        "minimize_result": hide_watch.get("minimize_result"),
        "ignored_existing_acrobat_window": hide_watch.get("ignored_existing_acrobat_window", False),
        "hide_watch_finished": hide_watch.get("hide_watch_finished", False),
        "hide_watch_exception": hide_watch.get("hide_watch_exception", ""),
        "process_id": process_id,
        "process_started": process_started,
        "process_exited_quickly": process_exited_quickly,
        "process_exit_code": process_exit_code,
        "close_after_print": close.get(
            "close_after_print", bool(getattr(settings, "acrobat_close_after_print", True))
        ),
        "close_delay_seconds": close.get(
            "close_delay_seconds", getattr(settings, "acrobat_close_delay_seconds", 10)
        ),
        "close_target_process_id": close.get("close_target_process_id"),
        "close_target_pids": close.get("close_target_pids", []),
        "close_skipped_existing_pids": close.get("close_skipped_existing_pids", []),
        "close_sent_pids": close.get("close_sent_pids", []),
        "close_result_by_pid": close.get("close_result_by_pid", {}),
        "close_window_handles": close.get("close_window_handles", []),
        "close_started": close.get("close_started", False),
        "acrobat_close_delay_started": close.get("close_delay_started", False),
        "acrobat_close_attempted": close.get("close_attempted", False),
        "acrobat_close_skipped_existing_pid": bool(close.get("close_skipped_existing_pids", [])),
        "close_finished": close.get("close_finished", False),
        "close_sent": close.get("close_sent", False),
        "close_result": close.get("close_result", ""),
        "close_skipped_reason": close.get("close_skipped_reason", ""),
        "exception_type": exception_type,
        "exception_message": exception_message,
        "traceback": traceback_text,
        "error_message": error_message,
        "fallback_used": fallback_used,
    }
    _LOGGER.info("Acrobat Reader経由印刷: %s", event)
    try:
        debug_dir = get_order_capture_debug_dir()
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"voucher_print_{datetime.now().strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        _LOGGER.warning("伝票印刷JSONLログの書き込みに失敗しました。", exc_info=True)


def _set_zero_page_margins(printer, q_margins_f_cls, q_page_layout_cls) -> None:
    margins = q_margins_f_cls(0, 0, 0, 0)
    try:
        layout = printer.pageLayout()
        layout.setMargins(margins)
        printer.setPageLayout(layout)
        return
    except Exception:
        pass
    try:
        printer.setPageMargins(margins, q_page_layout_cls.Unit.Millimeter)
    except Exception:
        _LOGGER.warning("プリンター余白0の設定に失敗しました。", exc_info=True)


def _print_pdf_bytes(pdf_bytes: bytes, printer) -> None:
    tmp_path = _write_temp_pdf(pdf_bytes)
    doc = None
    try:
        doc = _try_load_pdf_document(tmp_path)
        if doc is not None:
            try:
                _print_with_qpdf_document(doc, printer, tmp_path)
            except Exception as exc:
                _LOGGER.warning("QPdfDocument印刷に失敗したためPyMuPDFへフォールバックします: %s", exc)
                _print_with_pymupdf(pdf_bytes, printer)
        else:
            _print_with_pymupdf(pdf_bytes, printer)
    finally:
        if doc is not None:
            doc.close()
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_temp_pdf(pdf_bytes: bytes) -> Path:
    if not pdf_bytes:
        raise RuntimeError("印刷用PDFデータが空です。")

    fd, raw_path = tempfile.mkstemp(suffix=".pdf")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(pdf_bytes)
            fp.flush()
            os.fsync(fp.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise

    if not path.exists():
        raise RuntimeError(f"印刷用PDFの作成に失敗しました: {path}")
    size = path.stat().st_size
    if size <= 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"印刷用PDFが空です: {path}")
    return path


def _try_load_pdf_document(path: Path):
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtCore import QLibraryInfo, qVersion
    import PySide6

    if not path.exists():
        raise RuntimeError(f"印刷用PDFが見つかりません: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError(f"印刷用PDFが空です: {path}")

    doc = QPdfDocument(None)
    load_result = doc.load(str(path))
    status = doc.status()
    error = doc.error()
    page_count = doc.pageCount()
    _LOGGER.info(
        "印刷用PDF読み込み: path=%s size=%s load_result=%s status=%s error=%s pageCount=%s Qt=%s PySide6=%s plugins=%s",
        path,
        size,
        load_result,
        status,
        error,
        page_count,
        qVersion(),
        getattr(PySide6, "__version__", ""),
        QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath),
    )

    if load_result == QPdfDocument.Error.None_ and page_count > 0:
        return doc
    if status == QPdfDocument.Status.Ready and page_count > 0:
        return doc
    _LOGGER.warning(
        "QPdfDocumentでPDFを使用できないためPyMuPDFへフォールバックします: path=%s size=%s load_result=%s status=%s error=%s pageCount=%s",
        path,
        size,
        load_result,
        status,
        error,
        page_count,
    )
    doc.close()
    return None


def _print_with_qpdf_document(doc, printer, path: Path) -> None:
    from PySide6.QtGui import QPainter
    from PySide6.QtCore import QSizeF

    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("印刷を開始できませんでした。プリンターの設定を確認してください。")
    try:
        dpi = printer.resolution()
        for i in range(doc.pageCount()):
            if i > 0:
                printer.newPage()
            page_size_pt = doc.pagePointSize(i)
            render_size = QSizeF(
                page_size_pt.width() * dpi / 72.0,
                page_size_pt.height() * dpi / 72.0,
            ).toSize()
            image = doc.render(i, render_size)
            if image.isNull():
                raise RuntimeError(f"PDFページの描画に失敗しました: {path} page={i + 1}")
            _log_print_geometry(printer, page_size_pt.width(), page_size_pt.height(), image.width(), image.height())
            painter.drawImage(
                _target_rect_for_scale_mode(
                    printer,
                    image.width(),
                    image.height(),
                    page_size_pt.width(),
                    page_size_pt.height(),
                ),
                image,
            )
    finally:
        painter.end()


def _print_with_pymupdf(pdf_bytes: bytes, printer) -> None:
    from PySide6.QtGui import QImage, QPainter
    import fitz

    pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
    if pdf.page_count <= 0:
        raise RuntimeError("PDFに印刷可能なページがありません。")
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("印刷を開始できませんでした。プリンターの設定を確認してください。")
    try:
        dpi = printer.resolution()
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for i, page in enumerate(pdf):
            if i > 0:
                printer.newPage()
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            if image.isNull():
                raise RuntimeError(f"PDFページの画像化に失敗しました: page={i + 1}")
            page_rect = page.rect
            _log_print_geometry(printer, page_rect.width, page_rect.height, image.width(), image.height())
            painter.drawImage(
                _target_rect_for_scale_mode(
                    printer,
                    image.width(),
                    image.height(),
                    page_rect.width,
                    page_rect.height,
                ),
                image.copy(),
            )
    finally:
        painter.end()
        pdf.close()


def _target_rect_for_scale_mode(
    printer,
    image_width: int,
    image_height: int,
    pdf_page_width_pt: float,
    pdf_page_height_pt: float,
):
    """印刷倍率設定に応じた描画先を返す。

    actual_size は Acrobat Reader の「実際のサイズ」に合わせ、印刷可能領域への
    fit や shrink-to-fit を行わず PDF ページサイズ基準の100%で描画する。
    """
    scale_mode = str(getattr(printer, "_voucher_print_scale_mode", "actual_size") or "actual_size")
    if scale_mode == "fit_to_page":
        return _fit_to_printable_area_rect(printer, image_width, image_height)
    return _actual_size_rect(printer, image_width, image_height)


def _actual_size_rect(printer, image_width: int, image_height: int):
    from PySide6.QtCore import QRectF

    _warn_if_actual_size_may_clip(printer, image_width, image_height)
    return QRectF(0, 0, float(image_width), float(image_height))


def _fit_to_printable_area_rect(printer, image_width: int, image_height: int):
    """PDF画像を印刷可能領域の中央へ配置し、fit_to_page の場合だけ縮小する。"""
    from PySide6.QtCore import QRectF

    page = printer.pageLayout().paintRectPixels(printer.resolution())
    page_rect = QRectF(page)
    width = float(image_width)
    height = float(image_height)
    if width <= 0 or height <= 0:
        return page_rect
    scale = min(1.0, page_rect.width() / width, page_rect.height() / height)
    target_w = width * scale
    target_h = height * scale
    x = page_rect.x() + (page_rect.width() - target_w) / 2.0
    y = page_rect.y() + (page_rect.height() - target_h) / 2.0
    return QRectF(x, y, target_w, target_h)


def _warn_if_actual_size_may_clip(printer, image_width: int, image_height: int) -> None:
    try:
        paint = printer.pageLayout().paintRectPixels(printer.resolution())
        if image_width > paint.width() or image_height > paint.height():
            _LOGGER.warning(
                "100%%印刷のため、プリンターの余白によって端が見切れる可能性があります。"
                "出力が見切れる場合は、印刷設定で「用紙に合わせる」を選択してください。"
            )
    except Exception:
        return


def _log_print_geometry(
    printer,
    pdf_page_width_pt: float,
    pdf_page_height_pt: float,
    image_width: int,
    image_height: int,
) -> None:
    """PDFページサイズとプリンター用紙サイズの比較をログへ出す。"""
    dpi = float(printer.resolution())
    pdf_page_width_mm = float(pdf_page_width_pt) * 25.4 / 72.0
    pdf_page_height_mm = float(pdf_page_height_pt) * 25.4 / 72.0
    full_rect_mm = _page_rect_mm(printer, full=True)
    paint_rect_mm = _page_rect_mm(printer, full=False)
    full_rect_px = _page_rect_pixels(printer, full=True)
    paint_rect_px = _page_rect_pixels(printer, full=False)
    printer_page_width_mm = full_rect_mm.width() if full_rect_mm is not None else 0.0
    printer_page_height_mm = full_rect_mm.height() if full_rect_mm is not None else 0.0
    scale_x = image_width / max(1.0, float(pdf_page_width_pt) * dpi / 72.0)
    scale_y = image_height / max(1.0, float(pdf_page_height_pt) * dpi / 72.0)
    scale_mode = str(getattr(printer, "_voucher_print_scale_mode", "actual_size") or "actual_size")
    effective_scale = 1.0
    if scale_mode == "fit_to_page" and paint_rect_px is not None:
        effective_scale = min(
            1.0,
            paint_rect_px.width() / max(1.0, float(image_width)),
            paint_rect_px.height() / max(1.0, float(image_height)),
        )
    margins_mm = _margins_mm(printer)
    _LOGGER.info(
        "伝票即時印刷サイズ診断: pdf_page_width_pt=%.3f pdf_page_height_pt=%.3f "
        "pdf_page_width_mm=%.3f pdf_page_height_mm=%.3f "
        "printer_page_width_mm=%.3f printer_page_height_mm=%.3f "
        "printer_full_rect=%s printer_paint_rect=%s selected_paper_size=%s "
        "selected_orientation=%s scale_x=%.6f scale_y=%.6f effective_scale=%.6f "
        "full_page_enabled=%s margins_mm=%s scale_mode=%s",
        pdf_page_width_pt,
        pdf_page_height_pt,
        pdf_page_width_mm,
        pdf_page_height_mm,
        printer_page_width_mm,
        printer_page_height_mm,
        full_rect_mm,
        paint_rect_mm,
        getattr(getattr(printer, "_voucher_print_settings", None), "paper_size", "B5"),
        getattr(getattr(printer, "_voucher_print_settings", None), "orientation", "landscape"),
        scale_x,
        scale_y,
        effective_scale,
        _is_full_page_enabled(printer),
        margins_mm,
        scale_mode,
    )


def _page_rect_mm(printer, *, full: bool):
    from PySide6.QtGui import QPageLayout

    try:
        layout = printer.pageLayout()
        return (
            layout.fullRect(QPageLayout.Unit.Millimeter)
            if full
            else layout.paintRect(QPageLayout.Unit.Millimeter)
        )
    except Exception:
        return None


def _page_rect_pixels(printer, *, full: bool):
    try:
        layout = printer.pageLayout()
        return (
            layout.fullRectPixels(printer.resolution())
            if full
            else layout.paintRectPixels(printer.resolution())
        )
    except Exception:
        return None


def _margins_mm(printer):
    from PySide6.QtGui import QPageLayout

    try:
        return printer.pageLayout().margins(QPageLayout.Unit.Millimeter)
    except Exception:
        return None


def _is_full_page_enabled(printer) -> bool:
    try:
        return bool(printer.fullPage())
    except Exception:
        return True
