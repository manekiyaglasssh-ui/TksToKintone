from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
from pathlib import Path
from typing import Any

from app.voucher_templates import FORM_DETAIL_ROWS


_DISPLAY_NAME_ALIASES = {
    "営業所名称": "office_name",
    "請求書電話番号": "office_tel",
    "請求書FAX番号": "office_fax",
    "得意先コード": "customer_code",
    "得意先名称": "customer_name",
    "受注No": "order_no",
    "受注行No": "order_line_no",
    "納品日": "delivery_date",
    "納品書No": "voucher_no",
    "伝票区分": "voucher_type",
    "売上伝票区分名称": "trade_type",
    "出荷区分": "ship_type_code",
    "出荷区分名称": "ship_type",
    "入力者コード": "operator_code",
    "営業担当": "sales_rep",
    "営業担当者名称": "sales_rep",
    "工事担当": "construction_rep",
    "工事担当者名称": "construction_rep",
    "商品名称": "product_name",
    "商品注釈": "product_note",
    "窓記号": "window_symbol",
    "受注数量": "ordered_quantity",
    "数量単位名称": "quantity_unit_name",
    "統計数量": "stat_quantity",
    "受注統計数量": "ordered_stat_quantity",
    "売上単価": "sales_unit_price",
    "仕入単価": "purchase_unit_price",
    "明細指示区分": "detail_instruction_type",
    "納品書発行略称": "delivery_short_name",
    "加工仕上日": "finish_date",
    "納入先住所1": "delivery_address1",
    "受注見出摘要": "order_summary",
    "客先注文No_10桁": "customer_order_no_10",
    "物件No": "property_no",
    "物件名称1": "property_name",
    "営業担当者コード": "sales_rep_code",
    "工事担当者コード": "construction_rep_code",
    "W寸法": "width",
    "H寸法": "height",
    "売上計上月度": "sales_month",
    "OP区分": "op_type",
    "商品コード": "product_code",
    "00時ケース・ロット平米": "case_lot_square",
    "02時平米": "op02_square",
    "02時総平米": "op02_total_square",
}

_DISPLAY_NO_ALIASES = {
    "15": "operator",
    "34": "sales_rep",
    "36": "construction_rep",
}

_FALLBACK_DISPLAY_KEYS = {
    "office_name": ("1",),
    "office_tel": ("2",),
    "office_fax": ("3",),
    "customer_code": ("4",),
    "customer_name": ("5",),
    "order_no": ("6",),
    "order_line_no": ("7",),
    "delivery_date": ("8",),
    "voucher_no": ("9",),
    "trade_type": ("11",),
    "ship_type": ("13",),
    "operator": ("15",),
    "product_name": ("16",),
    "product_note": ("17",),
    "window_symbol": ("18",),
    "ordered_quantity": ("19",),
    "quantity_unit_name": ("21", "20"),
    "stat_quantity": ("22", "21"),
    "ordered_stat_quantity": ("23", "22"),
    "sales_unit_price": ("24", "23"),
    "purchase_unit_price": ("25", "24"),
    "detail_instruction_type": ("26", "25"),
    "delivery_short_name": ("27", "26"),
    "finish_date": ("28", "27"),
    "delivery_address1": ("29",),
    "order_summary": ("30", "29"),
    "property_no": ("31", "30"),
    "property_name": ("32", "31"),
    # 客先注文No_10桁（OLAP表示No=45）。OP列(36-44)が無いレイアウトでは
    # _is_current_olap_layout が False になり alias 解決をすり抜けるため、
    # 表示No=45 を明示的なフォールバックキーとして常に解決できるようにする。
    "customer_order_no_10": ("45",),
    "sales_rep": ("34", "33"),
    "construction_rep": ("36", "35"),
    "op_type": ("40",),
    "case_lot_square": ("42",),
    "op02_square": ("43",),
    "op02_total_square": ("44",),
}

_ALIAS_DISPLAY_NAMES = {
    alias: display_name for display_name, alias in _DISPLAY_NAME_ALIASES.items()
}
_ALIAS_DISPLAY_NAMES.update(
    {
        "operator": "担当者名称",
        "sales_rep": "営業担当",
        "construction_rep": "工事担当",
    }
)

_OLD_LAYOUT_KEYS = {
    "quantity_unit_name": ("20",),
    "stat_quantity": ("21",),
    "ordered_stat_quantity": ("22",),
    "sales_unit_price": ("23",),
    "purchase_unit_price": ("24",),
    "detail_instruction_type": ("25",),
    "delivery_short_name": ("26",),
    "finish_date": ("27",),
    "delivery_address1": ("28",),
    "order_summary": ("29",),
    "property_no": ("30",),
    "property_name": ("31",),
    "sales_rep": ("33",),
    "construction_rep": ("35",),
}

_R1_DISPLAY_ALIAS_KEYS: dict[str, tuple[str, str]] | None = None

_RESULT_STATUS_KEYS = {
    "DesignGroup",
    "Index",
    "MessageName",
    "MessageParams",
    "OutputLog",
    "PropertyName",
    "RData",
}


def parse_denpyo_numbers(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,\n\r]+", text):
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        values.append(stripped)
    return values


def is_missing_voucher_no(value: object) -> bool:
    """伝票Noが未発行相当か判定する。空/None/ゼロのみは未発行扱い。"""
    text = str(value or "").strip()
    if not text:
        return True
    return text.isdigit() and int(text) == 0


def extract_r1_rows(response_data: object, *, logger: logging.Logger | None = None) -> list[dict[str, str]]:
    raw_rows = _raw_r1_rows(response_data)

    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or _is_result_status_row(raw):
            continue
        row = _with_display_name_aliases(
            {str(key): "" if value is None else str(value) for key, value in raw.items()},
            logger=logger,
        )
        _compute_op_calculated_fields(row, logger=logger)
        rows.append(row)
    return rows


def count_r1_rows(response_data: object) -> int:
    return len([row for row in _raw_r1_rows(response_data) if isinstance(row, dict) and not _is_result_status_row(row)])


def first_r1_row_keys(response_data: object) -> list[str]:
    for row in _raw_r1_rows(response_data):
        if isinstance(row, dict) and not _is_result_status_row(row):
            return sorted((str(row_key) for row_key in row.keys()), key=_numeric_sort_key)
    return []


def response_top_keys(response_data: object) -> list[str]:
    if isinstance(response_data, dict):
        return [str(key) for key in response_data.keys()]
    return []


def response_data_keys(response_data: object) -> list[str]:
    if isinstance(response_data, dict):
        value = response_data.get("ResponseData")
        if isinstance(value, dict):
            return [str(key) for key in value.keys()]
    return []


def r1_list_type_name(response_data: object) -> str:
    r1_list = _r1_list(response_data)
    return type(r1_list).__name__ if r1_list is not None else "None"


def has_result_status_row(response_data: object) -> bool:
    return any(isinstance(row, dict) and _is_result_status_row(row) for row in _raw_r1_rows(response_data, allow_direct_dict=True))


def display_mapping_summary() -> dict[str, str]:
    return {
        alias: f"{display_name}:{key}"
        for alias, (display_name, key) in sorted(_r1_display_alias_keys().items())
    }


def build_voucher_pages(rows: list[dict[str, str]], *, today: date | None = None) -> list[dict[str, Any]]:
    if not rows:
        return []
    today = today or date.today()
    sorted_rows = sorted(rows, key=lambda row: (_s(row, "9"), _s(row, "6"), _int(row.get("7"))))
    pages: list[dict[str, Any]] = []
    for _, voucher_rows in groupby(sorted_rows, key=lambda row: (_s(row, "9"), _s(row, "6"))):
        for group in _chunks(list(voucher_rows), FORM_DETAIL_ROWS):
            pages.append(_build_page(group, today))
    return pages


def format_date_yy_mm_dd(value: str) -> str:
    text = (value or "").strip()
    if not text or text in {"0000/00/00", "0000-00-00"}:
        return ""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%y/%m/%d")
        except ValueError:
            continue
    compact = re.sub(r"\D", "", text)
    if len(compact) == 8:
        return f"{compact[2:4]}/{compact[4:6]}/{compact[6:8]}"
    return text


def format_month_day(value: str) -> str:
    formatted = format_date_yy_mm_dd(value)
    return formatted[3:] if len(formatted) == 8 else formatted


def format_number(value: str, *, suffix: str = "", force_int: bool = False) -> str:
    dec = _decimal(value)
    if dec is None:
        return (value or "").strip()
    if force_int or dec == dec.to_integral_value():
        text = f"{int(dec):,}"
    else:
        text = f"{dec.normalize():,f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def build_qr_code_image(order_no: str) -> io.BytesIO:
    buf = io.BytesIO()
    try:
        import qrcode
        img = qrcode.make(order_no or "")
        img.save(buf, format="PNG")
    except ModuleNotFoundError:
        from PIL import Image, ImageDraw

        text = order_no or ""
        img = Image.new("RGB", (90, 90), "white")
        draw = ImageDraw.Draw(img)
        cell = 5
        seed = sum((index + 1) * ord(ch) for index, ch in enumerate(text))
        for y in range(18):
            for x in range(18):
                finder = (x < 5 and y < 5) or (x >= 13 and y < 5) or (x < 5 and y >= 13)
                bit = finder or ((seed + x * 17 + y * 31 + x * y) % 7 in {0, 2, 5})
                if bit:
                    draw.rectangle((x * cell, y * cell, x * cell + cell - 1, y * cell + cell - 1), fill="black")
        img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _build_page(rows: list[dict[str, str]], today: date) -> dict[str, Any]:
    first = rows[0]
    details = [_detail_row(row) for row in rows]
    non_star_rows = [row for row in rows if _v(row, "product_name") != "*"]
    upper_total = sum((_decimal(_v(row, "sales_unit_price")) or Decimal("0")) for row in non_star_rows)
    lower_total = sum((_decimal(_v(row, "purchase_unit_price")) or Decimal("0")) for row in non_star_rows)
    summary_line1 = _blank_if_dash(_s(first, "delivery_address1"))
    summary_line2 = _s(first, "order_summary")
    return {
        "office_name": _v(first, "office_name"),
        "office_tel": _v(first, "office_tel"),
        "office_fax": _v(first, "office_fax"),
        "code_no": _v(first, "customer_code"),
        "customer_name": _v(first, "customer_name"),
        "order_no": _v(first, "order_no"),
        "customer_order_no_10": _v(first, "customer_order_no_10"),
        "issue_date": today.strftime("%y/%m/%d"),
        "delivery_date": format_date_yy_mm_dd(_v(first, "delivery_date")),
        "voucher_no": _v(first, "voucher_no"),
        "delivery_no": _v(first, "voucher_no"),
        "trade_type": _v(first, "trade_type"),
        "slip_type_name": _v(first, "trade_type"),
        "ship_type": _v(first, "ship_type"),
        "shipping_type_name": _v(first, "ship_type"),
        "operator": _v(first, "operator"),
        "operator_name": _v(first, "operator"),
        "sales_rep": _v(first, "sales_rep"),
        "construction_rep": _v(first, "construction_rep"),
        "details": details,
        "summary_line1": summary_line1,
        "summary_line2": summary_line2,
        "summary_lines": [summary_line1, summary_line2],
        "property_lines": [" ".join(
            part for part in (_s(first, "property_no"), _s(first, "property_name")) if part
        )],
        "total_note_upper": format_number(str(upper_total), force_int=True),
        "total_note_lower": format_number(str(lower_total), force_int=True),
        "qr_order_no": _v(first, "order_no"),
        # 取引区分（移動伝票=8 のPDF表示制御用）。OLAP取得時に得意先コードから付与される。
        "transaction_type": _s(first, "transaction_type"),
    }


def _detail_row(row: dict[str, str]) -> dict[str, Any]:
    is_star = _v(row, "product_name") == "*"
    qty = "" if is_star else format_number(_v(row, "ordered_quantity"), force_int=True)
    unit = "" if is_star else _v(row, "quantity_unit_name")
    if is_star:
        unit_price_display = ""
        amount_display = ""
    else:
        raw_unit_price, raw_amount = resolve_unit_and_amount_values(row)
        unit_price_display = _format_unit_display(raw_unit_price)
        amount_display = _format_unit_display(raw_amount)
    finish = format_month_day(_v(row, "finish_date"))
    upper_suffix = "加" if _v(row, "detail_instruction_type") == "2" else ""
    upper_note = " ".join(part for part in (format_number(_v(row, "sales_unit_price"), force_int=True), upper_suffix) if part)
    lower_note = " ".join(part for part in (format_number(_v(row, "purchase_unit_price"), force_int=True), _v(row, "delivery_short_name")) if part)
    return {
        # 品名列の表示はトリムしない（商品名称内のブランク・前後空白を保持する）。
        # 空判定・name=="*" 判定は別途 strip 済みの name_key / _v を使う（is_star 等）。
        "name": _v_raw(row, "product_name"),
        "name_key": _v(row, "product_name"),
        "item_name": _v(row, "product_name"),
        "dims": _v(row, "product_note"),
        "item_note": _v(row, "product_note"),
        "qty_spec": _v(row, "window_symbol"),
        "qty": f"{qty}{unit}" if qty or unit else "",
        "quantity": f"{qty}{unit}" if qty or unit else "",
        "unit_price": unit_price_display,
        "unit_price_display": unit_price_display,
        "amount": amount_display,
        "amount_display": amount_display,
        "note_lines": [] if is_star else [line for line in (upper_note, lower_note) if line],
        "finish_date": finish,
        # 合計欄（上下2段）算出用の元データ。表示用整形前の生値を保持する。
        "sales_unit_price": "" if is_star else _v(row, "sales_unit_price"),
        "purchase_unit_price": "" if is_star else _v(row, "purchase_unit_price"),
        "ordered_quantity": "" if is_star else _v(row, "ordered_quantity"),
    }


def _format_unit_display(value: str) -> str:
    """単価/金額表示用: 小数第3位で四捨五入、0または空の場合は空文字を返す。"""
    dec = _decimal(value)
    if dec is None or dec == 0:
        return ""
    rounded = dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return ""
    return format_number(str(rounded), suffix="㎡")


def resolve_unit_and_amount_values(
    row: dict[str, str], *, logger: logging.Logger | None = None
) -> tuple[str, str]:
    op_type = _v(row, "op_type")
    unit_name = _v(row, "quantity_unit_name")

    if op_type == "00":
        if unit_name in {"ケース", "ロット"}:
            return _v(row, "case_lot_square"), _v(row, "ordered_stat_quantity")
        return _v(row, "stat_quantity"), _v(row, "ordered_stat_quantity")

    if op_type == "01":
        unit_value = _v(row, "stat_quantity")
        amount_value = _v(row, "ordered_stat_quantity")

        if _is_zero_or_blank(unit_value):
            unit_value = _v(row, "op02_square")
            if logger:
                logger.info("OP区分01フォールバック: 統計数量=0 のため単価列に02時平米を使用")

        if _is_zero_or_blank(amount_value):
            amount_value = _v(row, "op02_total_square")
            if logger:
                logger.info("OP区分01フォールバック: 受注統計数量=0 のため金額列に02時総平米を使用")

        return unit_value, amount_value

    if op_type == "02":
        return _v(row, "op02_square"), _v(row, "op02_total_square")

    return _v(row, "stat_quantity"), _v(row, "ordered_stat_quantity")


def _with_display_name_aliases(row: dict[str, str], *, logger: logging.Logger | None = None) -> dict[str, str]:
    if _is_current_olap_layout(row):
        for alias, (_, key) in _r1_display_alias_keys().items():
            if alias not in row:
                row[alias] = _s(row, key)
    for alias, keys in _FALLBACK_DISPLAY_KEYS.items():
        if alias not in row:
            fallback_key = next((key for key in _fallback_keys(row, alias) if _s(row, key)), "")
            if fallback_key:
                value = _s(row, fallback_key)
                row[alias] = value
                if logger and alias not in _r1_display_alias_keys():
                    display_name = _ALIAS_DISPLAY_NAMES.get(alias, alias)
                    logger.warning(
                        "WARNING: OLAP表示名 '%s' が見つからないため、fallback key '%s' を使用しました。",
                        display_name,
                        fallback_key,
                    )
    if "operator" not in row:
        row["operator"] = _s(row, "15")
    return row


def _r1_display_alias_keys() -> dict[str, tuple[str, str]]:
    global _R1_DISPLAY_ALIAS_KEYS
    if _R1_DISPLAY_ALIAS_KEYS is None:
        _R1_DISPLAY_ALIAS_KEYS = _load_r1_display_alias_keys()
    return _R1_DISPLAY_ALIAS_KEYS


def _load_r1_display_alias_keys() -> dict[str, tuple[str, str]]:
    path = Path(__file__).resolve().parents[1] / "templates" / "voucher_olap_request.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, tuple[str, str]] = {}
    for column in payload.get("R1List", []):
        if not isinstance(column, dict):
            continue
        display_name = str(column.get("OLAP表示名") or "").strip()
        display_no = column.get("OLAP表示No")
        if display_name and display_no is not None:
            key = str(display_no)
            alias = _DISPLAY_NO_ALIASES.get(key) or _DISPLAY_NAME_ALIASES.get(display_name)
            if alias:
                result[alias] = (display_name, key)
    return result


def _r1_list(response_data: object) -> object | None:
    if isinstance(response_data, dict) and "ResponseData" in response_data:
        response_data = response_data.get("ResponseData")
    if isinstance(response_data, dict):
        return response_data.get("R1List")
    if isinstance(response_data, list):
        return response_data
    return None


def _raw_r1_rows(response_data: object, *, allow_direct_dict: bool = False) -> list[object]:
    r1_list = _r1_list(response_data)
    if isinstance(r1_list, dict):
        return [r1_list[key] for key in sorted(r1_list, key=_numeric_sort_key)]
    if isinstance(r1_list, list):
        return list(r1_list)
    if allow_direct_dict and isinstance(response_data, dict):
        return [response_data]
    if isinstance(response_data, dict) and "ResponseData" not in response_data and _looks_like_direct_r1_list(response_data):
        return [response_data[key] for key in sorted(response_data, key=_numeric_sort_key)]
    return []


def _looks_like_direct_r1_list(value: dict[object, object]) -> bool:
    if not value or any(str(key) in _RESULT_STATUS_KEYS for key in value):
        return False
    return all(isinstance(row, dict) for row in value.values())


def _is_result_status_row(row: dict[object, object]) -> bool:
    keys = {str(key) for key in row.keys()}
    return bool(keys & {"OutputLog", "RData"}) or _RESULT_STATUS_KEYS.issubset(keys)


def _v(row: dict[str, str], alias: str) -> str:
    value = _s(row, alias)
    if value:
        return value
    for key in _fallback_keys(row, alias):
        value = _s(row, key)
        if value:
            return value
    return ""


def _s_raw(row: dict[str, str], key: str) -> str:
    """値をトリムせず文字列化して返す（前後・全角・連続空白を保持する）。

    `_s` と異なり strip しない。品名など、ブランクを保持したい表示用途で使う。
    None は空文字にする。
    """
    value = row.get(key)
    return "" if value is None else str(value)


def _v_raw(row: dict[str, str], alias: str) -> str:
    """`_v` と同じキー解決で、値だけはトリムせず生のまま返す（品名のブランク保持用）。

    どのキーを採用するか（本来のキー or フォールバックキー）の判定は `_v` と同じく
    strip 後の非空判定で行うが、返す文字列は元のブランクを保持した生値にする。

    注意: alias 自体のキー（例 "product_name"）には `_with_display_name_aliases` が
    strip 済みの値を格納しているため、raw 取得ではそれを読まず、元の表示Noキー
    （または fallback キー）から生値を読み直す。これを怠ると先頭・末尾・全角の
    空白が失われる。
    """
    for key in _raw_source_keys(row, alias):
        if _s(row, key):
            return _s_raw(row, key)
    # 元キーが特定できない／全て空の場合のみ alias 値（strip 済みの可能性あり）に頼る。
    return _s_raw(row, alias)


def _raw_source_keys(row: dict[str, str], alias: str) -> list[str]:
    """`_v_raw` 用に、alias の元値が入っている生値キーを優先順で返す。

    現行レイアウトでは表示Noキー（例 商品名称=16）、加えて fallback キーを候補にする。
    alias 自体のキー（strip 済み値）は含めない。
    """
    keys: list[str] = []
    display = _r1_display_alias_keys().get(alias)
    if display and _is_current_olap_layout(row):
        keys.append(display[1])
    for key in _fallback_keys(row, alias):
        if key not in keys:
            keys.append(key)
    return keys


def _fallback_keys(row: dict[str, str], alias: str) -> tuple[str, ...]:
    if not _is_current_olap_layout(row) and alias in _OLD_LAYOUT_KEYS:
        return _OLD_LAYOUT_KEYS[alias]
    return _FALLBACK_DISPLAY_KEYS.get(alias, ())


def _is_current_olap_layout(row: dict[str, str]) -> bool:
    return any(key in row for key in ("36", "37", "38", "39", "40", "41", "42", "43", "44"))


def _s(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip().strip("\u3000")


def _blank_if_dash(value: object) -> str:
    text = str(value or "").strip().strip("\u3000")
    if text in ("", "-", "－"):
        return ""
    return text


def _decimal(value: str | None) -> Decimal | None:
    text = (value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _is_zero_or_blank(value: str) -> bool:
    dec = _decimal(value)
    return dec is None or dec == 0


def compute_op_calculated_fields(row: dict[str, str], *, logger: logging.Logger | None = None) -> None:
    """02時平米・02時総平米・00時ケース・ロット平米をアプリ側で計算してrowに書き込む。

    既にOLAP側から値が取得できている場合（非空）はスキップする。
    W寸法/H寸法/受注数量はカンマを除去して数値変換する。失敗時は空白のまま。
    """
    _compute_op_calculated_fields(row, logger=logger)


def _compute_op_calculated_fields(row: dict[str, str], *, logger: logging.Logger | None = None) -> None:
    width = _decimal(row.get("width"))
    height = _decimal(row.get("height"))
    if width is None or height is None:
        return
    try:
        op02_sq = width * height / Decimal("1000000")
        if not row.get("op02_square"):
            row["op02_square"] = str(op02_sq)
    except Exception:
        if logger:
            logger.warning("02時平米計算失敗: width=%s height=%s", row.get("width"), row.get("height"))
        return
    try:
        if not row.get("op02_total_square"):
            qty = _decimal(row.get("ordered_quantity"))
            if qty is not None:
                row["op02_total_square"] = str(op02_sq * qty)
    except Exception:
        if logger:
            logger.warning("02時総平米計算失敗: op02_square=%s ordered_quantity=%s", row.get("op02_square"), row.get("ordered_quantity"))
    try:
        if not row.get("case_lot_square"):
            row["case_lot_square"] = str((width * Decimal("25.4")) * (height * Decimal("25.4")) / Decimal("1000000"))
    except Exception:
        if logger:
            logger.warning("00時ケース・ロット平米計算失敗: width=%s height=%s", row.get("width"), row.get("height"))


def _int(value: str | None) -> int:
    try:
        return int(Decimal((value or "").replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        return 0


def _chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[index:index + size] for index in range(0, len(rows), size)]


def _numeric_sort_key(value: object) -> tuple[int, str]:
    text = str(value)
    return (int(text), text) if text.isdigit() else (10**9, text)
