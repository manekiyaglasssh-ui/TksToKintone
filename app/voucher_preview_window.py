"""伝票印刷プレビュー画面（アプリ内表示）。

PDFバイト列をメモリ上で受け取り、PyMuPDF（無ければ QPdfDocument）で各ページを
画像化して縦並びに表示する。一時PDFファイルや正式PDFは一切保存しない。
プレビュー画面の「印刷」ボタンからは、既存の印刷処理（プリンター選択ダイアログ）
を同じPDFバイト列で呼び出す。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

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

    def __init__(self, pdf_bytes: bytes, parent: "_QWidget | None" = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.pdf_bytes = bytes(pdf_bytes or b"")
        self._zoom = 1.0
        self._page_labels: list[QLabel] = []

        self.setWindowTitle("印刷プレビュー")
        self.resize(900, 1000)
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
    def _on_print(self) -> None:
        from app import voucher_print_service

        try:
            voucher_print_service.print_pdf_with_dialog(self.pdf_bytes, self)
        except Exception as exc:  # 印刷失敗はプレビューを閉じずに通知する。
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(self, "印刷エラー", f"印刷中にエラーが発生しました:\n{exc}")

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
