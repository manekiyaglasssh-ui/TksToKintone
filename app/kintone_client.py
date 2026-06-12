from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - dependency is installed in packaged Windows builds.
    requests = None  # type: ignore[assignment]

from app.models import AppConfig, KintoneResult


class _UnavailableRequestsHttpError(Exception):
    pass


REQUESTS_HTTP_ERROR = requests.HTTPError if requests is not None else _UnavailableRequestsHttpError


BATCH_SIZE = 100
SEARCH_KEY_HEADER = "検索キー"
DATE_FIELDS = ("仕上日", "発注日", "入庫日", "納品日", "売上日")
EMPTY_DATE_VALUES = {"", "null", "none", "-", "0", "0000/00/00", "0000-00-00"}
CHECKBOX_FIELDS: frozenset[str] = frozenset()
CHECKBOX_DELIMITER = "|"
EXCLUDED_CSV_HEADERS = frozenset({"工程"})

# CSV列名 -> kintoneフィールドコード のデフォルト補完エントリ。
# 既存インストール済み環境の field_mapping.json に不足している場合のみ自動追加する。
DEFAULT_FIELD_MAPPING_SUPPLEMENTS: dict[str, str] = {
    "加工名": "加工名",
}


class KintoneClient:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        supplement_field_mapping(config.paths.field_mapping_json, logger)
        self.mapping = load_field_mapping(config.paths.field_mapping_json)

    def register_rows(self, rows: list[dict[str, str]]) -> KintoneResult:
        normalized_rows = self._normalize_rows_for_kintone(rows)
        self._log_date_normalization_samples(rows, normalized_rows)
        self._log_checkbox_field_samples(normalized_rows)

        result = KintoneResult()
        for batch_start, batch in _chunks_with_start(normalized_rows, BATCH_SIZE):
            records = [self._to_upsert_record(row, batch_start + index) for index, row in enumerate(batch)]
            try:
                response_data = self._put_records_upsert(records)
                result.success_count += len(batch)
                insert_count, update_count = _count_upsert_operations(response_data)
                if insert_count or update_count:
                    self.logger.info("kintone登録成功: %s件 (追加: %s件, 更新: %s件)", len(batch), insert_count, update_count)
                else:
                    self.logger.info("kintone登録成功: %s件", len(batch))
            except REQUESTS_HTTP_ERROR as exc:
                result.failure_count += len(batch)
                result.failed_records.extend(batch)
                response = getattr(exc, "response", None)
                body = response.text if response is not None else ""
                self.logger.error("kintone登録失敗: HTTP %s %s", getattr(response, "status_code", ""), body)
            except Exception:
                result.failure_count += len(batch)
                result.failed_records.extend(batch)
                self.logger.exception("kintone登録失敗")
        return result

    def _to_upsert_record(self, row: dict[str, str], batch_index: int) -> dict[str, object]:
        search_key_field_code = self._search_key_field_code()
        search_key_value = row.get(SEARCH_KEY_HEADER, "").strip()
        if not search_key_value:
            raise ValueError(f"records[{batch_index}] {SEARCH_KEY_HEADER} が空のためkintoneへ追加/更新できません。")
        return {
            "updateKey": {"field": search_key_field_code, "value": search_key_value},
            "record": self._to_record(row, exclude_field_codes={search_key_field_code}),
        }

    def _to_record(self, row: dict[str, str], exclude_field_codes: set[str] | None = None) -> dict[str, object]:
        exclude_field_codes = exclude_field_codes or set()
        record: dict[str, object] = {}
        for csv_header, field_code in self.mapping.items():
            if field_code in exclude_field_codes:
                continue
            if csv_header in EXCLUDED_CSV_HEADERS:
                continue
            if csv_header in CHECKBOX_FIELDS:
                raw = row.get(csv_header, "")
                values = [v for v in raw.split(CHECKBOX_DELIMITER) if v] if raw else []
                if values:
                    record[field_code] = {"value": values}
            elif csv_header in row and row[csv_header] != "":
                record[field_code] = {"value": row[csv_header]}
        return record

    def _search_key_field_code(self) -> str:
        field_code = self.mapping.get(SEARCH_KEY_HEADER, "")
        if not field_code:
            raise ValueError(f"field_mapping.json に {SEARCH_KEY_HEADER} の設定がありません。")
        return field_code

    def _normalize_rows_for_kintone(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        normalized_rows: list[dict[str, str]] = []
        for record_index, row in enumerate(rows):
            normalized = dict(row)
            for field in DATE_FIELDS:
                if field in normalized:
                    normalized[field] = normalize_kintone_date(normalized[field], f"records[{record_index}] {field}")
            normalized_rows.append(normalized)
        return normalized_rows

    def _log_date_normalization_samples(self, rows: list[dict[str, str]], normalized_rows: list[dict[str, str]]) -> None:
        for index, (source, normalized) in enumerate(zip(rows[:3], normalized_rows[:3])):
            sample = {
                field: {
                    "input": source.get(field, ""),
                    "send": normalized.get(field, ""),
                }
                for field in DATE_FIELDS
                if field in source or field in normalized
            }
            self.logger.info("日付正規化結果 records[%s]: %s", index, json.dumps(sample, ensure_ascii=False))

    def _log_checkbox_field_samples(self, rows: list[dict[str, str]]) -> None:
        for csv_header in CHECKBOX_FIELDS:
            if csv_header not in self.mapping:
                self.logger.warning("field_mapping.json に %s が存在しないためkintoneへ送信されません。", csv_header)
                continue
            for index, row in enumerate(rows[:3]):
                raw = row.get(csv_header, "")
                values = [v for v in raw.split(CHECKBOX_DELIMITER) if v] if raw else []
                self.logger.info(
                    "%s送信サンプル records[%s]: input=%s send=%s",
                    csv_header, index, raw, json.dumps(values, ensure_ascii=False),
                )

    def _put_records_upsert(self, records: list[dict[str, object]]) -> dict[str, object]:
        if requests is None:
            raise RuntimeError("kintone登録には requests が必要です。requirements.txt をインストールしてください。")
        url = f"https://{self.config.kintone_domain}/k/v1/records.json"
        response = requests.put(
            url,
            headers={
                "X-Cybozu-API-Token": self.config.kintone_api_token,
                "Content-Type": "application/json",
            },
            json={"app": self.config.kintone_app_id, "upsert": True, "records": records},
            timeout=60,
        )
        response.raise_for_status()
        if not response.text:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}


def load_field_mapping(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"field_mapping.json はJSONオブジェクトにしてください: {path}")
    return {str(key): str(value) for key, value in data.items() if str(key).strip() and str(value).strip()}


def supplement_field_mapping(path: Path, logger: logging.Logger | None = None) -> None:
    """field_mapping.json に不足している標準エントリを追加してファイルに書き戻す。既存キーは上書きしない。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return
    added: dict[str, str] = {}
    for csv_key, field_code in DEFAULT_FIELD_MAPPING_SUPPLEMENTS.items():
        if csv_key not in data:
            data[csv_key] = field_code
            added[csv_key] = field_code
    if not added:
        return
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    if logger is not None:
        for csv_key, field_code in added.items():
            logger.info("field_mapping.json に不足しているマッピングを追加しました: %s -> %s", csv_key, field_code)


def _chunks_with_start(rows: list[dict[str, str]], size: int) -> Iterable[tuple[int, list[dict[str, str]]]]:
    for index in range(0, len(rows), size):
        yield index, rows[index : index + size]


def _count_upsert_operations(response_data: dict[str, object]) -> tuple[int, int]:
    records = response_data.get("records")
    if not isinstance(records, list):
        return 0, 0
    insert_count = 0
    update_count = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        operation = str(record.get("operation") or "").upper()
        if operation == "INSERT":
            insert_count += 1
        elif operation == "UPDATE":
            update_count += 1
    return insert_count, update_count


def normalize_kintone_date(value: object, label: str = "日付") -> str:
    text = "" if value is None else unicodedata.normalize("NFKC", str(value)).strip()
    if text.lower() in EMPTY_DATE_VALUES:
        return ""

    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            pass

    japanese_match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if japanese_match:
        year, month, day = (int(part) for part in japanese_match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    raise ValueError(f"{label} の日付形式が不正です: {value}")
