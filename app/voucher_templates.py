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
# ─── 出荷区分セル拡張のための右シフト量 ──────────────────────────────────────
# 上部ヘッダーの「出荷区分」セルを広げるため、入力者名境界（＝得意先名右枠線・
# 受注No左枠線・入力者名左枠線の整列境界 HDR_OPERATOR_X）から右側の要素を、
# まとめて同じ量だけ右へずらす（要件1〜4）。これにより、
#   ・出荷区分セル幅 = HDR_SHIPPING_SHIFT 分だけ拡張（1.3倍データ・全角ＰＭも収まる）
#   ・入力者名 / 仕上日 / AM・PM の各セル幅は維持（境界ごと右へ平行移動）
#   ・受注No左枠線 = 入力者名左枠線 の整列も維持
#   ・会社ロゴ・会社名はヘッダー枠右端と重ならないよう同量右へ移動
# を同時に満たす。用紙右余白には十分な余裕があるため右端からはみ出さない。
HDR_SHIPPING_SHIFT: float = 14.0

# ─── 取引区分セル拡張のための追加右シフト量 ──────────────────────────────────
# 「取引区分」データを出荷区分と同じ1.3倍にすると、現行28ptセルでは余裕が無いため、
# 取引区分/出荷区分 境界(HDR_TRADE_RIGHT)から右側を更に同量右へずらして取引区分セルを
# 広げる。出荷区分セル幅は HDR_OPERATOR_X も同量右へずらすことで維持する。
HDR_TRADE_SHIFT: float = 8.0
# 入力者名境界(HDR_OPERATOR_X)より右側に掛かる総右移動量（出荷区分拡張＋取引区分拡張）。
HDR_RIGHT_SHIFT: float = HDR_SHIPPING_SHIFT + HDR_TRADE_SHIFT

# 会社情報ブロック: 表の黒ヘッダー行のすぐ上・ヘッダー枠右側に配置
COMPANY_NAME_Y: float  = 453.0   # 会社名ベースライン（フォーム本体9pt下移動済み）
COMPANY_INFO_X: float  = 448.0 + HDR_RIGHT_SHIFT   # 会社名テキスト開始X（ロゴ右・右シフト）
COMPANY_LOGO_X: float  = 423.0 + HDR_RIGHT_SHIFT   # ロゴX（ヘッダー枠右端より右・右シフト）
COMPANY_LOGO_H: float  = 28.0    # ロゴ高さ（拡大）
COMPANY_LOGO_W: float  = 23.5    # ロゴ幅（拡大）

# ─── ヘッダー枠（左部分・角丸枠）─────────────────────────────────────────────
FORM_HDR_TOP: float   = 475.0   # ヘッダー枠 上端（9pt下移動: 484→475）
FORM_HDR_MID: float   = 452.0   # 行1/行2 水平仕切り（9pt下移動: 461→452）
FORM_HDR_BOT: float   = 431.0   # ヘッダー枠 下端 ＝ テーブル上端（9pt下移動: 440→431）
FORM_HDR_LEFT: float  = 45.0    # ヘッダー枠 左端 ＝ 明細表の品名左罫線
# ヘッダー枠右端も取引区分・出荷区分セル拡張分だけ右へ広げる（仕上日/AM・PMセルごと右移動）。
FORM_HDR_RIGHT: float = 420.0 + HDR_RIGHT_SHIFT   # ヘッダー枠 右端（右シフト）

# ヘッダー列境界
# Row2: 発行日 | 納品日 | 伝票No | 取引区分 | 出荷区分 | 入力者名 | AM・PM
# ヘッダーデータを1.3倍化したため（要件1）、最大幅の日付（例 26/12/31 ≒ 56.9pt）が
# 収まるよう発行日・納品日セルを各63ptに広げ、両者を同一幅にする（要件2）。
# 発行日と納品日の境界。発行日セル幅 = 納品日セル幅 = 63pt にするため、左端
# FORM_HDR_LEFT(45)から63pt右の 108 に置く。HDR_ROW1_DIVS[0] が本値を参照するため、
# コードNo列の右枠線と発行日列の右枠線も自動的に同一Xで揃う（要件2）。
HDR_DELIVERY_X: float = 108.0
# 納品日セルも幅63pt（108→171）。1.3倍の最大幅日付（≒56.9pt）が収まる。
# 後続の境界（伝票No以降）も右へずらし、各データ1.3倍が枠内に収まる幅を確保する。
# 入力者名セルは余裕があるため、その幅から後続セルの拡張分を吸収する。
HDR_DELIVERY_RIGHT: float = 171.0   # 納品日 / 伝票No 境界（発行日と同幅63pt）
HDR_VOUCHER_RIGHT:  float = 231.0   # 伝票No / 取引区分 境界（伝票No幅60pt）
# 取引区分 / 出荷区分 境界。取引区分データを1.3倍化したため HDR_TRADE_SHIFT 分右へずらし、
# 取引区分セル幅 = 28 + HDR_TRADE_SHIFT pt に拡張する（売上/加工/現金 等が収まる）。
HDR_TRADE_RIGHT:    float = 259.0 + HDR_TRADE_SHIFT   # 取引区分 / 出荷区分 境界（右シフト）
# 出荷区分 / 入力者名 境界＝入力者名セル左端。HDR_RIGHT_SHIFT 分右へずらすことで
# 出荷区分セル幅 = 34 + HDR_SHIPPING_SHIFT pt を維持（取引区分拡張分は HDR_TRADE_RIGHT と
# 一緒に右へ平行移動する）。1.3倍の出荷区分データ（店PM/販PM/直PM/倉PM、全角ＰＭ含む）が
# 枠内に収まる。HDR_ORDER_NO_X が本値を参照するため、得意先名右枠線・受注No左枠線・
# 入力者名左枠線の整列も保たれる。
HDR_OPERATOR_X:     float = 293.0 + HDR_RIGHT_SHIFT   # 出荷区分 / 入力者名 境界（右シフト）
# 入力者名 / AM・PM 境界。基本は右シフトに連動するが、「AM・PM」文字を1.2倍化したため、
# 文字と丸印がセル枠（AM・PM セル＝仕上日セル）に収まるよう左端を HDR_AMPM_WIDEN 分だけ
# 左へ広げる（要件1/2）。広げた分は余裕のある入力者名・受注No セルから分ける。
HDR_AMPM_WIDEN:     float = 10.0   # AM・PM/仕上日セルの左方向拡張量（AM・PM 1.2倍化対応）
HDR_AMPM_X:         float = 371.0 + HDR_RIGHT_SHIFT - HDR_AMPM_WIDEN   # 入力者名 / AM・PM 境界
HDR_ROW2_DIVS: list[float] = [
    HDR_DELIVERY_X, HDR_DELIVERY_RIGHT, HDR_VOUCHER_RIGHT,
    HDR_TRADE_RIGHT, HDR_OPERATOR_X, HDR_AMPM_X,
]
# Row1: コードNo | 得意先名（右端に殿） | 受注No | 仕上日
# コードNo列の右端境界を発行日列の右端(HDR_DELIVERY_X=108)に合わせ、コードNo列幅を
# 発行日列幅(63pt)と同一にする（要件2）。得意先名列はその右から開始する。
# 受注No セルの左枠線を下段「入力者名」セルの左枠線(HDR_OPERATOR_X)と縦に揃える。
# 得意先名列はその分だけ右に広がる。
HDR_ORDER_NO_X: float = HDR_OPERATOR_X
HDR_ROW1_DIVS: list[float] = [HDR_DELIVERY_X, HDR_ORDER_NO_X, HDR_AMPM_X]
HDR_SHIAGE_LABEL_Y: float = FORM_HDR_TOP - 7.0
# 仕上日の月・日（数値とラベル）のベースライン。セル下線(FORM_HDR_MID)ギリギリまで
# 下寄せする（要件3）。5.0→3.0。数値・ラベルは同一Yなので相互の重なりは生じない。
HDR_SHIAGE_MONTH_DAY_Y: float = FORM_HDR_MID + 3.0
# 仕上日サブセル(HDR_ROW1_DIVS[-1]=371〜FORM_HDR_RIGHT=420)の月日レイアウト（要件3）。
# 「日」ラベルは右寄せ、「月」ラベルは中央寄せ。月・日の数値データは各ラベルの左側に
# 大きめフォントで右寄せ配置し、ラベルと重ならないようにする。
HDR_SHIAGE_DATA_FS: float        = 9.0     # 仕上日データ（月/日の数値）フォント（大きめ）
HDR_SHIAGE_LABEL_FS: float       = 7.0     # 「月」「日」ラベルのフォント
# 仕上日データを1.3倍（要件1）にしても2桁の月日がラベルと重ならないよう、
# データ右端・ラベル位置を僅かに調整して各要素の隙間を確保している。
# 仕上日サブセル(HDR_ROW1_DIVS[-1]〜FORM_HDR_RIGHT)ごと HDR_RIGHT_SHIFT 右へ移動
# したため、月日のラベル・データのX基準も同量右へずらす（セル内の相対配置は不変）。
HDR_SHIAGE_DAY_LABEL_RX: float   = 419.0 + HDR_RIGHT_SHIFT   # 「日」右端（右寄せ・セル右端寄り）
HDR_SHIAGE_MONTH_LABEL_CX: float = 392.0 + HDR_RIGHT_SHIFT   # 「月」中心（中央寄せ・セル中央付近）
HDR_SHIAGE_MONTH_DATA_RX: float  = 388.0 + HDR_RIGHT_SHIFT   # 月データ右端（「月」ラベルの左）
HDR_SHIAGE_DAY_DATA_RX: float    = 411.5 + HDR_RIGHT_SHIFT   # 日データ右端（「日」ラベルの左）

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
NOKI_LINE_X: float  = HDR_DELIVERY_X   # 納期行テキスト開始X（コードNo列右端 = HDR_ROW1_DIVS[0]=108）

# 指図書系: 印枠（工場印/商品課印/配送者印）表の下・摘要/物件Noライン右端
# 表から離し、印鑑が押せる程度のサイズ。
# 印枠サイズを1.3倍に拡大し（要件4）、全体を1cm（≒28.35pt）左へ移動する（要件5）。
STAMP_BOX_SCALE: float  = 1.3        # 印枠の拡大率（幅・高さ）
STAMP_BOX_SHIFT_X: float = -28.35    # 印枠の左移動量（1cm ≒ 28.35pt）
_STAMP_W_BASE: float = 38.0    # 旧 印枠 幅（印鑑程度 ≈ 13mm）
_STAMP_H_BASE: float = 30.0    # 旧 印枠 高さ（印鑑程度 ≈ 10mm）
_STAMP_X_BASE: float = 651.0   # 旧 印枠 左端X（表右端付近）
STAMP_W: float    = _STAMP_W_BASE * STAMP_BOX_SCALE   # 印枠 幅（1.3倍）
STAMP_H: float    = _STAMP_H_BASE * STAMP_BOX_SCALE   # 印枠 高さ（1.3倍）
STAMP_X: float    = _STAMP_X_BASE + STAMP_BOX_SHIFT_X # 印枠 左端X（1cm左）
STAMP_GAP: float  = 10.0    # 表底辺からの空き距離

# 受領書(08) 検印/配送者印 枠（中央右側）。同じく1.3倍・1cm左（要件4/5）。
_DELIV_STAMP_W_BASE: float = 42.0    # 旧 検印/配送者印 枠 幅
_DELIV_STAMP_H_BASE: float = 32.0    # 旧 検印/配送者印 枠 高さ
DELIV_STAMP_W: float   = _DELIV_STAMP_W_BASE * STAMP_BOX_SCALE   # 検印/配送者印 枠 幅（1.3倍）
DELIV_STAMP_H: float   = _DELIV_STAMP_H_BASE * STAMP_BOX_SCALE   # 検印/配送者印 枠 高さ（1.3倍）
DELIV_STAMP_GAP: float = 8.0        # 枠間の空き距離

# ─── 合計行 ───────────────────────────────────────────────────────────────────
FORM_TOTAL_CELL_LEFT: float  = TBL_COLS[3]   # 単価列左
FORM_TOTAL_CELL_RIGHT: float = TBL_COLS[4]   # 単価列右

# ─── データ描画オフセット ─────────────────────────────────────────────────────
DATA_X_PAD: float       = 5.0    # セル左端からの X パディング
# ヘッダーデータのベースライン位置。セル下線ギリギリまで文字を下げる（下寄せ・要件3）。
# 行下端の罫線から本値だけ上にベースラインを置く。値を小さくするほど下線に近づく。
HDR_DATA_Y_INNER: float = 1.5    # ヘッダー行下端からの上方向オフセット（ベースライン）
DET_UPPER_OFFSET: float = 11.0   # 明細行上端からの下方向（上段ベースライン）
# 下段ベースラインを少しずつ下げて1段目とのバランスを取る。
# 19→21（要件2）→さらに +1mm(約2.83pt) → 23.83（今回の要件3）。
DET_LOWER_OFFSET: float = 23.83  # 明細行上端からの下方向（下段ベースライン）
# 数量列の2段目（数量データ「1枚」「1*」「〇枚」「〇＊」等）はセルの高さ方向
# 中央あたりへ寄せる（要件2）。1段目のコード（510中/510WC/510WC/A 等）は上段(yu)に
# 残し、数量データだけを中央へ移動する。行高 FORM_DETAIL_ROW_H(26pt) の幾何中央は
# 13pt。フォント高を見込みベースラインを16ptに置くと文字の視覚中心が概ねセル中央に
# 来る。枠線(26pt)内に収まり、上段コードとも重ならず、全行で位置が揃う。
DET_QTY_LOWER_OFFSET: float = 16.0

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
# 品名列のW/H（寸法）表示の左寄せ量。商品名称1段目は移動しない。
# 1.0cm = 約28.35pt。寸法2段目の右揃え基準Xから差し引いて使う。
# 今回の要件2でWH表示を1cm右へ移動するため 28.35→0.0（＝品名列右端基準）にする。
DIM_SHIFT_LEFT: float = 0.0

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

# ─── お客様注文No 表示 ─────────────────────────────────────────────────────────
# 従来この位置に「（本伝票には消費税は含まれておりません。）」を7.5ptで表示していた。
# 固定文言は全廃し、代わりにOLAPの「客先注文No_10桁」を従来比1.2倍サイズで表示する。
CUSTOMER_ORDER_NO_LABEL: str       = "お客様注文No. "
CUSTOMER_ORDER_NO_BASE_FONT_SIZE: float = 7.5            # 旧消費税文言サイズ（基準）
CUSTOMER_ORDER_NO_FONT_SIZE: float = CUSTOMER_ORDER_NO_BASE_FONT_SIZE * 1.2  # 1.2倍
TAX_Y: float     = FORM_BKNO_BOT - 11.0   # 物件No下線との重なり回避（摘要/物件No下の表示位置）

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
    "フィルム貼", "Rとり", "",
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
            "sales_unit_price": "1580",
            "purchase_unit_price": "7594",
            "ordered_quantity": "1",
        },
        {
            "name":       "ミラーガード 全周塗",
            "dims":       "（1303 * 1061 ミリ）",
            "qty_spec":   "",
            "qty":        "1*",
            "unit_price": "1.382㎡",
            "amount":     "1.382㎡",
            "note_lines": ["0 / 378 東大阪"],
            "sales_unit_price": "0",
            "purchase_unit_price": "378",
            "ordered_quantity": "1",
        },
    ],
}
