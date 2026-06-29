"""PyInstaller spec の同梱設定テスト（背景透過 rembg/pymatting 関連・要件6）。

spec は PyInstaller 実行時グローバル（Analysis 等）に依存し import/exec できないため、
ソースを読み取って必要な同梱設定が維持されているかを検証する。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[1] / "TksToKintone.spec"


class TestSpecPackaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SPEC_PATH.read_text(encoding="utf-8")

    def test_spec_exists_and_parses(self) -> None:
        # 構文として正しいこと（ast.parse は PyInstaller グローバル無しでも通る）。
        self.assertTrue(SPEC_PATH.exists())
        ast.parse(self.source)

    def test_pymatting_in_collect_all_targets(self) -> None:
        # collect_all を回すパッケージタプルに pymatting が含まれる（要件2）。
        match = re.search(r"for _pkg in \(([^)]*)\):", self.source)
        self.assertIsNotNone(match, "collect_all 対象ループが見つからない")
        targets = match.group(1)
        for pkg in ("'rembg'", "'onnxruntime'", "'PIL'", "'pymatting'"):
            self.assertIn(pkg, targets, f"{pkg} が collect_all 対象に含まれていない")

    def test_copy_metadata_imported_and_called_for_pymatting(self) -> None:
        # copy_metadata を import し、pymatting に対して呼ぶ（要件3）。
        self.assertIn("copy_metadata", self.source)
        self.assertRegex(self.source, r"from PyInstaller\.utils\.hooks import .*copy_metadata")
        # 配布名ループに pymatting が含まれ、copy_metadata がそのループ内で呼ばれる。
        match = re.search(r"for _dist_name in \(([^)]*)\):", self.source)
        self.assertIsNotNone(match, "copy_metadata 用の配布名ループが見つからない")
        dist_names = match.group(1)
        self.assertIn("'pymatting'", dist_names)
        self.assertIn("copy_metadata(_dist_name)", self.source)

    def test_metadata_uses_distribution_names(self) -> None:
        # metadata は配布名で指定する（PIL→Pillow, skimage→scikit-image）（要件3）。
        match = re.search(r"for _dist_name in \(([^)]*)\):", self.source)
        dist_names = match.group(1)
        self.assertIn("'Pillow'", dist_names)
        self.assertIn("'scikit-image'", dist_names)
        # import 名が誤って配布名ループに入っていないこと。
        self.assertNotIn("'PIL'", dist_names)
        self.assertNotIn("'skimage'", dist_names)

    def test_u2net_model_bundling_maintained(self) -> None:
        # u2net.onnx の同梱設定は維持されている（要件4）。
        self.assertIn("('assets/rembg/u2net.onnx', 'assets/rembg')", self.source)

    def test_docs_are_bundled_for_default_kakou_master(self) -> None:
        self.assertIn("('docs', 'docs')", self.source)

    def test_analysis_receives_extra_collections(self) -> None:
        # Analysis に extra_datas / extra_binaries / extra_hiddenimports が渡っている（要件9・10）。
        match = re.search(r"a = Analysis\((.*?)\n\)", self.source, re.DOTALL)
        self.assertIsNotNone(match, "Analysis 呼び出しが見つからない")
        analysis_args = match.group(1)
        self.assertRegex(analysis_args, r"datas\s*=\s*extra_datas")
        self.assertRegex(analysis_args, r"binaries\s*=\s*extra_binaries")
        self.assertRegex(analysis_args, r"hiddenimports\s*=\s*extra_hiddenimports")
        # collect_all の hidden は extra_hiddenimports へ集約する。
        self.assertIn("extra_hiddenimports += _hidden", self.source)

    def test_manual_metadata_helper_defined_and_used(self) -> None:
        # add_distribution_metadata 相当の処理が spec にある（要件1・6）。
        self.assertIn("import importlib.metadata", self.source)
        self.assertIn("def add_distribution_metadata", self.source)
        # dist._path を使って dist-info を extra_datas に追加している（要件6）。
        self.assertRegex(self.source, r"importlib_metadata\.distribution\(")
        self.assertRegex(self.source, r"Path\(dist\._path\)")
        self.assertRegex(self.source, r"extra_datas\.append\(\s*\(str\(dist_info_path\)")

    def test_pymatting_metadata_not_only_try_except_pass(self) -> None:
        # pymatting metadata を try/except pass だけに依存していない（要件1・6）。
        # 必須ループで add_distribution_metadata("pymatting") を呼んでいる。
        self.assertRegex(
            self.source,
            r"for required_dist in \([^)]*['\"]pymatting['\"][^)]*\):",
        )
        self.assertIn("add_distribution_metadata(required_dist)", self.source)
        # add_distribution_metadata は失敗時に RuntimeError を送出する（黙殺しない）。
        self.assertIn("raise RuntimeError", self.source)
        # 補助の copy_metadata ループは pass ではなく WARNING ログにしている。
        self.assertIn("WARNING: metadata not copied", self.source)


if __name__ == "__main__":
    unittest.main()
