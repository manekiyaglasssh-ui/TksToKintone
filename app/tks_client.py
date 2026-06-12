from __future__ import annotations

import csv
import copy
import io
import json
import logging
import re
import socket
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - dependency is installed in packaged Windows builds.
    requests = None  # type: ignore[assignment]

from app.models import AppConfig, RunInput
from tks_to_kintone.transform import SOURCE_HEADERS


LOGIN_PATH = "/c/ログイン認証"
OLAP_DATA_PATH = "/c/OLAPデータ"
LOGIN_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}
OLAP_HEADERS = {
    "Accept": "application/json, text/csv",
    "Content-Type": "application/json; charset=utf-8",
}


class BaseTksClient(ABC):
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    @abstractmethod
    def login(self, run_input: RunInput) -> None:
        raise NotImplementedError

    @abstractmethod
    def fetch_csvs(self, run_input: RunInput, work_dir: Path, encoding: str) -> tuple[Path, Path]:
        raise NotImplementedError

    def has_auth_cookie(self) -> bool:
        return False


class MockTksClient(BaseTksClient):
    """file:// CSVを使うモッククライアント。TKS実接続なしでCSV加工以降を検証する。"""

    def login(self, run_input: RunInput) -> None:
        self.logger.info("TKSログインはモック設定のためスキップします")

    def fetch_csvs(self, run_input: RunInput, work_dir: Path, encoding: str) -> tuple[Path, Path]:
        if not _is_file_url(self.config.tks_soba_csv_url) or not _is_file_url(self.config.tks_kakou_csv_url):
            raise ValueError("モック実行では TKS_SOBA_CSV_URL と TKS_KAKOU_CSV_URL に file:// パスを指定してください。")
        soba_csv = Path(self.config.tks_soba_csv_url.removeprefix("file://"))
        kakou_csv = Path(self.config.tks_kakou_csv_url.removeprefix("file://"))
        if not soba_csv.exists():
            raise FileNotFoundError(f"素板CSVが見つかりません: {soba_csv}")
        if not kakou_csv.exists():
            raise FileNotFoundError(f"加工CSVが見つかりません: {kakou_csv}")
        self.logger.info("モック素板CSV: %s", soba_csv)
        self.logger.info("モック加工CSV: %s", kakou_csv)
        return soba_csv, kakou_csv


class HttpTksClient(BaseTksClient):
    """TKS OLAP HTTP client."""

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        super().__init__(config, logger)
        if requests is None:
            raise RuntimeError("TKS実通信には requests が必要です。requirements.txt をインストールしてください。")
        self.session = requests.Session()

    def login(self, run_input: RunInput) -> None:
        url = self._endpoint(LOGIN_PATH)
        payload = self._build_login_payload(run_input)
        request_path = self._save_debug_json("login_request", _mask_login_payload(payload))
        self.logger.info("TKSログイン開始")
        self.logger.info("HTTPメソッド: POST")
        self.logger.info("ログインURL: %s", url)
        response = self.session.post(
            url,
            data=_json_bytes(payload),
            headers=LOGIN_HEADERS,
            timeout=60,
        )
        self._log_http_response("ログイン", response)
        response_path = self._save_debug_text("login_response", "json", _decode_response_text(response, "utf-8"))
        response.raise_for_status()
        data = _safe_json(response)
        self._log_login_result_status(data)
        response_data = data.get("ResponseData") if isinstance(data, dict) else None
        has_auth_cookie = self.has_auth_cookie()
        self.logger.info(".ASPXAUTH Cookie取得有無: %s", "あり" if has_auth_cookie else "なし")
        if not isinstance(response_data, dict):
            self.logger.error("TKSログイン失敗: ResponseData がありません")
            self._log_login_failure_debug_paths(request_path, response_path, data)
            raise RuntimeError(
                _format_login_failure(
                    x0=None,
                    result_message=_extract_result_status_message(data),
                    request_path=request_path,
                    response_path=response_path,
                    reason="ResponseData がありません。",
                )
            )
        x0 = response_data.get("Ｘ0", response_data.get("X0"))
        self.logger.info("ResponseData.Ｘ0: %s", x0)
        if x0 != "00":
            self.logger.error("TKSログイン失敗: ResponseData.Ｘ0 が 00 ではありません")
            self._log_login_failure_debug_paths(request_path, response_path, data)
            raise RuntimeError(
                _format_login_failure(
                    x0=x0,
                    result_message=_extract_result_status_message(data),
                    request_path=request_path,
                    response_path=response_path,
                    reason="レスポンスコードが 00 ではありません。",
                )
            )
        if not has_auth_cookie:
            self._log_login_failure_debug_paths(request_path, response_path, data)
            raise RuntimeError("TKSログインCookie .ASPXAUTH が取得できていません。")
        self.logger.info("TKSログイン成功")

    def has_auth_cookie(self) -> bool:
        return any(cookie.name == ".ASPXAUTH" for cookie in self.session.cookies)

    def fetch_csvs(self, run_input: RunInput, work_dir: Path, encoding: str) -> tuple[Path, Path]:
        kakou_path = self.fetch_kakou_csv(run_input.denpyo_numbers, work_dir, encoding)
        soba_path = self.fetch_soba_csv(run_input.denpyo_numbers, work_dir, encoding)
        return soba_path, kakou_path

    def fetch_kakou_csv(self, denpyo_numbers: list[str], work_dir: Path, encoding: str) -> Path:
        return self._fetch_olap_csv("kakou", denpyo_numbers, work_dir / "kakou_extract.csv", encoding)

    def fetch_soba_csv(self, denpyo_numbers: list[str], work_dir: Path, encoding: str) -> Path:
        return self._fetch_olap_csv("soba", denpyo_numbers, work_dir / "soba_extract.csv", encoding)

    def _fetch_olap_csv(
        self,
        kind: str,
        denpyo_numbers: list[str],
        output_path: Path,
        encoding: str,
    ) -> Path:
        label = "加工" if kind == "kakou" else "素板"
        url = self._endpoint(OLAP_DATA_PATH)
        self.logger.info("%sCSV取得開始", label)
        self.logger.info("HTTPメソッド: PUT")
        self.logger.info("OLAPデータ取得URL: %s", url)
        self.logger.info("対象伝票番号件数: %s", len(denpyo_numbers))
        payload = self._build_olap_payload(kind, denpyo_numbers)
        response = self.session.put(
            url,
            data=_json_bytes(payload),
            headers=OLAP_HEADERS,
            timeout=120,
        )
        self._log_http_response(f"{label}OLAP取得", response)
        response_text = _decode_response_text(response, encoding)
        self._save_debug_text(f"{kind}_response", "txt", response_text[:200000])
        response.raise_for_status()
        csv_text, row_count = self._convert_olap_response_to_csv(response, encoding, kind)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(csv_text, encoding=encoding, newline="")
        self.logger.info("%sCSV取得成功: %s", label, output_path)
        self.logger.info("CSV保存先: %s", output_path)
        self.logger.info("CSV行数: %s", row_count)
        return output_path

    def _build_login_payload(self, run_input: RunInput) -> OrderedDict[str, object]:
        return OrderedDict(
            [
                ("契約会社コード", self.config.company_code),
                ("ログインID", run_input.olap_login_id),
                ("パスワード", run_input.olap_password),
                ("ログイン認証区分", self.config.tks_login_auth_type),
                ("端末識別ID", self.config.tks_device_id),
                ("コンピュータ名", self.config.tks_computer_name or socket.gethostname()),
                ("IPアドレス", self.config.tks_ip_address),
                ("ScreenName", _screen_name_value(self.config.tks_screen_name)),
            ]
        )

    def _build_olap_payload(self, kind: str, denpyo_numbers: list[str]) -> OrderedDict[str, object]:
        if kind == "kakou":
            template_path = self.config.tks_kakou_request_template
        elif kind == "soba":
            template_path = self.config.tks_soba_request_template
        else:
            raise ValueError(f"未対応のOLAP種別です: {kind}")
        if template_path is None:
            raise RuntimeError(f"{kind} のOLAPリクエストテンプレートが設定されていません。")
        if not template_path.exists():
            raise FileNotFoundError(f"{kind} のOLAPリクエストテンプレートが見つかりません: {template_path}")

        with template_path.open("r", encoding="utf-8-sig") as fp:
            payload = json.load(fp, object_pairs_hook=OrderedDict)
        if not isinstance(payload, dict):
            raise RuntimeError(f"{kind} のOLAPリクエストテンプレートはJSONオブジェクトにしてください: {template_path}")
        payload = copy.deepcopy(payload)
        if kind == "kakou":
            _apply_r2_overrides(payload, self.config.tks_kakou_r2_overrides)
        else:
            _apply_r2_overrides(payload, self.config.tks_soba_r2_overrides)
        order_no_value = ",".join(denpyo_numbers)
        _replace_order_no_condition(payload, order_no_value, template_path)
        self.logger.info("%s OLAPテンプレート: %s", "加工" if kind == "kakou" else "素板", template_path)
        return payload

    def _convert_olap_response_to_csv(
        self,
        response: Any,
        encoding: str,
        kind: str,
    ) -> tuple[str, int]:
        text = _decode_response_text(response, encoding)
        if _is_csv_response(response, text):
            return text, _count_csv_rows(text)

        try:
            data = response.json()
        except json.JSONDecodeError:
            self.logger.error("%s OLAPレスポンスはCSVでもJSONでもありません。先頭1000文字: %s", kind, text[:1000])
            raise RuntimeError(f"{kind} OLAPレスポンスをCSVとして解釈できません。")

        table = _extract_table_rows(data)
        if table is None:
            self.logger.error("%s OLAP JSON構造が未対応です。先頭1000文字: %s", kind, _mask_response_text(text[:1000]))
            raise RuntimeError(
                f"{kind} OLAPレスポンスJSONから表形式データを抽出できません。"
                " ResponseData/R1List/Data/Rows の構造を確認してください。"
            )

        rows = _normalize_table_rows(table, SOURCE_HEADERS, kind)
        return _rows_to_csv_text(SOURCE_HEADERS, rows), len(rows)

    def _endpoint(self, path: str) -> str:
        return self.config.tks_base_url.rstrip("/") + quote(path, safe="/")

    def _save_debug_text(self, prefix: str, extension: str, text: str) -> Path:
        debug_dir = self.config.paths.work_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.{extension}"
        path.write_text(_mask_response_text(text), encoding="utf-8")
        return path

    def _save_debug_json(self, prefix: str, data: object) -> Path:
        debug_dir = self.config.paths.work_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.json"
        text = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        return path

    def _log_http_response(self, label: str, response: Any) -> None:
        content_type = response.headers.get("Content-Type", "")
        text = _decode_response_text(response, self.config.csv_encoding)
        self.logger.info("%s HTTPステータスコード: %s", label, response.status_code)
        self.logger.info("%s Content-Type: %s", label, content_type)
        self.logger.info("%s レスポンス先頭500文字: %s", label, _mask_response_text(text[:500]))
        try:
            data = response.json()
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self.logger.info("%s JSONトップレベルキー: %s", label, ", ".join(data.keys()))

    def _log_login_result_status(self, data: object) -> None:
        if not isinstance(data, dict):
            self.logger.info("ログイン ResultStatus: (JSONオブジェクトではありません)")
            return
        result_status = data.get("ResultStatus")
        output_log = result_status.get("OutputLog") if isinstance(result_status, dict) else None
        message_first = output_log.get("MessageFirst") if isinstance(output_log, dict) else None
        message_middle = output_log.get("MessageMiddle") if isinstance(output_log, dict) else None
        message_last = output_log.get("MessageLast") if isinstance(output_log, dict) else None
        self.logger.info("ログイン ResultStatus: %s", _compact_json(result_status))
        self.logger.info("ログイン OutputLog: %s", _compact_json(output_log))
        self.logger.info("ログイン MessageFirst: %s", _compact_json(message_first))
        self.logger.info("ログイン MessageMiddle: %s", _compact_json(message_middle))
        self.logger.info("ログイン MessageLast: %s", _compact_json(message_last))

    def _log_login_failure_debug_paths(self, request_path: Path, response_path: Path, data: object) -> None:
        self.logger.error("ResultStatus 内のメッセージ: %s", _extract_result_status_message(data))
        self.logger.error("login_request debugファイル: %s", request_path)
        self.logger.error("login_response debugファイル: %s", response_path)


def create_tks_client(config: AppConfig, logger: logging.Logger) -> BaseTksClient:
    if config.tks_client_mode == "mock":
        return MockTksClient(config, logger)
    if config.tks_client_mode == "http":
        return HttpTksClient(config, logger)
    raise ValueError(f"TKS_CLIENT_MODE は mock または http を指定してください: {config.tks_client_mode}")


def _safe_json(response: Any) -> object:
    if not response.text:
        return {}
    try:
        return response.json()
    except json.JSONDecodeError:
        return {}


def _is_file_url(value: str) -> bool:
    return value.lower().startswith("file://")


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _mask_login_payload(payload: OrderedDict[str, object]) -> OrderedDict[str, object]:
    masked = OrderedDict(payload)
    if "パスワード" in masked:
        masked["パスワード"] = "********"
    return masked


def _screen_name_value(value: str) -> int | str:
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else stripped


def _replace_order_no_condition(payload: dict[str, object], order_no_value: str, template_path: Path) -> None:
    r2_list = payload.get("R2List")
    if not isinstance(r2_list, list):
        raise RuntimeError(f"OLAPリクエストテンプレートに R2List がありません: {template_path}")
    for condition in r2_list:
        if isinstance(condition, dict) and condition.get("フィールド論理名") == "受注No":
            condition["OLAP値"] = order_no_value
            return
    raise RuntimeError(f"R2List に フィールド論理名=受注No の条件がありません: {template_path}")


def _apply_r2_overrides(payload: dict[str, object], overrides: dict[str, dict[str, str]]) -> None:
    if not overrides:
        return
    r2_list = payload.get("R2List")
    if not isinstance(r2_list, list):
        return
    for index, condition in enumerate(r2_list):
        if not isinstance(condition, dict):
            continue
        field_name = str(condition.get("フィールド論理名") or "")
        override = overrides.get(str(index), overrides.get(field_name))
        if not override:
            continue
        for key, value in override.items():
            condition[key] = value


def _decode_response_text(response: Any, fallback_encoding: str) -> str:
    encodings = [response.encoding, "utf-8-sig", fallback_encoding, "cp932"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return response.content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return response.content.decode(fallback_encoding, errors="replace")


def _is_csv_response(response: Any, text: str) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    stripped = text.lstrip("\ufeff\r\n\t ")
    if "text/csv" in content_type or "application/csv" in content_type:
        return True
    if stripped.startswith("{") or stripped.startswith("["):
        return False
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    return "," in first_line


def _count_csv_rows(text: str) -> int:
    rows = list(csv.reader(io.StringIO(text)))
    return max(len(rows) - 1, 0)


def _extract_table_rows(data: object) -> object | None:
    candidates = []
    if isinstance(data, dict):
        response_data = data.get("ResponseData")
        if isinstance(response_data, dict):
            candidates.extend(
                [
                    response_data.get("R1List"),
                    response_data.get("Data"),
                    response_data.get("Rows"),
                    response_data.get("records"),
                ]
            )
        candidates.extend([data.get("R1List"), data.get("Data"), data.get("Rows")])
        if not isinstance(response_data, dict):
            candidates.append(response_data)
    for candidate in candidates:
        if _looks_like_table(candidate):
            return candidate
    return None


def _looks_like_table(value: object) -> bool:
    if isinstance(value, list):
        return all(isinstance(row, dict | list) for row in value)
    if isinstance(value, dict):
        return all(isinstance(row, dict | list) for row in value.values())
    return False


def _normalize_table_rows(table: object, headers: list[str], kind: str = "") -> list[dict[str, str]]:
    raw_rows: list[object]
    if isinstance(table, dict):
        raw_rows = [table[key] for key in sorted(table, key=_numeric_sort_key)]
    elif isinstance(table, list):
        raw_rows = table
    else:
        return []

    rows: list[dict[str, str]] = []
    for raw_row in raw_rows:
        if isinstance(raw_row, list):
            row = {header: _string_value(raw_row[index]) if index < len(raw_row) else "" for index, header in enumerate(headers)}
        elif isinstance(raw_row, dict):
            row = _normalize_dict_row(raw_row, headers)
        else:
            continue
        _apply_olap_fixed_columns(row, kind)
        rows.append(row)
    return rows


def _normalize_dict_row(row: dict[object, object], headers: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for index, header in enumerate(headers, start=1):
        value = row.get(str(index), row.get(index, row.get(header, "")))
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        normalized[header] = _string_value(value)
    return normalized


def _apply_olap_fixed_columns(row: dict[str, str], kind: str) -> None:
    if not row.get("硝/加工"):
        if kind == "kakou":
            row["硝/加工"] = "2"
        elif kind == "soba":
            row["硝/加工"] = "1"
    if not row.get("追加区分"):
        row["追加区分"] = "1"


def _rows_to_csv_text(headers: list[str], rows: list[dict[str, str]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return output.getvalue()


def _numeric_sort_key(value: object) -> tuple[int, str]:
    text = str(value)
    return (int(text), text) if text.isdigit() else (10**9, text)


def _string_value(value: object) -> str:
    return "" if value is None else str(value)


def _compact_json(value: object) -> str:
    if value is None:
        return "(なし)"
    return _mask_response_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _extract_result_status_message(data: object) -> str:
    if not isinstance(data, dict):
        return "(なし)"
    result_status = data.get("ResultStatus")
    if not isinstance(result_status, dict):
        return "(なし)"

    messages: list[str] = []
    output_log = result_status.get("OutputLog")
    if isinstance(output_log, dict):
        for key in ("MessageFirst", "MessageMiddle", "MessageLast"):
            messages.extend(_extract_message_items(output_log.get(key)))

    for key in ("RData", "PropertyName", "MessageName", "MessageParams"):
        value = result_status.get(key)
        if value in (None, "", 0, [], {}):
            continue
        messages.append(f"{key}={_string_value(value)}")

    return " / ".join(messages) if messages else "(なし)"


def _extract_message_items(value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        items = value.get("Items")
        if isinstance(items, list):
            return [_string_value(item) for item in items if item not in (None, "")]
        return [_compact_json(value)]
    if isinstance(value, list):
        return [_string_value(item) for item in value if item not in (None, "")]
    return [_string_value(value)]


def _format_login_failure(
    x0: object,
    result_message: str,
    request_path: Path,
    response_path: Path,
    reason: str,
) -> str:
    return "\n".join(
        [
            f"TKSログインに失敗しました。{reason}",
            f"ResponseData.Ｘ0: {_string_value(x0) if x0 is not None else '(なし)'}",
            f"ResultStatus 内のメッセージ: {result_message}",
            f"login_request debugファイル: {request_path}",
            f"login_response debugファイル: {response_path}",
        ]
    )


def _mask_response_text(text: str) -> str:
    masked = text
    secret_keys = ("パスワード", "Password", "password", "KINTONE_API_TOKEN", "api_token", "token", "Cookie", ".ASPXAUTH")
    for key in secret_keys:
        masked = re.sub(rf'("{re.escape(key)}"\s*:\s*")[^"]*(")', rf'\1***\2', masked)
        masked = re.sub(rf"({re.escape(key)}=)[^;\s,]+", rf"\1***", masked)
    return masked
