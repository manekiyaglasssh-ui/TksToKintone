"""登録用の ㎡ / 総㎡ を算出する共通モジュール。

登録前確認画面の「CSV作成」と kintone 登録は、必ず本モジュールの算出結果を使う
（別々に計算しない）。算出ロジックは伝票作成処理（``app.voucher_data_mapper``）の
``resolve_unit_and_amount_values`` の OP区分による条件分岐をそのまま流用する。

OP区分が取得できない場合は、旧来の固定値 1 を入れず空欄を返す（要件7）。
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.voucher_data_mapper import (
    compute_op_calculated_fields,
    resolve_unit_and_amount_values,
)

# 商品名称が "*" の明細行は伝票作成処理と同じく ㎡/総㎡ を空欄にする。
STAR_PRODUCT_NAME = "*"

# 登録行（日本語表示名キー）→ 伝票内部エイリアスの対応。
# 伝票側の resolve_unit_and_amount_values / compute_op_calculated_fields は
# 内部エイリアス（op_type など）で動くため、登録行をエイリアス表現へ変換して渡す。
_REGISTRATION_TO_ALIAS = {
    "OP区分": "op_type",
    "数量単位名称": "quantity_unit_name",
    "㎡": "stat_quantity",            # ㎡列の元値はOLAP統計数量
    "総㎡": "ordered_stat_quantity",  # 総㎡列の元値はOLAP受注統計数量
    "W寸法": "width",
    "H寸法": "height",
    "受注数量": "ordered_quantity",
    "00時ケース・ロット平米": "case_lot_square",
    "02時平米": "op02_square",
    "02時総平米": "op02_total_square",
}


def _alias_row(row: dict[str, str]) -> dict[str, str]:
    alias: dict[str, str] = {}
    for jp_key, alias_key in _REGISTRATION_TO_ALIAS.items():
        value = row.get(jp_key)
        if value not in (None, ""):
            alias[alias_key] = str(value)
    return alias


def _strip_decimal(text: str) -> str:
    """数値文字列を末尾0を除いた表記へ正規化する。数値化できない場合は元の値を返す。"""
    cleaned = str(text or "").replace(",", "").strip()
    if not cleaned:
        return ""
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return cleaned
    normalized = number.normalize()
    formatted = format(normalized, "f")
    return "0" if formatted == "-0" else formatted


def format_area_3(value: str | None) -> str:
    """㎡ / 総㎡ を小数第3位までで四捨五入した文字列にする。

    例: "1.382483" -> "1.382", "2" -> "2.000"。空欄・数値化できない値は "" を返す。
    """
    if value is None:
        return ""
    text = str(value).replace(",", "").strip()
    if not text:
        return ""
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return ""
    return str(dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def calculate_area_values(
    row: dict[str, str], *, logger: logging.Logger | None = None
) -> tuple[str, str]:
    """登録用の (㎡, 総㎡) を返す。

    伝票作成処理と同じ OP区分の条件分岐で算出する:
      - OP区分 02: W×H から算出した 02時平米 / 02時総平米
      - OP区分 01: 統計数量 / 受注統計数量（0 のときは 02時平米 / 02時総平米へフォールバック）
      - OP区分 00 ほか: 数量単位名称に応じた統計数量 / 受注統計数量

    OP区分が取得できない場合は固定値 1 を入れず ("", "") を返す（要件7）。
    商品名称が "*" の明細行も伝票仕様どおり ("", "") を返す。
    """
    if str(row.get("商品名称") or "").strip() == STAR_PRODUCT_NAME:
        return "", ""
    op_type = str(row.get("OP区分") or "").strip()
    if not op_type:
        return "", ""
    alias = _alias_row(row)
    compute_op_calculated_fields(alias, logger=logger)
    m2, total_m2 = resolve_unit_and_amount_values(alias, logger=logger)
    return _strip_decimal(m2), _strip_decimal(total_m2)


def apply_area_values(
    row: dict[str, str], *, logger: logging.Logger | None = None
) -> dict[str, str]:
    """row の ㎡ / 総㎡ を OP区分に応じた値で上書きして返す（同一 row を返す）。

    算出後に小数第3位までで四捨五入する。CSV出力・kintone登録はこの丸め済み値を共通で使う。
    """
    m2, total_m2 = calculate_area_values(row, logger=logger)
    row["㎡"] = format_area_3(m2)
    row["総㎡"] = format_area_3(total_m2)
    return row
