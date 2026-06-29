"""OLAPリクエスト送信直前の OP区分 自己修復テスト。

古いテンプレートに OP区分 が無くても、_build_olap_payload() 後の R1List には
必ず OP区分 が含まれることを検証する。インストーラ側で古いテンプレートが残って
しまっても、送信されるリクエストには OP区分 が入る安全網。
"""
from __future__ import annotations

import json
import logging
import unittest
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import AppConfig, AppPaths
from app.tks_client import HttpTksClient, _ensure_op_kubun_in_r1list


def _op_kubun_count(r1_list: list[dict]) -> int:
    return sum(
        1
        for item in r1_list
        if isinstance(item, dict)
        and (item.get("OLAP表示名") == "OP区分" or item.get("フィールド論理名") == "OP区分")
    )


def _make_template(path: Path, *, with_op_kubun: bool) -> None:
    r1_list = [
        {"OLAP表示No": 1, "OLAP表示名": "受注No", "フィールド論理名": "受注No"},
        {"OLAP表示No": 2, "OLAP表示名": "受注行No", "フィールド論理名": "受注行No"},
    ]
    if with_op_kubun:
        r1_list.append(
            {"OLAP表示No": 34, "OLAP表示名": "OP区分", "フィールド論理名": "OP区分"}
        )
    payload = {
        "R1List": r1_list,
        "R2List": [
            {"フィールド論理名": "受注No", "OLAP値": ""},
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_config(tmp: Path, kakou_template: Path, soba_template: Path) -> AppConfig:
    paths = AppPaths(
        base_dir=tmp,
        config_env=tmp / "config.env",
        field_mapping_json=tmp / "field_mapping.json",
        work_dir=tmp / "work",
        log_dir=tmp / "logs",
        error_dir=tmp / "error",
    )
    return AppConfig(
        paths=paths,
        company_code="0001",
        kintone_domain="example.cybozu.com",
        kintone_app_id="1",
        kintone_api_token="token",
        csv_encoding="cp932",
        shukka_kbn_options=["0"],
        cleanup_retention_days=30,
        tks_client_mode="http",
        tks_kakou_request_template=kakou_template,
        tks_soba_request_template=soba_template,
    )


class OlapSelfRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.logger = logging.getLogger("test_olap_self_repair")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self, *, kakou_op: bool, soba_op: bool) -> HttpTksClient:
        kakou = self.tmp / "kakou_request_template.json"
        soba = self.tmp / "soba_request_template.json"
        _make_template(kakou, with_op_kubun=kakou_op)
        _make_template(soba, with_op_kubun=soba_op)
        config = _make_config(self.tmp, kakou, soba)
        return HttpTksClient(config, self.logger)

    def test_kakou_without_op_kubun_is_repaired(self) -> None:
        client = self._client(kakou_op=False, soba_op=True)
        payload = client._build_olap_payload("kakou", ["1386655"])
        self.assertEqual(_op_kubun_count(payload["R1List"]), 1)

    def test_soba_without_op_kubun_is_repaired(self) -> None:
        client = self._client(kakou_op=True, soba_op=False)
        payload = client._build_olap_payload("soba", ["1386655"])
        self.assertEqual(_op_kubun_count(payload["R1List"]), 1)

    def test_existing_op_kubun_is_not_duplicated(self) -> None:
        client = self._client(kakou_op=True, soba_op=True)
        payload = client._build_olap_payload("kakou", ["1386655"])
        self.assertEqual(_op_kubun_count(payload["R1List"]), 1)

    def test_repaired_item_has_expected_fields(self) -> None:
        payload = OrderedDict([("R1List", [])])
        repaired = _ensure_op_kubun_in_r1list(payload)
        self.assertTrue(repaired)
        item = payload["R1List"][0]
        self.assertEqual(item["OLAP表示No"], 34)
        self.assertEqual(item["OLAP表示名"], "OP区分")
        self.assertEqual(item["フィールド論理名"], "OP区分")

    def test_ensure_returns_false_when_already_present(self) -> None:
        payload = {"R1List": [{"OLAP表示名": "OP区分", "フィールド論理名": "OP区分"}]}
        self.assertFalse(_ensure_op_kubun_in_r1list(payload))

    def test_r2_override_still_applies_after_repair(self) -> None:
        kakou = self.tmp / "kakou_request_template.json"
        soba = self.tmp / "soba_request_template.json"
        _make_template(kakou, with_op_kubun=False)
        _make_template(soba, with_op_kubun=True)
        config = _make_config(self.tmp, kakou, soba)
        object.__setattr__(
            config, "tks_kakou_r2_overrides", {"受注No": {"演算式": "上書き"}}
        )
        client = HttpTksClient(config, self.logger)
        payload = client._build_olap_payload("kakou", ["1386655", "1386721"])
        # OP区分補完とR2オーバーライドが両立する。
        self.assertEqual(_op_kubun_count(payload["R1List"]), 1)
        order_condition = next(
            c for c in payload["R2List"] if c.get("フィールド論理名") == "受注No"
        )
        self.assertEqual(order_condition["演算式"], "上書き")
        # 受注No差し替えも従来どおり動く。
        self.assertEqual(order_condition["OLAP値"], "1386655,1386721")

    def test_bundled_templates_contain_op_kubun(self) -> None:
        # 同梱テンプレート自体にも OP区分 が含まれていること（最新版同梱の確認）。
        for name in ("kakou_request_template.json", "soba_request_template.json"):
            path = Path("docs/olap") / name
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            self.assertEqual(_op_kubun_count(data["R1List"]), 1, name)


if __name__ == "__main__":
    unittest.main()
