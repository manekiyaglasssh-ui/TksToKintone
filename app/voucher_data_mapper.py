from __future__ import annotations

import io
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
from itertools import groupby
from pathlib import Path
from typing import Any

from app.voucher_templates import FORM_DETAIL_ROWS

_logger = logging.getLogger(__name__)

UPPER_AREA_OP_CATEGORIES = frozenset({"00", "01", "02"})


def normalize_op_category(value: object) -> str:
    """OP区分を先頭ゼロを保った文字列として正規化する。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def op_category_value(row: dict[str, Any]) -> str:
    """新旧フィールド名からOP区分を取得する（数値への変換は行わない）。"""
    for key in ("op_category", "op_category_raw", "op_type"):
        value = row.get(key)
        if value not in (None, ""):
            return normalize_op_category(value)
    return ""


def should_draw_upper_area_by_op_category(row: dict[str, Any]) -> bool:
    """単価・金額列上段の㎡を表示できるOP区分か返す。"""
    return op_category_value(row) in UPPER_AREA_OP_CATEGORIES


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
    "数量単位コード": "quantity_unit_code",
    "統計数量": "stat_quantity",
    "受注統計数量": "ordered_stat_quantity",
    "売上単価": "sales_unit_price",
    "仕入単価": "purchase_unit_price",
    "明細指示区分": "detail_instruction_type",
    "納品書発行略称": "delivery_short_name",
    "加工仕上日": "finish_date",
    "納入先住所1": "delivery_address1",
    "納入先住所2": "delivery_address2",
    "配送コース": "delivery_course_code",
    "配送コースコード": "delivery_course_code",
    "配送コース名称": "delivery_course_name",
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
    # 納入先住所2（OLAP表示No=46）。古いテンプレート/レスポンスに無くても
    # 表示Noキーで解決できるよう明示する。値が無ければ単に空欄になる。
    "delivery_address2": ("46",),
    # 数量単位コード（OLAP表示No=47）。数量単位コード="19" の明細で数量列を
    # 空欄にするための判定に使う。古いテンプレート/レスポンスに無くても表示Noキーで
    # 解決でき、値が無ければ空欄扱い（既存の数量表示を維持する）。
    "quantity_unit_code": ("47",),
    "delivery_course_code": ("48",),
    "delivery_course_name": ("49",),
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


def extract_r1_rows(
    response_data: object,
    *,
    logger: logging.Logger | None = None,
    request_columns: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """OLAP R1明細をアプリ内キーへ正規化する。

    ``request_columns`` に実際に送信した R1List を渡すことで、古い
    テンプレートの送信直前補完で表示Noが48以外になっても、その実Noで
    解析できる。引数省略時は同梱テンプレートと旧固定Noの互換動作を保つ。
    """
    raw_rows = _raw_r1_rows(response_data)
    request_alias_keys = _display_alias_keys_from_columns(request_columns or [])

    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or _is_result_status_row(raw):
            continue
        string_row = {str(key): "" if value is None else str(value) for key, value in raw.items()}
        course_code_source_key = _alias_source_key(
            string_row, "delivery_course_code", request_alias_keys
        )
        course_name_source_key = _alias_source_key(
            string_row, "delivery_course_name", request_alias_keys
        )
        row = _with_display_name_aliases(
            string_row,
            logger=logger,
            request_alias_keys=request_alias_keys,
        )
        for alias, source_key in (
            ("delivery_course_code", course_code_source_key),
            ("delivery_course_name", course_name_source_key),
        ):
            if not source_key:
                continue
            row[f"{alias}_raw"] = _blank_if_dash(_s_raw(string_row, source_key))
            row[f"{alias}_response_key"] = source_key
            requested = request_alias_keys.get(alias)
            row[f"{alias}_display_no"] = (
                requested[1] if requested
                else (source_key if source_key.isdigit() else "")
            )
            metadata = _request_column_for_alias(request_columns or [], alias)
            row[f"{alias}_logical_name"] = str(
                metadata.get("フィールド論理名") or ""
            )
            if alias == "delivery_course_name":
                # 既存キャッシュ／呼び出し側との互換。値は名称列由来に限定する。
                row["delivery_course_response_key"] = source_key
                row["delivery_course_display_no"] = row[f"{alias}_display_no"]
        _compute_op_calculated_fields(row, logger=logger)
        rows.append(row)
        if logger:
            course_code = _blank_if_dash(row.get("delivery_course_code"))
            course_name = _blank_if_dash(row.get("delivery_course_name"))
            logger.info(
                "voucher_delivery_course_code_parsed "
                "order_no=%s voucher_no=%s course_code=%r response_key=%s "
                "display_no=%s logical_name=%s",
                _v(row, "order_no"),
                _v(row, "voucher_no"),
                course_code,
                course_code_source_key or "(not_found)",
                row.get("delivery_course_code_display_no", ""),
                row.get("delivery_course_code_logical_name", "配送コース"),
            )
            logger.info(
                "voucher_delivery_course_name_parsed "
                "order_no=%s voucher_no=%s course_code=%r course_name=%r "
                "response_key=%s display_no=%s logical_name=%s",
                _v(row, "order_no"),
                _v(row, "voucher_no"),
                course_code,
                course_name,
                course_name_source_key or "(not_found)",
                row.get("delivery_course_name_display_no", ""),
                row.get("delivery_course_name_logical_name", "配送コース名称"),
            )
    codes = [quantity_unit_code_value(row) for row in rows]
    if any(codes):
        (logger or _logger).info(
            "voucher_mapper_quantity_unit_code_mapped: rows=%s hidden19=%s",
            len(rows),
            sum(1 for code in codes if code == "19"),
        )
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
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            _v(row, "voucher_no"),
            _v(row, "order_no"),
            _int(_v(row, "order_line_no")),
        ),
    )
    pages: list[dict[str, Any]] = []
    for _, voucher_rows in groupby(
        sorted_rows, key=lambda row: _v(row, "voucher_no")
    ):
        voucher_group = list(voucher_rows)
        delivery_course_code = first_non_blank_delivery_course_code(voucher_group)
        delivery_course_name = first_non_blank_delivery_course(voucher_group)
        course_source_row = _first_non_blank_delivery_course_row(voucher_group)
        course_response_key = str(
            course_source_row.get("delivery_course_name_response_key") or ""
        )
        course_display_no = str(
            course_source_row.get("delivery_course_name_display_no") or ""
        )
        distinct = list(dict.fromkeys(
            value
            for row in voucher_group
            if (value := normalize_delivery_course_name(
                row.get("delivery_course_name")
                or row.get("delivery_course_name_raw")
            ))
        ))
        first = voucher_group[0]
        if len(distinct) > 1:
            _logger.warning(
                "voucher_delivery_course_conflict "
                "order_no=%s voucher_no=%s values=%r adopted=%r rule=first_non_blank",
                _v(first, "order_no"),
                _v(first, "voucher_no"),
                distinct,
                delivery_course_name,
            )
        for group in _chunks(voucher_group, FORM_DETAIL_ROWS):
            page = _build_page(
                group,
                today,
                delivery_course_code=delivery_course_code,
                delivery_course_name=delivery_course_name,
                delivery_course_response_key=course_response_key,
                delivery_course_display_no=course_display_no,
            )
            pages.append(page)
            _logger.info(
                "voucher_delivery_course_page_aggregated "
                "order_no=%s voucher_no=%s response_key=%s display_no=%s "
                "value=%r source_rows=%s rule=first_non_blank",
                page.get("order_no", ""),
                page.get("voucher_no", ""),
                course_response_key or "(not_available)",
                course_display_no,
                delivery_course_name,
                len(voucher_group),
            )
            _logger.info(
                "voucher_delivery_course_name_selected "
                "order_no=%s voucher_no=%s course_code=%r course_name=%r "
                "response_key=%s display_no=%s logical_name=%s rule=first_non_blank",
                page.get("order_no", ""), page.get("voucher_no", ""),
                delivery_course_code, delivery_course_name,
                course_response_key or "(not_available)", course_display_no,
                course_source_row.get("delivery_course_name_logical_name", "配送コース名称"),
            )
    return pages


def normalize_delivery_course_name(value: object) -> str:
    """配送コース名称を文字列のまま正規化する。"""
    return _blank_if_dash(value)


def normalize_delivery_course_code(value: object) -> str:
    """配送コースコードを文字列のまま正規化する（数値化しない）。"""
    return _blank_if_dash(value)


def first_non_blank_delivery_course_code(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        value = normalize_delivery_course_code(
            row.get("delivery_course_code") or row.get("delivery_course_code_raw")
        )
        if value:
            return value
    return ""


def first_non_blank_delivery_course(rows: list[dict[str, Any]]) -> str:
    """明細順で最初の非空配送コース名称を返す。"""
    for row in rows:
        value = normalize_delivery_course_name(
            row.get("delivery_course_name")
            or row.get("delivery_course_name_raw")
        )
        if value:
            return value
    return ""


def _first_non_blank_delivery_course_row(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    for row in rows:
        if normalize_delivery_course_name(
            row.get("delivery_course_name")
            or row.get("delivery_course_name_raw")
        ):
            return row
    return {}


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


def format_quantity(value: object) -> str:
    """数量を最大小数3桁で、末尾の不要なゼロを除いて表示する。

    OLAPから受け取った文字列/Decimalの10進精度を保つ。floatも直接
    ``Decimal`` へ渡さず、文字列表現を経由して二進浮動小数点の誤差を
    表示へ持ち込まない。想定外に4桁以上ある入力は四捨五入せず切り捨てる。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    numeric_text = text.replace(",", "")
    try:
        dec = Decimal(numeric_text)
    except (InvalidOperation, ValueError):
        return text
    if not dec.is_finite():
        return text
    if dec == 0:
        return "0"
    with localcontext() as context:
        context.prec = max(28, len(dec.as_tuple().digits) + abs(dec.adjusted()) + 4)
        dec = dec.quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    formatted = f"{dec:,.3f}"
    return formatted.rstrip("0").rstrip(".")


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


def _build_page(
    rows: list[dict[str, str]],
    today: date,
    *,
    delivery_course_code: str | None = None,
    delivery_course_name: str | None = None,
    delivery_course_response_key: str = "",
    delivery_course_display_no: str = "",
) -> dict[str, Any]:
    first = rows[0]
    details = [_detail_row(row) for row in rows]
    non_star_rows = [row for row in rows if _v(row, "product_name") != "*"]
    upper_total = sum((_decimal(_v(row, "sales_unit_price")) or Decimal("0")) for row in non_star_rows)
    lower_total = sum((_decimal(_v(row, "purchase_unit_price")) or Decimal("0")) for row in non_star_rows)
    delivery_address1 = _blank_if_dash(_s(first, "delivery_address1"))
    summary_line2 = _s(first, "order_summary")
    # 納入先住所2（伝票中央表示用）。空欄/None/空白のみ/"-" は空文字にする。
    # 古いレスポンス/キャッシュに delivery_address2 が無くても _s が空文字を返すため
    # エラーにならない。
    delivery_address2 = _blank_if_dash(_s(first, "delivery_address2"))
    delivery_address_combined = combine_delivery_address(
        delivery_address1, delivery_address2
    )
    summary_line1 = delivery_address_combined
    if delivery_course_code is None:
        delivery_course_code = first_non_blank_delivery_course_code(rows)
    if delivery_course_name is None:
        delivery_course_name = first_non_blank_delivery_course(rows)
        source_row = _first_non_blank_delivery_course_row(rows)
        delivery_course_response_key = str(
            source_row.get("delivery_course_name_response_key") or ""
        )
        delivery_course_display_no = str(
            source_row.get("delivery_course_name_display_no") or ""
        )
    delivery_course_code = normalize_delivery_course_code(delivery_course_code)
    delivery_course_name = normalize_delivery_course_name(delivery_course_name)
    delivery_course_name_logical_name = str(
        _first_non_blank_delivery_course_row(rows).get(
            "delivery_course_name_logical_name"
        ) or "配送コース名称"
    )
    _logger.info(
        "voucher_delivery_course_mapped "
        "order_no=%s voucher_no=%s response_key=%s display_no=%s "
        "value=%r detail_rows=%s",
        _v(first, "order_no"),
        _v(first, "voucher_no"),
        delivery_course_response_key or "(not_available)",
        delivery_course_display_no,
        delivery_course_name,
        len(rows),
    )
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
        "delivery_course_code": delivery_course_code,
        "delivery_course_code_raw": delivery_course_code,
        "delivery_course_name": delivery_course_name,
        "delivery_course_name_raw": delivery_course_name,
        "delivery_course_response_key": delivery_course_response_key,
        "delivery_course_display_no": delivery_course_display_no,
        "delivery_course_name_response_key": delivery_course_response_key,
        "delivery_course_name_display_no": delivery_course_display_no,
        "delivery_course_name_logical_name": delivery_course_name_logical_name,
        "construction_rep": _v(first, "construction_rep"),
        "details": details,
        "summary_line1": summary_line1,
        "summary_line2": summary_line2,
        "summary_lines": [summary_line1, summary_line2],
        "delivery_address1": delivery_address1,
        "delivery_address2": delivery_address2,
        "delivery_address_combined": delivery_address_combined,
        "property_lines": [" ".join(
            part for part in (_s(first, "property_no"), _s(first, "property_name")) if part
        )],
        "total_note_upper": format_number(str(upper_total), force_int=True),
        "total_note_lower": format_number(str(lower_total), force_int=True),
        "qr_order_no": _v(first, "order_no"),
        # 取引区分（移動伝票=8 のPDF表示制御用）。OLAP取得時に得意先コードから付与される。
        "transaction_type": _s(first, "transaction_type"),
        # 得意先マスタ「納品書単価・金額上段（硝子）」は取得・ログ用に保持する。
        "invoice_price_amount_upper_glass": _s(first, "invoice_price_amount_upper_glass"),
        "invoice_price_amount_upper_glass_raw": _s(
            first, "invoice_price_amount_upper_glass_raw"
        ) or _s(first, "invoice_price_amount_upper_glass"),
        "invoice_price_amount_upper_glass_enabled": normalize_invoice_price_amount_upper_glass(
            _s(first, "invoice_price_amount_upper_glass_raw")
            or _s(first, "invoice_price_amount_upper_glass")
        ),
        # 単価・金額列の下段（および合計行下段）の表示判定に使うのは下段フィールドのみ。
        "invoice_price_amount_lower_glass": _s(first, "invoice_price_amount_lower_glass"),
        "invoice_price_amount_lower_glass_raw": _s(
            first, "invoice_price_amount_lower_glass_raw"
        ) or _s(first, "invoice_price_amount_lower_glass"),
        "invoice_price_amount_lower_glass_enabled": normalize_invoice_price_amount_lower_glass(
            _s(first, "invoice_price_amount_lower_glass_raw")
            or _s(first, "invoice_price_amount_lower_glass")
        ),
    }


def combine_delivery_address(address1: object, address2: object) -> str:
    """PDF表示用に納入先住所1・2を空白なしで自然に連結する。

    元の2フィールドは変更せず、前後の半角・全角空白と欠損記号だけを除く。
    日本語住所の番地と建物名を想定し、データにない区切り文字は追加しない。
    """
    part1 = _blank_if_dash(address1)
    part2 = _blank_if_dash(address2)
    combined = f"{part1}{part2}" if part1 and part2 else part1 or part2
    if combined:
        _logger.info(
            "voucher_delivery_address_combined: address1=%s address2=%s combined=%s",
            part1,
            part2,
            combined,
        )
    return combined


def quantity_unit_code_value(row: dict[str, str]) -> str:
    """明細行の数量単位コードを文字列で取り出す（前後空白を除去する）。

    quantity_unit_code を優先し、無ければ quantity_unit_code_raw を見る。
    数値型 19 や " 19 " のような表記でも `str().strip()` で正規化する。
    未取得・空欄なら空文字を返す（既存の数量表示を維持するための判定に使う）。
    """
    for alias in ("quantity_unit_code", "quantity_unit_code_raw"):
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def is_quantity_hidden_by_unit_code(row: dict[str, str]) -> bool:
    """数量単位コードが「19」の明細行かどうかを判定する。

    True の場合、その明細行の数量列（受注数量＋数量単位名称）は空欄にする。
    「19」以外・空欄・未取得は False（従来通り数量を表示する）。
    """
    return quantity_unit_code_value(row) == "19"


def _detail_row(row: dict[str, str]) -> dict[str, Any]:
    is_star = _v(row, "product_name") == "*"
    unit_code = quantity_unit_code_value(row)
    hide_quantity = is_quantity_hidden_by_unit_code(row)
    if hide_quantity:
        _logger.info(
            "voucher_quantity_hidden_by_unit_code_19: order_no=%s order_line_no=%s",
            _v(row, "order_no"),
            _v(row, "order_line_no"),
        )
    elif unit_code:
        _logger.info(
            "voucher_quantity_drawn_by_unit_code: order_no=%s quantity_unit_code=%s",
            _v(row, "order_no"),
            unit_code,
        )
    else:
        _logger.debug("voucher_quantity_unit_code_missing_use_existing_behavior")
    # 数量列（受注数量＋数量単位名称）は、数量単位コード="19" の明細行のみ空欄にする。
    # 列そのもの・罫線・他列（品名/摘要/単価/金額/寸法）は従来通り出力する。
    qty = "" if (is_star or hide_quantity) else format_quantity(row.get("ordered_quantity"))
    unit = "" if (is_star or hide_quantity) else _v(row, "quantity_unit_name")
    op_category = op_category_value(row)
    if is_star or not should_draw_upper_area_by_op_category(row):
        unit_price_display = ""
        amount_display = ""
    else:
        raw_unit_price, _ = resolve_unit_and_amount_values(row)
        # 金額列上段は単価列上段と同じ元データ・同じ丸め後の数値に受注数量を掛けて
        # 算出する。W/H寸法からの再計算値（受注統計数量/02時総平米）は使わない。
        unit_upper = _rounded_unit_value(raw_unit_price)
        unit_price_display = _format_rounded_value(unit_upper)
        amount_upper = _amount_upper_value(unit_upper, _v(row, "ordered_quantity"))
        amount_display = _format_rounded_value(amount_upper)
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
        # 数量単位コードは内部データとして保持する（数量列の表示制御の根拠）。
        # Kintone登録・CSV出力には使わないが、あっても既存処理は壊れない。
        "quantity_unit_code": unit_code,
        # PDF上段㎡の表示判定用。元の op_type も変更せず保持し、数値化しない。
        "op_category": op_category,
        "op_category_raw": normalize_op_category(
            row.get("op_category_raw") or row.get("op_category") or row.get("op_type")
        ),
        "delivery_course_code": normalize_delivery_course_code(
            row.get("delivery_course_code")
        ),
        "delivery_course_code_raw": normalize_delivery_course_code(
            row.get("delivery_course_code_raw") or row.get("delivery_course_code")
        ),
        "delivery_course_name": normalize_delivery_course_name(
            row.get("delivery_course_name")
        ),
        "delivery_course_name_raw": normalize_delivery_course_name(
            row.get("delivery_course_name_raw")
            or row.get("delivery_course_name")
        ),
        "delivery_course_response_key": str(
            row.get("delivery_course_name_response_key") or ""
        ),
        "delivery_course_display_no": str(
            row.get("delivery_course_name_display_no") or ""
        ),
        "delivery_course_name_response_key": str(
            row.get("delivery_course_name_response_key") or ""
        ),
        "delivery_course_name_display_no": str(
            row.get("delivery_course_name_display_no") or ""
        ),
    }


def _rounded_unit_value(value: str) -> Decimal | None:
    """単価/金額表示と同じ丸め(小数第3位四捨五入)を適用した数値を返す。

    0または空（丸め結果が0を含む）の場合は None を返し、表示は空欄になる。
    """
    dec = _decimal(value)
    if dec is None or dec == 0:
        return None
    rounded = dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return None
    return rounded


def _amount_upper_value(unit_value: Decimal | None, quantity: str) -> Decimal | None:
    """金額列上段の数値 = 単価列上段(丸め後) × 受注数量。

    単価列上段が空(None)、または受注数量が数値化できない場合は None（空欄）。
    積も表示と同じ小数第3位丸めを適用し、丸め結果が0なら None を返す。
    """
    if unit_value is None:
        return None
    qty = _decimal(quantity)
    if qty is None:
        return None
    product = (unit_value * qty).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return product if product != 0 else None


def _format_rounded_value(value: Decimal | None) -> str:
    """丸め済みの数値を表示文字列(㎡付き)に整形する。None は空文字。"""
    if value is None:
        return ""
    return format_number(str(value), suffix="㎡")


def _format_unit_display(value: str) -> str:
    """単価/金額表示用: 小数第3位で四捨五入、0または空の場合は空文字を返す。"""
    return _format_rounded_value(_rounded_unit_value(value))


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


def _with_display_name_aliases(
    row: dict[str, str],
    *,
    logger: logging.Logger | None = None,
    request_alias_keys: dict[str, tuple[str, str]] | None = None,
) -> dict[str, str]:
    # レスポンスが表示名または論理名をそのままキーにする形式。
    # 数値キーのレイアウト判定に依存せず最優先で拾う。
    for source_name, alias in _DISPLAY_NAME_ALIASES.items():
        value = _blank_if_dash(_s_raw(row, source_name))
        if not _s(row, alias) and value:
            row[alias] = value

    # この通信で実際に送信した表示No。古いテンプレートへの
    # 動的補完で 49 以降になった場合も、固定Noより先に使う。
    for alias, (_, key) in (request_alias_keys or {}).items():
        if alias not in row and _s(row, key):
            row[alias] = _blank_if_dash(_s_raw(row, key))

    if _is_current_olap_layout(row):
        for alias, (_, key) in _r1_display_alias_keys().items():
            if alias not in row and alias not in (request_alias_keys or {}):
                row[alias] = _s(row, key)
    for alias, keys in _FALLBACK_DISPLAY_KEYS.items():
        if alias not in row and alias not in (request_alias_keys or {}):
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


def _display_alias_keys_from_columns(
    columns: list[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    """実送信 R1List から alias -> (表示名, 表示No) を作る。"""
    result: dict[str, tuple[str, str]] = {}
    for column in columns:
        if not isinstance(column, dict):
            continue
        display_name = str(column.get("OLAP表示名") or "").strip()
        logical_name = str(column.get("フィールド論理名") or "").strip()
        display_no = column.get("OLAP表示No")
        if display_no is None:
            continue
        key = str(display_no)
        alias = (
            _DISPLAY_NAME_ALIASES.get(display_name)
            or _DISPLAY_NAME_ALIASES.get(logical_name)
            or _DISPLAY_NO_ALIASES.get(key)
        )
        if alias:
            result[alias] = (display_name or logical_name, key)
    return result


def _request_column_for_alias(
    columns: list[dict[str, Any]], alias: str
) -> dict[str, Any]:
    """実送信列からaliasに対応する列定義を返す。コード／名称は別判定する。"""
    for column in columns:
        if not isinstance(column, dict):
            continue
        display_name = str(column.get("OLAP表示名") or "").strip()
        logical_name = str(column.get("フィールド論理名") or "").strip()
        mapped = (
            _DISPLAY_NAME_ALIASES.get(display_name)
            or _DISPLAY_NAME_ALIASES.get(logical_name)
        )
        if mapped == alias:
            return column
    return {}


def _alias_source_key(
    row: dict[str, str],
    alias: str,
    request_alias_keys: dict[str, tuple[str, str]],
) -> str:
    """alias の実レスポンスキーを解析優先順で返す。"""
    requested = request_alias_keys.get(alias)
    candidates: list[str] = []
    if requested:
        candidates.append(requested[1])
    candidates.extend(
        name for name, mapped_alias in _DISPLAY_NAME_ALIASES.items()
        if mapped_alias == alias
    )
    static = _r1_display_alias_keys().get(alias)
    if static:
        candidates.append(static[1])
    candidates.extend(_FALLBACK_DISPLAY_KEYS.get(alias, ()))
    unique_candidates = list(dict.fromkeys(candidates))
    for key in unique_candidates:
        if _blank_if_dash(row.get(key)):
            return key
    for key in unique_candidates:
        if key in row:
            # 空値でも「そのキーで返った」ことは診断価値がある。
            return key
    return ""


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


def normalize_invoice_price_amount_upper_glass(value: object) -> bool:
    """得意先マスタの「納品書単価・金額上段（硝子）」を bool へ正規化する。

    「1」のときだけ True（int 1・str "1" を含む）。空・0・"0"・None・未取得・
    "false"・"2"・その他はすべて False。bool(value) は使わない（"0" が True 扱いに
    なる事故を防ぐ）。判定は文字列化して厳密比較する。
    """
    return str(value).strip() == "1"


def normalize_invoice_price_amount_lower_glass(value: object) -> bool:
    """得意先マスタの「納品書単価・金額下段（硝子）」を bool へ正規化する。

    「1」のときだけ True（int 1・str "1" を含む）。空・0・"0"・9・"9"・None・
    未取得・その他はすべて False。bool(value) は使わない。
    """
    return str(value).strip() == "1"


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
