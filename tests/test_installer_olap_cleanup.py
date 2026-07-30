"""Inno Setup スクリプトのOLAPテンプレート更新設定テスト。

更新インストール時に古い _internal\\docs\\olap の *.json を削除し、最新版の
docs/olap を確実に再配置する設定が維持されていることを検証する。
spec/iss は実行できないためソースを読み取って静的に確認する。
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS_PATH = ROOT / "installer" / "tks-to-kintone.iss"


class InstallerOlapCleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = ISS_PATH.read_text(encoding="utf-8")

    def _install_delete_section(self) -> str:
        match = re.search(
            r"\[InstallDelete\](.*?)(?:\n\[|\Z)", self.source, re.DOTALL
        )
        self.assertIsNotNone(match, "[InstallDelete] セクションが見つからない")
        return match.group(1)

    def test_install_delete_removes_olap_json_wildcard(self) -> None:
        section = self._install_delete_section()
        self.assertRegex(
            section,
            r'Name:\s*"\{app\}\\_internal\\docs\\olap\\\*\.json"',
            "olap配下の*.json一括削除指定が無い",
        )

    def test_install_delete_removes_specific_templates(self) -> None:
        section = self._install_delete_section()
        for name in ("kakou_request_template.json", "soba_request_template.json"):
            self.assertIn(name, section, f"{name} の削除指定が無い")

    def test_files_section_bundles_docs_with_overwrite(self) -> None:
        # docs を含む dist 一式が ignoreversion recursesubdirs で上書きコピーされる。
        match = re.search(
            r'Source:\s*"\.\.\\dist\\TksToKintone\\\*";[^\n]*', self.source
        )
        self.assertIsNotNone(match, "dist一式コピーの[Files]行が見つからない")
        line = match.group(0)
        self.assertIn("ignoreversion", line)
        self.assertIn("recursesubdirs", line)

    def test_files_section_explicitly_installs_templates(self) -> None:
        # PyInstaller バンドル漏れに備え、リポジトリ docs/olap から
        # {app}\_internal\docs\olap へ各テンプレートを明示コピーする行があること。
        for name in ("kakou_request_template.json", "soba_request_template.json"):
            pattern = (
                r'Source:\s*"\.\.\\docs\\olap\\'
                + re.escape(name)
                + r'";\s*DestDir:\s*"\{app\}\\_internal\\docs\\olap";[^\n]*ignoreversion'
            )
            self.assertRegex(
                self.source,
                pattern,
                f"{name} を _internal\\docs\\olap へ明示コピーする[Files]行が無い",
            )


class BundledTemplateContentTest(unittest.TestCase):
    """更新後に配置されるテンプレートに OP区分 が含まれること。"""

    def _op_kubun_count(self, r1_list: list) -> int:
        return sum(
            1
            for item in r1_list
            if isinstance(item, dict)
            and (
                item.get("OLAP表示名") == "OP区分"
                or item.get("フィールド論理名") == "OP区分"
            )
        )

    def test_templates_contain_op_kubun(self) -> None:
        for name in ("kakou_request_template.json", "soba_request_template.json"):
            path = ROOT / "docs" / "olap" / name
            self.assertTrue(path.exists(), f"{name} が同梱されていない")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            self.assertEqual(self._op_kubun_count(data["R1List"]), 1, name)


if __name__ == "__main__":
    unittest.main()
