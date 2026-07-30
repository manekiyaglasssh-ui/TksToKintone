from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path

from app.models import AppConfig, AppPaths
from app.voucher_olap_service import (
    OlapFetchError,
    OlapNoDataError,
    VoucherOlapService,
    _enabled_op_field_names,
    _message_items,
    _olap_error_message,
    _request_conditions,
)


class _FakeResponse:
    def __init__(self, data: dict, *, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def put(self, *args, **kwargs) -> _FakeResponse:
        return self.response


class VoucherOlapServiceTest(unittest.TestCase):
    def test_missing_response_data_is_fetch_error_not_no_data(self) -> None:
        service, log_stream = _service_with_response(
            {
                "PropertyStatuses": [],
                "ResultStatus": {
                    "OutputLog": {
                        "MessageFirst": {"Items": ["指定された項目が存在しません: 02時平米"]},
                        "MessageMiddle": {"Items": []},
                        "MessageLast": {"Items": []},
                    }
                },
            }
        )

        with self.assertRaises(OlapFetchError) as ctx:
            service.fetch_voucher_rows("1405113")

        self.assertNotIsInstance(ctx.exception, OlapNoDataError)
        self.assertIn("OLAPデータ取得に失敗しました。", str(ctx.exception))
        self.assertIn("指定された項目が存在しません: 02時平米", str(ctx.exception))
        logs = log_stream.getvalue()
        self.assertIn("http_status_code=200", logs)
        self.assertIn("ResultStatus.OutputLog", logs)
        self.assertIn("MessageFirst.Items", logs)
        self.assertIn("PropertyStatuses", logs)
        self.assertIn("受注No", logs)
        self.assertIn("1405113", logs)
        self.assertIn("有効区分", logs)

    def test_empty_r1_list_is_no_data(self) -> None:
        service, _ = _service_with_response({"ResponseData": {"R1List": {}}})

        with self.assertRaises(OlapNoDataError) as ctx:
            service.fetch_voucher_rows("1405113")

        self.assertEqual(str(ctx.exception), "対象データが見つかりません。\n伝票番号を確認してください。")

    def test_olap_message_with_r1_list_is_fetch_error(self) -> None:
        service, _ = _service_with_response(
            {
                "ResultStatus": {
                    "OutputLog": {
                        "MessageFirst": {"Items": ["指定された項目が存在しません: 02時平米"]},
                        "MessageMiddle": {"Items": []},
                        "MessageLast": {"Items": []},
                    }
                },
                "ResponseData": {"R1List": {}},
            }
        )

        with self.assertRaises(OlapFetchError) as ctx:
            service.fetch_voucher_rows("1405113")

        self.assertIn("OLAPデータ取得に失敗しました。", str(ctx.exception))
        self.assertIn("指定された項目が存在しません: 02時平米", str(ctx.exception))

    def test_output_log_items_can_be_extracted_for_logging(self) -> None:
        output_log = {
            "MessageFirst": {"Items": ["first"]},
            "MessageMiddle": {"Items": [{"Message": "middle"}]},
            "MessageLast": {"Items": ["last"]},
        }

        self.assertEqual(_message_items(output_log, "MessageFirst"), ["first"])
        self.assertEqual(_message_items(output_log, "MessageMiddle"), [{"Message": "middle"}])
        self.assertEqual(_message_items(output_log, "MessageLast"), ["last"])

    def test_olap_error_message_is_in_display_message(self) -> None:
        data = {
            "ResultStatus": {
                "OutputLog": {
                    "MessageFirst": {"Items": [{"Message": "指定された項目が存在しません: 02時平米"}]},
                    "MessageMiddle": {"Items": []},
                    "MessageLast": {"Items": []},
                }
            }
        }

        message = _olap_error_message(data)
        self.assertIn("指定された項目が存在しません: 02時平米", message)
        self.assertIn(message, str(OlapFetchError(message)))

    def test_request_conditions_include_order_no_and_valid_flag(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload("1405113")
        conditions = _request_conditions(payload)

        self.assertIn(
            {"field": "受注No", "value": "1405113"},
            [{"field": condition["フィールド論理名"], "value": condition["OLAP値"]} for condition in conditions],
        )
        self.assertIn(
            {"field": "有効区分", "value": "1"},
            [{"field": condition["フィールド論理名"], "value": condition["OLAP値"]} for condition in conditions],
        )

    def test_quantity_unit_code_column_included_in_request(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload("1405113")
        names = [
            column.get("フィールド論理名")
            for column in payload["R1List"]
            if isinstance(column, dict)
        ]
        self.assertIn("数量単位コード", names)

    def test_quantity_unit_code_column_added_when_missing_from_template(self) -> None:
        from app.voucher_olap_service import _ensure_quantity_unit_code_column

        payload = {
            "OLAP対象データ": "OLAP_T01-03 受注入力明細データ",
            "R1List": [
                {
                    "OLAP表示No": 6,
                    "OLAP表示名": "受注No",
                    "フィールド論理名": "受注No",
                    "エンティティ論理名": "OLAP_T01-03 受注入力明細データ",
                }
            ],
        }
        _ensure_quantity_unit_code_column(payload)
        added = payload["R1List"][-1]
        self.assertEqual(added["フィールド論理名"], "数量単位コード")
        self.assertEqual(added["エンティティ論理名"], "OLAP_T01-03 受注入力明細データ")
        # 既に列がある場合は重複追加しない。
        _ensure_quantity_unit_code_column(payload)
        self.assertEqual(
            sum(
                1
                for column in payload["R1List"]
                if column.get("フィールド論理名") == "数量単位コード"
            ),
            1,
        )

    def test_delivery_course_name_column_included_in_request(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload("1405113")
        code_column = next(
            item for item in payload["R1List"]
            if item.get("OLAP表示名") == "配送コース"
        )
        name_column = next(
            item for item in payload["R1List"]
            if item.get("OLAP表示名") == "配送コース名称"
        )
        self.assertEqual(code_column["OLAP表示No"], 48)
        self.assertEqual(name_column["OLAP表示No"], 49)
        for column, logical_name in ((code_column, "配送コース"), (name_column, "配送コース名称")):
            self.assertEqual(column["エンティティ論理名"], "OLAP_M01-19 営業所別配送コースマスタ")
            self.assertEqual(column["フィールド論理名"], logical_name)
            self.assertEqual(column["XupperRoutingItems"][0]["フィールド論理名"], "営業所配送コース")

    def test_delivery_course_name_column_is_added_without_fixed_display_no(self) -> None:
        from app.voucher_olap_service import _ensure_delivery_course_name_column

        payload = {
            "OLAP対象データ": "OLAP_T01-03 受注入力明細データ",
            "R1List": [{
                "OLAP表示No": 72,
                "OLAP表示名": "受注No",
                "フィールド論理名": "受注No",
                "エンティティ論理名": "OLAP_T01-03 受注入力明細データ",
            }],
        }
        _ensure_delivery_course_name_column(payload)
        code, name = payload["R1List"][-2:]
        self.assertEqual((code["OLAP表示No"], name["OLAP表示No"]), (73, 74))
        self.assertEqual((code["フィールド論理名"], name["フィールド論理名"]), ("配送コース", "配送コース名称"))
        _ensure_delivery_course_name_column(payload)
        self.assertEqual(len(payload["R1List"]), 3)

    def test_delivery_course_name_column_is_added_as_49_after_old_template(self) -> None:
        from app.voucher_olap_service import _ensure_delivery_course_name_column

        payload = {
            "OLAP対象データ": "OLAP_T01-03 受注入力明細データ",
            "R1List": [{
                "OLAP表示No": 48,
                "OLAP表示名": "旧テンプレート最終列",
                "フィールド論理名": "旧テンプレート最終列",
                "エンティティ論理名": "OLAP_T01-03 受注入力明細データ",
            }],
        }
        _ensure_delivery_course_name_column(payload)
        self.assertEqual(
            [item["OLAP表示No"] for item in payload["R1List"][-2:]],
            [49, 50],
        )

    def test_request_and_response_delivery_course_diagnostics_have_actual_values(self) -> None:
        response = {"ResponseData": {"R1List": [{
            "6": "1405113",
            "9": "Z001",
            "16": "商品A",
            "48": "01",
            "49": "パレト",
        }]}}
        service, log_stream = _service_with_response(response)

        rows = service.fetch_voucher_rows("1405113")

        self.assertEqual(rows[0]["delivery_course_code"], "01")
        self.assertEqual(rows[0]["delivery_course_name"], "パレト")
        logs = log_stream.getvalue()
        self.assertIn("voucher_delivery_course_request_column", logs)
        self.assertIn("display_no=48", logs)
        self.assertIn("logical_name=配送コース名称", logs)
        self.assertIn("voucher_delivery_course_code_parsed", logs)
        self.assertIn("voucher_delivery_course_name_parsed", logs)
        self.assertIn("response_key=49", logs)
        self.assertIn("voucher_no=Z001", logs)
        self.assertIn("パレト", logs)

    def test_blank_sales_month_condition_is_removed(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload("1405113")
        conditions = _request_conditions(payload)

        self.assertNotIn("売上計上月度", [condition["フィールド論理名"] for condition in conditions])

    def test_disable_op_fields_removes_op_related_columns(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        full_payload, _ = _build_voucher_payload("1405113", disable_op_fields=False)
        disabled_payload, _ = _build_voucher_payload("1405113", disable_op_fields=True)

        # calc列3つは常にOLAPリクエストから除外される
        self.assertEqual(
            _enabled_op_field_names(full_payload),
            ["OP区分", "商品コード"],
        )
        self.assertEqual(_enabled_op_field_names(disabled_payload), [])
        # full_payload はOP区分・商品コードあり、disabled_payload はどちらもなし
        self.assertEqual(len(disabled_payload["R1List"]), len(full_payload["R1List"]) - 2)

    def test_calc_op_columns_always_excluded_from_olap_request(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        for kwargs in [
            {"disable_op_fields": False},
            {"disable_op_fields": True},
            {"enabled_op_fields": ["OP区分", "商品コード"]},
            {"enabled_op_fields": []},
        ]:
            payload, _ = _build_voucher_payload("1405113", **kwargs)
            column_names = [
                col.get("OLAP表示名")
                for col in payload["R1List"]
                if isinstance(col, dict)
            ]
            for calc_field in ("00時ケース・ロット平米", "02時平米", "02時総平米"):
                self.assertNotIn(
                    calc_field,
                    column_names,
                    msg=f"{calc_field} should not be in OLAP request with kwargs={kwargs}",
                )

    def test_enabled_op_fields_can_be_limited_by_setting(self) -> None:
        from app.voucher_olap_service import _build_voucher_payload

        payload, _ = _build_voucher_payload(
            "1405113",
            disable_op_fields=True,
            enabled_op_fields=["OP区分", "商品コード"],
        )

        self.assertEqual(_enabled_op_field_names(payload), ["OP区分", "商品コード"])

    def test_missing_response_data_with_message_name_only_is_fetch_error(self) -> None:
        service, _ = _service_with_response(
            {
                "PropertyStatuses": [],
                "ResultStatus": {
                    "MessageName": 6917529027641081863,
                    "OutputLog": {
                        "MessageFirst": {"Items": []},
                        "MessageMiddle": {"Items": []},
                        "MessageLast": {"Items": []},
                    },
                },
            }
        )

        with self.assertRaises(OlapFetchError) as ctx:
            service.fetch_voucher_rows("1405113")

        self.assertIn("OLAPデータ取得に失敗しました。", str(ctx.exception))
        self.assertIn("MessageName: 6917529027641081863", str(ctx.exception))


def _service_with_response(data: dict) -> tuple[VoucherOlapService, io.StringIO]:
    config = _config()
    log_stream = io.StringIO()
    logger = logging.getLogger(f"test_voucher_olap_service_{id(data)}")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(log_stream)
    logger.addHandler(handler)
    service = VoucherOlapService(config, logger)
    service._session = _FakeSession(_FakeResponse(data))
    return service, log_stream


def _config() -> AppConfig:
    base = Path(tempfile.gettempdir()) / "tks_to_kintone_test"
    paths = AppPaths(
        base_dir=base,
        config_env=base / "config.env",
        field_mapping_json=base / "field_mapping.json",
        work_dir=base / "work",
        log_dir=base / "logs",
        error_dir=base / "error",
    )
    return AppConfig(
        paths=paths,
        company_code="999",
        kintone_domain="example.cybozu.com",
        kintone_app_id="1",
        kintone_api_token="token",
        csv_encoding="utf-8",
        shukka_kbn_options=[],
        cleanup_retention_days=7,
        tks_client_mode="http",
        tks_base_url="https://example.test",
    )


if __name__ == "__main__":
    unittest.main()
