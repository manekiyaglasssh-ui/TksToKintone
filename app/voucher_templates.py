"""伝票テンプレート定義。

座標系: reportlab の (x, y) = ページ左下が原点。
ページサイズ: 729.4 x 515.5 pt (約257 x 182 mm、横向き)

アプリ描画方式の定数は FORM_* / HDR_* / TBL_* プレフィックスで管理する。
オーバーレイ方式(02-08)向けの旧座標定数も後方互換のために保持する。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# ── 伝票種別 ──────────────────────────────────────────────────────────────────

VOUCHER_TYPES: list[tuple[str, str]] = [
    ("01", "売上伝票"),
    ("02", "工場控"),
    ("03", "指図書(1)"),
    ("04", "指図書(2)"),
    ("05", "梱包明細書"),
    ("06", "配送指示書"),
    ("07", "納品書"),
    ("08", "受領書"),
]

VOUCHER_IDS = [vid for vid, _ in VOUCHER_TYPES]
VOUCHER_NAMES = {vid: name for vid, name in VOUCHER_TYPES}


def template_path(voucher_id: str, base_dir: Path | None = None) -> Path:
    root = base_dir or Path(__file__).resolve().parents[1]
    return root / "img" / f"sample_denpyou_{voucher_id}.pdf"


# ── ページサイズ ───────────────────────────────────────────────────────────────

PAGE_W: float = 729.4
PAGE_H: float = 515.5

# ══════════════════════════════════════════════════════════════════════════════
# アプリ描画方式 (売上伝票 01) のフォームレイアウト定数
# ══════════════════════════════════════════════════════════════════════════════

# ─── 用紙余白 ─────────────────────────────────────────────────────────────────
PAGE_MARGIN_X: float = 22.0   # 左右余白
PAGE_MARGIN_Y: float = 15.0   # 上下余白

FORM_ML: float   = PAGE_MARGIN_X               # 22  左端
FORM_MR: float   = PAGE_W - PAGE_MARGIN_X      # 707.4 → 右端
PRINT_SAFE_BOTTOM_MARGIN_PT: float = 24.0       # 実印刷の非印字領域対策（約8.5mm）
FORM_MB: float   = PRINT_SAFE_BOTTOM_MARGIN_PT  # 下部要素の最下端
CORNER_R: float  = 4.0                          # 角丸半径

# ─── タイトル ─────────────────────────────────────────────────────────────────
FORM_TITLE_Y: float         = 493.0   # タイトル文字ベースライン
FORM_TITLE_UL_Y: float      = 489.0   # タイトル下線（タイトル自体は移動しない）
FORM_TITLE_SHIFT_LEFT: float = 28.35   # 1.0cm = 約28.35pt（1.5cm左移動後、0.5cm右へ戻す）
FORM_TITLE_X: float         = 205.0 - FORM_TITLE_SHIFT_LEFT   # サンプル紙伝票のタイトル中心X
FORM_TITLE_UL_EXTEND: float = 14.0    # 下線を文字幅の両端からさらに伸ばすpt数（後方互換保持）
FORM_TITLE_UL_HALF: float = 84.0     # タイトル下線の固定ハーフ幅（全伝票共通・文字幅依存なし）
# 会社情報ブロック: 表の黒ヘッダー行のすぐ上・ヘッダー枠右側に配置
COMPANY_NAME_Y: float  = 453.0   # 会社名ベースライン（フォーム本体9pt下移動済み）
COMPANY_INFO_X: float  = 448.0   # 会社名テキスト開始X（ロゴ右）
COMPANY_LOGO_X: float  = 423.0   # ロゴX（ヘッダー枠右端より右）
COMPANY_LOGO_H: float  = 28.0    # ロゴ高さ（拡大）
COMPANY_LOGO_W: float  = 23.5    # ロゴ幅（拡大）

# ─── ヘッダー枠（左部分・角丸枠）─────────────────────────────────────────────
FORM_HDR_TOP: float   = 475.0   # ヘッダー枠 上端（9pt下移動: 484→475）
FORM_HDR_MID: float   = 452.0   # 行1/行2 水平仕切り（9pt下移動: 461→452）
FORM_HDR_BOT: float   = 431.0   # ヘッダー枠 下端 ＝ テーブル上端（9pt下移動: 440→431）
FORM_HDR_LEFT: float  = 45.0    # ヘッダー枠 左端 ＝ 明細表の品名左罫線
FORM_HDR_RIGHT: float = 420.0   # ヘッダー枠 右端

# ヘッダー列境界
# Row1: コードNo | 得意先名（右端に殿） | 受注No | 仕上日
HDR_ROW1_DIVS: list[float] = [90.0, 284.0, 371.0]
# Row2: 発行日 | 納品日 | 伝票No | 取引区分 | 出荷区分 | 入力者名 | AM・PM
HDR_ROW2_DIVS: list[float] = [90.0, 145.0, 197.0, 245.0, 284.0, 371.0]
HDR_SHIAGE_LABEL_Y: float = FORM_HDR_TOP - 7.0
HDR_SHIAGE_MONTH_DAY_Y: float = FORM_HDR_MID + 5.0

# ─── 明細テーブル（全幅・角丸外枠）─────────────────────────────────────────────
FORM_DETAIL_ROWS: int    = 7
FORM_DETAIL_ROW_H: float = 26.0
FORM_TBL_HDR_BOT: float  = 416.0   # テーブルヘッダー行下端（9pt下移動: 425→416）
FORM_DETAIL_BOT: float   = FORM_TBL_HDR_BOT - FORM_DETAIL_ROWS * FORM_DETAIL_ROW_H  # 257
FORM_TOTAL_ROW_H: float  = 26.0   # 合計行高さ = 通常行と同じ
FORM_TOTAL_BOT: float    = FORM_DETAIL_BOT - FORM_TOTAL_ROW_H                        # 242

# テーブル列境界 [左端, No/品名, 品名/数量, 数量/単価, 単価/金額, 金額/摘要, 右端]
TBL_COLS: list[float] = [34.0, FORM_HDR_LEFT, 336.0, 434.0, 502.0, 568.0, 695.0]
TBL_COL_LABELS: list[str] = ["No", "品　名", "数　量", "単　価", "金　額", ""]  # 摘要列は「摘」「要」に分割して描画

# 指図書系テーブル列境界 [左端, No/品名, 品名/数量, 数量/備考, 備考/受入日, 右端]
# 備考 = 単価+金額+摘要左半分（434〜631.5）を統合、受入日 = 摘要右半分（631.5〜695）
SHIZU_TBL_COLS: list[float] = [34.0, 45.0, 336.0, 434.0, 631.5, 695.0]
SHIZU_COL_LABELS: list[str] = ["No", "品　名", "数　量", "備　考", "受入日"]
SHIZU_MAX_W_NYUKI: float = 58.5  # SHIZU_TBL_COLS[-1] - SHIZU_TBL_COLS[-2] - DATA_X_PAD(5.0)

# 指図書系: 「現」と納期行の表示位置
GEN_CIRCLE_X: float = 72.0    # 点線丸「現」中心X（コードNo列右端寄り）
NOKI_LINE_X: float  = 90.0    # 納期行テキスト開始X（コードNo列右端 = HDR_ROW1_DIVS[0]）

# 指図書系: 印枠（工場印/商品課印/配送者印）表の下・摘要/物件Noライン右端
# 表から離し、印鑑が押せる程度の小さいサイズ
STAMP_X: float    = 651.0   # 印枠 左端X（表右端付近）
STAMP_W: float    = 38.0    # 印枠 幅（印鑑程度 ≈ 13mm）
STAMP_H: float    = 30.0    # 印枠 高さ（印鑑程度 ≈ 10mm）
STAMP_GAP: float  = 10.0    # 表底辺からの空き距離

# ─── 合計行 ───────────────────────────────────────────────────────────────────
FORM_TOTAL_CELL_LEFT: float  = TBL_COLS[3]   # 単価列左
FORM_TOTAL_CELL_RIGHT: float = TBL_COLS[4]   # 単価列右

# ─── データ描画オフセット ─────────────────────────────────────────────────────
DATA_X_PAD: float       = 5.0    # セル左端からの X パディング
HDR_DATA_Y_INNER: float = 4.0    # ヘッダー行下端からの上方向オフセット（ベースライン）
DET_UPPER_OFFSET: float = 11.0   # 明細行上端からの下方向（上段ベースライン）
DET_LOWER_OFFSET: float = 19.0   # 明細行上端からの下方向（下段ベースライン）

# 明細データ描画 X（列境界 + DATA_X_PAD）
TBL_X_NAME: float  = TBL_COLS[1] + DATA_X_PAD   # 45
TBL_X_QTY: float   = TBL_COLS[2] + DATA_X_PAD   # 318
TBL_X_UNIT: float  = TBL_COLS[3] + DATA_X_PAD   # 391
TBL_X_AMT: float   = TBL_COLS[4] + DATA_X_PAD   # 479
TBL_X_NOTE: float  = TBL_COLS[5] + DATA_X_PAD   # 563

# 明細カラム最大幅
TBL_MAX_NAME: float = 267.0
TBL_MAX_QTY: float  =  88.0
TBL_MAX_UNIT: float =  58.0
TBL_MAX_AMT: float  =  57.0
TBL_MAX_NOTE: float = 108.0

# ─── 品名・数量のアライメント X ─────────────────────────────────────────────────
DET_NAME_RX: float  = TBL_COLS[2] - DATA_X_PAD   # 品名列右端（寸法右揃え用）
DET_QTY_RX: float   = TBL_COLS[3] - DATA_X_PAD   # 数量列右端（数量右揃え用）
FS_DIM_LARGE: float = 9.0                         # 寸法行フォントサイズ（大きめ）
# 品名列のW/H（寸法）表示だけを左へ寄せる量。商品名称1段目は移動しない。
# 1.0cm = 約28.35pt。寸法2段目の右揃え基準Xから差し引いて使う。
DIM_SHIFT_LEFT: float = 28.35

# ─── 摘要列分割 ──────────────────────────────────────────────────────────────
TBL_NOTE_MID_X: float   = (TBL_COLS[5] + TBL_COLS[6]) / 2   # 摘要列中央X
TBL_NOTE_MID_PAD: float = 3.0                                  # 中央からの余白

# 印刷時の下部見切れ対策: 明細表は固定したまま、「摘要」ライン以降の下部要素
# だけを約2mm（reportlab座標で約5.7pt）上へ引き上げる。表・ヘッダーは動かさない。
LOWER_SHIFT_UP: float = 5.7

# ─── 摘要 / 物件No 行（テーブル下、狭幅）────────────────────────────────────
# 明細行底辺(FORM_DETAIL_BOT)からの余白。LOWER_SHIFT_UP 分だけ狭めることで、
# 表位置を固定したまま摘要から下の要素全体を上へ移動させる。
FORM_SUM_GAP: float  = 14.0 - LOWER_SHIFT_UP

FORM_SUM_TOP: float  = FORM_DETAIL_BOT - FORM_SUM_GAP
FORM_SUM_H: float    = 18.0                              # 摘要欄の高さ
FORM_SUM_BOT: float  = FORM_SUM_TOP - FORM_SUM_H
FORM_BKNO_TOP: float = FORM_SUM_BOT
FORM_BKNO_H: float   = 14.0                              # 物件No欄の高さ
FORM_BKNO_BOT: float = FORM_BKNO_TOP - FORM_BKNO_H

# 摘要/物件No欄の右端（全幅より短く、ヘッダー右端に揃える）
FORM_SUM_RIGHT: float    = TBL_COLS[2]      # 品名列右罫線まで
FORM_SUBROW_LBL_W: float = 35.0             # ラベル列幅（縦仕切りまで）
SUM_STAFF_X: float       = FORM_SUM_RIGHT + 8.0   # 担当者表示X（摘要下線右側）

# ─── 消費税文言 ───────────────────────────────────────────────────────────────
TAX_NOTICE: str  = "（本伝票には消費税は含まれておりません。）"
TAX_Y: float     = FORM_BKNO_BOT - 11.0   # 物件No下線との重なり回避（フォント7.5pt+余白）

# ─── 下部チェック欄 ───────────────────────────────────────────────────────────
FORM_LWR_TOP: float   = TAX_Y - 4.0        # 下部チェック欄 上端
# 下部チェック欄 下端も LOWER_SHIFT_UP 分だけ上げ、下部余白を増やして実印刷の見切れを防ぐ。
FORM_LWR_BOT: float   = FORM_MB + LOWER_SHIFT_UP
FORM_LWR_LEFT: float  = 42.0               # 上部表より少し内側
FORM_LWR_RIGHT: float = 688.0              # 上部表より少し内側

# チェック縦列（左側・細幅）
FORM_CHK_RIGHT: float = FORM_LWR_LEFT + 67.0  # チェック列 右端（日本語6文字+チェック欄）
FORM_RGHT_LEFT: float = FORM_CHK_RIGHT + 4.0  # 101 右側大枠 左端

# 切断仕上日 小枠（右側大枠の右上）
FORM_CUT_LEFT: float = FORM_LWR_RIGHT - 162.0  # 小枠 左端
FORM_CUT_TOP: float  = FORM_LWR_TOP        # 181
FORM_CUT_BOT: float  = FORM_LWR_TOP - 38.0  # 143   小枠の高さ38pt

# 加工チェックラベル（縦リスト）
PROC_LABELS: list[str] = [
    "エッジング", "広幅", "工場切", "手加工", "DM-10",
    "引手", "マルチ", "洗浄", "BOB", "印刷",
    "", "", "",
]

# ══════════════════════════════════════════════════════════════════════════════
# オーバーレイ方式 (02-08) の旧座標定数（後方互換）
# ══════════════════════════════════════════════════════════════════════════════

HEADER_ROW1_Y: float = 480.0
HEADER_ROW2_Y: float = 460.0

HDR1_CODE_NO_X: float    = 30.0
HDR1_CUSTOMER_X: float   = 82.0
HDR1_CUSTOMER_MAX: float = 148.0
HDR1_ORDER_NO_X: float   = 300.0
HDR1_SHIAGE_X: float     = 365.0

HDR2_ISSUE_DATE_X: float  = 29.0
HDR2_DELIVERY_X: float    = 82.0
HDR2_VOUCHER_NO_X: float  = 133.0
HDR2_TRADE_TYPE_X: float  = 193.0
HDR2_SHIP_TYPE_X: float   = 242.0
HDR2_OPERATOR_X: float    = 300.0
HDR2_OPERATOR_MAX: float  = 58.0

DETAIL_ROW1_TOP: float     = 435.0
DETAIL_ROW_H: float        = 24.0
DETAIL_UPPER_OFFSET: float = 6.0
DETAIL_LOWER_OFFSET: float = 13.0

COL_NAME_X: float   = 49.0
COL_QTY_X: float    = 321.0
COL_UNIT_X: float   = 393.0
COL_AMOUNT_X: float = 482.0
COL_NOTE_X: float   = 566.0

MAX_W_NAME: float   = 265.0
MAX_W_QTY: float    =  66.0
MAX_W_UNIT: float   =  82.0
MAX_W_AMOUNT: float =  77.0
MAX_W_NOTE: float   = 136.0

FS_HEADER: float = 8.0
FS_DETAIL: float = 8.0
FS_DIMS: float   = 7.0
FS_NOTE: float   = 7.5

# ── ダミーデータ ───────────────────────────────────────────────────────────────

DUMMY_DATA: dict[str, Any] = {
    "code_no":           "40630",
    "customer_name":     "株式会社たくみ硝子店",
    "order_no":          "1405113",
    "shiage_date":       "06/19",
    "issue_date":        "26/06/04",
    "delivery_date":     "26/06/19",
    "voucher_no":        "Z737704",
    "trade_type":        "売上",
    "ship_type":         "販PM",
    "operator":          "竹内（典）",
    "sales_rep":         "船橋",
    "construction_rep":  "",
    "details": [
        {
            "name":       "MT5 四方 磨き",
            "dims":       "（1303 * 1061 ミリ）",
            "qty_spec":   "",
            "qty":        "1枚",
            "unit_price": "1.382㎡",
            "amount":     "1.382㎡",
            "note_lines": ["1,580 加", "7,594 倉庫ま"],
        },
        {
            "name":       "ミラーガード 全周塗",
            "dims":       "（1303 * 1061 ミリ）",
            "qty_spec":   "",
            "qty":        "1*",
            "unit_price": "1.382㎡",
            "amount":     "1.382㎡",
            "note_lines": ["0 / 378 東大阪"],
        },
    ],
}
