"""伝票印刷プレビュー画面（アプリ内表示）。

PDFバイト列をメモリ上で受け取り、PyMuPDF（無ければ QPdfDocument）で各ページを
画像化して縦並びに表示する。一時PDFファイルや正式PDFは一切保存しない。
プレビュー画面の「印刷」ボタンからは、保存済み印刷設定で即時印刷する。
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.window_geometry import clamp_window_to_available_geometry

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget as _QWidget

_LOGGER = logging.getLogger(__name__)

# 表示倍率の範囲（100%=1.0）。
_MIN_ZOOM = 0.25
_MAX_ZOOM = 4.0
_ZOOM_STEP = 0.25
_BASE_RENDER_ZOOM = 2.0  # 1.0倍表示時の基準レンダリング解像度（PyMuPDF Matrix）。


class VoucherPrintPreviewWindow(QWidget):
    """PDFバイト列をアプリ内で表示する印刷プレビュー画面。"""

    def __init__(
        self, pdf_bytes: bytes, parent: "_QWidget | None" = None, *,
        edit_render_trace_id: str = "", edit_objects_sha256: str = "",
        preview_cache_hit: bool = False,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.pdf_bytes = bytes(pdf_bytes or b"")
        self.edit_render_trace_id = str(edit_render_trace_id or "")
        self.edit_objects_sha256 = str(edit_objects_sha256 or "")
        self.pdf_sha256 = hashlib.sha256(self.pdf_bytes).hexdigest()
        self.preview_cache_hit = bool(preview_cache_hit)
        self._zoom = 1.0
        self._page_labels: list[QLabel] = []
        self._print_in_progress = False
        self._print_workers: set[object] = set()

        self.setWindowTitle("印刷プレビュー")
        clamp_window_to_available_geometry(
            self,
            desired_width=900,
            desired_height=1000,
            min_width=720,
            min_height=560,
        )
        self._build_ui()
        self._render_pages()

    # ── UI構築 ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.print_button = QPushButton("印刷")
        self.print_button.clicked.connect(self._on_print)
        toolbar.addWidget(self.print_button)

        self.zoom_out_button = QPushButton("縮小")
        self.zoom_out_button.clicked.connect(self._on_zoom_out)
        toolbar.addWidget(self.zoom_out_button)

        self.zoom_reset_button = QPushButton("100%")
        self.zoom_reset_button.clicked.connect(self._on_zoom_reset)
        toolbar.addWidget(self.zoom_reset_button)

        self.zoom_in_button = QPushButton("拡大")
        self.zoom_in_button.clicked.connect(self._on_zoom_in)
        toolbar.addWidget(self.zoom_in_button)

        self.page_label = QLabel("")
        toolbar.addWidget(self.page_label)

        toolbar.addStretch(1)

        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.close)
        toolbar.addWidget(self.close_button)

        root.addLayout(toolbar)

        # 印刷状態はモーダルダイアログではなくステータスラベルへ表示する。
        self.status_label = QLabel("")
        self.status_label.setObjectName("previewPrintStatusLabel")
        root.addWidget(self.status_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._pages_container = QWidget()
        self._pages_layout = QVBoxLayout(self._pages_container)
        self._pages_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._pages_container)
        root.addWidget(self._scroll, 1)

    # ── ページ描画 ───────────────────────────────────────────────────────────
    def _render_pages(self) -> None:
        # 既存のページラベルをクリアする。
        for label in self._page_labels:
            self._pages_layout.removeWidget(label)
            label.deleteLater()
        self._page_labels = []

        _LOGGER.info(
            "event=voucher_preview_png_rasterize trace_id=%s pdf_sha256=%s "
            "edit_objects_sha256=%s cache_hit=%s zoom=%s",
            self.edit_render_trace_id, self.pdf_sha256,
            self.edit_objects_sha256, self.preview_cache_hit,
            _BASE_RENDER_ZOOM,
        )
        self._base_pixmaps = self._build_base_pixmaps(self.pdf_bytes)
        if not self._base_pixmaps:
            placeholder = QLabel("プレビューを表示できませんでした。")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._pages_layout.addWidget(placeholder)
            self._page_labels = [placeholder]
            self.page_label.setText("0 ページ")
            return

        for pixmap in self._base_pixmaps:
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._pages_layout.addWidget(label)
            self._page_labels.append(label)
        self._apply_zoom()
        _LOGGER.info(
            "event=voucher_preview_pixmap_shown trace_id=%s pdf_sha256=%s "
            "edit_objects_sha256=%s cache_hit=%s page_count=%s",
            self.edit_render_trace_id, self.pdf_sha256,
            self.edit_objects_sha256, self.preview_cache_hit,
            len(self._base_pixmaps),
        )

    @staticmethod
    def _build_base_pixmaps(pdf_bytes: bytes) -> list[QPixmap]:
        """PDFバイト列を1.0倍表示用の基準QPixmap一覧へレンダリングする。"""
        if not pdf_bytes:
            return []
        pixmaps = _render_with_pymupdf(pdf_bytes)
        if pixmaps:
            return pixmaps
        return _render_with_qpdf(pdf_bytes)

    def _apply_zoom(self) -> None:
        for label, base in zip(self._page_labels, self._base_pixmaps):
            target_w = max(1, int(base.width() * self._zoom))
            scaled = base.scaledToWidth(target_w, Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(scaled)
            label.setFixedSize(scaled.size())
        self.page_label.setText(f"{len(self._base_pixmaps)} ページ  {int(round(self._zoom * 100))}%")

    # ── 操作 ─────────────────────────────────────────────────────────────────
    def _set_status(self, text: str) -> None:
        """印刷状態をステータスラベルへ表示する（モーダルダイアログは出さない）。"""
        self.status_label.setText(text)

    def _on_print(self) -> None:
        from app import voucher_print_service

        voucher_print_service.log_preview_print_event("preview_print_clicked")
        # 印刷要求送信前の連打のみ抑止する。
        if self._print_in_progress:
            self._set_status("印刷処理中です")
            return
        try:
            from app.voucher_settings import (
                PRINT_BACKEND_ACROBAT,
                PRINT_BACKEND_SUMATRA,
                load_voucher_printer_settings,
            )

            settings = load_voucher_printer_settings()

            self._print_in_progress = True
            self.print_button.setEnabled(False)
            voucher_print_service.log_preview_print_event("preview_print_button_disabled")
            self._set_status("印刷要求を送信中...")

            if settings.print_backend not in (PRINT_BACKEND_ACROBAT, PRINT_BACKEND_SUMATRA):
                voucher_print_service.print_pdf_direct(self.pdf_bytes, self)
                self._print_in_progress = False
                self.print_button.setEnabled(True)
                voucher_print_service.log_preview_print_event("preview_print_button_enabled")
                self._set_status("印刷要求を送信しました")
                return

            def _status_for(event: str, message: str = "") -> str:
                if settings.print_backend == PRINT_BACKEND_ACROBAT:
                    return {
                        "enqueued": "Acrobat Reader印刷ジョブを登録しました",
                        "request_sent": "Acrobat Readerへ印刷要求を送信しました",
                        "finished": "Acrobat Reader印刷処理完了",
                        "error": f"Acrobat Reader印刷でエラーが発生しました: {message}",
                    }[event]
                return {
                    "enqueued": "SumatraPDF印刷ジョブを登録しました",
                    "request_sent": "SumatraPDFへ印刷要求を送信しました",
                    "finished": "SumatraPDF印刷処理完了",
                    "error": f"SumatraPDF印刷でエラーが発生しました: {message}",
                }[event]

            worker = voucher_print_service.start_print_pdf_background(
                self.pdf_bytes,
                self,
                job_name="preview",
                source_type="preview",
                selected_count=1,
                generated_pdf_count=1,
                merged_pdf_created=False,
            )
            voucher_print_service.log_preview_print_event("preview_print_worker_started")
            if worker is None:
                self._print_in_progress = False
                self.print_button.setEnabled(True)
                voucher_print_service.log_preview_print_event("preview_print_button_enabled")
                self._set_status(_status_for("request_sent"))
                return
            self._print_workers.add(worker)

            def _on_status_changed(message: str) -> None:
                self._set_status(message)

            def _on_request_sent(_payload: dict) -> None:
                # Popen成功＝印刷要求送信済み。終了確認は裏で継続するが、
                # ここで印刷中ガードを解除し次の印刷を止めない。
                self._print_in_progress = False
                self.print_button.setEnabled(True)
                voucher_print_service.log_preview_print_event("preview_print_request_sent_signal")
                voucher_print_service.log_preview_print_event("preview_print_button_enabled")
                self._set_status(_status_for("request_sent"))

            def _on_finished(_payload: dict) -> None:
                self._print_workers.discard(worker)
                self._print_in_progress = False
                self.print_button.setEnabled(True)
                voucher_print_service.log_preview_print_event("preview_print_finished_signal")
                self._set_status(_status_for("finished"))

            def _on_error(message: str, _payload: dict) -> None:
                # Popen前エラー（request_sent が来ない）でも error signal で必ず復帰させる。
                self._print_workers.discard(worker)
                self._print_in_progress = False
                self.print_button.setEnabled(True)
                voucher_print_service.log_preview_print_event(
                    "preview_print_failed_signal", error=str(message)
                )
                voucher_print_service.log_print_recovery_event(
                    "ui_error_received",
                    trigger="error",
                    source="preview",
                    ui_error_received=True,
                    ui_print_guard_released=True,
                    ui_button_enabled=True,
                    error_message=str(message),
                )
                # モーダルダイアログを出すとプレビューが固まるため、ステータス表示のみ。
                self._set_status(_status_for("error", str(message)))

            worker.status_changed.connect(_on_status_changed)
            worker.request_sent.connect(_on_request_sent)
            worker.finished.connect(_on_finished)
            worker.error.connect(_on_error)
            self._print_in_progress = False
            self.print_button.setEnabled(True)
            voucher_print_service.log_preview_print_event("preview_print_job_enqueued")
            self._set_status(_status_for("enqueued"))
        except Exception as exc:  # 印刷失敗はプレビューを閉じずにステータス表示のみで通知する。
            self._print_in_progress = False
            self.print_button.setEnabled(True)
            voucher_print_service.log_preview_print_event(
                "preview_print_failed_signal", error=str(exc)
            )
            self._set_status(f"印刷失敗: {exc}")

    def closeEvent(self, event) -> None:
        for worker in list(getattr(self, "_print_workers", set())):
            try:
                worker.cancel()
            except Exception:
                pass
        super().closeEvent(event)

    def _on_zoom_in(self) -> None:
        self._set_zoom(self._zoom + _ZOOM_STEP)

    def _on_zoom_out(self) -> None:
        self._set_zoom(self._zoom - _ZOOM_STEP)

    def _on_zoom_reset(self) -> None:
        self._set_zoom(1.0)

    def _set_zoom(self, zoom: float) -> None:
        zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        if self._base_pixmaps:
            self._apply_zoom()


def _render_with_pymupdf(pdf_bytes: bytes) -> list[QPixmap]:
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []
    pixmaps: list[QPixmap] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        matrix = fitz.Matrix(_BASE_RENDER_ZOOM, _BASE_RENDER_ZOOM)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            pixmaps.append(QPixmap.fromImage(image.copy()))
        doc.close()
    except Exception:
        _LOGGER.exception("PyMuPDFによるプレビュー描画に失敗しました。")
        return []
    return pixmaps


def _render_with_qpdf(pdf_bytes: bytes) -> list[QPixmap]:
    try:
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtCore import QBuffer, QByteArray, QSizeF
    except Exception:
        return []
    pixmaps: list[QPixmap] = []
    try:
        buffer = QBuffer()
        buffer.setData(QByteArray(pdf_bytes))
        buffer.open(QBuffer.OpenModeFlag.ReadOnly)
        doc = QPdfDocument(None)
        doc.load(buffer)
        for i in range(doc.pageCount()):
            size_pt = doc.pagePointSize(i)
            render_size = QSizeF(
                size_pt.width() * _BASE_RENDER_ZOOM,
                size_pt.height() * _BASE_RENDER_ZOOM,
            ).toSize()
            image = doc.render(i, render_size)
            if not image.isNull():
                pixmaps.append(QPixmap.fromImage(image))
        doc.close()
    except Exception:
        _LOGGER.exception("QPdfDocumentによるプレビュー描画に失敗しました。")
        return []
    return pixmaps
