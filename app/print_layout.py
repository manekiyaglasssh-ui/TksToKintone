"""Pure-Python pagination helpers for print_service (no PySide6 dependency).

All values are derived from the fixed block layout defined in print_service._draw_slip.
Keep these constants in sync whenever the layout changes.
"""
from __future__ import annotations

# ── Fixed layout values (mm, A4 landscape 297 × 210) ─────────────────────────
_PAGE_H_MM      = 210.0
_HEADER_H_MM    = 42.0
_BLOCK_GAP_MM   = 3.0
_TBL_HDR_H_MM   = 8.0
_TBL_ROW_H_MM   = 4.5
_MY_MM          = 8.0    # top margin (kept for header placement)
_BOTTOM_MARGIN_MM = 5.0  # physical bottom margin for all page types

PRINT_TITLE = "加工指図書"
DETAIL_COLUMN_DEFS = [
    ("No",        "",             8),
    ("商品コード", "商品コード",    24),
    ("商品名称",  "商品名称",      68),
    ("掛率集計名称", "掛率集計名称", 32),
    ("加工名",    "加工名",        32),
    ("W寸法",     "W寸法",         16),
    ("H寸法",     "H寸法",         16),
    ("硝子枚数",  "硝子枚数",      18),
    ("㎡",        "㎡",            16),
    ("総重量",    "総重量",        20),
]

# These formulas mirror the TABLE_BOTTOM expressions in print_service._draw_slip:
#   mid page:  TABLE_BOTTOM = ph - BOTTOM_MARGIN - TBL_ROW_H  (room for "（つづく）")
#   last page: TABLE_BOTTOM = ph - BOTTOM_MARGIN

_TABLE_Y_MM     = _MY_MM + _HEADER_H_MM + _BLOCK_GAP_MM   # = 53 mm
_TABLE_START_MM = _TABLE_Y_MM + _TBL_HDR_H_MM             # = 61 mm

# TABLE_BOTTOM in mm for each page type (matches _draw_slip)
_TABLE_BOTTOM_MID_MM  = _PAGE_H_MM - _BOTTOM_MARGIN_MM - _TBL_ROW_H_MM          # = 200.5 mm
_TABLE_BOTTOM_LAST_MM = _PAGE_H_MM - _BOTTOM_MARGIN_MM                         # = 205.0 mm

# Fallback constants (used when device pixel computation is not available, e.g. tests)
ROWS_PER_MID: int = max(1, int(
    (_TABLE_BOTTOM_MID_MM  - _TABLE_START_MM) / _TBL_ROW_H_MM
))  # = 31

ROWS_PER_LAST: int = max(1, int(
    (_TABLE_BOTTOM_LAST_MM - _TABLE_START_MM) / _TBL_ROW_H_MM
))  # = 32


def split_pages(
    rows: list[dict[str, str]],
    rows_per_mid: int = ROWS_PER_MID,
    rows_per_last: int = ROWS_PER_LAST,
) -> list[list[dict[str, str]]]:
    """Split *rows* into page chunks: fill middle pages first, balance last two.

    Algorithm
    ---------
    1. Fill middle pages with *rows_per_mid* rows until the last page can hold
       the remainder.
    2. The remaining rows become the last page.
    3. If the final page ends up very sparse, the last two pages are
       redistributed evenly.
    """
    if not rows:
        return [list(rows)]
    n = len(rows)
    if n <= rows_per_last:
        return [list(rows)]

    pages: list[list[dict[str, str]]] = []
    i = 0

    # Fill middle pages – each takes min(rows_per_mid, room_before_last_page)
    while n - i > rows_per_last:
        take = min(rows_per_mid, (n - i) - rows_per_last)
        pages.append(rows[i:i + take])
        i += take

    pages.append(rows[i:])  # last page

    # Balance sparse final pages after the footer was removed and last-page
    # capacity became larger than the middle-page capacity.
    if len(pages) >= 2 and len(pages[-1]) < max(2, rows_per_mid // 2):
        combined = pages[-2] + pages[-1]
        total = len(combined)
        new_penult = min((total + 1) // 2, rows_per_mid)
        new_last = total - new_penult
        if 1 <= new_last <= rows_per_last:
            pages[-2] = combined[:new_penult]
            pages[-1] = combined[new_penult:]

    return pages
