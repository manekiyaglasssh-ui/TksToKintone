"""伝票印刷サービス。

QPrintDialog でプリンターを選択し、QPdfDocument + QPainter で印刷する。
プリンター選択ダイアログで直接印刷を制御する。
"""
from __future__ import annotations

import os
import tempfile
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

_LOGGER = logging.getLogger(__name__)


def print_pdf_with_dialog(pdf_bytes: bytes, parent: "QWidget | None" = None) -> bool:
    """プリンター選択ダイアログを表示してPDFを印刷する。

    Args:
        pdf_bytes: 印刷するPDFのバイト列。
        parent: ダイアログの親ウィジェット。

    Returns:
        True: 印刷を実行した。False: ユーザーがキャンセルした。

    Raises:
        RuntimeError: 印刷処理に失敗した場合。
    """
    from PySide6.QtPrintSupport import QPrinter, QPrintDialog

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialog = QPrintDialog(printer, parent)
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return False

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

    return True


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
            painter.drawImage(0, 0, image)
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
            painter.drawImage(0, 0, image.copy())
    finally:
        painter.end()
        pdf.close()
