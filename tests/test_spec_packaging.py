"""PyInstaller specから背景除去依存が除去されていることのテスト。"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


class TestSpecPackaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path("TksToKintone.spec")
        cls.source = cls.path.read_text(encoding="utf-8")

    def test_spec_exists_and_parses(self) -> None:
        self.assertTrue(self.path.exists())
        ast.parse(self.source)

    def test_background_removal_packages_and_model_are_absent(self) -> None:
        lowered = self.source.lower()
        for token in ("rembg", "onnxruntime", "u2net", "pymatting"):
            self.assertNotIn(token, lowered)

    def test_existing_assets_docs_and_analysis_collections_remain(self) -> None:
        self.assertIn("PROJECT_ROOT / 'assets'", self.source)
        self.assertIn("PROJECT_ROOT / 'docs'", self.source)
        self.assertIn("datas=extra_datas", self.source)
        self.assertIn("binaries=extra_binaries", self.source)
        self.assertIn("hiddenimports=extra_hiddenimports", self.source)

    def test_all_project_inputs_are_rooted_and_spec_is_location_independent(self) -> None:
        for token in ("PROJECT_ROOT / 'templates'", "PROJECT_ROOT / 'assets'",
                      "PROJECT_ROOT / 'installer'", "PROJECT_ROOT / 'app'"):
            self.assertIn(token, self.source)
        self.assertNotIn("('templates', 'templates')", self.source)
        self.assertNotIn("icon=['assets/app_icon.ico']", self.source)
        self.assertNotIn("version='installer/version_info.txt'", self.source)


if __name__ == "__main__":
    unittest.main()
