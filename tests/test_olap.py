from __future__ import annotations

from tks_to_kintone.olap import build_olap_body, parse_olap_rows


def test_build_olap_body_keeps_order_condition() -> None:
    body = build_olap_body("1386655,1386721")

    assert body["OLAP対象データ"] == "OLAP_T01-03 受注入力明細データ"
    assert body["R2List"][2]["フィールド論理名"] == "受注No"
    assert body["R2List"][2]["OLAP値"] == "1386655,1386721"


def test_parse_olap_rows_sorts_numeric_keys() -> None:
    rows = parse_olap_rows(
        {
            "2": {"1": "B", "2": "2", "3": "C2"},
            "1": {"1": "A", "2": "1", "3": "C1"},
        }
    )

    assert [row["受注No"] for row in rows] == ["A", "B"]
    assert rows[0]["得意先コード"] == "C1"
