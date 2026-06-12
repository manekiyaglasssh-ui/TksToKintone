from __future__ import annotations

import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .csv_io import read_csv_dicts, write_quoted_csv


SOURCE_HEADERS = [
    "受注No",
    "受注行No",
    "硝/加工",
    "追加区分",
    "納品書No",
    "納品書行No",
    "納品日",
    "売上日",
    "発注日",
    "入庫日",
    "得意先コード",
    "得意先名称",
    "商品コード",
    "加工完成品商品コード",
    "商品名称",
    "W寸法",
    "H寸法",
    "掛率集計コード",
    "掛率集計名称",
    "掛率集計コード_1",
    "掛率集計名称_1",
    "受注数量",
    "硝子枚数",
    "㎡",
    "総㎡",
    "仕入金額",
    "仕入単価",
    "加工完成品仕入単価",
    "硝子厚み",
    "総重量",
    "品種区分",
    "発注先コード",
    "加工完成品仕入先コード",
]

OUTPUT_HEADERS = [
    "受注No",
    "受注行No",
    "硝/加工",
    "追加区分",
    "仕上日",
    "出荷区分",
    "工程",
    "納品日",
    "売上日",
    "発注日",
    "入庫日",
    "得意先コード",
    "得意先名称",
    "商品コード",
    "加工完成品商品コード",
    "商品名称",
    "W寸法",
    "H寸法",
    "掛率集計コード",
    "掛率集計名称",
    "掛率集計コード_1",
    "掛率集計名称_2",
    "受注数量",
    "硝子枚数",
    "㎡",
    "総㎡",
    "仕入金額",
    "仕入単価",
    "加工完成品仕入単価",
    "硝子厚み",
    "総重量",
    "品種区分",
    "発注先コード",
    "加工完成品仕入先コード",
    "検索キー",
    "発注コード_照合",
    "発注コード_本社判定",
    "加工判定",
    "洗浄区分",
    "判定",
]

HQ_ORDER_CODES = {"11111"}
GLASS = "1"
PROCESSING = "2"
OPTIONAL_SOURCE_HEADERS = {"総重量"}


def transform_files(
    glass_csv: Path,
    processing_csv: Path,
    output_csv: Path,
    shiage_date: str = "",
    shukka_kbn: str = "",
) -> list[dict[str, str]]:
    rows = transform_rows(read_csv_dicts(glass_csv), read_csv_dicts(processing_csv))
    for row in rows:
        row["仕上日"] = shiage_date
        row["出荷区分"] = shukka_kbn
    write_quoted_csv(output_csv, OUTPUT_HEADERS, rows)
    return rows


def transform_rows(
    glass_rows: list[dict[str, str]],
    processing_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    _validate_required_headers(glass_rows, "素板抽出ロジックCSV")
    _validate_required_headers(processing_rows, "加工抽出ロジックCSV")
    source_rows = [_normalize_source_row(row) for row in [*glass_rows, *processing_rows]]
    source_rows.sort(key=_sort_key)

    _set_order_quantity_and_product_name(source_rows)
    _apply_hinsyu_kbn_5(source_rows)
    _set_search_and_order_code_match(source_rows)
    _set_judgements(source_rows)

    return [_to_output_row(row) for row in source_rows if row.get("判定") != "-"]


def _validate_required_headers(rows: list[dict[str, str]], csv_name: str) -> None:
    if not rows:
        return
    missing = [header for header in SOURCE_HEADERS if header not in OPTIONAL_SOURCE_HEADERS and header not in rows[0]]
    if missing:
        raise ValueError(f"{csv_name} に必須列がありません: {', '.join(missing)}")


def _normalize_source_row(row: dict[str, str]) -> dict[str, str]:
    normalized = {header: row.get(header, "") for header in SOURCE_HEADERS}
    normalized["掛率集計名称_2"] = row.get("掛率集計名称_1", row.get("掛率集計名称_2", ""))
    return normalized


def _sort_key(row: dict[str, str]) -> tuple[int, int, int, int, str]:
    return (
        _to_int(row.get("受注No", "")),
        _to_int(row.get("納品書行No", "")),
        _to_int(row.get("受注行No", "")),
        _to_int(row.get("硝/加工", "")),
        row.get("商品コード", ""),
    )


def _set_order_quantity_and_product_name(rows: list[dict[str, str]]) -> None:
    current_order_quantity = ""
    current_glass_name = ""

    for row in rows:
        if row.get("硝/加工") == GLASS:
            current_order_quantity = row.get("受注数量", "")
            current_glass_name = _glass_name_prefix(row.get("商品名称", ""))

        row["硝子枚数"] = _format_excel_quantity(current_order_quantity)

        if row.get("硝/加工") != GLASS and current_glass_name:
            product_name = row.get("商品名称", "")
            if not product_name.startswith(current_glass_name):
                row["商品名称"] = current_glass_name + product_name


def _apply_hinsyu_kbn_5(rows: list[dict[str, str]]) -> None:
    for row in rows:
        if row.get("品種区分") == "5":
            row["掛率集計コード"] = row.get("掛率集計コード_1", "")
            row["掛率集計名称"] = row.get("掛率集計名称_2", "")
            row["発注先コード"] = row.get("加工完成品仕入先コード", "")


def _set_search_and_order_code_match(rows: list[dict[str, str]]) -> None:
    glass_order_code_by_line: dict[tuple[str, str], str] = {}
    for row in rows:
        row["検索キー"] = (
            row.get("受注No", "")
            + row.get("受注行No", "")
            + row.get("硝/加工", "")
            + row.get("追加区分", "")
        )
        if row.get("硝/加工") == GLASS:
            glass_order_code_by_line[(row.get("受注No", ""), row.get("受注行No", ""))] = row.get("発注先コード", "")

    for row in rows:
        row["発注コード_照合"] = glass_order_code_by_line.get((row.get("受注No", ""), row.get("受注行No", "")), "")
        row["発注コード_本社判定"] = "〇" if row["発注コード_照合"] in HQ_ORDER_CODES else "×"


def _set_judgements(rows: list[dict[str, str]]) -> None:
    for index, row in enumerate(rows):
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        current_type = row.get("硝/加工")
        next_type = next_row.get("硝/加工") if next_row else ""

        if current_type == GLASS:
            row["加工判定"] = "〇" if next_type == PROCESSING else "×"
        elif current_type == PROCESSING:
            row["加工判定"] = "〇"
        else:
            row["加工判定"] = "×"

        row["洗浄区分"] = "1:洗浄" if current_type == GLASS and next_type == PROCESSING else "0:不要"

        if current_type == GLASS:
            if row.get("発注コード_本社判定", "").strip() == "〇":
                row["判定"] = "〇(本社発注コード)"
            elif next_type == PROCESSING:
                row["判定"] = "〇(加工あり)"
            elif next_type == GLASS and next_row and next_row.get("発注先コード") == "11116":
                row["判定"] = "〇(セット品)"

    current_result = ""
    for row in rows:
        if row.get("硝/加工") == GLASS:
            current_result = row.get("判定", "")
        row["判定"] = current_result


def _to_output_row(row: dict[str, str]) -> dict[str, str]:
    output = {header: "" for header in OUTPUT_HEADERS}
    for header in OUTPUT_HEADERS:
        if header == "仕上日" or header == "出荷区分":
            continue
        if header == "総重量":
            output[header] = calculate_total_weight(row)
            continue
        if header == "掛率集計名称_2":
            output[header] = row.get("掛率集計名称_2", "")
        else:
            output[header] = _format_output_value(header, row.get(header, ""))
    return output


def calculate_total_weight(row: dict[str, str]) -> str:
    """Return total weight as a fixed 2-decimal string, or blank if unavailable."""
    area = _to_decimal(row.get("㎡", ""))
    thickness = _to_decimal(row.get("硝子厚み", ""))
    if area is None or thickness is None:
        return ""
    weight = area * thickness * Decimal("2.5")
    return format(weight.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _glass_name_prefix(value: str) -> str:
    wide = unicodedata.normalize("NFKC", value)
    wide = "".join(_to_full_width_ascii(char) for char in wide)
    return wide.split("　", 1)[0]


def _to_full_width_ascii(char: str) -> str:
    code = ord(char)
    if code == 0x20:
        return "　"
    if 0x21 <= code <= 0x7E:
        return chr(code + 0xFEE0)
    return char


def _format_output_value(header: str, value: str) -> str:
    if header in {"受注数量", "硝子枚数"}:
        return _format_excel_quantity(value)
    if header in {"加工完成品仕入単価", "硝子厚み", "総重量", "㎡", "総㎡"}:
        return _format_decimal_text(value)
    return value


def _format_excel_quantity(value: str) -> str:
    number = _to_decimal(value)
    if number is None:
        return value
    if number == number.to_integral_value():
        return f"{int(number)} "
    return _strip_decimal(number)


def _format_decimal_text(value: str) -> str:
    number = _to_decimal(value)
    if number is None:
        return value
    return _strip_decimal(number)


def _strip_decimal(number: Decimal) -> str:
    normalized = number.normalize()
    text = format(normalized, "f")
    return "0" if text == "-0" else text


def _to_decimal(value: str) -> Decimal | None:
    if value in {"", "-"}:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _to_int(value: str) -> int:
    number = _to_decimal(value)
    return int(number) if number is not None else 0
