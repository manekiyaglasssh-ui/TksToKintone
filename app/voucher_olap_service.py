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
        # 得意先コード単位の取引区分キャッシュ（要件7: 同一コードの重複問い合わせ防止）。
        self._transaction_type_cache: dict[str, str] = {}
        # 得意先マスタ（納品書単価・金額上段/下段（硝子））のキャッシュ。取引区分と同一の
        # OLAPリクエストで取得するため、コード単位で dict をまとめて保持する。
        self._customer_master_cache: dict[str, dict[str, str]] = {}

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
            payload, _ = _build_voucher_payload(order_no)
            self._log_response_diagnostics(order_no, data, request_executed=True)
            rows = extract_r1_rows(
                data,
                logger=self.logger,
                request_columns=_request_columns(payload),
            )
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
        rows = _extract_voucher_rows_or_raise(
            data,
            logger=self.logger,
            request_columns=_request_columns(payload),
        )
        if not rows:
            raise OlapNoDataError()
        self.last_response_r1_count += len(rows)
        self.logger.info("売上伝票OLAP取得完了: order_no=%s rows=%s", order_no, len(rows))
        if rows:
            unit_codes = [str(row.get("quantity_unit_code") or "").strip() for row in rows]
            self.logger.info(
                "voucher_olap_quantity_unit_code_parsed: order_no=%s codes=%s hidden19=%s",
                order_no,
                [code for code in unit_codes if code][:20],
                sum(1 for code in unit_codes if code == "19"),
            )
            course_codes = [str(row.get("delivery_course_code") or "").strip() for row in rows]
            course_names = [str(row.get("delivery_course_name") or "").strip() for row in rows]
            self.logger.info(
                "voucher_delivery_course_parsed: order_no=%s codes=%s names=%s",
                order_no,
                [value for value in course_codes if value][:20],
                [value for value in course_names if value][:20],
            )
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
        self._enrich_transaction_types(rows)
        return rows

    def _enrich_transaction_types(self, rows: list[dict[str, str]]) -> None:
        """各行の得意先コードから得意先マスタ項目を取得して行に保持する。

        取得するのは取引区分（移動伝票=8 判定用）と
        「納品書単価・金額上段（硝子）」「納品書単価・金額下段（硝子）」。
        得意先コードが空の場合や取得失敗時は空扱い（既存の伝票作成は止めない）。
        得意先コード単位でキャッシュし、同一コードの問い合わせを繰り返さない（要件7）。
        """
        for row in rows:
            customer_code = (row.get("customer_code") or row.get("4") or "").strip()
            master = self.fetch_customer_master_by_customer_code(customer_code)
            row["transaction_type"] = master.get("transaction_type", "")
            row["invoice_price_amount_upper_glass"] = master.get(
                "invoice_price_amount_upper_glass", ""
            )
            row["invoice_price_amount_upper_glass_raw"] = master.get(
                "invoice_price_amount_upper_glass", ""
            )
            row["invoice_price_amount_lower_glass"] = master.get(
                "invoice_price_amount_lower_glass", ""
            )
            row["invoice_price_amount_lower_glass_raw"] = master.get(
                "invoice_price_amount_lower_glass", ""
            )

    def fetch_transaction_type_by_customer_code(self, customer_code: str) -> str:
        """得意先コードに紐づく取引区分を別テーブル（得意先マスタ）から取得する。

        得意先コードが空なら空文字を返す。取得できなかった場合も空扱いとし、
        例外は送出せずログに原因を残す（要件5/6）。同一コードはキャッシュする（要件7）。
        """
        return self.fetch_customer_master_by_customer_code(customer_code).get(
            "transaction_type", ""
        )

    def fetch_customer_master_by_customer_code(self, customer_code: str) -> dict[str, str]:
        """得意先コードに紐づく得意先マスタ項目をまとめて取得する。

        取引区分と「納品書単価・金額上段（硝子）」「納品書単価・金額下段（硝子）」を
        1回のOLAPリクエストで取得する。
        得意先コードが空なら空値のdictを返す。取得失敗時も空扱いとし、例外は送出せず
        ログに原因を残す（要件5/6）。同一コードはキャッシュする（要件7）。
        """
        code = (customer_code or "").strip()
        empty = {
            "transaction_type": "",
            "invoice_price_amount_upper_glass": "",
            "invoice_price_amount_lower_glass": "",
        }
        if not code:
            return dict(empty)
        if code in self._customer_master_cache:
            return dict(self._customer_master_cache[code])

        result = dict(empty)
        self.logger.info("customer_master_fetch_started: customer_code=%s", code)
        try:
            if self.config.tks_client_mode == "mock":
                pass
            elif self._session is None:
                self.logger.warning(
                    "得意先マスタOLAP取得スキップ: requests未導入のためsession無し customer_code=%s",
                    code,
                )
            else:
                payload = build_transaction_type_payload(code)
                url = self._endpoint(OLAP_DATA_PATH)
                self.logger.info("得意先マスタOLAP取得リクエスト実行: customer_code=%s", code)
                response = self._session.put(
                    url,
                    data=_json_bytes(payload),
                    headers=OLAP_HEADERS,
                    timeout=60,
                )
                response.raise_for_status()
                data = response.json()
                result["transaction_type"] = parse_transaction_type(data)
                result["invoice_price_amount_upper_glass"] = (
                    parse_invoice_price_amount_upper_glass(data)
                )
                result["invoice_price_amount_lower_glass"] = (
                    parse_invoice_price_amount_lower_glass(data)
                )
                self.logger.info(
                    "取引区分OLAP取得完了: customer_code=%s transaction_type=%s",
                    code,
                    result["transaction_type"] or "(なし)",
                )
                self.logger.info(
                    "customer_master_field_loaded: customer_code=%s "
                    "invoice_price_amount_upper_glass=%s "
                    "invoice_price_amount_lower_glass=%s",
                    code,
                    result["invoice_price_amount_upper_glass"] or "(なし)",
                    result["invoice_price_amount_lower_glass"] or "(なし)",
                )
        except Exception:
            self.logger.exception(
                "customer_master_fetch_failed（伝票作成は継続します）: customer_code=%s", code
            )
            result = dict(empty)

        self._customer_master_cache[code] = dict(result)
        # 取引区分キャッシュも整合させておく（後方互換のため保持する）。
        self._transaction_type_cache[code] = result["transaction_type"]
        return dict(result)

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
        for course_column in _delivery_course_request_columns(payload):
            self.logger.info(
                "voucher_delivery_course_request_column "
                "order_no=%s voucher_no=%s display_no=%s display_name=%s "
                "logical_name=%s entity=%s routing_field=%s",
                order_no,
                "(response_pending)",
                course_column.get("OLAP表示No", "(not_found)"),
                course_column.get("OLAP表示名", "(not_found)"),
                course_column.get("フィールド論理名", "(not_found)"),
                course_column.get("エンティティ論理名", "(not_found)"),
                _course_routing_field(course_column) or "(not_found)",
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
    _ensure_customer_order_no_column(payload)
    _ensure_delivery_address2_column(payload)
    _ensure_quantity_unit_code_column(payload)
    _ensure_delivery_course_columns(payload)
    if enabled_op_fields:
        _keep_only_enabled_op_columns(payload, enabled_op_fields)
    elif _disable_op_fields_for_debug(default=bool(disable_op_fields)):
        _remove_op_related_columns(payload)
    _remove_calc_op_columns(payload)
    return payload, path


CUSTOMER_ORDER_NO_FIELD = "客先注文No_10桁"
DELIVERY_ADDRESS2_FIELD = "納入先住所2"
QUANTITY_UNIT_CODE_FIELD = "数量単位コード"
DELIVERY_COURSE_ENTITY = "OLAP_M01-19 営業所別配送コースマスタ"
DELIVERY_COURSE_CODE_DISPLAY_NAME = "配送コース"
DELIVERY_COURSE_CODE_FIELD = "配送コース"
DELIVERY_COURSE_NAME_DISPLAY_NAME = "配送コース名称"
DELIVERY_COURSE_NAME_FIELD = "配送コース名称"
DELIVERY_COURSE_ROUTING_FIELD = "営業所配送コース"

# 取引区分取得用（別テーブル: 得意先マスタ）。
TRANSACTION_TYPE_TARGET = "OLAP_M05-01 得意先マスタ"
TRANSACTION_TYPE_CUSTOMER_CODE_FIELD = "得意先コード"
TRANSACTION_TYPE_FIELD = "取引区分"
# 得意先マスタの「納品書単価・金額上段/下段（硝子）」フィールド。
# 下段表示判定に使うのは下段フィールドのみ。上段フィールドは取得・ログ用に保持する。
INVOICE_PRICE_AMOUNT_UPPER_GLASS_FIELD = "納品書単価・金額上段（硝子）"
INVOICE_PRICE_AMOUNT_LOWER_GLASS_FIELD = "納品書単価・金額下段（硝子）"


def _transaction_type_column(
    display_no: int, field_name: str, width: int, domain: str
) -> "OrderedDict[str, Any]":
    return OrderedDict(
        [
            ("OLAP表示No", display_no),
            ("OLAP表示名", field_name),
            ("OLAPデータ区分", "1"),
            ("エンティティ論理名", TRANSACTION_TYPE_TARGET),
            ("フィールド論理名", field_name),
            ("OLAP表示幅", width),
            ("OLAPフォントサイズ２", "0"),
            ("OLAP空白値表示", "-"),
            ("OLAP日付のフォーマットフラグ", "1"),
            ("OLAP数値の3桁区切りフラグ", "1"),
            ("OLAP桁数", 0),
            ("OLAP小数", 0),
            ("OLAP丸め", "0"),
            ("OLAP出力順序No", None),
            ("OLAP出力順", "2"),
            ("OLAP空白値を先頭表示フラグ", "0"),
            ("OLAP集計方法", "0"),
            ("OLAP合計表示フラグ", "0"),
            ("OLAP合計ラベル", "計"),
            ("OLAP合計ラベルのみ表示フラグ", None),
            ("OLAP重複を除くフラグ", "0"),
            ("OLAP演算式", None),
            ("OLAP演算式表記", ""),
            ("OLAPドメイン分類", domain),
            ("XupperRoutingItems", []),
        ]
    )


def build_transaction_type_payload(customer_code: str) -> "OrderedDict[str, Any]":
    """得意先コードを条件に取引区分を取得するOLAPリクエストを構築する。

    別テーブル（得意先マスタ）に対し、得意先コード一致条件で取引区分を取得する。
    docs/OLAPリクエストレスポンス_取引区分/得意先コード_取引区分.txt のサンプル準拠。
    """
    return OrderedDict(
        [
            ("OLAP出力レイアウト", "0"),
            ("OLAP対象データ", TRANSACTION_TYPE_TARGET),
            (
                "R1List",
                [
                    _transaction_type_column(1, TRANSACTION_TYPE_CUSTOMER_CODE_FIELD, 7, "0"),
                    _transaction_type_column(2, TRANSACTION_TYPE_FIELD, 3, "3"),
                    _transaction_type_column(
                        3, INVOICE_PRICE_AMOUNT_UPPER_GLASS_FIELD, 3, "3"
                    ),
                    _transaction_type_column(
                        4, INVOICE_PRICE_AMOUNT_LOWER_GLASS_FIELD, 3, "3"
                    ),
                ],
            ),
            (
                "R2List",
                [
                    OrderedDict(
                        [
                            ("OLAP表示No", 1),
                            ("OLAP一致指定フラグ", "1"),
                            ("OLAP一致指定", "0"),
                            ("OLAP除外指定フラグ", "0"),
                            ("OLAP値", str(customer_code or "").strip()),
                            ("OLAP範囲指定フラグ", "0"),
                            ("OLAP範囲_Fromフラグ", "1"),
                            ("OLAP範囲Val_From", ""),
                            ("OLAP範囲Sel_From", "0"),
                            ("OLAP範囲_Toフラグ", "1"),
                            ("OLAP範囲Val_To", ""),
                            ("OLAP範囲Sel_To", "0"),
                            ("OLAP月度指定フラグ", "0"),
                            ("OLAP月度指定", "0"),
                            ("OLAP条件グループ", "0"),
                            ("OLAP空白", "1"),
                            ("OLAPドメイン分類", "0"),
                            ("エンティティ論理名", TRANSACTION_TYPE_TARGET),
                            ("フィールド論理名", TRANSACTION_TYPE_CUSTOMER_CODE_FIELD),
                            ("XupperRoutingItems", []),
                        ]
                    )
                ],
            ),
            ("ScreenName", 0),
        ]
    )


def parse_transaction_type(data: object) -> str:
    """取引区分レスポンスから取引区分の値（表示No=2）を取り出す。

    取得できなければ空文字を返す。R1List は dict / list いずれの形でも対応する。
    """
    if not isinstance(data, dict):
        return ""
    response_data = data.get("ResponseData")
    if not isinstance(response_data, dict):
        return ""
    r1_list = response_data.get("R1List")
    rows: list[object] = []
    if isinstance(r1_list, dict):
        rows = [r1_list[key] for key in sorted(r1_list)]
    elif isinstance(r1_list, list):
        rows = list(r1_list)
    for row in rows:
        if isinstance(row, dict):
            value = row.get("2")
            if value not in (None, ""):
                return str(value).strip()
    return ""


def parse_invoice_price_amount_upper_glass(data: object) -> str:
    """得意先マスタレスポンスから「納品書単価・金額上段（硝子）」（表示No=3）を取り出す。

    取得できなければ空文字を返す。R1List は dict / list いずれの形でも対応する。
    """
    if not isinstance(data, dict):
        return ""
    response_data = data.get("ResponseData")
    if not isinstance(response_data, dict):
        return ""
    r1_list = response_data.get("R1List")
    rows: list[object] = []
    if isinstance(r1_list, dict):
        rows = [r1_list[key] for key in sorted(r1_list)]
    elif isinstance(r1_list, list):
        rows = list(r1_list)
    for row in rows:
        if isinstance(row, dict):
            value = row.get("3")
            if value not in (None, ""):
                return str(value).strip()
    return ""


def parse_invoice_price_amount_lower_glass(data: object) -> str:
    """得意先マスタレスポンスから「納品書単価・金額下段（硝子）」（表示No=4）を取り出す。

    取得できなければ空文字を返す。R1List は dict / list いずれの形でも対応する。
    """
    if not isinstance(data, dict):
        return ""
    response_data = data.get("ResponseData")
    if not isinstance(response_data, dict):
        return ""
    r1_list = response_data.get("R1List")
    rows: list[object] = []
    if isinstance(r1_list, dict):
        rows = [r1_list[key] for key in sorted(r1_list)]
    elif isinstance(r1_list, list):
        rows = list(r1_list)
    for row in rows:
        if isinstance(row, dict):
            value = row.get("4")
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _ensure_customer_order_no_column(payload: dict[str, Any]) -> None:
    """OLAP送信直前の補完処理。

    古いテンプレートに「客先注文No_10桁」列が無い場合でも、送信直前にR1Listへ
    追加してOLAPレスポンスに必ず含まれるようにする。
    """
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return
    for column in columns:
        if isinstance(column, dict) and (
            column.get("フィールド論理名") == CUSTOMER_ORDER_NO_FIELD
            or column.get("OLAP表示名") == CUSTOMER_ORDER_NO_FIELD
        ):
            return

    entity = ""
    for column in columns:
        if isinstance(column, dict) and column.get("フィールド論理名") == "受注No":
            entity = column.get("エンティティ論理名") or ""
            break
    if not entity:
        entity = str(payload.get("OLAP対象データ") or "")

    nos = [
        column.get("OLAP表示No")
        for column in columns
        if isinstance(column, dict) and isinstance(column.get("OLAP表示No"), int)
    ]
    new_no = (max(nos) + 1) if nos else 1

    columns.append(OrderedDict([
        ("OLAP表示No", new_no),
        ("OLAP表示名", CUSTOMER_ORDER_NO_FIELD),
        ("OLAPデータ区分", "1"),
        ("エンティティ論理名", entity),
        ("フィールド論理名", CUSTOMER_ORDER_NO_FIELD),
        ("OLAP表示幅", 10),
        ("OLAPフォントサイズ２", "0"),
        ("OLAP空白値表示", "-"),
        ("OLAP日付のフォーマットフラグ", "1"),
        ("OLAP数値の3桁区切りフラグ", "1"),
        ("OLAP桁数", 0),
        ("OLAP小数", 0),
        ("OLAP丸め", "0"),
        ("OLAP出力順序No", None),
        ("OLAP出力順", "2"),
        ("OLAP空白値を先頭表示フラグ", "0"),
        ("OLAP集計方法", "0"),
        ("OLAP合計表示フラグ", "0"),
        ("OLAP合計ラベル", "計"),
        ("OLAP合計ラベルのみ表示フラグ", ""),
        ("OLAP重複を除くフラグ", "0"),
        ("OLAP演算式", ""),
        ("OLAP演算式表記", ""),
        ("OLAPドメイン分類", "0"),
        ("XupperRoutingItems", []),
    ]))


def _ensure_delivery_address2_column(payload: dict[str, Any]) -> None:
    """OLAP送信直前の補完処理。

    古いテンプレートに「納入先住所2」列が無い場合でも、送信直前にR1Listへ
    追加してOLAPレスポンスに必ず含まれるようにする。
    既存の「納入先住所1」と同じエンティティ・命名に合わせる。
    """
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return
    for column in columns:
        if isinstance(column, dict) and (
            column.get("フィールド論理名") == DELIVERY_ADDRESS2_FIELD
            or column.get("OLAP表示名") == DELIVERY_ADDRESS2_FIELD
        ):
            return

    # エンティティ論理名は既存の「納入先住所1」列に合わせる。無ければ対象データを使う。
    entity = ""
    for column in columns:
        if isinstance(column, dict) and column.get("フィールド論理名") == "納入先住所1":
            entity = column.get("エンティティ論理名") or ""
            break
    if not entity:
        entity = str(payload.get("OLAP対象データ") or "")

    nos = [
        column.get("OLAP表示No")
        for column in columns
        if isinstance(column, dict) and isinstance(column.get("OLAP表示No"), int)
    ]
    new_no = (max(nos) + 1) if nos else 1

    columns.append(OrderedDict([
        ("OLAP表示No", new_no),
        ("OLAP表示名", DELIVERY_ADDRESS2_FIELD),
        ("OLAPデータ区分", "1"),
        ("エンティティ論理名", entity),
        ("フィールド論理名", DELIVERY_ADDRESS2_FIELD),
        ("OLAP表示幅", 40),
        ("OLAPフォントサイズ２", "0"),
        ("OLAP空白値表示", "-"),
        ("OLAP日付のフォーマットフラグ", "1"),
        ("OLAP数値の3桁区切りフラグ", "1"),
        ("OLAP桁数", 0),
        ("OLAP小数", 0),
        ("OLAP丸め", "0"),
        ("OLAP出力順序No", None),
        ("OLAP出力順", "2"),
        ("OLAP空白値を先頭表示フラグ", "0"),
        ("OLAP集計方法", "0"),
        ("OLAP合計表示フラグ", "0"),
        ("OLAP合計ラベル", "計"),
        ("OLAP合計ラベルのみ表示フラグ", ""),
        ("OLAP重複を除くフラグ", "0"),
        ("OLAP演算式", ""),
        ("OLAP演算式表記", ""),
        ("OLAPドメイン分類", "0"),
        ("XupperRoutingItems", []),
    ]))


def _ensure_quantity_unit_code_column(payload: dict[str, Any]) -> None:
    """OLAP送信直前の補完処理。

    古いテンプレートに「数量単位コード」列が無い場合でも、送信直前にR1Listへ
    追加してOLAPレスポンスに必ず含まれるようにする。数量単位コード="19" の明細で
    数量列を空欄にする判定に使う。エンティティは受注入力明細データに合わせる。
    """
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return
    for column in columns:
        if isinstance(column, dict) and (
            column.get("フィールド論理名") == QUANTITY_UNIT_CODE_FIELD
            or column.get("OLAP表示名") == QUANTITY_UNIT_CODE_FIELD
        ):
            return

    # エンティティ論理名は既存の「受注No」列（受注入力明細データ）に合わせる。
    entity = ""
    for column in columns:
        if isinstance(column, dict) and column.get("フィールド論理名") == "受注No":
            entity = column.get("エンティティ論理名") or ""
            break
    if not entity:
        entity = str(payload.get("OLAP対象データ") or "")

    nos = [
        column.get("OLAP表示No")
        for column in columns
        if isinstance(column, dict) and isinstance(column.get("OLAP表示No"), int)
    ]
    new_no = (max(nos) + 1) if nos else 1

    columns.append(OrderedDict([
        ("OLAP表示No", new_no),
        ("OLAP表示名", QUANTITY_UNIT_CODE_FIELD),
        ("OLAPデータ区分", "1"),
        ("エンティティ論理名", entity),
        ("フィールド論理名", QUANTITY_UNIT_CODE_FIELD),
        ("OLAP表示幅", 3),
        ("OLAPフォントサイズ２", "0"),
        ("OLAP空白値表示", "-"),
        ("OLAP日付のフォーマットフラグ", "1"),
        ("OLAP数値の3桁区切りフラグ", "1"),
        ("OLAP桁数", 0),
        ("OLAP小数", 0),
        ("OLAP丸め", "0"),
        ("OLAP出力順序No", None),
        ("OLAP出力順", "2"),
        ("OLAP空白値を先頭表示フラグ", "0"),
        ("OLAP集計方法", "0"),
        ("OLAP合計表示フラグ", "0"),
        ("OLAP合計ラベル", "計"),
        ("OLAP合計ラベルのみ表示フラグ", ""),
        ("OLAP重複を除くフラグ", "0"),
        ("OLAP演算式", ""),
        ("OLAP演算式表記", ""),
        ("OLAPドメイン分類", "0"),
        ("XupperRoutingItems", []),
    ]))
    logging.getLogger(__name__).info(
        "voucher_olap_quantity_unit_code_field_added: OLAP表示No=%s entity=%s",
        new_no,
        entity,
    )


def _ensure_delivery_course_columns(payload: dict[str, Any]) -> None:
    """配送コースのコード列と名称列を正しいマスタ参照定義で補完する。"""
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return
    # 旧誤定義（受注明細.配送コースを名称扱い）は削除する。正しいマスタ列は
    # エンティティ＋論理名で識別し、コードと名称を混同しない。
    columns[:] = [
        column for column in columns
        if not (
            isinstance(column, dict)
            and column.get("エンティティ論理名") == "OLAP_T01-03 受注入力明細データ"
            and column.get("フィールド論理名") == DELIVERY_COURSE_CODE_FIELD
        )
    ]
    for display_name, field_name, width in (
        (DELIVERY_COURSE_CODE_DISPLAY_NAME, DELIVERY_COURSE_CODE_FIELD, 6),
        (DELIVERY_COURSE_NAME_DISPLAY_NAME, DELIVERY_COURSE_NAME_FIELD, 30),
    ):
        if any(
            isinstance(column, dict)
            and column.get("エンティティ論理名") == DELIVERY_COURSE_ENTITY
            and column.get("フィールド論理名") == field_name
            for column in columns
        ):
            continue
        nos = [
            column.get("OLAP表示No") for column in columns
            if isinstance(column, dict) and isinstance(column.get("OLAP表示No"), int)
        ]
        new_no = (max(nos) + 1) if nos else 1
        columns.append(_delivery_course_column(new_no, display_name, field_name, width))
        logging.getLogger(__name__).info(
            "voucher_olap_delivery_course_field_added: display_no=%s "
            "display_name=%s entity=%s logical_name=%s routing_field=%s",
            new_no, display_name, DELIVERY_COURSE_ENTITY, field_name,
            DELIVERY_COURSE_ROUTING_FIELD,
        )


def _ensure_delivery_course_name_column(payload: dict[str, Any]) -> None:
    """旧内部API互換。コード列と名称列の両方を補完する。"""
    _ensure_delivery_course_columns(payload)


def _delivery_course_column(
    display_no: int, display_name: str, field_name: str, width: int
) -> OrderedDict[str, Any]:
    return OrderedDict({
        "OLAP表示No": display_no,
        "OLAP表示名": display_name,
        "OLAPデータ区分": "1",
        "エンティティ論理名": DELIVERY_COURSE_ENTITY,
        "フィールド論理名": field_name,
        "OLAP表示幅": width,
        "OLAPフォントサイズ２": "0",
        "OLAP空白値表示": "-",
        "OLAP日付のフォーマットフラグ": "1",
        "OLAP数値の3桁区切りフラグ": "1",
        "OLAP桁数": 0,
        "OLAP小数": 0,
        "OLAP丸め": "0",
        "OLAP出力順序No": None,
        "OLAP出力順": "2",
        "OLAP空白値を先頭表示フラグ": "0",
        "OLAP集計方法": "0",
        "OLAP合計表示フラグ": "0",
        "OLAP合計ラベル": "計",
        "OLAP合計ラベルのみ表示フラグ": "",
        "OLAP重複を除くフラグ": "0",
        "OLAP演算式": "",
        "OLAP演算式表記": "",
        "OLAPドメイン分類": "0",
        "XupperRoutingItems": [OrderedDict({
            "参照順": 1,
            "エンティティ論理名": "OLAP_T01-03 受注入力明細データ",
            "エンティティ表示名": "受注入力明細データ",
            "フィールド論理名": DELIVERY_COURSE_ROUTING_FIELD,
            "フィールド表示名": DELIVERY_COURSE_ROUTING_FIELD,
        })],
    })


def _extract_voucher_rows_or_raise(
    data: object,
    *,
    logger: logging.Logger | None = None,
    request_columns: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
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
    return extract_r1_rows(
        data, logger=logger, request_columns=request_columns
    )


def _request_columns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    columns = payload.get("R1List")
    if not isinstance(columns, list):
        return []
    return [column for column in columns if isinstance(column, dict)]


def _delivery_course_request_columns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for column in _request_columns(payload):
        if column.get("エンティティ論理名") == DELIVERY_COURSE_ENTITY and (
            column.get("フィールド論理名") in {
                DELIVERY_COURSE_CODE_FIELD, DELIVERY_COURSE_NAME_FIELD,
            }
        ):
            result.append(column)
    return result


def _delivery_course_request_column(payload: dict[str, Any]) -> dict[str, Any]:
    """旧内部API互換: 名称列を返す。"""
    return next((c for c in _delivery_course_request_columns(payload)
                 if c.get("フィールド論理名") == DELIVERY_COURSE_NAME_FIELD), {})


def _course_routing_field(column: dict[str, Any]) -> str:
    routing = column.get("XupperRoutingItems")
    if not isinstance(routing, list):
        return ""
    for item in routing:
        if isinstance(item, dict):
            return str(item.get("フィールド論理名") or "")
    return ""


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
