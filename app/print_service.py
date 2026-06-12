"""Printing service for registration preview slips (A4 landscape, monochrome).

Fixed-block layout (all dimensions in mm, A4 landscape = 297 × 210):

  MY=8  ┌──────────────────────────────────────────┬─────────────┐  HEADER_Y
        │ 加工指図書      まねきや硝子株式会社       │  QRコード   │
        │ 得意先コード：                             │             │
        │ 得意先名称：                               │ 受注No      │
        │ 受注No：    仕上日：    出荷区分：          │ ページ X/Y  │
        └──────────────────────────────────────────┴─────────────┘  HEADER_BOT=50
  GAP=3
        ┌─────────────────────────────────────────────────────────┐  TABLE_Y=53
        │ No │ 商品コード │ 商品名称 │ 掛率 │ 加工名 │ W │ H │ 硝子 │㎡│総重量│
        │ .. │            │          │   │   │      │      │  │    │
        └──────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QImage, QPainter, QPen, QPageLayout, QPageSize
from PySide6.QtWidgets import QMessageBox, QWidget

try:
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter

    _PRINT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    _PRINT_AVAILABLE = False

try:
    import qrcode
    import qrcode.constants

    _QR_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    _QR_AVAILABLE = False

from app.print_layout import (
    DETAIL_COLUMN_DEFS,
    PRINT_TITLE,
    ROWS_PER_LAST,
    ROWS_PER_MID,
    split_pages as _split_pages,
)

# ── Public API ────────────────────────────────────────────────────────────────

def print_order_slips(
    parent: QWidget,
    all_rows: list[dict[str, str]],
    order_values: dict[str, dict[str, str]],
) -> None:
    """Print one or more pages per 受注No, paginating long detail lists."""
    if not _PRINT_AVAILABLE:
        QMessageBox.warning(
            parent, "印刷エラー",
            "印刷機能が利用できません（PySide6.QtPrintSupport が必要です）。",
        )
        return

    # Group rows by 受注No (preserve insertion order)
    rows_by_order: dict[str, list[dict[str, str]]] = {}
    for row in all_rows:
        rows_by_order.setdefault(row.get("受注No", ""), []).append(row)

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPageOrientation(QPageLayout.Orientation.Landscape)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))

    dlg = QPrintDialog(printer, parent)
    if dlg.exec() != QPrintDialog.DialogCode.Accepted:
        return

    painter = QPainter()
    if not painter.begin(printer):
        QMessageBox.critical(parent, "印刷エラー", "印刷を開始できませんでした。")
        return

    try:
        # Compute rows-per-page from the actual device viewport (real device pixels).
        # This adapts to the printer's true resolution and page dimensions.
        rows_per_mid, rows_per_last = _compute_rows_per_page(painter, printer)
        import logging as _logging
        _logging.getLogger(__name__).info(
            "印刷レイアウト: rows_per_mid=%d, rows_per_last=%d", rows_per_mid, rows_per_last
        )

        first_page = True
        for order_no, rows in rows_by_order.items():
            values      = order_values.get(order_no, {})
            page_chunks = _split_pages(rows, rows_per_mid, rows_per_last)
            total_pages = len(page_chunks)

            # Compute absolute starting row index for each chunk.
            row_offsets: list[int] = []
            cumulative = 0
            for chunk in page_chunks:
                row_offsets.append(cumulative)
                cumulative += len(chunk)

            for page_idx, (chunk, row_offset) in enumerate(zip(page_chunks, row_offsets)):
                if not first_page:
                    printer.newPage()
                first_page = False

                _draw_slip(
                    painter, printer, order_no, chunk, values,
                    page_num=page_idx + 1,
                    total_pages=total_pages,
                    is_last_page=(page_idx == total_pages - 1),
                    row_offset=row_offset,
                )
    except Exception as exc:
        painter.end()
        QMessageBox.critical(parent, "印刷エラー", f"印刷中にエラーが発生しました:\n{exc}")
        return

    painter.end()


# ── Layout computation ────────────────────────────────────────────────────────

def _compute_rows_per_page(painter: "QPainter", printer: "QPrinter") -> tuple[int, int]:
    """Return (rows_per_mid, rows_per_last) computed from the actual device viewport.

    Uses real device-pixel dimensions obtained after painter.begin() so the result
    adapts to the printer's true resolution and page size.

    Coordinates match _draw_slip geometry exactly:
      table_start  = MY(8) + HEADER_H(42) + GAP(3) + TBL_HDR_H(8) = 61 mm
      mid  TABLE_BOTTOM = ph - BOTTOM_MARGIN(5) - TBL_ROW_H  → room for "（つづく）"
      last TABLE_BOTTOM = ph - BOTTOM_MARGIN(5)
    """
    dpi = printer.resolution()

    def px(mm_val: float) -> int:
        return int(mm_val * dpi / 25.4)

    page = painter.viewport()
    ph = page.height()

    table_start   = px(8) + px(42) + px(3) + px(8)  # MY + HEADER_H + GAP + TBL_HDR_H = 61 mm
    row_h         = px(4.5)
    bottom_margin = px(5)

    # Mid page: bottom_margin(5mm) + one row reserved for "（つづく）" text
    mid_bottom  = ph - bottom_margin - row_h
    # Last page: no footer block.
    last_bottom = ph - bottom_margin

    rows_per_mid  = max(1, (mid_bottom  - table_start) // row_h)
    rows_per_last = max(1, (last_bottom - table_start) // row_h)
    return rows_per_mid, rows_per_last


# ── Single-page drawing ───────────────────────────────────────────────────────

def _draw_slip(
    painter: QPainter,
    printer: "QPrinter",
    order_no: str,
    rows: list[dict[str, str]],
    values: dict[str, str],
    page_num: int = 1,
    total_pages: int = 1,
    is_last_page: bool = True,
    row_offset: int = 0,
) -> None:
    dpi = printer.resolution()

    def mm(v: float) -> int:
        return int(v * dpi / 25.4)

    page = painter.viewport()
    pw, ph = page.width(), page.height()

    # ── Fixed geometry (device pixels) ────────────────────────────────────────
    MX        = mm(10)
    MY        = mm(8)
    CW        = pw - 2 * MX
    HEADER_H  = mm(42)
    BLOCK_GAP = mm(3)
    QR_AREA_W = mm(34)
    INFO_W    = CW - QR_AREA_W
    TBL_HDR_H = mm(8)
    TBL_ROW_H = mm(4.5)   # compact rows: must match print_layout._TBL_ROW_H_MM

    HEADER_Y   = MY
    HEADER_BOT = HEADER_Y + HEADER_H
    TABLE_Y    = HEADER_BOT + BLOCK_GAP

    # TABLE_BOTTOM matches _compute_rows_per_page formulas exactly (5 mm physical bottom margin).
    # Mid pages:  reserve BOTTOM_MARGIN(5mm) + one row for "（つづく）" text.
    # Last pages: no footer block.
    BOTTOM_MARGIN = mm(5)
    if is_last_page:
        TABLE_BOTTOM = ph - BOTTOM_MARGIN
    else:
        TABLE_BOTTOM = ph - BOTTOM_MARGIN - TBL_ROW_H

    # ── Source data ───────────────────────────────────────────────────────────
    first          = rows[0] if rows else {}
    shiage         = values.get("仕上日",   first.get("仕上日",   ""))
    shukka         = values.get("出荷区分", first.get("出荷区分", ""))
    tokuisaki_code = first.get("得意先コード", "")
    tokuisaki_name = first.get("得意先名称",  "")

    # ── Unified font sizes ────────────────────────────────────────────────────
    F_TITLE = mm(7.5)
    F_BODY  = mm(3.5)
    F_TABLE = mm(3.0)

    # ── HEADER BLOCK ─────────────────────────────────────────────────────────
    painter.drawRect(QRect(MX, HEADER_Y, CW, HEADER_H))
    qr_div_x = MX + INFO_W
    painter.drawLine(qr_div_x, HEADER_Y, qr_div_x, HEADER_BOT)

    # Title row (10 mm)
    TITLE_H   = mm(10)
    TITLE_Y   = HEADER_Y + mm(1)
    TITLE_PAD = mm(3)
    _font(painter, F_TITLE, bold=True)
    painter.drawText(
        _rect(MX + TITLE_PAD, TITLE_Y, INFO_W - 2 * TITLE_PAD, TITLE_H),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, PRINT_TITLE,
    )
    _font(painter, F_BODY)
    painter.drawText(
        _rect(MX + TITLE_PAD, TITLE_Y, INFO_W - 2 * TITLE_PAD, TITLE_H),
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "まねきや硝子株式会社",
    )
    TITLE_SEP_Y = TITLE_Y + TITLE_H + mm(1)
    painter.drawLine(MX, TITLE_SEP_Y, MX + INFO_W, TITLE_SEP_Y)

    # Info rows (4 rows × 7 mm)
    ROW_H = mm(7)
    IY    = TITLE_SEP_Y + mm(1)
    IX    = MX + TITLE_PAD
    ITW   = INFO_W - 2 * TITLE_PAD
    _font(painter, F_BODY)

    painter.drawText(_rect(IX, IY, ITW, ROW_H), _LEFT_V, f"得意先コード：{tokuisaki_code}")
    IY += ROW_H
    painter.drawText(_rect(IX, IY, ITW, ROW_H), _LEFT_V, f"得意先名称：{tokuisaki_name}")
    IY += ROW_H
    sub_w = ITW // 3
    painter.drawText(_rect(IX,           IY, sub_w, ROW_H), _LEFT_V, f"受注No：{order_no}")
    painter.drawText(_rect(IX + sub_w,   IY, sub_w, ROW_H), _LEFT_V, f"仕上日：{shiage}")
    painter.drawText(_rect(IX + 2*sub_w, IY, sub_w, ROW_H), _LEFT_V, f"出荷区分：{shukka}")

    # ── QR CODE ───────────────────────────────────────────────────────────────
    QR_SIZE = mm(24)
    qr_x    = qr_div_x + (QR_AREA_W - QR_SIZE) // 2
    qr_y    = HEADER_Y + mm(4)

    qr_img = _make_qr_image(order_no)
    _font(painter, F_TABLE)
    if qr_img is not None:
        painter.drawImage(QRect(qr_x, qr_y, QR_SIZE, QR_SIZE), qr_img)
        painter.drawText(
            _rect(qr_div_x + mm(1), qr_y + QR_SIZE + mm(1), QR_AREA_W - mm(2), mm(4)),
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
            f"受注No: {order_no}",
        )
    else:
        painter.drawText(
            _rect(qr_div_x + mm(1), qr_y, QR_AREA_W - mm(2), mm(10)),
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
            "QR生成失敗",
        )

    # Page number (shown in QR area when multiple pages)
    if total_pages > 1:
        _font(painter, F_TABLE)
        painter.drawText(
            _rect(qr_div_x + mm(1), qr_y + QR_SIZE + mm(5.5), QR_AREA_W - mm(2), mm(4)),
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
            f"ページ {page_num} / {total_pages}",
        )

    # ── DETAIL TABLE ──────────────────────────────────────────────────────────
    col_defs = DETAIL_COLUMN_DEFS
    total_rel = sum(c[2] for c in col_defs)
    col_pxs   = [int(c[2] / total_rel * CW) for c in col_defs]
    col_pxs[-1] = CW - sum(col_pxs[:-1])

    # Header row
    _font(painter, F_TABLE, bold=True)
    x = MX
    for (label, _, _), cpx in zip(col_defs, col_pxs):
        r = QRect(x, TABLE_Y, cpx, TBL_HDR_H)
        painter.drawRect(r)
        painter.drawText(r.adjusted(1, 0, -1, 0), Qt.AlignmentFlag.AlignCenter, label)
        x += cpx

    # Data rows
    _font(painter, F_TABLE)
    y         = TABLE_Y + TBL_HDR_H
    truncated = False
    for row_i, row in enumerate(rows):
        if y + TBL_ROW_H > TABLE_BOTTOM:
            truncated = True
            break
        x = MX
        for (_, key, _), cpx in zip(col_defs, col_pxs):
            r     = QRect(x, y, cpx, TBL_ROW_H)
            inner = r.adjusted(2, 0, -2, 0)
            painter.drawRect(r)
            if key == "":
                # Absolute row number across all pages
                painter.drawText(inner, Qt.AlignmentFlag.AlignCenter,
                                 str(row_offset + row_i + 1))
            elif key == "掛率集計名称":
                # Show only when 硝/加工 == "2" (加工 row)
                value = row.get(key, "") if row.get("硝/加工", "") == "2" else ""
                painter.drawText(inner, _LEFT_V, value)
            elif key in {"商品名称", "加工名"}:
                painter.drawText(inner, _LEFT_V, row.get(key, ""))
            else:
                painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, row.get(key, ""))
            x += cpx
        y += TBL_ROW_H

    # Continuation / truncation note
    if truncated:
        _font(painter, F_TABLE)
        painter.drawText(
            _rect(MX + mm(2), y, CW - mm(4), TBL_ROW_H),
            _LEFT_V, "※明細が多いため一部省略",
        )
    elif not is_last_page:
        _font(painter, F_TABLE)
        painter.drawText(
            _rect(MX + mm(2), y, CW - mm(4), TBL_ROW_H),
            _LEFT_V, "（つづく）",
        )


# ── QR helper ─────────────────────────────────────────────────────────────────

def _make_qr_image(text: str) -> QImage | None:
    """Return a QImage of the QR code for *text*, or None on failure."""
    if not _QR_AVAILABLE:
        return None
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        pil_img = qr.make_image(fill_color="black", back_color="white")
        pil_rgb = pil_img.convert("RGB")
        w, h    = pil_rgb.size
        raw     = pil_rgb.tobytes("raw", "RGB")
        qimage  = QImage(raw, w, h, w * 3, QImage.Format.Format_RGB888)
        return qimage.copy()
    except Exception:
        return None


# ── Tiny helpers ───────────────────────────────────────────────────────────────

_LEFT_V = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def _font(painter: QPainter, pixel_size: int, bold: bool = False) -> None:
    f = QFont()
    f.setPixelSize(max(pixel_size, 1))
    f.setBold(bold)
    painter.setFont(f)


def _rect(x: int, y: int, w: int, h: int) -> QRect:
    return QRect(x, y, w, h)
