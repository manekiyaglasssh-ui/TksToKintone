from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.kintone_client import KintoneClient, normalize_kintone_date, supplement_field_mapping
from app.models import AppConfig, AppPaths


class KintoneDateTest(unittest.TestCase):
    def test_normalize_kintone_date_accepts_supported_formats(self) -> None:
        self.assertEqual(normalize_kintone_date("2026-05-25"), "2026-05-25")
        self.assertEqual(normalize_kintone_date("2026/05/25"), "2026-05-25")
        self.assertEqual(normalize_kintone_date("20260525"), "2026-05-25")
        self.assertEqual(normalize_kintone_date("2026年5月25日"), "2026-05-25")

    def test_normalize_kintone_date_treats_empty_values_as_blank(self) -> None:
        self.assertEqual(normalize_kintone_date(""), "")
        self.assertEqual(normalize_kintone_date(None), "")
        self.assertEqual(normalize_kintone_date("-"), "")
        self.assertEqual(normalize_kintone_date("null"), "")
        self.assertEqual(normalize_kintone_date("None"), "")
        self.assertEqual(normalize_kintone_date("0"), "")
        self.assertEqual(normalize_kintone_date("0000/00/00"), "")
        self.assertEqual(normalize_kintone_date("0000-00-00"), "")

    def test_register_rows_rejects_invalid_date_before_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, r"records\[0\] 発注日 の日付形式が不正です: xxxx"):
                client.register_rows([{"発注日": "xxxx"}])

    def test_failed_records_keep_normalized_sent_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client(Path(temp_dir))

            class FakeHttpError(Exception):
                response = None

            def raise_http_error(_records: object) -> None:
                raise FakeHttpError()

            with patch("app.kintone_client.REQUESTS_HTTP_ERROR", FakeHttpError), patch.object(
                client, "_put_records_upsert", raise_http_error
            ):
                result = client.register_rows([{"検索キー": "key-1", "発注日": "2026/05/25", "納品日": "0"}])

            self.assertEqual(result.failure_count, 1)
            self.assertEqual(result.failed_records, [{"検索キー": "key-1", "発注日": "2026-05-25", "納品日": ""}])

    def test_to_upsert_record_uses_search_key_and_excludes_it_from_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "発注日": "2026-05-25"}, 0)

            self.assertEqual(record["updateKey"], {"field": "検索キー", "value": "key-1"})
            self.assertEqual(record["record"], {"発注日": {"value": "2026-05-25"}})

    def test_register_rows_rejects_blank_search_key_before_put(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, r"records\[0\] 検索キー が空"):
                client.register_rows([{"検索キー": "", "発注日": "2026-05-25"}])


class FieldMappingSupplementTest(unittest.TestCase):
    def test_supplement_adds_missing_kakou_name_to_file_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping_path = Path(temp_dir) / "field_mapping.json"
            mapping_path.write_text('{"検索キー":"検索キー"}', encoding="utf-8-sig")

            client = _make_client(Path(temp_dir), mapping_path)

            self.assertIn("加工名", client.mapping)
            self.assertEqual(client.mapping["加工名"], "加工名")

            import json
            saved = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(saved["加工名"], "加工名")
            self.assertEqual(saved["検索キー"], "検索キー")

    def test_supplement_does_not_overwrite_existing_kakou_name_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping_path = Path(temp_dir) / "field_mapping.json"
            mapping_path.write_text('{"検索キー":"検索キー","加工名":"custom_kakou"}', encoding="utf-8-sig")

            client = _make_client(Path(temp_dir), mapping_path)

            self.assertEqual(client.mapping["加工名"], "custom_kakou")

    def test_supplement_standalone_adds_key_and_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping_path = Path(temp_dir) / "field_mapping.json"
            mapping_path.write_text('{"検索キー":"検索キー"}', encoding="utf-8-sig")

            supplement_field_mapping(mapping_path)

            import json
            saved = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
            self.assertIn("加工名", saved)

    def test_supplement_standalone_no_change_when_key_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import json

            from app.kintone_client import DEFAULT_FIELD_MAPPING_SUPPLEMENTS

            mapping_path = Path(temp_dir) / "field_mapping.json"
            existing = {key: f"existing_{key}" for key in DEFAULT_FIELD_MAPPING_SUPPLEMENTS}
            mapping_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8-sig")
            mtime_before = mapping_path.stat().st_mtime

            supplement_field_mapping(mapping_path)

            self.assertEqual(mapping_path.stat().st_mtime, mtime_before)


class KintoneFieldExclusionTest(unittest.TestCase):
    """工程は送信しない、加工名を送信する、というフィールド仕様を検証する。"""

    def test_koutei_excluded_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_koutei(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "工程": ""}, 0)
            self.assertNotIn("工程", record["record"])

    def test_koutei_excluded_even_when_has_value(self) -> None:
        """field_mapping.json に工程があっても kintone には送信しない。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_koutei(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "工程": "洗浄"}, 0)
            self.assertNotIn("工程", record["record"])

    def test_koutei_excluded_with_multiple_values(self) -> None:
        """パイプ区切り複数値でも 工程 は除外される。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_koutei(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "工程": "洗浄|印刷"}, 0)
            self.assertNotIn("工程", record["record"])

    def test_kakou_name_sent_as_plain_string(self) -> None:
        """加工名は文字列として kintone へ送信される。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_kakou_name(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "加工名": "エッチング"}, 0)
            self.assertEqual(record["record"]["加工名"], {"value": "エッチング"})

    def test_kakou_name_empty_not_sent(self) -> None:
        """加工名が空文字の場合は kintone へ送信しない。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_kakou_name(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "加工名": ""}, 0)
            self.assertNotIn("加工名", record["record"])

    def test_kakou_name_joined_value_sent(self) -> None:
        """「、」結合済みの加工名もそのまま文字列として送信される。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_kakou_name(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "加工名": "エッチング、DM-10、広幅"}, 0)
            self.assertEqual(record["record"]["加工名"], {"value": "エッチング、DM-10、広幅"})

    def test_koutei_excluded_and_kakou_name_in_same_record(self) -> None:
        """工程が除外され、加工名が含まれることを同時に確認。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_koutei_and_kakou_name(Path(temp_dir))
            record = client._to_upsert_record(
                {"検索キー": "key-1", "工程": "洗浄|印刷", "加工名": "エッチング"},
                0,
            )
            self.assertNotIn("工程", record["record"])
            self.assertEqual(record["record"]["加工名"], {"value": "エッチング"})

    def test_total_weight_not_sent_without_field_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "総重量": "2.12"}, 0)
            self.assertNotIn("総重量", record["record"])

    def test_total_weight_sent_when_field_mapping_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _client_with_total_weight(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "総重量": "2.12"}, 0)
            self.assertEqual(record["record"]["総重量"], {"value": "2.12"})


class KintoneKakouTypeCustomerTest(unittest.TestCase):
    """加工種類・得意先選択がkintone送信recordに含まれることを検証。"""

    def _client(self, tmp_path: Path) -> KintoneClient:
        mapping = tmp_path / "field_mapping.json"
        mapping.write_text(
            '{"検索キー":"検索キー","加工種類":"加工種類","得意先選択":"得意先選択"}',
            encoding="utf-8",
        )
        return _make_client(tmp_path, mapping)

    def test_kakou_type_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self._client(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "加工種類": "3"}, 0)
            self.assertEqual(record["record"]["加工種類"], {"value": "3"})

    def test_customer_selection_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self._client(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "得意先選択": "得意先2"}, 0)
            self.assertEqual(record["record"]["得意先選択"], {"value": "得意先2"})

    def test_empty_values_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self._client(Path(temp_dir))
            record = client._to_upsert_record({"検索キー": "key-1", "加工種類": "", "得意先選択": ""}, 0)
            self.assertNotIn("加工種類", record["record"])
            self.assertNotIn("得意先選択", record["record"])

    def test_supplement_adds_kakou_type_and_customer_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping_path = Path(temp_dir) / "field_mapping.json"
            mapping_path.write_text('{"検索キー":"検索キー"}', encoding="utf-8-sig")
            client = _make_client(Path(temp_dir), mapping_path)
            self.assertEqual(client.mapping["加工種類"], "加工種類")
            self.assertEqual(client.mapping["得意先選択"], "得意先選択")


def _client(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text('{"検索キー":"検索キー","発注日":"発注日","納品日":"納品日"}', encoding="utf-8")
    return _make_client(tmp_path, mapping)


def _client_with_koutei(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text('{"検索キー":"検索キー","発注日":"発注日","納品日":"納品日","工程":"工程"}', encoding="utf-8")
    return _make_client(tmp_path, mapping)


def _client_with_kakou_name(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text('{"検索キー":"検索キー","加工名":"加工名"}', encoding="utf-8")
    return _make_client(tmp_path, mapping)


def _client_with_koutei_and_kakou_name(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text('{"検索キー":"検索キー","工程":"工程","加工名":"加工名"}', encoding="utf-8")
    return _make_client(tmp_path, mapping)


def _client_with_total_weight(tmp_path: Path) -> KintoneClient:
    mapping = tmp_path / "field_mapping.json"
    mapping.write_text('{"検索キー":"検索キー","総重量":"総重量"}', encoding="utf-8")
    return _make_client(tmp_path, mapping)


def _make_client(tmp_path: Path, mapping: Path) -> KintoneClient:
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
    logger = logging.getLogger("test.kintone_client")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return KintoneClient(config, logger)


if __name__ == "__main__":
    unittest.main()
