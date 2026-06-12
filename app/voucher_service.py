"""伝票PDF作成サービス。

売上伝票(01), 工場控(02), 指図書系(03-06), 納品書/受領書(07-08):
reportlab でフォームを一から描画（アプリ描画方式）。

公開 API:
    create_vouchers_pdf(...)   - ファイル保存あり（PDF作成ボタン用）
    build_vouchers_pdf_bytes(...)  - ファイル保存なし（印刷ボタン用）
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader

import pypdf

from app.config import resource_path
from app.path_utils import ensure_voucher_output_dir, get_default_voucher_output_dir
from app.voucher_data_mapper import build_qr_code_image
from app.voucher_templates import (
    DUMMY_DATA,
    PAGE_W, PAGE_H,
    VOUCHER_NAMES,
    template_path,
    # アプリ描画方式 レイアウト定数
    FORM_ML, FORM_MR, FORM_MB, CORNER_R,
    FORM_TITLE_X, FORM_TITLE_Y, FORM_TITLE_UL_Y, FORM_TITLE_UL_HALF,
    GEN_CIRCLE_X, NOKI_LINE_X, STAMP_X, STAMP_W, STAMP_H, STAMP_GAP,
    COMPANY_LOGO_H, COMPANY_LOGO_W, COMPANY_LOGO_X, COMPANY_NAME_Y, COMPANY_INFO_X,
    FORM_HDR_TOP, FORM_HDR_MID, FORM_HDR_BOT, FORM_HDR_LEFT, FORM_HDR_RIGHT,
    HDR_ROW1_DIVS, HDR_ROW2_DIVS,
    HDR_SHIAGE_LABEL_Y, HDR_SHIAGE_MONTH_DAY_Y,
    FORM_DETAIL_ROWS, FORM_DETAIL_ROW_H, FORM_TBL_HDR_BOT,
    FORM_DETAIL_BOT, FORM_TOTAL_ROW_H, FORM_TOTAL_BOT,
    FORM_TOTAL_CELL_LEFT, FORM_TOTAL_CELL_RIGHT,
    TBL_COLS, TBL_COL_LABELS,
    SHIZU_TBL_COLS, SHIZU_COL_LABELS, SHIZU_MAX_W_NYUKI,
    DATA_X_PAD, HDR_DATA_Y_INNER, DET_UPPER_OFFSET, DET_LOWER_OFFSET,
    TBL_X_NAME, TBL_X_QTY, TBL_X_UNIT, TBL_X_AMT, TBL_X_NOTE,
    TBL_MAX_NAME, TBL_MAX_QTY, TBL_MAX_UNIT, TBL_MAX_AMT, TBL_MAX_NOTE,
    DET_NAME_RX, DET_QTY_RX, FS_DIM_LARGE, DIM_SHIFT_LEFT,
    TBL_NOTE_MID_X, TBL_NOTE_MID_PAD,
    SUM_STAFF_X,
    FORM_SUM_GAP, FORM_SUM_TOP, FORM_SUM_BOT,
    FORM_BKNO_TOP, FORM_BKNO_BOT,
    FORM_SUM_RIGHT, FORM_SUBROW_LBL_W,
    TAX_NOTICE, TAX_Y,
    FORM_LWR_TOP, FORM_LWR_BOT, FORM_CHK_RIGHT, FORM_RGHT_LEFT,
    FORM_LWR_LEFT, FORM_LWR_RIGHT, FORM_CUT_LEFT, FORM_CUT_TOP, FORM_CUT_BOT,
    PROC_LABELS,
    # オーバーレイ方式 座標定数
    HEADER_ROW1_Y, HEADER_ROW2_Y,
    HDR1_CODE_NO_X, HDR1_CUSTOMER_X, HDR1_CUSTOMER_MAX,
    HDR1_ORDER_NO_X, HDR1_SHIAGE_X,
    HDR2_ISSUE_DATE_X, HDR2_DELIVERY_X, HDR2_VOUCHER_NO_X,
    HDR2_TRADE_TYPE_X, HDR2_SHIP_TYPE_X, HDR2_OPERATOR_X, HDR2_OPERATOR_MAX,
    DETAIL_ROW1_TOP, DETAIL_ROW_H, DETAIL_UPPER_OFFSET, DETAIL_LOWER_OFFSET,
    COL_NAME_X, COL_QTY_X, COL_UNIT_X, COL_AMOUNT_X, COL_NOTE_X,
    MAX_W_NAME, MAX_W_QTY, MAX_W_UNIT, MAX_W_AMOUNT, MAX_W_NOTE,
    FS_HEADER, FS_DETAIL, FS_DIMS, FS_NOTE,
)

_FONT_NAME = "HeiseiKakuGo-W5"
_FONT_REGISTERED = False
DELIVERY_MASK_RGB = (0.7, 0.7, 0.7)
_log = logging.getLogger("tks_to_kintone_app")


def _ensure_font() -> None:
    global _FONT_REGISTERED
    if not _FONT_REGISTERED:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))
        _FONT_REGISTERED = True


def _ensure_edit_text_font() -> str:
    """指図書編集オブジェクト用の日本語フォント名を返す。"""
    _ensure_font()
    return _FONT_NAME


def _clean_display_text(value: Any) -> str:
    return str(value or "").strip().strip("\u3000")


def _summary_lines(data: dict[str, Any]) -> list[str]:
    if "summary_line1" in data or "summary_line2" in data:
        return [
            _clean_display_text(data.get("summary_line1")),
            _clean_display_text(data.get("summary_line2")),
        ]
    lines = data.get("summary_lines", [])
    if not isinstance(lines, list):
        lines = []
    return [_clean_display_text(line) for line in (list(lines) + ["", ""])[:2]]


def _summary_line_y(index: int) -> float:
    return FORM_SUM_BOT + 12.0 - index * 9.0


def _draw_summary_lines(c: rl_canvas.Canvas, data: dict[str, Any], fs: float) -> None:
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    for index, line in enumerate(_summary_lines(data)):
        if not line:
            continue
        _str(c, str(line), note_line_x, _summary_line_y(index), fs,
             max_w=FORM_SUM_RIGHT - note_line_x)


def _scene_rect_to_pdf_rect(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    return x, PAGE_H - y - h, w, h


def _scene_point_to_pdf_point(x: float, y: float) -> tuple[float, float]:
    return x, PAGE_H - y


def _scene_line_to_pdf(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
    pdf_x1, pdf_y1 = _scene_point_to_pdf_point(x1, y1)
    pdf_x2, pdf_y2 = _scene_point_to_pdf_point(x2, y2)
    return pdf_x1, pdf_y1, pdf_x2, pdf_y2


# 旧テスト/外部参照互換。
_scene_rect_to_pdf = _scene_rect_to_pdf_rect


# ── テキストユーティリティ ────────────────────────────────────────────────────

def _extract_note_number(s: str) -> float:
    """摘要列文字列（例: '1,580 加'）から先頭の数値を返す。"""
    m = re.match(r'^\s*([\d,]+)', s)
    if m:
        return float(m.group(1).replace(',', ''))
    return 0.0


def _is_date_str(s: str) -> bool:
    """文字列が日付形式（例: 06/19）かどうかを判定する。"""
    return bool(re.match(r'^\d{2}/\d{2}$', s.strip()))


def _split_note(s: str) -> tuple[str, str]:
    """摘要文字列 '数字 テキスト' を (数字部, テキスト部) に分割する。"""
    m = re.match(r'^\s*([\d,]+)\s*(.*)', s)
    if m:
        return m.group(1), m.group(2).strip()
    return s.strip(), ""


def _split_note_rows(s: str) -> list[tuple[str, str]]:
    """摘要文字列を表示段ごとの (数字部, テキスト部) に分割する。"""
    first_num, rest = _split_note(s)
    m = re.match(r'^/\s*([\d,]+)\s*(.*)', rest)
    if m:
        return [(first_num, ""), (m.group(1), m.group(2).strip())]
    return [(first_num, rest)]


def _clip(c: rl_canvas.Canvas, text: str, max_w: float, fs: float) -> str:
    c.setFont(_FONT_NAME, fs)
    while text and c.stringWidth(text, _FONT_NAME, fs) > max_w:
        text = text[:-1]
    return text


def _str(c: rl_canvas.Canvas, text: str, x: float, y: float, fs: float,
         max_w: float | None = None) -> None:
    if not text:
        return
    c.setFont(_FONT_NAME, fs)
    if max_w:
        text = _clip(c, text, max_w, fs)
    c.drawString(x, y, text)


def _cstr(c: rl_canvas.Canvas, text: str, cx: float, y: float, fs: float,
          max_w: float | None = None) -> None:
    """中央揃えで描画する。cx はカラム中心X。"""
    if not text:
        return
    c.setFont(_FONT_NAME, fs)
    if max_w:
        text = _clip(c, text, max_w, fs)
    c.drawCentredString(cx, y, text)


def _rstr(c: rl_canvas.Canvas, text: str, rx: float, y: float, fs: float,
          max_w: float | None = None) -> None:
    """右揃えで描画する。rx はカラム右端X。"""
    if not text:
        return
    c.setFont(_FONT_NAME, fs)
    if max_w:
        text = _clip(c, text, max_w, fs)
    c.drawRightString(rx, y, text)


def _top_round_rect_path(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float,
                         r: float) -> Any:
    """上角だけ角丸、下角は直角の矩形パスを返す。"""
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x, y + h - r)
    p.curveTo(x, y + h - r / 2, x + r / 2, y + h, x + r, y + h)
    p.lineTo(x + w - r, y + h)
    p.curveTo(x + w - r / 2, y + h, x + w, y + h - r / 2, x + w, y + h - r)
    p.lineTo(x + w, y)
    p.lineTo(x, y)
    return p


def _bottom_left_round_rect_path(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float,
                                 r: float) -> Any:
    """左下だけ角丸の矩形パスを返す。"""
    p = c.beginPath()
    p.moveTo(x + r, y)
    p.lineTo(x + w, y)
    p.lineTo(x + w, y + h)
    p.lineTo(x, y + h)
    p.lineTo(x, y + r)
    p.curveTo(x, y + r / 2, x + r / 2, y, x + r, y)
    p.close()
    return p


def _draw_detail_outline(c: rl_canvas.Canvas, left: float, right: float) -> None:
    """明細7行目左下と合計行右下だけ角丸にした表外枠を描く。"""
    top = FORM_HDR_BOT
    detail_bot = FORM_DETAIL_BOT
    total_bot = FORM_TOTAL_BOT
    r = CORNER_R

    p = c.beginPath()
    p.moveTo(left, detail_bot + r)
    p.lineTo(left, top - r)
    p.curveTo(left, top - r / 2, left + r / 2, top, left + r, top)
    p.lineTo(right - r, top)
    p.curveTo(right - r / 2, top, right, top - r / 2, right, top - r)
    p.lineTo(right, total_bot + r)
    p.curveTo(right, total_bot + r / 2, right - r / 2, total_bot, right - r, total_bot)
    p.lineTo(FORM_TOTAL_CELL_LEFT + r, total_bot)
    p.curveTo(
        FORM_TOTAL_CELL_LEFT + r / 2,
        total_bot,
        FORM_TOTAL_CELL_LEFT,
        total_bot + r / 2,
        FORM_TOTAL_CELL_LEFT,
        total_bot + r,
    )
    p.lineTo(FORM_TOTAL_CELL_LEFT, detail_bot)
    p.lineTo(left + r, detail_bot)
    p.curveTo(left + r / 2, detail_bot, left, detail_bot + r / 2, left, detail_bot + r)
    c.drawPath(p, stroke=1, fill=0)


def _draw_delivery_07_outline(c: rl_canvas.Canvas, left: float, right: float,
                              total_right: float) -> None:
    """納品書表外枠。右端マスク列は合計行に含めない。"""
    top = FORM_HDR_BOT
    detail_bot = FORM_DETAIL_BOT
    total_bot = FORM_TOTAL_BOT
    r = CORNER_R

    p = c.beginPath()
    p.moveTo(left, detail_bot + r)
    p.lineTo(left, top - r)
    p.curveTo(left, top - r / 2, left + r / 2, top, left + r, top)
    p.lineTo(right - r, top)
    p.curveTo(right - r / 2, top, right, top - r / 2, right, top - r)
    p.lineTo(right, detail_bot + r)
    p.curveTo(right, detail_bot + r / 2, right - r / 2, detail_bot, right - r, detail_bot)
    p.lineTo(total_right, detail_bot)
    p.lineTo(total_right, total_bot + r)
    p.curveTo(total_right, total_bot + r / 2,
              total_right - r / 2, total_bot,
              total_right - r, total_bot)
    p.lineTo(FORM_TOTAL_CELL_LEFT + r, total_bot)
    p.curveTo(
        FORM_TOTAL_CELL_LEFT + r / 2,
        total_bot,
        FORM_TOTAL_CELL_LEFT,
        total_bot + r / 2,
        FORM_TOTAL_CELL_LEFT,
        total_bot + r,
    )
    p.lineTo(FORM_TOTAL_CELL_LEFT, detail_bot)
    p.lineTo(left + r, detail_bot)
    p.curveTo(left + r / 2, detail_bot, left, detail_bot + r / 2, left, detail_bot + r)
    c.drawPath(p, stroke=1, fill=0)


def _cut_date_box_path(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float,
                       r: float) -> Any:
    """切断仕上日欄。左上・右下は直角、右上・左下だけ角丸にする。"""
    p = c.beginPath()
    p.moveTo(x, y + h)
    p.lineTo(x + w - r, y + h)
    p.curveTo(x + w - r / 2, y + h, x + w, y + h - r / 2, x + w, y + h - r)
    p.lineTo(x + w, y)
    p.lineTo(x + r, y)
    p.curveTo(x + r / 2, y, x, y + r / 2, x, y + r)
    p.lineTo(x, y + h)
    return p


# ══════════════════════════════════════════════════════════════════════════════
# アプリ描画方式: 売上伝票(01) を reportlab で一から描画
# ══════════════════════════════════════════════════════════════════════════════

def _draw_form_01(c: rl_canvas.Canvas, data: dict[str, Any],
                  title: str = "売　上　伝　票") -> None:
    """売上伝票フォームを一から描画してデータも印字する。"""
    _draw_form_structure_01(c, title)
    _draw_form_data_01(c, data)


def _draw_form_structure_01(c: rl_canvas.Canvas,
                             title: str = "売　上　伝　票") -> None:
    """罫線・ラベル・固定テキストなどフォームの骨格を描画する。"""
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)

    # ─── タイトル ─────────────────────────────────────────────────────────────
    c.setFont(_FONT_NAME, 16)
    c.drawCentredString(FORM_TITLE_X, FORM_TITLE_Y, title)
    ul_half = FORM_TITLE_UL_HALF   # 全伝票共通固定幅
    c.setLineWidth(1.0)
    c.line(FORM_TITLE_X - ul_half, FORM_TITLE_UL_Y,
           FORM_TITLE_X + ul_half, FORM_TITLE_UL_Y)

    # ─── 会社名（表ヘッダー上・ヘッダー枠右側）──────────────────────────────────
    logo_y = COMPANY_NAME_Y + 5.2 - COMPANY_LOGO_H / 2
    c.drawImage(str(resource_path("assets/manekiya_logo.png")), COMPANY_LOGO_X, logo_y,
                width=COMPANY_LOGO_W, height=COMPANY_LOGO_H, mask="auto")
    c.setFont(_FONT_NAME, 16)
    c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y, "まねきや硝子株式会社")

    # ─── ヘッダー枠（角丸・左部分）────────────────────────────────────────────
    c.setLineWidth(1.0)
    c.drawPath(
        _top_round_rect_path(
            c,
            FORM_HDR_LEFT,
            FORM_HDR_BOT,
            FORM_HDR_RIGHT - FORM_HDR_LEFT,
            FORM_HDR_TOP - FORM_HDR_BOT,
            CORNER_R,
        ),
        stroke=1,
        fill=0,
    )

    c.setLineWidth(0.5)
    c.line(FORM_HDR_LEFT, FORM_HDR_MID, FORM_HDR_RIGHT, FORM_HDR_MID)
    for x in HDR_ROW1_DIVS:
        c.line(x, FORM_HDR_MID, x, FORM_HDR_TOP)
    for x in HDR_ROW2_DIVS:
        c.line(x, FORM_HDR_BOT, x, FORM_HDR_MID)

    # ヘッダー列ラベル
    FS_LBL = 6.0

    def lbl(text: str, x: float, y: float) -> None:
        c.setFont(_FONT_NAME, FS_LBL)
        c.drawString(x + 1.5, y, text)

    r1_lbl_y = FORM_HDR_TOP - 8.0
    lbl("コードNo", FORM_HDR_LEFT, r1_lbl_y)
    lbl("得意先名", 90.0,          r1_lbl_y)
    lbl("受注No",   284.0,         r1_lbl_y)
    lbl("仕上日",   371.0,         HDR_SHIAGE_LABEL_Y)
    c.drawString(FORM_HDR_RIGHT - 28.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, "月")
    c.drawRightString(FORM_HDR_RIGHT - 4.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, "日")

    # 「殿」── 得意先名欄の右寄り。左罫線は描かない。
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(279.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "殿")

    r2_lbl_y = FORM_HDR_MID - 8.0
    lbl("発行日",   FORM_HDR_LEFT, r2_lbl_y)
    lbl("納品日",   90.0,          r2_lbl_y)
    lbl("伝票No",   145.0,         r2_lbl_y)
    lbl("取引区分", 197.0,         r2_lbl_y)
    lbl("出荷区分", 245.0,         r2_lbl_y)
    lbl("入力者名", 284.0,         r2_lbl_y)
    c.setFont(_FONT_NAME, 11.0)
    c.drawCentredString((371.0 + FORM_HDR_RIGHT) / 2, FORM_HDR_BOT + 8.0, "AM・PM")

    # ─── 明細テーブル外枠（角丸）──────────────────────────────────────────────
    table_left = TBL_COLS[0]
    table_right = TBL_COLS[-1]
    # テーブルヘッダー行（黒塗り・白文字）
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _top_round_rect_path(
            c,
            table_left,
            FORM_TBL_HDR_BOT,
            table_right - table_left,
            FORM_HDR_BOT - FORM_TBL_HDR_BOT,
            CORNER_R,
        ),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    tbl_hdr_cy = (FORM_HDR_BOT + FORM_TBL_HDR_BOT) / 2 - 4.0
    for x1, x2, label in zip(TBL_COLS, TBL_COLS[1:], TBL_COL_LABELS):
        c.drawCentredString((x1 + x2) / 2, tbl_hdr_cy, label)
    # 摘要列ヘッダ: 左半分「摘」・右半分「要」を中央揃えで描画
    c.drawCentredString((TBL_COLS[5] + TBL_NOTE_MID_X) / 2, tbl_hdr_cy, "摘")
    c.drawCentredString((TBL_NOTE_MID_X + TBL_COLS[6]) / 2, tbl_hdr_cy, "要")

    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)

    # 外枠と縦仕切り線。合計行の左側罫線は明細7行目下端で止める。
    c.setLineWidth(0.5)
    _draw_detail_outline(c, table_left, table_right)
    for x in TBL_COLS[1:-1]:
        bottom = FORM_DETAIL_BOT if x <= FORM_TOTAL_CELL_LEFT else FORM_TOTAL_BOT
        c.line(x, bottom, x, FORM_HDR_BOT)

    # 明細行間の水平線
    for i in range(1, FORM_DETAIL_ROWS):
        y = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        c.line(table_left, y, table_right, y)

    c.line(table_left, FORM_TBL_HDR_BOT, table_right, FORM_TBL_HDR_BOT)
    # 7行目下端 (左～合計セル左): 左側列の行7底辺
    c.line(table_left + CORNER_R, FORM_DETAIL_BOT, FORM_TOTAL_CELL_LEFT, FORM_DETAIL_BOT)
    # 7行目下端 (合計セル右～右端): 金額・摘要列の行7と合計行の区切り（issue 4）
    c.line(FORM_TOTAL_CELL_RIGHT, FORM_DETAIL_BOT, table_right, FORM_DETAIL_BOT)
    # 合計行底辺
    c.line(FORM_TOTAL_CELL_RIGHT, FORM_TOTAL_BOT, table_right - CORNER_R, FORM_TOTAL_BOT)

    # 行番号
    c.setFont(_FONT_NAME, 7.0)
    for i in range(FORM_DETAIL_ROWS):
        row_cy = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H - FORM_DETAIL_ROW_H / 2
        c.drawCentredString((table_left + TBL_COLS[1]) / 2, row_cy - 3.0, str(i + 1))

    # ─── 合計行（黒背景「合　計」セル）───────────────────────────────────────
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _bottom_left_round_rect_path(
            c,
            FORM_TOTAL_CELL_LEFT,
            FORM_TOTAL_BOT,
            FORM_TOTAL_CELL_RIGHT - FORM_TOTAL_CELL_LEFT,
            FORM_TOTAL_ROW_H,
            CORNER_R,
        ),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    c.drawCentredString(
        (FORM_TOTAL_CELL_LEFT + FORM_TOTAL_CELL_RIGHT) / 2,
        FORM_TOTAL_BOT + (FORM_TOTAL_ROW_H - 8) / 2,
        "合　計",
    )
    c.setFillColorRGB(0, 0, 0)

    # ─── 摘要 / 物件No（下線のみ）────────────────────────────────────────────
    c.setLineWidth(0.5)
    c.setFont(_FONT_NAME, 7.0)
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_SUM_BOT + 3.0, "摘　要")
    c.line(note_line_x, FORM_SUM_BOT, FORM_SUM_RIGHT, FORM_SUM_BOT)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_BKNO_BOT + 3.0, "物件No")
    c.line(note_line_x, FORM_BKNO_BOT, FORM_SUM_RIGHT, FORM_BKNO_BOT)

    # ─── 消費税注記 ────────────────────────────────────────────────────────────
    c.setFont(_FONT_NAME, 7.5)
    c.drawString(note_line_x, TAX_Y, TAX_NOTICE)

    # ─── 下部チェック欄・右側大枠 ────────────────────────────────────────────
    _draw_lower_section(c)


def _draw_lower_section(c: rl_canvas.Canvas) -> None:
    """縦リスト形式の加工チェック欄と右側大枠（切断仕上日入り）を描画する。"""
    ML    = FORM_LWR_LEFT
    CHK_R = FORM_CHK_RIGHT
    RGHT  = FORM_RGHT_LEFT
    MR    = FORM_LWR_RIGHT
    TOP   = FORM_LWR_TOP
    BOT   = FORM_LWR_BOT

    n      = len(PROC_LABELS)
    item_h = (TOP - BOT) / n   # 各チェック項目の高さ

    # 下部全体外枠（加工名エリアと右大枠を1つの角丸矩形で統合）
    c.setLineWidth(0.8)
    c.roundRect(ML, BOT, MR - ML, TOP - BOT, CORNER_R)
    # 加工名列と右大枠の縦仕切り
    c.setLineWidth(0.5)
    c.line(CHK_R, BOT, CHK_R, TOP)

    # 各チェック項目
    CB_W = 9.0
    CB_H = 9.0
    for i, label in enumerate(PROC_LABELS):
        item_top = TOP - i * item_h
        item_bot = item_top - item_h
        if i > 0:
            c.line(ML, item_top, CHK_R, item_top)
        c.setFont(_FONT_NAME, 6.5)
        c.drawString(ML + 3.0, item_bot + (item_h - 6.5) / 2 + 0.5, label)
        cb_x = CHK_R - CB_W - 3.0
        cb_y = item_bot + (item_h - CB_H) / 2
        c.rect(cb_x, cb_y, CB_W, CB_H)

    # 切断仕上日 小枠（右側大枠の右上）
    CL = FORM_CUT_LEFT
    CT = FORM_CUT_TOP
    CB_BOT = FORM_CUT_BOT

    c.setLineWidth(0.5)
    c.drawPath(
        _cut_date_box_path(c, CL, CB_BOT, MR - CL, CT - CB_BOT, CORNER_R),
        stroke=1,
        fill=0,
    )
    # ラベルを枠内上寄せで描画
    c.setFont(_FONT_NAME, 7.0)
    c.drawString(CL + 4.0, CT - 11.0, "切断仕上日")


# ── 画面行設定の反映（仕上日・AM/PM・加工名チェック）─────────────────────────
# 売上伝票(01)・工場控(02)・指図書系(03-06) に共通で重ねて描画する。
# 納品書(07)・受領書(08) では呼び出さない（仕上日/AM-PM欄をマスクしているため）。

# 仕上日（月・日）表示の左シフト量。月/日ラベルごと少し左へ寄せる（約3mm）。
# 01〜06 の仕上日欄にのみ適用し、07納品書・08受領書には適用しない。
FINISH_DATE_SHIFT: float = 8.0


def _draw_header_finish_date(c: rl_canvas.Canvas, finish_date: Any) -> None:
    """画面行設定の仕上日をヘッダー「仕上日」欄へ 〇月〇日 形式で描画する。

    OLAP取得データに仕上日があっても、画面で設定した値（finish_date）を優先する。
    既存フォームの「月」「日」ラベル位置に合わせて月・日の数値だけを埋める。
    全体を FINISH_DATE_SHIFT 分だけ左へ寄せる（ラベルも同量左に寄せて重ならないようにする）。
    """
    if not finish_date:
        return
    month = getattr(finish_date, "month", None)
    day = getattr(finish_date, "day", None)
    if month is None or day is None:
        return
    c.setFont(_FONT_NAME, 8.0)
    # 「月」ラベルは FORM_HDR_RIGHT - 28、「日」ラベルは右揃え FORM_HDR_RIGHT - 4。
    c.drawRightString(FORM_HDR_RIGHT - 30.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, str(month))
    c.drawRightString(FORM_HDR_RIGHT - 8.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, str(day))


def _draw_ampm_circle(c: rl_canvas.Canvas, am_pm: Any) -> None:
    """画面行設定の AM/PM に応じて「AM・PM」欄の該当文字へ丸印を描画する。"""
    if not am_pm:
        return
    fs = 11.0
    baseline = FORM_HDR_BOT + 8.0
    cx = (371.0 + FORM_HDR_RIGHT) / 2
    total_w = c.stringWidth("AM・PM", _FONT_NAME, fs)
    left = cx - total_w / 2
    am_w = c.stringWidth("AM", _FONT_NAME, fs)
    sep_w = c.stringWidth("・", _FONT_NAME, fs)
    pm_w = c.stringWidth("PM", _FONT_NAME, fs)
    if str(am_pm).strip().upper().startswith("P"):
        seg_center = left + am_w + sep_w + pm_w / 2
        seg_w = pm_w
    else:
        seg_center = left + am_w / 2
        seg_w = am_w
    cy = baseline + fs * 0.32
    rx = seg_w / 2 + 3.0
    ry = fs * 0.62
    c.saveState()
    c.setLineWidth(0.9)
    c.ellipse(seg_center - rx, cy - ry, seg_center + rx, cy + ry, stroke=1, fill=0)
    c.restoreState()


# チェックマーク（✔）の太さ。太字に見せるため通常枠線より太い実線で描く。
PROC_CHECK_LINE_WIDTH = 1.6


def _draw_process_check_marks(c: rl_canvas.Canvas, process_checks: Any) -> None:
    """画面行設定の加工名チェックON項目について左下チェック欄中央へ「✔」を描画する。

    既存の `_draw_lower_section` と同じ座標計算でチェックボックスを特定し、その枠の
    中央へ太字の「✔（チェックマーク）」を描く（枠線・枠の塗りつぶしは変更しない）。

    なお ReportLab で使用している和文 CID フォント (HeiseiKakuGo-W5) は
    U+2713/U+2714 等のチェックマーク字形を持たず、文字として描くと空白になる。
    そのため「✔」をベクター線（2本のストローク）で描画し、線幅を太くして太字の
    チェックマークに見せる。OFF の項目には何も描かない。
    """
    if not process_checks:
        return
    CHK_R = FORM_CHK_RIGHT
    TOP = FORM_LWR_TOP
    BOT = FORM_LWR_BOT
    n = len(PROC_LABELS)
    item_h = (TOP - BOT) / n
    CB_W = 9.0
    CB_H = 9.0
    c.saveState()
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(PROC_CHECK_LINE_WIDTH)
    c.setLineCap(1)   # 丸キャップ
    c.setLineJoin(1)  # 丸ジョイン
    for i, label in enumerate(PROC_LABELS):
        if not label or not process_checks.get(label):
            continue
        item_top = TOP - i * item_h
        item_bot = item_top - item_h
        cb_x = CHK_R - CB_W - 3.0
        cb_y = item_bot + (item_h - CB_H) / 2
        cx = cb_x + CB_W / 2
        cy = cb_y + CB_H / 2
        # 枠中央に収まる「✔」字形（左下→谷→右上）を2本の線分で描く。
        p_left = (cx - 3.0, cy - 0.2)
        p_low = (cx - 1.0, cy - 2.6)
        p_right = (cx + 3.4, cy + 3.0)
        c.lines([
            (p_left[0], p_left[1], p_low[0], p_low[1]),
            (p_low[0], p_low[1], p_right[0], p_right[1]),
        ])
    c.restoreState()


def _draw_row_settings(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """画面行設定（仕上日・AM/PM・加工名チェック）をフォームへ重ね描きする。"""
    _draw_header_finish_date(c, data.get("row_finish_date"))
    _draw_ampm_circle(c, data.get("row_am_pm"))
    _draw_process_check_marks(c, data.get("row_process_checks"))


def _draw_special_notes_section(c: rl_canvas.Canvas) -> None:
    """納品書/受領書の下部特記事項枠を描画する。"""
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    c.setLineWidth(0.8)
    c.roundRect(FORM_LWR_LEFT, FORM_LWR_BOT,
                FORM_LWR_RIGHT - FORM_LWR_LEFT,
                FORM_LWR_TOP - FORM_LWR_BOT,
                CORNER_R)
    c.setFont(_FONT_NAME, 14.0)
    c.drawString(FORM_LWR_LEFT + 8.0, FORM_LWR_TOP - 18.0, "特記事項")


def _draw_delivery_stamp_boxes(c: rl_canvas.Canvas) -> None:
    """受領書中央右側の検印/配送者印枠を描画する。"""
    box_w = 42.0
    box_h = 32.0
    gap = 8.0
    x = STAMP_X - box_w - gap
    y = FORM_SUM_TOP - box_h - 2.0
    for index, title in enumerate(("検印", "配送者印")):
        bx = x + index * (box_w + gap)
        c.setLineWidth(0.8)
        c.roundRect(bx, y, box_w, box_h, CORNER_R)
        c.setFont(_FONT_NAME, 7.0)
        c.drawCentredString(bx + box_w / 2, y + box_h - 10.0, title)


# ══════════════════════════════════════════════════════════════════════════════
# アプリ描画方式: 指図書系 (03-06) — 合計行なし・備考/受入日列
# ══════════════════════════════════════════════════════════════════════════════

def _draw_detail_outline_nototal(c: rl_canvas.Canvas, left: float, right: float) -> None:
    """合計行なし（明細7行のみ）の表外枠を描く。"""
    top = FORM_HDR_BOT
    bot = FORM_DETAIL_BOT
    r   = CORNER_R
    p = c.beginPath()
    p.moveTo(left, bot + r)
    p.lineTo(left, top - r)
    p.curveTo(left, top - r / 2, left + r / 2, top, left + r, top)
    p.lineTo(right - r, top)
    p.curveTo(right - r / 2, top, right, top - r / 2, right, top - r)
    p.lineTo(right, bot + r)
    p.curveTo(right, bot + r / 2, right - r / 2, bot, right - r, bot)
    p.lineTo(left + r, bot)
    p.curveTo(left + r / 2, bot, left, bot + r / 2, left, bot + r)
    p.lineTo(left, bot + r)
    c.drawPath(p, stroke=1, fill=0)


def _draw_gen_circle(c: rl_canvas.Canvas) -> None:
    """指図書系に点線の丸で囲った「現」を描画する（コードNo列右端寄り）。"""
    cx, cy, r = GEN_CIRCLE_X, FORM_TITLE_Y - 1.0, 11.0
    c.saveState()
    c.setDash([2, 3])
    c.setLineWidth(0.8)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.restoreState()
    c.setFont(_FONT_NAME, 9.0)
    c.drawCentredString(cx, cy - 4.5, "現")


def _draw_noki_line(c: rl_canvas.Canvas) -> None:
    """タイトル下線直下に納期・受入方法行を描画する（コードNo列右端から開始）。"""
    y = FORM_TITLE_UL_Y - 9.0
    c.setFont(_FONT_NAME, 7.5)
    c.drawString(NOKI_LINE_X, y,
                 "納期　　月　　日　　時　　分　／　受入方法　直取・配送")


def _draw_stamp_box(c: rl_canvas.Canvas, title: str) -> None:
    """印枠（工場印/商品課印/配送者印）を描画する。表底辺からSTAMP_GAP離して配置。"""
    x = STAMP_X
    w = STAMP_W
    h = STAMP_H
    y = FORM_DETAIL_BOT - STAMP_GAP - h   # 表底辺から空き分だけ下げた位置
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, CORNER_R)
    c.setFont(_FONT_NAME, 7.0)
    c.drawCentredString(x + w / 2, y + h - 10.0, title)


def _draw_form_structure_shizu(c: rl_canvas.Canvas, title: str,
                                stamp_title: str = "") -> None:
    """指図書系フォームの骨格を描画する（合計行なし・備考/受入日列）。"""
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)

    # ─── タイトル + 固定幅下線 ─────────────────────────────────────────────────
    c.setFont(_FONT_NAME, 16)
    c.drawCentredString(FORM_TITLE_X, FORM_TITLE_Y, title)
    ul_half = FORM_TITLE_UL_HALF   # 全伝票共通固定幅
    c.setLineWidth(1.0)
    c.line(FORM_TITLE_X - ul_half, FORM_TITLE_UL_Y,
           FORM_TITLE_X + ul_half, FORM_TITLE_UL_Y)

    # ─── 点線丸「現」────────────────────────────────────────────────────────
    _draw_gen_circle(c)

    # ─── 納期行 ───────────────────────────────────────────────────────────────
    _draw_noki_line(c)

    # ─── 会社名（表ヘッダー上・ヘッダー枠右側）──────────────────────────────────
    logo_y = COMPANY_NAME_Y + 5.2 - COMPANY_LOGO_H / 2
    c.drawImage(str(resource_path("assets/manekiya_logo.png")), COMPANY_LOGO_X, logo_y,
                width=COMPANY_LOGO_W, height=COMPANY_LOGO_H, mask="auto")
    c.setFont(_FONT_NAME, 16)
    c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y, "まねきや硝子株式会社")

    # ─── ヘッダー枠（角丸・左部分）────────────────────────────────────────────
    c.setLineWidth(1.0)
    c.drawPath(
        _top_round_rect_path(c, FORM_HDR_LEFT, FORM_HDR_BOT,
                             FORM_HDR_RIGHT - FORM_HDR_LEFT,
                             FORM_HDR_TOP - FORM_HDR_BOT, CORNER_R),
        stroke=1, fill=0,
    )
    c.setLineWidth(0.5)
    c.line(FORM_HDR_LEFT, FORM_HDR_MID, FORM_HDR_RIGHT, FORM_HDR_MID)
    for x in HDR_ROW1_DIVS:
        c.line(x, FORM_HDR_MID, x, FORM_HDR_TOP)
    for x in HDR_ROW2_DIVS:
        c.line(x, FORM_HDR_BOT, x, FORM_HDR_MID)

    FS_LBL = 6.0

    def lbl(text: str, x: float, y: float) -> None:
        c.setFont(_FONT_NAME, FS_LBL)
        c.drawString(x + 1.5, y, text)

    r1_lbl_y = FORM_HDR_TOP - 8.0
    lbl("コードNo", FORM_HDR_LEFT, r1_lbl_y)
    lbl("得意先名", 90.0,          r1_lbl_y)
    lbl("受注No",   284.0,         r1_lbl_y)
    lbl("仕上日",   371.0,         HDR_SHIAGE_LABEL_Y)
    c.drawString(FORM_HDR_RIGHT - 28.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, "月")
    c.drawRightString(FORM_HDR_RIGHT - 4.0 - FINISH_DATE_SHIFT, HDR_SHIAGE_MONTH_DAY_Y, "日")
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(279.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "殿")
    r2_lbl_y = FORM_HDR_MID - 8.0
    lbl("発行日",   FORM_HDR_LEFT, r2_lbl_y)
    lbl("納品日",   90.0,          r2_lbl_y)
    lbl("伝票No",   145.0,         r2_lbl_y)
    lbl("取引区分", 197.0,         r2_lbl_y)
    lbl("出荷区分", 245.0,         r2_lbl_y)
    lbl("入力者名", 284.0,         r2_lbl_y)
    c.setFont(_FONT_NAME, 11.0)
    c.drawCentredString((371.0 + FORM_HDR_RIGHT) / 2, FORM_HDR_BOT + 8.0, "AM・PM")

    # ─── 明細テーブル（指図書系: 合計行なし）────────────────────────────────
    table_left  = SHIZU_TBL_COLS[0]
    table_right = SHIZU_TBL_COLS[-1]

    # テーブルヘッダー行（黒塗り・白文字）
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _top_round_rect_path(c, table_left, FORM_TBL_HDR_BOT,
                             table_right - table_left,
                             FORM_HDR_BOT - FORM_TBL_HDR_BOT, CORNER_R),
        stroke=0, fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    tbl_hdr_cy = (FORM_HDR_BOT + FORM_TBL_HDR_BOT) / 2 - 4.0
    for x1, x2, label in zip(SHIZU_TBL_COLS, SHIZU_TBL_COLS[1:], SHIZU_COL_LABELS):
        c.drawCentredString((x1 + x2) / 2, tbl_hdr_cy, label)
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)

    # 外枠（底辺角丸・合計行なし）
    c.setLineWidth(0.5)
    _draw_detail_outline_nototal(c, table_left, table_right)

    # 縦仕切り（全て FORM_DETAIL_BOT まで）
    for x in SHIZU_TBL_COLS[1:-1]:
        c.line(x, FORM_DETAIL_BOT, x, FORM_HDR_BOT)

    # 明細行間の水平線
    for i in range(1, FORM_DETAIL_ROWS):
        y = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        c.line(table_left, y, table_right, y)
    c.line(table_left, FORM_TBL_HDR_BOT, table_right, FORM_TBL_HDR_BOT)

    # 行番号
    c.setFont(_FONT_NAME, 7.0)
    for i in range(FORM_DETAIL_ROWS):
        row_cy = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H - FORM_DETAIL_ROW_H / 2
        c.drawCentredString((table_left + SHIZU_TBL_COLS[1]) / 2, row_cy - 3.0, str(i + 1))

    # ─── 摘要 / 物件No（下線のみ）────────────────────────────────────────────
    c.setLineWidth(0.5)
    c.setFont(_FONT_NAME, 7.0)
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_SUM_BOT + 3.0, "摘　要")
    c.line(note_line_x, FORM_SUM_BOT, FORM_SUM_RIGHT, FORM_SUM_BOT)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_BKNO_BOT + 3.0, "物件No")
    c.line(note_line_x, FORM_BKNO_BOT, FORM_SUM_RIGHT, FORM_BKNO_BOT)

    # ─── 消費税注記 ────────────────────────────────────────────────────────────
    c.setFont(_FONT_NAME, 7.5)
    c.drawString(note_line_x, TAX_Y, TAX_NOTICE)

    # ─── 下部チェック欄 ───────────────────────────────────────────────────────
    _draw_lower_section(c)

    # ─── 印枠（工場印/商品課印/配送者印） ──────────────────────────────────────
    if stamp_title:
        _draw_stamp_box(c, stamp_title)


def _draw_form_data_shizu(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """指図書系フォームにデータを印字する（品名・数量・受入日のみ表示）。"""
    FS_VAL = 7.8
    FS_DIM = 7.0

    def val(text: str, x: float, y: float, fs: float = FS_VAL,
            max_w: float | None = None) -> None:
        _str(c, text, x, y, fs, max_w)

    # 営業所・TEL/FAX
    office_name = data.get("office_name", "")
    office_tel  = data.get("office_tel", "")
    office_fax  = data.get("office_fax", "")
    if office_name or office_tel or office_fax:
        c.setFont(_FONT_NAME, 9.5)
        if office_name:
            c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 11.0, str(office_name))
        tel_fax = "  ".join(part for part in (
            f"TEL {office_tel}" if office_tel else "",
            f"FAX {office_fax}" if office_fax else "",
        ) if part)
        if tel_fax:
            c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 21.0, tel_fax)

    # ヘッダー行1
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    val(data.get("code_no", ""),       x=FORM_HDR_LEFT + DATA_X_PAD, y=r1_y)
    val(data.get("customer_name", ""), x=90.0 + DATA_X_PAD,          y=r1_y, max_w=178.0)
    val(data.get("order_no", ""),      x=284.0 + DATA_X_PAD,         y=r1_y)

    # ヘッダー行2
    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    val(data.get("issue_date", ""),    x=FORM_HDR_LEFT + DATA_X_PAD, y=r2_y)
    val(data.get("delivery_date", ""), x=90.0 + DATA_X_PAD,          y=r2_y)
    val(data.get("voucher_no", ""),    x=145.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("trade_type", ""),    x=197.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("ship_type", ""),     x=245.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("operator", ""),      x=284.0 + DATA_X_PAD,         y=r2_y, max_w=80.0)

    # 受入日列の表示X。日付/場所は「加」のすぐ右の同一列に揃える。
    nyuki_x = SHIZU_TBL_COLS[-2] + DATA_X_PAD   # 受入日列左端（636.5pt）
    nyuki_data_x = nyuki_x + 16.0
    FS_NYUKI = 6.5

    # 明細行
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET

        is_star = row.get("name", "") == "*"

        # 品名（1段目=左寄せ、2段目=右寄せ大フォント）
        _str(c, row.get("name", ""), TBL_X_NAME, yu, FS_VAL, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, FS_DIM_LARGE, max_w=TBL_MAX_NAME)

        if not is_star:
            # 数量（1段目=左寄せ、2段目=右寄せ）
            _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
            _rstr(c, row.get("qty", ""), DET_QTY_RX, yl, FS_VAL, max_w=TBL_MAX_QTY)

            # 受入日列: 加工記号は左、日付/場所はそのすぐ右に揃える。
            notes = row.get("note_lines", [])
            finish_date = row.get("finish_date", "")
            note_rows = _split_note_rows(notes[0]) if notes else []
            txt0 = note_rows[0][1] if note_rows else ""
            txt1 = note_rows[1][1] if len(note_rows) > 1 else (
                _split_note_rows(notes[1])[0][1] if len(notes) > 1 else ""
            )
            if txt0 == "加":
                _str(c, txt0, nyuki_x, yu, FS_NYUKI, max_w=SHIZU_MAX_W_NYUKI)
            elif txt0:
                _str(c, txt0, nyuki_data_x, yu, FS_NYUKI, max_w=SHIZU_MAX_W_NYUKI)
            if finish_date:
                _str(c, finish_date, nyuki_data_x, yu, FS_NYUKI, max_w=SHIZU_MAX_W_NYUKI)
            if txt1:
                _str(c, txt1, nyuki_data_x, yl, FS_NYUKI, max_w=SHIZU_MAX_W_NYUKI)

    # 摘要 / 物件No データ
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    _draw_summary_lines(c, data, FS_DIM)
    for index, line in enumerate(
        [line for line in data.get("property_lines", []) if line][:1]
    ):
        val(str(line), x=note_line_x, y=FORM_BKNO_BOT + 3.0 + index * 9.0,
            fs=FS_DIM, max_w=FORM_SUM_RIGHT - note_line_x)

    # 営業担当（ラベル常時表示）
    sales_rep = str(data.get("sales_rep", "") or "")
    _str(c, f"営業担当：{sales_rep}", SUM_STAFF_X, FORM_SUM_BOT + 6.0, FS_DIM)

    # 工事担当（ラベル常時表示）
    construction_rep = str(data.get("construction_rep", "") or "")
    _str(c, f"工事担当：{construction_rep}", SUM_STAFF_X, FORM_BKNO_BOT + 4.0, FS_DIM)

    # QR コード
    qr_order_no = str(data.get("qr_order_no") or data.get("order_no") or "")
    if qr_order_no:
        qr_buf = build_qr_code_image(qr_order_no)
        c.drawImage(ImageReader(qr_buf), FORM_LWR_RIGHT - 58.0, FORM_LWR_BOT + 12.0,
                    width=44.0, height=44.0, mask="auto")

    # 画面行設定（仕上日・AM/PM・加工名チェック）を反映
    _draw_row_settings(c, data)


def _draw_form_shizu(c: rl_canvas.Canvas, data: dict[str, Any],
                     title: str, stamp_title: str = "",
                     edit_objects: list[dict[str, Any]] | None = None) -> None:
    """指図書系フォームを一から描画してデータも印字する。

    edit_objects を渡すと、指図書編集画面で保存したオブジェクトをフォームへ
    重ね描きする（指図書(1)/指図書(2)/梱包明細書で共通の位置に反映）。
    """
    _draw_form_structure_shizu(c, title, stamp_title)
    _draw_form_data_shizu(c, data)
    _draw_edit_objects(c, edit_objects)


def _draw_edit_objects(c: rl_canvas.Canvas,
                       objects: list[dict[str, Any]] | None) -> None:
    """指図書編集オブジェクト（テキスト・線・四角形・楕円）を重ね描きする。

    保存JSONの座標は編集画面と同じ左上原点のscene座標。ここでのみ
    reportlab座標（原点=左下）へ変換する。
    """
    if not objects:
        return
    text_font = _ensure_edit_text_font()
    debug_boxes = _voucher_edit_debug_boxes_enabled()
    seen_ids: set[str] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        # 同一IDのオブジェクトは一度だけ描画する（要件10: 重複描画防止）。
        obj_id = obj.get("id")
        if obj_id is not None:
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)
        obj_type = obj.get("type")
        stroke_rgb = _object_rgb(obj, "stroke_color")
        text_rgb = _object_rgb(obj, "text_color")
        fill_rgb = _object_rgb(obj, "fill_color")
        c.saveState()
        c.setStrokeColorRGB(*stroke_rgb)
        c.setFillColorRGB(*text_rgb)
        try:
            if obj_type == "symbol_text":
                draw_symbol_text(c, obj)
            elif obj_type == "image":
                _draw_edit_image(c, obj, obj_id)
            elif obj_type == "text":
                fs = float(obj.get("font_size") or 10.0)
                x = float(obj.get("x", 0.0))
                y = float(obj.get("y", 0.0))
                w = float(obj.get("width") or obj.get("w") or 0.0)
                h = float(obj.get("height") or obj.get("h") or fs)
                pdf_x, pdf_y, pdf_w, pdf_h = _scene_rect_to_pdf_rect(x, y, w, h)
                _log.debug(
                    "edit_object object_id=%s type=%s scene_x=%s scene_y=%s "
                    "scene_width=%s scene_height=%s pdf_x=%s pdf_y=%s "
                    "pdf_width=%s pdf_height=%s PAGE_W=%s PAGE_H=%s",
                    obj_id, obj_type, x, y, w, h, pdf_x, pdf_y, pdf_w, pdf_h,
                    PAGE_W, PAGE_H,
                )
                draw_text_in_scene_rect(
                    c, str(obj.get("text", "")), x, y, w, h, text_font, fs,
                    text_align="left",
                    vertical_align="top",
                    color=obj.get("text_color") or obj.get("color") or "#000000",
                )
            elif obj_type == "line":
                c.setLineWidth(float(obj.get("line_width") or 1.0))
                x1 = float(obj.get("x1", 0.0)); y1 = float(obj.get("y1", 0.0))
                x2 = float(obj.get("x2", 0.0)); y2 = float(obj.get("y2", 0.0))
                pdf_x1, pdf_y1, pdf_x2, pdf_y2 = _scene_line_to_pdf(x1, y1, x2, y2)
                _log.debug(
                    "edit_object object_id=%s type=%s scene_x1=%s scene_y1=%s "
                    "scene_x2=%s scene_y2=%s pdf_x1=%s pdf_y1=%s "
                    "pdf_x2=%s pdf_y2=%s PAGE_W=%s PAGE_H=%s",
                    obj_id, obj_type, x1, y1, x2, y2, pdf_x1, pdf_y1,
                    pdf_x2, pdf_y2, PAGE_W, PAGE_H,
                )
                c.line(pdf_x1, pdf_y1, pdf_x2, pdf_y2)
                if debug_boxes:
                    _draw_debug_line_points(c, pdf_x1, pdf_y1, pdf_x2, pdf_y2)
            elif obj_type == "rectangle":
                scene_x = float(obj.get("x", 0.0)); scene_y = float(obj.get("y", 0.0))
                w = float(obj.get("width") or obj.get("w") or 0.0)
                h = float(obj.get("height") or obj.get("h") or 0.0)
                x, y, pdf_w, pdf_h = _scene_rect_to_pdf_rect(scene_x, scene_y, w, h)
                _log.debug(
                    "edit_object object_id=%s type=%s scene_x=%s scene_y=%s "
                    "scene_width=%s scene_height=%s pdf_x=%s pdf_y=%s "
                    "pdf_width=%s pdf_height=%s PAGE_W=%s PAGE_H=%s",
                    obj_id, obj_type, scene_x, scene_y, w, h, x, y, pdf_w, pdf_h,
                    PAGE_W, PAGE_H,
                )
                c.setLineWidth(float(obj.get("line_width") or 1.0))
                if fill_rgb is not None:
                    c.setFillColorRGB(*fill_rgb)
                c.rect(x, y, pdf_w, pdf_h, stroke=1, fill=1 if fill_rgb is not None else 0)
                if debug_boxes:
                    _draw_debug_rect(c, x, y, pdf_w, pdf_h, (1.0, 0.0, 0.0))
                # 図形内テキストを水平中央・垂直中央で描画する（要件7）。
                inner = str(obj.get("text", ""))
                if inner:
                    fs = float(obj.get("font_size") or 10.0)
                    c.setFillColorRGB(*text_rgb)
                    draw_text_in_scene_rect(
                        c, inner, scene_x, scene_y, w, h, text_font, fs,
                        text_align=str(obj.get("text_align") or "center"),
                        vertical_align=str(obj.get("vertical_align") or "middle"),
                        color=obj.get("text_color") or obj.get("color") or "#000000",
                    )
            elif obj_type == "ellipse":
                scene_x = float(obj.get("x", 0.0)); scene_y = float(obj.get("y", 0.0))
                w = float(obj.get("width") or obj.get("w") or 0.0)
                h = float(obj.get("height") or obj.get("h") or 0.0)
                x, y, pdf_w, pdf_h = _scene_rect_to_pdf_rect(scene_x, scene_y, w, h)
                _log.debug(
                    "edit_object object_id=%s type=%s scene_x=%s scene_y=%s "
                    "scene_width=%s scene_height=%s pdf_x=%s pdf_y=%s "
                    "pdf_width=%s pdf_height=%s PAGE_W=%s PAGE_H=%s",
                    obj_id, obj_type, scene_x, scene_y, w, h, x, y, pdf_w, pdf_h,
                    PAGE_W, PAGE_H,
                )
                c.setLineWidth(float(obj.get("line_width") or 1.0))
                if fill_rgb is not None:
                    c.setFillColorRGB(*fill_rgb)
                c.ellipse(x, y, x + pdf_w, y + pdf_h, stroke=1,
                          fill=1 if fill_rgb is not None else 0)
                if debug_boxes:
                    _draw_debug_rect(c, x, y, pdf_w, pdf_h, (0.0, 0.6, 0.0))
                # 図形内テキストを水平中央・垂直中央で描画する（要件7）。
                inner = str(obj.get("text", ""))
                if inner:
                    fs = float(obj.get("font_size") or 10.0)
                    c.setFillColorRGB(*text_rgb)
                    draw_text_in_scene_rect(
                        c, inner, scene_x, scene_y, w, h, text_font, fs,
                        text_align=str(obj.get("text_align") or "center"),
                        vertical_align=str(obj.get("vertical_align") or "middle"),
                        color=obj.get("text_color") or obj.get("color") or "#000000",
                    )
        finally:
            c.restoreState()


def _draw_edit_image(c: rl_canvas.Canvas, obj: dict[str, Any], obj_id: Any) -> None:
    """画像オブジェクトをscene座標からPDF座標へ変換して重ね描きする（要件4）。

    base64 の PNG データを ImageReader 経由で drawImage する。指図書(1)(2)・
    梱包明細書（03/04/05）のみ呼ばれるため、他伝票には描画されない。
    """
    scene_x = float(obj.get("x", 0.0)); scene_y = float(obj.get("y", 0.0))
    w = float(obj.get("width") or obj.get("w") or 0.0)
    h = float(obj.get("height") or obj.get("h") or 0.0)
    x, y, pdf_w, pdf_h = _scene_rect_to_pdf_rect(scene_x, scene_y, w, h)
    data_b64 = obj.get("image_data") or ""
    if not data_b64 or pdf_w <= 0 or pdf_h <= 0:
        return
    try:
        image_bytes = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        _log.warning("edit_object 画像データのbase64復号に失敗しました object_id=%s", obj_id)
        return
    _log.debug(
        "edit_object object_id=%s type=image scene_x=%s scene_y=%s "
        "scene_width=%s scene_height=%s pdf_x=%s pdf_y=%s pdf_width=%s pdf_height=%s",
        obj_id, scene_x, scene_y, w, h, x, y, pdf_w, pdf_h,
    )
    img = ImageReader(io.BytesIO(image_bytes))
    c.drawImage(img, x, y, width=pdf_w, height=pdf_h, mask="auto")


def _voucher_edit_debug_boxes_enabled() -> bool:
    return str(os.environ.get("VOUCHER_EDIT_DEBUG_BOXES") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _draw_debug_rect(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float,
                     rgb: tuple[float, float, float]) -> None:
    c.saveState()
    try:
        c.setStrokeColorRGB(*rgb)
        c.setLineWidth(0.4)
        c.setDash(2, 2)
        c.rect(x, y, w, h, stroke=1, fill=0)
    finally:
        c.restoreState()


def _draw_debug_line_points(c: rl_canvas.Canvas, x1: float, y1: float,
                            x2: float, y2: float) -> None:
    c.saveState()
    try:
        c.setFillColorRGB(0.8, 0.0, 0.8)
        r = 1.5
        c.circle(x1, y1, r, stroke=0, fill=1)
        c.circle(x2, y2, r, stroke=0, fill=1)
    finally:
        c.restoreState()


def _draw_debug_text_guides(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float,
                            first_baseline_y: float) -> None:
    c.saveState()
    try:
        c.setStrokeColorRGB(0.0, 0.45, 1.0)
        c.setLineWidth(0.3)
        c.setDash(2, 2)
        c.rect(x, y, w, h, stroke=1, fill=0)
        c.setDash()
        c.setStrokeColorRGB(1.0, 0.0, 0.0)
        c.line(x, first_baseline_y, x + w, first_baseline_y)
        c.setStrokeColorRGB(0.0, 0.6, 0.0)
        c.line(x, y + h / 2.0, x + w, y + h / 2.0)
    finally:
        c.restoreState()


def _object_rgb(obj: dict[str, Any], key: str) -> tuple[float, float, float] | None:
    value = obj.get(key)
    if value is None and key in ("stroke_color", "text_color"):
        color = obj.get("color")
        if isinstance(color, (list, tuple)) and len(color) == 3:
            return tuple(float(c0) for c0 in color)  # type: ignore[return-value]
        return (0.0, 0.0, 0.0)
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return (
            int(value[1:3], 16) / 255.0,
            int(value[3:5], 16) / 255.0,
            int(value[5:7], 16) / 255.0,
        )
    return (0.0, 0.0, 0.0)


def draw_text_in_scene_rect(
    canvas: rl_canvas.Canvas,
    text: str,
    scene_x: float,
    scene_y: float,
    width: float,
    height: float,
    font_name: str,
    font_size: float,
    text_align: str = "center",
    vertical_align: str = "middle",
    color: Any = "#000000",
) -> None:
    """scene矩形内の水平・垂直配置に従ってテキストをPDFへ描画する。"""
    c = canvas
    x, y, w, h = _scene_rect_to_pdf_rect(scene_x, scene_y, width, height)
    c.setFont(font_name, font_size)
    rgb = _coerce_rgb(color)
    if rgb is not None:
        c.setFillColorRGB(*rgb)
    lines = text.splitlines() or [""]
    line_h = font_size * 1.2
    total_h = len(lines) * line_h
    vertical_align = vertical_align if vertical_align in {"top", "middle", "bottom"} else "middle"
    text_align = text_align if text_align in {"left", "center", "right"} else "center"
    if vertical_align == "top":
        first_baseline_y = y + h - font_size
    elif vertical_align == "bottom":
        first_baseline_y = y + total_h - font_size
    else:
        first_baseline_y = y + (h + total_h) / 2.0 - font_size
    if text_align == "left":
        draw_x = x
    elif text_align == "right":
        draw_x = x + w
    else:
        draw_x = x + w / 2.0
    if _voucher_edit_debug_boxes_enabled():
        _draw_debug_text_guides(c, x, y, w, h, first_baseline_y)
    for i, line in enumerate(lines):
        baseline = first_baseline_y - i * line_h
        if text_align == "left":
            c.drawString(draw_x, baseline, line)
        elif text_align == "right":
            c.drawRightString(draw_x, baseline, line)
        else:
            c.drawCentredString(draw_x, baseline, line)


def draw_symbol_text(canvas: rl_canvas.Canvas, obj: dict[str, Any]) -> None:
    """中心アンカーの短い注記テキストをPDFへ描画する。"""
    c = canvas
    text = str(obj.get("text", "")).strip()
    if not text:
        return
    font_name = _ensure_edit_text_font()
    font_size = float(obj.get("font_size") or 10.0)
    scene_x = float(obj.get("x", 0.0))
    scene_y = float(obj.get("y", 0.0))
    pdf_x = scene_x
    pdf_y = PAGE_H - scene_y
    c.setFont(font_name, font_size)
    rgb = _coerce_rgb(obj.get("text_color") or obj.get("color") or "#000000")
    if rgb is not None:
        c.setFillColorRGB(*rgb)
    anchor = str(obj.get("anchor") or "center")
    if anchor == "center":
        baseline_y = pdf_y - font_size * 0.35
        c.drawCentredString(pdf_x, baseline_y, text)
    else:
        baseline_y = pdf_y - font_size * 0.35
        c.drawCentredString(pdf_x, baseline_y, text)


def _coerce_rgb(value: Any) -> tuple[float, float, float] | None:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return (
            int(value[1:3], 16) / 255.0,
            int(value[3:5], 16) / 255.0,
            int(value[5:7], 16) / 255.0,
        )
    if isinstance(value, (list, tuple)) and len(value) == 3:
        vals = tuple(float(v) for v in value)
        if any(v > 1.0 for v in vals):
            return tuple(max(0.0, min(255.0, v)) / 255.0 for v in vals)  # type: ignore[return-value]
        return vals  # type: ignore[return-value]
    return None


def _build_scratch_shizu(data: dict[str, Any], title: str,
                          stamp_title: str = "",
                          edit_objects: list[dict[str, Any]] | None = None) -> bytes:
    """指図書系(03-06)を一から描画したPDFをバイト列で返す。"""
    _ensure_font()
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    _draw_form_shizu(c, data, title, stamp_title, edit_objects)
    c.save()
    return buf.getvalue()


def _draw_form_data_01(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """アプリ描画方式フォームにデータを印字する。"""
    FS_VAL = 7.8
    FS_DIM = 7.0

    def val(text: str, x: float, y: float, fs: float = FS_VAL,
            max_w: float | None = None) -> None:
        _str(c, text, x, y, fs, max_w)

    # 営業所・TEL/FAX（会社名グループ）
    office_name = data.get("office_name", "")
    office_tel = data.get("office_tel", "")
    office_fax = data.get("office_fax", "")
    if office_name or office_tel or office_fax:
        c.setFont(_FONT_NAME, 9.5)
        if office_name:
            c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 11.0, str(office_name))
        tel_fax = "  ".join(part for part in (f"TEL {office_tel}" if office_tel else "", f"FAX {office_fax}" if office_fax else "") if part)
        if tel_fax:
            c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 21.0, tel_fax)

    # ヘッダー行1（ベースライン: 行2境界 + HDR_DATA_Y_INNER）
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    val(data.get("code_no", ""),       x=FORM_HDR_LEFT + DATA_X_PAD, y=r1_y)
    val(data.get("customer_name", ""), x=90.0 + DATA_X_PAD,          y=r1_y, max_w=178.0)
    val(data.get("order_no", ""),      x=284.0 + DATA_X_PAD,         y=r1_y)

    # ヘッダー行2
    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    val(data.get("issue_date", ""),    x=FORM_HDR_LEFT + DATA_X_PAD, y=r2_y)
    val(data.get("delivery_date", ""), x=90.0 + DATA_X_PAD,          y=r2_y)
    val(data.get("voucher_no", ""),    x=145.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("trade_type", ""),    x=197.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("ship_type", ""),     x=245.0 + DATA_X_PAD,         y=r2_y)
    val(data.get("operator", ""),      x=284.0 + DATA_X_PAD,         y=r2_y, max_w=80.0)

    # 列右端 X（セル配置用）
    unit_rx  = TBL_COLS[4] - DATA_X_PAD           # 単価列右端
    amt_rx   = TBL_COLS[5] - DATA_X_PAD           # 金額列右端
    note_rx  = TBL_COLS[6] - DATA_X_PAD           # 摘要列右端（受注No/伝票No表示に使用）

    # 摘要列左右分割の最大幅
    note_left_max_w  = TBL_NOTE_MID_X - TBL_COLS[5] - TBL_NOTE_MID_PAD
    note_right_max_w = TBL_COLS[6] - TBL_NOTE_MID_X - TBL_NOTE_MID_PAD - DATA_X_PAD

    # 摘要集計（合計行用）
    upper_vals: list[float] = []
    lower_vals: list[float] = []

    # 明細行
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET   # 上段ベースライン
        yl = row_top - DET_LOWER_OFFSET   # 下段ベースライン

        is_star = row.get("name", "") == "*"

        # 品名列: 1段目=左寄せ、2段目=右寄せ（大フォント）
        _str(c, row.get("name", ""), TBL_X_NAME, yu, FS_VAL, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, FS_DIM_LARGE, max_w=TBL_MAX_NAME)

        if not is_star:
            # 数量列: 1段目=左寄せ、2段目=右寄せ
            _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
            _rstr(c, row.get("qty", ""), DET_QTY_RX, yl, FS_VAL, max_w=TBL_MAX_QTY)

            # 単価・金額（右揃え）
            _rstr(c, row.get("unit_price", ""), unit_rx, yu, FS_VAL, max_w=TBL_MAX_UNIT)
            _rstr(c, row.get("amount", ""),     amt_rx,  yu, FS_VAL, max_w=TBL_MAX_AMT)

            # 摘要: 数値は左側、加工記号は数値の右隣、日付/場所はそのすぐ右に揃える。
            notes = row.get("note_lines", [])
            finish_date = row.get("finish_date", "")
            note_text_x = TBL_NOTE_MID_X + TBL_NOTE_MID_PAD
            note_data_x = note_text_x + 16.0

            def draw_note_text(text: str, y: float) -> None:
                if not text:
                    return
                if text == "加":
                    _str(c, text, note_text_x, y, FS_DIM, max_w=note_right_max_w)
                else:
                    _str(c, text, note_data_x, y, FS_DIM, max_w=note_right_max_w)

            if notes:
                note_rows = _split_note_rows(notes[0])
                num, txt = note_rows[0]
                _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yu, FS_DIM, max_w=note_left_max_w)
                draw_note_text(txt, yu)
                if len(note_rows) > 1 and len(notes) == 1:
                    num, txt = note_rows[1]
                    _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yl, FS_DIM, max_w=note_left_max_w)
                    draw_note_text(txt, yl)
                    lower_vals.append(_extract_note_number(notes[0].split("/", 1)[1]))
                upper_vals.append(_extract_note_number(notes[0]))
            if finish_date:
                _str(c, finish_date, note_data_x, yu, FS_DIM, max_w=note_right_max_w)
            if len(notes) > 1:
                num, txt = _split_note_rows(notes[1])[0]
                _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yl, FS_DIM, max_w=note_left_max_w)
                draw_note_text(txt, yl)
                lower_vals.append(_extract_note_number(notes[1]))

    # 合計行: 摘要列の右端に上段・下段の合計を右揃えで表示。
    total_upper_y = FORM_DETAIL_BOT - DET_UPPER_OFFSET
    total_lower_y = FORM_DETAIL_BOT - DET_LOWER_OFFSET
    if upper_vals:
        s = sum(upper_vals)
        _rstr(c, f"{s:,.0f}" if s == int(s) else f"{s:,.2f}",
              note_rx, total_upper_y, FS_DIM)
    if lower_vals:
        s = sum(lower_vals)
        _rstr(c, f"{s:,.0f}" if s == int(s) else f"{s:,.2f}",
              note_rx, total_lower_y, FS_DIM)

    # 受注No / 伝票No（表の摘要列右下外側）
    order_no = str(data.get("order_no", "") or "")
    voucher_no = str(data.get("voucher_no", "") or "")
    if order_no:
        _rstr(c, f"受  {order_no}", note_rx, FORM_TOTAL_BOT - 7.0, FS_DIM)
    if voucher_no:
        _rstr(c, f"伝  {voucher_no}", note_rx, FORM_TOTAL_BOT - 16.0, FS_DIM)

    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    _draw_summary_lines(c, data, FS_DIM)
    for index, line in enumerate([line for line in data.get("property_lines", []) if line][:1]):
        val(str(line), x=note_line_x, y=FORM_BKNO_BOT + 3.0 + index * 9.0, fs=FS_DIM, max_w=FORM_SUM_RIGHT - note_line_x)

    # 営業担当（摘要下線の右側）: データが空でもラベルは常時表示
    sales_rep = str(data.get("sales_rep", "") or "")
    _str(c, f"営業担当：{sales_rep}", SUM_STAFF_X, FORM_SUM_BOT + 6.0, FS_DIM)

    # 工事担当（物件No下線の右側）: データが空でもラベルは常時表示
    construction_rep = str(data.get("construction_rep", "") or "")
    _str(c, f"工事担当：{construction_rep}", SUM_STAFF_X, FORM_BKNO_BOT + 4.0, FS_DIM)

    qr_order_no = str(data.get("qr_order_no") or data.get("order_no") or "")
    if qr_order_no:
        qr_buf = build_qr_code_image(qr_order_no)
        c.drawImage(ImageReader(qr_buf), FORM_LWR_RIGHT - 58.0, FORM_LWR_BOT + 12.0,
                    width=44.0, height=44.0, mask="auto")

    # 画面行設定（仕上日・AM/PM・加工名チェック）を反映
    _draw_row_settings(c, data)


def _build_scratch_01(data: dict[str, Any],
                       title: str = "売　上　伝　票") -> bytes:
    """売上伝票(01)または工場控を一から描画したPDFをバイト列で返す。"""
    _ensure_font()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    _draw_form_01(c, data, title)
    c.save()
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# アプリ描画方式: 納品書(07) / 受領書(08)
# ══════════════════════════════════════════════════════════════════════════════

def _fill_delivery_mask(c: rl_canvas.Canvas, x: float, y: float,
                        w: float, h: float) -> None:
    """非表示セルを濃いグレーで塗りつぶす。罫線は呼び出し側で再描画する。"""
    c.saveState()
    c.setFillColorRGB(*DELIVERY_MASK_RGB)
    c.rect(x, y, w, h, stroke=0, fill=1)
    c.restoreState()


def _fill_delivery_top_right_mask(c: rl_canvas.Canvas, x: float, y: float,
                                  w: float, h: float) -> None:
    """右上だけ角丸のマスクを描く。"""
    r = CORNER_R
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x, y + h)
    p.lineTo(x + w - r, y + h)
    p.curveTo(x + w - r / 2, y + h, x + w, y + h - r / 2, x + w, y + h - r)
    p.lineTo(x + w, y)
    p.lineTo(x, y)
    c.saveState()
    c.setFillColorRGB(*DELIVERY_MASK_RGB)
    c.drawPath(p, stroke=0, fill=1)
    c.restoreState()


def _fill_delivery_right_round_mask(c: rl_canvas.Canvas, x: float, y: float,
                                    w: float, h: float) -> None:
    """右上・右下だけ角丸のマスクを描く。"""
    r = CORNER_R
    p = c.beginPath()
    p.moveTo(x, y)
    p.lineTo(x, y + h)
    p.lineTo(x + w - r, y + h)
    p.curveTo(x + w - r / 2, y + h, x + w, y + h - r / 2, x + w, y + h - r)
    p.lineTo(x + w, y + r)
    p.curveTo(x + w, y + r / 2, x + w - r / 2, y, x + w - r, y)
    p.lineTo(x, y)
    c.saveState()
    c.setFillColorRGB(*DELIVERY_MASK_RGB)
    c.drawPath(p, stroke=0, fill=1)
    c.restoreState()


def _draw_delivery_header_masks(c: rl_canvas.Canvas) -> None:
    """納品書/受領書で発行日・仕上日・AM/PMセルをマスクする。"""
    _fill_delivery_mask(
        c,
        FORM_HDR_LEFT,
        FORM_HDR_BOT,
        HDR_ROW2_DIVS[0] - FORM_HDR_LEFT,
        FORM_HDR_MID - FORM_HDR_BOT,
    )
    _fill_delivery_top_right_mask(
        c,
        HDR_ROW1_DIVS[-1],
        FORM_HDR_MID,
        FORM_HDR_RIGHT - HDR_ROW1_DIVS[-1],
        FORM_HDR_TOP - FORM_HDR_MID,
    )
    _fill_delivery_mask(
        c,
        HDR_ROW2_DIVS[-1],
        FORM_HDR_BOT,
        FORM_HDR_RIGHT - HDR_ROW2_DIVS[-1],
        FORM_HDR_MID - FORM_HDR_BOT,
    )


def _draw_delivery_07_right_column_mask(c: rl_canvas.Canvas) -> None:
    """納品書の金額右側列をヘッダーから7行目までマスクする。"""
    _fill_delivery_right_round_mask(
        c,
        TBL_COLS[5],
        FORM_DETAIL_BOT,
        TBL_COLS[6] - TBL_COLS[5],
        FORM_HDR_BOT - FORM_DETAIL_BOT,
    )


def _draw_header_no_issue(c: rl_canvas.Canvas) -> None:
    """納品書/受領書向けヘッダー。発行日・仕上日・AM/PMはマスクして描く。"""
    _draw_delivery_header_masks(c)
    c.setLineWidth(1.0)
    c.drawPath(
        _top_round_rect_path(
            c,
            FORM_HDR_LEFT,
            FORM_HDR_BOT,
            FORM_HDR_RIGHT - FORM_HDR_LEFT,
            FORM_HDR_TOP - FORM_HDR_BOT,
            CORNER_R,
        ),
        stroke=1,
        fill=0,
    )
    c.setLineWidth(0.5)
    c.line(FORM_HDR_LEFT, FORM_HDR_MID, FORM_HDR_RIGHT, FORM_HDR_MID)
    for x in HDR_ROW1_DIVS:
        c.line(x, FORM_HDR_MID, x, FORM_HDR_TOP)
    for x in HDR_ROW2_DIVS:
        c.line(x, FORM_HDR_BOT, x, FORM_HDR_MID)

    c.setFont(_FONT_NAME, 6.0)

    def lbl(text: str, x: float, y: float) -> None:
        c.drawString(x + 1.5, y, text)

    r1_lbl_y = FORM_HDR_TOP - 8.0
    lbl("コードNo", FORM_HDR_LEFT, r1_lbl_y)
    lbl("得意先名", 90.0, r1_lbl_y)
    lbl("受注No", 284.0, r1_lbl_y)
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(279.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "御中")

    r2_lbl_y = FORM_HDR_MID - 8.0
    c.setFont(_FONT_NAME, 6.0)
    lbl("納品日", 90.0, r2_lbl_y)
    lbl("伝票No", 145.0, r2_lbl_y)
    lbl("取引区分", 197.0, r2_lbl_y)
    lbl("出荷区分", 245.0, r2_lbl_y)
    lbl("入力者名", 284.0, r2_lbl_y)


def _draw_company_detail_lines(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """会社名下の営業所/TEL/FAXを描画する。"""
    office_name = data.get("office_name", "")
    office_tel = data.get("office_tel", "")
    office_fax = data.get("office_fax", "")
    c.setFont(_FONT_NAME, 9.5)
    if office_name:
        c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 11.0, str(office_name))
    tel_fax = "  ".join(part for part in (
        f"TEL {office_tel}" if office_tel else "",
        f"FAX {office_fax}" if office_fax else "",
    ) if part)
    if tel_fax:
        c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y - 21.0, tel_fax)


def _draw_common_delivery_header(c: rl_canvas.Canvas, title: str, data: dict[str, Any]) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    c.setFont(_FONT_NAME, 16)
    c.drawCentredString(FORM_TITLE_X, FORM_TITLE_Y, title)
    c.setLineWidth(1.0)
    c.line(FORM_TITLE_X - FORM_TITLE_UL_HALF, FORM_TITLE_UL_Y,
           FORM_TITLE_X + FORM_TITLE_UL_HALF, FORM_TITLE_UL_Y)

    logo_y = COMPANY_NAME_Y + 5.2 - COMPANY_LOGO_H / 2
    c.drawImage(str(resource_path("assets/manekiya_logo.png")), COMPANY_LOGO_X, logo_y,
                width=COMPANY_LOGO_W, height=COMPANY_LOGO_H, mask="auto")
    c.setFont(_FONT_NAME, 16)
    c.drawString(COMPANY_INFO_X, COMPANY_NAME_Y, "まねきや硝子株式会社")
    _draw_company_detail_lines(c, data)
    _draw_header_no_issue(c)


def _draw_table_no_total(c: rl_canvas.Canvas, cols: list[float], labels: list[str]) -> None:
    table_left = cols[0]
    table_right = cols[-1]
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _top_round_rect_path(c, table_left, FORM_TBL_HDR_BOT,
                             table_right - table_left,
                             FORM_HDR_BOT - FORM_TBL_HDR_BOT,
                             CORNER_R),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    tbl_hdr_cy = (FORM_HDR_BOT + FORM_TBL_HDR_BOT) / 2 - 4.0
    for x1, x2, label in zip(cols, cols[1:], labels):
        c.drawCentredString((x1 + x2) / 2, tbl_hdr_cy, label)

    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    _draw_detail_outline_nototal(c, table_left, table_right)
    for x in cols[1:-1]:
        c.line(x, FORM_DETAIL_BOT, x, FORM_HDR_BOT)
    for i in range(1, FORM_DETAIL_ROWS):
        y = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        c.line(table_left, y, table_right, y)
    c.line(table_left, FORM_TBL_HDR_BOT, table_right, FORM_TBL_HDR_BOT)

    c.setFont(_FONT_NAME, 7.0)
    for i in range(FORM_DETAIL_ROWS):
        row_cy = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H - FORM_DETAIL_ROW_H / 2
        c.drawCentredString((table_left + cols[1]) / 2, row_cy - 3.0, str(i + 1))


def _draw_delivery_table_07(c: rl_canvas.Canvas) -> None:
    """納品書の明細表。金額右側列はマスクし、合計行には含めない。"""
    table_left = TBL_COLS[0]
    table_right = TBL_COLS[-1]
    total_right = TBL_COLS[5]
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _top_round_rect_path(c, table_left, FORM_TBL_HDR_BOT,
                             table_right - table_left,
                             FORM_HDR_BOT - FORM_TBL_HDR_BOT,
                             CORNER_R),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    tbl_hdr_cy = (FORM_HDR_BOT + FORM_TBL_HDR_BOT) / 2 - 4.0
    labels = ["No", "品　名", "数　量", "単　価", "金　額", ""]
    for x1, x2, label in zip(TBL_COLS, TBL_COLS[1:], labels):
        c.drawCentredString((x1 + x2) / 2, tbl_hdr_cy, label)
    _draw_delivery_07_right_column_mask(c)

    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    _draw_delivery_07_outline(c, table_left, table_right, total_right)
    for x in TBL_COLS[1:-1]:
        if x <= FORM_TOTAL_CELL_LEFT:
            bottom = FORM_DETAIL_BOT
        elif x == total_right:
            bottom = FORM_TOTAL_BOT + CORNER_R
        else:
            bottom = FORM_TOTAL_BOT
        c.line(x, bottom, x, FORM_HDR_BOT)
    for i in range(1, FORM_DETAIL_ROWS):
        y = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        c.line(table_left, y, total_right, y)
    c.line(table_left, FORM_TBL_HDR_BOT, total_right, FORM_TBL_HDR_BOT)
    c.line(table_left + CORNER_R, FORM_DETAIL_BOT, FORM_TOTAL_CELL_LEFT, FORM_DETAIL_BOT)
    c.line(FORM_TOTAL_CELL_RIGHT, FORM_DETAIL_BOT, table_right - CORNER_R, FORM_DETAIL_BOT)
    c.line(FORM_TOTAL_CELL_RIGHT, FORM_TOTAL_BOT, total_right - CORNER_R, FORM_TOTAL_BOT)

    c.setFont(_FONT_NAME, 7.0)
    for i in range(FORM_DETAIL_ROWS):
        row_cy = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H - FORM_DETAIL_ROW_H / 2
        c.drawCentredString((table_left + TBL_COLS[1]) / 2, row_cy - 3.0, str(i + 1))

    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _bottom_left_round_rect_path(
            c,
            FORM_TOTAL_CELL_LEFT,
            FORM_TOTAL_BOT,
            FORM_TOTAL_CELL_RIGHT - FORM_TOTAL_CELL_LEFT,
            FORM_TOTAL_ROW_H,
            CORNER_R,
        ),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    c.drawCentredString(
        (FORM_TOTAL_CELL_LEFT + FORM_TOTAL_CELL_RIGHT) / 2,
        FORM_TOTAL_BOT + (FORM_TOTAL_ROW_H - 8) / 2,
        "合　計",
    )
    c.setFillColorRGB(0, 0, 0)


def _draw_receipt_table_08(c: rl_canvas.Canvas) -> None:
    """受領書の明細表。受領印列は内部横線を描かない。"""
    receipt_cols = [TBL_COLS[0], TBL_COLS[1], TBL_COLS[2], TBL_COLS[3], TBL_COLS[6]]
    table_left = receipt_cols[0]
    table_right = receipt_cols[-1]
    receipt_left = receipt_cols[-2]
    c.setFillColorRGB(0, 0, 0)
    c.drawPath(
        _top_round_rect_path(c, table_left, FORM_TBL_HDR_BOT,
                             table_right - table_left,
                             FORM_HDR_BOT - FORM_TBL_HDR_BOT,
                             CORNER_R),
        stroke=0,
        fill=1,
    )
    c.setFillColorRGB(1, 1, 1)
    c.setFont(_FONT_NAME, 8.0)
    tbl_hdr_cy = (FORM_HDR_BOT + FORM_TBL_HDR_BOT) / 2 - 4.0
    for x1, x2, label in zip(receipt_cols, receipt_cols[1:], ["No", "品　名", "数　量", "受領印"]):
        c.drawCentredString((x1 + x2) / 2, tbl_hdr_cy, label)

    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    _draw_detail_outline_nototal(c, table_left, table_right)
    for x in receipt_cols[1:-1]:
        c.line(x, FORM_DETAIL_BOT, x, FORM_HDR_BOT)
    for i in range(1, FORM_DETAIL_ROWS):
        y = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        c.line(table_left, y, receipt_left, y)
    c.line(table_left, FORM_TBL_HDR_BOT, table_right, FORM_TBL_HDR_BOT)

    c.setFont(_FONT_NAME, 7.0)
    for i in range(FORM_DETAIL_ROWS):
        row_cy = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H - FORM_DETAIL_ROW_H / 2
        c.drawCentredString((table_left + receipt_cols[1]) / 2, row_cy - 3.0, str(i + 1))


def _draw_delivery_data_common(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_VAL = 7.8
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    _str(c, data.get("code_no", ""), FORM_HDR_LEFT + DATA_X_PAD, r1_y, FS_VAL)
    _str(c, data.get("customer_name", ""), 90.0 + DATA_X_PAD, r1_y, FS_VAL, max_w=178.0)
    _str(c, data.get("order_no", ""), 284.0 + DATA_X_PAD, r1_y, FS_VAL)

    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    _str(c, data.get("delivery_date", ""), 90.0 + DATA_X_PAD, r2_y, FS_VAL)
    _str(c, data.get("voucher_no", ""), 145.0 + DATA_X_PAD, r2_y, FS_VAL)
    _str(c, data.get("trade_type", ""), 197.0 + DATA_X_PAD, r2_y, FS_VAL)
    _str(c, data.get("ship_type", ""), 245.0 + DATA_X_PAD, r2_y, FS_VAL)
    _str(c, data.get("operator", ""), 284.0 + DATA_X_PAD, r2_y, FS_VAL, max_w=80.0)


def _draw_delivery_details_07(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_VAL = 7.8
    FS_DIM = 7.0
    unit_rx = TBL_COLS[4] - DATA_X_PAD
    amt_rx = TBL_COLS[5] - DATA_X_PAD
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET
        is_star = row.get("name", "") == "*"
        _str(c, row.get("name", ""), TBL_X_NAME, yu, FS_VAL, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, FS_DIM_LARGE, max_w=TBL_MAX_NAME)
        if is_star:
            continue
        _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
        _rstr(c, row.get("qty", ""), DET_QTY_RX, yl, FS_VAL, max_w=TBL_MAX_QTY)
        _rstr(c, row.get("unit_price", ""), unit_rx, yu, FS_VAL, max_w=TBL_MAX_UNIT)
        _rstr(c, row.get("amount", ""), amt_rx, yu, FS_VAL, max_w=TBL_MAX_AMT)


def _draw_delivery_details_08(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_VAL = 7.8
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET
        is_star = row.get("name", "") == "*"
        _str(c, row.get("name", ""), TBL_X_NAME, yu, FS_VAL, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, FS_DIM_LARGE, max_w=TBL_MAX_NAME)
        if is_star:
            continue
        _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
        _rstr(c, row.get("qty", ""), DET_QTY_RX, yl, FS_VAL, max_w=TBL_MAX_QTY)


def _draw_summary_rows(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_DIM = 7.0
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    c.setLineWidth(0.5)
    c.setFont(_FONT_NAME, 7.0)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_SUM_BOT + 3.0, "摘　要")
    c.line(note_line_x, FORM_SUM_BOT, FORM_SUM_RIGHT, FORM_SUM_BOT)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_BKNO_BOT + 3.0, "物件No")
    c.line(note_line_x, FORM_BKNO_BOT, FORM_SUM_RIGHT, FORM_BKNO_BOT)
    _draw_summary_lines(c, data, FS_DIM)
    for index, line in enumerate([line for line in data.get("property_lines", []) if line][:1]):
        _str(c, str(line), note_line_x, FORM_BKNO_BOT + 3.0 + index * 9.0, FS_DIM,
             max_w=FORM_SUM_RIGHT - note_line_x)
    sales_rep = str(data.get("sales_rep", "") or "")
    _str(c, f"営業担当：{sales_rep}", SUM_STAFF_X, FORM_SUM_BOT + 6.0, FS_DIM)
    construction_rep = str(data.get("construction_rep", "") or "")
    _str(c, f"工事担当：{construction_rep}", SUM_STAFF_X, FORM_BKNO_BOT + 4.0, FS_DIM)


def _draw_form_07(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    _draw_common_delivery_header(c, "納　品　書", data)
    _draw_delivery_table_07(c)
    _draw_delivery_data_common(c, data)
    _draw_delivery_details_07(c, data)
    _draw_summary_rows(c, data)
    _draw_special_notes_section(c)


def _draw_form_08(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    _draw_common_delivery_header(c, "受　領　書", data)
    _draw_receipt_table_08(c)
    _draw_delivery_data_common(c, data)
    _draw_delivery_details_08(c, data)
    _draw_summary_rows(c, data)
    _draw_delivery_stamp_boxes(c)
    _draw_special_notes_section(c)


def _build_scratch_delivery(data: dict[str, Any], voucher_id: str) -> bytes:
    _ensure_font()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    if voucher_id == "07":
        _draw_form_07(c, data)
    else:
        _draw_form_08(c, data)
    c.save()
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# オーバーレイ方式: 02-08 向け
# ══════════════════════════════════════════════════════════════════════════════

def _draw_template_title_overlay(c: rl_canvas.Canvas, voucher_id: str) -> None:
    """テンプレートPDFに焼き込まれた07/08タイトルだけを移動後位置に描き直す。"""
    titles = {"07": "納　品　書", "08": "受　領　書"}
    title = titles.get(voucher_id)
    if not title:
        return

    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.rect(100.0, FORM_TITLE_UL_Y - 1.0, 210.0, PAGE_H - FORM_TITLE_UL_Y + 1.0, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    c.setStrokeColorRGB(0, 0, 0)
    c.setFont(_FONT_NAME, 16)
    c.drawCentredString(FORM_TITLE_X, FORM_TITLE_Y, title)
    c.setLineWidth(1.0)
    c.line(FORM_TITLE_X - FORM_TITLE_UL_HALF, FORM_TITLE_UL_Y,
           FORM_TITLE_X + FORM_TITLE_UL_HALF, FORM_TITLE_UL_Y)
    c.restoreState()


def _draw_header_overlay(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    y1 = HEADER_ROW1_Y
    _str(c, data.get("code_no", ""),       HDR1_CODE_NO_X,  y1, FS_HEADER)
    _str(c, data.get("customer_name", ""), HDR1_CUSTOMER_X, y1, FS_HEADER, max_w=HDR1_CUSTOMER_MAX)
    _str(c, data.get("order_no", ""),      HDR1_ORDER_NO_X, y1, FS_HEADER)
    _str(c, data.get("shiage_date", ""),   HDR1_SHIAGE_X,   y1, FS_HEADER)
    y2 = HEADER_ROW2_Y
    _str(c, data.get("issue_date", ""),    HDR2_ISSUE_DATE_X, y2, FS_HEADER)
    _str(c, data.get("delivery_date", ""), HDR2_DELIVERY_X,   y2, FS_HEADER)
    _str(c, data.get("voucher_no", ""),    HDR2_VOUCHER_NO_X, y2, FS_HEADER)
    _str(c, data.get("trade_type", ""),    HDR2_TRADE_TYPE_X, y2, FS_HEADER)
    _str(c, data.get("ship_type", ""),     HDR2_SHIP_TYPE_X,  y2, FS_HEADER)
    _str(c, data.get("operator", ""),      HDR2_OPERATOR_X,   y2, FS_HEADER, max_w=HDR2_OPERATOR_MAX)


def _draw_details_overlay(c: rl_canvas.Canvas, details: list[dict[str, Any]]) -> None:
    note_mid_x = COL_NOTE_X + 63.5
    note_num_rx = note_mid_x - TBL_NOTE_MID_PAD
    note_text_x = note_mid_x + TBL_NOTE_MID_PAD
    note_data_x = note_text_x + 16.0
    note_left_max_w = note_mid_x - COL_NOTE_X - TBL_NOTE_MID_PAD
    note_right_max_w = COL_NOTE_X + MAX_W_NOTE - note_mid_x - TBL_NOTE_MID_PAD - DATA_X_PAD

    def draw_overlay_note_text(text: str, y: float) -> None:
        if not text:
            return
        if text == "加":
            _str(c, text, note_text_x, y, FS_NOTE, max_w=note_right_max_w)
        else:
            _str(c, text, note_data_x, y, FS_NOTE, max_w=note_right_max_w)

    for i, row in enumerate(details[:7]):
        row_top = DETAIL_ROW1_TOP - i * DETAIL_ROW_H
        yu = row_top - DETAIL_UPPER_OFFSET
        yl = row_top - DETAIL_LOWER_OFFSET
        _str(c, row.get("name", ""), COL_NAME_X, yu, FS_DETAIL, max_w=MAX_W_NAME)
        _str(c, row.get("dims", ""), COL_NAME_X, yl, FS_DIMS,   max_w=MAX_W_NAME)
        _str(c, row.get("qty_spec", ""), COL_QTY_X, yu, FS_DETAIL, max_w=MAX_W_QTY)
        _str(c, row.get("qty", ""),      COL_QTY_X, yl, FS_DETAIL, max_w=MAX_W_QTY)
        _str(c, row.get("unit_price", ""), COL_UNIT_X,   yu, FS_DETAIL, max_w=MAX_W_UNIT)
        _str(c, row.get("amount", ""),     COL_AMOUNT_X, yu, FS_DETAIL, max_w=MAX_W_AMOUNT)
        notes = row.get("note_lines", [])
        if notes:
            note_rows = _split_note_rows(notes[0])
            num, txt = note_rows[0]
            _rstr(c, num, note_num_rx, yu, FS_NOTE, max_w=note_left_max_w)
            draw_overlay_note_text(txt, yu)
            if len(note_rows) > 1 and len(notes) == 1:
                num, txt = note_rows[1]
                _rstr(c, num, note_num_rx, yl, FS_NOTE, max_w=note_left_max_w)
                draw_overlay_note_text(txt, yl)
        finish_date = row.get("finish_date", "")
        if finish_date:
            _str(c, finish_date, note_data_x, yu, FS_NOTE, max_w=note_right_max_w)
        if len(notes) > 1:
            num, txt = _split_note_rows(notes[1])[0]
            _rstr(c, num, note_num_rx, yl, FS_NOTE, max_w=note_left_max_w)
            draw_overlay_note_text(txt, yl)


def _build_overlay(page_width: float, page_height: float, voucher_id: str,
                   data: dict[str, Any]) -> bytes:
    _ensure_font()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))
    _draw_template_title_overlay(c, voucher_id)
    _draw_header_overlay(c, data)
    _draw_details_overlay(c, data.get("details", []))
    c.save()
    return buf.getvalue()


def _overlay_on_template(template_pdf_path: Path, overlay_bytes: bytes) -> pypdf.PageObject:
    template_reader = pypdf.PdfReader(str(template_pdf_path))
    overlay_reader = pypdf.PdfReader(io.BytesIO(overlay_bytes))
    page = template_reader.pages[0]
    page.merge_page(overlay_reader.pages[0])
    return page


# ── PDF バイト列組み立て（内部共通処理）──────────────────────────────────────

def _assemble_pdf_bytes(
    voucher_ids: list[str],
    print_data: dict[str, Any],
    base_dir: Path | None,
) -> bytes:
    """指定された伝票種別のページを結合した PDF を bytes で返す。ファイル保存しない。"""
    writer = pypdf.PdfWriter()
    for page_data in _normalize_pages_data(print_data):
        # 指図書(1)/指図書(2)/梱包明細書 に重ねる編集オブジェクトを受注Noで解決する。
        edit_objects = _resolve_edit_objects(page_data)
        for vid in voucher_ids:
            if vid == "01":
                page_bytes = _build_scratch_01(page_data)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "02":
                page_bytes = _build_scratch_01(page_data, title="工　場　控")
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "03":
                page_bytes = _build_scratch_shizu(page_data, title="指　図　書　(1)", stamp_title="工場印",
                                                  edit_objects=edit_objects)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "04":
                page_bytes = _build_scratch_shizu(page_data, title="指　図　書　(2)", stamp_title="商品課印",
                                                  edit_objects=edit_objects)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "05":
                page_bytes = _build_scratch_shizu(page_data, title="梱　包　明　細　書", stamp_title="配送者印",
                                                  edit_objects=edit_objects)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "06":
                page_bytes = _build_scratch_shizu(page_data, title="配　送　指　示　書", stamp_title="配送者印")
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid in ("07", "08"):
                page_bytes = _build_scratch_delivery(page_data, vid)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            else:
                tpl = template_path(vid, base_dir)
                reader = pypdf.PdfReader(str(tpl))
                page = reader.pages[0]
                pw = float(page.mediabox.width)
                ph = float(page.mediabox.height)
                overlay = _build_overlay(pw, ph, vid, page_data)
                writer.add_page(_overlay_on_template(tpl, overlay))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _resolve_edit_objects(page_data: dict[str, Any]) -> list[dict[str, Any]]:
    """ページデータから指図書編集オブジェクトを解決する。

    page_data に "edit_objects" があればそれを優先（事前読み込み済み）。
    無ければ受注Noをキーに保存済みオブジェクトを読み込む。
    """
    preset = page_data.get("edit_objects")
    if isinstance(preset, list):
        return preset
    order_no = str(page_data.get("order_no") or "").strip()
    if not order_no:
        return []
    try:
        from app.voucher_edit_objects import load_edit_objects
        return load_edit_objects(order_no)
    except Exception:
        return []


def _normalize_pages_data(print_data: dict[str, Any]) -> list[dict[str, Any]]:
    pages = print_data.get("pages")
    if isinstance(pages, list) and pages:
        return [page for page in pages if isinstance(page, dict)]
    return [print_data]


def _check_templates(voucher_ids: list[str], base_dir: Path | None) -> None:
    """オーバーレイ方式のテンプレートファイル存在チェック。"""
    missing: list[str] = []
    for vid in voucher_ids:
        if vid in ("01", "02", "03", "04", "05", "06", "07", "08"):
            continue
        tpl = template_path(vid, base_dir)
        if not tpl.exists():
            missing.append(f"{VOUCHER_NAMES.get(vid, vid)} ({tpl})")
    if missing:
        raise FileNotFoundError(
            "以下のテンプレートPDFが見つかりません:\n" + "\n".join(missing)
        )


# ── 公開 API ──────────────────────────────────────────────────────────────────

def merge_pdf_bytes(pdf_bytes_list: list[bytes]) -> bytes:
    """複数のPDFバイト列を1つのPDFバイト列に結合する。（選択行の一括出力用）"""
    parts = [b for b in pdf_bytes_list if b]
    if not parts:
        raise ValueError("結合対象のPDFがありません。")
    if len(parts) == 1:
        return parts[0]
    writer = pypdf.PdfWriter()
    for b in parts:
        reader = pypdf.PdfReader(io.BytesIO(b))
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def save_pdf_bytes(
    pdf_bytes: bytes,
    output_dir: Path | None = None,
    base_dir: Path | None = None,
    filename_token: str = "multi",
) -> Path:
    """PDFバイト列をタイムスタンプ付きファイルとして保存し、パスを返す。"""
    if output_dir is None:
        output_dir = get_default_voucher_output_dir(base_dir)
    output_dir = ensure_voucher_output_dir(output_dir)

    output_path = _build_output_pdf_path(output_dir, filename_token)
    try:
        with open(output_path, "wb") as fp:
            fp.write(pdf_bytes)
    except OSError as exc:
        raise RuntimeError(
            "PDF保存に失敗しました。\n"
            "PDF出力先に書き込みできるか確認してください。\n\n"
            f"対象パス:\n{output_path}\n\n詳細:\n{exc}"
        ) from exc
    return output_path


def create_vouchers_pdf(
    voucher_ids: list[str],
    data: dict[str, Any] | None = None,
    output_dir: Path | None = None,
    base_dir: Path | None = None,
) -> Path:
    """選択された伝票のPDFをファイルに保存して返す。（PDF作成ボタン用）

    Args:
        voucher_ids: 出力する伝票種別IDのリスト。
        data: 印字データ辞書。None の場合はダミーデータを使用する。
        output_dir: 出力先ディレクトリ。None の場合は安全な既定出力先を使う。
        base_dir: プロジェクトルート。None の場合は自動解決する。

    Returns:
        生成したPDFのパス。
    """
    if not voucher_ids:
        raise ValueError("伝票が1つも選択されていません。印刷する伝票を選択してください。")

    print_data = data if data is not None else DUMMY_DATA
    _check_templates(voucher_ids, base_dir)

    if output_dir is None:
        output_dir = get_default_voucher_output_dir(base_dir)
    output_dir = ensure_voucher_output_dir(output_dir)

    output_path = _build_output_pdf_path(output_dir, _filename_token_from_print_data(print_data))

    try:
        pdf_bytes = _assemble_pdf_bytes(voucher_ids, print_data, base_dir)
    except (FileNotFoundError, ValueError, RuntimeError):
        raise
    except Exception as exc:
        raise RuntimeError(f"PDF生成に失敗しました: {exc}") from exc

    try:
        with open(output_path, "wb") as fp:
            fp.write(pdf_bytes)
    except OSError as exc:
        raise RuntimeError(
            "PDF保存に失敗しました。\n"
            "PDF出力先に書き込みできるか確認してください。\n\n"
            f"対象パス:\n{output_path}\n\n詳細:\n{exc}"
        ) from exc

    return output_path


def _build_output_pdf_path(output_dir: Path, token: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_token = _sanitize_filename_token(token) or "unknown"
    path = output_dir / f"{timestamp}_{safe_token}.pdf"
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = output_dir / f"{timestamp}_{safe_token}_{index}.pdf"
        if not candidate.exists():
            return candidate
        index += 1


def _filename_token_from_print_data(print_data: dict[str, Any]) -> str:
    candidates: list[Any] = []
    pages = print_data.get("pages")
    if isinstance(pages, list) and pages:
        first_page = pages[0]
        if isinstance(first_page, dict):
            candidates.extend([
                first_page.get("voucher_no"),
                first_page.get("delivery_no"),
                first_page.get("order_no"),
            ])
    candidates.extend([
        print_data.get("voucher_no"),
        print_data.get("delivery_no"),
        print_data.get("order_no"),
    ])
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return "unknown"


def _sanitize_filename_token(token: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", str(token or "").strip())


def build_vouchers_pdf_bytes(
    voucher_ids: list[str],
    data: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> bytes:
    """選択された伝票のPDFをバイト列で返す。ファイル保存しない。（印刷ボタン用）

    Args:
        voucher_ids: 出力する伝票種別IDのリスト。
        data: 印字データ辞書。None の場合はダミーデータを使用する。
        base_dir: プロジェクトルート。None の場合は自動解決する。

    Returns:
        PDFのバイト列。
    """
    if not voucher_ids:
        raise ValueError("伝票が1つも選択されていません。印刷する伝票を選択してください。")

    print_data = data if data is not None else DUMMY_DATA
    _check_templates(voucher_ids, base_dir)

    try:
        return _assemble_pdf_bytes(voucher_ids, print_data, base_dir)
    except (FileNotFoundError, ValueError, RuntimeError):
        raise
    except Exception as exc:
        raise RuntimeError(f"PDF生成に失敗しました: {exc}") from exc
