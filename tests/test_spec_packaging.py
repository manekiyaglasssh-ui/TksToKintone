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
        self.assertIn("('assets', 'assets')", self.source)
        self.assertIn("('docs', 'docs')", self.source)
        self.assertIn("datas=extra_datas", self.source)
        self.assertIn("binaries=extra_binaries", self.source)
        self.assertIn("hiddenimports=extra_hiddenimports", self.source)


if __name__ == "__main__":
    unittest.main()
