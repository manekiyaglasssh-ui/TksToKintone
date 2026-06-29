"""ProgramData 側 OLAPテンプレート自動配置（ensure_olap_templates_installed）テスト。

更新時に古いテンプレートを削除しても、起動時・OLAP取得直前に同梱側から
ProgramData へ再配置されることを検証する。
"""
from __future__ import annotations

import json
import logging
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import config as app_config
from app.config import ensure_olap_templates_installed, olap_template_dir
from app.models import AppConfig, AppPaths
from app.tks_client import HttpTksClient

TEMPLATE_NAMES = ("kakou_request_template.json", "soba_request_template.json")


def _write_template(path: Path, marker: str) -> None:
    payload = {
        "R1List": [
            {"OLAP表示No": 1, "OLAP表示名": "受注No", "フィールド論理名": "受注No"},
            {"OLAP表示No": 34, "OLAP表示名": "OP区分", "フィールド論理名": "OP区分"},
        ],
        "R2List": [{"フィールド論理名": "受注No", "OLAP値": "", "marker": marker}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class EnsureOlapTemplatesInstalledTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base_dir = self.tmp / "ProgramData" / "Manekiya" / "TksToKintone"
        # アプリ同梱側ディレクトリ（dev環境の docs/olap 相当）を用意する。
        self.bundle_dir = self.tmp / "bundle" / "docs" / "olap"
        for name in TEMPLATE_NAMES:
            _write_template(self.bundle_dir / name, marker="bundled")
        # olap_template_source_dirs() を一時バンドルだけ指すように差し替える。
        self._patcher = mock.patch.object(
            app_config,
            "olap_template_source_dirs",
            return_value=[self.bundle_dir],
        )
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def test_creates_docs_olap_when_missing(self) -> None:
        # docs/olap が無くても自動作成される。
        self.assertFalse((self.base_dir / "docs" / "olap").exists())
        result = ensure_olap_templates_installed(self.base_dir)
        self.assertTrue(result.is_dir())
        self.assertEqual(result, olap_template_dir(self.base_dir))

    def test_copies_template_when_missing(self) -> None:
        # kakou_request_template.json が無ければ自動コピーされる。
        ensure_olap_templates_installed(self.base_dir)
        target = olap_template_dir(self.base_dir) / "kakou_request_template.json"
        self.assertTrue(target.exists())
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["R2List"][0]["marker"], "bundled")

    def test_redeploys_after_old_template_deleted(self) -> None:
        # 更新で古いテンプレートを削除した後、再配置される。
        ensure_olap_templates_installed(self.base_dir)
        target_dir = olap_template_dir(self.base_dir)
        for name in TEMPLATE_NAMES:
            (target_dir / name).unlink()
        for name in TEMPLATE_NAMES:
            self.assertFalse((target_dir / name).exists())

        ensure_olap_templates_installed(self.base_dir)
        for name in TEMPLATE_NAMES:
            self.assertTrue((target_dir / name).exists())

    def test_overwrites_when_bundled_is_newer(self) -> None:
        # 既存より同梱側が新しい場合は上書きする。
        ensure_olap_templates_installed(self.base_dir)
        target = olap_template_dir(self.base_dir) / "kakou_request_template.json"
        _write_template(target, marker="old")
        old_mtime = time.time() - 1000
        import os

        os.utime(target, (old_mtime, old_mtime))
        # 同梱側を更新（新しいmtime）。
        _write_template(self.bundle_dir / "kakou_request_template.json", marker="new")

        ensure_olap_templates_installed(self.base_dir)
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["R2List"][0]["marker"], "new")

    def test_missing_source_logs_all_candidates(self) -> None:
        # コピー元が見つからない場合、候補パスがすべてログに出る。
        self._patcher.stop()
        empty_dir = self.tmp / "empty" / "docs" / "olap"
        with mock.patch.object(
            app_config,
            "olap_template_source_dirs",
            return_value=[empty_dir],
        ):
            with self.assertLogs("tks_to_kintone_app", level="WARNING") as captured:
                ensure_olap_templates_installed(self.base_dir)
        joined = "\n".join(captured.output)
        for name in TEMPLATE_NAMES:
            self.assertIn(str(empty_dir / name), joined)
        # tearDown 用に再起動しておく。
        self._patcher.start()


class OlapFetchSelfRepairTest(unittest.TestCase):
    """OLAP取得直前にテンプレートが無くても自己復旧して処理を続行できる。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.base_dir = self.tmp / "ProgramData"
        self.bundle_dir = self.tmp / "bundle" / "docs" / "olap"
        for name in TEMPLATE_NAMES:
            _write_template(self.bundle_dir / name, marker="bundled")
        self.logger = logging.getLogger("test_olap_template_install")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, kakou_template: Path) -> AppConfig:
        paths = AppPaths(
            base_dir=self.base_dir,
            config_env=self.base_dir / "config.env",
            field_mapping_json=self.base_dir / "field_mapping.json",
            work_dir=self.base_dir / "work",
            log_dir=self.base_dir / "logs",
            error_dir=self.base_dir / "error",
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
            tks_soba_request_template=kakou_template,
        )

    def test_self_repair_when_template_missing(self) -> None:
        # 設定上のパスは ProgramData 側だが、まだ存在しない状態。
        missing = olap_template_dir(self.base_dir) / "kakou_request_template.json"
        self.assertFalse(missing.exists())
        config = self._config(missing)
        client = HttpTksClient(config, self.logger)

        with mock.patch.object(
            app_config, "olap_template_source_dirs", return_value=[self.bundle_dir]
        ), mock.patch.object(app_config, "default_base_dir", return_value=self.base_dir):
            payload = client._build_olap_payload("kakou", ["1386655"])

        # 復旧して取得処理（payload構築）まで進める。
        self.assertIn("R1List", payload)
        self.assertTrue(missing.exists())

    def test_clear_error_when_source_missing(self) -> None:
        # コピー元テンプレートが無い場合、探したパスとログ場所を含むエラー。
        missing = olap_template_dir(self.base_dir) / "kakou_request_template.json"
        config = self._config(missing)
        client = HttpTksClient(config, self.logger)
        empty_dir = self.tmp / "empty" / "docs" / "olap"

        with mock.patch.object(
            app_config, "olap_template_source_dirs", return_value=[empty_dir]
        ), mock.patch.object(app_config, "default_base_dir", return_value=self.base_dir):
            with self.assertRaises(FileNotFoundError) as ctx:
                client._build_olap_payload("kakou", ["1386655"])

        message = str(ctx.exception)
        self.assertIn(str(empty_dir / "kakou_request_template.json"), message)
        self.assertIn(str(config.paths.log_dir), message)

    def test_pyinstaller_internal_source(self) -> None:
        # _internal/docs/olap からコピーできる（PyInstaller onedir 相当）。
        internal_dir = self.tmp / "_meipass" / "_internal" / "docs" / "olap"
        for name in TEMPLATE_NAMES:
            _write_template(internal_dir / name, marker="internal")

        def _source_dirs() -> list[Path]:
            # resource_path("_internal/docs/olap") 相当のみを返す。
            return [
                self.tmp / "_meipass" / "docs" / "olap",
                internal_dir,
            ]

        with mock.patch.object(
            app_config, "olap_template_source_dirs", side_effect=_source_dirs
        ):
            ensure_olap_templates_installed(self.base_dir)

        target = olap_template_dir(self.base_dir) / "kakou_request_template.json"
        self.assertTrue(target.exists())
        data = json.loads(target.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["R2List"][0]["marker"], "internal")


if __name__ == "__main__":
    unittest.main()
