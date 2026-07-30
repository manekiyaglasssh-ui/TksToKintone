"""伝票PDF作成サービス。

売上伝票(01), 工場控(02), 指図書系(03-06), 納品書/受領書(07-08):
reportlab でフォームを一から描画（アプリ描画方式）。

公開 API:
    create_vouchers_pdf(...)   - ファイル保存あり（PDF作成ボタン用）
    build_vouchers_pdf_bytes(...)  - ファイル保存なし（印刷ボタン用）
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import os
import platform
import re
import struct
import subprocess
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import FF_FORCEBOLD, FF_ITALIC, TTFont, TTFontFace
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader

import pypdf

from app.config import resource_path
from app.line_decorations import line_segments, normalize_line_type
from app.path_utils import ensure_voucher_output_dir, get_default_voucher_output_dir
from app.processing_display_names import (
    load_processing_display_names,
    resolve_processing_display_name,
)
from app.voucher_settings import (
    PRICE_DISPLAY_ALWAYS_HIDE,
    PRICE_DISPLAY_ALWAYS_SHOW,
    PRICE_DISPLAY_CONDITIONAL,
    load_price_display_mode,
    normalize_price_display_mode,
)
from app.voucher_data_mapper import (
    build_qr_code_image,
    is_quantity_hidden_by_unit_code,
    quantity_unit_code_value,
    should_draw_upper_area_by_op_category,
)
from app.voucher_templates import (
    DUMMY_DATA,
    PAGE_W, PAGE_H,
    VOUCHER_NAMES,
    template_path,
    # アプリ描画方式 レイアウト定数
    FORM_ML, FORM_MR, FORM_MB, CORNER_R,
    FORM_TITLE_X, FORM_TITLE_Y, FORM_TITLE_UL_Y, FORM_TITLE_UL_HALF,
    GEN_CIRCLE_X, NOKI_LINE_X, STAMP_X, STAMP_W, STAMP_H, STAMP_GAP,
    DELIV_STAMP_W, DELIV_STAMP_H, DELIV_STAMP_GAP,
    COMPANY_LOGO_H, COMPANY_LOGO_W, COMPANY_LOGO_X, COMPANY_NAME_Y, COMPANY_INFO_X,
    FORM_HDR_TOP, FORM_HDR_MID, FORM_HDR_BOT, FORM_HDR_LEFT, FORM_HDR_RIGHT,
    HDR_ROW1_DIVS, HDR_ROW2_DIVS, HDR_DELIVERY_X,
    HDR_DELIVERY_RIGHT, HDR_VOUCHER_RIGHT, HDR_TRADE_RIGHT,
    HDR_OPERATOR_X, HDR_ORDER_NO_X, HDR_AMPM_X,
    HDR_SHIAGE_LABEL_Y, HDR_SHIAGE_MONTH_DAY_Y,
    HDR_SHIAGE_DATA_FS, HDR_SHIAGE_LABEL_FS,
    HDR_SHIAGE_DAY_LABEL_RX, HDR_SHIAGE_MONTH_LABEL_CX,
    HDR_SHIAGE_MONTH_DATA_RX, HDR_SHIAGE_DAY_DATA_RX,
    FORM_DETAIL_ROWS, FORM_DETAIL_ROW_H, FORM_TBL_HDR_BOT,
    FORM_DETAIL_BOT, FORM_TOTAL_ROW_H, FORM_TOTAL_BOT,
    FORM_TOTAL_CELL_LEFT, FORM_TOTAL_CELL_RIGHT,
    TBL_COLS, TBL_COL_LABELS,
    SHIZU_TBL_COLS, SHIZU_COL_LABELS, SHIZU_MAX_W_NYUKI,
    DATA_X_PAD, HDR_DATA_Y_INNER, DET_UPPER_OFFSET, DET_LOWER_OFFSET, DET_QTY_LOWER_OFFSET,
    TBL_X_NAME, TBL_X_QTY, TBL_X_UNIT, TBL_X_AMT, TBL_X_NOTE,
    TBL_MAX_NAME, TBL_MAX_QTY, TBL_MAX_UNIT, TBL_MAX_AMT, TBL_MAX_NOTE,
    DET_NAME_RX, DET_QTY_RX, FS_DIM_LARGE, DIM_SHIFT_LEFT,
    TBL_NOTE_MID_X, TBL_NOTE_MID_PAD,
    FORM_SUM_GAP, FORM_SUM_TOP, FORM_SUM_BOT,
    FORM_BKNO_TOP, FORM_BKNO_BOT,
    FORM_SUM_RIGHT, FORM_SUBROW_LBL_W, SUM_STAFF_X,
    TAX_Y,
    CUSTOMER_ORDER_NO_LABEL, CUSTOMER_ORDER_NO_FONT_SIZE,
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
_EDIT_FONT_CACHE: dict[tuple[Any, ...], str] = {}
_EDIT_FONT_METADATA: dict[tuple[str, bool, bool], dict[str, Any]] = {}
_EDIT_TTC_FACE_CACHE: dict[tuple[str, int, str, bool, bool], int | None] = {}
_EDIT_CMAP_CACHE: dict[tuple[str, int, int], tuple[tuple[int, int], ...]] = {}
_REGISTERED_EDIT_FONT_IDENTITIES: dict[str, tuple[Any, ...]] = {}
_WINDOWS_FONT_REGISTRY_CACHE: tuple[tuple[str, Path, bool, bool], ...] | None = None
_PDF_FONT_CACHE_LOCK = threading.RLock()
PDF_TEXT_RENDERER_REVISION = 5
PDF_FONT_REGISTRATION_METHOD = "reportlab_ttfont_subfont_v3"
DELIVERY_MASK_RGB = (0.7, 0.7, 0.7)
_log = logging.getLogger("tks_to_kintone_app")


def _price_display_mode(data: dict[str, Any]) -> str:
    """ページ指定を優先し、無ければ永続設定から単価表示モードを得る。"""
    cached = data.get("_resolved_price_display_mode")
    if cached in (PRICE_DISPLAY_CONDITIONAL, PRICE_DISPLAY_ALWAYS_SHOW, PRICE_DISPLAY_ALWAYS_HIDE):
        return str(cached)
    explicit = data.get("price_display_mode")
    mode = normalize_price_display_mode(explicit) if explicit is not None else load_price_display_mode()
    data["_resolved_price_display_mode"] = mode
    event = {
        PRICE_DISPLAY_ALWAYS_SHOW: "voucher_price_display_forced_show",
        PRICE_DISPLAY_ALWAYS_HIDE: "voucher_price_display_forced_hide",
        PRICE_DISPLAY_CONDITIONAL: "voucher_price_display_conditional",
    }[mode]
    _log.info("%s order_no=%s", event, data.get("order_no", ""))
    return mode


def resolve_price_amount_visibility(existing_visible: bool, mode: object) -> bool:
    """単価・明細金額・金額列合計で共有する3モードの表示判定。"""
    normalized = normalize_price_display_mode(mode)
    if normalized == PRICE_DISPLAY_ALWAYS_SHOW:
        return True
    if normalized == PRICE_DISPLAY_ALWAYS_HIDE:
        return False
    return bool(existing_visible)


def should_draw_price_amount(data: dict[str, Any], existing_visible: bool = True) -> bool:
    """ページのprice_display_modeを使って価格関連項目の最終表示可否を返す。"""
    return resolve_price_amount_visibility(existing_visible, _price_display_mode(data))


def price_amount_text_for_mode(
    data: dict[str, Any], value: object, *, existing_visible: bool = True
) -> str:
    """設定が表示可の場合だけ既存表示値を返す。空値から0等は生成しない。"""
    if not should_draw_price_amount(data, existing_visible):
        return ""
    return "" if value is None else str(value)


def unit_price_text_for_mode(data: dict[str, Any], value: object) -> str:
    """旧参照互換を保つ単価表示値ヘルパ。"""
    return price_amount_text_for_mode(data, value)


def line_amount_text_for_mode(data: dict[str, Any], value: object) -> str:
    """マッピング済みの正式な明細金額表示値へ共通モードを適用する。"""
    return price_amount_text_for_mode(data, value)

# ── ラベル用フォントとデータ用（太字）フォント（要件: 取得データを太字化）──────────
# 固定ラベル・見出しは従来フォント（LABEL_FONT_NAME）で描く。OLAP取得データや
# 画面入力値など「データ部分」は DATA_BOLD_FONT_NAME を使い太字化する。
# 日本語CIDフォント（HeiseiKakuGo-W5）には対応する太字ウェイトが無いため、
# 既存Gothic系フォントをベースに擬似太字（横方向の多重描画）で太らせる。描画位置・
# 文字サイズ・自動縮小ロジックは変えない。DATA_BOLD_FONT_NAME はセンチネルで、実際の
# c.setFont / stringWidth には _FONT_NAME（登録済みCIDフォント）を用いる。
LABEL_FONT_NAME = _FONT_NAME
DATA_FONT_NAME = _FONT_NAME
DATA_BOLD_FONT_NAME = "HeiseiKakuGo-W5-DataBold"
# 疑似太字の横方向オフセット量（ReportLabの描画単位であるpt）。
# 実機印刷では太くなり過ぎたため 0.3 → 0.15（半分）に弱める。全データ共通の強度で、
# 表内データ・ヘッダーデータで差を付けない。太字化はフォント変更ではなく、同一の
# _FONT_NAME を微小オフセットで重ね描きするのみ。今後の調整用に定数化する。
TEXT_SYNTHETIC_BOLD_OFFSET_PT = 0.15
# 正式Italicフェイスが無い場合に文字だけへ適用する横シアー。
TEXT_SYNTHETIC_ITALIC_SHEAR = 0.20
# 既存の帳票データ太字テスト／外部参照との互換名。値の責務は上の文字描画共通定数に集約する。
DATA_BOLD_OFFSET_PT = TEXT_SYNTHETIC_BOLD_OFFSET_PT

# 日本語/CJK glyphを正式Italic faceのmetadataだけで判断しないための共通範囲。
# Halfwidth and Fullwidth Formsは全角英数字・記号に加えて半角カナも含む。
_CJK_UNICODE_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2FFF),    # CJK Radicals / Kangxi Radicals / IDC
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0x3040, 0x309F),    # Hiragana
    (0x30A0, 0x30FF),    # Katakana
    (0x3100, 0x312F),    # Bopomofo
    (0x31A0, 0x31BF),    # Bopomofo Extended
    (0x31C0, 0x31EF),    # CJK Strokes
    (0x31F0, 0x31FF),    # Katakana Phonetic Extensions
    (0x3200, 0x33FF),    # Enclosed CJK Letters / CJK Compatibility
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),    # CJK Compatibility Forms
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
    (0x20000, 0x2FA1F),  # CJK Extensions B-I / Compatibility Supplement
    (0x30000, 0x323AF),  # CJK Extensions G-I
)


def is_cjk_character(char: str) -> bool:
    """日本語・CJK・全角互換文字ならTrueを返す。"""
    if not char:
        return False
    codepoint = ord(char[0])
    return any(start <= codepoint <= end for start, end in _CJK_UNICODE_RANGES)


def contains_cjk(text: str) -> bool:
    """文字列に日本語・CJK・全角互換文字が1文字でも含まれるか判定する。"""
    return any(is_cjk_character(char) for char in str(text or ""))


def _resolve_base_font(font_name: str | None) -> str:
    """描画/幅計算に使う実登録フォント名へ解決する（太字センチネルは _FONT_NAME）。"""
    if not font_name or font_name == DATA_BOLD_FONT_NAME:
        return _FONT_NAME
    return font_name


def _is_bold_font(font_name: str | None) -> bool:
    return font_name == DATA_BOLD_FONT_NAME


def _emit_text(c: "rl_canvas.Canvas", method_name: str, x: float, y: float,
               text: str, bold: bool) -> None:
    """指定した描画メソッドでテキストを描く。太字時は横方向に多重描画する。

    ベースライン(y)とアンカー(x)は動かさず、擬似太字は +x 方向の微小オフセットを
    重ねて実現する（CIDフォントに太字ウェイトが無いための代替）。
    """
    _draw_pdf_text(c, method_name, x, y, text, synthetic_bold=bold)


def draw_styled_pdf_text(
    c: "rl_canvas.Canvas", text: str, anchor_x: float, baseline_y: float,
    pdf_font_name: str | None = None, font_size: float | None = None, *,
    text_align: str = "left", synthetic_bold: bool = False,
    synthetic_italic: bool = False,
    trace_id: str = "", object_id: object = None,
    requested_italic: bool | None = None, edit_objects_sha256: str = "",
) -> None:
    """編集文字の実glyph描画と疑似Bold/Italicを同じgraphics stateで行う。"""
    if trace_id and object_id is not None:
        _log.info(
            "event=draw_styled_pdf_text trace_id=%s object_id=%s "
            "font_italic=%s synthetic_bold_used=%s synthetic_italic_used=%s "
            "edit_objects_sha256=%s",
            trace_id, object_id, requested_italic, synthetic_bold,
            synthetic_italic, edit_objects_sha256,
        )
    if pdf_font_name is not None and font_size is not None:
        c.setFont(pdf_font_name, font_size)
    alignment = text_align if text_align in {"left", "center", "right"} else "left"
    if not synthetic_italic:
        method_name = {
            "left": "drawString", "center": "drawCentredString",
            "right": "drawRightString",
        }[alignment]
        method = getattr(c, method_name)
        method(anchor_x, baseline_y, text)
        if synthetic_bold:
            method(anchor_x + TEXT_SYNTHETIC_BOLD_OFFSET_PT, baseline_y, text)
        return

    size = float(font_size or 0.0)
    try:
        width = float(pdfmetrics.stringWidth(text, pdf_font_name, size)) if (
            pdf_font_name and size > 0.0) else 0.0
    except Exception:
        width = float(len(text)) * size
    overhang = abs(TEXT_SYNTHETIC_ITALIC_SHEAR) * size
    if alignment == "right":
        local_x = -width - overhang
    elif alignment == "center":
        local_x = -(width + overhang) / 2.0
    else:
        local_x = 0.0

    c.saveState()
    try:
        c.translate(anchor_x, baseline_y)
        c.transform(1, 0, TEXT_SYNTHETIC_ITALIC_SHEAR, 1, 0, 0)
        # ReportLabのdrawRightString/drawCentredStringが内部で作るtext matrixを
        # 介さず、変換後のローカル座標でactual glyphを必ず1回描く。
        c.drawString(local_x, 0, text)
        if synthetic_bold:
            c.drawString(local_x + TEXT_SYNTHETIC_BOLD_OFFSET_PT, 0, text)
    finally:
        c.restoreState()


def _draw_pdf_text(c: "rl_canvas.Canvas", method_name: str, x: float, y: float,
                   text: str, *, synthetic_bold: bool = False,
                   synthetic_italic: bool = False) -> None:
    """旧内部API互換。編集文字は draw_styled_pdf_text に集約する。"""
    alignment = {
        "drawString": "left", "drawCentredString": "center",
        "drawRightString": "right",
    }.get(method_name, "left")
    draw_styled_pdf_text(
        c, text, x, y, text_align=alignment,
        synthetic_bold=synthetic_bold, synthetic_italic=synthetic_italic)

# ── データ部分の文字サイズ（要件2）─────────────────────────────────────────────
# OLAPデータ・画面入力値など「データ部分」だけを拡大する。タイトル・社名・各ラベル
# （FS_LBL=6.0 など）・加工名ラベル・固定文言・罫線はこの定数を使わず据え置く。
DATA_FONT_SIZE = 8.6          # 旧 FS_VAL=7.8（得意先名/受注No/品名/数量/単価/金額 等）
DETAIL_DATA_FONT_SIZE = 7.8   # 旧 FS_DIM=7.0（摘要/物件No/担当/受注見出摘要 等）
NYUKI_DATA_FONT_SIZE = 7.0    # 旧 FS_NYUKI=6.5（指図書系 受入日列）

# ── 明細データ拡大フォント（要件1・2）─────────────────────────────────────────
# 品名列2段目の寸法表示「（○○ * ○○ ミリ）」を従来比1.5倍にする。基準は従来の
# 寸法フォント FS_DIM_LARGE。品名1段目・加工名・摘要・ヘッダー文字は据え置く。
DETAIL_DIM_FONT_SIZE = FS_DIM_LARGE * 1.5          # 寸法表示専用（従来 FS_DIM_LARGE の1.5倍）
# 数量のデータ部分を従来比1.5倍にする。基準は従来のデータフォント DATA_FONT_SIZE。
# 列ヘッダー（数量/単価/金額）は据え置く。
DETAIL_QTY_VALUE_FONT_SIZE = DATA_FONT_SIZE * 1.5  # 数量データ専用
# 単価・金額のデータ部分（旧 1.5倍）を現在比 0.8倍へ縮小する（結果的に基準の約1.2倍）。
DETAIL_UNIT_PRICE_FONT_SIZE = DATA_FONT_SIZE * 1.5 * 0.8  # 単価データ専用
DETAIL_AMOUNT_FONT_SIZE = DATA_FONT_SIZE * 1.5 * 0.8      # 金額データ専用

# 品名一段目（商品名）を従来比1.2倍にする（要件3）。
# 二段目の寸法表示（DETAIL_DIM_FONT_SIZE）・品名ヘッダーは対象外。
DETAIL_NAME_FONT_SIZE = DATA_FONT_SIZE * 1.2       # 品名1段目（商品名）専用
# 品名（商品名称）が品名列幅を超える場合の自動縮小の目安下限。ここまでは段階的に
# フォントを下げて読みやすさを優先し、それでも収まらない長い名称は下限を割ってでも
# 全文字を表示する（途中切り捨て・省略記号は使わない）。
DETAIL_NAME_MIN_FONT_SIZE = 5.0
# 摘要列データを従来比1.2倍にする（要件4）。摘要ヘッダー・物件No/担当等は据え置く。
DETAIL_NOTE_FONT_SIZE = DETAIL_DATA_FONT_SIZE * 1.2  # 摘要列データ専用
# 表の摘要列フォント（売上伝票）。指図書系(03-06)の右端「受入日」列もこれに揃える。
TABLE_REMARK_FONT_SIZE = DETAIL_NOTE_FONT_SIZE

# ── ヘッダーデータ拡大フォント（要件1）──────────────────────────────────────────
# 上部ヘッダーのデータ部分（コードNo/得意先名/受注No/発行日/伝票No/入力者名/仕上日/
# 納品日/出荷区分）を現在の基準サイズから1.3倍にする。ラベル文字（コードNo・得意先名
# など）と取引区分データは据え置く。
HEADER_MAIN_VALUE_FONT_SIZE = DATA_FONT_SIZE * 1.3           # 主要ヘッダーデータ専用
HEADER_NOUHIN_VALUE_FONT_SIZE = DATA_FONT_SIZE * 1.3          # 納品日データ専用
HEADER_SHIPPING_VALUE_FONT_SIZE = DATA_FONT_SIZE * 1.3        # 出荷区分データ専用
# 取引区分データは出荷区分データと同じサイズ（1.3倍）にする（要件1）。ラベルは据え置き。
HEADER_TRADE_VALUE_FONT_SIZE = HEADER_SHIPPING_VALUE_FONT_SIZE  # 取引区分データ専用
# 得意先名データは基準の1.2倍からさらに1.2倍（＝基準の1.44倍）にする。ラベルは据え置き。
HEADER_CUSTOMER_VALUE_FONT_SIZE = DATA_FONT_SIZE * 1.2 * 1.2  # 得意先名データ専用
HEADER_FINISH_DATE_VALUE_FONT_SIZE = HDR_SHIAGE_DATA_FS * 1.3  # 仕上日の月・日データ専用

# ── ヘッダー値内の「I」の字間補正 ──────────────────────────────────────────────
# コードNo/伝票No/受注No の各「I」の直後に後続文字がある場合だけ、その後続文字
# 以降を右へずらす。値の開始位置、枠線・ラベル・他項目位置は変更しない。
HEADER_I_CHAR_GAP_PT: float = 4.0

# 中央の「摘要」「物件No」のデータだけを現在サイズから1.1倍にする。
# ラベル文字「摘　要」「物件No」は据え置く。
SUMMARY_PREVIOUS_TEXT_SCALE = 0.8
PROPERTY_PREVIOUS_TEXT_SCALE = 0.8
SUMMARY_TEXT_SCALE = SUMMARY_PREVIOUS_TEXT_SCALE * 1.1
PROPERTY_TEXT_SCALE = PROPERTY_PREVIOUS_TEXT_SCALE * 1.1
SUMMARY_VALUE_BASE_FONT_SIZE = DETAIL_DATA_FONT_SIZE * 1.3
PROPERTY_VALUE_BASE_FONT_SIZE = DETAIL_DATA_FONT_SIZE * 1.3
SUMMARY_VALUE_FONT_SIZE = SUMMARY_VALUE_BASE_FONT_SIZE * SUMMARY_TEXT_SCALE
PROPERTY_VALUE_FONT_SIZE = PROPERTY_VALUE_BASE_FONT_SIZE * PROPERTY_TEXT_SCALE

# 摘要は従来の下線右端まで、担当者名データはその右側へ表示する。
# 営業担当・工事担当の固定ラベルは描画しない。
SUMMARY_TEXT_RIGHT = FORM_SUM_RIGHT
STAFF_TEXT_RIGHT = STAMP_X - 8.0
STAFF_PREVIOUS_X = SUM_STAFF_X
STAFF_SHIFT_RIGHT = 28.35  # 約1cm
STAFF_TEXT_X = STAFF_PREVIOUS_X + STAFF_SHIFT_RIGHT
# 配送コース名称＋営業担当者名は1つの文字列として右寄せする。左端は旧配送コース
# 領域を利用し、担当者名だけを描いていた時の右端をアンカーにする。
DELIVERY_COURSE_GAP = 3.0
DELIVERY_COURSE_MAX_W = 100.0
DELIVERY_COURSE_X = STAFF_TEXT_X - DELIVERY_COURSE_GAP - DELIVERY_COURSE_MAX_W
# 営業担当者名は摘要下段、工事担当者名は物件No行と同じベースラインに置く。
SALES_REP_Y = FORM_SUM_BOT + 3.0
CONSTRUCTION_REP_Y = FORM_BKNO_BOT + 3.0

# 加工名一覧のラベルを基準(6.5)の1.2倍にする（要件5）。チェック欄・外枠は据え置く。
PROCESS_LABEL_BASE_FONT_SIZE = 6.5
PROCESS_LABEL_FONT_SIZE = PROCESS_LABEL_BASE_FONT_SIZE * 1.2  # 加工名ラベル専用

# AM・PM 丸印の線幅。従来 0.9 の2倍へ太くする（前回調整済み・維持）。
AMPM_CIRCLE_LINE_WIDTH = 1.8
# 「AM・PM」表示文字のフォントサイズ。基準11.0を1.2倍にする（要件1）。
AMPM_TEXT_BASE_FONT_SIZE = 11.0
AMPM_TEXT_FONT_SIZE = AMPM_TEXT_BASE_FONT_SIZE * 1.2
# AM/PMを囲う丸印の半径倍率。基準（基準フォント時の半径）を1.2倍にする（要件2）。
# 線幅は AMPM_CIRCLE_LINE_WIDTH で別管理（今回は変更しない）。
AMPM_CIRCLE_SCALE = 1.2
# 「AM・PM」表示のベースラインY。従来は FORM_HDR_BOT+8.0（やや上寄り）だったが、
# 行2セル(FORM_HDR_BOT〜FORM_HDR_MID)の縦中央へ下げて中央表示にする。
# fs*0.35 は全角文字の視覚中心をベースラインへ補正する係数。丸印もこのYに追従する。
AMPM_BASELINE_Y = (FORM_HDR_BOT + FORM_HDR_MID) / 2 - AMPM_TEXT_FONT_SIZE * 0.35

# 得意先名データの右端に置く「殿」「御中」用の確保幅（pt）。得意先名が長くても
# この分を残してクリップし、敬称と重ならないようにする（要件3）。
CUSTOMER_HONORIFIC_RESERVE = 20.0


def _customer_max_w() -> float:
    """得意先名データの最大幅。受注Noセル左端の手前で「殿/御中」分を残してクリップする。"""
    start = HDR_ROW1_DIVS[0] + DATA_X_PAD
    return HDR_ORDER_NO_X - CUSTOMER_HONORIFIC_RESERVE - start


# 反映先伝票の既定（旧データ互換: 指図書(1)/指図書(2)/梱包明細書）。
DEFAULT_EDIT_TARGET_VOUCHERS = ("03", "04", "05")


def _object_target_vouchers(obj: dict[str, Any]) -> list[str]:
    """編集オブジェクトの反映先伝票を返す。未設定なら ["03","04","05"]（要件7 旧互換）。"""
    targets = obj.get("target_vouchers")
    if isinstance(targets, list) and targets:
        return [str(v).strip() for v in targets if str(v).strip()]
    return list(DEFAULT_EDIT_TARGET_VOUCHERS)


def _filter_edit_objects(objects: list[dict[str, Any]] | None,
                         voucher_id: str) -> list[dict[str, Any]]:
    """反映先 target_vouchers に当該伝票が含まれるオブジェクトだけを抽出する（要件7）。"""
    if not objects:
        return []
    return [
        obj for obj in objects
        if isinstance(obj, dict) and voucher_id in _object_target_vouchers(obj)
    ]


def _ensure_font() -> None:
    global _FONT_REGISTERED
    with _PDF_FONT_CACHE_LOCK:
        if not _FONT_REGISTERED:
            pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))
            _FONT_REGISTERED = True


def _ensure_edit_text_font() -> str:
    """指図書編集オブジェクト用の日本語フォント名を返す。"""
    _ensure_font()
    return _FONT_NAME


def _normalized_font_name(value: object) -> str:
    # レジストリ、Qt、フォントnameテーブル間の全半角・大小文字・空白差を吸収する。
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(normalized.split())


def _font_name_text(value: object) -> str:
    """ReportLabのnameテーブル値(bytes/str)を比較可能な文字列へする。"""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16-be", "latin-1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
    return str(value or "")


def _style_flags(value: object) -> tuple[bool, bool]:
    style = _normalized_font_name(value)
    bold = any(token in style for token in (
        "bold", "semibold", "demibold", "black", "太字"))
    italic = any(token in style for token in ("italic", "oblique", "斜体"))
    return bold, italic


def _synthetic_style_flags(
    requested_bold: bool, requested_italic: bool,
    resolved_is_bold: bool, resolved_is_italic: bool,
) -> tuple[bool, bool]:
    """BoldとItalicを独立判定し、一方の実face状態を他方へ波及させない。"""
    synthetic_bold = bool(requested_bold) and not bool(resolved_is_bold)
    synthetic_italic = bool(requested_italic) and not bool(resolved_is_italic)
    return synthetic_bold, synthetic_italic


def _decode_sfnt_name(raw: bytes, platform_id: int) -> str:
    try:
        return raw.decode("utf-16-be" if platform_id in (0, 3) else "mac_roman")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1", errors="replace")


def _inspect_sfnt_face(path: Path, face_index: int = 0) -> dict[str, Any] | None:
    """選択したTTF/OTF/TTC faceの実スタイルをSFNTテーブルから取得する。"""
    try:
        raw = path.read_bytes()
        if raw[:4] == b"ttcf":
            count = struct.unpack_from(">I", raw, 8)[0]
            if face_index < 0 or face_index >= count:
                return None
            sfnt_offset = struct.unpack_from(">I", raw, 12 + face_index * 4)[0]
        else:
            if face_index != 0:
                return None
            count = 1
            sfnt_offset = 0
        num_tables = struct.unpack_from(">H", raw, sfnt_offset + 4)[0]
        tables: dict[bytes, tuple[int, int]] = {}
        for index in range(num_tables):
            record = sfnt_offset + 12 + index * 16
            tag, _checksum, offset, length = struct.unpack_from(">4sIII", raw, record)
            tables[tag] = (offset, length)

        names: dict[int, list[tuple[int, int, str]]] = {}
        if b"name" in tables:
            offset, _length = tables[b"name"]
            record_count, strings_offset = struct.unpack_from(">HH", raw, offset + 2)
            for index in range(record_count):
                record = offset + 6 + index * 12
                platform_id, encoding_id, _language_id, name_id, length, relative = (
                    struct.unpack_from(">HHHHHH", raw, record))
                if name_id not in (1, 2, 16, 17):
                    continue
                start = offset + strings_offset + relative
                value = _decode_sfnt_name(raw[start:start + length], platform_id).strip()
                if value:
                    # Windows Unicode, Unicode platform, then Macintoshを優先する。
                    priority = 3 if platform_id == 3 else 2 if platform_id == 0 else 1
                    names.setdefault(name_id, []).append((priority, encoding_id, value))

        def best_name(preferred: int, fallback: int) -> str:
            values = names.get(preferred) or names.get(fallback) or []
            return max(values, default=(0, 0, ""), key=lambda item: (item[0], item[1]))[2]

        fs_selection = 0
        if b"OS/2" in tables and tables[b"OS/2"][1] >= 64:
            fs_selection = struct.unpack_from(">H", raw, tables[b"OS/2"][0] + 62)[0]
        italic_angle = 0.0
        if b"post" in tables and tables[b"post"][1] >= 8:
            fixed = struct.unpack_from(">i", raw, tables[b"post"][0] + 4)[0]
            italic_angle = fixed / 65536.0
        mac_style = 0
        if b"head" in tables and tables[b"head"][1] >= 46:
            mac_style = struct.unpack_from(">H", raw, tables[b"head"][0] + 44)[0]
        family = best_name(16, 1)
        subfamily = best_name(17, 2)
        style_bold, style_italic = _style_flags(subfamily)
        is_bold = bool(fs_selection & (1 << 5)) or bool(mac_style & 1) or style_bold
        is_italic = bool(fs_selection & 1) or bool(mac_style & 2) or abs(italic_angle) > 0.01
        return {
            "family": family, "subfamily": subfamily,
            "is_bold": is_bold, "is_italic": is_italic,
            "italic_angle": italic_angle, "fs_selection": fs_selection,
            "fs_selection_italic": bool(fs_selection & 1),
            "post_italic_angle": italic_angle, "mac_style": mac_style,
            "ttc_face_index": face_index, "face_count": count,
        }
    except (OSError, IndexError, struct.error, ValueError) as exc:
        _log.warning("voucher_edit_text_sfnt_inspect_failed file=%s face_index=%s reason=%s",
                     path, face_index, exc)
        return None


def _font_family_matches(requested: object, resolved_family: object,
                         resolved_subfamily: object = "") -> bool:
    """Qt/Windows表示名にweightが連結されたfamilyもSFNT nameと照合する。"""
    wanted = _normalized_font_name(requested)
    family = _normalized_font_name(resolved_family)
    subfamily = _normalized_font_name(resolved_subfamily)
    if not wanted or not family:
        return False
    if wanted == family or wanted == family + subfamily:
        return True
    # 一部Windowsフォントはfamily側へSemilight等を含め、requested側は含めない。
    trailing_styles = (
        "regular", "semilight", "light", "medium", "semibold", "demibold",
        "bold", "black", "italic", "oblique",
    )
    return family.startswith(wanted) and family[len(wanted):] in trailing_styles


def _sfnt_cmap_ranges(path: Path, face_index: int) -> tuple[tuple[int, int], ...]:
    """選択faceのUnicode cmapを、glyph有無判定用のコードポイント範囲で返す。"""
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return ()
    cache_key = (str(path), modified_ns, int(face_index))
    with _PDF_FONT_CACHE_LOCK:
        cached = _EDIT_CMAP_CACHE.get(cache_key)
        if cached is not None:
            return cached
    ranges: list[tuple[int, int]] = []
    try:
        raw = path.read_bytes()
        if raw[:4] == b"ttcf":
            sfnt_offset = struct.unpack_from(">I", raw, 12 + face_index * 4)[0]
        else:
            sfnt_offset = 0
        num_tables = struct.unpack_from(">H", raw, sfnt_offset + 4)[0]
        cmap_offset = None
        for index in range(num_tables):
            record = sfnt_offset + 12 + index * 16
            tag, _checksum, offset, _length = struct.unpack_from(">4sIII", raw, record)
            if tag == b"cmap":
                cmap_offset = offset
                break
        if cmap_offset is None:
            return ()
        subtable_count = struct.unpack_from(">H", raw, cmap_offset + 2)[0]
        subtable_offsets: set[int] = set()
        for index in range(subtable_count):
            platform_id, _encoding_id, relative = struct.unpack_from(
                ">HHI", raw, cmap_offset + 4 + index * 8)
            if platform_id in (0, 3):
                subtable_offsets.add(cmap_offset + relative)
        for offset in subtable_offsets:
            format_no = struct.unpack_from(">H", raw, offset)[0]
            if format_no == 12:
                group_count = struct.unpack_from(">I", raw, offset + 12)[0]
                for group in range(group_count):
                    start, end, start_glyph = struct.unpack_from(
                        ">III", raw, offset + 16 + group * 12)
                    if start_glyph or end > start:
                        ranges.append((start, end))
            elif format_no == 4:
                seg_count = struct.unpack_from(">H", raw, offset + 6)[0] // 2
                end_codes = offset + 14
                start_codes = end_codes + seg_count * 2 + 2
                id_deltas = start_codes + seg_count * 2
                id_range_offsets = id_deltas + seg_count * 2
                for segment in range(seg_count):
                    end = struct.unpack_from(">H", raw, end_codes + segment * 2)[0]
                    start = struct.unpack_from(">H", raw, start_codes + segment * 2)[0]
                    delta = struct.unpack_from(">h", raw, id_deltas + segment * 2)[0]
                    range_offset = struct.unpack_from(
                        ">H", raw, id_range_offsets + segment * 2)[0]
                    if start > end or start == 0xFFFF:
                        continue
                    if range_offset == 0:
                        if any(((code + delta) & 0xFFFF) != 0
                               for code in (start, end)):
                            ranges.append((start, end))
                    else:
                        # format 4のglyphIdArrayは疎になり得るので実glyphだけを追加。
                        for code in range(start, end + 1):
                            glyph_pos = (id_range_offsets + segment * 2 + range_offset
                                         + (code - start) * 2)
                            glyph_id = struct.unpack_from(">H", raw, glyph_pos)[0]
                            if glyph_id:
                                ranges.append((code, code))
        merged: list[tuple[int, int]] = []
        for start, end in sorted(set(ranges)):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result = tuple(merged)
    except (OSError, IndexError, struct.error, ValueError) as exc:
        _log.warning("voucher_edit_text_cmap_inspect_failed file=%s face_index=%s reason=%s",
                     path, face_index, exc)
        result = ()
    with _PDF_FONT_CACHE_LOCK:
        _EDIT_CMAP_CACHE[cache_key] = result
    return result


def _font_missing_characters(metadata: dict[str, Any], text: str) -> list[str]:
    path_text = str(metadata.get("resolved_font_file") or "")
    face_index = metadata.get("ttc_face_index")
    if not path_text or face_index is None:
        return []
    ranges = _sfnt_cmap_ranges(Path(path_text), int(face_index))
    if not ranges:
        return []
    unique = dict.fromkeys(char for char in text if not char.isspace())
    return [
        char for char in unique
        if not any(start <= ord(char) <= end for start, end in ranges)
    ]


def _windows_font_file(family: str, bold: bool, italic: bool = False) -> Path | None:
    """Windowsのインストール済みフォントをレジストリから解決する。"""
    if platform.system() != "Windows":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - Windows専用
        return None
    global _WINDOWS_FONT_REGISTRY_CACHE
    wanted = _normalized_font_name(family)
    candidates: list[tuple[int, Path]] = []
    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    key_name = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    with _PDF_FONT_CACHE_LOCK:
        if _WINDOWS_FONT_REGISTRY_CACHE is None:
            started = time.perf_counter()
            entries: list[tuple[str, Path, bool, bool]] = []
            for root in roots:
                try:
                    with winreg.OpenKey(root, key_name) as key:
                        index = 0
                        while True:
                            try:
                                display_name, raw_path, _kind = winreg.EnumValue(key, index)
                            except OSError:
                                break
                            index += 1
                            display = _normalized_font_name(display_name)
                            path = Path(str(raw_path))
                            if not path.is_absolute():
                                path = windows_fonts / path
                            style_bold, style_italic = _style_flags(display)
                            if path.is_file():
                                entries.append((display, path, style_bold, style_italic))
                except OSError:
                    continue
            _WINDOWS_FONT_REGISTRY_CACHE = tuple(entries)
            _log.info(
                "event=perf_voucher_editor phase=font_registry_scan elapsed_ms=%.3f count=%s",
                (time.perf_counter() - started) * 1000.0, len(entries),
            )
        registry_entries = _WINDOWS_FONT_REGISTRY_CACHE
    for display, path, style_bold, style_italic in registry_entries:
        if wanted not in display or style_bold != bold or style_italic != italic:
            continue
        score = 18 if display.startswith(wanted) else 16
        candidates.append((score, path))
    return max(candidates, default=(0, None), key=lambda value: value[0])[1]


def _fontconfig_font_file(family: str, bold: bool,
                          italic: bool = False) -> Path | None:
    """fontconfig環境でfamilyに対応する実フォントファイルを解決する。"""
    if platform.system() == "Windows":
        return None
    style = (
        "Bold Italic" if bold and italic else
        "Bold" if bold else
        "Italic" if italic else
        "Regular"
    )
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{family}\n%{style}\n%{file}", f"{family}:style={style}"],
            check=True, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.splitlines()
    if len(lines) < 3 or _normalized_font_name(family) not in _normalized_font_name(lines[0]):
        return None
    matched_bold, matched_italic = _style_flags(lines[1])
    if matched_bold != bold or matched_italic != italic:
        return None
    path = Path(lines[-1].strip())
    return path if path.is_file() else None


_SAFE_JAPANESE_FONT_FAMILIES = (
    "Meiryo",
    "Yu Gothic",
    "MS Gothic",
    "Noto Sans CJK JP",
    "IPAexGothic",
)


def _edit_font_file(family: str, bold: bool, italic: bool) -> Path | None:
    return (_windows_font_file(family, bold, italic)
            or _fontconfig_font_file(family, bold, italic))


def _ttc_face_index(path: Path, family: str, bold: bool,
                    italic: bool) -> int | None:
    """実family/styleが一致するface indexを返す（単体フォントは0）。"""
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = -1
    cache_key = (str(path), modified_ns, _normalized_font_name(family), bool(bold), bool(italic))
    with _PDF_FONT_CACHE_LOCK:
        if cache_key in _EDIT_TTC_FACE_CACHE:
            return _EDIT_TTC_FACE_CACHE[cache_key]
    wanted = _normalized_font_name(family)
    inspect_started = time.perf_counter()
    # 実ファイルではReportLabへ要求したスタイル値ではなく、各faceのOS/2/post/headを
    # 読んで判定する。存在しないテスト用pathだけ旧TTFontFaceモック経路を残す。
    if path.is_file():
        first_metadata = _inspect_sfnt_face(path, 0)
        count = int((first_metadata or {}).get("face_count", 1))
        for index in range(count):
            face_metadata = first_metadata if index == 0 else _inspect_sfnt_face(path, index)
            if not face_metadata:
                continue
            if (_font_family_matches(family, face_metadata.get("family"),
                                     face_metadata.get("subfamily"))
                    and bool(face_metadata.get("is_bold")) == bool(bold)
                    and bool(face_metadata.get("is_italic")) == bool(italic)):
                with _PDF_FONT_CACHE_LOCK:
                    _EDIT_TTC_FACE_CACHE[cache_key] = index
                _log.info(
                    "event=perf_voucher_editor phase=ttc_face_inspection elapsed_ms=%.3f "
                    "file=%s face_index=%s resolved_family=%r resolved_subfamily=%r "
                    "resolved_is_bold=%s resolved_is_italic=%s resolved_italic_angle=%s "
                    "fs_selection_italic=%s",
                    (time.perf_counter() - inspect_started) * 1000.0, path, index,
                    face_metadata.get("family"), face_metadata.get("subfamily"),
                    face_metadata.get("is_bold"), face_metadata.get("is_italic"),
                    face_metadata.get("italic_angle"),
                    face_metadata.get("fs_selection_italic"),
                )
                return index
        first = None
    else:
        try:
            first = TTFontFace(str(path), subfontIndex=0)
            count = int(getattr(first, "numSubfonts", 1))
        except Exception as exc:
            _log.warning(
                "voucher_edit_text_ttc_inspect_failed file=%s requested_family=%r "
                "requested_bold=%s requested_italic=%s reason=%s",
                path, family, bool(bold), bool(italic), exc,
            )
            with _PDF_FONT_CACHE_LOCK:
                _EDIT_TTC_FACE_CACHE[cache_key] = None
            return None
    if first is None:
        _log.warning(
            "voucher_edit_text_ttc_face_not_found file=%s requested_family=%r "
            "requested_bold=%s requested_italic=%s face_count=%s",
            path, family, bool(bold), bool(italic), count,
        )
        with _PDF_FONT_CACHE_LOCK:
            _EDIT_TTC_FACE_CACHE[cache_key] = None
        return None
    for index in range(count):
        try:
            face = first if index == 0 else TTFontFace(str(path), subfontIndex=index)
            face_family = _font_name_text(getattr(face, "familyName", ""))
            face_style = _font_name_text(getattr(face, "styleName", ""))
            face_bold, face_italic = _style_flags(face_style)
            flags = int(getattr(face, "flags", 0))
            face_bold = face_bold or bool(flags & FF_FORCEBOLD)
            face_italic = face_italic or bool(flags & FF_ITALIC)
            if (_font_family_matches(family, face_family, face_style)
                    and face_bold == bool(bold)
                    and face_italic == bool(italic)):
                with _PDF_FONT_CACHE_LOCK:
                    _EDIT_TTC_FACE_CACHE[cache_key] = index
                _log.info(
                    "event=perf_voucher_editor phase=ttc_face_inspection elapsed_ms=%.3f file=%s face_index=%s",
                    (time.perf_counter() - inspect_started) * 1000.0, path, index,
                )
                return index
        except Exception as exc:
            _log.debug("voucher_edit_text_ttc_face_skipped file=%s face_index=%s reason=%s",
                       path, index, exc)
    _log.warning(
        "voucher_edit_text_ttc_face_not_found file=%s requested_family=%r "
        "requested_bold=%s requested_italic=%s face_count=%s",
        path, family, bool(bold), bool(italic), count,
    )
    with _PDF_FONT_CACHE_LOCK:
        _EDIT_TTC_FACE_CACHE[cache_key] = None
    _log.info(
        "event=perf_voucher_editor phase=ttc_face_inspection elapsed_ms=%.3f file=%s face_index=none",
        (time.perf_counter() - inspect_started) * 1000.0, path,
    )
    return None


def _register_edit_font(path: Path, bold: bool, italic: bool,
                        family: str = "") -> str | None:
    face_index = _ttc_face_index(path, family, bold, italic)
    if face_index is None:
        return None
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = -1
    face_metadata = _inspect_sfnt_face(path, face_index) if path.is_file() else None
    identity = (
        str(path.resolve()) if path.is_file() else str(path), int(face_index),
        modified_ns, _normalized_font_name((face_metadata or {}).get("family") or family),
        _normalized_font_name((face_metadata or {}).get("subfamily")),
        bool(bold), bool(italic), PDF_FONT_REGISTRATION_METHOD,
        PDF_TEXT_RENDERER_REVISION,
    )
    digest = hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9]+", "", str(family or "Font"))[:18] or "Font"
    registered_name = (
        f"VoucherEdit_{slug}_{digest[:16]}_face{face_index}_"
        f"b{int(bool(bold))}_i{int(bool(italic))}_r{PDF_TEXT_RENDERER_REVISION}"
    )
    try:
        with _PDF_FONT_CACHE_LOCK:
            known_identity = _REGISTERED_EDIT_FONT_IDENTITIES.get(registered_name)
            if known_identity is not None and known_identity != identity:
                # SHA短縮部の衝突時にも別名へ退避し、別fontを絶対に再利用しない。
                registered_name += "_" + digest[16:32]
                known_identity = _REGISTERED_EDIT_FONT_IDENTITIES.get(registered_name)
            if known_identity is None and registered_name in pdfmetrics.getRegisteredFontNames():
                registered_name += "_" + digest[16:32]
                known_identity = _REGISTERED_EDIT_FONT_IDENTITIES.get(registered_name)
            if known_identity is None:
                started = time.perf_counter()
                pdfmetrics.registerFont(TTFont(
                    registered_name, str(path), subfontIndex=face_index))
                _REGISTERED_EDIT_FONT_IDENTITIES[registered_name] = identity
                _log.info(
                    "event=perf_voucher_editor phase=reportlab_font_registration "
                    "elapsed_ms=%.3f font=%s file=%s face_index=%s family=%r subfamily=%r",
                    (time.perf_counter() - started) * 1000.0, registered_name,
                    path, face_index, (face_metadata or {}).get("family", family),
                    (face_metadata or {}).get("subfamily", ""),
                )
        return registered_name
    except Exception as exc:
        _log.warning(
            "voucher_edit_text_font_register_failed file=%s bold=%s italic=%s reason=%s",
            path, bool(bold), bool(italic), exc,
        )
        return None


def _resolve_edit_pdf_font(family: object, bold: bool = False,
                           italic: bool = False) -> str:
    """指定familyを捨てず、正式フェイスと必要最小限の疑似装飾を解決する。"""
    requested = str(family or "").strip()
    key = (requested, bool(bold), bool(italic))
    normalized_requested = _normalized_font_name(requested)
    with _PDF_FONT_CACHE_LOCK:
        previous = _EDIT_FONT_METADATA.get(key)
        previous_cache_key = tuple((previous or {}).get("cache_key_tuple") or ())
        if previous_cache_key and previous_cache_key in _EDIT_FONT_CACHE:
            cached_path = str((previous or {}).get("resolved_font_file") or "")
            try:
                cached_mtime = Path(cached_path).stat().st_mtime_ns if cached_path else -1
            except OSError:
                cached_mtime = -1
            if cached_mtime == (previous or {}).get("font_file_mtime_ns", -1):
                return _EDIT_FONT_CACHE[previous_cache_key]
    _ensure_font()
    candidates: list[tuple[str, bool, bool, bool, bool, bool]] = []

    def add_candidate(candidate_family: str, candidate_bold: bool,
                      candidate_italic: bool, fallback: bool,
                      synthetic_bold: bool, synthetic_italic: bool) -> None:
        candidate = (candidate_family, candidate_bold, candidate_italic,
                     fallback, synthetic_bold, synthetic_italic)
        if candidate_family and candidate not in candidates:
            candidates.append(candidate)

    def add_family_faces(candidate_family: str, fallback: bool) -> None:
        if bold and italic:
            add_candidate(candidate_family, True, True, fallback, False, False)
            add_candidate(candidate_family, True, False, fallback, False, True)
            add_candidate(candidate_family, False, False, fallback, True, True)
        elif bold:
            add_candidate(candidate_family, True, False, fallback, False, False)
            add_candidate(candidate_family, False, False, fallback, True, False)
        elif italic:
            add_candidate(candidate_family, False, True, fallback, False, False)
            add_candidate(candidate_family, False, False, fallback, False, True)
        else:
            add_candidate(candidate_family, False, False, fallback, False, False)

    add_family_faces(requested, False)
    for safe_family in _SAFE_JAPANESE_FONT_FAMILIES:
        if _normalized_font_name(safe_family) != _normalized_font_name(requested):
            add_family_faces(safe_family, True)

    for (candidate_family, candidate_bold, candidate_italic, fallback,
         synthetic_bold, synthetic_italic) in candidates:
        path = _edit_font_file(candidate_family, candidate_bold, candidate_italic)
        if path is None:
            continue
        registered_name = _register_edit_font(
            path, candidate_bold, candidate_italic, family=candidate_family)
        if registered_name is None:
            continue
        face_index = _ttc_face_index(
            path, candidate_family, candidate_bold, candidate_italic)
        face_metadata = (
            _inspect_sfnt_face(path, int(face_index))
            if face_index is not None and path.is_file() else None
        )
        resolved_family = str((face_metadata or {}).get("family") or candidate_family)
        resolved_subfamily = str((face_metadata or {}).get("subfamily") or "")
        resolved_bold = bool((face_metadata or {}).get("is_bold", candidate_bold))
        resolved_italic = bool((face_metadata or {}).get("is_italic", candidate_italic))
        resolved_italic_angle = float((face_metadata or {}).get("italic_angle", 0.0))
        # 最終防衛: 実faceが要求装飾を持たなければ候補指定に関係なく疑似装飾する。
        synthetic_bold, synthetic_italic = _synthetic_style_flags(
            bold, italic, resolved_bold, resolved_italic)
        try:
            modified_ns = path.stat().st_mtime_ns
        except OSError:
            modified_ns = -1
        cache_key = (
            normalized_requested, bool(bold), bool(italic), str(path),
            int(face_index) if face_index is not None else -1, modified_ns,
            PDF_FONT_REGISTRATION_METHOD, PDF_TEXT_RENDERER_REVISION,
        )
        fallback_reason = "requested_family_unavailable" if fallback else ""
        with _PDF_FONT_CACHE_LOCK:
            _EDIT_FONT_CACHE[cache_key] = registered_name
            _EDIT_FONT_METADATA[key] = {
            "selected_font": requested,
            "resolved_font": registered_name,
            "requested_family": requested,
            "normalized_requested_family": normalized_requested,
            "requested_bold": bool(bold),
            "requested_italic": bool(italic),
            "resolved_font_file": str(path),
            "resolved_pdf_font_name": registered_name,
            "resolved_family": resolved_family,
            "resolved_subfamily": resolved_subfamily,
            "resolved_bold": resolved_bold,
            "resolved_italic": resolved_italic,
            "resolved_is_bold": resolved_bold,
            "resolved_is_italic": resolved_italic,
            "resolved_italic_angle": resolved_italic_angle,
            "os2_fs_selection": (face_metadata or {}).get("fs_selection"),
            "os2_fs_selection_italic": (face_metadata or {}).get("fs_selection_italic"),
            "post_italic_angle": (face_metadata or {}).get("post_italic_angle"),
            "ttc_face_index": face_index,
            "fallback_used": fallback,
            "fallback_reason": fallback_reason,
            "glyph_fallback_used": False,
            "synthetic_bold": synthetic_bold,
            "synthetic_italic": synthetic_italic,
            "renderer_revision": PDF_TEXT_RENDERER_REVISION,
            "font_file_mtime_ns": modified_ns,
            "font_registration_method": PDF_FONT_REGISTRATION_METHOD,
            "cache_key": repr(cache_key),
            "cache_key_tuple": cache_key,
            }
        _log.info(
            "voucher_edit_text_font_resolved requested_family=%r requested_bold=%s "
            "requested_italic=%s resolved_font_file=%s resolved_pdf_font_name=%s "
            "resolved_family=%r resolved_subfamily=%r resolved_is_bold=%s "
            "resolved_is_italic=%s resolved_italic_angle=%s ttc_face_index=%s "
            "fallback_used=%s synthetic_bold_used=%s synthetic_italic_used=%s "
            "renderer_revision=%s",
            requested, bool(bold), bool(italic), path, registered_name,
            resolved_family, resolved_subfamily, resolved_bold, resolved_italic,
            resolved_italic_angle, face_index, fallback, synthetic_bold,
            synthetic_italic, PDF_TEXT_RENDERER_REVISION,
        )
        return registered_name

    _log.warning(
        "voucher_edit_text_font_fallback requested_family=%r requested_bold=%s "
        "requested_italic=%s resolved_font_file=%r resolved_pdf_font_name=%s "
        "fallback_used=true synthetic_bold_used=%s synthetic_italic_used=%s "
        "reason=no_registerable_family_face",
        requested, bool(bold), bool(italic), "", _FONT_NAME, bool(bold), bool(italic),
    )
    with _PDF_FONT_CACHE_LOCK:
        cache_key = (
            normalized_requested, bool(bold), bool(italic), "", -1, -1,
            "unicode_cid_fallback", PDF_TEXT_RENDERER_REVISION,
        )
        _EDIT_FONT_CACHE[cache_key] = _FONT_NAME
        _EDIT_FONT_METADATA[key] = {
        "selected_font": requested,
        "resolved_font": _FONT_NAME,
        "requested_family": requested,
        "normalized_requested_family": normalized_requested,
        "requested_bold": bool(bold),
        "requested_italic": bool(italic),
        "resolved_font_file": "",
        "resolved_pdf_font_name": _FONT_NAME,
        "resolved_family": _FONT_NAME,
        "resolved_bold": False,
        "resolved_italic": False,
        "resolved_is_bold": False,
        "resolved_is_italic": False,
        "resolved_subfamily": "",
        "resolved_italic_angle": 0.0,
        "ttc_face_index": None,
        "fallback_used": True,
        "fallback_reason": "no_registerable_family_face",
        "glyph_fallback_used": False,
        "synthetic_bold": bool(bold),
        "synthetic_italic": bool(italic),
        "renderer_revision": PDF_TEXT_RENDERER_REVISION,
        "font_file_mtime_ns": -1,
        "font_registration_method": "unicode_cid_fallback",
        "cache_key": repr(cache_key),
        "cache_key_tuple": cache_key,
        }
    return _FONT_NAME


def _resolved_edit_font_metadata(family: object, bold: bool, italic: bool,
                                 font_name: str) -> dict[str, Any]:
    """直前の解決結果を返す。モック/旧呼出しではCIDだけ安全側で疑似処理する。"""
    key = (str(family or "").strip(), bool(bold), bool(italic))
    metadata = _EDIT_FONT_METADATA.get(key)
    if metadata is not None:
        return metadata
    return {
        "synthetic_bold": bool(bold) and font_name == _FONT_NAME,
        "synthetic_italic": bool(italic) and font_name == _FONT_NAME,
    }


def _resolve_edit_text_font(
    family: object, bold: bool, italic: bool, text: str,
) -> tuple[str, dict[str, Any]]:
    """テキストの基底faceを解決する。CJKを含む場合はItalic faceを選ばない。"""
    native_italic_requested = bool(italic) and not contains_cjk(text)
    font_name = _resolve_edit_pdf_font(family, bold, native_italic_requested)
    metadata = dict(_resolved_edit_font_metadata(
        family, bold, native_italic_requested, font_name))
    metadata["requested_family"] = str(family or "").strip()
    metadata["requested_bold"] = bool(bold)
    metadata["requested_italic"] = bool(italic)
    metadata["native_italic_requested"] = native_italic_requested
    return font_name, metadata


def _font_metadata_for_text(
    metadata: dict[str, Any], text: str, *,
    requested_bold: bool = False, requested_italic: bool = False,
) -> dict[str, Any]:
    """共有解決metadataを汚さず、対象文字列固有のglyph fallback状態を付与する。"""
    result = dict(metadata)
    missing = _font_missing_characters(result, text)
    result["missing_glyphs"] = missing
    result["glyph_fallback_used"] = bool(missing)
    missing_set = set(missing)
    run_count = 0
    previous_signature: tuple[bool, bool] | None = None
    for char in text:
        if char in "\r\n":
            continue
        current_fallback = char in missing_set
        current_cjk = is_cjk_character(char)
        if char.isspace() and previous_signature is not None:
            current_fallback, current_cjk = previous_signature
        signature = (current_fallback, current_cjk)
        if previous_signature is None or signature != previous_signature:
            run_count += 1
        previous_signature = signature
    result["font_run_count"] = max(run_count, 1)
    result["run_contains_cjk"] = contains_cjk(text)
    if requested_italic and result["run_contains_cjk"]:
        # オブジェクト集約ログも、CJK runが必ず疑似斜体になることを示す。
        result["native_italic_face_used"] = False
        result["synthetic_italic"] = True
        result["italic_strategy"] = "synthetic_cjk"
    if missing:
        result["fallback_used"] = True
        result["fallback_reason"] = "missing_glyphs"
        selected_style = _synthetic_style_flags(
            requested_bold, requested_italic,
            bool(result.get("resolved_is_bold", result.get("resolved_bold", False))),
            bool(result.get("resolved_is_italic", result.get("resolved_italic", False))),
        )
        fallback_style = _synthetic_style_flags(
            requested_bold, requested_italic, False, False)
        has_selected_run = any(
            not char.isspace() and char not in missing_set for char in text)
        result["synthetic_bold"] = (
            fallback_style[0] or (has_selected_run and selected_style[0]))
        result["synthetic_italic"] = (
            fallback_style[1] or (has_selected_run and selected_style[1]))
    return result


def _font_run_face(
    font_name: str, font_metadata: dict[str, Any], bold: bool, italic: bool,
    run_contains_cjk: bool,
) -> tuple[str, dict[str, Any]]:
    """run種別に応じた実faceを返す。CJKは必ずupright Regular/Boldを使う。"""
    requested_family = str(font_metadata.get("requested_family") or "").strip()
    base_is_native_italic = bool(font_metadata.get(
        "resolved_is_italic", font_metadata.get("resolved_italic", False)))

    if run_contains_cjk and italic:
        if requested_family:
            upright_name = _resolve_edit_pdf_font(requested_family, bold, False)
            upright_metadata = dict(_resolved_edit_font_metadata(
                requested_family, bold, False, upright_name))
            upright_metadata.setdefault("requested_family", requested_family)
            return upright_name, upright_metadata
        if base_is_native_italic:
            # 旧/モックmetadataでfamilyが不明な場合もnative Italicとの二重適用を避ける。
            return _FONT_NAME, {
                "resolved_family": _FONT_NAME, "resolved_subfamily": "",
                "resolved_is_bold": False, "resolved_is_italic": False,
                "fallback_used": True, "fallback_reason": "cjk_upright_face_unknown",
            }
        return font_name, font_metadata

    if not run_contains_cjk and italic and contains_cjk(
            str(font_metadata.get("_full_text") or "")) and requested_family:
        native_name = _resolve_edit_pdf_font(requested_family, bold, True)
        native_metadata = dict(_resolved_edit_font_metadata(
            requested_family, bold, True, native_name))
        native_metadata.setdefault("requested_family", requested_family)
        return native_name, native_metadata
    return font_name, font_metadata


def _text_font_run_details(
    text: str, font_name: str, font_metadata: dict[str, Any],
    bold: bool, italic: bool,
) -> list[dict[str, Any]]:
    """CJK/非CJKとcmap fallback境界で分割し、run固有のfaceと装飾を返す。"""
    if not text:
        return []
    source_metadata = dict(font_metadata)
    source_metadata["_full_text"] = text
    raw_runs: list[tuple[str, bool]] = []
    current: list[str] = []
    current_cjk: bool | None = None
    for char in text:
        char_cjk = is_cjk_character(char)
        if char.isspace() and current_cjk is not None:
            char_cjk = current_cjk
        if current and char_cjk != current_cjk:
            raw_runs.append(("".join(current), bool(current_cjk)))
            current = []
        current.append(char)
        current_cjk = char_cjk
    if current:
        raw_runs.append(("".join(current), bool(current_cjk)))

    details: list[dict[str, Any]] = []
    for raw_text, run_cjk in raw_runs:
        run_font, run_metadata = _font_run_face(
            font_name, source_metadata, bold, italic, run_cjk)
        missing = set(_font_missing_characters(run_metadata, raw_text))
        pieces: list[tuple[str, bool]] = []
        piece: list[str] = []
        piece_fallback: bool | None = None
        for char in raw_text:
            use_fallback = char in missing
            if char.isspace() and piece_fallback is not None:
                use_fallback = piece_fallback
            if piece and use_fallback != piece_fallback:
                pieces.append(("".join(piece), bool(piece_fallback)))
                piece = []
            piece.append(char)
            piece_fallback = use_fallback
        if piece:
            pieces.append(("".join(piece), bool(piece_fallback)))

        for piece_text, use_fallback in pieces:
            actual_font = _FONT_NAME if use_fallback else run_font
            actual_metadata = ({
                "resolved_family": _FONT_NAME, "resolved_subfamily": "",
                "resolved_is_bold": False, "resolved_is_italic": False,
                "fallback_used": True, "fallback_reason": "missing_glyphs",
            } if use_fallback else run_metadata)
            resolved_bold = bool(actual_metadata.get(
                "resolved_is_bold", actual_metadata.get("resolved_bold", False)))
            resolved_italic = bool(actual_metadata.get(
                "resolved_is_italic", actual_metadata.get("resolved_italic", False)))
            if run_cjk and italic:
                synthetic_bold = bool(bold) and not resolved_bold
                synthetic_italic = True
                native_italic = False
                italic_strategy = "synthetic_cjk"
            else:
                synthetic_bold, synthetic_italic = _synthetic_style_flags(
                    bold, italic, resolved_bold, resolved_italic)
                native_italic = bool(italic) and resolved_italic
                italic_strategy = (
                    "native" if native_italic else
                    "synthetic_missing_face" if synthetic_italic else "none"
                )
            details.append({
                "text": piece_text,
                "font_name": actual_font,
                "synthetic_bold": synthetic_bold,
                "synthetic_italic": synthetic_italic,
                "run_contains_cjk": run_cjk,
                "resolved_family": actual_metadata.get(
                    "resolved_family", actual_metadata.get("selected_font", "")),
                "resolved_subfamily": actual_metadata.get("resolved_subfamily", ""),
                "resolved_is_bold": resolved_bold,
                "resolved_is_italic": resolved_italic,
                "native_italic_face_used": native_italic,
                "italic_strategy": italic_strategy,
            })
    return details


def _text_font_runs(text: str, font_name: str, font_metadata: dict[str, Any],
                    bold: bool, italic: bool) -> list[tuple[str, str, bool, bool]]:
    """互換tuple形式でCJK/glyph fallback分割済みfont runを返す。"""
    return [
        (run["text"], run["font_name"], run["synthetic_bold"],
         run["synthetic_italic"])
        for run in _text_font_run_details(text, font_name, font_metadata, bold, italic)
    ]


def draw_styled_pdf_text_runs(
    c: "rl_canvas.Canvas", runs: list[tuple[str, str, bool, bool]],
    anchor_x: float, baseline_y: float, font_size: float, *, text_align: str,
    trace_id: str = "", object_id: object = None,
    requested_italic: bool | None = None, edit_objects_sha256: str = "",
) -> float:
    """複数font runを一つの整列済み文字列として描き、総advance幅を返す。"""
    widths: list[float] = []
    for run_text, run_font, _run_bold, _run_italic in runs:
        try:
            widths.append(float(pdfmetrics.stringWidth(run_text, run_font, font_size)))
        except Exception:
            widths.append(float(len(run_text)) * font_size)
    total_width = sum(widths)
    right_extension = 0.0
    prefix_width = 0.0
    for (_text, _font, run_bold, run_italic), run_width in zip(runs, widths):
        overhang = (
            abs(TEXT_SYNTHETIC_ITALIC_SHEAR) * font_size if run_italic else 0.0)
        if run_bold:
            overhang += TEXT_SYNTHETIC_BOLD_OFFSET_PT
        right_extension = max(
            right_extension, prefix_width + run_width + overhang - total_width)
        prefix_width += run_width
    visual_width = total_width + right_extension
    cursor = (anchor_x - visual_width if text_align == "right" else
              anchor_x - visual_width / 2.0 if text_align == "center" else anchor_x)
    for (run_text, run_font, run_bold, run_italic), run_width in zip(runs, widths):
        draw_styled_pdf_text(
            c, run_text, cursor, baseline_y, run_font, font_size,
            text_align="left", synthetic_bold=run_bold,
            synthetic_italic=run_italic, trace_id=trace_id,
            object_id=object_id, requested_italic=requested_italic,
            edit_objects_sha256=edit_objects_sha256)
        cursor += run_width
    return total_width


def _log_pdf_text_font_runs(
    runs: list[dict[str, Any]], *, object_id: object, requested_family: object,
    requested_bold: bool, requested_italic: bool,
) -> None:
    """Windows実機診断用に、実際に描くface/strategyをfont run単位で記録する。"""
    for run in runs:
        _log.info(
            "event=voucher_edit_pdf_text_font_run object_id=%s run_text=%r "
            "requested_family=%r requested_bold=%s requested_italic=%s "
            "run_contains_cjk=%s resolved_family=%r resolved_subfamily=%r "
            "resolved_is_bold=%s resolved_is_italic=%s "
            "native_italic_face_used=%s synthetic_bold_used=%s "
            "synthetic_italic_used=%s italic_strategy=%s renderer_revision=%s",
            object_id, run["text"], str(requested_family or ""),
            str(bool(requested_bold)).lower(), str(bool(requested_italic)).lower(),
            str(bool(run["run_contains_cjk"])).lower(),
            run.get("resolved_family", ""), run.get("resolved_subfamily", ""),
            str(bool(run.get("resolved_is_bold"))).lower(),
            str(bool(run.get("resolved_is_italic"))).lower(),
            str(bool(run.get("native_italic_face_used"))).lower(),
            str(bool(run.get("synthetic_bold"))).lower(),
            str(bool(run.get("synthetic_italic"))).lower(),
            run.get("italic_strategy", "none"), PDF_TEXT_RENDERER_REVISION,
        )


def _object_text_style(obj: dict[str, Any], name: str) -> bool:
    """font_*を優先し、旧互換属性も受け入れる。"""
    return bool(obj.get(f"font_{name}", obj.get(name, False)))


def _log_pdf_text_style_received(obj: dict[str, Any], font_name: str,
                                 metadata: dict[str, Any]) -> None:
    _log.info(
        "event=voucher_edit_pdf_text_draw trace_id=%s object_id=%s text=%r font_family=%r "
        "font_size=%s font_bold=%s font_italic=%s font_underline=%s "
        "font_strikeout=%s edit_scope=%s voucher_no=%s requested_family=%r "
        "requested_bold=%s requested_italic=%s requested_underline=%s "
        "normalized_requested_family=%s resolved_font_file=%s resolved_pdf_font_name=%s "
        "ttc_face_index=%s resolved_family=%r resolved_subfamily=%r "
        "resolved_is_bold=%s resolved_is_italic=%s resolved_italic_angle=%s "
        "fallback_used=%s fallback_reason=%s synthetic_bold_used=%s "
        "synthetic_italic_used=%s glyph_fallback_used=%s font_run_count=%s "
        "missing_glyphs=%r cache_key=%r draw_helper=draw_styled_pdf_text "
        "cache_hit=false renderer_revision=%s edit_data_revision=%s "
        "edit_objects_sha256=%s",
        obj.get("_edit_render_trace_id", ""), obj.get("id"), obj.get("text"),
        obj.get("font_family"), obj.get("font_size"),
        _object_text_style(obj, "bold"),
        _object_text_style(obj, "italic"), _object_text_style(obj, "underline"),
        _object_text_style(obj, "strikeout"), obj.get("_edit_scope", "unknown"),
        obj.get("_edit_voucher_no", obj.get("_pdf_voucher_id", "")),
        obj.get("font_family"), _object_text_style(obj, "bold"),
        _object_text_style(obj, "italic"), _object_text_style(obj, "underline"),
        metadata.get("normalized_requested_family", ""),
        metadata.get("resolved_font_file", ""),
        metadata.get("resolved_pdf_font_name", font_name),
        metadata.get("ttc_face_index"), metadata.get("resolved_family", ""),
        metadata.get("resolved_subfamily", ""),
        metadata.get("resolved_is_bold", metadata.get("resolved_bold", False)),
        metadata.get("resolved_is_italic", metadata.get("resolved_italic", False)),
        metadata.get("resolved_italic_angle", 0.0),
        metadata.get("fallback_used", font_name == _FONT_NAME),
        metadata.get("fallback_reason", ""),
        metadata.get("synthetic_bold", False),
        metadata.get("synthetic_italic", False),
        metadata.get("glyph_fallback_used", False),
        metadata.get("font_run_count", 1),
        metadata.get("missing_glyphs", []), metadata.get("cache_key", ""),
        PDF_TEXT_RENDERER_REVISION, obj.get("_edit_data_revision", 0),
        obj.get("_edit_objects_sha256", ""),
    )


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


# 摘要上段（index=0）だけを少し上へ持ち上げ、下段（index=1）との間隔を広げる（要件6）。
# 下段の位置は変えない。物件No行・表本体と重ならない範囲（2pt）に留める。
SUMMARY_UPPER_SHIFT_UP = 2.0


def _is_star_row(row: dict[str, Any]) -> bool:
    """品名が対象外マーカー「*」の行か判定する（判定用の正規化値で比較する）。

    表示用の name はトリムしないため、空白付きでも正しく判定できるよう、優先して
    トリム済みの name_key を使い、無ければ name を strip して比較する。
    """
    key = row.get("name_key")
    if key is None:
        key = str(row.get("name", "")).strip().strip("　")
    return key == "*"


def _summary_line_y(index: int) -> float:
    y = FORM_SUM_BOT + 12.0 - index * 9.0
    if index == 0:
        y += SUMMARY_UPPER_SHIFT_UP
    return y


def _draw_summary_lines(c: rl_canvas.Canvas, data: dict[str, Any], fs: float) -> None:
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    for index, line in enumerate(_summary_lines(data)):
        if not line:
            continue
        if index == 0:
            draw_text_fit_width(
                c, str(line), note_line_x, _summary_line_y(index),
                SUMMARY_TEXT_RIGHT - note_line_x, DATA_BOLD_FONT_NAME, fs, 5.0,
            )
            _log.info("voucher_delivery_address_combined_drawn: value=%s", line)
        else:
            course_name = _clean_display_text(data.get("delivery_course_name"))
            if course_name:
                draw_text_fit_width(
                    c, str(line), note_line_x, _summary_line_y(index),
                    DELIVERY_COURSE_X - DELIVERY_COURSE_GAP - note_line_x,
                    DATA_BOLD_FONT_NAME, fs, 5.0,
                )
            else:
                _str(c, str(line), note_line_x, _summary_line_y(index), fs,
                     max_w=SUMMARY_TEXT_RIGHT - note_line_x)


def _draw_customer_order_no(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """摘要/物件No下に「お客様注文No. xxxx」を表示する（全伝票共通）。

    旧・消費税固定文言があった位置・基準で表示し、
    文字サイズは従来比1.2倍。客先注文No_10桁が空欄/None/空白のみの場合は何も描かない。
    """
    value = data.get("customer_order_no_10")
    if _name_debug_enabled():
        _log.info("customer_order_no_10 repr=%r", value)
    text = "" if value is None else str(value).strip().strip("　")
    if not text:
        return
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    c.setFont(_FONT_NAME, CUSTOMER_ORDER_NO_FONT_SIZE)
    c.drawString(note_line_x, TAX_Y, f"{CUSTOMER_ORDER_NO_LABEL}{text}")


def _draw_staff_values(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """営業担当・工事担当のデータだけを表示し、固定ラベルは描画しない。"""
    course_name = _clean_display_text(data.get("delivery_course_name"))
    if course_name in {"-", "－"}:
        course_name = ""
    order_no = _clean_display_text(data.get("order_no"))
    voucher_no = _clean_display_text(
        data.get("voucher_no") or data.get("delivery_no")
    )
    sales_person_name = _clean_display_text(data.get("sales_rep"))
    course_code = _clean_display_text(data.get("delivery_course_code"))
    combined_text = (
        f"{course_name} {sales_person_name}"
        if course_name and sales_person_name
        else course_name or sales_person_name
    )
    response_key = _clean_display_text(
        data.get("delivery_course_response_key")
    ) or "(not_available)"
    display_no = _clean_display_text(data.get("delivery_course_display_no"))
    logical_name = _clean_display_text(
        data.get("delivery_course_name_logical_name")
    ) or "配送コース名称"
    # 旧担当者名の描画開始位置と基準字号から右端を算出し、その位置を維持する。
    sales_width = c.stringWidth(
        sales_person_name, _resolve_base_font(DATA_BOLD_FONT_NAME),
        DETAIL_DATA_FONT_SIZE,
    )
    staff_right_x = min(STAFF_TEXT_RIGHT, STAFF_TEXT_X + sales_width)
    if not sales_person_name:
        staff_right_x = STAFF_TEXT_RIGHT
    combined_max_width = staff_right_x - DELIVERY_COURSE_X
    _log.info(
        "voucher_delivery_course_draw_requested "
        "order_no=%s voucher_no=%s response_key=%s display_no=%s "
        "course_code=%r delivery_course_name=%r sales_person_name=%r combined_text=%r "
        "logical_name=%s x=%s y=%s "
        "max_width=%s base_font_size=%s",
        order_no,
        voucher_no,
        response_key,
        display_no,
        course_code,
        course_name,
        sales_person_name,
        combined_text,
        logical_name,
        DELIVERY_COURSE_X,
        SALES_REP_Y,
        combined_max_width,
        DETAIL_DATA_FONT_SIZE,
    )
    if not course_name:
        _log.info(
            "voucher_delivery_course_draw_skipped "
            "order_no=%s voucher_no=%s response_key=%s display_no=%s "
            "value=%r reason=blank",
            order_no,
            voucher_no,
            response_key,
            display_no,
            course_name,
        )
    elif combined_max_width <= 0:
        _log.warning(
            "voucher_delivery_course_draw_skipped "
            "order_no=%s voucher_no=%s response_key=%s display_no=%s "
            "value=%r reason=non_positive_width max_width=%s",
            order_no,
            voucher_no,
            response_key,
            display_no,
            course_name,
            combined_max_width,
        )
    else:
        # 先行する黒背景セル等の描画状態を引き継がず、必ず黒・不透明で
        # 描画する。各帳票では構造/背景描画後にこの関数が呼ばれる。
        c.saveState()
        try:
            c.setFillColorRGB(0, 0, 0)
            used_font_size = draw_text_fit_width_right(
                c,
                combined_text,
                staff_right_x,
                SALES_REP_Y,
                combined_max_width,
                DATA_BOLD_FONT_NAME,
                DETAIL_DATA_FONT_SIZE,
                4.0,
            )
        finally:
            c.restoreState()
        _log.info(
            "voucher_delivery_course_staff_combined_drawn "
            "order_no=%s voucher_no=%s course_code=%r course_name=%r "
            "sales_person_name=%r combined_text=%r response_key=%s display_no=%s "
            "logical_name=%s "
            "x=%s y=%s max_width=%s font_size=%s "
            "draw_order=after_form_background_before_edit_objects",
            order_no,
            voucher_no,
            course_code,
            course_name,
            sales_person_name,
            combined_text,
            response_key,
            display_no,
            logical_name,
            DELIVERY_COURSE_X,
            SALES_REP_Y,
            combined_max_width,
            used_font_size,
        )
    values = ((data.get("construction_rep", ""), CONSTRUCTION_REP_Y),)
    if not course_name:
        values = ((data.get("sales_rep", ""), SALES_REP_Y),) + values
    for value, y in values:
        if value:
            _str(
                c,
                str(value),
                STAFF_TEXT_X,
                y,
                DETAIL_DATA_FONT_SIZE,
                max_w=STAFF_TEXT_RIGHT - STAFF_TEXT_X,
            )


# ── 移動伝票（取引区分8）ラベル / 単価・金額列下段表示 ────────────────────────
# 「移動伝票」ラベルは取引区分8のとき全伝票(01〜08)に表示する。
# 単価列・金額列の下段表示は得意先マスタ「納品書単価・金額下段（硝子）」=1 のとき
# 売上伝票(01)・工場控(02)・納品書(07) のみ対象。取引区分とは混ぜない。
MOVE_SLIP_LABEL = "移動伝票"
# 工事担当者名（CONSTRUCTION_REP_Y）の下に表示する。摘要/物件No/お客様注文No/QR/
# 会社情報/担当者名と重ならない位置。文字サイズは担当者名（DETAIL_DATA_FONT_SIZE）に合わせる。
MOVE_SLIP_LABEL_Y = CONSTRUCTION_REP_Y - 10.0


def _draw_move_slip_label(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """取引区分8のとき工事担当者名の下へ「移動伝票」を表示する（全伝票共通）。"""
    is_move_slip, move_slip_reason = _move_slip_decision(data)
    _log.info(
        "移動伝票ラベル表示判定: order_no=%r customer_code=%r customer_name=%r "
        "transaction_type=%r is_move_slip=%s move_slip_reason=%s",
        data.get("order_no", ""),
        data.get("customer_code", data.get("code_no", "")),
        data.get("customer_name", ""),
        data.get("transaction_type", ""),
        is_move_slip,
        move_slip_reason,
    )
    if not is_move_slip:
        return
    # 「移動伝票」は固定ラベルのため従来フォント（非太字）で描く。
    _str(c, MOVE_SLIP_LABEL, STAFF_TEXT_X, MOVE_SLIP_LABEL_Y, DETAIL_DATA_FONT_SIZE,
         max_w=STAFF_TEXT_RIGHT - STAFF_TEXT_X, font_name=LABEL_FONT_NAME)


def _is_move_slip(data: dict[str, Any]) -> bool:
    """移動伝票ラベル表示条件。取引区分が厳密に 8 / "8" のときだけ True。"""
    return is_move_slip_transaction_type(data.get("transaction_type"))


def _move_slip_decision(data: dict[str, Any]) -> tuple[bool, str]:
    is_move = _is_move_slip(data)
    return is_move, "transaction_type_8" if is_move else "not_transaction_type_8"


def is_invoice_price_amount_upper_glass_enabled(data: dict[str, Any]) -> bool:
    """得意先マスタ「納品書単価・金額上段（硝子）」が 1（有効）か判定する。

    伝票データに正規化済み bool（invoice_price_amount_upper_glass_enabled）が
    あればそれを優先し、無ければ生値から厳密に判定する（"1" のみ True）。
    bool(value) は使わない（"0" が True 扱いになる事故を防ぐ）。移動伝票とは別条件。
    """
    enabled = data.get("invoice_price_amount_upper_glass_enabled")
    if isinstance(enabled, bool):
        return enabled
    # 正規化済みフラグが無い場合のみ生値から判定する（従来条件は混ぜない）。
    return str(_invoice_price_amount_upper_glass_raw(data) or "").strip() == "1"


def _invoice_price_amount_upper_glass_raw(data: dict[str, Any]) -> Any:
    return data.get(
        "invoice_price_amount_upper_glass_raw",
        data.get("invoice_price_amount_upper_glass", ""),
    )


def _invoice_price_amount_lower_glass_raw(data: dict[str, Any]) -> Any:
    return data.get(
        "invoice_price_amount_lower_glass_raw",
        data.get("invoice_price_amount_lower_glass", ""),
    )


def is_invoice_price_amount_lower_glass_enabled(data: dict[str, Any]) -> bool:
    """得意先マスタ「納品書単価・金額下段（硝子）」が 1（有効）か判定する。

    単価・金額列の下段表示条件。取引区分や上段（硝子）値は一切見ない。
    """
    enabled = data.get("invoice_price_amount_lower_glass_enabled")
    if isinstance(enabled, bool):
        return enabled
    return str(_invoice_price_amount_lower_glass_raw(data) or "").strip() == "1"


def _price_amount_lower_decision(data: dict[str, Any]) -> tuple[bool, str]:
    """単価・金額列の下段（および合計行下段）の表示有無と理由を返す。

    取引区分は一切見ない。「納品書単価・金額下段（硝子）」が 1 のときだけ表示する。
    """
    if is_invoice_price_amount_lower_glass_enabled(data):
        return True, "invoice_lower_glass_1"
    return False, "hidden_invoice_lower_glass_not_1"


def _should_show_price_amount_lower(data: dict[str, Any]) -> bool:
    """単価・金額列の下段（および合計行下段）を表示すべきか判定する。"""
    show_lower, _reason = _price_amount_lower_decision(data)
    return show_lower


def _draw_move_slip_columns(c: rl_canvas.Canvas, data: dict[str, Any],
                            unit_rx: float, amt_rx: float) -> None:
    """単価列・金額列の下段、金額列合計行の下段を表示する。

    conditional の既存条件は「納品書単価・金額下段（硝子）=1」。
    always_show / always_hide は単価・明細金額・合計へ同じ共通判定を適用する。
    取引区分は見ない。
    """
    is_move, move_slip_reason = _move_slip_decision(data)
    upper_glass_enabled = is_invoice_price_amount_upper_glass_enabled(data)
    lower_glass_enabled = is_invoice_price_amount_lower_glass_enabled(data)
    show_lower, show_lower_reason = _price_amount_lower_decision(data)
    _log.info(
        "単価金額下段表示判定: order_no=%r customer_code=%r customer_name=%r "
        "transaction_type=%r is_move_slip=%s move_slip_reason=%s "
        "invoice_price_amount_upper_glass_raw=%r "
        "invoice_price_amount_upper_glass_enabled=%s "
        "invoice_price_amount_lower_glass_raw=%r "
        "invoice_price_amount_lower_glass_enabled=%s "
        "show_price_amount_lower=%s "
        "show_price_amount_lower_reason=%s",
        data.get("order_no", ""),
        data.get("customer_code", data.get("code_no", "")),
        data.get("customer_name", ""),
        data.get("transaction_type", ""),
        is_move,
        move_slip_reason,
        _invoice_price_amount_upper_glass_raw(data),
        upper_glass_enabled,
        _invoice_price_amount_lower_glass_raw(data),
        lower_glass_enabled,
        show_lower,
        show_lower_reason,
    )
    show_price_amount = should_draw_price_amount(data, show_lower)
    if not show_price_amount:
        return
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        if _is_star_row(row):
            continue
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yl = row_top - DET_LOWER_OFFSET   # 下段ベースライン
        unit = _parse_number_or_none(row.get("sales_unit_price"))
        if unit is not None:
            # 単価列下段: 売上単価（3桁区切り・右寄せ）。空欄・非数値は表示しない。
            _rstr(c, _format_total(unit), unit_rx, yl, DETAIL_UNIT_PRICE_FONT_SIZE, max_w=TBL_MAX_UNIT)
        qty = _parse_number_or_none(row.get("ordered_quantity"))
        if unit is not None and qty is not None:
            # 金額列下段: 売上単価 × 受注数量（元データの受注数量を使用・右寄せ）。
            _rstr(c, _format_total(unit * qty), amt_rx, yl, DETAIL_AMOUNT_FONT_SIZE, max_w=TBL_MAX_AMT)

    # 金額列の合計行下段: Σ(売上単価 × 受注数量)。
    total = calculate_sales_amount_total_for_move_slip(details)
    if total:
        total_lower_y = FORM_DETAIL_BOT - DET_LOWER_OFFSET
        _rstr(c, _format_total(total), amt_rx, total_lower_y, DETAIL_AMOUNT_FONT_SIZE)


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


def should_hide_quantity_by_unit_code(row: dict) -> bool:
    """明細行の数量列（受注数量＋数量単位名称）を空欄にすべきか判定する。

    数量単位コードが「19」の明細行のみ True。数値型 19 や " 19 " も正規化して判定する。
    「19」以外・空欄・未取得は False（従来通り数量を表示する）。
    判定ロジックは voucher_data_mapper に集約し、全帳票で共通の基準を使う。

    なお通常経路では voucher_data_mapper._detail_row が qty 文字列を組み立てる時点で
    既に空欄化済みのため、この関数は主にテスト・明示的な描画制御用の共通API。
    """
    return is_quantity_hidden_by_unit_code(row)


def should_draw_quantity(row: dict) -> bool:
    """数量列を描画すべきかどうか（should_hide_quantity_by_unit_code の否定）。"""
    return not should_hide_quantity_by_unit_code(row)


def upper_area_text_for_row(row: dict, key: str) -> str:
    """OP区分対象行だけ、単価・金額列上段の㎡文字列を返す。"""
    if not should_draw_upper_area_by_op_category(row):
        return ""
    return str(row.get(key, "") or "")


def _to_number(value: object) -> float:
    """単価・受注数量を数値へ変換する。空欄・非数値・記号付きは0扱い。

    表示用に '*' やカンマが付いた値でも数値部分のみを取り出す。
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r'-?[\d,]*\.?\d+', str(value).replace(',', ''))
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except ValueError:
        return 0.0


def calculate_unit_price_totals(rows: list[dict]) -> tuple[float, float]:
    """伝票ページの明細行から合計欄の上下2段を算出する。

    上段(sales_total)   = Σ(売上単価 × 受注数量)
    下段(purchase_total) = Σ(仕入単価 × 受注数量)

    金額列の合計ではなく、各行の元データ（売上単価・仕入単価・受注数量）から
    計算する。'*' 行（対象外行）や空行は合計に含めない。空欄・非数値は0扱い。
    """
    sales_total = 0.0
    purchase_total = 0.0
    for row in rows:
        if _is_star_row(row):
            continue
        qty = _to_number(row.get("ordered_quantity"))
        sales_total += _to_number(row.get("sales_unit_price")) * qty
        purchase_total += _to_number(row.get("purchase_unit_price")) * qty
    return sales_total, purchase_total


def is_move_slip_transaction_type(value: Any) -> bool:
    """取引区分が8（移動伝票）か判定する。文字列 "8"・数値 8 の両方を許容する。"""
    return str(value if value is not None else "").strip() == "8"


def _parse_number_or_none(value: Any) -> float | None:
    """数値変換できれば float、空欄・非数値なら None を返す（移動伝票の下段表示判定用）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = re.search(r'-?[\d,]*\.?\d+', text.replace(',', ''))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def calculate_sales_amount_total_for_move_slip(rows: list[dict]) -> float:
    """移動伝票(取引区分8)の金額列合計行下段 Σ(売上単価 × 受注数量) を算出する。

    name == "*" の対象外行・空行は対象外。空欄・非数値は0扱い。元データの受注数量を使う。
    右下合計欄の calculate_unit_price_totals とは役割が別（金額列の移動伝票専用合計）。
    """
    total = 0.0
    for row in rows:
        if _is_star_row(row):
            continue
        qty = _to_number(row.get("ordered_quantity"))
        total += _to_number(row.get("sales_unit_price")) * qty
    return total


def _format_total(value: float) -> str:
    """合計欄の表示整形。3桁区切り・整数なら小数なし、小数が出れば2桁に丸める。"""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


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


def _clip(c: rl_canvas.Canvas, text: str, max_w: float, fs: float,
          font_name: str = _FONT_NAME) -> str:
    base = _resolve_base_font(font_name)
    c.setFont(base, fs)
    while text and c.stringWidth(text, base, fs) > max_w:
        text = text[:-1]
    return text


def _str(c: rl_canvas.Canvas, text: str, x: float, y: float, fs: float,
         max_w: float | None = None, font_name: str = DATA_BOLD_FONT_NAME) -> None:
    if not text:
        return
    base = _resolve_base_font(font_name)
    c.setFont(base, fs)
    if max_w:
        text = _clip(c, text, max_w, fs, font_name)
    _emit_text(c, "drawString", x, y, text, _is_bold_font(font_name))


def _str_header_value(
    c: rl_canvas.Canvas,
    text: object,
    x: float,
    y: float,
    fs: float,
    max_w: float | None = None,
    font_name: str = DATA_BOLD_FONT_NAME,
    *,
    field: str = "",
    form_type: str = "",
    draw_path: str = "",
) -> None:
    """ヘッダー識別番号を共通I字間描画で左寄せする。"""
    draw_text_with_i_gap(
        c,
        "" if text is None else str(text).strip(),
        x,
        y,
        fs,
        max_width=max_w,
        font_name=font_name,
        align="left",
        field=field,
        form_type=form_type,
        draw_path=draw_path,
    )


def _has_i_gap_after(text: str, index: int) -> bool:
    """ASCII大文字I／全角大文字Ｉの直後に文字がある場合だけTrue。"""
    return index < len(text) - 1 and text[index] in {"I", "Ｉ"}


def i_spaced_text_width(
    c: rl_canvas.Canvas,
    text: object,
    font_name: str,
    font_size: float,
) -> float:
    """I字間補正を含む実描画幅を返す。"""
    value = "" if text is None else str(text)
    base = _resolve_base_font(font_name)
    gap_count = sum(
        1 for index in range(len(value)) if _has_i_gap_after(value, index)
    )
    return (
        c.stringWidth(value, base, font_size)
        + gap_count * HEADER_I_CHAR_GAP_PT
    )


def _clip_i_spaced_text(
    c: rl_canvas.Canvas,
    text: str,
    max_width: float,
    font_name: str,
    font_size: float,
) -> str:
    """補正幅込みで右端を越えない最長の先頭部分を返す。"""
    fitted = text
    while (
        fitted
        and i_spaced_text_width(c, fitted, font_name, font_size) > max_width
    ):
        fitted = fitted[:-1]
    return fitted


def _fit_i_spaced_font_size(
    c: rl_canvas.Canvas,
    text: str,
    max_width: float,
    font_name: str,
    base_font_size: float,
    min_font_size: float,
    *,
    step: float = 0.25,
) -> float:
    """I補正幅を含む総幅で、全文字が収まるフォントサイズを決める。"""
    font_size = base_font_size
    while (
        font_size - step >= min_font_size
        and i_spaced_text_width(c, text, font_name, font_size) > max_width
    ):
        font_size -= step
    width = i_spaced_text_width(c, text, font_name, font_size)
    if width > max_width:
        gap_width = sum(
            HEADER_I_CHAR_GAP_PT
            for index in range(len(text))
            if _has_i_gap_after(text, index)
        )
        base = _resolve_base_font(font_name)
        glyph_width = c.stringWidth(text, base, font_size)
        available = max(0.0, max_width - gap_width)
        if glyph_width > 0:
            font_size *= available / glyph_width
    return font_size


def draw_text_with_i_gap(
    c: rl_canvas.Canvas,
    text: object,
    anchor_x: float,
    y: float,
    font_size: float,
    *,
    max_width: float | None = None,
    min_font_size: float | None = None,
    font_name: str = DATA_BOLD_FONT_NAME,
    align: str = "left",
    field: str = "",
    form_type: str = "",
    draw_path: str = "",
) -> float:
    """I直後の4pt補正を幅計算と描画へ同一に適用する共通関数。

    ``anchor_x`` はleftなら開始X、centerなら中心X、rightなら右端X。
    補正対象がない文字列は従来どおり1回のdrawString系呼出しにする。
    """
    value = "" if text is None else str(text)
    if not value:
        return font_size
    if align not in {"left", "center", "right"}:
        raise ValueError("align must be left, center, or right")

    fitted = value
    used_font_size = font_size
    if max_width is not None:
        if min_font_size is None:
            fitted = _clip_i_spaced_text(
                c, fitted, max_width, font_name, used_font_size
            )
        else:
            used_font_size = _fit_i_spaced_font_size(
                c,
                fitted,
                max_width,
                font_name,
                used_font_size,
                min_font_size,
            )
    if not fitted:
        return used_font_size

    base = _resolve_base_font(font_name)
    bold = _is_bold_font(font_name)
    c.setFont(base, used_font_size)
    total_width = i_spaced_text_width(
        c, fitted, font_name, used_font_size
    )
    start_x = (
        anchor_x
        if align == "left"
        else anchor_x - total_width / 2.0
        if align == "center"
        else anchor_x - total_width
    )

    if not any(_has_i_gap_after(fitted, index) for index in range(len(fitted))):
        method = {
            "left": "drawString",
            "center": "drawCentredString",
            "right": "drawRightString",
        }[align]
        _emit_text(c, method, anchor_x, y, fitted, bold)
        return used_font_size

    segment_start = 0
    draw_x = start_x
    for index, char in enumerate(fitted):
        if not _has_i_gap_after(fitted, index):
            continue
        segment = fitted[segment_start:index + 1]
        _emit_text(c, "drawString", draw_x, y, segment, bold)
        draw_x += c.stringWidth(segment, base, used_font_size)
        draw_x += HEADER_I_CHAR_GAP_PT
        _log.info(
            "voucher_i_gap_applied field=%s index=%d gap_pt=%.3f "
            "form_type=%s draw_path=%s",
            field,
            index,
            HEADER_I_CHAR_GAP_PT,
            form_type,
            draw_path,
        )
        segment_start = index + 1
    tail = fitted[segment_start:]
    if tail:
        _emit_text(c, "drawString", draw_x, y, tail, bold)
    return used_font_size


def _name_debug_enabled() -> bool:
    """品名（商品名称）の描画直前デバッグログを出すか。

    通常は無効。テスト・デバッグ時に環境変数 VOUCHER_NAME_DEBUG を真値にすると、
    PDF描画直前の品名文字列を repr 形式でログ出力する（先頭スペース保持の確認用）。
    """
    return str(os.environ.get("VOUCHER_NAME_DEBUG") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def draw_text_fit_width(c: rl_canvas.Canvas, text: str, x: float, y: float,
                        max_width: float, font_name: str,
                        base_font_size: float, min_font_size: float,
                        *, step: float = 0.25) -> float:
    """`max_width` に収まるようフォントサイズを下げて左寄せ描画する。

    文字列は決して途中で切らない（省略記号も使わない）。スペース等は保持する。
    優先順位:
      1. `base_font_size` で収まればそのまま描く。
      2. 収まらなければ `step` ずつ下げ、`min_font_size` までで収まればそこで止める。
      3. `min_font_size` でも収まらなければ、収まる字号まで線形に縮小し全文字を表示する。
    描画に用いた最終フォントサイズを返す（テスト・検証用）。
    """
    if not text:
        return base_font_size
    base = _resolve_base_font(font_name)
    bold = _is_bold_font(font_name)
    fs = base_font_size
    # 2) 通常→min まで段階的に縮小。
    while fs - step >= min_font_size and c.stringWidth(text, base, fs) > max_width:
        fs -= step
    # 3) min でも収まらない長い名称は、収まる字号まで線形縮小（全文字表示を優先）。
    w = c.stringWidth(text, base, fs)
    if w > max_width and w > 0:
        fs = fs * max_width / w
    c.setFont(base, fs)
    _emit_text(c, "drawString", x, y, text, bold)
    return fs


def draw_text_fit_width_right(
    c: rl_canvas.Canvas, text: str, right_x: float, y: float,
    max_width: float, font_name: str, base_font_size: float,
    min_font_size: float, *, step: float = 0.25,
) -> float:
    """全文字を幅内へ縮小し、指定右端を維持して右寄せ描画する。"""
    if not text:
        return base_font_size
    base = _resolve_base_font(font_name)
    bold = _is_bold_font(font_name)
    fs = base_font_size
    while fs - step >= min_font_size and c.stringWidth(text, base, fs) > max_width:
        fs -= step
    width = c.stringWidth(text, base, fs)
    if width > max_width and width > 0:
        fs = fs * max_width / width
    c.setFont(base, fs)
    _emit_text(c, "drawRightString", right_x, y, text, bold)
    return fs


def _str_name(c: rl_canvas.Canvas, text: str, x: float, y: float, fs: float,
              max_w: float | None = None,
              min_fs: float = DETAIL_NAME_MIN_FONT_SIZE,
              font_name: str = DATA_BOLD_FONT_NAME) -> None:
    """品名列（商品名称）の描画。

    先頭・末尾・連続スペースを保持したままトリムは一切行わない。`max_w` を超える
    長い名称は `_clip` で切り捨てず、`draw_text_fit_width` でフォントを縮小して
    全文字を表示する（要件: 右端見切れ・省略の禁止）。デバッグ時のみ描画直前の値を
    repr で記録する。
    """
    if _name_debug_enabled():
        _log.info("product_name_display repr=%r", text)
    if not text:
        return
    if max_w:
        draw_text_fit_width(c, text, x, y, max_w, font_name, fs, min_fs)
    else:
        base = _resolve_base_font(font_name)
        c.setFont(base, fs)
        _emit_text(c, "drawString", x, y, text, _is_bold_font(font_name))


def _cstr(c: rl_canvas.Canvas, text: str, cx: float, y: float, fs: float,
          max_w: float | None = None, font_name: str = DATA_BOLD_FONT_NAME) -> None:
    """中央揃えで描画する。cx はカラム中心X。"""
    if not text:
        return
    base = _resolve_base_font(font_name)
    c.setFont(base, fs)
    if max_w:
        text = _clip(c, text, max_w, fs, font_name)
    _emit_text(c, "drawCentredString", cx, y, text, _is_bold_font(font_name))


def _rstr(c: rl_canvas.Canvas, text: str, rx: float, y: float, fs: float,
          max_w: float | None = None, font_name: str = DATA_BOLD_FONT_NAME) -> None:
    """右揃えで描画する。rx はカラム右端X。"""
    if not text:
        return
    base = _resolve_base_font(font_name)
    c.setFont(base, fs)
    if max_w:
        text = _clip(c, text, max_w, fs, font_name)
    _emit_text(c, "drawRightString", rx, y, text, _is_bold_font(font_name))


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
    lbl("コードNo", FORM_HDR_LEFT,     r1_lbl_y)
    lbl("得意先名", HDR_ROW1_DIVS[0],  r1_lbl_y)
    lbl("受注No",   HDR_ORDER_NO_X,             r1_lbl_y)
    lbl("仕上日",   HDR_ROW1_DIVS[-1], HDR_SHIAGE_LABEL_Y)
    _draw_shiage_month_day_labels(c)

    # 「殿」── 得意先名欄の右寄り。左罫線は描かない。
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(HDR_ORDER_NO_X - 5.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "殿")

    r2_lbl_y = FORM_HDR_MID - 8.0
    lbl("発行日",   FORM_HDR_LEFT, r2_lbl_y)
    lbl("納品日",   HDR_DELIVERY_X, r2_lbl_y)
    lbl("伝票No",   HDR_DELIVERY_RIGHT,         r2_lbl_y)
    lbl("取引区分", HDR_VOUCHER_RIGHT,         r2_lbl_y)
    lbl("出荷区分", HDR_TRADE_RIGHT,         r2_lbl_y)
    lbl("入力者名", HDR_OPERATOR_X,         r2_lbl_y)
    c.setFont(_FONT_NAME, AMPM_TEXT_FONT_SIZE)
    c.drawCentredString((HDR_AMPM_X + FORM_HDR_RIGHT) / 2, AMPM_BASELINE_Y, "AM・PM")

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
    display_names = load_processing_display_names()
    for i, label in enumerate(PROC_LABELS):
        item_top = TOP - i * item_h
        item_bot = item_top - item_h
        if i > 0:
            c.line(ML, item_top, CHK_R, item_top)
        # 加工名ラベルは1.2倍（要件5）。各セル内で縦中央寄せにし枠線と重ならないようにする。
        display_label = resolve_processing_display_name(label, display_names)
        draw_text_fit_width(
            c, display_label, ML + 3.0,
            item_bot + (item_h - PROCESS_LABEL_FONT_SIZE) / 2 + 0.5,
            CHK_R - ML - CB_W - 8.0, _FONT_NAME,
            PROCESS_LABEL_FONT_SIZE, 4.0,
        )
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

def _draw_shiage_month_day_labels(c: rl_canvas.Canvas) -> None:
    """仕上日サブセルの「月」（中央寄せ）「日」（右寄せ）ラベルを描画する（要件3）。

    01〜06 の仕上日欄で共通利用する。月・日の数値データ（_draw_header_finish_date）は
    各ラベルの左側に右寄せ配置されるため、ここでラベルだけを固定位置に描く。
    """
    c.setFont(_FONT_NAME, HDR_SHIAGE_LABEL_FS)
    c.drawCentredString(HDR_SHIAGE_MONTH_LABEL_CX, HDR_SHIAGE_MONTH_DAY_Y, "月")
    c.drawRightString(HDR_SHIAGE_DAY_LABEL_RX, HDR_SHIAGE_MONTH_DAY_Y, "日")


def _draw_header_finish_date(c: rl_canvas.Canvas, finish_date: Any) -> None:
    """画面行設定の仕上日をヘッダー「仕上日」欄へ 〇月〇日 形式で描画する。

    OLAP取得データに仕上日があっても、画面で設定した値（finish_date）を優先する。
    月の数値は「月」ラベルの左、日の数値は「日」ラベルの左へ大きめフォントで
    右寄せ配置し、「月」「日」ラベルと重ならないようにする（要件3）。
    """
    if not finish_date:
        return
    month = getattr(finish_date, "month", None)
    day = getattr(finish_date, "day", None)
    if month is None or day is None:
        return
    c.setFont(_FONT_NAME, HEADER_FINISH_DATE_VALUE_FONT_SIZE)
    c.drawRightString(HDR_SHIAGE_MONTH_DATA_RX, HDR_SHIAGE_MONTH_DAY_Y, str(month))
    c.drawRightString(HDR_SHIAGE_DAY_DATA_RX, HDR_SHIAGE_MONTH_DAY_Y, str(day))


def _draw_ampm_circle(c: rl_canvas.Canvas, am_pm: Any) -> None:
    """画面行設定の AM/PM に応じて「AM・PM」欄の該当文字へ丸印を描画する。

    「なし」（"none" / 空）の場合は AM・PM のどちらにも丸を付けない（要件1）。
    """
    stripped = str(am_pm or "").strip()
    if not stripped or stripped.lower() == "none":
        return
    # 文字は1.2倍（要件1）。丸の中心位置は実際に描画される1.2倍テキストに合わせて算出する。
    fs = AMPM_TEXT_FONT_SIZE
    base_fs = AMPM_TEXT_BASE_FONT_SIZE
    baseline = AMPM_BASELINE_Y
    cx = (HDR_AMPM_X + FORM_HDR_RIGHT) / 2
    total_w = c.stringWidth("AM・PM", _FONT_NAME, fs)
    left = cx - total_w / 2
    am_w = c.stringWidth("AM", _FONT_NAME, fs)
    sep_w = c.stringWidth("・", _FONT_NAME, fs)
    pm_w = c.stringWidth("PM", _FONT_NAME, fs)
    if stripped.upper().startswith("P"):
        seg_center = left + am_w + sep_w + pm_w / 2
        base_seg_w = c.stringWidth("PM", _FONT_NAME, base_fs)
    else:
        seg_center = left + am_w / 2
        base_seg_w = c.stringWidth("AM", _FONT_NAME, base_fs)
    cy = baseline + fs * 0.32
    # 丸の半径は基準フォント時の半径を1.2倍にする（要件2）。線幅は別管理で変更しない。
    rx = (base_seg_w / 2 + 3.0) * AMPM_CIRCLE_SCALE
    ry = (base_fs * 0.62) * AMPM_CIRCLE_SCALE
    c.saveState()
    c.setLineWidth(AMPM_CIRCLE_LINE_WIDTH)
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
    box_w = DELIV_STAMP_W
    box_h = DELIV_STAMP_H
    gap = DELIV_STAMP_GAP
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
    lbl("コードNo", FORM_HDR_LEFT,     r1_lbl_y)
    lbl("得意先名", HDR_ROW1_DIVS[0],  r1_lbl_y)
    lbl("受注No",   HDR_ORDER_NO_X,             r1_lbl_y)
    lbl("仕上日",   HDR_ROW1_DIVS[-1], HDR_SHIAGE_LABEL_Y)
    _draw_shiage_month_day_labels(c)
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(HDR_ORDER_NO_X - 5.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "殿")
    r2_lbl_y = FORM_HDR_MID - 8.0
    lbl("発行日",   FORM_HDR_LEFT, r2_lbl_y)
    lbl("納品日",   HDR_DELIVERY_X, r2_lbl_y)
    lbl("伝票No",   HDR_DELIVERY_RIGHT,         r2_lbl_y)
    lbl("取引区分", HDR_VOUCHER_RIGHT,         r2_lbl_y)
    lbl("出荷区分", HDR_TRADE_RIGHT,         r2_lbl_y)
    lbl("入力者名", HDR_OPERATOR_X,         r2_lbl_y)
    c.setFont(_FONT_NAME, AMPM_TEXT_FONT_SIZE)
    c.drawCentredString((HDR_AMPM_X + FORM_HDR_RIGHT) / 2, AMPM_BASELINE_Y, "AM・PM")

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

    # ─── 下部チェック欄 ───────────────────────────────────────────────────────
    _draw_lower_section(c)

    # ─── 印枠（工場印/商品課印/配送者印） ──────────────────────────────────────
    if stamp_title:
        _draw_stamp_box(c, stamp_title)


def _draw_form_data_shizu(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    """指図書系フォームにデータを印字する（品名・数量・受入日のみ表示）。"""
    FS_VAL = DATA_FONT_SIZE
    FS_DIM = DETAIL_DATA_FONT_SIZE

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

    # ヘッダー行1（データは1.3倍・下線ギリギリまで下寄せ。要件1/3）
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    _str_header_value(c, data.get("code_no", ""), FORM_HDR_LEFT + DATA_X_PAD, r1_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_ROW1_DIVS[0] - FORM_HDR_LEFT - DATA_X_PAD,
        field="code_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_shizu")
    val(data.get("customer_name", ""), x=HDR_ROW1_DIVS[0] + DATA_X_PAD, y=r1_y,
        fs=HEADER_CUSTOMER_VALUE_FONT_SIZE, max_w=_customer_max_w())
    _str_header_value(c, data.get("order_no", ""), HDR_ORDER_NO_X + DATA_X_PAD, r1_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_AMPM_X - HDR_ORDER_NO_X - DATA_X_PAD,
        field="order_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_shizu")

    # ヘッダー行2（データは1.3倍。取引区分データも出荷区分と同じ1.3倍・要件1）
    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    val(data.get("issue_date", ""),    x=FORM_HDR_LEFT + DATA_X_PAD, y=r2_y,
        fs=HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_DELIVERY_X - FORM_HDR_LEFT - DATA_X_PAD)
    val(data.get("delivery_date", ""), x=HDR_DELIVERY_X + DATA_X_PAD,  y=r2_y, fs=HEADER_NOUHIN_VALUE_FONT_SIZE, max_w=HDR_DELIVERY_RIGHT - HDR_DELIVERY_X - DATA_X_PAD)
    _str_header_value(c, data.get("voucher_no", ""), HDR_DELIVERY_RIGHT + DATA_X_PAD, r2_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_VOUCHER_RIGHT - HDR_DELIVERY_RIGHT - DATA_X_PAD,
        field="voucher_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_shizu")
    val(data.get("trade_type", ""),    x=HDR_VOUCHER_RIGHT + DATA_X_PAD, y=r2_y,
        fs=HEADER_TRADE_VALUE_FONT_SIZE, max_w=HDR_TRADE_RIGHT - HDR_VOUCHER_RIGHT - DATA_X_PAD)
    val(data.get("ship_type", ""),     x=HDR_TRADE_RIGHT + DATA_X_PAD, y=r2_y,
        fs=HEADER_SHIPPING_VALUE_FONT_SIZE, max_w=HDR_OPERATOR_X - HDR_TRADE_RIGHT - DATA_X_PAD)
    val(data.get("operator", ""),      x=HDR_OPERATOR_X + DATA_X_PAD, y=r2_y,
        fs=HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_AMPM_X - HDR_OPERATOR_X - DATA_X_PAD)

    # 受入日列の表示X。日付/場所は「加」のすぐ右の同一列に揃える。
    nyuki_x = SHIZU_TBL_COLS[-2] + DATA_X_PAD   # 受入日列左端（636.5pt）
    nyuki_data_x = nyuki_x + 16.0
    # 右端「受入日」列は売上伝票の摘要列と同じフォントサイズに揃える。
    FS_NYUKI = TABLE_REMARK_FONT_SIZE

    # 明細行
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET

        is_star = _is_star_row(row)

        # 品名（1段目=左寄せ、2段目=右寄せ大フォント）
        _str_name(c, row.get("name", ""), TBL_X_NAME, yu, DETAIL_NAME_FONT_SIZE, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, DETAIL_DIM_FONT_SIZE, max_w=TBL_MAX_NAME)

        if not is_star:
            # 数量（1段目=左寄せ、2段目=右寄せ）
            _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
            # 数量2段目はセル中央あたりへ寄せる（要件3）。他列の下段(yl)より上に置く。
            _rstr(c, row.get("qty", ""), DET_QTY_RX, row_top - DET_QTY_LOWER_OFFSET,
                  DETAIL_QTY_VALUE_FONT_SIZE, max_w=TBL_MAX_QTY)

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

    # 摘要 / 物件No データ（直前バージョンのサイズから1.1倍）
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    _draw_summary_lines(c, data, SUMMARY_VALUE_FONT_SIZE)
    _draw_staff_values(c, data)
    for index, line in enumerate(
        [line for line in data.get("property_lines", []) if line][:1]
    ):
        val(str(line), x=note_line_x, y=FORM_BKNO_BOT + 3.0 + index * 9.0,
            fs=PROPERTY_VALUE_FONT_SIZE, max_w=SUMMARY_TEXT_RIGHT - note_line_x)
    _draw_customer_order_no(c, data)

    # QR コード
    qr_order_no = str(data.get("qr_order_no") or data.get("order_no") or "")
    if qr_order_no:
        qr_buf = build_qr_code_image(qr_order_no)
        c.drawImage(ImageReader(qr_buf), FORM_LWR_RIGHT - 58.0, FORM_LWR_BOT + 12.0,
                    width=44.0, height=44.0, mask="auto")

    # 画面行設定（仕上日・AM/PM・加工名チェック）を反映
    _draw_row_settings(c, data)

    # 移動伝票(取引区分8)ラベル（指図書(1)/(2)・梱包明細書・配送指示書共通）。
    # 単価列・金額列の下段表示は対象外（売上伝票/工場控/納品書のみ）。
    _draw_move_slip_label(c, data)


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
        _apply_edit_object_pdf_state(c, obj)
        c.setStrokeColorRGB(*stroke_rgb)
        c.setFillColorRGB(*text_rgb)
        try:
            if obj_type == "symbol_text":
                draw_symbol_text(c, obj)
            elif obj_type == "image":
                _draw_edit_image(c, obj, obj_id)
            elif obj_type == "text":
                fs = float(obj.get("font_size") or 10.0)
                object_bold = _object_text_style(obj, "bold")
                object_italic = _object_text_style(obj, "italic")
                object_font, object_font_metadata = _resolve_edit_text_font(
                    obj.get("font_family"), object_bold, object_italic,
                    str(obj.get("text", "")))
                object_font_metadata = _font_metadata_for_text(
                    object_font_metadata, str(obj.get("text", "")),
                    requested_bold=object_bold, requested_italic=object_italic)
                _log_pdf_text_style_received(obj, object_font, object_font_metadata)
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
                    c, str(obj.get("text", "")), x, y, w, h, object_font, fs,
                    text_align="left",
                    vertical_align="top",
                    color=obj.get("text_color") or obj.get("color") or "#000000",
                    bold=object_bold,
                    italic=object_italic,
                    synthetic_bold=bool(object_font_metadata.get("synthetic_bold")),
                    synthetic_italic=bool(object_font_metadata.get("synthetic_italic")),
                    underline=_object_text_style(obj, "underline"),
                    strikeout=_object_text_style(obj, "strikeout"),
                    font_metadata=object_font_metadata,
                    trace_id=str(obj.get("_edit_render_trace_id") or ""),
                    object_id=obj_id,
                    edit_objects_sha256=str(
                        obj.get("_edit_objects_sha256") or ""),
                )
            elif obj_type == "line":
                c.setLineWidth(float(obj.get("line_width") or 1.0))
                x1 = float(obj.get("x1", 0.0)); y1 = float(obj.get("y1", 0.0))
                x2 = float(obj.get("x2", 0.0)); y2 = float(obj.get("y2", 0.0))
                line_type = normalize_line_type(obj.get("line_type"))
                pdf_x1, pdf_y1, pdf_x2, pdf_y2 = _scene_line_to_pdf(x1, y1, x2, y2)
                _log.debug(
                    "edit_object object_id=%s type=%s line_type=%s "
                    "scene_x1=%s scene_y1=%s scene_x2=%s scene_y2=%s "
                    "pdf_x1=%s pdf_y1=%s pdf_x2=%s pdf_y2=%s PAGE_W=%s PAGE_H=%s",
                    obj_id, obj_type, line_type, x1, y1, x2, y2, pdf_x1, pdf_y1,
                    pdf_x2, pdf_y2, PAGE_W, PAGE_H,
                )
                # 矢じり線分・二重平行線は scene 座標で計算し、各端点を PDF 座標へ
                # 変換して描く（編集画面と同じ line_decorations ロジックを使う）。
                for sx1, sy1, sx2, sy2 in line_segments(line_type, x1, y1, x2, y2):
                    px1, py1, px2, py2 = _scene_line_to_pdf(sx1, sy1, sx2, sy2)
                    c.line(px1, py1, px2, py2)
                if debug_boxes:
                    _draw_debug_line_points(c, pdf_x1, pdf_y1, pdf_x2, pdf_y2)
            elif obj_type == "freehand":
                # 手書きフリーハンド。scene座標の points をPDF座標へ変換し、丸い継ぎ目の
                # ポリラインとして描く（編集画面と同じ見え方・同じ位置に出す）。
                c.setLineWidth(float(obj.get("pen_width") or obj.get("line_width") or 1.0))
                c.setLineCap(1)   # round cap
                c.setLineJoin(1)  # round join
                raw_points = obj.get("points") or []
                pdf_points: list[tuple[float, float]] = []
                for p in raw_points:
                    try:
                        sx = float(p[0]); sy = float(p[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    pdf_points.append(_scene_point_to_pdf_point(sx, sy))
                if len(pdf_points) >= 2:
                    path = c.beginPath()
                    path.moveTo(pdf_points[0][0], pdf_points[0][1])
                    for px, py in pdf_points[1:]:
                        path.lineTo(px, py)
                    c.drawPath(path, stroke=1, fill=0)
            elif obj_type == "freehand_layer":
                # 手書きレイヤー。visible=false は描画しない（要件6）。各 stroke の
                # pen_width / stroke_color を反映し、scene座標をPDF座標へ変換して描く。
                if not bool(obj.get("visible", True)):
                    pass
                else:
                    c.setLineCap(1)   # round cap
                    c.setLineJoin(1)  # round join
                    layer_pw = float(obj.get("pen_width") or obj.get("line_width") or 1.0)
                    layer_color = obj.get("stroke_color") or "#000000"
                    for stroke in (obj.get("strokes") or []):
                        if not isinstance(stroke, dict):
                            continue
                        raw_points = stroke.get("points") or []
                        pdf_points = []
                        for p in raw_points:
                            try:
                                sx = float(p[0]); sy = float(p[1])
                            except (TypeError, ValueError, IndexError):
                                continue
                            pdf_points.append(_scene_point_to_pdf_point(sx, sy))
                        if len(pdf_points) < 2:
                            continue
                        c.setLineWidth(float(stroke.get("pen_width") or layer_pw))
                        s_rgb = _coerce_rgb(stroke.get("stroke_color") or layer_color)
                        if s_rgb is not None:
                            c.setStrokeColorRGB(*s_rgb)
                        path = c.beginPath()
                        path.moveTo(pdf_points[0][0], pdf_points[0][1])
                        for px, py in pdf_points[1:]:
                            path.lineTo(px, py)
                        c.drawPath(path, stroke=1, fill=0)
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
                    object_bold = _object_text_style(obj, "bold")
                    object_italic = _object_text_style(obj, "italic")
                    object_font, object_font_metadata = _resolve_edit_text_font(
                        obj.get("font_family"), object_bold, object_italic, inner)
                    object_font_metadata = _font_metadata_for_text(
                        object_font_metadata, inner,
                        requested_bold=object_bold, requested_italic=object_italic)
                    _log_pdf_text_style_received(obj, object_font, object_font_metadata)
                    c.setFillColorRGB(*text_rgb)
                    draw_text_in_scene_rect(
                        c, inner, scene_x, scene_y, w, h, object_font, fs,
                        text_align=str(obj.get("text_align") or "center"),
                        vertical_align=str(obj.get("vertical_align") or "middle"),
                        color=obj.get("text_color") or obj.get("color") or "#000000",
                        bold=object_bold,
                        italic=object_italic,
                        synthetic_bold=bool(object_font_metadata.get("synthetic_bold")),
                        synthetic_italic=bool(object_font_metadata.get("synthetic_italic")),
                        underline=_object_text_style(obj, "underline"),
                        strikeout=_object_text_style(obj, "strikeout"),
                        font_metadata=object_font_metadata,
                        trace_id=str(obj.get("_edit_render_trace_id") or ""),
                        object_id=obj_id,
                        edit_objects_sha256=str(
                            obj.get("_edit_objects_sha256") or ""),
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
                    object_bold = _object_text_style(obj, "bold")
                    object_italic = _object_text_style(obj, "italic")
                    object_font, object_font_metadata = _resolve_edit_text_font(
                        obj.get("font_family"), object_bold, object_italic, inner)
                    object_font_metadata = _font_metadata_for_text(
                        object_font_metadata, inner,
                        requested_bold=object_bold, requested_italic=object_italic)
                    _log_pdf_text_style_received(obj, object_font, object_font_metadata)
                    c.setFillColorRGB(*text_rgb)
                    draw_text_in_scene_rect(
                        c, inner, scene_x, scene_y, w, h, object_font, fs,
                        text_align=str(obj.get("text_align") or "center"),
                        vertical_align=str(obj.get("vertical_align") or "middle"),
                        color=obj.get("text_color") or obj.get("color") or "#000000",
                        bold=object_bold,
                        italic=object_italic,
                        synthetic_bold=bool(object_font_metadata.get("synthetic_bold")),
                        synthetic_italic=bool(object_font_metadata.get("synthetic_italic")),
                        underline=_object_text_style(obj, "underline"),
                        strikeout=_object_text_style(obj, "strikeout"),
                        font_metadata=object_font_metadata,
                        trace_id=str(obj.get("_edit_render_trace_id") or ""),
                        object_id=obj_id,
                        edit_objects_sha256=str(
                            obj.get("_edit_objects_sha256") or ""),
                    )
        finally:
            c.restoreState()


def _apply_edit_object_pdf_state(c: rl_canvas.Canvas,
                                 obj: dict[str, Any]) -> None:
    """透明度とscene基準の回転を文字・装飾線を含むオブジェクト全体へ適用する。"""
    try:
        opacity = max(0.0, min(1.0, float(obj.get("opacity", 1.0))))
    except (TypeError, ValueError):
        opacity = 1.0
    if hasattr(c, "setFillAlpha"):
        c.setFillAlpha(opacity)
    if hasattr(c, "setStrokeAlpha"):
        c.setStrokeAlpha(opacity)
    try:
        rotation = float(obj.get("rotation", 0.0))
    except (TypeError, ValueError):
        rotation = 0.0
    if not rotation or not hasattr(c, "translate") or not hasattr(c, "rotate"):
        return
    kind = obj.get("type")
    if kind in {"line", "freehand"}:
        if kind == "line":
            cx = (float(obj.get("x1", 0.0)) + float(obj.get("x2", 0.0))) / 2.0
            cy = (float(obj.get("y1", 0.0)) + float(obj.get("y2", 0.0))) / 2.0
        else:
            points = obj.get("points") or []
            valid = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
            if not valid:
                return
            cx = (min(p[0] for p in valid) + max(p[0] for p in valid)) / 2.0
            cy = (min(p[1] for p in valid) + max(p[1] for p in valid)) / 2.0
    elif kind == "symbol_text":
        cx = float(obj.get("x", 0.0)); cy = float(obj.get("y", 0.0))
    else:
        cx = float(obj.get("x", 0.0)) + float(
            obj.get("width") or obj.get("w") or 0.0) / 2.0
        cy = float(obj.get("y", 0.0)) + float(
            obj.get("height") or obj.get("h") or 0.0) / 2.0
    pdf_cx, pdf_cy = _scene_point_to_pdf_point(cx, cy)
    c.translate(pdf_cx, pdf_cy)
    c.rotate(-rotation)
    c.translate(-pdf_cx, -pdf_cy)


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
    bold: bool = False,
    italic: bool = False,
    synthetic_bold: bool | None = None,
    synthetic_italic: bool = False,
    underline: bool = False,
    strikeout: bool = False,
    font_metadata: dict[str, Any] | None = None,
    trace_id: str = "",
    object_id: object = None,
    edit_objects_sha256: str = "",
) -> None:
    """scene矩形内の水平・垂直配置に従ってテキストをPDFへ描画する。"""
    c = canvas
    if synthetic_bold is None:
        synthetic_bold = bool(bold) and font_name == _FONT_NAME
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
        run_details = (
            _text_font_run_details(line, font_name, font_metadata, bold, italic)
            if font_metadata is not None else []
        )
        if run_details:
            _log_pdf_text_font_runs(
                run_details, object_id=object_id,
                requested_family=font_metadata.get("requested_family", ""),
                requested_bold=bold, requested_italic=italic)
        needs_run_drawing = bool(run_details) and (
            len(run_details) != 1
            or run_details[0]["font_name"] != font_name
            or bool(run_details[0]["synthetic_bold"]) != bool(synthetic_bold)
            or bool(run_details[0]["synthetic_italic"]) != bool(synthetic_italic)
        )
        if needs_run_drawing:
            runs = [
                (run["text"], run["font_name"], run["synthetic_bold"],
                 run["synthetic_italic"])
                for run in run_details
            ]
            draw_styled_pdf_text_runs(
                c, runs, draw_x, baseline, font_size, text_align=text_align,
                trace_id=trace_id, object_id=object_id,
                requested_italic=italic,
                edit_objects_sha256=edit_objects_sha256)
        else:
            draw_styled_pdf_text(
                c, line, draw_x, baseline, font_name, font_size,
                text_align=text_align, synthetic_bold=bool(synthetic_bold),
                synthetic_italic=synthetic_italic, trace_id=trace_id,
                object_id=object_id, requested_italic=italic,
                edit_objects_sha256=edit_objects_sha256)
        if needs_run_drawing:
            _draw_pdf_text_run_decorations(
                c, run_details, draw_x, baseline, font_size,
                text_align=text_align, underline=underline, strikeout=strikeout,
                color=rgb)
        else:
            _draw_pdf_text_decorations(
                c, line, draw_x, baseline, font_name, font_size,
                text_align=text_align, underline=underline, strikeout=strikeout,
                color=rgb, synthetic_bold=synthetic_bold,
                synthetic_italic=synthetic_italic)


def _draw_pdf_text_decorations(
    c: rl_canvas.Canvas, text: str, anchor_x: float, baseline_y: float,
    font_name: str, font_size: float, *, text_align: str = "left",
    underline: bool = False, strikeout: bool = False,
    color: tuple[float, float, float] | None = None,
    synthetic_bold: bool = False, synthetic_italic: bool = False,
) -> None:
    """ReportLabの線で下線・取り消し線を文字幅に合わせて描く。"""
    if not text or not (underline or strikeout):
        return
    try:
        base_width = float(pdfmetrics.stringWidth(text, font_name, font_size))
    except Exception:
        base_width = float(len(text)) * font_size
    # シアーされた字形の上端はadvance幅より右へ張り出す。下線が字形より短く
    # ならないよう、em高相当の張り出しと疑似太字の重ね幅を加える。
    right_overhang = (
        abs(TEXT_SYNTHETIC_ITALIC_SHEAR) * font_size
        if synthetic_italic else 0.0
    )
    if synthetic_bold:
        right_overhang += TEXT_SYNTHETIC_BOLD_OFFSET_PT
    decorated_width = base_width + right_overhang
    if text_align == "right":
        x1 = anchor_x - decorated_width
        x2 = anchor_x
    elif text_align == "center":
        x1 = anchor_x - decorated_width / 2.0
        x2 = anchor_x + decorated_width / 2.0
    else:
        x1 = anchor_x
        x2 = anchor_x + base_width + right_overhang
    if color is not None:
        c.setStrokeColorRGB(*color)
    c.setLineWidth(max(0.45, font_size * 0.045))
    if underline:
        c.line(x1, baseline_y - font_size * 0.12, x2, baseline_y - font_size * 0.12)
    if strikeout:
        c.line(x1, baseline_y + font_size * 0.30, x2, baseline_y + font_size * 0.30)


def _draw_pdf_text_run_decorations(
    c: rl_canvas.Canvas, runs: list[dict[str, Any]], anchor_x: float,
    baseline_y: float, font_size: float, *, text_align: str = "left",
    underline: bool = False, strikeout: bool = False,
    color: tuple[float, float, float] | None = None,
) -> None:
    """font runの実advance合計と最大張り出しに合わせて水平装飾線を描く。"""
    if not runs or not (underline or strikeout):
        return
    widths: list[float] = []
    for run in runs:
        try:
            widths.append(float(pdfmetrics.stringWidth(
                run["text"], run["font_name"], font_size)))
        except Exception:
            widths.append(float(len(run["text"])) * font_size)
    base_width = sum(widths)
    right_overhang = 0.0
    prefix_width = 0.0
    for run, run_width in zip(runs, widths):
        run_overhang = (
            abs(TEXT_SYNTHETIC_ITALIC_SHEAR) * font_size
            if run["synthetic_italic"] else 0.0)
        if run["synthetic_bold"]:
            run_overhang += TEXT_SYNTHETIC_BOLD_OFFSET_PT
        right_overhang = max(
            right_overhang, prefix_width + run_width + run_overhang - base_width)
        prefix_width += run_width
    decorated_width = base_width + right_overhang
    if text_align == "right":
        x1, x2 = anchor_x - decorated_width, anchor_x
    elif text_align == "center":
        x1 = anchor_x - decorated_width / 2.0
        x2 = anchor_x + decorated_width / 2.0
    else:
        x1, x2 = anchor_x, anchor_x + decorated_width
    if color is not None:
        c.setStrokeColorRGB(*color)
    c.setLineWidth(max(0.45, font_size * 0.045))
    if underline:
        c.line(x1, baseline_y - font_size * 0.12, x2,
               baseline_y - font_size * 0.12)
    if strikeout:
        c.line(x1, baseline_y + font_size * 0.30, x2,
               baseline_y + font_size * 0.30)


def draw_symbol_text(canvas: rl_canvas.Canvas, obj: dict[str, Any]) -> None:
    """中心アンカーの短い注記テキストをPDFへ描画する。"""
    c = canvas
    text = str(obj.get("text", "")).strip()
    if not text:
        return
    bold = _object_text_style(obj, "bold")
    italic = _object_text_style(obj, "italic")
    underline = _object_text_style(obj, "underline")
    strikeout = _object_text_style(obj, "strikeout")
    font_name, font_metadata = _resolve_edit_text_font(
        obj.get("font_family"), bold, italic, text)
    font_metadata = _font_metadata_for_text(
        font_metadata, text, requested_bold=bold, requested_italic=italic)
    _log_pdf_text_style_received(obj, font_name, font_metadata)
    synthetic_bold = bool(font_metadata.get("synthetic_bold"))
    synthetic_italic = bool(font_metadata.get("synthetic_italic"))
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
    trace_kwargs = {
        "trace_id": str(obj.get("_edit_render_trace_id") or ""),
        "object_id": obj.get("id"),
        "requested_italic": italic,
        "edit_objects_sha256": str(obj.get("_edit_objects_sha256") or ""),
    }
    baseline_y = pdf_y - font_size * 0.35
    run_details = _text_font_run_details(
        text, font_name, font_metadata, bold, italic)
    if run_details:
        _log_pdf_text_font_runs(
            run_details, object_id=obj.get("id"),
            requested_family=font_metadata.get("requested_family", ""),
            requested_bold=bold, requested_italic=italic)
    needs_run_drawing = bool(run_details) and (
        len(run_details) != 1
        or run_details[0]["font_name"] != font_name
        or bool(run_details[0]["synthetic_bold"]) != synthetic_bold
        or bool(run_details[0]["synthetic_italic"]) != synthetic_italic
    )
    if needs_run_drawing:
        runs = [
            (run["text"], run["font_name"], run["synthetic_bold"],
             run["synthetic_italic"])
            for run in run_details
        ]
        draw_styled_pdf_text_runs(
            c, runs, pdf_x, baseline_y, font_size, text_align="center",
            **trace_kwargs)
        _draw_pdf_text_run_decorations(
            c, run_details, pdf_x, baseline_y, font_size,
            text_align="center", underline=underline, strikeout=strikeout,
            color=rgb)
    else:
        draw_styled_pdf_text(
            c, text, pdf_x, baseline_y, font_name, font_size,
            text_align="center", synthetic_bold=synthetic_bold,
            synthetic_italic=synthetic_italic, **trace_kwargs)
        _draw_pdf_text_decorations(
            c, text, pdf_x, baseline_y, font_name, font_size,
            text_align="center", underline=underline, strikeout=strikeout,
            color=rgb, synthetic_bold=synthetic_bold,
            synthetic_italic=synthetic_italic)


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
    FS_VAL = DATA_FONT_SIZE
    FS_DIM = DETAIL_DATA_FONT_SIZE

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

    # ヘッダー行1（ベースライン: 下線ギリギリまで下寄せ。データは1.3倍・要件1/3）
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    _str_header_value(c, data.get("code_no", ""), FORM_HDR_LEFT + DATA_X_PAD, r1_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_ROW1_DIVS[0] - FORM_HDR_LEFT - DATA_X_PAD,
        field="code_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_01")
    val(data.get("customer_name", ""), x=HDR_ROW1_DIVS[0] + DATA_X_PAD, y=r1_y,
        fs=HEADER_CUSTOMER_VALUE_FONT_SIZE, max_w=_customer_max_w())
    _str_header_value(c, data.get("order_no", ""), HDR_ORDER_NO_X + DATA_X_PAD, r1_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_AMPM_X - HDR_ORDER_NO_X - DATA_X_PAD,
        field="order_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_01")

    # ヘッダー行2（データは1.3倍・要件1/3。取引区分データも出荷区分と同じ1.3倍・要件1）
    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    val(data.get("issue_date", ""),    x=FORM_HDR_LEFT + DATA_X_PAD, y=r2_y,
        fs=HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_DELIVERY_X - FORM_HDR_LEFT - DATA_X_PAD)
    val(data.get("delivery_date", ""), x=HDR_DELIVERY_X + DATA_X_PAD,  y=r2_y, fs=HEADER_NOUHIN_VALUE_FONT_SIZE, max_w=HDR_DELIVERY_RIGHT - HDR_DELIVERY_X - DATA_X_PAD)
    _str_header_value(c, data.get("voucher_no", ""), HDR_DELIVERY_RIGHT + DATA_X_PAD, r2_y,
        HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_VOUCHER_RIGHT - HDR_DELIVERY_RIGHT - DATA_X_PAD,
        field="voucher_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_form_data_01")
    val(data.get("trade_type", ""),    x=HDR_VOUCHER_RIGHT + DATA_X_PAD, y=r2_y,
        fs=HEADER_TRADE_VALUE_FONT_SIZE, max_w=HDR_TRADE_RIGHT - HDR_VOUCHER_RIGHT - DATA_X_PAD)
    val(data.get("ship_type", ""),     x=HDR_TRADE_RIGHT + DATA_X_PAD, y=r2_y,
        fs=HEADER_SHIPPING_VALUE_FONT_SIZE, max_w=HDR_OPERATOR_X - HDR_TRADE_RIGHT - DATA_X_PAD)
    val(data.get("operator", ""),      x=HDR_OPERATOR_X + DATA_X_PAD, y=r2_y,
        fs=HEADER_MAIN_VALUE_FONT_SIZE, max_w=HDR_AMPM_X - HDR_OPERATOR_X - DATA_X_PAD)

    # 列右端 X（セル配置用）
    unit_rx  = TBL_COLS[4] - DATA_X_PAD           # 単価列右端
    amt_rx   = TBL_COLS[5] - DATA_X_PAD           # 金額列右端
    note_rx  = TBL_COLS[6] - DATA_X_PAD           # 摘要列右端（受注No/伝票No表示に使用）

    # 摘要列左右分割の最大幅
    note_left_max_w  = TBL_NOTE_MID_X - TBL_COLS[5] - TBL_NOTE_MID_PAD
    note_right_max_w = TBL_COLS[6] - TBL_NOTE_MID_X - TBL_NOTE_MID_PAD - DATA_X_PAD

    # 明細行
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET   # 上段ベースライン
        yl = row_top - DET_LOWER_OFFSET   # 下段ベースライン

        is_star = _is_star_row(row)

        # 品名列: 1段目=左寄せ、2段目=右寄せ（大フォント）
        _str_name(c, row.get("name", ""), TBL_X_NAME, yu, DETAIL_NAME_FONT_SIZE, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, DETAIL_DIM_FONT_SIZE, max_w=TBL_MAX_NAME)

        if not is_star:
            # 数量列: 1段目=左寄せ、2段目=右寄せ
            _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
            # 数量2段目はセル中央あたりへ寄せる（要件3）。他列の下段(yl)より上に置く。
            _rstr(c, row.get("qty", ""), DET_QTY_RX, row_top - DET_QTY_LOWER_OFFSET,
                  DETAIL_QTY_VALUE_FONT_SIZE, max_w=TBL_MAX_QTY)

            # 単価・金額（右揃え）
            _rstr(c, unit_price_text_for_mode(
                data, upper_area_text_for_row(row, "unit_price")),
                unit_rx, yu, DETAIL_UNIT_PRICE_FONT_SIZE, max_w=TBL_MAX_UNIT)
            _rstr(c, line_amount_text_for_mode(
                data, upper_area_text_for_row(row, "amount")),
                amt_rx, yu, DETAIL_AMOUNT_FONT_SIZE, max_w=TBL_MAX_AMT)

            # 摘要: 数値は左側、加工記号は数値の右隣、日付/場所はそのすぐ右に揃える。
            notes = row.get("note_lines", [])
            finish_date = row.get("finish_date", "")
            note_text_x = TBL_NOTE_MID_X + TBL_NOTE_MID_PAD
            note_data_x = note_text_x + 16.0

            # 摘要列データは専用フォント（基準の1.2倍）で描く（要件4）。
            FS_NOTE = DETAIL_NOTE_FONT_SIZE

            def draw_note_text(text: str, y: float) -> None:
                if not text:
                    return
                if text == "加":
                    _str(c, text, note_text_x, y, FS_NOTE, max_w=note_right_max_w)
                else:
                    _str(c, text, note_data_x, y, FS_NOTE, max_w=note_right_max_w)

            if notes:
                note_rows = _split_note_rows(notes[0])
                num, txt = note_rows[0]
                _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yu, FS_NOTE, max_w=note_left_max_w)
                draw_note_text(txt, yu)
                if len(note_rows) > 1 and len(notes) == 1:
                    num, txt = note_rows[1]
                    _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yl, FS_NOTE, max_w=note_left_max_w)
                    draw_note_text(txt, yl)
            if finish_date:
                _str(c, finish_date, note_data_x, yu, FS_NOTE, max_w=note_right_max_w)
            if len(notes) > 1:
                num, txt = _split_note_rows(notes[1])[0]
                _rstr(c, num, TBL_NOTE_MID_X - TBL_NOTE_MID_PAD, yl, FS_NOTE, max_w=note_left_max_w)
                draw_note_text(txt, yl)

    # 合計行: 摘要列の右端に上段・下段の合計を右揃えで表示。
    # 上段=Σ(売上単価×受注数量)、下段=Σ(仕入単価×受注数量)。金額列合計ではない。
    total_upper_y = FORM_DETAIL_BOT - DET_UPPER_OFFSET
    total_lower_y = FORM_DETAIL_BOT - DET_LOWER_OFFSET
    sales_total, purchase_total = calculate_unit_price_totals(details)
    if sales_total:
        _rstr(c, _format_total(sales_total), note_rx, total_upper_y, DETAIL_NOTE_FONT_SIZE)
    if purchase_total:
        _rstr(c, _format_total(purchase_total), note_rx, total_lower_y, DETAIL_NOTE_FONT_SIZE)

    # 受注No / 伝票No（表の摘要列右下外側）
    order_no = str(data.get("order_no", "") or "")
    voucher_no = str(data.get("voucher_no", "") or "")
    central_no_max_width = TBL_COLS[6] - TBL_COLS[5] - DATA_X_PAD * 2
    if order_no:
        draw_text_with_i_gap(
            c, f"受  {order_no}", note_rx, FORM_TOTAL_BOT - 7.0, FS_DIM,
            max_width=central_no_max_width, min_font_size=5.0,
            align="right", field="order_no", draw_path="_draw_form_data_01_central")
    if voucher_no:
        draw_text_with_i_gap(
            c, f"伝  {voucher_no}", note_rx, FORM_TOTAL_BOT - 16.0, FS_DIM,
            max_width=central_no_max_width, min_font_size=5.0,
            align="right", field="voucher_no", draw_path="_draw_form_data_01_central")

    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    _draw_summary_lines(c, data, SUMMARY_VALUE_FONT_SIZE)
    _draw_staff_values(c, data)
    for index, line in enumerate([line for line in data.get("property_lines", []) if line][:1]):
        val(str(line), x=note_line_x, y=FORM_BKNO_BOT + 3.0 + index * 9.0,
            fs=PROPERTY_VALUE_FONT_SIZE, max_w=SUMMARY_TEXT_RIGHT - note_line_x)
    _draw_customer_order_no(c, data)

    # 移動伝票(取引区分8)専用表示（売上伝票/工場控）。8以外は何も描画しない。
    _draw_move_slip_columns(c, data, unit_rx, amt_rx)
    _draw_move_slip_label(c, data)

    qr_order_no = str(data.get("qr_order_no") or data.get("order_no") or "")
    if qr_order_no:
        qr_buf = build_qr_code_image(qr_order_no)
        c.drawImage(ImageReader(qr_buf), FORM_LWR_RIGHT - 58.0, FORM_LWR_BOT + 12.0,
                    width=44.0, height=44.0, mask="auto")

    # 画面行設定（仕上日・AM/PM・加工名チェック）を反映
    _draw_row_settings(c, data)


def _build_scratch_01(data: dict[str, Any],
                       title: str = "売　上　伝　票",
                       edit_objects: list[dict[str, Any]] | None = None) -> bytes:
    """売上伝票(01)または工場控を一から描画したPDFをバイト列で返す。"""
    _ensure_font()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    _draw_form_01(c, data, title)
    _draw_edit_objects(c, edit_objects)
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
    lbl("得意先名", HDR_ROW1_DIVS[0], r1_lbl_y)
    lbl("受注No", HDR_ORDER_NO_X, r1_lbl_y)
    c.setFont(_FONT_NAME, 11)
    c.drawRightString(HDR_ORDER_NO_X - 5.0, (FORM_HDR_MID + FORM_HDR_TOP) / 2 - 5.0, "御中")

    r2_lbl_y = FORM_HDR_MID - 8.0
    c.setFont(_FONT_NAME, 6.0)
    lbl("納品日", HDR_DELIVERY_X, r2_lbl_y)
    lbl("伝票No", HDR_DELIVERY_RIGHT, r2_lbl_y)
    lbl("取引区分", HDR_VOUCHER_RIGHT, r2_lbl_y)
    lbl("出荷区分", HDR_TRADE_RIGHT, r2_lbl_y)
    lbl("入力者名", HDR_OPERATOR_X, r2_lbl_y)


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
    FS_VAL = DATA_FONT_SIZE
    FS_MAIN = HEADER_MAIN_VALUE_FONT_SIZE
    # ヘッダーデータは1.3倍・下線ギリギリまで下寄せ（要件1/3）。取引区分のみ据え置き。
    r1_y = FORM_HDR_MID + HDR_DATA_Y_INNER
    _str_header_value(c, data.get("code_no", ""), FORM_HDR_LEFT + DATA_X_PAD, r1_y, FS_MAIN,
         max_w=HDR_ROW1_DIVS[0] - FORM_HDR_LEFT - DATA_X_PAD,
         field="code_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_delivery_data_common")
    _str(c, data.get("customer_name", ""), HDR_ROW1_DIVS[0] + DATA_X_PAD, r1_y,
         HEADER_CUSTOMER_VALUE_FONT_SIZE, max_w=_customer_max_w())
    _str_header_value(c, data.get("order_no", ""), HDR_ORDER_NO_X + DATA_X_PAD, r1_y, FS_MAIN,
         max_w=HDR_AMPM_X - HDR_ORDER_NO_X - DATA_X_PAD,
         field="order_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_delivery_data_common")

    # 発行日セルは納品書/受領書ではマスクされるため issue_date は描画しない。
    r2_y = FORM_HDR_BOT + HDR_DATA_Y_INNER
    _str(c, data.get("delivery_date", ""), HDR_DELIVERY_X + DATA_X_PAD, r2_y, HEADER_NOUHIN_VALUE_FONT_SIZE,
         max_w=HDR_DELIVERY_RIGHT - HDR_DELIVERY_X - DATA_X_PAD)
    _str_header_value(c, data.get("voucher_no", ""), HDR_DELIVERY_RIGHT + DATA_X_PAD, r2_y, FS_MAIN,
         max_w=HDR_VOUCHER_RIGHT - HDR_DELIVERY_RIGHT - DATA_X_PAD,
         field="voucher_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_delivery_data_common")
    _str(c, data.get("trade_type", ""), HDR_VOUCHER_RIGHT + DATA_X_PAD, r2_y,
         HEADER_TRADE_VALUE_FONT_SIZE, max_w=HDR_TRADE_RIGHT - HDR_VOUCHER_RIGHT - DATA_X_PAD)
    _str(c, data.get("ship_type", ""), HDR_TRADE_RIGHT + DATA_X_PAD, r2_y,
         HEADER_SHIPPING_VALUE_FONT_SIZE, max_w=HDR_OPERATOR_X - HDR_TRADE_RIGHT - DATA_X_PAD)
    _str(c, data.get("operator", ""), HDR_OPERATOR_X + DATA_X_PAD, r2_y, FS_MAIN, max_w=HDR_AMPM_X - HDR_OPERATOR_X - DATA_X_PAD)


def _draw_delivery_details_07(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_VAL = DATA_FONT_SIZE
    FS_DIM = DETAIL_DATA_FONT_SIZE
    unit_rx = TBL_COLS[4] - DATA_X_PAD
    amt_rx = TBL_COLS[5] - DATA_X_PAD
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET
        is_star = _is_star_row(row)
        _str_name(c, row.get("name", ""), TBL_X_NAME, yu, DETAIL_NAME_FONT_SIZE, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, DETAIL_DIM_FONT_SIZE, max_w=TBL_MAX_NAME)
        if is_star:
            continue
        _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
        # 数量2段目はセル中央あたりへ寄せる（要件3）。
        _rstr(c, row.get("qty", ""), DET_QTY_RX, row_top - DET_QTY_LOWER_OFFSET,
              DETAIL_QTY_VALUE_FONT_SIZE, max_w=TBL_MAX_QTY)
        _rstr(c, unit_price_text_for_mode(
            data, upper_area_text_for_row(row, "unit_price")),
            unit_rx, yu, DETAIL_UNIT_PRICE_FONT_SIZE, max_w=TBL_MAX_UNIT)
        _rstr(c, line_amount_text_for_mode(
            data, upper_area_text_for_row(row, "amount")),
            amt_rx, yu, DETAIL_AMOUNT_FONT_SIZE, max_w=TBL_MAX_AMT)


def _draw_delivery_details_08(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    FS_VAL = DATA_FONT_SIZE
    details = data.get("details", [])[:FORM_DETAIL_ROWS]
    for i, row in enumerate(details):
        row_top = FORM_TBL_HDR_BOT - i * FORM_DETAIL_ROW_H
        yu = row_top - DET_UPPER_OFFSET
        yl = row_top - DET_LOWER_OFFSET
        is_star = _is_star_row(row)
        _str_name(c, row.get("name", ""), TBL_X_NAME, yu, DETAIL_NAME_FONT_SIZE, max_w=TBL_MAX_NAME)
        _rstr(c, row.get("dims", ""), DET_NAME_RX - DIM_SHIFT_LEFT, yl, DETAIL_DIM_FONT_SIZE, max_w=TBL_MAX_NAME)
        if is_star:
            continue
        _str(c, row.get("qty_spec", ""), TBL_X_QTY, yu, FS_VAL, max_w=TBL_MAX_QTY)
        # 数量2段目はセル中央あたりへ寄せる（要件3）。
        _rstr(c, row.get("qty", ""), DET_QTY_RX, row_top - DET_QTY_LOWER_OFFSET,
              DETAIL_QTY_VALUE_FONT_SIZE, max_w=TBL_MAX_QTY)


def _draw_summary_rows(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    note_line_x = FORM_HDR_LEFT + FORM_SUBROW_LBL_W + 18.0
    c.setLineWidth(0.5)
    c.setFont(_FONT_NAME, 7.0)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_SUM_BOT + 3.0, "摘　要")
    c.line(note_line_x, FORM_SUM_BOT, FORM_SUM_RIGHT, FORM_SUM_BOT)
    c.drawString(FORM_HDR_LEFT + 18.0, FORM_BKNO_BOT + 3.0, "物件No")
    c.line(note_line_x, FORM_BKNO_BOT, FORM_SUM_RIGHT, FORM_BKNO_BOT)
    # 摘要・物件Noデータは他伝票（01〜06）と同じ専用フォントを使う（要件3/4）。
    # 摘要上段の上方向シフトも _draw_summary_lines / _summary_line_y 経由で共通適用される。
    _draw_summary_lines(c, data, SUMMARY_VALUE_FONT_SIZE)
    _draw_staff_values(c, data)
    for index, line in enumerate([line for line in data.get("property_lines", []) if line][:1]):
        _str(c, str(line), note_line_x, FORM_BKNO_BOT + 3.0 + index * 9.0, PROPERTY_VALUE_FONT_SIZE,
             max_w=SUMMARY_TEXT_RIGHT - note_line_x)
    _draw_customer_order_no(c, data)


def _draw_form_07(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    _draw_common_delivery_header(c, "納　品　書", data)
    _draw_delivery_table_07(c)
    _draw_delivery_data_common(c, data)
    _draw_delivery_details_07(c, data)
    _draw_summary_rows(c, data)
    _draw_special_notes_section(c)
    # 移動伝票(取引区分8)専用表示（納品書）。8以外は何も描画しない。受領書(08)には反映しない。
    unit_rx = TBL_COLS[4] - DATA_X_PAD
    amt_rx = TBL_COLS[5] - DATA_X_PAD
    _draw_move_slip_columns(c, data, unit_rx, amt_rx)
    _draw_move_slip_label(c, data)


def _draw_form_08(c: rl_canvas.Canvas, data: dict[str, Any]) -> None:
    _draw_common_delivery_header(c, "受　領　書", data)
    _draw_receipt_table_08(c)
    _draw_delivery_data_common(c, data)
    _draw_delivery_details_08(c, data)
    _draw_summary_rows(c, data)
    _draw_delivery_stamp_boxes(c)
    _draw_special_notes_section(c)
    # 移動伝票(取引区分8)ラベル（受領書）。単価列・金額列の下段表示は対象外。
    _draw_move_slip_label(c, data)


def _build_scratch_delivery(data: dict[str, Any], voucher_id: str,
                            edit_objects: list[dict[str, Any]] | None = None) -> bytes:
    _ensure_font()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    if voucher_id == "07":
        _draw_form_07(c, data)
    else:
        _draw_form_08(c, data)
    _draw_edit_objects(c, edit_objects)
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
    _str_header_value(c, data.get("code_no", ""), HDR1_CODE_NO_X, y1, FS_HEADER,
                      field="code_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_header_overlay")
    _str(c, data.get("customer_name", ""), HDR1_CUSTOMER_X, y1, FS_HEADER, max_w=HDR1_CUSTOMER_MAX)
    _str_header_value(c, data.get("order_no", ""), HDR1_ORDER_NO_X, y1, FS_HEADER,
                      field="order_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_header_overlay")
    _str(c, data.get("shiage_date", ""),   HDR1_SHIAGE_X,   y1, FS_HEADER)
    y2 = HEADER_ROW2_Y
    _str(c, data.get("issue_date", ""),    HDR2_ISSUE_DATE_X, y2, FS_HEADER)
    _str(c, data.get("delivery_date", ""), HDR2_DELIVERY_X,   y2, FS_HEADER)
    _str_header_value(c, data.get("voucher_no", ""), HDR2_VOUCHER_NO_X, y2, FS_HEADER,
                      field="voucher_no", form_type=str(data.get("voucher_id", "")), draw_path="_draw_header_overlay")
    _str(c, data.get("trade_type", ""),    HDR2_TRADE_TYPE_X, y2, FS_HEADER)
    _str(c, data.get("ship_type", ""),     HDR2_SHIP_TYPE_X,  y2, FS_HEADER)
    _str(c, data.get("operator", ""),      HDR2_OPERATOR_X,   y2, FS_HEADER, max_w=HDR2_OPERATOR_MAX)


def _draw_details_overlay(c: rl_canvas.Canvas, details: list[dict[str, Any]], data: dict[str, Any] | None = None) -> None:
    data = data or {}
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
        _str_name(c, row.get("name", ""), COL_NAME_X, yu, FS_DETAIL, max_w=MAX_W_NAME)
        _str(c, row.get("dims", ""), COL_NAME_X, yl, FS_DIMS,   max_w=MAX_W_NAME)
        _str(c, row.get("qty_spec", ""), COL_QTY_X, yu, FS_DETAIL, max_w=MAX_W_QTY)
        _str(c, row.get("qty", ""),      COL_QTY_X, yl, FS_DETAIL, max_w=MAX_W_QTY)
        _str(c, unit_price_text_for_mode(
            data, upper_area_text_for_row(row, "unit_price")),
            COL_UNIT_X, yu, FS_DETAIL, max_w=MAX_W_UNIT)
        _str(c, line_amount_text_for_mode(
            data, upper_area_text_for_row(row, "amount")),
            COL_AMOUNT_X, yu, FS_DETAIL, max_w=MAX_W_AMOUNT)
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
    _draw_details_overlay(c, data.get("details", []), data)
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
    *,
    edit_render_trace_id: str = "",
    bypass_preview_cache: bool = False,
) -> bytes:
    """指定された伝票種別のページを結合した PDF を bytes で返す。ファイル保存しない。"""
    trace_id = str(
        edit_render_trace_id or print_data.get("_edit_render_trace_id") or uuid.uuid4()
    )
    _log.info(
        "event=voucher_pdf_assemble_started trace_id=%s renderer_revision=%s "
        "bypass_preview_cache=%s",
        trace_id, PDF_TEXT_RENDERER_REVISION, bool(bypass_preview_cache),
    )
    writer = pypdf.PdfWriter()
    for page_data in _normalize_pages_data(print_data):
        # 編集オブジェクトを受注Noで解決し、各伝票では target_vouchers に当該伝票が
        # 含まれるものだけを重ね描きする（要件3・7）。
        edit_objects = _resolve_edit_objects(page_data)
        for vid in voucher_ids:
            objs = []
            for source_obj in _filter_edit_objects(edit_objects, vid):
                obj = dict(source_obj)
                obj["_edit_render_trace_id"] = trace_id
                obj["_edit_objects_sha256"] = page_data.get(
                    "_edit_objects_sha256", "")
                obj["_edit_data_revision"] = page_data.get(
                    "_edit_data_revision", 0)
                obj.setdefault("_edit_scope", "preset")
                obj.setdefault("_edit_voucher_no", page_data.get("voucher_no", ""))
                obj["_pdf_voucher_id"] = vid
                objs.append(obj)
            if vid == "01":
                page_bytes = _build_scratch_01(page_data, edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "02":
                page_bytes = _build_scratch_01(page_data, title="工　場　控", edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "03":
                page_bytes = _build_scratch_shizu(page_data, title="指　図　書　(1)", stamp_title="工場印",
                                                  edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "04":
                page_bytes = _build_scratch_shizu(page_data, title="指　図　書　(2)", stamp_title="商品課印",
                                                  edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "05":
                page_bytes = _build_scratch_shizu(page_data, title="梱　包　明　細　書", stamp_title="配送者印",
                                                  edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid == "06":
                page_bytes = _build_scratch_shizu(page_data, title="配　送　指　示　書", stamp_title="配送者印",
                                                  edit_objects=objs)
                page_reader = pypdf.PdfReader(io.BytesIO(page_bytes))
                writer.add_page(page_reader.pages[0])
            elif vid in ("07", "08"):
                page_bytes = _build_scratch_delivery(page_data, vid, edit_objects=objs)
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
    pdf_bytes = buf.getvalue()
    _log.info(
        "event=voucher_pdf_bytes_ready trace_id=%s pdf_sha256=%s "
        "edit_objects_sha256=%s renderer_revision=%s",
        trace_id, hashlib.sha256(pdf_bytes).hexdigest(),
        print_data.get("_edit_objects_sha256", ""),
        PDF_TEXT_RENDERER_REVISION,
    )
    return pdf_bytes


def _prepare_pdf_generation_data(
    print_data: dict[str, Any], *, trace_id: str,
    reload_edit_objects: bool,
) -> dict[str, Any]:
    """PDF workerへ渡す直前に最新編集JSONをdeep copyし、snapshotを確定する。"""
    prepared = copy.deepcopy(print_data)
    prepared["_edit_render_trace_id"] = trace_id
    page_hashes: list[str] = []
    document_hashes: list[str] = []
    revisions: list[int] = []
    for page in _normalize_pages_data(prepared):
        order_no = str(page.get("order_no") or prepared.get("order_no") or "").strip()
        voucher_no = page.get("voucher_no")
        if reload_edit_objects and order_no:
            from app.voucher_edit_objects import (
                load_edit_document_metadata,
                load_edit_objects,
            )
            objects = load_edit_objects(order_no, voucher_no=voucher_no)
            metadata = load_edit_document_metadata(order_no)
            page["edit_objects"] = copy.deepcopy(objects)
            document_hash = str(metadata.get("edit_objects_sha256") or "")
            revision = int(metadata.get("edit_revision") or 0)
        else:
            objects = copy.deepcopy(
                page.get("edit_objects") if isinstance(page.get("edit_objects"), list)
                else _resolve_edit_objects(page)
            )
            page["edit_objects"] = objects
            document_hash = ""
            revision = int(page.get("_edit_data_revision") or 0)
        from app.voucher_edit_objects import edit_objects_sha256
        page_hash = edit_objects_sha256(objects)
        effective_hash = document_hash or page_hash
        page["_edit_render_trace_id"] = trace_id
        page["_edit_objects_sha256"] = effective_hash
        page["_page_edit_objects_sha256"] = page_hash
        page["_edit_data_revision"] = revision
        page_hashes.append(page_hash)
        document_hashes.append(effective_hash)
        revisions.append(revision)
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            _log.info(
                "event=voucher_pdf_worker_input trace_id=%s object_id=%s text=%r "
                "font_family=%r font_size=%s bold=%s italic=%s underline=%s "
                "strikeout=%s x=%s y=%s edit_scope=%s voucher_no=%s "
                "edit_data_revision=%s edit_objects_sha256=%s deep_copy=true",
                trace_id, obj.get("id"), obj.get("text"), obj.get("font_family"),
                obj.get("font_size"), _object_text_style(obj, "bold"),
                _object_text_style(obj, "italic"),
                _object_text_style(obj, "underline"),
                _object_text_style(obj, "strikeout"), obj.get("x"), obj.get("y"),
                obj.get("_edit_scope", "preset"), voucher_no, revision,
                effective_hash,
            )
    request_hash = hashlib.sha256(json.dumps(
        document_hashes, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if len(set(document_hashes)) == 1 and document_hashes:
        request_hash = document_hashes[0]
    prepared["_edit_objects_sha256"] = request_hash
    prepared["_edit_data_revision"] = max(revisions, default=0)
    return prepared


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
        return load_edit_objects(
            order_no, voucher_no=page_data.get("voucher_no"))
    except Exception:
        return []


def _normalize_pages_data(print_data: dict[str, Any]) -> list[dict[str, Any]]:
    pages = print_data.get("pages")
    if isinstance(pages, list) and pages:
        mode = print_data.get("price_display_mode")
        result: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            if mode is not None and "price_display_mode" not in page:
                page = dict(page)
                page["price_display_mode"] = mode
            result.append(page)
        return result
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


# ── テスト印刷用の簡易PDF（印刷設定確認用）─────────────────────────────────
# 用紙サイズ（mm, 縦向き基準の 幅 x 高さ）。JIS規格（伝票はB5=182x257mm）。
_TEST_PRINT_PAPER_MM: dict[str, tuple[float, float]] = {
    "B5": (182.0, 257.0),
    "B4": (257.0, 364.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A3": (297.0, 420.0),
    "LETTER": (215.9, 279.4),
}


def _test_print_page_size_pt(paper_size: str, orientation: str) -> tuple[float, float]:
    """用紙サイズ/印刷方向から reportlab のページサイズ(pt)を返す。

    未知の用紙サイズはB5にフォールバックする。orientation が landscape の
    ときは幅と高さを入れ替えて横向きにする（縦向きなら縦のまま）。
    """
    from reportlab.lib.units import mm

    key = str(paper_size or "").strip().upper() or "B5"
    width_mm, height_mm = _TEST_PRINT_PAPER_MM.get(key, _TEST_PRINT_PAPER_MM["B5"])
    width_pt, height_pt = width_mm * mm, height_mm * mm
    if str(orientation or "").strip().lower() == "landscape":
        return (max(width_pt, height_pt), min(width_pt, height_pt))
    return (min(width_pt, height_pt), max(width_pt, height_pt))


def build_test_print_pdf_bytes(settings: Any) -> bytes:
    """印刷設定確認用の簡易テスト印刷PDFを生成してバイト列で返す。

    既存の伝票テンプレートには依存せず、reportlab で1ページの確認用PDFを描く。
    用紙サイズ・印刷方向は settings（画面上の現在値）に従う（B5/A4、縦/横）。
    """
    from reportlab.lib.units import mm

    _ensure_font()

    paper_size = str(getattr(settings, "paper_size", "B5") or "B5").strip().upper() or "B5"
    orientation = str(getattr(settings, "orientation", "landscape") or "landscape").strip().lower()
    page_w, page_h = _test_print_page_size_pt(paper_size, orientation)

    orientation_label = "横" if orientation == "landscape" else "縦"
    color_mode = str(getattr(settings, "color_mode", "") or "").strip().lower()
    color_label = "カラー" if color_mode == "color" else "モノクロ（グレースケール）"
    scale_mode = str(getattr(settings, "scale_mode", "") or "").strip().lower()
    scale_label = "用紙に合わせる" if scale_mode == "fit_to_page" else "実サイズ"

    lines = [
        ("プリンター", str(getattr(settings, "printer_name", "") or "(未選択)")),
        ("用紙サイズ", paper_size),
        ("印刷方向", orientation_label),
        ("色", color_label),
        ("部数", str(getattr(settings, "copies", 1))),
        ("印刷倍率", scale_label),
        ("印刷方式", str(getattr(settings, "print_backend", "") or "")),
    ]

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle("TksToKintone テスト印刷")

    left = 20 * mm
    y = page_h - 22 * mm

    c.setFont(_FONT_NAME, 20)
    c.drawString(left, y, "TksToKintone テスト印刷")
    y -= 10 * mm

    c.setLineWidth(1)
    c.line(left, y, page_w - 20 * mm, y)
    y -= 12 * mm

    c.setFont(_FONT_NAME, 11)
    c.drawString(left, y, f"日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    y -= 9 * mm

    c.setFont(_FONT_NAME, 12)
    for label, value in lines:
        c.drawString(left, y, f"{label}: {value}")
        y -= 8 * mm

    y -= 6 * mm
    c.setFont(_FONT_NAME, 11)
    c.drawString(left, y, "このページは印刷設定確認用です。")

    c.showPage()
    c.save()
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


def save_named_pdf_bytes(
    pdf_bytes: bytes,
    output_dir: Path | None = None,
    base_dir: Path | None = None,
    filename_stem: str = "unknown",
) -> Path:
    """PDFバイト列を「指定名.pdf」で保存する（タイムスタンプなし）。

    同名ファイルが既に存在する場合は上書きせず _2, _3... の連番を付ける。
    受注No別に「1394161_伝票.pdf」を作る用途で使う（選択PDF作成）。
    """
    if output_dir is None:
        output_dir = get_default_voucher_output_dir(base_dir)
    output_dir = ensure_voucher_output_dir(output_dir)

    safe_stem = _sanitize_filename_token(filename_stem) or "unknown"
    output_path = output_dir / f"{safe_stem}.pdf"
    if output_path.exists():
        index = 2
        while True:
            candidate = output_dir / f"{safe_stem}_{index}.pdf"
            if not candidate.exists():
                output_path = candidate
                break
            index += 1
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
    *,
    edit_render_trace_id: str | None = None,
    reload_edit_objects: bool = False,
    bypass_preview_cache: bool = False,
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
    trace_id = str(edit_render_trace_id or uuid.uuid4())
    print_data = _prepare_pdf_generation_data(
        print_data, trace_id=trace_id,
        reload_edit_objects=reload_edit_objects,
    )
    _check_templates(voucher_ids, base_dir)

    if output_dir is None:
        output_dir = get_default_voucher_output_dir(base_dir)
    output_dir = ensure_voucher_output_dir(output_dir)

    output_path = _build_output_pdf_path(output_dir, _filename_token_from_print_data(print_data))

    try:
        pdf_bytes = _assemble_pdf_bytes(
            voucher_ids, print_data, base_dir,
            edit_render_trace_id=trace_id,
            bypass_preview_cache=bypass_preview_cache,
        )
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        _log_pdf_generation_exception(voucher_ids, print_data)
        raise RuntimeError("PDF生成中にエラーが発生しました。ログを確認してください。") from exc

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
    index = 2
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
                first_page.get("order_no"),
                first_page.get("voucher_no"),
                first_page.get("delivery_no"),
            ])
    candidates.extend([
        print_data.get("order_no"),
        print_data.get("voucher_no"),
        print_data.get("delivery_no"),
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
    *,
    edit_render_trace_id: str | None = None,
    reload_edit_objects: bool = False,
    bypass_preview_cache: bool = False,
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
    trace_id = str(edit_render_trace_id or uuid.uuid4())
    print_data = _prepare_pdf_generation_data(
        print_data, trace_id=trace_id,
        reload_edit_objects=reload_edit_objects,
    )
    _check_templates(voucher_ids, base_dir)

    try:
        return _assemble_pdf_bytes(
            voucher_ids, print_data, base_dir,
            edit_render_trace_id=trace_id,
            bypass_preview_cache=bypass_preview_cache,
        )
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        _log_pdf_generation_exception(voucher_ids, print_data)
        raise RuntimeError("PDF生成中にエラーが発生しました。ログを確認してください。") from exc


def _log_pdf_generation_exception(voucher_ids: list[str],
                                  print_data: dict[str, Any]) -> None:
    """画面へ出さないPDF文字属性と完全なtracebackを開発ログへ残す。"""
    pages = _normalize_pages_data(print_data)
    contexts: list[dict[str, Any]] = []
    order_numbers: list[str] = []
    for page in pages:
        order_numbers.append(str(page.get("order_no") or ""))
        try:
            objects = _resolve_edit_objects(page)
        except Exception:
            objects = []
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") not in {
                "text", "symbol_text", "rectangle", "ellipse"
            }:
                continue
            bold = _object_text_style(obj, "bold")
            italic = _object_text_style(obj, "italic")
            family = str(obj.get("font_family") or "")
            key = (family, bold, italic)
            metadata = _EDIT_FONT_METADATA.get(key, {})
            contexts.append({
                "selected_font": family,
                "bold": bold,
                "italic": italic,
                "underline": _object_text_style(obj, "underline"),
                "strikeout": _object_text_style(obj, "strikeout"),
                "resolved_font_file": metadata.get("resolved_font_file", "unresolved"),
                "resolved_pdf_font_name": metadata.get(
                    "resolved_pdf_font_name", "unresolved"),
                "fallback_used": metadata.get("fallback_used", "unknown"),
                "synthetic_bold_used": metadata.get("synthetic_bold", "unknown"),
                "synthetic_italic_used": metadata.get("synthetic_italic", "unknown"),
            })
    _log.exception(
        "pdf_generation_failed order_no=%r voucher_types=%r text_styles=%r",
        order_numbers, voucher_ids, contexts,
    )
