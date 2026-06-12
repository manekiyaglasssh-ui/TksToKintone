from __future__ import annotations

import copy
import json
import logging
import os
import socket
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from app.config import resource_path
from app.models import AppConfig
from app.tks_client import LOGIN_HEADERS, LOGIN_PATH, OLAP_DATA_PATH, OLAP_HEADERS
from app.voucher_data_mapper import (
    count_r1_rows,
    display_mapping_summary,
    extract_r1_rows,
    first_r1_row_keys,
    has_result_status_row,
    r1_list_type_name,
    resolve_unit_and_amount_values,
    response_data_keys,
    response_top_keys,
)


_OP_TOGGLEABLE_FIELD_NAMES = ("OP区分", "商品コード")
_OP_CALC_FIELD_NAMES = ("00時ケース・ロット平米", "02時平米", "02時総平米")
_OP_RELATED_FIELD_NAMES = _OP_TOGGLEABLE_FIELD_NAMES + _OP_CALC_FIELD_NAMES


class OlapFetchError(RuntimeError):
    """OLAPレスポンス構造またはOLAP側エラーによる取得失敗。"""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail.strip()
        message = "OLAPデータ取得に失敗しました。"
        if self.detail:
            message = f"{message}\n{self.detail}"
        else:
            message = f"{message}\nログを確認してください。"
        super().__init__(message)


class OlapNoDataError(RuntimeError):
    """OLAP取得は成立したが対象行が0件。"""

    def __init__(self) -> None:
        super().__init__("対象データが見つかりません。\n伝票番号を確認してください。")


class VoucherOlapService:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._session = requests.Session() if requests is not None else None
        self._logged_in = False
        self.last_response_r1_count = 0

    def login_if_needed(self, login_id: str, password: str) -> None:
        if self.config.tks_client_mode == "mock":
            self._logged_in = True
            self.logger.info("売上伝票OLAPログイン成功: mode=mock")
            return
        if requests is None or self._session is None:
            raise RuntimeError("TKS実通信には requests が必要です。requirements.txt をインストールしてください。")
        if self._logged_in and self._has_auth_cookie():
            return
        payload = OrderedDict(
            [
                ("契約会社コード", self.config.company_code),
                ("ログインID", login_id),
                ("パスワード", password),
                ("ログイン認証区分", self.config.tks_login_auth_type),
                ("端末識別ID", self.config.tks_device_id),
                ("コンピュータ名", self.config.tks_computer_name or socket.gethostname()),
                ("IPアドレス", self.config.tks_ip_address),
                ("ScreenName", _screen_name_value(self.config.tks_screen_name)),
            ]
        )
        try:
            response = self._session.post(
                self._endpoint(LOGIN_PATH),
                data=_json_bytes(payload),
                headers=LOGIN_HEADERS,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            response_data = data.get("ResponseData") if isinstance(data, dict) else None
            x0 = response_data.get("Ｘ0", response_data.get("X0")) if isinstance(response_data, dict) else None
            if x0 != "00":
                self.logger.error("売上伝票OLAPログイン失敗: ResponseData.Ｘ0=%s", x0)
                raise RuntimeError(f"TKSログインに失敗しました。ResponseData.Ｘ0={x0}")
            if not self._has_auth_cookie():
                self.logger.error("売上伝票OLAPログイン失敗: .ASPXAUTH cookie missing")
                raise RuntimeError("TKSログインCookie .ASPXAUTH が取得できていません。")
            self._logged_in = True
            self.logger.info("売上伝票OLAPログイン成功")
        except Exception:
            self.logger.exception("売上伝票OLAPログイン失敗")
            raise

    def fetch_voucher_rows(self, order_no: str) -> list[dict[str, str]]:
        if self.config.tks_client_mode == "mock":
            data = _load_mock_response()
            self._log_response_diagnostics(order_no, data, request_executed=True)
            rows = extract_r1_rows(data, logger=self.logger)
            filtered = [row for row in rows if (row.get("order_no") or row.get("6") or "").strip() == order_no]
            self.last_response_r1_count += len(filtered)
            return filtered
        if self._session is None:
            raise RuntimeError("TKS実通信には requests が必要です。")
        payload, template_path = _build_voucher_payload(
            order_no,
            disable_op_fields=self.config.tks_voucher_olap_disable_op_fields,
            enabled_op_fields=self.config.tks_voucher_olap_enabled_op_fields,
        )
        url = self._endpoint(OLAP_DATA_PATH)
        self.logger.info("売上伝票OLAPデータ取得リクエスト実行: order_no=%s", order_no)
        self._log_request_diagnostics(order_no, url, template_path, payload)
        response = self._session.put(
            url,
            data=_json_bytes(payload),
            headers=OLAP_HEADERS,
            timeout=120,
        )
        response_text = _response_text(response)
        self.logger.info("売上伝票OLAP HTTPレスポンス: order_no=%s status_code=%s", order_no, response.status_code)
        self.logger.info("売上伝票OLAPレスポンス本文先頭: order_no=%s body=%s", order_no, response_text[:3000])
        response.raise_for_status()
        data = response.json()
        self._log_response_diagnostics(
            order_no,
            data,
            request_executed=True,
            status_code=response.status_code,
            response_text=response_text,
            request_url=url,
            request_payload=payload,
        )
        self.logger.info(
            "売上伝票OLAP切り分け結果: order_no=%s 現在有効なOP追加項目=%s ResponseData有無=%s MessageName=%s",
            order_no,
            _enabled_op_field_names(payload),
            str(isinstance(data, dict) and "ResponseData" in data).lower(),
            _message_name(data),
        )
        rows = _extract_voucher_rows_or_raise(data, logger=self.logger)
        if not rows:
            raise OlapNoDataError()
        self.last_response_r1_count += len(rows)
        self.logger.info("売上伝票OLAP取得完了: order_no=%s rows=%s", order_no, len(rows))
        if rows:
            first = rows[0]
            unit_price, amount = resolve_unit_and_amount_values(first, logger=self.logger)
            self.logger.info(
                "売上伝票OLAP先頭行OP値: order_no=%s"
                " 有効OP追加項目=%s"
                " op_type=%s product_code=%s"
                " 02時平米=%s 02時総平米=%s 00時ケース・ロット平米=%s"
                " 単価列採用値=%s 金額列採用値=%s",
                order_no,
                _enabled_op_field_names(payload),
                first.get("op_type", ""),
                first.get("product_code", ""),
                first.get("op02_square", ""),
                first.get("op02_total_square", ""),
                first.get("case_lot_square", ""),
                unit_price,
                amount,
            )
        return rows

    def fetch_vouchers(self, order_nos: list[str], login_id: str, password: str) -> list[dict[str, str]]:
        self.logger.info("売上伝票PDF作成入力伝票番号: %s", ",".join(order_nos))
        self.logger.info("売上伝票OLAP表示名マッピング: %s", display_mapping_summary())
        self.last_response_r1_count = 0
        self.login_if_needed(login_id, password)
        rows: list[dict[str, str]] = []
        for order_no in order_nos:
            rows.extend(self.fetch_voucher_rows(order_no))
        return rows

    def _log_request_diagnostics(
        self,
        order_no: str,
        request_url: str,
        template_path: Path,
        payload: dict[str, Any],
    ) -> None:
        columns = [column for column in payload.get("R1List", []) if isinstance(column, dict)]
        self.logger.info("売上伝票OLAPリクエストURL: order_no=%s url=%s", order_no, request_url)
        self.logger.info("売上伝票OLAPテンプレート: order_no=%s path=%s", order_no, template_path)
        self.logger.info("売上伝票OLAP対象データ: order_no=%s target=%s", order_no, payload.get("OLAP対象データ"))
        self.logger.info("売上伝票OLAP 現在有効なOP追加項目: order_no=%s items=%s", order_no, _enabled_op_field_names(payload))
        self.logger.info("売上伝票OLAP R1List項目数: order_no=%s count=%s", order_no, len(columns))
        self.logger.info(
            "売上伝票OLAP R1List末尾項目: order_no=%s items=%s",
            order_no,
            _column_display_names(columns[-8:]),
        )
        self.logger.info(
            "売上伝票OLAP検索条件: order_no=%s conditions=%s",
            order_no,
            _format_json_for_log(_request_conditions(payload)),
        )

    def _log_response_diagnostics(
        self,
        order_no: str,
        data: object,
        *,
        request_executed: bool,
        status_code: int | None = None,
        response_text: str = "",
        request_url: str = "",
        request_payload: dict[str, Any] | None = None,
    ) -> None:
        if has_result_status_row(data):
            self.logger.error(
                "売上伝票OLAPレスポンス階層異常: ResultStatus/OutputLog/RData が明細候補に含まれています。order_no=%s",
                order_no,
            )
        self.logger.info(
            "売上伝票OLAPレスポンス: order_no=%s request_executed=%s top_keys=%s ResponseData_keys=%s "
            "R1List_type=%s normalized_rows=%s first_row_keys=%s",
            order_no,
            request_executed,
            response_top_keys(data),
            response_data_keys(data),
            r1_list_type_name(data),
            count_r1_rows(data),
            first_r1_row_keys(data),
        )
        if isinstance(data, dict) and "ResponseData" not in data:
            self._log_olap_failure_details(
                order_no,
                data,
                status_code=status_code,
                response_text=response_text,
                request_url=request_url,
                request_payload=request_payload,
            )
        elif isinstance(data, dict) and _olap_error_message(data):
            self._log_olap_failure_details(
                order_no,
                data,
                status_code=status_code,
                response_text=response_text,
                request_url=request_url,
                request_payload=request_payload,
            )

    def _log_olap_failure_details(
        self,
        order_no: str,
        data: dict[str, Any],
        *,
        status_code: int | None,
        response_text: str,
        request_url: str,
        request_payload: dict[str, Any] | None,
    ) -> None:
        result_status = data.get("ResultStatus")
        output_log = result_status.get("OutputLog") if isinstance(result_status, dict) else None
        self.logger.error("売上伝票OLAP取得失敗詳細: order_no=%s http_status_code=%s", order_no, status_code)
        self.logger.error("売上伝票OLAP取得失敗 MessageName: %s", result_status.get("MessageName") if isinstance(result_status, dict) else None)
        self.logger.error("売上伝票OLAP取得失敗レスポンス本文先頭: order_no=%s body=%s", order_no, response_text[:3000])
        self.logger.error("売上伝票OLAP取得失敗 ResultStatus: %s", _format_json_for_log(result_status))
        self.logger.error("売上伝票OLAP取得失敗 ResultStatus.OutputLog: %s", _format_json_for_log(output_log))
        self.logger.error(
            "売上伝票OLAP取得失敗 MessageFirst.Items: %s",
            _format_json_for_log(_message_items(output_log, "MessageFirst")),
        )
        self.logger.error(
            "売上伝票OLAP取得失敗 MessageMiddle.Items: %s",
            _format_json_for_log(_message_items(output_log, "MessageMiddle")),
        )
        self.logger.error(
            "売上伝票OLAP取得失敗 MessageLast.Items: %s",
            _format_json_for_log(_message_items(output_log, "MessageLast")),
        )
        self.logger.error("売上伝票OLAP取得失敗 PropertyStatuses: %s", _format_json_for_log(data.get("PropertyStatuses")))
        self.logger.error("売上伝票OLAP取得失敗 リクエストURL: %s", request_url)
        if request_payload is not None:
            self.logger.error(
                "売上伝票OLAP取得失敗 リクエスト検索条件: %s",
                _format_json_for_log(_request_conditions(request_payload)),
            )

    def _has_auth_cookie(self) -> bool:
        return self._session is not None and any(cookie.name == ".ASPXAUTH" for cookie in self._session.cookies)

    def _endpoint(self, path: str) -> str:
        return self.config.tks_base_url.rstrip("/") + quote(path, safe="/")


def _build_voucher_payload(
    order_no: str,
    *,
    disable_op_fields: bool | None = None,
    enabled_op_fields: list[str] | None = None,
) -> tuple[OrderedDict[str, Any], Path]:
    path = resource_path("templates/voucher_olap_request.json")
    with path.open("r", encoding="utf-8-sig") as fp:
        payload = json.load(fp, object_pairs_hook=OrderedDict)
    payload = copy.deepcopy(payload)
    for condition in payload.get("R2List", []):
        if isinstance(condition, dict) and condition.get("フィールド論理名") == "受注No":
            condition["OLAP値"] = order_no
        if isinstance(condition, dict) and condition.get("フィールド論理名") == "有効区分":
            condition["OLAP値"] = "1"
    _remove_blank_sales_month_condition(payload)
    if enabled_op_fields:
        _keep_only_enabled_op_columns(payload, enabled_op_fields)
    elif _disable_op_fields_for_debug(default=bool(disable_op_fields)):
        _remove_op_related_columns(payload)
    _remove_calc_op_columns(payload)
    return payload, path


def _extract_voucher_rows_or_raise(data: object, *, logger: logging.Logger | None = None) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        raise OlapFetchError("OLAPレスポンスがJSONオブジェクトではありません")
    if "ResponseData" not in data:
        raise OlapFetchError(_olap_error_message(data))
    response_data = data.get("ResponseData") or {}
    if not isinstance(response_data, dict):
        raise OlapFetchError("OLAPレスポンスのResponseDataがJSONオブジェクトではありません")
    if "R1List" not in response_data:
        raise OlapFetchError(_olap_error_message(data) or "OLAPレスポンスにR1Listがありません")
    olap_message = _olap_error_message(data)
    if olap_message:
        raise OlapFetchError(olap_message)
    return extract_r1_rows(data, logger=logger)


def _olap_error_message(data: dict[str, Any]) -> str:
    messages: list[str] = []
    result_status = data.get("ResultStatus")
    if isinstance(result_status, dict):
        output_log = result_status.get("OutputLog")
        for key in ("MessageFirst", "MessageMiddle", "MessageLast"):
            for item in _message_items(output_log, key):
                text = _message_item_text(item)
                if text and text not in messages:
                    messages.append(text)
        for key in ("MessageName", "PropertyName"):
            value = result_status.get(key)
            if value not in (None, "", 0, "0") and str(value) not in messages:
                messages.append(f"{key}: {value}")
    for status in data.get("PropertyStatuses") or []:
        text = _message_item_text(status)
        if text and text not in messages:
            messages.append(text)
    return "\n".join(messages)


def _message_items(output_log: object, key: str) -> list[object]:
    if not isinstance(output_log, dict):
        return []
    message = output_log.get(key)
    if not isinstance(message, dict):
        return []
    items = message.get("Items")
    return items if isinstance(items, list) else []


def _message_item_text(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        parts: list[str] = []
        for key in ("Message", "Text", "Value", "Name", "PropertyName", "MessageName"):
            value = item.get(key)
            if value not in (None, ""):
                parts.append(str(value))
        if parts:
            return " ".join(parts).strip()
        return _format_json_for_log(item)
    return "" if item is None else str(item).strip()


def _request_conditions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for condition in payload.get("R2List", []):
        if not isinstance(condition, dict):
            continue
        conditions.append(
            {
                "OLAP表示No": condition.get("OLAP表示No"),
                "エンティティ論理名": condition.get("エンティティ論理名"),
                "フィールド論理名": condition.get("フィールド論理名"),
                "OLAP値": condition.get("OLAP値"),
                "OLAP条件グループ": condition.get("OLAP条件グループ"),
                "XupperRoutingItems": condition.get("XupperRoutingItems"),
            }
        )
    return conditions


def _column_display_names(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "OLAP表示No": column.get("OLAP表示No"),
            "OLAP表示名": column.get("OLAP表示名"),
            "エンティティ論理名": column.get("エンティティ論理名"),
            "フィールド論理名": column.get("フィールド論理名"),
        }
        for column in columns
    ]


def _disable_op_fields_for_debug(*, default: bool = True) -> bool:
    value = os.environ.get("TKS_VOUCHER_OLAP_DISABLE_OP_FIELDS")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _remove_op_related_columns(payload: dict[str, Any]) -> None:
    columns = payload.get("R1List")
    if isinstance(columns, list):
        payload["R1List"] = [
            column
            for column in columns
            if not (isinstance(column, dict) and column.get("OLAP表示名") in _OP_TOGGLEABLE_FIELD_NAMES)
        ]


def _remove_calc_op_columns(payload: dict[str, Any]) -> None:
    columns = payload.get("R1List")
    if isinstance(columns, list):
        payload["R1List"] = [
            column
            for column in columns
            if not (isinstance(column, dict) and column.get("OLAP表示名") in _OP_CALC_FIELD_NAMES)
        ]


def _keep_only_enabled_op_columns(payload: dict[str, Any], enabled_names: list[str]) -> None:
    enabled = {name.strip() for name in enabled_names if name.strip()}
    columns = payload.get("R1List")
    if isinstance(columns, list):
        payload["R1List"] = [
            column
            for column in columns
            if not (
                isinstance(column, dict)
                and column.get("OLAP表示名") in _OP_TOGGLEABLE_FIELD_NAMES
                and column.get("OLAP表示名") not in enabled
            )
        ]


def _remove_blank_sales_month_condition(payload: dict[str, Any]) -> None:
    conditions = payload.get("R2List")
    if isinstance(conditions, list):
        payload["R2List"] = [
            condition
            for condition in conditions
            if not (
                isinstance(condition, dict)
                and condition.get("フィールド論理名") == "売上計上月度"
                and not str(condition.get("OLAP値") or "").strip()
            )
        ]


def _enabled_op_field_names(payload: dict[str, Any]) -> list[str]:
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return []
    return [
        str(column.get("OLAP表示名"))
        for column in columns
        if isinstance(column, dict) and column.get("OLAP表示名") in _OP_TOGGLEABLE_FIELD_NAMES
    ]


def _message_name(data: object) -> object:
    if not isinstance(data, dict):
        return None
    result_status = data.get("ResultStatus")
    if not isinstance(result_status, dict):
        return None
    return result_status.get("MessageName")


def _response_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return ""


def _format_json_for_log(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError:
        return str(value)


def _load_mock_response() -> object:
    path = Path(__file__).resolve().parents[1] / "docs" / "olap2" / "04_データ取得_レスポンス.txt"
    text = path.read_text(encoding="utf-8")
    body = text.split("\n\n", 1)[1].strip()
    return json.loads(body)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _screen_name_value(value: str) -> int | str:
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else stripped
