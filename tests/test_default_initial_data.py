"""初回起動時のデフォルト初期データ投入テスト。

- 加工名マスタ（同梱CSV116件）
- 得意先ヘッダー設定（東芝・日立・フジテック／エレベータ）
- Teams Webhook URL（テスト用／本番用=東大阪）
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from app.config import load_app_config
from app.customer_header import (
    DEFAULT_CUSTOMER_HEADERS,
    ensure_default_customer_headers,
)
from app.kakou_master import (
    KAKOU_MASTER_HEADERS,
    ensure_default_kakou_master,
    load_master,
)
from app.settings_service import (
    SETTINGS_TEAMS_WEBHOOK_URL_PROD,
    SETTINGS_TEAMS_WEBHOOK_URL_TEST,
    default_kakou_master_csv_path,
    ensure_default_initial_data,
    ensure_default_webhook_urls,
    find_default_kakou_master_csv,
)
from app.teams_notifier import (
    DEFAULT_TEAMS_PROD_WEBHOOK_URL,
    DEFAULT_TEAMS_TEST_WEBHOOK_URL,
    TEAMS_WEBHOOK_URL_PROD_ENV,
    TEAMS_WEBHOOK_URL_TEST_ENV,
)

DEFAULT_CSV = default_kakou_master_csv_path()
EXPECTED_MASTER_COUNT = 116


class FakeSettings:
    """QSettings の contains/value/setValue/sync を模した最小実装。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, object] = dict(initial or {})
        self.synced = False

    def contains(self, key: str) -> bool:
        return key in self._store

    def value(self, key: str, default: object = None) -> object:
        return self._store.get(key, default)

    def setValue(self, key: str, value: object) -> None:
        self._store[key] = value

    def sync(self) -> None:
        self.synced = True


class DefaultKakouMasterTest(unittest.TestCase):
    def test_bundled_csv_exists_and_has_116_rows(self) -> None:
        self.assertTrue(DEFAULT_CSV.exists(), f"同梱CSVが見つからない: {DEFAULT_CSV}")
        rows = load_master(DEFAULT_CSV)
        self.assertEqual(len(rows), EXPECTED_MASTER_COUNT)

    def test_seed_when_master_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kakou_master.csv"
            self.assertFalse(path.exists())
            created = ensure_default_kakou_master(path, DEFAULT_CSV)
            self.assertTrue(created)
            rows = load_master(path)
            self.assertEqual(len(rows), EXPECTED_MASTER_COUNT)

    def test_headers_loaded_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kakou_master.csv"
            ensure_default_kakou_master(path, DEFAULT_CSV)
            rows = load_master(path)
            self.assertEqual(list(rows[0].keys()), KAKOU_MASTER_HEADERS)

    def test_representative_row_loaded(self) -> None:
        rows = load_master(DEFAULT_CSV)
        first = rows[0]
        self.assertEqual(first["メーカー識別掛率集計コード"], "MK0010")
        self.assertEqual(first["メーカー識別コード"], "MK")
        self.assertEqual(first["掛率集計コード"], "0010")
        self.assertEqual(first["掛率集計名称"], "小口加工２ミリ")
        self.assertEqual(first["加工名"], "手加工")

    def test_seed_when_master_has_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kakou_master.csv"
            from app.kakou_master import save_master

            save_master(path, [])
            created = ensure_default_kakou_master(path, DEFAULT_CSV)
            self.assertTrue(created)
            self.assertEqual(len(load_master(path)), len(load_master(DEFAULT_CSV)))

    def test_does_not_overwrite_existing_master(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kakou_master.csv"
            from app.kakou_master import save_master

            existing = [{h: "" for h in KAKOU_MASTER_HEADERS}]
            existing[0]["掛率集計コード"] = "9999"
            existing[0]["加工名"] = "ユーザー登録済み"
            save_master(path, existing)

            created = ensure_default_kakou_master(path, DEFAULT_CSV)
            self.assertFalse(created)
            rows = load_master(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["加工名"], "ユーザー登録済み")

    def test_utf8_bom_is_read(self) -> None:
        # 同梱CSVはUTF-8 BOM付き。先頭にBOMが残らず正しく読める。
        with DEFAULT_CSV.open("rb") as fp:
            self.assertEqual(fp.read(3), b"\xef\xbb\xbf")
        rows = load_master(DEFAULT_CSV)
        self.assertNotIn("﻿", "".join(rows[0].keys()))

    def test_resolvable_under_pyinstaller_meipass(self) -> None:
        # exe実行時は sys._MEIPASS 配下から同梱CSVを参照できる。
        from app.config import resource_path

        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "docs" / "kakou_master_20260618_132327.csv"
            bundled.parent.mkdir(parents=True, exist_ok=True)
            bundled.write_bytes(DEFAULT_CSV.read_bytes())
            previous = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = temp_dir  # type: ignore[attr-defined]
            try:
                resolved = default_kakou_master_csv_path()
                self.assertEqual(resolved, bundled)
                self.assertTrue(resolved.exists())
            finally:
                if previous is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = previous  # type: ignore[attr-defined]


class FindDefaultKakouMasterCsvTest(unittest.TestCase):
    """初期CSV共通探索関数 find_default_kakou_master_csv のテスト。"""

    def _with_meipass(self, temp_dir: str):
        """sys._MEIPASS を temp_dir に差し替えるコンテキスト用ヘルパ。"""
        previous = getattr(sys, "_MEIPASS", None)
        sys._MEIPASS = temp_dir  # type: ignore[attr-defined]

        def restore() -> None:
            if previous is None:
                if hasattr(sys, "_MEIPASS"):
                    delattr(sys, "_MEIPASS")
            else:
                sys._MEIPASS = previous  # type: ignore[attr-defined]

        return restore

    def test_finds_docs_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "docs" / "kakou_master_20260618_132327.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(DEFAULT_CSV.read_bytes())
            restore = self._with_meipass(temp_dir)
            try:
                found = find_default_kakou_master_csv()
                self.assertEqual(found, target)
            finally:
                restore()

    def test_finds_templates_csv_when_docs_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "templates" / "kakou_master_default.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(DEFAULT_CSV.read_bytes())
            restore = self._with_meipass(temp_dir)
            # docs を持つリポジトリ直下から実行するとcwd相対候補が拾われるため退避。
            prev_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                found = find_default_kakou_master_csv()
                self.assertEqual(found, target)
            finally:
                os.chdir(prev_cwd)
                restore()

    def test_resolves_under_pyinstaller_internal_layout(self) -> None:
        # PyInstaller onedir では _MEIPASS が _internal を指す。その配下から解決できる。
        with tempfile.TemporaryDirectory() as temp_dir:
            internal = Path(temp_dir) / "_internal"
            target = internal / "docs" / "kakou_master_20260618_132327.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(DEFAULT_CSV.read_bytes())
            restore = self._with_meipass(str(internal))
            try:
                found = find_default_kakou_master_csv()
                self.assertEqual(found, target)
                rows = load_master(found)
                self.assertEqual(len(rows), EXPECTED_MASTER_COUNT)
            finally:
                restore()

    def test_logs_candidates_when_not_found(self) -> None:
        import logging

        with tempfile.TemporaryDirectory() as temp_dir:
            # CSVを一切置かない空ディレクトリ。カレント相対候補も外すため cwd を移す。
            restore = self._with_meipass(temp_dir)
            prev_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with self.assertLogs("tks_to_kintone_app", level=logging.WARNING) as cm:
                    found = find_default_kakou_master_csv()
                self.assertIsNone(found)
                joined = "\n".join(cm.output)
                self.assertIn("加工名マスタ初期CSVが見つかりません", joined)
                self.assertIn("kakou_master_20260618_132327.csv", joined)
                self.assertIn("kakou_master_default.csv", joined)
            finally:
                os.chdir(prev_cwd)
                restore()

    def test_startup_seed_and_reset_use_same_finder(self) -> None:
        # 起動時自動投入(ensure_default_kakou_master_from_bundle) と gui の reset が
        # 同じ find_default_kakou_master_csv を参照していることを担保する。
        import app.settings_service as settings_service

        self.assertIs(
            settings_service.ensure_default_kakou_master_from_bundle.__globals__[
                "find_default_kakou_master_csv"
            ],
            find_default_kakou_master_csv,
        )


class DefaultCustomerHeaderTest(unittest.TestCase):
    def _run_load(self, temp_dir: str):
        previous_home = os.environ.get("TKS_TO_KINTONE_HOME")
        os.environ["TKS_TO_KINTONE_HOME"] = temp_dir
        try:
            return load_app_config()
        finally:
            if previous_home is None:
                os.environ.pop("TKS_TO_KINTONE_HOME", None)
            else:
                os.environ["TKS_TO_KINTONE_HOME"] = previous_home

    def test_first_launch_customer1_label_and_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._run_load(temp_dir)
            self.assertEqual(config.customer_labels["得意先1"], "東芝・日立・フジテック")
            self.assertEqual(config.customer_match_patterns["得意先1"], "エレベータ")

    def test_first_launch_customer2to4_are_default_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._run_load(temp_dir)
            for index in (2, 3, 4):
                key = f"得意先{index}"
                self.assertEqual(config.customer_labels[key], f"得意先{index}")
                self.assertEqual(config.customer_match_patterns[key], "")

    def test_does_not_overwrite_existing_customer_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.env"
            config_path.write_text(
                "CUSTOMER_LABEL_1=独自名称\nCUSTOMER_MATCH_1=独自キーワード\n",
                encoding="utf-8",
            )
            wrote = ensure_default_customer_headers(config_path)
            self.assertTrue(wrote)  # 2〜4は補完される
            text = config_path.read_text(encoding="utf-8")
            # 既存の得意先1は保持される。
            self.assertIn("CUSTOMER_LABEL_1=独自名称", text)
            self.assertIn("CUSTOMER_MATCH_1=独自キーワード", text)
            self.assertNotIn("東芝・日立・フジテック", text)

    def test_all_blank_customer_headers_are_defaulted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.env"
            config_path.write_text(
                "\n".join(
                    [
                        "CUSTOMER_LABEL_1=",
                        "CUSTOMER_MATCH_1=",
                        "CUSTOMER_LABEL_2=",
                        "CUSTOMER_MATCH_2=",
                        "CUSTOMER_LABEL_3=",
                        "CUSTOMER_MATCH_3=",
                        "CUSTOMER_LABEL_4=",
                        "CUSTOMER_MATCH_4=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            wrote = ensure_default_customer_headers(config_path)
            self.assertTrue(wrote)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=東芝・日立・フジテック", text)
            self.assertIn("CUSTOMER_MATCH_1=エレベータ", text)
            self.assertIn("CUSTOMER_LABEL_2=得意先2", text)
            self.assertIn("CUSTOMER_MATCH_2=", text)

    def test_partial_customer_header_keeps_existing_blank_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.env"
            config_path.write_text(
                "CUSTOMER_LABEL_1=独自名称\nCUSTOMER_MATCH_1=\n",
                encoding="utf-8",
            )
            ensure_default_customer_headers(config_path)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=独自名称", text)
            self.assertIn("CUSTOMER_MATCH_1=\n", text)
            self.assertIn("CUSTOMER_LABEL_2=得意先2", text)

    def test_default_headers_constant(self) -> None:
        self.assertEqual(DEFAULT_CUSTOMER_HEADERS["customer1_label"], "東芝・日立・フジテック")
        self.assertEqual(DEFAULT_CUSTOMER_HEADERS["customer1_keywords"], "エレベータ")


class DefaultWebhookTest(unittest.TestCase):
    def _clear_env(self) -> dict[str, str | None]:
        saved = {
            TEAMS_WEBHOOK_URL_TEST_ENV: os.environ.pop(TEAMS_WEBHOOK_URL_TEST_ENV, None),
            TEAMS_WEBHOOK_URL_PROD_ENV: os.environ.pop(TEAMS_WEBHOOK_URL_PROD_ENV, None),
        }
        return saved

    def _restore_env(self, saved: dict[str, str | None]) -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_first_launch_sets_test_and_prod_urls(self) -> None:
        saved = self._clear_env()
        try:
            settings = FakeSettings()
            wrote = ensure_default_webhook_urls(settings)
            self.assertTrue(wrote)
            self.assertEqual(
                settings.value(SETTINGS_TEAMS_WEBHOOK_URL_TEST),
                DEFAULT_TEAMS_TEST_WEBHOOK_URL,
            )
            # 本番用は東大阪URL。
            self.assertEqual(
                settings.value(SETTINGS_TEAMS_WEBHOOK_URL_PROD),
                DEFAULT_TEAMS_PROD_WEBHOOK_URL,
            )
            self.assertIn("935f20267d734174b36d6c6ab6c953c9", DEFAULT_TEAMS_PROD_WEBHOOK_URL)
        finally:
            self._restore_env(saved)

    def test_does_not_overwrite_existing_webhook(self) -> None:
        settings = FakeSettings(
            {
                SETTINGS_TEAMS_WEBHOOK_URL_TEST: "https://user/test",
                SETTINGS_TEAMS_WEBHOOK_URL_PROD: "https://user/prod",
            }
        )
        wrote = ensure_default_webhook_urls(settings)
        self.assertFalse(wrote)
        self.assertEqual(settings.value(SETTINGS_TEAMS_WEBHOOK_URL_TEST), "https://user/test")
        self.assertEqual(settings.value(SETTINGS_TEAMS_WEBHOOK_URL_PROD), "https://user/prod")

    def test_webhook_url_not_logged(self) -> None:
        import logging

        saved = self._clear_env()
        try:
            settings = FakeSettings()
            with self.assertLogs("tks_to_kintone_app", level=logging.INFO) as cm:
                ensure_default_webhook_urls(settings)
            joined = "\n".join(cm.output)
            self.assertIn("Teams Webhook URL is configured", joined)
            self.assertNotIn(DEFAULT_TEAMS_TEST_WEBHOOK_URL, joined)
            self.assertNotIn(DEFAULT_TEAMS_PROD_WEBHOOK_URL, joined)
            self.assertNotIn("sig=", joined)
        finally:
            self._restore_env(saved)


class EnsureDefaultInitialDataTest(unittest.TestCase):
    def test_orchestrator_seeds_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kakou_path = Path(temp_dir) / "kakou_master.csv"
            config_path = Path(temp_dir) / "config.env"
            settings = FakeSettings()

            saved_test = os.environ.pop(TEAMS_WEBHOOK_URL_TEST_ENV, None)
            saved_prod = os.environ.pop(TEAMS_WEBHOOK_URL_PROD_ENV, None)
            try:
                ensure_default_initial_data(kakou_path, config_path, settings)
            finally:
                if saved_test is not None:
                    os.environ[TEAMS_WEBHOOK_URL_TEST_ENV] = saved_test
                if saved_prod is not None:
                    os.environ[TEAMS_WEBHOOK_URL_PROD_ENV] = saved_prod

            self.assertEqual(len(load_master(kakou_path)), EXPECTED_MASTER_COUNT)
            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn("CUSTOMER_LABEL_1=東芝・日立・フジテック", config_text)
            self.assertIn("CUSTOMER_MATCH_1=エレベータ", config_text)
            self.assertTrue(settings.contains(SETTINGS_TEAMS_WEBHOOK_URL_TEST))
            self.assertTrue(settings.contains(SETTINGS_TEAMS_WEBHOOK_URL_PROD))

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kakou_path = Path(temp_dir) / "kakou_master.csv"
            config_path = Path(temp_dir) / "config.env"
            settings = FakeSettings()
            ensure_default_initial_data(kakou_path, config_path, settings)
            # 2回目は加工名マスタを上書きしない。
            created = ensure_default_kakou_master(kakou_path, DEFAULT_CSV)
            self.assertFalse(created)
            self.assertEqual(len(load_master(kakou_path)), EXPECTED_MASTER_COUNT)


if __name__ == "__main__":
    unittest.main()
