from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from tks_to_kintone.transform import OUTPUT_HEADERS


SETTINGS_CSV_COLUMNS = "registration_preview/csv_columns"


@dataclass(frozen=True)
class CsvColumn:
    """CSV列の固定キーと表示・出力用ヘッダー。"""

    key: str
    header: str


@dataclass(frozen=True)
class CsvColumnSetting:
    key: str
    enabled: bool = True


# 登録前確認画面から作成するCSVの標準列順。
# key は永続化用の固定値であり、日本語の表示名を変更しても保存設定に影響しない。
STANDARD_CSV_COLUMNS: tuple[CsvColumn, ...] = (
    CsvColumn("order_no", "受注No"),
    CsvColumn("order_line_no", "受注行No"),
    CsvColumn("glass_processing_type", "硝/加工"),
    CsvColumn("additional_type", "追加区分"),
    CsvColumn("finish_date", "仕上日"),
    CsvColumn("shipping_type", "出荷区分"),
    CsvColumn("process", "工程"),
    CsvColumn("delivery_date", "納品日"),
    CsvColumn("sales_date", "売上日"),
    CsvColumn("order_date", "発注日"),
    CsvColumn("receipt_date", "入庫日"),
    CsvColumn("customer_code", "得意先コード"),
    CsvColumn("customer_name", "得意先名称"),
    CsvColumn("product_code", "商品コード"),
    CsvColumn("processed_product_code", "加工完成品商品コード"),
    CsvColumn("product_name", "商品名称"),
    CsvColumn("width", "W寸法"),
    CsvColumn("height", "H寸法"),
    CsvColumn("rate_summary_code", "掛率集計コード"),
    CsvColumn("rate_summary_name", "掛率集計名称"),
    CsvColumn("rate_summary_code_1", "掛率集計コード_1"),
    CsvColumn("rate_summary_name_2", "掛率集計名称_2"),
    CsvColumn("order_quantity", "受注数量"),
    CsvColumn("glass_quantity", "硝子枚数"),
    CsvColumn("area", "㎡"),
    CsvColumn("total_area", "総㎡"),
    CsvColumn("purchase_amount", "仕入金額"),
    CsvColumn("purchase_unit_price", "仕入単価"),
    CsvColumn("processed_purchase_unit_price", "加工完成品仕入単価"),
    CsvColumn("glass_thickness", "硝子厚み"),
    CsvColumn("total_weight", "総重量"),
    CsvColumn("product_category", "品種区分"),
    CsvColumn("supplier_code", "発注先コード"),
    CsvColumn("processed_supplier_code", "加工完成品仕入先コード"),
    CsvColumn("search_key", "検索キー"),
    CsvColumn("order_code_match", "発注コード_照合"),
    CsvColumn("order_code_hq_judgement", "発注コード_本社判定"),
    CsvColumn("processing_judgement", "加工判定"),
    CsvColumn("washing_type", "洗浄区分"),
    CsvColumn("judgement", "判定"),
    CsvColumn("op_type", "OP区分"),
    CsvColumn("processing_name", "加工名"),
    CsvColumn("processing_mm", "加工mm"),
    CsvColumn("processing_type", "加工種類"),
    CsvColumn("customer_selection", "得意先選択"),
)


def default_csv_column_settings() -> list[CsvColumnSetting]:
    return [CsvColumnSetting(column.key, True) for column in STANDARD_CSV_COLUMNS]


def reconcile_csv_column_settings(saved: Iterable[CsvColumnSetting]) -> list[CsvColumnSetting]:
    """保存済み順を維持し、不明キーを除外して新規列を末尾へ補完する。"""
    known_keys = {column.key for column in STANDARD_CSV_COLUMNS}
    result: list[CsvColumnSetting] = []
    seen: set[str] = set()
    for setting in saved:
        if setting.key in known_keys and setting.key not in seen:
            result.append(CsvColumnSetting(setting.key, bool(setting.enabled)))
            seen.add(setting.key)
    for column in STANDARD_CSV_COLUMNS:
        if column.key not in seen:
            result.append(CsvColumnSetting(column.key, True))
    return result


def load_csv_column_settings(settings: Any) -> list[CsvColumnSetting]:
    """QSettingsから列設定を読み込む。未保存・空・破損時は標準順へ戻す。"""
    raw = settings.value(SETTINGS_CSV_COLUMNS, "")
    if not isinstance(raw, str) or not raw.strip():
        return default_csv_column_settings()
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not values:
            raise ValueError("CSV column settings must be a non-empty list")
        parsed: list[CsvColumnSetting] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("CSV column setting must be an object")
            key = value.get("key")
            enabled = value.get("enabled")
            if not isinstance(key, str) or not key or not isinstance(enabled, bool):
                raise ValueError("CSV column setting has invalid fields")
            parsed.append(CsvColumnSetting(key, enabled))
        reconciled = reconcile_csv_column_settings(parsed)
        if not any(item.key in {column.key for column in STANDARD_CSV_COLUMNS} for item in parsed):
            return default_csv_column_settings()
        return reconciled
    except (TypeError, ValueError, json.JSONDecodeError):
        return default_csv_column_settings()


def save_csv_column_settings(settings: Any, columns: Iterable[CsvColumnSetting]) -> None:
    reconciled = reconcile_csv_column_settings(columns)
    payload = [{"key": item.key, "enabled": item.enabled} for item in reconciled]
    settings.setValue(SETTINGS_CSV_COLUMNS, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    settings.sync()


def enabled_csv_columns(settings: Iterable[CsvColumnSetting]) -> list[CsvColumn]:
    by_key = {column.key: column for column in STANDARD_CSV_COLUMNS}
    return [by_key[item.key] for item in settings if item.enabled and item.key in by_key]


# OUTPUT_HEADERSとのずれはデータ欠落に直結するため、定義変更時に即座に検出する。
assert [column.header for column in STANDARD_CSV_COLUMNS[:-4]] == OUTPUT_HEADERS
