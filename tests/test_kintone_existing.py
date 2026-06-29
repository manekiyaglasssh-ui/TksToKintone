"""Kintone既存データの検索・突合・登録前確認反映のテスト。"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.kintone_client import KintoneClient
from app.kintone_existing import (
    merge_existing_kintone_records_into_preview_rows,
    summarize_existing_reflection,
)
from app.models import AppConfig, AppPaths
from app.preview_state import PreviewState


# ── テスト用 KintoneClient ────────────────────────────────────────

_MAPPING_JSON = (
    '{"受注No":"受注No","検索キー":"検索キー","仕上日":"仕上日","出荷区分":"出荷区分",'
    '"加工名":"加工名","加工mm":"加工mm","㎡":"平方メートル","総㎡":"総平方メートル"}'
)


def _make_client(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text(_MAPPING_JSON, encoding="utf-8")
    paths = AppPaths(
        base_dir=tmp_path,
        config_env=tmp_path / "config.env",
        field_mapping_json=mapping,
        work_dir=tmp_path / "work",
        log_dir=tmp_path / "logs",
        error_dir=tmp_path / "error",
    )
    config = AppConfig(
        paths=paths,
        company_code="",
        kintone_domain="example.cybozu.com",
        kintone_app_id="1",
        kintone_api_token="token",
        csv_encoding="utf-8",
        shukka_kbn_options=[],
        cleanup_retention_days=7,
    )
    logger = logging.getLogger("test.kintone_existing")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return KintoneClient(config, logger)


class _FakeResponse:
    def __init__(self, records: list[dict]) -> None:
        self._payload = {"records": records}
        self.text = "{}" if records is None else "x"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FetchExistingRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.client = _make_client(Path(self._tmp.name))

    def test_query_uses_order_numbers_and_maps_back_to_csv_headers(self) -> None:
        record = {
            "受注No": {"value": "1405113"},
            "検索キー": {"value": "1405113-1-2-1"},
            "仕上日": {"value": "2026-06-20"},
            "平方メートル": {"value": "1.382"},
        }
        calls: list[dict] = []

        def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
            calls.append({"url": url, "params": params})
            return _FakeResponse([record])

        with patch("app.kintone_client.requests") as req:
            req.get.side_effect = fake_get
            rows = self.client.fetch_existing_records_by_order_numbers(["1405113", "1405114"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["受注No"], "1405113")
        self.assertEqual(rows[0]["仕上日"], "2026-06-20")
        self.assertEqual(rows[0]["㎡"], "1.382")  # 平方メートル -> ㎡ に逆引き
        query = calls[0]["params"]["query"]
        self.assertIn('受注No in ("1405113", "1405114")', query)

    def test_multiple_order_numbers_searched_together(self) -> None:
        seen: list[str] = []

        def fake_get(url, headers=None, params=None, timeout=None):  # noqa: ANN001
            seen.append(params["query"])
            return _FakeResponse([])

        with patch("app.kintone_client.requests") as req:
            req.get.side_effect = fake_get
            self.client.fetch_existing_records_by_order_numbers(["1001", "1002", "1003"])

        self.assertEqual(len(seen), 1)
        self.assertIn('"1001", "1002", "1003"', seen[0])

    def test_empty_order_numbers_returns_empty_without_request(self) -> None:
        with patch("app.kintone_client.requests") as req:
            rows = self.client.fetch_existing_records_by_order_numbers([])
        self.assertEqual(rows, [])
        req.get.assert_not_called()

    def test_no_existing_records_returns_empty(self) -> None:
        with patch("app.kintone_client.requests") as req:
            req.get.side_effect = lambda *a, **k: _FakeResponse([])
            rows = self.client.fetch_existing_records_by_order_numbers(["1001"])
        self.assertEqual(rows, [])


# ── 突合・反映 ────────────────────────────────────────────────────


def _preview_row(order_no: str, search_key: str, **extra: str) -> dict[str, str]:
    row = {
        "受注No": order_no,
        "検索キー": search_key,
        "硝/加工": "2",
        "商品名称": "品",
        "掛率集計コード": "0300",
        "掛率集計名称": "エッチング",
        "W寸法": "1000",
        "H寸法": "2000",
        "受注数量": "1",
        "OP区分": "02",
        "仕上日": "",
        "出荷区分": "",
    }
    row.update(extra)
    return row


class MergeExistingRecordsTest(unittest.TestCase):
    def test_receipt_level_fields_applied_to_all_rows_of_order(self) -> None:
        preview = [
            _preview_row("1405113", "1405113-1-2-1"),
            _preview_row("1405113", "1405113-2-2-1"),
            _preview_row("1405114", "1405114-1-2-1"),
        ]
        existing = [
            {"受注No": "1405113", "検索キー": "1405113-1-2-1", "仕上日": "2026-06-20", "出荷区分": "PM"},
        ]
        merged, _ = merge_existing_kintone_records_into_preview_rows(preview, existing)
        self.assertEqual(merged[0]["仕上日"], "2026-06-20")
        self.assertEqual(merged[1]["仕上日"], "2026-06-20")  # 同一受注No全行へ
        self.assertEqual(merged[1]["出荷区分"], "PM")
        self.assertEqual(merged[2]["仕上日"], "")  # 別受注Noは未反映

    def test_receipt_uses_first_non_empty(self) -> None:
        preview = [_preview_row("1405113", "k1")]
        existing = [
            {"受注No": "1405113", "検索キー": "kX", "仕上日": ""},
            {"受注No": "1405113", "検索キー": "kY", "仕上日": "2026-07-01"},
        ]
        merged, _ = merge_existing_kintone_records_into_preview_rows(preview, existing)
        self.assertEqual(merged[0]["仕上日"], "2026-07-01")

    def test_row_level_overrides_matched_by_search_key(self) -> None:
        preview = [
            _preview_row("1405113", "k1"),
            _preview_row("1405113", "k2"),
        ]
        existing = [
            {"受注No": "1405113", "検索キー": "k2", "加工種類": "3：短2",
             "加工名": "手修正加工", "加工mm": "12", "㎡": "9.999", "総㎡": "9.999"},
        ]
        _, existing_by_row = merge_existing_kintone_records_into_preview_rows(preview, existing)
        self.assertEqual(existing_by_row[0], {})  # k1 は既存なし
        # 反映対象は加工種類のみ。
        self.assertEqual(existing_by_row[1]["加工種類"], "3：短2")
        # 加工名・加工mm・㎡・総㎡ は反映対象外（Kintone既存値を取り込まない）。
        for excluded in ("加工名", "加工mm", "㎡", "総㎡"):
            self.assertNotIn(excluded, existing_by_row[1])

    def test_empty_existing_values_not_included(self) -> None:
        preview = [_preview_row("1405113", "k1")]
        existing = [{"受注No": "1405113", "検索キー": "k1", "加工種類": ""}]
        _, existing_by_row = merge_existing_kintone_records_into_preview_rows(preview, existing)
        self.assertNotIn("加工種類", existing_by_row[0])
        self.assertEqual(existing_by_row[0], {})

    def test_summary_single_order(self) -> None:
        existing = [
            {"受注No": "1405113", "検索キー": "k1"},
            {"受注No": "1405113", "検索キー": "k2"},
        ]
        self.assertEqual(summarize_existing_reflection(existing), "Kintone既存データを反映しました：1405113（2件）")

    def test_summary_multiple_orders(self) -> None:
        existing = [
            {"受注No": "1001", "検索キー": "a"},
            {"受注No": "1002", "検索キー": "b"},
            {"受注No": "1002", "検索キー": "c"},
        ]
        self.assertEqual(
            summarize_existing_reflection(existing),
            "Kintone既存データを反映しました：2件の受注No、3レコード",
        )

    def test_summary_empty(self) -> None:
        self.assertEqual(summarize_existing_reflection([]), "")


# ── PreviewState への反映（CSV/登録共通） ──────────────────────────

_MASTER = [
    {
        "掛率集計コード": "0300", "掛率集計名称": "エッチング", "加工名": "エッチング",
        "得意先1": "", "得意先2": "", "得意先3": "", "得意先4": "",
        "メーカー識別掛率集計コード": "MK0300", "メーカー識別コード": "MK", "掛率集計略称": "",
    },
]


class PreviewStateReflectionTest(unittest.TestCase):
    def _state(self, preview, existing):
        merged, existing_by_row = merge_existing_kintone_records_into_preview_rows(preview, existing)
        return PreviewState(rows=merged, kintone_existing_by_row=existing_by_row)

    def test_kintone_kakou_name_not_reflected(self) -> None:
        """加工名はKintone既存値を使わず、常にマスタから再判定する。"""
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "加工名": "手修正加工名"}],
        )
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["加工名"], "エッチング")  # マスタ再判定（Kintone値は無視）
        self.assertEqual(state.compute_kakou_name(0, _MASTER), "エッチング")

    def test_auto_kakou_name_kept_when_kintone_empty(self) -> None:
        state = self._state([_preview_row("1405113", "k1")], [])
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["加工名"], "エッチング")  # 自動判定を維持

    def test_kintone_kakou_mm_not_reflected(self) -> None:
        """加工mmはKintone既存値を使わず、常に加工種類とW/Hから再計算する。"""
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "加工mm": "33"}],
        )
        # 自動判定（加工種類1：四方）での再計算値であり、Kintoneの 33 は使わない。
        self.assertNotEqual(state.compute_kakou_mm(0), "33")
        self.assertEqual(state.compute_kakou_mm(0), state.compute_kakou_mm(0))

    def test_kintone_area_never_overrides_recalculation(self) -> None:
        """㎡ / 総㎡ はKintone既存値で上書きしない（常にOP区分から再計算）。"""
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "㎡": "7.777", "総㎡": "8.888"}],
        )
        rows = state.build_registration_rows(_MASTER)
        # OP区分02: 1000*2000/1000000 = 2 -> 2.000（Kintoneの 7.777/8.888 は無視）
        self.assertEqual(rows[0]["㎡"], "2.000")
        self.assertEqual(rows[0]["総㎡"], "2.000")

    def test_kintone_area_one_does_not_recontaminate(self) -> None:
        """Kintone側に過去の不具合値 ㎡=1 / 総㎡=1 が残っていても再汚染されない（要件7）。"""
        # W=1303, H=1061 -> 1303*1061/1000000 = 1.382483 -> 1.382
        preview = [_preview_row("1405113", "k1", W寸法="1303", H寸法="1061", 受注数量="1")]
        existing = [{"受注No": "1405113", "検索キー": "k1", "㎡": "1", "総㎡": "1"}]
        state = self._state(preview, existing)
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["㎡"], "1.382")
        self.assertEqual(rows[0]["総㎡"], "1.382")
        self.assertNotEqual(rows[0]["㎡"], "1")

    def test_auto_area_kept_when_kintone_empty(self) -> None:
        state = self._state([_preview_row("1405113", "k1")], [])
        rows = state.build_registration_rows(_MASTER)
        # OP区分02: 1000*2000/1000000 = 2 -> 2.000
        self.assertEqual(rows[0]["㎡"], "2.000")

    def test_kintone_shiage_shukka_reflected(self) -> None:
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "仕上日": "2026-06-20", "出荷区分": "PM"}],
        )
        self.assertEqual(state.shiage_by_row[0], "2026-06-20")
        self.assertEqual(state.shukka_by_row[0], "PM")

    def test_kintone_customer_selection_reflected(self) -> None:
        """得意先選択（受注No単位）がKintone既存値から反映される。"""
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "得意先選択": "得意先2"}],
        )
        self.assertEqual(state.customer_key_by_row[0], "得意先2")

    def test_kintone_customer_selection_default_when_empty(self) -> None:
        state = self._state([_preview_row("1405113", "k1")], [])
        self.assertEqual(state.customer_key_by_row[0], "selected")

    def test_kintone_kakou_type_reflected(self) -> None:
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "加工種類": "3：短2"}],
        )
        self.assertEqual(state.kakou_type_by_row[0], "3")

    def test_registration_rows_include_kakou_type_and_customer(self) -> None:
        """build_registration_rows の出力に 加工種類 / 得意先選択 が含まれる。"""
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "加工種類": "3：短2", "得意先選択": "得意先2"}],
        )
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["加工種類"], "3")          # Kintone既存値を反映
        self.assertEqual(rows[0]["得意先選択"], "得意先2")   # 受注No単位で反映

    def test_customer_selection_reflected_to_all_rows_of_order(self) -> None:
        """得意先選択は同一受注No全行へ反映される。"""
        preview = [_preview_row("1405113", "k1"), _preview_row("1405113", "k2")]
        existing = [{"受注No": "1405113", "検索キー": "k1", "得意先選択": "得意先3"}]
        state = self._state(preview, existing)
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["得意先選択"], "得意先3")
        self.assertEqual(rows[1]["得意先選択"], "得意先3")

    def test_default_customer_selection_sent_as_blank(self) -> None:
        """得意先未選択（既定 selected）は空欄で出力される（kintone不正登録防止）。"""
        state = self._state([_preview_row("1405113", "k1")], [])
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["得意先選択"], "")

    def test_screen_changed_values_sent(self) -> None:
        """画面で変更した 加工種類 / 得意先選択 が出力に反映される。"""
        state = self._state([_preview_row("1405113", "k1")], [])
        state.set_kakou_type(0, "2")
        state.set_customer_key_for_order(0, "得意先1")
        rows = state.build_registration_rows(_MASTER)
        self.assertEqual(rows[0]["加工種類"], "2")
        self.assertEqual(rows[0]["得意先選択"], "得意先1")

    def test_kakou_type_auto_when_kintone_empty(self) -> None:
        state = self._state([_preview_row("1405113", "k1")], [])
        # Kintone加工種類なし → 自動判定（既定 1：四方）
        self.assertEqual(state.kakou_type_by_row[0], "1")

    def test_csv_and_kintone_rows_match(self) -> None:
        state = self._state(
            [_preview_row("1405113", "k1")],
            [{"受注No": "1405113", "検索キー": "k1", "加工名": "手修正", "㎡": "7.777", "総㎡": "8.888"}],
        )
        csv_rows = state.build_registration_rows(_MASTER)
        kintone_rows = state.build_registration_rows(_MASTER)
        for field_name in ("加工名", "加工mm", "㎡", "総㎡", "仕上日", "出荷区分"):
            self.assertEqual(csv_rows[0].get(field_name), kintone_rows[0].get(field_name))
        # ㎡/総㎡ はOP区分から再計算された値（Kintoneの 7.777/8.888 ではない）
        self.assertEqual(csv_rows[0]["㎡"], "2.000")


if __name__ == "__main__":
    unittest.main()
